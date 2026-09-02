// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Is the final output stage emitting silence it did not intend to?
//!
//! Every content source outputd can run answers a period it cannot fill with
//! ZERO-FILL, not an error (D4 for the round-trip lane, `try_consume_slot` for
//! the ring). That is the right audio behaviour and the reason a fully deaf
//! chain reads green everywhere: the DAC keeps writing periods, the watchdog
//! keeps progressing, and the cumulative starvation counters are the only trace
//! — one per source, in three vocabularies, with no threshold on any of them.
//!
//! This tracker is the one place that says a RUN of them means the speaker is
//! deaf: it counts consecutive unfilled periods on whichever source is live and
//! reports the two edges. Detection only — no recovery, no fallback source
//! (#3458).

/// Wall-clock silence that means "deaf" rather than "slipped a period".
///
/// A single unfilled period is an ordinary producer slip; 2 s of unbroken
/// zero-fill is not something a healthy chain does, and it is short enough that
/// an operator watching the journal sees the edge while the fault is live.
const DEAF_SECONDS: u64 = 2;

/// Which edge one period crossed, if any, and how long the unbroken zero-fill
/// run was there. The caller logs it — keeping the `eprintln!` out of here is
/// what makes the run counting testable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContentFillEdge {
    /// This period completed [`ContentFill::deaf_threshold_periods`] unbroken
    /// unfilled periods. Emitted ONCE per outage, on the crossing period.
    Deaf { empty_periods: u64 },
    /// A deaf run of this many periods ended: content is flowing again.
    Recovered { empty_periods: u64 },
}

/// Consecutive-empty tracking for the live content source.
#[derive(Debug)]
pub struct ContentFill {
    source: &'static str,
    threshold_periods: u64,
    consecutive: u64,
    deaf: bool,
}

impl ContentFill {
    /// `source` names the live content source for the event line; the DAC's
    /// NEGOTIATED geometry sets the threshold.
    ///
    /// The threshold is periods, but the fact it stands for is seconds, so it
    /// is derived rather than pinned: `rate / period_frames` periods per second
    /// (48000/1024 ≈ 46.9 on today's boxes), rounded UP so a box with a longer
    /// period waits at least `DEAF_SECONDS`, never less. A hardcoded count
    /// would mean a different wall-clock on every period size.
    pub fn new(source: &'static str, dac: crate::alsa_backend::NegotiatedPcm) -> Self {
        // `.max(1)` on the divisor, not a check: `div_ceil` PANICS on zero,
        // and this runs on the daemon that owns the speaker.
        let periods_per_second =
            u64::from(dac.sample_rate).div_ceil(u64::from(dac.period_frames).max(1));
        Self {
            source,
            // At least one period: a geometry that would round to zero must
            // still take an edge rather than declare every period deaf.
            threshold_periods: (periods_per_second * DEAF_SECONDS).max(1),
            consecutive: 0,
            deaf: false,
        }
    }

    pub fn source(&self) -> &'static str {
        self.source
    }

    pub fn deaf_threshold_periods(&self) -> u64 {
        self.threshold_periods
    }

    pub fn consecutive_empty_periods(&self) -> u64 {
        self.consecutive
    }

    pub fn deaf(&self) -> bool {
        self.deaf
    }

    /// Observe one period. `served` is false when the source zero-filled.
    pub fn observe(&mut self, served: bool) -> Option<ContentFillEdge> {
        if served {
            let empty_periods = std::mem::take(&mut self.consecutive);
            if self.deaf {
                self.deaf = false;
                return Some(ContentFillEdge::Recovered { empty_periods });
            }
            return None;
        }
        self.consecutive = self.consecutive.saturating_add(1);
        if !self.deaf && self.consecutive >= self.threshold_periods {
            self.deaf = true;
            return Some(ContentFillEdge::Deaf {
                empty_periods: self.consecutive,
            });
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::alsa_backend::NegotiatedPcm;

    fn tracker() -> ContentFill {
        // 48000 / 1024 -> 47 periods per second, 94 for the 2 s threshold.
        ContentFill::new(
            "shm_ring",
            NegotiatedPcm {
                sample_rate: 48_000,
                period_frames: 1024,
                buffer_frames: 4096,
            },
        )
    }

    #[test]
    fn the_threshold_is_two_seconds_of_dac_periods() {
        assert_eq!(tracker().deaf_threshold_periods(), 94);
        let long_period = ContentFill::new(
            "shm_ring",
            NegotiatedPcm {
                sample_rate: 48_000,
                period_frames: 48_000,
                buffer_frames: 96_000,
            },
        );
        assert_eq!(long_period.deaf_threshold_periods(), 2);
    }

    #[test]
    fn the_deaf_edge_fires_once_at_the_threshold_and_not_before() {
        let mut fill = tracker();
        let threshold = fill.deaf_threshold_periods();
        for _ in 1..threshold {
            assert_eq!(fill.observe(false), None);
            assert!(!fill.deaf());
        }
        assert_eq!(
            fill.observe(false),
            Some(ContentFillEdge::Deaf {
                empty_periods: threshold
            })
        );
        assert!(fill.deaf());
        assert_eq!(fill.consecutive_empty_periods(), threshold);
        // A chronically dry producer cannot re-fire the edge.
        for _ in 0..threshold {
            assert_eq!(fill.observe(false), None);
        }
        assert!(fill.deaf());
        assert_eq!(fill.consecutive_empty_periods(), threshold * 2);
    }

    #[test]
    fn a_served_period_resets_the_run_and_only_a_deaf_run_recovers() {
        let mut fill = tracker();
        let threshold = fill.deaf_threshold_periods();
        fill.observe(false);
        fill.observe(false);
        // Below the threshold: an ordinary slip, so no edge in either direction.
        assert_eq!(fill.observe(true), None);
        assert_eq!(fill.consecutive_empty_periods(), 0);
        for _ in 0..threshold {
            fill.observe(false);
        }
        assert!(fill.deaf());
        assert_eq!(
            fill.observe(true),
            Some(ContentFillEdge::Recovered {
                empty_periods: threshold
            })
        );
        assert!(!fill.deaf());
        assert_eq!(fill.observe(true), None);
    }
}
