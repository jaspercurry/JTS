# ADR-0259: Room correction and bass extension are layers of the one tuning toolbox

- **Date:** 2026-09-08
- **Status:** Accepted. Supersedes (partial)
  [ADR-0231](0231-four-rulings-that-lived-only-in-code-comments.md) §5, the
  D11 boundary note; §§1–4 stand. Amends
  [ADR-0257](0257-bass-extension-resumes-rebased-on-wired-capture-and-validated-in-room-below-the-ceiling.md) §1.
- Refs: `seat-tuning-program/PLAN.md` §1–§4 on branch
  `claude/loudspeaker-tuning-architecture-iephfa` (the plan of record, never
  merged); tracking issue #4502; Bank, AES 134 (2013).

## Context

The measure → analyze → propose → apply → re-measure loop exists once, in the
speaker engine: clouds and their combination
(`jasper/active_speaker/crossover_v2/spatial.py`: `cloud_position_screens`
:219, `cloud_position_record` :860, `cloud_geometry_verdict` :1574,
`cloud_validity_floor_hz` :2062, `cloud_trusted_floor_hz` :2083;
`jasper/audio_measurement/spatial_combine.py:1442` `combine_positions`), a
programs registry (`jasper/active_speaker/measurement_programs.py`:
`ProgramPose` :39, `MeasurementProgram` :48, `_PROGRAMS` :121, `spot_program`
:151), the layering rule of `docs/measurement-loop-doctrine.md` §1a enforced
in graph composition (`crossover_v2/session_graph.py:151-186` composing
`GRAPH_SCOPES`, `crossover_v2/measure_spec.py:124`), and an applied candidate
that carries its trusted floor in `exclusion_evidence`
(`measured_crossover_candidate.py:393-412`) and is persisted whole
(`baseline_profile.py:3562`).

The room product is a second, operator-less copy of that loop from before the
toolbox model (master-plan R4). `jasper/correction/` (28 modules; `session.py`
2,373 lines) has no propose or prescription seam for an operator; its one LLM
hook is the in-product client (`correction/envelope.py:1295`). It captures
through the browser (`session.py:48,270,1135`;
`jasper/web/correction_room_flow.py:22-29`; the path ADR-0255 schedules for
deletion), combines with a power mean
(`jasper/audio_measurement/analysis.py:251-267`), designs with the
already-shared `design_peq` (`audio_measurement/peq.py`), and grades with an
accept-or-revert ladder (`correction/acceptance.py`) whose own docstring calls
its thresholds placeholders. `jasper/calibration_agent/` (13 modules, 4,477
lines) is that client, imported by `correction/envelope.py`,
`web/correction_handlers.py` and `web/correction_tuning.py`, and importing
`web/sound_setup.py` itself (`calibration_agent/sound_actions.py:62`).
The product is entangled with the shared measurement daemon
(`web/correction_setup.py` serves the room, crossover, sync and bass routes;
`correction_handlers.py` and `correction_capture.py` each hold a room half
beside the crossover half and the shared capture slot), and shared pieces are
misfiled inside it: `correction/level_match.py` (live through the crossover
backend); the household mic record
(`correction/household_mic.py`: `read_household_mic` :183,
`resolve_household_mic_calibration` :240) with its path/hint helpers
(`web/correction_capture.py`: `_calibration_root` :844, `_household_mic_path`
:853, `_default_setup_calibration_for_spec` :946;
`web/correction_crossover_v2.py:2396`); `acoustic_quality.SNR_BANDS_HZ`,
pinned as the first four rows of `snr_policy.CROSSOVER_SNR_BANDS_HZ`
(`tests/test_audio_measurement_snr_policy.py:59-62`); the variance-cap rule
(`correction/variance_cap.py`) and the room target math (`correction/target.py`).

The bass program planned a third copy: a wizard
(`jasper/bass_extension/ladder.py`, 611 lines), a backend and a UI
(`docs/bass-extension-waves/wave-4-commissioning-backend.md`, `wave-6-ui.md`,
`bass-commissioning-ux.md`), and a parked apply/bypass/recover transaction in
`bass_extension/__init__.py` (665 lines, zero callers) kept honest only by
`tests/test_bass_extension_plan_status.py`.

