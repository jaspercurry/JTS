// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Passive sample-rate-offset (SRO) estimator for the chip-AEC reference.
//!
//! Layer 0 of chip-AEC DAC portability: a purely *observational* drift
//! measurement. The output DAC and the XVF3800 mic run on potentially
//! different physical clocks; when the DAC drifts relative to the XVF, the
//! 16 kHz chip reference de-aligns from what the mic actually hears and
//! hardware echo cancellation degrades. This module MEASURES that drift in
//! ppm. It owns no control loop, no resampler, and never warps any audio —
//! it is fed periodic counter snapshots and returns an estimate plus a
//! status/verdict for `/state` and `jasper-doctor`.
//!
//! The struct is decoupled from ALSA and threads so its unit tests run on
//! any host (the rest of the crate links libasound, which is Linux-only).
//!
//! ## What is measured
//!
//! For each endpoint, "consumed frames" = `frames_written -
//! snd_pcm_delay_frames` (frames the device has actually clocked out, net of
//! the buffer still in flight). Converting each to seconds *in its own
//! nominal clock* gives:
//!
//! - `dac_seconds      = dac_consumed / 48000`
//! - `chip_ref_seconds = chip_ref_consumed / chip_ref_rate`
//!
//! If both endpoints share one physical clock (e.g. an Apple dongle whose
//! output and the XVF are clock-coherent), these two timelines advance in
//! lockstep and their ratio is 1.0 → 0 ppm. If the DAC's physical clock runs
//! fast, it clocks out *more* frames per real second, so `dac_seconds`
//! (computed with the nominal 48000) advances faster than `chip_ref_seconds`,
//! and the slope of `dac_seconds` vs `chip_ref_seconds` exceeds 1.0.
//!
//! ## Sign convention
//!
//! `sro_ppm = (slope - 1) * 1e6`, where `slope = d(dac_seconds) /
//! d(chip_ref_seconds)`. **Positive ppm means the DAC clock is running fast
//! relative to the XVF clock.** A clock-coherent pair reads ≈ 0 ppm; a DAC
//! running 50 ppm fast reads ≈ +50 ppm.
//!
//! ## Estimation
//!
//! A fixed-size ring of recent `(dac_seconds, chip_ref_seconds)` samples
//! (no per-update heap allocation). The slope is a least-squares fit of
//! `dac_seconds` against `chip_ref_seconds` over the window. We use a window
//! rather than a single long-baseline delta so a one-off implausible sample
//! does not dominate, while still being simple and allocation-free.

/// Below this absolute ppm a locked estimate is treated as clock-coherent;
/// the chip reference needs no compensation. (Layer-1 verdict threshold.)
/// PROVISIONAL: validate/tune per DAC profile on hardware (JTS3 HiFiBerry
/// first). See docs/HANDOFF-chip-aec-portability.md.
pub const SRO_COHERENT_PPM: f64 = 5.0;

/// Ring capacity. At one sample per ~second this is a ~32 s window — long
/// enough that quantization in the per-endpoint frame counters averages out
/// to a stable sub-ppm slope, short enough to follow a genuine clock change.
const WINDOW: usize = 32;

/// Minimum samples before the slope is trustworthy. Below this the estimator
/// is still `Observing` and returns `None`.
const MIN_SAMPLES: usize = 8;

/// A drift slope this far from coherent is implausible for two free-running
/// crystals (real clock pairs are within a few hundred ppm); reading it means
/// a counter glitched, so the estimate is marked untrusted rather than
/// reported as a real measurement.
const MAX_PLAUSIBLE_PPM: f64 = 5_000.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SroStatus {
    /// Not enough samples yet; estimate is `None`.
    Observing,
    /// A trusted slope estimate is available.
    Locked,
    /// Counters went non-monotonic or a sample was implausible; estimate is
    /// `None` and downstream should fall back rather than trust drift.
    Untrusted,
}

impl SroStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            SroStatus::Observing => "observing",
            SroStatus::Locked => "locked",
            SroStatus::Untrusted => "untrusted",
        }
    }
}

/// Layer-1 verdict: a thin classifier over the SRO estimate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AecClockVerdict {
    /// Locked and within `SRO_COHERENT_PPM`: the reference needs no
    /// compensation.
    Coherent,
    /// Locked but beyond `SRO_COHERENT_PPM`: a real, steady offset that a
    /// future resampling layer could compensate.
    Compensable,
    /// Untrusted estimate: drift cannot be measured right now.
    Fallback,
}

