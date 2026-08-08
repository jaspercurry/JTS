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
/// The conversion to the hardware's width happens at the DAC edge — in the
/// coherent single sink's `alsa_backend::AlsaBackend::write_dac_period`, and on a
/// composite sink in `alsa_backend::deinterleave_4ch_to_dual_stereo` as it splits
/// the period to the children's edge — nowhere upstream. Everything above that
/// edge is this type.
///
/// Either edge converts only when it is NARROWER than the spine, and then
/// exactly once: an `S16Le` edge — single DAC or composite child — is the one
/// quantization on the output path, and an `S24_3Le` edge is the same story at
/// 24 bits, while an `S32Le` edge is already the spine's own width and carries
/// these samples through untouched.
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
///   straight through, and a NARROWER edge — `S16Le` or `S24_3Le` — is where the
///   one output-path quantization happens.
///
/// Before the native-format write there were three spellings of "the format":
/// this enum with a single variant, a `FORMAT` const beside it, and
/// `alsa_backend`'s own `const FORMAT: Format = Format::S16LE`. One vocabulary
/// now, and `alsa_backend::alsa_format` is the single mapping into ALSA's own
/// `Format`.
///
/// `S24_3Le` is **not declared by any DAC profile today**, so no live box
/// resolves it: the registry accepts the value and nothing emits it. It exists
/// here because outputd must be able to PARSE what the reconciler may one day
/// write, and Rust ships first so the daemon is never the thing that has to catch
/// up (wide-output-path D9). Its write path is genuinely different — three packed
/// bytes per sample, so no `IO<'_, S>` sample type can carry it — which is the
/// other reason it lands on its own rather than riding a registry flip.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleFormat {
    S16Le,
    /// ALSA's `S24_3LE`: 24 signed bits in **three packed bytes**, no padding.
    /// Distinct from ALSA's `S24_LE`, which carries the same 24 bits inside a
    /// 4-byte word and is NOT in this vocabulary.
    S24_3Le,
    S32Le,
}

