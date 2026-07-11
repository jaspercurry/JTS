# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language drift guard for the USB "playing" RMS gate.

A host is treated as "playing" only when its audio level is above a shared
threshold — so a Mac streaming digital silence (a muted Zoom, an idle tab) does
NOT seize the speaker. That threshold lives in TWO languages:

  1. the combo path     jasper/source_state.py  USBSINK_PLAYING_RMS_DBFS
     — applied to fan-in's DIRECT-capture lane (the live USB path); gates mux's
       combo-liveness (frames-advanced AND audible) and the /state renderer level.
  2. the Rust anchor    rust/jasper-usbsink-audio/src/main.rs  PLAYING_RMS_DBFS
     — the standby-only bridge no longer computes ``playing`` (fan-in does), but
       it retains this constant as the Rust-side anchor so the two definitions
       cannot silently drift apart.

They can't share code across the Rust/Python boundary, so this test pins the two
constants equal. If a future change re-derives the gate in Rust and drifts from
Python (or vice versa), this fails first. Mirrors
tests/test_wifi_profile_hardening_contract.py. See AGENTS.md.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.source_state import USBSINK_PLAYING_RMS_DBFS

ROOT = Path(__file__).resolve().parents[1]
USBSINK_BRIDGE_MAIN = ROOT / "rust" / "jasper-usbsink-audio" / "src" / "main.rs"

# `const PLAYING_RMS_DBFS: f64 = -60.0;`
_RUST_CONST_RE = re.compile(
    r"const\s+PLAYING_RMS_DBFS\s*:\s*f64\s*=\s*(-?\d+(?:\.\d+)?)\s*;"
)


def _rust_playing_rms_dbfs() -> float:
    text = USBSINK_BRIDGE_MAIN.read_text()
    match = _RUST_CONST_RE.search(text)
    assert match is not None, (
        "PLAYING_RMS_DBFS not found in the usbsink bridge — the drift guard can no "
        "longer locate the Rust anchor (was it renamed or removed?)."
    )
    return float(match.group(1))


def test_python_and_rust_playing_rms_thresholds_match():
    assert USBSINK_PLAYING_RMS_DBFS == _rust_playing_rms_dbfs(), (
        "The Rust anchor (PLAYING_RMS_DBFS) and the combo path (Python "
        "USBSINK_PLAYING_RMS_DBFS) must agree, or the two definitions of what "
        "counts as USB 'playing' silently drift apart. Change both."
    )
