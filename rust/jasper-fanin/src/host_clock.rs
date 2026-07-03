// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Combo-mode host-slaved USB clock adapter for fan-in (C4/C5).
//!
//! In combo mode (`JASPER_FANIN_USB_DIRECT=enabled`) fan-in owns the
//! `hw:UAC2Gadget` capture, so — per the invariant *the daemon that owns the
//! gadget capture owns the pitch ctl* — fan-in also drives the host-clock
//! ladder that steers the gadget's `Capture Pitch 1000000` ctl. The ladder /
//! probe / servo / write-gate itself is the shared [`jasper_host_clock`] crate
//! (byte-identical to what the usbsink bridge runs in solo mode); this module
//! is the thin fan-in-side adapter:
//!
//! 1. [`HostClockSignals`] — the Arc atomics the mixer already publishes for the
//!    USB DIRECT lane (resampler fill gauge / input / output / lock, direct
//!    `present`), cloned once in `main` before the mixer starts. This is the ONLY
//!    coupling to the mixer; the ladder never touches a `LaneResampler` or a
//!    `PCM`.
//! 2. [`build_obs`] — maps those atomics onto the shared [`Obs`], including the
//!    resampler-derived setpoint (see below).
//! 3. [`HostClockActuator`] — the fan-in pitch-ctl actuator: fail-soft open,
//!    rate-limited write-error logs, and (unlike usbsink) a session-edge REOPEN
//!    so a boot-order race that opened the ctl before the gadget bound does not
//!    leave every write a no-op forever.
//! 4. [`run_host_clock_thread`] — the dedicated `fanin-host-clock` thread: a
//!    100 ms sleep loop gated to `TICK_INTERVAL_MS`, single-writer by
//!    construction (the `HostClock` and the ctl handle never leave it), with an
//!    exit-neutralize join point.
//!
//! ## Obs mapping (C4)
//!
//! | Obs field         | fan-in source                                        |
//! |-------------------|------------------------------------------------------|
//! | `host_connected`  | `DirectObservability.present`                        |
//! | `playing`         | `LaneResampler.locked_state`                         |
//! | `preempted`       | always `false` (mux preempt targets the standby      |
//! |                   | usbsink HTTP daemon, never this lane; SELECT/NONE    |
//! |                   | gates the SUM downstream, so steering continues      |
//! |                   | while deselected, keeping the lane converged)        |
//! | `fill_frames`     | resampler `fill_frames` gauge (cursor-relative,      |
//! |                   | frame-granular, published every render period)       |
//! | `capture_frames`  | resampler `input_frames` (raw, monotone)             |
//! | `playback_frames` | resampler `output_frames` (real periods only)        |
//!
//! **`capture_frames` is the RAW input counter — a `LaneResampler::trim_ring`
//! must NOT be subtracted from it.** The divergence the slope estimator
//! differences is `capture − playback = input_frames − output_frames`; a trim
//! only advances the resampler's read CURSOR (`next_input_frame`), touching
//! neither `input_frames` (bumped at `push_input`) nor `output_frames`
//! (DAC-paced), so the divergence is already smooth across a trim. An earlier
//! revision subtracted the cumulative `trimmed_frames` here as "TRIM
//! compensation"; that INJECTED the very phantom negative divergence STEP it
//! claimed to cancel. At a 1400-frame auto-trim — which fires at 2 s
//! (`AUTO_TRIM_DELAY_SECONDS`), inside the 4 s probe baseline — the subtraction
//! drove the probe `response_ratio` from ~0.85 to ~43 and railed the
//! feed-forward at +1000 ppm in the wrong direction. So the mapping is a plain
//! load of `input_frames`, no trim term.
//!
//! ## Setpoint (C4) — one setpoint shared with the inner loop
//!
//! `target_fill_frames := input_resampler_target_frames +
//! warmup_cushion_frames` — the resampler's HELD target
//! (`LaneResampler::hold_fill_frames`, surfaced as
//! `LaneResamplerObservability::target_fill_frames`). This is a deliberate
//! deviation from a bare "configured target": while locked, the inner
//! `RateController` disciplines this SAME fill toward the held target
//! (`error = fill − hold_fill_frames()`), so an outer loop pinned to the bare
//! base target would fight the inner integrator until one rails — the
//! documented JTS two-controller oscillation class. Sharing the setpoint keeps
//! the cascade legitimate with the ≥10× bandwidth separation the shared crate's
//! docstring derives (inner 0.016–0.128 Hz vs outer 0.0016 Hz). The ~5 ms win
//! lands later via the measured cushion-shrink follow-up
//! (`JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES` descent), exactly the
//! sequencing the shared module doc prescribes.

