# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language drift guard for the USB "playing" RMS gate.

A host is treated as "playing" only when its audio level is above a shared
threshold — so a Mac streaming digital silence (a muted Zoom, an idle tab) does
NOT seize the speaker. That threshold lives in TWO languages because two code
paths apply it:

  1. the solo bridge    rust/jasper-usbsink-audio/src/main.rs  PLAYING_RMS_DBFS
     — gates the bridge's own per-period ``playing`` flag.
  2. the combo path     jasper/source_state.py  USBSINK_PLAYING_RMS_DBFS
     — gates mux's combo-liveness (frames-advanced AND audible) and the
       /state renderer level, so a combo box behaves like a solo box.

They can't share code across the Rust/Python boundary, so this test pins the two
constants equal. If a future change moves one and not the other, combo and solo
boxes would disagree about what counts as "playing" — this fails first. Mirrors
tests/test_wifi_profile_hardening_contract.py. See AGENTS.md.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.source_state import USBSINK_PLAYING_RMS_DBFS

ROOT = Path(__file__).resolve().parents[1]
SOLO_BRIDGE_MAIN = ROOT / "rust" / "jasper-usbsink-audio" / "src" / "main.rs"

# `const PLAYING_RMS_DBFS: f64 = -60.0;`
_RUST_CONST_RE = re.compile(
    r"const\s+PLAYING_RMS_DBFS\s*:\s*f64\s*=\s*(-?\d+(?:\.\d+)?)\s*;"
)


def _rust_playing_rms_dbfs() -> float:
    text = SOLO_BRIDGE_MAIN.read_text()
    match = _RUST_CONST_RE.search(text)
    assert match is not None, (
        "PLAYING_RMS_DBFS not found in the solo bridge — the drift guard can no "
        "longer locate the Rust source of truth (was it renamed?)."
    )
    return float(match.group(1))


def test_python_and_rust_playing_rms_thresholds_match():
    assert USBSINK_PLAYING_RMS_DBFS == _rust_playing_rms_dbfs(), (
        "The solo bridge (Rust PLAYING_RMS_DBFS) and the combo path (Python "
        "USBSINK_PLAYING_RMS_DBFS) must agree, or a combo box and a solo box "
        "disagree about what counts as USB 'playing'. Change both."
    )
