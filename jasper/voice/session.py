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

    `pcm` is 24 kHz mono int16 payload. The optional provider item id is
    the stable handle needed by provider-specific truncation later (for
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
    """The provider half of a barge-in: stop generating, then trim the
    provider's history to what the household actually heard.

    Capability-based, never provider-name-based: each catalog provider
    declares a `catalog.InterruptReconcile` kind, and a provider that
    stops on the user's own voice (`LiveTurn.owns_interruption`)
    implements NEITHER member — the host skips the reconcile at its own
    boundary instead of calling two no-ops. Gemini interrupts generation
    through manual activity but has no API for trimming history to JTS
    playback counts. See ADR-0115.
    """

    async def cancel_response(self, reason: str) -> None:
        """Tell the provider to stop generating this turn's in-progress
        response — JTS deciding to stop the model, the inverse of
        `LiveTurn.wait_for_interrupt`, which observes the model reporting
        it stopped. Local TTS flush is a separate daemon-layer step:
        cancelling generation never stops already-queued DAC audio.

        ``reason`` is for the structured-log line only. Must be idempotent
        and must never raise on an already-complete or absent response."""
        ...

    async def truncate_assistant_audio(
        self, provider_item_id: str | None, audio_played_ms: int,
    ) -> None:
        """Trim an owned item to its confirmed local ledger boundary in ms.

        Zero is a valid boundary; an absent item is not. Local drain counts
        are estimates, not acoustic proof. Must tolerate None and never
        raise on an absent or finished response.
        """
        ...


@runtime_checkable
class ProviderTurn(Protocol):
    """What a provider adapter implements on top of `_base.BaseLiveTurn`;
    everything else `LiveTurn` names, the base implements once for every
    adapter. `_base.BaseLiveTurn` does not enforce these five via abstract
    methods, so conformance is pinned only by the isinstance checks in
    tests/test_voice_barge_in_contract.py — this Protocol must stay
    `runtime_checkable` for that pin to have teeth."""

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        ...

    async def send_text_context(self, text: str) -> None:
        """Add a text-only context item to the current turn without asking
        the provider to generate yet.

        Used for narrow daemon-initiated confirmation windows where the
        model needs one-shot routing context before live user audio. The
        normal wake path does not call this."""
        ...

    async def end_input(self) -> None:
        """Mark end-of-user-speech for this turn (sends `activity_end`).

        Idempotent — calling twice is a no-op."""
        ...

    async def release(self) -> None:
        """Release this turn and its pending input/output. Idempotent.

        An abandoned response must not reach a later turn. The adapter
        clears input or reopens the session before another turn can start.
        Pending tool work is cancelled without waiting for its executor;
        its results are discarded and its next actions cannot reach a later turn.
        """
        ...

    def capture(self) -> TurnCapture | None:
        """What this turn offers conversation history, or None when the
        provider exposes nothing to record. See `TurnCapture`."""
        ...


@runtime_checkable
class LiveTurn(ProviderTurn, Protocol):
    """An acquired voice exchange, or a continuous wake conversation, as
    the host consumes it: `ProviderTurn` plus the surface
    `_base.BaseLiveTurn` implements identically for every provider.

    The daemon acquires a turn from a `LiveConnection` on wake, streams
    user audio frames into it, awaits the model's response, and releases
    the turn when idle. The connection itself stays open across turns
    (see `LiveConnection`). Adapters declaring `continuous_input` retain
    this object through follow-ups.
    """

    # True when the provider stops generating on the user's own voice, so
    # the host must never issue its own barge-in flush for this turn: the
    # local detector scores the assistant's echo as well as the user, and
    # flushing the fan-in lane on that chops the reply mid-word. The host
    # still forwards mic audio and still honours conversation end. Such a
    # turn is deliberately not `Interruptible`.
    owns_interruption: bool
    # True when the adapter streams the microphone for the whole
    # conversation instead of one endpointed utterance per turn. Must
    # agree with the provider's `catalog.ProviderCatalogEntry` field of
    # the same name.
    continuous_input: bool
    # True while this turn waits on work it delegated to a backend model,
    # which produces no audio of its own: the conversation watchdog then
    # judges the wait on activity rather than on audio.
    backend_pending: bool

    def discard_input(self) -> None:
        """Synchronously revoke microphone audio accepted for this turn but
        not yet on the wire. Idempotent; a turn that buffers none no-ops."""
        ...

    def audio_out_chunks(self) -> AsyncIterator[AudioOutChunk]:
        """Yield TTS audio (24 kHz mono int16 PCM, with optional provider
        item identity) until the turn is released or the connection drops."""
        ...

    def last_activity_at(self) -> float:
        """Loop time (asyncio) of this turn's most recent PROGRESS event.
        `_base.BaseLiveTurn._note_activity` defines what counts; the idle
        watchdog reads this as "when did this turn last make progress"."""
        ...

    def last_chunk_at(self) -> float:
        """Loop time of the most recent audio chunk specifically (not tool
        calls / turn_complete), so the host can tell a model that is
        producing TTS from one that is merely working."""
        ...

    def end_input_at(self) -> float:
        """`time.monotonic()` of the moment the user's input was closed and
        the model was asked to answer, or 0.0 while input is still open.
        The idle watchdog measures its last-resort pre-response cap from
        here, so a long utterance cannot eat the model's budget."""
        ...

    def bytes_sent(self) -> int:
        ...

    def chunks_received(self) -> int:
        """Read with `bytes_sent` to detect the silent-failure mode where a
        provider accepts the connection but never produces any output
        (quota exhausted, service degraded)."""
        ...

    def usage(self) -> TurnUsage:
        """This turn's token usage. See `TurnUsage`."""
        ...

    def turn_lost(self) -> bool:
        """True if the connection dropped or the response failed before
        completion. The daemon ends the turn and plays its failure cue."""
        ...

    def server_turn_complete(self) -> bool:
        """True once the provider has signalled 'the model is done
        speaking' for this turn — the canonical clean close, which lets
        the idle watchdog end the turn without racing mid-response chunk
        gaps that look like idleness."""
        ...

    def request_local_interrupt(self) -> None:
        """Locally signal a user barge-in WITHOUT telling the provider.

        Resolves `wait_for_interrupt` so the playback path flushes local
        TTS immediately. This is the provider-agnostic detection + flush
        spine: it deliberately does NOT truncate or cancel the provider's
        in-flight response — `Interruptible` owns that."""
        ...

    async def wait_for_interrupt(self) -> None:
        """Resolve when this turn is interrupted — locally, or by the model
        signalling the user spoke over its speech. Used by the playback
        path to race write-current-chunk against flush-immediately."""
        ...

    def clear_interrupted(self) -> None:
        """Reset the interrupt event after the playback path has flushed
        its output in response."""
        ...

    def drop_pending_audio(self) -> int:
        """Drop assistant audio buffered for playback but not yet written,
        returning the number of chunks dropped.

        A local flush clears the DAC ring (~one write), but burst-delivery
        providers (OpenAI/Grok) enqueue the whole response's audio up
        front, so without dropping it the playback loop resumes writing the
        backlog and the assistant audibly talks over the user. Idempotent;
        must never raise."""
        ...

    def audio_chunks_pending(self) -> int:
        """Depth of the playout queue — what `drop_pending_audio` would
        drain, and the progress signal the idle watchdog measures on ALONE
        while it is nonzero. See ADR-0254."""
        ...

    def audio_dropped_bytes(self) -> int:
        """Assistant audio this turn never queued because the playout queue
        was already at its byte ceiling — a wedged consumer, not a barge-in
        (which drops through `drop_pending_audio`). Non-zero means the
        reply was truncated at the tail."""
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

    def warm_session_until(self) -> float | None:
        """Epoch seconds at which a billable session held open past the
        end of a conversation closes itself, or None when none is held.

        Only a provider that bills connected silence and keeps a session
        warm between conversations ever returns a number; the rest keep
        the base's None. Surfaced at ``/state.voice.live_session_warm_until``
        so an agent can see money on the meter."""
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
