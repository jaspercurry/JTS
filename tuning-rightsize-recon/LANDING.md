# Landing — fresh eyes on the tuning right-size program (2026-09-04, main 5d32f683d)

Verification of TARGET.md against HEAD, and the plan that lands the program. Eight read-only lanes (Sonnet digests, Opus verifications) produced the evidence; the verdicts and the plan are the orchestrator's. Citations are `file:line` at main 5d32f683d.

## 1. TARGET.md at HEAD

### Ten binaries

| Binary | Verdict | Evidence |
|---|---|---|
| `jasper-declare-geometry set\|show` | TRUE | `pyproject.toml:59` |
| `jasper-seat-level` | TRUE — uses only `EXIT_OK`/`EXIT_REFUSED`; no unreadable path, and it has none | `jasper/cli/seat_level.py:735` |
| `jasper-angle-capture plan\|stage\|withdraw\|serve` | TRUE for roster/subcommands; refusal shape is the `{"ok": false}` one | `jasper/cli/angle_capture.py:481` |
| `jasper-measure` | TRUE | `pyproject.toml:57` |
| `jasper-null` | TRUE as a binary; same admission gate as `jasper-measure` — not an NN gap | `jasper/active_speaker/program_playback.py:86` |
| `jasper-round open\|wait\|bank\|apply` | PARTIAL — own `_emit` receipt shape, `--json`-gated; `blocked` maps to two exit codes; transport-loss reasons are `answer_lost`/`wait_timeout` | `jasper/cli/round.py:92` |
| `jasper-round` `blocked` status maps to two exit codes | confirmed anomaly — `status`/`reason` printed interchangeably in text mode | `jasper/cli/round.py:191-192` |
| `jasper-crossover-prescriber status\|packet\|propose\|stage` | PARTIAL — `propose` writes nothing without `--out`; several bare `error:` stderr paths | `jasper/cli/crossover_prescriber.py:381` |
| crossover-prescriber bare `error:` stderr paths | confirmed — no JSON document on those paths | `crossover_prescriber.py:361,366,545,554,574` |
| `jasper-round-views <view>` | TRUE — 19 subcommands = 17 artifact rows + `repeat-floor` (publishes to the box) + `inventory` (writes `inventory.json`), both documented exceptions; `distortion` requires `--dumps` | `jasper/cli/round_views/_common.py:79-104` |
| `distortion --dumps` vs `classify-features`' default ring | PARTIAL — the same default is valid: both read the same `RING_SIDECAR_GLOB` sidecars (row 8.5) | `jasper/cli/round_views/distortion.py`; `harmonic_evidence.py` |
| `jasper-basic-profile review\|apply` | TRUE roster; refusal shape `{"status":"blocked","refused_by":"client"}` | `jasper/cli/basic_profile.py:249-251` |
| `jasper-audition start\|stop\|status` | TRUE; refusal document `--json`-gated | `jasper/cli/audition.py:179-196` |
| `jasper-arm-walk` gone | TRUE | absent from `[project.scripts]`; pinned by `tests/test_arm_walk.py:1389` |
| `jasper-round-bank` gone | TRUE | zero hits repo-wide |
| `jasper-delay-sweep` gone | TRUE | zero hits repo-wide |
| `jasper-gate-sweep` gone | TRUE | zero hits repo-wide |
| `jasper-classify-features` gone | TRUE | zero hits repo-wide |
| `jasper-read-distortion` gone | TRUE | zero hits repo-wide |
| `jasper-close-reference` gone | TRUE | zero hits repo-wide |
| `jasper-forward-model` gone | TRUE | zero hits repo-wide |
| `jasper-project-ring` gone | TRUE | only a historical docstring mention |
| `jasper-active-speaker` off the menu | TRUE | `scripts/generate-tuning-tool-menu.py:54-65 TUNING_TOOL_MODULES` names exactly the ten |
| `scripts/run-crossover-round.py` a transport, not a 2nd impl | TRUE (as documented) | `jasper/cli/round.py:316` description |
| menu `--check` | TRUE — exit 0 | `scripts/generate-tuning-tool-menu.py --check` → exit 0 |

### Conventions

