# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Prompt package builder for the JTS calibration advisor harness.

This is intentionally provider-neutral. It produces the compact system
instructions, response contract, and redacted context that a future
OpenAI/Anthropic/Gemini adapter can pass to a model. It does not call a
model and it does not execute tools.
"""
from __future__ import annotations

from typing import Any

from . import response

PROMPT_SCHEMA_VERSION = 1
GENERATED_BY = "jasper.calibration_agent.prompt.build_advisor_prompt_package"

_SYSTEM_INSTRUCTIONS = """\
You are the JTS audio tuning advisor inside an open-source smart speaker.
You are not the DSP authority. Deterministic JTS code owns measurement
math, safety gates, CamillaDSP config generation, volume, execution,
rollback, and persistence.

Your job is to explain the evidence, identify what is trustworthy or
suspicious, recommend remeasurement when evidence is weak, and propose
bounded preference-EQ actions only when the provided JTS policy permits
them. Keep room correction, target/house curves, preference EQ, and
active-speaker baseline work separate.

Preference tuning is human-in-the-loop. There is no single subjective
right answer; propose likely improvements as listening auditions for
the user to judge, not as objective truth.

Never request raw audio bytes, reveal secrets, emit CamillaDSP YAML,
emit FIR taps or coefficients, control volume, override confidence
gates, or claim that you have applied a change. If you propose an
audition or saved profile, output only the JSON action shape in the
contract. JTS will validate it and the user must approve persistence.
"""


def build_advisor_prompt_package(
    advisor_context: dict[str, Any],
    *,
    user_message: str | None = None,
) -> dict[str, Any]:
    """Return the versioned package a future model call may consume."""

    return {
        "artifact_schema_version": PROMPT_SCHEMA_VERSION,
        "kind": "jts_advisor_prompt_package",
        "generated_by": GENERATED_BY,
        "privacy": {
            "uses_redacted_advisor_context": True,
            "raw_audio_excluded": True,
            "secrets_excluded": True,
            "no_provider_call_made": True,
        },
        "messages": [
            {
                "role": "system",
                "content": _SYSTEM_INSTRUCTIONS,
            },
            {
                "role": "user",
                "content": (
                    user_message
                    or "Review this JTS measurement evidence and produce only the "
                    "contracted JSON response."
                ),
            },
        ],
        "response_contract": response.response_contract(),
        "advisor_context": advisor_context,
        "side_effects": [],
    }
