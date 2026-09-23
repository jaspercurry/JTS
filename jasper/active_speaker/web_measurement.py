# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-crossover measurement targets."""

from __future__ import annotations

from typing import Any

from jasper.active_speaker.measurement import (
    active_driver_targets,
    active_summed_targets,
)
from jasper.json_fields import utc_now_iso as _utc_now
from jasper.output_topology_store import load_output_topology


def status_payload() -> dict[str, Any]:
    """Return active-crossover driver and summed targets."""

    topology = load_output_topology()
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
    }
