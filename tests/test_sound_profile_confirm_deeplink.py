# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #1820 defect 3 — the ``/sound/`` confirm control must not stay buried.

An unconfirmed driver-safety profile refuses EVERY crossover measurement, and
"Confirm safety limits" is the only control that clears it. #1819 demoted that
button from a top-level primary action into a default-closed ``<details>``
Advanced disclosure, while promoting the enclosure selector — one of the fields
whose edit rotates the profile fingerprint and so clears the confirmation —
into the always-visible form. The invalidating input ended up more discoverable
than its only remedy, which is how the 2026-07-28 JTS3 session dead-ended.

These are static tripwires over the shipped ES module, in the same spirit as
``tests/test_web_wizard_conventions.py``: the browser behaviour itself needs a
device pass, but the structural facts the fix depends on — the hoisted control
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


def test_the_confirm_control_is_hoisted_out_of_the_advanced_disclosure():
    """The structural claim: the hoisted callout is emitted by the component
    card BEFORE the ``<details>`` Advanced editor opens, so it renders at top
    level rather than behind a closed disclosure."""

    source = _source()
    card = source[source.index("function renderDriverResearchCard("):]
    card = card[: card.index("\n  function ", 1)]

    hoisted = card.index("renderDriverSafetyConfirmCallout(topology)")
    advanced = card.index("driver-research__advanced-editor")
    assert hoisted < advanced, (
        "the confirm callout must render before the Advanced disclosure"
    )

    # It is a primary action, not another ghost button inside a disclosure.
    callout = source[source.index("function renderDriverSafetyConfirmCallout("):]
    callout = callout[: callout.index("\n  // Deep link")]
    assert "btn btn--primary" in callout
    assert 'data-act="confirm-driver-safety"' in callout


def test_the_hoisted_control_is_gated_on_the_servers_own_permission():
    """The server already advertises
    ``permissions.may_confirm_visible_driver_safety_profile``; the page must
    read it rather than inventing its own rule about who may confirm."""

    source = _source()
    state = source[source.index("function driverSafetyConfirmState("):]
    state = state[: state.index("\n  function driverSafetyConfirmHint(")]
    assert "may_confirm_visible_driver_safety_profile === true" in state
    # And on the real evaluation, not the profile's own self-reported status
    # alone (a stale profile reports "confirmed" until evaluated against the
    # live topology).
    assert "driver_safety_profile_evaluation" in state
    assert "confirmed_and_current === true" in state


def test_the_callout_never_offers_a_confirm_that_the_server_would_refuse():
    """``build_driver_safety_profile`` REFUSES a confirm while the profile has
    derived issues (status ``incomplete``). Offering the button there would be
    an error waiting to happen, so the callout drops it and names the
    add-the-missing-values action instead."""

    source = _source()
    state = source[source.index("function driverSafetyConfirmState("):]
    state = state[: state.index("\n  function driverSafetyConfirmHint(")]
    assert "status !== 'incomplete'" in state


def test_the_deeplink_opens_the_owning_step_before_scrolling():
    """A bare fragment is not enough — the control lives inside a collapsible
    step card that is only open when it is the current step."""

    source = _source()
    fn = source[source.index("function applyConfirmSafetyDeepLink("):]
    fn = fn[: fn.index("\n  function renderDriverResearchCard(")]
    assert "outputStepOverride = 'research';" in fn
    assert "render();" in fn
    assert "scrollIntoView" in fn
    # No-ops when there is nothing to confirm: a stale bookmark must not yank
    # an unrelated page into the component step.
    assert "needsConfirmation" in fn

    # And it is actually wired into both boot paths.
    assert source.count(
        "refreshOutputTopology({silent: true}).then(applyConfirmSafetyDeepLink);"
    ) == 2


def test_the_callout_has_a_style_rule():
    """A hoisted callout with no rule renders as an unstyled section — the same
    "technically present, practically invisible" failure this fixes."""

    assert re.search(
        r"^\s*\.driver-research__confirm\s*\{", SOUND_CSS.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
