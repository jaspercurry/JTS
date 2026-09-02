# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bass-timing adapter for the shared timing-locked null walk.

The adapter declares the bass-management scope.
:mod:`jasper.audio_measurement.delay_graph` is what validates the declared
scope today.
"""

from __future__ import annotations

from jasper.audio_measurement.null_walk import DelayWalkScope

SUB_MAINS_DELAY_WALK_SCOPE: DelayWalkScope = "bass_management"
