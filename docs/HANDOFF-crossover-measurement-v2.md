# Handoff: crossover measurement v2 — the conductor flow

The v2 flow measures and applies a fully-active 2-way crossover's
**level, delay, and polarity** from **three captures at one microphone
position** and ~3 phone taps. The phone is a dumb recorder; the Pi is
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
  speaker at tweeter height, tap Start, then follow the phone — apply is
  automatic (owner ruling, 2026-07-20; gotcha #18), no browser-tab step
  in between.
- **Flow selector — `JASPER_CROSSOVER_FLOW`.** Resolved by
  `active_crossover_flow()` in
  [`jasper/active_speaker/crossover_flow.py`](../jasper/active_speaker/crossover_flow.py).
  **The default is `v2`** (flipped from `legacy` on 2026-07-19 after W6
  hardware validation); the single opt-out is the exact literal
  `JASPER_CROSSOVER_FLOW=legacy` (case-insensitive, trimmed). Any
  unrecognized value resolves to the default — fail-safe by
  construction.
- **Phone capture page:** the Cloudflare Pages app under
  [`capture-page/`](../capture-page/README.md), served at
  `capture.jasper.tech`, relaying through `relay.jasper.tech`. Deploy
  from the repo root:
  `npx wrangler pages deploy capture-page/dist --project-name jts-capture-page --branch=main`
  — `--branch=main` is load-bearing: without it wrangler deploys a
  preview alias and the production domain keeps serving the stale page
  (the W6.10 Chrome-deadlock bug class); the custom domain lags the
  deploy by ~5 min. See the capture-page README's release ordering —
  the page's `supported_capture_protocol_versions` must include a
  protocol before the Pi emits it.

## Current status (2026-07-22)

Waves W1–W6 complete (PRs #1578–#1604). Hardware-validated on JTS3 +
UMIK-2: first fully-calibrated run 2026-07-19. **Legacy is deprecated**
and scheduled for deletion in W5b (see Future work). The v2 acoustic
playback binding
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
background thread once a trusted MEASURE accept lands (gotcha #18) — and
tests inject fakes.

### The capture flow

One relay session (`crossover_v2:session`) spans all three captures. The
conductor hands `authorize_begin` / `on_armed` / `consume_capture` to
`run_capture_plan` (`jasper/capture_relay/session.py`):

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
3. **APPLYING** (control page, no capture — auto, since 2026-07-20). The
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
4. **VERIFY** (~15 s, auto-arms on the apply-complete host event). A mono
   summed sweep through the **applied production graph** + a pilot pair.
   Pass = notch-excluded, validity-floor-clamped tracking error ≤ ±1.5 dB.
   On fail the applied graph **stays in force** (proof-checked safe) and
   the household is offered Try again / Undo.

**One mic position for the whole session: ~1 m on the listening axis,
tweeter height, facing the speaker.** The placement screen encodes a
tolerance window (~±0.3 m distance, ±10 cm height). Only the first
capture needs a tap; CHECK auto-advances into MEASURE, and a trusted
candidate auto-arms VERIFY with no household action in between.

The RESULT screen (phone end screen + wizard `done` screen) states the
outcome plainly first ("Your speaker is tuned. If it sounds worse than
before, you can undo.") with the measured numbers (trims/delay/polarity/
confidence/ripple) folded into a collapsed "Technical details" disclosure,
and Undo given the PRIMARY button on the wizard so the safety net is the
most visible thing on the screen.

## File map

| File | Responsibility |
|---|---|
| [`jasper/active_speaker/crossover_flow.py`](../jasper/active_speaker/crossover_flow.py) | The `JASPER_CROSSOVER_FLOW` selector — `active_crossover_flow()` / `resolve_crossover_flow()`. No product policy. |
| [`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py) | The conductor: `CrossoverV2Conductor`, `REASON_REGISTRY`, capture-plan builders (`build_v2_session_spec` / `build_v2_capture_plan` / `build_v2_verify_*`), `bind_program_playback_seams`, `derive_session_volume_db`, `open`/`abandon_measurement_volume`. |
| [`jasper/audio_measurement/program.py`](../jasper/audio_measurement/program.py) | Excitation-program model + composers: `ExcitationProgram`, `ProgramSegment`, `RoleBand`, `build_check_program` / `build_measure_program` / `build_verify_program`, `render_program_pcm`, `write_program_wav`, `mesm_gap_samples`. Pure data + pure composers, no safety decisions. |
| [`jasper/audio_measurement/program_analysis.py`](../jasper/audio_measurement/program_analysis.py) | The pure analysis: `analyze_program_capture` → `ProgramAnalysis`; locate/segment, drift (ε), per-driver gated TF, GCC-PHAT polarity/confidence seed + physical-gap-lobed declaration-bounded summed-flatness refinement, prediction, VERIFY tracking. All the analysis tuning constants. |
| [`jasper/active_speaker/session_volume_plan.py`](../jasper/active_speaker/session_volume_plan.py) | One fixed measurement volume per session: `session_measurement_volume_db` (the `min(−20, max(caps))` SSOT) + `SessionVolumePlan` (open/close/abandon, wall-clock ceiling, restore-once latch). |
| [`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py) | The web host: `/correction/crossover/v2/*` endpoint bindings, durable v2 state, the real analyze/publish/playback seams, `resolve_conductor_context`, `handle_v2_apply` / `handle_v2_restore`, calibration resolution, `ensure_crossover_preview_ready`, `persist_conductor_state`. |
| [`jasper/active_speaker/crossover_envelope_v2.py`](../jasper/active_speaker/crossover_envelope_v2.py) | The pure `status → envelope` renderer (schema 8): step list, screen dispatch, `REASON_REGISTRY` → template copy. |
| [`jasper/active_speaker/measured_crossover_candidate.py`](../jasper/active_speaker/measured_crossover_candidate.py) | `MeasuredCrossoverCandidate` — the fingerprinted apply artifact (trims + `MeasuredCrossoverAlignment`), folded through `emit_active_speaker_baseline_config` (`camilla_yaml.py`) and the delay/graph-safety proofs. |
| [`jasper/capture_relay/session.py`](../jasper/capture_relay/session.py), [`spec.py`](../jasper/capture_relay/spec.py) | Relay protocol v3: `CapturePlanEntry`, `CaptureBeginDeferred` / `CaptureBeginRefused`, `run_capture_plan`, hold/timeout budgets. |
| [`capture-page/`](../capture-page/README.md) | The static phone recorder (Cloudflare Pages). `js/main.js` runs the v3 session loop; `version.json` carries the supported protocol versions. |

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
   `declared_driver_sensitivities(draft)` (`design_draft.py`) is the
   SSOT (`manual_settings.drivers[].sensitivity_db_2v83_1m`). The same
   mapping threads into program admission *and* play-time readmission,
   so composed levels and the admission gate can never disagree about a
   derived ceiling.
3. **Session volume is `min(−20 dB, max(caps))`, not `min(caps)`.**
   `session_measurement_volume_db` lets the least-sensitive driver reach
   the reference level; more-sensitive drivers attenuate down digitally.
   `min(caps)` starved multi-way systems (a woofer 40 dB under —
   hardware-found). The value is latched once per session and refused
   below the −60 dB emergency floor.
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
   measurement window with an abort target, a ~1800 s wall-clock
   ceiling, and a restore-once latch drained by close / session-death /
   ceiling. A user who walks away can never leave the speaker pinned at
   measurement volume. The voice-daemon measurement pause is held for
   the *whole* session (acquired before the first volume set) so the
   idle reconciler can't revert it.
10. **CamillaDSP safety ceiling stays.** As everywhere in the DSP
    graph, `devices.volume_limit = 0.0` and positive writes clamp to
    0 dB. The program graph adds no headroom beyond the main volume.

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
| `drift_baselines_disagree` | MEASURE | 1 | glitch/dropped-buffer, or woofer-repeat level disagreement — auto retry |
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

**Budgets are cumulative per phase** (compared against the *last*
failure's budget) so alternating codes can't restart the meter; the
relay plan's `max_attempts` bounds the whole session.

Key `event=` lines (via `jasper.log_event`):

```sh
# Conductor phase walk (the /correction/ wizard runs under jasper-correction-web):
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(authorized|play|result|apply|apply_complete|restored)'
# Session volume lifecycle (fail-closed):
journalctl -u jasper-correction-web | grep -E 'event=correction\.session_volume_(opened|restored|restore_failed)'
# Calibration handoff / uncalibrated warnings:
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(calibration_resolve_failed|uncalibrated_capture|default_calibration_hint_failed)'
```

### Per-capture diagnostics — every CHECK/MEASURE/VERIFY logs its numbers

Before this, `event=correction.crossover_v2_result` carried only
`accepted`/`code` — a failed hardware run left no numbers to look at, and
only a *glitch* MEASURE capture got a partial view via
`event=program_analysis.glitch` (epsilon/residual/repeat-level only, WARN
level, glitch captures only). `CrossoverV2Conductor` now emits one
additional `log_event` per consumed capture, **on the accepted path AND
every rejection**, carrying that phase's full numeric diagnostics (pure
additive observability — none of these calls choose a verdict):

```sh
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(check|measure|verify)_diag'
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
  `delay_us`, `delay_role`, `polarity`, `predicted_ripple_db`, plus
  per-role `woofer_snr_db`/`woofer_snr_verdict`/`tweeter_snr_db`/
  `tweeter_snr_verdict`.
- `correction.crossover_v2_verify_diag` — `accepted`, `code`,
  `max_db_notch_excluded` (the number the tolerance actually gates on),
  `verify_tolerance_db`, `verify_gate_window_ms`, `measure_gate_window_ms`
  (the comparability pair behind `verify_inconclusive`), `validity_floor_hz`,
  `tracking_band_lo_hz`/`tracking_band_hi_hz`, `rms_db`.

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
    background thread right after a trusted MEASURE accept instead of from
    an HTTP handler alone). The `CaptureBeginDeferred` soft-hold mechanism
    between MEASURE and VERIFY is UNCHANGED — only the release trigger
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
- **Decide whether legacy `sound_current.yml` should update on v2
  apply.** Today it diverges cosmetically; the v2 SSOT is
  `active_speaker_baseline_profile.json`.

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
leveling, all intelligence server-side in pure functions. This mirrors
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

The default flips to `v2` on 2026-07-19. Legacy remains reachable via
`JASPER_CROSSOVER_FLOW=legacy` until W5b deletes it.

Last verified: 2026-07-23
