// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

#![cfg_attr(not(feature = "alsa-runtime"), allow(dead_code, unused_imports))]

//! Rust USB audio bridge for the production low-latency route.
//!
//! The contract is intentionally narrow:
//! - capture S32_LE stereo 48 kHz from the UAC2 gadget ALSA card
//! - write S16_LE stereo 48 kHz into the usbsink fan-in substream
//! - keep only a bounded 2-3 period ring in the audio path
//! - expose preempt/state without letting control-plane work block audio

mod host_clock;
mod impulse_tap;

use std::env;
use std::fs;
#[cfg(feature = "alsa-runtime")]
use std::io::{self, Read, Write};
use std::mem::MaybeUninit;
#[cfg(feature = "alsa-runtime")]
use std::net::{Shutdown, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
#[cfg(feature = "alsa-runtime")]
use std::sync::Mutex;
#[cfg(feature = "alsa-runtime")]
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use host_clock::{HostClock, HostClockConfig};
#[cfg(feature = "alsa-runtime")]
use impulse_tap::{ImpulseDetector, SinkAction, TapSink};
use impulse_tap::{TapConfig, TapEvent, TapState};

#[cfg(feature = "alsa-runtime")]
use alsa::pcm::{Access, Format, HwParams, PCM};
#[cfg(feature = "alsa-runtime")]
use alsa::{Direction, ValueOr};
use anyhow::{bail, Context, Result};
#[cfg(feature = "alsa-runtime")]
use log::{info, warn};
#[cfg(feature = "alsa-runtime")]
use sd_notify::NotifyState;
#[cfg(feature = "alsa-runtime")]
use signal_hook::consts::signal::{SIGINT, SIGTERM};
#[cfg(feature = "alsa-runtime")]
use signal_hook::flag;

const DEFAULT_CAPTURE_DEVICE: &str = "hw:UAC2Gadget";
const DEFAULT_PLAYBACK_DEVICE: &str = "usbsink_substream";
const DEFAULT_STATE_PATH: &str = "/run/jasper-usbsink/state.json";
const DEFAULT_PREEMPT_STATE_PATH: &str = "/run/jasper-usbsink/preempt.state";
const DEFAULT_PREEMPT_HOST: &str = "127.0.0.1";
const DEFAULT_PREEMPT_PORT: u16 = 8781;
const SAMPLE_RATE: u32 = 48_000;
const CHANNELS: u32 = 2;
const DEFAULT_PERIOD_FRAMES: u32 = 256;
const DEFAULT_RING_PERIODS: usize = 3;
const MIN_RING_PERIODS: usize = 2;
const MAX_RING_PERIODS: usize = 3;
const STATE_INTERVAL: Duration = Duration::from_millis(1000);
// Cadence the publisher drains the impulse-tap channel at. Short enough that a
// detection burst can never back up the bounded channel, long enough to stay a
// negligible idle wakeup when disarmed.
const TAP_DRAIN_INTERVAL: Duration = Duration::from_millis(100);
const WATCHDOG_INTERVAL: Duration = Duration::from_millis(5000);
const PLAYING_RMS_DBFS: f64 = -60.0;
// Bounded impulse-tap channel. The single detector fires at most once per
// refractory window (~4/s at the 250 ms default), so this can never fill under
// the harness; it exists purely as a drop-and-count safety net so the audio
// thread's `try_send` is always non-blocking.
const TAP_CHANNEL_CAPACITY: usize = 256;
static STATE_WRITE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug)]
struct Config {
    capture_device: String,
    playback_device: String,
    sample_rate: u32,
    channels: u32,
    period_frames: u32,
    ring_periods: usize,
    state_path: PathBuf,
    preempt_state_path: PathBuf,
    preempt_host: String,
    preempt_port: u16,
    /// Stage 1 host-slaved USB clock config. Default OFF; when disabled the
    /// feature is entirely inert (only the startup pitch neutralize runs, to
    /// heal a crashed predecessor). Daemon-local like every JASPER_USBSINK_*.
    host_clock: HostClockConfig,
    /// USB DIRECT standby mode (C5). When `true` (`JASPER_USBSINK_AUDIO_STANDBY=1`,
    /// exact literal), the bridge SKIPS its audio loop entirely — it opens no
    /// gadget capture and no playback, leaving `hw:UAC2Gadget` free for
    /// jasper-fanin's direct capture. It still runs the preempt HTTP listener,
    /// the state publisher (publishing `standby:true`), and the watchdog. The
    /// host-clock is forced disabled in standby regardless of env (the DLL has
    /// no fill source; fan-in's lane resampler owns all rate matching).
    /// Fail-safe default: anything but the exact literal `1` = today's behavior.
    audio_standby: bool,
}

impl Config {
    fn from_env() -> Result<Self> {
        let period_frames = env_u32("JASPER_USBSINK_BLOCK_FRAMES", DEFAULT_PERIOD_FRAMES)?;
        // Standby (C5): fail-safe — only the exact literal `1` (trimmed) enables.
        let audio_standby = env::var("JASPER_USBSINK_AUDIO_STANDBY")
            .map(|v| v.trim() == "1")
            .unwrap_or(false);
        // In standby the host-clock has no fill source (the audio loop that
        // feeds the DLL never runs), so force it disabled regardless of env —
        // fan-in's lane resampler owns all rate matching in direct mode. The
        // startup + exit neutralize still run (they heal a crashed predecessor
        // and never leave the host slaved).
        let host_clock = if audio_standby {
            host_clock::disabled_config()
        } else {
            host_clock::from_env(|key| env::var(key).ok()).map_err(anyhow::Error::msg)?
        };
        let cfg = Self {
            capture_device: env_string("JASPER_USBSINK_CAPTURE_DEVICE", DEFAULT_CAPTURE_DEVICE),
            playback_device: env_string("JASPER_USBSINK_PLAYBACK_DEVICE", DEFAULT_PLAYBACK_DEVICE),
            sample_rate: env_u32("JASPER_USBSINK_SAMPLE_RATE", SAMPLE_RATE)?,
            channels: env_u32("JASPER_USBSINK_CHANNELS", CHANNELS)?,
            period_frames,
            ring_periods: env_usize("JASPER_USBSINK_RING_PERIODS", DEFAULT_RING_PERIODS)?,
            state_path: PathBuf::from(env_string("JASPER_USBSINK_STATE_PATH", DEFAULT_STATE_PATH)),
            preempt_state_path: PathBuf::from(env_string(
                "JASPER_USBSINK_PREEMPT_STATE_PATH",
                DEFAULT_PREEMPT_STATE_PATH,
            )),
            preempt_host: env_string("JASPER_USBSINK_PREEMPT_HOST", DEFAULT_PREEMPT_HOST),
            preempt_port: env_u16("JASPER_USBSINK_PREEMPT_PORT", DEFAULT_PREEMPT_PORT)?,
            host_clock,
            audio_standby,
        };
        cfg.validate()?;
        Ok(cfg)
    }

    fn validate(&self) -> Result<()> {
        validate_audio_config(
            self.sample_rate,
            self.channels,
            self.period_frames,
            self.ring_periods,
        )
    }

    fn period_samples(&self) -> usize {
        (self.period_frames as usize) * (self.channels as usize)
    }

    /// Whether THIS daemon owns the gadget pitch ctl and may write it (open it,
    /// run the startup/exit neutralize, apply ladder actions).
    ///
    /// True in solo/normal mode — even when the host-clock feature is disabled,
    /// because usbsink still runs the one-shot startup neutralize to heal a
    /// crashed predecessor that left the host slaved. False in STANDBY (combo):
    /// jasper-fanin owns `hw:UAC2Gadget` and its pitch ctl, so usbsink must stay
    /// entirely hands-off — even a neutralize here would reset fan-in's live
    /// command behind its back on every clean stop/start cycle (P2-F1).
    fn owns_host_clock_ctl(&self) -> bool {
        !self.audio_standby
    }
}

fn validate_audio_config(
    sample_rate: u32,
    channels: u32,
    period_frames: u32,
    ring_periods: usize,
) -> Result<()> {
    if sample_rate != SAMPLE_RATE {
        bail!("JASPER_USBSINK_SAMPLE_RATE must be 48000 for usb_low_latency_48k");
    }
    if channels != CHANNELS {
        bail!("JASPER_USBSINK_CHANNELS must be stereo (2)");
    }
    if period_frames == 0 || period_frames % 2 != 0 {
        bail!("JASPER_USBSINK_BLOCK_FRAMES must be a positive period-aligned even frame count");
    }
    if !(MIN_RING_PERIODS..=MAX_RING_PERIODS).contains(&ring_periods) {
        bail!("JASPER_USBSINK_RING_PERIODS must be 2 or 3");
    }
    Ok(())
}

