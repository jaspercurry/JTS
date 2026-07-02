# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Microphone + wake page at /wake/.

The page is backend-driven. `jasper-control` owns the microphone/AEC
truth, including the user-facing `mic_settings` view model layered over
the storage/runtime profile ids. Browser JavaScript renders that model
and posts intent back through the existing proxy routes; it does not
decide what hardware can safely run.

Four stacked sections, one page:

  1. **Microphone** — compact read-only hardware/capability summary
     hydrated from `/aec.mic_settings.mic`.

  2. **Echo cancellation** — task-oriented profile choices:
     best available, hardware echo cancellation, software echo
     cancellation, or direct mic. These still write the canonical
     `JASPER_AUDIO_INPUT_PROFILE` ids behind the scenes.

  3. **Wake word** — wake model picker + sensitivity. This is separate
     from echo processing; changing the wake word should not require the
     household to reason about AEC engines.

  4. **Advanced wake fusion** — collapsed diagnostics/corpus controls
     for raw, DTLN, chip beam scoring, and DAC validation mode. These are
     intentionally not first-run controls.

Both sections write the same /var/lib/jasper/wake_model.env so a
model save preserves any JASPER_WAKE_THRESHOLD the slider wrote, and
vice versa. Restarts ride a shared mechanism: layer toggles go through
jasper-control's reconciler (which restarts jasper-aec-bridge +
jasper-voice as needed); sensitivity and model saves kick
jasper-voice directly.

Persistence: wake_model.env at mode 0644 (path + a number, not a
secret). The jasper-voice systemd unit sources it AFTER
/etc/jasper/jasper.env so wizard-written values win over operator-
managed defaults — same pattern as voice_provider.env.

Presentation runs on the canonical design system: the page renders
through ``canonical_page`` + ``canonical_header`` (app.css from
/assets/), and its behaviour lives in the ES module at
deploy/assets/wake/js/main.js (loaded as ``type="module"``). The
page-specific layer-row / slider / model-picker visuals are in
deploy/assets/wake/wake.css. There is no inline ``<script>``.

URL surface (after nginx strips the /wake/ prefix):
  GET  /                page render
  GET  /detection.json  proxy jasper-control /aec — includes the
                        backend-owned mic_settings view model
  POST /firmware/update proxy jasper-control /aec/firmware/update — start
                        a required mic firmware update job
  POST /profile         body {profile: str} — set canonical input profile
  POST /layer/aec       body {enabled: bool} — legacy compatibility shim
                        for the old software-AEC3 toggle; not rendered
  POST /layer/raw       body {enabled: bool} — set chip-direct leg
  POST /layer/dtln      body {enabled: bool} — set DTLN leg
  POST /layer/chip_aec_150 body {enabled: bool} — set optional 150° chip
                        beam wake detector
  POST /layer/chip_aec_210 body {enabled: bool} — set optional 210° chip
                        beam wake detector
  POST /layer/chip_aec  body {enabled: bool} — legacy compatibility shim
                        that sets both optional chip beam detectors
  POST /sensitivity     body {value: float}  — set wake threshold
  POST /save            write wake_model.env + restart voice daemon

