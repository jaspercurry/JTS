# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tool catalog wizard at /assistant/tools/.

Browse + search the first-party voice tool packs and turn packs/tools on/off.
"Add" = enable, "remove" = disable. No install-from-store (that is the
later marketplace). A pack whose backend is not configured starts off; when
the user turns it on, the detail page surfaces the setup wizard.

This page READS the catalog jasper-voice wrote at /run/jasper/tools.json
and writes tool UI state to /var/lib/jasper/tool_state.env plus prompt
overrides to /var/lib/jasper/tool_prompt_overrides.json. It does NOT
import jasper.tools / build the registry — the socket-activated wizard
stays light (the transit lazy-import lesson); it uses jasper.tool_catalog_view
(json + tool_state only) to read + overlay.

Toggle stages, Apply commits — two-step on purpose:
  * POST /toggle just writes staged tool UI state. It does NOT restart voice:
    restarting the assistant drops any in-progress conversation and makes
    the speaker briefly deaf, so doing it per-toggle (silently, N times as
    you tick boxes) is user-hostile — and an unthrottled per-toggle restart
    can feed jasper-voice's StartLimitAction=reboot ladder. The page reads
    each tool's on/off back through the overlay (catalog_view) so the UI
    converges instantly without waiting on — or being raced by — a restart.
  * POST /apply restarts jasper-voice ONCE so the staged changes go live. It
    is rate-limited (and reports honestly when a restart won't happen — no
    provider / bonded follower) so a burst of Apply calls can't trip reboot.

Persistence: tool_state.env at mode 0644 (a list of names, not a secret).
Fail-safe: a missing/malformed file = nothing disabled (every tool ON).

URL surface (after nginx strips /assistant/tools/):
  GET  /             page render
  GET  /catalog.json catalog metadata + the fresh disabled-set overlaid
                     ({..., tools:[...], pending: bool})
  GET  /guide        static contributor/developer authoring guide
  GET  /pack/<id>    pack detail page render
  POST /toggle-pack  body {id: str, enabled: bool} — write pack state
  POST /toggle       body {name: str, enabled: bool} — write tool_state.env
                     (stage only, no restart)
  POST /prompt       body {name: str, prompt: str} — write prompt override
  POST /prompt-reset body {name: str} — delete prompt override
  POST /apply        restart jasper-voice once to apply staged changes
