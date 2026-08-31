// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Per-lane WAKE FADE-IN: a short raised-cosine ramp over the first onset a
//! lane contributes after a long stretch of digital silence (issue #3443).
//!
//! A sender that resumes content mid-waveform hands the lane a hard step —
//! captured on jts3 as `[0, 0, 0, 0, -605, +1625, -1846, +1878]` at an AirPlay
//! session start, identical across three reconnects. Nothing else in the chain
//! fades in, so the step reaches the crossover, which rings on it: an audible
//! pop on the tweeter. Shaping the onset at the per-lane read fixes every
//! renderer lane at once (AirPlay, Spotify, Bluetooth, the USB gadget) because
//! they all converge on one period buffer before the sum.
//!
//! ## Why tracking and application are two calls
//!
//! Silence is a property of what a lane CAPTURES; the pop is a property of what
//! reaches the SUM. Those are not the same periods. mux arbitrates at 1 Hz
//! (`jasper/mux.py` `POLL_INTERVAL_SEC`), so a lane can wake and play for up to
//! a second before its `SELECT` lands — and a window spent on periods the
//! selection gate discards would leave the summed audio starting on the very
//! step this module exists to remove.
//!
//! So [`LaneWakeRamp::observe`] runs BEFORE the selection gate over the
//! captured period (read-only: the RMS meter downstream of it must keep
//! reporting a de-selected lane's TRUE level), and it only ARMS the window.
//! [`LaneWakeRamp::apply`] runs AFTER the gate and shapes the first periods that
//! actually enter the sum. A ramp therefore waits, however long mux takes.
//!
//! Out of scope: a mux switch between two ALREADY-playing sources still
//! hard-cuts. That is pre-existing behaviour and a different defect — this
//! module only ever shapes a lane that was silent.
//!
//! The TTS/cue path enters post-sum via `TtsMixer` and never passes here.

use std::f64::consts::PI;

use super::CHANNELS;
use crate::config::MEASUREMENT_LANE;

/// Contiguous digital-zero input that must precede an onset before the onset is
/// treated as a session start rather than as music.
///
/// The threshold exists to protect legitimate content: quiet passages, track
/// gaps, and the 0.5 s toggle bursts used by probe/test stimuli must NEVER be
/// softened. 1.5 s is comfortably above the longest of those and far below the
/// silence a real session start follows (a lane sits at digital zero from the
/// moment its renderer opens until the sender pushes audio).
///
/// Accepted edge: a rest of TRUE digital zero longer than this inside lossless
/// content gets its re-entry shaped by 10 ms. Deliberate — that is also exactly
/// what a hard step into a cold crossover sounds like.
const ARM_SILENCE_MS: u64 = 1_500;

/// Length of the raised-cosine fade applied to the onset. Long enough to keep
/// the step's energy out of the crossover's ringing band, short enough that
/// first audio is not perceptibly delayed.
const RAMP_MS: u64 = 10;

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

/// Where this lane's window is in its life cycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RampState {
    /// No window owed. Silence is being counted toward the arm threshold.
    Idle,
    /// Armed, and an onset was seen at `onset_frame` of the captured period —
    /// but no period carrying it has reached the sum yet. Waits indefinitely,
    /// which is the whole point: mux may be up to a second behind.
    Pending { onset_frame: u32 },
    /// In flight, `done` frames of the window applied so far.
    Running { done: u32 },
}

/// One lane's wake-ramp state. [`Self::observe`] advances it once per render
/// period; [`Self::apply`] consumes it on the periods that reach the sum.
pub(super) struct LaneWakeRamp {
    /// [`ARM_SILENCE_MS`] at the live sample rate.
    arm_frames: u64,
    /// [`RAMP_MS`] at the live sample rate; at least 1 so the window is total.
    ramp_frames: u32,
    /// Contiguous frames of digital zero seen so far. Counted in WALL-CLOCK
    /// periods, not captured frames: a lane that reads nothing still renders a
    /// silent period.
    zero_frames: u64,
    state: RampState,
    /// Whether the period `observe` last saw held any content. `apply` reads it
    /// so a silent period can never consume window (the two calls are one
    /// period's pair — see [`Self::apply`]).
    period_had_content: bool,
    /// False on [`MEASUREMENT_LANE`], where both calls are no-ops.
    enabled: bool,
}

impl LaneWakeRamp {
    pub(super) fn for_lane(label: &str, sample_rate: u32) -> Self {
        let rate = sample_rate.max(1) as u64;
        let arm_frames = rate * ARM_SILENCE_MS / 1000;
        Self {
            arm_frames,
            ramp_frames: (rate * RAMP_MS / 1000).max(1) as u32,
            // ARMED AT BOOT. fan-in restarts into a stream that is already
            // running (a deploy, or the unit's Restart=), and the first period
            // it reads is then mid-waveform at full amplitude — the same step
            // as a session start. Starting the count satisfied costs one
            // assignment and covers both that and cold-boot first audio.
            zero_frames: arm_frames,
            state: RampState::Idle,
            period_had_content: false,
            enabled: label != MEASUREMENT_LANE,
        }
    }

