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

import re

import pytest

from jasper.capture_relay import spec as spec_mod
from jasper.capture_relay.spec import (
    CAPTURE_PROTOCOL_VERSION,
    UI_COMPONENT_TYPES,
    BUILDERS,
    CaptureSpec,
    CaptureSpecError,
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

    # Crossover is magnitude FR: drift-insensitive, with calibrated per-flow
    # alignment gates already enforced by its analysis.
    xover = build_crossover_sweep_spec(driver_label="tweeter")
    assert xover.validity.clock_drift == "ignore"
    assert xover.validity.require_alignment is True

    # Level ramp is a pure level comparison (no WAV to align): AGC would flatten
    # the very level it maps (refuse), but it is not an alignment measurement and
    # drift is irrelevant. The Pi's RampController is the stop; duration is a
    # generous hard timeout.
    from jasper.capture_relay.spec import build_level_ramp_spec

    ramp = build_level_ramp_spec(geometry_label="speaker baffle")
    assert ramp.capture_protocol_version == CAPTURE_PROTOCOL_VERSION
    assert ramp.validity.clean_capture == "refuse"
    assert ramp.validity.allow_capability_fallback is True
    assert ramp.validity.require_alignment is False
    assert ramp.validity.clock_drift == "ignore"
    headings = [c for c in ramp.screen if c["type"] == "heading"]
    assert headings and "speaker baffle" in headings[0]["text"]
    assert [c for c in ramp.screen if c["type"] == "button"][0]["label"] == (
        "Start level check"
    )


def test_level_ramp_preserves_exact_pi_owned_placement_instruction():
    from jasper.active_speaker.capture_geometry import driver_placement_instruction
    from jasper.capture_relay.spec import build_level_ramp_spec

    placement = driver_placement_instruction("woofer")
    ramp = build_level_ramp_spec(
        geometry_label="speaker baffle measurement position",
        placement_instruction=placement,
    )

    steps = [c for c in ramp.screen if c["type"] == "steps"]
    assert steps[0]["items"][0] == placement
    assert "3 cm" in steps[0]["items"][0]
    assert "woofer cone" in steps[0]["items"][0]
    assert ramp.stimulus is not None
    assert ramp.stimulus.label == "1000 Hz level-match tone"


def test_crossover_level_reference_is_driver_specific_and_passband_safe():
    from jasper.active_speaker.capture_geometry import crossover_level_reference
    from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

    two_way = [
        crossover_level_reference(
            _two_way_preset("mono"), speaker_group_id="mono", role=role
        )
        for role in ("woofer", "tweeter")
    ]
    three_way = [
        crossover_level_reference(
            _three_way_preset("mono"), speaker_group_id="mono", role=role
        )
        for role in ("woofer", "mid", "tweeter")
    ]

    assert [(item.role, item.tone_frequency_hz) for item in two_way] == [
        ("woofer", 250.0),
        ("tweeter", 6250.0),
    ]
    assert [(item.role, item.tone_frequency_hz) for item in three_way] == [
        ("woofer", 120.0),
        ("mid", 935.4),
        ("tweeter", 6250.0),
    ]
    assert all(item.geometry.startswith("near_field_driver:mono:") for item in two_way)


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

    assert spec.capture_protocol_version == CAPTURE_PROTOCOL_VERSION
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


def test_crossover_driver_fixed_axis_uses_distinct_server_policy_and_copy():
    spec = build_crossover_sweep_spec(
        driver_label="woofer",
        driver_role="woofer",
        driver_capture_geometry="reference_axis",
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
    )

    assert spec.acknowledgement is not None
    assert spec.acknowledgement.id == "driver_reference_axis_v1"
    assert "tweeter axis" in spec.acknowledgement.label
    assert "will not move" in spec.acknowledgement.label
    steps = next(item for item in spec.screen if item["type"] == "steps")
    assert "about 1 metre away" in steps["items"][0]
    assert "measuring the woofer and every other driver" in steps["items"][0]
    button = next(item for item in spec.screen if item["type"] == "button")
    assert "fixed on-axis" in button["label"]


def test_crossover_driver_rejects_unknown_capture_geometry():
    with pytest.raises(spec_mod.CaptureSpecError, match="capture geometry"):
        build_crossover_sweep_spec(
            driver_role="woofer",
            driver_capture_geometry="browser_invented",
        )


def test_crossover_summed_capture_binds_fixed_reference_axis():
    """The STATIONARY shape — still the default, and still honest.

    ``guided_captures=0`` is what every pre-cloud caller emits, including the
    1-entry re-verify re-arm, whose "I will not move it" promise really is true
    for its single-capture session. This copy and policy id must stay reachable
    and unchanged; the guided-cloud variant is pinned separately below.
    """
    spec = build_crossover_sweep_spec(
        driver_label="summed crossover",
        driver_role="summed",
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
    )

    assert spec.acknowledgement is not None
    assert spec.acknowledgement.id == "summed_reference_axis_v1"
    assert "tweeter axis" in spec.acknowledgement.label
    assert "level with the centre" in spec.acknowledgement.label
    assert "will not move" in spec.acknowledgement.label
    steps = next(item for item in spec.screen if item["type"] == "steps")
    assert "tweeter axis" in steps["items"][0]
    assert "level with the centre" in steps["items"][0]
    assert "completely still" in steps["items"][0]
    # The ACTOR is the microphone, not the phone (owner ruling, 2026-07-29 on
    # issue #1806): households measure with a phone, a laptop, or a calibrated
    # USB mic, and the device is incidental to the instruction.
    assert steps["items"][2] == "Keep the microphone still until the sweep finishes"
    button = next(item for item in spec.screen if item["type"] == "button")
    assert "fixed on-axis" in button["label"]


def test_crossover_guided_cloud_never_promises_a_stationary_mic():
    """The GUIDED shape — the consent surface a spatial cloud may honestly show.

    Round-1 review blocker B1: the 16-entry cloud shipped carrying this
    builder's stationary copy, so the first screens a household read promised
    "I will not move it" for a session whose whole design is prompted mic
    moves. Consent to the stationary policy is not consent to a walk, so the
    guided shape gets its own policy id as well as its own words.

    What must SURVIVE the change is the per-sweep stillness promise — that one
    is still true, and it is the one an individual capture actually depends on.
    """
    spec = build_crossover_sweep_spec(
        driver_label="crossover",
        driver_role="summed",
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
        guided_captures=16,
        # A walk must say which of its captures announce themselves — see
        # ``_courtesy_beeps_step``. This shape is stage 1's: first and last.
        announced_captures=(1, 16),
    )

    assert spec.acknowledgement is not None
    assert spec.acknowledgement.id == "summed_guided_cloud_v1"
    label = spec.acknowledgement.label
    # Same starting axis, opposite movement promise.
    assert "tweeter axis" in label
    assert "level with the centre" in label
    assert "will not move" not in label
    assert "move it only when I am asked to" in label
    # …and the promise names no DEVICE, for the same reason the steps do not
    # ("microphone" is the actor; "phone" as a standalone word is the device).
    assert not re.search(r"\bphone\b", label)
    assert "16" in label

    steps = next(item for item in spec.screen if item["type"] == "steps")
    items = steps["items"]
    # #1941 R1: the orientation is at most six items, and each promise below is
    # pinned by CONTENT rather than by index — the reading ORDER is the thing
    # that requirement is free to restructure, the promises are not.
    assert len(items) <= 6
    orientation = "\n".join(items)

    # WHERE TO STAND. RE-DERIVED for the two-stage split (work order D7): "come
    # back to the mark" was stage 2's business — the post-apply anchor — so
    # this consent screen stopped promising a return the session it consents to
    # never makes.
    assert "that spot is your mark" in orientation
    assert "come back to the mark" not in orientation
    assert "completely still" not in orientation
    # …and the mark step says ONLY where to stand (#1941 R1): the capture count
    # belongs to the derived tier line, and repeating it one line below was
    # half of the density the owner reported.
    mark_step = next(i for i in items if "that spot is your mark" in i)
    assert "measurements" not in mark_step

    # WHAT YOU NEED, before the session (#1941 R2) — and the fact that
    # motivates it: every position is measured from the mark and named with a
    # distance, which is WHY a tape measure is worth fetching.
    assert "every position is named with a distance from the mark" in orientation
    bring_step = next(i for i in items if i.startswith("Bring a tape measure"))
    # The stand is a RECOMMENDATION (owner ruling, #1941 Q3), stated with its
    # physical reason — never as a requirement the flow cannot enforce.
    assert "stand or tripod for the microphone is worth using" in bring_step
    assert "change what it hears" in bring_step
    for demand in ("you must", "required", "do not proceed"):
        assert demand not in bring_step.lower()
    # …and the acknowledgement still promises only what the cloud needs, so a
    # hand-held mic is not consenting to something it cannot deliver. Word
    # boundaries so a future "understand"/"standing" cannot false-trip this.
    assert not re.search(r"\b(tripod|stands?)\b", label)

    # WHAT YOU WILL HEAR, and both hedges in it are load-bearing (#1979). "has"
    # rather than "is", because CHECK opens on a 12 s room-noise window BEFORE
    # the beeps, so an exhaustive description is false for the first capture of
    # the session; "tones" rather than "a tone", because no phase plays exactly
    # one (CHECK: four pilot chirps and no sweep; MEASURE: two pilots then six
    # sweeps; a prompted position: two pilots then one sweep).
    #
    # A third hedge since 2026-08-18: the beeps are quantified over the
    # captures that ACTUALLY announce and the tones over every one, because the
    # courtesy prelude now announces a session rather than a capture
    # (``crossover_v2.programs.courtesy_prelude_for_phase``). This spec is
    # built here with ``announced_captures=(1, 16)`` — stage 1's shape — so the
    # sentence must name both ends. The retired wording ("Each measurement has
    # three short beeps") is pinned OUT: a consent screen promising a warning
    # the speaker will not play is the same defect as one that promises
    # nothing.
    #
    # This pin is a LITERAL one and is therefore only as good as the shape
    # above. What checks the sentence against a real plan — the defect a
    # literal pin is structurally blind to — is
    # ``test_the_consent_beeps_sentence_matches_what_the_session_plays`` in
    # tests/test_crossover_v2_conductor.py, which walks the composed programs.
    assert (
        "The first and last measurements each have three short beeps and a "
        "pause" in orientation
    )
    assert "every measurement has rising tones" in orientation
    assert "Each measurement has three short beeps" not in orientation

    # Per-sweep stillness, kept verbatim and made explicit about what follows
    # it. This is the promise an individual capture depends on.
    assert (
        "Keep the microphone still until each sweep finishes, then follow the "
        "on-screen prompt to move it"
    ) in items
    button = next(item for item in spec.screen if item["type"] == "button")
    assert button["label"] == "The mic is on the mark — start measuring"
    assert "fixed on-axis" not in button["label"]


def test_guided_captures_is_a_summed_only_shape():
    with pytest.raises(CaptureSpecError):
        build_crossover_sweep_spec(
            driver_label="Woofer driver",
            driver_role="woofer",
            acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
            guided_captures=16,
        )
    with pytest.raises(CaptureSpecError):
        build_crossover_sweep_spec(
            driver_label="crossover",
            driver_role="summed",
            acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
            guided_captures=-1,
        )


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


def test_crossover_deadline_and_copy_include_stored_ambient_window():
    spec = build_crossover_sweep_spec(
        stimulus_duration_ms=12000,
        ambient_duration_ms=12000,
        hard_timeout_ms=0,
    )

    assert spec.duration_ms == (
        spec.pre_roll_ms + 12000 + 12000 + spec.post_roll_ms
    )
    steps = next(item for item in spec.screen if item["type"] == "steps")
    assert "measures the room noise" in steps["items"][1]


def test_crossover_sweep_and_level_ramp_both_offer_a_stop_button():
    # Fix 3 (2026-07-16): the phone had no user-tappable Stop during a driver
    # sweep or a level ramp. Both server-driven screens now declare a "stop"
    # button alongside "begin_capture" — the client wires it to the SAME
    # abort() path the visibility-abort case already exercises.
    from jasper.capture_relay.spec import build_level_ramp_spec

    xover = build_crossover_sweep_spec()
    ramp = build_level_ramp_spec()
    for spec in (xover, ramp):
        buttons = [c for c in spec.screen if c["type"] == "button"]
        actions = [b["action"] for b in buttons]
        assert "begin_capture" in actions
        assert "stop" in actions
        stop_button = next(b for b in buttons if b["action"] == "stop")
        assert stop_button["label"] == "Stop"
        # Round-trips through the wire contract like every other button.
        round_tripped = CaptureSpec.from_dict(spec.to_dict())
        round_tripped_actions = [
            c["action"] for c in round_tripped.screen if c["type"] == "button"
        ]
        assert "stop" in round_tripped_actions


def test_legal_45_second_pcm16_capture_fits_single_crossover_size_contract():
    from jasper.active_speaker.test_signal_plan import (
        CROSSOVER_CAPTURE_HARD_TIMEOUT_S,
        CROSSOVER_CAPTURE_MAX_WAV_BYTES,
    )
    from jasper.active_speaker import bundles, web_measurement
    from jasper.web import correction_setup

    legal_pcm16_bytes = 44 + int(48000 * 2 * CROSSOVER_CAPTURE_HARD_TIMEOUT_S)
    assert legal_pcm16_bytes < CROSSOVER_CAPTURE_MAX_WAV_BYTES
    assert web_measurement.MAX_CAPTURE_WAV_BYTES == CROSSOVER_CAPTURE_MAX_WAV_BYTES
    assert bundles.MAX_CAPTURE_WAV_BYTES == CROSSOVER_CAPTURE_MAX_WAV_BYTES
    assert correction_setup.MAX_CROSSOVER_WAV_BODY_BYTES == (
        CROSSOVER_CAPTURE_MAX_WAV_BYTES
    )
    spec = build_crossover_sweep_spec(
        hard_timeout_ms=int(CROSSOVER_CAPTURE_HARD_TIMEOUT_S * 1000),
        max_upload_bytes=CROSSOVER_CAPTURE_MAX_WAV_BYTES,
    )
    assert spec.max_upload_bytes == CROSSOVER_CAPTURE_MAX_WAV_BYTES


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
