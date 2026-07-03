// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Host-slaved USB clock — Stage 1 of the USB low-latency foundation, solo
//! (aloop) mode.
//!
//! The daemon-agnostic ladder + probe + servo + write-gate + ALSA actuator now
//! live in the shared [`jasper_host_clock`] crate (moved there when combo mode
//! gave fan-in its own copy of this loop — the two daemons share byte-identical
//! servo semantics and differ only in the `event=` log prefix and which env
//! keys they parse). This module is the usbsink-side thin shim: it re-exports
//! the shared types the daemon uses, parses the `JASPER_USBSINK_HOST_CLOCK*`
//! env keys, and snapshots [`Obs`] from this crate's `SharedState` atomics.
//!
//! See [`jasper_host_clock`]'s module docstring for the authoritative
//! derivation (cascade defense, feed-forward, cross-platform conditions).
//!
//! The invariant pinned across both daemons: **the daemon that owns the gadget
//! capture owns the pitch ctl.** In solo (aloop) mode the usbsink bridge owns
//! `hw:UAC2Gadget`, so it drives the ladder from `run_state_publisher`; in
//! combo (USB DIRECT) mode fan-in owns the capture and drives it instead, and
//! usbsink runs in standby with the feature force-disabled (C5).

use std::sync::atomic::Ordering;

// Re-export the shared ladder/servo/actuator surface so the rest of the daemon
// (main.rs) keeps importing `host_clock::{...}` unchanged. The `alsa` feature
// of the shared crate is enabled under usbsink's `alsa-runtime` (see Cargo.toml)
// so `AlsaPitchCtl` is available in the production build.
pub use jasper_host_clock::{
    ctl_card_from_capture, ppm_to_ctl_value, Action, HostClock, HostClockConfig, Obs, PitchCtl,
    TICK_INTERVAL_MS,
};

#[cfg(feature = "alsa-runtime")]
pub use jasper_host_clock::AlsaPitchCtl;

use crate::SharedState;

/// The `event=` log-line namespace prefix for the usbsink bridge's ladder.
const LOG_PREFIX: &str = "usbsink_audio";

/// Parse + validate the usbsink host-clock config from the daemon's env chain.
///
/// `enabled` gates the feature entirely; a non-empty value other than the
/// literal `enabled` (case-insensitive) is a warned no-op that stays disabled
/// (mirrors `JASPER_USBSINK_PREEMPT`'s literal idiom, inverted because this is
/// opt-in). Tunable ranges fail fast, like the daemon's `validate_audio_config`.
///
/// The parsed keys/ranges/defaults are unchanged from before the ladder moved
/// to the shared crate; this shim just builds the shared
/// [`HostClockConfig`] (threading `LOG_PREFIX`) instead of owning the struct.
///
/// `getenv` is injected so the parse is unit-testable without touching the
/// process environment.
pub fn from_env<F>(getenv: F) -> Result<HostClockConfig, String>
where
    F: Fn(&str) -> Option<String>,
{
    let enabled = match getenv("JASPER_USBSINK_HOST_CLOCK") {
        Some(raw) => {
            let v = raw.trim();
            if v.is_empty() {
                false
            } else if v.eq_ignore_ascii_case("enabled") {
                true
            } else {
                // Warned no-op: don't crash, don't silently enable.
                log::warn!(
                    "event=usbsink_audio.host_clock_config_ignored key=JASPER_USBSINK_HOST_CLOCK value={v:?} reason=not_literal_enabled"
                );
                false
            }
        }
        None => false,
    };

    let target_fill_frames =
        parse_env_u64(&getenv, "JASPER_USBSINK_HOST_CLOCK_TARGET_FILL_FRAMES", 384)?;
    let probe_ppm = parse_env_u64(&getenv, "JASPER_USBSINK_HOST_CLOCK_PROBE_PPM", 300)?;
    let probe_step_secs = parse_env_u64(&getenv, "JASPER_USBSINK_HOST_CLOCK_PROBE_SECONDS", 6)?;

    // Valid 128..(ring_periods-1)×period_frames. We do not have ring_periods
    // here; the loose floor/ceiling below is the hard safety bound (a
    // target below one period or above a few periods is nonsensical for a
    // 2-3 period ring). The daemon validates ring geometry separately.
    if !(128..=4096).contains(&target_fill_frames) {
        return Err(format!(
            "JASPER_USBSINK_HOST_CLOCK_TARGET_FILL_FRAMES={target_fill_frames} out of range 128..=4096"
        ));
    }
    // Floor is 200, not 100: a probe at or below the ~163 ppm Windows
    // (usbaudio2.sys) reaction deadband is GUARANTEED to measure ~no
    // response on a compliant deadbanded host → a spurious probe_fail → L2
    // every session. The shared module's cross-platform notes document that
    // deadband, so config validation must not accept a value they say
    // cannot work. Ceiling stays 800 to keep the whole probe inside the
    // ±1000 ppm validity window with margin. (Default 300 remains valid;
    // it also sits above the deadband — see the .env.example prose and the
    // HANDOFF's Windows-deadband caveat for the residual margin note.)
    if !(200..=800).contains(&probe_ppm) {
        return Err(format!(
            "JASPER_USBSINK_HOST_CLOCK_PROBE_PPM={probe_ppm} out of range 200..=800 (a probe at/below the ~163 ppm Windows usbaudio2.sys deadband would falsely fail every session; ceiling keeps the probe inside the ±1000 ppm validity window)"
        ));
    }
    if !(5..=10).contains(&probe_step_secs) {
        return Err(format!(
            "JASPER_USBSINK_HOST_CLOCK_PROBE_SECONDS={probe_step_secs} out of range 5..=10"
        ));
    }

    Ok(HostClockConfig {
        enabled,
        target_fill_frames: target_fill_frames as f64,
        probe_ppm: probe_ppm as f64,
        probe_step_secs,
        log_prefix: LOG_PREFIX,
    })
}

