# AEC-DIAG-01 Baseline

> **Status: historical.** Snapshot from 2026-06-18 diagnosing a
> suspected chip-AEC regression during the move to an
> outputd-centered final-reference architecture. Preserved for
> primary-source archaeology — specific facts (installed build IDs,
> live-device state, "what's working" lists) will drift over time.
> Read this for the narrative, not for current state. Current
> operational truth lives in [HANDOFF-aec.md](HANDOFF-aec.md).

Date: 2026-06-18
Scope: diagnostic baseline for the suspected chip-AEC regression after moving toward an outputd-centered final-reference architecture.
Constraint: no production changes. Live device inspection was read-only.

## Evidence Boundaries

Repo evidence is from `origin/main` at `f6540a0b` (PR 801, "Repair AEC latency probe for outputd reference") plus the local worktree, whose tree matches the PR content. Live evidence is from `jts.local` between about 06:45 and 06:47 America/New_York on 2026-06-18. The live device reports installed build `91725b97`, installed at `2026-06-17T23:14:04-04:00`, so PR 801 is repo evidence but is not yet live-device evidence.

## Repo Evidence

### 1. Current Audio And Reference Architecture

Current stereo production path:

```text
renderer lanes
  -> private snd-aloop substreams 0..4
  -> jasper-fanin
  -> hw:Loopback,0,7
  -> pcm.jasper_capture / CamillaDSP input
  -> CamillaDSP
  -> pcm.outputd_content_playback / pcm.outputd_content_capture
  -> jasper-outputd
  -> pcm.outputd_dac
  -> DAC / amp / speaker
  -> room / XVF3800 microphones
```

Assistant audio in the packaged solo topology enters before CamillaDSP:

```text
TTS/cues/chirps
  -> /run/jasper-fanin/tts.sock
  -> jasper-fanin post-duck mix
  -> CamillaDSP
  -> outputd content lane
  -> outputd final DAC write
```

Reference production paths originate in `jasper-outputd`, not from fan-in:

```text
jasper-outputd final stereo period
  -> 48 kHz S16_LE stereo UDP monitor to 127.0.0.1:9891
  -> chip-ref downsampler: stereo L+R average, 48 kHz -> 16 kHz
  -> 16 kHz S16_LE dual-mono playback to JASPER_OUTPUTD_CHIP_REF_PCM
  -> XVF3800 USB-IN reference endpoint
```

Chip-AEC bridge path:

```text
XVF3800 16 kHz 6-channel capture
  -> chip beam ch0/ch1 when SHF_BYPASS=0 and fixed beams are enabled
  -> bridge primary UDP :9876 from chip_aec_150
  -> scoring UDP :9887 chip_aec_150 and :9888 chip_aec_210
```

The bridge also consumes outputd UDP in chip-AEC mode for reference queue/RMS observability, but the actual echo cancellation in `xvf_chip_aec` depends on the XVF3800 USB-IN reference PCM.

Active/composite repo behavior, not active on the live device:

```text
CamillaDSP 2 -> N active graph
  -> pcm.outputd_active_content_playback/capture on Loopback substream 5
  -> jasper-outputd reads N channels
  -> wide single DAC: outputd writes N-channel DAC and folds all lanes to mono, L=R, scale 1/N
  -> composite dual Apple: outputd writes two stereo child DACs and folds pairwise to stereo
```

### Reference Content And Folding

For stereo `single_alsa`, no active folding is involved: outputd publishes the final stereo period it writes to the DAC path. For wide active `single_alsa`, outputd folds all driven lanes to mono and duplicates L=R. For composite `dual_apple`, outputd folds child pairs to stereo. Chip-ref then downmixes stereo to 16 kHz dual mono before writing XVF USB-IN.

### Current Probe Contract After PR 801

