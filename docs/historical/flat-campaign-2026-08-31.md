# Flat campaign, 2026-08-31 — the archived campaign record

> **Status: historical.** The owner-authored record of the 2026-08-31 flat
> campaign, banked verbatim below the rule. The campaign ran under
> [ADR-0192](../adr/0192-the-campaign-is-the-validation.md)'s ruling ("the
> campaign is the validation") on the blind-run baseline C5.
>
> **What this file is for.** Wave 6 of
> [`tuning-master-plan.md`](../tuning-master-plan.md) funds work whose
> evidence is this record — the per-filter re-examination list, the delay
> disposition, the trim-runaway scoreboard, the r8 regression that
> `forward_model` must postdict. Read this for *what was measured and what
> it taught*.
>
> **What it is not.** Not current state, not a roadmap, and not operational
> guidance: shipped operational truth is
> [`tuning-operator-runbook.md`](../tuning-operator-runbook.md), the
> acoustics method is [`tuning-methodology.md`](../tuning-methodology.md),
> and the planning authority is
> [`tuning-master-plan.md`](../tuning-master-plan.md). The §8 runbook
> adaptations below are campaign findings; whichever of them the live docs
> adopt, they hold in their own voice. Frozen: no further edits.

---

*A 2-way active DSP speaker (compression-driver horn tweeter + woofer, LR4 @ 2500 Hz, CamillaDSP on Raspberry Pi) taken from a factory-dark state to its flattest-ever measured response across two campaigns: a blind acceptance run and a targeted flat campaign. This document is self-contained: another LLM should be able to understand the speaker, the journey, the physics findings, and the methodology lessons from this file alone.*

## 1. The speaker and one crucial design fact

The tweeter is a compression driver on a **constant-directivity (CD) horn**. This matters for interpreting every curve in this record:

- A CD horn holds its coverage angle roughly constant with frequency. The price is that its **on-axis raw response falls with rising frequency** (roughly −6 dB/octave trend in the top octaves) — this downward tilt is a *designed property*, not a defect.
- Consequently the tweeter chain **must** carry substantial compensating EQ (in the final tune: a −6.9 dB low-shelf pivot plus a family of peaking cuts below it, which together produce a net rising drive toward the top). "The tweeter needs a lot of EQ" is expected for this topology.
- It also explains the factory entry state: with only an unmeasured datasheet trim and **no** CD compensation, the speaker measured ~9–11 dB dark in the top octaves. The entry tilt was mostly *missing horn compensation*, not broken drivers.
- Directivity was later demonstrated smooth (±22° family within ~1 dB of on-axis to 8 kHz, gentle beaming above) — which is what makes **on-axis flat a defensible target** for this speaker. On a speaker with ragged directivity, on-axis flat would be the wrong goal.

Measurement context: single-position robot-arm mic walks at ~1 m, 5 bearings (0°, ±7°, ±22°), 7 ms reflection gate → trusted region **357 Hz – 16 kHz**. Below 357 Hz the gate cannot resolve; above 16 kHz mic-cal tolerance and cm-scale position sensitivity are the same order as any correction. UMIK-2 with per-serial calibration applied in the analysis pipeline. All curves referenced to their own 357–2000 Hz power mean.

## 2. Where it started, where it ended

| State | Band 357–2k (±1.5) | Band 2k–8k (±2.0) | Band 8k–16k (±2.5) | Tilt | Notes |
|---|---|---|---|---|---|
| **Factory entry** | −4.64 | −6.83 | −10.45 | **9.03** | datasheet trim −10.8 dB, no linearization, no CD compensation |
| **Blind-run final (C5)** | +0.85 | +1.12 | −1.11 | 0.435 | 5 measure + 4 verify rounds, fresh agent, toolbox only |
| **Flat-campaign re-measure of C5** | +0.81 | +1.14 | −1.15 | 0.495 | hours later, agrees within 0.05 dB/band — the repeat floor |
| **Flat-campaign final (tonight)** | — | — | — | — | grade metric **1.112 → 0.90–0.93**; tilt-removed RMS **0.18 → 0.067 dB** |

