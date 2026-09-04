# Fresh-eyes audit of the tuning half (2026-09-04, main `b963dad84`)

Scope: `jasper/active_speaker/`, `jasper/audio_measurement/`, `jasper/correction/`
(boundary only), the tuning CLIs, `jasper/web/correction_*`, tuning-adjacent
`scripts/`, `experiments/usb-turntable/`, the three runbook docs and their tests.
Method: seven read-only Sonnet tiles (one per audit question), one Opus skeptic
per tile whose default verdict was *refute*, one Sonnet gap-fill over the ten
large files no tile mapped, and an orchestrator re-check of every finding this
report leans on. Main moved to `5aa1a1f` during the run; none of the merges
touched a file in scope (doctor, AEC, control, identity only), so the findings
stand at both SHAs. Skeptics refuted, downgraded or marked already-handled 55 of 69 raw findings; what
follows is what survived, ranked by how badly it hurts an LLM walking
`docs/tuning-methodology.md` end to end. The completeness critic's independent
top five agrees with the order below with one addition: the exit-code collision
(§2) was rediscovered by three tiles after the first skeptic downgraded it, and
sits between S1 and S2 in harm.

## 1. Where the walk stalls, and the smallest thing that closes each

| # | Stall | Evidence | Smallest fix | Kind |
|---|---|---|---|---|
| S1 | The topology and alignment doors (§3, §4) are unreachable from the box. `jasper-round open` takes only `--tier`/`--stage`; the wizard client posts `{"tier"}` and nothing else; only the laptop script sends `alignment_prescription`/`topology_prescription`. | `jasper/cli/round.py:257-278`; `jasper/active_speaker/wizard_client.py:183-191`; `scripts/run-crossover-round.py:1152-1303` | Two pass-through flags on `jasper-round open` (`--alignment-prescription`, `--topology-prescription`, file or `-`), threaded into the session-open body. | new-flag |
| S2 | §4's confirm is un-graded. `delay_landscape.confirmation_verdict` exists, is unit-tested, and has no production caller; `jasper-delay-sweep propose` writes no artifact (no `--out`), so the prediction lives in scrollback; `jasper-null` rows carry `delay_us`, `depth_db`, `polarity` but nothing joins them to the prediction. The methodology already admits it (§4 "Grading the confirmation is not wired"). | `crossover_v2/delay_landscape.py:430`; `jasper/cli/delay_sweep.py` (0 `--out` args); `jasper/cli/null_door.py:495-500,584` | `jasper-delay-sweep confirm <bundle>`: re-run `compute_landscape`, read `null_runs/*.json`, call `confirmation_verdict`, write `delay_confirmation.json` beside the round; give `propose` a beside-the-round `--out` default. | new-artifact (wires existing code) |
| S3 | The trim decision's evidence is dropped at the commit seam. `LinearizationState` persists only the outcome string; `"fitted"` maps to `COMMITTED_PAIR_UNRECORDED` by design, so which pair won and the anchor drift are unrecoverable from any artifact, and §5/§7 level-vs-shape diagnosis runs blind. | `crossover_v2/proposal.py:60-79` (its own docstring); `crossover_v2/intervention.py:741-771` | Persist `committed_side` and `anchor_drift_db` on `LinearizationState` at the commit seam; `trim_strategy_for_outcome` then stops guessing. | new-field |
| S4 | §1c's install step has no verb. The doc says "put that file on the speaker at `/var/lib/jasper/active_speaker_repeat_floor.json`"; `round-views repeat-floor` requires `--out`; `repeat_floor.write_repeat_floor` exists but no CLI targets the on-box path, and the bank script only pulls it. | `docs/tuning-methodology.md:165-170`; `jasper/cli/round_views.py:766-775`; `jasper/active_speaker/repeat_floor.py:114-126`; `scripts/bank-crossover-round.sh:196` | `--install` on `round-views repeat-floor` (or an on-box `--out` default) calling the existing writer. | new-flag |
| S5 | Which protection-phase composition a propose curve got is "recorded nowhere" (§4 step 1). The boolean exists (`analysis.configured_path_composed`) but the take record stamps only `phase`, and `delay-sweep propose --phase` never echoes what it read. | `audio_measurement/program_analysis.py:969,4108`; `crossover_v2/spatial.py:791,968`; `jasper/cli/delay_sweep.py:256` | Stamp the existing boolean beside `phase` in the take record; echo it in the propose payload; delete the doc sentence. | new-field |
| S6 | `jasper-round wait` prints a `session_id`; the next verb wants a directory. The join is literally `sessions/<session_id>` and the runbook shows it, so this is friction, not a stall. | `jasper/cli/round.py:150-167`; `active_speaker/bundles.py` | One line in `wait`'s text output naming the session directory when `--base-url` is loopback. | doc/one-line |

