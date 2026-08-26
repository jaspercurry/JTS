# W1 cutover briefs: the RecordStore and the retention lift

> **Scope.** The implementation-grade briefs for the cutover DAG's entry
> items — `W1-a` (the production `RecordStore`), `W1-b` (the thirteen fields),
> `W1-c` (the four-site retention lift) and `W1-d` (the 4j index) — as scheduled
> by [`REFACTOR-CUTOVER-2026-08.md`](REFACTOR-CUTOVER-2026-08.md) §1 and §7.
>
> **This document does not re-plan.** §1 owns *what* and *why*; this owns
> *exactly where and in what order*, at a grain a builder can execute without
> re-deriving. Where a derivation here disagrees with §1, the disagreement is
> recorded in the ledger below rather than papered over.

*(sections land incrementally — see the branch's commits)*
