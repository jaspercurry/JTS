// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! jasper-fanin — JTS renderer fan-in daemon.
//!
//! Reads N snd-aloop substream pairs (one per music renderer), sums
//! them sample-wise, and publishes the result to CamillaDSP over Ring A,
//! the SPSC shared-memory ring that is this daemon's only transport
//! toward the DSP (ADR-0100). No ALSA playback PCM is opened. This is
//! the production renderer topology; the old renderer-side dmix path
//! was retired after AirPlay burst testing exposed timing drops.
//!
//! This file is the entry point. Module layout:
//!   - `config`   — JASPER_FANIN_* env var parsing.
//!   - `watchdog` — progress-sentinel heartbeat (sd_notify pattern).
//!   - `mixer`    — ALSA read/sum/write loop.
//!   - `state`    — UDS STATUS endpoint for /state aggregation.
//!   - `xrun_log` — append-only ring of xrun events at
//!                  /var/lib/jasper/fanin/xrun_history.jsonl.
//!
//! The mux preempt path means simultaneous sources should not happen
//! in steady state. If future measurement shows audible source-handover
//! clicks, add ramping in the mixer with tests and doctor visibility.

mod config;
mod host_clock;
mod impulse_tap;
mod json;
mod lane_resampler;
mod loudness;
mod mixer;
mod playout;
mod source_notify;
mod state;
mod tts;
mod watchdog;
mod xrun_log;

use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::mpsc::channel;
use std::sync::Arc;

use anyhow::{Context, Result};
use jasper_tts_protocol::assistant_reference::{self, DaemonHooks};
use log::{error, info, warn};

use crate::config::Config;
use crate::mixer::Mixer;
use crate::state::{StateServer, StateServerConfig};
use crate::tts::{spawn_tts_server, tts_channels, TtsInput};
use crate::watchdog::Heartbeat;
use crate::xrun_log::XrunLog;

/// Stack bytes for every helper thread here. `mlockall(MCL_CURRENT|MCL_FUTURE)`
/// populates and pins a thread's WHOLE stack, so Rust's 2 MiB default costs
/// 2 MiB of unswappable RAM per thread; the mixer loop runs on `main`, not here.
pub(crate) const HELPER_STACK_BYTES: usize = 512 * 1024;

/// Exit code for a CONFIG-validation failure (sysexits.h EX_CONFIG).
/// The unit pairs it with `RestartPreventExitStatus=78`: a fail-closed config
/// rejection PARKS the unit failed (visible on /state + doctor) instead of
/// crash-looping — restarting cannot fix bad config, and on this unit the loop
/// escalates to `StartLimitAction=reboot` after five starts in five minutes.
/// See ADR-0141; fan-in carries only the exit-78 half.
const EXIT_CONFIG: i32 = 78;

/// Marker attached (as an `anyhow` context layer) to an error whose cause is
/// CONFIG-class. It covers the Ring A geometry declaration — every rejection
/// in `Config::from_env`'s ring block (an unparseable
/// `JASPER_FANIN_RING_WIRE_FORMAT`, an out-of-range `JASPER_FANIN_RING_SLOTS`,
/// a period that would shear a slot), all of which are pure env parsing, plus
/// the CONFIG-CLASS SUBSET of what `RingWriter::create_or_attach` can fail
/// with. None of those is repairable by restarting, so [`main`] downcasts for
/// the marker and exits [`EXIT_CONFIG`].
///
/// The marker is deliberately narrow. Fan-in's other startup failures (a
/// missing snd-aloop substream, a de-enumerated USB DAC) DO clear on a retry,
/// which is exactly what `Restart=on-failure` is for; marking every config
/// error would park the speaker on a transient hardware blip.
///
/// The ring open is the one site where both classes arrive through a single
/// call, so it does NOT tag blindly: `mixer::ring_open_error` gates the marker
/// on `io::ErrorKind::{InvalidInput, InvalidData}` — the two kinds
/// `jasper_ring` sets for a bad geometry declaration and an attach mismatch.
/// A held open-lock (`WouldBlock`) or an unapplied tmpfs permission
/// (`PermissionDenied`) is transient and keeps the restart ladder.
/// `only_config_class_ring_open_errors_park_the_unit` pins both directions.
#[derive(Debug)]
pub struct ConfigClassError;

impl std::fmt::Display for ConfigClassError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "config-class startup fault (park, do not restart-loop)")
    }
}

impl std::error::Error for ConfigClassError {}

