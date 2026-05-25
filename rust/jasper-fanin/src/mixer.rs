//! ALSA fan-in mixer — the core work loop.
//!
//! Reads from N capture PCMs (one per renderer's snd-aloop substream
//! pair), sums sample-wise, writes the summed stream to one playback
//! PCM (the "summed music" substream that CamillaDSP + AEC bridge
//! dsnoop on).
//!
//! ## Pacing
//!
//! The OUTPUT PCM is the metronome. We open it in blocking mode;
//! `writei()` blocks until the kernel has room in the output ring
//! (which empties at the system sample rate). That's what gates the
//! work loop to the right cadence.
//!
//! INPUTS are opened in non-blocking mode. Each iteration we read
//! one period from each input. If a renderer isn't producing audio
//! right now (the substream's writer hasn't opened, or is paused),
//! the non-blocking read returns -EAGAIN and we substitute silence
//! for that input. If a renderer produces faster than we drain
//! (shouldn't happen at matched 48 kHz steady-state, but possible
//! during a burst), the input substream overruns; we `try_recover`
//! and treat the affected period as silence.
//!
//! ## Mix math
//!
//! Inputs are S16_LE interleaved stereo. We accumulate into an i32
//! scratch buffer (using `saturating_add`) so simultaneous full-scale
//! inputs don't wrap, then clamp back to i16 for the output. Matches
//! ALSA dmix's clip behavior — audio sounds identical to today during
//! the Tier 2A transition (saturating clipping is louder than scaled
//! averaging when sources are simultaneous, but mux normally enforces
//! single-active anyway, so simultaneous is the brief handover case
//! only).
//!
//! ## Per-frame discipline
//!
//! `step()` does one period's worth of work: read all inputs, sum,
//! write output. `run()` calls `heartbeat.bump_progress()` after
//! every successful `step()`, satisfying the JTS progress-sentinel
//! contract documented in `src/watchdog.rs`.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Arc;

use alsa::pcm::{Access, Format, HwParams, State, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};
use log::{info, warn};

use crate::config::Config;
use crate::watchdog::Heartbeat;
use crate::xrun_log::{XrunEvent, XrunSource};

/// Stereo. The CamillaDSP capture + AEC bridge tap both expect 2
/// channels (matches the dmix's declared shape). Not configurable.
pub const CHANNELS: u32 = 2;

/// PCM sample format. Matches the dmix's declared format and the
/// dsnoop slave's format. Changing this would cascade through the
/// asoundrc, CamillaDSP, and the AEC bridge — out of scope for the
/// daemon.
pub const FORMAT: Format = Format::S16LE;

pub struct Mixer {
    inputs: Vec<Input>,
    output: PCM,
    /// Per-period scratch: i32 sum buffer absorbs the
    /// saturating-add accumulation before clamping back to i16
    /// in the output buffer. Holds `period_frames * CHANNELS` samples.
    sum_buf: Vec<i32>,
    /// Per-period output buffer (i16 interleaved). Same length as
    /// sum_buf.
    output_buf: Vec<i16>,
    /// Cumulative output frames written since startup. Surfaced via
    /// the STATUS endpoint.
    pub frames_written: Arc<AtomicU64>,
    /// Cumulative output xrun events.
    pub output_xrun_count: Arc<AtomicU64>,
    /// Channel for forwarding xrun events to the off-thread log
    /// writer. `try_send` is non-blocking on an unbounded channel
    /// (std::sync::mpsc::Sender::send only fails when the receiver
    /// is dropped, which happens at shutdown). Keeps the work loop's
    /// hot path off of disk I/O — the writer thread is the one
    /// stuck on fdatasync.
    xrun_tx: Sender<XrunEvent>,
    period_frames: u32,
}

pub struct Input {
    pcm: PCM,
    pub label: String,
    pub pcm_name: String,
    /// Per-input read buffer (i16 interleaved stereo).
    read_buf: Vec<i16>,
    pub xrun_count: Arc<AtomicU64>,
    pub frames_read: Arc<AtomicU64>,
}

