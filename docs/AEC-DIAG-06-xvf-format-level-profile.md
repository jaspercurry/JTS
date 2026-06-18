# AEC-DIAG-06 XVF Format, Level, and Profile

Date: 2026-06-18
Scope: rule in/out XVF3800 USB-IN reference format, channel, level, and
volatile chip profile issues on the current `jts.local` production speaker.
Constraint: no permanent tuning changes. Live probes were bounded and restored
services after capture.

## Evidence Boundary

Repo evidence is from the current worktree on 2026-06-18, including:

- `jasper/cli/aec_init.py`
- `jasper/cli/aec_bridge.py`
- `jasper/mics/xvf3800.py`
- `deploy/bin/jasper-aec-reconcile`
- `tests/test_aec_init.py`
- `tests/test_aec_reconcile.py`
- `tests/test_outputd_wiring.py`
- `rust/jasper-outputd/src/main.rs`
- `docs/HANDOFF-xvf3800.md`
- `docs/HANDOFF-aec.md`
- `docs/CHIP-AEC-EXPERIMENT.md`
- `docs/HANDOFF-speaker-output-reference.md`

Live evidence is from `jts.local` / `192.168.1.74` between about
15:13 and 15:17 America/New_York on 2026-06-18. The live runtime reports
installed build `91725b97`, installed at `2026-06-17T23:14:04-04:00`.

The live build does **not** yet expose the `reference_outputs.chip_ref_writer`
fields documented in `docs/AEC-DIAG-02-observability.md`; those counters remain
repo evidence until the outputd observability patch is deployed.

## 1. Known-Good Baseline

The current known-good chip-AEC production baseline is the Option-D profile
promoted after the 2026-05-29 lab result, not the older software-AEC fallback
that used `SHF_BYPASS=1`.

Expected chip-side profile:

```text
SHF_BYPASS=[0]
AUDIO_MGR_SYS_DELAY=[12]
AEC_ASROUTONOFF=[1]
AEC_ASROUTGAIN=[1.000]
AEC_FIXEDBEAMSONOFF=[1]
AEC_FIXEDBEAMSGATING=[1]
AEC_FIXEDBEAMSAZIMUTH_VALUES=[2.618, 3.665]   # 150/210 deg, in radians
AEC_FIXEDBEAMSELEVATION_VALUES=[0.000, 0.000]
AEC_AECEMPHASISONOFF=[2]
AEC_FAR_EXTGAIN=[0.000]
AUDIO_MGR_OP_L=[7, 0]
AUDIO_MGR_OP_R=[7, 1]
AEC_HPFONOFF=[2]                               # 125 Hz
AUDIO_MGR_REF_GAIN=[8.000]
```

Expected USB/reference shape:

```text
XVF USB-IN playback: 16 kHz, S16_LE, 2 channels, FL/FR
XVF USB capture:     16 kHz, S16_LE, 6 channels
Outputd ref source:  outputd_final_electrical, 48 kHz stereo UDP
Chip ref model:      outputd stereo average -> exact 48k/16k decimation -> dual mono
```

Repo test evidence:

- `tests/test_aec_init.py` pins the chip-AEC flag path to
  `SHF_BYPASS=0`, `AUDIO_MGR_SYS_DELAY`, `AEC_ASROUTONOFF=1`, fixed beams
  on/gated, and `OP_L/OP_R=[7,0]/[7,1]`.
- `tests/test_aec_reconcile.py` pins `auto` and `xvf_chip_aec` on 6-channel
  XVF firmware to `JASPER_AEC_CHIP_AEC_ENABLED=1`,
  `JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0`,
  `JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891`, and raw/DTLN cleared.
- `rust/jasper-outputd/src/main.rs`
  `chip_ref_downsampler_downmixes_and_decimates_exact_ratio` pins stereo
  downmix/decimation to dual-mono output.

## 2. Current Live Profile

Live state:

