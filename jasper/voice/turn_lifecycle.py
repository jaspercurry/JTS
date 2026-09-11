# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's turn: what is open, and how it ends.

Owns the in-flight turn — the provider turn object, the wake/session
state, the usage session id, the background response tasks — and the
whole teardown: cancel the background work, record the outcome to usage
and wake telemetry, release the provider session off the closure path,
journal a no-answer turn and cue it.

`WakeLoop` builds one at construction time, writes the owned attributes
directly when the loop causes the change, and reads them for
`session_status`. The loop-side reads the teardown needs are
injected as late-bound callables so a test rebinding `WakeLoop._play_cue`
still intercepts the teardown cue.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum

from jasper.log_event import log_event

from ..usage import UsageStore
from ._tasks import cancel_tracked_tasks
from .assistant_output import (
    INTERNAL_ERROR_CUE_SLUG,
    AssistantOutput,
    await_output_cleanup_owned,
    capture_cleanup_error,
)
from .content_activity import ContentActivityTracker
from .conversation_capture import ConversationCapture
from .output_gate import AssistantOutputEpisode
from .peering_client import PeeringClient
from .push_to_talk import PushToTalk
from .session import LiveConnection, LiveTurn
from .turn_playback import PRE_RESPONSE_CAPPED_REASON, PlaybackReport
from .turn_timeline import TurnTimeline
from .wake_telemetry import WakeTelemetry

logger = logging.getLogger("jasper.voice_daemon")

# `end` reasons the household or the daemon itself chose: whoever muted,
# shut down or spoke over the turn already knows why it went quiet, so no
# failure cue is owed however little the model said.
NO_ANSWER_CUE_SUPPRESSED_REASONS = frozenset({
    "mic_muted",
    "stopping",
    "barge_in",
    "conversation_ended",
    "measurement_active",
})


class InputAdmissionClosed(RuntimeError):
    def __init__(self, result: str) -> None:
        self.result = result
        super().__init__(result)


class State(Enum):
    WAKE = "wake"
    SESSION = "session"


