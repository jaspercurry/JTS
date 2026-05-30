"""Home Assistant connection wizard at /ha/.

Walks the household through three states:

  1. No URL configured. Surfaces a "Find Home Assistant on this network"
     button (mDNS browse of `_home-assistant._tcp.local.`) AND a manual
     URL field side-by-side. Cross-subnet networks return zero mDNS
     hits — manual fallback is a first-class path, not polish.

  2. URL set, no/invalid token. Token-paste textarea with a deep link
     to HA's Profile → Security tab (`<HA URL>/profile/security`)
     where the user creates a Long-Lived Access Token. Validation
     hits `GET /api/` (cheap) — NOT `/api/conversation/process` (which
     could cost real money on LLM-backed HA agents).

  3. Connected. Status card with instance name + version, current
     conversation agent (optional override), "Test connection"
     affordance, and a "Disconnect" danger button.

Persistence: /var/lib/jasper/home_assistant.env (mode 0600), sourced
into jasper-voice via the EnvironmentFile= chain in
deploy/systemd/jasper-voice.service. Keys:

  JASPER_HA_URL          base URL, e.g. http://homeassistant.local:8123
  JASPER_HA_TOKEN        Long-Lived Access Token (JWT, ~180-220 chars)
  JASPER_HA_AGENT_ID     optional conversation.* entity to route through
  JASPER_HA_RECENT_URLS  JSON-encoded list of last 3 successfully-used
                         URLs; surfaces as a quick-pick in state 1 for
                         households who move between networks

Why no OAuth: HA's IndieAuth requires the client_id to be a publicly-
reachable URL with a `<link rel="redirect_uri">` tag. RFC 8628 device
flow was accepted (architecture #1299, Jan 2026) but the prerequisite
PR core#161715 was still open as of May 2026. LLAT paste is the
documented industry-standard path for headless HA integrations until
device-flow ships. See docs/HANDOFF-homeassistant.md.

URL surface (after nginx strips /ha/):
  GET  /              page render (one of three states)
  POST /discover      mDNS browse, JSON list of found instances
  POST /ready         lightweight readiness probe (1 HA call) — used
                       by the connected-state JS during the post-save
                       restart-poll window so we don't hammer HA with
                       3 calls per second for 15 seconds
  POST /verify        full validation (3 HA calls: /api/, /api/config,
                       /api/states) — used by Test Connection + the
                       agent-picker populate-on-load
  POST /save          write env file, restart jasper-voice
  POST /disconnect    delete env file, restart jasper-voice
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from .. import home_assistant as _ha_mod
from ._common import (
    DIALOG_CSS,
    NAV_BACK_HTML,
    PAGE_STYLE,
    begin_request,
    csrf_field_html,
    delete_env_file,
    dialog_helpers_js,
    mask_secret,
    read_env_file,
    read_form,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_see_other,
    verify_csrf,
    write_env_file,
)

logger = logging.getLogger(__name__)


HA_ENV_FILE = _ha_mod.HA_ENV_FILE

# mDNS service the official HA zeroconf integration advertises. Always
# fully-qualified with the trailing `.local.` per the python-zeroconf
# contract.
HA_SERVICE_TYPE = "_home-assistant._tcp.local."

# How long to browse for HA instances on the LAN. python-zeroconf
# re-broadcasts queries with backoff (1s, 2s, 4s); 4s captures the
# common PTR → SRV → TXT roundtrip on most home networks. Longer
# rarely surfaces new instances — past 5s you're paying latency for
# nothing.
DISCOVERY_TIMEOUT_SEC = 4.0

# Validation request timeout (GET /api/). Healthy HA responds in <100ms;
# 5s gives generous slack for slow Pi hardware or busy networks while
# still failing fast on dead/unreachable URLs.
VERIFY_TIMEOUT = httpx.Timeout(timeout=5.0, connect=3.0)

# How many recent URLs to keep. Three is enough for a multi-network
# household ("home", "office", "parents' house") without UI clutter.
RECENT_URLS_MAX = 3

# Env keys — re-exported from jasper.home_assistant so the wizard's
# state-file shape stays in sync with the daemon's config loader.
ENV_URL = _ha_mod.ENV_URL
ENV_TOKEN = _ha_mod.ENV_TOKEN
ENV_AGENT_ID = _ha_mod.ENV_AGENT_ID
ENV_VERIFY_SSL = _ha_mod.ENV_VERIFY_SSL
ENV_RECENT_URLS = _ha_mod.ENV_RECENT_URLS


# ---- URL normalization ------------------------------------------------------

def _bracket_ipv6(host: str) -> str:
    """Wrap an IPv6 literal in brackets for RFC 3986 URL embedding.

    `http://fe80::1:8123` is not a valid URL — the colons in the v6
    literal collide with the host:port separator. The bracketed form
    `http://[fe80::1]:8123` is unambiguous. Pass IPv4 or mDNS hostnames
    through unchanged. Pass already-bracketed literals through too
    (idempotent).
    """
    s = str(host)
    if not s or s.startswith("["):
        return s
    # IPv6 literals always contain `:`; v4 dotted-quads never do; mDNS
    # hostnames ("uuid.local.") never do. So a colon in `s` means v6.
    if ":" in s:
        return f"[{s}]"
    return s


def _normalize_url(raw: str) -> str:
    """Accept any of: 'homeassistant.local', 'homeassistant.local:8123',
    'http://homeassistant.local:8123', 'http://homeassistant.local:8123/',
    'http://homeassistant.local:8123/api', '192.168.1.42:8123', etc.
    Return a normalized base URL with scheme + host + port, no trailing
    slash, no /api suffix. Empty input returns ''.

    Default scheme is http (LAN-typical stock HA install). Default port
    8123 (HA's default) is appended ONLY when no port is present AND
    the URL looks like a bare hostname or IP — paste of an https URL
    keeps whatever port was there or none.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        s = "http://" + s
    parsed = urllib.parse.urlparse(s)
    netloc = parsed.netloc or parsed.path  # urlparse oddities on "host:8123"
    if not netloc:
        return ""
    # Reject obvious garbage: netloc must start with an alphanumeric or
    # an IPv6 bracket. "://not-a-url" parses through to a netloc that
    # looks like ":" which isn't a real host.
    host_part = netloc.split("@")[-1]  # strip any user@
    bare_host = host_part.split(":")[0].lstrip("[").rstrip("]")
    if not bare_host or not bare_host[0].isalnum():
        return ""
    # Add the default port when missing. urllib.parse.urlsplit doesn't
    # give us "port absent" cleanly — check the netloc for a colon.
    if ":" not in host_part.split("]")[-1]:  # not [ipv6]
        if parsed.scheme == "http":
            netloc = netloc + ":8123"
    return f"{parsed.scheme}://{netloc}".rstrip("/").removesuffix("/api").rstrip("/")


# ---- verify_ssl ------------------------------------------------------------

def _verify_ssl_from_state(state: dict[str, str]) -> bool:
    """Read JASPER_HA_VERIFY_SSL from the env-file state. Default True;
    the wizard only writes "0" when the user explicitly enables the
    self-signed-cert checkbox. Mirrors config.py's parsing."""
    raw = state.get(ENV_VERIFY_SSL, "1").strip()
    return raw not in ("0", "false", "no")


# ---- Recent-URLs persistence ------------------------------------------------

def _recent_urls(state: dict[str, str]) -> list[str]:
    raw = state.get(ENV_RECENT_URLS, "").strip()
    if not raw:
        return []
    try:
        urls = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(urls, list):
        return []
    return [u for u in urls if isinstance(u, str) and u][:RECENT_URLS_MAX]


def _push_recent_url(existing: list[str], url: str) -> list[str]:
    """Move-to-front. Most-recent first, deduped, capped at RECENT_URLS_MAX."""
    out = [u for u in existing if u != url]
    out.insert(0, url)
    return out[:RECENT_URLS_MAX]


# ---- mDNS discovery ---------------------------------------------------------

async def _discover_async(timeout: float) -> list[dict[str, str]]:
    """Browse the LAN for `_home-assistant._tcp.local.` instances and
    resolve each one to {name, host, port, location_name, version, url}.

    Returns at most one entry per mDNS service name. Cross-subnet
    households return [] (mDNS is link-local). Lazy zeroconf import
    keeps module load cheap when this endpoint isn't hit."""
    # Lazy import — same pattern as jasper/peering/discovery.py
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    found: dict[str, dict[str, str]] = {}
    resolve_tasks: list[asyncio.Task] = []
    aiozc = AsyncZeroconf()

    async def _resolve(service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        try:
            ok = await info.async_request(aiozc.zeroconf, 3000)
        except Exception:  # noqa: BLE001
            logger.debug("ha discover: resolve failed for %s", name, exc_info=True)
            return
        if not ok:
            return
        props_raw = info.properties or {}
        props: dict[str, str] = {}
        for k, v in props_raw.items():
            try:
                key = k.decode() if isinstance(k, bytes) else str(k)
                val = (v.decode() if isinstance(v, bytes) else str(v)) if v else ""
                props[key] = val
            except Exception:  # noqa: BLE001
                continue
        addrs = []
        try:
            addrs = list(info.parsed_addresses())
        except Exception:  # noqa: BLE001
            pass
        # SRV record is the reliable source for host:port — TXT fields
        # like internal_url / external_url / base_url are often empty
        # strings in practice.
        host = (info.server or "").rstrip(".") if info.server else ""
        port = info.port or 8123
        # Prefer an IPv4 address if available; falls back to mDNS host.
        # python-zeroconf returns IPv4 addrs first when both are present,
        # so addrs[0] biases toward v4 — important because some HA
        # installs advertise both and v4 is the LAN-default-friendly
        # path. If only v6 is available we use it AND bracket it per
        # RFC 3986 — `http://fe80::1:8123` is not a valid URL.
        target_host = _bracket_ipv6(addrs[0]) if addrs else host
        if not target_host:
            return
        url = _normalize_url(f"http://{target_host}:{port}")
        found[name] = {
            "name": name,
            "host": host,
            "port": str(port),
            "location_name": props.get("location_name", "") or "Home Assistant",
            "version": props.get("version", ""),
            "url": url,
        }

    def _on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
        # zeroconf calls us from its own thread; create_task schedules on
        # the running loop. We don't care about Removed events for a
        # one-shot scan.
        from zeroconf import ServiceStateChange
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            try:
                task = asyncio.get_running_loop().create_task(_resolve(service_type, name))
                resolve_tasks.append(task)
            except RuntimeError:
                # Loop may have closed already — fine, just drop the event.
                pass

    browser = AsyncServiceBrowser(
        aiozc.zeroconf,
        [HA_SERVICE_TYPE],
        handlers=[_on_change],
    )
    try:
        await asyncio.sleep(timeout)
    finally:
        try:
            await browser.async_cancel()
        except Exception:  # noqa: BLE001
            pass
        # Drain pending resolves so we collect everything the browser
        # surfaced before the timeout fired.
        if resolve_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*resolve_tasks, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                pass
        try:
            await aiozc.async_close()
        except Exception:  # noqa: BLE001
            pass

    return list(found.values())


def discover_sync(timeout: float = DISCOVERY_TIMEOUT_SEC) -> list[dict[str, str]]:
    """Sync wrapper around _discover_async — used by the (sync)
    BaseHTTPRequestHandler. Each call spins up its own event loop;
    /discover is rare enough that the loop-startup cost (~10 ms) is
    fine."""
    try:
        return asyncio.run(_discover_async(timeout))
    except Exception:  # noqa: BLE001
        logger.exception("ha discover: scan failed")
        return []


# ---- Validation against HA --------------------------------------------------

async def _verify_async(
    url: str, token: str, *, verify_ssl: bool = True,
) -> dict[str, Any]:
    """Probe GET /api/ to confirm URL + token combo. Returns a dict the
    handler ships back as JSON:
        {ok: bool, instance_name: str, version: str, error: str|null,
         agents: [{entity_id, name}, ...]}

    On success also pulls /api/config (for location_name + version) and
    /api/states (for the conversation.* agent list). Failures map to
    user-facing error strings — no stack traces.
    """
    url = _normalize_url(url)
    if not url:
        return {"ok": False, "error": "URL is empty or unparseable."}
    if not token.strip():
        return {"ok": False, "error": "Token is empty."}

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        timeout=VERIFY_TIMEOUT, headers=headers, verify=verify_ssl,
    ) as client:
        try:
            r = await client.get(url + "/api/")
        except httpx.ConnectError:
            return {
                "ok": False,
                "error": f"Couldn't reach Home Assistant at {url}. "
                          "Check the URL and that the speaker can see it on the network.",
            }
        except httpx.TimeoutException:
            return {
                "ok": False,
                "error": f"Connection to {url} timed out. "
                          "Check the URL and that the speaker can see it on the network.",
            }
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"Network error: {e}"}

        if r.status_code == 401:
            return {
                "ok": False,
                "error": "Token wasn't accepted. Make sure you copied the whole "
                         "token from Home Assistant — they're around 180 characters long.",
            }
        if r.status_code != 200:
            return {
                "ok": False,
                "error": f"Unexpected response from Home Assistant: HTTP {r.status_code}.",
            }
        try:
            body = r.json()
        except ValueError:
            return {
                "ok": False,
                "error": "Home Assistant returned an unexpected response body.",
            }
        if body.get("message") != "API running.":
            return {
                "ok": False,
                "error": "URL didn't look like a Home Assistant instance "
                         "(/api/ returned a different response).",
            }

        # Connection works. Best-effort fetch of /api/config + /api/states for
        # the success-state display. Failures here are non-fatal — we only
        # use them to enrich the success card.
        instance_name = "Home Assistant"
        version = ""
        agents: list[dict[str, str]] = []
        try:
            cr = await client.get(url + "/api/config")
            if cr.status_code == 200:
                cb = cr.json()
                instance_name = str(cb.get("location_name") or instance_name)
                version = str(cb.get("version") or "")
        except (httpx.HTTPError, ValueError):
            pass
        try:
            sr = await client.get(url + "/api/states")
            if sr.status_code == 200:
                for s in sr.json() or []:
                    eid = str(s.get("entity_id") or "")
                    if not eid.startswith("conversation."):
                        continue
                    attrs = s.get("attributes") or {}
                    name = str(attrs.get("friendly_name") or eid.split(".", 1)[-1])
                    agents.append({"entity_id": eid, "name": name})
        except (httpx.HTTPError, ValueError):
            pass

    return {
        "ok": True,
        "url": url,
        "instance_name": instance_name,
        "version": version,
        "agents": agents,
    }