/// [`EXIT_CONFIG`] when `error` carries the [`ConfigClassError`] marker
/// ANYWHERE in its context chain, `None` otherwise. anyhow walks the whole
/// chain, so the marker survives the `.context(...)` layers the call sites add
/// above it on the way out.
fn config_class_exit_code(error: &anyhow::Error) -> Option<i32> {
    if error.downcast_ref::<ConfigClassError>().is_some() {
        Some(EXIT_CONFIG)
    } else {
        None
    }
}

/// Exit [`EXIT_CONFIG`] for a config-class error; return normally otherwise so
/// every other failure keeps the ordinary `Restart=on-failure` path.
fn park_on_config_class(error: &anyhow::Error) {
    if let Some(code) = config_class_exit_code(error) {
        // Both remediations are named, because "fix the config and restart" is
        // only right for HALF of this class. A stale ring file on tmpfs is not
        // a config value anyone can edit — an operator who only re-reads the
        // env file finds nothing wrong and restarts into the same park.
        error!(
            "event=fanin.config_park exit={} detail={:#} — the unit does not \
             restart on config errors (RestartPreventExitStatus=78). If the \
             detail names an env value (JASPER_FANIN_RING_WIRE_FORMAT / \
             _RING_SLOTS / a slot-shearing period), fix it and \
             `systemctl restart jasper-fanin`. If it names a ring header or \
             geometry MISMATCH, the ring file on disk was built against a \
             different geometry: `rm /dev/shm/jts-ring/program.ring` (or \
             reboot — /dev/shm is tmpfs), then restart",
            code, error,
        );
        std::process::exit(code);
    }
}

fn main() -> Result<()> {
    let result = run();
    // A config-class fault anywhere in startup or the work loop exits
    // EX_CONFIG so the unit parks rather than climbing the restart burst into
    // StartLimitAction=reboot. Placed here, around the whole daemon, so every
    // `?` inside `run` is covered by one decision site.
    if let Err(e) = &result {
        park_on_config_class(e);
    }
    result
}

