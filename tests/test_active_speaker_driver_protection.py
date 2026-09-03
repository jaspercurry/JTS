# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.active_speaker.calibration_level import MAX_TEST_LEVEL_DBFS
from jasper.active_speaker.driver_protection import (
    DRIVER_PROTECTION_KIND,
    LOW_LIMIT_DECLARED,
    LOW_LIMIT_STYLE_DEFAULT,
    derive_hf_measurement_ceiling_dbfs,
    driver_excitation_floor_hz,
    driver_protection_payload,
    driver_protection_profile,
    format_low_limit,
    tone_gate_low_limit,
)

# B&C's published minimum recommended crossover for the DE250 compression
# driver, and the compression_driver class default it sits below. The exact
# pair #2603 was filed on and #2874 finished.
DE250_LOW_LIMIT_HZ = 1600.0
COMPRESSION_DRIVER_CLASS_DEFAULT_HZ = 2000.0


def test_high_frequency_protection_requires_highpass_band_limit() -> None:
    """Absent and below-floor are different facts, named differently (#2874).

    They shared ``high_frequency_highpass_missing`` until then, so a tone whose
    protective high-pass was present -- at the manufacturer's own published
    figure -- was refused with a sentence saying one was MISSING. Both codes
    now name the floor they compared against and where that floor came from.
    """

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
    absent = [
        issue for issue in missing["issues"]
        if issue["code"] == "high_frequency_highpass_missing"
    ]
    assert absent, missing["issues"]
    assert "none is staged" in absent[0]["message"]
    assert "5000 Hz (class fallback; nothing declared)" in absent[0]["message"]

    assert blocked["kind"] == DRIVER_PROTECTION_KIND
    assert blocked["audio_allowed"] is False
    below = [
        issue for issue in blocked["issues"]
        if issue["code"] == "high_frequency_highpass_below_low_limit"
    ]
    assert below, blocked["issues"]
    # A staged high-pass is NOT reported as an absent one.
    assert "high_frequency_highpass_missing" not in {
        issue["code"] for issue in blocked["issues"]
    }
    assert "3000 Hz" in below[0]["message"]
    assert "5000 Hz (class fallback; nothing declared)" in below[0]["message"]

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
    """JTS3's own commissioned numbers, hand-computed, with no hedge left.

    ``bc_de250_dayton_e150he44_v1`` as commissioned on JTS3. The woofer's
    admitted cap is 0.0 dBFS (its declared peak, clamped by the low-frequency
    class default ``MAX_TEST_LEVEL_DBFS``). The sensitivities are 108.5 dB
    (B&C DE250) against 83.3 dB (Dayton Epique E150HE-44) — note where each
    comes from, because it is NOT one artifact: 83.3 is in the preset
    JSON, while the preset declares no tweeter ``sensitivity_db`` at all and
    108.5 rides JTS3's persisted design draft ``manual_settings``, which is the
    one owner of declared sensitivity (``declared_driver_sensitivities``).

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


def test_negative_delta_runs_out_of_headroom_at_the_global_test_ceiling() -> None:
    """A NEGATIVE sensitivity delta is legitimate, and the clamp is where it
    runs out of digital headroom — not an error stop.

    A high-sensitivity pro woofer (~97 dB) under a modest dome tweeter
    (~90 dB) gives delta = −7. The arithmetic correctly asks for MORE digital
    level than the woofer's own cap, because the quieter tweeter needs it to
    reach the same SPL: 0.0 − (−7) = +7 dBFS. Full scale is simply where that
    request runs out.

    Still safe by the derivation's own argument, which is why no hedge is
    wanted here: at the clamped 0 dBFS this tweeter makes ~90 dB while the
    already-admitted woofer at ITS 0 dBFS cap makes ~97 dB — acoustically
    quieter than something the ledger already permits.
    """

    ceiling = derive_hf_measurement_ceiling_dbfs(
        declared_lf_driver_cap_dbfs=0.0,
        sens_hf_db=90.0,
        sens_lf_db=97.0,
    )
    assert ceiling == pytest.approx(MAX_TEST_LEVEL_DBFS)
    assert ceiling == pytest.approx(0.0)


def test_a_swapped_declaration_is_not_caught_by_the_ledger() -> None:
    """The residual the retirement leaves, pinned so it cannot be forgotten.

    Sensitivities carry no plausibility validation, so swapping the two rows
    of a real JTS3-shaped declaration (108.5 tweeter / 83.3 woofer typed the
    wrong way round) presents delta = −25.2 for a driver that is genuinely
    25.2 dB MORE sensitive. The derivation cannot tell, and the ceiling lands
    at full scale — where the retired −35 dBFS hedge used to clamp it by
    accident.

    This is deliberately NOT fixed by refusing to derive on delta <= 0: the
    test above shows that same shape is a legitimate pro-woofer configuration.
    The two derive the SAME ceiling — both ask for more than full scale, both
    clamp to ``MAX_TEST_LEVEL_DBFS`` — so they are 0.0 dB apart and no constant
    here can separate them even in principle. Nor can a backstop be parked just
    under full scale: an ordinary CORRECT coax (88.5 woofer / 89.2 tweeter)
    derives −0.70, so that regime is where correct hardware already lives.
    Validating the declaration is the fix -- tracked as issue #2765, and it
    needs no invented anchor because the preset already ships the woofer's own
    ``sensitivity_db``. Pinned as the honest current behaviour so a reader is
    never surprised by it.
    """

    swapped = derive_hf_measurement_ceiling_dbfs(
        declared_lf_driver_cap_dbfs=0.0,
        sens_hf_db=83.3,
        sens_lf_db=108.5,
    )
    assert swapped == pytest.approx(MAX_TEST_LEVEL_DBFS)
    # 35 dB louder than the retired hedge would have allowed, on a declaration
    # that is simply wrong. The ledger is not what catches this.
    assert swapped - (-35.0) == pytest.approx(35.0)


# --- #2874: the tone gate anchors on the resolved low limit -------------------


def test_the_tone_gate_anchors_on_the_declared_low_limit_not_the_class_default(
) -> None:
    """The #2603 bug on the surface that ruling never reached.

    jts3 as declared: a DE250 tweeter whose manufacturer publishes 1.6 kHz,
    under a compression_driver class default of 2 kHz. A tone staged with a
    protective high-pass at exactly the published figure was refused -- and
    refused with copy claiming the high-pass was MISSING -- because
    ``_highpass_satisfied`` compared against the class constant. It now
    compares against the resolved low limit, and the refusal that remains at
    1500 Hz names the number it compared against and where that number came
    from, so nobody has to guess which of two floats bounds the corner.
    """

    at_the_declared_floor = driver_protection_payload(
        "tweeter",
        driver_style="compression_driver",
        protection_status="present",
        band_limit={"type": "highpass", "highpass_hz": DE250_LOW_LIMIT_HZ},
        declared_low_limit_hz=DE250_LOW_LIMIT_HZ,
    )
    assert at_the_declared_floor["band_limit_highpass_ok"] is True
    assert at_the_declared_floor["issues"] == []
    assert at_the_declared_floor["audio_allowed"] is True
    assert at_the_declared_floor["low_limit_hz"] == DE250_LOW_LIMIT_HZ
    assert at_the_declared_floor["low_limit_provenance"] == LOW_LIMIT_DECLARED
    assert at_the_declared_floor["low_limit_summary"] == (
        "1600 Hz (manufacturer declared)"
    )

    below = driver_protection_payload(
        "tweeter",
        driver_style="compression_driver",
        protection_status="present",
        band_limit={"type": "highpass", "highpass_hz": 1500.0},
        declared_low_limit_hz=DE250_LOW_LIMIT_HZ,
    )
    assert below["band_limit_highpass_ok"] is False
    refusal = next(
        issue for issue in below["issues"]
        if issue["code"] == "high_frequency_highpass_below_low_limit"
    )
    assert "1600 Hz (manufacturer declared)" in refusal["message"]
    assert "1500 Hz" in refusal["message"]


def test_the_class_default_still_gates_a_tone_for_an_undeclared_driver() -> None:
    """The one tone-gate job the class table keeps: the fallback.

    Nothing declared, so a staged 1900 Hz is still refused -- against the 2000
    Hz compression_driver default, and the copy says the floor is a class
    fallback rather than passing a code default off as a datasheet figure.
    """

    payload = driver_protection_payload(
        "tweeter",
        driver_style="compression_driver",
        protection_status="present",
        band_limit={"type": "highpass", "highpass_hz": 1900.0},
    )

    assert payload["band_limit_highpass_ok"] is False
    assert payload["audio_allowed"] is False
    assert payload["low_limit_hz"] == COMPRESSION_DRIVER_CLASS_DEFAULT_HZ
    assert payload["low_limit_provenance"] == LOW_LIMIT_STYLE_DEFAULT
    refusal = next(
        issue for issue in payload["issues"]
        if issue["code"] == "high_frequency_highpass_below_low_limit"
    )
    assert "class fallback" in refusal["message"]
    assert "2000 Hz" in refusal["message"]


def test_the_payload_never_prints_the_class_figure_beside_a_declared_one() -> None:
    """#2874's confusion surface, on the tone gate's own artifact.

    Two floats one key apart with only one of them labelled is what sent two
    readers looking for a second floor on the corner. The class figure still
    travels -- as ``low_limit_hz`` whenever it is the operative number -- but
    never as an unlabelled peer beside a declared one.
    """

    declared = driver_protection_payload(
        "tweeter",
        driver_style="compression_driver",
        protection_status="present",
        band_limit={"type": "highpass", "highpass_hz": DE250_LOW_LIMIT_HZ},
        declared_low_limit_hz=DE250_LOW_LIMIT_HZ,
    )
    assert "min_highpass_hz" not in declared
    assert declared["low_limit_hz"] == DE250_LOW_LIMIT_HZ

    undeclared = driver_protection_payload(
        "tweeter",
        driver_style="compression_driver",
        protection_status="present",
    )
    assert undeclared["low_limit_hz"] == (
        driver_protection_profile(
            "tweeter", driver_style="compression_driver"
        ).min_highpass_hz
    )


def test_tone_gate_low_limit_delegates_its_order_to_the_one_resolver() -> None:
    """Declared wins; absent falls back; a role with no anchor has no floor.

    Same three-step order ``resolve_driver_low_limit`` owns, reached through
    the tone gate's own entry point so the two cannot drift.
    """

    declared = tone_gate_low_limit(
        "tweeter",
        driver_style="compression_driver",
        declared_low_limit_hz=DE250_LOW_LIMIT_HZ,
    )
    assert declared is not None
    assert declared.frequency_hz == DE250_LOW_LIMIT_HZ
    assert declared.provenance == LOW_LIMIT_DECLARED
    assert format_low_limit(declared) == "1600 Hz (manufacturer declared)"

    fallback = tone_gate_low_limit("tweeter", driver_style="compression_driver")
    assert fallback is not None
    assert fallback.frequency_hz == COMPRESSION_DRIVER_CLASS_DEFAULT_HZ
    assert fallback.provenance == LOW_LIMIT_STYLE_DEFAULT
    assert format_low_limit(fallback) == "2000 Hz (class fallback; nothing declared)"

    # No style anchor exists for a low-frequency role, and inventing one is the
    # nanny behaviour the never-nanny ruling excludes.
    assert tone_gate_low_limit("woofer") is None


def test_a_declared_low_limit_above_the_class_default_still_tightens() -> None:
    """The gate is not one-directional relief.

    A supertweeter declared at 10 kHz under an 8 kHz class default keeps the
    stricter DECLARED number: the ruling is that the declaration wins, not that
    the lower of the two wins.
    """

    payload = driver_protection_payload(
        "tweeter",
        driver_style="supertweeter",
        protection_status="present",
        band_limit={"type": "highpass", "highpass_hz": 9000.0},
        declared_low_limit_hz=10000.0,
    )

    assert payload["band_limit_highpass_ok"] is False
    assert payload["low_limit_hz"] == 10000.0
    assert payload["low_limit_provenance"] == LOW_LIMIT_DECLARED



# --- the full_range (way-1) protection class --------------------------------


_BAND = {"measurement_band_hz": [40.0, 15000.0]}


@pytest.mark.parametrize(
    "driver, expected",
    [
        pytest.param(
            {"recommended_highpass_hz": 80.0, **_BAND}, 80.0, id="declared_owner",
        ),
        pytest.param(
            {"required_protection_filters": [
                {"kind": "highpass", "cutoff_hz": 90.0},
            ], **_BAND},
            90.0, id="a_stored_protective_highpass",
        ),
        pytest.param(
            {"measurement_band_hz": [60.0, 15000.0]}, 60.0, id="the_band_low_edge",
        ),
        pytest.param({"model": "Example"}, None, id="nothing_declared_is_no_floor"),
    ],
)
def test_the_excitation_floor_reads_the_declaration_and_never_invents_one(
    driver, expected
) -> None:
    """The class table is deliberately not consulted: the frequency a driver may
    be DRIVEN to is a question only a declaration answers."""
    assert driver_excitation_floor_hz(driver) == expected


def test_full_range_shares_its_ceiling_and_floor_test_duration_with_tweeter() -> None:
    """A single amp channel is at least as fragile as the tweeter it may drive
    -- the two classes share one figure on each axis, not independent ones."""
    full_range = driver_protection_profile("full_range", declared_floor_hz=60.0)
    tweeter = driver_protection_profile("tweeter", driver_style="dome_tweeter")

    assert full_range.max_auto_level_dbfs == tweeter.max_auto_level_dbfs
    assert full_range.floor_test_duration_ms == tweeter.floor_test_duration_ms
