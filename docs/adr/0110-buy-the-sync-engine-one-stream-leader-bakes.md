# ADR-0110: Buy the sync engine — one stereo stream, leader bakes, receivers pick a channel

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Keeping N speakers in sample-lock across consumer WiFi is the hardest part of
grouped playback: independent sound-card crystals drift in ppm, WiFi injects
50–200 ms jitter spikes, and clock domains hop on roaming. Snapcast already
solves it with a timestamp plus latency-buffer model — a per-client software
clock-offset estimate over the same unicast TCP connection, sample-stuffing as
the rate tracker, and a fixed playout buffer. A 2026-06-10 hardware spike
(jts3↔jts) cleared the resource gate: snapserver plus snapclient ≈ 15 MB Pss and
≈ 0.2 % CPU, with FLAC ≈ PCM.

The second question was where per-channel room correction runs. A prior-art pass
(Roon, Sonos, Music Assistant, Snapcast, Squeezelite/LMS, PipeWire) plus the
owner's ruling settled it, and one Snapcast property forced the shape: clients
are sample-locked only *within one group on one stream*, so separate L/R streams
drift independently (maintainer-confirmed, snapcast#747).

## Decision

**Adopt Snapcast as the clock/transport/dejitter engine; do not build network
audio sync.** JTS owns discovery, grouping, and the control plane — the same
boundary Music Assistant and Home Assistant draw.

**The leader's one CamillaDSP bakes all per-channel content correction into a
single stereo program** (left corrected for the leader's seat, right for the
follower's), writes it to a pipe, and snapserver streams that one stream to
everyone. Every receiver — including the leader's own localhost snapclient — is
a channel-*picker*: `jasper-outputd`'s `ChannelPick` selects
`left`/`right`/`mono`/`sub` receiver-side on the round-tripped stream. No
receiver runs content DSP.

Driver DSP is the one local-hardware exception: an active satellite that
physically drives woofer/tweeter amps runs its own crossover/protection graph on
the box that owns those DACs. That is hardware safety at the DAC, not room
correction, and it does not make the endpoint a brain.

**Exactly one rate adjuster per chain.** snapclient's sample-stuffing is the
synced chain's rate tracker, so no CamillaDSP in a bonded chain runs
`rate_adjust=true`. A dumb follower is the deliberate exception — its CamillaDSP
is out of the bonded path, feeding only the fallback lane into a sink that has a
real clock.

## Consequences

- The second content-DSP CamillaDSP and its per-follower room-DSP RAM cost never
  had to exist: a 1 GB Pi leader adds only snapserver plus one localhost
  snapclient.
- WiFi is the supported transport and buffer depth is the jitter lever; Ethernet
  is a best-case reference, never a requirement.
- Rejected: separate per-channel streams (they drift independently — hundreds of
  ms under WiFi jitter, which "sub sync is loose" does not cover); a member-side
  channel-split weave in each receiver's own CamillaDSP (built as
  `channel_split.py`, never wired, deleted 2026-08-25); a dedicated 3-channel
  L/R/LFE stream for 2.1 (the shipped wireless sub takes a clip-safe mono sum of
  the existing stereo and low-passes it receiver-side, so no stream-format
  change is needed).
- Accepted cost: a third-party dependency in the audio path, and Snapcast's
  central-server shape, which the fixed-leader model
  ([ADR-0111](0111-one-fixed-leader-no-election.md)) absorbs as bounded, visible
  degradation.
- The leader plays its own channel from the buffered round-trip, which makes its
  own music depend on a local loop — the failure that
  [ADR-0113](0113-the-leader-self-loop-is-never-a-single-point-of-failure.md)
  answers.
