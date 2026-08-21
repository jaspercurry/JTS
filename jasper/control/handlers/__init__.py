# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Concern-scoped route mixins for :mod:`jasper.control.server`.

The server factory retains request guards, route tables, and dispatch ordering.
These mixins own only the route bodies behind that stable boundary.
"""

from .aec import AecRoutes
from .grouping import GroupingRoutes
from .measurement import MeasurementRoutes
from .system import SystemRoutes
from .voice import VoiceRoutes
from .volume import VolumeRoutes

__all__ = [
    "AecRoutes",
    "GroupingRoutes",
    "MeasurementRoutes",
    "SystemRoutes",
    "VoiceRoutes",
    "VolumeRoutes",
]
