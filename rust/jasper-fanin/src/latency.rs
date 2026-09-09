// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Buffer motion is separate from clock correction. 0.2% changes pitch by
// at most 3.5 cents during acquisition, without skipping any PCM frames.
pub const BUFFER_ADJUST_PPM: f64 = 2000.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecayFrozenReason {
    Unlocked,
    NotL0,
    BufferLow,
    Warmup,
    AtFloor,
    Backoff,
    Reused,
}

impl DecayFrozenReason {
    pub fn code(reason: Option<Self>) -> u64 {
        match reason {
            None => 0,
            Some(Self::Unlocked) => 1,
            Some(Self::NotL0) => 2,
            Some(Self::BufferLow) => 3,
            Some(Self::Warmup) => 4,
            Some(Self::AtFloor) => 5,
            Some(Self::Backoff) => 6,
            Some(Self::Reused) => 7,
        }
    }
    pub fn code_str(code: u64) -> &'static str {
        match code {
            1 => "unlocked",
            2 => "not_l0",
            3 => "buffer_low",
            4 => "warmup",
            5 => "at_floor",
            6 => "backoff",
            7 => "reused",
            _ => "",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct DecayParams {
    pub enabled: bool,
    pub floor_frames: u64,
    pub stability_ms: u64,
}

impl DecayParams {
    #[cfg(test)]
    pub fn disabled() -> Self {
        Self {
            enabled: false,
            floor_frames: 0,
            stability_ms: 2000,
        }
    }
    pub fn build(
        self,
        ceiling: u64,
        period_frames: u32,
        sample_rate: u32,
        max_adjust_ppm: f64,
    ) -> CushionDecay {
        let periods = |ms: u64| {
            (ms * sample_rate.max(1) as u64 / (1000 * period_frames.max(1) as u64)).max(1)
        };
        let min_safe = jasper_resampler::minimum_safe_fill_frames(
            period_frames,
            max_adjust_ppm + BUFFER_ADJUST_PPM,
        ) as u64
            + crate::config::CUSHION_DECAY_FLOOR_MARGIN_FRAMES as u64;
        let floor = self.floor_frames.max(min_safe).min(ceiling);
        CushionDecay {
            enabled: self.enabled,
            ceiling,
            floor,
            learned_floor: floor,
            held: ceiling as f64,
            period_frames: period_frames.max(1) as f64,
            stability_periods: periods(self.stability_ms),
            // A 250 ms gap exceeds the whole acquisition buffer; extra
            // buffering cannot bridge it. Treat it as an idle/resume boundary.
            pause_periods: periods(250),
            settled_periods: 0,
            stable_periods: 0,
            idle_periods: 0,
            motion_ppm: 0.0,
            refilling: false,
            connection: 0,
            failed: false,
            last_good: None,
            interrupted_at: None,
            reused: false,
            resumes: 0,
            backoffs: 0,
            frozen_reason: Some(DecayFrozenReason::Unlocked),
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct DecaySignals {
    pub locked: bool,
    pub dll_l0_locked: bool,
    pub buffer_low: bool,
}

#[derive(Debug, Clone)]
pub struct CushionDecay {
    enabled: bool,
    ceiling: u64,
    floor: u64,
    learned_floor: u64,
    held: f64,
    period_frames: f64,
    stability_periods: u64,
    pause_periods: u64,
    stable_periods: u64,
    settled_periods: u64,
    idle_periods: u64,
    motion_ppm: f64,
    refilling: bool,
    connection: u64,
    failed: bool,
    last_good: Option<u64>,
    interrupted_at: Option<u64>,
    reused: bool,
    resumes: u64,
    backoffs: u64,
    frozen_reason: Option<DecayFrozenReason>,
}

impl CushionDecay {
    pub fn held(&self) -> u64 {
        self.held.round() as u64
    }
    pub fn held_exact(&self) -> f64 {
        self.held
    }
    pub fn enabled(&self) -> bool {
        self.enabled
    }
    pub fn floor(&self) -> u64 {
        self.floor
    }
    pub fn learned_floor(&self) -> u64 {
        self.learned_floor
    }
    pub fn resumes(&self) -> u64 {
        self.resumes
    }
    pub fn backoffs(&self) -> u64 {
        self.backoffs
    }
    pub fn active(&self) -> bool {
        self.motion_ppm > 0.0
    }
    pub fn demand_ppm(&self) -> f64 {
        self.motion_ppm
    }
    pub fn refilling(&self) -> bool {
        self.refilling
    }
    pub fn frozen_reason(&self) -> Option<DecayFrozenReason> {
        self.frozen_reason
    }

    // Only a continuously observed, configured physical USB connection may
    // reuse a buffer. Capture-handle reopen is not a physical disconnect.
    pub fn context(&mut self, connection: u64, failed: bool, locked: bool) {
        if connection != self.connection || failed && !self.failed {
            self.last_good = None;
            self.interrupted_at = None;
            self.reused = false;
            self.learned_floor = self.floor;
            self.snap_back(DecayFrozenReason::NotL0);
            if !locked {
                self.held = self.ceiling as f64;
                self.refilling = false;
            }
        }
        self.connection = connection;
        self.failed = failed;
    }

    pub fn snap_back(&mut self, reason: DecayFrozenReason) {
        if !self.enabled {
            return;
        }
        self.stable_periods = 0;
        self.settled_periods = 0;
        self.motion_ppm = 0.0;
        self.frozen_reason = Some(reason);
        if reason == DecayFrozenReason::Unlocked {
            // Keep the candidate through repeated idle capture resets.
            if self.interrupted_at.is_none() {
                self.interrupted_at = Some(self.held());
                self.reused = false;
                self.idle_periods = 0;
            }
            self.held = self.ceiling as f64;
            self.refilling = false;
        } else {
            self.refilling = self.held < self.ceiling as f64;
            self.last_good = None;
            self.reused = false;
        }
    }

    // Called after rendering. Integrate only the motion actually used in that
    // period, then prepare the next period's feed-forward and target together.
    pub fn tick(&mut self, s: DecaySignals) -> u64 {
        if !self.enabled {
            return self.held();
        }
        if !s.locked {
            self.motion_ppm = 0.0;
            self.idle_periods = self.idle_periods.saturating_add(1);
            if self.idle_periods >= self.pause_periods && self.connection != 0 && !self.failed {
                if let Some(target) = self.last_good {
                    self.held = target as f64;
                    self.reused = true;
                }
            }
            self.frozen_reason = Some(DecayFrozenReason::Unlocked);
            return self.held();
        }
        self.held = (self.held - self.motion_ppm * self.period_frames / 1e6)
            .clamp(self.learned_floor as f64, self.ceiling as f64);
        if let Some(failed_target) = self.interrupted_at.take() {
            if self.reused {
                self.resumes = self.resumes.saturating_add(1);
            } else if self.idle_periods < self.pause_periods && failed_target < self.ceiling {
                self.learned_floor = (failed_target + 2 * self.period_frames as u64)
                    .max(self.learned_floor)
                    .min(self.ceiling);
                self.last_good = None;
                self.backoffs = self.backoffs.saturating_add(1);
            }
        }
        self.idle_periods = 0;
        if self.refilling {
            if self.held < self.ceiling as f64 {
                self.motion_ppm = -BUFFER_ADJUST_PPM
                    .min((self.ceiling as f64 - self.held) * 1e6 / self.period_frames);
                return self.held();
            }
            self.refilling = false;
        }
        if self.failed || (!s.dll_l0_locked && self.connection == 0) {
            self.motion_ppm = 0.0;
            self.stable_periods = 0;
            self.snap_back(DecayFrozenReason::NotL0);
            return self.held();
        }
        if self.held == self.learned_floor as f64 && self.motion_ppm == 0.0 && s.dll_l0_locked {
            self.settled_periods = self.settled_periods.saturating_add(1);
            if self.settled_periods >= self.stability_periods && self.connection != 0 {
                self.last_good = Some(self.held());
            }
        } else {
            self.settled_periods = 0;
        }
        if s.buffer_low {
            self.motion_ppm = 0.0;
            self.frozen_reason = Some(DecayFrozenReason::BufferLow);
            return self.held();
        }
        self.stable_periods = self.stable_periods.saturating_add(1);
        if self.stable_periods < self.stability_periods {
            self.motion_ppm = 0.0;
            self.frozen_reason = Some(DecayFrozenReason::Warmup);
        } else if self.held > self.learned_floor as f64 {
            self.motion_ppm = BUFFER_ADJUST_PPM
                .min((self.held - self.learned_floor as f64) * 1e6 / self.period_frames);
            self.frozen_reason = None;
        } else {
            self.motion_ppm = 0.0;
            self.frozen_reason = Some(if self.reused && !s.dll_l0_locked {
                DecayFrozenReason::Reused
            } else if self.learned_floor > self.floor {
                DecayFrozenReason::Backoff
            } else {
                DecayFrozenReason::AtFloor
            });
        }
        self.held()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn signals(locked: bool) -> DecaySignals {
        DecaySignals {
            locked,
            dll_l0_locked: locked,
            buffer_low: false,
        }
    }

    #[test]
    fn buffer_dips_pause_probe_descent_without_restarting_the_settle_window() {
        let mut d = DecayParams {
            enabled: true,
            floor_frames: 576,
            stability_ms: 2000,
        }
        .build(2560, 256, 48000, 500.0);
        d.context(1, false, false);
        let mut s = signals(true);
        s.dll_l0_locked = false;
        for _ in 0..1000 {
            d.tick(s);
        }
        assert!(d.held() < 2560);
        s.buffer_low = true;
        d.tick(s);
        let held = d.held();
        for _ in 0..100 {
            d.tick(s);
        }
        assert_eq!(d.held(), held);
        assert_eq!(d.demand_ppm(), 0.0);
        assert_eq!(d.frozen_reason(), Some(DecayFrozenReason::BufferLow));
        s.buffer_low = false;
        d.tick(s);
        assert!(d.demand_ppm() > 0.0);
        d.tick(s);
        d.tick(s);
        assert!(d.held() < held);
    }

    #[test]
    fn a_provisional_low_buffer_is_reusable_only_after_timing_passes() {
        for (passed, expected) in [(false, 2560), (true, 576)] {
            let mut d = DecayParams {
                enabled: true,
                floor_frames: 576,
                stability_ms: 2000,
            }
            .build(2560, 256, 48000, 500.0);
            d.context(1, false, false);
            let mut s = signals(true);
            s.dll_l0_locked = passed;
            for _ in 0..6000 {
                d.tick(s);
            }
            assert_eq!(d.held(), 576);
            d.snap_back(DecayFrozenReason::Unlocked);
            for _ in 0..100 {
                d.tick(signals(false));
            }
            assert_eq!(d.held(), expected);
        }
    }

    #[test]
    fn a_short_stall_raises_the_floor_and_does_not_retry_the_failed_depth() {
        let mut d = DecayParams {
            enabled: true,
            floor_frames: 576,
            stability_ms: 2000,
        }
        .build(2560, 256, 48000, 500.0);
        d.context(1, false, false);
        for expected in [576, 1088, 1600] {
            for _ in 0..6000 {
                d.tick(signals(true));
            }
            assert_eq!(d.held(), expected);
            assert_eq!(d.demand_ppm(), 0.0);
            d.snap_back(DecayFrozenReason::Unlocked);
            d.tick(signals(false));
            d.tick(signals(true));
        }
        assert_eq!(d.backoffs(), 3);
        d.context(2, false, false);
        assert_eq!(d.learned_floor(), 576);
        assert_eq!(d.held(), 2560);
    }
}
