# ADR-0015: Only an accepted verdict grades the round — a session ending on a terminal rejection writes no receipt

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

Round grading is fire-once: the first call wins and writes the round's receipt.
The post-apply phase has a retry budget, so a rejected capture does not end the
session — the household can replace it. That makes "which capture grades the
round?" a real decision rather than a detail.

The ruling lives in one inline comment,
`jasper/active_speaker/crossover_v2_flow.py:9788-9795`, and nowhere else. The
issue the prose cites is
[#2291](https://github.com/jaspercurry/JTS/issues/2291).

This ADR extracts it before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7). The refactor's `save`
verb inherits the fire-once receipt, and this is the one case where NOT banking
is the honest outcome — a useful counterweight to the INTEGRITY class's
must-still-bank rule (ADR-0002).

## Decision

**Grade the round only on an accepted verdict.** Quoted from
`crossover_v2_flow.py:9788-9795`:

> **Only on an accepted verdict**, and that is load-bearing rather than tidy.
> VERIFY has a retry budget, so a rejected capture does NOT end the session:
> grading one would burn the fire-once guard on evidence the household then
> replaced, and the session would finish carrying a receipt describing a capture
> it did not end on — demanding operator recovery for a round that went on to
> succeed. A session that ends on a terminal rejection writes no round receipt,
> which is the honest record: its post-apply evidence never completed.

Two rules, and they are not the same rule:

1. **A fire-once guard may not be spent on replaceable evidence.** Anything a
   retry can supersede is not what the one-shot record is about. This
   generalizes past receipts to every fire-once write in the engine.
2. **Absence is the honest record for an incomplete round.** A receipt
   describing evidence the session did not end on is worse than no receipt,
   because it demands recovery for a round that succeeded.

**This is not a carve-out from must-still-bank.** ADR-0002's rule is that a
refusal must bank *what it measured* — the capture, its integrity verdict, its
reason. Rule 2 here is about the round's *summary* verdict, which is a
different artifact: the evidence banks, the summary does not exist yet.

**Where grading happens follows where the last evidence is.** The grade fires
at this seam only when the session plans no post-apply group; when it does, the
group's close grades it, *"from a call that cannot see this capture."*

## Consequences

- The engine's `save` verb writes a round receipt only for a round whose
  evidence completed. A consumer reading receipts can treat presence as "this
  round finished", which is what makes the record worth reading.
- Every fire-once write owes the same question: can a retry replace the thing
  this is about? If yes, it is not eligible to spend the guard.
- Absence has to be a readable state downstream. A reader that treats "no
  receipt" as an error reintroduces the operator-recovery demand from the other
  side.
- Deliberately given up: a receipt for every session. Sessions that ended on a
  terminal rejection are visible in the journal and the capture records; what
  they do not get is a summary verdict claiming a round completed.
