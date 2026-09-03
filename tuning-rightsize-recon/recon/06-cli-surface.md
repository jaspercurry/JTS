# Recon 06 — the LLM-facing CLI surface (jasper/cli/, experiments/usb-turntable/, scripts/)

Scope at HEAD (branch `claude/busy-goodall-mz0gvv`, 2026-09-02): 25 tuning CLI
modules + 4 shared helpers = **13,605 lines** (`wc -l` over the 29 files).
24 of them are `[project.scripts]` binaries exposing **48 subcommands**
(~60 distinct invocations). Tests for them: ~81k lines
(`ls tests/ | grep -iE 'cli|prescrib|round|measure|…' | xargs wc -l`).

---

## 1. The map

`(a)` in runbook tool menu / methodology · `(b)` engine entry · `(c)` lines ·
`(d)` re-implements engine work · `(e)` thin/fat.

| CLI | a | b (engine) | c | d | e |
|---|---|---|---|---|---|
| `jasper-active-speaker` (13 verbs) | **no** (testing-tooling only) | 12 `active_speaker.*` modules | 1797 | yes — `_reemit_staged_startup_anchor` 507–776 is 270 lines of stage-validate-publish | **fat** |
| `jasper-crossover-prescriber` (4) | yes | 4 prescription doors + `evidence_packet` | 1628 | yes — `_status_sections`/`_banked_section`/`_staged_section`/`_applied_section`/`_next_actions` = lines 754–1300, ~550 lines of "where this speaker stands" | **fat** |
| `jasper-measure` | yes | `TuningSession`, `measurement_door` | 1321 | yes — `_bind_compose`, `_CaptureAnnotatedStore`, session wiring | **fat** |
| `jasper-active-speaker-attempts-replay` | **no** | `attempts_loop` | 1079 | yes — `render_readme`, `_run_prose`, `_attempt_table`, `_why_that_reason` (~300 lines of report prose) | **fat, one-off study** |
| `jasper-null` | yes | *bypasses* `TuningSession`; uses `program_playback` + `WiredRecorder` directly | 1023 | **yes — second measure-once implementation** | **fat** |
| `jasper-angle-capture` (3) | yes | `angle_capture`, `angle_capture_spool` | 766 | partly (`_REGIME_STOPS` is engine data) | medium |
| `jasper-seat-level` | yes | `seat_level_ramp`, `seat_level_reference` | 738 | no | medium |
| `jasper-round-views` (9) | yes | `crossover_v2.round_views`, `frequency_view`, `repeat_floor` | 681 | no | **thin** |
| `jasper-basic-profile` (2) | yes | `WizardClient`, `baseline_profile` | 465 | no | thin |
| `jasper-read-distortion` | yes | `harmonic_evidence` | 353 | `round_bands_hz` is band arithmetic | thin |
| `jasper-forward-model` (2) | yes | `crossover_v2.forward_model` | 327 | no | **thin** |
| `jasper-round` (3) | yes | `wizard_client` | 324 | no | **thin** |
| `jasper-classify-features` | yes | `feature_classifier` | 300 | no | **thin** |
| `jasper-audition` (3) | yes | `active_speaker.audition` | 291 | no | **thin** |
| `jasper-delay-sweep` | yes | `delay_landscape`, `delay_sweep.sweep_spec` | 286 | no | **thin** |
| `jasper-active-speaker-emit-bench` | **no** | `active_speaker.bench.loop` | 285 | no | thin |
| `jasper-arm-walk` | yes | `active_speaker.arm_walk` | 283 | no | **thin** |
| `jasper-close-reference` (2) | yes | `crossover_v2.close_reference` | 280 | no | **thin** |
| `jasper-correction-bundle` (4) | **no** | `correction.bundle_tools` | 273 | no | thin |
| `jasper-gate-sweep` | yes | `crossover_v2.gate_sweep` | 223 | no | **thin** |
| `jasper-bass-extension-bench` | **no** (historical docs only) | `bass_extension.bench.*` | 194 | no | thin |
| `jasper-project-ring` | **menu: no**; methodology + exit-code table: yes | `ring_projection` | 167 | no | **thin** |
| `jasper-declare-geometry` (2) | yes | `measurement_geometry` | 165 | no | **thin** |
| `jasper-round-bank` | yes | `active_speaker.round_bank` | 131 | no | **thin** |
| `python -m jasper.cli.measurement_mic` | n/a (deploy bridge) | `mic_identity` | 45 | no | thin (62% prose) |

