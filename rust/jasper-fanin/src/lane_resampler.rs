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

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use jasper_resampler::{AudioRing, RateController, SincTable, RADIUS_FRAMES};

/// Observability counters for one armed lane resampler, cloned into the STATUS
/// snapshot. All `0` while the resampler is disabled (no instance exists).
#[derive(Clone)]
pub struct LaneResamplerObservability {
    /// Whether a resampler is armed on this lane (1) or not (0). A plain bool
    /// would do, but the atomic keeps the STATUS read lock-free and uniform
    /// with the rest of the per-input counters.
    pub armed: bool,
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
    /// The configured target fill the controller holds the ring at (static for
    /// the lane's life). Paired with `fill_frames` so STATUS shows current vs.
    /// target without the reader having to know the config.
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
    /// Target buffered frames the controller holds the ring at (the small fixed
    /// fill that replaces the catch-up sawtooth).
    target_fill_frames: usize,
    /// Output ppm safety bound (also drives the minimum-safe-fill margin).
    max_adjust_ppm: f64,
    /// Fractional read cursor in the ring's monotonic frame space.
    next_input_frame: f64,
    locked: bool,
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
}

impl LaneResampler {
    /// Construct a resampler for `channels` interleaved channels at
    /// `period_frames` per render, holding the ring at `target_fill_frames` and
    /// bounding pitch warp to `±max_adjust_ppm`.
    ///
    /// `ring_frames` is the input buffer depth: it MUST exceed
    /// `target_fill_frames` plus one render period plus the kernel radius, or a
    /// healthy steady state would overrun. Returns an error string (rather than
    /// a typed error) so the caller can log-and-fall-back without a new error
    /// enum — a construction failure here must degrade to "no resampler", never
    /// crash the daemon.
    pub fn new(
        channels: usize,
        period_frames: u32,
        sample_rate: u32,
        target_fill_frames: usize,
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
        let min_ring = target_fill_frames + period_frames + radius + 1;
        if ring_frames < min_ring {
            return Err(format!(
                "lane resampler ring_frames={ring_frames} too small; need >= {min_ring} \
                 (target_fill={target_fill_frames} + period={period_frames} + radius={radius} + 1)"
            ));
        }
        let ring = AudioRing::new(ring_frames, channels)
            .map_err(|e| format!("lane resampler ring: {e}"))?;
        Ok(Self {
            channels,
            period_frames,
            ring,
            sinc_table: SincTable::new(),
            controller: RateController::new(max_adjust_ppm, period_frames as u32, sample_rate),
            target_fill_frames,
            max_adjust_ppm,
            next_input_frame: 0.0,
            locked: false,
            input_frames: Arc::new(AtomicU64::new(0)),
            output_frames: Arc::new(AtomicU64::new(0)),
            silence_frames: Arc::new(AtomicU64::new(0)),
            overrun_frames: Arc::new(AtomicU64::new(0)),
            ratio_milli_ppm: Arc::new(AtomicU64::new(0)),
            lock_count: Arc::new(AtomicU64::new(0)),
            unlock_count: Arc::new(AtomicU64::new(0)),
            fill_frames: Arc::new(AtomicU64::new(0)),
        })
    }

    /// Clone the observability handles for the STATUS snapshot.
    pub fn observability(&self) -> LaneResamplerObservability {
        LaneResamplerObservability {
            armed: true,
            input_frames: Arc::clone(&self.input_frames),
            output_frames: Arc::clone(&self.output_frames),
            silence_frames: Arc::clone(&self.silence_frames),
            overrun_frames: Arc::clone(&self.overrun_frames),
            ratio_milli_ppm: Arc::clone(&self.ratio_milli_ppm),
            lock_count: Arc::clone(&self.lock_count),
            unlock_count: Arc::clone(&self.unlock_count),
            fill_frames: Arc::clone(&self.fill_frames),
            target_fill_frames: self.target_fill_frames as u64,
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
            // shows the lane filling before it locks.
            self.publish_fill(self.ring.fill_frames() as u64);
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

        let error_frames = fill - self.target_fill_frames as f64;
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
            for channel in 0..self.channels {
                out[frame * self.channels + channel] =
                    self.sinc_table
                        .interpolate(&self.ring, self.next_input_frame, channel);
            }
            self.next_input_frame += ratio;
        }

        // Free history behind the cursor, keeping the kernel's left taps.
        let keep_from = self.next_input_frame.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        self.output_frames
            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
        self.period_frames
    }