```text
host=jts / jts.local
build=91725b97
aec.profile=xvf_chip_aec
aec.bridge_active=true
voice.wake_legs=[on, chip_aec_150, chip_aec_210]
output_hardware.profile_id=apple_usb_c_dongle
outputd.sink_mode=single_alsa
outputd.dac.pcm=outputd_dac
outputd.dac.sample_rate=48000
outputd.reference_outputs.chip_ref_pcm=plughw:CARD=Array,DEV=0
outputd.reference_outputs.chip_ref_sample_rate=16000
outputd.reference_outputs.chip_ref_period_frames=320
outputd.reference_outputs.chip_ref_buffer_frames=1280
```

ALSA/USB descriptors:

```text
XVF playback:
  Status: Running
  Format: S16_LE
  Channels: 2
  Endpoint: 0x01 OUT (SYNC)
  Rates: 16000
  Channel map: FL FR

XVF capture:
  Status: Running
  Format: S16_LE
  Channels: 6
  Endpoint: 0x81 IN (SYNC)
  Rates: 16000
  Channel map: FL FR FC LFE RL RR

Apple DAC:
  Status: Running
  Format: S16_LE
  Channels: 2
  Endpoint: 0x02 OUT (SYNC)
  Rates: 48000
```

Mixer state:

```text
XVF PCM Playback Volume: 60,60 (0 dB)
XVF PCM Playback Switch: on,on
XVF Headset Capture Volume: 60,60,60,60,60,60 (0 dB)
XVF Headset Capture Switch: on,on,on,on,on,on
```

XVF readback:

```text
VERSION=[2, 0, 8]
BLD_MSG=['ua-io16-6ch-sqr']
BLD_REPO_HASH=['a1f70651e992d6f0bcff655b26925d33999b9c2d']
SHF_BYPASS=[0]
AUDIO_MGR_SYS_DELAY=[12]
AEC_ASROUTONOFF=[1]
AEC_ASROUTGAIN=[1.000]
AEC_FIXEDBEAMSONOFF=[1]
AEC_FIXEDBEAMSGATING=[1]
AEC_FIXEDBEAMSAZIMUTH_VALUES=[2.618, 3.665]
AEC_FIXEDBEAMSELEVATION_VALUES=[0.000, 0.000]
AEC_AECEMPHASISONOFF=[2]
AEC_FAR_EXTGAIN=[0.000]
AUDIO_MGR_OP_L=[7, 0]
AUDIO_MGR_OP_R=[7, 1]
AEC_HPFONOFF=[2]
AUDIO_MGR_REF_GAIN=[8.000]
AEC_AECCONVERGED=[1]
AEC_NUM_MICS=[4]
AEC_NUM_FARENDS=[1]
```

`AEC_AECSILENCELEVEL` was requested but is not exposed by the current
`jasper.xvf.xvf_host` command table, and no in-repo doc or test references
that parameter. Current status: not available through the JTS helper.

## 3. Reference Channel/Level Evidence

I added a reusable diagnostic:

```text
scripts/aec-probe-xvf-ref-level.sh
```

It briefly stops `jasper-aec-bridge` and `jasper-voice`, binds outputd's
reference UDP port, opens the XVF capture endpoint directly, plays a bounded
chirp through `correction_substream`, then restores services. It makes no XVF
profile writes and does not call `SAVE_CONFIGURATION` or `REBOOT`.

Live run:

```text
PI_HOST=192.168.1.74 CHIRP_GAIN=0.18 CAPTURE_SECONDS=2.2 \
  bash scripts/aec-probe-xvf-ref-level.sh
```

Reference results:

```text
reference_udp_48k:
  left:  rms=291.0 (-41.0 dBFS), peak=1048, clips=0
  right: rms=291.0 (-41.0 dBFS), peak=1048, clips=0
  left_right_rms_delta_db=0.00

chip_ref_model_16k_mono:
  rms=291.0 (-41.0 dBFS), peak=1048

chip_ref_after_AUDIO_MGR_REF_GAIN:
  linear_gain=8.000, estimated_rms=2327.9 (-23.0 dBFS)
```

XVF capture results from the same run:

```text
ch0 fixed 150 beam: rms=1813.1 (-25.1 dBFS), peak=14611, clips=0
ch1 fixed 210 beam: rms=1606.4 (-26.2 dBFS), peak=11665, clips=0
ch2 raw mic 0:      rms=3283.4 (-20.0 dBFS), peak=27174, clips=0
ch3 raw mic 1:      rms=3340.7 (-19.8 dBFS), peak=32629, clips=0
ch4 raw mic 2:      rms=3860.7 (-18.6 dBFS), peak=32760, clips=0
ch5 raw mic 3:      rms=3306.5 (-19.9 dBFS), peak=25116, clips=0
```

Interpretation:

- USB-IN format is legal: the live endpoint is exactly `S16_LE`, 2-channel,
  16 kHz, and outputd opens it at `period_frames=320`, `buffer_frames=1280`.
- The host-side reference is not clipped: outputd state reported
  `mix.clipped_samples=0`, the new diagnostic measured zero reference clips,
  and bridge logs under a separate noise stimulus reported `ref_clip=0.00%`.
- The reference is not too quiet after chip-side gain: the digital modeled
  16 kHz mono reference is `-41.0 dBFS`, but `AUDIO_MGR_REF_GAIN=8.0`
  raises the chip's internal reference estimate to about `-23.0 dBFS`, within
  about 2-4 dB of the fixed-beam RMS values in the diagnostic run.
- Left/right host delivery is safe: outputd's UDP reference was exactly
  matched left/right for the stimulus (`0.00 dB` delta), and the outputd chip
  downsampler writes dual mono. `docs/CHIP-AEC-EXPERIMENT.md` documents the
  XVF AEC reference channel as left/channel 0, and the live XVF reports one
  far-end (`AEC_NUM_FARENDS=1`); in the current dual-mono writer, a
  right-channel ignore/use difference cannot remove or corrupt the intended
  left/channel-0 reference.
- Raw mic channels were hotter than the fixed beams and one raw channel peaked
  near full scale during the chirp. That is a diagnostic stimulus-level caution,
  not reference clipping; future raw-channel probes should use a lower
  `CHIRP_GAIN` if raw mic headroom is the target.

Additional bridge-running noise check:

```text
15:16:44 chip_aec rms: ref=54, near=chip_aec_210:265,
         primary=chip_aec_150:275, ref_starve=0, ref_clip=0.00%
15:16:49 chip_aec rms: ref=51, near=chip_aec_210:218,
         primary=chip_aec_150:228, ref_starve=0, ref_clip=0.00%
AEC_AECCONVERGED=[1]
```

Additional one-shot chirp lag checks with `scripts/aec-probe-latency.sh`:

```text
ch0 fixed 150 beam: ref RMS=243, mic RMS=1758, lag=43.7 ms, peak/median=7.1x
ch1 fixed 210 beam: ref RMS=243, mic RMS=2651, lag=38.6 ms, peak/median=7.4x
ch2 raw mic 0:      ref RMS=316, mic RMS=3248, lag=68.5 ms, peak/median=88.6x
```

These lag probes are useful sanity checks that the speaker echo is measurable
on the XVF channels. They are not a complete long-window drift/delay gate.

## 4. Mismatches Found

No format, channel, level, or volatile-profile mismatch was found on the live
Apple-dongle speaker.

Observed gaps and caveats:

1. The live outputd build lacks the `chip_ref_writer` state fields documented
   in `AEC-DIAG-02`, so queue depth, chip-ref write delay, write errors, and
   reference-sequence lag cannot be confirmed from `/state` on this device yet.
2. `AEC_AECSILENCELEVEL` cannot currently be read through the JTS XVF helper.
   If that knob matters, add a narrow command-table entry from primary XMOS
   docs and hardware-validate it before using it in doctor/init logic.
3. Some older HANDOFF sections still discuss the software-AEC fallback
   baseline (`SHF_BYPASS=1`, `OP_L/OP_R=[8,0]`). That is correct for
   `xvf_software_aec3`, but it is not the chip-AEC production baseline. The
   current `xvf_chip_aec` baseline is the `SHF_BYPASS=0`, fixed-beam profile
   read back above.
