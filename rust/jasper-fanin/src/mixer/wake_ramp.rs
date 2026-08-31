// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Per-lane WAKE FADE-IN: a short raised-cosine ramp over the first onset a
//! lane produces after a long stretch of digital silence (issue #3443).
//!
//! A sender that resumes content mid-waveform hands the lane a hard step —
//! captured on jts3 as `[0, 0, 0, 0, -605, +1625, -1846, +1878]` at an AirPlay
//! session start, identical across three reconnects. Nothing else in the chain
//! fades in, so the step reaches the crossover, which rings on it: an audible
//! pop on the tweeter. Shaping the onset here, at the per-lane read, fixes every
//! renderer lane at once (AirPlay, Spotify, Bluetooth, the USB gadget) because
//! they all converge on one period buffer before the sum.
//!
//! This runs BEFORE the sum, so the TTS/cue path — which enters post-sum via
//! `TtsMixer` — is untouched.

use std::f64::consts::PI;

use super::CHANNELS;

/// Contiguous digital-zero input that must precede an onset before the onset is
/// treated as a session start rather than as music.
///
/// The threshold exists to protect legitimate content: quiet passages, track
/// gaps, and the 0.5 s toggle bursts used by probe/test stimuli must NEVER be
/// softened. 1.5 s is comfortably above the longest of those and far below the
/// silence a real session start follows (a lane sits at digital zero from the
/// moment its renderer opens until the sender pushes audio).
const ARM_SILENCE_MS: u64 = 1_500;

/// Length of the raised-cosine fade applied to the onset. Long enough to keep
/// the step's energy out of the crossover's ringing band, short enough that
/// first audio is not perceptibly delayed (non-negotiable 6: no silent
/// deafness).
const RAMP_MS: u64 = 10;

/// The measurement / diagnostic injection lane, which is NOT wake-ramped.
///
/// It carries stimuli, not program: sweeps, tones, and noise that the
/// measurement loop deconvolves against the signal it believes it played. Those
/// generators already taper their own onsets, so there is no pop here to fix,
/// and shaping a stimulus would silently alter the instrument. Renderer lanes
/// are the ones a sender can hand a mid-waveform step to.
const MEASUREMENT_LANE: &str = "correction";

/// A lane period sample, at either of the two scales a lane can carry (`i16` for
/// an aloop/ring lane, `i32` for the spine-scale USB DIRECT lane on a wide
/// wire). The state machine is identical at both, so it is written once.
pub(super) trait WakeSample: Copy {
    fn is_silent(self) -> bool;
    /// Multiply by `gain`, which the ramp keeps in `0.0..=1.0` — so this only
    /// ever moves a sample toward zero and cannot clip.
    fn scaled(self, gain: f64) -> Self;
}

impl WakeSample for i16 {
    fn is_silent(self) -> bool {
        self == 0
    }
    fn scaled(self, gain: f64) -> Self {
        (self as f64 * gain).round() as Self
    }
}

impl WakeSample for i32 {
    fn is_silent(self) -> bool {
        self == 0
    }
    fn scaled(self, gain: f64) -> Self {
        (self as f64 * gain).round() as Self
    }
}

/// One lane's wake-ramp state, advanced once per render period.
pub(super) struct LaneWakeRamp {
    /// [`ARM_SILENCE_MS`] at the live sample rate.
    arm_frames: u64,
    /// [`RAMP_MS`] at the live sample rate; at least 1 so the window is total.
    ramp_frames: u32,
    /// Contiguous frames of digital zero seen so far. Counted in WALL-CLOCK
    /// periods, not captured frames: a lane that reads nothing still renders a
    /// silent period.
    zero_frames: u64,
    /// Frames of the window already applied while a ramp spans more than one
    /// period; `None` when no ramp is in flight.
    ramp_done: Option<u32>,
    /// False on [`MEASUREMENT_LANE`], where `observe` is a no-op.
    enabled: bool,
}

