# ADR-0122: An endpoint's Layer-A crossover runs in CamillaDSP, not in outputd

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the design 2026-06-20; recorded here when
  HANDOFF-distributed-active.md was trimmed to its operational spine)

## Context

An active speaker's CamillaDSP splits the program across woofer/mid/tweeter
and **band-limits each driver** (Layer A). A bonded follower receives the
leader's corrected stereo program at `jasper-outputd`'s `dac_content` lane,
post-CamillaDSP, where the only transform is `ChannelPick`
(Stereo/Left/Right/Mono — duplication or a clip-safe −6 dB average, no
filtering). Sending that full-range feed to a tweeter destroys it, which is
why an active speaker refused to bond at all: the graph carrier rejects a
roleful active graph (`eq_on_active_bonded_member`) and outputd's round-trip
lane refuses a non-`SingleAlsa` sink.

Relocating Layer A onto the follower needs an engine. Two existed:

- **A — split inside `jasper-outputd` (Rust).** 100 % greenfield
  safety-critical DSP (biquads, LR crossovers, limiters, delays, the 2→N
  split) inside a reboot-on-fail daemon, **plus** relaxing that lane's hard
  2-in/2-out `SingleAlsa` constraint to N channels. No verifier exists for a
  Rust graph, so the safety contract would be re-implemented rather than
  re-proven.
- **B — CamillaDSP re-entry.** Point the box's *existing* CamillaDSP at the
  streamed program and emit a driver-domain-only graph.

## Decision

**Option B: the endpoint's Layer A runs in CamillaDSP, fed from the bonded
ingress — one reusable "endpoint crossover" capability, not three.** The same
config shape serves an active follower (pick L/R, split across its drivers),
an active leader's own drivers, and a brainy wireless sub (one driver with a
low-pass). The differences between those three are operational — RAM, voice,
sub sync — not architectural.

Concretely: the box's CamillaDSP captures the bonded ingress instead of
fan-in; it emits `channel-select (2→2) → split_active_<way>way (2→N) →
per-driver [crossover, delay, gain, limiter]` with **no** program prefix and
**no** EQ headroom (the leader baked Layer B/C); outputd's `dac_content`
`ChannelPick` is disabled on that box, because CamillaDSP now owns both the
pick and the split.

The emitter is a **parameterization of the shipped active emitter** —
compose, never text-splice — and `classify_camilla_graph` grows a
driver-domain-only arm that re-proves the emitted config verbatim.
Emitter and verifier stay independent.

## Consequences

- Layer A is **byte-for-byte the solo chain**, relocated. The shipped
  per-driver limiter, crossover high-pass and `0 dB` ceiling are reused
  unchanged, and the safety contract transfers with one new classification
  arm instead of being rewritten in Rust.
- **No new process and roughly neutral cost** on a follower: it already runs
  CamillaDSP for the fallback lane, and Option B repurposes that instance.
- `jasper-outputd` stays DSP-free on this path ("swap the engine, not the
  topology"). The `dac_content_lane_rejects_non_single_alsa_sink` fence is
  kept — it still guards the dumb-follower lane — and simply never fires on
  the active path, because Option B routes the active sink around that lane.
- The costs accepted: CamillaDSP's fixed pipeline latency in the synced path
  (nulled by snapcast's per-client `--latency`, which ships compensating
  nothing until a measurement produces an offset that generalises across
  DACs) and on-device rate-domain tuning, which must honor the
  `rate_adjust`+`AsyncSinc` oscillation trap — never both when capture rate
  equals playback rate.
- The coupling direction is **multiroom → active_speaker**, one-way.
  `active_speaker` accepts a capture device and a domain mode and stays
  ignorant of grouping; the multiroom reconciler decides both per role. This
  is what keeps solo-active EQ safe to reason about in isolation.
- Rejected with Option A: an N-channel `dac_content` lane. Anything wanting
  driver-domain DSP in outputd re-opens this ADR.
