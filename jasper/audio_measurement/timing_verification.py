# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Saved timing verification policy."""

from typing import Any, Mapping

from jasper.json_fields import finite_float

# 0.5 dB fallback: the predictor gives 0.0104 dB RMS for a 10 us error on an ideal 2.5 kHz LR4, so no room-independent floor can be derived.
TIMING_RESIDUAL_FLOOR_DB = 0.5


def timing_next_action(
    timing: Mapping[str, Any], *, measured: bool = False, needs_measurement: bool = False,
) -> dict[str, str] | None:
    verification = timing.get("verification") or {}
    residual, noise = (finite_float(verification.get(key)) for key in ("residual_rms_db", "repeat_noise_db"))
    if timing.get("saved") is not None:
        if (residual is not None and noise is not None
                and residual > 3 * noise and residual > TIMING_RESIDUAL_FLOOR_DB):
            return {"id": "reset_timing", "label": "the saved timing no longer explains the sum: re-measure timing (reset timing)"}
    elif measured:
        return {"id": "apply_timing", "label": "timing measured; apply a document to save it"}
    elif needs_measurement:
        return {"id": "measure_timing", "label": "measure timing again: louder, quieter room"}
    return None
