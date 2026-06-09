//! jasper-fanin — JTS renderer fan-in daemon.
//!
//! Reads N snd-aloop substream pairs (one per music renderer), sums
//! them sample-wise, writes to a single dedicated "summed music"
//! substream that CamillaDSP and the AEC bridge dsnoop on. This is
//! the production renderer topology; the old renderer-side dmix path
//! was retired after AirPlay burst testing exposed timing drops.
//!
//! Read `docs/HANDOFF-fan-in-daemon.md` for the full architecture,
//! resilience contract, and observability contract before modifying.
//!
//! This file is the entry point. Module layout:
//!   - `config`   — JASPER_FANIN_* env var parsing.
//!   - `watchdog` — progress-sentinel heartbeat (sd_notify pattern).
//!   - `mixer`    — ALSA read/sum/write loop.
//!   - `state`    — UDS STATUS endpoint for /state aggregation.
//!   - `xrun_log` — append-only ring of xrun events at
//!                  /var/lib/jasper/fanin/xrun_history.jsonl.
//! The mux preempt path means simultaneous sources should not happen
//! in steady state. If future measurement shows audible source-handover
//! clicks, add ramping in the mixer with tests and doctor visibility.

mod config;
mod loudness;
mod mixer;
mod state;
mod tts;
mod watchdog;
mod xrun_log;

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::sync::mpsc::channel;

use anyhow::{Context, Result};
use log::{error, info, warn};

