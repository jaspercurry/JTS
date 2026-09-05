// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The daemon skeleton `jasper-fanin` and `jasper-outputd` share: the
//! hand-built JSON emitter their observability payloads are written with
//! ([`json`]), the systemd notify seam, the config-class park contract, and
//! the helper-thread stack budget. Audio-free and ALSA-free.

pub mod json;

pub use sd_notify::NotifyState;

/// Stack bytes for every helper thread in a JTS audio daemon.
/// `mlockall(MCL_CURRENT|MCL_FUTURE)` populates and pins a thread's WHOLE
/// stack, so Rust's 2 MiB default costs 2 MiB of unswappable RAM per thread;
/// each daemon's audio loop runs on `main`, not on a helper.
pub const HELPER_STACK_BYTES: usize = 512 * 1024;

/// Exit code for a CONFIG-validation failure (sysexits.h EX_CONFIG). Both
/// daemons' units pair it with `RestartPreventExitStatus=78`: a fail-closed
/// config rejection PARKS the unit failed (visible on /state + doctor) instead
/// of crash-looping — restarting cannot fix bad config, and the restart burst
/// escalates to `StartLimitAction=reboot` after five starts in five minutes.
/// See ADR-0141.
pub const EXIT_CONFIG: i32 = 78;

/// Marker attached (as an `anyhow` context layer) to an error whose cause is
/// CONFIG-class, so `main` can downcast for it and exit [`EXIT_CONFIG`].
/// `anyhow` walks the whole chain, so the marker survives every
/// `.context(...)` layer a call site adds above it on the way out.
///
/// Attach it NARROWLY, at the site that knows the class. A startup failure
/// that clears on a retry — a missing snd-aloop substream, a de-enumerated USB
/// DAC, a held ring open-lock, an unapplied tmpfs permission — must keep
/// `Restart=on-failure`; marking every error parks the speaker on a transient
/// hardware blip.
#[derive(Debug)]
pub struct ConfigClassError;

impl std::fmt::Display for ConfigClassError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "config-class startup fault (park, do not restart-loop)")
    }
}

impl std::error::Error for ConfigClassError {}

/// Send one systemd notification; a no-op when `NOTIFY_SOCKET` is unset. The
/// `Type=notify` + `WatchdogSec=30s` contract in both daemons' units is
/// carried by this call.
pub fn notify(state: NotifyState<'_>) -> std::io::Result<()> {
    sd_notify::notify(&[state])
}

/// Pin the daemon's pages in RAM: `mlockall(MCL_CURRENT | MCL_FUTURE)` keeps
/// both currently-mapped pages and future allocations resident.
///
/// Call it AFTER helper threads are spawned, so MCL_FUTURE does not try to
/// lock pthread stack mmaps under a small local-dev RLIMIT_MEMLOCK. Failure is
/// non-fatal and the caller decides how to say so: the systemd units grant
/// `LimitMEMLOCK=infinity` so production succeeds, while `cargo test` / `cargo
/// run` as non-root hits RLIMIT_MEMLOCK and degrades to the
/// `Slice=jts-audio.slice` / `MemorySwapMax=0` belt, which is the load-bearing
/// protection anyway.
pub fn lock_memory() -> std::io::Result<()> {
    // SAFETY: mlockall is a single syscall with no aliasing concerns. It does
    // not dereference Rust pointers or create aliases.
    let rc = unsafe { libc::mlockall(libc::MCL_CURRENT | libc::MCL_FUTURE) };
    if rc == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}
