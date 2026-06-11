//! Post-round-trip TTS IPC for the final-output owner — the bonded-member
//! voice path (docs/HANDOFF-multiroom.md §2, Increment 5 PR-2).
//!
//! On a bonded member, conversational TTS must NOT ride the synced
//! stream (inv-3 — the ~buffer_ms playout delay is right for music and
//! wrong for speech). Each member's own voice replies instead mix HERE,
//! at the final output stage, downstream of the round-trip and upstream
//! of the reference publish — which is exactly inv-A's requirement (the
//! AEC reference must equal final DAC content, TTS-inclusive).
//!
//! ## Wire protocol — deliberately identical to jasper-fanin's
//!
//! This is the protocol-compatible twin of `rust/jasper-fanin/src/tts.rs`
//! (whose own header states the match is intentional "so Python can keep
//! one playout implementation"): newline-framed text commands (GAIN /
//! PREPARE_ASSISTANT / SEGMENT_START / AUDIO n + raw S16_LE bytes /
//! SEGMENT_END / PROGRAM_DUCK_* / CONTENT_METER_* / FLUSH / FLUSH_SYNC /
//! CLOSE) with a one-line JSON ack for FLUSH_SYNC. `jasper-voice`'s
//! `audio_io.py` speaks it unchanged — the reconciler only flips the
//! socket path per grouping role. The DUPLICATION of the parser/server
//! half with fanin is deliberate and bounded (a shared crate is a named
//! follow-up, not smuggled into this feature): keep the two `read_command`
//! implementations in sync when the protocol grows.
//!
//! ## What is NOT duplicated: the engine
//!
//! fanin's consumer half (its `TtsMixer`) owns queueing/loudness/ledger
//! emulation because fanin has none. outputd already HAS the real engine
//! — `OutputCore` (assistant segments, loudness decisions, saturating
//! mix, the `PlayoutLedger` marked against ACTUAL DAC progress). The
//! consumer here is therefore a thin `TtsBridge` that translates wire
//! commands into `OutputCore` calls, drained once per DAC period by the
//! audio loop. Notably this makes the FLUSH_SYNC ack HONEST for the
//! first time: fanin's twin hardcodes `"max_audio_played_ms":0` and
//! `"events":[]` (it cannot know DAC progress); here both come from the
//! ledger.
//!
//! Threading mirrors fanin: an accept thread + one thread per client
//! connection parse and enqueue; bounded channels with the fanin policy
//! (AUDIO drops-on-full with a counted warning — late speech is worse
//! than lost speech; control commands block briefly — losing a
//! SEGMENT_END or DUCK_OFF corrupts state). The audio loop never blocks
//! on any of it (inv-1: the DAC write stays the sole pacer).

use std::collections::VecDeque;
use std::fs;
use std::io::{self, BufRead, BufReader, Write as IoWrite};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result};

use crate::core::OutputCore;
use crate::ledger::{PlayoutEvent, SegmentId};
use crate::mixer::MAX_TTS_GAIN_DB;
use crate::types::{AssistantProfile, SegmentKind, CHANNELS, SAMPLE_RATE};

/// Hard per-AUDIO-command byte cap (matches fanin: ~10.9 s of stereo
/// S16 at 48 kHz). A malformed length header cannot OOM the daemon.
pub const MAX_AUDIO_BYTES: usize = 2 * 1024 * 1024;
pub const TTS_COMMAND_QUEUE_CAPACITY: usize = 128;
/// Default pending-audio budget: 2 s of queued-but-unplayed assistant
/// audio. Beyond it, new AUDIO drops (counted) — bounding both memory
/// and how stale a reply can get.
pub const DEFAULT_MAX_PENDING_FRAMES: u64 = 48_000 * 2;

const FLUSH_ACK_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, PartialEq)]
pub enum TtsCommand {
    GainDb(f32),
    PrepareAssistant {
        provider: String,
        model: String,
        voice: String,
        silence_target_lufs: f32,
    },
    ContentMeterPause,
    ContentMeterResume,
    ProgramDuckOn,
    ProgramDuckOff,
    SegmentStart {
        kind: SegmentKind,
        provider_item_id: Option<String>,
        profile: Option<AssistantProfile>,
    },
    Audio(Vec<i16>),
    SegmentEnd,
    Flush,
    FlushSync,
    Close,
}

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

