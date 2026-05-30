//! Local STATUS socket for outputd observability.
//!
//! The socket mirrors the fan-in daemon's shape: one command per
//! connection, `STATUS\n` returns a compact JSON snapshot, malformed
//! commands return a JSON error. `jasper-control /state`,
//! `jasper-doctor`, and an operator can all consume the same surface.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};

use crate::alsa_backend::{IoCounters, NegotiatedPcm};
use crate::config::Config;

const CONNECTION_READ_TIMEOUT: Duration = Duration::from_secs(2);
const ACCEPT_POLL_INTERVAL: Duration = Duration::from_millis(500);
const NEVER_MS: u64 = u64::MAX;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TtsQueueMetrics {
    pub pending_frames: u64,
    pub budget_frames: u64,
    pub max_pending_frames: u64,
    pub over_budget: bool,
    pub over_budget_periods: u64,
    pub over_budget_ms: u64,
    pub over_budget_streak_ms: u64,
}

pub struct OutputdState {
    started_at: Instant,
    backend: String,
    content_pcm: String,
    dac_pcm: String,
    chip_ref_pcm: Option<String>,
    reference_udp_target: Option<String>,
    sample_rate: AtomicU64,
    content_period_frames: AtomicU64,
    dac_period_frames: AtomicU64,
    content_buffer_frames: AtomicU64,
    dac_buffer_frames: AtomicU64,
    chip_ref_buffer_frames: AtomicU64,
    content_frames_read: AtomicU64,
    content_empty_period_count: AtomicU64,
    content_partial_period_count: AtomicU64,
    content_eagain_count: AtomicU64,
    dac_frames_written: AtomicU64,
    content_xrun_count: AtomicU64,
    dac_xrun_count: AtomicU64,
    last_content_xrun_ms: AtomicU64,
    last_dac_xrun_ms: AtomicU64,
    total_clipped_samples: AtomicU64,
    last_period_clipped_samples: AtomicU64,
    reference_sequence: AtomicU64,
    tts_pending_frames: AtomicU64,
    tts_budget_frames: AtomicU64,
    tts_max_pending_frames: AtomicU64,
    tts_over_budget: AtomicBool,
    tts_over_budget_periods: AtomicU64,
    tts_over_budget_ms: AtomicU64,
    tts_over_budget_streak_ms: AtomicU64,
    tts_dropped_commands: AtomicU64,
    tts_dropped_audio_frames: AtomicU64,
    last_progress_ms: AtomicU64,
    watchdog_pings_sent: AtomicU64,
}

impl OutputdState {
    pub fn new(config: &Config) -> Self {
        Self {
            started_at: Instant::now(),
            backend: config.backend.as_str().to_string(),
            content_pcm: config.content_pcm.clone(),
            dac_pcm: config.dac_pcm.clone(),
            chip_ref_pcm: config.chip_ref_pcm.clone(),
            reference_udp_target: config.reference_udp_target.clone(),
            sample_rate: AtomicU64::new(config.sample_rate as u64),
            content_period_frames: AtomicU64::new(config.period_frames as u64),
            dac_period_frames: AtomicU64::new(config.period_frames as u64),
            content_buffer_frames: AtomicU64::new(config.content_buffer_frames as u64),
            dac_buffer_frames: AtomicU64::new(config.dac_buffer_frames as u64),
            chip_ref_buffer_frames: AtomicU64::new(config.chip_ref_buffer_frames as u64),
            content_frames_read: AtomicU64::new(0),
            content_empty_period_count: AtomicU64::new(0),
            content_partial_period_count: AtomicU64::new(0),
            content_eagain_count: AtomicU64::new(0),
            dac_frames_written: AtomicU64::new(0),
            content_xrun_count: AtomicU64::new(0),
            dac_xrun_count: AtomicU64::new(0),
            last_content_xrun_ms: AtomicU64::new(NEVER_MS),
            last_dac_xrun_ms: AtomicU64::new(NEVER_MS),
            total_clipped_samples: AtomicU64::new(0),
            last_period_clipped_samples: AtomicU64::new(0),
            reference_sequence: AtomicU64::new(0),
            tts_pending_frames: AtomicU64::new(0),
            tts_budget_frames: AtomicU64::new(0),
            tts_max_pending_frames: AtomicU64::new(0),
            tts_over_budget: AtomicBool::new(false),
            tts_over_budget_periods: AtomicU64::new(0),
            tts_over_budget_ms: AtomicU64::new(0),
            tts_over_budget_streak_ms: AtomicU64::new(0),
            tts_dropped_commands: AtomicU64::new(0),
            tts_dropped_audio_frames: AtomicU64::new(0),
            last_progress_ms: AtomicU64::new(0),
            watchdog_pings_sent: AtomicU64::new(0),
        }
    }

