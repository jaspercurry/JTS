# Wake-corpus quality methodology — build record (2026-05 to 2026-07) — historical

> **Status: historical.** Frozen record of how the wake-corpus quality
> methodology evolved between May and July 2026: the phased implementation
> plan, the AEC3 sweep-slot retargeting sequence, the corpus metadata contract
> as it grew, and the one waveform-fusion result that was measured. Kept
> because the sweep-slot history is the only explanation for why old sessions'
> `aec3_variant_*` legs mean different things on different dates. **Current
> operational truth is
> [HANDOFF-wake-corpus-quality.md](../HANDOFF-wake-corpus-quality.md);**
> decisions are ADR-0135 and ADR-0136.

## Phased implementation plan (as written, 2026-05-27)

- **Phase 0 — methodology into fixtures.** Synthetic fixtures for hard
  clipping, soft clipping, isolated click, click burst, dropout, repeated
  samples, DC offset, AGC pumping, aliasing, processed-leg-only artifact, plus
  the pure-fricative and plosive negatives. Detector behaviour locked before
  the real corpus was touched. **Shipped** —
  `tests/test_analyze_wake_corpus_quality.py`.
- **Phase 1 — deterministic analyzer.** Laptop-side pass over
  `data/enrollment_positives/`, emitting JSON + CSV, Tier A plus selected
  Tier B, dependencies limited to stdlib WAV reading plus numpy/scipy.
  **Shipped 2026-06-07** — `scripts/analyze-wake-corpus-quality.sh`.
- **Phase 2 — cross-leg analysis and HTML review packages.** Grouping,
  alignment, event-coincidence tables, processed-minus-baseline deltas, then
  review packages with players and plots. **Cross-leg half shipped; the HTML
  review package was never built.**
- **Phase 3 — optional neural metrics.** SQUIM first, DNSMOS only with the
  short-clip repetition caveat printed in the output, NISQA/UTMOS only if they
  changed decisions in a listening review, all with pinned model versions and
  checksums. **Never built.**
- **Phase 4 — USB AGC characterization.** A controlled AGC-on/off experiment
  if the cheap USB mic exposed a real toggle, with thresholds tuned from paired
  data. **Never run** — the 2026-05-27 AGC-off pilot stayed a clue rather than
  proof, because music level and session context changed between legs.

## AEC3 sweep-slot retargeting sequence

`aec3_variant_1..3` are deliberately generic, stable slot names
(`jasper/aec_sweep.py`) because the hypothesis behind them changed roughly
weekly. What each slot *meant* is recorded per session in the sidecar's
`aec3_sweep_variants` and `aec3_sweep_config.hash`. The sequence:

- **2026-05-27 (v2):** corpus-only AEC3 sweep legs added to the leg-aware
  contract at all.
- **2026-05-27 (v3):** retargeted to the HF-preservation 2×2 — `hf_relaxed`,
  `hf_mask_upstream`, `hf_wide_open`.
- **2026-05-27 (v4):** retargeted to edge preservation under far+music —
  `hf_relaxed`, `nearend_fast`, `slow_attack`.
- **2026-05-27 (v5):** kept `hf_relaxed` + `slow_attack`, added the combined
  `edge_combo`.
- **2026-05-27 (v6):** retargeted to isolate dominant-near-end detection —
  `hf_slow_only`, `edge_combo`, `gentle_dnd`.
- **2026-05-28 (v7):** slots became runtime-configurable via
  `/var/lib/jasper/aec3_sweep_variants.json`, so labels and knobs could change
  without a full deploy while metadata recorded the exact config hash.
- **2026-05-28 (v8):** slots became source-aware. New recorder-created sweep
  sessions default to the **cheap USB mic** with `aec3_sweep_source` recorded;
  sessions predating that field were XVF-fed. The built-in USB pilot labels
  `usb_webrtc` as the 40 ms edge-combo delay-hint baseline and the three
  variant slots as 80/120/160 ms delay hints.

## Corpus metadata contract, as it grew

- **2026-05-29 (v10):** the chip-AEC comparison profile stopped using generic
  sweep slots for the fixed XVF hardware-AEC outputs, taking explicit
  `chip_aec_150` / `chip_aec_210` / `xvf_raw0_webrtc_aec3` / `xvf_raw0_dtln`
  leg names instead.
- **2026-06-01 (v12):** `metadata_schema_version=2` plus `audio_context` at
  session and clip level — production profile classification, AEC intent and
  runtime env, XVF3800 identity and firmware channel state, selected-leg
  details, outputd/DAC/reference env, optional validation-artifact status.
- **2026-06-02 (v13):** `ref` confirmed as part of the chip-AEC profile; cheap
  USB legs declared optional and not to be expected when no USB mic is
  attached.
- **2026-06-04/07-09 (v14, v18):** the canonical `capture_plan` at session and
  clip level, plus `capture_plan_id` and clip-start conformance — the layered
  graph of physical mic, native stream, source channel, transform, bridge
  requirements, wake/corpus role, resource load, expected UDP legs and
  mic/DAC/reference fingerprints.

Sessions predating any of these fields remain valid historical data. Quality
tooling displays the absence; it never fails a corpus over it.

## Waveform-fusion first result (2026-05-28)

Session `20260528T184424Z-d205`, 27 clips:

- Best XVF mix — `on + dtln`, RMS-matched, `dtln` delayed +10 ms, 50/50 —
  hit **20/27** as a single waveform and added **one** new clip over the
  original all-leg union.
- Best USB mix — `usb_webrtc + usb_dtln`, native levels, `usb_dtln` delayed
  +20 ms, 50/50 — hit **14/27** and added that *same* one clip.

Read at the time: promising research candidate, nowhere near displacing
score/decision fusion. ADR-0136 fixes the bar it would have to clear.

## Seed material

The methodology was assembled on 2026-05-27 from local research reports plus
the primary sources listed in the spine's reading list. Those reports were seed
material only; the repo-facing doc superseded them the day it landed.