**Orphans — in `[project.scripts]`, absent from the generated tool menu
(`scripts/generate-tuning-tool-menu.py:54-73` is a hand-maintained roster):**
`jasper-active-speaker`, `jasper-active-speaker-attempts-replay`,
`jasper-active-speaker-emit-bench`, `jasper-bass-extension-bench`,
`jasper-correction-bundle`, `jasper-project-ring` — **3,995 lines the LLM's
menu never shows**. Five of the six lack `AUTHORITY_TIER`; four also lack
`build_parser()`, so they *cannot* be added without work
(`grep -c 'def build_parser' jasper/cli/*.py`). `jasper-project-ring` is the
odd one out: methodology §6 and the runbook's exit-code table both name it,
the menu does not. That is a bug in the roster, not a decision.

**Overlap pairs asked about.**

- `round` / `round_views` / `round_bank`: **name collision, not duplication.**
  `round` is an HTTP wizard client (open/wait/apply), `round_views` is 9 pure
  read views, `round_bank` copies evidence. Three concerns sharing a prefix;
  an LLM reading the menu cannot tell that `jasper-round-views` is not a verb
  of `jasper-round`.
- `measure` / `angle_capture` / `arm_walk` / `close_reference`: **not
  duplicates.** `angle_capture` declares a walk, `arm_walk` serves it,
  `measure` takes captures, `close_reference` is pure compute. The real
  duplicate is **`measure` vs `null_door`** — see §4.
- `active_speaker` / `basic_profile` / `audition`: **three doors onto the
  played graph, three transports.** `basic_profile` and `audition` are thin
  and correct; `active_speaker commission-ramp *` is the off-menu 1797-line
  third one.
- `crossover_prescriber` / `forward_model` / `classify_features`: **not
  duplicates.** `forward_model` and `classify_features` are thin views;
  `crossover_prescriber` is the door + a 550-line status/next-actions engine
  that does not belong in a CLI.

---

## 2. Does the surface reflect "one verb per question"?

Partly, and the gap is at the top level, not at the verb level.

- ADR-0204 (accepted 2026-08-31) + master-plan R14 **ratify** verb-per-question
  and per-tool `--help`. This is a decision, not drift, and the proposal below
  keeps it.
- What is *not* ratified is **24 top-level binaries**. `jasper-round-views`
  already proves the right shape: 9 questions, one binary, one exit vocabulary,
  one `--out` convention. The other 7 pure-advisory read tools
  (`gate-sweep`, `close-reference`, `classify-features`, `read-distortion`,
  `project-ring`, `forward-model`, `delay-sweep`) are *the same shape*: read a
  banked round dir → compute → JSON to `--out`, play nothing, open no device.
  Evidence the boundary is already wrong: **`round_views.py:123` and
  `classify_features.py:78` both `from jasper.cli.gate_sweep import
  add_rungs_ms_argument`** — CLI-to-CLI imports of a shared argument.

**Proposed target surface (80/20, no framework, no new machinery):**

| Binary | Contents | From |
|---|---|---|
| `jasper-read <question>` (rename of `jasper-round-views`) | 9 existing views **+** `gate-sweep`, `close-reference distance\|compare`, `classify`, `distortion`, `project-ring`, `forward-model predict\|verify-delta`, `delay-sweep propose` | 8 binaries → 1 |
| `jasper-measure` | today + `null` as `jasper-measure null` (they are one session, §4) | 2 → 1 |
| `jasper-seat-level`, `jasper-angle-capture`, `jasper-arm-walk` | unchanged (rig/pose lane) | 3 |
| `jasper-crossover-prescriber` | unchanged verbs, status logic moved to engine | 1 |
| `jasper-round`, `jasper-round-bank`, `jasper-basic-profile`, `jasper-audition`, `jasper-declare-geometry` | unchanged | 5 |
| bench/off-menu | keep as-is but **stop shipping as `[project.scripts]`** — run via `python -m` | −5 binaries |

