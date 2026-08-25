# ADR-0007: Refuse, don't mislead — a gated session never prompts a pose its mover cannot reach

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

When a geometry verdict wants a retake, the session prompts the mover to a
wider spot. That prompt is written for a person walking. When the session's
begins are gated — held until something releases each `(index, attempt)` —
the mover may be an external positioner with a reachable envelope, and the
prompt asks for a pose outside it.

The owner's ruling is stated once, in a comment inside the retake branch,
`jasper/active_speaker/crossover_v2_flow.py:7807-7827`. Nothing in `docs/`
carries it.

Both of the refactor's front ends reach this branch — the wizard and the
LLM-plus-robot-arm runner differ only in who moves the mic — so the ruling
binds the engine rather than either front end.

## Decision

**Refuse rather than prompt, and the predicate is the gate, not the mover.**
Quoted from `crossover_v2_flow.py:7808-7811`:

> REFUSE rather than prompt (owner ruling: refuse, don't mislead) — for EITHER
> gated shape, because what decides is "these begins are HELD", not "an arm is
> moving".

**Three dishonest things, and the third is the gate's own.** From
`crossover_v2_flow.py:7810-7823`:

> Prompting did three dishonest things at once, and the third belongs to the
> gate, not the arm:
>
> 1. it asked for a pose an external positioner cannot reach —
>    ``CLOUD_GEOMETRY_RETRY_PROMPTS`` rung 1 is 75 cm off the mark, past every
>    pose in the walk, and rung 2 goes ABOVE it;
> 2. it recorded that un-made pose's 75 cm offset as the position's durable
>    evidence; and
> 3. the retry re-authorizes the SAME plan entry, so the position gate
>    republishes that entry's ORIGINAL bearing while the screen names the wider
>    spot. Two answers to where the microphone should be — and a person, who
>    COULD walk to the wider spot, is exactly who cannot be told which to
>    believe.

Item 3 is why the rule is keyed on the gate: a *person* under a gated shape is
harmed by the same defect, because the gate and the screen disagree about where
the microphone should be.

**A refusal here spends nothing.** From `crossover_v2_flow.py:7825-7827`:

> The retry budget is deliberately NOT spent and no take is dropped — nothing
> here is a retry, so the group keeps the evidence it legitimately has for
> whatever the session does with it next.

## Consequences

- The engine may not prompt a pose outside the mover's declared envelope. A
  preset that names poses an arm cannot reach refuses at the point the pose
  would be asked for, not silently at the arm.
- Budget accounting follows the same rule everywhere: something that is not a
  retry does not spend a retry, and does not drop evidence already earned.
- The mover-agnostic framing is load-bearing. A future check written as "is an
  arm moving?" reintroduces the defect for a gated human session; the question
  is always "are these begins held?"
- Deliberately given up: the chance that a wider retake succeeds anyway. A
  session that records an un-made pose as durable evidence is worse than a
  session that stops and says so.
