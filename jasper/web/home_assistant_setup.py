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
  POST /verify        validate URL+token combo against GET /api/, JSON
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
    NAV_BACK_HTML,
    PAGE_STYLE,
    delete_env_file,
    mask_secret,
    read_env_file,
    read_form,
    restart_voice_daemon,
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
        target_host = addrs[0] if addrs else host
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


def _render_index(state: dict[str, str], *, status_msg: str = "") -> bytes:
    machine = _state_machine(state)
    if machine == "connected":
        body = _state_connected_html(state)
    elif machine == "partial":
        body = _state_partial_html(state)
    else:
        body = _state_none_html(state)
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
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · JTS speaker</title>
<style>{PAGE_STYLE}{extra_css}</style>
</head>
<body>
{NAV_BACK_HTML}
<h1>{html.escape(title)}</h1>
{msg_html}
{body}
</body>
</html>""".encode()


def _state_none_html(state: dict[str, str]) -> str:
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


def _state_partial_html(state: dict[str, str]) -> str:
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


def _state_connected_html(state: dict[str, str]) -> str:
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
        onsubmit="return confirm('Disconnect this speaker from Home Assistant?');">
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

  // Populate the agent picker by re-verifying on page load. Cheap (one
  // GET /api/states call to HA) and gives the user the live list. When
  // we're in the post-save restart window, this same fetch doubles as
  // the readiness probe.
  async function pollOnce() {{
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
      populateAgents(await pollOnce());
      return;
    }}
    // Restart-window UX: insert a chip near the test button and poll
    // /verify every 1 s for up to 15 s. The 15 s ceiling covers
    // Type=notify boot for jasper-voice (model load + cue regen +
    // realtime backend handshake on a Pi 5).
    const chip = document.createElement('div');
    chip.style.cssText = 'padding: 0.6em 0.9em; background: #fff4e0;' +
      'border: 1px solid #f0c060; border-radius: 6px; margin: 1em 0;' +
      'display: flex; align-items: center; gap: 0.5em;';
    chip.innerHTML = '<span class="spinner"></span>' +
      '<span>Configuring… the speaker is finishing its restart. ' +
      'Voice commands will work in a few seconds.</span>';
    document.querySelector('.status-grid').insertAdjacentElement('afterend', chip);

    const deadline = Date.now() + 15000;
    let last = null;
    while (Date.now() < deadline) {{
      last = await pollOnce();
      if (last && last.ok) {{
        chip.style.background = '#e6f9ec';
        chip.style.borderColor = '#1db954';
        chip.innerHTML = '<span style="color: #14542a; font-weight: 600;">' +
          '✓ Ready.</span> Smart-home commands work now.';
        populateAgents(last);
        // Clean URL so a refresh doesn't re-run the restart UX.
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
      populateAgents(await pollOnce());
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
</script>
"""


# ---- Handler ----------------------------------------------------------------

def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            # Per wake_setup.py / wifi_setup.py: nginx hangs on 303 without
            # an explicit Content-Length:0. Load-bearing.
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: Any, *, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)
            if path == "/":
                state = read_env_file(cfg["state_path"])
                self._send_html(_render_index(
                    state, status_msg=qs.get("msg", [""])[0],
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
                        self._redirect(
                            f"./?msg={urllib.parse.quote(f'Could not reset: {e}')}",
                        )
                        return
                else:
                    delete_env_file(cfg["state_path"])
                self._redirect("./")
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/discover":
                instances = discover_sync(cfg.get("discovery_timeout", DISCOVERY_TIMEOUT_SEC))
                self._send_json({"instances": instances})
                return
            if path == "/verify":
                # /verify uses whatever URL+token are saved (no form
                # body) — the "Test connection" button and the agent
                # picker's on-load fetch both call this against the
                # persisted state. For new-token validation we don't
                # need a separate endpoint; /save runs validation
                # implicitly via the connected-state on-load check.
                state = read_env_file(cfg["state_path"])
                result = verify_sync(
                    state.get(ENV_URL, ""), state.get(ENV_TOKEN, ""),
                    verify_ssl=_verify_ssl_from_state(state),
                )
                self._send_json(result)
                return
            if path == "/save":
                self._handle_save()
                return
            if path == "/disconnect":
                self._handle_disconnect()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _handle_save(self) -> None:
            form = read_form(self)
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
                self._redirect(
                    "./?msg=" + urllib.parse.quote(
                        "Couldn't parse that URL. Try 'http://homeassistant.local:8123' "
                        "or 'http://192.168.1.42:8123'.",
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
                    self._redirect(f"./?msg={urllib.parse.quote(f'Could not save: {e}')}")
                    return
                self._redirect("./")
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
                    self._redirect(f"./?msg={urllib.parse.quote(f'Could not save: {e}')}")
                    return
                self._redirect("./")
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
                    self._redirect(f"./?msg={urllib.parse.quote(f'Could not save: {e}')}")
                    return
                self._redirect(
                    "./?msg=" + urllib.parse.quote(result.get("error", "Connection failed.")),
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
                self._redirect(f"./?msg={urllib.parse.quote(f'Could not save: {e}')}")
                return

            restart_voice_daemon()
            instance = result.get("instance_name") or "Home Assistant"
            version = result.get("version")
            label = f"{instance}" + (f" ({version})" if version else "")
            # restarting=1 tells the connected-state page to show a
            # "Configuring…" banner that auto-clears once /verify
            # returns OK (daemon back up + HA still reachable).
            self._redirect(
                "./?restarting=1&msg=" + urllib.parse.quote(
                    f"Connected to {label}. The speaker is restarting to pick up the change.",
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
                    self._redirect(f"./?msg={urllib.parse.quote(f'Could not disconnect: {e}')}")
                    return
            else:
                delete_env_file(cfg["state_path"])
            restart_voice_daemon()
            self._redirect(
                "./?msg=" + urllib.parse.quote(
                    "Disconnected. The speaker is restarting.",
                ),
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
