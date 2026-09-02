// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Per-lane fades around the mixer's sum (issue #3443). Two windows, one
//! raised-cosine shape, both on the lane's own period before it is summed:
//!
//! * the WAKE fade-in, over the first onset a lane contributes after a long
//!   stretch of digital silence, and
//! * the SELECTION fade, which carries a lane into and out of the sum when mux
//!   changes the source or a lane is muted.
//!
//! ## Wake fade-in
//!
//! A sender that resumes content mid-waveform hands the lane a hard step —
//! captured on jts3 as `[0, 0, 0, 0, -605, +1625, -1846, +1878]` at an AirPlay
//! session start, identical across three reconnects. Nothing else in the chain
//! fades in, so the step reaches the crossover, which rings on it: an audible
//! pop on the tweeter. Shaping the onset at the per-lane read fixes every
//! renderer lane at once (AirPlay, Spotify, Bluetooth, the USB gadget) because
//! they all converge on one period buffer before the sum.
//!
//! ### Why tracking and application are two calls
//!
//! Silence is a property of what a lane CAPTURES; the pop is a property of what
//! reaches the SUM. Those are not the same periods. mux arbitrates at 1 Hz
//! (`jasper/mux.py` `POLL_INTERVAL_SEC`), so a lane can wake and play for up to
//! a second before its `SELECT` lands — and a window spent on periods the
//! selection gate discards would leave the summed audio starting on the very
//! step this module exists to remove.
//!
//! So [`LaneFade::observe`] runs BEFORE the selection gate over the captured
//! period (read-only: the RMS meter downstream of it must keep reporting a
//! de-selected lane's TRUE level), and it only ARMS the window.
//! [`LaneFade::shape_period`] runs AFTER the gate and shapes the first periods
//! that actually enter the sum. A ramp therefore waits, however long mux takes.
//!
//! ## Selection fade
//!
//! The wake window shapes a lane that was SILENT. It does nothing for a lane
//! that is already playing when the sum stops taking it: mux's `SELECT` is one
//! atomic swap, so a source switch used to remove a full-amplitude waveform from
//! the sum between one sample and the next. That is the same step in the other
//! direction, and it is what a USB→AirPlay switch pops on — the USB host keeps
//! streaming, so the resampler's own shutdown de-click never arms and the lane
//! simply vanished. The mirror case is a lane that is already playing when
//! `SELECT` reaches it: it used to appear at full amplitude.
//!
//! So a lane's contribution is carried by its own window instead of by a gate:
//! it keeps being summed while the window walks down to zero, and rises from
//! zero when it comes back. Steady state (fully in, fully out) is bit-exact and
//! costs one comparison.
//!
//! Both lanes of a switch step on the same period boundary and advance in WALL
//! CLOCK, so the pair is complementary and the two gains sum to AT MOST 1.0
//! across the switch. Two lanes are briefly in the sum where one used to be, and
//! that is why it still cannot clip.
//!
//! At most, not exactly. An incoming lane that is also owed a wake window takes
//! the PRODUCT of the two, so the pair dips to 0.75 (−2.5 dB) mid-switch; and a
//! lane delivering a short or empty period holds its rise while the outgoing
//! lane keeps falling. Both are dips. Nothing pushes the sum up.
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

/// Length of both raised-cosine windows. Long enough to keep a step's energy out
/// of the crossover's ringing band, short enough that first audio is not
/// perceptibly delayed and that a source switch still reads as instant.
const RAMP_MS: u64 = 10;

/// A lane period sample, at either of the two scales a lane can carry (`i16` on
/// a narrow wire, `i32` at spine scale on a wide one). The state machine is
/// identical at both, so it is written once.
pub(super) trait FadeSample: Copy {
    fn is_silent(self) -> bool;
    /// Multiply by `gain`, which both windows keep in `0.0..=1.0` — so this only
    /// ever moves a sample toward zero and cannot clip.
    fn scaled(self, gain: f64) -> Self;
}

impl FadeSample for i16 {
    fn is_silent(self) -> bool {
        self == 0
    }
    fn scaled(self, gain: f64) -> Self {
        (self as f64 * gain).round() as Self
    }
}

impl FadeSample for i32 {
    fn is_silent(self) -> bool {
        self == 0
    }
    fn scaled(self, gain: f64) -> Self {
        (self as f64 * gain).round() as Self
    }
}

