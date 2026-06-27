// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Observe-only DAC playout-clock drift observer.
//!
//! MEASURES, in ppm, how far the speaker DAC's physical playout crystal drifts
//! from nominal wall-clock — and nothing else. It owns no control over the
//! audio path: it never resamples, never warps, never feeds its ratio back
//! anywhere. It is a clock-domain *observability* surface — the same ppm every
//! other DLL site reports (`clock.rate_diff`), for the one clock JTS otherwise
//! can't see.
//!
//! **What it is NOT — read before acting on the number.** This is the DAC clock
//! vs NOMINAL, *not* the DAC-vs-mic drift the software echo canceller actually
//! faces (and `outputd`, the reference SENDER, structurally can't see the mic
//! clock). WebRTC AEC3 also already self-compensates for render-vs-capture
//! drift via its delay estimator (see `jasper/cli/aec_bridge.py`'s own note),
//! so a large number here does NOT by itself justify a software-AEC resampling
//! fix. Treat it as a diagnostic for clock-domain reasoning — distributed /
//! multiroom sync, the chip-AEC reference, future rate-matched lanes — not as
//! an action trigger on the software-AEC path. (Research-doc increment 2 /
//! audio-foundation review G2: this is the "measure first" surface.)
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
//! **Sign convention:** [`DacClockObserver::sro_ppm`] reports
//! `(ratio - 1) * 1e6`. Positive ppm means the DAC clock runs fast relative to
//! wall-clock (it clocks out more frames per real second than the nominal
//! reference assumes); negative means slow.
//!
//! # Why a DLL and not the least-squares [`crate::aec_clock::SroEstimator`]
//!
//! The `SroEstimator` measures DAC-vs-chip-ref drift for the *chip-AEC* path; this
//! is the *DAC-clock* path against wall-clock — a distinct clock pair. Both
//! are observe-only, but this one composes the shared [`jasper_clock::Dll`] so
//! the loop math (the spa_dll convergence + the variance-driven bandwidth + the
//! resync hard-jump) is the one shared primitive, and the ppm here is directly
//! comparable to every other DLL site's `clock.rate_diff`.

use jasper_clock::{Dll, DllConfig, DllSnapshot};

/// Below this absolute ppm the DAC playout clock is reported as near-nominal
/// (`steady`); at or beyond it, `drifting`. A DAC-clock-health threshold,
/// deliberately distinct from the chip-AEC `SroEstimator`'s own
/// `SRO_COHERENT_PPM` (a different clock pair and a different decision), so the
/// two do not share one constant. PROVISIONAL pending more on-hardware data.
pub const DAC_STEADY_PPM: f64 = 5.0;

/// A snapshot a `/state` serializer can render without holding the estimator.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DacClockSnapshot {
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

impl DacClockSnapshot {
    /// DAC-clock health: `acquiring` (not yet locked), `steady` (locked +
    /// within `DAC_STEADY_PPM` of nominal), or `drifting` (locked + beyond).
    /// A DAC-clock-health classification — deliberately distinct from the
    /// chip-AEC path's coherent/compensable verdict (a different clock pair and
    /// decision), so the doctor surfaces the two separately.
    pub fn verdict(&self) -> &'static str {
        if !self.locked {
            "acquiring"
        } else if self.sro_ppm.abs() < DAC_STEADY_PPM {
            "steady"
        } else {
            "drifting"
        }
    }
}

/// Observe-only DAC playout-clock drift observer. Composes a
/// [`jasper_clock::Dll`] driven through a VIRTUAL closed loop (no audio path is
/// touched). Pure: no ALSA, no threads, no audio side effects.
#[derive(Debug)]
pub struct DacClockObserver {
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

impl DacClockObserver {
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

    /// The raw shared-DLL snapshot — the consistent `clock.rate_diff` telemetry
    /// shape (Inc 4) every DLL instance publishes. The `/state` serializer
    /// renders this with the one shared `rate_diff` writer so all DLL sites read
    /// identically.
    pub fn dll_snapshot(&self) -> DllSnapshot {
        self.dll.snapshot()
    }

    /// An immutable snapshot for `/state` / doctor.
    pub fn snapshot(&self) -> DacClockSnapshot {
        let DllSnapshot {
            ratio_ppm,
            error_mean,
            error_var,
            locked,
            updates,
            resync_count,
            ..
        } = self.dll.snapshot();
        DacClockSnapshot {
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
    fn drive(clock: &mut DacClockObserver, secs: u64, dac_ppm: f64) {
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
        let mut clock = DacClockObserver::new(RATE, PERIOD);
        drive(&mut clock, 200, 0.0);
        let snap = clock.snapshot();
        assert!(snap.locked, "a coherent clock should lock: {snap:?}");
        assert!(
            snap.sro_ppm.abs() < 1.0,
            "coherent clock ~0 ppm, got {}",
            snap.sro_ppm
        );
        assert_eq!(snap.verdict(), "steady");
    }

    #[test]
    fn fast_dac_reads_a_steady_offset() {
        let mut clock = DacClockObserver::new(RATE, PERIOD);
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
        assert_eq!(snap.verdict(), "drifting");
    }

    #[test]
    fn slow_dac_reads_a_negative_offset() {
        let mut clock = DacClockObserver::new(RATE, PERIOD);
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
        assert_eq!(snap.verdict(), "drifting");
    }

    #[test]
    fn decimates_sub_interval_calls() {
        let mut clock = DacClockObserver::new(RATE, PERIOD);
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
        let mut clock = DacClockObserver::new(RATE, PERIOD);
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
        let mut clock = DacClockObserver::new(RATE, PERIOD);
        clock.observe(48_000, 1024, -1.0);
        clock.observe(48_000, 1024, f64::NAN);
        assert_eq!(clock.snapshot().updates, 0);
    }

    #[test]
    fn delay_exceeding_written_is_skipped_not_panic() {
        let mut clock = DacClockObserver::new(RATE, PERIOD);
        // delay > written → consumed would be negative → skip.
        clock.observe(100, 200, 1.0);
        assert_eq!(clock.snapshot().updates, 0);
    }
}
