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

    Adapters normalise provider reports so callers may sum across turns
    without counting repeated usage snapshots twice. Retained context
    billed again on a later turn remains part of that turn's input count.

    `breakdown` is the provider's modality split in the rich form
    `usage.UsageStore.close_session` accepts, so the spend cap can price
    audio / text / cached input separately. When no split is supplied,
    the cap prices the two scalars as all-audio.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    breakdown: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnCapture:
    """What a finished turn offers conversation history.

    Adapters normalise absent or blank transcripts to None. Metadata must
    not substitute generated text for an absent native transcript.
    """

    user_text: str | None = None
    assistant_text: str | None = None
    data: dict[str, object] | None = None


@runtime_checkable
class Interruptible(Protocol):
    """A turn JTS can cut off mid-sentence and reconcile afterwards.

    Capability-based, never provider-name-based: each catalog provider
    declares a `catalog.InterruptReconcile` kind and implements this
    Protocol to match. Gemini interrupts generation through manual activity
    but has no API for trimming history to JTS playback counts.
    See ADR-0115.
    """

    # True when the provider stops generating on the user's own voice, so the
    # host must never issue its own barge-in flush for this turn: the local
    # detector scores the assistant's echo as well as the user, and flushing
    # the fan-in lane on that chops the reply mid-word. The host still
    # forwards mic audio and still honours conversation end.
    # `_base.BaseLiveTurn` carries the default; an adapter whose provider owns
    # acoustic interruption overrides it.
    owns_interruption: bool

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

        The idle watchdog measures playout PROGRESS on this depth ALONE
        while it is nonzero, and consults the drain deadline only once it
        reaches zero, ending the turn when neither has moved for
        ``response_stall_timeout``. See ADR-0254."""
        ...

    def audio_dropped_bytes(self) -> int:
        """Assistant audio this turn never queued because the playout
        queue was already at its byte ceiling — a wedged consumer, not a
        barge-in (which drops through ``drop_pending_audio``). Non-zero
        means the reply was truncated at the tail."""
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
        Gemini uses a manual activity-start marker to interrupt generation."""
        ...

    async def truncate_assistant_audio(
        self, provider_item_id: str | None, audio_played_ms: int,
    ) -> None:
        """Trim an owned item to its confirmed local ledger boundary in ms.

        Zero is a valid boundary; an absent item is not. Local drain counts
        are estimates, not acoustic proof. Gemini has no item truncate API.
        Must tolerate None and never raise on an absent or finished response.
        """
        ...


@runtime_checkable
class LiveTurn(Interruptible, Protocol):
    """An acquired voice exchange, or a continuous wake conversation.

    The daemon acquires a turn from a `LiveConnection` on wake, streams
    user audio frames into it, awaits the model's response, and releases
    the turn when idle. The connection itself stays open across turns
    (see `LiveConnection`). Adapters declaring `continuous_input = True`
    retain this object through follow-ups; `backend_pending` reports delegated
    work and `discard_input()` synchronously revokes buffered microphone audio.
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
        """Release this turn and its pending input/output. Idempotent.

        An abandoned response must not reach a later turn. The adapter
        clears input or reopens the session before another turn can start.
        Pending tool work is cancelled without waiting for its executor;
        its results are discarded and its next actions cannot reach a later turn.
        """
        ...

    def last_activity_at(self) -> float:
        """Loop time (asyncio.get_event_loop().time()) of the most recent
        PROGRESS event for this turn — an audio chunk, a transcript delta
        in either direction, a tool call, a response acknowledgement,
        turn_complete — plus the local tool milestones that produce no
        server message. Server errors, keepalives and session bookkeeping
        do NOT count: they prove the socket is open, not that the model
        is working. Returns the turn-start time until the first one
        arrives. The idle watchdog reads it as "when did this turn last
        make progress". See issue #4532."""
        ...

    def last_chunk_at(self) -> float:
        """Loop time of the most recent audio chunk specifically (not
        tool calls / turn_complete). Used by the daemon's barge-in gate
        to detect when the model is currently producing TTS."""
        ...

    def end_input_at(self) -> float:
        """`time.monotonic()` of the moment the user's input was closed
        and the model was asked to answer, or 0.0 while input is still
        open. The idle watchdog measures its last-resort pre-response cap
        from here, so a long utterance cannot eat the model's budget."""
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
        """True if the connection dropped or the response failed before
        completion. The daemon ends the turn and plays its failure cue."""
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

    One instance per daemon. Adapters may keep a socket alive across turns
    or prepare at startup and open only when a conversation is acquired,
    as required for providers that bill connected silence. Internally manages reconnection (including any
    rotation the provider's session cap forces) and context-reset on
    long idle gaps.

    Gemini Live, OpenAI Realtime, Grok, and OpenAI Live implement this
    interface. Provider wire details stay inside their adapters.
    """

    async def start(
        self,
        registry: ToolRegistry,
        system_instruction: "str | Callable[[], str]",
    ) -> None:
        """Prepare for acquisition, opening a persistent socket if appropriate,
        and start the background tasks
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
