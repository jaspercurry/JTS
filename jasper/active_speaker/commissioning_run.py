# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable identity and lifecycle journal for Active commissioning runs.

This store is deliberately narrower than the commissioning orchestrator.  It
does not apply graphs, admit captures, score candidates, or recover hardware.
It gives those later host adapters one fail-closed current-run authority:

* an exact session/run/owner-generation identity,
* immutable, unique attempt/target reservations,
* a bounded hash-chained journal of :class:`CommissioningTransition` values,
* atomic read-modify-write persistence under one advisory lock, and
* stale-callback rejection after service restart.

Every public read validates the complete schema, semantic invariants, nested
fingerprints, journal chain, and whole-file fingerprint. Polling is silent.
Successfully committed run creation/replacement, owner claims, attempt
reservations, and lifecycle transitions emit stable events.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jasper.atomic_io import atomic_write_text
from jasper.audio_measurement.evidence_identity import (
    EvidenceIdentityError,
    json_fingerprint,
)
from jasper.log_event import log_event

from .commissioning_lifecycle import (
    COMMISSIONING_STATES,
    CommissioningLifecycleError,
    CommissioningState,
    CommissioningTransition,
)

SCHEMA_VERSION = 1
STATE_KIND = "jts_active_commissioning_run_state"
ATTEMPT_KIND = "jts_active_commissioning_attempt"
JOURNAL_ENTRY_KIND = "jts_active_commissioning_transition_entry"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_commissioning_run.json")

# This is control-plane state, not an evidence store.  Keeping both collection
# counts and serialized bytes bounded prevents a corrupt or adversarial file
# from turning a status read into unbounded work on the 1 GB production Pi.
MAX_STATE_BYTES = 256 * 1024
MAX_ATTEMPTS = 256
MAX_TRANSITIONS = 128
MAX_OWNER_GENERATION = 2_147_483_647
MAX_ID_LENGTH = 160
DEFAULT_LOCK_TIMEOUT_S = 2.0
MAX_LOCK_TIMEOUT_S = 10.0
LOCK_POLL_INTERVAL_S = 0.01

logger = logging.getLogger(__name__)
_THREAD_LOCK = threading.RLock()
_UUID_HEX_RE = re.compile(r"[0-9a-f]{32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class CommissioningRunError(RuntimeError):
    """Durable commissioning run state is malformed or cannot be updated."""


class CommissioningRunConflict(CommissioningRunError):
    """A requested operation conflicts with the exact current run."""


class CommissioningRunStale(CommissioningRunConflict):
    """A callback or reservation belongs to an older run owner generation."""


class CommissioningRunLockTimeout(CommissioningRunConflict):
    """The bounded in-process/advisory store-lock deadline expired."""


@dataclass(frozen=True)
class CommissioningRunHandle:
    """Exact callback identity for one owner generation of a durable run."""

    session_id: str
    session_fingerprint: str
    run_id: str
    owner_id: str
    owner_generation: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "session_id",
            _identifier(self.session_id, field_name="session_id"),
        )
        object.__setattr__(
            self,
            "session_fingerprint",
            _sha256(self.session_fingerprint, field_name="session_fingerprint"),
        )
        object.__setattr__(
            self,
            "run_id",
            _uuid_hex(self.run_id, field_name="run_id"),
        )
        object.__setattr__(
            self,
            "owner_id",
            _uuid_hex(self.owner_id, field_name="owner_id"),
        )
        object.__setattr__(
            self,
            "owner_generation",
            _positive_int(
                self.owner_generation,
                field_name="owner_generation",
                maximum=MAX_OWNER_GENERATION,
            ),
        )


@dataclass(frozen=True)
class CommissioningAttemptHandle:
    """Exact identity for one immutable target attempt in a run generation."""

    run: CommissioningRunHandle
    attempt_id: str
    attempt_number: int
    target_id: str
    target_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.run, CommissioningRunHandle):
            raise TypeError("run must be a CommissioningRunHandle")
        object.__setattr__(
            self,
            "attempt_id",
            _uuid_hex(self.attempt_id, field_name="attempt_id"),
        )
        object.__setattr__(
            self,
            "attempt_number",
            _positive_int(
                self.attempt_number,
                field_name="attempt_number",
                maximum=MAX_ATTEMPTS,
            ),
        )
        object.__setattr__(
            self,
            "target_id",
            _identifier(self.target_id, field_name="target_id"),
        )
        object.__setattr__(
            self,
            "target_fingerprint",
            _sha256(self.target_fingerprint, field_name="target_fingerprint"),
        )


