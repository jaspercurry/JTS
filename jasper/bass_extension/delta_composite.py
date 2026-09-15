# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The 70 Hz slope-12 proof model, not an assertion about the native shelf corner."""

from __future__ import annotations

import math
from collections.abc import Sequence

from jasper.camilla_config_contract import DEFAULT_SAMPLE_RATE, FilterSpec, SHELF_Q
from jasper.sound.profile import _filter_response_complex

PROOF_SHELF_HZ = 70.0
# In this noiseless transfer model, |1 + delta| >= |1| gives exactly 0 dB;
# there is no measured uncertainty from which to infer an acoustic allowance.
COMPOSITE_DIP_TOLERANCE_DB = 0.0


def composite_response(
    frequencies: Sequence[float], *, boost_db: float, highpass_hz: float | None,
    lowpass_hz: float, lowpass_order: int = 2,
) -> list[complex]:
    shelf = _filter_response_complex(FilterSpec("proof", "Lowshelf", PROOF_SHELF_HZ, boost_db), frequencies)
    highpass = (_filter_response_complex(FilterSpec("delta", "Highpass", highpass_hz, 0.0, SHELF_Q), frequencies)
                if highpass_hz is not None else [1.0] * len(frequencies))
    if lowpass_order == 1:
        # Bilinear transform of 1/(1+s), with the same digital corner prewarp.
        corner = math.tan(math.pi * lowpass_hz / DEFAULT_SAMPLE_RATE)
        lowpass = [1.0 / (1.0 + 1j * math.tan(math.pi * f / DEFAULT_SAMPLE_RATE) / corner)
                   for f in frequencies]
    elif lowpass_order == 2:
        lowpass = _filter_response_complex(FilterSpec("delta", "Lowpass", lowpass_hz, 0.0, SHELF_Q), frequencies)
    else:
        raise ValueError("lowpass_order must be 1 or 2")
    return [1.0 + (h - 1.0) * hp * lp for h, hp, lp in zip(shelf, highpass, lowpass, strict=True)]


def modeled_composite_dip(**parameters: float | int | None) -> dict[str, float]:
    """Locate the deepest modeled dip across the audio band, including sub-bass."""
    frequencies = [0.01 * (DEFAULT_SAMPLE_RATE / 2.0 / 0.01) ** (i / 1024) for i in range(1025)]
    response = composite_response(frequencies, **parameters)
    index = min(range(len(response)), key=lambda i: abs(response[i]))
    lo, hi = frequencies[max(0, index - 1)], frequencies[min(len(frequencies) - 1, index + 1)]
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    for _ in range(48):
        left, right = hi - ratio * (hi - lo), lo + ratio * (hi - lo)
        values = composite_response([left, right], **parameters)
        if abs(values[0]) < abs(values[1]):
            hi = right
        else:
            lo = left
    frequency = (lo + hi) / 2.0
    magnitude = abs(composite_response([frequency], **parameters)[0])
    return {"modeled_dip_db": max(0.0, -20.0 * math.log10(max(magnitude, math.ulp(0.0)))),
            "modeled_dip_hz": frequency}