| Convention | Verdict | Evidence |
|---|---|---|
| One exit vocabulary | TRUE for codes — nothing outside {0,1,2,3}, one sanctioned exemption; sub-claim "reason=transport" FALSE — the slugs are `answer_lost`/`wait_timeout` | `jasper/active_speaker/wizard_client.py:75` |
| One refusal shape | FALSE — five shapes coexist (`_refusal` doc, `{"ok": false}`, `round._emit` receipts, `{"accepted": false}`, `{"status":"blocked",...}`); four tools gate the document behind `--json` | `jasper/active_speaker/crossover_v2/blend_prescription.py:315-320` |
| One artifact rule | PARTIAL — holds for 17 views + packet; `propose` is stdout-only without `--out`; `repeat-floor` has no default by design | `jasper/cli/round_views/repeat.py:85-86` |
| Answer small | PARTIAL — views put the summary on stderr and leave stdout empty unless `--out -`; `delay-landscape` prints the whole grid to stdout; only `delay-landscape` prints the next command | `jasper/cli/round_views/delay.py:136` |
| One `--help` style | PARTIAL — `prog`/`AUTHORITY_TIER` on all ten; four two-sentence descriptions; the round positional spelled six ways | `round_dir`/`bundle_dir`/`session_dir`/`round_dirs` variance |

The roster test pins exit-code constants only, never a printed refusal document —
this is the one LLM-facing convention still open.

### Boundary rule

| Edge | Verdict | Pin | Evidence |
|---|---|---|---|
| Engine (`active_speaker`, `crossover_v2`) never imports a surface | TRUE, pinned (whole-package rows) | `tests/test_correction_boundary_ssot.py:271,329` | 0 edges by AST scan |
| `audio_measurement` ↛ `active_speaker`, `correction` | TRUE, pinned | `tests/test_correction_boundary_ssot.py:266` | 0 outgoing edges into any of the five packages |
| `audio_measurement` ↛ `web` | TRUE, pinned | `tests/test_runtime_import_closure.py:28` | `FORBIDDEN` = web/control/voice_daemon/mux/camilla |
| `audio_measurement` ↛ `cli` | holds (0 edges), unpinned — wave 7 row 7.3 takes it | none | `FORBIDDEN` omits `cli`; `PACKAGE_BOUNDARIES` row 1 forbids only correction + active_speaker |
| `active_speaker` ↛ `web`, `cli`, `correction` | TRUE, pinned | `tests/test_correction_boundary_ssot.py:271` | 0 edges, no allowlist entries |
| `cli` → `web` | 5 edges, all allowlisted; allowlist cannot rot (an unused entry reddens) | `tests/test_correction_boundary_ssot.py:276`; allowlist `:168-188`; rot pin `:317-325` | `cli/doctor/correction.py:39,567,677,688`; `cli/measure.py:663` |
| `correction` → `active_speaker` (only `runtime_safety.py`) | TRUE, allowlisted | `tests/test_correction_boundary_ssot.py:281`; allowlist `:160-167` | `jasper/correction/runtime_safety.py:16` |
| Zero unallowlisted violations | TRUE — independent AST scan of 385 files, module-level and function-local | — | 6 forbidden edges found, all six allowlisted |
| Fast-lane coverage | gap — neither boundary test is in `scripts/test-fast`'s always-on guard list; merge-lane guarantees only | — | `scripts/test-fast` always-on list: dependency_groups, lint_contracts, deploy_wiring_guards, shell_awk_environ_convention, docs_impact |
| TARGET's "new row" / "extend to all of active_speaker" future tense | stale — all three already exist at HEAD | `tests/test_correction_boundary_ssot.py:271,276,281` | — |

### Round directory as memory

