# ADR-0113: The leader's self-loop is never a single point of failure for its own music

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Under the canonical bonded design the leader plays its own channel through a
localhost snapclient, so it is a follower of itself plus a streamer. That
symmetry is the design's main payoff — one member-playback path, validated once
— but it puts the leader's own music behind a local loop: its snapclient,
snapserver, and the round-trip FIFO. Routine Pi ALSA underruns in that loop
would make the brainy speaker go silent on its own music, even when it is
"bonded but alone".

For a *follower*, a starved FIFO reading as silence is correct. For the leader
hearing itself it is a silent failure, which the project does not accept.

## Decision

**When the self-loop is unhealthy, `jasper-outputd` falls back to the direct
fan-in lane rather than to silence** — a momentarily unsynced pair beats a
silent leader. This deliberately inverts the "a starved lane reads as silence"
rule for the leader's own playback.

The mechanics live in outputd's `dac_content` source: it starts in fallback and
the FIFO must demonstrate health for roughly 210 ms before it serves; starvation
falls back within the *same* period, so there is zero silence; recovery is
damped so the DAC never flaps between two time-offset copies of the program; and
staging is bounded with oldest-period overflow drops. The direct lane is drained
while the FIFO serves, so an upstream loopback writer can never stall.

The degraded state is surfaced, not hidden: a cue, a `/state` flag, a dashboard
card, and a row in the failure table.

## Consequences

- The pair-balance trim is applied to both the FIFO and the fallback periods, so
  a starvation transition produces no level jump, and it is applied before
  duck/mix/publish so the AEC reference carries the trimmed program.
- The DAC write loop stays the sole local timing owner: the FIFO side is a
  side-feed that never back-pressures it.
- Given up: sample-lock during the fallback window. The pair is briefly unsynced
  and says so.
- Off is free. With the lane env unset the loop is byte-identical to a solo
  speaker's — the solo-impact contract every grouping increment ships a
  regression test for.
