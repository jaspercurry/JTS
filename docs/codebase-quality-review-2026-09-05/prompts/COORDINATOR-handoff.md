# Coordinator handoff — the 2026-09-05 codebase quality review and its lanes

Written 2026-09-07 ~18:00 UTC by the coordinator session (`claude/codebase-quality-review-do78rn`,
session `01GUpqzJ9htPni1hauoGpETE`), for a successor coordinator. The successor is expected to
**re-investigate before agreeing with anything here**: re-grade `main` at HEAD with the same rubric,
check the numbers, and decide for themselves what the next round is. This document shows the work;
it does not prescribe the plan.

## 1. The mandate

The owner's goal, in their words across the review: a smaller, simpler, more elegant codebase. Less
cruft, less prose, less code where it makes sense. 80/20 solutions, never sprawl. Clear separation of
concerns, single source of truth, clear contracts, no god files, no duplicate implementations. The
bar is [AGENTS.md](../../../AGENTS.md): eight non-negotiables (hearing clamp, XVF brick guard,
secrets in compartments, one deploy path, renderer ALSA as the real user, no silent deafness, paid
tests never looped, `main` green), and the defaults (leave every file smaller, tests pin behaviour
not prose, comments only for non-derivable constraints, no guards for hypotheticals, no new
`JASPER_*` knobs, converge duplicates, evidence first). Scope is measured by what a change *adds*.

Two owner rulings made during execution that sharpen the mandate:

- **No duplicate code stays because it is untested.** Untested is the reason to converge, not to keep.
  A duplicate found in scope is converged this round by the lane that owns the file, with one
  behaviour pin on the real path. An issue records a duplicate; it does not park one.
- **A guard is not a deliverable.** Rows that added machinery (the env-knob contract rewrite, a
  second W6 guard, a JS style allowlist) were cut or deferred; rows that deleted or converged were
  approved. Every approved guard carries a removal condition.

## 2. How the review was done

Audited tree `2d571e6b8` (`origin/main` at 2026-09-05 06:56 EDT). Evidence directory:
[`docs/codebase-quality-review-2026-09-05/`](../README.md); report:
[`docs/audits/2026-09-05-codebase-quality-review.md`](../../audits/2026-09-05-codebase-quality-review.md);
published artifact "JTS Quality Review" (claude.ai/code/artifact/1c990d04-817e-466e-9a08-6f3f3bf798cc).

The method was the five-phase comb from `docs/DEEP-AUDIT-PLAYBOOK.md`, preceded by a research pass
that produced the rubric (`reports/research-best-practices.md`). About 65 Opus and Sonnet subagents:

| phase | what | agents |
|---|---|---|
| 0 | cartography: inventory and complexity, orphans, config ledger, duplicate systems, test-suite health, docs drift | 6 |
| 1 | tiled read of every product file: 38 tiles over 573,044 lines / 1,145 files (`jasper/`, `rust/`, `c/`, `deploy/`, `scripts/`, `experiments/`, `jasper_aec3/`, `.github/`); ~300k lines read fully, ~175k structurally; no tile skipped a file | 38 |
| 2 | cross-cutting lenses (boundaries/imports, resilience, observability, secrets+config, Pi performance) and end-to-end scenarios (privileged actions, audio chain, deploy, wake→response, volume) | 11 |
| 3 | adversarial verification: three skeptics over 55 sub-claims; 14 confirmed, 24 downgraded, 12 refuted, 3 upgraded, 5 new; three claims settled by execution against stubs | 3 |
| 4 | synthesis, plus a completeness critic (`reports/p4-critic.md`) asking what nobody opened | 2 |

Findings were normalized against AGENTS.md's bar, not the tile's own severity: 269 distinct findings
after dedup (13 Blocker, 243 Should-fix, 7 Nit, 6 earns-its-keep); tiles reported 23 blockers, 13
survived, 16 were demoted (debt, not correctness), 6 were promoted (silent failures). 73 rows were
flagged for a skeptic. The register's LOC-delta estimates sum to about −52,000 lines, stated as a
scale signal, not a budget. Register IDs (`R-nnn`) and report IDs (`R-nnn`) are different numberings.

**Known weaknesses of the method, from its own critic:** `tests/` (1,014 files, 606k lines) was in
no tile, so every test number is single-source; `deploy/bin/` (22 root executables, 8,942 lines)
was read only in slices and `jasper-deploy-health` (900 lines) by nobody before its deletion was
approved; 77 % of the register rows the critic sampled were never independently verified; nothing
was verified on hardware or at runtime. The tree moved ~1,200 commits between the audited SHA and
the first lane plans, so several findings were already fixed or stale when a lane reached them.

## 3. What it was graded against

