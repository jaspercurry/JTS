"""Sound curve and preference-EQ page at /sound/.

URL surface (after nginx strips /sound/):
  GET  /         page render
  GET  /state    persisted profile + preview + stock curve metadata
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
    PROFILE_PATH,
    SoundProfile,
    build_sound_filters,
    curve_payload,
    estimate_headroom_db,
    load_profile,
    response_preview,
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


def _state_payload(profile: SoundProfile) -> dict[str, Any]:
    from jasper.dsp_apply import last_dsp_apply_state

    return {
        "profile": profile.to_dict(),
        "curves": curve_payload(),
        "preview": response_preview(profile),
        "headroom_db": estimate_headroom_db(profile),
        "last_dsp_apply": last_dsp_apply_state(),
    }


async def _apply_profile(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    from jasper.sound.camilla_yaml import (
        BASE_CONFIG_PATH,
        emit_sound_config,
        extract_room_peqs_from_config,
        is_base_config,
        is_jts_generated_config,
        sound_config_path,
    )
    from jasper.dsp_apply import apply_dsp_config

    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)
    profile_id = str(time.time_ns())
    out_path = sound_config_path(config_path)
    stamped = profile.with_timestamp()
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
            stamped,
            room_peqs=room_peqs,
            out_path=out_path,
            profile_id=profile_id,
        )
        return {
            "prior_config_path": current_path,
            "room_peq_count": len(room_peqs),
            "sound_filter_count": len(build_sound_filters(stamped)),
        }

    apply_state = await apply_dsp_config(
        source="sound",
        candidate_path=out_path,
        prepare=_prepare_config,
        load_config=lambda path: cam.set_config_file_path(
            path, best_effort=False,
        ),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
        persist=lambda: save_profile(stamped, profile_path),
        sound_filter_count=len(build_sound_filters(stamped)),
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
    payload = _state_payload(stamped)
    payload["active_config_path"] = str(out_path)
    payload["preserved_room_peqs"] = apply_state.room_peq_count or 0
    payload["last_dsp_apply"] = apply_state.to_dict()
    return payload


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
  select {{ max-width: 100%; }}
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
  .plot .curve {{ fill: none; stroke: #1db954; stroke-width: 2.5; }}
  .plot.off .curve {{ stroke: #888; stroke-dasharray: 5 4; }}
  .meta-row {{
    display: flex; flex-wrap: wrap; gap: 0.8em; color: #666;
    font-size: 0.92em; margin-top: 0.5em;
  }}
  .curve-description {{ color: #666; margin-top: 0.35em; }}
  .advanced-note {{ display: none; color: #666; font-size: 0.92em; }}
  .advanced-note.on {{ display: block; }}
  .sr-only {{
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
  }}
</style>
"""


