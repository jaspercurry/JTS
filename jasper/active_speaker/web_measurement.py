# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Saved active-crossover measurement evidence and targets."""

from __future__ import annotations

import time
from typing import Any

from jasper.active_speaker.measurement import (
    active_driver_targets,
    active_summed_targets,
    load_measurement_state,
)
from jasper.output_topology import load_output_topology


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def status_payload() -> dict[str, Any]:
    """Return active-crossover targets plus saved measurement evidence."""

    topology = load_output_topology()
    measurements = load_measurement_state(topology)
    return {
        "ok": True,
        "generated_at": _utc_now(),
        "topology": {
            "topology_id": topology.topology_id,
            "status": topology.status,
        },
        "targets": {
            "drivers": active_driver_targets(topology),
            "summed": active_summed_targets(topology),
        },
        "measurements": measurements,
    }
