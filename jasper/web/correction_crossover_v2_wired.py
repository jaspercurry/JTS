# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover flow's WIRED capture provider (#2662 W2b).

The capture-source seam (decision 13,
:mod:`jasper.active_speaker.crossover_v2.capture_source`) says the conductor
asks for a capture of program X at position Y and a provider answers with WAV
plus metadata. This module is the Pi-attached measurement-mic provider: the
Pi plays AND records on one host, so everything the relay provider exists to
choreograph — sessions, encrypted upload, deferred begins over a public
Worker, host events, purge — does not exist here at all. What replaces it:

* **Source resolution** (:func:`resolve_v2_capture_source`): wired is chosen
  when a measurement-class USB capture card is present (registry-anchored
  usbid match, probe-at-use — :func:`jasper.audio_measurement.wired_capture.
  resolve_wired_mic`), with an explicit ``JASPER_CAPTURE_SOURCE`` override
  either way. Disclose-and-recommend, never nanny: the relay stays fully
  usable when chosen, and the auto pick is logged.
* **The session identity** (:func:`open_wired_capture`): the provider mints
  ``wired-<token>`` and the host keys durable state, evidence publishers and
  phase artifacts by it — the same key vocabulary as a relay session id, per
  the seam's ownership rule (the bundle id stays the canonical attribution
  identity; this id rides the existing alias).
* **The plan walk** (:func:`build_v2_wired_run_and_consume`): the same
  conductor conversation ``run_capture_plan`` carries for the relay —
  authorize (position gate first, admission second) → the host plays program
  X while the local recorder is already confirmed live → the answer lands in
  ``consume_capture`` — driven locally on a worker thread. Deferred begins
  become a local retry loop against the same :class:`PositionGate`; the
  held-set completion signal (work order D1) becomes a local
  ``threading.Event`` the host's ``request_complete`` seam sets.
* **The answer**: a :class:`WiredCaptureAnswer` carrying exactly the seam's
  four fields. Integrity counters come from the capture engine's own ALSA
  accounting plus the re-homed zero-run scan
  (:mod:`jasper.audio_measurement.wired_capture`), in the frame ledger's wire
  spelling — so ``reconcile_capture_frames`` and the analyzer's
  frame-accounting checks grade a wired take exactly as they grade a phone
  take, with the counters always REPORTED (a wired capture never passes on
  "not evaluated"). Calibration identity rides the existing household-mic
  stored-reference shape (``{"calibration": {"mode": "stored", ...}}``), so
  the session's UNCHANGED resolver — including the wrong-mic mismatch guard —
  turns it into a record; capture banks RAW audio + identity, analysis
  applies the curve.

What is NOT here, on purpose (the same boundary the relay provider states):
durable-state writes, the persisted failure codes, the session-volume
policy, admission, and the position gate are the host's
(:mod:`jasper.web.correction_crossover_v2`), reached late-bound so a test
double patched there is honored from this side of the seam. Failure mapping
stays in the flow's own reason vocabulary — a wired death never invents a
transport claim (``relay_timeout``) about a transport that does not exist:
the walk outliving its wall-clock ceiling persists
``session_ceiling_expired``, and a capture-chain fault persists through the
host's one program-failure classifier (``internal_error`` when unclassified,
with the real cause on the journal).

A voluntary RETAKE is initiated locally, through the host's
``request_retake`` seam (``POST /crossover/v2/retake``), on the relay's own
§2.6 terms. Those terms are stated once, where they are implemented
(:func:`build_v2_wired_run_and_consume`), rather than a second time here.

Deferred to W3 (stated, not implied): the wizard UI for wired sessions —
including the retake's own affordance, which today is that bare POST — and a
named household-facing reason for a mid-session mic loss (today that is
``internal_error`` copy with the specific cause in the journal).
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from jasper.active_speaker.crossover_v2.capture_source import (
    SOURCE_RELAY,
    SOURCE_WIRED,
)
from jasper.audio_measurement.wired_capture import (
    WiredCaptureError,
    WiredMicDevice,
    WiredRecorder,
    build_capture_integrity_report,
    encode_wav_s32,
    resolve_wired_mic,
    scan_zero_runs,
    select_capture_channel,
)
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.web.correction_crossover_v2 import PositionGate, V2VolumeHooks

