//! Shared audio/reference types.

pub const SAMPLE_RATE: u32 = 48_000;
pub const CHANNELS: u16 = 2;
pub const FORMAT: SampleFormat = SampleFormat::S16Le;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleFormat {
    S16Le,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AudioFormat {
    pub sample_rate: u32,
    pub channels: u16,
    pub sample_format: SampleFormat,
}

impl Default for AudioFormat {
    fn default() -> Self {
        Self {
            sample_rate: SAMPLE_RATE,
            channels: CHANNELS,
            sample_format: FORMAT,
        }
    }
}

impl AudioFormat {
    pub fn samples_for_frames(&self, frames: u32) -> usize {
        (frames as usize) * (self.channels as usize)
    }
}

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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReferencePacket {
    pub stream_id: u64,
    pub sequence: u64,
    pub monotonic_ns: u64,
    pub format: AudioFormat,
    pub frame_count: u32,
    pub clipped_samples: u32,
    pub samples: Vec<i16>,
}