Result: **24 → 10 tuning binaries**, 48 → ~50 subcommands (verb count
preserved, per ADR-0204/R14), one exit vocabulary for the whole advisory tier,
one `--out`/`--json` convention. The generator's roster becomes ~10 rows.
This is a re-parenting of existing `_cmd_*` functions, not a rewrite.

---

## 3. Shared helpers — adoption is under half

`grep -rln '_refusal import' --include=*.py .` etc.

| Helper | Lines | Importers | Non-adopters in scope |
|---|---|---|---|
| `_refusal.py` | 77 | 9 | 15 of 24 |
| `_report.py` | 37 | 4 | `read_distortion.py:48` and `close_reference.py:56` import `atomic_write_text` directly instead |
| `_unit_pair.py` | 47 | 2 (`close_reference`, `declare_geometry`) | — |
| `_logging.py` | 19 | 15 (10 in tuning scope) | — |

**Re-rolled per module:** 8 local refusal helpers
(`angle_capture._refuse`, `audition._refused`, `basic_profile._refuse_stale`,
`close_reference._failed`, `delay_sweep._refused`, `forward_model._refused`,
`measure._refused`, `seat_level._refused`) plus `round_views._write`.
`delay_sweep.py:81` and `forward_model.py:112` are **byte-identical 2-line
wrappers** of `_refusal.refused` — the "never add a third implementation"
rule, broken twice for two lines each.

**argparse boilerplate:** every one of the 24 rolls its own `build_parser()` +
`main()`. `build_parser` alone is 39–163 lines per tool (`arm_walk` 163 of 283
lines = 58% of the file). `--out` appears in 10 tools, `--output` in 2 (two
names, one concept). `--json` in 13. **15 tools hand-write an `EXIT CODES`
block in their epilog**, restating numbers that `_refusal.py` owns — a drift
surface against the counted-in-one-place pattern (ADR-0181) the same tools
cite. `json.dumps(..., indent=2, sort_keys=True)` appears 77 times across
`jasper/cli/` despite `_report.render_report` existing.

---

## 4. Boundary violations (highest-value findings)

### 4a. CLI imports web — three sites
```
jasper/cli/measure.py:804      from jasper.web.correction_crossover_v2_wired import WiredStimulusCapture
jasper/cli/null_door.py:180    from jasper.web.correction_crossover_backend import status_payload
jasper/cli/null_door.py:181    from jasper.web.correction_crossover_v2 import resolve_conductor_context
```
The target architecture is one engine + **two thin front ends**. Here the LLM
front end imports the web front end for engine seams. `resolve_conductor_context`
and `WiredStimulusCapture` belong in `active_speaker/crossover_v2/`.
Move: **low risk** (pure relocation), ~0 net lines, proven by
`tests/test_cli_measure.py` + `tests/test_null_confirm_door.py`.

### 4b. Two implementations of "measure once"
`measure.py` opens `TuningSession` + `measurement_door` + `WiredStimulusCapture`
(lines 785–905). `null_door.py` bypasses `TuningSession` entirely and rebuilds
the stack from `bind_program_playback_seams` + `play_program` + `WiredRecorder`
+ `encode_wav_s32` (lines 425–490, plus `_compose`, `_publish_program`,
`_resolve_mic`, `_run`). Same job, two stacks, both in `jasper/cli/`. This is
the single largest structural smell in my area.
Move: make `jasper-null` a `MeasureSpec` variant executed through
`TuningSession`, folded in as `jasper-measure null`. **−400 to −600 lines**,
risk **medium-high** (touches the excitation/volume clamp path — non-negotiable
tier, needs `/adversarial-review`), proven by `tests/test_null_confirm_door.py`
+ `tests/test_cli_measure.py` + one on-hardware round.

