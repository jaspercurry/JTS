# AEC-DIAG-02 Outputd Observability

Date: 2026-06-18
Scope: additive diagnostics for `jasper-outputd` DAC presentation timing and chip-reference writer timing.
Constraint: no production reference retiming, no delay line, no alternate production reference path.

## Summary

This patch extends outputd `STATUS` / `/state.outputd` with timing and health fields for the final DAC writer and the XVF3800 chip-reference writer. The fields are passive observations of the existing outputd-owned path:

```text
final outputd period
  -> DAC ALSA write
  -> outputd reference UDP
  -> chip-ref downsampler
  -> bounded chip-ref writer queue
  -> XVF USB-IN ALSA write
```

The patch does not change the samples written to the DAC, the samples sent over UDP, the chip-reference downmix/downsampling algorithm, ALSA period/buffer settings, or the order in which production writes happen.

## New `/state` Fields

### `dac`

`dac.snd_pcm_delay_frames`

- Unit: frames at `dac.sample_rate`.
- Source: latest successful `snd_pcm_delay()` call on the outputd DAC PCM. In composite/dual output mode this is the maximum child-DAC delay.
- Update cadence: once per outputd main loop after the DAC write, when ALSA reports delay successfully.
- Null behavior: `null` until a successful delay sample exists; stale if later delay reads fail.
- Diagnostic use: estimates queued DAC presentation depth. If this is stable while chip-ref timing drifts or stalls, the final DAC owner is probably not the timing source.

`dac.snd_pcm_delay_ms`

- Unit: milliseconds, computed from `snd_pcm_delay_frames / dac.sample_rate`.
- Update cadence/null behavior: same as `dac.snd_pcm_delay_frames`.
- Diagnostic use: easier wall-clock comparison against chip-ref delay and measured mic/ref latency.

`dac.snd_pcm_delay_sample_age_ms`

- Unit: milliseconds since the delay sample was collected.
- Update cadence: refreshed with each successful DAC delay read.
- Diagnostic use: distinguishes a current hardware delay value from a stale one caused by ALSA delay-read failures.

### `reference_outputs.chip_ref_writer`

`enabled`

- Unit: boolean.
- Source: true when `JASPER_OUTPUTD_CHIP_REF_PCM` is configured.
- Update cadence: fixed at process start.
- Diagnostic use: confirms the chip-reference writer is part of this outputd runtime.

`queue_depth_periods`

- Unit: queued chip-ref packets, where each packet is one outputd reference publish after 48 kHz to 16 kHz downsampling. Packet frame counts may vary slightly when the 48 kHz period is not an integer multiple of 16 kHz output frames.
- Source: atomic estimate around the existing bounded `sync_channel`.
- Update cadence: increments before a successful `try_send`, decrements when the chip-ref writer dequeues.
- Diagnostic use: separates outputd UDP publication from chip-ref writer backlog. A rising value means outputd is still publishing reference periods faster than the chip-ref writer is consuming them.

`queued_frames`

- Unit: 16 kHz chip-ref frames currently estimated in the queue.
- Source/update cadence: same queue accounting as `queue_depth_periods`, but frame-based.
- Diagnostic use: converts queue depth to a rough chip-ref delay estimate without assuming every packet equals `chip_ref_period_frames`.

`frames_written`

- Unit: 16 kHz chip-ref frames accepted by ALSA write calls, including startup priming silence.
- Source: summed successful `writei` return counts in the chip-ref writer.
- Update cadence: after each chip-ref ALSA write attempt.
- Diagnostic use: compare with `mix.reference_sequence` movement and UDP-side bridge reference counters. If UDP reference advances but this counter stalls, the problem is in the chip-ref writer/PCM path rather than outputd reference generation.

`snd_pcm_delay_frames`

- Unit: frames at `reference_outputs.chip_ref_sample_rate`.
- Source: latest successful `snd_pcm_delay()` call on the chip-ref playback PCM after a chip-ref write.
- Update cadence: after each successful delay read following a chip-ref write.
- Null behavior: `null` until available; stale if later delay reads fail.
- Diagnostic use: shows the XVF USB-IN playback queue depth as seen by ALSA. Compare against DAC delay to see whether the chip reference path is consistently ahead/behind the speaker path.

