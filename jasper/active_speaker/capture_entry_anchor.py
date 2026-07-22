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

Fail direction: a stash that is never restored leaves the speaker on the
all-muted staged anchor — muted, never loud. Recovery surfaces:
jasper-correction-web's own idle shutdown (the common abandon — the user
closes the tab and the wizard idles out minutes later), the service-start
claim boundary (a process that crashed/restarted mid-sequence), and any later
crossover apply (which repoints production itself and makes the stash inert).
After those, the only stranded-muted case left is a hard crash/reboot
mid-sequence — unchanged from before this stash existed, and exactly the
crash-recovery-MUTED posture the startup-load S3 guard prescribes; it
converges at the next correction-web start.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

STATE_KIND = "jts_active_speaker_capture_entry_anchor"
SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_capture_entry.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_CAPTURE_ENTRY_STATE"

logger = logging.getLogger(__name__)


def state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def record_entry(entry_config_path: str, *, path: str | Path | None = None) -> None:
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
    target = state_path(path)
    atomic_write_text(
        target,
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": STATE_KIND,
                "entry_config_path": entry,
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
    )


def pending_entry(*, path: str | Path | None = None) -> str | None:
    """The stashed production path awaiting sequence-level restore, or None.

    Fail-soft: an unreadable/malformed stash reads as ``None`` at WARN — the
    speaker then stays on the all-muted staged anchor (muted, never loud)
    until an operator reapplies a profile.
    """

    target = state_path(path)
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log_event(
            logger,
            "active_speaker.capture_entry_anchor",
            level=logging.WARNING,
            action="read",
            status="unreadable",
            reason=type(exc).__name__,
        )
        return None
    if (
        not isinstance(raw, Mapping)
        or raw.get("kind") != STATE_KIND
        or raw.get("schema_version") != SCHEMA_VERSION
    ):
        log_event(
            logger,
            "active_speaker.capture_entry_anchor",
            level=logging.WARNING,
            action="read",
            status="malformed",
        )
        return None
    entry = str(raw.get("entry_config_path") or "").strip()
    return entry or None


def clear(*, path: str | Path | None = None) -> None:
    state_path(path).unlink(missing_ok=True)
