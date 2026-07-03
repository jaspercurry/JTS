// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! UDS STATUS endpoint — surfaces daemon state for the
//! `/state` aggregator (`jasper-control`) and `jasper-doctor`.
//!
//! Listens on a Unix domain socket (default
//! `/run/jasper-fanin/control.sock`). Accepts one command per
//! connection:
//!
//!   `STATUS\n`  → responds with a JSON snapshot, closes connection.
//!   `SELECT <label>\n` → pass only one renderer lane to the sum.
//!   `AUTO\n`    → return to summing all active lanes.
//!   `NONE\n`    → pass no renderer lanes (correction/test still passes).
//!
//! Other input is rejected with `{"error": "unknown command"}`.
//!
//! ## Design choices
//!
//! - **UDS, not HTTP.** Lower overhead, no port conflict, easier to
//!   permission-gate to the `pi` group, idiomatic for local-only
//!   IPC. Matches `jasper-voice`'s control socket pattern.
//!
//! - **Single-shot connection.** Each connection serves one command
//!   then closes. Keeps the implementation tiny (~80 LOC of socket
//!   code) and avoids long-lived connections eating file descriptors
//!   if a client misbehaves.
//!
//! - **Hand-rolled JSON on the STATUS emit side, no `serde`.** Same
//!   reasoning as `xrun_log.rs`: the response shape is small and
//!   stable, so `snapshot_json` builds it by hand. (`serde_json` IS a
//!   crate dependency now — `impulse_tap.rs`'s `TapConfig::from_arm_body`
//!   needs the value model to PARSE an untrusted arm-body object — but
//!   this STATUS response deliberately does not use it: hand-rolling one
//!   fixed emit shape stays trivial and keeps the response path
//!   allocation-light.)
//!
//! - **Shared atomic counters.** The mixer's `frames_written`,
//!   per-input `frames_read`, `xrun_count` are already
//!   `Arc<AtomicU64>` (see `mixer.rs`). The state server clones the
//!   Arcs at construction; both threads see the same atomic without
//!   locking. Reading on the state-server side is `Relaxed`-ordered
//!   (same as the writes in the work loop) — staleness across
//!   threads is fine; the operator viewing `/state` doesn't care
//!   about a few-millisecond skew.
//!
//! - **Best-effort cleanup.** On startup, we unlink any existing
//!   socket file at the path (left behind by a crashed previous
//!   instance). On shutdown, we don't bother — systemd's
//!   `RuntimeDirectory=` cleans up the whole runtime dir on stop.
//!
//! - **Shutdown via `set_nonblocking` + poll.** The accept loop
//!   sets a 500ms timeout, checks the shutdown flag between accepts,
//!   so SIGTERM is honored within ~500ms.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use log::{info, warn};

use crate::impulse_tap::{TapConfig, TapState};
use crate::lane_resampler::LaneResamplerObservability;
use crate::mixer::{
    CouplingObservability, DirectObservability, Mixer, TrimControl, OUTPUT_DELAY_UNAVAILABLE,
};
use crate::tts::TtsMetrics;
use crate::watchdog::Heartbeat;

/// Read timeout on accepted connections. Defends against a client
/// connecting then not sending anything (would otherwise pin the
/// server thread).
const CONNECTION_READ_TIMEOUT: Duration = Duration::from_secs(2);

/// Poll interval for the accept loop, used to honor shutdown without
/// blocking indefinitely in `accept()`.
const ACCEPT_POLL_INTERVAL: Duration = Duration::from_millis(500);

/// Bound on how long a `TRIM` command waits for the mixer work loop to consume
/// the armed `pending` flag and publish the dropped-frame delta. The work loop
/// clears the flag at its next period boundary (~2.7–5.3 ms per period at
/// 128–256 frames, 48 kHz), so 200 ms is dozens of periods of slack — enough to
/// ride out a scheduling stall — while staying well under the connection read
/// timeout (2 s) so a stuck reply can never pin the server thread. On timeout
/// the reply is an honest `ERR` naming how much was dropped so far.
const TRIM_WAIT_TIMEOUT: Duration = Duration::from_millis(200);

/// Poll granularity while waiting for the work loop to service a `TRIM`. Short
/// enough to return promptly once the flag clears (a trim usually completes in
/// one period), long enough that the poll loop is not a busy-spin.
const TRIM_WAIT_POLL: Duration = Duration::from_millis(1);

pub struct StateServer {
    /// Process start instant — for uptime in the snapshot.
    started_at: Instant,
    /// Path to the UDS socket file.
    socket_path: PathBuf,
    /// Per-input state (shared with the mixer).
    inputs: Vec<InputSnapshotSource>,
    /// Output state (shared with the mixer).
    output_pcm: String,
    output_frames_written: Arc<AtomicU64>,
    output_xrun_count: Arc<AtomicU64>,
    output_delay_frames: Arc<AtomicU64>,
    /// Coupling transport echo + (under transport_pipe) the shared pipe
    /// counters. Cloned from the mixer so STATUS reads the same atomics the work
    /// loop writes.
    coupling: CouplingObservability,
    /// Music-only side-output (multi-room sync tap) — shared with the
    /// mixer. `None` pcm = solo speaker (tap disabled).
    music_output_pcm: Option<String>,
    music_frames_written: Arc<AtomicU64>,
    music_output_drops: Arc<AtomicU64>,
    selected_input_index: Arc<AtomicI32>,
    /// Watchdog handle for the heartbeat metrics.
    heartbeat: Arc<Heartbeat>,
    /// Echo of config knobs in the snapshot.
    sample_rate: u32,
    period_frames: u32,
    input_buffer_frames: u32,
    output_buffer_frames: u32,
    tts_metrics: Option<TtsMetrics>,
    /// Shared impulse-tap state (armed + counters + knobs), cloned from the
    /// mixer's `DirectTapHook` (C4). Served on `TAP_ARM`/`TAP_DISARM`/STATUS.
    tap: Arc<TapState>,
    /// Last-armed tap config, published on `TAP_ARM` for the writer thread and
    /// read on STATUS (C4).
    tap_config: Arc<Mutex<TapConfig>>,
    /// The tap's fixed sample rate (48 kHz) — passed to `TapState::arm` to
    /// resolve the refractory window into frames. Distinct from `sample_rate`
    /// (the fan-in mix rate); the direct gadget capture is a fixed 48 kHz
    /// endpoint.
    tap_sample_rate: u32,
    /// The combo-mode host-clock STATUS fragment (C7). Rendered once per tick by
    /// the `fanin-host-clock` thread into this shared string, embedded verbatim
    /// in `snapshot_json`. Always present (initialized to the disabled block by
    /// `main`), so the top-level `host_clock` key is stable whether the feature
    /// is armed or off. The state-server thread only READS it (single writer is
    /// the host-clock thread).
    host_clock_fragment: Arc<Mutex<String>>,
}

pub struct InputSnapshotSource {
    pub label: String,
    pub pcm_name: String,
    /// `true` on the USB DIRECT lane (STATUS `source:"direct"`); every other
    /// lane is `source:"lane"` (C7).
    pub is_direct: bool,
    /// USB DIRECT observability for the STATUS `direct{}` block (C7). `Some`
    /// only on the direct lane; `None` (and absent from STATUS) otherwise.
    pub direct: Option<DirectObservability>,
    pub frames_read: Arc<AtomicU64>,
    pub xrun_count: Arc<AtomicU64>,
    /// Cumulative frames discarded by the bounded catch-up resync on this
    /// lane (mixer's `drain_input_excess`). 0 on DAC-locked lanes; growing
    /// only on a free-running lane (the USB host-clock lane).
    pub catchup_resync_frames: Arc<AtomicU64>,
    /// Cumulative catch-up resync events (high-water crossings) on this lane.
    pub catchup_events: Arc<AtomicU64>,
    /// OPTIONAL per-input adaptive-resampler observability. `Some` only on the
    /// configured clock-crossing lane when the DEFAULT-OFF input resampler is
    /// armed; `None` (and absent from STATUS) for every lane otherwise.
    pub resampler: Option<LaneResamplerObservability>,
    /// Per-lane TRIM control + counters, shared with the mixer work thread. The
    /// `TRIM` command sets `pending` here; the work loop performs the drain and
    /// bumps `trims` / `trimmed_frames`, which STATUS surfaces.
    pub trim: Arc<TrimControl>,
}

pub struct StateServerConfig {
    pub socket_path: PathBuf,
    pub sample_rate: u32,
    pub period_frames: u32,
    pub input_buffer_frames: u32,
    pub output_buffer_frames: u32,
    pub output_pcm: String,
    pub music_output_pcm: Option<String>,
    pub tts_metrics: Option<TtsMetrics>,
    /// The combo-mode host-clock STATUS fragment (C7), created and initialized
    /// (to the disabled block) by `main` and updated by the `fanin-host-clock`
    /// thread. Always present so STATUS carries a definite top-level
    /// `host_clock` key.
    pub host_clock_fragment: Arc<Mutex<String>>,
}

