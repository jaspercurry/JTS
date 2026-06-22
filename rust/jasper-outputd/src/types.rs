// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Shared audio/reference types.

pub use jasper_tts_protocol::{AssistantProfile, SegmentKind};

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
