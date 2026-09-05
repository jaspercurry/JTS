// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The daemon skeleton `jasper-fanin` and `jasper-outputd` share: the
//! hand-built JSON emitter their observability payloads are written with
//! ([`json`]), the [`DaemonHooks`] each daemon speaks through, the systemd
//! notify seam, the config-class park contract, and the helper-thread stack
//! budget. Audio-free and ALSA-free.

pub mod hooks;
pub mod json;

pub use hooks::DaemonHooks;
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
/// With MCL_FUTURE active, a later thread spawn's stack mmap is locked too and
/// fails with EAGAIN under a small RLIMIT_MEMLOCK — which is why fan-in calls
/// this only once its helper threads are up. Failure is non-fatal: the systemd
/// units grant `LimitMEMLOCK=infinity` so production succeeds, while `cargo
/// test` / `cargo run` as non-root hits RLIMIT_MEMLOCK and degrades to the
/// `Slice=jts-audio.slice` / `MemorySwapMax=0` belt, which is the load-bearing
/// protection anyway.
pub fn lock_memory(hooks: DaemonHooks) {
    let prefix = hooks.event_prefix;
    // SAFETY: mlockall is a single syscall with no aliasing concerns. It does
    // not dereference Rust pointers or create aliases.
    let rc = unsafe { libc::mlockall(libc::MCL_CURRENT | libc::MCL_FUTURE) };
    if rc == 0 {
        (hooks.info)(&format!("event={prefix}.mlockall_ok"));
    } else {
        let error = std::io::Error::last_os_error();
        (hooks.error)(&format!(
            "event={prefix}.mlockall_failed errno={} detail={error}",
            error.raw_os_error().unwrap_or(0),
        ));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static EMITTED: Mutex<Vec<(&'static str, String)>> = Mutex::new(Vec::new());

    fn push(channel: &'static str, message: &str) {
        EMITTED.lock().unwrap().push((channel, message.to_string()));
    }

    /// Both outcomes name the hosting daemon and land on the channel that
    /// matches their severity: fan-in renders `error` at `error!`, so a
    /// failure demoted to `info`/`warn` would drop out of a default-level
    /// journal read.
    #[test]
    fn lock_memory_reports_one_outcome_under_the_daemon_prefix() {
        const HOOKS: DaemonHooks = DaemonHooks {
            event_prefix: "test",
            writer_stack_bytes: 0,
            info: |message| push("info", message),
            warn: |message| push("warn", message),
            error: |message| push("error", message),
        };

        lock_memory(HOOKS);
        // SAFETY: munlockall is a single syscall; it undoes the MCL_FUTURE the
        // call above may have armed for the rest of this test process.
        unsafe { libc::munlockall() };

        let emitted = EMITTED.lock().unwrap();
        assert_eq!(emitted.len(), 1, "{emitted:?}");
        let (channel, line) = &emitted[0];
        match line.split_once(' ') {
            None => {
                assert_eq!(line, "event=test.mlockall_ok");
                assert_eq!(*channel, "info");
            }
            Some((head, rest)) => {
                assert_eq!(head, "event=test.mlockall_failed");
                assert!(rest.starts_with("errno="), "{rest}");
                assert!(rest.contains(" detail="), "{rest}");
                assert_eq!(*channel, "error");
            }
        }
    }
}
