# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Coroutine,
    Protocol,
    runtime_checkable,
)

from ..log_event import log_event
from ..tools import ToolRegistry


class ConnectionState(Enum):
    """Provider-neutral states for persistent live voice connections."""

    IDLE_INIT = "idle_init"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    IN_TURN = "in_turn"
    RECONNECTING = "reconnecting"
    PAUSED_FOR_BACKOFF = "paused_for_backoff"
    FAILED = "failed"
    CLOSED = "closed"


CONNECTION_NOISY_TRANSITIONS = frozenset({
    (ConnectionState.CONNECTED, ConnectionState.IN_TURN),
    (ConnectionState.IN_TURN, ConnectionState.CONNECTED),
})


@dataclass(frozen=True)
class AudioOutChunk:
    """Provider audio plus playout identity for fan-in flush accounting.

    `pcm` is 24 kHz mono int16 payload; see `LiveTurn.audio_out` for
    the plain-bytes view. The optional provider item id is the
    stable handle needed by provider-specific truncation later (for
    OpenAI, `response.output_item.added.item.id`). Providers that do not
    expose per-response item ids leave it unset; fan-in still accounts
    for the local segment and returns played duration on flush.
    """

    pcm: bytes
    provider_item_id: str | None = None
    kind: str = "assistant"


