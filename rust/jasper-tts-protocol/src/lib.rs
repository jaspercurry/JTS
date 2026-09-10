// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The JTS assistant/TTS protocol and shared playout policy.
//!
//! Newline-framed text commands with binary AUDIO payloads, spoken by
//! `jasper-voice` (client) to whichever daemon owns assistant playout:
//! `jasper-fanin` on a solo speaker, `jasper-outputd` on a bonded
//! multiroom member. Both
//! daemons consume this one crate, which makes wire drift impossible —
//! the parser, command vocabulary, and the SEGMENT_START profile types
//! are defined once and consumed by both ends.
//!
//! It also owns the shared K-weighted assistant loudness policy used by
//! fan-in and outputd, and the versioned on-disk record that carries a
//! learned assistant reference across restarts ([`assistant_reference`]).
//! Queue capacity and the pending-frame budget, epochs, metrics, the
//! per-daemon playout LEDGERS behind the flush-ack — and the VALUES they report
//! (fan-in's pre-DSP mix-commit estimate vs outputd's DAC-true one) — and
//! final mixing engines stay per-daemon; they may legitimately diverge
//! without breaking compatibility. Wire vocabulary may not: that means the
//! command parser AND the `FLUSH_SYNC` ack KEY shape
//! ([`FLUSH_SYNC_ACK_KEYS`] / [`FLUSH_SYNC_ACK_EVENT_KEYS`]), which the
//! Python consumer and barge-in truncation parse, plus assistant loudness
//! decisions.

use std::fs;
use std::io::{self, BufRead, BufReader, Read};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{SyncSender, TrySendError};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use jasper_daemon::json::{event_age_ms, NEVER_MS};
use jasper_daemon::HELPER_STACK_BYTES;

pub mod assistant_reference;
pub mod loudness;

/// Wire frames are interleaved stereo.
pub const CHANNELS: u16 = 2;

/// The numeric width one AUDIO payload carries.
///
/// SELF-DESCRIBING, NOT NEGOTIATED. The writer spells its width in the command
/// verb (`AUDIO` / `AUDIO32`) and the reader parses exactly what arrived, so
/// there is no round trip, no agreement step, and nothing for the two ends to
/// disagree about.
///
/// BOTH WIDTHS STAY ON THE WIRE even though fan-in's program wire is `S32_LE`
/// unconditionally: a narrow payload entering the wide program is
/// `widen_i16_to_i32`, the exact conversion that primitive exists for, and
/// fan-in applies it at ingest. So a narrow writer costs one shift per sample,
/// not precision, and the narrow representation is what lets a narrow box
/// allocate a narrow queue (see [`TtsAudioSamples`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TtsWireWidth {
    /// `AUDIO` — interleaved stereo S16LE, the narrow wire.
    Narrow,
    /// `AUDIO32` — interleaved stereo S32LE at the i32 program-spine scale
    /// (`widen_i16_to_i32`'s scale: full scale is ±2^31, and an S16 value `s`
    /// appears as `s << 16`).
    Wide,
}

impl TtsWireWidth {
    /// Bytes per sample on the wire.
    pub fn sample_bytes(self) -> usize {
        match self {
            TtsWireWidth::Narrow => 2,
            TtsWireWidth::Wide => 4,
        }
    }

    /// The command verb a writer at this width spells.
    pub fn verb(self) -> &'static str {
        match self {
            TtsWireWidth::Narrow => "AUDIO",
            TtsWireWidth::Wide => "AUDIO32",
        }
    }
}

/// One audio payload's samples, at the width the wire declared.
///
/// Both daemons queue this rather than a `Vec<i16>` so a narrow box allocates
/// EXACTLY the bytes a bare `Vec<i16>` allocated — the narrow variant IS that
/// vector. `jasper-outputd` already declined to widen at enqueue for the same
/// reason (a multi-second reply's queue is `mlockall`'d); keeping the two
/// representations distinct honours that instead of paying the wide cost on
/// every box for a feature only a wide box uses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TtsAudioSamples {
    Narrow(Vec<i16>),
    Wide(Vec<i32>),
}

