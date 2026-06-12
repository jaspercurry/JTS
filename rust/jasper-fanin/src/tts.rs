//! Pre-DSP TTS IPC for active-speaker topologies.
//!
//! The wire protocol intentionally matches `jasper-outputd`'s TTS
//! socket so Python can keep one playout implementation. fan-in only
//! owns the pre-DSP summing concern: it accepts 48 kHz stereo S16_LE
//! TTS/cue audio, clamps positive gain, and mixes it into the summed
//! program lane before CamillaDSP performs crossover/protection.

use std::collections::VecDeque;
use std::fs;
use std::io::{self, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result};
use log::{info, warn};

use jasper_tts_protocol::{command_name, read_command, TtsCommand};
use crate::loudness::{
    apply_gain_i16, clamp_tts_gain_db, gain_db_to_linear, linear_to_db,
    AssistantGainDecision, AssistantLoudness, AssistantLoudnessConfig,
    AssistantProfile, SegmentKind, MAX_TTS_GAIN_DB,
};
use crate::mixer::CHANNELS;

pub const TTS_COMMAND_QUEUE_CAPACITY: usize = 128;
pub const DEFAULT_MAX_PENDING_FRAMES: u64 = 48_000 * 2;
const PACKED_DB_NONE: i64 = i64::MIN;

#[derive(Debug)]
pub struct QueuedTtsCommand {
    pub epoch: u64,
    pub command: TtsCommand,
}

#[derive(Debug)]
pub struct QueuedFlush {
    pub epoch: u64,
    pub ack: Option<SyncSender<FlushSummary>>,
}

#[derive(Debug, Clone)]
pub struct FlushSummary {
    requests: usize,
    pending_frames: u64,
    flushed_frames: u64,
}

#[derive(Debug, Clone)]
pub struct TtsMetrics {
    pending_frames: Arc<AtomicU64>,
    max_pending_frames: Arc<AtomicU64>,
    budget_frames: Arc<AtomicU64>,
    dropped_commands: Arc<AtomicU64>,
    dropped_audio_frames: Arc<AtomicU64>,
    stale_commands_dropped: Arc<AtomicU64>,
    flush_requests: Arc<AtomicU64>,
    flushed_frames: Arc<AtomicU64>,
    content_short_lufs_x10: Arc<AtomicI64>,
    content_anchor_lufs_x10: Arc<AtomicI64>,
    assistant_decision_seen: Arc<AtomicBool>,
    assistant_calibrated: Arc<AtomicBool>,
    assistant_profile_confidence_x100: Arc<AtomicU64>,
    assistant_baseline_lufs_x10: Arc<AtomicI64>,
    assistant_target_lufs_x10: Arc<AtomicI64>,
    assistant_source_lufs_x10: Arc<AtomicI64>,
    assistant_source_peak_dbfs_x10: Arc<AtomicI64>,
    assistant_requested_gain_db_x10: Arc<AtomicI64>,
    assistant_peak_cap_gain_db_x10: Arc<AtomicI64>,
    assistant_final_gain_db_x10: Arc<AtomicI64>,
}

