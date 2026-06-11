//! Local STATUS socket for outputd observability.
//!
//! The socket mirrors the fan-in daemon's shape: one command per
//! connection, `STATUS\n` returns a compact JSON snapshot, malformed
//! commands return a JSON error. `jasper-control /state`,
//! `jasper-doctor`, and an operator can all consume the same surface.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};

use crate::alsa_backend::{DualAppleStatus, IoCounters, NegotiatedPcm};
use crate::config::Config;
use crate::content_bridge::ContentBridgeMetrics;
use crate::dac_content::DacContentMetrics;

const CONNECTION_READ_TIMEOUT: Duration = Duration::from_secs(2);
const ACCEPT_POLL_INTERVAL: Duration = Duration::from_millis(500);
const NEVER_MS: u64 = u64::MAX;

pub struct OutputdState {
    started_at: Instant,
    backend: String,
    sink_mode: String,
    content_pcm: String,
    dac_pcm: String,
    dual_dac_a_pcm: Option<String>,
    dual_dac_b_pcm: Option<String>,
    dual_linked: AtomicBool,
    dual_delay_delta_frames: AtomicI64,
    dual_delay_delta_baseline_frames: AtomicI64,
    dual_delay_delta_error_frames: AtomicI64,
    dual_max_delay_delta_frames: AtomicI64,
    chip_ref_pcm: Option<String>,
    reference_udp_target: Option<String>,
    sample_rate: AtomicU64,
    content_period_frames: AtomicU64,
    dac_period_frames: AtomicU64,
    content_buffer_frames: AtomicU64,
    dac_buffer_frames: AtomicU64,
    content_bridge_mode: String,
    content_bridge_ring_frames: AtomicU64,
    content_bridge_target_fill_frames: AtomicU64,
    content_bridge_locked: AtomicBool,
    content_bridge_fill_frames: AtomicU64,
    content_bridge_min_fill_frames: AtomicU64,
    content_bridge_max_fill_frames: AtomicU64,
    content_bridge_ratio_ppm_x100: AtomicI64,
    content_bridge_input_frames: AtomicU64,
    content_bridge_output_frames: AtomicU64,
    content_bridge_silence_frames: AtomicU64,
    content_bridge_underrun_frames: AtomicU64,
    content_bridge_overrun_frames: AtomicU64,
    content_bridge_resync_count: AtomicU64,
    content_bridge_reset_count: AtomicU64,
    content_bridge_ratio_clamp_count: AtomicU64,
    content_bridge_lock_count: AtomicU64,
    content_bridge_unlock_count: AtomicU64,
    dac_content_fifo: Option<String>,
    dac_content_channel: String,
    dac_content_serving_fifo: AtomicBool,
    dac_content_fifo_periods: AtomicU64,
    dac_content_fallback_periods: AtomicU64,
    dac_content_fallback_transitions: AtomicU64,
    dac_content_recoveries: AtomicU64,
    dac_content_staged_periods: AtomicU64,
    dac_content_overflow_dropped_periods: AtomicU64,
    dac_content_open_failures: AtomicU64,
    dac_content_read_failures: AtomicU64,
    chip_ref_sample_rate: AtomicU64,
    chip_ref_period_frames: AtomicU64,
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
    last_progress_ms: AtomicU64,
    watchdog_pings_sent: AtomicU64,
}