/// The FLUSH_SYNC ack payload. Unlike fanin's twin (which hardcodes
/// zeros — it has no ledger), every field here is real: the ledger's
/// per-segment playout events and the max audio actually DRAINED to the
/// DAC, which is what barge-in needs to know.
#[derive(Debug, Clone)]
pub struct FlushSummary {
    pub requests: u64,
    pub pending_frames: u64,
    pub flushed_frames: u64,
    pub segments: usize,
    pub max_audio_played_ms: u64,
    events_json: String,
}

impl FlushSummary {
    pub fn from_events(
        requests: u64,
        pending_frames: u64,
        events: &[PlayoutEvent],
    ) -> Self {
        let flushed_frames: u64 = events.iter().map(|e| e.flushed_frames).sum();
        let max_audio_played_ms = events
            .iter()
            .map(|e| e.estimated_drained_frames * 1000 / (SAMPLE_RATE as u64))
            .max()
            .unwrap_or(0);
        let mut events_json = String::from("[");
        for (i, e) in events.iter().enumerate() {
            if i > 0 {
                events_json.push(',');
            }
            events_json.push_str(&format!(
                "{{\"segment\":{},\"kind\":\"{}\",\"provider_item_id\":{},\"queued_frames\":{},\"written_frames\":{},\"drained_frames\":{},\"flushed_frames\":{}}}",
                e.local_segment_id.0,
                e.kind.as_str(),
                match &e.provider_item_id {
                    Some(id) => format!("\"{}\"", id.replace('\\', "").replace('"', "")),
                    None => "null".to_string(),
                },
                e.queued_frames,
                e.written_frames,
                e.estimated_drained_frames,
                e.flushed_frames,
            ));
        }
        events_json.push(']');
        Self {
            requests,
            pending_frames,
            flushed_frames,
            segments: events.len(),
            max_audio_played_ms,
            events_json,
        }
    }

    fn to_json_line(&self) -> String {
        format!(
            "{{\"ok\":true,\"requests\":{},\"pending_frames\":{},\"segments\":{},\"flushed_frames\":{},\"max_audio_played_ms\":{},\"events\":{}}}\n",
            self.requests,
            self.pending_frames,
            self.segments,
            self.flushed_frames,
            self.max_audio_played_ms,
            self.events_json,
        )
    }
}

/// Socket-side counters for the STATUS `tts` block (daemon truth).
/// Cloneable handle over shared atomics — the socket threads and the
/// audio loop both write, the state server reads.
#[derive(Clone)]
pub struct TtsMetrics {
    pub requests: Arc<AtomicU64>,
    pub pending_frames: Arc<AtomicU64>,
    pub dropped_audio_frames: Arc<AtomicU64>,
    pub dropped_commands: Arc<AtomicU64>,
    pub flush_requests: Arc<AtomicU64>,
    pub flushed_frames: Arc<AtomicU64>,
    pub max_pending_frames: u64,
}

impl TtsMetrics {
    pub fn new(max_pending_frames: u64) -> Self {
        Self {
            requests: Arc::new(AtomicU64::new(0)),
            pending_frames: Arc::new(AtomicU64::new(0)),
            dropped_audio_frames: Arc::new(AtomicU64::new(0)),
            dropped_commands: Arc::new(AtomicU64::new(0)),
            flush_requests: Arc::new(AtomicU64::new(0)),
            flushed_frames: Arc::new(AtomicU64::new(0)),
            max_pending_frames,
        }
    }

    fn mark_dropped_audio(&self, frames: u64) {
        self.dropped_audio_frames.fetch_add(frames, Ordering::Relaxed);
        self.dropped_commands.fetch_add(1, Ordering::Relaxed);
    }
}

