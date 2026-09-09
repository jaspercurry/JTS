// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Per-input adaptive resampler for the clock-crossing (USB) fan-in lane.
//!
//! ## What problem this solves
//!
//! The fan-in work loop is paced by the blocking OUTPUT write — the local DAC
//! clock. Every renderer lane whose producer is clocked off the *same* DAC
//! (AirPlay / Spotify / Bluetooth / TTS) keeps its capture ring at ~one period
//! forever and needs no rate work. The **USB lane is the exception**: its
//! producer is the host (Mac) clock, free-running relative to our DAC-paced
//! drain, so a small residual rate gap accumulates in its snd-aloop ring.
//! [`crate::mixer`]'s bounded catch-up drain absorbs that gap by discarding
//! audio whenever the lane backs up past a high-water
//! (`CATCHUP_HIGH_WATER_PERIODS`) sized never to false-fire on a healthy
//! AirPlay burst — which lets the USB ring sit anywhere from 1 to ~14 periods,
//! a **5–75 ms latency sawtooth**.
//!
//! This module is the drop-FREE alternative: a per-lane windowed-sinc
//! resampler, DLL-steered to the DAC clock, that reconciles the host rate to
//! the DAC rate at the lane's input edge, so the lane sits at a small fixed
//! fill and the catch-up never fires. Reconciling here is also what lets
//! CamillaDSP stay DAC-paced without `rate_adjust` on the clockless USB input.
//!
//! The buffer it disciplines is per-INPUT, upstream of the sum — one resampler
//! per host-clocked lane, not one for the whole mix. The DLL control law lives
//! entirely inside [`RateController`]; this module never touches loop math.
//!
//! ## Capture-follower sign
//!
//! The error fed to the controller is `fill - target`. A too-full ring
//! (`error > 0`) settles to `ratio > 1`, which advances the fractional read
//! cursor by more than one input frame per output frame — consuming the host's
//! faster-arriving input FASTER and draining the ring back to target. This is
//! the convention [`jasper_resampler`] documents; we feed the raw
//! `fill - target` and the controller negates internally.
//!
//! ## Real-time safety
//!
//! - No allocation on the hot path: the ring is sized at construction and the
//!   per-period output is written into a caller-owned slice.
//! - No blocking and no ALSA I/O — the mixer feeds already-read frames via
//!   [`push_input`].
//! - No clock reads. Logging is count-gated like the rest of the daemon.
//! - Bounded work: `render_period` interpolates exactly
//!   `period_frames × channels` samples.
//!
//! ## Default OFF
//!
//! The mixer constructs a [`LaneResampler`] only for the configured
//! clock-crossing lane, and only when `JASPER_FANIN_INPUT_RESAMPLER=enabled`.
//! When disabled the per-lane read path is the strict one-period read plus
//! catch-up drain, which is deliberately kept as the fallback.

use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;

use jasper_resampler::{
    clamp_i16, clamp_i32, spine_acc_to_i16, AudioRing, RateController, SincTable, RADIUS_FRAMES,
};

pub use decay::{CushionDecay, DecayFrozenReason, DecayParams, DecaySignals, BUFFER_ADJUST_PPM};

/// Observability counters for one armed lane resampler, cloned into the STATUS
/// snapshot. Absence of this object means the resampler is disabled.
#[derive(Clone)]
pub struct LaneResamplerObservability {
    /// True only while the lane is rendering real DAC-paced audio.
    pub locked: Arc<AtomicBool>,
    /// Cumulative input frames pushed into the resampler.
    pub input_frames: Arc<AtomicU64>,
    /// Cumulative output frames emitted (period-aligned).
    pub output_frames: Arc<AtomicU64>,
    /// Cumulative silence frames emitted while unlocked/underfilled.
    pub silence_frames: Arc<AtomicU64>,
    /// Cumulative frames dropped by ring overrun. Stays 0 in steady state;
    /// growth means the ring is undersized or the host is wildly off-rate.
    pub overrun_frames: Arc<AtomicU64>,
    /// Last bounded resampler ratio, in ppm × 1000 (milli-ppm). Signed value
    /// stored as i64 bits in a u64; the STATUS layer reinterprets.
    pub ratio_milli_ppm: Arc<AtomicU64>,
    /// Times the controller's output ppm clamp engaged (the loop demanded more
    /// than `max_adjust_ppm`). Lifetime count, survives `reset()`.
    pub clamp_count: Arc<AtomicU64>,
    /// Times the controller reset a clamped loop wound against the fill error
    /// (see `jasper_resampler::RateController::anti_windup_count`). Non-zero
    /// means the lane hit the safety clamp hard. Lifetime count.
    pub anti_windup_count: Arc<AtomicU64>,
    /// Lock acquisitions — a value past 1 means the lane keeps re-locking
    /// (host discontinuities / under-provisioned ring).
    pub lock_count: Arc<AtomicU64>,
    /// Underfill unlocks — the resampler starved (target too low or a host
    /// stall) and fell back to silence rather than reading past the buffer.
    pub unlock_count: Arc<AtomicU64>,
    /// Current ring fill in frames, republished every `render_period`. Held
    /// near `held_target_frames` by the DLL while locked.
    pub fill_frames: Arc<AtomicU64>,
    /// The acquisition CEILING (base target plus the full warm-up cushion),
    /// static for the lane's life — the value the held target snaps back to on
    /// any discontinuity.
    pub target_fill_frames: u64,
    /// The LIVE held target the controller is disciplining the ring toward —
    /// equal to `target_fill_frames` unless the DEFAULT-OFF post-lock cushion
    /// decay has lowered it. Republished every render period. This is the ONE
    /// authoritative held-target value: the host-clock DLL reads the same
    /// atomic as its setpoint, so the two controllers can never disagree about
    /// where the fill should sit.
    pub held_target_frames: Arc<AtomicU64>,
    /// Live cushion-decay state (all `0`/inert while the decay feature is off).
    /// `enabled` = startup configuration; `active` = actively decaying;
    /// `floor` = the configured decay floor;
    /// `frozen_reason` = the stringly-typed reason decay is currently paused
    /// (`""` while actively decaying).
    pub decay_enabled: bool,
    pub decay_active: Arc<AtomicBool>,
    pub decay_floor_frames: u64,
    pub learned_floor_frames: Arc<AtomicU64>,
    pub warm_resumes: Arc<AtomicU64>,
    pub latency_backoffs: Arc<AtomicU64>,
    pub decay_frozen_reason: Arc<AtomicU64>,
    /// Buffer motion in signed milli-ppm, added after clock correction.
    pub decay_demand_milli_ppm: Arc<AtomicI64>,
    /// The decay's DECLARED refill window — see `CushionDecay::refilling` and
    /// ADR-0214. Always false while decay is off.
    pub decay_refilling: Arc<AtomicBool>,
}

/// What [`LaneResampler::plan_period`] decided this render period should do —
/// the width-independent verdict the narrow and wide emit tails both act on.
enum RenderPlan {
    /// Unlocked, underfilled, or one period short of the buffered edge: fill the
    /// caller's period with digital zero and count it as silence.
    Silence,
    /// Locked with runway: emit one period, advancing the cursor by `ratio`
    /// input frames per output frame.
    Emit { ratio: f64 },
}

/// A per-input windowed-sinc resampler that turns a free-running (host-clocked)
/// lane into a DAC-paced one. Owns its own ring, sinc table, rate controller,
/// and fractional read cursor, composing the shared [`jasper_resampler`]
/// primitives.
pub struct LaneResampler {
    channels: usize,
    period_frames: usize,
    /// Buffered host-clock input. Pushed by `push_input`, read at the
    /// fractional cursor by `render_period`.
    ring: AudioRing,
    sinc_table: SincTable,
    controller: RateController,
    /// Base configured target. The acquisition CEILING is
    /// `target_fill_frames + warmup_cushion_frames`; the small fixed fill that
    /// replaces the catch-up sawtooth. The LIVE held target
    /// (`hold_fill_frames()`) is that ceiling unless [`CushionDecay`] has lowered
    /// it post-lock.
    target_fill_frames: usize,
    /// Extra frames added to the DLL hold target for the armed lane: the
    /// WARM-UP cushion that keeps the first jittery seconds of host arrival
    /// from dipping the cursor-relative fill below `minimum_safe_fill` and
    /// thrashing lock→silence→relock. It is HELD, never drained back to the
    /// base target — draining it over-consumes the bursty USB cold feed and
    /// produces a cold-start limit cycle on hardware.
    warmup_cushion_frames: usize,
    /// Output ppm safety bound (also drives the minimum-safe-fill margin).
    max_adjust_ppm: f64,
    /// Fractional read cursor in the ring's monotonic frame space.
    next_input_frame: f64,
    locked: bool,
    /// One second of real playback before an underfill clears stale input.
    acquisition_grace_periods: u32,
    /// Frames left in the startup de-click ramp. Set to one render period on
    /// every lock, then counted down to zero while rendering real audio.
    startup_ramp_frames_remaining: usize,
    /// Frames left in the SHUTDOWN de-click ramp — the mirror of the startup
    /// one. Armed with one render period whenever a session ends
    /// (`unlock_for_underfill`, `reset`), so the lane glides its last emitted
    /// frame to zero instead of stepping there in one sample. Without it a host
    /// that stops streaming mid-waveform produces a step discontinuity, which
    /// is an audible click at the DAC. Zero means "emit true silence".
    shutdown_ramp_frames_remaining: usize,
    /// The last frame this lane emitted, per channel, at spine scale. The
    /// shutdown ramp decays THIS toward zero, so the tail starts exactly where
    /// the audio stopped. Cleared once the tail is spent. `i32` holds either
    /// width losslessly (the narrow path writes `i16` values).
    last_frame: Vec<i32>,
    /// Consecutive real render periods since the most recent lock. Early
    /// underfills during acquisition retain buffered input so the lane can keep
    /// priming; after this reaches `acquisition_grace_periods`, underfill is treated as
    /// a real discontinuity and clears stale buffered audio.
    real_periods_since_lock: u32,
    // Lifetime counters mirrored into observability atomics on update.
    input_frames: Arc<AtomicU64>,
    output_frames: Arc<AtomicU64>,
    silence_frames: Arc<AtomicU64>,
    overrun_frames: Arc<AtomicU64>,
    ratio_milli_ppm: Arc<AtomicU64>,
    clamp_count: Arc<AtomicU64>,
    anti_windup_count: Arc<AtomicU64>,
    lock_count: Arc<AtomicU64>,
    unlock_count: Arc<AtomicU64>,
    /// Live ring fill in frames, republished every `render_period` so STATUS
    /// can show the buffer is being held near target.
    fill_frames: Arc<AtomicU64>,
    locked_state: Arc<AtomicBool>,
    /// The DEFAULT-OFF post-lock cushion-decay state machine. Owns the LIVE held
    /// target (`decay.held()`), lowered from the acquisition ceiling toward the
    /// configured floor while locked + DLL-l0 + calm, snapped back on any
    /// discontinuity. When disabled it pins the held target at the ceiling
    /// forever (`hold_fill_frames()` == `target + cushion`, current behaviour).
    decay: CushionDecay,
    /// The LIVE held target gauge — the single source of truth the STATUS layer
    /// and the outer host-clock DLL both read. Republished whenever the decay
    /// tick changes the held target. Owned (written) ONLY here.
    held_target_frames: Arc<AtomicU64>,
    /// Decay observability atomics, republished on every decay tick.
    decay_active: Arc<AtomicBool>,
    decay_frozen_reason: Arc<AtomicU64>,
    /// The decay's declared refill window — see
    /// [`LaneResamplerObservability::decay_refilling`].
    decay_refilling: Arc<AtomicBool>,
    /// Periods the OPEN window has run, for its leave log only.
    refill_window_periods: u64,
    /// The decay's live demand gauge — see
    /// [`LaneResamplerObservability::decay_demand_milli_ppm`].
    learned_floor_frames: Arc<AtomicU64>,
    warm_resumes: Arc<AtomicU64>,
    latency_backoffs: Arc<AtomicU64>,
    decay_demand_milli_ppm: Arc<AtomicI64>,
}