impl StateServer {
    pub fn new(mixer: &Mixer, heartbeat: Arc<Heartbeat>, config: StateServerConfig) -> Self {
        let StateServerConfig {
            socket_path,
            sample_rate,
            period_frames,
            input_buffer_frames,
            output_buffer_frames,
            output_pcm,
            music_output_pcm,
            tts_metrics,
            host_clock_fragment,
        } = config;
        let inputs = mixer
            .inputs()
            .iter()
            .map(|inp| InputSnapshotSource {
                label: inp.label.clone(),
                pcm_name: inp.pcm_name.clone(),
                is_direct: inp.is_direct(),
                direct: inp.direct_observability(),
                frames_read: Arc::clone(&inp.frames_read),
                xrun_count: Arc::clone(&inp.xrun_count),
                catchup_resync_frames: Arc::clone(&inp.catchup_resync_frames),
                catchup_events: Arc::clone(&inp.catchup_events),
                resampler: inp.resampler_observability(),
                trim: inp.trim_control(),
            })
            .collect();
        Self {
            started_at: Instant::now(),
            socket_path,
            inputs,
            output_pcm,
            output_frames_written: Arc::clone(&mixer.frames_written),
            output_xrun_count: Arc::clone(&mixer.output_xrun_count),
            output_delay_frames: Arc::clone(&mixer.output_delay_frames),
            coupling: mixer.coupling.clone(),
            music_output_pcm,
            music_frames_written: Arc::clone(&mixer.music_frames_written),
            music_output_drops: Arc::clone(&mixer.music_output_drops),
            selected_input_index: mixer.selected_input_index(),
            heartbeat,
            sample_rate,
            period_frames,
            input_buffer_frames,
            output_buffer_frames,
            tts_metrics,
            tap: mixer.direct_tap_state(),
            tap_config: mixer.direct_tap_config(),
            // The direct gadget capture is a fixed 48 kHz endpoint; the tap's
            // refractory window resolves against that, not the mix rate.
            tap_sample_rate: 48_000,
            host_clock_fragment,
        }
    }

