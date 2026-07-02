// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Host-slaved USB clock — the daemon-agnostic core of Stage 1 of the USB
//! low-latency foundation.
//!
//! Default OFF. When enabled it steers the UAC2 gadget's asynchronous feedback
//! endpoint (the writable ALSA control `"Capture Pitch 1000000"`, iface=PCM,
//! numid=1) so the HOST (Mac / Windows PC) matches OUR local DAC rate, instead
//! of a lane resampler having to reconcile a standing rate offset in software.
//! The mechanism is a slow delay-locked loop over the gadget ring fill; the
//! ladder around it is a per-session compliance probe that refuses to trust a
//! host that ignores the feedback.
//!
//! # Which daemon owns this
//!
//! This crate holds the pure, I/O-free ladder/servo ([`HostClock`]) plus the
//! feature-gated ALSA actuator ([`AlsaPitchCtl`], behind `feature = "alsa"`).
//! It is consumed by BOTH USB clock owners:
//! - **solo (aloop) mode**: `jasper-usbsink-audio` owns the gadget capture and
//!   drives this from its state publisher (`JASPER_USBSINK_HOST_CLOCK`).
//! - **combo (USB DIRECT) mode**: `jasper-fanin` owns the gadget capture and
//!   drives this from a dedicated thread (`JASPER_FANIN_HOST_CLOCK`).
//!
//! The invariant pinned across both: **the daemon that owns the gadget capture
//! owns the pitch ctl.** Only one drives it at a time. Each daemon parses its
//! own `JASPER_*` env keys and builds a [`HostClockConfig`]; this crate is
//! parameterized on a single thing that differs between them — the `event=` log
//! prefix (`usbsink_audio` / `fanin`).
//!
//! # What this module does and does NOT touch
//!
//! It reuses the daemon's EXISTING error-signal atomics as its input via
//! [`Obs`] — the audio thread is untouched. It writes ONLY the pitch ctl, and
//! ONLY from a single owning thread (single writer by construction — the ctl
//! handle lives on that thread and nowhere else). It does not resize, bypass,
//! or modify the fan-in `lane_resampler` cushion; it does not touch
//! fan-in / outputd / CamillaDSP. Shrinking the cushion is a separate,
//! measurement-gated follow-up.
//!
//! # Two controllers in cascade — the review hotspot, and the defense
//!
//! With the feature enabled, the fan-in `lane_resampler` (fast inner loop) and
//! this pitch DLL (slow outer loop) both discipline the same audio chain. JTS
//! has a documented oscillation failure class when two rate controllers fight
//! (`docs/HANDOFF-usb-low-latency.md`; the CamillaDSP `rate_adjust` +
//! `AsyncSinc` incident). This is NOT that: it is a legitimate CASCADE —
//! a fast inner loop that absorbs residual + jitter, and a slow outer loop that
//! removes the standing rate offset at its source (the host). The defense is in
//! the numbers, derived from the actual inner-loop constant:
//!
//! ## Inner loop (cited)
//!
//! `rust/jasper-fanin/src/lane_resampler.rs` builds
//! `jasper_resampler::RateController::with_max_resync(max_ppm, period_frames,
//! sample_rate, Some(0.0))`, whose loop is
//! `jasper_clock::DllConfig::for_rate(256, 48000)` (JASPER_FANIN_PERIOD_FRAMES
//! defaults to 256; sample_rate 48000). It is updated once per rendered period,
//! i.e. every `256 / 48000 s ≈ 5.33 ms`. In the spa_dll formulation the config
//! `bw` IS the closed-loop bandwidth in Hz, adaptively clamped to
//! `[BW_MIN, BW_MAX] = [0.016, 0.128] Hz` (jasper-clock `lib.rs`). Its locked
//! floor is **0.016 Hz**; its acquiring maximum is **0.128 Hz**.
//!
//! ## Outer loop (this module)
//!
//! `Dll::new(DllConfig { period: 4800, rate: 48000, initial_bw: BW_MIN,
//! bw_retune_period: 0, max_error: 0, max_resync: 0 })` ticked at exactly 1 Hz.
//! With adaptive retune DISABLED (`bw_retune_period = 0`) the bandwidth is fixed
//! at `BW_MIN = 0.016 Hz` in the DLL's own timescale, and the *effective*
//! bandwidth referred to wall-clock ticks is
//! `bw · (period / rate) / T_tick = 0.016 × (4800 / 48000) / 1 s = 0.0016 Hz`,
//! deterministic and testable. That is **10× below the inner loop's locked
//! floor and 80× below its acquiring maximum — ≥10× separation in EVERY inner
//! state.** The slow settle is deliberate: PipeWire's docs warn UAC2 pitch
//! oscillates at a normal DLL bandwidth, and Windows `usbaudio2.sys` reacts with
//! a ~163 ppm deadband, so a wide/fast outer loop would ring against the host.
//!
//! ## Feed-forward so the slow loop does not rail the 3-period ring
//!
//! At 0.0016 Hz the DLL alone would take ~100 s to correct a standing offset —
//! long enough for the tiny 3×256-frame gadget ring to rail. So the probe's
//! neutral baseline phase measures the raw host rate offset and, on entering
//! `L0_LOCKED`, seeds the commanded bias with `-baseline_slope` (feed-forward).
//! Coarse correction is immediate; the 0.0016 Hz DLL only trims the residual.
//!
//! ## The falsifier
//!
//! `fill_variance` (EW variance of the gadget fill) and `fill_slope_ppm` are
//! published every enabled tick precisely so a soak can DETECT a cascade
//! limit-cycle: a two-controller oscillation shows up as periodic fill variance
//! the counters make visible. If a soak ever shows that, the answer is to widen
//! the separation or disable — the mechanism ships default-OFF for exactly this
//! reason.
//!
//! # Cross-platform conditions
//!
//! - **macOS**: honors asynchronous feedback well (the gold path).
//! - **Windows** (`usbaudio2.sys`): honors feedback dynamically but with a
//!   ~163 ppm reaction deadband and IGNORES commanded values outside roughly
//!   nominal ±1 sample/interval, so the steady-state commanded bias MUST stay
//!   inside a ±1000 ppm validity window (enforced by [`MAX_BIAS_PPM`]).
//! - Both react slowly ⇒ the low outer-loop bandwidth above.
//!
//! Per-session probe rationale: the host OS or the playing application can
//! change between sessions (a Mac unplugged and a Windows box plugged in; an
//! app that opens the endpoint in a mode that pins the rate). So compliance is
//! re-measured on every `(host_connected && playing)` edge rather than trusted
//! once at boot.
//!
//! Prior art: Pavel Hofman's `gaudio_ctl` demonstrates the gadget-side pitch
//! actuator; the DLL is JTS's own `jasper_clock` (a PipeWire `spa_dll` port).

use jasper_clock::{Dll, DllConfig, BW_MIN};

// ---- Pinned non-env constants (contract §2; tests assert these) ------------

/// The neutral pitch value: 1× nominal, no bias. Writing this un-slaves the
/// host. Verified live on jts.local (kernel 6.12.75): the ctl range is
/// 750000..1005000 with 1_000_000 the identity point.
pub const PITCH_NEUTRAL: i64 = 1_000_000;

/// Servo clamp: the total commanded bias (feed-forward + DLL trim) never leaves
/// ±this ppm. This is the Windows validity window, INTENTIONALLY tighter than
/// the hardware ctl range (750000..1005000 around neutral 1_000_000, i.e.
/// −250000 ppm to +5000 ppm) — a value outside ±1000 ppm is silently ignored
/// by `usbaudio2.sys`, so commanding it would be worse than useless.
pub const MAX_BIAS_PPM: f64 = 1000.0;

/// Write-suppression epsilon: a new command within this many ppm of the last
/// WRITTEN value is not re-written (no ctl spam). Reset paths bypass this.
pub const WRITE_EPSILON_PPM: f64 = 10.0;

/// Minimum wall-clock interval between non-reset ctl writes (≤ 1 Hz). Reset
/// paths (startup / shutdown / disable / demotion / idle / probe edges) bypass
/// this to force an immediate write.
pub const WRITE_MIN_INTERVAL_MS: u64 = 1000;

/// The host-clock control tick. The owning thread wakes on a short cadence; the
/// host-clock logic runs only once per this interval.
pub const TICK_INTERVAL_MS: u64 = 1000;

/// L1_WARN raw-demand threshold: if the loop's UNCLAMPED demand stays above this
/// for [`L1_SUSTAIN_TICKS`] the ppm is "unusually high" (a marginal host, or a
/// large real crystal offset). Warn surface only — commanding continues clamped.
pub const L1_WARN_PPM: f64 = 2500.0;

/// L1 → L0 release hysteresis: raw demand must fall back below this to clear the
/// warn. The gap to [`L1_WARN_PPM`] prevents flip-flop at the boundary.
pub const L1_RELEASE_PPM: f64 = 2000.0;

/// Consecutive ticks the raw demand must exceed [`L1_WARN_PPM`] to raise L1.
pub const L1_SUSTAIN_TICKS: u32 = 30;

/// Mid-stream demotion evidence: consecutive ticks with a SATURATED command
/// (|commanded| == MAX_BIAS_PPM) AND a fill slope still worse than the L2 floor
/// in the uncorrected direction ⇒ the host is not honoring the command → L2.
pub const L2_SUSTAIN_TICKS: u32 = 10;

/// Absolute mid-stream-demotion slope floor, in ppm, DECOUPLED from
/// [`HostClockConfig::probe_ppm`]. Demotion asks a physical question — "is the
/// ring still diverging fast enough, while the command is railed, that the host
/// clearly is not following?" — whose sensitivity is unrelated to how large a
/// probe STEP we chose. The effective L2 slope threshold is
/// `max(probe_ppm/2, L2_SLOPE_FLOOR_PPM)`: keep the historical probe-relative
/// term (so a large probe still needs proportionally strong evidence) but never
/// let it drop below this floor, so a small probe cannot make demotion
/// hair-trigger and a residual < probe_ppm/2 wrong-way drift under a railed
/// command still eventually demotes. At the default probe_ppm=300 the floor
/// (100) is below probe_ppm/2 (150), so default behavior is unchanged; it only
/// bites for probe_ppm < 200.
pub const L2_SLOPE_FLOOR_PPM: f64 = 100.0;