impl LaneResampler {
    /// Construct a resampler for `channels` interleaved channels at
    /// `period_frames` per render, holding the ring at
    /// `target_fill_frames + warmup_cushion_frames` and bounding pitch warp to
    /// `±max_adjust_ppm`.
    ///
    /// `warmup_cushion_frames` is added to `target_fill_frames` and held as the
    /// DLL setpoint. The `config.rs` `WARMUP_CUSHION_FRAMES` compiled default
    /// is an eight-period held cushion (`512 + 2048 = 2560` frames total); the
    /// shipped `usb_low_latency_48k` route runs a shallower six-period cushion
    /// (`512 + 1536 = 2048` frames total —
    /// `DEFAULT_USB_LOW_LATENCY_RESAMPLER_CUSHION_FRAMES` in
    /// `jasper/audio_runtime_plan.py`). Hardware soak/cold-start validation must
    /// pass before any lower route default ships.
    ///
    /// `ring_frames` is the input buffer depth: it MUST exceed
    /// `target_fill_frames` plus the warm-up cushion plus one render period plus
    /// the kernel radius, or the deep prefill could not seat. Returns an error
    /// string rather than a typed error so the caller can log-and-fall-back — a
    /// construction failure here must degrade to "no resampler", never crash
    /// the daemon.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        channels: usize,
        period_frames: u32,
        sample_rate: u32,
        target_fill_frames: usize,
        warmup_cushion_frames: usize,
        max_adjust_ppm: f64,
        ring_frames: usize,
        decay_params: DecayParams,
    ) -> Result<Self, String> {
        if channels == 0 {
            return Err("lane resampler channels must be > 0".to_string());
        }
        let period_frames = period_frames as usize;
        if period_frames == 0 {
            return Err("lane resampler period_frames must be > 0".to_string());
        }
        let radius = RADIUS_FRAMES as usize;
        // The ring must hold the deepest seating the lock ever uses (target +
        // warm-up cushion) plus one period of fresh arrival plus the kernel
        // radius, or the deep prefill could never accumulate. The decay only
        // LOWERS the held target, so the ring stays sized for the ceiling.
        let min_ring = target_fill_frames + warmup_cushion_frames + period_frames + radius + 1;
        if ring_frames < min_ring {
            return Err(format!(
                "lane resampler ring_frames={ring_frames} too small; need >= {min_ring} \
                 (target_fill={target_fill_frames} + warmup_cushion={warmup_cushion_frames} \
                 + period={period_frames} + radius={radius} + 1)"
            ));
        }
        let ring = AudioRing::new(ring_frames, channels)
            .map_err(|e| format!("lane resampler ring: {e}"))?;
        let acquisition_grace_periods = (sample_rate / period_frames.max(1) as u32).max(1);
        // The acquisition CEILING the decay lowers FROM and snaps back TO.
        let ceiling = (target_fill_frames + warmup_cushion_frames) as u64;
        let decay = decay_params.build(ceiling, period_frames as u32, sample_rate, max_adjust_ppm);
        Ok(Self {
            channels,
            period_frames,
            ring,
            sinc_table: SincTable::new(),
            // max_resync disabled (`Some(0.0)`): the fill legitimately moves by
            // more than one render period during USB burst acquisition, and
            // that is an excursion to slew through, not a discontinuity — hard
            // discontinuities arrive here as PCM xruns / explicit resets. With
            // the shared default enabled, a deeper held cushion repeatedly
            // resets the DLL at unity and the ring drifts away from target.
            controller: RateController::with_max_resync(
                max_adjust_ppm,
                period_frames as u32,
                sample_rate,
                Some(0.0),
            ),
            target_fill_frames,
            warmup_cushion_frames,
            max_adjust_ppm,
            next_input_frame: 0.0,
            locked: false,
            acquisition_grace_periods,
            startup_ramp_frames_remaining: 0,
            shutdown_ramp_frames_remaining: 0,
            last_frame: vec![0; channels],
            real_periods_since_lock: 0,
            input_frames: Arc::new(AtomicU64::new(0)),
            output_frames: Arc::new(AtomicU64::new(0)),
            silence_frames: Arc::new(AtomicU64::new(0)),
            overrun_frames: Arc::new(AtomicU64::new(0)),
            ratio_milli_ppm: Arc::new(AtomicU64::new(0)),
            clamp_count: Arc::new(AtomicU64::new(0)),
            anti_windup_count: Arc::new(AtomicU64::new(0)),
            lock_count: Arc::new(AtomicU64::new(0)),
            unlock_count: Arc::new(AtomicU64::new(0)),
            fill_frames: Arc::new(AtomicU64::new(0)),
            locked_state: Arc::new(AtomicBool::new(false)),
            held_target_frames: Arc::new(AtomicU64::new(ceiling)),
            decay_active: Arc::new(AtomicBool::new(false)),
            decay_frozen_reason: Arc::new(AtomicU64::new(DecayFrozenReason::code(
                decay.frozen_reason(),
            ))),
            learned_floor_frames: Arc::new(AtomicU64::new(decay.floor())),
            warm_resumes: Arc::new(AtomicU64::new(0)),
            latency_backoffs: Arc::new(AtomicU64::new(0)),
            decay_demand_milli_ppm: Arc::new(AtomicI64::new(0)),
            decay_refilling: Arc::new(AtomicBool::new(false)),
            refill_window_periods: 0,
            decay,
        })
    }

    /// The current published ring fill in frames — the same value STATUS shows.
    /// The USB DIRECT read calls this every period, so it must stay a single
    /// relaxed load and never allocate the way `observability()` does.
    pub fn fill_frames_gauge(&self) -> u64 {
        self.fill_frames.load(Ordering::Relaxed)
    }

    /// Clone the observability handles for the STATUS snapshot.
    pub fn observability(&self) -> LaneResamplerObservability {
        LaneResamplerObservability {
            locked: Arc::clone(&self.locked_state),
            input_frames: Arc::clone(&self.input_frames),
            output_frames: Arc::clone(&self.output_frames),
            silence_frames: Arc::clone(&self.silence_frames),
            overrun_frames: Arc::clone(&self.overrun_frames),
            ratio_milli_ppm: Arc::clone(&self.ratio_milli_ppm),
            clamp_count: Arc::clone(&self.clamp_count),
            anti_windup_count: Arc::clone(&self.anti_windup_count),
            lock_count: Arc::clone(&self.lock_count),
            unlock_count: Arc::clone(&self.unlock_count),
            fill_frames: Arc::clone(&self.fill_frames),
            // STATUS's `target_fill_frames` is the static ceiling; the LIVE held
            // target is the separate `held_target_frames` gauge below.
            target_fill_frames: self.ceiling_fill_frames() as u64,
            held_target_frames: Arc::clone(&self.held_target_frames),
            decay_enabled: self.decay.enabled(),
            decay_active: Arc::clone(&self.decay_active),
            decay_floor_frames: self.decay.floor(),
            decay_frozen_reason: Arc::clone(&self.decay_frozen_reason),
            learned_floor_frames: Arc::clone(&self.learned_floor_frames),
            warm_resumes: Arc::clone(&self.warm_resumes),
            latency_backoffs: Arc::clone(&self.latency_backoffs),
            decay_demand_milli_ppm: Arc::clone(&self.decay_demand_milli_ppm),
            decay_refilling: Arc::clone(&self.decay_refilling),
        }
    }

    /// Push `samples` (interleaved `i16`, this lane's just-read frames) into the
    /// input ring. A producer that outruns the ring drops oldest-first and
    /// counts the overrun — the resampler keeps running on the freshest audio.
    ///
    /// The ring stores spine-scale `i32`, so this widens on the way in
    /// ([`AudioRing::push_interleaved_narrow`]). That is bit-transparent for the
    /// narrow render path — see [`Self::render_period`].
    pub fn push_input(&mut self, samples: &[i16]) {
        let frames = samples.len() / self.channels;
        if frames == 0 {
            return;
        }
        self.input_frames
            .fetch_add(frames as u64, Ordering::Relaxed);
        let dropped = self
            .ring
            .push_interleaved_narrow(&samples[..frames * self.channels]);
        if dropped > 0 {
            self.overrun_frames.fetch_add(dropped, Ordering::Relaxed);
        }
    }

    /// Push `samples` (interleaved **spine-scale `i32`**) into the input ring —
    /// the wide sibling of [`Self::push_input`], for a lane whose capture is
    /// already S32 and must not be narrowed at ingest. Nothing is discarded on
    /// the way in. MUST be paired with [`Self::render_period_wide`]; the mixer
    /// picks ONE width pairing per lane from the resolved wire.
    pub fn push_input_wide(&mut self, samples: &[i32]) {
        let frames = samples.len() / self.channels;
        if frames == 0 {
            return;
        }
        self.input_frames
            .fetch_add(frames as u64, Ordering::Relaxed);
        let dropped = self
            .ring
            .push_interleaved(&samples[..frames * self.channels]);
        if dropped > 0 {
            self.overrun_frames.fetch_add(dropped, Ordering::Relaxed);
        }
    }

    /// Render exactly one period of DAC-paced output into `out` (interleaved
    /// `i16`, length `period_frames × channels`). Returns the number of frames
    /// that are real audio (vs silence) for the caller's mixing decision —
    /// `period_frames` when locked and rendering, `0` when silent.
    ///
    /// The state machine lives in [`Self::plan_period`]; this is its narrow
    /// emit tail. Each interpolated sample is narrowed ONCE, by
    /// [`spine_acc_to_i16`]: the ring is spine-scale, so dividing the exact
    /// power-of-two widening back out before the round is bit-transparent.
    pub fn render_period(&mut self, out: &mut [i16]) -> usize {
        // PANIC-AUDITED: out is the caller's own period buffer, sized period_frames x channels
        debug_assert_eq!(out.len(), self.period_frames * self.channels);
        let ratio = match self.plan_period() {
            RenderPlan::Silence => {
                return self.render_silence(out);
            }
            RenderPlan::Emit { ratio } => ratio,
        };
        for frame in 0..self.period_frames {
            let ramp_gain = self.frame_ramp_gain();
            for channel in 0..self.channels {
                let sample = spine_acc_to_i16(self.sinc_table.interpolate(
                    &self.ring,
                    self.next_input_frame,
                    channel,
                ));
                out[frame * self.channels + channel] = if ramp_gain < 1.0 {
                    clamp_i16(sample as f64 * ramp_gain)
                } else {
                    sample
                };
            }
            self.advance_cursor(ratio);
        }
        self.remember_last_frame_narrow(out);
        self.finish_period()
    }

    /// Render exactly one period into `out` (interleaved **spine-scale `i32`**)
    /// — the wide sibling of [`Self::render_period`], for a lane on a wide wire.
    /// The interpolator's accumulator is rounded at the i32 rails
    /// ([`clamp_i32`]) instead of being divided down to i16 first: there is no
    /// `>> 16` anywhere on this route, so a hi-res source's low bits reach the
    /// mixer's sum intact.
    pub fn render_period_wide(&mut self, out: &mut [i32]) -> usize {
        // PANIC-AUDITED: out is the caller's own period buffer, sized period_frames x channels
        debug_assert_eq!(out.len(), self.period_frames * self.channels);
        let ratio = match self.plan_period() {
            RenderPlan::Silence => {
                return self.render_silence_wide(out);
            }
            RenderPlan::Emit { ratio } => ratio,
        };
        for frame in 0..self.period_frames {
            let ramp_gain = self.frame_ramp_gain();
            for channel in 0..self.channels {
                let sample = clamp_i32(self.sinc_table.interpolate(
                    &self.ring,
                    self.next_input_frame,
                    channel,
                ));
                out[frame * self.channels + channel] = if ramp_gain < 1.0 {
                    clamp_i32(sample as f64 * ramp_gain)
                } else {
                    sample
                };
            }
            self.advance_cursor(ratio);
        }
        self.remember_last_frame_wide(out);
        self.finish_period()
    }

    /// The startup de-click ramp gain for the frame about to be emitted. MUST
    /// be read BEFORE [`Self::advance_cursor`] decrements the counter.
    fn frame_ramp_gain(&self) -> f64 {
        if self.startup_ramp_frames_remaining > 0 {
            let frames_done = self.period_frames - self.startup_ramp_frames_remaining;
            (frames_done + 1) as f64 / self.period_frames as f64
        } else {
            1.0
        }
    }

    fn advance_cursor(&mut self, ratio: f64) {
        self.next_input_frame += ratio;
        self.startup_ramp_frames_remaining = self.startup_ramp_frames_remaining.saturating_sub(1);
    }

    /// The shutdown de-click gain for frame `frame` of the tail period. Falls
    /// from unity to exactly zero over the period, so the period after the tail
    /// is true silence with no residual step.
    fn shutdown_gain(&self, frame: usize) -> f64 {
        1.0 - (frame + 1) as f64 / self.period_frames as f64
    }

    /// Arm the shutdown de-click tail, unless the lane was already silent.
    /// Called from every session-ending path. The tail can only scale the last
    /// emitted frame DOWN toward zero, so it can never raise output above what
    /// the lane was already producing.
    fn arm_shutdown_ramp(&mut self) {
        if self.last_frame.iter().any(|&s| s != 0) {
            self.shutdown_ramp_frames_remaining = self.period_frames;
        }
    }

    /// Record the period's LAST emitted frame so a later shutdown can decay
    /// from it. Once per period, not once per frame — only the final frame is
    /// ever read back.
    fn remember_last_frame_narrow(&mut self, out: &[i16]) {
        let base = (self.period_frames - 1) * self.channels;
        for channel in 0..self.channels {
            self.last_frame[channel] = out[base + channel] as i32;
        }
    }

    /// Wide sibling of [`Self::remember_last_frame_narrow`].
    fn remember_last_frame_wide(&mut self, out: &[i32]) {
        let base = (self.period_frames - 1) * self.channels;
        self.last_frame
            .copy_from_slice(&out[base..base + self.channels]);
    }

    /// Retire a spent tail so every later silent period is true digital zero.
    fn finish_shutdown_tail(&mut self) {
        self.shutdown_ramp_frames_remaining = 0;
        self.last_frame.fill(0);
    }

    /// Post-emit bookkeeping shared by both widths. Frees ring history behind
    /// the cursor while keeping the kernel's left taps.
    fn finish_period(&mut self) -> usize {
        let keep_from = self.next_input_frame.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        self.output_frames
            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
        self.real_periods_since_lock = self.real_periods_since_lock.saturating_add(1);
        self.period_frames
    }

    /// The width-independent half of a render period: lock acquisition, fill
    /// publication, the underfill / read-past-the-edge fail-closed gates, and
    /// the DLL ratio. The narrow and wide emit tails MUST share this one state
    /// machine; two copies would drift on a lock or unlock rule.
    fn plan_period(&mut self) -> RenderPlan {
        if !self.locked {
            // While priming, the published fill is the buffered-input depth, so
            // STATUS shows the lane filling toward the prefill before it locks.
            self.publish_fill(self.ring.fill_frames() as u64);
            self.try_lock();
        }
        if !self.locked {
            return RenderPlan::Silence;
        }

        // A reader-overrun (the ring dropped frames the cursor hadn't reached)
        // skips the cursor forward to the oldest live frame; without it the
        // cursor would read zeros.
        let read = self.ring.read_frame() as f64;
        if self.next_input_frame < read {
            self.next_input_frame = read;
        }

        let fill = self.ring.write_frame() as f64 - self.next_input_frame;
        // Locked: the CURSOR-RELATIVE fill is what the DLL disciplines toward
        // target, and what STATUS publishes.
        self.publish_fill(fill.max(0.0) as u64);
        let minimum_safe_fill = self.minimum_safe_fill_frames() as f64;
        if fill < minimum_safe_fill {
            self.unlock_for_underfill();
            return RenderPlan::Silence;
        }

        let error_frames = fill - self.decay.held_exact();
        let ratio = self.controller.next_ratio(error_frames) + self.decay.demand_ppm() / 1e6;
        self.publish_ratio();

        // Guard: emitting one period at this ratio must not read past the
        // newest written frame (kernel rightmost tap included). If it would,
        // unlock and silence — the fail-closed boundary.
        let required_end = self.next_input_frame + ratio * self.period_frames as f64;
        if required_end + RADIUS_FRAMES as f64 > self.ring.write_frame() as f64 {
            self.unlock_for_underfill();
            return RenderPlan::Silence;
        }

        RenderPlan::Emit { ratio }
    }

    /// Drop the lane's standing latency down to its held target by discarding
    /// the OLDEST buffered input, WITHOUT losing lock or resetting the
    /// controller. Returns the number of input frames dropped (0 when the lane
    /// is unlocked or already at/below its held target).
    ///
    /// The lane's live latency is the CURSOR-RELATIVE fill —
    /// `write_frame - next_input_frame`, the same value [`render_period`]
    /// disciplines toward [`hold_fill_frames`]. On hardware the USB lane was
    /// observed sitting at ~1919 frames against a 512-frame held target with
    /// lock churn: each idle/xrun/underfill `reset()` re-primed the DLL and the
    /// fill crept back up, deepening with every relock. A `reset()`-based trim
    /// is therefore the WRONG tool — it is the very lock-loss that produced the
    /// churn.
    ///
    /// This trim keeps the newest `hold_fill_frames()` frames instead: it
    /// advances the fractional read cursor over the oldest buffered frames and
    /// frees the ring history behind it. The only discontinuity is the one skip
    /// at the drop boundary — a single glitch, not a lock loss. `locked`, the
    /// `RateController` loop state, `real_periods_since_lock` and the startup
    /// ramp all survive, so the DLL sees the fill snap to target (an error step
    /// it already handles) rather than a re-acquisition.
    pub fn trim_ring(&mut self) -> u64 {
        if !self.locked {
            return 0;
        }
        // A reader-overrun could have advanced read_frame past the cursor; the
        // same guard render_period uses keeps the cursor at/after the oldest
        // live frame so the fill below is never negative.
        let read = self.ring.read_frame() as f64;
        if self.next_input_frame < read {
            self.next_input_frame = read;
        }
        let write = self.ring.write_frame() as f64;
        let fill = write - self.next_input_frame;
        let target = self.hold_fill_frames() as f64;
        if fill <= target {
            return 0;
        }
        let drop = fill - target;
        self.next_input_frame += drop;
        // Free ring history behind the new cursor, keeping the kernel's left
        // taps (identical bookkeeping to the end of render_period).
        let keep_from = self.next_input_frame.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        // Republish before the next render period runs, so STATUS never shows
        // the pre-trim fill.
        self.publish_fill(target.max(0.0) as u64);
        drop.round() as u64
    }

    /// Discard buffered input and re-prime on the next render (a hard
    /// discontinuity: a host pause/seek that steps the fill). The mixer calls
    /// this when the lane goes idle so a fresh play starts clean.
    pub fn reset(&mut self) {
        self.ring.clear();
        self.controller.reset();
        self.next_input_frame = 0.0;
        self.locked = false;
        self.locked_state.store(false, Ordering::Relaxed);
        self.startup_ramp_frames_remaining = 0;
        self.arm_shutdown_ramp();
        self.real_periods_since_lock = 0;
        self.snap_decay_back(DecayFrozenReason::Unlocked);
        self.publish_ratio();
    }

    // A shallow start makes buffer refill saturate the correction gauge used
    // by the host-clock probe. Start at the held target so it measures the host.
    fn try_lock(&mut self) {
        if self.ring.fill_frames() < self.startup_prefill_frames() {
            return;
        }
        let seat = self.hold_fill_frames();
        self.next_input_frame = (self.ring.write_frame() - seat as u64) as f64;
        let keep_from = self.next_input_frame.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        self.locked = true;
        self.locked_state.store(true, Ordering::Relaxed);
        self.startup_ramp_frames_remaining = self.period_frames;
        // A fresh lock supersedes any pending tail: the startup ramp owns the
        // transition back to audio, so a stale tail must not play under it.
        self.shutdown_ramp_frames_remaining = 0;
        // Not redundant with the line above. `plan_period` can lock here and
        // then `unlock_for_underfill` in the SAME call (both post-lock gates sit
        // after `try_lock`) without emitting a frame in between; that unlock
        // arms the tail, and a still-remembered frame from the PREVIOUS session
        // would decay stale audio into a session that never played.
        self.last_frame.fill(0);
        self.real_periods_since_lock = 0;
        self.controller.reset();
        self.lock_count.fetch_add(1, Ordering::Relaxed);
    }

    fn unlock_for_underfill(&mut self) {
        self.locked = false;
        self.locked_state.store(false, Ordering::Relaxed);
        self.unlock_count.fetch_add(1, Ordering::Relaxed);
        let acquisition_underfill = self.real_periods_since_lock < self.acquisition_grace_periods;
        if !acquisition_underfill {
            self.ring.clear();
        }
        self.controller.reset();
        self.next_input_frame = 0.0;
        self.startup_ramp_frames_remaining = 0;
        self.arm_shutdown_ramp();
        self.real_periods_since_lock = 0;
        self.snap_decay_back(DecayFrozenReason::Unlocked);
        self.publish_fill(if acquisition_underfill {
            self.ring.fill_frames() as u64
        } else {
            0
        });
        self.publish_ratio();
    }

    /// Snap the decay's held target back to the acquisition ceiling and publish
    /// the raised gauge immediately. Inert when the decay feature is off.
    fn snap_decay_back(&mut self, reason: DecayFrozenReason) {
        self.decay.snap_back(reason);
        self.publish_decay_gauges();
    }

    /// Force the still-locked `NotL0` snap-back that opens a cushion-refill
    /// window — the `DECAY_SNAP` control verb's only job. Identical to the
    /// snap a real ladder demotion takes, so what it provokes is the shipping
    /// path, not a test double (ADR-0214). Returns whether a window opened
    /// (false when the decay is off or the target was already at the ceiling).
    pub fn force_decay_snap_back(&mut self) -> bool {
        let was_refilling = self.decay.refilling();
        self.snap_decay_back(DecayFrozenReason::NotL0);
        self.note_refill_edge(was_refilling);
        self.decay.refilling()
    }

    /// Republish the held-target gauge + decay observability atomics. MUST be
    /// called by every path that mutates the decay's held target, so STATUS and
    /// the outer DLL setpoint always read a consistent snapshot. Relaxed
    /// stores; no allocation.
    fn publish_decay_gauges(&self) {
        self.learned_floor_frames
            .store(self.decay.learned_floor(), Ordering::Relaxed);
        self.warm_resumes
            .store(self.decay.resumes(), Ordering::Relaxed);
        self.latency_backoffs
            .store(self.decay.backoffs(), Ordering::Relaxed);
        // Refill flag BEFORE the raised held target: these are unordered relaxed
        // stores, and a servo tick landing between them must never see the raised
        // target with the window still closed — that is the one interleaving that
        // feeds the ladder a railed refill as a measurement (ADR-0214).
        self.decay_refilling
            .store(self.decay.refilling(), Ordering::Relaxed);
        self.held_target_frames
            .store(self.decay.held(), Ordering::Relaxed);
        self.decay_active
            .store(self.decay.active(), Ordering::Relaxed);
        let demand_ppm = self.decay.demand_ppm();
        self.decay_demand_milli_ppm
            .store((demand_ppm * 1000.0).round() as i64, Ordering::Relaxed);
        self.decay_frozen_reason.store(
            DecayFrozenReason::code(self.decay.frozen_reason()),
            Ordering::Relaxed,
        );
    }

    /// Whether the lane is currently locked. STATUS reads the `locked` atomic
    /// instead, so this is test-only; `#[cfg(test)]` keeps it out of the
    /// `-D warnings` binary build.
    #[cfg(test)]
    pub fn is_locked(&self) -> bool {
        self.locked
    }

    /// Render one period of silence — or, when a session has just ended, the
    /// shutdown de-click tail. Returns what the caller must MIX: the tail is
    /// real audio, so it reports `period_frames`; true silence reports 0. A
    /// tail that reported 0 would be written here and then dropped by the
    /// mixer's `sum_buf[..active]` slice, making the de-click a no-op.
    fn render_silence(&mut self, out: &mut [i16]) -> usize {
        if self.shutdown_ramp_frames_remaining > 0 {
            for frame in 0..self.period_frames {
                let gain = self.shutdown_gain(frame);
                for channel in 0..self.channels {
                    out[frame * self.channels + channel] =
                        clamp_i16(self.last_frame[channel] as f64 * gain);
                }
            }
            self.finish_shutdown_tail();
            self.output_frames
                .fetch_add(self.period_frames as u64, Ordering::Relaxed);
            return self.period_frames;
        }
        out.fill(0);
        self.count_silence_period();
        0
    }

    /// The wide sibling of [`Self::render_silence`].
    fn render_silence_wide(&mut self, out: &mut [i32]) -> usize {
        if self.shutdown_ramp_frames_remaining > 0 {
            for frame in 0..self.period_frames {
                let gain = self.shutdown_gain(frame);
                for channel in 0..self.channels {
                    out[frame * self.channels + channel] =
                        (self.last_frame[channel] as f64 * gain) as i32;
                }
            }
            self.finish_shutdown_tail();
            self.output_frames
                .fetch_add(self.period_frames as u64, Ordering::Relaxed);
            return self.period_frames;
        }
        out.fill(0);
        self.count_silence_period();
        0
    }

    fn count_silence_period(&mut self) {
        self.silence_frames
            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
    }

    /// Minimum buffered frames to safely render one period at the worst-case
    /// (max-ppm) ratio with kernel headroom. MUST delegate to the shared
    /// `jasper_resampler` helper — the single source of truth the config-time
    /// decay-floor validation also uses.
    fn minimum_safe_fill_frames(&self) -> usize {
        jasper_resampler::minimum_safe_fill_frames(
            self.period_frames as u32,
            self.max_adjust_ppm + decay::BUFFER_ADJUST_PPM,
        )
    }

    /// Frames the ring must hold before lock seats the cursor at the LIVE held
    /// target (the acquisition ceiling, `target + warm-up cushion`) with kernel
    /// headroom.
    fn startup_prefill_frames(&self) -> usize {
        self.hold_fill_frames() + RADIUS_FRAMES as usize + 1
    }

    /// The LIVE held target the controller disciplines the ring toward. Read
    /// from the held-target gauge (the single source of truth) so
    /// `render_period`'s DLL error, `trim_ring`'s drop target and the
    /// STATUS/outer-DLL setpoint can never disagree.
    fn hold_fill_frames(&self) -> usize {
        self.held_target_frames.load(Ordering::Relaxed) as usize
    }

    /// The static acquisition ceiling (`target + full warm-up cushion`) — the
    /// value the held target snaps back to on any discontinuity, and the depth
    /// the lock always seats at. Independent of the live decay.
    fn ceiling_fill_frames(&self) -> usize {
        self.target_fill_frames + self.warmup_cushion_frames
    }

    pub fn latency_context(&mut self, connection: u64, failed: bool) {
        self.decay.context(connection, failed, self.locked);
        self.publish_decay_gauges();
    }

    pub fn tick_decay(&mut self, dll_l0_locked: bool, commanded_ppm_abs: f64) {
        let was_refilling = self.decay.refilling();
        self.decay.tick(DecaySignals {
            locked: self.locked,
            dll_l0_locked,
            commanded_ppm_abs,
            // This period's own command, from the controller that produced it.
            ratio_saturated: self.controller.ratio_ppm().abs() >= self.max_adjust_ppm,
        });
        self.publish_decay_gauges();
        self.note_refill_edge(was_refilling);
    }

    fn note_refill_edge(&mut self, was_refilling: bool) {
        match (was_refilling, self.decay.refilling()) {
            (false, true) => {
                self.refill_window_periods = 0;
                log::info!(
                    "event=fanin.decay_refill state=enter reason=not_l0 deficit_frames={}",
                    (self.ceiling_fill_frames() as u64)
                        .saturating_sub(self.fill_frames.load(Ordering::Relaxed)),
                );
            }
            (true, false) => log::info!(
                "event=fanin.decay_refill state=leave periods={}",
                self.refill_window_periods,
            ),
            (true, true) => {
                self.refill_window_periods = self.refill_window_periods.saturating_add(1)
            }
            (false, false) => {}
        }
    }

    fn publish_ratio(&self) {
        // Store ppm × 1000 (milli-ppm) as i64 bits in the u64 atomic.
        let milli_ppm = (self.controller.ratio_ppm() * 1000.0).round() as i64;
        self.ratio_milli_ppm
            .store(milli_ppm as u64, Ordering::Relaxed);
        // Mirror the controller's lifetime rail counters alongside the ratio
        // they qualify, so STATUS can show when the bounded ratio was pinned at
        // ±max_adjust_ppm (#3464).
        self.clamp_count
            .store(self.controller.clamp_count(), Ordering::Relaxed);
        self.anti_windup_count
            .store(self.controller.anti_windup_count(), Ordering::Relaxed);
    }

    fn publish_fill(&self, frames: u64) {
        self.fill_frames.store(frames, Ordering::Relaxed);
    }
}

