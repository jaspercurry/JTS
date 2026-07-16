# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side active-crossover measurement backend.

The correction page owns the HTTPS browser surface. Active-speaker measurement
state, capture storage, preset resolution, and acoustic analysis are owned by
``jasper.active_speaker.web_measurement`` so another operator surface does not
need to rediscover the same evidence model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from jasper.active_speaker import web_commissioning, web_measurement
from jasper.active_speaker.commissioning_run import (
    CommissioningRunHandle,
    CommissioningRunStore,
)
from jasper.active_speaker.capture_geometry import (
    comparison_set_valid,
    quietest_locked_main_volume,
)
from jasper.active_speaker.crossover_level_run import (
    CrossoverLevelRunError,
    CrossoverLevelRunPhase,
    PHONE_TRANSPORT_GRACE_S,
    state_path as _level_run_state_path,
)
from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

logger = logging.getLogger(__name__)
EMERGENCY_SWEEP_VOLUME_DB = -60.0
_VOLUME_SAFETY_STATE_KIND = "jts_crossover_volume_safety"
_VOLUME_SAFETY_SCHEMA_VERSION = 1
_DEFAULT_VOLUME_SAFETY_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_volume_safety.json"
)
_VOLUME_READBACK_TOLERANCE_DB = 0.05
CamillaFactory = Callable[[], Any]

if TYPE_CHECKING:
    from jasper.active_speaker.crossover_level_run import (
        CrossoverLevelRunClaim,
        CrossoverLevelRunFailure,
    )
    from jasper.audio_measurement.ramp import MeasurementRamp
    from jasper.correction.level_match import LevelMatchOutcome, LevelMatchSession


class UnresolvedVolumeRecoveryResult(str, Enum):
    """Outcome of reconciling a durable uncertain listening volume."""

    EXACT_RESTORED = "exact_restored"
    EMERGENCY_ATTENUATED = "emergency_attenuated"
    FAILED = "failed"


def _malformed_volume_safety(reason: str) -> dict[str, Any]:
    return {
        "status": "unresolved",
        "reason": reason,
        "source": "unknown",
        "speaker_group_id": "",
        "role": "",
        "original_main_volume_db": None,
        "emergency_volume_db": EMERGENCY_SWEEP_VOLUME_DB,
    }


