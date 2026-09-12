# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Resolve a local measurement door's microphone and commissioning watch."""
from __future__ import annotations

from typing import Any

from jasper.active_speaker.plan_run import spl_watch
from jasper.audio_measurement.calibration import resolve_mic_sensitivity
from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from jasper.audio_measurement.wired_capture import WiredSplMonitor


def measurement_spl_watch(
    stated: float | None, *, topology: Any, preset: Any, device: Any,
    mic_serial: str | None = None,
) -> tuple[WiredSplMonitor | None, str]:
    sensitivity = (resolve_mic_sensitivity(mic_serial=mic_serial) if mic_serial
                   else resolved_household_sensitivity(device))
    return spl_watch(stated, topology=topology, preset=preset, sensitivity=sensitivity, device=device)