fn env_string(key: &str, default: &str) -> String {
    env::var(key)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_u32(key: &str, default: u32) -> Result<u32> {
    match env::var(key) {
        Ok(value) if !value.trim().is_empty() => value
            .trim()
            .parse::<u32>()
            .with_context(|| format!("parsing {key}")),
        _ => Ok(default),
    }
}

fn env_u16(key: &str, default: u16) -> Result<u16> {
    match env::var(key) {
        Ok(value) if !value.trim().is_empty() => value
            .trim()
            .parse::<u16>()
            .with_context(|| format!("parsing {key}")),
        _ => Ok(default),
    }
}

fn env_usize(key: &str, default: usize) -> Result<usize> {
    match env::var(key) {
        Ok(value) if !value.trim().is_empty() => value
            .trim()
            .parse::<usize>()
            .with_context(|| format!("parsing {key}")),
        _ => Ok(default),
    }
}

#[derive(Debug)]
struct PeriodRing {
    buf: Vec<i16>,
    period_samples: usize,
    periods: usize,
    read_period: usize,
    write_period: usize,
    len_periods: usize,
    overflow_events: u64,
    dropped_periods: u64,
    underflow_events: u64,
}

impl PeriodRing {
    fn new(period_samples: usize, periods: usize) -> Result<Self> {
        if period_samples == 0 {
            bail!("period_samples must be non-zero");
        }
        if !(MIN_RING_PERIODS..=MAX_RING_PERIODS).contains(&periods) {
            bail!("ring periods must be 2 or 3");
        }
        Ok(Self {
            buf: vec![0; period_samples * periods],
            period_samples,
            periods,
            read_period: 0,
            write_period: 0,
            len_periods: 0,
            overflow_events: 0,
            dropped_periods: 0,
            underflow_events: 0,
        })
    }

    fn fill_periods(&self) -> usize {
        self.len_periods
    }

    fn free_periods(&self) -> usize {
        self.periods.saturating_sub(self.len_periods)
    }

    fn clear(&mut self) -> usize {
        let dropped = self.len_periods;
        self.read_period = self.write_period;
        self.len_periods = 0;
        dropped
    }

    fn push_period(&mut self, period: &[i16]) -> Result<()> {
        if period.len() != self.period_samples {
            bail!(
                "period length {} does not match expected {}",
                period.len(),
                self.period_samples
            );
        }
        if self.len_periods == self.periods {
            self.read_period = (self.read_period + 1) % self.periods;
            self.len_periods -= 1;
            self.overflow_events += 1;
            self.dropped_periods += 1;
        }
        let start = self.write_period * self.period_samples;
        let end = start + self.period_samples;
        self.buf[start..end].copy_from_slice(period);
        self.write_period = (self.write_period + 1) % self.periods;
        self.len_periods += 1;
        Ok(())
    }

    fn pop_or_silence(&mut self, out: &mut [i16]) -> bool {
        if out.len() != self.period_samples {
            out.fill(0);
            self.underflow_events += 1;
            return false;
        }
        if self.len_periods == 0 {
            out.fill(0);
            self.underflow_events += 1;
            return false;
        }
        let start = self.read_period * self.period_samples;
        let end = start + self.period_samples;
        out.copy_from_slice(&self.buf[start..end]);
        self.read_period = (self.read_period + 1) % self.periods;
        self.len_periods -= 1;
        true
    }
}

#[derive(Debug)]
struct PeriodAssembler {
    buf: Vec<i16>,
    period_frames: usize,
    channels: usize,
    filled_frames: usize,
}

impl PeriodAssembler {
    fn new(period_frames: usize, channels: usize) -> Result<Self> {
        if period_frames == 0 {
            bail!("period_frames must be non-zero");
        }
        if channels == 0 {
            bail!("channels must be non-zero");
        }
        Ok(Self {
            buf: vec![0; period_frames * channels],
            period_frames,
            channels,
            filled_frames: 0,
        })
    }

    #[cfg(test)]
    fn filled_frames(&self) -> usize {
        self.filled_frames
    }

    fn push_frames<F>(
        &mut self,
        frames: &[i16],
        frame_count: usize,
        mut on_period: F,
    ) -> Result<usize>
    where
        F: FnMut(&[i16]) -> Result<()>,
    {
        let sample_count = frame_count
            .checked_mul(self.channels)
            .context("capture frame sample count overflow")?;
        if sample_count > frames.len() {
            bail!(
                "capture slice has {} samples, need {}",
                frames.len(),
                sample_count
            );
        }

        let mut src_frame = 0usize;
        let mut completed = 0usize;
        while src_frame < frame_count {
            let dst_free_frames = self.period_frames - self.filled_frames;
            let copy_frames = dst_free_frames.min(frame_count - src_frame);
            let src_start = src_frame * self.channels;
            let src_end = src_start + copy_frames * self.channels;
            let dst_start = self.filled_frames * self.channels;
            let dst_end = dst_start + copy_frames * self.channels;

            self.buf[dst_start..dst_end].copy_from_slice(&frames[src_start..src_end]);
            self.filled_frames += copy_frames;
            src_frame += copy_frames;

            if self.filled_frames == self.period_frames {
                on_period(&self.buf)?;
                self.filled_frames = 0;
                completed += 1;
            }
        }

        Ok(completed)
    }
}

#[derive(Default)]
struct SharedState {
    preempted: AtomicBool,
    playing: AtomicBool,
    host_connected: AtomicBool,
    rms_dbfs_x100: AtomicI32,
    ring_fill_periods: AtomicUsize,
    ring_capacity_periods: AtomicUsize,
    capture_xruns: AtomicU64,
    capture_partial_reads: AtomicU64,
    playback_xruns: AtomicU64,
    underflow_periods: AtomicU64,
    overflow_events: AtomicU64,
    dropped_periods: AtomicU64,
    preempt_silence_periods: AtomicU64,
    preempt_dropped_periods: AtomicU64,
    capture_frames: AtomicU64,
    playback_frames: AtomicU64,
    last_progress_epoch_ms: AtomicU64,
}

impl SharedState {
    fn new(ring_capacity_periods: usize) -> Self {
        let state = Self::default();
        state
            .ring_capacity_periods
            .store(ring_capacity_periods, Ordering::Relaxed);
        state
            .rms_dbfs_x100
            .store((-120.0_f64 * 100.0) as i32, Ordering::Relaxed);
        state
    }

    fn mark_progress(&self) {
        self.last_progress_epoch_ms
            .store(epoch_millis(), Ordering::Relaxed);
    }
}

/// Cross-thread handles for the impulse tap.
///
/// - `state` is the lock-free surface the audio thread reads (armed + detector
///   knobs) and the observable counters (`GET /tap`, `status_json`).
/// - `config` is the last-armed [`TapConfig`], written by the preempt listener
///   on arm and read by the publisher when it notices a new arm generation. The
///   audio thread NEVER locks this — it uses only the atomics on `state`.
///
/// The `SyncSender<TapEvent>` (audio → publisher) and its `Receiver` are held
/// separately because they are not clonable into an `Arc` bundle.
struct TapShared {
    state: Arc<TapState>,
    #[cfg(feature = "alsa-runtime")]
    config: Arc<Mutex<TapConfig>>,
}

impl TapShared {
    fn new() -> Self {
        Self {
            state: Arc::new(TapState::default()),
            #[cfg(feature = "alsa-runtime")]
            config: Arc::new(Mutex::new(TapConfig::default())),
        }
    }
}

/// Cross-thread publication of the host-clock telemetry fragment.
///
/// The [`HostClock`] ladder/servo lives on — and is written ONLY by — the
/// state-publisher thread (single writer, structurally: the pitch ctl handle
/// and the `HostClock` never leave that thread). But `state.json` is also
/// rendered by the preempt-listener thread (`GET /status`) and once from
/// `main`/the audio loop at startup. Those threads MUST NOT touch the
/// `HostClock`; instead the publisher renders the block once per state write
/// and stores the string here, and every `status_json` caller reads this
/// snapshot. Absent a publisher write yet, the initial value is the disabled
/// block so the first state.json is coherent.
#[cfg(feature = "alsa-runtime")]
struct HostClockShared {
    fragment: Arc<Mutex<String>>,
}

#[cfg(feature = "alsa-runtime")]
impl HostClockShared {
    fn new(cfg: HostClockConfig) -> Self {
        // Render the initial (disabled/idle) block so the very first state.json
        // write — before the publisher ticks — carries a definite host_clock.
        let initial = HostClock::new(cfg).status_fragment();
        Self {
            fragment: Arc::new(Mutex::new(initial)),
        }
    }

    /// The current fragment snapshot for a `status_json` caller (startup +
    /// audio-loop paths). A poisoned lock is recovered into rather than
    /// crashing the control plane — the telemetry is best-effort.
    fn snapshot(&self) -> String {
        read_host_clock_fragment(&self.fragment)
    }
}

/// Read the shared host-clock fragment, recovering into a poisoned lock rather
/// than crashing (best-effort telemetry). Used by every reader thread that
/// holds only the raw `Arc<Mutex<String>>` (preempt listener, audio loop).
#[cfg(feature = "alsa-runtime")]
fn read_host_clock_fragment(fragment: &Mutex<String>) -> String {
    match fragment.lock() {
        Ok(guard) => guard.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

/// Store a freshly-rendered host-clock fragment (publisher thread only, the
/// single writer). Recovers into a poisoned lock rather than crashing.
#[cfg(feature = "alsa-runtime")]
fn publish_host_clock_fragment(fragment: &Mutex<String>, rendered: String) {
    let mut guard = fragment
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    *guard = rendered;
}

// S32→S16 UAC2 narrowing is owned by the pure `jasper-resampler` crate so this
// bridge and jasper-fanin's direct capture narrow bit-identically by
// construction (C2). The runtime path calls `jasper_resampler::convert_s32_to_s16`
// directly; the scalar `s32_high_word_to_s16` is imported only where the pinned
// sign-boundary test uses it (in the test module below).

fn stage_capture_period(ring: &mut PeriodRing, period: &[i16], state: &SharedState) -> Result<()> {
    if state.preempted.load(Ordering::Relaxed) {
        state
            .preempt_dropped_periods
            .fetch_add(1, Ordering::Relaxed);
        Ok(())
    } else {
        ring.push_period(period)
    }
}

fn flush_ring_for_preempt(ring: &mut PeriodRing, state: &SharedState) {
    let flushed = ring.clear();
    if flushed > 0 {
        state
            .preempt_dropped_periods
            .fetch_add(flushed as u64, Ordering::Relaxed);
    }
}

/// Audio-thread impulse tap over one freshly-converted capture read.
///
/// Runs inline in the capture loop over the S16 slice, before it is staged into
/// the ring — this is the route's own ingress, so the timestamp binds to route
/// identity. The CALLER owns the `tap.armed()` gate (the single relaxed atomic
/// load that is the whole disarmed-path cost) and only invokes this when armed;
/// it also owns resetting the local detector to `None` on the disarm transition.
/// When armed this is pure arithmetic plus a non-blocking `try_send`; on a full
/// channel it drops-and-counts and never blocks the audio thread. Detector
/// params are reloaded lock-free only when a new arm generation is observed.
///
/// - `read_frames`: frames returned by this capture read.
/// - `read_start_frame`: cumulative captured frames BEFORE this read (the
///   detector's `period_start_frame`, so refractory anchoring is stable across
///   reads of any size).
/// - `read_ns`: `CLOCK_MONOTONIC` ns taken immediately after the read returned.
/// - `ring_fill_periods` / `period_frames`: ring backlog at the tap moment,
///   recorded as frames for the harness latency term.
#[cfg(feature = "alsa-runtime")]
#[allow(clippy::too_many_arguments)]
fn tap_over_read(
    tap: &TapState,
    detector: &mut Option<ImpulseDetector>,
    last_generation: &mut u64,
    sender: &std::sync::mpsc::SyncSender<TapEvent>,
    converted: &[i16],
    read_frames: usize,
    read_start_frame: u64,
    read_ns: i128,
    ring_fill_periods: usize,
    period_frames: u32,
) {
    // Reload detector params only when a fresh arm bumped the generation. The
    // Acquire load pairs with `TapState::arm`'s Release so the knobs are
    // visible.
    let generation = tap.generation_acquire();
    if detector.is_none() || generation != *last_generation {
        let (threshold, hysteresis, refractory_frames) = tap.detector_knobs();
        *detector = Some(ImpulseDetector::new(
            threshold,
            hysteresis,
            refractory_frames,
            CHANNELS as usize,
        ));
        *last_generation = generation;
    }
    let Some(detector) = detector.as_mut() else {
        return;
    };
    let Some(hit) = detector.detect(
        &converted[..read_frames * (CHANNELS as usize)],
        read_start_frame,
    ) else {
        return;
    };
    let event = TapEvent {
        monotonic_ns: impulse_tap::detection_monotonic_ns(
            read_ns,
            read_frames,
            hit.sample_offset_frames,
            SAMPLE_RATE,
        ),
        frame_index: read_start_frame.saturating_add(hit.sample_offset_frames as u64),
        ring_fill_frames: impulse_tap::ring_fill_frames(ring_fill_periods, period_frames),
        peak: hit.peak,
    };
    // Non-blocking hand-off to the publisher; drop-and-count on a full channel.
    if sender.try_send(event).is_err() {
        tap.note_dropped();
    }
}

fn convert_s32_to_s16(input: &[i32], output: &mut [i16]) -> Result<()> {
    // Delegates to the shared `jasper-resampler` narrowing (C2 parity by
    // construction); keeps the `Result` shape for the `?` capture call site.
    if !jasper_resampler::convert_s32_to_s16(input, output) {
        bail!("input/output sample slices must match");
    }
    Ok(())
}

fn rms_dbfs_i16(samples: &[i16]) -> f64 {
    if samples.is_empty() {
        return -120.0;
    }
    let sum_sq: f64 = samples
        .iter()
        .map(|sample| {
            let normalized = (*sample as f64) / 32768.0;
            normalized * normalized
        })
        .sum();
    let rms = (sum_sq / (samples.len() as f64)).sqrt();
    if rms <= 1.0e-9 {
        -120.0
    } else {
        (20.0 * rms.log10()).max(-120.0)
    }
}

#[cfg(feature = "alsa-runtime")]
fn open_capture(config: &Config) -> Result<PCM> {
    let pcm = PCM::new(&config.capture_device, Direction::Capture, true)
        .with_context(|| format!("opening capture PCM {}", config.capture_device))?;
    configure_pcm(
        &pcm,
        &config.capture_device,
        Direction::Capture,
        Format::S32LE,
        config,
    )?;
    pcm.start()
        .with_context(|| format!("starting capture PCM {}", config.capture_device))?;
    Ok(pcm)
}

#[cfg(feature = "alsa-runtime")]
fn open_playback(config: &Config) -> Result<PCM> {
    let pcm = PCM::new(&config.playback_device, Direction::Playback, false)
        .with_context(|| format!("opening playback PCM {}", config.playback_device))?;
    let negotiated = configure_pcm(
        &pcm,
        &config.playback_device,
        Direction::Playback,
        Format::S16LE,
        config,
    )?;
    configure_playback_sw_params(&pcm, &config.playback_device, negotiated.buffer_frames)?;
    Ok(pcm)
}

#[cfg(feature = "alsa-runtime")]
#[derive(Clone, Copy, Debug)]
struct NegotiatedPcm {
    buffer_frames: u32,
}

#[cfg(feature = "alsa-runtime")]
fn configure_pcm(
    pcm: &PCM,
    pcm_name: &str,
    direction: Direction,
    format: Format,
    config: &Config,
) -> Result<NegotiatedPcm> {
    let negotiated_rate;
    let negotiated_period;
    let negotiated_buffer;
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(config.channels)
            .with_context(|| format!("set_channels({})", config.channels))?;
        hwp.set_rate(config.sample_rate, ValueOr::Nearest)
            .with_context(|| format!("set_rate({})", config.sample_rate))?;
        hwp.set_format(format)
            .with_context(|| format!("set_format({format:?})"))?;
        hwp.set_access(Access::RWInterleaved)
            .context("set_access(RWInterleaved)")?;
        hwp.set_period_size(config.period_frames as i64, ValueOr::Nearest)
            .with_context(|| format!("set_period_size({})", config.period_frames))?;
        let requested_buffer = (config.period_frames as i64) * (config.ring_periods as i64);
        hwp.set_buffer_size_near(requested_buffer)
            .with_context(|| {
                format!(
                    "set_buffer_size_near({})",
                    config.period_frames * (config.ring_periods as u32)
                )
            })?;
        negotiated_rate = hwp.get_rate().context("get_rate")?;
        negotiated_period = hwp.get_period_size().context("get_period_size")? as u32;
        negotiated_buffer = hwp.get_buffer_size().context("get_buffer_size")? as u32;
        pcm.hw_params(&hwp).context("installing HwParams")?;
        if negotiated_buffer != requested_buffer as u32 {
            warn!(
                "event=usbsink_audio.alsa_buffer_near direction={direction:?} pcm={pcm_name} requested_frames={requested_buffer} negotiated_frames={negotiated_buffer}"
            );
        }
    }
    if negotiated_rate != config.sample_rate {
        bail!("{direction:?} PCM {pcm_name} negotiated {negotiated_rate} Hz, expected 48000");
    }
    if negotiated_period != config.period_frames {
        bail!(
            "{direction:?} PCM {pcm_name} negotiated period {negotiated_period}, expected {}",
            config.period_frames
        );
    }
    if negotiated_buffer < config.period_frames.saturating_mul(2) {
        bail!(
            "{direction:?} PCM {pcm_name} negotiated buffer {negotiated_buffer}, expected at least 2 periods"
        );
    }
    if negotiated_buffer % negotiated_period != 0 {
        bail!(
            "{direction:?} PCM {pcm_name} negotiated buffer {negotiated_buffer}, not period-aligned to {negotiated_period}"
        );
    }
    Ok(NegotiatedPcm {
        buffer_frames: negotiated_buffer,
    })
}

#[cfg(feature = "alsa-runtime")]
fn configure_playback_sw_params(pcm: &PCM, pcm_name: &str, buffer_frames: u32) -> Result<()> {
    let swp = pcm
        .sw_params_current()
        .with_context(|| format!("reading playback SwParams for {pcm_name}"))?;
    // ALSA's default start_threshold can be 1 frame on snd-aloop. After an
    // underrun recovery that restarts the stream nearly empty, causing repeated
    // playback xruns even though the downstream fan-in resampler masks them.
    // Start only once the negotiated playback buffer is full; steady-state
    // writes remain period-paced by ALSA.
    swp.set_start_threshold(buffer_frames as i64)
        .with_context(|| format!("setting playback start_threshold for {pcm_name}"))?;
    pcm.sw_params(&swp)
        .with_context(|| format!("installing playback SwParams for {pcm_name}"))
}

#[cfg(feature = "alsa-runtime")]
fn read_capture_frames(
    pcm: &PCM,
    input: &mut [i32],
    converted: &mut [i16],
    state: &SharedState,
) -> Result<Option<usize>> {
    let io = pcm
        .io_i32()
        .context("getting i32 IO handle for UAC2 capture")?;
    match io.readi(input) {
        Ok(frames) => {
            if frames == 0 {
                return Ok(None);
            }
            let samples = frames * (CHANNELS as usize);
            convert_s32_to_s16(&input[..samples], &mut converted[..samples])?;
            if samples < converted.len() {
                state.capture_partial_reads.fetch_add(1, Ordering::Relaxed);
            }
            state
                .capture_frames
                .fetch_add(frames as u64, Ordering::Relaxed);
            Ok(Some(frames))
        }
        Err(e) => {
            let errno = e.errno();
            if errno == libc::EAGAIN {
                return Ok(None);
            }
            if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                let count = state.capture_xruns.fetch_add(1, Ordering::Relaxed) + 1;
                warn!(
                    "event=usbsink_audio.capture_xrun count={} errno={}",
                    count, errno
                );
                pcm.try_recover(e, true)
                    .context("recovering capture xrun")?;
                return Ok(None);
            }
            Err(e).context("reading UAC2 capture PCM")
        }
    }
}

#[cfg(feature = "alsa-runtime")]
fn write_playback_period(pcm: &PCM, output: &[i16], state: &SharedState) -> Result<()> {
    let io = pcm
        .io_i16()
        .context("getting i16 IO handle for usbsink playback")?;
    let frames_total = output.len() / (CHANNELS as usize);
    let mut frames_done = 0usize;
    let mut recoveries = 0u32;
    while frames_done < frames_total {
        let offset = frames_done * (CHANNELS as usize);
        match io.writei(&output[offset..]) {
            Ok(frames) => {
                frames_done += frames;
                if frames == 0 {
                    recoveries += 1;
                    if recoveries > 3 {
                        bail!("playback writei returned 0 frames repeatedly");
                    }
                }
            }
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    let count = state.playback_xruns.fetch_add(1, Ordering::Relaxed) + 1;
                    warn!(
                        "event=usbsink_audio.playback_xrun count={} errno={}",
                        count, errno
                    );
                    pcm.try_recover(e, true)
                        .context("recovering playback xrun")?;
                    recoveries += 1;
                    if recoveries > 3 {
                        bail!("playback xrun recovery exceeded limit");
                    }
                } else {
                    return Err(e).context("writing usbsink playback PCM");
                }
            }
        }
    }
    state
        .playback_frames
        .fetch_add(frames_total as u64, Ordering::Relaxed);
    Ok(())
}

