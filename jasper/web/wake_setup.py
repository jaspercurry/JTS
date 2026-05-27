"""Wake-word page at /wake/.

Two stacked sections, one page:

  1. **Detection layers + sensitivity** — three iOS-style toggles
     (AEC3 echo cancellation, chip-direct mic, DTLN neural AEC) and a
     sensitivity slider. Polls jasper-control for live state, posts
     state-set requests back. The AEC master gates the bridge entirely;
     the raw + DTLN legs are sub-features layered on top, and are
     disabled (visually + interactively) when AEC is off because they
     consume the bridge's UDP stream.

  2. **Wake-word model picker** — radio over the curated registry in
     jasper/wake_models.py. Bundled openWakeWord names always show as
     available; non-bundled models surface a "not downloaded" hint when
     their `.onnx` file is missing on disk (install.sh fetches them on
     every deploy, so this state is usually transient).

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

URL surface (after nginx strips the /wake/ prefix):
  GET  /                page render
  GET  /detection.json  proxy jasper-control /aec — mode + bridge +
                        leg config + threshold
  POST /layer/aec       body {enabled: bool} — set AEC master
  POST /layer/raw       body {enabled: bool} — set chip-direct leg
  POST /layer/dtln      body {enabled: bool} — set DTLN leg
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
import json
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import wake_models
from ._common import (
    DEFAULT_CONTROL_BASE,
    PAGE_STYLE,
    TOGGLE_CSS,
    begin_request,
    csrf_fetch_helpers_js,
    csrf_field_html,
    csrf_meta_html,
    delete_env_file,
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
    verify_csrf,
    wrap_page,
    write_env_file,
)

logger = logging.getLogger(__name__)


WAKE_MODEL_FILE = wake_models.WAKE_MODEL_FILE

# Compiled-in default mirrored from jasper/config.py:_validate.
# Tests + `_active_threshold` reference it; the slider's min/max/step
# constants live inline in the rendered HTML (no Python tests exercise
# them so a duplicate Python constant would just rot).
DEFAULT_WAKE_THRESHOLD = 0.5


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
    """Bundled openWakeWord names are install-owned package resources.

    install.sh stages and hash-checks those ONNX files up front; checking
    the package path here would require importing openwakeword on every
    page render. External files have to exist on disk to be loadable; a
    missing file means a failed install-time download (rare, but flagged
    in the UI so the household knows what's going on).
    """
    if entry.bundled:
        return True
    return os.path.exists(entry.model)


# ----------------------------------------------------------------------
# HTML rendering.
# ----------------------------------------------------------------------


_WAKE_PAGE_STYLE = PAGE_STYLE + TOGGLE_CSS + """
  .wake-help { color: #555; font-size: 0.93em; margin: 0.4em 0 1.4em;
               line-height: 1.5; }

  /* ----- Detection layers + sensitivity card ------------------- */
  .layers-card {
    background: #fafafa; border: 1px solid #e6e6e6;
    border-radius: 8px; padding: 0.9em 1em 1em;
    margin: 0.6em 0 1.6em;
  }
  .layers-card h2 {
    font-size: 0.86em; margin: 0 0 0.5em;
    text-transform: uppercase; letter-spacing: 0.04em; color: #666;
  }
  .layers-card .intro {
    color: #666; font-size: 0.88em; margin: 0 0 0.8em;
    line-height: 1.5;
  }
  .layer-row {
    display: flex; align-items: flex-start; gap: 0.9em;
    padding: 0.7em 0; border-bottom: 1px solid #eee;
  }
  .layer-row:last-of-type { border-bottom: none; }
  .layer-row.disabled { opacity: 0.55; }
  .layer-row .body { flex: 1; }
  .layer-row .name {
    font-weight: 600; font-size: 1.0em; color: #222;
  }
  .layer-row .desc {
    color: #555; font-size: 0.86em; margin-top: 0.15em;
    line-height: 1.4;
  }
  .layer-row .meta {
    color: #888; font-size: 0.82em; margin-top: 0.25em;
    font-variant-numeric: tabular-nums;
  }
  .layer-row .status {
    color: #666; font-size: 0.82em; margin-top: 0.1em;
    font-variant-numeric: tabular-nums;
  }
  .sensitivity-row {
    padding-top: 0.9em; margin-top: 0.4em;
    border-top: 1px solid #eee;
  }
  .sensitivity-row .name { font-weight: 600; color: #222; }
  .sensitivity-row .desc {
    color: #666; font-size: 0.85em; margin: 0.15em 0 0.6em;
    line-height: 1.4;
  }
  .sensitivity-row .control {
    display: flex; gap: 0.6em; align-items: center;
  }
  .sensitivity-row input[type=range] {
    flex: 1; accent-color: #1db954;
  }
  .sensitivity-row input[type=range]:disabled { opacity: 0.4; }
  .sensitivity-row .value {
    min-width: 3em; text-align: right;
    font-variant-numeric: tabular-nums; color: #444;
  }

  /* ----- Model picker rows ------------------------------------- */
  .wake-row {
    display: block; padding: 0.9em 1em;
    background: #f4f4f4; border: 1px solid #e6e6e6; border-radius: 8px;
    margin-bottom: 0.6em; cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  .wake-row:hover { background: #f0fff4; border-color: #1db954; }
  .wake-row.active { background: #f0fff4; border-color: #1db954; }
  .wake-row.unavailable { opacity: 0.55; cursor: not-allowed; }
  .wake-row.unavailable:hover { background: #f4f4f4; border-color: #e6e6e6; }
  .wake-row .header {
    display: flex; align-items: center; gap: 0.6em;
    margin-bottom: 0.25em;
  }
  .wake-row input[type=radio] {
    width: auto; flex: none; margin: 0;
  }
  .wake-row .label {
    font-weight: 600; font-size: 1.02em; color: #222; flex: 1;
  }
  .wake-row .badge {
    background: #4a8; color: white; padding: 0.1em 0.55em;
    border-radius: 4px; font-size: 0.78em;
  }
  .wake-row .badge.recommended { background: #1db954; }
  .wake-row .badge.muted { background: #aaa; }
  .wake-row .pronunciation {
    color: #444; font-size: 0.93em; margin: 0.15em 0 0.35em 1.6em;
    font-style: italic;
  }
  .wake-row .description {
    color: #555; font-size: 0.9em; line-height: 1.5;
    margin: 0.2em 0 0 1.6em;
  }
  .wake-row .stats {
    color: #888; font-size: 0.83em; margin: 0.4em 0 0 1.6em;
    font-variant-numeric: tabular-nums;
  }
  .wake-row .stats a {
    color: #888; text-decoration: underline;
  }
  .wake-row .stats a:hover { color: #1db954; }

  h2.section { font-size: 0.86em; margin: 1.6em 0 0.5em;
                text-transform: uppercase; letter-spacing: 0.04em;
                color: #666; }
"""


def _wrap_wake_page(title: str, body: str, *, status_msg: str = "") -> bytes:
    page = wrap_page(title, body, status_msg=status_msg).decode()
    return page.replace(
        f"<style>{PAGE_STYLE}</style>",
        f"<style>{_WAKE_PAGE_STYLE}</style>",
    ).encode()


# Layer rows render with `disabled` initially — the /detection.json poll
# fires on page load and hydrates real state. Browsers don't fire
# change events while disabled, so the user can't toggle into a bad
# state during the ~50 ms first-paint window. Each tuple is
# (key sent over the wire, displayed label, short description, cost
# string, "requires AEC" gating). AEC itself ungated.
_LAYERS = (
    (
        "aec",
        "AEC3 echo cancellation",
        "Cancels the speaker's own music + TTS from the mic. "
        "Required for waking while music plays.",
        "~85 MB · ~22% core",
        False,
    ),
    (
        "raw",
        "Chip-direct mic (raw)",
        "Pre-AEC chip mic as a parallel wake layer. Catches wakes "
        "when AEC over-suppresses. Default on.",
        "~5 MB · negligible",
        True,
    ),
    (
        "dtln",
        "DTLN neural AEC",
        "Neural echo cancellation as a third wake layer. Best "
        "wake-rate boost; heaviest cost. Recommended only on 2 GB Pi.",
        "~75 MB · ~25% core",
        True,
    ),
)


def _layers_card_html() -> str:
    """Render the detection-layers + sensitivity card. State is
    hydrated by the /detection.json poll; first paint shows disabled
    toggles with em-dash status so a slow upstream doesn't cause UI
    flicker."""
    rows: list[str] = []
    for key, name, desc, meta, _gated in _LAYERS:
        rows.append(f"""
  <div class="layer-row" id="layer-row-{key}">
    <div class="body">
      <div class="name">{html.escape(name)}</div>
      <div class="desc">{html.escape(desc)}</div>
      <div class="meta">{html.escape(meta)}</div>
      <div class="status" id="layer-status-{key}">—</div>
    </div>
    {toggle_html(f"layer-{key}", disabled=True)}
  </div>""")
    return f"""
<div class="layers-card">
  <h2>Wake detection</h2>
  <p class="intro">
    Each layer scores the same wake word independently and OR-gates
    its fires with the others. Add layers to catch wakes the AEC
    sometimes misses; remove them to save RAM on 1 GB Pis. The
    sensitivity slider applies to every active layer. Toggling
    anything restarts jasper-voice (~15 s of dead wake).
  </p>
  {''.join(rows)}
  <div class="layer-row sensitivity-row">
    <div class="body" style="width:100%">
      <div class="name">Sensitivity</div>
      <div class="desc">
        Lower = wake fires more easily (more false positives);
        higher = needs a more confident match (more missed wakes).
      </div>
      <div class="control">
        <input type="range" id="sensitivity-input"
               min="0.05" max="0.95" step="0.05" value="0.5" disabled>
        <span class="value" id="sensitivity-value">—</span>
        <button class="secondary" id="sensitivity-save"
                type="button" disabled>Save</button>
      </div>
    </div>
  </div>
</div>"""


# JS that drives the detection card. Polls /detection.json every 3 s,
# reconciles state into the toggles + slider, posts /layer/<name> or
# /sensitivity on user interaction. Mirrors /sources/'s optimistic-
# flip-with-reconcile pattern — same dirty-flag plumbing keeps a
# poll from clobbering a click mid-flight. Slider uses an explicit
# Save button instead of apply-on-change so a drag doesn't restart
# the voice daemon on every pixel.
_LAYERS_SCRIPT = r"""
(() => {
  __CSRF_FETCH_HELPERS__
  const LAYERS = ['aec', 'raw', 'dtln'];
  const POLL_MS = 3000;
  const dirty = {};
  let ignorePollUntil = 0;
  let lastServerThreshold = null;

  function el(id) { return document.getElementById(id); }

  function statusLine(active, layerOn, gated, mode) {
    if (gated && mode !== 'auto') return '— requires AEC on';
    if (!layerOn) return '— off';
    if (active) return '✓ active';
    return '⏳ starting…';
  }

  function applyState(s) {
    const mode = s.mode;
    const bridgeOn = !!s.bridge_active;
    const legs = s.legs || {};
    const aecOn = (mode === 'auto');
    const rawOn = !!(legs.raw && legs.raw.configured);
    const dtlnOn = !!(legs.dtln && legs.dtln.configured);

    // AEC master row.
    if (!dirty.aec) {
      el('layer-aec').checked = aecOn;
      el('layer-aec').disabled = false;
    }
    el('layer-status-aec').textContent = aecOn
      ? (bridgeOn ? '✓ active'
                  : '⏳ starting (or chip not on 6-ch firmware)')
      : '— disabled';
    el('layer-row-aec').classList.toggle('disabled', !aecOn);

    // Legs require AEC; reflect that in disabled state + status copy.
    [['raw', rawOn], ['dtln', dtlnOn]].forEach(([name, on]) => {
      if (!dirty[name]) {
        el('layer-' + name).checked = on;
        el('layer-' + name).disabled = !aecOn;
      }
      el('layer-status-' + name).textContent =
        statusLine(bridgeOn, on, true, mode);
      el('layer-row-' + name).classList.toggle('disabled', !aecOn);
    });

    // Sensitivity — only overwrite from server when the user isn't
    // mid-drag and hasn't queued an unsaved change.
    const slider = el('sensitivity-input');
    const valueLabel = el('sensitivity-value');
    const saveBtn = el('sensitivity-save');
    const serverThr = (typeof s.threshold === 'number') ? s.threshold : 0.5;
    slider.disabled = false;
    if (lastServerThreshold === null ||
        (Math.abs(parseFloat(slider.value) - lastServerThreshold) < 0.001
         && !saveBtn.classList.contains('dirty'))) {
      slider.value = serverThr.toFixed(2);
      valueLabel.textContent = serverThr.toFixed(2);
      saveBtn.disabled = true;
      saveBtn.classList.remove('dirty');
    }
    lastServerThreshold = serverThr;
  }

  async function pollDetection() {
    if (document.visibilityState === 'hidden') return;
    if (Date.now() < ignorePollUntil) return;
    try {
      const r = await fetch('detection.json', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      applyState(await r.json());
    } catch (e) {
      LAYERS.forEach(name => {
        el('layer-status-' + name).textContent = 'Disconnected';
      });
    }
  }

  async function postLayer(name, wanted) {
    dirty[name] = true;
    ignorePollUntil = Date.now() + 1500;
    try {
      const r = await fetch('layer/' + name, {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({ enabled: wanted }),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
      // Server returns the full state after applying — reconcile right
      // away so the AEC-off → legs-disabled transition is instant.
      dirty[name] = false;
      applyState(body);
    } catch (err) {
      alert('Toggle failed: ' + err.message);
      dirty[name] = false;
      el('layer-' + name).checked = !wanted;  // roll back
    }
  }

  LAYERS.forEach(name => {
    el('layer-' + name).addEventListener('change', () => {
      const cb = el('layer-' + name);
      if (name === 'aec' && !cb.checked && !confirm(
          'Disable AEC echo cancellation?\n\n' +
          'jasper-voice will restart — wake unavailable ~15 s. ' +
          'Turning AEC off also pauses the raw + DTLN layers ' +
          '(they need the bridge running).')) {
        cb.checked = true;
        return;
      }
      if (name === 'dtln' && cb.checked && !confirm(
          'Enable DTLN neural AEC?\n\n' +
          '+~75 MB RAM, +~25% one core. Recommended for 2 GB Pis.\n' +
          'jasper-voice + bridge will restart (~15 s).')) {
        cb.checked = false;
        return;
      }
      postLayer(name, cb.checked);
    });
  });

  const slider = el('sensitivity-input');
  const valueLabel = el('sensitivity-value');
  const saveBtn = el('sensitivity-save');
  slider.addEventListener('input', () => {
    const v = parseFloat(slider.value);
    valueLabel.textContent = v.toFixed(2);
    const changed = lastServerThreshold === null ||
                    Math.abs(v - lastServerThreshold) > 0.001;
    saveBtn.disabled = !changed;
    saveBtn.classList.toggle('dirty', changed);
  });
  saveBtn.addEventListener('click', async () => {
    const v = parseFloat(slider.value);
    saveBtn.disabled = true;
    saveBtn.textContent = '…';
    try {
      const r = await fetch('sensitivity', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({ value: v }),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
      saveBtn.classList.remove('dirty');
    } catch (err) {
      alert('Save failed: ' + err.message);
    }
    saveBtn.textContent = 'Save';
    setTimeout(pollDetection, 500);
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') pollDetection();
  });
  pollDetection();
  setInterval(pollDetection, POLL_MS);
})();
"""


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
        classes.append("active")
    if not available:
        classes.append("unavailable")

    badges = []
    if is_active:
        badges.append('<span class="badge">active</span>')
    if entry.recommended and not is_active:
        badges.append('<span class="badge recommended">recommended</span>')
    if not available:
        badges.append('<span class="badge muted">not downloaded</span>')

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
  <div class="header">
    {radio}
    <span class="label">{html.escape(entry.label)}</span>
    {' '.join(badges)}
  </div>
  <div class="pronunciation">{html.escape(entry.pronunciation)}</div>
  <div class="description">{html.escape(entry.description)}</div>
  <div class="stats">{' · '.join(stats_bits)}</div>
</label>"""


def _custom_row_html(model: str, *, is_active: bool) -> str:
    """Operator set JASPER_WAKE_MODEL by hand to something outside the
    curated registry — show it as a non-clickable info row so the
    wizard never silently overwrites their choice. They keep it by
    leaving the radio alone; they replace it by picking a registered
    row and hitting Save."""
    return f"""
<label class="wake-row {'active' if is_active else ''}" style="cursor:default">
  <div class="header">
    <input type="radio" name="model" value="__custom__" checked disabled>
    <span class="label">Custom: {html.escape(model)}</span>
    {'<span class="badge">active</span>' if is_active else ''}
  </div>
  <div class="description">
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
      JTS keeps a local wake-event corpus on this speaker: short WAV
      windows around wake fires and near-misses plus SQLite metadata
      under <code>/var/lib/jasper/wake-events/</code>. The WAV audio
      is size-capped; metadata rows are kept for long-baseline wake
      reliability stats.
    </p>
    <p>
      The corpus does not leave the speaker automatically. Operators
      can inspect or export it with <code>jasper-wake-review</code>,
      <code>scripts/fetch-wake-events.sh</code>, and
      <code>scripts/reset-wake-events.sh</code>. Reset archives the
      corpus before starting fresh; delete old archives manually when
      you want erasure.
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
    # CSRF meta tag rides at the top of the body — the detection-card
    # JS reads it via querySelector for state-changing fetches; the
    # model picker form uses a hidden field via csrf_field_html().
    body = f"""
{csrf_meta_html(csrf_token) if csrf_token else ''}

{_layers_card_html()}

<h2 class="section">Wake word</h2>
<p class="wake-help">
  Pick which wake phrase the speaker listens for. Models marked
  <em>not downloaded</em> failed their install-time fetch and can be
  retried by re-running <code>bash scripts/deploy-to-pi.sh</code>.
  Saving restarts the voice daemon; it's listening again in about
  4 seconds.
</p>

<form method="post" action="save" id="wake-form">
  {csrf_field_html(csrf_token) if csrf_token else ''}
  {''.join(rows)}
  <p style="margin-top:1.4em">
    <button type="submit" id="wake-save">Save and restart voice</button>
  </p>
</form>

{_privacy_disclosure_html()}

<script>
  // Disable the Save button + change its label the instant the form
  // submits so the household sees something happen before the page
  // reloads. Without this, the redirect (which fires before the
  // daemon is fully back up) feels like a no-op — observed on PR #117.
  document.getElementById('wake-form').addEventListener('submit', function() {{
    var btn = document.getElementById('wake-save');
    btn.disabled = true;
    btn.textContent = 'Saving…';
  }});
</script>
<script>{_LAYERS_SCRIPT.replace("__CSRF_FETCH_HELPERS__", csrf_fetch_helpers_js())}</script>
"""
    return _wrap_wake_page(
        "Wake word", body, status_msg=status_msg,
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
    /aec/toggle (master) or /aec/leg (raw/dtln) call.

    AEC master is flip-only on the control side. We read the current
    mode and only POST when it differs from the requested state, so
    a "set true while already on" returns the existing state instead
    of toggling back to off. Returns (status, body) for proxying."""
    if layer == "aec":
        status, body = proxy_get("/aec", control_base=control_base, timeout=5.0)
        if status != 200:
            return status, body
        try:
            current_mode = json.loads(body.decode()).get("mode")
        except (UnicodeDecodeError, json.JSONDecodeError):
            current_mode = None
        already_in_state = (
            (enabled and current_mode == "auto")
            or (not enabled and current_mode == "disabled")
        )
        if already_in_state:
            # No-op: return the latest state read above so the client
            # reconciles to the truth without an extra round trip.
            return 200, body
        return proxy_post(
            "/aec/toggle", control_base=control_base, timeout=5.0,
        )
    if layer in ("raw", "dtln"):
        return proxy_post(
            "/aec/leg",
            control_base=control_base, timeout=5.0,
            body=json.dumps({"leg": layer, "enabled": enabled}).encode(),
        )
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


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


_VALID_LAYERS = ("aec", "raw", "dtln")


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                state = _load_state(cfg["state_path"])
                ctx = begin_request(self)
                send_html_response(self, _index_html(
                    state, ctx["csrf_token"], status_msg=ctx["flash"],
                ))
                return
            if path == "/detection.json":
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
                if not verify_csrf(self, form):
                    reject_csrf(self)
                    return
                self._handle_save(form)
                return
            if path.startswith("/layer/"):
                if not verify_csrf(self):
                    reject_csrf(self)
                    return
                self._handle_layer(path[len("/layer/"):])
                return
            if path == "/sensitivity":
                if not verify_csrf(self):
                    reject_csrf(self)
                    return
                self._handle_sensitivity()
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
            logger.info(
                "event=wake.layer layer=%s enabled=%s client=%s",
                layer, enabled, self.address_string(),
            )
            status, resp = _apply_layer(
                layer, enabled, control_base=cfg["control_base"],
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
            logger.info(
                "event=wake.sensitivity value=%.2f client=%s",
                value, self.address_string(),
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
                if new:
                    write_env_file(cfg["state_path"], new, mode=0o644)
                else:
                    delete_env_file(cfg["state_path"])
            except OSError as e:
                logger.exception("could not write wake-model env file")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return
            restart_voice_daemon()
            picked = new.get("JASPER_WAKE_MODEL", "")
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