/// Anti-windup threshold on the outer DLL, in FRAMES. When the total commanded
/// bias is railed at the ±[`MAX_BIAS_PPM`] clamp and the DLL's integrator is
/// demanding correction in the WRONG direction relative to the current fill
/// error (a post-transient windup: `z2 + z3` accumulated past the actuator's
/// authority and now points away from the target), reset the DLL and re-apply
/// the current error so the first bounded output points back toward target.
/// Mirrors `jasper_resampler::RateController::is_wound_against_error`
/// (threshold = half a period there); half of one 256-frame period ≈ 128
/// frames is the same "the error is genuinely non-trivial" gate.
pub const ANTI_WINDUP_THRESHOLD_FRAMES: f64 = 128.0;

// The outer DLL's loop timescale. `period / rate` is the DLL's per-update
// timescale in seconds; with a 1 s tick and this period/rate the effective
// bandwidth is `BW_MIN × (period/rate) / T_tick = 0.016 × 0.1 / 1 = 0.0016 Hz`.
const OUTER_DLL_PERIOD: f64 = 4800.0;
const OUTER_DLL_RATE: f64 = 48000.0;

/// Ladder state — the lock authority. `dll.locked` is diagnostic only (it is
/// expected false under the 256-frame ring quantization); THIS enum decides
/// whether the speaker trusts the host to follow the feedback.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ladder {
    /// Feature off, or no session yet. Pitch neutral, no DLL, no probe.
    Disabled,
    /// A session started; running the compliance probe (armed → baseline →
    /// step) before trusting the host.
    Probing,
    /// Probe passed; the DLL is actively steering the host, clamped.
    L0Locked,
    /// Locked but the raw demand is unusually high (sustained). Warn only.
    L1Warn,
    /// Probe failed, or mid-stream evidence the host stopped honoring the
    /// command. Pitch neutral until the next idle boundary re-probes.
    L2Fallback,
}

impl Ladder {
    /// The exact lowercase-snake token emitted in `state.json.host_clock.ladder`
    /// (contract §1). Pinned by a test.
    pub fn as_str(self) -> &'static str {
        match self {
            Ladder::Disabled => "disabled",
            Ladder::Probing => "probing",
            Ladder::L0Locked => "l0_locked",
            Ladder::L1Warn => "l1_warn",
            Ladder::L2Fallback => "l2_fallback",
        }
    }
}

/// Probe sub-phase while [`Ladder::Probing`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProbePhase {
    /// Just entered; waiting for the first tick to seat the baseline start.
    Armed,
    /// Commanding neutral, measuring the host's natural rate slope.
    Baseline,
    /// Commanding +probe_ppm, measuring the fill-slope response.
    Step,
}

/// Result of the most recent probe (contract §1 `probe.last_result`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeResult {
    None,
    Pass,
    Fail,
    Aborted,
}

impl ProbeResult {
    fn as_str(self) -> &'static str {
        match self {
            ProbeResult::None => "none",
            ProbeResult::Pass => "pass",
            ProbeResult::Fail => "fail",
            ProbeResult::Aborted => "aborted",
        }
    }
}

/// Validated host-clock configuration. Each consuming daemon parses its OWN
/// `JASPER_*` env keys and constructs this directly (usbsink's
/// `JASPER_USBSINK_HOST_CLOCK*`, fan-in's `JASPER_FANIN_HOST_CLOCK*`), so this
/// crate carries no env parsing — only the validated shape the ladder needs.
///
/// `log_prefix` is the ONE thing that differs between daemons: the `event=`
/// namespace prefix (`usbsink_audio` / `fanin`). Every structured log line the
/// ladder emits interpolates it, so the two daemons' journals stay
/// distinguishable while sharing byte-identical servo semantics.
#[derive(Debug, Clone, Copy)]
pub struct HostClockConfig {
    /// Whether the feature is armed. When `false` the ladder is inert (only the
    /// one-time startup neutralize runs) — every consuming daemon resolves this
    /// from its own literal-`enabled` gate.
    pub enabled: bool,
    /// Gadget fill setpoint in frames. usbsink default 384 (1.5 of the 3×256
    /// ring); fan-in derives it from its resampler's held target.
    pub target_fill_frames: f64,
    /// Probe step magnitude in ppm. Default 300 (inside ±1000 with margin).
    pub probe_ppm: f64,
    /// Probe step-phase duration in seconds. Default 6; a fixed 4 s neutral
    /// baseline phase runs first, so the whole probe is `4 + probe_step_secs`
    /// seconds — 10 s at the default, up to 14 s at the max (probe_step_secs 10).
    pub probe_step_secs: u64,
    /// The gadget ring period size in frames (for fill_frames scaling). Threaded
    /// from the daemon so this module stays self-contained.
    pub period_frames: u32,
    /// The `event=` namespace prefix for this daemon's ladder log lines
    /// (`usbsink_audio` / `fanin`). Static because it is a compile-time choice
    /// per daemon, not runtime config.
    pub log_prefix: &'static str,
}

impl HostClockConfig {
    /// A hard-disabled config with default tunables, for the given daemon
    /// `log_prefix`. In a mode where the audio loop that feeds the DLL never
    /// runs (usbsink standby; fan-in with direct off), there is no fill source,
    /// so the feature is forced off; the startup + exit pitch neutralize still
    /// run against this config (both are unconditional and never leave the host
    /// slaved), so a crashed predecessor is still healed. Never fails.
    pub fn disabled(period_frames: u32, log_prefix: &'static str) -> Self {
        Self {
            enabled: false,
            target_fill_frames: 384.0,
            probe_ppm: 300.0,
            probe_step_secs: 6,
            period_frames: period_frames.max(1),
            log_prefix,
        }
    }
}

/// The observation the ladder ticks on, sampled once per control tick from the
/// daemon's existing atomics. No clocks, no I/O — so the ladder is a pure,
/// fake-time-testable state machine. Each consuming daemon builds this from its
/// own shared state (usbsink's `SharedState`, fan-in's resampler/direct/trim
/// atomics); this crate defines only the shape.
#[derive(Debug, Clone, Copy)]
pub struct Obs {
    pub playing: bool,
    pub host_connected: bool,
    pub preempted: bool,
    /// Gadget PeriodRing fill in frames (ring_fill_periods × period_frames).
    pub fill_frames: f64,
    /// Cumulative captured frames (monotone).
    pub capture_frames: u64,
    /// Cumulative frames delivered to playback (monotone).
    pub playback_frames: u64,
}

/// An imperative action the ladder asks the caller (the owning thread) to
/// perform. The ladder is I/O-free; the owning thread holds the single ctl
/// handle and executes these. `WritePitch { reset: true }` bypasses the
/// epsilon/cadence suppression.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Action {
    /// Command this signed ppm bias. `reset` forces the write past the
    /// epsilon/cadence gate (for neutralize/idle/demote/probe-edge paths).
    WritePitch { ppm: f64, reset: bool },
}

// ---- Slope estimator -------------------------------------------------------

/// Exponentially-weighted estimator of the host-vs-DAC rate slope, in ppm, from
/// the monotone `(capture_frames − playback_frames)` divergence per tick.
///
/// The gadget fill is 256-frame quantized (whole periods), so differencing the
/// fill directly would be a coarse staircase. Instead we difference the
/// FRAME-granular cumulative counters: `d = Δ(capture − playback)` frames over
/// `Δt` ticks. The expected on-rate divergence is 0, so `d / expected_frames`
/// in ppm is the rate error. EW-smoothed so a single jittery tick does not move
/// the probe verdict.
#[derive(Debug, Clone)]
struct SlopeEstimator {
    alpha: f64,
    last_divergence: Option<i64>,
    slope_ppm: f64,
    have_slope: bool,
    // EW variance of the fill_frames signal (the limit-cycle falsifier).
    fill_mean: f64,
    fill_var: f64,
    have_fill: bool,
}

impl SlopeEstimator {
    fn new(alpha: f64) -> Self {
        Self {
            alpha,
            last_divergence: None,
            slope_ppm: 0.0,
            have_slope: false,
            fill_mean: 0.0,
            fill_var: 0.0,
            have_fill: false,
        }
    }

    /// Feed one tick. `divergence = capture − playback` frames (signed),
    /// `fill_frames` the gadget ring fill, `frames_per_tick` the expected
    /// on-rate frame count over one tick (rate × Δt). Returns the smoothed
    /// slope in ppm.
    fn update(&mut self, divergence: i64, fill_frames: f64, frames_per_tick: f64) -> f64 {
        if let Some(prev) = self.last_divergence {
            let delta = (divergence - prev) as f64;
            let inst_ppm = if frames_per_tick > 0.0 {
                delta / frames_per_tick * 1.0e6
            } else {
                0.0
            };
            if self.have_slope {
                self.slope_ppm += self.alpha * (inst_ppm - self.slope_ppm);
            } else {
                self.slope_ppm = inst_ppm;
                self.have_slope = true;
            }
        }
        self.last_divergence = Some(divergence);

        // EW mean/variance of the fill signal (West-style, non-negative).
        if self.have_fill {
            let d = fill_frames - self.fill_mean;
            self.fill_mean += self.alpha * d;
            self.fill_var = (1.0 - self.alpha) * (self.fill_var + self.alpha * d * d);
        } else {
            self.fill_mean = fill_frames;
            self.fill_var = 0.0;
            self.have_fill = true;
        }
        self.slope_ppm
    }

    fn slope_ppm(&self) -> f64 {
        if self.have_slope {
            self.slope_ppm
        } else {
            0.0
        }
    }

    fn fill_variance(&self) -> f64 {
        self.fill_var.max(0.0)
    }

    /// Re-arm at a session boundary: drop the divergence anchor and slope/fill
    /// history so a fresh session measures cleanly.
    fn rearm(&mut self) {
        self.last_divergence = None;
        self.slope_ppm = 0.0;
        self.have_slope = false;
        self.fill_mean = 0.0;
        self.fill_var = 0.0;
        self.have_fill = false;
    }
}

/// The full host-clock ladder + servo. Pure logic: `tick(obs, now_ms)` returns
/// the actions to perform. Owns the outer DLL, the slope estimator, the probe
/// state, the lifetime counters, and the last-written-command bookkeeping the
/// write-suppression needs.
pub struct HostClock {
    cfg: HostClockConfig,

