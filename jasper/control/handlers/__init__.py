# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Concern-scoped route mixins for :mod:`jasper.control.server`.

The server factory retains request guards, route tables, and dispatch ordering.
These mixins own the route bodies behind that stable boundary — grouping,
system, and peering also own module-level singletons, and peering owns its
daemon's lifecycle.
"""

from .aec import AecRoutes
from .grouping import GroupingRoutes
from .measurement import MeasurementRoutes
from .peering import PeeringRoutes
from .system import SystemRoutes
from .voice import VoiceRoutes
from .volume import VolumeRoutes

__all__ = [
    "AecRoutes",
    "GroupingRoutes",
    "MeasurementRoutes",
    "PeeringRoutes",
    "SystemRoutes",
    "VoiceRoutes",
    "VolumeRoutes",
]