use crate::config::Config;
use crate::mixer::Mixer;
use crate::state::StateServer;
use crate::tts::{spawn_tts_server, tts_channels, TtsInput};
use crate::watchdog::Heartbeat;
use crate::xrun_log::XrunLog;

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
        "event=fanin.config_loaded inputs={} output={} sample_rate={} period_frames={} input_buffer_frames={} output_buffer_frames={}",
        config.input_pcms.len(),
        config.output_pcm,
        config.sample_rate,
        config.period_frames,
        config.input_buffer_frames,
        config.output_buffer_frames,
    );

    // Heartbeat. The work loop calls `bump_progress()` after every
    // successful unit of work. A background thread pings
    // sd_notify WATCHDOG=1 every 10 s only if the sentinel is fresh.
    // This catches the failure mode that matters most — a deadlocked
    // work loop. READY=1 is sent later, after ALSA PCMs and the STATUS
    // endpoint are initialized.
    //
    // ORDER MATTERS: spawn this BEFORE mlockall. Thread creation
    // mmaps a stack; with MCL_FUTURE active, that mmap is locked,
    // which fails with EAGAIN if RLIMIT_MEMLOCK is below the stack
    // size. systemd's LimitMEMLOCK=infinity handles this in
    // production, but local dev / cargo run / unit tests run under
    // the default ulimit (64 KB on most distros) and fail without
    // this ordering. Caught by the chunk 3 smoke test.
    let heartbeat = Arc::new(Heartbeat::new());
    heartbeat.spawn();

    // Shutdown signal — caught from SIGTERM (systemd stop) and SIGINT
    // (Ctrl-C during `cargo run`). The work loop checks this every
    // iteration and exits cleanly.
    let shutdown = Arc::new(AtomicBool::new(false));
    install_signal_handlers(&shutdown)?;

    // Channel for xrun events: mixer.send (non-blocking), xrun-log
    // thread.recv (blocking, fdatasync to disk). Keeps the mixer's
    // hot path off of disk I/O.
    let (xrun_tx, xrun_rx) = channel();
    let xrun_log_path = config.xrun_log_path.clone();
    let xrun_writer = std::thread::Builder::new()
        .name("fanin-xrun-writer".into())
        .spawn(move || {
            let mut log = match XrunLog::new(&xrun_log_path) {
                Ok(l) => l,
                Err(e) => {
                    warn!(
                        "event=fanin.xrun_log.init_failed path={} detail={:#}",
                        xrun_log_path, e,
                    );
                    return;
                }
            };
            // Receiver loops until all senders drop (which happens
            // when main exits and mixer is dropped).
            while let Ok(event) = xrun_rx.recv() {
                log.record(&event);
            }
            info!("event=fanin.xrun_log.writer_stopped");
        })
        .context("spawning xrun-log writer thread")?;

    let (tts_input, tts_metrics) = if let Some(socket_path) = &config.tts_socket_path {
        let (tts_tx, tts_rx, tts_flush_tx, tts_flush_rx, metrics, epoch) =
            tts_channels(config.tts_max_pending_frames);
        spawn_tts_server(
            PathBuf::from(socket_path),
            tts_tx,
            tts_flush_tx,
            epoch,
            metrics.clone(),
        )?;
        (
            Some(TtsInput {
                rx: tts_rx,
                flush_rx: tts_flush_rx,
                metrics: metrics.clone(),
                max_pending_frames: config.tts_max_pending_frames,
                program_duck_db: config.tts_program_duck_db,
                assistant_loudness: config.assistant_loudness,
            }),
            Some(metrics),
        )
    } else {
        info!("event=fanin.tts_socket.disabled");
        (None, None)
    };

    // Open ALSA: N input PCMs + 1 output PCM. Every configured input is
    // required in the production fan-in topology; a missing lane means
    // one renderer can silently play without entering the summed music
    // reference.
    let mut mixer =
        Mixer::new(&config, xrun_tx, tts_input).context("opening ALSA PCMs")?;
    info!(
        "event=fanin.mixer.ready inputs_opened={} (of {} configured)",
        mixer.input_count(),
        config.input_pcms.len(),
    );

    // mlockall — pin pages in RAM so the audio path is never paged
    // out under memory pressure. Belt to the systemd unit's
    // LimitMEMLOCK=infinity + Slice=jts-audio.slice MemorySwapMax=0
    // suspenders. Non-fatal if it fails (e.g., local dev under default
    // ulimit); the slice membership is the load-bearing protection
    // in production.
    //
    // Called AFTER all helper threads spawn — see the heartbeat
    // comment above. mlockall's MCL_FUTURE locks future mmaps, which
    // collides with pthread_create's stack mmap if RLIMIT_MEMLOCK is
    // small. By the time we get here, the heartbeat thread is up;
    // the state-server and xrun-writer threads spawn next; mlockall
    // moves to after those.
    // (Continued below.)

    // UDS STATUS endpoint — surfaces daemon state for jasper-control's
    // /state aggregator and for jasper-doctor's check_fanin_service.
    // The server moves into its own thread; we share `shutdown` via
    // Arc clone.
    let state_server = StateServer::new(
        &mixer,
        Arc::clone(&heartbeat),
        PathBuf::from(&config.control_socket_path),
        config.sample_rate,
        config.period_frames,
        config.input_buffer_frames,
        config.output_buffer_frames,
        config.output_pcm.clone(),
        tts_metrics,
    );
    let state_server_shutdown = Arc::clone(&shutdown);
    let state_thread = std::thread::Builder::new()
        .name("fanin-state-server".into())
        .spawn(move || {
            if let Err(e) = state_server.run(&state_server_shutdown) {
                error!(
                    "event=fanin.state_server.failed detail={:#}",
                    e
                );
            }
        })
        .context("spawning state-server thread")?;

    heartbeat.notify_ready();

    // All threads spawned; now safe to mlockall. See the multi-line
    // comment above for why this can't go earlier under default ulimit.
    lock_memory();

    // Run the work loop. Returns Ok on graceful shutdown; Err on
    // structural failure (which systemd's Restart=on-failure handles
    // by bringing us back fresh).
    let result = mixer.run(&shutdown, &heartbeat);

    // Drop the mixer (and its xrun_tx Sender) so the writer thread's
    // recv loop terminates. Then join the helper threads with a
    // best-effort timeout — if either hangs, systemd's
    // TimeoutStopSec=5s will SIGKILL us anyway.
    drop(mixer);
    let _ = state_thread.join();
    let _ = xrun_writer.join();

    match &result {
        Ok(_) => {
            info!("event=fanin.shutdown reason=signal graceful=true");
        }
        Err(e) => {
            // Log with the full anyhow context chain. systemd captures
            // stderr to journald so the post-mortem evidence survives
            // even if we're being reaped.
            error!("event=fanin.shutdown reason=error detail={:#}", e);
        }
    }
    heartbeat.notify_stopping();
    result
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
    // It does not dereference Rust pointers or create aliases. We call
    // it after helper threads are spawned so MCL_FUTURE does not try to
    // lock pthread stack mmaps under a small local-dev RLIMIT_MEMLOCK.
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
