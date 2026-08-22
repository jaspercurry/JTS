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
