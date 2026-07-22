# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``bass_nearfield`` capture-spec builder (bass-extension bench).

Mirrors the ``crossover_sweep`` builder conventions in
``tests/test_capture_relay_kinds.py`` / ``tests/test_capture_relay_spec.py``:
measurement-clean (48 kHz / mono / EC-off / WAV), registry round-trip, stable
``to_dict``/``from_dict``, and — the point of this kind — a **server-derived**
near-field geometry that no operator/browser request can supply.
"""
from __future__ import annotations

import inspect

from jasper.active_speaker.capture_geometry import driver_placement_instruction
from jasper.capture_relay.spec import (
    BUILDERS,
    SHIPPED_KINDS,
    CaptureConstraints,
    CaptureSpec,
    build_bass_nearfield_spec,
)


def test_bass_nearfield_is_measurement_clean_48k_mono():
    s = build_bass_nearfield_spec()
    assert s.kind == "bass_nearfield"
    assert s.sample_rate_hz == 48000
    assert s.channels == 1
    assert s.output_format == "wav"
    # EC/AGC/NS/voice-isolation all off — anything on flattens the response.
    assert s.constraints == CaptureConstraints(False, False, False, False)
    d = s.to_dict()
    assert d["constraints"] == {
        "echoCancellation": False,
        "autoGainControl": False,
        "noiseSuppression": False,
        "voiceIsolation": False,
    }
    assert d["output"] == {"format": "wav"}


def test_bass_nearfield_registered_in_shipped_kinds_and_builders():
    assert "bass_nearfield" in SHIPPED_KINDS
    assert BUILDERS["bass_nearfield"] is build_bass_nearfield_spec
    # The kind resolves through the registry to a valid, opaque-JSON round trip.
    s = BUILDERS["bass_nearfield"]()
    assert s.kind == "bass_nearfield"
    assert CaptureSpec.from_dict(s.to_dict()).kind == "bass_nearfield"


def test_bass_nearfield_to_dict_from_dict_round_trip_is_stable():
    s = build_bass_nearfield_spec()
    again = CaptureSpec.from_dict(s.to_dict())
    assert again.to_dict() == s.to_dict()


def test_bass_nearfield_geometry_is_server_derived_never_request_supplied():
    # There is no geometry knob a browser/operator request could set — the
    # near-field geometry is baked in, the way the crossover builder treats
    # capture geometry as speaker policy.
    params = inspect.signature(build_bass_nearfield_spec).parameters
    assert "driver_capture_geometry" not in params
    assert not any("geometry" in name for name in params)
    # The placement copy is the single-sourced near-field woofer instruction.
    s = build_bass_nearfield_spec()
    steps = next(c for c in s.screen if c["type"] == "steps")
    assert steps["items"][0] == driver_placement_instruction("woofer")
    assert "3 cm" in steps["items"][0]
    assert "woofer cone" in steps["items"][0]


def test_bass_nearfield_validity_refuses_unclean_and_is_drift_insensitive():
    # Magnitude FR like room/crossover: drift-insensitive, alignment matters,
    # auto-gain would flatten the very response being measured.
    v = build_bass_nearfield_spec().validity
    assert v.clean_capture == "refuse"
    assert v.allow_capability_fallback is True
    assert v.require_alignment is True
    assert v.clock_drift == "ignore"


def test_bass_nearfield_window_is_floored_and_offers_stop():
    s = build_bass_nearfield_spec(hard_timeout_ms=30000, stimulus_duration_ms=5000)
    # The acoustic window is floored by the hard timeout (the Pi's
    # sweep_complete relay event is the real stop).
    assert s.duration_ms == 30000
    assert s.stimulus is not None and s.stimulus.played_by == "pi"
    actions = [c["action"] for c in s.screen if c["type"] == "button"]
    assert actions[0] == "begin_capture"
    assert "stop" in actions