impl SampleFormat {
    /// The `/state` + log wire value, spelled exactly as ALSA spells it.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::S16Le => "S16_LE",
            Self::S24_3Le => "S24_3LE",
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
/// The counterpart to [`widen_period`]: it serves the S16 DAC edge, the S16
/// reference taps (:9891 + chip-ref, S16 by contract), and the loudness meter's
/// S16 scratch. It rounds to nearest via
/// `jasper_resampler::narrow_i32_to_i16_round_slice`; the truncating
/// `s32_high_word_to_s16` is UAC2 *capture* semantics and must never appear on an
/// output path.
///
/// It is not the only i32→i16 conversion in the crate: a composite sink's S16
/// children are narrowed by the SCALAR `narrow_i32_to_i16_round` inside
/// `alsa_backend::deinterleave_4ch_to_dual_stereo`, because that conversion is
/// strided (one 4-channel period fans out into two stereo ones) and has no whole
/// slice to hand this wrapper. Same rounding primitive, same rails; only the
/// traversal differs.
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

/// Narrow one program period to **packed little-endian 24-bit bytes** — ALSA's
/// `S24_3LE` wire — or fail loudly on a staging-length mismatch.
///
/// The third member of the edge-conversion family beside [`widen_period`] and
/// [`narrow_period`], and the reason it is a separate function rather than a
/// width parameter on `narrow_period`: its OUTPUT ELEMENT IS A BYTE, not a
/// sample. Three bytes per sample means the staging length is
/// `samples.len() * 3`, which no `&mut [i16]`-shaped signature can express, and
/// no `IO<'_, S>` sample type can carry — that difference in kind is what makes
/// the `S24_3LE` edge a dedicated write path (wide-output-path D9) instead of one
/// more arm over a generic writer.
///
/// It rounds to nearest via `jasper_resampler::narrow_i32_to_i24_le_slice`, the
/// same discipline the S16 edge gets from `narrow_i32_to_i16_round_slice`. There
/// is no truncating 24-bit sibling to reach for by mistake; the rounding is still
/// pinned by a test, because "there is nothing else to reach for" is not the same
/// as "the rounding term cannot be deleted".
///
/// **Dormant on every live box today** — no DAC profile declares an `S24_3Le`
/// edge, so nothing calls this in production. The message names both sizes for
/// the same reason its siblings do: a mismatch here would write a short or torn
/// period at a speaker.
pub fn narrow_period_i24_le(samples: &[ProgramSample], out: &mut [u8]) -> Result<()> {
    if !jasper_resampler::narrow_i32_to_i24_le_slice(samples, out) {
        anyhow::bail!(
            "outputd packed-24 staging is {} bytes but the period is {} samples \
             ({} bytes at {} bytes per sample); writing that would emit a short \
             or stale period",
            out.len(),
            samples.len(),
            samples.len() * jasper_resampler::I24_LE_BYTES_PER_SAMPLE,
            jasper_resampler::I24_LE_BYTES_PER_SAMPLE
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

    /// **LOAD-BEARING — do not weaken.** This is the ONLY test in the tree that
    /// catches `narrow_period` being wired to the truncating slice primitive.
    /// Every other guard, including both transparency proofs, passes under that
    /// swap (truncation is exact on multiples of 65536), so weakening this vector
    /// silently removes the only detection for a half-LSB downward bias on every
    /// sample at the speaker edge.
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
    fn the_format_vocabulary_spells_every_variant_the_way_alsa_does() {
        // `as_str` is the STATUS wire value and the journal token, and the
        // chip-AEC alignment identity records `dac.format` off it — so a
        // misspelling here silently invalidates or mis-matches commissioned
        // artifacts. Pin all three literals, including the underscore position in
        // `S24_3LE` (it is NOT `S24LE_3` or `S243LE`, whatever alsa-rs calls its
        // own enum variant).
        assert_eq!(SampleFormat::S16Le.as_str(), "S16_LE");
        assert_eq!(SampleFormat::S24_3Le.as_str(), "S24_3LE");
        assert_eq!(SampleFormat::S32Le.as_str(), "S32_LE");
    }

    #[test]
    fn packed_24_narrowing_emits_three_little_endian_bytes_per_sample() {
        // The wrapper is wired to the PACKING primitive, at the period shape the
        // audio path uses. The pure crate owns the exhaustive byte-order and
        // rounding proofs
        // (`jasper_resampler::the_le24_pack_writes_three_little_endian_bytes_per_sample`);
        // this pins that outputd reaches them, and at 3 bytes per sample.
        let period = [0x7FFF_FF00u32 as i32, i32::MIN, 0, 256];
        let mut packed = vec![0u8; period.len() * 3];
        narrow_period_i24_le(&period, &mut packed).unwrap();
        assert_eq!(
            packed,
            vec![
                0xFF, 0xFF, 0x7F, //
                0x00, 0x00, 0x80, //
                0x00, 0x00, 0x00, //
                0x01, 0x00, 0x00, //
            ]
        );
    }

    #[test]
    fn packed_24_narrowing_rounds_rather_than_truncating() {
        // The 24-bit twin of
        // `narrow_period_is_wired_to_the_rounding_primitive_not_the_truncating_one`.
        // A period of multiples of 256 cannot tell rounding from `>> 8`, so the
        // vectors are deliberately OFF-GRID, and what truncation would have
        // produced is MEASURED beside them rather than asserted from memory.
        let off_grid = [-1i32, 128, -257, 383];
        let mut packed = vec![0u8; off_grid.len() * 3];
        narrow_period_i24_le(&off_grid, &mut packed).unwrap();

        let rounded: Vec<i32> = packed
            .chunks_exact(3)
            .map(|b| {
                i32::from_le_bytes([b[0], b[1], b[2], if b[2] & 0x80 != 0 { 0xFF } else { 0 }])
            })
            .collect();
        assert_eq!(rounded, vec![0, 1, -1, 1], "the packed edge must round");

        let truncated: Vec<i32> = off_grid.iter().map(|&s| s >> 8).collect();
        assert_eq!(truncated, vec![-1, 0, -2, 1]);
        assert_ne!(rounded, truncated);
    }

    #[test]
    fn packed_24_narrowing_rejects_a_staging_that_is_not_three_bytes_per_sample() {
        // The failure the wrapper exists to report, and the message has to name
        // both sizes AND the stride — a staging sized in samples, or at the
        // 4-byte `S24_LE` stride, is the mistake a 3-byte format invites.
        let mut sample_sized = vec![0u8; 4];
        let err = narrow_period_i24_le(&[1i32, 2, 3, 4], &mut sample_sized).unwrap_err();
        let text = err.to_string();
        assert!(text.contains("packed-24 staging is 4 bytes"), "{text}");
        assert!(text.contains("period is 4 samples"), "{text}");
        assert!(text.contains("12 bytes at 3 bytes per sample"), "{text}");

        let mut four_byte_stride = vec![0u8; 16];
        assert!(narrow_period_i24_le(&[1i32, 2, 3, 4], &mut four_byte_stride).is_err());
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
