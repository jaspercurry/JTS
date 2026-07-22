# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bass-owner excitation-limits derivation for the bench runner.

The runner does **not** reimplement admission. It composes the existing
two-boundary chain: this module maps one operator-authored
:class:`~jasper.bass_extension.bench.manifest.StimulusRequest` onto the existing
``RequestedDriverExcitationPlan`` and hands it to the existing
``prepare_driver_excitation_plan`` (``jasper.active_speaker.excitation_safety_plan``),
which binds the current driver-safety profile and returns admitted limits or a
typed refusal. A sustain hold that exceeds
``level_duration_limits.max_sweep_duration_s`` is a **correct** admission
refusal the runner surfaces — never split to sneak past the limit.

The generator's ``band`` carries the stimulus band (a sweep's endpoints or a
band-limited-noise band); its amplitude is derived from the requested effective
peak relative to the commanded main volume. This module adds no new
hardware-safety number — every bound comes from the driver-safety profile.
"""

from __future__ import annotations

from jasper.active_speaker.excitation_safety_plan import (
    DriverSweepGeneratorPlan,
    PreparedDriverExcitationPlan,
    RequestedDriverExcitationPlan,
    prepare_driver_excitation_plan,
)

from .manifest import StimulusRequest


def _amplitude_from_peak(effective_peak_dbfs: float, main_volume_db: float) -> float:
    """Solve amplitude from the effective-peak ledger (commissioning gain 0).

    ``effective_peak_dbfs = 20*log10(amplitude) + main_volume_db`` with the
    commissioning gain fixed at 0 dB, clamped into the generator's ``(0, 1]``.
    """

    amplitude_db = effective_peak_dbfs - main_volume_db
    amplitude = 10.0 ** (amplitude_db / 20.0)
    if not 0.0 < amplitude <= 1.0:
        raise ValueError(
            "requested effective peak is not representable at unity full scale "
            "for the commanded main volume"
        )
    return amplitude


def build_requested_bass_plan(
    *,
    target_fingerprint: str,
    commissioning_context_fingerprint: str,
    request: StimulusRequest,
) -> RequestedDriverExcitationPlan:
    """Map one manifest stimulus request onto the existing requested plan."""

    generator = DriverSweepGeneratorPlan(
        f1_hz=request.requested_stimulus_band_hz[0],
        f2_hz=request.requested_stimulus_band_hz[1],
        amplitude=_amplitude_from_peak(
            request.requested_stimulus_effective_peak_dbfs,
            request.requested_commanded_main_volume_db,
        ),
        duration_s=request.requested_hold_duration_s,
        repeat_count=request.requested_repeat_count,
        commissioning_gain_db=0.0,
        main_volume_db=request.requested_commanded_main_volume_db,
    )
    return RequestedDriverExcitationPlan(
        target_fingerprint=target_fingerprint,
        commissioning_context_fingerprint=commissioning_context_fingerprint,
        generator=generator,
    )


__all__ = [
    "DriverSweepGeneratorPlan",
    "PreparedDriverExcitationPlan",
    "RequestedDriverExcitationPlan",
    "build_requested_bass_plan",
    "prepare_driver_excitation_plan",
]
