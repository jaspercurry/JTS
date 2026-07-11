# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Generalization to more measurement kinds (phone-mic relay step 8).

The headline architectural claim (plan §6, §15): adding a measurement kind is a
**page + Pi** change with **zero relay change**. This suite proves the boundary
from the Pi side — every shipped kind builds a valid spec, carries its own
per-kind validity policy as DATA, and uses ONLY the closed UI component
vocabulary (so the page renderer needs no new component either). The relay's own
opacity is proven separately in tests/js/relay_worker_test.mjs.
"""
from __future__ import annotations

import pytest

from jasper.capture_relay import spec as spec_mod
from jasper.capture_relay.spec import (
    UI_COMPONENT_TYPES,
    BUILDERS,
    CaptureSpec,
    SHIPPED_KINDS,
    build_balance_burst_spec,
    build_crossover_sweep_spec,
    build_sync_marker_spec,
)


@pytest.mark.parametrize("kind", SHIPPED_KINDS)
def test_every_shipped_kind_builds_a_valid_spec(kind):
    s = BUILDERS[kind]()
    assert s.kind == kind
    s.validate()  # raises on any drift
    # Round-trips through the opaque-JSON form the relay stores.
    assert CaptureSpec.from_dict(s.to_dict()).kind == kind


@pytest.mark.parametrize("kind", SHIPPED_KINDS)
def test_window_contains_stimulus(kind):
    s = BUILDERS[kind]()
    assert s.duration_ms == s.pre_roll_ms + s.post_roll_ms + (
        s.duration_ms - s.pre_roll_ms - s.post_roll_ms
    )
    assert s.duration_ms > s.pre_roll_ms + s.post_roll_ms  # room for the stimulus


@pytest.mark.parametrize("kind", SHIPPED_KINDS)
def test_kinds_reuse_closed_component_vocabulary(kind):
    # Zero page-renderer change: every component a kind emits is already drawable.
    s = BUILDERS[kind]()
    for component in s.screen:
        assert component["type"] in UI_COMPONENT_TYPES
    # And the button action stays in the allowlist.
    buttons = [c for c in s.screen if c["type"] == "button"]
    assert buttons and buttons[0]["action"] in spec_mod.UI_BUTTON_ACTIONS


def test_per_kind_validity_policy_is_the_differentiation():
    # Sync timing must stay within one recording so clock drift cancels (§9).
    sync = build_sync_marker_spec()
    assert sync.validity.clock_drift == "single_window"
    assert sync.validity.require_alignment is True

    # Balance is a level comparison: AGC would flatten it (refuse), but it is not
    # an alignment measurement.
    balance = build_balance_burst_spec()
    assert balance.validity.clean_capture == "refuse"
    assert balance.validity.require_alignment is False
    assert balance.validity.clock_drift == "ignore"

    # Crossover is magnitude FR like room: drift-insensitive, alignment matters.
    xover = build_crossover_sweep_spec(driver_label="tweeter")
    assert xover.validity.clock_drift == "ignore"
    assert xover.validity.require_alignment is True

    # Level ramp is a pure level comparison (no WAV to align): AGC would flatten
    # the very level it maps (refuse), but it is not an alignment measurement and
    # drift is irrelevant. The Pi's RampController is the stop; duration is a
    # generous hard timeout.
    from jasper.capture_relay.spec import build_level_ramp_spec

    ramp = build_level_ramp_spec(geometry_label="speaker baffle")
    assert ramp.validity.clean_capture == "refuse"
    assert ramp.validity.allow_capability_fallback is True
    assert ramp.validity.require_alignment is False
    assert ramp.validity.clock_drift == "ignore"
    headings = [c for c in ramp.screen if c["type"] == "heading"]
    assert headings and "speaker baffle" in headings[0]["text"]


def test_server_driven_copy_names_the_driver():
    # The crossover UI copy comes from the Pi (no web deploy to relabel a driver).
    s = build_crossover_sweep_spec(driver_label="woofer")
    headings = [c for c in s.screen if c["type"] == "heading"]
    assert headings and "woofer" in headings[0]["text"]


def test_crossover_driver_requires_explicit_bound_placement_acknowledgement():
    binding = "placement_abcdefghijklmnopqrstuv"
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=binding,
    )

    assert spec.capture_protocol_version == 2
    assert spec.acknowledgement is not None
    assert spec.acknowledgement.id == "driver_same_distance_v1"
    assert spec.acknowledgement.binding_id == binding
    assert "3 cm" in spec.acknowledgement.label
    assert "woofer" in spec.acknowledgement.label
    steps = next(item for item in spec.screen if item["type"] == "steps")
    assert "3 cm" in steps["items"][0]
    assert "same distance" in steps["items"][0]
    button = next(item for item in spec.screen if item["type"] == "button")
    assert "positioned" in button["label"]
    round_tripped = CaptureSpec.from_dict(spec.to_dict())
    assert round_tripped.acknowledgement == spec.acknowledgement


def test_crossover_summed_capture_has_distinct_position_acknowledgement():
    spec = build_crossover_sweep_spec(
        driver_label="summed crossover",
        driver_role="summed",
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
    )

    assert spec.acknowledgement is not None
    assert spec.acknowledgement.id == "summed_listening_position_v1"
    assert "listening position" in spec.acknowledgement.label
    steps = next(item for item in spec.screen if item["type"] == "steps")
    assert "listening position" in steps["items"][0]


def test_crossover_sweep_stimulus_single_sourced_from_the_kernel():
    # CRITICAL CORRECTNESS (P7): the crossover_sweep spec must advertise the SAME
    # sweep length the active-crossover flow actually plays — there must be ONE
    # sweep definition, not a forked second constant. The Pi plays / deconvolves
    # from driver_acoustics.DEFAULT_DURATION_S; the phone's copy is sized from
    # the spec's stimulus_duration_ms, so a mismatch would mislabel the phone's
    # "stay quiet for N seconds" step.
    from jasper.active_speaker.driver_acoustics import DEFAULT_DURATION_S

    s = build_crossover_sweep_spec()
    seconds = round(DEFAULT_DURATION_S)
    steps = [c for c in s.screen if c["type"] == "steps"]
    assert steps and any(f"{seconds} seconds" in item for item in steps[0]["items"])


def test_crossover_and_sync_recording_deadlines_are_floored():
    # duration_ms is the phone's HARD recording deadline and its clock starts at
    # `armed` — the Pi's whole round trip (armed-poll, config load, WAV gen,
    # playback, fan-in release, rollback, relay posts) must fit inside it. A
    # bare pre+stimulus+post window (7.5 s crossover / 3.4 s sync) left ~1.5 s
    # for everything but the audio and structurally timed out every capture; the
    # room kind's hard_timeout_ms floor (30 s) is the shared contract — the
    # normal stop is the Pi's sweep_complete event, the deadline is only the
    # backstop.
    from jasper.capture_relay.spec import build_sync_marker_spec

    xover = build_crossover_sweep_spec()
    assert xover.duration_ms >= 30000
    sync = build_sync_marker_spec()
    assert sync.duration_ms >= 30000
    # An acoustic window LONGER than the floor still wins (never truncate).
    long = build_crossover_sweep_spec(stimulus_duration_ms=40000)
    assert long.duration_ms == long.pre_roll_ms + 40000 + long.post_roll_ms


def test_level_ramp_run_token_rides_the_spec():
    # The per-run nonce is an ADDITIVE spec field (schema pin): it round-trips
    # through to_dict/from_dict, defaults empty for every other kind, and is
    # validated to a bounded URL-safe shape.
    from jasper.capture_relay.spec import (
        CaptureSpecError,
        build_level_ramp_spec,
        build_room_sweep_spec,
    )

    ramp = build_level_ramp_spec(run_token="run_ab12-CD")
    assert ramp.run_token == "run_ab12-CD"
    assert ramp.setup_validation is True
    assert ramp.setup_binding_id == "level-run_ab12-CD"
    round_tripped = CaptureSpec.from_dict(ramp.to_dict())
    assert round_tripped.run_token == "run_ab12-CD"
    assert build_room_sweep_spec().run_token == ""
    with pytest.raises(CaptureSpecError, match="run_token"):
        build_level_ramp_spec(run_token="bad token!")
    with pytest.raises(CaptureSpecError, match="run_token"):
        build_level_ramp_spec(run_token="x" * 65)


def test_level_ramp_phone_timeout_exceeds_pi_safety_timeout():
    # The phone-side hard recording timeout must stay ABOVE the Pi's derived
    # safety timeout so the Pi's stop is always the real one (the review: the
    # old 45 s spec timeout raced a ramp whose own worst case exceeded it).
    from jasper.audio_measurement.ramp import MeasurementRamp
    from jasper.capture_relay.spec import build_level_ramp_spec

    ramp = build_level_ramp_spec()
    assert ramp.duration_ms / 1000.0 > MeasurementRamp().safety_timeout + 5.0


def test_builders_registry_is_complete():
    assert set(BUILDERS) == set(SHIPPED_KINDS)
    assert all(callable(b) for b in BUILDERS.values())


def test_adding_a_kind_touched_neither_relay_nor_validator():
    # The validator never enumerates kinds (so the schema/relay are kind-blind):
    import inspect

    source = inspect.getsource(CaptureSpec.validate)
    for kind in SHIPPED_KINDS:
        assert kind not in source
