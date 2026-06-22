# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the documented JTS tool upstream-failure contract.

The contract (docs/HANDOFF-prompting.md "The upstream-failure
contract"): on a hard upstream failure a tool returns
``{error: <speakable string>}``; SYSTEM_INSTRUCTION tells the model
to speak that ``error`` ~verbatim, so the base expectation is that
``error`` is itself the spoken sentence. A tool MAY add a friendlier
``spoken_error`` (``get_weather`` does), but must NEVER return an
empty success payload on a hard failure — that reads as a real
answer and produces a confident-wrong reply (the bus-tool bug).

This is a documented convention, NOT a framework-enforced contract
(``build_tool`` does not validate return shapes). These tests pin the
coupling between the doc's claims and the code/prompt they cite, so
the doc can't silently drift from the SYSTEM_INSTRUCTION rule or the
build_tool pointer it depends on.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTING_DOC = ROOT / "docs" / "HANDOFF-prompting.md"


def test_system_instruction_speaks_error_field_verbatim():
    """The doc's "base expectation: `error` is itself speakable" claim
    is grounded only if SYSTEM_INSTRUCTION still tells the model to
    speak the `error` field verbatim. If that meta-rule is reworded or
    removed, the documented contract is stale — fail here so the doc
    and prompt are updated together."""
    from jasper.voice_daemon import _build_system_instruction

    prompt = _build_system_instruction(location="")
    assert "`error` field" in prompt
    assert "verbatim" in prompt
    # The confirm sibling rule the cookbook also references.
    assert "`confirm` field" in prompt


def test_prompting_doc_states_the_failure_contract():
    """The convention must live where the next tool author looks (the
    prompting playbook), and must spell out the three load-bearing
    pieces: error is speakable, spoken_error is the optional extra, and
    an empty success payload on a hard failure is the bug to avoid."""
    # Collapse line wraps so multi-word phrases match regardless of
    # where markdown soft-wraps them.
    text = " ".join(PROMPTING_DOC.read_text(encoding="utf-8").split())
    assert "upstream-failure contract" in text
    # error is the speakable base expectation.
    assert "{error:" in text
    # spoken_error is documented as the optional friendlier line.
    assert "spoken_error" in text
    # The anti-pattern (the bus bug) is named, not just implied.
    assert "empty" in text.lower()
    assert "bus-tool bug" in text


def test_prompting_doc_marks_contract_as_convention_not_framework():
    """The task is explicit: do NOT propose a base class / framework.
    The doc must state the contract is a documented convention, not
    enforced by build_tool, so a future reader doesn't go build one."""
    # Collapse line wraps so the assertion matches the prose regardless
    # of where markdown soft-wraps it.
    text = " ".join(PROMPTING_DOC.read_text(encoding="utf-8").split())
    assert "documented convention" in text
    assert "not a framework-enforced contract" in text


def test_build_tool_docstring_points_at_the_contract():
    """build_tool deliberately does not validate return shapes; its
    docstring is the inline pointer that sends a tool author to the
    documented contract instead of guessing a failure shape."""
    from jasper.tools import build_tool

    doc = (build_tool.__doc__ or "")
    assert "upstream-failure contract" in doc
    assert "HANDOFF-prompting.md" in doc
