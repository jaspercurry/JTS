// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Observe-only software-AEC reference clock drift estimator.
//!
//! The software-AEC3 path (the `:9891` UDP reference outputd publishes, which
//! the AEC bridge subtracts from the mic) degrades when the *reference's
//! assumed clock* and the *speaker's actual playout clock* drift apart. The
//! AEC bridge treats the reference stream as advancing at the nominal sample
//! rate against wall-clock; the DAC's physical crystal does not. If they drift,
//! the echo path the linear AEC models slips out from under it.
//!
//! This module MEASURES that drift in ppm and nothing else. It owns no control
//! over the audio path — it never resamples, never warps, never feeds its ratio
//! back anywhere. It is the "measure before you fix" foundation (research-doc
//! increment 2 / audio-foundation review G2): surface the drift on `/state` +
//! doctor first, decide whether it is material, and only then consider a
//! compensating layer.
//!
//! # The error signal
//!
//! Each periodic sample pairs:
//! - `dac_consumed_frames = dac_frames_written - dac_snd_pcm_delay` — frames
//!   the DAC hardware has actually clocked out (net of the buffer in flight),
//! - `elapsed_seconds` — monotonic wall-clock since the first sample.
//!
//! # Virtual closed-loop observer
//!
//! A DLL is a *closed-loop* device: its output ratio is only meaningful when it
//! feeds back to null the error. Driving it open-loop with a fixed offset makes
//! the third integrator ramp without bound — the ratio is then garbage. But we
//! must NOT close the loop through the audio path (that would be resampling, an
//! explicit non-goal here). So we close it through a **virtual** nominal clock:
//!
//! - `virtual` advances each interval by `sample_rate * Δelapsed * ratio` — the
//!   nominal reference frames the AEC bridge assumes, scaled by the loop's own
//!   correction;
//! - the error fed to the DLL is `virtual - dac_consumed` (the capture /
//!   negative-feedback sign: virtual ahead → slow the virtual clock down);
//! - the loop converges so `virtual` tracks the actual DAC playout, and its
//!   `ratio` settles at exactly the drift. We read that ratio's ppm and apply it
//!   to **nothing** — only the virtual clock ever sees it.
//!
//! **Sign convention:** [`SoftwareAecRefClock::sro_ppm`] reports
//! `(ratio - 1) * 1e6`. Positive ppm means the DAC clock runs fast relative to
//! wall-clock (it clocks out more frames per real second than the nominal
//! reference assumes); negative means slow.
//!
//! # Why a DLL and not the least-squares [`crate::aec_clock::SroEstimator`]
//!
//! The `SroEstimator` measures DAC-vs-chip-ref drift for the *chip-AEC* path; this
//! is the *software-AEC* path against wall-clock — a distinct clock pair. Both
//! are observe-only, but this one composes the shared [`jasper_clock::Dll`] so
//! the loop math (the spa_dll convergence + the variance-driven bandwidth + the
//! resync hard-jump) is the one shared primitive, and the ppm here is directly
//! comparable to every other DLL site's `clock.rate_diff`.

use jasper_clock::{Dll, DllConfig, DllSnapshot};

/// Below this absolute ppm the software-AEC reference is treated as
/// clock-coherent (no compensation would help). Mirrors
/// [`crate::aec_clock::SRO_COHERENT_PPM`] so the two AEC paths classify on the
/// same threshold; PROVISIONAL pending on-hardware validation.
pub const SRO_COHERENT_PPM: f64 = 5.0;

/// A snapshot a `/state` serializer can render without holding the estimator.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SoftwareAecRefSnapshot {
    /// `true` once the DLL has acquired lock on the drift; the ppm is only
    /// meaningful when locked.
    pub locked: bool,
    /// The measured drift in ppm (the DLL's `clock.rate_diff`). Reported
    /// regardless of lock; treat as provisional until `locked`.
    pub sro_ppm: f64,
    /// Running mean of the drift error (frames).
    pub error_mean: f64,
    /// Running variance of the drift error (frames²).
    pub error_var: f64,
    /// How many drift samples have been fed.
    pub updates: u64,
    /// Times the loop hard-jumped on a discontinuity (clock reset / xrun).
    pub resync_count: u64,
}

impl SoftwareAecRefSnapshot {
    /// Coherent (locked + within threshold), compensable (locked + beyond), or
    /// fallback (not yet locked) — the same three-way verdict vocabulary as the
    /// chip-AEC path, so a single doctor surface can read both.
    pub fn verdict(&self) -> &'static str {
        if !self.locked {
            "fallback"
        } else if self.sro_ppm.abs() < SRO_COHERENT_PPM {
            "coherent"
        } else {
            "compensable"
        }
    }
}