// ---------------------------------------------------------------------
// Server half — ported from fanin (see module header).
// ---------------------------------------------------------------------

pub fn tts_channels(
    max_pending_frames: u64,
) -> (
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

pub fn spawn_tts_server(
    path: PathBuf,
    tx: SyncSender<QueuedTtsCommand>,
    flush_tx: SyncSender<QueuedFlush>,
    epoch: Arc<AtomicU64>,
    metrics: TtsMetrics,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).with_context(|| {
            format!("creating outputd TTS socket parent {}", parent.display())
        })?;
    }
    let _ = fs::remove_file(&path);
    let listener = UnixListener::bind(&path)
        .with_context(|| format!("binding outputd TTS socket {}", path.display()))?;
    eprintln!("event=outputd.tts_socket.listening path={}", path.display());
    thread::Builder::new()
        .name("outputd-tts-ipc".to_string())
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
                            eprintln!("event=outputd.tts_socket.spawn_failed detail={e}");
                        }
                    }
                    Err(e) => {
                        eprintln!("event=outputd.tts_socket.accept_failed detail={e}");
                    }
                }
            }
        })
        .context("spawning outputd TTS IPC accept thread")?;
    Ok(())
}

fn spawn_tts_client(
    stream: UnixStream,
    tx: SyncSender<QueuedTtsCommand>,
    flush_tx: SyncSender<QueuedFlush>,
    epoch: Arc<AtomicU64>,
    metrics: TtsMetrics,
) -> io::Result<()> {
    thread::Builder::new()
        .name("outputd-tts-client".to_string())
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
                if !queue_flush(&mut reader, &flush_tx, &epoch, &metrics, false) {
                    return;
                }
            }
            Ok(Some(TtsCommand::FlushSync)) => {
                if !queue_flush(&mut reader, &flush_tx, &epoch, &metrics, true) {
                    return;
                }
            }
            Ok(Some(command)) => {
                metrics.requests.fetch_add(1, Ordering::Relaxed);
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
                eprintln!("event=outputd.tts_socket.protocol_error detail={e}");
                return;
            }
        }
    }
}