"""
from __future__ import annotations

import functools
import logging
import math
import os
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..env_load import TOOL_STATE_ENV_PATH
from ..log_event import log_event
from ..tool_prompt_overrides import DEFAULT_PATH as PROMPT_OVERRIDES_FILE
from ..tool_prompt_overrides import read_prompt_overrides, write_prompt_overrides
from ..tool_catalog_view import DEFAULT_CATALOG_PATH, catalog_view
from ..tool_state import ToolState, read_tool_state, write_tool_state
from ._common import (
    RestartOutcome,
    begin_request,
    bonded_follower_active,
    dispatch_get,
    dispatch_post,
    json_body,
    read_active_provider,
    read_json_body,
    resolve_samples,
    restart_voice_daemon,
    send_html_response,
    send_json_response,
)
from .chrome import canonical_header, canonical_page, json_island

logger = logging.getLogger(__name__)

TOOLS_PAGE_CSS_HREF = "/assets/tools/tools.css"
_JSON_BODY_LIMIT = 65536

# Minimum seconds between /assistant/tools/-driven voice restarts. jasper-voice has
# StartLimitBurst=20 / StartLimitIntervalSec=300 / StartLimitAction=reboot
# (crash-loop guard); 20s caps Apply-driven restarts at ~15 per 300s, safely
# under that ladder, so neither a key-mashing household member nor a scripted
# LAN client can reboot the speaker by spamming Apply. The staged change is
# already persisted, so a throttled Apply loses nothing — the in-flight (or a
# later) restart picks it up.
_APPLY_MIN_INTERVAL_SEC = 20.0

# Upper bound on a single user-edited tool prompt override. The override text
# ships verbatim to the realtime model, which has a hard instructions+tools
# token ceiling (~16k on OpenAI Realtime), and tool descriptions already press
# against it. 8000 chars (~2k tokens) sits well above the longest shipped
# description (~3.8k chars) so a legitimate edit is never rejected, while a
# pathological paste can't blow the model's budget.
MAX_PROMPT_OVERRIDE_CHARS = 8000

# Serializes the read-modify-write of tool_state.env and the apply timestamp
# across the ThreadingHTTPServer's request threads, so two concurrent toggles
# can't lose an update (last-writer-wins on the unserialized RMW).
_STATE_LOCK = threading.Lock()

# In-memory fail-CLOSED floor for the Apply rate-limit. The persisted
# timestamp is the cross-restart source of truth, but if it can't be written
# (read-only rootfs after an unclean shutdown, disk full, bad perms) the file
# read would return 0.0 and the throttle would collapse — letting an Apply
# retry loop feed jasper-voice's StartLimitAction=reboot ladder. This module
# global holds the last apply time within the (long-lived, socket-activated)
# wizard process, so the throttle still bounds the rate across a burst even
# when the file write fails.
_LAST_APPLY = [0.0]


def _toggle_index(catalog_path: str, state_path: str) -> dict[str, dict[str, Any]]:
    """name -> overlaid catalog entry, for toggle validation. Uses the same
    overlay the page sees so 'is this toggleable?' matches the rendered UI."""
    view = catalog_view(catalog_path, state_path)
    return {
        t["name"]: t
        for t in view.get("tools", [])
        if isinstance(t.get("name"), str)
    }


def _pack_index(catalog_path: str, state_path: str) -> dict[str, dict[str, Any]]:
    """pack id -> overlaid catalog pack, for pack-toggle validation."""
    view = catalog_view(catalog_path, state_path)
    return {
        p["id"]: p
        for p in view.get("packs", [])
        if isinstance(p, dict) and isinstance(p.get("id"), str)
    }


def _read_apply_ts(path: str) -> float:
    """Last /apply restart time (epoch seconds). Missing/unreadable/non-finite
    -> 0.0 (a missing file means 'no recent restart'; the in-memory floor in
    _handle_apply guards the fail-open direction). Reject nan/inf explicitly:
    float('nan') parses fine but breaks the `remaining > 0` comparison (nan
    fails open, inf never fires)."""
    try:
        with open(path, encoding="utf-8") as fh:
            v = float(fh.read().strip() or "0")
    except (OSError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _write_apply_ts(path: str, ts: float) -> None:
    from ..atomic_io import atomic_write_text
    try:
        atomic_write_text(path, f"{ts:.3f}\n", mode=0o644)
    except OSError as e:  # best-effort — the cap degrades open, never blocks
        logger.warning("could not write apply timestamp %s: %s", path, e)


def _index_html(csrf_token: str = "") -> bytes:
    # The page renders client-side from /assistant/tools/catalog.json, so the body is
    # just a mount point plus the ES module entry. canonical_page emits the
    # shared app.css link, the CSRF meta tag (read by main.js for the POST),
    # and the icon sprite. The module graph is served static + revalidated
    # from /assets/tools/js/ (the `location ~ \\.js$` block in nginx).
    guide_link = (
        '<a class="btn btn--ghost tools-guide-link" href="/assistant/tools/guide/" '
        'target="_blank" rel="noopener">Guide</a>'
    )
    body = f"""
{canonical_header("Tools", back_href="/assistant/", back_label="Assistant", right_html=guide_link)}
<main class="page">
  <div class="tools-search">
    <input type="search" id="tools-search" placeholder="Search tools&hellip;"
           autocomplete="off" aria-label="Search tools">
  </div>
  <div id="tools-list" aria-busy="true">
    <div class="info-card tool-empty"><p>Loading the tool catalog&hellip;</p></div>
  </div>
  <div class="tools-apply" id="tools-apply" hidden>
    <span class="tools-apply__note">Staged changes restart the assistant once.</span>
    <button type="button" class="btn" id="tools-apply-btn">Apply changes</button>
  </div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</main>