| Artifact or field | Verdict | Evidence |
|---|---|---|
| `info.json` (after bank) | TRUE | `jasper/active_speaker/bundles.py:251` |
| `declared-geometry.json` (after bank) | TRUE | `jasper/active_speaker/round_bank.py:115` |
| `round_receipt.json` — the file | TRUE | `crossover_v2/record_store.py:128` |
| `round_receipt.json` — four verdicts (trust/safety/quality/headroom) | TRUE | `round_evidence.py:442 RoundEvaluation.axes()` |
| `round_receipt.json` — adoption row | TRUE | `contracts.py:1101 AdoptionDecision` |
| take records: `phase` | TRUE | `crossover_v2/spatial.py:826` |
| take records: `phase_composition` | TRUE | `crossover_v2/position_cycle.py:157` |
| `repeat-floor.json` (when banked) | TRUE | `round_bank.py:111` |
| `position_cycle.json` | PARTIAL — only the laptop transport writes it; on-box `jasper-round bank` does not; no production reader; `bank_round` has every input on disk after `round_bank.py:232-236` (row 8.4) | `scripts/run-crossover-round.py:761-763`; `round_bank.py:97` |
| After-analysis view artifacts | PARTIAL in the letter, TRUE in spirit — 2 of 17 are read back by code (the evidence packet); the other 15 are for the LLM to read, by design; `inventory` checks presence only | `evidence_packet.py:2727,2729`; `jasper/cli/round_views/inventory.py:56` |
| `packet.json` (after prescribe) | TRUE | `jasper/cli/crossover_prescriber.py:204` |
| `proposal.json` | NOT the prescription — the baseline-profile document written at apply time, one presence-only reader; the expectation fields ride in `candidate.json` instead | `bundles.py:937,977`; `record_store.py:127` |
| `expected_delta_db` | TRUE — genuine cross-session round-trip; spelling differs from TARGET's `expected_delta` | `driver_prescription.py:1489`; `round_views.py:652,772` |
| `declared_tilt_db_per_octave` | TRUE — spelling differs from TARGET's `declared_tilt_db` | `driver_prescription.py:150`; `round_views.py:773` |
| `delay_confirmation.json` | written, never read back by code — LLM only | `jasper/cli/round_views/delay.py:248` |
| `apply.json` (after apply) | PARTIAL — presence-only, like `proposal.json` | `jasper/active_speaker/bundles.py:951` |
| `applied-profile.json` | TRUE as a file | `APPLIED_PROFILE_DEFAULT_PATH` |
| `LinearizationState` w/ `committed_side` + `anchor_drift_db` | FALSE — `applied-profile.json` does not carry this dataclass | `crossover_v2/candidates.py:54` |
| `committed_side` | FALSE — a property on `TrimDecision`, reaches only a log event | `intervention.py:1513` |
| `anchor_drift_db` | PARTIAL — in-process only; `trim_strategy` is what's serialized | `contracts.py:676` |
| σ kind ("each σ names `kind`") | FALSE — two label registers carry `kind`; five σ producers carry none | `feature_classification.py:170`; `evidence_packet.py:547`; unlabelled: `spatial_combine.py:452`, `planning.py:301`, `gate_sweep.py:565`, `feature_classifier.py:2057` |
| §6a rung (`close_reference.json` verdict) | PARTIAL / mis-stated — verdict is per spec band, not a round-level rung; no production reader | `crossover_v2/close_reference.py:314`; written beside the FAR round, `round_views/close_reference.py:13` |

