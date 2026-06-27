// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Distrust `snd_pcm_htimestamp` — refine a delay with it, but verify.
//!
//! `snd_pcm_htimestamp` returns a high-resolution timestamp paired with the
//! available-frames count, letting a caller refine a whole-frame `snd_pcm_delay`
//! to sub-frame precision. PipeWire uses it for exactly that — and then
//! *distrusts* it: some devices return a garbage or stalled timestamp (the
//! Apple USB-C dongle is a known delay/timestamp liar; JTS already pins it with
//! the `resync_threshold_in_seconds=0.2` shairport workaround). PipeWire
//! sanity-checks each timestamp against `CLOCK_MONOTONIC` and stops trusting it
//! after a run of bad readings.
//!
//! This is the pure, generalised form of that workaround: one shared place that
//! takes successive `(audio_timestamp, monotonic_now)` readings, decides whether
//! each is plausible, and latches into a distrusting state after
//! `max_consecutive_lies` consecutive bad ones — after which a caller should
//! fall back to the unrefined whole-frame delay until the device proves itself
//! again. It owns NO ALSA call: the `snd_pcm_htimestamp` FFI and the delay
//! arithmetic stay at the call site (which links libasound); this is the
//! decision logic, so it unit-tests on any host.
//!
//! # What counts as a lie
//!
//! A reading is a lie when, relative to the monotonic clock sampled right after
//! the htimestamp call, the audio timestamp is either:
//! - **in the future** beyond a small tolerance (`future_tolerance_ns`) — a
//!   monotonic-vs-audio clock can never legitimately read ahead of the wall
//!   clock by more than scheduling jitter; or
//! - **impossibly stale** — older than `max_age_ns` (the timestamp stalled /
//!   the device stopped updating it), or
//! - **non-monotonic** — it went backwards versus the previous good reading
//!   (a wrapped or reset device timestamp).
//!
//! A non-finite / nonsensical input (now before the timestamp by more than the
//! tolerance, zero spans) is treated as a lie too — fail safe toward the
//! whole-frame delay rather than toward a bogus sub-frame refinement.

/// Tuning for an [`HtimestampGuard`]. Defaults target a 48 kHz audio device
/// scheduled at a few-ms period; widen the tolerances for a slower device.
#[derive(Debug, Clone, Copy)]
pub struct HtimestampGuardConfig {
    /// How far ahead of `monotonic_now` the audio timestamp may legitimately
    /// read (scheduling jitter / call-ordering slop) before it is a lie.
    pub future_tolerance_ns: i64,
    /// Oldest the audio timestamp may be (relative to `monotonic_now`) before it
    /// is treated as stalled. A healthy device updates it every period.
    pub max_age_ns: i64,
    /// Consecutive lies that latch the guard into the distrusting (disabled)
    /// state. PipeWire uses a small count; the first few bad readings are
    /// tolerated as transient.
    pub max_consecutive_lies: u32,
    /// Consecutive GOOD readings, while distrusting, that re-arm the guard. A
    /// device that recovers earns trust back rather than staying disabled
    /// forever.
    pub good_readings_to_rearm: u32,
}

impl Default for HtimestampGuardConfig {
    fn default() -> Self {
        Self {
            // 1 ms ahead is generous for call-ordering slop at audio rates.
            future_tolerance_ns: 1_000_000,
            // 200 ms stale ⇒ the timestamp has stalled. Mirrors the
            // shairport `resync_threshold_in_seconds=0.2` dongle workaround
            // this generalises.
            max_age_ns: 200_000_000,
            // Three consecutive lies before we stop trusting it.
            max_consecutive_lies: 3,
            // Eight clean readings to earn trust back.
            good_readings_to_rearm: 8,
        }
    }
}

