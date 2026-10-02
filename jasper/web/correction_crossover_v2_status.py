# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The web adapter over the v2 status projection.

The derivations live in
:mod:`jasper.active_speaker.crossover_envelope_v2`'s status-projection
section, which may not import this layer. This module supplies the answers
only the web host holds — the loaded state and the volume plan — and shapes
what comes back into ``status["crossover_v2"]``.

"""

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume

from typing import Any

from jasper.active_speaker import crossover_envelope_v2 as _projection


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block: the fields the envelope reads.

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
        "phase": _projection.crossover_v2_phase(state, review_declined=False),
        "failure": (state or {}).get("failure"),
        "needs_recovery": needs_recovery,
    }
