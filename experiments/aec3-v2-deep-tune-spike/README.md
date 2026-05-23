# AEC3 v2.1 deep-tune spike (laptop)

**Status:** **DELIVERED a usable tuning** as of 2026-05-22 night.
`BEST_A` is the production-target AEC3 config; it crosses the wake
threshold on the previously-silent `whisper-music` cell and beats
AEC3-stock on every other failing music cell. Triple-stream plan
(raw + BEST_A + DTLN-256 OR-fused) is the next sprint — see
[`docs/HANDOFF-mic-quality-v2.md`](../../docs/HANDOFF-mic-quality-v2.md)
"Triple-stream architecture plan."

## What this is

Per `docs/HANDOFF-aec.md` section E ("Vendor newer libwebrtc as a
Meson subproject") + `docs/HANDOFF-mic-quality-v2.md`'s reference
to AEC3's deep `EchoCanceller3Config` knobs, this spike validated
the access path AND ran a proper tuning campaign:

1. Vendor `webrtc-audio-processing` v2.1 from PipeWire's upstream
   fork; build statically with `-fPIC`.
2. Write our own `EchoControlFactory` subclass that constructs
   `EchoCanceller3` with a custom `EchoCanceller3Config`.
3. Plug into `AudioProcessingBuilder::SetEchoControlFactory`.
4. Expose every tunable knob as a Python kwarg via the binding.
5. Run a structured single-variable sweep against the
   `reference-conditions/` 10-cell baseline.
6. Combine the winning knobs into `BEST_A`.

Files in this directory:
- [`binding.cpp`](binding.cpp) — pybind11 binding (rev 3); accepts a
  `py::dict` of knobs, defaults match webrtc-audio-processing v2.1
- [`run_offline.py`](run_offline.py) — process all 10 baseline cells
  through a given config, write `aec-v2*.wav` outputs
- [`sweep.py`](sweep.py) — the **single-variable sweep methodology**
  used to identify BEST_A. Re-run with new knobs to extend.
- [`forensic.py`](forensic.py) — per-stream audio quality metrics
  (pumping CV, HF tearing, crest factor)

## The BEST_A config (canonical)

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

## How to rebuild

Prereqs on laptop: Python 3.9+ venv with `pybind11`, `numpy`,
`openwakeword`, `onnxruntime`; macOS or Linux toolchain with
`c++`, `meson`, `ninja`; existing JTS `reference-conditions/`
corpus.

```sh
# 1) Clone + build v2.1 static
git clone --depth 1 --branch v2.1 \
    https://gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing.git \
    /tmp/webrtc-aec3-vendor
cd /tmp/webrtc-aec3-vendor
meson setup builddir \
    -Ddefault_library=static \
    -Dc_args=-fPIC -Dcpp_args=-fPIC \
    --prefix=/tmp/webrtc-2.1-install
meson compile -C builddir
# (skip `meson install` — internal headers aren't installed; we
#  read them from the source tree directly via -I flags)

# 2) Build the binding
cd <jts-repo>/experiments/aec3-v2-deep-tune-spike/
PYINC=$(python -c "import sysconfig; print(sysconfig.get_paths()['include'])")
PYEXT=$(python -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")
PYBIND11_INC=$(python -c "import pybind11; print(pybind11.get_include())")
SRC=/tmp/webrtc-aec3-vendor; BLD=/tmp/webrtc-aec3-vendor/builddir
c++ -O3 -fPIC -shared -std=c++17 \
    -D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST \
    -DWEBRTC_LIBRARY_IMPL -DWEBRTC_POSIX -DWEBRTC_MAC \
    -DWEBRTC_APM_DEBUG_DUMP=0 \
    -I"$PYINC" -I"$PYBIND11_INC" \
    -I"$BLD" -I"$SRC" -I"$BLD/webrtc" -I"$SRC/webrtc" \
    -I"$BLD/subprojects/abseil-cpp-20240722.0" \
    -I"$SRC/subprojects/abseil-cpp-20240722.0" \
    -o "_aec3_v2_spike${PYEXT}" \
    binding.cpp \
    "$BLD/webrtc/modules/audio_processing/libwebrtc-audio-processing-2.a" \
    "$BLD"/subprojects/abseil-cpp-20240722.0/libabsl_*.a \
    -framework CoreFoundation -framework Foundation \
    -undefined dynamic_lookup

# On Linux/Pi 5: drop -framework flags, drop -D_LIBCPP_HARDENING_MODE,
# drop -undefined dynamic_lookup. Add -lpthread.

# 3) Run the sweep (re-derives BEST_A or extends it)
python sweep.py

# 4) Re-run full 10-cell scoring with any candidate config
python run_offline.py
```

## What still hasn't been tuned

The sweep covered ~27 configs but the `EchoCanceller3Config` surface
is larger. Untouched knobs that *might* help further (in priority
order):

1. **`nearend_tuning.max_dec_factor_lf` paired with normal=0.05.**
   Single-variable test had yell 9→8 (mild regression). Worth a
   joint sweep — maybe pairs better than solo.
2. **`echo_audibility.audibility_threshold_hf`** (default 10). Single
   test was neutral but only at value=100. Try 50 / 200 / 1000.
3. **`comfort_noise.noise_floor_dbfs`** (default −96). Higher floor
   masks pump dips perceptually — sound quality, not detection rate.
4. **`subband_nearend_detection.subband1.{low,high}`** + enable
   subband-nearend detection. Default `{1,1}` ranges are no-op;
   could target 3-7 kHz speech band specifically.
5. **`ep_strength.default_len`, `nearend_len`** (reverb tail priors,
   defaults 0.83). Untested. Might matter for our 192ms room reverb.
6. **The WebRTC field-trial mechanism** (`field_trial::InitFieldTrialsFromString()`).
   ~50 AEC3 trials available, including `Aec3SuppressorTuningOverride`
   for whole-config overrides via string. Different mechanism than
   the C++ struct; might unlock different combinations.
7. **Per-cell custom configs.** whisper-music wants opposite settings
   from fast-music (more vs less suppression). Could plumb a "config
   selector" that swaps configs based on detected signal type. Complex
   and probably not worth the operational cost.

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

## Productionization sketch

When ready to move BEST_A to the Pi:

1. Replace `jasper_aec3/`'s build system with the v2.1 static-vendor
   pattern from this spike. Setup.py needs to clone + build v2.1
   as part of `pip install -e .`.
2. Promote `binding.cpp` from this directory to
   `jasper_aec3/src/aec3_binding.cpp`. Set BEST_A as constructor
   defaults.
3. Expose the new knobs as env vars in `jasper/cli/aec_bridge.py`
   (e.g., `JASPER_AEC_ERLE_MAX_L`, `JASPER_AEC_NORMAL_MAX_DEC_LF`).
4. `install.sh` adds `meson` + `ninja` to apt deps. Native build
   on Pi 5 takes ~3-5 min.
5. Cross-compile risk: the v2.1 static-vendor pattern isn't widely
   documented for Debian aarch64 builds. Budget ~half a day for
   build-environment troubleshooting on Pi 5.

Effort: ~1.5-2 days for full productionization.
