# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run the phone-mic relay Worker harness inside the pytest CI lane.

The relay is a Cloudflare Worker (JavaScript), but its contract — opaque
spec/blob, hashed tokens, dual size cap, per-session rate limit, TTL, and
zero-relay-change-for-a-new-kind — is exercised by a pure-Node harness against an
in-memory store. Bridging it through pytest (mirroring how
``tests/test_sound_setup.py`` runs ``active_speaker_ui_test.mjs``) keeps the
relay covered by the existing Python CI matrix with no extra CI wiring.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parent / "js" / "relay_worker_test.mjs"
_NODE = shutil.which("node")


def test_relay_worker_contract():
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True, out
    # The harness counts each top-level test; a silent drop would shrink this.
    assert out["passed"] >= 19, out
