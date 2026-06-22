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
//! - **Hand-rolled JSON, no `serde`.** Same reasoning as
//!   `xrun_log.rs`: the shape is small and stable. Adding `serde`
//!   for one response shape would bring `serde_json` into the
//!   dependency graph, ~200 KB of compiled code, for marginal
//!   benefit.
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
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use log::{info, warn};

use crate::mixer::Mixer;
use crate::tts::TtsMetrics;
use crate::watchdog::Heartbeat;

/// Read timeout on accepted connections. Defends against a client
/// connecting then not sending anything (would otherwise pin the
/// server thread).
const CONNECTION_READ_TIMEOUT: Duration = Duration::from_secs(2);

/// Poll interval for the accept loop, used to honor shutdown without
/// blocking indefinitely in `accept()`.
const ACCEPT_POLL_INTERVAL: Duration = Duration::from_millis(500);

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
}

pub struct InputSnapshotSource {
    pub label: String,
    pub pcm_name: String,
    pub frames_read: Arc<AtomicU64>,
    pub xrun_count: Arc<AtomicU64>,
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
        } = config;
        let inputs = mixer
            .inputs()
            .iter()
            .map(|inp| InputSnapshotSource {
                label: inp.label.clone(),
                pcm_name: inp.pcm_name.clone(),
                frames_read: Arc::clone(&inp.frames_read),
                xrun_count: Arc::clone(&inp.xrun_count),
            })
            .collect();
        Self {
            started_at: Instant::now(),
            socket_path,
            inputs,
            output_pcm,
            output_frames_written: Arc::clone(&mixer.frames_written),
            output_xrun_count: Arc::clone(&mixer.output_xrun_count),
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
        StateServer {
            started_at: Instant::now(),
            socket_path: PathBuf::from("/tmp/fanin-test.sock"),
            inputs: vec![
                InputSnapshotSource {
                    label: "spotify".to_string(),
                    pcm_name: "hw:Loopback,1,0".to_string(),
                    frames_read: Arc::new(AtomicU64::new(12345)),
                    xrun_count: Arc::new(AtomicU64::new(0)),
                },
                InputSnapshotSource {
                    label: "airplay".to_string(),
                    pcm_name: "hw:Loopback,1,1".to_string(),
                    frames_read: Arc::new(AtomicU64::new(0)),
                    xrun_count: Arc::new(AtomicU64::new(2)),
                },
            ],
            output_pcm: "hw:Loopback,0,7".to_string(),
            output_frames_written: Arc::new(AtomicU64::new(98765)),
            output_xrun_count: Arc::new(AtomicU64::new(1)),
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
        }
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
    fn snapshot_json_per_input_fields() {
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(j.contains(r#""label":"spotify""#), "missing spotify label");
        assert!(j.contains(r#""label":"airplay""#), "missing airplay label");
        assert!(j.contains(r#""frames_read":12345"#));
        assert!(j.contains(r#""xrun_count":2"#)); // airplay
        assert!(j.contains(r#""pcm":"hw:Loopback,1,0""#));
    }

    #[test]
    fn snapshot_json_output_fields() {
        let server = make_test_server();
        let j = server.snapshot_json();
        assert!(j.contains(r#""pcm":"hw:Loopback,0,7""#));
        assert!(j.contains(r#""sample_rate":48000"#));
        assert!(j.contains(r#""frames_written":98765"#));
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
}
