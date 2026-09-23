# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What survives of the pure-planner suite, over the banked 2026-08-10 jts3 inputs.

The planner itself is deleted; these pin the contract it was built on — one
candidate corner, refused as a type when its sections disagree — and the σ
composition's named refusal for an unregistered mic tier.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2.contracts import (
    CandidateAcousticContext,
)
from tests.crossover_v2_fixtures import _candidate_sections
from tests.test_crossover_v2_incident_replay import (
    CONFIGURED_FC_HZ,
    SELECTED_FC_HZ,
    _conductor,
    _response,
)


def _sections_at(fc_hz: float) -> dict[str, tuple[Any, ...]]:
    """The session preset's sections, re-cornered — ``_candidate_sections``."""

    return _candidate_sections(_conductor(), fc_hz)


# the corner cannot be smuggled in


def test_a_context_cannot_be_built_from_sections_naming_two_corners():
    """The 2026-08-10 shape, refused at construction rather than planned.

    Not a message check: the refusal is a *type*, so a caller classifies on the
    exception class rather than on its wording. (The classifier of record was
    the Phase-1 ``planner_facade``, deleted in #2291 Phase 5c-iii; the property
    this test pins is the contract's, not that consumer's, which is why it
    outlived it.)
    """
    from jasper.active_speaker.crossover_v2.contracts import (
        CandidateFcDisagreementError,
    )

    mixed = {
        "woofer": _sections_at(CONFIGURED_FC_HZ)["woofer"],
        "tweeter": _sections_at(SELECTED_FC_HZ)["tweeter"],
    }
    with pytest.raises(CandidateFcDisagreementError) as caught:
        CandidateAcousticContext.from_sections(mixed)
    assert caught.value.refusal_reason == "candidate_fc_disagreement"


def test_the_request_refuses_a_context_whose_corner_disagrees_with_its_sections():
    """A hand-built context cannot re-open the hole ``from_sections`` closes."""

    from jasper.active_speaker.crossover_v2.contracts import (
        CandidateFcDisagreementError,
    )

    sections = _sections_at(SELECTED_FC_HZ)
    with pytest.raises(CandidateFcDisagreementError):
        CandidateAcousticContext(fc_hz=CONFIGURED_FC_HZ, sections_by_role=sections)


def test_an_unregistered_mic_tier_is_a_named_refusal_not_a_bare_key_error():
    """A caller error that reads as one, instead of as malformed planner output."""

    own = replace(_response("woofer"))
    with pytest.raises(iv.PlannerInputError, match="unknown mic tier"):
        iv.compose_sigma_db(
            own, own, tier="studio", valid_band_hz=(150.0, 4000.0)
        )
