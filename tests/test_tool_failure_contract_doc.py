# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the documented JTS tool upstream-failure contract.

The contract: on a hard upstream failure a tool returns
``{error: <speakable string>}``; SYSTEM_INSTRUCTION tells the model
to speak that ``error`` ~verbatim, so the base expectation is that
``error`` is itself the spoken sentence. A tool MAY add a friendlier
``spoken_error`` (``get_weather`` does), but must NEVER return an
empty success payload on a hard failure — that reads as a real
answer and produces a confident-wrong reply (the bus-tool bug).

This is a documented convention, NOT a framework-enforced contract
(``build_tool`` does not validate return shapes). This test pins the
SYSTEM_INSTRUCTION rule the convention depends on.
"""
from __future__ import annotations


def test_system_instruction_speaks_error_field_verbatim():
    """The "base expectation: `error` is itself speakable" claim
    is grounded only if SYSTEM_INSTRUCTION still tells the model to
    speak the `error` field verbatim. If that meta-rule is reworded or
    removed, the documented contract is stale."""
    from jasper.voice.prompt import _build_system_instruction

    prompt = _build_system_instruction(location="")
    assert "`error` field" in prompt
    assert "verbatim" in prompt
    # The confirm sibling rule the cookbook also references.
    assert "`confirm` field" in prompt