logger = logging.getLogger(__name__)

#: The source override (also documented in ``.env.example``): empty/``auto``
#: resolves by mic presence, ``wired`` requires a measurement mic (loud error
#: otherwise), ``relay`` keeps the phone flow even with a mic plugged in.
CAPTURE_SOURCE_ENV = "JASPER_CAPTURE_SOURCE"

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

#: Post-roll recorded after the play call returns. Derivation, evidence
#: named: the composed programs already END with 0.5 s of in-program tail
#: (``program.DEFAULT_MEASURE_TAIL_S`` / ``DEFAULT_VERIFY_TAIL_S``), and the
#: play call's return leads the acoustic end by the playback chain's
#: buffered depth — the same tens-to-hundreds-of-ms, device-dependent lead
#: ``PHASE_LADDER_START_SKEW_S`` (0.35 s) documents at the start edge. 1.0 s
#: covers that lead plus decay margin beyond the composed tail. A budget
#: allowance, not a measurement — the hardware smoke is where the real
#: play-return-to-silence interval gets measured.
WIRED_POST_ROLL_S = 1.0

#: Capture-budget allowance for everything BEFORE the program's first sample:
#: re-admission, the DSP writer lock, and the program-graph load all run
#: inside ``on_armed`` while the recorder is already rolling. The play seam's
#: own transport budget (``correction_setup._run_async``'s 60 s default)
#: bounds setup + program + restore together, so 20 s of setup allowance is
#: safely above the observed graph-load cost and safely inside that bound.
WIRED_PRE_PLAY_ALLOWANCE_S = 20.0

#: Program-duration stand-in when a plan carries no entry table (an older
#: single-capture shape): the analyzer's own "legitimate capture" ceiling
#: (``deconv.DEFAULT_MAX_CAPTURE_SECONDS``), read lazily at use so the owner
#: stays the owner.
def _fallback_program_s() -> float:
    from jasper.audio_measurement.deconv import DEFAULT_MAX_CAPTURE_SECONDS

    return float(DEFAULT_MAX_CAPTURE_SECONDS)


class _RetakeRequested(Exception):
    """How the hold loop tells the walk to abandon a begin nobody has released
    and re-open the previous slot instead.

    Private to one walk, and deliberately NOT one of the flow's reasons:
    nothing failed, and no household ever reads it.
    """


def resolve_v2_capture_source(
    env: Mapping[str, str] | None = None,
    *,
    proc_asound: str | os.PathLike[str] = "/proc/asound",
) -> tuple[str, WiredMicDevice | None]:
    """Which capture source this session should open, resolved at prepare.

    Probe-at-use: presence is read fresh from ``/proc/asound`` every time —
    the mic is plugged in for a measurement, so there is no steady state for
    a reconciler to own. Auto (unset/``auto``) selects wired exactly when a
    measurement-class mic is present, and logs the pick; explicit ``wired``
    with no mic raises :class:`WiredCaptureError` loudly (never a silent
    fallback); explicit ``relay`` always keeps the phone flow; anything else
    raises rather than measuring with a source nobody chose.
    """
    source = env if env is not None else os.environ
    raw = str(source.get(CAPTURE_SOURCE_ENV) or "").strip().lower()
    if raw == SOURCE_RELAY:
        return SOURCE_RELAY, None
    if raw == SOURCE_WIRED:
        device = resolve_wired_mic(proc_asound=proc_asound)
        if device is None:
            raise WiredCaptureError(
                f"{CAPTURE_SOURCE_ENV}=wired but no measurement-class USB "
                "microphone was found — plug in a registered measurement mic "
                "(e.g. miniDSP UMIK-2), or unset the override to use the "
                "phone relay"
            )
        return SOURCE_WIRED, device
    if raw not in ("", "auto"):
        raise WiredCaptureError(
            f"unrecognized {CAPTURE_SOURCE_ENV}={raw!r} "
            "(expected wired, relay, or empty/auto)"
        )
    device = resolve_wired_mic(proc_asound=proc_asound)
    if device is None:
        return SOURCE_RELAY, None
    log_event(
        logger,
        "correction.crossover_v2_wired_selected",
        card=device.card_id,
        usb_id=device.usb_id,
        model=device.model_key,
    )
    return SOURCE_WIRED, device