impl AecClockVerdict {
    pub fn as_str(self) -> &'static str {
        match self {
            AecClockVerdict::Coherent => "coherent",
            AecClockVerdict::Compensable => "compensable",
            AecClockVerdict::Fallback => "fallback",
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct Sample {
    dac_seconds: f64,
    chip_ref_seconds: f64,
}

/// Pure, allocation-light SRO estimator. Fed periodic counter snapshots;
/// never panics; fails soft to `Untrusted` + `None` on bad input.
#[derive(Debug)]
pub struct SroEstimator {
    ring: [Sample; WINDOW],
    len: usize,
    next: usize,
    status: SroStatus,
    sro_ppm: Option<f64>,
    last_dac_consumed: Option<f64>,
    last_chip_ref_consumed: Option<f64>,
}

impl Default for SroEstimator {
    fn default() -> Self {
        Self::new()
    }
}

impl SroEstimator {
    pub fn new() -> Self {
        Self {
            ring: [Sample {
                dac_seconds: 0.0,
                chip_ref_seconds: 0.0,
            }; WINDOW],
            len: 0,
            next: 0,
            status: SroStatus::Observing,
            sro_ppm: None,
            last_dac_consumed: None,
            last_chip_ref_consumed: None,
        }
    }

    /// Feed one periodic snapshot of the two endpoints' counters.
    ///
    /// `*_frames_written` and `*_delay_frames` are the per-endpoint
    /// `frames_written` and `snd_pcm_delay_frames` already tracked in state.
    /// `chip_ref_rate` is the chip reference sample rate (e.g. 16000).
    ///
    /// Returns the current `(estimate, status)`. The estimate is `None` while
    /// observing or when untrusted.
    pub fn update(
        &mut self,
        dac_frames_written: u64,
        dac_delay_frames: u64,
        chip_ref_frames_written: u64,
        chip_ref_delay_frames: u64,
        chip_ref_rate: u32,
    ) -> (Option<f64>, SroStatus) {
        // A zero chip-ref rate would divide by zero; treat as unmeasurable.
        if chip_ref_rate == 0 {
            return self.mark_untrusted();
        }
        // Consumed = written - in-flight. A negative result (delay exceeds
        // written) is implausible; fail soft.
        let dac_consumed = match (dac_frames_written).checked_sub(dac_delay_frames) {
            Some(v) => v as f64,
            None => return self.mark_untrusted(),
        };
        let chip_ref_consumed = match (chip_ref_frames_written).checked_sub(chip_ref_delay_frames) {
            Some(v) => v as f64,
            None => return self.mark_untrusted(),
        };

        // Monotonicity guard: consumed counts only advance. A decrease means
        // a counter reset or a snapshot tear — drop the history and re-observe.
        if let (Some(prev_dac), Some(prev_chip)) =
            (self.last_dac_consumed, self.last_chip_ref_consumed)
        {
            if dac_consumed < prev_dac || chip_ref_consumed < prev_chip {
                self.reset_window();
                self.last_dac_consumed = Some(dac_consumed);
                self.last_chip_ref_consumed = Some(chip_ref_consumed);
                return (self.sro_ppm, self.status);
            }
        }
        self.last_dac_consumed = Some(dac_consumed);
        self.last_chip_ref_consumed = Some(chip_ref_consumed);

        // u64->f64 is lossless below 2^53 frames (~6000 years at 48 kHz), and
        // the slope below uses mean-centered values, so ppm precision stays
        // safe even at multi-billion cumulative frame counts.
        let sample = Sample {
            dac_seconds: dac_consumed / f64::from(crate::types::SAMPLE_RATE),
            chip_ref_seconds: chip_ref_consumed / f64::from(chip_ref_rate),
        };
        self.ring[self.next] = sample;
        self.next = (self.next + 1) % WINDOW;
        if self.len < WINDOW {
            self.len += 1;
        }

        self.recompute();
        (self.sro_ppm, self.status)
    }

    pub fn status(&self) -> SroStatus {
        self.status
    }

    pub fn sro_ppm(&self) -> Option<f64> {
        self.sro_ppm
    }

    /// Layer-1 verdict over the current estimate. Delegates to the pure
    /// [`verdict_for`] so callers (and the /state serializer) can classify
    /// from a `(status, ppm)` snapshot without holding an `SroEstimator`.
    pub fn verdict(&self) -> AecClockVerdict {
        verdict_for(self.status, self.sro_ppm)
    }

    /// Human-readable one-liner explaining the current verdict.
    pub fn verdict_reason(&self) -> String {
        verdict_reason_for(self.status, self.sro_ppm)
    }

    fn recompute(&mut self) {
        if self.len < MIN_SAMPLES {
            self.status = SroStatus::Observing;
            self.sro_ppm = None;
            return;
        }
        match self.least_squares_slope() {
            Some(slope) => {
                let ppm = (slope - 1.0) * 1.0e6;
                if !ppm.is_finite() || ppm.abs() > MAX_PLAUSIBLE_PPM {
                    self.status = SroStatus::Untrusted;
                    self.sro_ppm = None;
                } else {
                    self.status = SroStatus::Locked;
                    self.sro_ppm = Some(ppm);
                }
            }
            // Degenerate window (no spread on the x axis): keep observing.
            None => {
                self.status = SroStatus::Observing;
                self.sro_ppm = None;
            }
        }
    }

    /// Least-squares slope of `dac_seconds` (y) vs `chip_ref_seconds` (x)
    /// over the window. `None` when x has no spread (cannot fit a slope).
    fn least_squares_slope(&self) -> Option<f64> {
        let n = self.len as f64;
        let mut sum_x = 0.0;
        let mut sum_y = 0.0;
        for s in self.iter() {
            sum_x += s.chip_ref_seconds;
            sum_y += s.dac_seconds;
        }
        let mean_x = sum_x / n;
        let mean_y = sum_y / n;
        let mut sxx = 0.0;
        let mut sxy = 0.0;
        for s in self.iter() {
            let dx = s.chip_ref_seconds - mean_x;
            sxx += dx * dx;
            sxy += dx * (s.dac_seconds - mean_y);
        }
        if sxx <= 0.0 {
            return None;
        }
        Some(sxy / sxx)
    }

    fn iter(&self) -> impl Iterator<Item = &Sample> {
        self.ring.iter().take(self.len)
    }

    fn reset_window(&mut self) {
        self.len = 0;
        self.next = 0;
        self.status = SroStatus::Observing;
        self.sro_ppm = None;
    }

    fn mark_untrusted(&mut self) -> (Option<f64>, SroStatus) {
        self.status = SroStatus::Untrusted;
        self.sro_ppm = None;
        (self.sro_ppm, self.status)
    }
}

/// Pure Layer-1 classification of a `(status, ppm)` snapshot. Free function so
/// the /state serializer and any future consumer (e.g. a Layer-2 compensator)
/// can classify without holding an `SroEstimator`. NOTE: this only observes;
/// Layer 2 owns the decision of how to *act* on a `Compensable` verdict.
pub fn verdict_for(status: SroStatus, sro_ppm: Option<f64>) -> AecClockVerdict {
    match status {
        SroStatus::Untrusted | SroStatus::Observing => AecClockVerdict::Fallback,
        SroStatus::Locked => match sro_ppm {
            Some(ppm) if ppm.abs() < SRO_COHERENT_PPM => AecClockVerdict::Coherent,
            Some(_) => AecClockVerdict::Compensable,
            // Locked-without-estimate is unreachable, but never panic.
            None => AecClockVerdict::Fallback,
        },
    }
}

/// Human-readable one-liner for a `(status, ppm)` snapshot. Allocates a
/// `String`, so callers MUST invoke this OUTSIDE any estimator lock.
pub fn verdict_reason_for(status: SroStatus, sro_ppm: Option<f64>) -> String {
    match verdict_for(status, sro_ppm) {
        AecClockVerdict::Coherent => format!(
            "sro {:.1} ppm within coherent threshold ({:.1} ppm)",
            sro_ppm.unwrap_or(0.0),
            SRO_COHERENT_PPM,
        ),
        AecClockVerdict::Compensable => format!(
            "sro {:.1} ppm exceeds coherent threshold ({:.1} ppm)",
            sro_ppm.unwrap_or(0.0),
            SRO_COHERENT_PPM,
        ),
        AecClockVerdict::Fallback => match status {
            SroStatus::Observing => format!("observing: need {} samples to lock", MIN_SAMPLES),
            _ => "drift untrusted (counter non-monotonic or implausible)".to_string(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CHIP_RATE: u32 = 16_000;

    /// Drive `samples` periodic snapshots where the DAC clock runs `ppm`
    /// fast relative to the XVF. Each step advances ~1 s of real (XVF) time.
    /// Returns the final `(estimate, status)`.
    fn drive(estimator: &mut SroEstimator, samples: usize, ppm: f64) -> (Option<f64>, SroStatus) {
        let mut chip_written: u64 = 0;
        // Fixed in-flight buffer depth on each endpoint (constant → does not
        // affect the slope, but mirrors the real counters).
        let dac_delay: u64 = 1024;
        let chip_delay: u64 = 320;
        let mut out = (None, SroStatus::Observing);
        for step in 1..=samples {
            // One second of XVF time advances chip_ref by its rate exactly...
            chip_written += u64::from(CHIP_RATE);
            // ...and the DAC by 48000 scaled up by the drift (fast clock
            // clocks out more nominal frames per real second). Round the
            // CUMULATIVE target, not the per-step increment, so the integer
            // counter tracks the true ppm instead of accumulating a one-sided
            // rounding bias.
            let dac_written = (48_000.0 * step as f64 * (1.0 + ppm / 1.0e6)).round() as u64;
            out = estimator.update(dac_written, dac_delay, chip_written, chip_delay, CHIP_RATE);
        }
        out
    }

    #[test]
    fn coherent_clocks_read_near_zero_ppm() {
        let mut est = SroEstimator::new();
        let (ppm, status) = drive(&mut est, WINDOW, 0.0);
        assert_eq!(status, SroStatus::Locked);
        let ppm = ppm.expect("locked estimate present");
        assert!(ppm.abs() < 0.5, "expected ~0 ppm, got {ppm}");
        assert_eq!(est.verdict(), AecClockVerdict::Coherent);
    }

    #[test]
    fn fast_dac_reads_positive_fifty_ppm() {
        let mut est = SroEstimator::new();
        let (ppm, status) = drive(&mut est, WINDOW, 50.0);
        assert_eq!(status, SroStatus::Locked);
        let ppm = ppm.expect("locked estimate present");
        assert!((ppm - 50.0).abs() < 2.0, "expected ~+50 ppm, got {ppm}");
        assert_eq!(est.verdict(), AecClockVerdict::Compensable);
    }

    #[test]
    fn non_monotonic_counter_marks_untrusted_or_resets() {
        let mut est = SroEstimator::new();
        // Build a locked history first.
        let (_, status) = drive(&mut est, WINDOW, 50.0);
        assert_eq!(status, SroStatus::Locked);
        // A snapshot where consumed goes negative (delay exceeds written) is
        // implausible and must be marked untrusted, never panic.
        let (ppm, status) = est.update(100, 200, 16_000, 320, CHIP_RATE);
        assert_eq!(status, SroStatus::Untrusted);
        assert!(ppm.is_none());
        assert_eq!(est.verdict(), AecClockVerdict::Fallback);
    }

    #[test]
    fn too_few_samples_stay_observing() {
        let mut est = SroEstimator::new();
        let (ppm, status) = drive(&mut est, MIN_SAMPLES - 1, 50.0);
        assert_eq!(status, SroStatus::Observing);
        assert!(ppm.is_none());
        assert_eq!(est.verdict(), AecClockVerdict::Fallback);
        assert!(est.verdict_reason().contains("observing"));
    }

    #[test]
    fn zero_chip_rate_is_untrusted_not_panic() {
        let mut est = SroEstimator::new();
        let (ppm, status) = est.update(48_000, 1024, 16_000, 320, 0);
        assert_eq!(status, SroStatus::Untrusted);
        assert!(ppm.is_none());
    }

    #[test]
    fn implausible_slope_is_untrusted() {
        let mut est = SroEstimator::new();
        // 50000 ppm (5%) is far beyond any real crystal pair.
        let (ppm, status) = drive(&mut est, WINDOW, 50_000.0);
        assert_eq!(status, SroStatus::Untrusted);
        assert!(ppm.is_none());
        assert_eq!(est.verdict(), AecClockVerdict::Fallback);
    }
}