def verify_sync(
    url: str, token: str, *, verify_ssl: bool = True,
) -> dict[str, Any]:
    """Sync wrapper around _verify_async — handler-side entry point."""
    try:
        return asyncio.run(_verify_async(url, token, verify_ssl=verify_ssl))
    except Exception as e:  # noqa: BLE001
        logger.exception("ha verify: unexpected error")
        return {"ok": False, "error": f"Internal error during validation: {e}"}


def ready_sync(
    url: str, token: str, *, verify_ssl: bool = True,
) -> dict[str, Any]:
    """Lightweight readiness probe — one HTTP call to GET /api/.

    Used by the connected-state JS during the post-save restart-poll
    window (up to 15 polls, 1 per second). Compared to verify_sync
    which makes 3 calls per invocation, this drops the worst-case HA
    request rate during the restart-poll from ~45 calls to ~15.

    Returns {ok: bool} — no rich data. The caller does one /verify
    at the end to populate the agent picker + instance name."""
    if not url or not token.strip():
        return {"ok": False}

    async def _probe() -> bool:
        client = _ha_mod.HAClient(url=url, token=token, verify_ssl=verify_ssl)
        try:
            return await client.healthcheck()
        finally:
            await client.aclose()

    try:
        return {"ok": asyncio.run(_probe())}
    except Exception:  # noqa: BLE001
        logger.debug("ha ready: probe failed", exc_info=True)
        return {"ok": False}


