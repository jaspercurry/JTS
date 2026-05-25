//! jasper-fanin — JTS renderer fan-in daemon.
//!
//! Reads N snd-aloop substream pairs (one per music renderer), sums
//! them sample-wise, writes to a single dedicated "summed music"
//! substream that CamillaDSP and the AEC bridge dsnoop on. Replaces
//! the renderer-side dmix (`pcm.jasper_renderer_mix`) and its ~85 ms
//! of buffering invisible to shairport's `snd_pcm_delay()`.
//!
//! Read `docs/HANDOFF-fan-in-daemon.md` for the full architecture,
//! resilience contract, and observability contract before modifying.
//!
//! This file is the entry point. Module layout:
//!   - `config`   — JASPER_FANIN_* env var parsing.
//!   - `watchdog` — progress-sentinel heartbeat (sd_notify pattern).
//!   - `mixer`    — the actual ALSA read/sum/write loop. (Phase 2 chunk 2.)
//!   - `state`    — UDS STATUS endpoint for /state aggregation. (Phase 2 chunk 3.)
//!   - `handover` — cosine ramp on input transitions. (Phase 2 chunk 3.)
//!   - `xrun_log` — append-only ring of xrun events. (Phase 2 chunk 3.)
//!
//! Today (Phase 2 chunk 1 — skeleton): main starts the heartbeat, idles
//! with a 1 Hz progress bump so the watchdog sentinel stays fresh,
//! exits cleanly on SIGTERM/SIGINT. No audio I/O yet — that lands in
//! Phase 2 chunk 2.

mod config;
mod watchdog;

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::Result;
use log::{error, info};

use crate::config::Config;
use crate::watchdog::Heartbeat;

fn main() -> Result<()> {
    // env_logger reads JASPER_FANIN_LOG_LEVEL (or RUST_LOG as a fallback
    // for cargo-run dev) and defaults to "info" so structured event=
    // lines land in journald at priority info. Timestamps to seconds —
    // journald owns precise timestamps; we just need ordering hints
    // for `cargo run` dev sessions.
    env_logger::Builder::from_env(
        env_logger::Env::default()
            .filter_or("JASPER_FANIN_LOG_LEVEL", "info"),
    )
    .format_timestamp_secs()
    .init();

    info!("event=fanin.boot version={}", env!("CARGO_PKG_VERSION"));

    // Parse JASPER_FANIN_* env vars. Errors here are structural —
    // the systemd EnvironmentFile didn't land or has a bad value.
    // Fail-hard with a clear message; systemd's Restart=on-failure
    // will retry on a 5 s backoff per the unit's RestartSec.
    let config = Config::from_env()?;
    info!(
        "event=fanin.config_loaded inputs={} output={} sample_rate={} period_frames={} buffer_frames={} handover_ramp_ms={}",
        config.input_pcms.len(),
        config.output_pcm,
        config.sample_rate,
        config.period_frames,
        config.buffer_frames,
        config.handover_ramp_ms,
    );

    // mlockall — pin pages in RAM so the audio path is never paged
    // out under memory pressure. The systemd unit also pins the daemon
    // to jts-audio.slice with MemorySwapMax=0, but mlockall is the
    // in-process belt-and-suspenders. Non-fatal if it fails (e.g.,
    // running `cargo test` as non-root locks down before
    // RLIMIT_MEMLOCK is raised by systemd's LimitMEMLOCK=infinity).
    lock_memory();

    // Heartbeat. The work loop calls `bump_progress()` after every
    // successful unit of work. A background thread pings
    // sd_notify WATCHDOG=1 every 10 s only if the sentinel is fresh.
    // This catches the failure mode that matters most — a deadlocked
    // work loop. See watchdog.rs comment block for the full rationale.
    let heartbeat = Arc::new(Heartbeat::new());
    heartbeat.spawn();

    // Shutdown signal — caught from SIGTERM (systemd stop) and SIGINT
    // (Ctrl-C during `cargo run`). The work loop checks this every
    // iteration and exits cleanly.
    let shutdown = Arc::new(AtomicBool::new(false));
    install_signal_handlers(&shutdown)?;

    // Phase 2 chunk 1 skeleton: the mixer doesn't exist yet, so the
    // daemon idles. A 1 Hz progress bump keeps the watchdog sentinel
    // fresh — enough to exercise the heartbeat without burning CPU.
    // Phase 2 chunk 2 replaces this loop with the real mixer.
    info!("event=fanin.idle reason=phase_2_chunk_1_skeleton");
    while !shutdown.load(Ordering::Relaxed) {
        heartbeat.bump_progress();
        std::thread::sleep(Duration::from_secs(1));
    }

    info!("event=fanin.shutdown reason=signal graceful=true");
    heartbeat.notify_stopping();
    Ok(())
}

/// Pin the daemon's pages in RAM. mlockall(MCL_CURRENT | MCL_FUTURE)
/// keeps both currently-mapped pages and future allocations resident.
///
/// Non-fatal on failure: log and continue. The systemd unit grants
/// LimitMEMLOCK=infinity so production lockall succeeds; `cargo test`
/// or `cargo run` as non-root hits RLIMIT_MEMLOCK and silently
/// degrades to the Slice=jts-audio.slice / MemorySwapMax=0 belt
/// (which is the load-bearing protection anyway).
fn lock_memory() {
    // SAFETY: mlockall is a single syscall with no aliasing concerns.
    // We're calling it once at startup with no concurrent threads
    // (heartbeat hasn't spawned yet).
    let rc = unsafe {
        libc::mlockall(libc::MCL_CURRENT | libc::MCL_FUTURE)
    };
    if rc == 0 {
        info!("event=fanin.mlockall_ok");
    } else {
        let err = std::io::Error::last_os_error();
        error!(
            "event=fanin.mlockall_failed errno={} detail={}",
            err.raw_os_error().unwrap_or(0),
            err,
        );
    }
}

fn install_signal_handlers(shutdown: &Arc<AtomicBool>) -> Result<()> {
    use signal_hook::consts::{SIGINT, SIGTERM};
    use signal_hook::flag;
    // signal_hook's `flag::register` is async-signal-safe and sets
    // the AtomicBool from inside the signal handler. The main loop
    // polls the atomic.
    flag::register(SIGTERM, Arc::clone(shutdown))?;
    flag::register(SIGINT, Arc::clone(shutdown))?;
    Ok(())
}
