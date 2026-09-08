# Brief: lane B — the seat cube is a program (wave 1, rows 1.4, 1.5, 1.7)

You are running the **program** lane of the JTS seat-matched tuning program
(`seat-tuning-program/PLAN.md` on branch `claude/loudspeaker-tuning-architecture-iephfa`;
tracking issue #4502). You extend the measurement-program pose vocabulary, add
the seat-cube and close-spot programs, add three room views over a banked
round, and write the runbook's "Room" section. Three PRs, one per row, each
from a fresh `origin/main` branch `claude/seat-w1-<row>-<slug>`. No hardware
session is yours (row 1.6 is the owner's).

## 0. Read first

`AGENTS.md`; the plan §1, §2, §7; ADR-0255…0258 and, once merged, ADR-0259 and
ADR-0260 (if not on main yet, proceed on the plan's §2 wording and cite
"ADR-0260 (Wave 0b)"); `docs/measurement-loop-doctrine.md` §1a;
`docs/tuning-methodology.md` §0–§2; `jasper/cli/round_views/repeat.py` end to
end (your view template). Every `file:line` below was verified at main
`f84d7da` (2026-09-08); re-verify at your HEAD and stop a row whose premise is
false.

## 1. Facts (verified at main `f84d7da`)

**Programs.** `ProgramPose(azimuth_deg: int, elevation_deg: int, repeats: int = 1)`
(`jasper/active_speaker/measurement_programs.py:39-44`, frozen);
`MeasurementProgram(program_id, size, poses)` (`:48-53`) with `mic_move_count`
(distinct bearings — the property is **not** called `pose_count`) and
`capture_count` (`:62-64`); `ANCHOR_REPEATS = 4` (`:35`). Registry `_PROGRAMS`
(`:121-129`): `baseline/full` 13 poses / 16 captures, `baseline/express` 5 / 8,
`tournament/full` 3 / 3, `tournament/express` 1 / 1. Queries:
`available_programs()` (`:132`), `program(id, size)` (`:142`, raises
`UnknownProgramError`), `spot_program(azimuth_deg, elevation_deg)` (`:151`,
deliberately not registered). Header (`:22-25`): "No level and no mark
distance. Every program measures at the walk's own `MARK_DISTANCE_M`, driven at
the banked seat-level anchor's own SPL."

**Where a pose flows.** The seam `jasper/active_speaker/angle_capture.py`:
`pose_at_angle(angle_deg, elevation_deg=0) -> CloudPositionPrompt` (~`:304`),
`_offset_cm_at` uses `MARK_DISTANCE_M` (`:358`), `request_for_program(program,
*, candidates=(), mover=...)` expands `azimuth_deg/elevation_deg/repeats`
pose-major. The CLI is `jasper/cli/angle_capture.py`: `plan/stage/show/withdraw/serve`
(`:758-768, 976-1023`), `--mover` ∈ {human, arm}, `handoff_url =
speaker_url(CROSSOVER_PAGE_PATH)` (`:344`). The web `position-ready` handler
(`jasper/web/correction_handlers.py:1369`) releases `{index, attempt}` only.
The prompt copy is `remote_position_prompt`
(`jasper/active_speaker/crossover_v2/capture_plan.py:485`) and hard-codes
`MARK_DISTANCE_M` ("On the mark, {M:g} m out…"). Readers of the pose fields:
`crossover_v2/round_captures.py:64-67` (`PoseCapture`; `pose_key` already keys
on the (azimuth, elevation, distance) triple), `crossover_v2/spatial.py:947-950`
(take record: `position_deg`, `position_axis`, `vertical_deg`, `mark_distance_m`),
`feature_classifier.py:1544`, `gate_sweep.py:1117`, `frequency_view.py:185`.
`MARK_DISTANCE_M = 1.0` is defined at `crossover_v2/spatial.py:1393` and
re-exported at `capture_plan.py:402` and `crossover_v2_flow.py:184`;
`round_views/close_reference.py:22-24,217` notes the sidecar's
`mark_distance_m` is published but pinned to 1.0 for every pose (#3498). So
distance already travels end to end; making it vary is a producer-side change.

**Windows.** `gating.gate_impulse_response(ir, sample_rate, ...)` (`jasper/audio_measurement/gating.py:583`)
and `exempt_gating_block(ir, sample_rate, *, reason)` (`:722`, records
`applied=False` with a reason; used for near-field at `driver_acoustics.py:442`).
`_driver_response` always gates (`program_analysis/response.py:260-327`);
`DriverResponse.gating` (`program_analysis/model.py:629-642`). **No `gate=None`
path exists.** `gate_sweep.py` re-reads raw WAVs independently (rungs `:71,1057,1086`).

**Reading a round.** Layout in `jasper/active_speaker/round_bank.py:10-20`
(`DEFAULT_CAMPAIGN_ROOT` `:62`); `record_index.bundle_measurements(bundle_dir,
*, kind, phase, position_deg, vertical_deg, candidate_id)` (`:127-153`,
`Measurement` `:33-45`); `position_cycle.read_pose_curve_pair(bundle_dir, *,
phase, position_deg, vertical_deg=0, roles)` (`:310-360`) and
`parse_curve_complex(curve) -> (freqs, tf, band)` (`:365-397`);
`pose_curve_record(curve)` lives in `crossover_v2/spatial.py:979-1017` with
shape `{role, band_hz, freqs_hz, magnitude_db, phase_deg, validity_floor_hz,
repeat_curves}`. `spatial_combine.combine_positions(captures, *, flag_threshold_db,
diag_fraction, spec_fraction, echo_band_hz, echo_search_us, signal_band_hz=None)
-> CombinedResponse` (`:1442-1454`) computes a median at `:1528` as its
disagreement screen; `analysis.spatial_average_db(magnitudes_db)` (`:251`) is
the power mean.

**The applied floor.** `state_paths.baseline_profile_state_path(path=None)`
defaults to `/var/lib/jasper/active_speaker_baseline_profile.json` (`:35-38`).
The candidate's `exclusion_evidence` (`measured_crossover_candidate.py:267,393-394,412`)
is built by `exclusion_evidence_json(cloud, *, cloud_result)`
(`crossover_v2/planning.py:273-320`) with keys `validity_floor_hz` (documented
as the trusted floor; `None` ≠ 0 Hz) and `gated_spec_curve {freqs_hz,
magnitude_db}`. Verify which floor the key carries: if it is the `1/T` floor,
the trusted floor is `gating.TRUSTED_FLOOR_MULTIPLIER` (2.5) times it.
`room_boundary.py`: `GATED_SPEC_LOWER_EDGE_HZ=250` (`:111`),
`ROOM_BOUNDARY_DEFAULT_HZ=350` (`:115`), `MIN=250`, `MAX=500` (`:121-122`).

**Views.** `ARTIFACT_BY_VIEW: dict[str, ViewArtifact(artifact, takes,
in_artifact_dir, producer)]` (`jasper/cli/round_views/_common.py:77-124`);
`AUTHORITY_TIER` (`:43-44`); families wired in `round_views/__init__.py:101-104`
(`_FAMILIES`) and `:118-121` (`add_parser(sub)`); exit vocabulary in
`jasper/cli/_refusal.py:35-41,45,69,79,96,104,112` (`EXIT_OK/REFUSED/UNREADABLE/WRITE_FAILED`,
`STATUS_BY_CODE`, `answered/refused/failed`). Template: `repeat.py:59-73,132-155`
(`_cmd_repeat`, `_load_round`, `_write`, `answer`, `add_parser`). The menu
generator already lists `jasper.cli.round_views` (`scripts/generate-tuning-tool-menu.py:48-59`;
`--check` `:121-135`), so new subcommands appear on regenerate.
`tests/test_cli_exit_vocabulary.py:213-214,237-260` maps every subcommand in
`_VIEW_RUN` — **a new subcommand without an entry raises KeyError**; the test
asserts `EXIT_OK`, no `status` key, arrays ≤ 16 (`MAX_ANSWER_ARRAY` `:211`),
artifact exists. Fixture builders: `tests/crossover_v2_banked_round.py:269-345`
(`bank_measure_round`), `:358-403` (`bank_verify_round`);
`tests/crossover_v2_fixtures.py:2058` (`bank_capture_round`).

**Docs.** Runbook headings (`docs/tuning-operator-runbook.md`): Entry contract
(3), One possible flow (31), Candidate batches (78), Evidence/recovery (131),
The tool menu (160; generated block 167-183), Find the analysis (185), URLs
(204), Debugging (217). Methodology sections 0–10 (`:10-218`); Wave 0b adds a
"Room" stub you replace.

## 2. Rows

### 1.4 — Pose vocabulary and two programs (branch `claude/seat-w1-1-4-seat-cube-program`)

1. `ProgramPose` gains `kind: str = "bearing"` (∈ {bearing, seat, close}),
   `distance_m: float | None = None` (None = the walk's mark distance, so every
   existing program is byte-identical), and `seat_offset_m: tuple[float, float,
   float] | None = None` (right, forward, up from the head centre; seat kind
   only). `mic_move_count` counts distinct (kind, bearing, distance, offset).
2. Register `seat/cube` (7 poses: head centre, ±0.30 m right, ±0.30 m forward,
   ±0.30 m up; `repeats=1`; also `seat/express` with head, +right, +forward)
   and `close/spot` (one pose, `kind=close`, `distance_m=0.30`, design axis).
   The seat programs use the **summed** stimulus (the VERIFY shape, mono sweep
   through the applied graph, `speaker_tune` scope), not the per-driver
   interleave; say so in the program's docstring and the request.
3. Producer changes only where a reader exists: `request_for_program` carries
   the new fields; `remote_position_prompt` renders per kind ("head centre at
   the listening position, ear height", "30 cm to the right of the head", …,
   and for close "0.3 m from the baffle on the design axis"); the take record
   (`spatial.py:947-950`) gains `pose_kind` and `seat_offset_m`; `PoseCapture`
   / `pose_key` include kind and offset so seat poses are distinct; the
   `arm` mover refuses `seat` and `close` kinds with a code (a turntable cannot
   reach the seat).
4. **Window by kind.** At analysis, a `seat`-kind take is analyzed ungated via
   `exempt_gating_block(..., reason="seat")`; `bearing` and `close` stay gated.
   This is the smallest change that gives an ungated path: it reuses the
   near-field exemption's mechanism, records `gating.applied=False` with its
   reason, and touches `_driver_response`'s caller only where it chooses the
   window. Cite doctrine §1a and ADR-0260 in the one comment you add.
5. `MARK_DISTANCE_M` stays as the bearing default; do not delete it in this
   lane (its readers are many); note in the PR body that it is now a default,
   not a pin.

Proof: `jasper-angle-capture plan` lists `seat/cube`, `seat/express`,
`close/spot` with their costs; a fixture round banked from `seat/cube` carries
seven takes with `pose_kind="seat"`, distinct `pose_key`s, and `gating.applied
== False`; `baseline/full` fixtures are byte-identical before and after
(pin it); `arm` + `seat` refuses with a code. Gate: `/code-review` medium; the
analysis window change is not a clamp, but re-run the gating test files.

### 1.5 — Three room views (branch `claude/seat-w1-1-5-room-views`)

New module `jasper/cli/round_views/room.py`, added to `_FAMILIES`, three
`ARTIFACT_BY_VIEW` rows, three `_VIEW_RUN` entries, stdout per ADR-0237:

- `room-median <round-dir>` → `room_median.json`: over the round's seat-kind
  takes, per frequency the **median** magnitude across positions and the
  spread (population σ), plus each position's deviation from the median, over
  20 Hz to the ceiling; `n_positions`; `window: "ungated"`; `ceiling_hz` and
  `ceiling_source` from the same logic as `room-ceiling`. Exact shape (this is
  lane C's input contract, keep it): `{"freqs_hz": [...], "median_db": [...],
  "spread_db": [...], "n_positions": int, "positions": [{"id": str,
  "deviation_db": [...]}], "ceiling_hz": float, "ceiling_source":
  "applied_candidate" | "fallback", "window": "ungated"}`. Answer on stdout:
  ceiling, n_positions, mean spread in three bands (20–60, 60–120, 120–ceiling),
  the path.
- `room-persistence <round-dir>` → `room_persistence.json`: features of the
  per-position curves versus the median (peaks and dips ≥ 3 dB, width ≥ 1/6
  octave), each with `presence_fraction` (positions where it appears with
  depth agreeing within 3 dB), `kind` (peak/dip), centre, width, median depth.
  Answer: the count of features at ≥ 0.7 presence and the top few by depth.
- `room-ceiling` → `room_ceiling.json`: read the applied profile at
  `baseline_profile_state_path()`, take the trusted floor from
  `exclusion_evidence` (see §1 on which floor the key carries), clamp to
  `[ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ]`; on a missing or unreadable
  profile fall back to `ROOM_BOUNDARY_DEFAULT_HZ` with
  `ceiling_source: "fallback"` and the reason. Pure read; no import of
  `jasper.correction`.

Use `spatial_combine`'s median where it fits; do not build a second combiner
if `combine_positions` can be called with a signal band and its median read.
If it cannot without dragging in the echo screen, a 10-line `np.median` over
the stacked curves in `room.py` is acceptable — say which in the PR body.

Proof: fixture round from 1.4 (or hand-built seat takes) → all three artifacts
written and listed by `inventory`; `test_cli_exit_vocabulary.py` green with the
three new entries; a missing applied profile yields the disclosed fallback.
Gate: `/code-review` medium.

### 1.7 — Runbook and methodology (branch `claude/seat-w1-1-7-room-docs`)

Replace the methodology "Room" stub (from Wave 0b) with one section: the seat
cube as the room's measurement, the ceiling as the seam, the three views and
what each answers, what is deliberately not done above the ceiling. Add a
runbook "Room" section after "Candidate batches" naming the verbs in order:
`jasper-angle-capture plan --program seat/cube` → walk → `jasper-round bank` →
`room-ceiling` → `room-median` → `room-persistence` → (lane C's door, when it
lands). Regenerate the menu. Proof: `generate-tuning-tool-menu.py --check`;
`docs-linkcheck.py --all`. Gate: sanity look.

## 3. Rules that bind this lane

- Standing rules in the kickoff snippet and plan §7.
- Do not touch `jasper/correction/` (lane A), the candidate model or doors
  (lane C), or `jasper/bass_extension/` (lane D).
- Additive only in the engine: new fields default to today's behavior; every
  existing program's plan, prompts and take records stay byte-identical, and a
  test pins it.
- No new `JASPER_*` knob; the 0.30 m offsets and distance are program
  constants with a `See ADR-0260` pointer.

## 4. Report back

Per PR: link, line delta, the new registry rows and `ARTIFACT_BY_VIEW` rows,
which floor key `exclusion_evidence` carried and how the ceiling was derived,
validation sentinels, "stale, not fixed here", and any premise in §1 found
false at HEAD with what you did instead.