impl Mixer {
    /// Open all inputs (best-effort: log + skip individual failures)
    /// and the output (must succeed; fatal if not). `xrun_tx` is the
    /// non-blocking channel to the off-thread xrun log writer.
    pub fn new(
        config: &Config,
        xrun_tx: Sender<XrunEvent>,
    ) -> Result<Self> {
        let period_samples =
            (config.period_frames as usize) * (CHANNELS as usize);

        let mut inputs = Vec::with_capacity(config.input_pcms.len());
        for (label, pcm_name) in
            config.input_renderers.iter().zip(&config.input_pcms)
        {
            match open_input(pcm_name, label, config) {
                Ok(input) => {
                    info!(
                        "event=fanin.input.opened label={} pcm={} period_frames={} buffer_frames={}",
                        label,
                        pcm_name,
                        config.period_frames,
                        config.buffer_frames,
                    );
                    inputs.push(input);
                }
                Err(e) => {
                    // Best-effort: a renderer might not have its
                    // substream wired up yet (e.g., usbsink disabled).
                    // Log and continue with the inputs that opened.
                    warn!(
                        "event=fanin.input.open_failed label={} pcm={} detail={:#}",
                        label, pcm_name, e
                    );
                }
            }
        }

        if inputs.is_empty() {
            anyhow::bail!(
                "no input PCMs opened successfully — daemon has nothing to mix. \
                 Check /etc/asound.conf for the per-renderer substream aliases \
                 (librespot_substream / shairport_substream / etc.) and snd-aloop \
                 module status (lsmod | grep snd_aloop)."
            );
        }

        let output = open_output(&config.output_pcm, config).with_context(|| {
            format!("opening output PCM {}", config.output_pcm)
        })?;
        info!(
            "event=fanin.output.opened pcm={} period_frames={} buffer_frames={}",
            config.output_pcm, config.period_frames, config.buffer_frames,
        );

        Ok(Self {
            inputs,
            output,
            sum_buf: vec![0i32; period_samples],
            output_buf: vec![0i16; period_samples],
            frames_written: Arc::new(AtomicU64::new(0)),
            output_xrun_count: Arc::new(AtomicU64::new(0)),
            xrun_tx,
            period_frames: config.period_frames,
        })
    }

    /// Number of successfully-opened inputs. May be less than the
    /// configured `JASPER_FANIN_INPUT_PCMS` length if some opens failed.
    pub fn input_count(&self) -> usize {
        self.inputs.len()
    }

    /// Read-only access to per-input counters for the STATUS endpoint
    /// (chunk 3 will use this).
    pub fn inputs(&self) -> &[Input] {
        &self.inputs
    }

    /// Drive the work loop until `shutdown` is set. Bumps the
    /// heartbeat sentinel after every successful frame.
    ///
    /// Errors here are escalated to the daemon main, which returns
    /// non-zero so systemd's `Restart=on-failure` brings us back.
    /// Transient errors (xruns) are handled inside `step()` without
    /// escalation.
    pub fn run(
        &mut self,
        shutdown: &AtomicBool,
        heartbeat: &Heartbeat,
    ) -> Result<()> {
        // Prime the output: write one period of zeros so the kernel
        // ring is non-empty when CamillaDSP / AEC bridge start reading.
        // Without this prime, the first writei could see -EPIPE
        // (underrun) before any data has been queued.
        self.output_buf.fill(0);
        write_output(
            &self.output,
            &self.output_buf,
            &self.output_xrun_count,
            &self.xrun_tx,
        )?;

        // Start the output stream now that it's primed. (PCM::new
        // with the default access creates the stream in PREPARED state;
        // explicit start() puts it in RUNNING.)
        if self.output.state() != State::Running {
            self.output.start().context("starting output PCM")?;
        }

        info!(
            "event=fanin.mixer.running inputs={} output_xruns=0",
            self.inputs.len(),
        );

        while !shutdown.load(Ordering::Relaxed) {
            self.step()?;
            heartbeat.bump_progress();
        }

        info!(
            "event=fanin.mixer.stopped frames_written={} output_xruns={}",
            self.frames_written.load(Ordering::Relaxed),
            self.output_xrun_count.load(Ordering::Relaxed),
        );

        Ok(())
    }

