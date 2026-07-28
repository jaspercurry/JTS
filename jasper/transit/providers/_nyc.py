# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure-data constants shared by NYC transit providers."""

from __future__ import annotations

from ..base import BoundingBox


# NYC envelope from Staten Island Ferry terminal up to Wakefield, plus a small
# margin. Coverage is city-level policy shared by subway and bus; each provider
# still performs its own real nearest-stop check inside this coarse rectangle.
NYC_BBOX = BoundingBox(
    lat_min=40.49,
    lat_max=40.92,
    lon_min=-74.26,
    lon_max=-73.69,
)
