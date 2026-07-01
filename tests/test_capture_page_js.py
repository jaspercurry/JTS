# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run the capture-page JS harnesses inside the pytest CI lane.

The static capture page (Cloudflare Pages) is JavaScript, but its security- and
contract-critical pieces are pure modules exercised by Node harnesses:

  - the fixed DATA renderer (XSS-inert: <script>/onerror=/javascript:/hostile
    component types render inert) — the plan §15 acceptance test;
  - the E2E crypto wire format (AES-256-GCM, IV-prepended, plaintext integrity);
  - the relay client request contract; and
  - the fragment parser.

Bridging them through pytest (mirroring ``tests/test_sound_setup.py``) keeps the
page covered by the existing Python CI matrix with no extra CI wiring.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper.correction.calibration import SUPPORTED_MODELS

_JS_DIR = Path(__file__).resolve().parent / "js"
_NODE = shutil.which("node")
_REPO = Path(__file__).resolve().parents[1]

_HARNESSES = [
    "capture_render_test.mjs",
    "capture_crypto_test.mjs",
    "capture_relay_client_test.mjs",
    "capture_fragment_test.mjs",
    "capture_constraints_test.mjs",
    "capture_wakelock_test.mjs",
]


@pytest.mark.parametrize("harness", _HARNESSES)
def test_capture_page_harness(harness: str):
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_JS_DIR / harness)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True, out
    assert out["passed"] >= 1, out


def test_capture_page_expired_link_message_points_back_to_speaker():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'message === "not_found"' in main_js
    assert "This one-time capture link has expired." in main_js
    assert "Return to the speaker page" in main_js


def test_capture_page_waits_for_pi_sweep_completion():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "Measuring room noise" in main_js
    assert "fetchPhoneStatus" in main_js
    assert 'phase === "sweep_complete"' in main_js
    assert "recordWindowMs" not in main_js


def test_capture_page_serial_models_match_pi_registry_keys():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "spec.calibration_models" in main_js
    for key in SUPPORTED_MODELS:
        assert f'value: "{key}"' not in main_js
        assert f'value: \'{key}\'' not in main_js
    for stale in (
        "minidsp_umik_1",
        "minidsp_umik_2",
        "dayton_imm_6c",
        "dayton_umm_6",
    ):
        assert stale not in main_js
