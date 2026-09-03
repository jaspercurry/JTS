# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.active_speaker import (
    DRIVER_TEST_SIGNAL_PLAN_KIND,
    ActiveSpeakerPreset,
    driver_test_signal_plan,
    driver_test_signal_plan_from_edges,
)
from jasper.active_speaker.test_signal_plan import driver_sweep_duration_s


def _preset(
    *,
    fc_hz: float = 1600,
    tweeter_protection_floor_hz: float | None = None,
) -> ActiveSpeakerPreset:
    tweeter: dict = {"manufacturer": "Example", "model": "Tweeter"}
    if tweeter_protection_floor_hz is not None:
        tweeter["protection_highpass_floor_hz"] = tweeter_protection_floor_hz
    return ActiveSpeakerPreset.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "tone-plan-test-v1",
        "name": "Tone plan test preset",
        "way_count": 2,
        "channel_map": {
            "layout": "mono",
            "outputs": [
                {
                    "index": 0,
                    "side": "mono",
                    "driver_role": "woofer",
                    "label": "mono woofer",
                    "startup_muted": True,
                },
                {
                    "index": 1,
                    "side": "mono",
                    "driver_role": "tweeter",
                    "label": "mono tweeter",
                    "startup_muted": True,
                },
            ],
        },
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "tweeter": tweeter,
        },
        "crossover_regions": [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": fc_hz,
            "target_type": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "delay_range_ms": [0.0, 0.5],
            "null_depth_threshold_db": 25,
        }],
        "safety": {
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    })


def _three_way_preset(
    *,
    woofer_mid_hz: float = 300,
    mid_tweeter_hz: float = 3000,
) -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "tone-plan-3way-v1",
        "name": "Tone plan 3-way test preset",
        "way_count": 3,
        "channel_map": {
            "layout": "mono",
            "outputs": [
                {
                    "index": 0,
                    "side": "mono",
                    "driver_role": "woofer",
                    "label": "mono woofer",
                    "startup_muted": True,
                },
                {
                    "index": 1,
                    "side": "mono",
                    "driver_role": "mid",
                    "label": "mono mid",
                    "startup_muted": True,
                },
                {
                    "index": 2,
                    "side": "mono",
                    "driver_role": "tweeter",
                    "label": "mono tweeter",
                    "startup_muted": True,
                },
            ],
        },
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "mid": {"manufacturer": "Example", "model": "Mid"},
            "tweeter": {"manufacturer": "Example", "model": "Tweeter"},
        },
        "crossover_regions": [
            {
                "id": "woofer_mid",
                "lower_driver": "woofer",
                "upper_driver": "mid",
                "fc_hz": woofer_mid_hz,
                "target_type": "LinkwitzRiley",
                "order": 4,
                "lower_polarity": "non-inverted",
                "upper_polarity": "non-inverted",
                "delay_range_ms": [0.0, 0.5],
                "null_depth_threshold_db": 25,
            },
            {
                "id": "mid_tweeter",
                "lower_driver": "mid",
                "upper_driver": "tweeter",
                "fc_hz": mid_tweeter_hz,
                "target_type": "LinkwitzRiley",
                "order": 4,
                "lower_polarity": "non-inverted",
                "upper_polarity": "non-inverted",
                "delay_range_ms": [0.0, 0.5],
                "null_depth_threshold_db": 25,
            },
        ],
        "safety": {
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    })


def test_driver_test_signal_plan_two_way_uses_crossover_and_protection_edges() -> None:
    preset = _preset(fc_hz=2000)

    woofer = driver_test_signal_plan(preset, "woofer")
    tweeter = driver_test_signal_plan(preset, "tweeter")

    assert woofer["kind"] == DRIVER_TEST_SIGNAL_PLAN_KIND
    assert woofer["status"] == "ready"
    assert woofer["frequency_hz"] == 250.0
    assert woofer["selection_reason"] == "role_native_woofer_below_lowpass_edge"
    assert woofer["frequency_hz"] <= woofer["allowed_band"]["maximum_tone_hz"]
    assert woofer["allowed_band"]["lowpass_hz"] == 2000

    assert tweeter["status"] == "ready"
    assert tweeter["frequency_hz"] > 5000
    assert tweeter["frequency_hz"] != 5000
    assert tweeter["allowed_band"]["highpass_hz"] == 5000
    assert {edge["kind"] for edge in tweeter["allowed_band"]["edges"]} == {
        "crossover_highpass",
        "protective_tweeter_highpass",
        "driver_protection_minimum",
    }


