// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Shared audio/reference types, and the program spine's width conversions.

use anyhow::Result;

pub use jasper_tts_protocol::{AssistantProfile, SegmentKind};

pub const SAMPLE_RATE: u32 = 48_000;
pub const CHANNELS: u16 = 2;

/// One sample of outputd's INTERNAL program — the spine every stage between
/// ingest and the final edge carries.
///
/// `i32`, left-justified: an S16 sample widened by `<< 16`, so full scale is the
/// i32 rail and the LSB is 1/65536 of what an S16 LSB was. It is a type alias
/// rather than a newtype on purpose — the mixer, the folds, and the gain math
/// stay plain integer arithmetic, and the alias exists so a signature reads
/// "program sample" instead of "some i32".
///
/// Why i32 and not f32/f64: this daemon runs `mlockall` on a 1 GB Pi and its
/// buffers are resident forever; i32 keeps the period buffers at 4 bytes/sample
/// with exact integer arithmetic, and the one place float is genuinely needed
/// (gain, ramps, biquads) uses **f64** — f32's 24-bit mantissa cannot represent
/// an i32 sample, so f32 gain math would silently truncate the bottom 7 bits of
/// every sample it touched.
///
/// The single quantization to the hardware's width happens at the DAC edge, in
/// `alsa_backend::AlsaBackend::write_dac_period`, and nowhere else on the output
/// path. Everything upstream of it is this type.
pub type ProgramSample = i32;

/// The crate's ONE sample-format vocabulary.
///
/// It spells TWO independent hops, and neither of them is the internal program
/// width (that is [`ProgramSample`], which is not a `SampleFormat` at all — the
/// spine has no ALSA-visible format because it never touches ALSA):
///
/// * `config::Config::content_format` — the CONTENT LANE, Camilla's post-DSP
///   snd-aloop hop, declared by `JASPER_OUTPUTD_CONTENT_FORMAT` (default
///   `S16Le`). Ingest requests exactly that and widens an S16 lane into the
///   `ProgramSample` spine as it reads.
/// * `config::Config::declared_dac_format` — the FINAL HARDWARE EDGE, declared
///   by the DAC registry (`DacProfile.final_edge_format`) and emitted by
///   `jasper-audio-hardware-reconcile` as `JASPER_OUTPUTD_DAC_FORMAT`. The ALSA
///   backend requests exactly that at the edge; an `S32Le` edge takes the spine
///   straight through, and an `S16Le` edge is where the one output-path
///   quantization happens.
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

/// Widen one S16 period into the program spine, or fail loudly on a length
/// mismatch.
///
/// The ONE place `jasper_resampler::widen_i16_to_i32_slice`'s `bool` becomes this
/// crate's `anyhow` idiom, so the "a mismatch would emit a short or stale period"
/// message has a single author instead of one copy per S16 ingress (the content
/// lane, the SHM ring, the round-trip FIFO, the rate-match bridge).
///
/// `Result` rather than `debug_assert!`, for two reasons. It matches the sibling
/// scratch-buffer check in `alsa_backend` — `deinterleave_4ch_to_dual_stereo`
/// bails on "scratch buffers are smaller than content period" — so the audio path
/// keeps ONE failure idiom. And a debug assertion would not run where it matters:
/// CI builds this crate with `cargo test --release`, where debug assertions are
/// compiled out, so its regression test would silently never fire (verified — the
/// `#[should_panic]` form fails there with "test did not panic as expected").
pub fn widen_period(samples: &[i16], out: &mut [ProgramSample]) -> Result<()> {
    if !jasper_resampler::widen_i16_to_i32_slice(samples, out) {
        anyhow::bail!(
            "outputd widening staging is {} samples but the period is {}; \
             writing that would emit a short or stale period",
            out.len(),
            samples.len()
        );
    }
    Ok(())
}

