# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover flow's WIRED capture provider (#2662 W2b).

The capture-source seam (decision 13,
:mod:`jasper.active_speaker.crossover_v2.capture_source`) says the conductor
asks for a capture of program X at position Y and a provider answers with WAV
plus metadata. This module is the Pi-attached measurement-mic provider, and
the only one: the Pi plays AND records on one host. What that buys:

* **Mic resolution** (:func:`resolve_v2_wired_mic`): wired is THE
  acoustic-measurement path (ADR-0188), found from a registry-anchored usbid
  match (probe-at-use —
  :func:`jasper.audio_measurement.wired_capture.require_wired_mic`, which
  also owns the shared no-mic disclosure); this adds the selection event.
* **The session identity** (:func:`open_wired_capture`): the provider mints
  ``wired-<token>`` and the host keys durable state, evidence publishers and
  phase artifacts by it, per the seam's ownership rule (the bundle id stays
  the canonical attribution identity; this id rides the existing alias).
* **The plan walk** (:func:`build_v2_wired_run_and_consume`): the conductor
  conversation ``run_capture_plan`` carries — authorize (position gate first,
  admission second) → the host plays program X while the local recorder is
  already confirmed live → the answer lands in ``consume_capture`` — driven
  locally on a worker thread. Deferred begins are a local retry loop against
  the same :class:`PositionGate`; the held-set completion signal (work order
  D1) is a local ``threading.Event`` the host's ``request_complete`` seam
  sets.
* **The answer**: minted by the ONE kernel
  (:func:`jasper.audio_measurement.wired_capture.mint_wired_answer`), which
  every wired take in the product shares — so the device block, the
  calibration reference and the integrity counters cannot differ by which
  door recorded. This module supplies only host vocabulary: the mic, the
  session, and the walk that asks for each take.

What is NOT here, on purpose: durable-state writes, the persisted failure
codes, the session-volume policy, admission, and the position gate are the
host's (:mod:`jasper.web.correction_crossover_v2`), reached late-bound so a
test double patched there is honored from this side of the seam. Failure
mapping stays in the flow's own reason vocabulary: the walk outliving its
wall-clock ceiling persists ``session_ceiling_expired``, and a capture-chain
fault persists through the host's one program-failure classifier
(``internal_error`` when unclassified, with the real cause on the journal).

A voluntary RETAKE is initiated locally, through the host's
``request_retake`` seam (``POST /crossover/v2/retake``), on the §2.6 terms
stated once where they are implemented
(:func:`build_v2_wired_run_and_consume`), rather than a second time here.

Deferred to W3 (stated, not implied): the wizard UI for wired sessions —
including the retake's own affordance, which today is that bare POST — and a
named household-facing reason for a mid-session mic loss (today that is
``internal_error`` copy with the specific cause in the journal).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
    CaptureFailed,
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

if TYPE_CHECKING:
    from jasper.web.correction_crossover_v2 import PositionGate, V2VolumeHooks

logger = logging.getLogger(__name__)

#: How often a held begin retries the position gate. The phone re-posts its
#: deferred ``begin_capture`` every 1.5 s (capture-page wait screen); the
#: local loop keeps that cadence so gate logging and driver pacing see the
#: same rhythm the remote tier was built against.
WIRED_HOLD_POLL_S = 1.5

#: Settle before auto-retrying a REJECTED capture on a gateless session (the
#: single-position shapes — a gated session re-gates, so its settle is the
#: driver's own re-confirm). Mirrors the phone's cancelable auto-advance
#: countdown giving the room a beat between takes; a budget allowance, not a
#: measurement.
WIRED_RETRY_SETTLE_S = 3.0

class _RetakeRequested(Exception):
    """How the hold loop tells the walk to abandon a begin nobody has released
    and re-open the previous slot instead.

    Private to one walk, and deliberately NOT one of the flow's reasons:
    nothing failed, and no household ever reads it.
    """


