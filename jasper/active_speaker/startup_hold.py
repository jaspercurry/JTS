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

THREE units reach the writers, and they are not all root, nor all the same
sandbox:

* ``jasper-web`` (``User=jasper-web``, ``ProtectSystem=strict``) — writes the
  hold from the ``/sound/`` commissioning flow and its
  ``POST /active-speaker/load-startup-config`` route, and is the only unit that
  clears it, from ``/active-speaker/rollback-startup-config``.
* ``jasper-correction-web`` (root, ``ProtectSystem=full``, ``UMask=0077``) —
  writes it from ``/correction/``'s driver-capture and level-match arms, which
  reach the same ``load_protected_startup_config`` through
  ``web_commissioning._ensure_commission_startup_anchor``.
* ``jasper-web-streambox.service`` (root, ``ProtectSystem=full``) — the same
  ``python -m jasper.web`` process, installed AS ``jasper-web.service`` on a
  streambox, so it serves the same routes as the first entry from a root
  identity and an unrestricted ``/run``.

``ProtectSystem=strict`` mounts the hierarchy read-only apart from ``/dev``,
``/proc``, and ``/sys``, so only the first of the three cannot write under
``/run`` on its own; it owns the directory through
``RuntimeDirectory=jasper-active-speaker`` (``deploy/jasper-web.service``), which
systemd creates as ``jasper-web:jasper`` mode 0755 and excludes from
``ProtectSystem=``. The two root writers and the root reader need nothing
further, so no supplementary group is involved.

Fail direction: a write failure never raises here, but it is not silent —
``load_protected_startup_config`` refuses the load with the
``staged_startup_hold_unavailable`` blocker before it applies anything, because
the reconcile it would kick undoes an unheld anchor. **A write failure over an
EXISTING marker is not a failure to hold:** the two root writers create the
marker ``root:root`` 0600 under their ``UMask=0077``, which ``jasper-web``
cannot ``touch()`` — but the marker's PRESENCE is the hold, and the reader
decides on exactly that, so the writer answers from the marker rather than from
its own call. Release works from either identity because ``unlink`` needs write
on the 0755 DIRECTORY, not on the file. A read failure resolves to "no hold",
which restores the saved baseline — audio, never silence, and never louder. A
failed CLEAR is fail-safe on its own: the baseline restore just waits for the
next rollback or reboot.
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

    Fail-safe: any read error resolves to ``False`` (no hold), so the selector
    restores the saved baseline rather than silently preserving silence.
    """

    try:
        return startup_hold_marker_path(path).exists()
    except OSError:
        return False


def hold_staged_startup(path: str | Path | None = None) -> bool:
    """Mark the staged startup anchor as held. Never raises.

    Returns whether the marker is now present. ``load_protected_startup_config``
    refuses the load on ``False`` rather than applying an anchor the next
    reconcile would undo, so this answer is load-bearing, not advisory.
    """

    marker = startup_hold_marker_path(path)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    except OSError as exc:
        # The marker being PRESENT is the hold — that is exactly what the reader
        # decides on — so an existing marker this caller merely cannot rewrite
        # still holds the anchor. ``touch()`` raises ``PermissionError`` on such a
        # file, and the sibling root writers leave one: their ``UMask=0077`` makes
        # the marker ``root:root`` 0600, which a non-root writer cannot rewrite.
        # Releasing it still works from either side, because ``unlink`` needs
        # write on the 0755 DIRECTORY, not on the file. So answer from the
        # marker, not from this call's outcome; a genuine cannot-create still
        # answers False and the load still refuses.
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
    """Clear the staged-startup hold. Best-effort; never raises.

    Returns whether the marker is now absent.
    """

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