The rubric grades at package/crate/daemon level, red/yellow/green, on twelve attributes: function
complexity, module depth, import-graph acyclicity, coupling direction, duplication, dead code,
test-to-behaviour fit, test truth (mutation), hotspot risk, single ownership of state, orphan surface
area, observability of failure. Whole-repo green requires every non-negotiable-tier module green on
rows 1, 3, 5, 6, 8, 10 and 12. The report collapsed this into ten attribute rows with letter grades.

## 4. What it found

The report's grade table at `2d571e6b8`, verbatim:

| Attribute | Grade | Δ vs 08-25 | One line |
|---|---|---|---|
| Hardware/audio safety (NN-1, NN-2) | **A−** | = | Ceiling one-owner at every emitter and not constructible above 0 dB from any shipped input; XVF brick guard holds; one unused write command to delete. |
| Secrets (NN-3) | **C+** | new | Compartments exemplary. The PSK stash is sourced unquoted as root; the general redactor misses every underscore-prefixed key name and has zero tests. |
| Deploy integrity (NN-4, NN-8) | **C+** | new | The default attended-sudo path bypasses both guards (verified by execution); health is advisory by design. |
| Resilient | **B** | ↓ from A | Held port 8780 reboots the box up to three times; `outputd.env` lost-update window; three uncapped STATUS readers; 2–3 forks/second at idle. |
| Observable | **B−** | ↓ from A− | Excellent primitives, no contract: 1,309 event names with no registry, 9 publish mechanisms, "daemon dead" ≠ "speaker silent". |
| Clean — separation & SSOT | **C+** | = | Module graph acyclic; package graph a 23-package SCC held by 15 shelving mistakes; ≈20 spellings of the sample rate. |
| Right-sized | **C** | ↑ from C− | Tuning zone 41 % of `jasper/`; 289 knobs nobody turns; ~2.3k product + ~1.6k test LOC verified dead. |
| Tests | **B−** | = | Honest and broad; 1,645 private-name patches; guards that measure the wrong thing; the redactor has zero tests. |
| Docs | **B** | ↑ from C+ | Governed corpus; 157 ADRs with no index, 3 batch-ADRs, one 1,694-line plan that declares itself complete. |
| Newcomer followability | **C** | ↑ from C− | Where things live is derivable from the graph, not from the tree. |

Verdict, verbatim: **"Overall: B engineering, C+ proportionality.** The last audit said the repo
needed an editor. It still does, but the first job now is a **welder**: a dozen seam fixes, most
under 30 lines, that make the non-negotiables true by construction. Then the structural moves that
let every later steward round find things without grep." And: "The machinery is good; the seams
are not."

Headline numbers behind the grades: 424k code lines in `jasper/` against 585k in `tests/` (19,461
test functions); 12 god files (the largest: `WakeLoop` 4,528 LOC / 96 methods, `sound_setup.py`
4,962, `runtime_contract.py` 5,152, `crossover_v2_flow.py` 3,703); 17 duplicated-primitive
concerns (atomic write ×15 hand-rolls, env parsers in 19 modules, `_utc_now` ×39, 136 distinct
`/var/lib/jasper*` literals, the sample rate ≈20 times); the module graph a DAG except one 4-module
cycle, but 1,708 function-local imports knit 72 modules into one SCC; 829 `JASPER_*` tokens of
which 289 are read with a default and written by nothing; 172 doctor checks of which 87 cannot
reach `fail`; idle fork rate 155–210/min, mostly one volume observer.

Four blockers (report §2.1): the unquoted PSK stash sourced as root; the attended-sudo deploy path
bypassing the identity and direction guards; the port-8780 reboot ladder; the silent wake-leg death.
All four are now fixed on `main` (P1, P2, P3, P9).

## 5. How the work was organized

One lane per non-A attribute, keyed to the grade table, plus three concern lanes; each lane a fresh
Fable session with a self-contained kickoff (`prompts/P*.md`, mirrored into the lane issue), told to
delegate every scout, build and test run to Sonnet or Opus subagents, run `/code-review` and
`/simplify` on every PR and `/adversarial-review` on the sensitive tier, post a plan at a gate, and
hand off on a new issue. Lanes and issues: P1 secrets #4193 · P2 deploy #4194 · P3 resilience #4195
· P4 observability #4197 · P5 structure/SSOT/god files #4199 · P6 right-sizing #4200 · P7 tests
#4201 · P8 docs #4202 · P9 voice loop #4208 · P11 web UI #4212 · P12 attached hardware #4213
(hardware asks on #4027). No P10. Three accounts: James Crane and Dip (remote), Space Hater (the
owner's machine, the only one that reaches the Pis). The tuning zone (`active_speaker/`,
`audio_measurement/`, `correction/`, 41 % of `jasper/`) stayed parked and read-only throughout.

