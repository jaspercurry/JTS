# Prompts to take each non-A attribute to A

One self-contained prompt per attribute from the review's grade table. Paste a file's full contents as
the kickoff message of a fresh Fable session on this repo. Each prompt makes the three rules the owner
cares most about impossible to miss — Fable delegates to Opus/Sonnet and does no lane work; every PR
gets `/code-review` and `/simplify` before merge; every finding is re-verified at HEAD — and each ends
at a plan gate the owner triages before code is written.

| File | Issue | Attribute | From |
|---|---|---|---|
| `P1-secrets.md` | #4193 | Secrets (NN-3) | C+ |
| `P2-deploy-integrity.md` | #4194 | Deploy integrity (NN-4, NN-8) | C+ |
| `P3-resilience.md` | #4195 | Resilience | B |
| `P4-observability.md` | #4197 | Observability | B− |
| `P5-structure-and-god-files.md` | #4199 | Separation & SSOT, followability, god files | C+ / C |
| `P6-right-sizing.md` | #4200 | Right-sizing | C |
| `P7-tests.md` | #4201 | Tests | B− |
| `P8-docs.md` | #4202 | Docs and prose | B |

Hardware/audio safety is already A− and needs only R-016's belt-and-braces row (in P4's doctor work
and P3's clamp event). The tuning zone is another agent's; every prompt hands tuning-zone items over
as suggestions.

## Sequencing

Every lane starts with a scout and a one-page plan that stops at the owner's triage; those phases
never conflict, so all eight can be kicked off at once. The hard ordering is only about code
landing on `main`:

1. **P6 deletions → P5 moves → P5 splits.** Do not move what is about to be deleted; do not split a
   file that is about to move. P5's cycle fix, layers contract and deferred-import rule need no wait.
2. **P5 moves → P7 execution.** Tests travel with their modules in P5's PRs and die with their
   subjects in P6's; P7 rewrites only what neither list names until both have merged.
3. **P5 moves → P8's last PR** (the stale-path pass). Everything else in P8 starts now.
4. **P6's peering deletion → P3's `peering/state.py` fix**, if both are open at once.

| Batch | Lanes | Why together |
|---|---|---|
| 1 (now) | P1, P2, P6, P5 (plan, cycle fix, contract) | Disjoint files; P1/P2 are small and close the two non-negotiable seams; P6 clears the ground P5 moves on |
| 2 (as P1/P2 close) | P3, P4, P8, P5 (moves) | P3/P4 split `jasper/control/` by file (stated in their prompts); P8 is prose-only |
| 3 (after P5 moves merge) | P7, P5 (splits), P8 (stale-path pass) | Tests and docs follow the tree |

Four sessions at a time is a comfortable ceiling: every lane rebases before each push, and more
than that makes `main` thrash. The general steward (#4085) and the doctor/state steward overlap
P3–P6; point them at these issues or pause them while the lanes run. The Wave-6 systems rows in
the review stay with the doctor/state steward.
