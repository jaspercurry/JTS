# Handoff — JTS tuning right-size, wave 4 onward

Written by the wave-3 orchestrator (session_015N7EavRVFCHwXkKTPLEJNF) for the
next orchestrating agent. Read AGENTS.md at HEAD first, then this file, then
`EXEC-W3.md` (the sub-agent brief; reuse it with the prefix bumped to `w4-`)
and `web-twin-map.md` (the row 2.4 map). Verify every claim below against
the tree before acting; the tree wins.

## 0. What wave 3 landed (do not redo)

Twelve PRs, each merged individually after `/simplify` + `/code-review` (agent)
and a second `/code-review` (orchestrator); prose PRs additionally passed an
Opus constants review against main before acceptance.

| PR | concern |
|---|---|
| #3935 | doctrine §1 names a tool per loop verb |
| #3943 | `driver_protection.py` §5 citations → ADR-0227 §1 (2 of 3; the third was right) |
| #3942 | lazy façade → `state_paths`; `startup_load` re-export shim deleted |
| #3939 | `jasper-round-views inventory`; one `ARTIFACT_BY_VIEW` table feeds writers and inventory |
| #3951 | `attempts_loop.replay/first_stop_index/summarize` deleted (SPENT) |
| #3948 | crossover_v2 prose, third pass: 22 files, 5,422 → 5,105 prose lines |
| #3947 | gating `getsource` pins → fragment-field behaviour pins |
| #3869 | six zero-caller defs deleted with SPENT/SUPERSEDED verdicts |
| #3946 | `correction/peq.py` → `audio_measurement/peq.py` (whole module) |
| #3949 | `web_commissioning` calls `audio_measurement.playback` directly |
| #3952 | `correction/coordinator.py` → `jasper/measurement_window.py`; pin: nothing in `active_speaker/` imports `jasper.correction` |
| #3950 | row 2.4 step 1: `_status.py` 858 → 240; six derivations moved into `crossover_envelope_v2.py` |

Not done, by decision (and why): further prose passes (the package is at the
constraint floor — two reviewer samples: 124/3/53 and 89% constraint); t3 test
docstrings; heavy-test prose-only trims; row 2.6 regroup-rename (churn, 273
refs); row 2.7; Phases 3–5; angle-walk widening (a feature, not right-sizing).

## 1. Corrections to the plan of record (PLAN.md), verified at HEAD

