# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.active_speaker.calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    MAX_TEST_LEVEL_DBFS,
    MIN_TEST_LEVEL_DBFS,
    calibration_level_payload,
)
from jasper.active_speaker.driver_protection import (
    AUTO_LEVEL_DECISION_KIND,
    DRIVER_PROTECTION_KIND,
    auto_level_decision,
    derive_hf_measurement_ceiling_dbfs,
    driver_protection_payload,
    driver_protection_profile,
)


def test_low_frequency_auto_level_raises_one_bounded_step_when_mic_is_low() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS + 4,
        observed_mic_dbfs=-52,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        observed_mic_dbfs=-52,
        floor_audio_confirmed=True,
    )

    assert decision["kind"] == AUTO_LEVEL_DECISION_KIND
    assert decision["status"] == "raise"
    assert decision["action"] == "raise"
    assert decision["next_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS + 4 + AUDIBLE_RAMP_STEP_DB
    )
    assert decision["applied_delta_db"] == AUDIBLE_RAMP_STEP_DB
    assert decision["driver_protection"]["role_class"] == "low_frequency"


def test_auto_level_can_raise_after_floor_confirmation_without_mic_reading() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        floor_audio_confirmed=True,
    )

    assert decision["status"] == "raise"
    assert decision["action"] == "raise"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS + AUDIBLE_RAMP_STEP_DB
    assert decision["reason"] == "operator-controlled raise toward audible"


def test_auto_level_does_not_raise_above_floor_without_confirmation() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS + 1,
        observed_mic_dbfs=-58,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        observed_mic_dbfs=-58,
        floor_audio_confirmed=False,
    )

    assert decision["status"] == "waiting_for_floor_confirmation"
    assert decision["action"] == "hold_for_floor_confirmation"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS + 1
    assert decision["applied_delta_db"] == 0


def test_auto_level_holds_when_mic_is_usable() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=-70,
        observed_mic_dbfs=-32,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        observed_mic_dbfs=-32,
        floor_audio_confirmed=True,
    )

    assert decision["status"] == "locked"
    assert decision["action"] == "hold"
    assert decision["next_level_dbfs"] == -70


def test_auto_level_resets_to_floor_on_clipping() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=-70,
        observed_mic_dbfs=-18,
        mic_clipping=True,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        observed_mic_dbfs=-18,
        mic_clipping=True,
        floor_audio_confirmed=True,
    )

    assert decision["status"] == "reset"
    assert decision["action"] == "reset_to_floor"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert decision["mic_meter"]["status"] == "clipping"


def test_high_frequency_auto_level_waits_for_floor_confirmation() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS,
        observed_mic_dbfs=-60,
    )

    decision = auto_level_decision(
        current,
        role="tweeter",
        driver_style="dome_tweeter",
        protection_status="software_guard_requested",
        band_limit={"type": "highpass", "highpass_hz": 3000},
        observed_mic_dbfs=-60,
        floor_audio_confirmed=False,
    )

    assert decision["status"] == "waiting_for_floor_confirmation"
    assert decision["action"] == "hold_for_floor_confirmation"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert decision["driver_protection"]["role_class"] == "high_frequency"


def test_high_frequency_auto_level_uses_driver_specific_cap() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=-65,
        observed_mic_dbfs=-60,
    )

    decision = auto_level_decision(
        current,
        role="tweeter",
        driver_style="ribbon_tweeter",
        protection_status="software_guard_requested",
        band_limit={"type": "highpass", "highpass_hz": 5000},
        observed_mic_dbfs=-60,
        floor_audio_confirmed=True,
    )

    assert decision["status"] == "maxed"
    assert decision["action"] == "hold_at_cap"
    assert decision["next_level_dbfs"] == -65
    assert decision["max_auto_level_dbfs"] == -65
    assert "auto_level_cap_reached" in {
        issue["code"] for issue in decision["issues"]
    }


