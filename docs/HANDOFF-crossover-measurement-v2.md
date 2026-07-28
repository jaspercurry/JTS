# Handoff: crossover measurement v2 — the conductor flow

The v2 flow measures and applies a fully-active 2-way crossover's
**level, delay, and polarity** from a **guided spatial cloud** — 16
captures at the **Full tier's** defaults, walked through a handful of
prompted microphone positions around one mark. (It was three captures at
a single fixed position until flat-linearization PR-3b made the cloud the
measurement; see "Position groups" below for what did and did not move.)
Since the flow-simplification work order's PR-U1
([`flat-linearization-flow-simplification-plan.md`](flat-linearization-flow-simplification-plan.md))
there is also an **Express tier** — 7 captures, a 4-position pre-apply
cloud and a mark-only post-apply check, no cross-position post-apply
claim. The household picks explicitly, every session, on the
`/correction/` wizard; the rest of this doc describes the Full tier's
walk unless a section says otherwise — read the plan doc for Express's
exact shape and its degraded-claims table.
The phone is a dumb recorder; the Pi is
the conductor; the analysis is a pure function of
`(ExcitationProgram, captured WAV)`. It replaces the legacy per-driver
near-field procedure, which never achieved a reliable end-to-end pass
on hardware. Canonical home for how v2 operates today — other docs link
here. The design/decision record (why it exists, the rejected
alternatives, the wave plan) is
[`crossover-measurement-productization-design.md`](crossover-measurement-productization-design.md);
this doc is the current operational truth.

## How to run it

