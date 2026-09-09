# Orchestrator handoff — seat-matched tuning program

You are taking over as the orchestrating session for the seat-matched tuning
program (issue #4502). The previous orchestrator's context is spent; this file
is the whole handoff. Read it, then `seat-tuning-program/PLAN.md` (§9 status
log first, newest entries at the top), then the briefs it names. Everything
below is true as of 2026-09-09 12:40Z, `origin/main` at `18cba5142`.

## 1. Where the program stands

Landed on main (all squash-merged, each reviewed against its brief):

| Row | PR | What |
|---|---|---|
| Wave 0 / 0b | #4488, #4517 | ADR-0255…0260 (wired-only; room ceiling = applied tune's trusted floor; bass resumes; sides × roles; one toolbox; flexible categorized poses, no nearfield rung) |
| 1.0 | #4510 | wired capture kernel in `jasper/audio_measurement/wired_capture.py` |
| 1.4 (lane B) | #4522 | pose vocabulary; `seat/cube` (7 poses), `seat/express`, `close/spot` |
| 1.5 (lane B) | #4524 | `room-ceiling`, `room-median`, `room-persistence` views; `room_median.json` |
| 1.7 (lane B) | #4525 | methodology §11 "Room", runbook "Room" walk |
| 2.3 (lane C) | #4520 | wall distances in declared geometry; `boundary-prior` view |
| 2.1 (lane C) | #4544 | Layer-3 room candidate kind: `audio_measurement/room_limits.py`, `crossover_v2/room_prescription.py` door, `room_correction` candidate field, `room_candidate` graph scope |
| 2.2 (lane C) | #4546 | `room-grade` view |
| 1.1 (lane A) | #4557 | `level_match`, household-mic record, `SNR_BANDS_HZ` moved into `audio_measurement` |
| 1.4 (lane A) | #4563 | bass wizard and parked apply pathway retired |
| 1.3 (lane A) | #4567 | in-product LLM client (`jasper/calibration_agent/`) and room-wizard LLM hooks retired |
| 1.2 (lane A) | #4602 | `jasper/correction/` and the room product retired (−41k lines) |
| 1.5 (lane A) | #4604 | `jasper-mic-calibration models\|fetch\|upload\|show`; ADR-0265 amends ADR-0259 §4 |
| 1.6 (lane A) | #4603 | doctor: a round-tripped active graph is managed |

Lanes A, B and C are complete except the owner's hardware rows (1.6 first
wired seat-cube session; 2.4 first room round through the candidate).

## 2. What is open: lane D (bass), the only unfinished lane

The lane D session (title "JTS D") built rows 3.1–3.5 on eight stacked
branches and never opened a PR. As of 12:40Z, none has moved since 11:31Z:

| Branch | Ahead / behind main | Content |
|---|---|---|
| `claude/seat-w3-3-1-bench-binding` | 2 / 177 | `bench/wired_play.py`: `WiredPlayAndCapture` mirroring `null_door._play_and_capture` (wired-only, faithful) |
| `claude/seat-w3-3-1b-bench-field` | 15 / 166 | unknown; probably the CLI binding 3.1 lacked |
| `claude/seat-w3-3-2-bass-fit` | 1 / 214 | `bass_extension/seat_fit.py`, `cli/round_views/bass_fit.py`, adapter parameter models |
| `claude/seat-w3-3-3a-bass-candidate` | 6 / 166 | Layer-2 scheduled candidate kind (row 3.3) |
| `claude/seat-w3-3-3b-bass-emission` | 8 / 166 | emitter stage for the bass candidate (row 3.3) |
| `claude/seat-w3-3-4a-rung-graph` | 10 / 166 | protection ladder graph (row 3.4, NN) |
| `claude/seat-w3-3-4b-ladder-view` | 12 / 166 | ladder view (row 3.4, NN) |
| `claude/seat-w3-3-5-bass-docs` | 1 / 75 | docs (row 3.5) |