Rules created during execution, all in [`prompts/README.md`](README.md): the owner's plan-gate
triage is also the merge word for every PR in that plan; the duplicates rule above; 400 changed
lines per PR unless pure deletion or a byte-identical move; a lane does not edit another lane's
files without a granted one-off, and cross-lane asks go on the owning lane's issue; every
"measure once on hardware" row goes on #4027 with the exact command and expected reading; ADR
numbers are reserved on the ledger issue #4405 before a PR opens (two lanes collided at 0247 and
again at 0248 taking "next free" from the directory); the coordinator ran an overnight mode
answering gates from the lane issues on a Routine (a session-only cron died with the idle container
once, 05:45–12:00 UTC).

## 6. Where things stand (2026-09-07 17:15 UTC, paused by the owner)

About 245 PRs merged since the review's baseline. The honest line count against
`2bc95106d` (2026-09-05 10:57 UTC): `jasper/` +1.9k, `rust/` −0.4k, `deploy/` +0.4k,
`scripts/` −1.0k, `tests/` +10.4k. **The tree is not smaller yet.** The deletions (P6 −4,246 net,
P9's daemon split, P2's install table) were offset by what the observability, hardware and secrets
lanes added, and by tests. The rows that shrink the tree are mostly the deferred ones.

| lane | state | handoff | in one line |
|---|---|---|---|
| P1 secrets | done | #4279 | one redactor (Python + aligned bash, ADR-0243), one logging bootstrap, the PSK quoting fixed, uncached wizard credentials; +430 product / +1,677 test lines |
| P2 deploy | round two landing wave B | #4248 (round one) | both guards before every rsync; `jasper-doctor --core` gates the deploy under `systemd-run` (ADR-0248, proven on jts3/jts4); locked bash env writer; `install.sh` 2,211 → 1,865 with a steps table; wave C, the seam deletion and the Cargo workspace deferred |
| P3 resilience | done for this round | #4416 | bind failures park instead of feeding the reboot ladder (ADR-0251); one lock for `fanin.env`/`outputd.env`; STATUS readers converged; naked connects bounded; idle forks on jts4 4.8 → 2.4/s; waves 2–4 deferred |
| P4 observability | done | #4327 | 26 PRs; `/state.audio_graph` deleted (ADR-0245); `skipped` means "nothing observed" stated in `doctor_contract.py`; two designs reviewed out and recorded |
| P5 structure | landing six PRs | none yet | executed import cycles 0; import-linter layers contract runs first in test-merge; `jasper/aec/`, `jasper/net/`, `jasper/platform/` born; one owner per path; the Google secrets path resolved at check time; rows 16–20 and the platform regroup deferred |
| P6 right-sizing | done | #4387 | 14 PRs, net −4,246: peering receive half (ADR-0246), legacy duck transport, `s0-sync` harness, dead wizard mains, XVF `CLEAR_CONFIGURATION`, host-clock DLL (ADR-0250); env-knob contract not executed, Cargo workspace and host-clock fold deferred with measurements |
| P9 voice loop | done | #4385 | 40 PRs; `voice_daemon.py` 5,093 → 3,288 across seven modules; providers on one base; refusal observables; server-VAD path deleted (ADR-0244); B1–B6 wait on the owner's ten-turn numbers |
| P11 web UI | paused after W1 + 3 W2 rows | note 5573398816 on #4212 | wizard mains converged, dead CSRF helper gone, `/sync/` moved, `correction_hub.py` gone, two allowlists deleted; the URL program (W2–W6) not started; deploy-and-eyeball owed |
| P12 hardware | done | #4324 | 21 PRs; #4209 root-caused; mic-candidate precedence fixed; `/state.microphone` vocabulary; only lane with a full hardware round; H1/H2 and the slider still owed |
| P7 tests, P8 docs | not started | — | queued as the first two lanes after the pause |

Hardware: jts4 `37e9557a5`, jts3 `d4cb8b49d` per P3's readings (P9 and P12 assert other jts3 SHAs
within the same day; read `/system/` rather than trusting any of the three), jts.local `162ab4088`
awaiting the owner. P6's fourteen merges first ran on hardware in P3's deploy, clean.

## 7. What is outstanding

- **Owner at the box:** deploy `main` to jts.local; ten spoken turns (P9 gate 0.2, `event=turn.timeline`);
  the #4209 slider; unplug the XVF3800 on jts3 (H1 reboot window, H2 mic-loss cue); the phone
  eyeball of the landing and hubs; one owner window for P3's R7 audible volume check and R13 DAC
  unplug; the PSK-on-argv call (P1 owner call A); R17's turntable udev port literal.
