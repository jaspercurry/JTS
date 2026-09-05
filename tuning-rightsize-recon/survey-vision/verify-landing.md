# verify-landing — LANDING.md §1 against HEAD f4ff89731

LANDING was written at main `5d32f683d`. HEAD is `f4ff89731` (86 commits later).
Every `file:line` below was opened at HEAD.

**Scope of drift:** only six of the ten binaries' files changed at all in the range:

```
jasper/cli/_refusal.py                      | 20 ++-   (da6ad6082)
jasper/cli/crossover_prescriber.py          | 12 +--   (da6ad6082)
jasper/cli/round.py                         | 15 +--   (da6ad6082)
jasper/cli/round_views/_common.py           | 11 +-    (6a5603b70)
jasper/cli/round_views/classify_features.py | 11 +-    (6a5603b70)
jasper/cli/round_views/delay.py             | 33 +--    (6a5603b70)
tests/test_correction_boundary_ssot.py      |  2 +-    (59e53e3cf)
```

`tests/test_cli_exit_vocabulary.py`, `pyproject.toml`, `scripts/test-fast`,
`scripts/generate-tuning-tool-menu.py`, and the other seven CLI modules are
**byte-identical** to `5d32f683d`.

---

## 1. Verdicts — "Ten binaries" table

| # | LANDING row | Verdict | Evidence at HEAD |
|---|---|---|---|
| 1 | `jasper-declare-geometry set\|show` TRUE — `pyproject.toml:59` | **TRUE (claim), citation FALSE** | Entry point is `pyproject.toml:228`. `pyproject.toml:59` is an `audioop-lts` dependency comment — at HEAD *and* at 5d32f683d (file unchanged). Subcommands `('set','show')` verified by importing `build_parser()`. |
| 2 | `jasper-seat-level` TRUE — only `EXIT_OK`/`EXIT_REFUSED`; no unreadable path, and it has none — `seat_level.py:735` | **PARTIAL** | `seat_level.py:735` returns exactly `EXIT_OK`/`EXIT_REFUSED` ✓. But `seat_level.py:713` calls `build_parser().error("pass --calibration-file or --mic-serial")` → argparse `SystemExit(2)`, stderr usage block, no document. It *does* have an exit-2 path. |
| 3 | `jasper-angle-capture plan\|stage\|withdraw\|serve` TRUE for roster; refusal shape is the `{"ok": false}` one — `angle_capture.py:481` | **PARTIAL** | Roster ✓ (`('plan','stage','withdraw','serve')`). `{"ok": false}` at `angle_capture.py:481` and `:493` ✓ — but only for plan/stage/withdraw, and only under `--json`. **`serve` uses the shared `failed()`** → `{status,reason,detail}`, ungated: `angle_capture.py:595`, `:617`. One binary, two shapes. |
| 4 | `jasper-measure` TRUE — `pyproject.toml:57` | **TRUE (claim), citation FALSE** | Entry point is `pyproject.toml:205`. `:57` is an OpenAI-SDK dependency comment at both SHAs. |
| 5 | `jasper-null` TRUE as a binary; same admission gate as measure — `program_playback.py:86` | **TRUE** | `pyproject.toml:206`. `program_playback.py:86 play_program(..., readmit: Readmit, ...)`; null calls it at `null_door.py:417`, measure reaches it via `crossover_v2/program_transaction.py:200`; the readmit seam is `crossover_v2/composition.py:177` → `program_admission.py:615 readmit_program_from_wav`. |
| 6 | `jasper-round open\|wait\|bank\|apply` PARTIAL — own `_emit` receipt, `--json`-gated; `blocked` → two exit codes; transport slugs `answer_lost`/`wait_timeout` — `round.py:92` | **TRUE (as PARTIAL), line moved** | `_emit` now `round.py:94` (+2 from the widened `_refusal` import). Machine-readable receipt only under `--json` (`round.py:95-96`); text mode prints key/value lines. Slugs at `wizard_client.py:75-76`. Subcommand order is `open, wait, apply, bank` (LANDING wrote `open\|wait\|bank\|apply`). |
| 7 | `blocked` maps to two exit codes — `round.py:191-192` | **TRUE, line moved to `round.py:167` + `:185`** | See §5. |
| 8 | `jasper-crossover-prescriber status\|packet\|propose\|stage` PARTIAL — `propose` writes nothing without `--out`; several bare `error:` paths — `:381` | **TRUE (as PARTIAL), lines moved −4** | Roster ✓. `propose` writes only `if args.out:` — `crossover_prescriber.py:376-377`. |
| 9 | bare `error:` stderr paths — `crossover_prescriber.py:361,366,545,554,574` | **PARTIAL — undercounts** | At HEAD there are **eight**: `:228`, `:235`, `:357`, `:362`, `:540`, `:543`, `:569`, `:580`. LANDING named five (its `packet` pair `:228/:235` and `stage`'s write-fail `:580` were missed). |
| 10 | `jasper-round-views <view>` TRUE — 19 subcommands = 17 artifact rows + `repeat-floor` + `inventory`; `distortion` requires `--dumps` — `_common.py:79-104` | **TRUE** | 19 subcommands confirmed by importing `build_parser()`. `ARTIFACT_BY_VIEW` = 17 rows, `_common.py:79-103`. `INVENTORY_ARTIFACT` `_common.py:106`, consumed `inventory.py:69`. `repeat-floor` absent by design, `_common.py:77-78`, enforced `repeat.py:85-86`. `distortion --dumps required=True` `distortion.py:78`. |
| 11 | `distortion --dumps` vs `classify-features`' default ring — PARTIAL, same default is valid | **TRUE (as PARTIAL)** | `distortion.py:78` `required=True`; `classify_features.py:175` `default=None` + auto-projection into `<bundle-dir>/<_PROJECTED_RING>`. Both read the same sidecars: `harmonic_evidence.py:584` and `feature_classifier.py:86` both consume `evidence_packet.py:204 RING_SIDECAR_GLOB`. |
| 12 | `jasper-basic-profile review\|apply` TRUE; refusal shape `{"status":"blocked","refused_by":"client"}` — `:249-251` | **PARTIAL** | Roster ✓; shape ✓ at `basic_profile.py:249-251`, `--json`-gated at `:262`. **But it is not the only refusal shape:** `basic_profile.py:301` `_dump(applied)` prints the *door's own* payload verbatim, ungated, exit 1 (`:302`); and `basic_profile.py:448-456` (`_DoorUnreachable`) exits **2 with no stdout document at all**. |
| 13 | `jasper-audition start\|stop\|status` TRUE; refusal document `--json`-gated — `:179-196` | **TRUE (line range `:179-198`)** | `_refused` at `audition.py:179`, gate at `:188`, dict `:191`. A third exit-1 path exists: `audition.py:133` returns `EXIT_REFUSED` while printing the ordinary `{status:"auditioning", ...}` state document, not a refusal document. |
| 14 | `jasper-arm-walk` gone — TRUE, pinned `tests/test_arm_walk.py:1389` | **TRUE** | Absent from `[project.scripts]`; `tests/test_arm_walk.py:1389 assert "jasper-arm-walk" not in text`. Only surviving mentions are `docs/adr/0188-*.md:18,67,80`. |
| 15-21 | `jasper-round-bank`, `jasper-delay-sweep`, `jasper-gate-sweep`, `jasper-classify-features`, `jasper-read-distortion`, `jasper-close-reference`, `jasper-forward-model` gone | **TRUE** | Repo-wide grep over `*.py *.toml *.md *.sh *.service`: zero hits for all seven. |
| 22 | `jasper-project-ring` gone — only a historical docstring | **TRUE** | One hit: `tests/test_crossover_v2_ring_projection.py:374` (docstring). |
| 23 | `jasper-active-speaker` off the menu — `generate-tuning-tool-menu.py:54-65` | **TRUE** | `TUNING_TOOL_MODULES` at `scripts/generate-tuning-tool-menu.py:54-65` names exactly the ten; `jasper-active-speaker` is in `pyproject.toml:196` but not in the roster. |
| 24 | `scripts/run-crossover-round.py` a transport, not a 2nd impl — `round.py:316` description | **TRUE, line moved to `round.py:306-312`** | The claim is in the parser `description`; `round.py:316` at HEAD is an epilog example line (−4 shift from `da6ad6082`). |
| 25 | menu `--check` → exit 0 | **TRUE** | Ran `scripts/generate-tuning-tool-menu.py --check` at HEAD: exit 0. |

## 2. Verdicts — "Conventions" table

| LANDING row | Verdict | Evidence at HEAD |
|---|---|---|
| One exit vocabulary — TRUE for codes, nothing outside {0,1,2,3}, one sanctioned exemption; sub-claim "reason=transport" FALSE, slugs are `answer_lost`/`wait_timeout` (`wizard_client.py:75`) | **TRUE**, with a caveat | Slugs at `wizard_client.py:75-76` ✓. Exemption = `jasper.cli.declare_geometry` only, `_refusal.py:29-31`, which declares `EXIT_NOT_FOUND = 2` at `declare_geometry.py:31`. `tests/test_cli_exit_vocabulary.py` green at HEAD (23 passed). Caveat: **argparse's own `SystemExit(2)`** is reachable in 5 modules with no document — `angle_capture.py:251,254,257,281`, `seat_level.py:713`, `close_reference.py:138,153,156`, `repeat.py:86`. No other integer literal is returned as an exit code anywhere in the ten. |
| One refusal shape — FALSE, five shapes coexist; four tools gate the document behind `--json` (`blend_prescription.py:315-320`) | **FALSE-verdict TRUE; the counts UNDERCOUNT** | `{"accepted": false, ...}` at `blend_prescription.py:317-322` (`def to_dict` at `:315`) ✓. But **eleven** distinct stdout refusal documents exist at HEAD, not five (§3 table), and **seven** tools gate a refusal document behind `--json`, not four: `seat_level.py:729`, `measure.py:899`, `audition.py:188`, `angle_capture.py:479,492`, `basic_profile.py:262`, `crossover_prescriber.py:365,559`, `round.py:95`. |
| One artifact rule — PARTIAL; 17 views + packet; `propose` stdout-only without `--out`; `repeat-floor` no default by design (`repeat.py:85-86`) | **TRUE (as PARTIAL)** | `repeat.py:85-86` unchanged; `crossover_prescriber.py:376-377`. |
| Answer small — PARTIAL; views put the summary on stderr and leave stdout empty unless `--out -`; `delay-landscape` prints the whole grid to stdout; only `delay-landscape` prints the next command (`delay.py:136`) | **TRUE (as PARTIAL), line moved to `delay.py:124`; three sub-claims need qualifying** | (a) The grid is still on stdout: `delay.py:124` prints `landscape.to_dict()`, whose `coordinates_us` / `predicted_null_depth_db` are full arrays (`delay_landscape.py:350-351`). (b) "stdout empty unless `--out -`" has **three** exceptions among views, not one: `delay.py:124`, `delay.py:162-171`, `close_reference.py:78`. Plus stdout is never empty on a refusal — `failed()` always prints. (c) "only delay-landscape prints the next command" is true **within round-views only**; outside it, `crossover_prescriber.py:1024-1026` prints a `next:` block on stdout and `basic_profile.py:228-232` prints the next command on stdout. |
| One `--help` style — PARTIAL; `prog`/`AUTHORITY_TIER` on all ten; four two-sentence descriptions; the round positional spelled six ways | **TRUE** | Verified by importing all ten and inspecting `build_parser()`: every one sets `prog` and exports `AUTHORITY_TIER`; exactly four descriptions are two sentences (`basic_profile` 192 ch, `seat_level` 350 ch, `round` 270 ch, `null_door` 92 ch); six positional spellings (§4). |
| "The roster test pins exit-code constants only, never a printed refusal document" | **PARTIAL** | It never invokes a CLI's `main`, correct. But it *does* pin a printed document — `_refusal.failed()`'s own — at `tests/test_cli_exit_vocabulary.py:100-103`. |

## 3. Verdicts — "Boundary rule" table

| LANDING row | Verdict | Evidence at HEAD |
|---|---|---|
| Engine never imports a surface — TRUE, pinned `:271,329` | **TRUE** | `tests/test_correction_boundary_ssot.py:270-274` (active_speaker row), `:329 test_crossover_v2_imports_no_web_front_end`. My AST scan: 0 edges from `jasper/active_speaker` and 0 from `crossover_v2`. |
| `audio_measurement` ↛ `active_speaker`, `correction` — TRUE, pinned `:266` | **TRUE** | `tests/test_correction_boundary_ssot.py:265-269`. 0 edges by scan. |
| `audio_measurement` ↛ `web` — TRUE, pinned `test_runtime_import_closure.py:28` | **TRUE** | `FORBIDDEN` at `tests/test_runtime_import_closure.py:28-34`; audio_measurement joins `TRUTH_LAYER_MODULES` at `:162-166`. 0 edges by scan. |
| `audio_measurement` ↛ `cli` — holds (0 edges), **unpinned**; wave 7 row 7.3 takes it | **CHANGED-SINCE `59e53e3cf`** | It is now pinned: `("jasper","cli")` was added to the row at `tests/test_correction_boundary_ssot.py:267`. This is the only LANDING boundary verdict the range invalidates. |
| `active_speaker` ↛ `web`, `cli`, `correction` — TRUE, pinned `:271` | **TRUE** | `tests/test_correction_boundary_ssot.py:270-274`; no allowlist entry for `jasper/active_speaker` (`:159-187`). |
| `cli` → `web` — 5 edges, all allowlisted; allowlist cannot rot; `:276`, allowlist `:168-188`, rot pin `:317-325`; sites `doctor/correction.py:39,567,677,688`, `measure.py:663` | **TRUE (claim), site lines CHANGED-SINCE the doctor batch (`ad61a5f61`…`645f4f03c`)** | Row `:275-279`; allowlist `:168-186` (4 module names); rot pin `:316-326`. My AST scan at HEAD finds exactly 5 sites: `jasper/cli/doctor/correction.py:36, :671, :739, :750` and `jasper/cli/measure.py:663`. Only `measure.py:663` still matches LANDING's citation. |
| `correction` → `active_speaker` (only `runtime_safety.py`) — TRUE, allowlisted | **TRUE** | Row `:280-284`; allowlist `:160-167`; the single edge is `jasper/correction/runtime_safety.py:16`. |
| Zero unallowlisted violations — TRUE, 6 edges, all six allowlisted | **TRUE** | Independent AST scan (module-level + function-local, `ast.walk`) reproduces exactly 6 forbidden edges, all allowlisted. |
| Fast-lane coverage — gap; neither boundary test in `scripts/test-fast`'s always-on list | **TRUE** | `scripts/test-fast:425-429` — always-on set is `test_dependency_groups`, `test_lint_contracts`, `test_deploy_wiring_guards`, `test_shell_awk_environ_convention`, `test_docs_impact`. Neither boundary test is there; file unchanged in the range. |
| TARGET's "new row" / "extend to all of active_speaker" future tense — stale, all three exist | **TRUE** | `tests/test_correction_boundary_ssot.py:265,270,275,280`. |

---

## Re-derived at HEAD (independent of LANDING's wording)

### 1. Refusal SHAPE census

**`jasper/cli/_refusal.py` in full.** `EXIT_OK=0` `:33`, `EXIT_REFUSED=1` `:35`, `EXIT_UNREADABLE=2` `:37`, `EXIT_WRITE_FAILED=3` `:39`.

```python
STATUS_BY_CODE = {                      # _refusal.py:43-47
    EXIT_REFUSED:      "refused",
    EXIT_UNREADABLE:   "unreadable",
    EXIT_WRITE_FAILED: "unwritable",
}
```

`refused(reason, detail, *, exit_code, status="refused")` — `_refusal.py:67-78` — prints
`json.dumps({"status","reason","detail"}, indent=2, sort_keys=True)` on **stdout**
(`:70-76`) *and* `f"{status} ({reason}): {detail}"` on **stderr** (`:77`), returns
`exit_code`. It is **never `--json`-gated** — the gating, where it exists, is the
caller's.

`failed(exit_code, reason, detail)` — `_refusal.py:81-86` — is `refused()` with
`status = STATUS_BY_CODE[exit_code]`, i.e. the word and the number can never disagree.

Also owned here: `OWN_EXIT_VOCABULARY = {"jasper.cli.declare_geometry"}` `:29-31`;
`read_source_bytes` `:50-53` and `read_json_source` `:56-64` (**new since 5d32f683d**,
`da6ad6082`); `StageFailed` `:89-94`; `stage()` `:97-109`.

**Census — every distinct stdout document on a refusal/failure path.**

| Binary | Path | Keys printed on stdout | `--json`-gated? | file:line | Exit |
|---|---|---|---|---|---|
| declare-geometry | `set` refused | *(none — stderr only)* | n/a | `declare_geometry.py:110` | 1 |
| declare-geometry | `set` write fail | *(none)* | n/a | `declare_geometry.py:119` | 3 |
| declare-geometry | `show` unreadable / absent | *(none)* | n/a | `declare_geometry.py:139`, `:142` | 2 (own `EXIT_NOT_FOUND`) |
| seat-level | any refusal | `status, reason, detail, reference_volume_db, measured_db_spl, restored, reachable_target_db_spl, ramp` | **yes** | `seat_level.py:729-730` (shape: `seat_level_ramp.py:281-291`) | 1 |
| seat-level | missing `--calibration-file`/`--mic-serial` | *(none — argparse usage)* | n/a | `seat_level.py:713` | 2 |
| angle-capture | `plan`/`stage`/`withdraw` refusal | `ok, reason, detail` | **yes** | `angle_capture.py:492-493` | 1 |
| angle-capture | `stage`/`withdraw` fs failure | `ok, reason, detail` (`reason="stage_failed"`) | **yes** | `angle_capture.py:479-483` | 3 |
| angle-capture | `serve` walk refused / non-OK loop code | `status, reason, detail` (shared `failed`) | **no** | `angle_capture.py:595`, `:617-625` | 1 |
| angle-capture | flag conflicts | *(none — argparse usage)* | n/a | `angle_capture.py:251,254,257,281` | 2 |
| measure | flag error / door refusal | `status, reason, detail` | **yes** | `measure.py:899-906` (via `:981`, `:991`) | 2 / 1 |
| measure | interrupted mid-batch | `status("partial"), reason, detail, session_id, bundle_dir, record_ids, specs, stopped_at` | **no** | `measure.py:929-950` | 1 |
| measure | restore failed | `<the whole run report> + status("restore_failed"), restore_error{reason,detail}` | **no** | `measure.py:968-971` | 1 |
| null | compose unavailable | `status("refused"), rows[]` | **no** (`jasper-null` has no `--json`) | `null_door.py:626-627` | 1 |
| null | walk finished, a row unmeasured | `status("banked"), rows[]` | **no** | `null_door.py:710-711` | 1 |
| null | interrupted | `status("partial"), reason, detail, banked_row_ids` | **no** | `null_door.py:884-889` | 1 |
| null | measurement door refused | `status, reason, detail` | **no** | `null_door.py:897-901` | 1 |
| null | wired capture failed | `status, reason, detail` | **no** | `null_door.py:907-911` | 1 |
| null | bad delay coordinate | *(none — stderr only)* | n/a | `null_door.py:917-918` | 2 |
| null | OSError | *(none — stderr only)* | n/a | `null_door.py:920-921` | 2 |
| round | `open` tier missing | `verb, status("blocked"), stage, reason` | **yes** (`_emit`, `round.py:95`) | `round.py:145-149` | 1 |
| round | `open` prescription unreadable | `verb, status("blocked"), stage, reason, detail` | **yes** | `round.py:154-158` | 1 |
| round | `open` refused / lost | `verb, status, stage, tier, path, http, session_id, phase, reason, detail` | **yes** | `round.py:164-182` | 1 **or** 2 |
| round | `wait` failed / lost / timed out | `verb, status, reason, phase, session_id, candidate_fingerprint, failure, waited_s` | **yes** | `round.py:192-204` | 1 / 2 / 2 |
| round | `apply` refused / lost | `verb, status, reason, refused_by, expected_candidate_fingerprint, candidate_fingerprint, http, outcome, detail` | **yes** | `round.py:211-229` | 1 / 2 |
| round | `bank` refused | `banked(false), reason, detail` | **yes** | `round.py:252` + `:276-277` | 1 |
| round | `bank` write failed | `banked(false), reason("write_failed"), detail` | **yes** | `round.py:256` + `:276-277` | 3 |
| crossover-prescriber | `packet` unreadable | *(none — bare `error:`)* | n/a | `crossover_prescriber.py:228` | 2 |
| crossover-prescriber | `packet` write failed | *(none)* | n/a | `crossover_prescriber.py:235` | 3 |
| crossover-prescriber | `propose` no evidence source | *(none)* | n/a | `crossover_prescriber.py:357` | 2 |
| crossover-prescriber | `propose` packet/OS error | *(none)* | n/a | `crossover_prescriber.py:362` | 2 |
| crossover-prescriber | `propose` gate refused | `accepted(false), reason, detail, evidence` | **yes** | `crossover_prescriber.py:365-366` (shape `blend_prescription.py:317-322`) | 1 |
| crossover-prescriber | `stage` no evidence source | *(none)* | n/a | `crossover_prescriber.py:540` | 2 |
| crossover-prescriber | `stage` missing `--state` | *(none)* | n/a | `crossover_prescriber.py:543-549` | 2 |
| crossover-prescriber | `stage` gate refused | `accepted(false), reason, detail, evidence` | **yes** | `crossover_prescriber.py:559-560` | 1 |
| crossover-prescriber | `stage` packet/state unreadable | *(none)* | n/a | `crossover_prescriber.py:569` | 2 |
| crossover-prescriber | `stage` write failed | *(none)* | n/a | `crossover_prescriber.py:580` | 3 |
| crossover-prescriber | `status` packet unbuildable | *(the ordinary status document)* | **yes** | `crossover_prescriber.py:1088-1089`, `:1092` | 2 |
| round-views | every stage failure | `status, reason, detail` (shared `failed`) | **no** | `round_views/__init__.py:165`, `:173`; `_common.py:176` | 1/2/3 |
| round-views | instrument refusing by name (distortion, classify-features, delay, close-reference) | `status, reason, detail` (detail = JSON blob or sentence) | **no** | `_common.py:169-176`; callers `__init__.py:169`, `classify_features.py:149`, `delay.py:110,133,141`, `close_reference.py:62-63` | 1 (2 for unreadable round) |
| round-views | `repeat-floor` with no destination; `close-reference` flag conflicts | *(none — argparse usage)* | n/a | `repeat.py:86`; `close_reference.py:138,153,156` | 2 |
| basic-profile | stale/mismatched fingerprint | `status("blocked"), refused_by, expected_candidate_fingerprint, candidate_fingerprint, issues[]` | **yes** | `basic_profile.py:249-263` | 1 |
| basic-profile | door refused the apply | *(the door's own payload, verbatim)* | **no** | `basic_profile.py:301` | 1 |
| basic-profile | door unreachable | *(none — stderr only)* | n/a | `basic_profile.py:448-456` | 2 |
| audition | `start`/`stop` refused | `status, reason, detail` | **yes** | `audition.py:188-195` | 1 |
| audition | `start` ended off the full layer | *(the state document: `status, ended, layer, …`)* | **yes** | `audition.py:124-125`, `:133` | 1 |

**Counts.** Eleven distinct stdout key-sets on failure paths; seven tools gate at least
one of them behind `--json`; **fifteen** distinct bare stderr-only paths carrying no
document at all (declare-geometry ×3, null ×2, prescriber ×8, basic-profile ×1, plus the
argparse-`error()` family ×8 across 5 modules).

**One wrong comment found:** `null_door.py:906` says "Every exit from this door speaks
JSON." Two exits do not — `null_door.py:917-918` and `:920-921` print a stderr sentence
and exit 2 with an empty stdout.

### 2. What `tests/test_cli_exit_vocabulary.py` pins

Unchanged since `5d32f683d`; 23 tests, green at HEAD.

| Pin | file:line | What it asserts |
|---|---|---|
| `test_no_tuning_cli_numbers_its_own_exits` | `:69-71` | AST: no module-scope `EXIT_*` assignment in any roster module (packages walked leaf-by-leaf, `:39-43`) |
| `test_every_tuning_cli_exit_name_is_the_shared_constant` | `:74-86` | Object **identity** (`is`) against `_refusal`'s constants for every `EXIT_*` a module exposes |
| `test_the_exempt_modules_are_real_and_in_the_menu` | `:89-93` | `OWN_EXIT_VOCABULARY` names only live roster modules |
| `test_the_record_status_and_the_exit_code_always_agree` | `:96-103` | **The one printed-document pin** — but it calls `_refusal.failed()` *directly*, asserting `'"status": "<word>"' in stdout` and `stderr.startswith(f"{status} (a_slug): ")`. No CLI `main` is invoked anywhere in the file. |
| `test_the_failing_codes_are_exactly_one_two_three` | `:106-110` | `EXIT_OK == 0` and `sorted(STATUS_BY_CODE) == [1,2,3]` |

So: **constants + the shared helper's own two streams.** Nothing pins that any of the ten
binaries actually calls that helper, nor what any of them prints. Every row of the §1
census above is therefore unpinned behaviour.

### 3. Answer-small census

| Tool / subcommand | Human summary → | stdout carries arrays/curves? | prints the artifact path? | prints the next command? | file:line |
|---|---|---|---|---|---|
| round-views `entry` | stderr | no (stdout empty unless `--out -`) | yes, in the stderr line | no | `grades.py:69-77` |
| round-views `frozen` | stderr | no | yes (stderr) | no | `grades.py:86-90` |
| round-views `per-seat` | stderr | no | yes (stderr) | no | `grades.py:119-124` |
| round-views `repeat` | stderr | no | yes (stderr) | no | `repeat.py:58-63` |
| round-views `repeat-floor` | stderr | no | yes (stderr, all destinations) | no | `repeat.py:103-108` |
| round-views `agreement` | stderr | no | yes (stderr) | no | `seats.py:77-82` |
| round-views `co-metrics` | stderr | no | yes (stderr) | no | `seats.py:100-105` |
| round-views `directivity` | stderr | no | yes (stderr) | no | `seats.py:133-137` |
| round-views `cloud-binding` | stderr | no | yes (stderr) | no | `cloud_binding.py:42-46` |
| round-views `forward-model` | stderr | no | yes (stderr) | no | `forward_model.py:88-94` |
| round-views `spec-sweep` | stderr | no | yes (stderr) | no | `sweeps.py:76-81` |
| round-views `gate-sweep` | stderr | no | yes (stderr) | no | `sweeps.py:99-104` |
| round-views `frequency` | stderr | no | yes (stderr) | no | `frequency.py:85-89` |
| round-views `distortion` | stderr | no | yes (stderr) | no | `distortion.py:47-53` |
| round-views `classify-features` | stderr (3 lines) | no | yes (stderr) | no | `classify_features.py:105-108`, `:114`, `:161` |
| round-views `close-reference` (compare) | stderr | no | yes (stderr) | no | `close_reference.py:118-119`, `:126` |
| round-views `close-reference --distance` | stderr | **stdout carries `{"status":"recommended","distance":…}`** | n/a (writes nothing) | no | `close_reference.py:78-91` |
| round-views `delay-landscape` | stderr (`optimum_line`) | **YES — the full grid** (`coordinates_us`, `predicted_null_depth_db`, `delay_landscape.py:350-351`) | yes, `"out"` key in the stdout doc | **yes** — `confirm_with` in the stdout doc | `delay.py:112-125` |
| round-views `delay-confirm` | stderr (`verdict_line`) | no — bounded 8-key document | yes, `"out"` key | no | `delay.py:162-172` |
| round-views `inventory` | stderr | no | yes (stderr) | *effectively* — `produced_by` names the producing command per missing artifact | `inventory.py:56-62`, `:72-78` |
| prescriber `status` | **stdout** (deliberate, `:1066-1068`) | no | reading-order doc paths, on stdout | **yes** — a `next:` block on stdout | `crossover_prescriber.py:1012-1026`, `:1091` |
| prescriber `packet` | **stdout** | no | **yes, on stdout** (`-> {artifact}`) | no | `crossover_prescriber.py:271-295`, `:241` |
| prescriber `propose` | stderr | no (unless `--json`, which dumps the whole prescription) | **no** — writes nothing without `--out` | no | `crossover_prescriber.py:381-385`, `:487-517` |
| prescriber `stage` | stderr | no (unless `--json`) | yes (stderr, `:596`) | no | `crossover_prescriber.py:595-597` |
| `jasper-null` | stderr (per-row `_line`, plan line) | **YES — `rows[]` on both terminal documents** | no (the `null_runs/` dir is never printed) | no (only in the `--help` epilog, `:768`, `:775`) | `null_door.py:614`, `:631-636`, `:626-627`, `:710` |
| `jasper-measure` | none (no stderr summary on success) | **YES — `record_ids[]` and `specs[]`** | yes, `bundle_dir` in the stdout doc | no | `measure.py:869-882`, `:994` |

Pattern: round-views is uniform (summary→stderr, stdout empty) with **three** exceptions;
crossover-prescriber is split **two verbs to stdout, two to stderr**; the two measuring
binaries put full arrays on stdout and give the LLM no small summary at all.

### 4. `--help` style

**Round-directory positional, every subcommand of the ten** (no `metavar` is set on any
of them, so argparse prints the `dest` verbatim):

| dest | metavar | Subcommands | file:line |
|---|---|---|---|
| `round_dir` | *(none)* | round-views `entry`, `per-seat`, `agreement`, `co-metrics`, `directivity`, `cloud-binding`, `forward-model`, `spec-sweep`, `gate-sweep`, `inventory` (10) | `grades.py:130,141`; `seats.py:143,158,166`; `cloud_binding.py:55`; `forward_model.py:106`; `sweeps.py:113,122`; `inventory.py:87` |
| `round_dirs` (`nargs="+"`) | *(none)* | round-views `repeat`, `repeat-floor` | `repeat.py:114`, `:122` |
| `baseline_dir` + `target_dir` | *(none)* | round-views `frozen` | `grades.py:135`, `:136` |
| `source_a` + `source_b` (`nargs="?"`) | *(none)* | round-views `frequency` | `frequency.py:96`, `:99` |
| `bundle_dir` | *(none)* | round-views `distortion`, `classify-features`, `delay-landscape`, `delay-confirm` | `distortion.py:74`; `classify_features.py:171`; `delay.py:180` |
| `session_dir` | *(none)* | prescriber `status`/`packet`/`propose`/`stage` (shared, `nargs="?"` for status/packet); `jasper-round bank` | `crossover_prescriber.py:1186-1188`; `round.py:420` |
| *(option, not positional)* `--bundle-dir` | *(none)* | `jasper-null` | `null_door.py:802` |
| *(options)* `--far-round` / `--close-round` | *(none)* | round-views `close-reference` | `close_reference.py:173`, `:174` |

**Six positional spellings** (`round_dir`, `round_dirs`, `baseline_dir`/`target_dir`,
`source_a`/`source_b`, `bundle_dir`, `session_dir`) plus **two option spellings** for the
same concept. LANDING's "spelled six ways" is TRUE and, counting the options, generous.
`_ROUND_DIR_HELP` (`_common.py:42`) supplies one help string to 12 of them; the other
seven write their own.

**Description lengths** (from `build_parser().description`, whitespace-normalised):

| Binary | prog set | AUTHORITY_TIER | Sentences | Chars |
|---|---|---|---|---|
| basic-profile | ✓ | ✓ | **2** | 192 |
| seat-level | ✓ | ✓ | **2** | 350 |
| angle-capture | ✓ | ✓ | 1 | 120 |
| measure | ✓ | ✓ | 1 | 58 |
| crossover-prescriber | ✓ | ✓ | 1 | 128 |
| round | ✓ | ✓ | **2** | 270 |
| round-views | ✓ | ✓ | 1 | **736** |
| null | ✓ | ✓ | **2** | 92 |
| audition | ✓ | ✓ | 1 | 58 |
| declare-geometry | ✓ | ✓ | 1 | 243 |

Four two-sentence descriptions, as LANDING said. The outlier LANDING did not name is
`round-views`: one 736-character sentence (`round_views/__init__.py:108-121`), 2.1× the
next longest and 12.7× the shortest.

### 5. The `jasper-round` "blocked → two exit codes" anomaly

**Still there.** `jasper/cli/round.py`:

```
:167   "status": "opened" if http == 200 else "blocked",
:174-178 "reason": ("" if http == 200 else REASON_ANSWER_LOST if http == 0 else REASON_OPEN_REFUSED),
:183-185 if http == 200: return EXIT_OK
         return EXIT_UNREADABLE if http == 0 else EXIT_REFUSED
```

So `status == "blocked"` is published for **both** exit 1 and exit 2; only `reason`
(`answer_lost` vs `open_refused`) separates them. LANDING cited `:191-192`; the −7-line
shift from `da6ad6082` puts the return at `:184-185`.

**Exact status → code mappings at HEAD:**

| Verb | status published | reason | code | file:line |
|---|---|---|---|---|
| `open` | `opened` | `""` | 0 | `round.py:167`, `:184` |
| `open` | `blocked` | `answer_lost` (http 0) | **2** | `round.py:167`, `:176`, `:185` |
| `open` | `blocked` | `open_refused` (http ≠ 0, ≠ 200) | **1** | `round.py:167`, `:177`, `:185` |
| `open` | `blocked` | `tier_required` (pre-flight) | 1 | `round.py:146-150` |
| `open` | `blocked` | `open_refused` (unreadable prescription doc) | 1 | `round.py:155-159` |
| `wait` | `terminal` | — | 0 | `round.py:87`, `:205` |
| `wait` | `failed` | — | 1 | `round.py:88`, `:205` |
| `wait` | `lost` | `answer_lost` | 2 | `round.py:89`; `wizard_client.py:317` |
| `wait` | `timed_out` | `wait_timeout` | 2 | `round.py:90`; `wizard_client.py:338` |
| `apply` | `applied` | `""` | 0 | `round.py:230-231` |
| `apply` | *(wizard's)* | `answer_lost` | 2 | `round.py:210`, `:232-234` |
| `apply` | *(wizard's)* | other | 1 | `round.py:235` |
| `bank` | `banked: true` | — | 0 | `round.py:261-266` |
| `bank` | `banked: false` | `exc.reason` | 1 | `round.py:252-253` |
| `bank` | `banked: false` | `write_failed` | 3 | `round.py:256-257` |

Two further shape anomalies in the same file: `bank` publishes `banked: bool` where every
other verb publishes `status: str` (`round.py:252`, `:256`, `:261`); and the text-mode
header line prints `status` **or** `reason` **or** `"ok"` interchangeably —
`round.py:98`: `print(f"{receipt['verb']}: {receipt.get('status') or receipt.get('reason') or 'ok'}")`.
Neither the wait-status map (`round.py:86-91`) nor any of these words is drawn from
`STATUS_BY_CODE`.

### 6. What landed since `5d32f683d`, per commit

`git log --oneline 5d32f683d..HEAD -- jasper/cli jasper/active_speaker/crossover_v2 tests/test_cli_exit_vocabulary.py` returns 33 entries, but 19 are merge commits whose first-parent diff replays main-side history that was already in `5d32f683d`. The **14 non-merge commits** and their verdict impact:

| Commit | Subject | Files in scope | Changes a verdict? |
|---|---|---|---|
| `da6ad6082` | One path-or-stdin reader for the prescription CLIs | `_refusal.py`, `crossover_prescriber.py`, `round.py` | **No verdict, lines only.** Deletes `round._read_document` and `prescriber._read_payload`; adds `read_source_bytes`/`read_json_source` to `_refusal.py:50-64`. Error surfaces unchanged. `round.py` −7 lines below `:102`, `crossover_prescriber.py` −4 below `:295`. **This is why every LANDING citation into those two files below those points is off by −7 / −4.** |
| `6a5603b70` | Give the round views one by-name refusal spelling | `round_views/_common.py`, `classify_features.py`, `delay.py` | **No verdict change.** `refused_by_name` widened to accept a sentence (`_common.py:169-176`); `delay` and `classify-features` stop hand-rolling `failed(...)`/`default_out`. The published record is byte-identical. `delay.py` −13 lines: LANDING's `delay.py:136` → `:124`. The grid is still on stdout. |
| `59e53e3cf` | Pin `jasper.audio_measurement` against importing `jasper.cli` | `tests/test_correction_boundary_ssot.py:267` | **YES — flips one boundary verdict.** LANDING's "holds, **unpinned** — wave 7 row 7.3 takes it" is now stale: it is pinned. |
| `2614bb94d` | Fold the five evidence publishers into RecordStore (ADR-0227 §12) | `crossover_v2/record_store.py` | No — this is LANDING's own row 8.11, executed. Touches no CLI, no refusal shape, no boundary row. |
| `30b608565` | Verify a banked route against what was written, not what arrived | `crossover_v2/record_store.py` | No — review follow-up to the above. |
| `915e480ff` | Return a report from the startup-anchor re-emit; the CLI prints | `jasper/cli/active_speaker.py` | No — `jasper-active-speaker` is off the tool menu (`generate-tuning-tool-menu.py:54-65`); none of the ten. |
| `5fef51376` | Read the mux control-socket path from one shared constant | `jasper/cli/system_soak.py` | No — not one of the ten. |
| `ad61a5f61`, `b9cb3233a`, `76f6eab75`, `cda6af1e5`, `f82606414`, `9dfb4c1a7`, `ba06e98ff`, `8a282d646`, `e2562cc51`, `9cbaf71ba`, `197004fc9`, `351f3f901`, `1ae2c7c64`, `645f4f03c` | the doctor cleanup batch (#4023 and siblings) | `jasper/cli/doctor/**` only | **No verdict change, but moves the `cli→web` allowlist citations.** `doctor/correction.py`'s three web imports are now at `:36`, `:671`, `:739`, `:750` (four sites, three module names), not LANDING's `:39,567,677,688`. The edge count (5 sites) and the allowlist are unchanged. |

**Net:** one verdict flips (`audio_measurement ↛ cli`, now pinned); nothing else in
§1's three tables changes truth value. What does change is **citation accuracy**: LANDING's
line numbers into `round.py` (−7), `crossover_prescriber.py` (−4), `round_views/delay.py`
(−13) and `cli/doctor/correction.py` (moved) no longer resolve, and two `pyproject.toml`
citations (`:57`, `:59`) never resolved at either SHA.

---

## Could not determine

Nothing material. Two notes on method: I did not attempt to invoke any of the ten
binaries' `main()` with refusing inputs (that would need hardware seams and a session
bundle), so the §1 census is derived from reading every `print`/`return` site rather than
from observed output; and the "eleven distinct shapes" count treats a document as distinct
when its stdout **key set** differs, which is the property an LLM parser sees — a looser
"is it status/reason/detail plus extras" grouping would collapse some of them toward
LANDING's five.