impl Default for TtsMetrics {
    fn default() -> Self {
        Self {
            pending_frames: Arc::new(AtomicU64::new(0)),
            max_pending_frames: Arc::new(AtomicU64::new(0)),
            budget_frames: Arc::new(AtomicU64::new(0)),
            dropped_commands: Arc::new(AtomicU64::new(0)),
            dropped_audio_frames: Arc::new(AtomicU64::new(0)),
            stale_commands_dropped: Arc::new(AtomicU64::new(0)),
            flush_requests: Arc::new(AtomicU64::new(0)),
            flushed_frames: Arc::new(AtomicU64::new(0)),
            content_short_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            content_anchor_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_decision_seen: Arc::new(AtomicBool::new(false)),
            assistant_calibrated: Arc::new(AtomicBool::new(false)),
            assistant_profile_confidence_x100: Arc::new(AtomicU64::new(0)),
            assistant_baseline_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_target_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_source_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_source_peak_dbfs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_requested_gain_db_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_peak_cap_gain_db_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_final_gain_db_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct TtsLoudnessSnapshot {
    pub content_short_lufs: Option<f64>,
    pub content_anchor_lufs: Option<f64>,
    pub decision_seen: bool,
    pub calibrated: bool,
    pub profile_confidence: f64,
    pub baseline_lufs: Option<f64>,
    pub target_lufs: Option<f64>,
    pub source_lufs: Option<f64>,
    pub source_peak_dbfs: Option<f64>,
    pub requested_gain_db: Option<f64>,
    pub peak_cap_gain_db: Option<f64>,
    pub final_gain_db: Option<f64>,
}

impl TtsMetrics {
    pub fn new(budget_frames: u64) -> Self {
        let metrics = Self::default();
        metrics
            .budget_frames
            .store(budget_frames, Ordering::Relaxed);
        metrics
    }

    pub fn pending_frames(&self) -> u64 {
        self.pending_frames.load(Ordering::Relaxed)
    }

    pub fn max_pending_frames(&self) -> u64 {
        self.max_pending_frames.load(Ordering::Relaxed)
    }

    pub fn budget_frames(&self) -> u64 {
        self.budget_frames.load(Ordering::Relaxed)
    }

    pub fn dropped_commands(&self) -> u64 {
        self.dropped_commands.load(Ordering::Relaxed)
    }

    pub fn dropped_audio_frames(&self) -> u64 {
        self.dropped_audio_frames.load(Ordering::Relaxed)
    }

    pub fn stale_commands_dropped(&self) -> u64 {
        self.stale_commands_dropped.load(Ordering::Relaxed)
    }

    pub fn flush_requests(&self) -> u64 {
        self.flush_requests.load(Ordering::Relaxed)
    }

    pub fn flushed_frames(&self) -> u64 {
        self.flushed_frames.load(Ordering::Relaxed)
    }

    pub fn loudness_snapshot(&self) -> TtsLoudnessSnapshot {
        let decision_seen = self.assistant_decision_seen.load(Ordering::Relaxed);
        TtsLoudnessSnapshot {
            content_short_lufs: unpack_optional_db(
                self.content_short_lufs_x10.load(Ordering::Relaxed),
            ),
            content_anchor_lufs: unpack_optional_db(
                self.content_anchor_lufs_x10.load(Ordering::Relaxed),
            ),
            decision_seen,
            calibrated: self.assistant_calibrated.load(Ordering::Relaxed),
            profile_confidence: (self
                .assistant_profile_confidence_x100
                .load(Ordering::Relaxed) as f64)
                / 100.0,
            baseline_lufs: if decision_seen {
                unpack_optional_db(self.assistant_baseline_lufs_x10.load(Ordering::Relaxed))
            } else {
                None
            },
            target_lufs: if decision_seen {
                unpack_optional_db(self.assistant_target_lufs_x10.load(Ordering::Relaxed))
            } else {
                None
            },
            source_lufs: if decision_seen {
                unpack_optional_db(self.assistant_source_lufs_x10.load(Ordering::Relaxed))
            } else {
                None
            },
            source_peak_dbfs: if decision_seen {
                unpack_optional_db(
                    self.assistant_source_peak_dbfs_x10.load(Ordering::Relaxed),
                )
            } else {
                None
            },
            requested_gain_db: if decision_seen {
                unpack_optional_db(
                    self.assistant_requested_gain_db_x10.load(Ordering::Relaxed),
                )
            } else {
                None
            },
            peak_cap_gain_db: if decision_seen {
                unpack_optional_db(
                    self.assistant_peak_cap_gain_db_x10.load(Ordering::Relaxed),
                )
            } else {
                None
            },
            final_gain_db: if decision_seen {
                unpack_optional_db(self.assistant_final_gain_db_x10.load(Ordering::Relaxed))
            } else {
                None
            },
        }
    }

    fn mark_pending(&self, frames: u64) {
        self.pending_frames.store(frames, Ordering::Relaxed);
        fetch_max(&self.max_pending_frames, frames);
    }

    fn mark_dropped_audio(&self, frames: u64) {
        self.dropped_commands.fetch_add(1, Ordering::Relaxed);
        self.dropped_audio_frames
            .fetch_add(frames, Ordering::Relaxed);
    }

    fn mark_stale_command_dropped(&self) {
        self.stale_commands_dropped.fetch_add(1, Ordering::Relaxed);
    }

    fn mark_flush(&self, requests: usize, frames: u64) {
        self.flush_requests
            .fetch_add(requests as u64, Ordering::Relaxed);
        self.flushed_frames.fetch_add(frames, Ordering::Relaxed);
    }

    fn mark_loudness(
        &self,
        content_short_lufs: Option<f32>,
        content_anchor_lufs: Option<f32>,
        decision: Option<&AssistantGainDecision>,
    ) {
        self.content_short_lufs_x10
            .store(pack_optional_db(content_short_lufs), Ordering::Relaxed);
        self.content_anchor_lufs_x10
            .store(pack_optional_db(content_anchor_lufs), Ordering::Relaxed);
        if let Some(decision) = decision {
            self.assistant_decision_seen.store(true, Ordering::Relaxed);
            self.assistant_calibrated
                .store(decision.calibrated, Ordering::Relaxed);
            self.assistant_profile_confidence_x100.store(
                (decision.profile_confidence.clamp(0.0, 1.0) * 100.0).round() as u64,
                Ordering::Relaxed,
            );
            self.assistant_baseline_lufs_x10
                .store(pack_optional_db(Some(decision.baseline_lufs)), Ordering::Relaxed);
            self.assistant_target_lufs_x10
                .store(pack_optional_db(Some(decision.target_lufs)), Ordering::Relaxed);
            self.assistant_source_lufs_x10
                .store(pack_optional_db(Some(decision.source_lufs)), Ordering::Relaxed);
            self.assistant_source_peak_dbfs_x10.store(
                pack_optional_db(Some(decision.source_peak_dbfs)),
                Ordering::Relaxed,
            );
            self.assistant_requested_gain_db_x10.store(
                pack_optional_db(Some(decision.requested_gain_db)),
                Ordering::Relaxed,
            );
            self.assistant_peak_cap_gain_db_x10.store(
                pack_optional_db(Some(decision.peak_cap_gain_db)),
                Ordering::Relaxed,
            );
            self.assistant_final_gain_db_x10
                .store(pack_optional_db(Some(decision.final_gain_db)), Ordering::Relaxed);
        }
    }
}

pub struct TtsInput {
    pub rx: Receiver<QueuedTtsCommand>,
    pub flush_rx: Receiver<QueuedFlush>,
    pub metrics: TtsMetrics,
    pub max_pending_frames: u64,
    pub program_duck_db: f32,
    pub assistant_loudness: AssistantLoudnessConfig,
}

pub struct TtsMixer {
    rx: Receiver<QueuedTtsCommand>,
    flush_rx: Receiver<QueuedFlush>,
    metrics: TtsMetrics,
    queue: VecDeque<i16>,
    current_gain_db: f32,
    active_epoch: u64,
    max_pending_frames: u64,
    program_duck_gain: f32,
    program_duck_active: bool,
    content_meter_paused: bool,
    active_segment_gain_db: Option<f32>,
    loudness: AssistantLoudness,
}

impl TtsMixer {
    pub fn new(input: TtsInput) -> Self {
        Self {
            rx: input.rx,
            flush_rx: input.flush_rx,
            metrics: input.metrics,
            queue: VecDeque::new(),
            current_gain_db: MAX_TTS_GAIN_DB,
            active_epoch: 0,
            max_pending_frames: input.max_pending_frames,
            program_duck_gain: gain_db_to_linear(input.program_duck_db),
            program_duck_active: false,
            content_meter_paused: false,
            active_segment_gain_db: None,
            loudness: AssistantLoudness::new(input.assistant_loudness),
        }
    }

    pub fn prepare_period(&mut self) -> bool {
        self.drain_flushes();
        self.drain_commands();
        self.program_duck_active || self.pending_frames() > 0
    }

    pub fn program_duck_gain(&self) -> f32 {
        self.program_duck_gain
    }

    pub fn observe_content_period(&mut self, samples: &[i16]) {
        if !self.content_meter_paused {
            self.loudness.observe_content_period(samples);
            self.metrics.mark_loudness(
                self.loudness.content_short_lufs(),
                self.loudness.content_anchor_lufs(),
                self.loudness.last_decision(),
            );
        }
    }

    pub fn mix_period(&mut self, sum: &mut [i32]) {
        for sample_sum in sum.iter_mut() {
            let Some(sample) = self.queue.pop_front() else {
                break;
            };
            *sample_sum = sample_sum.saturating_add(sample as i32);
        }
        self.metrics
            .mark_pending((self.queue.len() / (CHANNELS as usize)) as u64);
    }

    fn drain_commands(&mut self) {
        loop {
            let Ok(queued) = self.rx.try_recv() else {
                break;
            };
            let is_restore = matches!(
                &queued.command,
                TtsCommand::ProgramDuckOff | TtsCommand::ContentMeterResume
            );
            if queued.epoch != self.active_epoch && !is_restore {
                self.metrics.mark_stale_command_dropped();
                if !matches!(&queued.command, TtsCommand::Audio(_)) {
                    warn!(
                        "event=fanin.tts_command_dropped reason=stale_epoch command={} epoch={} active_epoch={}",
                        command_name(&queued.command),
                        queued.epoch,
                        self.active_epoch
                    );
                }
                continue;
            }
            match queued.command {
                TtsCommand::GainDb(db) => {
                    self.current_gain_db = clamp_tts_gain_db(db);
                }
                TtsCommand::Audio(samples) => {
                    if samples.is_empty() {
                        continue;
                    }
                    let incoming_frames =
                        (samples.len() / (CHANNELS as usize)) as u64;
                    if self.pending_frames().saturating_add(incoming_frames)
                        > self.max_pending_frames
                    {
                        self.metrics.mark_dropped_audio(incoming_frames);
                        warn!(
                            "event=fanin.tts_command_dropped reason=pending_budget_exceeded command=audio epoch={} frames={} pending_frames={} budget_frames={}",
                            queued.epoch,
                            incoming_frames,
                            self.pending_frames(),
                            self.max_pending_frames
                        );
                        continue;
                    }
                    let gain_db = match self.active_segment_gain_db {
                        Some(gain_db) => gain_db,
                        None => self.decide_segment_gain(SegmentKind::Assistant, None),
                    };
                    let gain = gain_db_to_linear(gain_db);
                    for sample in samples {
                        self.queue.push_back(apply_gain_i16(sample, gain));
                    }
                }
                TtsCommand::Flush | TtsCommand::FlushSync => {
                    let frames = self.clear_queue();
                    self.active_segment_gain_db = None;
                    self.metrics.mark_flush(1, frames);
                    info!(
                        "event=fanin.tts_flush requests=1 pending_frames={} flushed_frames={}",
                        frames, frames
                    );
                }
                TtsCommand::ProgramDuckOn => {
                    if !self.program_duck_active {
                        info!(
                            "event=fanin.program_duck on=true gain_db={:.1}",
                            linear_to_db(self.program_duck_gain)
                        );
                    }
                    self.program_duck_active = true;
                }
                TtsCommand::ProgramDuckOff => {
                    if self.program_duck_active {
                        info!("event=fanin.program_duck on=false");
                    }
                    self.program_duck_active = false;
                }
                TtsCommand::PrepareAssistant {
                    provider,
                    model,
                    voice,
                    silence_target_lufs,
                } => {
                    self.loudness.prepare_context(
                        provider,
                        model,
                        voice,
                        silence_target_lufs,
                    );
                    self.metrics.mark_loudness(
                        self.loudness.content_short_lufs(),
                        self.loudness.content_anchor_lufs(),
                        self.loudness.last_decision(),
                    );
                }
                TtsCommand::ContentMeterPause => {
                    self.content_meter_paused = true;
                }
                TtsCommand::ContentMeterResume => {
                    self.content_meter_paused = false;
                    self.loudness.clear_context();
                    self.metrics.mark_loudness(
                        self.loudness.content_short_lufs(),
                        self.loudness.content_anchor_lufs(),
                        self.loudness.last_decision(),
                    );
                }
                TtsCommand::SegmentStart { kind, profile, .. } => {
                    self.decide_segment_gain(kind, profile);
                }
                TtsCommand::SegmentEnd => {
                    self.active_segment_gain_db = None;
                }
                TtsCommand::Close => {}
            }
        }
        self.metrics.mark_pending(self.pending_frames());
    }

    fn decide_segment_gain(
        &mut self,
        kind: SegmentKind,
        profile: Option<AssistantProfile>,
    ) -> f32 {
        let decision =
            self.loudness
                .decide_gain(kind, self.current_gain_db, profile);
        let gain_db = decision.final_gain_db;
        log_assistant_loudness_decision(kind, &decision);
        self.metrics.mark_loudness(
            self.loudness.content_short_lufs(),
            self.loudness.content_anchor_lufs(),
            Some(&decision),
        );
        self.active_segment_gain_db = Some(gain_db);
        gain_db
    }

    fn drain_flushes(&mut self) {
        let mut requests = 0usize;
        let mut newest_epoch = self.active_epoch;
        let mut ack_txs = Vec::new();
        while let Ok(flush) = self.flush_rx.try_recv() {
            requests += 1;
            newest_epoch = newest_epoch.max(flush.epoch);
            if let Some(ack) = flush.ack {
                ack_txs.push(ack);
            }
        }
        if requests == 0 {
            return;
        }
        self.active_epoch = newest_epoch;
        let pending = self.pending_frames();
        let flushed = self.clear_queue();
        self.active_segment_gain_db = None;
        self.metrics.mark_flush(requests, flushed);
        self.metrics.mark_pending(0);
        info!(
            "event=fanin.tts_flush requests={} pending_frames={} flushed_frames={}",
            requests, pending, flushed
        );
        let summary = FlushSummary {
            requests,
            pending_frames: pending,
            flushed_frames: flushed,
        };
        for ack in ack_txs {
            let _ = ack.send(summary.clone());
        }
    }

    fn pending_frames(&self) -> u64 {
        (self.queue.len() / (CHANNELS as usize)) as u64
    }

    fn clear_queue(&mut self) -> u64 {
        let frames = self.pending_frames();
        self.queue.clear();
        frames
    }
}

pub fn spawn_tts_server(
    path: PathBuf,
    tx: SyncSender<QueuedTtsCommand>,
    flush_tx: SyncSender<QueuedFlush>,
    epoch: Arc<AtomicU64>,
    metrics: TtsMetrics,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating fanin TTS socket parent {}", parent.display()))?;
    }
    let _ = fs::remove_file(&path);
    let listener = UnixListener::bind(&path)
        .with_context(|| format!("binding fanin TTS socket {}", path.display()))?;
    info!("event=fanin.tts_socket.listening path={}", path.display());
    thread::Builder::new()
        .name("fanin-tts-ipc".to_string())
        .spawn(move || {
            for stream in listener.incoming() {
                match stream {
                    Ok(stream) => {
                        if let Err(e) = spawn_tts_client(
                            stream,
                            tx.clone(),
                            flush_tx.clone(),
                            Arc::clone(&epoch),
                            metrics.clone(),
                        ) {
                            warn!("event=fanin.tts_socket.spawn_failed detail={}", e);
                        }
                    }
                    Err(e) => {
                        warn!("event=fanin.tts_socket.accept_failed detail={}", e);
                    }
                }
            }
        })
        .context("spawning fanin TTS IPC accept thread")?;
    Ok(())
}

pub fn tts_channels(max_pending_frames: u64) -> (
    SyncSender<QueuedTtsCommand>,
    Receiver<QueuedTtsCommand>,
    SyncSender<QueuedFlush>,
    Receiver<QueuedFlush>,
    TtsMetrics,
    Arc<AtomicU64>,
) {
    let (tx, rx) = mpsc::sync_channel(TTS_COMMAND_QUEUE_CAPACITY);
    let (flush_tx, flush_rx) = mpsc::sync_channel(TTS_COMMAND_QUEUE_CAPACITY);
    let metrics = TtsMetrics::new(max_pending_frames);
    let epoch = Arc::new(AtomicU64::new(0));
    (tx, rx, flush_tx, flush_rx, metrics, epoch)
}

fn spawn_tts_client(
    stream: UnixStream,
    tx: SyncSender<QueuedTtsCommand>,
    flush_tx: SyncSender<QueuedFlush>,
    epoch: Arc<AtomicU64>,
    metrics: TtsMetrics,
) -> io::Result<()> {
    thread::Builder::new()
        .name("fanin-tts-client".to_string())
        .spawn(move || handle_tts_client(stream, tx, flush_tx, epoch, metrics))
        .map(|_| ())
}

fn handle_tts_client(
    stream: UnixStream,
    tx: SyncSender<QueuedTtsCommand>,
    flush_tx: SyncSender<QueuedFlush>,
    epoch: Arc<AtomicU64>,
    metrics: TtsMetrics,
) {
    let mut reader = BufReader::new(stream);
    loop {
        match read_command(&mut reader) {
            Ok(Some(TtsCommand::Close)) | Ok(None) => return,
            Ok(Some(TtsCommand::Flush)) => {
                if !queue_flush(&mut reader, &flush_tx, &epoch, false) {
                    return;
                }
            }
            Ok(Some(TtsCommand::FlushSync)) => {
                if !queue_flush(&mut reader, &flush_tx, &epoch, true) {
                    return;
                }
            }
            Ok(Some(command)) => {
                let current_epoch = epoch.load(Ordering::SeqCst);
                if !try_enqueue_tts_command(
                    &tx,
                    QueuedTtsCommand {
                        epoch: current_epoch,
                        command,
                    },
                    &metrics,
                ) {
                    return;
                }
            }
            Err(e) => {
                warn!("event=fanin.tts_socket.protocol_error detail={}", e);
                return;
            }
        }
    }
}

fn queue_flush(
    reader: &mut BufReader<UnixStream>,
    flush_tx: &SyncSender<QueuedFlush>,
    epoch: &AtomicU64,
    sync: bool,
) -> bool {
    let next_epoch = epoch.fetch_add(1, Ordering::SeqCst) + 1;
    if sync {
        let (ack_tx, ack_rx) = mpsc::sync_channel(1);
        if flush_tx
            .send(QueuedFlush {
                epoch: next_epoch,
                ack: Some(ack_tx),
            })
            .is_err()
        {
            return false;
        }
        let response = match ack_rx.recv_timeout(Duration::from_secs(2)) {
            Ok(summary) => summary.to_json_line(),
            Err(_) => "{\"ok\":false,\"error\":\"flush_ack_timeout\"}\n".to_string(),
        };
        return reader.get_mut().write_all(response.as_bytes()).is_ok();
    }
    flush_tx
        .send(QueuedFlush {
            epoch: next_epoch,
            ack: None,
        })
        .is_ok()
}

fn try_enqueue_tts_command(
    tx: &SyncSender<QueuedTtsCommand>,
    queued: QueuedTtsCommand,
    metrics: &TtsMetrics,
) -> bool {
    if !matches!(queued.command, TtsCommand::Audio(_)) {
        return enqueue_reliable_tts_command(tx, queued);
    }
    match tx.try_send(queued) {
        Ok(()) => true,
        Err(TrySendError::Full(queued)) => {
            let frames = dropped_audio_frames(&queued);
            metrics.mark_dropped_audio(frames);
            warn!(
                "event=fanin.tts_command_dropped reason=queue_full command=audio epoch={} frames={}",
                queued.epoch, frames
            );
            true
        }
        Err(TrySendError::Disconnected(_)) => false,
    }
}

fn enqueue_reliable_tts_command(
    tx: &SyncSender<QueuedTtsCommand>,
    queued: QueuedTtsCommand,
) -> bool {
    match tx.try_send(queued) {
        Ok(()) => true,
        Err(TrySendError::Full(queued)) => {
            warn!(
                "event=fanin.tts_command_backpressure reason=queue_full command={} epoch={}",
                command_name(&queued.command),
                queued.epoch
            );
            tx.send(queued).is_ok()
        }
        Err(TrySendError::Disconnected(_)) => false,
    }
}

fn dropped_audio_frames(queued: &QueuedTtsCommand) -> u64 {
    match &queued.command {
        TtsCommand::Audio(samples) => (samples.len() / (CHANNELS as usize)) as u64,
        _ => 0,
    }
}

impl FlushSummary {
    fn to_json_line(&self) -> String {
        format!(
            "{{\"ok\":true,\"requests\":{},\"pending_frames\":{},\"segments\":0,\"flushed_frames\":{},\"max_audio_played_ms\":0,\"events\":[]}}\n",
            self.requests, self.pending_frames, self.flushed_frames
        )
    }
}

fn fetch_max(cell: &AtomicU64, value: u64) {
    let mut current = cell.load(Ordering::Relaxed);
    while value > current {
        match cell.compare_exchange_weak(
            current,
            value,
            Ordering::Relaxed,
            Ordering::Relaxed,
        ) {
            Ok(_) => break,
            Err(next) => current = next,
        }
    }
}

fn pack_optional_db(value: Option<f32>) -> i64 {
    let Some(value) = value else {
        return PACKED_DB_NONE;
    };
    if !value.is_finite() {
        return PACKED_DB_NONE;
    }
    (value * 10.0).round() as i64
}

fn unpack_optional_db(value: i64) -> Option<f64> {
    if value == PACKED_DB_NONE {
        return None;
    }
    Some(value as f64 / 10.0)
}

fn log_assistant_loudness_decision(kind: SegmentKind, decision: &AssistantGainDecision) {
    info!(
        "event=fanin.assistant_loudness kind={} provider={} model={} voice={} calibrated={} confidence={:.2} baseline_lufs={:.1} target_lufs={:.1} source_lufs={:.1} source_peak_dbfs={:.1} requested_gain_db={:.1} peak_cap_gain_db={:.1} final_gain_db={:.1} reason={}",
        kind.as_str(),
        decision.provider.as_deref().unwrap_or("-"),
        decision.model.as_deref().unwrap_or("-"),
        decision.voice.as_deref().unwrap_or("-"),
        decision.calibrated,
        decision.profile_confidence,
        decision.baseline_lufs,
        decision.target_lufs,
        decision.source_lufs,
        decision.source_peak_dbfs,
        decision.requested_gain_db,
        decision.peak_cap_gain_db,
        decision.final_gain_db,
        decision.clamp_reason,
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::sync::{Mutex, Once};

    static TEST_LOGGER: TestLogger = TestLogger;
    static LOG_INIT: Once = Once::new();
    static TEST_LOGS: Mutex<Vec<String>> = Mutex::new(Vec::new());

    struct TestLogger;

    impl log::Log for TestLogger {
        fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
            metadata.level() <= log::Level::Info
        }

        fn log(&self, record: &log::Record<'_>) {
            if self.enabled(record.metadata()) {
                TEST_LOGS.lock().unwrap().push(record.args().to_string());
            }
        }

        fn flush(&self) {}
    }

    fn capture_logs() {
        LOG_INIT.call_once(|| {
            let _ = log::set_logger(&TEST_LOGGER);
            log::set_max_level(log::LevelFilter::Info);
        });
        TEST_LOGS.lock().unwrap().clear();
    }

    fn captured_logs() -> Vec<String> {
        TEST_LOGS.lock().unwrap().clone()
    }

    #[test]
    fn reads_outputd_compatible_gain_audio_and_flush() {
        let mut reader = Cursor::new(
            b"GAIN -12.5\nAUDIO 8\n\x01\0\x02\0\x03\0\x04\0PROGRAM_DUCK_ON\nFLUSH_SYNC\nPROGRAM_DUCK_OFF\n".to_vec(),
        );

        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::GainDb(-12.5))
        );
        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::Audio(vec![1, 2, 3, 4]))
        );
        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::ProgramDuckOn)
        );
        assert_eq!(read_command(&mut reader).unwrap(), Some(TtsCommand::FlushSync));
        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::ProgramDuckOff)
        );
    }

    #[test]
    fn reads_outputd_compatible_loudness_metadata() {
        let mut reader = Cursor::new(
            b"PREPARE_ASSISTANT openai gpt-realtime-2 marin -38.5\nSEGMENT_START assistant item_1 openai gpt-realtime-2 marin -25.0 -7.5 1.0\nSEGMENT_END\n".to_vec(),
        );

        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                silence_target_lufs: -38.5,
            })
        );
        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("item_1".to_string()),
                profile: Some(AssistantProfile {
                    provider: "openai".to_string(),
                    model: "gpt-realtime-2".to_string(),
                    voice: "marin".to_string(),
                    source_lufs: Some(-25.0),
                    source_peak_dbfs: Some(-7.5),
                    confidence: 1.0,
                }),
            })
        );
        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::SegmentEnd)
        );
    }

    #[test]
    fn rejects_non_stereo_audio_chunks() {
        let mut reader = Cursor::new(b"AUDIO 2\n\x01\0".to_vec());
        let err = read_command(&mut reader).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn tts_mixer_uses_loudness_for_implicit_segment_and_mixes_period() {
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::GainDb(12.0),
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![10_000, -10_000, 10_000, -10_000]),
        })
        .unwrap();
        let mut sum = vec![0i32; 4];

        assert!(mixer.prepare_period());
        mixer.mix_period(&mut sum);

        let expected = apply_gain_i16(10_000, gain_db_to_linear(-15.5)) as i32;
        assert_eq!(sum, vec![expected, -expected, expected, -expected]);
        assert_eq!(metrics.pending_frames(), 0);
        assert!(metrics.loudness_snapshot().decision_seen);
        drop(flush_tx);
    }

    #[test]
    fn tts_mixer_uses_profiled_segment_gain() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                silence_target_lufs: -38.0,
            },
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("item_1".to_string()),
                profile: Some(AssistantProfile {
                    provider: "openai".to_string(),
                    model: "gpt-realtime-2".to_string(),
                    voice: "marin".to_string(),
                    source_lufs: Some(-25.0),
                    source_peak_dbfs: Some(-8.0),
                    confidence: 1.0,
                }),
            },
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![10_000, -10_000]),
        })
        .unwrap();

        let mut sum = vec![0i32; 2];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut sum);

        let expected = apply_gain_i16(10_000, gain_db_to_linear(-11.5)) as i32;
        assert_eq!(sum, vec![expected, -expected]);
        let loudness = metrics.loudness_snapshot();
        assert!(loudness.decision_seen);
        assert!(loudness.calibrated);
        assert_eq!(loudness.final_gain_db, Some(-11.5));
    }

    #[test]
    fn flush_sync_ack_reports_pending_frames() {
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![1, 2, 3, 4]),
        })
        .unwrap();
        mixer.drain_commands();
        let (ack_tx, ack_rx) = mpsc::sync_channel(1);
        flush_tx
            .send(QueuedFlush {
                epoch: 1,
                ack: Some(ack_tx),
            })
            .unwrap();

        let mut sum = [0i32; 4];
        assert!(!mixer.prepare_period());
        mixer.mix_period(&mut sum);

        let ack = ack_rx.try_recv().unwrap();
        assert_eq!(ack.pending_frames, 2);
        assert_eq!(ack.flushed_frames, 2);
    }

    #[test]
    fn program_duck_command_marks_period_active_until_restore() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOn,
        })
        .unwrap();

        assert!(mixer.prepare_period());

        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOff,
        })
        .unwrap();
        assert!(!mixer.prepare_period());
    }

    #[test]
    fn program_duck_restore_survives_audio_epoch_flush() {
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOn,
        })
        .unwrap();
        assert!(mixer.prepare_period());
        flush_tx
            .send(QueuedFlush {
                epoch: 1,
                ack: None,
            })
            .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOff,
        })
        .unwrap();

        assert!(!mixer.prepare_period());
    }

    #[test]
    fn stale_program_duck_on_does_not_relatch_after_flush() {
        capture_logs();
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
        });
        flush_tx
            .send(QueuedFlush {
                epoch: 1,
                ack: None,
            })
            .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOn,
        })
        .unwrap();

        assert!(!mixer.prepare_period());
        assert_eq!(metrics.stale_commands_dropped(), 1);
        assert!(captured_logs().iter().any(|line| {
            line.contains("event=fanin.tts_command_dropped reason=stale_epoch")
                && line.contains("command=program_duck_on")
        }));
    }

    #[test]
    fn stale_audio_drop_is_counted_without_per_command_warn() {
        capture_logs();
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
        });
        flush_tx
            .send(QueuedFlush {
                epoch: 1,
                ack: None,
            })
            .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![1, 2, 3, 4]),
        })
        .unwrap();

        assert!(!mixer.prepare_period());
        assert_eq!(metrics.stale_commands_dropped(), 1);
        assert!(!captured_logs().iter().any(|line| {
            line.contains("event=fanin.tts_command_dropped reason=stale_epoch")
                && line.contains("command=audio")
        }));
    }

    #[test]
    fn stale_content_meter_resume_survives_flush() {
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ContentMeterPause,
        })
        .unwrap();
        let _ = mixer.prepare_period();
        assert!(mixer.content_meter_paused);

        flush_tx
            .send(QueuedFlush {
                epoch: 1,
                ack: None,
            })
            .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ContentMeterResume,
        })
        .unwrap();

        let _ = mixer.prepare_period();
        assert!(!mixer.content_meter_paused);
    }

}
