"""Per-driver commissioning safety: the protection-while-audible assertion.

The single-audio-path commissioning loads, one driver at a time, a config where
exactly the target driver's physical outputs are unmuted and every other output
is hard-muted. ``driver_commission_audible_evidence`` is the config-level form
of the Stage-5 rule "the protective high-pass must be present before the tweeter
is unmuted": it gates whether such a per-driver config is even allowed to load.

These tests pin (1) the audible mask is exactly the target (everything else
hard-muted, fail-closed), and (2) an AUDIBLE tweeter still carries its protective
Linkwitz-Riley high-pass + startup limiter — and that a config which unmutes a
tweeter while stripping that protection FAILS.
"""

from __future__ import annotations

import yaml as yaml_lib

from jasper.active_speaker import (
    ActiveSpeakerPreset,
    audible_outputs_for_role,
    driver_commission_audible_evidence,
    emit_active_speaker_commissioning_config,
)

# Reuse the canonical preset fixtures.
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def _preset(builder) -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(builder)


def _emit(preset: ActiveSpeakerPreset, audible: set[int]) -> str:
    return emit_active_speaker_commissioning_config(
        preset, playback_device=ACTIVE_PCM, audible_outputs=audible
    )


# --- mask correctness --------------------------------------------------------


def test_woofer_target_passes_with_tweeter_muted():
    preset = _preset(_two_way_preset())  # mono 2-way: woofer=0, tweeter=1
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    ev = driver_commission_audible_evidence(
        _emit(preset, woofer), preset=preset, audible_outputs=woofer
    )
    assert ev["passed"] is True
    assert ev["checks"]["audible_mask_correct"] is True
    assert ev["audible_outputs"] == sorted(woofer)
    # The tweeter is not audible for a woofer target -> its protection check is
    # vacuously satisfied while it stays muted.
    assert ev["audible_tweeter_outputs"] == []
    assert ev["checks"]["tweeter_protected_while_audible"] is True
    assert set(ev["muted_outputs"]) == set(audible_outputs_for_role(preset, "tweeter"))


def test_tweeter_target_passes_with_protection_present():
    preset = _preset(_two_way_preset())
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    ev = driver_commission_audible_evidence(
        _emit(preset, tweeter), preset=preset, audible_outputs=tweeter
    )
    assert ev["passed"] is True
    assert ev["audible_tweeter_outputs"] == sorted(tweeter)
    # An audible tweeter MUST keep its protective high-pass + limiter.
    assert ev["checks"]["tweeter_protected_while_audible"] is True
    assert ev["protective_highpass_hz"] == 3200.0  # 1600 Hz crossover * 2.0


def test_empty_mask_is_not_a_valid_per_driver_load():
    # The all-muted boot config is the staged crash-recovery config, not a
    # per-driver commissioning load: no driver audible -> mask not correct.
    preset = _preset(_two_way_preset())
    ev = driver_commission_audible_evidence(
        _emit(preset, set()), preset=preset, audible_outputs=set()
    )
    assert ev["checks"]["audible_mask_correct"] is False
    assert ev["passed"] is False


def test_claiming_more_audible_than_the_config_fails_closed():
    # Config unmutes only the woofer; claiming the tweeter is also audible must
    # fail (its commission-mute is engaged in the config) -> fail closed.
    preset = _preset(_two_way_preset())
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    ev = driver_commission_audible_evidence(
        _emit(preset, woofer), preset=preset, audible_outputs=woofer | tweeter
    )
    assert ev["checks"]["audible_mask_correct"] is False
    assert ev["passed"] is False


# --- the critical safety case ------------------------------------------------


def test_audible_tweeter_without_high_pass_fails():
    # An audible tweeter whose protective high-pass has been corrupted (here:
    # moved to the wrong frequency) MUST fail the protection-while-audible check.
    # This is the driver-damage hazard the gate exists to catch.
    preset = _preset(_two_way_preset())
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    parsed = yaml_lib.safe_load(_emit(preset, tweeter))
    parsed["filters"]["as_tweeter_protective_hp"]["parameters"]["freq"] = 200.0
    corrupted = yaml_lib.safe_dump(parsed)

    ev = driver_commission_audible_evidence(
        corrupted, preset=preset, audible_outputs=tweeter
    )
    assert ev["checks"]["tweeter_protected_while_audible"] is False
    assert ev["passed"] is False


def test_audible_tweeter_without_limiter_fails():
    preset = _preset(_two_way_preset())
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    parsed = yaml_lib.safe_load(_emit(preset, tweeter))
    # Weaken the startup limiter ceiling away from the protected value.
    parsed["filters"]["as_tweeter_startup_limiter"]["parameters"]["clip_limit"] = 0.0
    corrupted = yaml_lib.safe_dump(parsed)

    ev = driver_commission_audible_evidence(
        corrupted, preset=preset, audible_outputs=tweeter
    )
    assert ev["checks"]["tweeter_protected_while_audible"] is False
    assert ev["passed"] is False


# --- 3-way -------------------------------------------------------------------


def test_three_way_tweeter_target_protected():
    preset = _preset(_three_way_preset("stereo"))  # 6 outputs
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    ev = driver_commission_audible_evidence(
        _emit(preset, tweeter), preset=preset, audible_outputs=tweeter
    )
    assert ev["passed"] is True
    assert ev["audible_tweeter_outputs"] == sorted(tweeter)
    assert ev["checks"]["tweeter_protected_while_audible"] is True


def test_three_way_mid_target_keeps_tweeter_muted():
    preset = _preset(_three_way_preset("stereo"))
    mid = set(audible_outputs_for_role(preset, "mid"))
    ev = driver_commission_audible_evidence(
        _emit(preset, mid), preset=preset, audible_outputs=mid
    )
    assert ev["passed"] is True
    # The tweeter is not in the audible mid mask, so it stays muted.
    assert ev["audible_tweeter_outputs"] == []
    assert set(audible_outputs_for_role(preset, "tweeter")) <= set(ev["muted_outputs"])
