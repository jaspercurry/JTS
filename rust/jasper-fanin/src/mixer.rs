// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

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

use crate::config::{Config, Coupling};
use crate::fifo::{FifoWriteOutcome, FifoWriter};
use crate::lane_resampler::{LaneResampler, LaneResamplerObservability};
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

/// Sentinel for "no ALSA playback delay sample has landed yet".
pub const OUTPUT_DELAY_UNAVAILABLE: u64 = u64::MAX;

/// Per-input catch-up target, in WHOLE periods. The fill we want a lane's
/// capture ring to sit at right before the per-period read. One period is
/// the steady state for a lane clocked off the local DAC (its producer and
/// our consumer share the DAC clock, so its ring never grows).
const CATCHUP_TARGET_PERIODS: i64 = 1;

/// Per-input catch-up high-water, in WHOLE periods. A lane whose readable
/// backlog exceeds this is treated as FREE-RUNNING relative to our DAC-paced
/// drain (today only the USB lane: the host clock feeds it, while we read at
/// the DAC rate) and bounded-resynced down to TARGET.
///
/// The tuning constraint is two-sided, reasoned on ring OCCUPANCY (what
/// `avail_update` reports on a capture PCM — frames readable), NOT inter-burst
/// gap time. Lower bound: it MUST sit above the worst-case peak occupancy of a
/// HEALTHY networked lane, or we would clip legitimately-buffered audio. Two
/// effects stack — a WiFi-bursty AirPlay lane deposits an A-MPDU burst of ~4
/// packets (~5.5 periods) into its ring at once (then drains back at the DAC
/// rate), and a scheduling stall delays OUR drain (worst-case ~36.8 ms ≈ 6.9
/// periods on a stressed stock Pi 5, PREEMPT_RT not yet in; see
/// HANDOFF-fan-in-daemon.md) — so a stall coinciding with a burst is ~5.5 + 6.9
/// ≈ 12.4 periods of peak occupancy on a healthy lane. Upper bound: it MUST sit
/// below the input buffer depth (16 periods / 4096 frames, the "0 xruns over
/// 4.5 min" sizing) so the resync fires before overrun. 14 periods (~75 ms)
/// clears the ~12.4-period healthy burst+stall peak with ~1.6-period margin and
/// still leaves 2 periods under the 16-period buffer. A free-running lane grows
/// MONOTONICALLY (its producer's average rate exceeds ours), so it always
/// crosses this; a healthy lane's burst+stall peak stays below it.
///
/// NOT drift correction: this is a controlled, occasional drop-resync at the
/// residual drift rate, not a drop-FREE resampler (that is the later per-lane
/// adaptive resampler). Honest tradeoff: a backed-up lane loses a bounded
/// chunk of audio at each resync instead of cascading into an upstream
/// producer overflow.
const CATCHUP_HIGH_WATER_PERIODS: i64 = 14;

/// Hard cap on whole periods discarded in a single resync, so a pathological
/// `avail` (driver fault, or a huge buffer) can't turn the bounded
/// read-and-drop into an unbounded syscall spin inside the hot loop. A lane
/// further behind than this finishes resyncing over the next few periods —
/// still bounded per period.
const CATCHUP_MAX_DRAIN_PERIODS: i64 = 64;

/// Emit the rate-limited `event=fanin.input.catchup` log on the 1st resync
/// for a lane and then every Nth, so a chronically free-running lane can't
/// spam the journal. A resync only fires when a lane crosses the high-water,
/// which is already infrequent; this is defense-in-depth against a wedged
/// producer. Count-based (not time-based) so the hot loop never reads a clock.
const CATCHUP_LOG_EVERY: u64 = 64;

/// The final-output transport. `Alsa` (the default) writes the snd-aloop
/// substream and is paced by the blocking ALSA `writei` — byte-identical to the
/// pre-coupling daemon. `Fifo` is the writer primitive used by the public
/// `transport_pipe` mode: it writes a bounded named pipe that CamillaDSP
/// RawFile-captures. Both are the sole timing owner of the fan-in work loop in
/// their respective modes; only one is ever active.
enum Output {
    Alsa(PCM),
    Fifo(FifoWriter),
}

pub struct Mixer {
    inputs: Vec<Input>,
    output: Output,
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
    /// Last observed ALSA playback delay for the primary output PCM.
    /// `OUTPUT_DELAY_UNAVAILABLE` until the first successful sample.
    pub output_delay_frames: Arc<AtomicU64>,
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
    /// Coupling transport + (under `transport_pipe`) the shared pipe observability
    /// counters, cloned for the STATUS endpoint. `None` of the pipe fields under
    /// `Loopback` (the default), so STATUS reports `transport=loopback` with no
    /// pipe block — byte-identical to the pre-coupling snapshot.
    pub coupling: CouplingObservability,
}

/// Coupling transport echo + the shared pipe counters for the STATUS endpoint.
/// Under `Loopback` (default), `pipe` is `None` and STATUS reports only
/// `transport:"loopback"`. Under `transport_pipe`, `pipe` carries the pipe path
/// + the reopen / dropped-period / live-pipe-size atomics the `FifoWriter`
/// updates.
#[derive(Clone)]
pub struct CouplingObservability {
    pub transport: &'static str,
    pub pipe: Option<PipeObservability>,
}

/// The shared pipe counters (cloned Arcs from the live `FifoWriter`).
#[derive(Clone)]
pub struct PipeObservability {
    pub path: String,
    pub requested_pipe_bytes: u32,
    pub reopen_count: Arc<AtomicU64>,
    pub dropped_periods: Arc<AtomicU64>,
    pub actual_pipe_bytes: Arc<AtomicU64>,
}

