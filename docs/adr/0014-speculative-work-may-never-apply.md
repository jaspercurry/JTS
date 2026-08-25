# ADR-0014: Speculative background work may never apply — and its failure is a non-event, never a capture failure

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

The fit is the slowest thing in a session. A rider starts it on a background
thread while the household is still walking the position group, instead of
waiting for them to tap Continue. This file previously ran
`handle_v2_apply` on a thread; that is gone.

Both rules — what the thread may do, and what its failure means — live in one
function docstring, `jasper/web/correction_crossover_v2.py:5919-5935`.
`docs/HANDOFF-crossover-measurement-v2.md:6136` records the rider's no-hold
property but neither of these.

This ADR extracts them before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7).

## Decision

**One background thread, and it cannot apply.** Quoted from
`correction_crossover_v2.py:5919-5926`:

> **This is the ONLY background thread stage 1 starts, and it cannot apply.**
> Its target computes a candidate and banks it on the conductor; making that
> candidate real still needs the household's own confirmation, and applying it
> still needs their separate POST from the review screen (work order D1). The
> ``handle_v2_apply``-on-a-thread this file used to run is gone and is not
> coming back through here — pinned by ``test_no_session_path_applies_anything``,
> which reads this function's source as well as the runner's.

Two separate human acts stand between speculative work and a live graph: the
confirmation that makes a candidate real, and the POST that applies it. The
`save` verb is never reachable from a speculative path.

**A speculative result is an optimisation the correct path never depends on.**
From `correction_crossover_v2.py:5928-5932`:

> Fire-and-forget by design: every reason not to run is checked inside
> ``run_speculative_group_close`` under the conductor's own lock, and the result
> is an optimisation the confirm path never depends on. A thread that cannot be
> started is therefore a logged non-event — the confirm fits exactly as it did
> before this rider — and never a capture failure.

The test is falsifiable: delete the speculative path entirely and the session
must still be correct, only slower. Anything that fails when the thread does
not run was never an optimisation.

**Eligibility is decided under the owner's lock**, not at the call site — a
caller deciding whether to start speculative work is a second decision-maker
about session state.

**Daemonised**, so a session torn down mid-fit *"cannot hold the process open
waiting for work whose answer nobody will read."*

## Consequences

- The engine may run `analyze` and `recommend` speculatively and must never
  reach `save` from that path. The plan already fences the apply transaction as
  never-a-refactor-target; this is the rule that keeps speculative work on the
  correct side of that fence.
- Speculation is deletable by construction. That is the property that makes it
  safe to add more of it as the inner loop gets cheaper.
- A background failure is logged and dropped. Turning it into a capture failure
  would make an optimisation load-bearing.
- Deliberately given up: the wall-clock win when the speculative fit does not
  land. The session pays the fit at confirm, exactly as it did before.
- Note for whoever moves this code: the pin (`test_no_session_path_applies_anything`)
  reads the function's *source text*, which the charter's test policy forbids
  writing anew. It is a genuine invariant needing a behavioural pin instead —
  triage it when the wave moves this function, not before.