#[cfg(feature = "alsa-runtime")]
#[allow(clippy::too_many_arguments)]
fn run_audio_loop(
    config: &Config,
    state: &Arc<SharedState>,
    tap: &Arc<TapState>,
    tap_config: &Arc<Mutex<TapConfig>>,
    host_clock_fragment: &Arc<Mutex<String>>,
    tap_sender: &std::sync::mpsc::SyncSender<TapEvent>,
    shutdown: &Arc<AtomicBool>,
) -> Result<()> {
    let capture = open_capture(config)?;
    let playback = open_playback(config)?;
    let period_samples = config.period_samples();
    let mut captured_s32 = vec![0i32; period_samples];
    let mut converted_i16 = vec![0i16; period_samples];
    let mut output_i16 = vec![0i16; period_samples];
    let mut ring = PeriodRing::new(period_samples, config.ring_periods)?;
    let mut capture_assembler =
        PeriodAssembler::new(config.period_frames as usize, config.channels as usize)?;
    // Audio-thread-local impulse-tap state: rebuilt on each arm, never shared.
    let mut tap_detector: Option<ImpulseDetector> = None;
    let mut tap_last_generation: u64 = 0;
    let mut capture_frames_cursor: u64 = 0;

    info!(
        "event=usbsink_audio.started capture_device={} playback_device={} sample_rate={} channels={} period_frames={} ring_periods={}",
        config.capture_device,
        config.playback_device,
        config.sample_rate,
        config.channels,
        config.period_frames,
        config.ring_periods,
    );
    state.host_connected.store(true, Ordering::Relaxed);
    let hc_snapshot = read_host_clock_fragment(host_clock_fragment);
    write_state_json(config, state, tap, tap_config, &hc_snapshot)?;
    let _ = sd_notify::notify(&[NotifyState::Ready]);
    state.mark_progress();

    while !shutdown.load(Ordering::Relaxed) {
        while ring.free_periods() > 0 {
            let Some(frames) =
                read_capture_frames(&capture, &mut captured_s32, &mut converted_i16, state)?
            else {
                break;
            };
            // Disarmed-cost invariant: one relaxed atomic load per period and
            // nothing else — in particular NOT the `monotonic_ns()` vDSO read,
            // which is taken only inside the armed branch (immediately after the
            // read returns, with just this atomic load intervening, so the
            // sample-accurate timestamp is unaffected). When armed, run the
            // impulse tap over this read's converted slice BEFORE it is staged —
            // the route's own ingress. Ring fill here is the pre-read backlog the
            // click must still drain through.
            if tap.armed() {
                let read_ns = monotonic_ns();
                tap_over_read(
                    tap,
                    &mut tap_detector,
                    &mut tap_last_generation,
                    tap_sender,
                    &converted_i16,
                    frames,
                    capture_frames_cursor,
                    read_ns,
                    ring.fill_periods(),
                    config.period_frames,
                );
            } else if tap_detector.is_some() {
                // Drop stale latch/refractory state across an arm boundary so a
                // fresh arm starts clean. Cheap and only runs on the transition.
                tap_detector = None;
            }
            capture_frames_cursor = capture_frames_cursor.saturating_add(frames as u64);
            let samples = frames * (config.channels as usize);
            capture_assembler.push_frames(&converted_i16[..samples], frames, |period| {
                let rms = rms_dbfs_i16(period);
                state
                    .rms_dbfs_x100
                    .store((rms * 100.0).round() as i32, Ordering::Relaxed);
                state
                    .playing
                    .store(rms > PLAYING_RMS_DBFS, Ordering::Relaxed);
                stage_capture_period(&mut ring, period, state)
            })?;
        }

        if state.preempted.load(Ordering::Relaxed) {
            flush_ring_for_preempt(&mut ring, state);
            output_i16.fill(0);
            state
                .preempt_silence_periods
                .fetch_add(1, Ordering::Relaxed);
        } else if !ring.pop_or_silence(&mut output_i16) {
            state.underflow_periods.fetch_add(1, Ordering::Relaxed);
        }

        state
            .ring_fill_periods
            .store(ring.fill_periods(), Ordering::Relaxed);
        state
            .overflow_events
            .store(ring.overflow_events, Ordering::Relaxed);
        state
            .dropped_periods
            .store(ring.dropped_periods, Ordering::Relaxed);
        write_playback_period(&playback, &output_i16, state)?;
        state.mark_progress();
    }
    let _ = sd_notify::notify(&[NotifyState::Stopping]);
    Ok(())
}

