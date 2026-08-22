# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard for the headroom-era vocabulary across Python and the browser.

``headroom_cost_basis`` says WHICH derivation stamped a correction's disclosed
max-level cost. Python owns the vocabulary
(``linearization_fit.HEADROOM_COST_BASIS_*``); the browser decides from it
whether to print the figure plainly or warn that it was measured a way JTS no
longer uses (``deploy/assets/correction/js/crossover/main.js``). They cannot
share code, and the browser file is served as a static asset, so nothing but
this test connects the two.

**It exists because the drift already happened.** #2758 minted
``realized_peak_full_domain`` and the browser still tested
``basis === 'realized_peak'``, which would have told the household that every
FRESH correction was measured a way JTS no longer uses — the newest number on
the one screen whose purpose is honesty, labelled stale.

The contract is deliberately narrow: every PEAK era Python defines must be in
the browser's measured-set, and the browser must not claim an era Python does
not define. ``unknown`` is excluded on both sides on purpose — it is the value
that MEANS "no era was recorded", and it is what the warning branch is for.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.active_speaker.linearization_fit import (
    HEADROOM_COST_BASIS_REALIZED_PEAK,
    HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    HEADROOM_COST_BASIS_UNKNOWN,
)

ROOT = Path(__file__).resolve().parents[1]
BROWSER_MODULE = ROOT / "deploy/assets/correction/js/crossover/main.js"

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


def _browser_measured_bases() -> list[str]:
    """The literals in the browser's ``MEASURED_HEADROOM_BASES`` array."""
    source = BROWSER_MODULE.read_text(encoding="utf-8")
    match = re.search(
        r"const\s+MEASURED_HEADROOM_BASES\s*=\s*\[(?P<body>[^\]]*)\]", source
    )
    assert match, (
        "the browser no longer declares MEASURED_HEADROOM_BASES; if the era "
        "check moved, move this guard with it rather than deleting it"
    )
    return re.findall(r"'([^']+)'", match.group("body"))


def test_every_measured_era_python_defines_is_one_the_browser_prints_plainly():
    """The direction that produced the live regression: Python mints a new era,
    the browser does not learn it, and a current correction renders as stale."""
    assert set(MEASURED_ERAS) <= set(_browser_measured_bases())


def test_the_browser_claims_no_era_python_does_not_define():
    """The other direction, which would render a retired or misspelled era's
    number as a current figure — the failure that cannot be seen by reading
    either file alone."""
    assert set(_browser_measured_bases()) <= set(MEASURED_ERAS)


def test_unknown_is_not_a_measured_era_on_either_side():
    """``unknown`` is the ABSENCE of a recorded era, and it is what the
    republish path stamps on a candidate read off disk. Admitting it to the
    measured set would print a pre-#2758 number as a current figure — quietly,
    since the number itself looks ordinary."""
    assert HEADROOM_COST_BASIS_UNKNOWN not in MEASURED_ERAS
    assert HEADROOM_COST_BASIS_UNKNOWN not in _browser_measured_bases()


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