<script type="module" src="/assets/tools/js/main.js"></script>
"""
    return canonical_page(
        "Tools", body, csrf_token=csrf_token, page_css_href=TOOLS_PAGE_CSS_HREF,
    )


def _tool_name_from_path(path: str) -> str | None:
    """Return the URL-decoded tool slug from /tool/<name>, if valid."""
    prefix = "/tool/"
    if not path.startswith(prefix):
        return None
    raw = path[len(prefix):].strip("/")
    if not raw or "/" in raw:
        return None
    return urllib.parse.unquote(raw)


def _pack_id_from_path(path: str) -> str | None:
    """Return the URL-decoded pack id from /pack/<id>, if valid."""
    prefix = "/pack/"
    if not path.startswith(prefix):
        return None
    raw = path[len(prefix):].strip("/")
    if not raw or "/" in raw:
        return None
    return urllib.parse.unquote(raw)


def _detail_html(pack_id: str, csrf_token: str = "") -> bytes:
    body = f"""
{canonical_header("Tool pack", back_href="/assistant/tools/", back_label="Tools")}
<main class="page">
  <div id="tool-detail" aria-busy="true">
    <div class="info-card tool-empty"><p>Loading the tool pack&hellip;</p></div>
  </div>
  <div class="tools-apply" id="tools-apply" hidden>
    <span class="tools-apply__note">Staged changes restart the assistant once.</span>
    <button type="button" class="btn" id="tools-apply-btn">Apply changes</button>
  </div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
  {json_island("tool-detail-data", {"pack_id": pack_id})}
