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


#: Every era whose number IS a measured charge for the emitted chain. A new one
#: joins this tuple AND the browser's array in the same change — that pairing is
#: the whole contract.
#:
#: **``realized_peak`` is in this set on purpose, and the question is live.**
#: ``crossover_v2_status_block`` reads ``state["candidate"]`` straight out of the
#: persisted journey state and hands it to the envelope — nothing rebuilds the
#: summary on a page open — so a candidate stamped by a pre-#2758 build really
#: does render its own number after the deploy. It renders PLAINLY, and that is
#: the right answer at this magnitude: the two peak eras disagree by <= 0.019 dB
#: across the emission goldens and <= 0.3101 dB across the migration corpus at
#: its seed, against the ~5.6x the retired sum-of-positives rule was loose by —
#: which is the gap "measured a way JTS no longer uses" was written for. Making
#: every pre-deploy candidate wear that sentence over a few tenths of a dB would
#: spend the household's trust in the caveat on a case it does not fit.
#:
#: The class where ``realized_peak`` IS badly wrong — a cascade peaking in the
#: old grid's hole — is not left to this sentence at all: that graph stops
#: proving at the runtime contract, the deploy blocks on it, and the household
#: never plays it. Disclosure is not the safety net there.
#:
#: **This does not contradict the republish path passing ``unknown``**, which is
#: a different question with a different answer. There the artifact records NO
#: era, and ``unknown`` is what an absence honestly means — not a verdict that
#: ``realized_peak`` is untrustworthy. One says "this number is a measured
#: charge and close enough to print"; the other says "nothing recorded what
#: measured this number".
MEASURED_ERAS = (
    HEADROOM_COST_BASIS_REALIZED_PEAK,
    HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
)


def test_unknown_is_not_a_measured_era_on_either_side():
    """``unknown`` is the ABSENCE of a recorded era, and it is what the
    republish path stamps on a candidate read off disk. Admitting it to the
    measured set would print a pre-#2758 number as a current figure — quietly,
    since the number itself looks ordinary."""
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
