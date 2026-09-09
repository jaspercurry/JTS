# Orchestrator handoff — seat-matched tuning program

You are taking over as the orchestrating session for the seat-matched tuning
program (issue #4502). Read this file, then `seat-tuning-program/PLAN.md` §9
(status log, newest first — it carries every ruling below with its evidence),
then `briefs/wave-3-bass.md` for the lane still landing.

True as of 2026-09-09 ~15:35Z, `origin/main` at `4157a6030`. This is the
second handoff; the first one's plan for lane D was wrong about the branch
topology, and §1 explains how.

## 1. Where the program stands

Lanes A, B and C are complete on main except the owner's hardware rows (1.6
first wired seat-cube session, 2.4 first room round). **Lane D (bass) is the
only unfinished lane and is mid-landing.**

| Row | State |
|---|---|
| 3.2 `bass-fit` view | **LANDED** — PR #4641 squash-merged at `4157a6030` |
| 3.1 part 1 (bench seam) | **PR #4643 open, DO NOT MERGE on CI alone.** 4 must-fixes + 2 adversarial blockers; a fix agent was still pushing at handoff (its head moved five times in the last half hour, latest seen `af3e7a79a`, main merged in and the conflict resolved). **Read the current head — do not trust any SHA in this file for that branch** — and verify each finding in §3 against it |
| 3.2b adapters honour the margin policy | **not started; gates 3.3** |
| 3.3 candidate kind + emission | **PR #4660 open** on base `main`, `+3198/−1846` over 48 files, reported in full. Needs `/adversarial-review` + owner hardware pass; neither has run. **NN** |
| 3.4a rung graph | unopened branch `claude/seat-w3-3-4a-rung-graph` (`628432b2b`); **NN** |
| 3.4b ladder view | unopened branch `claude/seat-w3-3-4b-ladder-view` (`eda8ef1a5`) |
| 3.1 part 2 (`--live` binding) | unopened branch `claude/seat-w3-3-1b-bench-field` (`a36c06172`) |
| 3.5 docs | unopened branch `claude/seat-w3-3-5-bass-docs` (`12aba24f0`) |

**The branch topology the first handoff got wrong.** The eight original lane D
branches are one stack (`3-2 → 3-3a → 3-3b → 3-4a → 3-4b`), an independent
`3-1`, and `3-1b-bench-field`, which is a *merge* of `3-1` and `3-4a` plus two
commits that consume 3.3's candidate field and 3.4a's scope. So "3.1 with 3.1b"
cannot land first. Landing order: **3.1 part 1 → 3.2b → 3.3 → 3.4a → 3.4b →
3.1 part 2 → 3.5** (3.2 is done).

### 1a. Row 3.3's branch needs a rebase that drops row 3.2's commits

`claude/seat-w3-3-3-bass-candidate-kind` was built on top of row 3.2's branch
*before* 3.2 squash-merged, so its first three commits (`b52eb253c`,
`5c65cb146`, `6d7bb96ff`) are 3.2's content, now on main as one squashed
commit. Replay only its own ten with
`git rebase --onto origin/main 6d7bb96ff claude/seat-w3-3-3-bass-candidate-kind`.
It has no PR yet and conflicts with main in three files:
`docs/tuning-operator-runbook.md` (regenerate), `jasper/bass_extension/profile.py`
(the `LADDER_INCOMPLETE` trap in §2.8) and
`tests/test_active_speaker_baseline_profile.py`. Its ten commits go well past
the original 3.3a/3.3b: they also delete the apply-intent record and retire the
legacy profile's authority. **Review it as new work, not as a rebase of the
pre-read.** The agent that built it never reported, so nothing about it has
been verified — treat the pre-read at `/home/user/wt/PRE-3-3.md` (regenerate if
the container is gone) as describing the *older* 3.3a/3.3b branches only.

## 2. Rulings already made — do not relitigate, all recorded in §9

1. **3.3 stays one PR** (candidate kind + emission). Splitting leaves a window
   where a candidate carrying a bass field applies without its bass layer.
2. **3.4 splits into two PRs** (3.4a rung graph, 3.4b ladder). The intermediate
   state is fail-closed and it isolates the NN diff.
