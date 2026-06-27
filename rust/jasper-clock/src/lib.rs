// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! A pure, allocation-free clock-tracking delay-locked loop.
//!
//! This is the JTS port of PipeWire's `spa_dll`
//! (`spa/include/spa/utils/dll.h`, MIT, © 2019 Wim Taymans) — the ~20-line
//! second-order DLL it uses to reconcile two free-running audio clocks into a
//! rate ratio near 1.0. The math is lifted verbatim (see [`SpaDll`]); this
//! crate adds, on top of the bare loop, the operational hardening PipeWire's
//! ALSA plugin wraps around it: the variance-driven adaptive bandwidth and the
//! `max_resync` hard-jump.
//!
//! # Why a DLL and not a PI nudge
//!
//! Keeping two free-running clocks sample-aligned is a delay-locked-loop
//! problem by definition. A naïve first-order "nudge the rate by the buffer
//! error" loop leaves a *standing* offset under a constant rate error and
//! tends to oscillate. The DLL's third integrator (`z3`) gives **zero
//! steady-state frequency AND phase error** — a constant ppm offset settles to
//! a steady ratio with no residual drift. That property is the whole reason
//! this exists; it is pinned by the unit tests.
//!
//! # What this crate is NOT
//!
//! No I/O, no ALSA, no threads, no PipeWire dependency. It is fed a scalar
//! error each cycle and returns a scalar ratio. The *error source* (a buffer
//! fill delta, an `snd_pcm_delay` reading, a reference-vs-output timing gap)
//! and what to *do* with the ratio (drive a resampler, or merely observe) are
//! the caller's concern — this crate is the single shared loop primitive that
//! every clock-domain boundary composes.
//!
//! # Units
//!
//! The loop is dimensionless: feed it the same error units every cycle and it
//! returns a unitless ratio centred on 1.0 (`> 1.0` = run faster, `< 1.0` =
//! slower). The convenience accessor [`Dll::ratio_ppm`] reports the deviation
//! from unity in parts-per-million for telemetry.
//!
//! A DLL is meant to run *in a feedback loop*: the ratio it returns drives the
//! clock whose error it is fed, so at lock the error is nulled. Drive it open
//! loop with a fixed non-zero error and the integrators ramp without bound —
//! that is unphysical, not a bug. The closed-loop convergence (a constant
//! offset → steady ratio, zero residual) is exercised by the crate's tests.
//!
//! ```
//! use jasper_clock::{Dll, DllConfig};
//!
//! // Period and rate set the loop timescale; bandwidth auto-tunes from there.
//! let mut dll = Dll::new(DllConfig::for_rate(1024, 48_000));
//! // A perfectly-aligned clock (zero error) holds unity and locks.
//! for _ in 0..1_000 {
//!     dll.update(0.0);
//! }
//! assert!(dll.is_locked());
//! assert_eq!(dll.ratio_ppm(), 0.0);
//! ```

#![forbid(unsafe_code)]

/// PipeWire's bandwidth clamp (`SPA_DLL_BW_MAX` / `SPA_DLL_BW_MIN`). Wide to
/// acquire lock fast, narrow once locked to reject jitter.
pub const BW_MAX: f64 = 0.128;
pub const BW_MIN: f64 = 0.016;

/// The faithful `spa_dll` port: three cascaded integrators, lifted verbatim
/// from `spa/include/spa/utils/dll.h`. Holds ONLY the loop state; the adaptive
/// bandwidth and resync policy live in [`Dll`]. Keeping the bare loop separate
/// keeps the port auditable against upstream line-for-line.
///
/// Upstream also caches the last-set `bw` in the struct for its own retune
/// bookkeeping; here the [`Dll`] wrapper owns the live bandwidth, so this bare
/// struct carries only the integrator/coefficient state to stay DRY.
#[derive(Debug, Clone, Copy)]
struct SpaDll {
    z1: f64,
    z2: f64,
    z3: f64,
    w0: f64,
    w1: f64,
    w2: f64,
}

impl SpaDll {
    /// `spa_dll_init`: zero the integrators.
    fn new() -> Self {
        Self {
            z1: 0.0,
            z2: 0.0,
            z3: 0.0,
            w0: 0.0,
            w1: 0.0,
            w2: 0.0,
        }
    }