- **Deferred rows, each resuming from its triage:** P2 wave C (portability, `deploy/bin` prose, the
  581-line `first-party-runtime.sh` seam, the airplay push design) and row 12 (Cargo workspace with
  the `[profile.release]` hoist and cache-format bump); P5 rows 16–20 and PR 19; P11 W2–W6; P3
  waves 2–4; P6's env-knob contract (do not execute as planned; three holes documented on #4387).
- **Not started:** P7 (1,645 private-name patches, 209 source-text-reading test files, `caplog.text`
  in 91 files, the changed-file map) and P8 (157 ADRs without an index, three batch ADRs, the
  self-declared-complete 1,694-line plan, the stale-path pass after the moves).
- **Owner decisions the review priced but never scheduled** (report §8, register §h): the v1
  commissioning chain (#2202, ~21k LOC whose terminus says it cannot succeed on hardware); the
  `bass_extension` parked half (ADR-0018); `sound_setup` ↔ `web_commissioning` ownership; streambox
  volume ownership; the dac-content FIFO retirement (ADR-0220); the four dormant wake legs; the
  tuning zone itself.
- Residual issues filed by lanes: #4281, #4282, #4284, #4304 (P1); #4328, #4332, #4336, #4344,
  #4351, #4361, #4362, #4366, #4372, #4374, #4375, #4384 (P6); #4334 (P9); #4139 (numpy resident in
  `jasper-control` on jts4 with no active group, unexplained).

## 8. Facts and traps not to re-derive

From P6's ledger (#4387), verified at HEAD by that lane: `WakeEventStore.get_event` is live (the paid
eval lane calls it); the dead-symbol residual was 36 LOC, not 372 (per-symbol scans flag registry
functions, asyncio callbacks and D-Bus methods as dead); `HAClient.list_agents` is not dead; deleting
a *caller* of the volume clamp never removes the guard; two `AUTO` verbs exist on two sockets and
only fan-in's was dead; `AudioRing::trim_to` and `target_fill_frames` each name two different
things; `[profile.release]` is honoured only at a Cargo workspace root (so a workspace PR must hoist
`panic = "abort"` or silently lose it); ALSA's pkg-config is absent in the remote container so
`cargo test` needs a stub; CI's Rust toolchain is 1.85.0; `scripts/` has no `__init__.py`, so its
import dance is the idiom. The env guard `test_env_vars_codified.py` has three holes (constant-routed
reads invisible; commented-out `.env.example` lines and `deploy/bin` readers count as codification;
`__pycache__` is scanned) and cites a charter rule that no longer exists. Review claims already
superseded at HEAD: the outputd park has a reader; `/state.resilience.wifi_guardian` never existed;
accessory reader supervision landed. #4342's title claimed a fix its diff did not make; P3's #4391
made it. Fresh-session Fable coordinators editing by hand produced two text-slice misfires in one
night; every edit went to a builder after that.

## 9. What I would want checked about my own work

- The lane model, keyed to grade rows, put five lanes on `main` at once. It worked for throughput
  (245 PRs in two days) but produced the ADR races, several territory negotiations, and plans of 21–28
  rows that outran the pause. A successor may prefer fewer, deletion-first lanes.
- I approved observability and hardware rows that grew the tree while the mandate was to shrink it;
  the net +1.9k in `jasper/` is the measure of that. Re-grade "Right-sized" at HEAD before anything.
- The review's numbers were three days stale when lanes used them; every lane found plan estimates
  wrong (P6's 372 → 36, P3's six already-fixed rows, P2's row 6). Treat any number in the report as a
  hypothesis about HEAD.
- I could not resolve which SHA jts3 actually runs; three lanes asserted three values.
- The coordinator check went dark for six hours once because a session-only cron died with the idle
  container. Only a Routine survives that.

## 10. Where a successor might start (options, not instructions)

Re-grade `main` at HEAD with the same rubric and the register's theme weights; compare with §4 and
say where you disagree. Then decide: continue the lane model from the handoff issues, or run one
deletion-first pass (the deferred P2/P6/P5 rows are the ones with measured line deltas), or open the
owner decisions in §7 first since the tuning zone and the v1 chain dominate the remaining size. P7
and P8 are cheap and independent whichever way. Whatever the choice: every plan gate is the merge
word, every PR gets both passes, the duplicates rule stands, guards need a removal condition, and
nothing in the tuning zone moves without the owner's tick.

Pointers: the report and evidence directory above; `prompts/README.md` (queue, rules, state line);
the handoff issues in §6; the ADR ledger #4405; the hardware program #4027; the review branch
`claude/codebase-quality-review-do78rn` (never merged; it holds the report, the register and the
prompts).