/// What the caller should do with this cycle's htimestamp.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HtimestampVerdict {
    /// Trust the htimestamp refinement this cycle.
    Trust,
    /// This reading was a lie — ignore the refinement this cycle and use the
    /// whole-frame delay. The guard is still armed (not yet latched off).
    Reject,
    /// The guard has latched into the distrusting state after too many
    /// consecutive lies — keep using the whole-frame delay until it re-arms.
    Disabled,
}

impl HtimestampVerdict {
    /// Whether the caller should apply the sub-frame refinement this cycle.
    pub fn trust(self) -> bool {
        matches!(self, HtimestampVerdict::Trust)
    }

    pub fn as_str(self) -> &'static str {
        match self {
            HtimestampVerdict::Trust => "trust",
            HtimestampVerdict::Reject => "reject",
            HtimestampVerdict::Disabled => "disabled",
        }
    }
}

/// Pure distrust state machine for `snd_pcm_htimestamp` refinement. Fed one
/// `(audio_timestamp_ns, monotonic_now_ns)` pair per cycle; returns the verdict
/// and latches off after `max_consecutive_lies` consecutive lies, re-arming
/// after `good_readings_to_rearm` clean ones. No ALSA, no I/O.
#[derive(Debug)]
pub struct HtimestampGuard {
    config: HtimestampGuardConfig,
    /// `true` once latched into the distrusting state.
    disabled: bool,
    consecutive_lies: u32,
    consecutive_good: u32,
    last_audio_ts_ns: Option<i64>,
    // Lifetime telemetry.
    total_lies: u64,
    total_evaluations: u64,
    disable_events: u64,
}

impl Default for HtimestampGuard {
    fn default() -> Self {
        Self::new(HtimestampGuardConfig::default())
    }
}

impl HtimestampGuard {
    pub fn new(config: HtimestampGuardConfig) -> Self {
        Self {
            config,
            disabled: false,
            consecutive_lies: 0,
            consecutive_good: 0,
            last_audio_ts_ns: None,
            total_lies: 0,
            total_evaluations: 0,
            disable_events: 0,
        }
    }

    /// Evaluate one htimestamp reading.
    ///
    /// `audio_ts_ns` is the `snd_pcm_htimestamp` audio timestamp in nanoseconds;
    /// `monotonic_now_ns` is `CLOCK_MONOTONIC` sampled right after the
    /// htimestamp call. Returns the verdict; [`HtimestampVerdict::trust`] is the
    /// "apply the refinement" answer.
    pub fn evaluate(&mut self, audio_ts_ns: i64, monotonic_now_ns: i64) -> HtimestampVerdict {
        self.total_evaluations += 1;
        let is_lie = self.is_lie(audio_ts_ns, monotonic_now_ns);

        // A reading only updates the "last good" baseline when it is honest, so
        // a run of lies can't drag the monotonicity reference forward.
        if !is_lie {
            self.last_audio_ts_ns = Some(audio_ts_ns);
        }

        if is_lie {
            self.total_lies += 1;
            self.consecutive_lies = self.consecutive_lies.saturating_add(1);
            self.consecutive_good = 0;
            if !self.disabled && self.consecutive_lies >= self.config.max_consecutive_lies {
                self.disabled = true;
                self.disable_events += 1;
            }
            return if self.disabled {
                HtimestampVerdict::Disabled
            } else {
                HtimestampVerdict::Reject
            };
        }

        // Honest reading.
        self.consecutive_lies = 0;
        if self.disabled {
            self.consecutive_good = self.consecutive_good.saturating_add(1);
            if self.consecutive_good >= self.config.good_readings_to_rearm {
                self.disabled = false;
                self.consecutive_good = 0;
                return HtimestampVerdict::Trust;
            }
            // Still latched off while re-earning trust.
            return HtimestampVerdict::Disabled;
        }
        HtimestampVerdict::Trust
    }

