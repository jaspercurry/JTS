// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Rate-matched bridge for the post-Camilla content lane.
//!
//! The DAC remains the final graph driver. This bridge absorbs small
//! sample-rate offsets between the snd-aloop content capture side and
//! the DAC-paced output loop by keeping an explicit ring fill target and
//! nudging a precomputed windowed-sinc interpolator by a few ppm.

use anyhow::{Context, Result};
use jasper_clock::DllSnapshot;
// The windowed-sinc interpolator, the audio ring, and the rate controller now
// live in the shared `jasper-resampler` crate (extracted from here so the
// usbsink C++ binding and this daemon share one algorithm). content_bridge
// keeps its own lock / prefill / underfill / resync state machine and metrics
// below; only the reusable primitives are imported. `RADIUS_FRAMES` is aliased
// to the old local name to keep this module's references unchanged.
use jasper_resampler::RADIUS_FRAMES as SINC_RADIUS_FRAMES;
use jasper_resampler::{AudioRing, RateController, SincTable};

use crate::config::ContentBridgeConfig;
use crate::types::SAMPLE_RATE;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ContentBridgeMetrics {
    pub locked: bool,
    pub ring_capacity_frames: u64,
    pub target_fill_frames: u64,
    pub fill_frames: u64,
    pub min_fill_frames: u64,
    pub max_fill_frames: u64,
    pub ratio_ppm: f64,
    pub input_frames: u64,
    pub output_frames: u64,
    pub silence_frames: u64,
    pub underrun_frames: u64,
    pub overrun_frames: u64,
    pub resync_count: u64,
    pub reset_count: u64,
    pub ratio_clamp_count: u64,
    pub lock_count: u64,
    pub unlock_count: u64,
    /// The shared-DLL rate-diff snapshot (Inc 4): the rate controller's loop
    /// internals (ppm, error stats, bandwidth, the DLL's OWN lock/resync
    /// counters) in the one consistent telemetry shape every DLL site publishes.
    /// Distinct from the bridge-level `lock_count`/`resync_count`/`ratio_ppm`
    /// above, which count ring/cursor events, not loop events.
    pub rate_diff: DllSnapshot,
}

pub struct ContentBridge {
    config: ContentBridgeConfig,
    channels: usize,
    period_frames: usize,
    ring: AudioRing,
    sinc_table: SincTable,
    controller: RateController,
    next_input_frame: f64,
    locked: bool,
    input_frames: u64,
    output_frames: u64,
    silence_frames: u64,
    underrun_frames: u64,
    overrun_frames: u64,
    resync_count: u64,
    reset_count: u64,
    lock_count: u64,
    unlock_count: u64,
    min_fill_frames: u64,
    max_fill_frames: u64,
}

impl ContentBridge {
    pub fn new(config: ContentBridgeConfig, period_frames: u32, channels: usize) -> Result<Self> {
        if channels == 0 {
            anyhow::bail!("content bridge channel count must be > 0");
        }
        let period_frames = period_frames as usize;
        let ring = AudioRing::new(config.ring_frames as usize, channels)
            .context("creating content bridge ring")?;
        let sinc_table = SincTable::new();
        Ok(Self {
            config,
            channels,
            period_frames,
            ring,
            sinc_table,
            controller: RateController::new(
                config.max_adjust_ppm as f64,
                period_frames as u32,
                SAMPLE_RATE,
            ),
            next_input_frame: 0.0,
            locked: false,
            input_frames: 0,
            output_frames: 0,
            silence_frames: 0,
            underrun_frames: 0,
            overrun_frames: 0,
            resync_count: 0,
            reset_count: 0,
            lock_count: 0,
            unlock_count: 0,
            min_fill_frames: u64::MAX,
            max_fill_frames: 0,
        })
    }

    pub fn push_input(&mut self, samples: &[i16]) {
        let frames = samples.len() / self.channels;
        if frames == 0 {
            return;
        }
        self.input_frames += frames as u64;
        let dropped = self
            .ring
            .push_interleaved(&samples[..frames * self.channels]);
        if dropped > 0 {
            self.overrun_frames += dropped;
            if is_power_of_two(self.overrun_frames) {
                eprintln!(
                    "event=outputd.content_bridge.overrun overrun_frames={} dropped_frames={} fill_frames={} ring_frames={}",
                    self.overrun_frames,
                    dropped,
                    self.ring.fill_frames(),
                    self.ring.capacity_frames(),
                );
            }
        }
    }