fn queue_flush(
    reader: &mut BufReader<UnixStream>,
    flush_tx: &SyncSender<QueuedFlush>,
    epoch: &AtomicU64,
    metrics: &TtsMetrics,
    sync: bool,
) -> bool {
    metrics.flush_requests.fetch_add(1, Ordering::Relaxed);
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
        let response = match ack_rx.recv_timeout(FLUSH_ACK_TIMEOUT) {
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
            eprintln!(
                "event=outputd.tts_command_dropped reason=queue_full command=audio epoch={} frames={}",
                queued.epoch, frames,
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
            eprintln!(
                "event=outputd.tts_command_backpressure reason=queue_full command={} epoch={}",
                command_name(&queued.command),
                queued.epoch,
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

fn command_name(command: &TtsCommand) -> &'static str {
    match command {
        TtsCommand::GainDb(_) => "gain",
        TtsCommand::PrepareAssistant { .. } => "prepare_assistant",
        TtsCommand::ContentMeterPause => "content_meter_pause",
        TtsCommand::ContentMeterResume => "content_meter_resume",
        TtsCommand::ProgramDuckOn => "program_duck_on",
        TtsCommand::ProgramDuckOff => "program_duck_off",
        TtsCommand::SegmentStart { .. } => "segment_start",
        TtsCommand::Audio(_) => "audio",
        TtsCommand::SegmentEnd => "segment_end",
        TtsCommand::Flush => "flush",
        TtsCommand::FlushSync => "flush_sync",
        TtsCommand::Close => "close",
    }
}

// ---------------------------------------------------------------------
// Wire parser — ported from fanin; keep in sync (see module header).
// ---------------------------------------------------------------------

pub fn read_command<R: BufRead>(reader: &mut R) -> io::Result<Option<TtsCommand>> {
    let mut line = String::new();
    let n = reader.read_line(&mut line)?;
    if n == 0 {
        return Ok(None);
    }
    let line = line.trim_end_matches(['\r', '\n']);
    match line {
        "FLUSH" => return Ok(Some(TtsCommand::Flush)),
        "FLUSH_SYNC" => return Ok(Some(TtsCommand::FlushSync)),
        "PROGRAM_DUCK_ON" => return Ok(Some(TtsCommand::ProgramDuckOn)),
        "PROGRAM_DUCK_OFF" => return Ok(Some(TtsCommand::ProgramDuckOff)),
        "SEGMENT_END" => return Ok(Some(TtsCommand::SegmentEnd)),
        "CLOSE" => return Ok(Some(TtsCommand::Close)),
        "CONTENT_METER_PAUSE" => return Ok(Some(TtsCommand::ContentMeterPause)),
        "CONTENT_METER_RESUME" => return Ok(Some(TtsCommand::ContentMeterResume)),
        _ => {}
    }
    if let Some(rest) = line.strip_prefix("GAIN ") {
        let gain = rest
            .parse::<f32>()
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid GAIN value"))?;
        return Ok(Some(TtsCommand::GainDb(gain)));
    }
    if let Some(rest) = line.strip_prefix("AUDIO ") {
        let byte_len = rest
            .parse::<usize>()
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid AUDIO length"))?;
        if byte_len > MAX_AUDIO_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "AUDIO byte length exceeds max chunk size",
            ));
        }
        if byte_len % 2 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "AUDIO byte length must be even",
            ));
        }
        let frame_bytes = (CHANNELS as usize) * 2;
        if byte_len % frame_bytes != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "AUDIO byte length must contain whole stereo frames",
            ));
        }
        let mut bytes = vec![0u8; byte_len];
        reader.read_exact(&mut bytes)?;
        let samples = bytes
            .chunks_exact(2)
            .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]))
            .collect();
        return Ok(Some(TtsCommand::Audio(samples)));
    }
    if let Some(rest) = line.strip_prefix("SEGMENT_START ") {
        let mut parts = rest.split(' ');
        let raw_kind = parts.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "missing SEGMENT_START kind")
        })?;
        let raw_provider = parts.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing SEGMENT_START provider item id",
            )
        })?;
        let kind = SegmentKind::from_protocol(raw_kind).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "invalid SEGMENT_START kind")
        })?;
        let provider_item_id = if raw_provider == "-" {
            None
        } else {
            validate_token(raw_provider, "SEGMENT_START provider item id")?;
            Some(raw_provider.to_string())
        };
        let profile = match parts.next() {
            None => None,
            Some(provider) => {
                let model = parts.next().ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "missing SEGMENT_START model")
                })?;
                let voice = parts.next().ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "missing SEGMENT_START voice")
                })?;
                let source_lufs = parts.next().ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "missing SEGMENT_START source_lufs",
                    )
                })?;
                let source_peak_dbfs = parts.next().ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "missing SEGMENT_START source_peak_dbfs",
                    )
                })?;
                let confidence = parts.next().ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "missing SEGMENT_START confidence",
                    )
                })?;
                if parts.next().is_some() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "SEGMENT_START has too many arguments",
                    ));
                }
                validate_token(provider, "SEGMENT_START provider")?;
                validate_token(model, "SEGMENT_START model")?;
                validate_token(voice, "SEGMENT_START voice")?;
                Some(AssistantProfile {
                    provider: provider.to_string(),
                    model: model.to_string(),
                    voice: voice.to_string(),
                    source_lufs: parse_optional_f32(source_lufs, "SEGMENT_START source_lufs")?,
                    source_peak_dbfs: parse_optional_f32(
                        source_peak_dbfs,
                        "SEGMENT_START source_peak_dbfs",
                    )?,
                    confidence: parse_required_f32(confidence, "SEGMENT_START confidence")?,
                })
            }
        };
        return Ok(Some(TtsCommand::SegmentStart {
            kind,
            provider_item_id,
            profile,
        }));
    }
    if let Some(rest) = line.strip_prefix("PREPARE_ASSISTANT ") {
        let mut parts = rest.split(' ');
        let provider = parts.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing PREPARE_ASSISTANT provider",
            )
        })?;
        let model = parts.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "missing PREPARE_ASSISTANT model")
        })?;
        let voice = parts.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "missing PREPARE_ASSISTANT voice")
        })?;
        let silence_target = parts.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing PREPARE_ASSISTANT silence target",
            )
        })?;
        if parts.next().is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "PREPARE_ASSISTANT expects exactly four arguments",
            ));
        }
        validate_token(provider, "PREPARE_ASSISTANT provider")?;
        validate_token(model, "PREPARE_ASSISTANT model")?;
        validate_token(voice, "PREPARE_ASSISTANT voice")?;
        return Ok(Some(TtsCommand::PrepareAssistant {
            provider: provider.to_string(),
            model: model.to_string(),
            voice: voice.to_string(),
            silence_target_lufs: parse_required_f32(
                silence_target,
                "PREPARE_ASSISTANT silence target",
            )?,
        }));
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        format!("unknown TTS command: {line}"),
    ))
}