    /// `spa_dll_set_bw`: recompute the loop coefficients for a bandwidth.
    ///
    /// `w = 2*PI*bw*period/rate`, `w0 = 1 - exp(-20*w)`, `w1 = w*1.5/period`,
    /// `w2 = w/1.5`. Setting the bandwidth does NOT disturb the integrator
    /// state, so it can be retuned live without a phase glitch.
    fn set_bw(&mut self, bw: f64, period: f64, rate: f64) {
        let w = 2.0 * std::f64::consts::PI * bw * period / rate;
        self.w0 = 1.0 - (-20.0 * w).exp();
        self.w1 = w * 1.5 / period;
        self.w2 = w / 1.5;
    }

    /// `spa_dll_update`: advance the three integrators by one error sample and
    /// return the corrected ratio (`1.0 - (z2 + z3)`).
    fn update(&mut self, err: f64) -> f64 {
        self.z1 += self.w0 * (self.w1 * err - self.z1);
        self.z2 += self.w0 * (self.z1 - self.z2);
        self.z3 += self.w2 * self.z2;
        1.0 - (self.z2 + self.z3)
    }
}

/// Tuning for a [`Dll`]. The first three fields set the loop timescale and
/// bandwidth band; the last two are the operational hardening this crate adds
/// over the bare `spa_dll`.
#[derive(Debug, Clone, Copy)]
pub struct DllConfig {
    /// Nominal cycle size in error-unit samples (PipeWire's quantum/period).
    /// Only sets the loop timescale with `rate`; it is not a buffer bound.
    pub period: f64,
    /// Nominal rate the period is measured against (e.g. 48000). With `period`
    /// this fixes the loop's natural timescale.
    pub rate: f64,
    /// Initial bandwidth, used to acquire lock before the adaptive retune
    /// narrows it. Clamped to `[BW_MIN, BW_MAX]`.
    pub initial_bw: f64,
    /// How many `update` calls between adaptive-bandwidth retunes. PipeWire
    /// retunes every 3–5 s; at one error sample per cycle this is that window
    /// expressed in cycles. `0` disables the adaptive retune (fixed bandwidth).
    pub bw_retune_period: u64,
    /// Slew clamp: the error fed to the integrators is limited to
    /// `[-max_error, max_error]` so a single large transient produces a bounded
    /// correction rather than slamming the loop. This is PipeWire's `max_error`
    /// (`SPA_MAX(256, (threshold+headroom)/2)`), and it is what keeps the
    /// conditionally-stable loop stable under a big fill excursion. The raw
    /// (unclamped) error still drives the error statistics and the resync test.
    /// `0` (or non-finite) disables the clamp.
    pub max_error: f64,
    /// Hard-jump threshold: if a single RAW error exceeds this magnitude the
    /// loop has lost lock (an xrun, a device reset, a discontinuity) and is
    /// re-initialised rather than slewed toward the new operating point.
    ///
    /// JTS DIVERGENCE from upstream (named after PipeWire's `max_resync`
    /// concept but NOT its behavior): PipeWire, on `err > max_resync`, sets a
    /// resync flag, clamps `err` to `max_error`, and *still* feeds it to
    /// `spa_dll_update` (it keeps slewing). We instead re-initialise the loop
    /// and skip the offending sample entirely — a frank discontinuity (xrun,
    /// device reset) is better served by re-locking from scratch than by
    /// slewing through a clamped spike. `max_resync >= max_error` so the slew
    /// clamp engages first and the hard-jump is reserved for true
    /// discontinuities. `0` (or non-finite) disables the jump.
    pub max_resync: f64,
}

impl DllConfig {
    /// Default tuning for an audio clock at `rate` Hz scheduled in `period`
    /// frames. Acquires at the max bandwidth and retunes roughly every
    /// `rate / period` cycles (≈ 1 s).
    pub fn for_rate(period: u32, rate: u32) -> Self {
        let period = period.max(1) as f64;
        let rate = rate.max(1) as f64;
        // ≈ 1 s of cycles, floored at 1 so the retune always eventually runs.
        let bw_retune_period = (rate / period).round().max(1.0) as u64;
        // PipeWire's max_error floor is 256 frames or half a quantum, whichever
        // is larger; the slew clamp keeps the loop stable under a fill spike.
        let max_error = 256.0_f64.max(period / 2.0);
        Self {
            period,
            rate,
            initial_bw: BW_MAX,
            bw_retune_period,
            max_error,
            // The hard-jump is reserved for an error past a FULL period — a
            // frank discontinuity, well above the slew clamp.
            max_resync: period.max(max_error),
        }
    }
}

