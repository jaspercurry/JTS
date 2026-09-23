# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Format timing status without importing the NumPy-backed analysis stack."""

from collections.abc import Mapping
from typing import Any

from jasper.audio_measurement.timing_verification import TIMING_NOT_COMPARABLE
from jasper.json_fields import finite_float


def _number(value: Any) -> str | None:
    number = finite_float(value)
    return f"{round(number, 2):g}" if number is not None else None


def timing_status_lines(
    applied_profile: Mapping[str, Any] | None,
    speaker_round: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The saved record, then the speaker round packet's verdict and ``next_action`` as it states them."""
    round_ = speaker_round or {}
    action = round_.get("next_action") if isinstance(round_.get("next_action"), Mapping) else None
    timing = (applied_profile or {}).get("timing")
    if not isinstance(timing, Mapping):
        return {"saved": "", "verification": "", "next_action": action}
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
    verdict = round_.get("alignment_verdict")
    verification = (verdict.get("verification") if isinstance(verdict, Mapping) else None) or {}
    residual = _number(verification.get("residual_rms_db"))
    noise = _number(verification.get("repeat_noise_db"))
    verified = ""
    if verification.get("status") == TIMING_NOT_COMPARABLE:
        reasons = "; ".join(f"{code.replace('_', ' ')}: {', '.join(values)}"
                            for code, values in (verification.get("reasons") or {}).items())
        verified = f"Saved timing is not comparable with today's sum ({reasons})"
    elif residual is not None and noise is not None:
        verified = f"Saved timing explains today's sum to within {residual} dB; repeat noise {noise} dB"
    if verified:
        if action and action.get("label"):
            verified += f"; {action['label']}"
        verified += "."
    return {"saved": "; ".join(saved) + ".", "verification": verified, "next_action": action}