/// The STANDBY liveness loop (C5). Runs in place of `run_audio_loop` when
/// `JASPER_USBSINK_AUDIO_STANDBY=1`. It opens NO PCM (fan-in owns the gadget),
/// but MUST satisfy the same `Type=notify` + `WatchdogSec=15s` liveness contract
/// the audio loop does: it sends `READY=1` once (systemd blocks unit start until
/// this arrives), and drives `state.mark_progress()` on a cadence < the watchdog
/// interval so the `start_watchdog` thread keeps patting `WATCHDOG=1` (it only
/// pats when the progress sentinel is fresh) — without this the unit would
/// kill-loop. The state publisher, preempt listener, and watchdog threads run
/// unchanged; they publish `standby:true` and answer HTTP normally.
#[cfg(feature = "alsa-runtime")]
fn run_standby_loop(state: &Arc<SharedState>, shutdown: &Arc<AtomicBool>) -> Result<()> {
    info!("event=usbsink_audio.standby active=true (fan-in owns hw:UAC2Gadget; no PCM opened)");
    // host_connected is published best-effort from sysfs by the state publisher;
    // here we only own liveness. Send READY immediately so systemd unblocks.
    let _ = sd_notify::notify(&[NotifyState::Ready]);
    state.mark_progress();
    // Pat cadence well under WatchdogSec (15 s): 1 s matches the state-write
    // rhythm and keeps the sentinel fresh with wide margin.
    while !shutdown.load(Ordering::Relaxed) {
        state.mark_progress();
        thread::sleep(STATE_INTERVAL);
    }
    let _ = sd_notify::notify(&[NotifyState::Stopping]);
    Ok(())
}

#[cfg(feature = "alsa-runtime")]
fn bind_preempt_listener(config: &Config) -> Result<TcpListener> {
    let addr = format!("{}:{}", config.preempt_host, config.preempt_port);
    let listener =
        TcpListener::bind(&addr).with_context(|| format!("binding preempt listener {addr}"))?;
    listener
        .set_nonblocking(true)
        .with_context(|| format!("setting {addr} nonblocking"))?;
    info!("event=usbsink_audio.preempt_listening addr={addr}");
    Ok(listener)
}

#[cfg(feature = "alsa-runtime")]
#[allow(clippy::too_many_arguments)]
fn run_preempt_listener(
    config: Config,
    state: Arc<SharedState>,
    tap: Arc<TapState>,
    tap_config: Arc<Mutex<TapConfig>>,
    host_clock_fragment: Arc<Mutex<String>>,
    shutdown: Arc<AtomicBool>,
    listener: TcpListener,
) -> Result<()> {
    while !shutdown.load(Ordering::Relaxed) {
        match listener.accept() {
            Ok((stream, _peer)) => {
                let hc_snapshot = read_host_clock_fragment(&host_clock_fragment);
                if let Err(e) =
                    handle_preempt_request(stream, &config, &state, &tap, &tap_config, &hc_snapshot)
                {
                    warn!("event=usbsink_audio.preempt_error detail={}", e);
                }
            }
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(50));
            }
            Err(e) => return Err(e).context("accepting preempt request"),
        }
    }
    Ok(())
}

#[cfg(feature = "alsa-runtime")]
fn handle_preempt_request(
    mut stream: TcpStream,
    config: &Config,
    state: &SharedState,
    tap: &TapState,
    tap_config: &Mutex<TapConfig>,
    host_clock_fragment: &str,
) -> Result<()> {
    stream
        .set_read_timeout(Some(Duration::from_millis(250)))
        .context("setting preempt read timeout")?;
    let mut buf = [0u8; 2048];
    let n = stream.read(&mut buf).context("reading preempt request")?;
    let body = String::from_utf8_lossy(&buf[..n]);
    if body.starts_with("GET /status ") || body.starts_with("GET /preempt ") {
        let rms_dbfs = (state.rms_dbfs_x100.load(Ordering::Relaxed) as f64) / 100.0;
        let tap_fragment = tap.status_fragment(&tap_config_snapshot(tap_config));
        let status = status_json(config, state, rms_dbfs, &tap_fragment, host_clock_fragment);
        write_http_json(&mut stream, 200, &status)?;
        let _ = stream.shutdown(Shutdown::Both);
        return Ok(());
    }
    if body.starts_with("GET /tap ") {
        let tap_body = tap.status_body(&tap_config_snapshot(tap_config));
        write_http_json(&mut stream, 200, &tap_body)?;
        let _ = stream.shutdown(Shutdown::Both);
        return Ok(());
    }
    if body.starts_with("POST /tap/arm ") {
        return handle_tap_arm(&mut stream, &body, config, tap, tap_config);
    }
    if body.starts_with("POST /tap/disarm ") {
        return handle_tap_disarm(&mut stream, tap, tap_config);
    }
    let silenced = parse_preempt_silenced(&body);
    match silenced {
        Some(value) => {
            state.preempted.store(value, Ordering::Relaxed);
            write_preempt_state(&config.preempt_state_path, value)?;
            write_http_json(&mut stream, 200, "{\"ok\":true}\n")?;
        }
        None => {
            write_http_json(&mut stream, 400, "{\"ok\":false}\n")?;
        }
    }
    let _ = stream.shutdown(Shutdown::Both);
    Ok(())
}

/// Clone the last-armed tap config under its lock (listener/publisher only —
/// never the audio thread). A poisoned lock is recovered into rather than
/// crashing the control plane; the tap params are best-effort observability.
#[cfg(feature = "alsa-runtime")]
fn tap_config_snapshot(tap_config: &Mutex<TapConfig>) -> TapConfig {
    match tap_config.lock() {
        Ok(guard) => guard.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

#[cfg(feature = "alsa-runtime")]
fn handle_tap_arm(
    stream: &mut TcpStream,
    body: &str,
    config: &Config,
    tap: &TapState,
    tap_config: &Mutex<TapConfig>,
) -> Result<()> {
    let Some(request_body) = http_request_body(body) else {
        write_http_json(stream, 400, "{\"ok\":false,\"error\":\"missing body\"}\n")?;
        let _ = stream.shutdown(Shutdown::Both);
        return Ok(());
    };
    let Some(cfg) = TapConfig::from_arm_body(request_body) else {
        write_http_json(stream, 400, "{\"ok\":false,\"error\":\"bad arm params\"}\n")?;
        let _ = stream.shutdown(Shutdown::Both);
        return Ok(());
    };
    // Truncate the JSONL artifact synchronously so the arm reply only claims
    // success once the file is a clean slate. The publisher then appends.
    if let Err(e) = truncate_tap_file(&cfg.path) {
        warn!(
            "event=usbsink_audio.tap_arm_error detail={} path={}",
            e,
            cfg.path.display()
        );
        let msg = format!(
            "{{\"ok\":false,\"error\":\"cannot open path\",\"path\":\"{}\"}}\n",
            json_escape(&cfg.path.to_string_lossy())
        );
        write_http_json(stream, 500, &msg)?;
        let _ = stream.shutdown(Shutdown::Both);
        return Ok(());
    }
    // Publish config for the publisher first, then flip armed via `arm`.
    {
        let mut guard = tap_config
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *guard = cfg.clone();
    }
    tap.arm(&cfg, config.sample_rate, epoch_millis());
    info!(
        "event=usbsink_audio.tap_armed threshold={:.3} hysteresis={:.3} refractory_ms={} max_events={} auto_disarm_min={} path={}",
        cfg.threshold,
        cfg.hysteresis,
        cfg.refractory_ms,
        cfg.max_events,
        cfg.auto_disarm_min,
        cfg.path.display(),
    );
    let reply = format!(
        "{{\"ok\":true,\"armed\":true,\"path\":\"{}\"}}\n",
        json_escape(&cfg.path.to_string_lossy())
    );
    write_http_json(stream, 200, &reply)?;
    let _ = stream.shutdown(Shutdown::Both);
    Ok(())
}

#[cfg(feature = "alsa-runtime")]
fn handle_tap_disarm(
    stream: &mut TcpStream,
    tap: &TapState,
    tap_config: &Mutex<TapConfig>,
) -> Result<()> {
    tap.disarm();
    let written = tap.events_written();
    let dropped = tap.events_dropped();
    info!(
        "event=usbsink_audio.tap_disarmed events_written={} events_dropped={} path={}",
        written,
        dropped,
        tap_config_snapshot(tap_config).path.display(),
    );
    let reply = format!(
        "{{\"ok\":true,\"armed\":false,\"events_written\":{},\"events_dropped\":{}}}\n",
        written, dropped,
    );
    write_http_json(stream, 200, &reply)?;
    let _ = stream.shutdown(Shutdown::Both);
    Ok(())
}

/// Truncate (or create) the tap JSONL artifact under its tmpfs dir. Called from
/// the listener on arm so the reply reflects a clean slate. The publisher holds
/// the write handle after this; here we only create the parent + zero the file.
#[cfg(feature = "alsa-runtime")]
fn truncate_tap_file(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    }
    fs::write(path, b"").with_context(|| format!("truncating {}", path.display()))
}

fn parse_preempt_silenced(request: &str) -> Option<bool> {
    let body = http_request_body(request)?;
    let value: serde_json::Value = serde_json::from_str(body.trim()).ok()?;
    value.get("silenced")?.as_bool()
}

fn http_request_body(request: &str) -> Option<&str> {
    request
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .or_else(|| request.split_once("\n\n").map(|(_, body)| body))
}

#[cfg(feature = "alsa-runtime")]
fn write_http_json(stream: &mut TcpStream, status: u16, body: &str) -> Result<()> {
    let reason = if status == 200 { "OK" } else { "Bad Request" };
    let header = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream
        .write_all(header.as_bytes())
        .context("writing HTTP header")?;
    stream
        .write_all(body.as_bytes())
        .context("writing HTTP body")
}

#[cfg(feature = "alsa-runtime")]
fn write_preempt_state(path: &Path, silenced: bool) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    }
    let body = if silenced {
        "{\"silenced\":true}\n"
    } else {
        "{\"silenced\":false}\n"
    };
    fs::write(path, body).with_context(|| format!("writing {}", path.display()))
}

/// Best-effort host-attach probe for STANDBY mode (C5): true iff any USB device
/// controller reports `configured` in `/sys/class/udc/*/state`. The UAC2 gadget
/// makes the UDC `configured` once a host enumerates it, so this stands in for
/// the audio loop's `host_connected` (which never runs in standby). Any read
/// failure (no sysfs, gadget torn down) resolves to `false` — a conservative
/// "no host" rather than a stale true.
fn udc_state_configured() -> bool {
    let dir = match fs::read_dir("/sys/class/udc") {
        Ok(d) => d,
        Err(_) => return false,
    };
    for entry in dir.flatten() {
        let state_path = entry.path().join("state");
        if let Ok(raw) = fs::read_to_string(&state_path) {
            if raw.trim() == "configured" {
                return true;
            }
        }
    }
    false
}

fn read_preempt_state(path: &Path) -> bool {
    let Ok(raw) = fs::read_to_string(path) else {
        return false;
    };
    let lowered = raw.to_ascii_lowercase();
    lowered.trim() == "1" || lowered.contains("\"silenced\"") && lowered.contains("true")
}

/// Publisher-thread ownership of the tap JSONL artifact.
///
/// This thread is the SINGLE writer of the JSONL file (the audio thread only
/// hands events over the channel). On each new arm generation it re-opens the
/// file with truncate so a fresh arm always starts clean; it then appends one
/// line per drained event until the [`TapSink`] policy says to stop
/// (`max_events` cap or `auto_disarm` deadline). On the poll where the deadline
/// passes it disarms the shared state so a forgotten tap costs nothing.
#[cfg(feature = "alsa-runtime")]
#[derive(Default)]
struct TapPublisher {
    generation: u64,
    file: Option<fs::File>,
    sink: Option<TapSink>,
    initialized: bool,
    // Logged-once-per-arm latch so hitting `max_events` emits a single journal
    // breadcrumb, not one line per dropped event (which would be journal spam
    // at the drop rate). Reset on each re-arm alongside file/sink.
    cap_hit_logged: bool,
}

