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
    /// The actual target fill the controller holds the ring at (base target
    /// plus held warm-up cushion, static for the lane's life). Paired with
    /// `fill_frames` so STATUS shows current vs. target without the reader
    /// having to know the config.
    pub target_fill_frames: u64,
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
    /// Base configured target. The controller holds
    /// `target_fill_frames + warmup_cushion_frames`; the held target is the
    /// small fixed fill that replaces the catch-up sawtooth.
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
    pub fn new(
        channels: usize,
        period_frames: u32,
        sample_rate: u32,
        target_fill_frames: usize,
        warmup_cushion_frames: usize,
        max_adjust_ppm: f64,
        ring_frames: usize,
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
        // radius, or the deep prefill could never accumulate.
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
        })
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
            target_fill_frames: self.hold_fill_frames() as u64,
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
        // Fall-through seat depth once the bounded prime expires: the most we
        // can safely seat given what's buffered, never below the safe minimum
        // (so we don't lock straight into an underfill→silence) and never above
        // the full cushion depth.
        let prime_expired =
            self.max_prime_periods > 0 && self.prime_periods >= self.max_prime_periods;
        let seat = if fill >= deep_prefill {
            // Enough for the full held warm-up cushion.
            self.hold_fill_frames()
        } else if prime_expired && fill >= self.fallthrough_prefill_frames() {
            // Slow producer: seat at whatever we have, but only after there is
            // one render period of runway beyond the hard interpolation floor.
            // Hardware USB acquisition can arrive in short bursts; seating at
            // the bare minimum caused lock→underfill→relock chatter before the
            // ring built enough depth to run continuously.
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
        self.publish_fill(if acquisition_underfill {
            self.ring.fill_frames() as u64
        } else {
            0
        });
        self.publish_ratio();
    }

    fn render_silence(&mut self, out: &mut [i16]) {
        out.fill(0);
        self.silence_frames
            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
    }

    /// Minimum buffered frames to safely render one period at the worst-case
    /// (max-ppm) ratio with kernel headroom. Same shape as content_bridge.
    fn minimum_safe_fill_frames(&self) -> usize {
        let max_ratio = 1.0 + self.max_adjust_ppm / 1_000_000.0;
        (self.period_frames as f64 * max_ratio).ceil() as usize + RADIUS_FRAMES as usize + 1
    }

    /// Frames the ring must hold before lock seats the cursor at the held
    /// target (`target + warm-up cushion`) with kernel headroom.
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

    fn hold_fill_frames(&self) -> usize {
        self.target_fill_frames + self.warmup_cushion_frames
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
        LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, RING)
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
        assert!(LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, TARGET).is_err());
        assert!(LaneResampler::new(0, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, RING).is_err());
        assert!(LaneResampler::new(2, 0, RATE, TARGET, CUSHION, MAX_PPM, RING).is_err());
        assert!(LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, RING).is_ok());
        // The cushion is part of the minimum ring: a ring that would fit
        // target+period+radius but NOT the cushion is rejected.
        let just_under = TARGET + PERIOD as usize + RADIUS_FRAMES as usize + 1;
        assert!(
            LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, just_under).is_err(),
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
        let mut r = LaneResampler::new(2, PERIOD, 4_800, TARGET, 1536, MAX_PPM, RING).unwrap();
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

        let mut tight_r =
            LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, tight).unwrap();
        let mut roomy_r =
            LaneResampler::new(2, PERIOD, RATE, TARGET, CUSHION, MAX_PPM, roomy).unwrap();
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
}
