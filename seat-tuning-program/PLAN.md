# Seat-matched tuning — plan of record

**Status:** active. Wave 0 landed (PR #4488, ADR-0255…0258); Wave 0b landed
(PR #4517, ADR-0259/0260). Lane B landed rows 1.4/1.5/1.7 (PRs #4522, #4524,
#4525); lane C landed rows 2.3 (#4520), 2.1 (#4544) and 2.2 (#4546); lane A
landed rows 1.1–1.6 (#4557, #4602, #4563, #4567, #4604, #4603); lane D
is being integrated by the orchestrator (rebased, one PR per row). **Owner:** jaspercurry. **Orchestrating
session:** https://claude.ai/code/session_014RrgH2yP2zFdebvhGGfTXd (took over
2026-09-09 13:00Z from session_01CR6fGdpH8YDFPv9ZbyXmGJ; handoff in
`briefs/HANDOFF-ORCHESTRATOR.md`). **Tracking
issue:** [#4502](https://github.com/jaspercurry/JTS/issues/4502). **Where this lives:** branch
`claude/loudspeaker-tuning-architecture-iephfa`, never merged — fetch it.
Decisions live in `docs/adr/` on `main`; this file holds the vision, the plan,
the wave rows, the coordination rules and the status log.

## How to resume from a fresh session

1. `git fetch origin main claude/loudspeaker-tuning-architecture-iephfa`; read this
   file from that branch; read `AGENTS.md` at HEAD.
2. Read ADR-0255, 0256, 0257, 0258, 0259, 0260.
3. Find the next unstarted rows in §5. Each names its lane, tag, proof and gate.
4. Every wave is one fresh session per lane, working from a brief the
   orchestrating session writes; that session reviews the merged diff before
   the next brief. Verify every `file:line` at your HEAD — the tree wins.

## 1. Vision

One methodology, one toolbox, three programs. A person plugs a UMIK-2 into the
Pi, opens jts.local, and brings an LLM. The LLM uses the toolbox to measure the
speaker on and off axis, gated, and gets the drivers and crossover right (the
speaker stage). The speaker goes to its listening position. The LLM measures a
cube around the listener's head, ungated, and corrects the low band the room
owns — cuts for modes, admitted boosts for persistent dips, and a
volume-scheduled bass extension that uses the room's own gain — while the
speaker's direct sound above the ceiling stays exactly as the speaker stage left
it (the room and bass stages). Each stage is the same loop: measure at
positions, analyze, propose, apply, re-measure. Only the poses, the window, the
views and the candidate vocabulary change. The code computes and protects; the
LLM judges; the human moves the microphone and listens.

It is an open-source version of Dutch & Dutch Room Matching built on Balázs
Bank's method (AES 134, 2013), on hardware the software does not choose: a
passive 1-way, a 2-way, a 3-way, a 3-way with a cardioid bass channel, one
cabinet or a stereo pair on one DAC.

| | Bank 2013 | Dutch & Dutch Room Matching | JTS (this plan) |
|---|---|---|---|
| Speaker's direct sound | gated in-room IR, corrected at high resolution | factory anechoic reference per unit; factory FIR linearizes crossover phase | gated per unit in the speaker stage; FIR only if a structure-first campaign proves excess phase |
| Where the room is measured | listening area, several positions; remote points bound the gain | a cube around the head: centre plus six face centres ~30 cm out; averaged | the seat cube as a measurement program; median as trend, spread as confidence |
| What is corrected in-room | below a transition set by the gate achieved; minimum-phase IIR | low frequencies only; built-in parametric EQ | below the applied tune's trusted floor (ADR-0256); minimum-phase biquads |
| Target | 4th-order high-pass at 30 Hz | flat, optional +1 dB/oct room curve below 100 Hz | flat below the ceiling with a taper; extension corners as bass rungs; tilt is Layer 4 |
| Nulls | limit gain | don't boost, move | boost only into spatially persistent, modally plausible dips; N ≥ 3 |
| Bass and the wall | not addressed | rear woofers < 100 Hz coupled to the front wall at 10–50 cm; app takes wall distance and area first | LF boost policy of the in-room correction + volume schedule; a boundary prior from declared geometry |
| Who judges | the engineer | REW auto-EQ | the LLM, with code-owned hard stops |

## 2. Principles (each has an ADR or a doctrine line)

- **One loop, one toolbox.** Speaker, room and bass are programs, views and
  candidate kinds of the one engine, not separate products. (ADR-0259, Wave 0b.)
- **The seam is the ceiling.** Above it the speaker stage has authority and the
  room layer does nothing. Below it, speaker, room and bass are corrected
  together on the seat cube, through the applied tune. The ceiling is the
  applied candidate's trusted floor, clamped and disclosed (ADR-0256).
- **Layers by graph composition.** A capture for layer N plays through N and
  below and nothing above (`docs/measurement-loop-doctrine.md` §1a). Layers:
  1 speaker · 2 bass · 3 room · 4 preference (never measured).
- **Flexible poses, categorized.** A take carries its kind, distance, bearing
  or seat offset, and window. Gated bearings at ~1 m answer speaker questions
  above the trusted floor; a close take at ~0.3 m is the room-suppressed
  reference (`round-views close-reference`, kept); the ungated seat cube answers
  speaker-plus-room questions. The LLM reads the category and weighs the take.
  No pose is forbidden; none is required. (ADR-0260.)
- **No nearfield rung.** Bass extension is fitted on the seat-cube median to an
  extended-corner target (Bank's own target shape). Protection comes from
  declared plant facts, the in-room distortion-versus-level ladder and the
  limiter evidence. (ADR-0260.)
- **Two keys, always.** Output side × driver role (ADR-0258). Cardioid is a
  variant of the bass role with per-region band, delay, polarity and level.
- **Wired only** (ADR-0255). The Pi plays and records; the browser is a
  position-ready walk.
- **Code computes and protects; the LLM judges.** Hard stops are `AGENTS.md`'s
  closed list. Everything else is a disclosure.
- **Delete now what the vision retires.** The owner's ruling (2026-09-08): code
  we are confident we will not use goes now, not after a proof. Deletions carry
  SUPERSEDED verdicts and move shared pieces first.
- **Toolbox shape.** Every capability is a CLI verb + a banked artifact + a menu
  row + a methodology pointer. This program adds programs, views, candidate
  kinds and emitter stages; it does not restructure the engine's session or flow.

## 3. Three programs, one seam

| Program | Owns |
|---|---|
| Right-size (cleanup agent) | size and shape of `jasper/active_speaker/`; helper convergence; write-only records; the web-twin dissolution; decision D13 |
| Tuning flow (its agent) | the speaker stage as a product: the walk, the structure-first campaign (ADR-0203), operator views, the side-solo capture graph |
| **Seat-matched tuning (this)** | everything below the ceiling and per cabinet: the seat-cube program and pose vocabulary, room views and candidate kind, bass views, scheduled candidate kind, protection ladder, runtime scheduler, one headroom budget, per-side emission, the boundary prior — and the retirement of the room product and the bass wizard plan |

Rule of engagement: rows here add to `measurement_programs.py`,
`ARTIFACT_BY_VIEW`, the candidate bank, and the emitter. An engine change is a
request to the tuning-flow agent, not a change made here. The active emitter is
wanted by all three; rows 5.1 and 6.1 wait for it to be quiet.

## 4. Engine facts this plan leans on (main `a809d71c6`, 2026-09-08)

- Clouds and combination exist in the speaker engine:
  `jasper/active_speaker/crossover_v2/spatial.py` (`cloud_position_*`,
  `cloud_validity_floor_hz`), `jasper/audio_measurement/spatial_combine.py:1442`.
- Programs are a registry with a pose vocabulary
  (`jasper/active_speaker/measurement_programs.py`: `ProgramPose`,
  `MeasurementProgram`, `_PROGRAMS`, `spot_program`). Poses are bearings;
  distance is a pinned constant (`mark_distance_m = 1.0`, see
  `jasper/cli/round_views/close_reference.py:24`).
- The layering rule is enforced in `crossover_v2/session_graph.py`.
- The applied candidate carries the trusted floor (`exclusion_evidence`) and is
  persisted whole (`baseline_profile.py:3562`). No cross-package reader needed.
- Emitters: active has one room PEQ stage pre-split on both channels
  (`active_speaker/camilla_yaml.py:1790-1809`, `:1927-1931`); flat has
  `room_peqs_right` (`sound/camilla_yaml.py:344-375`). One headroom gain absorbs
  room and linearization boosts; bass boost is emitted per driver
  (`camilla_yaml.py:497-610`).
- Wired kernel: the recorder, zero-run scan and integrity report live in the
  leaf (`jasper/audio_measurement/wired_capture.py`). Since PR #4138 was cut,
  main moved `WiredCaptureAnswer`, `mint_wired_answer`, `make_wired_recorder`
  and `WiredStimulusCapture` into the engine
  (`jasper/active_speaker/crossover_v2/wired_stimulus.py`); #4138 puts the first
  three in the leaf and adds `require_wired_mic` plus a null-door intactness
  gate. The leaf home is the one this program needs: the bass bench cannot
  import `active_speaker` (the emitter already imports `bass_extension`), and
  the CLIs should not. #4138 is gated on an owner `jasper-null` hardware run.
- Household mic record: `read_household_mic` / `resolve_household_mic_calibration`
  in `jasper/correction/household_mic.py`; `_calibration_root`,
  `_household_mic_path`, `_default_setup_calibration_for_spec` in
  `jasper/web/correction_capture.py`; `default_setup_calibration_for_v2` in
  `jasper/web/correction_crossover_v2.py`. All move to `audio_measurement`
  beside `mic_identity.py` and `calibration.py` before `jasper/correction/`
  goes (ADR-0255 §3). This also clears #4138's held-back item.
- Bass, by module at main (lines): keep `alignment` 160, `targets` 177,
  `adapters/` 938, `limiter_evidence` 1,213 (pure math and the frozen protocol);
  keep `bench/` 5,682 for the limiter campaign, shrink after it has run once;
  `profile` 632 is absorbed by the scheduled candidate (3.3); delete `ladder`
  611 and the apply/bypass/recover transaction in `__init__` 665 with their
  tests. The bench imports only its own submodules, so the deletions do not
  touch it. The emitter's bass stage (`camilla_yaml.py:497-610`) and
  `classify_bass_extension_graph` generalize to N biquads per rung.
- Distortion-versus-level: `jasper/audio_measurement/distortion.py`,
  `round-views distortion`. Declared geometry: `jasper-declare-geometry`.
- **The room product is entangled with the shared measurement daemon.**
  `jasper/web/correction_setup.py` is the HTTPS daemon for room, crossover and
  bass pages; `correction_handlers.py` hosts both the room handlers
  (`_handle_start/_status/_upload_*/_next_position/_verify/_apply/_propose*/
  _interpret/_envelope/_autolevel_*`, `_maybe_auto_revert`, …) and the speaker
  walk's (`_handle_crossover_v2_*`, `_handle_calibration_*`);
  `correction_capture.py` holds the shared capture slot (`_run_capture`) beside
  room-only readiness and household-mic helpers. `jasper/correction/level_match.py`
  is live through the crossover backend (misfiled, not dead). The household mic
  record (ADR-0255 §3) and the calibration upload routes stay.
- `jasper/calibration_agent/` (23 files, 4,477 lines) is the in-product LLM
  client for the room wizard, imported by `correction/envelope.py`,
  `web/correction_handlers.py`, `web/correction_tuning.py`, `web/sound_setup.py`.
  Superseded by the operator-is-the-LLM model.
- Bass: `alignment.py`, `targets.py`, `adapters/*` (pure; `fit_plant` fits a
  2nd-order HP to a magnitude curve — re-pointed at the seat median it yields
  the effective in-situ corner), `limiter_evidence.py`, `bench/` (executor,
  runner, render, derivation — needed for the limiter campaign). `ladder.py` is
  the wizard state machine; `__init__.py`'s apply/bypass/recover transaction is
  the parked Layer-2 apply pathway (zero callers). Unbound: `PlayAndCapture`
  (`bench/executor.py:148`) and per-target `TargetPlan` binding (#1738).

## 5. Waves and rows

Tags: **A** ADR/doc · **D** delete · **P** program · **V** view · **C** candidate
kind · **E** emitter · **R** runtime · **H** hardware session. **NN** touches a
non-negotiable: `/adversarial-review` plus an owner hardware pass; merge held
until confirmed. Lanes run in parallel on disjoint files.

### Wave 0 — decisions (LANDED 2026-09-08, PR #4488)

ADR-0255 wired-only · ADR-0256 derived ceiling, per cabinet, median/σ/taper ·
ADR-0257 bass resumes, in-room validation, one budget · ADR-0258 sides × roles.

### Wave 0b — two ADRs (docs only, one PR)

| Row | Concern | Tag |
|---|---|---|
| 0b.1 | **ADR-0259: room correction and bass are layers of the one tuning toolbox.** Supersedes ADR-0231 §5 (D11). Restates master-plan R13 as "the layer boundary is graph composition". The operator is the LLM for every layer; no operator-less wizard. Names what retires, now: the room product's orchestration and pages, `calibration_agent/`, `web/correction_tuning.py`, the bass wizard (`ladder.py`), the parked Layer-2 apply pathway and its deadness tests (amending ADR-0257 §1: the engine's candidate apply supersedes it), and the superseded bass wave docs (wave-4 backend, wave-6 UI, commissioning UX). Names what moves first: `level_match` beside `audio_measurement/ramp.py`; the household mic record; `SNR_BANDS_HZ` to `snr_policy`; the variance-cap rule and room target math into `audio_measurement`. | A |
| 0b.2 | **ADR-0260: poses are flexible and categorized; bass has no nearfield rung.** Distance, kind (bearing / seat / close) and window are take attributes, not constants; every take is categorized for the LLM. `close-reference` stays as the room-suppressed diagnostic. Supersedes ADR-0257 §3's protection basis: declared plant facts + in-room distortion ladder + limiter evidence; the family is fitted on the seat-cube median to an extended-corner target (Bank's target shape as precedent). | A |
| 0b.3 | Pointer notes: methodology "Room" and "Bass" stubs → the ADRs; `room_boundary.py`'s roadmap paragraph → a pointer to ADR-0256 (docstring only). | A |

### Wave 1 — retire, and make the seat cube a program

Lane A (retire) and lane B (program + views) run in parallel; they touch
disjoint files.

| Row | Lane | Concern | Tag | Proof | Gate |
|---|---|---|---|---|---|
| 1.1 | A | **LANDED 2026-09-09 — PR #4557 merged (`82e82a03b`).** **Census, then move.** Grep every importer of `jasper.correction.*`, `jasper.calibration_agent.*` and the room handlers from `jasper/web/correction_crossover_v2*.py`, `active_speaker/`, `cli/`, `multiroom/`, `doctor/`. Move the shared pieces named in ADR-0259 before any deletion: `level_match` beside `audio_measurement/ramp.py`; the household mic record and its three path/hint helpers to `audio_measurement`; `SNR_BANDS_HZ` to `snr_policy`; the variance-cap rule and room target math to `audio_measurement`; the shared capture slot stays in the daemon. | D | census in the PR body; boundary tests green | code-review |
| 1.2 | A | **LANDED 2026-09-09 — PR #4602 merged (`c36dd0cea`), 191 files, −41,111.** **Delete the room product.** `jasper/correction/` orchestration whole (session, acceptance, autolevel, browser_audio, confidence, envelope, status, state_guard, runtime_integrity, runtime_safety, strategy, failures, evidence, replay_artifacts, bundles, bundle_tools, interop, fir_runtime, artifacts, acoustic_quality after its SNR table moves, `_numbers`); the room routes and handlers in `correction_setup.py`/`correction_handlers.py`/`correction_capture.py` (the crossover, sync, calibration and healthz routes stay); `web/correction_room_flow.py`; `web/correction_tuning.py`; `jasper/calibration_agent/` and `jasper-calibration-agent`; `jasper-correction-bundle`; the room JS (`deploy/assets/correction/js/main.js` room modules, `shared/js/measurement-audio.js`); doctor's room section re-pointed at the applied candidate or dropped; all their tests. SUPERSEDED verdict per module. | D | grep proof; `scripts/test-merge` green; the speaker walk still opens a v2 session on a fixture | code-review; D-tier |
| 1.3 | A | **LANDED 2026-09-09 — PR #4563 merged (`7ef2adbef`) as row 1.4 in the brief's numbering.** **Delete the bass wizard.** `bass_extension/ladder.py`, the apply/bypass/recover transaction in `bass_extension/__init__.py`, `tests/test_bass_extension_plan_status.py`, and the three superseded wave docs. Keep `alignment`, `targets`, `adapters`, `limiter_evidence`, `profile` (absorbed by 3.3), `bench/`. | D | grep proof; `/state.bass_extension` still reports | code-review |
| 1.5 | A | **LANDED 2026-09-09 — PR #4604 merged (`c06466a08`); ADR-0265.** **Mic calibration door.** #4602 left the only writers of `household_mic.json` and the calibration store behind routes with no nginx path. Move `_save_household_mic` into `audio_measurement/household_mic.py`; add `jasper-mic-calibration models\|fetch\|upload\|show`; delete the five orphaned daemon routes (`/calibration/*` SUPERSEDED, `/test-tone` and `/healthz` SPENT). Orchestrator-owned; branch `claude/seat-w1-1-5-mic-calibration-door`. | C | the CLI writes a record `jasper-measure` resolves; route table exact-set pin | code-review |
| 1.6 | A | **LANDED 2026-09-09 — PR #4603 merged (`d85d08152`).** **Doctor provenance.** A CamillaDSP round-trip strips the `# Source:` marker and an active graph is named for its role, so doctor's rewritten `check_correction_current_config` reads a healthy active-speaker box as unmanaged. Accept `classify_camilla_config_text`'s active classification; drop the duplicate `jts_emitter_source` parse; retarget `ramp.py`'s pointer. Orchestrator-owned; branch `claude/seat-w1-1-6-doctor-provenance`. | C | one pin: markerless active graph → managed | code-review |
| 1.0 | B | **LANDED 2026-09-08 — PR #4510 merged (`72bc34764`), #4138 closed.** The wired kernel in the leaf. Reconcile with main: `WiredCaptureAnswer`, `mint_wired_answer`, `make_wired_recorder` move from `crossover_v2/wired_stimulus.py` to the leaf `audio_measurement/wired_capture.py`; `WiredStimulusCapture` stays in the engine; keep its `require_wired_mic` and the null-door intactness gate. Its own gate holds: one owner `jasper-null` run confirming a clean take reports zero zero-runs and zero block gaps. Coordinate with the right-size orchestrator, who owns the PR. | R | the PR's four hardware checks; AST identity of moved bodies | owner null run, then code-review |
| 1.4 | B | **LANDED 2026-09-08 — PR #4522 merged (`4d0a0a94f`).** **Pose vocabulary.** `ProgramPose` gains kind (bearing / seat / close) and distance; `mark_distance_m` becomes per-take; the seat kind carries an offset from the head. Programs: `seat/cube` (head + six face centres at 30 cm; prompts in plain words) and `close/spot` (~0.3 m on the design axis). Human mover via the existing position-ready walk. | P | `jasper-angle-capture plan` lists both; a fixture round banks seven seat takes with the kind recorded | code-review |
| 1.5 | B | **LANDED 2026-09-08 — PR #4524 merged (`9f3ae539b`).** **Room views.** Ungated analysis as an option over banked takes; `room-median` (median, spread, per-position deviation below the ceiling), `room-persistence` (features holding across ≥ N positions), `room-ceiling` (the applied candidate's trusted floor clamped per ADR-0256, fallback disclosed). | V | three `ARTIFACT_BY_VIEW` rows; inventory lists them; ADR-0237 stdout | code-review |
| 1.6 | — | First wired seat-cube session on jts3 through the applied tune. | H | banked round, integrity clean | owner |
| 1.7 | B | **LANDED 2026-09-08 — PR #4525 merged (`0cbed8a57`).** Runbook "Room" section; menu regenerated. | A | menu `--check` | sanity |

### Wave 2 — the room candidate kind (lane C; starts after 0b on fixtures)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 2.1 | **LANDED 2026-09-09 — PR #4544 merged (`b083c06c8`).** Layer-3 candidate kind: cuts-only bells on the program bus below the ceiling, one set per side (mono = one side). Code-computed limits: per-bin cut depth from spread, a taper to flat over ~1/3 octave below the ceiling (in `design_peq`'s per-bin arrays), boost admission per the regime plan's D5 (persistent in ≥ 5 of 7, modally plausible, N ≥ 3, capped, level cost disclosed). The LLM authors inside the limits; the `propose`/`stage` doors validate. | C | door refuses out-of-limit filters with codes; fixture candidate round-trips through the emitter reader | code-review high |
| 2.2 | **LANDED 2026-09-09 — PR #4546 merged (`7db6bca73`).** `room-grade` view: re-measured cube against the target below the ceiling, incumbent beside it; regression is a disclosure; restore is the doctrine path. | V | fixture grades; no auto-revert machinery | code-review |
| 2.3 | **LANDED 2026-09-09 — PR #4520 merged (`d7d5fdc1e`).** Boundary prior view: from declared geometry predict the 2π/4π gain step and the quarter-wave null (c/4d) per wall. Advisory. | V | 85 cm → ≈100 Hz null on a fixture | code-review |
| 2.4 | Two seat-cube sessions on jts3: apply a Layer-3 candidate, re-measure, grade. | H | two banked rounds | owner |

### Wave 3 — the bass candidate kind and protection (lane D)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 3.1 | Bind the bench: `PlayAndCapture` and `TargetPlan` against the engine's play path and the wired recorder; `jasper-bass-extension-bench --live` stops failing closed. | R | fixture run; live path reaches the recorder | **NN** |
| 3.2b | **Added 2026-09-09, gates 3.3.** The adapters do not honour their own margin policy: `sealed.generate_family` ignores `subsonic_corner_ratio`/`subsonic_order` (ships 15 Hz 2nd-order where `conservative` declares 21.8 Hz 4th-order — at 10 Hz that is −1.8 dB against the policy's −21 dB, on a rung carrying +6 dB of infrasonic boost with no excursion model), and `generate_ported_family` ignores `boost_cap_db`. `_assert_bass_extension_safe` proves only the `LT → subsonic → limiter` ORDER, never the subsonic's values, so these numbers reach the DAC unchecked the moment 3.3's emission lands. Own PR, before 3.3. | C | **NN** |
| 3.2 | `bass-fit` view: fit the seat-cube median below the ceiling to an extended-corner target family (one corner per rung); the declared plant (adapters as parameter models; `fit_plant` on the median for the effective corner) supplies excursion-versus-boost. Publishes per rung: filters, boost, headroom cost, excursion margin. | V | fixture median → family JSON | code-review |
| 3.3 | Layer-2 scheduled candidate kind: the family keyed by listening level for the bass owner, emitted as named biquads per rung; a rung is admissible only with its protection evidence (3.4). Absorbs `profile.py`. | C | door refuses an unverified rung; emitter round-trip | code-review high |
| 3.4 | Protection ladder as a code-owned program: stepped-level sweeps at the seat, distortion-versus-level per rung, the sustain test, evidence banked per rung. A failing rung is inadmissible at that level. | P | fixture ladder; refusal codes | **NN** |
| 3.5 | Runbook "Bass" section; menu rows. | A | menu `--check` | sanity |

### Wave 4 — bass evidence and runtime (lane D, after 3)

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 4.1 | Supervised limiter bench campaign on jts3 per `limiter-evidence-protocol.md`; one accepted, replayable bundle. | H | owner present, **NN** |
| 4.1b | **The contract revision (added 2026-09-09).** The limiter-evidence protocol blocks all wave-4 production wiring until an ADR names the accepted bundle's exact `evidence_fingerprint`, records an independent review at zero blockers, and authorizes a named trusted caller; the tap-realization amendment separately forbids "a scheduler" by name. 4.2 and 4.3 cannot start before this merges. | A | owner sign-off |
| 4.2 | Runtime scheduler: pure target selection, instant retreat, gated re-extend, patching the named rung filters; no new daemon; no added latency. | R | **NN**, adversarial review |
| 4.3 | First production caller lands (the engine's apply of a scheduled candidate). | C | code-review |

### Wave 5 — one budget, one sequence (join)

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 5.1 | One headroom budget in the emitter: room + linearization + bass boost through one disclosed gain with its cost in maximum level. | E | **NN** |
| 5.2 | Runbook sequence: speaker tune → bass family → room candidate → bass re-check at the seat; `close-reference` named as the on-demand room-gain split. | A | sanity |

### Wave 6 — the pair and the 3-way

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 6.1 | Per-side room stage in the active emitter and its reader, converged with `room_peqs_right`. Needs the side-solo capture graph (tuning-flow agent). | E | **NN** adjacency |
| 6.2 | One bass family per bass system with a per-unit fit check. | C | code-review |
| 6.3 | Cardioid variant: an emitter output of the bass role with per-region band, delay, polarity and level (in phase below the LF corner for wall reinforcement; delayed and inverted in the mid band). Design conversation first; only when the 3-way exists. | E | design, then **NN** |

## 6. Lanes, gates, hardware

- **After Wave 0b, four lanes start at once:** A retire (1.1–1.3) · B program
  and views (1.4, 1.5, 1.7) · C room candidate on fixtures (2.1–2.3) · D bass
  (3.1–3.5). 1.4 and 3.1 wait for row 1.0 (PR #4138 landed).
- **Joins:** 2.4 needs 1.6; 4.x needs 3.x; 5.1 needs 2.1 and 4.3; 6.1 needs the
  emitter free.
- **Hardware on jts3, in order:** an adopted speaker tune from the
  structure-first campaign → 1.6 → 2.4 → 3.4 rungs → 4.1. The owner's time at
  the box is the critical path.

### 6a. Run order (the sequence, not just the lanes)

1. **Wave 0b brief → fresh session → one docs PR → orchestrator review → merge.**
   Nothing else starts before this merges; every lane cites its two ADRs.
2. **Row 1.0 (PR #4138 rebased) and your `jasper-null` hardware run.** The one
   hardware item that can happen early; it gates 1.4's capture and 3.1.
3. **Four fresh sessions at once**, each from its own brief:
   - Lane A, retire: three PRs in order — room product, calibration agent, bass
     wizard. Census first, shared pieces moved first, verdicts per module.
   - Lane B, program: 1.4 poses and programs, 1.5 room views, 1.7 docs.
   - Lane C, room candidate: 2.1, 2.2, 2.3 on fixtures.
   - Lane D, bass: 3.1 (after row 1.0), 3.2, 3.3, 3.4, 3.5.
4. **Hardware, in this order and only when the tuning-flow campaign has adopted
   a speaker tune:** 1.6 first seat-cube session → 2.4 two sessions → 3.4 rungs
   → 4.1 limiter campaign. 1.6 can run against whatever tune is applied; 2.4
   waits for an adopted tune or its grades are against a tune about to change.
5. **Wave 4** after 3.x and 4.1: runtime scheduler, first production caller.
6. **Wave 5** after 2.1 and 4.3: one headroom budget, runbook sequence.
7. **Wave 6** when the emitter is quiet and the second cabinet is real.

After every merge the orchestrating session reviews the diff against the row,
appends to §9, and comments once on #4502 with the PR links.

## 7. Session protocol

- One fresh session per lane per wave. The orchestrating session writes the
  brief (reading list, verified facts, rows, proofs, gates, report format) into
  `seat-tuning-program/briefs/wave-<n>-<lane>.md` on this branch, and the fresh
  session is pointed at that file. The brief is re-verified against HEAD when
  written; the plan holds the what and why, the brief the how. The orchestrator
  reviews the merged diff before the next brief.
- Sessions are spawned by the owner from `briefs/KICKOFF-TEMPLATE.md`, filled
  per lane by the orchestrating session; the snippet links the issue, the plan
  and the brief and carries the standing rules below.
- Model split: the lane session orchestrates and delegates — Sonnet verifies
  citations, reads and sweeps prose; Opus implements and relocates; the lane
  session keeps design, the non-negotiable review and final judgment.
- Standing engineering values (owner, 2026-09-08): simple, elegant, modular;
  80/20; one owner per concern; single source of truth; clear boundaries (the
  package-boundary tests are the contract); observability where a fact matters
  (log event, `/state`, doctor); reliability over cleverness; leave every file
  touched smaller unless the feature genuinely grew; add rows at the engine's
  extension points, never a new framework, daemon, database, knob or doc tier.
- Every session: `git fetch origin`; branch from fresh `origin/main`; verify
  premises first and stop a row whose premise is false; `scripts/test-fast`
  (final sentinel only; `scripts/test-merge` for import-structure and deletion
  PRs); `/simplify` then `/code-review` medium; NN rows add `/adversarial-review`
  and wait for the owner's hardware pass; line delta and verdicts in the PR body;
  no model identifiers in commits or PRs.

## 8. Not in this program

FIR (waits for the structure-first campaign and an excess-group-delay
measurement) · the residual tier above the ceiling · a crossover finder · the
cardioid channel's design · a database or memory service · new `JASPER_*`
knobs · any browser or relay capture · an operator-less wizard.

## 9. Status log

- 2026-09-09 14:55Z: Row 3.1 part 1 opened as PR #4643 (`+3013/−140`, 22 files,
  CI green) and reviewed: **REQUEST CHANGES**, fix agent dispatched. Two of the
  four must-fixes change what we thought was true:
  - **The bench as built can never produce an accepted bundle, so wave 4.1 was
    unreachable.** `describe_stimulus_program` renders the protocol's sustain
    hold as one sweep segment, so admission judges a 30/60/90 s hold against a
    per-*sweep* ceiling of 4–12 s. Reproduced against the repo's own fixture:
    1 s and 6 s admit, 12/30/90 s refuse `program_segment_outside_limits`, and
    `_classify_candidate` requires the sustain verdicts, so no campaign reaches
    `accepted`. Being fixed by describing the hold as a hold — no cap raised,
    no cap bypassed — plus a preflight, because the refusal currently arrives
    after the fader is raised and the recorder armed. The same finding catches
    the sweep rounding 4.0 → 4.013 s past a declared 4.0 (issue #2921's "must
    fit the SAME number").
  - **The hand-composed play seam was the wrong answer and is being reverted to
    the engine's binder.** The branch's stated reason — that
    `confirm_graph_is_live` does byte-equality against a submitted YAML — is
    false: it fingerprints a *normalized read-back*, as its own docstring says.
    The only real obstacle was that the bench fed it the pre-patch text while
    `ActivationReadback.active_config_raw`, the post-patch read-back the runner
    already banks, is exactly what the binder wants. Consuming it costs one
    optional `lock_source` kwarg and the existing `before_play` hook, and it
    closes a gap the hand-composition opened: the binder proves liveness inside
    `play_wav`, under the writer lock, on **every** play, where the bench proved
    once per activation and then played N rungs with the lock released between
    them while `measurement_window` deliberately does not pause CamillaDSP.
    One seam-composer, not two.
  Also must-fix: the onset correlation defeats `correlation`'s own memory
  backstop (~200 MB peak on a 90 s hold, on a 1 GB Pi, right after a
  stress-level hold — the reference was trimmed to 2 s, the search window was
  not), and an unproven THD banked as `thd_max_ratio: 0.0` against the frozen
  protocol's "must not be filled from a default".
  Judged sound and left alone: both orderings the author flagged for the
  adversarial tier. The give-back releasing the duck first occupies
  `graph.restore`'s slot in `measurement_door`'s own order, runs with the graph
  already restored and nothing playing, and is the only order that cannot
  strand a duck on a released claim. Raising to level before re-admission is
  *required* by `play_program.assert_ready()`, and admission judges the
  stimulus, not the level. No path can play louder than the engine allows.
  The five PROMOTEs are honest (bodies unchanged, second consumers real), and
  the non-negotiables are untouched by grep over the whole branch.

- 2026-09-09 14:40Z: Row 3.2 opened as PR #4641 (`+1028/−40`, 15 files, CI
  green) and reviewed: **REQUEST CHANGES**, fix agent dispatched. The review
  earned its keep by testing rather than reading — it built 72 synthetic
  in-room medians and found that repointing the ported adapter's
  `required_captures` at the new seat-median role admits `vented` cabinets into
  a fit that cannot run in-room: `fit_ported_plant` locates `fb` as a ≥4 dB
  local minimum, which in a seat median is a room mode, not the port null. 31
  of 72 were admitted; the worst published `fb_hz` 70.1 Hz off a 95 Hz room
  dip, an effective corner of 137 Hz against a real 38 Hz corner, and a deepest
  rung demanding 9.03 dB of boost under a policy capped at 6.0. The fix is one
  line: leave `required_captures` at `WOOFER_NEARFIELD` so `vented` refuses
  like `passive_radiator` already does. Also must-fix: a docstring pointing at
  the deleted `ladder.py`, and a published `model_db` carrying the subsonic
  high-pass the measurement does not have (1.194 dB off at 20 Hz against a
  published `fit_rms_db` of 0.192 — the fit-quality curve misstating fit
  quality exactly where the row reasons).
  Two premises of the brief's row 3.2 are false at HEAD and are now recorded
  as such: the design draft's cabinet block **cannot carry a declared f0/Q**
  (`driver_safety._normalise_cabinet` rejects every key outside its five), so
  the datasheet pair is an operator flag used only when the fit refuses and
  only for sealed; and **no excursion margin is computable anywhere** — there
  is no Xmax, no Sd and no displacement model in the tree, so the brief's
  per-rung excursion margin cannot be published. The review confirmed nothing
  downstream depends on it (3.3's schema has no excursion field; 3.4's ladder
  is the empirical bound) and that the substitute, the transform's own boost,
  is named for what it is. **This weakens ADR-0260's protection basis in one
  leg:** "declared plant facts" cannot bound excursion today, so the in-room
  ladder and the limiter evidence carry it alone. Recorded for the owner.
  New row **3.2b** (above) carries the margin-policy divergence the review
  found: it must land before 3.3's emission, because the subsonic's values are
  never proved by the graph check that proves its position.

- 2026-09-09 14:30Z: Wave-4 brief written (`briefs/wave-4-bass-runtime.md`),
  fact-checked against main `a1994ef69` by a Sonnet pass first. It found the
  thing the plan's row list was missing, now added as row **4.1b**: the
  limiter-evidence protocol blocks every wave-4 production wiring until a
  contract revision names the accepted bundle's exact `evidence_fingerprint`,
  records an independent review at zero blockers and zero should-fixes, and
  authorizes a named trusted caller — and the tap-realization amendment's
  "what this does NOT authorize" list forbids *a scheduler* by name. Rows 4.2
  and 4.3 are contract-blocked until that ADR merges. Other facts the brief now
  carries, each of which would have been a false premise: the context builder
  the protocol called missing has since been built (`bench/context.py`, whose
  limiter domain is byte-identical to the emitter's own bound), so the open gap
  is only that no production call feeds a real bundle into the replay; the R1
  live-patch precedent exists but is unstepped (`multiroom/runtime_balance.py`
  patches one named `Gain` today, and no interpolating sequencer exists
  anywhere); the DSP writer lock refuses every mutation while a bass apply-intent
  file exists, yet nothing has written that file since #4563, so a scheduler
  must either own the writer again or stop the readers checking for it; and
  "listening level" names two non-interchangeable numbers — the live 0-100 knob
  on `/state`, which the bass schema already speaks and a scheduler keys on,
  versus `seat_level_reference`'s one-shot measurement-session SPL constant,
  which it must not. The brief also carries a stop-and-report instruction: if a
  boosted rung can reach CamillaDSP after wave 3 without a matching charge into
  `active_baseline_headroom`, that is a level bug on the output path, not a
  wave-5 nicety.

- 2026-09-09 14:05Z: Lane D pre-reads done (two Sonnet read-only passes over
  3.3 and over 3.4+3.1b at their tips vs main). Findings and the
  orchestrator's rulings, all to be carried into the PRs:
  - **PR shape.** 3.3 stays ONE PR (3.3a candidate kind + door, 3.3b
    emission): landing 3.3a alone would leave a window where a candidate
    carrying a bass field applies without its bass layer, the failure lane C
    had to close with `measurement_candidate_room_scope`. 3.4 SPLITS into two
    PRs (3.4a rung graph, 3.4b ladder evidence + `bass-ladder` view): the
    intermediate state is fail-closed (the door refuses a boosted rung with no
    evidence) and it isolates the NN-tier diff — admission arithmetic and the
    `graph_safety` widening — for the adversarial review.
  - **3.4's `graph_safety` widening is real and must be named:**
    `bass_extension_block_valid` goes from "boost must be exactly 0" to
    "0..`boost_cap_db`". Gated by the door's admission rule, but it is the
    NN-tier hunk the adversarial review exists for.
  - **The sustain test is NOT built and will not be duplicated in the
    ladder.** The bench already owns `sustain_stress` (frozen
    `limiter-evidence-protocol.md`, rows 3.1/4.1); a second implementation in
    the ladder would be the third rule of Defaults broken. Ruling: the ladder
    document discloses that it carries swept-sine evidence only, and the
    door's hard stop (ladder AND limiter evidence for any boost > 0) already
    prevents admission on sweep evidence alone. The wave-4 brief names the
    sustain contract's home. Brief row 3.4's wording is superseded here.
  - **"The human starts each level" is NOT satisfied and must be fixed
    before 3.4a merges.** `--level-dbfs` is repeatable and the session plays
    the whole ladder back-to-back unattended; the SPL-ceiling check is a
    whole-request preflight, not a per-level gate. Required: for
    `graph_scope=bass_candidate` only, one level per invocation with a
    refusal code (or a per-level operator confirmation if separate
    invocations cannot bank into one round). Not a change to the shared
    session loop other scopes use.
  - **No `MeasurementProgram("bass","ladder")` registration, and none is
    wanted:** levels are not poses, and a pose-list would be machinery for
    its own sake. What the row must supply instead, and does not yet: each
    banked step records its position and `grade_ladder` refuses a
    mixed-position ladder — that is the substance of "at the seat, head-centre
    pose" (3.4b's own test proves the step binding is deliberately
    pose-agnostic).
  - **`BassExtensionRefusal.LADDER_INCOMPLETE` must survive the rebase.**
    #4563 deleted it from `profile.py` as producer-less; 3.4b's
    `round_views/bass_ladder.py` uses it live. The `profile.py` conflict
    (lane D moved the enum to a new `refusals.py`, main edited it in place)
    is delete-vs-modify and must not be resolved by taking a side.
  - **3.3 carries an accommodation that main has since killed:**
    `classify_bass_extension_graph`'s dual authority (`desired_bass_extension`
    or the legacy `desired_profile`) existed only for
    `bass_extension.apply_bass_extension`, gutted by #4563 an hour after 3.3
    was written. It rebases into provably dead code and goes in the same PR,
    with the stale "until that applier retires" docstring in
    `sound/graph_carrier.py`.
  - **Field-name deviation, accepted:** the shipped bass field has no
    `rung_id`/`lt`/`boost_db`/`level_cost_db`; the cost is
    `target.boost_headroom_db` and the filters are a validated list. The PR
    body carries the brief-name → shipped-name mapping so a reviewer can diff
    the two.
  - Rebase collisions, all rows: `docs/tuning-operator-runbook.md` (generated
    menu cell — regenerate, never hand-merge), `bass_extension/profile.py`,
    `cli/round_views/__init__.py`, `tests/test_cli_exit_vocabulary.py`,
    `tests/test_bass_extension_{profile,runtime_gate_ssot}.py`, and for 3.1b
    `cli/bass_extension_bench.py` (its docstring still names the deleted
    `apply_bass_extension`; take main's wording).
  - Sizes are 1.4k–1.9k changed lines per row against the 400-line target;
    the split above is as far as they divide without leaving an unsafe
    intermediate state.

- 2026-09-09 13:30Z: Orchestration taken over by a fresh session (owner stopped
  "JTS D"; rebasing its branches is allowed). Topology of the eight lane D
  branches, verified with `merge-base --is-ancestor`: one stack
  `3-2 → 3-3a → 3-3b → 3-4a → 3-4b`; `3-1` independent; `3-1b-bench-field` =
  merge(`3-1`, `3-4a`) + two commits ("Compose the limiter bench's campaign
  from the applied candidate's field", "Enter the bench through the door's
  seams and anchor it to the live graph"), so the handoff's "3.1 with 3.1b"
  cannot land first: those commits consume 3.3's candidate field and 3.4a's
  `bass_candidate` scope. Landing order revised: **3.1 part 1** (the wired
  play-and-capture seam and rung analysis, retitled to what it is, plus the
  `bass_extension` boundary row) in parallel with **3.2** (consuming the door's
  `read_room_median`, duplicate parser deleted) → **3.3** (3.3a+3.3b, one PR;
  emission on the output path → NN) → **3.4** (3.4a+3.4b, NN) → **3.1 part 2**
  (3.1b's two commits: `--live` binding) → **3.5** docs last, menu regenerated.
  Dispatched: Opus agents for 3.1 and 3.2 (rebase, findings, ladder, PR);
  Sonnet read-only pre-reads of 3.3 and of 3.4+3.1b against main (collision
  map, claim check, deleted-dependency scan). Container note for the ladder:
  pycamilladsp must be pip-installed WITH its deps (it needs
  `websocket-client`); the rest of §4's recipe holds.

- 2026-09-09 11:40Z: Lane D now has eight branches (`3-1`, `3-1b-bench-field`,
  `3-2`, `3-3a`, `3-3b`, `3-4a-rung-graph`, `3-4b-ladder-view`, `3-5-bass-docs`),
  160–208 commits behind main, none opened as a PR, none rebased, the
  pre-review notes unanswered. Nothing from lane D can land in this state.
  Owner options: (a) tell the lane D session to rebase and open 3.1/3.2 now;
  (b) stop it and hand integration to the orchestrator (rebase, apply the
  pre-review fixes, open PRs per row). Not done unilaterally: the session is
  still pushing (last 11:31Z), so a second author on those branches would
  collide. No owner reply yet on the done-screen decision (#4502 has only
  orchestrator comments).
- 2026-09-09 10:10Z: Row 1.5 LANDED: PR #4604 squash-merged at `c06466a08` after
  the review round (calibration files `0640` with the registry root's group;
  `event=correction.calibration_unresolvable` on a swallowed read failure;
  ADR-0265 amends ADR-0259 §4; runbook `sudo` invocation; parse/size
  refusals). Lane A is complete: the room product, the LLM client and the
  bass wizard are gone, the shared pieces live in `audio_measurement`, and
  the operator registers a microphone through `jasper-mic-calibration`.
  Open owner decision: the crossover done screen's zero-action case.
- 2026-09-09 10:00Z: #4604 (row 1.5) Opus review: REQUEST CHANGES — under
  `sudo` the calibration files land `root:jasper 0600` and the daemon
  (`jasper-web`) silently measures uncalibrated (`resolve_household_mic_calibration`
  swallows `OSError`). Fix agent dispatched: `atomic_write_text(mode=0o640)`
  for both files, an observable event on the swallowed read failure, an
  amending ADR for ADR-0259 §4, runbook `sudo` line, parse/size refusals,
  `--model` default aligned with `models`. Moved-not-copied, boundaries,
  exit vocabulary, secrets and deletions all verified clean.
- 2026-09-09 09:50Z: Row 1.6 LANDED: PR #4603 squash-merged at `d85d08152` after
  its re-run went green (the AirPlay fade test was a wall-clock flake; a
  hardening PR is warranted if it recurs). #4604 (row 1.5) awaits its Opus
  review and CI. Lane D pushed a fifth stacked branch (`3-4a-rung-graph`),
  103 behind main, still no PR.
- 2026-09-09 09:45Z: Rows 1.5 and 1.6 opened as PRs #4604 (mic calibration
  door, +615/−710: `jasper-mic-calibration models|fetch|upload|show`,
  `save_household_mic` in the leaf, five orphaned routes and
  `calibration.preview_curve` deleted) and #4603 (doctor provenance, net −5).
  #4603's first CI run failed only on the wall-clock AirPlay fade test
  (`test_airplay_volume_hook.py`), untouched by the PR and green on its base;
  stood down with one comment and one re-run. #4604 under Opus review.
  Tension to record later: ADR-0259 §4 said the calibration and healthz
  routes stay; #4602 left them unreachable and #4604 deletes them.
- 2026-09-09 09:20Z: Row 1.2 LANDED: PR #4602 squash-merged at `c36dd0cea` after
  a Sonnet claim check (10/10 pass) and an Opus design review (approve with
  fixes). `jasper/correction/` is gone. The review's one real gap — no box can
  register a microphone now — becomes row 1.5 (a CLI door, orchestrator's
  Opus agent, PR opening); the doctor provenance false-warn becomes row 1.6
  (same). Owner decision left open: the crossover done screen can mint zero
  actions on a first tune (mint a "Back to Sound" filler or accept it).
  Note for #4594 (another program): its `NGINX_PUBLIC_SURFACE` string still
  names `sound/room/`.
- 2026-09-09 08:35Z: Lane A opened row 1.2 (PR #4602, 191 files, +794/−41,111,
  CI green, rebased onto current main across #4580/#4570/#4598/#4599–#4601).
  Sonnet claim check and Opus design review running in parallel. Two owner
  decisions the PR surfaces: the daemon's root-mounted calibration,
  test-tone and healthz routes lost their only nginx path with the
  `/sound/room/` block (no browser page registers a microphone now — a CLI
  verb or a re-mount is needed for the toolbox's mic step); boxes that ran
  room correction keep `/var/lib/jasper/correction/{sweeps,captures,sessions}`
  on disk with no doctor row (deliberately not pruned: banked captures).
- 2026-09-09 07:40Z: Lane D pre-review (Sonnet, read-only, branches not yet
  PRs; posted on #4502): both branches stale (3.1 102 commits behind, 3.2
  139; not stacked on each other); 3.1 ships `bench/wired_play.py` (a faithful
  mirror of `null_door._play_and_capture`, wired-only) but not the CLI
  binding (`bass_extension_bench.py --live` still refuses), no `TargetPlan`
  binding, no `PACKAGE_BOUNDARIES` row; 3.2 adds a third `room_median.json`
  parser (`seat_fit.read_seat_median`) with no `window` check instead of
  consuming the door's `read_room_median`. Registration and side-effects
  clean. Lane D has since pushed 3.3a and 3.3b as well; still no PR.
- 2026-09-09 06:30Z: LLM client retired: PR #4567 squash-merged at `ce093fecc`
  (its session merged main twice, including #4580, and resolved the
  `correction_runtime.py` collision; Sonnet verified no revert). Lane A's
  row 1.2 branch is pushed and its PR follows. #4502 status posted.
- 2026-09-09 05:25Z: #4567 (LLM client retire) fix commit `2d96efeec` pushed
  (doctor privsep rows, prose, envelope v10 log line, prohibited-keys pin);
  review comment posted; merges on green. Follow-up parked: `usage.py` ~770
  still describes a tuning DB no writer creates (ledger consolidation).
  Process note from the owner: Sonnet does read-only verification (claim
  checks, CI triage, collision maps); Opus does design review and fixes;
  the orchestrator adjudicates and merges.
- 2026-09-09 04:55Z: Bass wizard retired: PR #4563 squash-merged at `7ef2adbef`
  after the fix commit (tripwire, reentrant refusal pin, `LADDER_INCOMPLETE`
  dropped). #4567 (LLM client) awaits its fix commit and CI.
- 2026-09-09 04:45Z: Opus reviews of lane A's deletion PRs, both APPROVE WITH
  FIXES, fix agents dispatched: #4563 (1.4) must keep the `"sealed_v1"`
  tripwire (three surviving literals vs `BASS_EXTENSION_RUNTIME_ADAPTER_IDS`),
  pin the reentrant pending-intent refusal in `dsp_apply`, drop the orphaned
  `LADDER_INCOMPLETE`; #4567 (1.3) must drop the daemon's tuning-surface read
  privileges from doctor's privsep spec, plus prose fixes and a content pin
  for `PROHIBITED_PRESCRIPTION_KEYS`. Verified: NN3 clean (no surviving
  `correction_*` reads a secret); no envelope persisted, so the schema bump is
  safe; the three deleted routes are gone from the route table, not just
  untested; #4580's rebase after #4567 is mechanical except dropping
  `TuningSetupUnavailable` from its new `correction_runtime.py`. Brief row 1.2
  updated so it does not re-plan the tuning surface 1.3 removed.
- 2026-09-09 04:25Z: Row 1.1 LANDED (PR #4557 squash-merged at `82e82a03b`). Lane A
  ran 1.3 and 1.4 before 1.2 (an import cycle: `correction/envelope.py` and the
  room's LLM routes import `calibration_agent`, which imports
  `correction.{bundles,evidence,strategy}`), so PR #4567 (row 1.3, −11,206,
  takes the room wizard's LLM hooks with the client) and PR #4563 (row 1.4,
  −4,790, bass wizard + parked apply pathway; encoder `_intent_payload` moves to
  `apply_intent.py`) are open and CI-green; Opus reviews running; 1.2 is built
  on 1.3's head and opens after it merges. Collision to manage: open PR #4580
  (p11 program, "correction wizard runtime floor") edits
  `web/correction_{capture,handlers,setup}.py`, `main.js` and
  `tests/test_web_correction_tuning.py`, which #4567 deletes — merge #4567
  first; #4580 rebases. Lane D pushed `claude/seat-w3-3-3a-bass-candidate` (6
  commits, +3597/−580, stacked on 3.1/3.2); still no lane D PR.
- 2026-09-09 02:28Z: Row 1.1 LANDED: PR #4557 squash-merged at `82e82a03b` on
  green CI after the fix commit.
- 2026-09-09 02:30Z: Row 1.1 LANDED: PR #4557 squash-merged at `82e82a03b` on
  green CI after the fix commit. Lane A proceeds to 1.2 (delete the room
  product); `room_limits.py` is on main so `variance_cap.py`/`target.py` go
  as SUPERSEDED.
- 2026-09-09 02:20Z: #4557 (row 1.1) reviewed by Opus: APPROVE WITH FIXES;
  fixes pushed as `4cd078db4` (hoist the v2 setup-calibration import; pin
  `resolved_household_mic`; one module-level `configured_calibration_root`).
  Merges on green. Carried to row 1.2: two renamed `audio_measurement` tests
  still import `correction.session` / the `acoustic_quality` alias. Carried to
  the tuning-flow agent: the two thin `DefaultSetupCalibration` hint builders
  (web, cli) have diverged; a `from_household` factory in `sweep_spec.py`
  folds them.
- 2026-09-09 01:55Z: Row 2.2 LANDED: PR #4546 squash-merged at `7db6bca73` after
  the merge-in of main (#4512's reconcile-test fix) went green. Lane C's
  software rows are all on main; 2.4 (first seat-cube room round on hardware)
  is the owner's.
- 2026-09-09 01:50Z: Lane A opened row 1.1 (PR #4557, moves only, 26 files
  +170/−180, CI clean); Opus review running. Lane D pushed
  `claude/seat-w3-3-1-bench-binding` (2 commits, +2960/−117, no PR yet) beside
  its `3-2-bass-fit` branch. #4546 (2.2) carries main's reconcile-test fix
  (#4512) after a red run that was main's, not the PR's; CI re-running.
- 2026-09-09 01:05Z: Row 2.1 LANDED: PR #4544 squash-merged at `b083c06c8` on
  green CI after the fix commit. #4546 (row 2.2) retargets to main; its
  merge-in and fixes dispatched.
- 2026-09-09 01:05Z: #4544 fixes pushed as `4b2f62a72` (gated-median refusal via
  `room_boundary.ROOM_MEDIAN_WINDOW`; `measurement_candidate_room_scope`;
  duplicate side refusal; comment narrowed); review comment posted; merges on
  green.
- 2026-09-09 00:40Z: Owner spawned lane A (retire) from the RETIRE kickoff
  snippet; brief updated first for `room_limits.py` landing via #4544.
- 2026-09-09 00:35Z: Lane C opened row 2.1 (PR #4544, the room candidate kind,
  +3028/−153, CI green) and row 2.2 (PR #4546, `room-grade`, stacked on 2.1).
  Lane D pushed `claude/seat-w3-3-2-bass-fit` (row 3.2; no PR yet; no 3.1
  bench-binding branch seen — ask the PR body why). Opus reviews:
  - #4544 APPROVE WITH FIXES; the orchestrator's agent is pushing them: refuse a
    median whose `window` is not `ungated`; `_validated_room_correction` must
    return the frozen mapping, not `dict(raw)` (post-fingerprint mutation);
    refuse a room-carrying candidate compiled under `scope="candidate"`
    (`measurement_candidate_room_scope`) instead of emitting without its room
    PEQs; refuse a repeated normalized side name; narrow the baseline_profile
    comment about which seam re-reads room PEQs. Verified true: door never
    designs; constants live once in `room_limits.py`; taper is 0 dB at the
    ceiling and full at ceiling/2^(1/3); `room_limits` is a pure leaf whose
    depth rule is identical to `correction/variance_cap.py` (lane A deletes
    the latter); room PEQs reach only the `room_candidate` recompose and the
    apply emit; boost charged as the sum of positive gains (upper bound); no
    clamp touched; tune-mismatch gate compares reduced projections, not whole
    dicts. Landed shape: `room_correction = {sides:{side:[{freq,q,gain}]},
    ceiling_hz, ceiling_source, basis:{round_id, room_median_sha256,
    admitted_boosts_hz}, boost_db_total, level_cost_db}`.
    Follow-ups (not blocking, for a later row): `baseline-reemit` and
    `jasper-audition` recompose with `room_peqs=()` and would drop an applied
    room layer (only `sound/graph_carrier.py`'s recomposes and
    `active_speaker/audition.build_reduced_yaml` re-read it), and
    `setup_status.py` ~`:650` recomposes the expected graph without
    `room_peqs` for its Layer-A fingerprint compare (confirm the fingerprint
    excludes the room stage or a room-corrected box reports drift) — thread
    the extraction or make the drop explicit; the multi-side refusal uses the
    generic `room_correction_invalid` (wants its own slug when per-side
    emission lands, ADR-0258); `level_reference_db` is a bin-count median on
    the take's native grid, not octave-weighted (disclosed); the three
    private helpers imported across doors want a `_prescription_common.py`.
  - #4546 APPROVE WITH FIXES (applied after 2.1 lands, with its two mechanical
    conflicts against #4520): an incumbent band with no bins grades as
    perfectly flat and reads `regressed: true` (return `None` metrics and add
    `incumbent_n_bins`); `ROOM_GRADE_RESOLUTION_DB` decides `regressed` but is
    not in the artifact. Follow-ups: pin the half-open band-mask change with a
    grid containing 60/120 Hz; a fixture whose `level_reference_db` is
    non-zero; record the median's sha256 in `room_grade.json`. Note: 2.2
    modifies lane B's `room-median` answer (band table moved to
    `room_views.py`, masks half-open) — accepted as the one-owner fix.
- 2026-09-09 00:15Z: Row 2.3 LANDED: PR #4520 squash-merged at `d7d5fdc1e` by
  the orchestrator after CI went green on the merged-and-fixed head.
- 2026-09-08 23:45Z: Lane B rows 1.4, 1.5, 1.7 LANDED (owner merged #4522 at
  `4d0a0a94f` and #4524 at `9f3ae539b`; orchestrator merged #4525 at
  `0cbed8a57` after review). Post-hoc Opus review against
  `briefs/wave-1-program.md`: engineering sound, no follow-up PR. Facts every
  later lane must use: `room_median.json` as written is `freqs_hz`,
  `median_db`, `spread_db`, `n_positions`, `positions[{id, pose_key,
  deviation_db}]`, `ceiling_hz`, `ceiling_source` ("applied_candidate" |
  "fallback"), `window` ("ungated" | "gated" | "mixed" — computed from the
  takes' `gating_applied`, not the constant the brief pinned; a consumer
  refuses anything but "ungated"). The ceiling multiplies the persisted raw
  `1/T` (`exclusion_evidence.validity_floor_hz`) by `TRUSTED_FLOOR_MULTIPLIER`
  before clamping — verified correct (the trusted figure is a separate,
  unpersisted key). Notes for a later tidy row, not blocking:
  `spatial.cloud_position_record` writes `mark_distance_m` twice (`:985` and
  via `pose_kind_fields`); a geometry-locked retry in `crossover_v2_flow.py`
  ~`:1708` rebuilds a bare prompt and would drop the pose kind (unreachable
  today); two function-local imports in `correction_crossover_v2.py`
  (`:1998`, `:4224`) lack a `# lazy` reason.
- 2026-09-08 23:45Z: Lane C row 2.3 PR #4520 reviewed (Opus): physics
  verified independently (null c/4d = 100.9 Hz at 0.85 m; +3 dB at c/8d — the
  brief's c/12d was wrong; the two-wall dB sum equals the rigid corner
  image-source product up to the per-wall clamp); advisory contract and
  package boundaries clean. It conflicted with main in four registry files
  after #4524; the orchestrator's Opus agent merged main in and pushed the
  small fixes (consume `ROOM_FLOOR_HZ` and the `ARTIFACT_BY_VIEW` artifact
  name instead of redeclaring; pin the `boundary_prior_ceiling_invalid`
  refusal; correct the corner-model prose; drop the unreachable
  `sound_speed_source == "argument"` branch; derive `_OPTIONAL` from
  `WALL_FIELD_BY_KEY`). Merges when green. Lane C's 2.1 is in progress on
  `claude/seat-w2-2-1-room-candidate`; 2.3 landed first.
- 2026-09-08 23:40Z: Lane A not yet spawned (no session newer than "JTS 0b").
  Lane D still on its first branch (bench binding), nothing pushed yet.
- 2026-09-08 ~20:40Z: Owner spawned lanes B (program), C (room candidate) and
  D (bass) as fresh sessions from the kickoff snippets; each is working on its
  brief's first branch (`claude/seat-w1-1-4-seat-cube-program`,
  `claude/seat-w2-2-1-room-candidate`, `claude/seat-w3-3-1-bench-binding`).
- 2026-09-08 21:42Z: Wave 0b LANDED: PR #4517 merged at `ddb9f51ec`. Lane A
  unblocked; owner told to spawn it.
- 2026-09-08 21:25Z: Wave 0b PR #4517 opened (ADR-0259, ADR-0260, pointer
  notes, `room_boundary.py` docstring). Reviewed against
  `briefs/wave-0b-decisions.md`: matches on every point; no changes requested;
  merges when its test lane is green. Lane A brief written:
  `briefs/wave-1-retire.md` (four PRs: move shared pieces; retire the room
  product; retire the calibration agent; retire the bass wizard).
- 2026-09-08 21:06Z: Wave 0b session started ("JTS 0b"). Lane A (retire) waits
  for its PR; lane B's docs row 1.7 also waits for it (it replaces 0b's
  methodology stub).
- 2026-09-08: Lane briefs written and verified against main `f84d7da`/`72bc347`
  by Sonnet fact-checkers: `briefs/wave-1-program.md` (lane B),
  `briefs/wave-2-room-candidate.md` (lane C), `briefs/wave-3-bass.md` (lane D).
  Corrections folded in: the room math cannot be imported across the
  `active_speaker → correction` boundary and moves to `audio_measurement` in
  lane C's first commit; no measurement scope includes room PEQ today, so lane C
  adds a `room_candidate` scope; boost admission is framed as adopting the
  regime plan's D5 with a room-specific threshold; `bass_extension` is absent
  from `PACKAGE_BOUNDARIES` and `camilla_yaml` imports it only under
  `TYPE_CHECKING` (the leaf home for the wired kernel stands for the CLIs, and
  lane D adds the boundary row); `MeasurementProgram.mic_move_count` is the
  property name (not `pose_count`); there is no `gate=None` analysis path, so
  the seat kind is analyzed via the near-field exemption mechanism.
- 2026-09-08: Row 1.0 LANDED: PR #4510 merged at `72bc34764`; #4138 closed as
  superseded. Lanes B (1.4) and D (3.1) are unblocked.
- 2026-09-08: Row 1.0 opened as PR #4510 (re-applies #4138 at HEAD; leaf
  placement reconciled with `wired_stimulus.py`; null-door intactness gate is
  disclosure-only by owner decision, promotion condition beside the code; the
  hardware null run is skipped). #4138 to close as superseded when it merges.
- 2026-09-08: Wave 0b brief written: `briefs/wave-0b-decisions.md`.
- 2026-09-08: `briefs/` added: `wave-0-decisions.md` (the brief Wave 0 ran
  from, recovered) and `reference-right-size-cleanup-brief.md` (what the
  cleanup program was told, kept here so our rows do not collide with it).
- 2026-09-08: Wave 0 merged (PR #4488). Same day the owner redirected the
  program: room and bass are layers of the one toolbox (was: separate
  products); bass has no nearfield rung (was: nearfield plant fit); poses are
  flexible and categorized (the ~0.3 m close reference stays); delete now what
  the vision retires. Tracking issue: #4502. PR #4138 (wired kernel to the
  leaf) adopted as row 1.0 after finding main had since moved the same kernel
  into the engine; the bass code is kept where it is pure math or the frozen
  protection protocol and deleted where it was the wizard (§4).
