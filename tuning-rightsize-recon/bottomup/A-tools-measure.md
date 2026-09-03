# A — measurement-side tools (bottom-up sizing)

Scope: the LLM-invoked measurement tools + the turntable mover + the off-menu
`jasper-active-speaker`. Read-only. Counts are `wc -l` at HEAD on branch
`claude/busy-goodall-mz0gvv`.

## Attribution rule

A module counts against a tool only when that tool is its **sole or dominant**
importer (importer counts from `scratchpad/bottomup/graph.json`). Everything the
brief names as shared substrate — `audio_measurement` DSP (`analysis`,
`program`, `null_walk`, `interference_nulls`, `wired_capture`, `calibration`),
`excitation_safety_plan` / `session_volume_plan` / `driver_safety` clamps,
`camilla_yaml` / `staging` emit, and the session/record contract
(`crossover_v2.session`, `record_store`, `contracts`) — is **excluded** and
sized elsewhere. So these numbers are the *tool-specific* code only.

## The table

| Tool | Does | Now (CLI + primary engine) | Clean 80/20 | Top reason for the gap |
|---|---|---|---|---|
| `jasper-declare-geometry` | write 4 rig numbers, print the derived entanglement floor | **165** (CLI 165; `measurement_geometry` 249 is substrate, 9 importers) | **80** | Already near-right. Only real cut is the `_unit_pair` helper (47 lines, 2 callers) and dual-unit printing. |
| `jasper-basic-profile` | review/apply the wizard's basic profile from the shell | **601** (CLI 465 + ⅓ `wizard_client` 136) | **150** | It is two HTTP POSTs + a receipt printer. 465 lines because it re-derives the profile summary, the issue list and a stale-fingerprint refusal the endpoint already runs (`basic_profile.py:246 _refuse_stale`). |
| `jasper-seat-level` | closed-loop ramp to a target seat SPL, bank the volume | **4,299** (CLI 738 + `seat_level_ramp` 2,245 + `branch_peak` 692 + `seat_level_reference` 394 + `wired_level_meter` 230) | **450** | **Over-abstraction.** See delta #1. |
| `jasper-angle-capture` | declare a pose walk, leave it on a spool for the next web session | **2,558** (CLI 766 + `angle_capture` 1,027 + `angle_capture_spool` 605 + `measurement_programs` 160) | **150** | **It exists only because the CLI and the web wizard cannot pass arguments.** See delta #3. |
| `jasper-arm-walk` | serve the wizard's position gate with the turntable | **1,455** (CLI 283 + `arm_walk` 1,036 + ⅓ `wizard_client` 136) | **250** | Poll/move/settle/POST is ~120 lines; the rest is `Mover`/`Session` Protocols, `WalkConfig`, `Trail`, `LoopbackSession`, `PowerVerdict`, `Pending`, `Poll` — 7 seam types for one loop that has exactly one implementation of each. |
| turntable mover (`experiments/usb-turntable/jts_turntable.py`) | serial adapter: power preflight, travel envelope, position, retry | **822** (vendor excluded) | **400** | Mostly **legitimately hard**: serial session retry on a vendor parse race (#2516), a ±45° travel envelope, throttle detection. Gap is prose (23%) and the hotplug-autostop path (`_run_hotplug_stop`, 75 lines). |
| `jasper-measure` | measure once at one placement, bank takes, print ids | **1,958** (CLI 1,321 + `measure_spec` 443 + ½ `crossover_v2.door` 194) | **650** | Prose 32%; `read_box_declaration` (`measure.py:241–372`, 132 lines) is a **self-disclosed second derivation** of `resolve_conductor_context`; four exception classes carrying partial-batch reporting. |
| `jasper-null` | play the summed reverse null at N delay coordinates, bank a row each | **1,516** (CLI 1,023 + ½ `door` 193 + ½ `delay_landscape` 300) | **100** | **Duplicate stack.** See delta #2. |
| `jasper-audition` | drop to a reduced DSP layer and put it back | **946** (CLI 291 + `audition` 654) | **250** | Legit transaction (swap graph, restore, cue on failure), but the restore path is 5 functions (`_swap_running_graph`/`_put_back`/`_restore_verdict`/`_undo_failed_arm`/`_durable_anchor`) where one try/finally would do. Prose 30%. |
| `jasper-round` | open/wait/apply a wizard round from the box | **460** (CLI 324 + ⅓ `wizard_client` 136) | **150** | It is deliberately thin — but it is the **third** copy of these verbs beside `scripts/run-crossover-round.py` (1,452) and the wizard's own buttons. |
| `jasper-round-bank` | copy a live session's evidence into the campaign home | **408** (CLI 131 + `round_bank` 277) | **100** | `cp -al` with an SSOT manifest. Fine; small prose trim. |
| `jasper-active-speaker` (off-menu, 13 verbs) | install/repair verbs for the runtime graph | **1,797** (CLI only; `path_safety`/`environment`/`camilla_yaml` are shared) | **350** | **Not a tuning tool and 8 of 13 verbs are dead or duplicated.** See "the ghost" below. |
| **TOTAL** | | **16,985** | **3,080** | |

Tests for the same units: `test_active_speaker_seat_level` 5,174 ·
`test_angle_capture_{seam,take,trigger}` 3,536 · `test_usb_turntable_experiment`
1,799 · `test_active_speaker_cli` 1,386 · `test_arm_walk` 1,370 ·
`test_cli_seat_level` 1,163 · `test_cli_measure` 883 · `test_null_confirm_door`
881 · `test_active_speaker_audition` 716 · `test_active_speaker_measurement_door`
439 · `test_cli_round` 347 · `test_round_bank` 323 · `test_cli_basic_profile` 283
· `test_measurement_mover_agnostic` 150 · `test_cli_declare_geometry` 135 ·
`test_measurement_programs` 113 = **18,698**. A parametrized rewrite against the
3,080-line surface lands ≈ **3,200**.

## Bottom-up total for this area

| | Product | Tests | Total |
|---|---|---|---|
| now | 16,985 | 18,698 | **35,683** |
| clean 80/20 | 3,080 | 3,200 | **6,280** |
| delta | −13,905 | −15,498 | **−29,403 (−82%)** |

Product delta by category (my apportionment of the 13,905):

| Category | Lines | Basis |
|---|---|---|
| **over-abstraction** | 5,900 | `seat_level_ramp` + `branch_peak`; `arm_walk`'s 7 seam types; `angle_capture`'s Request/ResolvedStop/program registry; per-tool refusal vocabularies (`REFUSE_*` re-rolled in 9 of 12 units) |
| **prose over the AGENTS.md bar** | 3,000 | measured: 26% of non-blank CLI lines, 40–46% of engine lines are comment+docstring. Keeping ~15% frees ~3.0k |
| **duplication** | 2,800 | `null` vs `measure`; `round` vs `run-crossover-round.py`; `active-speaker commission-*` vs the wizard's own POST routes; `read_box_declaration` vs `resolve_conductor_context` |
| **dead / ghost** | 1,500 | 4 `jasper-active-speaker` verbs with no caller; the angle spool's consumed-file lifecycle; `LoopbackSession`; `TurntableMover.move_to`'s unreachable branch (`arm_walk.py:393`) |
| **speculative / parked** | 700 | `tournament`/`spot` programs and `_screen_policy` in `angle_capture`; `--specs` mixed-pose refusal family |
| **legit** | 3,080 | what remains |

## The 3 biggest single deltas

### 1. The seat-level stack — 4,299 → 450 (−3,849)

`jasper-seat-level` answers one question: *what main volume makes the seat read
75 dB SPL?* The honest 80/20 is: read the mic, compute the gap, step, converge,
bank — 300 lines, plus a hard SPL stop that is already a non-negotiable and
already lives in `session_volume_plan`.

Evidence:
- `jasper/active_speaker/seat_level_ramp.py:1627` — `_walk_to_the_band` is **543
  non-blank lines in one function**, with 10 pieces of loop state each carrying
  a paragraph of narration (`:1668–1692`).
- `jasper/active_speaker/seat_level_ramp.py:1664–1697` — a 34-line docstring for
  a 12-line `write_fader`, arguing an asymmetric record point and citing
  `docs/adr/0005-fader-bound-asymmetric-record-point.md`. An ADR was minted for
  where a local variable is assigned.
- `jasper/active_speaker/branch_peak.py:38–56` — a three-bullet defence of
  writing a **second CamillaDSP renderer in Python** (biquads, mixers,
  pipelines, 692 lines, zero other importers) to derive one scalar, when the
  module's own documented fallback — the full-band peak — is what a clean
  implementation ships with. `jasper/cli/seat_level.py:40–44` confirms that
  fallback already exists and is "the conservative answer this verb shipped
  with."
- Prose in the pair: 882/2,246 (43%) and 249/693 (40%).

### 2. `jasper-null` is a second "measure once" implementation — 1,516 → 100 (−1,416)

`measure` and `null` are two independent play-and-capture stacks against the
same speaker, and only one of them uses the session/record contract.

- `jasper/cli/measure.py:796–890` — `measurement_door` → `BankedRecordStore` →
  `TuningSession`, banking standard takes with ids.
- `jasper/cli/null_door.py:428` `_play_and_capture`, `:382` `_publish_program`,
  `:406` `_resolve_mic`, `:554` `_row`, `:636` `_write_row` — raw `play_program`
  + `WiredRecorder` + a hand-rolled JSON row writer. `grep TuningSession
  jasper/cli/null_door.py` returns nothing.
- Everything `null` measures is already a `MeasureSpec`: `measure` ships
  `--polarity`, `--delayed-role`, `--delay-us`, `--level-matched` and `--specs`
  (a JSON list of specs against **one** placement — `measure.py:1282`), which is
  exactly `null`'s N-delay-coordinates-at-one-pose. The only thing `null` adds
  is reading depth from the capture (`null_door.py:490 _depth`, ~60 lines over
  `summed_capture_curve` + `shoulder_span` + `crossover_null_depth_db`).
- Clean shape: `jasper-measure --specs nulls.json` banks the takes;
  `jasper-null` becomes an **advisory** verb that reads depth off banked take
  ids. ~100 lines, and it stops being a "measured" tier tool at all.

There is a third and a fourth capture stack in this area:
`jasper-seat-level` uses `exec_correction_play` + `WiredLevelMeter`
(`cli/seat_level.py:475–481`), and the web wizard uses
`web/correction_crossover_v2_wired.WiredStimulusCapture`. Four ways to play a
signal and record it.

### 3. `jasper-angle-capture` is a workaround for two front ends — 2,558 → 150 (−2,408)

The tool writes a pose list to a spool file so the **web wizard** can pick it up
at session open:
- `jasper/active_speaker/angle_capture_spool.py:220 stage_angle_request`, `:333
  take_staged_angle_request`, `:438 _consume`, `:496 _validate` — 605 lines of
  stage/peek/take/withdraw/consumed-marker lifecycle with its own
  `AngleRequestRefused` vocabulary, for a JSON file.
- Consumers: `jasper/web/correction_crossover_v2.py:2032–2072` (session open)
  and `jasper/active_speaker/crossover_envelope_v2.py:2783`. **No CLI reads it.**
- `jasper/active_speaker/angle_capture.py` is 58% prose (511/1,028) and carries
  `per_driver_at`/`summed_at`/`both_at`/`_offset_cm_at`/`_screen_policy` — pose
  arithmetic for programs (`tournament`, `spot`) nothing on the menu drives.

If the pose walk were a flag on the measurement tool, the spool, its refusal
vocabulary, its consumed-marker protocol and its three test files (3,536 lines)
all disappear.

## The ghost: `jasper-active-speaker`

**Verdict: an operator/install tool, not an LLM tuning tool — and half of it is
dead.** It carries no `AUTHORITY_TIER`, is correctly absent from the generated
menu, and is named **zero times** in `docs/tuning-operator-runbook.md`,
`docs/tuning-methodology.md` or `docs/measurement-loop-doctrine.md`.

| Verb | Live caller | Verdict |
|---|---|---|
| `runtime-safe-graph` | `deploy/install.sh:1122,1218` | **keep** |
| `baseline-reemit` | `cli/doctor/audio_runtime.py:2201`, `control/transport_park.py:91`, `fanin/ring_health.py:1298` | **keep** (as recommended repair) |
| `commission-rollback` | `active_speaker/audition.py:165` message | keep, small |
| `commission-load` | wizard POSTs `./active-speaker/commission-load` (`deploy/assets/sound-profile/js/main.js:5925`) | **duplicate front end** |
| `commission-ramp step/ack/status/abort` | wizard POSTs `commission-ramp-*` (`main.js:5957,5966,5983`) | **duplicate front end** (4 verbs) |
| `startup-template` | none — only `docs/testing-tooling.md`, `docs/historical/…`, `calibration_agent/corpus/…` | **ghost** |
| `path-audit` | same | **ghost** |
| `path-probe` | same | **ghost** |
| `environment-probe` | same | **ghost** |

`startup-template` is the sole live caller of
`camilla_yaml.emit_active_speaker_startup_config` outside a status-registry
string (`correction/status.py:31`) — deleting the verb makes a 4.5k-line
substrate module's largest entry point collectible. Two handlers carry engine
logic that should not be in a CLI: `_cmd_runtime_safe_graph`
(`active_speaker.py:329–821`, **492 lines**) and `_cmd_baseline_reemit`
(`:821–1152`, **331 lines**).

## Does the tool set match the owner's model?

The owner's model is: **`measure`, whose mover is human or turntable**; plus
`seat-level`, `geometry`, `audition`. The code does not implement that.

**The mover is not a parameter of `measure` — it cannot be.**
`jasper/cli/measure.py:23–26` states outright that the door "prompts nobody" and
refuses a second `--position` (`REFUSE_ONE_POSITION_PER_RUN`, `measure.py:89`).
The turntable mover, meanwhile, only knows how to talk to the **web wizard**:
`arm_walk.py:423` `POSITION_READY_PATH = "/correction/crossover/v2/position-ready"`.
So `jasper-arm-walk` **cannot serve `jasper-measure`** — the two moving parts of
the owner's one sentence are not composable. A pose walk is therefore either N
manual `jasper-measure` runs, or a web-wizard round with `arm-walk` alongside,
and `jasper-angle-capture` exists purely to smuggle a pose list from the first
world into the second.

**Should be flags, not tools (4 of 11 menu tools in this area):**
- `jasper-null` → `jasper-measure --specs` + an advisory `null` read (delta #2).
- `jasper-angle-capture` → `jasper-measure --program baseline --size express`
  (delta #3).
- `jasper-arm-walk` → `jasper-measure --mover turntable` (default `--mover
  human`, which prints/serves the human-mover URL).
- `jasper-round` → the LLM's path is `measure` + `analyze` + `apply`; `round`
  and `scripts/run-crossover-round.py` are the wizard's sequencer wearing a CLI.

**Correctly their own tools:** `jasper-measure`, `jasper-seat-level`,
`jasper-declare-geometry`, `jasper-audition`, `jasper-round-bank`,
`jasper-basic-profile`.

**Missing:** nothing on the measurement side. There is no gap the LLM cannot
reach — the problem is the opposite. The one absent *seam* is `--mover
human|turntable` on `jasper-measure`, which is what would let three tools
collapse into one.

**Target shape for this area (6 tools, ~3.1k lines):**

```
jasper-measure    --mover human|turntable --program <name>|--position N --specs F
jasper-seat-level --target 75
jasper-declare-geometry set|show
jasper-audition   start|stop|status
jasper-round-bank
jasper-basic-profile review|apply
```
plus `jts_turntable.py` (the serial adapter, ~400) behind `--mover turntable`,
and `jasper-null` demoted to an advisory reader alongside the other
analyze-tier tools (another agent's area).
