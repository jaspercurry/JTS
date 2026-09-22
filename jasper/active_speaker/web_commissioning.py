# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared startup anchors and commission status."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from jasper.active_speaker.commission_ramp import (
    load_ramp_state,
)
from jasper.active_speaker.safe_playback import (
    load_safe_playback_state,
)
from jasper.active_speaker.staging import (
    DEFAULT_CAMILLA_CONFIG_DIR as DEFAULT_CAMILLA_CONFIG_DIR,
)
from jasper.active_speaker.commission_load import (
    load_commission_load_state,
)
from jasper.camilla import CamillaUnavailable

_COMMISSION_OPERATION_ERRORS = (
    CamillaUnavailable,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
)


async def attempt_graph_restore(
    restore: Callable[[], Awaitable[Any]],
) -> tuple[bool, str | None]:
    """Run one graph restore and never raise: ``(took_effect, raise_message)``.

    The one verdict the swap transaction reaches, here and on
    ``program_playback``'s measurement path: it TOOK, it RAISED (message
    present), or CamillaDSP REJECTED it (``False``, no message). Both failures
    are returned rather than collapsed to a bool because they are different
    failures at the same call site — #2198 is what an absent distinction costs.
    Callers own the consequence, which is the half that legitimately differs:
    a restore inside a ``finally`` reports, one inside an ``except`` raises.
    """
    try:
        restored = await restore()
    except _COMMISSION_OPERATION_ERRORS as exc:
        return False, str(exc)
    return restored is True, None


def commission_status_payload() -> dict[str, Any]:
    """Return the active-speaker operator measurement state."""

    return {
        "commission_load": load_commission_load_state(),
        "ramp": load_ramp_state(),
        "safe_playback": load_safe_playback_state(),
    }