Two comparability notes, honestly: (a) the campaign's attempt-grade metric (the number every candidate is scored on, worst-band family) is the apples-to-apples lineage: C5 graded 1.112; the final config graded 0.9035 and 0.93 in two verify rounds — a tie within the repeat floor, both clearly past the record. (b) The single most striking shape number is tilt-removed whole-curve RMS falling from 0.18 to 0.067 dB.

On-axis single-take repeat floor measured this campaign: **0.04 dB median** — the instrument is steady enough that 0.2 dB claims are honest claims.

## 3. The filter chains

### 3a. Starting chain (blind-run final "C5", 22 filters)

Structure: shared blend EQ + headroom trim → per-branch LR4 crossover @ 2500 Hz → per-driver linearization → delay (0) → per-branch gain → protective soft-clip limiter. All corrective EQ is cut-only except one small woofer boost; headroom cost 1.128 dB.

**Woofer channel:**

| # | Filter | Type | Parameters |
|---|--------|------|------------|
| 1 | `as_blend_1` | Peaking | 4463 Hz, Q 2.0, -1.52 dB |
| 2 | `active_baseline_headroom` | Gain | -1.13 dB |
| 3 | `as_woofer_woofer_tweeter_2500hz_lp` | LinkwitzRileyLowpass | 2500 Hz, LR4 (24 dB/oct) |
| 4 | `as_woofer_linearization_peak_1` | Peaking | 898 Hz, Q 1.5563, -7.27 dB |
| 5 | `as_woofer_linearization_peak_2` | Peaking | 1123 Hz, Q 2.0, -0.96 dB |
| 6 | `as_woofer_linearization_peak_3` | Peaking | 2197 Hz, Q 2.0, -2.17 dB |
| 7 | `as_woofer_linearization_peak_4` | Peaking | 410 Hz, Q 2.0, +2.23 dB |
| 8 | `as_woofer_linearization_peak_5` | Peaking | 365 Hz, Q 2.2, -1.40 dB |
| 9 | `as_woofer_delay` | Delay | 0.0 ms |
| 10 | `as_woofer_baseline_gain` | Gain | +0.00 dB |
| 11 | `as_woofer_baseline_limiter` | Limiter | soft-clip protection limiter |

**Tweeter channel:**

| # | Filter | Type | Parameters |
|---|--------|------|------------|
| 1 | `as_blend_1` | Peaking | 4463 Hz, Q 2.0, -1.52 dB |
| 2 | `active_baseline_headroom` | Gain | -1.13 dB |
| 3 | `as_tweeter_woofer_tweeter_2500hz_hp` | LinkwitzRileyHighpass | 2500 Hz, LR4 (24 dB/oct) |
| 4 | `as_tweeter_linearization_shelf` | Lowshelf | 5374 Hz, -6.90 dB (Lowshelf) |
| 5 | `as_tweeter_linearization_peak_1` | Peaking | 2323 Hz, Q 2.5796, -4.76 dB |
| 6 | `as_tweeter_linearization_peak_2` | Peaking | 3952 Hz, Q 2.0, -2.96 dB |
| 7 | `as_tweeter_linearization_peak_3` | Peaking | 4450 Hz, Q 1.7, -1.00 dB |
| 8 | `as_tweeter_linearization_peak_4` | Peaking | 7310 Hz, Q 1.5, -6.10 dB |
| 9 | `as_tweeter_linearization_peak_5` | Peaking | 10100 Hz, Q 1.6, -3.00 dB |
| 10 | `as_tweeter_linearization_peak_6` | Peaking | 15600 Hz, Q 1.6, -4.00 dB |
| 11 | `as_tweeter_delay` | Delay | 0.0 ms |
| 12 | `as_tweeter_baseline_gain` | Gain | +0.00 dB |
| 13 | `as_tweeter_baseline_limiter` | Limiter | soft-clip protection limiter |

### 3b. Final chain (tonight, 24 filters — two added)

**Woofer channel:**

