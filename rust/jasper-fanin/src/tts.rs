// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Pre-DSP TTS IPC for active-speaker topologies.
//!
//! The wire protocol intentionally matches `jasper-outputd`'s TTS
//! socket so Python can keep one playout implementation. fan-in only
//! owns the pre-DSP summing concern: it accepts 48 kHz stereo S16_LE
//! TTS/cue audio, sanitizes malformed gain, applies the shared peak-capped
//! assistant gain policy, and mixes it into the summed program lane before
//! CamillaDSP performs crossover/protection.

use std::collections::VecDeque;
use std::fs;
use std::io::{self, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, SyncSender, TrySendError};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use log::{info, warn};

use crate::loudness::{
    apply_gain_i16, gain_db_to_linear, linear_to_db, sanitize_tts_gain_db, AssistantGainDecision,
    AssistantLoudness, AssistantLoudnessConfig, AssistantProfile, HeldLoudnessReference,
    ReferenceKind, SegmentKind, DEFAULT_TTS_GAIN_DB, MIN_TTS_GAIN_DB,
};
use crate::mixer::CHANNELS;
use crate::playout::{PlayoutEvent, PlayoutLedger};
use jasper_tts_protocol::{command_name, read_command, TtsCommand, VolumeContext};

pub const TTS_COMMAND_QUEUE_CAPACITY: usize = 128;
pub const DEFAULT_MAX_PENDING_FRAMES: u64 = 48_000 * 2;
/// Fan-in's assistant wire protocol is contractually 48 kHz stereo
/// (`OutputdTtsPlayout` rejects any other rate) and matches the snd-aloop
/// mix rate, so the playout ledger's frames->ms math is fixed at 48 kHz.
const TTS_SAMPLE_RATE: u32 = 48_000;
// Keep this above voice's `JASPER_IDLE_TIMEOUT_SEC` default (20 s):
// fan-in only sees the one-shot duck IPC, not the provider turn state, so
// a shorter TTL could un-duck program audio during a legitimate quiet turn.
// If operations raise `JASPER_IDLE_TIMEOUT_SEC` above 30 s, retune this too.
const PROGRAM_DUCK_IDLE_RELEASE_TTL: Duration = Duration::from_secs(30);
const LIVE_VOLUME_RAMP_FRAMES: u32 = TTS_SAMPLE_RATE / 10;
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
    segments: usize,
    max_audio_played_ms: u64,
    events_json: String,
}

#[derive(Debug, Clone)]
pub struct TtsMetrics {
    loudness_state_seq: Arc<AtomicU64>,
    pending_frames: Arc<AtomicU64>,
    max_pending_frames: Arc<AtomicU64>,
    budget_frames: Arc<AtomicU64>,
    dropped_commands: Arc<AtomicU64>,
    dropped_audio_frames: Arc<AtomicU64>,
    stale_commands_dropped: Arc<AtomicU64>,
    program_duck_active: Arc<AtomicBool>,
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
    assistant_target_speaker_lufs_x10: Arc<AtomicI64>,
    assistant_envelope_offset_lu_x10: Arc<AtomicI64>,
    assistant_reference_kind: Arc<AtomicU64>,
    volume_context_seen: Arc<AtomicBool>,
    volume_context_canonical_db_x10: Arc<AtomicI64>,
    volume_context_downstream_db_x10: Arc<AtomicI64>,
    volume_context_tts_envelope_lufs_x10: Arc<AtomicI64>,
    volume_context_muted: Arc<AtomicBool>,
    volume_context_stamp_boot_ns: Arc<AtomicU64>,
    volume_context_rejected: Arc<AtomicU64>,
    held_content_speaker_lufs_x10: Arc<AtomicI64>,
    held_content_canonical_db_x10: Arc<AtomicI64>,
    held_assistant_speaker_lufs_x10: Arc<AtomicI64>,
    held_assistant_canonical_db_x10: Arc<AtomicI64>,
    held_assistant_calibration_offset_lu_x10: Arc<AtomicI64>,
}

impl Default for TtsMetrics {
    fn default() -> Self {
        Self {
            loudness_state_seq: Arc::new(AtomicU64::new(0)),
            pending_frames: Arc::new(AtomicU64::new(0)),
            max_pending_frames: Arc::new(AtomicU64::new(0)),
            budget_frames: Arc::new(AtomicU64::new(0)),
            dropped_commands: Arc::new(AtomicU64::new(0)),
            dropped_audio_frames: Arc::new(AtomicU64::new(0)),
            stale_commands_dropped: Arc::new(AtomicU64::new(0)),
            program_duck_active: Arc::new(AtomicBool::new(false)),
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
            assistant_target_speaker_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_envelope_offset_lu_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            assistant_reference_kind: Arc::new(AtomicU64::new(0)),
            volume_context_seen: Arc::new(AtomicBool::new(false)),
            volume_context_canonical_db_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            volume_context_downstream_db_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            volume_context_tts_envelope_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            volume_context_muted: Arc::new(AtomicBool::new(false)),
            volume_context_stamp_boot_ns: Arc::new(AtomicU64::new(0)),
            volume_context_rejected: Arc::new(AtomicU64::new(0)),
            held_content_speaker_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            held_content_canonical_db_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            held_assistant_speaker_lufs_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            held_assistant_canonical_db_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
            held_assistant_calibration_offset_lu_x10: Arc::new(AtomicI64::new(PACKED_DB_NONE)),
        }
    }
}

