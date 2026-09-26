# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A rigid piston's radiation limits from its declared diameter and the
microphone's distance (ADR-0366). No numpy: the measurement registry reads the
near-field distance on the web's numpy-free import path."""

from __future__ import annotations

import math

from .null_walk import DEFAULT_SOUND_SPEED_M_S

#: Farthest a pose at one driver sits from its dust cap while the room reads
#: about 40 dB down (ADR-0360).
NEAR_FIELD_MAX_DISTANCE_M = 0.1

# ka at which a circular piston is taken to be BEAMING outright, named by
# #1675's owner ruling, disclosure only (ADR-0011). ka=2 is roughly -6 dB at
# 45 deg off-axis (checked in
# docs/research/2026-07-23-driver-linearization/03-fact-check.md claim L).
BEAMING_KA = 2.0


def at_driver_near_field(driver: str, distance_m: float | None) -> bool:
    """Whether a pose names one driver and sits within :data:`NEAR_FIELD_MAX_DISTANCE_M` of it."""
    return bool(driver) and distance_m is not None and distance_m <= NEAR_FIELD_MAX_DISTANCE_M


def beaming_onset_hz(radiating_diameter_mm: float, *, ka: float = BEAMING_KA) -> float:
    """Frequency at which a piston of this diameter reaches ``ka``. ``f = ka*c / (2*pi*a)``;
    JTS3 woofer's 114 mm diameter gives 957.7 Hz at ka=1. GEOMETRY, not DSP-fixable
    (#1675). Non-positive input raises.
    """
    if not math.isfinite(radiating_diameter_mm) or radiating_diameter_mm <= 0.0:
        raise ValueError(
            f"radiating diameter must be positive (got {radiating_diameter_mm})"
        )
    if not math.isfinite(ka) or ka <= 0.0:
        raise ValueError(f"ka must be positive (got {ka})")
    radius_m = float(radiating_diameter_mm) / 2000.0
    return ka * DEFAULT_SOUND_SPEED_M_S / (2.0 * math.pi * radius_m)


def far_field_ceiling_hz(
    diameter_m: float,
    distance_m: float,
    *,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> float:
    """Highest frequency at which ``distance_m`` is still the driver's far field. Rayleigh
    distance ``2*a**2/lambda`` GROWS with frequency, so solving for ``f`` gives a
    CEILING: near-field at HIGH frequencies, never low ones.
    """
    radius = 0.5 * float(diameter_m)
    if radius <= 0.0:
        raise ValueError(f"diameter must be positive, got {diameter_m}")
    return float(sound_speed_m_s) * float(distance_m) / (2.0 * radius**2)
