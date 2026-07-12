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
import logging
import math
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from jasper.active_speaker import web_commissioning, web_measurement
from jasper.log_event import log_event

logger = logging.getLogger(__name__)
EMERGENCY_SWEEP_VOLUME_DB = -60.0
CamillaFactory = Callable[[], Any]

if TYPE_CHECKING:
    from jasper.correction.level_match import LevelMatchOutcome, LevelMatchSession


class CrossoverLevelLease:
    """Process-scoped near-field gain lease for Layer-A measurements.

    The shared :class:`LevelMatchSession` owns ramp math and relay semantics;
    this thin domain owner supplies only single-flight lifetime, observability,
    and an in-memory target/original pair. The target is asserted only inside a
    sweep window and restored in that window's ``finally``. It deliberately
    owns no CamillaDSP or relay client.
    """

    def __init__(self) -> None:
        from jasper.correction.level_match import LevelLockStore

        self.session_id = "active-crossover"
        self.level_lock_store = LevelLockStore()
        self._running: LevelMatchSession | None = None
        self._last: LevelMatchOutcome | None = None
        self._active_outcome: LevelMatchOutcome | None = None
        self._outcomes: dict[str, LevelMatchOutcome] = {}
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

    async def run_level_match(self, geometry: str, **ports: Any) -> Any:
        from jasper.audio_measurement.ramp import MeasurementRamp
        from jasper.correction.level_match import LevelMatchSession

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
        context_id = str(ports.pop("context_id", "") or "") or None
        set_main_volume_db = ports.get("set_main_volume_db")
        run = LevelMatchSession(
            session_id=self.session_id,
            store=self.level_lock_store,
            config=MeasurementRamp.from_env(allow_bounded_low_level=True),
        )
        self._running = run
        try:
            outcome = await run.run_for_geometry(geometry, **ports)
        finally:
            if self._running is run:
                self._running = None
        self._last = outcome
        self._active_outcome = outcome
        self._outcomes[geometry] = outcome
        if outcome.locked:
            if not callable(set_main_volume_db):
                raise RuntimeError("crossover level match has no volume restore port")
            restored = await self.restore_level_match_volume(set_main_volume_db)
            if not restored:
                raise RuntimeError(
                    "crossover level locked, but the listening volume could "
                    "not be restored"
                )
            self.context_id = context_id
        return outcome

    def invalidate_comparison_context(self) -> None:
        """Drop a prior lock/setup before a newly acquired level run begins."""

        from jasper.correction.level_match import LevelLockStore

        if self._running is not None:
            raise RuntimeError("cannot invalidate a running crossover level match")
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

    async def restore_level_match_volume(self, set_main_volume_db: Any) -> bool:
        from jasper.audio_measurement.ramp import RampState

        async with self._restore_lock:
            outcome = self._active_outcome or self._last
            if outcome is None or outcome.ramp.state is not RampState.LOCKED:
                return False
            ramp = outcome.ramp
            if ramp.restored or ramp.original_main_volume_db is None:
                return False
            applied = await set_main_volume_db(float(ramp.original_main_volume_db))
            if applied is False:
                log_event(
                    logger,
                    "correction.crossover_level_volume_restore_failed",
                    level=logging.ERROR,
                    to_db=f"{ramp.original_main_volume_db:.1f}",
                )
                return False
            ramp.restored = True
            self._active_outcome = None
            log_event(
                logger,
                "correction.crossover_level_volume_restored",
                to_db=f"{ramp.original_main_volume_db:.1f}",
            )
            return True

    async def acquire_driver_sweep_volume(
        self,
        speaker_group_id: str,
        role: str,
        get_main_volume_db: Any,
        set_main_volume_db: Any,
    ) -> bool:
        """Acquire a sweep-scoped lease at this driver's measured target."""

        geometry = f"near_field_driver:{speaker_group_id}:{role}"
        return await self._acquire_sweep_volume(
            self._outcomes.get(geometry), get_main_volume_db, set_main_volume_db
        )

    async def acquire_summed_sweep_volume(
        self,
        get_main_volume_db: Any,
        set_main_volume_db: Any,
    ) -> bool:
        """Use the quietest acquired driver lock for the full summed graph."""

        outcomes = [
            outcome
            for outcome in self._outcomes.values()
            if outcome.ramp.locked_main_volume_db is not None
        ]
        if not outcomes:
            return False
        safest = min(
            outcomes,
            key=lambda outcome: float(outcome.ramp.locked_main_volume_db or 0.0),
        )
        return await self._acquire_sweep_volume(
            safest, get_main_volume_db, set_main_volume_db
        )

    async def _acquire_sweep_volume(
        self,
        outcome: Any,
        get_main_volume_db: Any,
        set_main_volume_db: Any,
    ) -> bool:
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
            ):
                return False
            entry = await get_main_volume_db()
            if (
                isinstance(entry, bool)
                or not isinstance(entry, (int, float))
                or not math.isfinite(float(entry))
            ):
                return False
            # Record the restore target before the side effect. If CamillaDSP
            # applies the volume but its response is lost, the caller's finally
            # block still has a valid lease to restore.
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

    async def restore_sweep_volume(self, set_main_volume_db: Any) -> bool:
        """Restore the volume observed immediately before this sweep."""

        async with self._restore_lock:
            entry = self._sweep_entry_volume_db
            if entry is None:
                return False
            try:
                applied = await set_main_volume_db(entry)
            except (OSError, RuntimeError, ValueError):
                log_event(
                    logger,
                    "correction.crossover_sweep_volume_restore_failed",
                    level=logging.ERROR,
                    exc_info=True,
                    to_db=f"{entry:.1f}",
                )
                return False
            if applied is False:
                log_event(
                    logger,
                    "correction.crossover_sweep_volume_restore_failed",
                    level=logging.ERROR,
                    to_db=f"{entry:.1f}",
                )
                return False
            self._sweep_entry_volume_db = None
            log_event(
                logger,
                "correction.crossover_sweep_volume_restored",
                to_db=f"{entry:.1f}",
            )
            return True

    @property
    def sweep_volume_active(self) -> bool:
        return self._sweep_entry_volume_db is not None

    async def emergency_lower_sweep_volume(self, set_main_volume_db: Any) -> bool:
        """Fail-safe fallback when the exact pre-sweep volume cannot be restored."""

        async with self._restore_lock:
            if self._sweep_entry_volume_db is None:
                return False
            try:
                applied = await set_main_volume_db(EMERGENCY_SWEEP_VOLUME_DB)
            except (OSError, RuntimeError, ValueError):
                applied = False
            if applied is False:
                log_event(
                    logger,
                    "correction.crossover_sweep_emergency_volume_failed",
                    level=logging.CRITICAL,
                    to_db=f"{EMERGENCY_SWEEP_VOLUME_DB:.1f}",
                )
                return False
            self._sweep_entry_volume_db = None
            log_event(
                logger,
                "correction.crossover_sweep_emergency_volume_applied",
                level=logging.ERROR,
                to_db=f"{EMERGENCY_SWEEP_VOLUME_DB:.1f}",
            )
            return True

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
        def public_result(value: Any) -> dict[str, Any]:
            if not isinstance(value, Mapping):
                return {}
            return {
                key: value.get(key)
                for key in (
                    "attempt",
                    "accepted",
                    "reject_reason",
                    "failure_type",
                    "estimated_snr_db",
                    "snr_verdict",
                    "worst_band_id",
                    "snr_shortfall_db",
                    "clipping",
                    "above_validity_floor",
                    "validity_floor_hz",
                    "phase",
                )
                if value.get(key) is not None
            }

        def public_entry(value: Any) -> dict[str, Any]:
            if not isinstance(value, Mapping):
                return {}
            return {
                "target_id": value.get("target_id"),
                "target_fingerprint": value.get("target_fingerprint"),
                "attempts": value.get("attempts"),
                "status": value.get("status"),
                # Boolean state is enough for orphan detection; the unguessable
                # completion token and process owner never belong in /status.
                "inflight": bool(value.get("inflight")),
                "results": [
                    public_result(item) for item in value.get("results") or ()
                ],
                "reason": value.get("reason"),
                "updated_at": value.get("updated_at"),
            }

        with self._repeat_lock:
            raw_targets = payload.get("targets") or {}
            raw_targets = raw_targets if isinstance(raw_targets, Mapping) else {}
            public_targets = {
                str(target_id): public_entry(entry)
                for target_id, entry in raw_targets.items()
                if isinstance(entry, Mapping)
            }
            comparison = payload.get("comparison")
            self._durable_repeat_progress = {
                "schema_version": payload.get("schema_version"),
                "kind": payload.get("kind"),
                "status": payload.get("status"),
                "comparison": (
                    {
                        "comparison_set_id": comparison.get("comparison_set_id"),
                        "fingerprint": comparison.get("fingerprint"),
                    }
                    if isinstance(comparison, Mapping)
                    else None
                ),
                "targets": public_targets,
                "updated_at": payload.get("updated_at"),
            }
            raw_failures = payload.get("failures") or {}
            raw_failures = (
                raw_failures if isinstance(raw_failures, Mapping) else {}
            )
            failures = {
                str(target_id): public_entry(entry)
                for target_id, entry in raw_failures.items()
                if isinstance(entry, Mapping)
            }
            for target_id, entry in public_targets.items():
                if isinstance(entry, Mapping) and entry.get("status") in {
                    "aborted", "refused"
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
                results = [
                    result
                    for result in entry.get("results") or ()
                    if isinstance(result, Mapping)
                ]
                attempts = int(entry.get("attempts") or 0)
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
        return {
            "running": self._running is not None,
            "locks": self.level_lock_store.snapshot(),
            "last": self._last.snapshot() if self._last is not None else None,
            "context_id": self.context_id,
            "valid": context_valid,
            "targets": list(self._targets.values()),
            "driver_level_locks": locks,
            "missing_targets": missing,
            "next_target": self._targets.get(missing[0]) if missing else None,
            "ready": bool(self._targets) and not missing and context_valid,
            "repeats": self.repeat_snapshot(),
        }


_LEVEL_LEASE = CrossoverLevelLease()


def level_lease() -> CrossoverLevelLease:
    return _LEVEL_LEASE


def status_payload() -> dict[str, Any]:
    """Return active-crossover targets and saved measurement evidence."""

    payload = web_measurement.status_payload()
    payload["commission"] = web_commissioning.commission_status_payload()
    # Layer-A gate: only active (`active_2_way` / `active_3_way`) speakers have
    # driver/summed targets; a `full_range_passive` speaker has none, so
    # `active=False` is the honest "this speaker has no crossover to tune" flag
    # FOR the envelope-driven page to consume when it lands (revision plan §1 —
    # today `/crossover/envelope` gates on the same derivation; the shipped
    # tab/JS do not read it yet). Derived from the already-computed targets —
    # no extra topology read. Pinned by tests/test_web_correction_crossover_flow.py.
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
    # Level evidence is tied to the immutable profile that is actually loaded,
    # not the mutable next-design candidate. Capturing the first driver updates
    # candidate evidence and must not invalidate the safe active graph or its
    # near-field gain reference.
    setup_profile = payload["setup"].get("protected_profile")
    current_context_id = (
        str(setup_profile.get("source_fingerprint") or "") or None
        if isinstance(setup_profile, Mapping)
        else None
    )
    from jasper.active_speaker import repeat_admission

    comparison_set = (payload.get("measurements") or {}).get(
        "active_comparison_set"
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
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Atomically apply an explicitly manual or automatic Layer-A profile."""
    if tuning_owner not in {"manual", "automatic"}:
        raise ValueError("tuning_owner must be 'manual' or 'automatic'")
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
        from jasper.active_speaker.measurement import active_driver_targets

        repeat_targets = repeat_state.get("targets") or {}
        required_target_ids = {
            str(target.get("target_id") or "")
            for target in active_driver_targets(topology)
        }
        unresolved = [
            target_id
            for target_id, entry in repeat_targets.items()
            if not isinstance(entry, Mapping) or entry.get("status") != "completed"
        ]
        unresolved.extend(sorted(required_target_ids - set(repeat_targets)))
        if unresolved:
            raise ValueError(
                "crossover repeat persistence is incomplete; rerun the driver "
                "level check before automatic apply"
            )
    applied = load_applied_baseline_profile_state()
    legacy_manual_profile = (
        applied
        if tuning_owner == "manual"
        and isinstance(applied, Mapping)
        and not isinstance(applied.get("recomposition_snapshot"), Mapping)
        else None
    )
    cam = camilla_factory()
    try:
        payload = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
            get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
            tuning_owner=tuning_owner,
            preserved_applied_profile=legacy_manual_profile,
        )
    finally:
        await _LEVEL_LEASE.restore_level_match_volume(
            lambda db: cam.set_volume_db(db, best_effort=False)
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


async def apply_measured_profile(*, camilla_factory: CamillaFactory) -> dict[str, Any]:
    """Compatibility wrapper for callers that explicitly apply measurements."""
    return await apply_profile(
        tuning_owner="automatic",
        camilla_factory=camilla_factory,
    )


async def start_driver_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Start the safe per-driver audible confirmation path."""

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
) -> dict[str, Any]:
    """Play a mic-capture sweep through an already-confirmed driver."""

    payload = await web_commissioning.play_driver_capture_sweep(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
        applied_profile=applied_profile,
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
) -> dict[str, Any]:
    """Play a mic-capture sweep through an already-tested summed path."""

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
    preset: Any = None,
    repeat_store: Any = None,
) -> dict[str, Any]:
    """Analyze one secure browser WAV and record per-driver evidence."""

    transaction = getattr(repeat_store, "repeat_transaction", None)
    if callable(transaction):
        with transaction():
            payload = web_measurement.record_driver_capture(
                raw,
                wav_bytes,
                placement_proof=placement_proof,
                preset=preset,
                repeat_store=repeat_store,
            )
    else:
        payload = web_measurement.record_driver_capture(
            raw,
            wav_bytes,
            placement_proof=placement_proof,
            preset=preset,
            repeat_store=repeat_store,
        )
    log_event(
        logger,
        "correction.crossover_driver_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=raw.get("speaker_group_id"),
        role=raw.get("role"),
        placement_policy=(placement_proof or {}).get("policy_id"),
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
