# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read legacy crossover repeat records and close unfinished work at startup."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import advisory_file_lock, atomic_write_text
from jasper.log_event import log_event

STATE_KIND = "jts_active_speaker_repeat_admission"
SCHEMA_VERSION = 1
# Audible attempts and total reservations in stored repeat records.
MAX_ATTEMPTS = 4
MAX_RESERVATIONS = 8
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_repeat_admission.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_REPEAT_ADMISSION_STATE"
# Bounded backpressure for the write paths that keep the lock (ADR-0196).
DEFAULT_LOCK_TIMEOUT_S = 2.0
OWNER_ID = uuid.uuid4().hex
_THREAD_LOCK = threading.RLock()
logger = logging.getLogger(__name__)
_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)






def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _base() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": STATE_KIND,
        "comparison": None,
        "targets": {},
        "updated_at": None,
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _base()
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("crossover repeat admission state is unreadable") from exc
    if not isinstance(raw, Mapping) or raw.get("kind") != STATE_KIND:
        raise RuntimeError("crossover repeat admission state is malformed")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("crossover repeat admission schema is unsupported")
    comparison_raw = raw.get("comparison")
    comparison = None
    if comparison_raw is not None:
        if not isinstance(comparison_raw, Mapping):
            raise RuntimeError("crossover repeat comparison binding is malformed")
        comparison = {
            "comparison_set_id": str(comparison_raw.get("comparison_set_id") or ""),
            "fingerprint": str(comparison_raw.get("fingerprint") or ""),
        }
        if not all(comparison.values()):
            raise RuntimeError("crossover repeat comparison binding is incomplete")
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, Mapping):
        raise RuntimeError("crossover repeat targets are malformed")
    targets: dict[str, dict[str, Any]] = {}
    for key, value in targets_raw.items():
        target_id = str(key)
        if not target_id or not isinstance(value, Mapping):
            raise RuntimeError("crossover repeat target entry is malformed")
        attempts = value.get("attempts")
        status = value.get("status")
        inflight = value.get("inflight")
        results = value.get("results", [])
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 1 <= attempts <= MAX_RESERVATIONS
        ):
            raise RuntimeError("crossover repeat target state is invalid")
        if not isinstance(results, list):
            raise RuntimeError("crossover repeat target state is invalid")
        result_attempts: list[int] = []
        for item in results:
            if not isinstance(item, Mapping):
                raise RuntimeError("crossover repeat target state is invalid")
            result_attempt = item.get("attempt")
            if (
                isinstance(result_attempt, bool)
                or not isinstance(result_attempt, int)
                or not 1 <= result_attempt <= attempts
            ):
                raise RuntimeError("crossover repeat target state is invalid")
            emitted = item.get("audio_emitted")
            if emitted is not None and not isinstance(emitted, bool):
                raise RuntimeError("crossover repeat target state is invalid")
            result_attempts.append(result_attempt)
        result_attempts_ordered = result_attempts == sorted(set(result_attempts))
        if (
            status not in {"active", "ready", "completed", "refused", "aborted"}
            or (inflight is not None and (
                not isinstance(inflight, str) or _UUID_HEX_RE.fullmatch(inflight) is None
            ))
            or status != "active" and inflight is not None
            or len(results) > attempts
            or not result_attempts_ordered
            or str(value.get("target_id") or "") != target_id
            or not str(value.get("target_fingerprint") or "")
            or _UUID_HEX_RE.fullmatch(str(value.get("owner_id") or "")) is None
        ):
            raise RuntimeError("crossover repeat target state is invalid")
        targets[target_id] = {
            "target_id": target_id,
            "target_fingerprint": str(value["target_fingerprint"]),
            "owner_id": str(value["owner_id"]),
            "attempts": attempts,
            "status": status,
            "inflight": inflight,
            "results": [dict(item) for item in results],
            "reason": value.get("reason"),
            "updated_at": value.get("updated_at"),
        }
    out = _base()
    out["comparison"] = comparison
    out["targets"] = targets
    out["updated_at"] = raw.get("updated_at")
    return out


def _write(path: Path, state: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(dict(state), indent=2, sort_keys=True) + "\n",
        mode=0o640,
    )


@contextmanager
def _locked(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    with _THREAD_LOCK:
        with advisory_file_lock(
            lock_path,
            timeout_sec=DEFAULT_LOCK_TIMEOUT_S,
        ):
            yield


def claim_owner(*, path: str | Path | None = None) -> dict[str, Any]:
    """At service start, close active work left by the previous process.

    The correction web socket uses ``Accept=no`` and one service ``ExecStart``;
    there is exactly one owner process.  Claiming is deliberately explicit at
    that lifecycle boundary. Ordinary reads remain pure and a second live
    process cannot destructively steal an inflight reservation.
    """

    target = state_path(path)
    with _locked(target):
        state = _load(target)
        targets = dict(state["targets"])
        aborted: list[tuple[str, dict[str, Any]]] = []
        for key, raw in targets.items():
            entry = dict(raw)
            prior_status = entry.get("status")
            if (
                entry.get("owner_id") != OWNER_ID
                and prior_status in {"active", "ready"}
            ):
                reason = (
                    "service_restarted_during_finalization"
                    if prior_status == "ready"
                    else "service_restarted"
                )
                entry.update({
                    "status": "aborted",
                    "reason": reason,
                    "inflight": None,
                    "updated_at": _now(),
                })
                targets[key] = entry
                aborted.append((key, entry))
        if aborted:
            state.update({"targets": targets, "updated_at": _now()})
            _write(target, state)
            for target_id, entry in aborted:
                log_event(
                    logger,
                    "correction.crossover_repeat_aborted",
                    target=target_id,
                    attempts=entry.get("attempts"),
                    reason=entry.get("reason"),
                )
        return state