/// An immutable snapshot of a [`Dll`]'s observable state. The single shape
/// every consumer publishes on `/state` / doctor (DRY telemetry, increment 4);
/// it mirrors PipeWire's `clock.rate_diff` plus the error statistics and the
/// resync/lock counters JTS surfaces.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DllSnapshot {
    /// Correction ratio (1.0 = no correction). `> 1.0` runs faster.
    pub ratio: f64,
    /// `(ratio - 1) * 1e6` — the rate difference in ppm (`clock.rate_diff`).
    pub ratio_ppm: f64,
    /// Running mean of recent errors (exponential, the same averaging the
    /// adaptive bandwidth uses).
    pub error_mean: f64,
    /// Running variance of recent errors.
    pub error_var: f64,
    /// Current loop bandwidth after adaptive retuning.
    pub bandwidth: f64,
    /// Whether the loop is currently locked (acquired AND low residual error).
    pub locked: bool,
    /// Total error samples fed since construction / last reset.
    pub updates: u64,
    /// Times the loop crossed from unlocked → locked.
    pub lock_count: u64,
    /// Times the loop crossed from locked → unlocked.
    pub unlock_count: u64,
    /// Times a `max_resync` hard-jump re-initialised the loop.
    pub resync_count: u64,
}

/// Number of `update` calls a freshly-(re)initialised loop must run before its
/// lock verdict is trusted. Below this the integrators are still filling and a
/// transient low error would mislead a lock decision.
const LOCK_WARMUP_UPDATES: u64 = 64;

/// The shared clock-tracking loop: a [`SpaDll`] plus the adaptive bandwidth,
/// the `max_resync` hard-jump, running error statistics, and lock tracking.
///
/// Fed one scalar error per cycle via [`Dll::update`]; returns the corrected
/// ratio. Pure and allocation-free — every consumer in `jasper-outputd`
/// composes this with its own error source and reads [`Dll::snapshot`] for
/// `/state`.
#[derive(Debug, Clone)]
pub struct Dll {
    config: DllConfig,
    dll: SpaDll,
    // Exponential running error statistics. The averaging constant is the
    // loop's own w0 so the stats track the loop's settling timescale.
    err_avg: f64,
    err_var: f64,
    avg_coeff: f64,
    ratio: f64,
    bandwidth: f64,
    locked: bool,
    updates: u64,
    updates_since_retune: u64,
    lock_count: u64,
    unlock_count: u64,
    resync_count: u64,
}

impl Dll {
    /// Construct a loop with the given tuning and acquire-bandwidth.
    pub fn new(config: DllConfig) -> Self {
        let initial_bw = clamp_bw(config.initial_bw);
        let mut dll = SpaDll::new();
        dll.set_bw(initial_bw, config.period, config.rate);
        // Track the error statistics on the loop's own settling timescale.
        // w0 is the loop's per-cycle averaging weight; reuse it so the stats
        // and the loop agree on "recent".
        let avg_coeff = dll.w0.clamp(1e-4, 1.0);
        Self {
            config,
            dll,
            err_avg: 0.0,
            err_var: 0.0,
            avg_coeff,
            ratio: 1.0,
            bandwidth: initial_bw,
            locked: false,
            updates: 0,
            updates_since_retune: 0,
            lock_count: 0,
            unlock_count: 0,
            resync_count: 0,
        }
    }