use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use jasper_host_clock::{
    ctl_card_from_capture, ppm_to_ctl_value, Action, AlsaPitchCtl, HostClock, HostClockConfig,
    Ladder, Obs, ObsMode, TICK_INTERVAL_MS,
};

/// The `event=` log-line namespace prefix for the fan-in ladder.
pub const LOG_PREFIX: &str = "fanin";

/// The cross-thread signals the `fanin-host-clock` thread reads to build its
/// [`Obs`]. Every field is an `Arc` clone of an atomic the mixer already owns
/// and publishes for the USB DIRECT lane — taken once in `main` before the
/// mixer starts. The ladder thread holds ONLY these; it never touches a
/// `LaneResampler`, a `PCM`, or the mixer.
#[derive(Clone)]
pub struct HostClockSignals {
    /// Live cursor-relative resampler fill in frames (`LaneResampler`'s
    /// `fill_frames`), republished every render period. `host_clock`'s error
    /// signal.
    pub fill_frames: Arc<AtomicU64>,
    /// Cumulative input frames pushed into the resampler.
    pub input_frames: Arc<AtomicU64>,
    /// Cumulative output frames emitted (real periods only — silence paths
    /// return before the counter's `fetch_add`, verified in `render_period`).
    pub output_frames: Arc<AtomicU64>,
    /// Resampler lock state — the fan-in `playing` proxy.
    pub locked: Arc<AtomicBool>,
    /// USB DIRECT capture presence — the fan-in `host_connected` proxy.
    pub present: Arc<AtomicBool>,
    /// The lane resampler's LIVE correction ppm (its rate-adjustment relative to
    /// nominal), in **milli-ppm** (ppm × 1000) stored as i64 bits in this
    /// `AtomicU64` — the SAME atomic the resampler already publishes for STATUS
    /// (`LaneResamplerObservability::ratio_milli_ppm`), owned/written ONLY by the
    /// resampler on the mixer thread. This is the COMBO-mode probe/servo
    /// observable: with the resampler absorbing the host clock, its correction ppm
    /// is the honest host-vs-DAC rate-error readout (the fill slope is dead
    /// weight). Decoded in [`build_obs`] the same way the STATUS layer does
    /// (`(load() as i64) as f64 / 1000.0`).
    pub correction_milli_ppm: Arc<AtomicU64>,
    /// The resampler's LIVE HELD target fill — the ONE setpoint the outer loop
    /// shares with the inner `RateController` (single source of truth). Equal to
    /// `target + warmup cushion` unless the DEFAULT-OFF post-lock cushion decay
    /// has lowered it; the servo thread reads it fresh every tick and re-pins the
    /// ladder's setpoint to it, so the two controllers can never disagree about
    /// where the fill should sit.
    pub held_target_frames: Arc<AtomicU64>,
    /// REVERSE signal (servo thread → mixer): 1 iff the DLL ladder is
    /// `l0_locked`. The mixer's per-period decay tick reads this — decay only
    /// lowers the held target while the DLL is in this steady state.
    pub ladder_l0: Arc<AtomicBool>,
    /// REVERSE signal (servo thread → mixer): the DLL's last commanded bias in
    /// milli-ppm (ppm × 1000, rounded to a plain signed `AtomicI64` — no bit-cast;
    /// the sign is native, unlike the resampler's `ratio_milli_ppm` which packs an
    /// i64 into an `AtomicU64`). The decay tick reads its magnitude for the
    /// cascade-stability guard.
    pub commanded_milli_ppm: Arc<AtomicI64>,
}

