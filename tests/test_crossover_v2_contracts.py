# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 1: the crossover-v2 domain contracts and their invariants.

These are construction-time guards, so every one of them is asserted in BOTH
directions — a value that must be refused is refused, and the neighbouring
legal value still constructs. A guard pinned only on the failing side passes
just as happily when the guard is deleted and the type accepts everything.
"""

from __future__ import annotations

import dataclasses

import pytest

from jasper.active_speaker.branch_chain import CrossoverSection
from jasper.active_speaker.crossover_v2.contracts import CandidateFcDisagreementError, SCHEMA_VERSION
from jasper.active_speaker.crossover_v2 import (
    CandidateAcousticContext,
    CrossoverV2ContractError,
    ResponseCurve,
)

FC = 1648.7


def _sections(fc: float = FC) -> dict[str, tuple[CrossoverSection, ...]]:
    return {
        "woofer": (CrossoverSection(fc_hz=fc, order=4, highpass=False),),
        "tweeter": (CrossoverSection(fc_hz=fc, order=4, highpass=True),),
    }


def _context(fc: float = FC) -> CandidateAcousticContext:
    return CandidateAcousticContext(fc_hz=fc, sections_by_role=_sections(fc))


# --------------------------------------------------------------------------
# CandidateAcousticContext — the one Fc owner
# --------------------------------------------------------------------------


def test_a_context_whose_sections_all_name_its_corner_constructs():
    context = _context()
    assert context.fc_hz == FC
    assert context.roles == ("tweeter", "woofer")
    assert set(context.sections_by_role) == {"woofer", "tweeter"}


def test_a_section_cornered_away_from_the_declared_fc_fails_closed():
    """The 2026-08-10 defect, refused at construction.

    The configured session said 2,000 Hz and the candidate's sections said
    1,648.7 Hz. Whichever number the caller believes, a context cannot hold
    both.
    """
    sections = _sections(FC)
    sections["tweeter"] = (CrossoverSection(fc_hz=2000.0, order=4, highpass=True),)
    with pytest.raises(CrossoverV2ContractError, match="disagrees with"):
        CandidateAcousticContext(fc_hz=FC, sections_by_role=sections)


def test_a_declared_fc_away_from_agreeing_sections_fails_closed():
    """The same guard from the other side: the sections agree, the Fc does not."""

    with pytest.raises(CrossoverV2ContractError, match="disagrees with"):
        CandidateAcousticContext(fc_hz=2000.0, sections_by_role=_sections(FC))


def test_agreement_is_exact_and_not_toleranced():
    """A corner one float ulp away is a disagreement, not round-trip noise.

    ``REGION_FC_MATCH_TOLERANCE_HZ`` (1e-6) exists for a corner that has been
    through persisted JSON. These sections are built in-process from one float,
    so any inequality is real — and a tolerance here is precisely how a
    2,000 Hz session corner would eventually be allowed to stand in for a
    1,648.7 Hz candidate one.
    """
    sections = _sections(FC)
    sections["woofer"] = (
        CrossoverSection(fc_hz=FC + 1e-9, order=4, highpass=False),
    )
    with pytest.raises(CrossoverV2ContractError, match="disagrees with"):
        CandidateAcousticContext(fc_hz=FC, sections_by_role=sections)


def test_from_sections_derives_the_corner_when_the_sections_are_unanimous():
    context = CandidateAcousticContext.from_sections(_sections())
    assert context.fc_hz == FC
    assert context == _context()


def test_from_sections_refuses_a_split_corner_set():
    sections = _sections(FC)
    sections["tweeter"] = (CrossoverSection(fc_hz=2000.0, order=4, highpass=True),)
    with pytest.raises(CrossoverV2ContractError, match="different crossover corners"):
        CandidateAcousticContext.from_sections(sections)


def test_a_full_range_role_carries_no_sections_and_that_is_legal():
    """``sections_by_role`` gives a role with no crossover region no sections.

    That role runs full range in the emitted graph — the honest answer, and one
    the context must not turn into a refusal as long as some crossover exists.
    """
    sections = dict(_sections())
    sections["sub"] = ()
    context = CandidateAcousticContext(fc_hz=FC, sections_by_role=sections)
    assert context.sections_by_role["sub"] == ()
    assert context.fc_hz == FC


def test_a_context_with_no_crossover_section_anywhere_is_refused():
    with pytest.raises(CrossoverV2ContractError, match="at least one crossover"):
        CandidateAcousticContext(fc_hz=FC, sections_by_role={"woofer": ()})
    with pytest.raises(CrossoverV2ContractError, match="must not be empty"):
        CandidateAcousticContext(fc_hz=FC, sections_by_role={})


def test_a_non_positive_or_non_finite_corner_is_refused():
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(CrossoverV2ContractError):
            CandidateAcousticContext(fc_hz=bad, sections_by_role=_sections())


def test_the_context_is_immutable_including_its_section_map():
    context = _context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.fc_hz = 2000.0  # type: ignore[misc]
    # The accessor hands out a copy, so a caller cannot re-corner the context
    # it was given.
    context.sections_by_role["woofer"] = ()
    assert context.sections_by_role["woofer"] != ()


def test_the_context_fingerprint_ignores_role_insertion_order():
    forward = CandidateAcousticContext(
        fc_hz=FC,
        sections_by_role={
            "woofer": (CrossoverSection(fc_hz=FC, order=4, highpass=False),),
            "tweeter": (CrossoverSection(fc_hz=FC, order=4, highpass=True),),
        },
    )
    reversed_order = CandidateAcousticContext(
        fc_hz=FC,
        sections_by_role={
            "tweeter": (CrossoverSection(fc_hz=FC, order=4, highpass=True),),
            "woofer": (CrossoverSection(fc_hz=FC, order=4, highpass=False),),
        },
    )
    assert forward.fingerprint == reversed_order.fingerprint


def test_the_context_fingerprint_tracks_the_corner_and_the_section_shape():
    base = _context().fingerprint
    assert _context(fc=1700.0).fingerprint != base
    steeper = _sections()
    steeper["woofer"] = (CrossoverSection(fc_hz=FC, order=8, highpass=False),)
    assert (
        CandidateAcousticContext(fc_hz=FC, sections_by_role=steeper).fingerprint
        != base
    )


# --------------------------------------------------------------------------
# ResponseCurve
# --------------------------------------------------------------------------


def test_a_curve_refuses_non_finite_points_and_ragged_pairs():
    with pytest.raises(CrossoverV2ContractError, match="finite"):
        ResponseCurve([100.0, 200.0], [-1.0, float("nan")])
    with pytest.raises(CrossoverV2ContractError, match="one level per frequency"):
        ResponseCurve([100.0, 200.0], [-1.0])
    with pytest.raises(CrossoverV2ContractError, match="must have points"):
        ResponseCurve([], [])
    # …and the neighbouring legal curve still constructs.
    assert ResponseCurve([100.0, 200.0], [-1.0, -2.0]).db == (-1.0, -2.0)


# --------------------------------------------------------------------------
# the schema version
# --------------------------------------------------------------------------


def test_the_schema_version_is_pinned_to_its_value():
    """The VALUE, not just the constant — which is what makes it a version."""

    assert SCHEMA_VERSION == 3


# --------------------------------------------------------------------------
# the naming rule itself
# --------------------------------------------------------------------------


def test_the_refusal_reason_travels_by_TYPE_not_by_the_exceptions_prose():
    disagreement = CandidateFcDisagreementError("wording that no test owns")
    assert disagreement.refusal_reason == "candidate_fc_disagreement"
    assert CrossoverV2ContractError("anything").refusal_reason == "contract_invalid"
