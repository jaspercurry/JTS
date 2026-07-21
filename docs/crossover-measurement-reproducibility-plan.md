# Crossover measurement — reproducibility working plan

> **Status: PROPOSED — active working doc across sessions.** This is the
> execution reference for landing the "MEASURE is not reproducible → VERIFY
> fails" blocker on the v2 conductor flow. It is a *targeted refactor of the
> measurement core*, not a rewrite of the conductor architecture.
>
> Canonical operational truth: [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md).
> v2 decision record: [crossover-measurement-productization-design.md](crossover-measurement-productization-design.md).
> Keep those two authoritative; this doc is the plan + decision log until the
> work lands, after which the durable outcomes fold into the HANDOFF and this
> doc is archived.
>
> **Last updated:** 2026-07-21. **Owner:** Fable (architect/coordinator).

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

### T2-core — one consistent τ + a predictive confidence (causes b, c)
- **What:** coarse latency-immune cross-correlation (period disambiguation,
  geometry-bounded) → **bounded (±½-period, single-cycle-safe) reverse-null /
  summed-flatness refine** over the single capture's complex branch responses,
  reusing `_predicted_sum` / `_ripple_db` and evaluating reuse of `null_walk`'s
  selection logic. **One τ\*** flows to *both* prediction and apply. **Confidence
  = the sharpness of the ripple-minimum / null-depth vs τ** (a metric that *is*
  what VERIFY measures).
- **Benefits:** B1 (confidence predicts VERIFY) + B2 (prediction = apply). See §5.
- **Quality bar:** **SoC** (delay is one pure function; confidence falls out of
  it) + **observability** (the confidence + the objective curve into the diag
  events).
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
  principled confidence (it supersedes T2-core's sharpness heuristic), so we
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

- **B1 — a confidence that predicts VERIFY.** *Biggest quality benefit, and
  largely independent of placement.* Today the auto-apply gate uses the GCC peak
  margin, which doesn't predict whether applying the candidate will flatten the
  sum. That forces a lose-lose: set the floor loose → auto-apply things that
  then fail VERIFY (the current complaint); set it tight → reject good
  measurements and frustrate re-measures. A confidence that **is** the
  sum-flatness objective (T2-core's sharpness, or T2-robust's σ_τ) turns
  auto-apply into "apply when it will verify, guide when it won't" — which is
  exactly what every shipping calibrator does and what the product needs. This
  benefit stands **even if Tier 1 makes good-placement measurements
  reproducible**, because it's about decision quality, not delay accuracy.
- **B2 — one consistent τ (prediction = apply).** *Correctness fix that bites
  even at good placements.* Cause (c) is structural: the prediction assumes
  perfect argmax-peak alignment while a slightly different GCC-PHAT value is
  applied, so VERIFY can fail marginally on an otherwise-clean measurement.
  Deriving τ once, from the objective VERIFY checks, removes this by
  construction. Small conceptually, but it *requires* the T2-core objective to
  exist (you can't have "one τ from the flatness objective" without the flatness
  objective).
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

- **Status:** PROPOSED, awaiting owner sign-off on scope (§2 stop rule + tiering).
- **Next action:** on sign-off — deploy current `main` to JTS3, enable the
  retention marker, run **E0** (placement + baseline). Then M1 GO/NO-GO on the data.
- **JTS3:** up and available.

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