D11 (ADR-0231 §5) kept the products apart to bound the right-size program's
scope, not on acoustic grounds. The owner is now redesigning both around
Bank's two-stage method (ADR-0256) and Room Matching (`PLAN.md` §1); three
copies of one loop is the coupling the toolbox model exists to remove.

## Decision

Owner ruling, 2026-09-08:

1. **One toolbox.** Speaker tuning, room correction and bass extension are
   programs, views and candidate kinds of the one tuning toolbox: the speaker
   engine, the shared math in `jasper/audio_measurement/`, and the round
   views. The layering rule of doctrine §1a stays, in the order 1 speaker ·
   2 bass · 3 room · 4 preference, and the layer boundary is enforced by graph
   composition (`session_graph.py`), not by a second package. This supersedes
   ADR-0231 §5 (D11) and only §5. Master-plan R13 is restated as: *preserve
   the layer boundary by graph composition; room work follows an adopted
   speaker tune.*
2. **The operator is the LLM for every layer** (R4, extended from the speaker
   stage to room and bass). There is no operator-less room wizard and no
   in-product LLM client.
3. **What retires, in the next wave**, each with a SUPERSEDED verdict and the
   shared pieces of rule 4 moved first: the room product's orchestration and
   pages (`jasper/correction/` except the math that moves; the room routes
   and handlers inside `web/correction_setup.py`, `web/correction_handlers.py`
   and `web/correction_capture.py`; `web/correction_room_flow.py`;
   `web/correction_tuning.py`; the room JS in `deploy/assets/correction/js/`
   and `deploy/assets/shared/js/measurement-audio.js`);
   `jasper/calibration_agent/` and its CLI; `jasper-correction-bundle`; the
   bass wizard (`bass_extension/ladder.py`); the parked apply/bypass/recover
   transaction in `bass_extension/__init__.py` with its deadness tests; and
   the three superseded bass wave docs (wave 4 backend, wave 6 UI,
   commissioning UX).
4. **What moves first:** `level_match` beside `jasper/audio_measurement/ramp.py`;
   the household mic record and its three path/hint helpers into
   `audio_measurement` beside `mic_identity.py` and `calibration.py` (ADR-0255
   §3; this also clears the item PR #4138 held back); `SNR_BANDS_HZ` into
   `snr_policy`; the variance-cap rule and the room target math into
   `audio_measurement`. The shared capture slot stays in the daemon; the
   crossover, sync, calibration and healthz routes stay.
5. **ADR-0257 §1 is amended.** The engine's candidate apply supersedes the
   parked Layer-2 apply pathway. The pathway and its deadness tests are
   deleted in the retire wave, not by the PR that lands the first production
   caller. `alignment`, `targets`, `adapters/`, `limiter_evidence`, `profile`
   and `bench/` stay for the bass candidate kind (ADR-0260 §3).

This ADR deletes nothing.

## Consequences

- The new layers land at the engine's extension points and nowhere else: the
  programs registry for the seat cube and the close spot (ADR-0260);
  `ARTIFACT_BY_VIEW` (`jasper/cli/round_views/_common.py:93-126`) for the room
  and bass views; the candidate bank and apply transaction for the room and
  scheduled-bass candidate kinds; the emitter's room and bass stages
  (`jasper/active_speaker/camilla_yaml.py:1790-1809`, `:497-610`). No new
  framework, daemon, database, knob or doc tier.
- Not this program's: the engine's session, the flow and the web twin belong
  to the tuning-flow agent; an engine change is a request to it.
- Room correction needs an operator. A household with no LLM at the toolbox
  has no room correction: the one-button wizard is given up, and with it the
  placeholder-threshold accept-or-revert ladder that stood in for judgment.
- ADR-0256's reader of the applied floor lands in the toolbox's room views,
  not in `jasper/correction/`. The active emitter's per-side room set
  (ADR-0258) and `jasper/sound/camilla_yaml.py`'s `room_peqs_right` converge
  when the side axis lands; the room candidate kind emits into that one stage.
- Doctrine §1a names no bass layer today; the wave that lands the bass
  candidate kind adds its bullet.
- Rejected: teaching the separate room walk the ceiling (a third copy of the
  applied-candidate reader, the cloud combiner and the apply transaction);
  keeping `calibration_agent/` as an optional adviser (R4 forbids a second
  provider platform).
