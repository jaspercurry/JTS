# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared CamillaDSP config validation and apply lifecycle.

Generated DSP configs are safety-critical: a bad reload can silence
music, leave room correction half-applied, or make the UI lie about
what the speaker is actually running. This module keeps the lifecycle
small and explicit:

1. validate the candidate without changing live audio
2. load through CamillaDSP's runtime API
3. confirm/persist only after the runtime accepted it
4. roll back to the prior config on post-load failure
5. write a compact last-result record for operators and doctor

It intentionally has no heavy audio/science dependencies.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import inspect
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_DSP_APPLY_STATE_PATH = Path("/var/lib/jasper/dsp_apply_state.json")
DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S = 10.0
DEFAULT_DSP_WRITER_LOCK_POLL_INTERVAL_S = 0.05
CANONICAL_CAMILLA_CONFIG_DIR = Path("/var/lib/camilladsp/configs")
CANONICAL_DSP_WRITER_LOCK_PATH = CANONICAL_CAMILLA_CONFIG_DIR / ".dsp_apply.lock"


class BassExtensionApplyPending(RuntimeError):
    """A graph mutation was refused while durable bass rollback is pending."""


@dataclass(frozen=True)
class _DspLockOwnership:
    path: Path
    task: asyncio.Task[Any]
    recovery_permitted: bool


_DSP_LOCK_OWNERSHIP: ContextVar[_DspLockOwnership | None] = ContextVar(
    "jasper_dsp_lock_ownership", default=None
)


class DspWriterLockTimeout(TimeoutError):
    """The shared DSP writer boundary was not admitted before its deadline."""

    def __init__(
        self,
        lock_path: str | Path,
        *,
        timeout_s: float,
        waited_s: float,
        source: str,
    ) -> None:
        super().__init__(
            f"DSP writer lock was unavailable after {waited_s:.3f}s "
            f"(deadline {timeout_s:.3f}s)"
        )
        self.lock_path = Path(lock_path)
        self.timeout_s = timeout_s
        self.waited_s = waited_s
        self.source = source


class ValidationStatus(str, Enum):
    VALID = "valid"
    INVALID_CONFIG = "invalid_config"
    RUNNER_ERROR = "runner_error"
    MISSING = "missing"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class CamillaConfigValidationResult:
    """Result of a no-side-effect CamillaDSP config preflight."""

    status: ValidationStatus
    path: str
    argv: list[str] = field(default_factory=list)
    returncode: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None

    @property
    def ok_to_apply(self) -> bool:
        # Missing binary is allowed so dev machines without CamillaDSP can
        # still exercise the emitters; the live websocket reload remains
        # authoritative on the Pi.
        return self.status in {ValidationStatus.VALID, ValidationStatus.MISSING}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass
class DspApplyState:
    schema_version: int
    op_id: str
    source: str
    phase: str
    result: str
    started_at: str
    finished_at: str | None
    prior_config_path: str | None
    candidate_config_path: str
    active_config_path: str | None = None
    config_sha256: str | None = None
    room_peq_count: int | None = None
    sound_filter_count: int | None = None
    validator: dict[str, Any] | None = None
    prepare_error: str | None = None
    load_error: str | None = None
    persist_error: str | None = None
    confirm_error: str | None = None
    rollback_attempted: bool = False
    rollback_succeeded: bool | None = None
    rollback_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DspApplyError(RuntimeError):
    """Raised when a DSP apply transaction fails.

    ``state`` contains the persisted failure details and is safe to expose
    through status endpoints.
    """

    def __init__(self, message: str, state: DspApplyState) -> None:
        super().__init__(message)
        self.state = state


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _tail(text: str | bytes, limit: int = 1000) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-limit:] if len(text) > limit else text


def _camilladsp_binary() -> str | None:
    binary = os.environ.get("JASPER_CAMILLADSP_BIN")
    if binary:
        return binary
    default_binary = Path("/opt/camilladsp/camilladsp")
    if default_binary.exists():
        return str(default_binary)
    return shutil.which("camilladsp")