@dataclass(frozen=True)
class WiredCaptureSession:
    """One wired session's identity + plan — the ``pi_session`` stand-in.

    Carries exactly what the shared hosting reads off a relay
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
    """The mint result, shaped like ``correction_adapter.RelayCapture``.

    ``tap_link`` is empty by construction: there is no phone to hand a link
    to. The shared status holder publishes it as-is; the wired wizard surface
    (W3) is what will render this state.
    """

    pi_session: WiredCaptureSession
    tap_link: str = ""


def open_wired_capture(spec: Any, *, device: WiredMicDevice) -> WiredOpened:
    """Mint the wired session: validate the spec, mint the identity.

    ``spec.validate()`` is the same gate relay registration runs — it is what
    pins the 48 kHz rate for the wired path too. No network, no relay: the
    session exists the moment this returns.
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


@dataclass(frozen=True)
class WiredCaptureAnswer:
    """The seam's :class:`CaptureAnswer`, minted by the wired source —
    exactly the contract's four fields, nothing relay-shaped."""

    wav: bytes
    device: Mapping[str, Any] | None = None
    setup: Mapping[str, Any] | None = None
    capture_integrity: Mapping[str, Any] | None = None


def _wired_setup_reference(host: Any) -> Mapping[str, Any] | None:
    """The mic/cal identity REFERENCE the answer carries (seam contract).

    The wired analog of the phone's one-tap confirm: the household's
    remembered mic hint (``default_setup_calibration_for_v2`` — the same
    resolver that feeds the phone's prefill) becomes
    ``{"calibration": {"mode": "stored", calibration_id, model}}``, which the
    session's UNCHANGED resolver materializes — including the
    wrong-mic mismatch guard against this capture's reported device. No
    resolvable household record ⇒ ``None`` ⇒ the existing
    annotated-uncalibrated path (WARN, analysis still runs). Cal identity
    comes from the household record, never from USB serial — a real UMIK-2
    reports the generic "00000".
    """
    try:
        hint = host.default_setup_calibration_for_v2()
    except (OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_wired_setup_hint_failed",
            level=logging.WARNING,
        )
        return None
    if hint is None or not getattr(hint, "resolvable", False):
        return None
    return {
        "calibration": {
            "mode": "stored",
            "calibration_id": str(hint.calibration_id),
            "model": str(hint.model),
        }
    }


def _json_safe_dbfs(values: tuple[float, ...]) -> list[float | None]:
    """Per-channel RMS for the device metadata: rounded, ``None`` for a
    silent channel (−inf is not JSON)."""
    import math

    return [
        round(value, 1) if math.isfinite(value) else None for value in values
    ]


