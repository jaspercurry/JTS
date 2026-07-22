# Crossover measurement — reproducibility working plan

> **Status: ACTIVE — selector implemented (2026-07-22); pending adversarial review + JTS3 hardware confirmation.**
> This is the execution and decision reference for the "MEASURE is not
> reproducible → VERIFY fails" blocker on the v2 conductor flow. T2-core
> merged via PR #1647, its post-merge UMIK-2 repeat failed (bound-pinned
> 299.948 µs, VERIFY 5.264–6.454 dB max), and the follow-up overlay
> diagnosis + prior-art work located the architecture-level cause: the
> narrowband flatness objective was allowed to *select* the delay, and its
> basin ordering is capture-noise-dependent. Decision (see §10, 2026-07-22
> methodology entry): the drift-corrected physical peak-gap anchor owns lobe
> selection and the primary value; any fine refinement is bounded within the
> anchor's lobe and chosen by offline bake-off; flatness is demoted to
> evidence; railed/disagreeing selections route to guidance. This is a
> *targeted refactor of the measurement core*, not a rewrite of the conductor
> architecture.
>
> Canonical operational truth: [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md).
> v2 decision record: [crossover-measurement-productization-design.md](crossover-measurement-productization-design.md).
> Keep those two authoritative; this doc is the plan + decision log until the
> work lands, after which the durable outcomes fold into the HANDOFF and this
> doc is archived.
>
> **Last updated:** 2026-07-22. **Owner:** Fable (architect/coordinator).

---

## Revised plan (2026-07-22, post-hardware diagnosis)

> **Supersedes §1–§5.** Written after the protected JTS3 delay sweep and two
> fresh CHECK → MEASURE → APPLY → VERIFY flows. The older Tier 1/2/3
> framing remains decision archaeology — do not implement against it. This
> section is the current plan; §10 carries the exact evidence and §11 the gate
> state.

> **Post-merge repeat update (2026-07-22):** the corrected implementation did
> not reproduce its earlier green hardware result. With stored UMIK-2
> calibration, CHECK passed and capture integrity was clean, but MEASURE chose
> a signed −299.948 µs correction at its search bound and applied 0.2999 ms to
> the woofer. Three VERIFY captures failed at 5.264, 6.453, and 6.454 dB max.
> The flow then restored its exact pre-test volume and the sanctioned Undo
> restored the earlier 0.0537 ms profile. This supersedes the sequencing claim
> below that only a repeat remained: the upstream objective must first be made
> to track the hardware VERIFY curve. Do not resume T2-robust or Fix 4 yet.

### The refined diagnosis

The original offline τ-sweep found the root cause of "confidence never
passes" (E0, §10): a **correlation-band bug, not an estimator-quality
problem**. GCC
delay/confidence correlate over `[Fc/2, 2·Fc]` = `[1000, 4000]` Hz, but the
tweeter is only excited from 2000 Hz (its MEASURE sweep starts at `Fc`), so
`[1000, 2000]` is tweeter-deconvolution noise, not signal. Clamping the
correlation to the true driver-sweep overlap `[2000, 4000]` Hz takes
confidence from **0/12 → 12/12** on the corpus. This is cause (a) from §1,
but aimed at the driver-*excitation* band, not the reflection floor — which
is why T1.1 as originally scoped (§3) was a mathematical no-op (E0 confirmed
every branch's `validity_floor_hz` ≤ 302 Hz on this room, so the floor clamp
never binds).

The later hardware sweep located three upstream defects that offline flatness
alone could not settle:
- **Wrong comb-lobe prior.** GCC-PHAT's periodic peak selected a neighboring
  basin near 336 µs while hardware VERIFY bottomed at 40–50 µs. The raw IR
  peak gap was 208.333 µs, but 178.150 µs of that was inter-sweep clock
  drift. Removing only that clock term leaves the physical gap that anchors
  the correct 55 µs objective basin; discarding the whole gap or leaving the
  drift in both point elsewhere.
- **A candidate-specific prediction could explain its own mistake.** VERIFY
  must compare the applied response with the fixed independently aligned
  zero-residual target, not phase the target by the delay under test.
- **One-sided smoothing created a false hardware failure.** The production
  comparator smoothed the capture but not the prediction. At the retained
  50 µs hardware point this reported 1.991 dB max; smooth↔smooth is 0.490 dB
  and raw↔raw is 0.606 dB.

**Key limit — offline analysis cannot settle VERIFY-pass.** VERIFY
re-measures through the applied LR4 graph, and the raw MEASURE captures in
the corpus don't contain that re-measurement. One on-hardware VERIFY at the
fixed delay is the decisive test; offline work can only get the inputs to
that test right.

### The revised fix set (supersedes Tier 1/2, §3–§4)

1. **Clamp the three analysis bands to the driver-sweep overlap** —
   alignment correlation, ripple/prediction, and VERIFY tracking all read
   `[max(Fc/2, tweeter_sweep_lo), min(2·Fc, woofer_sweep_hi)]` from one
   declaration-driven SSOT helper (same DRY shape T1.1 aimed for, corrected
   band + source). **Offline-proven: 12/12 confidence** on the E0 corpus.
2. **Select inside the physical comb lobe.** Remove the measured inter-sweep
   clock contribution from the raw peak gap, retain the physical remainder,
   add declared parallax, and use that non-periodic seed to orient one
   declaration-bounded ±half-period lobe. GCC remains polarity,
   capture-confidence, and fallback evidence only.
3. **Keep VERIFY honest.** Compare against the fixed independently aligned
   target, smooth measured and predicted curves identically, and use the raw
   prediction only to identify a genuine modeled-notch interior.
4. **Retain the physical-plausibility bound and flatness evidence.** The
   candidate records GCC seed ripple, selected ripple improvement, and bound
   state; selection and apply use the same τ.
5. **Pause T2-robust and Fix 4.** Coherence weighting and a wider overlap are
   robustness layers, not fixes for an objective whose basin is wrong. Resume
   them only after the base selector tracks the protected hardware delay curve
   and clears another controlled repeat. The pre-merge independent 0/0 review
   remains code-quality evidence, not a substitute for that hardware gate.

### Owner decisions (2026-07-21)

- **Conservative widening only** — declared limits, never push the woofer
  past its declared top (breakup risk).
- **Skip driver-spacing collection.** E0 showed geometry is not the
  dominant term: the parallax model predicted the P1→P2 delay would fall
  ~90 µs; it rose ~58 µs instead. The declared delay range already bounds
  plausibility without a measured driver spacing.
- **The min-safe crossover is the value to always use.**
- **Keep everything dynamic / declaration-driven** for any future drivers —
  no hardcoded bands or spacings.
- **Sequencing:** corrected T2-core's independent review cleared 0 blockers /
  0 should-fixes, but the post-merge UMIK-2 repeat failed. Diagnose
  why the selector moved from the hardware-green 53.669 µs result to a bound-
  pinned 299.948 µs result before changing the robustness layers. Keep
  T2-robust and Fix 4 paused until the selector tracks the hardware VERIFY
  curve on fresh captures.

### Prior-art note