`snd_pcm_delay_ms`

- Unit: milliseconds, computed from chip-ref delay frames and `chip_ref_sample_rate`.
- Update cadence/null behavior: same as `snd_pcm_delay_frames`.
- Diagnostic use: direct timing comparison to `dac.snd_pcm_delay_ms`.

`snd_pcm_delay_sample_age_ms`

- Unit: milliseconds since the chip-ref delay sample was collected.
- Update cadence: refreshed with successful chip-ref delay reads.
- Diagnostic use: identifies stale chip-ref delay data.

`write_underrun_count`

- Unit: count.
- Source: chip-ref ALSA `writei` errors with `EPIPE`.
- Update cadence: when the writer observes an underrun.
- Diagnostic use: chip-ref-only underruns indicate the XVF reference endpoint is failing independently of outputd UDP publication.

`write_xrun_count`

- Unit: count.
- Source: chip-ref ALSA `writei` recoverable errors with `EPIPE` or `ESTRPIPE`.
- Update cadence: when the writer observes an underrun/suspend.
- Diagnostic use: broader chip-ref playback discontinuity counter.

`write_recovery_count`

- Unit: count.
- Source: calls to ALSA recovery for chip-ref write underrun/suspend.
- Update cadence: when recovery is attempted.
- Diagnostic use: shows whether observed chip-ref write faults were recoverable without restarting outputd.

`write_error_count`

- Unit: count.
- Source: chip-ref write attempts that returned an error after the writer's retry/recovery path.
- Update cadence: after failed chip-ref write attempts.
- Diagnostic use: non-fatal writer failure visibility. A rising value with a running outputd process points at chip-ref PCM trouble rather than restart-loop behavior.

`dropped_periods_due_to_full_queue`

- Unit: outputd reference periods.
- Source: bounded chip-ref queue `try_send` failures with `Full`.
- Update cadence: when outputd publishes a downsampled chip-ref packet but the writer queue is full.
- Diagnostic use: distinguishes "reference was generated but chip-ref writer could not keep up" from "reference was not generated."

`dropped_periods_due_to_disconnected_writer`

- Unit: outputd reference periods.
- Source: bounded chip-ref queue `try_send` failures with `Disconnected`.
- Update cadence: when the writer thread has exited and outputd drops a would-be chip-ref packet.
- Diagnostic use: makes writer death visible without making outputd's main DAC loop crash.

`last_write_age_ms`

- Unit: milliseconds since any frames were accepted by the chip-ref ALSA writer.
- Update cadence: after chip-ref write calls that write at least one frame.
- Null behavior: `null` before the first successful write.
- Diagnostic use: if outputd and UDP reference are active but this age grows, chip-ref writing has stalled.

`last_enqueued_reference_sequence`

- Unit: outputd reference sequence number.
- Source: sequence associated with the last downsampled chip-ref packet accepted into the writer queue.
- Update cadence: each successful chip-ref queue enqueue.
- Diagnostic use: anchors chip-ref queue admission to outputd reference publication.

`last_written_reference_sequence`

- Unit: outputd reference sequence number.
- Source: sequence associated with the last dequeued chip-ref packet whose frames were accepted by ALSA.
- Update cadence: after chip-ref writes that write frames from a queued reference packet.
- Null behavior: `null` until the first queued chip-ref packet is written. Startup priming silence does not set this field.
- Diagnostic use: anchors chip-ref ALSA writes to outputd reference publication.

`reference_sequence_lag`

- Unit: outputd reference periods.
- Source: `mix.reference_sequence - last_written_reference_sequence`, saturating at zero.
- Update cadence: every state snapshot from the latest stored counters.
- Diagnostic use: estimates how many outputd reference publishes the chip-ref writer is behind. If this grows while UDP consumers see current packets, the lag is chip-ref-specific.

`diagnostic_tee_path`

