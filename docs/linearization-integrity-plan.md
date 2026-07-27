# Linearization integrity: the fix ladder for the 10 dB-dark profile

**Status: fix work order (2026-07-27).** On 2026-07-27 the owner measured
the commissioned JTS3 profile against an iLoud Micro Monitor: the entire
tweeter passband sat **7–11 dB below its own midband**, cliffing exactly
at the 2 kHz crossover, and a four-filter hand correction reached
±0.9 dB (300 Hz–16 kHz). Three parallel forensics agents then allocated
the deficit to specific, confirmed mechanisms. This work order fixes
them. Durable evidence record (session artifacts, laptop-side):
`captures/iloud-comparison-20260727/` — `REPORT.md`,
`LINEARIZATION-AGENT-PROMPT.md`, and `FORENSICS-SYNTHESIS.md` (the
condensed findings this ladder executes against).

**The one-paragraph verdict.** Two confirmed model/input defects (a
mic-calibration sign flip on every vendor-fetched UMIK record, and a
shelf emitted at `slope: 6` while modeled at Butterworth Q — CamillaDSP's
Butterworth is slope 12), one confirmed structural defect (each driver is
fitted to a flat target at its own median; no stage ever compares the two
drivers' realized passband levels), one open frame defect (the measured
overlap trim reads the bare datasheet sensitivity gap even though a
−14.4 dB L-pad is physically in circuit — owner-confirmed), and an
accountability chain in which the one honest flatness instrument failed
all three bands two seconds before an unconditional auto-apply, with the
verdict reaching zero user-facing surfaces.

## PR-L1 — input integrity: mic-calibration sign convention

- `fetch_vendor_calibration` (`jasper/audio_measurement/calibration.py`)
  stores miniDSP UMIK files with `sign_convention="response"` — the
  adapter owns the vendor quirk (they are response curves; the correction
  is the negation).
- Migration for existing stored records (the live JTS3 household record
  is wrong): re-derive `correction_db` where the raw curve is retained,
  else re-fetch by serial; never silently double-negate an already-correct
  record — key the migration on the stored convention field.
- The `/correction/` upload UI's sign default and help copy; decide the
  phone-relay upload sign field (today dead code — the relay spec rejects
  the key) — either wire it through `DEFAULT_SETUP_CALIBRATION_KEYS`
  properly or remove the dead branch, not the current half-state.
- Contract test: a real-shaped UMIK fixture asserting the stored
  `correction_db` is the negated response, plus a call-site audit test
  (no production caller on the wrong default).
- Laptop-side: fix `captures/flat-linearization-20260725/s0-kit/analyze_s0.py`
  (known offender, wrong default) — kit parity, not a repo test subject.

## PR-L2 — model integrity: shelf realization

- Emit linearization shelves with an explicit `q: 0.70711` (or
  `slope: 12`) so CamillaDSP realizes exactly what
  `jasper.sound.profile._biquad_coeffs` models. Audit every shelf
  emitter, including the `/sound/` taste-EQ path, which shares the same
  evaluator.
- Correct the false "slope 6 ≡ Butterworth" claim everywhere it is
  stated: `jasper/sound/profile.py`, `jasper/active_speaker/camilla_yaml.py`,
  `jasper/active_speaker/linearization_fit.py`,
  `deploy/assets/sound-profile/js/eq-math.js`,
  `docs/HANDOFF-sound-preferences.md`.