    // Loop / servo.
    dll: Dll,
    slope: SlopeEstimator,
    /// The feed-forward bias seeded on L0 entry from the measured baseline
    /// slope; the DLL trims the residual around it.
    feed_forward_ppm: f64,
    /// Last commanded (clamped) bias — what telemetry reports and what the
    /// suppression epsilon compares against for the NEXT command.
    commanded_ppm: f64,
    /// Last value actually WRITTEN to the ctl (for the epsilon/cadence gate).
    last_written_ppm: f64,
    last_write_ms: Option<u64>,
    /// The most recent RAW (unclamped) demand — drives L1 and L2 evidence.
    raw_demand_ppm: f64,
    saturated: bool,

    // Ladder.
    ladder: Ladder,
    probe_phase: ProbePhase,
    probe_started_ms: u64,
    probe_baseline_slope_ppm: f64,
    probe_step_slope_ppm: f64,
    probe_result: ProbeResult,
    response_ratio: Option<f64>,
    l1_high_ticks: u32,
    l2_evidence_ticks: u32,

    // Session edge detection.
    session_active: bool,
    last_tick_ms: Option<u64>,

    // Lifetime counters + last transition token.
    demotions: u64,
    transitions: u64,
    /// Lifetime count of DLL anti-windup resets (diagnostic; not in the wire
    /// contract — surfaced only via the accessor for tests / future telemetry).
    anti_windup_events: u64,
    last_transition_reason: &'static str,

    // Whether the one-time startup neutralize has been emitted.
    startup_neutralized: bool,
}

/// Fixed 4 s neutral baseline before the step (contract §2). Not tunable.
const PROBE_BASELINE_SECS: u64 = 4;

impl HostClock {
    /// Build the ladder from validated config. The DLL is created with adaptive
    /// retune DISABLED and the resync/slew clamps OFF so its bandwidth — and
    /// hence the cascade separation — is a fixed, testable number (module docs).
    pub fn new(cfg: HostClockConfig) -> Self {
        let dll = Dll::new(DllConfig {
            period: OUTER_DLL_PERIOD,
            rate: OUTER_DLL_RATE,
            initial_bw: BW_MIN,
            bw_retune_period: 0, // fixed bandwidth ⇒ deterministic 0.0016 Hz
            max_error: 0.0,      // no slew clamp: the SERVO clamp (±1000 ppm) bounds output
            max_resync: 0.0,     // no hard-jump: fill excursions are the whole signal
        });
        // The slope EW alpha: track over ~a handful of ticks so the probe
        // measures within its phase windows but a single jittery tick can't
        // flip a verdict. 0.3 ≈ 3-tick memory.
        let slope = SlopeEstimator::new(0.3);
        Self {
            cfg,
            dll,
            slope,
            feed_forward_ppm: 0.0,
            commanded_ppm: 0.0,
            last_written_ppm: 0.0,
            last_write_ms: None,
            raw_demand_ppm: 0.0,
            saturated: false,
            ladder: Ladder::Disabled,
            probe_phase: ProbePhase::Armed,
            probe_started_ms: 0,
            probe_baseline_slope_ppm: 0.0,
            probe_step_slope_ppm: 0.0,
            probe_result: ProbeResult::None,
            response_ratio: None,
            l1_high_ticks: 0,
            l2_evidence_ticks: 0,
            session_active: false,
            last_tick_ms: None,
            demotions: 0,
            transitions: 0,
            anti_windup_events: 0,
            last_transition_reason: "startup",
            startup_neutralized: false,
        }
    }

    // ---- Accessors for telemetry --------------------------------------------