#[cfg(feature = "alsa-runtime")]
impl TapPublisher {
    /// Drain and process all currently-queued tap events, plus enforce the
    /// auto-disarm deadline even when the channel is idle. `now_ms` is the
    /// monotonic clock the sink deadline is measured against.
    fn poll(
        &mut self,
        receiver: &std::sync::mpsc::Receiver<TapEvent>,
        tap: &TapState,
        tap_config: &Mutex<TapConfig>,
        now_ms: i128,
    ) {
        // A fresh arm bumps the generation; (re)open the file with truncate and
        // build a new sink. The generation only advances on arm, so this is the
        // one place the single-writer file handle is reset. The channel is
        // drained here so a re-arm starts truly clean — this discards any
        // events still queued from a PRIOR arm (they would otherwise land in
        // the freshly-truncated file). It also discards, uncounted, any events
        // the audio thread happened to enqueue in the sub-100ms window between
        // this arm and this first post-arm poll; that is harmless because the
        // operator arms human-seconds before starting playback, so no real
        // click has been played yet when this drain runs.
        let generation = tap.generation_acquire();
        if !self.initialized || generation != self.generation {
            self.initialized = true;
            self.generation = generation;
            self.cap_hit_logged = false;
            while receiver.try_recv().is_ok() {}
            if tap.armed() {
                let cfg = tap_config_snapshot(tap_config);
                self.file = open_tap_file(&cfg.path);
                self.sink = Some(TapSink::armed(&cfg, now_ms));
            } else {
                self.file = None;
                self.sink = None;
            }
        }

        // Idle auto-disarm: even with no events, a passed deadline disarms.
        if tap.armed() && self.sink.as_ref().is_some_and(|sink| sink.expired(now_ms)) {
            self.finish_disarm(tap, "auto_disarm_deadline_idle");
        }

        while let Ok(event) = receiver.try_recv() {
            // Events can arrive after disarm (channel not drained yet); ignore.
            if !tap.armed() {
                continue;
            }
            let Some(sink) = self.sink.as_mut() else {
                continue;
            };
            match sink.decide(now_ms) {
                SinkAction::Append => {
                    let wrote = self
                        .file
                        .as_mut()
                        .is_some_and(|file| writeln!(file, "{}", event.to_jsonl()).is_ok());
                    if wrote {
                        tap.note_written();
                    } else {
                        tap.note_dropped();
                    }
                }
                SinkAction::DropAtCap => {
                    tap.note_dropped();
                    // Log the cap hit ONCE per arm so a run whose JSONL stopped
                    // growing has a journal breadcrumb (further drops are silent
                    // to avoid per-event spam). The tap stays armed and counting
                    // dropped — only the file stops growing.
                    if !self.cap_hit_logged {
                        self.cap_hit_logged = true;
                        info!(
                            "event=usbsink_audio.tap_cap_hit events_written={} events_dropped={}",
                            tap.events_written(),
                            tap.events_dropped(),
                        );
                    }
                }
                SinkAction::AutoDisarm => {
                    tap.note_dropped();
                    self.finish_disarm(tap, "auto_disarm_deadline");
                    break;
                }
            }
        }
    }

    /// Disarm the shared state and release the publisher-owned file/sink so a
    /// forgotten or capped tap stops costing anything. `reason` names the
    /// self-healing trigger for the journal breadcrumb — a run whose tap
    /// disarmed itself mid-way must not do so silently, or reconstructing why
    /// tap events stopped is guesswork. (Manual `POST /tap/disarm` logs its own
    /// `event=usbsink_audio.tap_disarmed`; this covers the automatic paths.)
    fn finish_disarm(&mut self, tap: &TapState, reason: &str) {
        info!(
            "event=usbsink_audio.tap_auto_disarmed reason={} events_written={} events_dropped={}",
            reason,
            tap.events_written(),
            tap.events_dropped(),
        );
        tap.disarm();
        self.file = None;
        self.sink = None;
    }
}

/// Open (create + truncate) the tap JSONL file for the publisher to own.
/// Returns `None` on failure — the arm preflight already surfaced any path
/// error to the operator, so here we just skip appends (counting drops).
#[cfg(feature = "alsa-runtime")]
fn open_tap_file(path: &Path) -> Option<fs::File> {
    if let Some(parent) = path.parent() {
        if fs::create_dir_all(parent).is_err() {
            return None;
        }
    }
    fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
        .ok()
}

#[cfg(feature = "alsa-runtime")]
#[allow(clippy::too_many_arguments)]
fn run_state_publisher(
    config: Config,
    state: Arc<SharedState>,
    tap: Arc<TapState>,
    tap_config: Arc<Mutex<TapConfig>>,
    host_clock_fragment: Arc<Mutex<String>>,
    tap_receiver: std::sync::mpsc::Receiver<TapEvent>,
    shutdown: Arc<AtomicBool>,
) -> Result<()> {
    let mut publisher = TapPublisher::default();
    // Host-clock: the publisher is the SOLE owner of the ladder AND the pitch
    // ctl handle — single-writer by construction (the audio thread and preempt
    // listener never touch either). The ctl open is fail-soft: on failure we
    // still run the ladder and publish telemetry, but WritePitch actions no-op
    // with a rate-limited error log; a missing card must never crash the
    // publisher (a UAC2 gadget can come and go).
    let mut host_clock = HostClock::new(config.host_clock);
    let mut pitch = HostClockActuator::open(&config);
    // Startup neutralize runs (even when the feature is merely disabled) so a
    // crashed predecessor that left the host slaved is healed — EXCEPT in
    // standby, where jasper-fanin owns the ctl and a neutralize here would stomp
    // its live command. The actuator is already inert in standby (ctl=None), so
    // this gate is belt-and-braces; it also keeps the log line honest.
    if config.owns_host_clock_ctl() {
        if let Some(action) = host_clock.startup_neutralize() {
            pitch.apply(action);
            log::info!("event=usbsink_audio.host_clock_pitch_reset reason=startup");
        }
    }
    publish_host_clock_fragment(&host_clock_fragment, host_clock.status_fragment());

    // Drain the tap channel on a short cadence so a burst can't back it up,
    // while state.json keeps its ~1 s publish rhythm. The host-clock tick runs
    // on the same 1 s STATE_INTERVAL rhythm (gated inside the 100 ms loop).
    let mut last_state_write = std::time::Instant::now();
    write_state_json(
        &config,
        &state,
        &tap,
        &tap_config,
        &host_clock.status_fragment(),
    )?;
    let mut last_hc_tick = std::time::Instant::now();
    while !shutdown.load(Ordering::Relaxed) {
        publisher.poll(&tap_receiver, &tap, &tap_config, monotonic_millis());
        if last_hc_tick.elapsed() >= Duration::from_millis(host_clock::TICK_INTERVAL_MS) {
            let obs = host_clock::obs_from_shared(
                &state,
                config.period_frames,
                config.host_clock.target_fill_frames,
            );
            for action in host_clock.tick(obs, monotonic_millis() as u64) {
                pitch.apply(action);
            }
            last_hc_tick = std::time::Instant::now();
        }
        if last_state_write.elapsed() >= STATE_INTERVAL {
            // Standby (C5): the audio loop that would set host_connected never
            // runs, so derive it best-effort from sysfs on the state-write
            // cadence (`/sys/class/udc/*/state == "configured"`), false on any
            // read failure. In non-standby the audio loop owns host_connected.
            if config.audio_standby {
                state
                    .host_connected
                    .store(udc_state_configured(), Ordering::Relaxed);
            }
            let fragment = host_clock.status_fragment();
            publish_host_clock_fragment(&host_clock_fragment, fragment.clone());
            // A periodic state.json write is TELEMETRY, not control: a
            // transient /run (tmpfs) write failure must NOT tear down the
            // publisher, because doing so would abandon the host-clock ladder
            // AND skip the exit-path pitch neutralize below — leaving the host
            // slaved to the last commanded bias while systemd still sees a
            // healthy unit (the watchdog reads the AUDIO thread's progress
            // epoch, not this write). So log + continue rather than `?`; the
            // next tick retries the write, and the neutrality invariant is
            // preserved on every real exit. (The pre-loop and shutdown writes
            // keep their `?` — those are one-shot and their failure is
            // genuinely fatal to a clean start/stop.)
            if let Err(e) = write_state_json(&config, &state, &tap, &tap_config, &fragment) {
                warn!("event=usbsink_audio.state_write_error detail={e}");
            }
            last_state_write = std::time::Instant::now();
        }
        thread::sleep(TAP_DRAIN_INTERVAL);
    }
    // Exit path: the neutrality invariant. Force the host back to a free-running
    // clock before we stop — a stopped daemon must NEVER leave the host slaved.
    // This runs on clean exit AND on SIGTERM/SIGINT (the shared shutdown flag)
    // AND on audio-thread error (main sets shutdown before joining). SIGKILL /
    // watchdog / OOM is covered by the unit's ExecStopPost belt-and-braces.
    // EXCEPT in standby: fan-in owns the ctl, so a neutralize here would reset
    // its live pitch command on the way down (the actuator is already inert, so
    // this gate is belt-and-braces + keeps the log honest).
    if config.owns_host_clock_ctl() {
        pitch.apply(host_clock.neutralize_for_exit("shutdown"));
        log::info!("event=usbsink_audio.host_clock_pitch_reset reason=shutdown");
    }
    // Final drain + state write on shutdown.
    publisher.poll(&tap_receiver, &tap, &tap_config, monotonic_millis());
    let fragment = host_clock.status_fragment();
    publish_host_clock_fragment(&host_clock_fragment, fragment.clone());
    write_state_json(&config, &state, &tap, &tap_config, &fragment)?;
    Ok(())
}

/// The publisher-thread wrapper around the optional pitch actuator. Holds the
/// real [`host_clock::AlsaPitchCtl`] when the card opens, else `None` (fail-soft
/// — the ladder still runs and publishes telemetry). Applies a ladder
/// [`host_clock::Action`], translating the commanded ppm to the ctl integer and
/// rate-limiting ctl-error logs so a flapping card cannot spam the journal.
#[cfg(feature = "alsa-runtime")]
struct HostClockActuator {
    ctl: Option<host_clock::AlsaPitchCtl>,
    last_error_ms: Option<u64>,
}

#[cfg(feature = "alsa-runtime")]
impl HostClockActuator {
    fn open(config: &Config) -> Self {
        // Standby (combo): jasper-fanin owns the gadget AND the pitch ctl in
        // this posture. The usbsink daemon must NOT open the ctl at all — even
        // the one-shot startup/exit neutralize would reset it to 1000000 behind
        // fan-in's back on every clean stop/start cycle (a deploy try-restart or
        // an operator restart), desyncing fan-in's >10 ppm write-suppression
        // epsilon and letting the host free-run un-slaved. So return an inert
        // actuator here; the neutralize calls below are also gated on
        // `owns_host_clock_ctl()` as belt-and-braces. The NOT-standby
        // ExecStopPost belt in jasper-usbsink.service covers the SIGKILL path.
        if !config.owns_host_clock_ctl() {
            info!("event=usbsink_audio.host_clock_ctl_skipped reason=standby_fanin_owns_ctl");
            return Self {
                ctl: None,
                last_error_ms: None,
            };
        }
        let card = host_clock::ctl_card_from_capture(&config.capture_device);
        match host_clock::AlsaPitchCtl::open(&card) {
            Ok(ctl) => {
                info!("event=usbsink_audio.host_clock_ctl_opened card={card}");
                Self {
                    ctl: Some(ctl),
                    last_error_ms: None,
                }
            }
            Err(e) => {
                // Not fatal: the gadget card may not be present (feature can be
                // enabled before a host is plugged, or on a box without the
                // dtoverlay). The ladder runs and publishes; pitch writes no-op.
                warn!("event=usbsink_audio.host_clock_ctl_error detail={e}");
                Self {
                    ctl: None,
                    last_error_ms: None,
                }
            }
        }
    }