#[path = "latency.rs"]
mod decay;

#[cfg(test)]
mod tests {
    use super::*;
    use jasper_host_clock::{
        Action, HostClock, HostClockConfig, Ladder, Obs, ObsMode, ProbeResult,
    };
    use jasper_resampler::clamp_i16;

    const RATE: u32 = 48_000;
    const PERIOD: u32 = 256;
    const TARGET: usize = 512;
    /// Warm-up cushion used in unit tests. The `usb_low_latency_48k` route
    /// defaults to a deeper six-period held cushion; one period keeps the test
    /// fixtures compact while preserving the same held-target behavior.
    const CUSHION: usize = PERIOD as usize;
    const MAX_PPM: f64 = 500.0;
    const RING: usize = 8192;

    fn build() -> LaneResampler {
        LaneResampler::new(
            2,
            PERIOD,
            RATE,
            TARGET,
            CUSHION,
            MAX_PPM,
            RING,
            DecayParams::disabled(),
        )
        .expect("resampler builds")
    }

    /// Frames that must be buffered for the held-cushion lock to seat:
    /// `TARGET + CUSHION + radius + 1`, plus a little slack the tests push.
    fn deep_prefill() -> usize {
        TARGET + CUSHION + RADIUS_FRAMES as usize + 1
    }

