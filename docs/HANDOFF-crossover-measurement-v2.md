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
  review_apply → verify`. The one-liner: place the mic ~1 m in front of
  the speaker at tweeter height, tap Start, then follow the phone.
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

## Current status (2026-07-19)

Waves W1–W6 complete (PRs #1578–#1604). Hardware-validated on JTS3 +
UMIK-2: first fully-calibrated run 2026-07-19. **Legacy is deprecated**
and scheduled for deletion in W5b (see Future work). The v2 acoustic
playback binding
(`bind_program_playback_seams`) is exercised on real CamillaDSP
hardware; every orchestration test injects fakes.

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
`publish_candidate`, `apply_complete`). The web host
([`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py))
binds the real seams; tests inject fakes.

### The capture flow

One relay session (`crossover_v2:session`) spans all three captures. The
conductor hands `authorize_begin` / `on_armed` / `consume_capture` to
`run_capture_plan` (`jasper/capture_relay/session.py`):

1. **CHECK** (~25 s, one tap). Ambient silence + two band-limited pilot
   chirps per driver at two levels (−10 dB apart). Yields the ambient
   floor, the behavioral AGC/linearity verdict, channel-map sanity, and
   the **solved gain plan** for MEASURE. Replaces the legacy per-driver
   level ramps and ambient waits.
2. **MEASURE** (~20 s, auto-advances behind a cancelable countdown).
   2-channel routing: pilot pair + guard silence + **woofer sweep →
   tweeter sweep → woofer sweep repeat** (the repeat is bit-identical —
   the two form the in-capture drift estimator + glitch detector).
   Yields per-driver gated complex responses (cal applied), relative
   delay (drift/parallax-corrected), polarity, trims, per-band SNR —
   folded into a `MeasuredCrossoverCandidate`.
3. **REVIEW / APPLY** (control page, no capture). One Apply action over
   the candidate (trims, polarity, delay, predicted-vs-target sum).
   Fc/slope stay preset-owned. VERIFY is soft-held (`CaptureBeginDeferred`,
   screen `awaiting_apply`) until apply lands.
4. **VERIFY** (~15 s, auto-arms on the apply-complete host event). A mono
   summed sweep through the **applied production graph** + a pilot pair.
   Pass = notch-excluded, validity-floor-clamped tracking error ≤ ±1.5 dB.
   On fail the applied graph **stays in force** (proof-checked safe) and
   the household is offered Try again / Undo.

**One mic position for the whole session: ~1 m on the listening axis,
tweeter height, facing the speaker.** The placement screen encodes a
tolerance window (~±0.3 m distance, ±10 cm height). Only the first
capture needs a tap; CHECK auto-advances into MEASURE, and apply
auto-arms VERIFY.

## File map

| File | Responsibility |
|---|---|
| [`jasper/active_speaker/crossover_flow.py`](../jasper/active_speaker/crossover_flow.py) | The `JASPER_CROSSOVER_FLOW` selector — `active_crossover_flow()` / `resolve_crossover_flow()`. No product policy. |
| [`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py) | The conductor: `CrossoverV2Conductor`, `REASON_REGISTRY`, capture-plan builders (`build_v2_session_spec` / `build_v2_capture_plan` / `build_v2_verify_*`), `bind_program_playback_seams`, `derive_session_volume_db`, `open`/`abandon_measurement_volume`. |
| [`jasper/audio_measurement/program.py`](../jasper/audio_measurement/program.py) | Excitation-program model + composers: `ExcitationProgram`, `ProgramSegment`, `RoleBand`, `build_check_program` / `build_measure_program` / `build_verify_program`, `render_program_pcm`, `write_program_wav`, `mesm_gap_samples`. Pure data + pure composers, no safety decisions. |
| [`jasper/audio_measurement/program_analysis.py`](../jasper/audio_measurement/program_analysis.py) | The pure analysis: `analyze_program_capture` → `ProgramAnalysis`; locate/segment, drift (ε), per-driver gated TF, GCC-PHAT alignment, prediction, VERIFY tracking. All the analysis tuning constants. |
| [`jasper/active_speaker/session_volume_plan.py`](../jasper/active_speaker/session_volume_plan.py) | One fixed measurement volume per session: `session_measurement_volume_db` (the `min(−20, max(caps))` SSOT) + `SessionVolumePlan` (open/close/abandon, wall-clock ceiling, restore-once latch). |
| [`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py) | The web host: `/correction/crossover/v2/*` endpoint bindings, durable v2 state, the real analyze/publish/playback seams, `resolve_conductor_context`, `handle_v2_apply` / `handle_v2_restore`, calibration resolution, `ensure_crossover_preview_ready`, `persist_conductor_state`. |
| [`jasper/active_speaker/crossover_envelope_v2.py`](../jasper/active_speaker/crossover_envelope_v2.py) | The pure `status → envelope` renderer (schema 7): step list, screen dispatch, `REASON_REGISTRY` → template copy. |
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
   repeated sweep is **mandatory**.
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
| `volume_unresolved` | session | — | the `volume_recovery` screen |
| `verify_out_of_tolerance` / `verify_inconclusive` | VERIFY | 2 | Try again / Undo / Re-measure |

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

## Future work — the post-W6 follow-ups issue

Tracked in the post-W6 follow-ups GitHub issue (filed 2026-07-19):

- **W5b — delete the legacy flow outright:** the `crossover_envelope`
  legacy body, the `correction_crossover_flow` legacy handlers, the
  selector, and the legacy test suite. This is the big deletion, gated
  on W6's green hardware run (now met).
- Smaller nits: the `apply_blocked` session-gating detail; a
  topology-fingerprint guard on restore; a candidate-config retention
  story; Stop-control "timed out" copy; a hub HTTP-routing nit; placement
  copy improvements; the verify-fail expert disclosure.
- **Constants tuning pass** once real ~1 m runs accumulate (VERIFY pilot
  band, gate-comparability margin, confidence floor, and the PROVISIONAL
  constants above).
- **Driver-spacing input for parallax correction.** `driver_spacing_m`
  is threaded but stays `0.0` today (topology/preset carry no spacing),
  so the §3.2 parallax correction is inert. It is self-cancelling at the
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

Last verified: 2026-07-20