def resolve_v2_wired_mic(
    *,
    proc_asound: str | os.PathLike[str] = "/proc/asound",
) -> WiredMicDevice:
    """The measurement mic this session records on, resolved at prepare.

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
    conductor: Any,
    *,
    volume: "V2VolumeHooks",
    stop_event: threading.Event,
    stop_lock: Any,
    ceiling_s: float,
    complete_event: threading.Event,
    retake_event: threading.Event | None = None,
    position_gate: "PositionGate | None" = None,
    evidence_refs: Mapping[str, Any] | None = None,
    poll_interval_s: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    capture_stimulus: Callable[[int, int, Any], Any] | None = None,
) -> Callable[[Any], Awaitable[Any]]:
    """Run held positions through the shared take executor; persist each verdict."""

    poll_s = WIRED_HOLD_POLL_S if poll_interval_s is None else float(poll_interval_s)

    async def _run_and_consume(pi_session: Any) -> None:
        from jasper.active_speaker.crossover_v2.journey import PHASE_DONE
        from jasper.active_speaker.crossover_v2.refusal_copy import (
            REASON_INTERNAL_ERROR,
            REASON_REGISTRY,
            REASON_SESSION_CEILING_EXPIRED,
        )
        from jasper.active_speaker.crossover_v2.session_graph import (
            SessionGraphError,
        )
        from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
        from jasper.measurement_window import MeasurementWindowError

        # The host's side of the seam, late-bound on purpose (#2662): the
        # persisted codes, the program-failure classifier, the local-seam
        # error type and the eager-fit starter are host policy, and resolving
        # them at call time keeps the host module their single point of truth
        # — a test double patched there is honored here.
        from jasper.web import correction_crossover_v2 as _host

        plan = getattr(pi_session.spec, "capture_plan", None)
        if plan is None:
            raise CaptureFailed("a wired session requires a capture_plan spec")
        session_id = str(pi_session.session_id)
        if capture_stimulus is None:
            raise CaptureFailed("the shared take executor is not bound")

        def _raise_if_stopped() -> None:
            with stop_lock:
                if stop_event.is_set():
                    raise CaptureStopped("capture stopped")

        def _retake_wanted() -> bool:
            """One take-and-clear of the household's retake signal.

            A level-triggered :class:`threading.Event` rather than a queue,
            deliberately: two taps before the walk next looks are ONE retake,
            which is what a household means by them. Cleared here so the same
            ask can never serve two slots.

            **An ask that arrives while a capture is IN FLIGHT is served at the
            next hold, and by then "the slot that just completed" may not be
            the capture they were watching.** If that in-flight capture is
            REJECTED, ``accepted`` never advanced, so the retake re-opens the
            slot accepted BEFORE it. That is faithful to the rule this
            mirrors — a retake names ``accepted_count``, and a rejected capture
            does not change it — and it is also not obviously what a person
            tapping mid-capture meant. It is written down rather than guessed
            at: the affordance that decides whether the ask is even offered
            mid-capture is the wizard's, and belongs to that ticket rather than
            to a second interpretation of the count here.
            """
            if retake_event is None or not retake_event.is_set():
                return False
            retake_event.clear()
            return True

        def _authorize(index: int, attempt: int, entry: Any, deadline: float) -> None:
            held_logged = False
            while True:
                _raise_if_stopped()
                try:
                    # The position gate AHEAD of the conductor, the same
                    # ordering: a hold is not an admission decision, and the
                    # gate's own budgets (per-hold + session ceiling) bound
                    # this loop exactly as they bound the phone's re-posts.
                    if position_gate is not None:
                        position_gate.gate(index, attempt, entry)
                    conductor.authorize_begin(index, attempt, entry)
                    return
                except CaptureBeginDeferred:
                    # A deferral from the CONDUCTOR (the retained-but-unreached
                    # VERIFY hold — D10) has no gate bounding it, so the loop
                    # carries the session ceiling itself: every wait in this
                    # runner is bounded, and the code that expires it is the
                    # honest cumulative clock, never a transport claim.
                    if monotonic() > deadline:
                        raise CaptureBeginRefused(
                            REASON_SESSION_CEILING_EXPIRED,
                            "The measurement ran out of time while a capture "
                            "was still being held back.",
                        ) from None
                    if not held_logged:
                        held_logged = True
                        log_event(
                            logger,
                            "correction.crossover_v2_wired_hold",
                            session_id=session_id,
                            index=index,
                            attempt=attempt,
                        )
                    if stop_event.wait(poll_s):
                        raise CaptureStopped("capture stopped") from None
                    # A HELD begin is exactly the retake window: the
                    # previous slot is accepted and this one has not started,
                    # so replacing the previous take is still meaningful. The
                    # walk decides what to do about it — this loop only stops
                    # waiting for a release nobody is coming to give.
                    if _retake_wanted():
                        # Say so before leaving: a hold nobody is running must
                        # stop being the position the envelope advertises, or
                        # the operator is sent to the wrong spot and the
                        # retake's own target is never published.
                        if position_gate is not None:
                            position_gate.abandon_hold()
                        raise _RetakeRequested from None

        def _capture_one(index: int, attempt: int, entry: Any) -> Mapping[str, Any]:
            try:
                verdict = capture_stimulus(index, attempt, entry)
            except OSError as exc:
                raise _host.CrossoverV2LocalSeamError(str(exc)) from exc
            if not isinstance(verdict, Mapping) or "accepted" not in verdict:
                raise CaptureFailed("the shared take executor returned no capture verdict")
            code = verdict.get("code") if isinstance(verdict, Mapping) else None
            _host.persist_conductor_state(
                conductor,
                failure_code=code if not verdict.get("accepted") else None,
                evidence=evidence_refs,
            )
            # The eager-fit trigger
            # (owner UX direction 2026-07-30).
            if verdict.get("awaiting_confirm"):
                _host._start_speculative_group_close(conductor)
            return verdict

        def _walk() -> None:
            deadline = monotonic() + float(ceiling_s)
            target = int(plan.capture_target)
            max_attempts = int(plan.max_attempts)
            accepted = 0
            attempt = 0

            def _serve_retake(
                accepted: int, attempt: int, deadline: float,
            ) -> tuple[int, bool]:
                """Re-capture the just-accepted slot — the terms are stated once,
                in this function's own runner docstring above.

                Returns ``(attempt, end_walk)``: the attempt counter after this,
                and whether the walk must stop. The three refusals below are the
                only policy here; everything else is the ordinary path.
                """
                if accepted < 1:
                    # Nothing has been accepted yet, so there is no take to
                    # replace — the ask reached a walk that had not measured
                    # anything. Dropped with a name rather than re-pointed at
                    # the capture about to run, which is a DIFFERENT spot.
                    log_event(
                        logger,
                        "correction.crossover_v2_wired_retake_refused",
                        level=logging.WARNING,
                        session_id=session_id,
                        reason="no_take_to_replace",
                    )
                    return attempt, False
                if attempt >= max_attempts:
                    # The plan's own budget, the only one a retake spends —
                    # there is no second budget to reason about. Refusing here
                    # keeps the set the household already has; the walk stays
                    # where it was.
                    log_event(
                        logger,
                        "correction.crossover_v2_wired_retake_refused",
                        level=logging.WARNING,
                        session_id=session_id,
                        reason="plan_attempts_spent",
                        index=accepted,
                        attempts=attempt,
                        max_attempts=max_attempts,
                    )
                    return attempt, False
                index = accepted
                attempt += 1
                entry = plan.entry_for_index(index)
                log_event(
                    logger,
                    "correction.crossover_v2_wired_retake",
                    session_id=session_id,
                    index=index,
                    attempt=attempt,
                )
                while True:
                    try:
                        _authorize(index, attempt, entry, deadline)
                        break
                    except _RetakeRequested:
                        # Asked AGAIN while this retake's own begin was held.
                        # It can only name the slot already being re-opened, so
                        # it is the same ask arriving twice and the honest
                        # answer is to keep waiting for the release. Swallowed
                        # rather than propagated: this function is reached from
                        # INSIDE the walk's own handler for that exception, so
                        # letting it escape would leave nothing to catch it and
                        # would end a healthy session on ``internal_error``.
                        # Bounded by the gate's per-hold and ceiling budgets
                        # exactly as the first wait is.
                        continue
                    except CaptureBeginRefused as refusal:
                        # A refused RETAKE must not end a session that already
                        # holds a usable take for this slot: the household
                        # asked for a bonus, not for the set to be torn down,
                        # and the per-slot extras ledger running out is the
                        # ordinary way here. The two clock deaths lose nothing
                        # by being swallowed — the walk's own deadline checks
                        # re-decide them next pass, same ceiling, same code.
                        log_event(
                            logger,
                            "correction.crossover_v2_wired_retake_refused",
                            level=logging.WARNING,
                            session_id=session_id,
                            reason="begin_refused",
                            index=index,
                            attempt=attempt,
                            code=str(getattr(refusal, "code", "") or ""),
                        )
                        return attempt, False
                verdict = _capture_one(index, attempt, entry)
                return attempt, verdict.get("terminal") is True

            while accepted < target:
                if attempt >= max_attempts:
                    # Attempt budget spent: mirror ``run_capture_plan``'s
                    # non-raising end — the post-walk shared code persists the
                    # conductor's own last failure and abandons the volume.
                    log_event(
                        logger,
                        "correction.crossover_v2_wired_exhausted",
                        level=logging.WARNING,
                        session_id=session_id,
                        accepted=accepted,
                        target=target,
                        attempts=attempt,
                    )
                    return
                # The wire index space, unchanged: 1-based, the next slot is
                # ``accepted + 1`` (``_poll_capture_plan``'s own arithmetic),
                # and the 1-based→0-based entry lookup is the plan's canonical
                # ``entry_for_index`` so this walk never respells it.
                index = accepted + 1
                attempt += 1
                entry = plan.entry_for_index(index)
                try:
                    _authorize(index, attempt, entry, deadline)
                except _RetakeRequested:
                    # This begin was HELD and never admitted, so the attempt
                    # number it claimed was never spent — the walk hands it
                    # back rather than charging two attempts for one retake.
                    # Re-using the pair later is safe precisely BECAUSE the
                    # begin was still held: the gate keys its releases on
                    # (index, attempt), and this one was never released.
                    attempt -= 1
                    attempt, end_walk = _serve_retake(accepted, attempt, deadline)
                    if end_walk:
                        return
                    continue
                verdict = _capture_one(index, attempt, entry)
                if verdict.get("accepted"):
                    accepted += 1
                if verdict.get("terminal") is True:
                    # The host decided no later capture can make the set
                    # usable, so end immediately.
                    return
                if not verdict.get("accepted") and position_gate is None:
                    # Gateless auto-retry gets a settle beat (module
                    # docstring); a gated retry re-gates, which is its settle.
                    if stop_event.wait(WIRED_RETRY_SETTLE_S):
                        raise CaptureStopped("capture stopped")
            # Target met. The held-set window (D1): while the host holds the
            # pre-apply group open, wait for the household's explicit
            # completion signal, bounded by the session's own wall-clock
            # ceiling — the honest cumulative clock, whose registered code
            # says exactly what ran out.
            while conductor.cloud_measure_group_awaiting_confirm():
                _raise_if_stopped()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    log_event(
                        logger,
                        "correction.crossover_v2_wired_confirm_expired",
                        level=logging.WARNING,
                        session_id=session_id,
                        ceiling_s=float(ceiling_s),
                    )
                    raise CaptureBeginRefused(
                        REASON_SESSION_CEILING_EXPIRED,
                        "The measurement ran out of time waiting for the "
                        "all-spots-measured confirmation.",
                    )
                if complete_event.wait(min(poll_s, remaining)):
                    complete_event.clear()
                    # The host's group-close seam — fit + persist. A refusal
                    # raised under it propagates like any admission refusal;
                    # `drive_group_close` says why PR-L4 no longer makes one.
                    _host.drive_group_close(conductor, evidence=evidence_refs)
                    continue
                # The set is held open, which is exactly when
                # the just-accepted slot is still retakeable. Asked AFTER the
                # completion wait above so a household that said "done" is
                # never asked to say it twice.
                if _retake_wanted():
                    attempt, end_walk = _serve_retake(accepted, attempt, deadline)
                    if end_walk:
                        return

        try:
            opened = await volume.open()
        except (
            SessionVolumePlanError, MeasurementWindowError, SessionGraphError,
        ) as exc:
            log_event(
                logger,
                "correction.crossover_v2_volume_open_failed",
                level=logging.WARNING,
                reason=type(exc).__name__,
            )
            raise CaptureFailed(
                "the measurement volume could not be opened"
            ) from exc
        opened_value = getattr(opened, "value", opened)
        if opened is not None and str(opened_value) != "opened":
            raise CaptureFailed(
                "the fixed measurement volume could not be confirmed"
            )

        walk_task = asyncio.create_task(asyncio.to_thread(_walk))
        try:
            try:
                await asyncio.shield(walk_task)
            except asyncio.CancelledError:
                # Stop drains the walk before cleanup: the
                # worker owns a live recorder and a DSP graph load, so
                # cleanup must not race it.
                stop_event.set()
                while not walk_task.done():
                    try:
                        await asyncio.shield(walk_task)
                    except asyncio.CancelledError:
                        continue
                    except (OSError, RuntimeError, ValueError):
                        break
                if walk_task.done() and not walk_task.cancelled():
                    walk_task.exception()
                await _abandon_best_effort(session_id, volume)
                raise
        except CaptureStopped:
            await _abandon_best_effort(session_id, volume)
            raise
        except CaptureBeginRefused as refusal:
            # THE REFUSAL'S OWN registered code wins (gate fix round S1): the
            # exception that ended the session is the freshest fact, while
            # ``last_failure_code`` is whatever the LAST REJECTED CAPTURE
            # stamped — so the opposite precedence would let a prior
            # rejection's code shadow a later ceiling expiry (a
            # capture-quality claim persisted over a clock that ran out).
            # The conductor's stamp is the fallback for refusals that carry
            # no registered code of their own (the admission arms raise with
            # a rendered MESSAGE and stamp the code separately).
            # REASON_CAPTURE_TIMEOUT fallback is deliberately NOT mirrored —
            # and the runner's own
            # inverted precedence is flagged (PR body) but out of scope: its
            # gate refusals reach a different arm shape via the phone.
            refusal_code = str(getattr(refusal, "code", "") or "")
            if refusal_code not in REASON_REGISTRY:
                refusal_code = ""
            code = refusal_code or conductor.last_failure_code or REASON_INTERNAL_ERROR
            # THE DRAIN IS IN A ``finally`` IN EVERY CLEANUP ARM OF THIS FILE:
            # the persist ends in ``save_v2_state`` -> ``atomic_write_text``, so
            # disk pressure (ENOSPC, EROFS) raises OSError out of it. The
            # SESSION_MEASUREMENT claim it runs ahead of has NO TTL, and all
            # three out-of-runner drains gate on ``VolumeOwner.holds_kind`` — so
            # an abandon skipped by a raising persist leaks the claim and wedges
            # the measurement pause until the process restarts. Recording the
            # failure must never cost the household its fader.
            try:
                _host._persist_terminal_failure(conductor, code)
            finally:
                await _abandon_best_effort(session_id, volume)
            raise
        except Exception as exc:  # noqa: BLE001 — cleanup-and-reraise
            # The catch-all cleanup arm (W6.1 gate ruling), minus the phone:
            # the seams raise open-endedly, and a wired capture-chain fault
            # (WiredCaptureError) lands here too — the honest persisted code
            # is the classifier's, else internal_error, with the real cause
            # on the journal.
            classified = _host.classify_program_failure(exc)
            code = classified[0] if classified else REASON_INTERNAL_ERROR
            refusals = classified[1] if classified else ()
            log_event(
                logger,
                "correction.crossover_v2_wired_failed",
                level=logging.WARNING,
                session_id=session_id,
                code=code,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            try:
                _host._persist_terminal_failure(conductor, code, refusals=refusals)
            finally:
                await _abandon_best_effort(session_id, volume)
            raise
        # Walk finished without a failure.
        done = conductor.current_phase == PHASE_DONE
        # Same guarantee as the cleanup arms above, and BOTH branches need it:
        # a raising persist would otherwise skip the close as well as the
        # abandon, and the close releases the very same claim.
        try:
            _host.persist_conductor_state(
                conductor,
                failure_code=None if done else conductor.last_failure_code,
                evidence=evidence_refs,
            )
        finally:
            if done:
                try:
                    await _drain_volume(session_id, volume.close, "volume_close")
                except (OSError, RuntimeError, ValueError) as exc:
                    log_event(
                        logger,
                        "correction.crossover_v2_volume_close_failed",
                        level=logging.CRITICAL,
                        session_id=session_id,
                        component="volume_close",
                        error_type=type(exc).__name__,
                    )
                    _host._persist_terminal_failure(conductor, REASON_INTERNAL_ERROR)
                    raise
            else:
                await _abandon_best_effort(session_id, volume)

    return _run_and_consume


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
