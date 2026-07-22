# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language bridge: the phone-side ambient-stats EMITTER
(capture-page/js/ambient-stats.js) against the REAL Pi-side PARSER
(jasper.audio_measurement.level_solver.parse_ambient_stats_event, #1543).

tests/js/capture_ambient_stats_test.mjs already pins the JS module's own
behavior in isolation. This file runs the JS emitter in Node, feeds its exact
JSON output into the real Python parser, and asserts the parser accepts it —
proving field-for-field wire compatibility rather than two independently
maintained schema descriptions drifting apart.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper.audio_measurement.level_solver import (
    AmbientBand,
    parse_ambient_stats_event,
)

_REPO = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")

# A tiny Node script: build a synthetic quiet-window buffer (a 250 Hz tone,
# matching the JS unit harness), emit the wire event via the real module, and
# print it as the last stdout line. Mirrors the strip-and-eval pattern the
# other capture-page bridges use (see ambient-stats.js's own dependency on
# measurement-audio.js / level-events.js, neither resolvable from a bare Node
# script path).
_EMIT_SCRIPT = r"""
const fs = require("fs");
const path = require("path");
const modulePath = path.resolve(process.argv[1], "capture-page/js/ambient-stats.js");
const raw = fs.readFileSync(modulePath, "utf8");
const rewritten = raw
  .replace(
    /^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']*measurement-audio\.js[^"']*["'];\s*/m,
    "const rmsToDbfs = (rms) => { const v = Number(rms); return v > 0 ? 20 * Math.log10(v) : -120; };\n",
  )
  .replace(
    /^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']*level-events\.js[^"']*["'];\s*/m,
    "const CLIP_ABS_THRESHOLD = 0.999;\n",
  );
const dataUrl = "data:text/javascript;base64," + Buffer.from(rewritten, "utf8").toString("base64");
import(dataUrl).then((m) => {
  const sampleRate = 48000;
  const durationS = 0.8;
  const n = Math.round(sampleRate * durationS);
  const clip = process.argv[2] === "clip";
  const samples = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    samples[i] = (clip ? 1.5 : 0.01) * Math.sin((2 * Math.PI * 250 * i) / sampleRate);
  }
  const event = m.buildAmbientStatsEvent(samples, sampleRate, process.argv[3], durationS);
  console.log(JSON.stringify(event));
});
"""


def _emit_event(run_token: str, *, clip: bool = False) -> dict:
    assert _NODE is not None
    proc = subprocess.run(
        [_NODE, "--input-type=commonjs", "-e", _EMIT_SCRIPT, "--",
         str(_REPO), "clip" if clip else "quiet", run_token],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_js_emitted_event_parses_cleanly_on_the_pi():
    event = _emit_event("bridge-run-token")
    bands = parse_ambient_stats_event(event, expected_run_token="bridge-run-token")
    assert bands is not None
    assert 1 <= len(bands) <= 64
    for band in bands:
        assert isinstance(band, AmbientBand)
        assert band.lo_hz > 0
        assert band.hi_hz > band.lo_hz
    # The tone is centered at 250 Hz; the loudest reported band should
    # contain it, same assertion as the JS-only unit harness.
    loudest = max(bands, key=lambda b: b.rms_dbfs)
    assert loudest.lo_hz <= 250.0 <= loudest.hi_hz


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_js_emitted_event_run_token_mismatch_falls_back():
    event = _emit_event("bridge-run-token")
    assert parse_ambient_stats_event(event, expected_run_token="a-different-token") is None


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_js_emitted_clipped_event_falls_back():
    event = _emit_event("bridge-run-token", clip=True)
    assert event["ambient_stats"]["clipped"] is True
    assert parse_ambient_stats_event(event, expected_run_token="bridge-run-token") is None