// The STATUS `assistant_loudness` snapshot is the shared wire shape, defined
// once in jasper-tts-protocol so fan-in and outputd cannot drift. fan-in
// derives it from its seqlock'd atomics below; outputd derives it from the
// engine. Both render it through `jasper_tts_protocol::render_assistant_loudness`.
pub use jasper_tts_protocol::loudness::TtsLoudnessSnapshot;

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

    pub fn program_duck_active(&self) -> bool {
        self.program_duck_active.load(Ordering::Relaxed)
    }

    pub fn flush_requests(&self) -> u64 {
        self.flush_requests.load(Ordering::Relaxed)
    }

    pub fn flushed_frames(&self) -> u64 {
        self.flushed_frames.load(Ordering::Relaxed)
    }

    pub fn loudness_snapshot(&self) -> TtsLoudnessSnapshot {
        loop {
            let before = self.loudness_state_seq.load(Ordering::Acquire);
            if before % 2 != 0 {
                std::hint::spin_loop();
                continue;
            }
            let snapshot = self.loudness_snapshot_unchecked();
            let after = self.loudness_state_seq.load(Ordering::Acquire);
            if before == after {
                return snapshot;
            }
        }
    }

    fn loudness_snapshot_unchecked(&self) -> TtsLoudnessSnapshot {
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
                unpack_optional_db(self.assistant_source_peak_dbfs_x10.load(Ordering::Relaxed))
            } else {
                None
            },
            requested_gain_db: if decision_seen {
                unpack_optional_db(self.assistant_requested_gain_db_x10.load(Ordering::Relaxed))
            } else {
                None
            },
            peak_cap_gain_db: if decision_seen {
                unpack_optional_db(self.assistant_peak_cap_gain_db_x10.load(Ordering::Relaxed))
            } else {
                None
            },
            final_gain_db: if decision_seen {
                unpack_optional_db(self.assistant_final_gain_db_x10.load(Ordering::Relaxed))
            } else {
                None
            },
            target_speaker_lufs: if decision_seen {
                unpack_optional_db(
                    self.assistant_target_speaker_lufs_x10
                        .load(Ordering::Relaxed),
                )
            } else {
                None
            },
            envelope_offset_lu: if decision_seen {
                unpack_optional_db(
                    self.assistant_envelope_offset_lu_x10
                        .load(Ordering::Relaxed),
                )
            } else {
                None
            },
            reference_kind: unpack_reference_kind(
                self.assistant_reference_kind.load(Ordering::Relaxed),
            ),
            volume_context: self.volume_context_seen.load(Ordering::Relaxed).then(|| {
                VolumeContext {
                    canonical_db: unpack_optional_db(
                        self.volume_context_canonical_db_x10.load(Ordering::Relaxed),
                    )
                    .unwrap_or_default() as f32,
                    downstream_db: unpack_optional_db(
                        self.volume_context_downstream_db_x10
                            .load(Ordering::Relaxed),
                    )
                    .unwrap_or_default() as f32,
                    tts_envelope_lufs: unpack_optional_db(
                        self.volume_context_tts_envelope_lufs_x10
                            .load(Ordering::Relaxed),
                    )
                    .unwrap_or_default() as f32,
                    muted: self.volume_context_muted.load(Ordering::Relaxed),
                    stamp_boot_ns: self.volume_context_stamp_boot_ns.load(Ordering::Relaxed),
                }
            }),
            volume_context_rejected: self.volume_context_rejected.load(Ordering::Relaxed),
            held_content: unpack_reference(
                self.held_content_speaker_lufs_x10.load(Ordering::Relaxed),
                self.held_content_canonical_db_x10.load(Ordering::Relaxed),
                0,
            ),
            held_assistant: unpack_reference(
                self.held_assistant_speaker_lufs_x10.load(Ordering::Relaxed),
                self.held_assistant_canonical_db_x10.load(Ordering::Relaxed),
                self.held_assistant_calibration_offset_lu_x10
                    .load(Ordering::Relaxed),
            ),
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

    fn mark_program_duck_active(&self, active: bool) {
        self.program_duck_active.store(active, Ordering::Relaxed);
    }

    fn mark_volume_context(&self, context: VolumeContext) {
        self.begin_loudness_state_write();
        self.volume_context_canonical_db_x10.store(
            pack_optional_db(Some(context.canonical_db)),
            Ordering::Relaxed,
        );
        self.volume_context_downstream_db_x10.store(
            pack_optional_db(Some(context.downstream_db)),
            Ordering::Relaxed,
        );
        self.volume_context_tts_envelope_lufs_x10.store(
            pack_optional_db(Some(context.tts_envelope_lufs)),
            Ordering::Relaxed,
        );
        self.volume_context_muted
            .store(context.muted, Ordering::Relaxed);
        self.volume_context_stamp_boot_ns
            .store(context.stamp_boot_ns, Ordering::Relaxed);
        self.volume_context_seen.store(true, Ordering::Relaxed);
        self.end_loudness_state_write();
    }

    fn mark_volume_context_rejected(&self) {
        self.volume_context_rejected.fetch_add(1, Ordering::Relaxed);
    }

    fn mark_held_references(
        &self,
        content: Option<HeldLoudnessReference>,
        assistant: Option<HeldLoudnessReference>,
    ) {
        self.begin_loudness_state_write();
        store_reference(
            content,
            &self.held_content_speaker_lufs_x10,
            &self.held_content_canonical_db_x10,
            None,
        );
        store_reference(
            assistant,
            &self.held_assistant_speaker_lufs_x10,
            &self.held_assistant_canonical_db_x10,
            Some(&self.held_assistant_calibration_offset_lu_x10),
        );
        self.end_loudness_state_write();
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
        self.begin_loudness_state_write();
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
            self.assistant_baseline_lufs_x10.store(
                pack_optional_db(Some(decision.baseline_lufs)),
                Ordering::Relaxed,
            );
            self.assistant_target_lufs_x10.store(
                pack_optional_db(Some(decision.target_lufs)),
                Ordering::Relaxed,
            );
            self.assistant_source_lufs_x10.store(
                pack_optional_db(Some(decision.source_lufs)),
                Ordering::Relaxed,
            );
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
            self.assistant_final_gain_db_x10.store(
                pack_optional_db(Some(decision.final_gain_db)),
                Ordering::Relaxed,
            );
            self.assistant_target_speaker_lufs_x10.store(
                pack_optional_db(decision.target_speaker_lufs),
                Ordering::Relaxed,
            );
            self.assistant_envelope_offset_lu_x10.store(
                pack_optional_db(decision.envelope_offset_lu),
                Ordering::Relaxed,
            );
            self.assistant_reference_kind.store(
                pack_reference_kind(decision.reference_kind),
                Ordering::Relaxed,
            );
        }
        self.end_loudness_state_write();
    }

    fn begin_loudness_state_write(&self) {
        self.loudness_state_seq.fetch_add(1, Ordering::AcqRel);
    }

    fn end_loudness_state_write(&self) {
        self.loudness_state_seq.fetch_add(1, Ordering::Release);
    }
}

pub struct TtsInput {
    pub rx: Receiver<QueuedTtsCommand>,
    pub flush_rx: Receiver<QueuedFlush>,
    pub metrics: TtsMetrics,
    pub max_pending_frames: u64,
    pub program_duck_db: f32,
    pub cue_duck_db: f32,
    pub assistant_loudness: AssistantLoudnessConfig,
    pub assistant_reference: Option<HeldLoudnessReference>,
    pub assistant_reference_tx: Option<Sender<HeldLoudnessReference>>,
}

pub type TtsChannelBundle = (
    SyncSender<QueuedTtsCommand>,
    Receiver<QueuedTtsCommand>,
    SyncSender<QueuedFlush>,
    Receiver<QueuedFlush>,
    TtsMetrics,
    Arc<AtomicU64>,
);

pub struct TtsMixer {
    rx: Receiver<QueuedTtsCommand>,
    flush_rx: Receiver<QueuedFlush>,
    metrics: TtsMetrics,
    queue: VecDeque<QueuedAudioBlock>,
    pending_samples: u64,
    current_gain_db: f32,
    active_epoch: u64,
    max_pending_frames: u64,
    program_duck_gain: f32,
    cue_duck_gain: f32,
    program_duck_active: bool,
    program_duck_last_refresh: Option<Instant>,
    program_duck_idle_release_ttl: Duration,
    content_meter_paused: bool,
    active_segment_gain_db: Option<f32>,
    active_segment_kind: Option<SegmentKind>,
    active_segment_decision: Option<Arc<AssistantGainDecision>>,
    active_segment_serial: u64,
    next_segment_serial: u64,
    assistant_segment_playback: Option<AssistantSegmentPlayback>,
    assistant_reference_disqualified_serial: Option<u64>,
    gain_ramp: GainRamp,
    assistant_reference_tx: Option<Sender<HeldLoudnessReference>>,
    loudness: AssistantLoudness,
    /// Per-segment playout accounting behind the FLUSH_SYNC ack. Drained at
    /// the mix-commit point (see [`crate::playout`]).
    ledger: PlayoutLedger,
}

impl TtsMixer {
    pub fn new(input: TtsInput) -> Self {
        let mut loudness = AssistantLoudness::new(input.assistant_loudness);
        loudness.set_held_assistant(input.assistant_reference);
        input
            .metrics
            .mark_held_references(None, loudness.held_assistant());
        Self {
            rx: input.rx,
            flush_rx: input.flush_rx,
            metrics: input.metrics,
            queue: VecDeque::new(),
            pending_samples: 0,
            current_gain_db: DEFAULT_TTS_GAIN_DB,
            active_epoch: 0,
            max_pending_frames: input.max_pending_frames,
            program_duck_gain: gain_db_to_linear(input.program_duck_db),
            cue_duck_gain: gain_db_to_linear(input.cue_duck_db),
            program_duck_active: false,
            program_duck_last_refresh: None,
            program_duck_idle_release_ttl: PROGRAM_DUCK_IDLE_RELEASE_TTL,
            content_meter_paused: false,
            active_segment_gain_db: None,
            active_segment_kind: None,
            active_segment_decision: None,
            active_segment_serial: 0,
            next_segment_serial: 1,
            assistant_segment_playback: None,
            assistant_reference_disqualified_serial: None,
            gain_ramp: GainRamp::default(),
            assistant_reference_tx: input.assistant_reference_tx,
            loudness,
            ledger: PlayoutLedger::new(TTS_SAMPLE_RATE),
        }
    }

