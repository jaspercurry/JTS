# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract pins for a driver's low limit having exactly ONE declared owner.

Owner ruling 2026-08-17 (issue #2603; ``active-speaker-tuning-layers-design.md``
decisions 8 and 9): a driver's bottom allowed frequency IS the manufacturer's
minimum recommended crossover frequency, carrying its slope condition, entered
once at component entry. Before this module existed the same fact was
co-declared in six places on one target, and jts3 shipped a 1648.7 Hz corner
that was legal under ``hard_excitation_band_hz[0] = 1600`` and illegal under
``required_protection_filters[highpass].cutoff_hz = 2000``.

The pins below are deliberately ordered as the argument runs: the owner is read
first, every derived field follows it (proved by MUTATION, not by re-asserting
a literal), the two backwards-compatible reads never loosen an already-deployed
box, and the one place a code default is allowed to speak is bounded.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker.driver_protection import (
    LOW_LIMIT_DECLARED,
    LOW_LIMIT_LEGACY_PROTECTION_FILTER,
    LOW_LIMIT_PLAUSIBILITY_FACTOR,
    LOW_LIMIT_STYLE_DEFAULT,
    PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE,
    apply_driver_low_limit,
    declared_protection_highpass_floor_hz,
    driver_low_limit_plausibility_band_hz,
    driver_low_limit_plausible,
    driver_protection_profile,
    resolve_driver_low_limit,
)

# The B&C DE250 exactly as the owner attested it on 2026-08-17: a published
# Recommended Crossover of 1.6 kHz footnoted "12 dB/oct. or higher slope
# high-pass filter", alongside a published 1.0-18.0 kHz frequency range. The
# 2000 Hz figure the old research prompt produced is NOT published for this
# driver by any source checked.
DE250_LOW_LIMIT_HZ = 1600.0
DE250_PUBLISHED_SLOPE_DB_PER_OCTAVE = 12.0
DE250_PUBLISHED_RANGE_HZ = [1000.0, 18000.0]

#: Every field the low limit owns. Named once here so the mutation pin below
#: cannot silently stop covering one of them.
DERIVED_FIELDS = (
    "hard_excitation_band_hz",
    "measurement_band_hz",
    "required_protection_filters",
)


def _de250(**overrides) -> dict:
    driver = {
        "role": "tweeter",
        "model": "DE250",
        "recommended_highpass_hz": DE250_LOW_LIMIT_HZ,
        "recommended_highpass_slope_db_per_octave": DE250_PUBLISHED_SLOPE_DB_PER_OCTAVE,
        "hard_excitation_band_hz": [DE250_LOW_LIMIT_HZ, 20000.0],
        "measurement_band_hz": list(DE250_PUBLISHED_RANGE_HZ),
    }
    driver.update(overrides)
    return driver


def _derived(driver: dict, *, style: str = "compression_driver") -> dict:
    return apply_driver_low_limit(driver, role="tweeter", driver_style=style)


def _highpass(driver: dict) -> dict:
    return next(
        item
        for item in driver["required_protection_filters"]
        if item["kind"] == "highpass"
    )


# --- the owner, and every consumer deriving from it -------------------------


def test_the_owner_is_the_declared_minimum_recommended_crossover() -> None:
    limit = resolve_driver_low_limit(
        _de250(), role="tweeter", driver_style="compression_driver"
    )
    assert limit is not None
    assert limit.frequency_hz == DE250_LOW_LIMIT_HZ
    assert limit.slope_db_per_octave == DE250_PUBLISHED_SLOPE_DB_PER_OCTAVE
    assert limit.provenance == LOW_LIMIT_DECLARED


@pytest.mark.parametrize("mutated_hz", [1250.0, 1600.0, 2500.0, 3000.0])
def test_every_derived_field_moves_with_the_owner(mutated_hz: float) -> None:
    """THE pin: mutate the one owner and every consumer follows.

    Proved by mutation rather than by re-asserting literals, because a literal
    assertion passes just as well against a field that is quietly declared
    somewhere else — which is the exact defect #2603 records.
    """

    derived = _derived(_de250(recommended_highpass_hz=mutated_hz))

    assert derived["hard_excitation_band_hz"][0] == mutated_hz
    assert _highpass(derived)["cutoff_hz"] == mutated_hz
    assert declared_protection_highpass_floor_hz(derived) == mutated_hz
    # The analysis window is a SEPARATE published fact (the datasheet response
    # range) and keeps its own value -- clamped up into the allowed band, never
    # dragged down below the frequency the driver may not be excited under.
    assert derived["measurement_band_hz"][0] == max(
        DE250_PUBLISHED_RANGE_HZ[0], mutated_hz
    )
    assert derived["measurement_band_hz"][1] == DE250_PUBLISHED_RANGE_HZ[1]