def state_path(path: str | Path | None = None) -> Path:
    """Resolve the durable production path while retaining a test seam."""

    return Path(path or DEFAULT_STATE_PATH)


def _identifier(value: Any, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_ID_LENGTH
        or _IDENTIFIER_RE.fullmatch(value) is None
    ):
        raise CommissioningRunError(f"{field_name} must be a bounded identifier")
    return value


def _sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CommissioningRunError(
            f"{field_name} must be a lowercase SHA-256 fingerprint"
        )
    return value


def _uuid_hex(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise CommissioningRunError(f"{field_name} must be a lowercase UUID hex id")
    return value


def _positive_int(value: Any, *, field_name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise CommissioningRunError(
            f"{field_name} must be an integer between 1 and {maximum}"
        )
    return value


def _lock_timeout(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= MAX_LOCK_TIMEOUT_S
    ):
        raise CommissioningRunError(
            f"lock_timeout_s must be finite and between 0 and {MAX_LOCK_TIMEOUT_S}"
        )
    return float(value)


def _timestamp(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise CommissioningRunError(f"{field_name} must be a UTC second timestamp")
    try:
        time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise CommissioningRunError(
            f"{field_name} must be a UTC second timestamp"
        ) from exc
    return value


def _fingerprint(payload: Mapping[str, Any], *, field_name: str) -> str:
    try:
        return json_fingerprint(payload, field_name=field_name)
    except EvidenceIdentityError as exc:
        raise CommissioningRunError(str(exc)) from exc


def _state_core(current: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": STATE_KIND,
        "current": dict(current) if current is not None else None,
    }


def _state_payload(current: Mapping[str, Any] | None) -> dict[str, Any]:
    core = _state_core(current)
    return {
        **core,
        "fingerprint": _fingerprint(core, field_name="commissioning run state"),
    }


def _attempt_core(
    *,
    attempt_id: str,
    attempt_number: int,
    owner_generation: int,
    target_id: str,
    target_fingerprint: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": ATTEMPT_KIND,
        "attempt_id": attempt_id,
        "attempt_number": attempt_number,
        "owner_generation": owner_generation,
        "target_id": target_id,
        "target_fingerprint": target_fingerprint,
        "created_at": created_at,
    }


def _attempt_payload(**fields: Any) -> dict[str, Any]:
    core = _attempt_core(**fields)
    return {
        **core,
        "fingerprint": _fingerprint(core, field_name="commissioning attempt"),
    }


def _journal_core(
    *,
    sequence: int,
    occurred_at: str,
    owner_generation: int,
    attempt_id: str | None,
    target_id: str | None,
    target_fingerprint: str | None,
    previous_entry_fingerprint: str | None,
    transition: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": JOURNAL_ENTRY_KIND,
        "sequence": sequence,
        "occurred_at": occurred_at,
        "owner_generation": owner_generation,
        "attempt_id": attempt_id,
        "target_id": target_id,
        "target_fingerprint": target_fingerprint,
        "previous_entry_fingerprint": previous_entry_fingerprint,
        "transition": dict(transition),
    }


def _journal_payload(**fields: Any) -> dict[str, Any]:
    core = _journal_core(**fields)
    return {
        **core,
        "fingerprint": _fingerprint(core, field_name="commissioning journal entry"),
    }


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CommissioningRunError(
                "commissioning run state contains duplicate object fields"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CommissioningRunError(
        f"commissioning run state contains non-JSON number {value}"
    )


def _parse_json(data: bytes) -> Any:
    if len(data) > MAX_STATE_BYTES:
        raise CommissioningRunError("commissioning run state exceeds size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommissioningRunError("commissioning run state is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except CommissioningRunError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CommissioningRunError("commissioning run state is unreadable") from exc


def _parse_attempt(raw: Any, *, expected_number: int) -> dict[str, Any]:
    expected = {
        "schema_version",
        "kind",
        "attempt_id",
        "attempt_number",
        "owner_generation",
        "target_id",
        "target_fingerprint",
        "created_at",
        "fingerprint",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CommissioningRunError(
            "commissioning attempt has unknown or missing fields"
        )
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != SCHEMA_VERSION
        or raw["kind"] != ATTEMPT_KIND
    ):
        raise CommissioningRunError("commissioning attempt schema is unsupported")
    attempt_id = _uuid_hex(raw["attempt_id"], field_name="attempt_id")
    attempt_number = _positive_int(
        raw["attempt_number"],
        field_name="attempt_number",
        maximum=MAX_ATTEMPTS,
    )
    if attempt_number != expected_number:
        raise CommissioningRunError("commissioning attempts are not contiguous")
    owner_generation = _positive_int(
        raw["owner_generation"],
        field_name="owner_generation",
        maximum=MAX_OWNER_GENERATION,
    )
    target_id = _identifier(raw["target_id"], field_name="target_id")
    target_fingerprint = _sha256(
        raw["target_fingerprint"], field_name="target_fingerprint"
    )
    created_at = _timestamp(raw["created_at"], field_name="created_at")
    result = _attempt_payload(
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        owner_generation=owner_generation,
        target_id=target_id,
        target_fingerprint=target_fingerprint,
        created_at=created_at,
    )
    if raw["fingerprint"] != result["fingerprint"]:
        raise CommissioningRunError(
            "declared commissioning attempt fingerprint does not match payload"
        )
    return result


def _parse_journal_entry(
    raw: Any,
    *,
    expected_sequence: int,
    expected_from_state: CommissioningState,
    expected_previous_fingerprint: str | None,
    attempts_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], CommissioningState]:
    expected = {
        "schema_version",
        "kind",
        "sequence",
        "occurred_at",
        "owner_generation",
        "attempt_id",
        "target_id",
        "target_fingerprint",
        "previous_entry_fingerprint",
        "transition",
        "fingerprint",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CommissioningRunError(
            "commissioning journal entry has unknown or missing fields"
        )
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != SCHEMA_VERSION
        or raw["kind"] != JOURNAL_ENTRY_KIND
    ):
        raise CommissioningRunError("commissioning journal entry schema is unsupported")
    sequence = _positive_int(
        raw["sequence"], field_name="sequence", maximum=MAX_TRANSITIONS
    )
    if sequence != expected_sequence:
        raise CommissioningRunError("commissioning journal is not contiguous")
    occurred_at = _timestamp(raw["occurred_at"], field_name="occurred_at")
    owner_generation = _positive_int(
        raw["owner_generation"],
        field_name="owner_generation",
        maximum=MAX_OWNER_GENERATION,
    )
    previous = raw["previous_entry_fingerprint"]
    if previous is not None:
        previous = _sha256(previous, field_name="previous_entry_fingerprint")
    if previous != expected_previous_fingerprint:
        raise CommissioningRunError("commissioning journal chain is broken")

    attempt_id = raw["attempt_id"]
    target_id = raw["target_id"]
    target_fingerprint = raw["target_fingerprint"]
    if attempt_id is None:
        if target_id is not None or target_fingerprint is not None:
            raise CommissioningRunError(
                "commissioning journal target requires an attempt identity"
            )
    else:
        attempt_id = _uuid_hex(attempt_id, field_name="attempt_id")
        target_id = _identifier(target_id, field_name="target_id")
        target_fingerprint = _sha256(
            target_fingerprint, field_name="target_fingerprint"
        )
        attempt = attempts_by_id.get(attempt_id)
        if (
            attempt is None
            or attempt["owner_generation"] != owner_generation
            or attempt["target_id"] != target_id
            or attempt["target_fingerprint"] != target_fingerprint
        ):
            raise CommissioningRunError(
                "commissioning journal attempt binding is inconsistent"
            )
    try:
        transition = CommissioningTransition.from_mapping(raw["transition"])
    except CommissioningLifecycleError as exc:
        raise CommissioningRunError(
            "commissioning journal transition is invalid"
        ) from exc
    if transition.from_state != expected_from_state:
        raise CommissioningRunError("commissioning journal lifecycle chain is broken")
    result = _journal_payload(
        sequence=sequence,
        occurred_at=occurred_at,
        owner_generation=owner_generation,
        attempt_id=attempt_id,
        target_id=target_id,
        target_fingerprint=target_fingerprint,
        previous_entry_fingerprint=previous,
        transition=transition.to_dict(),
    )
    if raw["fingerprint"] != result["fingerprint"]:
        raise CommissioningRunError(
            "declared commissioning journal fingerprint does not match payload"
        )
    return result, transition.to_state


def _parse_current(raw: Any) -> dict[str, Any]:
    expected = {
        "session_id",
        "session_fingerprint",
        "run_id",
        "owner_id",
        "owner_generation",
        "lifecycle_state",
        "attempts",
        "transition_journal",
        "started_at",
        "owner_claimed_at",
        "updated_at",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CommissioningRunError(
            "commissioning run entry has unknown or missing fields"
        )
    session_id = _identifier(raw["session_id"], field_name="session_id")
    session_fingerprint = _sha256(
        raw["session_fingerprint"], field_name="session_fingerprint"
    )
    run_id = _uuid_hex(raw["run_id"], field_name="run_id")
    owner_id = _uuid_hex(raw["owner_id"], field_name="owner_id")
    owner_generation = _positive_int(
        raw["owner_generation"],
        field_name="owner_generation",
        maximum=MAX_OWNER_GENERATION,
    )
    lifecycle_raw = raw["lifecycle_state"]
    if not isinstance(lifecycle_raw, str) or lifecycle_raw not in COMMISSIONING_STATES:
        raise CommissioningRunError("commissioning lifecycle state is invalid")
    lifecycle_state = cast(CommissioningState, lifecycle_raw)
    started_at = _timestamp(raw["started_at"], field_name="started_at")
    owner_claimed_at = _timestamp(
        raw["owner_claimed_at"], field_name="owner_claimed_at"
    )
    updated_at = _timestamp(raw["updated_at"], field_name="updated_at")

    attempts_raw = raw["attempts"]
    if not isinstance(attempts_raw, list) or len(attempts_raw) > MAX_ATTEMPTS:
        raise CommissioningRunError("commissioning attempts are malformed or unbounded")
    attempts = [
        _parse_attempt(item, expected_number=index)
        for index, item in enumerate(attempts_raw, start=1)
    ]
    attempt_ids = [str(item["attempt_id"]) for item in attempts]
    if len(set(attempt_ids)) != len(attempt_ids):
        raise CommissioningRunError("commissioning attempt identities are not unique")
    attempts_by_id = {str(item["attempt_id"]): item for item in attempts}

    journal_raw = raw["transition_journal"]
    if not isinstance(journal_raw, list) or len(journal_raw) > MAX_TRANSITIONS:
        raise CommissioningRunError(
            "commissioning transition journal is malformed or unbounded"
        )
    journal: list[dict[str, Any]] = []
    expected_state: CommissioningState = "unconfigured"
    previous: str | None = None
    for sequence, item in enumerate(journal_raw, start=1):
        parsed, expected_state = _parse_journal_entry(
            item,
            expected_sequence=sequence,
            expected_from_state=expected_state,
            expected_previous_fingerprint=previous,
            attempts_by_id=attempts_by_id,
        )
        journal.append(parsed)
        previous = str(parsed["fingerprint"])
    if lifecycle_state != expected_state:
        raise CommissioningRunError(
            "commissioning lifecycle state does not match its transition journal"
        )
    if any(int(item["owner_generation"]) > owner_generation for item in attempts):
        raise CommissioningRunError(
            "commissioning attempt belongs to a future owner generation"
        )
    if any(int(item["owner_generation"]) > owner_generation for item in journal):
        raise CommissioningRunError(
            "commissioning transition belongs to a future owner generation"
        )
    return {
        "session_id": session_id,
        "session_fingerprint": session_fingerprint,
        "run_id": run_id,
        "owner_id": owner_id,
        "owner_generation": owner_generation,
        "lifecycle_state": lifecycle_state,
        "attempts": attempts,
        "transition_journal": journal,
        "started_at": started_at,
        "owner_claimed_at": owner_claimed_at,
        "updated_at": updated_at,
    }


def _parse_state(raw: Any) -> dict[str, Any]:
    expected = {"schema_version", "kind", "current", "fingerprint"}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CommissioningRunError(
            "commissioning run state has unknown or missing fields"
        )
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != SCHEMA_VERSION
        or raw["kind"] != STATE_KIND
    ):
        raise CommissioningRunError("commissioning run state schema is unsupported")
    current_raw = raw["current"]
    current = _parse_current(current_raw) if current_raw is not None else None
    result = _state_payload(current)
    if raw["fingerprint"] != result["fingerprint"]:
        raise CommissioningRunError(
            "declared commissioning run fingerprint does not match payload"
        )
    return result


class CommissioningRunStore:
    """One durable current commissioning run with exact callback correlation."""

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        owner_id: str | None = None,
        now: Callable[[], float] = time.time,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = state_path(path)
        self._uuid_factory = uuid_factory
        self.owner_id = _uuid_hex(
            owner_id or uuid_factory().hex,
            field_name="owner_id",
        )
        self._now_clock = now
        self.lock_timeout_s = _lock_timeout(lock_timeout_s)
        self._monotonic = monotonic
        self._sleep = sleep

    def _now(self) -> str:
        value = float(self._now_clock())
        if not math.isfinite(value):
            raise CommissioningRunError("commissioning run clock is non-finite")
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))

    @contextmanager
    def _locked(self) -> Iterator[None]:
        started = float(self._monotonic())
        if not math.isfinite(started):
            raise CommissioningRunError("commissioning lock clock is non-finite")
        deadline = started + self.lock_timeout_s

        def remaining() -> float:
            value = float(self._monotonic())
            if not math.isfinite(value):
                raise CommissioningRunError("commissioning lock clock is non-finite")
            return deadline - value

        thread_budget = remaining()
        if thread_budget <= 0.0 or not _THREAD_LOCK.acquire(timeout=thread_budget):
            raise CommissioningRunLockTimeout(
                "timed out waiting for the in-process commissioning run lock"
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            with lock_path.open("a+", encoding="utf-8") as handle:
                os.chmod(lock_path, 0o640)
                file_lock_acquired = False
                while not file_lock_acquired:
                    if remaining() <= 0.0:
                        raise CommissioningRunLockTimeout(
                            "timed out waiting for the commissioning run file lock"
                        )
                    try:
                        fcntl.flock(
                            handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        file_lock_acquired = True
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                            raise CommissioningRunError(
                                "commissioning run file lock failed"
                            ) from exc
                        sleep_budget = remaining()
                        if sleep_budget <= 0.0:
                            raise CommissioningRunLockTimeout(
                                "timed out waiting for the commissioning run file lock"
                            ) from exc
                        self._sleep(min(LOCK_POLL_INTERVAL_S, sleep_budget))
                try:
                    yield
                finally:
                    if file_lock_acquired:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            _THREAD_LOCK.release()

    def _read(self) -> dict[str, Any]:
        try:
            # The limit belongs on the read itself, not on a preceding stat:
            # a non-cooperating writer could replace the file between those
            # calls and make ``read_bytes`` allocate an unbounded payload.
            with self.path.open("rb") as handle:
                data = handle.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return _state_payload(None)
        except OSError as exc:
            raise CommissioningRunError(
                "commissioning run state is unreadable"
            ) from exc
        if len(data) > MAX_STATE_BYTES:
            raise CommissioningRunError("commissioning run state exceeds size limit")
        return _parse_state(_parse_json(data))

    def _write(self, state: Mapping[str, Any]) -> None:
        validated = _parse_state(state)
        encoded = (
            json.dumps(validated, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        )
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise CommissioningRunError("commissioning run state exceeds size limit")
        atomic_write_text(
            self.path,
            encoded,
            mode=0o640,
            group_from_parent=True,
        )

    @staticmethod
    def _handle(current: Mapping[str, Any]) -> CommissioningRunHandle:
        return CommissioningRunHandle(
            session_id=str(current["session_id"]),
            session_fingerprint=str(current["session_fingerprint"]),
            run_id=str(current["run_id"]),
            owner_id=str(current["owner_id"]),
            owner_generation=int(current["owner_generation"]),
        )

    @staticmethod
    def _matches_handle(
        current: Mapping[str, Any], handle: CommissioningRunHandle
    ) -> bool:
        return (
            current.get("session_id") == handle.session_id
            and current.get("session_fingerprint") == handle.session_fingerprint
            and current.get("run_id") == handle.run_id
            and current.get("owner_id") == handle.owner_id
            and current.get("owner_generation") == handle.owner_generation
        )

    @staticmethod
    def _attempt_from_raw(
        handle: CommissioningRunHandle, raw: Mapping[str, Any]
    ) -> CommissioningAttemptHandle:
        return CommissioningAttemptHandle(
            run=handle,
            attempt_id=str(raw["attempt_id"]),
            attempt_number=int(raw["attempt_number"]),
            target_id=str(raw["target_id"]),
            target_fingerprint=str(raw["target_fingerprint"]),
        )

    @staticmethod
    def _matches_attempt(
        current: Mapping[str, Any], attempt: CommissioningAttemptHandle
    ) -> bool:
        if not CommissioningRunStore._matches_handle(current, attempt.run):
            return False
        return any(
            raw.get("attempt_id") == attempt.attempt_id
            and raw.get("attempt_number") == attempt.attempt_number
            and raw.get("owner_generation") == attempt.run.owner_generation
            and raw.get("target_id") == attempt.target_id
            and raw.get("target_fingerprint") == attempt.target_fingerprint
            for raw in cast(list[Mapping[str, Any]], current["attempts"])
        )

    def start(
        self,
        *,
        session_id: str,
        session_fingerprint: str,
    ) -> CommissioningRunHandle:
        """Create the only current run; an existing run must be recovered first."""

        session = _identifier(session_id, field_name="session_id")
        session_fp = _sha256(session_fingerprint, field_name="session_fingerprint")
        with self._locked():
            state = self._read()
            if state["current"] is not None:
                raise CommissioningRunConflict(
                    "a commissioning run already exists and must be recovered"
                )
            now = self._now()
            current = {
                "session_id": session,
                "session_fingerprint": session_fp,
                "run_id": self._uuid_factory().hex,
                "owner_id": self.owner_id,
                "owner_generation": 1,
                "lifecycle_state": "unconfigured",
                "attempts": [],
                "transition_journal": [],
                "started_at": now,
                "owner_claimed_at": now,
                "updated_at": now,
            }
            _uuid_hex(current["run_id"], field_name="run_id")
            state = _state_payload(current)
            self._write(state)
            handle = self._handle(current)
        log_event(
            logger,
            "correction.active_commissioning_run_started",
            session=handle.session_id,
            run_id=handle.run_id,
            owner_generation=handle.owner_generation,
            state_fingerprint=state["fingerprint"],
        )
        return handle

    def replace_current(
        self,
        *,
        session_id: str,
        session_fingerprint: str,
    ) -> CommissioningRunHandle:
        """Atomically start a fresh session and stale every prior callback.

        This is the explicit production boundary for abandoning the control-
        plane identity of a prior commissioning run.  A possibly live or
        unknown post-mutation graph cannot be hidden by replacement: those
        lifecycle states first require exact host recovery evidence.
        """

        session = _identifier(session_id, field_name="session_id")
        session_fp = _sha256(session_fingerprint, field_name="session_fingerprint")
        replaced: dict[str, Any] | None = None
        with self._locked():
            state = self._read()
            current_raw = state["current"]
            if isinstance(current_raw, Mapping):
                replaced = dict(current_raw)
                if replaced["lifecycle_state"] in {
                    "applied_unverified",
                    "blocked_live_state_unknown",
                }:
                    raise CommissioningRunConflict(
                        "live or unknown post-mutation state requires recovery before replacement"
                    )
            now = self._now()
            run_id = self._uuid_factory().hex
            _uuid_hex(run_id, field_name="run_id")
            if replaced is not None and run_id == replaced["run_id"]:
                raise CommissioningRunError(
                    "replacement commissioning run identity was not fresh"
                )
            current = {
                "session_id": session,
                "session_fingerprint": session_fp,
                "run_id": run_id,
                "owner_id": self.owner_id,
                "owner_generation": 1,
                "lifecycle_state": "unconfigured",
                "attempts": [],
                "transition_journal": [],
                "started_at": now,
                "owner_claimed_at": now,
                "updated_at": now,
            }
            state = _state_payload(current)
            self._write(state)
            fresh = self._handle(current)
        if replaced is None:
            log_event(
                logger,
                "correction.active_commissioning_run_started",
                session=fresh.session_id,
                run_id=fresh.run_id,
                owner_generation=fresh.owner_generation,
                state_fingerprint=state["fingerprint"],
            )
        else:
            log_event(
                logger,
                "correction.active_commissioning_run_replaced",
                prior_session=replaced["session_id"],
                prior_run_id=replaced["run_id"],
                prior_state=replaced["lifecycle_state"],
                new_session=fresh.session_id,
                new_run_id=fresh.run_id,
            )
        return fresh

    def claim_owner(self) -> CommissioningRunHandle | None:
        """Claim a persisted run and advance generation after a process restart."""

        prior_owner_id: str | None = None
        with self._locked():
            state = self._read()
            current_raw = state["current"]
            if current_raw is None:
                return None
            current = dict(cast(Mapping[str, Any], current_raw))
            if current["owner_id"] == self.owner_id:
                return self._handle(current)
            prior_owner_id = str(current["owner_id"])
            generation = int(current["owner_generation"])
            if generation >= MAX_OWNER_GENERATION:
                raise CommissioningRunError(
                    "commissioning owner generation is exhausted"
                )
            now = self._now()
            current.update(
                {
                    "owner_id": self.owner_id,
                    "owner_generation": generation + 1,
                    "owner_claimed_at": now,
                    "updated_at": now,
                }
            )
            state = _state_payload(current)
            self._write(state)
            handle = self._handle(current)
        assert prior_owner_id is not None
        log_event(
            logger,
            "correction.active_commissioning_owner_claimed",
            session=handle.session_id,
            run_id=handle.run_id,
            prior_owner_id=prior_owner_id,
            owner_generation=handle.owner_generation,
            state_fingerprint=state["fingerprint"],
        )
        return handle

    def reserve_attempt(
        self,
        handle: CommissioningRunHandle,
        *,
        target_id: str,
        target_fingerprint: str,
    ) -> CommissioningAttemptHandle:
        """Append one immutable, generation-bound attempt before async work."""

        if not isinstance(handle, CommissioningRunHandle):
            raise TypeError("handle must be a CommissioningRunHandle")
        target = _identifier(target_id, field_name="target_id")
        target_fp = _sha256(target_fingerprint, field_name="target_fingerprint")
        with self._locked():
            state = self._read()
            current_raw = state["current"]
            if not isinstance(current_raw, Mapping) or not self._matches_handle(
                current_raw, handle
            ):
                raise CommissioningRunStale(
                    "commissioning attempt belongs to a stale run generation"
                )
            current = dict(current_raw)
            attempts = list(cast(list[Mapping[str, Any]], current["attempts"]))
            if len(attempts) >= MAX_ATTEMPTS:
                raise CommissioningRunConflict(
                    "commissioning run reached its bounded attempt limit"
                )
            attempt_id = self._uuid_factory().hex
            _uuid_hex(attempt_id, field_name="attempt_id")
            if any(raw["attempt_id"] == attempt_id for raw in attempts):
                raise CommissioningRunError(
                    "commissioning attempt identity was not unique"
                )
            now = self._now()
            attempt = _attempt_payload(
                attempt_id=attempt_id,
                attempt_number=len(attempts) + 1,
                owner_generation=handle.owner_generation,
                target_id=target,
                target_fingerprint=target_fp,
                created_at=now,
            )
            attempts.append(attempt)
            current.update({"attempts": attempts, "updated_at": now})
            state = _state_payload(current)
            self._write(state)
            reserved = self._attempt_from_raw(handle, attempt)
        log_event(
            logger,
            "correction.active_commissioning_attempt_reserved",
            session=handle.session_id,
            run_id=handle.run_id,
            owner_generation=handle.owner_generation,
            attempt_id=reserved.attempt_id,
            attempt_number=reserved.attempt_number,
            target=reserved.target_id,
            target_fingerprint=reserved.target_fingerprint,
            state_fingerprint=state["fingerprint"],
        )
        return reserved

    def callback_is_current(
        self,
        callback: CommissioningRunHandle | CommissioningAttemptHandle,
    ) -> bool:
        """Return whether an async callback still owns this exact generation."""

        if not isinstance(
            callback, (CommissioningRunHandle, CommissioningAttemptHandle)
        ):
            raise TypeError("callback must be a commissioning handle")
        with self._locked():
            current = self._read()["current"]
            if not isinstance(current, Mapping):
                return False
            if isinstance(callback, CommissioningAttemptHandle):
                return self._matches_attempt(current, callback)
            return self._matches_handle(current, callback)

    def transition(
        self,
        handle: CommissioningRunHandle,
        transition: CommissioningTransition,
        *,
        attempt: CommissioningAttemptHandle | None = None,
    ) -> bool:
        """Commit one legal transition; stale async callbacks are ignored."""

        if not isinstance(handle, CommissioningRunHandle):
            raise TypeError("handle must be a CommissioningRunHandle")
        if not isinstance(transition, CommissioningTransition):
            raise TypeError("transition must be a CommissioningTransition")
        if attempt is not None and not isinstance(attempt, CommissioningAttemptHandle):
            raise TypeError("attempt must be a CommissioningAttemptHandle")
        committed: dict[str, Any] | None = None
        state_fingerprint: str | None = None
        with self._locked():
            state = self._read()
            current_raw = state["current"]
            if not isinstance(current_raw, Mapping) or not self._matches_handle(
                current_raw, handle
            ):
                return False
            if attempt is not None and not self._matches_attempt(current_raw, attempt):
                return False
            if transition.from_state != current_raw["lifecycle_state"]:
                raise CommissioningRunConflict(
                    "commissioning transition does not start at the current state"
                )
            journal = list(
                cast(list[Mapping[str, Any]], current_raw["transition_journal"])
            )
            if len(journal) >= MAX_TRANSITIONS:
                raise CommissioningRunConflict(
                    "commissioning run reached its bounded transition limit"
                )
            now = self._now()
            committed = _journal_payload(
                sequence=len(journal) + 1,
                occurred_at=now,
                owner_generation=handle.owner_generation,
                attempt_id=attempt.attempt_id if attempt is not None else None,
                target_id=attempt.target_id if attempt is not None else None,
                target_fingerprint=(
                    attempt.target_fingerprint if attempt is not None else None
                ),
                previous_entry_fingerprint=(
                    str(journal[-1]["fingerprint"]) if journal else None
                ),
                transition=transition.to_dict(),
            )
            journal.append(committed)
            current = dict(current_raw)
            current.update(
                {
                    "lifecycle_state": transition.to_state,
                    "transition_journal": journal,
                    "updated_at": now,
                }
            )
            state = _state_payload(current)
            self._write(state)
            state_fingerprint = str(state["fingerprint"])
        assert committed is not None
        assert state_fingerprint is not None
        log_event(
            logger,
            "correction.active_commissioning_transition",
            session=handle.session_id,
            run_id=handle.run_id,
            owner_generation=handle.owner_generation,
            sequence=committed["sequence"],
            from_state=transition.from_state,
            to_state=transition.to_state,
            evidence_kind=transition.evidence_kind,
            evidence_fingerprint=transition.evidence_fingerprint,
            failure_code=transition.failure_code,
            attempt_id=attempt.attempt_id if attempt is not None else None,
            target=attempt.target_id if attempt is not None else None,
            state_fingerprint=state_fingerprint,
        )
        return True

    def snapshot(self) -> dict[str, Any]:
        """Return a detached, fully validated snapshot without logging."""

        # Status polling must not create a lock file or parent directory merely
        # because no commissioning run has ever existed. A concurrent first
        # writer may become visible on the next poll; all non-empty reads still
        # take the advisory lock and validate the complete artifact.
        if not self.path.exists():
            return _state_payload(None)
        with self._locked():
            return cast(dict[str, Any], json.loads(json.dumps(self._read())))
