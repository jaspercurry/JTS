# ADR-0003: A gate's two terms must be the same instrument at the same position — the prediction gate's frame

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

PR-L4 item 2 spec-grades a candidate's predicted response before the session
applies it. Its first cut compared the MODEL's residual against the MEASURED
in-room cloud's. Adversarial review finding B1 showed that comparison made the
verdict a property of the room rather than of the correction, and the fix was a
frame change, not a threshold tune.

The argument lives in a test docstring —
`tests/test_crossover_v2_conductor.py:9426-9438`, on the parametrized
regression that pins it — and in a compressed form in
`docs/linearization-integrity-plan.md:147-153`. Both are inside the tuning
refactor's blast radius: the test rides the conductor suite the strangler
rewrites, and the five-file linearization plan family collapses to one archived
record in wave 7f (`docs/REFACTOR-TUNING-2026-08.md` §3). Neither the flow's
own gate prose (`jasper/active_speaker/crossover_v2_flow.py:9046-9111`) nor the
module that implements it
(`jasper/active_speaker/crossover_v2/accountability.py`) states why the frame
is what it is. `jasper/active_speaker/crossover_v2/verification.py:56-60` names
the comparison "model-vs-model" and calls itself its "*measured* twin", and
`jasper/active_speaker/attempts_loop.py:424-427` enforces the rule as a
refusal — both use the frame without arguing it.

This ADR extracts the argument before the code that carries it moves
(§0 rule 1, §6 R7).

## Decision

**A gate's before-term and after-term must be the same instrument at the same
position.** Quoted from `tests/test_crossover_v2_conductor.py:9428-9438`:

> PR-L4 review B1, the regression that motivated the frame change.
>
> The first cut compared the model's residual against the MEASURED in-room
> cloud's, which made the verdict a function of the ROOM: holding the
> correction constant and varying only the pre-apply measurement flipped a
> passing session into the gate's failing arm (a refusal at the time; a
> ``not_an_improvement`` ledger entry since the nanny burn-down), and every
> BETTER room fared worse. Both of the gate's terms are now the same instrument
> at the same position, so scaling the room's own measured response — the only
> thing this parametrization changes — must not move the verdict at all.

The gate therefore grades the RAW pre-fit predicted sum against the LINEARIZED
predicted sum through one evaluator: a same-instrument before/after.
`docs/linearization-integrity-plan.md:147-153` records the consequence for the
number:

> Item 2's gate grades the RAW pre-fit and the LINEARIZED predicted sums
> through one evaluator — a same-instrument before/after. An earlier revision
> compared the model against the measured in-room cloud; adversarial review
> showed that made the verdict a function of the ROOM (holding the correction
> constant, better rooms refused harder), and the threshold fell from 1.5 dB to
> 0.5 dB with the frame change because the comparison no longer has to absorb a
> cross-frame gap.

**The threshold is downstream of the frame.** A cross-frame comparison has to
absorb the gap between its two instruments, and the only place to put that
slack is the threshold. Naming the frame is what let 1.5 dB become 0.5 dB;
a threshold moved without naming a frame change is a tell that the comparison
is absorbing something.

**Grading a thing against itself is arithmetic, not evidence.** From
`jasper/active_speaker/crossover_v2/accountability.py:728-739`, the abstention
this frame forces when no fit ran:

> No fit ran this attempt (ineligible mic tier, or the fit failed into SF2's
> trims-only fallback), so `predicted_sum` IS `raw_predicted_sum` — the same
> object. Grading a thing against itself always returns "no improvement", which
> would file every trims-only candidate under
> :data:`LEDGER_NOT_AN_IMPROVEMENT` on the strength of arithmetic rather than
> evidence — a false entry in the ledger even now that it is only an entry.
> Abstain, loudly

**The same rule already refuses cross-frame grade comparisons**, and that
enforcement point outlives the god files — `attempts_loop.py:424-427`:

> ``provenance``: :data:`PROVENANCE_MODEL_GRADED` or
> :data:`PROVENANCE_REALIZED`. Comparing across the two is refused — a
> predicted grade and a measured one are different instruments, and the
> household wire requires deltas labelled model-vs-model.

A model-vs-model figure is a forecast about the correction, not a claim about
what the speaker will measure. Copy presenting it as the latter is a claim no
instrument in the session made.

## Consequences

- The verdict is a property of the correction, so a better room can no longer
  fare worse. The pinning test is a parametrization over the pre-apply
  measurement's scale, which is the mutation the defect would have to
  reintroduce.
- Any new gate in the engine owes an explicit statement of its two terms'
  frame. "Both terms are the same instrument at the same position" is
  checkable; "the numbers looked right" is not.
- The measured before-vs-after comparison is a *different* gate, not this one,
  and it belongs after the apply where both of ITS terms are measured. Keeping
  the two apart is what stopped a forecast from vetoing the measurement that
  would have settled the question.
- Deliberately given up: the ability to catch a correction that models well and
  measures badly *before* it plays. That is what the measured round with its
  pre-registered keep/rollback is for.
