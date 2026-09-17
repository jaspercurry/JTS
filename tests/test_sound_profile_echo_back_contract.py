# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The research echo panel covers the fields its heading names."""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from ._web_test_helpers import sound_page_js

_ECHO_FIELDS_FUNCTION = "function driverEchoBackFields() {"


def _echo_back_field_body() -> str:
    """Return the source of ``driverEchoBackFields``, brace-matched.

    Deliberately loud rather than lenient: if the function is renamed, moved,
    or reshaped, every assertion below would otherwise silently pass over an
    empty set and this file would guard nothing.
    """

    source = sound_page_js()
    start = source.find(_ECHO_FIELDS_FUNCTION)
    assert start != -1, f"Missing echo field list: {_ECHO_FIELDS_FUNCTION}"
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
        "unbalanced braces reading driverEchoBackFields from the /sound/ page JS"
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
    """The fields ``compute_driver_safety_profile`` computes from the driver declaration.

    Read out of the function rather than re-typed here, so this cannot drift
    into asserting yesterday's list.
    """

    from jasper.active_speaker.driver_safety import compute_driver_safety_profile

    tree = ast.parse(textwrap.dedent(inspect.getsource(compute_driver_safety_profile)))
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
        "compute_driver_safety_profile no longer assigns `safety_field_names`; the frozen-field "
        "list moved and this contract must follow it"
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
