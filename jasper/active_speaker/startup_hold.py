# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ephemeral marker: a protected startup-load session is holding the anchor.

``load_protected_startup_config`` writes the all-muted **staged startup anchor**
to the durable CamillaDSP statefile and then kicks
``jasper-audio-hardware-reconcile`` so outputd re-reads the active lane before
the next commissioning tone. That reconcile re-runs the graph selector
(:func:`jasper.active_speaker.runtime_contract.safe_graph_for_current_topology`).
On an ALREADY-commissioned box the selector would otherwise RESTORE the saved
approved baseline over the anchor — which is the right thing at a normal boot,
but during a re-commission it drifts the durable statefile off the anchor, and
``commission-load``'s persist phase then fails closed ("statefile drifted").

This marker is how the selector tells the two apart. While it is present, the
selector preserves the staged startup anchor instead of restoring the baseline
(see the deadlock-guard rung in ``safe_graph_for_current_topology``). It is
**ephemeral** — a plain file under ``/run`` — on purpose: a NORMAL boot starts
with an empty ``/run``, so it is never present then and the baseline-restore
rung fires exactly as before. A commissioned box always comes back to audio on
reboot; only a live, in-flight startup-load session sees the hold. This mirrors
``jasper.control.measurement_hold``'s "nothing persisted; a reboot drops it =
intended crash-safety" philosophy.

Both the writer (the startup-load path, in the root ``jasper-correction-web``
daemon) and the reader (the selector, run as root by the reconciler) are root,
so no group permissions are involved.

Fail direction, both sides: a write failure is best-effort and never turns a
successful startup load into a failure; a read failure resolves to "no hold",
which restores the saved baseline — audio, never silence, and never louder.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

STARTUP_HOLD_MARKER_ENV = "JASPER_ACTIVE_SPEAKER_STARTUP_HOLD_MARKER"
DEFAULT_STARTUP_HOLD_MARKER = Path("/run/jasper-active-speaker/staged-startup-hold")

__all__ = [
    "STARTUP_HOLD_MARKER_ENV",
    "DEFAULT_STARTUP_HOLD_MARKER",
    "startup_hold_marker_path",
    "staged_startup_hold_active",
    "hold_staged_startup",
    "release_staged_startup_hold",
]


def startup_hold_marker_path(path: str | Path | None = None) -> Path:
    """Resolve the hold marker path (explicit arg > env override > default)."""

    return Path(
        path
        or os.environ.get(STARTUP_HOLD_MARKER_ENV)
        or DEFAULT_STARTUP_HOLD_MARKER
    )


def staged_startup_hold_active(path: str | Path | None = None) -> bool:
    """True while a protected startup-load session holds the staged anchor.

    Fail-safe: any read error resolves to ``False`` (no hold), so the selector
    restores the saved baseline rather than silently preserving silence.
    """

    try:
        return startup_hold_marker_path(path).exists()
    except OSError:
        return False


def hold_staged_startup(path: str | Path | None = None) -> bool:
    """Mark the staged startup anchor as held. Best-effort; never raises.

    Returns whether the marker is now present.
    """

    marker = startup_hold_marker_path(path)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    except OSError as exc:
        logger.warning(
            "event=active_speaker.staged_startup_hold_write_failed path=%s error=%s",
            marker,
            type(exc).__name__,
        )
        return False


def release_staged_startup_hold(path: str | Path | None = None) -> bool:
    """Clear the staged-startup hold. Best-effort; never raises.

    Returns whether the marker is now absent.
    """

    marker = startup_hold_marker_path(path)
    try:
        marker.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning(
            "event=active_speaker.staged_startup_hold_clear_failed path=%s error=%s",
            marker,
            type(exc).__name__,
        )
        return False
