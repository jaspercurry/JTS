# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ephemeral marker: a protected startup-load session is holding the anchor.

While it is present, ``safe_graph_for_current_topology`` preserves the staged
startup anchor instead of restoring the approved baseline. It lives under
``/run`` on purpose — a normal boot starts with an empty ``/run``, so a
commissioned box always comes back to audio. One TAKE
(``load_protected_startup_config``) and three RELEASEs (that function's
``finally``, ``rollback_protected_startup_config``, and
``baseline_profile.persist_applied_baseline_profile``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from jasper.log_event import log_event

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

    Fail-safe: a read error resolves to ``False``, so the selector restores the
    saved baseline rather than preserving silence.
    """

    try:
        return startup_hold_marker_path(path).exists()
    except OSError:
        return False


def hold_staged_startup(path: str | Path | None = None) -> bool:
    """Mark the staged startup anchor as held; return whether it is now present.

    Never raises. ``load_protected_startup_config`` refuses the load on
    ``False`` rather than applying an anchor the next reconcile would undo.
    """

    marker = startup_hold_marker_path(path)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    except OSError as exc:
        # Presence is the hold, so answer from the marker, not from this call:
        # the root writers (``UMask=0077``) leave it ``root:root`` 0600, which
        # ``jasper-web`` cannot ``touch()`` but can still ``unlink`` (that needs
        # write on the 0755 RuntimeDirectory, not on the file).
        held = False
        try:
            held = marker.exists()
        except OSError:
            held = False
        log_event(
            logger,
            "active_speaker.staged_startup_hold_write_failed",
            level=logging.WARNING,
            path=marker,
            error=type(exc).__name__,
            held=held,
        )
        return held


def release_staged_startup_hold(path: str | Path | None = None) -> bool:
    """Clear the staged-startup hold; return whether it is now absent."""

    marker = startup_hold_marker_path(path)
    try:
        marker.unlink(missing_ok=True)
        return True
    except OSError as exc:
        log_event(
            logger,
            "active_speaker.staged_startup_hold_clear_failed",
            level=logging.WARNING,
            path=marker,
            error=type(exc).__name__,
        )
        return False