    /// Discard buffered input and re-prime on the next render (a hard
    /// discontinuity: a host pause/seek that steps the fill). The mixer calls
    /// this when the lane goes idle so a fresh play starts clean.
    pub fn reset(&mut self) {
        self.ring.clear();
        self.controller.reset();
        self.next_input_frame = 0.0;
        self.locked = false;
    }

    /// Lock once enough input has buffered to seat the cursor `target_fill`
    /// behind the write head with kernel headroom. Until then `render_period`
    /// emits silence (the lane simply hasn't started, exactly like an idle
    /// renderer's snd-aloop substream).
    fn try_lock(&mut self) {
        if self.ring.fill_frames() < self.startup_prefill_frames() {
            return;
        }
        self.next_input_frame = (self.ring.write_frame() - self.target_fill_frames as u64) as f64;
        let keep_from = self.next_input_frame.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        self.locked = true;
        self.controller.reset();
        self.lock_count.fetch_add(1, Ordering::Relaxed);
    }

    fn unlock_for_underfill(&mut self) {
        self.locked = false;
        self.unlock_count.fetch_add(1, Ordering::Relaxed);
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

    fn startup_prefill_frames(&self) -> usize {
        self.target_fill_frames + RADIUS_FRAMES as usize + 1
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
    const MAX_PPM: f64 = 500.0;
    const RING: usize = 8192;

    fn build() -> LaneResampler {
        LaneResampler::new(2, PERIOD, RATE, TARGET, MAX_PPM, RING).expect("resampler builds")
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

    #[test]
    fn rejects_undersized_ring_and_zero_dims() {
        // Ring smaller than target+period+radius+1 must be rejected, not silently
        // overrun in steady state.
        assert!(LaneResampler::new(2, PERIOD, RATE, TARGET, MAX_PPM, TARGET).is_err());
        assert!(LaneResampler::new(0, PERIOD, RATE, TARGET, MAX_PPM, RING).is_err());
        assert!(LaneResampler::new(2, 0, RATE, TARGET, MAX_PPM, RING).is_err());
        assert!(LaneResampler::new(2, PERIOD, RATE, TARGET, MAX_PPM, RING).is_ok());
    }

    #[test]
    fn silent_until_prefilled_then_locks_and_renders() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        // No input yet: render is silence, lane reports 0 real frames.
        assert_eq!(r.render_period(&mut out), 0);
        assert!(out.iter().all(|&s| s == 0));
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 0);

        // Push enough to prefill, then render: should lock and emit a full
        // period of real audio.
        r.push_input(&tone(TARGET + PERIOD as usize + 64));
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
        // Prefill.
        r.push_input(&tone(TARGET + PERIOD as usize));
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
        // published fill_frames must sit near the configured target (proving the
        // resampler is engaged and holding the buffer), and target_fill_frames
        // must echo the construction value.
        let mut r = build();
        let obs = r.observability();
        assert_eq!(
            obs.target_fill_frames, TARGET as u64,
            "target echoes construction value"
        );
        // Before any render: fill is 0 (nothing published yet).
        assert_eq!(obs.fill_frames.load(Ordering::Relaxed), 0);

        let mut out = vec![0i16; PERIOD as usize * 2];
        let block = tone(PERIOD as usize);
        r.push_input(&tone(TARGET + PERIOD as usize));
        for _ in 0..500 {
            r.push_input(&block);
            r.render_period(&mut out);
        }
        assert!(r.locked, "on-rate lane must lock");
        let fill = obs.fill_frames.load(Ordering::Relaxed);
        // Held within one period of target — the DLL drives steady-state fill
        // error to ~0; a one-period band absorbs the cursor's fractional walk.
        let target = TARGET as i64;
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
        r.push_input(&tone(TARGET + PERIOD as usize));
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

    #[test]
    fn reset_reprimes_cleanly() {
        let mut r = build();
        let mut out = vec![0i16; PERIOD as usize * 2];
        r.push_input(&tone(TARGET + PERIOD as usize + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize);
        r.reset();
        // After reset, silent until re-prefilled.
        assert_eq!(r.render_period(&mut out), 0);
        r.push_input(&tone(TARGET + PERIOD as usize + 64));
        assert_eq!(
            r.render_period(&mut out),
            PERIOD as usize,
            "re-locks after reset"
        );
        assert_eq!(r.lock_count.load(Ordering::Relaxed), 2);
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
}