@dataclass(frozen=True)
class TurnUsage:
    """One turn's token usage, normalised to a PER-TURN count.

    Adapters normalise even when the provider reports differently, so
    callers may SUM across turns without multi-counting: OpenAI Realtime
    sends per-response deltas (summed within the turn); Gemini Live sends
    a counter cumulative for the WebSocket's lifetime, so its adapter
    subtracts the baseline captured at turn start.

    `breakdown` is the provider's modality split in the rich form
    `usage.UsageStore.close_session` accepts, so the spend cap can price
    audio / text / cached input separately. None where the provider
    exposes no split (Gemini Live) — the cap then prices the two scalars
    as all-audio.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    breakdown: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnCapture:
    """What a finished turn offers conversation history.

    Providers differ in what they can offer, not in how it is written:
    OpenAI/Grok carry both transcripts, Gemini carries none and offers
    bounded `data` metadata instead. Adapters normalise blank text to
    None, so a None field was not captured rather than captured empty.
    See docs/conversation-history-plan.md.
    """

    user_text: str | None = None
    assistant_text: str | None = None
    data: dict[str, object] | None = None


@runtime_checkable
class Interruptible(Protocol):
    """A turn JTS can cut off mid-sentence and reconcile afterwards.

    Capability-based, never provider-name-based: each catalog provider
    declares a `catalog.InterruptReconcile` kind and implements this
    Protocol to match, so a `server_self_truncates` provider (Gemini)
    ships honest no-ops rather than being special-cased at the call site.
    See ADR-0115.
    """

    def request_local_interrupt(self) -> None:
        """Locally signal a user barge-in WITHOUT telling the provider.

        Sets the same interrupt event :meth:`LiveTurn.wait_for_interrupt`
        resolves on, so the playback path flushes local TTS immediately.
        This is the provider-agnostic *detection + flush* spine: it
        deliberately does NOT truncate or cancel the provider's in-flight
        response — ``cancel_response`` / ``truncate_assistant_audio`` own
        that."""
        ...

    def drop_pending_audio(self) -> int:
        """Drop assistant audio buffered for playback but not yet written,
        returning the number of chunks dropped.

        A local flush clears the DAC ring (~one write), but burst-delivery
        providers (OpenAI/Grok) enqueue the whole response's audio up
        front, so without dropping it the playback loop resumes writing the
        backlog and the assistant audibly talks over the user.
        Implementations drain their playout queue while PRESERVING any
        terminal end-of-audio sentinel, so the consumer still ends the
        turn. Idempotent; must never raise."""
        ...

    def audio_chunks_pending(self) -> int:
        """How many audio chunks the playout queue still holds — the depth
        ``drop_pending_audio`` would drain.

        The idle watchdog reads this to defer its tail-timer firing while
        there's still work to play: without it, a single tts.write that
        blocks longer than the tail timeout looks indistinguishable from
        "audio finished" and the turn ends mid-playback."""
        ...

    async def cancel_response(self, reason: str) -> None:
        """Explicitly tell the provider to stop generating the in-progress
        response for this turn — the *local/manual* cancel path.

        Called when JTS itself decides to stop the model: a barge-in the
        provider's own VAD did not initiate, a push-to-talk release, an
        operator/manual interrupt. It maps to the provider's "stop now"
        control where one exists (OpenAI/Grok ``response.cancel``).

        This is the inverse direction of ``LiveTurn.wait_for_interrupt()``
        / ``LiveTurn.clear_interrupted()``: those observe a
        provider-*reported* interruption; ``cancel_response`` is JTS
        telling the provider to stop, not the provider telling JTS it
        stopped. Local TTS flush is a separate daemon-layer step that does
        not depend on this call — cancelling provider generation never
        makes the already-queued DAC audio stop on its own.

        ``reason`` is for the structured-log line only. Must be idempotent
        and must never raise on an already-complete or absent response.
        Providers with no client cancel mechanism (Gemini) implement this
        as a no-op."""
        ...

    async def truncate_assistant_audio(
        self, provider_item_id: str | None, audio_played_ms: int,
    ) -> None:
        """Align the provider's conversation history to what the listener
        actually heard, after a barge-in cut local playback short. See
        ADR-0115.

        ``provider_item_id`` MUST be tolerated as ``None`` — the normal
        value on every call for a ``server_self_truncates`` provider
        (Gemini), and possible transiently for a ``needs_client_truncate``
        provider (OpenAI/Grok) before the turn's first audio item. Must be
        idempotent and must never raise."""
        ...


@runtime_checkable
class LiveTurn(Interruptible, Protocol):
    """A single conversational turn within a long-lived voice connection.

    The daemon acquires a turn from a `LiveConnection` on wake, streams
    user audio frames into it, awaits the model's response, and releases
    the turn when idle. The connection itself stays open across turns
    (see `LiveConnection`); a turn is just the slice of activity between
    `activity_start` and `activity_end`.
    """

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        ...

    async def send_text_context(self, text: str) -> None:
        """Add a text-only context item to the current turn without
        asking the provider to generate yet.

        Used for narrow daemon-initiated confirmation windows where the
        model needs one-shot routing context before live user audio. The
        normal wake path does not call this."""
        ...

    async def end_input(self) -> None:
        """Mark end-of-user-speech for this turn (sends `activity_end`).

        Idempotent — calling twice is a no-op."""
        ...

    def audio_out(self) -> AsyncIterator[bytes]:
        """Yield TTS audio chunks (24 kHz mono int16 PCM) until the turn
        is released or the connection drops."""
        ...

    def audio_out_chunks(self) -> AsyncIterator[AudioOutChunk]:
        """Yield TTS chunks with optional provider item identity."""
        ...

    async def release(self) -> None:
        """Release the turn back to the connection. Idempotent. Sends
        `activity_end` if it hasn't been sent yet, drains the playback
        queue, and removes the turn from the connection's active slot
        so a subsequent `acquire_turn()` can succeed."""
        ...

    def last_activity_at(self) -> float:
        """Loop time (asyncio.get_event_loop().time()) of the most recent
        observed model activity for this turn — either an audio chunk or
        turn_complete. Returns the turn-start time if neither has happened
        yet. The idle watchdog uses this so it doesn't kill a turn while
        the model is still streaming TTS."""
        ...

    def last_chunk_at(self) -> float:
        """Loop time of the most recent audio chunk specifically (not
        tool calls / turn_complete). Used by the daemon's barge-in gate
        to detect when the model is currently producing TTS."""
        ...

    def bytes_sent(self) -> int:
        """Total bytes of audio sent to the server during this turn.
        Used together with chunks_received() to detect the silent-failure
        mode where Gemini Live accepts the connection but never produces
        any output (quota exhausted, service degraded, etc)."""
        ...

    def chunks_received(self) -> int:
        """Total audio response chunks received from the server during
        this turn."""
        ...

    def usage(self) -> TurnUsage:
        """This turn's token usage. See `TurnUsage`."""
        ...

    def capture(self) -> TurnCapture | None:
        """What this turn offers conversation history, or None when the
        provider exposes nothing to record. See `TurnCapture`."""
        ...

    def turn_lost(self) -> bool:
        """True if the underlying connection dropped mid-turn (e.g. the
        WebSocket closed, GoAway timed out before audio finished). The
        daemon should treat this like "turn ended" but log the loss."""
        ...

    def server_turn_complete(self) -> bool:
        """True once the server has emitted server_content.turn_complete
        for this turn — the canonical 'model is done speaking' signal.
        The daemon's idle watchdog uses this to close the turn promptly
        without racing mid-response chunk gaps that look like idleness."""
        ...

    async def wait_for_interrupt(self) -> None:
        """Resolve when the model signals the user interrupted its speech.
        Used by the playback path to race write-current-chunk against
        flush-immediately."""
        ...

    def clear_interrupted(self) -> None:
        """Reset the interrupted flag/event after the playback path has
        flushed its output in response."""
        ...

    def _on_connection_lost(self) -> None:
        """The connection dropped while this turn was active: mark the
        turn lost and end its audio stream. Called by the supervisor, not
        by the daemon. Idempotent."""
        ...