PR 801 changes `scripts/aec-probe-latency.sh` to inject a chirp through `correction_substream`, capture outputd's final-reference UDP stream, capture a selected XVF3800 channel through direct ALSA (`hw:CARD=Array,DEV=0`), and cross-correlate mic vs ref. It defaults to `MIC_CHANNEL=0`, with `MIC_CHANNEL=1` for the other chip ASR beam and `MIC_CHANNEL=2` useful for older raw-channel comparisons. It stops `jasper-aec-bridge` because the bridge holds the XVF capture endpoint, then restores only services active at entry.

Tests added by PR 801 assert that the probe no longer uses PortAudio aliases or `jasper_capture`, that outputd UDP is the only reference source, and that chip beam channel selection is exposed.

### Current Validation Contract

`jasper-audio-hw-validate` remains passive. Tests and code show it samples outputd `/state` and bridge counters, reads selected XVF parameters, and may poll convergence, but it does not generate playback, open a capture loop, or directly measure fixed delay or long-window drift. Its `measured_drift_delay` check is `not_run` by design until an explicit probe is run.

## Live Evidence

### 2. Exact Current Live Configuration

Device identity:

```text
host: jts.local / hostname jts
kernel: Linux 6.12.75+rpt-rpi-2712 aarch64
live installed build: branch main, sha 91725b97
installed_at: 2026-06-17T23:14:04-04:00
```

Service state:

```text
jasper-outputd.service: active/running, pid 33785, started 2026-06-18 00:17:04 EDT
jasper-aec-bridge.service: active/running, pid 33902, started 2026-06-18 00:17:08 EDT
jasper-aec-init.service: active/exited
jasper-camilla.service: active/running
jasper-fanin.service: active/running
jasper-voice.service: active/running
```

AEC mode and env:

```text
JASPER_AEC_MODE=auto
JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec
JASPER_WAKE_LEG_RAW=0
JASPER_WAKE_LEG_DTLN=0
JASPER_WAKE_LEG_CHIP_AEC=1
JASPER_AEC_CHIP_AEC_ENABLED=1
JASPER_AEC_REF_SOURCE=outputd_udp
JASPER_AEC_OUTPUTD_REF_UDP_HOST=127.0.0.1
JASPER_AEC_OUTPUTD_REF_UDP_PORT=9891
JASPER_MIC_DEVICE=udp:9876
JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887
JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888
JASPER_MIC_DEVICE_RAW=''
JASPER_MIC_DEVICE_DTLN=''
```

Output hardware and outputd env:

```text
JASPER_AUDIO_DAC_ID=apple_usb_c_dongle
JASPER_AUDIO_DAC_CARD=A
selected_pcm=hw:CARD=A,DEV=0
physical_output_count=2
JASPER_OUTPUTD_BACKEND=alsa
JASPER_OUTPUTD_SINK=single_alsa
JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture
JASPER_OUTPUTD_DAC_PCM=outputd_dac
JASPER_OUTPUTD_ACTIVE_CHANNELS=''
JASPER_OUTPUTD_DUAL_DAC_A_PCM=''
JASPER_OUTPUTD_DUAL_DAC_B_PCM=''
JASPER_OUTPUTD_TTS_SOCKET=
JASPER_OUTPUTD_DAC_CONTENT_FIFO=
```

Outputd `/state`:

```text
backend=alsa
sink_mode=single_alsa
content.pcm=outputd_content_capture
content.period_frames=1024
content.buffer_frames=4096
content.xrun_count=19
dac.pcm=outputd_dac
dac.sample_rate=48000
dac.period_frames=1024
dac.buffer_frames=3072
dac.xrun_count=0
mix.reference_sequence=1097295 at direct status read
reference_outputs.speaker_reference_source=outputd_final_electrical
reference_outputs.speaker_reference_active=true
reference_outputs.speaker_reference_sample_rate=48000
reference_outputs.speaker_reference_channels=2
reference_outputs.udp_target=127.0.0.1:9891
reference_outputs.chip_ref_pcm=plughw:CARD=Array,DEV=0
reference_outputs.chip_ref_sample_rate=16000
reference_outputs.chip_ref_period_frames=320
reference_outputs.chip_ref_buffer_frames=1280
watchdog.last_progress_age_ms=2
```