fn run() -> Result<()> {
    // env_logger reads JASPER_FANIN_LOG_LEVEL (or RUST_LOG as a fallback
    // for cargo-run dev) and defaults to "info" so structured event=
    // lines land in journald at priority info. Timestamps to seconds —
    // journald owns precise timestamps; we just need ordering hints
    // for `cargo run` dev sessions.
    env_logger::Builder::from_env(
        env_logger::Env::default().filter_or("JASPER_FANIN_LOG_LEVEL", "info"),
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
        "event=fanin.config_loaded inputs={} sample_rate={} period_frames={} input_buffer_frames={}",
        config.input_pcms.len(),
        config.sample_rate,
        config.period_frames,
        config.input_buffer_frames,
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
        .stack_size(HELPER_STACK_BYTES)
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

    let (tts_input, tts_metrics, assistant_reference_writer) =
        if let Some(socket_path) = &config.tts_socket_path {
            let hooks = DaemonHooks {
                event_prefix: "fanin",
                writer_stack_bytes: HELPER_STACK_BYTES,
                info: |message| info!("{message}"),
                warn: |message| warn!("{message}"),
            };
            let assistant_reference = assistant_reference::load(
                std::path::Path::new(&config.assistant_reference_path),
                hooks,
            );
            let (assistant_reference_tx, assistant_reference_writer) =
                match assistant_reference::spawn_writer(
                    PathBuf::from(&config.assistant_reference_path),
                    assistant_reference,
                    hooks,
                ) {
                    Ok((tx, handle)) => (Some(tx), Some(handle)),
                    Err(error) => {
                        warn!(
                            "event=fanin.assistant_reference.writer_unavailable path={} detail={}",
                            config.assistant_reference_path, error
                        );
                        (None, None)
                    }
                };
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
                    cue_duck_db: config.tts_cue_duck_db,
                    assistant_loudness: config.assistant_loudness,
                    assistant_reference,
                    assistant_reference_tx,
                }),
                Some(metrics),
                assistant_reference_writer,
            )
        } else {
            info!("event=fanin.tts_socket.disabled");
            (None, None, None)
        };

    // The ASSISTANT wire this box will accept without converting — the reader's
    // half of the pair `jasper-voice` publishes as `event=tts_wire.resolved`.
    // Emitted unconditionally (even with the TTS socket disabled) and with BOTH
    // inputs named, so "which half made this narrow?" is answerable from one
    // line. Two lines side by side answer "is a mismatch converting right now"
    // without waiting for a payload to trigger the mismatch warn — which by
    // design fires at most once for the daemon's lifetime and may already have
    // scrolled out of the journal window.
    info!("{}", config.assistant_wire_resolved_line());

    // Open ALSA: N input PCMs + 1 output PCM. Every configured input is
    // required in the production fan-in topology; a missing lane means
    // one renderer can silently play without entering the summed music
    // reference.
    let mut mixer = Mixer::new(&config, xrun_tx, tts_input).context("opening ALSA PCMs")?;
    info!(
        "event=fanin.mixer.ready inputs_opened={} (of {} configured)",
        mixer.input_count(),
        config.input_pcms.len(),
    );
    let source_notify_signals = mixer.source_notify_signals();

    // Impulse-tap writer thread (C4) — the SINGLE JSONL writer for the USB
    // DIRECT ingress tap. Default-disarmed, so this thread idles at a 100 ms
    // wakeup until a TAP_ARM verb arrives. Spawned regardless of the direct
    // flag (the tap simply never fires when direct is off); it costs one idle
    // thread and no resident work when disarmed.
    let tap_state = mixer.direct_tap_state();
    let tap_config = mixer.direct_tap_config();
    let tap_receiver = mixer
        .take_direct_tap_receiver()
        // PANIC-AUDITED: startup-only, taken once right after Mixer::new and before READY=1
        .expect("tap receiver taken exactly once");
    let tap_shutdown = Arc::clone(&shutdown);
    let tap_writer = std::thread::Builder::new()
        .name("fanin-tap-writer".into())
        .stack_size(HELPER_STACK_BYTES)
        .spawn(move || {
            crate::impulse_tap::run_tap_writer(tap_receiver, tap_state, tap_config, tap_shutdown);
        })
        .context("spawning fanin-tap-writer thread")?;

    // USB source-flow wake adapter. It samples the direct lane's existing
    // frame counter on a non-audio helper thread and sends edge-only hints to
    // mux. Mux owns policy and patrol recovery; fan-in never selects a source.
    // Spawn before mlockall, like every other helper thread in this process.
    let source_notify_thread = source_notify_signals
        .map(|signals| {
            let mux_socket = PathBuf::from("/run/jasper-mux/control.sock");
            let notify_shutdown = Arc::clone(&shutdown);
            std::thread::Builder::new()
                .name("fanin-source-notify".into())
                .stack_size(HELPER_STACK_BYTES)
                .spawn(move || {
                    crate::source_notify::run(signals, &mux_socket, notify_shutdown);
                })
                .context("spawning fanin-source-notify thread")
        })
        .transpose()?;

    // Combo-mode host-slaved USB clock (C5). DEFAULT-OFF. When
    // JASPER_FANIN_HOST_CLOCK=enabled AND USB DIRECT is armed, a dedicated
    // `fanin-host-clock` thread steers the gadget's Capture Pitch ctl toward the
    // DAC clock (the shared jasper_host_clock ladder). The direct-off gate is
    // resolved HERE so a `enabled`+direct-off box logs one noop line and never
    // opens the ctl (the process that owns direct gadget capture also owns its
    // pitch control).
    //
    // The STATUS fragment Arc is created regardless (initialized to the
    // disabled block) so /state carries a definite host_clock from boot; the
    // thread updates it once per tick when armed.
    // The servo runs only when host-clock AND USB-direct are both armed
    // (`host_clock_servo_armed`).
    let host_clock_enabled_effective = config.host_clock_servo_armed();
    if config.host_clock_enabled && !host_clock_enabled_effective {
        // enabled + direct-off: inert, one warn, zero ctl writes. No process
        // owns direct gadget capture or its clock in this state.
        warn!("event=fanin.host_clock.noop reason=usb_direct_off");
    }
    // The setpoint is the resampler's HELD target (target + warmup cushion),
    // shared with the inner RateController (C4). Fall back to the config sum
    // when the mixer has no direct-lane resampler (signals absent) — the config
    // is inert then anyway, but keep the fragment coherent.
    let host_clock_signals = mixer.host_clock_signals();
    // The INITIAL setpoint that seeds the config / disabled-fragment. Once the
    // servo thread runs it re-pins the setpoint each tick from the resampler's
    // LIVE held-target gauge (single source of truth — tracks the cushion decay),
    // so this is only the boot-time seed (the ceiling, before any decay).
    let host_clock_setpoint = host_clock_signals
        .as_ref()
        .map(|s| {
            s.held_target_frames
                .load(std::sync::atomic::Ordering::Relaxed)
        })
        .unwrap_or_else(|| {
            u64::from(config.input_resampler_target_frames)
                + u64::from(config.input_resampler_warmup_cushion_frames)
        });
    let host_clock_config = crate::host_clock::build_config(
        host_clock_enabled_effective,
        config.host_clock_probe_ppm,
        host_clock_setpoint,
    );
    let host_clock_fragment = Arc::new(std::sync::Mutex::new(crate::host_clock::initial_fragment(
        host_clock_config,
    )));
    // Spawn the servo thread ONLY when armed AND the mixer exposes the direct
    // lane's signals. `enabled` without signals (resampler construction failed
    // → fail-soft) or direct-off both fall through to inert: no thread, the
    // fragment stays the disabled block. Spawned BEFORE lock_memory() per the
    // mlockall/pthread-stack ordering contract documented below.
    let host_clock_thread = match (host_clock_enabled_effective, host_clock_signals) {
        (true, Some(signals)) => {
            // The ctl card the servo thread opens — computed here (cheap string
            // op) and moved in, because the `AlsaPitchCtl` handle is `!Send` and
            // must be constructed inside the thread.
            let ctl_card = crate::host_clock::ctl_card_for_device(&config.usb_direct_device);
            let hc_fragment = Arc::clone(&host_clock_fragment);
            let hc_shutdown = Arc::clone(&shutdown);
            info!(
                "event=fanin.host_clock.armed probe_ppm={} correction_probe_secs={} setpoint_frames={}",
                config.host_clock_probe_ppm,
                jasper_host_clock::CORRECTION_PROBE_STEP_SECS,
                host_clock_setpoint,
            );
            Some(
                std::thread::Builder::new()
                    .name("fanin-host-clock".into())
                    .stack_size(HELPER_STACK_BYTES)
                    .spawn(move || {
                        crate::host_clock::run_host_clock_thread(
                            host_clock_config,
                            signals,
                            ctl_card,
                            hc_fragment,
                            hc_shutdown,
                        );
                    })
                    .context("spawning fanin-host-clock thread")?,
            )
        }
        (true, None) => {
            warn!("event=fanin.host_clock.noop reason=no_direct_resampler");
            // The fragment was seeded from the effective (enabled) config, but no
            // thread will ever update it here — so overwrite it with the DISABLED
            // rendering, or `/state.audio_graph.fanin.host_clock.enabled` would
            // read `true` forever while nothing runs (review N4). Build a disabled
            // config with the same setpoint/knobs so only `enabled` flips.
            let disabled = crate::host_clock::build_config(
                false,
                config.host_clock_probe_ppm,
                host_clock_setpoint,
            );
            *host_clock_fragment
                .lock()
                .unwrap_or_else(|p| p.into_inner()) = crate::host_clock::initial_fragment(disabled);
            None
        }
        (false, _) => None,
    };

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
        StateServerConfig {
            socket_path: PathBuf::from(&config.control_socket_path),
            sample_rate: config.sample_rate,
            period_frames: config.period_frames,
            input_buffer_frames: config.input_buffer_frames,
            tts_metrics,
            host_clock_fragment: Arc::clone(&host_clock_fragment),
        },
    );
    let state_server_shutdown = Arc::clone(&shutdown);
    let state_thread = std::thread::Builder::new()
        .name("fanin-state-server".into())
        .stack_size(HELPER_STACK_BYTES)
        .spawn(move || {
            if let Err(e) = state_server.run(&state_server_shutdown) {
                error!("event=fanin.state_server.failed detail={:#}", e);
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
    if let Some(handle) = assistant_reference_writer {
        let _ = handle.join();
    }
    let _ = state_thread.join();
    let _ = xrun_writer.join();
    let _ = tap_writer.join();
    if let Some(handle) = source_notify_thread {
        let _ = handle.join();
    }
    // Join the host-clock thread last: its loop exited on the shutdown flag and
    // forced a neutral pitch write on the way out (the neutrality invariant),
    // so by the time we return the host is un-slaved. `None` when the feature
    // was off / inert (no thread was spawned).
    if let Some(handle) = host_clock_thread {
        let _ = handle.join();
    }

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
    let rc = unsafe { libc::mlockall(libc::MCL_CURRENT | libc::MCL_FUTURE) };
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

#[cfg(test)]
mod tests {
    use super::*;

    /// The park decision must survive the `.context(...)` layers the real call
    /// sites stack ABOVE the marker. `Mixer::new`'s ring error is wrapped twice
    /// on the way out ("opening fan-in→camilla SHM ring …" then "opening ALSA
    /// PCMs"), so a downcast that only inspected the outermost error would miss
    /// it and the daemon would restart-loop into a reboot instead of parking.
    #[test]
    fn config_class_marker_survives_the_real_context_depth() {
        let error = anyhow::Error::new(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "ring geometry mismatch: sample_format 2 != 1",
        ))
        .context(ConfigClassError)
        .context("opening fan-in→camilla SHM ring /dev/shm/jts-ring/program.ring")
        .context("opening ALSA PCMs");
        assert_eq!(config_class_exit_code(&error), Some(EXIT_CONFIG));
    }

    /// A config-parse failure carries the marker straight off
    /// `Config::from_env`, with no extra context layers.
    #[test]
    fn unparseable_ring_wire_format_is_config_class() {
        let error = crate::config::RingWireFormat::from_env_value(Some("S24_3LE"))
            .expect_err("an unsupported wire token must fail loud");
        assert_eq!(config_class_exit_code(&error), Some(EXIT_CONFIG));
    }

    /// The marker is narrow on purpose: the transient hardware faults that
    /// `Restart=on-failure` exists to ride out must NOT park the unit.
    #[test]
    fn ordinary_startup_errors_keep_the_restart_path() {
        let error = anyhow::Error::new(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "No such file or directory (os error 2)",
        ))
        .context("opening ALSA PCMs");
        assert_eq!(config_class_exit_code(&error), None);
    }

    /// Drive the REAL ring-open classifier over the `io::ErrorKind`s
    /// `RingWriter::create_or_attach` can actually return, and assert which
    /// ones park.
    ///
    /// This is the guard on the narrowing. An earlier revision tagged EVERY
    /// `create_or_attach` failure config-class, which parks the speaker's audio
    /// on faults that clear themselves — a `.open.lock` still held by the
    /// outgoing daemon (`WouldBlock`), or a tmpfs directory whose
    /// mode/group systemd-tmpfiles has not applied yet (`PermissionDenied`).
    /// Both cases park a unit that `Restart=on-failure` would have recovered in
    /// 5 s, and the park is silent until someone reads `/state`.
    ///
    /// The scenarios are the ones the review probed on a real ring: a stale
    /// wide ring file (`InvalidData`, parks — correct), a held `.open.lock`
    /// (`WouldBlock`, restarts), and an unwritable ring directory
    /// (`PermissionDenied`, restarts).
    #[test]
    fn only_config_class_ring_open_errors_park_the_unit() {
        use crate::mixer::ring_open_error;

        // PARK: no restart can repair either of these.
        for (kind, detail) in [
            // A ring file created against a different geometry (stale wide ring
            // surviving a deploy; a conf.d and daemon that disagree).
            (
                std::io::ErrorKind::InvalidData,
                "ring header sample_format mismatch: file has 2, expected 1",
            ),
            // The geometry fan-in built from its own env is invalid.
            (
                std::io::ErrorKind::InvalidInput,
                "ring n_slots 1 out of range 2..=64",
            ),
        ] {
            let error = ring_open_error(
                "/dev/shm/jts-ring/program.ring",
                "S32_LE",
                std::io::Error::new(kind, detail),
            );
            assert_eq!(
                config_class_exit_code(&error),
                Some(EXIT_CONFIG),
                "{kind:?} is config-class and must park the unit at exit 78"
            );
        }

        // RESTART: transient, and the restart ladder is exactly what clears
        // them. A park here is a silent outage over a self-healing fault.
        for (kind, detail) in [
            (
                std::io::ErrorKind::WouldBlock,
                "another process holds the ring open lock",
            ),
            (
                std::io::ErrorKind::PermissionDenied,
                "Permission denied (os error 13)",
            ),
            (std::io::ErrorKind::StorageFull, "No space left on device"),
            (std::io::ErrorKind::OutOfMemory, "Cannot allocate memory"),
            (std::io::ErrorKind::AlreadyExists, "File exists"),
        ] {
            let error = ring_open_error(
                "/dev/shm/jts-ring/program.ring",
                "S16_LE",
                std::io::Error::new(kind, detail),
            );
            assert_eq!(
                config_class_exit_code(&error),
                None,
                "{kind:?} is transient and must keep Restart=on-failure, not park"
            );
        }
    }
}