    /// One period of work: read all inputs, sum, write output.
    fn step(&mut self) -> Result<()> {
        // 1. Clear the i32 sum scratch.
        self.sum_buf.fill(0);

        // 2. Read from each input, accumulate into sum_buf.
        let period_frames = self.period_frames as usize;
        for input in &mut self.inputs {
            let frames = read_input(input, period_frames, &self.xrun_tx)?;
            // Only sum the samples we actually got. `read_input`
            // zero-pads the tail of input.read_buf so reading the
            // full period is also safe; explicit bounds save a few
            // unnecessary saturating_add calls when an input is
            // silent.
            let active = frames * (CHANNELS as usize);
            mix_into(&mut self.sum_buf[..active], &input.read_buf[..active]);
        }

        // 3. Clamp i32 sum -> i16 output.
        saturate_to_i16(&self.sum_buf, &mut self.output_buf);

        // 4. Write to output (blocks; paces the loop).
        write_output(
            &self.output,
            &self.output_buf,
            &self.output_xrun_count,
            &self.xrun_tx,
        )?;

        self.frames_written
            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
        Ok(())
    }
}

/// Sum input samples into the running i32 accumulator with saturating
/// arithmetic. Pulled out for unit testability — no ALSA needed.
fn mix_into(sum: &mut [i32], input: &[i16]) {
    debug_assert_eq!(sum.len(), input.len());
    for (s, &i) in sum.iter_mut().zip(input) {
        *s = s.saturating_add(i as i32);
    }
}

/// Clamp i32 sum back to i16 for output. Pulled out for unit testability.
fn saturate_to_i16(sum: &[i32], out: &mut [i16]) {
    debug_assert_eq!(sum.len(), out.len());
    for (o, &s) in out.iter_mut().zip(sum) {
        *o = s.clamp(i16::MIN as i32, i16::MAX as i32) as i16;
    }
}

fn open_input(pcm_name: &str, label: &str, config: &Config) -> Result<Input> {
    // Non-blocking so a silent renderer's substream doesn't stall
    // the work loop. read_input handles -EAGAIN as "no data; treat
    // as silence."
    let pcm = PCM::new(pcm_name, Direction::Capture, true)
        .with_context(|| format!("opening capture PCM {}", pcm_name))?;
    configure_pcm(&pcm, config)
        .with_context(|| format!("configuring capture PCM {}", pcm_name))?;
    // Start the stream so reads return data (or EAGAIN) instead of
    // blocking forever in the PREPARED state.
    pcm.start()
        .with_context(|| format!("starting capture PCM {}", pcm_name))?;
    let period_samples =
        (config.period_frames as usize) * (CHANNELS as usize);
    Ok(Input {
        pcm,
        label: label.to_string(),
        pcm_name: pcm_name.to_string(),
        read_buf: vec![0i16; period_samples],
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
    })
}

fn open_output(pcm_name: &str, config: &Config) -> Result<PCM> {
    // Blocking. The blocking writei() is what paces the work loop —
    // it returns when the kernel has consumed enough of the output
    // ring to make room for the next period.
    let pcm = PCM::new(pcm_name, Direction::Playback, false)
        .with_context(|| format!("opening playback PCM {}", pcm_name))?;
    configure_pcm(&pcm, config)
        .with_context(|| format!("configuring playback PCM {}", pcm_name))?;
    Ok(pcm)
}

fn configure_pcm(pcm: &PCM, config: &Config) -> Result<()> {
    // HwParams must be dropped before pcm.hw_params() is called.
    // The alsa-rs API: build the params, install them, drop the
    // handle in this nested scope.
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(CHANNELS)
            .with_context(|| format!("set_channels({})", CHANNELS))?;
        hwp.set_rate(config.sample_rate, ValueOr::Nearest)
            .with_context(|| format!("set_rate({})", config.sample_rate))?;
        hwp.set_format(FORMAT)
            .with_context(|| format!("set_format({:?})", FORMAT))?;
        hwp.set_access(Access::RWInterleaved)
            .context("set_access(RWInterleaved)")?;
        hwp.set_period_size(config.period_frames as i64, ValueOr::Nearest)
            .with_context(|| {
                format!("set_period_size({})", config.period_frames)
            })?;
        hwp.set_buffer_size(config.buffer_frames as i64)
            .with_context(|| {
                format!("set_buffer_size({})", config.buffer_frames)
            })?;
        pcm.hw_params(&hwp).context("installing HwParams")?;
    }
    Ok(())
}

