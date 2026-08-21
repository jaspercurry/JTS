# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #1820 defect 3 — the ``/sound/`` safety-limits explanation must not
stay buried.

The separate "Confirm safety limits" ceremony is gone: saving the declaration IS
declaring it, so an ordinary edit can no longer leave a speaker unmeasurable
waiting on a second human click. What survives is the set of states in which the
declaration genuinely cannot be used — ``incomplete``, ``stale``, ``malformed``
— each of which the measurement wizard hard-stops on and deep-links here. The
callout that names WHICH one and what to edit still has to render at top level
rather than behind the default-closed Advanced disclosure, for the same reason
#1819 got this wrong: the invalidating input is in the always-visible form.

These are static tripwires over the shipped ES module, in the same spirit as
``tests/test_web_wizard_conventions.py``: the browser behaviour itself needs a
device pass, but the structural facts the fix depends on — the hoisted callout
exists OUTSIDE the Advanced disclosure, and its DOM id matches the href the
measurement wizard deep-links to — are pinned here so neither half can move
alone.
"""

from __future__ import annotations

import re
from pathlib import Path

from jasper.active_speaker.crossover_v2_flow import (
    REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
    REASON_REGISTRY,
)

MAIN_JS = Path("deploy/assets/sound-profile/js/main.js")
SOUND_CSS = Path("deploy/assets/sound-profile/sound.css")
ANCHOR_ID = "confirm-safety-limits"


def _source() -> str:
    return MAIN_JS.read_text(encoding="utf-8")


def test_the_deeplink_href_and_the_dom_id_agree():
    """The two halves of one link, in two languages. The registry owns the href
    (it renders the hard-stop screen's action); the page owns the id. A rename
    on either side without the other is a dead link the household lands on with
    nothing to do."""

    spec = REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED]
    assert spec.next_action is not None
    href = str(spec.next_action["href"])
    assert href == f"/sound/setup/#{ANCHOR_ID}"

    source = _source()
    assert f"var CONFIRM_SAFETY_ANCHOR_ID = '{ANCHOR_ID}';" in source
    # One literal, in the constant — the renderer and the fragment check both
    # read it, so the id cannot drift from itself inside the page either.
    assert source.count(f"'{ANCHOR_ID}'") == 1
    assert source.count("CONFIRM_SAFETY_ANCHOR_ID") >= 3


def test_the_review_callout_is_hoisted_out_of_the_advanced_disclosure():
    """The structural claim: the hoisted callout is emitted by the component
    card BEFORE the ``<details>`` Advanced editor opens, so it renders at top
    level rather than behind a closed disclosure."""

    source = _source()
    card = source[source.index("function renderDriverResearchCard("):]
    card = card[: card.index("\n  function ", 1)]

    hoisted = card.index("renderDriverSafetyReviewCallout(topology)")
    advanced = card.index("driver-research__advanced-editor")
    assert hoisted < advanced, (
        "the review callout must render before the Advanced disclosure"
    )


def test_no_confirm_control_survives_anywhere_on_the_page():
    """The nanny, pinned out. The confirm action, its request field, and the
    permission the page used to gate it on are all retired — a stray survivor
    would be a dead click that POSTs a field the server now rejects as unknown.
    """

    source = _source()
    assert "confirm-driver-safety" not in source
    assert "confirmSafetyProfile" not in source
    assert "confirm_safety_profile" not in source
    assert "may_confirm_visible_driver_safety_profile" not in source


def test_the_callout_reads_the_evaluation_not_the_profiles_self_report():
    """A stale profile reports ``confirmed`` in its own stored status until it is
    evaluated against the LIVE topology, so the page has to read the server's
    verdict rather than the artifact's self-description."""

    source = _source()
    state = source[source.index("function driverSafetyReviewState("):]
    state = state[: state.index("\n  function driverSafetyReviewHint(")]
    assert "driver_safety_profile_evaluation" in state
    assert "confirmed_and_current !== true" in state
    # 'missing' stays out: no active crossover pair means no declaration to
    # review, and the callout would be noise.
    assert "status !== 'missing'" in state


def test_the_deeplink_opens_the_owning_step_before_scrolling():
    """A bare fragment is not enough — the callout lives inside a collapsible
    step card that is only open when it is the current step."""

    source = _source()
    fn = source[source.index("function applySafetyLimitsDeepLink("):]
    fn = fn[: fn.index("\n  function renderDriverResearchCard(")]
    assert "outputStepOverride = 'research';" in fn
    assert "render();" in fn
    assert "scrollIntoView" in fn
    # No-ops when there is nothing to review: a stale bookmark must not yank an
    # unrelated page into the component step.
    assert "needsReview" in fn

    # And it is actually wired into both boot paths.
    assert source.count(
        "refreshOutputTopology({silent: true}).then(applySafetyLimitsDeepLink);"
    ) == 2


def test_the_callout_has_a_style_rule():
    """A hoisted callout with no rule renders as an unstyled section — the same
    "technically present, practically invisible" failure this fixes."""

    assert re.search(
        r"^\s*\.driver-research__confirm\s*\{", SOUND_CSS.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