</main>
<script type="module" src="/assets/tools/js/detail.js"></script>
"""
    return canonical_page(
        "Tool", body, csrf_token=csrf_token, page_css_href=TOOLS_PAGE_CSS_HREF,
    )


def _get_detail(handler: BaseHTTPRequestHandler, pack_id: str) -> None:
    ctx = begin_request(handler)
    send_html_response(handler, _detail_html(pack_id, ctx["csrf_token"]))


@resolve_samples({
    "/pack/sample": functools.partial(_get_detail, pack_id="sample"),
    "/tool/sample": functools.partial(_get_detail, pack_id="tool:sample"),
})
def _detail_route(path: str) -> Callable[[BaseHTTPRequestHandler], None] | None:
    """Bind /pack/<id> — and the old /tool/<name> links, which render the same
    page — to a route callable, so do_GET resolves before it guards. None when
    `path` is neither.

    Hand-rolled rather than `prefix_route`: the id is a path segment this
    hook extracts and binds into the route, and a nested `/pack/a/b` is not
    this family at all."""
    pack_id = _pack_id_from_path(path)
    if pack_id is None:
        name = _tool_name_from_path(path)
        pack_id = None if name is None else "tool:" + name
    if pack_id is None:
        return None
    return functools.partial(_get_detail, pack_id=pack_id)


def _guide_html(csrf_token: str = "") -> bytes:
    body = f"""
{canonical_header("Tool authoring guide", back_href="/assistant/tools/", back_label="Tools")}
<main class="page">
  <article class="info-card tool-guide">
    <p class="tool-guide__lede">This is the house style for first-party and
      trusted-PR capability packs. It is documentation and UI guidance only:
      no marketplace, install flow, sandbox, secret broker, or untrusted-code
      runtime lives here.</p>

    <section class="tool-guide__section">
      <h2>Tool or Pack</h2>
      <p>A tool is one callable leaf the model can invoke. A capability pack
        is the copyable user-facing unit that owns metadata, setup, runtime
        clients, tools, and tests.</p>
      <ul>
        <li>Create a new pack when the capability has its own user-facing
          identity, setup state, account, backend, or runtime client graph.</li>
        <li>Extend an existing pack when the new callable shares that identity
          and setup contract, such as another Spotify or transit action.</li>
        <li>Add a label when the distinction is taxonomy only, such as a city,
          mode, topic, or search facet that does not own behavior.</li>
      </ul>
    </section>

    <section class="tool-guide__section">
      <h2>Runtime Boundary</h2>
      <p>The runtime path is source-neutral:
        <code>CapabilityPack</code> -&gt; <code>ToolDefinition</code> +
        <code>ToolExecutor</code> -&gt; registry -&gt; serializers, catalog,
        and <code>dispatch_tool()</code>. First-party code and trusted PRs use
        the same boundary.</p>
      <ul>
        <li><code>ToolDefinition</code> owns the provider-neutral schema,
          model-facing prompt, labels, timeout, and risk metadata.</li>
        <li><code>ToolExecutor</code> only runs the backing implementation.
          Dispatch owns timeout, logging, scalar wrapping, and error shaping.</li>
        <li>The tools wizard reads catalog JSON and wizard-owned state. It must
          not import tool modules or build the registry.</li>
      </ul>
    </section>

    <section class="tool-guide__section">
      <h2>Reference Shapes</h2>
      <ul>
        <li><code>time</code>: simple explicit definition and Python executor,
          no setup.</li>
        <li><code>weather</code>: API-backed tool with local setup state and a
          clear upstream-failure answer.</li>
        <li><code>spotify</code> and <code>playback</code>: source-backed music
          actions that share existing renderer/router services.</li>
        <li><code>transit</code>: deep pack with wizard-owned configuration,
          provider registries, and several related tools.</li>
        <li><code>home_assistant</code>: high-risk action pack with longer
          timeout and consequential-action confirmation.</li>
      </ul>
    </section>

    <section class="tool-guide__section">
      <h2>Prompt Writing</h2>
      <ul>
        <li>Start with a short purpose sentence.</li>
        <li>Use positive call conditions: "Call this when..." with concrete
          user utterances and parameter rules.</li>
        <li>Use "Do not call when..." only for load-bearing false positives,
          routing boundaries, or safety constraints.</li>
        <li>State the response shape and voice-answer style the model should
          speak from.</li>
        <li>Include the failure contract. On hard failure, return
          <code>{{"error": "short speakable sentence"}}</code>, not an empty
          success payload.</li>
      </ul>
    </section>

    <section class="tool-guide__section">
      <h2>Docstrings and Descriptions</h2>
      <p>Keep rich docstrings for humans. Use
        <code>llm_description</code> only when the model needs a shorter
        prompt than the engineer-facing docstring. The provider sees user
        override, then <code>llm_description</code>, then the default
        <code>ToolDefinition.description</code>.</p>
    </section>

    <section class="tool-guide__section">
      <h2>Metadata</h2>
      <ul>
        <li>Add useful <code>labels</code>; they are catalog facets, not model
          prompt text.</li>
        <li>Set <code>untrusted_output=True</code> for tools returning
          outsider-controlled text such as email, calendar, web, or chat
          content.</li>
        <li>Set <code>consequential=True</code> for real-world or irreversible
          actions.</li>
        <li>Raise <code>timeout</code> only when a backend is legitimately slow,
          and use <code>log_payload=False</code> or <code>log_args=False</code>
          when logs would otherwise contain household content or secrets.</li>
      </ul>
    </section>

    <section class="tool-guide__section">
      <h2>Setup Ownership</h2>
      <p>Per-pack setup belongs in the pack and its wizard-owned state under
        <code>/var/lib/jasper/</code>. Defaults should fail safe and loudly:
        missing setup means <code>needs_setup</code> or a speakable
        <code>error</code>, not a silent empty answer. Do not add central
        <code>Config</code> fields for per-pack configuration unless the value
        is truly cross-cutting.</p>
    </section>

    <section class="tool-guide__section">
      <h2>Tests</h2>
      <ul>
        <li>Cover pack registration, derived manifest/catalog output, and
          dispatch behavior.</li>
        <li>Pin setup gates, failure contracts, timeout/redaction choices, and
          risk metadata when they are load-bearing.</li>
        <li>Add or update a <code>tests/voice_eval/regression/</code> scenario
          when model-visible behavior changes.</li>
      </ul>
    </section>

    <section class="tool-guide__section">
      <h2>Non-goals</h2>
      <p>This phase does not add a sandbox, marketplace, install flow, secret
        broker, no-code executable builder, MCP bridge, or untrusted-code
        runtime. Those belong only after JTS runs code the maintainer has not
        personally vetted.</p>
    </section>
  </article>
