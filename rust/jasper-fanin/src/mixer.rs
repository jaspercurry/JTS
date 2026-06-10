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

use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Arc;

use alsa::pcm::{Access, Format, Frames, HwParams, State, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};
use log::{info, warn};

use crate::config::Config;
use crate::tts::{TtsInput, TtsMixer};
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
    /// Per-period pre-duck program buffer for the assistant loudness
    /// meter. Same length as sum_buf.
    content_meter_buf: Vec<i16>,
    /// Cumulative output frames written since startup. Surfaced via
    /// the STATUS endpoint.
    pub frames_written: Arc<AtomicU64>,
    /// Cumulative output xrun events.
    pub output_xrun_count: Arc<AtomicU64>,
    /// Selected input index. -1 means auto/mix all active inputs;
    /// -2 means pass no renderer lanes; non-negative means pass only
    /// that source's lane. The correction/test lane is always mixed so
    /// diagnostics keep working even if the household selected a
    /// renderer manually or mux temporarily selected NONE.
    selected_input_index: Arc<AtomicI32>,
    /// Channel for forwarding xrun events to the off-thread log
    /// writer. `try_send` is non-blocking on an unbounded channel
    /// (std::sync::mpsc::Sender::send only fails when the receiver
    /// is dropped, which happens at shutdown). Keeps the work loop's
    /// hot path off of disk I/O — the writer thread is the one
    /// stuck on fdatasync.
    xrun_tx: Sender<XrunEvent>,
    period_frames: u32,
    tts: Option<TtsMixer>,
    /// OPTIONAL music-only (pre-TTS) side-output — the multi-room sync
    /// tap (`docs/HANDOFF-multiroom.md` §2 "inv-2 realization"). `None`
    /// on a solo speaker (zero added work). `write_music_only` keeps it a
    /// LOSSY tap so `output` stays the SOLE timing owner (inv-1).
    music_output: Option<PCM>,
    /// Per-period i16 scratch for the music-only output (post-duck,
    /// pre-TTS). Same length as `output_buf`.
    music_only_buf: Vec<i16>,
    /// Cumulative frames written to the music-only output. STATUS.
    pub music_frames_written: Arc<AtomicU64>,
    /// Cumulative periods DROPPED on the music-only output — ring full
    /// (consumer behind) or xrun. A growing value means the snapserver
    /// consumer is behind; surfaced via STATUS, NEVER escalated (inv-1).
    pub music_output_drops: Arc<AtomicU64>,
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
    /// Open all configured inputs and the output. Every configured input
    /// is required: a missing lane means one renderer silently drops out
    /// of the summed music reference. `xrun_tx` is the
    /// non-blocking channel to the off-thread xrun log writer.
    pub fn new(
        config: &Config,
        xrun_tx: Sender<XrunEvent>,
        tts: Option<TtsInput>,
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
                        config.input_buffer_frames,
                    );
                    inputs.push(input);
                }
                Err(e) => {
                    anyhow::bail!(
                        "required fan-in input '{}' ({}) failed to open: {:#}",
                        label,
                        pcm_name,
                        e,
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
            config.output_pcm, config.period_frames, config.output_buffer_frames,
        );

        // OPTIONAL music-only side-output (multi-room sync tap). Opened
        // BEST-EFFORT: a configured-but-unopenable music PCM must NEVER
        // take down the primary audio path, so on failure we log and run
        // as a solo speaker (music_output = None). Non-blocking so the
        // lossy-tap write can drop-on-full without ever blocking the work
        // loop (inv-1: `output` stays the sole timing owner).
        let music_output = match &config.music_output_pcm {
            Some(pcm_name) => match open_music_output(pcm_name, config) {
                Ok(pcm) => {
                    info!(
                        "event=fanin.music_output.opened pcm={} (multi-room sync tap)",
                        pcm_name,
                    );
                    Some(pcm)
                }
                Err(e) => {
                    warn!(
                        "event=fanin.music_output.open_failed pcm={} detail={:#} — \
                         continuing WITHOUT the music-only tap (primary output unaffected)",
                        pcm_name, e,
                    );
                    None
                }
            },
            None => {
                info!("event=fanin.music_output.disabled (solo speaker; no sync tap)");
                None
            }
        };

        Ok(Self {
            inputs,
            output,
            sum_buf: vec![0i32; period_samples],
            output_buf: vec![0i16; period_samples],
            content_meter_buf: vec![0i16; period_samples],
            frames_written: Arc::new(AtomicU64::new(0)),
            output_xrun_count: Arc::new(AtomicU64::new(0)),
            selected_input_index: Arc::new(AtomicI32::new(-2)),
            xrun_tx,
            period_frames: config.period_frames,
            tts: tts.map(TtsMixer::new),
            music_output,
            music_only_buf: vec![0i16; period_samples],
            music_frames_written: Arc::new(AtomicU64::new(0)),
            music_output_drops: Arc::new(AtomicU64::new(0)),
        })
    }

    /// Number of configured inputs. Mixer construction fails if any
    /// configured input cannot be opened.
    pub fn input_count(&self) -> usize {
        self.inputs.len()
    }

    /// Read-only access to per-input counters for the STATUS endpoint
    /// (chunk 3 will use this).
    pub fn inputs(&self) -> &[Input] {
        &self.inputs
    }

    /// Shared selected-input index for the STATUS/control endpoint.
    /// The audio loop reads this atomically once per period.
    pub fn selected_input_index(&self) -> Arc<AtomicI32> {
        Arc::clone(&self.selected_input_index)
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

        // 2. Drain TTS/control commands once at the period boundary.
        // When voice ducking is routed through fan-in, attenuate only
        // renderer/program lanes. TTS is mixed after this step so it
        // remains audible and then flows through CamillaDSP crossover.
        let mut program_gain = 1.0f32;
        if let Some(tts) = self.tts.as_mut() {
            if tts.prepare_period() {
                program_gain = tts.program_duck_gain();
            }
        }

        // 3. Read from each input, accumulate into sum_buf.
        let period_frames = self.period_frames as usize;
        let selected_input = self.selected_input_index.load(Ordering::Relaxed);
        for (idx, input) in self.inputs.iter_mut().enumerate() {
            let frames = read_input(input, period_frames, &self.xrun_tx)?;
            if !input_selected(selected_input, idx, &input.label) {
                continue;
            }
            // Only sum the samples we actually got. `read_input`
            // zero-pads the tail of input.read_buf so reading the
            // full period is also safe; explicit bounds save a few
            // unnecessary saturating_add calls when an input is
            // silent.
            let active = frames * (CHANNELS as usize);
            mix_into(&mut self.sum_buf[..active], &input.read_buf[..active]);
        }
        if let Some(tts) = self.tts.as_mut() {
            saturate_to_i16(&self.sum_buf, &mut self.content_meter_buf);
            tts.observe_content_period(&self.content_meter_buf);
        }
        if program_gain != 1.0 {
            apply_gain_to_sum(&mut self.sum_buf, program_gain);
        }
        // Music-only side-tap (multi-room sync): the program AS PLAYED
        // minus the assistant — taken POST-duck (so a synced follower
        // hears the music dip under the leader's local TTS, matching the
        // room) and PRE-TTS (so the leader's assistant NEVER leaks to
        // followers — the inv-3 guarantee). Lossy: drop-on-full, never
        // blocks, never escalates — the primary `output` below stays the
        // sole timing owner (inv-1). `None` on a solo speaker → no work.
        if let Some(music_out) = self.music_output.as_ref() {
            saturate_to_i16(&self.sum_buf, &mut self.music_only_buf);
            write_music_only(
                music_out,
                &self.music_only_buf,
                &self.music_frames_written,
                &self.music_output_drops,
            );
        }
        if let Some(tts) = self.tts.as_mut() {
            tts.mix_period(&mut self.sum_buf);
        }

        // 4. Clamp i32 sum -> i16 output.
        saturate_to_i16(&self.sum_buf, &mut self.output_buf);

        // 5. Write to output (blocks; paces the loop).
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

/// Sum input samples into the running i32 accumulator after applying a
/// period-stable gain. Used for fan-in-owned voice ducking so TTS can
/// be mixed after program attenuation and still pass through CamillaDSP.
fn mix_into_with_gain(sum: &mut [i32], input: &[i16], gain: f32) {
    debug_assert_eq!(sum.len(), input.len());
    for (s, &i) in sum.iter_mut().zip(input) {
        let scaled = ((i as f32) * gain)
            .round()
            .clamp(i16::MIN as f32, i16::MAX as f32) as i16;
        *s = s.saturating_add(scaled as i32);
    }
}

/// Apply a period-stable gain to the accumulated program sum. Used
/// after pre-duck content metering so the assistant loudness baseline
/// tracks the listener-facing content, not the temporary ducked level.
fn apply_gain_to_sum(sum: &mut [i32], gain: f32) {
    for sample in sum {
        *sample = ((*sample as f32) * gain)
            .round()
            .clamp(i32::MIN as f32, i32::MAX as f32) as i32;
    }
}

/// Clamp i32 sum back to i16 for output. Pulled out for unit testability.
fn saturate_to_i16(sum: &[i32], out: &mut [i16]) {
    debug_assert_eq!(sum.len(), out.len());
    for (o, &s) in out.iter_mut().zip(sum) {
        *o = s.clamp(i16::MIN as i32, i16::MAX as i32) as i16;
    }
}

fn input_selected(
    selected_input: i32,
    input_index: usize,
    label: &str,
) -> bool {
    selected_input == -1
        || selected_input == input_index as i32
        || label == "correction"
}

fn open_input(pcm_name: &str, label: &str, config: &Config) -> Result<Input> {
    // Non-blocking so a silent renderer's substream doesn't stall
    // the work loop. read_input handles -EAGAIN as "no data; treat
    // as silence."
    let pcm = PCM::new(pcm_name, Direction::Capture, true)
        .with_context(|| format!("opening capture PCM {}", pcm_name))?;
    configure_pcm(&pcm, config, config.input_buffer_frames)
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
    configure_pcm(&pcm, config, config.output_buffer_frames)
        .with_context(|| format!("configuring playback PCM {}", pcm_name))?;
    Ok(pcm)
}

/// Open the OPTIONAL music-only side-output. **Non-blocking** — unlike
/// the primary `open_output`, this PCM must NEVER pace the work loop
/// (`write_music_only` drops on a full ring instead of blocking), so the
/// primary output stays the sole timing owner (inv-1). Same format / rate
/// / period / buffer as the primary output.
fn open_music_output(pcm_name: &str, config: &Config) -> Result<PCM> {
    let pcm = PCM::new(pcm_name, Direction::Playback, true)
        .with_context(|| format!("opening music-only output PCM {}", pcm_name))?;
    configure_pcm(&pcm, config, config.output_buffer_frames)
        .with_context(|| format!("configuring music-only output PCM {}", pcm_name))?;
    Ok(pcm)
}

fn configure_pcm(pcm: &PCM, config: &Config, buffer_frames: u32) -> Result<()> {
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
        hwp.set_buffer_size(buffer_frames as i64)
            .with_context(|| {
                format!("set_buffer_size({})", buffer_frames)
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

/// Write one period to the OPTIONAL music-only side-output. This is a
/// LOSSY side-tap, NOT a paced output: it must never block the work loop
/// and never escalate an error — the primary `output` is the sole timing
/// owner (inv-1). On a full ring (`EAGAIN`/short avail: the consumer is
/// behind) or an underrun (`EPIPE`: the consumer hasn't started reading)
/// we DROP this whole period and count it — snapserver sees a brief gap,
/// exactly like a starved capture, never back-pressure on the DAC loop.
///
/// **Period-aligned by construction:** we only write when the ring has
/// room for a WHOLE period (checked via `avail_update`). Only this thread
/// writes this PCM and the consumer only frees space, so room observed is
/// room guaranteed — a partial write can't shear a period and desync the
/// stream. A non-zero, growing `drops` is the operator's "consumer behind"
/// signal (surfaced via STATUS).
fn write_music_only(
    pcm: &PCM,
    buf: &[i16],
    frames_written: &Arc<AtomicU64>,
    drops: &Arc<AtomicU64>,
) {
    let frames_total = (buf.len() / (CHANNELS as usize)) as Frames;
    match pcm.avail_update() {
        // Room for a full period → write below.
        Ok(avail) if avail >= frames_total => {}
        // Ring too full for a whole period (consumer behind) → drop.
        Ok(_) => {
            drops.fetch_add(1, Ordering::Relaxed);
            return;
        }
        // Underrun / error → recover for next period, drop this one.
        Err(e) => {
            let _ = pcm.try_recover(e, true);
            drops.fetch_add(1, Ordering::Relaxed);
            return;
        }
    }
    let io = match pcm.io_i16() {
        Ok(io) => io,
        Err(_) => {
            drops.fetch_add(1, Ordering::Relaxed);
            return;
        }
    };
    match io.writei(buf) {
        Ok(n) => {
            frames_written.fetch_add(n as u64, Ordering::Relaxed);
        }
        Err(e) => {
            // try_recover handles EPIPE/ESTRPIPE; any error → drop, never
            // propagate (a broken side-tap must not crash the daemon).
            let _ = pcm.try_recover(e, true);
            drops.fetch_add(1, Ordering::Relaxed);
        }
    }
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
    fn mix_into_with_gain_ducks_program_lane() {
        let mut sum = vec![0i32; 4];
        mix_into_with_gain(&mut sum, &[10_000, -10_000, 1_000, -1_000], 0.1);
        assert_eq!(sum, vec![1_000, -1_000, 100, -100]);
    }

    #[test]
    fn apply_gain_to_sum_ducks_after_program_sum() {
        let mut sum = vec![20_000i32, -20_000, 1_500, -1_500];
        apply_gain_to_sum(&mut sum, 0.1);
        assert_eq!(sum, vec![2_000, -2_000, 150, -150]);
    }

    #[test]
    fn music_only_tap_is_post_duck_and_pre_tts() {
        // Mirrors step()'s tap point exactly: the music-only buffer is the
        // summed program AFTER the program duck and BEFORE TTS is mixed.
        // Two music lanes summed:
        let mut sum = vec![0i32; 4];
        mix_into(&mut sum, &[10_000, -10_000, 8_000, -8_000]);
        mix_into(&mut sum, &[2_000, -2_000, 1_000, -1_000]);
        // Program duck applies (TTS active): attenuate the program by 0.5.
        apply_gain_to_sum(&mut sum, 0.5);
        // TAP HERE — clamp to i16 for the music-only output.
        let mut music_only = vec![0i16; 4];
        saturate_to_i16(&sum, &mut music_only);
        // Post-duck (×0.5), pre-TTS: (12000,-12000,9000,-9000) × 0.5.
        assert_eq!(music_only, vec![6_000, -6_000, 4_500, -4_500]);

        // Now TTS would mix into the PRIMARY sum only — the tapped buffer
        // is already captured and is unaffected, which is the inv-3
        // guarantee: the assistant never reaches the synced (follower)
        // stream. Prove the tap is independent of the later TTS add:
        for s in sum.iter_mut() {
            *s = s.saturating_add(20_000); // stand-in for tts.mix_period
        }
        assert_eq!(music_only, vec![6_000, -6_000, 4_500, -4_500]);
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

    #[test]
    fn selected_input_passes_auto_selected_and_correction() {
        assert!(input_selected(-1, 0, "spotify"));
        assert!(input_selected(1, 1, "airplay"));
        assert!(!input_selected(1, 0, "spotify"));
        assert!(input_selected(1, 4, "correction"));
        assert!(!input_selected(-2, 0, "spotify"));
        assert!(input_selected(-2, 4, "correction"));
    }
}