impl TtsAudioSamples {
    pub fn len(&self) -> usize {
        match self {
            TtsAudioSamples::Narrow(s) => s.len(),
            TtsAudioSamples::Wide(s) => s.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn width(&self) -> TtsWireWidth {
        match self {
            TtsAudioSamples::Narrow(_) => TtsWireWidth::Narrow,
            TtsAudioSamples::Wide(_) => TtsWireWidth::Wide,
        }
    }

    /// One sample promoted to the i32 program-spine scale.
    ///
    /// A narrow sample is widened with the shared `widen_i16_to_i32`; a wide
    /// sample is already there. Both daemons mix into an i32-spine program, so
    /// this is where the promotion has its one implementation.
    #[inline]
    pub fn spine_sample(&self, index: usize) -> i32 {
        match self {
            TtsAudioSamples::Narrow(s) => jasper_resampler::widen_i16_to_i32(s[index]),
            TtsAudioSamples::Wide(s) => s[index],
        }
    }
}

impl From<Vec<i16>> for TtsAudioSamples {
    fn from(samples: Vec<i16>) -> Self {
        TtsAudioSamples::Narrow(samples)
    }
}

impl From<Vec<i32>> for TtsAudioSamples {
    fn from(samples: Vec<i32>) -> Self {
        TtsAudioSamples::Wide(samples)
    }
}

/// Hard per-AUDIO-command byte cap (matches fanin: ~10.9 s of stereo
/// S16 at 48 kHz). A malformed length header cannot OOM the daemon.
pub const MAX_AUDIO_BYTES: usize = 2 * 1024 * 1024;

/// Hard cap for one newline-delimited command header. Production commands are
/// well under 1 KiB; this leaves generous metadata headroom while preventing a
/// local client from growing either daemon's parser buffer without bound.
pub const MAX_COMMAND_LINE_BYTES: usize = 8 * 1024;

/// Canonical top-level JSON keys of a `FLUSH_SYNC` acknowledgement line.
///
/// The ack is the response half of this wire protocol. fan-in (solo) and
/// outputd (bonded multiroom member) each render it from their OWN playout
/// ledger — the *values* differ (mix-commit vs DAC-true) but the *key
/// shape* must not, because one Python consumer (`jasper/tts_playout.py`,
/// `jasper/voice/turn_playback.py`) and the barge-in truncation path parse
/// both. Each daemon's tests assert its rendered ack satisfies this
/// contract; changing it is a deliberate wire change touching both daemons
/// and the Python consumer in the same PR. Extra keys are tolerated by the
/// `.get()`-based consumer; missing/renamed keys are the breakage this
/// pins.
pub const FLUSH_SYNC_ACK_KEYS: &[&str] = &[
    "ok",
    "requests",
    "pending_frames",
    "segments",
    "flushed_frames",
    "max_audio_played_ms",
    "events",
];

/// Canonical JSON keys of each object in a `FLUSH_SYNC` ack's `events`
/// array (the per-segment playout records barge-in truncation consumes).
/// See [`FLUSH_SYNC_ACK_KEYS`] for the contract rationale.
pub const FLUSH_SYNC_ACK_EVENT_KEYS: &[&str] = &[
    "segment",
    "kind",
    "provider_item_id",
    "queued_frames",
    "written_frames",
    "drained_frames",
    "flushed_frames",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SegmentKind {
    Assistant,
    Cue,
    Chirp,
}

impl SegmentKind {
    pub fn as_str(self) -> &'static str {
        match self {
            SegmentKind::Assistant => "assistant",
            SegmentKind::Cue => "cue",
            SegmentKind::Chirp => "chirp",
        }
    }

    pub fn from_protocol(value: &str) -> Option<Self> {
        match value {
            "assistant" => Some(SegmentKind::Assistant),
            "cue" => Some(SegmentKind::Cue),
            "chirp" => Some(SegmentKind::Chirp),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct AssistantProfile {
    pub provider: String,
    pub model: String,
    pub voice: String,
    pub source_lufs: Option<f32>,
    pub source_peak_dbfs: Option<f32>,
    pub confidence: f32,
}

/// Absolute speaker-volume facts supplied by the canonical volume owner.
///
/// ``canonical_db`` tracks user intent. ``downstream_db`` is attenuation
/// applied after the TTS mixer (CamillaDSP today). Keeping both absolute
/// makes updates idempotent and lets the mixer compensate across source
/// handoffs without knowing which renderer is active.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct VolumeContext {
    pub canonical_db: f32,
    pub downstream_db: f32,
    /// Product loudness envelope for speech when no music reference exists.
    pub tts_envelope_lufs: f32,
    pub muted: bool,
    /// CLOCK_BOOTTIME nanoseconds from the publisher's current boot.
    pub stamp_boot_ns: u64,
}

pub const MIN_ASSISTANT_PROFILE_DB: f32 = -120.0;
pub const MAX_ASSISTANT_PROFILE_DB: f32 = 0.0;

pub fn assistant_profile_db_in_range(value: f32) -> bool {
    value.is_finite() && (MIN_ASSISTANT_PROFILE_DB..=MAX_ASSISTANT_PROFILE_DB).contains(&value)
}

pub fn assistant_profile_confidence_in_range(value: f32) -> bool {
    value.is_finite() && (0.0..=1.0).contains(&value)
}

#[derive(Debug, Clone, PartialEq)]
pub enum TtsCommand {
    GainDb(f32),
    PrepareAssistant {
        provider: String,
        model: String,
        voice: String,
        tts_envelope_lufs: f32,
        volume_context: Option<VolumeContext>,
    },
    VolumeContext(VolumeContext),
    ContentMeterPause,
    ContentMeterResume,
    ProgramDuckOn,
    ProgramDuckOff,
    SegmentStart {
        kind: SegmentKind,
        provider_item_id: Option<String>,
        profile: Option<AssistantProfile>,
    },
    /// `AUDIO <bytes>` — interleaved stereo S16LE, the narrow wire.
    Audio(Vec<i16>),
    /// `AUDIO32 <bytes>` — interleaved stereo S32LE at the i32 program-spine
    /// scale, the wide wire. Sent only by a box whose ring wire format
    /// resolves to `S32_LE`; see [`TtsWireWidth`].
    AudioWide(Vec<i32>),
    SegmentEnd,
    FlushSync,
    Close,
}

impl TtsCommand {
    /// Whether this command carries an audio payload, at EITHER wire width.
    ///
    /// Both daemons gate stale-epoch logging, the pending-budget check, and
    /// frame accounting on "is this audio?". Asking here rather than at each
    /// site is what keeps a second payload verb from having to be remembered in
    /// seven places.
    pub fn is_audio(&self) -> bool {
        self.audio_width().is_some()
    }

    /// The wire width this payload declares, or `None` for a non-audio command.
    pub fn audio_width(&self) -> Option<TtsWireWidth> {
        match self {
            TtsCommand::Audio(_) => Some(TtsWireWidth::Narrow),
            TtsCommand::AudioWide(_) => Some(TtsWireWidth::Wide),
            _ => None,
        }
    }

    /// Whole stereo frames this command carries; 0 for a non-audio command.
    pub fn audio_frames(&self) -> u64 {
        let samples = match self {
            TtsCommand::Audio(samples) => samples.len(),
            TtsCommand::AudioWide(samples) => samples.len(),
            _ => return 0,
        };
        (samples / (CHANNELS as usize)) as u64
    }

    /// Take this command's payload as width-tagged samples, or `None` for a
    /// non-audio command. Consumes the command so the queue never copies a
    /// multi-second reply.
    pub fn into_audio_samples(self) -> Option<TtsAudioSamples> {
        match self {
            TtsCommand::Audio(samples) => Some(TtsAudioSamples::Narrow(samples)),
            TtsCommand::AudioWide(samples) => Some(TtsAudioSamples::Wide(samples)),
            _ => None,
        }
    }
}

pub fn command_name(command: &TtsCommand) -> &'static str {
    match command {
        TtsCommand::GainDb(_) => "gain",
        TtsCommand::PrepareAssistant { .. } => "prepare_assistant",
        TtsCommand::VolumeContext(_) => "volume_context",
        TtsCommand::ContentMeterPause => "content_meter_pause",
        TtsCommand::ContentMeterResume => "content_meter_resume",
        TtsCommand::ProgramDuckOn => "program_duck_on",
        TtsCommand::ProgramDuckOff => "program_duck_off",
        TtsCommand::SegmentStart { .. } => "segment_start",
        TtsCommand::Audio(_) => "audio",
        TtsCommand::AudioWide(_) => "audio32",
        TtsCommand::SegmentEnd => "segment_end",
        TtsCommand::FlushSync => "flush_sync",
        TtsCommand::Close => "close",
    }
}

pub fn read_command<R: BufRead>(reader: &mut R) -> io::Result<Option<TtsCommand>> {
    let mut line = String::new();
    let n = {
        let mut bounded = reader.take((MAX_COMMAND_LINE_BYTES + 1) as u64);
        bounded.read_line(&mut line)?
    };
    if n == 0 {
        return Ok(None);
    }
    if n > MAX_COMMAND_LINE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "TTS command line exceeds maximum length",
        ));
    }
    let line = line.trim_end_matches(['\r', '\n']);
    match line {
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
        let gain = parse_required_f32(rest, "GAIN value")?;
        return Ok(Some(TtsCommand::GainDb(gain)));
    }
    if let Some(rest) = line.strip_prefix("VOLUME_CONTEXT ") {
        let mut parts = rest.split(' ');
        let canonical_db = parts.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing VOLUME_CONTEXT canonical dB",
            )
        })?;
        let downstream_db = parts.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing VOLUME_CONTEXT downstream dB",
            )
        })?;
        let tts_envelope_lufs = parts.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing VOLUME_CONTEXT silence target",
            )
        })?;
        let muted = parts.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "missing VOLUME_CONTEXT mute")
        })?;
        let stamp_boot_ns = parts.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "missing VOLUME_CONTEXT stamp")
        })?;
        if parts.next().is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "VOLUME_CONTEXT expects exactly five arguments",
            ));
        }
        return Ok(Some(TtsCommand::VolumeContext(VolumeContext {
            canonical_db: parse_required_f32(canonical_db, "VOLUME_CONTEXT canonical dB")?,
            downstream_db: parse_required_f32(downstream_db, "VOLUME_CONTEXT downstream dB")?,
            tts_envelope_lufs: parse_required_f32(
                tts_envelope_lufs,
                "VOLUME_CONTEXT silence target",
            )?,
            muted: parse_bool_token(muted, "VOLUME_CONTEXT mute")?,
            stamp_boot_ns: stamp_boot_ns.parse::<u64>().map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "invalid VOLUME_CONTEXT stamp")
            })?,
        })));
    }
    if let Some(rest) = line.strip_prefix("AUDIO ") {
        let bytes = read_audio_payload(reader, rest, TtsWireWidth::Narrow)?;
        let samples = bytes
            .chunks_exact(2)
            .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]))
            .collect();
        return Ok(Some(TtsCommand::Audio(samples)));
    }
    if let Some(rest) = line.strip_prefix("AUDIO32 ") {
        let bytes = read_audio_payload(reader, rest, TtsWireWidth::Wide)?;
        let samples = bytes
            .chunks_exact(4)
            .map(|chunk| i32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]))
            .collect();
        return Ok(Some(TtsCommand::AudioWide(samples)));
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
                    source_lufs: parse_optional_profile_db(
                        source_lufs,
                        "SEGMENT_START source_lufs",
                    )?,
                    source_peak_dbfs: parse_optional_profile_db(
                        source_peak_dbfs,
                        "SEGMENT_START source_peak_dbfs",
                    )?,
                    confidence: parse_profile_confidence(confidence, "SEGMENT_START confidence")?,
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
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing PREPARE_ASSISTANT model",
            )
        })?;
        let voice = parts.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing PREPARE_ASSISTANT voice",
            )
        })?;
        let tts_envelope = parts.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "missing PREPARE_ASSISTANT silence target",
            )
        })?;
        let volume_context = match parts.next() {
            None => None,
            Some(canonical_db) => {
                let downstream_db = parts.next().ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "missing PREPARE_ASSISTANT downstream dB",
                    )
                })?;
                let context_tts_envelope_lufs = parts.next().ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "missing PREPARE_ASSISTANT context silence target",
                    )
                })?;
                let muted = parts.next().ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "missing PREPARE_ASSISTANT mute")
                })?;
                let stamp_boot_ns = parts.next().ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "missing PREPARE_ASSISTANT context stamp",
                    )
                })?;
                if parts.next().is_some() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "PREPARE_ASSISTANT expects four or nine arguments",
                    ));
                }
                Some(VolumeContext {
                    canonical_db: parse_required_f32(
                        canonical_db,
                        "PREPARE_ASSISTANT canonical dB",
                    )?,
                    downstream_db: parse_required_f32(
                        downstream_db,
                        "PREPARE_ASSISTANT downstream dB",
                    )?,
                    tts_envelope_lufs: parse_required_f32(
                        context_tts_envelope_lufs,
                        "PREPARE_ASSISTANT context silence target",
                    )?,
                    muted: parse_bool_token(muted, "PREPARE_ASSISTANT mute")?,
                    stamp_boot_ns: stamp_boot_ns.parse::<u64>().map_err(|_| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "invalid PREPARE_ASSISTANT context stamp",
                        )
                    })?,
                })
            }
        };
        validate_token(provider, "PREPARE_ASSISTANT provider")?;
        validate_token(model, "PREPARE_ASSISTANT model")?;
        validate_token(voice, "PREPARE_ASSISTANT voice")?;
        return Ok(Some(TtsCommand::PrepareAssistant {
            provider: provider.to_string(),
            model: model.to_string(),
            voice: voice.to_string(),
            tts_envelope_lufs: parse_required_f32(
                tts_envelope,
                "PREPARE_ASSISTANT silence target",
            )?,
            volume_context,
        }));
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        format!("unknown TTS command: {line}"),
    ))
}

