# WO-0 / P2 — position-variance classifier pass over the 2026-07-29 clouds

> Agent B, 2026-07-29. Read-only re-analysis; no new measurement, no Pi
> writes. Repo checkout at commit `28d259f42`.

**Verdict up front: the classifier works, the separation is not marginal —
source-fixed features hold to CV 0.6–1.5 %, room features walk at CV 15–17 %,
a factor of ten apart. But the classifier cannot be built on the persisted
session state: per-position curves are NOT stored. Only the raw per-position
WAVs survive, and only on the Pi. That is a harness data-layer requirement,
and it is the first thing WO-1/WO-2 should fix.**

---

## 1. The data-layer finding (state this first — it gates the probe)

`/var/lib/jasper/active_speaker_crossover_v2_state.json` persists, under
`cloud.cloud_measure`:

- **one aggregate 512-point curve** (`pipeline.curve`, 46.875 Hz spacing),
- **one aggregate geometry verdict** — `median_tau_us: 317.8`,
  `n_confident: 6`, `n_positions: 8`, `clustered_fraction: 1.0`,
  `locked: true`, plus the household sentence *"The measured echo pattern did
  not change between microphone positions."*,
- a `null_registry` with 8 candidate nulls, their depths, and
  `classification: "insufficient_evidence"`, `reason: "no_ladder"`,
- and `positions: [...]` — which contains **only**
  `{attempt, index, position_id}`. No curve. No per-position τ. No
  per-position feature list.

The per-position evidence files
(`…/evidence/v1/artifacts/crossover_v2/<cap>/positions/*.json`, ~580 bytes)
carry `summed_ripple_db`, `gate_window_ms`, `validity_floor_hz`,
`glitch_detected`, the prompt text, and a WAV pointer — again, no curve.

So the flow **already computes per-position τ** (it reports how many were
confident and that they clustered) and then **throws the per-position values
away**. Everything in section 3 below had to be rebuilt from the retained
`summed/summed_cloud_measure_*.wav` captures on the Pi, deconvolved against
`cloud_measure_program.wav`.

**Requirements this places on the quick-sweep harness (§6 of the plan):**

1. Persist a **per-position analysed curve** (a compact one — the aggregate is
   already stored at 512 points; per position at 1/12-octave from the validity
   floor to 20 kHz is ~130 points × 12 positions ≈ 1.5 k floats).
2. Persist the **per-position scalar diagnostics the pipeline already
   computes** — τ, its confidence/prominence, the gate actually used, the
   per-position ripple — not just their aggregate. `clustered_fraction: 1.0`
   is a summary of a distribution nobody can inspect.
3. Persist the raw per-position WAV **path and hash** in the state, not only
   in a sibling evidence file, so the state alone is replayable.
4. The retained WAVs are `0600 root:root` under `/var/lib/jasper/…` — the
   "0600 friction" the plan already lists. A laptop-side agent cannot read
   them without `sudo tar`.
5. `positions` currently records attempt indices that skip
   (`cloud_measure_03_a03`, `_03_a04`, `_04_a05`, `_06_a08` …) — retries are
   visible in the filenames but the state's `positions` list does not say
   which attempt was accepted for which position, so "12 captures, 8
   positions" has to be inferred from filenames.

---

## 2. Method