| # | Filter | Type | Parameters |
|---|--------|------|------------|
| 1 | `as_blend_1` | Peaking | 4432 Hz, Q 2.0, -1.81 dB |
| 2 | `as_blend_2` | Peaking | 2655 Hz, Q 2.0, -0.73 dB **← added this campaign** |
| 3 | `active_baseline_headroom` | Gain | -1.13 dB |
| 4 | `as_woofer_woofer_tweeter_2500hz_lp` | LinkwitzRileyLowpass | 2500 Hz, LR4 (24 dB/oct) |
| 5 | `as_woofer_linearization_peak_1` | Peaking | 898 Hz, Q 1.5563, -7.27 dB |
| 6 | `as_woofer_linearization_peak_2` | Peaking | 1123 Hz, Q 2.0, -0.96 dB |
| 7 | `as_woofer_linearization_peak_3` | Peaking | 2197 Hz, Q 2.0, -2.17 dB |
| 8 | `as_woofer_linearization_peak_4` | Peaking | 410 Hz, Q 2.0, +2.23 dB |
| 9 | `as_woofer_linearization_peak_5` | Peaking | 365 Hz, Q 2.2, -1.40 dB |
| 10 | `as_woofer_delay` | Delay | 0.0 ms |
| 11 | `as_woofer_baseline_gain` | Gain | +0.00 dB |
| 12 | `as_woofer_baseline_limiter` | Limiter | soft-clip protection limiter |

**Tweeter channel:**

| # | Filter | Type | Parameters |
|---|--------|------|------------|
| 1 | `as_blend_1` | Peaking | 4432 Hz, Q 2.0, -1.81 dB |
| 2 | `as_blend_2` | Peaking | 2655 Hz, Q 2.0, -0.73 dB **← added this campaign** |
| 3 | `active_baseline_headroom` | Gain | -1.13 dB |
| 4 | `as_tweeter_woofer_tweeter_2500hz_hp` | LinkwitzRileyHighpass | 2500 Hz, LR4 (24 dB/oct) |
| 5 | `as_tweeter_linearization_shelf` | Lowshelf | 5374 Hz, -6.90 dB (Lowshelf) |
| 6 | `as_tweeter_linearization_peak_1` | Peaking | 2323 Hz, Q 2.5796, -4.76 dB |
| 7 | `as_tweeter_linearization_peak_2` | Peaking | 3952 Hz, Q 2.0, -2.96 dB |
| 8 | `as_tweeter_linearization_peak_3` | Peaking | 4450 Hz, Q 1.7, -1.00 dB |
| 9 | `as_tweeter_linearization_peak_4` | Peaking | 7310 Hz, Q 1.5, -6.10 dB |
| 10 | `as_tweeter_linearization_peak_5` | Peaking | 8216 Hz, Q 1.8, +0.80 dB **← added this campaign** |
| 11 | `as_tweeter_linearization_peak_6` | Peaking | 10100 Hz, Q 1.6, -3.00 dB |
| 12 | `as_tweeter_linearization_peak_7` | Peaking | 15600 Hz, Q 1.6, -4.00 dB |
| 13 | `as_tweeter_delay` | Delay | 0.0 ms |
| 14 | `as_tweeter_baseline_gain` | Gain | +0.00 dB |
| 15 | `as_tweeter_baseline_limiter` | Limiter | soft-clip protection limiter |

The two additions: **`as_blend_2`** (2655 Hz, −0.73 dB — derived independently by the deterministic region solver, agreeing with the human/LLM σ-analysis that had sized the same cut at −0.9) and **`as_tweeter_linearization_peak_5`** (8216 Hz, +0.80 dB — filling a measured dip, sized under the dip per the boost rule). Everything else is C5's proven set, held verbatim by prescription pins.

## 4. What happened with POLARITY

First signed acoustic proof in this speaker's history, via the reverse-null instrument (summed two-channel gated sweep, one branch invertible):

- In-phase pair at 0 µs: fc sits **−0.4 dB** vs shoulders (no notch → drivers sum constructively)
- Tweeter-inverted at 0 µs: **−6.4 dB** null (the flip carves a notch)
- **Reverse-vs-in-phase margin: 6 dB → polarity is correct**, now by measurement rather than inference. Repeatability of the instrument: repeated takes agreed within 0.1 dB.

Note: the achievable null depth was capped by branch-level mismatch in the measurement graph (~5.7 dB real gap → cap ≈ 7–8 dB), so 6 dB is a strong result, not a weak one. On a level-matched graph the same proof would read 15–20 dB.

## 5. What happened with DELAY (the campaign's biggest lesson)