# ---- Page rendering ---------------------------------------------------------

def _profile_link(url: str) -> str:
    """Deep link to HA's Profile → Security tab where LLATs live. Built
    from the configured URL so the link Just Works for the household's
    actual install."""
    if not url:
        return ""
    return f"{url}/profile/security"


def _state_machine(state: dict[str, str]) -> str:
    """Return 'none' / 'partial' / 'connected' based on what's in the env file.
    Verified connectivity is NOT tested here (that's expensive); the
    'connected' state means both URL + token are set."""
    url = state.get(ENV_URL, "").strip()
    token = state.get(ENV_TOKEN, "").strip()
    if url and token:
        return "connected"
    if url and not token:
        return "partial"
    return "none"


def _render_index(state: dict[str, str], csrf_token: str = "", *, status_msg: str = "") -> bytes:
    machine = _state_machine(state)
    if machine == "connected":
        body = _state_connected_html(state, csrf_token)
    elif machine == "partial":
        body = _state_partial_html(state, csrf_token)
    else:
        body = _state_none_html(state, csrf_token)
    return _wrap("Home Assistant", body, status_msg=status_msg)


def _wrap(title: str, body: str, *, status_msg: str = "") -> bytes:
    """Local wrap_page replacement that adds the wizard-specific JS
    + CSS to the shared PAGE_STYLE."""
    lowered = status_msg.lower()
    if "error" in lowered or "fail" in lowered or "couldn't" in lowered:
        msg_class = "msg err"
    elif lowered.startswith(("saved", "connected", "cleared", "disconnected")):
        msg_class = "msg ok"
    else:
        msg_class = "msg"
    msg_html = (
        f'<p class="{msg_class}">{html.escape(status_msg)}</p>'
        if status_msg else ""
    )
    extra_css = """
      .discover-card { background: #f4f4f4; padding: 1em; border-radius: 8px; }
      .discover-list { display: flex; flex-direction: column; gap: 0.5em;
                       margin-top: 0.8em; }
      .discover-row {
        padding: 0.7em 0.9em; background: #fff; border: 1px solid #e6e6e6;
        border-radius: 6px; cursor: pointer; transition: border-color 0.15s;
      }
      .discover-row:hover { border-color: #1db954; }
      .discover-row .row-name { font-weight: 600; }
      .discover-row .row-url { color: #666; font-size: 0.9em;
                                font-family: ui-monospace, monospace; }
      .discover-empty { color: #888; font-size: 0.92em; font-style: italic;
                        margin-top: 0.6em; }
      .recent-urls { margin-top: 1em; }
      .recent-urls .recent-link {
        display: inline-block; margin: 0.2em 0.3em 0.2em 0;
        padding: 0.3em 0.6em; background: #fff; border: 1px solid #e6e6e6;
        border-radius: 4px; font-size: 0.88em;
        font-family: ui-monospace, monospace; color: #444; cursor: pointer;
      }
      .recent-urls .recent-link:hover { border-color: #1db954; color: #1db954; }
      .spinner {
        display: inline-block; width: 1em; height: 1em; margin-right: 0.3em;
        border: 2px solid #ccc; border-top-color: #1db954; border-radius: 50%;
        animation: spin 0.8s linear infinite; vertical-align: -2px;
      }
      @keyframes spin { to { transform: rotate(360deg); } }
      .status-grid {
        display: grid; grid-template-columns: max-content 1fr; gap: 0.5em 1em;
        margin: 1em 0;
      }
      .status-grid dt { font-weight: 600; color: #555; }
      .status-grid dd { margin: 0; font-family: ui-monospace, monospace; }
      .danger-zone {
        margin-top: 2em; padding: 1em; background: #fff5f5;
        border: 1px solid #fcc; border-radius: 6px;
      }
      .danger-zone p { margin: 0 0 0.6em; color: #844; font-size: 0.92em; }
      .voice-pack-card {
        margin: 1.4em 0; padding: 1em 1.2em;
        background: #f4f9f4; border: 1px solid #c9e3c9;
        border-radius: 8px;
      }
      .voice-pack-card h2 { margin: 0 0 0.6em; font-size: 1.05em; }
      .voice-pack-card p { margin: 0.4em 0; font-size: 0.94em; }
      .voice-pack-card code {
        background: #e6e6e6; padding: 0.1em 0.3em; border-radius: 3px;
        font-family: ui-monospace, monospace; font-size: 0.92em;
      }
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · JTS speaker</title>
<style>{PAGE_STYLE}{extra_css}{DIALOG_CSS}</style>
</head>
<body>
{NAV_BACK_HTML}
<h1>{html.escape(title)}</h1>
{msg_html}
<script>{dialog_helpers_js()}</script>
{body}
</body>
</html>""".encode()


def _state_none_html(state: dict[str, str], csrf_token: str = "") -> str:
    """Render state 1: no URL set. Discover + manual entry side-by-side
    plus any recent URLs the user previously connected to."""
    recent = _recent_urls(state)
    recent_html = ""
    if recent:
        chips = " ".join(
            f'<a class="recent-link" data-url="{html.escape(u)}">{html.escape(u)}</a>'
            for u in recent
        )
        recent_html = f"""
        <div class="recent-urls">
          <strong>Recent:</strong> {chips}
        </div>"""
    return f"""
<p class="sub">Connect your speaker to Home Assistant so it can control
your smart-home devices when you ask (lights, switches, thermostats,
scenes, scripts, automations).</p>

<h2>1. Choose a Home Assistant instance</h2>

<div class="discover-card">
  <button id="discover-btn" type="button">Find Home Assistant on this network</button>
  <span id="discover-status" class="hint" style="margin-left: 0.6em;"></span>
  <div id="discover-results" class="discover-list"></div>
  {recent_html}
</div>

<h2 style="margin-top: 1.4em;">Or enter the URL manually</h2>
<form id="manual-form" method="post" action="./save">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  <label for="url">Home Assistant URL</label>
  <input type="text" name="url" id="url" placeholder="http://homeassistant.local:8123"
         autocomplete="off" autocapitalize="off" spellcheck="false">
  <small>Common values: <code>homeassistant.local:8123</code>,
  <code>192.168.1.42:8123</code>, or whatever address you use to open
  Home Assistant in your browser.</small>
  <input type="hidden" name="token" value="">
  <input type="hidden" name="agent_id" value="">
  <p style="margin-top: 1em;">
    <button type="submit">Continue</button>
  </p>
</form>

<script>
  // Discover button: POST /discover, render the result list, fill the
  // manual URL when a row is clicked.
  const btn = document.getElementById('discover-btn');
  const status = document.getElementById('discover-status');
  const results = document.getElementById('discover-results');
  const urlField = document.getElementById('url');

  btn.addEventListener('click', async () => {{
    btn.disabled = true;
    status.innerHTML = '<span class="spinner"></span>Scanning the network…';
    results.innerHTML = '';
    try {{
      const r = await fetch('./discover', {{method: 'POST'}});
      const data = await r.json();
      const items = (data && data.instances) || [];
      if (items.length === 0) {{
        results.innerHTML =
          '<div class="discover-empty">No Home Assistant instances ' +
          'found on this network. Use the manual URL field below.</div>';
      }} else {{
        results.innerHTML = items.map(it =>
          '<div class="discover-row" data-url="' + it.url + '">' +
          '<div class="row-name">' + (it.location_name || 'Home Assistant') +
          (it.version ? ' <span class="hint">(' + it.version + ')</span>' : '') +
          '</div><div class="row-url">' + it.url + '</div></div>'
        ).join('');
        // Wire each row to the manual URL field
        results.querySelectorAll('.discover-row').forEach(row => {{
          row.addEventListener('click', () => {{
            urlField.value = row.dataset.url;
            urlField.focus();
            urlField.scrollIntoView({{behavior: 'smooth', block: 'center'}});
          }});
        }});
      }}
      status.textContent = '';
    }} catch (e) {{
      status.textContent = 'Scan failed: ' + e.message;
    }}
    btn.disabled = false;
  }});

  // Recent-URLs chips: clicking fills the manual URL field.
  document.querySelectorAll('.recent-link').forEach(el => {{
    el.addEventListener('click', () => {{
      urlField.value = el.dataset.url;
      urlField.focus();
    }});
  }});
</script>
"""