def test_high_frequency_protection_requires_highpass_band_limit() -> None:
    missing = driver_protection_payload(
        "tweeter",
        driver_style="ribbon_tweeter",
        protection_status="software_guard_requested",
    )
    blocked = driver_protection_payload(
        "tweeter",
        driver_style="ribbon_tweeter",
        protection_status="software_guard_requested",
        band_limit={"type": "highpass", "highpass_hz": 3000},
    )
    allowed = driver_protection_payload(
        "tweeter",
        driver_style="ribbon_tweeter",
        protection_status="software_guard_requested",
        band_limit={"type": "highpass", "highpass_hz": 5000},
    )

    assert missing["audio_allowed"] is False
    assert "high_frequency_highpass_missing" in {
        issue["code"] for issue in missing["issues"]
    }
    assert blocked["kind"] == DRIVER_PROTECTION_KIND
    assert blocked["audio_allowed"] is False
    assert "high_frequency_highpass_missing" in {
        issue["code"] for issue in blocked["issues"]
    }
    assert allowed["audio_allowed"] is True


# Style -> protective high-pass floor, pinned per driver style. A compression
# driver (JTS3 hardware: B&C DE250-8, punch #14) floors at 2000 Hz; every
# other declared style floors higher; an undeclared/unrecognised style keeps
# today's conservative 5000 Hz default. This is the table a mis-declared or
# never-declared style silently falls back to, so it is pinned in full rather
# than spot-checked.
@pytest.mark.parametrize(
    ("driver_style", "expected_floor_hz"),
    (
        ("compression_driver", 2000.0),
        ("horn_compression_driver", 2000.0),
        ("dome_tweeter", 3000.0),
        ("amt_tweeter", 3000.0),
        ("planar_tweeter", 3500.0),
        ("ribbon_tweeter", 5000.0),
        ("supertweeter", 8000.0),
        (None, 5000.0),
        ("", 5000.0),
        ("some_future_style_not_in_the_table", 5000.0),
    ),
)
def test_tweeter_style_high_pass_floor_table(
    driver_style: str | None, expected_floor_hz: float
) -> None:
    profile = driver_protection_profile("tweeter", driver_style=driver_style)

    assert profile.min_highpass_hz == expected_floor_hz


def test_undeclared_tweeter_style_keeps_conservative_floor_when_hardware_ceiling_is_lower() -> None:
    # JTS3 shape: a woofer/tweeter pair with a 4000 Hz hard code-policy
    # ceiling and a compression tweeter meant to cross around 1.8-2.5 kHz.
    # Before a style is declared, the driver reads as "unknown" and the
    # conservative 5000 Hz floor exceeds the 4000 Hz ceiling, so no coherent
    # crossover exists -- this is the exact deadlock the gap produced.
    undeclared = driver_protection_profile("tweeter", driver_style=None)
    assert undeclared.min_highpass_hz == 5000.0
    assert undeclared.min_highpass_hz > 4000.0  # hard ceiling in the JTS3 shape

    declared = driver_protection_profile("tweeter", driver_style="compression_driver")
    assert declared.min_highpass_hz == 2000.0
    assert declared.min_highpass_hz <= 4000.0


# --- derive_hf_measurement_ceiling_dbfs (W6.5 two-invariant protection model) -


def test_shipped_jts3_preset_numbers_derive_the_operative_ceiling() -> None:
    """The shipped preset's own numbers, hand-computed, with no hedge left.

    ``bc_de250_dayton_e150he44_v1`` as commissioned on JTS3: the woofer's
    admitted cap is 0.0 dBFS (its declared peak, clamped by the low-frequency
    class default ``MAX_TEST_LEVEL_DBFS``), and the declared sensitivities are
    108.5 dB (B&C DE250) against 83.3 dB (Dayton Epique E150HE-44).

        108.5 - 83.3 = 25.2 dB delta
        0.0 - 25.2   = -25.2 dBFS

    The retired -35.0 dBFS absolute hedge would have clamped this to -35, which
    is the 9.8 dB of unvalidated conservatism that capped the 2026-08-19
    leveling session at 68.07 dB SPL. Mutation guard: restore that hedge and
    this fails.
    """

    ceiling = derive_hf_measurement_ceiling_dbfs(
        declared_lf_driver_cap_dbfs=0.0,
        sens_hf_db=108.5,
        sens_lf_db=83.3,
    )
    assert ceiling == pytest.approx(-25.2)


