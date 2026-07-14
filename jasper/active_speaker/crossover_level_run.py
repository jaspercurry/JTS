# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable identity and timeout correlation for crossover level runs.

The correction relay owns phone transport and the shared measurement kernel
owns ramp math.  This Active-owned state machine binds those two asynchronous
surfaces to one exact request without retaining relay links or credentials.
It is deliberately separate from the crossover volume-safety latch: run state
explains progress and deduplicates dispatch, while the volume latch remains the
authority for recovery after any possible listening-level mutation.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.ramp import MeasurementRamp
from jasper.log_event import log_event

SCHEMA_VERSION = 1
REQUEST_KIND = "jts_active_crossover_level_run_request"
STATE_KIND = "jts_active_crossover_level_run_state"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_crossover_level_run.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_CROSSOVER_LEVEL_RUN_STATE"
PHONE_TRANSPORT_GRACE_S = 30.0

logger = logging.getLogger(__name__)
_THREAD_LOCK = threading.RLock()
_UUID_HEX_RE = re.compile(r"[0-9a-f]{32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class CrossoverLevelRunError(RuntimeError):
    """The durable run state or requested transition is unsafe."""


class CrossoverLevelRunConflict(CrossoverLevelRunError):
    """Another exact request currently owns the Active run slot."""


class CrossoverLevelRunPhase(str, Enum):
    """Public phases for one exact run."""

    AWAITING_PHONE = "awaiting_phone"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class CrossoverLevelRunDisposition(str, Enum):
    """Whether a claim may launch transport/backend work."""

    NEW = "new"
    DUPLICATE_ACTIVE = "duplicate_active"
    DUPLICATE_SUCCEEDED = "duplicate_succeeded"


class CrossoverLevelRunFailure(str, Enum):
    """Stable terminal reasons accepted from the Room transport adapter."""

    RELAY_REGISTRATION_FAILED = "relay_registration_failed"
    PHONE_ABORTED = "phone_aborted"
    LEVEL_MATCH_ACTION_FAILED = "level_match_action_failed"
    FINALIZATION_FAILED = "finalization_failed"
    SERVICE_RESTARTED = "service_restarted"


_ACTIVE_PHASES = frozenset(
    {
        CrossoverLevelRunPhase.AWAITING_PHONE.value,
        CrossoverLevelRunPhase.RUNNING.value,
    }
)
_TERMINAL_PHASES = frozenset(
    {
        CrossoverLevelRunPhase.SUCCEEDED.value,
        CrossoverLevelRunPhase.FAILED.value,
        CrossoverLevelRunPhase.INTERRUPTED.value,
    }
)


def state_path(path: str | Path | None = None) -> Path:
    """Resolve the production state path, retaining a test override seam."""

    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CrossoverLevelRunError(f"{field_name} must be a non-empty trimmed string")
    return value


def _sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CrossoverLevelRunError(
            f"{field_name} must be a lowercase SHA-256 fingerprint"
        )
    return value


def _uuid_hex(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise CrossoverLevelRunError(f"{field_name} must be a lowercase UUID hex id")
    return value


def _timestamp(value: Any, *, field_name: str, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    return _text(value, field_name=field_name)


def _ramp_payload(ramp: MeasurementRamp) -> dict[str, Any]:
    payload = asdict(ramp)
    # Round-trip the complete config so any future non-JSON or derived-only
    # field fails at the request boundary instead of changing at execution.
    try:
        rebuilt = MeasurementRamp(**payload)
    except (TypeError, ValueError) as exc:
        raise CrossoverLevelRunError("level-run ramp config is not replayable") from exc
    if asdict(rebuilt) != payload:
        raise CrossoverLevelRunError("level-run ramp config did not round-trip exactly")
    return payload


@dataclass(frozen=True)
class CrossoverLevelRunRequest:
    """One exact target/profile/topology and its frozen ramp configuration."""

    topology_id: str
    protected_profile_fingerprint: str
    target_id: str
    target_fingerprint: str
    geometry: str
    capture_geometry: str
    ramp_config: Mapping[str, Any]
    ramp_config_fingerprint: str = field(init=False)
    safety_timeout_ms: int = field(init=False)
    phone_hard_timeout_ms: int = field(init=False)
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        topology_id = _text(self.topology_id, field_name="topology_id")
        profile = _sha256(
            self.protected_profile_fingerprint,
            field_name="protected_profile_fingerprint",
        )
        target_id = _text(self.target_id, field_name="target_id")
        target_fingerprint = _sha256(
            self.target_fingerprint,
            field_name="target_fingerprint",
        )
        geometry = _text(self.geometry, field_name="geometry")
        capture_geometry = _text(self.capture_geometry, field_name="capture_geometry")
        if capture_geometry not in {"near_field", "reference_axis"}:
            raise CrossoverLevelRunError(
                "capture_geometry must be near_field or reference_axis"
            )
        from .capture_geometry import parse_driver_level_geometry

        try:
            parsed_geometry, speaker_group_id, role = parse_driver_level_geometry(
                geometry
            )
        except ValueError as exc:
            raise CrossoverLevelRunError("level-run geometry is invalid") from exc
        if parsed_geometry != capture_geometry:
            raise CrossoverLevelRunError(
                "capture_geometry does not match the level-run geometry"
            )
        if target_id != f"{speaker_group_id}:{role}":
            raise CrossoverLevelRunError(
                "level-run geometry does not identify the requested driver target"
            )
        if not isinstance(self.ramp_config, Mapping):
            raise CrossoverLevelRunError("ramp_config must be an object")
        try:
            ramp = MeasurementRamp(**dict(self.ramp_config))
        except (TypeError, ValueError) as exc:
            raise CrossoverLevelRunError("ramp_config is invalid") from exc
        ramp_payload = _ramp_payload(ramp)
        if dict(self.ramp_config) != ramp_payload:
            raise CrossoverLevelRunError(
                "ramp_config must contain the complete exact MeasurementRamp config"
            )
        safety_timeout_s = ramp.safety_timeout
        if not math.isfinite(safety_timeout_s):
            raise CrossoverLevelRunError(
                "level-run ramp safety timeout must be finite"
            )
        ramp_fingerprint = json_fingerprint(
            ramp_payload, field_name="crossover level ramp config"
        )
        safety_timeout_ms = math.ceil(safety_timeout_s * 1000.0)
        phone_hard_timeout_ms = math.ceil(
            (safety_timeout_s + PHONE_TRANSPORT_GRACE_S) * 1000.0
        )
        if phone_hard_timeout_ms <= safety_timeout_ms:
            raise CrossoverLevelRunError(
                "phone timeout must exceed the backend safety timeout"
            )
        object.__setattr__(self, "topology_id", topology_id)
        object.__setattr__(self, "protected_profile_fingerprint", profile)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "target_fingerprint", target_fingerprint)
        object.__setattr__(self, "geometry", geometry)
        object.__setattr__(self, "capture_geometry", capture_geometry)
        object.__setattr__(self, "ramp_config", MappingProxyType(ramp_payload))
        object.__setattr__(self, "ramp_config_fingerprint", ramp_fingerprint)
        object.__setattr__(self, "safety_timeout_ms", safety_timeout_ms)
        object.__setattr__(self, "phone_hard_timeout_ms", phone_hard_timeout_ms)
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    @classmethod
    def from_dict(cls, raw: Any) -> "CrossoverLevelRunRequest":
        if not isinstance(raw, Mapping):
            raise CrossoverLevelRunError("level-run request must be an object")
        expected = {
            "schema_version",
            "kind",
            "topology_id",
            "protected_profile_fingerprint",
            "target_id",
            "target_fingerprint",
            "geometry",
            "capture_geometry",
            "ramp_config",
            "ramp_config_fingerprint",
            "safety_timeout_ms",
            "phone_hard_timeout_ms",
            "fingerprint",
        }
        if set(raw) != expected:
            raise CrossoverLevelRunError(
                "level-run request has unknown or missing fields"
            )
        if (
            type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != SCHEMA_VERSION
            or raw.get("kind") != REQUEST_KIND
        ):
            raise CrossoverLevelRunError("level-run request schema is unsupported")
        ramp_config = raw["ramp_config"]
        if not isinstance(ramp_config, Mapping):
            raise CrossoverLevelRunError("ramp_config must be an object")
        request = cls(
            topology_id=_text(raw["topology_id"], field_name="topology_id"),
            protected_profile_fingerprint=_sha256(
                raw["protected_profile_fingerprint"],
                field_name="protected_profile_fingerprint",
            ),
            target_id=_text(raw["target_id"], field_name="target_id"),
            target_fingerprint=_sha256(
                raw["target_fingerprint"], field_name="target_fingerprint"
            ),
            geometry=_text(raw["geometry"], field_name="geometry"),
            capture_geometry=_text(
                raw["capture_geometry"], field_name="capture_geometry"
            ),
            ramp_config=ramp_config,
        )
        declared_ints = (raw.get("safety_timeout_ms"), raw.get("phone_hard_timeout_ms"))
        if any(type(value) is not int for value in declared_ints):
            raise CrossoverLevelRunError("level-run timeout fields must be integers")
        if (
            raw.get("ramp_config_fingerprint") != request.ramp_config_fingerprint
            or raw.get("safety_timeout_ms") != request.safety_timeout_ms
            or raw.get("phone_hard_timeout_ms") != request.phone_hard_timeout_ms
            or raw.get("fingerprint") != request.fingerprint
        ):
            raise CrossoverLevelRunError(
                "level-run request declarations do not match the frozen config"
            )
        return request

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": REQUEST_KIND,
            "topology_id": self.topology_id,
            "protected_profile_fingerprint": self.protected_profile_fingerprint,
            "target_id": self.target_id,
            "target_fingerprint": self.target_fingerprint,
            "geometry": self.geometry,
            "capture_geometry": self.capture_geometry,
            "ramp_config": dict(self.ramp_config),
            "ramp_config_fingerprint": self.ramp_config_fingerprint,
            "safety_timeout_ms": self.safety_timeout_ms,
            "phone_hard_timeout_ms": self.phone_hard_timeout_ms,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    def measurement_ramp(self) -> MeasurementRamp:
        """Rebuild the exact planned config for backend execution."""

        return MeasurementRamp(**dict(self.ramp_config))


def build_level_run_request(
    *,
    topology_id: str,
    protected_profile_fingerprint: str,
    target_id: str,
    target_fingerprint: str,
    geometry: str,
    ramp: MeasurementRamp,
) -> CrossoverLevelRunRequest:
    """Bind one exact target to a complete replayable ramp configuration."""

    from .capture_geometry import parse_driver_level_geometry

    try:
        capture_geometry, speaker_group_id, role = parse_driver_level_geometry(geometry)
    except ValueError as exc:
        raise CrossoverLevelRunError("level-run geometry is invalid") from exc
    physical_target_id = f"{speaker_group_id}:{role}"
    if target_id != physical_target_id:
        raise CrossoverLevelRunError(
            "level-run geometry does not identify the requested driver target"
        )
    return CrossoverLevelRunRequest(
        topology_id=topology_id,
        protected_profile_fingerprint=protected_profile_fingerprint,
        target_id=target_id,
        target_fingerprint=target_fingerprint,
        geometry=geometry,
        capture_geometry=capture_geometry,
        ramp_config=_ramp_payload(ramp),
    )


@dataclass(frozen=True)
class CrossoverLevelRunClaim:
    """Claim result consumed by the Room-owned transport adapter."""

    run_id: str
    disposition: CrossoverLevelRunDisposition
    request: CrossoverLevelRunRequest
    phase: CrossoverLevelRunPhase

    @property
    def should_dispatch(self) -> bool:
        return self.disposition is CrossoverLevelRunDisposition.NEW

    @property
    def phone_hard_timeout_ms(self) -> int:
        return self.request.phone_hard_timeout_ms


def _base_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": STATE_KIND,
        "current": None,
    }


class CrossoverLevelRunStore:
    """One durable current-run slot with atomic claim and exact-run updates."""

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        owner_id: str | None = None,
        now: Callable[[], float] = time.time,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.owner_id = _uuid_hex(owner_id or uuid_factory().hex, field_name="owner_id")
        self._now_clock = now
        self._uuid_factory = uuid_factory
        self._memory_state = _base_state()
        # A terminal success is deduplicable only while this service process
        # still holds the corresponding process-local level result. Durable
        # run state records what happened; it is not proof that the volatile
        # result still exists.
        self._live_succeeded_run_id: str | None = None

    def _now(self) -> str:
        value = float(self._now_clock())
        if not math.isfinite(value):
            raise CrossoverLevelRunError("level-run clock is non-finite")
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))

    @contextmanager
    def _locked(self):
        with _THREAD_LOCK:
            if self.path is None:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            with lock_path.open("a+", encoding="utf-8") as handle:
                os.chmod(lock_path, 0o640)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if self.path is None:
            raw: Any = self._memory_state
        else:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return _base_state()
            except (OSError, json.JSONDecodeError) as exc:
                raise CrossoverLevelRunError(
                    "crossover level-run state is unreadable"
                ) from exc
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"schema_version", "kind", "current"}
            or type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != SCHEMA_VERSION
            or raw.get("kind") != STATE_KIND
        ):
            raise CrossoverLevelRunError("crossover level-run state is malformed")
        current_raw = raw.get("current")
        if current_raw is None:
            return _base_state()
        if not isinstance(current_raw, Mapping):
            raise CrossoverLevelRunError("crossover level-run entry is malformed")
        expected = {
            "run_id",
            "owner_id",
            "request",
            "request_fingerprint",
            "phase",
            "claimed_at",
            "backend_started_at",
            "phone_armed_at",
            "phone_timeout_at",
            "completed_at",
            "terminal_reason",
            "late_success",
        }
        if set(current_raw) != expected:
            raise CrossoverLevelRunError(
                "crossover level-run entry has unknown or missing fields"
            )
        run_id = _uuid_hex(current_raw.get("run_id"), field_name="run_id")
        owner_id = _uuid_hex(current_raw.get("owner_id"), field_name="owner_id")
        request = CrossoverLevelRunRequest.from_dict(current_raw.get("request"))
        request_fingerprint = _sha256(
            current_raw.get("request_fingerprint"),
            field_name="request_fingerprint",
        )
        if request_fingerprint != request.fingerprint:
            raise CrossoverLevelRunError(
                "level-run request fingerprint is inconsistent"
            )
        try:
            phase = CrossoverLevelRunPhase(current_raw.get("phase"))
        except ValueError as exc:
            raise CrossoverLevelRunError(
                "crossover level-run phase is invalid"
            ) from exc
        claimed_at = _timestamp(
            current_raw.get("claimed_at"), field_name="claimed_at", optional=False
        )
        backend_started_at = _timestamp(
            current_raw.get("backend_started_at"), field_name="backend_started_at"
        )
        phone_armed_at = _timestamp(
            current_raw.get("phone_armed_at"), field_name="phone_armed_at"
        )
        phone_timeout_at = _timestamp(
            current_raw.get("phone_timeout_at"), field_name="phone_timeout_at"
        )
        completed_at = _timestamp(
            current_raw.get("completed_at"), field_name="completed_at"
        )
        terminal_reason = current_raw.get("terminal_reason")
        if terminal_reason is not None:
            terminal_reason = _text(terminal_reason, field_name="terminal_reason")
        late_success = current_raw.get("late_success")
        if type(late_success) is not bool:
            raise CrossoverLevelRunError("late_success must be boolean")
        if phase.value in _ACTIVE_PHASES and (
            completed_at is not None or terminal_reason is not None or late_success
        ):
            raise CrossoverLevelRunError("active crossover level-run state is terminal")
        if phase.value in _TERMINAL_PHASES and completed_at is None:
            raise CrossoverLevelRunError(
                "terminal crossover level run has no completion"
            )
        if phase is CrossoverLevelRunPhase.SUCCEEDED:
            if (
                terminal_reason is not None
                or late_success != (phone_timeout_at is not None)
                or phone_armed_at is None
                or backend_started_at is None
            ):
                raise CrossoverLevelRunError(
                    "successful crossover level run is inconsistent"
                )
        elif phase.value in _TERMINAL_PHASES:
            try:
                failure = CrossoverLevelRunFailure(terminal_reason)
            except ValueError as exc:
                raise CrossoverLevelRunError(
                    "failed crossover level run has an unknown reason"
                ) from exc
            if late_success:
                raise CrossoverLevelRunError(
                    "failed crossover level run is inconsistent"
                )
            if (
                phase is CrossoverLevelRunPhase.INTERRUPTED
                and failure is not CrossoverLevelRunFailure.SERVICE_RESTARTED
            ):
                raise CrossoverLevelRunError(
                    "interrupted crossover level run has an invalid reason"
                )
        elif phase is CrossoverLevelRunPhase.RUNNING and phone_armed_at is None:
            raise CrossoverLevelRunError("running crossover level run is not armed")
        elif (
            phase is CrossoverLevelRunPhase.RUNNING
            and phone_timeout_at is not None
            and backend_started_at is None
        ):
            raise CrossoverLevelRunError(
                "timed-out crossover level run started no backend"
            )
        current = {
            "run_id": run_id,
            "owner_id": owner_id,
            "request": request.to_dict(),
            "request_fingerprint": request.fingerprint,
            "phase": phase.value,
            "claimed_at": claimed_at,
            "backend_started_at": backend_started_at,
            "phone_armed_at": phone_armed_at,
            "phone_timeout_at": phone_timeout_at,
            "completed_at": completed_at,
            "terminal_reason": terminal_reason,
            "late_success": late_success,
        }
        return {**_base_state(), "current": current}

    def _write(self, state: Mapping[str, Any]) -> None:
        payload = json.loads(json.dumps(dict(state)))
        if self.path is None:
            self._memory_state = payload
            return
        atomic_write_text(
            self.path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )

    @staticmethod
    def _claim_from_current(
        current: Mapping[str, Any], disposition: CrossoverLevelRunDisposition
    ) -> CrossoverLevelRunClaim:
        return CrossoverLevelRunClaim(
            run_id=str(current["run_id"]),
            disposition=disposition,
            request=CrossoverLevelRunRequest.from_dict(current["request"]),
            phase=CrossoverLevelRunPhase(str(current["phase"])),
        )

    def claim(self, request: CrossoverLevelRunRequest) -> CrossoverLevelRunClaim:
        """Atomically claim or deduplicate one exact request."""

        if not isinstance(request, CrossoverLevelRunRequest):
            raise TypeError("request must be a CrossoverLevelRunRequest")
        with self._locked():
            state = self._read()
            current = state.get("current")
            if isinstance(current, Mapping):
                same = current.get("request_fingerprint") == request.fingerprint
                if current.get("phase") in _ACTIVE_PHASES:
                    if current.get("owner_id") != self.owner_id:
                        raise CrossoverLevelRunConflict(
                            "a prior service owner still holds the active level run"
                        )
                    if not same:
                        raise CrossoverLevelRunConflict(
                            "another crossover level request is already active"
                        )
                    claim = self._claim_from_current(
                        current, CrossoverLevelRunDisposition.DUPLICATE_ACTIVE
                    )
                    log_event(
                        logger,
                        "correction.crossover_level_run_deduplicated",
                        run_id=claim.run_id,
                        disposition=claim.disposition.value,
                        request_fingerprint=request.fingerprint,
                    )
                    return claim
                if (
                    current.get("phase") == CrossoverLevelRunPhase.SUCCEEDED.value
                    and same
                    and current.get("owner_id") == self.owner_id
                    and current.get("run_id") == self._live_succeeded_run_id
                ):
                    claim = self._claim_from_current(
                        current, CrossoverLevelRunDisposition.DUPLICATE_SUCCEEDED
                    )
                    log_event(
                        logger,
                        "correction.crossover_level_run_deduplicated",
                        run_id=claim.run_id,
                        disposition=claim.disposition.value,
                        request_fingerprint=request.fingerprint,
                    )
                    return claim
            now = self._now()
            self._live_succeeded_run_id = None
            current = {
                "run_id": self._uuid_factory().hex,
                "owner_id": self.owner_id,
                "request": request.to_dict(),
                "request_fingerprint": request.fingerprint,
                "phase": CrossoverLevelRunPhase.AWAITING_PHONE.value,
                "claimed_at": now,
                "backend_started_at": None,
                "phone_armed_at": None,
                "phone_timeout_at": None,
                "completed_at": None,
                "terminal_reason": None,
                "late_success": False,
            }
            _uuid_hex(current["run_id"], field_name="run_id")
            state["current"] = current
            self._write(state)
        claim = self._claim_from_current(current, CrossoverLevelRunDisposition.NEW)
        log_event(
            logger,
            "correction.crossover_level_run_claimed",
            run_id=claim.run_id,
            request_fingerprint=request.fingerprint,
            target=request.target_id,
            geometry=request.capture_geometry,
            safety_timeout_ms=request.safety_timeout_ms,
            phone_hard_timeout_ms=request.phone_hard_timeout_ms,
        )
        return claim

    def claim_owner(self) -> dict[str, Any] | None:
        """At service start, retire nonterminal work from the prior process."""

        interrupted: dict[str, Any] | None = None
        with self._locked():
            state = self._read()
            current = state.get("current")
            if (
                isinstance(current, Mapping)
                and current.get("phase") in _ACTIVE_PHASES
                and current.get("owner_id") != self.owner_id
            ):
                interrupted = dict(current)
                interrupted.update(
                    {
                        "phase": CrossoverLevelRunPhase.INTERRUPTED.value,
                        "completed_at": self._now(),
                        "terminal_reason": (
                            CrossoverLevelRunFailure.SERVICE_RESTARTED.value
                        ),
                        "late_success": False,
                    }
                )
                state["current"] = interrupted
                self._write(state)
        if interrupted is not None:
            log_event(
                logger,
                "correction.crossover_level_run_interrupted",
                run_id=interrupted["run_id"],
                request_fingerprint=interrupted["request_fingerprint"],
                reason=CrossoverLevelRunFailure.SERVICE_RESTARTED.value,
            )
        return self.snapshot()

    def invalidate_succeeded_result(self, *, geometry: str | None = None) -> bool:
        """Stop deduplicating a success whose process-local result was discarded."""

        requested_geometry = (
            _text(geometry, field_name="geometry") if geometry is not None else None
        )
        invalidated: dict[str, Any] | None = None
        with self._locked():
            current = self._read().get("current")
            if (
                not isinstance(current, Mapping)
                or current.get("phase") != CrossoverLevelRunPhase.SUCCEEDED.value
                or current.get("owner_id") != self.owner_id
                or current.get("run_id") != self._live_succeeded_run_id
            ):
                return False
            request = CrossoverLevelRunRequest.from_dict(current["request"])
            if requested_geometry is not None and request.geometry != requested_geometry:
                return False
            self._live_succeeded_run_id = None
            invalidated = {
                "run_id": current["run_id"],
                "request_fingerprint": request.fingerprint,
                "geometry": request.geometry,
            }
        assert invalidated is not None
        log_event(
            logger,
            "correction.crossover_level_run_result_invalidated",
            **invalidated,
        )
        return True

    def _update_active(
        self,
        run_id: str,
        update: Callable[[dict[str, Any]], bool],
    ) -> bool:
        _uuid_hex(run_id, field_name="run_id")
        with self._locked():
            state = self._read()
            current = state.get("current")
            if (
                not isinstance(current, Mapping)
                or current.get("run_id") != run_id
                or current.get("phase") not in _ACTIVE_PHASES
                or current.get("owner_id") != self.owner_id
            ):
                return False
            updated = dict(current)
            if not update(updated):
                return False
            state["current"] = updated
            self._write(state)
            return True

    def begin_backend(self, run_id: str, *, geometry: str) -> MeasurementRamp:
        """Atomically single-flight backend dispatch and return its frozen ramp."""

        requested_geometry = _text(geometry, field_name="geometry")
        _uuid_hex(run_id, field_name="run_id")
        with self._locked():
            state = self._read()
            current = state.get("current")
            if (
                not isinstance(current, Mapping)
                or current.get("run_id") != run_id
                or current.get("phase") != CrossoverLevelRunPhase.RUNNING.value
                or current.get("owner_id") != self.owner_id
                or current.get("phone_armed_at") is None
                or current.get("phone_timeout_at") is not None
                and current.get("backend_started_at") is None
            ):
                raise CrossoverLevelRunError(
                    "crossover level backend does not own this armed run"
                )
            if current.get("backend_started_at") is not None:
                raise CrossoverLevelRunConflict(
                    "crossover level backend already started for this run"
                )
            request = CrossoverLevelRunRequest.from_dict(current["request"])
            if request.geometry != requested_geometry:
                raise CrossoverLevelRunError(
                    "crossover level backend geometry changed after claim"
                )
            updated = dict(current)
            updated["backend_started_at"] = self._now()
            state["current"] = updated
            self._write(state)
            return request.measurement_ramp()

    def mark_phone_armed(self, run_id: str) -> bool:
        """Record a token-matched phone armed event once."""

        def update(current: dict[str, Any]) -> bool:
            if current.get("phone_armed_at") is not None:
                return False
            current["phone_armed_at"] = self._now()
            current["phase"] = CrossoverLevelRunPhase.RUNNING.value
            return True

        changed = self._update_active(run_id, update)
        if changed:
            log_event(
                logger,
                "correction.crossover_level_run_phone_armed",
                run_id=run_id,
            )
        return changed

    def mark_phone_timeout(self, run_id: str) -> bool:
        """Record a timeout, refusing audio if the backend has not started."""

        _uuid_hex(run_id, field_name="run_id")
        failed_before_backend = False
        annotated_success = False
        with self._locked():
            state = self._read()
            current = state.get("current")
            if (
                not isinstance(current, Mapping)
                or current.get("run_id") != run_id
                or current.get("owner_id") != self.owner_id
                or current.get("phone_armed_at") is None
                or current.get("phone_timeout_at") is not None
            ):
                return False
            updated = dict(current)
            timeout_at = self._now()
            updated["phone_timeout_at"] = timeout_at
            if current.get("phase") == CrossoverLevelRunPhase.SUCCEEDED.value:
                if current.get("backend_started_at") is None:
                    raise CrossoverLevelRunError(
                        "successful crossover level run started no backend"
                    )
                # The phone event may precede backend persistence but reach the
                # Pi on the next relay poll. Exact current-owner correlation may
                # annotate that already-terminal success; it never reopens it.
                updated["late_success"] = True
                annotated_success = True
            elif current.get("phase") not in _ACTIVE_PHASES:
                return False
            elif updated.get("backend_started_at") is None:
                # The live phone feed/clip guard ended before the backend owned
                # the run. Starting audio after that point would be unguarded.
                updated.update(
                    {
                        "phase": CrossoverLevelRunPhase.FAILED.value,
                        "completed_at": timeout_at,
                        "terminal_reason": CrossoverLevelRunFailure.PHONE_ABORTED.value,
                        "late_success": False,
                    }
                )
                failed_before_backend = True
            state["current"] = updated
            self._write(state)
        log_event(
            logger,
            "correction.crossover_level_run_phone_timeout",
            level=logging.WARNING,
            run_id=run_id,
            before_backend=failed_before_backend,
            after_success=annotated_success,
        )
        if failed_before_backend:
            log_event(
                logger,
                "correction.crossover_level_run_completed",
                level=logging.WARNING,
                run_id=run_id,
                phase=CrossoverLevelRunPhase.FAILED.value,
                reason=CrossoverLevelRunFailure.PHONE_ABORTED.value,
                late_success=False,
            )
        return True

    def _finish(
        self,
        run_id: str,
        *,
        succeeded: bool,
        reason: CrossoverLevelRunFailure | None,
    ) -> bool:
        if succeeded and reason is not None:
            raise ValueError("successful crossover level run has no failure reason")
        if not succeeded and not isinstance(reason, CrossoverLevelRunFailure):
            raise TypeError("failed crossover level run requires a typed reason")
        _uuid_hex(run_id, field_name="run_id")
        reason_value = reason.value if reason is not None else None
        late_success = False
        with self._locked():
            state = self._read()
            current_raw = state.get("current")
            if (
                not isinstance(current_raw, Mapping)
                or current_raw.get("run_id") != run_id
                or current_raw.get("phase") not in _ACTIVE_PHASES
                or current_raw.get("owner_id") != self.owner_id
            ):
                return False
            current = dict(current_raw)
            if succeeded and (
                current.get("phone_armed_at") is None
                or current.get("backend_started_at") is None
            ):
                raise CrossoverLevelRunError(
                    "successful crossover level run lacks armed phone or backend start"
                )
            current.update(
                {
                    "phase": (
                        CrossoverLevelRunPhase.SUCCEEDED.value
                        if succeeded
                        else CrossoverLevelRunPhase.FAILED.value
                    ),
                    "completed_at": self._now(),
                    "terminal_reason": reason_value,
                    "late_success": bool(
                        succeeded and current.get("phone_timeout_at") is not None
                    ),
                }
            )
            late_success = current["late_success"]
            state["current"] = current
            self._write(state)
            if succeeded:
                self._live_succeeded_run_id = run_id
            elif self._live_succeeded_run_id == run_id:
                self._live_succeeded_run_id = None
        log_event(
            logger,
            "correction.crossover_level_run_completed",
            level=(logging.INFO if succeeded else logging.WARNING),
            run_id=run_id,
            phase=(
                CrossoverLevelRunPhase.SUCCEEDED.value
                if succeeded
                else CrossoverLevelRunPhase.FAILED.value
            ),
            reason=reason_value,
            late_success=late_success,
        )
        return True

    def succeed(self, run_id: str) -> bool:
        """Complete the exact current run; stale completions are ignored."""

        return self._finish(run_id, succeeded=True, reason=None)

    def fail(self, run_id: str, *, reason: CrossoverLevelRunFailure) -> bool:
        """Fail the exact current run; stale completions are ignored."""

        return self._finish(run_id, succeeded=False, reason=reason)

    def snapshot(self) -> dict[str, Any] | None:
        """Return the browser-safe current run without config or relay secrets."""

        with self._locked():
            current = self._read().get("current")
            result_available = bool(
                isinstance(current, Mapping)
                and current.get("phase") == CrossoverLevelRunPhase.SUCCEEDED.value
                and current.get("owner_id") == self.owner_id
                and current.get("run_id") == self._live_succeeded_run_id
            )
        if not isinstance(current, Mapping):
            return None
        request = CrossoverLevelRunRequest.from_dict(current["request"])
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": current["run_id"],
            "request_fingerprint": request.fingerprint,
            "phase": current["phase"],
            "topology_id": request.topology_id,
            "protected_profile_fingerprint": request.protected_profile_fingerprint,
            "target_id": request.target_id,
            "target_fingerprint": request.target_fingerprint,
            "geometry": request.geometry,
            "capture_geometry": request.capture_geometry,
            "ramp_config_fingerprint": request.ramp_config_fingerprint,
            "safety_timeout_ms": request.safety_timeout_ms,
            "phone_hard_timeout_ms": request.phone_hard_timeout_ms,
            "claimed_at": current["claimed_at"],
            "backend_started_at": current["backend_started_at"],
            "phone_armed": current["phone_armed_at"] is not None,
            "phone_armed_at": current["phone_armed_at"],
            "phone_timeout": current["phone_timeout_at"] is not None,
            "phone_timeout_at": current["phone_timeout_at"],
            "completed_at": current["completed_at"],
            "terminal_reason": current["terminal_reason"],
            "late_success": current["late_success"],
            "result_available": result_available,
        }