    /// Run the accept loop. Blocks until `shutdown` is set; intended
    /// to be run on a dedicated thread.
    pub fn run(&self, shutdown: &AtomicBool) -> Result<()> {
        // Ensure parent dir exists. systemd's RuntimeDirectory=
        // handles this in production; for local dev / tests we create.
        if let Some(parent) = self.socket_path.parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                warn!(
                    "event=fanin.state_server.parent_dir_create_failed path={} detail={}",
                    parent.display(),
                    e
                );
            }
        }

        // Unlink any leftover socket from a crashed previous instance.
        // bind() would otherwise return -EADDRINUSE.
        let _ = std::fs::remove_file(&self.socket_path);

        let listener = UnixListener::bind(&self.socket_path)
            .with_context(|| format!("binding UDS socket at {}", self.socket_path.display()))?;
        listener
            .set_nonblocking(true)
            .context("set_nonblocking on listener")?;

        info!(
            "event=fanin.state_server.listening socket={}",
            self.socket_path.display()
        );

        while !shutdown.load(Ordering::Relaxed) {
            match listener.accept() {
                Ok((stream, _)) => {
                    if let Err(e) = self.handle_connection(stream) {
                        warn!("event=fanin.state_server.handle_failed detail={:#}", e);
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(ACCEPT_POLL_INTERVAL);
                }
                Err(e) => {
                    warn!("event=fanin.state_server.accept_failed detail={}", e);
                    std::thread::sleep(ACCEPT_POLL_INTERVAL);
                }
            }
        }

        let _ = std::fs::remove_file(&self.socket_path);
        info!("event=fanin.state_server.stopped");
        Ok(())
    }

    fn handle_connection(&self, mut stream: UnixStream) -> Result<()> {
        stream
            .set_read_timeout(Some(CONNECTION_READ_TIMEOUT))
            .context("set_read_timeout on connection")?;

        let mut reader = BufReader::new(stream.try_clone()?);
        let mut command = String::new();
        reader
            .read_line(&mut command)
            .context("reading command from connection")?;

        let command = command.trim();
        let response = match command {
            "STATUS" => self.snapshot_json(),
            "AUTO" => {
                let previous = self.selected_input_index.swap(-1, Ordering::Relaxed);
                if previous != -1 {
                    info!("event=fanin.source_select selected=auto");
                }
                self.snapshot_json()
            }
            "NONE" => {
                let previous = self.selected_input_index.swap(-2, Ordering::Relaxed);
                if previous != -2 {
                    info!("event=fanin.source_select selected=none");
                }
                self.snapshot_json()
            }
            cmd if cmd.starts_with("SELECT ") => {
                let label = cmd.trim_start_matches("SELECT ").trim();
                self.select_input_json(label)
            }
            "TRIM" => self.trim_command(None),
            cmd if cmd.starts_with("TRIM ") => {
                let label = cmd.trim_start_matches("TRIM ").trim();
                self.trim_command(Some(label))
            }
            "TAP_DISARM" => self.tap_disarm_command(),
            cmd if cmd.starts_with("TAP_ARM ") => {
                // Everything after the verb is the arm JSON body (rest of line).
                let body = cmd.trim_start_matches("TAP_ARM ").trim();
                self.tap_arm_command(body)
            }
            other => format!(
                r#"{{"error":"unknown command","received":"{}"}}"#,
                escape_json(other),
            ),
        };

        stream
            .write_all(response.as_bytes())
            .context("writing response")?;
        stream.write_all(b"\n").ok();
        Ok(())
    }

    fn select_input_json(&self, label: &str) -> String {
        if label.is_empty() {
            return r#"{"error":"missing input label"}"#.to_string();
        }
        if let Some((idx, input)) = self
            .inputs
            .iter()
            .enumerate()
            .find(|(_, input)| input.label == label)
        {
            let previous = self
                .selected_input_index
                .swap(idx as i32, Ordering::Relaxed);
            if previous != idx as i32 {
                info!("event=fanin.source_select selected={}", input.label);
            }
            self.snapshot_json()
        } else {
            format!(
                r#"{{"error":"unknown input label","label":"{}"}}"#,
                escape_json(label),
            )
        }
    }

    /// Handle a `TRIM` / `TRIM <label>` control command.
    ///
    /// `TRIM` (label `None`) requests a trim on EVERY lane; `TRIM <label>`
    /// targets one. Because the state-server thread cannot touch the `!Sync`
    /// capture `PCM` or the mixer-owned `LaneResampler` (both live on the mixer
    /// work thread), this sets each target lane's `pending` flag with a
    /// `Release` store and then briefly polls the lane's `trimmed_frames`
    /// counter for the work loop to consume the flag and publish the delta —
    /// reporting the frames ACTUALLY dropped from the resampler ring, not just
    /// "the request was queued." Mirrors the SELECT/AUTO/NONE split (control
    /// sets a shared atomic; the work loop does the state-owning work).
    ///
    /// Reply is a plain-text line: `OK trimmed=<frames_dropped>` (summed across
    /// targeted lanes) or `ERR <reason>`. Distinct from SELECT's JSON snapshot
    /// because a trim is a fire-and-report action, not a state query — the
    /// operator wants the one number back on the socket immediately. A lane
    /// with no armed resampler (the reservoir lives only in the resampler ring)
    /// clears its flag and reports 0 dropped — a documented no-op, not an error.
    fn trim_command(&self, label: Option<&str>) -> String {
        // Resolve the target lane set. An empty explicit label is an error;
        // `None` (bare `TRIM`) means all lanes.
        let targets: Vec<&InputSnapshotSource> = match label {
            Some("") => return "ERR missing input label".to_string(),
            Some(l) => match self.inputs.iter().find(|inp| inp.label == l) {
                Some(inp) => vec![inp],
                None => return format!("ERR unknown input label {}", l),
            },
            None => self.inputs.iter().collect(),
        };

        // Snapshot each target's cumulative trimmed_frames, arm the pending
        // flag, then wait (bounded) for the work loop to advance the counter.
        // The delta across all targets is the frames actually dropped.
        let before: Vec<u64> = targets
            .iter()
            .map(|inp| {
                let prev = inp.trim.trimmed_frames.load(Ordering::Relaxed);
                // Release so the work loop's Acquire swap observes the request.
                inp.trim.pending.store(true, Ordering::Release);
                prev
            })
            .collect();

        // Poll for the work loop to consume every armed flag. A trim that drops
        // 0 frames (lane already at target, or unarmed) still clears its pending
        // flag, so we wait on the flags clearing, not on the counter moving — a
        // legitimate 0-frame trim must not spin out the whole timeout.
        let deadline = Instant::now() + TRIM_WAIT_TIMEOUT;
        loop {
            let all_consumed = targets
                .iter()
                .all(|inp| !inp.trim.pending.load(Ordering::Acquire));
            if all_consumed || Instant::now() >= deadline {
                break;
            }
            std::thread::sleep(TRIM_WAIT_POLL);
        }

        let dropped: u64 = targets
            .iter()
            .zip(&before)
            .map(|(inp, &prev)| {
                inp.trim
                    .trimmed_frames
                    .load(Ordering::Relaxed)
                    .saturating_sub(prev)
            })
            .sum();

        // If a flag never cleared, the work loop didn't run within the window
        // (daemon paused / no periods). Report honestly rather than claim 0.
        let stuck = targets
            .iter()
            .any(|inp| inp.trim.pending.load(Ordering::Acquire));
        if stuck {
            info!(
                "event=fanin.trim.request result=timeout label={} dropped_so_far={}",
                label.unwrap_or("all"),
                dropped,
            );
            return format!(
                "ERR trim not serviced within {}ms (mixer loop idle?) dropped_so_far={}",
                TRIM_WAIT_TIMEOUT.as_millis(),
                dropped,
            );
        }

        info!(
            "event=fanin.trim.request result=ok label={} lanes={} dropped_frames={}",
            label.unwrap_or("all"),
            targets.len(),
            dropped,
        );
        format!("OK trimmed={}", dropped)
    }

    /// Handle `TAP_ARM {json}` (C4). Parses the remainder of the line with the
    /// bridge's `TapConfig::from_arm_body` (same keys, same validation/ceilings,
    /// same `/run/jasper-fanin/` path constraint), truncates the JSONL file
    /// synchronously so the reply only claims success once the file is a clean
    /// slate, publishes the config for the writer thread, then flips armed via
    /// `TapState::arm` (armed-before-generation ordering preserved). Reply is a
    /// plaintext line like `TRIM` — `OK armed path=<path>` or `ERR <reason>`.
    ///
    /// The state-server thread touches only shared atomics/the config mutex here
    /// (never the mixer-owned detector), mirroring the TRIM plumbing shape.
    fn tap_arm_command(&self, body: &str) -> String {
        let Some(cfg) = TapConfig::from_arm_body(body) else {
            return "ERR bad arm params".to_string();
        };
        // Truncate the JSONL synchronously so a fresh arm always starts clean.
        if let Err(e) = truncate_tap_file(&cfg.path) {
            warn!(
                "event=fanin.tap_arm_error detail={} path={}",
                e,
                cfg.path.display()
            );
            return format!("ERR cannot open path {}", cfg.path.display());
        }
        // Publish config for the writer thread first, then flip armed.
        {
            let mut guard = self
                .tap_config
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            *guard = cfg.clone();
        }
        self.tap.arm(&cfg, self.tap_sample_rate, epoch_millis());
        info!(
            "event=fanin.tap_armed threshold={:.3} hysteresis={:.3} refractory_ms={} max_events={} auto_disarm_min={} path={}",
            cfg.threshold,
            cfg.hysteresis,
            cfg.refractory_ms,
            cfg.max_events,
            cfg.auto_disarm_min,
            cfg.path.display(),
        );
        format!("OK armed path={}", cfg.path.display())
    }

    /// Handle `TAP_DISARM` (C4). Idempotent; reply names the per-arm counters.
    fn tap_disarm_command(&self) -> String {
        self.tap.disarm();
        let written = self.tap.events_written();
        let dropped = self.tap.events_dropped();
        info!(
            "event=fanin.tap_disarmed events_written={} events_dropped={}",
            written, dropped,
        );
        format!(
            "OK disarmed events_written={} events_dropped={}",
            written, dropped,
        )
    }

    /// Build the JSON snapshot. Reads each atomic with Relaxed
    /// ordering — same as the writes in the work loop. Across
    /// threads we accept a few-millisecond skew; an operator viewing
    /// `/state` doesn't care about sample-accurate counters.
    pub fn snapshot_json(&self) -> String {
        let mut buf = String::with_capacity(512);
        buf.push('{');

        // uptime_seconds (float, two decimals)
        let uptime = self.started_at.elapsed();
        push_kv_f64(
            &mut buf,
            "uptime_seconds",
            (uptime.as_millis() as f64) / 1000.0,
            2,
        );
        buf.push(',');

        // input_buffer_frames (all configured inputs use the same ALSA
        // buffer size)
        push_kv_u64(
            &mut buf,
            "input_buffer_frames",
            self.input_buffer_frames as u64,
        );
        buf.push(',');

        // selected_input: null in auto mode, otherwise the selected
        // label. NONE also renders null; selection_mode distinguishes
        // it from auto. Invalid values should not happen (only SELECT
        // can set non-negative indices), but render null if a future
        // version changes the input list under us.
        buf.push_str(r#""selection_mode":"#);
        let selected = self.selected_input_index.load(Ordering::Relaxed);
        if selected == -1 {
            buf.push_str(r#""auto""#);
        } else if selected == -2 {
            buf.push_str(r#""none""#);
        } else {
            buf.push_str(r#""select""#);
        }
        buf.push(',');

        buf.push_str(r#""selected_input":"#);
        if selected >= 0 {
            if let Some(input) = self.inputs.get(selected as usize) {
                buf.push('"');
                buf.push_str(&escape_json(&input.label));
                buf.push('"');
            } else {
                buf.push_str("null");
            }
        } else {
            buf.push_str("null");
        }
        buf.push(',');

        // inputs array
        buf.push_str(r#""inputs":["#);
        for (i, input) in self.inputs.iter().enumerate() {
            if i > 0 {
                buf.push(',');
            }
            buf.push('{');
            push_kv_str(&mut buf, "label", &input.label);
            buf.push(',');
            push_kv_str(&mut buf, "pcm", &input.pcm_name);
            buf.push(',');
            // source: "direct" on the USB DIRECT lane (reads hw:UAC2Gadget
            // directly), "lane" on every aloop-reading lane. Always present,
            // additive (the TRIM-block precedent) — C7.
            push_kv_str(
                &mut buf,
                "source",
                if input.is_direct { "direct" } else { "lane" },
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "frames_read",
                input.frames_read.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "xrun_count",
                input.xrun_count.load(Ordering::Relaxed),
            );
            buf.push(',');
            // Catch-up resync counters (mixer's drain_input_excess). Both
            // stay 0 on a DAC-locked lane; a growing pair is the operator's
            // "this lane is free-running and we're drop-resyncing it" signal
            // (today only the USB host-clock lane). Never escalated.
            push_kv_u64(
                &mut buf,
                "catchup_resync_frames",
                input.catchup_resync_frames.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "catchup_events",
                input.catchup_events.load(Ordering::Relaxed),
            );
            buf.push(',');
            // TRIM counters (the standing-fill one-shot drop). `trims` is how
            // many TRIMs actually dropped ≥1 frame on this lane; `trimmed_frames`
            // is the cumulative total dropped from the resampler ring; `pending`
            // shows an armed but not-yet-serviced request. All 0/false on a lane
            // never trimmed (including every unarmed lane), so the shape is
            // stable for the common case. Always present (unlike the optional
            // resampler block) — a flat, greppable pair like the catch-up
            // counters above.
            buf.push_str(r#""trim":{"#);
            push_kv_u64(&mut buf, "trims", input.trim.trims.load(Ordering::Relaxed));
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "trimmed_frames",
                input.trim.trimmed_frames.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_bool(
                &mut buf,
                "pending",
                input.trim.pending.load(Ordering::Relaxed),
            );
            buf.push('}');
            // OPTIONAL per-input adaptive resampler (DEFAULT-OFF). Rendered as a
            // nested object only when armed on this lane — absent for every lane
            // when the feature is off, so the default STATUS shape is unchanged.
            if let Some(r) = &input.resampler {
                buf.push(',');
                buf.push_str(r#""resampler":{"#);
                push_kv_bool(&mut buf, "armed", r.armed);
                buf.push(',');
                push_kv_bool(&mut buf, "locked", r.locked.load(Ordering::Relaxed));
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "input_frames",
                    r.input_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "output_frames",
                    r.output_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "silence_frames",
                    r.silence_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "overrun_frames",
                    r.overrun_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                // Stored as i64 milli-ppm in a u64 atomic; reinterpret + scale
                // back to ppm for display.
                let ratio_ppm = (r.ratio_milli_ppm.load(Ordering::Relaxed) as i64) as f64 / 1000.0;
                push_kv_f64(&mut buf, "ratio_ppm", ratio_ppm, 2);
                buf.push(',');
                // Live ring fill (current) vs. the configured hold target — the
                // operator's "the resampler engaged and is tracking" proof: a
                // fill_frames steady near target_fill_frames = locked & holding.
                push_kv_u64(
                    &mut buf,
                    "fill_frames",
                    r.fill_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                // The static acquisition ceiling (target + full cushion) — the
                // snap-back target, unchanged shape for backward compat.
                push_kv_u64(&mut buf, "target_fill_frames", r.target_fill_frames);
                buf.push(',');
                // The LIVE held target the controller (and the outer DLL) hold
                // the fill toward RIGHT NOW — equal to target_fill_frames unless
                // the DEFAULT-OFF post-lock cushion decay has lowered it. This is
                // the single-source-of-truth setpoint; watch it descend from the
                // ceiling toward the decay floor when decay is engaged.
                push_kv_u64(
                    &mut buf,
                    "held_target_frames",
                    r.held_target_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                // Post-lock cushion-decay state (all inert while decay is off):
                // active = actively decaying; floor = the configured decay floor;
                // frozen_reason = why decay is paused ("" while actively decaying,
                // else unlocked / not_l0 / cascade / warmup / at_floor).
                buf.push_str(r#""decay":{"#);
                push_kv_bool(&mut buf, "active", r.decay_active.load(Ordering::Relaxed));
                buf.push(',');
                push_kv_u64(&mut buf, "floor_frames", r.decay_floor_frames);
                buf.push(',');
                push_kv_str(
                    &mut buf,
                    "frozen_reason",
                    crate::lane_resampler::DecayFrozenReason::code_str(
                        r.decay_frozen_reason.load(Ordering::Relaxed),
                    ),
                );
                buf.push('}');
                buf.push(',');
                push_kv_u64(&mut buf, "lock_count", r.lock_count.load(Ordering::Relaxed));
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "unlock_count",
                    r.unlock_count.load(Ordering::Relaxed),
                );
                // OPTIONAL host-compliance persistence block (prime-at-floor).
                // Rendered only when the DEFAULT-OFF feature is armed on this lane;
                // absent otherwise (byte-identical STATUS shape). flag_present =
                // a persisted proof is believed present; proved_at = its epoch-s
                // timestamp (0 when absent); revoked_reason_last = the last
                // one-strike revoke reason ("" until one happens).
                if let Some(c) = &r.compliance {
                    buf.push(',');
                    buf.push_str(r#""compliance":{"#);
                    push_kv_bool(
                        &mut buf,
                        "flag_present",
                        c.flag_present.load(Ordering::Relaxed),
                    );
                    buf.push(',');
                    push_kv_u64(
                        &mut buf,
                        "proved_at",
                        c.proved_at_epoch_s.load(Ordering::Relaxed),
                    );
                    buf.push(',');
                    push_kv_str(
                        &mut buf,
                        "revoked_reason_last",
                        crate::host_compliance::revoke_reason_code_str(
                            c.revoked_reason_last_code.load(Ordering::Relaxed),
                        ),
                    );
                    buf.push('}');
                }
                buf.push('}');
            }
            // OPTIONAL USB DIRECT block (C7). Rendered only on the direct lane
            // (mirrors the optional `resampler` block): device, live presence,
            // cumulative opens/retries. Absent for every aloop lane, so the
            // default STATUS shape is unchanged.
            if let Some(d) = &input.direct {
                buf.push(',');
                buf.push_str(r#""direct":{"#);
                push_kv_str(&mut buf, "device", &d.device);
                buf.push(',');
                push_kv_bool(&mut buf, "present", d.present.load(Ordering::Relaxed));
                buf.push(',');
                push_kv_u64(&mut buf, "opens", d.opens.load(Ordering::Relaxed));
                buf.push(',');
                push_kv_u64(&mut buf, "retries", d.retries.load(Ordering::Relaxed));
                buf.push(',');
                // Negotiated gadget geometry (lever 2). 256/768 by default;
                // JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES overrides the period and
                // resolve_direct_buffer_frames derives the requested deep buffer.
                // buffer_frames is the ACTUALLY-negotiated hwp.get_buffer_size()
                // (the kernel may round the near-request up), read live so STATUS
                // matches the running PCM rather than the request.
                push_kv_u64(&mut buf, "period_frames", d.period_frames as u64);
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "buffer_frames",
                    d.buffer_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                // drain_avail{} — since-boot drain-ENTRY avail dwell stats
                // (lever 2). `mean`/`max` in frames; `hist` is a fixed 6-bucket
                // 64-frame-step histogram (boundaries [0,64,128,192,256,320,+]).
                // Additive sub-block; absent for every non-direct lane.
                let s = &d.drain_stats;
                let count = s.count.load(Ordering::Relaxed);
                let sum = s.sum.load(Ordering::Relaxed);
                let mean = if count == 0 {
                    0.0
                } else {
                    (sum as f64) / (count as f64)
                };
                buf.push_str(r#""drain_avail":{"#);
                push_kv_u64(&mut buf, "count", count);
                buf.push(',');
                push_kv_f64(&mut buf, "mean", mean, 1);
                buf.push(',');
                push_kv_u64(&mut buf, "max", s.max.load(Ordering::Relaxed));
                buf.push(',');
                buf.push_str(r#""hist":["#);
                for (i, bucket) in s.hist.iter().enumerate() {
                    if i > 0 {
                        buf.push(',');
                    }
                    buf.push_str(&bucket.load(Ordering::Relaxed).to_string());
                }
                buf.push(']');
                buf.push('}');
                buf.push('}');
            }
            buf.push('}');
        }
        buf.push(']');
        buf.push(',');

        // output object
        buf.push_str(r#""output":{"#);
        push_kv_str(&mut buf, "pcm", &self.output_pcm);
        buf.push(',');
        push_kv_u64(&mut buf, "sample_rate", self.sample_rate as u64);
        buf.push(',');
        push_kv_u64(&mut buf, "period_frames", self.period_frames as u64);
        buf.push(',');
        push_kv_u64(&mut buf, "buffer_frames", self.output_buffer_frames as u64);
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "frames_written",
            self.output_frames_written.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "xrun_count",
            self.output_xrun_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        let output_delay_frames = match self.output_delay_frames.load(Ordering::Relaxed) {
            OUTPUT_DELAY_UNAVAILABLE => None,
            frames => Some(frames),
        };
        push_kv_u64_opt(&mut buf, "snd_pcm_delay_frames", output_delay_frames);
        buf.push(',');
        push_kv_f64_opt(
            &mut buf,
            "snd_pcm_delay_ms",
            output_delay_frames.map(|frames| (frames as f64) * 1000.0 / (self.sample_rate as f64)),
            3,
        );
        buf.push(',');

        // coupling transport echo. `transport:"loopback"` (default) carries no
        // pipe block — byte-identical observability to the pre-coupling daemon.
        // `transport:"transport_pipe"` adds a `pipe` block with the shared-capture pipe
        // path, the requested + kernel-resolved pipe size, and the reopen /
        // dropped-period counters (a growing dropped_periods while CamillaDSP is
        // up means the shared capture is starving — jasper-doctor's actionable
        // signal). actual_pipe_bytes is 0 when the write end is not currently
        // open (reader absent / CamillaDSP reloading).
        push_kv_str(&mut buf, "transport", self.coupling.transport);
        if let Some(pipe) = &self.coupling.pipe {
            buf.push(',');
            buf.push_str(r#""pipe":{"#);
            push_kv_str(&mut buf, "path", &pipe.path);
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "requested_pipe_bytes",
                pipe.requested_pipe_bytes as u64,
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "actual_pipe_bytes",
                pipe.actual_pipe_bytes.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "reopen_count",
                pipe.reopen_count.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "dropped_periods",
                pipe.dropped_periods.load(Ordering::Relaxed),
            );
            buf.push('}');
        }
        // Ring A (shm_ring): the SPSC SHM ring counter block. `occupancy` is the
        // live write_seq-read_seq depth; `published` slots reached a live reader;
        // `full_waits` is the bounded live-reader back-pressure count; `drops`
        // folds no-reader + stuck-reader drops; `mirror_frames` / `mirror_drops`
        // are the lossy aloop side-tap's written-frame and drop counts (never
        // load-bearing; parity with music_output's frames_written/drops). Only
        // present under shm_ring — byte-identical observability to today under
        // loopback.
        if let Some(ring) = &self.coupling.ring {
            buf.push(',');
            buf.push_str(r#""ring":{"#);
            push_kv_str(&mut buf, "path", &ring.path);
            buf.push(',');
            push_kv_u64(&mut buf, "slots", ring.slots as u64);
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "occupancy",
                ring.occupancy.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "published",
                ring.published.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "full_waits",
                ring.full_waits.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(&mut buf, "drops", ring.drops.load(Ordering::Relaxed));
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "mirror_frames",
                ring.mirror_frames.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "mirror_drops",
                ring.mirror_drops.load(Ordering::Relaxed),
            );
            buf.push('}');
        }
        buf.push('}');
        buf.push(',');

        // music_output object — the multi-room sync tap (off on a solo
        // speaker). `enabled:false` with no further fields when unconfigured;
        // when configured, `drops` growing => the snapserver consumer is behind.
        buf.push_str(r#""music_output":{"#);
        match &self.music_output_pcm {
            Some(pcm) => {
                push_kv_bool(&mut buf, "enabled", true);
                buf.push(',');
                push_kv_str(&mut buf, "pcm", pcm);
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "frames_written",
                    self.music_frames_written.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "drops",
                    self.music_output_drops.load(Ordering::Relaxed),
                );
            }
            None => {
                push_kv_bool(&mut buf, "enabled", false);
            }
        }
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""tts":{"#);
        match &self.tts_metrics {
            Some(metrics) => {
                push_kv_bool(&mut buf, "enabled", true);
                buf.push(',');
                push_kv_u64(&mut buf, "pending_frames", metrics.pending_frames());
                buf.push(',');
                push_kv_u64(&mut buf, "max_pending_frames", metrics.max_pending_frames());
                buf.push(',');
                push_kv_u64(&mut buf, "budget_frames", metrics.budget_frames());
                buf.push(',');
                push_kv_u64(&mut buf, "dropped_commands", metrics.dropped_commands());
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "dropped_audio_frames",
                    metrics.dropped_audio_frames(),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "stale_commands_dropped",
                    metrics.stale_commands_dropped(),
                );
                buf.push(',');
                push_kv_u64(&mut buf, "flush_requests", metrics.flush_requests());
                buf.push(',');
                push_kv_u64(&mut buf, "flushed_frames", metrics.flushed_frames());
                buf.push(',');
                push_kv_bool(
                    &mut buf,
                    "program_duck_active",
                    metrics.program_duck_active(),
                );
                buf.push(',');
                buf.push_str(r#""assistant_loudness":{"#);
                let loudness = metrics.loudness_snapshot();
                push_kv_f64_opt(
                    &mut buf,
                    "content_short_lufs",
                    loudness.content_short_lufs,
                    1,
                );
                buf.push(',');
                push_kv_f64_opt(
                    &mut buf,
                    "content_anchor_lufs",
                    loudness.content_anchor_lufs,
                    1,
                );
                buf.push(',');
                push_kv_bool(&mut buf, "decision_seen", loudness.decision_seen);
                buf.push(',');
                push_kv_bool(&mut buf, "calibrated", loudness.calibrated);
                buf.push(',');
                push_kv_f64(
                    &mut buf,
                    "profile_confidence",
                    loudness.profile_confidence,
                    2,
                );
                buf.push(',');
                push_kv_f64_opt(&mut buf, "baseline_lufs", loudness.baseline_lufs, 1);
                buf.push(',');
                push_kv_f64_opt(&mut buf, "target_lufs", loudness.target_lufs, 1);
                buf.push(',');
                push_kv_f64_opt(&mut buf, "source_lufs", loudness.source_lufs, 1);
                buf.push(',');
                push_kv_f64_opt(&mut buf, "source_peak_dbfs", loudness.source_peak_dbfs, 1);
                buf.push(',');
                push_kv_f64_opt(&mut buf, "requested_gain_db", loudness.requested_gain_db, 1);
                buf.push(',');
                push_kv_f64_opt(&mut buf, "peak_cap_gain_db", loudness.peak_cap_gain_db, 1);
                buf.push(',');
                push_kv_f64_opt(&mut buf, "final_gain_db", loudness.final_gain_db, 1);
                buf.push('}');
            }
            None => {
                push_kv_bool(&mut buf, "enabled", false);
            }
        }
        buf.push('}');
        buf.push(',');

        // tap object (C4) — always present. The impulse tap over the USB DIRECT
        // ingress: armed state, per-arm counters, the live threshold/refractory,
        // the JSONL path. `armed:false` (the default) is the byte-stable shape
        // for the common case. Rendered from the bridge's `status_fragment`
        // verbatim so the two surfaces agree.
        let tap_cfg = {
            let guard = self
                .tap_config
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            guard.clone()
        };
        buf.push_str(r#""tap":"#);
        buf.push_str(&self.tap.status_fragment(&tap_cfg));
        buf.push(',');

        // host_clock object (C7) — always present, additive. In combo mode
        // (JASPER_FANIN_USB_DIRECT + JASPER_FANIN_HOST_CLOCK) fan-in owns the
        // gadget capture and drives the host-slaved USB clock, so this is the
        // combo-box analogue of usbsink's state.json host_clock block (solo
        // mode). Rendered verbatim from the fragment the `fanin-host-clock`
        // thread publishes each tick; the disabled block (enabled:false) when
        // the feature is off, so the top-level key is byte-stable either way.
        buf.push_str(r#""host_clock":"#);
        {
            let guard = self
                .host_clock_fragment
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            buf.push_str(&guard);
        }
        buf.push(',');

        // watchdog object
        buf.push_str(r#""watchdog":{"#);
        push_kv_u64(&mut buf, "pings_sent", self.heartbeat.pings_sent());
        buf.push(',');
        push_kv_u64(&mut buf, "pings_skipped", self.heartbeat.pings_skipped());
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "last_progress_age_ms",
            self.heartbeat.last_progress_age_ms(),
        );
        buf.push('}');

        buf.push('}');
        buf
    }
}

// ---- tap helpers -----------------------------------------------------

/// Truncate (or create) the tap JSONL artifact under its tmpfs dir on arm, so
/// the reply reflects a clean slate. The writer thread holds the append handle
/// after this; here we only create the parent + zero the file. Mirrors the
/// bridge's `truncate_tap_file`.
fn truncate_tap_file(path: &std::path::Path) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, b"")
}

/// Wall-clock ms since the epoch — used only for the tap's observability-only
/// `auto_disarm_at_epoch_ms` (enforcement uses the writer's monotonic clock).
fn epoch_millis() -> u64 {
    match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_millis() as u64,
        Err(_) => 0,
    }
}

// ---- JSON helpers ----------------------------------------------------

fn push_kv_str(buf: &mut String, key: &str, value: &str) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":""#);
    buf.push_str(&escape_json(value));
    buf.push('"');
}

fn push_kv_u64(buf: &mut String, key: &str, value: u64) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    buf.push_str(&value.to_string());
}

fn push_kv_u64_opt(buf: &mut String, key: &str, value: Option<u64>) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    match value {
        Some(value) => buf.push_str(&value.to_string()),
        None => buf.push_str("null"),
    }
}

fn push_kv_bool(buf: &mut String, key: &str, value: bool) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    buf.push_str(if value { "true" } else { "false" });
}

fn push_kv_f64(buf: &mut String, key: &str, value: f64, decimals: usize) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    buf.push_str(&format!("{:.*}", decimals, value));
}

fn push_kv_f64_opt(buf: &mut String, key: &str, value: Option<f64>, decimals: usize) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    match value {
        Some(value) => buf.push_str(&format!("{:.*}", decimals, value)),
        None => buf.push_str("null"),
    }
}

fn escape_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_test_server() -> StateServer {
        // Test-only: the direct fixture builds a DrainStats. Scoped here (not a
        // module-level import) so a non-test build doesn't carry an unused import.
        use crate::mixer::DrainStats;
        StateServer {
            started_at: Instant::now(),
            socket_path: PathBuf::from("/tmp/fanin-test.sock"),
            inputs: vec![
                InputSnapshotSource {
                    label: "spotify".to_string(),
                    pcm_name: "hw:Loopback,1,0".to_string(),
                    // Ordinary aloop lane → source:"lane", no direct block.
                    is_direct: false,
                    direct: None,
                    frames_read: Arc::new(AtomicU64::new(12345)),
                    xrun_count: Arc::new(AtomicU64::new(0)),
                    catchup_resync_frames: Arc::new(AtomicU64::new(0)),
                    catchup_events: Arc::new(AtomicU64::new(0)),
                    // No resampler armed on this lane (the default).
                    resampler: None,
                    // Never-trimmed lane: all counters 0, no pending request.
                    trim: Arc::new(TrimControl::test_fixture(0, 0, false)),
                },
                InputSnapshotSource {
                    label: "airplay".to_string(),
                    pcm_name: "hw:Loopback,1,1".to_string(),
                    is_direct: false,
                    direct: None,
                    frames_read: Arc::new(AtomicU64::new(0)),
                    xrun_count: Arc::new(AtomicU64::new(2)),
                    catchup_resync_frames: Arc::new(AtomicU64::new(1536)),
                    catchup_events: Arc::new(AtomicU64::new(2)),
                    // A lane WITH an armed resampler (fixture only — exercises
                    // the STATUS rendering path). ratio = +120 ppm → 120000
                    // milli-ppm stored in the u64 atomic.
                    resampler: Some(LaneResamplerObservability {
                        armed: true,
                        locked: Arc::new(AtomicBool::new(true)),
                        input_frames: Arc::new(AtomicU64::new(48000)),
                        output_frames: Arc::new(AtomicU64::new(47988)),
                        silence_frames: Arc::new(AtomicU64::new(256)),
                        overrun_frames: Arc::new(AtomicU64::new(0)),
                        ratio_milli_ppm: Arc::new(AtomicU64::new(120_000)),
                        lock_count: Arc::new(AtomicU64::new(1)),
                        unlock_count: Arc::new(AtomicU64::new(0)),
                        // Held near target (520 vs 512) — the "engaged & tracking"
                        // shape STATUS surfaces.
                        fill_frames: Arc::new(AtomicU64::new(520)),
                        target_fill_frames: 512,
                        // Decay INACTIVE on this fixture (held == ceiling); frozen
                        // reason code 0 → "" (actively-decaying rendering).
                        held_target_frames: Arc::new(AtomicU64::new(512)),
                        decay_active: Arc::new(AtomicBool::new(false)),
                        decay_floor_frames: 0,
                        decay_frozen_reason: Arc::new(AtomicU64::new(0)),
                        // No compliance persistence on this (non-direct) fixture
                        // lane — the block is absent, matching a decay-off lane.
                        compliance: None,
                    }),
                    // A lane that HAS been trimmed (fixture): 3 trims, 4608 frames
                    // dropped total, no request currently pending.
                    trim: Arc::new(TrimControl::test_fixture(3, 4608, false)),
                },
                InputSnapshotSource {
                    // A USB DIRECT lane fixture (source:"direct" + a direct{}
                    // block). Its audio comes from hw:UAC2Gadget, not an aloop
                    // substream (pcm name kept for parity with the label).
                    label: "usbsink".to_string(),
                    pcm_name: "hw:Loopback,1,3".to_string(),
                    is_direct: true,
                    direct: Some(DirectObservability {
                        device: "hw:UAC2Gadget".to_string(),
                        period_frames: 256,
                        buffer_frames: Arc::new(AtomicU64::new(768)),
                        present: Arc::new(AtomicBool::new(true)),
                        opens: Arc::new(AtomicU64::new(1)),
                        retries: Arc::new(AtomicU64::new(0)),
                        // Two drain-entry samples (128 + 192 = 320, mean 160,
                        // max 192) exercising buckets 2 and 3.
                        drain_stats: {
                            let s = DrainStats::new();
                            s.count.store(2, Ordering::Relaxed);
                            s.sum.store(320, Ordering::Relaxed);
                            s.max.store(192, Ordering::Relaxed);
                            s.hist[2].store(1, Ordering::Relaxed);
                            s.hist[3].store(1, Ordering::Relaxed);
                            s
                        },
                    }),
                    frames_read: Arc::new(AtomicU64::new(96000)),
                    xrun_count: Arc::new(AtomicU64::new(0)),
                    catchup_resync_frames: Arc::new(AtomicU64::new(0)),
                    catchup_events: Arc::new(AtomicU64::new(0)),
                    // Direct lanes always own a resampler; a minimal armed fixture.
                    resampler: Some(LaneResamplerObservability {
                        armed: true,
                        locked: Arc::new(AtomicBool::new(true)),
                        input_frames: Arc::new(AtomicU64::new(96000)),
                        output_frames: Arc::new(AtomicU64::new(95900)),
                        silence_frames: Arc::new(AtomicU64::new(0)),
                        overrun_frames: Arc::new(AtomicU64::new(0)),
                        ratio_milli_ppm: Arc::new(AtomicU64::new(0)),
                        lock_count: Arc::new(AtomicU64::new(1)),
                        unlock_count: Arc::new(AtomicU64::new(0)),
                        // ACTIVELY DECAYING fixture: the held target (1024) has
                        // dropped below the acquisition ceiling (2560) toward the
                        // floor (544), exercising the decay STATUS block's live
                        // path (active:true, frozen_reason:"").
                        fill_frames: Arc::new(AtomicU64::new(1024)),
                        target_fill_frames: 2560,
                        held_target_frames: Arc::new(AtomicU64::new(1024)),
                        decay_active: Arc::new(AtomicBool::new(true)),
                        decay_floor_frames: 544,
                        decay_frozen_reason: Arc::new(AtomicU64::new(0)),
                        // Host-compliance persistence ARMED fixture: a proof is
                        // present (proved_at set, no revoke yet), exercising the
                        // STATUS `compliance` block's populated path.
                        compliance: Some(crate::host_compliance::HostComplianceObservability::new(
                            true,
                            1_700_000_000,
                        )),
                    }),
                    trim: Arc::new(TrimControl::test_fixture(0, 0, false)),
                },
            ],
            output_pcm: "hw:Loopback,0,7".to_string(),
            output_frames_written: Arc::new(AtomicU64::new(98765)),
            output_xrun_count: Arc::new(AtomicU64::new(1)),
            output_delay_frames: Arc::new(AtomicU64::new(1024)),
            coupling: CouplingObservability {
                transport: "loopback",
                pipe: None,
                ring: None,
            },
            music_output_pcm: Some("hw:Loopback,0,6".to_string()),
            music_frames_written: Arc::new(AtomicU64::new(54321)),
            music_output_drops: Arc::new(AtomicU64::new(3)),
            selected_input_index: Arc::new(AtomicI32::new(-1)),
            heartbeat: Arc::new(Heartbeat::new()),
            sample_rate: 48000,
            period_frames: 256,
            input_buffer_frames: 4096,
            output_buffer_frames: 2048,
            tts_metrics: Some(TtsMetrics::new(96_000)),
            tap: Arc::new(TapState::default()),
            tap_config: Arc::new(Mutex::new(TapConfig::default())),
            tap_sample_rate: 48_000,
            // The disabled host-clock fragment — the common (feature-off) shape
            // STATUS always carries. `crate::host_clock::initial_fragment` on a
            // disabled config renders exactly this.
            host_clock_fragment: Arc::new(Mutex::new(crate::host_clock::initial_fragment(
                crate::host_clock::build_config(false, 300, 6, 2048),
            ))),
        }
    }

    fn make_pipe_test_server() -> StateServer {
        use crate::mixer::PipeObservability;
        let mut server = make_test_server();
        server.coupling = CouplingObservability {
            transport: "transport_pipe",
            pipe: Some(PipeObservability {
                path: "/run/jasper-fanin/camilla.pipe".to_string(),
                requested_pipe_bytes: 8192,
                reopen_count: Arc::new(AtomicU64::new(2)),
                dropped_periods: Arc::new(AtomicU64::new(7)),
                actual_pipe_bytes: Arc::new(AtomicU64::new(8192)),
            }),
            ring: None,
        };
        server
    }

    fn make_ring_test_server() -> StateServer {
        use crate::mixer::RingObservability;
        let mut server = make_test_server();
        server.coupling = CouplingObservability {
            transport: "shm_ring",
            pipe: None,
            ring: Some(RingObservability {
                path: "/dev/shm/jts-ring/program.ring".to_string(),
                slots: 8,
                occupancy: Arc::new(AtomicU64::new(6)),
                published: Arc::new(AtomicU64::new(12345)),
                full_waits: Arc::new(AtomicU64::new(9)),
                drops: Arc::new(AtomicU64::new(4)),
                mirror_frames: Arc::new(AtomicU64::new(7654)),
                mirror_drops: Arc::new(AtomicU64::new(2)),
            }),
        };
        server
    }

    #[test]
    fn snapshot_json_contains_expected_top_level_keys() {
        let server = make_test_server();
        let j = server.snapshot_json();
        for key in &[
            "uptime_seconds",
            "selection_mode",
            "selected_input",
            "inputs",
            "output",
            "music_output",
            "tts",
            "watchdog",
        ] {
            assert!(
                j.contains(&format!(r#""{}":"#, key)),
                "missing top-level key {} in snapshot: {}",
                key,
                j,
            );
        }
    }

    #[test]
    fn snapshot_json_always_carries_host_clock_block() {
        // C7: the combo-mode host-clock block is a top-level, always-present
        // sibling of `tap` — the disabled block when the feature is off, so the
        // key is byte-stable. It must parse as valid JSON (the fragment is
        // rendered by the shared crate; here we prove the fold-in is well-formed
        // and the disabled default shows through).
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(
            j.contains(r#""host_clock":{"#),
            "STATUS must always carry a top-level host_clock block: {j}"
        );
        let parsed: serde_json::Value = serde_json::from_str(&j).expect("STATUS parses");
        let hc = &parsed["host_clock"];
        assert_eq!(
            hc["enabled"].as_bool(),
            Some(false),
            "disabled fixture ⇒ enabled:false"
        );
        assert_eq!(hc["ladder"].as_str(), Some("disabled"));
        assert!(hc["probe"]["response_ratio"].is_null());
        // Sibling of tap, not nested inside it.
        assert!(parsed["tap"].is_object());
    }

    #[test]
    fn snapshot_json_embeds_an_enabled_host_clock_fragment_verbatim() {
        // When the host-clock thread publishes an armed fragment, STATUS embeds
        // it verbatim (the state-server only READS the shared string). Simulate
        // by swapping the fragment for an armed-config render.
        let mut server = make_test_server();
        server.host_clock_fragment = Arc::new(Mutex::new(crate::host_clock::initial_fragment(
            crate::host_clock::build_config(true, 300, 6, 2048),
        )));
        let j = server.snapshot_json();
        let parsed: serde_json::Value = serde_json::from_str(&j).expect("STATUS parses");
        assert_eq!(
            parsed["host_clock"]["enabled"].as_bool(),
            Some(true),
            "an armed fragment shows through verbatim: {j}"
        );
    }

    #[test]
    fn snapshot_json_per_input_fields() {
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(j.contains(r#""label":"spotify""#), "missing spotify label");
        assert!(j.contains(r#""label":"airplay""#), "missing airplay label");
        assert!(j.contains(r#""frames_read":12345"#));
        assert!(j.contains(r#""xrun_count":2"#)); // airplay
        assert!(j.contains(r#""pcm":"hw:Loopback,1,0""#));
        // Catch-up resync counters surfaced per input. spotify (DAC-locked)
        // is 0; airplay's fixture stands in for a free-running lane.
        assert!(
            j.contains(r#""catchup_resync_frames":0"#),
            "missing catchup_resync_frames=0 (spotify): {j}"
        );
        assert!(
            j.contains(r#""catchup_resync_frames":1536"#),
            "missing catchup_resync_frames=1536 (airplay fixture): {j}"
        );
        assert!(
            j.contains(r#""catchup_events":2"#),
            "missing catchup_events=2 (airplay fixture): {j}"
        );
    }

    #[test]
    fn snapshot_json_resampler_block_present_only_when_armed() {
        let server = make_test_server();
        let j = server.snapshot_json();
        // The armed lane (airplay fixture) renders a nested resampler object
        // with its counters; ratio reinterprets the milli-ppm atomic to ppm.
        assert!(
            j.contains(r#""resampler":{"#),
            "armed lane must render a resampler block: {j}"
        );
        assert!(j.contains(r#""armed":true"#), "missing armed flag: {j}");
        assert!(j.contains(r#""locked":true"#), "missing locked flag: {j}");
        assert!(
            j.contains(r#""input_frames":48000"#),
            "missing resampler input_frames: {j}"
        );
        assert!(
            j.contains(r#""ratio_ppm":120.00"#),
            "milli-ppm atomic must reinterpret to ppm: {j}"
        );
        // Live ring fill (current) + the configured hold target — the
        // engagement-proof gauge an operator reads off /state.fanin.
        assert!(
            j.contains(r#""fill_frames":520"#),
            "missing live resampler fill_frames: {j}"
        );
        assert!(
            j.contains(r#""target_fill_frames":512"#),
            "missing resampler target_fill_frames: {j}"
        );
        // The airplay fixture has decay INACTIVE (held == ceiling 512).
        assert!(
            j.contains(r#""held_target_frames":512"#),
            "missing live held_target_frames on the inactive-decay fixture: {j}"
        );
        // Every armed lane carries a decay block (inert when off). The airplay
        // fixture is not decaying: active:false, empty frozen_reason.
        assert!(
            j.contains(r#""decay":{"active":false,"floor_frames":0,"frozen_reason":""}"#),
            "missing inactive decay block on the airplay fixture: {j}"
        );
        // The direct fixture is ACTIVELY DECAYING: the held target (1024) sits
        // below its ceiling (2560) heading for the floor (544).
        assert!(
            j.contains(r#""target_fill_frames":2560"#),
            "missing the direct fixture's acquisition ceiling: {j}"
        );
        assert!(
            j.contains(r#""held_target_frames":1024"#),
            "missing the direct fixture's live (decayed) held target: {j}"
        );
        assert!(
            j.contains(r#""decay":{"active":true,"floor_frames":544,"frozen_reason":""}"#),
            "missing active decay block on the direct fixture: {j}"
        );
        assert!(
            j.contains(r#""lock_count":1"#),
            "missing resampler lock_count: {j}"
        );
        // TWO resampler blocks — the airplay fixture and the direct usbsink
        // lane both carry one; the spotify lane (None) must NOT, keeping the
        // default STATUS shape unchanged for unarmed lanes.
        assert_eq!(
            j.matches(r#""resampler":{"#).count(),
            2,
            "only the armed lanes may render a resampler block: {j}"
        );
    }

    #[test]
    fn snapshot_json_output_fields() {
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(j.contains(r#""pcm":"hw:Loopback,0,7""#));
        assert!(j.contains(r#""sample_rate":48000"#));
        assert!(j.contains(r#""frames_written":98765"#));
        assert!(j.contains(r#""snd_pcm_delay_frames":1024"#));
        assert!(j.contains(r#""snd_pcm_delay_ms":21.333"#));
    }

    #[test]
    fn snapshot_json_loopback_transport_has_no_pipe_block() {
        // Default coupling: transport=loopback, NO pipe block — byte-identical
        // observability to the pre-coupling daemon.
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(
            j.contains(r#""transport":"loopback""#),
            "missing transport: {j}"
        );
        assert!(
            !j.contains(r#""pipe":"#),
            "loopback must emit no pipe block: {j}"
        );
    }

    #[test]
    fn snapshot_json_transport_pipe_reports_pipe_observability() {
        // transport_pipe: transport + the shared pipe observability counters.
        let server = make_pipe_test_server();
        let j = server.snapshot_json();
        assert!(
            j.contains(r#""transport":"transport_pipe""#),
            "missing transport: {j}"
        );
        assert!(j.contains(r#""pipe":{"#), "missing pipe block: {j}");
        assert!(
            j.contains(r#""path":"/run/jasper-fanin/camilla.pipe""#),
            "missing pipe path: {j}"
        );
        assert!(
            j.contains(r#""requested_pipe_bytes":8192"#),
            "missing requested_pipe_bytes: {j}"
        );
        assert!(
            j.contains(r#""actual_pipe_bytes":8192"#),
            "missing actual_pipe_bytes: {j}"
        );
        assert!(
            j.contains(r#""reopen_count":2"#),
            "missing reopen_count: {j}"
        );
        assert!(
            j.contains(r#""dropped_periods":7"#),
            "missing dropped_periods: {j}"
        );
        // transport_pipe carries NO ring block.
        assert!(
            !j.contains(r#""ring":{"#),
            "transport_pipe must emit no ring block: {j}"
        );
    }

    #[test]
    fn snapshot_json_shm_ring_reports_ring_observability() {
        // shm_ring: transport + the shared ring counter block.
        let server = make_ring_test_server();
        let j = server.snapshot_json();
        assert!(
            j.contains(r#""transport":"shm_ring""#),
            "missing transport: {j}"
        );
        assert!(j.contains(r#""ring":{"#), "missing ring block: {j}");
        assert!(
            j.contains(r#""path":"/dev/shm/jts-ring/program.ring""#),
            "missing ring path: {j}"
        );
        assert!(j.contains(r#""slots":8"#), "missing slots: {j}");
        assert!(j.contains(r#""occupancy":6"#), "missing occupancy: {j}");
        assert!(j.contains(r#""published":12345"#), "missing published: {j}");
        assert!(j.contains(r#""full_waits":9"#), "missing full_waits: {j}");
        assert!(j.contains(r#""drops":4"#), "missing drops: {j}");
        assert!(
            j.contains(r#""mirror_frames":7654"#),
            "missing mirror_frames: {j}"
        );
        assert!(
            j.contains(r#""mirror_drops":2"#),
            "missing mirror_drops: {j}"
        );
        // shm_ring carries NO pipe block.
        assert!(
            !j.contains(r#""pipe":{"#),
            "shm_ring must emit no pipe block: {j}"
        );
    }

    #[test]
    fn snapshot_json_loopback_has_no_ring_block() {
        // Default coupling emits neither pipe nor ring — byte-identical to today.
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(
            !j.contains(r#""ring":{"#),
            "loopback must emit no ring block: {j}"
        );
    }

    #[test]
    fn snapshot_json_music_output_fields() {
        // Enabled (make_test_server configures the tap): pcm + counters.
        let mut server = make_test_server();
        let j = server.snapshot_json();
        assert!(
            j.contains(r#""music_output":{"enabled":true"#),
            "got: {}",
            j,
        );
        assert!(j.contains(r#""pcm":"hw:Loopback,0,6""#));
        // 54321 is the music tap's count, distinct from the output's 98765.
        assert!(j.contains(r#""frames_written":54321"#));
        assert!(j.contains(r#""drops":3"#));

        // Disabled (solo speaker): just enabled:false, no pcm/counters echoed.
        server.music_output_pcm = None;
        let j = server.snapshot_json();
        assert!(
            j.contains(r#""music_output":{"enabled":false}"#),
            "got: {}",
            j,
        );
        assert!(
            !j.contains(r#""hw:Loopback,0,6""#),
            "a disabled tap must not echo its pcm name",
        );
    }

    #[test]
    fn snapshot_json_tts_fields() {
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(j.contains(r#""tts":{"enabled":true"#));
        assert!(j.contains(r#""budget_frames":96000"#));
        assert!(j.contains(r#""stale_commands_dropped":0"#));
        assert!(j.contains(r#""program_duck_active":false"#));
        assert!(j.contains(r#""assistant_loudness":{"content_short_lufs":null"#));
        assert!(j.contains(r#""decision_seen":false"#));
        assert!(j.contains(r#""final_gain_db":null"#));
    }

    #[test]
    fn snapshot_json_reports_selected_input() {
        let server = make_test_server();
        assert!(server.snapshot_json().contains(r#""selected_input":null"#));
        assert!(server
            .snapshot_json()
            .contains(r#""selection_mode":"auto""#));
        server.selected_input_index.store(1, Ordering::Relaxed);
        assert!(server
            .snapshot_json()
            .contains(r#""selected_input":"airplay""#));
        assert!(server
            .snapshot_json()
            .contains(r#""selection_mode":"select""#));
        server.selected_input_index.store(-2, Ordering::Relaxed);
        assert!(server.snapshot_json().contains(r#""selected_input":null"#));
        assert!(server
            .snapshot_json()
            .contains(r#""selection_mode":"none""#));
    }

    #[test]
    fn select_input_json_updates_selection() {
        let server = make_test_server();
        let j = server.select_input_json("spotify");
        assert_eq!(server.selected_input_index.load(Ordering::Relaxed), 0);
        assert!(j.contains(r#""selected_input":"spotify""#));
    }

    #[test]
    fn none_command_updates_selection() {
        use std::io::{Read, Write};
        use std::net::Shutdown;
        use std::os::unix::net::UnixStream;

        let server = make_test_server();
        server.selected_input_index.store(1, Ordering::Relaxed);
        let (mut client, server_stream) = UnixStream::pair().unwrap();

        client.write_all(b"NONE\n").unwrap();
        client.shutdown(Shutdown::Write).unwrap();
        server.handle_connection(server_stream).unwrap();

        assert_eq!(server.selected_input_index.load(Ordering::Relaxed), -2,);
        let mut response = String::new();
        client.read_to_string(&mut response).unwrap();
        assert!(response.contains(r#""selection_mode":"none""#));
        assert!(response.contains(r#""selected_input":null"#));
    }

    #[test]
    fn select_input_json_rejects_unknown_label() {
        let server = make_test_server();
        let j = server.select_input_json("bluetooth");
        assert_eq!(server.selected_input_index.load(Ordering::Relaxed), -1);
        assert!(j.contains(r#""error":"unknown input label""#));
    }

    #[test]
    fn snapshot_json_is_valid_jsonlike() {
        // Quick well-formedness check: balanced braces and brackets.
        let server = make_test_server();
        let j = server.snapshot_json();
        let open_braces = j.matches('{').count();
        let close_braces = j.matches('}').count();
        assert_eq!(open_braces, close_braces, "unbalanced braces in: {}", j);
        let open_brackets = j.matches('[').count();
        let close_brackets = j.matches(']').count();
        assert_eq!(
            open_brackets, close_brackets,
            "unbalanced brackets in: {}",
            j
        );
        assert!(j.starts_with('{'));
        assert!(j.ends_with('}'));
    }

    #[test]
    fn escape_json_helper_handles_specials() {
        assert_eq!(escape_json("plain"), "plain");
        assert_eq!(escape_json(r#"a"b"#), r#"a\"b"#);
        assert_eq!(escape_json("a\nb"), "a\\nb");
    }

    // ---- TRIM STATUS shape ------------------------------------------------

    #[test]
    fn snapshot_json_per_input_trim_block() {
        let server = make_test_server();
        let j = server.snapshot_json();
        // Every lane renders a flat trim block (like the catch-up counters):
        // spotify, airplay, and the direct usbsink lane → three.
        assert_eq!(
            j.matches(r#""trim":{"#).count(),
            3,
            "each lane must render exactly one trim block: {j}"
        );
        // spotify fixture: never trimmed.
        assert!(
            j.contains(r#""trim":{"trims":0,"trimmed_frames":0,"pending":false}"#),
            "spotify trim block (never trimmed): {j}"
        );
        // airplay fixture: 3 trims, 4608 frames dropped.
        assert!(
            j.contains(r#""trim":{"trims":3,"trimmed_frames":4608,"pending":false}"#),
            "airplay trim block (fixture): {j}"
        );
    }

    // ---- TRIM command parse + dispatch ------------------------------------

    #[test]
    fn trim_command_rejects_empty_label() {
        let server = make_test_server();
        let resp = server.trim_command(Some(""));
        assert_eq!(resp, "ERR missing input label");
    }

    #[test]
    fn trim_command_rejects_unknown_label() {
        let server = make_test_server();
        let resp = server.trim_command(Some("bluetooth"));
        assert_eq!(resp, "ERR unknown input label bluetooth");
        // No flag armed on any lane.
        for inp in &server.inputs {
            assert!(!inp.trim.pending.load(Ordering::Acquire));
        }
    }

    #[test]
    fn trim_command_labeled_arms_only_the_target_and_reports_delta() {
        // Run trim_command on a background thread and service the mixer work
        // loop from THIS (main) thread. Inverting the roles removes the
        // scheduler race a background servicer had against trim_command's
        // TRIM_WAIT_TIMEOUT: under heavy CI parallelism a freshly-spawned
        // servicer could be starved past the 200 ms wall-clock bound (the flag
        // was serviced correctly but too late), a real starvation race, not a
        // logic bug. The test thread is already running, so it services the
        // armed flag the instant trim_command sets it and the reply always
        // lands. The trimmed_frames write happens-before the pending release,
        // so trim_command's Acquire load of the cleared flag sees the counter.
        let server = std::sync::Arc::new(make_test_server());
        let caller = std::sync::Arc::clone(&server);
        let handle = std::thread::spawn(move || caller.trim_command(Some("airplay")));
        // airplay is index 1. Service its flag as soon as trim_command arms it.
        let airplay = &server.inputs[1];
        loop {
            if airplay.trim.pending.load(Ordering::Acquire) {
                airplay
                    .trim
                    .trimmed_frames
                    .fetch_add(1024, Ordering::Relaxed);
                airplay.trim.pending.store(false, Ordering::Release);
                break;
            }
            if handle.is_finished() {
                break;
            }
            std::hint::spin_loop();
        }
        let resp = handle.join().unwrap();
        assert_eq!(resp, "OK trimmed=1024", "got: {resp}");
        // spotify (index 0) was never armed.
        assert!(!server.inputs[0].trim.pending.load(Ordering::Acquire));
    }

    #[test]
    fn trim_command_all_arms_every_lane_and_sums_delta() {
        // See the sibling labeled test: trim_command runs on a background thread
        // and the main (test) thread services EVERY lane's armed flag inline, so
        // the servicing can never be starved past trim_command's 200 ms
        // TRIM_WAIT_TIMEOUT on a contended CI runner. `trim_command(None)` arms
        // every lane, so this must service all of them — `done` is sized to the
        // real lane count (3: spotify/airplay/usbsink), not a hardcoded 2. Each
        // lane gets a distinct amount (512 >> i) so the summed delta is
        // unambiguous: 512 + 256 + 128 = 896.
        let server = std::sync::Arc::new(make_test_server());
        let caller = std::sync::Arc::clone(&server);
        let handle = std::thread::spawn(move || caller.trim_command(None));
        let mut done = vec![false; server.inputs.len()];
        let mut expected: u64 = 0;
        loop {
            for (i, inp) in server.inputs.iter().enumerate() {
                if !done[i] && inp.trim.pending.load(Ordering::Acquire) {
                    let amount = 512u64 >> i;
                    inp.trim.trimmed_frames.fetch_add(amount, Ordering::Relaxed);
                    inp.trim.pending.store(false, Ordering::Release);
                    done[i] = true;
                    expected += amount;
                }
            }
            if done.iter().all(|d| *d) || handle.is_finished() {
                break;
            }
            std::hint::spin_loop();
        }
        let resp = handle.join().unwrap();
        assert_eq!(resp, format!("OK trimmed={expected}"), "got: {resp}");
    }

    #[test]
    fn trim_command_times_out_when_loop_never_services() {
        // No work loop consumes the flag: bounded wait elapses, honest ERR.
        let server = make_test_server();
        let resp = server.trim_command(Some("spotify"));
        assert!(
            resp.starts_with("ERR trim not serviced within"),
            "got: {resp}"
        );
        assert!(resp.contains("dropped_so_far=0"), "got: {resp}");
    }

    #[test]
    fn trim_command_via_handle_connection_replies_plaintext() {
        // Exercise the full socket dispatch for `TRIM <label>`: the reply is the
        // plain-text OK/ERR line, not a JSON snapshot.
        use std::io::{Read, Write};
        use std::net::Shutdown;
        use std::os::unix::net::UnixStream;

        let server = make_test_server();
        let (mut client, server_stream) = UnixStream::pair().unwrap();
        client.write_all(b"TRIM spotify\n").unwrap();
        client.shutdown(Shutdown::Write).unwrap();
        // No work loop here, so spotify's flag never clears -> timeout ERR.
        server.handle_connection(server_stream).unwrap();
        let mut response = String::new();
        client.read_to_string(&mut response).unwrap();
        assert!(
            response.starts_with("ERR trim not serviced within"),
            "got: {response}"
        );
    }

    // ---- C7: source + direct{} STATUS shape --------------------------------

    #[test]
    fn snapshot_json_source_field_on_every_input() {
        let server = make_test_server();
        let j = server.snapshot_json();
        // Every lane carries a source; exactly one is "direct" (the usbsink
        // fixture), the other two are "lane".
        assert_eq!(
            j.matches(r#""source":"lane""#).count(),
            2,
            "spotify + airplay must be source:lane: {j}"
        );
        assert_eq!(
            j.matches(r#""source":"direct""#).count(),
            1,
            "only the usbsink lane is source:direct: {j}"
        );
    }

    #[test]
    fn snapshot_json_direct_block_present_only_on_the_direct_lane() {
        let server = make_test_server();
        let j = server.snapshot_json();
        assert_eq!(
            j.matches(r#""direct":{"#).count(),
            1,
            "only the direct lane renders a direct block: {j}"
        );
        assert!(
            j.contains(r#""device":"hw:UAC2Gadget""#),
            "direct block must name the gadget device: {j}"
        );
        assert!(j.contains(r#""present":true"#), "direct present flag: {j}");
        assert!(j.contains(r#""opens":1"#), "direct opens counter: {j}");
        assert!(j.contains(r#""retries":0"#), "direct retries counter: {j}");
        // The whole document still parses.
        let parsed: serde_json::Value = serde_json::from_str(&j).unwrap();
        let inputs = parsed["inputs"].as_array().unwrap();
        let direct = inputs.iter().find(|i| i["label"] == "usbsink").unwrap();
        assert_eq!(direct["source"].as_str(), Some("direct"));
        assert_eq!(direct["direct"]["present"].as_bool(), Some(true));
        // ADDITIVE (lever 2): negotiated geometry + drain-entry dwell stats.
        assert_eq!(direct["direct"]["period_frames"].as_u64(), Some(256));
        assert_eq!(direct["direct"]["buffer_frames"].as_u64(), Some(768));
        let da = &direct["direct"]["drain_avail"];
        assert_eq!(da["count"].as_u64(), Some(2));
        assert_eq!(da["max"].as_u64(), Some(192));
        // mean = 320/2 = 160.0 (serialized with one decimal).
        assert_eq!(da["mean"].as_f64(), Some(160.0));
        let hist = da["hist"].as_array().unwrap();
        assert_eq!(hist.len(), 6, "fixed 6-bucket histogram: {j}");
        assert_eq!(hist[2].as_u64(), Some(1)); // [128,192) — the 128 sample
        assert_eq!(hist[3].as_u64(), Some(1)); // [192,256) — the 192 sample
        assert_eq!(hist[0].as_u64(), Some(0));
        // A non-direct lane has no direct block.
        let spotify = inputs.iter().find(|i| i["label"] == "spotify").unwrap();
        assert!(spotify.get("direct").is_none());
        assert_eq!(spotify["source"].as_str(), Some("lane"));
    }

    // ---- C4: top-level tap{} fragment + TAP_ARM / TAP_DISARM ---------------

    #[test]
    fn snapshot_json_tap_fragment_always_present_and_disarmed_by_default() {
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(
            j.contains(r#""tap":{"#),
            "tap fragment must be present: {j}"
        );
        let parsed: serde_json::Value = serde_json::from_str(&j).unwrap();
        assert_eq!(parsed["tap"]["armed"].as_bool(), Some(false));
        assert_eq!(parsed["tap"]["events_written"].as_u64(), Some(0));
        assert_eq!(
            parsed["tap"]["path"].as_str(),
            Some("/run/jasper-fanin/impulse-tap.jsonl")
        );
    }

    #[test]
    fn tap_arm_command_arms_and_reports_path() {
        let server = make_test_server();
        // Use a tmp path inside a real dir so the synchronous truncate succeeds
        // in the test sandbox (the arm-body path constraint is /run/jasper-fanin
        // only, which the test env may not have — so we assert the OK/ERR shape
        // via the default path when the dir exists, else the ERR path).
        let resp = server.tap_arm_command(r#"{"threshold":0.2,"refractory_ms":250}"#);
        // Either the file truncated (OK armed) or the dir was absent (ERR cannot
        // open path) — both are valid plaintext replies; assert the shape.
        assert!(
            resp.starts_with("OK armed path=") || resp.starts_with("ERR cannot open path"),
            "unexpected arm reply: {resp}"
        );
        if resp.starts_with("OK armed") {
            // On success the tap is armed and STATUS reflects it.
            assert!(server.tap.armed());
            let parsed: serde_json::Value = serde_json::from_str(&server.snapshot_json()).unwrap();
            assert_eq!(parsed["tap"]["armed"].as_bool(), Some(true));
            assert!((parsed["tap"]["threshold"].as_f64().unwrap() - 0.2).abs() < 1e-3);
        }
    }

    #[test]
    fn tap_arm_command_rejects_bad_params() {
        let server = make_test_server();
        // A zero threshold is rejected by from_arm_body -> ERR, never arms.
        let resp = server.tap_arm_command(r#"{"threshold":0}"#);
        assert_eq!(resp, "ERR bad arm params");
        assert!(!server.tap.armed());
        // A path outside /run/jasper-fanin is rejected too.
        let resp = server.tap_arm_command(r#"{"path":"/etc/passwd"}"#);
        assert_eq!(resp, "ERR bad arm params");
        assert!(!server.tap.armed());
    }

    #[test]
    fn tap_disarm_command_reports_counters_and_is_idempotent() {
        let server = make_test_server();
        // Arm directly (bypassing the file truncate) then disarm.
        server
            .tap
            .arm(&TapConfig::default(), server.tap_sample_rate, 0);
        server.tap.note_written();
        server.tap.note_written();
        server.tap.note_dropped();
        let resp = server.tap_disarm_command();
        assert_eq!(resp, "OK disarmed events_written=2 events_dropped=1");
        assert!(!server.tap.armed());
        // Idempotent — a second disarm still replies OK with the counters intact.
        let resp = server.tap_disarm_command();
        assert_eq!(resp, "OK disarmed events_written=2 events_dropped=1");
    }

    #[test]
    fn tap_verbs_via_handle_connection_reply_plaintext() {
        use std::io::{Read, Write};
        use std::net::Shutdown;
        use std::os::unix::net::UnixStream;

        let server = make_test_server();
        // TAP_DISARM over the socket → plaintext OK line (not a JSON snapshot).
        let (mut client, server_stream) = UnixStream::pair().unwrap();
        client.write_all(b"TAP_DISARM\n").unwrap();
        client.shutdown(Shutdown::Write).unwrap();
        server.handle_connection(server_stream).unwrap();
        let mut response = String::new();
        client.read_to_string(&mut response).unwrap();
        assert!(response.starts_with("OK disarmed"), "got: {response}");
    }

    /// Pin the fan-in reserved tap basenames against the config defaults they
    /// mirror: control.sock / tts.sock / camilla.pipe all live under
    /// /run/jasper-fanin, and a TAP_ARM must be rejected at each. If a config
    /// default's basename ever changes, this fails loudly (the same
    /// cross-constant discipline the usbsink crate uses for its own files).
    #[test]
    fn reserved_tap_basenames_match_fanin_socket_defaults() {
        use crate::impulse_tap::{path_is_allowed, RESERVED_TAP_DIR_BASENAMES, TAP_PATH_DIR};
        // The config defaults (control_socket_path / tts_socket_path /
        // camilla_pipe_path) all resolve under TAP_PATH_DIR with these basenames.
        for basename in ["control.sock", "tts.sock", "camilla.pipe"] {
            let path = std::path::Path::new(TAP_PATH_DIR).join(basename);
            assert!(
                RESERVED_TAP_DIR_BASENAMES.contains(&basename),
                "{basename} must be reserved"
            );
            assert!(
                !path_is_allowed(&path),
                "TAP_ARM must reject fan-in's own file {}",
                path.display()
            );
        }
    }
}
