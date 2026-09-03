# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The stats identity every AEC-bridge suite stamps its `_BridgeStats` with."""
from __future__ import annotations

from jasper.cli.aec_bridge_engines import FRAME_SAMPLES, SAMPLE_RATE
from jasper.cli.aec_bridge_telemetry import StatsIdentity

IDENTITY = StatsIdentity(
    sample_rate_hz=SAMPLE_RATE,
    frame_samples=FRAME_SAMPLES,
    reference_source="outputd_udp",
    reference_endpoint="127.0.0.1:9891",
)
