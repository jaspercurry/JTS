# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persist and reconcile household on/off intent for local music sources.

``/var/lib/jasper/source_intent.env`` is the single source of truth.  systemd
enablement, process state, BlueZ ``Powered``, RF-kill, and the USB gadget shape
are derived runtime state.  The web process may write one fixed intent key and
start this module's root oneshot; only this root process performs privileged
source lifecycle work.

The implementation is intentionally concrete.  Lifecycle declarations with
an intent unit use one ordinary systemd path (today AirPlay and Spotify), USB
keeps its load-bearing gadget ordering, and Bluetooth owns its RF-kill/BlueZ
sequence.  There is no source plugin API or resident coordinator daemon.

Security
--------
The intent file is group-writable by the management UI and therefore
untrusted.  Accepted keys
are derived from :mod:`jasper.local_sources`; values are exactly ``enabled`` or
``disabled``.  A file can select only among the four declared sources.  It can
never name a unit, command, adapter, or arbitrary lifecycle operation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from dbus_next.errors import DBusError  # type: ignore

from jasper.atomic_io import (
    advisory_file_lock,
    atomic_write_text,
    read_regular_bytes_nofollow,
)
from jasper.env_file import parse_env_lines
from jasper.fanin.combo_health import (
    DIRECT_HEALTH_CAPTURING,
    DIRECT_HEALTH_IDLE,
    extract_direct_sample,
)
from jasper.fanin.status import read_fanin_status
from jasper.local_sources import (
    local_source_lifecycle,
    local_source_lifecycles,
)
from jasper.log_event import log_event
from jasper.music_sources import Source

logger = logging.getLogger(__name__)

RECONCILE_UNIT = "jasper-source-intent-reconcile.service"
SOURCE_INTENT_ENV = "/var/lib/jasper/source_intent.env"
SOURCE_STATUS_PATH = "/run/jasper-source-intent/status.json"

_INTENT_KEY_PREFIX = "JASPER_SOURCE_INTENT_"
_BLUETOOTH_INTENT_KEY = "JASPER_BLUETOOTH_SOURCE_INTENT"
_ENABLED = "enabled"
_DISABLED = "disabled"
_MAX_INTENT_BYTES = 64 * 1024
_MAX_STATUS_BYTES = 64 * 1024
_REQUEST_LOCK_TIMEOUT_SEC = 2.0
SOURCE_RECONCILE_LOCK_TIMEOUT_SECONDS = 5.0
_RESET_FAILED_ACTION_TIMEOUT_SEC = 5.0
_USB_DIRECT_SETTLE_ATTEMPTS = 20
_USB_DIRECT_SETTLE_SECONDS = 0.25

_BLUETOOTH_SERVICE = "bluetooth.service"
_ACCESSORY_RECONCILE_UNIT = "jasper-accessory-reconcile.service"
_USB_COUPLING_UNIT = "jasper-fanin-coupling-auto.service"
# Every blocking source-unit action has two finite layers: the systemd unit's
# explicit TimeoutStartSec/TimeoutStopSec contract, then this client's slightly
# longer subprocess bound.  A client must never report timeout while PID 1 may
# still legally be running the same job.  ``restart`` can consume both service
# ceilings, so its client bound covers their sum.  Simple services keep small
# start ceilings; AirPlay has a real pre-start renderer and USB may spend 30 s
# in its wait-card ExecStartPre.
_DEFAULT_UNIT_ACTION_TIMEOUT_SEC = 15.0
_UNIT_ENABLEMENT_ACTION_TIMEOUT_SEC = 5.0
_UNIT_STATE_QUERY_TIMEOUT_SEC = 2.0
_UNIT_AVAILABLE_QUERY_TIMEOUT_SEC = 2.0
_UNIT_ACTION_CLIENT_MARGIN_SEC = 1.0
_SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC: dict[str, tuple[float, float]] = {
    # unit: (TimeoutStartSec, TimeoutStopSec)
    "shairport-sync.service": (30.0, 5.0),
    "nqptp.service": (2.0, 5.0),
    "librespot.service": (2.0, 5.0),
    "bluealsa.service": (5.0, 5.0),
    "bluealsa-aplay.service": (2.0, 5.0),
    "bt-agent.service": (2.0, 10.0),
    "jasper-usbgadget.service": (5.0, 5.0),
    "jasper-usbsink.service": (40.0, 5.0),
    "jasper-usbsink-volume.service": (2.0, 5.0),
}
_CONTROL_UNIT_SYSTEMD_TIMEOUT_SEC: dict[str, tuple[float, float]] = {
    _BLUETOOTH_SERVICE: (10.0, 10.0),
}
# A synchronous start waits for the whole required dependency transaction, not
# just the named service. AirPlay's packaged unit Requires=/After= our nqptp
# timing service, so a cold start legally consumes both start ceilings.
_SOURCE_UNIT_START_DEPENDENCY_TIMEOUT_SEC: dict[str, float] = {
    "shairport-sync.service": _SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC["nqptp.service"][0],
    "jasper-usbsink.service": _SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC[
        "jasper-usbsink-volume.service"
    ][0],
}
# Owner oneshots are different: a synchronous ``systemctl start`` may join and
# wait for their full Type=oneshot activation. Bluetooth deliberately starts
# the accessory owner twice (old-pass barrier + guaranteed-fresh pass); USB
# starts coupling once. Their 5-second margin includes broker/client overhead.
_OWNER_UNIT_ACTION_TIMEOUT_SEC = {
    _ACCESSORY_RECONCILE_UNIT: 65.0,  # target TimeoutStartSec=60
    _USB_COUPLING_UNIT: 125.0,  # target TimeoutStartSec=120
}


