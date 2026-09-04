# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language contract for the /sound/ research echo-back panel (#2195).

The panel ("3. What JTS is running with") is the only surface in the product
that reads a research reply's **raw** ``field_provenance`` and shows it to a
household as authority.  Everywhere else, provenance reaches an operator only
after ``_profile_core`` has checked, per field, that the visible value still
equals the researched one and downgraded the entry to ``confidence: "unknown"``
when it does not.  The panel does not run that check -- it renders straight
after a paste, before anything is saved, so there is no confirmed profile to
read from yet.

That shortcut is safe today because of two invariants, and **neither is
enforced by the code that depends on them**:

1. A v1 packet is stripped of provenance before it can reach the page, so only
   a v2 packet's assertions are ever rendered.
2. Every key the panel echoes is inside ``_V2_RESEARCH_COMPARABLE_FIELDS``,
   which ``_validate_v2_research_prefill`` compares field-by-field against the
   visible values.  A bound v2 packet therefore cannot be attached to a draft
   whose visible value for an echoed key has drifted from the reply's.

Invariant 2 is what this module pins.  The failure it prevents is silent and
ugly: echo a key the staleness comparison does not cover, and the panel can
show a researcher's citation and ``confirmed`` badge next to a number the
operator typed -- the exact mislabelling the panel exists to end, arriving with
no error anywhere.  A drift on either side (a new echoed field, or a field
dropped from the comparison set) is a code change nobody would connect to this
page, which is why the tripwire lives here rather than in a comment.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path

SOUND_PROFILE_MAIN_JS = Path("deploy/assets/sound-profile/js/main.js")

_ECHO_FIELDS_FUNCTION = "function driverEchoBackFields() {"


def _echo_back_field_body() -> str:
    """Return the source of ``driverEchoBackFields``, brace-matched.

    Deliberately loud rather than lenient: if the function is renamed, moved,
    or reshaped, every assertion below would otherwise silently pass over an
    empty set and this file would guard nothing.
    """

    source = SOUND_PROFILE_MAIN_JS.read_text(encoding="utf-8")
    start = source.find(_ECHO_FIELDS_FUNCTION)
    assert start != -1, (
        f"{SOUND_PROFILE_MAIN_JS} no longer declares `{_ECHO_FIELDS_FUNCTION}`. "
        "If the echo-back field list moved or was renamed, point this contract "
        "at its new home -- do not delete it: it is the only guard that the "
        "panel's raw-provenance shortcut stays inside the server's "
        "divergence check."
    )
    open_brace = source.index("{", start + len(_ECHO_FIELDS_FUNCTION) - 1)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : index + 1]
    raise AssertionError(
        f"unbalanced braces reading driverEchoBackFields from {SOUND_PROFILE_MAIN_JS}"
    )


def _echoed_field_keys() -> set[str]:
    """The ``key:`` of every entry the panel renders a row for."""

    body = _echo_back_field_body()
    keys = re.findall(r"\bkey:\s*'([a-z0-9_]+)'", body)
    labels = re.findall(r"\blabel:\s*'", body)
    assert keys, (
        "found no `key: '...'` entries in driverEchoBackFields; the entry shape "
        "changed and this extraction needs updating"
    )
    assert len(keys) == len(labels), (
        "driverEchoBackFields entries no longer pair one key with one label "
        f"({len(keys)} keys, {len(labels)} labels); the extraction below is "
        "reading a shape it was not written for"
    )
    assert len(set(keys)) == len(keys), f"duplicate echoed key in {keys}"
    return set(keys)


def _frozen_safety_field_names() -> set[str]:
    """The fields ``_profile_core`` freezes into the confirmed safety profile.

    Read out of the function rather than re-typed here, so this cannot drift
    into asserting yesterday's list.
    """

    from jasper.active_speaker.driver_safety import _profile_core

    tree = ast.parse(textwrap.dedent(inspect.getsource(_profile_core)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "safety_field_names" not in targets:
            continue
        assert isinstance(node.value, ast.Tuple), (
            "safety_field_names is no longer a tuple literal; this reader needs "
            "updating"
        )
        return {
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    raise AssertionError(
        "_profile_core no longer assigns `safety_field_names`; the frozen-field "
        "list moved and this contract must follow it"
    )


def test_echoed_provenance_keys_stay_inside_the_server_divergence_check() -> None:
    """Every echoed key is one ``_validate_v2_research_prefill`` compares.

    See this module's docstring for why that containment is the whole safety
    argument for reading raw reply provenance on this page.  This is a subset
    assertion in one direction only: the comparison set may legitimately be
    wider than the panel, but the panel may never step outside it.
    """

    from jasper.active_speaker.design_draft import _V2_RESEARCH_COMPARABLE_FIELDS

    echoed = _echoed_field_keys()
    uncovered = echoed - set(_V2_RESEARCH_COMPARABLE_FIELDS)
    assert not uncovered, (
        f"{sorted(uncovered)} are echoed on /sound/ with the research reply's "
        "own provenance, but _validate_v2_research_prefill does not compare "
        "them against the visible values. A bound packet could then attribute "
        "a researcher's citation to a number the operator typed. Either add "
        "the field to _V2_RESEARCH_COMPARABLE_FIELDS or stop echoing it."
    )


def test_echo_back_renders_exactly_what_its_headline_claims() -> None:
    """The panel's completeness claim is the union of two server-owned sets.

    The headline reads: "Every value the research reply gave us that JTS asked
    it to source, or that gets frozen into this speaker's safety limits."  That
    sentence is only true while the rendered set equals
    ``_PROMPT_PROVENANCE_KEYS | safety_field_names`` exactly.

    It is pinned because the panel already shipped one over-claim: the first
    cut said "every value the research reply gave us" while three frozen fields
    (``measurement_band_hz``, ``crossover_search_band_hz``, ``cabinet``) went
    unechoed.  A confirmation gate whose whole purpose is accuracy is the last
    place a stale sentence should be able to hide, and the next field added to
    either set would recreate exactly that gap.
    """

    from jasper.active_speaker.driver_safety_prompt import _PROMPT_PROVENANCE_KEYS

    claimed = set(_PROMPT_PROVENANCE_KEYS) | _frozen_safety_field_names()
    echoed = _echoed_field_keys()

    assert echoed == claimed, (
        "the /sound/ echo-back panel no longer renders exactly what its "
        f"headline claims.\n  missing rows: {sorted(claimed - echoed)}\n"
        f"  rows beyond the claim: {sorted(echoed - claimed)}\n"
        "Add the row, or reword the headline in renderDriverEchoBack so the "
        "sentence is true of what the operator actually sees."
    )