/// A hard-disabled usbsink config (C5 standby). In standby the audio loop that
/// feeds the DLL never runs, so there is no fill source; the feature is forced
/// off regardless of env (fan-in's lane resampler owns all rate matching). The
/// startup + exit pitch neutralize still run against this config (both are
/// unconditional in the publisher and never leave the host slaved), so a
/// crashed predecessor is still healed. Never fails (no env parse).
pub fn disabled_config() -> HostClockConfig {
    HostClockConfig::disabled(LOG_PREFIX)
}

fn parse_env_u64<F>(getenv: &F, key: &str, default: u64) -> Result<u64, String>
where
    F: Fn(&str) -> Option<String>,
{
    match getenv(key) {
        Some(raw) if !raw.trim().is_empty() => raw
            .trim()
            .parse::<u64>()
            .map_err(|e| format!("parsing {key}={raw:?}: {e}")),
        _ => Ok(default),
    }
}

/// Snapshot an [`Obs`] from the daemon's shared atomics. Used by the real
/// publisher; the shared crate's tests build `Obs` directly with fake values.
pub fn obs_from_shared(state: &SharedState, period_frames: u32) -> Obs {
    let fill_periods = state.ring_fill_periods.load(Ordering::Relaxed);
    Obs {
        playing: state.playing.load(Ordering::Relaxed),
        host_connected: state.host_connected.load(Ordering::Relaxed),
        preempted: state.preempted.load(Ordering::Relaxed),
        fill_frames: (fill_periods as f64) * (period_frames as f64),
        capture_frames: state.capture_frames.load(Ordering::Relaxed),
        playback_frames: state.playback_frames.load(Ordering::Relaxed),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- Config parse ------------------------------------------------------
    //
    // These pin the usbsink-side `from_env` (the JASPER_USBSINK_* keys, ranges,
    // and defaults). The ladder/servo/fragment behavior is exhaustively tested
    // in the shared jasper-host-clock crate; here we only cover the env parse
    // this shim still owns.

    #[test]
    fn config_disabled_by_default_and_inert() {
        let cfg = from_env(|_| None).unwrap();
        assert!(!cfg.enabled);
        assert_eq!(cfg.log_prefix, "usbsink_audio");
    }

    #[test]
    fn config_enabled_only_on_literal_enabled() {
        let get = |k: &str| {
            if k == "JASPER_USBSINK_HOST_CLOCK" {
                Some("EnAbLeD".to_string())
            } else {
                None
            }
        };
        assert!(from_env(get).unwrap().enabled);
        // Any other value: warned no-op, stays disabled.
        let get_other = |k: &str| {
            if k == "JASPER_USBSINK_HOST_CLOCK" {
                Some("on".to_string())
            } else {
                None
            }
        };
        assert!(!from_env(get_other).unwrap().enabled);
    }

    #[test]
    fn config_rejects_out_of_range_tunables() {
        let with = |key: &'static str, val: &'static str| {
            move |k: &str| {
                if k == key {
                    Some(val.to_string())
                } else {
                    None
                }
            }
        };
        assert!(from_env(with("JASPER_USBSINK_HOST_CLOCK_TARGET_FILL_FRAMES", "64")).is_err());
        assert!(from_env(with("JASPER_USBSINK_HOST_CLOCK_PROBE_PPM", "50")).is_err());
        // Below/at the ~163 ppm Windows deadband is rejected (S4): a probe
        // there would falsely fail every session on a compliant host.
        assert!(
            from_env(with("JASPER_USBSINK_HOST_CLOCK_PROBE_PPM", "100")).is_err(),
            "PROBE_PPM=100 (<= ~163 ppm deadband) must be rejected"
        );
        assert!(from_env(with("JASPER_USBSINK_HOST_CLOCK_PROBE_PPM", "199")).is_err());
        // The floor itself (200, just above the deadband) is accepted.
        assert!(
            from_env(with("JASPER_USBSINK_HOST_CLOCK_PROBE_PPM", "200")).is_ok(),
            "PROBE_PPM=200 (above the deadband) must be accepted"
        );
        assert!(from_env(with("JASPER_USBSINK_HOST_CLOCK_PROBE_PPM", "1200")).is_err());
        assert!(from_env(with("JASPER_USBSINK_HOST_CLOCK_PROBE_SECONDS", "3")).is_err());
        assert!(from_env(with("JASPER_USBSINK_HOST_CLOCK_PROBE_SECONDS", "20")).is_err());
    }

    #[test]
    fn config_defaults_match_contract() {
        let cfg = from_env(|_| None).unwrap();
        assert_eq!(cfg.target_fill_frames, 384.0);
        assert_eq!(cfg.probe_ppm, 300.0);
        assert_eq!(cfg.probe_step_secs, 6);
    }

    #[test]
    fn disabled_config_is_off_with_usbsink_prefix() {
        let cfg = disabled_config();
        assert!(!cfg.enabled);
        assert_eq!(cfg.log_prefix, "usbsink_audio");
    }
}