Live ALSA hardware state:

```text
Loopback substream 5 active-content lane: closed
Loopback pcm0p/sub6: S16_LE, 2 ch, 48000 Hz, period_size=512, buffer_size=4096
Loopback pcm1c/sub6: S16_LE, 2 ch, 48000 Hz, period_size=1024, buffer_size=4096
Apple DAC A pcm0p: S16_LE, 2 ch, 48000 Hz, period_size=1024, buffer_size=3072
XVF Array pcm0p chip-ref playback: S16_LE, 2 ch, 16000 Hz, period_size=320, buffer_size=1280
XVF Array pcm0c capture: S16_LE, 6 ch, 16000 Hz, period_size=640, buffer_size=1920
Apple USB endpoint: 48 kHz stereo playback, SYNC endpoint
XVF USB playback endpoint: 16 kHz stereo, SYNC endpoint
XVF USB capture endpoint: 16 kHz 6-channel, SYNC endpoint
```

XVF3800 profile/register readback:

```text
VERSION=[2, 0, 8]
BLD_MSG=['ua-io16-6ch-sqr']
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
AEC_AECCONVERGED=[0]
AEC_NUM_MICS=[4]
AEC_NUM_FARENDS=[1]
```

Recent logs:

```text
outputd startup:
  event=outputd.alsa.opened ... channels=2 sample_rate=48000 content_period_frames=1024 content_buffer_frames=4096 dac_period_frames=1024 dac_buffer_frames=3072
  event=outputd.chip_ref.opened pcm=plughw:CARD=Array,DEV=0 sample_rate=16000 period_frames=320 buffer_frames=1280
  event=outputd.reference_udp.enabled target=127.0.0.1:9891
  event=outputd.ready backend=alsa sink_mode=single_alsa period_frames=1024

outputd runtime:
  content xrun count reached 19 by 2026-06-18 06:34:28 EDT
  DAC xrun count remained 0 in /state

bridge startup:
  starting: ref=udp:9891@48000 mic=Array@16000 ch=6->ch1 ... production_chip_aec=on chip_aec_primary=chip_aec_150
  outputd ref UDP opened: 127.0.0.1:9891 @ 48000 Hz stereo -> 16000 Hz mono (pre-AEC gain=+0.0 dB, HPF=125 Hz 2nd Butter)
  udp outputs: aec=127.0.0.1:9876 aec_source=chip_aec_150 raw0=127.0.0.1:9879 chip_aec_150=127.0.0.1:9887 chip_aec_210=127.0.0.1:9888

bridge recent idle window:
  chip_aec rms lines show ref=0, ref_q about 1..3, mic_q about 0..1, ref_starve=0

aec-init:
  applied chip-AEC profile with sys_delay=12 and verified writes for SHF_BYPASS, SYS_DELAY, ASR routing, fixed beams, emphasis, far gain, OP_L, and OP_R
```

Live bridge stats:

```text
frames_processed=1170482
packets_sent_by_leg.on=292620
packets_sent_by_leg.chip_aec_150=292620
packets_sent_by_leg.chip_aec_210=292620
queue_drops all zero
udp_send_drops_by_leg all zero
ref_starved_frames=1 since bridge start
frame_samples=320
out_frame_samples=1280
sample_rate_hz=16000
```

Latest audio validation artifact:

```text
status=warn
profile=xvf_chip_aec
recommendation=run_drift_delay_validation
dac_reference=pass
outputd_reference_health=pass
bridge_counter_window=pass
chip_profile_readback=pass
chip_convergence=not_observed
measured_drift_delay=not_run
notes include:
  No playback stimulus was generated.
  No capture loop was opened.
  Fixed delay and long-window drift still require an explicit playback/capture validation mode.
```

### 3. Current Failing Path Classification

Live evidence says the current path on `jts.local` is stereo single-sink, not active multi-channel and not composite.

Grounding:

```text
JASPER_OUTPUTD_SINK=single_alsa
JASPER_OUTPUTD_ACTIVE_CHANNELS=''
content.pcm=outputd_content_capture
dac.pcm=outputd_dac
selected output hardware=apple_usb_c_dongle, physical_output_count=2
Loopback active-content substream 5=closed
No dual_apple state block present
```

Inference: any current failure reproduced on this live device is a stereo single-sink outputd/chip-AEC failure. Active-lane folding and composite pair folding are repo-relevant risks, but they are not exercised by this live configuration.

## Timing Observability

### 4. Observable Timing Signals

Repo and live evidence currently expose:

- Outputd periods and buffers for content, DAC, and chip-ref PCM.
- Outputd `frames_read`, `frames_written`, `reference_sequence`, content xrun count, DAC xrun count, clipping count, and watchdog progress age.
- Outputd logs for ALSA open parameters, chip-ref open parameters, reference UDP enablement, priming, ready state, and xruns.
- ALSA `/proc/asound/*/hw_params` for currently opened content, DAC, XVF playback, and XVF capture streams.
- USB stream descriptors including current running status, momentary frequency, endpoint sync type, channels, format, and rates.
- Bridge startup config: reference source, mic device/rate/channels, primary chip-AEC leg, UDP output ports.
- Bridge RMS logs with ref RMS, beam RMS, `ref_q`, `mic_q`, `ref_starve`, and clip percentages.
- Bridge stats counters for frames processed, queue drops, UDP send drops, and ref-starved frames.
- XVF readbacks for volatile chip profile and `AEC_AECCONVERGED`.
- Audio validation passive windows for outputd reference health and bridge counter health.
- After PR 801 is deployed or an equivalent command is run, the latency probe can measure chirp peak lag between outputd UDP final reference and one selected XVF capture channel.

### 5. Missing Timing Signals

Current observability does not yet provide:

- A direct timestamped timeline tying outputd DAC writes, outputd UDP packets, chip-ref PCM writes, XVF USB-IN consumption, and XVF mic capture frames together.
- Chip-ref writer queue depth, frame counters, drop counters, write latency, or last-write age in outputd `/state`.
- A direct measurement of delay from outputd final electrical reference to XVF chip-AEC USB-IN processing.
- A direct measurement of delay from outputd final electrical reference to XVF capture under the currently running production chain.
- A long-window drift measurement between DAC playback, outputd UDP reference, chip-ref PCM, and XVF capture.
- A stimulus-bearing validation of `AEC_AECCONVERGED`; current convergence was only passively polled and read 0.
- A level/spectrum measurement of the chip USB-IN reference under controlled far-end stimulus.
- Evidence that the chip reference level after outputd downmix/decimation and `AUDIO_MGR_REF_GAIN=8` is in the chip's useful range.
- Active or composite reference folding measurements on the live device, because the live device is stereo.
- A measurement that correlates periodic content-capture xruns with bridge starvation, chip convergence loss, or wake failures.

## Probe History

### 6. What The Old Scripts Actually Measured

Pre-PR-801 `scripts/aec-probe-latency.sh` measured:

- A 200 ms log chirp injected through `correction_substream`.
- Reference captured from `pcm.jasper_capture`, which is the fan-in/Camilla input side, not outputd's final electrical reference.
- Mic captured through PortAudio/sounddevice device alias `Array`, channel 1.
- A 0-100 ms cross-correlation peak between that non-final digital reference and the selected mic channel.

It did not measure outputd UDP reference timing, chip-ref PCM timing, chip USB-IN arrival, outputd active folding, chip reference level, or long-window drift.

Current PR-801 `scripts/aec-probe-latency.sh` measures:

- The same `correction_substream` chirp path through fan-in, CamillaDSP, outputd, DAC, speaker, and mic.
- Outputd final-reference UDP at `127.0.0.1:9891`, downmixed/resampled to 16 kHz in the probe.
- Direct ALSA XVF3800 capture from `hw:CARD=Array,DEV=0`, selectable channel.
- A one-shot correlation peak between outputd final reference and the selected XVF channel.