/// Build the validated shared [`HostClockConfig`] for the fan-in ladder from the
/// parsed config knobs plus the resampler-derived setpoint. `enabled` here is
/// the ALREADY-RESOLVED effective flag (the direct-off gate is applied by the
/// caller in `main`, so a `enabled` + direct-off box passes `false` here and the
/// ladder is inert). `target_fill_frames` is the resampler's held target.
pub fn build_config(
    enabled: bool,
    probe_ppm: u32,
    probe_secs: u64,
    target_fill_frames: u64,
) -> HostClockConfig {
    HostClockConfig {
        enabled,
        target_fill_frames: target_fill_frames as f64,
        probe_ppm: probe_ppm as f64,
        probe_step_secs: probe_secs,
        // Combo mode runs the CORRECTION-ppm observable: a lane resampler sits
        // between the gadget ring and the mix and absorbs the host clock, so the
        // fill slope is structurally dead (the resampler flattens it — the
        // hardware defect on jts.local 2026-07-03). The probe reads the
        // resampler's own correction ppm, and the L0 servo drives it to 0. usbsink
        // solo, with no such stage, keeps `ObsMode::Fill`.
        obs_mode: ObsMode::Correction,
        log_prefix: LOG_PREFIX,
    }
}

/// Snapshot an [`Obs`] from the mixer's shared atomics. Pure (only relaxed atomic
/// loads); the mapping is the whole content.
pub fn build_obs(signals: &HostClockSignals) -> Obs {
    Obs {
        // The mixer preempt path targets the standby usbsink HTTP daemon, never
        // this lane; SELECT/NONE gates the SUM downstream. So the ladder never
        // sees a preempt — steering continues while the lane is deselected,
        // keeping it converged for the next un-mute.
        preempted: false,
        // Direct capture present ⇒ a host is attached and captured.
        host_connected: signals.present.load(Ordering::Relaxed),
        // Resampler locked ⇒ real DAC-paced audio is flowing.
        playing: signals.locked.load(Ordering::Relaxed),
        // Resampler LOCKED is the steady-regime gate for the probe's baseline:
        // while the resampler is still acquiring, its held target is filling from
        // empty (warmup ramp) — baselining then would measure that ramp, not the
        // host's clock drift. Same atomic as `playing` here because for fan-in
        // the resampler lock IS both "audio flowing" and "warmup done"; the
        // ladder reads `locked` distinctly so the two roles stay explicit.
        locked: signals.locked.load(Ordering::Relaxed),
        // Cursor-relative fill (frame-granular) — the DLL's error signal.
        fill_frames: signals.fill_frames.load(Ordering::Relaxed) as f64,
        // RAW cumulative input — NOT trim-compensated. A `trim_ring` only moves
        // the read cursor, never `input_frames` or `output_frames`, so the
        // divergence `capture − playback` is already smooth across a trim.
        // Subtracting `trimmed_frames` here would INJECT the phantom divergence
        // step it purported to cancel (see the module-level `Obs mapping` note).
        capture_frames: signals.input_frames.load(Ordering::Relaxed),
        // DAC-paced — the divergence anchor.
        playback_frames: signals.output_frames.load(Ordering::Relaxed),
        // The lane resampler's LIVE correction ppm — the COMBO-mode probe/servo
        // observable. Decoded from the milli-ppm atomic exactly as the STATUS
        // layer does: the value is an i64 (signed) stored in the u64's bits.
        correction_ppm: (signals.correction_milli_ppm.load(Ordering::Relaxed) as i64) as f64
            / 1000.0,
    }
}

