# ADR-0018: `jasper/bass_extension/` stays PARKED — neither wired up nor deleted

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

The bass-extension program's apply pathway is built and unwired. The 2026-08-25
deep audit listed its parked half as a deletion candidate
(`docs/DEEP-AUDIT-2026-08-25.md:79`, ~4,600 lines plus 715 optional test lines),
noting that *"deadness is structurally enforced by its own tests"* and that the
one CLI that could wire it in unconditionally raises `SystemExit` naming
[#1738](https://github.com/jaspercurry/JTS/issues/1738). The tuning refactor's
plan then inherited the package by boundary reconciliation and framed it as a
binary owner call — *"wire `jasper/bass_extension/` up, or delete it"*
(`docs/REFACTOR-TUNING-2026-08.md:1102`).

That framing offered two options. The owner chose a third.

Structurally-enforced deadness plus a deletion mandate is exactly the shape a
future right-sizing pass reads as an obvious cut. This ADR exists so it does
not.

## Decision

**The owner ruled on 2026-08-25: the package stays parked.** In his words:

> I want to leave it parked. I know that kind of goes against my ethos, but I
> would prefer to keep the base program parked.

Not wired. Not deleted. The current state is the intended state.

**What "parked" means, concretely:**

1. **The apply pathway keeps zero production callers, by design.**
   `apply_bass_extension`, `bypass_bass_extension`, and
   `recover_pending_bass_extension_apply` are unreachable from `jasper/` except
   for one internal delegation. This is the state `docs/tuning-master-plan.md`
   ticket 4.4 already records, and it is now a ruling rather than a status.
2. **The deadness-enforcing tests stay, and they are the park's enforcement.**
   `tests/test_bass_extension_plan_status.py::test_wave3_transactions_have_no_production_callers`
   walks every `jasper/**/*.py` by AST and fails if any of the three entry
   points acquires a caller; its sibling
   `test_readme_does_not_claim_bass_extension_has_no_code` asserts the three
   unshipped surfaces (`jasper/web/bassext_backend.py`,
   `jasper/bass_extension/scheduler.py`, `jasper/bass_extension/runtime.py`)
   do not exist. Under the refactor's S7 rule a deadness-enforcing test dies in
   the same PR as its subject — the subject is not dying, so neither do they.
   They are no longer "proof of an orphan"; they are the mechanism that keeps
   the park a park.
3. **The wire-up spec is not lost.** It lives in
   [#1738](https://github.com/jaspercurry/JTS/issues/1738) and in
   `docs/tuning-master-plan.md` ticket 4.4, which also names the two loose ends
   a resumption inherits: the bench migrating onto the hardened `play_program`,
   and `bench/excitation.py` as a zero-importer module to wire or delete when
   bass resumes.
4. **Scope, measured rather than repeated.** The package is 10,374 lines of
   Python under `jasper/bass_extension/` (bench subtree 5,910; top level 3,536;
   adapters 928) against 10,177 lines under `tests/test_bass_extension*.py`.
   The audit's ~4,600 + 715 is its *parked half* — `limiter_evidence.py`,
   `bench/executor.py`, their tests, and the `__init__` apply pathway — not the
   package. The park covers the package.
5. **The refactor's line budget never counted it.** `jasper/bass_extension/`
   appears in no row of the plan's net-lines table; the −90,000 floor stands
   without it, and the plan states the package *"blocks nothing and nothing
   blocks it."* Parking costs the refactor no target.

**Changing this needs a fresh owner ruling.** A future wire-up or deletion is a
new ADR superseding this one. A right-sizing pass, a deletion mandate, an
orphan sweep, or an audit finding is not sufficient authority — those are the
inputs this ruling was made against.

## Consequences

- ~20K lines of Python and test stay in the tree earning nothing today. That is
  the cost, named, and the owner accepted it against his own stated ethos.
- The deletion mandate has an explicit, enumerated exception. Agents reading
  "leave every file smaller than you found it" now have a place that says why
  this one is exempt, instead of re-litigating it every sweep.
- The park is cheap to hold precisely because the deadness is enforced: the
  package cannot quietly acquire a caller and become half-wired, which is the
  state that would actually cost something.
- Rejected: deleting now and recovering from git history later. Recovery is
  possible but the design context is not — the wire-up spec, the frozen limiter
  protocol revision, and the hardware prerequisites are what make the package
  worth more than its lines.
