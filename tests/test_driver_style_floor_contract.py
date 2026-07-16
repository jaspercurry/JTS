# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard for the tweeter driver-style -> high-pass-floor contract.

The authoritative style -> protective high-pass floor table is Python's
``_STYLE_HIGH_PASS_HZ`` in jasper/active_speaker/driver_protection.py. The
/sound/ page JS (deploy/assets/sound-profile/js/main.js, ``hfDriverStyles()``)
carries a display copy so the layout-card picker and the review-card hint can
name the floor a declared style buys. They can't share code (one Python, one
static browser module), so this test pins the pairs: if a floor changes or a
style is added on one side and not the other, this fails.

Python is allowed to be a superset (e.g. ``horn_compression_driver`` shares
the compression-driver floor and is deliberately not a separate picker
option); every JS entry must match Python exactly. The JS "conservative
N Hz" copy for an undeclared style must also match Python's unknown-style
default. Same pattern as tests/test_wifi_profile_hardening_contract.py.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.active_speaker.driver_protection import (
    _STYLE_HIGH_PASS_HZ,
    _UNKNOWN_HF_STYLE,
)

ROOT = Path(__file__).resolve().parents[1]
SOUND_JS = ROOT / "deploy" / "assets" / "sound-profile" / "js" / "main.js"


def _js_style_table() -> dict[str, float]:
    """Extract the (value, floor_hz) pairs from hfDriverStyles() in main.js."""
    text = SOUND_JS.read_text(encoding="utf-8")
    m = re.search(
        r"function hfDriverStyles\(\) \{\s*return \[(?P<body>.*?)\];",
        text,
        flags=re.S,
    )
    assert m is not None, "could not find hfDriverStyles() in main.js"
    entries = re.findall(
        r"\{value: '(?P<value>[a-z0-9_]+)', label: '[^']*', "
        r"floor_hz: (?P<floor>\d+(?:\.\d+)?)\}",
        m.group("body"),
    )
    assert entries, "hfDriverStyles() entries did not match the expected shape"
    return {value: float(floor) for value, floor in entries}


def test_js_style_floor_table_matches_python_policy() -> None:
    js_table = _js_style_table()
    assert len(js_table) >= 5, js_table
    for style, floor_hz in js_table.items():
        assert style in _STYLE_HIGH_PASS_HZ, (
            f"JS picker offers driver style {style!r} that Python's "
            "_STYLE_HIGH_PASS_HZ does not know"
        )
        assert floor_hz == _STYLE_HIGH_PASS_HZ[style], (
            f"JS floor for {style!r} is {floor_hz}, Python policy says "
            f"{_STYLE_HIGH_PASS_HZ[style]}"
        )


def test_js_conservative_default_copy_matches_python_unknown_floor() -> None:
    """Every 'conservative N Hz' string in the page copy must state Python's
    unknown-style floor -- the number an undeclared tweeter actually gets."""
    text = SOUND_JS.read_text(encoding="utf-8")
    matches = re.findall(r"conservative (\d+(?:\.\d+)?) Hz", text)
    assert matches, "expected the undeclared-style copy to name the floor"
    unknown_floor = _STYLE_HIGH_PASS_HZ[_UNKNOWN_HF_STYLE]
    for value in matches:
        assert float(value) == unknown_floor, (
            f"page copy says 'conservative {value} Hz' but Python's "
            f"unknown-style floor is {unknown_floor}"
        )