/// Rate-limit gate for ctl-error logs: log at most once per ~10 s so a flapping
/// card cannot spam the journal. Pure (no clock read inside — `now_ms` is the
/// caller's monotonic clock), so it is unit-testable. `last` is the last logged
/// ms, `None` meaning "never logged".
///
/// `Option::is_none_or` is stable only since Rust 1.82; the crate declares
/// rust-version 1.75, so clippy's `incompatible_msrv` (`-D warnings` in CI)
/// would reject it. `map_or` has been stable since 1.0 and is MSRV-safe.
fn should_log_ctl_error(last: Option<u64>, now_ms: u64) -> bool {
    last.map_or(true, |last| now_ms.saturating_sub(last) >= 10_000)
}

/// The fan-in pitch-ctl actuator: holds the real [`AlsaPitchCtl`] when the card
/// opens, else `None` (fail-soft — the ladder still runs and publishes
/// telemetry). Applies a ladder [`Action`], translating ppm → ctl integer and
/// rate-limiting ctl-error logs so a flapping card cannot spam the journal.
///
/// It holds the concrete `AlsaPitchCtl` directly — NOT a boxed `dyn PitchCtl` —
/// because `alsa::ctl::ElemValue` (inside `AlsaPitchCtl`) is `!Send`, so the ctl
/// handle can never cross a thread boundary. That is why the actuator is
/// constructed INSIDE the `fanin-host-clock` thread (from the card string
/// `main` moves in), mirroring usbsink's in-thread ctl ownership: the handle and
/// the `HostClock` never leave that thread (single-writer by construction).
///
/// Fan-in delta from usbsink: **session-edge reopen**. usbsink's gadget-up
/// `ExecStartPre` guarantees the card exists before the daemon starts, so its
/// actuator opens once. Fan-in has no such ordering guarantee — a boot-order
/// race (fan-in up before the gadget function binds) would leave the ctl `None`
/// and every write a silent no-op forever, so every session would spuriously
/// probe-fail into L2. So while `None`, we re-attempt the open on session rising
/// edges, and on a late successful open the thread forces one neutralize before
/// any command.
struct HostClockActuator {
    ctl: Option<AlsaPitchCtl>,
    card: String,
    last_error_ms: Option<u64>,
}

impl HostClockActuator {
    /// Open the pitch ctl for `card` (derived from the direct-capture device).
    /// Fail-soft: a missing card yields `ctl: None` (the ladder runs, writes
    /// no-op) rather than an error. Called INSIDE the servo thread.
    fn open(card: String) -> Self {
        let ctl = Self::try_open(&card);
        Self {
            ctl,
            card,
            last_error_ms: None,
        }
    }

    fn try_open(card: &str) -> Option<AlsaPitchCtl> {
        match AlsaPitchCtl::open(card) {
            Ok(ctl) => {
                log::info!("event=fanin.host_clock_ctl_opened card={card}");
                Some(ctl)
            }
            Err(e) => {
                // Not fatal: the gadget may not be bound yet (fan-in has no
                // gadget-up ExecStartPre — see the session-edge reopen below).
                log::warn!("event=fanin.host_clock_ctl_error detail={e}");
                None
            }
        }
    }

    /// True once the ctl is open. The thread skips the neutralize/apply path
    /// with a no-op when this is false, but the ladder still ticks (telemetry).
    fn is_open(&self) -> bool {
        self.ctl.is_some()
    }

    /// Attempt to (re)open the ctl if it is currently closed. Called on session
    /// rising edges. Returns `true` if this call transitioned closed→open (the
    /// caller then forces one neutralize before any command).
    fn reopen_if_closed(&mut self) -> bool {
        if self.ctl.is_some() {
            return false;
        }
        self.ctl = Self::try_open(&self.card);
        self.ctl.is_some()
    }

    /// Apply one ladder action, translating the commanded ppm to the ctl integer.
    /// A `None` ctl no-ops (fail-soft); a write error is logged at most once per
    /// ~10 s (`now_ms` is a monotonic clock so a flapping card cannot spam).
    fn apply(&mut self, action: Action, now_ms: u64) {
        use jasper_host_clock::PitchCtl;
        let Action::WritePitch { ppm, .. } = action;
        let value = ppm_to_ctl_value(ppm);
        let Some(ctl) = self.ctl.as_mut() else {
            return;
        };
        if let Err(e) = ctl.write(value) {
            if should_log_ctl_error(self.last_error_ms, now_ms) {
                self.last_error_ms = Some(now_ms);
                log::warn!("event=fanin.host_clock_ctl_error detail={e}");
            }
        }
    }
}