/// The raised-cosine window at `pos` of `n`: 0 at `pos == 0`, 1 at `pos == n`.
fn window_gain(pos: u32, n: u32) -> f64 {
    0.5 * (1.0 - (PI * f64::from(pos) / f64::from(n)).cos())
}

/// Where this lane's wake window is in its life cycle.
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

/// One lane's fade state. [`Self::observe`] advances the wake tracker once per
/// render period, before the selection gate; [`Self::shape_period`] applies both
/// windows to the periods that reach the sum.
pub(super) struct LaneFade {
    /// [`ARM_SILENCE_MS`] at the live sample rate.
    arm_frames: u64,
    /// [`RAMP_MS`] at the live sample rate; at least 1 so a window is total.
    ramp_frames: u32,
    /// Contiguous frames of digital zero seen so far. Counted in WALL-CLOCK
    /// periods, not captured frames: a lane that reads nothing still renders a
    /// silent period.
    zero_frames: u64,
    state: RampState,
    /// Whether the period `observe` last saw held any content. `shape_period`
    /// reads it so a silent period can never consume the wake window (the two
    /// calls are one period's pair).
    period_had_content: bool,
    /// Whether `observe` armed the wake window on THIS period. The onset offset
    /// only describes this period's own leading silence; on any later period it
    /// would skip real content, so it is only honoured here.
    armed_this_period: bool,
    /// Selection window position: 0 = fully out of the sum, `ramp_frames` =
    /// fully in. Starts out, because mux holds every lane out at startup.
    select_pos: u32,
    /// False on [`MEASUREMENT_LANE`], where every call is a no-op.
    enabled: bool,
}