    /// Re-initialise the loop, discarding integrator and statistics state but
    /// keeping the configuration and the lifetime counters (`lock_count`,
    /// `resync_count`, …). This is `spa_dll_init` + a bandwidth reset — call it
    /// on a hard discontinuity (xrun recovery, device re-open) so the loop
    /// re-locks from scratch instead of slewing from a stale operating point.
    pub fn reset(&mut self) {
        let initial_bw = clamp_bw(self.config.initial_bw);
        self.dll = SpaDll::new();
        self.dll
            .set_bw(initial_bw, self.config.period, self.config.rate);
        self.avg_coeff = self.dll.w0.clamp(1e-4, 1.0);
        self.err_avg = 0.0;
        self.err_var = 0.0;
        self.ratio = 1.0;
        self.bandwidth = initial_bw;
        if self.locked {
            self.locked = false;
            self.unlock_count += 1;
        }
        self.updates = 0;
        self.updates_since_retune = 0;
    }

    /// Feed one error sample and return the corrected ratio.
    ///
    /// A non-finite error is ignored (the prior ratio is returned) so a single
    /// bad reading can never poison the integrators. An error whose magnitude
    /// exceeds `max_resync` triggers a hard re-lock instead of a slew; otherwise
    /// the error is clamped to `max_error` before driving the integrators (the
    /// slew clamp that keeps the conditionally-stable loop stable), while the
    /// RAW error drives the error statistics and the lock verdict.
    pub fn update(&mut self, err: f64) -> f64 {
        if !err.is_finite() {
            return self.ratio;
        }

        if self.is_resync(err) {
            self.reset();
            self.resync_count += 1;
            // After a resync we do not slew toward the offending sample; the
            // loop restarts and the next in-band error drives it. Return unity.
            self.ratio = 1.0;
            return self.ratio;
        }

        // Slew clamp: the integrators only ever see a bounded error. The raw
        // error (below) still feeds the statistics and the lock verdict, so a
        // clamped excursion is still visible as drift in telemetry.
        let clamped = self.clamp_error(err);
        self.ratio = self.dll.update(clamped);
        self.accumulate_error(err);
        self.updates += 1;
        self.updates_since_retune += 1;
        self.maybe_retune_bandwidth();
        self.update_lock(err);
        self.ratio
    }

    fn is_resync(&self, err: f64) -> bool {
        let max_resync = self.config.max_resync;
        max_resync.is_finite() && max_resync > 0.0 && err.abs() > max_resync
    }

    fn clamp_error(&self, err: f64) -> f64 {
        let max_error = self.config.max_error;
        if max_error.is_finite() && max_error > 0.0 {
            err.clamp(-max_error, max_error)
        } else {
            err
        }
    }

    /// Exponential mean/variance update on the loop's own averaging weight.
    fn accumulate_error(&mut self, err: f64) {
        let delta = err - self.err_avg;
        self.err_avg += self.avg_coeff * delta;
        // West-style EW variance: track the squared deviation from the new
        // mean toward the same coefficient. Non-negative by construction.
        self.err_var = (1.0 - self.avg_coeff) * (self.err_var + self.avg_coeff * delta * delta);
    }

    /// PipeWire's variance-driven retune: `bw = (|err_avg| + sqrt(err_var)) /
    /// 1000`, clamped to `[BW_MIN, BW_MAX]`. Wide while acquiring (large error
    /// / variance), narrow once locked (small, steady error).
    fn maybe_retune_bandwidth(&mut self) {
        let retune_period = self.config.bw_retune_period;
        if retune_period == 0 || self.updates_since_retune < retune_period {
            return;
        }
        self.updates_since_retune = 0;
        let target_bw = clamp_bw((self.err_avg.abs() + self.err_var.max(0.0).sqrt()) / 1000.0);
        // Only touch the loop coefficients if the bandwidth actually moved —
        // set_bw is cheap but this keeps the retune a no-op when settled.
        if (target_bw - self.bandwidth).abs() > f64::EPSILON {
            self.bandwidth = target_bw;
            self.dll
                .set_bw(target_bw, self.config.period, self.config.rate);
        }
    }

    /// Lock verdict: acquired (past warmup) AND the recent error is small
    /// relative to the period. Edge-triggers the lock/unlock counters.
    fn update_lock(&mut self, _err: f64) {
        let was_locked = self.locked;
        // "Small" = within ~0.1% of a period of residual error. The DLL drives
        // the *steady-state* error to zero, so a locked loop sits well inside
        // this; an acquiring or disturbed loop does not.
        let lock_threshold = (self.config.period * 1e-3).max(1e-6);
        let acquired = self.updates >= LOCK_WARMUP_UPDATES;
        let low_error =
            self.err_avg.abs() < lock_threshold && self.err_var.max(0.0).sqrt() < lock_threshold;
        self.locked = acquired && low_error;
        if self.locked && !was_locked {
            self.lock_count += 1;
        } else if !self.locked && was_locked {
            self.unlock_count += 1;
        }
    }