def _volume_limit_safety_error(cfg_path: Path) -> str | None:
    """Return an error string when the config's ``devices.volume_limit``
    violates the JTS 0 dB safety ceiling, else None.

    CamillaDSP's own ``--check`` accepts a positive limit (it's legal
    Camilla config) and *defaults the main fader's maximum to +50 dB
    when the key is omitted* — both are loud-output hazards on a JTS
    speaker, so the apply gate rejects them here. Fail-open on an
    unreadable file: the load step will fail loudly on its own, and a
    read race must not invent a safety verdict.
    """
    from jasper.camilla_config_contract import parse_camilla_devices_config

    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError:
        return None
    limit = parse_camilla_devices_config(text).get("volume_limit")
    if limit is None:
        return (
            "config omits devices.volume_limit; CamillaDSP would default "
            "the main fader ceiling above 0 dB"
        )
    if limit > 0:
        return (
            f"devices.volume_limit={limit:.1f} dB exceeds the 0 dB JTS safety ceiling"
        )
    return None


def validate_camilla_config(path: str | Path) -> CamillaConfigValidationResult:
    """Validate a config using CamillaDSP's CLI contract, plus the JTS
    ``devices.volume_limit`` safety ceiling (which Camilla's own
    ``--check`` does not enforce).

    CamillaDSP treats the config file as a positional argument and
    ``-c``/``--check`` as the validation flag. Keep the exact argv tested:
    a prior regression passed the config as ``-c``'s argument and then
    passed ``--check`` again, making every generated config look invalid.
    """

    cfg_path = Path(path)
    limit_error = _volume_limit_safety_error(cfg_path)
    if limit_error:
        log_event(
            logger,
            "dsp.validate",
            result="volume_limit_rejected",
            path=cfg_path,
            err=limit_error,
            level=logging.ERROR,
        )
        return CamillaConfigValidationResult(
            status=ValidationStatus.INVALID_CONFIG,
            path=str(cfg_path),
            error=limit_error,
        )
    binary = _camilladsp_binary()
    if not binary:
        logger.info("camilladsp binary not found; skipping config preflight")
        return CamillaConfigValidationResult(
            status=ValidationStatus.MISSING,
            path=str(cfg_path),
            error="camilladsp binary not found",
        )

    argv = [binary, "--check", str(cfg_path)]
    try:
        result = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return CamillaConfigValidationResult(
            status=ValidationStatus.TIMEOUT,
            path=str(cfg_path),
            argv=argv,
            stdout_tail=_tail(e.stdout or ""),
            stderr_tail=_tail(e.stderr or ""),
            error=str(e),
        )
    except OSError as e:
        return CamillaConfigValidationResult(
            status=ValidationStatus.RUNNER_ERROR,
            path=str(cfg_path),
            argv=argv,
            error=str(e),
        )

    status = ValidationStatus.VALID
    if result.returncode != 0:
        status = ValidationStatus.INVALID_CONFIG
        if result.returncode == 2 and "Usage:" in result.stderr:
            status = ValidationStatus.RUNNER_ERROR
    validation = CamillaConfigValidationResult(
        status=status,
        path=str(cfg_path),
        argv=argv,
        returncode=result.returncode,
        stdout_tail=_tail(result.stdout),
        stderr_tail=_tail(result.stderr),
    )
    if status == ValidationStatus.VALID:
        return validation
    logger.error(
        "camilladsp preflight failed: path=%s status=%s rc=%s stdout=%r stderr=%r",
        cfg_path,
        status.value,
        result.returncode,
        validation.stdout_tail,
        validation.stderr_tail,
    )
    return validation


def _state_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_DSP_APPLY_STATE_PATH")
        or DEFAULT_DSP_APPLY_STATE_PATH
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def record_dsp_apply_state(
    state: DspApplyState,
    *,
    state_path: str | Path | None = None,
) -> None:
    """Persist the latest DSP apply result.

    Fail-soft: observability must never make an otherwise successful
    audio apply fail.
    """

    path = _state_path(state_path)
    try:
        _atomic_write_json(path, state.to_dict())
    except (OSError, TypeError) as e:
        log_event(
            logger,
            "dsp.apply_state_write_failed",
            path=path,
            err=repr(e),
            level=logging.WARNING,
        )


