# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persist household source intent and validate reconciliation receipts.

The group-writable intent file accepts only fixed source keys and exactly
``enabled`` or ``disabled``. It cannot name a unit, command, adapter, or
arbitrary lifecycle operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jasper.atomic_io import (
    advisory_file_lock,
    locked_update_env_file,
    read_regular_bytes_nofollow,
)
from jasper.control.restart_broker import manage_units
from jasper.env_file import parse_env_lines
from jasper.env_load import SOURCE_INTENT_ENV
from jasper.local_sources import local_source_lifecycle, local_source_lifecycles
from jasper.log_event import log_event
from jasper.music_sources import Source
from jasper.source_intent_units import RECONCILE_BROKER_TIMEOUT_SECONDS, RECONCILE_UNIT

logger = logging.getLogger(__name__)

SOURCE_STATUS_PATH = "/run/jasper-source-intent/status.json"

_INTENT_KEY_PREFIX = "JASPER_SOURCE_INTENT_"
_BLUETOOTH_INTENT_KEY = "JASPER_BLUETOOTH_SOURCE_INTENT"
_ENABLED = "enabled"
_DISABLED = "disabled"
_MAX_INTENT_BYTES = 64 * 1024
_MAX_STATUS_BYTES = 64 * 1024
_REQUEST_LOCK_TIMEOUT_SEC = 2.0
_INTENT_FILE_MODE = 0o660

IntentWriter = Callable[[str, Mapping[str, str]], None]
ReconcileKicker = Callable[[], Mapping[str, Any]]


def _env_slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def intent_env_key(subject: str | Source) -> str:
    """Return the fixed env key for a source (or a legacy unit string).

    Unit strings remain accepted because existing web/deploy callers use this
    helper, and the three shipped systemd-backed key names are persisted on
    deployed speakers.  Passing a :class:`Source` preserves those exact legacy
    keys; Bluetooth, which has no single intent unit, uses its source id.
    """

    if subject == Source.BLUETOOTH:
        # Deliberately outside the historical JASPER_SOURCE_INTENT_* namespace.
        # Pre-Bluetooth-intent releases reject unknown keys in that namespace
        # but ignore unrelated env keys, so a code rollback remains operable.
        return _BLUETOOTH_INTENT_KEY
    if isinstance(subject, Source):
        lifecycle = local_source_lifecycle(subject)
        identity = lifecycle.intent_unit or subject.value
    else:
        identity = subject
    return f"{_INTENT_KEY_PREFIX}{_env_slug(identity)}"


def source_intent_sources() -> tuple[Source, ...]:
    """The complete, fixed source-intent allowlist."""

    return tuple(lifecycle.source for lifecycle in local_source_lifecycles())


def _valid_keys() -> dict[str, Source]:
    return {
        intent_env_key(lifecycle.source): lifecycle.source
        for lifecycle in local_source_lifecycles()
    }


def read_intent(env_path: str) -> str:
    try:
        data = read_regular_bytes_nofollow(
            env_path,
            max_bytes=_MAX_INTENT_BYTES,
        )
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise RuntimeError(f"cannot read {env_path}: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{env_path} is not valid UTF-8: {exc}") from exc


@dataclass(frozen=True)
class _IntentProblem:
    event: str
    message: str
    source: Source | None = None
    key: str = ""
    value: str = ""


def parse_source_intents(
    text: str,
) -> tuple[dict[Source, bool], tuple[_IntentProblem, ...]]:
    """Parse defaults plus overrides without acting on malformed entries."""

    intents = {
        lifecycle.source: lifecycle.default_enabled
        for lifecycle in local_source_lifecycles()
    }
    valid = _valid_keys()
    problems: list[_IntentProblem] = []
    invalid_sources: set[Source] = set()
    assignments = {
        key: value.strip().strip("'\"")
        for key, value in parse_env_lines(text)
        if value is not None
    }
    for key, value in assignments.items():
        source = valid.get(key)
        if source is None:
            if not key.startswith(_INTENT_KEY_PREFIX):
                continue
            problems.append(
                _IntentProblem(
                    event="source_intent.rejected_unit",
                    message=f"unrecognized source intent key {key}",
                    key=key,
                )
            )
            continue
        if value == _ENABLED:
            intents[source] = True
        elif value == _DISABLED:
            intents[source] = False
        else:
            invalid_sources.add(source)
            problems.append(
                _IntentProblem(
                    event="source_intent.bad_value",
                    message=f"invalid intent value for {source.value}: {value}",
                    source=source,
                    key=key,
                    value=value,
                )
            )
    # An explicit malformed value must fail closed for that source. Returning
    # desired=False lets the root coordinator tear down an already-running
    # source; ``problems`` still makes the pass fail loudly/non-zero. Unknown
    # keys have no Source and therefore never authorize arbitrary action.
    # Other valid sources still reconcile independently.
    for source in invalid_sources:
        intents[source] = False
    return intents, tuple(problems)


