# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Known headroom eras and the candidate data projection."""
from __future__ import annotations


from jasper.active_speaker.linearization_fit import (
    HEADROOM_COST_BASIS_REALIZED_PEAK,
    HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    HEADROOM_COST_BASIS_UNKNOWN,
)


MEASURED_ERAS = (
    HEADROOM_COST_BASIS_REALIZED_PEAK,
    HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
)


def test_unknown_is_not_a_measured_era_on_either_side():
    """Unknown means no era was recorded."""
    assert HEADROOM_COST_BASIS_UNKNOWN not in MEASURED_ERAS


def test_the_reader_and_this_guard_agree_about_what_is_known():
    """The server-side reader has its own copy of the same question, so it is
    pinned to this one rather than left to agree by inspection."""
    from jasper.active_speaker.crossover_envelope_v2 import _headroom_cost_payload

    for era in MEASURED_ERAS:
        payload = _headroom_cost_payload(
            {"headroom_cost_db": 5.2, "headroom_cost_basis": era}
        )
        assert payload["basis"] == era
    assert _headroom_cost_payload(
        {"headroom_cost_db": 5.2, "headroom_cost_basis": "not_an_era"}
    )["basis"] == HEADROOM_COST_BASIS_UNKNOWN
