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
//! It is the shared home for BOTH USB clock owners:
//! - **solo (aloop) mode**: `jasper-usbsink-audio` owns the gadget capture and
//!   drives this from its state publisher (`JASPER_USBSINK_HOST_CLOCK`). This is
//!   the only consumer at this stack level.
//! - **combo (USB DIRECT) mode**: once that mode lands, `jasper-fanin` will own
//!   the gadget capture and drive this from a dedicated thread
//!   (`JASPER_FANIN_HOST_CLOCK`).
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

/// CORRECTION-mode outer-loop integral gain (ppm of command per ppm of smoothed
/// correction error, per 1 s tick). CORRECTION mode does **not** use the outer
/// DLL as its control law — the plant is structurally different from FILL mode
/// and the DLL's constants are wrong for it (review PR #1144). In FILL mode the
/// plant is an INTEGRATOR (a rate command sets the gadget-fill *slope*, so the
/// error the DLL sees is a slowly-ramping frame count); the DLL's own
/// integrators plus that plant integrator is the well-behaved cascade the module
/// docs describe. In CORRECTION mode the plant is instead **near-unity DC gain
/// through the inner resampler's lag** (a ppm command becomes, after the inner
/// `RateController` settles, ~the same ppm of correction — ppm→ppm, no
/// integrator). Driving that unity-gain-plus-lag plant with the FILL-tuned
/// third-order DLL puts loop gain > 1 past 180° of phase (inner lag at its
/// 0.016 Hz locked floor τ≈10 s + the 0.3-α estimator + 1 s sampling + host
/// application lag), so it limit-cycles — the exact fighting cascade the review
/// caught (compliant Mac at +20 ppm rails correction ±460 ppm on a ~21 s period).
///
/// The fix is a **pure-integral outer law** for CORRECTION mode: a single slow
/// integrator around the near-unity plant. One integrator + one dominant plant
/// pole is unconditionally stable for a small enough gain, and the feed-forward
/// seed (`−baseline_correction`) already cancels the DC crystal offset so the
/// integrator only trims the residual. `Ki = 0.05` was chosen against the REAL
/// inner `RateController` (composed exactly as `lane_resampler` builds it) across
/// the crystal / lag / noise / 3600 s matrix: it is a factor of ~5 below the
/// stability edge (`Ki ≳ 0.25` begins to ring, `≥ 0.5` limit-cycles), settles a
/// 100–250 ppm feed-forward miss in ~30–90 s with no overshoot, and holds
/// `correction_ppm → 0` with `cmd_p2p` at the ctl-quantization floor. Not
/// env-tunable (contract §2 fixed-constant discipline); a `const` a test pins.
pub const CORRECTION_INTEGRAL_GAIN: f64 = 0.05;

// Compile-time tripwire: the shipped integral gain must stay a healthy factor
// below the ~0.25 ring threshold measured against the real inner loop, and be
// strictly positive (a zero/negative gain would freeze or reverse the trim). If
// someone bumps `CORRECTION_INTEGRAL_GAIN` toward instability this fails the
// BUILD, not just a test — the servo-sim in `tests` proves it converges at this
// value, this pins that the value can't silently drift up.
const _: () = assert!(CORRECTION_INTEGRAL_GAIN > 0.0 && CORRECTION_INTEGRAL_GAIN <= 0.1);

/// CORRECTION-mode probe step-phase duration, in seconds — LONGER than FILL
/// mode's `probe_step_secs` because the observable is slower to respond.
///
/// The probe reads how far the observable MOVES under a pitch step. In FILL mode
/// the observable is the gadget-fill slope, which responds within a couple of
/// seconds (the fill starts diverging the instant the host rate changes). In
/// CORRECTION mode the observable is the INNER resampler's correction ppm, which
/// only moves as fast as that inner spa_dll loop can slew — and at its locked
/// floor (bw = 0.016 Hz, τ ≈ 10 s) that is much slower. Measured against the real
/// inner loop (review PR #1144): a 6 s step window reads a compliant −250 ppm
/// host at response_ratio ≈ +0.16 (FAIL — the inner correction has only slewed a
/// fraction of the way), but a 15 s window reads it at +0.84 (PASS), and the mid-
/// range stays ≥ +0.84 out to ±250 ppm. So the CORRECTION probe holds the step
/// for this fixed longer window regardless of `probe_step_secs`. FILL mode is
/// unchanged (hardware-validated at 6 s). Not env-tunable (contract §2); a `const`
/// a test pins.
pub const CORRECTION_PROBE_STEP_SECS: u64 = 15;

/// CORRECTION-mode probe: how far the baseline observable must be from zero (in
/// ppm, toward a rail) before the probe steps AWAY from that rail instead of
/// always stepping `+probe_ppm`.
///
/// The inner resampler's correction authority is only ±500 ppm. If the host's
/// crystal already sits near one rail (say +450 ppm, so baseline correction
/// ≈ +450), a fixed `+probe_ppm` step pushes a compliant host past +500 where the
/// inner correction CLAMPS — so the observable can only move the ~50 ppm of
/// remaining headroom and the response_ratio false-negatives (measured +0.19–0.33
/// against the real loop, FAIL, feature silently dead for that host — review
/// PR #1144). Worse, a headroom-normalized verdict then over-credits a NON-
/// compliant near-rail host (its natural crystal drift pushes correction the same
/// way). The physical fix: step AWAY from the nearer rail — command the host
/// SLOWER when its baseline correction is strongly positive, FASTER when strongly
/// negative — so a compliant response always has authority to show and a non-
/// compliant host's natural drift runs OPPOSITE the step (clearly negative ratio).
/// The verdict then normalizes by the SIGNED step actually applied, so compliant
/// → +1 in either direction. This deadband keeps the default `+probe_ppm` step for
/// near-zero baselines (the common Mac case) and only flips direction when the
/// baseline is genuinely near a rail. FILL mode is unaffected (always `+probe_ppm`).
pub const CORRECTION_PROBE_FLIP_DEADBAND_PPM: f64 = 150.0;

/// CORRECTION-mode anti-windup: when the total command is railed at
/// ±[`MAX_BIAS_PPM`] and the integral trim is still pushing further in the WRONG
/// direction relative to the current correction error (a windup that would keep
/// the command railed the wrong way and drain the fan-in cushion), the integrator
/// is HELD (not stepped further) so the next in-band error walks it back. This is
/// the CORRECTION-mode analog of the FILL-mode DLL reset-and-reapply — a pure
/// integrator has no hidden `z2+z3` to reset, so freezing its accumulation is the
/// equivalent bounded response. Gated on the correction error being non-trivial,
/// mode-scaled the same way the FILL gate is (half the probe step).
// The outer DLL's loop timescale (FILL mode only). `period / rate` is the DLL's
// per-update timescale in seconds; with a 1 s tick and this period/rate the
// effective bandwidth is `BW_MIN × (period/rate) / T_tick = 0.016 × 0.1 / 1 =
// 0.0016 Hz`.
const OUTER_DLL_PERIOD: f64 = 4800.0;
const OUTER_DLL_RATE: f64 = 48000.0;

/// Which observable the probe and the L0 servo run on — a TYPED, per-daemon
/// choice, never inferred from the data. The two USB clock owners feed
/// structurally different observables:
///
/// - [`ObsMode::Fill`] — **usbsink solo (aloop) mode.** No rate-matching stage
///   sits between the gadget ring and playback, so the gadget FILL slope is a
///   faithful readout of the host-vs-DAC rate error. The probe reads the fill
///   slope response; the L0 servo drives `fill − target → 0`. This is the
///   original servo, unchanged.
/// - [`ObsMode::Correction`] — **fan-in combo (USB DIRECT) mode.** The lane
///   resampler (±500 ppm authority) sits between the gadget ring and the mix and
///   ABSORBS host-clock drift to hold its fill at the held target. The fill
///   observable is therefore structurally dead: the resampler flattens the slope
///   the probe wants to measure and pins the fill by its own action, not by the
///   pitch commands (hardware-diagnosed on jts.local 2026-07-03 — the fill-based
///   probe reliably failed `response_ratio=-0.88` and the ladder parked in
///   `l2_fallback`; even a prior `l0_locked` had a dead fill-error signal). In
///   this mode the honest observable is the resampler's own live correction ppm
///   ([`Obs::correction_ppm`]): the probe reads how far the resampler's
///   correction MOVES in response to the pitch step, and the L0 servo drives
///   `correction_ppm → 0` (correction ≈ 0 sustained ⇒ the host is truly slaved,
///   the resampler idle, and the fill rides the resampler's target for free).
///
/// The mode is carried on [`HostClockConfig`] so each daemon states its
/// observable explicitly at construction; the ladder branches on it at exactly
/// two points (the probe response observable and the L0 error signal) and shares
/// everything else — one servo core, one clamp/deadband/cadence/anti-windup path,
/// one ladder-demotion machine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ObsMode {
    /// Fill-slope observable (usbsink solo). The gadget fill directly reads the
    /// host-vs-DAC rate error; no rate-matching stage flattens it.
    Fill,
    /// Resampler-correction-ppm observable (fan-in combo). A lane resampler
    /// absorbs drift, so the fill is dead weight; the resampler's live correction
    /// ppm is the honest rate-error readout.
    Correction,
}

impl ObsMode {
    /// The lowercase token surfaced in telemetry / logs (pinned by a test).
    pub fn as_str(self) -> &'static str {
        match self {
            ObsMode::Fill => "fill",
            ObsMode::Correction => "correction",
        }
    }
}