/// Read up to `requested_frames` from `input`. Returns the number of
/// frames actually read (may be less than requested if the kernel
/// has less ready, or 0 if non-blocking and no data).
///
/// Failure modes handled in-band:
///   - `EAGAIN` (no data right now): substitute silence; return 0.
///   - `EPIPE` / `ESTRPIPE` (overrun): `try_recover`, log, substitute
///     silence; return 0.
///
/// All other errors propagate up — they indicate a structural
/// problem (PCM closed, driver fault) that the daemon can't handle
/// at this layer.
fn read_input(
    input: &mut Input,
    requested_frames: usize,
    xrun_tx: &Sender<XrunEvent>,
) -> Result<usize> {
    let io = input
        .pcm
        .io_i16()
        .context("getting i16 IO handle for input")?;
    match io.readi(&mut input.read_buf) {
        Ok(frames) => {
            input
                .frames_read
                .fetch_add(frames as u64, Ordering::Relaxed);
            // Zero the tail of read_buf if we got less than a full
            // period. The mixer's sum loop bounds the read region by
            // `frames`, but defense-in-depth: future code paths that
            // read the whole buffer (e.g., RMS for active detection)
            // should see zeros, not stale data, in the unfilled tail.
            if frames < requested_frames {
                let active = frames * (CHANNELS as usize);
                for s in &mut input.read_buf[active..] {
                    *s = 0;
                }
            }
            Ok(frames)
        }
        Err(e) => {
            let errno = e.errno();
            if errno == libc::EAGAIN {
                // Non-blocking read with no data ready. Renderer is
                // idle (or hasn't opened its substream yet). Treat
                // as silence.
                input.read_buf.fill(0);
                Ok(0)
            } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                // Input overrun: renderer produced faster than we
                // drained. snd_pcm_recover restarts the stream.
                let count =
                    input.xrun_count.fetch_add(1, Ordering::Relaxed) + 1;
                warn!(
                    "event=fanin.xrun source=input label={} count={}",
                    input.label, count,
                );
                // Best-effort forward to the off-thread xrun log
                // writer. Send error means the receiver was dropped
                // (shutdown in progress); fine to ignore.
                let _ = xrun_tx.send(XrunEvent {
                    source: XrunSource::Input,
                    label: input.label.clone(),
                    frames: requested_frames as u32,
                    count,
                });
                input
                    .pcm
                    .try_recover(e, true)
                    .context("recovering input xrun")?;
                input.read_buf.fill(0);
                Ok(0)
            } else {
                Err(e).context(format!(
                    "reading from input {} ({})",
                    input.label, input.pcm_name
                ))
            }
        }
    }
}

