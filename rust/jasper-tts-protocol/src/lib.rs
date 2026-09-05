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
//! Queueing policy, epochs, metrics, the per-daemon
//! playout LEDGERS behind the flush-ack — and the VALUES they report
//! (fan-in's pre-DSP mix-commit estimate vs outputd's DAC-true one) — and
//! final mixing engines stay per-daemon; they may legitimately diverge
//! without breaking compatibility. Wire vocabulary may not: that means the
//! command parser AND the `FLUSH_SYNC` ack KEY shape
//! ([`FLUSH_SYNC_ACK_KEYS`] / [`FLUSH_SYNC_ACK_EVENT_KEYS`]), which the
//! Python consumer and barge-in truncation parse, plus assistant loudness
//! decisions.

use std::io::{self, BufRead, Read};

pub mod assistant_reference;
pub mod loudness;

/// Wire frames are interleaved stereo.
pub const CHANNELS: u16 = 2;

/// The numeric width one AUDIO payload carries — the assistant half of the
/// campaign's single per-box wire resolution (U2 / #2223).
///
/// SELF-DESCRIBING, NOT NEGOTIATED. The writer spells its width in the command
/// verb (`AUDIO` / `AUDIO32`) and the reader parses exactly what arrived, so
/// there is no round trip and no agreement step. Both ends nevertheless derive
/// the SAME answer from the SAME two inputs, through the SAME rule —
/// [`TtsWireWidth::from_box_declaration`], which `jasper-fanin`'s
/// `Config::program_wire_is_wide` calls and
/// `jasper.fanin_coupling.assistant_wire_is_wide` mirrors — so in a coherent box
/// the declaration and the reader's own resolution agree by construction.
///
/// WHY A DISAGREEMENT IS A WARNING AND NOT A PARK. A ring header or an ALSA
/// open carries no width of its own, so guessing wrong there is a 96 dB level
/// error and must fail closed (`jasper_fanin::mixer::program_width_disagreement`).
/// This payload names its own width on the wire, so neither direction is a level
/// error: a narrow payload entering a wide mix is `widen_i16_to_i32`, a wide
/// payload entering a narrow mix is `narrow_i32_to_i16_round`, and both are the
/// exact conversions those primitives exist for. What a mismatch DOES mean is
/// config drift — the two processes read the box's declaration at different
/// times and one of them is stale — so each daemon logs it once for its
/// lifetime rather than muting the speaker's only voice path over a precision
/// difference. This is PR #2330's round-3 lesson applied: check each axis at its
/// own strength, and do not park a configuration that is merely suboptimal.
///
/// The drift window is bounded, but by TWO mechanisms rather than the one this
/// used to name. A COUPLING flip is covered by `coupling_reconcile`, which
/// `try-restart`s `jasper-voice` when the flip changes the resolved assistant
/// width. The other way the answer moves is the RESOLVER'S DEFAULT changing —
/// the ring wire's narrow→wide flip did exactly that, with no coupling flip and
/// no reconciler transition — and what bounds THAT is the deploy itself: a
/// default only moves in an install, and an install parks `jasper-voice` and
/// restarts it through `jasper-aec-reconcile`, replacing the process that
/// cached the old answer.
///
/// What neither covers is an operator hand-editing
/// `JASPER_FANIN_RING_WIRE_FORMAT` on a live box without running the documented
/// arm ladder. That box can sit mismatched until its next restart — which is
/// tolerable precisely because of the paragraph above: the payload names its own
/// width, so the cost is one unnecessary conversion and one warn per daemon
/// lifetime, not a level error.
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

    /// THE BOX'S ASSISTANT WIRE WIDTH — the one rule, over BOTH halves of the
    /// declaration.
    ///
    /// A wide wire needs the declared `S32_LE` ring wire format **and** the
    /// `shm_ring` coupling. The format alone is not enough and the omission is
    /// not academic: the coupling half is what says the wide payload has
    /// anywhere to go, since fan-in publishes through the ring and nothing
    /// else (ADR-0100), so a box that spelled `S32_LE` without the ring is
    /// narrow. That is exactly the conjunction
    /// `jasper_fanin::Config::program_wire_is_wide` has always applied; this is
    /// that function's body, lifted here so the Python control plane can mirror
    /// ONE rule instead of re-deriving it.
    ///
    /// SAY IT THAT WAY, not "an snd-aloop substream is an S16 device". That
    /// earlier phrasing was over-broad and a reader could design around a limit
    /// snd-aloop does not have: the **post-DSP content lane role** runs
    /// `S32_LE` on every box in the fleet, on a substream pair
    /// (`hw:Loopback,0/1,6`) on the same card. The claim is about the ROLE, not
    /// the device — that same pair also carries the bonded active-follower
    /// round-trip, which opens it raw at `S16_LE`.
    ///
    /// Callers pass their own already-parsed halves rather than tokens, because
    /// both ends have them typed by the time they ask: fan-in has `Coupling` and
    /// `RingWireFormat`, and Python has `resolve_coupling` /
    /// `resolve_ring_wire_format`. Re-stringifying to cross this boundary would
    /// add a parse that could disagree with the parse that produced it.
    ///
    /// `tests/test_ring_wire_format_contract.py` pins the VERDICT — all four
    /// pairings — against this function's source, not just the token vocabulary.
    pub fn from_box_declaration(
        wire_format_is_wide: bool,
        coupling_is_shm_ring: bool,
    ) -> TtsWireWidth {
        if wire_format_is_wide && coupling_is_shm_ring {
            TtsWireWidth::Wide
        } else {
            TtsWireWidth::Narrow
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
    /// sample is already there. Callers that mix into an i32-spine program
    /// (`jasper-outputd` always, `jasper-fanin` on a wide wire) use this so the
    /// promotion has one implementation.
    #[inline]
    pub fn spine_sample(&self, index: usize) -> i32 {
        match self {
            TtsAudioSamples::Narrow(s) => jasper_resampler::widen_i16_to_i32(s[index]),
            TtsAudioSamples::Wide(s) => s[index],
        }
    }

    /// One sample reduced to the i16 sample scale.
    ///
    /// A narrow sample is returned untouched — that identity is what keeps the
    /// narrow mix path byte-identical. A wide sample is narrowed with the
    /// shared round-to-nearest saturating quantizer, which inverts
    /// `widen_i16_to_i32` exactly.
    #[inline]
    pub fn narrow_sample(&self, index: usize) -> i16 {
        match self {
            TtsAudioSamples::Narrow(s) => s[index],
            TtsAudioSamples::Wide(s) => jasper_resampler::narrow_i32_to_i16_round(s[index]),
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
/// shape* must not, because one Python consumer (`jasper/audio_io.py`,
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
    Flush,
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
        TtsCommand::Flush => "flush",
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
    use std::io::Cursor;

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
            TtsCommand::Flush,
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
        // The narrow wire's own answer for that signal is silence (round to
        // nearest of a quarter-step is zero), which is the contrast.
        assert_eq!(wide.narrow_sample(0), 0);
        assert_eq!(wide.narrow_sample(1), 0);
        let narrow = TtsAudioSamples::Narrow(vec![0, 0]);
        assert_eq!(narrow.spine_sample(0), 0);
        assert_ne!(
            narrow.spine_sample(0),
            wide.spine_sample(0),
            "an S16 payload cannot carry what the S32 one just did",
        );
    }

    /// The two conversions are exact inverses on every promoted value, which is
    /// what makes a width DISAGREEMENT a precision question and not a level
    /// error — the reason this axis warns instead of parking.
    #[test]
    fn narrowing_a_promoted_payload_returns_the_original_sample() {
        for probe in [0i16, 1, -1, 1234, -4321, i16::MAX, i16::MIN] {
            let narrow = TtsAudioSamples::Narrow(vec![probe]);
            let promoted = narrow.spine_sample(0);
            assert_eq!(promoted, (probe as i32) << 16, "promotion must be << 16");
            let wide = TtsAudioSamples::Wide(vec![promoted]);
            assert_eq!(
                wide.narrow_sample(0),
                probe,
                "narrowing must invert the promotion exactly",
            );
        }
    }

    /// THE VERDICT TABLE — all four pairings of the box's two declared halves.
    ///
    /// Only the conjunction is wide. The single most important row is
    /// (wide format, loopback coupling) → **Narrow**: fan-in's aloop write is
    /// pinned narrow (`from_box_declaration` above), so a box that declared a
    /// wide format but never armed the ring must NOT speak `AUDIO32`. An
    /// earlier revision of this PR keyed the Python side on the format token
    /// alone and got that row wrong.
    ///
    /// `tests/test_ring_wire_format_contract.py` pins the Python mirror against
    /// this same table, by reading this function's source.
    #[test]
    fn the_assistant_width_needs_both_halves_of_the_box_declaration() {
        assert_eq!(
            TtsWireWidth::from_box_declaration(true, true),
            TtsWireWidth::Wide,
            "wide format + shm_ring coupling is the ONLY wide pairing",
        );
        assert_eq!(
            TtsWireWidth::from_box_declaration(true, false),
            TtsWireWidth::Narrow,
            "a declared-wide box that never armed the ring outputs to an S16 \
             substream and must stay narrow",
        );
        assert_eq!(
            TtsWireWidth::from_box_declaration(false, true),
            TtsWireWidth::Narrow,
        );
        assert_eq!(
            TtsWireWidth::from_box_declaration(false, false),
            TtsWireWidth::Narrow,
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
}
