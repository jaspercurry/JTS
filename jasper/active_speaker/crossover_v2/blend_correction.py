"""The blend correction's shared bounds, and the reader for a persisted one.

A blend correction is at most :data:`BLEND_MAX_FILTERS` RBJ Peaking CUTS,
emitted PRE-SPLIT on the stereo bus (``camilla_yaml._emit_baseline_pipeline``):
one ``B(f)`` on every role scales the sum and leaves the inter-driver complex
ratio untouched, so the correction is common-mode by construction. Canonical
contract: ``docs/active-speaker-tuning-layers-design.md``, decision 10.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "BLEND_FILTER_Q",
    "BLEND_MAX_FILTERS",
    "BLEND_MAX_FILTER_CUT_DB",
    "BLEND_MIN_CUT_DB",
    "blend_filters_from_mapping",
]


# --------------------------------------------------------------------------- #
# bounds — every one derived, none chosen by taste
# --------------------------------------------------------------------------- #

#: How many Peaking cuts one blend correction may carry. Two, because the
#: evidence here does not support fine sculpting: one mono sweep per position,
#: an honesty mask that removes bins inside the very window being corrected, and
#: a null detector that is uncalibrated in this band (#2600 item 1).
BLEND_MAX_FILTERS = 2

#: Q of a blend cut. A deliberate tightening against the fit engine's own
#: ``Q ≤ 8`` peaking ceiling, and 2.0 is the Q every peaking filter the
#: series-1 fits emitted actually used, so the shape is one the loop has
#: already realized on hardware. A cut wider than its defect over-corrects the
#: shoulders, which is the skirt damage both prescribed rounds were rolled back
#: on.
BLEND_FILTER_Q = 2.0

#: Per-filter cut ceiling, dB — a deterministic solver's emission bound, and
#: nothing else's: a PRESCRIBED cut has no depth ceiling (ADR-0207). Derived
#: rather than chosen: the woofer's acknowledged ``measured_excess_db`` inside
#: the blind zone was 2.09–2.26 dB across series-1 rounds r1/r2/r4
#: (1291.4–2077.2 Hz), and this model's measured tracking error on jts3 is
#: 0.5 dB, so 2.26 + 0.5 = 2.76, rounded to 3.0.
BLEND_MAX_FILTER_CUT_DB = 3.0

#: The smallest cut worth emitting, dB — this model's own measured tracking
#: error, the same floor ``crossover_v2_flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB``
#: holds. A correction smaller than the gap between what the model predicts and
#: what the hardware realizes cannot be honestly claimed.
BLEND_MIN_CUT_DB = 0.5


# --------------------------------------------------------------------------- #
# reading a persisted correction back
# --------------------------------------------------------------------------- #


def blend_filters_from_mapping(raw: Any) -> tuple[dict[str, Any], ...] | None:
    """Normalize a persisted blend-correction list, or ``None`` if unreadable.

    The reader for the incumbent. "The incumbent is empty" and "the incumbent
    cannot be read" are different facts with different consequences — the first
    is a normal first round, the second is unknown — so an absent/``None`` input
    is NOT the same as ``[]`` and callers must keep them apart.

    Cuts-only is re-checked here rather than assumed, because this is where data
    that left the process comes back into it. A positive gain means the record is
    not one this module wrote: unreadable, not clampable.
    """

    if raw is None:
        return None
    if isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        return None
    if not isinstance(raw, Sequence):
        return None
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            return None
        if entry.get("biquad_type") != "Peaking":
            return None
        raw = (entry.get("freq"), entry.get("q"), entry.get("gain"))
        # Real numbers, NOT anything ``float()`` will coerce: this system writes
        # floats, so a ``"1900"`` is by definition a record something else wrote.
        # ``bool`` is excluded because it is an ``int`` subclass and
        # ``gain=True`` would read as a +1 dB boost.
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in raw
        ):
            return None
        freq, q, gain = (float(value) for value in raw)
        if not (math.isfinite(freq) and math.isfinite(q) and math.isfinite(gain)):
            return None
        if freq <= 0.0 or q <= 0.0 or gain > 0.0:
            return None
        out.append({"biquad_type": "Peaking", "freq": freq, "q": q, "gain": gain})
    if len(out) > BLEND_MAX_FILTERS:
        return None
    return tuple(out)