    pub fn enabled(&self) -> bool {
        self.cfg.enabled
    }
    pub fn ladder(&self) -> Ladder {
        self.ladder
    }
    pub fn commanded_ppm(&self) -> f64 {
        self.commanded_ppm
    }
    pub fn fill_slope_ppm(&self) -> f64 {
        self.slope.slope_ppm()
    }
    pub fn fill_variance(&self) -> f64 {
        self.slope.fill_variance()
    }
    pub fn dll_err_frames(&self) -> f64 {
        self.dll.error_mean()
    }
    pub fn dll_locked(&self) -> bool {
        self.dll.is_locked()
    }
    pub fn probe_result(&self) -> ProbeResult {
        self.probe_result
    }
    pub fn response_ratio(&self) -> Option<f64> {
        self.response_ratio
    }
    pub fn demotions(&self) -> u64 {
        self.demotions
    }
    pub fn transitions(&self) -> u64 {
        self.transitions
    }
    /// Lifetime count of outer-DLL anti-windup resets (diagnostic).
    pub fn anti_windup_events(&self) -> u64 {
        self.anti_windup_events
    }
    pub fn last_transition_reason(&self) -> &'static str {
        self.last_transition_reason
    }

    /// The one-time startup neutralize action. Emitted ONCE, unconditionally
    /// (even when the feature is disabled) so a crashed predecessor that left a
    /// stale pitch is healed. The owning thread calls this once right after
    /// opening the ctl. Returns `None` after the first call.
    pub fn startup_neutralize(&mut self) -> Option<Action> {
        if self.startup_neutralized {
            return None;
        }
        self.startup_neutralized = true;
        self.commanded_ppm = 0.0;
        Some(Action::WritePitch {
            ppm: 0.0,
            reset: true,
        })
    }

    /// The exit / disable neutralize. Forces a neutral write and drops the
    /// ladder to Disabled. Idempotent-safe: always emits the reset write so the
    /// invariant holds even if the last command was already neutral.
    pub fn neutralize_for_exit(&mut self, reason: &'static str) -> Action {
        self.transition_to(Ladder::Disabled, reason);
        self.commanded_ppm = 0.0;
        self.feed_forward_ppm = 0.0;
        Action::WritePitch {
            ppm: 0.0,
            reset: true,
        }
    }

    /// Advance one control tick. Returns the actions to perform (at most one
    /// pitch write per tick). `now_ms` is a monotonic millisecond clock (fake in
    /// tests). When the feature is disabled the ladder stays `Disabled` and this
    /// returns no actions after the startup neutralize — the loop is inert.
    pub fn tick(&mut self, obs: Obs, now_ms: u64) -> Vec<Action> {
        // Δt for slope scaling: the actual elapsed ticks (fake time in tests may
        // not be exactly 1 s). frames_per_tick = rate × Δt seconds.
        let dt_ms = match self.last_tick_ms {
            Some(prev) => now_ms.saturating_sub(prev).max(1),
            None => TICK_INTERVAL_MS,
        };
        self.last_tick_ms = Some(now_ms);
        let frames_per_tick = OUTER_DLL_RATE * (dt_ms as f64) / 1000.0;

        if !self.cfg.enabled {
            // Inert. The startup neutralize already ran; nothing to command.
            return Vec::new();
        }

        // Update the slope + fill variance every enabled tick regardless of
        // ladder state, so telemetry (and the falsifier) is always live.
        let divergence = (obs.capture_frames as i64) - (obs.playback_frames as i64);
        self.slope
            .update(divergence, obs.fill_frames, frames_per_tick);

        let session = obs.host_connected && obs.playing && !obs.preempted;
        let mut actions = Vec::new();

        // ---- Session-edge transitions (highest priority) --------------------
        if session && !self.session_active {
            // (host_connected && playing && !preempted) rising edge → re-probe.
            self.session_active = true;
            self.begin_probe(now_ms, &mut actions);
        } else if !session && self.session_active {
            // Session ended (stop / disconnect / preempt): pitch → neutral,
            // back to armed; L2 → PROBING only happens at THIS idle boundary.
            self.session_active = false;
            let reason = if obs.preempted {
                "preempted"
            } else if !obs.host_connected {
                "host_disconnected"
            } else {
                "stream_stop"
            };
            self.end_session(reason, &mut actions);
            return actions;
        }

        if !self.session_active {
            // Idle between sessions: hold neutral, do nothing.
            return actions;
        }

        // ---- Active-session ladder step -------------------------------------
        match self.ladder {
            Ladder::Probing => self.tick_probe(now_ms, &mut actions),
            Ladder::L0Locked | Ladder::L1Warn => self.tick_locked(obs, &mut actions),
            // Disabled/L2 while session_active only occurs transiently; L2 holds
            // neutral until the idle boundary re-probes (handled above).
            Ladder::Disabled | Ladder::L2Fallback => {}
        }
        actions
    }

    // ---- Probe -------------------------------------------------------------

    fn begin_probe(&mut self, now_ms: u64, actions: &mut Vec<Action>) {
        self.transition_to(Ladder::Probing, "session_start");
        self.probe_phase = ProbePhase::Armed;
        self.probe_started_ms = now_ms;
        self.probe_baseline_slope_ppm = 0.0;
        self.probe_step_slope_ppm = 0.0;
        self.slope.rearm();
        self.dll.reset();
        self.feed_forward_ppm = 0.0;
        // Command neutral for the baseline measurement (forced write).
        self.command(0.0, true, actions);
        log::info!(
            "event={}.host_clock_probe_start ppm={:.0} baseline_s={} step_s={}",
            self.cfg.log_prefix,
            self.cfg.probe_ppm,
            PROBE_BASELINE_SECS,
            self.cfg.probe_step_secs,
        );
    }

    fn tick_probe(&mut self, now_ms: u64, actions: &mut Vec<Action>) {
        let elapsed_ms = now_ms.saturating_sub(self.probe_started_ms);
        let baseline_ms = PROBE_BASELINE_SECS * 1000;
        let step_ms = baseline_ms + self.cfg.probe_step_secs * 1000;

        match self.probe_phase {
            ProbePhase::Armed => {
                // First tick after arming: enter baseline (still commanding
                // neutral). The slope estimator anchors this tick.
                self.probe_phase = ProbePhase::Baseline;
            }
            ProbePhase::Baseline => {
                if elapsed_ms >= baseline_ms {
                    // Baseline done: record the natural slope, command the step.
                    self.probe_baseline_slope_ppm = self.slope.slope_ppm();
                    self.probe_phase = ProbePhase::Step;
                    self.command(self.cfg.probe_ppm, true, actions);
                }
            }
            ProbePhase::Step => {
                if elapsed_ms >= step_ms {
                    self.probe_step_slope_ppm = self.slope.slope_ppm();
                    self.finish_probe(actions);
                }
            }
        }
    }

    fn finish_probe(&mut self, actions: &mut Vec<Action>) {
        // response_ratio = (step_slope − baseline_slope) / probe_ppm.
        // A compliant host, commanded +probe_ppm, shifts its delivery rate so
        // the fill slope moves by ~probe_ppm ⇒ ratio ≈ 1. A host that ignores
        // the command shows ~no slope change ⇒ ratio ≈ 0.
        let ratio =
            (self.probe_step_slope_ppm - self.probe_baseline_slope_ppm) / self.cfg.probe_ppm;
        self.response_ratio = Some(ratio);
        if ratio >= 0.5 {
            self.probe_result = ProbeResult::Pass;
            // Feed-forward: seed the commanded bias to cancel the measured
            // baseline rate offset so coarse correction is immediate; the slow
            // DLL only trims the residual. Sign: a host delivering FAST
            // (positive baseline slope, fill climbing) must be commanded slower
            // ⇒ negative bias.
            self.feed_forward_ppm = clamp_bias(-self.probe_baseline_slope_ppm);
            self.dll.reset();
            self.transition_to(Ladder::L0Locked, "probe_pass");
            self.command(self.feed_forward_ppm, true, actions);
            log::info!(
                "event={}.host_clock_probe_result result=pass response_ratio={:.3} baseline_slope_ppm={:.1} step_slope_ppm={:.1}",
                self.cfg.log_prefix,
                ratio,
                self.probe_baseline_slope_ppm,
                self.probe_step_slope_ppm,
            );
        } else {
            self.probe_result = ProbeResult::Fail;
            self.demotions += 1;
            self.transition_to(Ladder::L2Fallback, "probe_fail");
            self.command(0.0, true, actions); // pitch → neutral
            log::info!(
                "event={}.host_clock_probe_result result=fail response_ratio={:.3} baseline_slope_ppm={:.1} step_slope_ppm={:.1}",
                self.cfg.log_prefix,
                ratio,
                self.probe_baseline_slope_ppm,
                self.probe_step_slope_ppm,
            );
        }
    }

    // ---- Locked (L0/L1) -----------------------------------------------------

    fn tick_locked(&mut self, obs: Obs, actions: &mut Vec<Action>) {
        // Error = fill − target. Feeding this straight into the DLL gives the
        // right sign for a PRODUCER-side actuator: positive error (ring too
        // full) ⇒ Dll ratio < 1 ⇒ ratio_ppm < 0 ⇒ command the host SLOWER ⇒
        // fill falls. Closed negative feedback. (RateController is NOT reused —
        // its consumer-drain sign is inverted for this producer-side use, and
        // it hides the bandwidth knobs this cascade must pin. See module docs.)
        let err = obs.fill_frames - self.cfg.target_fill_frames;
        self.dll.update(err);
        let mut dll_trim_ppm = self.dll.ratio_ppm();

        // ---- Anti-windup ----------------------------------------------------
        // The ±MAX_BIAS_PPM clamp is a SAFETY bound on the actuator, not a bound
        // on the DLL's integrators (`z2 + z3` accumulate without limit — the
        // jasper-clock docs call out the clamped-actuator windup regime). A long
        // railed excursion can leave the DLL demanding correction in the WRONG
        // direction after the fill has crossed back past target, so the command
        // stays railed the wrong way and drains the fan-in cushion (the inner
        // lane resampler's authority is only ±500 ppm — see
        // rust/jasper-fanin/src/config.rs). When the total demand is railed AND
        // the DLL is wound against the current error, reset the loop and
        // re-apply the error so the first bounded output points back toward the
        // target. Mirrors jasper_resampler::RateController::is_wound_against_error
        // (reset-and-reapply idiom); the SIGN test differs by construction:
        // there the DLL is fed −error so a wound loop has raw_ppm.sign ==
        // error.sign; here the DLL is fed +error (producer sign), so normal
        // operation has trim.sign == −err.sign and a WOUND loop is trim.sign ==
        // err.sign.
        let total_raw = self.feed_forward_ppm + dll_trim_ppm;
        if total_raw.is_finite()
            && total_raw.abs() > MAX_BIAS_PPM
            && err.abs() >= ANTI_WINDUP_THRESHOLD_FRAMES
            && dll_trim_ppm.signum() != 0.0
            && err.signum() != 0.0
            && dll_trim_ppm.signum() == err.signum()
        {
            self.dll.reset();
            self.anti_windup_events = self.anti_windup_events.saturating_add(1);
            self.dll.update(err);
            dll_trim_ppm = self.dll.ratio_ppm();
        }

        // Total raw demand = feed-forward seed + DLL trim. The clamp bounds the
        // COMMAND; the raw demand still drives L1/L2 evidence so a railed host
        // is visible.
        let raw = self.feed_forward_ppm + dll_trim_ppm;
        self.raw_demand_ppm = raw;

        // ---- L2 mid-stream demotion evidence --------------------------------
        // Saturated command AND the fill still slopes the WRONG way (the host is
        // not following) for L2_SUSTAIN_TICKS ⇒ demote. The slope threshold is
        // max(probe_ppm/2, L2_SLOPE_FLOOR_PPM) — demotion sensitivity is a
        // physical question decoupled from the probe STEP magnitude, so a small
        // probe cannot make demotion hair-trigger nor let a residual wrong-way
        // drift under a railed command escape it forever (review S3).
        let saturated = raw.abs() >= MAX_BIAS_PPM;
        let slope = self.slope.slope_ppm();
        let l2_slope_threshold = (self.cfg.probe_ppm / 2.0).max(L2_SLOPE_FLOOR_PPM);
        // "Uncorrected direction": we are commanding to reduce |fill|, but the
        // slope magnitude is still worse than the L2 threshold pushing fill
        // further out. Sign check: if commanding negative (slow host) yet slope
        // is still strongly positive (fill climbing), the host ignores us — and
        // mutatis mutandis for the other sign.
        let uncorrected =
            (raw < 0.0 && slope > l2_slope_threshold) || (raw > 0.0 && slope < -l2_slope_threshold);
        if saturated && uncorrected {
            self.l2_evidence_ticks += 1;
        } else {
            self.l2_evidence_ticks = 0;
        }
        if self.l2_evidence_ticks >= L2_SUSTAIN_TICKS {
            self.l2_evidence_ticks = 0;
            self.demotions += 1;
            self.transition_to(Ladder::L2Fallback, "saturated_slope");
            self.feed_forward_ppm = 0.0;
            self.command(0.0, true, actions); // pitch → neutral (forced)
            return;
        }

        // ---- L0 ↔ L1 warn hysteresis (warn surface only) --------------------
        if raw.abs() > L1_WARN_PPM {
            self.l1_high_ticks = self.l1_high_ticks.saturating_add(1);
        } else if raw.abs() < L1_RELEASE_PPM {
            self.l1_high_ticks = 0;
        }
        if self.ladder == Ladder::L0Locked && self.l1_high_ticks >= L1_SUSTAIN_TICKS {
            self.transition_to(Ladder::L1Warn, "raw_demand_high");
        } else if self.ladder == Ladder::L1Warn && self.l1_high_ticks == 0 {
            self.transition_to(Ladder::L0Locked, "raw_demand_normal");
        }

        // Command the clamped bias (epsilon/cadence-suppressed).
        self.command(raw, false, actions);
    }

    // ---- Session boundary ---------------------------------------------------

    fn end_session(&mut self, reason: &'static str, actions: &mut Vec<Action>) {
        // If a probe was in flight, it is aborted (last_result="aborted").
        if self.ladder == Ladder::Probing {
            self.probe_result = ProbeResult::Aborted;
            log::info!(
                "event={}.host_clock_probe_result result=aborted response_ratio=null baseline_slope_ppm=null step_slope_ppm=null",
                self.cfg.log_prefix
            );
        }
        self.feed_forward_ppm = 0.0;
        self.l1_high_ticks = 0;
        self.l2_evidence_ticks = 0;
        // ANY → PROBING(armed) at the idle boundary; pitch → neutral. This is
        // the ONLY place L2 re-promotes toward PROBING.
        self.transition_to(Ladder::Probing, reason);
        self.probe_phase = ProbePhase::Armed;
        self.dll.reset();
        self.slope.rearm();
        self.command(0.0, true, actions);
        // The rising edge on the next (session) tick will begin_probe again.
        // Until then we sit Probing/Armed with neutral pitch; session_active is
        // false so tick() short-circuits to idle.
        log::info!(
            "event={}.host_clock_pitch_reset reason=idle",
            self.cfg.log_prefix
        );
    }

    // ---- Command + write-suppression ---------------------------------------

    /// Clamp the demand, update the commanded telemetry, and (unless suppressed)
    /// emit a `WritePitch` action. `reset=true` bypasses the epsilon + cadence
    /// gate (the neutrality-invariant paths).
    fn command(&mut self, demand_ppm: f64, reset: bool, actions: &mut Vec<Action>) {
        let clamped = clamp_bias(demand_ppm);
        let was_saturated = self.saturated;
        self.saturated = clamped.abs() >= MAX_BIAS_PPM;
        // Edge-triggered saturation log (once per episode).
        if self.saturated && !was_saturated {
            log::info!(
                "event={}.host_clock_saturated ppm={:.0}",
                self.cfg.log_prefix,
                clamped
            );
        }
        self.commanded_ppm = clamped;

        let now_ms = self.last_tick_ms.unwrap_or(0);
        if !reset {
            // Epsilon: skip if within WRITE_EPSILON_PPM of the last written.
            if (clamped - self.last_written_ppm).abs() < WRITE_EPSILON_PPM {
                return;
            }
            // Cadence: skip if the last write was < WRITE_MIN_INTERVAL_MS ago.
            if let Some(last) = self.last_write_ms {
                if now_ms.saturating_sub(last) < WRITE_MIN_INTERVAL_MS {
                    return;
                }
            }
        }
        self.last_written_ppm = clamped;
        self.last_write_ms = Some(now_ms);
        actions.push(Action::WritePitch {
            ppm: clamped,
            reset,
        });
    }

    fn transition_to(&mut self, to: Ladder, reason: &'static str) {
        if self.ladder == to {
            // Not a transition (e.g. probe→probe on re-arm); still record the
            // reason token so telemetry reflects the latest cause.
            self.last_transition_reason = reason;
            return;
        }
        log::info!(
            "event={}.host_clock_transition from={} to={} reason={}",
            self.cfg.log_prefix,
            self.ladder.as_str(),
            to.as_str(),
            reason,
        );
        self.ladder = to;
        self.last_transition_reason = reason;
        self.transitions += 1;
    }

    /// Render the `host_clock` block for `state.json` (contract §1). Byte-exact
    /// shape pinned by [`tests::host_clock_fragment_shape_is_stable`] and its
    /// Python twins (`tests/test_usbsink_host_clock_contract.py`,
    /// `tests/test_fanin_host_clock_contract.py`).
    pub fn status_fragment(&self) -> String {
        let ratio = match self.response_ratio {
            Some(r) => format!("{r:.4}"),
            None => "null".to_string(),
        };
        format!(
            concat!(
                "{{",
                "\"enabled\":{},",
                "\"ladder\":\"{}\",",
                "\"pitch_ppm_commanded\":{:.1},",
                "\"fill_frames\":{:.0},",
                "\"fill_slope_ppm\":{:.2},",
                "\"fill_variance\":{:.2},",
                "\"dll\":{{\"err_frames\":{:.2},\"locked\":{}}},",
                "\"probe\":{{\"last_result\":\"{}\",\"response_ratio\":{}}},",
                "\"demotions\":{},",
                "\"transitions\":{},",
                "\"last_transition_reason\":\"{}\"",
                "}}"
            ),
            json_bool(self.cfg.enabled),
            self.ladder.as_str(),
            self.commanded_ppm,
            self.published_fill_frames(),
            self.published_slope_ppm(),
            self.fill_variance(),
            self.dll_err_frames(),
            json_bool(self.dll_locked()),
            self.probe_result.as_str(),
            ratio,
            self.demotions,
            self.transitions,
            self.last_transition_reason,
        )
    }

    /// The published fill: the last observed gadget fill while a session is
    /// active, else 0 (contract: `fill_frames` reused atomics). Held on the
    /// estimator's mean so the block is coherent even between ticks.
    fn published_fill_frames(&self) -> f64 {
        if self.session_active {
            self.slope.fill_mean
        } else {
            0.0
        }
    }

    /// Slope is published only while playing && !preempted, else 0.0 (contract).
    fn published_slope_ppm(&self) -> f64 {
        if self.session_active {
            self.slope.slope_ppm()
        } else {
            0.0
        }
    }
}

