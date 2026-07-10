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
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.active_speaker import web_commissioning, web_measurement
from jasper.log_event import log_event

logger = logging.getLogger(__name__)
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
        self._restore_lock = asyncio.Lock()
        self.context_id: str | None = None
        self.noise_floor_db = None
        self.mic_calibration = None
        self.input_device = None

    async def run_level_match(self, geometry: str, **ports: Any) -> Any:
        from jasper.correction.level_match import LevelMatchSession

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
        )
        self._running = run
        try:
            outcome = await run.run_for_geometry(geometry, **ports)
        finally:
            if self._running is run:
                self._running = None
        self._last = outcome
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

    async def restore_level_match_volume(self, set_main_volume_db: Any) -> bool:
        from jasper.audio_measurement.ramp import RampState

        async with self._restore_lock:
            outcome = self._last
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
            log_event(
                logger,
                "correction.crossover_level_volume_restored",
                to_db=f"{ramp.original_main_volume_db:.1f}",
            )
            return True

    async def ensure_level_match_volume(self, set_main_volume_db: Any) -> bool:
        """Reassert the acquired gain immediately before each driver sweep."""
        from jasper.audio_measurement.ramp import RampState

        async with self._restore_lock:
            outcome = self._last
            if outcome is None or outcome.ramp.state is not RampState.LOCKED:
                return False
            ramp = outcome.ramp
            if ramp.locked_main_volume_db is None:
                return False
            if ramp.restored is not True:
                return True
            applied = await set_main_volume_db(float(ramp.locked_main_volume_db))
            if applied is False:
                log_event(
                    logger,
                    "correction.crossover_level_volume_reassert_failed",
                    level=logging.ERROR,
                    to_db=f"{ramp.locked_main_volume_db:.1f}",
                )
                return False
            ramp.restored = False
            log_event(
                logger,
                "correction.crossover_level_volume_reasserted",
                to_db=f"{ramp.locked_main_volume_db:.1f}",
            )
            return True

    def level_match_snapshot(
        self, *, current_context_id: str | None = None
    ) -> dict[str, Any]:
        context_valid = (
            current_context_id is None
            or self.context_id == current_context_id
        )
        return {
            "running": self._running is not None,
            "locks": self.level_lock_store.snapshot(),
            "last": self._last.snapshot() if self._last is not None else None,
            "context_id": self.context_id,
            "valid": context_valid,
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
) -> dict[str, Any]:
    """Play a mic-capture sweep through an already-confirmed driver."""

    payload = await web_commissioning.play_driver_capture_sweep(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
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


def record_driver_capture(raw: Mapping[str, Any], wav_bytes: bytes) -> dict[str, Any]:
    """Analyze one secure browser WAV and record per-driver evidence."""

    payload = web_measurement.record_driver_capture(raw, wav_bytes)
    log_event(
        logger,
        "correction.crossover_driver_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=raw.get("speaker_group_id"),
        role=raw.get("role"),
    )
    return payload


def record_summed_capture(raw: Mapping[str, Any], wav_bytes: bytes) -> dict[str, Any]:
    """Analyze one secure browser WAV and record summed-crossover evidence."""

    payload = web_measurement.record_summed_capture(raw, wav_bytes)
    log_event(
        logger,
        "correction.crossover_summed_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=raw.get("speaker_group_id"),
        verdict=payload.get("verdict"),
    )
    return payload