/// The dedicated `fanin-host-clock` thread body (C5). Owns the [`HostClock`]
/// ladder and the pitch-ctl actuator — single-writer by construction, neither
/// ever leaves this thread. Loops on a 100 ms sleep gated to `TICK_INTERVAL_MS`
/// (1 Hz servo cadence), renders the status fragment once per tick into the
/// shared string for STATUS, and neutralizes the pitch on exit.
///
/// Neutrality invariants (C6):
/// - **startup**: one unconditional `startup_neutralize()` — but ONLY when the
///   feature is armed (`config.enabled`), so we never stomp an active solo-mode
///   usbsink DLL. (When disabled the thread should not even be spawned; the
///   caller enforces that. This guard is belt-and-braces.)
/// - **exit**: the shutdown flag forces `neutralize_for_exit("shutdown")`.
/// - **lane absence**: `present=false` / unplug flows through the ladder's
///   session-end path, which already forces neutral.
///
/// `now_ms` is a monotonic millisecond clock so the write cadence and the
/// error-log rate-limit are immune to wall-clock jumps.
pub fn run_host_clock_thread(
    config: HostClockConfig,
    signals: HostClockSignals,
    ctl_card: String,
    fragment: Arc<std::sync::Mutex<String>>,
    shutdown: Arc<AtomicBool>,
) {
    // Construct the ctl actuator INSIDE the thread: `AlsaPitchCtl` holds a
    // `!Send` `ElemValue`, so the handle can never cross a thread boundary. It
    // lives here and nowhere else (single-writer by construction).
    let mut actuator = HostClockActuator::open(ctl_card);
    let mut hc = HostClock::new(config);
    let start = Instant::now();
    let now_ms = |start: &Instant| start.elapsed().as_millis() as u64;

    // Startup neutralize: heal a crashed predecessor / stale usbsink handover.
    // The action is unconditional-of-flag inside the ladder, but we only spawn
    // this thread when armed, so this only ever runs in combo-with-flag mode —
    // never stomping an active solo-mode usbsink command.
    if let Some(action) = hc.startup_neutralize() {
        actuator.apply(action, now_ms(&start));
        log::info!("event=fanin.host_clock_pitch_reset reason=startup");
    }
    publish_fragment(&fragment, hc.status_fragment());

    // The steering loop runs inside `catch_unwind` (N5). A panic mid-tick on
    // THIS helper thread would otherwise unwind past the exit-neutralize below
    // while the daemon keeps running — leaving the host slaved to the last
    // command until the unit stops (only then does the ExecStopPost belt fire).
    // Catching the unwind lets the same exit-neutralize run on the panic path.
    // `AssertUnwindSafe`: the only state touched after a caught panic is the
    // final neutral ctl write + fragment publish, both idempotent and safe on a
    // partially-updated `hc`/`actuator`.
    let loop_result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let mut last_session = false;
        let mut last_tick = Instant::now();
        while !shutdown.load(Ordering::Relaxed) {
            if last_tick.elapsed() >= Duration::from_millis(TICK_INTERVAL_MS) {
                let obs = build_obs(&signals);
                // Session rising edge: if the ctl never opened (boot-order race),
                // re-attempt now. On a late open, force ONE neutralize before any
                // command so a stale pitch from a crashed predecessor is cleared.
                let session = obs.host_connected && obs.playing && !obs.preempted;
                if session && !last_session && !actuator.is_open() && actuator.reopen_if_closed() {
                    actuator.apply(
                        Action::WritePitch {
                            ppm: 0.0,
                            reset: true,
                        },
                        now_ms(&start),
                    );
                    log::info!("event=fanin.host_clock_pitch_reset reason=late_ctl_open");
                }
                last_session = session;

                // Single-source-of-truth setpoint: re-pin the ladder's target to
                // the resampler's LIVE held target every tick (the DEFAULT-OFF
                // cushion decay lowers it over time). The resampler OWNS the value
                // via its `held_target_frames` gauge; the ladder only reads it, so
                // the outer loop can never disagree with the inner controller
                // about where the fill should sit. A no-op when decay is off (the
                // gauge stays at the ceiling forever).
                hc.set_target_fill_frames(signals.held_target_frames.load(Ordering::Relaxed) as f64);

                for action in hc.tick(obs, now_ms(&start)) {
                    actuator.apply(action, now_ms(&start));
                }
                // Publish the REVERSE signals the mixer's per-period decay tick
                // reads: whether the ladder is `l0_locked` (decay's steady-state
                // gate) and the last commanded bias in milli-ppm (its cascade
                // guard). Written every servo tick (~1 Hz); the decay tick reads
                // the latest snapshot each render period.
                signals
                    .ladder_l0
                    .store(hc.ladder() == Ladder::L0Locked, Ordering::Relaxed);
                signals.commanded_milli_ppm.store(
                    (hc.commanded_ppm() * 1000.0).round() as i64,
                    Ordering::Relaxed,
                );
                publish_fragment(&fragment, hc.status_fragment());
                last_tick = Instant::now();
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }));
    if loop_result.is_err() {
        // A caught panic: log it, fall through to the exit-neutralize so the
        // host is still un-slaved. (The thread then ends; the daemon keeps
        // running with the ladder inert until a restart re-spawns it.)
        log::error!("event=fanin.host_clock.thread_panic detail=caught_unwind_neutralizing");
    }

    // Exit: force the host back to a free-running clock — on BOTH the graceful
    // shutdown path and a caught panic. A stopped thread must NEVER leave the
    // host slaved. SIGKILL / watchdog is covered by the unit's combo-gated
    // ExecStopPost belt-and-braces (C6).
    actuator.apply(hc.neutralize_for_exit("shutdown"), now_ms(&start));
    log::info!("event=fanin.host_clock_pitch_reset reason=shutdown");
    publish_fragment(&fragment, hc.status_fragment());

    // Clear the REVERSE signals too, so a stopped servo thread (graceful OR
    // caught-panic) does not leave the mixer's decay tick reading a frozen
    // `ladder_l0=true`. Neutralizing only the actuator un-slaves the host but
    // leaves the outer-loop signal stale: the decay engine would keep stepping
    // the held target toward the floor with no live DLL pinning the fill,
    // driving the thin-cushion free-run churn loop (underfill unlock → snap-back
    // → relock → warmup → re-decay) until a daemon restart. Publishing
    // `l0=false` / `commanded=0` makes the very next decay tick snap the cushion
    // back to the ceiling (`DecayFrozenReason::NotL0`) and hold it there.
    signals.ladder_l0.store(false, Ordering::Relaxed);
    signals.commanded_milli_ppm.store(0, Ordering::Relaxed);
}