/// Clamp a demand to the ±[`MAX_BIAS_PPM`] servo window (the Windows validity
/// window), independent of the wider hardware ctl range. NaN clamps to 0.
fn clamp_bias(ppm: f64) -> f64 {
    if !ppm.is_finite() {
        return 0.0;
    }
    ppm.clamp(-MAX_BIAS_PPM, MAX_BIAS_PPM)
}

fn json_bool(v: bool) -> &'static str {
    if v {
        "true"
    } else {
        "false"
    }
}

/// The actuator side: turn a commanded ppm bias into the pitch ctl integer and
/// write it. `AlsaPitchCtl` is the real ALSA implementation (feature-gated);
/// a mock (tests) records every write. The trait keeps the ladder/servo logic
/// fully testable on a host that cannot link ALSA.
pub trait PitchCtl {
    /// Write the raw ctl value (1_000_000 + round(ppm)). Errors are surfaced so
    /// the owning thread can rate-limit-log them; a failure must NOT crash.
    fn write(&mut self, value: i64) -> Result<(), String>;
}

/// Convert a signed ppm bias to the ctl integer value, clamped to the hardware
/// range as a final defense (the servo clamp already bounds to ±1000 ppm, well
/// inside 750000..1005000).
pub fn ppm_to_ctl_value(ppm: f64) -> i64 {
    let v = PITCH_NEUTRAL + ppm.round() as i64;
    v.clamp(750_000, 1_005_000)
}

/// Derive the ctl card spec (e.g. `hw:UAC2Gadget`) from the capture device.
/// The capture device is already `hw:UAC2Gadget` by default; if an operator
/// overrode it to a plug/dsnoop form we take the `hw:<card>` prefix, and if we
/// can't parse one we fall back to the capture string verbatim (ALSA will
/// reject a bad name at open, surfaced as a ctl_error).
pub fn ctl_card_from_capture(capture_device: &str) -> String {
    let trimmed = capture_device.trim();
    // Common shapes: "hw:UAC2Gadget", "hw:CARD=UAC2Gadget,DEV=0", "plughw:UAC2Gadget".
    if let Some(rest) = trimmed.strip_prefix("plughw:") {
        return format!("hw:{}", card_token(rest));
    }
    if let Some(rest) = trimmed.strip_prefix("hw:") {
        return format!("hw:{}", card_token(rest));
    }
    trimmed.to_string()
}

/// Extract the card identifier from an ALSA device tail, handling both
/// `UAC2Gadget` and `CARD=UAC2Gadget,DEV=0` forms.
fn card_token(rest: &str) -> String {
    let rest = rest.trim();
    if let Some(after) = rest.strip_prefix("CARD=") {
        after.split(',').next().unwrap_or(after).trim().to_string()
    } else {
        rest.split(',').next().unwrap_or(rest).trim().to_string()
    }
}

#[cfg(feature = "alsa")]
mod alsa_ctl {
    use super::PitchCtl;
    use alsa::ctl::{Ctl, ElemId, ElemIface, ElemType, ElemValue};
    use std::ffi::CString;

    /// Real ALSA pitch actuator. Opens the card control device once and reuses
    /// the `ElemValue` for every write. Held ONLY by the owning thread.
    pub struct AlsaPitchCtl {
        ctl: Ctl,
        value: ElemValue,
    }

    impl AlsaPitchCtl {
        /// Open `card` (e.g. `hw:UAC2Gadget`) and prepare the
        /// iface=PCM, name="Capture Pitch 1000000" element value.
        ///
        /// Resolution is by the (iface, name) tuple ONLY — deliberately NOT
        /// by numid. `snd_ctl_elem_id_set_numid(id, N)` with a nonzero N makes
        /// the kernel's `snd_ctl_find_id` match purely on numid and IGNORE the
        /// name; numid 1 happening to be the pitch ctl today is a
        /// registration-order artifact of `u_audio.c`, not ABI. Were a future
        /// kernel to register another writable-integer control first (the most
        /// plausible neighbor on this card is `PCM Capture Volume`, the one-way
        /// host-slider input that drives `listening_level`), a numid-pinned
        /// write would silently retarget it — and the unconditional startup
        /// neutralize would do so even with the feature OFF. The name is stable
        /// ABI; matching on it keeps this path aligned with the unit's
        /// name-based `ExecStopPost` belt-and-braces, so both writers target
        /// the same element the same way. (alsa-0.11.0 exposes no public
        /// `Ctl`→`ElemInfo` fetch, so an at-open type/count assertion is not
        /// available at this crate version; the name match plus the hardware
        /// ctl-range clamp in `ppm_to_ctl_value` are the layered defenses, and
        /// a bad name simply surfaces as a fail-soft `elem_write` error.)
        pub fn open(card: &str) -> Result<Self, String> {
            let ctl = Ctl::new(card, false).map_err(|e| format!("open ctl {card}: {e}"))?;
            let mut id = ElemId::new(ElemIface::PCM);
            let name = CString::new("Capture Pitch 1000000")
                .map_err(|e| format!("ctl name cstring: {e}"))?;
            id.set_name(&name);
            let mut value =
                ElemValue::new(ElemType::Integer).map_err(|e| format!("elem value: {e}"))?;
            value.set_id(&id);
            Ok(Self { ctl, value })
        }
    }

    impl PitchCtl for AlsaPitchCtl {
        fn write(&mut self, value: i64) -> Result<(), String> {
            self.value.set_integer(0, value as i32);
            self.ctl
                .elem_write(&self.value)
                .map(|_| ())
                .map_err(|e| format!("elem_write({value}): {e}"))
        }
    }
}

#[cfg(feature = "alsa")]
pub use alsa_ctl::AlsaPitchCtl;

#[cfg(test)]
mod tests {
    use super::*;

    fn enabled_cfg() -> HostClockConfig {
        HostClockConfig {
            enabled: true,
            target_fill_frames: 384.0,
            probe_ppm: 300.0,
            probe_step_secs: 6,
            period_frames: 256,
            log_prefix: "usbsink_audio",
        }
    }

    fn obs(playing: bool, host: bool, fill: f64, cap: u64, play: u64) -> Obs {
        Obs {
            playing,
            host_connected: host,
            preempted: false,
            fill_frames: fill,
            capture_frames: cap,
            playback_frames: play,
        }
    }

    // ---- Pinned constants --------------------------------------------------

    #[test]
    fn pinned_constants_match_contract() {
        assert_eq!(PITCH_NEUTRAL, 1_000_000);
        assert_eq!(MAX_BIAS_PPM, 1000.0);
        assert_eq!(WRITE_EPSILON_PPM, 10.0);
        assert_eq!(WRITE_MIN_INTERVAL_MS, 1000);
        assert_eq!(TICK_INTERVAL_MS, 1000);
        assert_eq!(L1_WARN_PPM, 2500.0);
        assert_eq!(L1_RELEASE_PPM, 2000.0);
        assert_eq!(L1_SUSTAIN_TICKS, 30);
        assert_eq!(L2_SUSTAIN_TICKS, 10);
        assert_eq!(L2_SLOPE_FLOOR_PPM, 100.0);
        assert_eq!(ANTI_WINDUP_THRESHOLD_FRAMES, 128.0);
    }

    #[test]
    fn ctl_value_neutral_and_clamped() {
        assert_eq!(ppm_to_ctl_value(0.0), 1_000_000);
        assert_eq!(ppm_to_ctl_value(300.0), 1_000_300);
        assert_eq!(ppm_to_ctl_value(-300.0), 999_700);
        // Beyond hw range is clamped as a last defense.
        assert_eq!(ppm_to_ctl_value(1.0e9), 1_005_000);
        assert_eq!(ppm_to_ctl_value(-1.0e9), 750_000);
    }

    #[test]
    fn ctl_card_derivation_handles_common_forms() {
        assert_eq!(ctl_card_from_capture("hw:UAC2Gadget"), "hw:UAC2Gadget");
        assert_eq!(ctl_card_from_capture("plughw:UAC2Gadget"), "hw:UAC2Gadget");
        assert_eq!(
            ctl_card_from_capture("hw:CARD=UAC2Gadget,DEV=0"),
            "hw:UAC2Gadget"
        );
        assert_eq!(ctl_card_from_capture("hw:UAC2Gadget,0"), "hw:UAC2Gadget");
    }

