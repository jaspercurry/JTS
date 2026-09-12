# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Level samples and shared measurement step helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# The digital-full-scale hard ceiling: main_volume must never exceed this,
# independent of the dynamic cap. Mirrors camilla.py::_coerce_main_volume_db,
# duplicated here as defense-in-depth. Do not raise.
HARD_CEILING_DBFS = 0.0
SPL_CEILING_EXCEEDED = "spl_ceiling_exceeded"
# Bounds one step's overshoot on a non-linear chain: 75 + 6 stays below the 85 stop.
MAX_STEP_DB = 6.0
CEILING_MARGIN_DB = 3.0


def capped_gap_step_db(
    *, measured_db: float, target_db: float, cap_db: float = math.inf
) -> float:
    """How far one measured level step moves the level: the remaining gap.

    The one climb policy in the tree. Every step re-measures, so the policy
    needs the chain to be only LOCALLY monotone in dB, never globally linear.
    ``cap_db`` saturates the step UPWARD only -- downward motion reduces risk,
    the same asymmetry :mod:`jasper.active_speaker.calibration_level` states for
    its ``upward_step_limit_db``. Returns the step in dB, to be ADDED to the
    current commanded level; the caller still clamps against its own ceiling.
    """
    return min(float(target_db) - float(measured_db), float(cap_db))

# The exception set the ramp treats as recoverable-by-restore. A broad-but-named
# tuple rather than a blind ``except Exception`` (lint contract: no new BLE001
# suppressions): it covers every realistic failure of the injected callables
# while letting CancelledError / SystemExit / MemoryError propagate.
RECOVERABLE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
    ArithmeticError,
)


@dataclass(frozen=True)
class LevelSample:
    """One phone-reported mic-level sample.

    Batched, client-timestamped sample arrays ride the last-write-wins
    ``event`` slot, so the Pi's ~0.75 s poll never decimates the series.
    ``rms_dbfs`` / ``peak_dbfs`` are computed on the phone the same way the Pi's
    ``quality._dbfs`` computes them; ``clip`` marks a full-scale sample
    (immediate abort). ``agc_frozen`` is the phone's realized
    ``autoGainControl:false`` state, and ``False`` means the browser either
    reported AGC on or never reports the setting at all (every WebKit build).
    ``agc_unattested`` disambiguates those two: ``True`` means the browser could
    not attest either way, so the sample needs empirical verification before it
    is trusted as a gain-map reference; ``False`` means AGC was affirmatively
    reported on, so the level must never be a gain-map reference.
    An unattested chain is never encoded as bare ``agc_frozen=True``, so an older
    Pi falls back to "never trust" instead of trusting an unproven chain.
    """

    seq: int
    t_client_ms: int
    rms_dbfs: float
    peak_dbfs: float
    clip: bool = False
    agc_frozen: bool = True
    agc_unattested: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LevelSample:
        """Parse one sample from an untrusted phone payload.

        Strict on the numeric fields: a non-finite ``rms_dbfs`` / ``peak_dbfs``
        (JSON ``"NaN"`` / ``"Infinity"`` strings parse fine through ``float()``)
        raises ``ValueError``, so NaN can never reach the gain map.
        """
        rms = float(data["rms_dbfs"])
        peak = float(data.get("peak_dbfs", rms))
        if not (math.isfinite(rms) and math.isfinite(peak)):
            raise ValueError(f"non-finite level sample: rms={rms!r} peak={peak!r}")
        return cls(
            seq=int(data.get("seq", 0)),
            t_client_ms=int(data.get("t_client_ms", 0)),
            rms_dbfs=rms,
            peak_dbfs=peak,
            clip=bool(data.get("clip", False)),
            agc_frozen=bool(data.get("agc_frozen", True)),
            agc_unattested=bool(data.get("agc_unattested", False)),
        )