def _state_partial_html(state: dict[str, str], csrf_token: str = "") -> str:
    """Render state 2: URL set, token missing or invalid. Paste field
    with deep link to HA's profile page."""
    url = state.get(ENV_URL, "")
    profile_url = _profile_link(url)
    is_https = url.startswith("https://")
    verify_ssl = _verify_ssl_from_state(state)
    # HTTPS-only: show the self-signed-cert opt-in checkbox. Plain HTTP
    # has no TLS to verify. The form field is named `accept_self_signed`
    # to match the label semantics — checked means "yes, accept a
    # self-signed cert" (i.e. relax verify_ssl). Naming the field
    # `verify_ssl` would be backwards: a checked box would post the
    # opposite of what the user intends. See the _handle_save parser.
    ssl_block = ""
    if is_https:
        # Default state: checkbox UNCHECKED (verify_ssl=True is safe).
        # Pre-checked when the env file says verify_ssl is off so a
        # state-2 re-render preserves the user's prior choice.
        checked_attr = "checked" if not verify_ssl else ""
        ssl_block = f"""
  <label style="display: flex; align-items: center; gap: 0.5em;
                font-weight: 400; margin-top: 1em;">
    <input type="checkbox" name="accept_self_signed" value="on"
           {checked_attr}
           style="width: auto; margin: 0;">
    <span><strong>Accept a self-signed certificate.</strong>
    <span class="hint" style="display: block; margin-top: 0.2em;">
      Enable this only for Home Assistant instances on your own LAN
      that don't have a publicly-trusted certificate. Leaving it off
      is the safe default and works for Nabu Casa / Let's Encrypt /
      any other valid TLS.
    </span></span>
  </label>
  <input type="hidden" name="accept_self_signed_present" value="1">"""
    return f"""
<p class="sub">Step 2 of 2 — paste a token so the speaker can talk to
Home Assistant.</p>

<div style="background: #f4f4f4; padding: 0.7em 1em; border-radius: 8px;
            margin-bottom: 1em; font-family: ui-monospace, monospace;
            font-size: 0.92em; color: #555; word-break: break-all;">
  {html.escape(url)}
</div>

<form method="post" action="./save">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  <input type="hidden" name="url" value="{html.escape(url)}">

  <label for="token">Long-Lived Access Token</label>
  <textarea name="token" id="token" rows="3" style="width: 100%; padding: 0.5em;
            font-family: ui-monospace, monospace; font-size: 0.9em;
            border: 1px solid #bbb; border-radius: 4px; box-sizing: border-box;"
            autocomplete="off" autocapitalize="off" spellcheck="false"
            placeholder="eyJ0eXAiOi…  (~180 characters)"></textarea>
  <small>In Home Assistant, open
    <a href="{html.escape(profile_url)}" target="_blank" rel="noopener">{html.escape(profile_url)}</a>,
    scroll to the bottom, click <strong>Create Token</strong>, name it
    something like &ldquo;JTS Speaker&rdquo;, and paste the value here.
    The token is shown only once — copy it carefully.</small>
  {ssl_block}

  <p style="margin-top: 1em;">
    <button type="submit">Verify and save</button>
    <a class="btn secondary" href="./reset">Use a different URL</a>
  </p>
</form>
"""