    pub fn prepare_period(&mut self) -> bool {
        self.drain_flushes();
        self.drain_commands();
        self.release_expired_program_duck();
        self.program_duck_active || self.pending_frames() > 0
    }

    pub fn program_duck_gain(&self) -> f32 {
        // The mixer ducks the program (music/renderer) lane to this gain
        // whenever `prepare_period()` is true. Two regimes reach here:
        //
        //  * Explicit duck (`program_duck_active`): a voice turn or a spoken
        //    cue called PROGRAM_DUCK_ON. Speech has to stay intelligible over
        //    the music for seconds at a time, so duck hard (`program_duck_gain`).
        //
        //  * Segment-driven auto-duck (no explicit duck, but TTS frames are
        //    pending — the other half of `prepare_period`): this is a
        //    standalone short earcon/cue playing outside a turn (mute/unmute
        //    sparkle, wake/end chirp). Those are quick, self-loud cues, not
        //    speech to follow, so slamming the music by the full program duck
        //    is jarring — use the light `cue_duck_gain`. Assistant audio, if it
        //    ever reaches here without an explicit duck, still ducks hard.
        if self.program_duck_active {
            return self.program_duck_gain;
        }
        match self.active_segment_kind {
            Some(SegmentKind::Assistant) => self.program_duck_gain,
            _ => self.cue_duck_gain,
        }
    }

    pub fn observe_content_period(&mut self, samples: &[i16]) {
        if !self.content_meter_paused {
            self.loudness.observe_content_period(samples);
            self.metrics.mark_loudness(
                self.loudness.content_short_lufs(),
                self.loudness.content_anchor_lufs(),
                self.loudness.last_decision(),
            );
            self.metrics
                .mark_held_references(self.loudness.held_content(), self.loudness.held_assistant());
        }
    }

    pub fn mix_period(&mut self, sum: &mut [i32]) {
        let queued_samples_before = self.pending_samples;
        for frame_sum in sum.chunks_exact_mut(CHANNELS as usize) {
            let Some(front) = self.queue.front() else {
                break;
            };
            let target_gain_db = self.target_gain_db(front);
            let playout_context = self.loudness.current_volume_context();
            let muted = playout_context.is_some_and(|context| context.muted);
            let segment_serial = front.segment_serial;
            let assistant_reference_eligible = front.assistant_reference_eligible;
            let starts_assistant_playback = assistant_reference_eligible
                && !muted
                && playout_context.is_some()
                && self.assistant_reference_disqualified_serial != Some(segment_serial)
                && self
                    .assistant_segment_playback
                    .as_ref()
                    .map_or(true, |playback| playback.segment_serial != segment_serial);
            // Arc-clone the owned identity strings once when this segment first
            // reaches playout, never once per audio frame.
            let playback_decision = starts_assistant_playback
                .then(|| front.decision.as_ref().map(Arc::clone))
                .flatten();
            let gain = if muted {
                self.gain_ramp.force_silent();
                0.0
            } else {
                self.gain_ramp.retarget(target_gain_db);
                // The ramp is continuous across ordinary segment boundaries,
                // but a new segment can have a lower profile-derived peak
                // ceiling.  Attenuation for hearing/clip safety takes effect
                // immediately; never let the old ramp state exceed the
                // current block's cap, even for its first frame.
                self.gain_ramp.next_frame().min(front.peak_cap_linear)
            };

            let block_finished;
            let completes_assistant_reference;
            {
                let Some(front) = self.queue.front_mut() else {
                    break;
                };
                for (channel, sample_sum) in frame_sum.iter_mut().enumerate() {
                    let sample = front.samples[front.cursor + channel];
                    *sample_sum = sample_sum.saturating_add(apply_gain_i16(sample, gain) as i32);
                }
                front.cursor += CHANNELS as usize;
                self.pending_samples = self.pending_samples.saturating_sub(CHANNELS as u64);
                block_finished = front.cursor >= front.samples.len();
                completes_assistant_reference = front.completes_assistant_reference;
            }
            if assistant_reference_eligible && muted {
                self.assistant_reference_disqualified_serial = Some(segment_serial);
                if self
                    .assistant_segment_playback
                    .as_ref()
                    .is_some_and(|playback| playback.segment_serial == segment_serial)
                {
                    self.assistant_segment_playback = None;
                }
            } else if assistant_reference_eligible {
                if let (Some(decision), Some(context)) = (playback_decision, playout_context) {
                    self.assistant_segment_playback = Some(AssistantSegmentPlayback {
                        segment_serial,
                        decision,
                        last_gain_linear: gain,
                        context,
                    });
                } else if let (Some(playback), Some(context)) =
                    (self.assistant_segment_playback.as_mut(), playout_context)
                {
                    if playback.segment_serial == segment_serial {
                        playback.last_gain_linear = gain;
                        playback.context = context;
                    }
                }
            }
            if block_finished {
                self.queue.pop_front();
            }
            if block_finished && completes_assistant_reference {
                if let Some(playback) = self.assistant_segment_playback.take() {
                    if playback.segment_serial == segment_serial {
                        self.complete_assistant_reference(
                            &playback.decision,
                            linear_to_db(playback.last_gain_linear),
                            playback.context,
                        );
                    } else {
                        self.assistant_segment_playback = Some(playback);
                    }
                }
                if self.assistant_reference_disqualified_serial == Some(segment_serial) {
                    self.assistant_reference_disqualified_serial = None;
                }
            }
        }
        // Frames popped into the program this period are committed downstream
        // toward the DAC; advance the playout watermark by them. This pop is
        // paced by the blocking snd-aloop write, so the count is DAC-rate-
        // paced, not a queued-frame estimate (see [`crate::playout`]).
        let popped_samples = queued_samples_before.saturating_sub(self.pending_samples);
        self.ledger
            .advance_played(popped_samples / (CHANNELS as u64));
        self.metrics.mark_pending(self.pending_frames());
    }