</main>
"""
    return canonical_page(
        "Tool authoring guide",
        body,
        csrf_token=csrf_token,
        page_css_href=TOOLS_PAGE_CSS_HREF,
    )


def _pending(cfg: dict[str, Any]) -> bool:
    """Whether staged changes await Apply, read back through the overlay."""
    return bool(
        catalog_view(
            cfg["catalog_path"], cfg["state_path"], cfg["prompt_overrides_path"],
        ).get("pending")
    )


def _save_tool_state(handler: Any, path: str, state: ToolState) -> bool:
    """Write the staged tool state; False once a failed write is answered 500."""
    try:
        write_tool_state(path, state)
    except OSError as e:
        logger.exception("could not write tool_state.env")
        send_json_response(handler, {"error": f"save failed: {e}"}, status=500)
        return False
    return True


def _save_prompt_overrides(handler: Any, path: str, overrides: dict[str, str]) -> bool:
    """Write the prompt overrides; False once a failed write is answered 500."""
    try:
        write_prompt_overrides(path, overrides)
    except OSError as e:
        logger.exception("could not write tool prompt overrides")
        send_json_response(handler, {"error": f"save failed: {e}"}, status=500)
        return False
    return True


def _toggled(members: frozenset[str], key: str, member: bool) -> frozenset[str]:
    """``members`` with ``key`` added (``member``) or removed."""
    return members | {key} if member else members - {key}


def _get_index(handler: BaseHTTPRequestHandler) -> None:
    ctx = begin_request(handler)
    send_html_response(handler, _index_html(ctx["csrf_token"]))


def _get_catalog(cfg: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
    view = catalog_view(
        cfg["catalog_path"], cfg["state_path"], cfg["prompt_overrides_path"],
    )
    send_json_response(handler, view, status=200)


def _get_guide(handler: BaseHTTPRequestHandler) -> None:
    ctx = begin_request(handler)
    send_html_response(handler, _guide_html(ctx["csrf_token"]))


def _post_toggle(cfg: dict[str, Any], handler: Any, body: dict[str, Any]) -> None:
    name = body.get("name")
    enabled = body.get("enabled")
    if not isinstance(name, str) or not isinstance(enabled, bool):
        send_json_response(
            handler, {"error": "name (str) and enabled (bool) required"}, status=400,
        )
        return
    # Only configured tools (active/off in the overlaid catalog) are
    # toggleable: reject unknown names (a crafted POST can't poison
    # the disabled-set with garbage) AND needs_setup tools (no UI
    # control — toggling one just writes a meaningless entry).
    index = _toggle_index(cfg["catalog_path"], cfg["state_path"])
    entry = index.get(name)
    if entry is None:
        send_json_response(handler, {"error": "unknown tool"}, status=400)
        return
    if entry.get("disabled_by_pack") is True:
        send_json_response(handler, {"error": "pack disabled"}, status=400)
        return
    if entry.get("status") not in ("active", "off"):
        send_json_response(handler, {"error": "tool not configured"}, status=400)
        return
    with _STATE_LOCK:
        state = read_tool_state(cfg["state_path"])
        disabled = _toggled(state.disabled_tools, name, not enabled)
        if disabled != state.disabled_tools:
            staged = replace(state, disabled_tools=disabled)
            if not _save_tool_state(handler, cfg["state_path"], staged):
                return
            log_event(
                logger, "tools.toggle",
                name=name, enabled=enabled,
                client=handler.address_string(),
            )
    # Staged only — no restart. The page re-reads the overlay so the
    # UI converges immediately; `pending` tells it to offer Apply.
    send_json_response(
        handler,
        {"ok": True, "name": name, "enabled": enabled, "pending": _pending(cfg)},
        status=200,
    )


def _post_toggle_pack(cfg: dict[str, Any], handler: Any, body: dict[str, Any]) -> None:
    pack_id = body.get("id")
    enabled = body.get("enabled")
    if not isinstance(pack_id, str) or not isinstance(enabled, bool):
        send_json_response(
            handler, {"error": "id (str) and enabled (bool) required"}, status=400,
        )
        return
    index = _pack_index(cfg["catalog_path"], cfg["state_path"])
    entry = index.get(pack_id)
    if entry is None:
        send_json_response(handler, {"error": "unknown pack"}, status=400)
        return
    singleton_tool = entry.get("singleton_tool_name")
    setup_only = (
        int(entry.get("setup_required_count") or 0)
        >= int(entry.get("tool_count") or 0)
        and int(entry.get("tool_count") or 0) > 0
    )
    with _STATE_LOCK:
        state = read_tool_state(cfg["state_path"])
        if setup_only:
            staged = replace(state, setup_enabled_packs=_toggled(
                state.setup_enabled_packs, pack_id, enabled,
            ))
        elif isinstance(singleton_tool, str):
            staged = replace(state, disabled_tools=_toggled(
                state.disabled_tools, singleton_tool, not enabled,
            ))
        else:
            staged = replace(state, disabled_packs=_toggled(
                state.disabled_packs, pack_id, not enabled,
            ))
        if staged != state:
            if not _save_tool_state(handler, cfg["state_path"], staged):
                return
            log_event(
                logger, "tools.toggle_pack",
                pack=pack_id, enabled=enabled,
                singleton_tool=(
                    singleton_tool
                    if isinstance(singleton_tool, str)
                    else None
                ),
                client=handler.address_string(),
            )
    send_json_response(
        handler,
        {
            "ok": True,
            "id": pack_id,
            "enabled": enabled,
            "pending": _pending(cfg),
            "setup_required": setup_only,
        },
        status=200,
    )


def _post_prompt(cfg: dict[str, Any], handler: Any, body: dict[str, Any]) -> None:
    name = body.get("name")
    prompt = body.get("prompt")
    if not isinstance(name, str) or not isinstance(prompt, str):
        send_json_response(
            handler, {"error": "name (str) and prompt (str) required"}, status=400,
        )
        return
    if not prompt.strip():
        send_json_response(handler, {"error": "prompt cannot be blank"}, status=400)
        return
    if len(prompt) > MAX_PROMPT_OVERRIDE_CHARS:
        send_json_response(
            handler,
            {"error": f"prompt too long (max {MAX_PROMPT_OVERRIDE_CHARS} characters)"},
            status=400,
        )
        return
    index = _toggle_index(cfg["catalog_path"], cfg["state_path"])
    if name not in index:
        send_json_response(handler, {"error": "unknown tool"}, status=400)
        return
    # Editing a prompt back to the exact code default is a reset, not a
    # customization: storing it would leave prompt_customized() true
    # forever (the "customized" badge would never clear without an
    # explicit Reset). Treat an equal-to-default save as deleting the
    # override. default_description rides the overlaid catalog entry.
    default_text = (index[name].get("default_description") or "").strip()
    reset_to_default = prompt.strip() == default_text
    with _STATE_LOCK:
        overrides = read_prompt_overrides(cfg["prompt_overrides_path"])
        if reset_to_default:
            changed = overrides.pop(name, None) is not None
        else:
            changed = overrides.get(name) != prompt
            if changed:
                overrides[name] = prompt
        if changed:
            if not _save_prompt_overrides(handler, cfg["prompt_overrides_path"], overrides):
                return
            log_event(
                logger, "tools.prompt_override_saved",
                name=name, reset=reset_to_default,
                client=handler.address_string(),
            )
    send_json_response(
        handler, {"ok": True, "name": name, "pending": _pending(cfg)}, status=200,
    )


def _post_prompt_reset(cfg: dict[str, Any], handler: Any, body: dict[str, Any]) -> None:
    name = body.get("name")
    if not isinstance(name, str):
        send_json_response(handler, {"error": "name (str) required"}, status=400)
        return
    index = _toggle_index(cfg["catalog_path"], cfg["state_path"])
    if name not in index:
        send_json_response(handler, {"error": "unknown tool"}, status=400)
        return
    with _STATE_LOCK:
        overrides = read_prompt_overrides(cfg["prompt_overrides_path"])
        if name in overrides:
            del overrides[name]
            if not _save_prompt_overrides(handler, cfg["prompt_overrides_path"], overrides):
                return
            log_event(
                logger, "tools.prompt_override_reset",
                name=name, client=handler.address_string(),
            )
    send_json_response(
        handler, {"ok": True, "name": name, "pending": _pending(cfg)}, status=200,
    )


def _post_apply(cfg: dict[str, Any], handler: Any) -> None:
    # Mirror restart_voice_daemon's skip conditions so the response is
    # HONEST about whether a restart will actually happen — never an
    # ok-banner promising an effect the server knowingly won't deliver.
    if not read_active_provider():
        send_json_response(
            handler,
            {
                "restarted": False,
                "reason": "no_provider",
                "message": "Saved. Choose a voice provider at "
                "/assistant/voice/ to start the assistant.",
            },
            status=200,
        )
        return
    if bonded_follower_active():
        send_json_response(
            handler,
            {
                "restarted": False,
                "reason": "bonded",
                "message": "Saved. Changes apply when this speaker "
                "leaves the stereo pair.",
            },
            status=200,
        )
        return
    now = time.time()
    with _STATE_LOCK:
        # max(persisted, in-memory) so a failed ts write can't open
        # the throttle within this process (fail-closed floor).
        last = max(_read_apply_ts(cfg["apply_ts_path"]), _LAST_APPLY[0])
        remaining = _APPLY_MIN_INTERVAL_SEC - (now - last)
        if remaining > 0:
            send_json_response(
                handler,
                {
                    "restarted": False,
                    "reason": "throttled",
                    "retry_after": int(remaining) + 1,
                    "message": "The assistant is already restarting — "
                    "your changes are saved and will apply "
                    "shortly.",
                },
                status=200,
            )
            return
        _LAST_APPLY[0] = now
        _write_apply_ts(cfg["apply_ts_path"], now)
    log_event(logger, "tools.apply", client=handler.address_string())
    # jasper-voice re-filters the registry against tool_state.env on
    # restart (and re-writes the catalog JSON).
    outcome = restart_voice_daemon()
    if outcome is not RestartOutcome.RAN:
        send_json_response(
            handler,
            {
                "restarted": False,
                "reason": outcome.name.lower(),
                "message": "Saved, but the assistant did not restart — "
                "save again, or check System.",
            },
            status=200,
        )
        return
    send_json_response(
        handler,
        {
            "restarted": True,
            "message": "Restarting the assistant to apply your changes…",
        },
        status=200,
    )


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    cfg.setdefault(
        "apply_ts_path",
        os.path.join(os.path.dirname(cfg["state_path"]), "tools_apply.ts"),
    )
    cfg.setdefault("prompt_overrides_path", PROMPT_OVERRIDES_FILE)

    class Handler(BaseHTTPRequestHandler):
        # Abort a connection whose client stalls mid-request (e.g. a lying
        # Content-Length that never sends the body) instead of pinning a
        # request thread indefinitely. Defense in depth — the wizard's idle
        # watchdog already reaps stuck threads, but this fails the read fast.
        timeout = 30

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        # nginx strips the /assistant/tools/ prefix so we see paths like "/"
        # and "/catalog.json".
        def do_GET(self) -> None:  # noqa: N802
            dispatch_get(self, _GET_ROUTES, resolve=_detail_route)

        def do_POST(self) -> None:  # noqa: N802
            dispatch_post(self, _POST_ROUTES, guard="header")

        def _read_json(self) -> dict[str, Any] | None:
            """Parse the request body, answering 400 and returning None on a
            malformed one so `json_body` never dispatches it to a route."""
            parsed, err = read_json_body(self, max_bytes=_JSON_BODY_LIMIT)
            if err is not None:
                send_json_response(self, {"error": err}, status=400)
                return None
            return parsed

    # The tables stay local to this closure because they bind each route
    # body to `cfg`. GET's two detail routes (/pack/<id>, /tool/<name>) carry
    # a path parameter, so `_detail_route` binds them through the seam's
    # `resolve=` hook instead of a table key.
    _GET_ROUTES = {
        "/": _get_index,
        "/catalog.json": functools.partial(_get_catalog, cfg),
        "/guide": _get_guide,
    }
    _POST_ROUTES = {
        "/toggle": json_body(functools.partial(_post_toggle, cfg)),
        "/toggle-pack": json_body(functools.partial(_post_toggle_pack, cfg)),
        "/prompt": json_body(functools.partial(_post_prompt, cfg)),
        "/prompt-reset": json_body(functools.partial(_post_prompt_reset, cfg)),
        "/apply": functools.partial(_post_apply, cfg),
    }

    return Handler


def make_server(
    target,
    *,
    catalog_path: str = DEFAULT_CATALOG_PATH,
    state_path: str = TOOL_STATE_ENV_PATH,
    prompt_overrides_path: str = PROMPT_OVERRIDES_FILE,
    apply_ts_path: str | None = None,
) -> ThreadingHTTPServer:
    """Build the tools wizard server. `target` is a socket / (host, port)
    tuple / int port per systemd.make_http_server's contract."""
    from ..platform import systemd
    if apply_ts_path is None:
        apply_ts_path = os.path.join(os.path.dirname(state_path), "tools_apply.ts")
    return systemd.make_http_server(
        target,
        _make_handler({
            "catalog_path": catalog_path,
            "state_path": state_path,
            "prompt_overrides_path": prompt_overrides_path,
            "apply_ts_path": apply_ts_path,
        }),
    )
