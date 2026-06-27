# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.lean_lane.decide_lean_route — the Stage 4b pure routing
decision (default-OFF; nothing wires it yet)."""
from __future__ import annotations

import pytest

from jasper.lean_lane import decide_lean_route, lean_lane_enabled
from jasper.music_sources import Source

USB = Source.USBSINK
AP = Source.AIRPLAY


def d(active, winner, enabled=True):
    return decide_lean_route(
        active_sources=tuple(active), winner=winner, lean_enabled=enabled,
    )


# --- the one true lean case ---
def test_usb_sole_active_and_winner_is_lean():
    r = d([USB], USB)
    assert (r.route, r.reason) == ("lean", "usb_exclusive")


# --- list path: callers pass Mux._active_sources(current), which is a LIST.
# A list never equals the (Source.USBSINK,) tuple, so without the defensive
# tuple() normalization this would fail closed to "buffered". ---
def test_list_active_sources_usb_exclusive_is_lean():
    r = decide_lean_route(
        active_sources=[USB], winner=USB, lean_enabled=True,
    )
    assert (r.route, r.reason) == ("lean", "usb_exclusive")


# --- flag gating (default-OFF contract) ---
def test_flag_off_forces_buffered_even_when_usb_exclusive():
    r = d([USB], USB, enabled=False)
    assert (r.route, r.reason) == ("buffered", "flag_off")


# --- exclusivity ---
def test_usb_plus_airplay_mixing_is_buffered():
    assert d([USB, AP], USB).route == "buffered"
    assert d([USB, AP], USB).reason == "not_exclusive"


def test_non_usb_sole_source_is_buffered():
    assert d([AP], AP).route == "buffered"
    assert d([AP], AP).reason == "not_exclusive"


# --- winner disagreement (USB playing but not yet the audible winner) ---
def test_usb_exclusive_but_winner_not_usb_is_buffered():
    r = d([USB], None)
    assert (r.route, r.reason) == ("buffered", "non_usb_winner")


# --- idle ---
def test_no_active_sources_is_buffered():
    assert d([], None).reason == "idle"


# --- flag parser polarity (opt-IN) ---
@pytest.mark.parametrize(
    "val,expected",
    [
        ("enabled", True),
        ("ENABLED", True),
        (" enabled ", True),
        ("", False),
        ("disabled", False),
        ("1", False),
        ("true", False),
    ],
)
def test_lean_lane_enabled_only_on_exact_enabled(monkeypatch, val, expected):
    monkeypatch.setenv("JASPER_LEAN_LANE", val)
    assert lean_lane_enabled() is expected


def test_lean_lane_enabled_default_off(monkeypatch):
    monkeypatch.delenv("JASPER_LEAN_LANE", raising=False)
    assert lean_lane_enabled() is False