fn validate_token(value: &str, field: &str) -> io::Result<()> {
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("invalid {field}"),
        ));
    }
    Ok(())
}

fn parse_optional_f32(value: &str, field: &str) -> io::Result<Option<f32>> {
    if value == "-" {
        return Ok(None);
    }
    parse_required_f32(value, field).map(Some)
}

fn parse_required_f32(value: &str, field: &str) -> io::Result<f32> {
    let parsed = value
        .parse::<f32>()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, format!("invalid {field}")))?;
    if !parsed.is_finite() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("non-finite {field}"),
        ));
    }
    Ok(parsed)
}

// ---------------------------------------------------------------------
// The consumer: TtsBridge — wire commands → OutputCore, once per period.
// ---------------------------------------------------------------------

/// Drained by the audio loop each period (never blocks — try_recv only).
/// Owns the protocol-session state the engine doesn't: the current open
/// segment, the fallback gain, the program-duck flag, and the
/// flush-epoch gate (commands enqueued before a flush are stale and
/// dropped; `ProgramDuckOff` is exempt so a flush can never strand the
/// program ducked — fanin's rule, kept).
pub struct TtsBridge {
    rx: Receiver<QueuedTtsCommand>,
    flush_rx: Receiver<QueuedFlush>,
    metrics: TtsMetrics,
    program_duck_gain_db: f32,
    fallback_gain_db: f32,
    duck_active: bool,
    open_segment: Option<SegmentId>,
    active_epoch: u64,
    // Audio that arrived before any SEGMENT_START in this connection era
    // (the legacy GAIN+AUDIO cue path) opens an implicit Assistant
    // segment lazily; tracked so SEGMENT_START closes it first.
    implicit_segment: bool,
}

impl TtsBridge {
    pub fn new(
        rx: Receiver<QueuedTtsCommand>,
        flush_rx: Receiver<QueuedFlush>,
        metrics: TtsMetrics,
        program_duck_gain_db: f32,
    ) -> Self {
        Self {
            rx,
            flush_rx,
            metrics,
            program_duck_gain_db,
            fallback_gain_db: MAX_TTS_GAIN_DB,
            duck_active: false,
            open_segment: None,
            active_epoch: 0,
            implicit_segment: false,
        }
    }

    /// Linear gain to apply to the CONTENT period when the program duck
    /// is active (None = unity). The audio loop applies it BEFORE
    /// `core.prepare_period_with_content` so the reference carries the
    /// ducked program too (inv-A).
    pub fn content_duck_gain(&self) -> Option<f32> {
        if self.duck_active {
            Some(db_to_linear(self.program_duck_gain_db))
        } else {
            None
        }
    }

    pub fn pending_frames(&self, core: &OutputCore) -> u64 {
        core.pending_assistant_frames()
    }

    /// Drain flushes then commands into `core`. Called once per DAC
    /// period by the audio loop; O(queued) with bounded queues.
    pub fn drain(&mut self, core: &mut OutputCore) {
        self.drain_flushes(core);
        self.drain_commands(core);
        self.metrics
            .pending_frames
            .store(core.pending_assistant_frames(), Ordering::Relaxed);
    }

