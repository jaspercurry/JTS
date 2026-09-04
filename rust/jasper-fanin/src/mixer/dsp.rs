// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Nothing here touches ALSA or `Mixer` state, so every function is testable
//! from values alone.

use super::*;

/// Accumulate one lane's i16 period into the sum at the sum's own scale, with
/// saturating arithmetic. Pulled out for unit testability — no ALSA needed.
///
/// `Narrow` adds the sample as-is; `Wide` promotes it with the shared,
/// information-preserving `widen_i16_to_i32` first.
pub(super) fn mix_into(sum: &mut [i64], input: &[i16], width: ProgramWidth) {
    debug_assert_eq!(sum.len(), input.len());
    match width {
        ProgramWidth::Narrow => {
            for (s, &i) in sum.iter_mut().zip(input) {
                *s = s.saturating_add(i as i64);
            }
        }
        ProgramWidth::Wide => {
            for (s, &i) in sum.iter_mut().zip(input) {
                *s = s.saturating_add(jasper_resampler::widen_i16_to_i32(i) as i64);
            }
        }
    }
}

/// Accumulate one lane's **already spine-scale** period into the sum — the USB
/// DIRECT lane on a wide wire (#2223), the only producer with more than 16
/// significant bits to contribute. Nothing is shifted and nothing is narrowed,
/// so the low word a hi-res host sends survives from `readi` to the summed
/// write.
///
/// Only meaningful against a [`ProgramWidth::Wide`] sum: a narrow sum is in the
/// i16 scale and adding a spine-scale sample to it would be 96 dB of gain. The
/// mixer picks the pairing per lane from the ONE resolved width, and the
/// `debug_assert` states that contract for anyone who wires a new caller.
pub(super) fn mix_into_wide(sum: &mut [i64], input: &[i32], width: ProgramWidth) {
    debug_assert_eq!(sum.len(), input.len());
    debug_assert_eq!(
        width,
        ProgramWidth::Wide,
        "a spine-scale lane may only enter a spine-scale sum",
    );
    for (s, &i) in sum.iter_mut().zip(input) {
        *s = s.saturating_add(i as i64);
    }
}

/// Apply a period-stable gain to the accumulated program sum. Used
/// after pre-duck content metering so the assistant loudness baseline
/// tracks the listener-facing content, not the temporary ducked level.
///
/// Width-dispatched. A linear gain commutes with the promotion, so the two arms
/// are the same operation on paper — but not in floating point, and not at the
/// rails:
///
/// **The rails.** `Narrow` keeps the `i32` clamp, where it is unreachable (a
/// narrow sum of every lane is ~2^18 and a duck only ever attenuates). `Wide`
/// drops it: a spine-scale sum LEGITIMATELY exceeds `i32::MAX` — that headroom
/// above full scale is the reason the accumulator is `i64` — and the duck's
/// whole job is to pull such a sum back into range. Clamping to `i32` first
/// would spend the headroom before the duck could use it, turning a recoverable
/// over-full-scale sum into a clipped one. Saturation is the consumer's job
/// (`saturate_to_i16` / `clamp_sum_to_spine`), where it can see the value that
/// is actually leaving.
///
/// **The mantissa.** `f32` carries 24 bits, so `sum as f32` at spine scale
/// discards the bottom bits before the multiply happens — the same reason
/// `apply_gain` exists beside `apply_gain_i16`. `Wide` computes in `f64`, whose
/// 53-bit mantissa represents every `i32` (and every reachable sum) exactly.
/// `Narrow` stays `f32`: a narrow sum is under 2^24, so `f32` holds it exactly,
/// and an `f64` product can round differently in the last place — identical
/// arithmetic is not identical bytes.
///
/// Rust's float→int cast saturates, so the `Wide` arm needs no `i64` clamp of
/// its own; a product that overflowed would land on the rails rather than wrap.
pub(super) fn apply_gain_to_sum(sum: &mut [i64], gain: f32, width: ProgramWidth) {
    match width {
        ProgramWidth::Narrow => {
            for sample in sum {
                *sample = ((*sample as f32) * gain)
                    .round()
                    .clamp(i32::MIN as f32, i32::MAX as f32) as i64;
            }
        }
        ProgramWidth::Wide => {
            for sample in sum {
                *sample = ((*sample as f64) * f64::from(gain)).round() as i64;
            }
        }
    }
}