    /// Current correction ratio (1.0 = no correction).
    pub fn ratio(&self) -> f64 {
        self.ratio
    }

    /// Rate difference in ppm (`(ratio - 1) * 1e6`) — PipeWire's
    /// `clock.rate_diff`.
    pub fn ratio_ppm(&self) -> f64 {
        (self.ratio - 1.0) * 1.0e6
    }

    /// Running mean of recent errors.
    pub fn error_mean(&self) -> f64 {
        self.err_avg
    }

    /// Running variance of recent errors.
    pub fn error_variance(&self) -> f64 {
        self.err_var.max(0.0)
    }

    /// Current adaptive loop bandwidth.
    pub fn bandwidth(&self) -> f64 {
        self.bandwidth
    }

    /// Whether the loop is currently locked.
    pub fn is_locked(&self) -> bool {
        self.locked
    }

    /// Times a `max_resync` hard-jump re-initialised the loop.
    pub fn resync_count(&self) -> u64 {
        self.resync_count
    }

    /// Times the loop crossed unlocked → locked.
    pub fn lock_count(&self) -> u64 {
        self.lock_count
    }

    /// One immutable snapshot of every observable field. The single telemetry
    /// shape consumers serialize (increment 4).
    pub fn snapshot(&self) -> DllSnapshot {
        DllSnapshot {
            ratio: self.ratio,
            ratio_ppm: self.ratio_ppm(),
            error_mean: self.err_avg,
            error_var: self.error_variance(),
            bandwidth: self.bandwidth,
            locked: self.locked,
            updates: self.updates,
            lock_count: self.lock_count,
            unlock_count: self.unlock_count,
            resync_count: self.resync_count,
        }
    }
}

