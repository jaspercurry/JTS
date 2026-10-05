# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The microphone placement policy a commissioning bundle records.

Per-driver levels are comparable only within the same server-proven microphone
geometry, so a bundle names the placement policy its captures were made under.
"""

from __future__ import annotations

DRIVER_PLACEMENT_POLICY_ID = "driver_same_distance_v1"
