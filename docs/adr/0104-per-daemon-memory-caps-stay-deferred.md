# ADR-0104: Per-daemon memory caps and systemd-oomd stay deferred until a named trigger fires

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

Memory-pressure work on the 1 GB Pi shipped in two evidence-driven steps.
Stage 1 (2026-05-24) is the layer that works on the stock RPi 5 kernel with
the memory cgroup controller off: an `OOMScoreAdjust` ladder, zram resized to
50% with lz4, `vm.*` reclaim tuning, and MGLRU `min_ttl_ms=1000`. The
audio-protection subset of Stage 2 shipped the same day a `stress-ng` run
produced the evidence for it: the system survived, but music was audibly
"splotchy, crushed" and forensics showed `jasper-aec-bridge` with 42 MB in
zram — page-fault decompression latency exceeding the ALSA buffer's slack.
That bought `cgroup_enable=memory` plus `jts-audio.slice` / `jts-mic.slice`
with `MemorySwapMax=0`.

What Stage 2 would still add is per-daemon `MemoryHigh=`/`MemoryMax=`
enforcement on the non-audio path and systemd-oomd. Nothing has produced
evidence for either. The existing `MemoryHigh/Max` values in the unit files
were sized while they were silent no-ops, so nobody knows whether
`MemoryMax=120M` on `jasper-mux` is right or generous; enabling more of them
blind risks restarting healthy daemons. systemd-oomd additionally kills a
whole cgroup with no per-process forensics.

## Decision

**Ship structural memory protection only against an observed symptom.** The
remaining Stage 2 work — per-daemon caps outside the audio path, and oomd —
stays deferred until one of these fires:

- an observed slow memory leak (MemAvailable trending down over days in
  `/system/`'s memory sparkline);
- audio xruns correlated with memory pressure
  (`/proc/asound/card*/pcm*/sub*/xrun` ticking during PSI pressure events);
- a new dependency that is known-leaky and wants a hard cap to bound it;
- other operators reporting leak shapes the original hardware does not show.

The same discipline is what shipped the audio subset: same-day evidence →
same-day fix.

## Consequences

- A slow leak in `jasper-voice` or `jasper-control` is caught by the kernel
  OOM killer's badness heuristic and the `OOMScoreAdjust` ladder rather than
  by a per-daemon cap, so it degrades the box before it kills the leaker.
- The cost of the deferral is bounded and known: the analysis of what the
  full architecture would do, add, and cost does not have to be re-derived —
  only whether a trigger is present yet.
- Enabling the memory cgroup already made the `MemoryHigh=`/`MemoryMax=`
  directives that existed in six unit files live. Those are in effect today
  with values chosen when they were no-ops; that is a known, accepted
  exposure, not a validated configuration.
- Rejected: shipping the caps "while we're in here" to catch hypothetical
  future leaks. The right pattern is measure first, then size.
