# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Saved timing verification policy: one verdict and the one action it asks for."""

from typing import Any, Iterable, Mapping

from jasper.audio_measurement.evidence_reasons import REASON_GRAPH_MISMATCH, REASON_SNR_SHORT
from jasper.json_fields import finite_float

# 0.5 dB fallback: the predictor gives 0.0104 dB RMS for a 10 us error on an ideal 2.5 kHz LR4, so no room-independent floor can be derived.
TIMING_RESIDUAL_FLOOR_DB = 0.5
TIMING_COMPARABLE = "comparable"
TIMING_NOT_COMPARABLE = "not_comparable"
_MEASURE_TIMING = {"id": "measure_timing", "label": "measure timing again: louder, quieter room"}


def timing_verification(
    residual_rms_db: float | None, repeat_noise_db: float | None, *,
    snr_short: Iterable[str] = (), graph_mismatch: Iterable[str] = (),
) -> dict[str, Any]:
    """The saved pair's residual against today's sum; any reason makes it ``not_comparable`` (#5632 F3)."""
    named = ((REASON_SNR_SHORT, sorted(set(snr_short))), (REASON_GRAPH_MISMATCH, sorted(set(graph_mismatch))))
    reasons = {code: values for code, values in named if values}
    return {"residual_rms_db": residual_rms_db, "repeat_noise_db": repeat_noise_db,
            "residual_floor_db": TIMING_RESIDUAL_FLOOR_DB,
            "status": TIMING_NOT_COMPARABLE if reasons else TIMING_COMPARABLE, "reasons": reasons}


def timing_next_action(
    timing: Mapping[str, Any], *, measured: bool = False, needs_measurement: bool = False,
) -> dict[str, str] | None:
    verification = timing.get("verification") or {}
    residual, noise = (finite_float(verification.get(key)) for key in ("residual_rms_db", "repeat_noise_db"))
    if timing.get("saved") is not None:
        # See ADR-0345
        if REASON_GRAPH_MISMATCH in (verification.get("reasons") or {}):
            return {"id": "remeasure_timing", "label": "measure timing again: the take played a driver the check leaves out"}
        if verification.get("status") == TIMING_NOT_COMPARABLE:
            return dict(_MEASURE_TIMING)
        if (residual is not None and noise is not None
                and residual > 3 * noise and residual > TIMING_RESIDUAL_FLOOR_DB):
            return {"id": "reset_timing", "label": "the saved timing no longer explains the sum: re-measure timing (reset timing)"}
    elif measured:
        return {"id": "apply_timing", "label": "timing measured; apply a document to save it"}
    elif needs_measurement:
        return dict(_MEASURE_TIMING)
    return None