def read_source_intents(
    env_path: str = SOURCE_INTENT_ENV,
) -> dict[Source, bool]:
    """Read the strict desired-state map, filling absent keys from defaults."""

    intents, problems = parse_source_intents(read_intent(env_path))
    if problems:
        raise RuntimeError("; ".join(problem.message for problem in problems))
    return intents


def source_intent_enabled(
    source: Source,
    env_path: str = SOURCE_INTENT_ENV,
) -> bool:
    """Read one source's intent with affected-source failure isolation.

    A malformed value for ``source`` raises so its start gate fails closed.
    Problems owned by another recognized source do not park this one, and an
    unknown key cannot select this source or authorize an action. Full-map
    consumers that need the global validity verdict use
    :func:`read_source_intents`, which remains strict for every problem.
    """

    intents, problems = parse_source_intents(read_intent(env_path))
    relevant = [problem for problem in problems if problem.source == source]
    if relevant:
        raise RuntimeError("; ".join(problem.message for problem in relevant))
    return intents[source]


@dataclass(frozen=True)
class _TargetStatus:
    exact: bool
    succeeded: bool
    detail: str
    # The per-source outcomes THIS validated read saw, empty when the read
    # never got that far. Handed out so a caller can attribute the pass
    # without re-opening the file (see `_failed_siblings`).
    # excluded from eq/hash: keeps the frozen dataclass hashable; consumers
    # only field-access it
    sources: Mapping[str, Any] = field(default_factory=dict, compare=False)


def _read_target_status(
    *,
    path: str,
    source: Source,
    desired: str,
    intent_fingerprint: str,
    not_before_monotonic_ns: int,
) -> _TargetStatus:
    """Read one fresh completion acknowledgement without following symlinks."""

    if path == SOURCE_STATUS_PATH:
        try:
            parent = os.lstat(os.path.dirname(path))
            inode = os.lstat(path)
        except OSError as exc:
            return _TargetStatus(False, False, f"completion status is missing: {exc}")
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != 0
            or parent.st_mode & 0o022
            or not stat.S_ISREG(inode.st_mode)
            or inode.st_uid != 0
            or inode.st_mode & 0o022
        ):
            return _TargetStatus(False, False, "completion status ownership is unsafe")
    try:
        raw = read_regular_bytes_nofollow(path, max_bytes=_MAX_STATUS_BYTES)
        payload = json.loads(raw)
    except FileNotFoundError:
        return _TargetStatus(False, False, "completion status is missing")
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
    ) as exc:
        return _TargetStatus(False, False, f"completion status is unreadable: {exc}")
    if not isinstance(payload, dict):
        return _TargetStatus(False, False, "completion status is not an object")
    completed = payload.get("completed_monotonic_ns")
    if isinstance(completed, bool) or not isinstance(completed, int):
        return _TargetStatus(False, False, "completion status has no timestamp")
    if completed < not_before_monotonic_ns:
        return _TargetStatus(False, False, "completion status is stale")
    observed_fingerprint = payload.get("intent_fingerprint")
    if observed_fingerprint != intent_fingerprint:
        return _TargetStatus(False, False, "completion status intent does not match")
    raw_sources = payload.get("sources")
    sources: Mapping[str, Any] = raw_sources if isinstance(raw_sources, dict) else {}
    entry = sources.get(source.value)
    if not isinstance(entry, dict):
        return _TargetStatus(
            False, False, "completion status has no target result", sources
        )
    observed_desired = entry.get("desired")
    if observed_desired != desired:
        return _TargetStatus(
            False,
            False,
            f"completion status desired={observed_desired!r}, expected={desired!r}",
            sources,
        )
    result = entry.get("result")
    if result not in {"ok", "failed"}:
        return _TargetStatus(
            False, False, "completion status has invalid target result", sources
        )
    effective = str(entry.get("effective") or "unknown")
    reason = str(entry.get("reason") or "")
    if result == "ok":
        return _TargetStatus(True, True, f"target effective={effective}", sources)
    return _TargetStatus(
        True,
        False,
        f"target effective={effective} failed" + (f": {reason}" if reason else ""),
        sources,
    )


