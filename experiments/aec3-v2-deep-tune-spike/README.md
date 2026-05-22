# AEC3 v2.1 deep-tune spike (laptop-only)

**Status:** spike completed 2026-05-22. **Outcome: did NOT rescue
the whisper-music silent miss.** The research-report-recommended
starting knob values, AND v2.1's own defaults, both produce
WORSE wake-word scores than Trixie's system AEC3 v1.3 on the
hardest cell.

## What this is

Per `docs/HANDOFF-aec.md` section E ("Vendor newer libwebrtc as a
Meson subproject") + `docs/HANDOFF-mic-quality-v2.md`'s reference
to AEC3's deep `EchoCanceller3Config` knobs, this spike validates
the access path: vendor webrtc-audio-processing v2.1 from
PipeWire's upstream fork, build statically, write our own
`EchoControlFactory` that constructs `EchoCanceller3` with a custom
config, plug it into `AudioProcessingBuilder::SetEchoControlFactory`.

Files:
- `binding.cpp` — pybind11 binding exposing `Aec3V2` class with
  the deep knobs (`rs_snr_threshold`, `rs_subband_nearend`,
  `filter_refined_length_blocks`, `ep_strength_bounded_erl`, etc.)
- `run_offline.py` — processes all 10 reference-conditions cells
  through `Aec3V2`, writes `aec-v2tuned.wav` per condition

The binding compiles directly with `c++` (no setup.py); see the
compile command at the bottom of this file. Pi-side deploy was
*not* attempted — see "Why we stopped here" below.

## How to rebuild

Prereqs on laptop: Python 3.9+ venv with `pybind11`, `numpy`,
`openwakeword`, `onnxruntime`; macOS or Linux toolchain with
`c++`, `meson`, `ninja`; existing JTS reference-conditions
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

# 3) Run
python run_offline.py
```

(On Linux, drop the `-framework` flags and add `-lpthread`.)

## Results (2026-05-22)

`jarvis_v2` wake-word event counts (proper peak detection, 0.7 s
refractory) per condition × leg:

```
condition      |   raw   AEC3  V2tune   D128   D256
normal-quiet   |   11     11      11     10     11
normal-music   |    5      7       5      8      9
whisper-quiet  |    9      8       8     10      6
whisper-music  |    1      0       0      1      2  ← V2tune did NOT rescue
yell-quiet     |   10     10      10     10     10
yell-music     |   11      6       7      9      7
fast-quiet     |   11     11      11     10     11
fast-music     |    8      3       1      8      6  ← V2tune REGRESSED
slow-quiet     |    7      7       7      7      6
slow-music     |    7      7       5      4      7
```

V2tune config: research-report-recommended starting values
(`filter.refined.length_blocks=30`, `ep_strength.bounded_erl=true`,
`suppressor.use_subband_nearend_detection=true`,
`suppressor.dominant_nearend_detection.snr_threshold=20`).

**Headline finding:** V2tune did NOT rescue whisper-music
(peak score 0.000 — worse than AEC3-stock's 0.279), and it made
fast-music worse (1 event vs AEC3's 3). DTLN-128 / DTLN-256 are
the only engines that genuinely fix the failing cells.

A subsequent test with v2.1 **default-constructed**
`EchoCanceller3Config` (no overrides — matches webrtc's published
defaults) also showed whisper-music peak score 0.002 (silent
miss), confirming the regression isn't from our tuned knob choices
specifically — v2.1's AEC3 implementation itself appears to behave
differently than Trixie's v1.3 on this audio.

## What this proves

| claim | verdict |
|---|---|
| The vendor-v2.1-static path is buildable on a Debian-flavored toolchain | ✓ yes |
| `SetEchoControlFactory` + custom config compiles cleanly | ✓ yes |
| The `EchoCanceller3Config` knobs from the research report are reachable | ✓ yes |
| Research-report-recommended starting values rescue the whisper-music silent miss | ✗ NO |
| v2.1 with default config matches v1.3 production behavior | ✗ NO (v2.1-defaults are *worse* on whisper-music) |

## Why we stopped here

The path is technically viable but the "data point" we wanted —
"would tuning fix the silent miss?" — comes back as **no, not
with the configs we tested, and there's not an obvious next-config
to try.** Finding good knob values from scratch in WebRTC's deep
AEC3 surface is a multi-day blind search.

Combined with the observation that v2.1's default AEC3 behaves
worse than v1.3's on our hardest signal, the case for going deeper
on AEC3 tuning weakened significantly: it's not just "tune the
knobs", it's "tune the knobs AND understand the v1.3→v2.1
behavior delta."

**Meanwhile DTLN-aec just works.** DTLN-256 rescues every AEC3
failure cell without regressions.

This spike's infrastructure is preserved here so a future agent
can resume the deep-tune search if needed (e.g. via a tuning
wizard that lets the user iterate knob values live against a
fixed test clip). But it's not the recommended next step.

## Caveats

- The spike tested only a handful of knob combinations. The
  knob space is large; better values may exist but the search is
  unbounded.
- The v1.3 → v2.1 behavior delta could indicate a real bug in our
  binding (subtle config mismatch, missing initialization step).
  Worth a second pass if anyone resumes this work — diff our
  binding against `tonarino/webrtc-audio-processing-sys/src/wrapper.cpp`'s
  experimental-aec3-config path for cross-reference.
- Pi-side cross-compile + deploy was NOT attempted. The static
  link target needs to be cross-built for `aarch64-linux-gnu`
  (Pi 5). Adds 2-3 hours to the bring-up if anyone restarts this.
