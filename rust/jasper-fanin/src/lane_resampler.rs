// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Per-input adaptive resampler for the clock-crossing (USB) fan-in lane.
//!
//! ## What problem this solves
//!
//! The fan-in work loop is paced by the blocking OUTPUT write — the local DAC
//! clock. Every renderer lane whose producer is clocked off the *same* DAC
//! (AirPlay / Spotify / Bluetooth / TTS — all networked, DAC-disciplined)
//! keeps its capture ring at ~one period forever and needs no rate work. The
//! **USB lane is the exception**: its producer is the host (Mac) clock, free-
//! running relative to our DAC-paced drain, so a small residual rate gap
//! accumulates in its snd-aloop ring. Today that gap is absorbed by
//! [`crate::mixer`]'s bounded **catch-up drain** — a drop-CONTROLLED resync
//! that discards a chunk of audio whenever the lane backs up past a high-water
//! (`CATCHUP_HIGH_WATER_PERIODS`). That high-water is sized to never false-fire
//! on a healthy AirPlay burst, so it inherently lets the USB ring sit anywhere
//! from 1 to ~14 periods — a **5–75 ms latency sawtooth** (see
//! `docs/HANDOFF-usb-low-latency.md`).
//!
//! This module is the drop-FREE alternative: a per-lane windowed-sinc
//! resampler, DLL-steered to the DAC clock, that *reconciles* the host rate to
//! the DAC rate at the lane's input edge. The lane then sits at a small fixed
//! fill (no sawtooth) and the catch-up never fires. Moving reconciliation here,
//! at the fan-in input edge, is also what lets CamillaDSP stay DAC-paced
//! without `rate_adjust` on the clockless USB input — dissolving the underrun
//! class that `rate_adjust` produced on-device.
//!
//! ## How it composes the shared crate
//!
//! It reuses the EXACT primitives `jasper-outputd`'s `content_bridge.rs`
//! composes — [`AudioRing`] + [`SincTable`] + [`RateController`] from the
//! shared [`jasper_resampler`] crate — and runs the same
//! lock → render-period → underfill state machine. The only difference from
//! content_bridge is the *direction* of the buffer it disciplines: content
//! bridge is post-Camilla (one bridge for the whole mix); this is per-INPUT,
//! upstream of the sum. The DLL control law (the `b = sqrt(2)·ω/2` spa_dll
//! second-order loop, the variance-adaptive bandwidth, the `max_resync` hard
//! jump) lives entirely inside [`RateController`]; this module never touches
//! loop math.
//!
//! ## Capture-follower sign (inherited, unchanged)
//!
//! The error fed to the controller is `fill - target`. A too-full ring
//! (`error > 0`) settles to `ratio > 1`, which advances the fractional read
//! cursor by more than one input frame per output frame — consuming the host's
//! faster-arriving input FASTER and draining the ring back to target. This is
//! the same convention content_bridge proves and the crate documents; we feed
//! the raw `fill - target` and the controller negates internally.
//!
//! ## Real-time safety
//!
//! - No allocation on the hot path. The ring is sized at construction; the
//!   per-period output is written into a caller-owned slice; no `Vec` is
//!   produced inside `render_period`.
//! - No blocking. The mixer feeds already-read frames via [`push_input`]; this
//!   module never does ALSA I/O.
//! - No clock reads. Logging is count-gated like the rest of the daemon.
//! - Bounded work: `render_period` emits exactly one period, interpolating a
//!   fixed `period_frames × channels` samples.
//!
//! ## Default OFF — inert when disabled
//!
//! The mixer only constructs a [`LaneResampler`] for the configured
//! clock-crossing lane AND only when the feature is explicitly enabled
//! (`JASPER_FANIN_INPUT_RESAMPLER=enabled`). When disabled, no
//! [`LaneResampler`] exists, the per-lane read path is byte-for-byte today's
//! strict one-period read + catch-up drain, and this module is dead weight the
//! optimizer can see is never reached. The catch-up drain is intentionally
//! KEPT as the fallback; deleting it is a later, validation-gated step.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use jasper_resampler::{clamp_i16, AudioRing, RateController, SincTable, RADIUS_FRAMES};

pub use decay::{CushionDecay, DecayFrozenReason, DecayParams, DecaySignals};

/// Observability counters for one armed lane resampler, cloned into the STATUS
/// snapshot. All `0` while the resampler is disabled (no instance exists).
#[derive(Clone)]
pub struct LaneResamplerObservability {
    /// Whether a resampler is armed on this lane (1) or not (0). A plain bool
    /// would do, but the atomic keeps the STATUS read lock-free and uniform
    /// with the rest of the per-input counters.
    pub armed: bool,
    /// Live lock state. True only after the lane has acquired enough input to
    /// render real DAC-paced audio; false while priming, after reset, or after
    /// an underfill unlock.
    pub locked: Arc<AtomicBool>,
    /// Cumulative input frames pushed into the resampler.
    pub input_frames: Arc<AtomicU64>,
    /// Cumulative output frames emitted (period-aligned).
    pub output_frames: Arc<AtomicU64>,
    /// Cumulative silence frames emitted while unlocked/underfilled.
    pub silence_frames: Arc<AtomicU64>,
    /// Cumulative frames dropped by ring overrun (producer outran the ring —
    /// should stay 0 in steady state; growth means the ring is undersized or
    /// the host is wildly off-rate).
    pub overrun_frames: Arc<AtomicU64>,
    /// Last bounded resampler ratio, in ppm × 1000 (so it fits an integer
    /// atomic with milli-ppm resolution). Signed value stored as i64 bits in a
    /// u64; the STATUS layer reinterprets. Kept coarse on purpose.
    pub ratio_milli_ppm: Arc<AtomicU64>,
    /// Lock transitions (acquire) — a growing value past 1 means the lane keeps
    /// re-locking (host discontinuities / under-provisioned ring).
    pub lock_count: Arc<AtomicU64>,
    /// Underfill unlocks — the drop-free analogue of a catch-up event; a
    /// growing value means the resampler is starving (target too low or a host
    /// stall) and falling back to silence rather than reading past the buffer.
    pub unlock_count: Arc<AtomicU64>,
    /// Current ring fill, in frames, as of the last render period — the live
    /// "how full is the input buffer" gauge. Held near `target_fill_frames` by
    /// the DLL when locked; this is the operator's "the resampler is tracking"
    /// proof (a steady value near target = engaged & holding, a value drifting
    /// away from target = losing lock). Published every `render_period`.
    pub fill_frames: Arc<AtomicU64>,
    /// The acquisition CEILING the controller holds the ring at after lock
    /// (base target plus the full warm-up cushion). Static for the lane's life —
    /// the value the held target snaps back to on any discontinuity. Paired with
    /// `fill_frames` so STATUS shows current vs. ceiling without the reader
    /// having to know the config.
    pub target_fill_frames: u64,
    /// The LIVE held target the controller is disciplining the ring toward right
    /// now — equal to `target_fill_frames` (the ceiling) unless the DEFAULT-OFF
    /// post-lock cushion decay has lowered it. Republished every render period.
    /// This is the ONE authoritative held-target value: the host-clock DLL reads
    /// the same atomic as its setpoint (single source of truth), so the two
    /// controllers can never disagree about where the fill should sit.
    pub held_target_frames: Arc<AtomicU64>,
    /// Live cushion-decay state (all `0`/inert while the decay feature is off).
    /// `active` = actively decaying; `floor` = the configured decay floor;
    /// `frozen_reason` = the stringly-typed reason decay is currently paused
    /// (`""` while actively decaying).
    pub decay_active: Arc<AtomicBool>,
    pub decay_floor_frames: u64,
    pub decay_frozen_reason: Arc<AtomicU64>,
    /// DEFAULT-OFF host-compliance persistence observability (STATUS
    /// `resampler.compliance`). `Some` only when the feature is armed on this
    /// lane; `None` (no block) otherwise — byte-identical to today.
    pub compliance: Option<crate::host_compliance::HostComplianceObservability>,
}

/// A per-input windowed-sinc resampler that turns a free-running (host-clocked)
/// lane into a DAC-paced one. Owns its own ring, sinc table, rate controller,
/// and fractional read cursor — a per-lane sibling of `content_bridge`'s
/// `ContentBridge`, composing the same shared primitives.
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
    /// Extra frames added to the DLL hold target for the armed lane. This is
    /// the WARM-UP cushion: the headroom that keeps the first jittery seconds
    /// of host arrival from dipping the cursor-relative fill below
    /// `minimum_safe_fill` and thrashing lock→silence→relock.
    ///
    /// Hardware validation of the earlier "seat deep, then drain back to
    /// target" version showed a cold-start limit cycle on the real USB burst
    /// feed: the intentional over-consumption fought the startup burst shape.
    /// The cushion is now held, not drained.
    warmup_cushion_frames: usize,
    /// Output ppm safety bound (also drives the minimum-safe-fill margin).
    max_adjust_ppm: f64,
    /// Fractional read cursor in the ring's monotonic frame space.
    next_input_frame: f64,
    locked: bool,
    /// Consecutive render periods spent priming (unlocked, waiting for the
    /// deep prefill). Bounds the prime: once it exceeds `max_prime_periods`
    /// with *some* input buffered, `try_lock` falls through and seats at
    /// whatever safe depth is available, so a slow/sparse-but-real producer
    /// can never wedge in silence forever waiting for the full cushion.
    prime_periods: u32,
    /// Max consecutive priming periods before the fall-through lock. Bounded so
    /// the deep prefill never deadlocks on input that arrives just under the
    /// cushion threshold. `0` disables the fall-through (prime strictly to the
    /// full cushion) — used by tests that want the deterministic deep-prime.
    max_prime_periods: u32,
    /// Frames left in the startup de-click ramp. Set to one render period on
    /// every lock, then counted down to zero while rendering real audio.
    startup_ramp_frames_remaining: usize,
    /// Consecutive real render periods since the most recent lock. Early
    /// underfills during acquisition retain buffered input so the lane can keep
    /// priming; after this reaches `max_prime_periods`, underfill is treated as
    /// a real discontinuity and clears stale buffered audio.
    real_periods_since_lock: u32,
    // Lifetime counters mirrored into observability atomics on update.
    input_frames: Arc<AtomicU64>,
    output_frames: Arc<AtomicU64>,
    silence_frames: Arc<AtomicU64>,
    overrun_frames: Arc<AtomicU64>,
    ratio_milli_ppm: Arc<AtomicU64>,
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
    /// DEFAULT-OFF host-compliance persistence observability (STATUS
    /// `resampler.compliance`). `Some` only when the mixer armed the feature on
    /// this lane and injected the shared handles via
    /// [`set_compliance_observability`](Self::set_compliance_observability); the
    /// mixer is the sole writer, this only carries the handles for STATUS.
    compliance: Option<crate::host_compliance::HostComplianceObservability>,
}

