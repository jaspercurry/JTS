# Lane: pins-ci-record

HEAD verified: `f4ff89731979abfb0f27a4afb3f220d22d21e78c` (== origin/main, matches BRIEF-SURVEY's stated HEAD). Read-only; no repo state touched beyond `git log`/`git rev-parse`/`git status`/`grep`.

---

## A. Pins in the fast lane

**Claim: the two boundary tests run only in the merge lane, not `scripts/test-fast`.**

Verdict: **TRUE**.

- `scripts/test-fast`'s always-on guard list is exactly these 5 files, at `scripts/test-fast:426-429`:
  ```
  tests/test_dependency_groups.py tests/test_lint_contracts.py \
  tests/test_deploy_wiring_guards.py tests/test_shell_awk_environ_convention.py \
  tests/test_docs_impact.py
  ```
  Neither `tests/test_correction_boundary_ssot.py` nor `tests/test_runtime_import_closure.py` appears there, in the routing-policy target (`scripts/ci-classify.py:44` `ROUTING_POLICY_PYTEST_TARGETS = ("tests/test_ci_classifier.py",)`), or in the landing bundle (`scripts/ci-classify.py:29-41`).
- Both files are whole-tree AST scanners with no single "owning" production module (`tests/test_correction_boundary_ssot.py:1-14` docstring: cross-package SSOT/boundary contract over `jasper/active_speaker`, `jasper/audio_measurement`, `jasper/correction`; `tests/test_runtime_import_closure.py:1-13`: import-closure walk over the same packages plus `jasper/active_speaker` resolution). `scripts/test-fast`'s changed-file mapping (`jasper/*/*.py` arm, `scripts/test-fast:186-209`) only ever selects `tests/test_<module>.py` / `tests/test_<package>.py` / `tests/test_<parent>[_<module>].py` — no module in either package is named `correction_boundary_ssot` or `runtime_import_closure`, so no changed-file diff can ever select them (same class as the repo-wide scanners test-fast's own comment names as deliberately excluded, `scripts/test-fast:414-424`).
- `scripts/test-merge:77-78` runs the complete suite (`pytest -q --tb=short --ignore=tests/voice_eval -n 4`), which is where these two run.
- **One-line change to add them**: append both paths to the `lane_pipe_pytest` call at `scripts/test-fast:426-429`, e.g.
  ```
    tests/test_dependency_groups.py tests/test_lint_contracts.py \
    tests/test_deploy_wiring_guards.py tests/test_shell_awk_environ_convention.py \
    tests/test_docs_impact.py tests/test_correction_boundary_ssot.py tests/test_runtime_import_closure.py "$@"
  ```
- **`ci-classify.py` does not already route them for `jasper/` changes** — confirmed: it has no notion of these two files at all (`grep -n "test_correction_boundary_ssot\|test_runtime_import_closure" scripts/ .github/workflows/` → no hits). `ci-classify.py`'s only pytest-target registries are `LANDING_PYTEST_TARGETS`, `DOCS_TEST_FILES`, and `ROUTING_POLICY_PYTEST_TARGETS` (`scripts/ci-classify.py:29-107`) — none of which include them, and its lane classifier (`classify()`, `scripts/ci-classify.py:234-271`) only ever returns `fast-landing`/`docs`/`full`, never a boundary-specific lane. A `jasper/` change short of the landing/docs allowlists takes `full` (`.github/workflows/tests.yml:300-380`, `pytest-matrix` job, `if: needs.classify.outputs.lane == 'full'`), which runs `scripts/test-merge` — so the two tests DO run in CI on any ordinary `jasper/` PR, just via the `full`/merge lane, never via `test-fast`.

**This exact gap is independently confirmed by the record**: `/tmp/.../scratchpad/record/LANDING.md:62` — "Fast-lane coverage | gap — neither boundary test is in `scripts/test-fast`'s always-on guard list; merge-lane guarantees only" — and is queued as `LANDING.md:191` row **8.9** ("fast-lane guards: both boundary tests in `scripts/test-fast`'s always-on list", tag T, gate "sanity look", size "tiny") — **not yet landed** (confirmed against the tree above).

---

## B. CI pytest lane

