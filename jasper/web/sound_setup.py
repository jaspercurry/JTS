"""Sound curve and preference-EQ page at /sound/.

URL surface (after nginx strips /sound/):
  GET  /         page render
  GET  /state    persisted profile + preview + stock curve metadata
  POST /preview  preview a draft profile without touching live audio
  POST /audition validate and load a draft/bypass config without persisting
  POST /profiles/save save or update a named custom profile
  POST /profiles/rename rename a named custom profile
  POST /profiles/delete delete a named custom profile
  POST /apply    validate, persist, emit CamillaDSP config, load it
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from jasper.sound.profile import (
    ADVANCED_GAIN_LIMIT_DB,
    MAX_FREQ_HZ,
    MAX_PARAMETRIC_BANDS,
    MAX_Q,
    MIN_FREQ_HZ,
    MIN_Q,
    PROFILE_LIBRARY_PATH,
    PROFILE_PATH,
    SIMPLE_EQ_LIMIT_DB,
    SoundProfile,
    build_sound_filters,
    curve_payload,
    delete_named_profile,
    estimate_compare_headroom_db,
    estimate_headroom_db,
    load_profile_library,
    load_profile,
    profile_library_payload,
    rename_named_profile,
    response_component_payload,
    response_preview,
    save_named_profile,
    save_profile,
)

from ._common import (
    TOGGLE_CSS,
    begin_request,
    csrf_fetch_helpers_js,
    csrf_meta_html,
    reject_csrf,
    send_html_response,
    verify_csrf,
    wrap_page,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = "/var/lib/camilladsp/configs"
MAX_JSON_BYTES = 64 * 1024


def _camilla():
    from jasper.camilla import CamillaController

    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


def _state_payload(
    profile: SoundProfile,
    *,
    library_path: str | Path | None = None,
    include_library: bool = False,
) -> dict[str, Any]:
    from jasper.dsp_apply import last_dsp_apply_state

    payload = {
        "profile": profile.to_dict(),
        "curves": curve_payload(),
        "preview": response_preview(profile),
        "components": response_component_payload(profile),
        "headroom_db": estimate_headroom_db(profile),
        "limits": {
            "simple_gain_db": SIMPLE_EQ_LIMIT_DB,
            "advanced_gain_db": ADVANCED_GAIN_LIMIT_DB,
            "max_parametric_bands": MAX_PARAMETRIC_BANDS,
            "min_freq_hz": MIN_FREQ_HZ,
            "max_freq_hz": MAX_FREQ_HZ,
            "min_q": MIN_Q,
            "max_q": MAX_Q,
        },
        "last_dsp_apply": last_dsp_apply_state(),
    }
    if include_library:
        payload["profile_library"] = profile_library_payload(
            load_profile_library(library_path)
        )
    return payload


async def _apply_profile(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    apply_state, out_path, stamped = await _load_profile_config(
        profile.with_timestamp(),
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
        source="sound",
        persist_profile=True,
    )
    logger.info(
        "event=sound.apply enabled=%s curve=%s bass=%.1f mid=%.1f treble=%.1f "
        "room_peqs=%d config=%s op_id=%s",
        stamped.enabled,
        stamped.curve_id,
        stamped.simple_eq.bass_db,
        stamped.simple_eq.mid_db,
        stamped.simple_eq.treble_db,
        apply_state.room_peq_count or 0,
        out_path,
        apply_state.op_id,
    )
    payload = _state_payload(
        stamped,
        library_path=library_path,
        include_library=library_path is not None,
    )
    payload["active_config_path"] = str(out_path)
    payload["preserved_room_peqs"] = apply_state.room_peq_count or 0
    payload["last_dsp_apply"] = apply_state.to_dict()
    return payload


async def _audition_profile(
    profile: SoundProfile,
    *,
    compare_profiles: list[SoundProfile],
    audition_mode: str = "draft",
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    compare_headroom_db = estimate_compare_headroom_db(compare_profiles or [profile])
    apply_state, out_path, loaded = await _load_profile_config(
        profile,
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
        source="sound_audition",
        persist_profile=False,
        audition=True,
        compare_headroom_db=compare_headroom_db,
    )
    logger.info(
        "event=sound.audition mode=%s enabled=%s curve=%s bands=%d "
        "compare_headroom=%.1f room_peqs=%d config=%s op_id=%s",
        audition_mode,
        loaded.enabled,
        loaded.curve_id,
        len(loaded.parametric_bands),
        compare_headroom_db,
        apply_state.room_peq_count or 0,
        out_path,
        apply_state.op_id,
    )
    saved = load_profile(profile_path)
    payload = _state_payload(
        saved,
        library_path=library_path,
        include_library=library_path is not None,
    )
    payload.update(
        {
            "audition_profile": loaded.to_dict(),
            "audition_mode": audition_mode,
            "audition_headroom_db": compare_headroom_db,
            "active_config_path": str(out_path),
            "preserved_room_peqs": apply_state.room_peq_count or 0,
            "last_dsp_apply": apply_state.to_dict(),
        }
    )
    return payload


async def _load_profile_config(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any],
    source: str,
    persist_profile: bool,
    audition: bool = False,
    compare_headroom_db: float | None = None,
) -> tuple[Any, Path, SoundProfile]:
    from jasper.sound.camilla_yaml import (
        BASE_CONFIG_PATH,
        emit_sound_config,
        extract_room_peqs_from_config,
        is_base_config,
        is_jts_generated_config,
        sound_audition_config_path,
        sound_config_path,
    )
    from jasper.dsp_apply import apply_dsp_config

    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)
    profile_id = str(time.time_ns())
    out_path = (
        sound_audition_config_path(config_path)
        if audition
        else sound_config_path(config_path)
    )
    cam = camilla_factory()

    async def _prepare_config() -> dict[str, Any]:
        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            raise RuntimeError("CamillaDSP did not report a loaded config path")

        if is_base_config(current_path):
            room_peqs = []
        elif is_jts_generated_config(current_path, config_dir=config_path):
            room_peqs = extract_room_peqs_from_config(current_path)
        else:
            raise RuntimeError(
                "CamillaDSP is running a custom config that JTS cannot safely "
                f"preserve ({current_path}). Reset to {BASE_CONFIG_PATH} or apply "
                "room correction before changing sound EQ."
            )

        emit_sound_config(
            profile,
            room_peqs=room_peqs,
            out_path=out_path,
            profile_id=profile_id,
            headroom_override_db=compare_headroom_db,
            emit_preamp_without_sound=compare_headroom_db is not None,
        )
        return {
            "prior_config_path": current_path,
            "room_peq_count": len(room_peqs),
            "sound_filter_count": len(build_sound_filters(profile)),
        }

    apply_state = await apply_dsp_config(
        source=source,
        candidate_path=out_path,
        prepare=_prepare_config,
        load_config=lambda path: cam.set_config_file_path(
            path,
            best_effort=False,
        ),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
        persist=(lambda: save_profile(profile, profile_path))
        if persist_profile
        else None,
        sound_filter_count=len(build_sound_filters(profile)),
    )
    return apply_state, out_path, profile


_PAGE_CSS = f"""
<style>
{TOGGLE_CSS}
  .toolbar {{
    display: flex; align-items: center; justify-content: space-between;
    gap: 1em; padding: 0.8em 0; border-bottom: 1px solid #eee;
  }}
  .toolbar-title {{ font-weight: 700; color: #222; }}
  .eq-grid {{ display: grid; grid-template-columns: 1fr; gap: 1em; }}
  .eq-grid > * {{ min-width: 0; }}
  .field {{ margin: 1.1em 0; }}
  .profile-panel {{
    display: grid; gap: 0.75em; padding: 1em 0; border-bottom: 1px solid #eee;
  }}
  .profile-panel label {{ margin-top: 0; }}
  .profile-row {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 0.45em; }}
  .profile-actions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.55em; }}
  .profile-actions.stock {{ grid-template-columns: minmax(0, 1fr); }}
  .profile-actions button {{ min-height: 44px; padding-left: 0.7em; padding-right: 0.7em; }}
  .profile-actions button[hidden] {{ display: none; }}
  .profile-note {{ color: #666; font-size: 0.92em; }}
  .profile-state {{
    display: grid; gap: 0.25em; padding: 0.75em; border: 1px solid #e6e6e6;
    border-radius: 6px; background: #fafafa; color: #444;
  }}
  .profile-state strong {{ color: #222; }}
  select, input[type=number], input[type=text] {{ max-width: 100%; }}
  input[type=number], input[type=text] {{
    width: 100%; padding: 0.45em; border: 1px solid #bbb;
    border-radius: 4px; font-size: 1em; box-sizing: border-box;
  }}
  .compare-row {{
    display: grid; grid-template-columns: minmax(0, 1fr); gap: 0.5em;
    padding: 1em 0; border-bottom: 1px solid #eee;
  }}
  .compare-row label {{ margin-top: 0; }}
  .segmented {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.35em;
    margin-top: 0.4em;
  }}
  .segmented button {{
    background: #ededed; color: #222; min-height: 44px; padding: 0.65em 0.4em;
  }}
  .segmented button.active {{
    background: #1db954; color: white; font-weight: 700;
  }}
  .compare-note {{ color: #666; font-size: 0.92em; }}
  .mode-row {{
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.35em;
    margin: 1em 0 0.25em;
  }}
  .mode-row button {{
    background: #ededed; color: #222; min-height: 44px; padding: 0.65em 0.4em;
  }}
  .mode-row button.active {{
    background: #0b7285; color: white; font-weight: 700;
  }}
  .slider-row {{
    display: grid;
    grid-template-columns: minmax(3.7em, 4.5em) minmax(0, 1fr) minmax(3.8em, 4.2em);
    align-items: center; gap: 0.8em; margin: 1.2em 0;
  }}
  .slider-row label {{ margin: 0; }}
  input[type=range] {{ width: 100%; accent-color: #1db954; }}
  .value {{ font-variant-numeric: tabular-nums; text-align: right; color: #444; }}
  .button-row {{ display: flex; flex-wrap: wrap; gap: 0.7em; margin-top: 1.2em; }}
  .status-line {{ min-height: 1.4em; color: #555; margin-top: 0.8em; }}
  .status-line.err {{ color: #b42318; }}
  .plot {{
    display: block; box-sizing: border-box;
    width: 100%; max-width: 100%; height: 190px; margin: 1em 0 0;
    border: 1px solid #ddd; border-radius: 6px; background: #fbfbfb;
  }}
  .plot text {{ fill: #777; font-size: 11px; }}
  .plot .grid {{ stroke: #e3e3e3; stroke-width: 1; }}
  .plot .zero {{ stroke: #b8b8b8; stroke-width: 1.2; }}
  .plot .component {{ fill: none; stroke: #9aa1a8; stroke-width: 1.5; stroke-dasharray: 4 4; opacity: 0.7; }}
  .plot .component.selected {{ stroke: #0b7285; stroke-width: 2; stroke-dasharray: none; opacity: 0.95; }}
  .plot .curve {{ fill: none; stroke: #1db954; stroke-width: 2.5; }}
  .plot .band-width {{ fill: #0b7285; opacity: 0.08; }}
  .plot .band-width.selected {{ opacity: 0.13; }}
  .plot .band-marker {{ stroke: #0b7285; stroke-width: 1.2; stroke-dasharray: 3 4; opacity: 0.85; }}
  .plot .band-dot {{ fill: #fbfbfb; stroke: #0b7285; stroke-width: 2; }}
  .plot .band-dot.selected {{ fill: #0b7285; }}
  .plot.off .curve {{ stroke: #888; stroke-dasharray: 5 4; }}
  .meta-row {{
    display: flex; flex-wrap: wrap; gap: 0.8em; color: #666;
    font-size: 0.92em; margin-top: 0.5em;
  }}
  .curve-description {{ color: #666; margin-top: 0.35em; }}
  .advanced-eq {{
    border: 1px solid #e2e2e2; border-radius: 6px; overflow: hidden;
  }}
  .advanced-body {{ padding: 0.9em; }}
  .advanced-top {{
    display: flex; align-items: center; justify-content: space-between;
    gap: 0.8em; margin-bottom: 0.8em; color: #666; font-size: 0.92em;
  }}
  .band-list {{ display: grid; gap: 0.75em; }}
  .band-empty {{
    color: #666; background: #fafafa; border: 1px dashed #ccc;
    border-radius: 6px; padding: 0.9em;
  }}
  .band-item {{
    border: 1px solid #ddd; border-radius: 6px; padding: 0.85em;
    background: #fff;
  }}
  .band-item.selected {{ border-color: #0b7285; box-shadow: inset 0 0 0 1px #0b7285; }}
  .band-head {{
    display: flex; align-items: center; gap: 0.6em; margin-bottom: 0.75em;
  }}
  .band-head label {{ margin: 0; flex: 1; }}
  .band-head button {{ padding: 0.45em 0.7em; min-height: 44px; }}
  .band-controls {{ display: grid; gap: 0.8em; }}
  .band-controls .slider-row {{ margin: 0; grid-template-columns: minmax(4.2em, 5.2em) minmax(0, 1fr) minmax(4.6em, 5.4em); }}
  .band-type-row {{
    display: grid; grid-template-columns: minmax(4.2em, 5.2em) minmax(0, 1fr);
    align-items: center; gap: 0.8em;
  }}
  .band-type-row label {{ margin: 0; }}
  button.tiny {{ padding: 0.45em 0.7em; font-size: 0.92em; min-height: 44px; }}
  .freq-number {{
    width: 100%;
    font-variant-numeric: tabular-nums;
  }}
  .sr-only {{
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
  }}
  @media (max-width: 520px) {{
    body {{ padding: 0 0.8em; }}
    .slider-row, .band-controls .slider-row {{
      grid-template-columns: 1fr;
      gap: 0.35em;
    }}
    .value {{ text-align: left; }}
    .band-type-row {{ grid-template-columns: 1fr; gap: 0.35em; }}
    .segmented, .mode-row {{ grid-template-columns: 1fr; }}
    .band-head {{ flex-wrap: wrap; }}
    .band-head label {{ flex: 1 1 100%; }}
    .band-head button {{ flex: 1 1 calc(50% - 0.3em); }}
    .profile-actions {{
      display: grid;
      grid-template-columns: 1fr;
    }}
    .profile-actions button {{ width: 100%; min-height: 44px; }}
    .button-row {{
      display: grid;
      grid-template-columns: 1fr;
    }}
    .button-row button {{ width: 100%; min-height: 44px; }}
  }}
</style>
"""


def _index_html(csrf_token: str = "") -> bytes:
    csrf = csrf_meta_html(csrf_token) if csrf_token else ""
    body = (
        _PAGE_CSS
        + csrf
        + """
<p class="sub">Set the speaker's sound curve and preference EQ independently
from room correction.</p>

<div class="toolbar">
  <div class="toolbar-title">EQ</div>
  <label class="toggle" title="Turn preference EQ on or off">
    <input type="checkbox" id="eq-enabled" aria-label="Turn preference EQ on or off" disabled>
    <span class="track"></span>
  </label>
</div>

<div class="profile-panel">
  <div class="profile-state" id="profile-state">
    <div><strong>Applied:</strong> <span id="applied-profile-name">Loading...</span></div>
    <div><strong>Draft:</strong> <span id="draft-profile-state">Loading...</span></div>
  </div>
  <div class="profile-row">
    <label for="profile-select">Start from profile</label>
    <select id="profile-select" disabled></select>
    <div class="profile-note" id="profile-description"></div>
  </div>
  <div class="profile-row">
    <label for="profile-name">Custom profile name</label>
    <input type="text" id="profile-name" maxlength="48" autocomplete="off"
           placeholder="Custom Profile" disabled>
    <div class="profile-note">
      Saving here updates the profile library only. Use Apply to Speaker when
      you want this draft to become the live sound.
    </div>
  </div>
  <div class="profile-actions" id="profile-actions">
    <button type="button" id="save-new-profile" class="secondary" disabled>Save as Custom</button>
    <button type="button" id="update-profile" class="secondary" disabled>Update Custom</button>
    <button type="button" id="rename-profile" class="secondary" disabled>Rename</button>
    <button type="button" id="delete-profile" class="secondary" disabled>Delete</button>
  </div>
</div>

<div class="compare-row">
  <div>
    <label>Live compare</label>
    <div class="segmented" role="group" aria-label="Audition preference EQ">
      <button type="button" id="listen-bypass" data-mode="bypass" disabled>Bypass</button>
      <button type="button" id="listen-applied" data-mode="applied" disabled>Applied</button>
      <button type="button" id="listen-draft" data-mode="draft" disabled>Draft</button>
    </div>
  </div>
  <div class="compare-note" id="compare-note">
    Compare uses one shared headroom anchor so louder does not win by accident.
  </div>
</div>

<div class="eq-grid">
  <div class="field">
    <label for="curve">Sound curve</label>
    <select id="curve" disabled></select>
    <div class="curve-description" id="curve-description"></div>
  </div>

  <svg class="plot" id="plot" viewBox="0 0 620 190" role="img"
       aria-label="EQ response preview"></svg>
  <div class="sr-only" id="plot-summary" aria-live="polite"></div>
  <div class="meta-row">
    <span>Headroom: <strong id="headroom">0.0 dB</strong></span>
    <span id="updated">Not applied yet</span>
  </div>

  <div class="mode-row" role="group" aria-label="EQ editing mode">
    <button type="button" id="mode-basic" data-mode="basic" disabled>Basic</button>
    <button type="button" id="mode-advanced" data-mode="advanced" disabled>Advanced PEQ</button>
  </div>

  <div id="basic-controls">
    <div class="slider-row">
      <label for="bass">Bass</label>
      <input type="range" id="bass" min="-6" max="6" step="0.5" disabled>
      <div class="value" id="bass-value">0.0 dB</div>
    </div>
    <div class="slider-row">
      <label for="mid">Mid</label>
      <input type="range" id="mid" min="-6" max="6" step="0.5" disabled>
      <div class="value" id="mid-value">0.0 dB</div>
    </div>
    <div class="slider-row">
      <label for="treble">Treble</label>
      <input type="range" id="treble" min="-6" max="6" step="0.5" disabled>
      <div class="value" id="treble-value">0.0 dB</div>
    </div>
  </div>

  <section class="advanced-eq" id="advanced-controls" hidden>
    <div class="advanced-body">
      <div class="advanced-top">
        <span id="band-count">No advanced bands</span>
        <button type="button" id="add-band" class="secondary tiny" disabled>Add band</button>
      </div>
      <div class="band-list" id="band-list"></div>
    </div>
  </section>

  <div class="button-row">
    <button id="apply" disabled>Apply to Speaker</button>
    <button id="revert" class="secondary" disabled>Revert to Applied</button>
    <button id="reset" class="secondary" disabled>Reset Flat</button>
  </div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</div>

<script>
(function() {
  var savedProfile = null;
  var draftBands = [];
  var curvesById = {};
  var profileLibrary = [];
  var selectedProfileId = 'stock:flat';
  var applying = false;
  var previewTimer = null;
  var previewSeq = 0;
  var selectedBand = 0;
  var liveMode = 'applied';
  var eqMode = 'basic';
  var legacyMixedProfile = false;
  var controlsEnabled = false;
  var localPreviewFreqs = null;
  var limits = {
    simple_gain_db: 6,
    advanced_gain_db: 12,
    max_parametric_bands: 8,
    min_freq_hz: 20,
    max_freq_hz: 20000,
    min_q: 0.2,
    max_q: 10
  };
  function el(id) { return document.getElementById(id); }
  {csrf_fetch_helpers_js}
  function fmtDb(v) { return (Number(v) || 0).toFixed(1) + ' dB'; }
  function fmtFreq(v) {
    v = Number(v) || 0;
    return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + ' kHz' : Math.round(v) + ' Hz';
  }
  function fmtFreqInput(v) {
    return String(Math.round(clamp(v, limits.min_freq_hz, limits.max_freq_hz)));
  }
  function fmtQ(v) { return 'Q ' + (Number(v) || 0).toFixed(1); }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, Number(v) || 0)); }
  function clone(obj) { return JSON.parse(JSON.stringify(obj || {})); }
  function zeroSimpleEq() {
    return {bass_db: 0, mid_db: 0, treble_db: 0};
  }
  function simpleHasGain(profile) {
    var simple = (normalizeProfile(profile).simple_eq || {});
    return Math.abs(simple.bass_db || 0) >= 0.05 ||
           Math.abs(simple.mid_db || 0) >= 0.05 ||
           Math.abs(simple.treble_db || 0) >= 0.05;
  }
  function modeForProfile(profile) {
    profile = normalizeProfile(profile);
    return profile.parametric_bands.length ? 'advanced' : 'basic';
  }
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function(ch) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch];
    });
  }
  function freqToSlider(freq) {
    var lo = Math.log10(limits.min_freq_hz);
    var hi = Math.log10(limits.max_freq_hz);
    return Math.round((Math.log10(clamp(freq, limits.min_freq_hz, limits.max_freq_hz)) - lo) / (hi - lo) * 4000);
  }
  function sliderToFreq(pos) {
    var lo = Math.log10(limits.min_freq_hz);
    var hi = Math.log10(limits.max_freq_hz);
    return Math.pow(10, lo + clamp(pos, 0, 4000) / 4000 * (hi - lo));
  }
  function status(msg, isErr) {
    var node = el('status');
    node.textContent = msg || '';
    node.className = 'status-line' + (isErr ? ' err' : '');
  }
  function setControlsEnabled(on) {
    controlsEnabled = !!on;
    [
      'eq-enabled', 'curve', 'bass', 'mid', 'treble', 'apply', 'revert', 'reset',
      'add-band', 'listen-bypass', 'listen-applied', 'listen-draft',
      'profile-select', 'profile-name', 'save-new-profile', 'update-profile',
      'rename-profile', 'delete-profile',
      'mode-basic', 'mode-advanced'
    ].forEach(function(id) {
      el(id).disabled = !on || applying;
    });
    Array.prototype.forEach.call(el('band-list').querySelectorAll('input, select, button'), function(node) {
      node.disabled = !on || applying;
    });
    renderMode();
    updateProfileActions();
  }
  function normalizeProfile(raw) {
    raw = raw || {};
    var simple = raw.simple_eq || {};
    return {
      enabled: raw.enabled !== false,
      curve_id: raw.curve_id || 'flat',
      simple_eq: {
        bass_db: Number(simple.bass_db || 0),
        mid_db: Number(simple.mid_db || 0),
        treble_db: Number(simple.treble_db || 0)
      },
      parametric_bands: (raw.parametric_bands || []).map(function(b) {
        return {
          enabled: b.enabled !== false,
          type: b.type || b.biquad_type || 'Peaking',
          freq_hz: Number(b.freq_hz || b.freq || 1000),
          gain_db: Number(b.gain_db || b.gain || 0),
          q: Number(b.q || 1)
        };
      }),
      profile_id: raw.profile_id || '',
      profile_name: raw.profile_name || ''
    };
  }
  function profileIdentityForDraft() {
    var entry = selectedProfileEntry();
    if (!entry) return {profile_id: '', profile_name: ''};
    return {profile_id: entry.id, profile_name: entry.name};
  }
  function profileFromInputs() {
    var simple = eqMode === 'advanced' && !legacyMixedProfile ? zeroSimpleEq() : {
      bass_db: Number(el('bass').value || 0),
      mid_db: Number(el('mid').value || 0),
      treble_db: Number(el('treble').value || 0)
    };
    var identity = profileIdentityForDraft();
    return normalizeProfile({
      enabled: el('eq-enabled').checked,
      curve_id: el('curve').value || 'flat',
      simple_eq: simple,
      parametric_bands: eqMode === 'advanced' ? draftBands : [],
      profile_id: identity.profile_id,
      profile_name: identity.profile_name
    });
  }
  function bypassProfile() {
    var profile = profileFromInputs();
    profile.enabled = false;
    return profile;
  }
  function compareProfiles() {
    return [
      normalizeProfile(savedProfile),
      profileFromInputs(),
      bypassProfile()
    ];
  }
  function compareProfileForMode(mode) {
    if (mode === 'bypass') return bypassProfile();
    if (mode === 'applied') return normalizeProfile(savedProfile);
    return profileFromInputs();
  }
  function profileKey(profile) {
    profile = normalizeProfile(profile);
    return JSON.stringify({
      enabled: profile.enabled,
      curve_id: profile.curve_id,
      simple_eq: profile.simple_eq,
      parametric_bands: profile.parametric_bands
    });
  }
  function profileEntry(id) {
    return profileLibrary.find(function(entry) { return entry.id === id; }) || null;
  }
  function selectedProfileEntry() {
    return profileEntry(selectedProfileId);
  }
  function findProfileIdFor(profile) {
    profile = normalizeProfile(profile);
    if (profile.profile_id && profileEntry(profile.profile_id)) {
      return profile.profile_id;
    }
    var key = profileKey(profile);
    var stock = profileLibrary.find(function(entry) {
      return entry.kind === 'stock' && profileKey(entry.profile) === key;
    });
    if (stock) return stock.id;
    var custom = profileLibrary.find(function(entry) {
      return entry.kind === 'custom' && profileKey(entry.profile) === key;
    });
    if (custom) return custom.id;
    return 'stock:' + (normalizeProfile(profile).curve_id || 'flat');
  }
  function profileLabel(profile) {
    profile = normalizeProfile(profile);
    var matched = profile.profile_id ? profileEntry(profile.profile_id) : null;
    if (!matched) matched = profileEntry(findProfileIdFor(profile));
    var name = profile.profile_name || (matched ? matched.name : 'Unsaved Custom');
    if (matched && profileKey(profile) !== profileKey(matched.profile)) {
      return name + ' (edited)';
    }
    return name;
  }
  function profileNameForSave(entry) {
    if (entry && entry.kind === 'stock') return entry.name + ' Custom';
    if (entry) return entry.name;
    return 'Custom Profile';
  }
  function syncProfileName(entry) {
    el('profile-name').value = profileNameForSave(entry);
  }
  function draftDirtyFromApplied() {
    return profileKey(profileFromInputs()) !== profileKey(savedProfile);
  }
  function draftDirtyFromSelected() {
    var entry = selectedProfileEntry();
    if (!entry) return true;
    return profileKey(profileFromInputs()) !== profileKey(entry.profile);
  }
  function updateProfileState() {
    var appliedName = profileLabel(savedProfile);
    var entry = selectedProfileEntry();
    var draftName = entry ? entry.name : 'Unsaved Custom';
    var selectedDirty = draftDirtyFromSelected();
    var appliedDirty = draftDirtyFromApplied();
    el('applied-profile-name').textContent = appliedName;
    el('draft-profile-state').textContent =
      draftName + (selectedDirty ? ' with edits' : '') +
      (appliedDirty ? ' - not applied yet' : ' - matches applied sound');
  }
  function updateProfileActions() {
    var select = el('profile-select');
    var entry = selectedProfileEntry();
    if (select && select.value !== selectedProfileId) select.value = selectedProfileId;
    var isCustom = !!entry && entry.kind === 'custom';
    el('profile-description').textContent = entry
      ? (entry.kind === 'stock' ? 'Stock curve: ' : 'Custom profile: ') + (entry.description || entry.name)
      : '';
    el('profile-actions').classList.toggle('stock', !isCustom);
    el('save-new-profile').disabled = applying || !controlsEnabled;
    ['update-profile', 'rename-profile', 'delete-profile'].forEach(function(id) {
      el(id).hidden = !isCustom;
      el(id).disabled = applying || !controlsEnabled || !isCustom;
    });
    updateProfileState();
  }
  function renderMode() {
    var isAdvanced = eqMode === 'advanced';
    var disabled = applying || !controlsEnabled;
    el('basic-controls').hidden = isAdvanced;
    el('advanced-controls').hidden = !isAdvanced;
    el('mode-basic').classList.toggle('active', !isAdvanced);
    el('mode-advanced').classList.toggle('active', isAdvanced);
    ['bass', 'mid', 'treble'].forEach(function(id) {
      el(id).disabled = disabled || isAdvanced;
    });
    Array.prototype.forEach.call(el('band-list').querySelectorAll('input, select, button'), function(node) {
      node.disabled = disabled || !isAdvanced;
    });
    el('add-band').disabled = disabled || !isAdvanced;
  }
  function syncLabels() {
    el('bass-value').textContent = fmtDb(el('bass').value);
    el('mid-value').textContent = fmtDb(el('mid').value);
    el('treble-value').textContent = fmtDb(el('treble').value);
  }
  function prettyUpdated(value) {
    if (!value) return 'Not applied yet';
    var d = new Date(value);
    if (Number.isNaN(d.getTime())) return 'Updated';
    return 'Updated ' + d.toLocaleString([], {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
    });
  }
  function updateCurveDescription(curveId) {
    var curve = curvesById[curveId] || null;
    el('curve-description').textContent = curve ? curve.description || '' : '';
  }
  function updateAdvancedNote(profile) {
    var bands = profile && profile.parametric_bands ? profile.parametric_bands : [];
    var active = bands.filter(function(b) { return b && b.enabled !== false; }).length;
    el('band-count').textContent = active ? active + ' active advanced band' + (active === 1 ? '' : 's') : 'No active advanced bands';
  }
  function syncInputs(payload) {
    limits = Object.assign(limits, payload.limits || {});
    localPreviewFreqs = null;
    if (payload.profile_library) populateProfiles(payload.profile_library);
    savedProfile = normalizeProfile(payload.profile || {});
    selectedProfileId = findProfileIdFor(savedProfile);
    setFormFromProfile(savedProfile);
    syncProfileName(selectedProfileEntry());
    renderBands();
    el('headroom').textContent = fmtDb(payload.headroom_db || 0);
    el('updated').textContent = prettyUpdated((payload.profile || {}).updated_at);
    updateCurveDescription(savedProfile.curve_id || 'flat');
    updateAdvancedNote(savedProfile);
    renderPreview(payload, savedProfile.enabled !== false);
    updateCompareButtons(liveMode, payload.headroom_db || 0);
    updateProfileActions();
  }
  function setFormFromProfile(profile) {
    profile = normalizeProfile(profile);
    var simple = profile.simple_eq || {};
    eqMode = modeForProfile(profile);
    legacyMixedProfile = eqMode === 'advanced' && simpleHasGain(profile);
    el('eq-enabled').checked = profile.enabled !== false;
    el('curve').value = profile.curve_id || 'flat';
    el('bass').value = simple.bass_db || 0;
    el('mid').value = simple.mid_db || 0;
    el('treble').value = simple.treble_db || 0;
    draftBands = clone(profile.parametric_bands || []);
    syncLabels();
    updateCurveDescription(profile.curve_id || 'flat');
    updateAdvancedNote(profile);
    renderMode();
  }
  function renderPreview(payload, enabled) {
    el('headroom').textContent = fmtDb(payload.headroom_db || 0);
    drawPlot(payload.preview || [], enabled, payload.components || {});
    var peak = (payload.preview || []).reduce(function(max, point) {
      return Math.max(max, Number(point.db) || 0);
    }, 0);
    el('plot-summary').textContent = 'Preference EQ preview. Peak boost ' + fmtDb(peak) +
      ', headroom ' + fmtDb(payload.headroom_db || 0) + '.';
  }
  function previewFrequencies() {
    if (localPreviewFreqs) return localPreviewFreqs;
    localPreviewFreqs = [];
    for (var i = 0; i <= 120; i += 1) {
      localPreviewFreqs.push(limits.min_freq_hz * Math.pow(limits.max_freq_hz / limits.min_freq_hz, i / 120));
    }
    return localPreviewFreqs;
  }
  function specActive(spec) {
    return Math.abs(Number(spec.gain_db || 0)) >= 0.05;
  }
  function responseDb(spec, freq) {
    var safeFreq = Math.max(Number(freq) || 0, 1e-6);
    var center = Math.max(Number(spec.freq_hz || spec.freq || 1000), 1e-6);
    var gain = Number(spec.gain_db || spec.gain || 0);
    var type = spec.type || spec.biquad_type || 'Peaking';
    var x = Math.log(safeFreq / center) / Math.log(2);
    if (type === 'Lowshelf') return gain / (1 + Math.exp(3 * x));
    if (type === 'Highshelf') return gain / (1 + Math.exp(-3 * x));
    var q = Math.max(Number(spec.q || 1), 1e-3);
    var bw = 1 / q;
    return gain / (1 + Math.pow(x / bw, 2));
  }
  function simpleSpecs(profile) {
    var simple = profile.simple_eq || zeroSimpleEq();
    return [
      {type: 'Lowshelf', freq_hz: 105, gain_db: simple.bass_db || 0},
      {type: 'Peaking', freq_hz: 1000, gain_db: simple.mid_db || 0, q: 0.8},
      {type: 'Highshelf', freq_hz: 4000, gain_db: simple.treble_db || 0}
    ];
  }
  function advancedSpecs(profile) {
    return (profile.parametric_bands || []).filter(function(band) {
      return band && band.enabled !== false;
    }).map(function(band) {
      return {
        type: band.type || band.biquad_type || 'Peaking',
        freq_hz: band.freq_hz,
        gain_db: band.gain_db,
        q: band.q
      };
    });
  }
  function pointsForSpecs(specs, freqs, emptyWhenFlat) {
    specs = specs || [];
    var active = specs.some(specActive);
    if (emptyWhenFlat && !active) return [];
    return freqs.map(function(freq) {
      var db = specs.reduce(function(sum, spec) {
        return specActive(spec) ? sum + responseDb(spec, freq) : sum;
      }, 0);
      return {freq_hz: Math.round(freq * 1000) / 1000, db: Math.round(db * 1000) / 1000};
    });
  }
  function localPreviewPayload(profile) {
    profile = normalizeProfile(profile);
    var freqs = previewFrequencies();
    if (profile.enabled === false) {
      return {preview: pointsForSpecs([], freqs, false), components: {curve: [], simple: [], advanced: []}, headroom_db: 0};
    }
    var curveSpecs = ((curvesById[profile.curve_id] || {}).filters || []);
    var simple = simpleSpecs(profile);
    var advanced = advancedSpecs(profile);
    var preview = pointsForSpecs(curveSpecs.concat(simple, advanced), freqs, false);
    var peak = preview.reduce(function(max, point) {
      return Math.max(max, Number(point.db) || 0);
    }, 0);
    return {
      preview: preview,
      components: {
        curve: pointsForSpecs(curveSpecs, freqs, true),
        simple: pointsForSpecs(simple, freqs, true),
        advanced: (profile.parametric_bands || []).map(function(band, index) {
          return {
            index: index,
            enabled: band.enabled !== false,
            preview: pointsForSpecs(advancedSpecs({parametric_bands: [band]}), freqs, true)
          };
        })
      },
      headroom_db: Math.round(Math.max(0, peak) * 1000) / 1000
    };
  }
  function drawPath(points, cls, x, y, minDb, maxDb) {
    if (!points || !points.length) return '';
    var coords = points.map(function(p) {
      return [x(p.freq_hz), y(Math.max(minDb, Math.min(maxDb, p.db)))];
    });
    var d = 'M' + coords[0][0] + ' ' + coords[0][1];
    if (coords.length === 2) {
      d += ' L' + coords[1][0] + ' ' + coords[1][1];
    } else {
      for (var i = 1; i < coords.length - 1; i += 1) {
        var midX = (coords[i][0] + coords[i + 1][0]) / 2;
        var midY = (coords[i][1] + coords[i + 1][1]) / 2;
        d += ' Q' + coords[i][0] + ' ' + coords[i][1] + ' ' + midX + ' ' + midY;
      }
      d += ' Q' + coords[coords.length - 1][0] + ' ' + coords[coords.length - 1][1] +
           ' ' + coords[coords.length - 1][0] + ' ' + coords[coords.length - 1][1];
    }
    return '<path class="' + cls + '" d="' + d + '"></path>';
  }
  function drawBandMarkers(x, y, top, bottom, minDb, maxDb) {
    var html = '';
    if (eqMode !== 'advanced') return html;
    draftBands.forEach(function(band, index) {
      if (!band || band.enabled === false) return;
      var selected = index === selectedBand ? ' selected' : '';
      var freq = clamp(band.freq_hz, limits.min_freq_hz, limits.max_freq_hz);
      var gain = clamp(band.gain_db, minDb, maxDb);
      var cx = x(freq);
      var cy = y(gain);
      if ((band.type || 'Peaking') === 'Peaking') {
        var q = Math.max(Number(band.q || 1), 0.2);
        var lo = x(clamp(freq / Math.pow(2, 1 / q), limits.min_freq_hz, limits.max_freq_hz));
        var hi = x(clamp(freq * Math.pow(2, 1 / q), limits.min_freq_hz, limits.max_freq_hz));
        html += '<rect class="band-width' + selected + '" x="' + Math.min(lo, hi) + '" y="' + top +
                '" width="' + Math.abs(hi - lo) + '" height="' + (bottom - top) + '"></rect>';
      }
      html += '<line class="band-marker" x1="' + cx + '" x2="' + cx + '" y1="' + top +
              '" y2="' + bottom + '"></line>';
      html += '<circle class="band-dot' + selected + '" cx="' + cx + '" cy="' + cy +
              '" r="' + (selected ? 4.5 : 3.5) + '"></circle>';
    });
    return html;
  }
  function drawPlot(points, enabled, components) {
    var svg = el('plot');
    svg.classList.toggle('off', !enabled);
    var w = 620, h = 190, left = 42, right = 12, top = 12, bottom = 28;
    var minF = Math.log10(20), maxF = Math.log10(20000);
    var minDb = -12, maxDb = 12;
    function x(freq) {
      return left + (Math.log10(freq) - minF) / (maxF - minF) * (w - left - right);
    }
    function y(db) {
      return top + (maxDb - db) / (maxDb - minDb) * (h - top - bottom);
    }
    var html = '';
    [-6, 0, 6].forEach(function(db) {
      html += '<line class="' + (db === 0 ? 'zero' : 'grid') + '" x1="' + left + '" x2="' + (w - right) +
              '" y1="' + y(db) + '" y2="' + y(db) + '"></line>';
      html += '<text x="6" y="' + (y(db) + 4) + '">' + db + ' dB</text>';
    });
    [20, 100, 1000, 10000, 20000].forEach(function(freq) {
      html += '<line class="grid" y1="' + top + '" y2="' + (h - bottom) + '" x1="' + x(freq) +
              '" x2="' + x(freq) + '"></line>';
      html += '<text text-anchor="middle" x="' + x(freq) + '" y="' + (h - 8) + '">' +
              (freq >= 1000 ? (freq / 1000) + 'k' : freq) + '</text>';
    });
    html += drawPath((components || {}).curve || [], 'component', x, y, minDb, maxDb);
    html += drawPath((components || {}).simple || [], 'component', x, y, minDb, maxDb);
    ((components || {}).advanced || []).forEach(function(item) {
      html += drawPath(item.preview || [], item.index === selectedBand ? 'component selected' : 'component', x, y, minDb, maxDb);
    });
    html += drawPath(points, 'curve', x, y, minDb, maxDb);
    html += drawBandMarkers(x, y, top, h - bottom, minDb, maxDb);
    svg.innerHTML = html;
  }
  function populateCurves(curves) {
    curvesById = {};
    (curves || []).forEach(function(c) { curvesById[c.id] = c; });
    el('curve').innerHTML = (curves || []).map(function(c) {
      return '<option value="' + c.id + '">' + c.label + '</option>';
    }).join('');
  }
  function profileOptions(entries, kind, label) {
    var items = entries.filter(function(entry) { return entry.kind === kind; });
    if (!items.length) return '';
    return '<optgroup label="' + escapeHtml(label) + '">' + items.map(function(entry) {
      return '<option value="' + escapeHtml(entry.id) + '">' + escapeHtml(entry.name) + '</option>';
    }).join('') + '</optgroup>';
  }
  function populateProfiles(entries) {
    profileLibrary = entries || [];
    el('profile-select').innerHTML =
      profileOptions(profileLibrary, 'stock', 'Stock') +
      profileOptions(profileLibrary, 'custom', 'Custom');
    updateProfileActions();
  }
  function loadProfileEntry(entry) {
    if (!entry) return;
    selectedProfileId = entry.id;
    setFormFromProfile(entry.profile);
    syncProfileName(entry);
    renderBands();
    preview();
    updateProfileActions();
    status('Loaded ' + entry.name + ' as draft.');
  }
  async function mutateProfileLibrary(path, body) {
    applying = true;
    setControlsEnabled(true);
    try {
      var resp = await fetch(path, {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body || {})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'profile update failed');
      if (payload.profile_library) populateProfiles(payload.profile_library);
      if (payload.profile_entry) selectedProfileId = payload.profile_entry.id;
      if (payload.deleted_profile_id && selectedProfileId === payload.deleted_profile_id) {
        selectedProfileId = findProfileIdFor(profileFromInputs());
      }
      if (!profileEntry(selectedProfileId) && profileLibrary.length) {
        selectedProfileId = profileLibrary[0].id;
      }
      syncProfileName(selectedProfileEntry());
      updateProfileActions();
      return payload;
    } catch (e) {
      status('Could not update profiles: ' + e.message, true);
      return null;
    } finally {
      applying = false;
      setControlsEnabled(true);
    }
  }
  function bandSummary(band, index) {
    var state = band.enabled === false ? 'off' : 'on';
    return 'Band ' + (index + 1) + ' - ' + (band.type || 'Peaking') + ' - ' +
      fmtFreq(band.freq_hz) + ' - ' + fmtDb(band.gain_db) + ' - ' + fmtQ(band.q) + ' - ' + state;
  }
  function renderBands() {
    if (!draftBands.length) {
      el('band-list').innerHTML = '<div class="band-empty">No advanced bands yet.</div>';
      updateAdvancedNote(profileFromInputs());
      setControlsEnabled(true);
      return;
    }
    if (selectedBand >= draftBands.length) selectedBand = draftBands.length - 1;
    if (selectedBand < 0) selectedBand = 0;
    el('band-list').innerHTML = draftBands.map(function(band, index) {
      band = normalizeProfile({parametric_bands: [band]}).parametric_bands[0];
      draftBands[index] = band;
      var selected = index === selectedBand ? ' selected' : '';
      var checked = band.enabled !== false ? ' checked' : '';
      var qDisabled = band.type === 'Peaking' ? '' : ' disabled';
      return '<div class="band-item' + selected + '" data-index="' + index + '">' +
        '<div class="band-head">' +
          '<label><input type="checkbox" data-kind="enabled" data-index="' + index + '"' + checked + '> Band ' + (index + 1) + '</label>' +
          '<button type="button" class="secondary tiny" data-action="delete" data-index="' + index + '">Delete</button>' +
        '</div>' +
        '<div class="band-controls" aria-label="' + bandSummary(band, index) + '">' +
          '<div class="band-type-row"><label>Type</label><select data-kind="type" data-index="' + index + '">' +
            '<option value="Peaking"' + (band.type === 'Peaking' ? ' selected' : '') + '>Peak</option>' +
            '<option value="Lowshelf"' + (band.type === 'Lowshelf' ? ' selected' : '') + '>Low shelf</option>' +
            '<option value="Highshelf"' + (band.type === 'Highshelf' ? ' selected' : '') + '>High shelf</option>' +
          '</select></div>' +
          '<div class="slider-row"><label>Freq</label><input type="range" min="0" max="4000" step="1" value="' + freqToSlider(band.freq_hz) + '" data-kind="freq-slider" data-index="' + index + '"><input class="freq-number" type="number" min="' + limits.min_freq_hz + '" max="' + limits.max_freq_hz + '" step="1" value="' + fmtFreqInput(band.freq_hz) + '" data-kind="freq-number" data-index="' + index + '" aria-label="Band ' + (index + 1) + ' frequency in Hz"></div>' +
          '<div class="slider-row"><label>Gain</label><input type="range" min="-' + limits.advanced_gain_db + '" max="' + limits.advanced_gain_db + '" step="0.5" value="' + band.gain_db + '" data-kind="gain" data-index="' + index + '"><div class="value" data-readout="gain">' + fmtDb(band.gain_db) + '</div></div>' +
          '<div class="slider-row"><label>Width</label><input type="range" min="' + limits.min_q + '" max="' + limits.max_q + '" step="0.1" value="' + band.q + '" data-kind="q" data-index="' + index + '"' + qDisabled + '><div class="value" data-readout="q">' + (band.type === 'Peaking' ? fmtQ(band.q) : 'Shelf') + '</div></div>' +
        '</div>' +
      '</div>';
    }).join('');
    updateAdvancedNote(profileFromInputs());
    setControlsEnabled(true);
  }
  function updateBandReadouts(index) {
    var item = el('band-list').querySelector('[data-index="' + index + '"]');
    var band = draftBands[index];
    if (!item || !band) return;
    var slider = item.querySelector('[data-kind="freq-slider"]');
    var number = item.querySelector('[data-kind="freq-number"]');
    if (slider && document.activeElement !== slider) slider.value = freqToSlider(band.freq_hz);
    if (number && document.activeElement !== number) number.value = fmtFreqInput(band.freq_hz);
    item.querySelector('[data-readout="gain"]').textContent = fmtDb(band.gain_db);
    item.querySelector('[data-readout="q"]').textContent = band.type === 'Peaking' ? fmtQ(band.q) : 'Shelf';
  }
  function schedulePreview() {
    var profile = profileFromInputs();
    syncLabels();
    updateCurveDescription(el('curve').value || 'flat');
    updateAdvancedNote(profile);
    renderPreview(localPreviewPayload(profile), profile.enabled !== false);
    updateProfileState();
    status('Unsaved changes.');
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(preview, 90);
  }
  async function preview() {
    var seq = ++previewSeq;
    try {
      var profile = profileFromInputs();
      var resp = await fetch('./preview', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(profile)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'preview failed');
      if (seq !== previewSeq) return;
      renderPreview(payload, profile.enabled !== false);
    } catch (e) {
      if (seq !== previewSeq) return;
      status('Could not preview EQ: ' + e.message, true);
    }
  }
  function updateCompareButtons(mode, headroom) {
    liveMode = mode || liveMode;
    ['bypass', 'applied', 'draft'].forEach(function(name) {
      el('listen-' + name).classList.toggle('active', liveMode === name);
    });
    el('compare-note').textContent = 'Live: ' + liveMode.charAt(0).toUpperCase() + liveMode.slice(1) +
      '. Compare headroom anchor: ' + fmtDb(headroom || 0) + '.';
  }
  async function audition(mode) {
    applying = true;
    setControlsEnabled(true);
    status('Loading ' + mode + '...');
    try {
      var resp = await fetch('./audition', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          mode: mode,
          profile: compareProfileForMode(mode),
          compare_profiles: compareProfiles()
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'audition failed');
      updateCompareButtons(mode, payload.audition_headroom_db || 0);
      status('Listening to ' + mode + '.');
    } catch (e) {
      status('Could not audition EQ: ' + e.message, true);
    } finally {
      applying = false;
      setControlsEnabled(true);
    }
  }
  async function loadState() {
    try {
      var resp = await fetch('./state', {cache: 'no-store'});
      if (!resp.ok) throw new Error('state failed');
      var payload = await resp.json();
      populateCurves(payload.curves);
      syncInputs(payload);
      liveMode = 'applied';
      updateCompareButtons('applied', payload.headroom_db || 0);
      setControlsEnabled(true);
    } catch (e) {
      status('Could not load sound profile: ' + e.message, true);
    }
  }
  async function apply(profile) {
    applying = true;
    setControlsEnabled(true);
    status('Applying...');
    try {
      var resp = await fetch('./apply', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(profile)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'apply failed');
      liveMode = 'applied';
      syncInputs(payload);
      status('Applied to speaker.');
    } catch (e) {
      status('Could not apply EQ: ' + e.message, true);
    } finally {
      applying = false;
      setControlsEnabled(true);
    }
  }
  ['bass', 'mid', 'treble'].forEach(function(id) {
    el(id).addEventListener('input', schedulePreview);
  });
  el('curve').addEventListener('change', schedulePreview);
  ['basic', 'advanced'].forEach(function(mode) {
    el('mode-' + mode).addEventListener('click', function() {
      if (eqMode === mode && !legacyMixedProfile) return;
      eqMode = mode;
      legacyMixedProfile = false;
      if (mode === 'advanced') {
        el('bass').value = 0;
        el('mid').value = 0;
        el('treble').value = 0;
      }
      syncLabels();
      renderBands();
      renderMode();
      schedulePreview();
    });
  });
  el('apply').addEventListener('click', function() { apply(profileFromInputs()); });
  el('eq-enabled').addEventListener('change', function() {
    schedulePreview();
    audition(el('eq-enabled').checked ? 'draft' : 'bypass');
  });
  ['bypass', 'applied', 'draft'].forEach(function(mode) {
    el('listen-' + mode).addEventListener('click', function() { audition(mode); });
  });
  el('profile-select').addEventListener('change', function() {
    selectedProfileId = el('profile-select').value;
    loadProfileEntry(selectedProfileEntry());
  });
  el('profile-name').addEventListener('input', updateProfileActions);
  el('save-new-profile').addEventListener('click', async function() {
    var entry = selectedProfileEntry();
    var profile = profileFromInputs();
    var name = el('profile-name').value || profileNameForSave(entry);
    var payload = await mutateProfileLibrary('./profiles/save', {
      id: null,
      name: name,
      profile: profile
    });
    if (payload && payload.profile_entry) {
      status('Saved profile ' + payload.profile_entry.name + '.');
    }
  });
  el('update-profile').addEventListener('click', async function() {
    var entry = selectedProfileEntry();
    if (!entry || entry.kind !== 'custom') return;
    var profile = profileFromInputs();
    var payload = await mutateProfileLibrary('./profiles/save', {
      id: entry.id,
      name: el('profile-name').value || entry.name,
      profile: profile
    });
    if (payload && payload.profile_entry) {
      status('Updated profile ' + payload.profile_entry.name + '.');
    }
  });
  el('rename-profile').addEventListener('click', async function() {
    var entry = selectedProfileEntry();
    if (!entry || entry.kind !== 'custom') return;
    var payload = await mutateProfileLibrary('./profiles/rename', {
      id: entry.id,
      name: el('profile-name').value || entry.name
    });
    if (payload && payload.profile_entry) {
      status('Renamed profile to ' + payload.profile_entry.name + '.');
    }
  });
  el('delete-profile').addEventListener('click', async function() {
    var entry = selectedProfileEntry();
    if (!entry || entry.kind !== 'custom') return;
    if (!window.confirm('Delete profile "' + entry.name + '"?')) return;
    var payload = await mutateProfileLibrary('./profiles/delete', {id: entry.id});
    if (payload) status('Deleted profile.');
  });
  el('add-band').addEventListener('click', function() {
    if (draftBands.length >= limits.max_parametric_bands) {
      status('Advanced EQ is limited to ' + limits.max_parametric_bands + ' bands.', true);
      return;
    }
    draftBands.push({enabled: true, type: 'Peaking', freq_hz: 1000, gain_db: 0, q: 1});
    selectedBand = draftBands.length - 1;
    eqMode = 'advanced';
    renderBands();
    schedulePreview();
  });
  el('band-list').addEventListener('click', function(ev) {
    var action = ev.target.getAttribute('data-action');
    if (!action) return;
    var index = Number(ev.target.getAttribute('data-index'));
    if (action === 'delete') {
      draftBands.splice(index, 1);
      selectedBand = Math.max(0, Math.min(selectedBand, draftBands.length - 1));
      renderBands();
      schedulePreview();
    }
  });
  el('band-list').addEventListener('input', function(ev) {
    var index = Number(ev.target.getAttribute('data-index'));
    var kind = ev.target.getAttribute('data-kind');
    var band = draftBands[index];
    if (!band || !kind) return;
    selectedBand = index;
    if (kind === 'enabled') band.enabled = ev.target.checked;
    if (kind === 'freq-slider') band.freq_hz = sliderToFreq(ev.target.value);
    if (kind === 'freq-number') {
      if (String(ev.target.value).trim() === '') return;
      band.freq_hz = clamp(ev.target.value, limits.min_freq_hz, limits.max_freq_hz);
    }
    if (kind === 'gain') band.gain_db = clamp(ev.target.value, -limits.advanced_gain_db, limits.advanced_gain_db);
    if (kind === 'q') band.q = clamp(ev.target.value, limits.min_q, limits.max_q);
    updateBandReadouts(index);
    schedulePreview();
  });
  el('band-list').addEventListener('change', function(ev) {
    var index = Number(ev.target.getAttribute('data-index'));
    var kind = ev.target.getAttribute('data-kind');
    var band = draftBands[index];
    if (!band || !kind) return;
    selectedBand = index;
    if (kind === 'type') band.type = ev.target.value;
    if (kind === 'enabled') band.enabled = ev.target.checked;
    renderBands();
    schedulePreview();
  });
  el('revert').addEventListener('click', function() {
    setFormFromProfile(savedProfile);
    renderBands();
    preview();
    audition('applied');
  });
  el('reset').addEventListener('click', function() {
    setFormFromProfile({enabled: true, curve_id: 'flat',
                        simple_eq: {bass_db: 0, mid_db: 0, treble_db: 0},
                        parametric_bands: []});
    selectedProfileId = 'stock:flat';
    syncProfileName(selectedProfileEntry());
    updateProfileActions();
    renderBands();
    preview();
    status('Draft reset to Flat. Apply to Speaker when ready.');
  });
  loadState();
})();
</script>
"""
    )
    return wrap_page(
        "Sound",
        body.replace("{csrf_fetch_helpers_js}", csrf_fetch_helpers_js()),
    )


def _make_handler(
    *,
    profile_path: str | Path,
    library_path: str | Path,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length > MAX_JSON_BYTES:
                raise ValueError("request body too large")
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                ctx = begin_request(self)
                self._send_html(_index_html(ctx["csrf_token"]))
                return
            if path == "/state":
                self._send_json(
                    _state_payload(
                        load_profile(profile_path),
                        library_path=library_path,
                        include_library=True,
                    )
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/apply",
                "/audition",
                "/preview",
                "/profiles/save",
                "/profiles/rename",
                "/profiles/delete",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not verify_csrf(self):
                reject_csrf(self)
                return
            try:
                raw = self._read_json()
                if path.startswith("/profiles/"):
                    try:
                        if path == "/profiles/save":
                            requested_id = str(raw.get("id") or "")
                            entry = save_named_profile(
                                SoundProfile.from_mapping(raw.get("profile")),
                                name=raw.get("name"),
                                path=library_path,
                                profile_id=requested_id,
                            )
                            action = "update" if requested_id == entry.id else "create"
                            logger.info(
                                "event=sound.profile_library action=%s "
                                "profile_id=%s curve=%s bands=%d",
                                action,
                                entry.id,
                                entry.profile.curve_id,
                                len(entry.profile.parametric_bands),
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["profile_entry"] = entry.to_payload()
                        elif path == "/profiles/rename":
                            entry = rename_named_profile(
                                str(raw.get("id") or ""),
                                name=str(raw.get("name") or ""),
                                path=library_path,
                            )
                            logger.info(
                                "event=sound.profile_library action=rename "
                                "profile_id=%s curve=%s bands=%d",
                                entry.id,
                                entry.profile.curve_id,
                                len(entry.profile.parametric_bands),
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["profile_entry"] = entry.to_payload()
                        else:
                            deleted_id = str(raw.get("id") or "")
                            delete_named_profile(deleted_id, path=library_path)
                            logger.info(
                                "event=sound.profile_library action=delete profile_id=%s",
                                deleted_id,
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["deleted_profile_id"] = deleted_id
                    except OSError as e:
                        logger.exception("sound profile library update failed")
                        self._send_json({"error": str(e)}, status=502)
                        return
                    self._send_json(payload)
                    return
                if path == "/audition":
                    raw_profile = raw.get("profile", raw)
                else:
                    raw_profile = raw
                profile = SoundProfile.from_mapping(raw_profile)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
                self._send_json({"error": str(e)}, status=400)
                return
            if path == "/preview":
                self._send_json(_state_payload(profile))
                return
            try:
                if path == "/audition":
                    raw_compare = raw.get("compare_profiles", [])
                    if not isinstance(raw_compare, list):
                        raw_compare = []
                    compare_profiles = [
                        SoundProfile.from_mapping(item) for item in raw_compare[:3]
                    ]
                    audition_mode = str(raw.get("mode") or "draft")
                    if audition_mode not in {"bypass", "applied", "draft"}:
                        audition_mode = "draft"
                    payload = asyncio.run(
                        _audition_profile(
                            profile,
                            compare_profiles=compare_profiles,
                            audition_mode=audition_mode,
                            profile_path=profile_path,
                            library_path=library_path,
                            config_dir=config_dir,
                            camilla_factory=camilla_factory,
                        )
                    )
                else:
                    payload = asyncio.run(
                        _apply_profile(
                            profile,
                            profile_path=profile_path,
                            library_path=library_path,
                            config_dir=config_dir,
                            camilla_factory=camilla_factory,
                        )
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("sound profile apply failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(payload)

    return Handler


def make_server(
    target,
    *,
    profile_path: str | Path | None = None,
    library_path: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> ThreadingHTTPServer:
    from . import _systemd

    return _systemd.make_http_server(
        target,
        _make_handler(
            profile_path=profile_path
            or os.environ.get(
                "JASPER_SOUND_PROFILE_PATH",
                PROFILE_PATH,
            ),
            library_path=library_path
            or os.environ.get(
                "JASPER_SOUND_PROFILE_LIBRARY_PATH",
                PROFILE_LIBRARY_PATH,
            ),
            config_dir=config_dir
            or os.environ.get(
                "JASPER_SOUND_CONFIG_DIR",
                DEFAULT_CONFIG_DIR,
            ),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-sound-web",
        description="Sound curve and preference-EQ wizard",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_SOUND_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("JASPER_SOUND_WEB_PORT", "8784")),
    )
    parser.add_argument(
        "--profile-path",
        default=os.environ.get("JASPER_SOUND_PROFILE_PATH", PROFILE_PATH),
    )
    parser.add_argument(
        "--library-path",
        default=os.environ.get(
            "JASPER_SOUND_PROFILE_LIBRARY_PATH",
            PROFILE_LIBRARY_PATH,
        ),
    )
    parser.add_argument(
        "--config-dir",
        default=os.environ.get("JASPER_SOUND_CONFIG_DIR", DEFAULT_CONFIG_DIR),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(
        (args.host, args.port),
        profile_path=args.profile_path,
        library_path=args.library_path,
        config_dir=args.config_dir,
    )
    logger.info("jasper-sound-web listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
