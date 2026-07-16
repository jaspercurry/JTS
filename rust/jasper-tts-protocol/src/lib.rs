// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The JTS assistant/TTS protocol and shared playout policy.
//!
//! Newline-framed text commands with binary AUDIO payloads, spoken by
//! `jasper-voice` (client) to whichever daemon owns assistant playout:
//! `jasper-fanin` on a solo speaker, `jasper-outputd` on a bonded
//! multiroom member (HANDOFF-multiroom.md Increment 5 PR-2). Both
//! daemons previously carried byte-twin copies of this layer; this crate
//! is the extraction that makes wire drift impossible — the parser,
//! command vocabulary, and the SEGMENT_START profile types are defined
//! once and consumed by both ends.
//!
//! It also owns the shared K-weighted assistant loudness policy used by
//! fan-in and outputd. Queueing policy, epochs, metrics, the per-daemon
//! playout LEDGERS behind the flush-ack — and the VALUES they report
//! (fan-in's pre-DSP mix-commit estimate vs outputd's DAC-true one) — and
//! final mixing engines stay per-daemon; they may legitimately diverge
//! without breaking compatibility. Wire vocabulary may not: that means the
//! command parser AND the `FLUSH_SYNC` ack KEY shape
//! ([`FLUSH_SYNC_ACK_KEYS`] / [`FLUSH_SYNC_ACK_EVENT_KEYS`]), which the
//! Python consumer and barge-in truncation parse, plus assistant loudness
//! decisions.

use std::io::{self, BufRead};

pub mod loudness;

/// Wire frames are interleaved stereo S16LE.
pub const CHANNELS: u16 = 2;

/// Hard per-AUDIO-command byte cap (matches fanin: ~10.9 s of stereo
/// S16 at 48 kHz). A malformed length header cannot OOM the daemon.
pub const MAX_AUDIO_BYTES: usize = 2 * 1024 * 1024;

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
    Audio(Vec<i16>),
    SegmentEnd,
    Flush,
    FlushSync,
    Close,
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
        TtsCommand::SegmentEnd => "segment_end",
        TtsCommand::Flush => "flush",
        TtsCommand::FlushSync => "flush_sync",
        TtsCommand::Close => "close",
    }
}

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
            tts_envelope_lufs: parse_required_f32(
                tts_envelope,
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
    fn parser_accepts_stamped_volume_context_and_separate_prepare() {
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
                tts_envelope_lufs, ..
            } => {
                assert_eq!(*tts_envelope_lufs, -45.2);
            }
            other => panic!("unexpected {other:?}"),
        }
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
}