- Unit: path string or `null`.
- Source: `JASPER_OUTPUTD_CHIP_REF_TEE_PATH`.
- Update cadence: fixed at process start.
- Diagnostic use: confirms whether the optional raw sample tee was requested.

`diagnostic_tee_active`

- Unit: boolean.
- Source: true only while the optional diagnostic tee file is open in the chip-ref writer process.
- Update cadence: set true after a successful tee open; set false after a tee open failure or write failure.
- Diagnostic use: distinguishes "tee requested" from "tee actually recording." If `diagnostic_tee_path` is non-null but this is false, use `diagnostic_tee_open_error_count`, `diagnostic_tee_write_error_count`, and the outputd journal event to find the failure.

`diagnostic_tee_open_error_count`

- Unit: count.
- Source: failures to create/truncate the optional diagnostic tee file at outputd startup.
- Update cadence: when the configured tee path cannot be opened.
- Diagnostic use: makes systemd sandbox/path/permission failures visible in `/state` instead of relying only on the journal.

`diagnostic_tee_write_error_count`

- Unit: count.
- Source: write failures to the optional diagnostic tee file.
- Update cadence: when a tee write fails. After the first write failure, outputd disables the tee for that process.
- Diagnostic use: prevents a broken diagnostic file from being mistaken for missing chip-ref samples.

## Optional Chip-Ref Tee

Set `JASPER_OUTPUTD_CHIP_REF_TEE_PATH=/run/jasper-outputd/chip-ref.s16le` or another path writable by the outputd systemd sandbox to write the exact 16 kHz S16_LE stereo dual-mono packets dequeued by the chip-ref writer. The packaged unit currently allows writes under `/run/jasper-outputd` and `/var/lib/jasper`; arbitrary home or source-tree paths are expected to fail under `ProtectSystem=full` / `ProtectHome=read-only`. The file is created/truncated at outputd start.

Important boundaries:

- Disabled by default.
- Diagnostic only; it is not read by outputd, the AEC bridge, or the chip.
- It does not replace UDP or XVF USB-IN reference paths.
- Tee open/write failures are non-fatal. An open failure increments `diagnostic_tee_open_error_count`; a write failure increments `diagnostic_tee_write_error_count` and disables the tee. `diagnostic_tee_active` is the `/state` truth for whether the tee is currently recording.
- The tee is attached to the chip-ref writer side, not the main DAC loop. Enabling it can add diagnostic file I/O to the chip-ref worker, so it should be used for controlled captures, not normal production.

Capture format:

```text
sample_rate: 16000 Hz by default
channels: 2
format: signed 16-bit little-endian PCM
content: dual mono chip-reference samples after outputd stereo downmix/downsampling
```

## Distinguishing UDP Timing From Chip-Ref Timing

Use `mix.reference_sequence` and bridge UDP reference counters to confirm outputd is publishing the final electrical reference. Then compare the chip-ref writer fields:

- UDP advances, `chip_ref_writer.frames_written` advances, queue stays near zero: outputd UDP and chip-ref writer are both keeping up. Remaining latency/drift likely lives after ALSA write, inside USB/chip processing, or in the mic path.
- UDP advances, `queue_depth_periods` or `reference_sequence_lag` grows: outputd reference generation is healthy, but the chip-ref writer is falling behind.
- UDP advances, `dropped_periods_due_to_full_queue` grows: chip-ref writer cannot consume quickly enough; reference content is being dropped before XVF USB-IN.
- UDP advances, `last_write_age_ms` grows or `frames_written` stalls: chip-ref writer/PCM path is stuck or has exited.
- DAC delay is stable but chip-ref `snd_pcm_delay_ms` jumps or goes stale: the final speaker path is likely stable while the XVF reference playback endpoint is not.
- Both DAC delay and chip-ref delay move together with no queue/drop growth: timing variation may be upstream of outputd or due to common scheduling/load, not a chip-ref-only bottleneck.

The fields do not measure XVF internal USB-IN consumption or mic capture delay directly. They narrow the gap between outputd reference publication and chip-ref ALSA presentation so the next active probe can decide whether to focus on UDP/reference generation, the chip-ref PCM writer, or chip internal/mic timing.