def _unit_action_timeout_sec(unit: str, verb: str) -> float:
    if verb == "start" and unit in _OWNER_UNIT_ACTION_TIMEOUT_SEC:
        return _OWNER_UNIT_ACTION_TIMEOUT_SEC[unit]
    if verb in {"enable", "disable"}:
        return _UNIT_ENABLEMENT_ACTION_TIMEOUT_SEC
    if verb == "reset-failed":
        return _RESET_FAILED_ACTION_TIMEOUT_SEC
    bounds = _SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC.get(
        unit
    ) or _CONTROL_UNIT_SYSTEMD_TIMEOUT_SEC.get(unit)
    if bounds is None or verb not in {"start", "stop", "restart"}:
        return _DEFAULT_UNIT_ACTION_TIMEOUT_SEC
    start_timeout, stop_timeout = bounds
    dependency_timeout = _SOURCE_UNIT_START_DEPENDENCY_TIMEOUT_SEC.get(unit, 0.0)
    service_timeout = {
        "start": start_timeout + dependency_timeout,
        "stop": stop_timeout,
        "restart": start_timeout + stop_timeout + dependency_timeout,
    }[verb]
    return service_timeout + _UNIT_ACTION_CLIENT_MARGIN_SEC


# A maximally cold On pass can block once on AirPlay's main unit (its Requires=
# transaction brings nqptp up), Spotify, the Bluetooth control plane plus three
# runtime units, a USB gadget recompose, and USB standby start. The complete
# outer budget below also includes every enablement pre/action/post sequence,
# state-probe overhead, the two accessory/one coupling owner barriers, bounded
# BlueZ/RF-kill work, direct-lane settling, and failed-USB rollback.
_WORST_CASE_ORDINARY_START_ACTIONS = (
    ("shairport-sync.service", "start"),
    ("librespot.service", "start"),
    (_BLUETOOTH_SERVICE, "start"),
    ("bluealsa.service", "start"),
    ("bluealsa-aplay.service", "start"),
    ("bt-agent.service", "start"),
    ("jasper-usbgadget.service", "restart"),
    ("jasper-usbsink.service", "start"),
)
_WORST_CASE_ORDINARY_STOP_ACTIONS = (
    ("shairport-sync.service", "stop"),
    ("nqptp.service", "stop"),
    ("librespot.service", "stop"),
    ("bt-agent.service", "stop"),
    ("bluealsa-aplay.service", "stop"),
    ("bluealsa.service", "stop"),
    ("jasper-usbsink.service", "stop"),
    ("jasper-usbgadget.service", "restart"),
)
_NON_SYSTEMD_RECONCILE_BUDGET_SEC = 15.0
_MAX_ENABLEMENT_TRANSITIONS = 7
_MAX_ENSURE_ACTIVE_TRANSITIONS = len(_WORST_CASE_ORDINARY_START_ACTIONS)
# Ordinary/Bluetooth appliers converge every runtime unit. USB converges only
# its intent unit through _ensure_active; gadget/volume are ordered dependencies.
_MAX_FAILED_RESET_TRANSITIONS = sum(
    (1 if lifecycle.source == Source.USBSINK else len(lifecycle.runtime_units))
    for lifecycle in local_source_lifecycles()
)
_ENABLEMENT_TRANSITION_BUDGET_SEC = (
    2 * _UNIT_STATE_QUERY_TIMEOUT_SEC + _UNIT_ENABLEMENT_ACTION_TIMEOUT_SEC
)
_FAILED_RESET_BUDGET_SEC = _MAX_FAILED_RESET_TRANSITIONS * (
    2 * _UNIT_STATE_QUERY_TIMEOUT_SEC
    + _unit_action_timeout_sec("source.service", "reset-failed")
)
_ACTIVE_TRANSITION_BUDGET_SEC = sum(
    _unit_action_timeout_sec(unit, verb)
    for unit, verb in _WORST_CASE_ORDINARY_START_ACTIONS
) + (2 * _UNIT_STATE_QUERY_TIMEOUT_SEC * _MAX_ENSURE_ACTIVE_TRANSITIONS)
_OWNER_RECONCILE_BUDGET_SEC = (
    2 * _OWNER_UNIT_ACTION_TIMEOUT_SEC[_ACCESSORY_RECONCILE_UNIT]
    + _OWNER_UNIT_ACTION_TIMEOUT_SEC[_USB_COUPLING_UNIT]
)
_BLUETOOTH_CONTROL_BUDGET_SEC = 30.0
_USB_DIRECT_WAIT_BUDGET_SEC = (
    _USB_DIRECT_SETTLE_ATTEMPTS * 0.5
    + (_USB_DIRECT_SETTLE_ATTEMPTS - 1) * _USB_DIRECT_SETTLE_SECONDS
)
_USB_FAILED_ON_CLEANUP_BUDGET_SEC = 165.0
_RECONCILE_TIMEOUT_MARGIN_SEC = 21.25
_NON_OWNER_RECONCILE_BUDGET_SEC = (
    _NON_SYSTEMD_RECONCILE_BUDGET_SEC
    + _MAX_ENABLEMENT_TRANSITIONS * _ENABLEMENT_TRANSITION_BUDGET_SEC
    + _FAILED_RESET_BUDGET_SEC
    + _ACTIVE_TRANSITION_BUDGET_SEC
    + _unit_action_timeout_sec("jasper-usbgadget.service", "restart")
    + _BLUETOOTH_CONTROL_BUDGET_SEC
    + _USB_DIRECT_WAIT_BUDGET_SEC
    + _USB_FAILED_ON_CLEANUP_BUDGET_SEC
)
RECONCILE_SYSTEMD_TIMEOUT_SECONDS = (
    _NON_OWNER_RECONCILE_BUDGET_SEC
    + _OWNER_RECONCILE_BUDGET_SEC
    + _RECONCILE_TIMEOUT_MARGIN_SEC
)
RECONCILE_BROKER_TIMEOUT_SECONDS = RECONCILE_SYSTEMD_TIMEOUT_SECONDS + 10.0
_UAC2_CARD_PATH = "/proc/asound/UAC2Gadget"
_INTENT_FILE_MODE = 0o660
_SHARED_LOCK_MODE = 0o660
_BLUETOOTH_SETTLE_ATTEMPTS = 12
_BLUETOOTH_SETTLE_SECONDS = 0.25
_BLUETOOTH_DBUS_TIMEOUT_SEC = 0.75
_BLUETOOTH_BLUEZ_ATTEMPTS = 3