**Measured:** the inverted-null landscape across delay coordinates read −3.4 / **−9.4** / −6.4 dB at −200/−100/0 µs (negative = woofer delayed). Single unambiguous peak at **−100 µs** (parabolic vertex ≈ −85 µs) — the tweeter's acoustic center sits ~3 cm forward of the woofer's, textbook for a horn. The inverted-null metric is EQ-insensitive (the EQ is identical in both polarity states, so the flip isolates the raw interference term) — this is the *true* structural measurement.

**Falsified as an improvement, on three independent frames:**
1. Delay + machine-refit EQ: seat-verified **regression** (+3.2/−4.0/+3.9 dB band deviations, uncommanded frame shift) → auto-restored.
2. Delay + *everything held verbatim* (EQ family, trims pinned, only the delay changed): seat-verified **regression** (−3.1 dB at the crossover region — exactly where the in-phase null predicted a dip) → auto-restored.
3. Off-axis: alignment made ±22° left/right crossover-band asymmetry slightly **worse** (1.23 vs 0.99 dB RMS) — horizontal lobing on this rig is placement-dominated, not time-dominated.

**Mechanism (the important part):** two campaigns of response-space tuning had *EQ'd around* the misalignment — the crossover-region filters flatten the 0 µs summation, interference structure included. Correcting the time under that EQ trades one error for a bigger one; the in-phase null landscape confirms it (in-phase response is best at 0 µs while raw alignment is best at −100 µs — the disagreement between the two landscapes IS the fingerprint of EQ-encoded structure error). Folding the delay in properly would require a ground-up refit whose best case ties the current result on every frame this rig measures.

**Disposition:** the −100 µs stands as banked physical truth about the drivers; the applied delay stays 0. This also resolves an independent critique's warning ("if that zero is measured, fine; if uncommissioned, the smooth crossover is luck") — the zero is now measured, and the measured answer is that *this tune* genuinely prefers it.

## 6. Round-by-round (flat campaign)

| Round | What | Result | Lesson |
|---|---|---|---|
| r0 | 5-seat BEFORE verify of C5 | 0.81/1.14/−1.15, tilt 0.50 | baseline repeats within 0.05 dB/band hours after the blind run |
| σ | per-bin 5-seat spread analysis | σ@2.7k=0.45, σ@8.2k=0.51, σ@358=1.10 | +1.14 @ 2.7k and −1.15 @ 8.2k exceed 2σ → real features; 358 Hz is seat noise → leave |
| r1 | per-driver plants | tweeter classed compression_horn | envelope data for everything downstream |
| null | polarity pair + delay landscape + in-phase landscape | §4, §5 above | ~2 minutes of audio settled all structural truth |
| r2/r3 | delay staging attempts | flow lessons | measure rounds reset the applied flag; verify needs an applied cycle |
| r4 | delay + machine refit, applied | **regressed**, auto-restored in seconds | the free fitter is the confound |
| r5/r6 | packet + hand blend-cut rounds | staged via prescriber gate | the packet embeds its own response_format — the tool teaches its driver |
| r7 | delay + EQ/trims held by prescriptions | clean single-variable candidate | prescription pins displace the trim runaway (receipt prints the displaced value) |
| r8 | r7 applied + 5-seat verify | **regressed** (−3.1 @ fc), auto-restored | see §5 mechanism |
| r9/r10 | delay 0, EQ held, deterministic region solver free | **grade 1.112 → 0.9035**, kept | the solver independently derived the 2655 Hz cut |
| r11/r12 | +0.8 dB @ 8216 Hz (single variable) | grade 0.93; tilt-removed RMS 0.18 → **0.067**, kept | both real features addressed; attempt loop at its floor |

Safety/honesty machinery scoreboard: the trim-runaway (auto-fitter re-solving the tweeter trim −2.8→−3.3 dB across identical hardware) was displaced by pins **six times**; the seat-verify rejected and auto-restored an applied candidate **twice**; the speaker never stayed in a wrong state.

## 7. Attribution findings (why the big filters are legitimate)