    /// TRACK one render period, BEFORE the selection gate. Read-only.
    ///
    /// `period_frames` is the mixer's period, which is what a silent period is
    /// worth in wall clock even when the lane captured nothing (`period` is then
    /// empty).
    ///
    /// Returns `true` on the period a window is ARMED, so the caller can log it.
    pub(super) fn observe<S: WakeSample>(&mut self, period: &[S], period_frames: u32) -> bool {
        if !self.enabled {
            return false;
        }
        let first_sample = period.iter().position(|s| !s.is_silent());
        self.period_had_content = first_sample.is_some();
        let Some(first_sample) = first_sample else {
            self.zero_frames = self.zero_frames.saturating_add(period_frames as u64);
            // Silence long enough to re-arm cancels an unfinished window: what
            // resumes is a NEW session start and is owed the whole ramp, not
            // whatever was left of the last one.
            if self.zero_frames >= self.arm_frames {
                self.state = RampState::Idle;
            }
            return false;
        };
        let armed = self.zero_frames >= self.arm_frames;
        self.zero_frames = 0;
        if !armed || self.state != RampState::Idle {
            return false;
        }
        // Both channels of the onset frame take the same gain, so the ramp
        // cannot shift the stereo image.
        self.state = RampState::Pending {
            onset_frame: (first_sample / (CHANNELS as usize)) as u32,
        };
        true
    }

    /// APPLY the window to a period that is entering the sum, AFTER the
    /// selection gate. Pairs with the [`Self::observe`] call for the same
    /// period — a silent period holds the window rather than eating it, which is
    /// what makes a renderer stall mid-ramp resume softly instead of popping.
    pub(super) fn apply<S: WakeSample>(&mut self, period: &mut [S]) {
        if !self.enabled || !self.period_had_content {
            return;
        }
        self.state = match self.state {
            RampState::Idle => RampState::Idle,
            // The onset's own period: start the window at the first non-zero
            // frame. If mux held this lane out, that offset belongs to a period
            // that never reached the sum and the step the sum sees is at this
            // period's own boundary — which is where the window then starts.
            RampState::Pending { onset_frame } => self.shape(period, onset_frame, 0),
            // A window that outran its period continues at this period's first
            // frame. It advances by the frames this period actually carries, not
            // by wall clock, so a short period stretches the ramp slightly in
            // time. Deliberate: the window shapes the lane's own samples, and
            // the next full period finishes it.
            RampState::Running { done } => self.shape(period, 0, done),
        };
    }

