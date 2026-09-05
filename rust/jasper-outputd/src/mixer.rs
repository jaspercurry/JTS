// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Final mix math.
//!
//! The output owner keeps the same intentionally boring saturation behavior as
//! `jasper-fanin`: accumulate one step wider than the samples, then clamp back.
//! That makes clipping explicit and testable. On the i32 program spine the
//! accumulator is **i64** — two i32 samples can sum past the i32 rail, and a
//! wrap there is a full-scale sign flip, the loudest defect this daemon can
//! produce.

pub use jasper_tts_protocol::loudness::{
    apply_gain, apply_gain_i16, gain_db_to_linear, sanitize_tts_gain_db, DEFAULT_TTS_GAIN_DB,
    MIN_TTS_GAIN_DB,
};

use crate::types::ProgramSample;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct MixStats {
    pub clipped_samples: u32,
}

/// Sum the content and assistant periods into `out`, clamping to the spine's
/// rails and counting every sample the clamp had to move.
///
/// The mix happens at the program spine's width, upstream of the single edge
/// quantization. `saturating_add` on the i64 accumulator is belt over the width
/// argument above — two i32 operands cannot overflow i64 — kept because a silent
/// wrap here is the one failure mode that would be catastrophic rather than
/// merely wrong.
pub fn mix_saturating(
    content: &[ProgramSample],
    assistant: &[ProgramSample],
    out: &mut [ProgramSample],
) -> MixStats {
    // PANIC-AUDITED: both buffers are period-sized scratch the daemon allocates once
    debug_assert_eq!(content.len(), assistant.len());
    // PANIC-AUDITED: out is that same fixed-size period allocation
    debug_assert_eq!(content.len(), out.len());

    let mut clipped_samples = 0u32;
    for ((&c, &a), o) in content.iter().zip(assistant).zip(out) {
        let mixed = (c as i64).saturating_add(a as i64);
        let clamped = mixed.clamp(ProgramSample::MIN as i64, ProgramSample::MAX as i64);
        if clamped != mixed {
            clipped_samples += 1;
        }
        *o = clamped as ProgramSample;
    }

    MixStats { clipped_samples }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One S16 sample at the program spine's scale — the same conversion ingest
    /// applies, so these fixtures read as plain S16 values.
    fn w(sample: i16) -> ProgramSample {
        jasper_resampler::widen_i16_to_i32(sample)
    }

    #[test]
    fn sums_content_and_assistant_without_clipping() {
        let content = [w(1000), w(-1000), w(500), w(-500)];
        let assistant = [w(2000), w(500), w(-500), w(500)];
        let mut out = [0; 4];

        let stats = mix_saturating(&content, &assistant, &mut out);

        assert_eq!(out, [w(3000), w(-500), 0, 0]);
        assert_eq!(stats.clipped_samples, 0);
    }

    #[test]
    fn clamps_positive_and_negative_overflow() {
        // The pre-spine vectors, widened, and the SAME verdict: all four samples
        // clip. Widening buys resolution BELOW the rail, not headroom above it —
        // `w(i16::MAX) + w(1)` is `0x7FFF_0000 + 0x0001_0000` = one past the i32
        // rail, exactly as `i16::MAX + 1` was one past the i16 rail. Anything
        // that clipped before still clips, at the same signal level.
        let content = [w(i16::MAX), w(i16::MIN), w(20_000), w(-20_000)];
        let assistant = [w(1), w(-1), w(20_000), w(-20_000)];
        let mut out = [0; 4];

        let stats = mix_saturating(&content, &assistant, &mut out);

        assert_eq!(
            out,
            [
                ProgramSample::MAX,
                ProgramSample::MIN,
                ProgramSample::MAX,
                ProgramSample::MIN
            ]
        );
        assert_eq!(stats.clipped_samples, 4);
    }

    #[test]
    fn clamps_at_the_spine_rails_and_counts_only_moved_samples() {
        // Exactly at the rails is NOT clipping; one step beyond is. The i64
        // accumulator is what makes this observable at all — in i32 the first
        // two sums would wrap to the opposite rail instead of saturating.
        let content = [i32::MAX, i32::MIN, i32::MAX, i32::MIN];
        let assistant = [0, 0, 1, -1];
        let mut out = [0; 4];

        let stats = mix_saturating(&content, &assistant, &mut out);

        assert_eq!(out, [i32::MAX, i32::MIN, i32::MAX, i32::MIN]);
        assert_eq!(stats.clipped_samples, 2);
    }

    #[test]
    fn a_silent_assistant_leaves_the_content_period_bit_identical() {
        // The transparency path's mix step: with no assistant audio the mix must
        // be the identity on the content spine, at every sample, including the
        // rails. If it were not, the wide path would not be byte-transparent
        // even before the edge.
        let content = [
            i32::MAX,
            i32::MIN,
            0,
            1,
            -1,
            w(i16::MAX),
            w(i16::MIN),
            12_345,
        ];
        let assistant = [0; 8];
        let mut out = [7i32; 8];

        let stats = mix_saturating(&content, &assistant, &mut out);

        assert_eq!(out, content);
        assert_eq!(stats.clipped_samples, 0);
    }

    #[test]
    fn tts_gain_sanitize_allows_positive_and_rejects_nonfinite_values() {
        assert_eq!(sanitize_tts_gain_db(0.0), DEFAULT_TTS_GAIN_DB);
        assert_eq!(sanitize_tts_gain_db(12.0), 12.0);
        assert_eq!(sanitize_tts_gain_db(f32::NAN), MIN_TTS_GAIN_DB);
        assert_eq!(sanitize_tts_gain_db(f32::INFINITY), MIN_TTS_GAIN_DB);
    }

    #[test]
    fn tts_gain_sanitize_preserves_safe_range_and_floor() {
        assert_eq!(sanitize_tts_gain_db(-12.5), -12.5);
        assert_eq!(sanitize_tts_gain_db(-100.0), MIN_TTS_GAIN_DB);
    }

    #[test]
    fn apply_gain_scales_and_clips() {
        assert_eq!(apply_gain_i16(10_000, 0.5), 5000);
        assert_eq!(apply_gain_i16(i16::MAX, 2.0), i16::MAX);
        assert_eq!(apply_gain_i16(i16::MIN, 2.0), i16::MIN);
    }
}
