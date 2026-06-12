//! Final mix math.
//!
//! The first output owner keeps the same intentionally boring
//! saturation behavior as `jasper-fanin`: accumulate in i32, then clamp
//! to i16. That makes clipping explicit and testable.

pub use jasper_tts_protocol::loudness::{
    apply_gain_i16, clamp_tts_gain_db, gain_db_to_linear, MAX_TTS_GAIN_DB, MIN_TTS_GAIN_DB,
};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct MixStats {
    pub clipped_samples: u32,
}

pub fn mix_i16_saturating(content: &[i16], assistant: &[i16], out: &mut [i16]) -> MixStats {
    debug_assert_eq!(content.len(), assistant.len());
    debug_assert_eq!(content.len(), out.len());

    let mut clipped_samples = 0u32;
    for ((&c, &a), o) in content.iter().zip(assistant).zip(out) {
        let mixed = (c as i32).saturating_add(a as i32);
        let clamped = mixed.clamp(i16::MIN as i32, i16::MAX as i32);
        if clamped != mixed {
            clipped_samples += 1;
        }
        *o = clamped as i16;
    }

    MixStats { clipped_samples }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sums_content_and_assistant_without_clipping() {
        let content = [1000, -1000, 500, -500];
        let assistant = [2000, 500, -500, 500];
        let mut out = [0; 4];

        let stats = mix_i16_saturating(&content, &assistant, &mut out);

        assert_eq!(out, [3000, -500, 0, 0]);
        assert_eq!(stats.clipped_samples, 0);
    }

    #[test]
    fn clamps_positive_and_negative_overflow() {
        let content = [i16::MAX, i16::MIN, 20_000, -20_000];
        let assistant = [1, -1, 20_000, -20_000];
        let mut out = [0; 4];

        let stats = mix_i16_saturating(&content, &assistant, &mut out);

        assert_eq!(out, [i16::MAX, i16::MIN, i16::MAX, i16::MIN]);
        assert_eq!(stats.clipped_samples, 4);
    }

    #[test]
    fn tts_gain_clamp_rejects_positive_and_nonfinite_values() {
        assert_eq!(clamp_tts_gain_db(0.0), MAX_TTS_GAIN_DB);
        assert_eq!(clamp_tts_gain_db(12.0), MAX_TTS_GAIN_DB);
        assert_eq!(clamp_tts_gain_db(f32::NAN), MIN_TTS_GAIN_DB);
        assert_eq!(clamp_tts_gain_db(f32::INFINITY), MIN_TTS_GAIN_DB);
    }

    #[test]
    fn tts_gain_clamp_preserves_safe_range_and_floor() {
        assert_eq!(clamp_tts_gain_db(-12.5), -12.5);
        assert_eq!(clamp_tts_gain_db(-100.0), MIN_TTS_GAIN_DB);
    }

    #[test]
    fn apply_gain_scales_and_clips() {
        assert_eq!(apply_gain_i16(10_000, 0.5), 5000);
        assert_eq!(apply_gain_i16(i16::MAX, 2.0), i16::MAX);
        assert_eq!(apply_gain_i16(i16::MIN, 2.0), i16::MIN);
    }
}