def test_the_de250_stores_the_two_facts_apart() -> None:
    """1.6 kHz minimum crossover and a 1.0-18 kHz response range are different
    facts about the same driver, and the second is not a low limit."""

    derived = _derived(_de250())
    assert derived["recommended_highpass_hz"] == 1600.0
    assert derived["measurement_band_hz"] == [1600.0, 18000.0]
    assert derived["hard_excitation_band_hz"] == [1600.0, 20000.0]


def test_the_projection_touches_exactly_the_fields_the_low_limit_owns() -> None:
    """No silent widening. If a later change makes the projection write a field
    that is not in :data:`DERIVED_FIELDS`, that field has quietly acquired a
    second writer and this fails until the list says so out loud."""

    before = _de250()
    after = _derived(before)
    moved = {key for key in after if key not in before or after[key] != before[key]}
    owner_fields = {
        "recommended_highpass_hz",
        "recommended_highpass_slope_db_per_octave",
    }
    assert moved <= set(DERIVED_FIELDS) | owner_fields, moved


def test_the_projection_is_idempotent() -> None:
    """Re-deriving an already-derived payload is a no-op, which is what lets the
    safety profile's shape validator re-derive and refuse a hand-edited
    artifact whose fields no longer match its own declared owner."""

    once = _derived(_de250())
    assert _derived(once) == once


# --- the slope condition, and the margin computed downstream from it ---------


def test_the_published_slope_survives_and_the_requirement_is_computed() -> None:
    """Decision 9, literally: the DECLARATION carries what the manufacturer
    printed; the commissioning margin is computed here, named, and never
    written back into the datasheet field."""

    derived = _derived(_de250())
    assert (
        derived["recommended_highpass_slope_db_per_octave"]
        == DE250_PUBLISHED_SLOPE_DB_PER_OCTAVE
    )
    assert (
        _highpass(derived)["minimum_slope_db_per_octave"]
        == PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )


def test_a_published_slope_steeper_than_the_floor_wins() -> None:
    derived = _derived(_de250(recommended_highpass_slope_db_per_octave=36.0))
    assert _highpass(derived)["minimum_slope_db_per_octave"] == 36.0


def test_an_unpublished_slope_is_not_invented_on_the_declaration() -> None:
    """BMS's 4590 prints a recommended crossover with no slope qualifier at
    all. The declaration must say so rather than wear a fabricated condition;
    the derived REQUIREMENT still lands on the commissioning floor."""

    driver = _de250()
    driver.pop("recommended_highpass_slope_db_per_octave")
    limit = resolve_driver_low_limit(
        driver, role="tweeter", driver_style="compression_driver"
    )
    assert limit is not None
    assert limit.slope_db_per_octave is None
    assert limit.protection_slope_db_per_octave == PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE

    derived = _derived(driver)
    assert "recommended_highpass_slope_db_per_octave" not in derived
    assert (
        _highpass(derived)["minimum_slope_db_per_octave"]
        == PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )


# --- backwards compatibility: never loosen an already-deployed box ----------


def test_a_legacy_declaration_infers_the_owner_from_its_stored_highpass() -> None:
    """A draft or profile written before the owner existed still loads. jts3's
    stored shape carries BOTH 1600 and 2000 for one fact; the inference takes
    the protective number, so the box gets STRICTER, never looser, and the
    operator re-enters the published 1600 deliberately."""

    legacy = {
        "role": "tweeter",
        "hard_excitation_band_hz": [1600.0, 20000.0],
        "measurement_band_hz": [2000.0, 18000.0],
        "required_protection_filters": [
            {
                "kind": "highpass",
                "cutoff_hz": 2000.0,
                "minimum_slope_db_per_octave": 24.0,
                "family_or_equivalent": "equivalent_or_steeper",
            }
        ],
    }
    limit = resolve_driver_low_limit(
        legacy, role="tweeter", driver_style="compression_driver"
    )
    assert limit is not None
    assert limit.provenance == LOW_LIMIT_LEGACY_PROTECTION_FILTER
    assert limit.frequency_hz == 2000.0
    assert _derived(legacy)["hard_excitation_band_hz"][0] == 2000.0