SystemctlRunner = Callable[[str, bool], tuple[int, str]]
UnitRunner = Callable[[str, str], tuple[int, str]]
UnitProbe = Callable[[str], bool | None]
UnitAvailableProbe = Callable[[str], bool]
IntentWriter = Callable[[str, Mapping[str, str]], None]
ReconcileKicker = Callable[[], Mapping[str, Any]]
StatusWriter = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class BluetoothRfkillState:
    """Observed Linux RF-kill state for Bluetooth radios."""

    present: bool
    # ``soft_blocked`` means any Bluetooth RF-kill entry is blocked, which is
    # the correct On-path warning. ``all_soft_blocked`` proves the stronger Off
    # invariant when more than one adapter exists; ``None`` preserves the
    # single-adapter/test construction contract.
    soft_blocked: bool
    hard_blocked: bool
    all_soft_blocked: bool | None = None

    @property
    def fully_soft_blocked(self) -> bool:
        if self.all_soft_blocked is None:
            return self.soft_blocked
        return self.all_soft_blocked


@dataclass(frozen=True)
class ReconcileOps:
    """Injectable host operations used by the four concrete appliers.

    This is a test seam, not an extension contract.  The root coordinator owns
    every callable and source declarations never receive this object.
    """

    set_enabled: SystemctlRunner
    run_unit: UnitRunner
    unit_enabled: UnitProbe
    unit_active: UnitProbe
    unit_failed: UnitProbe
    unit_available: UnitAvailableProbe
    local_sources_allowed: Callable[[], bool]
    usb_audio_present: Callable[[], bool]
    usb_direct_present: Callable[[], bool]
    usb_direct_ready: Callable[[], bool]
    rfkill_state: Callable[[], BluetoothRfkillState]
    set_rfkill_blocked: Callable[[bool], tuple[int, str]]
    bluez_powered: Callable[[], bool | None]
    set_bluez_powered: Callable[[bool], tuple[int, str]]
    settle: Callable[[float], None]


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


def _read_intent(env_path: str) -> str:
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