/// Derive the ctl card spec (e.g. `hw:UAC2Gadget`) from the direct-capture
/// device, for `main` to move into the servo thread (which opens the actuator).
/// Thin re-export of the shared crate's parser so `main` needs only this module.
pub fn ctl_card_for_device(usb_direct_device: &str) -> String {
    ctl_card_from_capture(usb_direct_device)
}

/// Render the disabled-config fragment for the initial STATUS (before the thread
/// first ticks), so `/state` carries a definite `host_clock` block from boot.
pub fn initial_fragment(config: HostClockConfig) -> String {
    HostClock::new(config).status_fragment()
}

fn publish_fragment(fragment: &std::sync::Mutex<String>, rendered: String) {
    let mut guard = fragment
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    *guard = rendered;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn signals() -> HostClockSignals {
        HostClockSignals {
            fill_frames: Arc::new(AtomicU64::new(0)),
            input_frames: Arc::new(AtomicU64::new(0)),
            output_frames: Arc::new(AtomicU64::new(0)),
            locked: Arc::new(AtomicBool::new(false)),
            present: Arc::new(AtomicBool::new(false)),
            correction_milli_ppm: Arc::new(AtomicU64::new(0)),
            held_target_frames: Arc::new(AtomicU64::new(2048)),
            ladder_l0: Arc::new(AtomicBool::new(false)),
            commanded_milli_ppm: Arc::new(AtomicI64::new(0)),
        }
    }

    // ---- build_config setpoint --------------------------------------------

    #[test]
    fn build_config_uses_resampler_held_target_as_setpoint() {
        // The setpoint is the resampler's held target (target + cushion), NOT a
        // second env knob — the whole point of C4 (no outer loop fighting the
        // inner integrator).
        let cfg = build_config(true, 300, 6, 2048);
        assert_eq!(cfg.target_fill_frames, 2048.0);
        assert_eq!(cfg.probe_ppm, 300.0);
        assert_eq!(cfg.probe_step_secs, 6);
        assert_eq!(cfg.log_prefix, "fanin");
        assert!(cfg.enabled);
    }

    #[test]
    fn build_config_threads_the_resolved_enabled_flag() {
        // The direct-off gate is resolved by the caller; a false here yields an
        // inert config.
        let cfg = build_config(false, 300, 6, 2048);
        assert!(!cfg.enabled);
    }

    // ---- Obs mapping -------------------------------------------------------

    #[test]
    fn obs_maps_present_and_locked() {
        let s = signals();
        let obs = build_obs(&s);
        assert!(!obs.host_connected);
        assert!(!obs.playing);
        assert!(!obs.locked, "resampler unlocked ⇒ Obs.locked false");
        assert!(!obs.preempted, "fan-in never sees a preempt on this lane");

        s.present.store(true, Ordering::Relaxed);
        s.locked.store(true, Ordering::Relaxed);
        let obs = build_obs(&s);
        assert!(obs.host_connected, "present ⇒ host_connected");
        assert!(obs.playing, "locked ⇒ playing");
        assert!(
            obs.locked,
            "resampler locked ⇒ Obs.locked — the probe's steady-regime gate"
        );
    }

    #[test]
    fn obs_capture_is_raw_input_not_trim_compensated() {
        // capture_frames is the RAW cumulative input. A `LaneResampler::trim_ring`
        // only advances the read cursor; it does NOT bump `input_frames` (pushed
        // at capture) or `output_frames` (DAC-paced), so the divergence
        // `capture − playback` is already smooth across a trim. The Obs mapping
        // must therefore NOT subtract any trimmed-frames term — doing so was the
        // inverted-compensation bug that injected a phantom −N divergence step
        // into the slope estimator (probe response_ratio ~0.85 → ~43, +1000 ppm
        // feed-forward rail in the wrong direction).
        let s = signals();
        s.input_frames.store(100_000, Ordering::Relaxed);
        s.output_frames.store(95_000, Ordering::Relaxed);
        let obs = build_obs(&s);
        assert_eq!(
            obs.capture_frames, 100_000,
            "capture_frames must be the raw input counter (no trim subtraction)"
        );
        assert_eq!(
            obs.playback_frames, 95_000,
            "playback (DAC-paced) is the divergence anchor"
        );

        // A trim happening between two ticks bumps neither counter, so the very
        // next Obs sees the SAME divergence — no step. Emulate a period where
        // input/output advanced by one on-rate period each (a trim in between is
        // invisible to these counters by construction).
        s.input_frames.store(100_256, Ordering::Relaxed);
        s.output_frames.store(95_256, Ordering::Relaxed);
        let obs = build_obs(&s);
        assert_eq!(
            obs.capture_frames as i64 - obs.playback_frames as i64,
            5_000,
            "the divergence is unchanged across a trim — no phantom step"
        );
    }

    #[test]
    fn obs_fill_frames_is_frame_granular_from_the_gauge() {
        let s = signals();
        s.fill_frames.store(2050, Ordering::Relaxed);
        assert_eq!(build_obs(&s).fill_frames, 2050.0);
    }

    #[test]
    fn obs_correction_ppm_decodes_the_signed_milli_ppm_gauge() {
        // The correction observable is the resampler's `ratio_milli_ppm` (the same
        // atomic STATUS reads): milli-ppm, i64-bits-in-u64. build_obs must decode
        // it the SAME way the state layer does — `(load() as i64) as f64 / 1000`
        // — so a NEGATIVE correction (resampler running slower than nominal) reads
        // back with the right sign, not a giant positive u64.
        let s = signals();
        // +120.5 ppm ⇒ 120_500 milli-ppm.
        s.correction_milli_ppm.store(120_500, Ordering::Relaxed);
        assert_eq!(build_obs(&s).correction_ppm, 120.5);
        // −250.0 ppm ⇒ −250_000 milli-ppm, stored as its u64 bit pattern.
        s.correction_milli_ppm
            .store((-250_000i64) as u64, Ordering::Relaxed);
        assert_eq!(
            build_obs(&s).correction_ppm,
            -250.0,
            "a negative correction must decode with the right sign"
        );
    }

    #[test]
    fn build_config_selects_correction_obs_mode() {
        // Combo mode ALWAYS runs the CORRECTION observable — the fill slope is
        // dead when a lane resampler sits between the gadget ring and the mix.
        let cfg = build_config(true, 300, 6, 2048);
        assert_eq!(cfg.obs_mode, ObsMode::Correction);
    }

    // ---- ctl card derivation ----------------------------------------------

    #[test]
    fn ctl_card_for_device_strips_to_card_prefix() {
        assert_eq!(ctl_card_for_device("hw:UAC2Gadget"), "hw:UAC2Gadget");
        assert_eq!(ctl_card_for_device("hw:UAC2Gadget,0,0"), "hw:UAC2Gadget");
        assert_eq!(ctl_card_for_device("plughw:UAC2Gadget"), "hw:UAC2Gadget");
    }

    // ---- ctl-error log rate limit (pure) ----------------------------------

    #[test]
    fn ctl_error_log_is_rate_limited_to_ten_seconds() {
        // First error (never logged) always logs.
        assert!(should_log_ctl_error(None, 0));
        // A second error < 10 s after the last logged one is suppressed.
        assert!(!should_log_ctl_error(Some(0), 5_000));
        assert!(!should_log_ctl_error(Some(0), 9_999));
        // At/after 10 s, log again.
        assert!(should_log_ctl_error(Some(0), 10_000));
        assert!(should_log_ctl_error(Some(0), 25_000));
    }

    // ---- Actuator: fail-soft closed path ----------------------------------
    //
    // The actuator holds a concrete `AlsaPitchCtl` (`!Send`), so it cannot take
    // a mock write handle; the write-through path is exercised on-device / by
    // Linux CI. What IS unit-testable on any host is the fail-soft closed path:
    // a bad card yields a closed actuator whose `apply` no-ops and whose
    // `reopen_if_closed` re-fails without panicking (the boot-order-race safe
    // degrade). `AlsaPitchCtl::open` returns `Err` for a nonexistent card on
    // Linux CI and via the scratch-crate stub on macOS, so this runs everywhere.

    #[test]
    fn actuator_bad_card_is_closed_and_apply_no_ops() {
        let mut a = HostClockActuator::open("hw:__jts_nonexistent_test_card__".to_string());
        assert!(!a.is_open(), "a nonexistent card yields a closed actuator");
        // apply on a closed ctl is a no-op (never touches AlsaPitchCtl::write).
        a.apply(
            Action::WritePitch {
                ppm: 300.0,
                reset: true,
            },
            0,
        );
        assert!(!a.is_open());
    }

    #[test]
    fn actuator_reopen_of_bad_card_stays_closed() {
        let mut a = HostClockActuator::open("hw:__jts_nonexistent_test_card__".to_string());
        // The bogus card can never open, so reopen returns false and the ctl
        // stays None (writes keep no-op'ing — safe degrade, no spurious probe).
        assert!(!a.reopen_if_closed());
        assert!(!a.is_open());
    }
}
