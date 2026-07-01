// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Local control socket for outputd observability and cheap runtime knobs.
//!
//! The socket mirrors the fan-in daemon's shape: one command per
//! connection. `STATUS\n` returns a compact JSON snapshot, trim-only
//! pair-balance updates use `SET_DAC_CONTENT_TRIM_DB <db>\n`, and malformed
//! commands return a JSON error. `jasper-control /state`,
//! `jasper-doctor`, and an operator can all consume the same surface.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicI64, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};

use crate::aec_clock::SroEstimator;
use crate::alsa_backend::{CompositeStatus, IoCounters, NegotiatedPcm};
use crate::config::Config;
use crate::content_bridge::ContentBridgeMetrics;
use crate::dac_clock::DacClockObserver;
use crate::dac_content::DacContentMetrics;
use crate::local_content_pipe::{LocalContentPipeMetrics, LOCAL_CONTENT_PIPE_FORMAT};
use crate::tts::TtsMetrics;
use jasper_clock::DllSnapshot;
use std::sync::OnceLock;

const CONNECTION_READ_TIMEOUT: Duration = Duration::from_secs(2);
const ACCEPT_POLL_INTERVAL: Duration = Duration::from_millis(500);
const NEVER_MS: u64 = u64::MAX;
const OPTIONAL_U64_NONE: u64 = u64::MAX;
const DAC_CONTENT_TRIM_DB_MIN_TENTHS: i32 = -240;
const DAC_CONTENT_TRIM_DB_MAX_TENTHS: i32 = 0;

#[derive(Debug, Clone, Copy, Default)]
pub struct ChipRefWrite {
    pub frames_written: u64,
    pub delay_frames: Option<u64>,
    pub reference_sequence: Option<u64>,
    pub underruns: u64,
    pub xruns: u64,
    pub recoveries: u64,
    pub write_failed: bool,
}

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
    chip_ref_diagnostic_tee_path: Option<String>,
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
    // The content-bridge rate controller's shared-DLL snapshot (Inc 4): the
    // loop's OWN rate_diff (ppm, error stats, bandwidth, DLL-internal lock /
    // resync counters), published in the one consistent telemetry shape. Mutex
    // (not atomics) because it is a small multi-field Copy struct written once
    // per period by `mark_content_bridge` and read only by the state server —
    // mirrors the `sro_estimator` / `dac_clock` pattern.
    content_bridge_rate_diff: Mutex<DllSnapshot>,
    local_content_pipe: Option<String>,
    local_content_pipe_requested_pipe_bytes: AtomicU64,
    local_content_pipe_open: AtomicBool,
    local_content_pipe_open_failures: AtomicU64,
    local_content_pipe_read_failures: AtomicU64,
    local_content_pipe_reopen_count: AtomicU64,
    local_content_pipe_startup_empty_periods: AtomicU64,
    local_content_pipe_empty_periods: AtomicU64,
    local_content_pipe_partial_periods: AtomicU64,
    local_content_pipe_misaligned_bytes: AtomicU64,
    local_content_pipe_available_bytes: AtomicU64,
    local_content_pipe_actual_pipe_bytes: AtomicU64,
    dac_content_fifo: Option<String>,
    dac_content_channel: String,
    dac_content_highpass_hz: Option<f64>,
    dac_content_trim_db_tenths: AtomicI32,
    dac_content_trim_gain_bits: AtomicU32,
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
    dac_snd_pcm_delay_frames: AtomicU64,
    dac_snd_pcm_delay_sample_ms: AtomicU64,
    chip_ref_queue_depth_periods: AtomicU64,
    chip_ref_queued_frames: AtomicU64,
    chip_ref_frames_written: AtomicU64,
    chip_ref_snd_pcm_delay_frames: AtomicU64,
    chip_ref_snd_pcm_delay_sample_ms: AtomicU64,
    chip_ref_write_underrun_count: AtomicU64,
    chip_ref_write_xrun_count: AtomicU64,
    chip_ref_write_recovery_count: AtomicU64,
    chip_ref_write_error_count: AtomicU64,
    chip_ref_dropped_full_periods: AtomicU64,
    chip_ref_dropped_disconnected_periods: AtomicU64,
    chip_ref_last_write_ms: AtomicU64,
    chip_ref_last_enqueued_reference_sequence: AtomicU64,
    chip_ref_last_written_reference_sequence: AtomicU64,
    chip_ref_tee_active: AtomicBool,
    chip_ref_tee_open_error_count: AtomicU64,
    chip_ref_tee_write_error_count: AtomicU64,
    // Observe-only label (chip-AEC Layer 0): true when the reconciler armed
    // the chip-ref writer purely to MEASURE drift on the DAC playout clock (vs nominal)
    // (not for production chip-AEC). Set once at construction from config;
    // changes no behavior — surfaced in the aec_clock block so /state can
    // self-describe why the chip-ref writer is running.
    chip_ref_observe: bool,
    // Passive SRO (sample-rate-offset) drift estimator (chip-AEC Layer 0).
    // Ticked from `mark_chip_ref_write` — i.e. wherever the chip-ref delay is
    // already sampled — reading the already-stored DAC counters. Observe-only:
    // it never warps audio. Mutex (not atomics) because the estimate is a
    // small struct with a ring buffer; the chip-ref writer is the only writer
    // and the state server the only reader, so contention is negligible.
    sro_estimator: Mutex<SroEstimator>,
    // Last cumulative `chip_ref_frames_written` fed to the SRO estimator.
    // Decimates the ~50 Hz `mark_chip_ref_write` ticks down to ~1 Hz (the rate
    // the estimator's slope window is tuned for).
    sro_last_fed_chip_ref_frames: AtomicU64,
    // Observe-only DAC playout-clock drift observer (Inc 2). A
    // jasper-clock DLL fed the wall-clock-vs-DAC-playout frame error from
    // `mark_dac_delay` (where the DAC delay is already sampled). It NEVER warps
    // audio — it surfaces `dac_clock_ppm` + lock/verdict on /state.
    // Mutex for the same reason as `sro_estimator`: a small struct, single
    // writer (the playback loop) + single reader (the state server).
    dac_clock: Mutex<DacClockObserver>,
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
    // Bonded-member TTS lane (PR-2). Set once at startup when the
    // socket env is configured; the state server may briefly read
    // enabled:false before run_alsa sets it — harmless.
    tts: OnceLock<(String, TtsMetrics)>,
}

fn trim_db_tenths(trim_db: f32) -> i32 {
    (trim_db * 10.0).round() as i32
}