def build_v2_wired_run_and_consume(
    conductor: Any,
    *,
    volume: "V2VolumeHooks",
    stop_event: threading.Event,
    stop_lock: Any,
    device: WiredMicDevice,
    ceiling_s: float,
    complete_event: threading.Event,
    retake_event: threading.Event | None = None,
    position_gate: "PositionGate | None" = None,
    evidence_refs: Mapping[str, Any] | None = None,
    poll_interval_s: float | None = None,
    recorder_factory: Callable[[float], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[[Any, Any], Awaitable[Any]]:
    """The async ``run_and_consume(client, pi_session)`` for one WIRED session.

    Mirrors the relay runner's thread model — the walk runs on a worker
    thread (``asyncio.to_thread``), the awaiting task shields it through
    cancellation so Stop drains the walk before cleanup — and the relay
    runner's host-owned error mapping, minus everything phone-shaped (host
    events, purge, TTLs). ``client`` is accepted and ignored: the shared
    hosting passes ``None`` for a local kind.

    The walk, per capture N (index = accepted count):

    1. **authorize** — stop check, then the position gate AHEAD of the
       conductor (a hold is not an admission decision — the same ordering the
       relay states), then ``conductor.authorize_begin``. A gate deferral
       becomes a local retry loop at :data:`WIRED_HOLD_POLL_S`; the gate's own
       hold/ceiling budgets bound it, exactly as they bound the phone's
       re-posts.
    2. **capture-while-play** — the recorder starts and CONFIRMS audio is
       flowing before any excitation (the pre-roll guarantee), then
       ``conductor.on_armed`` plays the program synchronously through the
       real DSP chain; the recorder keeps rolling for
       :data:`WIRED_POST_ROLL_S` after the play returns. A local-seam
       ``OSError`` is wrapped in ``CrossoverV2LocalSeamError`` exactly as the
       relay wraps it (W6 finding G), so it lands in the internal-error arm,
       never a transport arm.
    3. **consume** — the minted :class:`WiredCaptureAnswer` goes to
       ``conductor.consume_capture``; the verdict is persisted and the
       eager-fit trigger honored, byte-for-byte the relay's consume policy.

    Rejected verdicts auto-retry the same index on the next attempt (the
    remote tier's auto-advance shape; a gated session re-confirms the
    position per (index, attempt), a gateless one settles
    :data:`WIRED_RETRY_SETTLE_S` first), bounded by the plan's own
    ``max_attempts``; ``terminal`` verdicts end the walk immediately.

    **The held set (work order D1)** works exactly as the relay's: when the
    target is met and the host still holds the pre-apply group open, the walk
    waits for ``complete_event`` (the host's ``request_complete`` seam — the
    wired stand-in for the phone's authenticated completion event) and then
    drives the host's group close. The wait is bounded by ``ceiling_s`` —
    the session's own wall-clock ceiling, the same clock the volume plan
    arms — and expiry persists the registry's own honest
    ``session_ceiling_expired``, never a transport claim.

    **A per-take RETAKE (``retake_event``, the host's ``request_retake`` seam)
    is honoured wherever the walk is WAITING ON A PERSON**, which is the
    relay's own window ("only while the begin for the next entry has not been
    seen yet") expressed locally: a HELD BEGIN, and the held-set window above.
    Nowhere else — between an accepted capture and the next begin nothing here
    pauses, so there is no moment to interject in that a hold does not already
    cover.

    It re-authorizes and re-captures the slot that JUST COMPLETED (``index ==
    accepted``, never ``accepted + 1``), spending an ordinary attempt against
    the plan's ``max_attempts`` and the conductor's own per-slot extras ledger.
    ``accepted`` is never advanced by one: the slot was counted once and stays
    counted. An accepted take REPLACES the retained position (the conductor's
    retention is per-index idempotent) and a rejected one leaves the original
    standing — nothing was dropped on its behalf. Both takes stay banked under
    their own attempt; the fit reads the retained one. ``complete_event`` wins
    a tie: a household that said "done" is not asked to say it twice.

    Leaving a held begin for a retake tells the gate so
    (:meth:`PositionGate.abandon_hold`), because a hold nobody is running any
    more must stop being the position the envelope advertises.
    """

    poll_s = WIRED_HOLD_POLL_S if poll_interval_s is None else float(poll_interval_s)

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        del client  # local provider: no relay client exists
        from jasper.active_speaker.crossover_v2_flow import (
            PHASE_DONE,
            REASON_INTERNAL_ERROR,
            REASON_REGISTRY,
            REASON_SESSION_CEILING_EXPIRED,
        )
        from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
        from jasper.capture_relay.session import (
            CaptureBeginDeferred,
            CaptureBeginRefused,
            CaptureFailed,
            CaptureStopped,
        )
        from jasper.correction.coordinator import MeasurementWindowError

        # The host's side of the seam, late-bound on purpose (#2662): the
        # persisted codes, the program-failure classifier, the local-seam
        # error type and the eager-fit starter are host policy, and resolving
        # them at call time keeps the host module their single point of truth
        # — a test double patched there is honored here.
        from jasper.web import correction_crossover_v2 as _host

        plan = getattr(pi_session.spec, "capture_plan", None)
        if plan is None:
            raise CaptureFailed("a wired session requires a capture_plan spec")
        sample_rate_hz = int(pi_session.spec.sample_rate_hz)
        session_id = str(pi_session.session_id)

        def _make_recorder(max_capture_s: float) -> Any:
            if recorder_factory is not None:
                return recorder_factory(max_capture_s)
            from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS

            channels = int(
                SUPPORTED_MODELS.get(device.model_key, {}).get(
                    "capture_channels", 2
                )
            )
            return WiredRecorder(
                device.pcm,
                sample_rate_hz=sample_rate_hz,
                channels=channels,
                max_capture_s=max_capture_s,
            )

        def _capture_budget_s(entry: Any) -> float:
            """Bound one capture's memory/duration from the plan's own facts:
            the entry's DECLARED acoustic length (never a deadline — the
            budget direction here is generous) plus the named pre-play and
            post-roll allowances."""
            duration_ms = getattr(entry, "duration_ms", None)
            program_s = (
                float(duration_ms) / 1000.0
                if isinstance(duration_ms, int) and duration_ms > 0
                else _fallback_program_s()
            )
            return program_s + WIRED_PRE_PLAY_ALLOWANCE_S + WIRED_POST_ROLL_S

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
            slot accepted BEFORE it. That is faithful to the relay rule this
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
                    # The position gate AHEAD of the conductor, the relay's
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
                    # A HELD begin is exactly the relay's retake window: the
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

        def _mint_answer(recording: Any) -> WiredCaptureAnswer:
            channel, mono, rms_dbfs = select_capture_channel(recording)
            zero_count, zero_runs = scan_zero_runs(mono)
            wav, encoded_frames = encode_wav_s32(
                mono, sample_rate_hz=recording.sample_rate_hz
            )
            report = build_capture_integrity_report(
                recording,
                encoded_frames=encoded_frames,
                zero_run_count=zero_count,
                zero_runs=zero_runs,
            )
            device_meta = {
                "label": f"{device.model_label} ({device.card_id})",
                "wired": True,
                "card": device.card_id,
                "usb_id": device.usb_id,
                "model_key": device.model_key,
                "pcm": device.pcm,
                "channel_selected": channel,
                "channel_rms_dbfs": _json_safe_dbfs(rms_dbfs),
            }
            return WiredCaptureAnswer(
                wav=wav,
                device=device_meta,
                setup=_wired_setup_reference(_host),
                capture_integrity=report,
            )

        def _capture_one(index: int, attempt: int, entry: Any) -> Mapping[str, Any]:
            recorder = _make_recorder(_capture_budget_s(entry))
            recorder.start()
            played = False
            try:
                try:
                    # Plays the phase's program through the real DSP chain and
                    # returns when playback (and graph restore) is done — the
                    # recorder has been rolling since before this line, which
                    # is the whole pre-roll story.
                    conductor.on_armed(None)
                except OSError as exc:
                    # Finding G's boundary, unchanged: a LOCAL seam OSError is
                    # not a transport death and must land in the
                    # internal-error arm.
                    raise _host.CrossoverV2LocalSeamError(str(exc)) from exc
                played = True
            finally:
                # ANY escape — a seam error, a cancellation — must release
                # the live ALSA device. Flag-in-finally rather than a broad
                # except: nothing is caught, only cleaned up after.
                if not played:
                    recorder.abort()
            recording = recorder.finish(tail_s=WIRED_POST_ROLL_S)
            answer = _mint_answer(recording)
            # Our own mint always carries both mappings; the `or {}` is for the
            # type only, never a reachable default.
            report = answer.capture_integrity or {}
            device_meta = answer.device or {}
            log_event(
                logger,
                "correction.crossover_v2_wired_capture",
                session_id=session_id,
                index=index,
                attempt=attempt,
                frames=recording.frames,
                gaps=recording.gap_count,
                gap_frames=recording.gap_frames,
                zero_runs=int(report.get("zero_run_count", 0)),
                channel=int(device_meta.get("channel_selected", 0)),
            )
            try:
                verdict = conductor.consume_capture(index, attempt, answer, entry)
            except OSError as exc:
                raise _host.CrossoverV2LocalSeamError(str(exc)) from exc
            code = verdict.get("code") if isinstance(verdict, Mapping) else None
            _host.persist_conductor_state(
                conductor,
                failure_code=code if not verdict.get("accepted") else None,
                evidence=evidence_refs,
            )
            # The eager-fit trigger, byte-for-byte the relay consume's policy
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
                    # usable — same immediate end the relay runner takes.
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
                    # (the PR-L4 accountability veto) propagates like any
                    # admission refusal.
                    _host.drive_group_close(conductor, evidence=evidence_refs)
                    continue
                # The set is held open, which the relay states is exactly when
                # the just-accepted slot is still retakeable. Asked AFTER the
                # completion wait above so a household that said "done" is
                # never asked to say it twice.
                if _retake_wanted():
                    attempt, end_walk = _serve_retake(accepted, attempt, deadline)
                    if end_walk:
                        return

        try:
            opened = await volume.open()
        except (SessionVolumePlanError, MeasurementWindowError) as exc:
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

        import asyncio

        walk_task = asyncio.create_task(asyncio.to_thread(_walk))
        try:
            try:
                await asyncio.shield(walk_task)
            except asyncio.CancelledError:
                # Stop drains the walk before cleanup, the relay's shape: the
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
            # stamped — so the relay's precedence would let a prior
            # rejection's code shadow a later ceiling expiry (a
            # capture-quality claim persisted over a clock that ran out).
            # The conductor's stamp is the fallback for refusals that carry
            # no registered code of their own (the admission arms raise with
            # a rendered MESSAGE and stamp the code separately). The relay's
            # REASON_RELAY_TIMEOUT fallback is deliberately NOT mirrored —
            # there is no link here to blame — and the relay runner's own
            # inverted precedence is flagged (PR body) but out of scope: its
            # gate refusals reach a different arm shape via the phone.
            refusal_code = str(getattr(refusal, "code", "") or "")
            if refusal_code not in REASON_REGISTRY:
                refusal_code = ""
            code = refusal_code or conductor.last_failure_code or REASON_INTERNAL_ERROR
            _host._persist_terminal_failure(conductor, code)
            await _abandon_best_effort(session_id, volume)
            raise
        except Exception as exc:  # noqa: BLE001 — cleanup-and-reraise, the relay's arm
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
            _host._persist_terminal_failure(conductor, code, refusals=refusals)
            await _abandon_best_effort(session_id, volume)
            raise
        # Walk finished without a failure.
        done = conductor.current_phase == PHASE_DONE
        _host.persist_conductor_state(
            conductor,
            failure_code=None if done else conductor.last_failure_code,
            evidence=evidence_refs,
        )
        if done:
            try:
                await volume.close()
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    logger,
                    "correction.crossover_v2_volume_close_failed",
                    level=logging.CRITICAL,
                    session_id=session_id,
                    component="volume_close",
                    error_type=type(exc).__name__,
                )
            else:
                log_event(
                    logger,
                    "correction.crossover_v2_cleanup_complete",
                    session_id=session_id,
                    component="volume_close",
                )
        else:
            await _abandon_best_effort(session_id, volume)

    return _run_and_consume


async def _abandon_best_effort(session_id: str, volume: Any) -> None:
    """Drain the walked-away volume — the §5.5 guarantee, the relay's copy of
    record (same events, same CRITICAL on failure)."""
    try:
        await volume.abandon()
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "correction.crossover_v2_volume_abandon_failed",
            level=logging.CRITICAL,
            session_id=session_id,
            component="volume_abandon",
            error_type=type(exc).__name__,
        )
        return
    log_event(
        logger,
        "correction.crossover_v2_cleanup_complete",
        session_id=session_id,
        component="volume_abandon",
    )