    /// Deterministic interleaved stereo tone, bounded inside i16.
    fn tone(frames: usize) -> Vec<i16> {
        let mut out = Vec::with_capacity(frames * 2);
        for n in 0..frames {
            let t = n as f64;
            let l = clamp_i16(8000.0 * (t * 0.013).sin());
            let r = clamp_i16(7000.0 * (t * 0.019).cos());
            out.push(l);
            out.push(r);
        }
        out
    }

    /// A phase-continuous tone so streaming pushes don't repeat from 0 (used by
    /// the cold-start models where successive bursts must be one signal).
    fn tone_at(phase: usize, frames: usize) -> Vec<i16> {
        let mut out = Vec::with_capacity(frames * 2);
        for n in 0..frames {
            let t = (phase + n) as f64;
            out.push(clamp_i16(8000.0 * (t * 0.013).sin()));
            out.push(clamp_i16(7000.0 * (t * 0.019).cos()));
        }
        out
    }

    #[test]
    fn rejects_undersized_ring_and_zero_dims() {
        // Ring smaller than target+cushion+period+radius+1 must be rejected, not
        // silently unable to seat the deep prefill.
        let d = DecayParams::disabled;
        assert!(
            LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, TARGET, d()).is_err()
        );
        assert!(LaneResampler::new(0, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, RING, d()).is_err());
        assert!(LaneResampler::new(2, 0, RATE, TARGET, CUSHION, MAX_PPM, RING, d()).is_err());
        assert!(LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, RING, d()).is_ok());
        // The cushion is part of the minimum ring: a ring that would fit
        // target+period+radius but NOT the cushion is rejected.
        let just_under = TARGET + PERIOD as usize + RADIUS_FRAMES as usize + 1;
        assert!(
            LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, just_under, d()).is_err(),
            "ring must include the warm-up cushion in its minimum"
        );
    }

    #[test]
    fn silent_until_prefilled_then_locks_and_renders() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        assert_eq!(r.render_period(&mut out), 0);
        assert!(out.iter().all(|&s| s == 0));
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 0);

        r.push_input(&tone(deep_prefill() + 64));
        let n = r.render_period(&mut out);
        assert_eq!(n, PERIOD as usize, "locked render emits a full period");
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 1);
        assert!(out.iter().any(|&s| s != 0), "real audio, not silence");
    }

    #[test]
    fn unity_rate_steady_state_holds_fill_near_target() {
        // Producer feeds exactly one period per render at the DAC rate (a lane
        // that is already on-rate): the resampler must hold the cursor and not
        // drift the fill, staying locked indefinitely.
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        let block = tone(PERIOD as usize);
        r.push_input(&tone(deep_prefill()));
        for _ in 0..2000 {
            r.push_input(&block);
            r.render_period(&mut out);
        }
        assert!(r.locked, "on-rate lane must stay locked");
        let ppm = r.controller.ratio_ppm();
        assert!(ppm.abs() <= MAX_PPM + 1e-6, "ratio within clamp: {ppm}");
    }

    #[test]
    fn observability_publishes_fill_near_target_when_locked() {
        let mut r = build();
        let obs = r.observability();
        assert_eq!(
            obs.target_fill_frames,
            (TARGET + CUSHION) as u64,
            "target echoes the held controller setpoint"
        );
        assert_eq!(obs.fill_frames.load(Ordering::Relaxed), 0);

        let mut out = vec![0i16; PERIOD as usize * 2];
        let block = tone(PERIOD as usize);
        r.push_input(&tone(deep_prefill()));
        for _ in 0..500 {
            r.push_input(&block);
            r.render_period(&mut out);
        }
        assert!(r.locked, "on-rate lane must lock");
        let fill = obs.fill_frames.load(Ordering::Relaxed);
        // Held within one period of the controller target; a one-period band
        // absorbs the cursor's fractional walk.
        let target = (TARGET + CUSHION) as i64;
        assert!(
            (fill as i64 - target).abs() <= PERIOD as i64,
            "published fill={fill} must hold near target={target} when locked"
        );
    }

    #[test]
    fn observability_publishes_fill_during_prefill() {
        // Before locking, the published fill tracks the buffered-input depth, so
        // "filling toward the prefill threshold" is distinguishable from a
        // stuck-at-zero dead lane.
        let mut r = build();
        let obs = r.observability();
        let mut out = vec![0i16; PERIOD as usize * 2];
        let partial = TARGET / 2;
        r.push_input(&tone(partial));
        assert_eq!(r.render_period(&mut out), 0, "still priming → silence");
        assert!(!r.locked);
        assert_eq!(
            obs.fill_frames.load(Ordering::Relaxed),
            partial as u64,
            "prefill fill tracks buffered-input depth"
        );
    }

    #[test]
    fn faster_producer_drives_drain_ratio_above_unity() {
        // The capture-follower sign gate: a host that feeds FASTER than the DAC
        // drains (ratio > 1) so the ring does not grow without bound. Feed ~150
        // ppm fast by occasionally pushing an extra frame.
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill()));
        let block = tone(PERIOD as usize);
        let extra = tone(1);
        let mut acc = 0.0f64;
        for _ in 0..20000 {
            r.push_input(&block);
            acc += PERIOD as f64 * 150.0 / 1.0e6; // ~150 ppm of extra frames
            if acc >= 1.0 {
                r.push_input(&extra);
                acc -= 1.0;
            }
            r.render_period(&mut out);
        }
        assert!(r.locked, "must stay locked tracking a fast producer");
        assert!(
            r.controller.ratio_ppm() > 0.0,
            "a faster producer must drive ratio > 1 (drain), got {} ppm",
            r.controller.ratio_ppm()
        );
    }

    #[test]
    fn overrun_is_counted_not_panicked() {
        let mut r = build();
        r.push_input(&tone(RING * 2));
        assert!(
            r.overrun_frames.load(Ordering::Relaxed) > 0,
            "a ring overflow must be counted"
        );
    }

    // ---- trim_ring: keep-newest, lock-preserving standing-fill trim -------

    /// From a DEEP cursor-relative fill, `trim_ring` must preserve lock, take
    /// no unlock/relock, snap the published fill to the held target, and keep
    /// the newest audio.
    #[test]
    fn trim_ring_drops_to_target_without_losing_lock() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        // Lock on a normal prefill.
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        let locks_before = r.lock_count.load(Ordering::Relaxed);
        let unlocks_before = r.unlock_count.load(Ordering::Relaxed);
        assert_eq!(locks_before, 1);
        assert_eq!(unlocks_before, 0);

        // A big burst puts the cursor-relative fill far above the held target
        // (the on-device 1919-vs-512 standing head-start).
        r.push_input(&tone(4000));
        let fill_before = r.ring.write_frame() as f64 - r.next_input_frame;
        let held = r.hold_fill_frames() as f64;
        assert!(
            fill_before > held + PERIOD as f64,
            "precondition: fill {fill_before} must be well above held target {held}"
        );
        let write_before = r.ring.write_frame();

        let dropped = r.trim_ring();

        assert!(dropped > 0, "a fill above target must drop frames");
        let fill_after = r.ring.write_frame() as f64 - r.next_input_frame;
        assert!(
            (fill_after - held).abs() < 1.0,
            "post-trim cursor fill {fill_after} must equal held target {held}"
        );
        assert_eq!(
            dropped as f64,
            (fill_before - held).round(),
            "dropped count must be the excess above target"
        );
        // write_frame untouched: the newest audio is preserved and only the
        // oldest head-start was skipped.
        assert_eq!(r.ring.write_frame(), write_before);
        assert!(r.locked, "trim must NOT drop lock");
        assert_eq!(
            r.lock_count.load(Ordering::Relaxed),
            locks_before,
            "trim must not re-lock (lock_count unchanged)"
        );
        assert_eq!(
            r.unlock_count.load(Ordering::Relaxed),
            unlocks_before,
            "trim must not unlock (unlock_count unchanged)"
        );
        assert_eq!(
            r.fill_frames.load(Ordering::Relaxed),
            held as u64,
            "STATUS fill must snap to the held target after trim"
        );
    }

    /// After a trim, the lane keeps rendering DAC-paced real audio from the
    /// retained newest window — no silence gap, no relock. This is the
    /// "single glitch at the drop boundary, not a lock loss" contract.
    #[test]
    fn trim_ring_keeps_rendering_real_audio_after_the_drop() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        r.push_input(&tone(4000));
        assert!(r.trim_ring() > 0);
        // On-rate from here: every period must be a full real period, proving
        // the lane stayed locked through the trim and reads the retained window.
        let block = tone(PERIOD as usize);
        for i in 0..200 {
            r.push_input(&block);
            assert_eq!(
                r.render_period(&mut out),
                PERIOD as usize,
                "post-trim render {i} must stay locked (no silence)"
            );
        }
        assert_eq!(
            r.unlock_count.load(Ordering::Relaxed),
            0,
            "no unlock across the trim + continued playback"
        );
        assert_eq!(
            r.lock_count.load(Ordering::Relaxed),
            1,
            "locked exactly once"
        );
    }

    /// `trim_ring` is a no-op when the lane is already at/below its held target
    /// (an on-rate lane the DLL is holding) — nothing to drop, no state change.
    #[test]
    fn trim_ring_is_noop_at_or_below_target() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        // Lock and run on-rate so the fill holds near the target.
        r.push_input(&tone(deep_prefill()));
        let block = tone(PERIOD as usize);
        for _ in 0..500 {
            r.push_input(&block);
            r.render_period(&mut out);
        }
        assert!(r.locked);
        let fill_before = r.ring.write_frame() as f64 - r.next_input_frame;
        let held = r.hold_fill_frames() as f64;
        // An on-rate lane holds at/near target, and the DLL may sit a hair
        // above it, so assert the strict boundary rather than "no drop".
        if fill_before <= held {
            let cursor_before = r.next_input_frame;
            assert_eq!(r.trim_ring(), 0, "at/below target must not drop");
            assert_eq!(
                r.next_input_frame, cursor_before,
                "no-op trim must not move the cursor"
            );
        }
        // Regardless, lock is preserved.
        assert!(r.locked);
        assert_eq!(r.unlock_count.load(Ordering::Relaxed), 0);
    }

    /// An UNLOCKED lane (priming / underfilled) has no standing fill to trim —
    /// `trim_ring` returns 0 and touches nothing, so it can never perturb
    /// acquisition.
    #[test]
    fn trim_ring_noop_while_unlocked() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        // Below the prefill threshold: still priming (unlocked).
        r.push_input(&tone(TARGET / 2));
        assert_eq!(r.render_period(&mut out), 0);
        assert!(!r.locked);
        let cursor_before = r.next_input_frame;
        let fill_before = r.ring.fill_frames();
        assert_eq!(r.trim_ring(), 0, "unlocked lane has nothing to trim");
        assert_eq!(r.next_input_frame, cursor_before, "cursor untouched");
        assert_eq!(
            r.ring.fill_frames(),
            fill_before,
            "buffered input untouched"
        );
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn reset_reprimes_cleanly() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        r.reset();
        assert_eq!(r.render_period(&mut out), PERIOD as usize, "de-click tail");
        assert_eq!(
            r.render_period(&mut out),
            0,
            "then silent until re-prefilled"
        );
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(
            r.render_period(&mut out),
            PERIOD as usize,
            "re-locks after reset"
        );
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn acquisition_underfill_retains_buffered_input_before_reprime() {
        let mut r = build();
        let obs = r.observability();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);

        // Starving inside the acquisition window must NOT clear the buffered
        // input: keeping it lets a real hardware burst continue priming instead
        // of throwing away progress and lock/unlock cycling forever.
        for _ in 0..20 {
            if !r.locked {
                break;
            }
            r.render_period(&mut out);
        }

        assert!(!r.locked, "starved acquisition must unlock");
        assert_eq!(r.unlock_count.load(Ordering::Relaxed), 1);
        assert!(
            r.ring.fill_frames() > 0,
            "early acquisition underfill must retain buffered input"
        );
        assert!(
            obs.fill_frames.load(Ordering::Relaxed) > 0,
            "published fill keeps showing retained acquisition input"
        );
    }

    #[test]
    fn underfill_unlock_drops_stale_tail_before_reprime() {
        let mut r = build();
        let obs = r.observability();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);

        // Past the acquisition grace window, underfill is a hard discontinuity
        // boundary: stale pre-pause samples must not survive into the next
        // acquisition.
        let block = tone(PERIOD as usize);
        for _ in 0..r.acquisition_grace_periods {
            r.push_input(&block);
            assert_eq!(r.render_period(&mut out), PERIOD as usize);
        }
        for _ in 0..20 {
            if !r.locked {
                break;
            }
            r.render_period(&mut out);
        }
        assert!(!r.locked, "starved lane must unlock");
        assert_eq!(r.unlock_count.load(Ordering::Relaxed), 1);
        assert_eq!(r.ring.fill_frames(), 0, "underfill clears stale audio");
        assert_eq!(
            obs.fill_frames.load(Ordering::Relaxed),
            0,
            "published fill resets with the cleared ring"
        );

        // A partial refill must not lock: the lane primes from fresh input only.
        r.push_input(&tone(deep_prefill() - 1));
        assert_eq!(r.render_period(&mut out), 0);
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn render_period_emits_exactly_one_period_of_samples() {
        let mut r = build();
        let mut out = vec![123i16; PERIOD as usize * 2];
        // Silence path still fills the whole buffer (no stale tail).
        r.render_period(&mut out);
        assert!(
            out.iter().all(|&s| s == 0),
            "silence fills the whole buffer"
        );
    }

    /// The resampler primes the ring to `TARGET + cushion` (the deep prefill)
    /// BEFORE it produces any real output: a ring that has only reached
    /// `TARGET + radius` must still be priming, silent.
    #[test]
    fn primes_to_target_plus_cushion_before_first_output() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];

        // Past the no-cushion prefill but below the deep prefill: must still be
        // priming (no lock, silence, 0 real frames).
        let old_threshold = TARGET + RADIUS_FRAMES as usize + 1; // pre-cushion lock point
        assert!(old_threshold < deep_prefill());
        r.push_input(&tone(old_threshold));
        assert_eq!(
            r.render_period(&mut out),
            0,
            "must still prime below the cushion threshold"
        );
        assert!(!r.locked, "no lock until the deep prefill seats");
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 0);

        r.push_input(&tone(CUSHION + PERIOD as usize));
        assert_eq!(r.render_period(&mut out), PERIOD as usize, "locks now");
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn first_locked_period_is_ramped_from_silence() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone_at(0, deep_prefill() + PERIOD as usize));

        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 1);
        let first_frame_peak = out[..2]
            .iter()
            .map(|&sample| i32::from(sample).abs())
            .max()
            .unwrap();
        let mid_period_peak = out[(PERIOD as usize)..(PERIOD as usize + 2)]
            .iter()
            .map(|&sample| i32::from(sample).abs())
            .max()
            .unwrap();
        assert!(
            first_frame_peak <= 64,
            "first frame after silence must be de-click ramped, got {first_frame_peak}"
        );
        assert!(
            mid_period_peak > first_frame_peak * 16,
            "startup ramp should rise within the first real period"
        );
        assert_eq!(r.startup_ramp_frames_remaining, 0);
    }

    /// The mirror of `first_locked_period_is_ramped_from_silence`. A session
    /// that ends mid-waveform must GLIDE the last emitted frame to zero over
    /// one period. Without the tail the first silent sample after real audio is
    /// a full-amplitude step — the click a household hears when a host stops
    /// streaming.
    #[test]
    fn session_end_glides_the_last_frame_to_zero() {
        let mut r = build();
        let period = PERIOD as usize;
        let mut out = vec![0i16; period * 2];

        r.push_input(&tone_at(0, deep_prefill() + period));
        assert_eq!(r.render_period(&mut out), period);
        let last_peak = out[(period - 1) * 2..period * 2]
            .iter()
            .map(|&s| i32::from(s).abs())
            .max()
            .unwrap();
        assert!(
            last_peak > 500,
            "fixture must end on real audio, got {last_peak}"
        );

        // The session ends. This period is the tail, not a hard cut.
        r.reset();
        assert_eq!(
            r.render_period(&mut out),
            period,
            "the tail is real audio and must be reported so the mixer sums it"
        );

        let magnitudes: Vec<i32> = (0..period)
            .map(|f| {
                out[f * 2..f * 2 + 2]
                    .iter()
                    .map(|&s| i32::from(s).abs())
                    .max()
                    .unwrap()
            })
            .collect();
        assert!(
            magnitudes[0] > 0,
            "tail must start from the last emitted frame, not from zero"
        );
        assert!(
            magnitudes[0] <= last_peak,
            "tail may only attenuate: {} > {last_peak}",
            magnitudes[0]
        );
        assert_eq!(
            magnitudes[period - 1],
            0,
            "tail must land exactly on zero so the next period adds no step"
        );
        for pair in magnitudes.windows(2) {
            assert!(
                pair[1] <= pair[0],
                "tail must decay monotonically: {pair:?}"
            );
        }

        r.render_period(&mut out);
        assert!(
            out.iter().all(|&s| s == 0),
            "every period after the tail is true digital silence"
        );
    }

    /// A fresh lock supersedes a pending tail: the startup ramp owns the return
    /// to audio, so a stale tail must never play underneath it.
    #[test]
    fn a_fresh_lock_discards_a_pending_shutdown_tail() {
        let mut r = build();
        let period = PERIOD as usize;
        let mut out = vec![0i16; period * 2];

        r.push_input(&tone_at(0, deep_prefill() + period));
        assert_eq!(r.render_period(&mut out), period);
        r.reset();
        assert!(
            r.shutdown_ramp_frames_remaining > 0,
            "ending a session with real audio arms the tail"
        );

        // Re-feed and re-lock before the tail ever renders.
        r.push_input(&tone_at(0, deep_prefill() + period));
        assert_eq!(r.render_period(&mut out), period, "locks again");
        assert_eq!(
            r.shutdown_ramp_frames_remaining, 0,
            "the pending tail is discarded by the new lock"
        );
        assert!(
            r.last_frame.iter().any(|&s| s != 0),
            "a locked lane remembers the frame it just emitted"
        );
    }

    /// `try_lock` clears the remembered FRAME, not just the tail counter.
    /// `plan_period` can lock and then `unlock_for_underfill` inside ONE call —
    /// both post-lock gates return Silence — arming a tail before any frame is
    /// emitted. Were the previous session's frame still remembered, that tail
    /// would decay stale audio into a session that never played.
    #[test]
    fn a_fresh_lock_forgets_the_previous_sessions_frame() {
        let mut r = build();
        let period = PERIOD as usize;
        let mut out = vec![0i16; period * 2];

        r.push_input(&tone_at(0, deep_prefill() + period));
        assert_eq!(r.render_period(&mut out), period);
        assert!(
            r.last_frame.iter().any(|&s| s != 0),
            "session one must leave a frame that could go stale"
        );

        r.reset();
        r.push_input(&tone_at(0, deep_prefill() + period));
        r.try_lock();
        assert!(
            r.last_frame.iter().all(|&s| s == 0),
            "a fresh lock must forget the previous session's frame"
        );

        // So an unlock before this session emits anything arms no tail.
        r.arm_shutdown_ramp();
        assert_eq!(
            r.shutdown_ramp_frames_remaining, 0,
            "nothing emitted this session, so nothing to decay"
        );
    }

    /// A lane that never emitted audio must not arm a tail — there is nothing
    /// to decay from, and a tail of zeros would only cost a period of work.
    #[test]
    fn a_silent_lane_arms_no_tail() {
        let mut r = build();
        r.reset();
        assert_eq!(r.shutdown_ramp_frames_remaining, 0);
    }

    /// A cold start (EMPTY ring) fed STEADY on-rate input emits ZERO silence
    /// after the initial prime, with no lock→silence→relock thrash: with the
    /// held cushion the lane locks once and holds.
    #[test]
    fn coldstart_steady_input_emits_zero_silence_after_prime() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        let period = PERIOD as usize;

        // Each iteration pushes one on-rate period THEN renders. Only silence
        // AFTER lock counts; the prime's leading silence is expected.
        let mut phase = 0usize;
        let mut locked_at: Option<usize> = None;
        for i in 0..3000usize {
            r.push_input(&tone_at(phase, period));
            phase += period;
            let n = r.render_period(&mut out);
            if r.locked && locked_at.is_none() {
                locked_at = Some(i);
            }
            if let Some(lock_i) = locked_at {
                if i > lock_i {
                    assert_eq!(
                        n, period,
                        "post-lock render {i} fell back to silence (warm-up thrash)"
                    );
                }
            }
        }
        assert!(locked_at.is_some(), "must lock on a steady producer");
        assert_eq!(
            r.lock_count.load(Ordering::Relaxed),
            1,
            "steady cold-start must lock exactly once"
        );
        assert_eq!(
            r.unlock_count.load(Ordering::Relaxed),
            0,
            "steady cold-start must never unlock (no silence thrash)"
        );
    }

    #[test]
    fn coldstart_bursty_input_locks_once_and_ramps_first_audio() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        let period = PERIOD as usize;
        let startup_bursts = [0, period * 2, 0, period, period, 0, period * 2, period];

        let mut phase = 0usize;
        let mut locked_at: Option<usize> = None;
        let mut first_locked_period = Vec::new();
        for i in 0..3000usize {
            let frames = startup_bursts.get(i).copied().unwrap_or(period);
            if frames > 0 {
                r.push_input(&tone_at(phase, frames));
                phase += frames;
            }
            let n = r.render_period(&mut out);
            if r.locked && locked_at.is_none() {
                locked_at = Some(i);
                first_locked_period = out.clone();
                assert_eq!(n, period, "first locked bursty render emits audio");
            }
            if let Some(lock_i) = locked_at {
                if i > lock_i {
                    assert_eq!(
                        n, period,
                        "post-lock bursty render {i} fell back to silence"
                    );
                }
            }
        }

        assert!(locked_at.is_some(), "bursty cold-start must lock");
        assert_eq!(
            r.lock_count.load(Ordering::Relaxed),
            1,
            "bursty cold-start must lock exactly once"
        );
        assert_eq!(
            r.unlock_count.load(Ordering::Relaxed),
            0,
            "bursty cold-start must never unlock"
        );
        assert_eq!(
            r.overrun_frames.load(Ordering::Relaxed),
            0,
            "bursty cold-start fixture must not hide drops in the resampler ring"
        );

        let first_frame_peak = first_locked_period[..2]
            .iter()
            .map(|&sample| i32::from(sample).abs())
            .max()
            .unwrap();
        assert!(
            first_frame_peak <= 64,
            "first bursty audio frame must be ramped from silence, got {first_frame_peak}"
        );
    }

    #[test]
    fn host_probe_distinguishes_compliance_after_a_stalled_start() {
        for (prefill, compliant, short_periods) in [
            (1024, true, 4),
            (1500, false, 4),
            (2560, true, 4),
            (2560, true, 0),
        ] {
            let params = DecayParams {
                enabled: true,
                floor_frames: 576,
                stability_ms: 2000,
                cascade_guard_ppm: 400.0,
            };
            let mut r =
                LaneResampler::new(2, PERIOD, RATE, TARGET, 2048, MAX_PPM, RING, params).unwrap();
            r.latency_context(1, false);
            let gauges = r.observability();
            let mut out = vec![0i16; PERIOD as usize * 2];
            r.push_input(&tone(prefill + RADIUS_FRAMES as usize + 1));
            for _ in 0..(RATE / PERIOD) {
                r.render_period(&mut out);
                if r.locked {
                    break;
                }
            }
            let mut clock = HostClock::new(HostClockConfig {
                enabled: true,
                probe_ppm: 300.0,
                obs_mode: ObsMode::Correction,
                log_prefix: "fanin",
            });
            clock.startup_neutralize();
            let mut pitch = 0.0;
            let mut fractional = 0.0_f64;
            let mut first_low = None;
            let mut resumed_at_low = false;
            for period in 1..=(RATE * 160 / PERIOD) {
                let seconds = period * PERIOD / RATE;
                let paused = (90..100).contains(&seconds);
                r.latency_context(
                    1,
                    !matches!(clock.ladder(), Ladder::L0Locked | Ladder::Probing),
                );
                if period == RATE * 92 / PERIOD {
                    r.reset();
                }
                let host_ppm = 50.0 + if compliant { pitch } else { 0.0 };
                fractional += PERIOD as f64 * (1.0 + host_ppm / 1e6);
                let frames = fractional.floor() as usize;
                fractional -= frames as f64;
                let frames = frames - if period <= short_periods { 128 } else { 0 };
                if !paused {
                    r.push_input(&tone(frames));
                }
                r.render_period(&mut out);
                r.tick_decay(
                    clock.ladder() == Ladder::L0Locked,
                    clock.commanded_ppm().abs(),
                );
                if compliant && r.locked && r.hold_fill_frames() == 576 {
                    first_low.get_or_insert(seconds);
                    if seconds == 100 {
                        resumed_at_low = true;
                    }
                }
                let elapsed_frames = period as u64 * PERIOD as u64;
                if elapsed_frames / RATE as u64 == (elapsed_frames - PERIOD as u64) / RATE as u64 {
                    continue;
                }
                let obs = Obs {
                    playing: r.locked,
                    host_connected: true,
                    preempted: false,
                    steady: r.locked && !gauges.decay_refilling.load(Ordering::Relaxed),
                    fill_frames: gauges.fill_frames.load(Ordering::Relaxed) as f64 + 2560.0
                        - gauges.held_target_frames.load(Ordering::Relaxed) as f64,
                    capture_frames: gauges.input_frames.load(Ordering::Relaxed),
                    playback_frames: gauges.output_frames.load(Ordering::Relaxed),
                    correction_ppm: (gauges.ratio_milli_ppm.load(Ordering::Relaxed) as i64) as f64
                        / 1000.0,
                };
                for Action::WritePitch { ppm, .. } in
                    clock.tick(obs, elapsed_frames * 1000 / RATE as u64)
                {
                    pitch = ppm.round();
                }
            }
            assert_eq!(
                clock.probe_result(),
                if compliant {
                    ProbeResult::Pass
                } else {
                    ProbeResult::Fail
                }
            );
            assert_eq!(
                clock.ladder(),
                if compliant {
                    Ladder::L0Locked
                } else {
                    Ladder::L2Fallback
                }
            );
            assert_eq!(r.unlock_count.load(Ordering::Relaxed), 1);
            if compliant {
                assert!(
                    first_low.unwrap() < if short_periods == 0 { 55 } else { 90 },
                    "prefill={prefill} first_low={first_low:?}"
                );
                assert!(resumed_at_low);
                assert_eq!(r.decay.resumes(), 1);
            }
            assert_eq!(r.hold_fill_frames() < 2560, compliant);
        }
    }

    /// A burst larger than the ring's headroom (capacity − target) overruns a
    /// tight ring but is fully ABSORBED by a larger one. The LATENCY setpoint
    /// (`target_fill_frames`) is identical in both — only the burst headroom
    /// (`ring_frames`) differs.
    #[test]
    fn larger_ring_absorbs_a_burst_a_tight_ring_overruns() {
        // Just past the construction minimum: no real burst room.
        let tight = TARGET + CUSHION + PERIOD as usize + RADIUS_FRAMES as usize + 1;
        let roomy = 16_384usize;
        // Exceeds the tight ring's headroom in one push, as a big catch-up read
        // after a host stall does.
        let burst = tight + 1024;

        let mut tight_r = LaneResampler::new(
            2,
            PERIOD,
            RATE,
            TARGET,
            CUSHION,
            MAX_PPM,
            tight,
            DecayParams::disabled(),
        )
        .unwrap();
        let mut roomy_r = LaneResampler::new(
            2,
            PERIOD,
            RATE,
            TARGET,
            CUSHION,
            MAX_PPM,
            roomy,
            DecayParams::disabled(),
        )
        .unwrap();
        let mut out = vec![0i16; PERIOD as usize * 2];
        tight_r.push_input(&tone(deep_prefill() + 64));
        roomy_r.push_input(&tone(deep_prefill() + 64));
        tight_r.render_period(&mut out);
        roomy_r.render_period(&mut out);
        assert_eq!(tight_r.target_fill_frames, roomy_r.target_fill_frames);

        tight_r.push_input(&tone(burst));
        roomy_r.push_input(&tone(burst));
        assert!(
            tight_r.overrun_frames.load(Ordering::Relaxed) > 0,
            "a burst past the tight ring's headroom must overrun"
        );
        assert_eq!(
            roomy_r.overrun_frames.load(Ordering::Relaxed),
            0,
            "the larger ring must absorb the same burst with no overrun"
        );
    }

    // ---- post-lock cushion decay (the held-target single source of truth) --

    /// Build a resampler with the DEFAULT-OFF decay ARMED. Floor is `TARGET + 32`
    /// (base target plus a small margin); the ms knobs both clamp to one render
    /// period so the descent runs fast.
    fn build_with_decay() -> LaneResampler {
        let params = DecayParams {
            enabled: true,
            floor_frames: (TARGET + 32) as u64,
            stability_ms: 1, // → 1 period (clamped up)
            cascade_guard_ppm: 400.0,
        };
        LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, RING, params)
            .expect("resampler builds with decay armed")
    }

    /// The `DECAY_SNAP` lever takes the SAME enter edge a real demotion does:
    /// it opens the window, logs it, and re-seats the period counter. The
    /// forced path runs before `tick_decay`, so without its own edge call the
    /// enter arm never fires and the next `state=leave` reports an accumulated
    /// period count.
    #[test]
    fn a_forced_decay_snap_takes_the_enter_edge_and_reseats_the_counter() {
        let mut r = build_with_decay();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        let block = tone(PERIOD as usize);
        // Descend so a snap-back has a real deficit to re-arm on.
        for _ in 0..40 {
            r.push_input(&block);
            r.render_period(&mut out);
            r.tick_decay(true, 0.0);
        }
        assert!(r.hold_fill_frames() < r.ceiling_fill_frames(), "no descent");
        r.refill_window_periods = 999; // stale count from an earlier window

        assert!(r.force_decay_snap_back(), "the lever must open a window");
        assert_eq!(
            r.refill_window_periods, 0,
            "the forced path must take the enter edge, not append to a stale count"
        );
        assert!(r.decay_refilling.load(Ordering::Relaxed), "gauge published");
    }

    #[test]
    fn decay_disabled_holds_target_at_ceiling_forever() {
        let mut r = build();
        let ceiling = (TARGET + CUSHION) as u64;
        assert_eq!(r.hold_fill_frames() as u64, ceiling);
        assert_eq!(r.held_target_frames.load(Ordering::Relaxed), ceiling);
        r.push_input(&tone(deep_prefill() + 64));
        for _ in 0..500 {
            r.push_input(&tone(PERIOD as usize));
            r.render_period(&mut vec![0i16; PERIOD as usize * 2]);
            r.tick_decay(true, 0.0);
            assert_eq!(r.hold_fill_frames() as u64, ceiling);
            assert!(!r.decay_active.load(Ordering::Relaxed));
        }
    }

    #[test]
    fn decay_lowers_held_target_only_while_locked_and_l0() {
        let mut r = build_with_decay();
        let ceiling = (TARGET + CUSHION) as u64;
        let floor = (TARGET + 32) as u64;
        // Before lock: ticking decay never lowers (locked == false).
        for _ in 0..100 {
            r.tick_decay(true, 0.0);
        }
        assert_eq!(r.hold_fill_frames() as u64, ceiling, "unlocked → ceiling");

        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        assert!(r.locked);

        // On-rate with the DLL at l0 and calm: the held target must descend.
        let block = tone(PERIOD as usize);
        for _ in 0..5000 {
            r.push_input(&block);
            r.render_period(&mut out);
            r.tick_decay(true, 0.0);
            if r.hold_fill_frames() as u64 == floor {
                break;
            }
        }
        assert_eq!(
            r.hold_fill_frames() as u64,
            floor,
            "decay must descend to the floor under sustained lock+l0"
        );
        // The published gauge tracks the live held target (single source).
        assert_eq!(r.held_target_frames.load(Ordering::Relaxed), floor);
        assert_eq!(
            r.observability().held_target_frames.load(Ordering::Relaxed),
            floor
        );
        // The static ceiling STATUS field is unchanged (it is the snap-back
        // target, not the live setpoint).
        assert_eq!(r.observability().target_fill_frames, ceiling);
        assert_eq!(r.observability().decay_floor_frames, floor);
    }

    #[test]
    fn decay_frozen_when_dll_not_l0() {
        let mut r = build_with_decay();
        let ceiling = (TARGET + CUSHION) as u64;
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        let block = tone(PERIOD as usize);
        // DLL not at l0: decay must never lower the held target.
        for _ in 0..2000 {
            r.push_input(&block);
            r.render_period(&mut out);
            r.tick_decay(false, 0.0);
        }
        assert_eq!(
            r.hold_fill_frames() as u64,
            ceiling,
            "held target must stay at the ceiling while DLL is not l0"
        );
        assert!(!r.decay_active.load(Ordering::Relaxed));
    }

    #[test]
    fn decay_cascade_guard_pauses_above_threshold() {
        let mut r = build_with_decay();
        let ceiling = (TARGET + CUSHION) as u64;
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        let block = tone(PERIOD as usize);
        // DLL commanding hard (> guard): decay pauses, held stays at ceiling.
        for _ in 0..2000 {
            r.push_input(&block);
            r.render_period(&mut out);
            r.tick_decay(true, 401.0);
        }
        assert_eq!(r.hold_fill_frames() as u64, ceiling);
        assert!(!r.decay_active.load(Ordering::Relaxed));
    }

    #[test]
    fn fast_descent_preserves_audio_and_keeps_clock_correction_separate() {
        let mut r = LaneResampler::new(
            2,
            PERIOD,
            RATE,
            TARGET,
            2048,
            MAX_PPM,
            RING,
            DecayParams {
                enabled: true,
                floor_frames: 576,
                stability_ms: 2000,
                cascade_guard_ppm: 400.0,
            },
        )
        .unwrap();
        let mut phase = r.startup_prefill_frames();
        r.push_input(&tone_at(0, phase));
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.render_period(&mut out);
        let mut previous = [out[out.len() - 2], out[out.len() - 1]];
        for _ in 0..(RATE * 25 / PERIOD) {
            r.push_input(&tone_at(phase, PERIOD as usize));
            phase += PERIOD as usize;
            assert_eq!(r.render_period(&mut out), PERIOD as usize);
            r.tick_decay(true, 0.0);
            for frame in out.chunks_exact(2) {
                for channel in 0..2 {
                    assert!((frame[channel] as i32 - previous[channel] as i32).abs() < 140);
                    previous[channel] = frame[channel];
                }
            }
            assert!(r.controller.ratio_ppm().abs() < 1.0);
            assert!(r.decay.demand_ppm().abs() <= decay::BUFFER_ADJUST_PPM);
        }
        assert_eq!(r.hold_fill_frames(), 576);
        assert_eq!(r.decay.demand_ppm(), 0.0);
        assert_eq!(r.unlock_count.load(Ordering::Relaxed), 0);
        assert_eq!(r.overrun_frames.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn pauses_reuse_the_buffer_but_short_stalls_and_disconnects_do_not() {
        for (idle_periods, disconnect, expected) in
            [(2000, false, 544), (2, false, 768), (2000, true, 768)]
        {
            let mut r = build_with_decay();
            r.latency_context(1, false);
            let mut out = vec![0i16; PERIOD as usize * 2];
            r.push_input(&tone(deep_prefill()));
            r.render_period(&mut out);
            for _ in 0..2000 {
                r.push_input(&tone(PERIOD as usize));
                r.render_period(&mut out);
                r.tick_decay(true, 0.0);
            }
            assert_eq!(r.hold_fill_frames(), 544);
            r.reset();
            for i in 0..idle_periods {
                if i == 1000 {
                    r.reset();
                } // Idle capture-handle reopen.
                r.render_period(&mut out);
                r.tick_decay(false, 0.0);
            }
            if disconnect {
                r.latency_context(2, false);
            }
            assert_eq!(r.hold_fill_frames(), expected);
            r.push_input(&tone(r.startup_prefill_frames()));
            r.render_period(&mut out);
            r.tick_decay(false, 0.0); // New timing probe runs in the background.
            assert_eq!(r.hold_fill_frames(), expected);
            assert_eq!(r.decay.resumes(), u64::from(expected == 544));
            if expected == 544 {
                r.latency_context(1, true);
                for _ in 0..1000 {
                    r.push_input(&tone(PERIOD as usize));
                    r.render_period(&mut out);
                    r.tick_decay(false, 0.0);
                }
                assert_eq!(r.hold_fill_frames(), 768);
                assert_eq!(r.decay.resumes(), 1);
                assert_eq!(r.unlock_count.load(Ordering::Relaxed), 0);
            }
        }
    }

    /// A ratio pinned at its ±max_adjust_ppm authority surfaces on the
    /// published `clamp_count` gauge (#3464).
    #[test]
    fn a_railed_ratio_increments_the_published_clamp_counter() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        let obs = r.observability();
        assert_eq!(
            obs.clamp_count.load(Ordering::Relaxed),
            0,
            "no rail before the overfill"
        );
        // A standing ~4000-frame overfill far exceeds what ±500 ppm can drain
        // (±0.128 frames/period), so the loop integrates past the authority and
        // the output clamp engages period after period.
        r.push_input(&tone(4000));
        let block = tone(PERIOD as usize);
        for _ in 0..2000 {
            r.push_input(&block);
            r.render_period(&mut out);
        }
        assert!(
            obs.clamp_count.load(Ordering::Relaxed) > 0,
            "a sustained overfill must surface on the published clamp counter"
        );
        assert_eq!(
            obs.ratio_milli_ppm.load(Ordering::Relaxed) as i64,
            (MAX_PPM * 1000.0) as i64,
            "the bounded ratio itself sits pinned at the +authority rail"
        );
    }

    /// An ARMED cushion decay FROZEN by `dll_l0=false` (`frozen_reason=not_l0`,
    /// held pinned at the ceiling) must behave BIT-IDENTICALLY to decay
    /// disabled over the SAME delivery trace — the armed-but-frozen path must
    /// not amplify or cause unlock churn, which is a property of the static
    /// held target and the delivery pattern.
    ///
    /// The trace has TWO regimes, both load-bearing for the pin:
    ///
    /// 1. A COALESCING-CHURN window (every 8th period stalls) that DOES produce
    ///    unlocks, so a NotL0-branch mutant touching lock / silence / output
    ///    accounting diverges and the identity stays non-vacuous.
    /// 2. A long CLEAN LOCKED TAIL delivered on time with `dll_l0=false`
    ///    throughout — the lane stays locked, so `stable_periods` accrues past
    ///    the ~1875-period warm-up window and the step interval elapses. This is
    ///    the ONLY regime where the NotL0 freeze does mechanical work: delete
    ///    the freeze and the armed run decays `held` down over the tail. The
    ///    churn window alone cannot catch that, since every unlock resets
    ///    `stable_periods`.
    ///
    /// The comparison folds a running FNV checksum of every rendered `out`
    /// period, so "bit-identical" is a claim about output PCM, not just the five
    /// aggregate counters (both runs are deterministic — no RNG, no clock).
    #[test]
    fn armed_frozen_decay_is_bit_identical_to_disabled_over_the_same_trace() {
        // The churny geometry (base target 256 + one-period cushion = 512 held),
        // NOT the module TARGET (512). Period 256, min_safe 274: the DLL holds
        // the pre-render fill at 512, so a single fully-withheld delivery period
        // drops it to 512 - 256 = 256, below min_safe 274 → underfill-unlock →
        // immediate re-lock next period. The production default held=2560 cannot
        // dip that far on one stall, which is why it is immune.
        const CHURNY_TARGET: usize = 256;
        // Churn only in the first window; the rest of the trace is a clean locked
        // tail long enough (≥ the ~1875-period stability window + a few step
        // intervals) that an unfrozen armed decay would step `held` down.
        const CHURN_PERIODS: usize = 1000;
        const TRACE_PERIODS: usize = 6000;
        fn run(decay_enabled: bool) -> (u64, u64, u64, u64, u64, u64) {
            let params = DecayParams {
                enabled: decay_enabled,
                floor_frames: 306,
                stability_ms: 10_000,
                cascade_guard_ppm: 400.0,
            };
            let mut r = LaneResampler::new(
                2,
                PERIOD,
                RATE,
                CHURNY_TARGET,
                PERIOD as usize,
                MAX_PPM,
                RING,
                params,
            )
            .expect("lane builds");
            let mut out = vec![0i16; PERIOD as usize * 2];
            let period = PERIOD as usize;
            // FNV-1a over every rendered output sample — makes the identity a
            // claim about the emitted PCM, not merely the aggregate counters.
            let mut checksum: u64 = 0xcbf2_9ce4_8422_2325;
            let mut absorb = |out: &[i16]| {
                for s in out {
                    checksum ^= *s as u16 as u64;
                    checksum = checksum.wrapping_mul(0x0000_0100_0000_01b3);
                }
            };
            // Delivery model (the mixer's per-period order + the gadget's
            // coalescing shape): the host produces one period of frames every
            // render period, but delivery to the ring is GATED during a stall
            // window — frames accumulate and flush in one burst when the stall
            // ends (the max_avail≈2×period signature). The render still consumes
            // a period each step, so during a stall the cursor-relative fill
            // drops; a stall long enough to drop the post-render fill below
            // min_safe (274) unlocks, and the immediate re-lock the next period
            // is the churn cycle. Deterministic (no RNG / clock) so both runs
            // replay byte-identically.
            let mut phase = 0usize;
            let mut pending = 0usize; // host-produced but not yet delivered
                                      // Deliver the deep prefill up front so both runs lock exactly once
                                      // before the churn regime starts.
            r.push_input(&tone_at(phase, CHURNY_TARGET + PERIOD as usize + 64));
            phase += CHURNY_TARGET + PERIOD as usize + 64;
            r.render_period(&mut out);
            absorb(&out);
            r.tick_decay(false, 0.0);
            // Regime 1 (i < CHURN_PERIODS): one period per interval delivered ON
            // TIME (fill held tight at the setpoint) except on every 8th period,
            // where delivery is withheld (fill dips one period below the
            // setpoint → below min_safe → unlock) and flushed the next period
            // (immediate re-lock). The 7 on-time periods between keep the fill
            // tight so each stall reliably dips it.
            //
            // Regime 2 (i ≥ CHURN_PERIODS): clean on-time delivery every period.
            // The lane stays LOCKED, so `stable_periods` accrues past the warm-up
            // window — the regime where the NotL0 freeze does its work.
            for i in 0..TRACE_PERIODS {
                pending += period; // host produced one period this interval
                if i % 8 == 7 && i < CHURN_PERIODS {
                    // Stall: withhold this interval's delivery (fill will dip).
                } else {
                    r.push_input(&tone_at(phase, pending));
                    phase += pending;
                    pending = 0;
                }
                r.render_period(&mut out);
                absorb(&out);
                // dll_l0 = false on every tick, so an armed decay must SNAP BACK
                // to the ceiling and never lower.
                r.tick_decay(false, 0.0);
            }
            let o = r.observability();
            (
                o.unlock_count.load(Ordering::Relaxed),
                o.lock_count.load(Ordering::Relaxed),
                o.held_target_frames.load(Ordering::Relaxed),
                o.silence_frames.load(Ordering::Relaxed),
                o.output_frames.load(Ordering::Relaxed),
                checksum,
            )
        }
        let disabled = run(false);
        let armed_frozen = run(true);
        assert_eq!(
            armed_frozen, disabled,
            "ARMED+frozen(not_l0) decay must be bit-identical to disabled \
             (unlocks, locks, held, silence, output, PCM checksum) — any \
             divergence means the NotL0 freeze is NOT mechanically inert (PR \
             #1141 regression). The clean locked tail is what makes deleting the \
             NotL0 snap-back diverge `held`; the churn window keeps it non-vacuous."
        );
        // The trace really did churn (else the identity is vacuous), and the
        // armed run stayed frozen at the ceiling through the whole tail.
        assert!(
            disabled.0 > 0,
            "the coalescing window must produce unlocks, or the identity proves nothing"
        );
        assert_eq!(
            disabled.2,
            (CHURNY_TARGET + PERIOD as usize) as u64,
            "the disabled run's held target must stay at the static ceiling \
             (target 256 + cushion 256 = 512); if this drifts the trace geometry \
             changed and the freeze comparison is no longer meaningful"
        );
    }

    #[test]
    fn decay_snaps_back_to_ceiling_on_reset() {
        let mut r = build_with_decay();
        let ceiling = (TARGET + CUSHION) as u64;
        let floor = (TARGET + 32) as u64;
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        let block = tone(PERIOD as usize);
        // Decay down a bit.
        for _ in 0..5000 {
            r.push_input(&block);
            r.render_period(&mut out);
            r.tick_decay(true, 0.0);
            if r.hold_fill_frames() as u64 == floor {
                break;
            }
        }
        assert!(r.hold_fill_frames() as u64 <= floor + 16);
        // Reset (host pause / idle): the held target must snap back to ceiling
        // IMMEDIATELY so the next lock seats at the full cushion.
        r.reset();
        assert_eq!(
            r.hold_fill_frames() as u64,
            ceiling,
            "reset must snap the held target back to the acquisition ceiling"
        );
        assert_eq!(r.held_target_frames.load(Ordering::Relaxed), ceiling);
    }

    #[test]
    fn decay_relock_after_underfill_seats_at_ceiling() {
        // After decay lowers the held target, an underfill unlock must snap it
        // back so the re-lock's startup prefill targets the FULL cushion, not
        // the shallow decayed depth (which gives relock chatter).
        let mut r = build_with_decay();
        let ceiling = (TARGET + CUSHION) as u64;
        let floor = (TARGET + 32) as u64;
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        let block = tone(PERIOD as usize);
        // Prove stable for the acquisition grace window, then decay down.
        for _ in 0..r.acquisition_grace_periods {
            r.push_input(&block);
            r.render_period(&mut out);
            r.tick_decay(true, 0.0);
        }
        for _ in 0..5000 {
            r.push_input(&block);
            r.render_period(&mut out);
            r.tick_decay(true, 0.0);
            if r.hold_fill_frames() as u64 == floor {
                break;
            }
        }
        assert!(r.hold_fill_frames() as u64 <= floor + 16);
        // Starve → underfill unlock. Held target must be back at ceiling.
        for _ in 0..20 {
            if !r.locked {
                break;
            }
            r.render_period(&mut out);
        }
        assert!(!r.locked, "starved lane must unlock");
        assert_eq!(
            r.hold_fill_frames() as u64,
            ceiling,
            "underfill unlock must snap the held target back to the ceiling"
        );
        // startup_prefill now targets the full ceiling again.
        assert_eq!(
            r.startup_prefill_frames(),
            ceiling as usize + RADIUS_FRAMES as usize + 1
        );
    }

    // ---- the widened DIRECT-lane route (#2223) ----------------------------
    //
    // These cover the route that skips the capture narrowing, and the contrast
    // between the two — the only honest way to state "the low bits survive":
    // the same hi-res input must come out DIFFERENT on the two routes, and the
    // wide one must be the faithful one.

    /// A known 24-bit sample in S24-in-S32 placement — the value the exit-gate
    /// fixture follows from the capture boundary to the summed write. The low
    /// byte (`0x56`) is exactly what the narrow route's `>> 16` discards.
    const HIRES_PATTERN: i32 = 0x1234_5600;
    /// The positive 24-bit rail, same placement.
    const HIRES_POSITIVE_RAIL: i32 = 0x7fff_ff00;
    /// The negative 24-bit rail (`0x800000` as a signed 24-bit value is
    /// −8388608), same placement — exactly `i32::MIN`.
    const HIRES_NEGATIVE_RAIL: i32 = i32::MIN;

    /// A constant interleaved stereo block at spine scale.
    fn wide_dc(value: i32, frames: usize) -> Vec<i32> {
        vec![value; frames * 2]
    }

    /// Drive a lane to lock on a CONSTANT spine-scale input and return a
    /// steady-state rendered period.
    ///
    /// A constant is the right probe for a bit-survival claim: the kernel's
    /// coefficients are normalised to sum to 1, so a DC input interpolates back
    /// to itself and any bit loss is the conversion's, not the interpolator's.
    ///
    /// The producer keeps feeding a period per render, because the lock seats
    /// the cursor at the held target rather than at everything buffered — a
    /// prime-once lane has exactly `hold_fill_frames() / period` renders of
    /// runway and then underfills into silence. The first rendered period is
    /// skipped because the startup de-click ramp scales it.
    fn wide_steady_period(value: i32) -> Vec<i32> {
        let mut r = build();
        let mut out = vec![0i32; PERIOD as usize * 2];
        r.push_input_wide(&wide_dc(value, deep_prefill() + PERIOD as usize));
        for _ in 0..3 {
            r.push_input_wide(&wide_dc(value, PERIOD as usize));
            assert_eq!(r.render_period_wide(&mut out), PERIOD as usize);
        }
        out
    }

    /// The narrow twin of [`wide_steady_period`]: the SAME gadget samples taken
    /// through the capture narrowing first, exactly as a narrow-wire box does.
    fn narrow_steady_period(value: i32) -> Vec<i16> {
        let narrow_dc = |frames: usize| {
            let wide = wide_dc(value, frames);
            let mut narrowed = vec![0i16; wide.len()];
            assert!(jasper_resampler::convert_s32_to_s16(&wide, &mut narrowed));
            narrowed
        };
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&narrow_dc(deep_prefill() + PERIOD as usize));
        for _ in 0..3 {
            r.push_input(&narrow_dc(PERIOD as usize));
            assert_eq!(r.render_period(&mut out), PERIOD as usize);
        }
        out
    }

    /// A known 24-bit pattern injected at the capture boundary reaches the
    /// lane's rendered period with its low bits intact, and the narrow route
    /// provably destroys them: the wide render must equal the injected sample,
    /// and the narrow render re-widened must NOT — it is short by exactly the
    /// low word.
    #[test]
    fn a_hi_res_sample_keeps_its_low_bits_through_the_wide_render() {
        for pattern in [HIRES_PATTERN, HIRES_POSITIVE_RAIL, HIRES_NEGATIVE_RAIL] {
            let wide = wide_steady_period(pattern);
            for (i, &s) in wide.iter().enumerate() {
                assert_eq!(
                    s, pattern,
                    "wide render sample {i} must carry {pattern:#010x} exactly",
                );
            }

            let narrow = narrow_steady_period(pattern);
            let narrow_rewidened = jasper_resampler::widen_i16_to_i32(narrow[0]);
            assert_eq!(
                narrow[0],
                jasper_resampler::s32_high_word_to_s16(pattern),
                "the narrow route must still keep exactly the high word",
            );
            // What the narrow route dropped is exactly the sample's low word —
            // computed from the pattern, so the claim is arithmetic rather than
            // a hand-copied constant.
            let lost = pattern.wrapping_sub(narrow_rewidened);
            let low_word = (pattern as u32 & 0xffff) as i32;
            assert_eq!(
                lost, low_word,
                "the narrow route must drop exactly the low word of {pattern:#010x}",
            );
            if low_word != 0 {
                assert_ne!(
                    wide[0], narrow_rewidened,
                    "the wide route must carry information the narrow one loses"
                );
            } else {
                // `i32::MIN` is the one vector with no low word at all, so both
                // routes agree there. Named rather than skipped: a rail is where
                // a SIGN error would show, and agreeing is the correct answer.
                assert_eq!(pattern, i32::MIN);
                assert_eq!(wide[0], narrow_rewidened);
            }
        }
    }

    /// The wide route's silence, lock, and unlock behaviour is the SAME state
    /// machine as the narrow route's — the two emit tails share `plan_period`,
    /// and this is the assertion that keeps them from drifting apart on a lock
    /// rule.
    #[test]
    fn the_wide_route_primes_locks_and_unlocks_exactly_like_the_narrow_one() {
        let mut wide = build();
        let mut narrow = build();
        let mut wide_out = vec![0i32; PERIOD as usize * 2];
        let mut narrow_out = vec![0i16; PERIOD as usize * 2];

        // Unprimed: both silent, both report 0 real frames, both count silence.
        assert_eq!(wide.render_period_wide(&mut wide_out), 0);
        assert_eq!(narrow.render_period(&mut narrow_out), 0);
        assert!(wide_out.iter().all(|&s| s == 0));
        assert!(narrow_out.iter().all(|&s| s == 0));
        assert_eq!(
            wide.silence_frames.load(Ordering::Relaxed),
            narrow.silence_frames.load(Ordering::Relaxed),
        );
        assert_eq!(wide.is_locked(), narrow.is_locked());

        // Same signal, one route narrowed at ingest: both lock on the same push.
        let frames = deep_prefill() + PERIOD as usize;
        let block = tone(frames);
        let mut widened = vec![0i32; block.len()];
        assert!(jasper_resampler::widen_i16_to_i32_slice(
            &block,
            &mut widened
        ));
        wide.push_input_wide(&widened);
        narrow.push_input(&block);
        assert_eq!(wide.render_period_wide(&mut wide_out), PERIOD as usize);
        assert_eq!(narrow.render_period(&mut narrow_out), PERIOD as usize);
        assert!(wide.is_locked() && narrow.is_locked());
        assert_eq!(
            wide.lock_count.load(Ordering::Relaxed),
            narrow.lock_count.load(Ordering::Relaxed),
        );

        // Starved: both unlock into silence on the same period.
        for _ in 0..8 {
            wide.render_period_wide(&mut wide_out);
            narrow.render_period(&mut narrow_out);
        }
        assert_eq!(wide.is_locked(), narrow.is_locked());
        assert_eq!(
            wide.unlock_count.load(Ordering::Relaxed),
            narrow.unlock_count.load(Ordering::Relaxed),
        );
        // ABSOLUTE anchors, so the equalities above cannot pass vacuously by
        // both routes simply never having done anything: starvation must really
        // have unlocked them, and it must have left them unlocked.
        assert!(
            wide.unlock_count.load(Ordering::Relaxed) > 0,
            "starvation must actually have unlocked the lanes"
        );
        assert!(!wide.is_locked(), "a starved lane must end unlocked");
    }

    /// An S16 signal carried on the WIDE route (widened at ingest rather than at
    /// the wire) renders to exactly what the narrow route renders, left-justified
    /// — the promotion is a scale change, never a content change. This is the
    /// property that makes flipping a box's wire inaudible for a source that
    /// never had more than 16 bits.
    #[test]
    fn a_widened_s16_signal_renders_identically_on_both_routes() {
        let mut wide = build();
        let mut narrow = build();
        let mut wide_out = vec![0i32; PERIOD as usize * 2];
        let mut narrow_out = vec![0i16; PERIOD as usize * 2];
        // One phase-continuous signal, fed to both routes a period at a time so
        // neither starves (see `wide_steady_period` for why priming once is not
        // enough).
        let mut phase = 0usize;
        let mut feed = |wide: &mut LaneResampler, narrow: &mut LaneResampler, frames: usize| {
            let block = tone_at(phase, frames);
            phase += frames;
            let mut widened = vec![0i32; block.len()];
            assert!(jasper_resampler::widen_i16_to_i32_slice(
                &block,
                &mut widened
            ));
            wide.push_input_wide(&widened);
            narrow.push_input(&block);
        };
        feed(&mut wide, &mut narrow, deep_prefill() + PERIOD as usize);
        for _ in 0..3 {
            feed(&mut wide, &mut narrow, PERIOD as usize);
            assert_eq!(wide.render_period_wide(&mut wide_out), PERIOD as usize);
            assert_eq!(narrow.render_period(&mut narrow_out), PERIOD as usize);
        }
        for (i, (&w, &n)) in wide_out.iter().zip(narrow_out.iter()).enumerate() {
            // The wide render rounds the accumulator at the i32 rails; the
            // narrow one divides by 2^16 and rounds at the i16 rails. So they
            // can differ by at most the HALF-step that second round discards —
            // 2^15 at spine scale, plus one for the tie direction. A 2^16 bound
            // would tolerate a whole i16 LSB, i.e. an actual off-by-one in the
            // promotion, which is exactly what this is here to exclude.
            let delta = (w as i64) - (jasper_resampler::widen_i16_to_i32(n) as i64);
            assert!(
                delta.abs() <= (1 << 15) + 1,
                "sample {i}: wide {w} vs widened-narrow {} differs by {delta}",
                jasper_resampler::widen_i16_to_i32(n),
            );
        }
    }
}