    pub fn set_negotiated(&self, content: NegotiatedPcm, dac: NegotiatedPcm) {
        self.sample_rate
            .store(dac.sample_rate as u64, Ordering::Relaxed);
        self.content_period_frames
            .store(content.period_frames as u64, Ordering::Relaxed);
        self.dac_period_frames
            .store(dac.period_frames as u64, Ordering::Relaxed);
        self.content_buffer_frames
            .store(content.buffer_frames as u64, Ordering::Relaxed);
        self.dac_buffer_frames
            .store(dac.buffer_frames as u64, Ordering::Relaxed);
    }

    pub fn mark_period(
        &self,
        counters: IoCounters,
        reference_sequence: u64,
        clipped_samples: u32,
        tts: TtsQueueMetrics,
    ) {
        let uptime_ms = self.uptime_ms();
        self.content_frames_read
            .store(counters.content_frames_read, Ordering::Relaxed);
        self.content_empty_period_count
            .store(counters.content_empty_period_count, Ordering::Relaxed);
        self.content_partial_period_count
            .store(counters.content_partial_period_count, Ordering::Relaxed);
        self.content_eagain_count
            .store(counters.content_eagain_count, Ordering::Relaxed);
        self.dac_frames_written
            .store(counters.dac_frames_written, Ordering::Relaxed);
        let previous_content_xruns = self
            .content_xrun_count
            .swap(counters.content_xrun_count, Ordering::Relaxed);
        if counters.content_xrun_count > previous_content_xruns {
            self.last_content_xrun_ms
                .store(uptime_ms, Ordering::Relaxed);
        }
        let previous_dac_xruns = self
            .dac_xrun_count
            .swap(counters.dac_xrun_count, Ordering::Relaxed);
        if counters.dac_xrun_count > previous_dac_xruns {
            self.last_dac_xrun_ms.store(uptime_ms, Ordering::Relaxed);
        }
        self.reference_sequence
            .store(reference_sequence, Ordering::Relaxed);
        self.tts_pending_frames
            .store(tts.pending_frames, Ordering::Relaxed);
        self.tts_budget_frames
            .store(tts.budget_frames, Ordering::Relaxed);
        self.tts_max_pending_frames
            .store(tts.max_pending_frames, Ordering::Relaxed);
        self.tts_over_budget
            .store(tts.over_budget, Ordering::Relaxed);
        self.tts_over_budget_periods
            .store(tts.over_budget_periods, Ordering::Relaxed);
        self.tts_over_budget_ms
            .store(tts.over_budget_ms, Ordering::Relaxed);
        self.tts_over_budget_streak_ms
            .store(tts.over_budget_streak_ms, Ordering::Relaxed);
        self.last_period_clipped_samples
            .store(clipped_samples as u64, Ordering::Relaxed);
        self.total_clipped_samples
            .fetch_add(clipped_samples as u64, Ordering::Relaxed);
        self.last_progress_ms.store(uptime_ms, Ordering::Relaxed);
    }

