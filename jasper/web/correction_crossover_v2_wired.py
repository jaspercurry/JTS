# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wired microphone binding and the daemon host of the shared plan executor."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginRefused,
    CaptureStopped,
)
from jasper.active_speaker.crossover_v2.program_transaction import (
    StimulusCaptureError as StimulusCaptureError,
)
from jasper.audio_measurement.wired_capture import (
    WiredCaptureAnswer as WiredCaptureAnswer,
    WiredMicDevice,
    require_wired_mic,
)
from jasper.active_speaker.crossover_v2.wired_stimulus import (
    WiredStimulusCapture as WiredStimulusCapture,
)
from jasper.log_event import log_event
from jasper.active_speaker import plan_run
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused, REASON_REGISTRY
from jasper.active_speaker.crossover_v2.journey import PHASE_VERIFY, PHASE_CLOUD_VERIFY
from jasper.web._common import refusal_envelope

logger = logging.getLogger(__name__)


def resolve_v2_wired_mic(
    *,
    proc_asound: str | os.PathLike[str] = "/proc/asound",
) -> WiredMicDevice:
    """The measurement mic this session records on, resolved at admission.

    ``require_wired_mic`` owns the probe and the disclosure
    (:class:`~jasper.audio_measurement.wired_capture.WiredMicMissing`); this
    adds the flow's own selection event.
    """
    device = require_wired_mic(proc_asound=proc_asound)
    log_event(
        logger,
        "correction.crossover_v2_wired_selected",
        card=device.card_id,
        usb_id=device.usb_id,
        model=device.model_key,
    )
    return device


@dataclass(frozen=True)
class WiredCaptureSession:
    """One wired session's identity + plan — the ``pi_session`` stand-in.

    Carries exactly what the shared hosting reads off a
    ``PiCaptureSession``: the provider-minted ``session_id`` (the seam's
    identity rule) and the validated ``spec`` whose ``capture_plan`` the walk
    follows and whose ``sample_rate_hz`` (pinned to 48 kHz by
    ``CaptureSpec.validate``) the recorder captures at.
    """

    session_id: str
    spec: Any
    device: WiredMicDevice


@dataclass(frozen=True)
class WiredOpened:
    """The mint result the shared capture slot is handed."""

    pi_session: WiredCaptureSession


def open_wired_capture(spec: Any, *, device: WiredMicDevice) -> WiredOpened:
    """Mint the wired session: validate the spec, mint the identity.

    ``spec.validate()`` is what pins the 48 kHz rate for the capture path.
    The session exists the moment this returns.
    """
    validated = spec.validate()
    session = WiredCaptureSession(
        session_id=f"wired-{secrets.token_urlsafe(16)}",
        spec=validated,
        device=device,
    )
    log_event(
        logger,
        "correction.crossover_v2_wired_open",
        session_id=session.session_id,
        card=device.card_id,
        model=device.model_key,
    )
    return WiredOpened(pi_session=session)


def build_v2_wired_run_and_consume(
    conductor: Any, *, volume: Any, stop_event: threading.Event, stop_lock: Any,
    ceiling_s: float, complete_event: threading.Event, retake_event: threading.Event,
    tuning: Any, manifest: Any, request: Any, captures: Any, analyze: Any, assessor: Any,
    candidate_scopes: Mapping[str, str], spl_monitor: str,
    position_gate: Any = None, evidence_refs: Mapping[str, Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[[Any], Awaitable[Any]]:
    async def run(pi_session: Any) -> None:
        from jasper.web import correction_crossover_v2 as host  # lazy: host binds runner

        session_id = pi_session.session_id
        deadline = monotonic() + ceiling_s
        signals = plan_run.RunSignals(retake_event, complete_event, stop_event)

        def admit(index: int, attempt: int, entry: Any, ledger: Any) -> None:
            if stop_event.is_set():
                raise CaptureStopped("capture stopped")
            if monotonic() > deadline:
                raise CaptureBeginRefused("session_ceiling_expired", "The run exceeded its time limit")
            conductor.authorize_begin(index, attempt, entry, executor_ledger=ledger)

        async def measure(spec: Any) -> Any:
            measured: list[Any] = []
            async def body() -> None:
                measured.append(await tuning.measure(spec))
            await host._play_under_session_pause(body)
            if spec.program_phase in {PHASE_VERIFY, PHASE_CLOUD_VERIFY}:
                await tuning.restore_graph()
            return measured[0]

        def publish_failure(exc: BaseException) -> str:
            envelope = refusal_envelope(exc)
            code = envelope["code"] or "internal_error"
            if isinstance(exc, (asyncio.CancelledError, CaptureStopped)):
                code = "user_stopped"
            envelope = refusal_envelope(code=code)
            if position_gate is not None:
                position_gate.abandon_hold()
                position_gate.publish({"status": "failed", "fault": code,
                                       "next_action": envelope["next_action"]})
            return str(code)

        try:
            opened = await volume.open()
            if opened is not None and str(getattr(opened, "value", opened)) != "opened":
                raise CrossoverV2Refused("The measurement volume did not open", code="measurement_volume_drift")
            result = await plan_run.run_plan(
                request, session=tuning, manifest=manifest, analyze=analyze, assessor=assessor, measure=measure,
                gate=position_gate, candidate_scopes=candidate_scopes, captures=captures,
                signals=signals, admit=admit, aborts={CaptureStopped: "user_stopped"},
                spl_monitor=spl_monitor, gain_ceiling_db=conductor._measure_gain_ceiling_db,
            )
            if result.reason and result.reason != "complete_requested":
                if result.reason == "user_stopped" or result.cancelled:
                    raise CaptureStopped("capture stopped")
                raise CrossoverV2Refused(result.detail, code=result.reason if result.reason in REASON_REGISTRY else "internal_error")
        except BaseException as exc:  # noqa: BLE001 - drain the held resources on every exit
            try:
                code = publish_failure(exc)
                host._persist_terminal_failure(conductor, code)
            finally:
                await _abandon_best_effort(session_id, volume)
            raise
        else:
            try:
                try:
                    host.persist_conductor_state(conductor, failure_code=None, evidence=evidence_refs)
                finally:
                    await _drain_volume(session_id, volume.close, "volume_close")
            except BaseException as exc:  # noqa: BLE001 - publish faults after the drain too
                publish_failure(exc)
                raise

    return run


async def _abandon_best_effort(session_id: str, volume: Any) -> None:
    await _drain_volume(session_id, volume.abandon, "volume_abandon")


async def _drain_volume(session_id: str, operation: Any, component: str) -> None:
    from jasper.web import correction_crossover_v2 as _host  # lazy: host binds this runner
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumePlanError, SessionVolumeRestoreResult,
    )

    status = "failed"
    try:
        result = await operation()
        status = str(getattr(result, "value", result)) if result is not None else "unknown"
        if result == SessionVolumeRestoreResult.FAILED:
            raise SessionVolumePlanError("session volume restore did not confirm")
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger, f"correction.crossover_v2_{component}_failed",
            level=logging.CRITICAL, session_id=session_id, component=component,
            error_type=type(exc).__name__,
        )
        raise
    finally:
        _host._persist_execution_result(session_id, volume_restore=status)
    log_event(
        logger, "correction.crossover_v2_volume_cleanup",
        session_id=session_id, component=component, outcome=status,
    )