Pre-review findings (Sonnet, read-only, 07:40Z; posted on #4502, unanswered):

- 3.1 as pushed is the capture kernel without the binding: `jasper/cli/bass_extension_bench.py --live` still refuses unconditionally, `TargetPlan` is not bound, `BenchRoleExecutor` has no caller, `require_wired_mic` is never reached from the live path, and `bass_extension` was not added to `PACKAGE_BOUNDARIES` in `tests/test_audio_measurement_boundary_ssot.py` (renamed from `test_correction_boundary_ssot.py` by #4602). Check whether `3-1b-bench-field` closes these.
- 3.2 wrote a third `room_median.json` parser (`seat_fit.read_seat_median`) with no `window` check. Consume `crossover_v2/room_prescription.read_room_median` (refuses `window != "ungated"`, carries the normalized median and `level_reference_db`) and delete the duplicate.
- Registration (`ARTIFACT_BY_VIEW`, `_FAMILIES`, `_VIEW_RUN`) and side-effects (reads no capture, changes no graph or candidate) were clean on 3.2.
- Since these branched, main gained: `room_boundary.ROOM_MEDIAN_WINDOW`, the door reader, `room_limits.py`, the `room_candidate` scope (#4544); the bass wizard's apply pathway deletion, `_intent_payload` moved to `apply_intent.py`, `LADDER_INCOMPLETE` removed (#4563); `jasper/correction/` gone and the boundary test renamed (#4602); `jasper-mic-calibration` (#4604). Expect conflicts in `bass_extension/{__init__,apply_intent,profile}.py`, `bench/`, `crossover_v2/door.py`, `cli/round_views/{__init__,_common}.py`, `tests/test_cli_exit_vocabulary.py`, the runbook's generated menu cell (regenerate with `scripts/generate-tuning-tool-menu.py`, never hand-merge).

### Landing plan for lane D

Precondition: the owner stops the "JTS D" session first (two authors on one
branch collide). The owner has said rebasing is fine.

1. One Opus agent per row, in order 3.1 (+3.1b), 3.2, 3.3 (3.3a+3.3b), 3.4
   (3.4a+3.4b), 3.5. Each: worktree from the branch, `git rebase origin/main`
   (force-push allowed on these branches now), resolve conflicts against the
   landed code above, fix the pre-review findings for that row, run the
   validation ladder (§4), open the PR with the standard body (§5).
2. Per PR: Sonnet claim check (grep proofs, census, stale importers, collision
   map, rebased, consumes `read_room_median`), then Opus design review against
   `briefs/wave-3-bass.md` and ADR-0257/0258/0260. Rows 3.4 and anything
   touching the DSP output path, `dsp_apply.py`, limiter or `install.sh` are
   non-negotiable tier: `/adversarial-review` too, and the merge waits for
   the owner's hardware pass (jts3, wired UMIK-2).
3. Push review fixes via an Opus agent (one commit, no history rewriting once
   a PR is open); merge (squash) on green; unsubscribe; update PLAN.md §5 row
   and §9; comment on #4502 when a row lands.
4. Fold 3.5 (docs) last, regenerating the menu.

## 3. Process that worked (keep it)

- Orchestrator: design judgment, adjudication, merging, PLAN.md, #4502
  comments. Opus: design reviews and every fix/implementation commit. Sonnet:
  read-only claim checks, CI-log triage, collision maps, pre-reviews.
- Every review prompt names the brief row, the ADRs, `AGENTS.md`
  non-negotiables and Defaults, `PACKAGE_BOUNDARIES`, and asks for
  `file:line` evidence, a verdict, and findings tagged must-fix / follow-up /
  note. Fix agents get the findings verbatim with file:line and "do not widen".
- CI red: first rule out a failure that is not the PR's (a module the diff
  does not touch; red on main at the base too). Known flakes: the wall-clock
  `tests/test_airplay_volume_hook.py::test_a_session_start_fade_up_is_adopted_once_at_its_settled_level`
  (re-run once after a standing-down comment; harden in its own PR if it
  recurs); main was briefly red on `tests/test_audio_hardware_reconcile.py`
  before #4512 (fixed).
- Check-ins: `send_later` ~60 min while anything is open; subscribe to each
  PR with `subscribe_pr_activity`; unsubscribe on merge.
- Owner merges some PRs themselves; that is fine. Post-hoc review still runs.

## 4. Validation ladder and container quirks

- No `.venv` in the container. Build one per worktree: `uv sync` fails
  (the `camilladsp` GitHub archive is 403 through the proxy); create a Python
  3.13 venv, pip-install the dependency groups from `pyproject.toml` from
  PyPI (skip `camilladsp` and `pyalsaaudio`; `pycamilladsp` at its pinned
  commit installs from a `git clone` of the public repo), install the system
  `libportaudio2` if `sounddevice` import fails, then `pip install -e . --no-deps`.
- Ladder: `ruff check jasper tests scripts`; `lint-imports` (2 kept / 0
  broken); `mypy --python-version 3.13` on touched modules (bare mypy dies on
  numpy stubs under the configured 3.11); `PYTHONPATH=. .venv/bin/python
  scripts/generate-tuning-tool-menu.py --check`; `python scripts/docs-linkcheck.py --all`;
  `python scripts/docs-impact.py --validate-only`; the affected suites; then
  `scripts/test-fast` and quote the final `==> test-fast: …` sentinel.
- Tolerated failures only under uid 0 in this container (all reproduce on
  clean main): `tests/test_active_speaker_bundles.py::test_open_bundle_returns_none_and_warns_on_write_failure`,
  `tests/test_tool_catalog.py::test_write_catalog_fail_soft_on_unwritable_path`,
  `tests/test_install_helpers.py::test_streambox_env_refresh_writes_the_profile_through_the_shared_lib`
  (no rsync), `tests/test_angle_capture_take.py::test_consumed_is_read_back_from_the_spool_not_asserted`.
- `git merge-tree --write-tree origin/main <branch>` works here for conflict
  probes. No `gh`; use the GitHub MCP tools (`mcp__github__*`). PR bodies over
  ~10 KB: fetch and PATCH rather than retyping.
- ADR numbers are reserved by comment on issue #4405; 0265 is the newest
  used; #4405 carries a double reservation on 0262 (flagged, unresolved).

## 5. Conventions (non-negotiable for every PR)

- Commit trailer, last lines: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  and `Claude-Session: <your session URL>`. No other model identifier in
  code, commits, PR titles or bodies.
- PR body: Summary; Line delta; Verdicts (SUPERSEDED / SPENT / PROMOTE with
  grep proof for every deletion); Test plan checkboxes (hardware-free
  validation; Hardware/Pi evidence or N/A with reason; no voice-eval);
  Validation evidence with sentinels verbatim; Notes for reviewer (stale, not
  fixed here; follow-ups); end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
  and the session URL.
- GitHub comments end with `---` and `_Generated by [Claude Code](https://claude.ai/code)_`.
- Squash merge with the PR title as the commit title; branches auto-delete.
- Never rewrite history on a branch with an open PR; add commits.
- `tests/voice_eval/` is paid; never run it.

## 6. Open items besides lane D

- Owner decision: the crossover done screen can mint zero actions on a first
  tune after #4602 (`crossover_envelope_v2.py` ~2738); mint a "Back to Sound"
  filler (`{"id":"sound_hub","label":"Back to Sound","href":"/sound/"}`, the
  shape the JS test fixture already uses) or accept the bare screen.
- Owner hardware rows 1.6 and 2.4; then the plan's later waves (4 limiter
  campaign and runtime scheduler; 5 one headroom budget; 6 per-side emission
  and the cardioid variant) — briefs not yet written; write them the way
  `briefs/wave-2-room-candidate.md` is written and review each against main
  first (every brief so far had at least one false premise at HEAD).
- Follow-ups parked in PLAN.md §9 (search "Follow-ups"): `baseline-reemit`
  and `setup_status` recompose without `room_peqs`; multi-side room refusal
  wants its own slug (ADR-0258); `_prescription_common.py` for the three
  doors' shared privates; `DefaultSetupCalibration.from_household`; tuning
  spend ledger consolidation; `jasper-correction-web` keeps a `jasper-secrets`
  group membership it no longer needs (install.sh, NN tier); #4594 (another
  program) still echoes `sound/room/` in `NGINX_PUBLIC_SURFACE`; eleven
  producer-less `BassExtensionRefusal` members for lane D's 3.3 to prune.

## 7. Kickoff for the fresh session

Paste this, then this file's path:

> You are the orchestrating session for the JTS seat-matched tuning program
> (issue #4502). Fetch `claude/loudspeaker-tuning-architecture-iephfa` and read
> `seat-tuning-program/briefs/HANDOFF-ORCHESTRATOR.md` first, then
> `seat-tuning-program/PLAN.md` §9 and `briefs/wave-3-bass.md`. Land lane D
> (rebasing is allowed; the owner has stopped the "JTS D" session), then keep
> the program moving. Delegate: Opus for reviews and every commit, Sonnet for
> read-only checks; you adjudicate and merge. Keep PLAN.md and #4502 current.