    pub fn mark_watchdog_ping(&self) {
        self.watchdog_pings_sent.fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_tts_command_dropped(&self, audio_frames: u64) {
        self.tts_dropped_commands
            .fetch_add(1, Ordering::Relaxed);
        if audio_frames > 0 {
            self.tts_dropped_audio_frames
                .fetch_add(audio_frames, Ordering::Relaxed);
        }
    }

    pub fn snapshot_json(&self) -> String {
        let mut buf = String::with_capacity(1024);
        let uptime_ms = self.uptime_ms();
        let content_xrun_count = self.content_xrun_count.load(Ordering::Relaxed);
        let dac_xrun_count = self.dac_xrun_count.load(Ordering::Relaxed);
        buf.push('{');
        push_kv_f64(&mut buf, "uptime_seconds", (uptime_ms as f64) / 1000.0, 2);
        buf.push(',');
        push_kv_str(&mut buf, "backend", &self.backend);
        buf.push(',');

        buf.push_str(r#""content":{"#);
        push_kv_str(&mut buf, "pcm", &self.content_pcm);
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "period_frames",
            self.content_period_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "buffer_frames",
            self.content_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "frames_read",
            self.content_frames_read.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "empty_periods",
            self.content_empty_period_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "partial_periods",
            self.content_partial_period_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "eagain_count",
            self.content_eagain_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "xrun_count", content_xrun_count);
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_xrun_age_ms",
            event_age_ms(uptime_ms, self.last_content_xrun_ms.load(Ordering::Relaxed)),
        );
        buf.push(',');
        push_kv_f64(
            &mut buf,
            "xrun_rate_per_hour",
            rate_per_hour(content_xrun_count, uptime_ms),
            3,
        );
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""dac":{"#);
        push_kv_str(&mut buf, "pcm", &self.dac_pcm);
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "sample_rate",
            self.sample_rate.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "period_frames",
            self.dac_period_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "buffer_frames",
            self.dac_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "frames_written",
            self.dac_frames_written.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "xrun_count", dac_xrun_count);
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_xrun_age_ms",
            event_age_ms(uptime_ms, self.last_dac_xrun_ms.load(Ordering::Relaxed)),
        );
        buf.push(',');
        push_kv_f64(
            &mut buf,
            "xrun_rate_per_hour",
            rate_per_hour(dac_xrun_count, uptime_ms),
            3,
        );
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""mix":{"#);
        push_kv_u64(
            &mut buf,
            "reference_sequence",
            self.reference_sequence.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "last_period_clipped_samples",
            self.last_period_clipped_samples.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "clipped_samples",
            self.total_clipped_samples.load(Ordering::Relaxed),
        );
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""reference_outputs":{"#);
        push_kv_str_opt(&mut buf, "chip_ref_pcm", self.chip_ref_pcm.as_deref());
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "chip_ref_buffer_frames",
            self.chip_ref_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_str_opt(
            &mut buf,
            "udp_target",
            self.reference_udp_target.as_deref(),
        );
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""tts":{"#);
        push_kv_u64(
            &mut buf,
            "pending_frames",
            self.tts_pending_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "budget_frames",
            self.tts_budget_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "max_pending_frames",
            self.tts_max_pending_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "over_budget",
            self.tts_over_budget.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "over_budget_periods",
            self.tts_over_budget_periods.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "over_budget_ms",
            self.tts_over_budget_ms.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "over_budget_streak_ms",
            self.tts_over_budget_streak_ms.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "dropped_commands",
            self.tts_dropped_commands.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "dropped_audio_frames",
            self.tts_dropped_audio_frames.load(Ordering::Relaxed),
        );
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""watchdog":{"#);
        let last_progress_ms = self.last_progress_ms.load(Ordering::Relaxed);
        let age_ms = uptime_ms.saturating_sub(last_progress_ms);
        push_kv_u64(
            &mut buf,
            "pings_sent",
            self.watchdog_pings_sent.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "last_progress_age_ms", age_ms);
        buf.push('}');

        buf.push('}');
        buf
    }

    fn uptime_ms(&self) -> u64 {
        self.started_at.elapsed().as_millis() as u64
    }
}

pub struct StateServer {
    socket_path: PathBuf,
    state: Arc<OutputdState>,
    listener: UnixListener,
}

impl StateServer {
    pub fn bind(socket_path: PathBuf, state: Arc<OutputdState>) -> Result<Self> {
        if let Some(parent) = socket_path.parent() {
            std::fs::create_dir_all(parent).with_context(|| {
                format!("creating outputd state socket parent {}", parent.display())
            })?;
        }
        let _ = std::fs::remove_file(&socket_path);
        let listener = UnixListener::bind(&socket_path)
            .with_context(|| format!("binding outputd STATUS socket {}", socket_path.display()))?;
        listener
            .set_nonblocking(true)
            .context("set_nonblocking on outputd state listener")?;
        eprintln!(
            "event=outputd.state_server.listening socket={}",
            socket_path.display()
        );

        Ok(Self {
            socket_path,
            state,
            listener,
        })
    }

    pub fn run(&self, shutdown: &AtomicBool) -> Result<()> {
        while !shutdown.load(Ordering::Relaxed) {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    if let Err(e) = self.handle_connection(stream) {
                        eprintln!("event=outputd.state_server.handle_failed detail={e:#}");
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(ACCEPT_POLL_INTERVAL);
                }
                Err(e) => {
                    eprintln!("event=outputd.state_server.accept_failed detail={e}");
                    std::thread::sleep(ACCEPT_POLL_INTERVAL);
                }
            }
        }

        let _ = std::fs::remove_file(&self.socket_path);
        eprintln!("event=outputd.state_server.stopped");
        Ok(())
    }

    fn handle_connection(&self, mut stream: UnixStream) -> Result<()> {
        stream
            .set_read_timeout(Some(CONNECTION_READ_TIMEOUT))
            .context("set_read_timeout on outputd state connection")?;
        let mut reader = BufReader::new(stream.try_clone()?);
        let mut command = String::new();
        reader
            .read_line(&mut command)
            .context("reading outputd state command")?;
        let command = command.trim();
        let response = match command {
            "STATUS" => self.state.snapshot_json(),
            other => format!(
                r#"{{"error":"unknown command","received":"{}"}}"#,
                escape_json(other)
            ),
        };
        stream
            .write_all(response.as_bytes())
            .context("writing outputd state response")?;
        stream.write_all(b"\n").ok();
        Ok(())
    }
}