def _load_volume_safety_state(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return _malformed_volume_safety("volume_safety_state_unreadable")
    if (
        not isinstance(raw, Mapping)
        or raw.get("kind") != _VOLUME_SAFETY_STATE_KIND
        or raw.get("schema_version") != _VOLUME_SAFETY_SCHEMA_VERSION
    ):
        return _malformed_volume_safety("volume_safety_state_malformed")
    if raw.get("status") == "resolved":
        return None
    original = raw.get("original_main_volume_db")
    if original is not None and (
        isinstance(original, bool)
        or not isinstance(original, (int, float))
        or not math.isfinite(float(original))
        or float(original) > 0
    ):
        original = None
    status = raw.get("status")
    if status not in {"active", "unresolved"}:
        return _malformed_volume_safety("volume_safety_state_malformed")
    return {
        "status": "unresolved",
        "reason": (
            "service_restarted_during_volume_transition"
            if status == "active"
            else str(raw.get("reason") or "volume_restore_unconfirmed")
        ),
        "source": str(raw.get("source") or "unknown"),
        "speaker_group_id": str(raw.get("speaker_group_id") or ""),
        "role": str(raw.get("role") or ""),
        "original_main_volume_db": (float(original) if original is not None else None),
        "emergency_volume_db": EMERGENCY_SWEEP_VOLUME_DB,
    }


def _write_volume_safety_state(path: Path | None, payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    atomic_write_text(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
        mode=0o640,
        group_from_parent=True,
    )


class CrossoverLevelLease:
    """Geometry-keyed gain lease and durable restore intent for Layer A.

    The shared :class:`LevelMatchSession` owns ramp math and relay semantics;
    this thin domain owner supplies only single-flight lifetime, observability,
    and the target/original pair. The process-global production lease injects a
    durable state path; ordinary test instances stay in-memory unless they opt
    into one. The target is asserted only inside a sweep window and restored in
    that window's ``finally``. It deliberately owns no CamillaDSP or relay
    client.
    """

    def __init__(
        self,
        *,
        volume_safety_state_path: str | Path | None = None,
        level_run_state_path: str | Path | None = None,
    ) -> None:
        from jasper.active_speaker.crossover_level_run import CrossoverLevelRunStore
        from jasper.correction.level_match import LevelLockStore

        self.session_id = "active-crossover"
        self.level_lock_store = LevelLockStore()
        self._running: LevelMatchSession | None = None
        self._last: LevelMatchOutcome | None = None
        self._active_outcome: LevelMatchOutcome | None = None
        self._outcomes: dict[str, LevelMatchOutcome] = {}
        self._level_result_lock = threading.RLock()
        self._targets: dict[str, dict[str, Any]] = {}
        self._restore_lock = asyncio.Lock()
        self._sweep_entry_volume_db: float | None = None
        self.context_id: str | None = None
        self.noise_floor_db = None
        self.mic_calibration = None
        self.input_device = None
        # Interim fixed-position repeats are process-local and scoped by both
        # the immutable comparison set and driver target.  Nothing from one
        # level/profile context can be paired with another.
        self._repeat_sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self._repeat_lock = threading.RLock()
        self._repeat_failures: dict[str, dict[str, Any]] = {}
        self._durable_repeat_progress: dict[str, Any] = {}
        self._volume_safety_state_path = (
            Path(volume_safety_state_path)
            if volume_safety_state_path is not None
            else None
        )
        self._volume_safety_state = _load_volume_safety_state(
            self._volume_safety_state_path
        )
        self._level_run_store = CrossoverLevelRunStore(path=level_run_state_path)

    @property
    def unresolved_volume_safety(self) -> dict[str, Any] | None:
        state = self._volume_safety_state
        return (
            dict(state)
            if state is not None and state.get("status") == "unresolved"
            else None
        )

    def assert_volume_safety_resolved(self) -> None:
        if self._volume_safety_state is not None:
            raise RuntimeError(
                "the crossover listening volume is not confirmed safe; JTS must "
                "restore it or apply emergency attenuation before another action"
            )

    def assert_sweep_volume_owned(
        self,
        *,
        source: str,
        speaker_group_id: str,
        role: str,
    ) -> None:
        """Require the live sweep to match this lease's durable intent."""

        state = self._volume_safety_state
        if (
            self._sweep_entry_volume_db is None
            or state is None
            or state.get("status") != "active"
            or state.get("source") != source
            or state.get("speaker_group_id") != speaker_group_id
            or state.get("role") != role
        ):
            raise RuntimeError(
                "the crossover sweep does not own the active volume lease"
            )

    def _persist_volume_safety(self, state: Mapping[str, Any]) -> None:
        _write_volume_safety_state(
            self._volume_safety_state_path,
            {
                "schema_version": _VOLUME_SAFETY_SCHEMA_VERSION,
                "kind": _VOLUME_SAFETY_STATE_KIND,
                **dict(state),
            },
        )

    def _begin_volume_transition(
        self,
        *,
        source: str,
        speaker_group_id: str,
        role: str,
        original_main_volume_db: float,
    ) -> None:
        self.assert_volume_safety_resolved()
        original = float(original_main_volume_db)
        if not math.isfinite(original) or original > 0:
            raise ValueError("crossover restore volume must be finite and <= 0 dB")
        state = {
            "status": "active",
            "reason": None,
            "source": str(source),
            "speaker_group_id": str(speaker_group_id),
            "role": str(role),
            "original_main_volume_db": original,
            "emergency_volume_db": EMERGENCY_SWEEP_VOLUME_DB,
        }
        # Write before the first volume mutation. A process crash or lost setter
        # response therefore hydrates as unresolved instead of forgetting risk.
        self._persist_volume_safety(state)
        self._volume_safety_state = state

    def _mark_volume_unresolved(self, reason: str) -> None:
        state = dict(self._volume_safety_state or _malformed_volume_safety(reason))
        state.update({"status": "unresolved", "reason": str(reason)})
        self._volume_safety_state = state
        try:
            self._persist_volume_safety(state)
        except OSError:
            # A prior active intent remains on disk when this is a real
            # transition, so restart still hydrates fail-closed.
            log_event(
                logger,
                "correction.crossover_level_volume_safety_persist_failed",
                level=logging.CRITICAL,
                reason=reason,
            )

    def _clear_volume_safety(self) -> None:
        self._persist_volume_safety({"status": "resolved"})
        self._volume_safety_state = None

    @staticmethod
    async def _set_and_confirm_volume(
        target_db: float,
        set_main_volume_db: Any,
        get_main_volume_db: Any,
    ) -> bool:
        try:
            applied = await set_main_volume_db(float(target_db))
            if applied is False:
                return False
            observed = await get_main_volume_db()
        except (OSError, RuntimeError, TimeoutError, ValueError):
            return False
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isfinite(float(observed))
        ):
            return False
        return abs(float(observed) - float(target_db)) <= _VOLUME_READBACK_TOLERANCE_DB

    async def _recover_volume_safety(
        self,
        set_main_volume_db: Any,
        get_main_volume_db: Any,
        *,
        allow_active: bool,
    ) -> UnresolvedVolumeRecoveryResult:
        """Resolve the one durable volume intent through confirmed readback."""

        async with self._restore_lock:
            state = self._volume_safety_state
            if state is None:
                return UnresolvedVolumeRecoveryResult.EXACT_RESTORED
            if state.get("status") == "active" and not allow_active:
                return UnresolvedVolumeRecoveryResult.FAILED
            exact = state.get("original_main_volume_db")
            candidates: list[tuple[str, float]] = []
            if (
                not isinstance(exact, bool)
                and isinstance(exact, (int, float))
                and math.isfinite(float(exact))
                and float(exact) <= 0
            ):
                candidates.append(("exact", float(exact)))
            candidates.append(("emergency", EMERGENCY_SWEEP_VOLUME_DB))
            for recovery, target in candidates:
                if not await self._set_and_confirm_volume(
                    target,
                    set_main_volume_db,
                    get_main_volume_db,
                ):
                    continue
                try:
                    self._clear_volume_safety()
                except OSError:
                    self._mark_volume_unresolved("volume_safety_clear_failed")
                    return UnresolvedVolumeRecoveryResult.FAILED
                outcome = self._active_outcome or self._last
                if outcome is not None and getattr(outcome, "ramp", None) is not None:
                    outcome.ramp.restored = True
                self._active_outcome = None
                self._sweep_entry_volume_db = None
                log_event(
                    logger,
                    "correction.crossover_level_volume_safety_recovered",
                    level=(logging.INFO if recovery == "exact" else logging.ERROR),
                    recovery=recovery,
                    source=state.get("source"),
                    to_db=f"{target:.1f}",
                )
                return (
                    UnresolvedVolumeRecoveryResult.EXACT_RESTORED
                    if recovery == "exact"
                    else UnresolvedVolumeRecoveryResult.EMERGENCY_ATTENUATED
                )
            self._mark_volume_unresolved("volume_restore_unconfirmed")
            log_event(
                logger,
                "correction.crossover_level_volume_safety_recovery_failed",
                level=logging.CRITICAL,
                source=state.get("source"),
            )
            return UnresolvedVolumeRecoveryResult.FAILED

    async def recover_unresolved_volume_safety(
        self,
        set_main_volume_db: Any,
        get_main_volume_db: Any,
    ) -> UnresolvedVolumeRecoveryResult:
        """Recover a latched prior failure, never a live measurement."""

        return await self._recover_volume_safety(
            set_main_volume_db,
            get_main_volume_db,
            allow_active=False,
        )

    async def _drain_volume_recovery(
        self,
        set_main_volume_db: Any,
        get_main_volume_db: Any,
    ) -> UnresolvedVolumeRecoveryResult:
        """Finish recovery even when cancellation repeats during cleanup."""

        cleanup = asyncio.create_task(
            self._recover_volume_safety(
                set_main_volume_db,
                get_main_volume_db,
                allow_active=True,
            )
        )
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
                if cleanup.done():
                    result = cleanup.result()
                    break
                continue
            break
        if cancelled:
            raise asyncio.CancelledError
        return result

    def configure_targets(self, targets: Sequence[Mapping[str, Any]]) -> None:
        """Freeze one complete, protected per-driver level plan."""

        normalized = {
            str(target["target_id"]): dict(target)
            for target in targets
            if str(target.get("target_id") or "")
        }
        if not normalized:
            raise ValueError("crossover level plan has no driver targets")
        if self._targets and self._targets != normalized:
            raise RuntimeError("crossover level targets changed during measurement")
        self._targets = normalized

    @staticmethod
    def _ramp_config_for_geometry(geometry: str) -> MeasurementRamp:
        """Freeze the same complete ramp config planning and execution consume."""

        from jasper.active_speaker.capture_geometry import (
            parse_driver_level_geometry,
        )
        from jasper.audio_measurement.ramp import (
            LISTENING_POSITION_CAP_BUMP_DB,
            LISTENING_POSITION_CAP_CEIL_DB,
            MeasurementRamp,
        )

        capture_geometry, _speaker_group_id, _role = parse_driver_level_geometry(
            str(geometry)
        )
        # A fixed-axis capture sits at the same roughly one-metre geometry as
        # Room measurement, so it uses that domain's reviewed cap. Near-field
        # retains the quieter shared default. Both still pass through the 0 dB
        # hard ceiling and live clip abort.
        return MeasurementRamp.from_env(
            allow_bounded_low_level=True,
            **(
                {
                    "cap_bump_db": LISTENING_POSITION_CAP_BUMP_DB,
                    "cap_ceil_db": LISTENING_POSITION_CAP_CEIL_DB,
                }
                if capture_geometry == "reference_axis"
                else {}
            ),
        )

    def phone_hard_timeout_ms(self, geometry: str) -> int:
        """The phone's hard capture deadline for this geometry, in ms.

        Derived from the SAME ramp config ``run_level_match`` actually
        executes (``_ramp_config_for_geometry``), so the phone's deadline can
        never undercut the server's real ``MeasurementRamp.safety_timeout`` —
        a flat client-side constant sized against today's defaults would
        silently drift out of sync the moment the ramp config (env-tuned
        knobs, geometry-specific caps) changes. ``PHONE_TRANSPORT_GRACE_S``
        is the same margin ``crossover_level_run.build_level_run_request``
        uses for its (currently unwired) exact-run ``phone_hard_timeout_ms``.
        """

        safety_timeout_s = self._ramp_config_for_geometry(geometry).safety_timeout
        return math.ceil((safety_timeout_s + PHONE_TRANSPORT_GRACE_S) * 1000.0)

    def claim_level_match_run(
        self,
        *,
        topology_id: str,
        protected_profile_fingerprint: str,
        target: Mapping[str, Any],
    ) -> CrossoverLevelRunClaim:
        """Claim one exact Active run before Room opens relay transport."""

        from jasper.active_speaker.crossover_level_run import build_level_run_request

        geometry = str(target.get("geometry") or "")
        request = build_level_run_request(
            topology_id=topology_id,
            protected_profile_fingerprint=protected_profile_fingerprint,
            target_id=str(target.get("target_id") or ""),
            target_fingerprint=str(target.get("target_fingerprint") or ""),
            geometry=geometry,
            ramp=self._ramp_config_for_geometry(geometry),
        )
        return self._level_run_store.claim(request)

    def claim_level_run_owner(self) -> dict[str, Any] | None:
        """Retire a prior process's unfinished run at service startup."""

        return self._level_run_store.claim_owner()

    def mark_level_run_phone_armed(self, run_id: str) -> bool:
        return self._level_run_store.mark_phone_armed(run_id)

    def mark_level_run_phone_timeout(self, run_id: str) -> bool:
        return self._level_run_store.mark_phone_timeout(run_id)

    def mark_level_run_succeeded(self, run_id: str) -> bool:
        with self._level_result_lock:
            current = self._level_run_store.snapshot()
            if current is None or current.get("run_id") != run_id:
                return self._level_run_store.succeed(run_id)
            if current.get("phase") not in {
                CrossoverLevelRunPhase.AWAITING_PHONE.value,
                CrossoverLevelRunPhase.RUNNING.value,
            }:
                return self._level_run_store.succeed(run_id)
            geometry = str(current.get("geometry") or "")
            if geometry not in self._outcomes:
                raise CrossoverLevelRunError(
                    "successful crossover level run has no process-local result"
                )
            return self._level_run_store.succeed(run_id)

    def mark_level_run_failed(
        self, run_id: str, *, reason: CrossoverLevelRunFailure
    ) -> bool:
        return self._level_run_store.fail(run_id, reason=reason)

    def level_run_snapshot(self) -> dict[str, Any] | None:
        """Return a fail-soft public projection while claims remain fail-closed."""

        from jasper.active_speaker.crossover_level_run import (
            SCHEMA_VERSION,
            CrossoverLevelRunError,
        )

        try:
            return self._level_run_store.snapshot()
        except (OSError, CrossoverLevelRunError, ValueError) as exc:
            log_event(
                logger,
                "correction.crossover_level_run_unavailable",
                level=logging.ERROR,
                reason=type(exc).__name__,
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "phase": "failed",
                "terminal_reason": "state_unavailable",
                "late_success": False,
            }

    async def run_level_match(self, geometry: str, **ports: Any) -> Any:
        from jasper.correction.level_match import LevelMatchSession

        self.assert_volume_safety_resolved()
        # The correction session adapter supplies these scheduler ports itself;
        # keep the crossover adapter at the same host boundary. Requiring every
        # web caller to know LevelMatchSession's test seams caused the hardware
        # path to fail before the ramp could start.
        loop = asyncio.get_running_loop()
        ports.setdefault("clock", loop.time)
        ports.setdefault("sleep", asyncio.sleep)
        if self._running is not None:
            raise RuntimeError("crossover level match already in progress")
        from jasper.audio_measurement.ramp import RampState

        if (
            self._last is not None
            and self._last.ramp.state is RampState.LOCKED
            and self._last.ramp.restored is not True
        ):
            raise RuntimeError(
                "crossover measurement level is already locked; finish or "
                "cancel the current crossover measurement first"
            )
        from jasper.active_speaker.capture_geometry import (
            parse_driver_level_geometry,
        )

        _capture_geometry, speaker_group_id, role = parse_driver_level_geometry(
            str(geometry)
        )
        context_id = str(ports.pop("context_id", "") or "") or None
        set_main_volume_db = ports.get("set_main_volume_db")
        get_main_volume_db = ports.get("get_main_volume_db")
        if not callable(set_main_volume_db) or not callable(get_main_volume_db):
            raise RuntimeError("crossover level match has no volume control ports")
        level_run_id = str(ports.pop("level_run_id", "") or "")
        # The Room adapter must supply this explicit feature-owned binding once
        # it claims a durable run. Never infer authority from the fail-soft
        # public snapshot or silently execute a fresh env-derived ramp when an
        # explicit claimed id is stale/corrupt. The empty case preserves the
        # existing Room path until that thin adapter lands in its own lane.
        if level_run_id and str(ports.get("run_token") or "") != level_run_id:
            from jasper.active_speaker.crossover_level_run import (
                CrossoverLevelRunError,
            )

            raise CrossoverLevelRunError(
                "crossover level run id does not match the relay run token"
            )
        config = (
            self._level_run_store.begin_backend(level_run_id, geometry=str(geometry))
            if level_run_id
            else self._ramp_config_for_geometry(str(geometry))
        )
        run = LevelMatchSession(
            session_id=self.session_id,
            store=self.level_lock_store,
            config=config,
        )
        original = await get_main_volume_db()
        if (
            isinstance(original, bool)
            or not isinstance(original, (int, float))
            or not math.isfinite(float(original))
            or float(original) > 0
        ):
            raise RuntimeError(
                "CamillaDSP did not report a safe pre-level listening volume"
            )
        self._begin_volume_transition(
            source="level_match",
            speaker_group_id=speaker_group_id,
            role=role,
            original_main_volume_db=float(original),
        )

        async def frozen_original_volume() -> float:
            return float(original)

        ports["get_main_volume_db"] = frozen_original_volume
        self._running = run
        outcome = None
        recovery = UnresolvedVolumeRecoveryResult.FAILED
        try:
            outcome = await run.run_for_geometry(geometry, **ports)
            self._active_outcome = outcome
        finally:
            if self._running is run:
                self._running = None
            recovery_completed = False
            try:
                recovery = await self._drain_volume_recovery(
                    set_main_volume_db,
                    get_main_volume_db,
                )
                recovery_completed = True
            finally:
                if outcome is None or not recovery_completed:
                    self.level_lock_store.discard(geometry)
        assert outcome is not None
        if recovery is not UnresolvedVolumeRecoveryResult.EXACT_RESTORED:
            self.level_lock_store.discard(geometry)
            raise RuntimeError(
                "JTS could not restore the exact pre-level listening volume; "
                + (
                    "it applied the -60 dB safe fallback. Set your volume again."
                    if recovery is UnresolvedVolumeRecoveryResult.EMERGENCY_ATTENUATED
                    else "stop playback and recover the crossover volume before continuing."
                )
            )
        with self._level_result_lock:
            self._last = outcome
            self._outcomes[geometry] = outcome
            if outcome.locked:
                self.context_id = context_id
        return outcome

    async def cancel_level_match(self) -> bool:
        """Ask the retained crossover ramp to stop through its safe restore."""

        running = self._running
        if running is None:
            return False
        return await running.cancel()

    def invalidate_comparison_context(self) -> None:
        """Drop a prior lock/setup before a newly acquired level run begins."""

        self.assert_volume_safety_resolved()
        from jasper.correction.level_match import LevelLockStore

        with self._level_result_lock:
            if self._running is not None:
                raise RuntimeError("cannot invalidate a running crossover level match")
            self._level_run_store.invalidate_succeeded_result()
            self.level_lock_store = LevelLockStore()
            self._last = None
            self._active_outcome = None
            self._outcomes = {}
            self._targets = {}
            self._sweep_entry_volume_db = None
            self.context_id = None
            self.noise_floor_db = None
            self.mic_calibration = None
            self.input_device = None
            self.relay_setup_binding = None
            self._repeat_sessions = {}
            self._repeat_failures = {}
            self._durable_repeat_progress = {}
        log_event(
            logger,
            "correction.crossover_level_context_invalidated",
        )

    async def acquire_driver_sweep_volume(
        self,
        speaker_group_id: str,
        role: str,
        get_main_volume_db: Any,
        set_main_volume_db: Any,
        *,
        capture_geometry: str = "near_field",
    ) -> bool:
        """Acquire a sweep-scoped lease at this driver's measured target."""

        from jasper.active_speaker.capture_geometry import driver_level_geometry

        geometry = driver_level_geometry(
            speaker_group_id, role, capture_geometry
        )
        return await self._acquire_sweep_volume(
            self._outcomes.get(geometry),
            get_main_volume_db,
            set_main_volume_db,
            source="driver_sweep",
            speaker_group_id=speaker_group_id,
            role=role,
        )

    def driver_sweep_locked_main_volume_db(
        self,
        speaker_group_id: str,
        role: str,
        *,
        capture_geometry: str,
    ) -> float | None:
        """Return the exact lock a geometry-scoped sweep will reassert."""

        from jasper.active_speaker.capture_geometry import driver_level_geometry

        geometry = driver_level_geometry(
            speaker_group_id, role, capture_geometry
        )
        outcome = self._outcomes.get(geometry)
        value = outcome.ramp.locked_main_volume_db if outcome is not None else None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) > 0
        ):
            return None
        return float(value)

    def discard_driver_level_outcome(
        self,
        speaker_group_id: str,
        role: str,
        *,
        capture_geometry: str,
    ) -> None:
        """Drop a level result that failed post-ramp identity validation."""

        from jasper.active_speaker.capture_geometry import driver_level_geometry

        geometry = driver_level_geometry(
            speaker_group_id, role, capture_geometry
        )
        with self._level_result_lock:
            discarded = self._outcomes.pop(geometry, None)
            if self._last is discarded:
                self._last = next(reversed(self._outcomes.values()), None)
            if not self._outcomes:
                self.context_id = None
            self.level_lock_store.discard(geometry)
            self._level_run_store.invalidate_succeeded_result(geometry=geometry)

    async def acquire_summed_sweep_volume(
        self,
        speaker_group_id: str,
        get_main_volume_db: Any,
        set_main_volume_db: Any,
    ) -> bool:
        """Use the quietest fixed-axis driver lock for one summed group."""

        from jasper.active_speaker.capture_geometry import parse_driver_level_geometry

        group_id = str(speaker_group_id or "")
        required_roles = {
            str(target.get("role") or "").lower()
            for target in self._targets.values()
            if str(target.get("speaker_group_id") or "") == group_id
        }
        if not required_roles:
            return False
        outcomes_by_role: dict[str, Any] = {}
        locked_volume_by_role: dict[str, float] = {}
        for geometry, outcome in self._outcomes.items():
            try:
                capture_geometry, outcome_group, role = parse_driver_level_geometry(
                    geometry
                )
            except ValueError:
                continue
            locked = outcome.ramp.locked_main_volume_db
            if (
                capture_geometry == "reference_axis"
                and outcome_group == group_id
                and not isinstance(locked, bool)
                and isinstance(locked, (int, float))
                and math.isfinite(float(locked))
                and float(locked) <= 0
            ):
                outcomes_by_role[role] = outcome
                locked_volume_by_role[role] = float(locked)
        quietest = quietest_locked_main_volume(
            locked_volume_by_role,
            frozenset(required_roles),
        )
        if quietest is None:
            return False
        safest = outcomes_by_role[quietest[0]]
        return await self._acquire_sweep_volume(
            safest,
            get_main_volume_db,
            set_main_volume_db,
            source="summed_sweep",
            speaker_group_id=group_id,
            role="summed",
        )

    async def _acquire_sweep_volume(
        self,
        outcome: Any,
        get_main_volume_db: Any,
        set_main_volume_db: Any,
        *,
        source: str,
        speaker_group_id: str,
        role: str,
    ) -> bool:
        self.assert_volume_safety_resolved()
        from jasper.audio_measurement.ramp import RampState

        async with self._restore_lock:
            if self._sweep_entry_volume_db is not None:
                raise RuntimeError("crossover sweep volume lease is already active")
            if outcome is None or outcome.ramp.state is not RampState.LOCKED:
                return False
            target = outcome.ramp.locked_main_volume_db
            if (
                isinstance(target, bool)
                or not isinstance(target, (int, float))
                or not math.isfinite(float(target))
                or float(target) > 0.0
            ):
                return False
            entry = await get_main_volume_db()
            if (
                isinstance(entry, bool)
                or not isinstance(entry, (int, float))
                or not math.isfinite(float(entry))
                or float(entry) > 0
            ):
                return False
            # Record the restore target before the side effect. If CamillaDSP
            # applies the volume but its response is lost, the caller's finally
            # block still has a valid lease to restore.
            self._begin_volume_transition(
                source=source,
                speaker_group_id=speaker_group_id,
                role=role,
                original_main_volume_db=float(entry),
            )
            self._sweep_entry_volume_db = float(entry)
            applied = await set_main_volume_db(float(target))
            if applied is False:
                log_event(
                    logger,
                    "correction.crossover_driver_level_volume_reassert_failed",
                    level=logging.ERROR,
                    to_db=f"{target:.1f}",
                )
                return False
            log_event(
                logger,
                "correction.crossover_driver_level_volume_reasserted",
                to_db=f"{target:.1f}",
            )
            return True

    async def finish_sweep_volume(
        self, set_main_volume_db: Any, get_main_volume_db: Any
    ) -> UnresolvedVolumeRecoveryResult:
        """Drain one sweep's durable exact-or-emergency volume recovery."""

        if self._sweep_entry_volume_db is None:
            return UnresolvedVolumeRecoveryResult.EXACT_RESTORED
        return await self._drain_volume_recovery(
            set_main_volume_db,
            get_main_volume_db,
        )

    @property
    def sweep_volume_active(self) -> bool:
        return self._sweep_entry_volume_db is not None

    def driver_level_locks(self) -> dict[str, dict[str, Any]]:
        """Return complete normalized excitation evidence for durable storage."""

        from jasper.audio_measurement.excitation import (
            AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
        )

        locks: dict[str, dict[str, Any]] = {}
        for target_id, target in self._targets.items():
            outcome = self._outcomes.get(str(target.get("geometry") or ""))
            locked = outcome.ramp.locked_main_volume_db if outcome is not None else None
            if locked is None:
                continue
            locks[target_id] = {
                "target_id": target_id,
                "speaker_group_id": str(target.get("speaker_group_id") or ""),
                "role": str(target.get("role") or ""),
                "tone_frequency_hz": float(target["tone_frequency_hz"]),
                "tone_peak_dbfs": AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
                "commissioning_gain_db": float(target["commissioning_gain_db"]),
                "locked_main_volume_db": float(locked),
            }
        return locks

    @staticmethod
    def repeat_session_key(
        comparison_set_id: str, target_fingerprint: str
    ) -> tuple[str, str]:
        return str(comparison_set_id), str(target_fingerprint)

    def append_driver_repeat(
        self,
        key: tuple[str, str],
        *,
        target_id: str,
        item: Mapping[str, Any],
        attempt: int | None = None,
    ) -> list[dict[str, Any]]:
        with self._repeat_lock:
            session = self._repeat_sessions.setdefault(
                key,
                {"target_id": target_id, "items": {}},
            )
            if session.get("target_id") != target_id:
                raise RuntimeError("crossover repeat target changed during capture")
            items = session["items"]
            index = int(attempt) if attempt is not None else len(items) + 1
            if not 1 <= index <= 4 or index in items:
                raise RuntimeError("crossover repeat attempt is duplicate or out of bounds")
            items[index] = dict(item)
            self._repeat_failures.pop(target_id, None)
            return [dict(items[key]) for key in sorted(items)]

    def driver_repeats(self, key: tuple[str, str]) -> list[dict[str, Any]]:
        with self._repeat_lock:
            session = self._repeat_sessions.get(key) or {}
            items = session.get("items") or {}
            return [dict(items[index]) for index in sorted(items)]

    def clear_driver_repeats(self, key: tuple[str, str]) -> None:
        with self._repeat_lock:
            self._repeat_sessions.pop(key, None)

    @contextmanager
    def repeat_transaction(self):
        """Serialize aggregate decisions after durable attempt reservation."""

        with self._repeat_lock:
            yield

    def record_repeat_failure(
        self, target_id: str, payload: Mapping[str, Any]
    ) -> None:
        with self._repeat_lock:
            self._repeat_failures[target_id] = dict(payload)

    def repeat_failure(self, target_id: str) -> dict[str, Any] | None:
        with self._repeat_lock:
            failure = self._repeat_failures.get(target_id)
            return dict(failure) if failure is not None else None

    def active_repeat_bindings(self) -> set[tuple[str, str]]:
        with self._repeat_lock:
            return set(self._repeat_sessions)

    def set_durable_repeat_progress(self, payload: Mapping[str, Any]) -> None:
        from jasper.active_speaker.crossover_eligibility import (
            mapping_sequence,
            nonnegative_int,
        )

        def public_result(value: Any) -> dict[str, Any] | None:
            if not isinstance(value, Mapping):
                return None
            attempt = nonnegative_int(value.get("attempt"))
            accepted = value.get("accepted")
            if not 1 <= attempt <= 4 or not isinstance(accepted, bool):
                return None
            public: dict[str, Any] = {
                "attempt": attempt,
                "accepted": accepted,
            }
            string_fields = (
                "reject_reason",
                "failure_type",
                "snr_verdict",
                "worst_band_id",
                "phase",
            )
            numeric_fields = (
                "estimated_snr_db",
                "snr_shortfall_db",
                "validity_floor_hz",
            )
            bool_fields = ("clipping", "above_validity_floor")
            for key in string_fields:
                item = value.get(key)
                if item is None:
                    continue
                if not isinstance(item, str):
                    return None
                public[key] = item
            for key in numeric_fields:
                item = value.get(key)
                if item is None:
                    continue
                if (
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(float(item))
                ):
                    return None
                public[key] = float(item)
            for key in bool_fields:
                item = value.get(key)
                if item is None:
                    continue
                if not isinstance(item, bool):
                    return None
                public[key] = item
            return public

        def malformed_entry() -> dict[str, Any]:
            return {
                "target_id": None,
                "target_fingerprint": None,
                "attempts": 0,
                "status": "malformed",
                "inflight": False,
                "results": [],
                "reason": "malformed_durable_repeat_state",
                "updated_at": None,
            }

        def public_entry(value: Any) -> dict[str, Any]:
            if not isinstance(value, Mapping):
                return malformed_entry()
            attempts = nonnegative_int(value.get("attempts"))
            raw_results = value.get("results")
            result_items = mapping_sequence(raw_results)
            results = [public_result(item) for item in result_items]
            status = value.get("status")
            target_id = value.get("target_id")
            target_fingerprint = value.get("target_fingerprint")
            reason = value.get("reason")
            updated_at = value.get("updated_at")
            inflight = value.get("inflight")
            projected_results = [result for result in results if result is not None]
            result_attempts = [result["attempt"] for result in projected_results]
            full_coverage = (
                inflight is None
                and result_attempts == list(range(1, attempts + 1))
            )
            interrupted_coverage = bool(
                status == "aborted"
                and inflight is None
                and result_attempts == list(range(1, attempts))
            )
            inflight_coverage = bool(
                isinstance(inflight, str)
                and inflight
                and status == "active"
                and result_attempts == list(range(1, attempts))
            )
            if (
                not 1 <= attempts <= 4
                or not isinstance(raw_results, (list, tuple))
                or len(result_items) != len(raw_results)
                or any(result is None for result in results)
                or not (full_coverage or interrupted_coverage or inflight_coverage)
                or not isinstance(target_id, str)
                or not target_id
                or not isinstance(target_fingerprint, str)
                or not target_fingerprint
                or (reason is not None and not isinstance(reason, str))
                or (updated_at is not None and not isinstance(updated_at, str))
                or (
                    inflight is not None
                    and (not isinstance(inflight, str) or not inflight)
                )
                or (status != "active" and inflight is not None)
                or status not in {"active", "ready", "completed", "refused", "aborted"}
            ):
                return malformed_entry()
            return {
                "target_id": target_id,
                "target_fingerprint": target_fingerprint,
                "attempts": attempts,
                "status": status,
                # Boolean state is enough for orphan detection; the unguessable
                # completion token and process owner never belong in /status.
                "inflight": bool(inflight),
                "results": projected_results,
                "reason": reason,
                "updated_at": updated_at,
            }

        with self._repeat_lock:
            raw_targets = payload.get("targets") or {}
            raw_targets = raw_targets if isinstance(raw_targets, Mapping) else {}
            public_targets = {
                target_id: public_entry(entry)
                for target_id, entry in raw_targets.items()
                if isinstance(target_id, str) and target_id
            }
            comparison = payload.get("comparison")
            public_comparison = None
            if isinstance(comparison, Mapping):
                comparison_set_id = comparison.get("comparison_set_id")
                fingerprint = comparison.get("fingerprint")
                if isinstance(comparison_set_id, str) and isinstance(fingerprint, str):
                    public_comparison = {
                        "comparison_set_id": comparison_set_id,
                        "fingerprint": fingerprint,
                    }
            schema_version = payload.get("schema_version")
            kind = payload.get("kind")
            durable_status = payload.get("status")
            durable_updated_at = payload.get("updated_at")
            self._durable_repeat_progress = {
                "schema_version": (
                    schema_version
                    if isinstance(schema_version, int)
                    and not isinstance(schema_version, bool)
                    else None
                ),
                "kind": kind if isinstance(kind, str) else None,
                "status": (durable_status if isinstance(durable_status, str) else None),
                "comparison": public_comparison,
                "targets": public_targets,
                "updated_at": (
                    durable_updated_at if isinstance(durable_updated_at, str) else None
                ),
            }
            raw_failures = payload.get("failures") or {}
            raw_failures = (
                raw_failures if isinstance(raw_failures, Mapping) else {}
            )
            failures = {
                target_id: public_entry(entry)
                for target_id, entry in raw_failures.items()
                if isinstance(target_id, str) and target_id
            }
            for target_id, entry in public_targets.items():
                if isinstance(entry, Mapping) and entry.get("status") in {
                    "aborted",
                    "refused",
                    "malformed",
                }:
                    failures[str(target_id)] = dict(entry)
            for target_id, failure in failures.items():
                if isinstance(failure, Mapping):
                    self._repeat_failures[str(target_id)] = dict(failure)

    def repeat_snapshot(self) -> dict[str, Any]:
        from jasper.active_speaker.commissioning_capture import (
            DEFAULT_REPEAT_TARGET,
            aggregate_driver_repeats,
        )

        from jasper.active_speaker.repeat_admission import MAX_ATTEMPTS
        from jasper.active_speaker.crossover_eligibility import (
            mapping_sequence,
            nonnegative_int,
        )

        with self._repeat_lock:
            targets: dict[str, Any] = {}
            for (
                comparison_set_id,
                target_fingerprint,
            ), session in self._repeat_sessions.items():
                item_map = session.get("items") or {}
                items = [dict(item_map[index]) for index in sorted(item_map)]
                aggregate = aggregate_driver_repeats(
                    items, target=DEFAULT_REPEAT_TARGET
                )
                targets[str(session.get("target_id") or "")] = {
                    "comparison_set_id": comparison_set_id,
                    "target_fingerprint": target_fingerprint,
                    "attempts": len(items),
                    "accepted": aggregate["accepted"],
                    "target": DEFAULT_REPEAT_TARGET,
                    "needed_recapture": aggregate["needed_recapture"],
                }

            # Playback admission is the authority for attempts, including
            # captures that failed in transport before acoustic analysis.  Use
            # its ledger for user-facing counts so the UI cannot promise a
            # fifth attempt while the safety gate correctly refuses one.
            durable_targets = self._durable_repeat_progress.get("targets") or {}
            for target_id, raw in durable_targets.items():
                if not isinstance(raw, Mapping):
                    continue
                entry = dict(raw)
                results = list(mapping_sequence(entry.get("results")))
                attempts = nonnegative_int(entry.get("attempts"))
                accepted = sum(
                    1 for result in results if result.get("accepted") is True
                )
                displayed = dict(targets.get(str(target_id)) or {})
                displayed.update({
                    "comparison_set_id": (
                        self._durable_repeat_progress.get("comparison") or {}
                    ).get("comparison_set_id"),
                    "target_fingerprint": entry.get("target_fingerprint"),
                    "attempts": attempts,
                    "accepted": accepted,
                    "target": DEFAULT_REPEAT_TARGET,
                    "needed_recapture": (
                        entry.get("status") == "active"
                        and attempts < MAX_ATTEMPTS
                        and accepted < DEFAULT_REPEAT_TARGET
                    ),
                    "status": entry.get("status"),
                })
                targets[str(target_id)] = displayed

            return {
                "targets": targets,
                "failures": dict(self._repeat_failures),
                "durable": dict(self._durable_repeat_progress),
            }

    def level_match_snapshot(
        self, *, current_context_id: str | None = None
    ) -> dict[str, Any]:
        context_valid = (
            current_context_id is None
            or self.context_id == current_context_id
        )
        locks = self.driver_level_locks()
        missing = [target_id for target_id in self._targets if target_id not in locks]
        from jasper.active_speaker.capture_geometry import driver_level_geometry

        reference_axis_driver_locks: dict[str, float] = {}
        for target_id, target in self._targets.items():
            geometry = driver_level_geometry(
                str(target.get("speaker_group_id") or ""),
                str(target.get("role") or ""),
                "reference_axis",
            )
            outcome = self._outcomes.get(geometry)
            locked = (
                outcome.ramp.locked_main_volume_db
                if outcome is not None
                else None
            )
            if (
                not isinstance(locked, bool)
                and isinstance(locked, (int, float))
                and math.isfinite(float(locked))
                and float(locked) <= 0
            ):
                reference_axis_driver_locks[target_id] = float(locked)
        return {
            "running": self._running is not None,
            "locks": self.level_lock_store.snapshot(),
            "last": self._last.snapshot() if self._last is not None else None,
            "context_id": self.context_id,
            "valid": context_valid,
            "targets": list(self._targets.values()),
            "driver_level_locks": locks,
            "reference_axis_driver_locks": reference_axis_driver_locks,
            "unresolved_volume_safety": self.unresolved_volume_safety,
            "missing_targets": missing,
            "next_target": self._targets.get(missing[0]) if missing else None,
            "ready": bool(
                self._targets
                and not missing
                and context_valid
                and self._volume_safety_state is None
            ),
            "run": self.level_run_snapshot(),
            "repeats": self.repeat_snapshot(),
        }