impl LaneFade {
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
            armed_this_period: false,
            select_pos: 0,
            enabled: label != MEASUREMENT_LANE,
        }
    }

    /// TRACK one render period, BEFORE the selection gate. Read-only.
    ///
    /// `period_frames` is the mixer's period, which is what a silent period is
    /// worth in wall clock even when the lane captured nothing (`period` is then
    /// empty).
    ///
    /// Returns `true` on the period the wake window is ARMED, so the caller can
    /// log it.
    pub(super) fn observe<S: FadeSample>(&mut self, period: &[S], period_frames: u32) -> bool {
        if !self.enabled {
            return false;
        }
        self.armed_this_period = false;
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
        self.armed_this_period = true;
        true
    }

    /// Shape one period AFTER the selection gate and report whether the lane
    /// still reaches the sum. `contributes` is the gate's verdict; a lane that
    /// has just lost it keeps being summed until its selection window closes.
    ///
    /// Pairs with the [`Self::observe`] call for the same period.
    pub(super) fn shape_period<S: FadeSample>(
        &mut self,
        period: &mut [S],
        period_frames: u32,
        contributes: bool,
    ) -> bool {
        if !self.enabled {
            return contributes;
        }
        // Fully out and staying out: the lane contributes nothing and neither
        // window is in flight. This is every unselected lane, every period.
        if !contributes && self.select_pos == 0 {
            return false;
        }
        // Deliberately NOT gated on `contributes`: past the early return above,
        // this period REACHES THE SUM, and a wake window in flight must keep
        // shaping it. Gating here would strip the wake attenuation off the very
        // period mux drops the lane on — the sum would see the lane jump from
        // its ramp position straight back to unity, a bigger step upward than
        // the one this module was written to remove.
        self.apply_wake(period);
        self.apply_select(period, period_frames, contributes);
        true
    }

    /// The wake window, on a period that is entering the sum. A silent period
    /// holds the window rather than eating it, which is what makes a renderer
    /// stall mid-ramp resume softly instead of popping.
    fn apply_wake<S: FadeSample>(&mut self, period: &mut [S]) {
        if !self.period_had_content {
            return;
        }
        self.state = match self.state {
            RampState::Idle => RampState::Idle,
            // The onset's own period: skip its leading digital zero so the
            // window opens on the first real sample instead of spending itself
            // on silence. On any LATER period — mux held the lane out, which is
            // the normal case at 1 Hz arbitration — that offset belongs to a
            // period the sum never saw, and the step the sum sees is at this
            // period's own boundary, so the window starts there.
            RampState::Pending { onset_frame } => {
                let start = if self.armed_this_period {
                    onset_frame
                } else {
                    0
                };
                self.shape(period, start, 0)
            }
            // A window that outran its period continues at this period's first
            // frame. It advances by the frames this period actually carries, not
            // by wall clock, so a short period stretches the ramp slightly in
            // time. Deliberate: the window shapes the lane's own samples, and
            // the next full period finishes it.
            RampState::Running { done } => self.shape(period, 0, done),
        };
    }

    /// The selection window. Bit-exact in the steady state.
    ///
    /// The two directions do NOT advance the same way, and that asymmetry is the
    /// whole correctness of this function:
    ///
    /// * Fading IN advances only over frames that actually carry content into
    ///   the sum — the rule [`Self::apply_wake`] already follows. A renderer
    ///   whose ring is still empty when `SELECT` lands (a fresh AirPlay session
    ///   is exactly that) would otherwise spend the window on periods the sum
    ///   never heard, and the first period carrying real samples would enter at
    ///   unity: the step this module exists to remove.
    /// * Fading OUT advances in WALL CLOCK. A lane being dropped may deliver
    ///   short or empty periods, and counting only captured frames would leave
    ///   it lingering in the sum long past the window.
    ///
    /// A short read can therefore make the incoming lane rise more slowly than
    /// the outgoing lane falls. The two gains then sum to slightly under 1.0 —
    /// never above it, so the headroom argument holds in the safe direction.
    fn apply_select<S: FadeSample>(
        &mut self,
        period: &mut [S],
        period_frames: u32,
        contributes: bool,
    ) {
        let n = self.ramp_frames;
        if contributes && (self.select_pos == n || !self.period_had_content) {
            return;
        }
        let start = self.select_pos;
        let mut pos = start;
        for frame in period.chunks_exact_mut(CHANNELS as usize) {
            if contributes && pos == n {
                // The rest of the period passes at unity, untouched.
                break;
            }
            // Both rails are exact, and skipping the cosine at zero is worth a
            // branch: the closing period of every fade-out sits there.
            let gain = if pos == 0 { 0.0 } else { window_gain(pos, n) };
            for sample in frame.iter_mut() {
                *sample = sample.scaled(gain);
            }
            pos = self.advanced(pos, 1, contributes);
        }
        // The gain is indexed per SAMPLE above, but the window ADVANCES IN WALL
        // CLOCK. Both lanes of a switch therefore step by the same amount even
        // when they deliver different frame counts, which is what keeps them
        // complementary. Advancing on delivered frames instead lets an outgoing
        // lane that short-reads (`read_input` returns partial `readi` counts on
        // every DEFAULT-arm lane) fall slower than the incoming lane rises: the
        // two gains then sum ABOVE 1.0 and the sum clips for the length of the
        // window — the artifact this module exists to remove.
        self.select_pos = self.advanced(start, period_frames, contributes);
    }

    /// `pos` moved `frames` toward fully-in or fully-out, clamped to the window.
    fn advanced(&self, pos: u32, frames: u32, contributes: bool) -> u32 {
        if contributes {
            pos.saturating_add(frames).min(self.ramp_frames)
        } else {
            pos.saturating_sub(frames)
        }
    }

    /// Multiply `period` by the raised-cosine window from `start_frame` on,
    /// resuming at `done` frames already applied. Returns where the window now
    /// stands.
    fn shape<S: FadeSample>(&self, period: &mut [S], start_frame: u32, done: u32) -> RampState {
        let n = self.ramp_frames;
        let mut pos = done;
        for frame in period
            .chunks_exact_mut(CHANNELS as usize)
            .skip(start_frame as usize)
        {
            if pos >= n {
                return RampState::Idle;
            }
            let gain = window_gain(pos, n);
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
    fn contributing<S: FadeSample>(fade: &mut LaneFade, period: &mut [S]) -> bool {
        let armed = fade.observe(period, PERIOD);
        assert!(
            fade.shape_period(period, PERIOD, true),
            "a contributing lane always reaches the sum"
        );
        armed
    }

    /// One period on a lane mux is holding OUT of the sum: tracked, then offered
    /// to `shape_period`, which reports whether it still reaches the sum.
    fn deselected<S: FadeSample>(fade: &mut LaneFade, period: &mut [S]) -> (bool, bool) {
        let armed = fade.observe(period, PERIOD);
        let mixes = fade.shape_period(period, PERIOD, false);
        (armed, mixes)
    }

    /// Drive `frames` of digital zero through a lane, in or out of the sum.
    fn drive_silence(fade: &mut LaneFade, frames: u32, contributes: bool) {
        let mut silent = [0i16; SAMPLES];
        for _ in 0..(frames / PERIOD) {
            let armed = if contributes {
                contributing(fade, &mut silent[..])
            } else {
                deselected(fade, &mut silent[..]).0
            };
            assert!(!armed);
            assert!(silent.iter().all(|s| *s == 0));
        }
    }

    /// A lane already fully in the sum, with both windows spent — the steady
    /// state every "does this touch normal playback" pin needs.
    fn settled_lane(label: &str) -> LaneFade {
        let mut fade = LaneFade::for_lane(label, RATE);
        for _ in 0..(RAMP_FRAMES / (PERIOD as usize) + 2) {
            let mut warm = [i16::MAX; SAMPLES];
            contributing(&mut fade, &mut warm[..]);
        }
        fade
    }

    /// One period holding `lead_zero_frames` of silence then constant
    /// full-scale — the captured onset shape (silence, then content mid-period).
    fn onset_period<S: FadeSample + Default>(lead_zero_frames: usize, level: S) -> Vec<S> {
        let mut buf = vec![S::default(); SAMPLES];
        for sample in buf.iter_mut().skip(lead_zero_frames * (CHANNELS as usize)) {
            *sample = level;
        }
        buf
    }

    /// The gain a window carries at `frame`, as the test's own expectation.
    fn expected_gain(frame: usize) -> f64 {
        0.5 * (1.0 - (PI * (frame as f64) / (RAMP_FRAMES as f64)).cos())
    }

    /// Every `CHANNELS`-th sample: one channel of an interleaved period.
    fn one_channel<S: FadeSample>(period: &[S]) -> Vec<S> {
        period.iter().step_by(CHANNELS as usize).copied().collect()
    }

    /// Pin 1, at both lane scales: after the arming silence, an abrupt
    /// full-scale onset is ramped — it starts at zero, rises monotonically, and
    /// is at unity within the window.
    fn wake_onset_is_ramped<S>(level: S)
    where
        S: FadeSample + Default + PartialOrd + std::fmt::Debug,
    {
        const LEAD: usize = 4;
        let mut fade = settled_lane("airplay");
        drive_silence(&mut fade, 2 * RATE, true);

        let mut first = onset_period::<S>(LEAD, level);
        assert!(
            contributing(&mut fade, &mut first[..]),
            "the onset after 2 s of silence must arm a window"
        );
        let mut second = [level; SAMPLES];
        assert!(!contributing(&mut fade, &mut second[..]));
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
        let ramped: Vec<S> = one_channel(&out[LEAD * (CHANNELS as usize)..])
            .into_iter()
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
        let mut fade = settled_lane("airplay");
        drive_silence(&mut fade, RATE / 2, true);
        let mut onset = [i16::MAX; SAMPLES];
        assert!(
            !contributing(&mut fade, &mut onset[..]),
            "a 0.5 s gap must not arm the window"
        );
        assert!(
            onset.iter().all(|s| *s == i16::MAX),
            "onset must pass unaltered"
        );
    }

    /// Pin 3: continuous audio on a settled lane is never altered, including the
    /// zero samples every waveform crosses. Neither window may touch playback.
    #[test]
    fn continuous_audio_is_bit_exact() {
        let mut fade = settled_lane("airplay");
        let source: Vec<i16> = (0..SAMPLES)
            .map(|i| [0i16, 12_000, -12_000, i16::MIN][i % 4])
            .collect();
        for _ in 0..(4 * RATE / PERIOD) {
            let mut period = source.clone();
            assert!(!contributing(&mut fade, &mut period[..]));
            assert_eq!(period, source, "continuous audio must not be shaped");
        }
    }

    /// Pin 4: the measurement lane is exempt from BOTH windows — a stimulus
    /// reaches the sum exactly as its generator produced it however long the
    /// lane was silent, and it leaves the sum the instant mux says so.
    #[test]
    fn measurement_lane_stimulus_is_never_shaped() {
        let mut fade = LaneFade::for_lane(MEASUREMENT_LANE, RATE);
        drive_silence(&mut fade, 4 * RATE, true);
        let mut stimulus = [i16::MAX; SAMPLES];
        assert!(!contributing(&mut fade, &mut stimulus[..]));
        assert!(stimulus.iter().all(|s| *s == i16::MAX));

        let (_, mixes) = deselected(&mut fade, &mut stimulus[..]);
        assert!(!mixes, "the exempt lane is gated, not faded");
        assert!(stimulus.iter().all(|s| *s == i16::MAX));
    }

    /// Pin 5 (the mux race): a lane that wakes while mux still has it out of the
    /// sum keeps its window owed, and spends it on the first period that
    /// actually contributes — a full second of arbitration later.
    #[test]
    fn window_waits_for_the_selection_gate() {
        const LEAD: usize = 7;
        let mut fade = LaneFade::for_lane("airplay", RATE);
        drive_silence(&mut fade, 2 * RATE, false);

        // The onset, and a second of playing, all discarded by the gate.
        let mut onset = onset_period::<i16>(LEAD, i16::MAX);
        let (armed, mixes) = deselected(&mut fade, &mut onset[..]);
        assert!(armed, "arming is pre-gate");
        assert!(
            !mixes,
            "an out lane with a closed selection window is skipped"
        );
        for _ in 0..(RATE / PERIOD) {
            let mut playing = [i16::MAX; SAMPLES];
            assert!(!deselected(&mut fade, &mut playing[..]).0);
        }

        // SELECT lands. The first contributing period must be shaped from its
        // own first frame — nothing may pass at full scale ahead of the window.
        let mut selected = [i16::MAX; SAMPLES];
        contributing(&mut fade, &mut selected[..]);
        assert!(
            selected[0].is_silent(),
            "the first period to reach the sum must start at the bottom of the window"
        );
        // Both windows rise together, so nothing in this period reaches full
        // scale: the sum never sees the step.
        assert!(selected.iter().all(|s| *s < i16::MAX));
        for pair in one_channel(&selected[..]).windows(2) {
            assert!(pair[0] <= pair[1], "the opening must rise: {:?}", pair);
        }
    }

    /// Pin 6 (renderer stall): silence mid-window HOLDS the position rather than
    /// eating it, so resumed audio still rises from where the window stopped —
    /// and silence long enough to re-arm resets the window entirely.
    #[test]
    fn silence_mid_window_holds_then_re_arms() {
        let mut fade = settled_lane("airplay");
        drive_silence(&mut fade, 2 * RATE, true);
        let mut onset = [i16::MAX; SAMPLES];
        assert!(contributing(&mut fade, &mut onset[..]));
        let held = PERIOD as usize; // one period of the window is spent

        // A stalled renderer: one wholly silent period, well under the arm
        // threshold.
        let mut stalled = [0i16; SAMPLES];
        contributing(&mut fade, &mut stalled[..]);

        // Resume: the window continues from where it was held, NOT from unity.
        let mut resumed = [i16::MAX; SAMPLES];
        contributing(&mut fade, &mut resumed[..]);
        let expected = (f64::from(i16::MAX) * expected_gain(held)).round() as i16;
        assert_eq!(
            resumed[0], expected,
            "the window must resume at its held position"
        );

        // A stall past the arm threshold instead abandons the window: what comes
        // back is a new session start and is owed the whole ramp.
        drive_silence(&mut fade, 2 * RATE, true);
        let mut restart = [i16::MAX; SAMPLES];
        assert!(contributing(&mut fade, &mut restart[..]));
        assert!(restart[0].is_silent(), "a re-armed window starts over");
    }

    /// Pin 7 (daemon restart): a lane is armed the moment it is constructed, so
    /// fan-in re-entering a stream that never stopped shapes its first period
    /// instead of stepping into it.
    #[test]
    fn a_fresh_lane_is_armed_at_boot() {
        let mut fade = LaneFade::for_lane("airplay", RATE);
        let mut mid_stream = [i16::MAX; SAMPLES];
        assert!(contributing(&mut fade, &mut mid_stream[..]));
        assert!(mid_stream[0].is_silent());
    }

    /// Pin 8 (the source switch): a lane that is PLAYING when mux drops it
    /// glides to zero over the window and only then leaves the sum. It used to
    /// vanish between one sample and the next — the pop heard switching
    /// USB→AirPlay with music playing.
    #[test]
    fn a_dropped_lane_fades_out_before_it_leaves_the_sum() {
        let mut fade = settled_lane("usbsink");

        let mut out: Vec<i16> = Vec::new();
        for periods in 0.. {
            assert!(periods < 16, "the fade must terminate");
            let mut period = [i16::MAX; SAMPLES];
            if !deselected(&mut fade, &mut period[..]).1 {
                break;
            }
            out.extend_from_slice(&period);
        }

        let fell = one_channel(&out[..]);
        assert_eq!(
            fell.first().copied(),
            Some(i16::MAX),
            "the fade starts at unity — no step at the switch"
        );
        for pair in fell.windows(2) {
            assert!(pair[0] >= pair[1], "the fade must fall: {:?}", pair);
        }
        assert!(
            fell[RAMP_FRAMES..].iter().all(|s| *s == 0),
            "the lane reaches digital zero by the end of the window and stays there"
        );
        // The window closes mid-period, and the rest of that period is summed as
        // the zeros it now is; the lane is skipped from the NEXT period on.
        assert!(
            fell.len() < RAMP_FRAMES + (PERIOD as usize),
            "the lane must leave the sum within one period of closing: {}",
            fell.len()
        );
    }

    /// Pin 9 (the other half of the switch): a lane that is ALREADY playing when
    /// `SELECT` reaches it rises from zero instead of appearing at full scale.
    /// The wake window cannot cover this — the lane never went silent, so it can
    /// never re-arm, and the selection window is the only one left.
    #[test]
    fn an_already_playing_lane_fades_in_when_selected() {
        let mut fade = settled_lane("airplay");
        // mux drops the lane while it keeps streaming, and the fade-out runs to
        // completion — the source the owner switched AWAY from, still playing.
        for periods in 0.. {
            assert!(periods < 16, "the fade-out must terminate");
            let mut playing = [i16::MAX; SAMPLES];
            if !deselected(&mut fade, &mut playing[..]).1 {
                break;
            }
        }

        let mut selected = [i16::MAX; SAMPLES];
        contributing(&mut fade, &mut selected[..]);
        assert!(
            selected[0].is_silent(),
            "an already-playing lane must not appear at full scale"
        );
        let rising = one_channel(&selected[..]);
        for pair in rising.windows(2) {
            assert!(pair[0] <= pair[1], "the entry must rise: {:?}", pair);
        }
        assert!(
            rising.iter().all(|s| *s < i16::MAX),
            "the window is longer than one period"
        );
    }

    /// Pin 10: a lane that stalls to nothing while leaving the sum still closes
    /// its window, so it reaches the fully-out fast path instead of being handed
    /// to the sum forever.
    #[test]
    fn a_dropped_lane_that_reads_nothing_still_closes() {
        let mut fade = settled_lane("airplay");
        let mut empty: [i16; 0] = [];
        let mut periods = 0;
        while fade.shape_period(&mut empty[..], PERIOD, false) {
            periods += 1;
            assert!(periods < 16, "an empty-period fade must terminate");
        }
        assert!(periods > 0, "the window was open and had to close");
    }

    /// Pin 11a (the fresh-session hole): a lane whose renderer ring is still
    /// EMPTY when `SELECT` lands must HOLD its selection window, not spend it on
    /// periods the sum never heard. Spending it there opens the lane at unity on
    /// the first period that carries real samples — which is a step, and is what
    /// a first AirPlay session start looks like. The wake window cannot cover
    /// this: the gap here is far under `ARM_SILENCE_MS`.
    #[test]
    fn an_empty_ring_at_select_holds_the_selection_window() {
        let mut fade = settled_lane("airplay");
        // mux drops the lane while it streams; the fade-out runs to completion.
        for periods in 0.. {
            assert!(periods < 16, "the fade-out must terminate");
            let mut playing = [i16::MAX; SAMPLES];
            if !deselected(&mut fade, &mut playing[..]).1 {
                break;
            }
        }
        // SELECT returns, but the ring hands over nothing for a while.
        let mut empty: [i16; 0] = [];
        for _ in 0..(2 * RAMP_FRAMES / (PERIOD as usize) + 2) {
            assert!(!fade.observe(&empty[..], PERIOD));
            assert!(fade.shape_period(&mut empty[..], PERIOD, true));
        }
        // The first period that actually reaches the sum owes the whole window.
        let mut first = [i16::MAX; SAMPLES];
        contributing(&mut fade, &mut first[..]);
        assert!(
            first[0].is_silent(),
            "the window must open on the first sample the sum hears"
        );
        assert!(
            first.iter().all(|s| *s < i16::MAX),
            "the window is longer than one period"
        );
    }

    /// Pin 11b: the same hold for a period that is present but digitally SILENT
    /// — an attached renderer sitting idle. Silence reaches the sum as nothing,
    /// so it must not buy window either.
    #[test]
    fn a_silent_period_at_select_holds_the_selection_window() {
        let mut fade = settled_lane("airplay");
        for periods in 0.. {
            assert!(periods < 16, "the fade-out must terminate");
            let mut playing = [i16::MAX; SAMPLES];
            if !deselected(&mut fade, &mut playing[..]).1 {
                break;
            }
        }
        drive_silence(&mut fade, 4 * (RAMP_FRAMES as u32), true);
        let mut first = [i16::MAX; SAMPLES];
        contributing(&mut fade, &mut first[..]);
        assert!(first[0].is_silent(), "silence must not spend the window");
    }

    /// Pin 11c: a lane being DROPPED closes in wall clock, not in captured
    /// frames. A renderer handing over half-periods on its way out would
    /// otherwise linger in the sum for twice the window — and would fall more
    /// slowly than the incoming lane rises, which is the one way the pair could
    /// sum above unity.
    #[test]
    fn a_dropped_lane_closes_in_wall_clock_not_captured_frames() {
        let mut fade = settled_lane("usbsink");
        let half = SAMPLES / 2;
        let mut periods = 0;
        loop {
            // A short read: the lane captured only half a period this time.
            let mut short = [i16::MAX; SAMPLES];
            if !deselected(&mut fade, &mut short[..half]).1 {
                break;
            }
            periods += 1;
            assert!(periods < 8, "a short-read fade must still close");
        }
        let allowed = RAMP_FRAMES.div_ceil(PERIOD as usize);
        assert!(
            periods <= allowed,
            "the fade took {periods} periods of short reads; \
             wall clock allows at most {allowed}"
        );
    }

    /// Pin 11d (the real headroom pin, replacing the algebra one): drive TWO
    /// lanes through a switch — the outgoing one short-reading, which is the
    /// case that can desynchronise them — and check the summed gain never
    /// exceeds unity. Pin 11 only proves an identity about `window_gain`; this
    /// proves the stepping puts two real lanes on complementary positions.
    #[test]
    fn two_lanes_across_a_switch_never_sum_above_unity() {
        let mut outgoing = settled_lane("usbsink");
        let mut incoming = settled_lane("airplay");
        // Park the incoming lane fully out, still streaming.
        for periods in 0.. {
            assert!(periods < 16, "the fade-out must terminate");
            let mut playing = [i16::MAX; SAMPLES];
            if !deselected(&mut incoming, &mut playing[..]).1 {
                break;
            }
        }

        const LEVEL: i16 = 16_000;
        let half = SAMPLES / 2;
        for period in 0..8 {
            // The outgoing lane short-reads on the switch period itself.
            let n_out = if period == 0 { half } else { SAMPLES };
            let mut out = [LEVEL; SAMPLES];
            let mut inc = [LEVEL; SAMPLES];
            let out_mixes = deselected(&mut outgoing, &mut out[..n_out]).1;
            incoming.observe(&inc[..], PERIOD);
            let in_mixes = incoming.shape_period(&mut inc[..], PERIOD, true);
            assert!(in_mixes, "the incoming lane always reaches the sum");
            // Only the frames the outgoing lane actually delivered are summed;
            // past those it contributes nothing at all.
            for f in 0..(n_out / (CHANNELS as usize)) {
                let a = if out_mixes {
                    i32::from(out[f * (CHANNELS as usize)])
                } else {
                    0
                };
                let b = i32::from(inc[f * (CHANNELS as usize)]);
                assert!(
                    a + b <= i32::from(LEVEL) + 1,
                    "period {period} frame {f}: {a} + {b} exceeds one lane at full gain"
                );
            }
        }
    }

    /// Pin 11e (the wake window must survive the gate flipping): a lane whose
    /// wake ramp is still RUNNING when mux drops it keeps that attenuation on
    /// the period it is dropped in. Stripping it would hand the sum a jump from
    /// the ramp's position straight back to unity — a bigger step upward than
    /// the one the wake ramp exists to remove.
    #[test]
    fn a_running_wake_window_survives_being_dropped() {
        let mut fade = LaneFade::for_lane("airplay", RATE);
        drive_silence(&mut fade, 2 * RATE, true);

        // The onset arms and starts the wake window while selected.
        let mut onset = [i16::MAX; SAMPLES];
        assert!(contributing(&mut fade, &mut onset[..]));
        let last = onset[(PERIOD as usize - 1) * (CHANNELS as usize)];
        assert!(last < i16::MAX, "the window is longer than one period");

        // mux drops the lane on the very next period, mid-window. The wake
        // window keeps going (its next position), and the selection window is
        // still at unity on this period's first frame — so the sum sees the
        // ramp continue, not jump back to full scale.
        let mut dropped = [i16::MAX; SAMPLES];
        assert!(deselected(&mut fade, &mut dropped[..]).1);
        // Both windows are mid-flight at the same position, so this frame takes
        // their PRODUCT. Stripping the wake half would leave the selection half
        // alone — a jump upward, which is what this pins against.
        let g = expected_gain(PERIOD as usize);
        let expected = (f64::from(i16::MAX) * g * g).round() as i16;
        let wake_stripped = (f64::from(i16::MAX) * g).round() as i16;
        assert_eq!(
            dropped[0], expected,
            "the wake window must carry across the flip (it was at {last}; \
             stripping it would give {wake_stripped})"
        );
    }

    /// Pin 11 (headroom across a switch): the WINDOW FUNCTION is complementary —
    /// `w(p) + w(n-p) == 1`. This pins the algebra only; the stepping that makes
    /// two real lanes land on complementary positions is pinned by
    /// [`a_dropped_lane_closes_in_wall_clock_not_captured_frames`], which is the
    /// case that can break it.
    #[test]
    fn the_two_halves_of_a_switch_sum_to_unity() {
        let n = RAMP_FRAMES as u32;
        for pos in 0..=n {
            let total = window_gain(pos, n) + window_gain(n - pos, n);
            assert!(
                (total - 1.0).abs() < 1e-12,
                "gains must sum to unity at {pos}: {total}"
            );
        }
    }

    /// Pin 12 (the stale onset offset): the onset offset describes the leading
    /// silence of the period it was seen in and NOTHING else. A lane that wakes
    /// while mux has it out, is then selected, and only afterwards delivers its
    /// first non-empty period must be shaped from that period's own first frame.
    /// Skipping the stale offset instead would hand the sum that many frames of
    /// full-scale content and then step to zero gain — two discontinuities where
    /// the window intends one rise.
    #[test]
    fn a_stale_onset_offset_never_skips_real_content() {
        const LEAD: usize = 7;
        let mut fade = LaneFade::for_lane("airplay", RATE);
        drive_silence(&mut fade, 2 * RATE, false);

        // Wake while mux still has the lane out: the window is owed, and the
        // offset belongs to this period.
        let mut onset = onset_period::<i16>(LEAD, i16::MAX);
        assert!(
            deselected(&mut fade, &mut onset[..]).0,
            "arming is pre-gate"
        );

        // SELECT lands, but the renderer's ring is empty for long enough to open
        // the selection window fully. The wake window is HELD, not spent.
        drive_silence(&mut fade, 2 * (RAMP_FRAMES as u32), true);

        // First real content. Every frame of it is the window's to shape.
        let mut first = [i16::MAX; SAMPLES];
        contributing(&mut fade, &mut first[..]);
        assert!(
            first[0].is_silent(),
            "the window must open on the first sample the sum sees"
        );
        for pair in one_channel(&first[..]).windows(2) {
            assert!(pair[0] <= pair[1], "the opening must rise: {:?}", pair);
        }
    }
}
