# WO-0 / P6 — Farina harmonic-IR extraction from the sweeps we already run

> Agent B, 2026-07-29. Read-only re-analysis; no new measurement, no Pi
> writes. Repo checkout at commit `28d259f42`. Analysis venv
> `/Users/jaspercurry/Code/JTS/.venv` (numpy 2.4.6, scipy 1.17.1); no
> matplotlib/soundfile available, so plots are hand-emitted SVG.

**Verdict up front: P6 is productizable, at zero measurement cost, from the
MEASURE program the crossover flow already plays — but only with three
guards the current pipeline does not apply (LF high-pass before
deconvolution, a per-order noise-floor window, and reuse of the existing
glitch/ε gate). Without the high-pass the harmonic impulses are buried 15–20 dB
under deconvolved LF room noise and the method reports nothing.**

---

## 1. Why the method works here without any change to the stimulus

`jasper/audio_measurement/sweep.py` already emits a **synchronized** ESS
(Novak *et al.* 2015), not a vanilla Farina sweep, and its own module
docstring says why: harmonic-distortion impulses fall at predictable
offsets. For a synchronized sweep the identity is exact —
`sin(N·φ(t)) = sin(φ(t + L·ln N))` — so an N-th-order distortion product
deconvolves to a **clean impulse response at `−L·ln N` seconds**, with no
residual phase rotation to correct.

The repo already exposes the untrimmed operator P6 needs:
`jasper.audio_measurement.deconv.regularized_deconvolution_full` returns the
full circular deconvolution. (`deconvolve()` cannot be used — its
`direct_arrival_window` throws the negative-time region away.) So the whole
probe is: full deconvolution → window at `−L·ln N` → FFT → divide by the
gated fundamental. No new stimulus, no new capture, no new dependency.

Sweep constants recovered and cross-checked:

Sweep parameters, fitted back out of each segment's own PCM (instantaneous
frequency of an ESS is `f1·e^{t/L}`, so `ln f` is linear in `t`):

| corpus | f1 → f2 | duration | L | Δ(H2) | Δ(H3) |
|---|---|---|---|---|---|
| laptop full-range (2026-07-27) | 20 Hz → 20000 Hz | 10.0162 s | 1.4500 | 1.005 s | 1.593 s |
| MEASURE woofer leg (×3) | 149.8 → 4007.0 Hz | 4.010 s | 1.2200 | 0.846 s | 1.340 s |
| MEASURE tweeter leg (×3) | 1997.6 → 20024.7 Hz | 3.003 s | 1.3030 | 0.903 s | 1.431 s |
| MEASURE woofer pilots (×2) | 148.9 → 4031.7 Hz | 0.814 s | 0.2467 | 0.171 s | 0.271 s |
| cloud summed sweep | 149.8 → 20032.9 Hz | 6.006 s | 1.2267 | 0.850 s | 1.348 s |

`synchronized_sweep_metadata(20, 20000, 10.0, 48000, -12.0)` reproduces the
archived `stimuli/sweep_meta.json` exactly (L = 1.45, n = 480780), so the
laptop corpus and the product share one stimulus definition.

### Extractor validation (synthetic controls)

