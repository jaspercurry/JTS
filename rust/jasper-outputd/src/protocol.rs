//! Local TTS IPC protocol for the outputd path.
//!
//! This is deliberately tiny and ordered. Python sends ASCII command
//! lines over a Unix stream; binary audio payloads immediately follow
//! `AUDIO <byte_len>\n`. The audio payload shape is 48 kHz stereo
//! S16_LE after Python has resampled/duplicated provider PCM, but
//! before outputd applies its final gain clamp.

use std::io::{self, BufRead};

use crate::types::CHANNELS;

pub const MAX_AUDIO_BYTES: usize = 2 * 1024 * 1024;
const FRAME_BYTES: usize = (CHANNELS as usize) * 2;

#[derive(Debug, Clone, PartialEq)]
pub enum TtsCommand {
    GainDb(f32),
    Audio(Vec<i16>),
    Flush,
    Close,
}

pub fn read_command<R: BufRead>(reader: &mut R) -> io::Result<Option<TtsCommand>> {
    let mut line = String::new();
    let n = reader.read_line(&mut line)?;
    if n == 0 {
        return Ok(None);
    }
    let line = line.trim_end_matches(['\r', '\n']);
    if line == "FLUSH" {
        return Ok(Some(TtsCommand::Flush));
    }
    if line == "CLOSE" {
        return Ok(Some(TtsCommand::Close));
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
        if byte_len % FRAME_BYTES != 0 {
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

    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        format!("unknown TTS command: {line}"),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn reads_gain_flush_and_close_commands() {
        let mut reader = Cursor::new(b"GAIN -12.5\nFLUSH\nCLOSE\n".to_vec());

        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::GainDb(-12.5))
        );
        assert_eq!(read_command(&mut reader).unwrap(), Some(TtsCommand::Flush));
        assert_eq!(read_command(&mut reader).unwrap(), Some(TtsCommand::Close));
        assert_eq!(read_command(&mut reader).unwrap(), None);
    }

    #[test]
    fn reads_audio_payload_as_little_endian_i16() {
        let payload = [1000i16, -1000, i16::MAX, i16::MIN]
            .into_iter()
            .flat_map(i16::to_le_bytes)
            .collect::<Vec<u8>>();
        let mut bytes = b"AUDIO 8\n".to_vec();
        bytes.extend_from_slice(&payload);
        let mut reader = Cursor::new(bytes);

        assert_eq!(
            read_command(&mut reader).unwrap(),
            Some(TtsCommand::Audio(vec![1000, -1000, i16::MAX, i16::MIN])),
        );
    }

    #[test]
    fn rejects_odd_audio_byte_count() {
        let mut reader = Cursor::new(b"AUDIO 3\nabc".to_vec());

        let err = read_command(&mut reader).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        assert!(err.to_string().contains("even"));
    }

    #[test]
    fn rejects_audio_payloads_that_do_not_end_on_stereo_frame_boundary() {
        let mut reader = Cursor::new(b"AUDIO 2\nab".to_vec());

        let err = read_command(&mut reader).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        assert!(err.to_string().contains("stereo frames"));
    }

    #[test]
    fn rejects_overlarge_audio_payloads_before_allocation() {
        let mut reader =
            Cursor::new(format!("AUDIO {}\n", MAX_AUDIO_BYTES + FRAME_BYTES).into_bytes());

        let err = read_command(&mut reader).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        assert!(err.to_string().contains("max chunk"));
    }

    #[test]
    fn rejects_unknown_command() {
        let mut reader = Cursor::new(b"START nope\n".to_vec());

        let err = read_command(&mut reader).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        assert!(err.to_string().contains("unknown"));
    }
}