/// Per-frame linear-gain slew such that a full 0.0→1.0 traversal takes
/// `ms` milliseconds at `sample_rate`. Floored so a misconfigured 0 can't
/// divide by zero (config validation already bounds `ms >= 1`).
pub(super) fn duck_step_per_frame(ms: u32, sample_rate: u32) -> f32 {
    let frames = (ms.max(1) as f32) * (sample_rate.max(1) as f32) / 1000.0;
    1.0 / frames.max(1.0)
}

/// Glide `current` toward `target` and apply the gliding gain to the
/// interleaved program sum, one linear step per frame. Ducking DOWN uses
/// `attack_step`; releasing UP uses `release_step`. The clamp to `target`
/// means it never overshoots and lands exactly, so callers can compare
/// `current == target` to detect a settled duck. Returns the updated
/// `current` for the caller to persist across periods.
///
/// A ~25 dB program duck that switches level in one sample injects a broadband
/// click and a "pump" into music playing under a short earcon/cue; ramping the
/// edges removes both.
///
/// Width-dispatched for exactly the reasons [`apply_gain_to_sum`] documents —
/// this is the same multiply with a per-frame gain, and its steady state is
/// asserted equal to that function's.
pub(super) fn ramp_program_duck(
    sum: &mut [i64],
    channels: usize,
    mut current: f32,
    target: f32,
    attack_step: f32,
    release_step: f32,
    width: ProgramWidth,
) -> f32 {
    debug_assert!(channels >= 1);
    let frames = sum.len() / channels;
    for f in 0..frames {
        if current > target {
            current = (current - attack_step).max(target);
        } else if current < target {
            current = (current + release_step).min(target);
        }
        if current != 1.0 {
            let base = f * channels;
            match width {
                ProgramWidth::Narrow => {
                    for s in &mut sum[base..base + channels] {
                        *s = ((*s as f32) * current)
                            .round()
                            .clamp(i32::MIN as f32, i32::MAX as f32)
                            as i64;
                    }
                }
                ProgramWidth::Wide => {
                    for s in &mut sum[base..base + channels] {
                        *s = ((*s as f64) * f64::from(current)).round() as i64;
                    }
                }
            }
        }
    }
    current
}

/// Clamp the sum back to i16 for an S16 consumer — the narrow ring wire and the
/// assistant content meter. Pulled out for unit testability.
///
/// `Narrow` is a bare clamp: the sum is already in the i16 numeric scale, so the
/// only question is saturation.
///
/// `Wide` has 16 more bits to shed, and sheds them with the shared
/// [`jasper_resampler::narrow_i32_to_i16_round`] — a round-to-nearest quantizer,
/// NOT a truncating shift. It inverts `widen_i16_to_i32` exactly, so a wide sum
/// built only from promoted i16 lanes narrows back to the identical bytes the
/// narrow sum would have produced; a wide sum carrying real low bits rounds
/// rather than stepping half an LSB toward −∞ on every sample.
pub(super) fn saturate_to_i16(sum: &[i64], out: &mut [i16], width: ProgramWidth) {
    debug_assert_eq!(sum.len(), out.len());
    match width {
        ProgramWidth::Narrow => {
            for (o, &s) in out.iter_mut().zip(sum) {
                *o = s.clamp(i16::MIN as i64, i16::MAX as i64) as i16;
            }
        }
        ProgramWidth::Wide => {
            for (o, &s) in out.iter_mut().zip(sum) {
                *o = jasper_resampler::narrow_i32_to_i16_round(clamp_sum_to_spine(s));
            }
        }
    }
}

/// Saturate one accumulator sample into the i32 spine range.
///
/// The sum accumulates in `i64` for headroom (see [`ProgramWidth`]); every
/// consumer of a WIDE sum — the ring payload and the i16 narrowing above — needs
/// it back inside i32 first, and this is the one place that clamp lives.
#[inline]
fn clamp_sum_to_spine(sum_sample: i64) -> i32 {
    sum_sample.clamp(i32::MIN as i64, i32::MAX as i64) as i32
}

/// Bytes one sample occupies on an S32LE ring wire.
pub(super) const WIDE_BYTES_PER_SAMPLE: usize = 4;