Refuted stalls worth recording so they are not re-litigated: §1b, §7 and §10
naming no verb is by design (the methodology header assigns "which verb" to the
runbook); the seat-level reference is consumed automatically by every session's
volume plan and disclosed by doctor, so no `show` verb is owed; the `fc_min`,
`θ_null` and comb-peak arithmetic is left to the LLM deliberately and the
lobing bound cannot be coded because centre-to-centre spacing is not declared
(`driver_spacing_m` is plumbed and pinned at 0.0, §0);
`jasper-project-ring` is named at the exact point of need.

## 2. Tool sizes: confirmed, overturned, and the vocabulary problem

| Tool | Recon verdict | This audit | Evidence |
|---|---|---|---|
| `jasper-null` | too big | **confirmed**: 919 lines of program synthesis, play, capture and bank with no engine module; also re-declares its exit codes locally | `cli/null_door.py:30,137-720` |
| `jasper-measure` | orphan-ish, too big | **confirmed as duplicate**: `read_box_declaration` is a self-admitted second derivation of `resolve_conductor_context`'s readers | `cli/measure.py:194-330,205` |
| `jasper-active-speaker` | too big | **partly overturned**: 12 verbs under one binary is the endorsed shape; the real engine work is `_reemit_staged_startup_anchor` (269 lines) | `cli/active_speaker.py:510-779` |
| `run-crossover-round.py` | too big | **overturned on size** (composes on-box tools by subprocess), **confirmed on vocabulary** (12-value exit set) | `scripts/run-crossover-round.py:222,470-986` |
| angle-capture `plan`, prescriber `propose` | too small | **overturned**: dry-run beside its mutating sibling is the toolbox's convention | `cli/angle_capture.py:469,488`; `cli/crossover_prescriber.py:258` |
| `delay-sweep propose` | too small | **overturned**: flattening breaks the menu row for one token | `cli/delay_sweep.py:227` |
| `project-ring` | flag not binary | **overturned**: named at the point of need; `--dumps` refuses cleanly | `docs/tuning-methodology.md` §6 |
| `bank-crossover-round.sh` | duplicate of `round-bank` | **downgraded**: hand-typed twin of `round_inputs` filenames, drift is reported not silent | `scripts/bank-crossover-round.sh:163-200` |
| `jts_turntable.py` | reachable by path | **confirmed** (11 verbs, not 9); product code hardcodes the deployed path | `active_speaker/arm_walk.py:104` |

**Exit vocabularies are the one Q2 finding that hurts the walk.** The round
family alone speaks three: `jasper-round` (1 transport-lost, 2 refused, 3
timeout), `jasper-round-bank` (2 refused, 3 bank-failed), and the shared rule
(1 refused, 2 unreadable, 3 unwritable); the prescriber inverts 1/2;
`jasper-null` and `jasper-measure` re-declare 0/1/2 and have no unwritable code
although both file artifacts; the bench mixes two dialects in one file. An LLM
branching on `$?` misreads a refusal as a transport loss.
(`cli/round.py:56-68`, `cli/null_door.py:30`, `cli/measure.py:1019`,
`cli/crossover_prescriber.py`, `cli/bass_extension_bench.py:49`.)

## 3. Judgment and determinism on the wrong side

Almost every candidate here was refuted: `decide_trim`'s 6 dB sanity margin is
not a veto (level-preserving fallback, disclosed, re-measured), the doors refuse
only on declared bands, and the methodology's hand arithmetic is a stated
choice. Two survive:

- **S3 above**: the machine made a graded decision and threw the grade away,
  so the LLM re-derives it from a summed curve.
- **Latent role swap in a clamp path**: the topology floor and ceiling are read
  positionally (`pair[1]` as tweeter, `pair[0]` as woofer), guarded by
  `len == 2` and `"tweeter" in role_targets`, never by `pair[1].role`. A
  reordering of the role tuple would silently swap the declared floor and
  ceiling that §4's third clamp checks. `web/correction_crossover_v2.py:6172-6181`.
  Fix: look the pair up by role. Non-negotiable adjacent.

## 4. Where a tool dumps

- `jasper-crossover-prescriber packet` with no `--out` prints the whole
  curve-bearing document to stdout; every cloud position's `magnitude_db` and
  every lateral take's curve is inline (`crossover_v2/evidence_packet.py:769-781,930-932`).
  The small view already exists (`status`), so the fix is one rule, not a new
  artifact: `packet` defaults `--out` beside the round and prints the path plus
  the `status` sections. `cli/crossover_prescriber.py:206,1252`.
- Five analysis tools print JSON unconditionally; the action tools gate it behind
  `--json`. Fine for an LLM caller; one runbook sentence.

## 5. God files, verified at HEAD

