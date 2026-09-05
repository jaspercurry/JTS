# The tuning toolbox — shape at HEAD (2026-09-05)

One page a new LLM reads to know the whole toolbox. Every `file:line` was opened in the tree
carrying wave 9 batches 1, 2a and 2b.

## The tools (ten binaries, one verb family each)

| Binary | Question it answers | Artifact it writes | Authority |
|---|---|---|---|
| `jasper-declare-geometry set\|show` | What rig am I measuring on? | `/var/lib/jasper/measurement_geometry.json` (`audio_measurement/measurement_geometry.py:25`), frozen per round as `declared-geometry.json` (`crossover_v2/round_inputs.py:64`) | advisory (`set` writes) |
| `jasper-seat-level` | What volume is the measurement reference? | `/var/lib/jasper/active_speaker_seat_level_reference.json` (`active_speaker/seat_level_reference.py:47`) | measured |
| `jasper-angle-capture plan\|stage\|show\|withdraw\|serve` | Which poses will the next round walk, who moves the mic (`--mover human\|arm`, `active_speaker/angle_capture.py:129`), and — `show` — what is staged now? | the staged walk and its `handoff_url` (`cli/angle_capture.py:350`) | mutating (`stage`/`serve`) |
| `jasper-measure` | Measure this speaker once at one placement | banked takes with ids | measured |
| `jasper-null` | What cancels at each delay coordinate, played for real? | a row per coordinate under `<bundle>/null_runs/` (`cli/null_door.py:35`) | measured |
| `jasper-round open\|wait\|apply\|bank` | Open, wait on, apply and bank one round from the box | the banked round tree (`active_speaker/round_bank.py:14-23`) | mutating-with-gates |
| `jasper-crossover-prescriber status\|packet\|propose\|stage` | Where does this speaker stand, which candidates are banked (`cli/crossover_prescriber.py:748`), is my prescription admissible? | `packet.json` (`:217`), `proposal_receipt.json` (`:345`), the staged slot | advisory (`stage` mutates) |
| `jasper-round-views <view>` | One question about a banked round, answered small — incl. `candidates` (configs at one held pose), `findings` (three attribution phases), `close-reference --at-hz` | one named JSON per view, the twenty rows of `ARTIFACT_BY_VIEW` (`round_views/_common.py:91-123`) | advisory |
| `jasper-basic-profile review\|apply` | Put the declared crossover on, nothing else | live tune | mutating-with-gates |
| `jasper-audition start\|stop\|status` | What does the correction layer sound like, by ear? | runtime only | mutating (runtime) |

Retired into these ten: `jasper-arm-walk` → `angle-capture serve`; `jasper-round-bank` → `round
bank`; `delay-sweep`, `gate-sweep`, `classify-features`, `read-distortion`, `close-reference`,
`forward-model`, `project-ring` → `round-views` views. `jasper-active-speaker` is a commissioning
and deploy binary, off the menu by design (`scripts/generate-tuning-tool-menu.py:55-65`);
`scripts/run-crossover-round.py` is a laptop transport for three `jasper-round` verbs
(`cli/round.py:305-308`).

## Conventions every tool obeys

- **stdout is the answer** (ADR-0237). Success prints one JSON document — the scalars its human
  line reports, plus `out`/`bytes` for an artifact and `next` for what to run next; the human line
  goes to stderr (`cli/_refusal.py:69-76`; `round_views/_common.py:195-210`). No numeric array
  over 16; curves stay in the file. `--out -` is depth on demand: the artifact IS the document.
- **One failure shape**: stdout `{"status", "reason", "detail"}`, one sentence on stderr, `status
  == STATUS_BY_CODE[code]` over one vocabulary — 0 ok, 1 refused, 2 unreadable, 3 unwritable
  (`cli/_refusal.py:35-49`, `:79-99`). Everything else rides under `detail`; a run that stopped
  early is a refusal, not a fourth status; a success document has no `status`; no `--json` gate
  survives. Exempt: `declare-geometry`'s numbering (`:31-33`) and argparse errors. Pinned by
  invoking each `main()` and each view (`tests/test_cli_exit_vocabulary.py:182-199`, `:288-313`).
- **One artifact rule**: what computes writes one named JSON beside the round, `ARTIFACT_BY_VIEW`
  names it, `--out` overrides (`round_views/_common.py:236-262`); `inventory` names what is
  missing, with sizes, and each absentee's producer as a runnable line
  (`round_views/inventory.py:45-54`, `:76-90`). Every verb sets `prog`, `AUTHORITY_TIER` and the
  positionals `<round-dir>`/`<bundle-dir>` (`round_views/_common.py:50-51`); the menu is
  generated.

## The round directory as memory

`jasper-round bank` files the copied bundle, the five SSOT siblings
(`crossover_v2/round_inputs.py:59-64`), `position_cycle.json` and `provenance.json`
(`active_speaker/round_bank.py:14-23`); the pose index is derived from the bundle on the box,
best-effort, and a round that could not take one says so in `provenance.json`'s `missing`
(`:171-190`). Inside the bundle the record store files each take, the candidate, the receipt and
each attribution phase (`crossover_v2/record_store.py:108-142`); a take row carries `phase`,
`candidate_id`, `captured_at` (`crossover_v2/record_index.py:33-44`), `phase_composition`
(`crossover_v2/position_cycle.py:167-182`). Then the per-view artifacts, `packet.json`,
`proposal_receipt.json`. Session-crossing facts live in those files, not in the LLM's context:
`expected_delta_db` and `declared_tilt_db_per_octave` ride inside `candidate.json`
(`crossover_v2/driver_prescription.py:149-150`, `:1488-1499`), which `round-views frozen` reads
back beside the two measurements it compares — the target round on its own reference levels and on
the baseline's (`crossover_v2/round_views.py:721-773`); `trim_decision` (`{strategy,
committed_side, anchor_drift_db}`, empty where none was committed) says which trim pair the levels
came from, on the candidate (`active_speaker/measured_crossover_candidate.py:265`, `:410`) and
profile (`active_speaker/baseline_profile.py:2939`).

## Boundaries, as an import rule

`PACKAGE_BOUNDARIES` (`tests/test_correction_boundary_ssot.py:264-285`) pins that
`audio_measurement` imports no `correction`, `active_speaker` or `cli`; `active_speaker` no
`correction`, `web` or `cli`; `cli` no `web`; `correction` no `active_speaker`. `audio_measurement
↛ web` is pinned by `tests/test_runtime_import_closure.py:28-34`'s FORBIDDEN list. Six edges are
allowlisted with an argument each (`:159-187`): `correction/runtime_safety.py` reads the declared
driver caps, `cli/doctor/correction.py` takes four web adapters, and `cli/measure.py` binds
`WiredStimulusCapture` — which stays even after the kernel moves down: the household-mic hint it
mints through lives in `web/correction_setup.py`, and lifting it out is its own row.

## Out of scope by design

Room correction (`jasper/correction/`, the PEQ wizard, `jasper-correction-bundle`,
`jasper-calibration-agent`), the benches (`jasper-active-speaker-emit-bench`,
`jasper-bass-extension-bench`, `jasper/active_speaker/bench/`), the voice stack,
`jasper-active-speaker`. The methodology attributes room effects there and prescribes none.