// ---------------------------------------------------------------------
// The server half — bounds, counters and accept loop, defined once and
// consumed by both daemons.
// ---------------------------------------------------------------------

/// How long a client may take to finish a command it has ALREADY started
/// writing — a stall bound, not a latency target. See ADR-0254.
pub const TTS_FRAME_DEADLINE: Duration = Duration::from_secs(30);

/// Concurrent client connections a TTS server retains; sixteen reader
/// threads reserve 8 MiB of stack. See ADR-0254.
pub const TTS_MAX_CLIENTS: usize = 16;

/// Read one command, bounding only the time spent MID-FRAME.
///
/// An IDLE connection must never be dropped: the voice daemon holds one
/// connection for its whole process lifetime and idles for hours between
/// turns. So this blocks with no timeout until the first byte of the next
/// command arrives, arms `deadline` over the rest of that command — the verb
/// line plus any `AUDIO`/`AUDIO32` payload — then disarms it again.
///
/// A client that stalls mid-frame surfaces as an error for which
/// [`is_frame_timeout`] is true; the caller closes the connection.
pub fn read_command_deadlined(
    reader: &mut BufReader<UnixStream>,
    deadline: Duration,
) -> io::Result<Option<TtsCommand>> {
    if reader.fill_buf()?.is_empty() {
        return Ok(None);
    }
    reader.get_ref().set_read_timeout(Some(deadline))?;
    let command = read_command(reader);
    // Disarm on every path: the next command may be hours away, and a still
    // armed deadline would read as a stall. Best-effort, because a failing
    // disarm must not discard a command that was read whole, nor replace the
    // timeout error `is_frame_timeout` has to recognise.
    let _ = reader.get_ref().set_read_timeout(None);
    command
}

/// True for the error a read deadline produces. Unix reports a socket read
/// timeout as `EAGAIN`, which maps to `WouldBlock`; other platforms report
/// `TimedOut`.
pub fn is_frame_timeout(err: &io::Error) -> bool {
    matches!(
        err.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
    )
}

/// Bounded pool of per-connection server slots, and the refusals it has
/// handed back.
///
/// An accept loop takes a slot BEFORE it spawns the reader thread and moves
/// the slot into that thread, so the slot returns when the thread exits and
/// retained threads cannot exceed the capacity. Refusals are counted here, so
/// a server publishes them without keeping a second counter of its own.
#[derive(Clone, Debug)]
pub struct TtsClientSlots {
    in_use: Arc<AtomicUsize>,
    rejected: Arc<AtomicU64>,
    capacity: usize,
}

impl TtsClientSlots {
    pub fn new(capacity: usize) -> Self {
        Self {
            in_use: Arc::new(AtomicUsize::new(0)),
            rejected: Arc::new(AtomicU64::new(0)),
            capacity,
        }
    }