The methods are all standard: GCC-PHAT TDOA, sum-flatness (Open Sound
Meter's method), coherence-weighting (Knapp–Carter), geometry/min-crossover
priors (universal practice) — see §12 for the borrow table. The shipped flow
had *deviated* from standard practice (narrow tweeter excitation with a
correlation band extending below it); this fix set brings it back in line,
not a novel design. No open-source library code has been imported yet
(concepts only, per §12's borrow-mode table); a clean-room coherence-weighted
port stays reserved for if the widened overlap proves insufficient.

### Sequencing

The corrected T2-core merged to `main` via PR #1647, passes retained replay and
an earlier calibrated JTS3 VERIFY, and cleared its pre-merge T2-specific
independent review at 0 blockers / 0 should-fixes. Its required post-merge
UMIK-2 repeat then failed three VERIFY captures. T2-robust and Fix 4 remain
paused until the base selector tracks the protected hardware delay curve on
fresh captures. The older sequencing below is retained as decision archaeology.

---

## 0. Why this doc exists

The v2 flow is shipped and correct in architecture, but the driver-delay
measurement is **not reproducible in a real room**: two runs of the same setup
disagreed ~2× on measured delay and only marginally cleared the confidence
floor, so the flow auto-applies a marginal candidate and then can't VERIFY it.
This has been worked for several weeks. The purpose of this doc is to land it
**this session or the next, without scope ballooning**, by (a) fixing the
cheap, high-confidence root causes first, (b) *measuring on hardware* whether
that's enough, and (c) escalating to the estimator rewrite **only with
evidence and a clear quality benefit**. It is written to be resumable across
sessions: see §9 (decision log) and §10 (current status / next action).

The diagnosis phase is done. What remains is largely **empirical** — and the
first measurement is nearly free.

---

## 1. The blocker, at the code level

Run-to-run delay disagreement → marginal auto-apply → VERIFY fails. Four
coupled causes, all located:

- **(a) SSOT/DRY break — the validity-floor clamp is applied to two of three
  consumers.** The "trustworthy band = `[max(Fc/2, branch_floor), 2·Fc]`" fact
  is applied in the trim solve (`program_analysis._build_candidate`) and in
  VERIFY tracking (`_analyze_verify`), but **NOT** in the delay estimator
  (`_estimate_alignment`). At a short-gate placement the GCC-PHAT band feeds
  its own noise-dominated, reflection-corrupted sub-floor bins into the exact
  number the trust floor gates on. Same *class* as gotchas #6/#16/#19 (the
  RMS-vs-peak estimator sweep); this is the un-swept sibling for the
  floor-clamp invariant.
- **(b) The confidence doesn't predict VERIFY.** `alignment.confidence` is the
  GCC-PHAT peak-margin `(primary−secondary)/primary` — how *unambiguous* the
  correlation peak is, not how *accurate* the delay is nor whether the sum will
  flatten. A sharp-but-wrong peak clears the 0.6 trust floor.
- **(c) Two derivations of "the delay."** `_build_candidate` predicts the sum by
  referencing each branch to its own IR argmax peak (assumes *perfect*
  alignment), but the delay actually applied to DSP is the sub-sample GCC-PHAT
  value. Those two numbers aren't guaranteed equal, so VERIFY can fail even when
  the measurement looks "good" — the prediction is not a check on the applied
  delay.
- **(d) Placement.** The flow measures at **tweeter height** with the parallax
  correction **inert** (`driver_spacing_m=0.0`, `correction_crossover_v2.py`) —
  the exact reading miniDSP's driver-alignment note warns is incorrect, and the
  *most* placement-sensitive geometry (parallax ∝ 1/r, steepest on the tweeter
  axis). Real-room placement wobble → delay wobble.

Tier 1 (§3) fixes (a) and (d). Tier 2 (§4) fixes (b) and (c) and adds
low-SNR robustness.

---

## 2. Definition of done (the stop rule)

**We stop and ship the moment this holds; everything not required to hit it is
backlog.**

> On JTS3 + a real measurement mic, at a **good placement**, the flow completes
> **5 consecutive full runs** (CHECK→MEASURE→apply→VERIFY) that each:
> 1. **VERIFY passes with margin** — notch-excluded max tracking error ≤ **1.2 dB**
>    (inside the 1.5 dB gate), and
> 2. **the measured driver delay is reproducible within ±1 sample (±20.8 µs)**
>    across the 5 runs, and
> 3. **no glitch/retry storm** — median captures-per-phase ≤ 1.2.

**"Good placement"** is defined measurably (not by vibes): mic on a stand at
driver-midpoint height, ~1 m on-axis, ≥ ~1 m clear of the nearest large
surface, yielding a MEASURE gate window ≥ ~3 ms (validity floor ≤ ~350 Hz —
read from `crossover_v2_measure_diag.gate_window_ms` / `validity_floor_hz`).

If the estimator rewrite (Tier 2) lands, we additionally require the **confidence
metric to be predictive**: across the corpus, runs the flow auto-applied should
VERIFY-pass and runs it routed to guidance should have VERIFY-failed — i.e. the
gate stops gambling.

---

## 3. Tier 1 — small, safe fixes (do now)

> **Superseded 2026-07-21.** §3–§5 (this Tier 1/2 framing and its
> rationale) are decision archaeology from before the offline τ-sweep
> feasibility experiment. E0 (§10) showed T1.1's floor-clamp was a
> mathematical no-op on this room's data and T1.2's midway placement never
> moved confidence — the real fix is a driver-sweep-overlap correlation-band
> correction, not the reflection-floor clamp these sections describe. Kept
> for history; do not implement against this framing. Current plan:
> "Revised plan (2026-07-21, post-feasibility)" near the top of this doc.

Both are small, offline-provable on retained clips, and directly attack the two
most likely root causes. Neither is risky.

### T1.1 — Unify the trustworthy band (cause a)
- **What:** one helper computes `[max(Fc/2, branch_floor), 2·Fc]`; the delay
  estimator, the trim solve, and VERIFY tracking all consume it.
- **Quality bar:** **SSOT + DRY** — one owner of "which bins are trustworthy,"
  three consumers; sweeps the last un-clamped sibling.
- **Size:** small (~30–50 lines + tests) in `jasper/audio_measurement/program_analysis.py`.
- **Builder:** Sonnet-5-high.
- **Success (offline, on the corpus):** at short-gate placements the delay
  spread narrows *or* confidence honestly drops to guidance instead of passing
  marginally.

### T1.2 — Midway-between-drivers placement (cause d)
- **What:** move the placement target from tweeter height to the vertical
  midpoint between the drivers, where geometric parallax is **zero by symmetry
  and stationary w.r.t. height error**. **Delete** `parallax_us()` and the
  `driver_spacing_m` threading (currently inert; retires a Future-work item by
  deletion). Placement-screen copy/picture updated.
- **Quality bar:** **80/20 + simplicity + SSOT** — don't *correct* a
  placement-sensitive term, place where it doesn't exist; removes an inert
  config path.
- **Size:** small code; **the change to a shipped default must be
  hardware-validated first** (E0, §7).
- **Builder:** Sonnet-5-high.
- **Success:** midway measurements verify flat at 1 m *and* have tighter
  run-to-run delay spread than tweeter-height (E0 decides; if midway does NOT
  win, keep tweeter-axis and *activate* the parallax correction instead —
  `driver_spacing_m` from topology).

**After Tier 1: re-measure on JTS3 and check §2. If met → DONE.** If not, the
measurement data justifies Tier 2 with specifics.

---

## 4. Tier 2 — the estimator (the committed quality target)

This is the largest piece and the main ballooning risk, so we keep discipline
two ways: **(1) Tier 1 ships first** (fast win + clean data to build the
estimator against), and **(2) the §2 stop rule still governs when we're
*done*.** But the estimator itself — **B1+B2+B3, done right** — is the committed
quality target (owner call, 2026-07-21), not a build/no-build gamble; it's built
in two layers with a clear 80/20 line so effort tracks benefit. Rationale and
benefits: §5.

### T2-core — one consistent τ + honest quality evidence (causes b, c)
- **What:** coarse latency-immune cross-correlation (period disambiguation,
  polarity, capture-confidence seed) → **declaration-bounded summed-flatness
  refine** over the single capture's complex branch responses, reusing
  `_predicted_sum` / `_ripple_db`. The active region's `delay_range_ms`
  constrains the magnitude; the drift-corrected physical peak gap plus
  parallax supplies the signed center of one ±half-period comb lobe without
  requiring a pre-existing `delay_target_driver`. GCC supplies polarity,
  capture confidence, and fallback only. **One τ\*** is selected and applied;
  VERIFY compares the result with the zero-residual aligned target that τ\*
  is supposed to realize, rather than letting a wrong candidate predict its
  own wrong-lobe response. The implemented T2-core scope deliberately
  keeps confidence as the labelled GCC seed/capture confidence (fix 1 already
  made that gate reliable); seed ripple, objective improvement, selection
  delta, and boundary state are separate diagnostics. A predictive sharpness
  or σ_τ confidence remains T2-robust work rather than being implied by the
  GCC number.
- **Benefits:** B2 (selector/apply/target consistency) plus the evidence needed
  to evaluate B1; T2-core does not claim that its GCC seed-confidence predicts
  VERIFY.
- **Quality bar:** **SoC** (one declaration-bounded delay selector) +
  **observability** (seed, selected delay, ripple improvement, and bound state
  in the diagnostic evidence/events).
- **Size:** medium; reuses existing machinery. **Builder:** Opus-high (tricky SP).

### T2-robust — coherence-weighted phase-slope + CRB σ_τ (best practice, IN SCOPE)
- **What:** replace/augment the correlation core with a **coherence-weighted
  phase-slope regression** of the inter-driver cross-spectrum (`arg{W·T*}=−ωτ`),
  yielding τ\* and a rigorous **σ_τ** (the coherence Cramér-Rao bound). PHAT is
  documented to fail at low SNR by up-weighting noisy bins; coherence weighting
  is the low-SNR-correct choice (Knapp-Carter/Piersol/Quazi; corroborated by
  OSM's + HouseCurve's coherence gating).
- **Benefit:** B3 (robustness at modest SNR / imperfect placement — the
  ship-to-real-people bar). See §5.
- **Status (owner call, 2026-07-21): IN SCOPE, not data-gated.** Coherence
  weighting is documented best practice, and the σ_τ it produces *is* the
  principled confidence (it supersedes any future T2-core sharpness heuristic), so we
  build it as the target estimator. The corpus (U3) *validates* it delivers the
  expected robustness — it does not decide whether to build it.
- **Size:** medium-large. **Builder:** Opus-high.

Prior-art & FTO note: build the estimator **clean-room from the math** (Open
Sound Meter is GPL — method reference only). The one adjacent patent
(US 11,832,067, Intel mic-localization) differs on every element; the reverse-null
/ sum-flatness approach is decades-old prior art and is the design *furthest*
from its group-delay-from-phase claim. Light qualified-counsel look before merge.

---

## 5. What the estimator rewrite actually buys us (the honest case)

This is the decision Tier 2 hinges on. Tier 1 makes the *measurement*
reproducible **at a good placement**; Tier 2 makes the *decision* trustworthy
and the measurement robust **everywhere else**. Three benefits, ranked:

- **B1 — a confidence that predicts VERIFY.** *Biggest remaining quality
  benefit, and
  largely independent of placement.* Today the auto-apply gate uses the GCC peak
  margin, which doesn't predict whether applying the candidate will flatten the
  sum. That forces a lose-lose: set the floor loose → auto-apply things that
  then fail VERIFY (the current complaint); set it tight → reject good
  measurements and frustrate re-measures. A confidence that **is** the
  sum-flatness objective (a future sharpness metric, or T2-robust's σ_τ) turns
  auto-apply into "apply when it will verify, guide when it won't" — which is
  exactly what every shipping calibrator does and what the product needs. This
  benefit stands **even if Tier 1 makes good-placement measurements
  reproducible**, because it's about decision quality, not delay accuracy.
  T2-core intentionally does not claim this benefit yet: it labels the
  existing number `gcc_phat_seed` and retains flatness-quality evidence
  separately.
- **B2 — selector/apply/target consistency.** *Correctness fix that bites even
  at good placements.* Cause (c) is structural: a GCC delay could be applied
  while VERIFY checked an unrelated peak-aligned model. T2-core derives one
  τ from the flatness objective and applies it; VERIFY checks the fixed aligned
  target that this τ is supposed to realize. A candidate-specific prediction
  is forbidden because it could explain away a wrong-lobe apply. Small
  conceptually, but it requires the T2-core objective to exist.
- **B3 — robustness at modest SNR / imperfect placement.** *The
  ship-to-real-people bar.* Coherence weighting down-weights the low-SNR band
  edges (woofer rolling off high, tweeter low) that PHAT wrongly up-weights. It's
  the difference between "reproducible for a careful user in a quiet room with a
  good mic" and "reproducible for real households." Marginal add on top of
  B1+B2; **data-gated** (U3).

**Decision (owner call, 2026-07-21): build B1 + B2 + B3.** B1/B2 are structural
correctness + decision-honesty wins that likely bite even at good placements.
B3 is included too — coherence weighting is documented best practice (the
low-SNR-correct estimator, and the source of a principled σ_τ confidence), not a
speculative add, so we do it right rather than ship the sharpness heuristic and
revisit. Discipline is preserved by *sequence, not omission*: **Tier 1 ships
first** (fast win + clean data to build against), the **§2 stop rule still
governs when we stop**, and the corpus *validates* each layer delivers
(E-replay), rather than justifying skipping it. "Do it right" and "80/20" are
both honored — the 80/20 lives in reusing existing machinery for the core and in
letting the corpus tune the coherence weighting, not in dropping a best-practice
layer.

---

## 6. Unknowns, and how we resolve each

| # | Unknown | How we resolve it | Cost |
|---|---|---|---|
| U1 | Is Tier 1 alone enough to hit §2? | E0: after T1.1 + T1.2, run the 5× stop-criterion measurement on JTS3. | ~1 h |
| U2 | Does midway placement verify flat at 1 m *and* beat tweeter-height on reproducibility? | E0-placement: measure the same setup at tweeter-height / midway / bad-desk; apply each; VERIFY at 1 m. **No code needed** — a physical mic move. | ~45 min |
| U3 | Does B3's coherence weighting deliver the expected low-SNR robustness (validation, not a build-gate)? | Offline replay of T2-core vs T2-robust on the retained corpus; confirm tighter delay spread at low SNR and σ_τ that tracks actual VERIFY. | offline |
| U4 | Reuse `null_walk`'s selection logic vs a small purpose-built refiner? | Read its selection API at T2-core design; reuse only if the abstraction genuinely fits (don't force DRY). | ~30 min |
| U5 | Does the T2 objective need per-bin coherence we don't compute today? | We have per-band SNR vs the ambient floor; evaluate SNR-derived weighting as the coherence proxy from a single ESS capture. | design |

The whole estimator redesign is **provable offline on retained clips** before
production code (the analysis is a pure function of `(program, WAV)`) — that is
the single biggest lever against wasted hardware runs.

---

## 7. Experiments (cheap; JTS3 + retained clips)

- **E0 — the placement + baseline experiment (FIRST, nearly free).** Deploy
  current `main` (diagnostics + capture retention already in `856903ca1`),
  enable the retention marker, and capture repeat runs at (good tweeter-height,
  good midway, bad-desk). Resolves U1 partially and U2 fully; produces the corpus
  everything else replays against. **No product code.**
- **E-replay — offline estimator evaluation.** Re-run analysis variants
  (T1.1 floor-clamp; T2-core; T2-robust) over the corpus; compare delay spread,
  confidence-vs-VERIFY, gate windows. Resolves U3. **No hardware runs.**
- **E-confirm — hardware confirmation** after each tier merges: the 5× stop
  criterion at a good placement.

---

## 8. Milestones / JTS3 checkpoints & orchestration

- **M0 (JTS3 #1):** deployed + corpus captured + placement question answered (E0).
- **M1 (JTS3 #2):** Tier 1 merged; re-measure vs §2. **GO/NO-GO on Tier 2 here,
  on the data.**
- **M2 (JTS3 #3):** T2-core merged (if triggered); confidence predicts VERIFY.
- **M3 (JTS3 #4):** T2-robust merged (coherence weighting + σ_τ); U3 validates it delivers.
- **Ship:** §2 met → fold outcomes into the HANDOFF; archive this doc.

**Orchestration:** Opus-high for the T2 SP core; Sonnet-5-high elsewhere;
**independent Opus adversarial review at 0 blockers / 0 should-fixes** with the
canonical prompt; hardware-validate on JTS3 before each merge; small
independently-mergeable PRs, rebased on a fast-moving `main`.

---

## 9. Tier 3 — explicitly deferred (NOT this session → GitHub issues)

Captured so they're off the table for the landing work:
- **Fail-fast placement at CHECK** — estimate the reflection-free window at the
  25 s CHECK and guide placement before spending a MEASURE→apply→VERIFY cycle.
  (Nice UX; not required to land the blocker.)
- **Capture integrity** — stamp the WAV from the actual `AudioContext.sampleRate`
  (refuse on mismatch) + an AudioWorklet dropout counter reported to the Pi.
  (Wild-robustness for secondary failures; not the core blocker.)
- **Constants tuning** — re-derive PROVISIONAL constants from the accumulated
  corpus (folds issue #1605).

---

## 10. Decision log (append; newest first)

- *2026-07-22 (fine-stage bake-off result: anchor + gated-GCC local-peak snap
  wins; phase-slope and broadband-xcorr ruled out with recorded negative
  results)* — The offline bake-off (artifacts:
  `captures/xover-e0-2026-07-21/bakeoff-20260722/`, 16 validated captures ×
  5 candidates, harness reproduced every production/sidecar reference value
  to ≤0.04 µs before scoring) evaluated fine-stage candidates on the two
  hardware-anchored captures plus every repeat pair in the retained corpus.

  **Winner: anchor + nearest-GCC-local-peak snap.** Compute the
  drift-corrected physical peak-gap anchor as today; then snap to the
  nearest *local maximum* of the existing upsampled GCC-PHAT correlation
  within a radius of the anchor; fall back to the bare anchor when no local
  maximum exists inside the radius. Evidence: on the hardware pair it lands
  in the pass valley both runs (applied woofer delay 33.5 / 31.7 µs,
  interpolated aligned-VERIFY ≈ 2.17/2.22 dB raw-metric — comparable to the
  anchor's own accuracy) with **1.77 µs** run-to-run spread; across all 8
  repeat pairs its spread is **median 3.5 / max 7.2 µs — 0/8 pairs exceed
  the ±20.8 µs stop-rule budget**, vs 4/8 for the integer anchor (max
  44.7 µs — a genuine tweeter-argmax ±1–2-sample instability on
  back-to-back mic-untouched attempts, confirmed in the raw data) and 3/8
  for sub-sample envelope refinement (refining an unstable peak stays
  unstable). The snap *heals* anchor jitter: the 44.7 µs anchor-jump pair
  converged to 6.9 µs; max observed snap distance anywhere in the corpus is
  39.1 µs. **Snap radius: period/6 at Fc (≈83 µs at 2 kHz)** — the same λ/6
  as the GPS lobe-selection budget, 2× the max observed legitimate snap,
  and structurally below the +166 µs stable-but-wrong correlation feature
  (see below). Polarity/confidence machinery unchanged (existing GCC
  capture confidence still gates at the 0.6 floor). With the snap bounded
  closed-form and an anchor fallback, nothing can rail — the
  railed-value-auto-applied hole closes structurally, with no new conductor
  gate needed.

  **Recorded negative results (do not re-litigate):**
  - *Gated-GCC "window max" reading:* the tallest correlation peak within
    ±half-period of the anchor is a stable wrong answer on this hardware —
    applied 193/192 µs (1.5 µs "precision"), 7.4 dB on the hardware curve.
    Within-window *maximum* ≠ nearest local peak; only the latter is safe.
  - *Broadband IR cross-correlation (b2):* cross-correlating two
    different-band drivers' IRs is NOT a broadband arrival comparator — the
    correlation is dominated by the shared overlap band and degenerates to
    the same comb (selected ~285–319 µs, 4.4–5.0 dB on hardware). The REW
    "cross-corr align" analogy only holds for same-source measurements.
  - *Anchor + coherence-weighted phase-slope (d):* railed on **16/16**
    captures with a tightly clustered systematic residual (+388 ± 38 µs,
    same sign, placement-independent; synthetic self-test recovers known
    residuals to 0.07 µs, window tapers move the real-data result <0.3 µs).
    The as-crossed branches' relative phase over the overlap band is
    dominated by the drivers'/filters' differential dispersion, not by
    arrival misalignment — a cross-spectrum phase slope cannot recover the
    VERIFY-optimal delay here. This further re-scopes T2-robust: its
    phase-slope core is not viable on as-crossed branches; any future σ_τ
    confidence layer needs a different base quantity.
  - *Corpus caveat:* under the current (fix-1) correlation band, the E0
    captures score 0.62–0.78 GCC confidence — above the 0.6 floor — so the
    confidence gate would NOT have filtered the captures whose anchor
    jumped; the snap is what carries the reproducibility clause.

- *2026-07-22 (methodology decision: physical anchor primary; flatness demoted
  from selector to evidence)* — The owner-directed diagnosis of the failed
  controlled repeat is complete, and it changes the estimator's architecture,
  not its tuning. Three independent evidence streams converged:

  **(1) The offline overlay** (artifacts:
  `captures/xover-e0-2026-07-21/overlay-20260722/`) replayed both same-morning
  captures through the exact production objective across ±700 µs, validated
  against production's own numbers (every diag value reproduced exactly,
  program reconstruction byte-validated). Findings: the failing capture's
  flatness landscape **genuinely prefers the wrong comb basin** (ripple
  7.15–7.29 dB around −300…−318 µs vs 7.96–8.99 dB at the hardware-correct
  −40…−50 µs) — an objective problem, not a search bug. A 2×2 code-delta
  matrix (pre-merge `81f06e1b5` vs merged `bdc893d22a`, both captures) shows
  **the capture picks the basin, never the code** — capture-dependent
  bistability, no regression from the post-review commits. The two captures'
  landscapes differ only in second-order structure (same notch, same
  SNR-safe regime); the drift-corrected physical peak gap moved 21.7 µs
  (28.281 → 49.948 µs ≈ one argmax sample) between runs. Critically, a
  windowed counterfactual on the failing capture shows **no window size saves
  the flatness metric**: at ±80 µs it picks −6 µs (the far shoulder of the
  hardware valley, 44 µs from the anchor) because the metric's fine structure
  disagrees with hardware *inside* the correct basin too; at ±250 µs it rails
  to the wrong basin (what production did). Meanwhile the bare physical
  anchor pointed at woofer delay 28.3 µs (run A) and 49.9 µs (run B) — both
  inside the measured hardware pass valley (20–70 µs; optimum 40–50 at
  1.94–1.99 dB on the honest aligned metric).

  **(2) Prior art** (two reports:
  `captures/xover-e0-2026-07-21/prior-art-20260722/REPORT.md` and
  `DEEP-RESEARCH-REPORT.md`, the latter owner-run deep research). The field
  is unanimous: every shipping/respected tool (REW, Acourate, Audiolense,
  Trinnov, Smaart practice, miniDSP, HouseCurve, van Veen/McCarthy live-sound
  method) anchors driver delay on a **broadband, non-periodic quantity**
  (per-driver IR arrival / leading edge / geometry) and uses narrowband
  phase/correlation/flatness only as a **bounded, anchored, validated fine
  step**. No tool ships an unconstrained narrowband search as the decider.
  REW's own history added an allowed-delay-range bound + cursor-frequency
  weighting to its alignment tool after wrong-lobe problems. Ianniello 1982
  formalizes it: gated (window-constrained) correlators tolerate lower SNR
  than ungated before anomalous (wrong-lobe) estimates explode.

  **(3) The sizing math** (GPS integer-ambiguity resolution, Teunissen; see
  DEEP-RESEARCH-REPORT §Q4): with comb period λ ≈ 500 µs at Fc=2 kHz, a
  coarse anchor with error σ selects the correct lobe with ≥99.7% probability
  when σ ≤ λ/6 ≈ 83 µs. Our anchor's observed run-to-run delta is 21.7 µs
  (≈1 argmax sample; σ ≈ 15 µs, n=2) — inside the budget with ~5× margin,
  even against the bias-tightened λ/8 variant. The anchor is strong enough to
  own lobe selection outright; no narrowband objective is needed for that
  role, and per the overlay none can be trusted with it.

  **Decision (architect, Fable): hybrid (c), restructured so each stage does
  the one job it is provably good at.**
  - **Lobe selection + primary value = the drift-corrected physical peak-gap
    anchor** (+ declared parallax), as today, upgraded from integer-sample
    argmax to a **sub-sample arrival estimate** (the argmax quantum of
    20.8 µs is currently the anchor's dominant noise term; pyfar-style
    sub-sample IR-delay estimation is the standard cheap fix, plan §12).
  - **Fine stage: a bounded refinement within the anchor's lobe, chosen by
    offline bake-off** on the retained corpus among: (i) no refine
    (sub-sample anchor alone), (ii) gated GCC — the correlation peak nearest
    the anchor (Ianniello's gated mode; GCC's within-lobe precision measured
    at σ ≈ 3.8 µs on the E0 corpus), (iii) coherence-weighted phase-slope
    regression within the lobe (the T2-robust core, re-scoped to this role).
    Selection criteria: distance from the hardware optimum on the two
    hardware-anchored captures, within-placement reproducibility ≤ ±1 sample
    across the corpus's repeat pairs, and honest failure behavior. The
    ±1-sample stop-rule clause is why a fine stage exists at all: the
    integer-argmax anchor alone has σ ≈ 15 µs and would brush against it.
  - **Summed-magnitude flatness is demoted from selector to evidence** — it
    remains as diagnostics and (optionally) a cross-lobe validator, but it
    never chooses the applied delay. Both hardware runs are explained by
    this role assignment: run A passed because flatness happened to agree
    with the anchor; run B failed because flatness was allowed to overrule
    it.
  - **Gating: a railed fine stage or an anchor↔fine disagreement beyond the
    window budget routes to `low_alignment_confidence` guidance (re-measure),
    never auto-apply.** `flatness_at_bound` was diagnostics-only when run B
    railed — a decision-honesty hole this closes at the conductor.
  - **VERIFY is unchanged** (fixed independently-aligned target, identical
    smoothing) — it caught the failure honestly and is the safety net for
    the residual risk.
  - **Fix 4 (widen tweeter sweep) is no longer load-bearing for lobe
    safety** — re-evaluate later purely as SNR/robustness hardening, only if
    the corpus shows the fine stage needs bandwidth. **T2-robust** is
    reshaped, not resurrected wholesale: its phase-slope core is fine-stage
    candidate (iii) above; its σ_τ predictive-confidence layer stays paused
    until the base selector passes the stop rule.
  - Noted for later, not this increment: the active preset resolved on JTS3
    is `preview-default-2way` with a generic `delay_range_ms=[0,1]`, not a
    speaker-specific declaration — the declared-range rail is doing less
    work than intended on this bench.

- *2026-07-22 (post-merge controlled repeat failed; prior sound restored)* —
  On merged T2-core plus the review-only status/contract-text corrections, a
  fresh headless CHECK → MEASURE → automatic APPLY → VERIFY run used the
  stored UMIK-2 calibration (`minidsp-minidsp_umik2-b7343c0c625b`). CHECK
  passed with 48.35 dB tweeter and 24.95 dB woofer pilot SNR; both 10 dB
  linearity steps and channel-map checks passed. MEASURE capture integrity was
  clean: **30.656 ppm** clock drift, **0.02 sample** maximum residual,
  **0.001 dB** repeat-level delta, and no detected glitch. Nevertheless the
  selector refined GCC **−352.220 µs** to signed **−299.948 µs**, reported only
  **0.4451 dB** objective improvement, and landed **at the flatness bound**.
  The live Camilla graph read back the matching **0.2999 ms woofer delay**.

  VERIFY then failed three same-session captures at **5.264, 6.453,
  and 6.454 dB max** (RMS **1.805, 2.282, and 2.270 dB**) against the 1.5 dB
  gate. MEASURE and VERIFY used matching **7.0 ms** gates, **142.857 Hz**
  validity floors, and the declared **2–4 kHz** tracking band. No input
  overflow, xrun, clipping, service failure, or safety-unit failure was
  observed. The session restored listening volume exactly to **−15.151515
  dB**; the v2 Undo transaction then restored the prior protected profile and
  live **0.0537 ms** woofer delay. This repeat fails the T2-core completion
  gate and reopens the upstream objective-vs-hardware diagnosis. T2-robust and
  Fix 4 remain paused.

- *2026-07-22 (upstream diagnosis + corrected JTS3 VERIFY pass)* — Per the
  owner-directed pause, T2-robust and Fix 4 were not started. A clean protected
  hardware sweep first applied woofer delays from **0–1100 µs** in 50 µs
  steps, then refined **0–100 µs** in 10 µs steps. The honest fixed aligned-τ
  VERIFY curve bottoms at the Camilla sample-quantized **40–50 µs** pair. The
  production comparator initially reported **1.991 dB max** there, but that was
  not physical disagreement: it compared a 1/6-octave-smoothed capture with a
  raw prediction. On the identical retained 50 µs capture, smooth↔smooth is
  **0.312 dB RMS / 0.490 dB max** and raw↔raw is **0.347 / 0.606 dB**. Woofer,
  tweeter, and VERIFY all used the same **7.0 ms** gate and **142.857 Hz**
  validity floor with no detected earlier reflection, ruling out the room gate
  as the primary failure.

  The sweep also located the selector defect. The live MEASURE had a raw peak
  gap of **208.333 µs**, an inter-sweep clock contribution of **178.150 µs**,
  and therefore a physical remainder of **30.183 µs**. The corrected flatness
  curve's relevant local minimum is **55 µs**, matching hardware; leaving the
  clock term in moves that same minimum to **233 µs**, while centering the
  lobe on periodic GCC selects the neighboring ~**336 µs** basin. T2-core now
  centers its bounded lobe on the negative corrected physical gap plus
  parallax; GCC remains polarity/capture-confidence evidence. VERIFY's
  reference remains the fixed zero-residual aligned target so a wrong-lobe
  candidate cannot explain itself. Both measured and predicted curves receive
  identical smoothing; the raw prediction is used only for the modeled-notch
  mask. The standalone physical-delay/clock-drift test and a production-path
  wrong-GCC-lobe regression pin both contracts. The raw-notch-mask branch is
  separately pinned against smoothing erasing notch identity. Targeted suite:
  **189 passed**.

  Two fresh real relay + UMIK-2 flows then passed CHECK → MEASURE → automatic
  APPLY → VERIFY on JTS3. The final calibrated run selected a **53.669 µs
  woofer delay** from GCC **−354.167 µs**, improved objective ripple
  **7.1304→5.3800 dB**, and the live Camilla graph read back the same
  **0.0537 ms** delay. VERIFY passed the product's 1.5 dB gate at **0.824 dB
  RMS / 1.279 dB max** over **2–4 kHz**, with matching 7.0 ms MEASURE/VERIFY
  gates and the 142.857 Hz floor. The session restored volume exactly to
  **−15.15 dB**, recorded zero input overflows, and the user surface reached
  `done` / `Verified.` The earlier scripted run without the browser's stored-
  calibration payload also passed at **1.220 dB max**; it is supporting
  evidence only, not the definitive calibrated gate. Fresh independent
  adversarial re-review cleared **0 blockers / 0 should-fixes** after the final
  contract-text and raw-notch-mask coverage corrections. `scripts/test-fast`
  passed **2,585 + 13** tests and the focused final suite passed **189**. The
  full local merge runner remains an infrastructure caveat: macOS Python
  subprocesses SIGSEGV under both parallel and serial suite load, while the
  three implicated test cases pass **18/18** in isolation. Nothing is merged.

- *2026-07-22 (T2 hardware frame correction; Gate 1 failed)* — The first
  reviewed T2 deploy proved the estimator ran and the candidate was applied,
  but **failed Gate 3**: GCC seed **−355.531 µs** refined to **−233.531 µs**
  and the graph applied one 233.5 µs woofer delay, while three VERIFY captures
  worsened to **7.579 / 7.636 / 7.613 dB** (prior fix-1 baseline ~4.29 dB).
  Retained MEASURE/VERIFY replay plus a bounded protected-graph probe located
  a frame error around the measured **208.333 µs** full-IR argmax gap. The
  production response moved one-for-one with commanded delay but carried a
  stable model-coordinate offset across both the old and new runs. Direct
  probe results were 0 µs →
  **4.01 dB**, 50 µs → **2.79 dB**, 100 µs → **3.29 dB**, and 233.5 µs →
  **7.58–7.64 dB** against the original prediction (the 50 µs capture's
  smoothed crossover-ripple standard deviation was **0.94 dB**). The corrected
  frame removes only the inter-sweep clock term
  `ε × (tweeter_start − woofer_start)` from the argmax gap and retains the
  physical remainder in objective/prediction. The mandatory physics test now
  injects a **170 µs physical gap plus 170 µs clock offset**, recovers the
  physical −170 µs correction, and flattens the actual common-time-origin
  sum; the production-path fixture repeats the proof with nonzero drift on
  both delay signs. Targeted suite: **160 passed**.

  That correction invalidated the earlier Gate 1 result. On the two retained
  M1 captures, raw peak gap **208.3 µs** minus measured inter-sweep drift
  **180.6 / 182.8 µs** leaves a physical gap of **27.7 / 25.6 µs**. Inside
  the required GCC-centered lobe the corrected selector chooses **−335.5 /
  −329.3 µs** and improves max-minus-min ripple only **7.118→7.040 dB /
  7.082→6.998 dB**, nowhere near the ≤1.5–2 dB offline target. A full
  declaration-range scan lobe-hops to **−684 / −680 µs** at **4.680 /
  4.698 dB**, also inadequate. The new live MEASURE has **178.2 µs** clock
  contribution and **30.2 µs** physical remainder; its corrected selector
  chooses **−335.5 µs**, while the bounded hardware probe was best near a
  **50 µs woofer delay**. Per the T2 session stop rule, work stopped at Gate
  1: no corrected adversarial re-review, redeploy, or merge. JTS3 remains on
  reviewed commit `4b3f23258` and was restored to its pre-probe volume.

- *2026-07-21 (T2 Gate 1, hardware-free)* — Implemented the
  declaration-bounded summed-flatness delay selection on
  `claude/xover-t2-flatness`. The selected delay was then made to feed both
  prediction and apply in the argmax/parallax reference frame; GCC remained the
  labelled seed/polarity/capture-confidence source. A physical known-sum test
  recovers the flattening delay, and production-path coverage pins nonzero
  parallax on both signed lobes. Replaying the two retained M1 captures reduced
  max-minus-min ripple from **37.524→5.042 dB** and **30.253→5.118 dB**
  (standard deviation **7.729→1.065 dB** and **6.881→1.082 dB**), selecting
  a common **−22.0 µs peak-frame residual** instead of GCC's
  −353.5/−348.2 µs full-IR seed. The 2026-07-22 entry above records why
  that result was invalid: it did not remove the inter-sweep clock
  contribution from the argmax gap, and a candidate-specific prediction could
  explain its own apply. This historical Gate 1 claim is superseded and must
  not be used as merge evidence.

- *2026-07-21 (hardware validation, fix 1+3)* — **The reviewed increment
  (fix 1 band-clamp + fix 3 plausibility bound; fix 2 reverted, fix 4
  dropped) was deployed to JTS3 and run headless** — direct-Pi control, no
  browser, same shape as E0. **Fix 1 validated:** MEASURE
  `alignment_confidence` went from E0's ~0.50 (every run refused) to
  **0.737** (accepts, reproducible across 2 runs); `predicted_ripple` dropped
  from E0's 10.96–24.72 dB to **5.3 dB**. First time the flow has completed
  CHECK→MEASURE→auto-apply→VERIFY end to end. **VERIFY reproducibly fails at
  ~4.29 dB** against the 1.5 dB gate. Root cause: fix 1 fixes *confidence*,
  but the confident GCC delay (~348 µs) is still off the delay that actually
  flattens the sum — the 1-octave-overlap comb ambiguity from the "refined
  diagnosis" above, now confirmed on hardware rather than only in the
  offline corpus. **Reverting fix 2 was the right call**: VERIFY compares the
  applied sum against the τ=0-aligned flattest prediction, not a
  prediction-at-the-applied-delay, so a wrong delay fails honestly — predicting
  at the applied delay would have masked it. Caveat: the mic was at an
  uncontrolled placement this run (owner away), so the 4.29 dB conflates
  placement error with delay-accuracy error until a controlled run separates
  them. Corpus: `captures/xover-e0-2026-07-21/M1-fix-deployed/`.
  **Recommendation (owner decision pending): T2 (flatness estimator) as the
  primary fix, fix 4 (widen the tweeter sweep) as reliability hardening.**
  Feasibility already showed the flatness-optimal delay hits 0.6–2.0 dB
  summed ripple vs GCC's ~4–5 dB — and because v2 VERIFY measures as-crossed,
  that ripple number *is* what VERIFY scores, so switching the applied delay
  from the GCC correlation peak to the flatness-minimizing delay is what gets
  VERIFY under the gate. Offline-provable on the retained captures; same
  class of subtle-SP risk as the reverted fix 2, so it needs its own physics
  test plus its own adversarial review before merge. Fix 4 is recommended as
  hardening on top, not instead: it collapses T2's comb of near-equal minima
  to a single deeper minimum, avoiding lobe-hopping and buying margin under
  1.5 dB — but it's hardware-gated and needs the excitation-admission
  reconciliation the adversarial review flagged (the widened sweep is
  currently refused by `admit_excitation` unless the admitted band widens
  too). **Sequence:** build T2 (offline-provable) → controlled hardware
  VERIFY → add fix 4 if T2 alone is marginal; do both for the robust end
  state. The reviewed fix1+fix3 increment stays on `claude/xover-measure-fix`
  (adversarial-reviewed at 0 blockers / 0 should-fixes), **not merged** —
  merge-now-vs-hold-for-T2 is an owner decision, since fix 1 alone converts
  "MEASURE refuses" into "applies → VERIFY fails → undo," a real but
  incomplete improvement.
- *2026-07-21 (E0 results)* — **E0 executed on hardware** via headless
  direct-Pi control (`e0_capture.py` speaking the real phone/relay wire
  protocol from a Mac + UMIK-2 against `jts3.local`; no browser touched, no
  product code changed) at `856903ca1`. First confirmed `856903ca1` vs
  `a04694627` (the prior successful calibration campaign's commit) is
  purely additive around alignment/confidence — `_gcc_phat`,
  `_estimate_alignment`, `_build_candidate`, `_predicted_sum`,
  `_solve_trims`, `ALIGNMENT_CONFIDENCE_TRUST_FLOOR` all byte-unchanged in
  the diff — so E0's outcome is evidence about the estimator, not a
  regression artifact. 3 placements (`P1-tweeter`, `P2-midpoint`,
  `P3-baddesk`), 7 completed runs, **every run refused at MEASURE with
  `low_alignment_confidence`**; none reached VERIFY. Three findings: (1)
  confidence never cleared 0.60 anywhere (max observed 0.5189); (2) GCC
  delay reproducible *within* a run (σ ≈ 0.16 samples, mic untouched) but
  wanders non-geometrically across runs — `P3-baddesk`'s own two repeat
  runs disagreed by ~15 samples (~306 µs) with nothing about the placement
  nominally changed; (3) predicted ripple 10.96–24.72 dB everywhere,
  including at `P1-tweeter` (the "good" placement) — candidates genuinely
  don't flatten the sum, independent of the confidence gate. **Tier 1
  confirmed insufficient by the data**: T1.1's floor-clamp is a
  mathematical no-op on this entire corpus (every branch
  `validity_floor_hz` ≤ 302 Hz, so `max(1000, floor)` = 1000 on all 14
  MEASURE captures — the room's ~10 ft-cube geometry caps the
  reflection-free gate window at ~7 ms regardless of mic height, visible
  directly in the data as 12/14 floors landing at exactly `1000/7 =
  142.857` Hz); T1.2's midway placement does not lift confidence
  (`P2-midpoint`'s 0.436–0.474 band sits at/below `P1-tweeter`'s
  0.467–0.519) and its own success criterion (verify flat *and* tighter
  spread) couldn't even be evaluated — no run at any placement ever
  reached VERIFY. **Decision: go straight to T2-core** — skip re-deriving
  more Tier-1 hardware data; E0 is the confirmation the stop rule (§2)
  calls for. **Sequencing:** fix the estimator (T2-core, §4) against the
  retained corpus using the same headless direct-Pi-control harness first
  (fast offline iteration + fast hardware confirm), *then* productionize
  the phone/browser capture-page experience — not before, since every
  capture path (phone or headless) depends on the same estimator. Corpus:
  `captures/xover-e0-2026-07-21/` (`MANIFEST.md` full per-capture table,
  `RESULTS.md` full write-up).
- *2026-07-21 (later)* — Owner calls: **B3 (coherence-weighted phase-slope +
  σ_τ) is IN SCOPE** as documented best practice, not data-gated — build the
  full estimator (B1+B2+B3) right; discipline preserved by sequence (Tier 1
  first) + the §2 stop rule, not by dropping a layer. **Borrow aggressively**
  from open-source prior art (any language — pseudocode / port / learn),
  captured as concrete repos in §12 for the implementer.
- *2026-07-21* — Plan reframed from a 6-phase build into **Tier 1 (small, now) /
  Tier 2 (estimator, gated on measurement) / Tier 3 (deferred)** with a written
  stop rule (§2), to prevent scope creep after several weeks of work. Distance
  settled at **1 m** (near-field invalid at 2 kHz per Keele `ka<1`; miniDSP
  measures high crossovers at 1 m). Midway placement adopted as a Tier-1
  *simplification* pending E0. Estimator rewrite justified by B1/B2 (§5) but
  gated on E0's data.

## 11. Current status / next action

- **Status: implementation built; pending adversarial review + JTS3
  confirmation.** Diagnosis of the failed repeat is complete (overlay artifacts
  in `captures/xover-e0-2026-07-21/overlay-20260722/`; prior-art reports in
  `captures/xover-e0-2026-07-21/prior-art-20260722/`), the offline fine-stage
  bake-off ran (`captures/xover-e0-2026-07-21/bakeoff-20260722/`) and picked
  **anchor + gated-GCC local-peak snap** (§10, 2026-07-22), and that selector is
  now implemented in `jasper/audio_measurement/program_analysis.py`
  (`_gcc_local_peak_snap` + the `GCC_SNAP_RADIUS_PERIODS` = period/6 radius; the
  `_flatness_search_lobe_us` / `_flatness_delay_us` *selection* path is deleted,
  flatness demoted to `alignment_seed_ripple_db` / `flatness_improvement_db`
  evidence; `flatness_at_bound` retired for `anchor_delay_us` / `snap_delta_us`
  / `snap_found`). Hardware-free physics/regression tests land in the same
  change; canonical operational truth is
  [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md)
  "Delay selection". Local replay on the two hardware-anchored captures matches
  the bake-off within <1 µs (run A applied 33.7 µs, run B 31.4 µs; anchors
  28.281 / 49.948 exact; A–B spread 2.4 µs, inside the ±20.8 µs stop-rule).
- **Next action:** independent adversarial review (0/0) of the implemented
  selector, then an on-device JTS3 CHECK→MEASURE→APPLY→VERIFY confirmation —
  the decisive hardware-reproducibility test the offline work cannot settle.
  PR #1647's CI and 0/0 review remain code-quality evidence only. Do not resume
  T2-robust / Fix 4 until the base selector clears that hardware gate.
- **Room caveat carried forward:** JTS3's room is a ~10 ft cube, capping
  any reflection-free analysis window at ~7 ms regardless of mic
  placement (confirmed directly in E0's data — see §10). Any future
  measurement session in this room should expect the same ceiling; it is
  not something placement alone can fix.
- **JTS3:** up and available.
- **Corpus:** `captures/xover-e0-2026-07-21/` (`MANIFEST.md` / `RESULTS.md`)
  plus the fix-1-deployed run at
  `captures/xover-e0-2026-07-21/M1-fix-deployed/`.

---

## 12. Reference implementations & prior art to borrow from

**Borrow aggressively — but borrow-mode is load-bearing** (this project is
Apache-2.0, and we want it widely adopted, so no copyleft contamination):

- **Adapt/port** (MIT / BSD / Apache): copy or port with attribution.
- **External dependency only** (LGPL): import as a dep, don't vendor inline.
- **File-isolate or clean-room** (MPL-2.0): keep vendored files separate under
  their own license, or reimplement.
- **Clean-room the *method* only** (GPL / non-OSI / closed): read to learn the
  algorithm, reimplement on scipy/pyfar; **never copy code**.

Licenses below were read from each repo's LICENSE/metadata (verified 2026-07-21),
not guessed.

| Component | Repo | URL | License | Lang | What to borrow | Mode |
|---|---|---|---|---|---|---|
| Clock-drift oracle | microsoft/Asynchronous_impulse_response_measurement | https://github.com/microsoft/Asynchronous_impulse_response_measurement | MIT | MATLAB | Joint clock-drift + IR estimation from async capture (Gamper HSCMA 2017) — the exact SRO algorithm | Adapt/port (MATLAB→Python; repo archived but readable) |
| Sweep gen + deconv → IR | pyfar/pyfar | https://github.com/pyfar/pyfar | MIT | Python | `exponential_sweep_freq`, `dsp.deconvolve`, `RegularizedSpectrumInversion`; **`find_impulse_response_start` / `find_impulse_response_delay`** (sub-sample delay) | Adapt/port |
| Sweep + **repeated sweeps** + capture | maj4e/pyrirtool | https://github.com/maj4e/pyrirtool | MIT | Python | ESS with repeated/averaged sweeps + deconvolution + `sounddevice` — closest to our capture harness | Adapt/port |
| Farina ESS reference | tikonen/farina_sweep | https://github.com/tikonen/farina_sweep | MIT | MATLAB/Py | Canonical Farina ESS + inverse filter + harmonic separation, well-commented | Adapt/port |
| **Coherence-weighted delay estimator (B3)** | SiggiGue/gccestimating | https://github.com/SiggiGue/gccestimating | **MPL-2.0** | Python | GCC family: CC, Roth, **SCOT**, PHAT, Eckart, **Hannan–Thomson ML** — the coherence-weighted TDOA T2-robust needs | File-isolate (MPL) or clean-room |
| Delay estimator baseline | LCAV/pyroomacoustics | https://github.com/LCAV/pyroomacoustics | MIT | Py/C++ | `experimental.localization.tdoa(..., phat=True)` — clean GCC-PHAT drop-in w/ fractional interp | Adapt/port |
| Min-phase / **excess group delay** (cross-check) | scipy/scipy | https://github.com/scipy/scipy | BSD-3 | Python | `signal.minimum_phase` + `group_delay`; excess GD = total − min-phase — **already a std dep, essentially free** | Use directly |
| Min-phase/excess-phase decomp | nettings/drc-fir | https://github.com/nettings/drc-fir | **GPL-2.0+** | C++ | Mature min/excess-phase decomposition + windowing strategy | Clean-room method only |
| **Reverse-null / complex-sum validator (T2-core)** | psmokotnin/osm | https://github.com/psmokotnin/osm | **GPL-3.0** | C++/QML | Dual-channel TF, **coherence**, delay finder, **complex virtual sum** — best-matched to our sum-flatness/reverse-null check | Clean-room method only |
| FDW + x-corr align workflow | xPoiler/XPDRC | https://github.com/xPoiler/XPDRC | **Non-commercial, non-OSI** | Py/HTML | Concept only: REW-API IR extraction, x-corr alignment, excess-phase invert, FDW | **Concept only — license incompatible, do not reuse code** |
| Phase-linearization concept | rePhase | https://rephase.org | Freeware, closed | — | UX/approach concept | No source — concept only |
| TF-measurement harness | odoare/measpy | https://github.com/odoare/measpy | **LGPL-3.0** | Python | `tfe_welch` TF estimation, sweep gen, IR | External dep (import, don't vendor) |
| TF-measurement harness | PyTTaMaster/PyTTa | https://github.com/PyTTaMaster/PyTTa | MIT | Python | `FRFMeasurement` / `PlayRec` dual-channel FRF+IR scaffolding | Adapt/port |
| Acoustic signal utils | python-acoustics/python-acoustics | https://github.com/python-acoustics/python-acoustics | BSD-3 | Python | Bands/levels/generators grab-bag | Adapt/port |

**Highest-value borrows, mapped to this plan:**
- **Clock-drift oracle (E0/E-replay):** port `microsoft/Asynchronous_impulse_response_measurement`
  (MIT) MATLAB→Python; use `pyfar.find_impulse_response_delay` for the sub-sample residual. Our
  drift estimator is already hardware-validated — this is a regression oracle, not a rewrite.
- **T2-core / T2-robust delay estimator:** start from `pyroomacoustics.tdoa` (MIT GCC-PHAT
  baseline), step up to `SiggiGue/gccestimating` (MPL — SCOT + Hannan–Thomson ML) for the
  coherence-weighted B3 estimator (file-isolate or clean-room the weighting).
- **T2-core reverse-null / sum-flatness validator:** clean-room `psmokotnin/osm`'s complex
  virtual-sum + coherence; build on scipy/pyfar. (We already have `_predicted_sum` / `_ripple_db` —
  osm is the method reference for turning that into an auto-optimizer + confidence.)
- **Excess-GD cross-check (T2):** `scipy.signal.minimum_phase` + `group_delay` — already a
  dependency, so this cross-check is nearly free.
- **Do NOT reuse code from:** XPDRC (non-commercial/non-OSI, REW-dependent) or rePhase (closed) —
  concept references only.
