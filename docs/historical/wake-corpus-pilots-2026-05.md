# Wake-corpus tuning pilots (2026-05) — historical

> **Status: historical.** Frozen record of the May 2026 offline pilots run on
> the wake-corpus recorder while the gold corpus was still being scoped:
> AEC3 variant sweeps, DTLN passes, the BEST_A ablation, and the chip-AEC lab
> result. Kept because each number cost a recording session and because these
> are the measurements that decided which legs are worth fusing. Every result
> is from its stated date, against the chain of that date, on **marginal
> far+music clips** — not a clean training/held-out split, and not a recall
> figure for any production claim. Current operational truth is
> [HANDOFF-wake-training-experiment.md](../HANDOFF-wake-training-experiment.md).

All hit counts are at wake threshold 0.5 on the named session's marginal
far+music clips.

## BEST_A ablation (2026-05-28, session `20260528T140258Z-35c4`)

21 clips, config hash `5782b4d229e8`. BEST_A compared against NS-off,
AGC1-off, and NS+AGC1-off.

- All four AEC3 variants: **16/21** individually.
- XVF raw: **17/21**. AEC-only union: **17/21**.
- AEC + XVF raw/raw0 union: **21/21**.

NS-off rescued some clips and regressed others, so it reads as a fusion
direction rather than a single-chain replacement.

## Edge-family fusion (2026-05-28, session `20260528T141727Z-28bf`)

46 clips, config hash `97980f2b1971`. BEST_A, Edge, Edge NS-off, Edge
NS+AGC1-off.

- Individually: **30/46, 32/46, 32/46, 33/46**. AEC-only union: **38/46**.
- Practical three-leg set: BEST_A + Edge + Edge NS-off (38/46).
- For two AEC3 legs, BEST_A + Edge NS-off was the level-stable pick; BEST_A +
  Edge NS+AGC1-off maximized the strict union but was quieter.

## DTLN-256 (2026-05-28, same session, using the captured reference)

`dtln_off` from XVF chip ch1, `dtln_raw0` from ch2, `dtln_usb_raw` from USB
raw:

- Existing XVF AEC3 sweep union **38/46**; `dtln_off` 29/46; `dtln_raw0`
  28/46; `dtln_usb_raw` 27/46.
- All DTLN projections unioned to **36/46**; XVF AEC3 + XVF DTLN **41/46**;
  everything + DTLN **42/46**.
- DTLN-only saves over every existing recorded leg: clips 17
  (`dtln_off=0.986`) and 28 (`dtln_raw0=0.761`).
- USB side: existing union 25/46, existing + USB DTLN **32/46**.
- **The level caveat that matters:** DTLN outputs were much quieter than the
  main AEC legs (median RMS ≈ −38.5 dBFS `dtln_off`, −40.7 `dtln_raw0`,
  −31.9 `dtln_usb_raw` vs ≈ −29 dBFS for the main AEC legs). Normalize levels
  or calibrate per-leg thresholds before treating DTLN as production fusion.
  A blind fixed make-up gain sweep did **not** materially improve wake rate.

An earlier pass on session `20260528T131605Z-9708` agreed in direction: XVF
AEC union 25/64 → 28/64 with XVF DTLN; USB existing 15/64 → 19/64 with USB
DTLN. Do not mix DTLN into AEC3 sweep sessions on the 1 GB Pi — the
neural-engine cost is real.

## DTLN noise-suppression (2026-05-28, offline, `breizhn/DTLN`)

DTLN-NS did **not** improve the main candidate legs: raw-input NS union
29/46, XVF AEC3 post-filter NS union 29/46 (vs the original AEC3 union
38/46), DTLN-aec→NS 34/46 (vs DTLN-aec 36/46). It added exactly one new save
(clip 25, `ns_dtln_raw0=0.883`), raising the everything+DTLN+NS union to
43/46. Kept as offline research, not live test-mode scope.

## Waveform fusion (2026-05-28, session `20260528T184424Z-d205`)

`scripts/_waveform_fusion_experiment.py` mixes AEC3 + DTLN waveforms across
delay/weight grids and checks whether any mix beats same-pair score fusion.
Best XVF `on + dtln` waveform mix hit **20/27** and added clip 15 over the
full original-leg union — but score/decision fusion remains the
production-shaped architecture. Research evidence, not a production path.

## Chip-AEC lab result (2026-05-29)

The positive result that made the recorder's chip-AEC comparison profile
worth building:

- **The old feeder path was the timing/drift problem.** A direct DAC +
  XVF3800 USB-IN fanout held about **1 ppm over 15 minutes**.
- Candidate leg for corpus design: category-7 ASR output, fixed gated
  **150°/210°** beams, `AEC_AECEMPHASISONOFF=2`, with **150° the standout
  beam**.

This is what the `chip_aec_150` / `chip_aec_210` legs were built around; they
later graduated from corpus-only to hardware-conditional production legs
inside the `xvf_chip_aec` input profile.

## AEC3 sweep-slot history

The recorder's three runtime-labeled AEC3 variant slots were retargeted
repeatedly through late May as each pass answered its question: HF
preservation (`aec3_hf_relaxed` / `aec3_hf_mask_upstream` /
`aec3_hf_wide_open`), then edge preservation under far+music
(`aec3_nearend_fast`, `aec3_slow_attack`), then the combined direction
(`aec3_edge_combo`), then DND isolation (`aec3_hf_slow_only`,
`aec3_gentle_dnd`). The sweep later moved to a USB-fed source with the XVF
`on` leg kept as the same-utterance AEC3 reference, comparing stream-delay
hints (`usb_webrtc` 40 ms, variants at 80 / 120 / 160 ms).

## Why none of this is the gold corpus

These pilots are analysis data, deliberately recorded on marginal clips to
discriminate between chain variants. They are **not** the clean
training/held-out split the training plan needs: conditions were not sampled
to the corpus design, several sessions predate the raw0 leg, and the clip
sets were chosen for being marginal. The plan's Session A / Session B
recording remained the real milestone.