/// Fill an S32LE ring slot payload from the period's mix sum.
///
/// A wide wire implies a [`ProgramWidth::Wide`] sum (`Mixer::new` refuses to run
/// any other pairing), so the sum is ALREADY in the wire's own spine scale: the
/// only conversion left is the `i64`→`i32` saturation the accumulator's headroom
/// made necessary, plus the explicit little-endian byte order. Because the
/// promotion happens at each lane's sum entry, a lane with more than 16
/// significant bits puts them here intact, while a period built only from i16
/// lanes lands on exactly the bytes the narrow payload would carry —
/// `wide_payload_is_information_equivalent_to_the_narrow_payload` pins that.
///
/// `out` is the preallocated `ring_wide_payload` — `4 * sum.len()` bytes,
/// sized once at construction from the same `period_samples` that sizes
/// `sum_buf`, so this allocates nothing. `to_le_bytes` states the wire's
/// little-endianness rather than inheriting the host's, and the 4-byte
/// `copy_from_slice` cannot fail: `chunks_exact_mut(4)` yields exactly 4-byte
/// chunks and `to_le_bytes` returns exactly 4 bytes.
pub(super) fn fill_wide_ring_payload(sum: &[i64], out: &mut [u8]) {
    debug_assert_eq!(out.len(), sum.len() * WIDE_BYTES_PER_SAMPLE);
    for (chunk, &s) in out.chunks_exact_mut(WIDE_BYTES_PER_SAMPLE).zip(sum) {
        chunk.copy_from_slice(&clamp_sum_to_spine(s).to_le_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FULL_SCALE_WIDE: i64 = (32_767i64) << 16;

    #[test]
    fn mix_into_sums_two_inputs() {
        let mut sum = vec![0i64; 4];
        mix_into(&mut sum, &[100, 200, 300, 400], ProgramWidth::Narrow);
        mix_into(&mut sum, &[50, 50, 50, 50], ProgramWidth::Narrow);
        assert_eq!(sum, vec![150, 250, 350, 450]);
    }

    #[test]
    fn mix_into_saturates_at_i32_bounds_but_stays_room_for_i16_saturation() {
        // Two max-i16 inputs sum to 2 × 32767 = 65534 — well within i32.
        // Only saturate_to_i16 should clip; mix_into just accumulates.
        let mut sum = vec![0i64; 1];
        mix_into(&mut sum, &[i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut sum, &[i16::MAX], ProgramWidth::Narrow);
        assert_eq!(sum[0], 65534);
    }

    #[test]
    fn mix_into_cancels_positive_and_negative() {
        let mut sum = vec![0i64; 2];
        mix_into(&mut sum, &[5000, -3000], ProgramWidth::Narrow);
        mix_into(&mut sum, &[-5000, 3000], ProgramWidth::Narrow);
        assert_eq!(sum, vec![0, 0]);
    }

    #[test]
    fn apply_gain_to_sum_ducks_after_program_sum() {
        let mut sum = vec![20_000i64, -20_000, 1_500, -1_500];
        apply_gain_to_sum(&mut sum, 0.1, ProgramWidth::Narrow);
        assert_eq!(sum, vec![2_000, -2_000, 150, -150]);
    }

    #[test]
    fn duck_step_per_frame_matches_requested_time() {
        // 15 ms at 48 kHz = 720 frames for a full 0->1 traversal.
        let step = duck_step_per_frame(15, 48_000);
        assert!((step - 1.0 / 720.0).abs() < 1e-9);
        // A misconfigured 0 floors to 1 ms rather than dividing by zero.
        assert!(duck_step_per_frame(0, 48_000).is_finite());
        assert!(duck_step_per_frame(15, 0).is_finite());
    }

    #[test]
    fn ramp_program_duck_glides_it_does_not_step() {
        // A constant program signal, one period long. Ducking DOWN toward
        // 0.5 must NOT drop every frame to 0.5 at once: early frames stay near
        // full level and the level descends monotonically.
        let channels = 2usize;
        let frames = 64usize;
        let mut sum = vec![10_000i64; frames * channels];
        // attack_step chosen so it takes ~the whole period to reach target.
        let attack = (1.0 - 0.5) / (frames as f32);
        let current = ramp_program_duck(
            &mut sum,
            channels,
            1.0,
            0.5,
            attack,
            1.0,
            ProgramWidth::Narrow,
        );
        // First frame is essentially un-ducked (no instantaneous 25 dB drop).
        assert!(
            sum[0] > 9_800,
            "onset stepped instead of ramping: {}",
            sum[0]
        );
        // Level descends monotonically frame to frame.
        let frame_val = |f: usize| sum[f * channels];
        for f in 1..frames {
            assert!(
                frame_val(f) <= frame_val(f - 1),
                "duck ramp not monotonic at frame {f}"
            );
        }
        // Landed at (or approaching) the target by the end.
        assert!(
            (current - 0.5).abs() < 0.02,
            "did not reach target: {current}"
        );
        assert!(frame_val(frames - 1) < 6_000);
    }

    #[test]
    fn ramp_program_duck_release_returns_to_unity_and_stops_scaling() {
        // Releasing UP from a ducked 0.5 back to 1.0: once it lands on 1.0
        // it must stop scaling entirely (samples pass through unchanged).
        let channels = 2usize;
        let frames = 8usize;
        let mut sum = vec![10_000i64; frames * channels];
        // release_step large enough to reach 1.0 within the first frame.
        let current =
            ramp_program_duck(&mut sum, channels, 0.5, 1.0, 1.0, 1.0, ProgramWidth::Narrow);
        assert_eq!(current, 1.0);
        // The last frame, fully released, is unscaled.
        assert_eq!(sum[(frames - 1) * channels], 10_000);
    }

    #[test]
    fn ramp_program_duck_steady_state_is_flat_multiply_equivalent() {
        // When current already equals target, every frame scales by the same
        // constant — identical to apply_gain_to_sum (the step() steady-state
        // fast path uses apply_gain_to_sum; this guards their equivalence).
        let channels = 2usize;
        let mut ramped = vec![20_000i64, -20_000, 1_500, -1_500];
        let mut flat = ramped.clone();
        let current = ramp_program_duck(
            &mut ramped,
            channels,
            0.1,
            0.1,
            0.01,
            0.01,
            ProgramWidth::Narrow,
        );
        apply_gain_to_sum(&mut flat, 0.1, ProgramWidth::Narrow);
        assert_eq!(current, 0.1);
        assert_eq!(ramped, flat);
    }

    #[test]
    fn saturate_to_i16_clamps_positive_overflow() {
        let mut out = vec![0i16; 1];
        saturate_to_i16(&[100_000], &mut out, ProgramWidth::Narrow);
        assert_eq!(out[0], i16::MAX);
    }

    #[test]
    fn saturate_to_i16_clamps_negative_overflow() {
        let mut out = vec![0i16; 1];
        saturate_to_i16(&[-100_000], &mut out, ProgramWidth::Narrow);
        assert_eq!(out[0], i16::MIN);
    }

    #[test]
    fn saturate_to_i16_passes_in_range_values() {
        let mut out = vec![0i16; 4];
        saturate_to_i16(&[0, 1000, -1000, 32767], &mut out, ProgramWidth::Narrow);
        assert_eq!(out, vec![0, 1000, -1000, i16::MAX]);
    }

    #[test]
    fn mix_three_inputs_full_pipeline() {
        // Three inputs at ~1/3 max each: sum approaches max but
        // doesn't saturate. Models the realistic three-renderer
        // simultaneous-handover transient.
        let mut sum = vec![0i64; 4];
        mix_into(
            &mut sum,
            &[10_000, 10_000, 10_000, 10_000],
            ProgramWidth::Narrow,
        );
        mix_into(
            &mut sum,
            &[10_000, 10_000, 10_000, 10_000],
            ProgramWidth::Narrow,
        );
        mix_into(
            &mut sum,
            &[10_000, 10_000, 10_000, 10_000],
            ProgramWidth::Narrow,
        );
        let mut out = vec![0i16; 4];
        saturate_to_i16(&sum, &mut out, ProgramWidth::Narrow);
        assert_eq!(out, vec![30_000, 30_000, 30_000, 30_000]);
    }

    #[test]
    fn mix_three_max_inputs_saturates_output() {
        // Three max-positive inputs sum to 98_301, well above i16::MAX.
        // Saturation clips to 32767.
        let mut sum = vec![0i64; 2];
        mix_into(&mut sum, &[i16::MAX, i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut sum, &[i16::MAX, i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut sum, &[i16::MAX, i16::MAX], ProgramWidth::Narrow);
        let mut out = vec![0i16; 2];
        saturate_to_i16(&sum, &mut out, ProgramWidth::Narrow);
        assert_eq!(out, vec![i16::MAX, i16::MAX]);
    }

    /// The overflow pin: two full-scale lanes land at exactly twice one lane, in
    /// the right sign, with no wrap, at BOTH widths. An `i32` accumulator could
    /// not hold that — `65534 << 16` wraps the sign bit and turns the loudest
    /// possible program into non-monotonic fold-over — which is why the
    /// promotion happens at each lane's sum entry into an `i64`.
    #[test]
    fn two_full_scale_lanes_do_not_wrap_at_either_width() {
        // Narrow: unchanged — 65534, comfortably inside the accumulator.
        let mut narrow = vec![0i64; 1];
        mix_into(&mut narrow, &[i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut narrow, &[i16::MAX], ProgramWidth::Narrow);
        assert_eq!(narrow[0], 65_534);
        assert!(narrow[0] > 0, "a full-scale POSITIVE sum stays positive");

        // Wide: the same sum promoted, and crucially NOT the wrapped value an
        // i32 accumulator would have produced.
        let mut wide = vec![0i64; 1];
        mix_into(&mut wide, &[i16::MAX], ProgramWidth::Wide);
        mix_into(&mut wide, &[i16::MAX], ProgramWidth::Wide);
        assert_eq!(wide[0], 65_534i64 << 16);
        assert!(wide[0] > 0);
        assert_ne!(
            wide[0],
            (65_534i32).wrapping_shl(16) as i64,
            "the i32-wrap value must not be reachable"
        );

        // The negative twin.
        let mut wide_neg = vec![0i64; 1];
        mix_into(&mut wide_neg, &[i16::MIN], ProgramWidth::Wide);
        mix_into(&mut wide_neg, &[i16::MIN], ProgramWidth::Wide);
        assert_eq!(wide_neg[0], -65_536i64 << 16);
        assert!(wide_neg[0] < 0);
    }

    /// THE HEADROOM PROPERTY, and the reason the accumulator is `i64` rather
    /// than `i32` (see [`ProgramWidth`]).
    ///
    /// Two full-scale lanes legitimately exceed full scale, and the program duck
    /// can bring them back into range before the write. With an `i32`
    /// accumulator at spine scale the mix would saturate FIRST and the duck
    /// would then be attenuating an already-clipped value — audible distortion,
    /// not a rounding difference. Both widths must recover the same ducked
    /// level.
    #[test]
    fn the_sum_keeps_duck_recoverable_headroom_above_full_scale_at_both_widths() {
        let duck = 0.1f32;

        let mut narrow = vec![0i64; 1];
        mix_into(&mut narrow, &[i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut narrow, &[i16::MAX], ProgramWidth::Narrow);
        apply_gain_to_sum(&mut narrow, duck, ProgramWidth::Narrow);
        // 65534 * 0.1 — recovered cleanly, well inside i16.
        assert_eq!(narrow[0], 6_553);

        let mut wide = vec![0i64; 1];
        mix_into(&mut wide, &[i16::MAX], ProgramWidth::Wide);
        mix_into(&mut wide, &[i16::MAX], ProgramWidth::Wide);
        apply_gain_to_sum(&mut wide, duck, ProgramWidth::Wide);
        let mut out = vec![0i16; 1];
        saturate_to_i16(&wide, &mut out, ProgramWidth::Wide);
        assert_eq!(
            out[0], 6_553,
            "the wide path must recover the same ducked level, not a clipped one"
        );
        // Stated as the failure it guards: an accumulator that saturated at the
        // MIX would leave the duck attenuating i32::MAX instead.
        let clipped_first = ((i32::MAX as f32) * duck).round() as i64;
        assert_ne!(
            wide[0], clipped_first,
            "a saturating-at-the-mix accumulator would land here"
        );
    }

    /// The promotion is exact at both rails: `i16::MIN << 16` is precisely
    /// `i32::MIN` and `i16::MAX << 16` is `0x7FFF_0000`, so no rail overflows and
    /// none is silently rounded. Asserted through the sum entry that performs it.
    #[test]
    fn the_wide_sum_entry_left_justifies_the_whole_i16_range() {
        let lane = [0i16, i16::MAX, i16::MIN, 1, -1];
        let mut sum = vec![0i64; lane.len()];
        mix_into(&mut sum, &lane, ProgramWidth::Wide);
        assert_eq!(
            sum,
            vec![
                0,
                0x7FFF_0000i64,
                i32::MIN as i64,
                0x0001_0000,
                -0x0001_0000
            ]
        );
    }

    /// A known 24-bit sample in S24-in-S32 placement, and both 24-bit rails —
    /// the same vectors the lane-level fixture and the `jasper-resampler`
    /// contract test use, restated here because THIS is where the claim has to
    /// land: the summed write.
    const U2_HIRES_VECTORS: [i32; 3] = [0x1234_5600, 0x7fff_ff00, i32::MIN];

    /// THE EXIT-GATE FIXTURE: a hi-res sample injected where the DIRECT capture
    /// hands its period to the mixer survives — low bits and all — into the
    /// bytes published on a wide wire.
    ///
    /// Driven through the REAL sum entry (`mix_into_wide`) and the REAL payload
    /// fill, so it fails if either reintroduces a narrowing, a shift, or a clamp
    /// into the i16 range.
    ///
    /// The contrast is the point: the same sample taken through the NARROW sum
    /// entry loses its low byte, and the test asserts the exact number of bits
    /// each route keeps rather than only that they differ.
    #[test]
    fn a_hi_res_direct_lane_keeps_its_low_bits_all_the_way_to_the_wide_payload() {
        for pattern in U2_HIRES_VECTORS {
            // The wide route: the lane's spine-scale period enters the sum
            // untouched and is published as-is.
            let mut sum = vec![0i64; 4];
            mix_into_wide(&mut sum, &[pattern; 4], ProgramWidth::Wide);
            let mut payload = vec![0u8; sum.len() * WIDE_BYTES_PER_SAMPLE];
            fill_wide_ring_payload(&sum, &mut payload);
            for (i, chunk) in payload.chunks_exact(WIDE_BYTES_PER_SAMPLE).enumerate() {
                let bytes: [u8; WIDE_BYTES_PER_SAMPLE] = chunk.try_into().unwrap();
                assert_eq!(
                    i32::from_le_bytes(bytes),
                    pattern,
                    "published sample {i} must be {pattern:#010x} bit for bit",
                );
            }

            // The narrow route, for contrast: the capture narrowing runs first,
            // and everything below bit 16 is gone before the sum ever sees it.
            let narrowed = jasper_resampler::s32_high_word_to_s16(pattern);
            let mut narrow_sum = vec![0i64; 4];
            mix_into(&mut narrow_sum, &[narrowed; 4], ProgramWidth::Narrow);
            let mut narrow_out = vec![0i16; 4];
            saturate_to_i16(&narrow_sum, &mut narrow_out, ProgramWidth::Narrow);
            let survived_narrow = jasper_resampler::widen_i16_to_i32(narrow_out[0]);
            assert_eq!(
                pattern & !0xffff,
                survived_narrow,
                "the narrow route keeps exactly the high word and nothing below it",
            );
        }

        // Named explicitly on the one vector that HAS low bits, so this test
        // cannot pass on rails alone.
        let with_low_bits = U2_HIRES_VECTORS[0];
        assert_ne!(with_low_bits & 0xffff, 0, "the probe must carry low bits");
        let mut sum = vec![0i64; 1];
        mix_into_wide(&mut sum, &[with_low_bits], ProgramWidth::Wide);
        let mut payload = vec![0u8; WIDE_BYTES_PER_SAMPLE];
        fill_wide_ring_payload(&sum, &mut payload);
        let published = i32::from_le_bytes(payload[..].try_into().unwrap());
        assert_eq!(
            published & 0xffff,
            with_low_bits & 0xffff,
            "the low word must reach the wire, not just the high word"
        );
    }

    /// A hi-res lane MIXED WITH an ordinary S16 lane keeps both at the right
    /// level and keeps its own low bits — the promotion and the pass-through
    /// have to agree about the scale or one source is 96 dB off.
    #[test]
    fn a_wide_lane_and_an_s16_lane_sum_at_the_same_scale() {
        let hires = 0x0012_3456i32; // small, so the sum cannot saturate
        let s16 = 1_000i16;
        let mut sum = vec![0i64; 2];
        mix_into_wide(&mut sum, &[hires; 2], ProgramWidth::Wide);
        mix_into(&mut sum, &[s16; 2], ProgramWidth::Wide);
        let expected = (hires as i64) + (jasper_resampler::widen_i16_to_i32(s16) as i64);
        assert_eq!(sum, vec![expected; 2]);
        // The S16 lane still dominates by exactly the ratio of its level to the
        // hi-res sample's, i.e. the promotion did not scale it wrong.
        assert_eq!(
            jasper_resampler::widen_i16_to_i32(s16) as i64,
            (s16 as i64) << 16
        );
        // And the hi-res lane's low bits are still in the sum.
        assert_ne!(sum[0] & 0xffff, 0);
    }

    // ------------------------------------------------------------------
    // The `i32` rails and the `f32` mantissa on the `i64` accumulator.
    // ------------------------------------------------------------------

    /// THE NARROW DUCK'S BYTES, pinned as literals.
    ///
    /// The narrow arm of `apply_gain_to_sum` / `ramp_program_duck` must stay the
    /// same arithmetic down to the `f32` multiply and the (unreachable) `i32`
    /// clamp. These are the numbers a narrow box produces.
    #[test]
    fn the_narrow_duck_is_byte_identical_to_its_committed_golden() {
        let mut sum = vec![20_000i64, -20_000, 1_500, -1_500, 32_767, -32_768];
        apply_gain_to_sum(&mut sum, 0.1, ProgramWidth::Narrow);
        assert_eq!(sum, vec![2_000, -2_000, 150, -150, 3_277, -3_277]);

        let mut ramped = vec![20_000i64, -20_000, 1_500, -1_500, 32_767, -32_768];
        let current = ramp_program_duck(&mut ramped, 2, 0.1, 0.1, 0.01, 0.01, ProgramWidth::Narrow);
        assert_eq!(current, 0.1);
        assert_eq!(ramped, sum, "the ramp's steady state IS the flat multiply");

        // THE MANTISSA IS PART OF THOSE BYTES. The vectors above do not
        // distinguish an `f32` product from an `f64` one — mutation testing
        // showed an f64 narrow arm passing them — because the two agree on
        // very nearly every value. `50 * 0.01` is one of the few where they do
        // not: the f32 product rounds UP to exactly 0.5 and `round()` takes it
        // to 1, while f64 keeps 0.49999998... and rounds to 0. That one-step
        // difference is the shipped behaviour, quirk included.
        let mut edge = vec![50i64];
        apply_gain_to_sum(&mut edge, 0.01, ProgramWidth::Narrow);
        assert_eq!(edge[0], 1, "the shipped f32 product rounds up here");
        let via_f64 = (50.0_f64 * f64::from(0.01f32)).round() as i64;
        assert_eq!(via_f64, 0, "an f64 product would round down");
        assert_ne!(
            edge[0], via_f64,
            "the probe must distinguish f32 from f64, or this guards nothing",
        );
        let mut edge_ramped = vec![50i64];
        ramp_program_duck(
            &mut edge_ramped,
            1,
            0.01,
            0.01,
            0.001,
            0.001,
            ProgramWidth::Narrow,
        );
        assert_eq!(edge_ramped[0], 1, "the ramp keeps the same f32 product");
    }

    /// THE RAILS — the correctness half of n5.
    ///
    /// A spine-scale sum legitimately exceeds `i32::MAX`: that headroom above
    /// full scale is why the accumulator is `i64`, and the duck's job is to
    /// bring such a sum back into range. Clamping the ducked value to `i32`
    /// spent the headroom before anything downstream could use it. This drives
    /// `step()`'s real order — sum the lanes, duck the program, THEN add the
    /// assistant — because that is where the clamped and unclamped values stop
    /// agreeing at the speaker.
    #[test]
    fn the_wide_duck_keeps_the_i64_headroom_the_i32_rails_would_have_spent() {
        let duck = 0.5f32;
        // Three full-scale wide lanes: 6_442_254_336, three times over the
        // `i32` rail and entirely legitimate mid-chain.
        let mut sum = vec![0i64; 1];
        for _ in 0..3 {
            mix_into(&mut sum, &[i16::MAX], ProgramWidth::Wide);
        }
        assert_eq!(sum[0], 3 * FULL_SCALE_WIDE);
        assert!(
            sum[0] > i32::MAX as i64,
            "the probe must exceed the old rails, or this test guards nothing",
        );

        let ducked_probe = sum[0];
        apply_gain_to_sum(&mut sum, duck, ProgramWidth::Wide);
        assert_eq!(sum[0], 3_221_127_168, "the ducked sum keeps its headroom");

        // The clamped value is COMPUTED FROM THE CLAMPING EXPRESSION rather
        // than written down: `i32::MAX as f32` rounds UP to 2^31, so
        // `.clamp(_, i32::MAX as f32) as i64` lands on 2_147_483_648, not
        // `i32::MAX`.
        let spent_value = ((ducked_probe as f32) * duck)
            .round()
            .clamp(i32::MIN as f32, i32::MAX as f32) as i64;
        assert_eq!(
            spent_value,
            i32::MAX as i64 + 1,
            "the f32 upper rail is 2^31, one above i32::MAX",
        );
        assert_ne!(
            sum[0], spent_value,
            "the old i32 rails would have landed exactly here",
        );

        // Now the assistant enters, as it does in `step()`, pulling the sum
        // back into range. The clamped and unclamped paths differ by ~18 dB at
        // the speaker, not by a rounding step.
        let assistant = -2_000_000_000i64;
        let kept = vec![sum[0] + assistant];
        let spent = vec![spent_value + assistant];
        let mut kept_out = vec![0i16; 1];
        let mut spent_out = vec![0i16; 1];
        saturate_to_i16(&kept, &mut kept_out, ProgramWidth::Wide);
        saturate_to_i16(&spent, &mut spent_out, ProgramWidth::Wide);
        assert_eq!(kept_out[0], 18_633);
        // 2_250 on BOTH rail candidates (i32::MAX and the f32 clamp's 2^31):
        // the two differ by one spine LSB, far below one i16 step, so the
        // audible verdict is the same either way.
        assert_eq!(spent_out[0], 2_250);
        assert_eq!(
            {
                let alt = vec![(i32::MAX as i64) + assistant];
                let mut alt_out = vec![0i16; 1];
                saturate_to_i16(&alt, &mut alt_out, ProgramWidth::Wide);
                alt_out[0]
            },
            spent_out[0],
            "both old-rail candidates land on the same i16 code",
        );
        // ~18 dB, stated as a ratio rather than left for the reader to divide.
        assert!(
            (kept_out[0] as f64 / spent_out[0] as f64) > 8.0,
            "kept={} spent={}",
            kept_out[0],
            spent_out[0],
        );
    }

    /// The ramp is the same multiply with a per-frame gain, and `step()` reaches
    /// it on every duck transition.
    #[test]
    fn the_wide_ramp_keeps_the_same_headroom_as_the_flat_wide_multiply() {
        let mut ramped = vec![3 * FULL_SCALE_WIDE, 3 * FULL_SCALE_WIDE];
        let mut flat = ramped.clone();
        // current == target means every frame scales by the same constant.
        let current = ramp_program_duck(&mut ramped, 2, 0.5, 0.5, 0.01, 0.01, ProgramWidth::Wide);
        apply_gain_to_sum(&mut flat, 0.5, ProgramWidth::Wide);
        assert_eq!(current, 0.5);
        assert_eq!(ramped, flat);
        assert_eq!(ramped[0], 3_221_127_168);
        assert!(
            ramped[0] > i32::MAX as i64,
            "the ramp must not clamp to the i32 rails either",
        );
    }

    /// THE MANTISSA — the precision half of n5.
    ///
    /// `f32` carries 24 bits, so `sum as f32` at spine scale rounds the value
    /// before the multiply happens; near `2^31` the `f32` grid is 256 wide. The
    /// wide arm computes in `f64`, whose 53-bit mantissa holds every reachable
    /// sum exactly. This is a −144 dBFS-class correction, not a level fix —
    /// stated as what it is.
    #[test]
    fn the_wide_duck_multiplies_in_f64_because_f32_cannot_hold_a_spine_sum() {
        let probe = 2_147_483_000i64;
        let gain = 0.5f32;
        let mut wide = vec![probe];
        apply_gain_to_sum(&mut wide, gain, ProgramWidth::Wide);
        assert_eq!(wide[0], 1_073_741_500, "f64: the exact half of the probe");

        // What an f32 multiply would have produced, spelled out rather than
        // asserted by inequality alone, so the test names the value it rejects.
        let via_f32 = ((probe as f32) * gain).round() as i64;
        assert_eq!(via_f32, 1_073_741_504);
        assert_ne!(
            wide[0], via_f32,
            "the probe must distinguish f32 from f64, or this test guards nothing",
        );

        // And the narrow arm keeps f32 deliberately: a narrow sum is under
        // 2^24, where f32 is exact, and an f64 product can round differently in
        // the last place. Same value, both widths, when the sum is small.
        let small = 20_000i64;
        let mut narrow_small = vec![small];
        let mut wide_small = vec![small];
        apply_gain_to_sum(&mut narrow_small, 0.1, ProgramWidth::Narrow);
        apply_gain_to_sum(&mut wide_small, 0.1, ProgramWidth::Wide);
        assert_eq!(narrow_small, wide_small);
    }
}