impl LaneWakeRamp {
    pub(super) fn for_lane(label: &str, sample_rate: u32) -> Self {
        let rate = sample_rate.max(1) as u64;
        Self {
            arm_frames: rate * ARM_SILENCE_MS / 1000,
            ramp_frames: (rate * RAMP_MS / 1000).max(1) as u32,
            zero_frames: 0,
            ramp_done: None,
            enabled: label != MEASUREMENT_LANE,
        }
    }

    /// Advance one render period over this lane's interleaved period buffer,
    /// shaping it in place when a wake onset lands. `period_frames` is the
    /// mixer's period, which is what a silent period is worth in wall clock even
    /// when the lane captured nothing (`period` is then empty).
    ///
    /// Returns `true` on the period a ramp STARTS, so the caller can log it.
    pub(super) fn observe<S: WakeSample>(&mut self, period: &mut [S], period_frames: u32) -> bool {
        if !self.enabled {
            return false;
        }
        if let Some(done) = self.ramp_done {
            // A ramp that outran its period continues at this period's first
            // frame — the lane's stream is contiguous across the boundary.
            self.ramp_done = self.shape(period, 0, done);
            return false;
        }
        let Some(first_sample) = period.iter().position(|s| !s.is_silent()) else {
            self.zero_frames = self.zero_frames.saturating_add(period_frames as u64);
            return false;
        };
        let armed = self.zero_frames >= self.arm_frames;
        self.zero_frames = 0;
        if !armed {
            return false;
        }
        // Both channels of the onset frame take the same gain, so the ramp
        // cannot shift the stereo image.
        let onset_frame = (first_sample / (CHANNELS as usize)) as u32;
        self.ramp_done = self.shape(period, onset_frame, 0);
        true
    }

