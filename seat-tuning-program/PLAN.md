# Seat-matched tuning — plan of record

**Status:** active. Wave 0 landed (PR #4488, ADR-0255…0258). Wave 0b (two more
ADR rows) is next. **Owner:** jaspercurry. **Orchestrating session:**
https://claude.ai/code/session_01CR6fGdpH8YDFPv9ZbyXmGJ (this file's author).
**Where this lives:** branch `claude/loudspeaker-tuning-architecture-iephfa`,
never merged — fetch it. Decisions live in `docs/adr/` on `main`; this file
holds the plan, the wave rows, the coordination rules and the status log.

## How to resume from a fresh session

1. `git fetch origin main claude/loudspeaker-tuning-architecture-iephfa` and read
   this file from that branch. Read `AGENTS.md` at HEAD.
2. Read ADR-0255, 0256, 0257, 0258 (and 0259/0260 once Wave 0b lands).
3. Find the next unstarted rows in §5. Each row names its lane, gate and proof.
4. Every wave is one fresh session working from a brief the orchestrating session
   writes; the orchestrating session reviews the merged diff before the next
   brief. Verify every `file:line` at your HEAD before acting — the tree wins.

## 1. What this program is

An open-source version of what Dutch & Dutch call Room Matching, built on
Balázs Bank's combined quasi-anechoic and in-room method (AES 134, 2013), on top
of JTS's existing speaker-tuning toolbox. The two sources agree:

| | Bank 2013 | Dutch & Dutch Room Matching | JTS (this plan) |
|---|---|---|---|
| Speaker's direct sound | gated in-room IR, corrected at high resolution | factory anechoic reference per unit; factory FIR linearizes crossover phase | gated per unit in `jasper/active_speaker/` (Layer 1); FIR only after a structure-first campaign proves excess phase |
| Where the room is measured | listening area, several positions; remote random points bound the gain | a cube around the head: centre plus six face centres, ~30 cm; average response | the seat cube as a measurement program; median as trend, spread as confidence |
| What is corrected in-room | below a transition set by the gate achieved; minimum-phase IIR | low frequencies only; the built-in parametric EQ (IIR), REW-optimized | below the applied tune's trusted floor (ADR-0256); minimum-phase biquads |
| Target | 4th-order high-pass at 30 Hz | flat, optional room curve +1 dB/oct below 100 Hz | flat below the ceiling with a taper; preference tilt stays Layer 4 |
| Nulls | limit gain | don't boost, move | boost only into spatially persistent, modally plausible dips; N ≥ 3 |
| Bass and the wall | not addressed | rear woofers < 100 Hz coupled to the front wall at 10–50 cm; app takes wall distance and listening-area size first ("90% with a tape measure") | bass extension is the LF boost policy of the in-room correction plus a volume schedule; a boundary prior from declared geometry seeds the LLM |
| Who judges | the engineer | REW auto-EQ | the LLM operator, with code-owned hard stops |

What neither source has and JTS does: per-unit gating instead of a factory
reference, an LLM in the loop, a volume-scheduled bass family, and a topology
vocabulary that covers a passive 1-way through a 3-way with a cardioid channel.

## 2. Principles (each has an ADR or a doctrine line)

- **One loop.** Measure at positions → the LLM analyzes → propose → apply →
  re-measure. Speaker, room and bass are programs, views and candidate kinds of
  that loop, not separate products. (Wave 0b supersedes ADR-0231 §5.)
- **The seam is the ceiling.** Above it the speaker stage has authority and the
  room layer does nothing. Below it, speaker, room and bass are corrected
  together on the seat cube, through the applied tune. The ceiling is derived
  from the applied candidate's trusted floor (ADR-0256).
- **Layers by graph composition.** A capture for layer N plays through N and
  below and nothing above (`docs/measurement-loop-doctrine.md` §1a). Layers:
  1 speaker (gated, angles) · 2 bass (in-room, seat cube, volume-scheduled) ·
  3 room (in-room, seat cube, per cabinet) · 4 preference (never measured).