### 4c. Truth layer and scripts reach into CLI privates
- `scripts/run-crossover-round.py:199` — `from jasper.cli.angle_capture import
  _REGIME_STOPS` (private name, from a laptop script).
- `crossover_v2/position_cycle.py:33,174`, `branch_peak.py:136,193`,
  `arm_walk.py:193` cite `jasper.cli.*` privates in docstrings.
`_REGIME_STOPS` is engine data; it belongs in `measurement_programs.py`.
**−0 lines, low risk.**

### 4d. Recommendation logic in a CLI
`crossover_prescriber.py:754–1300` (`_banked_section`, `_staged_section`,
`_applied_section`, `_status_sections`, `_next_actions`, `status_document`) is
"analyze + recommend" — two of the engine's four verbs — living in argparse's
file. Move to `crossover_v2/status.py`; the CLI keeps `_print_status`.
**~−550 lines out of `jasper/cli/`**, risk medium,
`tests/test_crossover_v2_prescriber_status.py` (1053 lines) is the pin.

---

## 5. Exit codes — five vocabularies, exit `2` means five things

`grep -rn '^EXIT_[A-Z_]* *= *[0-9]' jasper/cli/*.py jasper/active_speaker/*.py scripts/run-crossover-round.py`

| Tool | `1` | `2` | `3` |
|---|---|---|---|
| `_refusal.py` (the stated shared rule) | REFUSED | UNREADABLE | WRITE_FAILED |
| `measure`, `null_door`, `delay_sweep`, `forward_model` | REFUSED | **INPUT** | — |
| `round`, `basic_profile` | **TRANSPORT** | **REFUSED** | TIMEOUT |
| `angle_capture`, `round_bank` | — | **REFUSED** | STAGE/BANK_FAILED |
| `crossover_prescriber` | **EVIDENCE_UNREADABLE** | **REFUSED** | STAGE_FAILED |
| `declare_geometry` | — | **NOT_FOUND** | — |
| `arm_walk` | — | — | 3–15 own names |
| `scripts/run-crossover-round.py` | — | — | 3–12, 78 |

Two extra smells:
1. **`delay_sweep.py:58-60` and `forward_model.py:65-67` re-declare
   `EXIT_OK/EXIT_REFUSED/EXIT_INPUT` with the shared rule's *numbers* under
   *different names*, while importing `refused` from `_refusal`.** Pure
   duplication: import the constants, delete the local `_refused`. **−12 lines,
   zero risk.**
2. **The runbook's exit-code table is stale.** It lists 11 rows; **10 tuning
   CLIs with their own `EXIT_*` constants have no row at all**
   (`measure`, `null`, `delay-sweep`, `forward-model`, `seat-level`,
   `audition`, `angle-capture`, `round`, `round-bank`, `basic-profile`).
   The table is hand-written next to a *generated* tool menu — it should be
   generated from the same roster.

---

## 6. `experiments/usb-turntable/` — production, and the path is the least of it

2,219 Python lines: `jts_turntable.py` (821, JTS-owned adapter) + `vendor/usb_turntable/`
(1,398, Apache-2.0 vendored upstream) + 296-line README. Tests: 1,799 lines
(`tests/test_usb_turntable_experiment.py`) + 1,370 (`tests/test_arm_walk.py`).

Production wiring (`grep -rn 'jts_turntable\|usb-turntable'`):
- `deploy/systemd/jasper-turntable-autostop@.service:13` —
  `ExecStart=/usr/bin/python3 /opt/jasper/experiments/usb-turntable/jts_turntable.py …`
- `deploy/udev/99-jasper-turntable-autostop.rules`
- `deploy/lib/install/python-runtime.sh:245` stages the tree onto the Pi
- `jasper/active_speaker/arm_walk.py:141` — `DEFAULT_TOOL_PATH = Path("/opt/jasper/experiments/usb-turntable/jts_turntable.py")`, driven as a subprocess
- `pyproject.toml:339` mypy `files`, `:313` ruff exclude; `scripts/ci-classify.py:99`