pub struct Input {
    pcm: PCM,
    pub label: String,
    pub pcm_name: String,
    /// Per-input read buffer (i16 interleaved stereo). Reused as the
    /// discard scratch by the catch-up drain — no per-period allocation.
    read_buf: Vec<i16>,
    pub xrun_count: Arc<AtomicU64>,
    pub frames_read: Arc<AtomicU64>,
    /// Cumulative frames DISCARDED by the bounded catch-up resync on this
    /// lane (see `drain_input_excess`). Non-zero only on a free-running
    /// lane (the USB host-clock lane); stays 0 forever on DAC-locked lanes.
    /// A growing value is the operator's "this lane is drifting and we are
    /// drop-resyncing it" signal — surfaced via STATUS, never escalated.
    pub catchup_resync_frames: Arc<AtomicU64>,
    /// Cumulative catch-up resync EVENTS (each is one high-water crossing
    /// that discarded ≥1 period). Paired with `catchup_resync_frames` so
    /// STATUS shows both how often and how much.
    pub catchup_events: Arc<AtomicU64>,
    /// OPTIONAL per-input adaptive resampler (DEFAULT-OFF). `Some` only on the
    /// configured clock-crossing lane when `JASPER_FANIN_INPUT_RESAMPLER` is
    /// `enabled`. When `Some`, this lane is rate-reconciled to the DAC clock
    /// (drop-free) instead of catch-up-drained; when `None` (the default for
    /// every lane), the read path is byte-for-byte today's behaviour.
    resampler: Option<LaneResampler>,
}