def test_an_internally_consistent_legacy_declaration_is_a_no_op() -> None:
    """The backwards-compatibility promise, stated as a test: a stored artifact
    whose three low-limit numbers already agree derives to itself, so deploying
    this change does not un-confirm every profile in the field -- only the
    drifted ones."""

    consistent = {
        "role": "tweeter",
        "hard_excitation_band_hz": [5000.0, 20000.0],
        "measurement_band_hz": [5000.0, 20000.0],
        "required_protection_filters": [
            {
                "kind": "highpass",
                "cutoff_hz": 5000.0,
                "minimum_slope_db_per_octave": 24.0,
                "family_or_equivalent": "equivalent_or_steeper",
            }
        ],
    }
    derived = apply_driver_low_limit(consistent, role="tweeter", driver_style=None)
    # Every value the stored artifact ALREADY carried is unchanged. That is the
    # property the safety profile's canonical re-derive depends on, and it is
    # what keeps this change from un-confirming the field.
    for field, value in consistent.items():
        assert derived[field] == value, field
    # The derivation does write the OWNER back, so a pre-owner artifact gains
    # the one field it was missing. That is the collapse, not a drift: the
    # value is read straight out of the artifact's own stored high-pass.
    assert derived["recommended_highpass_hz"] == 5000.0


def test_do_not_test_below_hz_is_retired_rather_than_derived() -> None:
    """It was an optional SECOND declaration of this same line, and its only
    consumer was a crossover-preview blocker.

    Collapsed onto the owner it would have fired on exactly the condition
    #2491 routes deliberately -- preview DISCLOSES a corner below the declared
    floor, ``path_safety`` REFUSES it at load -- and blocking at preview makes
    that load gate unreachable. What replaces it is strictly more reliable:
    the disclosure and the gate both read one always-derived number, where the
    blocker only fired when a separate optional field happened to be declared
    (#2132's fail-open).
    """

    stale = _de250(do_not_test_below_hz=1200.0)
    derived = _derived(stale)
    # Not written...
    assert derived["do_not_test_below_hz"] == 1200.0
    # ...and not read: the value that governs is the owner, which no longer
    # has to agree with this leftover key for the design to be graded right.
    assert _highpass(derived)["cutoff_hz"] == DE250_LOW_LIMIT_HZ
    assert declared_protection_highpass_floor_hz(derived) == DE250_LOW_LIMIT_HZ


def test_a_declared_owner_outranks_a_stale_stored_highpass() -> None:
    """The collapse itself: once the published figure is entered, the stale
    second declaration stops being consulted."""

    stale = _de250(
        required_protection_filters=[
            {
                "kind": "highpass",
                "cutoff_hz": 2000.0,
                "minimum_slope_db_per_octave": 24.0,
                "family_or_equivalent": "equivalent_or_steeper",
            }
        ],
    )
    assert _highpass(_derived(stale))["cutoff_hz"] == DE250_LOW_LIMIT_HZ


def test_a_declared_lowpass_requirement_is_preserved() -> None:
    """A mid's upper limit is a different fact with a different owner, and the
    low-limit projection must not eat it."""

    mid = {
        "role": "mid",
        "recommended_highpass_hz": 500.0,
        "hard_excitation_band_hz": [500.0, 6000.0],
        "measurement_band_hz": [500.0, 5000.0],
        "required_protection_filters": [
            {
                "kind": "lowpass",
                "cutoff_hz": 3000.0,
                "minimum_slope_db_per_octave": 24.0,
                "family_or_equivalent": "equivalent_or_steeper",
            }
        ],
    }
    derived = apply_driver_low_limit(mid, role="mid", driver_style=None)
    kinds = {item["kind"]: item for item in derived["required_protection_filters"]}
    assert kinds["lowpass"]["cutoff_hz"] == 3000.0
    assert kinds["highpass"]["cutoff_hz"] == 500.0


# --- the style table's new job: default, anchor, never a veto ---------------


def test_the_style_default_answers_an_absent_owner() -> None:
    """"Absent" is a legitimate research answer, so an absent owner is an
    ordinary path, not an exceptional one -- and the answer is labelled a code
    default, never a datasheet fact."""

    absent = {
        "role": "tweeter",
        "hard_excitation_band_hz": [3000.0, 20000.0],
        "measurement_band_hz": [3000.0, 20000.0],
    }
    limit = resolve_driver_low_limit(
        absent, role="tweeter", driver_style="dome_tweeter"
    )
    assert limit is not None
    assert limit.provenance == LOW_LIMIT_STYLE_DEFAULT
    assert limit.frequency_hz == 3000.0
    assert limit.slope_db_per_octave is None