`.github/workflows/tests.yml` (single workflow file besides `docs-links.yml`, `first-party-arm64-release.yml`; `ls .github/workflows/`):

| Fact | Value | Evidence |
|---|---|---|
| Triggers | `push` to `main`; every `pull_request` | `tests.yml:3-6` |
| Lane selection | `classify` job runs `scripts/ci-classify.py`, outputs `lane` ∈ {`fast-landing`,`docs`,`full`} | `tests.yml:16-36` |
| `pytest-matrix` job | `if: needs.classify.outputs.lane == 'full'` | `tests.yml:300-303` |
| `pytest-matrix` timeout-minutes | **45** (raised from 30; comment cites 27m43s / 25,774 tests on 2026-09-04 with 3 sibling runs cancelled at the old 30-min cap) | `tests.yml:305-308` |
| `-n` (xdist workers) | **4**, set inside `scripts/test-merge:78` (`pytest -q --tb=short --ignore=tests/voice_eval -n 4 "$@"`), not in the workflow YAML itself | `tests.yml:380` invokes `scripts/test-merge`; `scripts/test-merge:77-78` |
| `--durations` | **absent** — no `--durations` flag anywhere in `tests.yml` or `scripts/test-merge` (`grep -n durations .github/workflows/tests.yml scripts/test-merge` → no hits) | — |
| Other lanes/triggers | `fast-landing` (needs `classify`, only `deploy/index.html`+registered landing tests, `tests.yml:38-80`); `docs` (prose-only allowlist + doc-contract bundle + linkcheck, `tests.yml:82-170`); `shell`/`python-policy`/`js`/`rust` all gate on `lane == 'full'`; `ci` is the sole required branch-protection context aggregating all of the above (`tests.yml:567-677`) | `tests.yml` throughout |