/// Ladder state — the lock authority. `dll.locked` is diagnostic only (it is
/// expected false under the 256-frame ring quantization); THIS enum decides
/// whether the speaker trusts the host to follow the feedback.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ladder {
    /// Feature off, or no session yet. Pitch neutral, no DLL, no probe.
    Disabled,
    /// A session started; running the compliance probe (await-lock → baseline →
    /// step) before trusting the host. The await-lock phase holds neutral until
    /// the lane leaves its warmup ramp so the baseline measures clock drift.
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
    /// Pre-probe wait: commanding neutral, holding until the lane reports
    /// [`Obs::locked`] continuously for [`PROBE_SETTLE_SECS`] before baselining.
    /// This is where the probe sits from the session rising edge until the lane
    /// leaves its warmup ramp, so the baseline measures clock drift, not the
    /// resampler's one-time fill ramp. Lock loss here (or in a later phase)
    /// returns to this state and restarts the settle timer.
    AwaitLock,
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
    /// Which observable the probe + L0 servo run on (see [`ObsMode`]). usbsink
    /// solo passes [`ObsMode::Fill`]; fan-in combo passes [`ObsMode::Correction`].
    /// TYPED per daemon, never inferred — the ladder branches on it at the two
    /// observable-specific points and shares everything else.
    pub obs_mode: ObsMode,
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
    pub fn disabled(log_prefix: &'static str) -> Self {
        Self {
            enabled: false,
            target_fill_frames: 384.0,
            probe_ppm: 300.0,
            probe_step_secs: 6,
            // A disabled ladder never probes or servos, so the observable mode is
            // moot; default to `Fill` (the original behaviour). A daemon that runs
            // in `Correction` mode (fan-in) builds its own enabled config with the
            // right mode — this ctor is only the inert/neutralize-only shape.
            obs_mode: ObsMode::Fill,
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
    /// The lane is in its STEADY regime — the gate for starting the probe's
    /// baseline. A session (`host_connected && playing && !preempted`) begins the
    /// moment audio starts flowing, but at that instant the lane is still in its
    /// warmup ramp: the fan-in resampler's held target is filling from empty (0 →
    /// held target) and the gadget ring is priming. Baselining THEN measures that
    /// one-time warmup fill ramp as if it were the host's natural rate slope,
    /// contaminating the probe verdict (hardware-diagnosed on jts.local
    /// 2026-07-03: `baseline_slope_ppm=1460.6` measured the ramp, not clock
    /// drift). So the ladder holds a pre-probe wait, commanding neutral, until
    /// this signal is true (plus a settle delay). Each daemon maps its own steady
    /// indicator: fan-in → resampler LOCKED (its warmup ramp is a genuine 0→held
    /// -target fill climb that must complete before baselining); usbsink solo →
    /// simply `playing`. usbsink's only start-of-session contaminant is the
    /// sub-second gadget-ring prime + capture-backlog slurp, which the settle
    /// delay covers — a live ring-fill gate would DEADLOCK the probe there,
    /// because nothing steers the ring toward target while the probe holds
    /// neutral, so a host slower than our DAC keeps the ring at its underflow
    /// floor forever (see `obs_from_shared` in the usbsink shim).
    pub locked: bool,
    /// Gadget PeriodRing fill in frames (ring_fill_periods × period_frames).
    pub fill_frames: f64,
    /// Cumulative captured frames (monotone).
    pub capture_frames: u64,
    /// Cumulative frames delivered to playback (monotone).
    pub playback_frames: u64,
    /// The lane resampler's LIVE correction ppm (its rate-adjustment relative to
    /// nominal, `(ratio − 1) × 1e6`). Meaningful ONLY in [`ObsMode::Correction`]
    /// (fan-in combo): it is the honest host-vs-DAC rate-error readout when a
    /// rate-matching stage sits between the gadget ring and the mix. In
    /// [`ObsMode::Fill`] (usbsink solo) there is no such stage, so this is `0.0`
    /// and never consulted. Sign convention (see [`ObsMode::Correction`]): the
    /// resampler feeds `error = fill − target` (capture-follower), so a host
    /// running FAST fills the ring, driving the resampler's correction ppm
    /// POSITIVE (consume faster to hold fill). Commanding the host +ppm (faster)
    /// therefore moves the resampler correction MORE positive; slaving the host to
    /// the DAC drives it toward 0.
    pub correction_ppm: f64,
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
    // EW mean of the lane resampler's correction ppm — the CORRECTION-mode probe
    // observable (dead in FILL mode, where `Obs::correction_ppm` is always 0). It
    // is smoothed on the SAME alpha as the slope so the two modes' probe windows
    // have identical memory, and re-armed at the same session boundaries.
    correction_mean_ppm: f64,
    have_correction: bool,
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
            correction_mean_ppm: 0.0,
            have_correction: false,
        }
    }

    /// Feed one tick. `divergence = capture − playback` frames (signed),
    /// `fill_frames` the gadget ring fill, `correction_ppm` the lane resampler's
    /// live correction ppm (0 in FILL mode), `frames_per_tick` the expected
    /// on-rate frame count over one tick (rate × Δt). Returns the smoothed
    /// slope in ppm.
    fn update(
        &mut self,
        divergence: i64,
        fill_frames: f64,
        correction_ppm: f64,
        frames_per_tick: f64,
    ) -> f64 {
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

        // EW mean of the resampler correction ppm (CORRECTION-mode observable).
        if correction_ppm.is_finite() {
            if self.have_correction {
                self.correction_mean_ppm +=
                    self.alpha * (correction_ppm - self.correction_mean_ppm);
            } else {
                self.correction_mean_ppm = correction_ppm;
                self.have_correction = true;
            }
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

    /// The smoothed resampler correction ppm (CORRECTION-mode probe/servo signal).
    /// 0 until the first finite sample (and always 0 in FILL mode, where the input
    /// is 0).
    fn correction_mean_ppm(&self) -> f64 {
        if self.have_correction {
            self.correction_mean_ppm
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
        self.correction_mean_ppm = 0.0;
        self.have_correction = false;
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
    /// slope; the outer trim (DLL in FILL mode, integral in CORRECTION mode)
    /// trims the residual around it.
    feed_forward_ppm: f64,
    /// CORRECTION-mode outer-loop integral accumulator, in ppm. The pure-integral
    /// control law for CORRECTION mode ([`CORRECTION_INTEGRAL_GAIN`]) steps this
    /// each locked tick by `−Ki · correction_mean`; the DLL is not ticked in that
    /// mode. Zero in FILL mode (which uses the DLL). Reset to 0 on every L0 entry
    /// and every session/probe boundary alongside `feed_forward_ppm` and the DLL.
    correction_trim_ppm: f64,
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
    /// Monotonic ms at which the lane first reported `locked` in the current
    /// AwaitLock wait; `None` until lock is seen (or after it is lost). The probe
    /// leaves AwaitLock once `now_ms − lock_since_ms >= PROBE_SETTLE_SECS`.
    lock_since_ms: Option<u64>,
    /// The probe's baseline/step observable, recorded at each phase boundary.
    /// In FILL mode these are the fill SLOPE means; in CORRECTION mode they are
    /// the resampler-CORRECTION means (whichever observable the mode selects). One
    /// pair, reused, so the probe-verdict machinery is byte-identical across modes.
    probe_baseline_obs_ppm: f64,
    probe_step_obs_ppm: f64,
    /// The SIGNED pitch step actually applied in the current probe's step phase.
    /// FILL mode always uses `+probe_ppm`; CORRECTION mode steps AWAY from the
    /// nearer inner-authority rail (see [`CORRECTION_PROBE_FLIP_DEADBAND_PPM`]), so
    /// this can be `−probe_ppm`. `finish_probe` normalizes the response by THIS
    /// signed magnitude so a compliant host reads ≈ +1 in either direction.
    probe_step_ppm: f64,
    probe_result: ProbeResult,
    response_ratio: Option<f64>,
    l1_high_ticks: u32,
    l2_evidence_ticks: u32,
    /// The most recent smoothed resampler correction ppm — the CORRECTION-mode
    /// L0 end-state observable (drives to ~0 when the host is truly slaved).
    /// Surfaced in the status fragment (additive). Always 0 in FILL mode.
    last_correction_ppm: f64,

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

/// Settle delay after the lane first reports [`Obs::locked`] before the probe's
/// baseline phase begins. The lock edge means "warmup ramp done", but the fill
/// can still be settling for a tick or two as the inner rate controller trims
/// the last of the ramp; this holds neutral a little longer so the baseline
/// slope reflects the steady host rate, not the tail of the ramp. Measured on
/// the tick clock (accumulated `now_ms`), never wall time — so it is fake-time
/// testable and unaffected by the owning thread's wake jitter. Not env-tunable
/// (contract §2 fixed-constant discipline); a `const` so a test can pin it.
pub const PROBE_SETTLE_SECS: u64 = 2;

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
            correction_trim_ppm: 0.0,
            commanded_ppm: 0.0,
            last_written_ppm: 0.0,
            last_write_ms: None,
            raw_demand_ppm: 0.0,
            saturated: false,
            ladder: Ladder::Disabled,
            probe_phase: ProbePhase::AwaitLock,
            probe_started_ms: 0,
            lock_since_ms: None,
            probe_baseline_obs_ppm: 0.0,
            probe_step_obs_ppm: 0.0,
            probe_step_ppm: 0.0,
            probe_result: ProbeResult::None,
            response_ratio: None,
            l1_high_ticks: 0,
            l2_evidence_ticks: 0,
            last_correction_ppm: 0.0,
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

    pub fn ladder(&self) -> Ladder {
        self.ladder
    }
    pub fn commanded_ppm(&self) -> f64 {
        self.commanded_ppm
    }

    /// Update the fill setpoint the locked loop disciplines toward. The setpoint
    /// is normally fixed at construction (usbsink solo mode), but fan-in combo
    /// mode shares it with the inner resampler's LIVE held target — which the
    /// DEFAULT-OFF post-lock cushion decay lowers over time — so the servo thread
    /// re-pins it each tick from the resampler's held-target gauge. This is the
    /// single-source-of-truth wiring: the resampler owns the value; the ladder
    /// only ever reads it. No effect on the ladder state — the next `tick_locked`
    /// simply sees the new `error = fill − target` (a bounded step the DLL
    /// already handles), so a slowly-descending setpoint is a gentle ramp, not a
    /// re-acquisition. `NaN`/non-finite is ignored (keeps the last good value).
    pub fn set_target_fill_frames(&mut self, target_fill_frames: f64) {
        if target_fill_frames.is_finite() {
            self.cfg.target_fill_frames = target_fill_frames;
        }
    }

    /// The fill setpoint the locked loop currently disciplines toward (for tests
    /// / telemetry). Tracks `set_target_fill_frames`.
    pub fn target_fill_frames(&self) -> f64 {
        self.cfg.target_fill_frames
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
    /// True iff a LIVE session's probe is in the pre-probe wait — `session_active`
    /// AND `Probing` AND holding neutral in [`ProbePhase::AwaitLock`] until the
    /// lane leaves its warmup ramp (locked for the settle window). Distinguishes
    /// "probing but waiting for the lane to settle" from "probing and actively
    /// measuring", which the bare `ladder=probing` token cannot. Surfaced in the
    /// status fragment.
    ///
    /// The `session_active` guard matters between sessions: `end_session` parks
    /// the ladder in `Probing`/`AwaitLock` (that is the armed-for-next-session
    /// resting state), so WITHOUT this guard an enabled-but-idle box would
    /// publish `waiting_for_lock:true` forever with nothing playing — reading as
    /// an active-session claim when no session is in flight. It is `false` while
    /// idle; it goes `true` only once a session's rising edge re-enters the wait.
    pub fn probe_waiting_for_lock(&self) -> bool {
        self.session_active
            && self.ladder == Ladder::Probing
            && self.probe_phase == ProbePhase::AwaitLock
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
        self.correction_trim_ppm = 0.0;
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

        // Update the slope + fill variance + correction mean every enabled tick
        // regardless of ladder state, so telemetry (and the falsifier) is always
        // live. `obs.correction_ppm` is 0 in FILL mode (usbsink solo), so the
        // correction estimator stays at 0 there and only ever moves in CORRECTION
        // mode (fan-in combo).
        let divergence = (obs.capture_frames as i64) - (obs.playback_frames as i64);
        self.slope.update(
            divergence,
            obs.fill_frames,
            obs.correction_ppm,
            frames_per_tick,
        );
        // Cache the smoothed correction ppm for the status fragment (additive;
        // 0 in FILL mode). The L0 servo reads the same value below.
        self.last_correction_ppm = self.slope.correction_mean_ppm();

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
            Ladder::Probing => self.tick_probe(obs, now_ms, &mut actions),
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
        // Enter the pre-probe wait: hold neutral until the lane reports `locked`
        // for PROBE_SETTLE_SECS. `probe_started_ms` is (re)seated at the AwaitLock
        // → Baseline transition, so the 4 s baseline window is measured from lock,
        // not from this session edge (that was the warmup-ramp contamination).
        self.probe_phase = ProbePhase::AwaitLock;
        self.lock_since_ms = None;
        self.probe_started_ms = now_ms;
        self.probe_baseline_obs_ppm = 0.0;
        self.probe_step_obs_ppm = 0.0;
        self.probe_step_ppm = 0.0;
        self.slope.rearm();
        self.dll.reset();
        self.feed_forward_ppm = 0.0;
        self.correction_trim_ppm = 0.0;
        // Command neutral for the baseline measurement (forced write).
        self.command(0.0, true, actions);
        log::info!(
            "event={}.host_clock_probe_wait reason=await_lock settle_s={} obs_mode={}",
            self.cfg.log_prefix,
            PROBE_SETTLE_SECS,
            self.cfg.obs_mode.as_str(),
        );
    }

    /// The probe/servo observable for the configured mode: the fill SLOPE
    /// (usbsink solo, FILL mode) or the resampler CORRECTION mean (fan-in combo,
    /// CORRECTION mode). This is the ONE observable-specific branch point on the
    /// measurement side — both values share the same sign property (a compliant
    /// host commanded +probe_ppm moves the observable +probe_ppm; its neutral
    /// baseline value is the host's natural excess rate), so the probe verdict and
    /// feed-forward math below are byte-identical across modes.
    fn probe_observable_ppm(&self) -> f64 {
        match self.cfg.obs_mode {
            ObsMode::Fill => self.slope.slope_ppm(),
            ObsMode::Correction => self.slope.correction_mean_ppm(),
        }
    }

    /// Whether the lane is in a SETTLE-ELIGIBLE regime this tick — the gate for
    /// the pre-probe [`ProbePhase::AwaitLock`] settle window to accrue. Lock-only
    /// in BOTH modes: [`Obs::locked`] is the whole gate.
    ///
    /// Lock-only is correct, NOT a missing rail check. A railed CORRECTION-mode
    /// baseline is still measurable and fail-biased, so gating settle on the rail
    /// buys nothing and deadlocks beyond-authority hosts:
    /// - The probe steps AWAY from the nearer inner-authority rail (see
    ///   [`CORRECTION_PROBE_FLIP_DEADBAND_PPM`]), so a compliant response is
    ///   visible even when the baseline is clipped at the ±500 ppm rail. (Verified
    ///   on jts.local hardware: a probe from `baseline=500` stepped to `258`,
    ///   `response_ratio=0.807` PASS.)
    /// - A STEADY-STATE clipped rail is fail-biased, never pass-biased: the
    ///   baseline is pinned at ±500 and stays there, so it UNDERSTATES demand and
    ///   shrinks the response numerator. A noncompliant host reads
    ///   `baseline=500 → step=500 → ratio≈0 → FAIL`. For a stationary rail,
    ///   removing the guard cannot manufacture a false PASS. (A DECAYING transient
    ///   rail is different: its observable slews toward the step direction as it
    ///   decays, which can mimic a compliant response and INFLATE the ratio. That
    ///   is a latent class the 450-ppm guard never closed either — it only delayed
    ///   baselining until the smoothed correction fell below 450, and a slow decay
    ///   keeps moving through the step window regardless. Transient-decay false
    ///   passes are closed by root-fixing the transient causes — the 2026-07-03
    ///   `NotL0` snap rail is fixed by #1161 — plus the `DllDemotion` / churn
    ///   one-strike and the two-strike ProbeFail revocation nets, NOT by this
    ///   settle gate.)
    /// - A rail guard here is a deadlock: a host beyond the ±500 inner authority
    ///   (jts.local's Mac runs ~+600 ppm fast) rails at +500 STEADY-STATE under
    ///   the neutral pitch AwaitLock commands. Coming off the rail needs the
    ///   servo's pitch authority; the servo waits on the probe; the probe (guard)
    ///   waits on the unrail — deadlock. Two sessions on 2026-07-05 sat in
    ///   AwaitLock for 6+ min, correction pinned 500.0, pitch 0.0, no proof.
    ///
    /// The transient the guard was built for (2026-07-03: a floor-primed lock
    /// whose held target snapped to the ceiling on the first `NotL0` decay tick,
    /// forcing a −500 rebuild rail) was ROOT-FIXED in fan-in by the prime-aware
    /// `NotL0` snap-back, so it no longer exists. In FILL mode there is no
    /// resampler, so this was already `obs.locked`.
    fn settle_regime_ok(&self, obs: Obs) -> bool {
        obs.locked
    }

    fn tick_probe(&mut self, obs: Obs, now_ms: u64, actions: &mut Vec<Action>) {
        // Lock loss AFTER the wait (during baseline or step) means the lane fell
        // back into its warmup regime — the measurement in flight is now
        // contaminated, so restart the wait rather than trust it. Handled first
        // so it takes priority over the phase's own elapsed-time checks.
        if !obs.locked && self.probe_phase != ProbePhase::AwaitLock {
            self.restart_probe_wait(actions);
            return;
        }

        let elapsed_ms = now_ms.saturating_sub(self.probe_started_ms);
        let baseline_ms = PROBE_BASELINE_SECS * 1000;
        // The step-phase duration is mode-specific: FILL uses the configured
        // `probe_step_secs` (hardware-validated at 6 s); CORRECTION holds the step
        // for the longer fixed window because its inner-loop observable is slower
        // to slew (see CORRECTION_PROBE_STEP_SECS).
        let step_secs = match self.cfg.obs_mode {
            ObsMode::Fill => self.cfg.probe_step_secs,
            ObsMode::Correction => CORRECTION_PROBE_STEP_SECS,
        };
        let step_ms = baseline_ms + step_secs * 1000;

        match self.probe_phase {
            ProbePhase::AwaitLock => {
                if self.settle_regime_ok(obs) {
                    // Track when the settle-eligible regime (locked) first became
                    // continuously true; once it has held for the settle window,
                    // begin the baseline. The timer is on the tick clock
                    // (`now_ms`), never wall time.
                    let since = *self.lock_since_ms.get_or_insert(now_ms);
                    if now_ms.saturating_sub(since) >= PROBE_SETTLE_SECS * 1000 {
                        // Seat the baseline window fresh from HERE and re-anchor the
                        // slope so the natural-rate measurement starts clean (the
                        // next tick's slope update is the baseline's first sample).
                        self.probe_phase = ProbePhase::Baseline;
                        self.probe_started_ms = now_ms;
                        self.slope.rearm();
                        log::info!(
                            "event={}.host_clock_probe_start ppm={:.0} baseline_s={} step_s={}",
                            self.cfg.log_prefix,
                            self.cfg.probe_ppm,
                            PROBE_BASELINE_SECS,
                            step_secs,
                        );
                    }
                } else {
                    // Not in the settle regime (not locked yet / lock lost): reset
                    // the timer so the settle window requires CONTINUOUS lock. A
                    // railed correction does NOT hold here — the probe steps away
                    // from the rail and reads it fail-biased (see
                    // `settle_regime_ok`); gating settle on the rail deadlocks a
                    // beyond-authority host.
                    self.lock_since_ms = None;
                }
            }
            ProbePhase::Baseline => {
                if elapsed_ms >= baseline_ms {
                    // Baseline done: record the natural observable (fill slope OR
                    // resampler correction, per mode), then command the step. The
                    // step DIRECTION is mode-specific: FILL always steps +probe_ppm;
                    // CORRECTION steps AWAY from the nearer inner-authority rail so a
                    // near-rail host still has room to show a compliant response (see
                    // CORRECTION_PROBE_FLIP_DEADBAND_PPM). The signed step is recorded
                    // so finish_probe normalizes the response by the right magnitude.
                    self.probe_baseline_obs_ppm = self.probe_observable_ppm();
                    self.probe_step_ppm = self.choose_probe_step_ppm();
                    self.probe_phase = ProbePhase::Step;
                    self.command(self.probe_step_ppm, true, actions);
                }
            }
            ProbePhase::Step => {
                if elapsed_ms >= step_ms {
                    self.probe_step_obs_ppm = self.probe_observable_ppm();
                    self.finish_probe(actions);
                }
            }
        }
    }

    /// The signed pitch step to apply for the probe's step phase. FILL mode always
    /// steps `+probe_ppm`. CORRECTION mode steps `−probe_ppm` when the baseline
    /// correction is strongly positive (host already near the +rail, so a +step
    /// would clamp) and `+probe_ppm` otherwise — stepping AWAY from the nearer rail
    /// so a compliant response always has inner authority to show. The deadband
    /// (`CORRECTION_PROBE_FLIP_DEADBAND_PPM`) keeps the default `+probe_ppm` for the
    /// common near-zero-baseline case (a Mac at a small crystal offset).
    fn choose_probe_step_ppm(&self) -> f64 {
        match self.cfg.obs_mode {
            ObsMode::Fill => self.cfg.probe_ppm,
            ObsMode::Correction => {
                if self.probe_baseline_obs_ppm > CORRECTION_PROBE_FLIP_DEADBAND_PPM {
                    -self.cfg.probe_ppm
                } else {
                    self.cfg.probe_ppm
                }
            }
        }
    }

    /// Lock was lost mid-measurement: drop back to the AwaitLock wait, command
    /// neutral, rearm the slope, and clear the settle timer so the next lock edge
    /// must again hold for PROBE_SETTLE_SECS before baselining. Stays in
    /// [`Ladder::Probing`] (no L2 demotion — a warmup re-entry is not a
    /// compliance failure).
    fn restart_probe_wait(&mut self, actions: &mut Vec<Action>) {
        self.probe_phase = ProbePhase::AwaitLock;
        self.lock_since_ms = None;
        self.probe_baseline_obs_ppm = 0.0;
        self.probe_step_obs_ppm = 0.0;
        self.probe_step_ppm = 0.0;
        self.slope.rearm();
        self.dll.reset();
        self.feed_forward_ppm = 0.0;
        self.correction_trim_ppm = 0.0;
        self.command(0.0, true, actions);
        log::info!(
            "event={}.host_clock_probe_wait reason=lock_lost settle_s={}",
            self.cfg.log_prefix,
            PROBE_SETTLE_SECS,
        );
    }

    fn finish_probe(&mut self, actions: &mut Vec<Action>) {
        // response_ratio = (step_obs − baseline_obs) / probe_step_ppm, normalized
        // by the SIGNED step actually applied (`probe_step_ppm`, not the unsigned
        // `probe_ppm`) so a compliant host reads ≈ +1 regardless of step direction.
        //
        // The observable differs by mode, but the sign property is the SAME, so
        // this formula and the +1-for-compliant normalization hold for both:
        //   * FILL (usbsink solo): always steps +probe_ppm; a compliant host shifts
        //     its delivery rate so the fill slope moves by ~+probe_ppm.
        //   * CORRECTION (fan-in combo): may step ±probe_ppm (away from the nearer
        //     inner-authority rail — see choose_probe_step_ppm). The resampler
        //     absorbs the host's clock, so it flattens the fill slope — but its OWN
        //     correction ppm reveals the host rate. A compliant host commanded
        //     `+step` runs faster (or `−step` slower), so its correction ppm moves
        //     by ~the same signed step; dividing by that signed step gives ratio
        //     ≈ +1 either way (see `ObsMode::Correction` / `Obs::correction_ppm`).
        //   In both modes a host that ignores the step moves the observable ~0 ⇒
        //   ratio ≈ 0 (a NON-compliant near-rail host's natural crystal drift runs
        //   OPPOSITE the away-from-rail step ⇒ clearly negative). Same pass band
        //   (>= 0.5) and demotion semantics.
        //
        // A degenerate `probe_step_ppm == 0` (never produced — the step is always
        // ±probe_ppm with probe_ppm > 0) would divide by zero; guard it to a fail.
        let ratio = if self.probe_step_ppm.abs() > f64::EPSILON {
            (self.probe_step_obs_ppm - self.probe_baseline_obs_ppm) / self.probe_step_ppm
        } else {
            0.0
        };
        self.response_ratio = Some(ratio);
        if ratio >= 0.5 {
            self.probe_result = ProbeResult::Pass;
            // Feed-forward: seed the commanded bias to cancel the measured baseline
            // rate offset so coarse correction is immediate; the slow DLL only trims
            // the residual. Sign holds across modes: a host running FAST shows a
            // POSITIVE baseline observable (fill climbing in FILL mode; resampler
            // consuming faster ⇒ positive correction ppm in CORRECTION mode), and
            // must be commanded SLOWER ⇒ negative bias.
            self.feed_forward_ppm = clamp_bias(-self.probe_baseline_obs_ppm);
            self.dll.reset();
            // CORRECTION mode's integral trim starts from 0 on L0 entry — the
            // feed-forward carries the DC crystal cancel and the integrator only
            // trims the residual around it (no-op in FILL mode, which uses the DLL).
            self.correction_trim_ppm = 0.0;
            self.transition_to(Ladder::L0Locked, "probe_pass");
            self.command(self.feed_forward_ppm, true, actions);
            log::info!(
                "event={}.host_clock_probe_result result=pass obs_mode={} response_ratio={:.3} baseline_obs_ppm={:.1} step_obs_ppm={:.1}",
                self.cfg.log_prefix,
                self.cfg.obs_mode.as_str(),
                ratio,
                self.probe_baseline_obs_ppm,
                self.probe_step_obs_ppm,
            );
        } else {
            self.probe_result = ProbeResult::Fail;
            self.demotions += 1;
            self.transition_to(Ladder::L2Fallback, "probe_fail");
            self.command(0.0, true, actions); // pitch → neutral
            log::info!(
                "event={}.host_clock_probe_result result=fail obs_mode={} response_ratio={:.3} baseline_obs_ppm={:.1} step_obs_ppm={:.1}",
                self.cfg.log_prefix,
                self.cfg.obs_mode.as_str(),
                ratio,
                self.probe_baseline_obs_ppm,
                self.probe_step_obs_ppm,
            );
        }
    }

    // ---- Locked (L0/L1) -----------------------------------------------------

    fn tick_locked(&mut self, obs: Obs, actions: &mut Vec<Action>) {
        // The control law AND its error signal are mode-specific (the ONE
        // observable-specific branch on the servo side). Both signs are chosen so
        // a POSITIVE error means "host too fast" and the response commands the
        // host SLOWER (`+error ⇒ trim < 0`) — closed negative feedback in both
        // modes.
        //
        //  * FILL (usbsink solo): `err = fill − target` (frames), and the outer
        //    control law is the DLL. Ring too full ⇒ err > 0 ⇒ command slower ⇒
        //    fill falls to target. The plant is an INTEGRATOR (a rate command sets
        //    the fill SLOPE), so the DLL's own integrators plus that plant
        //    integrator is the well-behaved cascade the module docs describe.
        //    (`RateController` is NOT reused — its consumer-drain sign is inverted
        //    for this producer-side use, and it hides the bandwidth knobs this
        //    cascade must pin. See module docs.) The end-state is fill ≈ target.
        //  * CORRECTION (fan-in combo): `err = correction_ppm` (the resampler's
        //    live correction, EW-smoothed), and the outer control law is a
        //    PURE INTEGRAL, NOT the DLL. The lane resampler pins the fill by its
        //    OWN action, so `fill − target` is dead weight; instead we drive the
        //    resampler's correction to 0. correction > 0 (host faster than DAC, the
        //    resampler consuming faster to hold fill) ⇒ err > 0 ⇒ command host
        //    slower ⇒ the host slaves to the DAC and the correction relaxes to ~0.
        //    The plant here is near-unity DC gain through the inner loop's lag
        //    (ppm→ppm, no integrator), so the FILL-tuned third-order DLL would
        //    limit-cycle against it (review PR #1144); a single slow integrator
        //    ([`CORRECTION_INTEGRAL_GAIN`]) around a near-unity plant is
        //    unconditionally stable at this gain, with the feed-forward carrying
        //    the DC crystal cancel. The end-state is correction_ppm ≈ 0, at which
        //    point the resampler is idle and the fill rides its held target for
        //    free.
        let err = match self.cfg.obs_mode {
            ObsMode::Fill => obs.fill_frames - self.cfg.target_fill_frames,
            ObsMode::Correction => self.slope.correction_mean_ppm(),
        };

        // ---- Outer trim (mode-specific control law) + anti-windup -----------
        // The ±MAX_BIAS_PPM clamp is a SAFETY bound on the ACTUATOR, not a bound
        // on the loop's internal integrators — the jasper-clock docs call out the
        // clamped-actuator windup regime. A long railed excursion can leave the
        // loop demanding correction in the WRONG direction after the error has
        // crossed back past zero, so the command stays railed the wrong way and
        // drains the fan-in cushion (the inner lane resampler's authority is only
        // ±500 ppm — see rust/jasper-fanin/src/config.rs). The "the error is
        // genuinely non-trivial" gate that arms anti-windup is mode-scaled: half a
        // period (128 frames) in FILL, half the probe step in CORRECTION (where
        // err is a ppm, not a frame count).
        let anti_windup_threshold = match self.cfg.obs_mode {
            ObsMode::Fill => ANTI_WINDUP_THRESHOLD_FRAMES,
            ObsMode::Correction => self.cfg.probe_ppm / 2.0,
        };
        let trim_ppm = match self.cfg.obs_mode {
            ObsMode::Fill => {
                self.dll.update(err);
                let mut dll_trim_ppm = self.dll.ratio_ppm();
                // When the total demand is railed AND the DLL is wound against the
                // current error, reset the loop and re-apply the error so the
                // first bounded output points back toward the target. Mirrors
                // jasper_resampler::RateController::is_wound_against_error
                // (reset-and-reapply idiom); the SIGN test differs by
                // construction: there the DLL is fed −error so a wound loop has
                // raw_ppm.sign == error.sign; here the DLL is fed +error (producer
                // sign), so normal operation has trim.sign == −err.sign and a
                // WOUND loop is trim.sign == err.sign.
                let total_raw = self.feed_forward_ppm + dll_trim_ppm;
                if total_raw.is_finite()
                    && total_raw.abs() > MAX_BIAS_PPM
                    && err.abs() >= anti_windup_threshold
                    && dll_trim_ppm.signum() != 0.0
                    && err.signum() != 0.0
                    && dll_trim_ppm.signum() == err.signum()
                {
                    self.dll.reset();
                    self.anti_windup_events = self.anti_windup_events.saturating_add(1);
                    self.dll.update(err);
                    dll_trim_ppm = self.dll.ratio_ppm();
                }
                dll_trim_ppm
            }
            ObsMode::Correction => {
                // Pure-integral outer law: `trim += −Ki · err` (negative feedback,
                // matching the FILL sign — a positive correction error commands the
                // host slower). The DLL is intentionally NOT ticked in this mode.
                //
                // Anti-windup by CONDITIONAL INTEGRATION: a pure integrator has no
                // hidden `z2+z3` to reset, and the ±MAX_BIAS_PPM clamp is applied to
                // the OUTPUT (`raw` below), not the accumulator — so left unchecked
                // the accumulator would wind past the rail and, once the error
                // reverses, take many ticks to unwind while the command stays
                // pinned. The standard fix is to skip the integration step whenever
                // the total command is ALREADY railed and this step would push it
                // FURTHER into the rail (`step` same sign as the railed total).
                // Steps that move the total back toward zero always apply, so the
                // integrator unwinds immediately when the error reverses. Gated on a
                // non-trivial error (probe_ppm/2) so ordinary near-target jitter
                // doesn't count as a windup event.
                let step = -CORRECTION_INTEGRAL_GAIN * err;
                let candidate = self.correction_trim_ppm + step;
                let total_raw = self.feed_forward_ppm + candidate;
                let railed_further = total_raw.is_finite()
                    && total_raw.abs() > MAX_BIAS_PPM
                    && err.abs() >= anti_windup_threshold
                    && step.signum() != 0.0
                    && total_raw.signum() == step.signum();
                if railed_further {
                    self.anti_windup_events = self.anti_windup_events.saturating_add(1);
                    // Hold the integrator (do not accumulate further into the rail);
                    // the output clamp below still bounds the command.
                } else if candidate.is_finite() {
                    self.correction_trim_ppm = candidate;
                }
                self.correction_trim_ppm
            }
        };

        // Total raw demand = feed-forward seed + outer trim. The clamp bounds the
        // COMMAND; the raw demand still drives L1/L2 evidence so a railed host
        // is visible.
        let raw = self.feed_forward_ppm + trim_ppm;
        self.raw_demand_ppm = raw;

        // ---- L2 mid-stream demotion evidence --------------------------------
        // Saturated command AND the observable still points the WRONG way (the
        // host is not following) for L2_SUSTAIN_TICKS ⇒ demote. The threshold is
        // max(probe_ppm/2, L2_SLOPE_FLOOR_PPM) — demotion sensitivity is a
        // physical question decoupled from the probe STEP magnitude, so a small
        // probe cannot make demotion hair-trigger nor let a residual wrong-way
        // drift under a railed command escape it forever (review S3). The
        // observable is mode-specific: the fill SLOPE in FILL mode, the resampler
        // CORRECTION in CORRECTION mode — both share the "host too fast ⇒ positive"
        // sign, so the same wrong-way test applies to both.
        let saturated = raw.abs() >= MAX_BIAS_PPM;
        let observable = self.probe_observable_ppm();
        let l2_slope_threshold = (self.cfg.probe_ppm / 2.0).max(L2_SLOPE_FLOOR_PPM);
        // "Uncorrected direction": we are commanding to reduce the error, but the
        // observable magnitude is still worse than the L2 threshold in the
        // uncorrected direction. Sign check: if commanding negative (slow host) yet
        // the observable is still strongly positive (host still fast), the host
        // ignores us — and mutatis mutandis for the other sign.
        let uncorrected = (raw < 0.0 && observable > l2_slope_threshold)
            || (raw > 0.0 && observable < -l2_slope_threshold);
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
            self.correction_trim_ppm = 0.0;
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
        // Distinguish a probe that was actively MEASURING (Baseline/Step — a real
        // measurement in flight, now aborted) from one that never left the
        // pre-probe AwaitLock wait (no baseline was ever taken — nothing to
        // abort). Logging "result=aborted" for the latter reads as measurement
        // churn in the journal and hides the "never started" shape (e.g. a box
        // stuck in AwaitLock). Emit a distinct token and leave `probe_result`
        // untouched in the await-lock case, since no verdict was produced.
        if self.ladder == Ladder::Probing {
            match self.probe_phase {
                ProbePhase::AwaitLock => {
                    log::info!(
                        "event={}.host_clock_probe_result result=await_lock_ended response_ratio=null baseline_slope_ppm=null step_slope_ppm=null",
                        self.cfg.log_prefix
                    );
                }
                ProbePhase::Baseline | ProbePhase::Step => {
                    self.probe_result = ProbeResult::Aborted;
                    log::info!(
                        "event={}.host_clock_probe_result result=aborted response_ratio=null baseline_slope_ppm=null step_slope_ppm=null",
                        self.cfg.log_prefix
                    );
                }
            }
        }
        self.feed_forward_ppm = 0.0;
        self.correction_trim_ppm = 0.0;
        self.l1_high_ticks = 0;
        self.l2_evidence_ticks = 0;
        // ANY → PROBING(await-lock) at the idle boundary; pitch → neutral. This
        // is the ONLY place L2 re-promotes toward PROBING.
        self.transition_to(Ladder::Probing, reason);
        self.probe_phase = ProbePhase::AwaitLock;
        self.lock_since_ms = None;
        self.dll.reset();
        self.slope.rearm();
        self.command(0.0, true, actions);
        // The rising edge on the next (session) tick will begin_probe again.
        // Until then we sit Probing/AwaitLock with neutral pitch; session_active
        // is false so tick() short-circuits to idle (and probe_waiting_for_lock()
        // reads false while idle — it is session_active-gated).
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
    /// Python twin (`tests/test_usbsink_host_clock_contract.py`; a
    /// `tests/test_fanin_host_clock_contract.py` twin arrives with combo mode).
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
                "\"obs_mode\":\"{}\",",
                "\"pitch_ppm_commanded\":{:.1},",
                "\"fill_frames\":{:.0},",
                "\"fill_slope_ppm\":{:.2},",
                "\"fill_variance\":{:.2},",
                "\"correction_ppm\":{:.2},",
                "\"dll\":{{\"err_frames\":{:.2},\"locked\":{}}},",
                "\"probe\":{{\"last_result\":\"{}\",\"response_ratio\":{},\"waiting_for_lock\":{}}},",
                "\"demotions\":{},",
                "\"transitions\":{},",
                "\"last_transition_reason\":\"{}\"",
                "}}"
            ),
            json_bool(self.cfg.enabled),
            self.ladder.as_str(),
            self.cfg.obs_mode.as_str(),
            self.commanded_ppm,
            self.published_fill_frames(),
            self.published_slope_ppm(),
            self.fill_variance(),
            self.published_correction_ppm(),
            self.dll_err_frames(),
            json_bool(self.dll_locked()),
            self.probe_result.as_str(),
            ratio,
            json_bool(self.probe_waiting_for_lock()),
            self.demotions,
            self.transitions,
            self.last_transition_reason,
        )
    }

    /// The published resampler correction ppm — the CORRECTION-mode L0 end-state
    /// observable (drives to ~0 when the host is truly slaved). Published only
    /// while a session is active (0 between sessions, matching the fill/slope
    /// publishing convention); always 0 in FILL mode, where nothing feeds it.
    fn published_correction_ppm(&self) -> f64 {
        if self.session_active {
            self.last_correction_ppm
        } else {
            0.0
        }
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
            obs_mode: ObsMode::Fill,
            log_prefix: "usbsink_audio",
        }
    }

    /// A CORRECTION-mode enabled config (fan-in combo): the L0 servo and the probe
    /// run on the resampler-correction observable, not the fill slope.
    fn correction_cfg() -> HostClockConfig {
        HostClockConfig {
            obs_mode: ObsMode::Correction,
            log_prefix: "fanin",
            ..enabled_cfg()
        }
    }

    /// Build an [`Obs`] with the lane already LOCKED (steady regime). Most tests
    /// want a lane that has left its warmup ramp, so the probe's AwaitLock wait
    /// clears after the settle window; `obs_unlocked` covers the warmup case.
    fn obs(playing: bool, host: bool, fill: f64, cap: u64, play: u64) -> Obs {
        obs_locked(playing, host, fill, cap, play, true)
    }

    /// An [`Obs`] whose lane is NOT locked (still in its warmup ramp). Used to
    /// prove the probe holds in AwaitLock until the lane settles.
    fn obs_unlocked(playing: bool, host: bool, fill: f64, cap: u64, play: u64) -> Obs {
        obs_locked(playing, host, fill, cap, play, false)
    }

    fn obs_locked(playing: bool, host: bool, fill: f64, cap: u64, play: u64, locked: bool) -> Obs {
        Obs {
            playing,
            host_connected: host,
            preempted: false,
            locked,
            fill_frames: fill,
            capture_frames: cap,
            playback_frames: play,
            // FILL-mode tests carry no resampler correction (usbsink solo has no
            // resampler); the CORRECTION-mode servo-sim tests build Obs directly
            // with a live correction_ppm.
            correction_ppm: 0.0,
        }
    }

    // ---- CORRECTION-mode servo-sim (fan-in combo) --------------------------
    //
    // The combo-mode redesign: with a lane resampler between the gadget ring and
    // the mix, the FILL slope is dead (the resampler flattens it). The honest
    // observable is the resampler's OWN correction ppm. These tests close the
    // CORRECTION-mode ladder against the REAL inner loop —
    // `jasper_resampler::RateController` constructed EXACTLY as
    // `jasper-fanin`'s `lane_resampler` builds it (±500 ppm authority, period
    // 256 @ 48 kHz, `max_resync` disabled) — not a hand-written first-order
    // tracker. That fidelity is the whole point of this rewrite (review PR
    // #1144): the earlier `SyntheticResampler` modelled the inner loop as a fast
    // one-pole tracker (α = 0.5/tick, no ±500 clamp, no adaptive-bandwidth
    // dynamics), so the FILL-tuned outer DLL looked convergent; against the real
    // spa_dll (locked-floor τ ≈ 10 s + adaptive-bw + clamp nonlinearities) that
    // same DLL law LIMIT-CYCLES. The fix is the pure-integral CORRECTION-mode
    // outer law ([`CORRECTION_INTEGRAL_GAIN`]); these tests pin that it converges
    // and does NOT oscillate against the real dynamics.
    //
    // The host honors commands the way hardware does: it reads the ladder's
    // `Action::WritePitch` output (so the 10 ppm epsilon + 1 Hz cadence + integer
    // ctl resolution all apply), applies it after a lag, and the fill integrates
    // the host-vs-consumer rate difference honestly (the inner loop's lag shows up
    // as real fill motion). `published_ppm()` mirrors the fan-in adapter's
    // milli-ppm rounding of the resampler ratio.

    /// The real inner lane: `jasper_resampler::RateController` built exactly as
    /// `lane_resampler` constructs it, plus the honest fill integration
    /// (`fill += period·(host − consume)`), so the CORRECTION observable it
    /// publishes carries the real composite-loop dynamics.
    struct RealLane {
        ctl: jasper_resampler::RateController,
        fill: f64,
        corr_ppm: f64,
    }

    /// The held target the fan-in lane disciplines toward in combo mode.
    const CORR_SIM_TARGET_FILL: f64 = 2048.0;
    /// Inner render period (frames) and rate — the lane_resampler geometry.
    const CORR_SIM_PERIOD: f64 = 256.0;
    const CORR_SIM_RATE: f64 = 48_000.0;

    impl RealLane {
        fn new() -> Self {
            // try_lock() seats fill at the held target and resets the controller;
            // `with_max_resync(_, _, _, Some(0.0))` is the exact lane_resampler
            // construction (max_resync disabled so a large valid fill excursion
            // slews through the ±500 clamp instead of hard-jumping).
            let ctl =
                jasper_resampler::RateController::with_max_resync(500.0, 256, 48_000, Some(0.0));
            Self {
                ctl,
                fill: CORR_SIM_TARGET_FILL,
                corr_ppm: 0.0,
            }
        }

        /// One inner render period: the host delivers at `(1 + host_ppm/1e6)`, the
        /// resampler consumes at `(1 + corr/1e6)`; the fill integrates the
        /// difference; the controller updates from `fill − target`.
        fn step(&mut self, host_ppm: f64, noise: f64) {
            self.fill += CORR_SIM_PERIOD * (host_ppm - self.corr_ppm) / 1.0e6 + noise;
            let _ratio = self.ctl.next_ratio(self.fill - CORR_SIM_TARGET_FILL);
            self.corr_ppm = self.ctl.ratio_ppm();
        }

        /// The fan-in adapter's milli-ppm-rounded publish (`ratio_milli_ppm`
        /// atomic decoded back to ppm), so the ladder sees the same quantized
        /// signal the daemon would.
        fn published_ppm(&self) -> f64 {
            ((self.corr_ppm * 1000.0).round() as i64) as f64 / 1000.0
        }
    }

    /// A host that honors the ladder's `Action::WritePitch` commands after a lag,
    /// at integer-ppm ctl resolution — or ignores them entirely (non-compliant).
    struct SimHost {
        crystal_ppm: f64,
        compliant: bool,
        /// (apply-at-ms, ppm) pending writes.
        pending: std::collections::VecDeque<(u64, f64)>,
        applied_ppm: f64,
        lag_ms: u64,
    }

    impl SimHost {
        fn new(crystal_ppm: f64, compliant: bool, lag_ms: u64) -> Self {
            Self {
                crystal_ppm,
                compliant,
                pending: std::collections::VecDeque::new(),
                applied_ppm: 0.0,
                lag_ms,
            }
        }
        fn write_pitch(&mut self, now_ms: u64, ppm: f64) {
            if self.compliant {
                self.pending.push_back((now_ms + self.lag_ms, ppm.round()));
            }
        }
        fn effective_ppm(&mut self, now_ms: u64) -> f64 {
            while let Some(&(at, ppm)) = self.pending.front() {
                if at <= now_ms {
                    self.applied_ppm = ppm;
                    self.pending.pop_front();
                } else {
                    break;
                }
            }
            self.crystal_ppm + self.applied_ppm
        }
    }

    /// Deterministic xorshift jitter (bounded ±amp frames), so the noise variant
    /// is reproducible.
    fn corr_sim_noise(state: &mut u64, amp: f64) -> f64 {
        if amp == 0.0 {
            return 0.0;
        }
        *state ^= *state << 13;
        *state ^= *state >> 7;
        *state ^= *state << 17;
        ((*state as f64 / u64::MAX as f64) - 0.5) * 2.0 * amp
    }

    /// The composite-loop trace of a CORRECTION-mode run against the REAL inner
    /// controller: for `secs` outer ticks, run one second of inner render periods,
    /// feed the published correction to the ladder, and route its pitch writes
    /// back into the host (honored with lag + ctl-integer resolution). Returns the
    /// HostClock plus the published-correction / commanded-ppm / fill traces.
    fn run_correction_real(
        crystal: f64,
        compliant: bool,
        lag_ms: u64,
        noise_amp: f64,
        secs: usize,
    ) -> (HostClock, Vec<f64>, Vec<f64>, Vec<f64>) {
        let mut hc = HostClock::new(correction_cfg());
        hc.startup_neutralize();
        let mut lane = RealLane::new();
        let mut host = SimHost::new(crystal, compliant, lag_ms);
        let inner_dt_ms = CORR_SIM_PERIOD / CORR_SIM_RATE * 1000.0; // ≈ 5.333 ms
        let inner_per_outer = (1000.0 / inner_dt_ms).round() as usize; // ≈ 188
        let mut rng: u64 = 0x9E37_79B9_7F4A_7C15;
        let mut now_ms = 0.0f64;
        let mut cap: u64 = 1_000_000_000;
        let mut corr_trace = Vec::with_capacity(secs);
        let mut cmd_trace = Vec::with_capacity(secs);
        let mut fill_trace = Vec::with_capacity(secs);
        for sec in 1..=secs as u64 {
            for _ in 0..inner_per_outer {
                now_ms += inner_dt_ms;
                let h = host.effective_ppm(now_ms as u64);
                lane.step(h, corr_sim_noise(&mut rng, noise_amp));
            }
            cap += 48_000;
            let obs = Obs {
                playing: true,
                host_connected: true,
                preempted: false,
                locked: true,
                fill_frames: lane.fill,
                capture_frames: cap,
                playback_frames: cap,
                correction_ppm: lane.published_ppm(),
            };
            let t_ms = sec * 1000;
            for Action::WritePitch { ppm, .. } in hc.tick(obs, t_ms) {
                host.write_pitch(t_ms, ppm);
            }
            corr_trace.push(lane.published_ppm());
            cmd_trace.push(hc.commanded_ppm());
            fill_trace.push(lane.fill);
        }
        (hc, corr_trace, cmd_trace, fill_trace)
    }

    /// Peak-to-peak of the last `n` samples (the oscillation magnitude the review
    /// used to characterise the limit cycle).
    fn tail_p2p(trace: &[f64], n: usize) -> f64 {
        let tail = &trace[trace.len().saturating_sub(n)..];
        let max = tail.iter().cloned().fold(f64::MIN, f64::max);
        let min = tail.iter().cloned().fold(f64::MAX, f64::min);
        max - min
    }

    fn tail_mean(trace: &[f64], n: usize) -> f64 {
        let tail = &trace[trace.len().saturating_sub(n)..];
        tail.iter().sum::<f64>() / tail.len() as f64
    }

    #[test]
    fn correction_mode_compliant_host_probes_pass_and_locks_l0() {
        // A compliant host at a +250 ppm crystal offset, closed against the REAL
        // inner controller. Under the +probe_ppm step it runs faster, so the
        // resampler's published correction ppm rises by ~probe_ppm ⇒
        // response_ratio in the pass band ⇒ L0.
        let (hc, ..) = run_correction_real(250.0, true, 200, 0.0, 40);
        assert_eq!(
            hc.probe_result(),
            ProbeResult::Pass,
            "a compliant host must pass the correction-mode probe against the real inner loop"
        );
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        let ratio = hc.response_ratio().unwrap();
        assert!(
            ratio >= 0.5,
            "correction-mode response_ratio must pass (>= 0.5), got {ratio}"
        );
    }

    #[test]
    fn correction_mode_noncompliant_host_probes_fail_and_falls_to_l2() {
        // A host that ignores the pitch command: the real inner loop holds its
        // correction near the crystal offset regardless of the step ⇒
        // response_ratio ≈ 0 ⇒ fail ⇒ L2, pitch neutral, demotion counted.
        let (hc, ..) = run_correction_real(250.0, false, 200, 0.0, 40);
        assert_eq!(
            hc.probe_result(),
            ProbeResult::Fail,
            "a non-compliant host must fail the correction-mode probe"
        );
        assert_eq!(hc.ladder(), Ladder::L2Fallback);
        assert_eq!(hc.demotions(), 1);
        assert_eq!(hc.commanded_ppm(), 0.0, "L2 commands neutral pitch");
        let ratio = hc.response_ratio().unwrap();
        assert!(
            ratio < 0.5,
            "non-compliant response_ratio must fail (< 0.5), got {ratio}"
        );
    }

    #[test]
    fn correction_mode_l0_servo_drives_correction_toward_zero() {
        // The headline L0 convergence test, now against the REAL inner controller
        // (this is the exact assertion the earlier synthetic model passed while
        // the real dynamics limit-cycled — review PR #1144). Lock a compliant host
        // at a range of realistic crystal offsets, run the closed loop, and assert
        // the pure-integral outer law drives the published correction ppm toward 0
        // AND does not oscillate (tail peak-to-peak stays tiny — a limit cycle
        // would show hundreds of ppm of periodic swing).
        for offset in [-250.0, 20.0, 250.0] {
            let (hc, corr, cmd, _fill) = run_correction_real(offset, true, 200, 0.0, 900);
            assert_eq!(
                hc.ladder(),
                Ladder::L0Locked,
                "must stay L0 through convergence at crystal {offset}"
            );
            // Tail correction driven to ~0 (host truly slaved).
            let tail_corr = tail_mean(&corr, 300).abs();
            assert!(
                tail_corr < offset.abs() * 0.25 + 5.0,
                "L0 servo must drive |correction| well below the initial offset \
                 {offset}; tail |correction| mean was {tail_corr}"
            );
            // No limit cycle: the published-correction and command tails are
            // essentially flat (the FILL-tuned DLL railed these ±hundreds of ppm).
            let corr_p2p = tail_p2p(&corr, 300);
            let cmd_p2p = tail_p2p(&cmd, 300);
            assert!(
                corr_p2p < 40.0,
                "correction must not limit-cycle at crystal {offset}; tail corr_p2p={corr_p2p} ppm"
            );
            assert!(
                cmd_p2p < 40.0,
                "command must not limit-cycle at crystal {offset}; tail cmd_p2p={cmd_p2p} ppm"
            );
            // The settled command ~cancels the crystal offset.
            let tail_cmd = tail_mean(&cmd, 300);
            assert!(
                (tail_cmd + offset).abs() < 60.0,
                "settled command {tail_cmd} should ~cancel the crystal offset {offset}"
            );
            // Command never leaves the ±MAX_BIAS_PPM clamp.
            assert!(cmd.iter().all(|c| c.abs() <= MAX_BIAS_PPM + 1e-6));
        }
    }

    #[test]
    fn correction_mode_l0_does_not_limit_cycle_under_lag_noise_and_long_soak() {
        // Robustness matrix: the pure-integral outer law must stay flat (no
        // fighting-cascade limit cycle) across host application lag, injected fill
        // jitter, near-authority crystals, and a long soak — the exact conditions
        // the review swept where the old DLL law railed correction ±460 ppm on a
        // ~21 s period. Each entry asserts the tail correction/command are not
        // oscillating; the noise variant is allowed a wider (but still bounded)
        // band because the injected jitter itself propagates into the observable.
        struct Case {
            crystal: f64,
            lag_ms: u64,
            noise: f64,
            secs: usize,
            corr_p2p_max: f64,
            cmd_p2p_max: f64,
        }
        let cases = [
            // Typical Mac crystal, instant and lagged hosts.
            Case {
                crystal: 20.0,
                lag_ms: 0,
                noise: 0.0,
                secs: 900,
                corr_p2p_max: 40.0,
                cmd_p2p_max: 40.0,
            },
            Case {
                crystal: 20.0,
                lag_ms: 200,
                noise: 0.0,
                secs: 900,
                corr_p2p_max: 40.0,
                cmd_p2p_max: 40.0,
            },
            // Larger offsets, both signs.
            Case {
                crystal: -250.0,
                lag_ms: 200,
                noise: 0.0,
                secs: 900,
                corr_p2p_max: 40.0,
                cmd_p2p_max: 40.0,
            },
            Case {
                crystal: 250.0,
                lag_ms: 1000,
                noise: 0.0,
                secs: 900,
                corr_p2p_max: 40.0,
                cmd_p2p_max: 40.0,
            },
            // Near the ±500 ppm inner authority.
            Case {
                crystal: 450.0,
                lag_ms: 200,
                noise: 0.0,
                secs: 900,
                corr_p2p_max: 40.0,
                cmd_p2p_max: 40.0,
            },
            // Injected fill jitter: the observable wanders but the COMMAND stays
            // quiet (no self-sustained oscillation the servo is driving).
            Case {
                crystal: 250.0,
                lag_ms: 200,
                noise: 0.5,
                secs: 900,
                corr_p2p_max: 250.0,
                cmd_p2p_max: 120.0,
            },
            // Long soak: the +20 ppm case that periodically railed at 3600 s under
            // the old law must stay flat.
            Case {
                crystal: 20.0,
                lag_ms: 200,
                noise: 0.0,
                secs: 3600,
                corr_p2p_max: 40.0,
                cmd_p2p_max: 40.0,
            },
        ];
        for c in cases {
            let (hc, corr, cmd, _fill) =
                run_correction_real(c.crystal, true, c.lag_ms, c.noise, c.secs);
            assert_eq!(
                hc.ladder(),
                Ladder::L0Locked,
                "must stay L0 (no spurious demotion) at crystal {} lag {}ms noise {}",
                c.crystal,
                c.lag_ms,
                c.noise
            );
            let corr_p2p = tail_p2p(&corr, 300);
            let cmd_p2p = tail_p2p(&cmd, 300);
            assert!(
                corr_p2p < c.corr_p2p_max,
                "correction limit-cycles at crystal {} lag {}ms noise {}: tail corr_p2p={corr_p2p} ppm (max {})",
                c.crystal, c.lag_ms, c.noise, c.corr_p2p_max
            );
            assert!(
                cmd_p2p < c.cmd_p2p_max,
                "command limit-cycles at crystal {} lag {}ms noise {}: tail cmd_p2p={cmd_p2p} ppm (max {})",
                c.crystal, c.lag_ms, c.noise, c.cmd_p2p_max
            );
        }
    }

    #[test]
    fn correction_mode_fragment_carries_obs_mode_and_correction() {
        // The status fragment for a CORRECTION-mode config carries obs_mode
        // "correction" and a correction_ppm field (additive), and parses as JSON.
        let hc = HostClock::new(correction_cfg());
        let frag = hc.status_fragment();
        assert!(frag.contains("\"obs_mode\":\"correction\""));
        assert!(frag.contains("\"correction_ppm\":"));
        let parsed: serde_json::Value = serde_json::from_str(&frag).unwrap();
        assert_eq!(parsed["obs_mode"].as_str(), Some("correction"));
        assert!(parsed["correction_ppm"].as_f64().is_some());
    }

    #[test]
    fn obs_mode_tokens_are_stable() {
        assert_eq!(ObsMode::Fill.as_str(), "fill");
        assert_eq!(ObsMode::Correction.as_str(), "correction");
    }

    // ---- Live setpoint (fan-in cushion-decay single source of truth) -------

    #[test]
    fn set_target_fill_frames_updates_the_locked_setpoint() {
        // The fan-in combo mode re-pins the setpoint each tick from the
        // resampler's live held target (which the cushion decay lowers). The
        // setter must move the value the locked loop disciplines toward, and
        // ignore a non-finite input (keeping the last good value).
        let mut hc = HostClock::new(enabled_cfg());
        assert_eq!(hc.target_fill_frames(), 384.0);
        hc.set_target_fill_frames(320.0);
        assert_eq!(hc.target_fill_frames(), 320.0);
        // Non-finite is ignored (no NaN poisoning the error term).
        hc.set_target_fill_frames(f64::NAN);
        assert_eq!(hc.target_fill_frames(), 320.0);
        hc.set_target_fill_frames(f64::INFINITY);
        assert_eq!(hc.target_fill_frames(), 320.0);
    }

    #[test]
    fn lowering_the_setpoint_shifts_the_locked_error_toward_slower_host() {
        // Drive to L0 at a steady fill, then lower the setpoint below the fill:
        // the error (fill − target) becomes positive, so the loop commands the
        // host SLOWER (negative bias) to drain the ring to the new lower target —
        // exactly the descent the cushion decay wants, with no re-acquisition.
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        drive_to_l0(&mut hc, 0.0);
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        // Hold the fill AT the old target so the loop is settled.
        let mut t = 100_000u64;
        let mut cap = hc_cap_start();
        for _ in 0..30 {
            t += 1000;
            hc.tick(obs(true, true, 384.0, cap, cap), t);
            cap += (OUTER_DLL_RATE * 1000.0 / 1000.0) as u64;
        }
        // Now lower the setpoint; keep feeding the SAME fill (384). The error is
        // now +64 (fill above the new 320 target), so the command trends negative
        // (slow the host) to drain toward the lower setpoint.
        hc.set_target_fill_frames(320.0);
        for _ in 0..30 {
            t += 1000;
            hc.tick(obs(true, true, 384.0, cap, cap), t);
            cap += (OUTER_DLL_RATE * 1000.0 / 1000.0) as u64;
        }
        assert!(
            hc.commanded_ppm() < 0.0,
            "a fill above the lowered setpoint must command the host slower, got {} ppm",
            hc.commanded_ppm()
        );
        // And it stays LOCKED — a setpoint step is a bounded error the loop
        // handles, not a re-acquisition.
        assert_eq!(hc.ladder(), Ladder::L0Locked);
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
        // The lane is LOCKED from the first tick, so the probe clears its
        // AwaitLock settle then runs baseline → step. A COMPLIANT host runs at a
        // +200 ppm crystal offset PLUS the commanded pitch: at baseline the
        // command is neutral (host +200), during the +300 step it follows
        // (host +500). Keying on `hc.commanded_ppm()` (not a hardcoded tick
        // schedule) makes the response track the ACTUAL step timing regardless of
        // the settle delay.
        let offset = 200.0;
        for t in 1u64..=(PROBE_SETTLE_SECS + PROBE_BASELINE_SECS + 6 + 3) {
            let host_ppm = offset + hc.commanded_ppm();
            cap += (48000.0 * (1.0 + host_ppm / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
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

    /// Preempt while ACTIVELY MEASURING (past AwaitLock, into the baseline)
    /// aborts the probe: last_result="aborted", pitch neutral, back to armed.
    #[test]
    fn preempt_mid_measurement_aborts() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 48000;
        let mut play: u64 = 48000;
        let mut t = 0u64;
        // Lock + settle + one baseline tick so we are genuinely measuring.
        for _ in 0..(PROBE_SETTLE_SECS + 1) {
            t += 1;
            cap += 48000;
            play += 48000;
            hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
        }
        assert_eq!(hc.ladder(), Ladder::Probing);
        assert!(!hc.probe_waiting_for_lock(), "baseline is in flight");
        // Preempt mid-baseline: session ends → probe aborted (a measurement WAS
        // in flight).
        t += 1;
        let mut ob = obs(true, true, 400.0, cap + 48000, play + 48000);
        ob.preempted = true;
        let actions = hc.tick(ob, t * 1000);
        assert_eq!(hc.probe_result(), ProbeResult::Aborted);
        assert!(
            matches!(actions.last(), Some(Action::WritePitch { ppm, reset: true }) if *ppm == 0.0),
            "abort forces neutral pitch"
        );
    }

    /// Ending the session while the probe never left AwaitLock is NOT an abort —
    /// no baseline was ever taken, so there is nothing to abort. `probe_result`
    /// stays as it was (None on a first session), and the neutral pitch write
    /// still fires. This pins the review's Nit 3: the journal must not read
    /// "result=aborted" for a probe that never started measuring.
    #[test]
    fn end_session_in_await_lock_does_not_mark_aborted() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        // One tick of an unlocked (warmup) session: enters Probing/AwaitLock and
        // stays there (never locks ⇒ never baselines).
        hc.tick(obs_unlocked(true, true, 400.0, 48000, 48000), 1000);
        assert!(hc.probe_waiting_for_lock(), "still in AwaitLock");
        assert_eq!(hc.probe_result(), ProbeResult::None);
        // Preempt while still in AwaitLock: session ends, but no abort verdict.
        let mut ob = obs_unlocked(true, true, 400.0, 96000, 96000);
        ob.preempted = true;
        let actions = hc.tick(ob, 2000);
        assert_eq!(
            hc.probe_result(),
            ProbeResult::None,
            "await-lock end is not an abort (no measurement was in flight)"
        );
        assert!(
            matches!(actions.last(), Some(Action::WritePitch { ppm, reset: true }) if *ppm == 0.0),
            "session end still forces neutral pitch"
        );
    }

    // ---- Probe lock-gate (session-start warmup-ramp fix) -------------------

    /// The core defect fix: while the lane is UNLOCKED (its warmup fill ramp),
    /// the probe must NOT begin its baseline — it holds in AwaitLock, commanding
    /// neutral, so the baseline never measures the ramp as clock drift. The
    /// hardware evidence (jts.local 2026-07-03) was a baseline_slope of +1460 ppm
    /// that was purely the resampler's 0→held-target fill ramp.
    #[test]
    fn probe_does_not_baseline_while_unlocked() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 0;
        let mut play: u64 = 0;
        // 20 s of playing-but-UNLOCKED with a huge divergence slope (the warmup
        // ramp): capture races ahead of playback. If the probe baselined here it
        // would measure this ramp and (once stepped) fail. It must not: the
        // ladder stays Probing/AwaitLock, pitch neutral, and never reaches a
        // verdict.
        for t in 1u64..=20 {
            cap += (48000.0 * (1.0 + 1460.0 / 1.0e6)) as u64; // the ramp slope
            play += 48000;
            let actions = hc.tick(obs_unlocked(true, true, 400.0, cap, play), t * 1000);
            assert_eq!(hc.ladder(), Ladder::Probing, "unlocked ⇒ still probing");
            assert!(
                hc.probe_waiting_for_lock(),
                "unlocked ⇒ probe is waiting for lock"
            );
            // Any write while waiting is neutral (never the +probe_ppm step).
            if let Some(Action::WritePitch { ppm, .. }) = actions.last() {
                assert_eq!(*ppm, 0.0, "await-lock commands only neutral pitch");
            }
        }
        assert_eq!(
            hc.probe_result(),
            ProbeResult::None,
            "no verdict is reached while the lane never locks"
        );
    }

    /// Once the lane locks, the probe waits PROBE_SETTLE_SECS of CONTINUOUS lock
    /// before the baseline starts (measured on the tick clock), then runs a clean
    /// baseline+step over locked, on-rate data → passes and locks L0.
    #[test]
    fn probe_starts_after_lock_plus_settle_and_passes() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 0;
        let mut play: u64 = 0;
        let mut t = 0u64;
        // Phase A: 5 s unlocked warmup with a steep ramp slope. Probe waits.
        for _ in 0..5 {
            t += 1;
            cap += (48000.0 * (1.0 + 1460.0 / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs_unlocked(true, true, 400.0, cap, play), t * 1000);
        }
        assert!(hc.probe_waiting_for_lock(), "still waiting during warmup");
        assert_eq!(hc.probe_result(), ProbeResult::None);
        // Phase B: lane locks; a compliant host at a modest +100 ppm offset that
        // follows the commanded step. The settle + baseline + step now run over
        // clean locked data.
        let offset = 100.0;
        for _ in 0..(PROBE_SETTLE_SECS + PROBE_BASELINE_SECS + 6 + 3) {
            t += 1;
            let host_ppm = offset + hc.commanded_ppm();
            cap += (48000.0 * (1.0 + host_ppm / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
        }
        assert_eq!(
            hc.probe_result(),
            ProbeResult::Pass,
            "clean locked baseline+step passes"
        );
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        assert!(
            !hc.probe_waiting_for_lock(),
            "locked ⇒ no longer waiting for lock"
        );
    }

    /// The settle timer is on the TICK clock, not wall time: it requires the
    /// accumulated `now_ms` since lock to reach PROBE_SETTLE_SECS. A single lock
    /// tick does not immediately baseline; a lock that is younger than the settle
    /// window keeps waiting, and only crossing the window (in tick time) starts
    /// the baseline. We drive fake time in exact 1 s steps and assert the
    /// baseline has NOT started one tick before the settle boundary and HAS by
    /// two ticks after.
    #[test]
    fn settle_timer_uses_tick_clock_not_wall_time() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 1_000_000_000;
        let mut play: u64 = cap;
        // Rising edge at t=10 s → AwaitLock, lock_since=10 s.
        let mut t = 10u64;
        cap += 48000;
        play += 48000;
        hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
        assert!(hc.probe_waiting_for_lock(), "just entered await-lock");
        // One tick later (t=11 s): only 1 s of lock < 2 s settle ⇒ still waiting.
        t += 1;
        cap += 48000;
        play += 48000;
        hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
        assert!(
            hc.probe_waiting_for_lock(),
            "1 s of lock is below the {PROBE_SETTLE_SECS}s settle ⇒ still waiting"
        );
        // At t=12 s: 2 s of lock == settle ⇒ baseline begins (no longer waiting).
        t += 1;
        cap += 48000;
        play += 48000;
        hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
        assert!(
            !hc.probe_waiting_for_lock(),
            "at the settle boundary the baseline starts"
        );
        assert_eq!(hc.ladder(), Ladder::Probing, "baselining is still Probing");
    }

    /// A CORRECTION-mode [`Obs`] with the lane LOCKED and a specified live
    /// correction ppm — the CORRECTION-mode probe observable. `cap`/`play` are
    /// irrelevant to the CORRECTION observable (the probe uses the correction
    /// mean, not the fill slope) but supplied so the Obs is well-formed.
    fn obs_corr(correction_ppm: f64, cap: u64, play: u64) -> Obs {
        Obs {
            playing: true,
            host_connected: true,
            preempted: false,
            locked: true,
            fill_frames: 400.0,
            capture_frames: cap,
            playback_frames: play,
            correction_ppm,
        }
    }

    /// LOCK-ONLY settle in CORRECTION mode (jts.local 2026-07-05): a railed
    /// correction that is LOCKED does NOT hold the probe in AwaitLock — the settle
    /// window accrues AT the rail and the probe baselines against it. This is the
    /// inversion of the removed 2026-07-03 rail guard (`settle_regime_ok` reverted
    /// to `obs.locked`), which deadlocked beyond-authority hosts: their correction
    /// rails at +500 STEADY-STATE under the neutral pitch AwaitLock commands and
    /// only comes off the rail with the servo's pitch authority — which the guard
    /// withheld. A railed baseline is fine because the probe steps AWAY from the
    /// rail (visible response) and clipping is fail-biased. Feed a steady railed
    /// correction from lock and assert the probe LEAVES AwaitLock after the settle
    /// window and reaches a verdict, rather than hanging forever.
    #[test]
    fn correction_probe_settle_accrues_at_the_rail() {
        let mut hc = HostClock::new(correction_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 1_000_000_000;
        let mut play: u64 = cap;
        let mut t = 0u64;
        // Locked AND railed at −500 from the first tick. Under the removed guard
        // this hung in AwaitLock forever (the false-fix that deadlocked the
        // beyond-authority host). Lock-only: the settle timer runs at the rail.
        // Give it the full settle + baseline + step window. The correction stays
        // pinned at −500 regardless of the step (a NON-responsive lane), so the
        // step reads ≈ baseline ⇒ response_ratio ≈ 0 ⇒ a FAIL verdict — the point
        // is that a VERDICT is reached (AwaitLock released, probe ran), not that a
        // pinned rail passes. (A COMPLIANT railed host — one whose correction moves
        // off the rail under the away-from-rail step — is pinned separately in
        // `beyond_authority_railed_host_probes_pass_then_fail`.)
        let window = PROBE_SETTLE_SECS + PROBE_BASELINE_SECS + CORRECTION_PROBE_STEP_SECS + 3;
        // After the settle window (2 s), the probe must already have left AwaitLock.
        for step in 0..window {
            t += 1;
            cap += 48000;
            play += 48000;
            hc.tick(obs_corr(-500.0, cap, play), t * 1000);
            if step >= PROBE_SETTLE_SECS {
                assert!(
                    !hc.probe_waiting_for_lock(),
                    "lock-only settle: a railed-but-locked correction leaves AwaitLock \
                     after the settle window (it does NOT deadlock)"
                );
            }
        }
        assert_eq!(
            hc.probe_result(),
            ProbeResult::Fail,
            "a non-responsive pinned rail baselines, steps, and FAILS (fail-biased) — \
             a verdict IS reached, no deadlock"
        );
        assert_eq!(hc.ladder(), Ladder::L2Fallback);
        assert_eq!(hc.demotions(), 1);
    }

    /// The beyond-authority DEADLOCK regression (jts.local 2026-07-05) and its
    /// fail-bias corollary, in one composition test against the real
    /// `HostClock::tick` / `SlopeEstimator` probe pipeline (the CORRECTION-mode
    /// observable is `SlopeEstimator::correction_mean_ppm`; the `Dll` is `reset`
    /// but not ticked during probing, so it does not discriminate here). Model a
    /// host whose excess rate is +600 ppm — BEYOND the
    /// lane resampler's ±500 ppm inner authority — so the observed correction
    /// CLIPS at +500 under neutral pitch (the AwaitLock command). The observed
    /// correction is `excess + applied_pitch` clamped to ±500: at +600 it rails at
    /// +500 (baseline); a COMPLIANT host commanded −300 (the away-from-rail step,
    /// since baseline 500 > the flip deadband) shows +300 — unrailed, a visible
    /// response — while a NON-compliant host ignores the pitch and stays pinned at
    /// +500.
    ///
    /// Under the removed rail guard BOTH variants deadlocked: the correction rails
    /// at +500 from lock, the guard refused to accrue the settle window, so the
    /// probe never left AwaitLock, never commanded the pitch that would unrail the
    /// compliant host, and never reached a verdict — exactly the two 6-min-stuck
    /// sessions observed on hardware (correction pinned 500.0, pitch 0.0). Lock-
    /// only settle: the settle accrues at the rail, the probe runs, and the two
    /// hosts are correctly discriminated — PASS for compliant (proving a railed
    /// baseline is measurable via the away-from-rail step), FAIL for non-compliant
    /// (proving that for a STEADY-STATE rail, removing the guard cannot manufacture
    /// a false PASS: a stationary clipped baseline understates demand, so a
    /// non-responsive host reads ratio ≈ 0). A DECAYING transient rail is out of
    /// scope for this steady-state composition — it can inflate the ratio and the
    /// 450 guard never closed it either; that class is covered by root-fixing
    /// transient causes (#1161) plus the two-strike/churn revocation nets.
    #[test]
    fn beyond_authority_railed_host_probes_pass_then_fail() {
        // A beyond-authority lane: observed correction = (excess + applied pitch)
        // clamped to the ±500 inner authority. Compliant follows the commanded
        // pitch; non-compliant ignores it. Runs long enough for the full settle +
        // baseline + step window plus slack.
        fn run(excess_ppm: f64, compliant: bool) -> HostClock {
            let mut hc = HostClock::new(correction_cfg());
            hc.startup_neutralize();
            let mut cap: u64 = 1_000_000_000;
            let mut play: u64 = cap;
            let total = PROBE_SETTLE_SECS + PROBE_BASELINE_SECS + CORRECTION_PROBE_STEP_SECS + 4;
            for t in 1u64..=total {
                let applied = if compliant { hc.commanded_ppm() } else { 0.0 };
                let corr = (excess_ppm + applied).clamp(-500.0, 500.0);
                cap += 48_000;
                play += 48_000;
                hc.tick(obs_corr(corr, cap, play), t * 1000);
            }
            hc
        }

        // Compliant beyond-authority host: rails at +500 baseline, unrails to +300
        // under the −300 away-from-rail step ⇒ response_ratio in the pass band ⇒
        // PASS and L0. (Hardware analogue: baseline 500 → step 258, ratio 0.807.)
        let hc_ok = run(600.0, true);
        assert_eq!(
            hc_ok.probe_result(),
            ProbeResult::Pass,
            "a COMPLIANT beyond-authority host must PASS from a railed baseline — \
             the away-from-rail step makes the clipped baseline measurable"
        );
        assert_eq!(hc_ok.ladder(), Ladder::L0Locked);
        assert!(
            !hc_ok.probe_waiting_for_lock(),
            "the railed settle accrued and the probe ran (no deadlock)"
        );
        let ratio_ok = hc_ok.response_ratio().unwrap();
        assert!(
            ratio_ok >= 0.5,
            "compliant railed response_ratio must pass (>= 0.5), got {ratio_ok}"
        );

        // Non-compliant beyond-authority host: pinned at +500 through the step ⇒
        // response_ratio ≈ 0 ⇒ FAIL. Pins the fail-bias claim (removing the guard
        // cannot make a truly non-compliant host pass).
        let hc_bad = run(600.0, false);
        assert_eq!(
            hc_bad.probe_result(),
            ProbeResult::Fail,
            "a NON-compliant beyond-authority host must still FAIL — a clipped \
             baseline understates demand and is fail-biased, never pass-biased"
        );
        assert_eq!(hc_bad.ladder(), Ladder::L2Fallback);
        assert_eq!(hc_bad.commanded_ppm(), 0.0, "L2 commands neutral pitch");
        let ratio_bad = hc_bad.response_ratio().unwrap();
        assert!(
            ratio_bad < 0.5,
            "non-compliant railed response_ratio must fail (< 0.5), got {ratio_bad}"
        );
    }

    /// Defect F (jts.local 2026-07-05, 22:07 EDT): a ~0-1 s gap between a stream
    /// stop and a new stream start left the CORRECTION-mode ladder "in AwaitLock for
    /// the entire next session with dead observation (correction_ppm 0.0, dll
    /// locked=false) while the lane itself was locked and railing; no session_start
    /// transition fired". PROVES this is the beyond-authority rail deadlock that
    /// #1167's rail-guard removal fixed — NOT a residual session-edge re-arm bug.
    ///
    /// The F symptom is a COMPOSITION of two facts, both closed by #1167:
    ///   1. The ladder was stuck in AwaitLock because the correction railed at +500
    ///      (beyond the ±500 inner authority) under the neutral AwaitLock pitch. The
    ///      OLD rail guard refused to accrue settle at the rail, so the probe hung.
    ///      Post-#1167 `settle_regime_ok == obs.locked`, so a railing-but-locked lane
    ///      LEAVES AwaitLock and reaches a verdict.
    ///   2. "No session_start fired" is the CONSEQUENCE, not a separate bug: across a
    ///      sub-second stop→start the resampler never lost lock, so `playing` (=
    ///      resampler locked) never went false at a tick → no session falling edge →
    ///      no fresh `begin_probe`. That is CORRECT: an uninterrupted session must not
    ///      re-probe. The only reason it was pathological in the incident is that the
    ///      session it stayed in was the DEADLOCKED AwaitLock one. With the deadlock
    ///      gone, an uninterrupted railing session progresses to a verdict on its own.
    ///
    /// This test drives the real `HostClock::tick` pipeline through: a railing
    /// beyond-authority session that reaches a verdict; then the sub-second gap as a
    /// single stop tick with `playing=false` (lock briefly lost across the micro-gap
    /// — the falling-edge variant) immediately followed by a start that re-arms the
    /// probe; and asserts the ladder is NEVER stuck in AwaitLock with a live session
    /// — the observation feed stays alive (the probe/servo keep running). The
    /// harder-to-see no-falling-edge variant of fact 2 (lock never lost, so no
    /// session edge at all) is covered by session 1's own uninterrupted run to a
    /// verdict. No code changed for F; this pins that #1167 already closed it.
    #[test]
    fn defect_f_fast_stop_start_does_not_deadlock_await_lock() {
        // A compliant beyond-authority host: correction = (excess + applied pitch)
        // clamped to ±500. It rails at +500 baseline and unrails under the servo.
        let excess = 600.0;
        let mut hc = HostClock::new(correction_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 1_000_000_000;
        let mut play: u64 = cap;
        let mut t = 0u64;
        let tick_corr = |hc: &mut HostClock, t: &mut u64, cap: &mut u64, play: &mut u64| {
            *t += 1;
            let applied = hc.commanded_ppm();
            let corr = (excess + applied).clamp(-500.0, 500.0);
            *cap += 48_000;
            *play += 48_000;
            hc.tick(obs_corr(corr, *cap, *play), *t * 1000)
        };

        // Session 1: run the full settle + baseline + step window. Under the OLD
        // guard this hung in AwaitLock (the F deadlock). Post-#1167 it must leave
        // AwaitLock within the settle window and reach a verdict.
        let window = PROBE_SETTLE_SECS + PROBE_BASELINE_SECS + CORRECTION_PROBE_STEP_SECS + 4;
        for step in 0..window {
            tick_corr(&mut hc, &mut t, &mut cap, &mut play);
            if step >= PROBE_SETTLE_SECS {
                assert!(
                    !hc.probe_waiting_for_lock(),
                    "F: a railing-but-locked session must LEAVE AwaitLock after the \
                     settle window — the #1167 fix; the old guard deadlocked here"
                );
            }
        }
        assert_ne!(
            hc.ladder(),
            Ladder::Probing,
            "F: session 1 reached a verdict (L0/L1/L2), not stuck Probing/AwaitLock"
        );

        // The ~0-1 s gap: ONE stop tick where lock is briefly reported lost
        // (playing=false), immediately followed by a start. This is the tightest
        // reproduction of "0-1 s gap between stream stop and new stream start".
        let mut stop = obs_corr(500.0, cap + 48_000, play + 48_000);
        stop.playing = false;
        t += 1;
        cap += 48_000;
        play += 48_000;
        hc.tick(stop, t * 1000);
        // The idle boundary parks the ladder armed (Probing/AwaitLock) — that is the
        // ARMED-for-next-session state, but the session is no longer active.
        // `probe_waiting_for_lock` is session_active-gated, so it reads false while
        // idle (the armed-but-not-in-a-live-session state).
        assert!(
            !hc.probe_waiting_for_lock(),
            "F: the stop tick ended the active session (armed for the next, idle)"
        );

        // Session 2 starts immediately (the new stream). The rising edge fires a
        // fresh begin_probe (session_start), re-entering the AwaitLock wait — the
        // observable re-arm signal (transitions() is NOT it: end_session already
        // moved the ladder Probing, so begin_probe's Probing→Probing is a same-state
        // token update, per every_session_reprobes). The F report's 'no
        // session_start' only happened because the prior AwaitLock was DEADLOCKED;
        // with the deadlock gone the edge re-arms and the probe wait is LIVE again.
        tick_corr(&mut hc, &mut t, &mut cap, &mut play);
        assert!(
            hc.probe_waiting_for_lock(),
            "F: session 2's rising edge re-armed the probe (session_active + AwaitLock \
             live) — a fresh session_start, not a dead-in-await_lock carryover"
        );
        // The rest of session 2 must progress the SAME way — leave AwaitLock, reach a
        // verdict — proving the observation feed is alive across the fast gap.
        for step in 1..window {
            tick_corr(&mut hc, &mut t, &mut cap, &mut play);
            if step >= PROBE_SETTLE_SECS {
                assert!(
                    !hc.probe_waiting_for_lock(),
                    "F: session 2 also leaves AwaitLock — the observation feed is \
                     alive, NOT dead-in-await_lock as the incident showed"
                );
            }
        }
        assert_ne!(
            hc.ladder(),
            Ladder::Probing,
            "F: session 2 reached a verdict — no perpetual AwaitLock deadlock"
        );
    }

    /// FILL mode (usbsink solo) settles on LOCK regardless of the `correction_ppm`
    /// field, which it never consults (there is no resampler; real usbsink Obs
    /// carries correction 0). Lock-only settle applies in both modes now; this
    /// feeds a bogus railed −500 to prove FILL never reads it. (Formerly named
    /// `fill_mode_settle_ignores_correction_rail_guard`, back when a
    /// CORRECTION-only rail guard existed; the guard is gone, but FILL-mode
    /// correction-obliviousness is still worth pinning.)
    #[test]
    fn fill_mode_settle_ignores_correction_field() {
        let mut hc = HostClock::new(enabled_cfg()); // ObsMode::Fill
        hc.startup_neutralize();
        let mut cap: u64 = 1_000_000_000;
        let mut play: u64 = cap;
        let mut t = 10u64;
        // Locked, on-rate FILL data, but carry a bogus railed correction_ppm. FILL
        // mode never consults it, so the settle proceeds after the normal 2 s.
        for _ in 0..(PROBE_SETTLE_SECS + PROBE_BASELINE_SECS + 6 + 3) {
            t += 1;
            let host_ppm = 100.0 + hc.commanded_ppm();
            cap += (48000.0 * (1.0 + host_ppm / 1.0e6)) as u64;
            play += 48000;
            let mut ob = obs(true, true, 400.0, cap, play);
            ob.correction_ppm = -500.0; // never consulted in FILL mode
            hc.tick(ob, t * 1000);
        }
        assert_eq!(
            hc.probe_result(),
            ProbeResult::Pass,
            "FILL mode settles on lock and passes, ignoring the correction field"
        );
        assert_eq!(hc.ladder(), Ladder::L0Locked);
    }

    /// Lock loss DURING the baseline restarts the wait: the in-flight measurement
    /// is contaminated (the lane re-entered its warmup regime), so the ladder
    /// drops back to AwaitLock, commands neutral, and requires a fresh
    /// lock+settle before baselining again. It does NOT demote to L2 (a warmup
    /// re-entry is not a compliance failure).
    #[test]
    fn lock_loss_during_baseline_restarts_the_wait() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 1_000_000_000;
        let mut play: u64 = cap;
        let mut t = 0u64;
        // Get into the baseline: lock + settle, then one baseline tick.
        for _ in 0..(PROBE_SETTLE_SECS + 1) {
            t += 1;
            cap += 48000;
            play += 48000;
            hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
        }
        assert!(!hc.probe_waiting_for_lock(), "baseline has started");
        assert_eq!(hc.ladder(), Ladder::Probing);
        // Now lose lock mid-baseline → restart the wait.
        t += 1;
        cap += 48000;
        play += 48000;
        let actions = hc.tick(obs_unlocked(true, true, 400.0, cap, play), t * 1000);
        assert!(
            hc.probe_waiting_for_lock(),
            "lock loss mid-baseline returns to await-lock"
        );
        assert_eq!(
            hc.ladder(),
            Ladder::Probing,
            "no demotion to L2 on warmup re-entry"
        );
        assert_ne!(hc.ladder(), Ladder::L2Fallback);
        assert!(
            matches!(actions.last(), Some(Action::WritePitch { ppm, reset: true }) if *ppm == 0.0),
            "restart forces neutral pitch"
        );
        // The restarted wait requires a fresh full settle: one lock tick is not
        // enough to re-baseline.
        t += 1;
        cap += 48000;
        play += 48000;
        hc.tick(obs(true, true, 400.0, cap, play), t * 1000);
        assert!(
            hc.probe_waiting_for_lock(),
            "one lock tick after a restart is below the settle window"
        );
    }

    /// usbsink solo maps `Obs::locked` settle-only (`= playing`), NOT gated on a
    /// live ring-fill level. This pins the invariant the ladder relies on: with a
    /// slow host whose gadget ring never reaches the fill target (rides the
    /// underflow floor under neutral pitch), the probe must still leave AwaitLock
    /// after the settle and reach a verdict — a fill-level gate here would
    /// deadlock it forever (review finding 1). We drive `fill_frames` pinned BELOW
    /// target the whole time and confirm the probe baselines and passes anyway.
    #[test]
    fn usbsink_style_settle_only_lock_probes_despite_low_ring() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        let mut cap: u64 = 1_000_000_000;
        let mut play: u64 = cap;
        let mut t = 0u64;
        // usbsink mapping: locked = playing (fill is irrelevant to the gate). We
        // model a slow-host ring stuck at 1 period (256 frames), well below the
        // 384-frame target, for the ENTIRE session.
        let ob = |cap: u64, play: u64| -> Obs {
            let mut o = obs(true, true, 256.0, cap, play);
            o.locked = true; // == playing; the low fill does NOT unset it
            o
        };
        // A modest, compliant host at +100 ppm that follows the commanded step.
        let offset = 100.0;
        for _ in 0..(PROBE_SETTLE_SECS + PROBE_BASELINE_SECS + 6 + 3) {
            t += 1;
            let host_ppm = offset + hc.commanded_ppm();
            cap += (48000.0 * (1.0 + host_ppm / 1.0e6)) as u64;
            play += 48000;
            hc.tick(ob(cap, play), t * 1000);
        }
        assert_eq!(
            hc.probe_result(),
            ProbeResult::Pass,
            "settle-only lock probes and passes even with the ring below target"
        );
        assert_eq!(hc.ladder(), Ladder::L0Locked);
        assert!(
            !hc.probe_waiting_for_lock(),
            "settle-only ⇒ not stuck in AwaitLock on a low ring"
        );
    }

    /// The disabled-config fragment now carries `probe.waiting_for_lock:false`
    /// (the new observable). Pins the additive shape at the accessor level in
    /// addition to the byte-exact `host_clock_fragment_shape_is_stable`.
    #[test]
    fn waiting_for_lock_is_false_when_not_probing() {
        let hc = HostClock::new(enabled_cfg());
        assert!(
            !hc.probe_waiting_for_lock(),
            "Disabled ladder is not waiting for lock"
        );
        assert!(hc.status_fragment().contains("\"waiting_for_lock\":false"));
    }

    /// `waiting_for_lock` is gated on a LIVE session (review Nit 4): `end_session`
    /// parks the ladder in Probing/AwaitLock as its armed-for-next-session resting
    /// state, but with no session flowing the flag must read `false` — otherwise
    /// an enabled-but-idle box publishes `waiting_for_lock:true` forever, reading
    /// as an active-session claim. It flips to `true` only once a session's rising
    /// edge re-enters the wait.
    #[test]
    fn waiting_for_lock_is_false_while_idle_between_sessions() {
        let mut hc = HostClock::new(enabled_cfg());
        hc.startup_neutralize();
        // Bring a session up (locked lane) then stop it → end_session parks the
        // ladder in Probing/AwaitLock, but session_active is now false.
        hc.tick(obs(true, true, 400.0, 48000, 48000), 1000);
        assert!(
            hc.probe_waiting_for_lock(),
            "live session ⇒ waiting is true"
        );
        // Session stops (not playing): idle boundary.
        hc.tick(obs(false, true, 400.0, 96000, 96000), 2000);
        assert_eq!(hc.ladder(), Ladder::Probing, "parked in Probing while idle");
        assert!(
            !hc.probe_waiting_for_lock(),
            "idle box is NOT waiting for lock (no live session)"
        );
        assert!(
            hc.status_fragment().contains("\"waiting_for_lock\":false"),
            "idle fragment carries waiting_for_lock:false"
        );
        // A fresh session re-enters the wait ⇒ true again.
        hc.tick(obs(true, true, 400.0, 144000, 144000), 3000);
        assert!(
            hc.probe_waiting_for_lock(),
            "next session's rising edge re-enters the wait"
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
    /// string verbatim. Its Python twin
    /// (`tests/test_usbsink_host_clock_contract.py`; a
    /// `tests/test_fanin_host_clock_contract.py` twin arrives with combo mode)
    /// greps this identical literal
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
            r#"{"enabled":false,"ladder":"disabled","obs_mode":"fill","pitch_ppm_commanded":0.0,"fill_frames":0,"fill_slope_ppm":0.00,"fill_variance":0.00,"correction_ppm":0.00,"dll":{"err_frames":0.00,"locked":false},"probe":{"last_result":"none","response_ratio":null,"waiting_for_lock":false},"demotions":0,"transitions":0,"last_transition_reason":"startup"}"#
        );
        // And it parses as valid JSON.
        let parsed: serde_json::Value = serde_json::from_str(&fragment).unwrap();
        assert_eq!(parsed["enabled"].as_bool(), Some(false));
        assert_eq!(parsed["ladder"].as_str(), Some("disabled"));
        assert_eq!(parsed["obs_mode"].as_str(), Some("fill"));
        assert_eq!(parsed["correction_ppm"].as_f64(), Some(0.0));
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
        let disabled_fanin = HostClockConfig::disabled("fanin");
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
    ///
    /// The lane is LOCKED from the first tick (`obs` sets `locked=true`), so the
    /// probe clears its AwaitLock settle after PROBE_SETTLE_SECS, then runs the
    /// 4 s baseline + 6 s step. A COMPLIANT host follows the commanded pitch: its
    /// effective rate is `offset + commanded`, so during the +probe_ppm step the
    /// divergence slope moves ~probe_ppm ⇒ a passing response_ratio. Keying the
    /// host rate on `hc.commanded_ppm()` (not a hardcoded tick schedule) makes
    /// this robust to the settle-delay shift in phase timing.
    fn drive_to_l0(hc: &mut HostClock, offset_ppm: f64) {
        let mut cap: u64 = 0;
        let mut play: u64 = 0;
        // settle (2) + baseline (4) + step (6) + slack. The rising edge is tick 1.
        for t in 1u64..=(PROBE_SETTLE_SECS + PROBE_BASELINE_SECS + 6 + 3) {
            let host_ppm = offset_ppm + hc.commanded_ppm();
            cap += (48000.0 * (1.0 + host_ppm / 1.0e6)) as u64;
            play += 48000;
            hc.tick(obs(true, true, 384.0, cap, play), t * 1000);
        }
    }
}