    fn drain_commands(&mut self) {
        loop {
            let Ok(queued) = self.rx.try_recv() else {
                break;
            };
            let is_restore = matches!(
                &queued.command,
                TtsCommand::ProgramDuckOff
                    | TtsCommand::ContentMeterResume
                    | TtsCommand::VolumeContext(_)
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
                    self.current_gain_db = sanitize_tts_gain_db(db);
                }
                TtsCommand::Audio(samples) => {
                    if samples.is_empty() {
                        continue;
                    }
                    let incoming_frames = (samples.len() / (CHANNELS as usize)) as u64;
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
                    if self.active_segment_gain_db.is_none() {
                        self.begin_segment_gain(SegmentKind::Assistant, None);
                    }
                    let gain_db = self.active_segment_gain_db.unwrap_or(DEFAULT_TTS_GAIN_DB);
                    let decision = self.active_segment_decision.clone();
                    let peak_cap_gain_db = decision
                        .as_ref()
                        .map_or(gain_db, |value| value.peak_cap_gain_db);
                    self.pending_samples =
                        self.pending_samples.saturating_add(samples.len() as u64);
                    self.queue.push_back(QueuedAudioBlock {
                        samples,
                        cursor: 0,
                        base_gain_db: gain_db,
                        peak_cap_gain_db,
                        peak_cap_linear: gain_db_to_linear(peak_cap_gain_db),
                        decision,
                        segment_serial: self.active_segment_serial,
                        assistant_reference_eligible: self.active_segment_kind
                            == Some(SegmentKind::Assistant),
                        completes_assistant_reference: false,
                    });
                    // Only accounted after the budget check above passes, so
                    // the ledger total tracks exactly what is on the queue.
                    self.ledger.note_queued(incoming_frames);
                    if self.program_duck_active {
                        self.refresh_program_duck();
                    }
                }
                TtsCommand::Flush | TtsCommand::FlushSync => {
                    let frames = self.clear_queue();
                    self.active_segment_gain_db = None;
                    self.active_segment_kind = None;
                    self.active_segment_decision = None;
                    self.assistant_segment_playback = None;
                    // Defensive: flushes are normally intercepted before the
                    // command channel (see `handle_tts_client`) and handled by
                    // `drain_flushes`. If one ever reaches here, keep the
                    // ledger consistent with the now-cleared queue.
                    self.ledger.flush();
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
                    self.metrics.mark_program_duck_active(true);
                    self.refresh_program_duck();
                }
                TtsCommand::ProgramDuckOff => {
                    if self.program_duck_active {
                        info!("event=fanin.program_duck on=false");
                    }
                    self.program_duck_active = false;
                    self.program_duck_last_refresh = None;
                    self.metrics.mark_program_duck_active(false);
                }
                TtsCommand::PrepareAssistant {
                    provider,
                    model,
                    voice,
                    tts_envelope_lufs,
                } => {
                    self.loudness
                        .prepare_context(provider, model, voice, tts_envelope_lufs);
                    self.metrics.mark_loudness(
                        self.loudness.content_short_lufs(),
                        self.loudness.content_anchor_lufs(),
                        self.loudness.last_decision(),
                    );
                }
                TtsCommand::VolumeContext(context) => {
                    if self.loudness.update_volume_context(context) {
                        self.metrics.mark_volume_context(context);
                        info!(
                            "event=fanin.volume_context canonical_db={:.1} downstream_db={:.1} tts_envelope_lufs={:.1} muted={} stamp_boot_ns={}",
                            context.canonical_db,
                            context.downstream_db,
                            context.tts_envelope_lufs,
                            context.muted,
                            context.stamp_boot_ns,
                        );
                    } else {
                        self.metrics.mark_volume_context_rejected();
                        warn!(
                            "event=fanin.volume_context_rejected reason=stale_or_invalid incoming_stamp_boot_ns={} accepted_stamp_boot_ns={}",
                            context.stamp_boot_ns,
                            self.loudness
                                .current_volume_context()
                                .map_or(0, |accepted| accepted.stamp_boot_ns),
                        );
                    }
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
                TtsCommand::SegmentStart {
                    kind,
                    provider_item_id,
                    profile,
                } => {
                    self.ledger.start_segment(provider_item_id, kind);
                    self.begin_segment_gain(kind, profile);
                }
                TtsCommand::SegmentEnd => {
                    let mut drained_completion = None;
                    if self.active_segment_kind == Some(SegmentKind::Assistant) {
                        if let Some(block) = self
                            .queue
                            .iter_mut()
                            .rev()
                            .find(|block| block.segment_serial == self.active_segment_serial)
                        {
                            block.completes_assistant_reference = true;
                        } else if let Some(playback) = self.assistant_segment_playback.take() {
                            if playback.segment_serial == self.active_segment_serial {
                                drained_completion = Some(playback);
                            }
                        }
                    }
                    self.active_segment_gain_db = None;
                    self.active_segment_kind = None;
                    self.active_segment_decision = None;
                    self.ledger.end_segment();
                    if let Some(playback) = drained_completion {
                        self.complete_assistant_reference(
                            &playback.decision,
                            linear_to_db(playback.last_gain_linear),
                            playback.context,
                        );
                    }
                    if self.assistant_reference_disqualified_serial
                        == Some(self.active_segment_serial)
                    {
                        self.assistant_reference_disqualified_serial = None;
                    }
                }
                TtsCommand::Close => {}
            }
        }
        self.metrics.mark_pending(self.pending_frames());
    }

    fn refresh_program_duck(&mut self) {
        self.program_duck_last_refresh = Some(Instant::now());
    }

    fn release_expired_program_duck(&mut self) {
        if !self.program_duck_active || self.pending_frames() > 0 {
            return;
        }
        let Some(last_refresh) = self.program_duck_last_refresh else {
            return;
        };
        let idle = last_refresh.elapsed();
        if idle < self.program_duck_idle_release_ttl {
            return;
        }
        info!(
            "event=fanin.program_duck on=false reason=idle_ttl idle_ms={}",
            idle.as_millis()
        );
        self.program_duck_active = false;
        self.program_duck_last_refresh = None;
        self.metrics.mark_program_duck_active(false);
    }

    fn begin_segment_gain(&mut self, kind: SegmentKind, profile: Option<AssistantProfile>) -> f32 {
        self.assistant_segment_playback = None;
        let decision = self
            .loudness
            .decide_gain(kind, self.current_gain_db, profile);
        let gain_db = decision.final_gain_db;
        log_assistant_loudness_decision(kind, &decision);
        self.metrics.mark_loudness(
            self.loudness.content_short_lufs(),
            self.loudness.content_anchor_lufs(),
            Some(&decision),
        );
        self.active_segment_gain_db = Some(gain_db);
        self.active_segment_kind = Some(kind);
        self.active_segment_decision = Some(Arc::new(decision));
        self.active_segment_serial = self.next_segment_serial;
        self.next_segment_serial = self.next_segment_serial.saturating_add(1);
        gain_db
    }

    fn target_gain_db(&self, block: &QueuedAudioBlock) -> f32 {
        let residual = block
            .decision
            .as_ref()
            .map_or(0.0, |decision| self.loudness.live_gain_delta_db(decision));
        sanitize_tts_gain_db((block.base_gain_db + residual).min(block.peak_cap_gain_db))
            .max(MIN_TTS_GAIN_DB)
    }

    fn complete_assistant_reference(
        &mut self,
        decision: &AssistantGainDecision,
        effective_gain_db: f32,
        playout_context: VolumeContext,
    ) {
        let Some(reference) = self.loudness.complete_assistant_segment_at(
            decision,
            effective_gain_db,
            playout_context,
        ) else {
            return;
        };
        info!(
            "event=fanin.assistant_reference.updated speaker_lufs={:.1} canonical_db={:.1} calibration_offset_lu={:.1}",
            reference.speaker_lufs,
            reference.canonical_db,
            reference.calibration_offset_lu,
        );
        if let Some(tx) = &self.assistant_reference_tx {
            let _ = tx.send(reference);
        }
        self.metrics
            .mark_held_references(self.loudness.held_content(), self.loudness.held_assistant());
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
        // Snapshot the ledger BEFORE clearing the queue: the events carry the
        // per-segment played/flushed split and provider item ids barge-in
        // truncation needs. `flushed` (cleared-queue frames) equals the
        // per-segment flushed total in normal operation.
        let events = self.ledger.flush();
        let flushed = self.clear_queue();
        debug_assert_eq!(
            flushed,
            events.iter().map(|e| e.flushed_frames).sum::<u64>(),
            "ledger flushed-frame total must equal the cleared audio queue depth"
        );
        self.active_segment_gain_db = None;
        self.active_segment_kind = None;
        self.active_segment_decision = None;
        self.assistant_segment_playback = None;
        self.assistant_reference_disqualified_serial = None;
        self.metrics.mark_flush(requests, flushed);
        self.metrics.mark_pending(0);
        let summary = FlushSummary::from_parts(requests, pending, flushed, &events);
        info!(
            "event=fanin.tts_flush requests={} pending_frames={} flushed_frames={} segments={} max_audio_played_ms={}",
            requests, pending, flushed, summary.segments, summary.max_audio_played_ms
        );
        for ack in ack_txs {
            let _ = ack.send(summary.clone());
        }
    }

    fn pending_frames(&self) -> u64 {
        self.pending_samples / (CHANNELS as u64)
    }

    fn clear_queue(&mut self) -> u64 {
        let frames = self.pending_frames();
        self.queue.clear();
        self.pending_samples = 0;
        self.gain_ramp = GainRamp::default();
        frames
    }
}

struct QueuedAudioBlock {
    samples: Vec<i16>,
    cursor: usize,
    base_gain_db: f32,
    peak_cap_gain_db: f32,
    peak_cap_linear: f32,
    decision: Option<Arc<AssistantGainDecision>>,
    segment_serial: u64,
    assistant_reference_eligible: bool,
    completes_assistant_reference: bool,
}

