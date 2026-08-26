# ADR-0123: An active leader's crossover runs after the round-trip, in a second CamillaDSP

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the design 2026-06-21; recorded here when
  HANDOFF-distributed-active.md was trimmed to its operational spine)

## Context

The household runs multiple active-crossover speakers and needs any speaker,
active or passive, to work as either leader or follower. So an *active
speaker leading a pair* is a v1 requirement, not a someday feature.

Every member, the leader included, plays its own **localhost snapclient** —
the leader is its own receiver, so its DACs are fed by the round-tripped
stream. A passive leader's receiver job is dumb (outputd `ChannelPick`), so
one CamillaDSP suffices: bake → wire. An active leader's receiver job is the
**crossover**, and outputd has no DSP (ADR-0122).

The obvious objection is that one CamillaDSP can open many channels and chain
stages, so why not merge the bake and the crossover into one instance?

## Decision

**An active leader runs two CamillaDSP instances: camilla#1 bakes the program
domain to the wire, camilla#2 runs this box's endpoint-crossover config on
the round-tripped stream.** It is literally the follower's endpoint config,
applied on the leader's own drivers.

## Consequences

- **The reason is time, not channel count.** The leader's two outputs sit at
  different points in the sync timeline: the wire feed is the *pre-stream*
  source, produced before snapserver, while the DAC feed must play the
  *round-tripped, network-buffered* stream to stay phase-locked with the
  follower. One pipeline pass emits at one time point, so the crossover must
  sit downstream of snapserver→snapclient — a separate process from the
  pre-stream bake.
- **"Just add a sync delay" does not exist, because sync is not a delay.** It
  is continuous clock-drift correction between two independent DAC
  oscillators — a control loop that stuffs/resamples. Two DACs slide
  ~1 ms/min apart at typical ppm, so a stereo pair comb-filters within
  minutes without it. A fixed or queried `Delay` is a scalar and cannot track
  that; the localhost snapclient round-trip reuses snapcast's proven sync
  engine, which the leader already runs even when passive.
- **Computing the crossover in one wider instance and splitting downstream
  fails too:** the N driver channels would land on the *un-corrected* side,
  and snapcast drift-corrects only the *stereo* stream — there is no
  N-channel snapclient. The crossover must follow the corrected stereo.
- The sync mechanism and the instance count are **orthogonal**: sync is
  settled (the round-trip); the second instance is purely "the crossover runs
  after the corrected stream." This is exactly why **solo** needs one
  instance — no follower means no wire output and no clock to match, so the
  crossover stays in the single low-latency graph.
- camilla#2 is a *light* driver-DSP instance (biquad crossovers + limiters,
  no room FIR), and the added latency is its chunk buffer — **fixed**, which
  is what snapcast's per-client `--latency` exists to null, and the same
  latency a solo active speaker already carries.
- camilla#1's bake emits the program domain only with a `File` sink to the
  snapcast FIFO, so `classify_camilla_graph` carries one exemption: **a flat
  program graph whose `devices.playback.type == File` is safe regardless of
  topology** — no DAC is attached, so no driver can be over-driven. The
  exemption is keyed strictly on the playback *type*, reusing the same
  `playback_is_pipe` parser as the leader-pipe liveness check so the two
  cannot disagree. The dangerous direction — a flat *Alsa*-sink graph
  reaching the DAC — is not exempted, and the pipe bake is not selectable as
  a solo speaker's own graph.
- Rejected: collapsing to one instance by putting the crossover in outputd
  (ADR-0122's Option A). That discards the proven crossover engine and its
  re-proof.
- Costs carried, not blockers: two CamillaDSP on a 1 GB Pi (RAM has headroom;
  the binding limit is CPU jitter and Pi 5 thermal throttling), and leader
  TTS, which ADR-0125 resolves.