3. **The sustain test is not duplicated in the ladder.** The bench owns
   `sustain_stress` under the frozen limiter-evidence protocol. The ladder
   discloses it carries swept-sine evidence only; the door already requires
   ladder AND limiter evidence for any boost. This supersedes brief row 3.4's
   wording. **The owner was told and has not objected.**
4. **"The human starts each level" must be fixed before 3.4a merges** — one
   level per invocation for the `bass_candidate` scope, with a refusal code,
   or a per-level confirmation. Not a change to the shared session loop.
5. **No `MeasurementProgram("bass","ladder")` registration** — levels are not
   poses. What 3.4 must supply instead: each step records its position and
   `grade_ladder` refuses a mixed-position ladder.
6. **Per-driver caps come from the summed admission gate**, not from
   `assert_stimulus_band_protected`. Where the design and adversarial reviews
   disagreed on this, the adversarial reading won (see §4).
8. **`BassExtensionRefusal.LADDER_INCOMPLETE` must survive every rebase.**
   #4563 deleted it from `profile.py` as producer-less; row 3.4b's
   `round_views/bass_ladder.py` uses it live. Lane D moved the enum to a new
   `refusals.py` while main edited it in place, so git reports delete-vs-modify
   in `profile.py` on every lane D rebase. Keep the move AND the member.
9. **Wave 4 gained row 4.1b**, the contract-revision ADR, because the frozen
   protocol blocks all production wiring until one names the accepted bundle's
   fingerprint and authorizes a caller, and the tap amendment forbids "a
   scheduler" by name.

## 3. Findings still open

- **#4643's six** (a fix round was in flight at handoff — verify against the
  head rather than assuming any are done): the sustain hold rendered as a sweep so admission judges it
  by a per-sweep ceiling (no campaign can ever reach `accepted` — this would
  have failed at the speaker during wave 4.1); the hand-composed play seam
  reverting to the engine's binder (`confirm_graph_is_live` fingerprints a
  *normalized read-back*, not bytes — the branch's stated reason was false);
  a ~200 MB correlation peak on a 1 GB Pi; a defaulted `thd_max_ratio: 0.0`
  the protocol forbids; plus the two blockers in §4.
- **Row 3.2b** (gates 3.3): `sealed.generate_family` ignores
  `subsonic_corner_ratio`/`subsonic_order` (ships 15 Hz 2nd-order where
  `conservative` declares 21.8 Hz 4th-order — at 10 Hz that is −1.8 dB against
  −21 dB, on a rung with +6 dB of infrasonic boost and no excursion model) and
  `generate_ported_family` ignores `boost_cap_db`.
  `_assert_bass_extension_safe` proves the `LT → subsonic → limiter` ORDER and
  never the subsonic's values.
- **`activation.py:186`** carries a pre-existing issue #2202 defect that will
  block the first supervised campaign regardless of row 3.1. Out of scope
  there; the owner needs it closed before wave 4.1 hardware time.
- **No excursion margin is computable anywhere** — no Xmax, no Sd, no
  displacement model. ADR-0260's protection basis names "declared plant facts"
  as one of three legs and that leg cannot carry a number today; the in-room
  ladder and the limiter evidence carry it alone. Owner should decide whether
  that amends the ADR.

### 3a. A CI artifact that will mislead you on #4643

Every CI "failure" on that PR's intermediate heads on 2026-09-09 was a run
**cancellation** caused by the fix agent's own next push — verified from the job
logs, which read `pytest matrix result: cancelled`, not a failing test. The
aggregate `ci` and `pytest` checks report a cancelled matrix as a failure. Judge
that PR only on a run that completes undisturbed on a head nobody is still
pushing to.

## 4. What the reviews are for — the evidence they produced