def test_operator_worked_example_derives_without_the_retired_hedge() -> None:
    # The operator's own worked example (2026-07-19 ruling), woofer cap -8 with
    # the same 25.2 dB delta: -8 - 25.2 = -33.2. The retired -35 hedge clamped
    # even this, 1.8 dB below the sensitivity arithmetic.
    ceiling = derive_hf_measurement_ceiling_dbfs(
        declared_lf_driver_cap_dbfs=-8.0,
        sens_hf_db=108.5,
        sens_lf_db=83.3,
    )
    assert ceiling == pytest.approx(-33.2)


def test_ceiling_never_exceeds_the_lf_cap_less_the_sensitivity_delta() -> None:
    """The anti-runaway direction, over the whole realistic input envelope.

    The invariant that replaced the absolute hedge: a high-frequency driver is
    admitted at the ACOUSTIC level its low-frequency sibling is already
    admitted at, so its ceiling sits the full sensitivity delta below that
    sibling's own cap and can never be raised past it by the derivation.
    """

    for lf_cap in (0.0, -8.0, -20.0, -30.0, -55.0):
        for sens_hf, sens_lf in (
            (108.5, 83.3),
            (100.0, 84.0),
            (89.2, 88.5),
            (90.0, 90.0),
        ):
            ceiling = derive_hf_measurement_ceiling_dbfs(
                declared_lf_driver_cap_dbfs=lf_cap,
                sens_hf_db=sens_hf,
                sens_lf_db=sens_lf,
            )
            assert ceiling <= lf_cap - (sens_hf - sens_lf) + 1e-9
            # A real pair has the more sensitive driver on top, so the ceiling
            # also stays at or below the sibling's own cap.
            assert ceiling <= lf_cap + 1e-9


def test_sensitivity_relative_ceiling_is_the_operative_one() -> None:
    # A quieter LF cap (-30) with a 16 dB sensitivity delta: -30 - 16 = -46.
    # Under the retired hedge this was the interesting case only because it
    # fell BELOW -35; it is now simply what the derivation returns.
    ceiling = derive_hf_measurement_ceiling_dbfs(
        declared_lf_driver_cap_dbfs=-30.0,
        sens_hf_db=100.0,
        sens_lf_db=84.0,
    )
    assert ceiling == pytest.approx(-46.0)


def test_zero_sensitivity_delta_lands_on_the_lf_cap_itself() -> None:
    # Equal sensitivities: the derivation returns the LF cap outright, because
    # equal-sensitivity drivers reach the same acoustic level at the same
    # digital level. The retired -35 hedge overrode this by 15 dB; nothing
    # does now, which is the point -- the LF driver's own admitted cap is the
    # bound, not a constant.
    ceiling = derive_hf_measurement_ceiling_dbfs(
        declared_lf_driver_cap_dbfs=-20.0,
        sens_hf_db=90.0,
        sens_lf_db=90.0,
    )
    assert ceiling == pytest.approx(-20.0)


def test_impossible_declared_delta_is_stopped_at_the_global_test_ceiling() -> None:
    """The one case the remaining bound exists for -- and it is not a hedge.

    Declared sensitivities carry no plausibility validation, so a swapped or
    mis-typed pair can present a NEGATIVE delta, which the arithmetic would
    turn into a ceiling above digital full scale (0.0 - -12 = +12 dBFS). A cap
    above full scale switches off per-channel admission enforcement for that
    driver entirely, so ``MAX_TEST_LEVEL_DBFS`` stops it. Nothing about a real
    tweeter/woofer pair reaches this branch.
    """

    ceiling = derive_hf_measurement_ceiling_dbfs(
        declared_lf_driver_cap_dbfs=0.0,
        sens_hf_db=80.0,
        sens_lf_db=92.0,
    )
    assert ceiling == pytest.approx(MAX_TEST_LEVEL_DBFS)
    assert ceiling == pytest.approx(0.0)