/// Observe-only software-AEC reference drift estimator. Composes a
/// [`jasper_clock::Dll`] driven through a VIRTUAL closed loop (no audio path is
/// touched). Pure: no ALSA, no threads, no audio side effects.
#[derive(Debug)]
pub struct SoftwareAecRefClock {
    dll: Dll,
    sample_rate: f64,
    /// Minimum elapsed-seconds delta between accepted samples. The DAC delay is
    /// sampled ~per-period (~50 Hz at 1024/48k); feeding the loop that fast
    /// collapses the drift baseline below `snd_pcm_delay` quantization. Pace it
    /// to ~1 Hz so each error reflects ~1 s of accumulated drift.
    min_sample_interval_s: f64,
    last_elapsed_s: Option<f64>,
    /// Guards against a non-monotonic counter snapshot: consumed frames and
    /// elapsed time only ever advance. A regression means a device reset / tear
    /// — drop history and re-observe instead of feeding a bogus error.
    last_consumed: Option<f64>,
    /// The virtual nominal frame position. Advances each interval by
    /// `sample_rate * Δelapsed * ratio`; the DLL error is `virtual - consumed`,
    /// so the loop converges with `virtual` tracking the real DAC playout and
    /// its `ratio` settling at the true drift. Anchored to `consumed` at the
    /// first accepted sample. Observe-only — `virtual` feeds nothing audible.
    virtual_frames: f64,
}

impl SoftwareAecRefClock {
    /// Construct for a sink at `sample_rate` Hz. (`period_frames` is accepted
    /// for constructor symmetry with the other outputd estimators; the DLL
    /// timescale here is set by the ~1 Hz drift-sample cadence, not the audio
    /// period.)
    pub fn new(sample_rate: u32, _period_frames: u32) -> Self {
        let sample_rate = sample_rate.max(1);
        // The DLL is fed at ~1 Hz, so its "period" (cycle size) is one second of
        // frames and its rate is the sample rate — one error sample per nominal
        // second. max_resync then guards against a >1 s frame discontinuity.
        let config = DllConfig::for_rate(sample_rate, sample_rate);
        Self {
            dll: Dll::new(config),
            sample_rate: f64::from(sample_rate),
            min_sample_interval_s: 1.0,
            last_elapsed_s: None,
            last_consumed: None,
            virtual_frames: 0.0,
        }
    }

    /// Feed one periodic snapshot. `dac_frames_written` and `dac_delay_frames`
    /// are the counters already tracked in state; `elapsed_seconds` is the
    /// monotonic time since outputd's first reference publish. The pair is
    /// accepted only once `min_sample_interval_s` has elapsed since the last
    /// accepted sample; sub-interval calls are ignored (return without touching
    /// the loop). OBSERVE-ONLY — the ratio drives only the virtual clock.
    pub fn observe(
        &mut self,
        dac_frames_written: u64,
        dac_delay_frames: u64,
        elapsed_seconds: f64,
    ) {
        if !elapsed_seconds.is_finite() || elapsed_seconds < 0.0 {
            return;
        }
        // Consumed = written - in-flight; a negative result (delay exceeds
        // written) is implausible — skip it.
        let consumed = match dac_frames_written.checked_sub(dac_delay_frames) {
            Some(v) => v as f64,
            None => return,
        };

        let last_elapsed = match self.last_elapsed_s {
            // First accepted sample: anchor the virtual clock to the actual
            // consumed position so the loop starts at zero error, and record
            // the baseline without driving the loop yet (no interval to span).
            None => {
                self.virtual_frames = consumed;
                self.last_elapsed_s = Some(elapsed_seconds);
                self.last_consumed = Some(consumed);
                return;
            }
            Some(last) => last,
        };

        // Decimate to ~1 Hz.
        let interval = elapsed_seconds - last_elapsed;
        if interval < self.min_sample_interval_s {
            return;
        }
        // Monotonicity guard: a regression in consumed (or time) is a device
        // reset / tear — re-anchor and re-observe rather than feed a bogus jump.
        if let Some(prev) = self.last_consumed {
            if consumed < prev || elapsed_seconds < last_elapsed {
                self.reset();
                self.virtual_frames = consumed;
                self.last_elapsed_s = Some(elapsed_seconds);
                self.last_consumed = Some(consumed);
                return;
            }
        }

        // Advance the virtual nominal clock by the loop's current correction,
        // then feed the negative-feedback error. The loop converges so virtual
        // tracks the real DAC playout; its ratio settles at the drift.
        self.virtual_frames += self.sample_rate * interval * self.dll.ratio();
        let err = self.virtual_frames - consumed;
        let _ratio = self.dll.update(err);

        self.last_elapsed_s = Some(elapsed_seconds);
        self.last_consumed = Some(consumed);
    }

    /// Re-initialise the loop on a discontinuity (xrun / device re-open).
    pub fn reset(&mut self) {
        self.dll.reset();
        self.last_elapsed_s = None;
        self.last_consumed = None;
        self.virtual_frames = 0.0;
    }

    /// The measured drift in ppm (the DLL's `clock.rate_diff`).
    pub fn sro_ppm(&self) -> f64 {
        self.dll.ratio_ppm()
    }

    /// Whether the drift loop has locked.
    pub fn is_locked(&self) -> bool {
        self.dll.is_locked()
    }