/// Write a full period to the output. Retries on transient xrun via
/// `try_recover`; propagates structural errors.
fn write_output(
    pcm: &PCM,
    buf: &[i16],
    xrun_counter: &Arc<AtomicU64>,
    xrun_tx: &Sender<XrunEvent>,
) -> Result<()> {
    let io = pcm
        .io_i16()
        .context("getting i16 IO handle for output")?;
    let frames_total = buf.len() / (CHANNELS as usize);
    let mut frames_done = 0;
    // Limit recovery attempts per period to avoid an infinite loop
    // if the device is structurally broken.
    let mut recoveries = 0;
    const MAX_RECOVERIES_PER_PERIOD: u32 = 3;

    while frames_done < frames_total {
        let offset = frames_done * (CHANNELS as usize);
        match io.writei(&buf[offset..]) {
            Ok(n) => {
                frames_done += n;
                if n == 0 {
                    // Defensive: a zero-frame write that didn't error
                    // would spin. Treat as transient and back off
                    // one iteration via a recovery attempt.
                    recoveries += 1;
                    if recoveries > MAX_RECOVERIES_PER_PERIOD {
                        anyhow::bail!(
                            "output writei returned 0 frames repeatedly"
                        );
                    }
                }
            }
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    let count =
                        xrun_counter.fetch_add(1, Ordering::Relaxed) + 1;
                    let pending = frames_total - frames_done;
                    warn!(
                        "event=fanin.xrun source=output count={} frames_pending={}",
                        count, pending,
                    );
                    let _ = xrun_tx.send(XrunEvent {
                        source: XrunSource::Output,
                        label: "output".to_string(),
                        frames: pending as u32,
                        count,
                    });
                    pcm.try_recover(e, true)
                        .context("recovering output xrun")?;
                    recoveries += 1;
                    if recoveries > MAX_RECOVERIES_PER_PERIOD {
                        anyhow::bail!(
                            "output xrun recovery exceeded {} attempts in one period",
                            MAX_RECOVERIES_PER_PERIOD,
                        );
                    }
                    // Loop continues; retry the write from `frames_done`.
                } else {
                    return Err(e).context("writing to output PCM");
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // Pure-function tests for the mix math. No ALSA needed.

    #[test]
    fn mix_into_sums_two_inputs() {
        let mut sum = vec![0i32; 4];
        mix_into(&mut sum, &[100, 200, 300, 400]);
        mix_into(&mut sum, &[50, 50, 50, 50]);
        assert_eq!(sum, vec![150, 250, 350, 450]);
    }

    #[test]
    fn mix_into_saturates_at_i32_bounds_but_stays_room_for_i16_saturation() {
        // Two max-i16 inputs sum to 2 × 32767 = 65534 — well within i32.
        // Only saturate_to_i16 should clip; mix_into just accumulates.
        let mut sum = vec![0i32; 1];
        mix_into(&mut sum, &[i16::MAX]);
        mix_into(&mut sum, &[i16::MAX]);
        assert_eq!(sum[0], 65534);
    }

    #[test]
    fn mix_into_cancels_positive_and_negative() {
        let mut sum = vec![0i32; 2];
        mix_into(&mut sum, &[5000, -3000]);
        mix_into(&mut sum, &[-5000, 3000]);
        assert_eq!(sum, vec![0, 0]);
    }

    #[test]
    fn saturate_to_i16_clamps_positive_overflow() {
        let mut out = vec![0i16; 1];
        saturate_to_i16(&[100_000], &mut out);
        assert_eq!(out[0], i16::MAX);
    }

    #[test]
    fn saturate_to_i16_clamps_negative_overflow() {
        let mut out = vec![0i16; 1];
        saturate_to_i16(&[-100_000], &mut out);
        assert_eq!(out[0], i16::MIN);
    }

    #[test]
    fn saturate_to_i16_passes_in_range_values() {
        let mut out = vec![0i16; 4];
        saturate_to_i16(&[0, 1000, -1000, 32767], &mut out);
        assert_eq!(out, vec![0, 1000, -1000, i16::MAX]);
    }

    #[test]
    fn mix_three_inputs_full_pipeline() {
        // Three inputs at ~1/3 max each: sum approaches max but
        // doesn't saturate. Models the realistic three-renderer
        // simultaneous-handover transient.
        let mut sum = vec![0i32; 4];
        mix_into(&mut sum, &[10_000, 10_000, 10_000, 10_000]);
        mix_into(&mut sum, &[10_000, 10_000, 10_000, 10_000]);
        mix_into(&mut sum, &[10_000, 10_000, 10_000, 10_000]);
        let mut out = vec![0i16; 4];
        saturate_to_i16(&sum, &mut out);
        assert_eq!(out, vec![30_000, 30_000, 30_000, 30_000]);
    }

    #[test]
    fn mix_three_max_inputs_saturates_output() {
        // Three max-positive inputs sum to 98_301, well above i16::MAX.
        // Saturation clips to 32767.
        let mut sum = vec![0i32; 2];
        mix_into(&mut sum, &[i16::MAX, i16::MAX]);
        mix_into(&mut sum, &[i16::MAX, i16::MAX]);
        mix_into(&mut sum, &[i16::MAX, i16::MAX]);
        let mut out = vec![0i16; 2];
        saturate_to_i16(&sum, &mut out);
        assert_eq!(out, vec![i16::MAX, i16::MAX]);
    }
}