- **898 Hz, −7.27 dB, Q 1.56 (woofer):** sits on a real resonance. Narrow-band decay analysis of the banked woofer IR shows a slow-decay ridge spanning 700–1000 Hz (time-to-−20 dB up to ~48 ms at 800 Hz vs 6–10 ms just outside it, same room, same capture). A reflection would comb; an artifact wouldn't localize. EQ correctly flattens the steady-state magnitude; the stored-energy tail remains (EQ cannot remove it) — mechanical damping is the only deeper fix. Honest claim: *magnitude corrected, decay documented.*
- **~1 kHz small comb:** ≤1 ms path reflection (~34 cm — floor/stand/cabinet edge), inside any usable gate. A placement fact, not a tune fact. EQ forbidden.
- **Tweeter top-octave cuts (10.1 k −3.0, 15.6 k −4.0):** part CD-horn compensation shaping, part measured response; the 15.6 kHz filter is the least trustworthy in the chain (mic-cal + positioning tolerance at that frequency ≈ the correction size) — which is why the graded band up there carries ±2.5 dB tolerance and the trusted region ends at 16 kHz.

## 8. Methodology / runbook adaptations (for ANY speaker, not this one)

**Fastest-learning sequence for a fresh agent on an unknown speaker:**
1. **Entry family first** (multi-seat + per-bin σ). Every later claim is comparative, and σ decides which features are real *before* anything is sized. One arm walk = the campaign's honesty budget.
2. **Per-driver plants.**
3. **Polarity pair** (~30 s, two takes). Also a free end-to-end validation of mic → graph → emitter → capture.
4. **Delay landscape** (three takes). Steps 3+4 settle the structural truth in **under two minutes of audio** — and every response-space conclusion silently depends on them.
5. First candidate may free-fit. **Every subsequent change is a prescription-held single-variable round.**
6. **Decay read** (offline, free, from already-banked IRs) for any filter deeper than 3 dB — magnitude says where, decay says which tool.

**Beware notes (each earned on hardware this campaign):**
- EQ fitted on a misaligned structure verifies today and fights alignment forever. A smooth crossover region with an unmeasured zero delay is luck, not proof.
- Monotonic drift in a re-solved value across identical rounds (e.g. a trim walking −1.4 → −2.1 → −3.1) is the *fitter*, not the speaker. Pin what you have proven.
- The inverted-null landscape is EQ-insensitive; the in-phase landscape includes the EQ's opinion. **Their disagreement is the fingerprint of an EQ-encoded structure error.**
- A shallow null means nothing until the row's branch-gap ceiling is read — level mismatch caps depth mathematically.
- A measure round resets the applied flag; verify-after-measure needs an apply between. Runtime-only comparisons go through the small doors instead.
- One staged-prescription slot, consumed by the next round. Stage immediately before the round that should eat it.
- A previously staged walk silently overrides a newly commanded angle list. Check or restage, every time.
- On a speaker **with tuning history**, verify structure (polarity, delay) before building on any inherited EQ — inherited response work encodes old structure errors. (This is the whole story of this campaign in one sentence.)
- Grade with the grader. A home-rolled comparison in a lookalike analysis frame will disagree with the instrument and waste a round.
- Boosts spend maximum SPL and carry a width ceiling; a boost should never exceed the measured dip it fills. Cuts are cheap; prefer them.
- Know the driver topology before judging tilt: a CD horn's falling raw response *requires* large compensating EQ — "lots of tweeter EQ" is correct there, and an entry curve that looks broken may just be uncompensated.
- Tools should teach at the surface: the evidence packet embedding its own `response_format` (required fields, bounds, and *why*) let the driving LLM self-correct in one round-trip. Extend that pattern rather than growing the doc.

## 9. Public claim (kept deliberately narrow)

*Flat on its design axis, reflection-gated, 357 Hz – 16 kHz, measured through the same analysis path the grades read — with smooth directivity demonstrated (not assumed), polarity proven, the inter-driver time offset measured and its disposition explained, and a named mechanism for every place the response is not flat.* Not claimed: better than the factory voicing frame, flat in-room, or flat below the gate's floor.

## 10. Provenance

Every number above comes from banked measurement records (WAVs + per-take JSON + round receipts), campaign directories `captures/flat-campaign-2026-08-31/r0…r12`, reverse-null rows in `bundle/null_runs/`, and the two live config YAMLs; curves plotted verbatim from the pipeline's own banked products (reflection-gated, mic-calibrated, 1/24-octave smoothed). Final config: `active_speaker_baseline_candidate_49d3401a3708.yml`. The interactive companion pages: "Nine Decibels Dark" (blind run) and "Really Really Flat" (this campaign).
