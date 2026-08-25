# ADR-0016: The reference mark is a stable identity, not a coordinate — and it has exactly one owner

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

A round compares a before capture against an after capture. `program_id`
equality cannot see position — a capture taken a metre away replays the
identical program — so the comparison needs a second identity axis saying
*where* the two sides were measured.

The ruling that defines what that axis does and does not claim lives on the
constant itself, `jasper/active_speaker/crossover_v2_flow.py:325-346`, and
nowhere else. The issue the prose cites is
[#2291](https://github.com/jaspercurry/JTS/issues/2291).

This ADR extracts it before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7). The refactor's ONE
CAPTURE RECORD makes place a first-class field (§1), so what a place identity
claims is a contract question the new record has to answer.

## Decision

**One owner, because both sides must stamp the same string.** Quoted from
`crossover_v2_flow.py:336-340`:

> **One owner, deliberately.** Both sides must stamp the SAME string or every
> round grades
> :data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_MARK_MISMATCH`,
> so the post-apply side imports this constant rather than spelling the literal
> a second time.

**It is an identity, not a coordinate — and the claim it makes is narrow.**
From `crossover_v2_flow.py:340-345`:

> It is a stable identity, not a coordinate: nothing measures where the mark
> physically is, and the flow makes no claim that two sessions' marks are the
> same place — only that within ONE round the mic did not move between the two
> captures, which is what the round's own choreography (baseline last in stage
> 1, VERIFY first in stage 2, no prompted move between them) is for.

Three things this pins:

1. **Scope of the claim is one round.** Cross-session comparison on mark
   equality is not licensed by this field, and reading it that way would be
   comparing two places nothing measured.
2. **The guarantee comes from choreography, not from measurement.** The mark
   holds because the round is ordered so nothing prompts a move between the two
   captures. Change that ordering and the identity stops meaning anything,
   silently.
3. **A comparison whose marks disagree is refused, not scored.** Two captures
   from different places are not a before/after pair — the same frame rule
   ADR-0003 states for a gate's two terms, applied to place.

## Consequences

- The engine's capture record carries place as an identity with a stated scope.
  A future coordinate-valued place field is a *different* field with a
  different claim, not an upgrade of this one.
- Any reordering of a round's capture sequence has to re-examine the mark's
  guarantee. The invariant is "no prompted move between the pair", and it lives
  in the choreography rather than in a check.
- One owner per identity string, imported rather than re-spelled. A second
  literal is a silent every-round mismatch.
- Deliberately given up: cross-session before/after comparison. Getting that
  would require measuring where the mark physically is, which nothing does.