- Parity test: compute the realized response from CamillaDSP's actual
  slope→Q formula (`Q = 1/√((A+1/A)(1/S−1)+2)`, `S = slope/12` — pinned
  by CamillaDSP's own `lowshelf_slope_vs_q` test) and assert
  model == reality across gains including −11 dB. The existing
  Python↔JS parity fixture pins two wrong models against each other;
  it gains a third leg that pins both to CamillaDSP.

## PR-L3 — close the trim-frame defect (~4–7 dB, the largest chunk)

The measured overlap trim (`solve_branch_trims` →
`trim_band_average_db.tweeter = −24.74`) sits at the *bare* datasheet
sensitivity gap (25.2 dB) although the −14.4 dB L-pad is physically in
circuit (real gap ≈ 10.8 dB), and 8–13 dB off the same analysis's own
`DriverResponse` curves. The datasheet path is correct
(`baseline_profile.py` folds the pad via `effective_sensitivity_db`)
but is overridden by the broken measured value.

- Offline replay harness against the archived 2026-07-27 MEASURE
  captures (session `d5b171fa81a5` evidence store): instrument
  `solve_branch_trims`' frame (`level_w`, `level_t`, band `lo`/`hi`)
  beside each role's `target_level_db` for the same capture; identify
  the frame term (drive-gain normalization, band clamp asymmetry,
  `_aligned_branch_tf` vs `_driver_response` reference mismatch).
- Fix the located defect; pin with a regression test built from the
  replay's numbers.
- The replay harness lives with the session artifacts; the log line and
  the fix land in the repo.

## PR-L4 — accountability: the missing assertions

From the verification-accountability audit, plus one found during
follow-up. Each lands with its test; items 1–2 are the load-bearing
pair.

1. **Inter-driver realized-level assertion** — each driver's realized
   power-band average over its own passband (not the overlap) vs the
   design intent, with tolerance. The one check that catches this whole
   class; would have fired at ~9 dB.
2. **Spec-grade the prediction before auto-apply** — `predicted_sum`
   exists at candidate-build time and carried the dark shape; run
   `evaluate_flat_spec` on it and refuse auto-apply (fail the session
   loudly, keep the speaker untouched) when the predicted post-apply
   spec is not materially better than the measured pre-apply spec.
3. **Measured-vs-datasheet trim cross-check** — the two estimates
   disagreed by ~12 dB in the same run and were compared nowhere; a
   disagreement beyond a stated tolerance refuses the trim and surfaces
   both numbers.
4. **Applied implies graded** — a session ending `applied: true` without
   a passing post-apply grade either restores the stashed
   `pre_apply_profile` or surfaces as an explicit unverified state.
5. **Forbid vacuous completeness** — `summed_validation_complete` /
   `driver_target_proof_complete` must assert non-zero evidence counts
   (amend `tests/test_active_speaker_baseline_profile.py`, which
   currently pins the contradiction as intended).
6. **Doctor**: warn on "applied profile with no post-apply grade"
   (keeps the deliberate PHASE_CLOUD_VERIFY-only failure gating).
7. **Wire `overall_passed` into user-facing copy** — the spec verdict
   gets a vote on at least one non-collapsed wizard surface.
8. **Fix the `/state` dual-view** — the stale `/sound/` staging
   candidate renders a second, contradictory baseline answer.
9. **Wild-trim guard**: pair drift-from-anchor with the realized-level
   check (its fallback points darker on this failure mode).

## PR-L5 — doctrine amendment: evidence-gated correction, delta-probe verification

Owner-ruled 2026-07-27 (recorded in FORENSICS-SYNTHESIS.md):

- **Boost is allowed and uncapped.** Null-exclusion stays as a measured
  fact (registry-gated). Headroom spend is disclosed ("this correction
  costs N dB of maximum level"), never silently limited. The protection
  layer (tweeter protection, limiters, the 0 dB ceiling) remains the
  hard rail and is not weakened.
- **"Reduce our own cuts" is a first-class operation** distinct from
  boost.
- **Both drivers' targets live in one shared level frame** (closes the
  L4-item-1 class at the fit level, not just the assertion level).
- **Delta-probe verification**: every applied correction change is
  verified as a realized-vs-commanded per-frequency map (one sweep
  before/after), classified: *matched* (keep) / *model-error* (rollback
  + flag — permanently catches the L2 bug class) / *level-dependent
  shortfall* (driver compression diagnostic) / *spatially costly*
  (cross-position spread widened — interference; service verdict routes
  placement-vs-speaker via the tau-ladder). Rollback is automatic on the
  non-matched classes.
- **Topology-agnostic fit core** (constraint on every PR in this
  ladder): measured response in, allowed vocabulary in, filters out.
  Active grants per-driver channels + alignment + pad authority; a
  passive speaker is the 1-way case on the summed chain (S4
  generalization inherits this for free). Gating bounds linearization's
  reach (~140 Hz at 7 ms); below that is the room-correction layer's
  jurisdiction on every topology.
- Future (design recorded, not built here): the stereo pair flow —
  tune A, apply to B as prior, short delta session (express-tier
  shape), pair-match report band-by-band; large or null-shaped deviation
  is a diagnostic, not a bigger correction.

## Recommission acceptance

After L1–L4 land and deploy: a fresh commissioning session on JTS3 whose
applied result (a) passes the new gates, (b) produces a *matched*
delta-probe map, and (c) lands within ~1 dB of the hand-fix ground truth
(`captures/iloud-comparison-20260727/`, reproducible via
`kit/payoff_eq.py`) without hand tuning. The owner listens; the owner's
ear is the final gauge.

## Process and sequencing

- Standard per-PR gate: implementation + independent adversarial review
  to 0 blockers / 0 should-fixes; serial local lanes; corpus tests
  PASSED (not SKIPPED) with both `JTS_FLAT_LIN_CORPUS` and
  `JTS_FLAT_LIN_S0` roots where reachable.
- L1, L2, L3 are independent and start immediately (L3 is
  offline-forensics-first). L4 touches
  `jasper/active_speaker/crossover_v2_flow.py` and therefore lands
  **after** the flow-simplification PR-U1 (#1771) merges. L5 is
  design-heavy and follows L4.
- The express/UX ladder (`docs/flat-linearization-flow-simplification-plan.md`)
  continues in parallel; several L4 disclosure surfaces land on the
  screens that ladder is redesigning — coordinate copy, don't duplicate.
- This work order was reviewed by the architect against the three
  forensics reports rather than a fresh adversarial round (deviation
  from the standard gate, stated openly): its content is settled
  measured fact plus owner rulings from the same evening, and every
  implementation PR below it still gets the full gate.

Last verified: 2026-07-27