    fn drain_flushes(&mut self, core: &mut OutputCore) {
        loop {
            let Ok(flush) = self.flush_rx.try_recv() else {
                break;
            };
            let pending_before = core.pending_assistant_frames();
            let events = core.flush_assistant();
            self.open_segment = None;
            self.implicit_segment = false;
            self.active_epoch = flush.epoch;
            let flushed: u64 = events.iter().map(|e| e.flushed_frames).sum();
            self.metrics
                .flushed_frames
                .fetch_add(flushed, Ordering::Relaxed);
            eprintln!(
                "event=outputd.tts_flush epoch={} pending_frames={} segments={} flushed_frames={}",
                flush.epoch,
                pending_before,
                events.len(),
                flushed,
            );
            if let Some(ack) = flush.ack {
                let summary = FlushSummary::from_events(
                    self.metrics.flush_requests.load(Ordering::Relaxed),
                    pending_before,
                    &events,
                );
                let _ = ack.send(summary); // client gone = fine
            }
        }
    }

    fn drain_commands(&mut self, core: &mut OutputCore) {
        loop {
            let Ok(queued) = self.rx.try_recv() else {
                break;
            };
            let is_duck_restore = matches!(&queued.command, TtsCommand::ProgramDuckOff);
            if queued.epoch != self.active_epoch && !is_duck_restore {
                continue; // pre-flush stale command
            }
            match queued.command {
                TtsCommand::GainDb(db) => {
                    self.fallback_gain_db = db; // clamped inside the core
                }
                TtsCommand::PrepareAssistant {
                    provider,
                    model,
                    voice,
                    silence_target_lufs,
                } => {
                    core.prepare_assistant_context(provider, model, voice, silence_target_lufs);
                }
                TtsCommand::ContentMeterPause => core.pause_content_meter(),
                TtsCommand::ContentMeterResume => core.resume_content_meter(),
                TtsCommand::ProgramDuckOn => self.duck_active = true,
                TtsCommand::ProgramDuckOff => self.duck_active = false,
                TtsCommand::SegmentStart {
                    kind,
                    provider_item_id,
                    profile,
                } => {
                    self.close_open_segment(core);
                    let id = core.start_assistant_segment_with_profile(
                        provider_item_id,
                        kind,
                        self.fallback_gain_db,
                        profile,
                    );
                    self.open_segment = Some(id);
                    self.implicit_segment = false;
                }
                TtsCommand::Audio(samples) => {
                    let incoming = (samples.len() / (CHANNELS as usize)) as u64;
                    if core
                        .pending_assistant_frames()
                        .saturating_add(incoming)
                        > self.metrics.max_pending_frames
                    {
                        self.metrics.mark_dropped_audio(incoming);
                        eprintln!(
                            "event=outputd.tts_command_dropped reason=pending_budget_exceeded command=audio epoch={} frames={} pending_frames={} budget_frames={}",
                            queued.epoch,
                            incoming,
                            core.pending_assistant_frames(),
                            self.metrics.max_pending_frames,
                        );
                        continue;
                    }
                    let id = match self.open_segment {
                        Some(id) => id,
                        None => {
                            // Legacy GAIN+AUDIO path (cues): open an
                            // implicit Assistant segment until the next
                            // boundary (SEGMENT_START / flush).
                            let id = core.start_assistant_segment(
                                None,
                                SegmentKind::Assistant,
                                self.fallback_gain_db,
                            );
                            self.open_segment = Some(id);
                            self.implicit_segment = true;
                            id
                        }
                    };
                    core.append_assistant_audio_with_segment_gain(id, samples);
                }
                TtsCommand::SegmentEnd => self.close_open_segment(core),
                // Handled in the client/flush threads; never enqueued.
                TtsCommand::Flush | TtsCommand::FlushSync | TtsCommand::Close => {}
            }
        }
    }

