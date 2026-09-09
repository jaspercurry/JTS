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

**Owner update, 2026-09-09:** §§1–3 and Wave 2b define the next delivery.
Earlier briefs and status entries describe earlier scope; refresh them before
dispatch. The 11-position cloud and earlier stereo room support are agreed.
Correction above the current room ceiling is a research question (§1c), not
an approved filter policy or a permanent exclusion from Room.

**Room delivery:** rows 2.6 and 2.7 are open as
[PR #4662](https://github.com/jaspercurry/JTS/pull/4662) and
[PR #4663](https://github.com/jaspercurry/JTS/pull/4663). Neither is a stereo
capture or hardware proof. The existing speaker-tuning owner keeps that flow.

## How to resume from a fresh session

1. `git fetch origin main claude/loudspeaker-tuning-architecture-iephfa`; read this
   file from that branch; read `AGENTS.md` at HEAD.
2. Read ADR-0255, 0256, 0257, 0258, 0259, 0260.
3. Find the next unstarted rows in §5. Each names its lane, tag, proof and gate.
4. Every wave is one fresh session per lane, working from a brief the
   orchestrating session writes; that session reviews the merged diff before
   the next brief. Verify every `file:line` at your HEAD — the tree wins.
   Do not reuse a rebase command from a handoff without checking current refs.

## 1. Vision

One methodology, one toolbox, three user programs. The code computes and
protects; an external LLM chooses experiments and judges the evidence; the
human places the microphone, starts each pose batch, and listens.

| Program | Question and output |
|---|---|
| Speaker tuning | Tune drivers, crossover, timing and direct-sound response from suitable on/off-axis measurements. Save the accepted speaker tune. Defer fine low-frequency EQ where gating cannot support it; keep driver protection throughout. |
| Room correction | Put the speakers in their final locations and measure around the listener's head through the saved speaker tune. Correct each cabinet's room response within its established capability. Low-frequency correction is the first delivery; evidence-based upper-band correction remains open (§1c). Save separate room filters per side. |
| Bass extension, optional | Use available level margin to extend bass at low and medium listening levels, with less extension as volume rises. Save a per-cabinet family and its tested level limits. Amplifier margin alone does not establish driver travel or thermal limits. |

All three use the same measure → analyze → propose → trial → re-measure loop
and the same explicit candidate adoption. Room does not depend on completing
bass extension. Extension can reuse compatible room evidence for fitting,
but needs its own level and sustain evidence. After adding extension, verify
the complete low-frequency chain and revise the room filters if needed.

Bank's [AES 134 paper](https://dsp.mit.bme.hu/userfiles/publikaciok/bank_aes134.pdf)
supports correcting direct sound and then the combined low-frequency response
through the first correction. Its 30 Hz target is an example, not a driver
limit. Multiple-position design is proposed in §4.3; spatial robustness and
formal listening tests remain future work in the paper. JTS adds its own
spatial program and protection work; it is not a proven copy of Bank or Dirac.

### 1a. The default room cloud

Use 11 positions around the head: a 3 × 3 grid at ear height, plus centre-above
and centre-below. Offsets are `(right, forward, up)` in metres. Start at 0.30 m:
all combinations of right/forward in `{-0.30, 0, +0.30}` with up `0`, then
`(0, 0, +0.30)` and `(0, 0, -0.30)`. Walk each front/middle/back row across
left/centre/right, then the two vertical positions. Keep speakers fixed.

This is the owner's chosen default, not a universal optimum. Dutch & Dutch's
[written guide](https://support.dutchdutch.com/how-to-take-your-musical-enjoyment-from-great-to-incredible-rew-roommatching-guide/)
uses seven positions for one listener and allows more; the recalled 11-point
video has not been identified. The current program remains seven-point until
row 2.7 lands; existing rounds retain their original coordinates. One registry
owns the new pose list and default spacing; the UI, CLI and LLM menu consume
it. Bank actual coordinates and program identity. Poses remain flexible.

At each position, measure left alone, then right alone under one Start grant.
Thus stereo has 11 placements and 22 baseline sweeps; mono has 11 sweeps.
Each cabinet plays its complete accepted speaker tune. Keep side, candidate,
played graph, scope, level and calibration distinct; repeats are not new
positions. Publish median, spread and individual curves per side and tune.
Use the actual unique-position count for persistence fractions, not a fixed
five-of-seven rule. Check the pair together when its combined response is the
remaining question; never substitute that capture for separate-side evidence.

### 1b. Entry and resume

The Room correction tab is a small entry page: **Copy instructions**, the
accepted speaker tune and room-session status, and **Continue measurement**.
The instructions identify the device/side, tune, saved round and toolbox entry
point, and link the one maintained method. They do not duplicate the runbook
or assume that a pasted prompt grants an LLM access to local tools. Use the
existing placement/Start screen, recording store, plots and apply/restore path.
No second wizard, LLM client, session runner or database. Bass extension is a
separate optional entry into those same tools; subjective voicing stays separate.

### 1c. Research: room correction above the current ceiling

The owner wants Room to consider useful mid/treble correction, informed by the
saved gated and off-axis speaker measurements as well as the seat cloud.
Dirac is a research reference, not a promised implementation match.
This resumes the question in `docs/room-correction-regime-plan.md` D2; its old
1 kHz proposal and numeric bounds are not newly accepted policy.

Compare compatible records of the exact applied tune over a shared trusted
band, with a disclosed scalar level reference. A seat magnitude minus a gated
magnitude is not a uniquely isolated room transfer function: distance, angle,
directivity and reflected energy differ. Do not use frequency-shaped alignment
to erase the deviation being tested, or infer phase cancellation from a spatial
magnitude median. Missing compatibility removes the attribution claim, not the
ability to take another useful measurement.

Research broad, spatially stable trends and bounded filters first, using the
speaker's direct-sound and off-axis evidence to interpret them. A flat gated
target is not automatically the right in-room target. Narrow moving dips do
not establish a correctable defect. Compare prediction, room measurements,
direct-sound effects and listening at matched levels; report each separately.
Uncertain benefit is a disclosure, not a safety stop. Exact filter bounds,
upper frequency, target and any phase correction remain to be decided from
the research and experiments; this plan changes no runtime ceiling or clamp.

Source basis: [Toole, JAES 2015](https://aes.org/publications/elibrary-page/?id=17839)
explains why an in-room curve alone cannot identify loudspeaker quality or a
unique correction. [Dirac's technical white paper](https://www.dirac.com/wp-content/uploads/2025/07/Dirac-Live-Whitepaper.pdf)
describes gain-limited, multi-position mixed-phase correction, including excess
phase common across positions. That is a manufacturer's method claim, not
independent proof of benefit for JTS; ordinary PEQ cannot claim the same phase
control. Research can begin now; any experiment needing a new filter/scope
first needs that bounded capability, not a blanket full-band enablement.

First experiment for 2.11: use raw curves from one compatible seat cloud and
the saved gated on/off-axis set, since the room-median artifact is already
cropped to today's ceiling. Fit all but one unique seat position and predict
the held-out position; repeat for every position. Compare the same low-band
candidate alone with that candidate plus a small, broad upper-band change.
Declare the listening-window weights, target, scalar level reference and
trial limits before fitting. Crop to common support and the gated trusted
floors; any gap above the current room ceiling remains a gap. Report held-out
seat error and changes to gated on-axis/off-axis response separately. A common
EQ cannot reduce the per-frequency seat spread in dB. These are offline
predictions, not proof of perceived benefit, phase correction, full sound
power, driver margin or realized DSP. A later live trial needs its own bounded
filter contract and matched-level listening. Missing identity is disclosed;
it removes the associated attribution claim rather than inventing provenance.

## 2. Principles (each has an ADR or a doctrine line)

- **One loop, one toolbox.** Speaker, room and bass are programs, views and
  candidate kinds of the one engine, not separate products. (ADR-0259, Wave 0b.)
- **Separate current policy from future scope.** ADR-0256 owns today's
  low-band ceiling. Disclose clamping, any gap below the speaker's trusted
  floor, and filter tails; do not promise exactly zero change above it.
  §1c explores an upper room tier without changing the accepted speaker tune
  or silently widening current emission. Amend the relevant ADR before that
  policy is implemented; the research does not hold up the low-band release.
- **Layers by graph composition.** A capture for layer N plays through N and
  below and nothing above (`docs/measurement-loop-doctrine.md` §1a). Layers:
  1 speaker · 2 bass · 3 room · 4 preference (never measured).
- **Flexible poses, categorized.** A take carries its kind, distance, bearing
  or seat offset, and window. Gated bearings at ~1 m answer speaker questions
  above the trusted floor; a close take at ~0.3 m is the room-suppressed
  reference (`round-views close-reference`, kept); the ungated seat cloud answers
  speaker-plus-room questions. The LLM reads the category and weighs the take.
  No pose is forbidden; none is required. (ADR-0260.)
- **Fit is not protection proof.** The seat-cloud fit includes room gain.
  It does not measure driver displacement or heat. Keep the digital boost
  budget, declared physical limits, distortion/level ladder and limiter
  evidence distinct; no invented excursion margin. Close-mic remains optional
  (ADR-0260); a protection revision belongs in its own ADR and reviewed row.
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

## 3. Engineering ownership (distinct from the user programs)

| Program | Owns |
|---|---|
| Right-size (cleanup agent) | size and shape of `jasper/active_speaker/`; helper convergence; write-only records; the web-twin dissolution; decision D13 |
| Tuning flow (its agent) | the speaker stage as a product: the walk, the structure-first campaign (ADR-0203), operator views, the side-solo capture graph |
| **Seat-matched tuning (this)** | the room cloud, per-cabinet room evidence/candidates/emission, bass fit and protection, optional extension schedule, shared headroom integration, entry page and upper-room research |

Rule of engagement: rows here add to `measurement_programs.py`,
`ARTIFACT_BY_VIEW`, the candidate bank, and the emitter. An engine change is a
request to the tuning-flow owner with concrete inputs, outputs and behavior
tests. Agree one writer before either lane edits shared files; an active branch
is not permission to overlap it. The engine owns protected capture, provenance,
candidate identity, trial, adoption and restore. Programs own poses, views,
targets and candidate vocabulary. Bass owns its protection evidence and level
policy, not another apply path. One emitter owns the complete graph and boost
budget. Saved speaker tunes are inputs to Room, not files Room retunes.

## 4. Current integration facts (main `f575fe364`, 2026-09-09)

Recheck at the implementation head; §9 retains the earlier history.

- Programs and pose coordinates live in `measurement_programs.py`; shared
  scopes and temporary graph ownership live in `measure_spec.py` and
  `crossover_v2/session_graph.py`. Raw takes retain candidate, played graph,
  microphone position, level and calibration. Extend these owners.
- Room candidate/apply is wired for mono. `measured_crossover_candidate.py`
  `_validated_room_correction` refuses stereo at lines 195–201 because the
  active emitter takes one room set. The flat emitter's `room_peqs_right` is
  not proof of stereo support here. Row 2.9 converges the two representations.
- `crossover_v2/room_views.py:155–192` deduplicates by `pose_id` and drops
  candidate/graph identity. An offline probe of `seat/cube` with two candidates
  yields 14 stop IDs at seven places and one pooled “14-position” median.
  Row 2.6 must fix this before room comparisons or bass fitting consume it.
- `room_ceiling.json` discloses the trusted floor and clamp. A 625 Hz trusted
  floor clamps to 500 Hz; no data earns the missing band by that clamp.
  `room_prescription.py` allows 0.5 dB at the taper; bell tails are not zero
  above it. Make these limits visible, not a claim of exact preservation.
- Shared capture/calibration is in `audio_measurement`; the former room
  orchestrator, embedded LLM client and bass wizard are retired. Room survives
  the current audition/recompose paths; that earlier follow-up is closed.
- `bass-fit` reads the same room-median artifact. `seat_fit.py:81–88` labels
  its level as digital only; no displacement margin is computed. Offline
  adapter probes under the 6 dB policy yielded 6.43 dB sealed and 9.20 dB
  ported boost. Row 3.2b covers the actual response cap, not just target labels.
- Bass candidate/emission is open as #4660, not landed. #4643 is the open
  bench seam; its graph protection and role-completeness findings remain.
  Its sustain body cannot use the 6–12 s sweep limit for a 30–90 s hold.
- No production bass scheduler is proved. Main has the room/speaker dB
  headroom machinery; row 5.1 adds the extension charge before runtime use.
  A family fit and passing software tests are not a hardware campaign.

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
| 1.6 | — | First wired seat-cloud session on jts3 through the applied tune, after 2.6/2.7. | H | banked 11-position mono round, integrity and identities correct | owner |
| 1.7 | B | **LANDED 2026-09-08 — PR #4525 merged (`0cbed8a57`).** Runbook "Room" section; menu regenerated. | A | menu `--check` | sanity |

### Wave 2 — the room candidate kind (lane C; starts after 0b on fixtures)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 2.1 | **LANDED 2026-09-09 — PR #4544 merged (`b083c06c8`).** Layer-3 candidate kind: cuts-only bells on the program bus below the ceiling, one set per side (mono = one side). Code-computed limits: per-bin cut depth from spread, a taper to flat over ~1/3 octave below the ceiling (in `design_peq`'s per-bin arrays), boost admission per the regime plan's D5 (persistent in ≥ 5 of 7, modally plausible, N ≥ 3, capped, level cost disclosed). The LLM authors inside the limits; the `propose`/`stage` doors validate. | C | door refuses out-of-limit filters with codes; fixture candidate round-trips through the emitter reader | code-review high |
| 2.2 | **LANDED 2026-09-09 — PR #4546 merged (`7db6bca73`).** `room-grade` view: re-measured cube against the target below the ceiling, incumbent beside it; regression is a disclosure; restore is the doctrine path. | V | fixture grades; no auto-revert machinery | code-review |
| 2.3 | **LANDED 2026-09-09 — PR #4520 merged (`d7d5fdc1e`).** Boundary prior view: from declared geometry predict the 2π/4π gain step and the quarter-wave null (c/4d) per wall. Advisory. | V | 85 cm → ≈100 Hz null on a fixture | code-review |
| 2.4 | Mono room loop after 1.6: trial a small cut through the accepted speaker tune, re-measure the same cloud, grade, adopt or restore. Room works without bass extension. | H | identified before/after rounds; saved graph and normal playback readback; disclosed coverage | owner |

### Wave 2b — complete the room program (next, with lane D in parallel)

These are new rows, not claims that the earlier mono work supports stereo.
Speaker tuning keeps its current owner. Coordinate shared changes under §3.

| Row | Concern | Tag | Proof / dependency |
|---|---|---|---|
| 2.5 | **PARTIAL — ADR-0277 in PR #4663 records the 11-position default and preserves saved cubes.** Record other program decisions in append-only ADRs where needed. Resolve cabinet/side identity and the per-role vs per-cabinet trim ambiguity with the speaker owner. Upper-band policy stays open under 2.11. | A | Current decision and implementation scope agree; no retroactive rewrite of ADR history. |
| 2.6 | **OPEN — PR #4662.** One room summary per declared side, exact candidate, played graph, scope, level and calibration reference. Reuses the canonical record reader; keys positions by physical pose, discloses repeats/unusable takes and common valid frequency coverage. Prescription, grade and bass fit carry the selected basis. Current records without a side or resolved calibration status report those facts as unknown. | V | Two candidates at seven poses yield separate seven-position summaries; retakes do not add positions. Narrow measured coverage still checks filter tails against the complete room policy band. |
| 2.7 | **OPEN — PR #4663.** Registers the 11-position default (§1a) once; old `seat/cube` and `seat/express` identities retain their coordinates. Prompts/counts come from the registry; the runbook and generated menu agree. Counts use unique poses per side and tune; thresholds use the existing fraction policy and actual count. | P | Fixture preview and staged records agree on 11 mono sweeps. No hardware walk yet; stereo reaches 22 only after 2.8. No duplicated product pose list. |
| 2.8 | Side-solo capture through each cabinet's accepted speaker tune, left then right at a held pose. Extend shared scope/routing with the speaker owner; account for side-specific level/trim and protection. | P | **NN**: actual graph mutes the other side and preserves all driver protection; receipt identifies the side; one Start per pose batch. |
| 2.9 | **Moved from 6.1.** Per-side room emission and extraction, converged with `room_peqs_right`. Keep every side's evidence and filters separate; use a common target only over supported coverage. Extend the existing budget, apply and restore owners. | E | **NN**: distinct left/right filters survive candidate fingerprint, emission, readback, trial, apply and restore; mono behavior preserved. Remove the stereo refusal only once this path exists. |
| 2.10 | Small Room entry/resume page (§1b), generated tool entry and optional method pointers. Use existing session, plots and human placement screen. | P | Fresh and resumed sessions identify the same saved tune/round; copied instructions refer to current tool contracts; no second state owner. |
| 2.11 | Research upper-room correction (§1c): speaker/seat compatibility, directivity, target, useful bandwidth and broad bounded candidates. Compare low-band-only with a proposed upper-tier trial; room score, direct/off-axis effects and listening stay distinct. | V/A | Cited research and one bounded experiment design with open limits; amend the ceiling/filter contract only if implementation is chosen. Research does not block 2.4/2.12. |
| 2.12 | Stereo room acceptance after 2.4 and 2.8–2.10: measure 11 positions per side, trial separate corrections, compare and verify the saved pair. Add a both-playing check where it answers a remaining question. | H | Separate side evidence, useful before/after result and normal-playback readback; no claim of stereo completion from the mono trial alone. |

### Wave 3 — the bass candidate kind and protection (lane D)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 3.1 | Bind the bench: `PlayAndCapture` and `TargetPlan` against the engine's play path and the wired recorder; `jasper-bass-extension-bench --live` stops failing closed. | R | fixture run; live path reaches the recorder | **NN** |
| 3.2b | **Gates 3.3.** Enforce the margin policy on each adapter's actual generated response: sealed subsonic ratio/order must follow the policy; sealed and ported/PR boost must obey its cap. Keep the physical-protection and digital-budget claims distinct. Rebuild affected families after correction. | C | Parameterized policy/response behavior, including low-Q sealed and ported/PR cases; actual protective-filter values survive emission. | **NN** |
| 3.2 | **LANDED — PR #4641, `4157a6030`.** `bass-fit` fits an effective in-room response and publishes a family, filter boost and digital level bound. It does not publish measured driver displacement, heat limits or excursion margin. Compatible room evidence supplies the fit, not protection proof. | V | fixture median → family JSON | code-review |
| 3.3 | **OPEN — PR #4660.** Layer-2 candidate family for the bass owner, with evidence-bound rungs and named filters. Review current refs afresh; the old rebase recipe is stale. Preserve natural-at-rest until the runtime work is complete. First emission support is sealed; ported/PR analysis does not imply runtime support. | C/E | door refuses an unverified boosted rung; emitter round-trip; one candidate authority replaces legacy profile/apply intent | **NN** |
| 3.1c | Distinct sustain-hold limit in the driver-safety schema and fingerprint; sweep limits stay separate. Fix the known activation/readback issue #2202 before the supervised campaign. This is required before a 30–90 s hold, not permission to raise sweep caps. | P | Hold and sweep admission use their own declared/code-side limits; graph proof matches live readback. | **NN** |
| 3.4 | Protection ladder as a code-owned program: stepped-level sweeps at the seat, distortion-versus-level per rung, the sustain test, evidence banked per rung. A failing rung is inadmissible at that level. | P | fixture ladder; refusal codes | **NN** |
| 3.5 | Runbook "Bass" section; menu rows. | A | menu `--check` | sanity |

### Wave 4 — bass evidence and runtime (lane D, after 3)

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 4.1 | Supervised limiter bench campaign on jts3 per `limiter-evidence-protocol.md`; one accepted, replayable bundle. | H | owner present, **NN** |
| 4.1b | **The contract revision (added 2026-09-09).** The limiter-evidence protocol blocks all wave-4 production wiring until an ADR names the accepted bundle's exact `evidence_fingerprint`, records an independent review at zero blockers, and authorizes a named trusted caller; the tap-realization amendment separately forbids "a scheduler" by name. 4.2 and 4.3 cannot start before this merges. | A | owner sign-off |
| 4.2 | Runtime scheduler after 4.1b and 5.1: pure target selection, instant retreat, gated re-extend, patching named rung filters from tested level limits. Use the canonical fader's dB; no new daemon or added latency. | R | **NN**, adversarial review |
| 4.3 | First production caller lands (the engine's apply of a scheduled candidate). | C | code-review |

### Wave 5 — one budget, one sequence (join)

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 5.1 | **Before 4.2/4.3.** One dB level/headroom contract and emitter budget for speaker + room + bass boost. Declare the conversion from the fit's 0–100 level to runtime dB once; charge the complete active rung's response. Physical limits remain separate from digital capacity. | E | **NN** |
| 5.2 | Runbook: accepted speaker tune → room correction → optional bass extension → complete-chain room/bass recheck. Reuse compatible evidence; changed lower layers require a new identified validation, not a fresh speaker campaign. `close-reference` stays optional. | A | sanity |

### Wave 6 — remaining bass variants (room stereo moved earlier)

| Row | Concern | Tag | Gate |
|---|---|---|---|
| 6.2 | Per-cabinet bass fit/family and protection evidence for stereo extension. Shared driver models do not prove equal safe level limits. Required before calling stereo bass extension complete; independent of the earlier stereo room release. | C/E | **NN** where emission/protection changes |
| 6.3 | Future rear bass output with its own band, delay, polarity, level and protection. Keep side, driver role and physical output distinct; resolve the current one-output-per-side/role validator when this design starts. Do not assume a directivity pattern or implement cardioid control now. | E | design, then **NN** |

## 6. Lanes, gates, hardware

Wave numbers retain history; this is the current order. Do not restart the
landed retirement or speaker work. Two bounded work lanes can proceed:
Room completion (2b) and bass protection (3); upper-room research is read-only
until its experiment scope is settled. Shared files have one assigned writer.

1. Refresh the affected briefs against this plan and current refs. Keep the
   existing speaker owner in charge of its engine; agree the side-solo and
   evidence contracts before editing shared capture/emitter files.
2. Room: 2.5 → 2.6 → 2.7 → 1.6 → 2.4 for the first mono proof. Build 2.8/2.9
   with the shared owner and 2.10 at the UI boundary, then 2.12 for stereo.
   Neither room proof waits for bass extension, cardioid or upper-band EQ.
3. Bass: close #4643's graph-backed per-driver protection and completeness
   findings; land its part-1 seam after reconciliation with main. Fix 3.2b
   before 3.3 emission. Then 3.3 → 3.4a rung graph → 3.4b ladder evidence →
   3.1 part 2 live binding → 3.5. Add 3.1c before any sustain campaign.
4. Hardware extension: accepted speaker tune and identified seat evidence →
   protected rung measurements (3.4) → supervised limiter campaign (4.1,
   including #2202 fixed) → contract revision (4.1b). Do not infer a safe
   listening level from the seat fit's digital bound.
5. Join the dB budget in 5.1 after the candidate shape is settled and before
   runtime scheduler/caller 4.2/4.3. Then validate extension through the full
   room chain and publish the optional program sequence (5.2).
6. Per-cabinet stereo extension (6.2) and the rear-driver project (6.3) follow
   when their hardware is available. They do not defer stereo room correction.

Inspect current open PRs before assigning a writer: #4625 overlaps the bench;
#4565, #4569 and #4588 overlap emitter/baseline work. These are collision
pointers to recheck, not permanent dependencies or an instruction to stop
other agents. #4660 now has a PR; review its current diff, not the handoff's
old 13-commit rebase command. Record what actually lands in §9. Refresh issue
#4502 when authorized to publish a status update; its old body is not current
implementation evidence.

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

## 8. Deferred implementation

Upper-room correction is in research scope (§1c, 2.11), with no new bandwidth,
phase, filter or target policy yet. Room FIR remains deferred. Cardioid design
waits for the rear-driver project. No crossover finder, new database/memory
service, `JASPER_*` knob, browser/relay capture, embedded LLM client or
operator-less wizard is introduced by this plan.

## 9. Status log

- 2026-09-09 17:50Z: **Room foundations opened, not landed.** PR #4662
  selects compatible room records, counts physical positions, reports usable
  coverage and carries the evidence basis into prescription, grade and bass
  fit. PR #4663 adds the default 11-position cloud and ADR-0277 while keeping
  old cube/express records unchanged. Native medium reviews completed; the
  room review's filter-tail finding is fixed and covered by cut/boost cases.
  Fast lanes passed (8,245 room; 5,439 cloud); full merge checks remain open.
  Both PRs hit a runner package-index failure before CI tests; the existing
  repair is #4664. No deploy, audio playback or hardware walk occurred.
  Next: land these after green checks, run 1.6/2.4 with the owner, and agree
  the shared side/trim contract before 2.8/2.9. Room entry and upper-band
  research remain 2.10/2.11; this does not advance bass protection/runtime.

- 2026-09-09 17:00Z: **Owner-approved program update (plan only).** Speaker
  tuning, room correction and optional level-dependent bass extension share
  one toolbox. Room's default becomes the 11-position cloud in §1a; stereo
  measures each side at each place (22 baseline sweeps). Wave 2b brings
  side-solo capture and per-side emission into the first complete room release
  (former 6.1 is now 2.9), adds the small LLM entry/resume page, and puts the
  candidate-mixing fix before room evidence is reused. Room works before
  bass extension; 5.1's common dB budget now precedes the runtime scheduler.
  Corrected 3.2's false excursion-margin claim and added the distinct sustain
  row 3.1c. Higher-frequency room correction is explicitly open for research
  using compatible gated/off-axis and seat evidence (§1c, 2.11); no upper band,
  gain limit or phase algorithm is approved by this update. Cardioid stays
  deferred. Main rechecked at `f575fe364`; no product code, deployed tune,
  clamp, ADR history or hardware state changed. Existing briefs need a scope
  refresh before new dispatch; older entries below remain historical.

- 2026-09-09 16:45Z: **Row 3.3 opened as PR #4660** (`+3198/−1846`, 48 files,
  11 commits, base `main`). The agent did not go quiet as an earlier entry
  assumed — it ran ~2.7 h and reported in full. It needs
  `/adversarial-review` and the owner's hardware pass (NN tier, DSP output
  path); neither has run. Highlights a reviewer should check first:
  - **The brief's premise that `_assert_bass_extension_safe` is unchanged is
    false — it was re-plumbed**, but the safety logic is byte-identical (the
    strict `LT → subsonic → limiter` index assertion and the limiter params);
    the two hunks are the evidence source and `block["roles"][0]` →
    `block["role"]`, the same value. The re-plumb derives authority from the
    candidate's own field rather than from a summary of the block the emitter
    just built — a strengthening.
  - **A fail-closed behaviour change worth the adversarial pass:** where main
    returned "no block" for a non-`sealed_v1` profile — silently emitting a
    graph with none of the protection the profile described — an unemittable
    field now raises and refuses the whole graph.
  - **`/state.bass_extension` was superseded before it shipped, correctly.**
    ADR-0270 (#4652, merged 16:00Z) caps `/state` at thirteen keys, so the
    wiring was dropped and `control/state_aggregate.py` is 0 lines changed; the
    fact stays in `setup_status.bass_extension_state`, its owning module.
    **Verified independently: ADR-0270 is on main.**
  - `jasper/bass_extension/apply_intent.py` deleted whole (168 lines) once the
    dual-authority accommodation went; the `sealed_v1` tripwire replaced by an
    emission-support contract (armed set == adapters whose alignment the field
    reader recomputes, every other registered adapter refused behaviourally).
  - Two tightenings beyond the brief: a transform declaring
    `boost_headroom_db: 0.0` while carrying a real `LinkwitzTransform` is now
    unpersistable (it would have slipped the protection stop), and the doctor
    fails on an unreadable family instead of reporting "not commissioned / ok"
    on a speaker whose graph the runtime proof is already blocking.
  - `LADDER_INCOMPLETE` is deliberately kept with no producer today, with a
    comment naming its removal condition. **Do not re-delete it on a
    no-producer scan** — its producer is row 3.4b.
  - A sixth container flake seen twice and not since:
    `test_run_crossover_round.py::test_the_runner_never_claims_the_park_it_cannot_see`,
    a 120 s-vs-90 s ssh-timeout race under parallel load; the file passes whole
    and standalone on the branch and its base.

- 2026-09-09 16:00Z: **B1 re-check verdict on #4643: the deviation is justified,
  the defect is NOT closed. The PR does not merge.** Two new blockers, and the
  orchestrator's 14:05Z ruling is formally superseded.
  - **The ruling was unimplementable, for a reason neither earlier review
    found.** The summed admission gate hardcodes
    `bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY`, so
    `bass_extension_block_valid` demands the *complete absence* of every
    `bass_ext*` filter. Probed against the suite's own live graph:
    `valid=False, reason=bass_extension_block_forbidden,
    definitions=('bass_ext_lt','bass_ext_subsonic')` → `GRAPH_NOT_PROVEN`. The
    gate can never admit any bench program on any bench graph at any level.
    The fix agent's own arguments were partly wrong too: its mono-VERIFY point
    holds, its "every pass refuses" was overstated (a derived HF ceiling
    supersedes the declared −65 dBFS where both sensitivities are declared),
    and its claim that `protection_requirement_present` credits a dB figure is
    **false** — that check proves the declared filter's type, cutoff and slope
    are present in the graph text, which is exactly the proof this row wants.
  - **B1a — the replacement's protection excuse is tautological and
    unverified.** `apply_driver_low_limit` writes the high-pass cutoff and
    `hard_excitation_band_hz[0]` from one number, so on the
    `program_admission=True` path an HF role's test reduces to
    `frequency >= frequency` and can never refuse a normally-authored tweeter
    (fixture: floor 1500.0, cutoff 1500.0). Nothing checks the declared filter
    against the graph that will play — **and the proof is now available**:
    `graph_yaml` is threaded in, and `view_from_camilla_dict(yaml.safe_load(...))`
    is the parse `activation._prove_active_graph` already performs. Sharper
    still, the bench passes `program_admission=True`, the *proven* protective
    high-pass path, taking the ceiling loosening without supplying the proof.
  - **B1b — the completeness check was dropped with the summed gate.** Any role
    absent from `role_targets` passes silently (probed: drop `"tweeter"` and
    the guard is quiet); the summed gate's
    `set(role_targets.values()) != set(physical)` leg was the only thing
    catching it. Out-of-band drivers are likewise unguarded.
  - Confirmed closed: the band margin (`ceiling = min(corners)/2`, boundary
    200.0/200.001, correct for LR4 and LR8) — but `SUPPORTED_LR_ORDERS`
    includes 2, an LR2 pair is only −14.0 dB at fc/2, and the code ignores
    `region.order` while its comment names LR4. Nothing new was broken by the
    binder consumption: `confirm_graph_is_live` → fader proof → aplay now sit
    inside the writer lock, matching the wizard's own nesting.
  - **M1 part 2 remains open**: the preflight refuses at 6–12 s against the
    protocol's 30/60/90 s holds, so the campaign still cannot run.
  Recorded ruling, superseding 14:05Z: **per-driver caps do not come from the
  summed admission gate — it cannot admit a bench graph at all. The bench's own
  guard is the right shape, but it must prove the declared protection filter
  against `graph_yaml` rather than trusting the declaration, and must fail
  closed on a role it has no target for.**

- 2026-09-09 15:45Z: #4643's fix round landed at `af3e7a79a` (conflict with the
  merged 3.2 resolved, main merged in twice, never rebased). Three of the six
  findings are closed as instructed, and three produced findings of their own
  that matter more than the fixes:
  - **The bench still cannot run the protocol's sustain hold, and now refuses
    it honestly instead of failing at the speaker.** The preflight
    (`bench_hold_exceeds_declared_duration`) fires before the fader leaves the
    floor. But `effective_sweep_duration_limit_s` is
    `min(declared, driver_sweep_duration_s(role))` and the code-side table caps
    a woofer at 12 s, so no owner authoring reaches the protocol's 30/60/90 s.
    The fix agent refused both available shortcuts — raising the cap, and
    describing the hold as a chain of ≤ 4 s sweeps, which changes no thermal
    exposure — and named the honest fix: **a sustain-hold limit distinct from
    `max_sweep_duration_s` in the driver-safety profile**, a schema and
    fingerprint change deserving its own reviewed PR. **This is a new required
    row before wave 4.1 can run**, alongside `activation.py:186`'s pre-existing
    #2202 defect.
  - **The adversarial review's memory blocker was overstated, and the fix agent
    said so with numbers.** The ~200 MB figure is the cost of correlating the
    whole capture, but the caller's existing slice already bounded it to ~24 s;
    measured peak on a 90 s hold was 50.3 MB before and 48.5 MB after. The real
    defect was the inert backstop, now restored. Recorded because a fixer
    correcting a reviewer with measurements is the behaviour this process wants.
  - **The orchestrator's B1 ruling was declined, with evidence.** The ruling
    said per-driver caps must come from the summed admission gate. The fix agent
    found that gate applies `peak <= cap` to every role unconditionally, and
    against the repo's own fixture the tweeter cap is −65 dBFS against the
    bench's −55 dBFS effective, so **every bench pass would refuse** — for
    content two octaves inside the tweeter's stopband; and that crediting the
    declared high-pass with a dB figure is the separately-reviewed protection
    model the frozen protocol excludes. It closed the defect instead with
    `assert_driver_caps_evaluated`, resolving every role through
    `resolve_driver_excitation_ceilings` and judging a driver against its cap
    when the stimulus band reaches it, requiring a declared protection filter
    otherwise. **A focused adversarial re-check of that deviation is running;
    #4643 does not merge until it returns.** If it confirms the numbers, the
    ruling in the 14:05Z entry is superseded by this one.

- 2026-09-09 15:35Z: Handoff refreshed against verified state (`origin/main`
  `4157a6030`). Two facts a successor must not get wrong:
  - **Row 3.3 has a branch nobody has reviewed.**
    `claude/seat-w3-3-3-bass-candidate-kind` (`732cfd388`, 13 ahead, no PR) was
    built on row 3.2's pre-squash branch, so its first three commits are 3.2's
    content now on main; replay only its own ten with
    `git rebase --onto origin/main 6d7bb96ff`. Those ten go well past the
    original 3.3a/3.3b — they also delete the apply-intent record and retire
    the legacy profile's authority — and the agent that wrote them never
    reported, so the earlier pre-read describes the *older* branches, not this
    one. Review it as new work. It conflicts with main in the runbook menu,
    `bass_extension/profile.py` and `tests/test_active_speaker_baseline_profile.py`.
  - **#4643 is conflicted against main** now that 3.2 landed (both touch
    `adapters/sealed.py` and `alignment.py`). Resolving it must not undo 3.2's
    two corrections: `PortedAdapter.required_captures` stays
    `(CaptureRole.WOOFER_NEARFIELD,)`, and `sealed.fit_plant` keeps its
    `ValueError` guard.
  Also recorded: every CI "failure" on #4643's intermediate heads today was a
  run *cancellation* caused by the fix agent's own next push, verified from the
  job logs, not a red test. Judge that PR only on a run that completes
  undisturbed on its final head.

- 2026-09-09 15:25Z: **Row 3.2 LANDED**: PR #4641 squash-merged at `4157a6030`
  (+1119/−38 over 16 files) after a Sonnet claim check (8/10 pass
  independently, including a byte-for-byte `--dry-run` comparison against main
  and a live end-to-end run), an Opus design review (REQUEST CHANGES, three
  must-fixes, all fixed and re-verified: vented admission 31/72 → 0/72; the
  published model now matches the plant the fit fitted, `max |model − plant|`
  0.0; the pointer to the deleted `ladder.py` gone), and one CI round lost to
  the orchestrator's own menu-staging error (see 15:12Z). `jasper-round-views
  bass-fit` is on main and the duplicate `room_median.json` parser is gone —
  the door's reader is the one reader tree-wide.
  Carried forward: row **3.2b** (the adapters' margin-policy divergence) still
  gates row 3.3; the missing excursion model is the owner's to rule on against
  ADR-0260.

- 2026-09-09 15:20Z: Wave 5/6 fact base gathered and banked at
  `facts/wave-5-6-facts.md` (verified at main `d112fa5a1`); wave 4's is at
  `facts/wave-4-facts.md`. Both are raw material for the briefs, not doctrine
  — re-verify before writing. The four findings that change what those waves
  are:
  - **5.1's real problem is units, not plumbing.** Room correction and
    linearization disclose their cost in dB; bass discloses only a 0-100
    `max_listening_level` and a margin-policy name, never dB. "One disclosed
    gain with its cost in maximum level" first needs the three layers to speak
    one unit.
  - **5.2 writes from nothing.** The runbook has a speaker flow and a Room
    section and zero bass guidance.
  - **6.3 (cardioid) is not merely undesigned, it is blocked in code.**
    `ActiveChannelMap.validate_for_way` requires the output set to equal
    exactly one output per `(side, role)` pair and raises on a duplicate — and
    a cardioid bass role is a second output of the same role. That validator
    is the design conversation's first subject.
  - **Two contradictions with ADR-0258**, both recorded for the owner:
    per-role facts (crossover, delay, gain, polarity) apply identically across
    sides today, contradicting ADR-0258 rule 3's claim that level trim is a
    per-cabinet fact; and the flat emitter's `room_peqs_right` is documented
    as a multi-room leader/follower axis, not the stereo-pair side axis
    ADR-0258 frames it as. 6.1 cannot converge with it until that is settled.
  Also confirmed: wave 5's stop-and-report condition (a boosted rung reaching
  CamillaDSP with no headroom charge) **cannot fire at HEAD**, closed three
  independent ways — the profile invariant forcing the natural target to zero
  boost, the emitter reading only `targets[-1]`, and `graph_safety`'s own
  `boost != 0.0` re-proof from the rendered graph text. It becomes live only
  when row 3.3's emission lands, which is exactly why the wave-4 brief carries
  it as a stop-and-report.
- 2026-09-09 15:12Z: #4641 went red on the merge-in, and it was the
  orchestrator's own error, not the branch's: the runbook conflict was
  resolved by staging one side and regenerating afterwards, but `git commit`
  during a merge commits the index, so the regeneration never landed and the
  committed cell carried `bass-fit` without main's `windows`. Both failures
  (`test_the_committed_runbook_table_equals_the_regenerated_one`,
  `test_check_mode_agrees_and_writes_nothing`) had that one cause. Fixed in a
  follow-up commit, verified against the committed tree rather than the
  working tree. **Note for every future rebase in this lane:** after
  regenerating the menu, `git add` it before committing, and verify with
  `git show HEAD:docs/tuning-operator-runbook.md`, not with a working-tree
  check — a working-tree `--check` passes while the commit is still wrong.

- 2026-09-09 15:15Z: **Orchestration handed off again** (owner switched agents
  mid-session). `briefs/HANDOFF-ORCHESTRATOR.md` rewritten for the successor:
  it carries the corrected branch topology, the seven rulings this session
  made, the findings still open, the review pattern that produced them, and a
  corrected container recipe (pycamilladsp must be installed WITH its
  dependencies — the previous handoff's `--no-deps` fails on
  `websocket-client` — plus a fifth tolerated uid-0 test,
  `test_audio_hardware_reconcile.py::…[mid_stage_abort-1]`, confirmed on clean
  main). State at handoff: row 3.2 (#4641) reviewed, fixed and merging; row
  3.1 part 1 (#4643) open with four must-fixes and two adversarial blockers
  whose fix round was in flight — **not mergeable as it stands**; rows 3.2b,
  3.3, 3.4a, 3.4b, 3.1 part 2 and 3.5 not landed. Wave-4 brief written; waves
  5 and 6 not written.

- 2026-09-09 15:05Z: #4643 adversarial review (NN tier): **two blockers, both on
  the excitation path, both probed rather than argued**, sent to the fix agent
  with the design review's four.
  - **Non-owner drivers get the same bytes with no cap evaluated.** The seam
    re-admits through `readmit_program_from_wav`, the *isolated-driver* gate,
    declaring both source channels as the bass owner's role;
    `_evaluate_program:493` resolves a cap only for roles present in
    `channel_roles`, so mid/tweeter/mains caps are never consulted — while the
    artifact is a 2-channel mix through the full installed crossover, the case
    the engine routes to `readmit_summed_program_from_wav:667` (peak ≤ cap for
    *every* role, plus `protection_requirement_present` proven off the graph
    text). Adjudicated where the two reviews disagreed: the design review
    called `assert_stimulus_band_protected` the right compensating stop; the
    adversarial review is right that it is not one, because it compares against
    `preset.crossover_regions` — a declaration, not the graph that will be live
    — and drops the per-driver caps. It also has no margin: its own comment
    says the band must stop short of the corner and the test is
    `>= min(corners)`, so `band=(100.0, 399.999)` passes against `fc = 400 Hz`,
    driving the tweeter ~6 dB below the woofer's stress level with no cap of
    its own. The per-role caps come from the summed gate; the band check stays
    only if it earns its place, with its edge fixed and pinned.
  - **The fader level and the live graph are proven before the lock and before
    admission, not when audio is emitted.** The engine puts both proofs inside
    the lock immediately before aplay (`composition.py:136`); the bench's
    `_play_wav_polled` runs neither, and the raise happens ~3 steps earlier.
    Not hypothetical: `measurement_hold.py` is scoped to source-observed volume
    changes only, and `conductor_context.py:521` records that a deliberate
    household "louder" still moves the fader mid-session. This falls out of the
    design review's binder fix by construction.
  Recorded for the owner ahead of wave 4.1: `activation.py:186` carries a
  pre-existing issue #2202 defect the reviewer says will block the first
  supervised campaign regardless of this row. Out of scope here, named now.
  Everything else in the diff was found sound: clamps, SPL stop, secrets, env
  writers, and the five promotions; the duck-first give-back order was
  independently confirmed sound by both reviews.

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