    /// A slot, or `Err` carrying this pool's refusal count INCLUDING this
    /// one — so a caller journals `Err(1)` and every Nth refusal after,
    /// letting the counter carry the rest.
    pub fn try_acquire(&self) -> Result<TtsClientSlot, u64> {
        let capacity = self.capacity;
        match self
            .in_use
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |in_use| {
                (in_use < capacity).then_some(in_use + 1)
            }) {
            Ok(_) => Ok(TtsClientSlot {
                in_use: Arc::clone(&self.in_use),
            }),
            Err(_) => Err(self.rejected.fetch_add(1, Ordering::Relaxed) + 1),
        }
    }

    pub fn in_use(&self) -> usize {
        self.in_use.load(Ordering::Relaxed)
    }

    /// Connections refused because every slot was taken.
    pub fn rejected(&self) -> u64 {
        self.rejected.load(Ordering::Relaxed)
    }
}

/// One occupied slot; returns to its pool on drop.
#[derive(Debug)]
pub struct TtsClientSlot {
    in_use: Arc<AtomicUsize>,
}

impl Drop for TtsClientSlot {
    fn drop(&mut self) {
        self.in_use.fetch_sub(1, Ordering::Relaxed);
    }
}

/// The socket-side tallies both TTS servers publish in their STATUS `tts`
/// block: the client-slot pool plus what the reader threads refused, timed
/// out on, or dropped. Cloneable handle over shared atomics — reader
/// threads write, the state server reads. See ADR-0254.
#[derive(Clone, Debug)]
pub struct TtsServerCounters {
    slots: TtsClientSlots,
    frame_timeouts: Arc<AtomicU64>,
    protocol_errors: Arc<AtomicU64>,
    dropped_commands: Arc<AtomicU64>,
    dropped_audio_frames: Arc<AtomicU64>,
    /// Milliseconds after `epoch` at the last dropped AUDIO command, or
    /// [`NEVER_MS`] while nothing has been dropped.
    last_drop_ms: Arc<AtomicU64>,
    /// Monotonic reference for `last_drop_ms`, captured once and copied (not
    /// re-read) by every clone, so all holders age a drop identically.
    epoch: Instant,
}

impl Default for TtsServerCounters {
    fn default() -> Self {
        Self {
            slots: TtsClientSlots::new(TTS_MAX_CLIENTS),
            frame_timeouts: Arc::new(AtomicU64::new(0)),
            protocol_errors: Arc::new(AtomicU64::new(0)),
            dropped_commands: Arc::new(AtomicU64::new(0)),
            dropped_audio_frames: Arc::new(AtomicU64::new(0)),
            last_drop_ms: Arc::new(AtomicU64::new(NEVER_MS)),
            epoch: Instant::now(),
        }
    }
}

impl TtsServerCounters {
    /// The pool [`serve`] draws connection slots from.
    pub fn slots(&self) -> &TtsClientSlots {
        &self.slots
    }

    /// One AUDIO command shed because the playout queue was full — the
    /// command and the frames it carried are counted together.
    pub fn mark_dropped_audio(&self, frames: u64) {
        self.dropped_commands.fetch_add(1, Ordering::Relaxed);
        self.dropped_audio_frames
            .fetch_add(frames, Ordering::Relaxed);
        self.last_drop_ms
            .store(self.epoch.elapsed().as_millis() as u64, Ordering::Relaxed);
    }

    pub fn mark_frame_timeout(&self) {
        self.frame_timeouts.fetch_add(1, Ordering::Relaxed);
    }

    /// One connection dropped because the client broke the wire protocol.
    pub fn mark_protocol_error(&self) {
        self.protocol_errors.fetch_add(1, Ordering::Relaxed);
    }

    pub fn connections_rejected(&self) -> u64 {
        self.slots.rejected()
    }

    pub fn tts_clients(&self) -> u64 {
        self.slots.in_use() as u64
    }

    pub fn frame_timeouts(&self) -> u64 {
        self.frame_timeouts.load(Ordering::Relaxed)
    }

    pub fn protocol_errors(&self) -> u64 {
        self.protocol_errors.load(Ordering::Relaxed)
    }

    pub fn dropped_commands(&self) -> u64 {
        self.dropped_commands.load(Ordering::Relaxed)
    }

    pub fn dropped_audio_frames(&self) -> u64 {
        self.dropped_audio_frames.load(Ordering::Relaxed)
    }

    /// How long ago the last AUDIO command was dropped, `None` until one is.
    /// Recency for `dropped_commands`: a count that stopped moving hours ago
    /// reads differently from one a live overload is still bumping.
    pub fn last_drop_age_ms(&self) -> Option<u64> {
        event_age_ms(
            self.epoch.elapsed().as_millis() as u64,
            self.last_drop_ms.load(Ordering::Relaxed),
        )
    }
}

/// Bind `path` and serve TTS clients on it, returning once it is listening.
///
/// One detached thread accepts; each admitted connection gets its own
/// thread running `handle`, which holds that connection's slot until it
/// returns. `daemon` (`fanin` / `outputd`) names the owner in thread names,
/// error contexts and `event=` lines; `log` takes those lines at whatever
/// level the caller journals at.
pub fn serve<H>(
    daemon: &'static str,
    path: &Path,
    slots: TtsClientSlots,
    log: impl Fn(String) + Send + 'static,
    handle: H,
) -> io::Result<()>
where
    H: Fn(UnixStream) + Clone + Send + 'static,
{
    let context = |e: &io::Error, what: String| io::Error::new(e.kind(), format!("{what}: {e}"));
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| {
            context(
                &e,
                format!("creating {daemon} TTS socket parent {}", parent.display()),
            )
        })?;
    }
    let _ = fs::remove_file(path);
    let listener = UnixListener::bind(path).map_err(|e| {
        context(
            &e,
            format!("binding {daemon} TTS socket {}", path.display()),
        )
    })?;
    thread::Builder::new()
        .name(format!("{daemon}-tts-ipc"))
        .stack_size(HELPER_STACK_BYTES)
        .spawn(move || {
            for stream in listener.incoming() {
                let stream = match stream {
                    Ok(stream) => stream,
                    Err(e) => {
                        log(format!(
                            "event={daemon}.tts_socket.accept_failed detail={e}"
                        ));
                        continue;
                    }
                };
                // Best-effort: a socket that refuses the option is still
                // worth serving. See ADR-0254.
                let _ = stream.set_write_timeout(Some(TTS_FRAME_DEADLINE));
                let slot = match slots.try_acquire() {
                    Ok(slot) => slot,
                    // Every refusal is counted for STATUS; only the first
                    // and every 100th afterward are worth a journal line, so
                    // a real ceiling stays visible past one transient
                    // refusal at boot.
                    Err(count) if count == 1 || count % 100 == 0 => {
                        log(format!(
                            "event={daemon}.tts_socket.connection_rejected \
                             max_clients={TTS_MAX_CLIENTS} count={count}"
                        ));
                        continue;
                    }
                    Err(_) => continue,
                };
                let handle = handle.clone();
                let spawned = thread::Builder::new()
                    .name(format!("{daemon}-tts-client"))
                    .stack_size(HELPER_STACK_BYTES)
                    .spawn(move || {
                        // Held for the connection's life; released when this
                        // thread ends.
                        let _slot = slot;
                        handle(stream);
                    });
                if let Err(e) = spawned {
                    log(format!("event={daemon}.tts_socket.spawn_failed detail={e}"));
                }
            }
        })
        .map_err(|e| context(&e, format!("spawning {daemon} TTS IPC accept thread")))?;
    Ok(())
}

