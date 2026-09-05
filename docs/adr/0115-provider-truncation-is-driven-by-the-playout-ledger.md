# ADR-0115: Provider truncation is driven by the local playout ledger

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

When a user interrupts the assistant, two things must agree: what the listener
actually heard, and what the realtime provider believes it said. Getting that
wrong leaves the model's conversation state describing audio nobody heard.

The tempting inputs are the cheap ones — when the provider's event arrived, or
how many frames were handed to the audio path. Both are wrong in the same
direction: arrival time is network-shaped, and queued frames are not heard
frames. Only the component holding the output clock knows how much audio was
actually emitted.

## Decision

**Robust barge-in is JTS-owned, and provider truncation is driven from the final
playout ledger — never from provider event arrival time or queued-frame
estimates.** The sequence is: detect real user speech locally while assistant
audio plays; flush the active TTS transport synchronously; the TTS owner
advances its epoch and drops queued assistant frames; the flush acknowledgement
returns per-segment provider item id, flushed frames, and `audio_played_ms`; the
voice-provider adapter then issues the provider's cancel/truncate using *that*
acknowledgement.

Stopping audible audio comes first and reconciling provider state second,
because the local flush is the latency-critical action.

The ledger's owner follows the topology. In the solo packaged topology fan-in
owns assistant audio and reports a mix-commit count — frames committed toward
the snd-aloop program, DAC-rate-paced by the blocking write. That over-reads
true acoustic playout by the fixed downstream pipeline depth, which is the
conservative direction for truncation. On a bonded member outputd owns the mix
and reports the DAC-clock-true number.

## Consequences

- A ledger reporting `max_audio_played_ms = 0` makes the adapter a no-op plus a
  warning rather than a truncation, so a missing ledger can never degrade into
  truncating on bytes received.
- Closing fan-in's offset to exact DAC-clock precision (subtracting outputd's
  reported DAC delay) stays a known follow-up; over-reading is safe until then.
- Per-provider wiring status — which packs self-truncate as a no-op and what
  remains before default-on — belongs to
  HANDOFF-barge-in.md (deleted per ADR-0199), not to the output-side docs.
- A runtime guard hard-disables barge-in for a session whose active profile has
  no AEC reference, so a speaker with no echo cancellation cannot barge in on
  its own TTS bleed.