struct AssistantSegmentPlayback {
    segment_serial: u64,
    decision: Arc<AssistantGainDecision>,
    last_gain_linear: f32,
    context: VolumeContext,
}

#[derive(Default)]
struct GainRamp {
    initialized: bool,
    current_linear: f32,
    target_linear: f32,
    step_linear: f32,
    remaining_frames: u32,
    target_db: f32,
}

impl GainRamp {
    fn force_silent(&mut self) {
        self.initialized = true;
        self.current_linear = 0.0;
        self.target_linear = 0.0;
        self.step_linear = 0.0;
        self.remaining_frames = 0;
        // Force the next non-muted target through `retarget`, even when the
        // target itself is the -60 dB floor, so unmute always ramps from zero.
        self.target_db = f32::NAN;
    }

    fn retarget(&mut self, target_db: f32) {
        if self.initialized && (target_db - self.target_db).abs() < 0.01 {
            return;
        }
        let target_linear = gain_db_to_linear(target_db);
        if !self.initialized {
            self.initialized = true;
            self.current_linear = target_linear;
            self.target_linear = target_linear;
            self.target_db = target_db;
            return;
        }
        self.target_linear = target_linear;
        self.target_db = target_db;
        self.remaining_frames = LIVE_VOLUME_RAMP_FRAMES;
        self.step_linear = (target_linear - self.current_linear) / (LIVE_VOLUME_RAMP_FRAMES as f32);
    }