_LEVEL_LEASE = CrossoverLevelLease(
    volume_safety_state_path=_DEFAULT_VOLUME_SAFETY_STATE_PATH,
    level_run_state_path=_level_run_state_path(),
)
_COMMISSIONING_RUN_STORE = CommissioningRunStore()


def level_lease() -> CrossoverLevelLease:
    return _LEVEL_LEASE


def claim_level_run_owner() -> dict[str, Any] | None:
    """Service-lifecycle adapter for the Room-owned web entry point."""

    return _LEVEL_LEASE.claim_level_run_owner()


def claim_commissioning_run_owner() -> CommissioningRunHandle | None:
    """Retire callbacks owned by a prior correction-web process."""

    return _COMMISSIONING_RUN_STORE.claim_owner()


def begin_commissioning_run(
    comparison_set: Mapping[str, Any],
) -> CommissioningRunHandle:
    """Bind a fresh durable run to one authoritative comparison session."""

    if not comparison_set_valid(comparison_set):
        raise ValueError("commissioning comparison set is invalid")
    session_id = comparison_set.get("bundle_session_id")
    session_fingerprint = comparison_set.get("fingerprint")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError(
            "commissioning run requires a fresh production evidence bundle"
        )
    if (
        not isinstance(session_fingerprint, str)
        or len(session_fingerprint) != 64
        or any(
            character not in "0123456789abcdef"
            for character in session_fingerprint
        )
    ):
        raise ValueError("commissioning comparison identity is unavailable")
    return _COMMISSIONING_RUN_STORE.replace_current(
        session_id=session_id,
        session_fingerprint=session_fingerprint,
    )


