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

Deferred to W3 (stated, not implied): the wizard UI for wired sessions —
including a voluntary-retake affordance (the held-set window below already
holds the set open exactly as the relay does; nothing local can INITIATE a
retake yet) and a named household-facing reason for a mid-session mic loss
(today that is ``internal_error`` copy with the specific cause in the
journal).
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
            from jasper.audio_measurement.calibration import SUPPORTED_MODELS

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
                _authorize(index, attempt, entry, deadline)
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
            # The conductor's own refusal carries its code in
            # last_failure_code; a gate (or ceiling) refusal carries its own
            # registered code on the exception. The relay's REASON_RELAY_TIMEOUT
            # fallback is deliberately NOT mirrored — there is no link here to
            # blame, so an unregistered code degrades to internal_error.
            gate_code = str(getattr(refusal, "code", "") or "")
            if gate_code not in REASON_REGISTRY:
                gate_code = ""
            code = conductor.last_failure_code or gate_code or REASON_INTERNAL_ERROR
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