    /// Multiply `period` by the raised-cosine window from `start_frame` on,
    /// resuming at `done` frames already applied. Returns where the window now
    /// stands.
    fn shape<S: WakeSample>(&self, period: &mut [S], start_frame: u32, done: u32) -> RampState {
        let n = self.ramp_frames;
        let mut pos = done;
        for frame in period
            .chunks_exact_mut(CHANNELS as usize)
            .skip(start_frame as usize)
        {
            if pos >= n {
                return RampState::Idle;
            }
            let gain = 0.5 * (1.0 - (PI * (pos as f64) / (n as f64)).cos());
            for sample in frame.iter_mut() {
                *sample = sample.scaled(gain);
            }
            pos += 1;
        }
        if pos < n {
            RampState::Running { done: pos }
        } else {
            RampState::Idle
        }
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

    /// One period on a lane that CONTRIBUTES this period: tracked, then shaped.
    fn contributing<S: WakeSample>(ramp: &mut LaneWakeRamp, period: &mut [S]) -> bool {
        let armed = ramp.observe(period, PERIOD);
        ramp.apply(period);
        armed
    }

    /// One period on a lane mux is holding OUT of the sum: tracked, never shaped.
    fn deselected<S: WakeSample>(ramp: &mut LaneWakeRamp, period: &[S]) -> bool {
        ramp.observe(period, PERIOD)
    }

    /// Drive `frames` of digital zero through a contributing lane.
    fn drive_silence(ramp: &mut LaneWakeRamp, frames: u32) {
        let mut silent = [0i16; SAMPLES];
        for _ in 0..(frames / PERIOD) {
            assert!(!contributing(ramp, &mut silent[..]));
            assert!(silent.iter().all(|s| *s == 0));
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

    /// The gain the window carries at `frame`, as the test's own expectation.
    fn window_gain(frame: usize) -> f64 {
        0.5 * (1.0 - (PI * (frame as f64) / (RAMP_FRAMES as f64)).cos())
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
            contributing(&mut ramp, &mut first[..]),
            "the onset after 2 s of silence must arm a window"
        );
        let mut second = [level; SAMPLES];
        assert!(!contributing(&mut ramp, &mut second[..]));
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
        // Spend the boot arming on a wake, so the gap is the only silence.
        let mut wake = [i16::MAX; SAMPLES];
        assert!(contributing(&mut ramp, &mut wake[..]));
        for _ in 0..4 {
            let mut audio = [i16::MAX; SAMPLES];
            contributing(&mut ramp, &mut audio[..]);
        }

        drive_silence(&mut ramp, RATE / 2);
        let mut onset = [i16::MAX; SAMPLES];
        assert!(
            !contributing(&mut ramp, &mut onset[..]),
            "a 0.5 s gap must not arm the window"
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
        // Spend the boot arming, and let its window run out, first; after that,
        // four seconds of unbroken content must never re-arm.
        let mut wake = source.clone();
        assert!(contributing(&mut ramp, &mut wake[..]));
        for _ in 0..(RAMP_FRAMES / (PERIOD as usize) + 1) {
            let mut settle = source.clone();
            contributing(&mut ramp, &mut settle[..]);
        }
        for _ in 0..(4 * RATE / PERIOD) {
            let mut period = source.clone();
            assert!(!contributing(&mut ramp, &mut period[..]));
            assert_eq!(period, source, "continuous audio must not be shaped");
        }
    }

    /// Pin 4: the measurement lane is exempt — a stimulus reaches the sum
    /// exactly as its generator produced it, however long the lane was silent.
    #[test]
    fn measurement_lane_stimulus_is_never_shaped() {
        let mut ramp = LaneWakeRamp::for_lane(MEASUREMENT_LANE, RATE);
        drive_silence(&mut ramp, 4 * RATE);
        let mut stimulus = [i16::MAX; SAMPLES];
        assert!(!contributing(&mut ramp, &mut stimulus[..]));
        assert!(stimulus.iter().all(|s| *s == i16::MAX));
    }

    /// Pin 5 (the mux race): a lane that wakes while mux still has it out of the
    /// sum keeps its window owed, and spends it on the first period that
    /// actually contributes — a full second of arbitration later.
    #[test]
    fn window_waits_for_the_selection_gate() {
        let mut ramp = LaneWakeRamp::for_lane("airplay", RATE);
        drive_silence(&mut ramp, 2 * RATE);

        // The onset, and a second of playing, all discarded by the gate.
        let onset = [i16::MAX; SAMPLES];
        assert!(deselected(&mut ramp, &onset[..]), "arming is pre-gate");
        for _ in 0..(RATE / PERIOD) {
            assert!(!deselected(&mut ramp, &onset[..]));
        }

        // SELECT lands. The first contributing period must be shaped, not
        // handed to the sum as a step.
        let mut selected = [i16::MAX; SAMPLES];
        contributing(&mut ramp, &mut selected[..]);
        assert!(
            selected[0].is_silent(),
            "the first period to reach the sum must start at the bottom of the window"
        );
        let probe = 100;
        let expected = (f64::from(i16::MAX) * window_gain(probe)).round() as i16;
        assert_eq!(selected[probe * (CHANNELS as usize)], expected);
        // The window is longer than a period, so nothing in this one reaches
        // full scale: the sum never sees the step.
        assert!(selected.iter().all(|s| *s < i16::MAX));
    }

    /// Pin 6 (renderer stall): silence mid-window HOLDS the position rather than
    /// eating it, so resumed audio still rises from where the window stopped —
    /// and silence long enough to re-arm resets the window entirely.
    #[test]
    fn silence_mid_window_holds_then_re_arms() {
        let mut ramp = LaneWakeRamp::for_lane("airplay", RATE);
        let mut onset = [i16::MAX; SAMPLES];
        assert!(contributing(&mut ramp, &mut onset[..]));
        let held = PERIOD as usize; // one period of the window is spent

        // A stalled renderer: one wholly silent period, well under the arm
        // threshold.
        let mut stalled = [0i16; SAMPLES];
        contributing(&mut ramp, &mut stalled[..]);

        // Resume: the window continues from where it was held, NOT from unity.
        let mut resumed = [i16::MAX; SAMPLES];
        contributing(&mut ramp, &mut resumed[..]);
        let expected = (f64::from(i16::MAX) * window_gain(held)).round() as i16;
        assert_eq!(
            resumed[0], expected,
            "the window must resume at its held position"
        );

        // A stall past the arm threshold instead abandons the window: what comes
        // back is a new session start and is owed the whole ramp.
        drive_silence(&mut ramp, 2 * RATE);
        let mut restart = [i16::MAX; SAMPLES];
        assert!(contributing(&mut ramp, &mut restart[..]));
        assert!(restart[0].is_silent(), "a re-armed window starts over");
    }

    /// Pin 7 (daemon restart): a lane is armed the moment it is constructed, so
    /// fan-in re-entering a stream that never stopped shapes its first period
    /// instead of stepping into it.
    #[test]
    fn a_fresh_lane_is_armed_at_boot() {
        let mut ramp = LaneWakeRamp::for_lane("airplay", RATE);
        let mut mid_stream = [i16::MAX; SAMPLES];
        assert!(contributing(&mut ramp, &mut mid_stream[..]));
        assert!(mid_stream[0].is_silent());
    }
}
