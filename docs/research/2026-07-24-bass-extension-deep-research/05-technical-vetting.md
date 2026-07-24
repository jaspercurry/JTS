# Technical vetting of the 2026-07-24 deep-research reports

> **Provenance:** in-repo adversarial vetting pass (2026-07-24), run after the
> four external reports were archived. Every load-bearing number was
> **recomputed** (numpy/scipy: the 3-node thermal-cascade ODE via matrix
> exponential, full least-squares curve fits, exact binomial/Clopper–Pearson
> statistics, ISO-226 threshold comparisons, and the repo's own
> `lt_response_db` at its pinned Qp). **Where this document and reports 01–04
> disagree, this document is the citation authority** — the reports remain
> archived verbatim as the record of what was delivered, errors included. A
> contract revision citing this directory MUST cite the corrected values and
> caveats below, not the reports' raw text.

## 1. Verdicts per check

**Report 04 thermal cascade — discrepancy, recoverable.** The reported
captured fractions 45/58/62 % reproduce the *naive decoupled weighting*
(Σ wᵢ(1−e^(−t/τᵢ)), wᵢ=Rtᵢ/Rt) to <0.5 pt. The **true coupled ladder ODE**
with the same parameters gives **42.2 / 53.2 / 57.0 %** (41.1 / 52.5 / 56.7 when τv is read as
Rtv·Ctv = 21.2 s rather than the stated 20 s — the citation band below
absorbs this) — the naive form is
always 3–5 points optimistic, because the modal time constants are
19.0/65.4/1491 s (the slow mode is 34 % slower than Rtm·Ctm; Ctg is not
small vs Ctm). The fractions are insensitive to τm across 15–35 min but
**sensitive to Rtg, which has no cited source** (Rtg 1→3 K/W moves the 30 s
fraction 42.2→36.6 %); the "Driver-A-like" parameter set is actually a
composite of two different drivers (Klippel Driver A + Chapman's 170 mm τg).
Rtm/Rt = 37.3 % reproduces. The 1.5/0.58 ≈ 2.6 dB extrapolation: the linear
form overstates by ~11 % at this magnitude (true nonlinear PC over the
coupled fractions ≈ **2.5 dB**) — headline survives. The printed
**δ = 0.0393 K⁻¹ is 10× wrong** (α_Cu = 0.00393 K⁻¹; report 01's own
"≈0.4 %/°C" is right); verified scale-invariant in the extrapolation, but it
must never be quoted for dB→coil-temperature conversion. The derived tier
table **1.2 / 1.5 / 1.6 dB reproduces robustly under every model variant**
(nonlinear+naive, linear+naive, nonlinear+coupled) — the strongest number in
report 04. The **derating derivation is dimensionally unsound** (it subtracts
two output-compression figures and applies the result as a target-boost
derating); strict thermal equivalence — hold eventual coil ΔT at the
screened value, ΔT ∝ P — gives **−2.40 dB** (computed on the naive 0.576
fraction; the coupled 0.532 gives −2.74 dB), i.e. the report's 1.0–1.5 dB is
~1 dB *less* conservative than its own logic implies. Also internal:
the stated R-split sums to 11.0 K/W against the same bullet's "measured
4.6–7.5 K/W"; the quoted AN42 spreads average 4.6 %, not the stated 4.9 %.

**Report 03 knee identity — exact, but applied to the wrong error shape.**
|H(f0)|/passband = 20·log₁₀(Qtc) verified to machine precision;
Qtc_fit/Qtc_true = 10^(−Δ/20) and the 11.5 %/dB rule follow exactly — **for a
knee-localized error**. The report then uses this to reason about phone-mic
LF roll-off, which is monotonic. Full 3-parameter least-squares fits over the
report's own band (0.3f0–3f0) show that under every realistic roll-off shape
the conclusion inverts: a 1st-order roll-off (1 dB droop at f0) biases fitted
**Qtc +24…+32 % (sign inverted — reads less-damped, not more)** and fitted
**f0 +14…+22 %** (the report's table allows ±5–10 % for phone f0); a
2nd-order roll-off gives +91…+124 % Qtc / +28…+39 % f0. Mechanism: the mic
high-pass cascades with the box high-pass and a 2nd-order fit absorbs the
product as higher f0 *and* higher Q. The fit-residual gate catches the
2nd-order case (2.1–3.2 dB RMS → RED) but the 1st-order and log-tilt cases
pass **green** carrying 7–22 % f0 bias. Survives untouched: the
require-calibration *policy* (rests on commercial-convergence evidence) and
the two-sided Qtc escalation guard (<0.45 / >1.2), which is robust to the
sign inversion by construction.

**Report 02 ABX statistics — off-by-one.** Exact binomial:
P(X≥17 | n=25, p=.5) = **0.0539**, NOT <0.05; the minimal per-listener
threshold is **18/25** (p = 0.0216). The report attached correct p-values to
k one lower than they belong to. Pooled **32/50 is correct** (p = 0.0325,
minimal). Clopper–Pearson upper bound on 25/50: one-sided 95 % = **0.624**
(two-sided-95 % 0.645) — the report's "~0.67" matches no standard 95 %
convention. And **n ≈ 80** pooled trials already exclude p_c > 0.60, not the
stated 100–150.

**Report 02 SPL / ISO-226 — reproduced, more conservative than claimed.**
All dBFS→SPL arithmetic exact. ISO-226 thresholds: a 30 dB SPL tone is below
absolute threshold for **f < 86 Hz** (25 dB: f < 108 Hz); margin at 50 Hz is
+14 dB. The verdict holds with margin — but the "confirm no content above
~200 Hz" caveat is loose: audibility risk begins at **~90 Hz** at the hot
calibration. Sharpen the FFT-verify band accordingly.

**Report 01 excursion physics — fully coherent.** x ∝ 1/f² at constant SPL
(4×/octave); +6 dB = 2× excursion; one-octave LT ≈ +12.3 dB ≈ ×3.98 volts ≈
×15.85 power; and the closure holds (4× volts at f0/2 in the
stiffness-controlled region restores exactly the SPL 1× gave at f0). The
three statements are one physical fact stated three ways.

## 2. Composed cross-report rules (for the Wave-5 contract)

**Rule A — rung ↔ update composition (no contradiction).** Per-update cap
and per-rung cap compose: `N_updates = ceil(max_f |ΔdB| / 0.1)` evaluated on
the **computed old→new response pair** over 10–500 Hz (never a nominal boost
figure, never a hardcoded ΔFp/ΔQp translation), at a fixed cadence of
10–50 updates/s, subject to max_f |ΔdB| ≤ 1.5 dB per rung. Computed with the
repo's `lt_response_db` (Qp pinned 0.65): per-target max|ΔdB| at the shipped
`n_targets=5` is **1.49 / 2.23 / 2.98 dB** (conservative/normal/aggressive) —
so honouring ≤1.5 dB/rung **requires n_targets ≥ 5 / 7 / 9** per tier, a
consequence neither report states. Report 02's ΔFp translation (0.5–1 % per
0.1 dB) verifies; its **ΔQp ≤ 0.01–0.02 is 1.7–3.4× too permissive** at
Qp=0.65 (0.02 ≈ 0.27 dB/step) — do not adopt; moot while Qp stays pinned.

**Rule B — retreat direction (real tension, resolved by ordering, not
speed).** A one-rung retreat at ≤0.1 dB/update is 0.4–1.5 s — 15–30× slower
than report 01's "tens of ms", and 2–4× slower than CamillaDSP's 400 ms
volume ramp. Resolution: **(B1)** for JTS-originated level increases (dial,
voice, dashboard) — make-before-break: complete the retreat, then gate the
`main_volume` ramp on it. **(B2)** for source-originated increases (Spotify/
Bluetooth/USB-host/AirPlay observed via `VolumeCoordinator`) the retreat
cannot lead: the contract states a bounded un-retreated window equal to the
retreat duration, and the commissioned excursion ceiling carries explicit
headroom for one rung over that window. **(B3)** report 01's "tens of ms" is
NOT adopted as a JTS latency target — it is protection-loop guidance for
signal-envelope-limiter architectures; JTS is target-selection and originates
every B1 rise itself. Record as a deliberate, reasoned divergence (it is
also the differentiator vs the Microsoft MBDRC claim). **(B4)** masking by
the volume move is a supporting argument, never the mechanism — it does not
close the excursion window.

**Rule C — three budgets, never netted.** C1: thermal derating (report 04,
static, judgment ~1.0–1.5 dB, thermal only). C2: excursion headroom for the
B2 un-retreated window (not covered by report 04). C3: instantaneous
headroom (report 01's control input to the schedule — not a margin).

## 3. Classification of the routing table's 11 rows

| # | Class | Basis |
|---|---|---|
| 1 | Structure **CONTRACT-READY**; numbers **NEEDS-BENCH** | Flat-gate inconsistency verified from code (only flat gates among tiered siblings). Tier table 1.2/1.5/1.6 robust under every model variant, but inherits fractions 3–5 pts optimistic + uncited Rtg. Measurement: one ≥10 min sustain hold on the JTS driver logging sag(t) → fit Rtm/Rt and τv/τg/τm. Fit-and-extrapolate is a **Wave-4 data-shape change** (`assess_sustain` takes start/end scalars only), not a `targets.py` edit. |
| 2 | Structure **CONTRACT-READY**; value **NEEDS-BENCH** | The symmetric `abs()` provably conflates opposite-sign mechanisms. Measurement: ≥5 repeat holds; gate above observed spread (report's own >3–4 % noise-limit test). |
| 3 | **JUDGMENT-CALL** | Direction sound; derivation unsound (strict equivalence −2.40 dB). Adopt as engineering margin with stated caveats: not derived, thermal-only, does not cover excursion (Rule C). |
| 4 | **CONTRACT-READY** | No number to validate; partly shipped (`limiter_evidence.py` carries `commanded_main_volume_db` + peaks). Gap = limiter-state sampling across the hold + reject-if-limiter-acted. |
| 5 | Policy **CONTRACT-READY**; error bars + f0/fb fallback **NEEDS-BENCH** | Require/recommend policy rests on commercial convergence — untouched. But "restrict phone to f0/fb" assumes f0 survives; under realistic roll-offs f0 takes +14…+39 % bias (the maximum §1's computed cases produce). Measurement: same driver fitted from calibrated UMIK-2 vs uncalibrated phone vs a DATS/impedance reference. |
| 6 | Null method **CONTRACT-READY** (confirmation); threshold **JUDGMENT-CALL** | `_locate_fb` already null-based. The >5–10 % flag rests on a single documented case; repo runs a different 20 % check today. |
| 7 | Shape map **CONTRACT-READY**; numbers **JUDGMENT-CALL — do not loosen** | Shape map corroborated by the tilt experiment. The gate is a *weak* guard against mic tilt (1st-order/log-tilt pass green with 7–22 % f0 bias). **Do not loosen the shipped `rms > 1.5` refusal to 2.0 on this report's authority.** |
| 8 | Composition + ≤1.5 dB/rung + 25 Hz/ms ceiling **CONTRACT-READY**; 0.1 dB cap **JUDGMENT-CALL**; "tens of ms" **RESTATED** (Rule B3); ΔQp translation **REJECTED** | ≤1.5 dB/rung conflicts with shipped `n_targets=5` → forces n ≥ 5/7/9. 0.1 dB cap self-flagged as conservative extrapolation — adopt with that caveat. |
| 9 | Verdict + FFT-verify **CONTRACT-READY (sharpened to ~90 Hz)**; ABX thresholds **BLOCKED as written**; protocol shape **CONTRACT-READY** | See blocker 1. Pooled 32/50 may be cited. |
| 10 | **JUDGMENT-CALL / maintainer decision** | Internally consistent; differentiators match the repo's actual architecture. Note: a parallel-chain crossfade (reports 01/02 fallback) is not an MBDRC — a contract should say so explicitly. |
| 11 | **NEEDS-BENCH** (backlog routing correct) | Hazard: ~2.5–3 dB does not follow from the report's own 60 s anchor (2.35–2.60) and would LOOSEN today's equivalent gate; the ≤90 s fitted asymptote is a lower bound — compounds unsafe. Same measurement as row 1. |

## 4. Citation blockers (a contract revision must not cite these as written)

1. **ABX per-listener threshold and equivalence bound:** cite **18/25**
   (not 17/25 — that is p = 0.054) and **0.62** (one-sided 95 % CP; not
   0.67), and **n ≈ 80** to exclude p_c > 0.60. Pooled 32/50 is correct.
2. **Report 03's mic-tilt error partition:** do not cite the box-type
   error-bar table, the 10–12 %/dB rule *as a phone-mic sensitivity*, or the
   f0/fb-only phone fallback until the UMIK-2-vs-phone-vs-impedance bench
   comparison runs. The require-calibration policy itself may proceed.
3. **Report 04's captured fractions:** cite as **≈41–46 / 52–58 / 56–62 %,
   model-dependent** (never point values), always with the Rtg-uncited
   caveat. The tier table 1.2/1.5/1.6 may be cited as-is.
4. **δ = 0.0393 K⁻¹:** 10× wrong; never quote for temperature conversion.
5. **No loosening on these reports' authority:** the ~2.5–3 dB asymptotic
   limit and the 1–2 dB RMS warn band both sit above shipped/derived gates;
   any loosening waits for JTS-hardware data.

Clean to cite as-is: report 01's excursion physics; report 02's SPL/ISO-226
verdict (which under-claims its own margin); the pooled ABX threshold; the
composed Rules A–C above.
