//! The JTS assistant/TTS wire protocol — the SINGLE definition.
//!
//! Newline-framed text commands with binary AUDIO payloads, spoken by
//! `jasper-voice` (client) to whichever daemon owns assistant playout:
//! `jasper-fanin` on a solo speaker, `jasper-outputd` on a bonded
//! multiroom member (HANDOFF-multiroom.md Increment 5 PR-2). Both
//! daemons previously carried byte-twin copies of this layer; this
//! crate is the extraction that makes wire drift impossible — the
//! parser, command vocabulary, and the SEGMENT_START profile types are
//! defined once and consumed by both ends.
//!
//! Scope is deliberately the WIRE only: queueing policy, epochs,
//! metrics, flush-ack summaries, and the mixing engines stay
//! per-daemon — they may legitimately diverge without breaking
//! compatibility. What lives here may not.

use std::io::{self, BufRead};

/// Wire frames are interleaved stereo S16LE.
pub const CHANNELS: u16 = 2;

/// Hard per-AUDIO-command byte cap (matches fanin: ~10.9 s of stereo
/// S16 at 48 kHz). A malformed length header cannot OOM the daemon.
pub const MAX_AUDIO_BYTES: usize = 2 * 1024 * 1024;

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

pub fn command_name(command: &TtsCommand) -> &'static str {
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
}