impl LaneResampler {
    /// Construct a resampler for `channels` interleaved channels at
    /// `period_frames` per render, holding the ring at
    /// `target_fill_frames + warmup_cushion_frames` and bounding pitch warp to
    /// `±max_adjust_ppm`.
    ///
    /// `warmup_cushion_frames` is added to `target_fill_frames` and held as the
    /// DLL setpoint. The earlier c57 warm-up path seated this deep but then let
    /// the DLL drain the cushion away; on hardware, that over-consumed the
    /// bursty USB feed during acquisition and caused lock/unlock cycling. The
    /// current `usb_low_latency_48k` route keeps a conservative six-period
    /// held cushion (`512 + 1536 = 2048` frames total); hardware soak/cold-start
    /// validation must pass before any lower route default ships.
    ///
    /// `ring_frames` is the input buffer depth: it MUST exceed
    /// `target_fill_frames` plus the warm-up cushion plus one render period plus
    /// the kernel radius, or the deep prefill could not seat. Returns an error
    /// string (rather than a typed error) so the caller can log-and-fall-back
    /// without a new error enum — a construction failure here must degrade to
    /// "no resampler", never crash the daemon.
    ///
    /// The argument list is one over clippy's default seven: all but
    /// `decay_params` are flat primitive lane geometry that reads clearly at the
    /// single call site (`build_lane_resampler`), and bundling them into a struct
    /// would be churn without added clarity — so the lint is allowed here rather
    /// than obscuring a well-understood constructor.
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
        // radius, or the deep prefill could never accumulate. The decay only ever
        // LOWERS the held target below this ceiling, so the ring stays sized for
        // the acquisition depth (the ceiling) regardless of decay.
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
        // Bound the deep prime so a slow-but-real producer can never wedge in
        // silence: after ~1 s of priming with some input buffered, fall through
        // and seat at whatever safe depth exists. 1 s of periods at this rate.
        let max_prime_periods = (sample_rate / period_frames.max(1) as u32).max(1);
        // The acquisition CEILING the decay lowers FROM and snaps back TO.
        let ceiling = (target_fill_frames + warmup_cushion_frames) as u64;
        let decay = decay_params.build(ceiling, period_frames as u32, sample_rate, max_adjust_ppm);
        Ok(Self {
            channels,
            period_frames,
            ring,
            sinc_table: SincTable::new(),
            // The input lane's fill can legitimately move by more than one
            // render period during USB burst acquisition. Treat that as a
            // buffer-fill excursion to slew through, not a discontinuity: hard
            // discontinuities already arrive here as PCM xruns / explicit
            // resets. Leaving the shared default max_resync enabled made a
            // deeper held cushion repeatedly reset the DLL at unity and let
            // the ring drift away from target.
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
            prime_periods: 0,
            max_prime_periods,
            startup_ramp_frames_remaining: 0,
            real_periods_since_lock: 0,
            input_frames: Arc::new(AtomicU64::new(0)),
            output_frames: Arc::new(AtomicU64::new(0)),
            silence_frames: Arc::new(AtomicU64::new(0)),
            overrun_frames: Arc::new(AtomicU64::new(0)),
            ratio_milli_ppm: Arc::new(AtomicU64::new(0)),
            lock_count: Arc::new(AtomicU64::new(0)),
            unlock_count: Arc::new(AtomicU64::new(0)),
            fill_frames: Arc::new(AtomicU64::new(0)),
            locked_state: Arc::new(AtomicBool::new(false)),
            decay,
            // Seed the live held-target gauge at the ceiling (== hold_fill_frames
            // before any decay). Republished on every decay tick.
            held_target_frames: Arc::new(AtomicU64::new(ceiling)),
            decay_active: Arc::new(AtomicBool::new(false)),
            decay_frozen_reason: Arc::new(AtomicU64::new(DecayFrozenReason::NONE_CODE)),
            compliance: None,
        })
    }

    /// Inject the shared host-compliance observability handles so STATUS can
    /// render `resampler.compliance`. Called once by the mixer after it seeds the
    /// persistence state. Idempotent-ish: a later call replaces the handles.
    pub fn set_compliance_observability(
        &mut self,
        obs: crate::host_compliance::HostComplianceObservability,
    ) {
        self.compliance = Some(obs);
    }

    /// The current published ring fill in frames — the same value STATUS shows,
    /// read via a single relaxed atomic load (no Arc clones). Hot-path safe: the
    /// USB DIRECT read calls this every period for the tap's diagnostic
    /// `ring_fill_frames` field, so it must not allocate like `observability()`.
    pub fn fill_frames_gauge(&self) -> u64 {
        self.fill_frames.load(Ordering::Relaxed)
    }

    /// Clone the observability handles for the STATUS snapshot.
    pub fn observability(&self) -> LaneResamplerObservability {
        LaneResamplerObservability {
            armed: true,
            locked: Arc::clone(&self.locked_state),
            input_frames: Arc::clone(&self.input_frames),
            output_frames: Arc::clone(&self.output_frames),
            silence_frames: Arc::clone(&self.silence_frames),
            overrun_frames: Arc::clone(&self.overrun_frames),
            ratio_milli_ppm: Arc::clone(&self.ratio_milli_ppm),
            lock_count: Arc::clone(&self.lock_count),
            unlock_count: Arc::clone(&self.unlock_count),
            fill_frames: Arc::clone(&self.fill_frames),
            // The static acquisition ceiling (target + full cushion) — the value
            // the held target snaps back to. STATUS shows this as
            // `target_fill_frames` (unchanged shape); the LIVE held target is the
            // separate `held_target_frames` gauge below.
            target_fill_frames: self.ceiling_fill_frames() as u64,
            held_target_frames: Arc::clone(&self.held_target_frames),
            decay_active: Arc::clone(&self.decay_active),
            decay_floor_frames: self.decay.floor(),
            decay_frozen_reason: Arc::clone(&self.decay_frozen_reason),
            compliance: self.compliance.as_ref().map(|c| c.clone_handles()),
        }
    }

    /// Push `samples` (interleaved `i16`, this lane's just-read frames) into the
    /// input ring. A producer that outruns the ring drops oldest-first and
    /// counts the overrun — the resampler keeps running on the freshest audio.
    pub fn push_input(&mut self, samples: &[i16]) {
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
    /// The state machine mirrors content_bridge's `render_period`: wait for a
    /// startup prefill before locking; once locked, drive the ratio from the
    /// fill error and advance the fractional cursor; on underfill, unlock and
    /// emit silence rather than reading past the buffered input.
    pub fn render_period(&mut self, out: &mut [i16]) -> usize {
        debug_assert_eq!(out.len(), self.period_frames * self.channels);

        if !self.locked {
            // While priming, the buffered-input depth IS the fill the operator
            // watches climb toward the startup prefill — publish it so STATUS
            // shows the lane filling before it locks. Count priming periods so
            // the deep prefill falls through for a slow producer (see try_lock).
            self.publish_fill(self.ring.fill_frames() as u64);
            if self.ring.fill_frames() > 0 {
                self.prime_periods = self.prime_periods.saturating_add(1);
            }
            self.try_lock();
        }
        if !self.locked {
            self.render_silence(out);
            return 0;
        }

        // A reader-overrun (the ring dropped frames the cursor hadn't reached)
        // skips the cursor forward to the oldest live frame — same guard
        // content_bridge uses; without it the cursor would read zeros.
        let read = self.ring.read_frame() as f64;
        if self.next_input_frame < read {
            self.next_input_frame = read;
        }

        let fill = self.ring.write_frame() as f64 - self.next_input_frame;
        // Locked: the cursor-relative fill is what the DLL disciplines toward
        // target — the value that proves engagement. Publish it for STATUS.
        self.publish_fill(fill.max(0.0) as u64);
        let minimum_safe_fill = self.minimum_safe_fill_frames() as f64;
        if fill < minimum_safe_fill {
            self.unlock_for_underfill();
            self.render_silence(out);
            return 0;
        }

        let error_frames = fill - self.hold_fill_frames() as f64;
        let ratio = self.controller.next_ratio(error_frames);
        self.publish_ratio();

        // Guard: emitting one period at this ratio must not read past the
        // newest written frame (kernel rightmost tap included). If it would,
        // unlock and silence — the same fail-closed boundary as content_bridge.
        let required_end = self.next_input_frame + ratio * self.period_frames as f64;
        if required_end + RADIUS_FRAMES as f64 > self.ring.write_frame() as f64 {
            self.unlock_for_underfill();
            self.render_silence(out);
            return 0;
        }

        for frame in 0..self.period_frames {
            let ramp_gain = if self.startup_ramp_frames_remaining > 0 {
                let frames_done = self.period_frames - self.startup_ramp_frames_remaining;
                (frames_done + 1) as f64 / self.period_frames as f64
            } else {
                1.0
            };
            for channel in 0..self.channels {
                let sample =
                    self.sinc_table
                        .interpolate(&self.ring, self.next_input_frame, channel);
                out[frame * self.channels + channel] = if ramp_gain < 1.0 {
                    clamp_i16(sample as f64 * ramp_gain)
                } else {
                    sample
                };
            }
            self.next_input_frame += ratio;
            self.startup_ramp_frames_remaining =
                self.startup_ramp_frames_remaining.saturating_sub(1);
        }

        // Free history behind the cursor, keeping the kernel's left taps.
        let keep_from = self.next_input_frame.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        self.output_frames
            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
        self.real_periods_since_lock = self.real_periods_since_lock.saturating_add(1);
        self.period_frames
    }

    /// Drop the lane's standing latency down to its held target by discarding
    /// the OLDEST buffered input, WITHOUT losing lock or resetting the
    /// controller. Returns the number of input frames dropped (0 when the lane
    /// is unlocked or already at/below its held target).
    ///
    /// ## Why this exists (the standing-fill trim, v2)
    ///
    /// The lane's live latency is the CURSOR-RELATIVE fill —
    /// `write_frame - next_input_frame`, the same value [`render_period`]
    /// disciplines toward [`hold_fill_frames`]. On hardware the USB lane was
    /// observed sitting at ~1919 frames against a 512-held target with lock
    /// churn: each idle/xrun/underfill `reset()` re-primed the DLL and the fill
    /// crept back up, deepening with every relock. A `reset()`-based trim is
    /// therefore the WRONG tool — it is the very lock-loss that produced the
    /// churn. This trim drops the excess in place: it advances the fractional
    /// read cursor forward over the oldest buffered frames (skipping past the
    /// stale head-start), then frees the ring history the cursor no longer
    /// needs. The only discontinuity is the one skip at the drop boundary — a
    /// single glitch, not a lock loss. `locked`, the `RateController` loop
    /// state, and the retained recent history all survive.
    ///
    /// ## Keep-newest, lock-preserving mechanics
    ///
    /// - No-op unless locked and `fill > hold_fill_frames()` (nothing to trim).
    /// - Advance `next_input_frame` forward by the excess so the post-trim
    ///   cursor-relative fill equals `hold_fill_frames()` — the newest frames
    ///   (those ahead of the new cursor) are preserved; the oldest are skipped.
    /// - Re-seat the cursor no earlier than the ring's live read boundary (guard
    ///   against a cursor that had lagged `read_frame`), then `drop_before` frees
    ///   the history behind it, keeping the kernel's left taps.
    /// - `locked`, `controller`, `real_periods_since_lock`, and the startup ramp
    ///   are untouched — the next `render_period` continues from the new cursor
    ///   with the same loop state, so the DLL simply sees the fill snap to target
    ///   (an error step it already handles) rather than a re-acquisition.
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
        // Skip the cursor forward over the oldest `drop` frames — keeping the
        // newest `target` frames ahead of it. One discontinuity at this skip;
        // lock and loop state are preserved.
        self.next_input_frame += drop;
        // Free ring history behind the new cursor, keeping the kernel's left
        // taps (identical bookkeeping to the end of render_period).
        let keep_from = self.next_input_frame.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        // Republish the (now-at-target) fill so STATUS reflects the drop
        // immediately, before the next render period runs.
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
        self.prime_periods = 0;
        self.startup_ramp_frames_remaining = 0;
        self.real_periods_since_lock = 0;
        // Session-boundary snap: re-prime the next `try_lock` at the FLOOR when a
        // live host-compliance proof says this host is compliant (skip the
        // ~2.5-min descent — per-session, not construction-only), else re-seat at
        // the full acquisition ceiling (a fresh cold start must acquire deep).
        // Instant + no glitch — moving a setpoint just lets the fill refill from
        // input. The proof-validity signal is the same atomic the mixer's revoke
        // path clears, so a revoked/absent proof lands this at the ceiling.
        self.snap_decay_back_honoring_proof(DecayFrozenReason::Unlocked);
        self.publish_ratio();
    }

    /// Lock once enough input has buffered to seat the cursor at the held
    /// target (`target_fill + warm-up cushion`) behind the write head with
    /// kernel headroom. Until then `render_period` emits silence (the lane
    /// simply hasn't started, exactly like an idle renderer's snd-aloop
    /// substream).
    ///
    /// The c57 cushion-drain path intentionally consumed faster after lock to
    /// return to the base target. That passed a steady-input unit test but
    /// backfired on hardware's bursty cold feed. The DLL now acquires and holds
    /// the deeper setpoint from the first real period, avoiding startup
    /// over-consumption.
    ///
    /// Bounded prime: if the full cushion never accumulates (a slow-but-real
    /// producer delivering just under one period per render) the loop would sit
    /// silent forever. After `max_prime_periods` priming periods with at least
    /// the safe minimum buffered, fall through and seat at whatever depth is
    /// available so a real stream always starts.
    fn try_lock(&mut self) {
        let fill = self.ring.fill_frames();
        let deep_prefill = self.startup_prefill_frames();
        // FLOOR-PRIME SEATING (jts.local 2026-07-03). When the lane is
        // floor-primed off a valid host-compliance proof, `hold_fill_frames()`
        // already equals the decay floor, so `deep_prefill` (= floor + kernel
        // radius + 1) is small — one render period plus ~12 ms of ring. In that
        // case DISABLE the shallow bounded-prime fall-through: the lock must seat
        // at the full floor depth, never at the bare interpolation minimum. The
        // old fall-through could seat the cursor at ~minimum_safe (≈274 fr) while
        // the held target sat at the floor (≈576 fr), so the resampler then
        // RAILED its ±500 ppm correction for the ~13 s it took to build the fill
        // from the shallow seat up to the floor — and the lock-gated compliance
        // probe, running during that railed acquisition, measured
        // baseline≈step≈−500 ppm → response_ratio≈0 → a spurious probe FAIL that
        // revoked the proof. Seating AT the floor removes the post-lock railed
        // build entirely. COST: on a floor-primed session the lock waits for the
        // fill to reach floor + radius + 1 instead of the shallow minimum —
        // roughly `floor − minimum_safe` extra frames (≈302 frames ≈ 6 ms at
        // 48 kHz) of buffered silence/stream before the first audio period. That
        // is a one-time per-session cost paid to keep the (much larger) descent
        // skip honest. A floor-primed host is a proven-compliant one delivering
        // at ~DAC rate, and the lane consumes NOTHING while priming (the ring
        // only grows), so reaching the shallow floor depth is a few render
        // periods for any real stream — the fall-through's slow-producer guard is
        // not load-bearing at this depth. NON-PRIMED (ceiling) sessions are
        // UNCHANGED: `is_floor_primed()` is false, so `deep_prefill` is the full
        // ceiling (target + warm-up cushion + radius + 1 ≈ 2577 fr) and the
        // shallow fall-through still fires on the bounded-prime expiry exactly as
        // before — a fresh cold start still acquires deep. A ceiling session's
        // deep-prefill/held-target relationship already seats near-target (the
        // seat IS `hold_fill_frames()` on the deep path), so it needs no change.
        let floor_primed = self.is_floor_primed();
        // Fall-through seat depth once the bounded prime expires: the most we
        // can safely seat given what's buffered, never below the safe minimum
        // (so we don't lock straight into an underfill→silence) and never above
        // the full cushion depth. Suppressed entirely while floor-primed (see
        // above) so the lock cannot seat shallow.
        let prime_expired =
            self.max_prime_periods > 0 && self.prime_periods >= self.max_prime_periods;
        let seat = if fill >= deep_prefill {
            // Enough for the full held target (the floor when floor-primed, the
            // warm-up cushion otherwise).
            self.hold_fill_frames()
        } else if !floor_primed && prime_expired && fill >= self.fallthrough_prefill_frames() {
            // Slow producer (ceiling acquisition only): seat at whatever we have,
            // but only after there is one render period of runway beyond the hard
            // interpolation floor. Hardware USB acquisition can arrive in short
            // bursts; seating at the bare minimum caused lock→underfill→relock
            // chatter before the ring built enough depth to run continuously.
            // NEVER taken while floor-primed — that path seats at the floor via
            // the deep-prefill branch above, so no shallow-seat railed build.
            fill - (RADIUS_FRAMES as usize + 1)
        } else {
            // Keep priming.
            return;
        };
        self.next_input_frame = (self.ring.write_frame() - seat as u64) as f64;
        let keep_from = self.next_input_frame.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        self.locked = true;
        self.locked_state.store(true, Ordering::Relaxed);
        self.prime_periods = 0;
        self.startup_ramp_frames_remaining = self.period_frames;
        self.real_periods_since_lock = 0;
        self.controller.reset();
        self.lock_count.fetch_add(1, Ordering::Relaxed);
    }

    fn unlock_for_underfill(&mut self) {
        self.locked = false;
        self.locked_state.store(false, Ordering::Relaxed);
        self.unlock_count.fetch_add(1, Ordering::Relaxed);
        let acquisition_underfill = self.real_periods_since_lock < self.max_prime_periods;
        if !acquisition_underfill {
            self.ring.clear();
        }
        self.controller.reset();
        self.next_input_frame = 0.0;
        self.prime_periods = 0;
        self.startup_ramp_frames_remaining = 0;
        self.real_periods_since_lock = 0;
        // Session-boundary snap: the underfill unlock is where a stopped host
        // (Mac sleeps / stops streaming) ends the session. Re-prime the NEXT lock
        // at the FLOOR when a live compliance proof says this host is compliant
        // (a written proof is EVIDENCE the floor held for the settle window with
        // zero churn), else at the ceiling (a fresh cold start acquires deep).
        // Self-healing if the floor is later unsustainable: the re-lock re-arms the
        // early-revalidation window, and the RevalidationTracker's churn
        // discriminator revokes the proof if this unlock is FOLLOWED by a relock
        // within `HOST_COMPLIANCE_CHURN_CONFIRM_SECS` — i.e. the host kept streaming
        // but the floor could not hold (unlock→relock churn). A terminal stream-end
        // (this unlock with NO relock — the macOS short-session norm) does NOT revoke:
        // the pending strike expires harmlessly, so the proof survives and the next
        // real stream still primes at the floor (see HANDOFF-usb-low-latency.md
        // revalidation section). Without a snap here, a re-lock after decay would seat
        // at the shallow decayed depth and re-thrash lock.
        self.snap_decay_back_honoring_proof(DecayFrozenReason::Unlocked);
        self.publish_fill(if acquisition_underfill {
            self.ring.fill_frames() as u64
        } else {
            0
        });
        self.publish_ratio();
    }

    /// Snap the decay's held target back to the acquisition ceiling and publish
    /// the raised gauge + decay observability immediately. The UNCONDITIONAL
    /// ceiling snap: used by [`snap_decay_to_ceiling`](Self::snap_decay_to_ceiling)
    /// (the revalidation-failure escape, which must always re-seat deep) and, on a
    /// lock-loss path with NO live compliance proof, by
    /// [`snap_decay_back_honoring_proof`](Self::snap_decay_back_honoring_proof).
    /// Inert (no-op-cheap) when the decay feature is off.
    fn snap_decay_back(&mut self, reason: DecayFrozenReason) {
        self.decay.snap_back(reason);
        self.publish_decay_gauges();
    }

    /// Snap the decayed held target back at a session boundary (`reset`,
    /// `unlock_for_underfill`), choosing the destination by the LIVE persisted
    /// proof: FLOOR when a valid, unrevoked proof is live on this lane (skip the
    /// ~2.5-min descent for the next session — the whole point of persisted
    /// compliance, now PER-SESSION rather than construction-only); the CEILING
    /// otherwise (unchanged — a fresh session with no proof re-acquires deep).
    ///
    /// The proof-validity signal is [`live_proof_present`](Self::live_proof_present),
    /// which reads the SAME `flag_present` atomic the mixer's revoke/write path
    /// mutates — the single source of truth that governs revocation. So a revoke
    /// (which clears `flag_present` before any subsequent snap) makes the very next
    /// session boundary land at the ceiling, and a clean proof makes it land at the
    /// floor. On the floor path the machine sets its `floor_prime_pending` latch so
    /// the not-yet-locked prime periods hold the floor across `try_lock` — identical
    /// to a construction-time prime. Inert (no-op-cheap) when the decay feature is
    /// off (`live_proof_present` is `false`, so this is the ceiling `snap_back`,
    /// itself a no-op at the pinned ceiling).
    fn snap_decay_back_honoring_proof(&mut self, reason: DecayFrozenReason) {
        if self.live_proof_present() {
            // A valid proof is live → re-prime at the floor for the next session.
            // `prime_at_floor` seeds `held == floor`, the `AtFloor` reason, and the
            // `floor_prime_pending` latch (holds the floor across the pre-lock prime
            // periods). No-op on a disabled machine, but `live_proof_present` is only
            // ever true when the feature is armed.
            self.decay.prime_at_floor();
        } else {
            // No live proof → re-acquire from the ceiling (today's behaviour).
            self.decay.snap_back(reason);
        }
        self.publish_decay_gauges();
    }

    /// Whether a valid, unrevoked host-compliance proof is live on this lane RIGHT
    /// NOW, read from the shared `flag_present` atomic the mixer owns (set on a
    /// valid boot load / successful write, cleared on a revoke). This is the ONE
    /// authoritative "is the proof valid" signal — the same state the mixer's
    /// revocation path mutates — so the session-boundary snap destination and the
    /// mixer's per-lock `floor_primed` revalidation gate can never disagree. `false`
    /// whenever the feature is off (no `compliance` handle was injected). A single
    /// relaxed atomic load; no allocation.
    fn live_proof_present(&self) -> bool {
        self.compliance
            .as_ref()
            .map(|c| c.flag_present.load(Ordering::Relaxed))
            .unwrap_or(false)
    }

    /// Republish the held-target gauge + decay observability atomics from the
    /// current decay state. Shared by every path that mutates the decay's held
    /// target (`snap_decay_back`, `snap_decay_back_honoring_proof`,
    /// `prime_decay_at_floor`, `tick_decay`) so STATUS and the outer DLL setpoint
    /// always read a consistent snapshot. Three relaxed stores; no allocation.
    fn publish_decay_gauges(&self) {
        self.held_target_frames
            .store(self.decay.held(), Ordering::Relaxed);
        self.decay_active
            .store(self.decay.active(), Ordering::Relaxed);
        self.decay_frozen_reason.store(
            DecayFrozenReason::code(self.decay.frozen_reason()),
            Ordering::Relaxed,
        );
    }

    /// Prime the post-lock cushion decay directly at its floor (the persisted
    /// host-compliance prime-at-floor entry). Called at lane build time, BEFORE
    /// the first render period, when the mixer has a VALID persisted proof whose
    /// recorded floor matches this lane's live floor. Seeds the held target at the
    /// floor so the first `try_lock` seats the cursor shallow (skipping the
    /// ~2.5-min descent) and re-publishes the held-target + decay atomics so
    /// STATUS and the outer DLL setpoint read the floor from period one. A
    /// no-op-cheap when decay is disabled (the held target stays at the ceiling).
    ///
    /// Build time is the FIRST prime; subsequent per-session primes ride
    /// [`snap_decay_back_honoring_proof`](Self::snap_decay_back_honoring_proof) at
    /// each session boundary (both call `CushionDecay::prime_at_floor`).
    pub fn prime_decay_at_floor(&mut self) {
        self.decay.prime_at_floor();
        self.publish_decay_gauges();
    }

    /// The configured decay floor (total frames) — the geometry a persisted
    /// compliance proof records and is validated against. Reads the same value
    /// STATUS shows as `decay.floor_frames`.
    pub fn decay_floor_frames(&self) -> u64 {
        self.decay.floor()
    }

    /// Whether the decay is at (or below) its floor as of the last tick — the
    /// compliance proof's "descent complete" signal. True iff the decay's frozen
    /// reason is `AtFloor` (which includes the prime-at-floor entry). Always
    /// `false` when decay is disabled (the machine never reaches AtFloor).
    pub fn decay_at_floor(&self) -> bool {
        self.decay.frozen_reason() == Some(DecayFrozenReason::AtFloor)
    }

    /// Whether the lane is currently FLOOR-PRIMED and awaiting its first lock —
    /// the held target is seeded at the floor and `try_lock` has not yet seated
    /// the cursor. When true, `try_lock` seats AT the floor depth rather than
    /// taking the shallow bounded-prime fall-through, so the lock lands at the
    /// target and the resampler does not rail to build the fill up afterward
    /// (the hardware-diagnosed probe false-fail, jts.local 2026-07-03). Always
    /// `false` on a non-primed lane (feature off or no valid proof), so a
    /// ceiling session's seating is unchanged. A plain delegated field read.
    fn is_floor_primed(&self) -> bool {
        self.decay.floor_prime_pending()
    }

    /// The cumulative underfill-unlock count — the compliance proof's churn gauge
    /// (it watches the DELTA across the settle window) and the mixer's early-
    /// session revalidation trigger. A single relaxed load (no Arc clone).
    pub fn unlock_count(&self) -> u64 {
        self.unlock_count.load(Ordering::Relaxed)
    }

    /// Whether the lane is currently locked (rendering real DAC-paced audio). The
    /// mixer's host-compliance service reads this to detect lock edges (a fresh
    /// lock begins a session; a lock loss ends it) without reaching into the
    /// resampler's private `locked` field. A plain field read.
    pub fn is_locked(&self) -> bool {
        self.locked
    }

    /// Snap the decayed held target back to the acquisition ceiling NOW — the
    /// host-compliance REVALIDATION escape. When a floor-primed session fails
    /// revalidation (probe fail / DLL demotion / early underfill unlock) the mixer
    /// calls this to abandon the (evidently wrong) floor prime and re-seat deep,
    /// exactly the existing snap-back primitive the lock-loss paths use. Raising a
    /// setpoint needs no drop (the fill refills from input); this only lifts the
    /// held target + republishes the gauges. No-op-cheap when decay is off.
    pub fn snap_decay_to_ceiling(&mut self) {
        self.snap_decay_back(DecayFrozenReason::Unlocked);
    }

    fn render_silence(&mut self, out: &mut [i16]) {
        out.fill(0);
        self.silence_frames
            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
    }

    /// Minimum buffered frames to safely render one period at the worst-case
    /// (max-ppm) ratio with kernel headroom. Same shape as content_bridge.
    /// Delegates to the shared `jasper_resampler` helper — the single source of
    /// truth the config-time decay-floor validation also uses.
    fn minimum_safe_fill_frames(&self) -> usize {
        jasper_resampler::minimum_safe_fill_frames(self.period_frames as u32, self.max_adjust_ppm)
    }

    /// Frames the ring must hold before lock seats the cursor at the LIVE held
    /// target with kernel headroom. The held target is the decay floor when the
    /// lane is floor-primed (so this is the small floor-seat depth that makes the
    /// lock land at the target with no railed post-lock build) and the
    /// acquisition ceiling (`target + warm-up cushion`) otherwise.
    fn startup_prefill_frames(&self) -> usize {
        self.hold_fill_frames() + RADIUS_FRAMES as usize + 1
    }

    /// Minimum buffered frames for the bounded-prime fallback. This is lower
    /// than the full held-cushion prefill, but high enough that the first
    /// fallback lock has one full render period of runway if the next USB burst
    /// is late.
    fn fallthrough_prefill_frames(&self) -> usize {
        let interpolation_runway =
            self.minimum_safe_fill_frames() + self.period_frames + RADIUS_FRAMES as usize + 1;
        let usb_burst_runway =
            self.target_fill_frames + (2 * self.period_frames) + RADIUS_FRAMES as usize + 1;
        interpolation_runway.max(usb_burst_runway)
    }

    /// The LIVE held target the controller disciplines the ring toward — the
    /// decayed setpoint when the DEFAULT-OFF cushion decay is engaged, otherwise
    /// the static ceiling. Read from the held-target gauge (the single source of
    /// truth), so `render_period`'s DLL error, `trim_ring`'s drop target, and the
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

    /// Advance the DEFAULT-OFF post-lock cushion decay one render period and
    /// publish the (possibly-lowered) held target + decay observability. The
    /// caller (the mixer work loop) supplies the outer-DLL signals the decay
    /// needs (`dll_l0_locked`, `commanded_ppm_abs`); the resampler's own
    /// `locked` state is filled in here. No-op-cheap when the feature is off
    /// (one `CushionDecay::tick` early-return + three relaxed stores).
    ///
    /// The decay clock is render PERIODS — this is called exactly once per
    /// `render_period`, never on a wall clock.
    pub fn tick_decay(&mut self, dll_l0_locked: bool, commanded_ppm_abs: f64) {
        self.decay.tick(DecaySignals {
            locked: self.locked,
            dll_l0_locked,
            commanded_ppm_abs,
        });
        self.publish_decay_gauges();
    }

    fn publish_ratio(&self) {
        // Store ppm × 1000 (milli-ppm) as i64 bits in the u64 atomic.
        let milli_ppm = (self.controller.ratio_ppm() * 1000.0).round() as i64;
        self.ratio_milli_ppm
            .store(milli_ppm as u64, Ordering::Relaxed);
    }

    fn publish_fill(&self, frames: u64) {
        self.fill_frames.store(frames, Ordering::Relaxed);
    }
}