class TurnLifecycle:
    """The in-flight turn's state, and every path that ends one."""

    def __init__(
        self,
        assistant_output: AssistantOutput,
        connection: LiveConnection,
        content_activity: ContentActivityTracker,
        usage_store: UsageStore,
        push_to_talk: PushToTalk,
        *,
        timeline: TurnTimeline,
        peering: PeeringClient,
        wake_telemetry: WakeTelemetry,
        conversation_capture: ConversationCapture,
        spawn: Callable[..., "asyncio.Task"],
        arm_refractory: Callable[[], None],
        play_cue: Callable[[str], Awaitable[bool]],
        mic_muted: Callable[[], bool],
    ) -> None:
        self._output = assistant_output
        self._connection = connection
        self._content_activity = content_activity
        self._usage_store = usage_store
        self._push_to_talk = push_to_talk
        self._timeline = timeline
        self._peering = peering
        self._telemetry = wake_telemetry
        self._conversation_capture = conversation_capture
        self._spawn = spawn
        self._arm_refractory = arm_refractory
        self._play_cue = play_cue
        self._mic_muted = mic_muted

        self.turn: LiveTurn | None = None
        self.state: State = State.WAKE
        self.session_id: int | None = None
        # Re-entrancy guard for `end`. A bare flag, deliberately NOT an
        # early `state` flip — `state` must stay SESSION through the
        # teardown so output-stream gates hold.
        self.ending: bool = False
        # The previous turn's provider teardown, still in flight. The user's
        # closure never waits on it; the next acquire does, so one provider
        # session is open at a time.
        self.pending_release: asyncio.Task | None = None
        self.playback_report = PlaybackReport()
        self.bg_tasks: set[asyncio.Task] = set()
        self.output_episode: AssistantOutputEpisode | None = None
        self.conversation_end_requested = False
        self.barge_in_active: bool = False

        # End-of-utterance detection state (per-turn), written by the loop's
        # frame handlers. `audio_stream_end` MUST be sent the moment the
        # user stops speaking, not at turn cleanup: without it the server
        # stays in "listening for end of turn-1" and the next turn's audio
        # is silently swallowed. Silero gives per-frame speech probability;
        # consecutive silence after speech accumulates until it crosses the
        # threshold, then turn.end_input() sends the marker.
        self.user_speech_seen: bool = False
        self.input_ended: bool = False
        # Read on the asyncio loop clock to match what the silence detector
        # reads; anchors NO_SPEECH_ABORT_SEC, HARD_RECORDING_CAP_SEC and the
        # push-to-talk hold cap.
        self.started_at_loop: float = 0.0
        self.max_silero_aec: float = 0.0
        self.max_silero_raw: float = 0.0
        self.silero_aec_armed_at_ms: int | None = None
        self.silero_raw_armed_at_ms: int | None = None
        # Decided once per turn in `_begin_turn_inner`: true when this turn's
        # session audio comes from a push-to-talk source, so the button owns
        # both boundaries and local VAD must not become a second writer of
        # end-of-input.
        self.manual_endpoint_this_turn: bool = False
        self.continuous_speech_started = self.continuous_last_speech = 0.0

        # Turns since daemon start that were asked a question and produced no
        # answer. Published as /state.voice.silent_responses_session.
        self.silent_responses_session: int = 0
        # The subset of those the idle watchdog's pre-response cap released
        # (issue #4532). Published as /state.voice.turns_pre_response_capped;
        # the cap's removal condition reads it.
        self.turns_pre_response_capped: int = 0

        self._bg_end_scheduled: bool = False
        self._transition_lock = asyncio.Lock()

    def take_pending_release(self) -> "asyncio.Task | None":
        release, self.pending_release = self.pending_release, None
        return release

    def arm_background_end(self) -> None:
        """End the turn when a response/playback background task completes.

        The primary mic loop also checks ``bg_tasks`` on each session
        frame, but a manual source stops producing frames after button
        release, so teardown must be anchored to response completion
        rather than to a later button press tickling the frame loop.
        """
        self._bg_end_scheduled = False
        for task in self.bg_tasks:
            task.add_done_callback(self._on_turn_background_done)

    def background_end_reason(self) -> str | None:
        done = [task for task in self.bg_tasks if task.done()]
        failed = [task for task in done if not task.cancelled() and task.exception() is not None]
        if failed:
            return "playback_failed"
        if not done:
            return None
        # `idle_watchdog` names its own ending when it chose one; both
        # background tasks return None on the ordinary paths.
        chosen = [r for t in done if not t.cancelled() and isinstance(r := t.result(), str)]
        return chosen[0] if chosen else (self.playback_report.stop_reason or "ended")

    def _on_turn_background_done(self, task: asyncio.Task) -> None:
        if task not in self.bg_tasks:
            return
        if self.ending or self.turn is None or self._bg_end_scheduled:
            return
        self._bg_end_scheduled = True
        self._spawn(
            self.finish_response(self.background_end_reason() or "ended"),
            name="voice-turn-background-end",
        )

    def request_conversation_end(self) -> None:
        if self.turn is None:
            return
        self.conversation_end_requested = True
        self.turn.request_local_interrupt()
        self._spawn(self.end("conversation_ended"), name="conversation-end")

    async def finish_response(self, reason: str) -> None:
        if self.ending or self.turn is None:
            return
        if reason == "spend_cap_reached":
            await self.end(reason)
            await self._play_cue(reason)
            return
        await self.end("conversation_ended" if self.conversation_end_requested else reason)

    def endpointer_label(self) -> str:
        """Which mechanism closes the current turn's user input.

        One vocabulary, two readers: ``/state.voice.endpointer`` (live)
        and the wake-events ``endpointer`` column (at turn end). Kept in
        one place so a new endpointer can't be named two things.

        ``push_to_talk`` is only observable on the ``/state`` side today.
        The corpus row is created by ``begin_event`` on the wake path, and
        a button turn never takes that path, so it has no row to label —
        see ``corpus_endpointer_label``.
        """
        if self.manual_endpoint_this_turn:
            return "push_to_talk"
        continuous = self.turn is not None and self.turn.continuous_input
        return "continuous_audio" if continuous else "silero_aec"

    def corpus_endpointer_label(self, *, user_speech_seen: bool) -> str:
        """The wake-events ``endpointer`` value for the finished turn.

        Same vocabulary as ``endpointer_label`` plus ``no_speech_abort``,
        which is a verdict about *listening* and so only meaningful when
        something was listening for speech. Keyed on the resolved label so
        that if button turns ever gain corpus rows, one cannot be recorded
        as a no-speech abort it never performed.
        """
        label = self.endpointer_label()
        if label in {"silero_aec", "continuous_audio"} and not user_speech_seen:
            return "no_speech_abort"
        return label

    def reset(self) -> None:
        try:
            self._content_activity.resume()
        finally:
            self.turn = None
            self.session_id = None
            self.output_episode = None
            self.conversation_end_requested = False
            self.bg_tasks = set()
            self._bg_end_scheduled = False
            self._push_to_talk.active_source = None
            self.barge_in_active = False
            self.state = State.WAKE
            self._arm_refractory()

    def _log_no_answer(
        self,
        event: str,
        /,
        *,
        end_reason: str,
        counted: bool = False,
        **fields: object,
    ) -> bool:
        """Journal no-answer turns; suppress cues for deliberate endings."""
        suppressed = end_reason in NO_ANSWER_CUE_SUPPRESSED_REASONS
        if counted and not suppressed:
            self.silent_responses_session += 1
            fields["count"] = self.silent_responses_session
        log_event(
            logger,
            event,
            fields={
                "provider": self._output.cfg.voice_provider,
                "model": self._output.cfg.active_voice_model,
                **fields,
                **({"suppressed": end_reason} if suppressed else {}),
            },
            level=logging.INFO if suppressed else logging.WARNING,
        )
        return not suppressed

    async def end(self, reason: str = "ended") -> None:
        if self.ending:
            return
        async with self._transition_lock:
            await self._end_turn_owned(reason)

    async def _end_turn_owned(self, reason: str) -> None:
        # SESSION must cover the chirp and refusal cue so neither wakes itself.
        if self.ending or self.turn is None:
            return
        self.ending = True
        self.turn.discard_input()
        try:
            await await_output_cleanup_owned(
                self._end_turn_inner(reason), task_name="turn-end-cleanup",
            )
        finally:
            self.ending = False

    async def _end_turn_inner(self, reason: str = "ended") -> None:
        episode = self.output_episode
        play_no_answer_cue = False
        try:
            # IPC cancellation can confirm an accepted prefix during the join.
            await cancel_tracked_tasks(set(self.bg_tasks))
            if reason not in NO_ANSWER_CUE_SUPPRESSED_REASONS:
                reason = self.background_end_reason() or reason
            self.bg_tasks.clear()
            play_no_answer_cue = reason == "playback_failed"
            play_no_answer_cue = await self._record_and_release_turn(reason, episode)
        finally:
            try:
                await self._output.finish_turn_episode(
                    episode, completed=reason != "playback_failed",
                )
                self.barge_in_active = False
                if play_no_answer_cue:
                    # A paused connection owns its remedy cue. Keep SESSION
                    # through its drain so the cue cannot wake the detectors.
                    error = await capture_cleanup_error(lambda: self._play_cue(
                        self._connection.wake_cue()
                        if reason != "playback_failed" and self._connection.is_paused()
                        else INTERNAL_ERROR_CUE_SLUG
                    ))
                    if isinstance(error, Exception):
                        logger.warning("teardown no-answer cue failed: %s", error)
                    elif error is not None:
                        raise error
            finally:
                self.reset()

    def _reply_lost(self) -> bool:
        assert self.turn is not None
        return self.turn.turn_lost() and not self.turn.server_turn_complete()

    async def _record_turn_outcome(self, reason: str) -> None:
        failed = reason == "playback_failed" or self._reply_lost()
        capped = reason == PRE_RESPONSE_CAPPED_REASON
        if capped:
            self.turns_pre_response_capped += 1
        self._timeline.emit(
            "failed" if failed else PRE_RESPONSE_CAPPED_REASON if capped else "complete",
        )
        if not failed:
            await self._telemetry.stage("turn_complete")
        # Capture event_id BEFORE the outcome write clears it.
        session_vad_eid = self._telemetry.current_event_id
        terminal_outcome = (
            "session_failed" if failed else "completed" if self.user_speech_seen else "no_speech"
        )
        await self._telemetry.outcome(terminal_outcome, reason)

        if session_vad_eid is not None:
            await self._telemetry.record_session_vad(
                session_vad_eid,
                max_silero_aec=self.max_silero_aec or None,
                max_silero_raw=self.max_silero_raw or None,
                silero_aec_armed_at_ms=self.silero_aec_armed_at_ms,
                silero_raw_armed_at_ms=self.silero_raw_armed_at_ms,
                endpointer=self.corpus_endpointer_label(
                    user_speech_seen=self.user_speech_seen,
                ),
                music_playing_at_turn=self._content_activity.music_is_playing(),
                music_db_at_turn=self._content_activity.music_dbfs,
            )

    async def _release_turn(self, turn: LiveTurn) -> None:
        """Tear the provider turn down off the closure path, and time it.

        The user's chirp, duck restore and return to wake listening run
        while this is in flight; `_begin_turn` is what waits for it.
        """
        started = time.monotonic()
        error = await capture_cleanup_error(turn.release)
        fields: dict[str, object] = {
            "provider": self._output.cfg.voice_provider,
            "ms": int((time.monotonic() - started) * 1000),
        }
        if error is not None:
            fields["exc_type"] = type(error).__name__
        log_event(
            logger, "turn.release", fields=fields,
            level=logging.WARNING if isinstance(error, Exception) else logging.INFO,
        )
        if error is not None and not isinstance(error, Exception):
            raise error

    async def _release_and_capture(
        self, turn: LiveTurn, *, session_id: int, mic_muted: bool,
    ) -> None:
        """Release the turn, then persist what it transcribed.

        Transcript deltas keep arriving through the provider's close
        handshake, so a capture read before `release()` returns loses the
        tail of the assistant's line. The turn's own session id and mute
        state are passed in: `reset` clears them, and the next turn
        opens its session before it awaits this task.
        """
        try:
            await self._release_turn(turn)
        finally:
            try:
                captured = turn.capture()
            except (RuntimeError, TypeError, ValueError) as exc:
                log_event(
                    logger,
                    "turn.capture_failed",
                    exc_type=type(exc).__name__,
                    level=logging.WARNING,
                )
                captured = None
            if captured is not None:
                self._conversation_capture.record(
                    captured.user_text,
                    captured.assistant_text,
                    data_json=captured.data,
                    session_id=session_id,
                    mic_muted=mic_muted,
                )

    async def _record_and_release_turn(
        self, reason: str, episode: AssistantOutputEpisode | None,
    ) -> bool:
        drain_wait_sec: float | None = None
        if self.turn is not None and (last_chunk_at := self.turn.last_chunk_at()) > 0:
            # From the last audio chunk, not the last activity of any kind:
            # the anchor also moves on transcript deltas and turn_complete,
            # which arrive after the audio and would shorten the tail.
            drain_wait_sec = max(0.0, time.monotonic() - last_chunk_at)
        turn = self.turn
        assert turn is not None
        phases: list[tuple[str, Callable[[], object]]] = [
            ("turn_outcome", lambda: self._record_turn_outcome(reason)),
            ("peering_end", lambda: self._peering.session_ended(reason)),
        ]
        async def end_segment() -> None:
            if episode is not None and self._output.gate.is_current(episode):
                try:
                    # Endings where the queued tail must not reach the room:
                    # output already failed, or the user asked us to stop.
                    if reason in ("playback_failed", "conversation_ended"):
                        await self._output.tts.flush()
                finally:
                    await self._output.tts.end_segment()

        phases.append(("end_segment", end_segment))
        if self.input_ended or self.user_speech_seen or self.manual_endpoint_this_turn:
            phases.append(("end_input", lambda: asyncio.wait_for(turn.end_input(), timeout=2.0)))
        cleanup_base_error: BaseException | None = None
        for phase, operation in phases:
            error = await capture_cleanup_error(operation)
            if isinstance(error, Exception):
                log_event(
                    logger, "turn.cleanup_phase_failed", phase=phase,
                    exc_type=type(error).__name__, err=str(error), level=logging.WARNING,
                )
            elif cleanup_base_error is None:
                cleanup_base_error = error
        session_id = self.session_id
        assert session_id is not None
        self.pending_release = self._spawn(
            self._release_and_capture(
                turn,
                session_id=session_id,
                mic_muted=self._mic_muted(),
            ),
            name="turn-release",
        )

        play_no_answer_cue = False
        usage = turn.usage()
        cost = self._usage_store.close_session(
            session_id,
            usage.input_tokens,
            usage.output_tokens,
            usage=usage.breakdown,
        )
        bytes_sent = turn.bytes_sent()
        chunks_received = turn.chunks_received()
        lost_mid_reply = self._reply_lost()
        silent = not self.playback_report.accepted_audio and not turn.turn_lost()
        if reason == "playback_failed":
            play_no_answer_cue = self._log_no_answer(
                "turn.output_failed",
                end_reason=reason,
                counted=not self.playback_report.accepted_audio,
                accepted_audio=self.playback_report.accepted_audio,
                chunks_received=chunks_received,
                bytes_sent=bytes_sent,
            )
        elif bytes_sent == 0:
            self._log_no_answer(
                "turn.silent_response",
                end_reason=reason,
                reason="no_audio_sent",
                bytes_sent=bytes_sent,
                chunks_received=chunks_received,
                turn_lost=lost_mid_reply,
                endpointer=self.endpointer_label(),
            )
        elif bytes_sent > 0 and (silent or lost_mid_reply):
            model = self._output.cfg.active_voice_model
            if self.input_ended:
                diagnosis: dict[str, object] = (
                    {}
                    if reason in NO_ANSWER_CUE_SUPPRESSED_REASONS
                    else {
                        "reason": reason,
                        "bytes_sent": bytes_sent,
                        "endpointer": self.endpointer_label(),
                    }
                )
                play_no_answer_cue = self._log_no_answer(
                    "turn.silent_response",
                    end_reason=reason,
                    counted=True,
                    **diagnosis,
                    chunks_received=chunks_received,
                    turn_lost=lost_mid_reply,
                )
            elif silent and self.manual_endpoint_this_turn:
                log_event(
                    logger,
                    "turn.silent_response",
                    provider=self._output.cfg.voice_provider,
                    model=model,
                    reason="hold_timeout",
                    bytes_sent=bytes_sent,
                    chunks_received=chunks_received,
                    turn_lost=lost_mid_reply,
                    idle_timeout_sec=float(self._output.cfg.idle_timeout_sec),
                    endpointer=self.endpointer_label(),
                    level=logging.WARNING,
                )
            elif silent:
                log_event(
                    logger,
                    "turn.silent_response",
                    provider=self._output.cfg.voice_provider,
                    model=model,
                    reason="recording_timeout",
                    bytes_sent=bytes_sent,
                    chunks_received=chunks_received,
                    turn_lost=lost_mid_reply,
                    endpointer=self.endpointer_label(),
                    level=logging.WARNING,
                )
            else:
                play_no_answer_cue = self._log_no_answer(
                    "turn.silent_response",
                    end_reason=reason,
                    counted=True,
                    reason="connection_lost",
                    bytes_sent=bytes_sent,
                    chunks_received=chunks_received,
                    turn_lost=lost_mid_reply,
                    endpointer=self.endpointer_label(),
                )
        elif bytes_sent > 0 and turn.audio_dropped_bytes() > 0:
            play_no_answer_cue = self._log_no_answer(
                "turn.truncated_response",
                end_reason=reason,
                dropped_bytes=turn.audio_dropped_bytes(),
                chunks_received=chunks_received,
                endpointer=self.endpointer_label(),
            )
        drain_part = (
            f", drain wait {drain_wait_sec:.2f}s"
            if drain_wait_sec is not None else ""
        )
        paced_sec = self._output.tts.take_paced_sec()
        paced_part = f", paced {paced_sec:.2f}s" if paced_sec > 0.05 else ""
        logger.info(
            "turn ended: in=%d out=%d tokens, est $%.4f "
            "(sent=%dB, recv=%d chunks%s%s%s)",
            usage.input_tokens, usage.output_tokens, cost,
            bytes_sent, chunks_received, drain_part,
            paced_part,
            ", turn_lost" if turn.turn_lost() else "",
        )

        if cleanup_base_error is not None:
            raise cleanup_base_error
        return play_no_answer_cue