`reanalysis-position-variance.py`. For each retained per-position summed
capture: bounded GCC-PHAT alignment to the program (bounded because the
program's repeats make an unrestricted argmax lock onto a repeat period),
`regularized_deconvolution_full` against the program's own summed-sweep
segment, the flow's own **7 ms gate** on the linear IR, UMIK-2 0°
calibration applied, 1/12-octave smoothing.

Two families of measurement per position:

- **Feature tracking** — for each named candidate feature, the deepest local
  deviation from a 1/1.5-octave baseline inside a bounded search window;
  the window half-width and whether the extremum hit the window edge are both
  recorded, so a reported spread can be read as a lower bound when it does.
- **Dominant echo delay τ** — cepstral peak over 4–18 kHz (the flow's own
  `echo_band_hz`), parabolic-interpolated. *Resolution matters:* the first
  pass used an 83 µs quefrency grid and reported a spurious 0 % spread with
  every position landing on 333.3 µs. The corrected grid is 2.6 µs.

Validation that this reproduces the shipped pipeline: the power mean of my
12 re-derived noon curves, normalised to 300–3000 Hz, matches the state's own
persisted combined curve with **rms 0.84 dB over 200–2000 Hz and 0.39 dB over
2–8 kHz (corr 0.968)**. Above 8 kHz it diverges to 2.6 dB rms — expected,
since that is exactly where positions disagree most and the flow's
spatial-combine rule is not a plain power mean. Treat absolute HF levels here
as mine, not the product's; the *feature frequencies* are unaffected.

Corpus: 12 per-position captures (noon, 2026-07-29 12:00–12:07) and 10
(morning, 08:29–08:37). `cloud_measure_program.wav` is byte-identical between
the two sessions (sha256 `9c27a04c…`), so cross-session comparison is exact.

---

## 3. The feature-stability table

Evidence gate: a feature is classified only when it is **≥ 2 dB deep in ≥ 60 %
of positions**; the CV is taken over the positions where it *is* deep, so a
shallow wobble cannot masquerade as a room-variant null. Thresholds:
**CV < 3 % ⇒ source-fixed, CV > 8 % ⇒ position-variant**, between ⇒ unsure.
`CV/win` is the CV as a fraction of the search half-width — a value near 1
would mean the search window, not the data, set the answer.

| session | feature | n / deep | mean Hz | spread Hz | spread % | CV % | CV/win | mean depth | class |
|---|---|---|---|---|---|---|---|---|---|
| noon | comb rung 8.6 k | 12/12 | 8499.6 | 284.9 | 3.35 | **0.90** | 0.060 | −3.33 dB | **source-fixed** |
| noon | comb rung 11.5 k | 12/12 | 11389.5 | 599.9 | 5.27 | **1.53** | 0.109 | −3.35 dB | **source-fixed** |
| noon | comb rung 5.4 k | 12/0 | 5294.9 | 344.2 | 6.50 | 1.98 | 0.132 | −1.34 dB | insufficient evidence |
| noon | comb rung 15.0 k | 12/4 | 15373.2 | 143.6 | 0.93 | 0.39 | 0.032 | −2.42 dB | insufficient evidence |
| noon | Fc notch ~1.9 k | 12/7 | 1686.0 | 547.8 | 32.5 | 11.77 | 0.471 | −2.36 dB | insufficient evidence (see §5) |
| noon | LF dip ~250 | 12/0 | 250.1 | 167.7 | 67.1 | 26.32 | 0.752 | −0.61 dB | insufficient evidence |
| noon | LF dip ~450 | 12/5 | 418.7 | 237.3 | 56.7 | 25.18 | 0.719 | −1.67 dB | insufficient evidence |
| noon | LF dip ~800 | 12/12 | 734.9 | 295.9 | 40.3 | **15.16** | 0.433 | −4.37 dB | **position-variant** |
| noon | LF dip ~1250 | 12/9 | 1161.1 | 521.5 | 44.9 | **15.40** | 0.513 | −2.71 dB | **position-variant** |
| morning | comb rung 8.6 k | 10/10 | 8451.0 | 142.9 | 1.69 | **0.58** | 0.038 | −3.00 dB | **source-fixed** |
| morning | comb rung 11.5 k | 10/10 | 11414.9 | 402.1 | 3.52 | **1.38** | 0.098 | −2.92 dB | **source-fixed** |
| morning | comb rung 5.4 k | 10/0 | 5353.6 | 521.5 | 9.74 | 2.71 | 0.181 | −1.37 dB | insufficient evidence |
| morning | comb rung 15.0 k | 10/1 | 14741.4 | 1282.4 | 8.70 | 4.01 | 0.334 | −0.95 dB | insufficient evidence |
| morning | Fc notch ~1.9 k | 10/5 | 1675.6 | 169.2 | 10.10 | 4.31 | 0.172 | −2.14 dB | insufficient evidence (see §5) |
| morning | LF dip ~250 | 10/0 | 273.7 | 174.3 | 63.7 | 25.25 | 0.721 | −0.49 dB | insufficient evidence |
| morning | LF dip ~450 | 10/5 | 380.9 | 21.9 | 5.75 | 2.52 | 0.072 | −2.00 dB | insufficient evidence |
| morning | LF dip ~800 | 10/9 | 853.5 | 428.5 | 50.2 | **17.37** | 0.496 | −3.09 dB | **position-variant** |
| morning | LF dip ~1250 | 10/9 | 1166.3 | 658.4 | 56.5 | **16.43** | 0.548 | −3.43 dB | **position-variant** |

Full per-position rows in `reanalysis-position-variance.csv`; the summary
table in `reanalysis-position-variance-summary.csv`; overlay plots in
`reanalysis-plot-positions-noon.svg` / `-morning.svg`.

**The separation is a factor of ten, in both sessions independently.**
Source-fixed features: CV 0.58–1.53 %. Room features: CV 15.2–17.4 %. Any
threshold in 3–8 % gives the same answer; the classifier is not
threshold-sensitive in the regime that matters.

---

## 4. τ across positions — M2 confirmed, with a caveat about the model

Per-position dominant HF echo delay (`reanalysis-position-tau.csv`):

| session | n | median τ | range | spread | CV |
|---|---|---|---|---|---|
| morning | 10 | 309.8 µs | 296.4 – 313.9 | 17.5 µs | **2.3 %** |
| noon | 12 | 307.7 µs | 121.1 – 315.9 | (one outlier) | 18.6 % |
| noon, outlier removed | 11 | 310.2 µs | 299.9 – 315.9 | 16.0 µs | **2.07 %** |
| **both, 21 confident positions** | 21 | **310.0 µs** | 296.4 – 315.9 | 19.5 µs | **2.13 %** |

**τ is source-fixed at CV 2.1 % across 21 microphone positions in two
sessions three and a half hours apart.** Implied path difference
310 µs × 343 m/s = **10.6 cm**, i.e. a round-trip inside a horn ≈ **5.3 cm
deep** — which corroborates the dissertation's own arithmetic correction
(0.3 ms is a *path difference*, implying a ~5 cm horn, not a 10 cm one) rather
than the earlier 10 cm-depth reading.

**Cross-checks against prior work (agreement is the signal here):**

- The flow's own `arrival_tau_us` for this session is **317.8 µs**; my
  independent cepstral estimator gives 310.0 µs — **+2.5 %** apart, from two
  different algorithms on the same captures. Corroboration.
- The prior Fc/comb forensics carried **τ ≈ 303 µs**; that is **−2.3 %** from
  this pass's median and sits inside the observed per-position range
  (296–316 µs). Corroboration — and the per-position range now gives that
  number an uncertainty it did not have.

**The one disagreement, and it matters.** A single-delay comb puts nulls at
odd multiples of `1/(2τ)`. The two solid rungs are 8500 and 11390 Hz (noon) /
8451 and 11415 Hz (morning) — spacings of 2890 and 2964 Hz, implying
**τ = 346 and 337 µs**, 9–12 % away from the cepstral 310 µs. The rungs are
real and rock-steady, but they are **not a clean single-τ ladder**. The
shipped detector reached the same conclusion from a different direction:
`null_registry.classification = "insufficient_evidence"`,
`reason = "no_ladder"`, with 8.6 k / 11.5 k / 15.0 k logged as
`no_matching_rung` and the shallower candidates (4619, 5369, 6786, 9683,
13007 Hz) rejected as `below_min_depth` at the 2.5 dB threshold. My pass
confirms both halves of that: the shallow candidates really are 0.6–1.4 dB
(unclassifiable), and the deep ones really do not fit one τ.

So M2's **fix class (`document_as_physics`) is right, but its signature
should not be "a τ ladder"** — it should be "HF ripple whose feature
frequencies are position-invariant", with τ reported as a characteristic
delay with a range, not as a ladder generator. Two-reflection geometry (horn
throat *and* mouth, or throat + a cabinet edge) is the obvious next
hypothesis; P4 (rotation solo) is the probe that would name the reflector.

**The one position-variant HF datapoint.** Noon position
`cloud_measure_04_a05` reports τ = 121.1 µs at *high* cepstral prominence
(14.5×, the strongest in the set) — not noise, a genuinely different dominant
reflector at that one spot (121 µs ⇒ 4.2 cm path difference, something very
close to the mic). A P2 detector must treat a single high-confidence outlier
as "this position saw a different reflector", not as scatter to be averaged
away: the flow's `clustered_fraction: 1.0` did not surface it.

---

## 5. The Fc notch is the interesting negative result

Per-position Fc-region minima (search window 1900 Hz ± 25 %):

- **noon:** 1425, 1425, 1430, 1684, 1723, 1775, 1783, 1783, 1973, 1978, 1980,
  1980 Hz — depths −1.0 to −6.0 dB
- **morning:** 1455, 1589, 1614, 1695, 1697, 1723, 1747, 1758, 1764, 1969 Hz
  — depths −0.9 to −4.4 dB

That is a **32–39 % frequency spread** and it is present at only 7/12 and 5/10
positions at the 2 dB depth gate — so under the gate above it comes out
*insufficient evidence*, and under a 1.5 dB gate it would come out
**position-variant** (CV 11.8 % / 4.3 %). Either way the honest reading is the
same and it is not the reading the seed table assumes:

> The Fc-region dip is **not a stable property of the speaker** across the
> positional freedom the cloud actually exercises. Its centre frequency moves
> by far more than the dissertation's own ">~10 % with vertical angle ⇒
> vertical lobing, do not EQ" threshold.

Consequences for the mechanism set:

- **M1** (inter-driver time misalignment at Fc, fix class `delay`) is seeded
  from a 5.0 dB phase penalty at 1919 Hz measured at **one** position. This
  pass says a single-position Fc measurement cannot by itself adjudicate M1 —
  the same speaker shows the dip anywhere from 1425 to 1980 Hz depending on
  where the mic is. P1 (reverse-null) and P5 (design-axis / vertical offset)
  are load-bearing, not optional, before a `delay` prescription.
- **M4** (frame mismatch, window vs power) gains direct support: the
  window-frame and cloud-frame Fc answers *must* differ, because the cloud's
  own Fc answer varies by 39 % within itself.
- The hard routing rule ("`eq` is never the routed class for a
  position-variant null") should be extended in spirit: **`delay` fitted from
  a single position at a position-variant feature deserves the same
  suspicion.**

---

## 6. Recommended P2 detector (thresholds seeded by this pass)

Inputs: per-position gated curves (which the harness must start persisting)
at ≥ 1/12-octave from the validity floor upward.

```
for each candidate feature f_nominal:
    for each position p:
        (f_p, depth_p, hit_edge_p) = deepest deviation from a
            1/1.5-octave baseline within f_nominal * (1 ± tol)
    frac_deep = fraction of positions with |depth| >= 2.0 dB
    if frac_deep < 0.60:            -> insufficient_evidence   (no finding)
    CV = stdev(f_p over deep positions) / mean * 100
    if CV < 3 %                     -> source_fixed        confidence: confident
    elif CV > 8 %                   -> position_variant    confidence: confident
    else                            -> unsure, recommend P4 (rotation solo)
    if any(hit_edge_p)              -> spread is a LOWER bound; widen and re-run
```

Search half-widths are policy, not physics, and must be recorded with the
finding: HF rung windows must stay inside half the rung spacing (± 15 % at
8.6 k / 11.5 k); room-candidate windows are deliberately wider (± 35 %)
because a boundary null is *expected* to walk. Report `CV / half-width`; if it
approaches 1 the window chose the answer.

Additional gates this pass argues for:

- **Minimum positions.** Both classes were unambiguous at n = 10 and n = 12
  with 100 % / 80–100 % depth coverage. Below ~6 deep positions the CV
  estimate is too noisy to separate 3 % from 8 %; refuse and say so.
- **Outlier surfacing, not averaging.** One position with a high-confidence
  but discordant τ is a finding ("one microphone position saw a nearby
  reflector"), not scatter.
- **Two mechanisms are directly seeded by this table**: the ~800 Hz and
  ~1250 Hz dips are position-variant, 3–4 dB deep, present at 80–100 % of
  positions — that is M5 (boundary/SBIR, desk bounce) with real numbers, and
  its fix class is `physical`. The geometry model should be checked against
  them: a first cancellation near 800 Hz implies a direct-vs-reflected path
  difference of ~21 cm.

---

## 7. Amendments proposed to the seed mechanism table

| # | current seed | what this pass says |
|---|---|---|
| M2 | "source-fixed reflection comb; τ ≈ 303 µs; signature = position-invariant comb, τ from excluded-band machinery" | **Confirmed source-fixed** (CV 2.1 % τ, CV 0.6–1.5 % feature frequency, 21 positions, 2 sessions). But **drop "comb/ladder" from the signature** — the rungs do not fit one τ (346/337 µs from spacing vs 310 µs cepstral), and the shipped ladder detector already refuses with `no_ladder`. Restate as "position-invariant HF ripple, characteristic delay 310 ± 8 µs". τ ≈ 303 µs prior **agrees** within the observed range. |
| M5 | "boundary/SBIR (desk bounce); observed: room-line corpus; predicted by geometry" | **Now observed in the crossover-line corpus too**, with numbers: dips at 735/854 Hz and 1161/1166 Hz, 2.7–4.4 dB deep, present at 80–100 % of positions, CV 15–17 %. Promote from "predicted" to "observed, two sessions". |
| M1 | "sum notch at Fc; per-position stable" | **"per-position stable" is contradicted.** The Fc dip walks 1425–1980 Hz across the cloud. Amend the signature; make P1/P5 required evidence before a `delay` prescription. |
| M4 | "frame mismatch (window vs power)" | Supported: the cloud frame's own internal Fc disagreement (39 %) is larger than most window-vs-cloud deltas being argued about. |
| — | *(new)* | **"Single-position discordant reflector"** is a real observed state (τ = 121 µs at one of 12 positions, prominence 14.5×). Whether it earns a registry entry or is just a P2 output flag is a design call; it must not be averaged away. |

---

## 8. Files

| file | contents |
|---|---|
| `reanalysis-position-variance.py` | the pass (alignment, gating, feature tracking, cepstral τ, classification, plots) |
| `reanalysis-position-variance.csv` | 198 rows — per session / position / feature: nominal Hz, search tolerance, found Hz, depth dB, edge flag |
| `reanalysis-position-variance-summary.csv` | the stability table above, machine-readable |
| `reanalysis-position-tau.csv` | per-position τ and cepstral prominence, 22 rows |
| `reanalysis-position-method.json` | gate, validity floor, program segment, program-hash note |
| `reanalysis-plot-positions-noon.svg` / `-morning.svg` | all per-position gated curves overlaid, normalised to 300–3000 Hz |
| `reanalysis-data/pi-pull/` | the read-only Pi pull |

*Last verified: 2026-07-29*