def _index_html(csrf_token: str = "") -> bytes:
    csrf = csrf_meta_html(csrf_token) if csrf_token else ""
    body = _PAGE_CSS + csrf + """
<p class="sub">Set the speaker's sound curve and preference EQ independently
from room correction.</p>

<div class="toolbar">
  <div class="toolbar-title">EQ</div>
  <label class="toggle" title="Turn preference EQ on or off">
    <input type="checkbox" id="eq-enabled" aria-label="Turn preference EQ on or off" disabled>
    <span class="track"></span>
  </label>
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
  <div class="advanced-note" id="advanced-note"></div>

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

  <div class="button-row">
    <button id="apply" disabled>Apply</button>
    <button id="reset" class="secondary" disabled>Reset</button>
  </div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</div>

<script>
(function() {
  var latest = null;
  var curvesById = {};
  var applying = false;
  var previewTimer = null;
  function el(id) { return document.getElementById(id); }
  {csrf_fetch_helpers_js}
  function fmtDb(v) { return (Number(v) || 0).toFixed(1) + ' dB'; }
  function status(msg, isErr) {
    var node = el('status');
    node.textContent = msg || '';
    node.className = 'status-line' + (isErr ? ' err' : '');
  }
  function setControlsEnabled(on) {
    ['eq-enabled', 'curve', 'bass', 'mid', 'treble', 'apply', 'reset'].forEach(function(id) {
      el(id).disabled = !on || applying;
    });
  }
  function profileFromInputs() {
    return {
      enabled: el('eq-enabled').checked,
      curve_id: el('curve').value || 'flat',
      simple_eq: {
        bass_db: Number(el('bass').value || 0),
        mid_db: Number(el('mid').value || 0),
        treble_db: Number(el('treble').value || 0)
      },
      parametric_bands: latest && latest.profile ? latest.profile.parametric_bands || [] : []
    };
  }
  function appliedProfileWithEnabled(enabled) {
    var profile = Object.assign({}, latest && latest.profile ? latest.profile : {});
    profile.enabled = enabled;
    profile.simple_eq = profile.simple_eq || {bass_db: 0, mid_db: 0, treble_db: 0};
    profile.parametric_bands = profile.parametric_bands || [];
    profile.curve_id = profile.curve_id || 'flat';
    return profile;
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
    var node = el('advanced-note');
    node.className = 'advanced-note' + (active ? ' on' : '');
    node.textContent = active ? active + ' advanced EQ band' + (active === 1 ? '' : 's') + ' active.' : '';
  }
  function syncInputs(payload) {
    latest = payload;
    var profile = payload.profile || {};
    var simple = profile.simple_eq || {};
    el('eq-enabled').checked = profile.enabled !== false;
    el('curve').value = profile.curve_id || 'flat';
    el('bass').value = simple.bass_db || 0;
    el('mid').value = simple.mid_db || 0;
    el('treble').value = simple.treble_db || 0;
    syncLabels();
    el('headroom').textContent = fmtDb(payload.headroom_db || 0);
    el('updated').textContent = prettyUpdated(profile.updated_at);
    updateCurveDescription(profile.curve_id || 'flat');
    updateAdvancedNote(profile);
    renderPreview(payload, profile.enabled !== false);
  }
  function renderPreview(payload, enabled) {
    el('headroom').textContent = fmtDb(payload.headroom_db || 0);
    drawPlot(payload.preview || [], enabled);
    var peak = (payload.preview || []).reduce(function(max, point) {
      return Math.max(max, Number(point.db) || 0);
    }, 0);
    el('plot-summary').textContent = 'Preference EQ preview. Peak boost ' + fmtDb(peak) +
      ', headroom ' + fmtDb(payload.headroom_db || 0) + '.';
  }
  function drawPlot(points, enabled) {
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
    if (points.length) {
      var d = points.map(function(p, i) {
        return (i ? 'L' : 'M') + x(p.freq_hz) + ' ' + y(Math.max(minDb, Math.min(maxDb, p.db)));
      }).join(' ');
      html += '<path class="curve" d="' + d + '"></path>';
    }
    svg.innerHTML = html;
  }
  function populateCurves(curves) {
    curvesById = {};
    (curves || []).forEach(function(c) { curvesById[c.id] = c; });
    el('curve').innerHTML = (curves || []).map(function(c) {
      return '<option value="' + c.id + '">' + c.label + '</option>';
    }).join('');
  }
  function schedulePreview() {
    syncLabels();
    updateCurveDescription(el('curve').value || 'flat');
    status('Unsaved changes.');
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(preview, 120);
  }
  async function preview() {
    try {
      var profile = profileFromInputs();
      var resp = await fetch('./preview', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(profile)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'preview failed');
      renderPreview(payload, profile.enabled !== false);
    } catch (e) {
      status('Could not preview EQ: ' + e.message, true);
    }
  }
  async function loadState() {
    try {
      var resp = await fetch('./state', {cache: 'no-store'});
      if (!resp.ok) throw new Error('state failed');
      var payload = await resp.json();
      populateCurves(payload.curves);
      syncInputs(payload);
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
      syncInputs(payload);
      status('Applied.');
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
  el('apply').addEventListener('click', function() { apply(profileFromInputs()); });
  el('eq-enabled').addEventListener('change', function() {
    apply(appliedProfileWithEnabled(el('eq-enabled').checked));
  });
  el('reset').addEventListener('click', function() {
    apply({enabled: true, curve_id: 'flat',
           simple_eq: {bass_db: 0, mid_db: 0, treble_db: 0},
           parametric_bands: []});
  });
  loadState();
})();
</script>
"""
    return wrap_page(
        "Sound",
        body.replace("{csrf_fetch_helpers_js}", csrf_fetch_helpers_js()),
    )


def _make_handler(
    *,
    profile_path: str | Path,
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
                self._send_json(_state_payload(load_profile(profile_path)))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {"/apply", "/preview"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not verify_csrf(self):
                reject_csrf(self)
                return
            try:
                profile = SoundProfile.from_mapping(self._read_json())
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
                self._send_json({"error": str(e)}, status=400)
                return
            if path == "/preview":
                self._send_json(_state_payload(profile))
                return
            try:
                payload = asyncio.run(_apply_profile(
                    profile,
                    profile_path=profile_path,
                    config_dir=config_dir,
                    camilla_factory=camilla_factory,
                ))
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
    config_dir: str | Path | None = None,
) -> ThreadingHTTPServer:
    from . import _systemd

    return _systemd.make_http_server(
        target,
        _make_handler(
            profile_path=profile_path or os.environ.get(
                "JASPER_SOUND_PROFILE_PATH", PROFILE_PATH,
            ),
            config_dir=config_dir or os.environ.get(
                "JASPER_SOUND_CONFIG_DIR", DEFAULT_CONFIG_DIR,
            ),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-sound-web",
        description="Sound curve and preference-EQ wizard",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_SOUND_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_SOUND_WEB_PORT", "8784")),
    )
    parser.add_argument(
        "--profile-path",
        default=os.environ.get("JASPER_SOUND_PROFILE_PATH", PROFILE_PATH),
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
