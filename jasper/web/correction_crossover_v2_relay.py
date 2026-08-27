# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover flow's RELAY capture provider (#2662, strangler slice 1).

The capture-source seam (decision 13,
:mod:`jasper.active_speaker.crossover_v2.capture_source`) says the conductor
asks for a capture of program X at position Y and a provider answers with WAV
plus metadata — and that a source's own choreography is that provider's
private internals. This module IS the phone-relay provider: everything here is
mechanics only the relay path has —

* the plan-walk hosting (:func:`build_v2_run_and_consume`): the REAL
  ``jasper.capture_relay.session.run_capture_plan`` on a worker thread, the
  shielded cancel-drain, the purge on every exit path, and the translation of
  relay-internal deaths (``CaptureTimeout`` / ``RelayError`` / phone Stop)
  into the flow's own reason vocabulary before the host persists them;
* the phone's progress choreography: the pre-tone phase ladder
  (:func:`start_program_phase_ladder` on :class:`PlaybackStartSignal`), the
  terminal host events that stop a phone waiting on a dead session, and the
  purge grace that lets those events land before the session vanishes;
* the relay link's TTL policy (:func:`relay_link_ttl_s`).

What is NOT here, on purpose: durable-state writes, the persisted failure
codes, the session-volume policy, admission, and the position gate — those are
the host's (:mod:`jasper.web.correction_crossover_v2`), reached late-bound
through the host module so it stays their single point of truth (and so test
doubles patched onto the host are honored from this side of the seam). The
host injects the volume lifecycle (``V2VolumeHooks``) and the gate; this
module decides only WHEN to drive them, which any provider must.

Split out of ``correction_crossover_v2.py`` behavior-unchanged — the host
re-publishes the names its callers and tests already address there.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.web.correction_crossover_v2 import PositionGate, V2VolumeHooks

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# pre-tone phase ladder (#1824 D3/D4)
# --------------------------------------------------------------------------- #

# The phone-visible names for what the speaker is ACTUALLY doing. Only
# ``sweep_started``/``sweep_complete`` existed before, and ``sweep_started`` was
# posted synchronously in ``on_armed`` — ~4.6 s before any sound on a courtesy-
# prelude program (0.6 s of beeps + a 3.0 s settle + the 1.0 s pre-pilot
# ambient window), so the phone said "Playing the measurement tone…" through
# the entire quiet stretch the household is being asked to be quiet in. The two
# names below are that missing middle. ``ambient_started`` in particular had NO
# producer anywhere on the Pi — the capture page's countdown consumer for it
# has been shipped-but-dead since the producer it was written against was
# deleted (see docs/HANDOFF-correction.md).
HOST_PHASE_PRELUDE_STARTED = "prelude_started"
HOST_PHASE_AMBIENT_STARTED = "ambient_started"
HOST_PHASE_SWEEP_STARTED = "sweep_started"
HOST_PHASE_SWEEP_COMPLETE = "sweep_complete"

# NAMED RESIDUAL — ON-DEVICE: the size of this bias is NOT measured. The ladder
# is anchored where the host HANDS a program to the playback path, which leads
# real audio by the verified-source read plus the ALSA/output prefill: tens to a
# few hundred ms, device-dependent, and not one number we can look up. Delaying
# every step is INTENDED to bias late, so the residual lands on the safe side —
# a phase line appearing late is harmless, a phone claiming the tone is playing
# while the room is silent is the failure this ladder exists to remove. It is
# not a guarantee: a prefill longer than this value would still put the sweep
# line marginally early.
#
# Two things push the same way and are worth knowing before tuning it: the
# phone only repaints on its own poll (``progress_poll_ms``, ≤1 s), which adds
# further late bias on the rendered line; and the household reads copy, not
# timestamps. Measure the real anchor-to-audio interval on hardware before
# changing this, and prefer replacing the whole estimate with an observed
# playback start over shrinking the number from reasoning alone.
PHASE_LADDER_START_SKEW_S = 0.35