4. The new diagnostic proves outputd sends matched left/right and models the
   chip-ref mono path, but it does not temporarily route XVF category 12
   ("amplified far-end + system delay") to USB capture. That more invasive
   mux probe remains possible, but today's evidence was sufficient without
   writing chip routing registers.

## 5. Recommended Production Values

Carry these into production for the current supported XVF chip-AEC profile:

```text
JASPER_AUDIO_INPUT_PROFILE=auto       # resolves to xvf_chip_aec on 6-ch XVF
JASPER_AEC_CHIP_AEC_ENABLED=1
JASPER_AEC_REF_SOURCE=outputd_udp
JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0
JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE=16000
JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES=320
JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES=1280

SHF_BYPASS=[0]
AUDIO_MGR_SYS_DELAY=[12]
AEC_ASROUTONOFF=[1]
AEC_ASROUTGAIN=[1.0]
AEC_FIXEDBEAMSONOFF=[1]
AEC_FIXEDBEAMSGATING=[1]
AEC_FIXEDBEAMSAZIMUTH_VALUES=[2.61799, 3.66519]
AEC_FIXEDBEAMSELEVATION_VALUES=[0.0, 0.0]
AEC_AECEMPHASISONOFF=[2]
AEC_FAR_EXTGAIN=[0.0]
AUDIO_MGR_REF_GAIN=[8.0]
AUDIO_MGR_OP_L=[7, 0]
AUDIO_MGR_OP_R=[7, 1]
AEC_HPFONOFF=[2]
```

Do not hard-code Apple-dongle assumptions into the architecture. Keep outputd
as the production owner of final speaker output and reference publication.
For every DAC/output profile, the safe decision procedure is:

1. Reconcile/config owns hardware-profile defaults and whether chip-AEC is
   supported, degraded, or requires calibration.
2. Outputd reports the final-output reference and timing health for the active
   sink shape.
3. A calibration/probe measures live reference-to-air-to-mic delay and drift
   before enabling chip-AEC on a new or uncertain DAC.
4. Only after dynamic timing is exhausted, carry a small per-profile residual
   `AUDIO_MGR_SYS_DELAY` trim into production.
5. If timing or reference constraints are not measurable or stable, fall back
   gracefully to software AEC3/direct mic and surface that in doctor/state.

This supports Apple USB-C dongle, HiFiBerry/DAC hats, DAC8x-style active
profiles, multiple Apple dongles, and future USB DACs without making chip-AEC
success depend on one DAC's folklore.

## 6. Tests To Guard This

Existing passing tests run in this investigation:

```text
/Users/jaspercurry/Code/JTS/.venv/bin/pytest -q \
  tests/test_aec_init.py \
  tests/test_aec_reconcile.py \
  tests/test_outputd_wiring.py

68 passed
```

Recommended additional guard coverage:

1. `tests/test_aec_init.py`: assert the entire chip-AEC profile, including
   `AEC_ASROUTGAIN`, `AEC_AECEMPHASISONOFF`, beam azimuth/elevation values,
   `AEC_FAR_EXTGAIN`, and `AUDIO_MGR_REF_GAIN` read/write handling if that
   becomes init-owned.
2. `tests/test_aec_reconcile.py`: add DAC-profile matrix expectations:
   chip-AEC supported, calibration-required, and degraded/fallback states,
   with outputd chip-ref env written only for supported/calibrated profiles.
3. Outputd Rust tests: keep the existing exact-ratio downsampler test and add
   a fixture that asserts chip-ref output is stereo dual-mono for unequal
   input L/R and cannot clip when outputd's folded reference is within range.
4. Doctor/audio validation: add checks that report:
   `chip_aec_supported`, `chip_aec_needs_calibration`, or `chip_aec_degraded`
   for the active DAC; include chip-ref writer health once the
   `AEC-DIAG-02` fields are live.
5. Hardware probe test/documentation: keep
   `scripts/aec-probe-xvf-ref-level.sh` as diagnostic-only and add a static
   convention test if needed to prevent it from calling persistent XVF commands
   (`SAVE_CONFIGURATION`, `REBOOT`) or writing production env files.