fn push_kv_str(buf: &mut String, key: &str, value: &str) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    buf.push('"');
    buf.push_str(&escape_json(value));
    buf.push('"');
}

fn push_kv_str_opt(buf: &mut String, key: &str, value: Option<&str>) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    match value {
        Some(value) => {
            buf.push('"');
            buf.push_str(&escape_json(value));
            buf.push('"');
        }
        None => buf.push_str("null"),
    }
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

fn event_age_ms(uptime_ms: u64, event_ms: u64) -> Option<u64> {
    if event_ms == NEVER_MS {
        None
    } else {
        Some(uptime_ms.saturating_sub(event_ms))
    }
}

fn rate_per_hour(count: u64, uptime_ms: u64) -> f64 {
    if count == 0 || uptime_ms == 0 {
        return 0.0;
    }
    (count as f64) * 3_600_000.0 / (uptime_ms as f64)
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
    use crate::config::{BackendMode, Config};

    fn test_config() -> Config {
        Config {
            backend: BackendMode::Alsa,
            content_pcm: "outputd_content_capture".to_string(),
            dac_pcm: "outputd_dac".to_string(),
            sample_rate: 48_000,
            period_frames: 1024,
            content_buffer_frames: 4096,
            dac_buffer_frames: 3072,
            chip_ref_pcm: None,
            chip_ref_buffer_frames: 4096,
            reference_udp_target: None,
            stream_id: 1,
            tts_socket_path: None,
            control_socket_path: None,
        }
    }

    #[test]
    fn snapshot_json_contains_outputd_health_fields() {
        let state = OutputdState::new(&test_config());
        state.mark_period(
            IoCounters {
                content_frames_read: 2048,
                content_empty_period_count: 4,
                content_partial_period_count: 5,
                content_eagain_count: 6,
                dac_frames_written: 1024,
                content_xrun_count: 1,
                dac_xrun_count: 2,
            },
            42,
            3,
            TtsQueueMetrics {
                pending_frames: 512,
                budget_frames: 96_000,
                max_pending_frames: 120_000,
                over_budget: true,
                over_budget_periods: 7,
                over_budget_ms: 149,
                over_budget_streak_ms: 64,
            },
        );

        let j = state.snapshot_json();
        for needle in [
            r#""backend":"alsa""#,
            r#""content":{"pcm":"outputd_content_capture""#,
            r#""dac":{"pcm":"outputd_dac""#,
            r#""sample_rate":48000"#,
            r#""period_frames":1024"#,
            r#""frames_read":2048"#,
            r#""empty_periods":4"#,
            r#""partial_periods":5"#,
            r#""eagain_count":6"#,
            r#""last_xrun_age_ms":"#,
            r#""xrun_rate_per_hour":"#,
            r#""frames_written":1024"#,
            r#""reference_sequence":42"#,
            r#""last_period_clipped_samples":3"#,
            r#""clipped_samples":3"#,
            r#""reference_outputs":{"chip_ref_pcm":null,"chip_ref_buffer_frames":4096,"udp_target":null}"#,
            r#""tts":{"pending_frames":512,"budget_frames":96000,"max_pending_frames":120000,"over_budget":true,"over_budget_periods":7,"over_budget_ms":149,"over_budget_streak_ms":64}"#,
            r#""watchdog""#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
    }

    #[test]
    fn snapshot_json_accumulates_clipping() {
        let state = OutputdState::new(&test_config());
        state.mark_period(IoCounters::default(), 1, 2, TtsQueueMetrics::default());
        state.mark_period(IoCounters::default(), 2, 5, TtsQueueMetrics::default());

        let j = state.snapshot_json();
        assert!(j.contains(r#""last_period_clipped_samples":5"#));
        assert!(j.contains(r#""clipped_samples":7"#));
    }

    #[test]
    fn snapshot_json_reports_never_for_missing_xruns() {
        let state = OutputdState::new(&test_config());
        state.mark_period(IoCounters::default(), 1, 0, TtsQueueMetrics::default());

        let j = state.snapshot_json();
        assert_eq!(j.matches(r#""last_xrun_age_ms":null"#).count(), 2);
        assert_eq!(j.matches(r#""xrun_rate_per_hour":0.000"#).count(), 2);
    }
}