**It should not move under `jasper/`.** The hotplug unit runs it under
`/usr/bin/python3`, not the venv — it must stay import-free of `jasper` (and of
numpy). `arm_walk.py` deliberately drives it as a *subprocess* for the same
reason. Absorbing it into the package would break the autostop path.

**What it should do is stop being called an experiment.** Rename the directory
to `movers/usb-turntable/` (or `tools/turntable/`) and delete the apologetic
README preamble. Cost: 5 path references + 3 test path constants + the
`AGENTS.md`/`README.md` lines that exist only to explain the anomaly.
**~−15 lines of apology, ~10 path edits, risk low-medium** (a wrong path
silently disables the autostop unit — verify with the unit file test in
`tests/test_usb_turntable_experiment.py:1756`). Honest uncertainty: this is
pure legibility with zero behavior win, which is exactly why the owner deferred
it on 2026-08-25. Low priority.

---

## 7. Prose over the AGENTS.md bar

`tokenize`-based count over the 29 files: **3,172 of 13,605 lines are
docstrings/comments (23%)**. Worst: `crossover_prescriber` 35%, `seat_level`
31%, `null_door` 29%, `measure` 28%, `measurement_mic` 62% (28 prose lines
around 10 lines of code). Note ~2,000 of those lines are `--help` text, which
**ADR-0204 decision 1 deliberately puts in the tool** — that is not slop. The
slop is history and narration inside `"""docstrings"""`. 13 hard-coded dates,
30 `#NNNN` citations in tuning CLIs.

Three examples:

1. `jasper/cli/crossover_prescriber.py:560-565` — history, banned outright:
   > "Until 2026-08-23 the unvouched filters were refused instead, which meant
   > a role could never keep an incumbent shelf: the fit engine placed it and no
   > verdict vouches for it, so naming the role deleted it (#2863)."

2. `jasper/cli/crossover_prescriber.py:605-608` — narration + reviewer-address:
   > "Extracted rather than copied when the second class arrived, and the exact
   > wording of both lines is preserved: ``stage``'s "for round N" sits where it
   > always did…"

3. `jasper/cli/forward_model.py:86-104` — a 19-line campaign lab notebook in
   `--help` (r7/r8/r9/r10/r11/r12 round numbers, "grade 1.112 -> 0.9035",
   "See historical/flat-campaign-2026-08-31.md section 5"). This ships to the
   operator's terminal on every `--help`.

Estimated recoverable: **~600–900 lines** across the 24 files without touching
a single behavioral contract. Risk **low** — but AGENTS.md's own note applies:
do not bulk-delete by script; one file per PR.

---

## 8. Dead code / dead flags

**No confirmed dead flags.** I scanned all 197 argparse options across the 24
tuning CLIs against `docs/`, `scripts/`, `deploy/` and `tests/`; every flag has
at least one caller. But **~35 flags have a test as their *only* caller** —
no doc, no script, no runbook line names them. Examples with exactly one hit
anywhere outside the CLI: `active_speaker --coupling`, `--baseline-id`,
`--applied-baseline-state`, `--no-applied-baseline`;
`attempts_replay --hard-cap-attempts`, `--target-attempts`;
`classify_features --gates-ms`; `read_distortion --full-range-band`;
`round_bank --campaign-root`; `round --timeout-s`;
`forward_model --measured-round`, `--polarity-sign`, `--residual-delay-us`;
`measure --level-dbfs`, `--prompt`. These are candidates for a
"does the LLM ever need this?" pass, not for blind deletion.

**Dead-ish code:** `jasper-active-speaker-attempts-replay` (1079 lines) replays
two named 2026-07/08 capture banks (`captures/repeat-floor-20260731`,
`captures/r11-loop-proof-corpus`) that are `.gitignore`d and not in the tree.
It is a finished study, and ~300 of its lines are a README generator. Strong
candidate for **delete-and-cite-the-result-in-an-ADR** (~−1079 lines), risk low
if the owner confirms the study is closed; medium if the repeat-floor control
is meant to be re-runnable. I could not settle this from the tree — flag for
the owner.

---

## 9. Stale docs in my area

| Doc | What at HEAD contradicts it |
|---|---|
| `docs/tuning-operator-runbook.md` "Exit codes" table | 10 tuning CLIs with their own `EXIT_*` constants have no row (§5). It also says "the read-only measurement tools share ONE" while `measure`/`null_door`/`delay_sweep`/`forward_model` re-declare it under different names. |
| Same, "Other surfaces" | Lists `jasper-doctor` and `GET :8780/state` but not the 6 off-menu binaries the LLM can still find with `ls /opt/jasper/.venv/bin/`. |
| `scripts/generate-tuning-tool-menu.py:54-73` | Roster omits `jasper-project-ring`, which methodology §6 and the runbook's own exit-code table both name. |
| `docs/testing-tooling.md:193-215, 620-720` | Carries per-tool contracts for 5 orphan CLIs — exactly the "folder of per-tool documents" ADR-0204 decision 1 said not to create; it just lives in one file instead of many. |

---

## 10. Top moves, ranked

| # | Move | Δ lines | Risk | Proof |
|---|---|---|---|---|
| 1 | Delete `delay_sweep._refused` / `forward_model._refused`; import `EXIT_*` from `_refusal` instead of re-declaring | −12 | none | `tests/test_crossover_v2_forward_model.py`, `test_delay_sweep*.py` |
| 2 | Move `WiredStimulusCapture` / `resolve_conductor_context` / `status_payload` out of `jasper/web/` into `crossover_v2/`; delete the 3 CLI→web imports | ~0 | low | `test_cli_measure.py`, `test_null_confirm_door.py` |
| 3 | Move `_REGIME_STOPS` into `measurement_programs.py`; fix `run-crossover-round.py:199` | ~0 | low | `test_run_crossover_round.py` |
| 4 | Prose pass, one file per PR, worst-first (`crossover_prescriber`, `seat_level`, `null_door`, `measure`) — history, dates, campaign notebooks out of `--help` | −600…−900 | low | `test_tuning_tool_menu_generator.py` + full lane |
| 5 | Fold the 7 advisory read binaries into `jasper-round-views` (rename `jasper-read`); one exit vocabulary, one `--out` | −250…−400, **24 → 17 binaries** | low-med | each tool's existing test file, re-pointed; regenerate the menu |
| 6 | Lift `crossover_prescriber`'s status/next-actions engine (754–1300) into `crossover_v2/status.py` | −550 out of `jasper/cli/` | med | `test_crossover_v2_prescriber_status.py` (1053 lines) |
| 7 | Fold `jasper-null` into `jasper-measure` as a `MeasureSpec` variant through `TuningSession` — kill the second measure stack | −400…−600, **−1 binary** | **med-high** (clamp path; `/adversarial-review` tier) | `test_null_confirm_door.py` + one on-hardware round |
| 8 | Un-ship the 5 bench/study binaries from `[project.scripts]` (keep as `python -m`); add `jasper-project-ring` to the generator roster | −5 entry points | low | `test_build_and_ci_contracts.py` |
| 9 | Delete `active_speaker_attempts_replay.py` if the study is closed; record the result in an ADR | −1079 (+664 test) | low *if owner confirms* | owner call |
| 10 | Generate the runbook's exit-code table from the same roster as the tool menu | −40 hand-written rows | low | extend `test_tuning_tool_menu_generator.py` |
| 11 | Rename `experiments/usb-turntable/` → `movers/usb-turntable/`; delete the apology preamble | −15 | low-med | `test_usb_turntable_experiment.py:1756` unit-path pins |

Ordering note: 1–3 are same-day, reviewable, and unblock 5. 7 is the biggest
single win and the only one in the non-negotiable review tier. 11 is pure
legibility and the owner already deferred it once — last.

**Uncertainty I could not resolve from the tree:** whether the attempts-replay
study is closed (move 9); whether `jasper-bass-extension-bench` is still on a
live program (its only docs are `docs/historical/`); and whether the owner
wants the 5 bench binaries discoverable at all (move 8).
