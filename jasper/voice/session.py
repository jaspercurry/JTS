from __future__ import annotations

from typing import AsyncIterator, Callable, Protocol, runtime_checkable

from ..tools import ToolRegistry


@runtime_checkable
class LiveTurn(Protocol):
    """A single conversational turn within a long-lived voice connection.

    The daemon acquires a turn from a `LiveConnection` on wake, streams
    user audio frames into it, awaits the model's response, and releases
    the turn when idle. The connection itself stays open across turns
    (see `LiveConnection`); a turn is just the slice of activity between
    `activity_start` and `activity_end`.
    """

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        ...

    async def end_input(self) -> None:
        """Mark end-of-user-speech for this turn (sends `activity_end`).

        Idempotent — calling twice is a no-op."""
        ...

    def audio_out(self) -> AsyncIterator[bytes]:
        """Yield TTS audio chunks (24 kHz mono int16 PCM) until the turn
        is released or the connection drops."""
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

    def last_chunk_played_at(self) -> float:
        """Loop time when the playback consumer last DEQUEUED an audio
        chunk via ``audio_out()``. Distinct from ``last_chunk_at()``,
        which is when the chunk was RECEIVED from the server.

        The two timestamps can diverge by several seconds. OpenAI
        Realtime delivers all of a response's audio chunks back-to-back
        over the WebSocket — typically faster than real-time — while
        the consumer drains them at real-time playback rate via ALSA.
        For deciding when audio playback is fully drained (the
        daemon's idle-watchdog tail wait), this is the correct signal.
        Using the network-arrival anchor instead ends the turn 1.5 s
        after chunks STOPPED ARRIVING, well before the consumer has
        finished playing them — abandoning the queue tail and audibly
        cutting off the model mid-sentence.

        Returns 0.0 if the consumer has not dequeued any chunks yet."""
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

    def usage_tokens(self) -> dict[str, int]:
        """Latest cumulative usage_metadata observed during this turn.
        Note: Gemini Live's usage counters are session-cumulative, not
        per-turn — values here reflect the connection's lifetime usage
        as of the last server message processed in this turn."""
        ...

    def usage_breakdown(self) -> "dict | None":
        """Provider-specific token-detail breakdown if available, else
        None. The OpenAI Realtime adapter populates this from each
        ``response.done`` event's ``response.usage`` object so the
        spend cap can split by modality (audio / text / cached input
        priced at $32 / $4 / $0.40 per million tokens respectively).
        Gemini Live doesn't surface a modality breakdown and returns
        None — the spend cap then falls back to the scalar all-audio
        estimate, which matches the historical behaviour.

        Shape when populated:
          ``{"input_tokens": int, "output_tokens": int,``
          `` "input_token_details": {"audio_tokens": int,``
          ``                         "text_tokens": int,``
          ``                         "cached_tokens": int,``
          ``                         "cached_tokens_details": {...}},``
          `` "output_token_details": {"audio_tokens": int,``
          ``                          "text_tokens": int}}``
        """
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

    def audio_chunks_pending(self) -> int:
        """How many audio chunks are queued waiting for the playback
        consumer to dequeue. The idle watchdog reads this to defer its
        tail-timer firing while there's still work to play — without it,
        a single tts.write that blocks longer than the tail timeout
        looks indistinguishable from "audio finished" and the turn ends
        mid-playback. Adapters that don't track this can return 0; the
        watchdog falls back to its dequeue-timestamp heuristic, which is
        correct for providers whose chunks arrive at real-time rate."""
        ...

    def interrupted(self) -> bool:
        """True if the model reported being interrupted by user audio.
        Cleared by clear_interrupted() once the daemon has flushed
        playback in response."""
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


@runtime_checkable
class LiveConnection(Protocol):
    """Provider-agnostic interface for a long-lived voice connection.

    One instance per daemon: opened at startup, kept alive for the
    daemon's lifetime via the provider's session-resumption mechanism,
    closed at shutdown. Internally manages reconnection, keepalive, and
    context-reset on long idle gaps.

    v1 ships one implementation (Gemini Live). Future providers
    (OpenAI Realtime, etc) plug in by writing another adapter against
    this Protocol — daemon code (voice_daemon.py) imports only this
    interface and `LiveTurn`.
    """

    async def start(
        self,
        registry: ToolRegistry,
        system_instruction: "str | Callable[[], str]",
    ) -> None:
        """Open the connection and start the background tasks
        (receive loop, keepalive, reconnect supervisor). Returns once
        the initial handshake is complete or raises if the initial
        connect fails after retries.

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
        """True if the connection is currently in a backoff/failed state
        and cannot accept turns. The daemon's wake handler can check
        this before paying the cost of opening a turn (so wake events
        during a known-down period are a clean no-op)."""
        ...


@runtime_checkable
class VoiceSession(Protocol):
    """Legacy provider-agnostic interface for a bidirectional voice session.

    Predates the persistent-connection rework — represents a single
    open-then-close-per-wake session. New code should use `LiveConnection`
    + `LiveTurn` instead. Kept here so any out-of-tree consumers don't
    immediately break.
    """

    async def connect(self, registry: ToolRegistry, system_instruction: str) -> None:
        ...

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        ...

    async def end_input(self) -> None:
        ...

    def audio_out(self) -> AsyncIterator[bytes]:
        ...

    async def close(self) -> None:
        ...

    def usage_tokens(self) -> dict[str, int]:
        ...

    def turn_count(self) -> int:
        """Return the number of completed model turns observed."""
        ...

    def last_activity_at(self) -> float:
        """Loop time (asyncio.get_event_loop().time()) of the most recent
        observed model activity — either an audio chunk or turn_complete.
        Returns the session-start time if neither has happened yet. The
        idle watchdog uses this so it doesn't kill a session while the
        model is still streaming TTS."""
        ...

    def last_chunk_at(self) -> float:
        """Loop time of the most recent audio chunk specifically (not
        tool calls / turn_complete). Used by the daemon's barge-in gate
        to detect when the model is currently producing TTS."""
        ...

    def bytes_sent(self) -> int:
        """Total bytes of audio sent to the server during this session.
        Used together with chunks_received() to detect the silent-failure
        mode where Gemini Live accepts the connection but never produces
        any output (quota exhausted, service degraded, etc)."""
        ...

    def chunks_received(self) -> int:
        """Total audio response chunks received from the server during
        this session."""
        ...

    def interrupted(self) -> bool:
        """True if the model reported being interrupted by user audio.
        Cleared by clear_interrupted() once the daemon has flushed
        playback in response."""
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