def _parse_source_intents(
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

    intents, problems = _parse_source_intents(_read_intent(env_path))
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

    intents, problems = _parse_source_intents(_read_intent(env_path))
    relevant = [problem for problem in problems if problem.source == source]
    if relevant:
        raise RuntimeError("; ".join(problem.message for problem in relevant))
    return intents[source]


@dataclass(frozen=True)
class _TargetStatus:
    exact: bool
    succeeded: bool
    detail: str


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
    sources = payload.get("sources")
    entry = sources.get(source.value) if isinstance(sources, dict) else None
    if not isinstance(entry, dict):
        return _TargetStatus(False, False, "completion status has no target result")
    observed_desired = entry.get("desired")
    if observed_desired != desired:
        return _TargetStatus(
            False,
            False,
            f"completion status desired={observed_desired!r}, expected={desired!r}",
        )
    result = entry.get("result")
    if result not in {"ok", "failed"}:
        return _TargetStatus(
            False, False, "completion status has invalid target result"
        )
    effective = str(entry.get("effective") or "unknown")
    reason = str(entry.get("reason") or "")
    if result == "ok":
        return _TargetStatus(True, True, f"target effective={effective}")
    return _TargetStatus(
        True,
        False,
        f"target effective={effective} failed" + (f": {reason}" if reason else ""),
    )


def _default_write_status(path: str, payload: Mapping[str, Any]) -> None:
    """Atomically publish the root-owned, world-readable completion fact."""

    atomic_write_text(
        path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        mode=0o644,
    )


def _intent_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _publish_reconcile_status(
    *,
    path: str | None,
    intent_fingerprint: str,
    outcomes: Mapping[str, Mapping[str, str]],
    writer: StatusWriter | None,
) -> bool:
    if path is None:
        return True
    payload: Mapping[str, Any] = {
        "completed_monotonic_ns": time.monotonic_ns(),
        "intent_fingerprint": intent_fingerprint,
        "sources": dict(outcomes),
    }
    try:
        (writer or _default_write_status)(path, payload)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        log_event(
            logger,
            "source_intent.status_write_failed",
            path=path,
            error=str(exc),
            level=logging.WARNING,
        )
        return False
    return True


def _default_write_intent(path: str, updates: Mapping[str, str]) -> None:
    from jasper.atomic_io import locked_update_env_file

    locked_update_env_file(
        path,
        updates,
        mode=_INTENT_FILE_MODE,
        group_from_parent=True,
        lock_mode=_SHARED_LOCK_MODE,
        max_bytes=_MAX_INTENT_BYTES,
        lock_timeout_sec=_REQUEST_LOCK_TIMEOUT_SEC,
    )


def kick_source_reconcile(
    *, reason: str = "source enable/disable"
) -> Mapping[str, Any]:
    """Run the canonical source owner synchronously without changing intent."""

    from jasper.control.restart_broker import manage_units

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
            mode=_SHARED_LOCK_MODE,
            group_from_parent=True,
            timeout_sec=_REQUEST_LOCK_TIMEOUT_SEC,
        ):
            request_started_ns = time.monotonic_ns()
            write(env_path, {key: value})
            try:
                fingerprint = _intent_fingerprint(_read_intent(env_path))
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
        log_event(
            logger,
            "source.intent_sibling_failure",
            source=source.value,
            desired=value,
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


def _run_systemctl(unit: str, enabled: bool) -> tuple[int, str]:
    return _run_unit_action(unit, "enable" if enabled else "disable")


def _run_unit_action(unit: str, verb: str) -> tuple[int, str]:
    timeout = _unit_action_timeout_sec(unit, verb)
    try:
        process = subprocess.run(
            ["systemctl", verb, unit],
            check=False,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    detail = (process.stderr or process.stdout or "").strip()
    return process.returncode, detail


def _query_unit_state(query: str, unit: str) -> bool | None:
    try:
        process = subprocess.run(
            ["systemctl", query, unit],
            check=False,
            timeout=_UNIT_STATE_QUERY_TIMEOUT_SEC,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    state = (process.stdout or "").strip().lower()
    if query == "is-enabled":
        if state in {"enabled", "enabled-runtime"}:
            return True
        if state in {
            "disabled",
            "masked",
            "masked-runtime",
            "not-found",
            "static",
            "indirect",
            "generated",
            "transient",
            "linked",
            "linked-runtime",
            "alias",
        }:
            return False
    elif query == "is-active":
        if state == "active":
            return True
        if state in {"inactive", "failed"}:
            return False
    elif query == "is-failed":
        if state == "failed":
            return True
        if state in {
            "active",
            "activating",
            "deactivating",
            "inactive",
            "maintenance",
            "reloading",
        }:
            return False
    return None


def _unit_enabled(unit: str) -> bool | None:
    return _query_unit_state("is-enabled", unit)


def _unit_active(unit: str) -> bool | None:
    return _query_unit_state("is-active", unit)


def _unit_failed(unit: str) -> bool | None:
    return _query_unit_state("is-failed", unit)


def _unit_available(unit: str) -> bool:
    try:
        process = subprocess.run(
            ["systemctl", "show", unit, "-p", "LoadState", "--value"],
            check=False,
            timeout=_UNIT_AVAILABLE_QUERY_TIMEOUT_SEC,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return process.returncode == 0 and process.stdout.strip() == "loaded"


def _local_sources_allowed() -> bool:
    try:
        from jasper.install_profile import (
            install_profile_allows_local_sources,
            read_install_profile,
        )
        from jasper.local_sources.guard import local_sources_allowed

        if not install_profile_allows_local_sources(read_install_profile()):
            return False
        return local_sources_allowed()[0]
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "source.reconcile.role_probe_failed",
            error=str(exc),
            level=logging.WARNING,
        )
        return False


def _usb_audio_present() -> bool:
    try:
        return os.path.isdir(_UAC2_CARD_PATH)
    except OSError:
        return False


def _usb_direct_sample():
    return extract_direct_sample(read_fanin_status())


def _usb_direct_present() -> bool:
    return _usb_direct_sample() is not None


def _usb_direct_ready() -> bool:
    sample = _usb_direct_sample()
    return bool(
        sample is not None
        and sample.present
        and sample.health in {DIRECT_HEALTH_IDLE, DIRECT_HEALTH_CAPTURING}
    )


def read_bluetooth_rfkill_state() -> BluetoothRfkillState:
    present = False
    soft_states: list[bool] = []
    hard_blocked = False
    for entry in Path("/sys/class/rfkill").glob("rfkill*"):
        try:
            if (entry / "type").read_text(encoding="utf-8").strip() != "bluetooth":
                continue
            present = True
            soft_states.append(
                (entry / "soft").read_text(encoding="utf-8").strip() == "1"
            )
            hard_blocked = hard_blocked or (
                (entry / "hard").read_text(encoding="utf-8").strip() == "1"
            )
        except OSError as exc:
            raise RuntimeError(f"cannot read Bluetooth RF-kill state: {exc}") from exc
    return BluetoothRfkillState(
        present,
        any(soft_states),
        hard_blocked,
        all(soft_states) if soft_states else False,
    )


def _set_bluetooth_rfkill_blocked(blocked: bool) -> tuple[int, str]:
    try:
        process = subprocess.run(
            ["rfkill", "block" if blocked else "unblock", "bluetooth"],
            check=False,
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    detail = (process.stderr or process.stdout or "").strip()
    return process.returncode, detail


def _read_bluez_powered() -> bool | None:
    try:
        from jasper.bluetooth.adapter import state

        snapshot = asyncio.run(
            asyncio.wait_for(
                state(),
                timeout=_BLUETOOTH_DBUS_TIMEOUT_SEC,
            )
        )
        return bool(snapshot.get("powered", False))
    except (DBusError, OSError, RuntimeError, asyncio.TimeoutError):
        return None


def _set_bluez_powered(enabled: bool) -> tuple[int, str]:
    try:
        from jasper.bluetooth.adapter import set_powered

        asyncio.run(
            asyncio.wait_for(
                set_powered(enabled),
                timeout=_BLUETOOTH_DBUS_TIMEOUT_SEC,
            )
        )
    except (DBusError, OSError, RuntimeError, asyncio.TimeoutError) as exc:
        return 1, str(exc)
    return 0, ""


def default_reconcile_ops() -> ReconcileOps:
    return ReconcileOps(
        set_enabled=_run_systemctl,
        run_unit=_run_unit_action,
        unit_enabled=_unit_enabled,
        unit_active=_unit_active,
        unit_failed=_unit_failed,
        unit_available=_unit_available,
        local_sources_allowed=_local_sources_allowed,
        usb_audio_present=_usb_audio_present,
        usb_direct_present=_usb_direct_present,
        usb_direct_ready=_usb_direct_ready,
        rfkill_state=read_bluetooth_rfkill_state,
        set_rfkill_blocked=_set_bluetooth_rfkill_blocked,
        bluez_powered=_read_bluez_powered,
        set_bluez_powered=_set_bluez_powered,
        settle=time.sleep,
    )


def _check_result(rc: int, detail: str, operation: str) -> None:
    if rc != 0:
        raise RuntimeError(f"{operation} failed: {detail or f'rc={rc}'}")


def _ensure_enabled(ops: ReconcileOps, unit: str, desired: bool) -> bool:
    current = ops.unit_enabled(unit)
    if current is desired:
        return False
    rc, detail = ops.set_enabled(unit, desired)
    _check_result(rc, detail, f"systemctl {'enable' if desired else 'disable'} {unit}")
    if ops.unit_enabled(unit) is not desired:
        raise RuntimeError(f"{unit} enablement did not converge to {desired}")
    return True


def _ensure_active(
    ops: ReconcileOps,
    unit: str,
    desired: bool,
    *,
    force: bool = False,
) -> bool:
    current = ops.unit_active(unit)
    if current is desired and not force:
        return _reset_failed_if_needed(ops, unit) if not desired else False
    verb = "start" if desired else "stop"
    rc, detail = ops.run_unit(unit, verb)
    _check_result(rc, detail, f"systemctl {verb} {unit}")
    if ops.unit_active(unit) is not desired:
        raise RuntimeError(f"{unit} active state did not converge to {desired}")
    if not desired:
        _reset_failed_if_needed(ops, unit)
    return True


def _reset_failed_if_needed(ops: ReconcileOps, unit: str) -> bool:
    """Converge desired-Off units from ``failed`` to terminal ``inactive``."""

    failed = ops.unit_failed(unit)
    if failed is None:
        raise RuntimeError(f"could not determine whether {unit} is failed")
    if not failed:
        return False
    rc, detail = ops.run_unit(unit, "reset-failed")
    _check_result(rc, detail, f"systemctl reset-failed {unit}")
    if ops.unit_failed(unit) is not False:
        raise RuntimeError(f"{unit} failed state did not reset to inactive")
    return True


def _attempt_teardown(
    errors: list[str],
    operation: str,
    action: Callable[[], object],
) -> None:
    """Run one safe teardown step and retain a bounded error for the caller."""

    try:
        action()
    except (DBusError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        errors.append(f"{operation}: {exc}")


def _reconcile_systemd_source(
    source: Source,
    desired: bool,
    allowed: bool,
    ops: ReconcileOps,
) -> str:
    lifecycle = local_source_lifecycle(source)
    if lifecycle.intent_unit is None:
        raise RuntimeError(f"{source.value} has no systemd intent unit")
    if lifecycle.intent_unit not in lifecycle.runtime_units:
        raise RuntimeError(f"{source.value} lifecycle declaration is incomplete")
    effective_on = desired and allowed
    # All source-owned runtime units mirror the same persisted intent.  This
    # matters for AirPlay's nqptp companion: leaving it enabled would let boot
    # or a later role-restoration pass revive part of a source the household
    # turned off.
    if effective_on:
        for unit in lifecycle.runtime_units:
            _ensure_enabled(ops, unit, True)
    else:
        # Off and follower parking are safety transitions. A failed unit-file
        # mutation must not prevent later resources from being stopped; the
        # final ExecCondition also blocks any stale/queued restart.
        teardown_errors: list[str] = []
        for unit in lifecycle.runtime_units:
            _attempt_teardown(
                teardown_errors,
                f"set {unit} enabled={desired}",
                partial(_ensure_enabled, ops, unit, desired),
            )
        for unit in lifecycle.runtime_units:
            _attempt_teardown(
                teardown_errors,
                f"stop {unit}",
                partial(
                    _ensure_active,
                    ops,
                    unit,
                    False,
                    force=False,
                ),
            )
        if teardown_errors:
            raise RuntimeError("; ".join(teardown_errors))
        return "parked" if desired else "off"

    # Stop the main/intent unit first so it releases its companions cleanly;
    # start it first so systemd's Requires=/After= graph establishes them in
    # the package-declared order.  Explicit checks then verify every resource.
    _ensure_active(
        ops,
        lifecycle.intent_unit,
        effective_on,
        force=False,
    )
    for unit in lifecycle.runtime_units:
        if unit != lifecycle.intent_unit:
            _ensure_active(
                ops,
                unit,
                effective_on,
                force=False,
            )
    return "on"


def _reconcile_usbsink(
    desired: bool,
    allowed: bool,
    ops: ReconcileOps,
) -> str:
    lifecycle = local_source_lifecycle(Source.USBSINK)
    unit = lifecycle.intent_unit
    if unit is None or len(lifecycle.advertise_units) != 1:
        raise RuntimeError("USB lifecycle declaration is incomplete")
    gadget = lifecycle.advertise_units[0]
    effective_on = desired and allowed

    if effective_on:
        coupling_started = False
        try:
            # Enablement is written before composition because gadget-up uses
            # it as a derived readiness mirror in addition to canonical intent.
            _ensure_enabled(ops, unit, True)
            # Arm fan-in's direct lane while UAC2 is still absent. The lane's
            # bounded reopen loop can wait for the card; this guarantees a
            # consumer is already standing by before the gadget advertises the
            # host-visible endpoint.
            if ops.usb_audio_present() and not ops.usb_direct_present():
                # Repair an unsafe pre-coordinator/old-boot state first. The
                # gadget's three-way gate now suppresses UAC2 while the live
                # direct lane is absent, so this restart withdraws audio but
                # preserves the NCM management link.
                rc, detail = ops.run_unit(gadget, "restart")
                _check_result(rc, detail, f"systemctl restart {gadget}")
                if ops.usb_audio_present():
                    raise RuntimeError(
                        "USB audio remained advertised without a direct consumer"
                    )
            if not ops.usb_direct_present():
                rc, detail = ops.run_unit(_USB_COUPLING_UNIT, "start")
                _check_result(rc, detail, f"systemctl start {_USB_COUPLING_UNIT}")
                coupling_started = True
            if not ops.usb_audio_present():
                rc, detail = ops.run_unit(gadget, "restart")
                _check_result(rc, detail, f"systemctl restart {gadget}")
            _ensure_active(ops, unit, True)
            if not ops.usb_audio_present():
                raise RuntimeError("USB audio function did not appear after recompose")
            for attempt in range(_USB_DIRECT_SETTLE_ATTEMPTS):
                if ops.usb_direct_ready():
                    break
                if attempt + 1 < _USB_DIRECT_SETTLE_ATTEMPTS:
                    ops.settle(_USB_DIRECT_SETTLE_SECONDS)
            else:
                raise RuntimeError(
                    "fan-in direct USB capture lane did not become ready"
                )
            return "on"
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            # A failed On transition must not strand a host-visible UAC2
            # endpoint without its fan-in consumer. Preserve canonical desired
            # On, temporarily withdraw the derived readiness mirror, recompose
            # to NCM-only, and disarm coupling. The next pass retries cleanly.
            cleanup_errors: list[str] = []
            _attempt_teardown(
                cleanup_errors,
                f"stop {unit}",
                lambda: _ensure_active(ops, unit, False, force=False),
            )
            _attempt_teardown(
                cleanup_errors,
                f"disable {unit} after failed On",
                lambda: _ensure_enabled(ops, unit, False),
            )
            if ops.usb_audio_present():
                _attempt_teardown(
                    cleanup_errors,
                    f"recompose {gadget} after failed On",
                    lambda: _check_result(
                        *ops.run_unit(gadget, "restart"),
                        f"systemctl restart {gadget}",
                    ),
                )
            if ops.usb_audio_present():
                # A failed NCM-only recompose must never be followed by
                # removing the still-advertised endpoint's consumer. Stop the
                # composite owner as the final safe state before touching
                # direct capture.
                _attempt_teardown(
                    cleanup_errors,
                    f"stop {gadget} after failed rollback",
                    lambda: _check_result(
                        *ops.run_unit(gadget, "stop"),
                        f"systemctl stop {gadget}",
                    ),
                )
            if ops.usb_audio_present():
                cleanup_errors.append(
                    "USB audio function remained after failed-On rollback; "
                    "direct capture left armed"
                )
            elif coupling_started or ops.usb_direct_present():
                _attempt_teardown(
                    cleanup_errors,
                    f"disarm {_USB_COUPLING_UNIT} after failed On",
                    lambda: _check_result(
                        *ops.run_unit(_USB_COUPLING_UNIT, "start"),
                        f"systemctl start {_USB_COUPLING_UNIT}",
                    ),
                )
            detail = f"USB On transition failed: {exc}"
            if cleanup_errors:
                detail += "; fail-closed cleanup: " + "; ".join(cleanup_errors)
            raise RuntimeError(detail) from exc

    # Off and follower parking are safety transitions. Keep going through
    # stop, NCM-only recompose, and coupling disarm even if the derived
    # enablement mirror cannot be repaired.
    teardown_errors: list[str] = []

    _attempt_teardown(
        teardown_errors,
        f"set {unit} enabled={desired}",
        lambda: _ensure_enabled(ops, unit, desired),
    )
    _attempt_teardown(
        teardown_errors,
        f"stop {unit}",
        lambda: _ensure_active(ops, unit, False, force=False),
    )
    if ops.usb_audio_present():
        _attempt_teardown(
            teardown_errors,
            f"recompose {gadget}",
            lambda: _check_result(
                *ops.run_unit(gadget, "restart"),
                f"systemctl restart {gadget}",
            ),
        )
    if ops.usb_audio_present():
        _attempt_teardown(
            teardown_errors,
            f"stop {gadget} after failed UAC2 withdrawal",
            lambda: _check_result(
                *ops.run_unit(gadget, "stop"),
                f"systemctl stop {gadget}",
            ),
        )
    audio_withdrawn = not ops.usb_audio_present()
    if not audio_withdrawn:
        teardown_errors.append(
            "USB audio function remained after recompose; direct capture left armed"
        )
    # Always converge the persisted fan-in decision, even when the derived
    # unit is already Off and the live fan-in probe is absent. Otherwise stale
    # JASPER_FANIN_USB_DIRECT=enabled can survive a down daemon and re-arm on a
    # later start despite canonical Off/follower parking.
    if audio_withdrawn:
        _attempt_teardown(
            teardown_errors,
            f"start {_USB_COUPLING_UNIT}",
            lambda: _check_result(
                *ops.run_unit(_USB_COUPLING_UNIT, "start"),
                f"systemctl start {_USB_COUPLING_UNIT}",
            ),
        )
    if teardown_errors:
        raise RuntimeError("; ".join(teardown_errors))
    return "parked" if desired else "off"


def withdraw_usbsink_audio_for_fallback(
    *,
    ops: ReconcileOps | None = None,
) -> tuple[bool, str]:
    """Withdraw host-visible UAC2 before the health watcher removes its consumer.

    The periodic fan-in health check owns the decision to disarm a broken
    direct lane, but this coordinator remains the only owner of the USB source
    lifecycle and ConfigFS composition.  The watcher calls this narrow phase
    after publishing its fallback marker and *before* restarting fan-in.  NCM
    therefore survives the ordinary path while UAC2 is never left advertised
    without a consumer.

    A failed recompose leaves the direct lane running.  If UAC2 is still
    present, the composite gadget is stopped as the final fail-closed action;
    the caller then refuses to disarm fan-in and reports the failed fallback.
    The caller holds :func:`source_reconcile_lock`; the health wrapper acquires
    it before the coupling lock so this phase cannot race ordinary source work.
    """

    operations = ops or default_reconcile_ops()
    lifecycle = local_source_lifecycle(Source.USBSINK)
    unit = lifecycle.intent_unit
    if unit is None or len(lifecycle.advertise_units) != 1:
        return False, "USB lifecycle declaration is incomplete"
    gadget = lifecycle.advertise_units[0]
    errors: list[str] = []

    _attempt_teardown(
        errors,
        f"stop {unit} before fallback",
        lambda: _ensure_active(operations, unit, False, force=False),
    )
    # This is derived readiness, not household intent.  Canonical desired-On
    # remains in source_intent.env and is surfaced as degraded until a later
    # explicit clear event re-arms the combo.
    _attempt_teardown(
        errors,
        f"disable {unit} before fallback",
        lambda: _ensure_enabled(operations, unit, False),
    )
    if operations.usb_audio_present():
        _attempt_teardown(
            errors,
            f"recompose {gadget} without UAC2",
            lambda: _check_result(
                *operations.run_unit(gadget, "restart"),
                f"systemctl restart {gadget}",
            ),
        )
    if operations.usb_audio_present():
        _attempt_teardown(
            errors,
            f"stop {gadget} after failed UAC2 withdrawal",
            lambda: _check_result(
                *operations.run_unit(gadget, "stop"),
                f"systemctl stop {gadget}",
            ),
        )
    if operations.usb_audio_present():
        errors.append("USB audio function remained after fallback withdrawal")
    return not errors, "; ".join(errors)


def _wait_for_bluetooth_radio(
    ops: ReconcileOps,
    *,
    required: bool = True,
) -> BluetoothRfkillState:
    """Wait briefly for Pi firmware/kernel RF-kill registration."""

    state = ops.rfkill_state()
    for attempt in range(_BLUETOOTH_BLUEZ_ATTEMPTS):
        if state.present:
            return state
        if attempt + 1 < _BLUETOOTH_BLUEZ_ATTEMPTS:
            ops.settle(_BLUETOOTH_SETTLE_SECONDS)
            state = ops.rfkill_state()
    if required:
        raise RuntimeError("Bluetooth radio did not appear before the settle deadline")
    return state


def _rfkill_converge(ops: ReconcileOps, blocked: bool) -> bool:
    state = _wait_for_bluetooth_radio(ops)
    if not blocked and state.hard_blocked:
        raise RuntimeError("Bluetooth radio is hardware-blocked")
    converged = state.fully_soft_blocked if blocked else not state.soft_blocked
    if converged:
        return False
    rc, detail = ops.set_rfkill_blocked(blocked)
    _check_result(
        rc,
        detail,
        f"rfkill {'block' if blocked else 'unblock'} bluetooth",
    )
    state = _wait_for_bluetooth_radio(ops)
    converged = state.fully_soft_blocked if blocked else not state.soft_blocked
    if not converged:
        raise RuntimeError(f"Bluetooth RF-kill did not converge to blocked={blocked}")
    if not blocked and state.hard_blocked:
        raise RuntimeError("Bluetooth radio is hardware-blocked")
    return True


def _bluez_power_converge(ops: ReconcileOps, desired: bool) -> bool:
    """Retry the bounded Adapter1 transition while hci settles after RF-kill."""

    detail = "BlueZ adapter unavailable"
    changed = False
    for attempt in range(_BLUETOOTH_SETTLE_ATTEMPTS):
        if ops.bluez_powered() is desired:
            return changed
        rc, attempt_detail = ops.set_bluez_powered(desired)
        changed = True
        if rc != 0:
            detail = attempt_detail or f"rc={rc}"
        if attempt + 1 < _BLUETOOTH_SETTLE_ATTEMPTS:
            ops.settle(_BLUETOOTH_SETTLE_SECONDS)
    if ops.bluez_powered() is desired:
        return changed
    raise RuntimeError(
        f"BlueZ Powered did not converge to {str(desired).lower()}: {detail}"
    )


def _reconcile_bluetooth(
    desired: bool,
    allowed: bool,
    ops: ReconcileOps,
) -> str:
    lifecycle = local_source_lifecycle(Source.BLUETOOTH)
    effective_on = desired and allowed
    start_order = (
        "bluealsa.service",
        "bluealsa-aplay.service",
        "bt-agent.service",
    )
    stop_order = tuple(reversed(start_order))
    if set(start_order) != set(lifecycle.runtime_units):
        raise RuntimeError("Bluetooth lifecycle declaration is incomplete")

    # systemd enablement is derived state. Mirroring household intent onto all
    # three units preserves boot state; role park/restore still comes back
    # through this same coordinator rather than teaching grouping Bluetooth.
    teardown_errors: list[str] = []

    if effective_on:
        for unit in start_order:
            _ensure_enabled(ops, unit, True)
    else:
        for unit in start_order:
            _attempt_teardown(
                teardown_errors,
                f"set {unit} enabled={desired}",
                partial(_ensure_enabled, ops, unit, desired),
            )

    def reconcile_accessories() -> None:
        # Optional Bluetooth accessories own their own adapter-unit registry.
        # Ask that owner to converge after the radio/source intent changes;
        # keep its concrete units out of this source lifecycle coordinator.
        if not ops.unit_available(_ACCESSORY_RECONCILE_UNIT):
            return
        # A voice-start boot transaction may already be activating this
        # oneshot. The first start can therefore join a pass that read the old
        # intent; a second start after it completes guarantees one fresh pass
        # began after this coordinator changed the radio/source state.
        for _ in range(2):
            rc, detail = ops.run_unit(_ACCESSORY_RECONCILE_UNIT, "start")
            _check_result(
                rc,
                detail,
                f"systemctl start {_ACCESSORY_RECONCILE_UNIT}",
            )

    if desired and not allowed:
        # Parking suppresses local playback/advertising, not the shared radio.
        # In particular, do not create a new RF-kill block that grouping's
        # ordinary start-if-enabled restore has no authority to clear.
        for unit in stop_order:
            _attempt_teardown(
                teardown_errors,
                f"stop {unit}",
                partial(
                    _ensure_active,
                    ops,
                    unit,
                    False,
                    force=False,
                ),
            )
        _attempt_teardown(
            teardown_errors,
            "reconcile Bluetooth accessories",
            reconcile_accessories,
        )
        if teardown_errors:
            raise RuntimeError("; ".join(teardown_errors))
        return "parked"

    if effective_on:
        _ensure_active(ops, _BLUETOOTH_SERVICE, True)
        _wait_for_bluetooth_radio(ops)
        _rfkill_converge(ops, False)
        _bluez_power_converge(ops, True)
        for unit in start_order:
            _ensure_active(ops, unit, True)
        reconcile_accessories()
        return "on"

    # Off is fail-closed: attempt every safe teardown step even if an earlier
    # service refuses to stop. RF-kill is the strongest final guard, so a
    # bluealsa or BlueZ failure must never prevent that attempt.
    for unit in stop_order:
        _attempt_teardown(
            teardown_errors,
            f"stop {unit}",
            partial(
                _ensure_active,
                ops,
                unit,
                False,
                force=False,
            ),
        )
    _attempt_teardown(
        teardown_errors,
        f"start {_BLUETOOTH_SERVICE} control plane",
        lambda: _ensure_active(ops, _BLUETOOTH_SERVICE, True),
    )
    radio_state: BluetoothRfkillState | None = None
    try:
        # A conclusively absent adapter is already safe-Off. Read/probe errors
        # still fail loudly, but absence itself must not make a household Off
        # request fail after all source-owned services were torn down.
        radio_state = _wait_for_bluetooth_radio(ops, required=False)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        teardown_errors.append(f"wait for Bluetooth radio: {exc}")
    if radio_state is not None and radio_state.present:
        _attempt_teardown(
            teardown_errors,
            "set BlueZ Powered=false",
            lambda: _bluez_power_converge(ops, False),
        )
        _attempt_teardown(
            teardown_errors,
            "RF-kill Bluetooth",
            lambda: _rfkill_converge(ops, True),
        )
    _attempt_teardown(
        teardown_errors,
        "reconcile Bluetooth accessories",
        reconcile_accessories,
    )
    if teardown_errors:
        raise RuntimeError("; ".join(teardown_errors))
    return "off"


def _apply_source(
    source: Source,
    desired: bool,
    allowed: bool,
    ops: ReconcileOps,
) -> str:
    if source == Source.USBSINK:
        return _reconcile_usbsink(desired, allowed, ops)
    if source == Source.BLUETOOTH:
        return _reconcile_bluetooth(desired, allowed, ops)
    # Ordinary sources are selected by their lifecycle declaration, not a
    # second central enum set.  This is deliberately only a dispatch rule—not
    # a plugin API: USB and Bluetooth keep their concrete ordered appliers,
    # while any declared source with one intent unit uses the common systemd
    # mechanism without another coordinator edit.
    if local_source_lifecycle(source).intent_unit is not None:
        return _reconcile_systemd_source(source, desired, allowed, ops)
    raise RuntimeError(f"unsupported source {source.value}")


def _reconcile_once(
    *,
    env_path: str = SOURCE_INTENT_ENV,
    ops: ReconcileOps | None = None,
    status_path: str | None = None,
    status_writer: StatusWriter | None = None,
) -> int:
    """Converge every declared source while the process lock is held.

    The operation bundle is the only injection seam.  Full convergence always
    handles both persistent enablement and runtime state, so there is no
    separate deploy-only stop mode.
    """

    operations = ops or default_reconcile_ops()

    try:
        text = _read_intent(env_path)
    except RuntimeError as exc:
        log_event(
            logger,
            "source_intent.read_failed",
            error=str(exc),
            level=logging.WARNING,
        )
        failure_outcomes = {
            source.value: {
                "desired": "unknown",
                "effective": "degraded",
                "result": "failed",
                "reason": str(exc)[:300],
            }
            for source in source_intent_sources()
        }
        _publish_reconcile_status(
            path=status_path,
            intent_fingerprint="",
            outcomes=failure_outcomes,
            writer=status_writer,
        )
        return 1

    fingerprint = _intent_fingerprint(text)
    intents, problems = _parse_source_intents(text)
    failures = len(problems)
    outcomes: dict[str, dict[str, str]] = {}
    invalid_sources = {
        problem.source
        for problem in problems
        if problem.event == "source_intent.bad_value" and problem.source is not None
    }
    for problem in problems:
        fields: dict[str, Any] = {}
        if problem.source is not None:
            fields["source"] = problem.source.value
        if problem.key:
            fields["key"] = problem.key
        if problem.value:
            fields["value"] = problem.value
        log_event(logger, problem.event, level=logging.WARNING, **fields)

    allowed = operations.local_sources_allowed()
    applied = 0
    for source in source_intent_sources():
        if source not in intents:
            continue
        desired = intents[source]
        desired_label = (
            "invalid"
            if source in invalid_sources
            else "enabled"
            if desired
            else "disabled"
        )
        try:
            effective = _apply_source(source, desired, allowed, operations)
        except (
            DBusError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as exc:
            failures += 1
            failure_reason = str(exc)[:300]
            outcomes[source.value] = {
                "desired": desired_label,
                "effective": "degraded",
                "result": "failed",
                "reason": failure_reason,
            }
            log_event(
                logger,
                "source.reconcile",
                source=source.value,
                desired=desired_label,
                effective="degraded",
                result="failed",
                reason=failure_reason,
                level=logging.WARNING,
            )
            continue
        applied += 1
        if source in invalid_sources:
            outcomes[source.value] = {
                "desired": "invalid",
                "effective": effective,
                "result": "failed",
                "reason": "invalid_intent_fail_closed",
            }
            log_event(
                logger,
                "source.reconcile",
                source=source.value,
                desired="invalid",
                effective=effective,
                result="failed",
                reason="invalid_intent_fail_closed",
                level=logging.WARNING,
            )
        else:
            outcomes[source.value] = {
                "desired": desired_label,
                "effective": effective,
                "result": "ok",
                "reason": "",
            }
            log_event(
                logger,
                "source.reconcile",
                source=source.value,
                desired=desired_label,
                effective=effective,
                result="ok",
            )

    if not _publish_reconcile_status(
        path=status_path,
        intent_fingerprint=fingerprint,
        outcomes=outcomes,
        writer=status_writer,
    ):
        failures += 1
    log_event(
        logger,
        "source_intent.reconciled",
        applied=applied,
        failures=failures,
    )
    return 1 if failures else 0


def reconcile(
    *,
    env_path: str = SOURCE_INTENT_ENV,
    ops: ReconcileOps | None = None,
    status_path: str | None = None,
    status_writer: StatusWriter | None = None,
) -> int:
    """Serialize and converge every source to the latest persisted intent.

    systemd, boot, deploy, and direct operator invocations all share this lock.
    The intent read happens after acquisition, so two coordinator processes can
    never apply opposite snapshots concurrently.
    """

    try:
        with source_reconcile_lock(env_path=env_path):
            return _reconcile_once(
                env_path=env_path,
                ops=ops,
                status_path=status_path,
                status_writer=status_writer,
            )
    except TimeoutError as exc:
        log_event(
            logger,
            "source_intent.lock_timeout",
            error=str(exc),
            level=logging.WARNING,
        )
        return 1


def source_reconcile_lock(
    *,
    env_path: str = SOURCE_INTENT_ENV,
    timeout_sec: float = SOURCE_RECONCILE_LOCK_TIMEOUT_SECONDS,
):
    """Return the shared source-lifecycle reconcile lock context.

    Cross-subsystem callers that must compose source lifecycle work with a
    second reconciler acquire this lock first.  In particular, the fan-in
    combo health fallback then acquires its coupling lock, preserving the one
    global order ``source -> coupling`` used by :func:`reconcile`.
    """

    return advisory_file_lock(
        f"{env_path}.reconcile.lock",
        mode=_SHARED_LOCK_MODE,
        group_from_parent=True,
        timeout_sec=timeout_sec,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-source-intent-reconcile",
        description="Converge local music sources to persisted household intent.",
    )
    parser.add_argument("--env-path", default=SOURCE_INTENT_ENV)
    parser.add_argument("--status-path", default=SOURCE_STATUS_PATH)
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.reason:
        log_event(logger, "source_intent.begin", reason=args.reason)
    try:
        Path(args.status_path).parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    except OSError as exc:
        log_event(
            logger,
            "source_intent.status_dir_failed",
            path=str(Path(args.status_path).parent),
            error=str(exc),
            level=logging.ERROR,
        )
        return 1
    return reconcile(env_path=args.env_path, status_path=args.status_path)


if __name__ == "__main__":
    raise SystemExit(main())