    fn apply(&mut self, action: host_clock::Action) {
        use host_clock::PitchCtl;
        let host_clock::Action::WritePitch { ppm, .. } = action;
        let value = host_clock::ppm_to_ctl_value(ppm);
        let Some(ctl) = self.ctl.as_mut() else {
            return;
        };
        if let Err(e) = ctl.write(value) {
            // Rate-limit ctl-error logs to at most one per ~10 s so repeated
            // write failures do not crash or spam the publisher.
            let now = monotonic_millis() as u64;
            // `Option::is_none_or` is stable only since Rust 1.82; the crate
            // declares rust-version 1.75, so clippy's `incompatible_msrv`
            // (on by default, `-D warnings` in CI) would reject it. `map_or`
            // has been stable since 1.0 and is MSRV-safe.
            let should_log = self
                .last_error_ms
                .map_or(true, |last| now.saturating_sub(last) >= 10_000);
            if should_log {
                self.last_error_ms = Some(now);
                warn!("event=usbsink_audio.host_clock_ctl_error detail={e}");
            }
        }
    }

    /// True when this actuator holds no ctl handle (standby, or a failed open).
    /// A ctl-less actuator's `apply` is a no-op, so no pitch write can leave the
    /// process. Test-only accessor pinning the standby hands-off invariant.
    #[cfg(test)]
    fn is_inert(&self) -> bool {
        self.ctl.is_none()
    }
}

#[cfg(feature = "alsa-runtime")]
fn write_state_json(
    config: &Config,
    state: &SharedState,
    tap: &TapState,
    tap_config: &Mutex<TapConfig>,
    host_clock_fragment: &str,
) -> Result<()> {
    if let Some(parent) = config.state_path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    }
    let rms_dbfs = (state.rms_dbfs_x100.load(Ordering::Relaxed) as f64) / 100.0;
    let tap_fragment = tap.status_fragment(&tap_config_snapshot(tap_config));
    let body = status_json(config, state, rms_dbfs, &tap_fragment, host_clock_fragment);
    let tmp = unique_state_tmp_path(&config.state_path);
    fs::write(&tmp, body).with_context(|| format!("writing {}", tmp.display()))?;
    fs::rename(&tmp, &config.state_path).with_context(|| {
        format!(
            "renaming {} to {}",
            tmp.display(),
            config.state_path.display()
        )
    })
}

fn unique_state_tmp_path(path: &Path) -> PathBuf {
    let seq = STATE_WRITE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("state.json");
    path.with_file_name(format!(".{file_name}.{}.{}.tmp", std::process::id(), seq))
}

fn status_json(
    config: &Config,
    state: &SharedState,
    rms_dbfs: f64,
    tap_fragment: &str,
    host_clock_fragment: &str,
) -> String {
    format!(
        concat!(
            "{{",
            "\"schema_version\":1,",
            "\"implementation\":\"rust\",",
            "\"standby\":{},",
            "\"updated_at\":\"{}\",",
            "\"playing\":{},",
            "\"preempted\":{},",
            "\"host_connected\":{},",
            "\"rms_dbfs\":{:.2},",
            "\"capture_device\":\"{}\",",
            "\"playback_device\":\"{}\",",
            "\"sample_rate\":{},",
            "\"channels\":{},",
            "\"period_frames\":{},",
            "\"ring\":{{\"fill_periods\":{},\"capacity_periods\":{}}},",
            "\"counters\":{{",
            "\"capture_xruns\":{},",
            "\"capture_partial_reads\":{},",
            "\"playback_xruns\":{},",
            "\"underflow_periods\":{},",
            "\"overflow_events\":{},",
            "\"dropped_periods\":{},",
            "\"preempt_silence_periods\":{},",
            "\"preempt_dropped_periods\":{},",
            "\"capture_frames\":{},",
            "\"playback_frames\":{}",
            "}},",
            "\"tap\":{},",
            "\"host_clock\":{},",
            "\"last_progress_epoch_ms\":{}",
            "}}\n"
        ),
        json_bool(config.audio_standby),
        iso8601_now(),
        json_bool(state.playing.load(Ordering::Relaxed)),
        json_bool(state.preempted.load(Ordering::Relaxed)),
        json_bool(state.host_connected.load(Ordering::Relaxed)),
        rms_dbfs,
        json_escape(&config.capture_device),
        json_escape(&config.playback_device),
        config.sample_rate,
        config.channels,
        config.period_frames,
        state.ring_fill_periods.load(Ordering::Relaxed),
        state.ring_capacity_periods.load(Ordering::Relaxed),
        state.capture_xruns.load(Ordering::Relaxed),
        state.capture_partial_reads.load(Ordering::Relaxed),
        state.playback_xruns.load(Ordering::Relaxed),
        state.underflow_periods.load(Ordering::Relaxed),
        state.overflow_events.load(Ordering::Relaxed),
        state.dropped_periods.load(Ordering::Relaxed),
        state.preempt_silence_periods.load(Ordering::Relaxed),
        state.preempt_dropped_periods.load(Ordering::Relaxed),
        state.capture_frames.load(Ordering::Relaxed),
        state.playback_frames.load(Ordering::Relaxed),
        tap_fragment,
        host_clock_fragment,
        state.last_progress_epoch_ms.load(Ordering::Relaxed),
    )
}

fn json_bool(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn json_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c.is_control() => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn epoch_millis() -> u64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => duration.as_millis() as u64,
        Err(_) => 0,
    }
}

/// `CLOCK_MONOTONIC` in nanoseconds — the tap's ingress timeline.
///
/// The impulse tap and the Python mic harness both read `CLOCK_MONOTONIC` on
/// the same Pi; that shared timeline is the only reason their cross-process
/// timestamp subtraction is valid, and why the tap never uses epoch time.
/// On the (never-observed) syscall failure this returns 0 rather than crashing
/// the audio thread; a stray 0-anchored event is dropped downstream by the
/// harness's pairing window rather than corrupting the run.
fn monotonic_ns() -> i128 {
    let mut ts = MaybeUninit::<libc::timespec>::uninit();
    let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, ts.as_mut_ptr()) };
    if rc != 0 {
        return 0;
    }
    let ts = unsafe { ts.assume_init() };
    (ts.tv_sec as i128) * 1_000_000_000 + (ts.tv_nsec as i128)
}

/// `CLOCK_MONOTONIC` in milliseconds — the publisher's auto-disarm clock.
fn monotonic_millis() -> i128 {
    monotonic_ns() / 1_000_000
}

fn iso8601_now() -> String {
    let duration = match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(value) => value,
        Err(_) => return "1970-01-01T00:00:00.000Z".to_string(),
    };
    let secs = duration.as_secs() as libc::time_t;
    let millis = duration.subsec_millis();
    let mut tm = MaybeUninit::<libc::tm>::uninit();
    let tm_ptr = unsafe { libc::gmtime_r(&secs, tm.as_mut_ptr()) };
    if tm_ptr.is_null() {
        return format!("{}Z", epoch_millis());
    }
    let tm = unsafe { tm.assume_init() };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        millis,
    )
}

#[cfg(feature = "alsa-runtime")]
fn start_watchdog(state: Arc<SharedState>, shutdown: Arc<AtomicBool>) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        while !shutdown.load(Ordering::Relaxed) {
            let last = state.last_progress_epoch_ms.load(Ordering::Relaxed);
            let age = epoch_millis().saturating_sub(last);
            if last > 0 && age < 10_000 {
                let _ = sd_notify::notify(&[NotifyState::Watchdog]);
            }
            thread::sleep(WATCHDOG_INTERVAL);
        }
    })
}

#[cfg(feature = "alsa-runtime")]
fn install_signal_handlers(shutdown: &Arc<AtomicBool>) -> Result<()> {
    flag::register(SIGTERM, Arc::clone(shutdown)).context("registering SIGTERM")?;
    flag::register(SIGINT, Arc::clone(shutdown)).context("registering SIGINT")?;
    Ok(())
}

#[cfg(feature = "alsa-runtime")]
fn main() -> Result<()> {
    env_logger::init();
    let config = Config::from_env().context("loading jasper-usbsink-audio config")?;
    let shutdown = Arc::new(AtomicBool::new(false));
    install_signal_handlers(&shutdown)?;
    let shared = Arc::new(SharedState::new(config.ring_periods));
    if read_preempt_state(&config.preempt_state_path) {
        shared.preempted.store(true, Ordering::Relaxed);
    }
    // Impulse-tap wiring. Default-off: `tap.state` starts disarmed, so the audio
    // loop pays one relaxed atomic load per read until armed via the 8781
    // control plane. The bounded channel keeps the audio thread's hand-off
    // non-blocking (drop-and-count on Full); the publisher thread is the sole
    // JSONL writer.
    let tap = TapShared::new();
    // Shared host-clock telemetry fragment: the publisher renders it, the
    // preempt listener and the startup/audio paths read a snapshot. Seeded with
    // the initial (disabled/idle) block so the first state.json is coherent.
    let host_clock_shared = HostClockShared::new(config.host_clock);
    let (tap_sender, tap_receiver) =
        std::sync::mpsc::sync_channel::<TapEvent>(TAP_CHANNEL_CAPACITY);
    let preempt_listener = bind_preempt_listener(&config)?;
    write_state_json(
        &config,
        &shared,
        &tap.state,
        &tap.config,
        &host_clock_shared.snapshot(),
    )?;

    let state_thread = {
        let cfg = config.clone();
        let state = Arc::clone(&shared);
        let tap_state = Arc::clone(&tap.state);
        let tap_cfg = Arc::clone(&tap.config);
        let hc_fragment = Arc::clone(&host_clock_shared.fragment);
        let stop = Arc::clone(&shutdown);
        thread::spawn(move || {
            run_state_publisher(
                cfg,
                state,
                tap_state,
                tap_cfg,
                hc_fragment,
                tap_receiver,
                stop,
            )
        })
    };
    let preempt_thread = {
        let cfg = config.clone();
        let state = Arc::clone(&shared);
        let tap_state = Arc::clone(&tap.state);
        let tap_cfg = Arc::clone(&tap.config);
        let hc_fragment = Arc::clone(&host_clock_shared.fragment);
        let stop = Arc::clone(&shutdown);
        thread::spawn(move || {
            run_preempt_listener(
                cfg,
                state,
                tap_state,
                tap_cfg,
                hc_fragment,
                stop,
                preempt_listener,
            )
        })
    };
    let watchdog_thread = start_watchdog(Arc::clone(&shared), Arc::clone(&shutdown));

    // In STANDBY (C5) the bridge does NOT open the gadget capture/playback —
    // jasper-fanin owns hw:UAC2Gadget directly. We still send READY=1 and drive
    // the progress sentinel on the publisher cadence so the Type=notify +
    // WatchdogSec=15s unit does not kill-loop, and keep the state publisher /
    // preempt listener / watchdog threads running. The audio loop is skipped
    // entirely (no PCM open).
    let audio_result = if config.audio_standby {
        run_standby_loop(&shared, &shutdown)
    } else {
        run_audio_loop(
            &config,
            &shared,
            &tap.state,
            &tap.config,
            &host_clock_shared.fragment,
            &tap_sender,
            &shutdown,
        )
    };
    shutdown.store(true, Ordering::Relaxed);

    join_result_thread(state_thread, "state publisher")?;
    join_result_thread(preempt_thread, "preempt listener")?;
    let _ = watchdog_thread.join();
    audio_result
}