    /// Is this reading a lie? See the module docs for the three cases.
    fn is_lie(&self, audio_ts_ns: i64, monotonic_now_ns: i64) -> bool {
        // In the future beyond tolerance: the audio clock cannot legitimately
        // read ahead of the wall clock by more than scheduling jitter.
        if audio_ts_ns > monotonic_now_ns + self.config.future_tolerance_ns {
            return true;
        }
        // Impossibly stale: the timestamp has stalled / stopped updating.
        // (`monotonic_now - audio_ts` is the timestamp's age.)
        if monotonic_now_ns - audio_ts_ns > self.config.max_age_ns {
            return true;
        }
        // Non-monotonic versus the last honest reading: a wrapped / reset
        // device timestamp.
        if let Some(prev) = self.last_audio_ts_ns {
            if audio_ts_ns < prev {
                return true;
            }
        }
        false
    }

    /// Whether the guard is currently latched into the distrusting state.
    pub fn is_disabled(&self) -> bool {
        self.disabled
    }

    /// A telemetry snapshot for `/state` / doctor.
    pub fn stats(&self) -> HtimestampGuardStats {
        HtimestampGuardStats {
            disabled: self.disabled,
            consecutive_lies: self.consecutive_lies,
            total_lies: self.total_lies,
            total_evaluations: self.total_evaluations,
            disable_events: self.disable_events,
        }
    }
}

