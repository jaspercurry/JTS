# ADR-0135: Corpus quality analysis ranks clips for review; it never rejects one

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-wake-corpus-quality.md was
  trimmed to its operational spine; the position dates to the 2026-05-27
  methodology work)

## Context

The deliberate wake corpus is small, expensive to record, and the measurement
instrument that every wake-model claim rests on (ADR-0129, ADR-0131). An
automatic quality filter over it is tempting and dangerous in equal measure: a
detector that silently drops clips changes the instrument, and short 1-3 s wake
utterances are exactly the regime where the available quality metrics are least
trustworthy — integrated loudness is unstable, neural MOS predictors are
out-of-domain, and sample-derivative click detectors fire on plosives and
fricatives.

## Decision

**The analyzer produces a sorted, explained review queue. It never deletes a
clip and never excludes one from training.**

Three rules follow from that:

1. **Deterministic sample- and frame-domain facts rank first** — exact clip
   counts, near-clip mass, flat-top runs, DC offset, RMS/crest, spectral
   aggregates, LPC residual outliers. These are cheap, reproducible, and mean
   what they say.
2. **Neural MOS-style predictors are advisory only** and are shown as such.
   They may reorder a queue; they may never be the sole reason a clip is
   flagged.
3. **Cross-leg coincidence is the arbiter**, not any single-leg threshold. The
   corpus captures the same utterance on many legs simultaneously, so "present
   in every mic leg at the same aligned time" (speech or a room event) and
   "present only in the DTLN leg" (a processing artifact) are distinguishable
   facts rather than judgement calls.

## Consequences

- **The output is a queue plus reasons**, not a score. A single polished number
  hides localized damage — one click can matter more than a good average — so
  the report carries per-event timestamps, per-leg flags and the reason strings
  that produced the ranking.
- **A human listening pass is always in the loop**, which is the same posture
  the training program takes (ADR-0131): metrics rank, ears select.
- **Heavy models never run on the Pi.** Recording, metadata and cheap
  deterministic checks are the Pi's share of this work; neural metrics and
  review packages are laptop-side, after rsync. The 1 GB budget makes this a
  hard line, not a preference.
- **Detector behaviour is pinned by synthetic fixtures**, not by real-corpus
  spot checks — hard clip, soft clip, isolated click, click burst, dropout,
  repeated samples, DC offset, AGC pumping, and the negative cases (fricative,
  plosive) that a naive detector gets wrong.