impl OutputdState {
    pub fn new(config: &Config) -> Self {
        Self {
            started_at: Instant::now(),
            backend: config.backend.as_str().to_string(),
            sink_mode: config.sink_mode.as_str().to_string(),
            content_pcm: config.content_pcm.clone(),
            dac_pcm: config.dac_pcm.clone(),
            dual_dac_a_pcm: config.dual_dac_a_pcm.clone(),
            dual_dac_b_pcm: config.dual_dac_b_pcm.clone(),
            dual_linked: AtomicBool::new(false),
            dual_delay_delta_frames: AtomicI64::new(pack_optional_i64(None)),
            dual_delay_delta_baseline_frames: AtomicI64::new(pack_optional_i64(None)),
            dual_delay_delta_error_frames: AtomicI64::new(pack_optional_i64(None)),
            dual_max_delay_delta_frames: AtomicI64::new(config.dual_max_delay_delta_frames),
            chip_ref_pcm: config.chip_ref_pcm.clone(),
            reference_udp_target: config.reference_udp_target.clone(),
            sample_rate: AtomicU64::new(config.sample_rate as u64),
            content_period_frames: AtomicU64::new(config.period_frames as u64),
            dac_period_frames: AtomicU64::new(config.period_frames as u64),
            content_buffer_frames: AtomicU64::new(config.content_buffer_frames as u64),
            dac_buffer_frames: AtomicU64::new(config.dac_buffer_frames as u64),
            content_bridge_mode: config.content_bridge_mode.as_str().to_string(),
            content_bridge_ring_frames: AtomicU64::new(config.content_bridge.ring_frames as u64),
            content_bridge_target_fill_frames: AtomicU64::new(
                config.content_bridge.target_fill_frames as u64,
            ),
            content_bridge_locked: AtomicBool::new(false),
            content_bridge_fill_frames: AtomicU64::new(0),
            content_bridge_min_fill_frames: AtomicU64::new(0),
            content_bridge_max_fill_frames: AtomicU64::new(0),
            content_bridge_ratio_ppm_x100: AtomicI64::new(0),
            content_bridge_input_frames: AtomicU64::new(0),
            content_bridge_output_frames: AtomicU64::new(0),
            content_bridge_silence_frames: AtomicU64::new(0),
            content_bridge_underrun_frames: AtomicU64::new(0),
            content_bridge_overrun_frames: AtomicU64::new(0),
            content_bridge_resync_count: AtomicU64::new(0),
            content_bridge_reset_count: AtomicU64::new(0),
            content_bridge_ratio_clamp_count: AtomicU64::new(0),
            content_bridge_lock_count: AtomicU64::new(0),
            content_bridge_unlock_count: AtomicU64::new(0),
            dac_content_fifo: config.dac_content_fifo.clone(),
            dac_content_channel: config.dac_content_channel.as_str().to_string(),
            dac_content_serving_fifo: AtomicBool::new(false),
            dac_content_fifo_periods: AtomicU64::new(0),
            dac_content_fallback_periods: AtomicU64::new(0),
            dac_content_fallback_transitions: AtomicU64::new(0),
            dac_content_recoveries: AtomicU64::new(0),
            dac_content_staged_periods: AtomicU64::new(0),
            dac_content_overflow_dropped_periods: AtomicU64::new(0),
            dac_content_open_failures: AtomicU64::new(0),
            dac_content_read_failures: AtomicU64::new(0),
            chip_ref_sample_rate: AtomicU64::new(config.chip_ref_sample_rate as u64),
            chip_ref_period_frames: AtomicU64::new(config.chip_ref_period_frames as u64),
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
        self.last_period_clipped_samples
            .store(clipped_samples as u64, Ordering::Relaxed);
        self.total_clipped_samples
            .fetch_add(clipped_samples as u64, Ordering::Relaxed);
        self.last_progress_ms.store(uptime_ms, Ordering::Relaxed);
    }

    pub fn mark_watchdog_ping(&self) {
        self.watchdog_pings_sent.fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_dual_apple_status(&self, status: &DualAppleStatus) {
        self.dual_linked.store(status.linked, Ordering::Relaxed);
        self.dual_delay_delta_frames.store(
            pack_optional_i64(status.delay_delta_frames),
            Ordering::Relaxed,
        );
        self.dual_delay_delta_baseline_frames.store(
            pack_optional_i64(status.delay_delta_baseline_frames),
            Ordering::Relaxed,
        );
        self.dual_delay_delta_error_frames.store(
            pack_optional_i64(status.delay_delta_error_frames),
            Ordering::Relaxed,
        );
        self.dual_max_delay_delta_frames
            .store(status.max_delay_delta_frames, Ordering::Relaxed);
    }

    pub fn mark_dac_content(&self, metrics: DacContentMetrics) {
        self.dac_content_serving_fifo
            .store(metrics.serving_fifo, Ordering::Relaxed);
        self.dac_content_fifo_periods
            .store(metrics.fifo_periods, Ordering::Relaxed);
        self.dac_content_fallback_periods
            .store(metrics.fallback_periods, Ordering::Relaxed);
        self.dac_content_fallback_transitions
            .store(metrics.fallback_transitions, Ordering::Relaxed);
        self.dac_content_recoveries
            .store(metrics.recoveries, Ordering::Relaxed);
        self.dac_content_staged_periods
            .store(metrics.staged_periods, Ordering::Relaxed);
        self.dac_content_overflow_dropped_periods
            .store(metrics.overflow_dropped_periods, Ordering::Relaxed);
        self.dac_content_open_failures
            .store(metrics.open_failures, Ordering::Relaxed);
        self.dac_content_read_failures
            .store(metrics.read_failures, Ordering::Relaxed);
    }

    pub fn mark_content_bridge(&self, metrics: ContentBridgeMetrics) {
        self.content_bridge_locked
            .store(metrics.locked, Ordering::Relaxed);
        self.content_bridge_ring_frames
            .store(metrics.ring_capacity_frames, Ordering::Relaxed);
        self.content_bridge_target_fill_frames
            .store(metrics.target_fill_frames, Ordering::Relaxed);
        self.content_bridge_fill_frames
            .store(metrics.fill_frames, Ordering::Relaxed);
        self.content_bridge_min_fill_frames
            .store(metrics.min_fill_frames, Ordering::Relaxed);
        self.content_bridge_max_fill_frames
            .store(metrics.max_fill_frames, Ordering::Relaxed);
        self.content_bridge_ratio_ppm_x100.store(
            (metrics.ratio_ppm.clamp(-50_000.0, 50_000.0) * 100.0).round() as i64,
            Ordering::Relaxed,
        );
        self.content_bridge_input_frames
            .store(metrics.input_frames, Ordering::Relaxed);
        self.content_bridge_output_frames
            .store(metrics.output_frames, Ordering::Relaxed);
        self.content_bridge_silence_frames
            .store(metrics.silence_frames, Ordering::Relaxed);
        self.content_bridge_underrun_frames
            .store(metrics.underrun_frames, Ordering::Relaxed);
        self.content_bridge_overrun_frames
            .store(metrics.overrun_frames, Ordering::Relaxed);
        self.content_bridge_resync_count
            .store(metrics.resync_count, Ordering::Relaxed);
        self.content_bridge_reset_count
            .store(metrics.reset_count, Ordering::Relaxed);
        self.content_bridge_ratio_clamp_count
            .store(metrics.ratio_clamp_count, Ordering::Relaxed);
        self.content_bridge_lock_count
            .store(metrics.lock_count, Ordering::Relaxed);
        self.content_bridge_unlock_count
            .store(metrics.unlock_count, Ordering::Relaxed);
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
        push_kv_str(&mut buf, "sink_mode", &self.sink_mode);
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

        buf.push_str(r#""content_bridge":{"#);
        push_kv_str(&mut buf, "mode", &self.content_bridge_mode);
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "enabled",
            self.content_bridge_mode == "rate_match",
        );
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "locked",
            self.content_bridge_locked.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "ring_frames",
            self.content_bridge_ring_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "target_fill_frames",
            self.content_bridge_target_fill_frames
                .load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "fill_frames",
            self.content_bridge_fill_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "min_fill_frames",
            self.content_bridge_min_fill_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "max_fill_frames",
            self.content_bridge_max_fill_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_f64(
            &mut buf,
            "ratio_ppm",
            (self.content_bridge_ratio_ppm_x100.load(Ordering::Relaxed) as f64) / 100.0,
            2,
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "input_frames",
            self.content_bridge_input_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "output_frames",
            self.content_bridge_output_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "silence_frames",
            self.content_bridge_silence_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "underrun_frames",
            self.content_bridge_underrun_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "overrun_frames",
            self.content_bridge_overrun_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "resync_count",
            self.content_bridge_resync_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "reset_count",
            self.content_bridge_reset_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "ratio_clamp_count",
            self.content_bridge_ratio_clamp_count
                .load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "lock_count",
            self.content_bridge_lock_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "unlock_count",
            self.content_bridge_unlock_count.load(Ordering::Relaxed),
        );
        buf.push('}');
        buf.push(',');

        // Multi-room round-trip lane (Increment 3) — DAEMON-TRUTH health
        // for /state + jasper-doctor (never a Python mirror of env
        // intent). enabled:false with no further fields when the lane is
        // not configured (solo — zero cost, zero noise).
        buf.push_str(r#""dac_content":{"#);
        match self.dac_content_fifo.as_deref() {
            Some(fifo) => {
                push_kv_bool(&mut buf, "enabled", true);
                buf.push(',');
                push_kv_str(&mut buf, "fifo", fifo);
                buf.push(',');
                push_kv_str(&mut buf, "channel", &self.dac_content_channel);
                buf.push(',');
                push_kv_bool(
                    &mut buf,
                    "serving_fifo",
                    self.dac_content_serving_fifo.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "fifo_periods",
                    self.dac_content_fifo_periods.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "fallback_periods",
                    self.dac_content_fallback_periods.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "fallback_transitions",
                    self.dac_content_fallback_transitions
                        .load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "recoveries",
                    self.dac_content_recoveries.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "staged_periods",
                    self.dac_content_staged_periods.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "overflow_dropped_periods",
                    self.dac_content_overflow_dropped_periods
                        .load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "open_failures",
                    self.dac_content_open_failures.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "read_failures",
                    self.dac_content_read_failures.load(Ordering::Relaxed),
                );
            }
            None => {
                push_kv_bool(&mut buf, "enabled", false);
            }
        }
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

        if self.sink_mode == "dual_apple" {
            buf.push_str(r#""dual_apple":{"#);
            push_kv_str_opt(&mut buf, "dac_a_pcm", self.dual_dac_a_pcm.as_deref());
            buf.push(',');
            push_kv_str_opt(&mut buf, "dac_b_pcm", self.dual_dac_b_pcm.as_deref());
            buf.push(',');
            push_kv_bool(
                &mut buf,
                "linked",
                self.dual_linked.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_i64_opt(
                &mut buf,
                "delay_delta_frames",
                unpack_optional_i64(self.dual_delay_delta_frames.load(Ordering::Relaxed)),
            );
            buf.push(',');
            push_kv_i64_opt(
                &mut buf,
                "delay_delta_baseline_frames",
                unpack_optional_i64(
                    self.dual_delay_delta_baseline_frames
                        .load(Ordering::Relaxed),
                ),
            );
            buf.push(',');
            push_kv_i64_opt(
                &mut buf,
                "delay_delta_error_frames",
                unpack_optional_i64(self.dual_delay_delta_error_frames.load(Ordering::Relaxed)),
            );
            buf.push(',');
            push_kv_i64(
                &mut buf,
                "max_delay_delta_frames",
                self.dual_max_delay_delta_frames.load(Ordering::Relaxed),
            );
            buf.push('}');
            buf.push(',');
        }

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
        push_kv_str(&mut buf, "speaker_reference_source", "outputd_final_electrical");
        buf.push(',');
        push_kv_bool(&mut buf, "speaker_reference_is_fallback", false);
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "speaker_reference_active",
            self.chip_ref_pcm.is_some() || self.reference_udp_target.is_some(),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "speaker_reference_sample_rate",
            self.sample_rate.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "speaker_reference_channels",
            crate::types::CHANNELS as u64,
        );
        buf.push(',');
        push_kv_str_opt(&mut buf, "chip_ref_pcm", self.chip_ref_pcm.as_deref());
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "chip_ref_sample_rate",
            self.chip_ref_sample_rate.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "chip_ref_period_frames",
            self.chip_ref_period_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "chip_ref_buffer_frames",
            self.chip_ref_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_str_opt(&mut buf, "udp_target", self.reference_udp_target.as_deref());
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

fn push_kv_i64(buf: &mut String, key: &str, value: i64) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    buf.push_str(&value.to_string());
}

fn push_kv_i64_opt(buf: &mut String, key: &str, value: Option<i64>) {
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

const PACKED_I64_NONE: i64 = i64::MIN;

fn pack_optional_i64(value: Option<i64>) -> i64 {
    value.unwrap_or(PACKED_I64_NONE)
}

fn unpack_optional_i64(value: i64) -> Option<i64> {
    if value == PACKED_I64_NONE {
        None
    } else {
        Some(value)
    }
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
    use crate::config::{
        BackendMode, Config, ContentBridgeConfig, ContentBridgeMode, SinkMode,
        DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM, DEFAULT_CONTENT_BRIDGE_RING_FRAMES,
        DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES,
    };

    fn test_config() -> Config {
        Config {
            backend: BackendMode::Alsa,
            sink_mode: SinkMode::SingleAlsa,
            content_pcm: "outputd_content_capture".to_string(),
            content_channels: 2,
            dac_pcm: "outputd_dac".to_string(),
            dual_dac_a_pcm: None,
            dual_dac_b_pcm: None,
            dual_require_link: false,
            dual_max_delay_delta_frames: 2,
            sample_rate: 48_000,
            period_frames: 1024,
            content_buffer_frames: 4096,
            dac_buffer_frames: 3072,
            content_bridge_mode: ContentBridgeMode::Direct,
            content_bridge: ContentBridgeConfig {
                ring_frames: DEFAULT_CONTENT_BRIDGE_RING_FRAMES,
                target_fill_frames: DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES,
                max_adjust_ppm: DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM,
            },
            chip_ref_pcm: None,
            chip_ref_sample_rate: 16_000,
            chip_ref_period_frames: 320,
            chip_ref_buffer_frames: 4096,
            reference_udp_target: None,
            stream_id: 1,
            control_socket_path: None,
            dac_content_fifo: None,
            dac_content_channel: crate::dac_content::ChannelPick::Stereo,
        }
    }

    fn dual_test_config() -> Config {
        Config {
            sink_mode: SinkMode::DualApple,
            content_pcm: "outputd_active_content_capture".to_string(),
            content_channels: 4,
            dac_pcm: "dual_apple_usb_c_dac_4ch".to_string(),
            dual_dac_a_pcm: Some("hw:CARD=A,DEV=0".to_string()),
            dual_dac_b_pcm: Some("hw:CARD=B,DEV=0".to_string()),
            ..test_config()
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
        );

        let j = state.snapshot_json();
        for needle in [
            r#""backend":"alsa""#,
            r#""content":{"pcm":"outputd_content_capture""#,
            r#""content_bridge":{"mode":"direct""#,
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
            r#""reference_outputs":{"speaker_reference_source":"outputd_final_electrical","speaker_reference_is_fallback":false,"speaker_reference_active":false,"speaker_reference_sample_rate":48000,"speaker_reference_channels":2,"chip_ref_pcm":null,"chip_ref_sample_rate":16000,"chip_ref_period_frames":320,"chip_ref_buffer_frames":4096,"udp_target":null}"#,
            r#""watchdog""#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        assert!(!j.contains(r#""tts":"#), "retired outputd TTS state present in {j}");
        assert!(
            !j.contains(r#""assistant_loudness":"#),
            "duplicate outputd loudness state present in {j}"
        );
    }

    #[test]
    fn snapshot_json_contains_dual_apple_runtime_health() {
        let state = OutputdState::new(&dual_test_config());
        state.mark_dual_apple_status(&DualAppleStatus {
            dac_a_pcm: "hw:CARD=A,DEV=0".to_string(),
            dac_b_pcm: "hw:CARD=B,DEV=0".to_string(),
            linked: true,
            delay_delta_frames: Some(7),
            delay_delta_baseline_frames: Some(5),
            delay_delta_error_frames: Some(2),
            max_delay_delta_frames: 2,
        });

        let j = state.snapshot_json();
        for needle in [
            r#""sink_mode":"dual_apple""#,
            r#""dual_apple":{"dac_a_pcm":"hw:CARD=A,DEV=0""#,
            r#""dac_b_pcm":"hw:CARD=B,DEV=0""#,
            r#""linked":true"#,
            r#""delay_delta_frames":7"#,
            r#""delay_delta_baseline_frames":5"#,
            r#""delay_delta_error_frames":2"#,
            r#""max_delay_delta_frames":2"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
    }

    #[test]
    fn snapshot_json_dac_content_disabled_is_quiet_and_enabled_is_full() {
        // Solo (lane unconfigured): just enabled:false — zero noise.
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        assert!(
            j.contains(r#""dac_content":{"enabled":false}"#),
            "missing quiet disabled block in {j}"
        );

        // Member (lane configured): full daemon-truth health block.
        let cfg = Config {
            dac_content_fifo: Some("/run/jasper-grouping/member-content.fifo".to_string()),
            dac_content_channel: crate::dac_content::ChannelPick::Left,
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_dac_content(DacContentMetrics {
            serving_fifo: true,
            fifo_periods: 100,
            fallback_periods: 7,
            fallback_transitions: 2,
            recoveries: 2,
            staged_periods: 3,
            overflow_dropped_periods: 1,
            open_failures: 4,
            read_failures: 5,
        });
        let j = state.snapshot_json();
        for needle in [
            r#""dac_content":{"enabled":true"#,
            r#""fifo":"/run/jasper-grouping/member-content.fifo""#,
            r#""channel":"left""#,
            r#""serving_fifo":true"#,
            r#""fifo_periods":100"#,
            r#""fallback_periods":7"#,
            r#""fallback_transitions":2"#,
            r#""recoveries":2"#,
            r#""staged_periods":3"#,
            r#""overflow_dropped_periods":1"#,
            r#""open_failures":4"#,
            r#""read_failures":5"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
    }

    #[test]
    fn snapshot_json_accumulates_clipping() {
        let state = OutputdState::new(&test_config());
        state.mark_period(IoCounters::default(), 1, 2);
        state.mark_period(IoCounters::default(), 2, 5);

        let j = state.snapshot_json();
        assert!(j.contains(r#""last_period_clipped_samples":5"#));
        assert!(j.contains(r#""clipped_samples":7"#));
    }

    #[test]
    fn snapshot_json_reports_never_for_missing_xruns() {
        let state = OutputdState::new(&test_config());
        state.mark_period(IoCounters::default(), 1, 0);

        let j = state.snapshot_json();
        assert_eq!(j.matches(r#""last_xrun_age_ms":null"#).count(), 2);
        assert_eq!(j.matches(r#""xrun_rate_per_hour":0.000"#).count(), 2);
    }
}
