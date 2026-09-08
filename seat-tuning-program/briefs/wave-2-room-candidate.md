# Brief: lane C — the Layer-3 room candidate kind (wave 2, rows 2.1–2.3)

You are running the **room candidate** lane of the JTS seat-matched tuning
program (`seat-tuning-program/PLAN.md` on branch
`claude/loudspeaker-tuning-architecture-iephfa`; tracking issue #4502). You add
a room-correction **candidate kind** to the speaker-tuning engine, a **grade
view**, and a **boundary-prior view**. Three PRs, one per row, each from a fresh
`origin/main` branch `claude/seat-w2-<row>-<slug>`. Work on fixtures; no hardware
session is yours (row 2.4 is the owner's).

## 0. Read first

`AGENTS.md`; the plan §1, §2, §7; ADR-0255…0258 and, once merged, ADR-0259
(one toolbox) and ADR-0260 (flexible poses; seat cube) — if 0259/0260 are not
on main yet, proceed on the plan's §2 wording and cite "ADR-0259 (Wave 0b)";
`docs/measurement-loop-doctrine.md` §1a; `docs/room-correction-regime-plan.md`
D1, D2, D5, D7 (the acoustic proposal this lane implements — D5 in particular is
the boost-admission proposal you are adopting, with a room-specific threshold);
the blend prescription door end to end
(`jasper/active_speaker/crossover_v2/blend_prescription.py`), which is your
template. Every `file:line` below was verified at main `f84d7da` (2026-09-08);
re-verify at your HEAD and stop a row whose premise is false.

## 1. Facts (verified at main `f84d7da`)

**The candidate model.** `MeasuredCrossoverCandidate`
(`jasper/active_speaker/measured_crossover_candidate.py:216-269`) already carries
`blend_correction`: a flat pre-split program-bus PEQ list scoped to the
crossover blend region. Optional fields are declared once in
`_OPTIONAL_FIELD_TYPES` (`:107-113`), which drives absence tolerance,
unknown-field refusal and the reopen comparison; `__post_init__` (`:271-367`)
freezes and validates; `_core()` (`:369-397`) fingerprints, omitting empty
optionals; `from_mapping` (`:423-518`) reopens with a tamper check. The bank
(`candidate_bank.py:117,148-181,196-263`) looks up by fingerprint only.
`require_candidate_trial` (`candidate_trials.py:26-73`) demands an intact
`graph_scope="candidate"` capture before an authored candidate may apply.

**The doors.** `blend_prescription.py` is the closest template: an LLM-authored
document with `read_blend_prescription` (`:1016`), `prescription_route`
(`:1107`), `blend_prescription_to_candidate_fields` (`:1147`), `_check_bounds`
(`:774`), `_check_composed` (`:832`), `positional_support` (`:564`); constants
`PRESCRIPTION_MAX_FILTER_BOOST_DB=3.0`, `PRESCRIPTION_MAX_TOTAL_BOOST_DB=4.0`,
`BOOST_MIN_TESTIFYING_POSITIONS=3`, `BOOST_MAX_DISSENTING_POSITIONS=1`
(`:161-183`); boosts are refused at the last gate (`BOOST_ROUTE_UNAVAILABLE`,
`:203-221`). The doors' convention: bounds with a source are RESTATED, not
imported. `jasper/cli/crossover_prescriber.py` `_gate` (`:322-373`) dispatches
on the document's `kind`; `propose`/`stage`/`status`/`packet` parsers at
`:1417,1424,1448,1470`.

**Apply and emit.** `build_baseline_profile_candidate`
(`jasper/active_speaker/baseline_profile.py:2025-2029`) does **not** accept room
PEQs; `recompose_applied_baseline_yaml` (`:3087-3097`) does
(`room_peqs: Sequence[PeqFilter] = ()`) but only on top of an already-applied
baseline. The emitter already has the stage: `room_peqs` in
`_emit_baseline_filter_definitions` and `emit_active_speaker_baseline_config`
(`jasper/active_speaker/camilla_yaml.py:1790-1809,3495,3509`), named by
`_room_peq_name` (`:869`), wired pre-split on `channels: [0, 1]` (`:1927-1931`),
and headroom-charged by `active_baseline_headroom`, which folds
`total_positive_boost_db(room_peqs)` with linearization headroom into one
pre-split gain (`:1836-1856`). `PeqFilter` is `freq, q, gain`, no `type`
(`jasper/camilla_config_contract.py:208-213`); `total_positive_boost_db`
(`:216-229`). The round-trip reader `extract_room_peqs_from_config_text`
(`jasper/sound/camilla_yaml.py:1012-1063`) is peaking-only and ignores
right-channel names.

**The designer.** `design_peq` (`jasper/audio_measurement/peq.py:105-119`):
`f_low=20.0, f_high=ROOM_BOUNDARY_DEFAULT_HZ, max_filters=5,
max_cut_db: float | np.ndarray = -10.0` (per-bin array supported),
`max_boost_db: float = 3.0` (scalar only), `cuts_only=True`, Q in [1.0, 8.0];
`predicted_response` (`:223-232`).

**Room math that must move (boundary).** `jasper/active_speaker` may not import
`jasper.correction` (`tests/test_correction_boundary_ssot.py:291-310`). The
pieces you need live there today: `variance_cap.py:53-59,196-266`
(`allowed_depth_db = base_max_cut_db * min(1, TOLERABLE/max(sigma, eps))`,
`plan_depth_cap` returns the per-bin array `design_peq` takes), `target.py`
(`flat_target` :19, `harman_target` :24, `house_curve` :48), `spatial.py`
(`HIGH_CONFIDENCE_STD_DB=4.0`, `MEDIUM_CONFIDENCE_STD_DB=6.0`,
`MIN_POSITIONS_FOR_HIGH=3`, `:21-35`). Leaf helpers already in place:
`analysis.spatial_average_db` (`:251-266`), `deviation_metrics` (`:270-296`),
`room_boundary.py` (`GATED_SPEC_LOWER_EDGE_HZ=250`, `ROOM_BOUNDARY_DEFAULT_HZ=350`,
`MIN/MAX=250/500`, `:97-106`). No `LF_BOOST_PRESENCE_FRACTION` exists.
`interference_nulls.POSITION_PRESENCE_FRACTION=0.70` (`:269`) is the HF gated
instrument's constant — do not borrow it (the regime plan says so).

**Grading shape.** `flat_spec.SPEC_BANDS` and `evaluate_flat_spec`
(`jasper/active_speaker/flat_spec.py:36-39,354-365`, `graded_lo_hz`/`graded_hi_hz`
`:102-103`); the `frozen` view (`jasper/cli/round_views/grades.py:83-92` →
`crossover_v2/round_views.py:721-763`) is role-keyed, so the room grade needs
its own band-keyed shape.

**Layering.** `compile_tuning_graph` (`jasper/active_speaker/measurement_emit.py:71-81`)
composes the temporary graph per scope; `GRAPH_SCOPES=("drivers","base",
"speaker_tune","candidate")` (`crossover_v2/measure_spec.py:123-124`); dispatch
in `session_graph.py:161-181`. **None of the scopes includes room PEQ**: the
docstring says "None includes room, preference or bass-extension processing".

**Geometry.** `DeclaredGeometry` has `speaker_height_m, mic_height_m,
distance_m, ceiling_height_m` (`jasper/audio_measurement/measurement_geometry.py:68-79`);
`jasper-declare-geometry set` accepts exactly those (`jasper/cli/declare_geometry.py:81-90`);
frozen per round as `declared-geometry.json` (`crossover_v2/round_inputs.py:70,116,173,185`).
**No wall distances exist.**

**Artifact contract with lane B.** Lane B's `room-median` view writes
`room_median.json` beside the round: `{"freqs_hz": [...], "median_db": [...],
"spread_db": [...], "n_positions": int, "positions": [{"id", "pose_key", "deviation_db": [...]}],
"ceiling_hz": float, "ceiling_source": "applied_candidate" | "fallback",
"window": "ungated" | "gated" | "mixed"}`. **As landed (PR #4524,
`jasper/active_speaker/crossover_v2/room_views.py`):** `window` is computed from
the takes' `gating_applied`, not pinned; your door refuses anything but
`"ungated"` with a code. `pose_key` is the only distinguisher between the seven
seat poses (all at bearing 0°); do not exact-match the key set. Read the numbers
in-process from `room_views.py` where you can rather than re-parsing the file.
Do not build a second median.

## 2. Rows

### 2.1 — The Layer-3 candidate kind and its door (branch `claude/seat-w2-2-1-room-candidate`)

1. **Move the room math to the leaf, first commit.** New module
   `jasper/audio_measurement/room_limits.py`: the depth-cap rule from
   `variance_cap.py` (formula and constants, with their `See ADR-0256` pointer),
   the target functions from `correction/target.py` (`flat_target`,
   `harman_target`, `house_curve`), and the **taper**: given `ceiling_hz`, a
   per-bin multiplier that goes from 1 at `ceiling_hz / 2^(1/3)` to 0 at
   `ceiling_hz` (linear in dB over log-frequency), applied to both the cut and
   boost arrays. Leave `jasper/correction/variance_cap.py` and `target.py` in
   place as thin re-exports or untouched — lane A deletes them after this lands;
   say so in the PR body. `design_peq` gains a per-bin `max_boost_db` array
   mirroring `max_cut_db` (the regime plan's D5 insertion point); scalar still
   accepted; no behavior change for existing callers (pin it).
2. **Boost admission**, in `room_limits.py`, adopting D5 with room-specific
   constants: a dip is boostable only when present with agreeing depth in
   ≥ `ROOM_BOOST_PRESENCE_MIN_FRACTION = 0.7` of positions **and** `n_positions ≥ 3`
   (7-position cube → ≥ 5 of 7; 3 → 3 of 3; below 3 refused outright), its
   width is at least 1/6 octave and its depth ≤ 10 dB (modally plausible — a
   deep narrow null is refused), and the boost is capped at +6 dB per admitted
   dip and +6 dB total positive boost; the cost is the total positive boost in
   dB of maximum level, disclosed. Everything else stays cuts-only. Constants
   restated with a `See docs/room-correction-regime-plan.md D5` pointer.
3. **The candidate field.** `room_correction` on `MeasuredCrossoverCandidate`:
   `{ "sides": { "<side>": [ {freq, q, gain}, ... ] }, "ceiling_hz", "ceiling_source",
   "basis": {"round_id", "room_median_sha256"}, "boost_db_total", "level_cost_db" }`
   with a mono layout using the single side name the profile declares. Add it to
   `_OPTIONAL_FIELD_TYPES`; validate in `__post_init__` (cuts negative, Q in
   [1, 8], freq inside [20, ceiling], boosts only where admitted per the basis);
   fingerprinted through `_core()`.
4. **The door.** `jasper/active_speaker/crossover_v2/room_prescription.py`,
   modeled line for line on `blend_prescription.py`'s shape: `read_room_prescription`,
   `room_prescription_route`, `room_prescription_to_candidate_fields`,
   `_check_bounds` (the limits above, computed from the cited `room_median.json`),
   `_check_composed` (≤ 8 filters per side, total boost cap, taper respected),
   refusal codes on the shared `_refusal.py` vocabulary. Add the `kind` branch in
   `crossover_prescriber._gate`. The LLM authors the filters; the door validates
   — it never designs. (`design_peq` remains available to the LLM as a
   `room-suggest` view if you find it earns its keep; optional, not required.)
5. **Apply.** `build_baseline_profile_candidate` threads
   `measured_candidate.room_correction` into the emitter's existing `room_peqs`
   (mono: the one side's list). `recompose_applied_baseline_yaml`'s `room_peqs`
   parameter keeps working for its multiroom callers. `require_candidate_trial`
   applies unchanged: a room candidate is unmeasured until its graph has played.
6. **Scope.** Add `"room_candidate"` to `GRAPH_SCOPES` and `compile_tuning_graph`:
   the accepted speaker tune plus the named candidate's room PEQs, excluding
   preference and bass. This is the one engine touch in this lane — keep it to
   the scope table and the compose function, cite `measurement-loop-doctrine.md`
   §1a, and note it in the PR body as a coordination item with the tuning-flow
   agent.

Proof: the door refuses out-of-limit filters with codes (parametrized over: a
boost with insufficient presence, a boost into a narrow deep null, a filter
above the ceiling, total boost over cap, Q out of range); a fixture candidate
applies through a fake Camilla and round-trips through
`extract_room_peqs_from_config_text`; `design_peq` per-bin boost array pinned
with scalar parity; boundary tests green; `python3 scripts/generate-tuning-tool-menu.py --check`.
Gate: `/code-review` high; the emitter threading touches DSP math on the output
path, so `/adversarial-review` on that commit as well; no hardware pass needed
(no new clamp, the headroom trim already charges room boosts).

### 2.2 — `room-grade` view (branch `claude/seat-w2-2-2-room-grade`)

A `jasper-round-views room-grade` verb: given a round captured under
`room_candidate` scope (or `speaker_tune` for the incumbent), read
`room_median.json`, grade the median against the room target below
`ceiling_hz` in the plan's bands (20–ceiling split at 60 and 120 Hz, or one
band if the ceiling is low), report per band: RMS deviation, max deviation,
spread, and the incumbent's same numbers beside it when a baseline round is
named. Regression is a **disclosure** in the answer, never a verdict; restore
is the doctrine's existing path. Artifact `room_grade.json` via
`ARTIFACT_BY_VIEW`; stdout per ADR-0237 (no arrays over 16). Proof: fixture
rounds; `tests/test_cli_exit_vocabulary.py` green. Gate: `/code-review`.

### 2.3 — `boundary-prior` view and wall distances (branch `claude/seat-w2-2-3-boundary-prior`)

1. `DeclaredGeometry` gains optional `front_wall_m` and `side_wall_m` (nearest
   side wall), `jasper-declare-geometry set` gains `--front-wall` / `--side-wall`;
   absent stays unknown (never zero); `declared-geometry.json` carries them.
2. `jasper-round-views boundary-prior`: from the declared distances predict,
   per wall, the quarter-wave null `f_null = c / (4 d)` and the transition from
   4π to 2π loading (gain rising toward +6 dB below `c / (4 d)` for one wall),
   summed across declared walls as a rough dB-versus-frequency prior over
   20 Hz–ceiling; advisory only, labeled as a model. Use the round's own speed
   of sound where banked. Artifact `boundary_prior.json`. Proof: `d = 0.85 m`
   gives `f_null ≈ 101 Hz`; missing geometry → a disclosed `unknown`, not a
   refusal. Gate: `/code-review`.

## 3. Rules that bind this lane

- Standing rules in the kickoff snippet and plan §7: delegate (Sonnet reads and
  verifies, Opus implements), `scripts/test-fast` then `/simplify` then
  `/code-review` before every push, one concern per PR, deletions with verdicts,
  no model identifiers, PR body with line delta and validation sentinels.
- Boundaries are the contract: new pure math goes in `jasper/audio_measurement`;
  the door and candidate field in `jasper/active_speaker`; nothing imports
  `jasper.correction`. Run `tests/test_correction_boundary_ssot.py`.
- Restate bounds with a pointer; never import a constant from another
  instrument. Never a new `JASPER_*` knob, daemon, database or doc tier.
- The LLM judges; code computes limits and protects. No auto-revert machinery.
- Per-side is data now, emission later (wave 6): the candidate carries sides;
  the emitter uses the one side a mono layout declares.
- Do not touch `jasper/correction/` beyond leaving it importable; lane A owns
  its deletion. Do not touch `measurement_programs.py` or the round views lane
  B owns (`room-median`, `room-persistence`, `room-ceiling`).

## 4. Report back

Per PR: link, line delta, the refusal codes added, the artifacts added to
`ARTIFACT_BY_VIEW`, validation sentinels, "stale, not fixed here", and any
premise in §1 found false at HEAD with what you did instead. Flag explicitly if
the `room_candidate` scope needed more than the scope table and the compose
function.
