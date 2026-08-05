// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Shared audio/reference types.

pub use jasper_tts_protocol::{AssistantProfile, SegmentKind};

pub const SAMPLE_RATE: u32 = 48_000;
pub const CHANNELS: u16 = 2;

/// The crate's ONE sample-format vocabulary.
///
/// Two different things are spelled with it, and the difference is the whole
/// point of the native-format write:
///
/// * [`AudioFormat::default`] — outputd's INTERNAL program format. Always
///   `S16Le`, everywhere: the mixer, the LR4 sub crossover, TTS, and the
///   published AEC reference are i16 end to end.
/// * `config::Config::declared_dac_format` — the FINAL HARDWARE EDGE, declared
///   by the DAC registry (`DacProfile.final_edge_format`) and emitted by
///   `jasper-audio-hardware-reconcile` as `JASPER_OUTPUTD_DAC_FORMAT`. The ALSA
///   backend requests exactly that at the edge and widens the i16 program into
///   it at the final write — nowhere else.
///
/// Before the native-format write there were three spellings of "the format":
/// this enum with a single variant, a `FORMAT` const beside it, and
/// `alsa_backend`'s own `const FORMAT: Format = Format::S16LE`. One vocabulary
/// now, and `alsa_backend::alsa_format` is the single mapping into ALSA's own
/// `Format`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleFormat {
    S16Le,
    S32Le,
}

impl SampleFormat {
    /// The `/state` + log wire value, spelled exactly as ALSA spells it.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::S16Le => "S16_LE",
            Self::S32Le => "S32_LE",
        }
    }
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
            // outputd's internal program format, NOT the DAC edge. It stays
            // `S16Le` whatever the edge negotiates: widening happens at the
            // final ALSA write and nowhere upstream of it.
            sample_format: SampleFormat::S16Le,
        }
    }
}

impl AudioFormat {
    pub fn samples_for_frames(&self, frames: u32) -> usize {
        (frames as usize) * (self.channels as usize)
    }
}
