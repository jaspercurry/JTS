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

## Actionable deltas (routing table)

Findings that impact existing or planned contract numbers. **None are
hot-patched**: the affected values are inert until commissioning ships, so
each lands via its owning wave's reviewed revision.

| # | Finding | Report | Contract home | Disposition |
|---|---|---|---|---|
| 1 | `sustain_sag_fail_db=1.5` flat across tiers is **tier-inconsistent**: 30/60/90 s holds capture only ~45/58/62 % of steady-state compression (magnet soak, ~35–40 % of total, develops over 15–35 min). Asymptote-consistent observed limits ≈ 1.2 dB (aggressive/30 s) / 1.5 (normal/60 s) / 1.6 (conservative/90 s), or fit-and-extrapolate the sag(t) trajectory. | 04 | `MarginPolicy` in `jasper/bass_extension/targets.py` (rides `algorithm_version`) | Wave-1/4 numerics contract revision |
| 2 | `sustain_fc_shift_fail_pct=5.0` **symmetric** sits inside measurement noise (single factors ±3.0–4.6 %, unit spread ±4.9 %) and conflates port compression (raises fb) with suspension warm-up (lowers Fs). Replace with a **directional** gate (~+8–10 % upward only; downward = mechanical), a 5–15 s pre-conditioning burst, and small-signal-before-large ordering. | 04 | Same as #1 | Same revision |
| 3 | A ~1.0–1.5 dB **runtime headroom derating** on the boosted target is needed to cover un-captured magnet soak and the ~60 % program-dependent thermal-resistance spread. | 04 | Plan §8.4 headroom math | Wave-5 contract input |
| 4 | During sustain holds, **log commanded DSP level + limiter/gain state** — limiter action is indistinguishable from thermal sag without V/I sensing (which is a permanent non-goal). | 04 | Commissioning backend | Wave-4 revision |
| 5 | Mic-calibration policy: **require** calibration for phone mics (uncalibrated phone Qtc error ±25–40 % — or restrict phone fits to f0/fb and refuse Qtc); **recommend** for known USB mics (Qtc ±10–15 % with cal); conditional escalation when fitted Qtc is implausible (<0.45 or >1.2). Answers the mic-calibration open question (#8) tracked in `docs/bass-extension-waves/bass-commissioning-ux.md` (landing via its own PR). | 03 | Plan §7.1 preconditions | Wave-4 revision |
| 6 | Ported fb from the **cone-null frequency**, never magnitude-only port+cone summation (needs phase); a summation-vs-null disagreement >5–10 % flags crosstalk (documented ~16 % fb error case). `ported_v1` is already null-based — consistent; add the crosstalk caveat to its validation notes. | 03 | `adapters/ported.py` docs/tests | Confirmation + notes |
| 7 | Fit-residual gates: accept ≈ RMS ≤1.0 dB & max ≤3 dB; refuse ≈ RMS >2 dB or max >5 dB; plus a residual-**shape** diagnostic map (mic-distance droop, mic moved, room modes, port mis-weighting, clipping, SNR, wrong box model). Wave-1's sealed 1.5 dB RMS refusal sits inside the recommended warn band — revisit deliberately. | 03 | Adapters + capture-quality evidence | Same numerics revision as #1 |
| 8 | Scheduler parameter envelope: volume-rung lookup table; ~1–1.5 dB boost delta per rung; asymmetric dwell/hysteresis; retreat fast (tens of ms) / extend slow (hundreds of ms–s, release ≈2–3× attack); ≤0.1 dB response-delta per update at 10–50 Hz cadence; never hard-swap coefficients (crossfade/interpolate); CamillaDSP's own `volume_ramp_time` 400 ms as in-engine precedent; Bose's 25 Hz/ms emergency slew as the never-approach ceiling. | 01 + 02 | Plan §8.2 `select_target` | Wave-5 contract revision inputs |
| 9 | Measured −75 dBFS coefficient-patch bursts: **inaudible under program** at domestic calibration; risk case is near-silence; drive toward −90 dBFS via smaller steps; FFT-verify bursts are LF-concentrated. On-device ABX verification protocol: 2 listeners × 25 trials, 17/25 per-listener / ≈32/50 pooled thresholds, catch trials, Clopper–Pearson equivalence bound (N=50 excludes only p_c >≈0.67 — state it). | 02 | Wave-5 transitions + Wave-7 validation | Protocol adoption at those waves |
| 10 | Patents: Microsoft US 12,342,139 (assignee is **Microsoft, not Google**; adjusted expiry 2041-12-12) + Google US 10,200,003 / 10,666,217. Differentiators to preserve: per-**unit** commissioning, target-**selection** scheduling (no runtime MBDRC), open-source non-sensing feedforward. Professional FTO opinion recommended. | 01 | Plan §13 item 8 (patent implications; cross-refs §1 and §15) | Plan update + maintainer business decision |
| 11 | Short holds cannot bound steady-state compression; future enhancement: log full sag(t), two-term exponential fit, extrapolated-asymptote limit ~2.5–3 dB (τm poorly identified from ≤90 s — treat as lower bound, pair with #3). | 04 | Commissioning backend | Backlog (post-v1 enhancement) |