    /// An immutable snapshot for `/state` / doctor.
    pub fn snapshot(&self) -> SoftwareAecRefSnapshot {
        let DllSnapshot {
            ratio_ppm,
            error_mean,
            error_var,
            locked,
            updates,
            resync_count,
            ..
        } = self.dll.snapshot();
        SoftwareAecRefSnapshot {
            locked,
            sro_ppm: ratio_ppm,
            error_mean,
            error_var,
            updates,
            resync_count,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RATE: u32 = 48_000;
    const PERIOD: u32 = 1024;

    /// Drive `secs` seconds of paired snapshots where the DAC clock runs `ppm`
    /// relative to wall-clock. One snapshot per period (~50 Hz) so the
    /// decimation gate is exercised; only ~1/s is accepted.
    fn drive(clock: &mut SoftwareAecRefClock, secs: u64, dac_ppm: f64) {
        // ~50 Hz period cadence.
        let period_s = f64::from(PERIOD) / f64::from(RATE);
        let total_steps = (secs as f64 / period_s) as u64;
        let dac_delay: u64 = 1024;
        for step in 1..=total_steps {
            let elapsed = step as f64 * period_s;
            // A DAC running `ppm` fast clocks out proportionally more frames.
            let consumed = f64::from(RATE) * elapsed * (1.0 + dac_ppm / 1.0e6);
            let written = (consumed.round() as u64) + dac_delay;
            clock.observe(written, dac_delay, elapsed);
        }
    }

    #[test]
    fn coherent_clock_reads_near_zero_ppm_and_locks() {
        let mut clock = SoftwareAecRefClock::new(RATE, PERIOD);
        drive(&mut clock, 200, 0.0);
        let snap = clock.snapshot();
        assert!(snap.locked, "a coherent clock should lock: {snap:?}");
        assert!(
            snap.sro_ppm.abs() < 1.0,
            "coherent clock ~0 ppm, got {}",
            snap.sro_ppm
        );
        assert_eq!(snap.verdict(), "coherent");
    }

    #[test]
    fn fast_dac_reads_a_steady_offset() {
        let mut clock = SoftwareAecRefClock::new(RATE, PERIOD);
        // DAC runs +50 ppm fast → clocks out more frames than wall-clock
        // predicts. The virtual closed loop converges so its ratio reports the
        // drift directly: positive ppm == fast DAC (documented sign convention).
        drive(&mut clock, 400, 50.0);
        let snap = clock.snapshot();
        assert!(snap.locked, "should lock on a steady offset: {snap:?}");
        assert!(
            (snap.sro_ppm - 50.0).abs() < 3.0,
            "expected ~+50 ppm (fast DAC), got {}",
            snap.sro_ppm
        );
        assert_eq!(snap.verdict(), "compensable");
    }

    #[test]
    fn slow_dac_reads_a_negative_offset() {
        let mut clock = SoftwareAecRefClock::new(RATE, PERIOD);
        // A DAC running 80 ppm slow clocks out fewer frames than wall-clock
        // predicts → the loop ratio settles negative.
        drive(&mut clock, 400, -80.0);
        let snap = clock.snapshot();
        assert!(snap.locked, "should lock on a steady offset: {snap:?}");
        assert!(
            (snap.sro_ppm + 80.0).abs() < 3.0,
            "expected ~-80 ppm (slow DAC), got {}",
            snap.sro_ppm
        );
        assert_eq!(snap.verdict(), "compensable");
    }

    #[test]
    fn decimates_sub_interval_calls() {
        let mut clock = SoftwareAecRefClock::new(RATE, PERIOD);
        // Feed 100 sub-second calls (all within the first second): the loop
        // accepts at most one, so it cannot have acquired lock.
        let dac_delay = 1024;
        for step in 1..=100u64 {
            let elapsed = step as f64 * 0.001; // 1 ms apart → all sub-1 s
            let consumed = f64::from(RATE) * elapsed;
            clock.observe(consumed.round() as u64 + dac_delay, dac_delay, elapsed);
        }
        assert!(
            !clock.is_locked(),
            "sub-interval calls must not accumulate enough samples to lock"
        );
        assert!(clock.snapshot().updates <= 1);
    }

    #[test]
    fn non_monotonic_consumed_resets_history() {
        let mut clock = SoftwareAecRefClock::new(RATE, PERIOD);
        drive(&mut clock, 200, 0.0);
        assert!(clock.is_locked());
        // A snapshot where consumed regresses (device reset) past the interval:
        // the loop re-initialises rather than feeding a huge negative error.
        let elapsed = 1_000.0;
        clock.observe(0, 0, elapsed); // consumed = 0, way below prior
        assert!(!clock.is_locked(), "a consumed regression drops lock");
    }

    #[test]
    fn negative_or_nonfinite_elapsed_is_ignored() {
        let mut clock = SoftwareAecRefClock::new(RATE, PERIOD);
        clock.observe(48_000, 1024, -1.0);
        clock.observe(48_000, 1024, f64::NAN);
        assert_eq!(clock.snapshot().updates, 0);
    }

    #[test]
    fn delay_exceeding_written_is_skipped_not_panic() {
        let mut clock = SoftwareAecRefClock::new(RATE, PERIOD);
        // delay > written → consumed would be negative → skip.
        clock.observe(100, 200, 1.0);
        assert_eq!(clock.snapshot().updates, 0);
    }
}
