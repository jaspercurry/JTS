# JTS cleanup programme — full handoff, 2026-09-07

**Read this, then verify it.** Every number here was measured by an agent, most of them
tonight, but the tree moves ~380 commits a day and three separate lanes asserted three
different SHAs for the same Pi within eighteen hours. Treat this document as a set of
testable claims, not as truth. Where a claim matters to what you decide, re-measure it —
the commands are given.

Written by the review coordinator session (branch `claude/codebase-quality-review-do78rn`).
Supersedes [#4417](https://github.com/jaspercurry/JTS/issues/4417), which covered the same
ground before the re-grade and the bulk analysis existed.

---

## 1. The mandate

The owner's goal, in their own words across the programme: **a smaller, simpler, more
elegant codebase.** Less cruft, less prose, less code where it makes sense. 80/20
solutions, never sprawl. Clear separation of concerns, single source of truth, clear
contracts, no god files, no duplicate implementations.

The bar is [AGENTS.md](../../../AGENTS.md): eight non-negotiables (hearing clamp, XVF brick
guard, secrets in compartments, one deploy path, renderer ALSA as the real user, no silent
deafness, paid tests never looped, `main` green) and its defaults (leave every file smaller,
tests pin behaviour not prose, comments only for non-derivable constraints, no guards for
hypotheticals, no new `JASPER_*` knobs, converge duplicates, evidence first). Scope is
measured by what a change *adds*.

Two owner rulings made mid-flight sharpen it:

- **No duplicate code stays because it is untested.** Untested is the reason to converge,
  not to keep. A duplicate found in scope is converged this round by the lane that owns the
  file, with one behaviour pin on the real path. An issue records a duplicate; it does not
  park one.
- **A guard is not a deliverable.** Rows that added machinery were cut or deferred; rows
  that deleted or converged were approved. Every approved guard carries a removal condition.

---

## 2. What actually ran

A five-phase review produced a report and a 269-finding register; eleven execution lanes
were derived from its grade table and run as separate Fable sessions across three accounts.
**But the lanes were not the only thing merging.** Since the review's baseline
`2bc95106d` (2026-09-05 10:57 UTC) there have been **251 merged PRs** from these streams:

| stream | merged PRs | what it was |
|---|---:|---|
| architect session (P1 secrets + P2 deploy round one + P4 handover) | 44 | the original coordinator branch, spans three lanes |
| P9 voice loop (#4208) | 42 | decompose `WakeLoop`, converge providers, refusal observability |
| P12 attached hardware (#4213, program #4027) | 29 | DACs, mics, usbsink, the two reconcilers |
| P6 right-sizing (#4200) | 17 | deletions |
| general steward round (#4030) | ~26 | ran in parallel, not a review lane |
| doctor/state stream (#4028) | 16 | the doctor and `/state` improvements |
| P5 structure and SSOT (#4199) | 15 | packages, layers contract, one-owner-per-path |
| P2 deploy round two (#4194) | 10 | install steps table, health gate |
| P3 resilience (#4195) | 8 | park instead of reboot, bounded waits |
| P11 web UI (#4212, program #4031) | 7 + 5 | URL/IA programme |
| P4 observability (#4197) | 7 | `/state` shape, doctor status vocabulary |
| P1 secrets (own branch) | 2 | |
| smart-speaker audit, coordinator, dependabot, assorted | ~30 | |

Lanes and their issues: P1 secrets #4193 · P2 deploy #4194 · P3 resilience #4195 · P4
observability #4197 · P5 structure #4199 · P6 right-sizing #4200 · P7 tests #4201 (never
started) · P8 docs #4202 (never started) · P9 voice #4208 · P11 web #4212 · P12 hardware
#4213. There is no P10. Hardware asks all go to #4027.

**Handoff issues:** #4279 (P1) · #4248 (P2 round one) · #4421 (P2 round three, the live
queue) · #4416 (P3) · #4327 (P4) · #4385 (P9) · #4324 (P12) · #4387 (P6) · #4417
(coordinator, superseded by this) · comment 5573398816 on #4212 (P11, no issue yet) ·
**P5 has no handoff issue at all.**

---

## 3. How it was graded

The rubric (`docs/codebase-quality-review-2026-09-05/reports/research-best-practices.md` §5)
grades at package/crate/daemon level, red/yellow/green, on twelve attributes: function
complexity, module depth, import-graph acyclicity, coupling direction, duplication, dead
code, test-to-behaviour fit, test truth (mutation), hotspot risk, single ownership of state,
orphan surface area, observability of failure. Whole-repo green requires every
non-negotiable-tier module green on rows 1, 3, 5, 6, 8, 10 and 12. The report collapsed this
into ten attribute rows with letter grades, which is what the lanes were keyed to.

The review's own stated weaknesses, from its completeness critic: `tests/` (1,059 files,
631k lines) was in no tile, so every test number is single-source; `deploy/bin/` (22 root
executables) was read only in slices and `jasper-deploy-health` (900 lines) by nobody before
its deletion was approved; 77% of sampled register rows were never independently verified;
**nothing was verified on hardware or at runtime.**

---

## 4. The re-grade at HEAD

Measured at `a99a746cc`, 1,024 commits past the baseline. Line counts exclude blanks,
comments and docstrings (the review's headline numbers were physical lines — they are not
comparable without this note).

| Attribute | 09-05 | HEAD | Δ | measured evidence |
|---|---|---|---|---|
| Hardware/audio safety | A− | **A−** | = | ceiling one-owner at both emitters (`camilla_config_contract.py:182,203`), clamp now emits `event=camilla.main_volume_clamped`, `CLEAR_CONFIGURATION` deleted; but `set_active_config_raw` still validates only non-emptiness |
| Secrets (NN-3) | C+ | **B** | ↑2 | both blockers closed: the root `source` of the PSK stash is gone (`jasper-wifi-guardian:98-131`), the redactor's `\b` is now a negative lookbehind; 0 tests → 2 test files |
| Deploy integrity | C+ | **B** | ↑1–2 | the attended-sudo bypass is gone; identity (`:811`) and direction (`:841`) both run before `rsync` (`:863`); ADR-0248 makes health gate rather than advise |
| Resilient | B | **B+** | ↑1 | bind failures park (ADR-0251); `wake.leg_died` with done-callbacks; `heal_shared_state_modes` call sites 12 → 1; still 8 raw `b"STATUS` reads |
| Observable | B− | **B** | ↑1 | `/state` gained `schema_version = 3`, sections 25 → 27; doctor checks 173 → 179; **but event names 1,369 → 1,385 with still 0 registries** and no `cues` section |
| Clean — separation & SSOT | C+ | **B−** | ↑1 | a layers contract now exists and is KEPT (1 contract, 6 layers, 0 `ignore_imports`, 74 holdouts, runs in `test-merge`); modules in cycles 145 → 114; **but duplicate primitives moved in single digits** |
| Right-sized | C | **C+** | ↑1 | product −1,662 code lines; `WakeLoop` 4,528 → 2,636 (−42%); `deploy/index.html` 1,551 → 396 (−74%); **but the tuning zone did not move at all** |
| Tests | B− | **B−** | = | `assert callable(` 78 → 26, `caplog.text` 709 → 654 — but +31 files, +11,647 lines, and `._private` reaches in tests went **up** 7,728 → 7,876 |
| Docs | B | **B−** | **↓1** | ADRs 157 → 171 files, **still no index**; `docs/**/*.md` 296 → 317 files |
| Newcomer followability | C | **C+** | ↑1 | 68 of ~142 top-level names ranked into a layer, 74 in `exhaustive_ignores`; flat top-level modules 116 → 109; `active_speaker/` still a 107-file flat root |

### Scale, measured

| tree | code HEAD | code BASE | Δ |
|---|---:|---:|---:|
| `jasper/` | 288,504 | 287,913 | **+591** |
| `rust/` | 40,370 | 40,630 | −260 |
| `deploy/` | 39,713 | 41,157 | **−1,444** |
| `scripts/` | 20,799 | 21,348 | −549 |
| `c/` | 4,957 | 4,957 | 0 |
| **product total** | **394,343** | **396,005** | **−1,662** |
| `tests/` | 410,726 | 403,229 | **+7,497** |

### What measurably improved

An enforced import contract where there was none, and it is KEPT over 775 files and 3,910
dependencies. Cycles: modules inside one 145 → 114, largest SCC 99 → 76. `WakeLoop` really
was split (96 → 75 methods). `deploy/index.html` −74%. Both secrets blockers closed with
tests where there were none. The deploy guard bypass is gone. `/state` has a schema version.
Knobs read-with-a-default-and-never-written 297 → 274. `assert callable(` down two thirds.

### What measurably did not

The tuning zone is frozen: `active_speaker/` root still **107 files**;
`runtime_contract.py` **5,152 → 5,152**; `CrossoverV2Session` **3,703 lines / 155 methods,
byte-identical**; functions over 400 code lines **15 → 15, the same fifteen in the same
order**; `correction_setup._make_handler` 994 → **995**. The event vocabulary grew without a
registry. Duplicate primitives moved by single digits (hand-rolled atomic writes 25 → 19,
private env parsers 32 → 31, `_utc_now` copies 17 → 13, `/var/lib/jasper*` literals 191 →
188, `48000` sites 100 → 99). Tests grew faster than product and coupled harder. The
env-codified guard still accepts a prose `.env.example` comment as codification. ADRs grew
9% with no index.

---

## 5. The problem, defined

Three findings together explain the outcome, and they are the thing to think hardest about:

**1. The lane model could not shrink the tree, by construction.** Lanes were keyed to grade
attributes. Raising secrets, deploy integrity, observability, resilience and hardware means
*adding* code — redactors, locks, events, `/state` fields, doctor rows, park paths. Only two
of eleven lanes (P6 right-sizing, P5 structure) were deletion-shaped, and P6's largest
remaining rows were the ones deferred. Seven grades went up and the tree stayed flat because
those are the same fact, not a contradiction.

**2. The bulk was exempt from the programme by design.** The tuning zone —
`active_speaker/`, `audio_measurement/`, `correction/` — is **147,603 code lines, 43.1% of
`jasper/`, 252 files**, and every lane was instructed to treat it as read-only. Its tuning
rows were listed under owner-gated headings and, with one exception, never ticked. So the
programme worked the 57% that was already healthiest.

**3. There is almost no dead code left, and that is the real news.** An adversarial,
registry-aware scan of all 8,878 module-level defs in `jasper/` found **52 lines** of true
symbol-level orphans. What looks like bulk is **live-but-thin**: large subsystems whose only
entry point is one web route or one hand-run laptop CLI. Of 775 Python modules, 680 are in
the import closure of a shipped entry point; only 8 modules (3,391 lines, all
`bass_extension`) are reachable from nothing at all.

**The consequence:** the remaining size is not a code-quality problem any more. It is a
**product-scope question**. Nothing significant can be deleted on reachability evidence
alone. What is left needs the owner to decide which capabilities the speaker still has.
That is a different kind of decision than the one this programme was set up to make, and it
is why a fresh agent should not simply resume the lane queue.

---

## 6. Where the remaining weight actually is

Ranked by removable lines × confidence. "Reachable?" cites evidence, not inference.

| # | candidate | product | tests | reachable? | conf |
|---|---|---:|---:|---|---|
| 1 | **`bass_extension` parked half** — `bench/**`, `ladder.py`, `limiter_evidence.py`, the apply/bypass/recover trio | 6,511 | 5,496 | **NO.** 8 modules have zero importers; `apply_bass_extension`/`bypass`/`recover` have 0 callers in `jasper/`; the bench terminus is an unconditional `raise SystemExit`. Six production readers guard an intent file **no production writer can create** | HIGH |
| 2 | **v1 per-driver commissioning/measurement residue** — unreachable function bodies inside live modules (`web_commissioning` 1,448, `web_measurement` 1,281, `commissioning_admission` 901, …) | 5,172 | 6,559 | **NO** for the functions, YES for their host modules. Corroborated in-tree: `correction_crossover_backend.py:525` says its only supplier is itself unreachable. A per-function excision, not a file delete | HIGH |
| 3 | **the v1 apply arm** — `apply_profile` + `commissioning_apply.py` + `apply_candidate` + `attest_geometry` | 1,587 | 1,504 | **NO.** `apply_profile` has exactly three occurrences repo-wide: its own `def` and two string literals. No route, no test, no JS | HIGH |
| 4 | **`crossover_v2` tier-B subtree + round/angle CLIs** — 39 modules | 13,847 | 16,891 | **YES, but only by hand from the laptop.** Never in a unit or `install.sh`; reached from `scripts/run-crossover-round.py` and documented in the operator runbook | LOW (reachable; only *value* is in question) |
| 5 | **tier-C console scripts** — 48 modules installed but invoked by nothing in `deploy/`, `scripts/` or CI | 16,611 | ~10,000 | **MIXED.** Documented operator tools vs an undocumented+untested subset of ~2,000 lines. Per-script decision | MED split / LOW as a block |
| 6 | **`docs/historical/`** | 19,807 | — | explicitly outside the orphan sweep per `doc-map.toml:14,19` | HIGH |
| 7 | **self-declaring stale plan docs** — `dumb-endpoint-bringup`, `multiroom-pairing-reliability-plan`, `REFACTOR-CUTOVER-2026-08`, `tool-platform-plan`, `audit-pending-followups`, the bass-extension plan + waves | 13,270 | — | each declares its own staleness in its header | MED-HIGH |
| 8 | `audio_validation` artifact builders | 727 | 1,074 | **UNRESOLVED** — the module is live for constants; the builders are called only by a console script nothing invokes, but the doctor prints that command as an operator remedy | LOW |
| 9 | `active_speaker/bench/` + emit-bench CLI | 1,795 | 2,082 | NO from any unit; imports `bass_extension.bench.{render,derivation}` so it is coupled to row 1 | MED |
| 10 | `route_latency/` + harness | 2,338 | 2,119 | NO from any unit, but **ADR-0108** names it as the mechanism by which a latency claim is earned | LOW-MED |
| 11 | true symbol-level orphans (3 defs) | 52 | 0 | **NO.** Zero occurrences anywhere outside the defining line | HIGH |
| 12 | orphan `scripts/` (`_sync_measure_audio.py`, `_wake_pipeline_common.py`) | 116 | 0 | **NO.** Filenames appear in no other file | HIGH |
| 13 | `experiments/aec3-v2-deep-tune-spike/` (README only) | 201 | 0 | header says DELIVERED 2026-05-22 | HIGH |

**Roughly 13,300 product + 13,600 test lines are removable on evidence alone (rows 1–3, 11–13).
Another ~33,000 doc lines (rows 6–7). Everything past that — about 30,000 more product and
test lines in rows 4–5 and 8–10 — needs the owner to retire a capability.**

### What the analysis could not establish

`jasper-wake-enroll` (no unit, no doc, no test, no invoker found — but the wake-enrollment
web flow may shell out at runtime). Whether `sudo jasper-audio-validate --stdout`, which the
doctor advertises as an operator remedy, is still used. The exact test lines that die with
rows 2 and 3 (those test files cover live and dead functions in one file). Whether
`crossover_v2`'s 29,971 tier-A lines contain further dead arms — that needs a per-route
trace of the twelve `/crossover/v2/*` handlers, which was not done.

---

## 7. Outstanding work

**200 distinct items. 176 of them (88%) have no ticket** — they exist only inside a handoff
issue body or a lane comment. 51 need an owner decision; 22 need a person at a box.

| lane | items | where recorded | notable |
|---|---:|---|---|
| P1 secrets | 19 | #4279 | 5 of 6 installer env writers still bypass the shared lib; the redactor sweep never covered all of `jasper/`; `experiments/usb-turntable` prints outside the redaction path and nobody has checked what it prints |
| P2 deploy | 21 | #4421 (live), #4248 | row 6 (installer env writers, NN) fully scouted but unbuilt; 8 findings recorded-not-fixed from the staging tree; the Cargo workspace; the 581-line seam deletion |
| P3 resilience | 30 | #4416 | waves 2–4 = 24 rows, all with file:line, proof and a guard-removal condition; two parked tuning rows |
| P4 observability | 15 | #4327 | 22 class-A ambiguous doctor rows; five reader tri-states; `/state.aec` still spawns `systemctl` per request |
| P5 structure | 10 | **#4199 comments only — no handoff issue** | rows 16–20 and PR 19 (the `jasper/platform/` regroup, 534 import lines) deferred |
| P6 right-sizing | 16 | #4387 | the env-knob contract: "do not execute as planned", three holes documented; row 7b fold recommended dropped |
| P7 tests | 6 | #4201 (kickoff only) | **never started.** 984 private-name patches, 247 source-text readers, `caplog.text` in 654 places |
| P8 docs | 6 | #4202 (kickoff only) | **never started.** 171 ADRs, no index; three batch ADRs; the stale-path pass |
| P9 voice | 8 | #4385 | B1–B6 parked on the owner's ten-turn numbers; `CamillaController` never on the exit stack (a deliberate gap) |
| P11 web | 13 | comment 5573398816, **no issue** | W2–W6 of the URL programme; EQ-1; a wifi DOM harness; the phone eyeball |
| P12 hardware | 17 | #4324 | the `eval` bridge trust model (large, cross-cutting); three bash-4-only sites; HW-6's premise unproven |
| coordinator | 5 | #4417 | re-grade (now done); which SHA jts3 runs; the next shape |

**Ticketed follow-up defects (15):** #4281, #4282, #4284 (a live NN-3 leak), #4304 (P1) ·
#4332, #4336, #4344, #4361, #4362, #4366, #4372, #4374, #4375, #4384 (P6) · #4334 (P9).
**Pre-existing issues the review touched (16):** #1738, #2202, #2574, #3769, #4027, #4028,
#4029, #4030, #4031, #4085, #4121, #4123, #4124, #4139, #4211, #4214. **190 issues are open
in total**; 138 of them are tuning/measurement-zone work that no lane was allowed to touch.

### Owner decisions — 24, deduplicated

The big four, by lines at stake: **OD-1** the v1 commissioning chain (#2202 — repair or
delete); **OD-2** the `bass_extension` parked half (ADR-0018 forbids deleting on orphan
grounds alone, so this needs an ADR, not a scan); **OD-7** the tuning zone itself; **OD-3**
`sound_setup` ↔ `web_commissioning` ownership (2,784 divergent lines). Then: streambox
volume ownership · the dac-content FIFO retirement (ADR-0220 — did the bonded-pair run
happen?) · the four dormant wake legs · peering's election · the Cargo workspace · the
`caplog.text` migration · `experiments/usb-turntable`'s rename · does `assertive` room
correction ship · the AEC3 lab knobs · two doctor rows that are security regressions and
cannot fail · five files no tile ever reviewed (`scripts/{use,jasper-pipe-probe,rust-ci-needed}`
have had **no reviewer at all**) · plus lane-level calls listed in each handoff.

### Owner at a box — 17 items

Deploy `main` to jts.local (still on `162ab4088`, several hundred commits behind) · ten
spoken turns for P9's gate 0.2 (unblocks B1–B6) · the #4209 volume-slider check · unplug the
XVF3800 on jts3 for H1/H2 · the A4 park-cue listen · one window covering P3's R7 audible
volume check and R13 DAC unplug · HW-5 staging cost on jts4 · HW-6 on a *streaming* box ·
the source-intent grep on spares · P1's six verification rows · the P11 phone eyeball ·
the numpy-on-jts4 lead (#4139, still unexplained) · #3656's bonded-pair listen.
Exact recipes are in #4324, #4385, #4416 and #4027.

---

## 8. Rules and conventions in force

Created or changed during execution; all in
[`prompts/README.md`](README.md).

- **The merge word:** the owner's triage at a plan gate is also the merge word for every PR
  in that plan, sensitive tier included, once the review passes have no open blockers.
- **The duplicates rule** (§1).
- **400 changed lines per PR** unless it is a pure deletion or a byte-identical move.
- **Territory:** a lane does not edit another lane's files without a granted one-off;
  cross-lane asks go on the owning lane's issue; hardware asks go to #4027 with the exact
  command and expected reading.
- **ADR numbers are reserved on [#4405](https://github.com/jaspercurry/JTS/issues/4405)**
  before a PR opens — never from the directory listing. Two lanes collided at 0247, then
  both hopped to 0248 independently. 0247 and 0249 are now retired gaps.
- **Review tiers:** `/code-review` + `/simplify` on everything; `/adversarial-review` on the
  non-negotiable tier.
- **The tuning zone is parked** and read-only for every lane; its rows go under an
  owner-gated heading.
- Coordinator check-ins must be a durable Routine — a session-only cron dies with the idle
  container (it did, for six hours).

---

## 9. Traps — do not re-derive these

From P6's ledger (#4387), each verified at HEAD by that lane: `WakeEventStore.get_event` is
**live** (the paid eval lane calls it). The dead-symbol residual was 36 lines, not 372 — a
per-symbol scan falsely flags ~150 `@doctor_check` functions, asyncio protocol callbacks and
D-Bus `@method()` handlers. `HAClient.list_agents` is **not** dead. Deleting a *caller* of
the volume clamp never removes the guard. Two `AUTO` verbs exist on two sockets; only
fan-in's was dead. `AudioRing::trim_to` and `target_fill_frames` each name two different
things. **`[profile.release]` is honoured only at a Cargo workspace root** — a workspace PR
must hoist `panic = "abort"` or silently lose it. ALSA's pkg-config is absent in the remote
container, so `cargo test` needs a stub. CI's Rust toolchain is pinned to 1.85.0.
`scripts/` has no `__init__.py`, so its import dance is the idiom, not residue.

The env guard `test_env_vars_codified.py` has **three holes**: blind to reads routed through
a module-level constant (136 in `jasper/`, including `JASPER_HA_URL`); a *reader* in
`deploy/bin` and a **commented-out** `.env.example` line both count as codification; and
`__pycache__` is scanned, so a stale `.pyc` is a valid codification surface. It also cites
an AGENTS.md rule that no longer exists.

Review claims already superseded at HEAD: the outputd park **has** a reader;
`/state.resilience.wifi_guardian` never existed; accessory reader supervision landed.
#4342's title claimed a fix its diff did not make; #4391 made it.

---

## 10. Ways to slice this — options, not instructions

Each is sized from §6 and §7. They are not exclusive.

**A. Evidence-only deletion pass.** Rows 1–3, 11–13 of §6 plus the doc rows: ~13,300 product
+ ~13,600 test + ~33,000 doc lines, all with HIGH confidence and no owner decision except
the ADR that ADR-0018 requires for `bass_extension`. Largest measurable shrink available
without a product call. Six to ten PRs.

**B. Answer the four big owner decisions first.** OD-1 (v1 chain), OD-2 (bass extension),
OD-7 (tuning zone), OD-3 (sound_setup ownership). These gate rows 4, 5, 9 and 10 — about
30,000 further lines. Cheap in agent time, expensive in owner thought. Nothing else unlocks
comparable weight.

**C. Finish what is built.** P5's two open PRs need a rebase; P5 and P11 owe handoff issues;
P2's #4421 queue is fully scouted. Small, closes the programme cleanly.

**D. The two lanes that never ran.** P7 (tests) and P8 (docs) are independent of everything
else and address two of the three attributes that did *not* improve. P8 in particular is
cheap: an ADR index is one file, and the docs grade fell for its absence.

**E. Ticket the 176.** Nothing tracks 88% of the outstanding work outside prose in issue
bodies. Whatever else happens, this is the cheapest insurance against losing it.

**A caution on shape.** Five concurrent lanes produced 251 merges in two days, and also: two
ADR-number collisions, several territory negotiations, plans of 21–28 rows that outran the
pause, and one PR merged a review round early by the coordinator (#4392 — caught and fixed
forward by #4420 before any box carried it). Fewer, deletion-shaped lanes may be the better
trade now that the seam-fixing work is done.

---

## 11. Verify these first

In rough order of how much a wrong answer would cost you:

1. **Re-measure the scale numbers in §4.** They were taken at `a99a746cc`; `main` moves fast.
2. **Re-run the reachability analysis for §6 rows 1–3 before deleting anything.** The method
   over-approximates deliberately, but it is a static scan and the traps in §9 exist because
   scans of this kind have been wrong here before.
3. **Which SHA each Pi actually runs.** Three lanes asserted three different values for jts3
   within eighteen hours. Read `http://<box>/system/`; do not trust any document, including
   this one.
4. **Whether the open PRs still merge cleanly** (#4373, #4376 need rebases; P11 has several
   in flight).
5. **The claim that there is no dead code left.** It is the load-bearing finding of §5 and it
   came from one agent's scan.

Pointers: the report `docs/CODEBASE-QUALITY-REVIEW-2026-09-05.md` · the evidence directory
`docs/codebase-quality-review-2026-09-05/` (register, 62 agent reports, the rubric, every
lane kickoff) · the handoff issues in §2 · the ADR ledger #4405 · the hardware program #4027
· this branch `claude/codebase-quality-review-do78rn`, which is never merged and holds all of
the above.