# One sibling's reason must never crowd another sibling's NAME out of the
# field. Capping per entry rather than only the joined string bounds the worst
# case structurally: the fixed 4-source allowlist gives at most 3 siblings, so
# 3 * (len("bluetooth") + len(": ") + 80) + 2 separators = 277 <= the 300-char
# overall backstop below, and every failing source is always named.
_MAX_SIBLING_REASON_CHARS = 80


def _failed_siblings(sources: Mapping[str, Any], source: Source) -> str:
    """Name the sources OTHER than ``source`` that failed the same pass.

    Only called once the requested source has been proved converged, so the
    aggregate's non-zero exit belongs to something else. The coordinator
    already published a per-source outcome for every source; reading it turns
    an opaque ``aggregate_error="rc=1"`` into the source that actually failed.
    Issue #2175 is the cost of not doing this: a reader of the journal saw
    Bluetooth reconcile ``result=ok`` and the request still log a warning
    naming ``source=bluetooth``, and concluded the Bluetooth toggle had failed
    — it was USB's gadget restart timing out.

    Takes the ``sources`` mapping :func:`_read_target_status` already parsed
    rather than re-reading the file. The re-read happened after the request
    lock was released and skipped that function's fingerprint/monotonic
    validation, so it could name a sibling from a DIFFERENT reconcile pass than
    the ``aggregate_error`` it decorates. One validated read now backs both
    fields.

    Only declared sources are named, so an untrusted key in the status
    document can never reach the journal. Best-effort and non-raising: this
    decorates a warning, so an unexpected document reports nothing rather than
    turning a converged request into a failed one. An empty result is honest —
    the aggregate can also fail on a bad intent key or an unpublishable status,
    neither of which is a sibling source.
    """

    declared = {declared_source.value for declared_source in source_intent_sources()}
    failures: list[str] = []
    for name in sorted(declared - {source.value}):
        entry = sources.get(name)
        if not isinstance(entry, dict) or entry.get("result") != "failed":
            continue
        reason = str(entry.get("reason") or "").strip()[:_MAX_SIBLING_REASON_CHARS]
        failures.append(f"{name}: {reason}" if reason else name)
    return "; ".join(failures)[:300]


def intent_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_INTENT_ENV_OWNER = "JTS /sources intent control"


def _default_write_intent(path: str, updates: Mapping[str, str]) -> None:
    locked_update_env_file(
        path,
        updates,
        mode=_INTENT_FILE_MODE,
        max_bytes=_MAX_INTENT_BYTES,
        lock_timeout_sec=_REQUEST_LOCK_TIMEOUT_SEC,
        owner=_INTENT_ENV_OWNER,
    )


def kick_source_reconcile(
    *, reason: str = "source enable/disable"
) -> Mapping[str, Any]:
    """Run the canonical source owner synchronously without changing intent."""

    return manage_units(
        RECONCILE_UNIT,
        verb="start",
        reason=reason,
        no_block=False,
        timeout=RECONCILE_BROKER_TIMEOUT_SECONDS,
    )


