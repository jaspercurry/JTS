# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The request-time doors that describe a HANDOFF between two branches.

A speaker with no crossover region can open neither — see ADR-0212.
"""

from __future__ import annotations

from typing import Any, Callable

from .alignment_prescription import (
    ALIGNMENT_NO_CROSSOVER_REGION,
    alignment_prescription_response_format,
)
from .topology_prescription import (
    TOPOLOGY_NO_CROSSOVER_REGION,
    topology_prescription_response_format,
)

__all__ = ["request_time_prescriptions"]


def request_time_prescriptions(
    no_crossover: bool,
    absence: Callable[[str, bool, str], dict[str, Any]],
) -> dict[str, Any]:
    """The two doors that open at session request time, or why they do not."""
    if not no_crossover:
        return {
            "alignment": alignment_prescription_response_format(),
            "topology": topology_prescription_response_format(),
        }
    return {
        door: {
            "available": False,
            **absence(refusal, False, f"request_time_prescriptions.{door}"),
        }
        for door, refusal in (
            ("alignment", ALIGNMENT_NO_CROSSOVER_REGION),
            ("topology", TOPOLOGY_NO_CROSSOVER_REGION),
        )
    }