    pub fn render_period(&mut self, out: &mut [i16]) -> ContentBridgeMetrics {
        let requested_frames = out.len() / self.channels;
        assert_eq!(
            requested_frames, self.period_frames,
            "content bridge output buffer must be exactly one period"
        );

        if !self.locked {
            self.try_lock();
        }
        if !self.locked {
            self.render_silence(out);
            return self.metrics();
        }

        self.resync_if_reader_was_overrun();
        let fill = self.fill_from_cursor();
        self.mark_fill(fill);
        let minimum_safe_fill = self.minimum_safe_fill_frames();
        if fill < minimum_safe_fill as f64 {
            self.unlock_for_underfill(fill, requested_frames, minimum_safe_fill, 0);
            self.render_silence(out);
            return self.metrics();
        }

        let error_frames = fill - self.config.target_fill_frames as f64;
        let prior_clamp_count = self.controller.clamp_count();
        let ratio = self.controller.next_ratio(error_frames);
        let clamp_count = self.controller.clamp_count();
        if clamp_count != prior_clamp_count && is_power_of_two(clamp_count) {
            eprintln!(
                "event=outputd.content_bridge.ratio_clamped count={} ratio_ppm={:.2} fill_frames={:.1} target_fill_frames={}",
                clamp_count,
                self.controller.ratio_ppm(),
                fill,
                self.config.target_fill_frames,
            );
        }
        let required_end = self.next_input_frame + ratio * requested_frames as f64;
        if required_end + SINC_RADIUS_FRAMES as f64 > self.ring.write_frame() as f64 {
            let missing = required_end + SINC_RADIUS_FRAMES as f64 - self.ring.write_frame() as f64;
            self.unlock_for_underfill(
                fill,
                requested_frames,
                minimum_safe_fill,
                missing.ceil().max(0.0) as u64,
            );
            self.render_silence(out);
            return self.metrics();
        }

        for frame in 0..requested_frames {
            for channel in 0..self.channels {
                out[frame * self.channels + channel] =
                    self.interpolate_channel(self.next_input_frame, channel);
            }
            self.next_input_frame += ratio;
        }

        let keep_from = self.next_input_frame.floor() as i64 - SINC_RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        self.output_frames += requested_frames as u64;
        self.metrics()
    }

    pub fn metrics(&self) -> ContentBridgeMetrics {
        let fill = if self.locked {
            self.fill_from_cursor().max(0.0).round() as u64
        } else {
            self.ring.fill_frames() as u64
        };
        ContentBridgeMetrics {
            locked: self.locked,
            ring_capacity_frames: self.ring.capacity_frames() as u64,
            target_fill_frames: self.config.target_fill_frames as u64,
            fill_frames: fill,
            min_fill_frames: if self.min_fill_frames == u64::MAX {
                fill
            } else {
                self.min_fill_frames
            },
            max_fill_frames: self.max_fill_frames.max(fill),
            ratio_ppm: self.controller.ratio_ppm(),
            input_frames: self.input_frames,
            output_frames: self.output_frames,
            silence_frames: self.silence_frames,
            underrun_frames: self.underrun_frames,
            overrun_frames: self.overrun_frames,
            resync_count: self.resync_count,
            reset_count: self.reset_count,
            ratio_clamp_count: self.controller.clamp_count(),
            lock_count: self.lock_count,
            unlock_count: self.unlock_count,
            rate_diff: self.controller.dll_snapshot(),
        }
    }

    fn try_lock(&mut self) {
        if self.ring.fill_frames() < self.startup_prefill_frames() {
            return;
        }
        self.next_input_frame =
            (self.ring.write_frame() - self.config.target_fill_frames as u64) as f64;
        let keep_from = self.next_input_frame.floor() as i64 - SINC_RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        self.locked = true;
        self.lock_count += 1;
        self.controller.reset();
        if should_log_transition(self.lock_count) {
            eprintln!(
                "event=outputd.content_bridge.locked fill_frames={} target_fill_frames={} ring_frames={} lock_count={}",
                self.ring.fill_frames(),
                self.config.target_fill_frames,
                self.ring.capacity_frames(),
                self.lock_count,
            );
        }
    }

    fn resync_if_reader_was_overrun(&mut self) {
        let read = self.ring.read_frame() as f64;
        if self.next_input_frame >= read {
            return;
        }
        let skipped = (read - self.next_input_frame).ceil() as u64;
        self.next_input_frame = read;
        self.underrun_frames += skipped;
        self.resync_count += 1;
        if should_log_transition(self.resync_count) {
            eprintln!(
                "event=outputd.content_bridge.resync reason=reader_overrun skipped_frames={} resync_count={}",
                skipped,
                self.resync_count,
            );
        }
    }

