# ADR-0182: The VERIFY pilot-transfer ceiling rests on one clean multi-attempt session

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Measurement-honesty gate G3 refuses a VERIFY attempt as *inconclusive* when the
session's own summed-pilot transfer has stepped more than
`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB` (0.35 dB) since the sitting's first
usable attempt. The gate reads as a tolerance, and it is not: it is a
**recorder-drift detector**, sized against one night's hardware evidence, and
that evidence lives only in the comment above the constant
(`jasper/active_speaker/crossover_v2_flow.py:769-782`). The refactor
(`docs/REFACTOR-CUTOVER-2026-08.md` §6.1) moves that block, so the derivation
is extracted first. No code moves here; the comment shrinks to `See ADR-0182`
in the PR that moves its row (§6.1's 7305–7924).

## Decision

**The evidence base, quoted verbatim from `crossover_v2_flow.py:769-781`:**

> Measurement-honesty gate G3 (2026-07-22): the gate's OWN metric (summed-
> pilot transfer step) measured the phone's input chain stepping 0.75-0.82
> dB across the dishonest 1.192 → 2.111 → 2.835 dB VERIFY attempt sequence on
> 2026-07-22 hardware, producing verdicts that read as "speaker out of
> tolerance" when the recorder was what changed — the one clean multi-
> attempt session on the same rig stepped ≤0.05 dB by that SAME metric. (A
> separate, coarser frequency-differential estimate of the same drift put it
> at ~0.56 dB — kept only as secondary corroborating context; the pilot-band
> numbers above are what this gate actually measures and are the primary
> evidence.) VERIFY replays the IDENTICAL program through the IDENTICAL
> applied graph on every attempt, so its own leading pilot pair's transfer
> (captured level minus programmed gain) should not move between attempts
> either — a step this large is the input chain moving, not the speaker.

**The separation is what makes 0.35 defensible, and it is a gap, not a
percentile.** The dishonest session stepped 0.751 and 0.823 dB; the one clean
multi-attempt session in the corpus stepped 0.047 dB at worst. 0.35 sits
between them with margin on both sides — about 7× the clean session's worst
step, and about half the dishonest one's smallest.

**The limitation the primary source records, and this ADR carries forward.**
From `captures/xover-e0-2026-07-21/honesty-guards-proof-20260722/REPORT.md`'s
G3 section: only one clean multi-attempt session exists in the corpus, and a
phone-class chain was observed stepping ~0.33 dB without firing — *"recorded as
a known threshold consideration, not tuned tonight (n=1 clean session;
precision favored over recall for honest-refusal trust)."* **n = 1 on the
quiet side is the whole basis for the lower bound**, and any future re-tune
argues against that, not against the number.

**Two structural properties the number depends on, stated because a mover can
lose them silently.** The comparison is *inter-attempt within one sitting* —
identical program, identical applied graph, so reconstruction-gain error is a
per-session constant that cancels in the step. And the first usable attempt of
a session only records the reference; since
[#1927](https://github.com/jaspercurry/JTS/issues/1927) no prior session's
number can reach the gate, so a first attempt is structurally unable to fire it
and what the gate means is exactly what it measures: *the chain moved DURING
this sitting.*

## Consequences

- G3 refuses, and it is an INTEGRITY refusal under
  [ADR-0002](0002-measure-again-discriminator.md)'s discriminator: a stepped
  recording chain is a defect in the capture, and measuring again is what fixes
  it. The demotion that converted the ripple does not reach here.
- The ceiling is reused, never restated. `_note_level_reference_reset` compares
  across the session boundary and asks the same constant what a level move is,
  so *"materially different"* cannot drift from *"would have fired"* — but that
  comparison is **reported, never enforced**, and no verdict reads it.
- Phone-class chains can step up to ~0.33 dB without firing. That is a known
  and accepted recall gap; the threshold was set for precision, because a
  false honest-refusal costs more trust than a missed one.
- Deliberately given up: cross-phase coverage. A chain that steps between
  MEASURE and VERIFY attempt 1 and then holds is invisible to this gate — every
  attempt agrees with the others and the session fails VERIFY honestly as
  out-of-tolerance rather than dishonestly. The alternative needed trim and
  band acoustic modelling across two different pilot roles; that was rejected
  as the invasive shape, and the inter-attempt comparison is model-free.
- The constant's banner still reads *"PROVISIONAL pending W6 bench
  validation"*. Whether that label is spent is an open question for the owner,
  deliberately not answered here.