/// One command a reader thread took off the wire, stamped with the flush
/// epoch current when it was read. Consumers gate on that stamp to discard
/// what a later flush superseded.
#[derive(Debug)]
pub struct QueuedTtsCommand {
    pub epoch: u64,
    pub command: TtsCommand,
}

/// Where one TTS server's reader threads hand what they read: the daemon's
/// playout queue, the flush epoch they stamp commands with, the socket
/// counters they bump, and the daemon name its `event=` lines carry.
#[derive(Clone)]
pub struct TtsCommandSink {
    pub daemon: &'static str,
    pub tx: SyncSender<QueuedTtsCommand>,
    pub epoch: Arc<AtomicU64>,
    pub counters: TtsServerCounters,
}

/// Read one admitted client until it closes, stalls mid-frame, breaks the
/// protocol, or loses its consumer — the loop both TTS servers run.
///
/// Only `FLUSH_SYNC` differs between them (the ack and its bookkeeping belong
/// to whichever daemon owns the consumer), so `flush` performs one, returning
/// false to close the connection. `on_command` is the daemon's own tally of
/// accepted commands, kept where a daemon publishes one.
pub fn serve_client(
    sink: &TtsCommandSink,
    stream: UnixStream,
    frame_deadline: Duration,
    log: impl Fn(String),
    on_command: impl Fn(),
    flush: impl Fn(&mut BufReader<UnixStream>) -> bool,
) {
    let daemon = sink.daemon;
    let mut reader = BufReader::new(stream);
    loop {
        match read_command_deadlined(&mut reader, frame_deadline) {
            Ok(Some(TtsCommand::Close)) | Ok(None) => return,
            Ok(Some(TtsCommand::FlushSync)) => {
                if !flush(&mut reader) {
                    return;
                }
            }
            Ok(Some(command)) => {
                on_command();
                let queued = QueuedTtsCommand {
                    epoch: sink.epoch.load(Ordering::SeqCst),
                    command,
                };
                if !try_enqueue_command(daemon, &sink.tx, queued, &sink.counters, &log) {
                    return;
                }
            }
            Err(e) if is_frame_timeout(&e) => {
                sink.counters.mark_frame_timeout();
                log(format!(
                    "event={daemon}.tts_socket.frame_timeout deadline_s={}",
                    frame_deadline.as_secs()
                ));
                return;
            }
            Err(e) => {
                sink.counters.mark_protocol_error();
                log(format!(
                    "event={daemon}.tts_socket.protocol_error detail={e}"
                ));
                return;
            }
        }
    }
}

/// Hand one command to a daemon's playout queue.
///
/// AUDIO that finds the queue full is DROPPED and counted — late speech is
/// worse than lost speech, and a reader thread parked on a send cannot read
/// the `FLUSH_SYNC` that ends the turn. Every other verb waits instead: losing a
/// `SEGMENT_END` or a `PROGRAM_DUCK_OFF` corrupts consumer state that no
/// later command repairs. Only the hand-off rule lives here; the queue's
/// capacity and any pending-frame budget stay with the daemon that owns the
/// consumer. See ADR-0254.
///
/// `daemon` and `log` name the owner and journal at its level, exactly as
/// [`serve`] takes them. False only when the consumer is gone.
pub fn try_enqueue_command(
    daemon: &str,
    tx: &SyncSender<QueuedTtsCommand>,
    queued: QueuedTtsCommand,
    counters: &TtsServerCounters,
    log: impl Fn(String),
) -> bool {
    if !queued.command.is_audio() {
        return enqueue_reliable_command(daemon, tx, queued, log);
    }
    match tx.try_send(queued) {
        Ok(()) => true,
        Err(TrySendError::Full(queued)) => {
            let frames = queued.command.audio_frames();
            counters.mark_dropped_audio(frames);
            log(format!(
                "event={daemon}.tts_command_dropped reason=queue_full command=audio \
                 epoch={} frames={frames}",
                queued.epoch
            ));
            true
        }
        Err(TrySendError::Disconnected(_)) => false,
    }
}

/// Wait for room, journaling the squeeze: a consumer that has stopped
/// draining shows up here before the turn stalls on it.
fn enqueue_reliable_command(
    daemon: &str,
    tx: &SyncSender<QueuedTtsCommand>,
    queued: QueuedTtsCommand,
    log: impl Fn(String),
) -> bool {
    match tx.try_send(queued) {
        Ok(()) => true,
        Err(TrySendError::Full(queued)) => {
            log(format!(
                "event={daemon}.tts_command_backpressure reason=queue_full command={} epoch={}",
                command_name(&queued.command),
                queued.epoch
            ));
            tx.send(queued).is_ok()
        }
        Err(TrySendError::Disconnected(_)) => false,
    }
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

/// Read one AUDIO/AUDIO32 binary payload after its length header.
///
/// Shared by both payload verbs so the byte cap, the sample alignment, and the
/// whole-stereo-frame rule are stated ONCE and cannot drift between widths. The
/// cap is a BYTE cap, deliberately: it bounds the allocation a malformed header
/// can request, and that bound must not move because a box declared a wider
/// wire. A wide payload therefore carries half the frames of a narrow one at
/// the cap — which is why the Python writer chunks by BYTES, not frames.
fn read_audio_payload<R: BufRead>(
    reader: &mut R,
    raw_len: &str,
    width: TtsWireWidth,
) -> io::Result<Vec<u8>> {
    let verb = width.verb();
    let byte_len = raw_len.parse::<usize>().map_err(|_| {
        io::Error::new(io::ErrorKind::InvalidData, format!("invalid {verb} length"))
    })?;
    if byte_len > MAX_AUDIO_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{verb} byte length exceeds max chunk size"),
        ));
    }
    let sample_bytes = width.sample_bytes();
    if byte_len % sample_bytes != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{verb} byte length must be a whole number of samples"),
        ));
    }
    let frame_bytes = (CHANNELS as usize) * sample_bytes;
    if byte_len % frame_bytes != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{verb} byte length must contain whole stereo frames"),
        ));
    }
    let mut bytes = vec![0u8; byte_len];
    reader.read_exact(&mut bytes)?;
    Ok(bytes)
}

fn parse_optional_f32(value: &str, field: &str) -> io::Result<Option<f32>> {
    if value == "-" {
        return Ok(None);
    }
    parse_required_f32(value, field).map(Some)
}

