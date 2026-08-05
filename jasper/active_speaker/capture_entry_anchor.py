# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable production entry-config stash for automatic capture sequences.

Why this exists: every automatic driver measurement (the level-match tone and
each repeat sweep attempt) used to restore CamillaDSP's persisted production
config path in its own per-attempt teardown. The next attempt then had to
reload the all-muted staged anchor again, producing two rapid CamillaDSP
config swaps (``SetConfigFilePath``+``Reload`` followed ~150 ms later by the
inline commissioning ``SetConfig``) immediately before the measurement
``aplay``. Hardware-reproduced on JTS3 2026-07-16 as deterministic sweep
transport timeouts: the double config bounce stalls the fan-in ->
loopback-ring -> CamillaDSP capture chain and the sweep writer starves.

The sequence now de-anchors production ONCE. The first automatic load stashes
the entry production path here; per-attempt teardown only rolls the RUNNING
graph back to the all-muted staged anchor; the persisted path stays anchored
between attempts (the crash posture ``startup_load``'s S3 guard prefers — the
durable config pointing at the all-muted staged anchor means any crash/reboot
during the sequence comes back muted, never loud). Production is restored
exactly once from this stash at sequence exit / recovery surfaces
(``web_commissioning.restore_pending_capture_entry_config``).

R15 reuses this same durable owner for its inline protected-neutral graph swap.
That mode does not repoint CamillaDSP's persisted config path, but CamillaDSP
can outlive correction-web while still executing the inline graph. The stored
``restore_mode`` distinguishes the legacy staged-anchor obligation from this
inline obligation; restart recovery reloads the exact entry config before
correction-web may accept another request.

Fail direction: a stash that is never restored leaves the speaker on the
all-muted staged anchor — muted, never loud. Recovery surfaces:
jasper-correction-web's own idle shutdown (the common abandon — the user
closes the tab and the wizard idles out minutes later), the service-start
claim boundary (a process that crashed/restarted mid-sequence), and any later
crossover apply (which repoints production itself and makes the stash inert).
An inline-graph obligation is stricter: correction-web does not accept
requests after restart until the exact production config has reloaded or a
new production owner has demonstrably superseded it. Unreadable state remains
a blocking obligation rather than being conflated with an absent stash.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

STATE_KIND = "jts_active_speaker_capture_entry_anchor"
SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_capture_entry.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_CAPTURE_ENTRY_STATE"

logger = logging.getLogger(__name__)

RESTORE_MODE_STAGED_ANCHOR = "staged_anchor"
RESTORE_MODE_INLINE_GRAPH = "inline_graph"
_RESTORE_MODES = {RESTORE_MODE_STAGED_ANCHOR, RESTORE_MODE_INLINE_GRAPH}


@dataclass(frozen=True)
class CaptureEntryAnchor:
    entry_config_path: str
    restore_mode: str


class CaptureEntryAnchorStateError(RuntimeError):
    """The durable restore obligation exists but cannot be interpreted."""


def state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def record_entry(
    entry_config_path: str,
    *,
    restore_mode: str = RESTORE_MODE_STAGED_ANCHOR,
    path: str | Path | None = None,
) -> None:
    """Durably stash the production config path a capture sequence de-anchors.

    Called only when the persisted CamillaDSP path is NOT the staged anchor —
    i.e. exactly when a load is about to replace live production with the
    all-muted anchor. Later loads in the same sequence see the anchor as their
    entry path and never reach this writer, so the stash keeps the original
    production path for the whole sequence. A failure here must propagate:
    de-anchoring production without a durable restore target would strand the
    speaker muted with no recorded way back.
    """

    entry = str(entry_config_path or "").strip()
    if not entry:
        raise ValueError("capture entry stash requires a production config path")
    if restore_mode not in _RESTORE_MODES:
        raise ValueError("capture entry stash restore mode is invalid")
    target = state_path(path)
    pending = pending_anchor(path=target, fail_on_corrupt=True)
    if pending is not None and pending != CaptureEntryAnchor(entry, restore_mode):
        raise RuntimeError("a different capture entry restore is already pending")
    atomic_write_text(
        target,
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": STATE_KIND,
                "entry_config_path": entry,
                "restore_mode": restore_mode,
                "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        mode=0o640,
        group_from_parent=True,
    )
    log_event(
        logger,
        "active_speaker.capture_entry_anchor",
        action="record",
        entry_config_path=entry,
        restore_mode=restore_mode,
    )


def pending_anchor(
    *,
    path: str | Path | None = None,
    fail_on_corrupt: bool = False,
) -> CaptureEntryAnchor | None:
    """The stashed production path awaiting sequence-level restore, or None.

    Compatibility callers may retain the historical fail-soft ``None`` view.
    Recovery owners pass ``fail_on_corrupt=True`` so unreadable state blocks
    request acceptance and cannot masquerade as an absent obligation.
    """

    target = state_path(path)
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log_event(
            logger,
            "active_speaker.capture_entry_anchor",
            level=logging.WARNING,
            action="read",
            status="unreadable",
            reason=type(exc).__name__,
        )
        if fail_on_corrupt:
            raise CaptureEntryAnchorStateError(
                f"capture entry anchor is unreadable: {type(exc).__name__}"
            ) from exc
        return None
    if (
        not isinstance(raw, Mapping)
        or set(raw)
        not in (
            {"schema_version", "kind", "entry_config_path", "stored_at"},
            {
                "schema_version",
                "kind",
                "entry_config_path",
                "restore_mode",
                "stored_at",
            },
        )
        or raw.get("kind") != STATE_KIND
        or raw.get("schema_version") != SCHEMA_VERSION
        or not isinstance(raw.get("stored_at"), str)
        or not raw.get("stored_at")
    ):
        log_event(
            logger,
            "active_speaker.capture_entry_anchor",
            level=logging.WARNING,
            action="read",
            status="malformed",
        )
        if fail_on_corrupt:
            raise CaptureEntryAnchorStateError("capture entry anchor is malformed")
        return None
    entry = str(raw.get("entry_config_path") or "").strip()
    # State written before R15 belongs to the original staged-anchor owner.
    restore_mode = str(raw.get("restore_mode") or RESTORE_MODE_STAGED_ANCHOR)
    if not entry or restore_mode not in _RESTORE_MODES:
        log_event(
            logger,
            "active_speaker.capture_entry_anchor",
            level=logging.WARNING,
            action="read",
            status="malformed",
        )
        if fail_on_corrupt:
            raise CaptureEntryAnchorStateError("capture entry anchor is malformed")
        return None
    return CaptureEntryAnchor(entry, restore_mode)


def pending_entry(*, path: str | Path | None = None) -> str | None:
    """Compatibility view of the exact pending restore target."""

    pending = pending_anchor(path=path)
    return pending.entry_config_path if pending is not None else None


def clear(*, path: str | Path | None = None) -> None:
    state_path(path).unlink(missing_ok=True)