def commissioning_run_status(
    comparison_set: Mapping[str, Any] | None,
    *,
    expected_topology_id: str | None,
    expected_profile_context_id: str | None,
) -> dict[str, Any]:
    """Project durable lifecycle authority without exposing process identity."""

    try:
        snapshot = _COMMISSIONING_RUN_STORE.snapshot()
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "status": "unavailable",
            "reason": "commissioning_run_state_unavailable",
            "error_type": type(exc).__name__,
        }
    current = snapshot.get("current")
    if not isinstance(current, Mapping):
        return {
            "status": "not_started",
            "reason": "commissioning_run_not_started",
            "state_fingerprint": snapshot.get("fingerprint"),
        }
    comparison_current = bool(
        isinstance(comparison_set, Mapping)
        and comparison_set_valid(comparison_set)
        and isinstance(expected_topology_id, str)
        and bool(expected_topology_id)
        and isinstance(expected_profile_context_id, str)
        and bool(expected_profile_context_id)
        and comparison_set.get("topology_id") == expected_topology_id
        and comparison_set.get("profile_context_id")
        == expected_profile_context_id
        and current.get("session_id") == comparison_set.get("bundle_session_id")
        and current.get("session_fingerprint") == comparison_set.get("fingerprint")
    )
    journal = current.get("transition_journal")
    last_transition = journal[-1] if isinstance(journal, list) and journal else None
    attempts = current.get("attempts")
    result = {
        "status": "current" if comparison_current else "stale",
        "reason": (
            None if comparison_current else "commissioning_comparison_set_changed"
        ),
        "session_id": current.get("session_id"),
        "run_id": current.get("run_id"),
        "owner_generation": current.get("owner_generation"),
        "lifecycle_state": current.get("lifecycle_state"),
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
        "last_transition": last_transition,
        "updated_at": current.get("updated_at"),
        "state_fingerprint": snapshot.get("fingerprint"),
    }
    if comparison_current:
        result["profile_context_id"] = expected_profile_context_id
        try:
            from jasper.active_speaker.bundles import sessions_dir
            from jasper.active_speaker.commissioning_evidence_store import (
                CommissioningEvidenceStore,
            )
            from jasper.active_speaker.commissioning_isolated_producer import (
                isolated_evidence_status,
                resume_isolated_evidence,
            )

            run = _COMMISSIONING_RUN_STORE.current_handle()
            if run is None:
                raise ValueError("current commissioning run disappeared")
            evidence_store = CommissioningEvidenceStore.open(
                sessions_dir() / run.session_id,
                expected_session_id=run.session_id,
            )
            resume_isolated_evidence(
                run=run,
                run_store=_COMMISSIONING_RUN_STORE,
                evidence_store=evidence_store,
            )
            result["isolated_evidence"] = isolated_evidence_status(
                run=run,
                run_store=_COMMISSIONING_RUN_STORE,
                evidence_store=evidence_store,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            result["isolated_evidence"] = {
                "status": "unavailable",
                "reason": "isolated_evidence_state_unavailable",
                "error_type": type(exc).__name__,
            }
    return result


def _commissioning_authority_snapshot() -> Any:
    """Load the exact current product state consumed by the Active host."""

    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.commissioning_host import (
        CommissioningHostAuthoritySnapshot,
    )
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.active_speaker.profile import ActiveSpeakerPreset
    from jasper.output_topology import load_output_topology

    topology = load_output_topology()
    applied_profile_raw = load_applied_baseline_profile_state()
    if not isinstance(applied_profile_raw, Mapping):
        raise ValueError("the protected applied crossover profile is unavailable")
    applied_profile = dict(applied_profile_raw)
    snapshot = applied_profile.get("recomposition_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    preset_raw = snapshot.get("preset")
    if not isinstance(preset_raw, Mapping):
        raise ValueError("the protected applied crossover preset is unavailable")
    preset = ActiveSpeakerPreset.from_mapping(dict(preset_raw))
    draft = load_design_draft(topology=topology)
    safety_profile = draft.get("driver_safety_profile")
    if not isinstance(safety_profile, Mapping):
        raise ValueError("the confirmed driver safety profile is unavailable")
    measurements = load_measurement_state(topology)
    comparison_set = measurements.get("active_comparison_set")
    if not isinstance(comparison_set, Mapping) or not comparison_set_valid(
        comparison_set
    ):
        raise ValueError("the active crossover comparison set is unavailable")
    calibration_id = str(comparison_set.get("calibration_id") or "")
    if not calibration_id:
        raise ValueError("a calibrated measurement microphone is required")
    from jasper.audio_measurement.calibration import (
        CalibrationCurve,
        load_calibration_record,
    )

    calibration = load_calibration_record(calibration_id)
    calibration_curve = calibration.curve
    if not isinstance(calibration_curve, CalibrationCurve):
        raise ValueError("the selected microphone calibration is unavailable")
    return CommissioningHostAuthoritySnapshot(
        topology=topology,
        preset=preset,
        safety_profile=dict(safety_profile),
        comparison_set=dict(comparison_set),
        applied_profile=dict(applied_profile),
        calibration_id=calibration_id,
        calibration=calibration_curve,
    )


def commissioning_recorder_binding() -> tuple[Any, str]:
    """Return the exact persisted calibration and recorder identity for capture."""

    from jasper.audio_measurement.calibration import load_calibration_record

    authority = _commissioning_authority_snapshot()
    device_sha256 = str(authority.comparison_set.get("device_sha256") or "")
    if len(device_sha256) != 64:
        raise ValueError("the commissioning recorder identity is unavailable")
    return load_calibration_record(authority.calibration_id), device_sha256


def _commissioning_capture_service() -> Any:
    from jasper.active_speaker.bundles import sessions_dir
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )
    from jasper.active_speaker.commissioning_service import (
        CommissioningCaptureService,
    )

    run = _COMMISSIONING_RUN_STORE.current_handle()
    if run is None:
        raise ValueError("the active crossover commissioning run is not started")
    evidence_store = CommissioningEvidenceStore.open(
        sessions_dir() / run.session_id,
        expected_session_id=run.session_id,
    )
    return CommissioningCaptureService(
        run=run,
        run_store=_COMMISSIONING_RUN_STORE,
        evidence_store=evidence_store,
        load_current_authority=_commissioning_authority_snapshot,
    )


def commissioning_region_status() -> dict[str, Any]:
    """Project strict summed-region progress from the one Active authority."""

    try:
        return _commissioning_capture_service().status()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        code = getattr(exc, "code", "region_commissioning_unavailable")
        return {
            "schema_version": 1,
            "kind": "jts_active_region_commissioning_status",
            "status": "unavailable",
            "reason": str(code),
            "detail": str(exc),
        }


def attest_commissioning_region_geometry(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Persist the operator's signed path difference for the current region."""

    expected = str(raw.get("expected_target_fingerprint") or "")
    if not expected:
        raise ValueError("the current crossover region identity is required")
    return _commissioning_capture_service().attest_geometry(
        expected_target_fingerprint=expected,
        signed_acoustic_path_difference_mm=raw.get(
            "signed_acoustic_path_difference_mm"
        ),
    )


def prepare_commissioning_candidate() -> dict[str, Any]:
    """Publish the exact current measured candidate, or idempotently reopen it."""

    from jasper.active_speaker.commissioning_service import (
        CommissioningServiceError,
    )

    service = _commissioning_capture_service()
    try:
        candidate = service.publish_candidate()
    except CommissioningServiceError as exc:
        if exc.code != "candidate_scoring_failed":
            raise
        status = service.status()
        if status.get("status") != "candidate_refused":
            raise RuntimeError(
                "candidate refusal did not reopen as authoritative status"
            ) from exc
        return status
    return {"status": "candidate_ready", "candidate": candidate}


async def restore_commissioning_candidate(
    *, camilla_factory: CamillaFactory
) -> dict[str, Any]:
    """Restore a pending strict candidate apply from its exact predecessor."""

    from jasper.active_speaker.commissioning_service import (
        commissioning_runtime_port,
    )

    _LEVEL_LEASE.assert_volume_safety_resolved()
    service = _commissioning_capture_service()
    cam = camilla_factory()
    return await service.restore_candidate(
        runtime_port=commissioning_runtime_port(cam),
        load_config_path=lambda path: cam.set_config_file_path(
            path, best_effort=False
        ),
    )


async def capture_next_commissioning_region(
    raw_capture_transport: Any,
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Execute one server-selected normal/reverse/delay recorder capture."""

    from jasper.active_speaker.commissioning_service import (
        CommissioningServiceError,
        commissioning_runtime_port,
    )
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR

    _LEVEL_LEASE.assert_volume_safety_resolved()
    service = _commissioning_capture_service()
    before = service.status()
    if before.get("status") != "collecting":
        raise ValueError("the commissioning run has no recorder capture ready")
    camilla = camilla_factory()
    capture = await service.capture_next(
        commissioning_runtime_port(camilla),
        raw_capture_transport=raw_capture_transport,
        config_dir=str(DEFAULT_CAMILLA_CONFIG_DIR),
    )
    if capture is None:
        raise RuntimeError("the server did not issue the expected recorder capture")
    after = service.status()
    if after.get("status") == "measured":
        try:
            service.publish_candidate()
        except CommissioningServiceError as exc:
            if exc.code != "candidate_scoring_failed":
                raise
        after = service.status()
    return {
        "status": "recorded",
        "capture_fingerprint": capture.fingerprint,
        "evidence_kind": capture.evidence_kind,
        "speaker_group_id": capture.speaker_group_id,
        "region_id": capture.region_id,
        "next": after,
    }


async def capture_next_commissioning_verification(
    raw_capture_transport: Any,
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Capture one server-selected post-apply combined-response repeat."""

    from jasper.active_speaker.commissioning_service import commissioning_runtime_port
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR

    _LEVEL_LEASE.assert_volume_safety_resolved()
    service = _commissioning_capture_service()
    if service.status().get("status") != "applied_unverified":
        raise ValueError("the commissioning run has no post-apply capture ready")
    camilla = camilla_factory()
    return await service.capture_post_apply(
        commissioning_runtime_port(camilla),
        raw_capture_transport=raw_capture_transport,
        config_dir=str(DEFAULT_CAMILLA_CONFIG_DIR),
    )


def status_payload() -> dict[str, Any]:
    """Return active-crossover targets and saved measurement evidence."""

    payload = web_measurement.status_payload()
    payload["commission"] = web_commissioning.commission_status_payload()
    # Layer-A gate: only active (`active_2_way` / `active_3_way`) speakers have
    # driver/summed targets; a `full_range_passive` speaker has none, so
    # `active=False` is the honest "this speaker has no crossover to tune" flag
    # for the envelope-driven page to consume. Derived from the already-computed
    # targets. (The active-only block below does its own fail-soft topology
    # read for the safety-profile evaluation.) Pinned by
    # tests/test_web_correction_crossover_flow.py.
    targets_raw = payload.get("targets")
    targets: dict[str, Any] = targets_raw if isinstance(targets_raw, dict) else {}
    driver_count = len(targets.get("drivers") or [])
    summed_count = len(targets.get("summed") or [])
    payload["active"] = bool(driver_count or summed_count)
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.setup_status import read_active_speaker_setup_status

    payload["setup"] = read_active_speaker_setup_status()
    # The envelope gates the measurement flow on the driver safety profile's
    # own confirmed-and-current verdict (evaluate_driver_safety_profile), not
    # on "protected setup" readiness alone: JTS3 hardware evidence showed an
    # operator admitted through level locks into driver sweeps while the
    # profile still self-described as incomplete, only refused by the deep
    # excitation admission after burning acceptance repeats. Load fresh (not
    # the design draft's own stale save-time evaluation) so a topology change
    # since the last save is honoured; unreadable is reported as None so the
    # envelope fails closed rather than silently treating it as authorized.
    if payload["active"]:
        from jasper.active_speaker.design_draft import load_design_draft
        from jasper.output_topology import load_output_topology

        try:
            safety_topology = load_output_topology()
            safety_draft = load_design_draft(topology=safety_topology)
            payload["driver_safety_profile_evaluation"] = safety_draft.get(
                "driver_safety_profile_evaluation"
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            payload["driver_safety_profile_evaluation"] = None
        # The envelope's "speaker_setup" gate needs to tell "anchored
        # mid-sequence by design" (PR #1523's crash-safe staged-config
        # posture between capture attempts) apart from "setup genuinely
        # unfinished". The capture-entry stash's presence IS that sequence
        # boundary — see capture_entry_anchor's module docstring and
        # crossover_envelope._setup_ready. pending_entry() is fail-soft
        # (returns None on an unreadable/malformed stash), so a read
        # failure here degrades to the pre-#1523 strict gate rather than a
        # false bypass.
        from jasper.active_speaker.capture_entry_anchor import pending_entry

        payload["capture_entry_pending"] = pending_entry() is not None
    # Level evidence is tied to the immutable profile that is actually loaded,
    # not the mutable next-design candidate. Capturing the first driver updates
    # candidate evidence and must not invalidate the safe active graph or its
    # near-field gain reference.
    setup_profile = payload["setup"].get("protected_profile")
    current_context_id = (
        str(setup_profile.get("candidate_fingerprint") or "") or None
        if isinstance(setup_profile, Mapping)
        else None
    )
    from jasper.active_speaker import repeat_admission

    comparison_set = (payload.get("measurements") or {}).get(
        "active_comparison_set"
    )
    payload["region_commissioning"] = commissioning_region_status()
    # A successful Active-owned region projection has already revalidated the
    # durable comparison against the exact retained apply predecessor.  After
    # apply, that is the evidence context; the newly installed profile is the
    # verification subject, not a reason to stale the run that installed it.
    region_context_id = str(
        payload["region_commissioning"].get("profile_context_id") or ""
    )
    if (
        isinstance(comparison_set, Mapping)
        and region_context_id
        and comparison_set.get("profile_context_id") == region_context_id
    ):
        current_context_id = region_context_id
    payload["commissioning_run"] = commissioning_run_status(
        comparison_set if isinstance(comparison_set, Mapping) else None,
        expected_topology_id=(payload.get("topology") or {}).get("topology_id"),
        expected_profile_context_id=current_context_id,
    )
    try:
        durable_repeats = repeat_admission.snapshot(
            comparison_set if isinstance(comparison_set, Mapping) else None
        )
    except (OSError, RuntimeError, ValueError) as exc:
        durable_repeats = {
            "status": "unavailable",
            "targets": {},
            "error": str(exc),
        }
    _LEVEL_LEASE.set_durable_repeat_progress(durable_repeats)
    payload["level_match"] = _LEVEL_LEASE.level_match_snapshot(
        current_context_id=current_context_id
    )
    payload["applied_profile"] = load_applied_baseline_profile_state()
    logger.debug(
        "crossover status active=%s drivers=%d summed=%d",
        payload["active"],
        driver_count,
        summed_count,
    )
    return payload


async def apply_profile(
    *,
    tuning_owner: str,
    expected_candidate_fingerprint: str,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Atomically apply an explicitly manual or automatic Layer-A profile."""
    _LEVEL_LEASE.assert_volume_safety_resolved()
    if tuning_owner not in {"manual", "automatic"}:
        raise ValueError("tuning_owner must be 'manual' or 'automatic'")
    if tuning_owner == "automatic":
        try:
            commissioning_service = _commissioning_capture_service()
            lifecycle = commissioning_service.run_store.lifecycle_state(
                commissioning_service.run
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            commissioning_service = None
            lifecycle = None
            try:
                current = _COMMISSIONING_RUN_STORE.snapshot().get("current")
            except (OSError, RuntimeError, TypeError, ValueError) as state_exc:
                raise ValueError(
                    "automatic crossover apply requires readable commissioning authority"
                ) from state_exc
            if isinstance(current, Mapping):
                raise ValueError(
                    "the strict commissioning candidate authority is unavailable"
                ) from exc
        if lifecycle in {
            "candidate_ready",
            "applied_unverified",
            "rolled_back",
            "blocked_live_state_unknown",
        }:
            if lifecycle == "blocked_live_state_unknown":
                raise ValueError(
                    "the previous crossover must be restored before applying"
                )
            from jasper.active_speaker.commissioning_service import (
                commissioning_runtime_port,
            )

            cam = camilla_factory()
            payload = await commissioning_service.apply_candidate(
                expected_candidate_fingerprint=expected_candidate_fingerprint,
                runtime_port=commissioning_runtime_port(cam),
                load_config_path=lambda path: cam.set_config_file_path(
                    path, best_effort=False
                ),
            )
            log_event(
                logger,
                "correction.crossover_profile_apply",
                status=payload.get("status"),
                tuning_owner=tuning_owner,
                authority="strict_commissioning_candidate",
                candidate_fingerprint=expected_candidate_fingerprint,
            )
            return payload
        if lifecycle is not None:
            raise ValueError(
                "automatic crossover apply requires a reviewed strict candidate, "
                f"not commissioning lifecycle {lifecycle}"
            )
    from jasper.active_speaker.baseline_profile import apply_baseline_profile
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.output_topology import load_output_topology

    topology = load_output_topology()
    draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=draft)
    measurements = load_measurement_state(topology)
    if tuning_owner == "automatic":
        from jasper.active_speaker import repeat_admission

        comparison_set = measurements.get("active_comparison_set")
        if not isinstance(comparison_set, Mapping):
            raise ValueError(
                "automatic crossover apply requires a current repeat-bound "
                "measurement set"
            )
        try:
            repeat_state = repeat_admission.snapshot(comparison_set)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                "crossover repeat safety state is unavailable; rerun the driver "
                "level check before apply"
            ) from exc
        from jasper.active_speaker.crossover_eligibility import (
            automatic_measurement_eligibility,
        )
        from jasper.active_speaker.measurement import active_driver_targets
        from jasper.active_speaker.setup_status import (
            read_active_speaker_setup_status,
        )

        setup = read_active_speaker_setup_status()
        protected_profile = setup.get("protected_profile")
        profile_context_id = (
            str(protected_profile.get("candidate_fingerprint") or "")
            if isinstance(protected_profile, Mapping)
            else ""
        )
        eligibility = automatic_measurement_eligibility(
            topology_id=topology.topology_id,
            profile_context_id=profile_context_id,
            driver_targets=active_driver_targets(topology),
            measurements=measurements,
            repeat_state=repeat_state,
        )
        if not eligibility.ready:
            raise ValueError(
                "current near-field and fixed-axis crossover evidence and "
                "their exact repeat persistence must all be complete; resume "
                "the guided driver measurements before automatic apply"
            )
    applied = load_applied_baseline_profile_state()
    legacy_manual_profile = (
        applied
        if tuning_owner == "manual"
        and isinstance(applied, Mapping)
        and not isinstance(applied.get("recomposition_snapshot"), Mapping)
        else None
    )

    def refresh_inputs():
        current_topology = load_output_topology()
        current_draft = load_design_draft()
        current_preview = load_crossover_preview(current_design_draft=current_draft)
        current_measurements = load_measurement_state(current_topology)
        return (
            current_topology,
            current_draft,
            current_preview,
            current_measurements,
        )

    cam = camilla_factory()
    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
        tuning_owner=tuning_owner,
        preserved_applied_profile=legacy_manual_profile,
        expected_candidate_fingerprint=expected_candidate_fingerprint,
        refresh_inputs=refresh_inputs,
    )
    issue_codes = [
        str(issue.get("code"))
        for issue in payload.get("issues") or []
        if isinstance(issue, Mapping) and issue.get("code")
    ]
    log_event(
        logger,
        "correction.crossover_profile_apply",
        status=payload.get("status"),
        tuning_owner=tuning_owner,
        issue_count=len(issue_codes),
        issue_codes=issue_codes,
        refusal_reason=(issue_codes[0] if payload.get("status") == "blocked" else None),
    )
    return payload


async def apply_measured_profile(
    *, expected_candidate_fingerprint: str, camilla_factory: CamillaFactory
) -> dict[str, Any]:
    """Compatibility wrapper for callers that explicitly apply measurements."""
    return await apply_profile(
        tuning_owner="automatic",
        expected_candidate_fingerprint=expected_candidate_fingerprint,
        camilla_factory=camilla_factory,
    )


async def start_driver_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Start the safe per-driver audible confirmation path."""

    _LEVEL_LEASE.assert_volume_safety_resolved()
    payload = await web_commissioning.start_driver_test(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
    )
    log_event(
        logger,
        "correction.crossover_driver_test",
        status=payload.get("status"),
        group_id=raw.get("speaker_group_id"),
        role=raw.get("role"),
    )
    return payload


async def confirm_driver_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Record the operator acknowledgement for a per-driver test."""

    payload = await web_commissioning.confirm_driver_test(
        raw,
        camilla_factory=camilla_factory,
    )
    log_event(
        logger,
        "correction.crossover_driver_confirm",
        status=payload.get("status"),
        outcome=raw.get("outcome"),
    )
    return payload


async def abort_driver_test(*, camilla_factory: CamillaFactory) -> dict[str, Any]:
    """Stop any per-driver audible test and re-mute the transient graph."""

    payload = await web_commissioning.abort_driver_test(
        camilla_factory=camilla_factory,
    )
    log_event(
        logger,
        "correction.crossover_driver_abort",
        status=payload.get("status"),
    )
    return payload


async def start_summed_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Run the safe combined-driver audible test."""

    _LEVEL_LEASE.assert_volume_safety_resolved()
    payload = await web_commissioning.start_summed_test(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
    )
    log_event(
        logger,
        "correction.crossover_summed_test",
        status=payload.get("status"),
        group_id=raw.get("speaker_group_id"),
    )
    return payload


async def play_driver_capture_sweep(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
    applied_profile: dict[str, Any] | None = None,
    locked_main_volume_db: float | None = None,
    volume_lease_prepared: bool = False,
    fanin_gate_context: web_commissioning.FaninGateContext | None = None,
) -> dict[str, Any]:
    """Play a mic-capture sweep through an already-confirmed driver.

    ``fanin_gate_context`` threads through to
    ``web_commissioning.play_driver_capture_sweep`` — set only by the
    relay flow when this sweep runs inside a correction measurement window
    (see ``FaninGateContext``).
    """

    if volume_lease_prepared:
        _LEVEL_LEASE.assert_sweep_volume_owned(
            source="driver_sweep",
            speaker_group_id=str(raw.get("speaker_group_id") or ""),
            role=str(raw.get("role") or "").lower(),
        )
    else:
        _LEVEL_LEASE.assert_volume_safety_resolved()
    payload = await web_commissioning.play_driver_capture_sweep(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
        applied_profile=applied_profile,
        locked_main_volume_db=locked_main_volume_db,
        fanin_gate_context=fanin_gate_context,
    )
    log_event(
        logger,
        "correction.crossover_driver_capture_sweep",
        status=payload.get("status"),
        group_id=raw.get("speaker_group_id"),
        role=raw.get("role"),
    )
    return payload


async def play_summed_capture_sweep(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
    volume_lease_prepared: bool = False,
) -> dict[str, Any]:
    """Play a mic-capture sweep through an already-tested summed path."""

    if volume_lease_prepared:
        _LEVEL_LEASE.assert_sweep_volume_owned(
            source="summed_sweep",
            speaker_group_id=str(raw.get("speaker_group_id") or ""),
            role="summed",
        )
    else:
        _LEVEL_LEASE.assert_volume_safety_resolved()
    payload = await web_commissioning.play_summed_capture_sweep(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
    )
    log_event(
        logger,
        "correction.crossover_summed_capture_sweep",
        status=payload.get("status"),
        group_id=raw.get("speaker_group_id"),
    )
    return payload


def record_driver_capture(
    raw: Mapping[str, Any],
    wav_bytes: bytes,
    *,
    placement_proof: Mapping[str, Any] | None = None,
    admission_handoff: Mapping[str, Any] | None = None,
    preset: Any = None,
    repeat_store: Any = None,
) -> dict[str, Any]:
    """Analyze one secure browser WAV and record per-driver evidence."""

    _LEVEL_LEASE.assert_volume_safety_resolved()

    def record_authoritative(**inputs: Any) -> Mapping[str, Any]:
        from jasper.active_speaker.baseline_profile import (
            load_applied_baseline_profile_state,
        )
        from jasper.active_speaker.bundles import sessions_dir
        from jasper.active_speaker.commissioning_evidence_store import (
            CommissioningEvidenceStore,
        )
        from jasper.active_speaker.commissioning_isolated_producer import (
            promote_isolated_driver_capture,
        )

        run = _COMMISSIONING_RUN_STORE.current_handle()
        comparison_set = inputs.get("comparison_set")
        if (
            run is None
            or not isinstance(comparison_set, Mapping)
            or run.session_id != comparison_set.get("bundle_session_id")
            or run.session_fingerprint != comparison_set.get("fingerprint")
        ):
            raise ValueError(
                "fixed-axis capture has no current exact commissioning run"
            )
        applied = load_applied_baseline_profile_state()
        if not isinstance(applied, Mapping):
            raise ValueError(
                "fixed-axis capture has no protected applied profile authority"
            )
        evidence_store = CommissioningEvidenceStore.open(
            sessions_dir() / run.session_id,
            expected_session_id=run.session_id,
        )
        return promote_isolated_driver_capture(
            **inputs,
            applied_profile=applied,
            run=run,
            run_store=_COMMISSIONING_RUN_STORE,
            evidence_store=evidence_store,
        )

    transaction = getattr(repeat_store, "repeat_transaction", None)
    if callable(transaction):
        with transaction():
            payload = web_measurement.record_driver_capture(
                raw,
                wav_bytes,
                placement_proof=placement_proof,
                admission_handoff=admission_handoff,
                preset=preset,
                repeat_store=repeat_store,
                authoritative_recorder=(
                    record_authoritative if admission_handoff is not None else None
                ),
            )
    else:
        payload = web_measurement.record_driver_capture(
            raw,
            wav_bytes,
            placement_proof=placement_proof,
            admission_handoff=admission_handoff,
            preset=preset,
            repeat_store=repeat_store,
            authoritative_recorder=(
                record_authoritative if admission_handoff is not None else None
            ),
        )
    log_event(
        logger,
        "correction.crossover_driver_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=raw.get("speaker_group_id"),
        role=raw.get("role"),
        placement_policy=(placement_proof or {}).get("policy_id"),
        authoritative_status=(payload.get("authoritative_evidence") or {}).get(
            "status"
        ),
    )
    return payload


def record_summed_capture(
    raw: Mapping[str, Any],
    wav_bytes: bytes,
    *,
    placement_proof: Mapping[str, Any] | None = None,
    preset: Any = None,
) -> dict[str, Any]:
    """Analyze one secure browser WAV and record summed-crossover evidence."""

    _LEVEL_LEASE.assert_volume_safety_resolved()
    payload = web_measurement.record_summed_capture(
        raw,
        wav_bytes,
        placement_proof=placement_proof,
        preset=preset,
    )
    log_event(
        logger,
        "correction.crossover_summed_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=raw.get("speaker_group_id"),
        verdict=payload.get("verdict"),
        placement_policy=(placement_proof or {}).get("policy_id"),
    )
    return payload
