# Bass Extension deep research — 2026-07-24 (index + routing)

> **Provenance:** four external deep-research reports commissioned by the
> maintainer and delivered 2026-07-24, archived verbatim in this directory.
> They are **research artifacts, not contracts**: nothing here changes any
> wave authorization. Every actionable finding routes through a reviewed
> contract revision per the table below; where a report and a frozen contract
> disagree, the contract wins until its owner revises it. Commissioned as the
> "prior art first" step of the bass-extension program — the parameters below
> ground wave contracts in what ships, instead of guesses.

## Reports

1. [`01-prior-art-dynamic-bass.md`](01-prior-art-dynamic-bass.md) — shipped
   volume/level-scheduled LF extension systems (B&O ABL, Devialet SAM,
   Dynaudio, TI PurePath, NXP/Goodix TFA, Sonos), the patent landscape, and a
   parameter envelope for the volume-indexed scheduler.
2. [`02-inaudible-filter-transitions.md`](02-inaudible-filter-transitions.md)
   — retuning live biquads inaudibly: structures, JNDs, step/cadence limits,
   the −75 dBFS artifact-floor verdict, and an on-device ABX protocol.
3. [`03-nearfield-parameter-extraction.md`](03-nearfield-parameter-extraction.md)
   — Keele near-field method limits, sealed/ported/PR parameter error bars,
   mic-calibration policy evidence, and fit-residual diagnostics.
4. [`04-sustain-hold-thermal-port.md`](04-sustain-hold-thermal-port.md) —
   what 30/60/90 s sustain holds do and don't reveal about thermal and port
   compression, and threshold recalibration.
5. [`05-technical-vetting.md`](05-technical-vetting.md) — **in-repo
   adversarial vetting of reports 01–04** (2026-07-24): every load-bearing
   number recomputed. **Citation authority over the reports where they
   disagree.** Found: an ABX off-by-one (per-listener threshold is 18/25,
   not 17/25; equivalence bound 0.62, not 0.67), a sign-inverting flaw in
   report 03's mic-tilt reasoning (realistic roll-offs bias Qtc *high* and
   f0 far beyond the published table), report 04's captured fractions are
   the naive decoupled weighting (coupled ODE: 42/53/57 %), a 10×-wrong
   copper coefficient, plus composed cross-report scheduler rules (A: rung↔
   update composition forces `n_targets ≥ 5/7/9` per tier; B: retreat
   ordering — make-before-break for JTS-originated rises; C: three budgets
   never netted) and a per-row contract-readiness classification.

## Actionable deltas (routing table)

Findings that impact existing or planned contract numbers. **None are
hot-patched**: the affected values are inert until commissioning ships, so
each lands via its owning wave's reviewed revision.