def last_dsp_apply_state(
    *,
    state_path: str | Path | None = None,
) -> dict[str, Any] | None:
    path = _state_path(state_path)
    try:
        blob = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return blob if isinstance(blob, dict) else None


def dsp_write_epoch_from_state(state: dict[str, Any] | None) -> str:
    """Return the durable DSP-write epoch encoded by the latest apply state."""

    if not state:
        return "none"
    op_id = state.get("op_id")
    return str(op_id) if op_id else "none"


def dsp_write_epoch(*, state_path: str | Path | None = None) -> str:
    """Return the current durable DSP-write epoch.

    Live/non-durable DSP surfaces use this as a stale-write fence. Durable
    applies record a new op_id, so a live request that was created before a
    save or room-correction apply can be skipped before it touches audio.
    """

    return dsp_write_epoch_from_state(last_dsp_apply_state(state_path=state_path))


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any | None = None

    def try_acquire(self) -> bool:
        """Attempt one non-blocking acquisition.

        Keeping the operation synchronous is intentional: ``LOCK_NB`` cannot
        wait in the kernel, so cancellation cannot strand a worker which later
        acquires the lock after its coroutine has gone away.
        """

        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o660)
            try:
                # The generated-config directory is root:jasper setgid; the lock
                # has to be writable by whichever process reaches the apply path
                # first. O_CREAT still respects umask, so publish the intended
                # mode explicitly.
                try:
                    os.fchmod(fd, 0o660)
                except OSError:
                    pass
                self._fh = os.fdopen(fd, "a+", encoding="utf-8")
            except Exception:  # noqa: BLE001
                os.close(fd)
                raise
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def _positive_finite(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{field_name} must be a positive finite number")
    return result


@contextlib.asynccontextmanager
async def _dsp_apply_lock(
    path: Path,
    *,
    timeout_s: float = DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_DSP_WRITER_LOCK_POLL_INTERVAL_S,
    source: str = "unspecified",
    allow_pending_bass_extension_recovery: bool = False,
    bass_extension_intent_path: str | Path | None = None,
):
    timeout = _positive_finite(timeout_s, field_name="timeout_s")
    poll_interval = _positive_finite(
        poll_interval_s,
        field_name="poll_interval_s",
    )
    if not isinstance(source, str) or not source or source != source.strip():
        raise ValueError("source must be non-empty trimmed text")
    from jasper.bass_extension import BASS_EXTENSION_APPLY_INTENT_PATH

    task = asyncio.current_task()
    if task is None:  # pragma: no cover - an async context always has a task
        raise RuntimeError("DSP writer lock requires an asyncio task")
    intent_path = Path(
        bass_extension_intent_path or BASS_EXTENSION_APPLY_INTENT_PATH
    )
    owned = _DSP_LOCK_OWNERSHIP.get()
    if owned is not None and owned.task is task and owned.path == path:
        permitted = owned.recovery_permitted
        if intent_path.exists() and not permitted:
            raise BassExtensionApplyPending(
                "bass-extension rollback is pending; graph mutation refused"
            )
        yield
        return

    lock = _FileLock(path)
    started = time.monotonic()
    deadline = started + timeout
    contended = False
    first_attempt = True
    admitted = False
    try:
        while True:
            # The first non-blocking attempt is immediate. Every retry checks
            # its monotonic deadline before touching flock, so an event-loop
            # stall cannot turn a timed-out waiter into a late owner.
            if not first_attempt and time.monotonic() >= deadline:
                acquired = False
            else:
                acquired = lock.try_acquire()
            first_attempt = False
            if acquired and time.monotonic() < deadline:
                admitted = True
                break
            if acquired:
                # The local open/flock attempt itself crossed the deadline.
                # Release in the outer finally and report no admission.
                lock.release()
            if not contended:
                contended = True
                log_event(
                    logger,
                    "dsp.writer_lock",
                    result="waiting",
                    source=source,
                    timeout_ms=round(timeout * 1000),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                waited = max(0.0, time.monotonic() - started)
                log_event(
                    logger,
                    "dsp.writer_lock",
                    result="timeout",
                    source=source,
                    wait_ms=round(waited * 1000),
                    timeout_ms=round(timeout * 1000),
                    level=logging.WARNING,
                )
                raise DspWriterLockTimeout(
                    path,
                    timeout_s=timeout,
                    waited_s=waited,
                    source=source,
                )
            await asyncio.sleep(min(poll_interval, remaining))
        if contended:
            log_event(
                logger,
                "dsp.writer_lock",
                result="acquired",
                source=source,
                wait_ms=round(max(0.0, time.monotonic() - started) * 1000),
            )
        if intent_path.exists() and not allow_pending_bass_extension_recovery:
            raise BassExtensionApplyPending(
                "bass-extension rollback is pending; graph mutation refused"
            )
        token = _DSP_LOCK_OWNERSHIP.set(
            _DspLockOwnership(path, task, allow_pending_bass_extension_recovery)
        )
        try:
            yield
        finally:
            _DSP_LOCK_OWNERSHIP.reset(token)
    except asyncio.CancelledError:
        if contended and not admitted:
            log_event(
                logger,
                "dsp.writer_lock",
                result="cancelled",
                source=source,
                wait_ms=round(max(0.0, time.monotonic() - started) * 1000),
            )
        raise
    finally:
        # Release synchronously so cancellation cannot interrupt ownership
        # cleanup. flock(LOCK_UN) and close are local, non-blocking operations.
        lock.release()


@contextlib.asynccontextmanager
async def _maybe_dsp_apply_lock(
    path: Path,
    *,
    acquire: bool = True,
    timeout_s: float = DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S,
    source: str = "unspecified",
):
    # ``acquire`` is the historical nested-call hint.  The private admission
    # boundary is now authoritative: it reuses same-task ownership and acquires
    # otherwise, so a stale/incorrect ``False`` can never bypass the lock or
    # pending-intent check. The pending-intent guard uses ``_dsp_apply_lock``'s
    # own defaults (recovery off, canonical intent path).
    async with _dsp_apply_lock(
        path,
        timeout_s=timeout_s,
        source=source,
    ):
        yield


def dsp_apply_lock_path(config_dir: str | Path) -> Path:
    """Return the shared local lock path for generated CamillaDSP configs."""

    return Path(config_dir) / ".dsp_apply.lock"


def _production_or_pytest_lock_path(config_dir: str | Path) -> Path:
    """Return the canonical production lock or an injected pytest-local lock."""

    directory = Path(config_dir)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            directory.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        except (OSError, ValueError):
            pass
        else:
            return dsp_apply_lock_path(directory)
    return CANONICAL_DSP_WRITER_LOCK_PATH


def _default_apply_lock_path(candidate: Path) -> Path:
    """Use the fixed production lock, retaining pytest temp-path injection."""

    return _production_or_pytest_lock_path(candidate.parent)


@contextlib.asynccontextmanager
async def dsp_writer_lock(
    config_dir: str | Path,
    *,
    source: str,
    timeout_s: float = DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S,
    allow_pending_bass_extension_recovery: bool = False,
    bass_extension_intent_path: str | Path | None = None,
):
    """Serialize JTS DSP writers with bounded, cancellation-safe admission."""

    async with _dsp_apply_lock(
        _production_or_pytest_lock_path(config_dir),
        timeout_s=timeout_s,
        source=source,
        allow_pending_bass_extension_recovery=(
            allow_pending_bass_extension_recovery
        ),
        bass_extension_intent_path=bass_extension_intent_path,
    ):
        yield


@contextlib.asynccontextmanager
async def camilla_graph_mutation(
    *,
    source: str,
    lock_path: str | Path = CANONICAL_DSP_WRITER_LOCK_PATH,
    bass_extension_intent_path: str | Path | None = None,
):
    """Admit one CamillaDSP graph mutation at the global writer boundary."""

    async with _dsp_apply_lock(
        Path(lock_path),
        source=source,
        bass_extension_intent_path=bass_extension_intent_path,
    ):
        yield


async def _maybe_call(fn: Callable[[], Any] | None) -> Any:
    if fn is None:
        return None
    value = fn()
    if inspect.isawaitable(value):
        return await value
    return value


async def _rollback(
    *,
    state: DspApplyState,
    load_config: Callable[[str], Awaitable[bool]],
    prior_config_path: str | None,
) -> None:
    if not prior_config_path:
        return
    state.rollback_attempted = True
    try:
        ok = await load_config(prior_config_path)
    except Exception as e:  # noqa: BLE001
        state.rollback_succeeded = False
        state.rollback_error = str(e)
        log_event(
            logger,
            "dsp.apply",
            op_id=state.op_id,
            source=state.source,
            phase="rollback",
            result="error",
            prior=prior_config_path,
            level=logging.ERROR,
            exc_info=True,
        )
        return
    state.rollback_succeeded = bool(ok)
    if not ok:
        state.rollback_error = "CamillaDSP rejected prior config path"


async def apply_dsp_config(
    *,
    source: str,
    candidate_path: str | Path,
    load_config: Callable[[str], Awaitable[bool]],
    prior_config_path: str | Path | None = None,
    get_current_config_path: Callable[[], Awaitable[str | None]] | None = None,
    prepare: Callable[[], Any] | None = None,
    persist: Callable[[], Any] | None = None,
    room_peq_count: int | None = None,
    sound_filter_count: int | None = None,
    state_path: str | Path | None = None,
    lock_path: str | Path | None = None,
    acquire_lock: bool = True,
    lock_timeout_s: float = DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S,
    expected_candidate_sha256: str | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> DspApplyState:
    """Validate and load a generated CamillaDSP config.

    The lock is local-process/file-system coordination for JTS writers
    (`/sound/`, `/correction/`, and future DSP surfaces). CamillaDSP is
    still the authority for whether the candidate can actually run.

    ``acquire_lock=False`` remains a compatibility hint for callers that expect
    to be inside :func:`dsp_writer_lock`.  The private boundary verifies that
    task-local ownership and acquires the same lock when it is absent; the flag
    cannot bypass admission or pending-intent refusal.

    ``lock_timeout_s`` bounds only admission to the shared writer boundary.
    Once admitted, this function runs validation, mutation, confirmation, and
    rollback to their terminal result without an outer transaction deadline.

    When ``expected_candidate_sha256`` is provided, the candidate is hashed
    again after validation and immediately before load. A changed file is
    refused without asking CamillaDSP to load it.
    """

    candidate = Path(candidate_path)
    lock = (
        Path(lock_path)
        if lock_path is not None
        else _default_apply_lock_path(candidate)
    )
    state = DspApplyState(
        schema_version=1,
        op_id=uuid.uuid4().hex,
        source=source,
        phase="start",
        result="in_progress",
        started_at=_utc_now(),
        finished_at=None,
        prior_config_path=str(prior_config_path) if prior_config_path else None,
        candidate_config_path=str(candidate),
        room_peq_count=room_peq_count,
        sound_filter_count=sound_filter_count,
    )

    async with _maybe_dsp_apply_lock(
        lock,
        acquire=acquire_lock,
        timeout_s=lock_timeout_s,
        source=source,
    ):
        if prepare is not None:
            state.phase = "prepare"
            record_dsp_apply_state(state, state_path=state_path)
            try:
                metadata = await _maybe_call(prepare)
                if isinstance(metadata, dict):
                    if (
                        metadata.get("prior_config_path")
                        and not state.prior_config_path
                    ):
                        state.prior_config_path = str(metadata["prior_config_path"])
                    if "room_peq_count" in metadata:
                        state.room_peq_count = metadata["room_peq_count"]
                    if "sound_filter_count" in metadata:
                        state.sound_filter_count = metadata["sound_filter_count"]
            except Exception as e:  # noqa: BLE001
                state.result = "prepare_failed"
                state.prepare_error = str(e)
                state.finished_at = _utc_now()
                record_dsp_apply_state(state, state_path=state_path)
                raise DspApplyError(f"DSP config preparation failed: {e}", state) from e

        state.config_sha256 = _sha256(candidate)

        if state.prior_config_path is None and get_current_config_path is not None:
            state.phase = "snapshot"
            try:
                current = await get_current_config_path()
                state.prior_config_path = str(current) if current else None
            except Exception as e:  # noqa: BLE001
                log_event(
                    logger,
                    "dsp.apply",
                    op_id=state.op_id,
                    source=source,
                    phase="snapshot",
                    result="error",
                    err=repr(e),
                    level=logging.WARNING,
                )

        state.phase = "validate"
        validation = validate(candidate)
        state.validator = validation.to_dict()
        if not validation.ok_to_apply:
            state.result = validation.status.value
            state.finished_at = _utc_now()
            record_dsp_apply_state(state, state_path=state_path)
            raise DspApplyError(
                _validation_failure_message(validation),
                state,
            )

        if expected_candidate_sha256 is not None:
            state.phase = "proof"
            state.config_sha256 = _sha256(candidate)
            if (
                not expected_candidate_sha256
                or state.config_sha256 != expected_candidate_sha256
            ):
                state.result = "candidate_changed"
                state.finished_at = _utc_now()
                record_dsp_apply_state(state, state_path=state_path)
                raise DspApplyError(
                    "DSP candidate changed after validation and before load",
                    state,
                )

        state.phase = "load"
        record_dsp_apply_state(state, state_path=state_path)
        try:
            ok = await load_config(str(candidate))
            if not ok:
                raise RuntimeError("CamillaDSP rejected candidate config path")
        except Exception as e:  # noqa: BLE001
            state.load_error = str(e)
            await _rollback(
                state=state,
                load_config=load_config,
                prior_config_path=state.prior_config_path,
            )
            state.result = _rollback_result("load_failed", state)
            state.finished_at = _utc_now()
            record_dsp_apply_state(state, state_path=state_path)
            raise DspApplyError(f"CamillaDSP reload failed: {e}", state) from e

        if get_current_config_path is not None:
            state.phase = "confirm"
            try:
                active = await get_current_config_path()
                state.active_config_path = active
                if active and Path(active) != candidate:
                    raise RuntimeError(
                        f"active config is {active}, expected {candidate}"
                    )
            except Exception as e:  # noqa: BLE001
                state.confirm_error = str(e)
                await _rollback(
                    state=state,
                    load_config=load_config,
                    prior_config_path=state.prior_config_path,
                )
                state.result = _rollback_result("confirm_failed", state)
                state.finished_at = _utc_now()
                record_dsp_apply_state(state, state_path=state_path)
                raise DspApplyError(
                    f"CamillaDSP reload confirmation failed: {e}", state
                ) from e

        state.phase = "persist"
        try:
            await _maybe_call(persist)
        except Exception as e:  # noqa: BLE001
            state.persist_error = str(e)
            await _rollback(
                state=state,
                load_config=load_config,
                prior_config_path=state.prior_config_path,
            )
            state.result = _rollback_result("persist_failed", state)
            state.finished_at = _utc_now()
            record_dsp_apply_state(state, state_path=state_path)
            raise DspApplyError(
                f"DSP config applied but state persistence failed: {e}", state
            ) from e

        if state.active_config_path is None:
            state.active_config_path = str(candidate)
        state.phase = "done"
        state.result = "success"
        state.finished_at = _utc_now()
        record_dsp_apply_state(state, state_path=state_path)
        log_event(
            logger,
            "dsp.apply",
            op_id=state.op_id,
            source=source,
            result="success",
            candidate=candidate,
        )
        return state


def _rollback_result(prefix: str, state: DspApplyState) -> str:
    if not state.rollback_attempted:
        return prefix
    if state.rollback_succeeded:
        return f"{prefix}_rolled_back"
    return f"{prefix}_rollback_failed"


def _validation_failure_message(result: CamillaConfigValidationResult) -> str:
    if result.status == ValidationStatus.INVALID_CONFIG:
        return f"generated CamillaDSP config is invalid: {result.path}"
    if result.status == ValidationStatus.TIMEOUT:
        return f"CamillaDSP config validator timed out: {result.path}"
    return (
        "CamillaDSP config validator could not run cleanly "
        f"({result.status.value}): {result.path}"
    )