Every review round here found something reading alone would not have:
the ported adapter admitting vented cabinets into a fit that needs a nearfield
null (31 of 72 synthetic in-room medians produced a wrong family, one demanding
9.03 dB under a 6.0 dB cap — the reviewer built the medians); the sustain hold
that could never be admitted (reproduced against the repo's own fixture); two
excitation-path blockers probed with real numbers (a band ending at `fc − ε`
passing while driving the tweeter ~6 dB below the woofer's stress level with no
cap of its own; the fader and graph proven three steps before audio is
emitted). **Keep the three-review pattern for NN rows: Sonnet claim check,
Opus design review, `/adversarial-review`.** Prompts that worked are visible in
this session's history; they name the brief row, the ADRs, AGENTS.md, and ask
for `file:line` evidence, a verdict, and findings tagged must-fix / follow-up /
note.

## 5. Process and conventions (unchanged, still binding)

- Orchestrator adjudicates and merges; **Opus for every review and every commit,
  Sonnet for read-only claim checks, CI triage and collision maps.**
- Agent conventions file used all session (workspace, ladder, PR body shape,
  trailers): recreate it from §5 of the previous handoff plus the container
  notes below; every agent was pointed at it before doing anything.
- Commit trailer, last lines: `Co-Authored-By: <your model> <noreply@anthropic.com>`
  and `Claude-Session: <your session URL>`. No model identifier anywhere else.
- PR body: Summary (with premises found false at HEAD); Line delta; Verdicts
  with grep proof per deletion; Test plan checkboxes; Validation evidence with
  sentinels verbatim; Notes for reviewer. Squash merge, PR title as commit title.
- Never rewrite history on an open PR — add commits, merge main in for conflicts.
- Post one review-round comment per PR before merging; comment on #4502 per row.
- `tests/voice_eval/` is paid. Never run it.

## 6. Container quirks (updated — the first handoff's recipe was incomplete)

- Build a venv per worktree: Python 3.13, pip-install the `full`+`dev`+
  `fast-landing` groups from PyPI, **skip `camilladsp` and `pyalsaaudio`**, and
  install pycamilladsp from a git clone at its pinned commit **with its
  dependencies** (`pip install <clone>`, not `--no-deps` — it needs
  `websocket-client`; the first handoff said `--no-deps` and that fails).
  Then `pip install -e . --no-deps`.
- Ladder: `ruff check jasper tests scripts`; `lint-imports` (2 kept / 0 broken);
  `mypy --python-version 3.13` on touched modules (bare mypy dies on numpy
  stubs); `generate-tuning-tool-menu.py --check`; `docs-linkcheck.py --all`;
  `docs-impact.py --validate-only`; affected suites; then `scripts/test-fast`.
- **Tolerated failures under uid 0 (all reproduce on clean main):** the four
  the first handoff lists, plus
  `tests/test_audio_hardware_reconcile.py::test_every_pass_ends_with_one_exit_event_carrying_its_own_status[mid_stage_abort-1]`
  (confirmed on a clean main worktree this session).
- `mypy` reports 3 pre-existing errors in
  `crossover_v2/feature_classifier.py` that no lane D branch touches.
- The runbook's generated tool-menu cell conflicts on almost every rebase.
  Regenerate it with `scripts/generate-tuning-tool-menu.py`, never hand-merge —
  **then `git add` it before committing, and verify with
  `git show HEAD:docs/tuning-operator-runbook.md`.** This session lost a CI
  cycle to exactly this: `git commit` during a merge commits the index, so a
  regeneration done after `git add` never lands, and a working-tree `--check`
  passes while the commit is still wrong.
- No `gh`; use `mcp__github__*`.

## 7. Owner decisions still open

- The crossover done screen can mint zero actions on a first tune (#4602):
  mint a "Back to Sound" filler or accept the bare screen. Asked twice, no reply.
- Hardware rows 1.6 and 2.4, then the hardware passes on 3.3, 3.4a, 3.4b.
- Whether the missing excursion model amends ADR-0260 (§3 above).
- #4405 carries a double reservation on ADR-0262, still unresolved.

## 8. Briefs

`briefs/wave-4-bass-runtime.md` is written and fact-checked (it carries row
4.1b and three corrected premises). Waves 5 and 6 are **not** written, but
their fact base is banked on this branch: `facts/wave-4-facts.md` (main
`a1994ef69`) and `facts/wave-5-6-facts.md` (main `d112fa5a1`), each with
`file:line` evidence. Treat them as raw material, not doctrine — re-verify
before writing, because **every brief in this program has shipped with at
least one false premise.** PLAN.md §9 (2026-09-09 15:20Z) summarizes the four
findings that change what waves 5 and 6 are, including a code-level blocker
for the cardioid row and two contradictions with ADR-0258 the owner should
settle first. Write them the way `briefs/wave-2-room-candidate.md` is
written.