Audit stall S3 (the trim decision's evidence dropped at the commit seam) is half-closed by the trio above:
`trim_strategy` lands in the artifact, `anchor_drift_db` and `committed_side` do not.

## 2. Waves 4–6: right, wrong, and what I would have done differently

Right:
- stalls first — S1–S5 closed in wave 4, each with a pin.
- nine binaries into two with no shims and byte-identical `--help`.
- one exit-code set with an AST roster pin.
- integration-branch batching once the serial-rebase tax was measured.
- four lanes stopped on false premises and said so (wave-3 row 2.1 twice; wave-6 rows 3.2 and 3.10).
- the NN clamp row proven byte-identical across 6 emitters × 14 value classes before being held for the hardware pass (#3991).
- ADR-0231 turned four code-comment-only rulings into a record.
- two mechanics lessons caught before CI, not by it — `--ours` reinstating a deleted entry point, `--ours` dropping a sibling's prose (WAVE-LOG wave 5).

Wrong or incomplete:
1. Shape after size. Wave 4 unified exit CODES; the refusal SHAPE the LLM parses was
   left five-way while wave 6 spent eight PRs on relocations the LLM never sees.
2. Relocation is not layering. AST-identity moves carried surface code into the engine verbatim:
   - row 3.3 put `argparse.Namespace` and eleven `print(` calls into `startup_load.py`; wave 7 row 7.4 now pays for it.
   - row 3.6 added +582 lines of package overhead and a 312-line pure re-export facade.
   - the proof guaranteed nothing broke and also guaranteed nothing improved.
3. The target was never re-verified after the waves. Six TARGET sentences were never true and no row owned them:
   - σ kind
   - `LinearizationState` in applied-profile
   - reason=transport
   - stdout summaries
   - position_cycle on bank
   - the "new row" future tense
   - a target that overclaims misleads the next agent more than a shorter one would.
4. The CI leak noticed in wave 4 stayed a follow-up for three waves and taxed every PR — six cancels at the 30-min limit, three spawn-bound failures; it should have been a wave-4 row.

What I would have done differently:
1. refusal shape before god-file splits.
2. every relocation carries the one contract change that makes its destination honest (return a report; import from the owning module) or does not happen.
3. the wave close re-verifies every TARGET sentence the wave touched (what LANDING.md now does after the fact).

## 3. Where the program is against the vision

Vision item → status:
- every capability a CLI subcommand with argparse inputs: TRUE — ten binaries, menu `--check`
  green, zero retired names.
- named JSON artifact beside the round via one table: TRUE for views + packet; `propose` the
  exception.
- generated menu row: TRUE.
- methodology pointer at the step: TRUE where a verb is owed; six steps name no verb by design
  — §1b polarity, §3 corner (via the topology door on `jasper-round open`), §5 level match ("no
  separate verb"), §6a rung 4, §7 summed verify — and §6a rung 3 discloses "not built" (the LLM
  declares the distance).
- one exit vocabulary with a named reason: codes TRUE; shape FALSE.
- truth < engine < surfaces pinned: TRUE (merge lane), last edge pinned by wave 7.
- evidence = JSON bundles, no DB: TRUE; room correction separate: TRUE, pinned.

Dropped list: HOLDS on every row.
- 3.1 — 88 exception classes with no cluster larger than 4.
- 3.2 — 5 `*_door` defs, two same-named in different layers.
- 3.4 — 103 duplicated helper names, mostly 3-line coercers with differing raise shapes;
  stays "inside a PR already touching the file".
- web-twin destinations never created and the walk does not need them.
- Phase 5 rewrites close no stall.

One correction: 3.10 (level lease) was already true, and the remaining v1 half is dead,
not a convergence.

Held list:
- `jasper-null` fold → DROP: same admission gate (`program_playback.py:86` via `readmit_program_from_wav`, `program_admission.py:615`); TARGET keeps it as a binary;
  the walk is closed by delay-confirm.
- `run-crossover-round.py` codes → DROP: phase vocabulary of a laptop orchestrator, documented
  in `EXIT_NAMES`, 15 pins; collapsing loses the phase.
- `bass_extension_bench --live` → not this program (ADR-0018/0229).

Honest position: the program is ~85% of the vision.
What remains is one LLM-facing convention, a handful of memory fields, one duplicated
capture kernel, docs history, and the fold.
One short closing wave, then stop; after it, code shape has no more value for the LLM
than hardware rounds do.

## 4. Wave 8 — the landing plan

Coordination: wave 7 (other session, in flight, one batch) owns
`round_views/{delay,classify_features,distortion}.py` by-name arms, `mux.py`/`renderer.py`/`system_soak.py`
literals, the am→cli pin, `startup_load` reemit report, `round.py/_read_document` +
`crossover_prescriber._read_payload` helper, `commissioning_run.replace_current`.
Wave 8 rows touching `round.py` / `crossover_prescriber.py` / `round_views/delay.py` start after wave 7 lands.
Note for wave 7's 7.5: lane E found only TWO path-or-stdin readers (bytes-for-hash vs parsed) — the third `== "-"` is a writer.

| Row | Concern | Tag | Proof | Gate | Size |
|---|---|---|---|---|---|
| 8.1a | one refusal shape — `jasper-round`: receipts print `_refusal`'s document on every refusal regardless of `--json`; `status` ∈ STATUS_BY_CODE with `answer_lost`/`wait_timeout` as `reason` under `unreadable`; `blocked` maps to one code | W | roster shape pin (8.1d) green for round; existing round tests | code-review high (apply path) | small |
| 8.1b | one refusal shape — angle_capture (`{"ok": false}` retired), basic_profile (`refused_by` becomes `detail`), audition + measure (refusal document no longer `--json`-gated); `close-reference --distance` success stops using the key `status` | W | 8.1d green | code-review | small |
| 8.1c | one refusal shape — crossover_prescriber: bare `error:` paths and the `{"accepted": false}` verdict print the shared document (verdict in `detail`) | W | 8.1d green | code-review | small |
| 8.1d | the pin: extend tests/test_cli_exit_vocabulary.py from "constants are shared" to "every refusal prints `{"status","reason","detail"}` on stdout and status == STATUS_BY_CODE[code]" — one parametrized test over the roster, invoking each CLI's `main` with a refusing input (or an AST pin that every EXIT_* return site is a `refused(`/`failed(` call); declare_geometry exempt | T | test red on HEAD for 8.1a–c's files, green after | sanity look | small |
| 8.2 | one wired capture kernel: `WiredStimulusCapture`, `make_wired_recorder`, the pre-play/post-roll constants → the engine (`crossover_v2/`); `null_door._play_and_capture` deleted and null runs get the zero-run scan + integrity report measure has; the three `resolve_wired_mic` refusal copies → one; `BOUNDARY_ALLOWLIST["jasper/cli/measure.py"]` entry deleted | R+D | astmove identity of moved bodies; null run pinned to refuse on a zero capture; allowlist test green with the entry gone | two-pass code-review (relocation rule); owner: one null run on the box | medium |
| 8.3 | trim evidence in the artifact: the linearization block of applied-profile.json carries `anchor_drift_db` and `committed_side` beside `trim_strategy` (closes audit S3) | W | one behaviour pin; fields land as top-level siblings of the mirror, NOT inside `recomposition_snapshot` (which `baseline_candidate_fingerprint` hashes) | code-review | small |
| 8.4 | `jasper-round bank` writes `position_cycle.json` from the take records so on-box rounds match laptop rounds; the write sits OUTSIDE `bank_round`'s `except OSError` rmtree block and a failure is disclosed, never fatal to the bank | W | banked fixture round carries it | code-review | small |
| 8.5 | answer small: `propose` defaults `--out` beside the packet as the accepted-result envelope it already writes (name from the module's vocabulary + one constant; NOT `prescription.json`, which `stage --prescription` reads) and prints the admissibility summary + path; `delay-landscape` prints the bounded summary + `confirm_with` + path, the grid stays in the artifact; `distortion --dumps` defaults to the same ring as classify-features | W | stdout pinned array-free; artifact appears | code-review | small |
| 8.6 | dead defs with verdicts: `CrossoverLevelLease.{run_level_match, configure_targets, driver_sweep_locked_main_volume_db}` (SUPERSEDED by correction/session.run_level_match + measured rounds; 0 prod callers; 15 test sites) and `FaninGateContext` nested mode (SPENT: never constructed in production; the relabel path and the `fanin_gate_context` parameter on six signatures go; standalone path untouched) | D | repo-wide grep + verdict per symbol in the PR body; no live volume write path changes (stop if one does) | code-review; owner sanity look because the file is in the volume domain (no hardware pass: nothing live changes) | deletion |
| 8.7 | docs history pass on the three docs: delete dated/changelog/"used to" sentences (26 dated + the doctrine's deleted-table narrative; the (a)–(i) citation map stays while code still cites it — re-count at HEAD first), fix the dead `captures/...` pointer, no restatement of what another file owns | P | astsame vs merge-base (prose-only); Opus constants review; menu --check 0 | sanity look | small–medium |
| 8.8 | --help style: one metavar `<round-dir>` across round-views/prescriber/null; four descriptions to one sentence; no dest/behaviour change | P | menu regenerates; `--help` diff is metavar/description only | sanity look | tiny |
| 8.9 | fast-lane guards: both boundary tests in `scripts/test-fast`'s always-on list | T | test-fast runs them on an unrelated change | sanity look | tiny |
| 8.10 | record fold: TARGET.md rewritten to HEAD truth (slugs, stderr summaries, field spellings, σ sentence, close_reference per band, position_cycle per 8.4, LinearizationState per 8.3, future tense removed, subcommand order); WAVE-LOG wave-8 fold; LANDING.md table re-verified; #3769 closed | P | every TARGET sentence has a file:line or a row | orchestrator | small |
| owner | #3991 hardware pass then merge (other session merges); #3934 merged (merge of main pushed by this session); one full round walked on the box with the ten binaries after wave 8 lands — the proof static analysis cannot give | NN / hardware | `volume_limit: 0.0` in the emitted config; `inventory` on the walked round names nothing missing | owner | — |

Batching: 8.1a–d + 8.3 + 8.4 + 8.5 + 8.8 + 8.9 in one code batch (file-disjoint lanes: round.py | angle_capture+basic_profile+audition+measure | crossover_prescriber | test file | contracts/baseline_profile | round_bank | round_views delay+distortion | metavars | test-fast).
8.2 and 8.6 in a second batch (both touch the capture/level domain and need the owner's look).
8.7 + 8.10 as the docs close. ~12 PRs, 2–3 CI runs.

## 5. Definition of done

1. Refusals: every one of the ten binaries prints one stdout document `{"status","reason","detail"}`
   whose `status` equals the exit code's word, with `declare-geometry` the one exempt human-only
   door; pinned by the roster test.
2. Artifacts: every file TARGET names after bank / analysis / prescribe / apply is produced by the on-box path, or TARGET stops naming it;
   `inventory` names what a round is missing; no computing verb is stdout-only.
3. Memory: the graded trim decision (`trim_strategy`, `anchor_drift_db`, `committed_side`)
   and the pre-registered expectation round-trip through the round directory, pinned.
4. Capture: one wired capture kernel in the engine; a null run refuses on a dead capture
   the way a measure does.
5. Boundaries: every edge TARGET states has a pin; the pins run in the fast lane;
   the cli→web allowlist holds three names (doctor's) or fewer.
6. Docs: the three docs carry no dated history and no dead pointer; menu `--check` green;
   the methodology names a verb at every step where one is owed, and names the by-design exceptions.
7. Hygiene: zero-production-caller code in the toolbox's domain deleted with verdicts.
8. Hardware: #3991 merged after the pass; one round walked end to end on the box
   with the landed binaries.
9. Record: TARGET matches HEAD sentence for sentence; WAVE-LOG folded; #3769 closed
   pointing at LANDING.md.

Then stop. Any further right-sizing is churn until a hardware round asks for something.

## 6. Not doing, and why

- program_analysis facade repoint (67 importers, 312-line `__init__`): the facade is a pure re-export table;
  every name is defined once in its submodule; repointing 67 files changes nothing observable.
- helper convergence (`_utc_now` ×12, `_sha256` ×10, `_finite_*` ×40): 3-line helpers with differing raise shapes;
  byte-level risk in evidence fingerprints; no LLM effect. PLAN's 3.4 drop holds.
- further god-file splits (56 files > 1,500 lines): the remaining big files are single-concern
  (camilla_yaml, evidence_packet, runtime_contract), safety-adjacent, or the other product; the acceptance
  bar's "no god files" should read "no file with two lifecycles or two owners" — wave 6 split exactly those.
- sound_setup sync→async mux calls: the commissioning wizard, not the toolbox; the bridge already exists
  in web_commissioning; a one-off PR if the owner wants it (five call sites, five test patches), not a program row.
- FaninGateContext ↔ measurement_window convergence: two contracts by design (fail-closed measurement isolation
  vs disclose-and-continue commissioning tone); delete the dead nested mode (8.6) and stop.
- run-crossover-round.py exit codes: a phase vocabulary for a laptop orchestrator, documented, pinned 15×;
  collapsing loses the phase; TARGET's "transport, not second implementation" is already true.
- jasper-null fold into `measure --kind null` or a relocation of its 925 lines: same admission gate;
  TARGET keeps it a binary; the walk is closed; only its capture block is owed (8.2).
- bass_extension_bench --live: parked by ADR-0018 and exempt by ADR-0229; the owner's
  call outside this program.
- σ-kind labelling of the five unlabelled σ producers: the two σ the LLM acts on carry `kind`;
  the rest is machinery; the sentence changes instead.
- closing the doctor's three cli→web edges (`crossover_v2_status_block`, `GRADE_*`, `_systemd`): pinned,
  cannot rot, doctor is operator-facing; no walk benefit.
- reemit report return: taken by wave 7 (7.4); not re-rowed.
- RecordStore-as-the-seam fold (ADR-0227 #12, standing unexecuted): 19 writer functions bypass it and five
  of RecordStore's six routes have no production writer — the ruling describes a seam that was never adopted.
  Recommend the owner re-rule it (an ADR superseding 0227 #12: RecordStore banks take records; direct
  publishers stay) or give it its own program; not a wave-8 row.
- renaming tests/test_arm_walk.py / test_round_bank.py: the engine modules `arm_walk.py` and `round_bank.py`
  still exist under those names; the test names match their subjects.

## 7. Housekeeping this session did

- #3934 merged with `origin/main` and pushed: SHA `07c2b65cec45bb9fdfdbf289ce8ec86ea20e80e5`, no conflicts; `scripts/test-fast` sentinel `==> test-fast: 240 passed`.
- This file, `LANDING.md`.