The /layer/* and /sensitivity routes proxy to jasper-control's
/aec/{toggle,leg,threshold} on 127.0.0.1:8780. Wizard-side URLs use
the user-facing vocabulary (layers, sensitivity) so the surface
reads as a coherent wake page rather than leaking the AEC internals.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..audio_input_view import profile_choice_specs, valid_profile_ids
from ..atomic_io import locked_update_env_file
from ..log_event import log_event
from .. import wake_models
from ._common import (
    pair_banner_html,
    DEFAULT_CONTROL_BASE,
    begin_request,
    canonical_header,
    canonical_page,
    csrf_field_html,
    forward_control_token_headers,
    proxy_get,
    proxy_post,
    read_env_file,
    read_form,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_proxy_json,
    send_see_other,
    toggle_html,
    guard_read_request,
    guard_mutating_request,
)

logger = logging.getLogger(__name__)


WAKE_MODEL_FILE = wake_models.WAKE_MODEL_FILE

# Cache-busted link to this page's own stylesheet. canonical_page() links
# app.css itself; page CSS rides in via page_css_href.
WAKE_PAGE_CSS_HREF = "/assets/wake/wake.css"

# Compiled-in default mirrored from jasper/config.py:_validate.
# Tests + `_active_threshold` reference it; the slider's min/max/step
# constants live inline in the rendered HTML (no Python tests exercise
# them so a duplicate Python constant would just rot).
DEFAULT_WAKE_THRESHOLD = 0.3


# ----------------------------------------------------------------------
# State helpers — pure where possible.
# ----------------------------------------------------------------------


def _load_state(path: str = WAKE_MODEL_FILE) -> dict[str, str]:
    """Read the wizard-managed env file ({} on missing/blank)."""
    return read_env_file(path)


def _active_model(state: dict[str, str]) -> str:
    """The wake-model string the daemon would actually load right now.

    Order of preference:
      1. wake_model.env (wizard-managed)
      2. process env (systemd already merged /etc/jasper/jasper.env)
      3. compiled-in default ("hey_jarvis")
    """
    val = state.get("JASPER_WAKE_MODEL", "").strip()
    if val:
        return val
    return os.environ.get("JASPER_WAKE_MODEL", "").strip() or "hey_jarvis"


def _active_threshold(state: dict[str, str]) -> float:
    """The wake threshold the daemon would actually load right now.

    Same precedence ladder as `_active_model`: wizard-managed env file
    wins over process env (systemd-merged /etc/jasper/jasper.env) wins
    over the compiled default. Malformed values fall through to the
    next layer rather than crashing the page — the daemon's validator
    catches genuinely-broken values at startup.
    """
    for source in (state.get("JASPER_WAKE_THRESHOLD", ""),
                   os.environ.get("JASPER_WAKE_THRESHOLD", "")):
        raw = source.strip()
        if not raw:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        if 0.0 <= val <= 1.0:
            return val
    return DEFAULT_WAKE_THRESHOLD


def _is_available(entry: wake_models.WakeModelEntry) -> bool:
    """Return whether the model can be selected without crashing voice.

    Bundled openWakeWord names are install-owned package resources. We
    check their resource path via importlib metadata rather than importing
    openwakeword on every page render. External files have to exist on
    disk to be loadable; a missing file means a failed install-time
    download, flagged in the UI so the household knows what's going on.
    """
    if entry.bundled:
        asset_path = _bundled_asset_path(entry)
        if asset_path is None:
            return False
        try:
            return asset_path.is_file() and asset_path.stat().st_size > 0
        except OSError:
            return False
    return os.path.exists(entry.model)


def _bundled_asset_path(entry: wake_models.WakeModelEntry) -> Path | None:
    asset = wake_models.openwakeword_asset_by_key(entry.key)
    if asset is None:
        return None
    spec = importlib.util.find_spec("openwakeword")
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).resolve().parent / "resources" / "models" / asset.filename


# ----------------------------------------------------------------------
# HTML rendering.
# ----------------------------------------------------------------------


# Layer rows render with `disabled` initially — the /detection.json poll
# fires on page load and hydrates real state. Browsers don't fire change
# events while disabled, so the user can't toggle into a bad first-paint
# state. Each tuple is (key sent over the wire, displayed label, short
# description, cost string).
_FUSION_LAYERS = (
    (
        "raw",
        "Direct/raw wake stream",
        "Parallel raw mic wake scoring for software-AEC experiments.",
        "~5 MB · negligible",
    ),
    (
        "dtln",
        "DTLN neural AEC",
        "Neural cleanup as an optional wake stream. Recommended only on 2 GB Pi.",
        "~75 MB · ~25% core",
    ),
    (
        "chip_aec_150",
        "Extra chip beam 150°",
        "Optional wake scoring on the XVF3800 150° hardware-AEC beam.",
        "~30 MB · light",
    ),
    (
        "chip_aec_210",
        "Extra chip beam 210°",
        "Optional wake scoring on the XVF3800 210° hardware-AEC beam.",
        "~30 MB · light",
    ),
)


_ECHO_PROFILES = profile_choice_specs(section="echo")
_ADVANCED_PROFILES = profile_choice_specs(section="advanced")


def _profile_rows_html(rowspec: tuple[Any, ...]) -> str:
    rows: list[str] = []
    for spec in rowspec:
        key = spec.profile
        rows.append(f"""
    <label class="profile-row is-disabled" id="profile-row-{key}" data-profile="{key}">
      <input type="radio" name="profile-choice" id="profile-{key}"
             value="{key}" disabled>
      <span class="profile-copy">
        <span class="profile-name" id="profile-name-{key}">{html.escape(spec.label)}</span>
        <span class="profile-desc" id="profile-desc-{key}">{html.escape(spec.description)}</span>
      </span>
      <span class="badge badge--muted" id="profile-badge-{key}">{html.escape(spec.badge)}</span>
      <span class="profile-choice-status" id="profile-status-{key}">—</span>
    </label>""")
    return "".join(rows)


def _echo_card_html() -> str:
    return f"""
<section class="section echo-card">
  <div class="section__head">
    <h2 class="section__title">Echo cancellation</h2>
  </div>
  <div class="info-card">
    <div class="echo-status">
      <div class="echo-status__title" id="echo-status-title">checking…</div>
      <div class="echo-status__detail" id="echo-status-detail">—</div>
    </div>
    <div class="firmware-update" id="firmware-update-card" hidden>
      <div class="firmware-update__copy">
        <div class="firmware-update__title" id="firmware-update-title">Firmware update</div>
        <div class="firmware-update__detail" id="firmware-update-detail">—</div>
        <div class="firmware-update__meta" id="firmware-update-meta">—</div>
      </div>
      <button class="btn btn--primary" type="button"
              id="firmware-update-button" disabled>Download and update firmware</button>
    </div>
    {_profile_rows_html(_ECHO_PROFILES)}
    <div class="mic-status-warning" id="echo-status-warning" hidden></div>
  </div>
</section>"""


def _advanced_fusion_html() -> str:
    """Render collapsed expert fusion/validation controls."""
    rows: list[str] = []
    for key, name, desc, meta in _FUSION_LAYERS:
        rows.append(f"""
  <div class="layer-row" id="layer-row-{key}">
    <div class="layer-body">
      <div class="layer-name" id="layer-name-{key}">{html.escape(name)}</div>
      <div class="layer-desc" id="layer-desc-{key}">{html.escape(desc)}</div>
      <div class="layer-meta" id="layer-meta-{key}">{html.escape(meta)}</div>
      <div class="layer-status" id="layer-status-{key}">—</div>
    </div>
    {toggle_html(f"layer-{key}", disabled=True)}
  </div>""")
    return f"""
<section class="section advanced-fusion-card">
  <details class="disclosure">
    <summary>Advanced wake fusion</summary>
    <div class="disclosure-body">
  <div class="info-card">
    <p class="info-card__note">
      Expert controls for corpus tests, nonstandard hardware, and DAC validation.
      Changes switch the input profile to custom.
    </p>
    <div class="fusion-summary" id="fusion-summary">checking…</div>
    {_profile_rows_html(_ADVANCED_PROFILES)}
    {''.join(rows)}
  </div>
    </div>
  </details>
</section>"""


def _mic_status_card_html() -> str:
    """Render the compact read-only mic/topology card.

    Values are placeholders until deploy/assets/wake/js/main.js hydrates
    them from /detection.json. Keep this card non-controlling: the
    echo/fusion rows below are the action surface.
    """
    return """
<section class="section mic-status-card">
  <div class="section__head">
    <h2 class="section__title">Microphone</h2>
  </div>
  <div class="info-card">
    <div class="mic-status-grid">
      <div class="mic-status-item mic-status-item--wide">
        <div class="mic-status-label">Detected mic</div>
        <div class="mic-status-value" id="mic-status-name">checking…</div>
      </div>
      <div class="mic-status-item">
        <div class="mic-status-label">Firmware</div>
        <div class="mic-status-value" id="mic-status-firmware">checking…</div>
      </div>
      <div class="mic-status-item">
        <div class="mic-status-label">Mode</div>
        <div class="mic-status-value" id="mic-status-mode">checking…</div>
      </div>
      <div class="mic-status-item mic-status-item--wide">
        <div class="mic-status-label">Session audio</div>
        <div class="mic-status-value" id="mic-status-session-source">checking…</div>
      </div>
      <div class="mic-status-item mic-status-item--wide">
        <div class="mic-status-label">Wake legs</div>
        <div class="mic-status-value" id="mic-status-wake-legs">checking…</div>
      </div>
      <div class="mic-status-item mic-status-item--wide">
        <div class="mic-status-label">Wake phrase</div>
        <div class="mic-status-value" id="mic-status-wake-word">checking…</div>
      </div>
    </div>
    <div class="mic-status-warning" id="mic-status-warning" hidden></div>
  </div>
</section>"""


def _sensitivity_html() -> str:
    return """
<div class="sensitivity-panel">
  <div class="sensitivity-copy">
    <div class="sensitivity-title">Sensitivity</div>
    <div class="sensitivity-desc">
      Lower fires more easily; higher needs a more confident wake match.
    </div>
  </div>
  <div class="sensitivity-control">
    <input type="range" id="sensitivity-input"
           min="0.05" max="0.95" step="0.05" value="0.5" disabled>
    <span class="sensitivity-value" id="sensitivity-value">—</span>
    <button class="btn btn--ghost" id="sensitivity-save"
            type="button" disabled>Save</button>
  </div>
</div>"""


def _row_html(
    entry: wake_models.WakeModelEntry,
    *,
    is_active: bool,
    available: bool,
) -> str:
    """Render one model row. Disabled state shows a "not downloaded"
    badge instead of "recommended" / "active" so the household can
    tell at a glance why they can't pick it."""
    classes = ["wake-row"]
    if is_active:
        classes.append("is-active")
    if not available:
        classes.append("is-unavailable")

    badges = []
    if is_active:
        badges.append('<span class="badge">active</span>')
    if entry.recommended and not is_active:
        badges.append('<span class="badge">recommended</span>')
    if not available:
        badges.append('<span class="badge badge--muted">not downloaded</span>')

    radio_attrs = ['type="radio"', 'name="model"', f'value="{html.escape(entry.key)}"']
    if is_active:
        radio_attrs.append("checked")
    if not available:
        radio_attrs.append("disabled")
    radio = f'<input {" ".join(radio_attrs)}>'

    stats_bits: list[str] = []
    if entry.fa_per_hour is not None:
        stats_bits.append(
            f"~{entry.fa_per_hour:.2f} false fires/hour (author-reported)"
        )
    if entry.bundled:
        stats_bits.append("bundled with openWakeWord")
    else:
        stats_bits.append("downloaded at install time")
    stats_bits.append(
        f'<a href="{html.escape(entry.source_url)}" target="_blank" rel="noopener">source ↗</a>'
    )

    return f"""
<label class="{' '.join(classes)}">
  <div class="wake-row__head">
    {radio}
    <span class="wake-row__label">{html.escape(entry.label)}</span>
    {' '.join(badges)}
  </div>
  <div class="pronunciation">{html.escape(entry.pronunciation)}</div>
  <div class="wake-row__desc">{html.escape(entry.description)}</div>
  <div class="wake-row__stats">{' · '.join(stats_bits)}</div>
</label>"""


def _custom_row_html(model: str, *, is_active: bool) -> str:
    """Operator set JASPER_WAKE_MODEL by hand to something outside the
    curated registry — show it as a non-clickable info row so the
    wizard never silently overwrites their choice. They keep it by
    leaving the radio alone; they replace it by picking a registered
    row and hitting Save."""
    active_cls = " is-active" if is_active else ""
    active_badge = '<span class="badge">active</span>' if is_active else ""
    return f"""
<label class="wake-row{active_cls}" style="cursor:default">
  <div class="wake-row__head">
    <input type="radio" name="model" value="__custom__" checked disabled>
    <span class="wake-row__label">Custom: {html.escape(model)}</span>
    {active_badge}
  </div>
  <div class="wake-row__desc">
    Set via <code>JASPER_WAKE_MODEL</code> in
    <code>/etc/jasper/jasper.env</code>. The wizard won't touch this
    unless you pick one of the rows above and hit Save (which writes
    <code>/var/lib/jasper/wake_model.env</code>, layered on top).
  </div>
</label>"""


def _privacy_disclosure_html() -> str:
    return """
<details class="disclosure">
  <summary>Wake recordings and privacy</summary>
  <div class="disclosure-body">
    <p>
      JTS stores short wake-event WAV windows and SQLite metadata locally
      under <code>/var/lib/jasper/wake-events/</code> for reliability review.
    </p>
    <p>
      Nothing leaves the speaker automatically. Review or export with
      <code>jasper-wake-review</code> and <code>scripts/fetch-wake-events.sh</code>.
      <code>scripts/reset-wake-events.sh</code> archives before resetting;
      delete old archives manually when you want erasure.
    </p>
  </div>
</details>"""


def _index_html(state: dict[str, str], csrf_token: str = "", *, status_msg: str = "") -> bytes:
    active = _active_model(state)
    active_entry = wake_models.by_model(active)
    rows: list[str] = []
    if active_entry is None and active:
        # Custom row at the top so the household sees what's currently
        # in effect before the registered alternatives.
        rows.append(_custom_row_html(active, is_active=True))
    for entry in wake_models.REGISTRY:
        rows.append(_row_html(
            entry,
            is_active=(active_entry is entry),
            available=_is_available(entry),
        ))
    # The CSRF meta tag (read by the detection-card module for state-changing
    # fetches) is emitted by canonical_page() when csrf_token is given; the
    # model-picker form additionally carries a hidden field via
    # csrf_field_html(). The page's behaviour ships as the ES module at
    # /assets/wake/js/main.js — no inline <script>.
    body = f"""
{canonical_header("Wake word")}
{pair_banner_html()}
<main class="page">
  {_mic_status_card_html()}

  {_echo_card_html()}

  <section class="section">
    <div class="section__head">
      <h2 class="section__title">Wake word</h2>
    </div>
    <p class="wake-help">
      Choose the wake phrase and sensitivity. Saving restarts voice; it listens
      again in about 4 seconds. Re-run deploy to retry models marked
      <em>not downloaded</em>.
    </p>
    {_sensitivity_html()}

    <form method="post" action="save" id="wake-form">
      {csrf_field_html(csrf_token) if csrf_token else ''}
      {''.join(rows)}
      <div class="form-actions">
        <button type="submit" class="btn btn--primary" id="wake-save">Save and restart voice</button>
      </div>
    </form>

    {_privacy_disclosure_html()}
  </section>

  {_advanced_fusion_html()}
</main>
<script type="module" src="/assets/wake/js/main.js"></script>
"""
    return canonical_page(
        "Wake word", body,
        csrf_token=csrf_token,
        page_css_href=WAKE_PAGE_CSS_HREF,
    )


# ----------------------------------------------------------------------
# Save logic — pure where possible.
# ----------------------------------------------------------------------


def _apply_save(
    form: dict[str, str],
    current: dict[str, str],
) -> tuple[dict[str, str], str | None]:
    """Validate the form selection and produce the new wake_model.env
    state. Returns `(state, error)`; the caller writes the file iff
    error is None.

    The sensitivity slider lives in the same page but posts directly
    to jasper-control via /wake/sensitivity, which writes
    JASPER_WAKE_THRESHOLD into the same env file. Here we preserve
    whatever value is already there by starting from `dict(current)`
    (write_env_file overwrites the whole file with whatever dict we
    pass)."""
    key = (form.get("model") or "").strip()
    new = dict(current)
    if not key:
        # No `model` field submitted — happens when a Custom wake
        # model is active (the radio is rendered with `disabled`,
        # so the browser skips it). With the slider gone from this
        # form, there's nothing else to save in this case.
        return current, "No model selected."
    if key == "__custom__":
        # Defensive — the input is disabled in the rendered form,
        # but a crafted POST could submit it. Reject so we never
        # persist a nonsense token to the env file.
        return current, "The custom row is read-only — pick a registered model."
    entry = wake_models.by_key(key)
    if entry is None:
        return current, f"Unknown model: {key!r}."
    if not _is_available(entry):
        return current, (
            f"{entry.label} isn't downloaded yet on this speaker. "
            "Re-run `bash scripts/deploy-to-pi.sh` to fetch it, then "
            "try again."
        )
    new["JASPER_WAKE_MODEL"] = entry.model
    return new, None


# ----------------------------------------------------------------------
# Detection-card request handlers — proxy to jasper-control with the
# wizard's user-facing vocabulary (layer/aec, sensitivity) rewritten
# to jasper-control's internal vocabulary (aec/toggle, aec/leg,
# aec/threshold) at the proxy layer.
# ----------------------------------------------------------------------

# Maximum JSON body length accepted on /layer/* and /sensitivity. Real
# payloads are ~20 B ({"enabled": true} / {"value": 0.5}); anything
# bigger is malformed or abusive and rejected before we proxy upstream.
_LAYER_BODY_LIMIT = 4096


def _read_json_body(handler: BaseHTTPRequestHandler) -> tuple[dict | None, str | None]:
    """Read and parse a small JSON body from `handler`. Returns
    `(parsed, error)` — exactly one is non-None. Hard-caps at
    `_LAYER_BODY_LIMIT` so we never read megabytes off the wire."""
    length = int(handler.headers.get("Content-Length") or "0")
    if length < 0 or length > _LAYER_BODY_LIMIT:
        return None, "invalid body length"
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"invalid JSON body: {e}"
    if not isinstance(parsed, dict):
        return None, "body must be a JSON object"
    return parsed, None


def _apply_layer(
    layer: str, enabled: bool, *, control_base: str,
) -> tuple[int, bytes]:
    """Translate a /layer/<name> POST into jasper-control's
    /aec/toggle (software AEC3) or /aec/leg (raw/dtln/chip beam) call.

    The software-AEC3 toggle is flip-only on the control side. We read the current
    mode and only POST when it differs from the requested state, so
    a "set true while already on" returns the existing state instead
    of toggling back to off. Chip-AEC mode bypasses WebRTC AEC3 but still
    needs the bridge as its chip-beam carrier; "software AEC3 off" is
    therefore already true in that mode, and must not be translated into a
    bridge-disable POST. Returns (status, body) for proxying."""
    if layer == "aec":
        status, body = proxy_get("/aec", control_base=control_base, timeout=5.0)
        if status != 200:
            return status, body
        try:
            payload = json.loads(body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        current_mode = payload.get("mode")
        software_aec3 = payload.get("software_aec3") or {}
        software_bypassed = bool(software_aec3.get("bypassed"))
        already_in_state = (
            (
                enabled
                and current_mode == "auto"
                and not software_bypassed
            )
            or (
                not enabled
                and (current_mode == "disabled" or software_bypassed)
            )
        )
        if already_in_state:
            # No-op: return the latest state read above so the client
            # reconciles to the truth without an extra round trip.
            return 200, body
        return proxy_post(
            "/aec/toggle", control_base=control_base, timeout=5.0,
        )
    if layer in ("raw", "dtln", "chip_aec_150", "chip_aec_210"):
        return proxy_post(
            "/aec/leg",
            control_base=control_base, timeout=5.0,
            body=json.dumps({"leg": layer, "enabled": enabled}).encode(),
        )
    if layer == "chip_aec":
        # Legacy compatibility for older browser bundles/bookmarks: the
        # old single chip toggle now maps to both explicit extra-beam toggles.
        status, body = 200, b"{}"
        for beam in ("chip_aec_150", "chip_aec_210"):
            status, body = proxy_post(
                "/aec/leg",
                control_base=control_base, timeout=5.0,
                body=json.dumps({"leg": beam, "enabled": enabled}).encode(),
            )
            if status != 200:
                return status, body
        return status, body
    return 400, b'{"error":"unknown layer"}'


def _apply_sensitivity(
    value: float, *, control_base: str,
) -> tuple[int, bytes]:
    """Forward a /sensitivity POST to jasper-control's
    /aec/threshold. Wire-level vocabulary translates: wizard says
    `value`, jasper-control's API says `threshold`."""
    return proxy_post(
        "/aec/threshold",
        control_base=control_base, timeout=5.0,
        body=json.dumps({"threshold": value}).encode(),
    )


def _apply_profile(profile: str, *, control_base: str) -> tuple[int, bytes]:
    """Forward a /profile POST to jasper-control's /aec/profile."""
    return proxy_post(
        "/aec/profile",
        control_base=control_base,
        timeout=5.0,
        body=json.dumps({"profile": profile}).encode(),
    )


def _start_firmware_update(
    *,
    control_base: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Forward the mic firmware-update action to jasper-control."""
    return proxy_post(
        "/aec/firmware/update",
        control_base=control_base,
        timeout=5.0,
        body=b"{}",
        headers=headers,
    )


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


_VALID_LAYERS = ("aec", "raw", "dtln", "chip_aec_150", "chip_aec_210", "chip_aec")
_VALID_PROFILES = valid_profile_ids()


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                if not guard_read_request(self):
                    return
                state = _load_state(cfg["state_path"])
                ctx = begin_request(self)
                send_html_response(self, _index_html(
                    state, ctx["csrf_token"], status_msg=ctx["flash"],
                ))
                return
            if path == "/detection.json":
                if not guard_read_request(self):
                    return
                status, body = proxy_get(
                    "/aec",
                    control_base=cfg["control_base"], timeout=5.0,
                )
                send_proxy_json(self, body, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            # Form-bodied save uses the form-token CSRF check; JSON-
            # bodied state-set requests use the X-CSRF-Token header.
            if path == "/save":
                form = read_form(self)
                if not guard_mutating_request(self, form):
                    reject_csrf(self)
                    return
                self._handle_save(form)
                return
            if path.startswith("/layer/"):
                if not guard_mutating_request(self):
                    reject_csrf(self)
                    return
                self._handle_layer(path[len("/layer/"):])
                return
            if path == "/profile":
                if not guard_mutating_request(self):
                    reject_csrf(self)
                    return
                self._handle_profile()
                return
            if path == "/sensitivity":
                if not guard_mutating_request(self):
                    reject_csrf(self)
                    return
                self._handle_sensitivity()
                return
            if path == "/firmware/update":
                if not guard_mutating_request(self):
                    reject_csrf(self)
                    return
                status, body = _start_firmware_update(
                    control_base=cfg["control_base"],
                    headers=forward_control_token_headers(self),
                )
                send_proxy_json(self, body, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _handle_layer(self, layer: str) -> None:
            if layer not in _VALID_LAYERS:
                send_proxy_json(
                    self,
                    json.dumps({"error": f"unknown layer {layer!r}"}).encode(),
                    status=400,
                )
                return
            body, err = _read_json_body(self)
            if err is not None:
                send_proxy_json(
                    self,
                    json.dumps({"error": err}).encode(),
                    status=400,
                )
                return
            enabled = body.get("enabled") if body is not None else None
            if not isinstance(enabled, bool):
                send_proxy_json(
                    self,
                    b'{"error":"enabled must be a boolean"}',
                    status=400,
                )
                return
            log_event(
                logger,
                "wake.layer",
                layer=layer,
                enabled=enabled,
                client=self.address_string(),
            )
            status, resp = _apply_layer(
                layer, enabled, control_base=cfg["control_base"],
            )
            send_proxy_json(self, resp, status=status)

        def _handle_profile(self) -> None:
            body, err = _read_json_body(self)
            if err is not None:
                send_proxy_json(
                    self,
                    json.dumps({"error": err}).encode(),
                    status=400,
                )
                return
            profile = body.get("profile") if body is not None else None
            if not isinstance(profile, str) or profile not in _VALID_PROFILES:
                send_proxy_json(
                    self,
                    b'{"error":"profile is not supported"}',
                    status=400,
                )
                return
            log_event(
                logger,
                "wake.profile",
                profile=profile,
                client=self.address_string(),
            )
            status, resp = _apply_profile(
                profile, control_base=cfg["control_base"],
            )
            send_proxy_json(self, resp, status=status)

        def _handle_sensitivity(self) -> None:
            body, err = _read_json_body(self)
            if err is not None:
                send_proxy_json(
                    self,
                    json.dumps({"error": err}).encode(),
                    status=400,
                )
                return
            value = body.get("value") if body is not None else None
            try:
                value = float(value)
            except (TypeError, ValueError):
                send_proxy_json(
                    self,
                    b'{"error":"value must be a number"}',
                    status=400,
                )
                return
            if not 0.0 <= value <= 1.0:
                send_proxy_json(
                    self,
                    b'{"error":"value must be between 0 and 1"}',
                    status=400,
                )
                return
            log_event(
                logger,
                "wake.sensitivity",
                value=f"{value:.2f}",
                client=self.address_string(),
            )
            status, resp = _apply_sensitivity(
                value, control_base=cfg["control_base"],
            )
            send_proxy_json(self, resp, status=status)

        def _handle_save(self, form: dict[str, str]) -> None:
            current = _load_state(cfg["state_path"])
            new, err = _apply_save(form, current)
            if err is not None:
                send_see_other(self, "./", flash=err)
                return
            try:
                # _apply_save always stamps JASPER_WAKE_MODEL on the success
                # path (errors are guarded above via `err`), so `new` is never
                # empty. Only update that key under the shared lock: the
                # sensitivity slider writes JASPER_WAKE_THRESHOLD to this same
                # file from jasper-control, and stale form state must not erase
                # a concurrent threshold save.
                new = locked_update_env_file(
                    cfg["state_path"],
                    {"JASPER_WAKE_MODEL": new["JASPER_WAKE_MODEL"]},
                    mode=0o644,
                )
            except OSError as e:
                logger.exception("could not write wake-model env file")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            restart_voice_daemon()
            picked = new.get("JASPER_WAKE_MODEL", "")
            # Parity with the wake.layer/profile/sensitivity sub-actions above —
            # the primary model change was the one mutation this page didn't log.
            # The model name is not a secret.
            log_event(
                logger,
                "wake.model",
                model=picked,
                client=self.address_string(),
            )
            entry = wake_models.by_model(picked)
            label = entry.label if entry else picked
            threshold_str = new.get("JASPER_WAKE_THRESHOLD", "")
            extra = (
                f" (sensitivity {threshold_str})"
                if threshold_str else ""
            )
            send_see_other(
                self, "./",
                flash=f"Saved. Voice daemon restarting on {label}{extra}.",
            )

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(
    target,
    *,
    state_path: str = WAKE_MODEL_FILE,
    control_base: str = DEFAULT_CONTROL_BASE,
) -> ThreadingHTTPServer:
    from . import _systemd
    cfg = {"state_path": state_path, "control_base": control_base}
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-wake-web",
        description="Wake-word picker UI for the Jasper smart speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_WAKE_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_WAKE_WEB_PORT", "8774")),
    )
    parser.add_argument(
        "--state", default=os.environ.get("JASPER_WAKE_MODEL_FILE", WAKE_MODEL_FILE),
    )
    parser.add_argument(
        "--control-base",
        default=os.environ.get("JASPER_CONTROL_BASE", DEFAULT_CONTROL_BASE),
        help="jasper-control HTTP base URL (default 127.0.0.1:8780)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(
        (args.host, args.port),
        state_path=args.state,
        control_base=args.control_base,
    )
    logger.info(
        "jasper-wake-web listening on http://%s:%d (state=%s)",
        args.host, args.port, args.state,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