def test_the_protection_edge_follows_the_declared_low_limit_not_the_class_table(
) -> None:
    """#2874, on the edge that sets the staged high-pass.

    ``driver_protection_minimum`` sits inside the ``max`` that picks the tone's
    own band limit, so anchoring it on the class table let a code default raise
    a commissioning tone above the frequency the manufacturer published --
    which would have left the tone gate's fix inert on the one path that
    produces a high-frequency band limit in production.

    A DE250-shaped tweeter (declared 1600 Hz) crossed at 700 Hz: the edge is
    the declared 1600, not the 5000 Hz undeclared-tweeter default, and the
    plan's protection block agrees with it.
    """

    declared = _preset(fc_hz=700, tweeter_protection_floor_hz=1600)
    plan = driver_test_signal_plan(declared, "tweeter")

    edges = {
        edge["kind"]: edge for edge in plan["allowed_band"]["edges"]
    }
    assert edges["driver_protection_minimum"]["frequency_hz"] == 1600.0
    assert edges["driver_protection_minimum"]["source"] == "driver_low_limit:declared"
    assert "manufacturer declared" in edges["driver_protection_minimum"]["reason"]
    assert plan["allowed_band"]["highpass_hz"] == 1600.0
    assert plan["band_limit"]["highpass_hz"] == 1600.0
    assert plan["driver_protection"]["low_limit_hz"] == 1600.0
    assert plan["driver_protection"]["low_limit_provenance"] == "declared"
    assert plan["driver_protection"]["band_limit_highpass_ok"] is True

    # Same crossover, nothing declared: the class default is still the edge,
    # and still labelled as the fallback it is.
    undeclared = driver_test_signal_plan(_preset(fc_hz=700), "tweeter")
    undeclared_edges = {
        edge["kind"]: edge for edge in undeclared["allowed_band"]["edges"]
    }
    assert undeclared_edges["driver_protection_minimum"]["frequency_hz"] == 5000.0
    assert undeclared_edges["driver_protection_minimum"]["source"] == (
        "driver_low_limit:style_default"
    )
    assert undeclared["driver_protection"]["low_limit_provenance"] == "style_default"


def test_driver_test_signal_plan_three_way_places_each_role_in_its_band() -> None:
    preset = _three_way_preset(woofer_mid_hz=300, mid_tweeter_hz=3000)

    woofer = driver_test_signal_plan(preset, "woofer")
    mid = driver_test_signal_plan(preset, "mid")
    tweeter = driver_test_signal_plan(preset, "tweeter")

    assert woofer["status"] == "ready"
    assert woofer["frequency_hz"] == 120.0
    assert woofer["allowed_band"]["lowpass_hz"] == 300
    assert mid["status"] == "ready"
    assert mid["frequency_hz"] == 948.7
    assert mid["allowed_band"]["highpass_hz"] == 300
    assert mid["allowed_band"]["lowpass_hz"] == 3000
    assert tweeter["status"] == "ready"
    assert tweeter["frequency_hz"] > 6000
    assert tweeter["allowed_band"]["highpass_hz"] == 6000


def test_driver_test_signal_plan_subwoofer_stays_above_floor_and_below_lowpass() -> None:
    plan = driver_test_signal_plan_from_edges(
        "subwoofer",
        crossover_lowpass_hz=80,
        crossover_edge_source="future_subwoofer_compiled_edges",
    )

    assert plan["status"] == "ready"
    assert plan["allowed_band"]["highpass_hz"] == 25.0
    assert plan["allowed_band"]["lowpass_hz"] == 80.0
    assert plan["frequency_hz"] == 50.0
    assert plan["selection_reason"] == "role_native_subwoofer_tone"


