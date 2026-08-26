# ADR-0125: Leader TTS and the follower fail-closed cue inject pre-crossover

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the design 2026-06-20 against `jts3`
  measurements; recorded here when HANDOFF-distributed-active.md was trimmed
  to its operational spine)

## Context

Tweeter-safe TTS on an active leader wants three things that fight:

- **P1** it passes through Layer A (tweeter-safe),
- **P2** it does not take the snapcast round-trip (low-latency),
- **P3** it is summed before outputd's AEC-reference publish tap — multiroom
  inv-A, non-negotiable, or the speaker wakes on and talks over its own
  voice.

Today's outputd mix gives P2+P3 but not P1, and it is a 2-channel
`single_alsa` mixer that does not apply to an N-channel active sink anyway.
Injecting at fan-in gives P1+P3 but not P2 on a leader.

A follower is voice-parked, so it has no conversational TTS — but it does
need an **audible** cue when its ingress stalls, and that cue faces exactly
the same tweeter-safety question.

The measured incremental latencies on `jts3` (solo active 2-way @ 48 kHz) are
in [distributed-active-bringup-2026-06.md](../historical/distributed-active-bringup-2026-06.md).

## Decision

**Leader-only voice is ratified, and both the leader's TTS and the follower's
fail-closed cue are injected at one point: the crossover instance's own
input, post-snapclient and upstream of Layer A.**

- **Leader-only voice.** A bonded follower is voice/AEC-parked and inv-A
  keeps TTS off the stream, so "voice plays only from the leader" is already
  de-facto true; ratify it. The assistant lives on the leader — fine in one
  room. This removes any need to stream or sync TTS, killing the
  round-trip-latency worry outright. It does **not** by itself make the
  *leader's own* TTS tweeter-safe.
- **Leader TTS** is summed into camilla#2's input. Because that is *after*
  snapclient, TTS traverses only the crossover DSP, never snapcast.
- **The follower cue** uses the same injection point, follower-local. It is
  written by a long-running writer (the grouping supervisor /
  `jasper-control`) — never `jasper-voice`, which is parked, never the
  reconciler oneshot, which cannot play a cue, and never a post-camilla mix.

## Consequences

- **The incremental cost of tweeter-safe leader voice is DSP latency, not
  round-trip latency:** roughly the crossover instance's chunk, which is
  exactly what a solo active speaker already pays for its own voice. The
  working ceiling for the band-limiting + playout stage is ≈150 ms, against a
  target of the solo-active baseline. **This introduces no new latency
  class.**
- Rejected — a protective filter on the TTS lane at outputd (sub-ms, and P1
  on paper): its tweeter-safe forms are either *skip-tweeter*, a low-pass
  that muffles speech below the crossover where consonants live, or
  *re-implementing the per-driver crossover in outputd Rust* — precisely what
  ADR-0122 rejected, plus relaxing outputd's 2-channel constraint. It also
  leaves the follower cue needing a camilla-input path anyway, so it costs
  two mechanisms where this decision buys one.
- Rejected — injecting upstream of the bake at fan-in: it adds the full
  snapcast playout (~400 ms) and streams TTS to the follower, which inv-A
  forbids.
- **Unification was decisive.** The follower cue must inject upstream of
  Layer A regardless, so leader TTS and follower cue become one mechanism,
  validated once.
- The content duck must follow the same injection point, so the published
  reference still carries the ducked program (inv-A). ADR-0124 makes that
  free: the duck is commanded over the same socket as TTS.
- `jasper-outputd` stays dumb on this path.
- **Not the same as detecting the stall.** The grouping supervisor's
  `dac_content` starvation watch is skipped on active endpoints — it watches
  the dumb-member round-trip, not the active endpoint's own ingress.
  Detecting *that* starvation remains the deferred prerequisite for
  triggering the cue; the hard no-full-range guarantee does not depend on it,
  because the loaded graph is always the re-proven driver-domain baseline, so
  a starved ingress resolves to silence through Layer A.
