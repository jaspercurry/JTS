// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Nothing here touches ALSA or `Mixer` state, so every function is testable
//! from values alone.

/// Accumulate one lane's **spine-scale** period into the sum, with saturating
/// arithmetic. Nothing is shifted and nothing is narrowed, so the low word a
/// hi-res host sends survives from `readi` to the summed write. Pulled out for
/// unit testability — no ALSA needed.
pub(super) fn mix_into(sum: &mut [i64], input: &[i32]) {
    // PANIC-AUDITED: both buffers are the mixer's one fixed-size period allocation
    debug_assert_eq!(sum.len(), input.len());
    for (s, &i) in sum.iter_mut().zip(input) {
        *s = s.saturating_add(i as i64);
    }
}

/// Apply a period-stable gain to the accumulated program sum. Used
/// after pre-duck content metering so the assistant loudness baseline
/// tracks the listener-facing content, not the temporary ducked level.
///
/// **The rails.** No `i32` clamp: a spine-scale sum LEGITIMATELY exceeds
/// `i32::MAX` — that headroom above full scale is the reason the accumulator is
/// `i64` — and the duck's whole job is to pull such a sum back into range.
/// Clamping to `i32` first would spend the headroom before the duck could use
/// it, turning a recoverable over-full-scale sum into a clipped one. Saturation
/// is the consumer's job (`saturate_to_i16` / `clamp_sum_to_spine`), where it
/// can see the value that is actually leaving.
///
/// **The mantissa.** `f32` carries 24 bits, so `sum as f32` at spine scale
/// discards the bottom bits before the multiply happens — the same reason
/// `apply_gain` exists beside `apply_gain_i16`. This computes in `f64`, whose
/// 53-bit mantissa represents every `i32` (and every reachable sum) exactly.
///
/// Rust's float→int cast saturates, so no `i64` clamp is needed; a product that
/// overflowed would land on the rails rather than wrap.
pub(super) fn apply_gain_to_sum(sum: &mut [i64], gain: f32) {
    for sample in sum {
        *sample = ((*sample as f64) * f64::from(gain)).round() as i64;
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
/// The rails and the mantissa are exactly as [`apply_gain_to_sum`] documents —
/// this is the same multiply with a per-frame gain, and its steady state is
/// asserted equal to that function's.
pub(super) fn ramp_program_duck(
    sum: &mut [i64],
    channels: usize,
    mut current: f32,
    target: f32,
    attack_step: f32,
    release_step: f32,
) -> f32 {
    // PANIC-AUDITED: channels is the daemon's fixed channel-count constant at the only call site
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
            for s in &mut sum[base..base + channels] {
                *s = ((*s as f64) * f64::from(current)).round() as i64;
            }
        }
    }
    current
}

/// Narrow the sum to i16 for its one S16 consumer, the assistant content meter.
/// Pulled out for unit testability.
///
/// The 16 extra bits are shed with the shared
/// [`jasper_resampler::narrow_i32_to_i16_round`] — a round-to-nearest quantizer,
/// NOT a truncating shift. It inverts `widen_i16_to_i32` exactly, and a sum
/// carrying real low bits rounds rather than stepping half an LSB toward −∞ on
/// every sample.
pub(super) fn saturate_to_i16(sum: &[i64], out: &mut [i16]) {
    // PANIC-AUDITED: both buffers are the mixer's one fixed-size period allocation
    debug_assert_eq!(sum.len(), out.len());
    for (o, &s) in out.iter_mut().zip(sum) {
        *o = jasper_resampler::narrow_i32_to_i16_round(clamp_sum_to_spine(s));
    }
}

/// Saturate one accumulator sample into the i32 spine range.
///
/// The sum accumulates in `i64` for headroom; every consumer — the ring payload
/// and the i16 narrowing above — needs it back inside i32 first, and this is the
/// one place that clamp lives.
#[inline]
fn clamp_sum_to_spine(sum_sample: i64) -> i32 {
    sum_sample.clamp(i32::MIN as i64, i32::MAX as i64) as i32
}

/// Bytes one sample occupies on the S32LE ring wire.
pub(super) const BYTES_PER_SAMPLE: usize = 4;

