# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the SYSTEM_INSTRUCTION's Unclear Audio section
(jasper/voice/prompt.py), which enumerates fragment and empty-argument
triggers rather than relying on a single "don't call any tool" rule.
"""


def test_unclear_audio_section_present():
    from jasper.voice.prompt import _build_system_instruction
    prompt = _build_system_instruction(location="")
    # The section header concept (clarification request) must exist.
    # We don't pin literal wording too tightly — phrasing may evolve —
    # but the user-visible clarification line and the "no tool" rule
    # must both be present.
    assert "Sorry, I didn't catch that" in prompt
    assert "don't call any tool" in prompt


def test_unclear_audio_lists_fragment_trigger():
    """The model needs the explicit 'fragment' trigger; without it, the
    model perceives 'transcript came back, it's short' as 'audio was
    clear and the user said exactly that one word' and then hallucinates
    a tool call. Pinning these literal example fragments because they
    are the exact ones observed in production failures."""
    from jasper.voice.prompt import _build_system_instruction
    prompt = _build_system_instruction(location="")
    assert "fragment" in prompt.lower()
    assert "What?" in prompt
    assert "That's" in prompt


def test_unclear_audio_lists_empty_args_antipattern():
    """The model is observed calling tools with empty-string args
    (`{'direction': '', 'line': ''}`, `{'station_label': ''}`) as a
    hallucination signature. Including this anti-pattern in the prompt
    lets the model self-detect: 'I'm about to pass empty strings — I
    must be guessing.'"""
    from jasper.voice.prompt import _build_system_instruction
    prompt = _build_system_instruction(location="")
    assert "empty-string arguments" in prompt
