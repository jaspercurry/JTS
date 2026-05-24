"""Regression tests for the SYSTEM_INSTRUCTION's Unclear Audio section.

Background (2026-05-24): the VAD test matrix surfaced a dangerous failure
mode where empty or one-word STT transcripts ("What?", "That's...", "")
still caused the model to confidently call tools — including in one case
`home_assistant("turn on the bedroom lights")` which actually executed
and turned the lights on while the user was asking about weather. The
existing Unclear Audio rule said "don't call any tool" but the model was
interpreting "unclear" too narrowly. The fix enumerates specific triggers
(fragments, empty-string arguments) per the prompting playbook's
"enumerate triggers; conditional rules over absolutes" guidance.

See docs/HANDOFF-vad-experiments.md "Known product bug" + the rationale
block above the Unclear Audio section in voice_daemon.py.
"""


def test_unclear_audio_section_present():
    from jasper.voice_daemon import _build_system_instruction
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
    from jasper.voice_daemon import _build_system_instruction
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
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(location="")
    assert "empty-string arguments" in prompt