- **Two keys, always.** Output side × driver role (ADR-0258). Cardioid is a
  variant of the bass role with its own per-region band, delay, polarity, level.
- **Wired only** (ADR-0255). The Pi plays and records; the browser is a
  position-ready walk.
- **Code computes and protects; the LLM judges.** Hard stops are the closed list
  in `AGENTS.md`: hearing, hardware damage (excitation caps, driver bands,
  limiter, the bass protection ladder), integrity of a claimed measurement.
  Everything else is a disclosure the LLM weighs.
- **No nearfield rung.** Bass extension is designed on the seat cube's median
  to an extended-corner target (Bank's own target shape). Protection comes from
  declared plant facts, the in-room distortion-versus-level ladder and the
  limiter evidence. A close-mic capture stays an optional diagnostic
  (`round-views close-reference`). (Wave 0b supersedes ADR-0257 §3's "nearfield
  fit stays as the protection basis".)
- **Build toward the toolbox shape** (owner ruling, right-size wave 2): every
  capability is a CLI verb + a banked artifact + a menu row + a methodology
  pointer. Programs, views and candidate kinds are the engine's extension
  points; this program adds rows there and changes no engine core.

## 3. The three programs and their seam

| Program | Owner | Owns |
|---|---|---|
| Right-size (cleanup) | its own brief | size and shape of `jasper/active_speaker/`; helper convergence; write-only records; the web-twin dissolution; decision D13 |
| Tuning flow | its own agent | the speaker stage as a product: walk, structure-first campaign (ADR-0203), views the operator reads, the side-solo capture graph |
| **Seat-matched tuning (this)** | this session + one fresh session per wave | everything below the ceiling and everything per cabinet: the seat-cube program, room views and candidate kind, bass views and scheduled candidate kind, the protection ladder, the runtime scheduler, per-side emission, the boundary prior |

Rule of engagement: this program adds programs to `measurement_programs.py`,
views to `ARTIFACT_BY_VIEW`, candidate kinds to the candidate bank, and stages to
the emitter. It does not restructure `crossover_v2_flow.py`, the session, or the
web twin. If a row needs an engine change, it is filed to the tuning-flow agent
as a request, not made here.

## 4. Engine facts this plan leans on (verified at main `a809d71c6`, 2026-09-08)

- Multi-position clouds and their combination exist in the speaker engine:
  `jasper/active_speaker/crossover_v2/spatial.py` (`cloud_position_*`,
  `cloud_validity_floor_hz`, `cloud_trusted_floor_hz`) and
  `jasper/audio_measurement/spatial_combine.py:1442` `combine_positions`.
- Programs are a registry with a pose vocabulary:
  `jasper/active_speaker/measurement_programs.py` (`ProgramPose`,
  `MeasurementProgram`, `_PROGRAMS`, `spot_program`). Poses are bearings today;
  the seat cube needs a seat-relative pose kind.
- The layering rule is enforced in graph composition:
  `jasper/active_speaker/crossover_v2/session_graph.py`.
- The applied candidate carries the trusted floor (`exclusion_evidence`) and is
  persisted whole by `persist_applied_baseline_profile`
  (`jasper/active_speaker/baseline_profile.py:3562`). Inside the engine no
  cross-package reader is needed.
- The active emitter has one room PEQ stage pre-split applied to both channels
  (`jasper/active_speaker/camilla_yaml.py:1790-1809`, `:1927-1931`); the flat
  emitter has a per-channel axis `room_peqs_right`
  (`jasper/sound/camilla_yaml.py:344-375`). Per ADR-0258 these converge.
- Headroom: one upstream gain absorbs room and linearization boosts
  (`active_baseline_headroom`); bass boost is emitted per driver after
  linearization (`camilla_yaml.py:497-610`). One budget is an emitter change.
- Wired capture kernel: `jasper/audio_measurement/wired_capture.py` (recorder,
  zero-run scan, integrity report); answer minting lands with PR #4138.
- The room product to be retired: `jasper/correction/` (28 files;
  `session.py` 2,364 lines; no operator seam) and `jasper/web/correction_{setup,
  room_flow,handlers,capture,tuning}.py` (~5.9k lines). Keep only the math
  already in `jasper/audio_measurement/` (`peq.design_peq`, `analysis`,
  `spatial_combine`) and `correction/variance_cap.py`'s rule as a view input.
- Bass: `jasper/bass_extension/` ~10.1k lines; `alignment.py`, `targets.py`,
  `adapters/*` are pure; `ladder.py` is a pure state machine; the bench
  executor's `PlayAndCapture` (`bench/executor.py:148`) and per-target
  `TargetPlan` binding are unbound (#1738). Deadness tests:
  `tests/test_bass_extension_plan_status.py` (stay until the first production
  caller, ADR-0257 §1).
- Distortion-versus-level exists: `jasper/audio_measurement/distortion.py`
  (ESS harmonic extraction) and `round-views distortion`.
- Declared geometry exists: `jasper-declare-geometry` →
  `/var/lib/jasper/measurement_geometry.json`.

## 5. Waves and rows

Tags: **P** program · **V** view · **C** candidate kind · **E** emitter ·
**D** delete · **A** ADR/doc · **R** runtime · **H** hardware session. **NN**
touches a non-negotiable: adversarial review plus an owner hardware pass; merge
held until the owner confirms. Lanes run in parallel on disjoint files; a lane's
rows run in order.

### Wave 0 — decisions (LANDED 2026-09-08, PR #4488)

ADR-0255 wired-only for every product · ADR-0256 derived ceiling, per-cabinet
room correction, median/σ/taper, D6/D7 reaffirmed, residual tier last ·
ADR-0257 bass resumes on wired capture, validated in-room, one headroom budget ·
ADR-0258 sides × roles, cardioid as a bass-role variant.

### Wave 0b — two more rows (docs only, one PR)

| Row | Concern | Tag |
|---|---|---|
| 0b.1 | ADR: room correction and bass are layers of the one tuning toolbox — programs, views, candidate kinds — not separate products. Supersedes ADR-0231 §5 (D11). Restates master-plan R13 as "the layer boundary is enforced by graph composition". Names what retires (`jasper/correction/` orchestration and its pages) and when (after Wave 1's hardware proof). Records the product consequence: the operator is the LLM for every layer; no operator-less room wizard. | A |
| 0b.2 | ADR: bass extension has no nearfield rung. Supersedes ADR-0257 §3's protection basis: declared plant facts + in-room distortion ladder + limiter evidence; the family is fitted on the seat-cube median to an extended-corner target; close-mic stays optional. Records Bank's own target (4th-order HP at 30 Hz) as the precedent. | A |
| 0b.3 | Pointer notes: methodology gains "Room" and "Bass" section stubs pointing at the ADRs; `room_boundary.py`'s roadmap paragraph is replaced by a pointer to ADR-0256 (docstring-only change, allowed in a docs PR). | A |

### Wave 1 — the seat cube is a program (lane: room capture)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 1.1 | Seat-relative pose kind in `measurement_programs.py` and a `seat/cube` program: head + six face centres at 30 cm (D&D geometry; D7's six-position default becomes seven with the centre). Prompts name the position in plain words. The walk page's position-ready flow drives it, human mover. | P | program listed by `jasper-angle-capture plan`; a fixture round banks seven takes with `pose_kind: seat` | code-review |
| 1.2 | Ungated analysis option on banked takes (the gate is an analysis choice, not a capture choice) and the room views: `room-median` (median, spread, per-position deviation), `room-persistence` (which features hold across ≥ N positions), `room-ceiling` (the applied candidate's trusted floor clamped per ADR-0256, fallback disclosed). | V | three `ARTIFACT_BY_VIEW` rows; inventory lists them; stdout obeys ADR-0237 | code-review |
| 1.3 | First wired seat-cube session on jts3 through the applied tune. | H | banked round with seven takes; integrity report clean | owner |
| 1.4 | Retire the room product: delete `jasper/correction/` orchestration (session, wizard state, browser capture, acceptance ladder, status, envelope) and `jasper/web/correction_{setup,room_flow,handlers,capture,tuning}.py`, their JS and their tests. Keep `variance_cap.py`'s rule as a view input (move to `audio_measurement` or the view). SUPERSEDED verdicts per module in the PR body. | D | grep proof; the household mic record survives (ADR-0255 §3); `test_correction_boundary_ssot` updated for the removed package | code-review; D-tier |
| 1.5 | Runbook and methodology: "Room" section replaces the stub; menu regenerated. | A | menu `--check` | sanity |

### Wave 2 — the room candidate kind (lane: room algorithm)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 2.1 | Layer-3 candidate kind: cuts-only bells on the program bus below the ceiling, one set per side (mono = one side). Code-computed limits: per-bin cut depth from spread (the variance-cap rule), taper to flat over ~1/3 octave below the ceiling (built into `design_peq`'s per-bin arrays, no API change), boost admission per the regime plan's D5 (persistent in ≥ 5 of 7, modally plausible, N ≥ 3, capped, cost disclosed). The LLM authors the candidate inside these limits; `propose`/`stage` doors validate it. | C | door refuses out-of-limit filters with codes; fixture candidate applies and round-trips through the emitter reader | code-review high |
| 2.2 | `room-grade` view: the re-measured cube against the room target below the ceiling, with the incumbent for comparison; regression surfaces as a disclosure, restore is the existing doctrine path. | V | fixture round grades; no auto-revert machinery | code-review |
| 2.3 | Boundary prior view: from declared geometry (speaker-to-wall distances, seat distance) predict the 2π/4π gain step and the quarter-wave null frequency (c/4d) per wall. Advisory; the LLM reads it beside `room-median`. | V | fixture geometry → predicted null at 85 cm ≈ 100 Hz | code-review |
| 2.4 | Two seat-cube sessions on jts3: apply a Layer-3 candidate, re-measure, grade. Numbers replace the room product's placeholder thresholds in the view's disclosure bands. | H | two banked rounds | owner |

### Wave 3 — the bass candidate kind and protection (lane: bass)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 3.1 | Bind the bench: `PlayAndCapture` and the per-target `TargetPlan` binding against the engine's play path and the wired recorder; `jasper-bass-extension-bench --live` stops failing closed. | R | bench runs a synthetic target on a fixture; live path reaches the recorder | **NN** (excitation) |
| 3.2 | `bass-fit` view: fit the seat-cube median below the ceiling to an extended-corner target family (corner per rung), with the declared plant (sealed/ported/PR adapters as parameter models) supplying the excursion-versus-boost curve. Publishes per rung: filters, boost, headroom cost, predicted excursion margin. | V | fixture median → family JSON | code-review |
| 3.3 | Layer-2 scheduled candidate kind: the family keyed by listening level, for the bass owner (`bass_management_corner_hz()`), emitted as named biquads per rung. Apply admits a rung only with its protection evidence (3.4) attached. | C | door refuses an unverified rung; emitter round-trip | code-review high |
| 3.4 | Protection ladder as a code-owned program: stepped-level sweeps at the seat with distortion-versus-level per rung and the sustain test, banked as evidence per rung. Hard stop: a rung whose distortion rise or limiter evidence fails is not admissible at that level. | P | fixture ladder; refusal codes | **NN** |
| 3.5 | Runbook and methodology "Bass" section; menu rows. | A | menu `--check` | sanity |

### Wave 4 — bass evidence and runtime (lane: bass, after 3)

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 4.1 | Supervised limiter bench campaign on jts3 per `docs/bass-extension-waves/limiter-evidence-protocol.md`; one accepted, replayable bundle. | H | owner present, **NN** |
| 4.2 | Runtime scheduler: pure target selection with instant retreat and gated re-extend, patching the named rung filters; no new daemon; no added latency. | R | **NN**, adversarial review |
| 4.3 | First production caller lands; deadness tests die in the same PR; ADR-0257 §1 satisfied. | C | code-review |

### Wave 5 — one budget, one sequence (join)

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 5.1 | One headroom budget in the emitter: room + linearization + bass boost through one disclosed gain with its cost in maximum level; the WARN-only stacking goes. | E | **NN** |
| 5.2 | Runbook sequence: speaker tune → bass family → room candidate → bass re-check at the seat; each step names the program and views it uses. | A | sanity |
| 5.3 | Optional: `close-reference` on the woofer as the LLM's on-demand room-gain split. | V | code-review |

### Wave 6 — the pair and the 3-way

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 6.1 | Per-side room stage in the active emitter and its reader, converged with `room_peqs_right` (ADR-0258 consequence). Needs the side-solo capture graph (tuning-flow agent). | E | **NN** adjacency; after the emitter is quiet |
| 6.2 | One bass family per bass system with a per-unit fit check. | C | code-review |
| 6.3 | Cardioid variant: an emitter output of the bass role with per-region band, delay, polarity and level (in-phase below the LF corner for wall reinforcement; delayed and inverted in the mid band). Design conversation first; this row only when the 3-way exists. | E | design, then **NN** |

## 6. Lanes, gates, hardware

- **Lanes:** room capture (1.x) · room algorithm (2.1–2.3) · bass (3.x → 4.x).
  All three start after Wave 0b merges; 1.1 and 3.1 also wait for PR #4138.
- **Joins:** 2.4 needs 1.3; 5.1 needs 2.1 and 4.3; 6.1 needs the emitter free.
- **Hardware sessions on jts3, in order:** an adopted speaker tune from the
  structure-first campaign (tuning-flow agent) → 1.3 → 2.4 → 3.4 rungs → 4.1.
  The owner's time at the box is the critical path.
- **Contention:** the active emitter (`camilla_yaml.py`) is wanted by three
  programs; rows 5.1 and 6.1 schedule when it is quiet. The session and flow
  modules are the tuning-flow agent's; this program does not touch them.

## 7. Session protocol

- One fresh session per wave (or per lane inside a wave when lanes are
  independent). The orchestrating session writes the brief: reading list,
  verified `file:line` facts, rows, proofs, gates, report format. The brief is
  the only context the fresh session gets.
- Model split that worked in Wave 0: Sonnet agents verify every citation and
  sweep for stale prose; Opus reviews; the top model adjudicates premises and
  writes. For code waves: Opus implements, the top model designs and holds the
  non-negotiable review.
- Every session: `git fetch origin`; branch from fresh `origin/main`; verify
  premises first and stop a row whose premise is false; `scripts/test-fast`
  (final sentinel only); `/simplify` then `/code-review` medium; NN rows add
  `/adversarial-review` and wait for the owner's hardware pass; one PR per row
  or per tightly coupled row pair; line delta and verdicts in the PR body; no
  model identifiers in commits or PRs.
- After merge, the orchestrating session reviews the merged diff against the
  row before writing the next brief, and appends to §9.

## 8. Not in this program

FIR (waits for the structure-first campaign and an excess-group-delay
measurement) · the residual tier above the ceiling (last, if ever) · a crossover
finder · the cardioid channel's design · a database or memory service · new
`JASPER_*` knobs · any browser or relay capture.

## 9. Status log

- 2026-09-08: Wave 0 merged (PR #4488: ADR-0255/0256/0257/0258; pointer notes
  on the regime plan and the bass plan; 0018 superseded, 0222 amended). Owner
  redirected the program on the same day: room and bass become layers of the one
  toolbox (was: separate products), and bass has no nearfield rung (was:
  nearfield plant fit). Both recorded here as Wave 0b rows.
