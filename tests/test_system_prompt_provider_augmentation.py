# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for per-provider SYSTEM_INSTRUCTION augmentation.

The voice prompt is a shared base + a thin per-provider delta (NOT
separate prompts). OpenAI and Grok use the base verbatim (Grok is
OpenAI-Realtime-compatible); only Gemini gets an augmentation today.

These pin the *contract* — base verbatim for OpenAI/Grok (so tuning the
Gemini delta can never silently regress the live OpenAI/Grok prompt) and
additive-only for Gemini — not the Gemini delta's exact wording, which is
iterated via voice-eval. See docs/HANDOFF-prompting.md "Provider deltas"
and the rationale block in jasper/voice/prompt.py.
"""


def _build(**kw):
    from jasper.voice_daemon import _build_system_instruction
    return _build_system_instruction(location="Sunset Park, Brooklyn", **kw)


def test_openai_and_grok_use_base_verbatim():
    """OpenAI/Grok get NO augmentation — byte-identical to the no-provider
    base. The safety pin: tuning the Gemini delta cannot regress the live
    OpenAI/Grok prompt because they don't share the delta."""
    base = _build()
    assert _build(provider="openai") == base
    assert _build(provider="grok") == base


def test_unknown_or_empty_provider_uses_base_verbatim():
    """Unset / unknown providers degrade gracefully to the shared base."""
    base = _build()
    assert _build(provider="") == base
    assert _build(provider="bogus-provider") == base


def test_gemini_augmentation_is_additive():
    """Gemini's variant is the full base with a non-empty delta appended —
    nothing in the base is removed or reordered, and the delta is
    Gemini-only (does not leak into the base)."""
    base = _build()
    gemini = _build(provider="gemini")
    assert gemini.startswith(base), "Gemini delta must be appended; base kept intact"
    delta = gemini[len(base):]
    assert delta.strip(), "Gemini must receive a non-empty augmentation"
    assert delta not in base, "augmentation must be Gemini-only, not in the base"


def test_gemini_augmentation_preserves_load_bearing_base_rules():
    """Additive, not a rewrite: load-bearing base rules survive verbatim in
    the Gemini variant. Guards against a future delta that rewrites the base
    instead of appending to it."""
    gemini = _build(provider="gemini")
    assert "Sorry, I didn't catch that" in gemini   # unclear-audio clarification
    assert "don't call any tool" in gemini           # unclear-audio tool guard
    assert "speak it verbatim" in gemini             # error-field meta-rule