/// Narrow one program period to S16, or fail loudly on a length mismatch.
///
/// The counterpart to [`widen_period`], and the ONLY i32→i16 conversion on the
/// output path besides nothing else — it serves the S16 DAC edge, the S16
/// reference taps (:9891 + chip-ref, S16 by contract), the composite children,
/// and the loudness meter's S16 scratch. It rounds to nearest via
/// `jasper_resampler::narrow_i32_to_i16_round`; the truncating
/// `s32_high_word_to_s16` is UAC2 *capture* semantics and must never appear on an
/// output path.
pub fn narrow_period(samples: &[ProgramSample], out: &mut [i16]) -> Result<()> {
    if !jasper_resampler::narrow_i32_to_i16_round_slice(samples, out) {
        anyhow::bail!(
            "outputd narrowing staging is {} samples but the period is {}; \
             writing that would emit a short or stale period",
            out.len(),
            samples.len()
        );
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AudioFormat {
    pub sample_rate: u32,
    pub channels: u16,
}

impl Default for AudioFormat {
    fn default() -> Self {
        Self {
            sample_rate: SAMPLE_RATE,
            channels: CHANNELS,
        }
    }
}

impl AudioFormat {
    pub fn samples_for_frames(&self, frames: u32) -> usize {
        (frames as usize) * (self.channels as usize)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn widen_period_rejects_a_length_mismatch_and_names_both_sizes() {
        // The failure the shared wrapper exists to report: without it the pure
        // primitive's `false` would be dropped and the period written short or
        // stale, with nothing in any counter to show for it.
        let mut short = [0i32; 2];
        let err = widen_period(&[1i16, 2, 3, 4], &mut short).unwrap_err();
        let text = err.to_string();
        assert!(text.contains("widening staging is 2 samples"), "{text}");
        assert!(text.contains("period is 4"), "{text}");
    }

    #[test]
    fn narrow_period_rejects_a_length_mismatch_and_names_both_sizes() {
        let mut short = [0i16; 2];
        let err = narrow_period(&[1i32, 2, 3, 4], &mut short).unwrap_err();
        let text = err.to_string();
        assert!(text.contains("narrowing staging is 2 samples"), "{text}");
        assert!(text.contains("period is 4"), "{text}");
    }

    #[test]
    fn narrow_period_is_wired_to_the_rounding_primitive_not_the_truncating_one() {
        // `jasper-resampler` holds TWO i32->i16 conversions: the rounding one
        // this path must use, and `s32_high_word_to_s16`, which truncates by
        // design for UAC2 capture. On a widened S16 period they agree exactly, so
        // the transparency proof CANNOT tell them apart — swapping this wrapper to
        // the truncating primitive would leave every other test in this crate
        // green while putting a half-LSB downward bias on every sample at the
        // speaker edge, which is the exact audible defect the wide path exists to
        // remove. This vector is the only thing in outputd that distinguishes
        // them, so it is what pins the wiring.
        let off_grid = [-1i32, 32_768, -65_537, 98_304];
        let mut rounded = [0i16; 4];
        narrow_period(&off_grid, &mut rounded).unwrap();
        assert_eq!(
            rounded,
            [0, 1, -1, 2],
            "narrow_period must round to nearest"
        );

        // What truncation would have produced, so the contrast is measured here
        // rather than asserted from memory.
        let truncated: Vec<i16> = off_grid
            .iter()
            .map(|&s| jasper_resampler::s32_high_word_to_s16(s))
            .collect();
        assert_eq!(truncated, vec![-1, 0, -2, 1]);
        assert_ne!(rounded.to_vec(), truncated);
    }

    #[test]
    fn the_two_wrappers_are_an_exact_round_trip_at_period_shape() {
        // The pure primitives own the exhaustive proof
        // (`jasper_resampler::every_i16_value_survives_the_widen_narrow_round_trip`);
        // this pins that outputd's own wrappers are wired to THOSE primitives
        // and not to a look-alike pair.
        let period = [i16::MAX, i16::MIN, 0, 1, -1, 12_345, -12_345, 32_766];
        let mut wide = [0i32; 8];
        widen_period(&period, &mut wide).unwrap();
        assert_eq!(wide[0], 0x7FFF_0000);
        assert_eq!(wide[1], i32::MIN);
        let mut back = [0i16; 8];
        narrow_period(&wide, &mut back).unwrap();
        assert_eq!(back, period);
    }
}
