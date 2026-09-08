# ADR-0126: A subwoofer's crossover executes on the receiver, on the one shared stereo stream

- **Date:** 2026-08-26
- **Status:** Superseded by
  [ADR-0236](0236-independent-subwoofers-are-deleted-a-dac-channel-sub-stays.md)
  (shipped 2026-06-23; recorded here when HANDOFF-distributed-active.md was
  trimmed to its operational spine)

## Context

"Subwoofer" covers two different designs that shorthand conflates: a sub on a
*single* box's spare DAC output (a solo-active concern, orthogonal to
wireless), and a *separate bonded sub box* in a grouped set. This ADR is
about the second.

For a bonded sub, the filtering can run on either end, and the shared
`jasper.camilla_emit` primitives can emit the fragment for either host:

- **Receiver-side.** The sub picks mono from the one shared stereo stream and
  low-passes locally; the leader only specifies the corner.
- **Sender-side.** The leader pre-bakes the sub's low-pass (plus an optional
  subsonic/excursion high-pass) and streams a finished mono sub channel; the
  sub is a pure `ChannelPick`.

Sender-side lets the sub be the cheapest possible box, but the shared
2-channel stereo stream cannot carry a pre-filtered sub channel without
stripping the mains' bass or changing the pinned format — so it costs a
**second leader bake and a separate, loosely-synced sub stream**.

## Decision

**Receiver-side is the default: one shared full-range stereo stream, and each
member applies its own filter locally.** The sub low-passes; with a sub
present, every non-sub member applies the complementary high-pass at the same
corner (bass management, default on).

Where that filter runs depends on what the member is:

- A **dumb/passive member** filters in its local `jasper-outputd`
  `dac_content` lane — `ChannelPick::Sub` (clip-safe mono sum, then a 4th-order
  Linkwitz-Riley low-pass) for the sub, an LR4 high-pass for the mains.
- An **active endpoint** has `dac_content` disabled (ADR-0122), so its
  bass-management high-pass lives in the CamillaDSP Layer-A graph and must
  pass that graph's no-full-range re-proof.

`/rooms/` stores one bond-level corner and a mains-high-pass toggle, and the
reconciler fans the same corner out as the sub low-pass and the mains
high-pass, clearing the high-pass whenever there is no sub, the toggle is
off, the member is the sub, or the member is an active endpoint.

**Setting and execution are independent:** the corner is *set* on the
leader's pair page, because the leader orchestrates the pair, even though the
filter *executes* on each receiver.

## Consequences

- No transport change and no extra leader work: the sub reuses the ordinary
  full-range member path, and the mains still derive their own program from
  the same stream.
- **A sub never plays full-range.** The FIFO path, the fallback period before
  the policy converges, and a missing filter all resolve to mono-plus-lowpass
  or silence. The earlier behavior — a `sub` member mapped to full-range mono
  — is retired.
- Loose sync between sub and mains is acceptable because bass is
  non-localizable; this is what makes the shared-stream approach viable at
  all.
- Sender-side pre-baking survives as a documented **exception** for a
  maximally-cheap sub endpoint that cannot run its own filter. Nothing
  implements it today: the member-side wrapper that once did (a
  `channel_split` module) was removed as dead code in 2026-08.
- The brainy/active-endpoint sub path stays separate work precisely because
  CamillaDSP Layer A, not outputd `dac_content`, owns driver protection
  there.
- Both hosts reuse the one LR4 primitive; they differ only in which box runs
  it and whether the sub needs its own stream.
