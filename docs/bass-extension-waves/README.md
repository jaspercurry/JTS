# Bass Extension — wave prompts (Codex delegation kit)

This directory holds one self-contained implementation prompt per
wave of the Bass Extension program. The architecture plan of record
is [`docs/HANDOFF-bass-extension-plan.md`](../HANDOFF-bass-extension-plan.md);
these prompts are the execution contracts derived from it.

**Operator usage:** start a fresh Codex session per wave and say
"Read `docs/bass-extension-waves/wave-N-….md` and execute it." Do not
hand Codex more than one wave. Waves must land in order (each PR
merged before the next starts) except where a prompt says otherwise.
A Wave 5 lane launched after Wave 3 is currently only a mandatory
preflight/stop audit; it is not implementation authority while Wave 4's
bench limiter evidence remains unresolved. The pure total-refusal producer
skeleton authorized by the
[`limiter-evidence-protocol.md`](limiter-evidence-protocol.md) amendment is not
Wave 4 production authority. Wave 4 contract Revision 9 additionally authorizes
one hardware-free commissioning slice (the pure state machine to an in-memory
`review`, an injected synthetic dry run, and synthetic evidence intake through
that producer); hardware playback, live CamillaDSP mutation, persistence, the
real bench runner, and all production wiring remain blocked pending Jasper's
accepted bench bundle and a later reviewed revision.
Across the frozen contracts, bond entry may preserve and re-prove an
already-accepted sealed profile's natural pair on the existing local
active-speaker driver-domain graph, but profile mutation and Wave 5
runtime patching remain refused while bonded until multi-Camilla
ownership is separately proved.

The reviewed amendment that authorizes *building* the limiter-evidence bench
runner — the producer's on-device evidence source, still a contract until
Jasper's bench pass and a later Wave 4 revision name its accepted bundle — is
[`limiter-bench-runner-protocol.md`](limiter-bench-runner-protocol.md).

| Wave | Prompt file | Executor | Prereqs |
|---|---|---|---|
| 0 | `wave-0-hardware-spikes.md` | **Operator + hardware** (not Codex) | none — run early |
| 1 | `wave-1-numerics.md` | Codex | none |
| 2 | `wave-2-profile-observability.md` | Codex | Wave 1 merged |
| 3 | `wave-3-graph-emission.md` | Codex | Waves 1–2 merged + Wave 0 memo |
| 4 | `wave-4-commissioning-backend.md` | Codex | Waves 1–3 merged + crossover-program hardware burn-in (met); contract rev 9 authorizes only the hardware-free commissioning slice; production wiring still requires accepted limiter bench evidence |
| 5 | `wave-5-runtime-scheduler.md` | Codex | Waves 2–3 merged + Wave 0 memo for the mandatory stop audit; implementation additionally requires merged Wave 4 limiter producer + replacement Wave 5 prompt |
| 6 | `wave-6-ui.md` | Codex | Wave 4 merged (Wave 5 for live status) |
| 7 | `wave-7-hardware-validation.md` | Operator, Codex assists | everything |

Every wave prompt **incorporates this README by reference**. Codex:
read this file completely before your wave file.

**Program coordination** may be run by a dedicated Codex coordinator
session — its master prompt, merge-gate duties, sequencing rules, and
escalation boundaries live in [`coordinator.md`](coordinator.md). The
coordinator launches the wave sessions; it never implements.

---

## The engineering charter (binding for every wave)

You are implementing a specified slice of a reviewed architecture.
The review bar is a staff maintainer who values small, boring,
correct diffs. These rules are hard constraints, not suggestions:

1. **The file allowlist is absolute.** Each wave prompt lists the
   files you may create or modify. If completing the spec seems to
   require touching any other file, STOP and report (see protocol
   below). Do not "just quickly" edit a neighbor.
2. **Smallest diff that satisfies the spec.** Rough line budgets are
   given per wave; overshooting a budget by more than ~50 % means you
   have overbuilt — go back and delete. When two designs both satisfy
   the spec, pick the one with less code and note the rejected
   alternative in one sentence in the PR description.