    pub fn reset_after_discontinuity(&mut self, reason: &str) {
        let fill = self.ring.fill_frames();
        if self.locked {
            self.unlock_count += 1;
        }
        self.ring.clear();
        self.controller.reset();
        self.next_input_frame = 0.0;
        self.locked = false;
        self.reset_count += 1;
        self.min_fill_frames = u64::MAX;
        self.max_fill_frames = 0;
        if should_log_transition(self.reset_count) {
            eprintln!(
                "event=outputd.content_bridge.reset reason={} prior_fill_frames={} reset_count={}",
                reason, fill, self.reset_count,
            );
        }
    }

    fn unlock_for_underfill(
        &mut self,
        fill: f64,
        requested_frames: usize,
        minimum_fill_frames: usize,
        missing_frames: u64,
    ) {
        self.unlock_count += 1;
        self.locked = false;
        self.underrun_frames += requested_frames as u64;
        if should_log_transition(self.unlock_count) {
            eprintln!(
                "event=outputd.content_bridge.unlocked reason=underfill fill_frames={:.1} minimum_fill_frames={} missing_frames={} unlock_count={}",
                fill,
                minimum_fill_frames,
                missing_frames,
                self.unlock_count,
            );
        }
    }

    fn render_silence(&mut self, out: &mut [i16]) {
        out.fill(0);
        let frames = (out.len() / self.channels) as u64;
        self.output_frames += frames;
        self.silence_frames += frames;
    }

    fn fill_from_cursor(&self) -> f64 {
        self.ring.write_frame() as f64 - self.next_input_frame
    }

    fn mark_fill(&mut self, fill: f64) {
        let fill = fill.max(0.0).round() as u64;
        self.min_fill_frames = self.min_fill_frames.min(fill);
        self.max_fill_frames = self.max_fill_frames.max(fill);
    }

    fn minimum_safe_fill_frames(&self) -> usize {
        jasper_resampler::minimum_safe_fill_frames(
            self.period_frames as u32,
            self.config.max_adjust_ppm as f64,
        )
    }

    fn startup_prefill_frames(&self) -> usize {
        self.config.target_fill_frames as usize + SINC_RADIUS_FRAMES as usize + 1
    }

    fn interpolate_channel(&self, pos: f64, channel: usize) -> i16 {
        // Delegates to the shared windowed-sinc kernel; the table + ring are the
        // jasper-resampler primitives this module now composes.
        self.sinc_table.interpolate(&self.ring, pos, channel)
    }
}

fn is_power_of_two(value: u64) -> bool {
    value != 0 && (value & (value - 1)) == 0
}