| File (lines) | Verdict | The one split |
|---|---|---|
| `web/correction_crossover_v2.py` (7608) | mixed, but the skeptic refuted the big relocations: `prepare_v2_session` and `handle_v2_apply` parse requests and delegate | move `resolve_conductor_context` (4257-4497) *down* into the engine; that also removes three of the six cli→web imports |
| `active_speaker/startup_load.py` (2333) | god file by its own docstring: two lifecycles | split at ~1173 → `commission_load.py` |
| `active_speaker/web_commissioning.py` (2331) | god file | lift the raw AF_UNIX mux client (573-914) into its own module |
| `web/correction_crossover_backend.py` (1734) | god file: `CrossoverLevelLease` is a 925-line class mixing volume recovery, level-match orchestration, repeat bookkeeping and payload validation | web-twin step 10; last and non-negotiable tier |
| `cli/active_speaker.py` (1800) | one engine block in a CLI | `_reemit_staged_startup_anchor` (510-779) → `startup_load` |
| `audio_measurement/program_analysis.py` (4977) | eight banner sections, pure, no I/O | mechanical package split along the banners; optional |
| `active_speaker/staging.py` (2459) | cohesive but exports a declaration vocabulary four unrelated modules import | `declaration_vocabulary.py` (342-531) |
| `active_speaker/driver_safety.py` (2956) | cohesive plus a 300-line LLM prompt template | `driver_safety_prompt.py` (1402-1706) |
| `camilla_yaml.py` (4025) | single concern; the hearing clamp is six identical inline pairs | one module-local `_assert_volume_limit` keeping `ActiveSpeakerConfigError` (lines 2101, 2272, 2675, 3297, 3586, 3812); non-negotiable tier |
| `runtime_contract.py`, `baseline_profile.py`, `crossover_v2_flow.py`, `commissioning_evidence.py`, `web/correction_setup.py` | large-but-cohesive, or safety-adjacent, or the other product | leave |

Sideways edges at HEAD: six cli→web imports in three modules
(`cli/measure.py:711`, `cli/null_door.py:145-146`, `cli/doctor/correction.py:567,677,688`);
zero audio_measurement→engine; one correction→active_speaker
(`correction/runtime_safety.py:16`), documented but not pinned as the only one.
Neither boundary test forbids cli→web (`tests/test_correction_boundary_ssot.py:217-228`).

Dead or misplaced: `crossover_v2/priors.candidate_priors` (own docstring: no
production caller; tests only) → delete; `correction_crossover_backend.begin_commissioning_run`
(zero callers) → delete; `crossover_v2/attempt_grading.py` is 31 lines of
constants wearing a grading name → fold; twelve `_utc_now` copies. Not dead:
`delta_probe_run.py` (Phase-0 flag was wrong: `crossover_v2_flow.py:175,4354`),
`calibration_agent` (live behind `web/correction_tuning.py`, the other product).

## 6. The retired plan's phases 3 to 5

Keep: 3.5 (the CLI surface, reshaped as §1 and §2 above rather than "absorb
everything into `measure`"), the one-helper form of 3.6, 4.4/web-twin step 10
(one volume owner, last), and the conductor test split (the fixture half of 3.9
already landed and a ratchet forbids importing the 12,097-line file).
Drop as right-sizing for its own sake: 3.1 (73 exception classes → 1: the LLM
sees codes, not classes), 3.2, 3.3, 3.7, 4.1–4.3, all of Phase 5, and the three
never-created web-twin destinations (`session_assembly`, `apply_transaction`,
`capture_wired`). 3.8 is the room-correction product. Where 2.6/2.7 or 3.4 fall
inside a wave's diff they happen as part of that PR, never as their own.

## 7. Coverage ledger and what only hardware can prove

Opened by a named reader: 146 of 275 in-scope product files, 141,540 of 204,968
lines (69%). Unopened: 44k lines of `active_speaker/` top level (the whole
`commissioning_*` cluster, the driver-safety cluster, `linearization_envelope.py`,
`seat_level_ramp.py`, `design_draft.py`), 13k of `crossover_v2/` (37 modules,
among them `journey.py`, `capture_dispatch.py` and `candidates.py`, which two
skeptic verdicts cite without a tile having opened them), 3k of `web/correction_*`
(including `correction_crossover_v2_wired.py`, the source of one cli→web import),
the bench package, `scripts/compare-readings.py`, and three shell scripts. Tests
(879 files, 572k lines) were sized and their top ten docstrings read, not combed.
Rationalised past, per the completeness critic: `verification.py`'s four
`evaluate_*` bodies and `refusal_copy.py`'s registry were trusted on their
docstrings for the "no hidden veto" verdict; `ramp.py`, `spatial_combine.py` and
`playback.py` were read at signature depth. The first run's eight agents on the
wrong model tier opened files and wrote nothing.

Static only: every claim above. Hardware or runtime only: whether the six
inline clamps and the shared primitive agree on a live config; whether S1's
flags reach the door with the same refusal text the laptop path gets; S2's
verdict on a real null grid; the turntable path on an installed Pi.
