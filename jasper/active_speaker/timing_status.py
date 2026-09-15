# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator lines for saved timing and its latest verification."""

from collections.abc import Mapping
from typing import Any

from jasper.json_fields import finite_float


def _number(value: Any) -> str | None:
    number = finite_float(value)
    return f"{number:g}" if number is not None else None


def timing_status_lines(
    applied_profile: Mapping[str, Any] | None,
    speaker_round: Mapping[str, Any] | None,
) -> dict[str, str]:
    timing = (applied_profile or {}).get("timing")
    if not isinstance(timing, Mapping):
        return {"saved": "", "verification": ""}
    delay = _number(timing.get("delay_us"))
    saved = [f"Saved timing: delay {delay} µs" if delay is not None else "Saved timing",
             f"polarity {timing.get('polarity')}", f"provenance {timing.get('provenance')}"]
    measured = timing.get("measured") if timing.get("provenance") == "measured" else None
    if isinstance(measured, Mapping):
        margin = _number(measured.get("margin_db"))
        spread_db = _number(measured.get("repeat_spread_db"))
        spread_us = _number(measured.get("repeat_spread_us"))
        if margin is not None:
            saved.append(f"margin {margin} dB")
        if spread_db is not None and spread_us is not None:
            saved.append(f"repeat spread {spread_db} dB / {spread_us} µs")
    round_ = speaker_round or {}
    verdict = round_.get("alignment_verdict")
    verification = verdict.get("verification") if isinstance(verdict, Mapping) else None
    residual = _number(verification.get("residual_rms_db")) if isinstance(verification, Mapping) else None
    noise = _number(verification.get("repeat_noise_db")) if isinstance(verification, Mapping) else None
    verified = ""
    if residual is not None and noise is not None:
        verified = f"Saved timing explains today's sum to within {residual} dB; repeat noise {noise} dB"
        action = round_.get("next_action")
        if isinstance(action, Mapping) and action.get("label"):
            verified += f"; {action['label']}"
        verified += "."
    return {"saved": "; ".join(saved) + ".", "verification": verified}