def test_a_code_default_may_unblock_but_never_refuses() -> None:
    """The never-nanny boundary (2026-08-14), kept intact under the new ruling.

    The stamped high-pass is what ``declared_protection_highpass_floor_hz``
    reads into the preset clamp and the ``path_safety`` load gate. Inventing
    one where the operator declared nothing would refuse a design the household
    chose, on a number this module made up -- so a ``style_default`` limit is
    resolvable (above) but never stamped.
    """

    absent = {
        "role": "tweeter",
        "hard_excitation_band_hz": [3000.0, 20000.0],
        "measurement_band_hz": [3000.0, 20000.0],
    }
    derived = apply_driver_low_limit(absent, role="tweeter", driver_style="dome_tweeter")
    assert derived == absent
    assert declared_protection_highpass_floor_hz(derived) is None


def test_a_sourced_figure_below_the_style_default_wins() -> None:
    """The headline reversal: the class default is no longer a veto over
    manufacturer data. 1600 Hz sits below the 2000 Hz compression-driver
    default and is believed, because B&C published it."""

    anchor = driver_protection_profile(
        "tweeter", driver_style="compression_driver"
    ).min_highpass_hz
    assert anchor is not None and DE250_LOW_LIMIT_HZ < anchor
    assert driver_low_limit_plausible(
        DE250_LOW_LIMIT_HZ, role="tweeter", driver_style="compression_driver"
    )
    assert _highpass(_derived(_de250()))["cutoff_hz"] == DE250_LOW_LIMIT_HZ


def test_the_plausibility_band_is_anchored_on_the_style_default() -> None:
    anchor = driver_protection_profile(
        "tweeter", driver_style="compression_driver"
    ).min_highpass_hz
    assert anchor is not None
    band = driver_low_limit_plausibility_band_hz(
        "tweeter", driver_style="compression_driver"
    )
    assert band == (
        anchor / LOW_LIMIT_PLAUSIBILITY_FACTOR,
        anchor * LOW_LIMIT_PLAUSIBILITY_FACTOR,
    )


@pytest.mark.parametrize(
    "frequency_hz, plausible",
    [
        (499.0, False),   # just outside the low edge
        (500.0, True),    # ON the low edge -- inclusive
        (800.0, True),    # a real large-format compression-driver datasheet
        (1600.0, True),   # the DE250
        (8000.0, True),   # ON the high edge -- inclusive
        (8001.0, False),  # just outside the high edge
        (200.0, False),   # the mission's worked garbage case
    ],
)
def test_plausibility_refuses_only_garbage(frequency_hz: float, plausible: bool) -> None:
    """Both edges, inclusive, and a real 800 Hz datasheet inside them. The band
    is a garbage catcher (a transposed digit, a woofer's number pasted into a
    tweeter row), never a judgement about whether a published number is wise."""

    assert (
        driver_low_limit_plausible(
            frequency_hz, role="tweeter", driver_style="compression_driver"
        )
        is plausible
    )


def test_a_role_with_no_style_anchor_has_no_plausibility_bound() -> None:
    """Every low-frequency role. Inventing a bound we cannot justify is the
    nanny behaviour the ruling excludes."""

    assert driver_low_limit_plausibility_band_hz("woofer") is None
    assert driver_low_limit_plausible(12.0, role="woofer") is True


def test_a_woofer_with_nothing_declared_has_no_low_limit() -> None:
    woofer = {
        "role": "woofer",
        "hard_excitation_band_hz": [25.0, 5000.0],
        "measurement_band_hz": [35.0, 4500.0],
        "required_protection_filters": [
            {
                "kind": "lowpass",
                "cutoff_hz": 3000.0,
                "minimum_slope_db_per_octave": 24.0,
                "family_or_equivalent": "equivalent_or_steeper",
            }
        ],
    }
    assert resolve_driver_low_limit(woofer, role="woofer") is None
    assert apply_driver_low_limit(woofer, role="woofer") == woofer


# --- broken declarations are named, never stamped into an invalid shape -----


def test_a_low_limit_above_its_own_band_ceiling_leaves_the_band_alone() -> None:
    """Stamping would produce an inverted range that normalisation refuses with
    an unrelated message. The existing band-relationship vocabulary is what
    should name this declaration, so the projection steps back."""

    broken = _de250(
        recommended_highpass_hz=25000.0,
        hard_excitation_band_hz=[1600.0, 20000.0],
        measurement_band_hz=[1000.0, 18000.0],
    )
    derived = _derived(broken)
    assert derived["hard_excitation_band_hz"] == [1600.0, 20000.0]
    assert derived["measurement_band_hz"] == [1000.0, 18000.0]