class PlaybackStartSignal:
    """The seam between the play binding and the runner's phone-phase posts.

    ``bind_production_play`` is built inside ``_open`` — before the runner
    exists — so the play path cannot post to the phone itself. It fires this
    signal at the instant a program's WAV reaches the playback call; the runner
    installs a handler for the duration of ONE armed capture and clears it
    afterwards, so a late fire from a play that outlived its capture is a no-op
    rather than a phase post against the wrong capture.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handler: Callable[[Any], None] | None = None

    def install(self, handler: Callable[[Any], None]) -> None:
        with self._lock:
            self._handler = handler

    def clear(self) -> None:
        with self._lock:
            self._handler = None

    def fire(self, program: Any) -> None:
        with self._lock:
            handler = self._handler
        if handler is None:
            return
        try:
            handler(program)
        except (
            OSError, RuntimeError, ValueError, AttributeError, KeyError, TypeError,
        ):
            # Progress reporting must never be able to stop a measurement, and
            # this fires from INSIDE the play path — an escape here would abort
            # a sweep over a status line. The handler reads a program object it
            # does not own (AttributeError/TypeError/KeyError on an unexpected
            # shape) and starts a thread (RuntimeError), so the list covers what
            # it can actually raise; pinned by
            # test_playback_signal_handler_failure_never_stops_the_measurement.
            logger.warning("v2 playback-start signal handler failed", exc_info=True)


def program_phase_schedule(
    program: Any,
) -> tuple[tuple[float, str, dict[str, Any]], ...]:
    """When each phone-visible phase of ``program`` actually becomes audible.

    Returns ``((offset_s, phase, extra_fields), …)`` ordered by offset, where
    ``offset_s`` is measured from the start of the program WAV and
    ``extra_fields`` are the wire fields that phase carries (empty for phases
    that carry none).

    Offsets are read off the program's OWN segment table
    (``start_sample``/``sample_rate_hz``), never re-derived from the composer's
    constants: a program that stops carrying a courtesy prelude or an ambient
    window simply stops emitting that phase, and a composer that moves either
    one moves this schedule with it. A program with no segments (or an
    unreadable rate) yields an empty schedule — the caller then posts nothing,
    which is what an older/simpler program should produce.

    ``ambient_started`` carries ``quiet_requested``, and it is a MEASUREMENT
    fact, not a presentation one. The two ambient windows are opposites:

    * every non-CHECK phase's 1 s pre-pilot window "MUST be measured with the
      room already quiet" — the phone should ask.
    * CHECK's 12 s window is the SESSION's room-noise measurement, "deliberately
      taken before the household is asked to go quiet" (both quotes:
      :mod:`jasper.audio_measurement.program`'s module docstring). The ambient
      band-floor report and the gain solve read it. A phone that asked for quiet
      during THAT window would change the very floor it is measuring — a copy
      string silently editing the measurement.

    Asked of the program's own ``phase``, which is what distinguishes the two
    windows — see the flag's own comment for why it is no longer derived from
    where the courtesy beeps sit.
    """
    from jasper.audio_measurement.program import (
        AMBIENT_SEGMENT_ID,
        KIND_COURTESY_TONE,
        PROGRAM_PHASE_CHECK,
        STIMULUS_KINDS,
    )

    try:
        rate = float(getattr(program, "sample_rate_hz", 0) or 0)
    except (TypeError, ValueError):
        return ()
    segments = tuple(getattr(program, "segments", ()) or ())
    if rate <= 0 or not segments:
        return ()

    steps: list[tuple[float, str, dict[str, Any]]] = []
    beeps = [s for s in segments if s.kind == KIND_COURTESY_TONE]
    if beeps:
        steps.append(
            (
                min(s.start_sample for s in beeps) / rate,
                HOST_PHASE_PRELUDE_STARTED,
                {},
            )
        )
    ambient = [s for s in segments if s.segment_id == AMBIENT_SEGMENT_ID]
    if ambient:
        first = min(ambient, key=lambda s: s.start_sample)
        steps.append(
            (
                first.start_sample / rate,
                HOST_PHASE_AMBIENT_STARTED,
                {
                    "duration_s": first.n_samples / rate,
                    # WHICH window this is, asked of the program's own phase.
                    # CHECK's is the session room-noise measurement and the
                    # only one that must NOT be hushed; every other phase's is
                    # the 1 s pre-pilot window the pilot SNR guard reads.
                    #
                    # It used to ask "does this window sit after the courtesy
                    # beeps?", which answered the same while every capture
                    # carried a prelude. Since the 2026-08-18 trim only a
                    # session's OPENING capture does, so that question would
                    # answer "no warning played, do not ask" on the
                    # mid-session captures whose window has to be quiet.
                    "quiet_requested": (
                        str(getattr(program, "phase", "")) != PROGRAM_PHASE_CHECK
                    ),
                },
            )
        )
    stimulus = [s for s in segments if s.kind in STIMULUS_KINDS]
    if stimulus:
        steps.append(
            (
                min(s.start_sample for s in stimulus) / rate,
                HOST_PHASE_SWEEP_STARTED,
                {},
            )
        )
    steps.sort(key=lambda step: step[0])
    return tuple(steps)


def start_program_phase_ladder(
    post_phase: Callable[..., None],
    program: Any,
    *,
    skew_s: float | None = None,
) -> Callable[[], None]:
    """Post ``program``'s phase ladder on the program's own clock.

    Runs on one short-lived daemon thread (the play call owns the caller's
    thread for the whole program) and returns a cancel callable the caller
    invokes when playback returns — so a program that ends early, fails, or is
    stopped cannot leave a timer behind that posts a phase for a capture that
    is already over.

    ``skew_s`` defaults to :data:`PHASE_LADDER_START_SKEW_S`, read at CALL time
    so the constant stays the single place the bias is expressed (and a test
    can drive the ladder without waiting it out).
    """
    schedule = program_phase_schedule(program)
    if not schedule:
        return lambda: None

    skew = PHASE_LADDER_START_SKEW_S if skew_s is None else float(skew_s)
    done = threading.Event()

    def _run() -> None:
        started = time.monotonic()
        for offset_s, phase, extra in schedule:
            delay = (offset_s + skew) - (time.monotonic() - started)
            if delay > 0 and done.wait(delay):
                return
            if done.is_set():
                return
            post_phase(phase, **extra)

    thread = threading.Thread(
        target=_run, name="crossover-v2-phase-ladder", daemon=True
    )
    thread.start()

    def _cancel() -> None:
        done.set()
        # UNBOUNDED join, deliberately. The relay's host-event slot is
        # last-write-wins, so a ladder post still in flight when this returns
        # would land AFTER whatever the caller posts next — overwriting a
        # `sweep_complete`, or worse a terminal `capture_result`, and putting
        # the phone back to polling a refusal it can no longer see (the
        # expired-link pathology this PR removes elsewhere). A bounded join
        # would leave exactly that race open on a slow post.
        #
        # Safe to wait: the only thing this thread does is
        # ``client.post_host_event``, whose urllib transport carries
        # ``capture_relay.client.DEFAULT_TIMEOUT_S`` (15 s) and whose failures
        # the caller already swallows as OSError — so the wait is bounded by
        # the transport, not by hope.
        thread.join()

    return _cancel


# W6 hardware run 3 finding H: the catch-all cleanup arm below posts a
# terminal host event (§5.10's ``capture_result``) so the phone stops
# recording into silence, then purges the relay session. Purging
# immediately after can race the phone's very next poll — the driver saw a
# bare 404 on the session's own status endpoint ~1 s after the terminal
# event, because the session was already gone. This grace gives the just-
# posted event a bounded window to actually reach the phone (over the
# public relay) before the session disappears; it does not delay the
# household's volume restore, which stays immediate.
TERMINAL_FAILURE_PURGE_GRACE_S = 3.0


def build_v2_run_and_consume(
    conductor: Any,
    *,
    volume: "V2VolumeHooks",
    stop_event: threading.Event,
    stop_lock: Any,
    position_gate: "PositionGate | None" = None,
    evidence_refs: Mapping[str, Any] | None = None,
    poll_interval_s: float | None = None,
    timeout_s: float | None = None,
    first_begin_timeout_s: float | None = None,
    playback_started: PlaybackStartSignal | None = None,
) -> Callable[[Any, Any], Any]:
    """The async ``run_and_consume(client, pi_session)`` for one v2 session.

    Mirrors ``build_crossover_relay_plan_run_and_consume``'s thread model: the
    REAL :func:`jasper.capture_relay.session.run_capture_plan` runs on a
    worker thread (``asyncio.to_thread``), the awaiting task shields it
    through cancellation (Stop drains the runner before purging), and the
    relay session is purged on every exit path.

    **No session applies anything (two-stage commission work order D1).** This
    runner took ``run_async``/``camilla_factory``/``idle_hold`` until PR-T3 for
    one reason: to fire ``handle_v2_apply`` on a background thread the instant
    the pre-apply cloud closed, and to keep the socket-activated wizard alive
    while that thread ran (#1854). Apply is now the household's own POST from
    the review screen — served in-request, so the idle tracker's ordinary
    in-flight-request accounting covers it — and this runner needs none of the
    three. What replaced them is the ``complete_capture_set`` seam below: the
    household's explicit "Continue" closes the group, fits the candidate, and
    persists it, and the journey stops there.

    Host-owned error mapping (S1c):

    * ``CaptureTimeout`` / ``RelayError`` / ``OSError`` / generic
      ``CaptureFailed`` — relay-session death ⇒ ``relay_timeout`` failure
      state + volume ABANDON (the §5.5 walked-away guarantee). The
      ``OSError`` here is genuinely the relay TRANSPORT (e.g.
      ``run_capture_plan``'s poll loop reaching an unreachable host) — a
      LOCAL play/analyze seam OSError never reaches this arm: ``on_armed``/
      ``consume`` below convert it to ``CrossoverV2LocalSeamError``
      first (W6 hardware run 3 finding G), so it falls through to the
      catch-all arm's ``internal_error`` instead.
    * ``CaptureAborted`` — a deliberate phone Stop (``reason == "stopped"``)
      gets its own honest ``user_stopped`` failure state; every other abort
      reason (backgrounded / vanished) still reads as ``relay_timeout``. Both
      abandon the volume identically — only the persisted reason differs.
    * ``CaptureBeginRefused`` — the conductor already recorded the phase's own
      failure code; persist it + abandon. (PR-L4's seam was the group-close
      producer and has refused nothing since (c)/(i); admission still does.)
    * ``CaptureStopped`` / cancellation — expected control flow: abandon the
      volume, no failure code.
    * ANY other ``Exception`` — the W6.1 catch-all cleanup arm: the seams
      raise open-endedly (``CamillaUnavailable`` is a bare Exception), so
      every non-relay failure posts a terminal host event, persists
      ``program_unplayable`` (program/admission/flow classes) or
      ``internal_error`` (everything else — including
      ``CrossoverV2LocalSeamError``), abandons the volume (releasing the
      session measurement pause), waits a bounded grace period (finding H —
      the just-posted terminal host event must reach the phone before the
      relay session is purged out from under its next poll), purges, and
      re-raises.
    * Plan complete with every phase this session ran accepted ⇒ CLOSE (exact
      restore); a completed plan that did not reach done (attempt budget
      exhausted) abandons. Stage 1 takes the CLOSE path like any other complete
      session: its phases are all accepted, the household is walking back to a
      browser rather than to another capture, and an exact restore is the
      honest end for a measurement that finished.
    """

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        from jasper.capture_relay.client import RelayError
        from jasper.capture_relay.session import (
            HOST_PHASE_CAPTURE_RESULT,
            HOST_PHASE_CAPTURE_SET_EXHAUSTED,
            CaptureAborted,
            CaptureBeginRefused,
            CaptureFailed,
            CaptureStopped,
            TIME_BUDGET_NONE,
            CaptureTimeout,
            expired_time_budget,
            purge,
            run_capture_plan,
        )
        from jasper.active_speaker.crossover_v2.journey import (
            PHASE_APPLYING,
            PHASE_DONE,
        )
        from jasper.active_speaker.crossover_v2.refusal_copy import (
            REASON_INTERNAL_ERROR,
            REASON_REGISTRY,
            REASON_RELAY_TIMEOUT,
            REASON_REVIEW_HOLD_TIMEOUT,
            REASON_USER_STOPPED,
            TRANSIENT_AUTO_RETRY_CODES,
        )
        from jasper.active_speaker.crossover_v2_flow import v2_first_begin_timeout_s
        from jasper.active_speaker.crossover_v2.session_graph import (
            SessionGraphError,
        )
        from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
        from jasper.correction.coordinator import MeasurementWindowError

        # The host's side of the seam, late-bound through the host module on
        # purpose (#2662): durable-state persistence, the program-failure
        # classifier, the local-seam error type, the gate's terminal codes and
        # the eager-fit starter are host policy, and resolving them at call
        # time keeps the host module their single point of truth — a test
        # double patched there is honored here.
        from jasper.web import correction_crossover_v2 as _host

        def complete_capture_set() -> None:
            """The household's "all spots measured — Continue" tap (D1).

            **This is the group-close seam at the host boundary**, and the
            only thing that fits a correction in stage 1. It replaces the
            inference this runner used to make — that VERIFY's begin, the one
            index past the walked cloud, WAS the confirmation — which stage 1
            structurally cannot supply because it has no VERIFY entry.

            What it deliberately does NOT do any more is apply. Until PR-T3
            this function's predecessor (``_fire_auto_apply``) started the
            apply transaction on its own background thread the instant the
            group closed: unconditionally, inside the relay session, three
            seconds before VERIFY, with the household holding a phone. The
            2026-07-28 ruling made the review interlude the apply decision
            point, so the candidate this builds is a PROPOSAL and the apply is
            the household's own POST to ``/correction/crossover/v2/apply``.

            The persist is load-bearing and must happen here: ``handle_v2_apply``
            reads the candidate off the DURABLE state, not off the conductor,
            so a confirmation whose fit never reached disk would leave the
            review screen with nothing to review.

            A ``CaptureBeginRefused`` raised under the close propagates to the
            runner, which publishes it to the phone and ends the session
            exactly as an admission refusal does — a contract, not a claim
            about PR-L4's seam, which has refused nothing since (c) and (i).

            The sequence itself — persist FIRST (the combine plus the fit are
            the slowest thing in the session, the wizard renders from durable
            state, and until that write landed the speaker page kept telling
            the household to confirm on their phone for the several seconds
            after they already had), confirm, persist the fitted result — is
            host policy every provider must drive identically, so its single
            owner is ``_host.drive_group_close`` (#2662 W2b); this closure is
            the relay's WHEN, not a second WHAT.
            """
            _host.drive_group_close(conductor, evidence=evidence_refs)

        def authorize(index: int, attempt: int, entry: Any = None) -> None:
            # Admission, and ONLY admission. The group close used to run here
            # first, inferred from a begin whose index was past the walked
            # cloud, and fired the apply behind it; both moved to
            # ``complete_capture_set`` above, on the household's explicit
            # signal (work order D1). Nothing on this path applies anything.
            with stop_lock:
                if stop_event.is_set():
                    raise CaptureStopped("capture stopped")
            # The POSITION GATE, ahead of the conductor and deliberately so: a
            # hold is not an admission decision, and routing it through
            # ``authorize_begin`` would spend ledger and stamp failure state on
            # a capture not refused anything. It raises ``CaptureBeginDeferred``
            # past this frame to the runner, which the conductor never sees. On
            # THIS runner the gate is the remote tier's alone — the other gated
            # shape (#2879) is WIRED-only, and a relay hand-walk keeps its tap.
            if position_gate is not None:
                position_gate.gate(index, attempt, entry)
            conductor.authorize_begin(index, attempt, entry)

        def completion_signal_required() -> bool:
            """Whether this set is the household's to end (D1).

            True exactly while the pre-apply cloud is walked and unconfirmed —
            the window in which a voluntary retake of the final position still
            means something, and the window the runner must not close by
            arithmetic. Every other session shape (the post-apply session, the
            recovery re-verify) never stashes a pre-apply group, so this is
            always False there and the runner ends those sets exactly as it
            always has.

            **This is decoupled from the candidate, and has to stay that way**
            (eager-fit rider on #1806, shipped 2026-07-30). The predicate used
            to resolve through ``self._candidate is None``, which is ALSO the
            group close's fire-once guard — so the rider, which fits a
            candidate BEFORE the household confirms, would have flipped this to
            False and un-held the runner's set, shutting the retake window in
            the same instant, silently. It now resolves through
            ``_group_confirmed``: the held-set question is "has the household
            confirmed?", never "does a candidate exist?". An eagerly-fitted
            candidate parks in ``_speculative_close`` and is invisible here.

            The two must not be re-merged. If you are tempted, the discriminator
            is ``test_an_eager_fit_failure_surfaces_on_the_confirm_not_before``:
            after a close that RAISED, ``_candidate`` is unset (T3's
            retryability) but the household has confirmed, so this must read
            False. Only the decoupled predicate gets that right. See the
            predicate's own docstring on the conductor.
            """
            return bool(conductor.cloud_measure_group_awaiting_confirm())

        def _post_sweep_phase_best_effort(phase: str, **extra: Any) -> None:
            """Post one phone-visible progress phase (§5.10 progress).

            The capture page's ``waitForSweepComplete``
            (``capture-page/js/main.js``) polls ``host_event.phase`` around its
            own play wait and otherwise sits until ITS OWN timeout elapses —
            the v2 runner posted nothing (W6 run 5), so a real phone could
            never complete a v2 capture.

            The phases this posts, in the order one capture produces them:
            ``prelude_started`` (the courtesy beeps), ``ambient_started``
            (the room-listening window; carries ``duration_s`` and
            ``quiet_requested``), ``sweep_started`` (the tone) — all three from
            :func:`start_program_phase_ladder`, on the program's own clock —
            and finally ``sweep_complete``, the ONLY phase that makes the
            phone's wait return. ``**extra`` carries whatever fields a phase
            declares; the relay relays them verbatim.

            Best-effort: a transient post failure here is a progress-only miss,
            not a capture failure — the existing terminal host event (or the
            phone's own wait timeout) still resolves the phone's wait on any
            real failure.
            """
            armed = conductor.armed_capture
            index, attempt = armed if armed is not None else (None, None)
            try:
                client.post_host_event(
                    pi_session.session_id,
                    pi_session.pull_token,
                    {"phase": phase, "index": index, "attempt": attempt, **extra},
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 sweep progress host-event post failed", exc_info=True
                )

        def on_armed(state: Any) -> None:
            if stop_event.is_set():
                raise CaptureStopped("capture stopped")
            # The pre-tone phase ladder (#1824 D4). ``sweep_started`` used to be
            # posted right here, synchronously, BEFORE the play seam had done
            # anything at all — so the phone announced the measurement tone
            # ~4.6 s before the first sound of a courtesy-prelude program and
            # stayed on that line through the beeps, the settle and the room-
            # listening window. The ladder instead posts each phase when it
            # actually becomes audible, anchored at the play path's own WAV
            # handoff (``PlaybackStartSignal``).
            #
            # Backwards-compatible in the one direction that matters: when no
            # play-start signal is wired (a host binding its own play seam, or
            # a test fake), keep the legacy eager post so the phone's wait still
            # resolves rather than sitting until its own timeout.
            cancel_ladder: Callable[[], None] = lambda: None

            def _start_ladder(program: Any) -> None:
                nonlocal cancel_ladder
                cancel_ladder = start_program_phase_ladder(
                    _post_sweep_phase_best_effort, program
                )

            if playback_started is not None:
                playback_started.install(_start_ladder)
            else:
                _post_sweep_phase_best_effort(HOST_PHASE_SWEEP_STARTED)
            # Finding G: on_armed's ``conductor.on_armed`` → ``seams.play`` is a
            # LOCAL seam (the DSP writer lock, CamillaController) — an OSError
            # here (e.g. EROFS opening the lock file) is not a relay-transport
            # death and must not be caught by the relay-death arm below.
            try:
                conductor.on_armed(state)
            except OSError as exc:
                raise _host.CrossoverV2LocalSeamError(str(exc)) from exc
            finally:
                if playback_started is not None:
                    playback_started.clear()
                cancel_ladder()
            _post_sweep_phase_best_effort(HOST_PHASE_SWEEP_COMPLETE)

        def consume(index: int, attempt: int, result: Any):
            # Same local-seam boundary as on_armed, for consume_capture's
            # analyze seam.
            try:
                verdict = conductor.consume_capture(index, attempt, result)
            except OSError as exc:
                raise _host.CrossoverV2LocalSeamError(str(exc)) from exc
            code = verdict.get("code") if isinstance(verdict, Mapping) else None
            _host.persist_conductor_state(
                conductor,
                failure_code=code if not verdict.get("accepted") else None,
                evidence=evidence_refs,
            )
            # (An ``auto_apply``-keyed branch lived here until
            # flow-simplification PR-U1, and the flag it read is itself gone
            # since PR-T3 removed auto-apply. It fired the apply off whichever
            # capture verdict carried the flag — MEASURE's accept originally,
            # the CLOUD_MEASURE group close after the 2026-07-27 timing move.
            # §2.6 moved the trigger off a capture verdict entirely and onto
            # the household's confirmation past the walked cloud, so the flag
            # no longer reaches any verdict a production session emits and the
            # branch became unreachable. It is DELETED rather than left as a
            # comment-that-lies: ``authorize`` above is now the only place that
            # fires ``_fire_auto_apply``, which is the whole point of putting
            # the confirm seam at the host boundary.)
            #
            # The EAGER FIT trigger (owner UX direction, 2026-07-30). This
            # verdict flag marks the one accept that leaves a walked, unconfirmed
            # pre-apply cloud — the group close — and a voluntary retake raises
            # it again on ITS accept, which is exactly when a re-fit is wanted:
            # the retake dropped the previous bank when it re-stashed the
            # combine. Started after the persist so the durable state the wizard
            # renders is already the held-window state, and the fit is racing the
            # household's walk rather than the write.
            if verdict.get("awaiting_confirm"):
                _host._start_speculative_group_close(conductor)
            return verdict

        async def _purge_best_effort() -> None:
            try:
                purged = await asyncio.to_thread(purge, client, pi_session)
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    logger,
                    "correction.crossover_v2_cleanup_failed",
                    level=logging.WARNING,
                    session_id=pi_session.session_id,
                    component="relay_purge",
                    error_type=type(exc).__name__,
                )
                return
            log_event(
                logger,
                (
                    "correction.crossover_v2_cleanup_complete"
                    if purged is not False
                    else "correction.crossover_v2_cleanup_failed"
                ),
                level=(logging.INFO if purged is not False else logging.WARNING),
                session_id=pi_session.session_id,
                component="relay_purge",
            )

        async def _abandon_best_effort() -> None:
            try:
                await volume.abandon()
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    logger,
                    "correction.crossover_v2_volume_abandon_failed",
                    level=logging.CRITICAL,
                    session_id=pi_session.session_id,
                    component="volume_abandon",
                    error_type=type(exc).__name__,
                )
                return
            log_event(
                logger,
                "correction.crossover_v2_cleanup_complete",
                session_id=pi_session.session_id,
                component="volume_abandon",
            )

        async def _post_terminal_failure_host_event(code: str) -> None:
            """Tell the phone the session is over so it stops waiting (§5.10).

            A play-seam failure escapes ``run_capture_plan`` WITHOUT posting a
            capture verdict, so the phone records into silence and then polls
            ``capture_result`` forever (W6.1 hardware run 2 froze at
            ``capture_authorized``). Address a terminal ``capture_result``
            (accepted=false, carrying the §5.10 reason so the phone can render
            the failure screen) to the armed capture; fall back to
            ``capture_set_exhausted`` when no capture was armed. Best-effort —
            the operator wizard also shows the persisted failure.

            Issue #2089: the exhausted fallback used to carry only
            ``{"phase": ...}`` — no ``budget``, no cause. Only the catch-all
            program-failure classifier below can reach this branch in
            practice: the OTHER caller (the ``CaptureBeginRefused`` arm)
            never does, because every refusal that can fire before anything
            is armed sets ``relay_published_refusal`` first inside
            ``authorize_begin``, which gates that caller's own post.

            The wire now carries an honest cause: ``budget`` is always
            ``TIME_BUDGET_NONE`` (the same "neither clock ran out" bucket PR
            #2084 shipped for ``_post_session_over_host_event``), plus
            ``code``/``reason``/``banner`` whenever the failure code
            resolves. **This does not change what the phone shows today** —
            its only pre-arm observer, ``waitForCaptureAuthorized``, ignores
            ``budget`` entirely and renders generic session-ended /
            "Link expired" copy from the relay spec, never from this event.
            Rendering the honest cause is issue #2446, which must NOT route
            a pre-arm failure through ``renderPlanExhausted``'s
            transport-flavored copy — that renderer describes a session that
            had already begun.
            """
            spec = REASON_REGISTRY.get(code)
            armed = conductor.armed_capture
            if armed is not None and spec is not None:
                index, attempt = armed
                event: dict[str, Any] = {
                    "phase": HOST_PHASE_CAPTURE_RESULT,
                    "index": index,
                    "attempt": attempt,
                    "accepted": False,
                    "code": spec.code,
                    "template": spec.template,
                    "reason": spec.message or spec.banner,
                    "banner": spec.banner,
                    "auto_retry": spec.code in TRANSIENT_AUTO_RETRY_CODES,
                }
            else:
                event = {
                    "phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED,
                    "budget": TIME_BUDGET_NONE,
                }
                if spec is not None:
                    event.update(
                        code=spec.code,
                        reason=spec.message or spec.banner,
                        banner=spec.banner,
                    )
            try:
                await asyncio.to_thread(
                    client.post_host_event,
                    pi_session.session_id,
                    pi_session.pull_token,
                    event,
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 terminal host-event post failed", exc_info=True
                )

        async def _post_session_over_host_event(budget: str = "") -> None:
            """Tell the phone the whole SESSION ended so its deferred-retry loop
            stops waiting (W6.10 blocker #3).

            ``budget`` names WHICH clock ran out (work order D8, issue #1807),
            or ``""`` when the death was not a timeout at all. The wire always
            carries the field: ``""`` is published as ``TIME_BUDGET_NONE``
            rather than omitted (issue #2083). Omitting it made the phone render
            this event as ``renderPlanExhausted`` — "the speaker reached its
            measurement attempt limit" — which is untrue of BOTH the cases that
            reach here. It is untrue of an expiry (no attempt limit was reached,
            a clock ran out) and untrue of a transport death (no clock ran out
            either, the relay went away), and the three want different things
            from the household. An older page treats the explicit ``"none"`` as
            an unnamed budget and behaves exactly as it does today.

            A watchdog collapse during the "waiting for apply" REVIEW hold
            (``CaptureTimeout``) otherwise left the phone re-posting the same
            ``begin_capture`` against a still-200 relay session with NO terminal
            signal — it sat on the hold screen forever (Chrome round 2: "the
            phone saw nothing"). Unlike ``_post_terminal_failure_host_event``
            this is session-level (``capture_set_exhausted``), not addressed to
            the last-armed capture (MEASURE was accepted — a per-index
            ``capture_result`` there would misreport it): the collapse is not a
            per-capture verdict. Best-effort — the purge-driven 404 the phone
            reads as ``deadSession`` is the backstop, and this post fails
            harmlessly when the failure was the relay transport itself.
            """
            try:
                await asyncio.to_thread(
                    client.post_host_event,
                    pi_session.session_id,
                    pi_session.pull_token,
                    {
                        "phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED,
                        "budget": budget or TIME_BUDGET_NONE,
                    },
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 session-over host-event post failed", exc_info=True
                )

        try:
            opened = await volume.open()
        except (
            SessionVolumePlanError, MeasurementWindowError, SessionGraphError,
        ) as exc:
            # volume.open() raised BEFORE the capture loop owns cleanup — the
            # relay session is already minted (run 2's retry leaked one here
            # when the prior session's volume state was still open, firing
            # SessionVolumePlanError). Purge it best-effort before surfacing so
            # it cannot linger to worker TTL; the volume hook already released
            # any measurement pause it took.
            log_event(
                logger,
                "correction.crossover_v2_volume_open_failed",
                level=logging.WARNING,
                reason=type(exc).__name__,
            )
            await _purge_best_effort()
            raise CaptureFailed(
                "the measurement volume could not be opened"
            ) from exc
        opened_value = getattr(opened, "value", opened)
        if opened is not None and str(opened_value) != "opened":
            # The plan drained itself (emergency attenuation / failure); the
            # recovery screen keys on needs_recovery via the status block.
            # The freshly-minted relay session must not linger to worker TTL
            # when no capture will ever run against it.
            await _purge_best_effort()
            raise CaptureFailed(
                "the fixed measurement volume could not be confirmed"
            )
        plan_kwargs: dict[str, Any] = {}
        if poll_interval_s is not None:
            plan_kwargs["poll_interval_s"] = poll_interval_s
        if timeout_s is not None:
            plan_kwargs["timeout_s"] = timeout_s
        # The FIRST begin gets the wider v2 placement-reading budget (fold-in);
        # every later window (arm/upload/between-capture) keeps the tight
        # per-phase backstop. The REVIEW hold's own rescope lives in the runner.
        plan_kwargs["first_begin_timeout_s"] = (
            first_begin_timeout_s if first_begin_timeout_s is not None
            else v2_first_begin_timeout_s()
        )
        capture_task = asyncio.create_task(
            asyncio.to_thread(
                run_capture_plan,
                client,
                pi_session,
                authorize_begin=authorize,
                on_armed=on_armed,
                consume_capture=consume,
                stop_requested=stop_event.is_set,
                # The held-set pair (work order D1): stage 1's final cloud
                # position IS its capture target, so without these the runner
                # would end the set on arithmetic — the fit would never run,
                # the household's retake window would shut at the same moment,
                # and the review screen would have nothing to review.
                completion_signal_required=completion_signal_required,
                on_completion_signal=complete_capture_set,
                **plan_kwargs,
            )
        )
        try:
            try:
                await asyncio.shield(capture_task)
            except asyncio.CancelledError:
                stop_event.set()
                while not capture_task.done():
                    try:
                        await asyncio.shield(capture_task)
                    except asyncio.CancelledError:
                        continue
                    except (OSError, RuntimeError, ValueError):
                        break
                if capture_task.done() and not capture_task.cancelled():
                    capture_task.exception()
                await _abandon_best_effort()
                await _purge_best_effort()
                raise
        except CaptureStopped:
            await _abandon_best_effort()
            await _purge_best_effort()
            raise
        except CaptureBeginRefused as refusal:
            # The conductor's own budget refusal — its failure code is already
            # in _last_reason. Publish that exact named verdict before cleanup.
            #
            # The POSITION GATE refuses from the same seam but AHEAD of the
            # conductor, so it leaves `last_failure_code` unset: without the
            # middle term a gate refusal fell through to REASON_RELAY_TIMEOUT
            # and told the household "the measurement link timed out" about a
            # transport that never failed. The refusal carries its own code;
            # trust it only when the registry knows it, so an unregistered code
            # from some future raiser degrades to the old fallback rather than
            # reaching a screen with no copy.
            gate_code = str(getattr(refusal, "code", "") or "")
            if gate_code not in REASON_REGISTRY:
                gate_code = ""
            code = conductor.last_failure_code or gate_code or REASON_RELAY_TIMEOUT
            _host._persist_terminal_failure(conductor, code)
            # Only where the relay published nothing itself: on the
            # authorize_begin path it already posted `capture_refused` for the
            # REFUSED index, and this slot is last-write-wins (panel SF1). On
            # the consume path nothing precedes it and the phone waits forever.
            #
            # A gate refusal is an authorize_begin refusal too — `run_capture_plan`
            # posts `capture_refused` with the gate's own code and message before
            # re-raising — but nothing sets the conductor's flag for it, because
            # the conductor never saw it. Re-posting here would overwrite that
            # honest, index-bearing event with a terminal one in the same
            # last-write-wins slot.
            already_published = (
                conductor.relay_published_refusal
                or gate_code in _host.POSITION_GATE_TERMINAL_CODES
            )
            if not already_published:
                await _post_terminal_failure_host_event(code)
            await _abandon_best_effort()
            await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
            await _purge_best_effort()
            raise
        except (CaptureTimeout, CaptureAborted, CaptureFailed, RelayError, OSError) as exc:
            # Relay-session death (§5.10): relay_timeout ⇒ session restart; the
            # walked-away user's volume is always drained. Tell the phone the
            # session is over BEFORE purging (W6.10 blocker #3) — mirror the
            # catch-all arm's terminal-then-grace-then-purge so a watchdog
            # collapse during the apply hold reaches the phone's deferred-retry
            # loop instead of leaving it polling a still-live session forever.
            #
            # A deliberate phone Stop (CaptureAborted, reason == "stopped") is
            # NOT a relay-transport death — it is the household explicitly
            # ending the measurement. Splitting it out gives it its own honest
            # copy instead of the dishonest "the measurement link timed out"
            # claim every other death in this tuple gets.
            code = REASON_RELAY_TIMEOUT
            if isinstance(exc, CaptureAborted) and exc.reason == "stopped":
                code = REASON_USER_STOPPED
            elif (
                isinstance(exc, CaptureTimeout)
                and conductor.current_phase == PHASE_APPLYING
            ):
                # The deferred apply/"review" hold (CaptureBeginDeferred
                # "awaiting_apply") expired: MEASURE was accepted but the
                # conductor's own auto-apply never landed within
                # REVIEW_HOLD_BUDGET_S. RETAINED but unreached since PR-T3
                # (D10): no shipped session parks on that hold any more, so
                # this arm cannot fire in production — kept with the hold it
                # classifies. current_phase is PHASE_APPLYING ONLY in
                # that exact window (MEASURE accepted, VERIFY pending, apply not
                # observed), so it cleanly separates a hold expiry from a
                # generic transport death (#1605) — name the real cause instead
                # of the dishonest "the measurement link timed out". A rare
                # apply-landed-but-phone-bailed race still renders honestly: the
                # envelope's applied-keyed override keys on durable
                # ``applied``, not on this phase.
                code = REASON_REVIEW_HOLD_TIMEOUT
            # WHICH clock ran out, named once and disclosed everywhere (work
            # order D8, issue #1807). Both a step expiry and the relay TTL
            # arrive in this arm and both persist as REASON_RELAY_TIMEOUT, so
            # before this line the only surface that could tell them apart was
            # the exception's own message — and the household saw "the speaker
            # reached its measurement attempt limit", which is neither of them.
            budget = expired_time_budget(exc)
            if budget:
                log_event(
                    logger,
                    "correction.crossover_v2_time_budget_expired",
                    level=logging.WARNING,
                    budget=budget,
                    phase=str(getattr(exc, "phase", "") or ""),
                    conductor_phase=conductor.current_phase,
                    # What the expiry PRESERVED: the phases whose captures were
                    # accepted before the clock ran out. They are on disk as
                    # evidence; they are not a set the next session resumes
                    # from, and the phone's copy says so rather than implying a
                    # resume that does not exist.
                    accepted_phases=",".join(sorted(conductor.accepted_phases)),
                )
            verdict_preserved = _host._persist_terminal_failure(conductor, code)
            if not verdict_preserved:
                await _post_session_over_host_event(budget)
            await _abandon_best_effort()
            await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
            await _purge_best_effort()
            raise
        except Exception as exc:  # noqa: BLE001 — cleanup-and-reraise, see below
            # CATCH-ALL cleanup arm (W6.1 gate ruling). The seams raise
            # open-endedly — CamillaUnavailable is a bare Exception (a DSP
            # wedge in load/restore escaped the previously-enumerated arms:
            # volume left active, relay session leaked, phone frozen at
            # capture_authorized), analyze/emit raise ValueError/RuntimeError,
            # the held measurement window raises MeasurementWindowError — so
            # ANY non-relay failure gets the same honest cleanup: tell the
            # phone (still polling capture_result), persist a terminal
            # failure, drain the volume (whose hook also releases the session
            # measurement pause), purge the relay session, then RE-RAISE so
            # the outer relay net still logs and flips /status.relay to
            # failed. Program-side classes keep their own honest code via
            # ``classify_program_failure`` (issue #1820: a not-confirmed safety
            # profile is NOT the same failure as a level ceiling, and the
            # underlying refusal slugs ride out with it); everything else is
            # internal_error.
            classified = _host.classify_program_failure(exc)
            code = classified[0] if classified else REASON_INTERNAL_ERROR
            refusals = classified[1] if classified else ()
            if classified:
                log_event(
                    logger,
                    "correction.crossover_v2_program_failure",
                    level=logging.WARNING,
                    code=code,
                    refusals=",".join(refusals),
                    error_type=type(exc).__name__,
                    # confirm_graph_is_live's three failures share a type and
                    # a code; this is what distinguishes them (panel nit).
                    detail=str(exc),
                )
            await _post_terminal_failure_host_event(code)
            _host._persist_terminal_failure(conductor, code, refusals=refusals)
            await _abandon_best_effort()
            # Finding H: give the just-posted terminal host event a bounded
            # grace window to reach the phone before the session is purged
            # out from under its next poll. Volume restore above stays
            # immediate — only the purge waits.
            await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
            await _purge_best_effort()
            raise
        # Plan finished without a transport failure.
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
                    session_id=pi_session.session_id,
                    component="volume_close",
                    error_type=type(exc).__name__,
                )
            else:
                log_event(
                    logger,
                    "correction.crossover_v2_cleanup_complete",
                    session_id=pi_session.session_id,
                    component="volume_close",
                )
        else:
            await _abandon_best_effort()
        await _purge_best_effort()

    return _run_and_consume


#: Headroom added to a remote stage's own wall-clock ceiling when its relay link
#: is minted (issue #2509).
#:
#: The ceiling bounds the WALK. The link has to outlive it, for two reasons that
#: both sit outside the walk: the TTL clock starts at mint, a few seconds before
#: the measurement volume opens and the ceiling's clock starts; and the ceiling
#: drains the volume rather than ending the session, so the final blob pull, its
#: analysis, and the purge all happen on the far side of it.
#:
#: 300 s is deliberately coarse — nothing has timed those tails, and this is a
#: BUDGET ALLOWANCE, not a measurement. It is chosen at the same magnitude as
#: the longest single pause the flow already admits (``V2_FIRST_BEGIN_TIMEOUT_S``
#: — a reference point, not a derivation), because being generous costs only a
#: dead link sitting in relay storage a few minutes longer than it had to.
REMOTE_RELAY_TTL_MARGIN_S = 300


def relay_link_ttl_s(plan_shape: Any, wall_clock_ceiling_s: float) -> int:
    """The relay link TTL a stage about to be minted should ask for (#2509).

    ``capture_relay.session.DEFAULT_TTL_S`` (900 s) is an ABSOLUTE clock —
    ``TIME_BUDGET_LINK``, counted from the mint and refreshed by nothing. A
    hand-walked stage finishes well inside it. A REMOTE stage does not fit: its
    own wall-clock ceiling is 1800 s (stage 1) / 2040 s (stage 2) at the shipped
    shape, and a single stalled position may spend
    ``REMOTE_POSITION_HOLD_BUDGET_S`` of that on its own. The first real
    remote run died at ~890 s with the phone still posting, on a 404 from a link
    that had run out under it (issue #2509).

    So a remote stage sizes its link from the ceiling it is already arming —
    ``wall_clock_ceiling_s``, the caller's own
    :func:`~jasper.active_speaker.crossover_v2_flow.session_wall_clock_ceiling_s`
    value, passed in rather than recomputed so the link and the volume can never
    describe different sessions — plus :data:`REMOTE_RELAY_TTL_MARGIN_S`, and
    clamped at what the Worker grants.

    Hand-walked shapes (and the tier-less recovery re-arm, ``plan_shape=None``)
    keep the default, which is the scope of the observed failure: no
    hand-walked run has been observed to reach 900 s. This is the seam a
    hand-walked shape would be widened at.
    """
    from jasper.capture_relay.session import DEFAULT_TTL_S, MAX_TTL_S

    if plan_shape is None or not plan_shape.externally_positioned:
        return DEFAULT_TTL_S
    return min(
        MAX_TTL_S, math.ceil(wall_clock_ceiling_s) + REMOTE_RELAY_TTL_MARGIN_S
    )