#[cfg(feature = "alsa-runtime")]
fn join_result_thread(handle: thread::JoinHandle<Result<()>>, label: &str) -> Result<()> {
    match handle.join() {
        Ok(result) => result.with_context(|| format!("{label} failed")),
        Err(_) => bail!("{label} panicked"),
    }
}

#[cfg(not(feature = "alsa-runtime"))]
fn main() -> Result<()> {
    bail!("jasper-usbsink-audio was built without the alsa-runtime feature")
}

#[cfg(test)]
mod tests {
    use super::*;
    // The scalar narrowing lives in `jasper-resampler`; the runtime path calls
    // `convert_s32_to_s16` (fully qualified above), so the scalar is imported
    // only here for the pinned sign-boundary vector.
    use jasper_resampler::s32_high_word_to_s16;

    /// A disabled host-clock config for the daemon-level status_json tests.
    /// The host-clock ladder itself is tested exhaustively in the shared
    /// `jasper-host-clock` crate; these tests only assert the daemon folds the
    /// block in.
    fn test_host_clock_config() -> HostClockConfig {
        host_clock::from_env(|_| None).unwrap()
    }

    /// The disabled host-clock fragment, for status_json fold-in tests.
    fn test_host_clock_fragment() -> String {
        HostClock::new(test_host_clock_config()).status_fragment()
    }

    /// C2 sign-boundary vector. `s32_high_word_to_s16` now comes from the
    /// shared `jasper-resampler` crate (imported at module top), so this test
    /// pins THIS crate's view of the shared narrowing. The identical vector is
    /// re-asserted in jasper-resampler AND jasper-fanin, so a drift in the one
    /// shared definition fails every suite.
    #[test]
    fn s32_high_word_truncation_preserves_sign_boundaries() {
        assert_eq!(s32_high_word_to_s16(0), 0);
        assert_eq!(s32_high_word_to_s16(0x7fff_ffff), 0x7fff);
        assert_eq!(s32_high_word_to_s16(i32::MIN), i16::MIN);
        assert_eq!(s32_high_word_to_s16(-1), -1);
        assert_eq!(s32_high_word_to_s16(-65_536), -1);
        assert_eq!(s32_high_word_to_s16(-65_537), -2);
    }

    #[test]
    fn reserved_tap_basenames_cover_the_daemons_own_files() {
        // N4 guard: RESERVED_TAP_DIR_BASENAMES (impulse_tap.rs) is hand-synced
        // with the daemon's own state files. If either DEFAULT_*_PATH gains a
        // new basename or moves, the unauthenticated arm endpoint could target
        // (truncate) the daemon's own file. Pin the two constants against each
        // other directly — stronger than a source grep, and it lives in the one
        // crate that can see both. Also assert both files live under the tap
        // dir (the reservation only matters for files inside TAP_PATH_DIR).
        for daemon_path in [DEFAULT_STATE_PATH, DEFAULT_PREEMPT_STATE_PATH] {
            let path = std::path::Path::new(daemon_path);
            assert_eq!(
                path.parent(),
                Some(std::path::Path::new(impulse_tap::TAP_PATH_DIR)),
                "{daemon_path} is not under TAP_PATH_DIR — the reservation \
                 assumes the daemon's own files share the tap dir"
            );
            let basename = path.file_name().and_then(|n| n.to_str()).unwrap();
            assert!(
                impulse_tap::RESERVED_TAP_DIR_BASENAMES.contains(&basename),
                "daemon file {basename:?} ({daemon_path}) is NOT in \
                 RESERVED_TAP_DIR_BASENAMES — the unauthenticated /tap/arm \
                 endpoint could truncate it. Add it to the reserved list."
            );
            // The reservation must actually reject an arm targeting that file.
            assert!(
                !impulse_tap::path_is_allowed(path),
                "path_is_allowed must reject the daemon's own file {daemon_path}"
            );
        }
    }

    #[test]
    fn ring_underflow_writes_silence_and_counts() {
        let mut ring = PeriodRing::new(4, 2).unwrap();
        let mut out = [7i16; 4];

        assert!(!ring.pop_or_silence(&mut out));

        assert_eq!(out, [0, 0, 0, 0]);
        assert_eq!(ring.underflow_events, 1);
    }

    #[test]
    fn ring_overflow_drops_oldest_whole_period() {
        let mut ring = PeriodRing::new(2, 2).unwrap();
        ring.push_period(&[1, 1]).unwrap();
        ring.push_period(&[2, 2]).unwrap();
        ring.push_period(&[3, 3]).unwrap();
        let mut out = [0i16; 2];

        assert!(ring.pop_or_silence(&mut out));
        assert_eq!(out, [2, 2]);
        assert!(ring.pop_or_silence(&mut out));
        assert_eq!(out, [3, 3]);
        assert_eq!(ring.overflow_events, 1);
        assert_eq!(ring.dropped_periods, 1);
    }

    #[test]
    fn ring_free_periods_tracks_available_write_slots() {
        let mut ring = PeriodRing::new(2, 3).unwrap();
        assert_eq!(ring.free_periods(), 3);

        ring.push_period(&[1, 1]).unwrap();
        ring.push_period(&[2, 2]).unwrap();
        assert_eq!(ring.fill_periods(), 2);
        assert_eq!(ring.free_periods(), 1);

        let mut out = [0i16; 2];
        assert!(ring.pop_or_silence(&mut out));
        assert_eq!(ring.fill_periods(), 1);
        assert_eq!(ring.free_periods(), 2);
    }

    #[test]
    fn period_assembler_waits_for_a_complete_period() {
        let mut assembler = PeriodAssembler::new(4, 2).unwrap();
        let mut periods: Vec<Vec<i16>> = Vec::new();

        let completed = assembler
            .push_frames(&[1, 10, 2, 20, 3, 30], 3, |period| {
                periods.push(period.to_vec());
                Ok(())
            })
            .unwrap();
        assert_eq!(completed, 0);
        assert_eq!(assembler.filled_frames(), 3);
        assert!(periods.is_empty());

        let completed = assembler
            .push_frames(&[4, 40], 1, |period| {
                periods.push(period.to_vec());
                Ok(())
            })
            .unwrap();

        assert_eq!(completed, 1);
        assert_eq!(assembler.filled_frames(), 0);
        assert_eq!(periods, vec![vec![1, 10, 2, 20, 3, 30, 4, 40]]);
    }

    #[test]
    fn period_assembler_carries_remainder_after_completed_period() {
        let mut assembler = PeriodAssembler::new(3, 2).unwrap();
        let mut periods: Vec<Vec<i16>> = Vec::new();

        let completed = assembler
            .push_frames(&[1, 10, 2, 20, 3, 30, 4, 40], 4, |period| {
                periods.push(period.to_vec());
                Ok(())
            })
            .unwrap();

        assert_eq!(completed, 1);
        assert_eq!(assembler.filled_frames(), 1);
        assert_eq!(periods, vec![vec![1, 10, 2, 20, 3, 30]]);

        let completed = assembler
            .push_frames(&[5, 50, 6, 60], 2, |period| {
                periods.push(period.to_vec());
                Ok(())
            })
            .unwrap();

        assert_eq!(completed, 1);
        assert_eq!(assembler.filled_frames(), 0);
        assert_eq!(periods[1], vec![4, 40, 5, 50, 6, 60]);
    }

