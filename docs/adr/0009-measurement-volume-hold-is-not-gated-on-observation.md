# ADR-0009: The measurement-volume hold is the safety ledger's integrity, not forensics — it is never gated on a diagnostics flag

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

A measurement session declares a volume, and its excitation-safety ledger
admits each program *against that declared volume*. Before every stimulus the
session re-proves the fader is still there. Several other per-capture records
in the same path — provenance, for one — are gated on an `observing` flag,
because they are diagnostics.

The rule that the volume hold is *not* one of those lives in one closure's
docstring, `jasper/web/correction_crossover_v2.py:5672-5684`. The drift-refusal
*mechanism* is documented (`docs/HANDOFF-crossover-measurement-v2.md:3819`,
`docs/HANDOFF-volume.md:689`); the forensics-versus-safety-ledger distinction,
and the "not gated on `observing`" rule it produces, are not.

This ADR extracts it before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7). The refactor stands up a
single volume owner with one write door (§3 wave 5a), which makes "may this
door be behind a flag?" a question that gets asked again.

## Decision

**The hold is one discipline, owned by the plan, and never conditional.**
Quoted from `correction_crossover_v2.py:5675-5681`:

> The ONE volume discipline both playback shapes use (#2925), owned by the plan
> (``SessionVolumePlan.hold_measurement_volume``) because only the plan can
> serialize it against its own drains. NOT gated on ``observing`` the way
> provenance is: that record is forensics, this is the safety ledger's own
> integrity — ``readmit_program_from_wav`` admitted this program against the
> DECLARED volume, so a stimulus emitted at any other level was never the one
> that was admitted.

The distinction that decides it: **a forensic record describes what happened
and may be sampled; a safety-ledger fact is a precondition of the thing
happening at all and may not.** Anything an admission decision was made
*against* belongs to the second class.

**Serialization belongs to the level's owner.** Only the plan can order the
hold against its own drains, so the hold lives with the plan rather than at
each call site. A second holder is a second answer to "what level is in effect
right now".

**No level to prove is the plan's answer to give, and it discloses.** *"``None``
means the plan holds no volume to prove; that is its question to answer, so this
discloses and plays on."* Absence of a declared level is not a defect in this
capture — ADR-0002's test says disclose, not refuse.

## Consequences

- The engine's single volume owner may not put its re-prove behind a
  diagnostics or verbosity flag. Every stimulus in a session that declared a
  level is proven against that level.
- The forensics-versus-integrity split generalizes: when deciding whether a
  per-capture record may be sampled, ask whether an admission decision was made
  against it.
- Serialization stays with whoever owns the level, so the ranked-claim design
  inherits the constraint that one owner orders holds against its own releases.
- Deliberately given up: the cheapest possible capture loop. The hold is a read
  and possibly a write per stimulus, on a path where the alternative is
  emitting a stimulus nothing admitted.