/// The DEFAULT-OFF post-lock cushion-decay engine — a PURE, render-period-clocked
/// state machine that lowers the resampler's held target from its acquisition
/// ceiling toward a floor while the lane is locked, the outer host-clock DLL is
/// `l0_locked`, and the DLL is not commanding hard. No atomics, no ALSA, no
/// clock: the mixer ticks it once per render period. Scratch-crate-testable on
/// any host (fan-in cannot compile on macOS).
///
/// ## Why decay, not a static lower cushion
///
/// The full acquisition cushion is load-bearing during the bursty USB cold start
/// (a static 128-frame cushion was refuted twice on hardware — free-run never
/// locks; under the live DLL it locks but latency REGRESSES from lock churn
/// re-priming the fill above the setpoint). Steady state, once the DLL has pinned
/// the fill at the setpoint, does NOT need the full cushion. So: acquire deep,
/// then decay the held target only while the system proves it is in the stable
/// `l0_locked` regime, and snap all the way back the instant it leaves.
mod decay {
    /// Why the held target is currently frozen (not decaying) — surfaced in
    /// STATUS so an operator can see *why* a decay run stalled. `None` (via the
    /// `code`/`NONE_CODE` mapping) means actively decaying.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum DecayFrozenReason {
        /// Resampler is not locked — snapped back to the ceiling.
        Unlocked,
        /// The DLL ladder is not `l0_locked` — snapped back to the ceiling.
        NotL0,
        /// The DLL is commanding hard (|commanded_ppm| > guard) — hold, no step.
        Cascade,
        /// Locked + l0 but still inside the post-lock stability window — hold.
        Warmup,
        /// Held target is already at the floor — nothing left to decay.
        AtFloor,
    }

    impl DecayFrozenReason {
        /// The STATUS wire code for "actively decaying" (no frozen reason).
        pub const NONE_CODE: u64 = 0;

        /// Map an optional reason to its stable STATUS integer code (stored in a
        /// lock-free atomic; the state layer maps back to a string). `0` == none
        /// (actively decaying). Codes are a wire contract — append, never renumber.
        pub fn code(reason: Option<DecayFrozenReason>) -> u64 {
            match reason {
                None => Self::NONE_CODE,
                Some(DecayFrozenReason::Unlocked) => 1,
                Some(DecayFrozenReason::NotL0) => 2,
                Some(DecayFrozenReason::Cascade) => 3,
                Some(DecayFrozenReason::Warmup) => 4,
                Some(DecayFrozenReason::AtFloor) => 5,
            }
        }

        /// Map a STATUS code back to its lowercase string for the JSON block.
        /// Unknown codes render as `""` (treated as "actively decaying").
        pub fn code_str(code: u64) -> &'static str {
            match code {
                1 => "unlocked",
                2 => "not_l0",
                3 => "cascade",
                4 => "warmup",
                5 => "at_floor",
                _ => "",
            }
        }
    }

    /// Validated decay knobs from config, plus the derived `enabled` gate. The
    /// resampler owns the ceiling (target + cushion) and derives the render-period
    /// intervals from the sample rate / period at construction — the caller passes
    /// only the frame/ms knobs so there is ONE place (`build`) that converts ms →
    /// periods.
    #[derive(Debug, Clone, Copy)]
    pub struct DecayParams {
        pub enabled: bool,
        /// Total held-target floor in frames (must be >= base target + a small
        /// margin; config validates fail-loud).
        pub floor_frames: u64,
        /// Frames dropped per decay step.
        pub step_frames: u64,
        /// Wall interval between steps, in ms — converted to render periods here.
        pub interval_ms: u64,
        /// Post-lock stability window before the first step, in ms — converted to
        /// render periods here.
        pub stability_ms: u64,
        /// |commanded_ppm| above which decay pauses (the cascade-stability guard).
        pub cascade_guard_ppm: f64,
    }

    impl DecayParams {
        /// A hard-disabled params (current behaviour: held pinned at ceiling).
        /// Test-only: the daemon always builds `DecayParams` from the parsed env
        /// config (see `mixer::build_lane_resampler`), so this convenience is only
        /// used by the resampler unit tests. Gated `#[cfg(test)]` so it is not
        /// dead code in the `jasper-fanin` binary build (`-D warnings`).
        #[cfg(test)]
        pub fn disabled() -> Self {
            Self {
                enabled: false,
                floor_frames: 0,
                step_frames: 16,
                interval_ms: 1000,
                stability_ms: 10_000,
                cascade_guard_ppm: 400.0,
            }
        }

        /// Convert `ms` at the lane's `period_frames`/`sample_rate` to a
        /// render-period count (>= 1 so a tiny ms value still ticks). The decay
        /// clock is render periods, so every wall-time knob is normalised HERE.
        fn ms_to_periods(ms: u64, period_frames: u32, sample_rate: u32) -> u64 {
            let period_frames = period_frames.max(1) as u64;
            let sample_rate = sample_rate.max(1) as u64;
            // periods = ms/1000 * rate / period_frames.
            ((ms.saturating_mul(sample_rate)) / (1000 * period_frames)).max(1)
        }

        /// Build the runtime state machine, deriving the render-period intervals
        /// from the lane geometry and clamping the floor defensively.
        ///
        /// Two clamps, both fail-safe (a bad knob degrades to a safe run, never
        /// misbehaviour): the floor is raised to the physical
        /// `minimum_safe_fill_frames` (the underfill-unlock threshold) so decay
        /// can never descend onto churn-by-construction — a held target at/below
        /// that value sits on the unlock threshold and per-period fill jitter
        /// would trip lock churn. It is then capped at `ceiling` (nothing to
        /// decay above the acquisition depth). Config validation rejects an
        /// out-of-range floor fail-loud when the feature is armed; this is the
        /// belt-and-braces for anything that slips past (a custom geometry where
        /// `minimum_safe` exceeds the validated `target + margin` bound).
        pub fn build(
            self,
            ceiling: u64,
            period_frames: u32,
            sample_rate: u32,
            max_adjust_ppm: f64,
        ) -> CushionDecay {
            let interval_periods =
                Self::ms_to_periods(self.interval_ms, period_frames, sample_rate);
            let stability_periods =
                Self::ms_to_periods(self.stability_ms, period_frames, sample_rate);
            let min_safe =
                jasper_resampler::minimum_safe_fill_frames(period_frames, max_adjust_ppm) as u64;
            // Never decay onto (or below) the underfill-unlock threshold. Keep the
            // same working margin above it that the config validation enforces, so
            // ordinary DLL steering jitter around the pinned setpoint cannot cross
            // the threshold from the floor. The `.min(ceiling)` keeps a
            // pathological `min_safe > ceiling` geometry (nothing safe to decay
            // to) degrading to "no decay" rather than a floor above the ceiling.
            let safe_floor =
                min_safe.saturating_add(crate::config::CUSHION_DECAY_FLOOR_MARGIN_FRAMES as u64);
            let floor = self.floor_frames.max(safe_floor).min(ceiling);
            CushionDecay::new(
                self.enabled,
                ceiling,
                floor,
                self.step_frames,
                interval_periods,
                stability_periods,
                self.cascade_guard_ppm,
            )
        }
    }

    /// The per-tick signals the decay reads that it cannot derive itself: the
    /// resampler's own lock state plus the outer DLL's ladder/command. Sampled
    /// once per render period.
    #[derive(Debug, Clone, Copy)]
    pub struct DecaySignals {
        /// The resampler is locked and rendering real DAC-paced audio.
        pub locked: bool,
        /// The outer host-clock DLL ladder is `l0_locked` (the only steady state
        /// where the fill is pinned at the setpoint). Decay REQUIRES this — with
        /// the DLL off / probing / demoted, the held cushion is load-bearing.
        pub dll_l0_locked: bool,
        /// The DLL's last commanded bias magnitude in ppm. When the DLL is
        /// working hard (> the cascade guard) the fill is in transient, so decay
        /// pauses.
        pub commanded_ppm_abs: f64,
    }

    /// The decay state machine. See the module docstring for the "acquire deep,
    /// decay in steady state, snap back on any discontinuity" rationale.
    #[derive(Debug, Clone)]
    pub struct CushionDecay {
        enabled: bool,
        /// The acquisition hold the held target starts at and snaps back to.
        ceiling: u64,
        /// The lowest the held target may decay to (total frames).
        floor: u64,
        /// Frames dropped per decay step.
        step: u64,
        /// Render periods between decay steps.
        interval_periods: u64,
        /// Render periods of continuous locked+l0+calm required before the FIRST
        /// step (the post-lock warm-up window).
        stability_periods: u64,
        /// |commanded_ppm| above which decay pauses.
        cascade_guard_ppm: f64,

        /// Current held target (the live setpoint). Starts at `ceiling`.
        held: u64,
        /// Consecutive locked+l0+calm periods (resets on any freeze condition).
        stable_periods: u64,
        /// Periods since the last decay step (only advances while decaying).
        periods_since_step: u64,
        /// Last computed reason; `None` while actively decaying.
        frozen_reason: Option<DecayFrozenReason>,
        /// PRIME-AT-FLOOR latch: `true` between [`prime_at_floor`] and the first
        /// period this machine sees the lane LOCKED. While set, the not-yet-locked
        /// prime periods keep the held target at the FLOOR instead of snapping it
        /// up to the ceiling — so the imminent `try_lock` seats the cursor at the
        /// floor depth (the whole point of persisted compliance). Cleared the first
        /// time `tick` observes `locked`; after that, any lock LOSS snaps back to
        /// the ceiling normally (a real re-acquisition must start deep). Always
        /// `false` on a machine that was never primed.
        floor_prime_pending: bool,
    }

    impl CushionDecay {
        /// Build the machine. The caller (config) validates the knobs fail-loud;
        /// this constructor clamps defensively (`floor <= ceiling`, `step >= 1`,
        /// `interval >= 1`) so a bad value degrades to "no decay" not misbehaviour.
        pub fn new(
            enabled: bool,
            ceiling: u64,
            floor: u64,
            step: u64,
            interval_periods: u64,
            stability_periods: u64,
            cascade_guard_ppm: f64,
        ) -> Self {
            Self {
                enabled,
                ceiling,
                floor: floor.min(ceiling),
                step: step.max(1),
                interval_periods: interval_periods.max(1),
                stability_periods,
                cascade_guard_ppm,
                held: ceiling,
                stable_periods: 0,
                periods_since_step: 0,
                frozen_reason: if enabled {
                    Some(DecayFrozenReason::Warmup)
                } else {
                    None
                },
                floor_prime_pending: false,
            }
        }

        /// The live held target (the resampler's setpoint). Always `ceiling` when
        /// disabled.
        pub fn held(&self) -> u64 {
            self.held
        }

        /// The floor (for STATUS).
        pub fn floor(&self) -> u64 {
            self.floor
        }

        /// True iff actively decaying (enabled, not frozen, above the floor).
        pub fn active(&self) -> bool {
            self.enabled && self.frozen_reason.is_none() && self.held > self.floor
        }

        /// The current frozen reason (for STATUS). `None` while decaying.
        pub fn frozen_reason(&self) -> Option<DecayFrozenReason> {
            self.frozen_reason
        }

        /// Whether a prime-at-floor is currently PENDING — the held target is
        /// seeded at the floor and the lane has NOT yet locked since the prime
        /// (`prime_at_floor` set the latch; the first locked `tick` clears it).
        /// Read by `LaneResampler::try_lock` so a floor-primed lock seats the
        /// cursor AT the floor depth instead of taking the shallow bounded-prime
        /// fall-through — the seat lands at the target, so there is no post-lock
        /// railed fill-build (the hardware-diagnosed probe false-fail on
        /// jts.local 2026-07-03). Always `false` on a machine that was never
        /// primed (feature off, or no valid proof), so the ceiling path is
        /// byte-identical to today.
        pub fn floor_prime_pending(&self) -> bool {
            self.floor_prime_pending
        }

        /// Snap the held target back to the ceiling and reset decay progress.
        /// Called on any hard boundary (unlock / DLL demotion / stream stop).
        /// Raising a setpoint needs no drop — the fill refills from input.
        pub fn snap_back(&mut self, reason: DecayFrozenReason) {
            self.held = self.ceiling;
            self.stable_periods = 0;
            self.periods_since_step = 0;
            // A snap-back is a hard discontinuity: whatever prime-at-floor state we
            // were in is abandoned. Clear the latch so the raised ceiling actually
            // takes effect (the whole point of a snap-back is to re-acquire deep).
            self.floor_prime_pending = false;
            if self.enabled {
                self.frozen_reason = Some(reason);
            }
        }

        /// Prime the machine directly at the floor — the persisted-host-compliance
        /// prime-at-floor entry (the proof from a prior session says this host was
        /// compliant, so skip the ~2.5-min descent and start where decay would have
        /// landed). Seeds `held == floor` and the `AtFloor` frozen reason so the
        /// imminent `try_lock` seats the cursor at the FLOOR depth; the ceiling is
        /// unchanged, so ANY discontinuity after lock still snaps all the way back
        /// to it (the snap-back primitive is the revalidation-failure escape,
        /// untouched).
        ///
        /// Sets the [`floor_prime_pending`](Self::floor_prime_pending) latch so the
        /// not-yet-locked prime periods do NOT snap the held target up to the
        /// ceiling (the `!locked` branch of `tick` would otherwise undo the prime
        /// before the first lock ever seats). The latch clears the first period
        /// `tick` sees the lane locked.
        ///
        /// A no-op-cheap on a disabled machine (nothing to prime). Called at lane
        /// build time BEFORE the first tick; the mixer gates it on a VALID
        /// persisted proof whose recorded floor matches the live floor.
        pub fn prime_at_floor(&mut self) {
            if !self.enabled {
                return;
            }
            self.held = self.floor;
            self.stable_periods = 0;
            self.periods_since_step = 0;
            self.frozen_reason = Some(DecayFrozenReason::AtFloor);
            self.floor_prime_pending = true;
        }

        /// Advance one render period, returning the (possibly-lowered) held
        /// target. Pure: no clock, no I/O. The decay clock is render PERIODS.
        pub fn tick(&mut self, s: DecaySignals) -> u64 {
            if !self.enabled {
                return self.held;
            }
            // Prime-at-floor latch: while the lane has not yet locked since a
            // prime-at-floor, HOLD the floor across the pre-lock prime periods
            // instead of snapping up to the ceiling. Without this, the `!locked`
            // branch below would raise the held target to the ceiling on the very
            // first tick and `try_lock` would then seat deep — defeating the
            // persisted-compliance prime. The latch clears the instant the lane
            // locks (below), so a subsequent lock LOSS snaps back normally.
            if self.floor_prime_pending && !s.locked {
                self.stable_periods = 0;
                self.periods_since_step = 0;
                self.frozen_reason = Some(DecayFrozenReason::AtFloor);
                return self.held;
            }
            // Hard boundaries first: any loss of lock or DLL steady-state snaps
            // the held target back to the ceiling in one tick.
            if !s.locked {
                self.snap_back(DecayFrozenReason::Unlocked);
                return self.held;
            }
            // The lane is locked. Clear the prime-at-floor latch: from here on, a
            // lock LOSS is a real discontinuity that snaps back to the ceiling.
            self.floor_prime_pending = false;
            if !s.dll_l0_locked {
                self.snap_back(DecayFrozenReason::NotL0);
                return self.held;
            }
            // Cascade-stability guard: the DLL is working hard, so the fill is in
            // a transient — hold the current held target (do NOT step, do NOT snap
            // back), and reset stability so a burst re-earns the warm-up window.
            if s.commanded_ppm_abs > self.cascade_guard_ppm {
                self.stable_periods = 0;
                self.periods_since_step = 0;
                self.frozen_reason = Some(DecayFrozenReason::Cascade);
                return self.held;
            }
            // Locked + l0 + calm: accrue stability.
            self.stable_periods = self.stable_periods.saturating_add(1);
            if self.stable_periods < self.stability_periods {
                self.frozen_reason = Some(DecayFrozenReason::Warmup);
                return self.held;
            }
            // Past the warm-up window. If already at floor, nothing to do.
            if self.held <= self.floor {
                self.held = self.floor;
                self.frozen_reason = Some(DecayFrozenReason::AtFloor);
                return self.held;
            }
            // Actively decaying: step once per interval.
            self.frozen_reason = None;
            self.periods_since_step = self.periods_since_step.saturating_add(1);
            if self.periods_since_step >= self.interval_periods {
                self.periods_since_step = 0;
                self.held = self.held.saturating_sub(self.step).max(self.floor);
                if self.held <= self.floor {
                    self.frozen_reason = Some(DecayFrozenReason::AtFloor);
                }
            }
            self.held
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        const CEIL: u64 = 2560; // target 512 + cushion 2048
        const FLOOR: u64 = 544; // target 512 + 32
        const STEP: u64 = 16;
        const INTERVAL: u64 = 188; // ~1 s at 48k / 256
        const STABILITY: u64 = 1880; // ~10 s

        fn locked_l0(commanded_ppm_abs: f64) -> DecaySignals {
            DecaySignals {
                locked: true,
                dll_l0_locked: true,
                commanded_ppm_abs,
            }
        }

        fn build() -> CushionDecay {
            CushionDecay::new(true, CEIL, FLOOR, STEP, INTERVAL, STABILITY, 400.0)
        }

        #[test]
        fn disabled_pins_ceiling_forever() {
            let mut d = CushionDecay::new(false, CEIL, FLOOR, STEP, INTERVAL, STABILITY, 400.0);
            for _ in 0..100_000 {
                assert_eq!(d.tick(locked_l0(0.0)), CEIL);
            }
            assert!(!d.active());
            assert_eq!(d.frozen_reason(), None);
        }

        #[test]
        fn holds_ceiling_through_warmup_then_decays() {
            let mut d = build();
            for _ in 0..STABILITY - 1 {
                assert_eq!(d.tick(locked_l0(0.0)), CEIL);
            }
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::Warmup));
            // The stability-th tick crosses the window (first decaying tick).
            assert_eq!(d.tick(locked_l0(0.0)), CEIL);
            assert!(d.active(), "past warm-up, should be actively decaying");
            for _ in 0..INTERVAL - 2 {
                assert_eq!(d.tick(locked_l0(0.0)), CEIL);
            }
            // The INTERVAL-th decaying tick fires the first step.
            assert_eq!(d.tick(locked_l0(0.0)), CEIL - STEP);
        }

        #[test]
        fn decays_monotonically_to_floor_and_stops() {
            let mut d = build();
            for _ in 0..2_000_000 {
                let h = d.tick(locked_l0(0.0));
                assert!((FLOOR..=CEIL).contains(&h));
                if h == FLOOR {
                    break;
                }
            }
            assert_eq!(d.held(), FLOOR);
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::AtFloor));
            assert!(!d.active(), "at floor is not active");
            for _ in 0..1000 {
                assert_eq!(d.tick(locked_l0(0.0)), FLOOR);
            }
        }

        #[test]
        fn steps_are_exactly_step_frames_each_interval() {
            let mut d = build();
            for _ in 0..STABILITY {
                d.tick(locked_l0(0.0));
            }
            let mut last = d.held();
            for _ in 0..10 {
                for _ in 0..INTERVAL {
                    d.tick(locked_l0(0.0));
                }
                assert_eq!(last - d.held(), STEP);
                last = d.held();
            }
        }

        #[test]
        fn unlock_snaps_back_to_ceiling_in_one_tick() {
            let mut d = build();
            for _ in 0..STABILITY + INTERVAL * 5 {
                d.tick(locked_l0(0.0));
            }
            assert!(d.held() < CEIL);
            let h = d.tick(DecaySignals {
                locked: false,
                dll_l0_locked: true,
                commanded_ppm_abs: 0.0,
            });
            assert_eq!(h, CEIL);
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::Unlocked));
            assert!(!d.active());
        }

        #[test]
        fn dll_demotion_snaps_back_to_ceiling() {
            let mut d = build();
            for _ in 0..STABILITY + INTERVAL * 5 {
                d.tick(locked_l0(0.0));
            }
            assert!(d.held() < CEIL);
            let h = d.tick(DecaySignals {
                locked: true,
                dll_l0_locked: false,
                commanded_ppm_abs: 0.0,
            });
            assert_eq!(h, CEIL);
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::NotL0));
        }

        #[test]
        fn cascade_guard_pauses_without_snapping_back_but_resets_warmup() {
            let mut d = build();
            for _ in 0..STABILITY + INTERVAL * 3 {
                d.tick(locked_l0(0.0));
            }
            let held_before = d.held();
            assert!(held_before < CEIL);
            let h = d.tick(locked_l0(401.0));
            assert_eq!(h, held_before, "cascade guard holds, does not snap back");
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::Cascade));
            assert!(!d.active());
            for _ in 0..STABILITY - 1 {
                assert_eq!(d.tick(locked_l0(0.0)), held_before);
            }
            for _ in 0..INTERVAL {
                d.tick(locked_l0(0.0));
            }
            assert_eq!(d.held(), held_before - STEP);
        }

        #[test]
        fn cascade_guard_boundary_is_strict_greater_than() {
            let mut d = build();
            for _ in 0..STABILITY {
                d.tick(locked_l0(0.0));
            }
            // Exactly at the guard: NOT paused (strict >).
            d.tick(locked_l0(400.0));
            assert_ne!(
                d.frozen_reason(),
                Some(DecayFrozenReason::Cascade),
                "commanded_ppm == guard must not pause (strict >)"
            );
            // Just over: paused.
            d.tick(locked_l0(400.001));
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::Cascade));
        }

        #[test]
        fn snap_back_then_recovery_re_earns_full_warmup() {
            let mut d = build();
            for _ in 0..STABILITY + INTERVAL * 2 {
                d.tick(locked_l0(0.0));
            }
            d.tick(DecaySignals {
                locked: false,
                dll_l0_locked: true,
                commanded_ppm_abs: 0.0,
            });
            assert_eq!(d.held(), CEIL);
            for _ in 0..STABILITY - 1 {
                assert_eq!(d.tick(locked_l0(0.0)), CEIL);
            }
            for _ in 0..INTERVAL {
                d.tick(locked_l0(0.0));
            }
            assert_eq!(d.held(), CEIL - STEP);
        }

        #[test]
        fn floor_clamped_to_ceiling_when_misconfigured() {
            let mut d = CushionDecay::new(true, 512, 9999, STEP, INTERVAL, 1, 400.0);
            assert_eq!(d.floor(), 512);
            for _ in 0..100_000 {
                assert_eq!(d.tick(locked_l0(0.0)), 512);
            }
        }

        #[test]
        fn last_step_clamps_to_floor_on_non_divisible_geometry() {
            // Nit 3: every other test geometry has (ceiling - floor) an exact
            // multiple of STEP, so `held.saturating_sub(step).max(floor)` never
            // exercises a non-divisible remainder. Here ceiling - floor = 2560 -
            // 545 = 2015 = 125*16 + 15, so the final step is a 15-frame remainder
            // that must clamp EXACTLY to the floor (never overshoot below it), and
            // decay must then stop with AtFloor.
            const ODD_FLOOR: u64 = 545;
            let mut d = CushionDecay::new(true, CEIL, ODD_FLOOR, STEP, INTERVAL, STABILITY, 400.0);
            let mut prev = CEIL;
            for _ in 0..2_000_000 {
                let h = d.tick(locked_l0(0.0));
                // Monotone non-increasing, never below the floor.
                assert!(h <= prev);
                assert!(
                    h >= ODD_FLOOR,
                    "held {h} must never dip below floor {ODD_FLOOR}"
                );
                prev = h;
                if h == ODD_FLOOR {
                    break;
                }
            }
            assert_eq!(d.held(), ODD_FLOOR, "must land exactly on the floor");
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::AtFloor));
            assert!(!d.active());
            // Stays pinned at the floor.
            for _ in 0..1000 {
                assert_eq!(d.tick(locked_l0(0.0)), ODD_FLOOR);
            }
        }

        #[test]
        fn build_lifts_a_churny_floor_above_minimum_safe_fill() {
            // Finding 2: DecayParams::build must defensively lift a floor that
            // sits on/below the physical underfill-unlock threshold
            // (minimum_safe_fill_frames) so decay is never churn-by-construction,
            // even if a churny value slips past config validation.
            const PERIOD: u32 = 256;
            const RATE: u32 = 48_000;
            const MAX_PPM: f64 = 500.0;
            let min_safe = jasper_resampler::minimum_safe_fill_frames(PERIOD, MAX_PPM) as u64;
            let safe_floor = min_safe + crate::config::CUSHION_DECAY_FLOOR_MARGIN_FRAMES as u64;
            let ceiling = 4096u64; // roomy — well above safe_floor
            let params = DecayParams {
                enabled: true,
                floor_frames: min_safe, // churn-by-construction: on the threshold
                step_frames: STEP,
                interval_ms: 1000,
                stability_ms: 10_000,
                cascade_guard_ppm: 400.0,
            };
            let d = params.build(ceiling, PERIOD, RATE, MAX_PPM);
            assert_eq!(
                d.floor(),
                safe_floor,
                "the churny floor must be lifted to minimum_safe_fill + margin"
            );

            // A floor already comfortably above the safe floor is left untouched.
            let params = DecayParams {
                floor_frames: safe_floor + 500,
                ..params
            };
            let d = params.build(ceiling, PERIOD, RATE, MAX_PPM);
            assert_eq!(d.floor(), safe_floor + 500, "a safe floor is not perturbed");

            // A pathological geometry where even the safe floor exceeds the
            // ceiling degrades to "no decay" (floor capped at ceiling), never a
            // floor above the ceiling.
            let tiny_ceiling = min_safe; // below safe_floor
            let d = params.build(tiny_ceiling, PERIOD, RATE, MAX_PPM);
            assert_eq!(d.floor(), tiny_ceiling, "floor never exceeds the ceiling");
        }

        #[test]
        fn frozen_reason_codes_roundtrip() {
            // The wire codes are a contract: append, never renumber.
            assert_eq!(DecayFrozenReason::code(None), 0);
            assert_eq!(DecayFrozenReason::code_str(0), "");
            for r in [
                DecayFrozenReason::Unlocked,
                DecayFrozenReason::NotL0,
                DecayFrozenReason::Cascade,
                DecayFrozenReason::Warmup,
                DecayFrozenReason::AtFloor,
            ] {
                let code = DecayFrozenReason::code(Some(r));
                assert_ne!(code, 0);
                assert_eq!(DecayFrozenReason::code_str(code), r.as_expected_str());
            }
        }

        impl DecayFrozenReason {
            fn as_expected_str(self) -> &'static str {
                match self {
                    DecayFrozenReason::Unlocked => "unlocked",
                    DecayFrozenReason::NotL0 => "not_l0",
                    DecayFrozenReason::Cascade => "cascade",
                    DecayFrozenReason::Warmup => "warmup",
                    DecayFrozenReason::AtFloor => "at_floor",
                }
            }
        }

        #[test]
        fn ms_to_periods_converts_at_lane_geometry() {
            // 1000 ms at 48k / 256 ≈ 187.5 → 187 periods.
            assert_eq!(DecayParams::ms_to_periods(1000, 256, 48_000), 187);
            // 10_000 ms → 1875 periods.
            assert_eq!(DecayParams::ms_to_periods(10_000, 256, 48_000), 1875);
            // Tiny ms still yields >= 1 period.
            assert_eq!(DecayParams::ms_to_periods(1, 256, 48_000), 1);
        }

        #[test]
        fn prime_at_floor_seeds_the_floor_and_holds_it_across_pre_lock_periods() {
            // The persisted-compliance prime: seed at the floor, and the
            // not-yet-locked prime periods must HOLD the floor rather than snap up
            // to the ceiling (a bare snap-back would defeat the prime before the
            // first lock).
            let mut d = build();
            d.prime_at_floor();
            assert_eq!(d.held(), FLOOR, "primed directly at the floor");
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::AtFloor));
            // Ticks while STILL unlocked keep the floor (the whole point — try_lock
            // then seats shallow).
            for _ in 0..500 {
                assert_eq!(
                    d.tick(DecaySignals {
                        locked: false,
                        dll_l0_locked: false,
                        commanded_ppm_abs: 0.0,
                    }),
                    FLOOR,
                    "pre-lock prime periods hold the floor, do not snap to ceiling"
                );
            }
            // Once locked, the held target STAYS at the floor throughout (the
            // latency win — it never rises). The reason passes through Warmup while
            // the post-lock stability window re-confirms l0, then settles at
            // AtFloor. The load-bearing invariant is `held == FLOOR` the whole way.
            for _ in 0..STABILITY + 10 {
                assert_eq!(
                    d.tick(locked_l0(0.0)),
                    FLOOR,
                    "primed-at-floor held target never rises after lock"
                );
            }
            assert_eq!(
                d.frozen_reason(),
                Some(DecayFrozenReason::AtFloor),
                "past the post-lock stability window it re-confirms AtFloor"
            );
            assert!(!d.active());
        }

        #[test]
        fn prime_at_floor_then_post_lock_unlock_snaps_back_to_ceiling() {
            // After the primed lane has LOCKED, a subsequent unlock is a real
            // discontinuity: the prime latch is spent, so it snaps back to the
            // ceiling (a re-acquisition must start deep, not at the floor).
            let mut d = build();
            d.prime_at_floor();
            // Lock at the floor.
            assert_eq!(d.tick(locked_l0(0.0)), FLOOR);
            // Now lose lock: snaps to the ceiling (NOT held at the floor).
            let h = d.tick(DecaySignals {
                locked: false,
                dll_l0_locked: true,
                commanded_ppm_abs: 0.0,
            });
            assert_eq!(h, CEIL, "post-lock unlock snaps back to ceiling");
            assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::Unlocked));
        }

        #[test]
        fn prime_at_floor_is_a_noop_when_decay_disabled() {
            // A disabled machine pins the ceiling; priming must not lower it.
            let mut d = CushionDecay::new(false, CEIL, FLOOR, STEP, INTERVAL, STABILITY, 400.0);
            d.prime_at_floor();
            assert_eq!(d.held(), CEIL, "disabled decay never leaves the ceiling");
            for _ in 0..1000 {
                assert_eq!(d.tick(locked_l0(0.0)), CEIL);
            }
        }

        #[test]
        fn prime_at_floor_underfill_before_lock_snaps_to_ceiling_via_snap_back() {
            // If the prime attempt underfill-unlocks before ever locking, the
            // resampler calls snap_back directly (not via tick's !locked branch),
            // which clears the prime latch → re-acquire deep. Emulate that here.
            let mut d = build();
            d.prime_at_floor();
            assert_eq!(d.held(), FLOOR);
            // A snap_back (what unlock_for_underfill calls) clears the prime.
            d.snap_back(DecayFrozenReason::Unlocked);
            assert_eq!(d.held(), CEIL);
            // A following unlocked tick must NOT re-seat the floor (latch cleared).
            let h = d.tick(DecaySignals {
                locked: false,
                dll_l0_locked: false,
                commanded_ppm_abs: 0.0,
            });
            assert_eq!(h, CEIL, "after snap_back the prime latch is spent");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
        // No input yet: render is silence, lane reports 0 real frames.
        assert_eq!(r.render_period(&mut out), 0);
        assert!(out.iter().all(|&s| s == 0));
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 0);

        // Push enough to prefill (TARGET + cushion + headroom), then render:
        // should lock and emit a full period of real audio.
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
        // Prefill to the held-cushion threshold so the lane locks.
        r.push_input(&tone(deep_prefill()));
        for _ in 0..2000 {
            r.push_input(&block);
            r.render_period(&mut out);
        }
        assert!(r.locked, "on-rate lane must stay locked");
        // Ratio stays within the clamp and near unity (no standing offset).
        let ppm = r.controller.ratio_ppm();
        assert!(ppm.abs() <= MAX_PPM + 1e-6, "ratio within clamp: {ppm}");
    }

    #[test]
    fn observability_publishes_fill_near_target_when_locked() {
        // The STATUS "ring fill" gauge: once locked on an on-rate producer, the
        // published fill_frames must sit near the held target (proving the
        // resampler is engaged and holding the buffer), and target_fill_frames
        // must echo that actual controller setpoint.
        let mut r = build();
        let obs = r.observability();
        assert_eq!(
            obs.target_fill_frames,
            (TARGET + CUSHION) as u64,
            "target echoes the held controller setpoint"
        );
        // Before any render: fill is 0 (nothing published yet).
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
        // Before locking, the published fill must track the buffered-input depth
        // so the operator sees the lane filling toward the prefill threshold —
        // a "starting up" signal distinct from a stuck-at-zero dead lane.
        let mut r = build();
        let obs = r.observability();
        let mut out = vec![0i16; PERIOD as usize * 2];
        // Push less than the prefill threshold: stays unlocked, but fill is
        // published as the partial buffered depth (non-zero, below target).
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
        // Push far more than the ring holds in one shot: oldest-first drop,
        // counted, no panic.
        let mut r = build();
        r.push_input(&tone(RING * 2));
        assert!(
            r.overrun_frames.load(Ordering::Relaxed) > 0,
            "a ring overflow must be counted"
        );
    }

    // ---- trim_ring: keep-newest, lock-preserving standing-fill trim -------

    /// Drive the lane to a DEEP cursor-relative fill (a standing head-start well
    /// above the held target), then `trim_ring` and assert: lock survives, no
    /// unlock/relock happened, the published fill snapped to the held target,
    /// and the newest audio is what remains (the cursor kept the recent frames).
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

        // Slam a big burst in so the cursor-relative fill sits far above the
        // held target (simulates the on-device 1919-vs-512 standing head-start).
        r.push_input(&tone(4000));
        let fill_before = r.ring.write_frame() as f64 - r.next_input_frame;
        let held = r.hold_fill_frames() as f64;
        assert!(
            fill_before > held + PERIOD as f64,
            "precondition: fill {fill_before} must be well above held target {held}"
        );
        let write_before = r.ring.write_frame();

        let dropped = r.trim_ring();

        // Frames were dropped, and the post-trim cursor-relative fill is exactly
        // the held target — the newest `target` frames are kept.
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
        // write_frame is untouched: the newest audio is preserved, only the
        // oldest head-start was skipped.
        assert_eq!(r.ring.write_frame(), write_before);
        // Lock state and loop are intact — no reset, no unlock, no relock.
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
        // Published fill reflects the drop immediately.
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
        // Feed on-rate and keep rendering: every period must be a full real
        // period (never silence), proving the lane stayed locked through the
        // trim and reads the retained window.
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
        // On-rate lane holds at/near target; only trim if there is genuine
        // excess. If the DLL happens to sit a hair above target, a trim of that
        // tiny excess is still a no-op-ish; assert the strict boundary instead.
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
        // After reset, silent until re-prefilled.
        assert_eq!(r.render_period(&mut out), 0);
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

        // Starve immediately after the first real period. This is still the
        // acquisition window, so an underfill must NOT clear the buffered input:
        // keeping it lets real hardware burst fill continue priming instead of
        // throwing away progress and lock/unlock cycling forever.
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

        // First prove the lane was truly stable for the same duration used as
        // the acquisition grace window. After that, underfill is a hard
        // discontinuity boundary, so stale pre-pause samples must not survive
        // into the next acquisition.
        let block = tone(PERIOD as usize);
        for _ in 0..r.max_prime_periods {
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

        // A partial refill after the pause is not enough to lock using stale
        // tail; the lane must prime from fresh input only.
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

    /// WARM-UP FIX, part 1: the resampler primes the ring to `TARGET + cushion`
    /// (the deep prefill) BEFORE it produces any real output. A ring that has
    /// only reached the OLD threshold (`TARGET + radius`, no cushion) must still
    /// be priming — silent — proving the first output waits for the deeper fill.
    #[test]
    fn primes_to_target_plus_cushion_before_first_output() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];

        // Fill to just past the OLD (no-cushion) prefill but below the new deep
        // prefill: must still be priming (no lock, silence, 0 real frames).
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

        // Top up past the deep prefill: now it locks and emits real audio, and
        // the cursor is seated at the deep (target+cushion) fill.
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

    /// WARM-UP FIX, part 2 (the headline): a cold start (EMPTY ring) fed STEADY
    /// on-rate input emits ZERO silence after the initial prime, with NO
    /// lock→silence→relock thrash. This is the regression that pins the
    /// ~27k-silence / ~62-relock cold-start glitch the on-device counters
    /// surfaced. With the held cushion the lane locks once and holds.
    #[test]
    fn coldstart_steady_input_emits_zero_silence_after_prime() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        let period = PERIOD as usize;

        // Drive the DAC-paced loop from an empty ring: each render pushes one
        // on-rate period THEN renders. Count silence emitted AFTER the lane has
        // locked (the prime's leading silence is expected and fine).
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
                // Once locked on a steady on-rate producer, every subsequent
                // render must be a full real period — never a silence frame.
                if i > lock_i {
                    assert_eq!(
                        n, period,
                        "post-lock render {i} fell back to silence (warm-up thrash)"
                    );
                }
            }
        }
        assert!(locked_at.is_some(), "must lock on a steady producer");
        // It locked exactly once and never unlocked — no thrash.
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

    /// WARM-UP FIX, part 3: a slow-but-real producer (delivers JUST under one
    /// period per render for a while) must NOT wedge forever in prime-silence —
    /// the bounded prime falls through and locks at whatever safe depth exists.
    #[test]
    fn slow_producer_falls_through_and_locks_within_the_prime_bound() {
        // Use a runtime-like cushion so the fallback threshold sits below the
        // deep prefill. The compact test cushion locks via the deep path first,
        // which is fine for ordinary tests but would not exercise fallback.
        // A tiny rate keeps max_prime_periods small and the test fast: at
        // 4800 Hz / 256 period, max_prime_periods = 18.
        let mut r = LaneResampler::new(
            2,
            PERIOD,
            4_800,
            TARGET,
            1536,
            MAX_PPM,
            RING,
            DecayParams::disabled(),
        )
        .unwrap();
        let max_prime = r.max_prime_periods;
        assert!(max_prime >= 1);
        let mut out = vec![0i16; PERIOD as usize * 2];

        // Feed enough for the bounded-prime fallback, but never enough for the
        // full cushion: below the deep prefill, above the USB-burst runway.
        let buffered = r.fallthrough_prefill_frames();
        assert!(
            buffered < r.startup_prefill_frames(),
            "below the deep prefill"
        );
        r.push_input(&tone(buffered));

        // Render up to the prime bound + 1: the lane must lock by then via the
        // fall-through path (not stay silent forever waiting for the cushion).
        let mut locked = false;
        for _ in 0..(max_prime + 2) {
            r.render_period(&mut out);
            if r.locked {
                locked = true;
                break;
            }
        }
        assert!(
            locked,
            "a slow-but-real producer must lock via the bounded-prime fall-through"
        );
    }

    /// OVERRUN FIX: a burst larger than the ring's headroom (capacity − target)
    /// overruns a tight ring but is fully ABSORBED by a larger one. This pins
    /// the residual `overrun_frames` / usbsink `dropped_full` the on-device
    /// counters showed (input bursts spiking the ring above capacity). The
    /// LATENCY setpoint (`target_fill_frames`) is identical in both — only the
    /// burst headroom (`ring_frames`) differs.
    #[test]
    fn larger_ring_absorbs_a_burst_a_tight_ring_overruns() {
        // Tight ring: just past the construction minimum (no real burst room).
        let tight = TARGET + CUSHION + PERIOD as usize + RADIUS_FRAMES as usize + 1;
        // Roomy ring: lots of headroom above the target setpoint.
        let roomy = 16_384usize;
        // A burst that exceeds the tight ring's headroom in one push (a big
        // catch-up read after a host stall).
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
        // Both lock at the same target.
        let mut out = vec![0i16; PERIOD as usize * 2];
        tight_r.push_input(&tone(deep_prefill() + 64));
        roomy_r.push_input(&tone(deep_prefill() + 64));
        tight_r.render_period(&mut out);
        roomy_r.render_period(&mut out);
        assert_eq!(tight_r.target_fill_frames, roomy_r.target_fill_frames);

        // Slam the burst into both.
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
    /// (base target plus a small margin); interval/stability are tiny so tests
    /// run fast (interval 1 period, stability 3 periods).
    fn build_with_decay() -> LaneResampler {
        let params = DecayParams {
            enabled: true,
            floor_frames: (TARGET + 32) as u64,
            step_frames: 16,
            interval_ms: 1,  // → 1 period (clamped up)
            stability_ms: 1, // → 1 period (clamped up)
            cascade_guard_ppm: 400.0,
        };
        LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, RING, params)
            .expect("resampler builds with decay armed")
    }

    #[test]
    fn decay_disabled_holds_target_at_ceiling_forever() {
        // With decay off (the default), hold_fill_frames and the published held
        // target both equal the static ceiling, byte-for-byte today's behaviour.
        let mut r = build();
        let ceiling = (TARGET + CUSHION) as u64;
        assert_eq!(r.hold_fill_frames() as u64, ceiling);
        assert_eq!(r.held_target_frames.load(Ordering::Relaxed), ceiling);
        r.push_input(&tone(deep_prefill() + 64));
        for _ in 0..500 {
            r.push_input(&tone(PERIOD as usize));
            r.render_period(&mut vec![0i16; PERIOD as usize * 2]);
            // Ticking decay while disabled never moves the held target.
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

        // Lock the lane.
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        assert!(r.locked);

        // Feed on-rate and tick decay each period with the DLL at l0 and calm:
        // the held target must descend toward the floor.
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

    /// PR #1141's inertness claim, pinned: an ARMED cushion decay that is FROZEN
    /// by `dll_l0=false` (the evidence-(a) condition — `frozen_reason=not_l0`,
    /// held pinned at the ceiling) must behave BIT-IDENTICALLY to decay disabled
    /// over the SAME delivery trace. This is the mechanical proof that the
    /// armed-but-frozen decay path in the observed 16/115-vs-0-5 hardware run did
    /// not amplify (or cause) the unlock churn — the churn is a property of the
    /// static held target and the delivery pattern, not the decay code.
    ///
    /// The trace has TWO regimes, both load-bearing for the pin:
    ///
    /// 1. An initial COALESCING-CHURN window (every 8th period stalls) that DOES
    ///    produce unlocks — this pins that a NotL0-branch mutant which touched
    ///    lock / silence / output accounting would diverge, and keeps the
    ///    identity non-vacuous (`disabled` really churns).
    /// 2. A long CLEAN LOCKED TAIL, delivered on time with `dll_l0=false`
    ///    throughout — the lane stays locked, so `stable_periods` accrues past
    ///    the ~1875-period warm-up window and the decay's step interval elapses.
    ///    This is the ONLY regime where the NotL0 freeze does mechanical work: in
    ///    the shipped code the freeze holds `held` at the ceiling; delete the
    ///    freeze and the armed run decays `held` down over the tail, diverging
    ///    the `held` field. The churn window alone can NOT catch that — every
    ///    unlock resets `stable_periods`, so decay could never step there
    ///    regardless of the NotL0 branch (the mutant-passes hole the tail closes).
    ///
    /// The comparison folds a running FNV checksum of every rendered `out`
    /// period, so "bit-identical" is a claim about output PCM, not just the five
    /// aggregate counters (both runs are deterministic — no RNG, no clock).
    #[test]
    fn armed_frozen_decay_is_bit_identical_to_disabled_over_the_same_trace() {
        // The exact churny LAB geometry (base target 256 + one-period cushion =
        // 512 held), NOT the module TARGET (512). Period 256, min_safe 274: the
        // DLL holds the pre-render fill at 512, so a single fully-withheld
        // delivery period drops it to 512 - 256 = 256 (below min_safe 274) →
        // underfill-unlock → immediate re-lock next period. This is the observed
        // churn cycle. (The production default held=2560 could never dip that far
        // on one stall — that is why it is immune.)
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
                step_frames: 16,
                interval_ms: 1000,
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
            // Faithful delivery model (the mixer's per-period order + the gadget's
            // coalescing shape): the host produces one period of frames every
            // render period, but delivery to the ring is GATED during a stall
            // window — frames accumulate and flush in one burst when the stall
            // ends (the max_avail≈2×period signature). The render still consumes a
            // period each step, so during a stall the cursor-relative fill drops.
            // A stall long enough to drop the post-render fill below min_safe (274)
            // unlocks; the immediate re-lock the next period is the churn cycle.
            // Deterministic (no RNG / clock) so both runs replay byte-identically.
            let mut phase = 0usize;
            let mut pending = 0usize; // host-produced but not yet delivered
                                      // First fully prefill + lock on a clean burst, then run the churn
                                      // regime. Deliver the deep prefill up front so both runs lock once.
            r.push_input(&tone_at(phase, CHURNY_TARGET + PERIOD as usize + 64));
            phase += CHURNY_TARGET + PERIOD as usize + 64;
            r.render_period(&mut out);
            absorb(&out);
            r.tick_decay(false, 0.0);
            // Regime 1 (i < CHURN_PERIODS): the host produces exactly one period
            // per interval and it is delivered ON TIME (fill held tight at the
            // setpoint) EXCEPT on an isolated stall period, where delivery is
            // withheld (fill dips one period below the setpoint → below min_safe →
            // unlock) and flushed the next period (immediate re-lock). Every 8th
            // period stalls; the 7 between keep the fill tight so each stall
            // reliably dips it. This is the isolated-coalescing shape.
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
                // The frozen condition from evidence (a): dll_l0 = false on every
                // tick, so an armed decay must SNAP BACK to the ceiling (never
                // lower). The freeze fires in both regimes, but only regime 2's
                // long locked run lets `stable_periods` reach warm-up, so that is
                // where deleting it would let `held` decay — the load-bearing case.
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
        // Sanity: the trace really did churn (else the identity is vacuous), AND
        // the armed run stayed frozen at the ceiling through the whole tail (if it
        // had decayed, `held` would be below the ceiling and the assert above
        // would already have fired — this re-states the ceiling explicitly).
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
        // The regression that matters: after decay lowers the held target, an
        // underfill unlock must snap it back so the re-lock's startup prefill
        // targets the FULL cushion (not the shallow decayed depth), avoiding
        // relock chatter.
        let mut r = build_with_decay();
        let ceiling = (TARGET + CUSHION) as u64;
        let floor = (TARGET + 32) as u64;
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        let block = tone(PERIOD as usize);
        // Prove stable for the acquisition grace window, then decay down.
        for _ in 0..r.max_prime_periods {
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

    // ---- PER-SESSION prime-at-floor (PR #1146 → per-session): the session-end
    // snap-back honours the LIVE persisted proof --------------------------------

    const CEIL: u64 = (TARGET + CUSHION) as u64;
    const FLOOR: u64 = (TARGET + 32) as u64;

    /// Inject a live-proof observability handle (mirrors what the mixer's
    /// `set_compliance_observability` does with the boot-time load) so
    /// `live_proof_present()` returns `flag_present`. Returns the shared handle so
    /// the test can flip `flag_present` mid-lifetime exactly as the mixer's
    /// `on_written` / `on_revoked` do.
    fn arm_compliance(
        r: &mut LaneResampler,
        flag_present: bool,
    ) -> crate::host_compliance::HostComplianceObservability {
        let obs = crate::host_compliance::HostComplianceObservability::new(flag_present, 0, 0);
        r.set_compliance_observability(obs.clone_handles());
        obs
    }

    /// Drive a freshly-built armed-decay resampler through a full session A: lock,
    /// hold the acquisition grace, then decay all the way to the floor under
    /// sustained lock + l0 + calm. Leaves the lane LOCKED at the floor. Shared by
    /// the per-session tests so each starts from the exact "session A settled at the
    /// floor" state the hardware evidence describes.
    fn settle_to_floor(r: &mut LaneResampler, out: &mut [i16]) {
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(r.render_period(out), PERIOD as usize, "session A locks");
        let block = tone(PERIOD as usize);
        // Acquisition grace so the underfill path treats later starvation as a real
        // session boundary, not an acquisition underfill.
        for _ in 0..r.max_prime_periods {
            r.push_input(&block);
            r.render_period(out);
            r.tick_decay(true, 0.0);
        }
        for _ in 0..5000 {
            r.push_input(&block);
            r.render_period(out);
            r.tick_decay(true, 0.0);
            if r.hold_fill_frames() as u64 == FLOOR {
                break;
            }
        }
        assert_eq!(
            r.hold_fill_frames() as u64,
            FLOOR,
            "session A must reach the floor before the session ends"
        );
    }

    /// Starve the lane until it underfill-unlocks — the natural session end when
    /// the host (Mac) stops streaming. Ticks decay each period exactly as the mixer
    /// does, so the `floor_prime_pending` latch semantics run.
    fn end_session_by_starving(r: &mut LaneResampler, out: &mut [i16]) {
        for _ in 0..64 {
            if !r.is_locked() {
                break;
            }
            r.render_period(out);
            r.tick_decay(true, 0.0);
        }
        assert!(
            !r.is_locked(),
            "starving the lane must end the session (unlock)"
        );
    }

    #[test]
    fn per_session_hardware_scenario_session_b_seats_at_floor_in_same_lane() {
        // THE HARDWARE SCENARIO (jts.local 2026-07-03), reproduced as a state
        // machine in ONE lane object: session A proves + the proof is written (the
        // mixer sets flag_present via on_written), the stream stops (underfill
        // unlock), and session B — a fresh lock in the SAME daemon/lane lifetime —
        // seats at the FLOOR from its first lock, NOT the 2560 ceiling. This is the
        // whole fix, and it is ALSO the mutation guard against "snap always goes to
        // the ceiling" (the pre-fix behaviour) — under that mutation session B would
        // seat at the ceiling and this asserts it does not.
        let mut r = build_with_decay();
        let obs = arm_compliance(&mut r, false); // no proof yet at construction
        let mut out = vec![0i16; PERIOD as usize * 2];

        // --- Session A: descend to the floor, then "write" the proof. ---
        settle_to_floor(&mut r, &mut out);
        obs.on_written(1_700_000_000); // the mixer's ProofOutcome::Write side effect

        // --- Stream stops → session A ends (underfill unlock). ---
        end_session_by_starving(&mut r, &mut out);
        // With a live proof, the session-boundary snap re-primes at the FLOOR (the
        // per-session fix), not the ceiling.
        assert_eq!(
            r.hold_fill_frames() as u64,
            FLOOR,
            "a clean session end with a live proof must re-prime the held target at the FLOOR"
        );
        // The prime targets the shallow floor depth, not the ceiling.
        assert_eq!(
            r.startup_prefill_frames(),
            FLOOR as usize + RADIUS_FRAMES as usize + 1,
            "startup prefill must target the floor, so try_lock seats shallow"
        );

        // --- Session B: a fresh stream re-locks and seats at the floor. ---
        // Prime with exactly the floor-depth prefill (+ slack) and render → lock.
        r.push_input(&tone(FLOOR as usize + RADIUS_FRAMES as usize + 1 + 64));
        assert_eq!(
            r.render_period(&mut out),
            PERIOD as usize,
            "session B locks"
        );
        assert!(r.is_locked());
        assert_eq!(
            r.hold_fill_frames() as u64,
            FLOOR,
            "session B's held target is the floor from its first lock (no re-descent)"
        );
        // The seated cursor-relative fill is the floor depth — session B pays the
        // shallow latency immediately, not the full fat cushion. Allow a small
        // interpolation-cursor slack.
        let seated_fill = r.fill_frames_gauge();
        assert!(
            seated_fill <= FLOOR + RADIUS_FRAMES as u64,
            "session B seats shallow (fill {seated_fill} near floor {FLOOR}), not at the ceiling"
        );
    }

    /// FLOOR-PRIME SEATING (Part 1, jts.local 2026-07-03). THE hardware bug: a
    /// floor-primed lock seated SHALLOW via the bounded-prime fall-through — well
    /// below the floor target — so the resampler then RAILED for seconds building
    /// the fill up to the floor (and the probe running during that railed build
    /// spuriously failed). The fix suppresses the fall-through while floor-primed:
    /// the lock must reach the full floor prefill and seat AT the floor.
    ///
    /// This test picks a geometry where the fall-through prefill sits BELOW the
    /// floor prefill (a big cushion + a high floor), so a floor-primed lock with
    /// only fall-through-depth buffered WOULD have seated shallow under the old
    /// code. It asserts the lane instead keeps priming (no shallow lock), then
    /// seats at the floor once the floor depth is buffered. It is ALSO the
    /// mutation guard: reverting the `!floor_primed` gate in `try_lock` (so the
    /// fall-through fires while primed) makes the lane lock shallow here and the
    /// assertions fail.
    #[test]
    fn floor_primed_lock_does_not_seat_shallow_via_fallthrough() {
        // Geometry: a big cushion so the floor can sit ABOVE the fall-through
        // prefill. target 512, cushion 768 (ceiling 1280), floor 1100. A low rate
        // keeps max_prime_periods small (18 at 4800/256) so the test is fast.
        const FP_TARGET: usize = 512;
        const FP_CUSHION: usize = 768;
        const FP_FLOOR: u64 = 1100;
        let params = DecayParams {
            enabled: true,
            floor_frames: FP_FLOOR,
            step_frames: 16,
            interval_ms: 1,
            stability_ms: 1,
            cascade_guard_ppm: 400.0,
        };
        let mut r = LaneResampler::new(
            2, PERIOD, 4_800, FP_TARGET, FP_CUSHION, MAX_PPM, RING, params,
        )
        .expect("resampler builds");
        let _obs = arm_compliance(&mut r, true);
        // Prime at the floor (a valid proof primed this session), so the held
        // target — and the deep prefill — are the FLOOR, and the fall-through must
        // be suppressed.
        r.prime_decay_at_floor();
        assert_eq!(r.hold_fill_frames() as u64, FP_FLOOR, "primed at the floor");
        assert!(r.is_floor_primed(), "the floor-prime latch is set pre-lock");

        let floor_prefill = r.startup_prefill_frames(); // FP_FLOOR + 16 + 1 = 1117
        let fallthrough = r.fallthrough_prefill_frames(); // 1041 for this geometry
        assert!(
            fallthrough < floor_prefill,
            "geometry must expose the fall-through-below-floor window \
             (fallthrough {fallthrough} < floor prefill {floor_prefill})"
        );
        let mut out = vec![0i16; PERIOD as usize * 2];

        // Buffer EXACTLY the fall-through depth — enough for the old shallow seat,
        // NOT enough for the floor seat. Under the old code this would lock at
        // `fill - (RADIUS+1)` (≈1024), which is BELOW the floor 1100.
        r.push_input(&tone(fallthrough));
        // Drive well past the bounded-prime bound so `prime_expired` is true.
        for _ in 0..(r.max_prime_periods + 4) {
            assert_eq!(
                r.render_period(&mut out),
                0,
                "a floor-primed lane must NOT seat shallow — it stays priming until \
                 the floor depth is buffered (the fall-through is suppressed)"
            );
            assert!(!r.is_locked());
        }

        // Now top up to the floor prefill. The lane seats AT the floor.
        r.push_input(&tone(floor_prefill - fallthrough + 32));
        assert_eq!(
            r.render_period(&mut out),
            PERIOD as usize,
            "with the floor depth buffered the lane finally locks"
        );
        assert!(r.is_locked());
        assert_eq!(
            r.hold_fill_frames() as u64,
            FP_FLOOR,
            "the held target is the floor"
        );
        let seated_fill = r.fill_frames_gauge();
        assert!(
            seated_fill >= FP_FLOOR - RADIUS_FRAMES as u64,
            "the lock seated AT the floor ({seated_fill} ≈ {FP_FLOOR}), not shallow — \
             so there is no post-lock railed fill-build"
        );
    }

    #[test]
    fn per_session_revoked_proof_then_new_session_seats_at_ceiling() {
        // A revoke mid-life (the mixer clears flag_present via on_revoked) must make
        // the very NEXT session boundary land at the CEILING: a distrusted proof
        // means descend + re-prove. Composes the #1146 revocation with the new
        // per-session snap.
        let mut r = build_with_decay();
        let obs = arm_compliance(&mut r, true); // a valid proof is live at construction
        let mut out = vec![0i16; PERIOD as usize * 2];

        // Session settles at the floor with the live proof.
        settle_to_floor(&mut r, &mut out);
        // The mixer revokes (probe fail / L2 / early unlock): snap to ceiling NOW +
        // clear the flag. `snap_decay_to_ceiling` is the unconditional escape.
        r.snap_decay_to_ceiling();
        obs.on_revoked(crate::host_compliance::RevokeReason::ProbeFail);
        assert_eq!(
            r.hold_fill_frames() as u64,
            CEIL,
            "the revoke escape snaps to the ceiling immediately"
        );

        // A subsequent session end (unlock) with the proof now REVOKED must also
        // land at the ceiling — not resurrect the floor.
        // Re-lock first (fresh session), then starve it.
        r.push_input(&tone(deep_prefill() + 64));
        assert_eq!(
            r.render_period(&mut out),
            PERIOD as usize,
            "re-lock at ceiling"
        );
        end_session_by_starving(&mut r, &mut out);
        assert_eq!(
            r.hold_fill_frames() as u64,
            CEIL,
            "with the proof revoked, the next session boundary re-primes at the CEILING"
        );
        assert_eq!(
            r.startup_prefill_frames(),
            CEIL as usize + RADIUS_FRAMES as usize + 1,
            "a revoked lane descends from the full ceiling"
        );
    }

    #[test]
    fn per_session_proof_deleted_mid_lifetime_then_new_session_seats_at_ceiling() {
        // If the proof is (re)deleted mid-lifetime, the next snap-back must reflect
        // it. The daemon is the sole writer of the proof lifecycle, so the tracker
        // state (`flag_present`) mirrors the on-disk truth: clearing it — the in-
        // daemon effect of a delete — makes the next session boundary land at the
        // ceiling. (This asserts the snap reads the tracker, per the design.)
        let mut r = build_with_decay();
        let obs = arm_compliance(&mut r, true);
        let mut out = vec![0i16; PERIOD as usize * 2];

        settle_to_floor(&mut r, &mut out);
        // Proof deleted mid-lifetime → flag_present cleared (the daemon mirrors the
        // disk state it owns).
        obs.flag_present.store(false, Ordering::Relaxed);

        end_session_by_starving(&mut r, &mut out);
        assert_eq!(
            r.hold_fill_frames() as u64,
            CEIL,
            "a proof deleted mid-lifetime must make the next session boundary re-prime at the ceiling"
        );
    }

    #[test]
    fn per_session_no_proof_session_end_seats_at_ceiling_mutation_guard() {
        // MUTATION GUARD against "snap always goes to the FLOOR": with a compliance
        // handle present but NO live proof (flag_present=false), a clean session end
        // must re-prime at the CEILING (today's cold-start behaviour). Paired with
        // the hardware-scenario test (which fails if snap always → ceiling), the two
        // pin the snap destination to the live-proof signal from both sides.
        let mut r = build_with_decay();
        let _obs = arm_compliance(&mut r, false); // handle present, no proof
        let mut out = vec![0i16; PERIOD as usize * 2];

        settle_to_floor(&mut r, &mut out);
        end_session_by_starving(&mut r, &mut out);
        assert_eq!(
            r.hold_fill_frames() as u64,
            CEIL,
            "with no live proof the session boundary must re-prime at the CEILING, not the floor"
        );
        assert_eq!(
            r.startup_prefill_frames(),
            CEIL as usize + RADIUS_FRAMES as usize + 1
        );
    }

    #[test]
    fn per_session_reset_path_also_honours_the_live_proof() {
        // The explicit `reset()` (mixer idle / device-loss / xrun recovery) is the
        // other session-boundary path, and it honours the proof the same way as the
        // underfill unlock: floor with a live proof, ceiling without. Guards that the
        // fix covers BOTH snap sites, not just the underfill one.
        let mut r = build_with_decay();
        let obs = arm_compliance(&mut r, true);
        let mut out = vec![0i16; PERIOD as usize * 2];

        settle_to_floor(&mut r, &mut out);
        r.reset();
        assert_eq!(
            r.hold_fill_frames() as u64,
            FLOOR,
            "reset() with a live proof re-primes at the floor"
        );

        // Flip the proof off and reset again → ceiling.
        obs.flag_present.store(false, Ordering::Relaxed);
        // Re-lock then reset so the machine is in a decayed state again.
        r.push_input(&tone(FLOOR as usize + RADIUS_FRAMES as usize + 1 + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        r.reset();
        assert_eq!(
            r.hold_fill_frames() as u64,
            CEIL,
            "reset() with no live proof re-primes at the ceiling"
        );
    }
}
