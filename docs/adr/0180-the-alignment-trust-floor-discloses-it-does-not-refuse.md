# ADR-0180: The alignment trust floor discloses; it stopped refusing at the nanny burn-down

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

`ALIGNMENT_CONFIDENCE_TRUST_FLOOR` (0.6) used to refuse a MEASURE capture whose
GCC-seed/capture confidence fell below it, and to spend one of the household's
retries doing so. The nanny burn-down demoted it. That demotion is recorded in
exactly one place — the comment above the constant in
`jasper/active_speaker/crossover_v2_flow.py:688-697` — and the refactor
(`docs/REFACTOR-CUTOVER-2026-08.md` §6.1) moves that block, so the ruling is
extracted before its carriage moves.

**Two live restatements say the opposite, and this ADR is what settles them.**
The ripple essay 46 lines further down still lists the trust floor among the
checks that *"still REFUSE"* (`crossover_v2_flow.py:743-745`), and
[ADR-0002](0002-measure-again-discriminator.md) repeats that line from the same
source. Both were true when written and are stale at HEAD; the flow's own
module docstring already carries the correction
(`crossover_v2_flow.py:70-72`).

This ADR moves no code. The comment it quotes shrinks to `See ADR-0180` in the
PR that moves its row (§6.1's 4627–5279), not here.

## Decision

**The floor is a disclosure trigger. A capture below it is ACCEPTED, and the
confidence is banked as a reservation.** Quoted verbatim from
`crossover_v2_flow.py:688-697`:

> Below this GCC-seed/capture confidence (see ``AlignmentEstimate.confidence``
> and ``confidence_source`` in ``program_analysis.py``), the capture is
> ACCEPTED and the confidence is banked as a reservation — see
> ``_note_alignment_confidence_reservation``. **It is a disclosure trigger, not
> a gate**, since the nanny burn-down: it refused MEASURE and spent a retry
> until then, on a number this file's own comment called PROVISIONAL pending
> W6 bench validation, and the one live bench datum undercut it (two captures
> at ~0.677, one accepted and one refused 58 s apart). Converting it did not
> recalibrate it — 0.6 is the same number, and only what crossing it does
> changed.

**Conversion is not recalibration.** 0.6 survives as the threshold; only the
consequence of crossing it changed. That is the same rule
[ADR-0002](0002-measure-again-discriminator.md) states for the ripple, applied
to the check ADR-0002 named as still refusing.

**What the demotion did NOT touch, because the delay backstop is a different
question.** `ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS`
(`crossover_v2_flow.py:699-711`) still refuses, and it exists precisely because
a high confidence at the wrong lag clears this floor — the two are not
redundant, and demoting one says nothing about the other.

## Consequences

- A room or a rig that simply reads below 0.6 no longer costs a household a
  retry. The confidence rides the receipt instead, which is what
  [ADR-0002](0002-measure-again-discriminator.md)'s discriminator asks for: a
  low correlation describes the capture's evidence, and measuring again at the
  same placement does not raise it.
- The alignment term the fit builds on can be less trustworthy than usual and
  the round proceeds anyway. The disclosure is logged at WARNING, not INFO, so
  an operator reading a session does not have to know to look for it.
- **Superseded here:** any reading of `crossover_v2_flow.py:743-745`, or of
  [ADR-0002](0002-measure-again-discriminator.md)'s *"the alignment trust
  floor … still refuse"* sentence, as current behaviour. ADR-0002 is immutable
  and reads as of its date, so this ADR is the later record; the stale comment
  line is not, and it should die with the essay that carries it (§6.1's row
  4627–5279).
- Deliberately given up: the guarantee that no candidate is ever fitted on a
  weakly-correlated alignment estimate. The estimate's own confidence is
  disclosed instead, and the delay-plausibility backstop still catches the
  failure mode that actually damages a tune — a confident answer at the wrong
  lag.
