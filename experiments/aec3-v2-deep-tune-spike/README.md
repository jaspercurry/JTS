# AEC3 v2.1 deep-tune spike (laptop)

**Historical record — 2026-05-22 tuning results.** Current build and BEST_A
defaults live in [`jasper_aec3/`](../../jasper_aec3/); runtime engines live in
[`jasper/aec_engines/`](../../jasper/aec_engines/).

## What this is

Vendoring newer libwebrtc as a Meson subproject to reach AEC3's deep
`EchoCanceller3Config` knobs, this spike validated the access path AND
ran a proper tuning campaign:

1. Vendor `webrtc-audio-processing` v2.1 from PipeWire's upstream
   fork; build statically with `-fPIC`.
2. Write our own `EchoControlFactory` subclass that constructs
   `EchoCanceller3` with a custom `EchoCanceller3Config`.
3. Plug into `AudioProcessingBuilder::SetEchoControlFactory`.
4. Expose every tunable knob as a Python kwarg via the binding.
5. Run a structured single-variable sweep against the
   `reference-conditions/` 10-cell baseline.
6. Combine the winning knobs into `BEST_A`.

The spike's code (`binding.cpp`, `run_offline.py`, `sweep.py`,
`forensic.py`) shipped its output into `jasper_aec3` and has been
deleted from the tree; it remains available in git history. What each
did:
- `binding.cpp` — pybind11 binding (rev 3); accepts a
  `py::dict` of knobs, defaults match webrtc-audio-processing v2.1
- `run_offline.py` — process all 10 baseline cells
  through a given config, write `aec-v2*.wav` outputs
- `sweep.py` — the **single-variable sweep methodology**
  used to identify BEST_A.
- `forensic.py` — per-stream audio quality metrics
  (pumping CV, HF tearing, crest factor)

## The measured BEST_A config

```python
BEST_A = dict(
    # Top-level AudioProcessing::Config
    stream_delay_ms=40,
    ns_enabled=True, ns_level="low",
    agc1_enabled=True, agc1_target_dbfs=9, agc1_max_gain_db=18,

    # Filter (length matters — revert hurt all 3 failing cells)
    filter_refined_length_blocks=30,        # was 13 default

    # EpStrength
    ep_strength_bounded_erl=False,           # FIX from V2tune
                                             # (was True — silently
                                             # disables Transparent Mode)
    ep_strength_default_gain=0.3,            # was 1.0 default

    # Erle (THE KEY BEST_A discovery: lower caps than V2FIXED)
    erle_max_l=1.5,                          # NEW; V2FIXED=2.0, default=4.0
    erle_max_h=1.0,                          # NEW; V2FIXED=1.2, default=1.5
    erle_onset_detection=False,              # was True default

    # EchoAudibility
    use_stationarity_properties=True,        # was False default

    # Suppressor — normal tuning (echo-dominant mode)
    conservative_hf_suppression=True,        # the direct "less HF" knob
    normal_mask_hf_enr_transparent=0.3,      # LF parity (was 0.07 — 4× more aggressive than LF!)
    normal_mask_hf_enr_suppress=0.4,         # LF parity (was 0.1)
    normal_mask_hf_emr_transparent=0.3,
    normal_max_dec_factor_lf=0.05,           # was 0.25 default (5× slower gain attack)
)
```

## Results — BEST_A on the 10-condition baseline

Wake-word event counts per cell, using jarvis_v2 with proper peak
detection + 0.7 s refractory (matching production WakeLoop).

```
condition      |  raw  AEC3stock  BEST_A  D256  | improvement vs AEC3-stock
normal-quiet   |  11      11        11     11   | tied
normal-music   |   5       7         7      9   | tied (D256 +2)
whisper-quiet  |   9       8         8      6   | tied
whisper-music  |   1     0/0.28    1/0.76  2/0.98 | ✓ FIRES (was silent miss)
yell-quiet     |  10      10        10     10   | tied
yell-music     |  11       6         9      7   | ✓ +3 events
fast-quiet     |  11      11        11     11   | tied
fast-music     |   8       3         4      6   | ✓ +1 event
slow-quiet     |   7       7         7      6   | tied
slow-music     |   7       7         6      7   | -1 event
```

Music-cells totals: AEC3-stock 23, BEST_A 27, D256 31. BEST_A is
+17% over AEC3-stock; D256 is +35%. **BEST_A's headline win is
firing whisper-music** (peak score 0.76 vs AEC3-stock's 0.28 —
crosses the 0.5 threshold).

## What the sweep campaign learned

A single-variable sweep from V2FIXED (the prior pass) ran ~27
configurations against the 4 music cells. Key findings:

**V2FIXED knobs that ACTUALLY helped (reverting hurt):**
- `filter.refined.length_blocks=30` — revert: fast 5→3, normal 7→5
- `erle.max_l=2.0, max_h=1.2` — revert: all failing cells worse
- `use_stationarity_properties=True` — revert: fast 5→3
- `normal_max_dec_factor_lf=0.05` — revert: yell 9→8

**V2FIXED knobs that DIDN'T matter (reverting was neutral):**
- `bounded_erl=False` — neutral (but keep, for safety: True silently
  disables WebRTC Transparent Mode)
- `erle.onset_detection=False` — neutral

**V2FIXED knobs that HURT some cells (mixed):**
- `default_gain=0.3` — hurt whisper peak score (0.13 vs 0.38 default).
  Keep for general use; might be the lever to flip per-cell.
- `conservative_hf_suppression=True` — hurt fast-music (5→6 with revert)
- `normal_mask_hf parity` — hurt whisper peak (0.13 vs 0.29 default)

**The two NEW winners (each fires whisper-music when added to V2FIXED):**
- `erle.max_l=1.5, max_h=1.0` → whisper 1 event @ 0.76 ← chosen for BEST_A
- `nearend_tuning.mask_hf parity (0.3/0.4)` → whisper 1 event @ 0.85
  (alternative path; regresses normal-music slightly)

**The two winners don't combine.** Adding BOTH to V2FIXED yielded
whisper 0/0.07 (cancellation effect). They appear to interact
inside AEC3's logic. BEST_A picks the first (more robust on other
cells).

Two other single-variable tests: `nearend_tuning.max_dec_factor_lf`
reduced yell events from 9 to 8; `echo_audibility.audibility_threshold_hf=100`
(default 10) was neutral.

## What didn't work (don't retry without new evidence)

- **Maximally loosened RS knobs** (`rs_snr_threshold=1.0, hold_duration=10,
  high_bands_max_gain=100.0`, AGC1 off): pumping unchanged, scores
  unchanged. The basic "less suppression" instinct doesn't fix
  pumping — pumping is intrinsic to AEC3's adaptive-filter +
  spectral-suppression interaction.
- **Disabling AGC1**: pumping unchanged. AGC1 isn't the cause.
- **`high_bands_suppression.max_gain_during_echo > 1.0`**: silently
  clamped to 1.0 by `Validate()`. The only way to raise this is the
  `WebRTC-Aec3SuppressorAntiHowlingGainOverride` field trial.
- **Combining both whisper-music winners** (erle lower + nearend
  mask_hf parity): cancels out. Pick one.