def test_a_declared_full_range_driver_gets_a_tone_instead_of_a_refusal() -> None:
    """``full_range`` has no class figure standing in for a crossover edge, so
    its floor lives only in the declaration — and reaches BOTH halves of the
    plan, the profile that picks the tone and the envelope that admits it."""
    declared = driver_test_signal_plan_from_edges(
        "full_range",
        declared_low_limit_hz=80.0,
        crossover_edge_source="way1_no_crossover",
    )

    assert declared["status"] == "ready"
    assert declared["driver_protection"]["audio_allowed"] is True
    assert declared["issues"] == []
    assert declared["frequency_hz"] is not None
    # Both halves read the same floor: the profile that picks the tone…
    assert declared["driver_protection"]["floor_test_frequency_hz"] == 80.0

    # …and silence stays the answer for a driver nobody has described, under
    # the name of the missing declaration.
    undeclared = driver_test_signal_plan_from_edges(
        "full_range", crossover_edge_source="way1_no_crossover",
    )

    assert undeclared["status"] == "blocked"
    assert "full_range_low_edge_undeclared" in {
        issue["code"] for issue in undeclared["issues"]
    }


def test_driver_test_signal_plan_blocks_impossibly_narrow_band() -> None:
    plan = driver_test_signal_plan(
        _three_way_preset(woofer_mid_hz=1000, mid_tweeter_hz=1100),
        "mid",
    )

    assert plan["status"] == "blocked"
    assert plan["frequency_hz"] is None
    assert "driver_test_signal_no_safe_band" in {
        issue["code"] for issue in plan["issues"]
    }


def test_driver_sweep_duration_is_longer_for_lf_and_bounded_for_tweeter() -> None:
    assert driver_sweep_duration_s("subwoofer") == 12.0
    assert driver_sweep_duration_s("woofer") == 12.0
    assert driver_sweep_duration_s("mid") == 8.0
    assert driver_sweep_duration_s("tweeter") == 4.0
    assert driver_sweep_duration_s("future_role") == 6.0


def test_driver_ambient_duration_is_right_sized_per_driver_not_worst_case() -> None:
    """A tweeter's 4 s sweep must not inherit the longest driver's ~14 s pause
    (the pre-2026-07-16 fixed CROSSOVER_AMBIENT_DURATION_S). Contract: the
    capture spec's ambient_duration_ms and this plan module's own duration
    table must agree, per driver kind, through
    ``jasper.active_speaker.test_signal_plan.driver_ambient_duration_s`` — the
    single function the capture spec builder (correction_setup._open) resolves
    the per-driver ambient window through."""
    from jasper.active_speaker.test_signal_plan import (
        AMBIENT_DURATION_MARGIN_S,
        CROSSOVER_AMBIENT_DURATION_S,
        DRIVER_SWEEP_DURATIONS_S,
        driver_ambient_duration_s,
    )

    for role, sweep_s in DRIVER_SWEEP_DURATIONS_S.items():
        expected = sweep_s + AMBIENT_DURATION_MARGIN_S
        assert driver_ambient_duration_s(role) == expected

    # A short driver's ambient window is strictly shorter than the historical
    # worst-case constant — the whole point of the right-sizing fix.
    assert driver_ambient_duration_s("tweeter") < CROSSOVER_AMBIENT_DURATION_S
    assert driver_ambient_duration_s("mid") < CROSSOVER_AMBIENT_DURATION_S
    # The longest driver's own ambient window still matches the worst-case
    # ceiling exactly (it IS the driver the ceiling was sized against).
    assert driver_ambient_duration_s("woofer") == CROSSOVER_AMBIENT_DURATION_S

    # The analyzer's REAL pairing requirement, pinned to its own named
    # constant: _capture_to_magnitude selects the quiet crop starting
    # AMBIENT_CONTROLLED_LEAD_S before the sweep-length window and raises when
    # that start precedes the controlled interval — so it effectively requires
    # ambient_duration >= kernel sweep duration + AMBIENT_CONTROLLED_LEAD_S.
    # The kernel sweep runs slightly LONGER than the requested duration (the
    # synchronized-sweep kernel rounds ~12.0 s up to ~12.09 s so its phase
    # closes cleanly); ROUNDING_ALLOWANCE covers that growth with headroom.
    # Importing the analyzer's constant means a future margin reduction below
    # its requirement fails HERE instead of silently rejecting every capture
    # at runtime.
    from jasper.active_speaker.driver_acoustics import AMBIENT_CONTROLLED_LEAD_S

    ROUNDING_ALLOWANCE_S = 0.25  # synchronized-sweep kernel phase-rounding (~0.09 s)
    for role, sweep_s in DRIVER_SWEEP_DURATIONS_S.items():
        assert driver_ambient_duration_s(role) >= (
            sweep_s + AMBIENT_CONTROLLED_LEAD_S + ROUNDING_ALLOWANCE_S
        )