/// Observable counters for an [`HtimestampGuard`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HtimestampGuardStats {
    pub disabled: bool,
    pub consecutive_lies: u32,
    pub total_lies: u64,
    pub total_evaluations: u64,
    pub disable_events: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    const MS: i64 = 1_000_000;

    /// An honest stream (timestamp a hair behind now, advancing) is always
    /// trusted and never latches off.
    #[test]
    fn honest_stream_is_always_trusted() {
        let mut g = HtimestampGuard::default();
        let mut now = 1_000 * MS;
        let mut ts = now - MS / 2; // 0.5 ms behind now
        for _ in 0..1000 {
            assert_eq!(g.evaluate(ts, now), HtimestampVerdict::Trust);
            now += 5 * MS;
            ts += 5 * MS;
        }
        assert!(!g.is_disabled());
        assert_eq!(g.stats().total_lies, 0);
    }

    /// A timestamp in the future beyond tolerance is a lie.
    #[test]
    fn future_timestamp_is_a_lie() {
        let mut g = HtimestampGuard::default();
        let now = 1_000 * MS;
        // 2 ms ahead, tolerance is 1 ms ⇒ lie.
        assert_eq!(g.evaluate(now + 2 * MS, now), HtimestampVerdict::Reject);
        assert_eq!(g.stats().total_lies, 1);
        // Within tolerance (0.5 ms ahead) ⇒ trusted.
        assert_eq!(g.evaluate(now + MS / 2, now), HtimestampVerdict::Trust);
    }

    /// A stalled (impossibly old) timestamp is a lie.
    #[test]
    fn stalled_timestamp_is_a_lie() {
        let mut g = HtimestampGuard::default();
        let now = 10_000 * MS;
        // 300 ms old, max_age is 200 ms ⇒ lie.
        assert_eq!(g.evaluate(now - 300 * MS, now), HtimestampVerdict::Reject);
        assert_eq!(g.stats().total_lies, 1);
    }

    /// A backwards timestamp (vs the last honest one) is a lie.
    #[test]
    fn backwards_timestamp_is_a_lie() {
        let mut g = HtimestampGuard::default();
        let now = 1_000 * MS;
        assert_eq!(g.evaluate(now - MS, now), HtimestampVerdict::Trust);
        // Next reading goes backwards in audio time while now advances.
        assert_eq!(
            g.evaluate(now - 2 * MS, now + 5 * MS),
            HtimestampVerdict::Reject
        );
    }

    /// After `max_consecutive_lies` consecutive lies the guard latches off, and
    /// stays off across further lies.
    #[test]
    fn latches_off_after_consecutive_lies() {
        let mut g = HtimestampGuard::default(); // max_consecutive_lies = 3
        let now = 1_000 * MS;
        let liar = now + 10 * MS; // always 10 ms in the future
        assert_eq!(g.evaluate(liar, now), HtimestampVerdict::Reject); // 1
        assert_eq!(g.evaluate(liar, now), HtimestampVerdict::Reject); // 2
        assert_eq!(g.evaluate(liar, now), HtimestampVerdict::Disabled); // 3 → latch
        assert!(g.is_disabled());
        assert_eq!(g.evaluate(liar, now), HtimestampVerdict::Disabled);
        assert_eq!(g.stats().disable_events, 1);
    }

    /// A single good reading among lies resets the consecutive-lie counter, so
    /// transient bad readings never latch the guard off.
    #[test]
    fn transient_lies_do_not_latch_off() {
        let mut g = HtimestampGuard::default();
        let now = 1_000 * MS;
        let liar = now + 10 * MS;
        let honest = now - MS;
        assert_eq!(g.evaluate(liar, now), HtimestampVerdict::Reject); // lie 1
        assert_eq!(g.evaluate(liar, now), HtimestampVerdict::Reject); // lie 2
        assert_eq!(g.evaluate(honest, now), HtimestampVerdict::Trust); // reset
        assert_eq!(g.evaluate(liar, now), HtimestampVerdict::Reject); // lie 1 again
        assert!(!g.is_disabled(), "transient lies must not latch off");
    }

    /// Once latched off, a run of honest readings re-arms the guard and it
    /// trusts again.
    #[test]
    fn rearms_after_a_run_of_good_readings() {
        let cfg = HtimestampGuardConfig {
            max_consecutive_lies: 2,
            good_readings_to_rearm: 4,
            ..HtimestampGuardConfig::default()
        };
        let mut g = HtimestampGuard::new(cfg);
        let mut now = 1_000 * MS;
        let liar = now + 10 * MS;
        // Latch off.
        g.evaluate(liar, now);
        assert_eq!(g.evaluate(liar, now), HtimestampVerdict::Disabled);
        assert!(g.is_disabled());
        // Good readings while disabled: still disabled until the rearm count.
        let mut ts = now - MS;
        for _ in 0..3 {
            assert_eq!(g.evaluate(ts, now), HtimestampVerdict::Disabled);
            now += 5 * MS;
            ts += 5 * MS;
        }
        // The 4th good reading re-arms and trusts.
        assert_eq!(g.evaluate(ts, now), HtimestampVerdict::Trust);
        assert!(!g.is_disabled());
    }

    /// `now` before the timestamp by more than tolerance is the future case;
    /// equal timestamps (zero age) are honest, not a lie.
    #[test]
    fn boundary_conditions() {
        let mut g = HtimestampGuard::default();
        let now = 1_000 * MS;
        // Exactly at tolerance ahead: not a lie (strictly greater is the test).
        assert_eq!(g.evaluate(now + MS, now), HtimestampVerdict::Trust);
        // Exactly now (zero age, not behind the prior): honest.
        let mut g = HtimestampGuard::default();
        assert_eq!(g.evaluate(now, now), HtimestampVerdict::Trust);
        // Exactly max_age old: not a lie (strictly greater is the test).
        let mut g = HtimestampGuard::default();
        assert_eq!(g.evaluate(now - 200 * MS, now), HtimestampVerdict::Trust);
    }

    /// Telemetry counters accumulate correctly.
    #[test]
    fn stats_accumulate() {
        let mut g = HtimestampGuard::default();
        let now = 1_000 * MS;
        g.evaluate(now - MS, now); // good
        g.evaluate(now + 10 * MS, now); // lie
        g.evaluate(now - MS, now); // good
        let s = g.stats();
        assert_eq!(s.total_evaluations, 3);
        assert_eq!(s.total_lies, 1);
        assert!(!s.disabled);
        assert_eq!(s.consecutive_lies, 0);
    }
}
