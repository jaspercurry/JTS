# ADR-0254: Runtime buffers are bounded and drop-and-count

- **Date:** 2026-09-07
- **Status:** Accepted
- Refs: the local runtime-cleanup lane (secrets, bounded resources,
  operating evidence); the audit that opened it is #4427.

## Context

Three runtime buffers had no bound, and each one turned a slow or wedged
consumer into unbounded growth on a 1 GB box:

1. `BaseLiveTurn._audio_q` (`jasper/voice/_base.py`) carried a turn's
   outbound TTS PCM (24 kHz mono int16 = 48 000 B/s) with no `maxsize`.
   Providers burst a whole response ahead of realtime and none caps
   response length. Worse, the idle watchdog (`turn_playback.idle_watchdog`)
   deferred while the queue was merely non-empty, so a playout consumer
   wedged behind queued audio deferred the end of the turn forever — no
   answer and no wake (non-negotiable #6).
2. jasper-fanin's xrun-event channel (`rust/jasper-fanin/src/main.rs`) was
   an unbounded `mpsc::channel` whose sender runs on the SCHED_FIFO mixer
   thread and whose receiver `fdatasync`s once per event; an xrun storm
   (up to 187.5 events/s per input lane at 256 frames @ 48 kHz) outruns it.
3. Both TTS servers (`rust/jasper-fanin/src/tts.rs`,
   `rust/jasper-outputd/src/tts.rs`) spawn one detached thread per accepted
   Unix-socket connection with no read deadline and no connection ceiling.
   The voice daemon's `TtsPlayout` holds ONE connection for its whole
   process lifetime and idles for hours between turns, so an idle deadline
   would break the daemon.

## Decision

1. **Playout queue.** `AUDIO_OUT_QUEUE_MAX_BYTES = 600 s × 48 000 B/s`
   (28.8 MB) is a memory ceiling, not a response-length limit: only a wedged
   consumer reaches it. Over the ceiling the incoming chunk is dropped
   (drop-newest: tail truncation, the same shape as a barge-in flush),
   `audio_dropped_bytes()` counts it, and `event=turn.audio_overflow` is
   logged once per turn. The terminal `None` sentinel is never subject to
   the bound, so the consumer always ends the turn. A barge-in flush drains
   the whole queue (a chunk can land behind the sentinel) and resets the
   dropped-byte count with it: a truncation the household caused by talking
   over the model is not the overflow the cue below is for. The idle watchdog
   defers on playout *progress* — the turn's own pending count, which falls
   exactly when the consumer dequeues; the shared drain deadline is consulted
   only once the queue is empty, because another writer can advance it and
   mask a wedged consumer — and ends the turn with
   `event=turn.playout_stalled` when nothing moves for
   `response_stall_timeout`. A truncated turn plays the
   `internal_error` cue at end of turn, journalled as
   `event=turn.truncated_response`; a stalled playout cannot be made audible
   through the pipe that stalled, so the WARN event and the cue-outcome
   snapshot are its handle.
   *Rejected:* backpressure (the producer is the provider's shared receive
   loop — tool calls, `turn_complete` and close frames ride it); drop-oldest
   (cuts the start of the answer, the most audible loss); a tuning knob.
2. **Fan-in xrun channel.** `sync_channel(EVENT_CHANNEL_CAPACITY)` — 256,
   one definition shared with the impulse tap's channel — and `try_send` from
   the mixer thread, the sender and its `xrun_events_dropped` gauge bundled as
   `XrunSink`. The gauge is published on the STATUS socket; live `xrun_count`
   is bumped before the send, so a full channel loses only forensic JSONL
   lines, never the count. Both channels share one `send_drop_counted`
   helper, and it counts EVERY failed send: `Disconnected` is not only
   shutdown — the writer thread returns early when its log file will not
   open, and a gauge reading zero while every event is lost is the wrong
   answer.
3. **TTS servers.** No idle deadline. `TTS_FRAME_DEADLINE = 30 s` runs from
   the first byte of a command to the end of that command (header and
   payload); a healthy client writes a 2 MiB frame in well under 100 ms, and
   30 s (not 5) because a false trigger drops the daemon's lifetime
   connection. The same deadline is armed as a WRITE timeout once per
   accepted connection, so a client that stops reading its `FLUSH_SYNC` ack
   cannot pin its slot inside `write_all`. `TTS_MAX_CLIENTS = 16` concurrent
   connections against a real load of one lifetime plus at most two
   transient; the headroom is over leaked idle connections, which are
   legitimate here, so that a handful of them cannot refuse the daemon its
   reconnect. An excess connection is closed on accept. All three live once
   in `jasper-tts-protocol` and both servers
   consume them; `frame_timeouts` and `connections_rejected` publish beside
   `dropped_commands`. So do the accept loop that enforces them
   (`serve`) and the counters that publish them (`TtsServerCounters`): each
   daemon supplies only its own per-connection handler and queueing policy.
   `TtsPlayout` reconnects on a server-closed socket, so
   a false deadline costs one reconnect, not deafness.
   *Known limit:* `SO_RCVTIMEO` is per read, so a client that dribbles one
   byte under the deadline is not cut — the bound targets the client that
   stops writing, the failure actually seen.

## Consequences

Memory is bounded per turn, per channel and per server, and every drop is
counted where an operator already looks (`/state`, STATUS, the journal).
What this gives up: the tail of an answer past 600 s of unplayed audio,
xrun log lines during a storm, and a seventeenth TTS client. These bounds are
permanent machinery: their tie is non-negotiable #6 (a turn that cannot end
keeps the speaker from answering the next wake) and the Pi's RAM budget.
Code points here with `# See ADR-0254` / `// See ADR-0254`.