# ``WakeLoop.play_supervisor_cue`` in production: takes a cue slug.
CuePlayer = Callable[[str], Coroutine[Any, Any, object]]


@runtime_checkable
class LiveConnection(Protocol):
    """Provider-agnostic interface for a long-lived voice connection.

    One instance per daemon: opened at startup, kept alive for the
    daemon's lifetime via the provider's session-resumption mechanism,
    closed at shutdown. Internally manages reconnection (including any
    rotation the provider's session cap forces) and context-reset on
    long idle gaps.

    Three implementations ship today: Gemini Live, OpenAI Realtime, and
    Grok (a thin OpenAI subclass). A fourth provider plugs in by writing
    another adapter against this Protocol — daemon code imports only
    this interface and `LiveTurn`.
    """

    async def start(
        self,
        registry: ToolRegistry,
        system_instruction: "str | Callable[[], str]",
    ) -> None:
        """Open the connection and start the background tasks
        (receive loop, reconnect supervisor). Returns once
        the initial handshake completes, or — when the provider rejects
        it terminally — returns with the connection paused
        (``is_paused()`` True, ``last_failure_detail()`` set) and the
        supervisor still retrying. Raises only when a transient
        initial-connect retry budget is exhausted.

        `system_instruction` may be a fixed string or a callable
        producing one — implementations should call the callable on
        every (re)connect so dynamic content (e.g. current local time)
        stays fresh across the connection's lifetime."""
        ...

    async def stop(self) -> None:
        """Gracefully close the connection and stop all background
        tasks. Idempotent."""
        ...

    async def acquire_turn(self) -> LiveTurn:
        """Acquire a fresh turn within the current connection. May block
        briefly while a reconnect or context-reset completes. Raises if
        the connection is in a terminal `failed` state."""
        ...

    def is_paused(self) -> bool:
        """True while the connection cannot accept turns: the first
        connect is still dialling, or a reconnect is in backoff, or the
        provider rejected us terminally. The daemon's wake handler
        checks this before paying the cost of opening a turn, and cues
        rather than opening one."""
        ...

    def last_failure_detail(self) -> str | None:
        """Why the last reconnect failed, or None while healthy.

        Provider-agnostic and already redacted — see
        ``_supervisor.failure_detail``. Surfaced at
        ``/state.voice.connection_error``."""
        ...

    def wake_cue(self) -> str:
        """The cue a wake plays while this connection is paused: the
        remedy for a terminal outage, else the generic ``cant_connect``."""
        ...

    def request_reconnect_now(self) -> bool:
        """Ask a paused connection to stop waiting and retry now.

        The daemon calls this wherever it refuses a wake for
        ``is_paused()``: during a long terminal poll the wake word is
        the household asking whether the outage is over. Implementations
        rate-gate it so repeated wakes cannot outpace the ordinary
        reconnect ramp. Returns whether a retry was actually asked for."""
        ...

    def set_failure_escalation_cb(self, cb: CuePlayer | None) -> None:
        """Wire the cue player for a terminal connection failure. The
        daemon calls this once the ``WakeLoop`` exists."""
        ...


def log_first_chunk(
    logger: logging.Logger,
    provider: str,
    *,
    turn_start_monotonic: float,
    end_input_monotonic: float,
) -> None:
    """Emit ``event=turn.first_chunk`` for a turn's first assistant audio.

    ``since_end_input_ms`` is the provider's own latency — the interval
    between asking for a response and the first audio of it. It is omitted
    when this turn was never asked (0.0), which is the only honest answer:
    there is no interval to report. ``since_turn_start_ms`` spans the user's
    whole utterance plus local endpointing, so it is not a provider number.
    """
    now = time.monotonic()
    fields: dict[str, Any] = {"provider": provider}
    if end_input_monotonic:
        fields["since_end_input_ms"] = int((now - end_input_monotonic) * 1000)
    fields["since_turn_start_ms"] = int((now - turn_start_monotonic) * 1000)
    log_event(logger, "turn.first_chunk", **fields)