**fd-exhaustion diagnosis (WAVE-LOG.md, "fd" search), 5 lines:**
1. **Found**: two measured passes (7,097-test chunked run; 10,533-of-26,095 single-process run at CI's own invocation minus `-n 4`) found **no per-test fd accumulator** — fd count only drifted 13→17 (`WAVE-LOG.md:169`).
2. **Found**: the one real (if minor) leak located — `tests/test_conversation_history.py` never closes its `ConversationStore` (GC-recovered fixture teardown) (`WAVE-LOG.md:169`).
3. **NOT reached**: the back half of the 26,095-test suite was never measured (the single-process pass covered only 10,533 of 26,095) — the diagnosis is incomplete, and CI's actual failure mode (4 xdist workers against a tight runner-process ulimit, `WAVE-LOG.md:169`) was never reproduced directly.
4. **NOT reached**: no fix PR was ever opened for the leak (`WAVE-LOG.md:169`: "No fix PR"); the owner's requested `--durations=25` (`WAVE-LOG.md:95`) was never added either.
5. **Landed since**: only the `timeout-minutes` bump. `git log --oneline -20 -- .github/workflows tests/conftest.py` shows `6d42ba14f ci: give the full pytest lane headroom over its 30-minute limit` (matches `WAVE-LOG.md:93` `#3963`/`1cbde81` — squashed to a different SHA on merge) as the only workflow-touching commit in that window; **no commit touches `tests/conftest.py`** in the last 20 relevant commits, and grepping `tests/conftest.py` finds no fd/leak-cleanup fixture (only unrelated xdist-worker/timeout comments at `tests/conftest.py:102,109,111,281,312`). The fd leak and `--durations=25` remain fully unaddressed at HEAD.

---

## C. Record digest

### Lessons still binding

**Mechanics that worked:**
- Verify-premise-first before any relocation/deletion (killed 2 of the audit's row candidates outright: `WAVE-LOG.md:62-63` row 2.1 twice, `WAVE-LOG.md:143,147` rows 3.2/3.10) — cheaper than shipping a wrong PR.
- Two-pass code-review specifically for cross-package-depth relocations: caught a HARDWARE-FATAL unre-levelled deferred import that would have crashed every real measurement window (`WAVE-LOG.md:81`).
- `astmove`/`astsame` (AST-identity diffing) as the acceptance gate for pure relocations — used on nearly every wave-3/5/6 row.
- Batched integration branches (one CI run per batch) once serial per-PR rebasing was shown to be the actual bottleneck (`LANDING.md:117,123`), not CI runtime itself.
- Merge three-way, never `git checkout --ours`, on a generated file or doc a lane touched (`WAVE-LOG.md:171`) — `--ours` twice silently destroyed work (reinstated a deleted entry point at `LANDING.md:117`; dropped a sibling's prose at `WAVE-LOG.md:171`), caught only by re-merging, not CI.

**Mechanics that lost work / are flagged as wrong in hindsight** (`LANDING.md:107-122`):
1. "Shape after size": exit-code vocabulary was unified in wave 4, but the refusal-document *shape* (5 incompatible JSON shapes) was left open for 3 more waves while 8 PRs of pure relocation shipped — the LLM never benefits from relocation, only from shape.
2. AST-identity relocation moves carried surface concerns into the engine verbatim without giving them an honest contract (row 3.3 put `argparse.Namespace`+`print(` into `startup_load.py`; wave-7 row 7.4 had to pay it back).
3. TARGET.md was never re-verified against HEAD after each wave — 6 sentences went stale with no row ever owning the fix (σ kind, `LinearizationState` fields, `reason=transport`, stdout summaries, `position_cycle` on bank, a "new row" future-tense claim).
4. **The CI fd leak (sub-task B) is explicitly named as the worst instance of this**: "noticed in wave 4, stayed a follow-up for three waves and taxed every PR... it should have been a wave-4 row" (`LANDING.md:122`).

### Follow-up / held / owner-question / not-started items and where they sit now

| Item (source) | Now |
|---|---|
| CI fd leak + owner-requested `--durations=25` (`WAVE-LOG.md:95,169`; explicitly re-flagged as a process failure at `LANDING.md:122`) | **ORPHAN** — no row in `LANDING.md` §4, no entry in §6. Confirmed still absent at HEAD (sub-task B). |
| Role-swap risk in the topology clamp, flagged "Non-negotiable adjacent" (`AUDIT-FRESH-EYES.md:71-76`, `web/correction_crossover_v2.py:6172-6181` at the audit's SHA) | **RESOLVED, not via a tracked row** — verified fixed at HEAD: `jasper/web/correction_crossover_v2.py:5718-5726` now fetches both bands `context.declared_band("tweeter"/"woofer")` by name, with a comment explicitly rejecting tuple-position reads. Landed as wave-4 row 1.8 / PR #3965 (`WAVE-LOG.md:93`) before the audit's fix was ever turned into a LANDING row — the tree is ahead of the record here. |
| `resolve_conductor_context` relocation, audit's "one split" for the 7,608-line `web/correction_crossover_v2.py` god file (`AUDIT-FRESH-EYES.md:93`) | **RESOLVED**, not named in LANDING §4/§6 either way — verified at HEAD: the function now lives at `jasper/active_speaker/crossover_v2/conductor_context.py:303`, and the web host is down to 7,166 lines (`wc -l`), matching wave-5 row 1.7 / PR #3962 (`WAVE-LOG.md:93`). |
| `jasper-crossover-prescriber packet` dumping the whole curve doc with no `--out` (`AUDIT-FRESH-EYES.md:80-85`) | **RESOLVED** — verified at HEAD: `jasper/cli/crossover_prescriber.py:202-209` (`PACKET_ARTIFACT = "packet.json"`) writes beside the round by default; matches wave-4 PR #3958 (`WAVE-LOG.md:93`). |
| Three private path-or-stdin JSON readers (`WAVE-LOG.md:96,121`) | Has a row-equivalent: wave-7 row 7.5 / PR #4019 converged them (`WAVE-LOG.md:161,165`, "net +8, 3→1"). |
| A **fourth** path-or-stdin reader missed by 7.5, at `cli/aec_sweep_config.py:33` (`WAVE-LOG.md:167`, "Verified, not shipped") | **ORPHAN** — confirmed still present and unconverged at HEAD (`_read_payload`, `jasper/cli/aec_sweep_config.py:33`); not in `LANDING.md` §4 or §6. |
| `CommissioningRunStore.replace_current`, SPENT verdict pending a test re-expression (`WAVE-LOG.md:167`) | Has a pointer, not a row: named only in `LANDING.md:174`'s wave-7 coordination note ("commissioning_run.replace_current" owned by "wave 7 (other session, in flight)") — no dedicated row in §4's table, no §6 entry either. Borderline orphan; at minimum unresolved in the tracked plan. |
| `tests/test_ring_active_endpoint.py:4180` asserting on source text (`WAVE-LOG.md:167`) | **ORPHAN** — no mention anywhere in `LANDING.md`. (Line has since drifted; file is now 4,210 lines; content at the new offset reads as a docstring, not an obvious source-text assertion — flagging as COULD-NOT-DETERMINE whether the underlying defect still exists verbatim, but the *tracking* gap is confirmed regardless.) |
| `jasper-round wait`'s session-directory doc one-liner, audit S6 (`AUDIT-FRESH-EYES.md:27`) | **ORPHAN** — friction-only per the audit itself, but genuinely absent from both `LANDING.md` §4 and §6. |
| held-back cli→web items: `WiredStimulusCapture`, `crossover_v2_status_block`, `GRADE_*` (`WAVE-LOG.md:96`) | Split: `WiredStimulusCapture` → row **8.2**; `crossover_v2_status_block`/`GRADE_*` → §6 not-doing ("closing the doctor's three cli→web edges... no walk benefit", `LANDING.md:244`). Both accounted for. |
| TOOLBOX GAP: no tool banks a pre-registered expectation delta (`WAVE-LOG.md:67`) | Resolved into row **8.5** + DoD item 3 (`LANDING.md:187,207-208`, "the pre-registered expectation round-trip through the round directory, pinned"). |
| Row 3.7 (NN, camilla clamp helper) / #3991 hardware pass; #3934 | Both closed per record: `WAVE-LOG.md:173` — "Row 3.7 (#3991, NN) merged by the owner at d048426 after the hardware pass; #3934 (owner) merged at 35f6812." Confirmed still true (sub-task D: no open PRs at all). |
| Row 8.11 (RecordStore evidence-publisher fold, ADR-0227 ruling 12) | Row exists (`LANDING.md:193`) and is **merged**: PR #4024, `merged: true`, `merged_at 2026-09-04T19:35:04Z` (sub-task D lookup). |

**Orphans, summarized** (the finding the brief asks for): the CI fd-leak/`--durations` follow-up, the 4th JSON-reader at `cli/aec_sweep_config.py:33`, the `test_ring_active_endpoint.py` source-text assertion, the `jasper-round wait` doc one-liner, and (borderline) `CommissioningRunStore.replace_current`'s test re-expression — none of these five have a row in `LANDING.md` §4 nor an entry in §6's not-doing list. Two other candidate orphans (the topology role-swap risk, the `resolve_conductor_context` god-file split) turned out to be **already fixed at HEAD**, landed before/alongside the audit that flagged them — the record's own audit doc was stale relative to the tree even at the time it was written.

---

## D. Open PRs in scope

`mcp__github__list_pull_requests(owner=jaspercurry, repo=JTS, state=open, perPage=50)` → **empty result** (single page; connectivity independently confirmed by a `state=all` call returning real, recently-closed PRs, so this is a genuine zero, not an auth/pagination artifact).

**There are zero open pull requests in `jaspercurry/JTS`** as of this check. Consequently:
- No open PR touches `jasper/cli`, `jasper/active_speaker`, `jasper/audio_measurement`, `jasper/attribution`, `docs/tuning-*`, `docs/measurement-loop-doctrine.md`, or `tests/test_correction_boundary_ssot.py` — there is nothing for another lane to avoid right now.
- No open `claude/tuning-rightsize/*` branch exists as a PR. The most recent one, `claude/tuning-rightsize/w8-11-record-store-seam` (#4024, row 8.11), is **merged** (verified via `pull_request_read`: `state: closed`, `merged: true`, `merged_at: 2026-09-04T19:35:04Z`, base `main@fc27f1f75`).
- This matches and extends the record's own close-out note at `WAVE-LOG.md:173`: "Nothing from waves 1–3 or 7 remains open" — now true of wave 8 and the whole repo too, at least at this instant.

**Caveat**: this is a point-in-time snapshot (checked 2026-09-05); other agents in this multi-agent repo can open PRs at any moment, so a lane relying on "nothing is in flight" should re-check immediately before acting on it, not trust this report.
