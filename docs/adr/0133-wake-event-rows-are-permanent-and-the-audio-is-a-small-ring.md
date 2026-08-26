# ADR-0133: Wake-event rows are permanent; the wake-event audio is a small ring

- **Date:** 2026-08-26
- **Status:** Accepted (supersedes the 1 GiB audio cap that shipped with the
  original telemetry store; recorded when HANDOFF-wake-telemetry.md was
  trimmed to its operational spine)

## Context

Every wake event produces two things with very different value densities: a
row of structured funnel/context data worth roughly half a kilobyte, and up to
five six-second WAVs worth roughly half a megabyte. Both land on the same SD
card on a Pi with 1 GB of RAM. The original store capped audio at 1 GiB, which
was sized for "5-7 weeks of corpus" back when the corpus was still being
gathered by hand.

## Decision

**DB rows are never deleted; audio lives in a byte-capped oldest-first ring,
and the cap is small — 128 MiB by default.**

When retention deletes a WAV, the row survives and its `audio_*_path` columns
are rewritten to a `'rolled_off'` sentinel, so a query can always distinguish
"this leg was never captured" (NULL) from "this leg was captured and has since
aged out".

## Consequences

- **Funnel statistics outlive the audio.** Wake rate, per-leg trigger share
  and suspected-false-accept counts stay answerable over a multi-year baseline
  at a row footprint that never threatens the card.
- **Audio is a working set, not an archive.** ~128 MiB holds on the order of
  230 three-leg events — days, not weeks. Anything worth keeping longer gets
  pulled off the Pi into the gold corpus, which is the durable instrument
  (ADR-0129, ADR-0131). Extracting to the corpus is now the operator's job to
  do promptly rather than an afterthought.
- **Storage checks derive from the configured cap, never a literal.** The
  doctor watchdog warns at the *configured* audio cap plus a fixed allowance
  for the DB and transient overshoot, so a Pi that deliberately raises the cap
  does not warn spuriously and a healthy ring never warns at all. A hard-coded
  threshold would have had to be edited in lockstep with this decision; a
  derived one did not.
- **The sweep stays cheap enough to run on every attach.** A small cap means
  the full directory scan touches hundreds of files, not thousands, and a
  running size estimate keeps the common under-cap attach O(1).
- **Compression was not needed.** FLAC would roughly double the horizon at the
  cost of CPU per write and a dependency; with the corpus as the durable store,
  doubling a working set is not worth either.
