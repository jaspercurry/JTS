# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ``status["crossover_v2"]`` block the envelope reads."""

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume

from typing import Any


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The stored run failure, and whether the session volume needs recovery.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    state = v2state.load_v2_state()
    try:
        needs_recovery = bool(v2volume.session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    return {
        "failure": (state or {}).get("failure"),
        "needs_recovery": needs_recovery,
    }
