# ADR-0181: The ripple disclosure's corpus is thirteen captures, counted in one place

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

[ADR-0002](0002-measure-again-discriminator.md) already owns the *ruling* that
converted `MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB` from a refusal into a
disclosure — the owner's 2026-08-03 decision on
[#2087](https://github.com/jaspercurry/JTS/issues/2087) and the discriminator
it generalizes. It does **not** carry the threshold's *evidence base*: the
incident that set 15.0 dB, the corpus that bounds it, and the frame it is
calibrated in. Those live only in the comment above the constant
(`jasper/active_speaker/crossover_v2_flow.py:713-727` and `:752-763`), and the
refactor (`docs/REFACTOR-CUTOVER-2026-08.md` §6.1) moves that block.

**One paragraph of it is load-bearing in a way a reviewer will not guess.** The
comment declares itself the single source for the corpus composition and names
the recurrence that made it so:
[#2015](https://github.com/jaspercurry/JTS/issues/2015) traced a wrong
restatement of the count to a copy that dropped one capture. Deleting the
paragraph into git history restores exactly that drift; re-typing it anywhere
creates the second copy it exists to prevent. This ADR becomes the one citable
copy, and the comment shrinks to `See ADR-0181` in the PR that moves its row
(§6.1's 4627–5279) — not here. No code moves in this ADR's PR.

## Decision

**The incident, the corpus, and the margin — quoted verbatim from
`crossover_v2_flow.py:713-727`:**

> Measurement-honesty disclosure G1 (2026-07-22; converted from a refusal to a
> disclosure by owner ruling on 2026-08-03, issue #2087): a corrupted
> phone-chain MEASURE capture on 2026-07-22 hardware built a candidate whose
> ``predicted_ripple_db`` was 27.316 dB at an alignment confidence (0.703) that
> cleared ALIGNMENT_CONFIDENCE_TRUST_FLOOR above — the candidate auto-applied,
> then failed three VERIFYs at 5.3-6.7 dB. Every clean MEASURE that same day
> predicted 4.387-9.031 dB — 13 captures precisely: 4 on UMIK-2, 8 on
> iMM-6C, 1 accepted phone-chain measure. This composition is OWNED here;
> cite this comment rather than re-quoting a count (issue #2015 traced a
> since-corrected 12-capture, two-chain restatement elsewhere to a copy
> that dropped the phone measure). Primary source: that night's own
> retention sidecars, tabulated in ``captures/xover-e0-2026-07-21/
> honesty-guards-proof-20260722/REPORT.md``'s G1 table. This threshold sits ~6
> dB above the clean corpus's worst case and ~12 dB below the corrupt one —
> wide margin on both sides.

**The composition is now OWNED HERE, and the rule that came with it comes too:
cite this ADR rather than re-quoting the count.** Thirteen: four UMIK-2, eight
iMM-6C, one accepted phone-chain measure. A second written copy of that
breakdown anywhere in the tree is the #2015 defect, whatever it is attached to.

**The frame the number is calibrated in, verbatim from
`crossover_v2_flow.py:752-763`** — a corpus threshold only compares to captures
measured the same way, so the frame is part of the threshold:

> THE FRAME THIS NUMBER IS CALIBRATED IN, named because a corpus threshold is
> only comparable to captures measured the same way: the zero-residual summed
> branch sum at the polarity the candidate SHIPS. The delay stays pinned at
> zero residual — that is the documented evasion channel, since a candidate's
> own alignment could otherwise lower its own disclosure. Polarity is not a
> continuum a capture can shop along, and since #2598 it is a SELECTED
> quantity, so scoring coherence at a polarity the candidate does not ship
> would make a fine capture read as an incoherent one (the 2026-08-15 inverted
> rounds reported 14.13 dB for a pair that sums to a fraction of a dB the right
> way round). The 2026-07-22 corpus predates that selection, but every capture
> in it was graded at the polarity its own candidate shipped, so the frame is
> the same one.

## Consequences

- The count has one written home. A document, a comment or a report that needs
  it links here; nobody re-derives it from the capture directory, and nobody
  restates it in prose. That is the whole mechanism #2015 bought.
- The threshold's margin is legible: 15.0 dB sits ~6 dB above the clean
  corpus's worst case (9.031 dB) and ~12 dB below the corrupt one (27.316 dB).
  Moving it means re-arguing against those two numbers, not against a feeling
  about ripple.
- The frame is a precondition, not a footnote. A future scorer that grades
  coherence at a non-shipping polarity, or that lets a candidate's own
  alignment move the residual, is comparing against a corpus it no longer
  matches — and the 2026-08-15 inverted rounds are the recorded instance of
  exactly that reading 14.13 dB for a pair that sums fine.
- Deliberately not restated here: the *ruling* — why crossing this threshold
  discloses rather than refuses. That is
  [ADR-0002](0002-measure-again-discriminator.md)'s, and duplicating it would
  rebuild the drift this ADR exists to stop.
- The constant's banner still reads *"PROVISIONAL pending W6 bench
  validation"*. Whether that label is spent is an open question for the owner,
  deliberately not answered here.