# Self-contained prompt the user pastes into their coding agent
# (Claude Code, Cursor, Aider, ChatGPT with tool use, etc.) to audit
# their actual HA usage and build a personalized pack of sentence-
# trigger automations. Generic — assumes nothing about which agent
# runs it. Two placeholders ({HA_URL_PLACEHOLDER},
# {HA_TOKEN_PLACEHOLDER}) get substituted client-side: the default
# "Copy prompt" button keeps them as visible placeholders; the
# opt-in "Copy with credentials" button substitutes the live URL +
# token after a confirm dialog. Methodology mirrors what we learned
# pairing through this manually: classify firings by source so the
# proposal targets real voice intent, not every entity in HA.
VOICE_PACK_PROMPT = """\
# JTS smart speaker — Home Assistant voice setup

You're helping me wire my Home Assistant scenes/scripts to natural
voice phrases so my JTS smart speaker
(https://github.com/jaspercurry/JTS) can control them reliably.

## Why this is needed

JTS forwards smart-home commands to my Home Assistant's conversation
API. HA's default conversation agent is rule-based — it only
understands precise phrasing like "turn off the bedroom lights", not
"bedroom off" or "bedroom dark". The fix is **sentence-trigger
automations** in HA, one per phrase, that bypass HA's NLU and route
directly to a scene or script.

## Credentials

- HA URL: `{HA_URL_PLACEHOLDER}`
- Long-lived access token: `{HA_TOKEN_PLACEHOLDER}`

If either is a placeholder, ask me to paste the real value. The token
comes from `<HA URL>/profile/security` → "Long-Lived Access Tokens" →
Create.

## Step 0 — Confirm I've taken a backup

Before any writes, ask me to take an HA backup (Settings → System →
Backups → Create backup). 30 seconds, gives a clean rollback point.
Wait for my confirmation before continuing.

## Step 1 — Audit my actual usage (~21 days of logbook)

Authenticate every HA request with `Authorization: Bearer <token>`.

Pull the logbook:

```
GET <HA_URL>/api/logbook/<ISO-8601 start with +00:00 offset>?end_time=<ISO-8601 end>
```

(Default window is ~1 day if you omit `end_time` — supply both.)

Each entry has fields: `entity_id`, `name`, `when`, `domain` (e.g.
`'homekit'`), `context_domain`, `context_name`, `context_user_id`,
`message`. **Use these to classify each scene/script firing by
source:**

- `domain == 'homekit'` OR `context_domain == 'homekit'` → **HomeKit /
  Siri / Home app** (user voice today)
- `context_domain == 'automation'` AND `context_name` matches
  "Hue Remote" / "Remote" / "Btn" → **physical button**
- `context_domain == 'automation'` AND `context_name` matches "Motion"
  → **motion sensor** (skip — automatic, not voice-relevant)
- `context_domain == 'automation'` AND `context_name` matches
  "Door Unlocked" / "Unlock" → **door-unlock chain** (automatic)
- `context_domain == 'conversation'` → **existing HA voice** (possibly
  JTS already)
- `context_domain == 'alexa'` → **Alexa voice**
- `context_domain == 'mobile_app'` → **HA mobile app**
- Otherwise → **manual / dashboard / schedule**

Aggregate per scene/script: total fires, breakdown by source, friendly
name.

Also pull `/api/states` to get the canonical `friendly_name` for every
scene/script — HomeKit sometimes overwrites friendly_name to
"HomeKit" in logbook entries.

## Step 2 — Ask me clarifying questions

Based on what you find, ask me ~3–6 focused questions:

- **Layout** — any open-plan / shared rooms? ("Kitchen and living room
  are one space" changes whether "kitchen off" should kill both rooms
  or just kitchen-side fixtures.)
- **Naming — the load-bearing question.** For each scene/script that
  fires more than a handful of times, ask what I'd actually **say** to
  trigger it. HA's `friendly_name` often describes *what the lights
  do* (e.g. "Cook Mode" — bright kitchen for cooking) rather than *how
  I'd describe them* (e.g. "kitchen bright"). Walk the list with me
  and capture phrase → target pairs explicitly. This is the step that
  prevents "I said 'kitchen bright' and nothing happened" 48 hours
  after deploy.
- **Door unlocks** — should I add voice for unlocks? Default is **no**
  — voice unlock is a weaker security posture than phone + FaceID.
  Lock is safe to add.
- **Existing voice surfaces** — am I migrating from HomeKit/Alexa to
  JTS, or supplementing? (Affects which targets to prioritize.)
- **Sanity check** — anything from your audit that looks like a
  phrase I'd say but isn't obvious to you.

## Step 3 — Propose, then wait for explicit OK

Present the proposal in **two sections** so I can see both what you're
wiring AND what you're skipping. The second section is the one that
prevents gaps from shipping.

### Section A — Phrases I propose to wire

Grouped **by room/area** (not by priority — by where in the house
they live). For each row:

- Phrase aliases (3–6 variants — HA syntax supports `(a|b|c)` and
  `[optional]`; be liberal, natural speech varies)
- → target `entity_id`
- Fire count over the audit window + source breakdown
- Priority hint (P1 = heavy voice use today / P2 = occasional /
  P3 = obvious gap, no voice today)

Example:

```
BEDROOM
  "bedroom (medium|med|med bright)" / "set [the] bedroom to (medium|med)"
    → scene.warm_bedroom_med_bright_side_floor_lamps
    (20 fires: 11 Hue Remote, 9 HomeKit voice — P1)
```

### Section B — Scenes/scripts I did NOT wire (and why)

List **every other scene and script in my HA** with a 1-line reason
each:

- "Motion-only — fires N times via sensor, never voice-relevant"
- "Never fired in the audit window — likely unused"
- "Friendly name `<X>` is ambiguous — what phrase would you say?"
- "Has Hue Remote button but no clear verbal phrase — confirm before
  wiring"
- "Bundled into another target's phrases (e.g. covered by `kitchen
  off` which fires `<combined_script>`)"

For each in this section, ask whether I want it wired. **This is
where gaps surface**: e.g. a `Cook Mode` script I'd call "kitchen
bright" — if I don't see "kitchen bright" in Section A, this is
where I notice and ask you to add it before deploy.

---

**Wait for me to say "proceed" before deploying anything.** I will
likely edit either section first — moving entries from B → A,
renaming phrases, tightening aliases.

## Step 4 — Deploy via HA's config API

For each approved automation:

1. **Check for existing** — GET `/api/states`, find any automation
   whose `attributes.friendly_name` equals `"Voice: <Alias>"`. If
   found, reuse its `attributes.id` (a numeric string). If not,
   generate a fresh id, e.g. `str(int(time.time() * 1000) + index)`.
2. **POST** to `/api/config/automation/config/{numeric_id}` with body:

```json
{
  "alias": "Voice: <Alias>",
  "description": "Auto-generated by JTS voice pack. Target: <entity_id>",
  "mode": "single",
  "trigger": [{"platform": "conversation",
               "command": ["phrase1", "phrase2"]}],
  "condition": [],
  "action": [{
    "service": "scene.turn_on",
    "target": {"entity_id": "<entity_id>"},
    "metadata": {}
  }]
}
```

For scripts use `"service": "script.turn_on"`.

3. After all writes, POST `/api/services/automation/reload` with body
   `{}` so changes take effect immediately.
4. **Verify** by GETting each `/api/config/automation/config/{id}`
   back. Confirm alias, the conversation trigger commands list, and
   the action target entity_id.

## Step 5 — Hand off

Tell me which phrases to try, grouped by room. Format: "Say to JTS:
'bedroom dark' / 'open the blinds' / ...". **Do NOT trigger them
yourself** via `/api/conversation/process` — that fires the action
immediately, which moves real things in my home (lights, blinds,
locks). I'll test on JTS.

## Idempotency / re-runs

I may re-paste this prompt weeks from now after adding new HA
scenes/scripts. When you re-run:

- Detect existing `Voice:` automations from `/api/states` (filter
  where `friendly_name` starts with `"Voice: "`).
- Propose only **net-new** targets and **updates** to existing
  aliases. Don't re-create what's already there.
- If renaming a `Voice:` automation (alias change), **UPDATE in
  place** at its existing numeric id. **Never CREATE a new automation
  with the new name** — that orphans the old one with the same
  trigger phrases still active.

## DO NOT

- **Don't fire test actions** via `/api/conversation/process`. It
  executes; for blinds, locks, or scenes affecting rooms I might be
  in, that's disruptive. Verify by GET only.
- **Don't add voice for door unlocks** unless I explicitly say yes,
  even if my data shows heavy unlock use today. Lock is fine.
- **Don't modify any automation that doesn't start with `Voice:`.**
  My Hue Remote automations, motion sensors, and door-unlock chains
  are out of scope.
- **Don't propose voice for entities with zero fires** in the audit
  window. Low signal, high clutter.
- **Don't propose more than ~20 phrases** in one batch. If the pack
  is large, prioritize P1 first and offer to add more later.
- **Don't assume I want to replace HomeKit/Alexa.** Ask first.
- **Don't leave orphans on rename.** UPDATE in place at the existing
  numeric id; never CREATE under the new alias.

## API reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/` | Confirm token works |
| GET | `/api/logbook/{iso_start}?end_time={iso_end}` | Usage history |
| GET | `/api/states` | Find entities + existing `Voice:` automations |
| GET | `/api/config/automation/config/{numeric_id}` | Read a UI-managed automation |
| POST | `/api/config/automation/config/{numeric_id}` | Create or update |
| POST | `/api/services/automation/reload` (body `{}`) | Reload after writes |

Authenticate with `Authorization: Bearer <token>`. Conversation trigger
schema is `{"platform": "conversation", "command": [...]}` at the top
of the `trigger` list. HA's config API normalizes singular
`trigger`/`action` keys to plural `triggers`/`actions` on read —
accept both shapes when verifying.

---

Begin by acknowledging the brief and (if URL/token are placeholders)
asking me for the real values. Then proceed with the audit and report
what you find before proposing.
"""