    #[test]
    fn preempt_drops_new_capture_and_flushes_existing_ring() {
        let mut ring = PeriodRing::new(2, 3).unwrap();
        let state = SharedState::new(3);

        stage_capture_period(&mut ring, &[1, 1], &state).unwrap();
        assert_eq!(ring.fill_periods(), 1);

        state.preempted.store(true, Ordering::Relaxed);
        stage_capture_period(&mut ring, &[2, 2], &state).unwrap();
        assert_eq!(ring.fill_periods(), 1);
        assert_eq!(state.preempt_dropped_periods.load(Ordering::Relaxed), 1);

        flush_ring_for_preempt(&mut ring, &state);
        assert_eq!(ring.fill_periods(), 0);
        assert_eq!(state.preempt_dropped_periods.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn config_validation_rejects_non_48k_non_stereo_and_bad_ring() {
        assert!(validate_audio_config(44_100, 2, 256, 3).is_err());
        assert!(validate_audio_config(48_000, 1, 256, 3).is_err());
        assert!(validate_audio_config(48_000, 2, 0, 3).is_err());
        assert!(validate_audio_config(48_000, 2, 256, 4).is_err());
        assert!(validate_audio_config(48_000, 2, 256, 3).is_ok());
    }

    #[test]
    fn preempt_request_parser_accepts_mux_shape() {
        assert_eq!(
            parse_preempt_silenced("POST /preempt HTTP/1.1\r\n\r\n{\"silenced\": true}"),
            Some(true)
        );
        assert_eq!(
            parse_preempt_silenced("POST /preempt HTTP/1.1\r\n\r\n{\"silenced\": false}"),
            Some(false)
        );
        assert_eq!(parse_preempt_silenced("GET / HTTP/1.1\r\n\r\n"), None);
        assert_eq!(
            parse_preempt_silenced(
                "POST /preempt HTTP/1.1\r\n\r\n{\"silenced\":\"true\",\"other\":true}"
            ),
            None
        );
        assert_eq!(
            parse_preempt_silenced("POST /preempt HTTP/1.1\r\n\r\n{\"note\":\"silenced true\"}"),
            None
        );
    }

    #[test]
    fn state_tmp_paths_are_unique_for_concurrent_publishers() {
        let path = PathBuf::from("/run/jasper-usbsink/state.json");

        let first = unique_state_tmp_path(&path);
        let second = unique_state_tmp_path(&path);

        assert_ne!(first, second);
        assert_eq!(first.parent(), path.parent());
        assert_eq!(second.parent(), path.parent());
        assert!(first
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with(".state.json.")));
    }

    #[test]
    fn preempt_state_reader_accepts_json_and_legacy_one() {
        let path = std::env::temp_dir().join(format!(
            "jasper-usbsink-audio-preempt-test-{}",
            std::process::id()
        ));
        std::fs::write(&path, "{\"silenced\":true}\n").unwrap();
        assert!(read_preempt_state(&path));
        std::fs::write(&path, "0\n").unwrap();
        assert!(!read_preempt_state(&path));
        std::fs::write(&path, "1\n").unwrap();
        assert!(read_preempt_state(&path));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn status_json_carries_route_health_fields() {
        let cfg = Config {
            capture_device: "hw:UAC2Gadget".to_string(),
            playback_device: "usbsink_substream".to_string(),
            sample_rate: SAMPLE_RATE,
            channels: CHANNELS,
            period_frames: DEFAULT_PERIOD_FRAMES,
            ring_periods: DEFAULT_RING_PERIODS,
            state_path: PathBuf::from(DEFAULT_STATE_PATH),
            preempt_state_path: PathBuf::from(DEFAULT_PREEMPT_STATE_PATH),
            preempt_host: DEFAULT_PREEMPT_HOST.to_string(),
            preempt_port: DEFAULT_PREEMPT_PORT,
            host_clock: test_host_clock_config(),
            audio_standby: false,
        };
        let state = SharedState::new(DEFAULT_RING_PERIODS);
        state.preempted.store(true, Ordering::Relaxed);
        state.ring_fill_periods.store(2, Ordering::Relaxed);
        state.underflow_periods.store(5, Ordering::Relaxed);

        let tap = TapState::default();
        let tap_fragment = tap.status_fragment(&TapConfig::default());
        let body = status_json(
            &cfg,
            &state,
            -63.25,
            &tap_fragment,
            &test_host_clock_fragment(),
        );

        assert!(body.contains("\"implementation\":\"rust\""));
        assert!(body.contains("\"preempted\":true"));
        assert!(body.contains("\"host_connected\":false"));
        assert!(body.contains("\"ring\":{\"fill_periods\":2,\"capacity_periods\":3}"));
        assert!(body.contains("\"capture_partial_reads\":0"));
        assert!(body.contains("\"underflow_periods\":5"));
        // Tap sub-object is folded in and disarmed by default.
        assert!(body.contains("\"tap\":{\"armed\":false"));
        // The whole document parses as valid JSON with the tap object nested.
        let parsed: serde_json::Value = serde_json::from_str(body.trim()).unwrap();
        assert_eq!(parsed["tap"]["armed"].as_bool(), Some(false));
        assert_eq!(parsed["tap"]["events_written"].as_u64(), Some(0));
    }

    #[test]
    fn status_json_host_connected_is_independent_of_rms_activity() {
        let cfg = Config {
            capture_device: "hw:UAC2Gadget".to_string(),
            playback_device: "usbsink_substream".to_string(),
            sample_rate: SAMPLE_RATE,
            channels: CHANNELS,
            period_frames: DEFAULT_PERIOD_FRAMES,
            ring_periods: DEFAULT_RING_PERIODS,
            state_path: PathBuf::from(DEFAULT_STATE_PATH),
            preempt_state_path: PathBuf::from(DEFAULT_PREEMPT_STATE_PATH),
            preempt_host: DEFAULT_PREEMPT_HOST.to_string(),
            preempt_port: DEFAULT_PREEMPT_PORT,
            host_clock: test_host_clock_config(),
            audio_standby: false,
        };
        let state = SharedState::new(DEFAULT_RING_PERIODS);
        state.host_connected.store(true, Ordering::Relaxed);
        state.playing.store(false, Ordering::Relaxed);

        let tap = TapState::default();
        let tap_fragment = tap.status_fragment(&TapConfig::default());
        let body = status_json(
            &cfg,
            &state,
            -120.0,
            &tap_fragment,
            &test_host_clock_fragment(),
        );

        assert!(body.contains("\"host_connected\":true"));
        assert!(body.contains("\"playing\":false"));
    }

    #[test]
    fn status_json_folds_armed_tap_sub_object() {
        let cfg = Config {
            capture_device: "hw:UAC2Gadget".to_string(),
            playback_device: "usbsink_substream".to_string(),
            sample_rate: SAMPLE_RATE,
            channels: CHANNELS,
            period_frames: DEFAULT_PERIOD_FRAMES,
            ring_periods: DEFAULT_RING_PERIODS,
            state_path: PathBuf::from(DEFAULT_STATE_PATH),
            preempt_state_path: PathBuf::from(DEFAULT_PREEMPT_STATE_PATH),
            preempt_host: DEFAULT_PREEMPT_HOST.to_string(),
            preempt_port: DEFAULT_PREEMPT_PORT,
            host_clock: test_host_clock_config(),
            audio_standby: false,
        };
        let state = SharedState::new(DEFAULT_RING_PERIODS);
        let tap = TapState::default();
        let tap_cfg = TapConfig::default();
        tap.arm(&tap_cfg, SAMPLE_RATE, 1_000);
        let body = status_json(
            &cfg,
            &state,
            -120.0,
            &tap.status_fragment(&tap_cfg),
            &test_host_clock_fragment(),
        );

        let parsed: serde_json::Value = serde_json::from_str(body.trim()).unwrap();
        assert_eq!(parsed["tap"]["armed"].as_bool(), Some(true));
        assert_eq!(
            parsed["tap"]["max_events"].as_u64(),
            Some(impulse_tap::DEFAULT_MAX_EVENTS)
        );
        assert_eq!(
            parsed["tap"]["path"].as_str(),
            Some(impulse_tap::DEFAULT_TAP_PATH)
        );
    }

    #[test]
    fn status_json_nests_host_clock_block() {
        let cfg = Config {
            capture_device: "hw:UAC2Gadget".to_string(),
            playback_device: "usbsink_substream".to_string(),
            sample_rate: SAMPLE_RATE,
            channels: CHANNELS,
            period_frames: DEFAULT_PERIOD_FRAMES,
            ring_periods: DEFAULT_RING_PERIODS,
            state_path: PathBuf::from(DEFAULT_STATE_PATH),
            preempt_state_path: PathBuf::from(DEFAULT_PREEMPT_STATE_PATH),
            preempt_host: DEFAULT_PREEMPT_HOST.to_string(),
            preempt_port: DEFAULT_PREEMPT_PORT,
            host_clock: test_host_clock_config(),
            audio_standby: false,
        };
        let state = SharedState::new(DEFAULT_RING_PERIODS);
        let tap = TapState::default();
        let tap_fragment = tap.status_fragment(&TapConfig::default());
        let body = status_json(
            &cfg,
            &state,
            -120.0,
            &tap_fragment,
            &test_host_clock_fragment(),
        );

        // The whole document parses and the host_clock block is a sibling of tap.
        let parsed: serde_json::Value = serde_json::from_str(body.trim()).unwrap();
        assert_eq!(parsed["host_clock"]["enabled"].as_bool(), Some(false));
        assert_eq!(parsed["host_clock"]["ladder"].as_str(), Some("disabled"));
        assert!(parsed["host_clock"]["probe"]["response_ratio"].is_null());
        // schema_version stays 1 (additive block).
        assert_eq!(parsed["schema_version"].as_u64(), Some(1));
    }

    // ---- C5: standby mode -------------------------------------------------

    /// Build a Config fixture with `audio_standby` explicitly set (host clock
    /// disabled to match the standby invariant), for the standby status tests.
    fn standby_config(audio_standby: bool) -> Config {
        Config {
            capture_device: "hw:UAC2Gadget".to_string(),
            playback_device: "usbsink_substream".to_string(),
            sample_rate: SAMPLE_RATE,
            channels: CHANNELS,
            period_frames: DEFAULT_PERIOD_FRAMES,
            ring_periods: DEFAULT_RING_PERIODS,
            state_path: PathBuf::from(DEFAULT_STATE_PATH),
            preempt_state_path: PathBuf::from(DEFAULT_PREEMPT_STATE_PATH),
            preempt_host: DEFAULT_PREEMPT_HOST.to_string(),
            preempt_port: DEFAULT_PREEMPT_PORT,
            host_clock: host_clock::disabled_config(),
            audio_standby,
        }
    }

    #[test]
    fn status_json_carries_standby_field() {
        // Non-standby: standby:false, schema_version stays 1.
        let cfg = standby_config(false);
        let state = SharedState::new(DEFAULT_RING_PERIODS);
        let tap = TapState::default();
        let body = status_json(
            &cfg,
            &state,
            -120.0,
            &tap.status_fragment(&TapConfig::default()),
            &test_host_clock_fragment(),
        );
        let parsed: serde_json::Value = serde_json::from_str(body.trim()).unwrap();
        assert_eq!(parsed["standby"].as_bool(), Some(false));
        assert_eq!(parsed["schema_version"].as_u64(), Some(1));

        // Standby: standby:true, and the not-running defaults hold (playing
        // false, rms -120, ring/counters zero) — a misdirected harness run is
        // diagnosable from standby:true.
        let cfg = standby_config(true);
        let body = status_json(
            &cfg,
            &state,
            -120.0,
            &tap.status_fragment(&TapConfig::default()),
            &test_host_clock_fragment(),
        );
        let parsed: serde_json::Value = serde_json::from_str(body.trim()).unwrap();
        assert_eq!(parsed["standby"].as_bool(), Some(true));
        assert_eq!(parsed["playing"].as_bool(), Some(false));
        assert!((parsed["rms_dbfs"].as_f64().unwrap() - (-120.0)).abs() < 1e-6);
        assert_eq!(parsed["ring"]["fill_periods"].as_u64(), Some(0));
        assert_eq!(parsed["counters"]["capture_frames"].as_u64(), Some(0));
    }

    #[test]
    fn standby_forces_host_clock_disabled_regardless_of_env() {
        // Even with the host-clock env set to `enabled`, standby resolves the
        // host clock to disabled (no fill source in standby). We can't call
        // Config::from_env (it reads the process env), so exercise the same
        // decision the from_env branch makes: standby → host_clock::disabled_config.
        let hc = host_clock::disabled_config();
        assert!(!hc.enabled, "standby host clock must be disabled");
        // The disabled config renders the disabled fragment.
        let fragment = HostClock::new(hc).status_fragment();
        let parsed: serde_json::Value = serde_json::from_str(&fragment).unwrap();
        assert_eq!(parsed["enabled"].as_bool(), Some(false));
        assert_eq!(parsed["ladder"].as_str(), Some("disabled"));
    }

    #[test]
    fn standby_never_owns_host_clock_ctl_solo_always_does() {
        // P2-F1: `owns_host_clock_ctl()` is the single decision that gates the
        // actuator open AND both the startup/exit neutralize. In standby (combo)
        // fan-in owns the gadget pitch ctl, so usbsink must return false and stay
        // hands-off — even a neutralize would reset fan-in's live command to
        // 1000000 behind its back on every clean stop/start cycle (deploy
        // try-restart / operator restart), desyncing fan-in's >10 ppm
        // write-suppression epsilon. In solo/normal mode usbsink owns it and
        // returns true EVEN when the host-clock feature is disabled (it still
        // runs the startup neutralize to heal a crashed predecessor). This pure
        // predicate is card-independent, so it pins the decision in CI where no
        // gadget is present. Ownership is independent of `host_clock.enabled`.
        assert!(
            !standby_config(true).owns_host_clock_ctl(),
            "standby must NOT own the ctl (fan-in owns it in combo)"
        );
        assert!(
            standby_config(false).owns_host_clock_ctl(),
            "solo mode owns the ctl even with host-clock disabled"
        );
    }

    #[cfg(feature = "alsa-runtime")]
    #[test]
    fn standby_actuator_is_inert_never_opens_the_ctl() {
        // The actuator honors `owns_host_clock_ctl()`: the standby short-circuit
        // in `HostClockActuator::open` fires BEFORE any ALSA call, so it returns
        // an inert (ctl=None) actuator whose `apply` is a no-op — no pitch write
        // can leave the process. Runs in CI with no gadget card present.
        let pitch = HostClockActuator::open(&standby_config(true));
        assert!(
            pitch.is_inert(),
            "standby actuator must hold no ctl (fan-in owns it)"
        );
    }

    #[test]
    fn audio_standby_env_only_enabled_by_literal_one() {
        // The exact-literal parse (matching the from_env branch): only "1"
        // (trimmed) enables; anything else is today's behavior.
        for (raw, expected) in [
            (Some("1"), true),
            (Some(" 1 "), true),
            (Some("0"), false),
            (Some("true"), false),
            (Some("enabled"), false),
            (Some(""), false),
            (None, false),
        ] {
            let got = match raw {
                Some(v) => v.trim() == "1",
                None => false,
            };
            assert_eq!(got, expected, "standby parse for {raw:?}");
        }
    }
}