| # | Finding | Report | Contract home | Disposition |
|---|---|---|---|---|
| 1 | `sustain_sag_fail_db=1.5` flat across tiers is **tier-inconsistent**: 30/60/90 s holds capture only **≈41–46 / 52–58 / 56–62 % (model-dependent — vetting: the reports' 45/58/62 is the naive decoupled weighting; the coupled ODE gives the lower ends)** of steady-state compression (magnet soak, ~35–40 % of total, develops over 15–35 min). Asymptote-consistent observed limits ≈ 1.2 dB (aggressive/30 s) / 1.5 (normal/60 s) / 1.6 (conservative/90 s) — **the tier table is robust under every model variant** — or fit-and-extrapolate the sag(t) trajectory. | 04 + 05 | `MarginPolicy` in `jasper/bass_extension/targets.py` (rides `algorithm_version`) | Wave-1/4 numerics contract revision; **JTS-hardware ≥10 min hold to set the fractions before citing point values** |
| 2 | `sustain_fc_shift_fail_pct=5.0` **symmetric** sits inside measurement noise (single factors ±3.0–4.6 %, unit spread ±4.9 %) and conflates port compression (raises fb) with suspension warm-up (lowers Fs). Replace with a **directional** gate (~+8–10 % upward only; downward = mechanical), a 5–15 s pre-conditioning burst, and small-signal-before-large ordering. | 04 | Same as #1 | Same revision |
| 3 | A ~1.0–1.5 dB **runtime headroom derating** on the boosted target is needed to cover un-captured magnet soak and the ~60 % program-dependent thermal-resistance spread. **Vetting: the report's derivation is dimensionally unsound (strict thermal equivalence gives −2.40 dB); adopt as an engineering margin with stated caveats — thermal-only, does not cover excursion (see 05 Rule C).** | 04 + 05 | Plan §8.4 headroom math | Wave-5 contract input (judgment-call, caveats stated) |
| 4 | During sustain holds, **log commanded DSP level + limiter/gain state** — limiter action is indistinguishable from thermal sag without V/I sensing (which is a permanent non-goal). | 04 | Commissioning backend | Wave-4 revision |
| 5 | Mic-calibration policy: **require** calibration for phone mics; **recommend** for known USB mics; conditional escalation when fitted Qtc is implausible (<0.45 or >1.2). Answers the mic-calibration open question (#8) tracked in `docs/bass-extension-waves/bass-commissioning-ux.md`. **Vetting: the POLICY is contract-ready (commercial-convergence evidence), but report 03's derived error bars and the "restrict phone fits to f0/fb" fallback are BLOCKED as written — under realistic mic roll-offs the Qtc bias sign inverts and f0 bias reaches +14…+39 %; bench comparison (UMIK-2 vs phone vs impedance reference) required before citing the numbers.** | 03 + 05 | Plan §7.1 preconditions | Wave-4 revision (policy now; numbers after bench) |
| 6 | Ported fb from the **cone-null frequency**, never magnitude-only port+cone summation (needs phase); a summation-vs-null disagreement >5–10 % flags crosstalk (documented ~16 % fb error case). `ported_v1` is already null-based — consistent; add the crosstalk caveat to its validation notes. | 03 | `adapters/ported.py` docs/tests | Confirmation + notes |
| 7 | Fit-residual gates: accept ≈ RMS ≤1.0 dB & max ≤3 dB; refuse ≈ RMS >2 dB or max >5 dB; plus a residual-**shape** diagnostic map (mic-distance droop, mic moved, room modes, port mis-weighting, clipping, SNR, wrong box model). Wave-1's sealed 1.5 dB RMS refusal sits inside the recommended warn band — revisit deliberately. | 03 | Adapters + capture-quality evidence | Same numerics revision as #1 |
| 8 | Scheduler parameter envelope: volume-rung lookup table; ~1–1.5 dB boost delta per rung (**vetting: at the shipped `n_targets=5` the per-rung deltas are 1.49/2.23/2.98 dB — compliance forces `n_targets ≥ 5/7/9` per tier**); asymmetric dwell/hysteresis; extend slow (hundreds of ms–s, release ≈2–3× attack); ≤0.1 dB response-delta per update at 10–50 Hz cadence evaluated on the computed response pair (**never the report's ΔQp translation — rejected, 1.7–3.4× too permissive**); never hard-swap coefficients; CamillaDSP's 400 ms `volume_ramp_time` precedent; Bose's 25 Hz/ms as the never-approach ceiling. **Retreat direction: "tens of ms" is NOT adopted — see 05 Rule B (make-before-break for JTS-originated rises; bounded un-retreated window + excursion headroom for source-originated ones).** | 01 + 02 + 05 | Plan §8.2 `select_target` | Wave-5 contract revision inputs, **via 05's composed Rules A–C** |
| 9 | Measured −75 dBFS coefficient-patch bursts: **inaudible under program** at domestic calibration; risk case is near-silence; drive toward −90 dBFS via smaller steps; FFT-verify bursts are LF-concentrated **below ~90 Hz** (vetting sharpened the report's ~200 Hz band). On-device ABX verification protocol: 2 listeners × 25 trials, **18/25 per-listener** / ≈32/50 pooled thresholds (vetting corrected the report's 17/25 — that is p=0.054), catch trials, Clopper–Pearson equivalence bound (**0.62** one-sided 95 %, not the report's 0.67; **n≈80** pooled trials suffice to exclude p_c >0.60). | 02 + 05 | Wave-5 transitions + Wave-7 validation | Protocol adoption at those waves, **citing 05's corrected thresholds** |
| 10 | Patents: Microsoft US 12,342,139 (assignee is **Microsoft, not Google**; adjusted expiry 2041-12-12) + Google US 10,200,003 / 10,666,217. Differentiators to preserve: per-**unit** commissioning, target-**selection** scheduling (no runtime MBDRC), open-source non-sensing feedforward. Professional FTO opinion recommended. | 01 | Plan §13 item 8 (patent implications; cross-refs §1 and §15) | Plan update + maintainer business decision |
| 11 | Short holds cannot bound steady-state compression; future enhancement: log full sag(t), two-term exponential fit, extrapolated-asymptote limit (τm poorly identified from ≤90 s — treat as lower bound, pair with #3). **Vetting hazard: the report's ~2.5–3 dB limit does not follow from its own 60 s anchor (2.35–2.60 dB) and would LOOSEN today's equivalent gate — no loosening on this report's authority; JTS-hardware data first.** | 04 + 05 | Commissioning backend | Backlog (post-v1 enhancement; bench-gated) |

## Technical vetting (2026-07-24)

[`05-technical-vetting.md`](05-technical-vetting.md) recomputed every
load-bearing number above and is the **citation authority** where it and the
reports disagree. Its five citation blockers, in one line each: (1) ABX
per-listener threshold is **18/25** and the equivalence bound **0.62** —
report 02's 17/25 / 0.67 are off-by-one / non-standard; (2) report 03's
derived error-bar table and phone f0/fb fallback are blocked pending a
UMIK-2-vs-phone-vs-impedance bench comparison (sign-inverting flaw in the
tilt reasoning — the require-calibration policy itself proceeds); (3) report
04's captured fractions are cited only as the band ≈41–46/52–58/56–62 %
(the robust tier table 1.2/1.5/1.6 dB may be cited as-is); (4) the printed
δ=0.0393 K⁻¹ is 10× wrong — never quote for temperature conversion; (5)
neither report is authority to **loosen** any shipped gate. Clean to cite
as-is: report 01's excursion physics, report 02's SPL/ISO-226 verdict, the
pooled 32/50 ABX threshold, and 05's composed scheduler Rules A–C.