fn parse_optional_profile_db(value: &str, field: &str) -> io::Result<Option<f32>> {
    let Some(parsed) = parse_optional_f32(value, field)? else {
        return Ok(None);
    };
    if !assistant_profile_db_in_range(parsed) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "{field} must be between {MIN_ASSISTANT_PROFILE_DB:.0} and {MAX_ASSISTANT_PROFILE_DB:.0}"
            ),
        ));
    }
    Ok(Some(parsed))
}

fn parse_profile_confidence(value: &str, field: &str) -> io::Result<f32> {
    let parsed = parse_required_f32(value, field)?;
    if !assistant_profile_confidence_in_range(parsed) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{field} must be between 0 and 1"),
        ));
    }
    Ok(parsed)
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

fn parse_bool_token(value: &str, field: &str) -> io::Result<bool> {
    match value {
        "0" => Ok(false),
        "1" => Ok(true),
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("invalid {field}; expected 0 or 1"),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Write};

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
    fn parser_accepts_stamped_volume_context_and_legacy_prepare() {
        let cmds = parse_all(
            b"VOLUME_CONTEXT -36.4 -36.4 -45.2 0 123456\nPREPARE_ASSISTANT openai m v -45.2\n",
        );
        assert_eq!(
            cmds[0],
            TtsCommand::VolumeContext(VolumeContext {
                canonical_db: -36.4,
                downstream_db: -36.4,
                tts_envelope_lufs: -45.2,
                muted: false,
                stamp_boot_ns: 123456,
            })
        );
        match &cmds[1] {
            TtsCommand::PrepareAssistant {
                tts_envelope_lufs,
                volume_context,
                ..
            } => {
                assert_eq!(*tts_envelope_lufs, -45.2);
                assert_eq!(*volume_context, None);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn parser_accepts_atomic_prepare_volume_context() {
        let mut reader = Cursor::new(
            b"PREPARE_ASSISTANT openai m v -45.2 -36.4 -36.4 -45.2 1 123456\n".to_vec(),
        );

        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::PrepareAssistant {
                provider: "openai".to_string(),
                model: "m".to_string(),
                voice: "v".to_string(),
                tts_envelope_lufs: -45.2,
                volume_context: Some(VolumeContext {
                    canonical_db: -36.4,
                    downstream_db: -36.4,
                    tts_envelope_lufs: -45.2,
                    muted: true,
                    stamp_boot_ns: 123456,
                }),
            })
        );
    }

    #[test]
    fn parser_rejects_out_of_range_profile_metadata() {
        for line in [
            "SEGMENT_START assistant - gemini m1 v1 -120.1 -6.0 1.0\n",
            "SEGMENT_START assistant - gemini m1 v1 0.1 -6.0 1.0\n",
            "SEGMENT_START assistant - gemini m1 v1 -24.0 -120.1 1.0\n",
            "SEGMENT_START assistant - gemini m1 v1 -24.0 0.1 1.0\n",
            "SEGMENT_START assistant - gemini m1 v1 -24.0 -6.0 -0.1\n",
            "SEGMENT_START assistant - gemini m1 v1 -24.0 -6.0 1.1\n",
        ] {
            let mut reader = Cursor::new(line.as_bytes().to_vec());
            assert!(read_command(&mut reader).is_err(), "{line}");
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
    fn parser_rejects_oversized_command_lines_before_unbounded_growth() {
        let line = format!("GAIN {}\n", "1".repeat(MAX_COMMAND_LINE_BYTES));
        let mut reader = Cursor::new(line.into_bytes());
        let error = read_command(&mut reader).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("maximum length"), "{error}");
    }

    #[test]
    fn parser_rejects_non_finite_gain() {
        for value in ["NaN", "inf", "-inf"] {
            let mut reader = Cursor::new(format!("GAIN {value}\n").into_bytes());
            let error = read_command(&mut reader).unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::InvalidData);
            assert!(error.to_string().contains("non-finite"), "{error}");
        }
    }

    #[test]
    fn parser_eof_is_clean_close() {
        let mut reader = Cursor::new(Vec::new());
        assert!(matches!(read_command(&mut reader), Ok(None)));
    }

    #[test]
    fn flush_sync_ack_key_contract_is_stable() {
        // The shared FLUSH_SYNC ack wire shape. Both daemons' renderers and
        // the Python consumer agree on exactly these keys; changing either
        // list is a deliberate wire-contract change. Each daemon has a guard
        // test asserting its rendered ack contains every key here.
        assert_eq!(
            FLUSH_SYNC_ACK_KEYS,
            [
                "ok",
                "requests",
                "pending_frames",
                "segments",
                "flushed_frames",
                "max_audio_played_ms",
                "events",
            ]
        );
        assert_eq!(
            FLUSH_SYNC_ACK_EVENT_KEYS,
            [
                "segment",
                "kind",
                "provider_item_id",
                "queued_frames",
                "written_frames",
                "drained_frames",
                "flushed_frames",
            ]
        );
    }

    // ------------------------------------------------------------------
    // U2 PR-2 — the assistant wire's two widths.
    // ------------------------------------------------------------------

    /// THE NARROW WIRE'S BYTES, pinned exactly.
    ///
    /// Captured from the pre-change parser: `AUDIO 8` followed by four LE i16
    /// samples yields those four samples, and the command reports itself as
    /// narrow. Everything else in this PR is allowed to move; this is not.
    #[test]
    fn the_narrow_audio_verb_parses_exactly_as_it_always_has() {
        let mut reader = Cursor::new(b"AUDIO 8\n\x01\x00\x02\x00\xfe\xff\x00\x80".to_vec());
        let command = read_command(&mut reader).unwrap().unwrap();
        assert_eq!(command, TtsCommand::Audio(vec![1, 2, -2, i16::MIN]));
        assert_eq!(command.audio_width(), Some(TtsWireWidth::Narrow));
        assert_eq!(command.audio_frames(), 2);
        assert_eq!(command_name(&command), "audio");
    }

    /// The wide verb, and that it is a DIFFERENT verb rather than the same one
    /// reinterpreted — `AUDIO32` does not match the `AUDIO ` prefix, so the two
    /// cannot be confused by a reader that only knows one of them.
    #[test]
    fn the_wide_audio_verb_parses_s32_samples_at_spine_scale() {
        let mut reader = Cursor::new(
            b"AUDIO32 16\n\x00\x00\x01\x00\x00\x00\x02\x00\x34\x12\x00\x00\x00\x00\x00\x80"
                .to_vec(),
        );
        let command = read_command(&mut reader).unwrap().unwrap();
        assert_eq!(
            command,
            TtsCommand::AudioWide(vec![0x0001_0000, 0x0002_0000, 0x0000_1234, i32::MIN]),
        );
        assert_eq!(command.audio_width(), Some(TtsWireWidth::Wide));
        assert_eq!(command.audio_frames(), 2);
        assert_eq!(command_name(&command), "audio32");
    }

    /// A stream carrying an `AUDIO32` header must not be readable as `AUDIO`
    /// by accident: the prefix test is `"AUDIO "` WITH the space.
    #[test]
    fn the_wide_verb_is_not_a_prefix_of_the_narrow_one() {
        assert!(!"AUDIO32 16".starts_with("AUDIO "));
        assert_eq!(TtsWireWidth::Narrow.verb(), "AUDIO");
        assert_eq!(TtsWireWidth::Wide.verb(), "AUDIO32");
        assert_eq!(TtsWireWidth::Narrow.sample_bytes(), 2);
        assert_eq!(TtsWireWidth::Wide.sample_bytes(), 4);
    }

    /// Non-audio commands report no width and no frames — the property the
    /// daemons' stale-epoch and budget checks rely on.
    #[test]
    fn only_audio_commands_report_a_width_or_frames() {
        for command in [
            TtsCommand::ProgramDuckOn,
            TtsCommand::SegmentEnd,
            TtsCommand::FlushSync,
            TtsCommand::GainDb(-12.0),
        ] {
            assert!(!command.is_audio(), "{command:?} must not read as audio");
            assert_eq!(command.audio_width(), None);
            assert_eq!(command.audio_frames(), 0);
            assert_eq!(command.into_audio_samples(), None);
        }
        assert!(TtsCommand::Audio(vec![1, 2]).is_audio());
        assert!(TtsCommand::AudioWide(vec![1, 2]).is_audio());
    }

    /// Both verbs enforce the SAME byte cap and the SAME whole-stereo-frame
    /// rule, at their own sample size.
    #[test]
    fn the_wide_verb_rejects_oversized_partial_sample_and_partial_frame() {
        let mut reader = Cursor::new(format!("AUDIO32 {}\n", MAX_AUDIO_BYTES + 4).into_bytes());
        assert!(
            read_command(&mut reader).is_err(),
            "cap must apply to AUDIO32"
        );
        let mut reader = Cursor::new(b"AUDIO32 6\n".to_vec());
        assert!(read_command(&mut reader).is_err(), "6 bytes is 1.5 samples");
        let mut reader = Cursor::new(b"AUDIO32 4\n\x01\0\0\0".to_vec());
        assert!(
            read_command(&mut reader).is_err(),
            "one sample is half a frame"
        );
    }

    /// THE PRECISION CLAIM, stated as a contrast rather than a bare survival.
    ///
    /// A signal below the S16 grid reaches a wide payload's consumer intact and
    /// is entirely absent from a narrow one's — the narrow wire has no code for
    /// it at all. This is what the wide verb buys.
    #[test]
    fn a_sub_16_bit_signal_reaches_a_wide_payload_and_cannot_reach_a_narrow_one() {
        // 0x0000_4000: a quarter of one i16 LSB. Nothing on the S16 grid.
        let wide = TtsAudioSamples::Wide(vec![0x0000_4000, -0x0000_4000]);
        assert_eq!(wide.spine_sample(0), 0x0000_4000);
        assert_eq!(wide.spine_sample(1), -0x0000_4000);
        // The nearest thing the narrow wire can spell is silence, which is the
        // contrast.
        let narrow = TtsAudioSamples::Narrow(vec![0, 0]);
        assert_eq!(narrow.spine_sample(0), 0);
        assert_ne!(
            narrow.spine_sample(0),
            wide.spine_sample(0),
            "an S16 payload cannot carry what the S32 one just did",
        );
    }

    /// A payload's own accessors agree with the command that carried it.
    #[test]
    fn a_payload_reports_the_width_of_the_verb_that_delivered_it() {
        let narrow = TtsCommand::Audio(vec![1, 2, 3, 4])
            .into_audio_samples()
            .unwrap();
        assert_eq!(narrow.width(), TtsWireWidth::Narrow);
        assert_eq!(narrow.len(), 4);
        assert!(!narrow.is_empty());
        let wide = TtsCommand::AudioWide(vec![1, 2])
            .into_audio_samples()
            .unwrap();
        assert_eq!(wide.width(), TtsWireWidth::Wide);
        assert_eq!(wide.len(), 2);
        assert!(TtsAudioSamples::Narrow(Vec::new()).is_empty());
    }

    const TEST_DEADLINE: Duration = Duration::from_millis(20);

    /// A client that announces a payload and then stops writing is cut loose.
    #[test]
    fn a_mid_frame_stall_hits_the_deadline() {
        let (client, server) = UnixStream::pair().unwrap();
        let mut reader = BufReader::new(server);
        (&client).write_all(b"AUDIO 1000\n").unwrap();

        let err = read_command_deadlined(&mut reader, TEST_DEADLINE).unwrap_err();

        assert!(is_frame_timeout(&err), "unexpected error kind: {err:?}");
        drop(client);
    }

    /// The other half of the same rule: an idle client is NOT cut loose. The
    /// voice daemon holds one connection for hours between turns.
    #[test]
    fn an_idle_connection_outlives_the_deadline() {
        let (client, server) = UnixStream::pair().unwrap();
        let mut reader = BufReader::new(server);
        let writer = thread::spawn(move || {
            thread::sleep(TEST_DEADLINE * 3);
            (&client).write_all(b"FLUSH_SYNC\n").unwrap();
            client
        });

        let command = read_command_deadlined(&mut reader, TEST_DEADLINE).unwrap();

        assert_eq!(command, Some(TtsCommand::FlushSync));
        drop(writer.join().unwrap());
    }

    /// Past the ceiling a connection is refused and counted without taking a
    /// slot, and a slot its holder releases readmits the next one.
    #[test]
    fn the_client_ceiling_counts_refusals_and_readmits_a_released_slot() {
        let slots = TtsClientSlots::new(2);
        let first = slots.try_acquire().expect("within the ceiling");
        let second = slots.try_acquire().expect("within the ceiling");

        assert_eq!(slots.try_acquire().err(), Some(1), "third slot admitted");
        assert_eq!(slots.try_acquire().err(), Some(2), "refusals not counted");
        assert_eq!(slots.in_use(), 2, "a refusal must not consume a slot");
        assert_eq!(slots.rejected(), 2);

        drop(second);
        assert_eq!(slots.in_use(), 1);
        let third = slots.try_acquire().expect("released slot not readmitted");
        assert_eq!(slots.rejected(), 2, "an admission must not count a refusal");

        drop((first, third));
        assert_eq!(slots.in_use(), 0);
    }

    /// The counters both daemons embed: an AUDIO drop bumps the command and
    /// the frame tally together, and refusals reach STATUS through the pool
    /// the ceiling opens at.
    #[test]
    fn the_server_counters_tally_drops_timeouts_and_refusals() {
        let counters = TtsServerCounters::default();
        counters.mark_dropped_audio(480);
        counters.mark_dropped_audio(240);
        counters.mark_frame_timeout();

        assert_eq!(counters.dropped_commands(), 2);
        assert_eq!(counters.dropped_audio_frames(), 720);
        assert_eq!(counters.frame_timeouts(), 1);

        let held: Vec<_> = (0..TTS_MAX_CLIENTS)
            .map(|_| counters.slots().try_acquire().expect("within the ceiling"))
            .collect();
        assert_eq!(counters.tts_clients(), TTS_MAX_CLIENTS as u64);
        assert!(counters.slots().try_acquire().is_err());
        assert_eq!(counters.connections_rejected(), 1);
        drop(held);
        assert_eq!(counters.tts_clients(), 0);
    }

    /// Drive one client through [`serve_client`]: write `payload`, then go
    /// quiet and wait for the reader thread to end. That join is the "drops
    /// the client" half of the pins below.
    fn serve_client_payload(payload: &[u8]) -> TtsServerCounters {
        let counters = TtsServerCounters::default();
        let (tx, _rx) = std::sync::mpsc::sync_channel(1);
        let sink = TtsCommandSink {
            daemon: "test",
            tx,
            epoch: Arc::new(AtomicU64::new(0)),
            counters: counters.clone(),
        };
        let (mut client, server) = UnixStream::pair().unwrap();
        let handle = thread::spawn(move || {
            serve_client(
                &sink,
                server,
                Duration::from_millis(20),
                |_| {},
                || {},
                |_| true,
            );
        });

        client.write_all(payload).unwrap();
        client.flush().unwrap();
        handle.join().unwrap();
        drop(client);
        counters
    }

    /// A client that breaks the wire is dropped, and the break reaches the
    /// counters both daemons publish from.
    #[test]
    fn serve_client_counts_a_protocol_error_and_drops_the_client() {
        let counters = serve_client_payload(b"NOT_A_COMMAND\n");

        assert_eq!(counters.protocol_errors(), 1);
        assert_eq!(counters.frame_timeouts(), 0);
    }

    /// A client that announces a payload and then stops writing is dropped on
    /// the frame deadline, so its reader thread cannot park forever.
    #[test]
    fn serve_client_counts_a_frame_timeout_and_drops_the_client() {
        let counters = serve_client_payload(b"AUDIO 1000\n");

        assert_eq!(counters.frame_timeouts(), 1);
        assert_eq!(counters.protocol_errors(), 0);
    }

    /// [`serve`] creates its socket's parent, hands every admitted connection
    /// to `handle` on its own thread, refuses past the ceiling with ONE
    /// journal line, and readmits when a handler returns its slot.
    #[test]
    fn serve_admits_up_to_the_ceiling_and_readmits_a_released_slot() {
        let dir = std::env::temp_dir().join(format!("jts-tts-serve-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let path = dir.join("tts.sock");
        let slots = TtsClientSlots::new(1);
        let (log_tx, log_rx) = std::sync::mpsc::channel();
        let (seen_tx, seen_rx) = std::sync::mpsc::channel();

        serve(
            "test",
            &path,
            slots.clone(),
            move |line| log_tx.send(line).unwrap(),
            move |stream| {
                // Runs until the client hangs up, so the slot stays held.
                seen_tx.send(()).unwrap();
                let mut reader = BufReader::new(stream);
                while matches!(read_command(&mut reader), Ok(Some(_))) {}
            },
        )
        .expect("serve failed to bind");

        let first = UnixStream::connect(&path).expect("first client refused");
        seen_rx.recv_timeout(Duration::from_secs(5)).unwrap();

        let second = UnixStream::connect(&path).expect("connect past the ceiling");
        let rejected = log_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert!(
            rejected.contains("event=test.tts_socket.connection_rejected")
                && rejected.contains("count=1"),
            "unexpected log line: {rejected}"
        );
        assert_eq!(slots.rejected(), 1);
        drop(second);

        // The handler returns on EOF, and its slot returns with the thread.
        drop(first);
        let freed = std::time::Instant::now();
        while slots.in_use() != 0 {
            assert!(freed.elapsed() < Duration::from_secs(5), "slot never freed");
            std::hint::spin_loop();
        }
        let third = UnixStream::connect(&path).expect("readmission refused");
        seen_rx.recv_timeout(Duration::from_secs(5)).unwrap();

        assert_eq!(slots.rejected(), 1, "readmission counted as a refusal");
        drop(third);
        let _ = fs::remove_dir_all(&dir);
    }

    /// The hand-off rule both daemons share: a full queue SHEDS audio at
    /// either wire width, counts the command and its frames together, and
    /// leaves the connection alive. Blocking here would stall the reader
    /// thread that has to read the `FLUSH_SYNC` ending the turn.
    #[test]
    fn a_full_queue_sheds_audio_and_counts_it() {
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        let counters = TtsServerCounters::default();
        let (log_tx, log_rx) = std::sync::mpsc::channel();
        let log = |line: String| log_tx.send(line).unwrap();

        let queue = |epoch, command| {
            try_enqueue_command(
                "test",
                &tx,
                QueuedTtsCommand { epoch, command },
                &counters,
                log,
            )
        };
        assert!(queue(1, TtsCommand::Audio(vec![0; 8])), "the only slot");
        assert!(
            queue(2, TtsCommand::AudioWide(vec![0; 12])),
            "a shed frame must not close the connection"
        );

        assert_eq!(counters.dropped_commands(), 1);
        assert_eq!(
            counters.dropped_audio_frames(),
            6,
            "12 samples / 2 channels"
        );
        let dropped = log_rx.try_recv().expect("the drop went unjournaled");
        assert!(
            dropped.contains("event=test.tts_command_dropped")
                && dropped.contains("epoch=2")
                && dropped.contains("frames=6"),
            "unexpected log line: {dropped}"
        );

        assert_eq!(rx.try_recv().unwrap().epoch, 1);
        assert!(rx.try_recv().is_err(), "the shed command was queued anyway");
    }

    /// A control verb never drops: the reader waits for the consumer to make
    /// room, because losing a `SEGMENT_END` or a `PROGRAM_DUCK_OFF` corrupts
    /// state that no later command repairs.
    #[test]
    fn a_control_verb_waits_for_room_instead_of_dropping() {
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        let counters = TtsServerCounters::default();
        let (log_tx, log_rx) = std::sync::mpsc::channel();
        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![0; 8]),
        })
        .unwrap();

        let sender = {
            let counters = counters.clone();
            thread::spawn(move || {
                try_enqueue_command(
                    "test",
                    &tx,
                    QueuedTtsCommand {
                        epoch: 0,
                        command: TtsCommand::ProgramDuckOff,
                    },
                    &counters,
                    |line| log_tx.send(line).unwrap(),
                )
            })
        };

        let squeezed = log_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert!(
            squeezed.contains("event=test.tts_command_backpressure")
                && squeezed.contains("command=program_duck_off"),
            "unexpected log line: {squeezed}"
        );

        // Freeing the slot is the ONLY way the verb can arrive; had it been
        // shed like audio, this second read would time out.
        assert!(rx
            .recv_timeout(Duration::from_secs(5))
            .unwrap()
            .command
            .is_audio());
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(5)).unwrap().command,
            TtsCommand::ProgramDuckOff
        );
        assert!(sender.join().unwrap(), "a landed command reported failure");
        assert_eq!(counters.dropped_commands(), 0, "control counted as a drop");
    }
}