fn should_log_transition(count: u64) -> bool {
    count <= 3 || is_power_of_two(count)
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::config::{
        ContentBridgeConfig, DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM,
        DEFAULT_CONTENT_BRIDGE_RING_FRAMES, DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES,
    };

    fn bridge_config() -> ContentBridgeConfig {
        ContentBridgeConfig {
            ring_frames: DEFAULT_CONTENT_BRIDGE_RING_FRAMES,
            target_fill_frames: DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES,
            max_adjust_ppm: DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM,
        }
    }

    fn silent_frames(frames: usize) -> Vec<i16> {
        vec![0; frames * 2]
    }

    #[test]
    fn waits_for_target_prefill_before_locking() {
        let mut bridge = ContentBridge::new(bridge_config(), 1024, 2).unwrap();
        let mut out = silent_frames(1024);
        bridge.push_input(&silent_frames(2048));
        let metrics = bridge.render_period(&mut out);
        assert!(!metrics.locked);
        assert_eq!(metrics.silence_frames, 1024);

        bridge.push_input(&silent_frames(2048 + SINC_RADIUS_FRAMES as usize + 1));
        let metrics = bridge.render_period(&mut out);
        assert!(metrics.locked);
        assert_eq!(metrics.lock_count, 1);
    }

    #[test]
    fn faster_source_settles_with_positive_ratio_without_overrun() {
        let mut bridge = ContentBridge::new(bridge_config(), 1024, 2).unwrap();
        bridge.push_input(&silent_frames(
            DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES as usize + SINC_RADIUS_FRAMES as usize + 1,
        ));
        let mut out = silent_frames(1024);
        let mut carry = 0.0f64;

        for _ in 0..12_000 {
            carry += 1024.0 * 1.0001;
            let frames = carry.floor() as usize;
            carry -= frames as f64;
            bridge.push_input(&silent_frames(frames));
            bridge.render_period(&mut out);
        }

        let metrics = bridge.metrics();
        assert!(metrics.locked);
        assert!(metrics.ratio_ppm > 0.0);
        assert_eq!(metrics.overrun_frames, 0);
        assert!(metrics.fill_frames < DEFAULT_CONTENT_BRIDGE_RING_FRAMES as u64);
    }

    /// The DLL win over the old proportional+integral controller (Inc 3): under
    /// a constant rate offset the loop settles to a STEADY operating point and
    /// stays there — it does not keep drifting (a runaway) or ring (oscillate),
    /// and its ratio matches the source offset. The settled `ratio_ppm` is the
    /// observable the old PI loop could not hold without a standing error.
    #[test]
    fn constant_offset_converges_to_a_steady_ratio() {
        let mut bridge = ContentBridge::new(bridge_config(), 1024, 2).unwrap();
        bridge.push_input(&silent_frames(
            DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES as usize + SINC_RADIUS_FRAMES as usize + 1,
        ));
        let mut out = silent_frames(1024);
        let mut carry = 0.0f64;
        let feed = |bridge: &mut ContentBridge, carry: &mut f64, out: &mut [i16]| {
            *carry += 1024.0 * 1.0001; // steady +100 ppm source
            let frames = carry.floor() as usize;
            *carry -= frames as f64;
            bridge.push_input(&silent_frames(frames));
            bridge.render_period(out);
        };
        // Warm up to lock.
        for _ in 0..15_000 {
            feed(&mut bridge, &mut carry, &mut out);
        }
        let ratio_mid = bridge.metrics().ratio_ppm;
        let fill_mid = bridge.metrics().fill_frames as i64;
        // Run a settled window.
        for _ in 0..15_000 {
            feed(&mut bridge, &mut carry, &mut out);
        }
        let metrics = bridge.metrics();
        assert!(metrics.locked);
        // Steady: the ratio barely moves between the two late checkpoints (no
        // drift, no ringing) and the fill is not running away.
        assert!(
            (metrics.ratio_ppm - ratio_mid).abs() < 1.0,
            "ratio must be steady at lock: {ratio_mid} -> {}",
            metrics.ratio_ppm
        );
        assert!(
            (metrics.fill_frames as i64 - fill_mid).abs() < 64,
            "fill must hold steady (no runaway): {fill_mid} -> {}",
            metrics.fill_frames
        );
        // And the ratio compensates the +100 ppm source (reader reads faster).
        assert!(
            metrics.ratio_ppm > 50.0 && metrics.ratio_ppm < 150.0,
            "ratio should track ~+100 ppm, got {}",
            metrics.ratio_ppm
        );
    }

    /// Inc 3 transient: a brief fill excursion that stays WITHIN the ring's
    /// safe bounds must be RIDDEN — the shared DLL's slew clamp keeps the loop
    /// locked (no unlock) and does not hard-jump (no resync) on a momentary
    /// wobble, then re-settles. This is distinct from the overrun/underfill
    /// tests, which cross the hard thresholds on purpose; here the failure mode
    /// guarded against is an over-sensitive resync/unlock firing on normal
    /// source jitter.
    #[test]
    fn transient_fill_excursion_is_ridden_without_unlock_or_resync() {
        let mut bridge = ContentBridge::new(bridge_config(), 1024, 2).unwrap();
        bridge.push_input(&silent_frames(
            DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES as usize + SINC_RADIUS_FRAMES as usize + 1,
        ));
        let mut out = silent_frames(1024);
        let mut carry = 0.0f64;
        let feed = |bridge: &mut ContentBridge, carry: &mut f64, out: &mut [i16], rate: f64| {
            *carry += 1024.0 * rate;
            let frames = carry.floor() as usize;
            *carry -= frames as f64;
            bridge.push_input(&silent_frames(frames));
            bridge.render_period(out);
        };
        // Lock on a nominal (rate == 1.0) source.
        for _ in 0..15_000 {
            feed(&mut bridge, &mut carry, &mut out, 1.0);
        }
        assert!(bridge.metrics().locked, "precondition: the loop is locked");
        let resyncs_before = bridge.metrics().resync_count;
        let unlocks_before = bridge.metrics().unlock_count;

        // A brief +1% excursion (~100 periods): pushes fill up transiently but
        // stays well under the ring, then returns to nominal. A transient the
        // loop must ride, NOT a hard overrun.
        for _ in 0..100 {
            feed(&mut bridge, &mut carry, &mut out, 1.01);
            assert!(
                (bridge.metrics().fill_frames as u64) < DEFAULT_CONTENT_BRIDGE_RING_FRAMES as u64,
                "the excursion must stay within the ring (else it is an overrun, not a transient)"
            );
        }
        // Recover to nominal and re-settle.
        for _ in 0..15_000 {
            feed(&mut bridge, &mut carry, &mut out, 1.0);
        }
        let metrics = bridge.metrics();
        assert!(
            metrics.locked,
            "the loop must stay/return locked through the transient: {metrics:?}"
        );
        assert_eq!(
            metrics.resync_count, resyncs_before,
            "a within-bounds transient must NOT resync: {} -> {}",
            resyncs_before, metrics.resync_count
        );
        assert_eq!(
            metrics.unlock_count, unlocks_before,
            "a within-bounds transient must NOT unlock: {} -> {}",
            unlocks_before, metrics.unlock_count
        );
    }

    #[test]
    fn slower_source_settles_with_negative_ratio_without_underflow() {
        let mut bridge = ContentBridge::new(bridge_config(), 1024, 2).unwrap();
        bridge.push_input(&silent_frames(
            DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES as usize + SINC_RADIUS_FRAMES as usize + 1,
        ));
        let mut out = silent_frames(1024);
        let mut carry = 0.0f64;

        for _ in 0..12_000 {
            carry += 1024.0 * 0.9999;
            let frames = carry.floor() as usize;
            carry -= frames as f64;
            bridge.push_input(&silent_frames(frames));
            bridge.render_period(&mut out);
        }

        let metrics = bridge.metrics();
        assert!(metrics.locked);
        assert!(metrics.ratio_ppm < 0.0);
        assert_eq!(metrics.unlock_count, 0);
    }

    #[test]
    fn ring_overrun_drops_oldest_and_resyncs_reader() {
        let mut bridge = ContentBridge::new(
            ContentBridgeConfig {
                ring_frames: 4096,
                target_fill_frames: 2048,
                max_adjust_ppm: 500,
            },
            1024,
            2,
        )
        .unwrap();
        bridge.push_input(&silent_frames(4096));
        let mut out = silent_frames(1024);
        bridge.render_period(&mut out);
        bridge.push_input(&silent_frames(8192));
        bridge.render_period(&mut out);
        let metrics = bridge.metrics();
        assert!(metrics.overrun_frames > 0);
        assert!(metrics.resync_count > 0);
    }

    #[test]
    fn reset_after_discontinuity_clears_ring_and_requires_relock() {
        let mut bridge = ContentBridge::new(bridge_config(), 1024, 2).unwrap();
        bridge.push_input(&silent_frames(
            DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES as usize + SINC_RADIUS_FRAMES as usize + 1,
        ));
        let mut out = silent_frames(1024);
        let metrics = bridge.render_period(&mut out);
        assert!(metrics.locked);

        bridge.reset_after_discontinuity("test");
        let metrics = bridge.render_period(&mut out);
        assert!(!metrics.locked);
        assert_eq!(metrics.reset_count, 1);
        assert!(metrics.silence_frames >= 1024);
    }

    #[test]
    fn underfill_unlocks_before_rendering_missing_future_samples() {
        let mut bridge = ContentBridge::new(bridge_config(), 1024, 2).unwrap();
        bridge.push_input(&silent_frames(
            DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES as usize + SINC_RADIUS_FRAMES as usize + 1,
        ));
        let mut out = vec![123; 1024 * 2];
        let metrics = bridge.render_period(&mut out);
        assert!(metrics.locked);

        let unsafe_fill = bridge.minimum_safe_fill_frames() - 1;
        bridge.next_input_frame = bridge.ring.write_frame() as f64 - unsafe_fill as f64;
        let metrics = bridge.render_period(&mut out);

        assert!(!metrics.locked);
        assert_eq!(metrics.unlock_count, 1);
        assert!(metrics.underrun_frames >= 1024);
        assert!(out.iter().all(|sample| *sample == 0));
    }

    #[test]
    fn minimum_safe_fill_uses_shared_resampler_contract() {
        for (period_frames, max_adjust_ppm) in [(256, 1), (480, 500), (1024, 5000)] {
            let mut config = bridge_config();
            config.max_adjust_ppm = max_adjust_ppm;
            let bridge = ContentBridge::new(config, period_frames, 2).unwrap();
            let shared_floor =
                jasper_resampler::minimum_safe_fill_frames(period_frames, max_adjust_ppm as f64);

            assert_eq!(bridge.minimum_safe_fill_frames(), shared_floor);
        }

        let bridge = ContentBridge::new(bridge_config(), 1024, 2).unwrap();
        assert_eq!(bridge.minimum_safe_fill_frames(), 1042);
    }
}