fn validate_trim_db_tenths(trim_db: f32) -> Result<i32> {
    if !trim_db.is_finite() {
        anyhow::bail!("trim_db must be finite");
    }
    let tenths = trim_db_tenths(trim_db);
    if !(DAC_CONTENT_TRIM_DB_MIN_TENTHS..=DAC_CONTENT_TRIM_DB_MAX_TENTHS).contains(&tenths) {
        anyhow::bail!(
            "trim_db must be between {:.1} and {:.1} dB",
            DAC_CONTENT_TRIM_DB_MIN_TENTHS as f32 / 10.0,
            DAC_CONTENT_TRIM_DB_MAX_TENTHS as f32 / 10.0,
        );
    }
    Ok(tenths)
}

fn trim_gain_bits(trim_db_tenths: i32) -> u32 {
    let trim_db = trim_db_tenths as f32 / 10.0;
    let gain = if trim_db < 0.0 {
        10f32.powf(trim_db / 20.0)
    } else {
        1.0
    };
    gain.to_bits()
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
            chip_ref_diagnostic_tee_path: config.chip_ref_tee_path.clone(),
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
            content_bridge_rate_diff: Mutex::new(DllSnapshot::idle()),
            local_content_pipe: config.local_content_pipe.clone(),
            local_content_pipe_requested_pipe_bytes: AtomicU64::new(
                config.local_content_pipe_bytes as u64,
            ),
            local_content_pipe_open: AtomicBool::new(false),
            local_content_pipe_open_failures: AtomicU64::new(0),
            local_content_pipe_read_failures: AtomicU64::new(0),
            local_content_pipe_reopen_count: AtomicU64::new(0),
            local_content_pipe_startup_empty_periods: AtomicU64::new(0),
            local_content_pipe_empty_periods: AtomicU64::new(0),
            local_content_pipe_partial_periods: AtomicU64::new(0),
            local_content_pipe_misaligned_bytes: AtomicU64::new(0),
            local_content_pipe_available_bytes: AtomicU64::new(0),
            local_content_pipe_actual_pipe_bytes: AtomicU64::new(0),
            dac_content_fifo: config.dac_content_fifo.clone(),
            dac_content_channel: config.dac_content_channel.as_str().to_string(),
            dac_content_highpass_hz: config.dac_content_highpass_hz,
            dac_content_trim_db_tenths: AtomicI32::new(trim_db_tenths(config.dac_content_trim_db)),
            dac_content_trim_gain_bits: AtomicU32::new(trim_gain_bits(trim_db_tenths(
                config.dac_content_trim_db,
            ))),
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
            dac_snd_pcm_delay_frames: AtomicU64::new(OPTIONAL_U64_NONE),
            dac_snd_pcm_delay_sample_ms: AtomicU64::new(NEVER_MS),
            chip_ref_queue_depth_periods: AtomicU64::new(0),
            chip_ref_queued_frames: AtomicU64::new(0),
            chip_ref_frames_written: AtomicU64::new(0),
            chip_ref_snd_pcm_delay_frames: AtomicU64::new(OPTIONAL_U64_NONE),
            chip_ref_snd_pcm_delay_sample_ms: AtomicU64::new(NEVER_MS),
            chip_ref_write_underrun_count: AtomicU64::new(0),
            chip_ref_write_xrun_count: AtomicU64::new(0),
            chip_ref_write_recovery_count: AtomicU64::new(0),
            chip_ref_write_error_count: AtomicU64::new(0),
            chip_ref_dropped_full_periods: AtomicU64::new(0),
            chip_ref_dropped_disconnected_periods: AtomicU64::new(0),
            chip_ref_last_write_ms: AtomicU64::new(NEVER_MS),
            chip_ref_last_enqueued_reference_sequence: AtomicU64::new(OPTIONAL_U64_NONE),
            chip_ref_last_written_reference_sequence: AtomicU64::new(OPTIONAL_U64_NONE),
            chip_ref_tee_active: AtomicBool::new(false),
            chip_ref_tee_open_error_count: AtomicU64::new(0),
            chip_ref_tee_write_error_count: AtomicU64::new(0),
            chip_ref_observe: config.chip_ref_observe,
            sro_estimator: Mutex::new(SroEstimator::new()),
            sro_last_fed_chip_ref_frames: AtomicU64::new(0),
            dac_clock: Mutex::new(DacClockObserver::new(
                config.sample_rate,
                config.period_frames,
            )),
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
            tts: OnceLock::new(),
        }
    }

    pub fn set_tts(&self, socket: String, metrics: TtsMetrics) {
        let _ = self.tts.set((socket, metrics));
    }

    pub fn dac_content_trim_gain(&self) -> f32 {
        f32::from_bits(self.dac_content_trim_gain_bits.load(Ordering::Relaxed))
    }

    pub fn dac_content_trim_db(&self) -> f32 {
        self.dac_content_trim_db_tenths.load(Ordering::Relaxed) as f32 / 10.0
    }

    pub fn set_dac_content_trim_db(&self, trim_db: f32) -> Result<f32> {
        if self.dac_content_fifo.is_none() {
            anyhow::bail!("dac_content lane is not enabled");
        }
        let tenths = validate_trim_db_tenths(trim_db)?;
        self.dac_content_trim_db_tenths
            .store(tenths, Ordering::Relaxed);
        self.dac_content_trim_gain_bits
            .store(trim_gain_bits(tenths), Ordering::Relaxed);
        Ok(tenths as f32 / 10.0)
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

    pub fn mark_period(&self, counters: IoCounters, reference_sequence: u64, clipped_samples: u32) {
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

    pub fn mark_dual_apple_status(&self, status: &CompositeStatus) {
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

    pub fn mark_dac_delay(&self, delay_frames: u64) {
        let uptime_ms = self.uptime_ms();
        self.dac_snd_pcm_delay_frames
            .store(delay_frames, Ordering::Relaxed);
        self.dac_snd_pcm_delay_sample_ms
            .store(uptime_ms, Ordering::Relaxed);
        // Observe-only (Inc 2): tick the DAC playout-clock drift observer
        // with the freshly-sampled DAC delay paired with the cumulative frames
        // written and the monotonic uptime. The estimator self-decimates to
        // ~1 Hz, so calling it every period is cheap and harmless. It NEVER
        // touches the audio path. `try_lock` so a /state reader (the only other
        // contender) can never make the playback loop wait; a skipped tick just
        // drops one ~1 Hz sample.
        let dac_written = self.dac_frames_written.load(Ordering::Relaxed);
        let elapsed_seconds = uptime_ms as f64 / 1000.0;
        if let Ok(mut clock) = self.dac_clock.try_lock() {
            clock.observe(dac_written, delay_frames, elapsed_seconds);
        }
    }

    pub fn mark_chip_ref_queue_admitted(&self, frames: u64) {
        self.chip_ref_queue_depth_periods
            .fetch_add(1, Ordering::Relaxed);
        self.chip_ref_queued_frames
            .fetch_add(frames, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_enqueued(&self, reference_sequence: u64) {
        self.chip_ref_last_enqueued_reference_sequence
            .store(reference_sequence, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_dequeued(&self, frames: u64) {
        subtract_saturating(&self.chip_ref_queue_depth_periods, 1);
        subtract_saturating(&self.chip_ref_queued_frames, frames);
    }

    pub fn mark_chip_ref_dropped_full(&self) {
        self.chip_ref_dropped_full_periods
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_dropped_disconnected(&self) {
        self.chip_ref_dropped_disconnected_periods
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_write(&self, event: ChipRefWrite) {
        let ChipRefWrite {
            frames_written,
            delay_frames,
            reference_sequence,
            underruns,
            xruns,
            recoveries,
            write_failed,
        } = event;
        if frames_written > 0 {
            let uptime_ms = self.uptime_ms();
            self.chip_ref_frames_written
                .fetch_add(frames_written, Ordering::Relaxed);
            self.chip_ref_last_write_ms
                .store(uptime_ms, Ordering::Relaxed);
            if let Some(sequence) = reference_sequence {
                self.chip_ref_last_written_reference_sequence
                    .store(sequence, Ordering::Relaxed);
            }
            if let Some(delay_frames) = delay_frames {
                self.chip_ref_snd_pcm_delay_frames
                    .store(delay_frames, Ordering::Relaxed);
                self.chip_ref_snd_pcm_delay_sample_ms
                    .store(uptime_ms, Ordering::Relaxed);
                // Tick the passive SRO estimator here — the chip-ref delay is
                // freshly sampled. Read the already-stored DAC counters; this
                // never touches the audio path. A fresh chip-ref consumed
                // count (frames_written includes this write) pairs with the
                // most recent DAC snapshot. Both deltas are well below the
                // estimator's monotonicity/plausibility guards.
                let dac_written = self.dac_frames_written.load(Ordering::Relaxed);
                let dac_delay =
                    unpack_optional_u64(self.dac_snd_pcm_delay_frames.load(Ordering::Relaxed));
                let chip_ref_written = self.chip_ref_frames_written.load(Ordering::Relaxed);
                let chip_ref_rate = self.chip_ref_sample_rate.load(Ordering::Relaxed) as u32;
                // Only feed once the DAC has a real delay sample; otherwise the
                // pair is incomplete and the estimator should keep observing.
                if let Some(dac_delay) = dac_delay {
                    // Decimate to ~1 sample/sec. `mark_chip_ref_write` runs once
                    // per chip-ref period (~50 Hz); feeding the estimator that
                    // fast collapses its slope baseline to a fraction of a
                    // second, where snd_pcm_delay measurement jitter swamps the
                    // ppm estimate. The estimator's window is tuned for ~1 Hz,
                    // so only feed once ~1 s of chip-ref frames has elapsed.
                    // `mark_chip_ref_write` is called only from the single
                    // chip-ref writer thread, so this load/compare/store gate
                    // needs no compare-and-swap. (Single-DAC today; a future
                    // multi-clock-domain composite sink would need a per-child
                    // estimator + tick path, not one shared estimator.)
                    let interval = u64::from(chip_ref_rate.max(1));
                    let last_fed = self.sro_last_fed_chip_ref_frames.load(Ordering::Relaxed);
                    if chip_ref_written.saturating_sub(last_fed) >= interval {
                        self.sro_last_fed_chip_ref_frames
                            .store(chip_ref_written, Ordering::Relaxed);
                        if let Ok(mut est) = self.sro_estimator.lock() {
                            est.update(
                                dac_written,
                                dac_delay,
                                chip_ref_written,
                                delay_frames,
                                chip_ref_rate,
                            );
                        }
                    }
                }
            }
        }
        if underruns > 0 {
            self.chip_ref_write_underrun_count
                .fetch_add(underruns, Ordering::Relaxed);
        }
        if xruns > 0 {
            self.chip_ref_write_xrun_count
                .fetch_add(xruns, Ordering::Relaxed);
        }
        if recoveries > 0 {
            self.chip_ref_write_recovery_count
                .fetch_add(recoveries, Ordering::Relaxed);
        }
        if write_failed {
            self.chip_ref_write_error_count
                .fetch_add(1, Ordering::Relaxed);
        }
    }

    pub fn mark_chip_ref_tee_opened(&self) {
        self.chip_ref_tee_active.store(true, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_tee_open_error(&self) {
        self.chip_ref_tee_active.store(false, Ordering::Relaxed);
        self.chip_ref_tee_open_error_count
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_tee_write_error(&self) {
        self.chip_ref_tee_active.store(false, Ordering::Relaxed);
        self.chip_ref_tee_write_error_count
            .fetch_add(1, Ordering::Relaxed);
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

    pub fn mark_local_content_pipe(&self, metrics: LocalContentPipeMetrics) {
        self.local_content_pipe_open
            .store(metrics.open, Ordering::Relaxed);
        self.local_content_pipe_requested_pipe_bytes
            .store(metrics.requested_pipe_bytes, Ordering::Relaxed);
        self.local_content_pipe_open_failures
            .store(metrics.open_failures, Ordering::Relaxed);
        self.local_content_pipe_read_failures
            .store(metrics.read_failures, Ordering::Relaxed);
        self.local_content_pipe_reopen_count
            .store(metrics.reopen_count, Ordering::Relaxed);
        self.local_content_pipe_startup_empty_periods
            .store(metrics.startup_empty_periods, Ordering::Relaxed);
        self.local_content_pipe_empty_periods
            .store(metrics.empty_periods, Ordering::Relaxed);
        self.local_content_pipe_partial_periods
            .store(metrics.partial_periods, Ordering::Relaxed);
        self.local_content_pipe_misaligned_bytes
            .store(metrics.misaligned_bytes, Ordering::Relaxed);
        self.local_content_pipe_available_bytes
            .store(metrics.available_bytes, Ordering::Relaxed);
        self.local_content_pipe_actual_pipe_bytes
            .store(metrics.actual_pipe_bytes, Ordering::Relaxed);
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
        // The rate controller's shared-DLL rate_diff (Inc 4). `try_lock` so this
        // per-period mark never blocks on a concurrent /state read; on the rare
        // contention we skip one update (the next period refreshes it).
        if let Ok(mut slot) = self.content_bridge_rate_diff.try_lock() {
            *slot = metrics.rate_diff;
        }
    }

    pub fn snapshot_json(&self) -> String {
        let mut buf = String::with_capacity(1024);
        let uptime_ms = self.uptime_ms();
        let sample_rate = self.sample_rate.load(Ordering::Relaxed);
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
        push_kv_str(
            &mut buf,
            "source",
            if self.local_content_pipe.is_some() {
                "local_pipe"
            } else {
                "alsa"
            },
        );
        buf.push(',');
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
        if let Some(pipe) = self.local_content_pipe.as_deref() {
            buf.push(',');
            buf.push_str(r#""local_pipe":{"#);
            push_kv_bool(&mut buf, "enabled", self.local_content_pipe.is_some());
            buf.push(',');
            push_kv_str(&mut buf, "path", pipe);
            buf.push(',');
            push_kv_str(&mut buf, "format", LOCAL_CONTENT_PIPE_FORMAT);
            buf.push(',');
            push_kv_bool(
                &mut buf,
                "open",
                self.local_content_pipe_open.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "requested_pipe_bytes",
                self.local_content_pipe_requested_pipe_bytes
                    .load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "available_bytes",
                self.local_content_pipe_available_bytes
                    .load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "actual_pipe_bytes",
                self.local_content_pipe_actual_pipe_bytes
                    .load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "reopen_count",
                self.local_content_pipe_reopen_count.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "startup_empty_periods",
                self.local_content_pipe_startup_empty_periods
                    .load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "empty_periods",
                self.local_content_pipe_empty_periods
                    .load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "partial_periods",
                self.local_content_pipe_partial_periods
                    .load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "misaligned_bytes",
                self.local_content_pipe_misaligned_bytes
                    .load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "open_failures",
                self.local_content_pipe_open_failures
                    .load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "read_failures",
                self.local_content_pipe_read_failures
                    .load(Ordering::Relaxed),
            );
            buf.push('}');
        }
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
        buf.push(',');
        // The rate controller's shared-DLL rate_diff (Inc 4) — the loop's OWN
        // ppm / error stats / bandwidth / lock+resync counters, in the same
        // shape every DLL site publishes. `try_lock`; on contention emit the
        // idle placeholder rather than block or panic.
        let cb_rate_diff = self
            .content_bridge_rate_diff
            .try_lock()
            .map(|s| *s)
            .unwrap_or_else(|_| DllSnapshot::idle());
        push_dll_rate_diff(&mut buf, "rate_diff", &cb_rate_diff);
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
                match self.dac_content_highpass_hz {
                    Some(hz) => buf.push_str(&format!("\"main_highpass_hz\":{hz:.1}")),
                    None => buf.push_str("\"main_highpass_hz\":null"),
                }
                buf.push(',');
                buf.push_str(&format!("\"trim_db\":{:.1}", self.dac_content_trim_db()));
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

        // Bonded-member TTS lane (PR-2) — daemon truth for /state +
        // doctor. enabled:false when the lane is off (solo: fanin owns
        // TTS) — zero noise, mirroring dac_content.
        buf.push_str(r#""tts":{"#);
        match self.tts.get() {
            Some((socket, m)) => {
                push_kv_bool(&mut buf, "enabled", true);
                buf.push(',');
                push_kv_str(&mut buf, "socket", socket);
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "pending_frames",
                    m.pending_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(&mut buf, "budget_frames", m.max_pending_frames);
                buf.push(',');
                push_kv_u64(&mut buf, "requests", m.requests.load(Ordering::Relaxed));
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "dropped_audio_frames",
                    m.dropped_audio_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "dropped_commands",
                    m.dropped_commands.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "flush_requests",
                    m.flush_requests.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "flushed_frames",
                    m.flushed_frames.load(Ordering::Relaxed),
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
        push_kv_u64(&mut buf, "sample_rate", sample_rate);
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
        let dac_delay_frames =
            unpack_optional_u64(self.dac_snd_pcm_delay_frames.load(Ordering::Relaxed));
        push_kv_u64_opt(&mut buf, "snd_pcm_delay_frames", dac_delay_frames);
        buf.push(',');
        push_kv_f64_opt(
            &mut buf,
            "snd_pcm_delay_ms",
            frames_to_ms_opt(dac_delay_frames, sample_rate),
            3,
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "snd_pcm_delay_sample_age_ms",
            event_age_ms(
                uptime_ms,
                self.dac_snd_pcm_delay_sample_ms.load(Ordering::Relaxed),
            ),
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
            push_kv_bool(&mut buf, "linked", self.dual_linked.load(Ordering::Relaxed));
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
        push_kv_str(
            &mut buf,
            "speaker_reference_source",
            "outputd_final_electrical",
        );
        buf.push(',');
        push_kv_bool(&mut buf, "speaker_reference_is_fallback", false);
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "speaker_reference_active",
            self.chip_ref_pcm.is_some() || self.reference_udp_target.is_some(),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "speaker_reference_sample_rate", sample_rate);
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
        let chip_ref_sample_rate = self.chip_ref_sample_rate.load(Ordering::Relaxed);
        let chip_ref_delay_frames =
            unpack_optional_u64(self.chip_ref_snd_pcm_delay_frames.load(Ordering::Relaxed));
        let chip_ref_last_written_sequence = unpack_optional_u64(
            self.chip_ref_last_written_reference_sequence
                .load(Ordering::Relaxed),
        );
        let chip_ref_last_enqueued_sequence = unpack_optional_u64(
            self.chip_ref_last_enqueued_reference_sequence
                .load(Ordering::Relaxed),
        );
        let reference_sequence = self.reference_sequence.load(Ordering::Relaxed);
        let chip_ref_sequence_lag = chip_ref_last_written_sequence
            .map(|written| reference_sequence.saturating_sub(written));
        buf.push_str(r#""chip_ref_writer":{"#);
        push_kv_bool(&mut buf, "enabled", self.chip_ref_pcm.is_some());
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "queue_depth_periods",
            self.chip_ref_queue_depth_periods.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "queued_frames",
            self.chip_ref_queued_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "frames_written",
            self.chip_ref_frames_written.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64_opt(&mut buf, "snd_pcm_delay_frames", chip_ref_delay_frames);
        buf.push(',');
        push_kv_f64_opt(
            &mut buf,
            "snd_pcm_delay_ms",
            frames_to_ms_opt(chip_ref_delay_frames, chip_ref_sample_rate),
            3,
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "snd_pcm_delay_sample_age_ms",
            event_age_ms(
                uptime_ms,
                self.chip_ref_snd_pcm_delay_sample_ms
                    .load(Ordering::Relaxed),
            ),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "write_underrun_count",
            self.chip_ref_write_underrun_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "write_xrun_count",
            self.chip_ref_write_xrun_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "write_recovery_count",
            self.chip_ref_write_recovery_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "write_error_count",
            self.chip_ref_write_error_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "dropped_periods_due_to_full_queue",
            self.chip_ref_dropped_full_periods.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "dropped_periods_due_to_disconnected_writer",
            self.chip_ref_dropped_disconnected_periods
                .load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_write_age_ms",
            event_age_ms(
                uptime_ms,
                self.chip_ref_last_write_ms.load(Ordering::Relaxed),
            ),
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_enqueued_reference_sequence",
            chip_ref_last_enqueued_sequence,
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_written_reference_sequence",
            chip_ref_last_written_sequence,
        );
        buf.push(',');
        push_kv_u64_opt(&mut buf, "reference_sequence_lag", chip_ref_sequence_lag);
        buf.push(',');
        push_kv_str_opt(
            &mut buf,
            "diagnostic_tee_path",
            self.chip_ref_diagnostic_tee_path.as_deref(),
        );
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "diagnostic_tee_active",
            self.chip_ref_tee_active.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "diagnostic_tee_open_error_count",
            self.chip_ref_tee_open_error_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "diagnostic_tee_write_error_count",
            self.chip_ref_tee_write_error_count.load(Ordering::Relaxed),
        );
        buf.push('}');
        buf.push(',');
        push_kv_str_opt(&mut buf, "udp_target", self.reference_udp_target.as_deref());
        buf.push(',');

        // Passive chip-AEC clock drift (Layer 0). Observe-only: SRO estimate,
        // a thin verdict, and the latency budget outputd already knows. No
        // audio path is affected by anything in this block.
        // Read the Copy snapshot under the lock, then build the human-readable
        // reason string AFTER releasing it — never allocate while the lock is
        // held, so a /state query can't make the chip-ref writer wait on a heap
        // allocation. (It is off the audio path either way, but this keeps the
        // lock hold to a few non-allocating reads.)
        let (sro_ppm, sro_status, verdict, poisoned) = match self.sro_estimator.lock() {
            Ok(est) => (est.sro_ppm(), est.status(), est.verdict(), false),
            // A poisoned lock should never happen (the estimator never panics),
            // but never surface a panic from /state — degrade to fallback.
            Err(_) => (
                None,
                crate::aec_clock::SroStatus::Untrusted,
                crate::aec_clock::AecClockVerdict::Fallback,
                true,
            ),
        };
        let verdict_reason = if poisoned {
            "sro estimator lock poisoned".to_string()
        } else {
            crate::aec_clock::verdict_reason_for(sro_status, sro_ppm)
        };
        let sro_status = sro_status.as_str();
        let verdict = verdict.as_str();
        // Observe-only DAC playout-clock drift (Inc 2). `try_lock` so a
        // /state read never blocks the playback loop's tick; on contention or a
        // (never-expected) poisoned lock, report a not-yet-locked idle snapshot
        // rather than waiting or panicking. We read BOTH the AEC-specific
        // snapshot (the named ppm + verdict) and the raw shared-DLL snapshot
        // (the Inc-4 rate_diff) under one lock acquisition.
        let (dac_clock, dac_clock_rate_diff) = match self.dac_clock.try_lock() {
            Ok(clock) => (clock.snapshot(), clock.dll_snapshot()),
            Err(_) => (
                crate::dac_clock::DacClockSnapshot {
                    locked: false,
                    sro_ppm: 0.0,
                    error_mean: 0.0,
                    error_var: 0.0,
                    updates: 0,
                    resync_count: 0,
                },
                DllSnapshot::idle(),
            ),
        };
        let dac_presentation_ms = frames_to_ms_opt(dac_delay_frames, sample_rate);
        let playback_queue_ms = frames_to_ms_opt(
            Some(self.dac_buffer_frames.load(Ordering::Relaxed)),
            sample_rate,
        );
        let chip_ref_queue_ms = frames_to_ms_opt(
            Some(self.chip_ref_queued_frames.load(Ordering::Relaxed)),
            chip_ref_sample_rate,
        );
        buf.push_str(r#""aec_clock":{"#);
        push_kv_f64_opt(&mut buf, "chip_ref_sro_ppm", sro_ppm, 3);
        buf.push(',');
        push_kv_str(&mut buf, "sro_estimator_status", sro_status);
        buf.push(',');
        push_kv_str(&mut buf, "verdict", verdict);
        buf.push(',');
        push_kv_str(&mut buf, "verdict_reason", &verdict_reason);
        buf.push(',');
        // Observe-only label: the chip-ref writer was armed purely to MEASURE
        // drift on the DAC playout clock vs nominal (not chip-AEC). Pure
        // self-description; no audio path reads it.
        push_kv_bool(&mut buf, "observe", self.chip_ref_observe);
        buf.push(',');
        // Observe-only DAC playout-clock drift (Inc 2): the shared
        // jasper-clock DLL locked onto the :9891-reference-vs-DAC-playout error,
        // surfaced as ppm + lock + verdict (the doctor-readable field). This
        // NEVER warps audio — it is the measure-before-fix signal for software
        // AEC, distinct from the chip-AEC `chip_ref_sro_ppm` above.
        buf.push_str(r#""dac_clock":{"#);
        // AEC-specific surface: the named ppm + the doctor-readable verdict.
        push_kv_f64(&mut buf, "dac_clock_ppm", dac_clock.sro_ppm, 3);
        buf.push(',');
        push_kv_bool(&mut buf, "locked", dac_clock.locked);
        buf.push(',');
        push_kv_str(&mut buf, "verdict", dac_clock.verdict());
        buf.push(',');
        // Shared rate_diff (Inc 4): the loop's full state in the one consistent
        // shape — error stats, bandwidth, DLL lock/resync counters — so this DLL
        // site reads identically to the content-bridge controller's. The named
        // ppm above and `rate_diff.ppm` are the same value (the AEC surface
        // keeps the named field for the doctor; the shape stays DRY).
        push_dll_rate_diff(&mut buf, "rate_diff", &dac_clock_rate_diff);
        buf.push('}');
        buf.push(',');
        buf.push_str(r#""latency":{"#);
        push_kv_f64_opt(&mut buf, "dac_presentation_ms", dac_presentation_ms, 3);
        buf.push(',');
        push_kv_f64_opt(&mut buf, "playback_queue_ms", playback_queue_ms, 3);
        buf.push(',');
        push_kv_f64_opt(&mut buf, "chip_ref_queue_ms", chip_ref_queue_ms, 3);
        buf.push('}');
        buf.push('}');
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
        let response = if command == "STATUS" {
            self.state.snapshot_json()
        } else if let Some(raw) = command.strip_prefix("SET_DAC_CONTENT_TRIM_DB ") {
            match raw.trim().parse::<f32>() {
                Ok(trim_db) => match self.state.set_dac_content_trim_db(trim_db) {
                    Ok(applied) => format!(r#"{{"ok":true,"trim_db":{applied:.1}}}"#),
                    Err(e) => format!(r#"{{"error":"{}"}}"#, escape_json(&e.to_string())),
                },
                Err(_) => format!(
                    r#"{{"error":"trim_db must be a number","received":"{}"}}"#,
                    escape_json(raw.trim())
                ),
            }
        } else {
            format!(
                r#"{{"error":"unknown command","received":"{}"}}"#,
                escape_json(command)
            )
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

fn push_kv_f64_opt(buf: &mut String, key: &str, value: Option<f64>, decimals: usize) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
    match value {
        Some(value) => buf.push_str(&format!("{:.*}", decimals, value)),
        None => buf.push_str("null"),
    }
}

/// The ONE shared `clock.rate_diff` telemetry writer (Inc 4). Every DLL
/// instance in outputd publishes its loop state through this single shape, so
/// `/state` / doctor read every clock-domain boundary identically (mirrors
/// PipeWire's `clock.rate_diff`). Emits a nested object under `key`:
/// `{ppm, error_mean, error_var, bandwidth, locked, updates, lock_count,
/// unlock_count, resync_count}`.
fn push_dll_rate_diff(buf: &mut String, key: &str, snap: &DllSnapshot) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":{"#);
    push_kv_f64(buf, "ppm", snap.ratio_ppm, 3);
    buf.push(',');
    push_kv_f64(buf, "error_mean", snap.error_mean, 4);
    buf.push(',');
    push_kv_f64(buf, "error_var", snap.error_var, 4);
    buf.push(',');
    push_kv_f64(buf, "bandwidth", snap.bandwidth, 4);
    buf.push(',');
    push_kv_bool(buf, "locked", snap.locked);
    buf.push(',');
    push_kv_u64(buf, "updates", snap.updates);
    buf.push(',');
    push_kv_u64(buf, "lock_count", snap.lock_count);
    buf.push(',');
    push_kv_u64(buf, "unlock_count", snap.unlock_count);
    buf.push(',');
    push_kv_u64(buf, "resync_count", snap.resync_count);
    buf.push('}');
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

fn unpack_optional_u64(value: u64) -> Option<u64> {
    if value == OPTIONAL_U64_NONE {
        None
    } else {
        Some(value)
    }
}

fn frames_to_ms_opt(frames: Option<u64>, sample_rate: u64) -> Option<f64> {
    if sample_rate == 0 {
        return None;
    }
    frames.map(|frames| (frames as f64) * 1000.0 / (sample_rate as f64))
}

fn subtract_saturating(value: &AtomicU64, delta: u64) {
    let _ = value.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
        Some(current.saturating_sub(delta))
    });
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
            local_content_pipe: None,
            local_content_pipe_bytes: 8192,
            chip_ref_pcm: None,
            chip_ref_sample_rate: 16_000,
            chip_ref_period_frames: 320,
            chip_ref_buffer_frames: 4096,
            chip_ref_observe: false,
            chip_ref_tee_path: None,
            reference_udp_target: None,
            stream_id: 1,
            control_socket_path: None,
            dac_content_fifo: None,
            dac_content_channel: crate::dac_content::ChannelPick::Stereo,
            dac_content_highpass_hz: None,
            dac_content_trim_db: 0.0,
            tts_socket_path: None,
            tts_max_pending_frames: crate::tts::DEFAULT_MAX_PENDING_FRAMES,
            tts_program_duck_db: -25.0,
            active_lane: false,
        }
    }

    fn dual_test_config() -> Config {
        Config {
            sink_mode: SinkMode::Composite,
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
            r#""content":{"source":"alsa","pcm":"outputd_content_capture""#,
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
            r#""snd_pcm_delay_frames":null"#,
            r#""snd_pcm_delay_ms":null"#,
            r#""snd_pcm_delay_sample_age_ms":null"#,
            r#""reference_sequence":42"#,
            r#""last_period_clipped_samples":3"#,
            r#""clipped_samples":3"#,
            r#""reference_outputs":{"speaker_reference_source":"outputd_final_electrical""#,
            r#""chip_ref_pcm":null"#,
            r#""chip_ref_sample_rate":16000"#,
            r#""chip_ref_period_frames":320"#,
            r#""chip_ref_buffer_frames":4096"#,
            r#""chip_ref_writer":{"enabled":false"#,
            r#""queue_depth_periods":0"#,
            r#""queued_frames":0"#,
            r#""dropped_periods_due_to_full_queue":0"#,
            r#""last_enqueued_reference_sequence":null"#,
            r#""last_written_reference_sequence":null"#,
            r#""reference_sequence_lag":null"#,
            r#""diagnostic_tee_path":null"#,
            r#""diagnostic_tee_active":false"#,
            r#""diagnostic_tee_open_error_count":0"#,
            r#""diagnostic_tee_write_error_count":0"#,
            r#""udp_target":null"#,
            r#""watchdog""#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        // PR-2 (Increment 5) UN-RETIRED the outputd TTS lane that 9102e13
        // removed — this assertion used to pin its absence; it now pins
        // the solo shape: present but disabled (fanin owns solo TTS).
        assert!(
            j.contains(r#""tts":{"enabled":false}"#),
            "solo tts block must be present-but-disabled in {j}"
        );
        assert!(
            !j.contains(r#""assistant_loudness":"#),
            "duplicate outputd loudness state present in {j}"
        );
    }

    #[test]
    fn snapshot_json_reports_local_content_pipe_metrics() {
        let cfg = Config {
            local_content_pipe: Some("/run/jasper-outputd/content.pipe".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_period(
            IoCounters {
                content_frames_read: 512,
                content_empty_period_count: 2,
                content_partial_period_count: 1,
                content_eagain_count: 0,
                dac_frames_written: 512,
                content_xrun_count: 0,
                dac_xrun_count: 0,
            },
            7,
            0,
        );
        state.mark_local_content_pipe(LocalContentPipeMetrics {
            enabled: true,
            open: true,
            frames_read: 512,
            requested_pipe_bytes: 4096,
            open_failures: 3,
            read_failures: 4,
            reopen_count: 5,
            startup_empty_periods: 8,
            empty_periods: 2,
            partial_periods: 1,
            misaligned_bytes: 6,
            available_bytes: 128,
            actual_pipe_bytes: 4096,
        });

        let j = state.snapshot_json();
        for needle in [
            r#""content":{"source":"local_pipe""#,
            r#""local_pipe":{"enabled":true"#,
            r#""path":"/run/jasper-outputd/content.pipe""#,
            r#""format":"S32_LE""#,
            r#""open":true"#,
            r#""requested_pipe_bytes":4096"#,
            r#""available_bytes":128"#,
            r#""actual_pipe_bytes":4096"#,
            r#""reopen_count":5"#,
            r#""startup_empty_periods":8"#,
            r#""empty_periods":2"#,
            r#""partial_periods":1"#,
            r#""misaligned_bytes":6"#,
            r#""open_failures":3"#,
            r#""read_failures":4"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
    }

    #[test]
    fn snapshot_json_contains_dual_apple_runtime_health() {
        let state = OutputdState::new(&dual_test_config());
        state.mark_dual_apple_status(&CompositeStatus {
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
            dac_content_highpass_hz: Some(80.0),
            dac_content_trim_db: -3.5,
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
            r#""trim_db":-3.5"#,
            r#""main_highpass_hz":80.0"#,
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
    fn dac_content_trim_can_update_live_without_restarting_outputd() {
        let cfg = Config {
            dac_content_fifo: Some("/run/jasper-grouping/member-content.fifo".to_string()),
            dac_content_channel: crate::dac_content::ChannelPick::Right,
            dac_content_trim_db: 0.0,
            ..test_config()
        };
        let state = OutputdState::new(&cfg);

        assert_eq!(state.dac_content_trim_db(), 0.0);
        assert_eq!(state.dac_content_trim_gain(), 1.0);

        assert_eq!(state.set_dac_content_trim_db(-3.54).unwrap(), -3.5);

        let expected_gain = 10f32.powf(-3.5 / 20.0);
        assert_eq!(state.dac_content_trim_db(), -3.5);
        assert!((state.dac_content_trim_gain() - expected_gain).abs() < 0.000_001);
        assert!(state.snapshot_json().contains(r#""trim_db":-3.5"#));
    }

    #[test]
    fn dac_content_trim_live_update_rejects_disabled_or_boosting_lane() {
        let state = OutputdState::new(&test_config());
        assert!(state.set_dac_content_trim_db(-1.0).is_err());

        let cfg = Config {
            dac_content_fifo: Some("/run/jasper-grouping/member-content.fifo".to_string()),
            dac_content_channel: crate::dac_content::ChannelPick::Left,
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        assert!(state.set_dac_content_trim_db(0.1).is_err());
        assert!(state.set_dac_content_trim_db(-24.1).is_err());
        assert!(state.set_dac_content_trim_db(f32::NAN).is_err());
        assert_eq!(state.dac_content_trim_db(), 0.0);
    }

    #[test]
    fn snapshot_json_tts_disabled_is_quiet_and_enabled_is_full() {
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        assert!(
            j.contains(r#""tts":{"enabled":false}"#),
            "missing quiet disabled tts block in {j}"
        );

        let state = OutputdState::new(&test_config());
        let metrics = TtsMetrics::new(96_000);
        metrics.pending_frames.store(123, Ordering::Relaxed);
        metrics.flushed_frames.store(7, Ordering::Relaxed);
        state.set_tts("/run/jasper-outputd/tts.sock".to_string(), metrics);
        let j = state.snapshot_json();
        for needle in [
            r#""tts":{"enabled":true"#,
            r#""socket":"/run/jasper-outputd/tts.sock""#,
            r#""pending_frames":123"#,
            r#""budget_frames":96000"#,
            r#""flushed_frames":7"#,
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
    fn snapshot_json_reports_dac_delay_and_chip_ref_writer_counters() {
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            chip_ref_tee_path: Some("/tmp/outputd-chip-ref.s16le".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_period(IoCounters::default(), 12, 0);
        state.mark_dac_delay(240);
        state.mark_chip_ref_queue_admitted(320);
        state.mark_chip_ref_enqueued(10);
        state.mark_chip_ref_dequeued(320);
        state.mark_chip_ref_write(ChipRefWrite {
            frames_written: 320,
            delay_frames: Some(640),
            reference_sequence: Some(10),
            underruns: 1,
            xruns: 1,
            recoveries: 1,
            write_failed: false,
        });
        state.mark_chip_ref_dropped_full();
        state.mark_chip_ref_dropped_disconnected();
        state.mark_chip_ref_tee_open_error();
        state.mark_chip_ref_tee_opened();
        state.mark_chip_ref_tee_write_error();

        let j = state.snapshot_json();
        for needle in [
            r#""snd_pcm_delay_frames":240"#,
            r#""snd_pcm_delay_ms":5.000"#,
            r#""chip_ref_writer":{"enabled":true"#,
            r#""queue_depth_periods":0"#,
            r#""queued_frames":0"#,
            r#""frames_written":320"#,
            r#""snd_pcm_delay_frames":640"#,
            r#""snd_pcm_delay_ms":40.000"#,
            r#""write_underrun_count":1"#,
            r#""write_xrun_count":1"#,
            r#""write_recovery_count":1"#,
            r#""write_error_count":0"#,
            r#""dropped_periods_due_to_full_queue":1"#,
            r#""dropped_periods_due_to_disconnected_writer":1"#,
            r#""last_enqueued_reference_sequence":10"#,
            r#""last_written_reference_sequence":10"#,
            r#""reference_sequence_lag":2"#,
            r#""diagnostic_tee_path":"/tmp/outputd-chip-ref.s16le""#,
            r#""diagnostic_tee_active":false"#,
            r#""diagnostic_tee_open_error_count":1"#,
            r#""diagnostic_tee_write_error_count":1"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        assert!(!j.contains(r#""last_write_age_ms":null"#), "{j}");
    }

    #[test]
    fn snapshot_json_reports_never_for_missing_xruns() {
        let state = OutputdState::new(&test_config());
        state.mark_period(IoCounters::default(), 1, 0);

        let j = state.snapshot_json();
        assert_eq!(j.matches(r#""last_xrun_age_ms":null"#).count(), 2);
        assert_eq!(j.matches(r#""xrun_rate_per_hour":0.000"#).count(), 2);
    }

    #[test]
    fn snapshot_json_aec_clock_observing_by_default() {
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        for needle in [
            r#""aec_clock":{"chip_ref_sro_ppm":null"#,
            r#""sro_estimator_status":"observing""#,
            r#""verdict":"fallback""#,
            // Observe mode is off in test_config (default).
            r#""observe":false"#,
            // Inc 2: observe-only DAC playout-clock drift — fresh, not yet
            // locked, ppm 0, acquiring verdict.
            r#""dac_clock":{"dac_clock_ppm":0.000"#,
            r#""locked":false"#,
            // Inc 4: the shared rate_diff shape, idle placeholder values.
            r#""rate_diff":{"ppm":0.000"#,
            r#""bandwidth":0.1280"#,
            r#""updates":0"#,
            r#""resync_count":0"#,
            r#""latency":{"dac_presentation_ms":null"#,
            // test_config dac_buffer_frames=3072 / 48000 → 64 ms.
            r#""playback_queue_ms":64.000"#,
            r#""chip_ref_queue_ms":0.000"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        // The dac_clock block's verdict is "acquiring" before lock; it
        // sits between observe and latency.
        assert!(
            j.contains(r#""dac_clock":{"#) && j.contains(r#""verdict":"acquiring""#),
            "dac_clock block must be present with an acquiring verdict: {j}"
        );
    }

    #[test]
    fn every_dll_site_publishes_the_same_rate_diff_shape() {
        // Inc 4: both DLL instances (the content-bridge rate controller and the
        // DAC-clock observer) publish their loop state through the
        // single shared `rate_diff` writer. The shape must appear under BOTH
        // blocks with the same field set, so /state / doctor read every
        // clock-domain boundary identically.
        let state = OutputdState::new(&test_config());
        // Push a content-bridge metrics sample so its rate_diff slot is set.
        state.mark_content_bridge(ContentBridgeMetrics {
            locked: true,
            ring_capacity_frames: 16_384,
            target_fill_frames: 4096,
            fill_frames: 4096,
            min_fill_frames: 4000,
            max_fill_frames: 4200,
            ratio_ppm: 12.5,
            input_frames: 1000,
            output_frames: 1000,
            silence_frames: 0,
            underrun_frames: 0,
            overrun_frames: 0,
            resync_count: 0,
            reset_count: 0,
            ratio_clamp_count: 0,
            lock_count: 1,
            unlock_count: 0,
            rate_diff: jasper_clock::DllSnapshot {
                ratio: 1.000_05,
                ratio_ppm: 50.0,
                error_mean: -0.5,
                error_var: 0.25,
                bandwidth: 0.05,
                locked: true,
                updates: 1234,
                lock_count: 1,
                unlock_count: 0,
                resync_count: 2,
            },
        });
        let j = state.snapshot_json();
        // Exactly two rate_diff objects: content_bridge + dac_clock.
        assert_eq!(
            j.matches(r#""rate_diff":{"ppm":"#).count(),
            2,
            "exactly two rate_diff blocks (one per DLL site) in {j}"
        );
        // The content-bridge rate_diff carries the loop's OWN values.
        assert!(
            j.contains(r#""rate_diff":{"ppm":50.000,"error_mean":-0.5000,"error_var":0.2500,"bandwidth":0.0500,"locked":true,"updates":1234,"lock_count":1,"unlock_count":0,"resync_count":2}"#),
            "content_bridge rate_diff must carry the DLL snapshot fields: {j}"
        );
    }

    #[test]
    fn mark_dac_delay_ticks_dac_clock_without_panicking() {
        // Wiring test: the mark_dac_delay path (the playback loop's tick) feeds
        // the observe-only DAC-clock observer DLL. The tick reads REAL
        // uptime for its wall-clock, so a unit test can't drive it to a
        // deterministic lock in a tight loop — the convergence math is pinned by
        // the dac_clock module's own tests. Here we assert the tick
        // path is exercised and never panics, and the block stays present.
        let state = OutputdState::new(&test_config());
        for step in 1..=64u64 {
            state.mark_period(
                IoCounters {
                    dac_frames_written: step * 48_000,
                    ..IoCounters::default()
                },
                1,
                0,
            );
            state.mark_dac_delay(1024);
        }
        let j = state.snapshot_json();
        assert!(j.contains(r#""dac_clock":{"#), "block present: {j}");
    }

    #[test]
    fn snapshot_json_aec_clock_observe_reflects_config() {
        // The `observe` label is a pure passthrough of config.chip_ref_observe
        // (set by the reconciler when it armed the chip-ref writer for drift
        // MEASUREMENT on the DAC playout clock (vs nominal)). It changes no behavior; this
        // pins both polarities so the wire contract can't silently drop it.
        let off = OutputdState::new(&test_config());
        assert!(
            off.snapshot_json().contains(r#""observe":false"#),
            "observe must be false when config.chip_ref_observe is off"
        );
        let cfg_on = Config {
            chip_ref_observe: true,
            ..test_config()
        };
        let on = OutputdState::new(&cfg_on);
        assert!(
            on.snapshot_json().contains(r#""observe":true"#),
            "observe must be true when config.chip_ref_observe is on"
        );
    }

    #[test]
    fn snapshot_json_aec_clock_locks_and_classifies_drift() {
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        // Drive enough paired DAC + chip-ref snapshots for the estimator to
        // lock. The DAC runs ~50 ppm fast relative to the 16 kHz chip ref.
        // Round the CUMULATIVE DAC target each step so the integer counter
        // tracks the true ppm (per-step rounding would bias the slope).
        for step in 1..=40u64 {
            let dac_written = (48_000.0 * step as f64 * (1.0 + 50.0 / 1.0e6)).round() as u64;
            // The DAC counters land via the playback-loop marks...
            state.mark_period(
                IoCounters {
                    dac_frames_written: dac_written,
                    ..IoCounters::default()
                },
                1,
                0,
            );
            state.mark_dac_delay(1024);
            // ...and the chip-ref write ticks the estimator with both pairs.
            state.mark_chip_ref_write(ChipRefWrite {
                frames_written: 16_000,
                delay_frames: Some(320),
                ..ChipRefWrite::default()
            });
        }
        let j = state.snapshot_json();
        assert!(
            j.contains(r#""sro_estimator_status":"locked""#),
            "estimator should lock in {j}"
        );
        assert!(
            j.contains(r#""verdict":"compensable""#),
            "50 ppm should classify compensable in {j}"
        );
        assert!(
            j.contains(r#""chip_ref_sro_ppm":50."#) || j.contains(r#""chip_ref_sro_ppm":49."#),
            "expected ~50 ppm estimate in {j}"
        );
    }

    #[test]
    fn aec_clock_decimates_sub_interval_chip_ref_writes() {
        // In production `mark_chip_ref_write` fires ~50 Hz, one ~320-frame
        // chip-ref period per call. The estimator must be fed only ~1 Hz, so
        // many sub-interval writes must NOT accumulate enough samples to lock:
        // without decimation 100 feeds would lock; with it ~2 feeds stay
        // observing. This pins the decimation gate.
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        let mut dac: u64 = 0;
        for _ in 0..100u64 {
            dac += 960; // 48k/16k ratio, clock-coherent
            state.mark_period(
                IoCounters {
                    dac_frames_written: dac,
                    ..IoCounters::default()
                },
                1,
                0,
            );
            state.mark_dac_delay(1024);
            state.mark_chip_ref_write(ChipRefWrite {
                frames_written: 320,
                delay_frames: Some(320),
                ..ChipRefWrite::default()
            });
        }
        let j = state.snapshot_json();
        assert!(
            j.contains(r#""sro_estimator_status":"observing""#),
            "100 sub-interval (~320-frame) writes decimate to ~2 feeds and must \
             stay observing, not lock: {j}"
        );
    }
}
