# Seat-matched tuning — plan of record

**Status:** active. Wave 0 landed (PR #4488, ADR-0255…0258); Wave 0b landed
(PR #4517, ADR-0259/0260). Lane B landed rows 1.4/1.5/1.7 (PRs #4522, #4524,
#4525); lane C landed row 2.3 (#4520), 2.1 in progress; lane D in progress;
lane A spawned 00:40Z. **Owner:** jaspercurry. **Orchestrating
session:** https://claude.ai/code/session_01CR6fGdpH8YDFPv9ZbyXmGJ. **Tracking
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
| 1.1 | A | **Census, then move.** Grep every importer of `jasper.correction.*`, `jasper.calibration_agent.*` and the room handlers from `jasper/web/correction_crossover_v2*.py`, `active_speaker/`, `cli/`, `multiroom/`, `doctor/`. Move the shared pieces named in ADR-0259 before any deletion: `level_match` beside `audio_measurement/ramp.py`; the household mic record and its three path/hint helpers to `audio_measurement`; `SNR_BANDS_HZ` to `snr_policy`; the variance-cap rule and room target math to `audio_measurement`; the shared capture slot stays in the daemon. | D | census in the PR body; boundary tests green | code-review |
| 1.2 | A | **Delete the room product.** `jasper/correction/` orchestration whole (session, acceptance, autolevel, browser_audio, confidence, envelope, status, state_guard, runtime_integrity, runtime_safety, strategy, failures, evidence, replay_artifacts, bundles, bundle_tools, interop, fir_runtime, artifacts, acoustic_quality after its SNR table moves, `_numbers`); the room routes and handlers in `correction_setup.py`/`correction_handlers.py`/`correction_capture.py` (the crossover, sync, calibration and healthz routes stay); `web/correction_room_flow.py`; `web/correction_tuning.py`; `jasper/calibration_agent/` and `jasper-calibration-agent`; `jasper-correction-bundle`; the room JS (`deploy/assets/correction/js/main.js` room modules, `shared/js/measurement-audio.js`); doctor's room section re-pointed at the applied candidate or dropped; all their tests. SUPERSEDED verdict per module. | D | grep proof; `scripts/test-merge` green; the speaker walk still opens a v2 session on a fixture | code-review; D-tier |
| 1.3 | A | **Delete the bass wizard.** `bass_extension/ladder.py`, the apply/bypass/recover transaction in `bass_extension/__init__.py`, `tests/test_bass_extension_plan_status.py`, and the three superseded wave docs. Keep `alignment`, `targets`, `adapters`, `limiter_evidence`, `profile` (absorbed by 3.3), `bench/`. | D | grep proof; `/state.bass_extension` still reports | code-review |
| 1.0 | B | **LANDED 2026-09-08 — PR #4510 merged (`72bc34764`), #4138 closed.** The wired kernel in the leaf. Reconcile with main: `WiredCaptureAnswer`, `mint_wired_answer`, `make_wired_recorder` move from `crossover_v2/wired_stimulus.py` to the leaf `audio_measurement/wired_capture.py`; `WiredStimulusCapture` stays in the engine; keep its `require_wired_mic` and the null-door intactness gate. Its own gate holds: one owner `jasper-null` run confirming a clean take reports zero zero-runs and zero block gaps. Coordinate with the right-size orchestrator, who owns the PR. | R | the PR's four hardware checks; AST identity of moved bodies | owner null run, then code-review |
| 1.4 | B | **LANDED 2026-09-08 — PR #4522 merged (`4d0a0a94f`).** **Pose vocabulary.** `ProgramPose` gains kind (bearing / seat / close) and distance; `mark_distance_m` becomes per-take; the seat kind carries an offset from the head. Programs: `seat/cube` (head + six face centres at 30 cm; prompts in plain words) and `close/spot` (~0.3 m on the design axis). Human mover via the existing position-ready walk. | P | `jasper-angle-capture plan` lists both; a fixture round banks seven seat takes with the kind recorded | code-review |
| 1.5 | B | **LANDED 2026-09-08 — PR #4524 merged (`9f3ae539b`).** **Room views.** Ungated analysis as an option over banked takes; `room-median` (median, spread, per-position deviation below the ceiling), `room-persistence` (features holding across ≥ N positions), `room-ceiling` (the applied candidate's trusted floor clamped per ADR-0256, fallback disclosed). | V | three `ARTIFACT_BY_VIEW` rows; inventory lists them; ADR-0237 stdout | code-review |
| 1.6 | — | First wired seat-cube session on jts3 through the applied tune. | H | banked round, integrity clean | owner |
| 1.7 | B | **LANDED 2026-09-08 — PR #4525 merged (`0cbed8a57`).** Runbook "Room" section; menu regenerated. | A | menu `--check` | sanity |

### Wave 2 — the room candidate kind (lane C; starts after 0b on fixtures)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 2.1 | Layer-3 candidate kind: cuts-only bells on the program bus below the ceiling, one set per side (mono = one side). Code-computed limits: per-bin cut depth from spread, a taper to flat over ~1/3 octave below the ceiling (in `design_peq`'s per-bin arrays), boost admission per the regime plan's D5 (persistent in ≥ 5 of 7, modally plausible, N ≥ 3, capped, level cost disclosed). The LLM authors inside the limits; the `propose`/`stage` doors validate. | C | door refuses out-of-limit filters with codes; fixture candidate round-trips through the emitter reader | code-review high |
| 2.2 | `room-grade` view: re-measured cube against the target below the ceiling, incumbent beside it; regression is a disclosure; restore is the doctrine path. | V | fixture grades; no auto-revert machinery | code-review |
| 2.3 | **LANDED 2026-09-09 — PR #4520 merged (`d7d5fdc1e`).** Boundary prior view: from declared geometry predict the 2π/4π gain step and the quarter-wave null (c/4d) per wall. Advisory. | V | 85 cm → ≈100 Hz null on a fixture | code-review |
| 2.4 | Two seat-cube sessions on jts3: apply a Layer-3 candidate, re-measure, grade. | H | two banked rounds | owner |

### Wave 3 — the bass candidate kind and protection (lane D)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 3.1 | Bind the bench: `PlayAndCapture` and `TargetPlan` against the engine's play path and the wired recorder; `jasper-bass-extension-bench --live` stops failing closed. | R | fixture run; live path reaches the recorder | **NN** |
| 3.2 | `bass-fit` view: fit the seat-cube median below the ceiling to an extended-corner target family (one corner per rung); the declared plant (adapters as parameter models; `fit_plant` on the median for the effective corner) supplies excursion-versus-boost. Publishes per rung: filters, boost, headroom cost, excursion margin. | V | fixture median → family JSON | code-review |
| 3.3 | Layer-2 scheduled candidate kind: the family keyed by listening level for the bass owner, emitted as named biquads per rung; a rung is admissible only with its protection evidence (3.4). Absorbs `profile.py`. | C | door refuses an unverified rung; emitter round-trip | code-review high |
| 3.4 | Protection ladder as a code-owned program: stepped-level sweeps at the seat, distortion-versus-level per rung, the sustain test, evidence banked per rung. A failing rung is inadmissible at that level. | P | fixture ladder; refusal codes | **NN** |
| 3.5 | Runbook "Bass" section; menu rows. | A | menu `--check` | sanity |

### Wave 4 — bass evidence and runtime (lane D, after 3)

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 4.1 | Supervised limiter bench campaign on jts3 per `limiter-evidence-protocol.md`; one accepted, replayable bundle. | H | owner present, **NN** |
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
    room layer (only `sound/graph_carrier.py` re-reads it) — thread the
    extraction or make the drop explicit; the multi-side refusal uses the
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
