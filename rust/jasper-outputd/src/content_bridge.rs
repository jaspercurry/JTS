//! Rate-matched bridge for the post-Camilla content lane.
//!
//! The DAC remains the final graph driver. This bridge absorbs small
//! sample-rate offsets between the snd-aloop content capture side and
//! the DAC-paced output loop by keeping an explicit ring fill target and
//! nudging a precomputed windowed-sinc interpolator by a few ppm.

use anyhow::{Context, Result};

use crate::config::ContentBridgeConfig;

const SINC_RADIUS_FRAMES: i64 = 16;
const SINC_TAPS: usize = (SINC_RADIUS_FRAMES as usize) * 2 + 1;
const SINC_PHASES: usize = 2048;
const SINC_CUTOFF: f64 = 0.97;
const PROPORTIONAL_PPM_PER_FRAME: f64 = 0.02;
const INTEGRAL_PPM_PER_FRAME_PERIOD: f64 = 0.00005;

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
}

pub struct ContentBridge {
    config: ContentBridgeConfig,
    channels: usize,
    period_frames: usize,
    ring: AudioRing,
    sinc_table: Vec<[f64; SINC_TAPS]>,
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
    pub fn new(
        config: ContentBridgeConfig,
        period_frames: u32,
        channels: usize,
    ) -> Result<Self> {
        if channels == 0 {
            anyhow::bail!("content bridge channel count must be > 0");
        }
        let period_frames = period_frames as usize;
        let ring = AudioRing::new(config.ring_frames as usize, channels)
            .context("creating content bridge ring")?;
        let sinc_table = build_sinc_table();
        Ok(Self {
            config,
            channels,
            period_frames,
            ring,
            sinc_table,
            controller: RateController::new(config.max_adjust_ppm as f64),
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
        let dropped = self.ring.push_interleaved(&samples[..frames * self.channels]);
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
                reason,
                fill,
                self.reset_count,
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
        let max_ratio = 1.0 + self.config.max_adjust_ppm as f64 / 1_000_000.0;
        (self.period_frames as f64 * max_ratio).ceil() as usize
            + SINC_RADIUS_FRAMES as usize
            + 1
    }

    fn startup_prefill_frames(&self) -> usize {
        self.config.target_fill_frames as usize + SINC_RADIUS_FRAMES as usize + 1
    }

    fn interpolate_channel(&self, pos: f64, channel: usize) -> i16 {
        let center = pos.floor() as i64;
        let frac = pos - center as f64;
        let phase = ((frac * SINC_PHASES as f64).floor() as usize).min(SINC_PHASES - 1);
        let coeffs = &self.sinc_table[phase];
        let mut acc = 0.0f64;
        for tap in 0..SINC_TAPS {
            let offset = tap as i64 - SINC_RADIUS_FRAMES;
            let frame = center + offset;
            acc += self.ring.sample(frame, channel) as f64 * coeffs[tap];
        }
        clamp_i16(acc)
    }
}

struct AudioRing {
    data: Vec<i16>,
    channels: usize,
    capacity_frames: usize,
    read_frame: u64,
    write_frame: u64,
}

impl AudioRing {
    fn new(capacity_frames: usize, channels: usize) -> Result<Self> {
        if capacity_frames == 0 {
            anyhow::bail!("content bridge ring capacity must be > 0");
        }
        let samples = capacity_frames
            .checked_mul(channels)
            .context("content bridge ring sample capacity overflow")?;
        Ok(Self {
            data: vec![0; samples],
            channels,
            capacity_frames,
            read_frame: 0,
            write_frame: 0,
        })
    }

    fn capacity_frames(&self) -> usize {
        self.capacity_frames
    }

    fn fill_frames(&self) -> usize {
        (self.write_frame - self.read_frame) as usize
    }

    fn read_frame(&self) -> u64 {
        self.read_frame
    }

    fn write_frame(&self) -> u64 {
        self.write_frame
    }

    fn push_interleaved(&mut self, samples: &[i16]) -> u64 {
        let frames = samples.len() / self.channels;
        let mut dropped = 0u64;
        for frame in 0..frames {
            if self.fill_frames() == self.capacity_frames {
                self.read_frame += 1;
                dropped += 1;
            }
            let dst = (self.write_frame as usize % self.capacity_frames) * self.channels;
            let src = frame * self.channels;
            self.data[dst..dst + self.channels]
                .copy_from_slice(&samples[src..src + self.channels]);
            self.write_frame += 1;
        }
        dropped
    }

    fn clear(&mut self) {
        self.read_frame = self.write_frame;
    }

    fn drop_before(&mut self, frame: i64) {
        if frame <= 0 {
            return;
        }
        let frame = frame as u64;
        if frame > self.read_frame {
            self.read_frame = frame.min(self.write_frame);
        }
    }

    fn sample(&self, frame: i64, channel: usize) -> i16 {
        if frame < 0 {
            return 0;
        }
        let frame = frame as u64;
        if frame < self.read_frame || frame >= self.write_frame {
            return 0;
        }
        let idx = (frame as usize % self.capacity_frames) * self.channels + channel;
        self.data[idx]
    }
}

struct RateController {
    max_adjust_ppm: f64,
    integral_error: f64,
    ratio_ppm: f64,
    clamp_count: u64,
}

impl RateController {
    fn new(max_adjust_ppm: f64) -> Self {
        Self {
            max_adjust_ppm,
            integral_error: 0.0,
            ratio_ppm: 0.0,
            clamp_count: 0,
        }
    }

    fn reset(&mut self) {
        self.integral_error = 0.0;
        self.ratio_ppm = 0.0;
    }

    fn next_ratio(&mut self, error_frames: f64) -> f64 {
        self.integral_error += error_frames;
        let max_integral = self.max_adjust_ppm / INTEGRAL_PPM_PER_FRAME_PERIOD;
        self.integral_error = self.integral_error.clamp(-max_integral, max_integral);

        let requested_ppm = PROPORTIONAL_PPM_PER_FRAME * error_frames
            + INTEGRAL_PPM_PER_FRAME_PERIOD * self.integral_error;
        let clamped_ppm = requested_ppm.clamp(-self.max_adjust_ppm, self.max_adjust_ppm);
        if (requested_ppm - clamped_ppm).abs() > f64::EPSILON {
            self.clamp_count += 1;
        }
        self.ratio_ppm = clamped_ppm;
        1.0 + clamped_ppm / 1_000_000.0
    }

    fn ratio_ppm(&self) -> f64 {
        self.ratio_ppm
    }

    fn clamp_count(&self) -> u64 {
        self.clamp_count
    }
}

fn sinc(x: f64) -> f64 {
    if x.abs() < 1.0e-8 {
        1.0
    } else {
        let pix = std::f64::consts::PI * x;
        pix.sin() / pix
    }
}

fn blackman_harris(x: f64) -> f64 {
    const A0: f64 = 0.35875;
    const A1: f64 = 0.48829;
    const A2: f64 = 0.14128;
    const A3: f64 = 0.01168;
    let phase = 2.0 * std::f64::consts::PI * x;
    A0 - A1 * phase.cos() + A2 * (2.0 * phase).cos() - A3 * (3.0 * phase).cos()
}

fn build_sinc_table() -> Vec<[f64; SINC_TAPS]> {
    let mut table = Vec::with_capacity(SINC_PHASES);
    for phase in 0..SINC_PHASES {
        let frac = phase as f64 / SINC_PHASES as f64;
        let mut coeffs = [0.0f64; SINC_TAPS];
        let mut norm = 0.0f64;
        for (tap, coeff) in coeffs.iter_mut().enumerate() {
            let offset = tap as i64 - SINC_RADIUS_FRAMES;
            let distance = frac - offset as f64;
            *coeff = sinc(distance * SINC_CUTOFF)
                * SINC_CUTOFF
                * blackman_harris(tap as f64 / (SINC_TAPS - 1) as f64);
            norm += *coeff;
        }
        if norm.abs() > 1.0e-9 {
            for coeff in &mut coeffs {
                *coeff /= norm;
            }
        }
        table.push(coeffs);
    }
    table
}

fn clamp_i16(value: f64) -> i16 {
    value
        .round()
        .clamp(i16::MIN as f64, i16::MAX as f64) as i16
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
}