/// Fill an S32LE ring slot payload from the period's mix sum.
///
/// The sum is ALREADY in the wire's own spine scale, so the only conversion left
/// is the `i64`→`i32` saturation the accumulator's headroom made necessary, plus
/// the explicit little-endian byte order. A lane with more than 16 significant
/// bits puts them here intact.
///
/// `out` is the preallocated `ring_payload` — `4 * sum.len()` bytes,
/// sized once at construction from the same `period_samples` that sizes
/// `sum_buf`, so this allocates nothing. `to_le_bytes` states the wire's
/// little-endianness rather than inheriting the host's, and the 4-byte
/// `copy_from_slice` cannot fail: `chunks_exact_mut(4)` yields exactly 4-byte
/// chunks and `to_le_bytes` returns exactly 4 bytes.
pub(super) fn fill_ring_payload(sum: &[i64], out: &mut [u8]) {
    // PANIC-AUDITED: both buffers are allocated once in Mixer::new from the same period_samples
    debug_assert_eq!(out.len(), sum.len() * BYTES_PER_SAMPLE);
    for (chunk, &s) in out.chunks_exact_mut(BYTES_PER_SAMPLE).zip(sum) {
        chunk.copy_from_slice(&clamp_sum_to_spine(s).to_le_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One full-scale lane at spine scale.
    const FULL_SCALE: i64 = i32::MAX as i64;

    #[test]
    fn mix_into_sums_two_inputs() {
        let mut sum = vec![0i64; 4];
        mix_into(&mut sum, &[100, 200, 300, 400]);
        mix_into(&mut sum, &[50, 50, 50, 50]);
        assert_eq!(sum, vec![150, 250, 350, 450]);
    }

    #[test]
    fn mix_into_cancels_positive_and_negative() {
        let mut sum = vec![0i64; 2];
        mix_into(&mut sum, &[5000, -3000]);
        mix_into(&mut sum, &[-5000, 3000]);
        assert_eq!(sum, vec![0, 0]);
    }

    /// The overflow pin: two full-scale lanes land at exactly twice one lane, in
    /// the right sign, with no wrap. An `i32` accumulator could not hold that —
    /// it wraps the sign bit and turns the loudest possible program into
    /// non-monotonic fold-over — which is why the sum accumulates in `i64`.
    #[test]
    fn two_full_scale_lanes_do_not_wrap() {
        let mut sum = vec![0i64; 1];
        mix_into(&mut sum, &[i32::MAX]);
        mix_into(&mut sum, &[i32::MAX]);
        assert_eq!(sum[0], 2 * FULL_SCALE);
        assert!(sum[0] > 0, "a full-scale POSITIVE sum stays positive");
        assert!(
            sum[0] > i32::MAX as i64,
            "the probe must exceed the i32 rails, or this guards nothing",
        );

        let mut neg = vec![0i64; 1];
        mix_into(&mut neg, &[i32::MIN]);
        mix_into(&mut neg, &[i32::MIN]);
        assert_eq!(neg[0], 2 * (i32::MIN as i64));
        assert!(neg[0] < 0);
    }

    #[test]
    fn apply_gain_to_sum_ducks_after_program_sum() {
        let mut sum = vec![20_000i64, -20_000, 1_500, -1_500];
        apply_gain_to_sum(&mut sum, 0.1);
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
        let current = ramp_program_duck(&mut sum, channels, 1.0, 0.5, attack, 1.0);
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
        let current = ramp_program_duck(&mut sum, channels, 0.5, 1.0, 1.0, 1.0);
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
        let current = ramp_program_duck(&mut ramped, channels, 0.1, 0.1, 0.01, 0.01);
        apply_gain_to_sum(&mut flat, 0.1);
        assert_eq!(current, 0.1);
        assert_eq!(ramped, flat);
    }

    /// The content meter's narrowing: the rails saturate, and an i16-grid value
    /// promoted into the sum comes back as itself (`narrow_i32_to_i16_round`
    /// inverts `widen_i16_to_i32` exactly).
    #[test]
    fn saturate_to_i16_saturates_the_rails_and_round_trips_the_grid() {
        let mut out = vec![0i16; 2];
        saturate_to_i16(&[4 * FULL_SCALE, -4 * FULL_SCALE], &mut out);
        assert_eq!(out, vec![i16::MAX, i16::MIN]);

        let grid = [0i16, 1_000, -1_000, i16::MAX, i16::MIN];
        let sum: Vec<i64> = grid
            .iter()
            .map(|&s| jasper_resampler::widen_i16_to_i32(s) as i64)
            .collect();
        let mut out = vec![0i16; grid.len()];
        saturate_to_i16(&sum, &mut out);
        assert_eq!(out, grid);
    }

    /// Three simultaneous full-scale lanes — the realistic three-renderer
    /// handover transient — accumulate without wrapping and clip only at the
    /// consumer.
    #[test]
    fn three_full_scale_lanes_saturate_at_the_consumer_not_the_mix() {
        let mut sum = vec![0i64; 2];
        for _ in 0..3 {
            mix_into(&mut sum, &[i32::MAX, i32::MIN]);
        }
        assert_eq!(sum, vec![3 * FULL_SCALE, 3 * (i32::MIN as i64)]);
        let mut out = vec![0i16; 2];
        saturate_to_i16(&sum, &mut out);
        assert_eq!(out, vec![i16::MAX, i16::MIN]);
    }

    /// A known 24-bit sample in S24-in-S32 placement, and both 24-bit rails —
    /// the same vectors the lane-level fixture and the `jasper-resampler`
    /// contract test use, restated here because THIS is where the claim has to
    /// land: the summed write.
    const U2_HIRES_VECTORS: [i32; 3] = [0x1234_5600, 0x7fff_ff00, i32::MIN];

    /// THE EXIT-GATE FIXTURE: a hi-res sample injected where the DIRECT capture
    /// hands its period to the mixer survives — low bits and all — into the
    /// bytes published on the wire.
    ///
    /// Driven through the REAL sum entry and the REAL payload fill, so it fails
    /// if either reintroduces a narrowing, a shift, or a clamp into the i16
    /// range.
    #[test]
    fn a_hi_res_direct_lane_keeps_its_low_bits_all_the_way_to_the_payload() {
        for pattern in U2_HIRES_VECTORS {
            let mut sum = vec![0i64; 4];
            mix_into(&mut sum, &[pattern; 4]);
            let mut payload = vec![0u8; sum.len() * BYTES_PER_SAMPLE];
            fill_ring_payload(&sum, &mut payload);
            for (i, chunk) in payload.chunks_exact(BYTES_PER_SAMPLE).enumerate() {
                let bytes: [u8; BYTES_PER_SAMPLE] = chunk.try_into().unwrap();
                assert_eq!(
                    i32::from_le_bytes(bytes),
                    pattern,
                    "published sample {i} must be {pattern:#010x} bit for bit",
                );
            }
        }

        // Named explicitly on the one vector that HAS low bits, so this test
        // cannot pass on rails alone.
        let with_low_bits = U2_HIRES_VECTORS[0];
        assert_ne!(with_low_bits & 0xffff, 0, "the probe must carry low bits");
        let mut sum = vec![0i64; 1];
        mix_into(&mut sum, &[with_low_bits]);
        let mut payload = vec![0u8; BYTES_PER_SAMPLE];
        fill_ring_payload(&sum, &mut payload);
        let published = i32::from_le_bytes(payload[..].try_into().unwrap());
        assert_eq!(
            published & 0xffff,
            with_low_bits & 0xffff,
            "the low word must reach the wire, not just the high word"
        );
    }

    /// A hi-res lane MIXED WITH an i16-grid source keeps both at the right
    /// level and keeps its own low bits — the promotion at the S16 source's
    /// entry and the lane pass-through have to agree about the scale or one
    /// source is 96 dB off.
    #[test]
    fn a_hi_res_lane_and_an_i16_grid_source_sum_at_the_same_scale() {
        let hires = 0x0012_3456i32; // small, so the sum cannot saturate
        let s16 = 1_000i16;
        let mut sum = vec![0i64; 2];
        mix_into(&mut sum, &[hires; 2]);
        mix_into(&mut sum, &[jasper_resampler::widen_i16_to_i32(s16); 2]);
        let expected = (hires as i64) + (jasper_resampler::widen_i16_to_i32(s16) as i64);
        assert_eq!(sum, vec![expected; 2]);
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

    /// THE RAILS.
    ///
    /// A spine-scale sum legitimately exceeds `i32::MAX`: that headroom above
    /// full scale is why the accumulator is `i64`, and the duck's job is to
    /// bring such a sum back into range. Clamping the ducked value to `i32`
    /// spent the headroom before anything downstream could use it. This drives
    /// `step()`'s real order — sum the lanes, duck the program, THEN add the
    /// assistant — because that is where the clamped and unclamped values stop
    /// agreeing at the speaker.
    #[test]
    fn the_duck_keeps_the_i64_headroom_the_i32_rails_would_have_spent() {
        let duck = 0.5f32;
        // Three full-scale lanes: three times over the `i32` rail and entirely
        // legitimate mid-chain.
        let mut sum = vec![0i64; 1];
        for _ in 0..3 {
            mix_into(&mut sum, &[jasper_resampler::widen_i16_to_i32(i16::MAX)]);
        }
        assert_eq!(sum[0], 3 * ((32_767i64) << 16));
        assert!(
            sum[0] > i32::MAX as i64,
            "the probe must exceed the old rails, or this test guards nothing",
        );

        let ducked_probe = sum[0];
        apply_gain_to_sum(&mut sum, duck);
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
        saturate_to_i16(&kept, &mut kept_out);
        saturate_to_i16(&spent, &mut spent_out);
        assert_eq!(kept_out[0], 18_633);
        // 2_250 on BOTH rail candidates (i32::MAX and the f32 clamp's 2^31):
        // the two differ by one spine LSB, far below one i16 step, so the
        // audible verdict is the same either way.
        assert_eq!(spent_out[0], 2_250);
        assert_eq!(
            {
                let alt = vec![(i32::MAX as i64) + assistant];
                let mut alt_out = vec![0i16; 1];
                saturate_to_i16(&alt, &mut alt_out);
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
    fn the_ramp_keeps_the_same_headroom_as_the_flat_multiply() {
        let full = (32_767i64) << 16;
        let mut ramped = vec![3 * full, 3 * full];
        let mut flat = ramped.clone();
        // current == target means every frame scales by the same constant.
        let current = ramp_program_duck(&mut ramped, 2, 0.5, 0.5, 0.01, 0.01);
        apply_gain_to_sum(&mut flat, 0.5);
        assert_eq!(current, 0.5);
        assert_eq!(ramped, flat);
        assert_eq!(ramped[0], 3_221_127_168);
        assert!(
            ramped[0] > i32::MAX as i64,
            "the ramp must not clamp to the i32 rails either",
        );
    }

    /// THE MANTISSA.
    ///
    /// `f32` carries 24 bits, so `sum as f32` at spine scale rounds the value
    /// before the multiply happens; near `2^31` the `f32` grid is 256 wide. The
    /// duck computes in `f64`, whose 53-bit mantissa holds every reachable sum
    /// exactly. This is a −144 dBFS-class correction, not a level fix — stated
    /// as what it is.
    #[test]
    fn the_duck_multiplies_in_f64_because_f32_cannot_hold_a_spine_sum() {
        let probe = 2_147_483_000i64;
        let gain = 0.5f32;
        let mut sum = vec![probe];
        apply_gain_to_sum(&mut sum, gain);
        assert_eq!(sum[0], 1_073_741_500, "f64: the exact half of the probe");

        // What an f32 multiply would have produced, spelled out rather than
        // asserted by inequality alone, so the test names the value it rejects.
        let via_f32 = ((probe as f32) * gain).round() as i64;
        assert_eq!(via_f32, 1_073_741_504);
        assert_ne!(
            sum[0], via_f32,
            "the probe must distinguish f32 from f64, or this test guards nothing",
        );
    }
}