    /// Apply the raised-cosine window to `period` from `start_frame` on,
    /// resuming at `done` frames already applied. Returns the new in-flight
    /// position, or `None` once the window has reached unity.
    fn shape<S: WakeSample>(&self, period: &mut [S], start_frame: u32, done: u32) -> Option<u32> {
        let n = self.ramp_frames;
        let mut pos = done;
        for frame in period
            .chunks_exact_mut(CHANNELS as usize)
            .skip(start_frame as usize)
        {
            if pos >= n {
                return None;
            }
            let gain = 0.5 * (1.0 - (PI * (pos as f64) / (n as f64)).cos());
            for sample in frame.iter_mut() {
                *sample = sample.scaled(gain);
            }
            pos += 1;
        }
        (pos < n).then_some(pos)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RATE: u32 = 48_000;
    /// Deliberately SHORTER than the window so every case here also exercises
    /// the cross-period continuation.
    const PERIOD: u32 = 256;
    /// [`RAMP_MS`] at [`RATE`], spelled out so the 10 ms is pinned here rather
    /// than re-derived from the code under test.
    const RAMP_FRAMES: usize = 480;
    const SAMPLES: usize = (PERIOD as usize) * (CHANNELS as usize);

    /// Drive `frames` of digital zero through the ramp, one period at a time.
    fn drive_silence(ramp: &mut LaneWakeRamp, frames: u32) {
        let mut silent = [0i16; SAMPLES];
        for _ in 0..(frames / PERIOD) {
            assert!(!ramp.observe(&mut silent[..], PERIOD));
        }
    }

    /// One period holding `lead_zero_frames` of silence then constant
    /// full-scale — the captured onset shape (silence, then content mid-period).
    fn onset_period<S: WakeSample + Default>(lead_zero_frames: usize, level: S) -> Vec<S> {
        let mut buf = vec![S::default(); SAMPLES];
        for sample in buf.iter_mut().skip(lead_zero_frames * (CHANNELS as usize)) {
            *sample = level;
        }
        buf
    }

    /// Pin 1, at both lane scales: after the arming silence, an abrupt
    /// full-scale onset is ramped — it starts at zero, rises monotonically, and
    /// is at unity within the window.
    fn wake_onset_is_ramped<S>(level: S)
    where
        S: WakeSample + Default + PartialOrd + std::fmt::Debug,
    {
        const LEAD: usize = 4;
        let mut ramp = LaneWakeRamp::for_lane("airplay", RATE);
        drive_silence(&mut ramp, 2 * RATE);

        let mut first = onset_period::<S>(LEAD, level);
        assert!(
            ramp.observe(&mut first[..], PERIOD),
            "the onset after 2 s of silence must start a ramp"
        );
        let mut second = [level; SAMPLES];
        assert!(!ramp.observe(&mut second[..], PERIOD));
        let out: Vec<S> = first.iter().chain(second.iter()).copied().collect();

        // The silent lead is untouched, and the onset frame itself starts at the
        // bottom of the window rather than at full scale.
        assert!(out[..LEAD * (CHANNELS as usize)]
            .iter()
            .all(|s| s.is_silent()));
        assert!(
            out[LEAD * (CHANNELS as usize)].is_silent(),
            "the window starts at zero gain"
        );
        // Monotonic (non-decreasing after rounding) rise across the window, on
        // one channel — the window is per frame, so both channels match.
        let ramped: Vec<S> = out[LEAD * (CHANNELS as usize)..]
            .iter()
            .step_by(CHANNELS as usize)
            .copied()
            .take(RAMP_FRAMES + 1)
            .collect();
        for pair in ramped.windows(2) {
            assert!(pair[0] <= pair[1], "window must rise: {:?}", pair);
        }
        // Unity by the end of the window, and flat full scale after it.
        let unity_from = (LEAD + RAMP_FRAMES) * (CHANNELS as usize);
        assert!(
            out[unity_from..].iter().all(|s| *s == level),
            "content past the window is untouched"
        );
    }

    #[test]
    fn wake_onset_is_ramped_at_both_lane_scales() {
        wake_onset_is_ramped(i16::MAX);
        wake_onset_is_ramped(i32::MAX);
    }

    /// Pin 2: a sub-threshold gap (0.5 s — the probe toggle burst) never softens
    /// the content that follows it.
    #[test]
    fn half_second_gap_passes_the_onset_bit_exact() {
        let mut ramp = LaneWakeRamp::for_lane("airplay", RATE);
        // Start from an already-woken lane so the gap is the only silence.
        drive_silence(&mut ramp, 2 * RATE);
        let mut wake = vec![i16::MAX; SAMPLES];
        assert!(ramp.observe(&mut wake[..], PERIOD));
        for _ in 0..4 {
            let mut audio = vec![i16::MAX; SAMPLES];
            ramp.observe(&mut audio[..], PERIOD);
        }

        drive_silence(&mut ramp, RATE / 2);
        let mut onset = vec![i16::MAX; SAMPLES];
        assert!(
            !ramp.observe(&mut onset[..], PERIOD),
            "a 0.5 s gap must not arm the ramp"
        );
        assert!(
            onset.iter().all(|s| *s == i16::MAX),
            "onset must pass unaltered"
        );
    }

    /// Pin 3: continuous audio is never altered, including the zero samples
    /// every waveform crosses.
    #[test]
    fn continuous_audio_is_bit_exact() {
        let mut ramp = LaneWakeRamp::for_lane("airplay", RATE);
        let source: Vec<i16> = (0..SAMPLES)
            .map(|i| [0i16, 12_000, -12_000, i16::MIN][i % 4])
            .collect();
        // Four seconds — well past the arming threshold, which must never fire
        // while the lane keeps producing content.
        for _ in 0..(4 * RATE / PERIOD) {
            let mut period = source.clone();
            assert!(!ramp.observe(&mut period[..], PERIOD));
            assert_eq!(period, source, "continuous audio must not be shaped");
        }
    }

    /// Pin 4: the measurement lane is exempt — a stimulus reaches the sum
    /// exactly as its generator produced it, however long the lane was silent.
    #[test]
    fn measurement_lane_stimulus_is_never_shaped() {
        let mut ramp = LaneWakeRamp::for_lane(MEASUREMENT_LANE, RATE);
        drive_silence(&mut ramp, 4 * RATE);
        let mut stimulus = vec![i16::MAX; SAMPLES];
        assert!(!ramp.observe(&mut stimulus[..], PERIOD));
        assert!(stimulus.iter().all(|s| *s == i16::MAX));
    }
}
