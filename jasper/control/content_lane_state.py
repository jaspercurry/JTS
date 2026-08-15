# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only snapshot of jasper-outputd's content-lane park record.

``deploy/bin/jasper-outputd-failure-reconcile`` runs from
``jasper-outputd.service``'s ``ExecStopPost``. When outputd fails to open
its content lane on ``CONTENT_LANE_PARK_AFTER`` consecutive starts, that
helper writes a park record to ``/run/jasper-outputd-content-lane.state``
and stops the unit out-of-band, deliberately, so the unit cannot ride
``Restart=on-failure`` into ``StartLimitAction=reboot``.

Until now that record had **no reader**: the park was a silent speaker whose
only trace was a journal line. This module is the reader, and it is
deliberately ONE reader with two consumers — ``jasper-doctor``'s
``check_outputd_content_lane_park`` and ``/state.resilience.content_lane`` —
so the operator-facing verdict cannot differ between the two surfaces.

Shape mirrors :mod:`jasper.control.bootloop_guard_state`: the closest
precedent in the tree (a ``/run`` marker written by a shell one-shot, read
fresh at request time, never raising, with a top-level discriminator).

**Freshness.** Re-read on every call, never cached and never sourced from
``os.environ``: jasper-control is not restarted when outputd parks, so a
value captured at import would be permanently wrong — the same trap
``jasper.voice.provider_state`` and ``jasper.transit.state`` exist to avoid.

**Readability.** The record lands ``root:jasper`` mode ``0660``:
``jasper-outputd.service`` sets ``Group=jasper`` + ``UMask=0007`` and runs
without a ``User=`` (so root). ``jasper-control.service`` runs
``User=jasper-control`` ``Group=jasper``, so it is group-readable there.
That readability is INHERITED from the two unit files rather than enforced by
an explicit ``chmod`` in the writer, so this reader treats ``EACCES`` as
``unreadable`` rather than as "not parked" — a permissions regression must
degrade loudly, not silently report a healthy speaker.
"""
from __future__ import annotations

import os
import time
from typing import Any

from ..env_load import parse_env_text

#: Must equal ``CONTENT_LANE_STATE`` in
#: ``deploy/bin/jasper-outputd-failure-reconcile``. Pinned against that script
#: by ``tests/test_content_lane_state.py`` — a literal duplicated across a
#: shell writer and a Python reader is exactly the pair that drifts.
DEFAULT_STATE_PATH = "/run/jasper-outputd-content-lane.state"

#: Must equal ``CONTENT_LANE_WINDOW_SEC`` in the same script. A count-only
#: record older than this is NOT a live streak — the writer's own semantics,
#: mirrored here so the two agree on what "currently failing" means.
DEFAULT_WINDOW_SEC = 300

#: The ``reason=`` the writer stamps on a park (as opposed to a count-only
#: record written while a streak is still below the park threshold).
PARK_REASON = "content_lane_open_repeated"


def _state_path() -> str:
    return os.environ.get(
        "JASPER_OUTPUTD_CONTENT_LANE_STATE", DEFAULT_STATE_PATH
    )


def _window_sec() -> int:
    raw = os.environ.get("JASPER_OUTPUTD_CONTENT_LANE_WINDOW_SEC")
    if raw is None:
        return DEFAULT_WINDOW_SEC
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_WINDOW_SEC


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def snapshot(
    path: str | None = None, *, now: float | None = None
) -> dict[str, Any]:
    """Fail-soft read of the content-lane record.

    Returns one of three shapes, discriminated by ``status``:

    ``{"status": "absent", "parked": False}``
        No record — outputd has not failed its content lane this boot
        (``/run`` wipes at boot, so this is per-boot truth).

    ``{"status": "unreadable", "parked": False, "error": ...}``
        The record exists but could not be read. Reported distinctly from
        "absent" on purpose: a permissions regression must not read as a
        healthy speaker.

    ``{"status": "present", "parked": bool, ...}``
        A record was read. ``parked`` is True only for a real park
        (``reason=content_lane_open_repeated``); a count-only record means a
        streak is in progress but the unit has not been stopped. For those,
        ``streak_live`` says whether the record is inside the writer's own
        streak window or is a stale leftover from a lane that has since
        recovered.

    ``{"status": "unintelligible", "parked": False, ...}``
        A non-park record whose ``count``/``last_failure_epoch`` do not parse.
        Reported distinctly rather than as a healthy "recovered" record: this
        module's whole posture is that a surface it cannot read must not
        report a healthy speaker, and an unintelligible record deserves the
        same treatment as an unreadable one. (Reachable only via a partial
        write that still renames — the writer is tempfile+rename with
        ``> "$tmp" 2>/dev/null || true``, so ENOSPC can truncate it.)

    Never raises.
    """
    target = path if path is not None else _state_path()
    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {"status": "absent", "parked": False}
    except OSError as exc:
        return {
            "status": "unreadable",
            "parked": False,
            "path": target,
            "error": str(exc),
        }

    try:
        fields = parse_env_text(text)
    except Exception:  # noqa: BLE001 - a malformed record must not raise here
        fields = {}

    count = _int_or_none(fields.get("count"))
    last_failure_epoch = _int_or_none(fields.get("last_failure_epoch"))
    parked = fields.get("reason") == PARK_REASON

    result: dict[str, Any] = {
        "status": "present",
        "parked": parked,
        "path": target,
        "count": count,
        "last_failure_epoch": last_failure_epoch,
    }

    if parked:
        result.update(
            {
                "reason": fields.get("reason"),
                "lane": fields.get("lane"),
                "parked_utc": fields.get("parked_utc"),
                "detail": fields.get("detail"),
                "action": fields.get("action"),
                "re_arm": fields.get("re_arm"),
            }
        )
        return result

    # A non-park record we cannot read is NOT a recovered lane. Without this,
    # a truncated record produced `ok | ... a stale record of None failure(s)
    # remains (Nones old ...)` — unintelligible text AND the opposite verdict
    # from the unreadable path two branches up.
    if count is None or last_failure_epoch is None:
        result["status"] = "unintelligible"
        return result

    # Count-only record: is the streak live, or a leftover from a lane that
    # recovered? The writer clears the record on a clean stop, not on a
    # successful start, so a stale record can outlive the problem it recorded.
    now_sec = time.time() if now is None else now
    age = now_sec - last_failure_epoch
    result["streak_live"] = 0 <= age < _window_sec()
    result["age_sec"] = int(age)
    return result
