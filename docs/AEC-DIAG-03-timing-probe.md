# AEC-DIAG-03 Timing Probe

Date: 2026-06-18
Scope: diagnostic timing probe for outputd final-reference, chip-ref writer tee, legacy `jasper_capture`, and XVF3800 capture channels.
Constraint: diagnostic only. No production routing change.

## Summary

[`scripts/aec-probe-timing.py`](../scripts/aec-probe-timing.py) injects a
controlled chirp through `correction_substream`, captures one selected
reference tap and one selected XVF3800 capture channel, then writes:

- `results.json` - full run metadata, outputd state snapshots, metrics,
  warnings, and artifact paths.
- `results.csv` - spreadsheet-friendly one row per run/profile.
- `summary.md` - short human summary with the evidence limits repeated.
- `*-ref-*.wav`, `*-mic-ch*.wav`, `*-stimulus.wav` - short 16 kHz mono
  analysis WAVs plus the played 48 kHz stereo stimulus.

The probe is intentionally separate from
[`scripts/aec-probe-latency.sh`](../scripts/aec-probe-latency.sh). The
older script has probe-history baggage: before PR 801 it measured
`pcm.jasper_capture`, which is a pre-DSP fan-in/Camilla input tap, not
production outputd timing. The new script forces the caller to name the
reference source with `--ref-source` and writes that source into JSON,
CSV, and Markdown.

## Reference Sources

Supported `--ref-source` values:

| Source | Captures | Sample shape | Timing caveat |
|---|---|---|---|
| `outputd_udp` | outputd's UDP final speaker-reference monitor on `127.0.0.1:9891` | 48 kHz S16_LE stereo, downmixed/decimated to 16 kHz mono by the probe | This is outputd's final electrical reference, not the XVF USB-IN chip-reference PCM. It does not prove chip-ref writer or chip USB-IN timing. |
| `chip_ref_tee` | outputd's optional chip-ref writer tee at `/run/jasper-outputd/aec-timing-probe-chip-ref.s16le` | 16 kHz S16_LE dual mono, downmixed to mono by the probe | This is writer-side sample content. It does not timestamp XVF internal USB-IN consumption. |
| `jasper_capture` | legacy pre-DSP `pcm.jasper_capture` path via the `jasper_ref` ALSA diagnostic wrapper by default | 48 kHz S16_LE stereo, downmixed/decimated to 16 kHz mono by the probe | Historical comparison only. It must not be confused with outputd final timing. |

`chip_ref_tee` uses the outputd tee described in
[`docs/AEC-DIAG-02-observability.md`](AEC-DIAG-02-observability.md).
The script enables it with a temporary `/run/systemd` drop-in that loads
`/run/jasper-outputd-aec-timing-probe.env`. The env file is written mode
`0600` and removed during restore. It does not edit persistent
`/var/lib/jasper/*.env` files.

## Mic Channels

The script defaults to `--mic-channel 2` because raw mic0 is the best
available acoustic timing channel.

| Channel | Label |
|---:|---|
| `0` | `ch0 = conference/beam in chip-AEC mode` |
| `1` | `ch1 = ASR beam in chip-AEC mode` |
| `2` | `ch2 = raw mic0, preferred for acoustic timing` |

Processed chip beams (`ch0`/`ch1`) are still useful, but a low-confidence
or shifted peak there may reflect chip suppression/beam processing rather
than the raw speaker-to-mic acoustic path.

## Profiles

The probe can temporarily retune outputd's period/buffer shape for repeated
profiles. This is a diagnostic retune only; production routing is unchanged.

The probe holds `JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=4096` and varies the
outputd period plus DAC buffer. Built-in profile names:

| Name | `JASPER_OUTPUTD_PERIOD_FRAMES` | `JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES` | `JASPER_OUTPUTD_DAC_BUFFER_FRAMES` |
|---|---:|---:|---:|
| `default` | 1024 | 4096 | 3072 |
| `1024/2048` | 1024 | 4096 | 2048 |
| `512/1024` | 512 | 4096 | 1024 |

Use `--profiles all` to run those three in sequence. A custom
`PERIOD/BUFFER` value is accepted only when `BUFFER >= 2 x PERIOD` and the
pinned 4096-frame content buffer is also at least `2 x PERIOD`.