def request_source_intent(
    source: Source,
    enabled: bool,
    *,
    env_path: str = SOURCE_INTENT_ENV,
    status_path: str = SOURCE_STATUS_PATH,
    writer: IntentWriter | None = None,
    kicker: ReconcileKicker | None = None,
) -> None:
    """Atomically record one source intent and synchronously reconcile it.

    The write intentionally remains authoritative if convergence fails.  A
    caller should then render desired-on/effective-degraded rather than rolling
    the household's choice back to observed runtime state. Success requires a
    fresh completion acknowledgement for the exact intent fingerprint and
    target; a stale joined activation gets one bounded retry.
    """

    write = writer or _default_write_intent
    kick = kicker or kick_source_reconcile
    key = intent_env_key(source)
    value = _ENABLED if enabled else _DISABLED
    # Serialize the complete write + synchronous apply transaction across the
    # /sources and /bluetooth web processes.  Without this outer lock, two
    # concurrent systemctl starts can join the same already-activating oneshot:
    # the later write is durable, but the running reconciler may already have
    # read the older file and both callers would incorrectly return success.
    # The writer's own adjacent lock still protects generic read/modify/write
    # callers; this request-only lock protects the larger transaction.
    request_lock_path = f"{env_path}.request.lock"
    response: Mapping[str, Any] = {"ok": False, "error": "not run"}
    target_status = _TargetStatus(False, False, "completion status was not read")
    try:
        with advisory_file_lock(
            request_lock_path,
            timeout_sec=_REQUEST_LOCK_TIMEOUT_SEC,
        ):
            request_started_ns = time.monotonic_ns()
            write(env_path, {key: value})
            try:
                fingerprint = intent_fingerprint(read_intent(env_path))
            except RuntimeError as exc:
                log_event(
                    logger,
                    "source.intent_write_failed",
                    source=source.value,
                    desired=value,
                    error=str(exc),
                    level=logging.WARNING,
                )
                raise RuntimeError(
                    f"could not verify recorded {source.value} {value} intent: {exc}"
                ) from exc
            # A start can join a oneshot that already read an older snapshot.
            # Its status then has an old timestamp/fingerprint, so run exactly
            # one fresh pass. A normal fresh acknowledgement stops after pass 1.
            for _ in range(2):
                try:
                    response = kick()
                except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                    log_event(
                        logger,
                        "source.intent_apply_failed",
                        source=source.value,
                        desired=value,
                        error=str(exc),
                        level=logging.WARNING,
                    )
                    raise RuntimeError(
                        f"could not apply {source.value} {value} intent: {exc}"
                    ) from exc
                target_status = _read_target_status(
                    path=status_path,
                    source=source,
                    desired=value,
                    intent_fingerprint=fingerprint,
                    not_before_monotonic_ns=request_started_ns,
                )
                if target_status.exact:
                    break
    except TimeoutError as exc:
        log_event(
            logger,
            "source.intent_busy",
            source=source.value,
            desired=value,
            error=str(exc),
            level=logging.WARNING,
        )
        raise RuntimeError(
            "source settings are busy applying another change; retry shortly"
        ) from exc
    except OSError as exc:
        log_event(
            logger,
            "source.intent_write_failed",
            source=source.value,
            desired=value,
            error=str(exc),
            level=logging.WARNING,
        )
        raise RuntimeError(
            f"could not record {source.value} {value} intent: {exc}"
        ) from exc
    aggregate_detail = (
        "ok"
        if response.get("ok")
        else str(response.get("error") or f"rc={response.get('rc')}")
    )
    if not target_status.exact or not target_status.succeeded:
        detail = f"aggregate={aggregate_detail}; {target_status.detail}"
        log_event(
            logger,
            "source.intent_apply_failed",
            source=source.value,
            desired=value,
            error=detail,
            level=logging.WARNING,
        )
        raise RuntimeError(f"could not apply {source.value} {value} intent: {detail}")
    if not response.get("ok"):
        # The requested source converged; the aggregate did not. Name the
        # sibling that failed so this warning cannot be read as "the toggle the
        # household pressed failed" (#2175). Attributed from the same validated
        # read that proved the target converged — not a fresh, unlocked one.
        log_event(
            logger,
            "source.intent_sibling_failure",
            source=source.value,
            desired=value,
            failed_siblings=_failed_siblings(target_status.sources, source) or None,
            aggregate_error=aggregate_detail,
            target=target_status.detail,
            level=logging.WARNING,
        )
    log_event(
        logger,
        "source.intent_requested",
        source=source.value,
        desired=value,
    )