def _state_connected_html(state: dict[str, str], csrf_token: str = "") -> str:
    """Render state 3: URL + token both set. We optimistically display
    the connection as healthy; the user can hit "Test connection" to
    verify against the live HA. Includes an advanced agent picker
    and a Disconnect button."""
    url = state.get(ENV_URL, "")
    token = state.get(ENV_TOKEN, "")
    agent_id = state.get(ENV_AGENT_ID, "")
    return f"""
<p class="sub">Connected. The speaker will delegate smart-home requests
to this Home Assistant instance.</p>

<dl class="status-grid">
  <dt>URL</dt>
  <dd>{html.escape(url)}</dd>
  <dt>Token</dt>
  <dd>{html.escape(mask_secret(token))}</dd>
  <dt>Agent</dt>
  <dd id="agent-display">{html.escape(agent_id) if agent_id else "(Home Assistant default)"}</dd>
</dl>

<p>
  <button id="test-btn" type="button">Test connection</button>
  <span id="test-status" class="hint" style="margin-left: 0.6em;"></span>
</p>

<div class="voice-pack-card">
  <h2>Make voice phrases work for your setup</h2>
  <p>HA's default voice agent only understands precise phrasing —
  "turn off the bedroom lights" works, but "bedroom off" or
  "bedroom dark" doesn't. The fix is sentence-trigger automations
  in HA, one per phrase, that route directly to your scenes and
  scripts.</p>
  <p>Rather than write these by hand, copy the prompt below into
  your coding agent of choice (Claude Code, Cursor, Aider, ChatGPT
  with tool use, etc.) and let it audit your actual usage, ask a
  few clarifying questions, and deploy the pack.</p>
  <p>
    <button id="copy-voice-prompt-btn" type="button">📋 Copy prompt</button>
    <button id="copy-voice-prompt-creds-btn" type="button" class="secondary">📋 Copy with HA credentials</button>
    <span id="copy-voice-prompt-feedback" class="copy-feedback"></span>
  </p>
  <p class="hint" style="margin-top: 0.8em;"><strong>Recommended:</strong>
  take an HA backup first (Settings → System → Backups → Create
  backup). The agent only creates automations prefixed
  <code>Voice:</code> and never modifies your existing ones, but
  a backup is cheap insurance.</p>
</div>

<details class="disclosure">
  <summary>Conversation agent (advanced)</summary>
  <div class="disclosure-body">
    <p>By default the speaker uses whichever conversation agent you've
    set as the default in Home Assistant — Settings → Voice Assistants
    → Default agent. Override here only when you want JTS to use a
    different agent than your other Home Assistant interfaces (e.g.
    a cheaper rule-based agent for the speaker, an LLM-backed agent
    for the dashboard).</p>

    <form method="post" action="./save">
      {csrf_field_html(csrf_token) if csrf_token else ''}
      <input type="hidden" name="url" value="{html.escape(url)}">
      <!-- Token deliberately omitted. The save handler keeps the
           existing token when the form's token field is empty. -->
      <input type="hidden" name="token" value="">
      <label for="agent_id">Agent override</label>
      <select name="agent_id" id="agent_id">
        <option value="">(use Home Assistant's default)</option>
      </select>
      <small>The list populates from your Home Assistant when you open
      this page. Pick an option and click Save.</small>
      <p style="margin-top: 0.8em;">
        <button type="submit">Save agent override</button>
      </p>
    </form>
  </div>
</details>

<div class="danger-zone">
  <p><strong>Disconnect.</strong> Removes the URL and token from this
  speaker. Smart-home commands will stop working until you reconnect.
  Doesn't change anything in Home Assistant itself.</p>
  <form method="post" action="./disconnect"
        onsubmit="return jtsConfirmSubmit(this, 'Disconnect this speaker from Home Assistant?', {{danger:true}});">
    {csrf_field_html(csrf_token) if csrf_token else ''}
    <button type="submit" class="danger">Disconnect</button>
  </form>
</div>

<script>
  const agentSelect = document.getElementById('agent_id');
  const agentDisplay = document.getElementById('agent-display');
  const currentAgent = {json.dumps(agent_id)};

  // restarting=1 marker → the page just landed from a successful
  // /save. jasper-voice restarts asynchronously (--no-block), so
  // /verify might 401 or hit a transient error for a few seconds.
  // Poll /verify with a short cadence + a 15 s ceiling, show a
  // "Configuring…" chip that clears once we see ok=true. Without
  // this UX, the user sees a green "Connected to X" banner and may
  // immediately try a voice command against a still-rebooting daemon.
  const urlParams = new URLSearchParams(window.location.search);
  const isRestarting = urlParams.get('restarting') === '1';

  // Two endpoints, two purposes:
  //   /ready  — one HA HTTP call (GET /api/). Used for the
  //             restart-poll loop where we just need a yes/no.
  //   /verify — three HA HTTP calls (/api/, /api/config,
  //             /api/states). Used for the initial agent-picker
  //             populate AND the final post-readiness enrichment.
  async function pollReady() {{
    try {{
      const r = await fetch('./ready', {{method: 'POST'}});
      const data = await r.json();
      return Boolean(data && data.ok);
    }} catch (e) {{
      return false;
    }}
  }}
  async function fullVerify() {{
    try {{
      const r = await fetch('./verify', {{method: 'POST'}});
      return await r.json();
    }} catch (e) {{
      return null;
    }}
  }}

  function populateAgents(data) {{
    if (!data || !data.ok) return;
    // Wipe any prior options except the default one.
    while (agentSelect.options.length > 1) {{
      agentSelect.remove(1);
    }}
    const agents = data.agents || [];
    for (const a of agents) {{
      const opt = document.createElement('option');
      opt.value = a.entity_id;
      opt.textContent = a.name + ' (' + a.entity_id + ')';
      if (a.entity_id === currentAgent) opt.selected = true;
      agentSelect.appendChild(opt);
    }}
    if (data.instance_name && data.instance_name !== 'Home Assistant') {{
      document.title = data.instance_name + ' · Home Assistant · JTS speaker';
    }}
  }}

  (async () => {{
    if (!isRestarting) {{
      // Regular page load: one /verify call to populate agents +
      // instance metadata.
      populateAgents(await fullVerify());
      return;
    }}
    // Restart-window UX: insert a chip near the test button and poll
    // /ready every 1 s for up to 15 s. The 15 s ceiling covers
    // Type=notify boot for jasper-voice (model load + cue regen +
    // realtime backend handshake on a Pi 5). One HA call per second
    // instead of three — easier on HA when the household has an
    // LLM-backed conversation agent that doesn't like burst traffic.
    const chip = document.createElement('div');
    chip.style.cssText = 'padding: 0.6em 0.9em; background: #fff4e0;' +
      'border: 1px solid #f0c060; border-radius: 6px; margin: 1em 0;' +
      'display: flex; align-items: center; gap: 0.5em;';
    chip.innerHTML = '<span class="spinner"></span>' +
      '<span>Configuring… the speaker is finishing its restart. ' +
      'Voice commands will work in a few seconds.</span>';
    document.querySelector('.status-grid').insertAdjacentElement('afterend', chip);

    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {{
      if (await pollReady()) {{
        // Daemon is back. One full /verify to pull the agent list +
        // instance name + version for the success chip.
        const data = await fullVerify();
        chip.style.background = '#e6f9ec';
        chip.style.borderColor = '#1db954';
        chip.innerHTML = '<span style="color: #14542a; font-weight: 600;">' +
          '✓ Ready.</span> Smart-home commands work now.';
        populateAgents(data);
        history.replaceState(null, '', window.location.pathname);
        return;
      }}
      await new Promise(r => setTimeout(r, 1000));
    }}
    // Timed out — show a friendly fallback.
    chip.style.background = '#fff5f5';
    chip.style.borderColor = '#fcc';
    chip.innerHTML = '<span style="color: #844;">⚠</span> ' +
      'The restart is taking longer than expected. Try ' +
      '<button id="late-test" type="button" ' +
      'style="background: #fff; color: #1db954; border: 1px solid #1db954;' +
      'padding: 0.2em 0.5em; border-radius: 4px; margin: 0 0.2em;">Test connection</button> ' +
      'in a moment.';
    document.getElementById('late-test').addEventListener('click', async () => {{
      populateAgents(await fullVerify());
    }});
    history.replaceState(null, '', window.location.pathname);
  }})();

  // Test connection button.
  const testBtn = document.getElementById('test-btn');
  const testStatus = document.getElementById('test-status');
  testBtn.addEventListener('click', async () => {{
    testBtn.disabled = true;
    testStatus.innerHTML = '<span class="spinner"></span>Checking…';
    try {{
      const r = await fetch('./verify', {{method: 'POST'}});
      const data = await r.json();
      if (data.ok) {{
        testStatus.innerHTML = '<span style="color: #14542a;">' +
          '✓ Connected to ' + (data.instance_name || 'Home Assistant') +
          (data.version ? ' (' + data.version + ')' : '') + '.</span>';
      }} else {{
        testStatus.innerHTML = '<span style="color: #a33;">' +
          (data.error || 'Connection failed.') + '</span>';
      }}
    }} catch (e) {{
      testStatus.textContent = 'Network error: ' + e.message;
    }}
    testBtn.disabled = false;
  }});

  // ---- Voice-pack prompt: two copy buttons -------------------------
  // Default button copies the prompt with visible <placeholders> for
  // URL + token (safer to share, includes brief paste-in instructions
  // for the agent). Credentials button fetches the live values from
  // ./credentials-for-copy after a confirm dialog that flags the
  // security trade-off, substitutes them in, and copies. The live
  // values are NEVER rendered into the page body — see
  // do_POST('/credentials-for-copy') for the rationale.
  // The template is JSON-encoded server-side so multi-line prose,
  // backticks, and curly braces survive embedding in a JS string
  // literal without quoting gymnastics.
  const VOICE_PROMPT_TEMPLATE = {json.dumps(VOICE_PACK_PROMPT)};
  const URL_PLACEHOLDER_FOR_SHARING = '<your HA URL, e.g. http://homeassistant.local:8123>';
  const TOKEN_PLACEHOLDER_FOR_SHARING =
    '<paste a long-lived access token from HA → Profile → Security → Long-Lived Access Tokens>';

  async function copyToClipboard(text) {{
    try {{
      await navigator.clipboard.writeText(text);
      return true;
    }} catch (e) {{
      // execCommand fallback for browsers that block writeText in
      // non-secure contexts (rare on LAN but cheap insurance).
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px;';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try {{ ok = document.execCommand('copy'); }} catch (e2) {{}}
      document.body.removeChild(ta);
      return ok;
    }}
  }}

  function showCopyFeedback(msg, ok) {{
    const fb = document.getElementById('copy-voice-prompt-feedback');
    fb.textContent = msg;
    fb.style.color = ok ? '#1db954' : '#a33';
    fb.classList.add('shown');
    setTimeout(() => fb.classList.remove('shown'), 3000);
  }}

  document.getElementById('copy-voice-prompt-btn')
    .addEventListener('click', async () => {{
      const text = VOICE_PROMPT_TEMPLATE
        .replace('{{HA_URL_PLACEHOLDER}}', URL_PLACEHOLDER_FOR_SHARING)
        .replace('{{HA_TOKEN_PLACEHOLDER}}', TOKEN_PLACEHOLDER_FOR_SHARING);
      const ok = await copyToClipboard(text);
      showCopyFeedback(
        ok ? '✓ Prompt copied — paste into your coding agent'
           : 'Copy failed — try selecting the page text manually',
        ok);
    }});

  document.getElementById('copy-voice-prompt-creds-btn')
    .addEventListener('click', async () => {{
      const ok = await jtsConfirm(
        'This will put your Home Assistant URL and a long-lived ' +
        'access token onto your clipboard.\\n\\n' +
        'Anyone with this token can control your Home Assistant. ' +
        'Do NOT:\\n' +
        '  • paste into a public chat or shared doc\\n' +
        '  • commit to a git repo\\n' +
        '  • share a screenshot of the prompt\\n\\n' +
        'Continue?');
      if (!ok) return;
      // Fetch credentials lazily. The page intentionally never holds
      // the live URL/token in DOM — see the server-side
      // /credentials-for-copy handler's docstring. CSRF token is the
      // same one the Disconnect form uses; we read it from the
      // hidden input rather than plumbing a separate meta tag.
      const csrfEl = document.querySelector('input[name="csrf_token"]');
      if (!csrfEl) {{
        showCopyFeedback('Could not fetch credentials (no CSRF token)', false);
        return;
      }}
      let creds;
      try {{
        const r = await fetch('./credentials-for-copy', {{
          method: 'POST',
          headers: {{'X-CSRF-Token': csrfEl.value}},
        }});
        if (!r.ok) {{
          showCopyFeedback(
            `Could not fetch credentials (server returned ${{r.status}})`,
            false);
          return;
        }}
        creds = await r.json();
      }} catch (e) {{
        showCopyFeedback('Could not fetch credentials — network error', false);
        return;
      }}
      const text = VOICE_PROMPT_TEMPLATE
        .replace('{{HA_URL_PLACEHOLDER}}', creds.url)
        .replace('{{HA_TOKEN_PLACEHOLDER}}', creds.token);
      const copied = await copyToClipboard(text);
      showCopyFeedback(
        copied ? '✓ Prompt + credentials copied — paste into your coding agent'
               : 'Copy failed — try the placeholder button instead',
        copied);
    }});
</script>
"""