fn clamp_bw(bw: f64) -> f64 {
    if !bw.is_finite() {
        return BW_MIN;
    }
    bw.clamp(BW_MIN, BW_MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn audio_dll() -> Dll {
        Dll::new(DllConfig::for_rate(1024, 48_000))
    }

    const PERIOD: f64 = 1024.0;

    /// Closed-loop clock simulator — the faithful test harness for a DLL.
    ///
    /// A DLL is meant to live in a feedback loop: the ratio it returns *drives*
    /// the clock whose error it is fed, so a settled loop has its error nulled.
    /// Testing it open-loop (handing it a fixed error forever) is unphysical —
    /// the third integrator ramps without bound because nothing cancels the
    /// forcing.
    ///
    /// Model (PipeWire's capture / negative-feedback convention): a producer
    /// writes `PERIOD * (1 + ppm/1e6)` frames per cycle into a ring held at
    /// `TARGET` fill; our consumer drains `PERIOD * ratio`. The error the loop
    /// is fed is `TARGET - fill` (negative feedback: fill too high → positive
    /// error → speed the consumer up). At lock the consumer ratio matches the
    /// producer's `ppm` and the fill error nulls — the z3 property. Returns the
    /// per-cycle ratio history.
    fn run_closed_loop(dll: &mut Dll, ppm: f64, cycles: usize) -> Vec<f64> {
        const TARGET: f64 = 4096.0;
        let producer_per_cycle = PERIOD * (1.0 + ppm / 1.0e6);
        let mut fill = TARGET;
        let mut ratio = 1.0_f64;
        let mut ratios = Vec::with_capacity(cycles);
        for _ in 0..cycles {
            // Producer writes; consumer drains at the previous ratio.
            fill += producer_per_cycle - ratio * PERIOD;
            // Negative feedback: error pushes the consumer toward target fill.
            ratio = dll.update(TARGET - fill);
            ratios.push(ratio);
        }
        ratios
    }

    /// The defining property: a constant clock offset settles to a steady ratio
    /// with ZERO residual phase error (the z3 third-integrator property). A
    /// first-order loop CANNOT do this — it leaves a standing offset.
    #[test]
    fn constant_offset_converges_to_zero_residual() {
        let mut dll = audio_dll();
        run_closed_loop(&mut dll, 50.0, 60_000);
        // The loop drove its own residual phase error to ~0 despite the
        // constant +50 ppm forcing — only possible with the phase+frequency
        // integrators. A first-order nudge loop would sit at a standing offset.
        assert!(
            dll.error_mean().abs() < 1e-2,
            "residual error should vanish, got mean={}",
            dll.error_mean()
        );
        assert!(
            dll.is_locked(),
            "loop should report locked after convergence"
        );
    }

    /// The ratio tracks the offset with the right magnitude AND sign: a source
    /// running +50 ppm fast is matched by an output ratio ~50 ppm fast (SAME
    /// direction). A faster-filling ring needs a faster consumer to hold the
    /// fill level steady — the ratio nulls the standing error, it does not
    /// invert the offset.
    #[test]
    fn tracks_a_constant_offset_without_standing_error() {
        for ppm in [-120.0, -50.0, 50.0, 120.0] {
            let mut dll = audio_dll();
            run_closed_loop(&mut dll, ppm, 80_000);
            assert!(
                dll.error_mean().abs() < 1e-2,
                "standing error should be driven out at {ppm} ppm, got {}",
                dll.error_mean()
            );
            // Output ratio runs the SAME direction as the source offset: a
            // faster producer needs a faster consumer to hold the fill steady.
            assert!(
                (dll.ratio_ppm() - ppm).abs() < 3.0,
                "ratio should match ~{ppm} ppm, got {} ppm",
                dll.ratio_ppm()
            );
        }
    }

    /// The failure mode the DLL exists to avoid: NO sustained oscillation. In a
    /// closed loop the settled ratio must converge monotonically to its target,
    /// not ring around it — once settled, the ratio's sign of change must not
    /// keep flipping. (A badly-tuned first-order loop ping-pongs across target.)
    #[test]
    fn does_not_oscillate_under_constant_forcing() {
        let mut dll = audio_dll();
        let ratios = run_closed_loop(&mut dll, 50.0, 80_000);
        // Look only at the settled tail.
        let tail = &ratios[70_000..];
        let target: f64 = tail.iter().sum::<f64>() / tail.len() as f64;
        // Count sign flips of (ratio - target). Sustained oscillation produces
        // many; a settled, jitter-free loop produces ~none.
        let mut sign_flips = 0usize;
        let mut prev_sign = 0i8;
        for r in tail {
            let s = ((r - target).signum()) as i8;
            if s != 0 && prev_sign != 0 && s != prev_sign {
                sign_flips += 1;
            }
            if s != 0 {
                prev_sign = s;
            }
        }
        // Allow a handful from f64 rounding at the noise floor; reject ringing.
        assert!(
            sign_flips < 50,
            "settled ratio oscillates ({sign_flips} sign flips across target)"
        );
        // And the settled spread is tiny.
        let var: f64 = tail
            .iter()
            .map(|r| (r - target) * (r - target))
            .sum::<f64>()
            / tail.len() as f64;
        assert!(var < 1e-16, "settled ratio variance too large: {var}");
    }

    /// Overshoot bound: acquiring the lock from cold must not wildly overshoot
    /// the target ratio (an unstable / under-damped loop would). The peak
    /// correction stays within a small multiple of the steady-state target.
    #[test]
    fn acquisition_does_not_wildly_overshoot() {
        let mut dll = audio_dll();
        let ratios = run_closed_loop(&mut dll, 50.0, 80_000);
        let target_ppm = (ratios.last().unwrap() - 1.0) * 1.0e6;
        let peak_ppm = ratios
            .iter()
            .map(|r| (r - 1.0) * 1.0e6)
            .fold(0.0_f64, |m, p| m.max(p.abs()));
        // A critically/over-damped DLL overshoots modestly; cap at 3x target.
        assert!(
            peak_ppm <= target_ppm.abs() * 3.0 + 1.0,
            "acquisition overshoot too large: peak={peak_ppm} ppm target={target_ppm} ppm"
        );
    }

    /// The adaptive bandwidth must NARROW after lock: it acquires wide (to lock
    /// fast) then shrinks toward BW_MIN once the closed-loop error and its
    /// variance are small (to reject jitter).
    #[test]
    fn bandwidth_narrows_after_lock() {
        let mut dll = audio_dll();
        // Starts at the acquire (max) bandwidth.
        assert!(
            (dll.bandwidth() - BW_MAX).abs() < 1e-9,
            "should acquire at BW_MAX, got {}",
            dll.bandwidth()
        );
        // A locked closed loop drives the error → 0, so |avg| and variance
        // shrink → the retune narrows the bandwidth to the BW_MIN floor.
        run_closed_loop(&mut dll, 50.0, 80_000);
        assert!(
            dll.is_locked(),
            "loop should lock before checking bandwidth"
        );
        assert!(
            dll.bandwidth() < BW_MAX,
            "bandwidth should narrow after lock, still {}",
            dll.bandwidth()
        );
        assert!(
            dll.bandwidth() >= BW_MIN,
            "bandwidth must respect BW_MIN, got {}",
            dll.bandwidth()
        );
    }

    /// The slew clamp (`max_error`) bounds the per-cycle correction even when a
    /// transient error spikes below the resync threshold — the loop must not
    /// slam, it slews. We feed a large-but-sub-resync error and confirm the
    /// integrator only moved by a bounded amount.
    #[test]
    fn slew_clamp_bounds_a_sub_resync_spike() {
        let cfg = DllConfig::for_rate(1024, 48_000);
        // A spike between max_error and max_resync: clamped (not a resync).
        let spike = (cfg.max_error + cfg.max_resync) / 2.0;
        assert!(spike > cfg.max_error && spike <= cfg.max_resync);
        let mut clamped_dll = Dll::new(cfg);
        let r_clamped = clamped_dll.update(spike);
        assert_eq!(
            clamped_dll.resync_count(),
            0,
            "sub-resync spike must not resync"
        );

        // The same spike with the clamp disabled moves the ratio much further.
        let mut unclamped_cfg = cfg;
        unclamped_cfg.max_error = 0.0;
        let mut unclamped_dll = Dll::new(unclamped_cfg);
        let r_unclamped = unclamped_dll.update(spike);
        assert!(
            (r_clamped - 1.0).abs() < (r_unclamped - 1.0).abs(),
            "clamp must bound the correction: clamped={r_clamped} unclamped={r_unclamped}"
        );
        // But the RAW spike is still visible in the error statistics (it is not
        // hidden by the clamp), so telemetry still sees the excursion.
        assert!(clamped_dll.error_mean().abs() > 0.0);
    }

    /// The default config keeps `max_resync >= max_error` (slew clamp engages
    /// before the hard-jump) — the ordering PipeWire relies on.
    #[test]
    fn default_config_orders_clamp_below_resync() {
        let cfg = DllConfig::for_rate(1024, 48_000);
        assert!(cfg.max_error > 0.0);
        assert!(
            cfg.max_resync >= cfg.max_error,
            "max_resync ({}) must be >= max_error ({})",
            cfg.max_resync,
            cfg.max_error
        );
        assert!(cfg.bw_retune_period >= 1);
        assert!((cfg.initial_bw - BW_MAX).abs() < 1e-12);
    }

    /// A huge transient error past `max_resync` must hard-jump (reset) the loop
    /// rather than slew toward it, and bump the resync counter.
    #[test]
    fn resync_jump_fires_past_max_resync() {
        let mut dll = audio_dll();
        // Establish a lock first (closed loop on a small offset).
        run_closed_loop(&mut dll, 30.0, 60_000);
        assert!(dll.is_locked(), "loop should lock before the discontinuity");
        let resyncs_before = dll.resync_count();

        // A discontinuity: error far exceeds max_resync (== period == 1024).
        let ratio = dll.update(50_000.0);
        assert_eq!(
            dll.resync_count(),
            resyncs_before + 1,
            "a past-max_resync error must trigger one resync"
        );
        // The loop re-initialised: ratio back to unity, no longer locked.
        assert!((ratio - 1.0).abs() < 1e-12, "resync returns unity ratio");
        assert!(!dll.is_locked(), "resync drops lock");
        // The integrators were cleared, so the loop re-acquires from scratch
        // rather than continuing from stale state.
        run_closed_loop(&mut dll, 30.0, 60_000);
        assert!(dll.is_locked(), "loop re-locks after a resync");
    }

    /// max_resync == 0 disables the hard-jump: a large error slews instead.
    #[test]
    fn resync_disabled_when_threshold_zero() {
        let mut cfg = DllConfig::for_rate(1024, 48_000);
        cfg.max_resync = 0.0;
        let mut dll = Dll::new(cfg);
        let before = dll.resync_count();
        dll.update(1.0e9);
        assert_eq!(
            dll.resync_count(),
            before,
            "resync must stay off at zero threshold"
        );
    }

    /// A non-finite error is ignored, never poisoning the integrators.
    #[test]
    fn non_finite_error_is_ignored() {
        let mut dll = audio_dll();
        for _ in 0..1000 {
            dll.update(1.0);
        }
        let ratio_before = dll.ratio();
        let updates_before = dll.snapshot().updates;
        let r = dll.update(f64::NAN);
        assert_eq!(r, ratio_before, "NaN returns the prior ratio unchanged");
        assert_eq!(
            dll.snapshot().updates,
            updates_before,
            "NaN does not count as an update"
        );
        let r = dll.update(f64::INFINITY);
        assert_eq!(r, ratio_before, "inf returns the prior ratio unchanged");
    }

    /// Zero error keeps the ratio at exactly unity (no spurious correction).
    #[test]
    fn zero_error_holds_unity() {
        let mut dll = audio_dll();
        for _ in 0..10_000 {
            let r = dll.update(0.0);
            assert!(
                (r - 1.0).abs() < 1e-12,
                "zero error must hold unity, got {r}"
            );
        }
        assert_eq!(dll.ratio_ppm(), 0.0);
        assert!(dll.is_locked(), "a perfectly-aligned clock locks");
    }

    /// reset() clears loop + stats but preserves the lifetime resync counter.
    #[test]
    fn reset_clears_state_keeps_lifetime_counters() {
        let mut dll = audio_dll();
        for _ in 0..40_000 {
            dll.update(2.0);
        }
        // Force a resync to bump the lifetime counter.
        dll.update(50_000.0);
        let resyncs = dll.resync_count();
        assert!(resyncs >= 1);
        // Re-lock then explicit reset.
        for _ in 0..40_000 {
            dll.update(2.0);
        }
        dll.reset();
        assert_eq!(dll.ratio(), 1.0);
        assert_eq!(dll.error_mean(), 0.0);
        assert!(!dll.is_locked());
        assert_eq!(
            dll.resync_count(),
            resyncs,
            "reset preserves the lifetime resync counter"
        );
        assert!(
            (dll.bandwidth() - BW_MAX).abs() < 1e-9,
            "reset returns to acquire bandwidth"
        );
    }

    /// The bare spa_dll port reproduces the upstream coefficients exactly.
    #[test]
    fn spa_dll_coefficients_match_upstream_formula() {
        let mut d = SpaDll::new();
        let bw = 0.05;
        let period = 1024.0;
        let rate = 48_000.0;
        d.set_bw(bw, period, rate);
        let w = 2.0 * std::f64::consts::PI * bw * period / rate;
        assert!((d.w0 - (1.0 - (-20.0 * w).exp())).abs() < 1e-15);
        assert!((d.w1 - (w * 1.5 / period)).abs() < 1e-15);
        assert!((d.w2 - (w / 1.5)).abs() < 1e-15);
        // The bare update returns 1 - (z2 + z3) and is finite for a finite err.
        assert!(d.update(1.0).is_finite());
    }

    /// Error statistics stay non-negative (variance) and finite under a noisy
    /// error stream — they must never feed a NaN into the bandwidth retune.
    #[test]
    fn error_statistics_stay_finite_and_nonnegative() {
        let mut dll = audio_dll();
        for i in 0..50_000 {
            // Pseudo-noise: deterministic, bounded, sign-alternating.
            let x = ((i as f64) * 0.7).sin() * 4.0;
            dll.update(x);
        }
        assert!(dll.error_mean().is_finite());
        assert!(dll.error_variance().is_finite());
        assert!(dll.error_variance() >= 0.0);
        assert!(dll.bandwidth() >= BW_MIN && dll.bandwidth() <= BW_MAX);
    }
}