- **Row 2.1 as written was moot.** `audio_measurement` had zero code imports
  from `correction` (PR #3542 broke that cycle). The real cycle was
  `correction` ↔ `active_speaker`; its one up-edge cannot move (the graph
  classifier is `runtime_contract`'s entry point, 19k-line closure, abuts the
  hearing clamp). It was broken from the engine side (#3946/#3949/#3952).
  `correction` now sits above `active_speaker` with one edge; the
  `room_boundary.py` placement argument still holds and its note says so.
- **Row 2.4 is ten ordered steps, not one PR** (`web-twin-map.md`). ~21% of
  the 9,975 lines is duplicate code; ~4,900 lines are engine work with no
  engine home (three of the plan's six destinations do not exist). Spine:
  durable state (step 4) before assembly/publishers/playback/apply. Steps 6
  (playback: excitation-ceiling path) and 10 (level lease: `set_volume_db`
  path) are non-negotiable tier → `/adversarial-review`.
- The `_status.py` "TWIN" verdicts in the map mean twin *concern*, not
  duplicate code; step 1 was a relocation with byte-identical output, not a
  deletion. Expect the same for later steps.

## 2. Wave 4 backlog, in the order I would run it

**A. Row 2.4 steps 2–4** (Opus; one PR each; merged individually; the prose
lanes are done so the owner files are free):
- Step 2: copy/refusal twins → `refusal_copy.py` (~130 lines).
- Step 3: grading → `verification.py` (`_post_apply_grade`, `_spatial_grade`,
  `classify_program_failure`, ~422). No NN adjacency.
- Step 4: durable state → `durable_state.py` (`load/save_v2_state`,
  `_update_current_review`, `reset_v2_journey_state`, `observe_*`,
  `persist_conductor_state`, ~700). Fold in the #3950 follow-ups: move
  `review_declined` into `durable_state.py` (collapses the three
  `crossover_v2_phase(x, review_declined=review_declined(x))` pairings);
  re-home `crossover_v2_phase` → `journey.py` and the two cloud projections →
  `durable_state.py`.
- Then 5 (publishers), 9 (apply/rollback — parallel with 6–8 accepting one
  conflict window in `durable_state.py`), 6 (playback, NN), 7 (`_wired.py` →
  `capture_wired.py`), 8 (session assembly, last of the big ones), 10 (level
  lease, NN, last).

**B. Small follow-ups surfaced this wave** (Opus or Sonnet, one PR each):
- Converge `jasper/measurement_window.py` with `control/measurement_hold.py`
  (same concern; the agent judged it out of scope for the move).
- `driver_protection.py` ~179 comment block duplicates ADR-0227 §9 nearly
  verbatim → pointer (prose-only; the constants review gate applies).
- `attempts_loop.DECISIONS` is test-only vocabulary now (kept "in doubt").
- `docs/historical/**` carries 4 stale paths from #3952 (frozen records;
  leave unless the owner wants them repointed).

**C. Toolbox** (Opus): the "recommend" half-verb — nothing banks a
pre-registered expected delta while ruling R8 leans on it. Candidate: an
`expectation.json` beside the round written by `jasper-crossover-prescriber
propose`, read by `jasper-round-views frozen`/`agreement`. Owner decision
first (§4).

Phases 3–5 stay after Phase 2. The prose program is closed.

## 3. Mechanics that cost us time this wave (add to the brief; most are in EXEC-W3.md)

- **Relocations across package depth:** resolve every relative import,
  module-scope AND deferred, with `importlib.util.resolve_name` + `find_spec`
  before push, and pin it with an AST walk. #3952's first head missed three
  deferred `..control` imports; tests stub the callers so the suite passed;
  it would have killed every measurement window on hardware. The agent's
  second `/code-review` pass caught it. Keep the two-pass review.
- **`astsame.py` compares against the merge-base now**, not `origin/main`;
  main moves under a branch during a wave.
- **Reviewer false positives:** two "symbol does not exist" findings on
  #3948 came from a reviewer reading a pre-rename main. Verify symbol claims
  at HEAD before sending a PR back.
- **Container:** `/root/.local/bin/pytest` lacked numpy/scipy/pyyaml and
  `pytest-asyncio`; agents built throwaway venvs. 45 async tests in
  `test_measurement_window.py` "fail" locally for that reason alone. Compare
  failing-ID sets against a pristine `origin/main` worktree before believing
  a local failure; CI is the authority.
- A commit guard that greps for `" passed"` also matches `"45 failed, 82
  passed"`. Test for the absence of `failed`.
- `/code-review` inside an agent defaults to cwd; run it from the worktree.
- Prose passes on `crossover_v2/` are at the floor: ≈130 lines removed by two
  Sonnet lanes for ≈575k tokens, and every accepted PR still had one
  compressed-into-false sentence the constants review caught. Do not
  reopen without a rewrite-against-contract reason.
- Row 2.1 cost two stopped agents (~185k tokens) before the third shipped;
  both stops were correct premise checks. A "verify the premise first, stop
  if false" clause in the assignment is worth its cost on every relocation.

## 4. Open questions for the owner (ask, do not assume)

1. Rulings with no ADR, kept as one-line comments: `capture_source.py`
   Decision 13 / #2662; `blend_correction.py` "refusals HOLD the incumbent"
   (panel ruling 2026-08-18, no issue number); MS-4 and MS-14
   (`playback_transaction.py` / `program_transaction.py`; MS-4 survives only
   in `docs/historical/`, MS-14 nowhere). Collect into an ADR-0227-style
   record?
2. Toolbox gap: bank the pre-registered expected delta (§2 C)?
3. D11 (room correction is a separate product): moving its PEQ math down
   into the truth layer (#3946) was treated as consistent with D11. Confirm,
   or record it as a D11 amendment.
4. The dead-defs PR (#3869) is merged; `DECISIONS` in `attempts_loop.py` is
   the one held-back symbol. Delete or keep?
