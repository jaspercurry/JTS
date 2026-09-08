# Seat-matched tuning — plan of record

**Status:** active. Wave 0 landed (PR #4488, ADR-0255…0258). Next: Wave 0b (two
ADRs), then four lanes in parallel. **Owner:** jaspercurry. **Orchestrating
session:** https://claude.ai/code/session_01CR6fGdpH8YDFPv9ZbyXmGJ. **Tracking
issue:** see §9. **Where this lives:** branch
`claude/loudspeaker-tuning-architecture-iephfa`, never merged — fetch it.
Decisions live in `docs/adr/` on `main`; this file holds the vision, the plan,
the wave rows, the coordination rules and the status log.

## How to resume from a fresh session

1. `git fetch origin main claude/loudspeaker-tuning-architecture-iephfa`; read this
   file from that branch; read `AGENTS.md` at HEAD.
2. Read ADR-0255, 0256, 0257, 0258, and 0259/0260 once Wave 0b lands.
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
- Wired kernel: `jasper/audio_measurement/wired_capture.py`; answer minting
  lands with PR #4138.
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
| 1.1 | A | **Census, then move.** Grep every importer of `jasper.correction.*`, `jasper.calibration_agent.*` and the room handlers from `jasper/web/correction_crossover_v2*.py`, `active_speaker/`, `cli/`, `multiroom/`, `doctor/`. Move the shared pieces named in ADR-0259 before any deletion. | D | census in the PR body; boundary tests green | code-review |
| 1.2 | A | **Delete the room product.** `jasper/correction/` orchestration whole (session, acceptance, autolevel, browser_audio, confidence, envelope, status, state_guard, runtime_integrity, runtime_safety, strategy, failures, evidence, replay_artifacts, bundles, bundle_tools, interop, fir_runtime, artifacts, acoustic_quality after its SNR table moves, `_numbers`); the room routes and handlers in `correction_setup.py`/`correction_handlers.py`/`correction_capture.py` (the crossover, sync, calibration and healthz routes stay); `web/correction_room_flow.py`; `web/correction_tuning.py`; `jasper/calibration_agent/` and `jasper-calibration-agent`; `jasper-correction-bundle`; the room JS (`deploy/assets/correction/js/main.js` room modules, `shared/js/measurement-audio.js`); doctor's room section re-pointed at the applied candidate or dropped; all their tests. SUPERSEDED verdict per module. | D | grep proof; `scripts/test-merge` green; the speaker walk still opens a v2 session on a fixture | code-review; D-tier |
| 1.3 | A | **Delete the bass wizard.** `bass_extension/ladder.py`, the apply/bypass/recover transaction in `bass_extension/__init__.py`, `tests/test_bass_extension_plan_status.py`, and the three superseded wave docs. Keep `alignment`, `targets`, `adapters`, `limiter_evidence`, `profile` (absorbed by 3.3), `bench/`. | D | grep proof; `/state.bass_extension` still reports | code-review |
| 1.4 | B | **Pose vocabulary.** `ProgramPose` gains kind (bearing / seat / close) and distance; `mark_distance_m` becomes per-take; the seat kind carries an offset from the head. Programs: `seat/cube` (head + six face centres at 30 cm; prompts in plain words) and `close/spot` (~0.3 m on the design axis). Human mover via the existing position-ready walk. | P | `jasper-angle-capture plan` lists both; a fixture round banks seven seat takes with the kind recorded | code-review |
| 1.5 | B | **Room views.** Ungated analysis as an option over banked takes; `room-median` (median, spread, per-position deviation below the ceiling), `room-persistence` (features holding across ≥ N positions), `room-ceiling` (the applied candidate's trusted floor clamped per ADR-0256, fallback disclosed). | V | three `ARTIFACT_BY_VIEW` rows; inventory lists them; ADR-0237 stdout | code-review |
| 1.6 | — | First wired seat-cube session on jts3 through the applied tune. | H | banked round, integrity clean | owner |
| 1.7 | B | Runbook "Room" section; menu regenerated. | A | menu `--check` | sanity |

### Wave 2 — the room candidate kind (lane C; starts after 0b on fixtures)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 2.1 | Layer-3 candidate kind: cuts-only bells on the program bus below the ceiling, one set per side (mono = one side). Code-computed limits: per-bin cut depth from spread, a taper to flat over ~1/3 octave below the ceiling (in `design_peq`'s per-bin arrays), boost admission per the regime plan's D5 (persistent in ≥ 5 of 7, modally plausible, N ≥ 3, capped, level cost disclosed). The LLM authors inside the limits; the `propose`/`stage` doors validate. | C | door refuses out-of-limit filters with codes; fixture candidate round-trips through the emitter reader | code-review high |
| 2.2 | `room-grade` view: re-measured cube against the target below the ceiling, incumbent beside it; regression is a disclosure; restore is the doctrine path. | V | fixture grades; no auto-revert machinery | code-review |
| 2.3 | Boundary prior view: from declared geometry predict the 2π/4π gain step and the quarter-wave null (c/4d) per wall. Advisory. | V | 85 cm → ≈100 Hz null on a fixture | code-review |
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
  (3.1–3.5). 1.4 and 3.1 also wait for PR #4138.
- **Joins:** 2.4 needs 1.6; 4.x needs 3.x; 5.1 needs 2.1 and 4.3; 6.1 needs the
  emitter free.
- **Hardware on jts3, in order:** an adopted speaker tune from the
  structure-first campaign → 1.6 → 2.4 → 3.4 rungs → 4.1. The owner's time at
  the box is the critical path.

## 7. Session protocol

- One fresh session per lane per wave. The orchestrating session writes the
  brief (reading list, verified facts, rows, proofs, gates, report format) and
  reviews the merged diff before the next brief.
- Model split: Sonnet verifies citations and sweeps prose; Opus implements and
  reviews; the top model designs, adjudicates premises, and holds the
  non-negotiable review.
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

- 2026-09-08: Wave 0 merged (PR #4488). Same day the owner redirected the
  program: room and bass are layers of the one toolbox (was: separate
  products); bass has no nearfield rung (was: nearfield plant fit); poses are
  flexible and categorized (the ~0.3 m close reference stays); delete now what
  the vision retires. Tracking issue opened on GitHub (number recorded by the
  session that opens it).