# ---- Handler ----------------------------------------------------------------

def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _redirect(self, location: str) -> None:
            # Kept as a thin compat layer for callsites that don't carry
            # a flash message. New code paths use send_see_other from
            # _common with `flash=` instead.
            send_see_other(self, location)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: Any, *, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                state = read_env_file(cfg["state_path"])
                ctx = begin_request(self)
                self._send_html(_render_index(
                    state, ctx["csrf_token"], status_msg=ctx["flash"],
                ))
                return
            if path == "/reset":
                # Clear URL + token + agent (keep recent URLs) and go back
                # to state 1. Equivalent to "Use a different URL" link.
                state = read_env_file(cfg["state_path"])
                recent = _recent_urls(state)
                values: dict[str, str] = {}
                if recent:
                    values[ENV_RECENT_URLS] = json.dumps(recent)
                if values:
                    try:
                        write_env_file(cfg["state_path"], values, mode=0o600)
                    except OSError as e:
                        send_see_other(self, "./", flash=f"Could not reset: {e}")
                        return
                else:
                    delete_env_file(cfg["state_path"])
                send_see_other(self, "./")
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/discover":
                # Read-only network probe — no state change, no CSRF.
                instances = discover_sync(cfg.get("discovery_timeout", DISCOVERY_TIMEOUT_SEC))
                self._send_json({"instances": instances})
                return
            if path == "/ready":
                # Lightweight readiness — one HA call. Used by the
                # connected-state JS to poll for "is the daemon back
                # up + HA still reachable" without re-fetching the
                # agent list on every iteration.
                state = read_env_file(cfg["state_path"])
                self._send_json(ready_sync(
                    state.get(ENV_URL, ""), state.get(ENV_TOKEN, ""),
                    verify_ssl=_verify_ssl_from_state(state),
                ))
                return
            if path == "/verify":
                # /verify uses whatever URL+token are saved (no form
                # body) — the "Test connection" button and the agent
                # picker's on-load fetch both call this against the
                # persisted state. Read-only; no CSRF needed.
                state = read_env_file(cfg["state_path"])
                result = verify_sync(
                    state.get(ENV_URL, ""), state.get(ENV_TOKEN, ""),
                    verify_ssl=_verify_ssl_from_state(state),
                )
                self._send_json(result)
                return
            if path == "/credentials-for-copy":
                # Returns the live HA URL + token to the page's JS so
                # the "📋 Copy with HA credentials" button can substitute
                # them into the voice-pack prompt template and put the
                # result on the clipboard.
                #
                # Why a separate endpoint instead of inlining the values
                # into the page (the old design): inlining renders the
                # raw token into the connected-state HTML body, where
                # any browser extension can read it, screenshots capture
                # it, "view source" / "save page as" persist it, and a
                # stale tab keeps it in memory indefinitely. The new
                # shape fetches lazily on the user's click, holds the
                # token in a local const for one event-loop turn, copies,
                # and lets it fall out of scope. Cross-origin reads are
                # SOP-blocked by the browser; same-origin abuse is
                # gated by the CSRF header.
                #
                # CSRF: required (the response leaks credentials). The
                # connected-state page already renders the token in a
                # hidden input for the Disconnect form, so the JS reads
                # it from there and forwards as the X-CSRF-Token header.
                if not verify_csrf(self):
                    reject_csrf(self)
                    return
                state = read_env_file(cfg["state_path"])
                url_val = state.get(ENV_URL, "")
                token_val = state.get(ENV_TOKEN, "")
                if not (url_val and token_val):
                    # Defensive — shouldn't happen if the connected-state
                    # page even rendered (it only does when both are set).
                    self._send_json(
                        {"error": "credentials not set"}, status=400,
                    )
                    return
                self._send_json({"url": url_val, "token": token_val})
                return
            if path not in ("/save", "/disconnect"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not verify_csrf(self, form):
                reject_csrf(self)
                return
            if path == "/save":
                self._handle_save(form)
                return
            if path == "/disconnect":
                self._handle_disconnect()
                return

        def _handle_save(self, form: dict[str, str]) -> None:
            # form is pre-read by the POST router so it can verify CSRF
            # before we consume the body. All previous control flow
            # remains the same below.
            raw_url = (form.get("url") or "").strip()
            raw_token = (form.get("token") or "").strip()
            agent_id = (form.get("agent_id") or "").strip()
            # Read existing state once, reuse below for both the
            # ssl-flag-preservation case and the token-preservation
            # case.
            existing = read_env_file(cfg["state_path"])
            # The "Accept self-signed certificate" checkbox renders in
            # state 2 (the token-paste page) when the URL is https://.
            # Browser convention: absent = unchecked, "on" or any
            # non-empty value = checked. Semantics: checked means the
            # user opted into accepting a self-signed cert (i.e.
            # relax TLS verification → verify_ssl=False). Inverted
            # because the field is named after the user's action, not
            # the resulting verify_ssl value. When the form omits the
            # marker entirely (e.g. state 1's URL-only submit),
            # preserve whatever the env file already says — don't
            # silently re-enable verification.
            if "accept_self_signed_present" in form:
                verify_ssl = not bool(form.get("accept_self_signed"))
            else:
                verify_ssl = _verify_ssl_from_state(existing)

            normalized_url = _normalize_url(raw_url)
            if not normalized_url:
                send_see_other(
                    self, "./",
                    flash=(
                        "Couldn't parse that URL. Try "
                        "'http://homeassistant.local:8123' or "
                        "'http://192.168.1.42:8123'."
                    ),
                )
                return

            existing_token = existing.get(ENV_TOKEN, "").strip()
            # When the URL changes, drop the prior token — it belongs to a
            # different HA instance. The user has to re-paste.
            if existing_token and existing.get(ENV_URL, "") != normalized_url and not raw_token:
                values = {ENV_URL: normalized_url}
                # Keep recent URLs around
                recent = _recent_urls(existing)
                if recent:
                    values[ENV_RECENT_URLS] = json.dumps(recent)
                try:
                    write_env_file(cfg["state_path"], values, mode=0o600)
                except OSError as e:
                    send_see_other(self, "./", flash=f"Could not save: {e}")
                    return
                send_see_other(self, "./")
                return

            # When the user submitted state-1's form (URL only, token field
            # is empty hidden input), just persist the URL and bounce to
            # state 2 for the token paste.
            if not raw_token and not existing_token:
                values = {ENV_URL: normalized_url}
                recent = _recent_urls(existing)
                if recent:
                    values[ENV_RECENT_URLS] = json.dumps(recent)
                try:
                    write_env_file(cfg["state_path"], values, mode=0o600)
                except OSError as e:
                    send_see_other(self, "./", flash=f"Could not save: {e}")
                    return
                send_see_other(self, "./")
                return

            token = raw_token or existing_token
            # We have URL + token. Validate against the live HA before
            # persisting so we never write a broken config that would
            # leave the daemon talking to a dead URL.
            result = verify_sync(normalized_url, token, verify_ssl=verify_ssl)
            if not result.get("ok"):
                # Keep the URL in the env file so the user lands in state
                # 2 with a still-valid URL on the next render — only the
                # token gets dropped.
                values = {ENV_URL: normalized_url}
                # Persist verify_ssl so state-2's hint is accurate.
                if not verify_ssl:
                    values[ENV_VERIFY_SSL] = "0"
                recent = _recent_urls(existing)
                if recent:
                    values[ENV_RECENT_URLS] = json.dumps(recent)
                try:
                    write_env_file(cfg["state_path"], values, mode=0o600)
                except OSError as e:
                    send_see_other(self, "./", flash=f"Could not save: {e}")
                    return
                send_see_other(
                    self, "./",
                    flash=result.get("error", "Connection failed."),
                )
                return

            # Validation passed. Persist URL + token + agent + verify_ssl +
            # bump recent URLs.
            recent = _push_recent_url(_recent_urls(existing), normalized_url)
            values = {
                ENV_URL: normalized_url,
                ENV_TOKEN: token,
                ENV_AGENT_ID: agent_id,
                ENV_RECENT_URLS: json.dumps(recent),
            }
            # Only write the flag when explicitly off — keeps the env
            # file small and matches "absent = default safe value".
            if not verify_ssl:
                values[ENV_VERIFY_SSL] = "0"
            try:
                write_env_file(cfg["state_path"], values, mode=0o600)
            except OSError as e:
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return

            restart_voice_daemon()
            instance = result.get("instance_name") or "Home Assistant"
            version = result.get("version")
            label = f"{instance}" + (f" ({version})" if version else "")
            # restarting=1 is read by the connected-state page's JS — it
            # shows a "Configuring…" banner that auto-clears once /verify
            # returns OK (daemon back up + HA still reachable). The flash
            # text travels in the cookie now, not the URL.
            send_see_other(
                self, "./?restarting=1",
                flash=(
                    f"Connected to {label}. The speaker is restarting "
                    f"to pick up the change."
                ),
            )

        def _handle_disconnect(self) -> None:
            # Keep recent URLs so the user can quickly reconnect from
            # state 1 — same as wifi_setup's "Forget but remember".
            existing = read_env_file(cfg["state_path"])
            recent = _recent_urls(existing)
            if recent:
                try:
                    write_env_file(
                        cfg["state_path"],
                        {ENV_RECENT_URLS: json.dumps(recent)},
                        mode=0o600,
                    )
                except OSError as e:
                    send_see_other(self, "./", flash=f"Could not disconnect: {e}")
                    return
            else:
                delete_env_file(cfg["state_path"])
            restart_voice_daemon()
            send_see_other(
                self, "./",
                flash="Disconnected. The speaker is restarting.",
            )

    return Handler


def make_server(target, *, state_path: str = HA_ENV_FILE) -> ThreadingHTTPServer:
    """Used by jasper.web.__main__ to colocate this server with the
    other settings wizards inside one process. `target` is a
    socket/tuple/int per _systemd.make_http_server's contract."""
    from . import _systemd
    cfg = {"state_path": state_path}
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-homeassistant-web",
        description="Home Assistant connection wizard for the JTS speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_HA_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_HA_WEB_PORT", "8778")),
    )
    parser.add_argument(
        "--state", default=os.environ.get("JASPER_HA_FILE", HA_ENV_FILE),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port), state_path=args.state)
    logger.info("jasper-homeassistant-web listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