impl Mixer {
    /// Open all configured inputs and the output. Every configured input
    /// is required: a missing lane means one renderer silently drops out
    /// of the summed music reference. `xrun_tx` is the
    /// non-blocking channel to the off-thread xrun log writer.
    pub fn new(config: &Config, xrun_tx: Sender<XrunEvent>, tts: Option<TtsInput>) -> Result<Self> {
        let period_samples = (config.period_frames as usize) * (CHANNELS as usize);

        let mut inputs = Vec::with_capacity(config.input_pcms.len());
        for (label, pcm_name) in config.input_renderers.iter().zip(&config.input_pcms) {
            // DEFAULT-OFF: build a per-input resampler ONLY for the configured
            // clock-crossing lane AND only when explicitly enabled. Every other
            // lane (and every lane when the feature is off) gets `None` — the
            // byte-identical-to-today path. A construction failure degrades to
            // `None` with a warning rather than failing the daemon.
            let resampler =
                if config.input_resampler_enabled && label == &config.input_resampler_lane_label {
                    build_lane_resampler(label, config)
                } else {
                    None
                };
            match open_input(pcm_name, label, config, resampler) {
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

        // DEFAULT-OFF feature: if the resampler is armed by env but its
        // configured lane label matched no live input, NO `LaneResampler` was
        // constructed above — the feature silently no-ops. Surface that ONCE
        // (the review's flagged-missing diagnostic) so an operator who set the
        // env var can see WHY they observed no effect, with the available
        // labels to fix the typo.
        if let Some(available) = resampler_lane_not_found(
            config.input_resampler_enabled,
            &config.input_resampler_lane_label,
            &config.input_renderers,
        ) {
            warn!(
                "event=fanin.resampler.noop reason=lane_not_found requested={} available=[{}]",
                config.input_resampler_lane_label, available,
            );
        }

        // Final-output transport. Loopback (default) opens the ALSA snd-aloop
        // substream — byte-identical to today. Fifo ensures + lazily opens the
        // bounded named pipe CamillaDSP RawFile-captures (the lower-latency
        // coupling). Exactly one is active.
        let (output, coupling) = match config.camilla_coupling {
            Coupling::Loopback => {
                let pcm = open_output(&config.output_pcm, config)
                    .with_context(|| format!("opening output PCM {}", config.output_pcm))?;
                info!(
                    "event=fanin.output.opened transport=alsa pcm={} period_frames={} buffer_frames={}",
                    config.output_pcm, config.period_frames, config.output_buffer_frames,
                );
                (
                    Output::Alsa(pcm),
                    CouplingObservability {
                        transport: "loopback",
                        pipe: None,
                    },
                )
            }
            Coupling::TransportPipe => {
                // The pipe is created here (producer owns it); the write end is
                // opened reader-first lazily on the first period so startup is
                // never gated on CamillaDSP being up.
                let writer = FifoWriter::new(
                    &config.camilla_pipe_path,
                    config.period_frames,
                    config.camilla_pipe_bytes,
                )
                .with_context(|| {
                    format!("ensuring fan-in→camilla pipe {}", config.camilla_pipe_path)
                })?;
                info!(
                    "event=fanin.output.opened transport=transport_pipe path={} period_frames={} requested_pipe_bytes={}",
                    config.camilla_pipe_path, config.period_frames, config.camilla_pipe_bytes,
                );
                // Capture the shared counters before the writer moves into Output.
                let (reopen_count, dropped_periods, actual_pipe_bytes) = writer.observability();
                (
                    Output::Fifo(writer),
                    CouplingObservability {
                        transport: "transport_pipe",
                        pipe: Some(PipeObservability {
                            path: config.camilla_pipe_path.clone(),
                            requested_pipe_bytes: config.camilla_pipe_bytes,
                            reopen_count,
                            dropped_periods,
                            actual_pipe_bytes,
                        }),
                    },
                )
            }
        };

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
            output_delay_frames: Arc::new(AtomicU64::new(OUTPUT_DELAY_UNAVAILABLE)),
            selected_input_index: Arc::new(AtomicI32::new(-2)),
            xrun_tx,
            period_frames: config.period_frames,
            tts: tts.map(TtsMixer::new),
            music_output,
            music_only_buf: vec![0i16; period_samples],
            music_frames_written: Arc::new(AtomicU64::new(0)),
            music_output_drops: Arc::new(AtomicU64::new(0)),
            coupling,
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
    pub fn run(&mut self, shutdown: &AtomicBool, heartbeat: &Heartbeat) -> Result<()> {
        // Prime + start is ALSA-specific. The FIFO transport has no kernel ring
        // to prime and no PREPARED→RUNNING transition; its write end opens
        // reader-first lazily inside step() and paces on the pipe.
        if let Output::Alsa(pcm) = &self.output {
            // Prime the output: write one period of zeros so the kernel
            // ring is non-empty when CamillaDSP / AEC bridge start reading.
            // Without this prime, the first writei could see -EPIPE
            // (underrun) before any data has been queued.
            self.output_buf.fill(0);
            write_output(
                pcm,
                &self.output_buf,
                &self.output_xrun_count,
                &self.xrun_tx,
            )?;

            // Start the output stream now that it's primed. (PCM::new
            // with the default access creates the stream in PREPARED state;
            // explicit start() puts it in RUNNING.)
            if pcm.state() != State::Running {
                pcm.start().context("starting output PCM")?;
            }
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
            let frames = if input.resampler.is_some() {
                // ARMED clock-crossing lane (DEFAULT-OFF; only the USB lane when
                // enabled). The resampler OWNS rate reconciliation: read ALL
                // available frames into it (DLL-steered to the DAC clock) and
                // render exactly one DAC-paced period. The catch-up drain is
                // bypassed here on purpose — the resampler holds the ring at a
                // small fixed fill (no sawtooth), which is the whole point.
                read_into_resampler_and_render(input, period_frames, &self.xrun_tx)?
            } else {
                // DEFAULT path — byte-for-byte today's behaviour.
                //
                // Bounded catch-up resync BEFORE the period read, for EVERY lane
                // regardless of selection. A free-running lane (the USB host-clock
                // lane) backs its capture ring up past the high-water; we discard
                // the excess down to one period here so the upstream producer never
                // overflows and back-pressure can reach the host. A DAC-locked lane
                // sits at one period and this is a single `avail_update` no-op.
                // INTENTIONALLY independent of `input_selected` below: a de-selected
                // (muxed-out) free-running lane STILL backs up and must be drained,
                // so do NOT move this under the selection gate. Drop-controlled,
                // not drop-free — see the constant docs.
                drain_input_excess(input, period_frames);
                read_input(input, period_frames, &self.xrun_tx)?
            };
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

        // 5. Write to output (blocks; paces the loop). Dispatch on transport:
        //    - Alsa: blocking writei, returns when the loopback ring has room
        //      (DAC-paced via the dsnoop consumer). Counts every period.
        //    - Fifo: blocking pipe write, returns when the pipe has room
        //      (DAC-paced via CamillaDSP's RawFile capture). A reader-gone /
        //      no-reader turn returns Waited (the bounded reopen-wait already
        //      slept), dropping this period; we still return Ok so run() bumps
        //      the heartbeat — the loop is alive and bounded, never wedged.
        match &mut self.output {
            Output::Alsa(pcm) => {
                write_output(
                    pcm,
                    &self.output_buf,
                    &self.output_xrun_count,
                    &self.xrun_tx,
                )?;
                store_output_delay(pcm, &self.output_delay_frames);
                self.frames_written
                    .fetch_add(self.period_frames as u64, Ordering::Relaxed);
            }
            Output::Fifo(writer) => {
                match writer.write_period(&self.output_buf) {
                    FifoWriteOutcome::Wrote => {
                        self.frames_written
                            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
                    }
                    FifoWriteOutcome::Waited => {
                        // No reader / reader-gone: the writer already waited a
                        // bounded REOPEN_WAIT. Drop this period (CamillaDSP is
                        // reloading or not yet up) — do NOT count frames. The
                        // loop stays alive; the heartbeat is bumped by run().
                    }
                }
            }
        }
        Ok(())
    }
}

fn store_output_delay(pcm: &PCM, delay_frames: &AtomicU64) {
    if let Ok(delay) = pcm.delay() {
        delay_frames.store(delay.max(0) as u64, Ordering::Relaxed);
    }
}

impl Input {
    /// The lane's resampler observability handles for STATUS, or `None` when no
    /// resampler is armed on this lane (the default — DEFAULT-OFF feature).
    pub fn resampler_observability(&self) -> Option<LaneResamplerObservability> {
        self.resampler.as_ref().map(|r| r.observability())
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

fn input_selected(selected_input: i32, input_index: usize, label: &str) -> bool {
    selected_input == -1 || selected_input == input_index as i32 || label == "correction"
}

/// Pure decision for the bounded catch-up resync: given the frames a lane
/// currently has readable (`avail`) and the period size, how many WHOLE
/// periods should be discarded to bring the ring down to `CATCHUP_TARGET_PERIODS`?
///
/// Returns 0 unless `avail` exceeds `CATCHUP_HIGH_WATER_PERIODS` — so a
/// healthy DAC-locked lane (ring ~1 period) never drains. When it does fire:
///   - WHOLE periods only — discarding a fractional period would shear the
///     stream and desync this lane from its siblings in the per-period sum.
///   - Leaves at least `CATCHUP_TARGET_PERIODS` readable, so the immediately
///     following normal read in `step()` still gets a full period (the
///     resync never induces an underrun).
///   - Capped at `CATCHUP_MAX_DRAIN_PERIODS` so a bogus `avail` can't spin
///     the hot loop on syscalls.
///
/// Pure (no ALSA) for unit testability — `drain_input_excess` does the I/O.
fn catchup_drain_periods(avail: i64, period_frames: i64) -> i64 {
    debug_assert!(period_frames > 0);
    let high_water = period_frames * CATCHUP_HIGH_WATER_PERIODS;
    if avail <= high_water {
        return 0;
    }
    let target = period_frames * CATCHUP_TARGET_PERIODS;
    // avail > high_water >= target ⇒ (avail - target) > 0.
    let excess_periods = (avail - target) / period_frames; // floor
    excess_periods.min(CATCHUP_MAX_DRAIN_PERIODS)
}

/// Pure decision for the "armed but lane label not found" no-op warning.
///
/// Returns `Some(available_labels_csv)` when the resampler is ENABLED but its
/// configured `lane_label` matches NONE of the live `input_labels` — the state
/// in which the feature silently does nothing because `Mixer::new` constructs
/// no `LaneResampler`. Returns `None` (no warning) when the feature is off, or
/// when the label DOES match a live lane (the normal armed path). The returned
/// CSV is the human-facing "here are the labels you could have meant" hint.
///
/// Pulled out as a pure function (no ALSA) so the once-only warning decision is
/// unit-testable on a non-Linux host via the macOS-ALSA-scratch convention.
fn resampler_lane_not_found(
    enabled: bool,
    lane_label: &str,
    input_labels: &[String],
) -> Option<String> {
    if !enabled {
        return None;
    }
    if input_labels.iter().any(|l| l == lane_label) {
        return None;
    }
    Some(input_labels.join(","))
}

/// Resolve the input resampler's burst-ring capacity (frames) from the scalar
/// knobs.
///
/// `requested` is the explicit `input_resampler_ring_frames` env override
/// (non-zero pins it) OR, when `0`, twice the lane's ALSA
/// `input_buffer_frames`. The 2x derived default is deliberate: hardware USB
/// testing showed a 4096-frame ring could stay locked but still overrun on
/// snd-aloop burst arrivals, while an 8192-frame ring absorbed the same bursts
/// without adding steady latency (the hold target controls latency; ring
/// capacity is just headroom). The result is floored to the resampler's
/// STRUCTURAL minimum (`target + warm-up cushion + period + radius + 1`) so a
/// tiny configured value can never make `LaneResampler::new` reject the ring.
///
/// Pure over primitives (no ALSA, no `Config`) so it is unit-testable on a
/// non-Linux host via the macOS-ALSA-scratch convention.
fn resampler_ring_frames(
    requested_ring_frames: u32,
    input_buffer_frames: u32,
    target_frames: u32,
    warmup_cushion_frames: u32,
    period_frames: u32,
) -> usize {
    let radius = jasper_resampler::RADIUS_FRAMES as usize;
    let min_ring = target_frames as usize
        + warmup_cushion_frames as usize
        + period_frames as usize
        + radius
        + 1;
    let requested = if requested_ring_frames > 0 {
        requested_ring_frames as usize
    } else {
        (input_buffer_frames as usize).saturating_mul(2)
    };
    requested.max(min_ring)
}

/// Build the per-input resampler for the clock-crossing lane, or `None` on a
/// construction failure (which we log and degrade past — the lane just runs the
/// catch-up fallback). Sizes the resampler's input ring for burst headroom and
/// holds a warm-up cushion above the base target during acquisition/steady
/// state (see `lane_resampler.rs`).
fn build_lane_resampler(label: &str, config: &Config) -> Option<LaneResampler> {
    let ring_frames = resampler_ring_frames(
        config.input_resampler_ring_frames,
        config.input_buffer_frames,
        config.input_resampler_target_frames,
        config.input_resampler_warmup_cushion_frames,
        config.period_frames,
    );
    let cushion = config.input_resampler_warmup_cushion_frames as usize;
    let target = config.input_resampler_target_frames as usize;
    match LaneResampler::new(
        CHANNELS as usize,
        config.period_frames,
        config.sample_rate,
        target,
        cushion,
        config.input_resampler_max_adjust_ppm as f64,
        ring_frames,
    ) {
        Ok(r) => {
            // Canonical arming line the operator greps for to confirm the
            // DEFAULT-OFF feature engaged on this lane. Keep the event name and
            // the lane/base target/held target/max-ppm fields stable —
            // jasper-trace / doc point at them. warmup_cushion + ring_frames
            // are extra diagnostic detail.
            let held_target = target + cushion;
            info!(
                "event=fanin.resampler.armed lane={} target_frames={} held_target_frames={} \
                 warmup_cushion_frames={} max_adjust_ppm={} ring_frames={} \
                 (DLL-steered to DAC clock; catch-up drain bypassed on this lane)",
                label,
                target,
                held_target,
                cushion,
                config.input_resampler_max_adjust_ppm,
                ring_frames,
            );
            Some(r)
        }
        Err(e) => {
            warn!(
                "event=fanin.resampler.noop reason=construction_failed lane={} detail={} — \
                 falling back to catch-up drain on this lane",
                label, e,
            );
            None
        }
    }
}

fn open_input(
    pcm_name: &str,
    label: &str,
    config: &Config,
    resampler: Option<LaneResampler>,
) -> Result<Input> {
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
    let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
    Ok(Input {
        pcm,
        label: label.to_string(),
        pcm_name: pcm_name.to_string(),
        read_buf: vec![0i16; period_samples],
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
        catchup_resync_frames: Arc::new(AtomicU64::new(0)),
        catchup_events: Arc::new(AtomicU64::new(0)),
        resampler,
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
            .with_context(|| format!("set_period_size({})", config.period_frames))?;
        hwp.set_buffer_size(buffer_frames as i64)
            .with_context(|| format!("set_buffer_size({})", buffer_frames))?;
        pcm.hw_params(&hwp).context("installing HwParams")?;
    }
    Ok(())
}

/// Bounded per-input catch-up resync. Called once per lane per period,
/// BEFORE the normal `read_input`.
///
/// ## Why this exists
///
/// Every lane is read exactly one period per work-loop iteration, and the
/// loop is paced by the blocking OUTPUT write (the local DAC clock). A lane
/// whose producer is clocked off the *same* DAC (every networked renderer:
/// AirPlay / Spotify / Bluetooth / TTS) keeps its capture ring at ~one
/// period forever — it can't outrun a consumer on its own clock. The USB
/// lane is different: its producer is the host (Mac) clock, and the gadget's
/// async feedback currently tracks the snd-aloop jiffies timer, not the DAC,
/// so a small residual rate gap accumulates. With a strict one-period read
/// and no catch-up, that excess never drains — the ring fills monotonically
/// until it overruns, by which point the *upstream* usbsink producer queue
/// has already overflowed (dropped_full) because back-pressure never reached
/// the host.
///
/// This drains the excess down to one period when a lane's readable backlog
/// crosses the high-water, so the ring stays bounded and back-pressure can
/// propagate. It is GENERIC per-input: it only ever fires for a lane that
/// actually backs up. A DAC-locked lane sits at one period and this is a
/// single non-blocking `avail_update` — no reads, no effect.
///
/// ## Honesty
///
/// Drop-CONTROLLED, not drop-FREE: a backed-up lane loses a few ms of audio
/// at each resync (an occasional discard at the residual drift rate), traded
/// against a cascading upstream overflow. True drop-free for the mixed path
/// is the later per-lane adaptive resampler; this does NOT resample.
///
/// ## RT-safety
///
/// No allocation (discards into the lane's existing `read_buf` scratch) and
/// no blocking (`avail_update` is a non-blocking query; the discard `readi`
/// only ever reads frames `avail_update` already reported ready). The number
/// of discard reads is capped per call (`CATCHUP_MAX_DRAIN_PERIODS`). The
/// log is count-gated, so the common no-resync path touches no clock and
/// emits nothing.
fn drain_input_excess(input: &mut Input, period_frames: usize) {
    // Non-blocking query of how many frames are readable right now.
    // EAGAIN/error here just means "no usable reading right now" — leave
    // the normal read_input path to handle recovery; never block or panic.
    let avail = match input.pcm.avail_update() {
        Ok(a) => a,
        Err(_) => return,
    };
    let to_drain = catchup_drain_periods(avail, period_frames as i64);
    if to_drain == 0 {
        return; // healthy lane — the overwhelmingly common path.
    }

    let io = match input.pcm.io_i16() {
        Ok(io) => io,
        Err(_) => return,
    };
    // Discard whole periods into the existing read_buf scratch (reused; no
    // allocation). read_input overwrites read_buf next, so trashing it here
    // is safe. On non-blocking capture, readi returns Err(EAGAIN) the instant
    // the ring drops below one period (it drained faster than avail claimed) —
    // that Err arm is the normal early-stop. Ok(0) is a defensive guard for a
    // 0-frame return that shouldn't occur here. The 0..to_drain bound (≤ MAX)
    // means it can never spin regardless.
    let mut discarded_frames: u64 = 0;
    for _ in 0..to_drain {
        match io.readi(&mut input.read_buf) {
            Ok(0) => break,
            Ok(n) => discarded_frames += n as u64,
            Err(_) => break,
        }
    }
    if discarded_frames == 0 {
        return;
    }

    input
        .catchup_resync_frames
        .fetch_add(discarded_frames, Ordering::Relaxed);
    let events = input.catchup_events.fetch_add(1, Ordering::Relaxed) + 1;
    // Rate-limited: 1st event for this lane, then every Nth. Count-based so
    // the hot loop reads no clock. Logged outside any tight inner loop.
    if events == 1 || events % CATCHUP_LOG_EVERY == 0 {
        warn!(
            "event=fanin.input.catchup label={} discarded_frames={} avail_frames={} \
             target_frames={} events={} total_resync_frames={} \
             (free-running lane drop-resync; not drop-free)",
            input.label,
            discarded_frames,
            avail,
            period_frames * (CATCHUP_TARGET_PERIODS as usize),
            events,
            input.catchup_resync_frames.load(Ordering::Relaxed),
        );
    }
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
                let count = input.xrun_count.fetch_add(1, Ordering::Relaxed) + 1;
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

/// Cap on period-equivalent work for the ARMED lane drain in one `step()` call.
/// Like `CATCHUP_MAX_DRAIN_PERIODS`, this bounds syscall work per period so a
/// pathological `avail` (driver fault) can't spin the hot loop. Frames beyond
/// the cap stay in the kernel ring and are read next period — the resampler's
/// own ring is the rate buffer, so leaving a little behind is harmless.
const RESAMPLER_MAX_READ_PERIODS: i64 = 64;

/// Return the bounded number of currently readable frames that the armed-lane
/// drain should pull into the resampler this period. Pure helper for the
/// real-time cap math; ALSA I/O happens in `read_into_resampler_and_render`.
fn resampler_read_budget_frames(avail: Frames, period_frames: usize) -> usize {
    if avail <= 0 {
        return 0;
    }
    let max_frames = period_frames.saturating_mul(RESAMPLER_MAX_READ_PERIODS as usize);
    (avail as usize).min(max_frames)
}

fn recover_resampler_input_xrun(
    input: &mut Input,
    error: alsa::Error,
    period_frames: usize,
    xrun_tx: &Sender<XrunEvent>,
    operation: &str,
) -> Result<()> {
    let count = input.xrun_count.fetch_add(1, Ordering::Relaxed) + 1;
    warn!(
        "event=fanin.xrun source=input label={} count={} op={} (resampler lane)",
        input.label, count, operation,
    );
    let _ = xrun_tx.send(XrunEvent {
        source: XrunSource::Input,
        label: input.label.clone(),
        frames: period_frames as u32,
        count,
    });
    input
        .pcm
        .try_recover(error, true)
        .context("recovering resampler input xrun")?;
    // `try_recover` can leave a capture PCM in PREPARED. The ordinary
    // read_input path will kick that forward with the next readi(), but the
    // resampler path polls avail_update() before reading; without an explicit
    // restart it can sit at avail=0 forever after a startup xrun.
    if input.pcm.state() != State::Running {
        input
            .pcm
            .start()
            .with_context(|| format!("restarting resampler input {} after xrun", input.label))?;
    }
    if let Some(r) = input.resampler.as_mut() {
        r.reset();
    }
    input.read_buf.fill(0);
    Ok(())
}

/// Read all currently-available frames from an ARMED lane into its resampler,
/// then render exactly one DAC-paced period into `read_buf`. Returns the number
/// of real (non-silence) frames the render produced — `period_frames` when the
/// resampler is locked, `0` while it is priming or underfilled (the lane is
/// silent, exactly as an idle renderer's substream is today).
///
/// This REPLACES the `drain_input_excess` + strict-one-period `read_input` pair
/// for the armed lane. Rate reconciliation lives in the resampler (DLL-steered
/// to the DAC clock); the catch-up drain is intentionally bypassed here.
///
/// RT-safety: bounded syscalls (`avail_update` probes plus reads of frames
/// already reported ready, capped at `RESAMPLER_MAX_READ_PERIODS` periods
/// total), no allocation (reads into the existing `read_buf` scratch, pushes
/// into the resampler's pre-sized ring), no blocking (non-blocking capture). An
/// `EPIPE`/`ESTRPIPE` overrun recovers + resets the resampler (a discontinuity)
/// and renders silence for this period.
fn read_into_resampler_and_render(
    input: &mut Input,
    period_frames: usize,
    xrun_tx: &Sender<XrunEvent>,
) -> Result<usize> {
    let mut read_budget_remaining =
        period_frames.saturating_mul(RESAMPLER_MAX_READ_PERIODS as usize);
    if read_budget_remaining > 0 {
        // Drain every frame ALSA reports ready, including final partial periods.
        // Re-check after each drained snapshot so frames that arrive during this
        // step do not sit in the kernel ring for a full extra render period.
        // The total work remains bounded by `read_budget_remaining`.
        let mut stop_drain = false;
        while read_budget_remaining > 0 && !stop_drain {
            let avail = match input.pcm.avail_update() {
                Ok(avail) => avail,
                Err(e) => {
                    let errno = e.errno();
                    if errno == libc::EAGAIN {
                        break;
                    } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                        recover_resampler_input_xrun(
                            input,
                            e,
                            period_frames,
                            xrun_tx,
                            "avail_update",
                        )?;
                        break;
                    } else {
                        return Err(e).context(format!(
                            "querying resampler input {} ({})",
                            input.label, input.pcm_name
                        ));
                    }
                }
            };
            let mut frames_remaining =
                resampler_read_budget_frames(avail, period_frames).min(read_budget_remaining);
            if frames_remaining == 0 {
                break;
            }
            while frames_remaining > 0 {
                let frames_to_read = frames_remaining.min(period_frames);
                let samples_to_read = frames_to_read * (CHANNELS as usize);
                let read_result = {
                    let io = input
                        .pcm
                        .io_i16()
                        .context("getting i16 IO handle for resampler input")?;
                    io.readi(&mut input.read_buf[..samples_to_read])
                };
                match read_result {
                    Ok(0) => {
                        stop_drain = true;
                        break;
                    }
                    Ok(n) => {
                        input.frames_read.fetch_add(n as u64, Ordering::Relaxed);
                        let samples = n * (CHANNELS as usize);
                        if let Some(r) = input.resampler.as_mut() {
                            r.push_input(&input.read_buf[..samples]);
                        }
                        frames_remaining = frames_remaining.saturating_sub(n);
                        read_budget_remaining = read_budget_remaining.saturating_sub(n);
                        // Short read means the ring emptied earlier than
                        // `avail_update` claimed; stop rather than spin.
                        if n < frames_to_read {
                            stop_drain = true;
                            break;
                        }
                    }
                    Err(e) => {
                        let errno = e.errno();
                        if errno == libc::EAGAIN {
                            stop_drain = true;
                            break;
                        } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                            // Lane overrun: a discontinuity. Recover the PCM and
                            // reset the resampler so it re-primes from fresh input
                            // rather than interpolating across the gap.
                            recover_resampler_input_xrun(
                                input,
                                e,
                                period_frames,
                                xrun_tx,
                                "readi",
                            )?;
                            stop_drain = true;
                            break;
                        } else {
                            return Err(e).context(format!(
                                "reading from resampler input {} ({})",
                                input.label, input.pcm_name
                            ));
                        }
                    }
                }
            }
        }
    }

    // Render exactly one DAC-paced period into read_buf for the mixer to sum.
    let real_frames = match input.resampler.as_mut() {
        Some(r) => r.render_period(&mut input.read_buf),
        None => {
            // Unreachable in practice (only called when resampler.is_some()),
            // but stay safe: emit silence.
            input.read_buf.fill(0);
            0
        }
    };
    Ok(real_frames)
}

/// Write a full period to the output. Retries on transient xrun via
/// `try_recover`; propagates structural errors.
fn write_output(
    pcm: &PCM,
    buf: &[i16],
    xrun_counter: &Arc<AtomicU64>,
    xrun_tx: &Sender<XrunEvent>,
) -> Result<()> {
    let io = pcm.io_i16().context("getting i16 IO handle for output")?;
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
                        anyhow::bail!("output writei returned 0 frames repeatedly");
                    }
                }
            }
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    let count = xrun_counter.fetch_add(1, Ordering::Relaxed) + 1;
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
                    pcm.try_recover(e, true).context("recovering output xrun")?;
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
    fn resampler_lane_not_found_only_warns_when_armed_and_missing() {
        let labels = vec![
            "spotify".to_string(),
            "airplay".to_string(),
            "usbsink".to_string(),
            "correction".to_string(),
        ];
        // Disabled → never warn, regardless of label.
        assert_eq!(resampler_lane_not_found(false, "usbsink", &labels), None);
        assert_eq!(resampler_lane_not_found(false, "nope", &labels), None);
        // Enabled + label present → armed normally, no warning.
        assert_eq!(resampler_lane_not_found(true, "usbsink", &labels), None);
        assert_eq!(resampler_lane_not_found(true, "spotify", &labels), None);
        // Enabled + label absent → warn, returning the available-labels CSV the
        // operator can use to fix the typo.
        assert_eq!(
            resampler_lane_not_found(true, "usbsink_typo", &labels),
            Some("spotify,airplay,usbsink,correction".to_string()),
        );
        // The match is exact (a substring must NOT count as found).
        assert_eq!(
            resampler_lane_not_found(true, "usb", &labels),
            Some("spotify,airplay,usbsink,correction".to_string()),
        );
    }

    #[test]
    fn resampler_ring_frames_derives_floors_and_overrides() {
        let radius = jasper_resampler::RADIUS_FRAMES as usize;
        let min_ring = |target: u32, cushion: u32, period: u32| {
            target as usize + cushion as usize + period as usize + radius + 1
        };

        // requested=0 → derive a 2x burst ring from the ALSA input buffer
        // when that exceeds the structural minimum. The extra capacity is
        // headroom only; it does not change the resampler's held latency target.
        assert_eq!(
            resampler_ring_frames(0, 4096, 512, 256, 256),
            8192,
            "0 derives a 2x burst ring from input_buffer_frames"
        );

        // A non-zero override pins the capacity (the Fix-2 burst-headroom knob),
        // independent of the ALSA input buffer.
        assert_eq!(
            resampler_ring_frames(8192, 4096, 512, 256, 256),
            8192,
            "explicit ring_frames overrides the derived value"
        );

        // Both the derived and the override path floor to the structural minimum
        // so LaneResampler::new can never reject the ring.
        let floor = min_ring(512, 256, 256);
        assert_eq!(
            resampler_ring_frames(0, 64, 512, 256, 256),
            floor,
            "a tiny input buffer floors to the structural minimum"
        );
        assert_eq!(
            resampler_ring_frames(100, 64, 512, 256, 256),
            floor,
            "a tiny explicit override also floors to the structural minimum"
        );

        // The warm-up cushion is part of the minimum (Fix-1 ↔ Fix-2 coupling):
        // a bigger cushion raises the floor.
        assert!(
            resampler_ring_frames(0, 0, 512, 512, 256) > resampler_ring_frames(0, 0, 512, 256, 256),
            "a larger cushion raises the ring floor"
        );
    }

    #[test]
    fn resampler_read_budget_drains_partials_and_caps_pathological_backlog() {
        // The armed lane must pull the final partial period too. A one-period
        // read loop leaves this residue behind and lets the USB snd-aloop lane
        // fill even though the resampler's own ring has room.
        assert_eq!(
            resampler_read_budget_frames(TEST_PERIOD + 17, TEST_PERIOD as usize),
            (TEST_PERIOD + 17) as usize,
        );
        assert_eq!(
            resampler_read_budget_frames(TEST_PERIOD - 1, TEST_PERIOD as usize),
            (TEST_PERIOD - 1) as usize,
        );
        assert_eq!(resampler_read_budget_frames(0, TEST_PERIOD as usize), 0);
        assert_eq!(resampler_read_budget_frames(-1, TEST_PERIOD as usize), 0);

        let cap = (TEST_PERIOD as usize) * (RESAMPLER_MAX_READ_PERIODS as usize);
        assert_eq!(
            resampler_read_budget_frames(10_000 * TEST_PERIOD, TEST_PERIOD as usize),
            cap,
            "read budget must stay bounded on bogus/pathological avail"
        );
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

    // ---- Catch-up resync decision (pure; no ALSA). The production default
    //      period is 256 frames. These pin the constants + the floor/cap math
    //      so a healthy lane never drains and a free-running lane resyncs to
    //      exactly one period without inducing an underrun.

    const TEST_PERIOD: i64 = 256;

    #[test]
    fn catchup_no_drain_at_or_below_high_water() {
        // A DAC-locked lane sits ~1 period; jitter up to (and including) the
        // high-water must NEVER drain — that is the invariant that keeps the
        // networked lanes' behavior unchanged.
        for periods in 0..=CATCHUP_HIGH_WATER_PERIODS {
            assert_eq!(
                catchup_drain_periods(periods * TEST_PERIOD, TEST_PERIOD),
                0,
                "avail={} periods must not drain",
                periods,
            );
        }
    }

    #[test]
    fn catchup_drains_excess_down_to_one_period() {
        // A resync only fires ABOVE the high-water (14 periods); once it does,
        // the WHOLE excess over TARGET is discarded, leaving exactly one period.
        // 15 periods (one over the high-water) → discard 14, leave 1.
        assert_eq!(catchup_drain_periods(15 * TEST_PERIOD, TEST_PERIOD), 14);
        // 16 periods (the full 4096-frame input buffer) → discard 15, leave 1.
        assert_eq!(catchup_drain_periods(16 * TEST_PERIOD, TEST_PERIOD), 15);
    }

    #[test]
    fn catchup_leaves_at_least_target_and_makes_progress() {
        // For every avail above the high-water: after discarding the planned
        // whole periods the remainder is >= target (never an induced underrun)
        // and strictly less than avail (we always make progress).
        let target = CATCHUP_TARGET_PERIODS * TEST_PERIOD;
        for periods in (CATCHUP_HIGH_WATER_PERIODS + 1)..200 {
            let avail = periods * TEST_PERIOD;
            let drained = catchup_drain_periods(avail, TEST_PERIOD);
            assert!(drained > 0, "avail={} must drain", avail);
            let remaining = avail - drained * TEST_PERIOD;
            assert!(
                remaining >= target,
                "avail={} drained={} remaining={} < target={}",
                avail,
                drained,
                remaining,
                target,
            );
        }
    }

    #[test]
    fn catchup_fractional_excess_is_floored() {
        // Just over the high-water by less than a period: the excess over
        // target floors, so we never discard a period we don't fully have
        // and never dip below target.
        let target = CATCHUP_TARGET_PERIODS * TEST_PERIOD;
        let avail = CATCHUP_HIGH_WATER_PERIODS * TEST_PERIOD + (TEST_PERIOD - 1);
        let drained = catchup_drain_periods(avail, TEST_PERIOD);
        let remaining = avail - drained * TEST_PERIOD;
        assert!(
            remaining >= target,
            "remaining={} < target={}",
            remaining,
            target
        );
    }

    #[test]
    fn catchup_is_bounded_by_max() {
        // A pathological backlog caps at MAX so the hot loop can't spin on
        // discard syscalls; the rest finishes over subsequent periods.
        assert_eq!(
            catchup_drain_periods(10_000 * TEST_PERIOD, TEST_PERIOD),
            CATCHUP_MAX_DRAIN_PERIODS,
        );
    }

    #[test]
    fn catchup_zero_or_negative_avail_never_drains() {
        // avail_update can momentarily report 0; a negative (odd driver
        // state) must also be a clean no-op rather than underflow.
        assert_eq!(catchup_drain_periods(0, TEST_PERIOD), 0);
        assert_eq!(catchup_drain_periods(-1, TEST_PERIOD), 0);
        assert_eq!(catchup_drain_periods(-10_000, TEST_PERIOD), 0);
    }

    #[test]
    // The asserts compare named const tuning parameters — that IS the regression
    // guard (a future edit that violates the bracket makes assert!(false) panic).
    // clippy::assertions_on_constants would otherwise flag the const comparison.
    #[allow(clippy::assertions_on_constants)]
    fn catchup_high_water_brackets_burst_stall_occupancy_and_buffer() {
        // Guard the two-sided tuning relationship that keeps the catch-up from
        // (a) clipping a healthy networked lane's peak ring OCCUPANCY, or
        // (b) firing too late to prevent an overrun.
        //
        // Lower bound: reasoned on OCCUPANCY (avail = frames readable on a
        // capture PCM), NOT inter-burst gap time. A healthy AirPlay lane's
        // worst-case peak fill STACKS two effects: an A-MPDU burst deposit
        // (~4 packets ≈ 5.5 periods at 256/48 kHz) plus a scheduling stall that
        // delays our drain (~36.8 ms ≈ 6.9 periods, stressed stock Pi 5;
        // PREEMPT_RT not yet in). Peak ≈ 5.5 + 6.9 ≈ 12.4 periods. The
        // high-water must sit ABOVE that so a healthy burst+stall never trips a
        // resync. Use ceil = 13 periods as the documented ceiling.
        const AIRPLAY_BURST_PERIODS: i64 = 6; // ~5.5, ceil
        const SCHED_STALL_PERIODS: i64 = 7; // ~6.9, ceil (36.8 ms stressed Pi 5)
        const HEALTHY_PEAK_OCCUPANCY_PERIODS: i64 = AIRPLAY_BURST_PERIODS + SCHED_STALL_PERIODS; // 13
        assert!(
            CATCHUP_HIGH_WATER_PERIODS > HEALTHY_PEAK_OCCUPANCY_PERIODS,
            "high_water={} must clear the healthy burst+stall peak occupancy ({} periods)",
            CATCHUP_HIGH_WATER_PERIODS,
            HEALTHY_PEAK_OCCUPANCY_PERIODS,
        );
        // Occupancy at exactly the healthy peak must NOT drain.
        assert_eq!(
            catchup_drain_periods(HEALTHY_PEAK_OCCUPANCY_PERIODS * TEST_PERIOD, TEST_PERIOD),
            0,
            "a healthy burst+stall occupancy peak must never be drop-resynced",
        );

        // Upper bound: the high-water must sit below the default input buffer
        // depth (4096 frames = 16 periods at 256) with margin, so the resync
        // fires before the ring overruns.
        const DEFAULT_INPUT_BUFFER_PERIODS: i64 = 16; // 4096 / 256
        assert!(
            CATCHUP_HIGH_WATER_PERIODS < DEFAULT_INPUT_BUFFER_PERIODS,
            "high_water={} must stay under the input buffer ({} periods)",
            CATCHUP_HIGH_WATER_PERIODS,
            DEFAULT_INPUT_BUFFER_PERIODS,
        );
    }
}