    fn next_frame(&mut self) -> f32 {
        if self.remaining_frames > 0 {
            self.current_linear += self.step_linear;
            self.remaining_frames -= 1;
            if self.remaining_frames == 0 {
                self.current_linear = self.target_linear;
            }
        }
        self.current_linear
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

pub fn tts_channels(max_pending_frames: u64) -> TtsChannelBundle {
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
    /// Build the ack from the ledger's flush events. `flushed_frames` is the
    /// mixer's cleared-queue count (kept for backward-compatible metrics); in
    /// normal operation it equals the per-segment flushed total, asserted in
    /// debug builds at the call site.
    fn from_parts(
        requests: usize,
        pending_frames: u64,
        flushed_frames: u64,
        events: &[PlayoutEvent],
    ) -> Self {
        let segments = events.len();
        let max_audio_played_ms = events.iter().map(|e| e.audio_played_ms).max().unwrap_or(0);
        Self {
            requests,
            pending_frames,
            flushed_frames,
            segments,
            max_audio_played_ms,
            events_json: render_events_json(events),
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

/// Render the ledger flush events as the ack's `events` JSON array. Field
/// names mirror `jasper-outputd`'s ack (`segment`, `written_frames`,
/// `drained_frames`) so barge-in consumes one shape regardless of which
/// daemon owns playout; at fan-in's single mix-commit point written ==
/// drained == played. `provider_item_id` is escaped (the upstream protocol
/// already restricts it to graphic ASCII, but strip quote/backslash as
/// defense in depth so a value can never break the JSON).
fn render_events_json(events: &[PlayoutEvent]) -> String {
    let mut json = String::from("[");
    for (i, e) in events.iter().enumerate() {
        if i > 0 {
            json.push(',');
        }
        let provider_item_id = match &e.provider_item_id {
            Some(id) => format!("\"{}\"", id.replace(['\\', '"'], "")),
            None => "null".to_string(),
        };
        json.push_str(&format!(
            "{{\"segment\":{},\"kind\":\"{}\",\"provider_item_id\":{},\"queued_frames\":{},\"written_frames\":{},\"drained_frames\":{},\"flushed_frames\":{}}}",
            e.local_segment_id,
            e.kind.as_str(),
            provider_item_id,
            e.queued_frames,
            e.played_frames,
            e.played_frames,
            e.flushed_frames,
        ));
    }
    json.push(']');
    json
}

fn fetch_max(cell: &AtomicU64, value: u64) {
    let mut current = cell.load(Ordering::Relaxed);
    while value > current {
        match cell.compare_exchange_weak(current, value, Ordering::Relaxed, Ordering::Relaxed) {
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

fn store_reference(
    reference: Option<HeldLoudnessReference>,
    speaker: &AtomicI64,
    canonical: &AtomicI64,
    calibration: Option<&AtomicI64>,
) {
    speaker.store(
        pack_optional_db(reference.map(|value| value.speaker_lufs)),
        Ordering::Relaxed,
    );
    canonical.store(
        pack_optional_db(reference.map(|value| value.canonical_db)),
        Ordering::Relaxed,
    );
    if let Some(calibration) = calibration {
        calibration.store(
            pack_optional_db(reference.map(|value| value.calibration_offset_lu)),
            Ordering::Relaxed,
        );
    }
}

fn unpack_reference(
    speaker: i64,
    canonical: i64,
    calibration: i64,
) -> Option<HeldLoudnessReference> {
    Some(HeldLoudnessReference {
        speaker_lufs: unpack_optional_db(speaker)? as f32,
        canonical_db: unpack_optional_db(canonical)? as f32,
        calibration_offset_lu: unpack_optional_db(calibration)? as f32,
    })
}

fn pack_reference_kind(kind: ReferenceKind) -> u64 {
    match kind {
        ReferenceKind::LiveContent => 1,
        ReferenceKind::HeldContent => 2,
        ReferenceKind::HeldAssistant => 3,
        ReferenceKind::FirstUseFallback => 4,
    }
}

fn unpack_reference_kind(value: u64) -> Option<&'static str> {
    match value {
        1 => Some("live_content"),
        2 => Some("held_content"),
        3 => Some("held_assistant"),
        4 => Some("first_use_fallback"),
        _ => None,
    }
}

fn log_assistant_loudness_decision(kind: SegmentKind, decision: &AssistantGainDecision) {
    info!(
        "event=fanin.assistant_loudness kind={} provider={} model={} voice={} reference={} calibrated={} confidence={:.2} baseline_lufs={:.1} target_lufs={:.1} target_speaker_lufs={} envelope_offset_lu={} source_lufs={:.1} source_peak_dbfs={:.1} requested_gain_db={:.1} peak_cap_gain_db={:.1} final_gain_db={:.1} reason={}",
        kind.as_str(),
        decision.provider.as_deref().unwrap_or("-"),
        decision.model.as_deref().unwrap_or("-"),
        decision.voice.as_deref().unwrap_or("-"),
        decision.reference_kind.as_str(),
        decision.calibrated,
        decision.profile_confidence,
        decision.baseline_lufs,
        decision.target_lufs,
        decision
            .target_speaker_lufs
            .map(|value| format!("{value:.1}"))
            .unwrap_or_else(|| "-".to_string()),
        decision
            .envelope_offset_lu
            .map(|value| format!("{value:.1}"))
            .unwrap_or_else(|| "-".to_string()),
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

    use std::cell::RefCell;
    use std::sync::Once;

    static TEST_LOGGER: TestLogger = TestLogger;
    static LOG_INIT: Once = Once::new();

    // Per-thread buffer: tests run on parallel threads, and a shared buffer's
    // capture_logs() clear() can race another test between its log emission
    // and its captured_logs() assertion (observed in CI as a one-off failure
    // of the stale_program_duck log assert while its sibling metrics assert
    // passed). Thread-local capture is sound here because every test drives
    // the mixer synchronously on its own thread; a test that logs from a
    // spawned thread would capture nothing and fail deterministically.
    thread_local! {
        static TEST_LOGS: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
    }

    struct TestLogger;

    impl log::Log for TestLogger {
        fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
            metadata.level() <= log::Level::Info
        }

        fn log(&self, record: &log::Record<'_>) {
            if self.enabled(record.metadata()) {
                let line = record.args().to_string();
                TEST_LOGS.with(|logs| logs.borrow_mut().push(line));
            }
        }

        fn flush(&self) {}
    }

    fn capture_logs() {
        LOG_INIT.call_once(|| {
            let _ = log::set_logger(&TEST_LOGGER);
            log::set_max_level(log::LevelFilter::Info);
        });
        TEST_LOGS.with(|logs| logs.borrow_mut().clear());
    }

    fn captured_logs() -> Vec<String> {
        TEST_LOGS.with(|logs| logs.borrow().clone())
    }

    use std::io::Cursor;

    fn run_tts_client_payload(
        payload: &[u8],
        tx: &SyncSender<QueuedTtsCommand>,
        flush_tx: &SyncSender<QueuedFlush>,
        epoch: &Arc<AtomicU64>,
        metrics: &TtsMetrics,
    ) {
        let (mut client, server) = UnixStream::pair().unwrap();
        let tx = tx.clone();
        let flush_tx = flush_tx.clone();
        let epoch = Arc::clone(epoch);
        let metrics = metrics.clone();
        let handle = thread::spawn(move || {
            handle_tts_client(server, tx, flush_tx, epoch, metrics);
        });
        client.write_all(payload).unwrap();
        client.flush().unwrap();
        drop(client);
        handle.join().unwrap();
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
        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::FlushSync)
        );
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
                tts_envelope_lufs: -38.5,
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
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
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

        // First-use quiet-room speech lands exactly on the envelope. The
        // ordinary music-relative assistant offset does not apply here.
        let expected = apply_gain_i16(10_000, gain_db_to_linear(-17.0)) as i32;
        assert_eq!(sum, vec![expected, -expected, expected, -expected]);
        assert_eq!(metrics.pending_frames(), 0);
        assert!(metrics.loudness_snapshot().decision_seen);
        drop(flush_tx);
    }

    #[test]
    fn segment_driven_auto_duck_is_light_for_cues_but_full_for_speech() {
        // While prepare_period() is true the mixer ducks the program lane to
        // program_duck_gain(). A standalone earcon/cue (TTS frames pending, no
        // explicit PROGRAM_DUCK_ON) must only lightly duck the music; a voice
        // turn (explicit duck) or assistant audio must still duck hard.
        let full = gain_db_to_linear(-24.0);
        let light = gain_db_to_linear(-6.0);
        let approx = |a: f32, b: f32| (a - b).abs() < 1e-6;

        fn new_mixer() -> (
            SyncSender<QueuedTtsCommand>,
            SyncSender<QueuedFlush>,
            TtsMixer,
        ) {
            let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
            let mixer = TtsMixer::new(TtsInput {
                rx,
                flush_rx,
                metrics,
                max_pending_frames: 48_000,
                program_duck_db: -24.0,
                cue_duck_db: -6.0,
                assistant_loudness: AssistantLoudnessConfig::default(),
                assistant_reference: None,
                assistant_reference_tx: None,
            });
            (tx, flush_tx, mixer)
        }
        fn queue_segment(tx: &SyncSender<QueuedTtsCommand>, kind: SegmentKind) {
            tx.send(QueuedTtsCommand {
                epoch: 0,
                command: TtsCommand::SegmentStart {
                    kind,
                    provider_item_id: None,
                    profile: None,
                },
            })
            .unwrap();
            tx.send(QueuedTtsCommand {
                epoch: 0,
                command: TtsCommand::Audio(vec![1_000, -1_000]),
            })
            .unwrap();
        }

        // Standalone mute/unmute sparkle (cue) → light duck.
        let (tx, flush_tx, mut mixer) = new_mixer();
        queue_segment(&tx, SegmentKind::Cue);
        assert!(mixer.prepare_period());
        assert!(approx(mixer.program_duck_gain(), light));
        drop(flush_tx);

        // Standalone wake/end chirp → light duck.
        let (tx, flush_tx, mut mixer) = new_mixer();
        queue_segment(&tx, SegmentKind::Chirp);
        assert!(mixer.prepare_period());
        assert!(approx(mixer.program_duck_gain(), light));
        drop(flush_tx);

        // Assistant audio, even absent an explicit duck → full duck.
        let (tx, flush_tx, mut mixer) = new_mixer();
        queue_segment(&tx, SegmentKind::Assistant);
        assert!(mixer.prepare_period());
        assert!(approx(mixer.program_duck_gain(), full));
        drop(flush_tx);

        // Explicit PROGRAM_DUCK_ON (a voice turn / spoken cue) → full duck,
        // regardless of which segment kind happens to be queued.
        let (tx, flush_tx, mut mixer) = new_mixer();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOn,
        })
        .unwrap();
        queue_segment(&tx, SegmentKind::Cue);
        assert!(mixer.prepare_period());
        assert!(approx(mixer.program_duck_gain(), full));
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
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                tts_envelope_lufs: -38.0,
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

        let expected = apply_gain_i16(10_000, gain_db_to_linear(-13.0)) as i32;
        assert_eq!(sum, vec![expected, -expected]);
        let loudness = metrics.loudness_snapshot();
        assert!(loudness.decision_seen);
        assert!(loudness.calibrated);
        assert_eq!(loudness.final_gain_db, Some(-13.0));
    }

    #[test]
    fn new_segment_peak_cap_applies_to_every_frame_during_gain_ramp() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });

        let profile = |source_lufs, source_peak_dbfs| AssistantProfile {
            provider: "openai".to_string(),
            model: "gpt-realtime-2".to_string(),
            voice: "marin".to_string(),
            source_lufs: Some(source_lufs),
            source_peak_dbfs: Some(source_peak_dbfs),
            confidence: 1.0,
        };
        for command in [
            TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                tts_envelope_lufs: -30.0,
            },
            TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("loud_prior".to_string()),
                profile: Some(profile(-30.0, -20.0)),
            },
            TtsCommand::Audio(vec![30_000, 30_000]),
            TtsCommand::SegmentEnd,
        ] {
            tx.send(QueuedTtsCommand { epoch: 0, command }).unwrap();
        }
        let mut prior = [0i32; 2];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut prior);
        assert_eq!(prior, [30_000, 30_000]);

        for command in [
            TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                tts_envelope_lufs: -30.0,
            },
            TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("capped_next".to_string()),
                profile: Some(profile(-50.0, 0.0)),
            },
            TtsCommand::Audio(vec![30_000; 128 * (CHANNELS as usize)]),
            TtsCommand::SegmentEnd,
        ] {
            tx.send(QueuedTtsCommand { epoch: 0, command }).unwrap();
        }
        let mut capped = vec![0i32; 128 * (CHANNELS as usize)];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut capped);

        let cap = apply_gain_i16(30_000, gain_db_to_linear(-3.0)).abs() as i32;
        assert!(
            capped.iter().all(|sample| sample.abs() <= cap),
            "every rendered frame must respect the new segment's -3 dB cap"
        );
    }

    #[test]
    fn live_volume_update_ramps_queued_speech_and_commits_the_achieved_reference() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(12_000);
        let (reference_tx, reference_rx) = mpsc::channel();
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 12_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: Some(HeldLoudnessReference {
                speaker_lufs: -41.0,
                canonical_db: -30.0,
                calibration_offset_lu: 0.0,
            }),
            assistant_reference_tx: Some(reference_tx),
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::VolumeContext(VolumeContext {
                canonical_db: -30.0,
                downstream_db: 0.0,
                tts_envelope_lufs: -41.0,
                muted: false,
                stamp_boot_ns: 1,
            }),
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                tts_envelope_lufs: -41.0,
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
                    source_lufs: Some(-41.0),
                    source_peak_dbfs: Some(-20.0),
                    confidence: 1.0,
                }),
            },
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![10_000; 9_600 * (CHANNELS as usize)]),
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::SegmentEnd,
        })
        .unwrap();

        let mut first = vec![0i32; 4_800 * (CHANNELS as usize)];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut first);
        assert!(first.iter().all(|sample| *sample == 10_000));

        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::VolumeContext(VolumeContext {
                canonical_db: -24.0,
                downstream_db: 0.0,
                tts_envelope_lufs: -39.44,
                muted: false,
                stamp_boot_ns: 2,
            }),
        })
        .unwrap();
        let mut second = vec![0i32; 4_800 * (CHANNELS as usize)];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut second);

        assert!(
            (second[0] - 10_000).abs() <= 1,
            "ramp starts without a step discontinuity"
        );
        assert!(second[200] > 10_000, "ramp makes audible progress");
        let expected_last = apply_gain_i16(10_000, gain_db_to_linear(1.56)) as i32;
        assert!((second[second.len() - 1] - expected_last).abs() <= 2);
        let reference = reference_rx
            .try_recv()
            .expect("completed assistant reference");
        assert!((reference.speaker_lufs - -39.44).abs() < 0.02);
        assert_eq!(reference.canonical_db, -24.0);
    }

    #[test]
    fn drained_audio_then_segment_end_commits_playout_time_reference() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(12_000);
        let (reference_tx, reference_rx) = mpsc::channel();
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 12_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: Some(reference_tx),
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::VolumeContext(VolumeContext {
                canonical_db: -30.0,
                downstream_db: 0.0,
                tts_envelope_lufs: -41.0,
                muted: false,
                stamp_boot_ns: 1,
            }),
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "m".to_string(),
                voice: "v".to_string(),
                tts_envelope_lufs: -41.0,
            },
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("late_end".to_string()),
                profile: Some(AssistantProfile {
                    provider: "openai".to_string(),
                    model: "m".to_string(),
                    voice: "v".to_string(),
                    source_lufs: Some(-30.0),
                    source_peak_dbfs: Some(-20.0),
                    confidence: 1.0,
                }),
            },
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![10_000, 10_000]),
        })
        .unwrap();

        let mut sum = vec![0i32; 2];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut sum);
        assert!(reference_rx.try_recv().is_err());

        // A dial update after playout but before provider SEGMENT_END must not
        // be combined with the old effective gain.
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::VolumeContext(VolumeContext {
                canonical_db: -20.0,
                downstream_db: 0.0,
                tts_envelope_lufs: -36.0,
                muted: false,
                stamp_boot_ns: 2,
            }),
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::SegmentEnd,
        })
        .unwrap();
        mixer.prepare_period();

        let reference = reference_rx.try_recv().expect("late END commits");
        assert!((reference.speaker_lufs - -41.0).abs() < 0.02);
        assert_eq!(reference.canonical_db, -30.0);
        assert!((reference.calibration_offset_lu - 0.0).abs() < 0.02);
    }

    #[test]
    fn muted_frame_disqualifies_late_end_assistant_reference() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(12_000);
        let (reference_tx, reference_rx) = mpsc::channel();
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 12_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: Some(reference_tx),
        });
        for command in [
            TtsCommand::VolumeContext(VolumeContext {
                canonical_db: -30.0,
                downstream_db: 0.0,
                tts_envelope_lufs: -41.0,
                muted: false,
                stamp_boot_ns: 1,
            }),
            TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "m".to_string(),
                voice: "v".to_string(),
                tts_envelope_lufs: -41.0,
            },
            TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("muted_late_end".to_string()),
                profile: Some(AssistantProfile {
                    provider: "openai".to_string(),
                    model: "m".to_string(),
                    voice: "v".to_string(),
                    source_lufs: Some(-41.0),
                    source_peak_dbfs: Some(-20.0),
                    confidence: 1.0,
                }),
            },
            TtsCommand::Audio(vec![10_000; 4 * (CHANNELS as usize)]),
        ] {
            tx.send(QueuedTtsCommand { epoch: 0, command }).unwrap();
        }

        let mut audible = [0i32; CHANNELS as usize];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut audible);
        assert!(audible.iter().all(|sample| *sample != 0));

        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::VolumeContext(VolumeContext {
                canonical_db: -30.0,
                downstream_db: 0.0,
                tts_envelope_lufs: -41.0,
                muted: true,
                stamp_boot_ns: 2,
            }),
        })
        .unwrap();
        let mut muted_tail = [1i32; 3 * (CHANNELS as usize)];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut muted_tail);
        assert!(muted_tail.iter().all(|sample| *sample == 1));

        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::SegmentEnd,
        })
        .unwrap();
        mixer.prepare_period();

        assert!(reference_rx.try_recv().is_err());
    }

    #[test]
    fn loudness_metrics_expose_accepted_context_rejections_and_held_reference() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(12_000);
        let held = HeldLoudnessReference {
            speaker_lufs: -41.0,
            canonical_db: -30.0,
            calibration_offset_lu: 0.5,
        };
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 12_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: Some(held),
            assistant_reference_tx: None,
        });
        let accepted = VolumeContext {
            canonical_db: -20.0,
            downstream_db: -20.0,
            tts_envelope_lufs: -38.0,
            muted: false,
            stamp_boot_ns: 20,
        };
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::VolumeContext(accepted),
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::VolumeContext(VolumeContext {
                stamp_boot_ns: 10,
                ..accepted
            }),
        })
        .unwrap();
        mixer.prepare_period();

        let snapshot = metrics.loudness_snapshot();
        assert_eq!(snapshot.volume_context, Some(accepted));
        assert_eq!(snapshot.volume_context_rejected, 1);
        assert_eq!(snapshot.held_assistant, Some(held));
    }

    #[test]
    fn flush_clears_drained_assistant_completion_candidate() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(12_000);
        let (reference_tx, reference_rx) = mpsc::channel();
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 12_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: Some(reference_tx),
        });
        for command in [
            TtsCommand::VolumeContext(VolumeContext {
                canonical_db: -30.0,
                downstream_db: 0.0,
                tts_envelope_lufs: -41.0,
                muted: false,
                stamp_boot_ns: 1,
            }),
            TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "m".to_string(),
                voice: "v".to_string(),
                tts_envelope_lufs: -41.0,
            },
            TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("flushed".to_string()),
                profile: Some(AssistantProfile {
                    provider: "openai".to_string(),
                    model: "m".to_string(),
                    voice: "v".to_string(),
                    source_lufs: Some(-30.0),
                    source_peak_dbfs: Some(-20.0),
                    confidence: 1.0,
                }),
            },
            TtsCommand::Audio(vec![10_000, 10_000]),
        ] {
            tx.send(QueuedTtsCommand { epoch: 0, command }).unwrap();
        }
        let mut sum = vec![0i32; 2];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut sum);

        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Flush,
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::SegmentEnd,
        })
        .unwrap();
        mixer.prepare_period();
        assert!(reference_rx.try_recv().is_err());
    }

    #[test]
    fn unmute_ramps_from_silence_even_at_the_gain_floor() {
        let mut ramp = GainRamp::default();
        ramp.retarget(0.0);
        assert_eq!(ramp.next_frame(), 1.0);

        ramp.force_silent();
        ramp.retarget(MIN_TTS_GAIN_DB);
        let first = ramp.next_frame();

        assert!(first > 0.0);
        assert!(first < gain_db_to_linear(MIN_TTS_GAIN_DB));
        for _ in 1..LIVE_VOLUME_RAMP_FRAMES {
            ramp.next_frame();
        }
        assert_eq!(ramp.current_linear, gain_db_to_linear(MIN_TTS_GAIN_DB));
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
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
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
    fn flush_sync_ack_reports_audio_played_ms_for_mid_segment_barge_in() {
        // 4800-frame period = 100 ms at 48 kHz, so the barge-in "within one
        // output period of the real played duration" criterion is 100 ms.
        const PERIOD_FRAMES: usize = 4800;
        const PERIOD_MS: u64 = 100;
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(96_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 96_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });

        // One assistant segment carrying 1000 ms (48000 frames) of audio.
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("item-7".to_string()),
                profile: None,
            },
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![6000i16; 48_000 * (CHANNELS as usize)]),
        })
        .unwrap();

        // Commit exactly three periods (300 ms) downstream before barge-in.
        let mut sum = vec![0i32; PERIOD_FRAMES * (CHANNELS as usize)];
        for _ in 0..3 {
            assert!(mixer.prepare_period());
            sum.iter_mut().for_each(|s| *s = 0);
            mixer.mix_period(&mut sum);
        }

        // Barge-in: synchronous flush mid-segment.
        let (ack_tx, ack_rx) = mpsc::sync_channel(1);
        flush_tx
            .send(QueuedFlush {
                epoch: 1,
                ack: Some(ack_tx),
            })
            .unwrap();
        mixer.prepare_period(); // drains the flush and answers the ack

        let ack = ack_rx.try_recv().expect("flush ack");
        // 3 periods x 4800 frames = 14400 frames committed = 300 ms heard.
        let expected_ms = 300u64;
        assert!(
            ack.max_audio_played_ms > 0,
            "barge-in needs a nonzero played-ms (was the hardcoded 0)"
        );
        assert!(
            ack.max_audio_played_ms.abs_diff(expected_ms) <= PERIOD_MS,
            "played-ms {} not within one {PERIOD_MS}ms period of {expected_ms}",
            ack.max_audio_played_ms,
        );
        assert_eq!(ack.max_audio_played_ms, expected_ms);
        assert_eq!(ack.segments, 1);
        // 48000 queued - 14400 heard = 33600 frames dropped unheard.
        assert_eq!(ack.flushed_frames, 33_600);

        // The ack JSON carries the per-segment provider item id and real
        // played-ms, and is no longer the hardcoded empty/zero shape.
        let line = ack.to_json_line();
        assert!(line.contains("\"provider_item_id\":\"item-7\""), "{line}");
        assert!(line.contains("\"max_audio_played_ms\":300"), "{line}");
        assert!(!line.contains("\"events\":[]"), "{line}");
        assert!(!line.contains("\"max_audio_played_ms\":0"), "{line}");
    }

    #[test]
    fn flush_sync_ack_satisfies_shared_key_contract() {
        // The FLUSH_SYNC ack key shape is a shared wire contract
        // (jasper-tts-protocol) so fan-in's solo ack and outputd's
        // bonded-member ack cannot drift apart under the one Python
        // consumer. outputd has the mirror of this test.
        use jasper_tts_protocol::{FLUSH_SYNC_ACK_EVENT_KEYS, FLUSH_SYNC_ACK_KEYS};

        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });
        // A flushed segment so the `events` array is non-empty and its keys
        // are exercised too.
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::SegmentStart {
                kind: SegmentKind::Assistant,
                provider_item_id: Some("item-x".to_string()),
                profile: None,
            },
        })
        .unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![7i16; 4 * (CHANNELS as usize)]),
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
        mixer.prepare_period();

        let line = ack_rx.try_recv().expect("flush ack").to_json_line();
        for key in FLUSH_SYNC_ACK_KEYS {
            assert!(
                line.contains(&format!("\"{key}\":")),
                "fan-in ack missing top-level key {key}: {line}"
            );
        }
        for key in FLUSH_SYNC_ACK_EVENT_KEYS {
            assert!(
                line.contains(&format!("\"{key}\":")),
                "fan-in ack missing event key {key}: {line}"
            );
        }
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
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOn,
        })
        .unwrap();

        assert!(mixer.prepare_period());
        assert!(metrics.program_duck_active());

        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOff,
        })
        .unwrap();
        assert!(!mixer.prepare_period());
        assert!(!metrics.program_duck_active());
    }

    #[test]
    fn one_shot_duck_client_close_does_not_release_before_restore() {
        let (tx, rx, flush_tx, flush_rx, metrics, epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });

        run_tts_client_payload(
            b"PROGRAM_DUCK_ON\nCLOSE\n",
            &tx,
            &flush_tx,
            &epoch,
            &metrics,
        );
        assert!(mixer.prepare_period());
        assert!(metrics.program_duck_active());

        run_tts_client_payload(
            b"AUDIO 8\n\x01\0\x02\0\x03\0\x04\0CLOSE\n",
            &tx,
            &flush_tx,
            &epoch,
            &metrics,
        );
        let mut sum = [0i32; 4];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut sum);
        assert!(metrics.program_duck_active());

        run_tts_client_payload(
            b"PROGRAM_DUCK_OFF\nCLOSE\n",
            &tx,
            &flush_tx,
            &epoch,
            &metrics,
        );
        assert!(!mixer.prepare_period());
        assert!(!metrics.program_duck_active());
    }

    #[test]
    fn flush_sync_dead_client_exit_does_not_own_duck_release() {
        let (tx, rx, flush_tx, flush_rx, metrics, epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });
        let (mut client, server) = UnixStream::pair().unwrap();
        let handle = thread::spawn(move || {
            handle_tts_client(server, tx, flush_tx, epoch, metrics);
        });

        client.write_all(b"PROGRAM_DUCK_ON\n").unwrap();
        client.flush().unwrap();
        let mut ducked = false;
        for _ in 0..50 {
            if mixer.prepare_period() {
                ducked = true;
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(ducked, "PROGRAM_DUCK_ON did not reach mixer");
        assert!(mixer.metrics.program_duck_active());

        client.write_all(b"FLUSH_SYNC\n").unwrap();
        client.flush().unwrap();
        drop(client);
        for _ in 0..50 {
            mixer.prepare_period();
            if handle.is_finished() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(
            handle.is_finished(),
            "FLUSH_SYNC client handler did not exit"
        );
        handle.join().unwrap();
        assert!(mixer.metrics.program_duck_active());

        mixer.program_duck_idle_release_ttl = Duration::from_millis(1);
        mixer.program_duck_last_refresh = Instant::now().checked_sub(Duration::from_secs(1));
        assert!(!mixer.prepare_period());
        assert!(!mixer.metrics.program_duck_active());
    }

    #[test]
    fn program_duck_audio_refresh_keeps_idle_ttl_alive() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });
        mixer.program_duck_idle_release_ttl = Duration::from_secs(60);
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOn,
        })
        .unwrap();
        assert!(mixer.prepare_period());
        mixer.program_duck_last_refresh = Instant::now().checked_sub(Duration::from_secs(120));
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![1, 2, 3, 4]),
        })
        .unwrap();

        let mut sum = [0i32; 4];
        assert!(mixer.prepare_period());
        mixer.mix_period(&mut sum);
        assert!(mixer.prepare_period());
        assert!(metrics.program_duck_active());
    }

    #[test]
    fn program_duck_idle_ttl_waits_for_zero_pending_frames() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });
        mixer.program_duck_idle_release_ttl = Duration::from_millis(1);
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOn,
        })
        .unwrap();
        assert!(mixer.prepare_period());
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![1, 2, 3, 4]),
        })
        .unwrap();
        assert!(mixer.prepare_period());
        assert_eq!(mixer.pending_frames(), 2);
        mixer.program_duck_last_refresh = Instant::now().checked_sub(Duration::from_secs(1));

        assert!(mixer.prepare_period());
        assert!(metrics.program_duck_active());
        let mut sum = [0i32; 4];
        mixer.mix_period(&mut sum);
        assert_eq!(mixer.pending_frames(), 0);
        assert!(!mixer.prepare_period());
        assert!(!metrics.program_duck_active());
    }

    #[test]
    fn program_duck_auto_releases_after_idle_ttl_without_audio() {
        let (tx, rx, _flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mut mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics: metrics.clone(),
            max_pending_frames: 48_000,
            program_duck_db: -25.0,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });
        mixer.program_duck_idle_release_ttl = Duration::from_millis(1);
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::ProgramDuckOn,
        })
        .unwrap();

        assert!(mixer.prepare_period());
        mixer.program_duck_last_refresh = Instant::now().checked_sub(Duration::from_secs(1));
        assert!(!mixer.prepare_period());
        assert!(!metrics.program_duck_active());
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
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
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
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
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
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
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
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
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