    #[test]
    fn clamp_bias_bounds_and_handles_nonfinite() {
        assert_eq!(clamp_bias(1500.0), 1000.0);
        assert_eq!(clamp_bias(-1500.0), -1000.0);
        assert_eq!(clamp_bias(f64::NAN), 0.0);
        assert_eq!(clamp_bias(250.0), 250.0);
    }

    // ---- Disabled feature is inert ----------------------------------------

    #[test]
    fn disabled_feature_only_neutralizes_at_startup() {
        let mut cfg = enabled_cfg();
        cfg.enabled = false;
        let mut hc = HostClock::new(cfg);
        // Startup neutralize still runs (heals a crashed predecessor).
        assert_eq!(
            hc.startup_neutralize(),
            Some(Action::WritePitch {
                ppm: 0.0,
                reset: true
            })
        );
        assert_eq!(hc.startup_neutralize(), None, "startup neutralize is once");
        // Ticks are inert regardless of session state.
        for t in 0..50 {
            let a = hc.tick(obs(true, true, 400.0, t * 48000, t * 48000), t * 1000);
            assert!(a.is_empty(), "disabled feature must not command");
        }
        assert_eq!(hc.ladder(), Ladder::Disabled);
    }

    // ---- Probe pass path ---------------------------------------------------

    /// Drive a COMPLIANT synthetic host through a full session probe → L0.
    /// The compliant host shifts its delivery rate to follow the commanded
    /// pitch, so the fill-slope moves ~probe_ppm during the step ⇒ pass.
    #[test]
    fn compliant_host_probes_pass_and_locks_l0() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 0;
        let mut play: u64 = 0;
        // Host runs +200 ppm fast at baseline; during the step it FOLLOWS the
        // +300 command (delivers 300 ppm faster) — a compliant response.
        let mut t = 1u64;
        // Rising edge → begin probe.
        let mut ladder_seen = Vec::new();
        for _ in 0..14 {
            // capture advances a bit faster than playback => positive divergence
            // slope. Baseline: +200 ppm. Step (after t>=5s): +200+300.
            let elapsed_s = t.saturating_sub(1);
            let host_ppm = if elapsed_s >= 4 { 200.0 + 300.0 } else { 200.0 };
            cap += (48000.0 * (1.0 + host_ppm / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
            ladder_seen.push(hc.ladder());
            t += 1;
        }
        assert_eq!(hc.probe_result(), ProbeResult::Pass);
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        let ratio = hc.response_ratio().unwrap();
        assert!(ratio >= 0.5, "response_ratio should pass: {ratio}");
    }

    /// A NON-compliant host (ignores the pitch command) fails the probe → L2,
    /// pitch neutral, demotion counted.
    #[test]
    fn noncompliant_host_probes_fail_and_falls_to_l2() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 0;
        let mut play: u64 = 0;
        // Host runs +200 ppm the WHOLE time — no response to the step command.
        let mut last_action_neutral = false;
        for t in 1u64..14 {
            cap += (48000.0 * (1.0 + 200.0 / 1.0e6)) as u64;
            play += 48000;
            let actions = hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
            if let Some(Action::WritePitch { ppm, .. }) = actions.last() {
                last_action_neutral = *ppm == 0.0;
            }
        }
        assert_eq!(hc.probe_result(), ProbeResult::Fail);
        assert_eq!(hc.ladder(), Ladder::L2Fallback);
        assert_eq!(hc.demotions(), 1);
        assert!(last_action_neutral, "L2 must command neutral pitch");
    }

