# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The web adapter over the v2 status projection.

The derivations live in
:mod:`jasper.active_speaker.crossover_envelope_v2`'s status-projection
section, which may not import this layer. This module supplies the answers
only the web host holds — the loaded state, the volume plan, the applied
record — and shapes what comes back into ``status["crossover_v2"]``.

"""

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume

from typing import Any

from jasper.active_speaker import crossover_envelope_v2 as _projection
from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    state = v2state.load_v2_state()
    session_id = (state or {}).get("session_id")
    try:
        needs_recovery = bool(v2volume.session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    identity = applied_identity(load_applied_baseline_profile_state())
    return {
        "phase": _projection.crossover_v2_phase(state, review_declined=False),
        # save_v2_state stamps transitions; polls must not create a second clock (#1947).
        "updated_at": (state or {}).get("updated_at"),
        "applied": bool((state or {}).get("applied")),
        "candidate": (state or {}).get("candidate"),
        "accepted_sound_revision": (state or {}).get("accepted_sound_revision"),
        # The coordinator owns the ordinal and adoption receipt (#2537, #2602).
        "round_receipt": (state or {}).get("round_receipt"),
        "execution": (state or {}).get("execution"),
        "failure": (state or {}).get("failure"),
        "needs_recovery": needs_recovery,
        "applied_identity": identity,
        "session_id": session_id,
    }