Before touching real data the extractor was checked against three synthetics
(`reanalysis-farina-validate.py`, and the control block inside this
directory's run log):

| control | H2 | H3 | "empty" offsets |
|---|---|---|---|
| ideal `y = x + 0.02x² + 0.005x³` | −76.9 dB | −106.9 dB | −135 … −150 dB |
| same **+ 100 ppm clock drift** | −68.7 dB | −98.5 dB | −111 … −128 dB |
| same **+ white noise at −50 dB** | −76.9 dB | −101.4 dB | **−102 dB, uniform** |

Two things this settles: (a) the extractor recovers discrete orders with
50–65 dB of separation from empty offsets, and (b) **clock drift does not
smear the harmonic structure** — a 100 ppm drift lifts everything ~8 dB but
keeps the orders discrete. Stationary noise raises a *uniform* floor.
Neither explains what the real JTS3 captures first showed.

---

## 2. The blocker, and the fix: deconvolved LF room noise

On the raw 2026-07-27 JTS3 sweep the negative-time region was not a set of
discrete impulses but a **continuum**, ~20 dB above the far floor, spanning
implied order ratios 1.0 → 3.6 continuously (`reanalysis-plot-farina-etc.svg`).
The iLoud, measured in the same room, same mic, same session, at matched
level, showed a textbook discrete H3 at exactly `−1.5929 s` (implied order
3.000) over an −88 dB floor.

High-passing the *capture* at 120 Hz before deconvolution collapses the
continuum completely:

| JTS3 desk r1, dB re own linear peak | ctrl 0.2 s | ctrl 0.5 s | ctrl 0.8 s | ctrl 1.3 s | far floor |
|---|---|---|---|---|---|
| no high-pass | −67.3 | −63.7 | −64.7 | −60.7 | −80.4 |
| high-pass 120 Hz | −81.7 | −81.7 | −82.0 | −81.9 | −81.2 |

After the high-pass the floor is flat to 0.8 dB across the whole harmonic
region and H3 stands 10 dB proud of it. The cause is the 20 Hz sweep start:
the regularized inverse has enormous gain at the low band edge, where a
desk-height speaker in a real room sits in traffic/HVAC rumble, and the ESS
inverse maps that LF energy into a bounded negative-time smear rather than a
uniform floor.

**This is the single most important P6 productization requirement.** It is
also a latent hazard for anything else that reads the non-causal part of the
deconvolution; the shipped linear path is protected only because
`direct_arrival_window` discards that region.

---

## 3. Results — corpus A: JTS3 vs a commercial reference, same mic and room

Source: `captures/iloud-comparison-20260727/sweeps/` — UMIK-2 s/n 8108494 at
0°, laptop-side, 48 kHz, 10 s synchronized ESS, three repeats per state,
levels matched to 0.15 dB over 500–2000 Hz before capture. This is the
cleanest control in the corpus for the mic-floor question: a commercial
speaker measured through the identical chain.

Distortion vs **fundamental** frequency, 1/6-octave, power-mean per band,
mic-calibration corrected at both f₀ and N·f₀
(`reanalysis-farina-bands.csv`, `reanalysis-plot-thd-jts3-vs-iloud.svg`):

| band (fundamental) | JTS3 H2 | JTS3 H3 | iLoud H2 | iLoud H3 |
|---|---|---|---|---|
| 150–300 Hz | −52.7 (at floor) | **−45.6 dB / 0.53 %** | −49.5 / 0.33 % | −52.5 / 0.24 % |
| 300–600 Hz | −54.1 (at floor) | **−48.9 dB / 0.36 %** | −51.4 / 0.27 % | −50.2 / 0.31 % |
| 600–1200 Hz | −61.9 (at floor) | −57.3 / 0.14 % | −48.9 / 0.36 % | **−42.6 dB / 0.74 %** |
| 1200–2400 Hz | −67.9 / 0.04 % | −61.3 / 0.09 % | −57.8 / 0.13 % | −50.7 / 0.29 % |
| 2400–4800 Hz | −70.3 (at floor) | −61.3 / 0.09 % | −66.5 / 0.05 % | −67.9 / 0.04 % |
| 4800–9600 Hz | −69.3 (at floor) | −67.3 (at floor) | −65.5 / 0.05 % | −74.1 / 0.02 % |

"at floor" = less than 6 dB above the per-order noise-floor window.

Repeatability across the three repeats of a state: **sd 0.03–0.42 dB**
(iLoud H3 600–1200 Hz: −42.59 / −42.66 / −42.62). The instrument is far more
repeatable than the effects being measured.

**Mic-floor answer.** The UMIK-2 + laptop chain resolves distortion with
6–30 dB of headroom at 0.02 %–0.74 %; the iLoud's H3 at 600–1200 Hz sits
29 dB above the floor. The microphone is *not* the sensitivity limit at the
levels that matter here. The two real limits are (i) LF room noise, removed
by the high-pass, and (ii) the *fundamental's own level* — where JTS3 was
10 dB down in the top octaves, the relative floor rose to −71 dB and H2 became
unmeasurable. That is a speaker-response limitation, not a mic limitation.

**Household-grade instrument.** The 2026-07-22 corpus
(`captures/xover-e0-2026-07-21/capture-dump-archive-20260722/`) has 14 UMIK-2
and 31 iMM-6C captures through the browser relay path. Their quiet-window
noise floors above 120 Hz are −68.8 dBFS (UMIK-2) and −73.3 dBFS (iMM-6C), but
their capture peaks were −15.3 and −22.3 dBFS, so the usable in-capture
dynamic range is 53.5 dB and 51.0 dB — within 2.5 dB of each other. Sweep
deconvolution adds ~25–30 dB of processing gain, which is how an ~52 dB
capture yields 80 dB harmonic floors. Note the corpus's "phone series"
(`phone-forensics-20260722/phone_manifest.py`) is *iPhone + iMM-6C*, i.e. a
calibrated mic on a phone; there is **no built-in-phone-mic sweep in the
corpus**, so the dissertation's phone-mic-harmonic-floor caveat remains
untested by our own data — flag it, do not claim it either way.

---

## 4. Results — corpus B: per-driver, from the shipped MEASURE program

Source: three Pi-side MEASURE captures pulled read-only from
`jts3.local:/var/lib/jasper/xover-capture-dump/`, each deconvolved against
**its own** `measure_program.wav` (the three programs' sha256 differ — each
session solves its own gains). Each program is 2-channel: ch0 → woofer path,
ch1 → tweeter path, with three bit-identical repeats of each driver's sweep
plus a leading two-level woofer pilot pair.

Per-driver means over the three in-capture repeats, 2026-07-29 noon session
(`reanalysis-plot-thd-per-driver.svg`):

| driver | band (fundamental) | H2 | sd | H3 | sd | headroom (H3) |
|---|---|---|---|---|---|---|
| woofer | 200–400 Hz | −53.0 / 0.22 % | 0.68 | **−47.0 dB / 0.45 %** | 0.11 | 18.6 dB |
| woofer | 400–800 Hz | −62.7 / 0.07 % | 0.58 | −51.7 / 0.26 % | 0.26 | 24.4 dB |
| woofer | 800–1600 Hz | −58.7 / 0.12 % | 0.12 | −60.5 / 0.09 % | 0.13 | 23.9 dB |
| tweeter | 2400–4800 Hz | −68.0 (at floor) | 0.62 | **−56.1 dB / 0.16 %** | 0.06 | 16.3 dB |
| tweeter | 4800–9000 Hz | −65.8 (at floor) | 0.73 | −60.7 / 0.09 % | 0.45 | 7.7 dB |

The 08:29 morning session reproduces this to within 1–4 dB with the same
ordering.

**Which driver dominates: the woofer, by ~9 dB.** Its third harmonic at
200–400 Hz (0.45 %) is the largest supported distortion anywhere in the
speaker, and it is 9 dB above the tweeter's worst supported band. For the
woofer H3 > H2 across the whole band — the symmetric-nonlinearity signature
(suspension / motor `Bl(x)`), not an asymmetric one. The tweeter's H2 sits at
or near the floor in both bands, so the horn's *even*-order behaviour is
currently unmeasured, not measured-as-low.

**Independent cross-check.** The laptop full-range corpus (different day,
different capture path, different DSP state, both drivers running) gives JTS3
H3 = −45.6 dB at 150–300 Hz and −48.9 dB at 300–600 Hz. The Pi per-driver
woofer-solo numbers are −47.0 at 200–400 and −51.7 at 400–800. Two entirely
independent measurement paths agree on the woofer's H3 within 1.5–3 dB. That
is a real corroboration of the extraction, not a self-consistency check.

### The glitched capture is nonsense, and the flow already knows

The 2026-07-28 16:23 MEASURE (`glitch_detected: true`,
`glitch_inputs: residual_desync`, `max_residual_samples: 1018`,
`discontinuity_samples: -2090.5`, `alignment_confidence: 0.0`,
`tweeter_repeat_epsilon_ppm: 495.2`) yields apparent THD of **1.9–3.2 %** with
every band flagged at-floor — an order of magnitude wrong. The flow had
already rejected that capture. **P6 must consume the existing
glitch / ε / alignment gate rather than re-deriving one**; a spliced capture
produces confidently wrong distortion numbers, which is exactly the
instrument-error class WO-0 exists to catalogue.

---

## 5. Is an onset level derivable from existing multi-level data?

**No — and the reason is specific and fixable.**

Three candidate levers exist, and each fails for a different reason:

1. **The in-capture two-level pilot pair** (`pilot_woofer_lo` / `_hi`, exactly
   10.0 dB apart, 1.5 s apart in the same capture, same DSP, same geometry)
   is the *right* instrument — it is P3, already shipping, in every MEASURE
   program. But its low leg sits at the harmonic floor: headroom 0.2–2.8 dB
   at noon, 0.1–2.0 dB in the morning. Only the `hi` leg is measurable
   (H3 headroom 13.6–16.3 dB). The pilot is 0.8 s, which gives ~7 dB less
   deconvolution processing gain than the 4.0 s sweep, and it is deliberately
   10 dB down. Both are fixable in the composer.
   *Side result the pilot pair does deliver today:* the two legs' recovered
   **fundamental** transfer functions match within 0.2 dB across a 10 dB level
   change — an independent confirmation of the flow's own `linearity_ok`.
2. **Cross-session drive differences.** The three MEASURE programs are
   −6.00 / −8.51 / −11.13 dBFS on the woofer and −15.01 / −30.54 / −31.23 dBFS
   on the tweeter. The 16 dB tweeter lever lives only in the glitch-rejected
   2026-07-28 capture. The remaining clean lever is 2.62 dB, and it is
   partially confounded: the two sessions' applied DSP differs by up to 1.1 dB
   in the bands where the harmonics land.
3. **The 2026-07-27 EQ states** (`desk` / `eqlow` / `eq` / `eq2`) look like a
   level ladder (capture rms −44 to −31 dBFS) but are **not usable as one**:
   `eqlow` is the cut-only pass-1 filter set *without* its +10.58 dB makeup
   gain, i.e. a −10 dB low-shelf below 2973 Hz. Acoustically-measured
   THD carries a `D(N·f₀)/D(f₀)` term — the speaker's own response ratio
   between the fundamental and where its harmonic lands — so a shelving change
   that is not flat across `f₀ → N·f₀` moves the measured ratio without any
   change in the driver. `eqlow` reports H3 *rising* 8.6 dB at a *lower*
   level, which is the artefact, not a physical onset. Excluded with reason.

What the clean-but-small lever suggests (low confidence, stated as a
hypothesis for P3 to test, not a finding):

| woofer band | H3 change for +2.6 dB drive | slope | memoryless-cubic expectation |
|---|---|---|---|
| 200–400 Hz | −0.16 dB | −0.06 dB/dB | +2.0 dB/dB |
| 400–800 Hz | +3.59 dB | +1.45 dB/dB | +2.0 dB/dB |
| 800–1600 Hz | +3.95 dB | +2.65 dB/dB (1.49 dB acoustic lever) | +2.0 dB/dB |

i.e. the 200–400 Hz H3 — the *largest* distortion in the speaker — appears
**level-invariant in ratio** over the range tested, while 400–1600 Hz grows
with level. If that survives a proper probe it points at a fixed
mechanism (a radiating/structural resonance, a port or cabinet artefact)
at 200–400 Hz rather than large-signal motor nonlinearity, which would change
the fix class from "level policy" to `physical`. **P3 needs a deliberate
≥12 dB two-level pair with the DSP held constant.**

---

## 6. Recommended P6 detector

Add to `jasper/attribution/` as one mechanism module (M6), consuming the
MEASURE capture the flow already has.

**Preconditions (refuse, do not guess):**
- `glitch_detected == false`, `alignment_status == "ok"`, and the phase's
  `epsilon_ppm` within its existing bound. A spliced capture reported 3.1 %
  THD in this pass.
- The capture high-passed at `max(120 Hz, 0.8 × f1_segment)` **before**
  deconvolution. Non-negotiable; without it the detector sees nothing.

**Per order N ∈ {2, 3} and per driver segment:**
1. Window the linear IR with the flow's existing 7 ms gate; window the
   harmonic IR identically at `−L·ln N`.
2. Estimate a per-order floor from ≥3 same-width windows placed **earlier in
   time** than the harmonic mark (its reverberant tail runs the other way);
   power-average them. This floor is deliberately conservative — for H2 the
   floor windows sit between H2 and H3 and pick up H3's tail.
3. Validity band: `f₀ ∈ [1.05·f1, f2/N/1.05]` — H2 to `f2/2`, H3 to `f2/3`.
4. Report `THD_N(f₀) = |H_N(N·f₀)| − |H_1(f₀)|`, mic-cal corrected at **both**
   frequencies (the cal does not cancel across the octave).

**Thresholds (seeded by this pass, all in dB re fundamental):**

| test | value | why this number |
|---|---|---|
| evidence gate | band THD ≥ **6 dB** above the per-order floor | below this the three repeats stop agreeing; at 4–5 dB headroom repeat sd was 0.6–0.7 dB vs 0.1–0.3 dB at ≥15 dB |
| repeat agreement | 3 in-capture repeats within **1.5 dB** | observed sd 0.06–0.73 dB on clean captures |
| report-worthy | band THD ≥ **−54 dB (0.2 %)** with the evidence gate met | separates the woofer's 200–800 Hz H3 (0.26–0.45 %) from everything at 0.04–0.12 % |
| household-visible | band THD ≥ **−40 dB (1 %)** | nothing in the corpus reaches this; the glitched capture's false 1.9–3.2 % is what this threshold would have caught |
| level-dependence (needs P3) | ratio slope ≥ **+1.0 dB/dB** ⇒ nonlinear; \|slope\| < 0.5 dB/dB ⇒ level-invariant | cubic gives +2.0; the observed clean split was −0.06 vs +1.45 |

**Fix-class routing.** A level-invariant-in-ratio band routes to `physical`
(a fixed mechanism), a level-growing band to `physical` + level policy, and a
band that only appears when the DSP boosts it routes to `measure_differently`
(the `D(N·f₀)/D(f₀)` trap above). **THD measured downstream of a non-flat
applied filter is not the driver's THD** — the detector must record the
applied-filter response at `f₀` and `N·f₀` alongside every finding, or the
`eqlow` artefact will be re-derived as a defect.

**Cost:** zero new measurement. Three extra deconvolutions per driver segment
on an existing capture; the harmonic windows are ≤8 ms each.

---

## 7. Files

| file | contents |
|---|---|
| `reanalysis-lib.py` | shared helpers (float-WAV reader, ESS fit, harmonic windows, 1/n-octave smoothing, SVG plotter). `reanalysis_lib.py` beside it is a symlink so Python can import it — the hyphenated name is unimportable |
| `reanalysis-farina-validate.py` | first-pass structure probe + control offsets |
| `reanalysis-farina-etc.py` | negative-time ETC → `reanalysis-plot-farina-etc.svg` |
| `reanalysis-farina-segment.py` | segments MEASURE/cloud/verify programs from their own PCM |
| `reanalysis-farina-main.py` | the pass: both corpora → CSVs + plots |
| `reanalysis-farina-bands.csv` | 222 rows: per capture / band / order — THD dB, %, floor, headroom, supported flag, drive |
| `reanalysis-farina-curves.csv` | per-capture THD and floor vs fundamental, 200 log-spaced points per order |
| `reanalysis-farina-method.json` | gate/window/segment parameters |
| `reanalysis-plot-thd-jts3-vs-iloud.svg` | JTS3 vs iLoud H2/H3 with floors |
| `reanalysis-plot-thd-per-driver.svg` | woofer solo vs tweeter solo vs the two pilots |
| `reanalysis-data/pi-pull/` | the read-only Pi pull (captures, programs, v2 state, UMIK-2 cal) |

Pi pull was read-only (`sudo tar -cf -` over ssh, no writes, no restarts):
7 capture WAVs from `xover-capture-dump/`, 3 `measure_program.wav`, 2
`cloud_measure_program.wav`, 22 per-position `summed/` WAVs, the v2 state, the
evidence JSONs, and the UMIK-2 calibration text. 81 MB total.

*Last verified: 2026-07-29*
