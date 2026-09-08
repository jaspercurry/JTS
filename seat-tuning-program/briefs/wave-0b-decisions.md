# Brief: Wave 0b of the seat-matched tuning program (two ADRs, docs only)

You are working in `jaspercurry/JTS`. This wave records **two owner decisions as
ADRs** plus **pointer-only amendments**. It changes **no product code, no tests,
deletes no files**. Output: one docs-only PR the owner can merge, plus the report
in §7. The program's plan of record is `seat-tuning-program/PLAN.md` on branch
`claude/loudspeaker-tuning-architecture-iephfa` (fetch it; never merge it); this
brief is `seat-tuning-program/briefs/wave-0b-decisions.md` there.

## 1. Read first, in this order

1. `AGENTS.md` at HEAD.
2. `docs/adr/README.md` and `docs/adr/TEMPLATE.md`. Follow them for numbering,
   headings, and how supersession is recorded (status lines on old ADRs were
   edited in the 0126/0188 convention by Wave 0, PR #4488; do the same). The
   Wave 0 session reserved its ADR numbers on issue #4405 before opening the
   PR — read that issue and follow the same convention if it is still how
   numbers are reserved. Highest ADR at main `32c491dbb` is 0258; you will
   write 0259 and 0260 unless the reservation says otherwise.
3. The four Wave 0 ADRs: 0255 (wired-only), 0256 (derived ceiling, per
   cabinet, median/σ/taper, D6/D7 reaffirmed), 0257 (bass resumes; §1 keeps the
   deadness tests until the first production caller; §3 keeps the nearfield fit
   as the protection basis), 0258 (sides × roles; cardioid variant).
4. ADR-0231 §5 (D11: room correction and speaker tuning are two products; shared
   math only in `jasper/audio_measurement/`), ADR-0199 (no handoff tier),
   ADR-0229 (bass plan docs exempt), ADR-0192 (nearfield parked for the speaker
   stage; gating goes lower), ADR-0203 (structure-first), ADR-0237 (stdout is
   the answer).
5. `docs/measurement-loop-doctrine.md` §1a (the layering rule) and §2 (the
   authority model: code computes, the LLM judges, the human handles the room);
   `docs/tuning-master-plan.md` rulings R4 (the operator is the external LLM)
   and R13 (preserve room correction and its layer boundary).
6. `seat-tuning-program/PLAN.md` §1 (vision and the Bank / Dutch & Dutch table),
   §2 (principles), §4 (engine facts). Do not restate the plan in the ADRs;
   cite it as the plan and cite `file:line` for facts.

Every `file:line` below was read at main `32c491dbb` (2026-09-08). Verify at
your HEAD; if a premise is false, stop that row and say so in the PR body.

## 2. Facts the two ADRs rest on

**The one loop already exists in the speaker engine.** Multi-position clouds and
their combination: `jasper/active_speaker/crossover_v2/spatial.py`
(`cloud_position_screens` :219, `cloud_position_record` :860,
`cloud_geometry_verdict` :1574, `cloud_validity_floor_hz` :2062,
`cloud_trusted_floor_hz` :2083) and
`jasper/audio_measurement/spatial_combine.py:1442` `combine_positions`. Programs
are a registry: `jasper/active_speaker/measurement_programs.py` (`ProgramPose`
:39 with `azimuth_deg`/`elevation_deg` only; `MeasurementProgram` :48;
`_PROGRAMS` :121; `spot_program` :151; the header at :23 says "No level and no
mark distance"). The layering rule is enforced in graph composition
(`jasper/active_speaker/crossover_v2/session_graph.py`). The applied candidate
carries the trusted floor as `exclusion_evidence`
(`jasper/active_speaker/measured_crossover_candidate.py:393-412`) and is
persisted whole by `persist_applied_baseline_profile`
(`jasper/active_speaker/baseline_profile.py:3562`). The candidate bank, the
apply transaction, the banked-round format, `ARTIFACT_BY_VIEW`
(`jasper/cli/round_views/_common.py:91-123`) and the position-ready walk are
the extension points.

**The room product is a pre-toolbox wizard with no operator seam.**
`jasper/correction/` (28 modules; `session.py` 2,364 lines) has zero
occurrences of LLM/operator/propose/prescription vocabulary; it captures
through the browser (`session.py:48,270,1135`; `jasper/web/correction_room_flow.py:22-29`),
combines with a power mean (`jasper/audio_measurement/analysis.py:251-267`),
designs with `design_peq` (already in `audio_measurement/peq.py`), and grades
with an automated accept/revert ladder (`correction/acceptance.py`) whose
thresholds its own docstring calls placeholders. It is entangled with the
shared measurement daemon: `jasper/web/correction_setup.py` serves room,
crossover, sync and bass routes; `jasper/web/correction_handlers.py` holds
both the room handlers and `_handle_crossover_v2_*`;
`jasper/web/correction_capture.py` holds the shared capture slot beside
room-only helpers. `jasper/correction/level_match.py` is live through the
crossover backend. The household mic record lives in
`jasper/correction/household_mic.py` (`read_household_mic` :183,
`resolve_household_mic_calibration` :240) with path/hint helpers in
`jasper/web/correction_capture.py` (`_calibration_root` :844,
`_household_mic_path` :853, `_default_setup_calibration_for_spec` :946) and
`jasper/web/correction_crossover_v2.py:2396`. `jasper/calibration_agent/`
(13 Python files, 4,477 lines) is the in-product LLM client for the room
wizard, imported by `correction/envelope.py`, `web/correction_handlers.py`,
`web/correction_tuning.py`, `web/sound_setup.py`. `acoustic_quality.SNR_BANDS_HZ`
is identity-pinned against `snr_policy.CROSSOVER_SNR_BANDS_HZ`
(`tests/test_audio_measurement_snr_policy.py`).

**Bass.** `jasper/bass_extension/`: `alignment.py` 160, `targets.py` 177,
`adapters/` 938 (`fit_plant` fits a 2nd-order high-pass to a magnitude curve),
`limiter_evidence.py` 1,213, `profile.py` 632, `ladder.py` 611 (the wizard
state machine), `__init__.py` 665 (the parked apply/bypass/recover
transaction, zero callers), `bench/` 5,682 (imports only its own submodules).
Deadness tests: `tests/test_bass_extension_plan_status.py`. The unbuilt wave
docs assume the deleted relay: `docs/bass-extension-waves/wave-4-commissioning-backend.md:362,389`,
`bass-commissioning-ux.md:230-234`, `wave-6-ui.md`, `wave-7-hardware-validation.md:24-26`.
The nearfield fit is the plan's protection basis (`docs/HANDOFF-bass-extension-plan.md`
§5.3, §6.1); room gain is neither modeled nor measured.

**Close reference is not nearfield, and it stays.** `jasper/cli/round_views/close_reference.py`
compares a close round (`--close-m`) with the far round and recommends a
distance (`--distance`, via `active_speaker/branch_chain.recommended_distance`);
its docstring at :24 notes that `mark_distance_m = 1.0` is pinned for every
take (`crossover_v2/spatial.py:1393` `MARK_DISTANCE_M`). Methodology §6 names
it beside `classify-features`, `gate-sweep` and `distortion`.

**Sources.** Bank (AES 134, 2013) sets the in-room transition from the gate
achieved, corrects speaker and room jointly below it with minimum-phase IIR on
responses measured through stage one, skips nearfield explicitly, and uses a
4th-order high-pass at 30 Hz as the in-room target. Dutch & Dutch Room Matching
measures a cube around the head (centre plus six face centres about 30 cm out),
averages, corrects low frequencies only with parametric EQ, does not boost
nulls, and places rear woofers below 100 Hz against the front wall at 10–50 cm.
`PLAN.md` §1 has the comparison table; cite it rather than re-deriving.

## 3. ADR-0259 — Room correction and bass are layers of the one tuning toolbox

Template sections: Context / Decision / Consequences. Under ~100 lines.

- **Context:** the facts above, compressed: the loop exists once; the room
  product is a second, operator-less copy of it from before the toolbox model
  (R4); the bass program planned a third (wizard, backend, UI). D11 kept the
  products apart to keep the right-size program's scope bounded, not on
  acoustic grounds; the owner is now redesigning both.
- **Decision (owner, 2026-09-08):**
  1. Speaker tuning, room correction and bass extension are **programs, views
     and candidate kinds of the one tuning toolbox**. The layers of doctrine
     §1a stay (1 speaker · 2 bass · 3 room · 4 preference) and the layer
     boundary is enforced by graph composition, not by a second package.
     Supersedes ADR-0231 §5 (D11) — a partial supersession, the other four
     rulings in 0231 stand. Master-plan R13 is restated as "preserve the layer
     boundary by graph composition; room work follows an adopted speaker tune".
  2. **The operator is the LLM for every layer** (R4 extended). There is no
     operator-less room wizard and no in-product LLM client.
  3. **What retires, now, in the next wave** (SUPERSEDED verdicts, moved
     pieces first): the room product's orchestration and pages
     (`jasper/correction/` except the math that moves; the room routes and
     handlers inside `correction_setup.py`, `correction_handlers.py`,
     `correction_capture.py`; `correction_room_flow.py`; `correction_tuning.py`;
     the room JS); `jasper/calibration_agent/` and its CLI;
     `jasper-correction-bundle`; the bass wizard `ladder.py`; the parked
     apply/bypass/recover transaction and its deadness tests; the superseded
     bass wave docs (wave 4 backend, wave 6 UI, commissioning UX).
  4. **What moves first:** `level_match` beside `audio_measurement/ramp.py`; the
     household mic record and its three path/hint helpers into
     `audio_measurement` (ADR-0255 §3; this also clears PR #4138's held-back
     item); `SNR_BANDS_HZ` into `snr_policy`; the variance-cap rule and the room
     target math into `audio_measurement`. The shared capture slot stays in the
     daemon; the crossover, sync, calibration and healthz routes stay.
  5. **Amends ADR-0257 §1:** the engine's candidate apply supersedes the parked
     Layer-2 apply pathway; the pathway and its deadness tests are deleted in
     the retire wave rather than waiting for a first production caller.
- **Consequences:** name the extension points the new layers use (programs
  registry, `ARTIFACT_BY_VIEW`, candidate kinds, emitter stages); name what this
  program does not touch (the session, the flow, the web twin — the tuning-flow
  agent's); state the product consequence (room correction needs an operator);
  state that `jasper/sound/camilla_yaml.py`'s `room_peqs_right` and the active
  emitter's future per-side set converge (ADR-0258). Gives up: a one-button
  room correction for a household without an LLM.

## 4. ADR-0260 — Poses are flexible and categorized; bass has no nearfield rung

- **Context:** poses are bearings with a pinned 1 m distance; the close
  reference exists as a view but not as a pose kind; the bass plan's protection
  basis is a dust-cap fit the relay was to capture; Bank skips nearfield and
  targets a 4th-order high-pass at 30 Hz in-room; Room Matching measures a
  seven-point cube.
- **Decision (owner, 2026-09-08):**
  1. **A take carries its kind, distance and window** as attributes: kind ∈
     {bearing, seat, close}; distance in metres per take (the `MARK_DISTANCE_M`
     pin retires as a constant and becomes the bearing kind's default); window
     (gated / ungated) is an analysis choice recorded on the analysis, not on
     the capture. Every banked take is categorized so the LLM can weigh it: a
     gated bearing at ~1 m answers speaker questions above the trusted floor; a
     close take at ~0.3 m is the room-suppressed reference; the ungated seat
     cube answers speaker-plus-room questions. **No pose is forbidden; none is
     required.**
  2. **The seat cube is a program**: head plus six face centres about 30 cm
     out, seven takes. This amends ADR-0256 §4's "six-position default" for
     the seat program only; D7's spirit (a small cloud, don't chase more)
     stands.
  3. **Bass extension has no nearfield rung.** The family is fitted on the
     seat-cube median, through the applied tune, below the ceiling, to an
     extended-corner target per rung (Bank's target shape as precedent).
     Protection basis: declared plant facts (the adapters as parameter models),
     the in-room distortion-versus-level ladder (`audio_measurement/distortion.py`),
     and the limiter evidence protocol. Supersedes ADR-0257 §3's "nearfield fit
     stays as the protection basis"; §3's in-room validation and one headroom
     budget stand. Room gain is published when a close reference is taken, and
     estimated against the declared plant otherwise.
  4. **Close-mic stays optional** as `round-views close-reference`; the nearfield
     mic-ceiling spike and any `WOOFER_NEARFIELD`-style required role retire.
- **Consequences:** the pose vocabulary row and the seat program are the next
  wave's; the bass fit view consumes the median; `fit_plant` is re-pointed at
  the median for the effective in-situ corner; the protection ladder is a
  code-owned program (hardware damage is a hard stop regardless of who judges).
  Gives up: a room-free corner and Q for pole placement, in exchange for a fit
  that includes the room's own gain.

## 5. Pointer-only amendments (no rewrites, no deletions)

- `docs/adr/README.md`: two index rows; 0231 row → "§5 superseded by 0259";
  0257 row → "§1 amended by 0259, §3 superseded by 0260"; 0256 row → "§4 seat
  default amended by 0260".
- ADR-0231, ADR-0257, ADR-0256: status line only, in the 0126/0188 convention.
- `docs/tuning-master-plan.md`: one line under the decision register: "R13
  restated by ADR-0259".
- `docs/tuning-methodology.md`: two stubs, "Room" and "Bass", each one
  paragraph pointing at 0259/0260 and saying the sections are written in the
  waves that land the programs. Keep the prose bar: a pointer, not a plan.
- `jasper/audio_measurement/room_boundary.py`: replace the "Roadmap: RC1 …"
  docstring paragraph (:97-100) with one sentence pointing at ADR-0256
  (docstring-only; no code change; the module's tests must not change).
- Do **not** delete the bass wave docs here (Wave 1 lane A does, with
  verdicts). Do not touch `AGENTS.md`, `README.md`, product code or tests.
  Anything else you find stale goes in the PR body under "stale, not fixed
  here".

## 6. Mechanics

- `git fetch origin`; confirm `git merge-base --is-ancestor origin/main HEAD`;
  work on the branch your session designates (else `claude/seat-wave0b-decisions`).
- Validate: `python3 scripts/docs-linkcheck.py --all`;
  `python3 scripts/docs-impact.py` (read `--help`; `docs/doc-map.toml` may need
  rows); `scripts/test-fast`, trusting only the final `==> <lane>: N passed`
  sentinel (if the container lacks the venv, say so and let CI's docs lane be
  the arbiter, as Wave 0 did).
- Review tier: docs — author judgment plus a sanity look; run `/code-review low`.
  Model split that worked in Wave 0: Sonnet agents verify every citation and
  sweep for stale prose; an Opus review pass; you adjudicate premises and write.
- Fetch again before pushing; `git push -u origin <branch>`; confirm the remote
  ref advanced. One PR titled "ADRs: one tuning toolbox for room and bass;
  flexible poses and no nearfield rung". Body: the two decisions in one line
  each, the amended pointer lines, "stale, not fixed here", validation
  sentinels. No model identifiers in commits or the PR.

## 7. Report back

Print: the PR link; the two ADR numbers and titles; every file changed with a
one-line reason; the "stale, not fixed here" list; any premise from §2 found
false at HEAD and what you did instead.