## Usage

One short acoustic timing smoke test against outputd UDP:

```sh
python3 scripts/aec-probe-timing.py \
  --ref-source outputd_udp \
  --mic-channel 2 \
  --duration 2
```

Compare chip-ref writer tee bytes to raw acoustic mic:

```sh
python3 scripts/aec-probe-timing.py \
  --ref-source chip_ref_tee \
  --mic-channel 2 \
  --duration 2
```

Run the three standard outputd period/buffer profiles:

```sh
python3 scripts/aec-probe-timing.py \
  --ref-source outputd_udp \
  --mic-channel 2 \
  --profiles all \
  --runs 2
```

Historical pre-DSP comparison, explicitly labeled:

```sh
python3 scripts/aec-probe-timing.py \
  --ref-source jasper_capture \
  --jasper-capture-pcm jasper_ref \
  --mic-channel 2
```

The laptop-side wrapper SSHes to `${PI_USER:-pi}@${PI_HOST:-jts.local}`,
runs the hardware worker with `/opt/jasper/.venv/bin/python`, pulls the
remote artifact directory into `logs/` via `sudo tar`, and leaves the
remote `/tmp` artifact in place for manual inspection. Remote artifacts
stay under a root-owned, mode `0700` directory because the WAVs include
short mic captures.

## Side Effects And Restore

During a run, the script:

- Stops `shairport-sync.service`, `jasper-voice.service`, and
  `jasper-aec-bridge.service` if they were active. The bridge owns the
  XVF capture endpoint, so direct ALSA capture requires this.
- Applies a temporary outputd drop-in under `/run/systemd/system` that
  points to `/run/jasper-outputd-aec-timing-probe.env`; that root-only env
  file carries the selected period/buffer profile and, for `chip_ref_tee`,
  the tee path.
- Restarts `jasper-outputd.service` once per profile.
- Restores outputd by removing the drop-in, daemon-reloading, and
  restarting outputd if it was active at entry.
- Restarts only the services that were active at entry.
- Handles `SIGINT`, `SIGTERM`, and `SIGHUP` in the Pi worker so an
  interrupted SSH session still runs the restore path before exiting.

It does not edit production routing, CamillaDSP configuration,
`/etc/asound.conf`, or persistent JTS env files.

## Metrics

Every row reports:

- Lag in samples and milliseconds, with positive lag meaning the mic
  channel trails the selected reference.
- Correlation confidence: `high`, `medium`, or `low`.
- Normalized correlation peak and peak-to-median ratio.
- RMS, dBFS RMS, peak, clipping count/percent, sample rate, sample count,
  and duration for both reference and mic captures.
- Outputd `/state` snapshots before and after the run, including DAC delay
  and chip-ref writer observability fields when the deployed outputd build
  exposes them.
- Warnings that follow the chosen reference source and mic channel.

## What It Proves

The probe proves that, for a controlled chirp routed through the normal
output path, a selected reference tap and a selected XVF capture channel
have an observed correlation peak at a measured lag.

That is enough to answer questions like:

- Does `outputd_udp` vs raw mic show a stable speaker-to-mic delay?
- Does `chip_ref_tee` contain the expected stimulus with usable RMS and
  no clipping?
- Do outputd period/buffer profiles shift the observed correlation lag?
- Do chip ASR beams respond very differently from raw mic0 under the same
  stimulus?

## What It Does Not Prove

The probe does not directly timestamp:

- Physical speaker diaphragm motion.
- XVF3800 USB-IN hardware consumption.
- XVF3800 internal AEC alignment.
- The exact ordering of outputd DAC write, UDP send, chip-ref ALSA write,
  and chip capture at sub-period precision.

For that reason, an `outputd_udp` to mic result must not be treated as a
chip-ref input result. A `chip_ref_tee` to mic result narrows the question
to writer-side reference content/timing, but it still does not prove the
chip consumed those samples at that instant.

## First Results

Initial hardware smoke test on `jts.local`, 2026-06-18:

```sh
python3 scripts/aec-probe-timing.py \
  --pi-host jts.local \
  --ref-source outputd_udp \
  --mic-channel 2 \
  --duration 1.2 \
  --search-ms 300 \
  --profiles default \
  --runs 1 \
  --output-dir logs
```

Artifact path:
`logs/aec-timing-probe-20260618T152700Z/`

| Run | Profile | Reference | Mic | Lag | Confidence | Peak | Ref RMS | Mic RMS | Clipping |
|---|---|---|---|---:|---|---:|---:|---:|---|
| `default-run1` | `default` (`1024/3072`) | `outputd_udp` | `ch2` raw mic0 | 84.06 ms / 1345 samples | high | 0.545 normalized / 123.8x peak-to-median | 423.4 (-37.8 dBFS) | 3622.3 (-19.1 dBFS) | 0.0% ref, 0.0% mic |

The run wrote `results.json`, `results.csv`, `summary.md`,
`default-run1-ref-outputd_udp.wav`, `default-run1-mic-ch2.wav`, and
`default-run1-stimulus.wav`. After the probe, `jasper-outputd`,
`jasper-aec-bridge`, `jasper-voice`, and `shairport-sync` all reported
`active`.

Interpretation boundary: this first smoke proves the script can measure a
high-confidence 84.06 ms outputd-UDP-to-raw-mic correlation on the default
profile. It does **not** prove chip-ref USB-IN timing. The live outputd
build used for this smoke did not expose the AEC-DIAG-02
`chip_ref_writer`/`snd_pcm_delay` state fields in its `STATUS` payload, so
the JSON only contains the older outputd state keys for this first run.

Follow-up hardware smoke on `jts.local`, 2026-06-18, after the
review-fix pass:

```sh
python3 scripts/aec-probe-timing.py \
  --pi-host jts.local \
  --ref-source outputd_udp \
  --mic-channel 2 \
  --duration 1.0 \
  --search-ms 300 \
  --profiles 1024/2048 \
  --runs 1 \
  --output-dir logs
```

Artifact path:
`logs/aec-timing-probe-20260618T154655Z/`

| Run | Profile | Reference | Mic | Lag | Confidence | Peak | Ref RMS | Mic RMS | Clipping |
|---|---|---|---|---:|---|---:|---:|---:|---|
| `1024-2048-run1` | `1024/2048` | `outputd_udp` | `ch2` raw mic0 | 42.31 ms / 677 samples | high | 0.596 normalized / 198.5x peak-to-median | 461.4 | 3738.9 | 0.0% ref, 0.0% mic |

The same pass verified that the temporary profile reached outputd:
`journalctl` showed `content_period_frames=1024`,
`content_buffer_frames=4096`, and `dac_buffer_frames=2048`. Remote
artifacts stayed private after the laptop pull:
`/tmp/aec-timing-probe-20260618T154655Z` was `0700 root:root`, and
`results.json`, `summary.md`, and the mic WAV were `0600 root:root`.
After the probe, `jasper-outputd`, `jasper-aec-bridge`, `jasper-voice`,
and `shairport-sync` all reported `active`.

Attempted `chip_ref_tee` smoke on the same deployed build:

```sh
python3 scripts/aec-probe-timing.py \
  --pi-host jts.local \
  --ref-source chip_ref_tee \
  --mic-channel 2 \
  --duration 0.6 \
  --search-ms 300 \
  --profiles 512/1024 \
  --runs 1 \
  --output-dir logs
```

The script failed before stimulus playback because the deployed outputd
did not create `/run/jasper-outputd/aec-timing-probe-chip-ref.s16le` and
its `STATUS` payload only exposed the older keys
`backend`, `content`, `content_bridge`, `dac`, `dac_content`, `mix`,
`reference_outputs`, `sink_mode`, `tts`, `uptime_seconds`, and
`watchdog`. The restore path still completed: all four services were
`active`, the temporary `512/1024` outputd journal entry confirmed the
profile had been applied, and the failed-run remote artifact directory
was `0700 root:root`.

Validation still needed: rerun `chip_ref_tee` after deploying an outputd
build with `JASPER_OUTPUTD_CHIP_REF_TEE_PATH` support, run
`jasper_capture` if the legacy tap is still present, and collect a
successful `512/1024` timing result against a supported reference source.