    /// L2 does NOT re-probe mid-stream; it re-probes only at the idle boundary
    /// (stream stop), then a fresh session rising edge starts a new probe.
    #[test]
    fn l2_repromotes_only_at_idle_boundary() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 0;
        let mut play: u64 = 0;
        // Drive to L2 via a failing probe.
        for t in 1u64..14 {
            cap += (48000.0 * (1.0 + 200.0 / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
        }
        assert_eq!(hc.ladder(), Ladder::L2Fallback);
        // Keep the session playing: still L2, no re-probe.
        for t in 14u64..30 {
            cap += (48000.0 * (1.0 + 200.0 / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
            assert_eq!(hc.ladder(), Ladder::L2Fallback, "no mid-stream re-probe");
        }
        // Stream stops (playing=false): idle boundary → Probing (armed).
        let stop = hc.tick(obs(false, true, 400.0, cap, play), 30_000);
        assert_eq!(hc.ladder(), Ladder::Probing);
        assert!(
            matches!(stop.last(), Some(Action::WritePitch { ppm, reset: true }) if *ppm == 0.0),
            "idle boundary forces neutral pitch"
        );
    }

    /// Every new session re-probes (per-session compliance).
    #[test]
    fn every_session_reprobes() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        // Session 1: rising edge begins a probe.
        hc.tick(obs(true, true, 400.0, 48000, 48000), 1000);
        assert_eq!(hc.ladder(), Ladder::Probing);
        // Session stop.
        hc.tick(obs(false, true, 400.0, 96000, 96000), 2000);
        assert_eq!(hc.ladder(), Ladder::Probing);
        let t1 = hc.transitions();
        // Session 2 rising edge: a NEW probe begins (transition recorded).
        hc.tick(obs(true, true, 400.0, 144000, 144000), 3000);
        assert!(hc.transitions() >= t1, "a new session re-probes");
    }

    /// Preempt (or mid-probe stop) aborts the probe: last_result="aborted",
    /// pitch neutral, back to armed.
    #[test]
    fn preempt_mid_probe_aborts() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        hc.tick(obs(true, true, 400.0, 48000, 48000), 1000);
        assert_eq!(hc.ladder(), Ladder::Probing);
        // Preempt: session ends → probe aborted.
        let mut ob = obs(true, true, 400.0, 96000, 96000);
        ob.preempted = true;
        let actions = hc.tick(ob, 2000);
        assert_eq!(hc.probe_result(), ProbeResult::Aborted);
        assert!(
            matches!(actions.last(), Some(Action::WritePitch { ppm, reset: true }) if *ppm == 0.0),
            "abort forces neutral pitch"
        );
    }

    // ---- Closed-loop servo -------------------------------------------------

    /// The headline servo test: a host at a constant crystal offset, once
    /// L0-locked, is steered to hold the ring at target WITHOUT oscillation.
    /// We simulate a modeled host reaction lag (the host takes ~1-2 ticks to
    /// act on a new command) and quantized 256-frame fill.
    #[test]
    fn locked_servo_settles_without_oscillation() {
        for offset_ppm in [-200.0, 200.0] {
            let mut hc = HostClock::new(enabled_cfg());
            hc.startup_neutralize();
            // Force into L0 quickly with a clean compliant probe, then hand off
            // to the closed loop below.
            drive_to_l0(&mut hc, offset_ppm);
            assert_eq!(hc.ladder(), Ladder::L0Locked, "must lock before servo test");

            // Closed loop: the host's *effective* delivery rate = its crystal
            // offset PLUS the commanded bias it is honoring (with a 1-tick lag).
            // fill integrates (host_rate − dac_rate) frames each tick.
            let target = 384.0;
            let mut fill = target;
            let mut cap: u64 = hc_cap_start();
            let mut play: u64 = cap; // start aligned
            let mut commanded_history = vec![0.0f64];
            let mut fills = Vec::new();
            let mut t = 100u64;
            for _ in 0..400 {
                // Host honors the command from ~1 tick ago (reaction lag).
                let honored = *commanded_history
                    .get(commanded_history.len().saturating_sub(2))
                    .unwrap_or(&0.0);
                let host_ppm = offset_ppm + honored;
                let host_frames = 48000.0 * (1.0 + host_ppm / 1.0e6);
                cap += host_frames as u64;
                play += 48000;
                // Quantize fill to whole 256-frame periods (the gadget ring).
                fill += host_frames - 48000.0;
                let quantized = (fill / 256.0).round() * 256.0;
                let actions = hc.tick(obs(true, true, quantized, cap, play), t * 1000);
                let cmd = match actions.last() {
                    Some(Action::WritePitch { ppm, .. }) => *ppm,
                    None => *commanded_history.last().unwrap(),
                };
                commanded_history.push(cmd);
                fills.push(hc.commanded_ppm());
                t += 1;
            }
            // Settled tail: the commanded ppm should converge to ~ −offset (to
            // cancel the crystal offset) and NOT oscillate. Count sign flips of
            // the tail's derivative.
            let tail = &fills[300..];
            let mean: f64 = tail.iter().sum::<f64>() / tail.len() as f64;
            // Commanded bias ≈ −offset (feed-forward + trim cancel the offset).
            assert!(
                (mean + offset_ppm).abs() < 120.0,
                "settled command {mean} should ~cancel offset {offset_ppm}"
            );
            // Bounded: never leaves the clamp.
            assert!(tail.iter().all(|c| c.abs() <= MAX_BIAS_PPM + 1e-6));
            // No sustained oscillation: few sign flips around the mean.
            let mut flips = 0usize;
            let mut prev = 0i8;
            for c in tail {
                let s = (c - mean).signum() as i8;
                if s != 0 && prev != 0 && s != prev {
                    flips += 1;
                }
                if s != 0 {
                    prev = s;
                }
            }
            assert!(
                flips < 40,
                "settled command oscillates ({flips} sign flips) for offset {offset_ppm}"
            );
        }
    }

    /// The servo CLAMP: a huge persistent fill error drives the DLL demand well
    /// past ±MAX_BIAS_PPM, but the commanded bias never leaves the ±1000 ppm
    /// window (the Windows validity window). We assert on the FIRST ticks after
    /// L0 — before the 10-tick L2 mid-stream demotion window can neutralize —
    /// because a railed-and-unfollowed host is CORRECTLY demoted to L2 shortly
    /// after (that path is exercised by the mid-stream demotion test). The DLL
    /// trim rails within a tick or two on a large open-loop error, so this is
    /// deterministic.
    #[test]
    fn clamp_holds_under_large_offset() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 500.0);
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        // A huge positive fill error (ring far above target). Feed it for a few
        // ticks and assert the command rails at the clamp and a saturation is
        // observable — checked inside the pre-L2 window (< L2_SUSTAIN_TICKS).
        let mut cap = hc_cap_start();
        let mut play = cap;
        let mut railed = false;
        for i in 0..(L2_SUSTAIN_TICKS - 1) {
            cap += 48000;
            play += 48000;
            hc.tick(obs(true, true, 20000.0, cap, play), (100 + i as u64) * 1000);
            if hc.commanded_ppm().abs() >= MAX_BIAS_PPM - 1e-6 {
                railed = true;
            }
        }
        assert!(
            railed,
            "command must rail at ±{MAX_BIAS_PPM} ppm under a huge fill error, got {}",
            hc.commanded_ppm()
        );
        // And it never exceeded the clamp on any tick.
        assert!(hc.commanded_ppm().abs() <= MAX_BIAS_PPM + 1e-6);
    }

    /// Mid-stream demotion: a locked host that STOPS honoring the command (the
    /// ring keeps diverging in the uncorrected direction while the command is
    /// saturated) is demoted to L2 after L2_SUSTAIN_TICKS, pitch → neutral,
    /// demotion counted.
    #[test]
    fn saturated_unfollowed_host_demotes_to_l2_midstream() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 500.0);
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        let demotions_before = hc.demotions();
        // Host ignores the command: fill pinned huge AND the slope keeps pushing
        // fill up (capture persistently outruns playback) — the "uncorrected"
        // condition. Command saturates negative; slope stays strongly positive.
        let mut cap = hc_cap_start();
        let mut play = cap;
        let mut neutral_after_demote = false;
        for i in 0..(L2_SUSTAIN_TICKS + 5) {
            // +2000 ppm divergence >> probe_ppm/2, in the uncorrected direction.
            cap += (48000.0 * (1.0 + 2000.0 / 1.0e6)) as u64;
            play += 48000;
            let actions = hc.tick(obs(true, true, 20000.0, cap, play), (100 + i as u64) * 1000);
            if hc.ladder() == Ladder::L2Fallback && !neutral_after_demote {
                neutral_after_demote = matches!(
                    actions.last(),
                    Some(Action::WritePitch { ppm, reset: true }) if *ppm == 0.0
                );
            }
        }
        assert_eq!(hc.ladder(), Ladder::L2Fallback, "must demote mid-stream");
        assert_eq!(hc.demotions(), demotions_before + 1, "demotion counted");
        assert!(neutral_after_demote, "demotion forces neutral pitch");
    }

    // ---- Anti-windup (S3) --------------------------------------------------

    /// After a large transient rails the command, once the fill CROSSES BACK
    /// past the target the anti-windup reset keeps the DLL from holding the
    /// command railed the WRONG way. Without the guard, the integrator that
    /// wound up demanding "slow the host" (negative) while the ring was far
    /// above target would keep the command pinned negative after the ring
    /// dropped below target, draining the cushion. With it, the command
    /// promptly points back toward target (positive) and the counter ticks.
    #[test]
    fn anti_windup_reset_prevents_wrong_way_rail_after_transient() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 0.0);
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        let target = hc.cfg.target_fill_frames;

        let mut cap = hc_cap_start();
        let mut play = cap;
        let mut t = 200u64;
        // Phase 1: a big POSITIVE fill error (ring far above target). The DLL
        // integrator winds toward a strongly NEGATIVE command (slow the host).
        // Keep the divergence slope small so we exercise WINDUP, not the L2
        // demotion path (which needs a sustained wrong-way slope).
        for _ in 0..8 {
            cap += 48000;
            play += 48000;
            hc.tick(obs(true, true, target + 5000.0, cap, play), t * 1000);
            t += 1;
        }
        assert!(
            hc.commanded_ppm() < -MAX_BIAS_PPM + 1.0,
            "command should rail negative during the high-fill transient, got {}",
            hc.commanded_ppm()
        );
        let windup_before = hc.anti_windup_events();
        // Phase 2: the ring collapses to FAR BELOW target (error flips sign).
        // A wound DLL would keep commanding negative for many ticks; the guard
        // resets it so the command swings positive toward the new error.
        let mut recovered_positive = false;
        for _ in 0..8 {
            cap += 48000;
            play += 48000;
            hc.tick(
                obs(true, true, (target - 5000.0).max(0.0), cap, play),
                t * 1000,
            );
            if hc.commanded_ppm() > 0.0 {
                recovered_positive = true;
            }
            t += 1;
        }
        assert!(
            hc.anti_windup_events() > windup_before,
            "anti-windup should engage on the sign-flip transient"
        );
        assert!(
            recovered_positive,
            "after the fill crossed below target the command must point back toward target (positive), not stay railed negative — commanded={}",
            hc.commanded_ppm()
        );
    }

    /// The anti-windup guard must NOT fire in normal locked tracking (small
    /// on-target errors), or it would needlessly reset the loop every tick.
    #[test]
    fn anti_windup_does_not_fire_in_steady_tracking() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 100.0);
        let before = hc.anti_windup_events();
        let mut cap = hc_cap_start();
        let mut play = cap;
        for i in 0..60 {
            cap += 48000;
            play += 48000;
            // On-target ring, tiny error — the command is well inside the clamp.
            hc.tick(obs(true, true, 384.0, cap, play), (300 + i as u64) * 1000);
        }
        assert_eq!(
            hc.anti_windup_events(),
            before,
            "anti-windup must not engage while tracking on-target"
        );
    }

    /// The L2 slope threshold is DECOUPLED from probe_ppm via the absolute
    /// floor `max(probe_ppm/2, L2_SLOPE_FLOOR_PPM)`. With a SMALL probe
    /// (probe_ppm=100 ⇒ probe_ppm/2 = 50) a bare probe_ppm/2 threshold would
    /// make demotion HAIR-TRIGGER: a mere 50-ppm wrong-way drift would demote.
    /// The floor (100) keeps the operative threshold at 100 regardless of the
    /// tiny probe, so a 70-ppm drift — above probe_ppm/2 but below the floor —
    /// must NOT demote. This proves demotion sensitivity is set by the physical
    /// floor, not by the (unrelated) probe-step magnitude.
    #[test]
    fn l2_slope_floor_prevents_hair_trigger_demotion_with_small_probe() {
        let mut cfg = enabled_cfg();
        cfg.probe_ppm = 100.0; // probe_ppm/2 = 50, below the 100 ppm floor
        let mut hc = HostClock::new(cfg);
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 100.0); // compliant probe follows +100 step
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        // Ring pinned huge (command rails negative) AND a +70 ppm wrong-way
        // drift — ABOVE probe_ppm/2 (50) but BELOW the floor (100). A pure
        // probe_ppm/2 threshold would demote; the floor must prevent it.
        let mut cap = hc_cap_start();
        let mut play = cap;
        for i in 0..(L2_SUSTAIN_TICKS + 10) {
            cap += (48000.0 * (1.0 + 70.0 / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 20000.0, cap, play), (400 + i as u64) * 1000);
        }
        assert_ne!(
            hc.ladder(),
            Ladder::L2Fallback,
            "a 70 ppm drift (below the 100 ppm floor) must NOT demote even with \
             a tiny probe — the floor decouples demotion sensitivity from the \
             probe-step magnitude"
        );
    }

    /// The complement: with the same small probe, a drift ABOVE the floor
    /// (140 ppm > 100) under a railed command DOES demote — the floor is a
    /// real threshold, not an unconditional escape hatch.
    #[test]
    fn l2_slope_floor_still_demotes_above_floor_with_small_probe() {
        let mut cfg = enabled_cfg();
        cfg.probe_ppm = 100.0;
        let mut hc = HostClock::new(cfg);
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 100.0);
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        let demotions_before = hc.demotions();
        let mut cap = hc_cap_start();
        let mut play = cap;
        for i in 0..(L2_SUSTAIN_TICKS + 5) {
            cap += (48000.0 * (1.0 + 140.0 / 1.0e6)) as u64; // > 100 floor
            play += 48000;
            hc.tick(obs(true, true, 20000.0, cap, play), (400 + i as u64) * 1000);
        }
        assert_eq!(
            hc.ladder(),
            Ladder::L2Fallback,
            "drift above floor must demote"
        );
        assert_eq!(hc.demotions(), demotions_before + 1);
    }

    /// At the DEFAULT probe (probe_ppm=300 ⇒ probe_ppm/2 = 150) the floor (100)
    /// is BELOW probe_ppm/2, so the operative L2 threshold stays 150 — the
    /// floor only ever RAISES sensitivity for small probes, never lowers the
    /// default. A +120 ppm wrong-way drift is below 150, so a railed command
    /// facing it must NOT demote within the sustain window (it stays locked).
    /// This pins that the S3 floor change did not weaken default behavior.
    #[test]
    fn default_probe_demotion_threshold_unchanged_by_floor() {
        let mut hc = HostClock::new(enabled_cfg()); // probe_ppm = 300
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 500.0);
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        // A +120 ppm wrong-way drift: below probe_ppm/2 (150). Must NOT demote
        // within the L2 window — the default threshold is unchanged at 150.
        let mut cap = hc_cap_start();
        let mut play = cap;
        for i in 0..(L2_SUSTAIN_TICKS + 5) {
            cap += (48000.0 * (1.0 + 120.0 / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 20000.0, cap, play), (500 + i as u64) * 1000);
        }
        assert_ne!(
            hc.ladder(),
            Ladder::L2Fallback,
            "at default probe (threshold 150) a 120 ppm drift is sub-threshold; \
             the railed command must NOT demote — the floor must not lower the \
             default sensitivity"
        );
    }

    // ---- Write suppression -------------------------------------------------

    /// Steady state within the epsilon produces no repeated writes; and writes
    /// obey the ≤1 Hz cadence.
    #[test]
    fn steady_state_suppresses_writes() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 100.0);
        // Now feed a perfectly on-target ring so the command barely changes.
        let mut cap = hc_cap_start();
        let mut play = cap;
        let mut writes = 0usize;
        let mut t = 100u64;
        for _ in 0..60 {
            cap += 48000;
            play += 48000;
            let actions = hc.tick(obs(true, true, 384.0, cap, play), t * 1000);
            writes += actions
                .iter()
                .filter(|a| matches!(a, Action::WritePitch { reset: false, .. }))
                .count();
            t += 1;
        }
        // At most one non-reset write per second, and near-zero once settled.
        assert!(
            writes <= 5,
            "steady state should barely write, got {writes}"
        );
    }

    #[test]
    fn reset_writes_bypass_suppression() {
        let mut hc = HostClock::new(enabled_cfg());
        // Startup neutralize is a forced write even though last==0.
        let a = hc.startup_neutralize().unwrap();
        assert_eq!(
            a,
            Action::WritePitch {
                ppm: 0.0,
                reset: true
            }
        );
        // Exit neutralize also forces a write regardless of last command.
        let exit = hc.neutralize_for_exit("shutdown");
        assert_eq!(
            exit,
            Action::WritePitch {
                ppm: 0.0,
                reset: true
            }
        );
        assert_eq!(hc.ladder(), Ladder::Disabled);
    }

    // ---- Pitch reset on every exit path ------------------------------------

    #[test]
    fn every_exit_path_forces_neutral_write() {
        // (a) startup
        let mut hc = HostClock::new(enabled_cfg());
        assert!(matches!(
            hc.startup_neutralize(),
            Some(Action::WritePitch { ppm, reset: true }) if ppm == 0.0
        ));

        // (b) shutdown/disable exit
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        assert!(matches!(
            hc.neutralize_for_exit("shutdown"),
            Action::WritePitch { ppm, reset: true } if ppm == 0.0
        ));

        // (c) demotion (probe fail) forces neutral. Latch with `||=` so the
        // quiet L2 ticks that follow the demotion (which emit no action) can't
        // clobber the observation from the demotion tick itself.
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap = 0u64;
        let mut play = 0u64;
        let mut demote_neutral = false;
        for t in 1u64..14 {
            cap += (48000.0 * (1.0 + 200.0 / 1.0e6)) as u64;
            play += 48000;
            let actions = hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
            if hc.ladder() == Ladder::L2Fallback {
                demote_neutral |= matches!(actions.last(), Some(Action::WritePitch { ppm, reset: true }) if *ppm == 0.0);
            }
        }
        assert!(demote_neutral, "demotion forces neutral pitch");

        // (d) idle boundary forces neutral.
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        hc.tick(obs(true, true, 400.0, 48000, 48000), 1000);
        let idle = hc.tick(obs(false, true, 400.0, 96000, 96000), 2000);
        assert!(
            matches!(idle.last(), Some(Action::WritePitch { ppm, reset: true }) if *ppm == 0.0),
            "idle forces neutral pitch"
        );
    }

    // ---- L1 warn -----------------------------------------------------------

    /// L1_WARN (warn surface only): the raw demand is unusually high but the
    /// loop is still tracking (slope BELOW the L2 uncorrected threshold, so this
    /// is NOT a demotion). The physical case is a host with a large real crystal
    /// offset that we can only partially correct through the ±1000 ppm clamp:
    /// the residual fill error persists, the DLL demand integrates past
    /// L1_WARN_PPM, yet the fill is only creeping (small slope), so we keep
    /// commanding (clamped) rather than demoting. The commanded bias stays
    /// clamped throughout; only the WARN surface changes.
    #[test]
    fn sustained_high_demand_raises_l1_warn() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 100.0);
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        // A persistent moderate fill error (err = 1384 - 384 = 1000 frames)
        // drives the DLL demand well past L1_WARN_PPM within the sustain window,
        // while a SMALL steady slope (+100 ppm < probe_ppm/2 = 150) keeps the L2
        // "uncorrected" evidence from firing.
        let mut cap = hc_cap_start();
        let mut play = cap;
        let mut saw_l1 = false;
        let mut ever_demoted = false;
        for i in 0..(L1_SUSTAIN_TICKS + 80) {
            cap += (48000.0 * (1.0 + 100.0 / 1.0e6)) as u64; // +100 ppm slope
            play += 48000;
            hc.tick(obs(true, true, 1384.0, cap, play), (100 + i as u64) * 1000);
            if hc.ladder() == Ladder::L1Warn {
                saw_l1 = true;
            }
            if hc.ladder() == Ladder::L2Fallback {
                ever_demoted = true;
            }
            // The command never leaves the clamp even while warned.
            assert!(hc.commanded_ppm().abs() <= MAX_BIAS_PPM + 1e-6);
        }
        assert!(saw_l1, "sustained high demand must raise L1_WARN");
        assert!(
            !ever_demoted,
            "a tracking-but-high loop must WARN (L1), not demote (L2)"
        );
    }

    // ---- state.json fragment (byte-exact twin fixture) ---------------------

    /// BYTE-EXACT contract pin. The disabled default fragment must match this
    /// string verbatim. Its Python twins
    /// (`tests/test_usbsink_host_clock_contract.py`,
    /// `tests/test_fanin_host_clock_contract.py`) grep this identical literal
    /// out of this source, so the expected value is a RAW string literal
    /// (`r#"..."#`) — the bare (unescaped) `"` bytes appear contiguously in the
    /// source, exactly matching the Python side's bare-quote fixture. Same
    /// twin-fixture discipline as the Stage 0 tap
    /// (`tap_event_jsonl_shape_is_stable`).
    #[test]
    fn host_clock_fragment_shape_is_stable() {
        let mut cfg = enabled_cfg();
        cfg.enabled = false;
        let hc = HostClock::new(cfg);
        let fragment = hc.status_fragment();
        assert_eq!(
            fragment,
            r#"{"enabled":false,"ladder":"disabled","pitch_ppm_commanded":0.0,"fill_frames":0,"fill_slope_ppm":0.00,"fill_variance":0.00,"dll":{"err_frames":0.00,"locked":false},"probe":{"last_result":"none","response_ratio":null},"demotions":0,"transitions":0,"last_transition_reason":"startup"}"#
        );
        // And it parses as valid JSON.
        let parsed: serde_json::Value = serde_json::from_str(&fragment).unwrap();
        assert_eq!(parsed["enabled"].as_bool(), Some(false));
        assert_eq!(parsed["ladder"].as_str(), Some("disabled"));
        assert!(parsed["probe"]["response_ratio"].is_null());
    }

    /// The `log_prefix` parameterization: the status_fragment shape does NOT
    /// depend on the prefix (both daemons emit byte-identical wire telemetry),
    /// and the prefix is carried on the config so each daemon's `event=` lines
    /// stay namespaced. A fan-in-prefixed disabled config renders the SAME
    /// fragment as the usbsink-prefixed one — the prefix is a log-only knob.
    #[test]
    fn log_prefix_does_not_alter_wire_fragment() {
        let usb = {
            let mut cfg = enabled_cfg();
            cfg.enabled = false;
            cfg.log_prefix = "usbsink_audio";
            HostClock::new(cfg).status_fragment()
        };
        let fanin = {
            let mut cfg = enabled_cfg();
            cfg.enabled = false;
            cfg.log_prefix = "fanin";
            HostClock::new(cfg).status_fragment()
        };
        assert_eq!(
            usb, fanin,
            "the status_fragment wire shape must be identical across daemons; \
             log_prefix only namespaces the event= log lines"
        );
        // And the disabled() ctor threads the prefix through unchanged.
        let disabled_fanin = HostClockConfig::disabled(256, "fanin");
        assert_eq!(disabled_fanin.log_prefix, "fanin");
        assert!(!disabled_fanin.enabled);
    }

    #[test]
    fn ladder_tokens_match_contract() {
        assert_eq!(Ladder::Disabled.as_str(), "disabled");
        assert_eq!(Ladder::Probing.as_str(), "probing");
        assert_eq!(Ladder::L0Locked.as_str(), "l0_locked");
        assert_eq!(Ladder::L1Warn.as_str(), "l1_warn");
        assert_eq!(Ladder::L2Fallback.as_str(), "l2_fallback");
    }

    // ---- Ctl-write serialization (mock) ------------------------------------

    struct MockPitchCtl {
        writes: Vec<i64>,
    }
    impl PitchCtl for MockPitchCtl {
        fn write(&mut self, value: i64) -> Result<(), String> {
            self.writes.push(value);
            Ok(())
        }
    }

    /// All writes flow through ONE PitchCtl handle; the ladder emits at most one
    /// WritePitch per tick, and the actuator translates each to a single ctl
    /// write. This mirrors the owning thread's single-writer structure.
    #[test]
    fn all_writes_go_through_one_ctl_at_one_per_tick() {
        let mut hc = HostClock::new(enabled_cfg());
        let mut ctl = MockPitchCtl { writes: Vec::new() };
        // Startup neutralize → one write.
        if let Some(Action::WritePitch { ppm, .. }) = hc.startup_neutralize() {
            ctl.write(ppm_to_ctl_value(ppm)).unwrap();
        }
        let mut cap = 0u64;
        let mut play = 0u64;
        for t in 1u64..40 {
            cap += (48000.0 * (1.0 + 150.0 / 1.0e6)) as u64;
            play += 48000;
            let actions = hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
            // At most one pitch write per tick.
            assert!(
                actions
                    .iter()
                    .filter(|a| matches!(a, Action::WritePitch { .. }))
                    .count()
                    <= 1,
                "at most one ctl write per tick"
            );
            for a in actions {
                let Action::WritePitch { ppm, .. } = a;
                ctl.write(ppm_to_ctl_value(ppm)).unwrap();
            }
        }
        // First write is neutral.
        assert_eq!(ctl.writes[0], 1_000_000);
        // Every written value is inside the hw range.
        assert!(ctl
            .writes
            .iter()
            .all(|&v| (750_000..=1_005_000).contains(&v)));
    }

    // ---- Helpers -----------------------------------------------------------

    fn hc_cap_start() -> u64 {
        // A large starting cumulative frame count so divergence deltas dominate.
        1_000_000_000
    }

    /// Fast-forward a HostClock into L0_LOCKED with a compliant probe at the
    /// given crystal offset. Used by servo tests to isolate the locked loop.
    fn drive_to_l0(hc: &mut HostClock, offset_ppm: f64) {
        let mut cap: u64 = 0;
        let mut play: u64 = 0;
        for t in 1u64..14 {
            let elapsed_s = t.saturating_sub(1);
            // Compliant: follows the +probe_ppm step during the step phase.
            let host_ppm = if elapsed_s >= 4 {
                offset_ppm + 300.0
            } else {
                offset_ppm
            };
            cap += (48000.0 * (1.0 + host_ppm / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 384.0, cap, play), t * 1000);
        }
    }
}