3. **No new abstractions beyond those named in the prompt.** No base
   classes, factories, registries, plugin systems, wrappers, or
   "manager" objects that the prompt does not name. The prompt's
   dataclasses and function signatures are frozen — implement them
   exactly; do not add fields, parameters, or variants.
4. **No speculation.** Nothing for a future wave, no configuration
   knobs the spec doesn't name, no `TODO` scaffolding, no
   defensive handling for states the system cannot reach. If you
   catch yourself writing "this will be useful later," delete it.
5. **No new dependencies, env vars, daemons, threads, subprocesses,
   or asyncio tasks** unless the wave prompt names them.
6. **Do not refactor, rename, reformat, or "improve" existing code.**
   Match the local style of the files you touch, even where you'd do
   it differently. Additions to existing modules are append-only
   unless the prompt says otherwise.
7. **Reuse before writing.** Each prompt names the existing helpers
   to build on. If you find yourself re-implementing something
   (fingerprinting, atomic writes, band levels, smoothing), stop and
   look again — it almost certainly exists and the prompt or
   `docs/testing-tooling.md` names it.
8. **Ambiguity protocol.** If the spec is ambiguous on a
   non-safety point, choose the *simpler* interpretation and record
   the choice in the PR description. If the ambiguity touches audio
   safety (levels, filters, protection, emission), STOP and report
   instead.
9. **Tests are part of the deliverable, and they are pinned in the
   prompt.** Write exactly the listed test coverage (more cases are
   welcome; fewer is a failure). Deterministic, hardware-free, no
   network, no sleeps. Mirror the style of the named exemplar test
   files.
10. **Every value must trace to the prompt, the plan, or existing
    code.** Do not invent thresholds, defaults, ranges, or magic
    numbers. If a needed value is missing, that is a stop-and-report,
    not a judgment call.

## Preflight protocol (run before writing any code)

1. `git fetch origin main` and branch from `origin/main`. Record the
   base SHA; it goes in your PR description.
2. Verify every item in the wave prompt's **Preflight facts** list
   (files exist, functions/constants are importable under the stated
   names). One command per fact is fine (`git grep`, `python -c
   "import …"`).
3. If any fact fails: **do not adapt silently.** Stop and report
   which fact drifted. The prompt was written against a moving main;
   drift means the prompt needs a revision, not that you should
   improvise around it.
4. Read the wave's **Required reading** in the order listed. Skim is
   fine for context items; read carefully where the prompt says so.

## Process rules

- Branch name: `codex/bass-ext-wave-N-<slug>`.
- Acceptance commands assume the repo venv at `.venv/` — if your
  checkout lacks one, create it first (`python3 -m venv .venv &&
  .venv/bin/pip install -e ".[full]"`); do not substitute a system
  pytest.
- Run `scripts/test-fast` before every push; run the wave's
  **Acceptance commands** before opening the PR.
- One PR per wave unless the prompt splits it. Keep PRs reviewable:
  if the diff exceeds ~1,500 lines including tests, ask whether the
  prompt intended a split before pushing.
- PR description must contain: base SHA, the wave file name, the
  acceptance-command output summary, any ambiguity choices made
  (rule 8), and any allowlist near-misses (files you wanted but
  didn't touch).
- PR bodies end with the repo's standard agent attribution.
- Never merge with red CI; never push to `main`; never touch
  `.github/`.

## The adversarial review gate (mandatory, every wave PR)

After your acceptance commands pass and before merge, the branch must
survive the **independent adversarial review** defined in
[`adversarial-review.md`](adversarial-review.md) — the canonical JTS
staff-maintainer review prompt (COAH bar: separation of concerns,
single source of truth, safety, observability, resilience, tests,
docs). The loop is specified there: fresh reviewing context (never
the implementing session), fix every Blocker and Should-fix, re-run
until a clean pass, paste the final verdict into the PR. A wave PR
without a clean review verdict does not merge — the orchestrator
treats a missing or failed gate the same as red CI.

## Stop-and-report protocol

A good blocked-report is short and specific: the preflight fact or
spec line that failed, what you found instead (file/function/line),
and one suggested resolution. Post it as the PR description of a
draft PR (or as your final message if no branch exists yet), then
stop. Do not partially implement around a blocker.
