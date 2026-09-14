# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The bass experiment's target, relative to the fit's reference band."""

from types import MappingProxyType

TARGET = MappingProxyType({"freqs_hz": (20.0, 60.0), "magnitude_db": (0.0, 0.0)})
TOLERANCE_DB = 3.0


def target_band_hz() -> tuple[float, float]:
    frequencies = TARGET["freqs_hz"]
    return frequencies[0], frequencies[-1]