- **Household surface:** `http://jts.local/correction/` → the crossover
  step. The screens are `speaker_setup → microphone_check → measure →
  apply → verify`. The one-liner: place the mic ~1 m in front of the
  speaker at tweeter height, choose Quick tune or Full measurement (the
  `microphone_check` screen's tier chooser, flow-simplification PR-U3 —
  both first-class; **Full carries the Recommended badge until a Full
  commission has completed on this topology** — S4, adversarial review of
  PR #1780 — then Quick tune does, so an express-only household is never
  nudged away from the wider walk that mitigates §1.3's HF-null row), tap
  Start, then follow the phone — apply is automatic (owner ruling,
  2026-07-20; gotcha #18), no
  browser-tab step in between. Since flat-linearization PR-3b the phone
  also prompts a series of small mic moves inside the measure and verify
  steps (the spatial cloud); the wizard's five screens are unchanged,
  because the cloud changed how many captures a step takes, not what the
  household is doing.
- **Only flow — v2.** W5b (2026-07-24) retired the legacy per-driver
  flow and the `JASPER_CROSSOVER_FLOW` selector.
  `build_crossover_envelope` dispatches straight to
  `build_crossover_envelope_v2` now; a stale
  `JASPER_CROSSOVER_FLOW=legacy` carried on an old box no longer selects
  anything (pinned by `test_legacy_env_still_serves_v2_envelope`).
- **Phone capture page:** the Cloudflare Pages app under
  [`capture-page/`](../capture-page/README.md), served at
  `capture.jasper.tech`, relaying through `relay.jasper.tech`. Deploy
  from the repo root:
  `npx wrangler pages deploy capture-page/dist --project-name jts-capture-page --branch=main`
  — `--branch=main` is load-bearing: without it wrangler deploys a
  preview alias and the production domain keeps serving the stale page
  (the W6.10 Chrome-deadlock bug class); the custom domain lags the
  deploy by ~5 min. See the capture-page README's release ordering, which
  depends on direction: the page must advertise a protocol BEFORE any Pi
  emits it (add → page first), and must keep advertising it UNTIL no Pi
  emits it (remove → Pi first). The list holds exactly one entry today, so
  the two sides have to move close together.
- **Relay Worker:** the Cloudflare Worker under
  [`relay/`](../relay/README.md), served at `relay.jasper.tech`. It is a
  **third independent release**, and like the page it ships **before**
  the Pi — accepting a larger capture plan is backwards compatible,
  emitting one is not. Deploy `cd relay && npx wrangler deploy`, then
  verify the public artifact before touching any Pi:
  `curl -fsS https://relay.jasper.tech/capabilities` — confirm
  `max_capture_plan_attempts` is at least what the Pi build will emit
  (32 as of PR-3a; the pre-capacity Worker's ceiling was 8). Only then
  `bash scripts/deploy-to-pi.sh`. The Worker's blob-index space IS the
  capture-plan attempt ceiling, so a stale Worker would otherwise reject
  the ninth capture mid-session; the Pi instead reads `/capabilities` at
  session setup and refuses before registering
  (`event=capture_relay.plan_capacity_refused`). Full rule in
  [`relay/README.md`](../relay/README.md) "Release order".

## Current status (2026-07-22)

**2026-07-24 — Layer-1a linearization now EMITS (#1668 PR-D), not yet
hardware-validated.** The fit engine's output (PR-C) now actually reaches
the applied graph — see "Linearization EMISSION" and "Flatness-verify"
below, and [`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md)
for the full Layer-1a design. Landed on a branch, adversarially reviewed
in-session, and hardware-free-tested; it is explicitly gated on JTS3
validation and the owner's listening-ladder protocol before merge — do not
treat this section as confirming an audible result yet.

**2026-07-24 — VERIFY-prediction coherence bug found in that same JTS3
validation, fixed.** Every eligible+fitted candidate failed VERIFY at a
deterministic ~1.7 dB tracking mismatch (three-attempt repeatability
1.688–1.699 dB against the ±1.5 dB `VERIFY_TOLERANCE_DB`): the persisted
prediction `_verify_priors` hands VERIFY was still built from the RAW
measured branches even though the emitted graph carried the Layer-1a
correction filters (PR-D, above) — so the correctly-linearized measured
summation was compared against a prediction that never modeled them. Fixed
in `CrossoverV2Conductor._fit_linearization`
([`crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py)):
whenever it fits (the same eligibility that emits), it now also rebuilds
the persisted prediction from the SAME linearized branches (`W_lin`/
`T_lin`, reusing `program_analysis.predicted_branch_sum` — no second
implementation) at the trim this attempt actually committed to. The
ineligible/raw path and pre-fix persisted eras are untouched — pinned by
`tests/test_crossover_v2_conductor.py`. Offline replay on the real #1667
N=3 capture confirms the mechanism: the predicted tracking mismatch
collapses from a nonzero value (matching the fitted filters' own response)
to exactly 0 dB once the persisted prediction models the same curves the
emitter realizes. Still gated on the SAME JTS3 re-validation as the
paragraph above — this closes a known-bad comparison, it does not by
itself confirm an audible result.

Waves W1–W6 complete (PRs #1578–#1604). Hardware-validated on JTS3 +
UMIK-2: first fully-calibrated run 2026-07-19. **W5b deleted the legacy
flow** (2026-07-24) — v2 is the only flow, per "Only flow — v2" above.
The v2 acoustic playback binding
(`bind_program_playback_seams`) is exercised on real CamillaDSP
hardware; every orchestration test injects fakes. T2's summed-flatness
delay refinement merged via PR #1647 on 2026-07-22. Its first JTS3 run failed
VERIFY, but a clean hardware delay sweep then isolated the wrong-lobe prior and
a one-sided VERIFY smoothing bug. The corrected selector subsequently applied a
53.669 µs woofer delay and passed a calibrated JTS3 VERIFY at 1.279 dB max
(1.5 dB gate); the pre-merge T2-specific adversarial re-review cleared 0
blockers / 0 should-fixes.

The required post-merge UMIK-2 repeat did **not** reproduce that result
(MEASURE railed to a signed −299.948 µs correction at the flatness search
bound; three VERIFY captures failed at 5.264–6.454 dB max; Undo restored the
prior profile). The follow-up diagnosis proved the flatness objective's comb
basin ordering is capture-noise-dependent and replaced the selector: **the
drift-corrected physical peak-gap anchor now owns lobe selection and the
primary delay, refined only by a bounded nearest-GCC-local-peak snap
(±period/6); flatness is evidence, never a selector** (see "Delay selection"
below). The replacement cleared an independent adversarial review at
0 blockers / 0 should-fixes / 0 nits and its on-device confirmation:
three fresh headless JTS3 flows selected 32.411 / 31.013 / 33.783 µs —
2.77 µs total spread together with the two replayed hardware-anchored
captures — with VERIFY passing at 1.233 and **0.597 dB max** (best recorded
on this rig); the one VERIFY-failed run was a measured room-noise event
(CHECK woofer SNR 17.4 dB vs 23.3 nominal) with the selector still
in-cluster. **The stop rule was then met the same day** (owner-delegated
controlled campaign, quieted room): six consecutive measurement verdicts all
passed — worst 1.106 dB max, five of six ≤ 0.55 — with selections spanning
**1.22 µs** total (median 27.7 µs) against the ±20.8 µs criterion, every
phase single-attempt. One session was a relay-layer transport void (capture
uploaded, never analyzed — issue #1650); ambient-noise events measurably
degrade VERIFY while leaving selection unaffected, and CHECK's woofer-band
SNR predicts VERIFY health — productized as the anomaly-detection/discard-UX
workstream, issue #1652. Final dispositions: Fix 4 shelved (revival trigger:
phone-mic-era cluster spread or `snap_found=false`), T2-robust retired (its
phase-slope core rails systematically on as-crossed branches, +388 ± 38 µs
16/16; its predictive-confidence goal lives on in #1652). The
reproducibility working plan is archived as decision archaeology. See
[`crossover-measurement-reproducibility-plan.md`](historical/crossover-measurement-reproducibility-plan.md)
§10–§11 for the exact evidence and gate state.

The first phone-class-mic series (Dayton iMM-6C on a computer, same headless
path, same evening) mapped the next frontier: woofer-band SNR matches a
reference mic, but its ~8–10 dB lower **tweeter-band** SNR scatters the
anchor/correlation — accepted selections spanned 22.4 µs (vs the UMIK's
1.22 µs), brushing the ±1-sample budget, with the confidence gate refusing
honestly twice (the first such refusals ever — including one snap capped at
its radius edge). Two confounds are being attributed offline before any
hardening decision: an audible `event=outputd.xrun` playback glitch
(15:52:26) in one refusal's window, and hallway transients behind the one
VERIFY fail. Offline forensics then attributed every anomaly (see the
#1652/#1654 comment threads): the xrun capture's sweep segments located
−25…−28 ms off schedule while `glitch_detected` stayed False — the
repeat-pair gate is structurally blind to uniform whole-capture shifts, and
the per-segment location residual/confidence the analysis already computes
is a free detector for it (now enforced — see the measurement-honesty gates
below); the residual mic signal is a single unambiguous
correlation peak at LOW prominence whose position wanders (not lobe
ambiguity). **The naive sub-sample anchor upgrade is refuted** — it left the
iMM-6C span unchanged and degraded the UMIK span 12× in direct testing — so
the standing levers are tweeter-sweep bandwidth (Fix 4, #1654) and/or
energy, decided after the iPhone-chain series. Live trail: #1654 (Fix 4
shelf + mechanism data), #1652 (anomaly detection/attribution), #1650
(relay voids), #1656 (calibration identity follows the saved setup — the
iMM-6C series silently ran under the UMIK's calibration curve;
magnitude-only impact, but it makes the saved-mic serial-entry UI bug a
correctness issue).

**A glitch verdict is a timeline SPLICE, not clock drift (2026-07-27,
issue #1765).** Across the whole 2026-07-22…27 JTS3 journal (57 MEASURE
captures) exactly two captures tripped `glitch_detected`, and **both fired
on the residual guard with epsilon comfortably inside `MAX_DRIFT_PPM`** —
2026-07-23 at 30.53 ppm / 798.46 samples, 2026-07-27 at 94.12 ppm / 32.36
samples. The 500 ppm clock-drift bound has never fired in production. Read
`epsilon_ppm` on a *glitched* capture as an artefact, not a measurement.

The 2026-07-27 capture was recovered from the retained dump and
cross-correlated (ncc 0.92–0.99) against the two captures that PASSED
minutes later on the same program and rig: five of its six located sweeps
agreed within 0.8 samples while the first woofer sweep sat 64.3 samples
apart — **one +64-sample (1.333 ms) insertion between `sweep_w` and
`sweep_t`**. That single step predicts every reported number: the woofer
pair straddles it (32.1 ppm true drift + 64/1_036_800 = 93.8 vs 94.118
observed), the tweeter pair sits entirely after it (32.097 ppm = the true
drift), demeaned residual 32.2 predicted vs 32.357 observed, and the
primary-sweep W↔T misalignment drove `delay_us` +15 → −847.6 µs and
`predicted_ripple_db` 4.4 → 16.8.

Attribution: **the leading hypothesis is Pi-side playback fill — and
`event=outputd.xrun` does NOT rule it out.** An earlier pass on this issue
concluded "capture side" from the absence of xrun lines in the playback
window; that inference is invalid, and the owner refuted it directly by
hearing audible tears during sweeps launched from the **command line**,
with no browser in the path at all.

The mechanism: on a short content read `jasper-outputd` zero-fills the
deficit and still writes a full period to the DAC
(`read_content_period` → `out[active..].fill(0)`,
`rust/jasper-outputd/src/alsa_backend.rs`). That INSERTS
`requested − frames` samples into the emitted timeline — audible as a brief
tear, and an *arbitrary* frame count rather than a fixed granule, so a
64-frame fill is entirely ordinary. Production confirms the insertion is
real: `dac_frames_written` exceeds `frames_read` by thousands of frames
(8,576 → 10,752 → 12,928 across three consecutive log lines on 2026-07-27).

**Why the journal looked clean: the `event=outputd.xrun` eprintln fires ONLY
in the `EPIPE`/`ESTRPIPE` branch.** Partial-period fills and `EAGAIN` empty
periods increment `content_partial_period_count` /
`content_empty_period_count` and log *nothing* — those counters surface only
as fields on a later line that some other condition happened to emit
(visible in the data: `empty_periods` climbs by 2 while `count` climbs by 1).
The exact sample-inserting event is structurally silent. Compounding it,
`jasper/correction/runtime_integrity.py` — the layer whose whole job is
"did the audio path glitch during this capture?" — gates on `xrun_count`
and never reads the partial/empty period counters, so a measurement that
spans a fill passes its integrity check.

The fill is periodic on this box, and the cadence + its clock-offset cause
are owned by
[HANDOFF-audio-graph-consolidation.md](HANDOFF-audio-graph-consolidation.md)
§G ("What `direct` mode costs today") — not restated here. What matters for
*this* doc: the last fill event before the 2026-07-27 session was 12:39:40
and the next was due inside the failing capture's playback window. That is
NOT proof for that capture — outputd restarted around the session (counter
reset 89 → 1) and the counters were never sampled then — but the signature,
the arbitrary fill size, and the audible tears all fit.

The fill is no longer silent: `event=outputd.content_fill` plus the
`outputd_content_fill_increased` gate in
[`jasper/correction/runtime_integrity.py`](../jasper/correction/runtime_integrity.py)
mean a capture that spans one is now flagged rather than passing its own
integrity check (#1768). Still open: step-aware recovery using the N=3
redundancy already paid for — a located step lets the analysis pick a
step-free sub-window instead of retrying. `_locate_discontinuity` now names
the step (size + which segment it landed after) on every MEASURE capture,
which is the input that work needs — see the diagnostics section below.

**Measurement-honesty gates (2026-07-22 night).** Three additive acceptance
gates convert the corrupted-capture signatures above into honest
refusals/retries — no selection math and no VERIFY comparison semantics
changed. MEASURE refuses a candidate whose `predicted_ripple_db` exceeds
`MEASURE_PREDICTED_RIPPLE_CEILING_DB` (15 dB; the corrupted phone solve
predicted 27.3 dB where every clean capture that day predicted 4.4–9.0 —
reuses `low_alignment_confidence`, same household action). MEASURE
rejects-and-auto-retries as a glitch when any sweep locates off schedule
(`_sweep_schedule_ok`: |residual| > 5 ms or locate confidence < 0.3; the
xrun signature was −25…−28 ms at 0.07–0.12 confidence vs ≤1.5 ms at ≥0.69
on every clean capture — reuses `drift_baselines_disagree`). VERIFY refuses
with the new `verify_level_shift` reason (verify-fail template, budget 2)
when a later attempt's summed-pilot transfer steps more than 0.35 dB from
the session's first verify attempt (the phone chain stepped 0.75–0.82 dB
across the dishonest 1.19→2.11→2.84 dB attempt sequence; the one clean
multi-attempt session stepped ≤0.05 dB). All thresholds are PROVISIONAL
named constants in `crossover_v2_flow.py`; the per-capture diag events
carry the new numbers plus a `guard` disambiguation field. Offline proof
(45-capture retention archive + both hardware-anchored overlay runs): zero
false fires, every must-refuse capture refused — evidence + replay scripts
in `captures/xover-e0-2026-07-21/honesty-guards-proof-20260722/` (session
artifact, not in-repo).

---

## Architecture — the conductor model

Three parties, one direction of authority (the Pi):

- **Phone = dumb recorder.** Per phase it records a known-length window
  and uploads one encrypted WAV. No live phone↔Pi feedback mid-capture,
  no per-repeat gestures. It reads the next capture's plan entry
  (duration + prompt) from the relay session and posts a WAV back.
- **Pi = conductor.** `CrossoverV2Conductor` in
  [`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py)
  owns sequencing, admission, retry budgets, and verdicts. It compiles
  one **excitation program** per phase (a pure-data schedule of stimuli
  with per-segment digital gains + safety attestation), plays it as one
  continuous stream at a single session volume, and analyzes the upload.
- **Analysis = pure functions.** `analyze_program_capture` in
  [`jasper/audio_measurement/program_analysis.py`](../jasper/audio_measurement/program_analysis.py)
  maps `(ExcitationProgram, WAV, cal, geometry, priors) → ProgramAnalysis`
  with no hidden state, so every verdict is reproducible offline from
  the stored artifacts.

The conductor is I/O-free: all side effects cross an injected
`V2FlowSeams` boundary (`play`, `analyze`, `publish_check`,
`publish_candidate`, `apply_complete`, `apply_failed`). The web host
([`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py))
binds the real seams — including firing the auto-apply itself on a
background thread once the candidate-carrying verdict lands (gotcha #18;
that is the **pre-apply cloud group's close** since the 2026-07-27 timing
move, MEASURE's own accept on the pre-cloud 3-entry shape — see "When the
fit runs" below) — and tests inject fakes.

### The capture flow

One relay session (`crossover_v2:session`) spans **16 captures** at the
**Full tier's** shipped defaults (`TIER_FULL`). The conductor hands
`authorize_begin` / `on_armed` / `consume_capture` to `run_capture_plan`
(`jasper/capture_relay/session.py`):

| index | phase | gate | what it is |
|---|---|---|---|
| 1 | `check` | tap | microphone check |
| 2 | `measure` | countdown | design-axis anchor, per-driver |
| 3–10 | `cloud_measure` | tap each | 8 prompted pre-apply positions |
| 11 | `verify` | on apply | design-axis anchor, summed |
| 12–16 | `cloud_verify` | tap each | 5 prompted post-apply positions |

**Express tier (`TIER_EXPRESS`, flow-simplification PR-U1)** is the same
layout at a smaller shape — 7 captures, (N=5, M=1): index 1 `check`,
index 2 `measure` anchor, indexes 3–6 (4 prompted `cloud_measure`
positions), index 7 `verify` anchor. `M=1` means **no `cloud_verify`
phase at all** — the done screen rides the VERIFY entry itself, and there
is no post-apply cross-position claim (see
[`flat-linearization-flow-simplification-plan.md`](flat-linearization-flow-simplification-plan.md)
§1.3's degraded-claims table). `resolve_plan_shape` in
`crossover_v2_flow.py` is the single place both counts are resolved from
a tier id; `express_cloud_measure_positions()` derives the 5 (the
`cloud_measure` group's total captures — the anchor plus 4 prompted
positions) from `CLOUD_POSITION_PROMPTS`' wide-offset guarantee, never a
hardcoded literal (N3 fix, adversarial review of PR #1780: the function
derives the position COUNT, not the curve count the anchor is excluded
from — see "Positions are not curves" below).

The two `cloud_*` phases are **position groups** (flat-linearization
PR-3b): one phase spanning many capture indexes, one prompted mic move
each. They exist because
[`flat-linearization-plan.md`](flat-linearization-plan.md) fundamental 1
makes the spatial cloud *the* measurement and demotes single-point
capture to a diagnostic — spatial averaging is what removes early
boundary interference that gating alone cannot. Read that doc for the
physics; this section is the operational shape.

What is **not** cloud-averaged, by plan doctrine: alignment, level,
polarity, and per-driver linearization stay single-position at the
design-axis anchor (MEASURE). VERIFY's tracking comparator likewise
stays at the anchor — it answers "did apply do what the model
predicted", which a moved mic cannot answer.

**When the fit runs** (owner decision, 2026-07-27). The fit still reads
MEASURE's single-position analysis — the line above is unchanged — but it
RUNS at the pre-apply group's close (index 10), not at MEASURE's accept
(index 2), and takes the closed cloud's honesty verdict as an extra
constraint. That is what makes the correction envelope's two cloud terms
(`spatial_exclusion_limit`, `position_stability_limit`) reachable at all:
before the move the candidate was built eight captures before the cloud
it was supposed to consume, and auto-apply fired while positions 3–10
were still being walked — so the "pre-apply cloud" was captured through
an already-corrected speaker. The move closes both. Every MEASURE trust
gate stays at index 2 (they read the analysis, not the candidate), so a
doomed session still fails at sweep two rather than after a nine-position
walk. A session with **no** pre-apply group — the pre-cloud 3-entry shape
the conductor defaults to, and the 1-entry re-verify path — still builds
at MEASURE with the same accept, payload keys and apply timing it had
before: the rule is "the fit runs at the last capture before the apply".
No wire bytes, screens, or plan entries changed; only conductor-internal
timing. (Those shapes' `candidate.json` does gain an always-empty
`exclusion_evidence` key, which is omitted from the fingerprinted core
when empty, so the fingerprint is unchanged.)

The deferring shape holds MEASURE's analysis across the prompted walk,
because it is the fit's input. That retention is scoped tightly on
purpose: the object is dominated by per-occurrence float64/complex128
arrays on the analysis FFT grid — one two-occurrence `DriverResponse`
measured **33.6 MB** on the S0 corpus's own grid (524,289 bins;
production MEASURE uses a different program and grid, so read this as
the order of magnitude, not the number) — so a session that never defers
does not store it at all, and a group close releases it as soon as the
fit has consumed it. Re-consumption cannot strand on the release: the
relay admits a begin only at `(accepted_count + 1, attempts_used + 1)`
and dedupes processed pairs, so a group closes for the last time exactly
once.

1. **CHECK** (~25 s, one tap). Ambient silence + two band-limited pilot
   chirps per driver at two levels (−10 dB apart). Yields the ambient
   floor, the behavioral AGC/linearity verdict, channel-map sanity, and
   the **solved gain plan** for MEASURE. Replaces the legacy per-driver
   level ramps and ambient waits.
2. **MEASURE** (~33 s, auto-advances behind a cancelable countdown).
   2-channel routing: pilot pair + guard silence + **three interleaved
   woofer/tweeter sweep cycles** — `w1 → t1 → w2 → t2 → w3 → t3`
   (sweep-composition PR-A, #1668; was one woofer-only repeat, ~+15 s
   program length). Every cycle past the first is bit-identical to that
   driver's first sweep — the repeats form the in-capture drift estimator
   + glitch detector, now for BOTH drivers. Yields per-driver gated complex
   responses (cal applied), relative
   delay, polarity, trims, per-band SNR — folded into a
   `MeasuredCrossoverCandidate`. GCC-PHAT supplies a drift/parallax-corrected
   seed, polarity, and capture confidence. The delay actually selected and
   applied is the minimum-ripple applied delay
   within the crossover region's declared `delay_range_ms` magnitude range
   (plus the same plausibility margin used by the conductor). The
   drift-corrected physical peak gap supplies the sign and centers one
   ±half-period comb lobe inside the range; GCC is deliberately not the lobe
   prior because its periodic peak can identify a neighboring comb basin.
**Layer 1a driver linearization (#1668 PR-C).** MEASURE additionally fits
a per-driver cut-only linearization (a rising-slope Highshelf + Peaking cuts,
plus the CD-horn top-octave give-back stage — a Lowshelf backbone + optional
trailing Highshelf taper, #1668 — honoring the correction envelope's per-bin
depth ceiling) whenever the mic resolved to the "reference" trust tier AND both
drivers cleared a paired ≥3-occurrence gate — otherwise the candidate is
byte-identical to the plain trims-only shape from before this PR. When eligible,
the fit is applied to each branch in the linear domain BEFORE the trim solve, so
trim reflects the linearized (not raw) response — the ordering that structurally
defuses #1667's band-average bias. The linearized trim is then ANCHORED (#1668,
after the 2026-07-24 JTS3 runs): each branch's raw committed trim plus the level
its own emitted cascade removed from its reference band
(`LinearizationFit.correction_giveback_db`), normalized non-positive — a
level-preserving give-back that replaced the `solve_branch_trims` overlap-band
seed, which returned only 5.81 dB of a 9.27 dB spend and left the tweeter band
~3 dB low. A ripple scan that then drifts >6 dB from that anchor is discarded in
favor of the anchored pair.
The fit result travels on `MeasuredCrossoverCandidate.linearization` (empty
dict = not attempted), and — since the 2026-07-27 timing move — the cloud
inputs that constrained it travel beside it on
`MeasuredCrossoverCandidate.exclusion_evidence`: the merged honesty intervals
handed to `spatial_exclusion_limit`, the `band_spread`/`n_positions` behind
`position_stability_limit`, and the identified-null registry with its τ/r per
null. Empty dict = no cloud evidence entered this fit, which is also every
pre-move candidate's implicit claim. Enough to re-derive both envelope terms
from `candidate.json` alone; it deliberately duplicates data in
`cloud_measure.json`, because that file is prunable session evidence while this
travels with the correction it justifies. Same optional-field conventions as
`linearization` (omitted from the fingerprint when empty, fingerprinted when
present, accepted absent on `from_mapping` — that last one is load-bearing
and was a caught blocker: `to_dict()` always writes the key, so the reopen
comparison must `setdefault` it or every pre-PR-6b candidate refuses as
`candidate_tampered` on the live apply path). Measured cost: **5,294 bytes**
of `candidate.json` on the S0 ten-position cloud — registry 3,307,
`band_spread` 1,596, intervals 287 — scaling with what the honesty
instruments found, not with capture length. Design and fitting-policy SSOT:
[`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md)
"Layer 1a concretely"; engine at
[`jasper/active_speaker/linearization_fit.py`](../jasper/active_speaker/linearization_fit.py),
correction-envelope core at
[`jasper/active_speaker/linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py).

**Linearization EMISSION (#1668 PR-D).** The fit result is no longer
evidence-only: `emit_active_speaker_baseline_config` (`camilla_yaml.py`)
gained a `linearization` parameter — one Peaking/Highshelf/Lowshelf chain per
role (the CD-horn stage adds a leading Lowshelf backbone and an optional
trailing Highshelf taper, #1668), emitted immediately after that driver's
crossover HP/LP and before
bass-extension, via the shared `emit_filter_spec` primitive. The two
RICH-candidate seams —
`measured_crossover_candidate.compile_candidate_config` and
`baseline_profile.build_baseline_profile_candidate` (which also carries the
reduced result in the applied profile's `recomposition_snapshot` and
top-level payload) — thread a persisted `LinearizationFit` result through
the shared reduction, `linearization_fit.linearization_filters_by_role`.
`baseline_profile.recompose_applied_baseline_yaml` (the `/sound`
preference-EQ seam) — the fix for a since-closed silent-reversion gap —
deliberately does NOT call that helper: its snapshot's `linearization` key
is already in the reduced shape `build_baseline_profile_candidate` wrote,
so it re-validates that shape inline, era-tolerantly, instead of re-reducing
it (calling the shared helper on an already-reduced mapping silently
returns nothing for every role). The net effect is the same — recompose no
longer silently drops the stage on every EQ/room recompose — but the
reduction path differs; do not "consolidate" the two onto one helper call.
The runtime-safety verifier
(`runtime_contract._baseline_output_chain`) independently re-proves any
linearization-named filter in the emitted chain (Peaking/Highshelf/Lowshelf
type, non-positive gain, one leading shelf + one optional trailing Highshelf
taper) — self-proving from the graph text alone, so no new
evidence parameter needed threading through `classify_camilla_graph`'s
other callers. Emission is empty by default; a candidate/snapshot with no
`linearization` key, or an empty one, stays byte-identical to the
pre-PR-D graph.

**Gauge fix (2026-07-24): the WHY travels alongside the WHAT, and the
OBSERVE-layer honesty ladder reaches the wizard.** Before this fix, the
conductor's `_last_linearization_outcome` (one of "fitted" /
"trim_rejected" / "ineligible_mic_tier" / "ineligible_repeats" /
"fit_failed" / "" — see `crossover_v2_flow.py`'s own `__init__` comment)
lived only as an in-memory attribute logged once per MEASURE attempt —
linearization could silently not run while every screen looked the same.
`MeasuredCrossoverCandidate.linearization_outcome` now carries the SAME
verdict as a new, era-tolerant, fingerprinted field, threaded verbatim
through `build_baseline_profile_candidate` (top-level `linearization_outcome`,
sibling to `linearization`) and `_frozen_applied_profile` (same allowlist
fix `linearization` itself needed for Gap 3c) into
`setup_status.read_active_speaker_setup_status`'s `protected_profile`
block, surfaced on `/state.active_speaker_setup.protected_profile.
linearization_outcome`. Separately, `jasper.web.correction_crossover_v2.
_candidate_summary` reads each role's `observe_octave_summary` (the
OBSERVE-layer honesty-ladder field above) straight off the live session's
rich candidate and threads the 8k/12k/16k values into the wizard's RESULT
screen (`candidate_review.linearization_octaves`,
`candidate_review.linearization_outcome`) — the number that says "the top
octave is 9 dB down and nothing corrected it." That reduction is
session-scoped only (mirrors `linearization`'s own reduced applied-profile
copy, which strips the honesty-ladder fields via
`linearization_filters_by_role` — see that function's docstring); it does
not thread into the durable applied-profile artifact.
**The level-match frame (PR-L3, 2026-07-27).** `solve_branch_trims`
([`jasper/audio_measurement/program_analysis.py`](../jasper/audio_measurement/program_analysis.py))
reads **each branch on its own side of Fc** — `[Fc/ρ, Fc]` for the woofer,
`[Fc, Fc·ρ]` for the tweeter, log-symmetric by construction
(`branch_level_bands_hz`), with `ρ ≤ 1 octave` narrowed by whichever
branch's own validity span binds first. It is never handed the SHARED
both-branches-excited overlap band.

Why: band-power-averaging is a level match only when each branch is
weighted symmetrically about Fc. The shared band's lower edge is clamped UP
to the tweeter's sweep floor (`overlap_band_hz`), and a tweeter swept from
Fc upward — JTS3's real geometry — leaves `[Fc, 2·Fc]`, entirely on the side
where the woofer is inside its crossover skirt. That measures skirt depth,
not sensitivity: **+10.59 dB on an ideal LR4 pair with two equal-sensitivity
drivers** (closed form, pinned by
`test_one_sided_overlap_band_biases_the_level_match`). On the archived JTS3
MEASURE captures it put the tweeter trim 10.9 dB (2026-07-27 session
`d5b171fa81a5`) and 13.1 dB (2026-07-25 run 5) below the same analysis's own
per-driver `target_level_db` frame — the ideal-pair figure accounts for the
observed error to within **0.27–2.47 dB** across the two sessions, the
remainder being each real driver's own rolloff on top of the filter's. That
trim is what the linearized give-back anchors on — the origin of the
~10 dB-dark tweeter. Widening back
to the nominal `[Fc/2, 2·Fc]` is not a fix either (+3.03 dB residual on the
same ideal pair, and it averages the tweeter over bins it was never excited
in). Every MEASURE analysis now discloses the frame:
`event=program_analysis.branch_level_match` carries `level_w_db`,
`level_t_db`, and both bands, to be read beside the per-role
`target_level_db` that `correction.crossover_v2_linearization_giveback`
carries for the same capture.

**Ripple-optimal trim polish (#1667 Phase 3, scoped by PR-L3).**
`solve_ripple_optimal_trim` re-solves the tweeter trim for minimum
summed-response ripple, scanned in a bounded window (±10 dB,
`RIPPLE_TRIM_SEARCH_WINDOW_DB`) around the band-average seed and clamped to
the physically valid attenuation range; a result more than
`RIPPLE_TRIM_SANITY_MARGIN_DB` (6 dB) from the seed is distrusted and
discarded in favor of band-average, with a WARNING (never a silent wild
trim). Selection is flat-minimum-regularized (architect follow-up): among
every scanned candidate within `RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB`
(0.25 dB) of the scan's global minimum ripple, the one closest to the
band-average seed wins — a shallow ripple bowl's exact minimizer is
sensitive to measurement noise and would otherwise wander session to
session, which is a worse product property than a fraction-of-a-dB of extra
ripple.

The polish runs **only where its own band straddles Fc** — at BOTH call
sites: the raw candidate's
(`event=program_analysis.ripple_trim_skipped`) and the Layer 1a linearized
re-solve's (`event=correction.crossover_v2_linearization_ripple_trim_skipped`),
the one whose result becomes `role_attenuations_db`. Both carry
`reason=ripple_band_one_sided`. #1667 was written against a
biased seed and its whole corpus was one-sided geometry, where the summed
ripple is the tweeter's own and barely responds to the tweeter's gain: it
recovered only 2.1–4.1 dB of the 10.9–13.1 dB frame error, and once PR-L3
fixed the seed the same objective pulled the other way (replayed on the
2026-07-25 run-5 capture it moved an unbiased −12.368 dB seed 7.9 dB back
down, stopped only by the sanity guard). A selector that cannot see the
woofer does not set the woofer's handoff level. Both the scan and its
straddle guard are wired into BOTH paths — the raw candidate
(`CrossoverCandidate.trim_db`, with the band-average seed preserved as
`trim_band_average_db` evidence) and the Layer 1a linearized re-solve above
— so consumer/phone-tier captures, ineligible for linearization, get the
same treatment. The linearized-path trim is correct
only with the linearization filters emitted (#1668 PR-D); the two land
together. Design rationale:
[`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md)
"Decisions already made" #2 and "Execution plan" Phase 3.
3. **CLOUD-MEASURE** (8 × ~16 s, one tap each). The pre-apply spatial
   cloud, between MEASURE and APPLYING: the same mono summed sweep
   VERIFY plays, captured at prompted positions around the mark.
   Per-position work is deliberately light — the same locate/linearity
   screens plus "did this yield a usable summed response"; the heavy
   pass runs once at group end. See "Position groups" below.
4. **APPLYING** (control page, no capture — auto, since 2026-07-20). The
   conductor itself evaluates the candidate: alignment confidence
   `< ALIGNMENT_CONFIDENCE_TRUST_FLOOR` (0.6) rejects MEASURE with
   `low_alignment_confidence` (guidance to re-measure at a cleaner mic
   position — never a question); otherwise it fires the SAME apply
   transaction a household's tap used to trigger
   (`jasper.web.correction_crossover_v2.handle_v2_apply`) on its own
   background thread. VERIFY is soft-held (`CaptureBeginDeferred`, screen
   `awaiting_apply`) exactly as before — the phone now sees "Applying to
   your speaker…" instead of "waiting for the household to apply", and the
   release is the auto-apply completing, never a human. An auto-apply
   failure (blocked or errored) persists `apply_failed` and the deferred
   hold is refused with the honest reason instead of holding toward a
   dishonest `relay_timeout`. See gotcha #18 for the full rationale.
5. **VERIFY** (~15 s, auto-arms on the apply-complete host event). A mono
   summed sweep through the **applied production graph** + a pilot pair,
   captured back at the mark (the apply hold's copy is where the
   household is told to walk back). Pass = notch-excluded,
   validity-floor-clamped tracking error ≤ ±1.5 dB. On fail the applied
   graph **stays in force** (proof-checked safe) and the household is
   offered Try again / Undo.

6. **CLOUD-VERIFY** (5 × ~16 s, one tap each). The post-apply cloud,
   walking the same prompted positions.

### Position groups — the operational rules

- **Constants** (`crossover_v2_flow.py`, each with its rationale in
  place): `DEFAULT_CLOUD_MEASURE_POSITIONS` 9 (min 6, max 12),
  `DEFAULT_CLOUD_VERIFY_POSITIONS` 6, `GEOMETRY_RETRY_POSITIONS` 2 — the
  **Full tier's** rules; `MIN_CLOUD_MEASURE_POSITIONS` never moves for
  Express. The counts are wall-clock choices, not statistical optima —
  S0's stability data says more positions is strictly better. **Express**
  (`TIER_EXPRESS`) is a distinct named shape, not a loosened Full floor:
  `express_cloud_measure_positions()` (= 5, derived —
  `_min_positions_for_two_wide_offsets()`) and
  `EXPRESS_CLOUD_VERIFY_POSITIONS` (= 1, no cloud-verify group at all).
  `resolve_plan_shape(tier)` is the one function both `build_v2_capture_plan`
  and `build_v2_cloud_index_phase_map` resolve their (N, M) from — see "The
  capture flow" above.
- **Positions are not curves.** Both counts include the group's anchor,
  so each group combines `N − 1` / `M − 1` **summed curves**: MEASURE's
  anchor is a per-driver capture with no `summed_response` at all, and
  VERIFY's anchor does capture one but is consumed by the tracking
  verdict rather than joined to the group. At the shipped defaults the
  pre-apply cloud combines 8 curves and the post-apply cloud 5 — the 8 is
  why the default is 9 and not 8 (adjudication 3a). Compare
  those numbers, not the position counts, against
  [`flat-linearization-plan.md`](flat-linearization-plan.md)
  fundamental 1's "N≈8–12 gated sweeps".
- **No new phone mechanism.** Prompts ride the shipped
  `CapturePlanEntry.screen` + `AUTO_ADVANCE_TAP`; the deployed capture
  page renders per-entry screens and has no plan-length cap. What the
  cloud *did* need was relay capacity — PR-3a raised
  `MAX_CAPTURE_PLAN_ATTEMPTS` 8 → 32 and gated emission on the Worker's
  `GET /capabilities`. A stale Worker refuses the session at
  registration with a clean 400 (`RelayCapacityUnavailable`, a
  `ValueError` so the dispatcher's refusal arm answers it), and
  `jasper-doctor`'s `check_capture_relay` reports the advertised ceiling
  so an operator finds it before a household taps Start.
- **Prompt copy** is `CLOUD_POSITION_PROMPTS` — hand-widths and
  forearms, never centimetres (owner ruling, S0 studio session). The
  ORDER is load-bearing: both groups walk the same table from the front,
  so two wide (~forearm) offsets must sit inside the first
  `MIN_CLOUD_MEASURE_POSITIONS − 1` entries or the LF half of the
  measurement quietly disappears. Express walks the SAME table from the
  front at its own, shorter length (`express_cloud_measure_positions()`),
  derived from exactly where the table's second wide offset falls — a
  reordered table moves Express's floor with it rather than shipping a
  silently one-wide "quick tune". Pinned by test
  (`test_cloud_prompts_front_load_the_wide_offsets`).
- **Geometry-locked retake.** At group end the conductor runs
  `combine_cloud_positions` once and reads its `geometry` field for this
  retake decision (N3 review finding, 2026-07-27: this bullet's prior
  "reads ONE field" wording was stale against the "Landed as shipped"
  paragraph further down, which is the current, accurate description — the
  SAME combined result also feeds PR-4's honest-instrument pipeline, not
  just this retake gate). A `locked`
  verdict that is not `thin_evidence`-qualified rejects the group's last
  position with `cloud_geometry_locked` and a wider-spot instruction —
  at most `GEOMETRY_RETRY_POSITIONS` times, then it accepts and
  RECORDS the verdict — journal plus the durable state's `cloud` block.
  PR-4 carries the verdict (and its plain-language guidance copy) onto the
  envelope and `/state`; no household-facing surface renders it yet — PR-7
  does — so do not read "records" (or "carries") as "discloses". Bounded
  because a source-fixed comb (S0's horn
  rim) never decorrelates no matter how far the mic moves. The replaced
  take is dropped from the cloud — that is what the protocol's retake
  lever means (the same index is measured again), not a claim that
  dropping beats appending. That claim was made and withdrawn in review:
  appending a wide position actually fills a null further (−6.1 dB vs
  −7.7 dB on the reviewer's power-mean counterexample) and lowers
  `clustered_fraction` more. Replacing is what the runner permits, not
  what the estimator prefers.
- **Retry budget is per POSITION, not per group.** `_slot_of_index`
  keys attempt bookkeeping by `phase:index` inside a group and by the
  bare phase everywhere else, so CHECK/MEASURE/VERIFY bookkeeping is
  unchanged and a retake at position 2 cannot refuse position 7.
- **Session budget.** `session_wall_clock_ceiling_s(plan)` scales the
  walked-away measurement-volume ceiling with plan length
  (1800 s + 120 s per capture beyond the 3-entry baseline = 3360 s for the
  Full tier's shipped 16-capture plan, 2280 s for Express's 7, hard-capped
  by `session_volume_plan.MAX_WALL_CLOCK_CEILING_S` = 3600 s). The
  restore ladder and the restore-once latch are unchanged: a walked-away
  household can never leave the speaker at measurement volume.
- **Resume is unchanged (§5.6).** A new relay session invalidates every
  capture phase including the clouds; a group interrupted mid-way
  resumes only within the same session. `V2ConductorSnapshot.
  session_phases` records which phases a session actually runs, so a
  verify-only re-arm (still 1 entry, still byte-identical on the wire)
  reaches DONE instead of waiting on a group it never had.
- **Artifacts.** Every accepted cloud position writes its WAV plus a
  metadata sidecar (prompt text, index, timestamps, QC verdict) into the
  session bundle via `bind_position_retention`; the closing geometry
  verdict lands in the durable state's `cloud` block. Retention rose
  256 MiB → 1 GiB for this; the publish-time free-space floor
  (`MIN_FREE_SPACE_AFTER_PUBLISH_BYTES`) was deliberately *decoupled*
  and frozen at 256 MiB — see its comment.
- **PR-4's seam** is `combine_cloud_positions(positions)` →
  `CombinedResponse | None`. `cloud_position_capture` is the
  per-position assembly underneath and does not change. The
  gated-IR reconstruction those functions perform is validated against
  the S0 corpus by
  [`tests/test_crossover_v2_cloud_geometry_corpus.py`](../tests/test_crossover_v2_cloud_geometry_corpus.py)
  (`JTS_FLAT_LIN_S0`-gated), not asserted.

  **Landed as shipped (2026-07-26).** `_close_cloud_group` calls
  `combine_cloud_positions` exactly ONCE per group close and derives BOTH
  the retry-gating verdict (`_geometry_verdict_from_combined`, a pure
  function of the already-combined result) and the honest-instrument
  pipeline (`assemble_cloud_group_result`) from that single object — the
  contract this section's opening paragraph states. An earlier revision of
  this wiring called `combine_cloud_positions` a SECOND time from the
  pipeline step (justified then as "byte-for-byte deterministic, so it
  agrees with the retry-gating call anyway"); round-1 review measured the
  actual cost — seconds-per-combine, 3-6 s across runs/hosts on the S0
  ten-position corpus (interpreter-bound `smooth_fractional_octave`, worse
  on a Pi 5) — and reversed it: real operator seconds are not worth
  spending on a claim that was true but unnecessary to rely on. With
  `GEOMETRY_RETRY_POSITIONS = 2` allowing up to 3 close attempts per group
  (2 retries + the accepting close), the pre-fix worst case was 3 × 2 = 6
  combines (round-2 review, 2026-07-27, correcting an earlier "up to 4x"
  claim, and restating the earlier "5.6-6.2 s" point figure as a regime —
  it did not reproduce across hosts/runs).
  `cloud_geometry_verdict(positions)` (PR-3b's original seam) is now a
  positions-only convenience wrapper the conductor itself does not call
  (kept for `test_crossover_v2_cloud_geometry_corpus.py` and any other
  direct caller); `test_crossover_v2_conductor.py`'s `_lock` monkeypatch was
  updated to patch `_geometry_verdict_from_combined` instead (the seam the
  conductor actually calls now), with the real (unmocked) combine still
  running underneath it. The wiring contract itself — the mask/geometry/
  registry consumed TOGETHER — is `assemble_cloud_group_result`, issue
  #1742 item 4's single result-assembly function, called once per closed
  group and never read from in pieces.

**Flatness — a SIBLING claim, report-only, and since the flat-linearization
plan's PR-5 it comes from the CLOUD, not from the VERIFY capture.** It
answers a different question ("is the speaker flat") than integration-verify
("did the applied crossover realize its own predicted summation") — the
design doc's finding was that integration-verify alone cannot see an
uncompensated tweeter rolloff, because the prediction shares that rolloff.

*History, because the shape changed twice.* #1668 PR-D computed it per
VERIFY capture (`ProgramAnalysis.flatness_tracking`, that capture's own grid
from its own validity floor to `FLATNESS_VERIFY_HI_HZ` = 16 kHz, graded
against its own band mean, tolerance `FLATNESS_VERIFY_TOLERANCE_DB` = 3.0,
PROVISIONAL and never bench-derived). The 2026-07-24 gauge fix persisted it
to `verify.flatness` and rendered it on the verify_fail and "done" screens.
**PR-5 retired that whole construction** — function, both constants, the
`ProgramAnalysis` field, the `PhaseVerdict` relay, the conductor's
`flatness_evidence` stash, the `verify.flatness` state key, and the
`flatness_*` fields on `event=correction.crossover_v2_verify_diag`. A single
mic position cannot answer "is the speaker flat", and having it answer
anyway produced two disagreeing numbers per session (the plan's
MEASURE-vs-VERIFY ledger-discrepancy class).

*Current shape.* One construction: `combine_positions`' power-mean spec
curve → the merged honesty mask (screen ∪ null registry) plus the group's
gate-validity clamp → `flat_spec.evaluate_flat_spec` →
`flat_spec.spec_flatness_gauge`, all inside
`assemble_cloud_group_result`, which publishes it as the pipeline's
`flatness` key. Every surface COPIES that dict: `_compact_cloud_status`
(`/state` + the envelope's `cloud` block), the doctor's
`check_crossover_v2_cloud_pipeline` detail, and
`crossover_envelope_v2._flatness_details_lines`, which renders it into
`expert_details` on the verify_fail and "done"/RESULT screens from
`PHASE_CLOUD_VERIFY` only (`cloud_measure` is the uncorrected pre-apply
baseline — rendering it would report a corrected speaker as bad forever).
Logged once per closed group as `event=correction.crossover_v2_cloud_spec`.
The tolerances are the spec table's own per-band values
(`flat_spec.SPEC_BANDS`) rather than one provisional constant. Contract test:
[`tests/test_flat_spec_ssot.py`](../tests/test_flat_spec_ssot.py).

**No longer report-only** (linearization-integrity PR-L4). It still does not
gate `_verify_verdict`'s accepted/code logic — that stays a tracking judgement
— but the spec verdict now has three consumers that act on it:

* `CrossoverV2Conductor._assert_accountable` grades the RAW and LINEARIZED
  predicted sums through the same `evaluate_flat_spec` and refuses the
  auto-apply when the correction does not materially better its own model
  (`correction_not_an_improvement`);
* `crossover_envelope_v2` reads an explicitly failing `PHASE_CLOUD_VERIFY`
  verdict into the done screen's PRIMARY copy and swaps the "Verified." badge
  for one that names which instrument passed — previously the verdict reached
  only a line inside the collapsed disclosure;
* `crossover_v2_status_block` folds it into the new `post_apply_grade` key
  (see below).

**`/state.crossover_v2.post_apply_grade`** (PR-L4 item 4) answers "was the
correction now on the speaker ever checked after it landed?" — `state` is one
of `not_applied` / `graded` (a walked post-apply position group) /
`mark_verified` (VERIFY passed at the mark; express's whole grade) /
`inconclusive` / `failed` / `unverified`, with `graded` as the single boolean a
caller can key on. Read it, do not re-derive it: `jasper-doctor`'s
`check_crossover_v2_applied_is_graded` is its second consumer and warns on an
applied profile that was never graded — the silence
`check_crossover_v2_cloud_pipeline` structurally cannot see, because that check
gates on a FAILING `PHASE_CLOUD_VERIFY` verdict and a missing one renders as no
phase at all.

*The carve-out disclosure* (flat-linearization plan PR-6b, owner decision 1 of
2026-07-25). The gauge says how flat the speaker measured and how many
spec-band bins left grading; `carve_outs_by_band` — same registry, same
`evaluate_flat_spec` report, published as the pipeline's `carve_outs` key —
says WHICH ranges left and why. One entry per spec band, always all of them and
in the report's own order, each holding the ranges that overlap that band
tagged with the instrument that carved them: `identified_null` rows carry
τ/r/rung/depth/classification (the exclusion reason of record), `position_screen`
rows carry none of that because the screen measures disagreement, not an
arrival. The two are listed separately rather than merged, so "both instruments
flagged this range" stays visible; `merged_excluded_bands_hz` remains the merged
view for counting. Two copy registers per band, both owned in
`crossover_v2_flow` beside `_geometry_guidance_copy` so a chart callout and the
expert disclosure cannot diverge: a plain-language `disclosure` headline, and an
`expert` line carrying τ and both r estimates. The rows ride the same chain the
gauge does (`_compact_cloud_status` → `/state` + the envelope's `cloud` block →
`_flatness_details_lines`' `expert_details`), `PHASE_CLOUD_VERIFY` only for the
rendered lines, for the same pre-apply-baseline reason. **The spec table is not
changed by any of this** — 8–16 kHz still reads ±2.5 dB, applied to whatever
survives the carve-out. `carve_outs` is the largest key on a `/state` cloud
entry (3162 of 4056 JSON bytes on the S0 ten-position cloud, measured
2026-07-27) because the copy strings ARE the disclosure; that cost is stated in
`_compact_cloud_status`'s docstring and pinned by
[`tests/test_crossover_v2_cloud_pipeline.py`](../tests/test_crossover_v2_cloud_pipeline.py),
which also pins the copy discipline (no hardware nouns; the `position_invariant`
wording names travels-with-the-speaker OR a fixed path, never one of the two).

*The before/after chart and anomaly callouts* (flat-linearization plan PR-7).
`jts.local`'s `/correction/` crossover page renders the gauge and the
carve-outs above rather than only disclosing them in `expert_details` text:
`deploy/assets/correction/js/crossover/chart.js` draws the combined cloud
curve for both `cloud_measure` and `cloud_verify` on one canvas, and
`cloud.js` renders the carve-outs' `disclosure`/`expert` strings verbatim as
callout cards (server-owned copy; the frontend never phrases anomaly text
itself). Two payload additions feed it: `_compact_cloud_status` gained
`reference_db` (report-level) and `spec_bands[].tolerance_db` — both were
already computed by `evaluate_flat_spec`, only not previously projected —
and a new, separate `_chart_cloud_status`/`cloud_chart` key carries the
decimated curve at its OWN 256-point ceiling (half of
`crossover_v2_flow.CLOUD_CURVE_MAX_JSON_POINTS`'s persisted-artifact
resolution, since this key rides every ~1.5 s envelope poll — measured
20,653 bytes for both phases on the S0 corpus, against 41,161 unhalved).
Kept off the compact `cloud` key so the doctor (which reads only `cloud`)
never has to parse curve-shaped data mixed into it; it still rides the same
envelope response as `cloud`, so the split does not reduce that response's
byte cost, only its shape for a `cloud`-only reader.

**The chart plots each phase relative to its OWN `reference_db`, never a
shared one.** Linearization is cut-only, so `cloud_verify`'s reference is
always at or below `cloud_measure`'s; an early draft plotted both curves in
absolute dB against `cloud_verify`'s reference alone, which displaced the
whole "Before" curve by a level change the spec never grades, under a
corridor that was not testing what it claimed to (caught by review,
2026-07-27). The corridor is `0 ± tolerance_db` per band in this deviation
frame — the same window for both curves — with the y-domain bounded to the
spec's own GRADED frequency range (derived from `specBands`, e.g.
250–16,000 Hz, never hardcoded): the curve's worst point sits inside
`flat_spec.BEST_EFFORT_ABOVE_HZ`'s "never specced" region on real hardware
data (a driver's natural top-octave rolloff), and letting an ungraded
extreme set the y-scale reproduces the same failure one band further out.
The wider displayed range (20 Hz–20 kHz) is still drawn at full resolution
and canvas-clipped where it exceeds that bound.

**Provenance.** A re-armed verify-only session can carry a `cloud` group
forward from an earlier session verbatim (see "Session binding" above) —
`_cloud_summary` stamps each closed group with its producing session id,
and `_compact_cloud_status` compares it against the caller's current
session to render a `provenance_note` (empty unless the data is genuinely
stale, mirroring `_geometry_guidance_copy`'s own "silent unless actionable"
rule). Geometry's own "spread the mic further" guidance
(`geometry_guidance`) renders as a plain caption; `thin_evidence` softens
the copy server-side, so the frontend never branches on it. Contract tests:
[`tests/test_crossover_v2_cloud_visualization.py`](../tests/test_crossover_v2_cloud_visualization.py)
(page-shell ids, hardware-noun discipline over this PR's own authored copy)
and [`tests/js/crossover_cloud_callouts_test.mjs`](../tests/js/crossover_cloud_callouts_test.mjs)
(rendered-HTML pins for the callout/provenance/geometry text, including the
`position_invariant` cannot-classify phrasing, verbatim). The chart's own
pixel rendering is verified on-device only — CI cannot see pixels — and is
still owed to the HW product smoke below.

*The gate-validity clamp.* `cloud_validity_floor_hz` takes the WORST
(highest) reflection-gate floor across the group's positions — the same
"worse of the two" rule `_measure_validity_floor_hz` applies to the driver
branches — and bins below it are excluded from the spec evaluation, from the
reference level as well as the deviations. It is deliberately kept OUT of
`merged_excluded_bands_hz` (and so out of `/state`'s
`excluded_interval_count`), which stays the honesty instruments' own
"how much interference did we find" number; `validity_floor_hz` discloses the
clamp separately and is carried through `_compact_cloud_status` onto `/state`,
the envelope, and the doctor, so a live surface can tell a combed room apart
from one capture's collapsed gate.

Measured cost on the S0 main leg (all pinned by
[`tests/test_flat_spec_ssot.py`](../tests/test_flat_spec_ssot.py)): nine of
ten positions gate to 142.9 Hz — below the 250 Hz spec edge, so the clamp is a
no-op and changes no graded number. `cloud_04` collapsed to 1777.8 Hz, and
clamping there moves **1009 bins** out of the 250 Hz–2 kHz band, re-centres
the reference **−27.2670 → −28.3166 dB**, moves the **headline `max_db`
−8.9399 → −7.8903 dB (+1.0495 dB, the flattering direction — exactly the
reference shift, since the worst bin survives the clamp)**, moves the pooled
RMS 3.7649 → 3.1524 dB, and **flips the 250 Hz–2 kHz band verdict** from
+4.1637 dB (fail) to −1.2855 dB (pass). The headline number therefore moves
*further* than the RMS, and the direction is response-shape dependent —
measured on this corpus, not a property of the clamp. It is the same speaker
graded on fewer bins, visible in the gauge's own `n_bins`/`n_excluded` pair.

*Deferred alternative:* per-position, per-bin validity masking inside
`combine_positions` (mask each position below its own floor, keep the other
positions' good data) is strictly better and is deferred only because it is a
`spatial_combine` signature/estimator change, not a wiring one. Revisit
trigger: a real session where one collapsed gate meaningfully shrinks the
graded band — the `cloud_04` case above is already that evidence.

**The fit's honesty ladder is NOT this claim.** `LinearizationFit`'s
fit/verify/observe levels (including `observe_octave_summary`, rendered by
`crossover_envelope_v2._linearization_octave_rows`) are per-driver
diagnostics on the fit's envelope grid from the single design-axis MEASURE
capture. PR-5 relabeled the rendered line accordingly ("fit residual vs
target (design-axis capture, not the spatial measurement)") so no surface
presents a per-driver fit residual as the measurement. The two will
legitimately disagree.

**One MARK for the whole session: ~1 m on the listening axis, tweeter
height, facing the speaker.** The placement screen encodes a tolerance
window (~±0.3 m distance, ±10 cm height) for that mark. The session
starts there, returns to it for MEASURE and VERIFY, and prompts small
moves around it for the two position groups — so the mic is stationary
*per sweep*, never *per session*. Taps: CHECK is the one tap before any
measuring, CHECK auto-advances into MEASURE, a trusted candidate
auto-arms VERIFY with no household action in between, and each prompted
cloud position needs its own tap because the household has to move the
mic first.

**Pre-capture courtesy tone (issue #1677).** Every capture's program
opens with three short ~1 kHz beeps + ~3 s of silence
from the speaker under test itself, before that phase's own content
resumes — a "quiet please, a measurement is starting" warning ahead of
each capture (16 of them in a Full-tier cloud session, 7 on Express,
where it used to be 3), replacing the 2026-07-23 lab-only interim (a
Mac-side `osascript
beep`, then a fan-in-TTS-lane 3-beep burst). It is composed as an ordinary
prepended segment group on the SAME `ExcitationProgram` the phase already
plays and admits — never a second playback path — so it rides the session's
existing volume/admission machinery for free. Its level is derived per
program channel from that channel's own loudest scheduled stimulus gain
(`courtesy_tone_gain_db`, 6 dB below, clamped to never exceed it and never
positive), and its kind (`KIND_COURTESY_TONE`) is deliberately excluded from
`STIMULUS_KINDS` so the locate/deconvolution machinery treats it exactly
like a silence segment — invisible to analysis, present in the recording.
Default ON with no config switch (see `COURTESY_PRELUDE_ENABLED` in
`crossover_v2_flow.py`); the phone's per-phase `duration_ms` budget derives
from the same lengthened program, so it is not a second thing to keep in
sync. See the "courtesy-tone prelude" section of
[`jasper/audio_measurement/program.py`](../jasper/audio_measurement/program.py)'s
module docstring for the segment shape.

The RESULT screen (phone end screen + wizard `done` screen) states the
outcome plainly first ("Your speaker is tuned. If it sounds worse than
before, you can undo.") with the measured numbers (trims/delay/polarity/
confidence/ripple) folded into a collapsed "Technical details" disclosure,
and Undo given the PRIMARY button on the wizard so the safety net is the
most visible thing on the screen.

## File map

| File | Responsibility |
|---|---|
| [`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py) | The conductor: `CrossoverV2Conductor`, `REASON_REGISTRY`, capture-plan builders (`build_v2_session_spec` / `build_v2_capture_plan` / `build_v2_verify_*`), `bind_program_playback_seams`, `derive_session_volume_db`, `open`/`abandon_measurement_volume`. Also the position-group choreography (flat-linearization PR-3b): the cloud constants + `CLOUD_POSITION_PROMPTS`, `build_v2_cloud_index_phase_map`, `cloud_capture_target` / `cloud_plan_max_attempts` / `assert_cloud_plan_fits_relay_capacity`, `session_wall_clock_ceiling_s`, and the combine seam (`cloud_position_capture` / `cloud_geometry_verdict`). PR-4 adds the contract-derived bands (`_composed_swept_band_hz`, `_derive_cloud_echo_band_hz` → `_CloudEchoBand`, which clamps the analysis band up to `ECHO_BAND_HF_REGIME_FLOOR_HZ` and discloses the clamp — issue #1763) and the wiring-contract assembly (`assemble_cloud_group_result`, `group_cloud_result`). |
| [`jasper/audio_measurement/program.py`](../jasper/audio_measurement/program.py) | Excitation-program model + composers: `ExcitationProgram`, `ProgramSegment`, `RoleBand`, `build_check_program` / `build_measure_program` / `build_verify_program`, `render_program_pcm`, `write_program_wav`, `mesm_gap_samples`. Pure data + pure composers, no safety decisions. |
| [`jasper/audio_measurement/program_analysis.py`](../jasper/audio_measurement/program_analysis.py) | The pure analysis: `analyze_program_capture` → `ProgramAnalysis`; locate/segment, drift (ε), per-driver gated TF, GCC-PHAT polarity/confidence seed + physical-gap-lobed declaration-bounded summed-flatness refinement, prediction, VERIFY tracking. All the analysis tuning constants. It no longer owns any flatness claim — flatness-verify (#1668 PR-D) was retired here by the flat-linearization plan's PR-5 and now lives on the cloud pipeline; see "Flatness" above. |
| [`jasper/audio_measurement/spatial_combine.py`](../jasper/audio_measurement/spatial_combine.py) | The spatial-cloud combiner + echo/geometry diagnostics (flat-linearization S1, #1741 offline core; wired into the live flow by PR-4, #1756): `combine_positions` → `CombinedResponse` (power-mean spec curve, per-position curves, exclusion mask, `.geometry`/`GeometryLock`), `detect_echo` → `EchoDiagnostic` (two-estimator echo detection; `effective_floor_us`, `earlier_dominant_arrival`/`band_below_passband` refusal hardening from PR-2, #1749), `assess_geometry`, `usable_echo_estimates`. Pure computation, numpy only, no I/O/logging/policy. |
| [`jasper/audio_measurement/interference_nulls.py`](../jasper/audio_measurement/interference_nulls.py) | The orthogonal interference-null identification gate (PR-1, #1751): `identify_interference_nulls` → `InterferenceNullReport` of `IdentifiedNull` records (τ/r/rung/depth/classification), fits a null ladder to the combined cloud's measured minima and corroborates against the cloud's arrival estimates; `position_invariant` / `position_dependent` / `insufficient_evidence`. Consumed by PR-4's `assemble_cloud_group_result` and PR-6b's (#1760) carve-out disclosure. Same purity contract as `spatial_combine`, zero production callers until PR-4. |
| [`jasper/active_speaker/session_volume_plan.py`](../jasper/active_speaker/session_volume_plan.py) | One fixed measurement volume per session: `session_measurement_volume_db` (the `min(−20, max(caps))` SSOT) + `SessionVolumePlan` (open/close/abandon, wall-clock ceiling, restore-once latch). |
| [`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py) | The web host: `/correction/crossover/v2/*` endpoint bindings, durable v2 state, the real analyze/publish/playback seams, `resolve_conductor_context`, `handle_v2_apply` / `handle_v2_restore`, calibration resolution, `ensure_crossover_preview_ready`, `persist_conductor_state`. |
| [`jasper/active_speaker/crossover_envelope_v2.py`](../jasper/active_speaker/crossover_envelope_v2.py) | The pure `status → envelope` renderer (schema 8): step list, screen dispatch, `REASON_REGISTRY` → template copy. |
| [`jasper/active_speaker/measured_crossover_candidate.py`](../jasper/active_speaker/measured_crossover_candidate.py) | `MeasuredCrossoverCandidate` — the fingerprinted apply artifact (trims + `MeasuredCrossoverAlignment` + `linearization`), folded through `emit_active_speaker_baseline_config` (`camilla_yaml.py`) and the delay/graph-safety proofs. |
| [`jasper/active_speaker/linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py) | Layer-1a correction envelope (#1668 PR-B): `compose_envelope` → per-bin allowed correction depth + `ReasonCode`, `compute_sigma_curve`, and the term functions it takes the `min` across — `mic_trust_limit` / `repeatability_limit` / `class_prior_limit`, the two stubs, plus the optional cloud-derived `spatial_exclusion_limit` / `position_stability_limit` (flat-linearization PR-6a). Read the module for the current set; this list is illustrative, not a contract. Pure computation, no policy. |
| [`jasper/active_speaker/linearization_fit.py`](../jasper/active_speaker/linearization_fit.py) | Layer-1a fit engine (#1668 PR-C): `fit_driver_linearization` → `LinearizationFit` (cut-only rising Highshelf + `jasper.correction.peq.design_peq` peaking loop, adaptive band trim, the CD-horn top-octave `_hf_continuation_stage` — a Lowshelf-backbone give-back + declared-class hold/taper policy, #1668 — `MAX_NORMALIZATION_SPEND_DB` budget now 18 dB, `correction_giveback_db` (the SSOT the conductor's anchored trim consumes), the `verify_band_hz`/`observe_octave_summary` honesty-ladder fields added in PR-D). Pure computation; the conductor (`crossover_v2_flow._compose_sigma_db` / `_build_candidate`) owns eligibility policy and wiring. Also owns `linearization_filters_by_role`, the reduction the two rich-candidate emission call sites share (`recompose_applied_baseline_yaml` deliberately does not call it — see "Linearization EMISSION" above). |
| [`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py) | The baseline emitter. `emit_active_speaker_baseline_config`'s `linearization` parameter (#1668 PR-D) is what actually plays the Layer-1a fit — see "Linearization EMISSION" above; `_validated_linearization` independently re-validates it (Peaking/Highshelf/Lowshelf, non-positive gain, one leading shelf + one optional trailing Highshelf taper) before any filter reaches CamillaDSP. |
| [`jasper/capture_relay/session.py`](../jasper/capture_relay/session.py), [`spec.py`](../jasper/capture_relay/spec.py) | Session-spanning capture plans: `CapturePlanEntry`, `CaptureBeginDeferred` / `CaptureBeginRefused`, `run_capture_plan`, hold/timeout budgets. Also the one `CAPTURE_PROTOCOL_VERSION`. |
| [`capture-page/`](../capture-page/README.md) | The static phone recorder (Cloudflare Pages). `js/main.js` runs the session-spanning loop when the spec carries a `capture_plan`; `version.json` carries the supported capture protocol. |

## Contracts & invariants (preserve these)

1. **Two-invariant protection model.** Exactly two safety invariants,
   one owner each — everything that once looked like "safety hedging"
   was deleted:
   - *Never too loud:* one derived ceiling per driver. On the
     program-admission path an HF driver's ceiling is
     `min(declared_lf_cap − (sens_hf − sens_lf), −35 dBFS)`, derived
     from declared sensitivities (`derive_hf_measurement_ceiling_dbfs`
     in `driver_protection.py`). This **supersedes** the old −65 dB
     seed on the proven-HP path.
   - *Never the wrong frequency range:* declared band + a proven
     high-pass before any full-range content. MEASURE's channel routing
     carries each driver's crossover filter by construction, so the
     tweeter is always behind its ≥24 dB/oct HP.
2. **Sensitivities live in exactly one place: the declaration.**
   `declared_effective_driver_sensitivities(draft)` (`design_draft.py`,
   #1665) is the SSOT (`manual_settings.drivers[].sensitivity_db_2v83_1m`,
   folded through any declared in-line pad —
   `jasper.active_speaker.driver_pad.effective_sensitivity_db`; the older
   `declared_driver_sensitivities` reader still exists but no longer feeds
   `resolve_conductor_context`). The same mapping threads into program
   admission *and* play-time readmission, so composed levels and the
   admission gate can never disagree about a derived ceiling — and, as of
   #1665, an L-pad'd driver's EFFECTIVE (not naked) sensitivity is what
   sets that ceiling and the session measurement volume.
3. **Session volume is `min(−20 dB, max(caps))`, not `min(caps)`.**
   `session_measurement_volume_db` lets the least-sensitive driver reach
   the reference level; more-sensitive drivers attenuate down digitally.
   `min(caps)` starved multi-way systems (a woofer 40 dB under —
   hardware-found). The value is latched once per session and refused
   below the −60 dB emergency floor. **Nothing moves it, including the
   apply boundary** — see invariant 10a.
4. **Analysis is a pure function of `(program, WAV)`.** No side-channel
   state. The `program_id` is a content hash and fingerprints the
   analysis and the candidate, so a re-run can never be mistaken for a
   resume.
5. **Clock drift is estimated in-capture.** Alignment error = ε ×
   T_separation. Each MEASURE capture embeds a repeated sweep so ε is
   estimated from the longest available baseline (Gamper least-squares
   ratio); baseline disagreement ⇒ glitch ⇒ reject + one retry. The
   repeated sweep is **mandatory**. The primary gate (both the timing
   epsilon and the woofer-repeat level-agreement check) is anchored to the
   WOOFER's first-vs-last located sweep specifically — a design invariant,
   not an artifact of there being only one repeat (sweep-composition PR-A,
   #1668, three interleaved cycles per driver). The tweeter's own repeats
   contribute a diagnostic-only per-role epsilon (never gated) as evidence
   for future hardening.
6. **Adaptive gating, never a false verdict.** The reflection gate width
   sets a validity floor `f_valid_hz = 1/window_s`. VERIFY requires its
   gate window ≥ MEASURE's; if a shorter VERIFY gate is forced, the
   verdict is `verify_inconclusive` — never a false pass/fail.
7. **Apply is read-only compose, then transactional apply.**
   `handle_v2_apply` reopens the published candidate
   (`MeasuredCrossoverCandidate.from_mapping`, the tamper check), gates
   on `expected_candidate_fingerprint`, translates the *measured*
   fingerprint into the *baseline* candidate's own
   `candidate_fingerprint` at the host boundary (asserting the
   composition is still bound to the reviewed measured candidate), then
   rides the existing `apply_baseline_profile` transaction with rollback.
8. **Undo survives everything.** `handle_v2_apply` stashes the
   `pre_apply_profile` and `persist_conductor_state` carries it
   *unconditionally* forward across every snapshot, so
   `handle_v2_restore` can sha-pin a restore to the prior compiled
   config even after a VERIFY re-arm.
9. **The walked-away guarantee.** The `SessionVolumePlan` holds one
   measurement window with an abort target, a wall-clock ceiling, and a
   restore-once latch drained by close / session-death / ceiling. The
   ceiling is sized from the plan the session actually emits —
   `DEFAULT_WALL_CLOCK_CEILING_S` (1800 s) plus
   `WALL_CLOCK_CEILING_PER_ENTRY_S` (120 s) per capture beyond the
   3-entry baseline, hard-capped at `MAX_WALL_CLOCK_CEILING_S`
   (3600 s) — so the Full tier's 16-capture cloud gets 3360 s, Express's
   7-capture cloud gets 2280 s, and the 1-entry re-verify gets the bare
   1800 s. **The number moves with the plan; the
   cap is what makes the guarantee.** A user who walks away can never
   leave the speaker pinned at measurement volume. The voice-daemon measurement pause is held for
   the *whole* session (acquired before the first volume set) so the
   idle reconciler can't revert it. A **failed auto-apply** drains the
   plan proactively too (#1811): that failure is terminal — the
   `apply_failed` seam turns VERIFY's hold into a refusal — so the level
   is restored the moment the apply dies rather than at the phone's next
   begin, which is what every other enforcement here waits for (the
   wall-clock ceiling and the stale-active reconcile are both
   lazy-on-read).
10. **CamillaDSP safety ceiling stays.** As everywhere in the DSP
    graph, `devices.volume_limit = 0.0` and positive writes clamp to
    0 dB. The program graph adds no headroom beyond the main volume.

    **10a. The apply boundary's level move is DECLARED, never
    compensated (#1811).** The conductor's auto-apply swaps the
    production graph ~3 s before VERIFY arms. The applied graph absorbs
    its correction's boost as a pre-split common attenuation
    (`camilla_yaml.linearization_headroom_db` → `active_baseline_headroom`),
    so the same commanded volume drives the speaker measurably quieter —
    −7.9 dB broadband, −14.5/−18 dB in the pilot band, on the session whose
    apply moved that attenuation 0 → −22.458 dB.

    **That attenuation is the excitation-safety property, not a bug to
    cancel.** It is what makes the emitted boost safe: the graph is
    `−H` pre-split and `+L_r(f)` post-split with `L_r ≤ H`, so a boosted
    band lands at or under unity no matter how deep the correction
    (`camilla_yaml.MAX_LINEARIZATION_BOOST_DB`'s note). The compose-time
    excitation clamp (`_compose_verify_program` → `back_off_gain`) models
    `program_peak + session_volume ≤ min(caps)` and knows nothing of
    `L_r`, so raising the commanded volume by `H` to "restore" the level
    would put the boosted band at `min(caps) + L_r(f)` — over the
    compression driver's cap by the branch's own boost, on a sustained
    swept sine, far below the per-driver limiters' −12 dBFS reach. The
    exactly-safe grant is `H − max(L_peak) = 0`. **VERIFY therefore
    measures the corrected speaker at the unchanged commanded level.**

    What the move needs is to be *declared to the analysis*, because the
    post-apply capture is compared against a prediction that carries no
    such term. `handle_v2_apply` computes it
    (`baseline_profile.applied_program_level_delta_db` — a difference of
    two `linearization_headroom_db` calls, so it cannot drift from the
    emitted gain) and `observe_apply_success` persists it as
    `expected_post_apply_offset_db` in the **same state write** as the
    `applied` flag — so the flag that releases VERIFY's hold can never
    become visible without the offset beside it. The conductor reads it
    back through the `applied_offset_db` seam and hands it to
    `classify_delta_probe`, which removes it before classifying.

    The **VERIFY tracking gate needs no such treatment** — it is already
    level-offset-invariant (`audio_measurement.analysis.
    _offset_invariant_rms_and_max` mean-centers; pinned by
    `test_the_notch_excluded_gate_is_level_offset_invariant_too`). The
    delta probe deliberately is not, because a level shortfall is one of
    the things it classifies, which is exactly why it needs the offset
    handed to it explicitly.

    The declared offset is an honest **partial** account: it reads the
    linearization headroom only, so a household with room-PEQ or
    output-trim attenuation can carry a real move it does not see. That
    remainder is measured where the correction commanded nothing and
    surfaces as `delta_probe.VERDICT_LEVEL_MISMATCH` — a named finding,
    not a rollback (see that constant's own note for why reverting on our
    own bookkeeping gap would be a false accusation).

    The household-facing loudness drop is **not** owned here: its root
    cause is the size of the charge (#1808 shrinks it to ~4 dB) and the
    two-stage flow (#1806) makes the apply user-confirmed, so the change
    stops being a surprise.
11. **Linearization emission is independently re-validated at every
    boundary, never trust-the-caller (#1668 PR-D).** The emitter
    (`_validated_linearization`) and the runtime-safety verifier
    (`_consume_linearization_chain`) each re-prove biquad type ∈
    {Peaking, Highshelf, Lowshelf}, non-positive gain, and the
    shelf-placement structure (one leading shelf + one optional
    trailing Highshelf taper, #1668) from scratch — the fit engine's
    own cut-only invariant is not assumed to have survived a JSON
    round-trip. Full safety-posture rationale (the
    non-positive-gain policy, the boost-cap deferral) is owned by
    [`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md);
    this doc does not restate it.

## Failure taxonomy & debugging

Terminal verdicts are **internal reason codes, not screens.**
`REASON_REGISTRY` (in `crossover_v2_flow.py`) maps each code to one of
four templates (`silent_auto_retry` / `fix_and_retry` / `hard_stop` /
`session_restart`) plus the two special screens (`verify_fail`,
`volume_recovery`), its owning phase, and its retry budget. The
conductor decides the code; the envelope renders the copy — one copy
source, no drift.

| Code | Phase | Budget | Meaning |
|---|---|---|---|
| `agc_behavioral_fail` | CHECK / MEASURE / VERIFY | 1 | phone AGC changed levels mid-capture |
| `noisy_room_linearity` | CHECK | 1 | linearity failed *and* the ambient SNR floor failed — room, not phone |
| `snr_floor` | CHECK / MEASURE | 1 | room too loud / phone too far; also the quiet pilot's own in-band SNR too low to trust the linearity estimate (gotcha #16) |
| `channel_map_mismatch` | CHECK | 0 (hard stop) | drivers played out of order (wiring, or a very noisy/quiet room) |
| `clipped` | MEASURE / VERIFY | 1 | auto quieter retry (gain −3 dB) |
| `drift_baselines_disagree` | MEASURE | 1 | glitch/dropped-buffer, or woofer-repeat level disagreement — auto retry. One code covers the whole capture-glitch class by design; `glitch_inputs` in the diag says which bound actually tripped (#1765) |
| `delay_exceeds_search_window` | MEASURE | 1 | mic likely off the pictured spot |
| `locate_failed` | any | 1 | couldn't hear the speaker |
| `program_unplayable` | play seam | 0 (hard stop) | admission refused the program (bug/tamper/infeasible profile) |
| `internal_error` | any host fault | 0 | catch-all cleanup arm caught a seam raise |
| `relay_timeout` | any | new session | link/session died — Start over mints a fresh one |
| `user_stopped` | any | new session | the household tapped Stop on the phone — honest copy, not a manufactured "timed out" (gotcha #18) |
| `volume_unresolved` | session | — | the `volume_recovery` screen |
| `verify_out_of_tolerance` / `verify_inconclusive` | VERIFY | 2 | Try again / Undo / Re-measure |
| `low_alignment_confidence` | MEASURE | 1 | alignment confidence below the trust floor, OR the measured delay falls outside the crossover region's declared `delay_range_ms` search bound (± a modest margin) — a confidently-wrong GCC estimate. Either way: re-measure at a cleaner mic position (gotcha #18) |
| `apply_failed` | APPLYING | new session | the conductor's own auto-apply came back blocked or errored (gotcha #18). Unlike every other "new session" row, MEASURE's OWN evidence is NOT invalidated (`_persist_terminal_failure`'s §5.6 reset is scoped away from this one code) — an apply failure says nothing about the mic position, and keeping MEASURE accepted is what lets the specific blocked-issue nudge actually render (adversarial review SF2, 2026-07-20) |
| `driver_levels_disagree` | confirm seam | 0 (hard stop) | linearization-integrity PR-L4 item 1: after the committed trim the two drivers' realized levels — read on their own mirrored ±1-octave half-bands about Fc, not across each whole passband — sit further than `REALIZED_LEVEL_MATCH_TOLERANCE_DB` apart, so a flat sum is impossible whatever the per-driver fit achieved. Refused BEFORE the apply thread starts, so the speaker is untouched |
| `correction_not_an_improvement` | confirm seam | 0 (hard stop) | PR-L4 item 2: the PREDICTED post-apply response fails the flat spec and is not better than the measured pre-apply state by `PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`. Also refused before the apply |
| `correction_model_error` | VERIFY / post-apply group | 0 (hard stop) | linearization-integrity PR-L5: the delta probe's realized-vs-commanded map does not match in SHAPE — the emitted filters are not doing what the fit's model of them says. Catches the PR-L2 shelf-Q class permanently. **Fires AFTER the apply**, so it rolls the correction back first and then names itself |
| `correction_level_shortfall` | VERIFY / post-apply group | 0 (hard stop) | PR-L5: the shape landed but the depth did not — realized/commanded scale below `DELTA_PROBE_SHORTFALL_GAIN_CEILING` on a commanded LIFT. A driver-compression diagnostic. Rolled back |
| `correction_spatially_costly` | post-apply group | 0 (hard stop) | PR-L5: the map matched at the mark and the cross-position level spread WIDENED past `DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB` — the correction fitted one position's interference rather than the speaker. Placement, not filters. Rolled back |
| `correction_rollback_failed` | VERIFY / post-apply group | 0 (hard stop) | PR-L5: the probe found one of the three defects above AND the automatic rollback could not run (no binding, a refused restore, or a seam that raised). The correction is therefore **still applied**, and this row exists so the copy says so instead of promising a restore that did not happen. Names Undo as the manual action |

**Auto-apply is no longer unconditional at the confirm seam.** PR-6b's
`_publish_measure_candidate` returned `auto_apply: True` on the reasoning that
MEASURE's trust gates had already decided — true about the CAPTURE, silent
about the CORRECTION built from it. `_assert_accountable` runs its three pre-apply gates — PR-L5's level-frame
agreement, then PR-L4's items 1 and 2, most-specific-first — between the
candidate build and the publish, so a refusal leaves no candidate for anything
downstream to apply. (The four `correction_*` rows are different: they fire
after the apply, from the delta probe.) All three raise through
`CrossoverV2Conductor._refuse`, which stamps `_last_failure_code`: the host's
`CaptureBeginRefused` arm reads THAT, not the exception, and falls back to
`relay_timeout` when it is unset — so raising any other way would render a
deliberate refusal as a manufactured timeout.

**The delta probe verifies the apply, and rolls it back itself** (PR-L5). The
three `correction_*` rows above are the only refusals in this flow that fire
after the speaker has already changed, so each one UNDOES the correction before
it names itself — the household copy says "the previous sound has been put
back" and that is already true when they read it. The rollback runs the same
`handle_v2_restore` the Undo button runs (bound as the conductor's `rollback`
seam by `bind_delta_probe_rollback`), never a second restore path that could
drift from it; a conductor with no binding still refuses and says so on
`event=correction.crossover_v2_delta_probe_rollback`.

What the probe is, and what it is not: it classifies
`measured − predicted` — the SAME comparison the `verify_out_of_tolerance`
tracking check gates on, read off `ProgramAnalysis.verify_tracking_curve` so
there is one construction and two consumers — but over **the band the
correction actually commands something in**, where tracking looks only at the
`[Fc/2, 2·Fc]` handoff window. On JTS3 that is the difference between seeing
a 5–12 kHz shelf-realization defect and not: 2026-07-27's lived an octave and a
half above tracking's band and no tolerance there could have caught it. Design
and the verdict-priority rule: `jasper/active_speaker/delta_probe.py`.

Unlike the tracking check, this comparison is **not** level-offset-invariant —
a level shortfall is one of the things it classifies — so it takes the apply
boundary's declared move (`expected_offset_db`, invariant 10a) and removes it
before classifying. What survives is measured where the correction commanded
nothing and reported as `residual_offset_db`; a material, sufficient residual
is the `level_mismatch` verdict, which is a finding, not a rollback.

Because it is not a rollback it reaches no refusal screen, so it is surfaced
three other ways instead of passing silently: the probe logs at WARNING, the
verdict is persisted as `verify.delta_probe` (four scalars — verdict, reason,
and the two level numbers), and the done screen carries a caveat nudge
alongside its "Verified." badge. When there are too few quiet bins to run the
discriminator at all, the verdict below it carries a
`|level_check_unavailable` suffix in its `reason` — a rollback decided without
that check says so.

**Budgets are cumulative per phase** (compared against the *last*
failure's budget) so alternating codes can't restart the meter; the
relay plan's `max_attempts` bounds the whole session.

Key `event=` lines (via `jasper.log_event`):

```sh
# Conductor phase walk (the /correction/ wizard runs under jasper-correction-web):
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(authorized|play|result|apply|apply_complete|restored|cloud_group_complete|cloud_geometry_retry)'
# Session volume lifecycle (fail-closed). ``persist_failed`` is CRITICAL and
# means the durable intent could not be written — it belongs in any sweep of
# this family, not just the happy three:
journalctl -u jasper-correction-web | grep -E 'event=correction\.session_volume_(opened|restored|restore_failed|persist_failed)'
# Apply boundary (#1811): the declared level move, the proactive volume close
# when the auto-apply dies, and the CRITICAL line when that close could not be
# confirmed (a speaker possibly still at measurement volume — sweep for it):
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(applied|apply_failure_volume_closed|volume_abandon_failed)'
# Calibration handoff / uncalibrated warnings:
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(calibration_resolve_failed|uncalibrated_capture|default_calibration_hint_failed)'
# Accountability + delta probe (PR-L4/L5) — why a session refused, and what
# the speaker actually did with the correction:
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(level_frame_refused|level_match_refused|prediction_refused|realized_level_match|delta_probe|delta_probe_rollback|delta_probe_restore)'
```

`event=correction.crossover_v2_linearization_giveback` carries the shared
level frame beside the trim it produced (`level_frame_system_db`,
`level_frame_reference_role`, `level_frame_offset_db`) — a large
`level_frame_offset_db` is the 10 dB-dark shape being CORRECTED, not a new
problem.

Two further events cover what the correction COSTS (#1808, #1809):

- `event=correction.crossover_v2_linearization_fit_band` — the band each
  driver was allowed to add level in, solved from its own crossover
  (`radiating_band_hz`, `crossover_order`). A boost outside it is the #1809
  defect; cuts outside it are ordinary.
- `event=correction.crossover_v2_linearization_headroom` — per role,
  `chain_peak_db` (what the emitted `crossover ⊗ linearization ⊗ trim` chain
  actually puts above unity), `headroom_cost_db` (that peak plus the 1.0 dB
  `margin_db`, or 0.0 for a chain that never exceeds unity — this is what the
  emitter attenuates the program by), `trim_db`, and `sum_of_positives_db`
  (what the retired pre-#1808 rule WOULD have charged, kept so the reclaimed
  loudness is visible in the journal). A large gap between the last two is the
  correction spending nothing where it used to spend a lot.
- `event=correction.crossover_v2_linearization_no_crossover` (WARNING) — a
  role the session's preset carries no crossover region for. Its branch is
  treated as running full range: no lift bound and no headroom credit, which
  is what the emitter would build for it. On a 2-way conductor this is a
  defect in the supplied preset, which is why it is a warning rather than
  silence.

**Reading a `headroom_cost_db` from before 2026-07-28.** A candidate
persisted under the retired sum-of-positives rule discloses a much larger
number than re-emitting it would charge today — ~22.5 dB vs ~5 on the
2026-07-28 JTS3 profile. The stamp is deliberately not re-derived on load
(it records what that graph was emitted with); a recommission replaces it.

### Per-capture diagnostics — every capture logs its numbers

Before this, `event=correction.crossover_v2_result` carried only
`accepted`/`code` — a failed hardware run left no numbers to look at, and
only a *glitch* MEASURE capture got a partial view via
`event=program_analysis.glitch` (epsilon/residual/repeat-level only, WARN
level, glitch captures only). `CrossoverV2Conductor` now emits one
additional `log_event` per consumed capture, **on the accepted path AND
every rejection**, carrying that phase's full numeric diagnostics (pure
additive observability — none of these calls choose a verdict). The two
position groups emit `crossover_v2_cloud_diag`, which is 13 of the 16
captures in a Full-tier session (4 of Express's 7) — grep for
`check|measure|verify` alone and you see three:

```sh
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(check|measure|verify|cloud)_diag'
```

- `correction.crossover_v2_check_diag` — `accepted`, `code`,
  `pilot_snr_ok`, plus per-role (`woofer_`/`tweeter_`) `snr_db`,
  `captured_delta_db`, `programmed_delta_db`,
  `channel_map_target_rise_db`, `channel_map_cross_rise_db`.
- `correction.crossover_v2_measure_diag` — `accepted`, `code`,
  `alignment_confidence`, `alignment_confidence_source`,
  `alignment_seed_delay_us`, `alignment_refinement_delta_us`,
  `alignment_seed_ripple_db`, `flatness_improvement_db`,
  `anchor_delay_us`, `snap_delta_us`, `snap_found`,
  `gate_window_ms`, `validity_floor_hz`,
  `epsilon_ppm`, `max_residual_samples`, `repeat_level_delta_db`,
  `glitch_inputs`, `discontinuity_samples`, `discontinuity_after_segment`,
  `delay_us`, `delay_role`, `polarity`, `predicted_ripple_db`, plus
  per-role `woofer_snr_db`/`woofer_snr_verdict`/`tweeter_snr_db`/
  `tweeter_snr_verdict`. (A `linearization` field rode this line until the
  2026-07-27 timing move. The fit now runs eight captures later, so this line
  could only ever have reported `""` — the field moved to
  `..._candidate_built` below rather than being kept as a permanently-empty
  one, the same treatment PR-5 gave the per-capture `flatness_*` fields.)
- `correction.crossover_v2_candidate_built` — once per built candidate, from
  whichever capture is the last before the apply: `candidate_fingerprint`,
  `linearization` (the outcome — fitted / trim_rejected / ineligible_* /
  fit_failed), `cloud_evidence` (did a cloud verdict reach the envelope),
  `excluded_bands`, `cloud_positions`. Absent entirely when a measurement was
  rejected — a session with no candidate makes no linearization claim.
  Its sibling `correction.crossover_v2_fit_without_cloud` (WARNING) names the
  honest degradation: a group whose pipeline never became available, so the
  fit ran with no cloud terms.
- `correction.crossover_v2_verify_diag` — `accepted`, `code`,
  `max_db_notch_excluded` (the number the tolerance actually gates on),
  `verify_tolerance_db`, `verify_gate_window_ms`, `measure_gate_window_ms`
  (the comparability pair behind `verify_inconclusive`), `validity_floor_hz`,
  `tracking_band_lo_hz`/`tracking_band_hi_hz`, `rms_db`.
- `correction.crossover_v2_cloud_spec` — the spec verdict, once per CLOSED
  group (not per capture): `phase`, `available`, `reason`, `spec_passed`,
  `spec_evaluable`, `flatness_max_db`, `flatness_max_hz`, `flatness_rms_db`,
  `spec_n_excluded`, `validity_floor_hz`. Emitted from `_run_cloud_pipeline`,
  and since the flat-linearization plan's PR-5 it is the ONLY place a
  flatness number is logged (see "Flatness" above).

Source: the `_log_check_diag` / `_log_measure_diag` / `_log_verify_diag`
methods on `CrossoverV2Conductor` in `crossover_v2_flow.py`, called from thin
`_consume_<phase>` wrappers around the unchanged `_<phase>_verdict` logic.
Two small threads-through landed alongside this so the numbers were actually
on the object: `program_analysis.DriftEstimate.repeat_level_delta_db` and
`PilotObservation.snr_db` / `.channel_map_target_rise_db` /
`.channel_map_cross_rise_db` (previously local variables inside
`_estimate_drift` / `_channel_map_ok`, logged transiently or not at all).

### Operator capture retention — raw WAVs for offline analysis

Off by default. An operator debugging a hardware failure creates the marker
file, and every subsequent capture's raw WAV + a diagnostic sidecar lands on
disk for offline analysis (this productizes a hot-patch that used to live
directly in `bind_production_analyze._analyze` and kept getting silently
wiped by every deploy — runtime Python is copied fresh from the rsync
checkout into `/opt/jasper`, see AGENTS.md "Runtime Python lives in
/opt/jasper").

```sh
# Enable — creates the dir + marker; next capture onward is retained:
ssh pi@jts.local 'sudo mkdir -p /var/lib/jasper/xover-capture-dump && \
  sudo touch /var/lib/jasper/xover-capture-dump/ENABLED'

# Inspect what landed:
ssh pi@jts.local 'ls -la /var/lib/jasper/xover-capture-dump/'
scp 'pi@jts.local:/var/lib/jasper/xover-capture-dump/*' ./captures/

# Disable — delete the marker (or the whole directory); the very next
# capture goes back to zero retention behavior, no restart needed:
ssh pi@jts.local 'sudo rm -f /var/lib/jasper/xover-capture-dump/ENABLED'
```

Each retained capture is two files, `<timestamp>_<phase>_<device>.wav` +
`<timestamp>_<phase>_<device>.json`. The JSON sidecar carries `phase`,
`device_label`, `wav_bytes`, `wav_sha256_12`, `setup_mode`,
`setup_calibration_id`, and `diagnostic` — the same
`program_analysis.analysis_diagnostic_summary(analysis)` numbers as the
per-capture diag events above (keyed by each response's own role string
rather than a hardcoded woofer/tweeter label, since this runs at the
`analyze` seam, before the conductor's role mapping exists — so it has no
`accepted`/`code`, only the analysis's own numbers).

Ring-buffered by **both** file count (`XOVER_CAPTURE_DUMP_MAX_FILES = 90`)
and total bytes (`XOVER_CAPTURE_DUMP_MAX_BYTES = 300 MB`), oldest-first
deletion, so a forgotten marker cannot fill the SD card. The enable marker
itself (`XOVER_CAPTURE_DUMP_ENABLED_MARKER`) is excluded from both caps and
never a prune candidate — without that, the ring buffer would eventually
delete its own on/off switch (it's typically the oldest file in the
directory) and silently re-disable retention. Because the intended operator
workflow is `ls`/`scp`/`rm`-ing this directory *while captures keep
landing*, a file can legitimately vanish between one step of a prune pass
and the next; every `.stat()`/`.unlink()` in `_prune_capture_dump` is
individually guarded (skip a vanished file, don't fail the pass), and the
whole prune body is additionally wrapped so any other `OSError` still
degrades to a WARN instead of propagating — genuinely never-raise, not
merely best-effort by convention. A write OR prune failure is caught and
logged at `event=correction.crossover_v2_capture_retain_failed` (WARN) and
never affects the measurement itself; a successful retain logs
`event=correction.crossover_v2_capture_retained` (`phase`, `bytes`, `path`).
Diagnostic-logging failures (Part 1) are guarded the same way, through
`CrossoverV2Conductor._safe_log_diag` — a bug in a `_log_*_diag` method logs
`event=correction.crossover_v2_diag_log_failed` (WARN) instead of crashing
the capture or changing the verdict already decided.
Source: `_maybe_retain_capture` / `_prune_capture_dump` in
`jasper/web/correction_crossover_v2.py`; constants
`XOVER_CAPTURE_DUMP_DIR` / `_MAX_FILES` / `_MAX_BYTES` at the top of that
module.

Session state on the Pi (both mode 0640, atomic writes):

- **Conductor/flow state:**
  `/var/lib/jasper/active_speaker_crossover_v2_state.json` — phase,
  candidate, verify, failure, `apply_blocked`, `pre_apply_profile`,
  `applied`, evidence refs, `session_id`. Threaded into the envelope as
  `status["crossover_v2"]`.
- **Session volume state:**
  `/var/lib/jasper/active_speaker_crossover_session_volume.json` —
  `status`, `opened_at`, `measurement_volume_db`,
  `original_main_volume_db`. A missing/malformed file hydrates
  fail-closed.

Endpoints (POST, dispatched from `correction_setup`):
`/correction/crossover/v2/session`, `/apply`, `/verify`, `/restore`,
and the shared `/correction/crossover/recover-volume`.

## Hardware benchmarks (campaign results, 2026-07-18/19, JTS3 + UMIK-2)

Attributed as campaign measurements, not code guarantees:

- **Start → applied crossover: 75 s** (run 7, scripted full pass, 2026-07-18).
- **ε (clock drift):** ≈30 ppm, repeatable 29.90–30.02 ppm across runs
  (0.68 µs equivalent delay repeat), agreeing with an independent bench
  probe to 0.1 ppm. Uncorrected, the same rig would accumulate
  ~200–300 µs across a program — why the repeat is mandatory.
- **Trim repeatability:** 0.02 dB. First calibrated run applied a
  tweeter trim of **−16.41 dB**, with the calibration id resolved and
  applied across all three phases (recorded under `evidence.calibration`
  in the v2 state file).
- **Failure honesty verified:** a deliberately bad desk placement gave a
  0.667 ms gate window → 1500 Hz validity floor, and the flow returned
  `verify_inconclusive` rather than a false pass — the design working as
  intended.
- Reference drivers: Dayton Epique E150HE-44 woofer (~83.3 dB) + B&C
  DE250-8 compression tweeter (~108.5 dB), LR4 @ 2000 Hz — a 25.2 dB
  sensitivity spread that drove the W6.5 sensitivity-derived ceiling
  ruling.

Analysis tuning constants live at the top of `program_analysis.py`
(linearity `LINEARITY_TOLERANCE_DB`, repeat `REPEAT_LEVEL_TOLERANCE_DB`,
channel-map `CHANNEL_MAP_TARGET_RISE_DB`/`CHANNEL_MAP_CROSS_RISE_DB`,
alignment `DEFAULT_ALIGN_SEARCH_MS`/`GCC_UPSAMPLE`, VERIFY
`VERIFY_NOTCH_EXCLUSION_DB`) and `crossover_v2_flow.py`
(`VERIFY_TOLERANCE_DB`, `MEASUREMENT_DISTANCE_M`). All are **PROVISIONAL**
pending broader ~1 m runs — a constants-tuning pass is owed (Future work).

The GCC alignment band, flatness-delay objective, trim solve, predicted
ripple, and VERIFY-tracking band are all clamped to the true driver-sweep
overlap —
`[max(Fc/2, tweeter_sweep_lo), min(2·Fc, woofer_sweep_hi)]` — rather than
trusting the nominal `Fc ± 1 octave` span, since a driver's MEASURE sweep
only ever excites its own declared band (e.g. a tweeter sweep starting AT
Fc leaves `[Fc/2, Fc)` as pure deconvolution noise for that branch). One
SSOT helper, `_overlap_band_hz` in `program_analysis.py`, computes the
clamp; every consumer reads the real sweep bounds off the program's own
segments rather than re-deriving the nominal edges.

### Delay selection — physical anchor primary, gated local-peak snap

**Selection is anchor-primary; summed-magnitude flatness is evidence, never a
selector.** Methodology decision:
[crossover-measurement-reproducibility-plan.md](historical/crossover-measurement-reproducibility-plan.md)
§10, 2026-07-22 (bake-off verdict + methodology entries). The narrowband
flatness objective's basin ordering is capture-noise dependent and preferred
the wrong comb lobe on a hardware repeat, so it no longer chooses the delay.

`_estimate_alignment` remains the coarse, drift-corrected GCC-PHAT source for
polarity and capture-quality confidence, and now also computes the fine stage.
Two steps:

1. **Anchor (primary value; owns lobe selection).** The drift-corrected
   physical peak gap `(argmax|tweeter IR| − argmax|woofer IR|)/fs` with the
   inter-sweep clock term removed, plus declared parallax, in
   `AlignmentEstimate`'s signed frame. The anchor is non-periodic, so it selects
   the comb lobe outright — it cannot land on a neighbouring lobe the way GCC's
   periodic correlation peak can.
2. **Gated local-peak snap (fine step).** `_gcc_local_peak_snap` snaps the
   anchor to the nearest local maximum of the SAME upsampled GCC-PHAT
   correlation `_estimate_alignment` already computed (shared `_gcc_correlation`
   core — one correlation, never a second formula), searching only within
   ±(period/6) at Fc (`GCC_SNAP_RADIUS_PERIODS`, ≈83 µs at Fc = 2 kHz — the λ/6
   GPS lobe-selection budget). Magnitude finds the peak; the same ±1-bin
   `_parabolic_peak` sub-sample refine as the global-peak path applies. No local
   maximum inside the radius ⇒ the bare anchor is kept (`snap_found=False`). The
   snap is bounded closed-form, so it can never rail onto a neighbouring lobe,
   and it heals the ±1–2-sample integer-argmax jitter of the bare anchor (the
   reproducibility clause — bake-off: a 44.7 µs anchor jump collapsed to 6.9 µs).

`_build_candidate` selects `alignment.snapped_delay_us` when present, else the
bare anchor; polarity/confidence machinery is unchanged. GCC's global
correlation peak stays the polarity and capture-quality seed (`seed_delay_us`,
`confidence_source='gcc_phat_seed'`) and is NOT the applied delay. The declared
`delay_range_ms` (expanded by `ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS`) is the
outer plausibility rail (Fix 3): a final selected value outside it routes to
`low_alignment_confidence` re-measure guidance in `crossover_v2_flow`, never
auto-apply. `delay_target_driver` is intentionally not required — a fresh preset
has no applied-delay target until this measurement chooses one.

The complex branch TFs are independently argmax-peak-referenced. The raw
deconvolved-IR argmax gap must first have the inter-sweep clock term
`ε × (tweeter_start − woofer_start)` removed. The remaining physical peak gap is
retained: the listening-plane prediction phases the tweeter by
`objective_reference_gap + selected_signed_delay` (the residual relative to the
argmax-referenced frame — never the full applied delay, the reverted fix-2).
Removing the whole peak gap loses real driver timing; retaining its clock-drift
component recreates the 2026-07-22 JTS3 mismatch. After selection, the alignment
record preserves `delay_us == raw_delay_us - parallax_us`; `seed_delay_us`
retains the corrected GCC seed. `alignment_confidence` remains GCC seed/capture
confidence, labelled `gcc_phat_seed` — it is not a confidence score for any
flatness minimum.

Flatness survives only as evidence on the candidate: `alignment_seed_ripple_db`
is the summed ripple AT the anchor, `flatness_improvement_db` is
`anchor_ripple − selected_ripple` (may be slightly negative — the snap is chosen
for lobe-correctness, not ripple), and `anchor_delay_us` / `snap_delta_us` /
`snap_found` record the fine step. `flatness_at_bound` is retired.

VERIFY compares the applied response with the independently aligned
zero-residual target sum. Do not phase that reference by a candidate-specific
delay: doing so lets a wrong comb-lobe apply explain itself and recreates the
fix-2 false-pass class. The selected applied delay is what proves the correction
realizes the aligned target in the original physical frame.

Both measured and predicted magnitude curves receive the same 1/6-octave
smoothing before tracking error is computed. The unsmoothed prediction is used
only to identify the interior of a genuine modeled notch for the established
notch-exclusion mask. Comparing a smoothed capture with a raw prediction caused
a false 1.99 dB failure at the hardware-best delay; like-for-like comparison of
that same capture is 0.490 dB max (raw-to-raw is 0.606 dB).

## Gotchas — the W6 bug-class catalog (do not reintroduce)

Each was found on hardware and fixed at root cause (no wrapper layers,
no retries-as-bodge). Treat these as regression fences.

1. **Read the playback device through `resolve_active_playback_device`,
   never a nonexistent `topology.playback_device`** (#1590). The
   topology has no such attribute; `resolve_conductor_context` resolves
   it via `playback_route.resolve_active_playback_device`.
2. **Session volume is `min(−20, max(caps))`.** `min(caps)` starved the
   woofer ~40 dB; the emergency-floor invariant would also catch the
   inverted derivation at runtime (#1591-adjacent).
3. **Hold the measurement pause + volume for the whole session.** The
   jasper-voice idle reconciler reverted the session volume within
   ~200 ms when it was protected only per-play; open the pause *before*
   the first set and register the abort target (#1591). Seam raises
   (`ProgramPlaybackRefused`, `CamillaUnavailable` — a bare `Exception`)
   must hit the catch-all cleanup arm, not escape leaving volume active
   and the phone frozen.
4. **Use `DEFAULT_CAMILLA_CONFIG_DIR` as the writer-lock SSOT** (#1592).
   Creating `.dsp_apply.lock` under a read-only path raised `EROFS`; a
   local seam `OSError` is wrapped (`CrossoverV2LocalSeamError`) so it is
   never misclassified as a relay-transport death.
5. **Pipeline references and mixer names must close.** The emitter
   produced mixer `program_route_2way` while the pipeline referenced
   `split_active_2way`; CamillaDSP rejected it only at LOAD time (#1593).
   `pipeline_reference_closure_errors` (`graph_safety.py`) is now a build
   gate that reports *every* dangling reference before apply.
6. **Channel-map is band-relative, not total-energy** (#1594). LF room
   rumble vetoed a total-energy discriminator; identification now needs
   target-band rise ≥12 dB over that channel's own ambient and cross-band
   rise <6 dB.
7. **The −65 dB tweeter cap is a relic** (#1595). The HF measurement
   ceiling is derived from sensitivity (invariant 1/2 above); the old
   seed read near-inaudible (27 dB in-band SNR) on the DE250.
8. **Apply must translate fingerprint vocabularies** (#1596). The seam's
   freshness guard compares the *baseline* candidate's fingerprint;
   forwarding the *measured* fingerprint made every apply refuse
   `baseline_candidate_fingerprint_mismatch`.
9. **Never compare depths inside a predicted notch** (#1597). VERIFY
   tracking excludes predicted-notch regions (keyed on predicted level)
   and clamps to this capture's own validity floor — comparing notch
   depths is meaningless (a run-7 27.83 dB raw max against a predicted
   sum whose own ripple was ~30 dB).
10. **Undo must reach a v2-aware path, not the legacy 500** (#1598).
    `/crossover/v2/restore` reloads the stashed `pre_apply_profile`; the
    legacy `/crossover/restore` expects a pending commissioning-run apply
    a v2 apply never creates.
11. **Predictions must share the adaptive reflection gate** (#1600). A
    fixed-65 ms prediction window baked a desk-bounce null into the
    predicted sum, invisible to the gate-comparability rule; the
    prediction now uses the same adaptive gate as `_driver_response`
    (verified rms 1.496 dB / max 5.115 dB on a real WAV).
12. **The deferred REVIEW hold has its own watchdog budget** (#1601). A
    stale deployed capture page (pre-v3 contract) deadlocked Chrome and
    the watchdog killed the review hold; the deferred hold rescopes to
    `REVIEW_HOLD_BUDGET_S` (900 s) and the page gained hold/countdown
    states.
13. **Ensure the crossover preview at session start** (#1602). A missing
    preview baked the generic bundled preset into the candidate and
    blocked apply forever; `ensure_crossover_preview_ready` (one
    generator, two callers via `save_crossover_preview`) runs at the top
    of `resolve_conductor_context`.
14. **`pre_apply_profile` is carried forward unconditionally** (#1603).
    A VERIFY re-arm used to wipe it, losing Undo.
15. **Calibration piggybacks on every begin** (#1604). The phone posted
    its mic setup (including calibration id) only once, racing the armed
    state, so calibration was never applied. `main.js` now attaches
    `setup: setupWirePayload()` to every `begin_capture` post — a
    last-write-wins slot the Pi reads on each arm.
16. **Linearity is band-relative + ambient-compensated, not full-band
    peak** (2026-07-20). Sibling of gotcha #6/#1594's channel-map fix —
    the linearity gate hadn't gotten the same treatment yet. Two real
    hardware captures (Dayton iMM-6C and UMIK-2, same room/placement)
    both failed `agc_behavioral_fail`: continuous LF room rumble ~30 dB
    above the tweeter-band ambient inflated the quiet woofer pilot's
    full-band PEAK enough to compress the captured 10 dB delta past the
    0.5 dB tolerance, even though both mics agreed the driver was linear
    once measured in its own declared band with ambient-subtracted RMS
    (9.8-10.0 dB on both). `_pilot_observations` now measures each
    pilot's level in its own band (`_band_power`, the same mechanism
    `_channel_map_ok` uses) with the CHECK ambient window's in-band power
    subtracted (power domain) before converting to dB. When the quiet
    pilot's own in-band SNR doesn't clear `PILOT_MIN_SNR_DB` (≈12.4 dB,
    derived from the tolerance + a bounded ambient-nonstationarity model
    — see the constant's comment), the estimate isn't trustworthy either
    way: `linearity_ok` is forced True (never a false FAILURE) and
    `PilotObservation.snr_valid` / `ProgramAnalysis.pilot_snr_ok` flag it
    so `crossover_v2_flow._consume_check` routes to `snr_floor`, never
    `agc_behavioral_fail`.
17. **A pilot level used ABSOLUTELY needs a peak reference, not the
    ambient-subtracted linearity estimate** (2026-07-20, same PR as #16,
    caught in review). `_solve_gain_plan` computes `k = level - gain_db` —
    an absolute estimate of the whole capture chain's dB gain, not a
    delta — then aims `MeasurementPriors.target_capture_dbfs` (documented
    as a capture-PEAK target) through it. Gotcha #16's ambient-subtracted
    `level_*_dbfs` briefly fed this too, silently shifting `k` by however
    much ambient power was subtracted (measured 13-17 dB on the two real
    captures once measured — worse than a synthetic-fixture reviewer
    estimate of ~7 dB — because a real room's ambient floor is far from
    flat across bands). `PilotObservation` now carries a SEPARATE
    `peak_lo_dbfs`/`peak_hi_dbfs` — the exact pre-#16 full-band peak,
    verbatim — for this one absolute-use consumer; `level_*_dbfs` stays
    ambient-subtracted for the (delta-safe) linearity verdict only. An
    in-band (band-limited) peak was tried as a "more robust" replacement
    but empirically introduced its own bandlimiting-leakage bias (up to
    ~1.3 dB on a real capture, windowed or not) — worse than a few tenths
    — so the verbatim pre-#16 computation was kept instead of trading one
    subtle bug for a smaller one.
18. **The human mid-flow Apply gate was a dead end — removed** (owner
    ruling, 2026-07-20). A hardware session proved it out: phone-only
    users cannot bounce to a second browser tab to tap Apply, and "apply
    this?" is unanswerable the moment after measuring — the household has
    no basis to judge a raw candidate. Prior art (Sonos Trueplay, Genelec
    GLM, Anthem ARC) all measure → apply → verify automatically, with the
    human judgment happening AFTER, by ear, with undo available. Fixed by
    promoting the review-screen's confidence nudge
    (`ALIGNMENT_CONFIDENCE_NUDGE_FLOOR`, informed consent) into a hard
    MEASURE-phase gate (`ALIGNMENT_CONFIDENCE_TRUST_FLOOR`, now owned by
    `crossover_v2_flow.py` — the decision-maker, not the renderer) and
    having the conductor fire the SAME apply transaction a household's tap
    used to trigger (`handle_v2_apply`, unchanged, now called from a
    background thread right after the candidate-carrying accept instead of
    from an HTTP handler alone — that accept was MEASURE's when this landed
    and is the pre-apply cloud group's close since 2026-07-27, see "When the
    fit runs"). The `CaptureBeginDeferred` soft-hold mechanism
    before VERIFY is UNCHANGED — only the release trigger
    moved from a human tap to the auto-apply completing, and its copy
    changed from "waiting for the household to apply" to "Applying to
    your speaker…". `REVIEW_HOLD_BUDGET_S` shrank from 900 s (sized for a
    human review) to 30 s (sized for the apply transaction's own latency).
    A separate, unrelated fix landed in the same PR: a deliberate phone
    Stop (`CaptureAborted`, `reason == "stopped"`) was bucketed into the
    same `relay_timeout` ("link timed out") catch-all as a genuine
    transport death — `CaptureAborted` now carries a structured `reason`
    attribute so the two can be told apart, and Stop gets its own honest
    `user_stopped` code.

    **Adversarial review (SF1, same PR): the auto-apply worker didn't
    coordinate with session death.** The background thread had no idea a
    Stop (host-driven `stop_event`, or a phone Stop the relay loop's own
    poll already turned into a persisted terminal failure) had landed, so
    the interleaving could produce incoherent durable state — `applied=True`
    silently clobbered back to a "nothing happened" story, or a `failure`
    code silently clobbering a genuine `applied=True`. Three-part fix: (a)
    a best-effort cooperative pre-apply check (`stop_event.is_set()` OR an
    already-persisted failure code) skips the transaction entirely before
    it starts — logged `event=correction.crossover_v2_auto_apply_skipped_stopped`;
    (b) `observe_apply_success` no longer blindly clobbers an existing
    `failure` code to `None` (the reverse race — `_persist_terminal_failure`
    already preserved `applied=True` once observed, for the same session —
    was already correct); (c) the envelope now appends an honest "the
    crossover was already applied" acknowledgment to any
    `TEMPLATE_SESSION_RESTART` code's copy (`relay_timeout`, `user_stopped`)
    rendered once applied, since that copy's own "start over…" framing is
    written for the pre-apply phases and is actively wrong once something
    genuinely got applied. Neither check can fully close the race (an
    in-flight DSP write can't be safely interrupted mid-transaction) — (b)
    is what guarantees the FINAL DURABLE STATE is always coherent regardless
    of which side of the race wins. (A second adversarial pass found that
    claim did not extend to the RENDER — see immediately below.)

    **Second adversarial pass, same PR: durable-state coherence did not
    imply render honesty ("interleaving A").** (b) guarantees `applied` and
    `failure` end up coherent together, but says nothing about
    `accepted_phases` — and (c)'s acknowledgment originally fired on
    `active_step == "verify"`, DERIVED from `_phase_from_state`, not from
    `applied` directly. When a Stop's `_persist_terminal_failure` call lands
    WHILE the auto-apply transaction is still mid-flight, `applied` reads
    False at that instant, so the §5.6 reset (correctly scoped away from
    `apply_failed` alone, per SF2) fires for `user_stopped` and clears
    `accepted_phases`. The auto-apply's own success can then land moments
    later and flip `applied` True — but `accepted_phases` stays cleared, so
    `_phase_from_state` resolves the combination to `PHASE_CHECK`, not
    `PHASE_VERIFY`. (c)'s acknowledgment, keyed on that derived phase, never
    fired: the household saw "You stopped the measurement. Start over,"
    with no Undo, over a genuinely-changed crossover. Fix:
    `crossover_envelope_v2._failure_envelope` now takes `applied` as an
    explicit parameter — the RAW `status["crossover_v2"]["applied"]` state
    fact — and keys its override on that alone, never on `active_step`/phase.
    This is the general form of the rule the PR should have shipped the
    first time: **any failure screen rendered while `applied` is durably
    True says the crossover was applied and offers Undo, regardless of what
    phase/active_step/template says** — because phase derivation is exactly
    the kind of thing this same race can corrupt.

19. **The repeat-level drift gate is band-relative RMS, not full-band
    peak** (2026-07-20). The THIRD sibling of the same full-band-estimator
    class as gotcha #6/#1594 (channel-map) and #16 (linearity) — this one
    hadn't gotten the treatment. MEASURE plays two bit-identical woofer
    sweeps bracketing the tweeter sweep; `_estimate_drift` rejects
    (`drift_baselines_disagree`) when their captured levels disagree past
    `REPEAT_LEVEL_TOLERANCE_DB` (0.3 dB), the guard against browser AGC
    riding the gain mid-program. It compared `w1.peak_dbfs` vs
    `w2.peak_dbfs` — full-band single-sample PEAK, which is unstable for a
    low-frequency room-mode-excited sweep: the loudest sample jumps between
    otherwise-identical sweeps. Two real captures — a Dayton iMM-6C
    (iPhone) AND a UMIK-2 (computer, no AGC, exonerating the mic path) —
    both false-rejected at ~0.64 dB by peak while agreeing to ≤0.24 dB by
    in-band RMS. `_estimate_drift` now measures each woofer sweep's level
    as in-band RMS in the sweep's own declared band (`_band_power`, the
    #1615 helper, with the composer's edge fade trimmed) — the failing
    0.64 dB drops to 0.14 dB (pass). Teeth kept: a genuine uniform
    (AGC-shaped) gain difference survives band-limiting and still trips the
    gate (`test_repeat_level_step_is_flagged_as_glitch`); a peak-only LF
    transient no longer does (`test_repeat_level_lf_transient_does_not_false_reject`).
    The epsilon/residual timing sub-conditions are untouched.

## Future work — the post-W6 follow-ups issue

Tracked in the post-W6 follow-ups GitHub issue (filed 2026-07-19):

- **W5b — delete the legacy flow outright:** the `crossover_envelope`
  legacy body, the `correction_crossover_flow` legacy handlers, the
  selector, and the legacy test suite. This is the big deletion, gated
  on W6's green hardware run (now met).
- Smaller nits: the `apply_blocked` session-gating detail; a
  topology-fingerprint guard on restore; a candidate-config retention
  story; a hub HTTP-routing nit; placement copy improvements; the
  verify-fail expert disclosure. (Stop-control "timed out" copy — fixed,
  gotcha #18.)
- **Constants tuning pass** once real ~1 m runs accumulate (VERIFY pilot
  band, gate-comparability margin, confidence floor, and the PROVISIONAL
  constants above).
- **Driver-spacing input for parallax correction.** `driver_spacing_m`
  is threaded but stays `0.0` today (topology/preset carry no spacing),
  so the §3.2 parallax correction is inert in production. The flatness
  refinement preserves the nonzero-geometry raw/corrected-frame contract,
  pinned on both signed delay lobes by a production-path test. Parallax is
  self-cancelling at the
  mic position (baked into both MEASURE and VERIFY) but the *listening
  position* carries the full geometric error.
- **`sound_current.yml` does NOT update on a v2 apply — by decision, not
  omission (#1605).** `sound_current.yml` means "the last durable `/sound`
  render," never "the config CamillaDSP is currently running." A v2 apply
  writes the content-addressed `active_speaker_baseline_candidate_<fp>.yml`
  and points CamillaDSP at it; the runtime truth is whatever CamillaDSP's
  statefile reports, and the Layer-A truth is
  `active_speaker_baseline_profile.json`. Mirroring v2 applies into
  `sound_current.yml` would create a second mutable Layer-A artifact and
  weaken the content-addressed Apply/Undo ownership, so we deliberately do
  not converge the bytes. Readers treat it accordingly: `graph_carrier`
  recognizes generated configs by content (the fixed name matters only for
  the PR #1009 stale-bake recovery), `jasper-doctor` uses it as a
  last-resort fallback and recognizes content-addressed active-baseline
  names, and `multiroom.leader_config` stashes/restores whatever CamillaDSP
  reports live rather than opening a fixed name. See
  [`HANDOFF-sound-preferences.md`](HANDOFF-sound-preferences.md) for the
  `sound_current.yml` lifecycle. (Deferred cleanups, not required by this
  decision: drop the doctor's fixed-file fallback in favor of an explicit
  active-path-unavailable report, and a name migration to
  `sound_preferences_current.yml` — both owner-gated.)

## Boundaries / non-goals

- **3-way is a v2 non-goal.** The program/WAV layer generalizes to N
  channels, but the candidate and prediction would need to reshape from
  one alignment triple to per-boundary entries — a schema change.
- **Subwoofer/main alignment belongs to the bass-extension program.**
  v2 measures nothing below its gated validity floor.
- **Fc/slope re-derivation and driver EQ beyond trims are a v3 door.**
  v2 deliberately measures *as-crossed* branches and cannot recover them
  (dividing out the target filter explodes stopband noise).

---

## History appendix — the campaign (W1–W6)

Snapshot narrative, for "why did we end up here," not current state.

The v2 rebuild ran 2026-07-17 → 2026-07-19 (PRs #1578–#1604), architected
by Fable. Its motivation and full decision record are in
[`crossover-measurement-productization-design.md`](crossover-measurement-productization-design.md);
the first-principles research is
[`crossover-measurement-deep-research-2026-07-18.md`](crossover-measurement-deep-research-2026-07-18.md);
the on-hardware log that motivated it is
[`crossover-room-e2e-validation-log.md`](crossover-room-e2e-validation-log.md).

**Why v2 exists.** The legacy flow's cost was structural, not
parametric: a full automatic 2-way run was ~17 page actions + ~12
phone-capture round-trips across two mic geometries, and its delay/
polarity machinery was never wired into the wizard. The ~86 fix-PRs it
absorbed in 2026-07 concentrated in exactly the machinery that
multiplicity demands (repeat admission, geometry handoff, identity
validation, volume restore) — the measurement *math* was never the bug
source. v2's lever was collapsing the interaction topology, not tuning
steps: fewer/richer captures, one mic position, zero user-facing
leveling, all intelligence server-side in pure functions. (History, as
written in 2026-07: "one mic position" held until flat-linearization
PR-3b traded it back for the spatial cloud — deliberately, because the
plan's own evidence says a single point cannot separate the speaker
from the room. The *interaction* lever it describes is intact; the mic
count is not.) This mirrors
every shipping calibrator that owns its output chain (Genelec GLM,
Trinnov, Anthem ARC, Sonos Trueplay) — none exposes a level control;
Dirac/REW push leveling onto the user precisely because they don't own
the chain.

**Wave plan (each wave: implementer in an isolated worktree →
hardware-free tests in the same PR → adversarial-review gate (0
blockers / 0 should-fixes) → green CI → squash-merge). Contracts frozen
so waves could run in parallel.**

- **W1 — measurement core (pure).** `program.py` composer + locator /
  segmenter + drift estimator + GCC-PHAT sub-sample alignment +
  `analyze_program_capture` + prediction. Synthetic-fixture round-trips
  with injected ε / delay / polarity / noise / glitch.
- **W2 — playback + safety.** Channel-routed commissioning graph variant
  + multi-segment excitation admission + `SessionVolumePlan` (fail-closed
  latch reuse) + admitted playback. The W2 adversarial gate caught the
  `min(caps)` misreading and reframed it as `min(−20, max(caps))`.
- **W3 — protocol.** `CapturePlanEntry` (spec + session loop + capture
  page); per-entry locator windows; the relay worker stayed opaque.
- **W4 — apply extension.** `MeasuredCrossoverCandidate` — measured
  polarity/delay through the preset → `camilla_yaml` → delay/graph-safety
  proofs; candidate fingerprint over the new evidence.
- **W5a — the v2 happy path.** The conductor phase orchestration, the
  schema-7 envelope, the auto-advance tap policy, the four failure-screen
  templates, phase persistence + session binding, and the MEASURE/VERIFY
  leading pilot pair + repeat-agreement acceptance. Legacy kept as the
  fallback.
- **W6 — hardware validation (JTS3 + UMIK-2).** The scripted-then-Chrome
  validation ladder: first a scripted bench probe (five trials through
  the mux test-gate → `correction` lane → production chain) established
  ε ≈ 30 ppm and the longest-baseline rule; then full runs through real
  Chrome + relay + the phone. W6 surfaced the bug catalog above across
  run rounds — the first runs (W6.1) caught five cap/cleanup/volume
  defects; W6.5 was the sensitivity-derived-ceiling ruling; W6.7/W6.9
  were the measurement-honesty (notch-aware, gate-consistent prediction)
  fixes; W6.10–W6.12 closed the Chrome-round deadlock, the calibration
  race, and the Undo/`pre_apply_profile` forward-carry. Run 7 reached
  start→applied in 75 s; the first fully-calibrated run (2026-07-19)
  applied a −16.41 dB tweeter trim with calibration resolved on all
  three phases.
- **W5b — deletions + polish.** Gated on W6's first green run (now met);
  see Future work. Deleting the only working flow before the replacement
  touched hardware was the one sequencing risk the plan refused.

The default flipped to `v2` on 2026-07-19. W5b (2026-07-24) then deleted the
legacy flow and the `JASPER_CROSSOVER_FLOW` selector outright — v2 is the only
crossover-measurement flow now.

Last verified: 2026-07-28