    fn close_open_segment(&mut self, core: &mut OutputCore) {
        if let Some(id) = self.open_segment.take() {
            core.end_assistant_segment(id);
        }
        self.implicit_segment = false;
    }
}

fn db_to_linear(db: f32) -> f32 {
    10f32.powf(db / 20.0)
}

// Keep VecDeque import meaningful if the queue moves here later.
#[allow(unused)]
type _Unused = VecDeque<i16>;

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    // ---------- parser (the wire contract, kept in sync with fanin) ----

    fn parse_all(bytes: &[u8]) -> Vec<TtsCommand> {
        let mut reader = Cursor::new(bytes.to_vec());
        let mut out = Vec::new();
        while let Ok(Some(cmd)) = read_command(&mut reader) {
            out.push(cmd);
        }
        out
    }

    #[test]
    fn parser_round_trips_the_fanin_corpus() {
        let cmds = parse_all(
            b"GAIN -12.5\nAUDIO 8\n\x01\0\x02\0\x03\0\x04\0PROGRAM_DUCK_ON\nFLUSH_SYNC\nPROGRAM_DUCK_OFF\n",
        );
        assert_eq!(
            cmds,
            vec![
                TtsCommand::GainDb(-12.5),
                TtsCommand::Audio(vec![1, 2, 3, 4]),
                TtsCommand::ProgramDuckOn,
                TtsCommand::FlushSync,
                TtsCommand::ProgramDuckOff,
            ]
        );
    }

    #[test]
    fn parser_segment_start_with_and_without_profile() {
        let cmds = parse_all(
            b"SEGMENT_START assistant item-1\nSEGMENT_START cue - gemini m1 v1 -16.5 - 0.8\nSEGMENT_END\n",
        );
        assert_eq!(cmds.len(), 3);
        match &cmds[0] {
            TtsCommand::SegmentStart {
                kind,
                provider_item_id,
                profile,
            } => {
                assert_eq!(*kind, SegmentKind::Assistant);
                assert_eq!(provider_item_id.as_deref(), Some("item-1"));
                assert!(profile.is_none());
            }
            other => panic!("unexpected {other:?}"),
        }
        match &cmds[1] {
            TtsCommand::SegmentStart { profile, .. } => {
                let p = profile.as_ref().unwrap();
                assert_eq!(p.provider, "gemini");
                assert_eq!(p.source_lufs, Some(-16.5));
                assert_eq!(p.source_peak_dbfs, None); // "-"
                assert_eq!(p.confidence, 0.8);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn parser_rejects_oversized_odd_and_partial_frame_audio() {
        let mut reader = Cursor::new(format!("AUDIO {}\n", MAX_AUDIO_BYTES + 2).into_bytes());
        assert!(read_command(&mut reader).is_err());
        let mut reader = Cursor::new(b"AUDIO 3\n".to_vec());
        assert!(read_command(&mut reader).is_err());
        let mut reader = Cursor::new(b"AUDIO 2\n\x01\0".to_vec()); // half a stereo frame
        assert!(read_command(&mut reader).is_err());
    }

    #[test]
    fn parser_eof_is_clean_close() {
        let mut reader = Cursor::new(Vec::new());
        assert!(matches!(read_command(&mut reader), Ok(None)));
    }

    // ---------- the bridge against the REAL engine ----------

    fn bridge_with_core() -> (
        TtsBridge,
        OutputCore,
        SyncSender<QueuedTtsCommand>,
        SyncSender<QueuedFlush>,
    ) {
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) =
            tts_channels(DEFAULT_MAX_PENDING_FRAMES);
        let bridge = TtsBridge::new(rx, flush_rx, metrics, -12.0);
        let core = OutputCore::new(4, 7);
        (bridge, core, tx, flush_tx)
    }

    fn send(tx: &SyncSender<QueuedTtsCommand>, epoch: u64, command: TtsCommand) {
        tx.send(QueuedTtsCommand { epoch, command }).unwrap();
    }

    #[test]
    fn bridge_segment_lifecycle_mixes_into_dac_output() {
        let (mut bridge, mut core, tx, _ftx) = bridge_with_core();
        send(
            &tx,
            0,
            TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("item-9".into()),
                profile: None,
            },
        );
        send(&tx, 0, TtsCommand::Audio(vec![1000i16; 8])); // one 4-frame period
        send(&tx, 0, TtsCommand::SegmentEnd);
        bridge.drain(&mut core);

        core.push_content_period(vec![100i16; 8]);
        let report = core.step();
        assert_eq!(report.clipped_samples, 0);
        // Content + gain-scaled assistant — the DAC got a real mix.
        let written = &core.dac().periods[0];
        assert!(written.iter().all(|&s| s > 100), "assistant missing: {written:?}");
    }

    #[test]
    fn bridge_implicit_segment_for_legacy_gain_audio_cues() {
        let (mut bridge, mut core, tx, _ftx) = bridge_with_core();
        send(&tx, 0, TtsCommand::GainDb(-9.0));
        send(&tx, 0, TtsCommand::Audio(vec![2000i16; 8]));
        bridge.drain(&mut core);
        assert!(core.pending_assistant_frames() > 0);
        assert!(bridge.open_segment.is_some() && bridge.implicit_segment);
    }

    #[test]
    fn bridge_flush_acks_with_real_ledger_numbers() {
        let (mut bridge, mut core, tx, ftx) = bridge_with_core();
        send(
            &tx,
            0,
            TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("cut-short".into()),
                profile: None,
            },
        );
        // 3 periods queued; play ONE before the flush.
        send(&tx, 0, TtsCommand::Audio(vec![3000i16; 24]));
        bridge.drain(&mut core);
        core.push_content_period(vec![0i16; 8]);
        core.step();

        let (ack_tx, ack_rx) = mpsc::sync_channel(1);
        ftx.send(QueuedFlush {
            epoch: 1,
            ack: Some(ack_tx),
        })
        .unwrap();
        bridge.drain(&mut core);
        let summary = ack_rx.try_recv().expect("flush ack");
        assert_eq!(summary.segments, 1);
        assert!(summary.flushed_frames > 0, "unplayed audio must flush");
        // One 4-frame period at 48k ≈ 0ms (integer math) — drained is
        // reported in frames→ms; assert the JSON has the real fields.
        let line = summary.to_json_line();
        assert!(line.contains("\"events\":[{"));
        assert!(line.contains("\"provider_item_id\":\"cut-short\""));
        assert!(!line.contains("\"events\":[]"));
    }

    #[test]
    fn bridge_drops_stale_epoch_commands_but_honors_duck_off() {
        let (mut bridge, mut core, tx, ftx) = bridge_with_core();
        send(&tx, 0, TtsCommand::ProgramDuckOn);
        bridge.drain(&mut core);
        assert!(bridge.content_duck_gain().is_some());

        // Flush bumps the epoch; stale audio must be dropped, but the
        // stale-epoch DUCK_OFF must still land (never strand the duck).
        ftx.send(QueuedFlush {
            epoch: 5,
            ack: None,
        })
        .unwrap();
        send(&tx, 0, TtsCommand::Audio(vec![1i16; 8])); // stale
        send(&tx, 0, TtsCommand::ProgramDuckOff); // stale but exempt
        bridge.drain(&mut core);
        assert_eq!(core.pending_assistant_frames(), 0);
        assert!(bridge.content_duck_gain().is_none());
    }

    #[test]
    fn bridge_enforces_pending_budget() {
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(8); // 8-frame budget
        let mut bridge = TtsBridge::new(rx, flush_rx, metrics.clone(), -12.0);
        let mut core = OutputCore::new(4, 7);
        let _ = flush_tx;
        send(&tx, 0, TtsCommand::Audio(vec![1i16; 16])); // 8 frames: fits
        send(&tx, 0, TtsCommand::Audio(vec![1i16; 16])); // 8 more: over budget
        bridge.drain(&mut core);
        assert_eq!(core.pending_assistant_frames(), 8);
        assert_eq!(metrics.dropped_audio_frames.load(Ordering::Relaxed), 8);
    }
}