It still does not measure long-window drift, chip-ref PCM write timing, chip USB-IN consumption timing, or chip reference level/folding correctness directly.

Current audio validation measured only passive stability/readback:

- Outputd reference state advanced without DAC xruns or clipping in the sampled 60 s window.
- Bridge counters advanced without drops or reference starvation in that sampled 60 s window.
- Chip profile readback matched expected settings.
- Drift and fixed delay were explicitly `not_run`.

Historical chip-AEC experiment scripts measured direct-source fanout/harness behavior and showed the old `plug:jasper_capture` drift issue was a harness artifact. Those results are useful context, but they are not a direct measurement of the current production outputd-centered chain.

## Inference

### 7. Hypothesis Table

| Rank | Hypothesis | Evidence For | Evidence Against Or Limit | Next Measurement |
|---:|---|---|---|---|
| 1 | Measurement gap is masking the real failure mode | Live validation explicitly says `measured_drift_delay=not_run`; live build does not include PR 801; old probe used `jasper_capture`; passive logs are mostly idle `ref=0` | Passive outputd and bridge counters are not currently failing in the 60 s validation window | Deploy/use PR-801-equivalent probe and run controlled delay/drift measurements without treating passive validation as timing proof |
| 2 | Actual timing/delay/drift between DAC, chip-ref, and mic is wrong | Chip-AEC depends on DAC output, XVF USB-IN reference, and mic capture staying aligned; outputd architecture changed the production reference point; live content path has periodic content xruns | Live DAC endpoint and XVF endpoints both report SYNC; DAC xrun count is 0; no current ref starvation in bridge logs | Measure outputd UDP ref vs mic lag repeatedly over time; separately instrument chip-ref write timing and drift under stimulus |
| 3 | Chip-ref format or level is wrong | Chip sees only one far end; outputd sends 16 kHz S16_LE dual mono, but live bridge idle ref RMS is 0 and no stimulus-level evidence was collected; `AUDIO_MGR_REF_GAIN=8` may be significant | Live ALSA format/rate/channels/period/buffer match the intended chip endpoint; XVF profile readback matches; UAC2 PCM volume was set to unity | Under controlled far-end audio, measure outputd UDP RMS, chip-ref write samples, chip convergence, and residual beam behavior |
| 4 | Reference content/folding is wrong | Active/composite code folds references; active folding is a plausible regression area after outputd-centered work | Live failing path is stereo, `ACTIVE_CHANNELS=''`, active lane closed, no composite; stereo path should not invoke active folding | If failure is also seen on active hardware, capture live active config and verify folded reference against driven lanes with stimulus |

### 8. Unknowns To Measure Next

- Exact outputd-final-reference-to-XVF-capture delay on this live path using the PR-801 probe or equivalent.
- Repeatability of that delay across `MIC_CHANNEL=0`, `MIC_CHANNEL=1`, and a raw mic channel such as `MIC_CHANNEL=2`.
- Long-window drift between outputd final reference and XVF capture over at least 5, 15, and 30 minutes.
- Chip-ref PCM writer health in outputd: frames written, queue depth, drops, write latency, and last-write age.
- Whether chip-ref PCM data has the expected RMS, crest, spectrum, and polarity under controlled far-end content.
- Whether `AEC_AECCONVERGED` reaches 1 under meaningful far-end stimulus with current `AUDIO_MGR_SYS_DELAY=12`.
- Whether changing only measured delay compensation would move the chip into convergence without changing reference content or level.
- Whether the periodic outputd content xruns correlate with any wake/AEC failure windows.
- Whether the live device must be updated past build `91725b97` before trusting PR-801 probe behavior on-device.
- Whether the same regression reproduces on an active multi-channel or composite path. If yes, collect that device's `sink_mode`, `ACTIVE_CHANNELS`, content lane, DAC profile, folded-reference behavior, and chip-ref level separately.
