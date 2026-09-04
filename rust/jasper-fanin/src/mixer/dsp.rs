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
