// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! A pure, arbitrary-ratio windowed-sinc resampler and the rate controller
//! that drives it.
//!
//! This crate is the ONE shared resampling algorithm in the JTS audio
//! daemons. It holds three composable pieces:
//!
//! - [`SincTable`] — a precomputed Blackman-Harris windowed-sinc interpolation
//!   table (2048 sub-sample phases × 33 taps). Built once and shared; never
//!   rebuilt per block (it is ~540 KB of `f64`).
//! - [`RateController`] — the shared [`jasper_clock::Dll`] (spa_dll loop) wired
//!   to a buffer-fill error and bounded by an output ppm clamp. It turns "the
//!   ring is `error_frames` away from its target" into a resampler ratio.
//! - [`BlockResampler`] — a streaming resampler that pushes interleaved input
//!   into an internal [`AudioRing`] and emits output frames by advancing a
//!   *fractional* read cursor (`next_input_frame += ratio` per output frame),
//!   so successive blocks are phase-continuous (no per-block click).
//!
//! [`resample_i16`] is a one-shot convenience over a fresh resampler — the
//! stateless reference pinned by the in-crate `golden_vector_is_stable` test.
//!
//! # Provenance
//!
//! The interpolation math (the sinc/window coefficients, the table layout, the
//! per-frame interpolation, the `i16` rounding) was originally lifted verbatim
//! from `jasper-outputd`'s `content_bridge.rs`. That module (the `rate_match`
//! content bridge) was deleted in the P5c cleanup; `jasper-fanin`'s
//! `lane_resampler.rs` is the crate's sole resampling-algorithm consumer now.
//! This is a Rust-only primitive — there
//! is no cross-language binding or contract test in this repo (a prior
//! C++/usbsink mirror + Python contract test were cut; see
//! docs/RESEARCH-pipewire-low-latency.md). Silent math drift is caught by the
//! in-crate `golden_vector_is_stable` regression test instead.
//!
//! # What this crate is NOT
//!
//! No I/O, no ALSA, no threads, no allocation on the hot path beyond the output
//! `Vec` the caller asked for. It is fed interleaved `i16` and a ratio and
//! returns interleaved `i16`; *where* the samples come from and *how* the ratio
//! is decided (a queue depth, an `snd_pcm_delay` reading) are the caller's
//! concern. Same doctrine as the sibling [`jasper_clock`] crate, so it compiles
//! and unit-tests on any host.
//!
//! # The capture-follower ratio convention
//!
//! Both the [`RateController`] sign and the [`BlockResampler`] cursor follow
//! PipeWire's *capture* direction, which content_bridge already proves:
//!
//! - The controller feeds the DLL the **negated** fill error (`fill - target`),
//!   so a too-full ring (`error_frames > 0`) settles to `ratio > 1`.
//! - The resampler advances its read cursor by `ratio` input frames per output
//!   frame, so `ratio > 1` consumes input **faster** and emits **fewer** output
//!   frames — draining the ring. This is mathematically PipeWire's capture
//!   `1.0 / corr`, located as the single inversion at the DLL's error input.
//!
//! ```
//! use jasper_resampler::{SincTable, resample_i16};
//!
//! // A stereo ramp resampled at unity is (after the cursor warms past the
//! // sinc radius) a faithful copy.
//! let table = SincTable::new();
//! let input: Vec<i16> = (0..2048).flat_map(|n| [n as i16, -(n as i16)]).collect();
//! let out = resample_i16(&input, 2, 1.0, &table);
//! assert!(!out.is_empty());
//! ```

#![forbid(unsafe_code)]

use jasper_clock::{Dll, DllConfig, DllSnapshot};

/// Half-width of the interpolation kernel, in input frames. The kernel spans
/// `[-RADIUS_FRAMES, +RADIUS_FRAMES]` around the fractional read position.
pub const RADIUS_FRAMES: i64 = 16;
/// Number of FIR taps per phase (`2 * RADIUS_FRAMES + 1`).
pub const TAPS: usize = (RADIUS_FRAMES as usize) * 2 + 1;
/// Number of precomputed sub-sample phases (interpolation resolution).
pub const PHASES: usize = 2048;

/// Minimum buffered frames to safely render one period at the worst-case
/// (max-ppm) ratio with kernel headroom, given a lane's period size and
/// max-adjust authority. This is the physical lock floor: a locked lane whose
/// cursor-relative fill drops below this cannot interpolate one more period
/// without reading past the newest written frame, so it must unlock into
/// silence. Any held-target setpoint at or below this value is churn-by-
/// construction (it sits on the underfill-unlock threshold). Pure — the single
/// source of truth for the formula, shared by the resampler's own underfill
/// gate and by config-time floor validation.
pub fn minimum_safe_fill_frames(period_frames: u32, max_adjust_ppm: f64) -> usize {
    let max_ratio = 1.0 + max_adjust_ppm / 1_000_000.0;
    (period_frames as f64 * max_ratio).ceil() as usize + RADIUS_FRAMES as usize + 1
}
/// Sinc cutoff as a fraction of Nyquist — slightly below 1.0 to tame the
/// passband edge of the windowed kernel.
const CUTOFF: f64 = 0.97;

// ---------------------------------------------------------------------------
// Kernel math — lifted verbatim from content_bridge.rs. Do not "clean up" the
// f64 ops, the Blackman-Harris coefficients, the normalization, or the
// rounding: any change is a silent output-drift risk, caught by the in-crate
// `golden_vector_is_stable` regression test.
// ---------------------------------------------------------------------------

fn sinc(x: f64) -> f64 {
    if x.abs() < 1.0e-8 {
        1.0
    } else {
        let pix = std::f64::consts::PI * x;
        pix.sin() / pix
    }
}

fn blackman_harris(x: f64) -> f64 {
    const A0: f64 = 0.35875;
    const A1: f64 = 0.48829;
    const A2: f64 = 0.14128;
    const A3: f64 = 0.01168;
    let phase = 2.0 * std::f64::consts::PI * x;
    A0 - A1 * phase.cos() + A2 * (2.0 * phase).cos() - A3 * (3.0 * phase).cos()
}

fn build_sinc_table() -> Vec<[f64; TAPS]> {
    let mut table = Vec::with_capacity(PHASES);
    for phase in 0..PHASES {
        let frac = phase as f64 / PHASES as f64;
        let mut coeffs = [0.0f64; TAPS];
        let mut norm = 0.0f64;
        for (tap, coeff) in coeffs.iter_mut().enumerate() {
            let offset = tap as i64 - RADIUS_FRAMES;
            let distance = frac - offset as f64;
            *coeff =
                sinc(distance * CUTOFF) * CUTOFF * blackman_harris(tap as f64 / (TAPS - 1) as f64);
            norm += *coeff;
        }
        if norm.abs() > 1.0e-9 {
            for coeff in &mut coeffs {
                *coeff /= norm;
            }
        }
        table.push(coeffs);
    }
    table
}

/// Round-to-nearest, saturating to the `i16` range — the exact rounding the
/// daemon path uses, so cross-language output matches at the LSB.
pub fn clamp_i16(value: f64) -> i16 {
    value.round().clamp(i16::MIN as f64, i16::MAX as f64) as i16
}

/// Round-to-nearest, saturating to the `i32` range — the spine-scale sibling of
/// [`clamp_i16`], and the ONLY rounding applied on the wide interpolation path.
///
/// Same `f64::round` (half away from zero) as [`clamp_i16`], so the two differ
/// only in where they saturate. `i32::MAX as f64` is exactly `2147483647.0` and
/// `i32::MIN as f64` exactly `-2147483648.0`, so the clamp rails are exact and
/// the `as i32` cast never sees an out-of-range value.
pub fn clamp_i32(value: f64) -> i32 {
    value.round().clamp(i32::MIN as f64, i32::MAX as f64) as i32
}

/// The exact scale factor between the i16 sample scale and the i32 spine scale:
/// `2^16`, the same factor [`widen_i16_to_i32`] applies as a shift.
///
/// Named once because the byte-identity of the narrow resample path depends on
/// it being an exact power of two: scaling every ring sample by `2^16` scales
/// the interpolator's `f64` accumulator by exactly `2^16` (a power-of-two
/// multiply changes only the exponent, never the mantissa or a rounding
/// decision, and the kernel's magnitudes are far from subnormal or overflow),
/// so dividing back out before the i16 round reproduces the pre-spine result
/// bit for bit. [`spine_acc_to_i16`] is that division.
pub const SPINE_SCALE_F64: f64 = 65_536.0;

/// Narrow a spine-scale interpolator accumulator to `i16` with the HISTORICAL
/// rounding — `clamp_i16(acc / 2^16)`.
///
/// The one place the narrow render path's rounding lives. [`SincTable::interpolate`]
/// returns its raw accumulator at the ring's own (i32 spine) scale; a narrow
/// consumer divides by [`SPINE_SCALE_F64`] and rounds ONCE here. Rounding at i32
/// first and narrowing afterwards would round twice and is not the same
/// function — do not compose [`clamp_i32`] with [`narrow_i32_to_i16_round`] to
/// get here.
#[inline]
pub fn spine_acc_to_i16(acc: f64) -> i16 {
    clamp_i16(acc / SPINE_SCALE_F64)
}

/// Narrow one S32_LE sample to S16 by keeping the high word — an arithmetic
/// right shift by 16, sign-preserving, no rounding, no dither.
///
/// This is the EXACT UAC2-gadget capture narrowing the JTS USB path uses. It
/// lives in this pure crate so jasper-fanin's DIRECT capture shares the same
/// tested conversion primitive as the resampler math, rather than embedding an
/// ALSA-local copy. The pinned sign-boundary vector is asserted here and in the
/// consuming fan-in crate.
///
/// Semantics (pinned): `(sample >> 16) as i16` — `>>` on `i32` is arithmetic,
/// so the sign extends and `i32::MIN` maps to `i16::MIN` (full-scale negative),
/// `-1` maps to `-1`, `0x7fff_ffff` maps to `0x7fff`. Truncation, not
/// rounding: `-65_537` (`0xFFFE_FFFF`) maps to `-2`.
#[inline]
pub fn s32_high_word_to_s16(sample: i32) -> i16 {
    (sample >> 16) as i16
}

/// Narrow a slice of interleaved S32_LE samples into an equal-length S16 slice
/// via [`s32_high_word_to_s16`]. `input` and `output` MUST be the same length
/// (the caller sizes both to the same sample count); mismatched lengths are a
/// programming error and return `false` without touching `output` past the
/// common prefix.
///
/// Returns `true` on success. Allocation-free — the caller owns both slices.
/// Kept as a slice-map sibling of [`clamp_i16`] so the ALSA owner does not also
/// own a second conversion implementation.
pub fn convert_s32_to_s16(input: &[i32], output: &mut [i16]) -> bool {
    if input.len() != output.len() {
        return false;
    }
    for (src, dst) in input.iter().zip(output.iter_mut()) {
        *dst = s32_high_word_to_s16(*src);
    }
    true
}

/// Widen one S16 sample to S32 by left-justifying it — `i32::from(s) << 16`.
///
/// The OUTPUT side's widening primitive, and the exact conversion ALSA's own
/// S16→S32 `plug` performs, which is why retiring that plug is bit-transparent.
/// It preserves sign and full scale (`i16::MIN` → `i32::MIN`, `i16::MAX` →
/// `0x7FFF_0000`), adds no gain, no dither, and no rounding, and is inverted
/// exactly by [`narrow_i32_to_i16_round`].
///
/// `i32::from` FIRST is load-bearing: shifting the i16 and widening afterwards
/// would shift every significant bit out of a 16-bit value.
///
/// Moved here from `jasper-outputd`'s `alsa_backend` (its sign-boundary and
/// scale-change vectors travelled with it) when outputd's program spine became
/// i32: the widening stopped being one sink's private staging step and became
/// the conversion at every S16 ingress into the wide spine — the content lane,
/// the SHM ring, the round-trip FIFO, and the TTS wire. One tested primitive for
/// all of them.
#[inline]
pub fn widen_i16_to_i32(sample: i16) -> i32 {
    i32::from(sample) << 16
}

/// Widen a slice of interleaved S16 samples into an equal-length S32 slice via
/// [`widen_i16_to_i32`].
///
/// `input` and `output` MUST be the same length. A mismatch is a programming
/// error and returns `false` **without touching `output` at all** — because the
/// alternative (widening the common prefix) would leave the tail STALE, which at
/// a speaker is a short or torn period that no counter can see. Both directions
/// are wrong: output shorter than input truncates the period, output longer
/// leaves a tail the period never produced.
///
/// Returns `true` on success. Allocation-free — the caller owns both slices.
/// `bool` rather than a `Result` because this crate has exactly one dependency
/// (`jasper-clock`) and no error type; the ALSA-side callers map `false` onto
/// their own `anyhow` idiom in ONE place per direction
/// (`jasper_outputd::types::widen_period`).
pub fn widen_i16_to_i32_slice(input: &[i16], output: &mut [i32]) -> bool {
    if input.len() != output.len() {
        return false;
    }
    for (src, dst) in input.iter().zip(output.iter_mut()) {
        *dst = widen_i16_to_i32(*src);
    }
    true
}

/// Narrow one S32 sample to S16 by **rounding to nearest** and saturating —
/// full-scale S32 maps onto full-scale S16.
///
/// **This is the speaker-edge quantizer. [`s32_high_word_to_s16`] is not.** That
/// sibling truncates by design because it implements UAC2-gadget *capture*
/// semantics (keep the high word, drop the rest); truncation is a half-LSB DC
/// step toward −∞ on every sample, and at a speaker edge under 18–21 dB of
/// digital attenuation that error is the granular decay-tail crackle this
/// primitive exists to remove. Do NOT reach for `s32_high_word_to_s16` on any
/// output path; the only callers of it are the USB capture path, and the only
/// i32→i16 conversions on the output side are this function and its slice
/// sibling.
///
/// Semantics (pinned, and the vectors below are the contract):
///
/// * Round-half-**up** (toward +∞), computed in i64 as `(s + 32768) >> 16` — an
///   arithmetic shift, so it floors, and adding half the step first makes that
///   floor a round-to-nearest. i64 because `i32::MAX + 32768` overflows i32.
/// * Saturating: `i32::MAX` rounds up past `i16::MAX` and is clamped to it.
///   `i32::MIN` maps to `i16::MIN` with no clamp needed.
/// * Zeros round to zeros — the reason no idle hiss appears at the S16 edge.
/// * It inverts [`widen_i16_to_i32`] EXACTLY: a widened sample is a multiple of
///   65536, never a half-step, so the round is a no-op and the round trip is
///   bit-identical. That identity is what makes an i32 spine byte-transparent
///   for S16 content into an S16 edge.
/// * Halves go toward +∞, which differs from [`clamp_i16`]'s `f64::round`
///   (half away from zero) at exactly `−0.5` steps: this returns `0` where
///   `clamp_i16` returns `−1`. Named rather than hidden — the integer form is
///   branchless and exact, the divergence is one LSB on inputs a widened S16
///   period cannot contain, and both behaviours are pinned by tests.
#[inline]
pub fn narrow_i32_to_i16_round(sample: i32) -> i16 {
    let rounded = ((sample as i64) + 32_768) >> 16;
    rounded.clamp(i16::MIN as i64, i16::MAX as i64) as i16
}

/// Narrow a slice of interleaved S32 samples into an equal-length S16 slice via
/// [`narrow_i32_to_i16_round`].
///
/// Same length contract, same all-or-nothing refusal, and the same `bool` return
/// as [`widen_i16_to_i32_slice`] — see that function for why.
pub fn narrow_i32_to_i16_round_slice(input: &[i32], output: &mut [i16]) -> bool {
    if input.len() != output.len() {
        return false;
    }
    for (src, dst) in input.iter().zip(output.iter_mut()) {
        *dst = narrow_i32_to_i16_round(*src);
    }
    true
}

/// The byte width of ONE S24_3LE sample on the wire: three packed bytes, no
/// padding byte.
///
/// The single source of truth for that stride. [`narrow_i32_to_i24_le_slice`]'s
/// length contract and its callers' staging sizing both read this instead of
/// restating a `3` — the number is the difference between S24_3LE (3 bytes) and
/// ALSA's other 24-bit spellings (`S24_LE`/`S24_BE`, which carry 24 bits inside
/// a 4-byte word), and a `3` open-coded in two places is a wrong period length
/// waiting to happen.
pub const I24_LE_BYTES_PER_SAMPLE: usize = 3;

/// The rails of a signed 24-bit sample: ±2^23. Rust has no `i24`, so unlike the
/// i16 narrowing's `i16::MIN`/`i16::MAX` these have to be named, and they are
/// named ONCE rather than spelled as literals at each use.
const I24_MIN: i64 = -8_388_608;
const I24_MAX: i64 = 8_388_607;

/// Narrow one S32 spine sample to **24 significant bits** by rounding to nearest
/// and saturating — full-scale S32 maps onto full-scale S24.
///
/// The 24-bit sibling of [`narrow_i32_to_i16_round`], and it is a QUANTIZER on
/// the same terms: it is the single conversion at an `S24_3LE` speaker edge, and
/// like its i16 sibling it must never be replaced by a truncating shift. At 24
/// bits the audible stakes are far lower than at 16 (the quantization floor sits
/// below any DAC's own analog noise), but truncation is a half-LSB DC step toward
/// −∞ on every sample whatever the width, and there is no reason to pay it.
/// [`s32_high_word_to_s16`] remains UAC2 *capture* semantics and belongs on no
/// output path at any width.
///
/// **The return is the 24-bit value SIGN-EXTENDED into an i32 — not a
/// left-justified one.** So the result always lies in `I24_MIN..=I24_MAX` and its
/// top 8 bits are pure sign, which is exactly what lets
/// [`narrow_i32_to_i24_le_slice`] drop the high byte losslessly. Do not feed this
/// output to anything expecting spine scale; it is 256× quieter.
///
/// Semantics (pinned, and the vectors in the tests are the contract):
///
/// * Round-half-**up** (toward +∞), computed in i64 as `(s + 128) >> 8` — an
///   arithmetic shift, so it floors, and adding half the step (2^7) first makes
///   that floor a round-to-nearest. i64 because `i32::MAX + 128` overflows i32.
///   Same shape as the i16 sibling with 8 bits dropped instead of 16.
/// * Saturating: `i32::MAX` rounds up past `I24_MAX` and is clamped to it.
///   `i32::MIN` maps to `I24_MIN` with no clamp needed.
/// * Zeros round to zeros — no idle hiss at this edge either.
/// * A spine sample that is an exact multiple of 256 (i.e. content that was
///   already 24-bit at spine scale) is carried through with no rounding at all,
///   which is the 24-bit analogue of the S16 round-trip identity.
#[inline]
pub fn narrow_i32_to_i24_round(sample: i32) -> i32 {
    let rounded = ((sample as i64) + 128) >> 8;
    rounded.clamp(I24_MIN, I24_MAX) as i32
}

/// Narrow a slice of interleaved S32 spine samples into **packed little-endian
/// 24-bit bytes** — ALSA's `S24_3LE` wire — via [`narrow_i32_to_i24_round`].
///
/// `output` MUST be exactly `input.len() * I24_LE_BYTES_PER_SAMPLE` bytes. A
/// mismatch is a programming error and returns `false` **without touching
/// `output` at all**, for the same reason the i16 slice pair does: at a speaker,
/// a partially-written period is a short or torn period that no counter can see.
///
/// The pack drops the high byte of each rounded value. That is lossless, not a
/// truncation: [`narrow_i32_to_i24_round`] clamps into the 24-bit range, so byte
/// 3 of the little-endian representation is pure sign extension of bit 23 — which
/// is precisely the invariant `to_le_bytes()[..3]` relies on, and it is pinned by
/// a test at both rails.
///
/// Returns `true` on success. Allocation-free — the caller owns both slices, and
/// on the output path that staging is sized once at open. `bool` rather than a
/// `Result` for the same reason as its siblings: this crate has one dependency
/// and no error type, and the ALSA-side caller maps `false` onto its own `anyhow`
/// idiom in ONE place (`jasper_outputd::types::narrow_period_i24_le`).
pub fn narrow_i32_to_i24_le_slice(input: &[i32], output: &mut [u8]) -> bool {
    if output.len() != input.len() * I24_LE_BYTES_PER_SAMPLE {
        return false;
    }
    for (src, dst) in input
        .iter()
        .zip(output.chunks_exact_mut(I24_LE_BYTES_PER_SAMPLE))
    {
        dst.copy_from_slice(
            &narrow_i32_to_i24_round(*src).to_le_bytes()[..I24_LE_BYTES_PER_SAMPLE],
        );
    }
    true
}

/// The dBFS floor an empty / digitally-silent i16 slice reports. Lives in this
/// pure crate so fan-in's telemetry and its tests share one sentinel.
pub const RMS_DBFS_FLOOR: f64 = -120.0;

/// Per-period RMS in dBFS of an interleaved i16 slice.
///
/// The ONE definition of the USB path's per-lane level metric. It lives in this
/// pure crate so fan-in's USB DIRECT lane and the mux's -60 dBFS activity gate
/// depend on one tested metric rather than a hand-synced copy.
///
/// Semantics (pinned): each sample is normalized by `/ 32768.0`, the mean square
/// is `sqrt`-ed, and an rms at or below a `1.0e-9` epsilon (empty or fully
/// silent) returns [`RMS_DBFS_FLOOR`]; otherwise `20 * log10(rms)` clamped up to
/// the floor. Allocation-free; no ALSA, so it unit-tests on any host.
pub fn rms_dbfs_i16(samples: &[i16]) -> f64 {
    if samples.is_empty() {
        return RMS_DBFS_FLOOR;
    }
    let sum_sq: f64 = samples
        .iter()
        .map(|sample| {
            let normalized = (*sample as f64) / 32768.0;
            normalized * normalized
        })
        .sum();
    let rms = (sum_sq / (samples.len() as f64)).sqrt();
    if rms <= 1.0e-9 {
        RMS_DBFS_FLOOR
    } else {
        (20.0 * rms.log10()).max(RMS_DBFS_FLOOR)
    }
}

/// Per-period RMS in dBFS of an interleaved **spine-scale i32** slice — the
/// wide sibling of [`rms_dbfs_i16`], for a lane whose samples are i32 rather
/// than i16.
///
/// Identical shape, identical epsilon, identical floor; only the normalizer
/// changes (`/ 2147483648.0`, i.e. `2^31`, where the narrow one uses `2^15`).
/// That makes the two report the SAME dBFS for the same acoustic signal, which
/// is what lets STATUS and mux's activity gate keep one meaning for `rms_dbfs`
/// no matter which width a lane carries.
pub fn rms_dbfs_i32(samples: &[i32]) -> f64 {
    if samples.is_empty() {
        return RMS_DBFS_FLOOR;
    }
    let sum_sq: f64 = samples
        .iter()
        .map(|sample| {
            let normalized = (*sample as f64) / 2_147_483_648.0;
            normalized * normalized
        })
        .sum();
    let rms = (sum_sq / (samples.len() as f64)).sqrt();
    if rms <= 1.0e-9 {
        RMS_DBFS_FLOOR
    } else {
        (20.0 * rms.log10()).max(RMS_DBFS_FLOOR)
    }
}

/// A precomputed windowed-sinc interpolation table.
///
/// Built ONCE (it is `PHASES * TAPS` `f64` ≈ 540 KB) and shared across every
/// resample call — both [`BlockResampler`] and `jasper-outputd`'s content
/// bridge hold one and pass it to [`SincTable::interpolate`]. Never rebuild it
/// per block.
#[derive(Debug, Clone)]
pub struct SincTable {
    phases: Vec<[f64; TAPS]>,
}

impl SincTable {
    /// Build the table (the only allocating/CPU-heavy step in the crate).
    pub fn new() -> Self {
        Self {
            phases: build_sinc_table(),
        }
    }

    /// Interpolate one channel of `ring` at fractional frame position `pos`,
    /// returning the RAW `f64` accumulator at the ring's own (i32 spine) scale.
    ///
    /// `pos` is an absolute frame index in the ring's monotonic frame space
    /// (the same space [`AudioRing::write_frame`] / [`AudioRing::read_frame`]
    /// report). Out-of-window taps read as zero (the ring returns 0 outside
    /// `[read_frame, write_frame)`), so the edges of a fresh stream ramp in.
    ///
    /// **The caller owns the rounding**, and which one it owns is the width
    /// decision: a narrow consumer calls [`spine_acc_to_i16`] (the historical
    /// `clamp_i16` of the pre-spine value), a wide one calls [`clamp_i32`].
    /// Returning `f64` rather than rounding here is what keeps the two paths a
    /// SINGLE rounding each — an `i32`-rounding interpolator would force a
    /// narrow consumer to round twice, which is a different function.
    pub fn interpolate(&self, ring: &AudioRing, pos: f64, channel: usize) -> f64 {
        let center = pos.floor() as i64;
        let frac = pos - center as f64;
        let phase = ((frac * PHASES as f64).floor() as usize).min(PHASES - 1);
        let coeffs = &self.phases[phase];
        let mut acc = 0.0f64;
        for (tap, coeff) in coeffs.iter().enumerate().take(TAPS) {
            let offset = tap as i64 - RADIUS_FRAMES;
            let frame = center + offset;
            acc += ring.sample(frame, channel) as f64 * coeff;
        }
        acc
    }
}

impl Default for SincTable {
    fn default() -> Self {
        Self::new()
    }
}

/// A fixed-capacity interleaved **i32 spine-scale** ring addressed by a
/// monotonic frame counter.
///
/// Lifted verbatim from `content_bridge.rs`: writes advance `write_frame`,
/// drops oldest-first on overflow (advancing `read_frame`), and
/// [`AudioRing::sample`] reads any frame in `[read_frame, write_frame)` (0
/// outside that window). The monotonic counters let a fractional read cursor
/// live in the *same* coordinate space as the writes, which is what makes the
/// streaming resampler phase-continuous across blocks.
///
/// # Why the storage is i32 even for an S16 lane
///
/// There is ONE ring type, not a narrow one and a wide one. An S16 producer
/// pushes through [`AudioRing::push_interleaved_narrow`], which widens with
/// [`widen_i16_to_i32`] (`<< 16`) on the way in; an S32 producer pushes its
/// samples unchanged. The interpolation kernel then runs at spine scale for
/// both, and the ONE narrowing a 16-bit consumer needs happens at its output
/// boundary ([`spine_acc_to_i16`]).
///
/// That is bit-transparent for the narrow path, not merely close: every ring
/// sample is scaled by the exact power of two [`SPINE_SCALE_F64`], so the
/// kernel's `f64` accumulator is scaled by exactly the same factor, and
/// dividing it back out before the i16 round reproduces the pre-spine result
/// sample for sample. Storing i16 and i32 in two ring types would instead have
/// meant two interpolators and two kernels to keep in step.
#[derive(Debug, Clone)]
pub struct AudioRing {
    data: Vec<i32>,
    channels: usize,
    capacity_frames: usize,
    read_frame: u64,
    write_frame: u64,
}

impl AudioRing {
    /// Allocate a ring holding `capacity_frames` interleaved frames of
    /// `channels`. Errors on a zero capacity or a sample-count overflow.
    pub fn new(capacity_frames: usize, channels: usize) -> Result<Self, RingError> {
        if capacity_frames == 0 {
            return Err(RingError::ZeroCapacity);
        }
        if channels == 0 {
            return Err(RingError::ZeroChannels);
        }
        let samples = capacity_frames
            .checked_mul(channels)
            .ok_or(RingError::CapacityOverflow)?;
        Ok(Self {
            data: vec![0; samples],
            channels,
            capacity_frames,
            read_frame: 0,
            write_frame: 0,
        })
    }

    /// Capacity in frames.
    pub fn capacity_frames(&self) -> usize {
        self.capacity_frames
    }

    /// Frames currently buffered (`write_frame - read_frame`).
    pub fn fill_frames(&self) -> usize {
        (self.write_frame - self.read_frame) as usize
    }

    /// The oldest frame index still readable.
    pub fn read_frame(&self) -> u64 {
        self.read_frame
    }

    /// One past the newest written frame index.
    pub fn write_frame(&self) -> u64 {
        self.write_frame
    }

    /// Push interleaved **spine-scale i32** frames, dropping oldest-first on
    /// overflow. Returns the number of frames dropped (overrun).
    pub fn push_interleaved(&mut self, samples: &[i32]) -> u64 {
        let frames = samples.len() / self.channels;
        let mut dropped = 0u64;
        for frame in 0..frames {
            if self.fill_frames() == self.capacity_frames {
                self.read_frame += 1;
                dropped += 1;
            }
            let dst = (self.write_frame as usize % self.capacity_frames) * self.channels;
            let src = frame * self.channels;
            self.data[dst..dst + self.channels].copy_from_slice(&samples[src..src + self.channels]);
            self.write_frame += 1;
        }
        dropped
    }

    /// Push interleaved **S16** frames, widening each with [`widen_i16_to_i32`]
    /// on the way in. Same oldest-first drop and same return as
    /// [`AudioRing::push_interleaved`].
    ///
    /// The widening is done here rather than in a caller-owned scratch buffer so
    /// a narrow producer needs no second allocation and no second copy — the
    /// hot path is one pass, exactly as it was when the ring stored i16.
    pub fn push_interleaved_narrow(&mut self, samples: &[i16]) -> u64 {
        let frames = samples.len() / self.channels;
        let mut dropped = 0u64;
        for frame in 0..frames {
            if self.fill_frames() == self.capacity_frames {
                self.read_frame += 1;
                dropped += 1;
            }
            let dst = (self.write_frame as usize % self.capacity_frames) * self.channels;
            let src = frame * self.channels;
            for channel in 0..self.channels {
                self.data[dst + channel] = widen_i16_to_i32(samples[src + channel]);
            }
            self.write_frame += 1;
        }
        dropped
    }

    /// Discard everything buffered (read catches up to write).
    pub fn clear(&mut self) {
        self.read_frame = self.write_frame;
    }

    /// Drop the OLDEST buffered frames so that at most `target_fill_frames`
    /// remain — a keep-NEWEST trim. Advances `read_frame` toward `write_frame`
    /// (never touches `write_frame`, so the newest audio is preserved) and
    /// returns the number of frames dropped.
    ///
    /// This is the standing-fill trim primitive: when a streaming consumer's
    /// buffer has accumulated more latency than its held target, this discards
    /// the excess oldest history in one step. It is a no-op (returns 0) when the
    /// ring already holds `<= target_fill_frames`.
    ///
    /// Unlike [`AudioRing::clear`], this keeps a live window: the caller's
    /// fractional read cursor, which lives in the same monotonic frame space,
    /// must be re-seated past the new `read_frame` by the caller (the ring
    /// cannot know the cursor). The single discontinuity is at the dropped
    /// boundary; the retained newest frames are untouched, so a cursor seated
    /// into them keeps its recent interpolation history.
    pub fn trim_to(&mut self, target_fill_frames: usize) -> u64 {
        let target = target_fill_frames as u64;
        let fill = self.write_frame - self.read_frame;
        if fill <= target {
            return 0;
        }
        let drop = fill - target;
        self.read_frame += drop;
        drop
    }

    /// Advance `read_frame` up to (but not past) `frame`, freeing history the
    /// cursor no longer needs. A non-positive or already-consumed `frame` is a
    /// no-op; it never advances past `write_frame`.
    pub fn drop_before(&mut self, frame: i64) {
        if frame <= 0 {
            return;
        }
        let frame = frame as u64;
        if frame > self.read_frame {
            self.read_frame = frame.min(self.write_frame);
        }
    }

    /// Read one channel of one frame, at spine scale. Returns 0 for any frame
    /// outside the live window `[read_frame, write_frame)` (including negative
    /// indices), so a kernel reaching past the buffered edges reads silence
    /// there.
    pub fn sample(&self, frame: i64, channel: usize) -> i32 {
        if frame < 0 {
            return 0;
        }
        let frame = frame as u64;
        if frame < self.read_frame || frame >= self.write_frame {
            return 0;
        }
        let idx = (frame as usize % self.capacity_frames) * self.channels + channel;
        self.data[idx]
    }
}

/// Construction error for [`AudioRing`] / [`BlockResampler`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RingError {
    /// `capacity_frames == 0`.
    ZeroCapacity,
    /// `channels == 0`.
    ZeroChannels,
    /// `capacity_frames * channels` overflowed `usize`.
    CapacityOverflow,
}

impl std::fmt::Display for RingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ZeroCapacity => write!(f, "audio ring capacity must be > 0"),
            Self::ZeroChannels => write!(f, "audio ring channel count must be > 0"),
            Self::CapacityOverflow => write!(f, "audio ring sample capacity overflow"),
        }
    }
}

impl std::error::Error for RingError {}

/// Drives a resampler ratio that holds a buffer at its target fill.
///
/// The loop math is the shared [`jasper_clock::Dll`] (the spa_dll second-order
/// DLL) — the same loop content_bridge used to embed directly; this is that
/// controller lifted into the shared crate so both content_bridge and the
/// usbsink path use one implementation. The DLL's third integrator gives zero
/// steady-state fill error; its `max_error` slew clamp and `max_resync`
/// hard-jump come for free.
///
/// Site-specific I/O stays at the call site: the *source* of `error_frames`
/// (a queue depth, a ring fill delta) and what the ratio *drives* are the
/// caller's concern. The caller's `max_adjust_ppm` safety bound on how far the
/// resampler may ever warp pitch is preserved as an OUTPUT clamp around the DLL
/// ratio, independent of loop state.
#[derive(Debug, Clone)]
pub struct RateController {
    dll: Dll,
    max_adjust_ppm: f64,
    anti_windup_threshold_frames: f64,
    ratio_ppm: f64,
    clamp_count: u64,
    anti_windup_count: u64,
}

impl RateController {
    /// Construct a controller whose loop timescale is `period_frames` at `rate`
    /// Hz and whose output ratio is clamped to `±max_adjust_ppm`.
    ///
    /// `rate` is explicit (not hardcoded) so each consumer passes its own
    /// nominal rate: content_bridge passes `outputd`'s 48000, the usbsink path
    /// passes its capture rate.
    pub fn new(max_adjust_ppm: f64, period_frames: u32, rate: u32) -> Self {
        Self::with_max_resync(max_adjust_ppm, period_frames, rate, None)
    }

    /// Construct a controller, optionally overriding the DLL hard-resync
    /// threshold. `None` keeps [`DllConfig::for_rate`]'s default; `Some(0.0)`
    /// disables hard resync so large but valid buffer-fill excursions slew back
    /// through the normal clamp instead of repeatedly resetting the loop.
    pub fn with_max_resync(
        max_adjust_ppm: f64,
        period_frames: u32,
        rate: u32,
        max_resync_frames: Option<f64>,
    ) -> Self {
        let mut config = DllConfig::for_rate(period_frames, rate);
        if let Some(max_resync) = max_resync_frames {
            config.max_resync = max_resync;
        }
        Self {
            dll: Dll::new(config),
            max_adjust_ppm,
            anti_windup_threshold_frames: (period_frames.max(1) as f64 / 2.0).max(1.0),
            ratio_ppm: 0.0,
            clamp_count: 0,
            anti_windup_count: 0,
        }
    }

    /// Re-initialise the loop (discard integrators, return to acquire
    /// bandwidth) while keeping the lifetime counters. Call on a hard
    /// discontinuity.
    pub fn reset(&mut self) {
        self.dll.reset();
        self.ratio_ppm = 0.0;
    }

    /// Feed one fill error and return the bounded resampler ratio.
    ///
    /// `error_frames = fill - target`. Negative feedback: a ring that is too
    /// full (`error_frames > 0`) must be drained by reading FASTER (ratio > 1).
    /// The DLL's `corr = 1 - (z2 + z3)` produces ratio > 1 for a NEGATIVE input
    /// error, so feed `-error_frames`. The result is then clamped to
    /// `±max_adjust_ppm` (counting when the clamp engages) so the resampler can
    /// never warp pitch past the safety bound regardless of loop state.
    pub fn next_ratio(&mut self, error_frames: f64) -> f64 {
        let mut raw_ppm = (self.dll.update(-error_frames) - 1.0) * 1_000_000.0;
        if self.is_wound_against_error(raw_ppm, error_frames) {
            // The output clamp is a safety bound, not an integrator bound. A
            // long excursion can leave the DLL hidden behind the clamp still
            // demanding drain after the buffer has crossed below target (or
            // the inverse). Reset to acquire bandwidth and re-apply the current
            // error so the first bounded output points back toward the target.
            self.dll.reset();
            self.anti_windup_count += 1;
            raw_ppm = (self.dll.update(-error_frames) - 1.0) * 1_000_000.0;
        }
        let clamped_ppm = raw_ppm.clamp(-self.max_adjust_ppm, self.max_adjust_ppm);
        if (raw_ppm - clamped_ppm).abs() > f64::EPSILON {
            self.clamp_count += 1;
        }
        self.ratio_ppm = clamped_ppm;
        1.0 + clamped_ppm / 1_000_000.0
    }

    fn is_wound_against_error(&self, raw_ppm: f64, error_frames: f64) -> bool {
        raw_ppm.is_finite()
            && error_frames.is_finite()
            && self.max_adjust_ppm.is_finite()
            && self.max_adjust_ppm > 0.0
            && raw_ppm.abs() > self.max_adjust_ppm
            && error_frames.abs() >= self.anti_windup_threshold_frames
            && raw_ppm.signum() != 0.0
            && error_frames.signum() != 0.0
            && raw_ppm.signum() != error_frames.signum()
    }

    /// The last bounded ratio in ppm (`(ratio - 1) * 1e6`).
    pub fn ratio_ppm(&self) -> f64 {
        self.ratio_ppm
    }

    /// Times the output ppm clamp engaged (the loop wanted to warp past the
    /// safety bound).
    pub fn clamp_count(&self) -> u64 {
        self.clamp_count
    }

    /// Times the controller reset a clamped DLL whose raw output was pushing
    /// away from the current fill error. Non-zero means the caller hit the
    /// safety clamp hard enough to require anti-windup.
    pub fn anti_windup_count(&self) -> u64 {
        self.anti_windup_count
    }

    /// The shared-DLL telemetry snapshot (the consistent `clock.rate_diff`
    /// shape every DLL site publishes on `/state` / doctor).
    pub fn dll_snapshot(&self) -> DllSnapshot {
        self.dll.snapshot()
    }

    /// Whether the underlying loop is currently locked.
    pub fn is_locked(&self) -> bool {
        self.dll.is_locked()
    }

    /// Times a `max_resync` hard-jump re-initialised the loop (a discontinuity,
    /// e.g. a host pause/seek that steps the fill).
    pub fn resync_count(&self) -> u64 {
        self.dll.resync_count()
    }
}

/// A streaming arbitrary-ratio resampler that keeps a fractional read cursor
/// across calls.
///
/// Push interleaved input via [`BlockResampler::resample_block`]; it buffers
/// into an internal [`AudioRing`] and emits whole output frames by advancing
/// `next_input_frame += ratio` per output frame and interpolating the ring at
/// that fractional position. Because the cursor persists between calls,
/// chopping a long signal into 10 ms blocks yields the same samples as one
/// shot — no per-block discontinuity (the streaming-cursor guarantee).
///
/// Capture-follower semantics: `ratio > 1` advances the cursor by more than one
/// input frame per output frame, so it consumes input FASTER and emits FEWER
/// output frames (draining the buffer); `ratio < 1` emits more. This matches
/// content_bridge's `next_input_frame += ratio` and PipeWire's capture
/// `1.0 / corr`.
#[derive(Debug, Clone)]
pub struct BlockResampler {
    ring: AudioRing,
    channels: usize,
    next_input_frame: f64,
    primed: bool,
    table: SincTable,
}

impl BlockResampler {
    /// Construct a resampler for `channels` interleaved channels with an
    /// internal ring of `ring_capacity_frames`. Builds its own [`SincTable`].
    pub fn new(channels: usize, ring_capacity_frames: usize) -> Result<Self, RingError> {
        if channels == 0 {
            return Err(RingError::ZeroChannels);
        }
        let ring = AudioRing::new(ring_capacity_frames, channels)?;
        Ok(Self {
            ring,
            channels,
            next_input_frame: 0.0,
            primed: false,
            table: SincTable::new(),
        })
    }

    /// Construct a resampler that shares a prebuilt [`SincTable`] (clones the
    /// table handle, so the heavy build happens once across many resamplers).
    pub fn with_table(
        channels: usize,
        ring_capacity_frames: usize,
        table: SincTable,
    ) -> Result<Self, RingError> {
        if channels == 0 {
            return Err(RingError::ZeroChannels);
        }
        let ring = AudioRing::new(ring_capacity_frames, channels)?;
        Ok(Self {
            ring,
            channels,
            next_input_frame: 0.0,
            primed: false,
            table,
        })
    }

    /// Push `input` (interleaved `i16`) and emit resampled interleaved output.
    ///
    /// The number of output frames is `floor(available_input_frames / ratio)`
    /// for whatever input frames are now available ahead of the cursor (a
    /// non-finite or non-positive `ratio` is treated as unity — the loop layer
    /// owns clamping, this is a last-ditch guard so the cursor never stalls or
    /// runs backwards). Consumed history is dropped, keeping
    /// `RADIUS_FRAMES + 1` frames behind the cursor so the kernel always has
    /// its left taps.
    pub fn resample_block(&mut self, input: &[i16], ratio: f64) -> Vec<i16> {
        let ratio = if ratio.is_finite() && ratio > 0.0 {
            ratio
        } else {
            1.0
        };
        if !input.is_empty() {
            self.ring.push_interleaved_narrow(input);
        }

        // On the first block, seat the cursor RADIUS_FRAMES into the buffered
        // input so the kernel has left-hand taps from frame 0 (otherwise the
        // first RADIUS_FRAMES outputs are computed against zero-padded history
        // and ramp in). This is the one-shot/streaming edge convention.
        if !self.primed {
            if self.ring.fill_frames() == 0 {
                return Vec::new();
            }
            self.next_input_frame = self.ring.read_frame() as f64 + RADIUS_FRAMES as f64;
            self.primed = true;
        }

        // Emit output frames while the kernel's rightmost tap (`floor(pos) +
        // RADIUS_FRAMES`) is still a written frame. The boundary
        // `pos + RADIUS_FRAMES + 1.0 <= write_frame` keeps that tap strictly
        // inside `[read_frame, write_frame)` (since `floor(pos) <= pos`), so no
        // output is computed against unwritten input; the cursor stops one step
        // short and the remaining input carries to the next call. This single
        // condition is the sole emit gate — when no frame fits, the loop simply
        // produces an empty Vec.
        let write_frame = self.ring.write_frame() as f64;
        let mut pos = self.next_input_frame;
        let mut out: Vec<i16> = Vec::new();
        while pos + RADIUS_FRAMES as f64 + 1.0 <= write_frame {
            for channel in 0..self.channels {
                // The ring is spine-scale, so the accumulator is too: narrow
                // back with the ONE historical rounding. Input widened by an
                // exact power of two and divided back out is bit-identical to
                // the pre-spine i16 ring — `golden_vector_is_stable` is the
                // empirical proof of that argument.
                out.push(spine_acc_to_i16(
                    self.table.interpolate(&self.ring, pos, channel),
                ));
            }
            pos += ratio;
        }
        self.next_input_frame = pos;

        // Free history the cursor has passed, keeping RADIUS_FRAMES + 1 behind.
        let keep_from = pos.floor() as i64 - RADIUS_FRAMES - 1;
        self.ring.drop_before(keep_from);
        out
    }

    /// Input frames buffered ahead of the read cursor (frames that could still
    /// contribute to future output). Zero before the first block primes.
    pub fn pending_input_frames(&self) -> usize {
        if !self.primed {
            return self.ring.fill_frames();
        }
        let ahead = self.ring.write_frame() as f64 - self.next_input_frame;
        ahead.max(0.0).floor() as usize
    }

    /// Discard all buffered input and re-prime on the next block (the
    /// hard-resync / discontinuity path: a fresh phase from the next input).
    pub fn reset(&mut self) {
        self.ring.clear();
        self.next_input_frame = 0.0;
        self.primed = false;
    }
}

/// One-shot stateless resample of an interleaved `i16` buffer at a fixed ratio.
///
/// A fresh [`BlockResampler`] is fed the whole buffer once with zero-padded
/// edges. This is the stateless reference pinned by the in-crate
/// `golden_vector_is_stable` regression test. For streaming use, hold a
/// [`BlockResampler`] instead (this discards cross-call cursor continuity).
///
/// The internal ring is sized to hold the whole input plus kernel headroom, so
/// nothing is dropped. Capture-follower semantics apply: `ratio > 1` returns
/// FEWER frames than the input, `ratio < 1` returns more.
pub fn resample_i16(input: &[i16], channels: usize, ratio: f64, table: &SincTable) -> Vec<i16> {
    if channels == 0 || input.is_empty() {
        return Vec::new();
    }
    let frames = input.len() / channels;
    // Ring must hold every input frame plus a little kernel headroom so the
    // one-shot never drops (a one-shot has no producer to drain it).
    let capacity = frames + TAPS + 1;
    let mut resampler = match BlockResampler::with_table(channels, capacity, table.clone()) {
        Ok(r) => r,
        Err(_) => return Vec::new(),
    };
    resampler.resample_block(input, ratio)
}

/// The cross-language contract fixture: one canonical deterministic input and
/// the ratios at which the Rust [`resample_i16`] output and the C++/usbsink
/// `RateResampler.resample_block` output must agree to ≤1 LSB.
///
/// This is the SINGLE definition of the fixture — the in-crate golden test, the
/// `golden_vector` example (which the Python contract test shells out to), and
/// the C++ side (which re-derives the same input) all reference it, so the three
/// can never silently drift apart. Doc-hidden: it is test/tooling surface, not
/// a runtime API.
#[doc(hidden)]
pub mod golden {
    use super::clamp_i16;

    /// The canonical 256-frame deterministic stereo input. Pure integer-seeded
    /// trig so it is bit-reproducible on any host. The C++ contract test
    /// generates the identical signal.
    pub fn canonical_input() -> Vec<i16> {
        (0..256)
            .flat_map(|n| {
                let t = n as f64;
                let l = clamp_i16(6000.0 * (t * 0.05).sin() + 1500.0 * (t * 0.21).sin());
                let r = clamp_i16(5000.0 * (t * 0.07).cos());
                [l, r]
            })
            .collect()
    }

    /// Channel count of the canonical input.
    pub const CHANNELS: usize = 2;

    /// The ratios the contract test pins. 1.0 is the pass-through; the small
    /// ±ppm offsets are the realistic capture-follower operating points.
    pub const RATIOS: [f64; 4] = [1.0, 1.0001, 0.9999, 1.0005];
}

#[cfg(test)]
mod tests {
    use super::*;

    const RATE: u32 = 48_000;
    const PERIOD: u32 = 480; // 10 ms at 48 kHz streaming-block example.

    /// Deterministic interleaved stereo test signal: two summed sines plus a
    /// slow linear sweep, distinct per channel. Bounded well inside i16 so the
    /// kernel never clamps (clamping would mask interpolation differences).
    fn stereo_signal(frames: usize) -> Vec<i16> {
        let mut out = Vec::with_capacity(frames * 2);
        for n in 0..frames {
            let t = n as f64;
            let l = 8000.0 * (t * 0.013).sin()
                + 4000.0 * (t * 0.071).sin()
                + 2000.0 * (t / frames.max(1) as f64);
            let r = 7000.0 * (t * 0.019).sin() + 3500.0 * (t * 0.043).cos();
            out.push(clamp_i16(l));
            out.push(clamp_i16(r));
        }
        out
    }

    fn frames_of(interleaved: &[i16], channels: usize) -> usize {
        interleaved.len() / channels
    }

    #[test]
    fn sinc_table_has_expected_shape() {
        let table = SincTable::new();
        assert_eq!(table.phases.len(), PHASES);
        assert_eq!(TAPS, 33);
        assert_eq!(RADIUS_FRAMES, 16);
        // Phase 0 (zero fractional offset) is a unit impulse at the center tap
        // after normalization: the center coefficient dominates and the row
        // sums to ~1.
        let row0 = &table.phases[0];
        let sum: f64 = row0.iter().sum();
        assert!(
            (sum - 1.0).abs() < 1e-9,
            "phase 0 must be normalized: {sum}"
        );
        // The center tap dominates phase 0 (a near-impulse). It sits at ~CUTOFF
        // (0.97) before normalization brings the row to sum 1; after
        // normalization it stays the dominant tap by a wide margin over its
        // neighbours.
        let center = row0[RADIUS_FRAMES as usize];
        let neighbour = row0[RADIUS_FRAMES as usize - 1].abs();
        assert!(
            center > 0.9,
            "phase 0 center tap should dominate, got {center}"
        );
        assert!(
            center > neighbour * 10.0,
            "phase 0 center tap must dwarf its neighbour: {center} vs {neighbour}"
        );
    }

    /// Ratio == 1.0 is a faithful pass-through: once the cursor has warmed past
    /// the kernel radius, the resampled signal reproduces the input to ≤1 LSB.
    #[test]
    fn unity_ratio_is_pass_through_within_one_lsb() {
        let table = SincTable::new();
        let input = stereo_signal(4096);
        let out = resample_i16(&input, 2, 1.0, &table);

        // The one-shot seats the cursor at RADIUS_FRAMES, so output frame k is
        // input frame k + RADIUS_FRAMES. Compare the overlapping region and
        // skip the final RADIUS frames where the right kernel tail runs past
        // the buffered input (those ramp down).
        let radius = RADIUS_FRAMES as usize;
        let out_frames = frames_of(&out, 2);
        let mut compared = 0usize;
        for k in 0..out_frames {
            let in_frame = k + radius;
            if in_frame + radius >= 4096 {
                break;
            }
            for ch in 0..2 {
                let got = out[k * 2 + ch] as i32;
                let want = input[in_frame * 2 + ch] as i32;
                assert!(
                    (got - want).abs() <= 1,
                    "unity pass-through off by >1 LSB at frame {k} ch {ch}: got {got} want {want}"
                );
            }
            compared += 1;
        }
        assert!(compared > 3000, "should have compared most frames");
    }

    /// The capture-follower frame-count law: ratio > 1 consumes input faster
    /// and emits FEWER output frames than input; ratio < 1 emits MORE.
    #[test]
    fn ratio_changes_output_frame_count_capture_follower() {
        let table = SincTable::new();
        let input = stereo_signal(8192);
        let in_frames = frames_of(&input, 2);

        let faster = resample_i16(&input, 2, 1.01, &table); // consume faster
        let slower = resample_i16(&input, 2, 0.99, &table); // consume slower
        let unity = resample_i16(&input, 2, 1.0, &table);

        let faster_frames = frames_of(&faster, 2);
        let slower_frames = frames_of(&slower, 2);
        let unity_frames = frames_of(&unity, 2);

        assert!(
            faster_frames < unity_frames,
            "ratio>1 must emit fewer frames: {faster_frames} !< {unity_frames}"
        );
        assert!(
            slower_frames > unity_frames,
            "ratio<1 must emit more frames: {slower_frames} !> {unity_frames}"
        );
        // The counts track ~ input/ratio (within the kernel-edge slack).
        let approx_faster = (in_frames as f64 / 1.01) as usize;
        assert!(
            faster_frames.abs_diff(approx_faster) < 2 * TAPS,
            "ratio>1 output ~ input/ratio: {faster_frames} vs ~{approx_faster}"
        );
    }

    /// The streaming-cursor guarantee: resampling a long signal in 10 ms blocks
    /// yields the SAME samples as one shot — no per-block discontinuity/click.
    #[test]
    fn block_streaming_matches_one_shot() {
        let table = SincTable::new();
        let input = stereo_signal(16_384);
        let ratio = 1.0001;

        let one_shot = resample_i16(&input, 2, ratio, &table);

        // Feed the same signal in 480-frame (10 ms) blocks through one
        // streaming resampler, accumulating output.
        let mut streamer = BlockResampler::with_table(2, 32_768, table.clone()).expect("streamer");
        let block = PERIOD as usize;
        let mut streamed: Vec<i16> = Vec::new();
        let total_frames = frames_of(&input, 2);
        let mut f = 0usize;
        while f < total_frames {
            let end = (f + block).min(total_frames);
            let chunk = &input[f * 2..end * 2];
            streamed.extend_from_slice(&streamer.resample_block(chunk, ratio));
            f = end;
        }

        // Both seat the cursor identically (RADIUS_FRAMES into frame 0 on the
        // first non-empty block), so output frame k is the same sample. They
        // may differ by a few frames at the very tail (the streamer can emit a
        // couple more once all input is present); compare the common prefix.
        let common = one_shot.len().min(streamed.len());
        assert!(common > 10_000, "should have a long common region");
        let mut max_diff = 0i32;
        for i in 0..common {
            max_diff = max_diff.max((one_shot[i] as i32 - streamed[i] as i32).abs());
        }
        assert!(
            max_diff <= 1,
            "block streaming must match one-shot within 1 LSB, max_diff={max_diff}"
        );
    }

    /// Block streaming with VARYING per-block ratios (what the live loop does)
    /// must still be phase-continuous: no clicks at block seams. We can't
    /// compare to a single one-shot ratio here, so assert the seam continuity
    /// directly — the sample-to-sample step across a block boundary is no larger
    /// than the steps just inside each block (a discontinuity would spike it).
    #[test]
    fn varying_ratio_blocks_have_no_seam_discontinuity() {
        let table = SincTable::new();
        let mut streamer = BlockResampler::with_table(2, 32_768, table.clone()).expect("streamer");
        let block = PERIOD as usize;
        // A continuous low-frequency tone makes seams obvious if they exist.
        let tone: Vec<i16> = (0..40_000)
            .flat_map(|n| {
                let v = clamp_i16(9000.0 * ((n as f64) * 0.01).sin());
                [v, v]
            })
            .collect();

        let ratios = [1.0, 1.0003, 0.9997, 1.0006, 0.9994, 1.0001];
        let mut streamed: Vec<i16> = Vec::new();
        let mut block_lengths: Vec<usize> = Vec::new();
        let total = frames_of(&tone, 2);
        let mut f = 0usize;
        let mut ri = 0usize;
        while f < total {
            let end = (f + block).min(total);
            let chunk = &tone[f * 2..end * 2];
            let produced = streamer.resample_block(chunk, ratios[ri % ratios.len()]);
            block_lengths.push(frames_of(&produced, 2));
            streamed.extend_from_slice(&produced);
            f = end;
            ri += 1;
        }

        // Walk the left channel; find the max |delta| INSIDE blocks vs the
        // |delta| exactly AT each block seam. A click at a seam would make the
        // seam delta an outlier.
        let left: Vec<i32> = streamed.iter().step_by(2).map(|&s| s as i32).collect();
        // Cumulative frame index of each seam.
        let mut seam_indices: Vec<usize> = Vec::new();
        let mut acc = 0usize;
        for (i, len) in block_lengths.iter().enumerate() {
            acc += len;
            if i + 1 < block_lengths.len() && acc > 0 && acc < left.len() {
                seam_indices.push(acc);
            }
        }
        let mut max_interior = 0i32;
        for w in left.windows(2) {
            max_interior = max_interior.max((w[1] - w[0]).abs());
        }
        let mut max_seam = 0i32;
        for &s in &seam_indices {
            if s < left.len() {
                max_seam = max_seam.max((left[s] - left[s - 1]).abs());
            }
        }
        // The seam step must be within the normal interior step range — no
        // click. (Equality is allowed; a discontinuity would make it much
        // larger.)
        assert!(
            max_seam <= max_interior,
            "block seams introduce a discontinuity: max_seam={max_seam} max_interior={max_interior}"
        );
    }

    /// `RateController` sign + convergence, exercised in the SAME closed-loop
    /// model jasper-clock's `tracks_a_constant_offset_without_standing_error`
    /// uses: a producer fills a ring at +ppm, the controller drives the
    /// consumer. At lock the ratio matches the producer's ppm (SAME sign) and
    /// the fill holds at target — the capture-follower sign gate.
    fn run_rate_loop(ctl: &mut RateController, ppm: f64, cycles: usize) -> (f64, f64) {
        const TARGET: f64 = 1920.0; // 40 ms at 48 kHz, the usbsink target.
        let period = PERIOD as f64;
        let producer_per_cycle = period * (1.0 + ppm / 1.0e6);
        let mut fill = TARGET;
        let mut ratio = 1.0_f64;
        for _ in 0..cycles {
            fill += producer_per_cycle - ratio * period;
            // error_frames = fill - target (the controller negates internally).
            ratio = ctl.next_ratio(fill - TARGET);
        }
        (ratio, fill - TARGET)
    }

    #[test]
    fn rate_controller_tracks_offset_with_capture_follower_sign() {
        for ppm in [-120.0, -50.0, 50.0, 120.0] {
            let mut ctl = RateController::new(500.0, PERIOD, RATE);
            let (_ratio, residual) = run_rate_loop(&mut ctl, ppm, 80_000);
            // Standing fill error is driven out (the z3 property).
            assert!(
                residual.abs() < 1.0,
                "standing fill error should vanish at {ppm} ppm, got {residual}"
            );
            // Output ratio runs the SAME direction as the producer offset: a
            // faster-filling ring needs a faster consumer to hold fill steady.
            assert!(
                (ctl.ratio_ppm() - ppm).abs() < 3.0,
                "ratio should track ~{ppm} ppm, got {} ppm",
                ctl.ratio_ppm()
            );
            assert!(ctl.is_locked(), "loop should lock at {ppm} ppm");
        }
    }

    /// A too-full ring (positive fill error) drives ratio > 1 (drain faster);
    /// a too-empty ring drives ratio < 1. The single-step sign, made explicit.
    #[test]
    fn rate_controller_single_step_sign_is_drain_on_overfill() {
        let mut ctl = RateController::new(500.0, PERIOD, RATE);
        // Warm a few cycles at zero error so the loop is past warmup.
        for _ in 0..200 {
            ctl.next_ratio(0.0);
        }
        // One positive fill error (ring too full) -> ratio should rise above
        // the prior (drain faster).
        let before = ctl.ratio_ppm();
        ctl.next_ratio(64.0);
        assert!(
            ctl.ratio_ppm() >= before,
            "overfill must not lower the consume rate: {} -> {}",
            before,
            ctl.ratio_ppm()
        );
    }

    /// The output ppm clamp engages and counts when the loop wants to warp
    /// past `max_adjust_ppm`. Drive a large sustained offset against a tight
    /// clamp; the reported ratio saturates at the bound and clamp_count climbs.
    #[test]
    fn rate_controller_output_clamp_bounds_ratio() {
        // Tiny 10 ppm clamp vs a big +400 ppm producer: the loop wants far more
        // than the clamp allows, so it saturates.
        let mut ctl = RateController::new(10.0, PERIOD, RATE);
        let _ = run_rate_loop(&mut ctl, 400.0, 5_000);
        assert!(
            ctl.ratio_ppm().abs() <= 10.0 + 1e-9,
            "ratio must respect the ±10 ppm clamp, got {}",
            ctl.ratio_ppm()
        );
        assert!(
            ctl.clamp_count() > 0,
            "the clamp should have engaged under a 400 ppm forcing"
        );
    }

    #[test]
    fn rate_controller_anti_windup_reverses_after_crossing_target() {
        // Hardware failure mode: a long overfill pins the bounded output at the
        // positive clamp. Without anti-windup the hidden DLL integrators can
        // keep demanding "drain faster" after the buffer has crossed below
        // target, walking the lane into underfill.
        let mut ctl = RateController::with_max_resync(10.0, PERIOD, RATE, Some(0.0));
        for _ in 0..5_000 {
            ctl.next_ratio(PERIOD as f64 * 4.0);
        }
        assert_eq!(ctl.ratio_ppm(), 10.0, "precondition: pinned high");
        let anti_windups = ctl.anti_windup_count();

        ctl.next_ratio(-(PERIOD as f64));

        assert!(
            ctl.ratio_ppm() < 0.0,
            "after crossing below target the bounded output must reverse, got {} ppm",
            ctl.ratio_ppm()
        );
        assert_eq!(
            ctl.anti_windup_count(),
            anti_windups + 1,
            "wrong-way saturated output must trigger anti-windup"
        );
    }

    /// A `max_resync`-sized step (a host pause/seek empties then refills the
    /// queue) hard-jumps the loop: resync_count climbs and the ratio returns to
    /// unity rather than slewing through the spike.
    #[test]
    fn rate_controller_hard_resyncs_on_a_step() {
        let mut ctl = RateController::new(500.0, PERIOD, RATE);
        // Establish a lock on a small offset.
        run_rate_loop(&mut ctl, 30.0, 60_000);
        assert!(ctl.is_locked(), "precondition: locked before the step");
        let resyncs_before = ctl.resync_count();

        // A discontinuity: an error far past max_resync (== one PERIOD here).
        // next_ratio negates the error internally; magnitude is what trips the
        // resync, so the sign does not matter.
        let ratio = ctl.next_ratio(50_000.0);
        assert_eq!(
            ctl.resync_count(),
            resyncs_before + 1,
            "a past-max_resync error must trigger one resync"
        );
        assert!((ratio - 1.0).abs() < 1e-12, "resync returns unity ratio");
        assert!(!ctl.is_locked(), "resync drops lock");
        // And it re-locks cleanly afterward.
        run_rate_loop(&mut ctl, 30.0, 60_000);
        assert!(ctl.is_locked(), "loop re-locks after a resync");
    }

    #[test]
    fn rate_controller_can_slew_large_fill_error_without_resync() {
        let mut ctl = RateController::with_max_resync(10.0, PERIOD, RATE, Some(0.0));
        let ratio = ctl.next_ratio(PERIOD as f64 * 4.0);
        assert_eq!(
            ctl.resync_count(),
            0,
            "large but valid buffer-fill errors should slew, not hard-resync"
        );
        assert!(
            ratio > 1.0,
            "positive fill error must consume input faster even past one period"
        );
        assert_eq!(ctl.ratio_ppm(), 10.0, "safety clamp still applies");
    }

    #[test]
    fn rate_controller_reset_returns_to_unity() {
        let mut ctl = RateController::new(500.0, PERIOD, RATE);
        run_rate_loop(&mut ctl, 80.0, 40_000);
        assert!(ctl.ratio_ppm().abs() > 1.0, "precondition: nonzero ratio");
        ctl.reset();
        assert_eq!(ctl.ratio_ppm(), 0.0, "reset zeroes the reported ppm");
    }

    /// BlockResampler resync re-primes the cursor: after a reset, the next block
    /// starts a fresh phase from the new input (no stale cursor / no panic).
    #[test]
    fn block_resampler_reset_reprimes() {
        let table = SincTable::new();
        let mut r = BlockResampler::with_table(2, 8192, table).expect("resampler");
        let input = stereo_signal(2048);
        let _ = r.resample_block(&input, 1.0);
        assert!(r.pending_input_frames() < 2048);
        r.reset();
        assert_eq!(r.pending_input_frames(), 0, "reset clears buffered input");
        // A fresh block after reset produces output again (re-primes cleanly).
        let out = r.resample_block(&stereo_signal(2048), 1.0);
        assert!(!out.is_empty(), "resampler re-primes and emits after reset");
    }

    /// resample_block never panics on degenerate ratios; a non-finite or
    /// non-positive ratio falls back to unity (defense in depth — the loop owns
    /// real clamping).
    #[test]
    fn degenerate_ratio_falls_back_to_unity() {
        let table = SincTable::new();
        let input = stereo_signal(2048);
        for bad in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            let mut r = BlockResampler::with_table(2, 8192, table.clone()).expect("resampler");
            let out = r.resample_block(&input, bad);
            // Unity-ish output frame count (within kernel slack), not empty.
            assert!(
                !out.is_empty(),
                "degenerate ratio {bad} should emit (unity)"
            );
        }
    }

    #[test]
    fn empty_input_and_zero_channels_are_safe() {
        let table = SincTable::new();
        assert!(resample_i16(&[], 2, 1.0, &table).is_empty());
        assert!(resample_i16(&[1, 2, 3, 4], 0, 1.0, &table).is_empty());
        let mut r = BlockResampler::with_table(2, 1024, table).expect("resampler");
        assert!(r.resample_block(&[], 1.0).is_empty());
    }

    #[test]
    fn audio_ring_rejects_zero_capacity_and_channels() {
        assert_eq!(AudioRing::new(0, 2).unwrap_err(), RingError::ZeroCapacity);
        assert_eq!(AudioRing::new(16, 0).unwrap_err(), RingError::ZeroChannels);
        assert!(AudioRing::new(16, 2).is_ok());
    }

    #[test]
    fn minimum_safe_fill_is_period_scaled_ratio_plus_kernel_headroom() {
        // = ceil(period × (1 + max_ppm/1e6)) + RADIUS_FRAMES + 1. This is the
        // physical underfill-unlock floor shared by the fan-in lane's render gate
        // and the cushion-decay floor validation, so pin the exact arithmetic.
        // period 256 / +500 ppm: ceil(256 * 1.0005) = ceil(256.128) = 257; + 16
        // radius + 1 = 274.
        assert_eq!(minimum_safe_fill_frames(256, 500.0), 274);
        // period 480 / +500 ppm: ceil(480 * 1.0005) = ceil(480.24) = 481; + 17 =
        // 498.
        assert_eq!(minimum_safe_fill_frames(480, 500.0), 498);
        // Zero max-ppm: unity ratio, so exactly period + radius + 1.
        assert_eq!(
            minimum_safe_fill_frames(256, 0.0),
            256 + RADIUS_FRAMES as usize + 1
        );
        // Monotone in both arguments.
        assert!(minimum_safe_fill_frames(256, 1000.0) >= minimum_safe_fill_frames(256, 500.0));
        assert!(minimum_safe_fill_frames(512, 500.0) > minimum_safe_fill_frames(256, 500.0));
    }

    #[test]
    fn clamp_i16_rounds_nearest_and_saturates_at_sample_bounds() {
        assert_eq!(clamp_i16(1.49), 1);
        assert_eq!(clamp_i16(1.5), 2);
        assert_eq!(clamp_i16(-1.5), -2);
        assert_eq!(clamp_i16(i16::MAX as f64), i16::MAX);
        assert_eq!(clamp_i16(i16::MAX as f64 + 0.5), i16::MAX);
        assert_eq!(clamp_i16(1.0e12), i16::MAX);
        assert_eq!(clamp_i16(i16::MIN as f64), i16::MIN);
        assert_eq!(clamp_i16(i16::MIN as f64 - 0.5), i16::MIN);
        assert_eq!(clamp_i16(-1.0e12), i16::MIN);
    }

    /// The pinned S32→S16 sign-boundary vector (C2). This is the SINGLE
    /// definition of the UAC2 narrowing math; jasper-fanin's direct capture
    /// consumes [`s32_high_word_to_s16`] and re-asserts this exact vector in its
    /// own suite.
    #[test]
    fn s32_high_word_truncation_preserves_sign_boundaries() {
        assert_eq!(s32_high_word_to_s16(0), 0);
        assert_eq!(s32_high_word_to_s16(0x7fff_ffff), 0x7fff);
        assert_eq!(s32_high_word_to_s16(i32::MIN), i16::MIN);
        assert_eq!(s32_high_word_to_s16(-1), -1);
        assert_eq!(s32_high_word_to_s16(-65_536), -1);
        assert_eq!(s32_high_word_to_s16(-65_537), -2);
    }

    #[test]
    fn convert_s32_to_s16_maps_each_sample_and_rejects_length_mismatch() {
        let input = [0i32, 0x7fff_ffff, i32::MIN, -1, -65_536, -65_537];
        let mut output = [7i16; 6];
        assert!(convert_s32_to_s16(&input, &mut output));
        assert_eq!(output, [0, 0x7fff, i16::MIN, -1, -1, -2]);
        // Length mismatch is a programming error: return false, don't panic.
        let mut short = [0i16; 3];
        assert!(!convert_s32_to_s16(&input, &mut short));
    }

    // ---- The widened DIRECT-lane path (U2 / #2223) ------------------------
    // The sign-boundary vectors above pin what the NARROW capture route does to
    // a gadget sample. These pin what the WIDE route does with the same values:
    // it carries them, unchanged, all the way into the interpolation ring.

    /// The U2 sign-boundary vectors, on the widened side. Every value the
    /// narrow route truncates through `s32_high_word_to_s16` reaches the ring
    /// BIT-EXACT on the wide route — including the S24-in-S32 placements the
    /// exit-gate fixture uses and both 24-bit sign rails.
    #[test]
    fn the_wide_capture_route_carries_every_sign_boundary_sample_unchanged() {
        // (raw gadget sample, what the NARROW route would keep)
        let vectors: [(i32, i16); 8] = [
            (0, 0),
            (0x7fff_ffff, 0x7fff),
            (i32::MIN, i16::MIN),
            (-1, -1),
            (-65_536, -1),
            (-65_537, -2),
            // 0x123456 in S24-in-S32 placement (24-bit value left-justified
            // into 32 bits): the low byte is exactly what the narrow route
            // discards.
            (0x1234_5600, 0x1234),
            // The positive 24-bit rail 0x7FFFFF, same placement.
            (0x7fff_ff00, 0x7fff),
        ];
        let mut ring = AudioRing::new(64, 1).unwrap();
        for (raw, narrow_keeps) in vectors {
            assert_eq!(
                s32_high_word_to_s16(raw),
                narrow_keeps,
                "narrow route vector drifted for {raw:#x}"
            );
            let before = ring.write_frame() as i64;
            ring.push_interleaved(&[raw]);
            assert_eq!(
                ring.sample(before, 0),
                raw,
                "wide route must carry {raw:#x} into the ring bit-exact"
            );
        }
    }

    /// The BIT-IDENTITY LEMMA the narrow resample path rests on: widening every
    /// ring sample by the exact power of two [`SPINE_SCALE_F64`] scales the
    /// interpolator's accumulator by exactly that factor, so dividing it back
    /// out and rounding reproduces the pre-spine `clamp_i16(acc)` sample for
    /// sample.
    ///
    /// Asserted directly on the arithmetic (`spine_acc_to_i16(acc * 2^16) ==
    /// clamp_i16(acc)`) across the interesting magnitudes, INCLUDING the
    /// half-step values where a rounding-mode difference would show, and both
    /// saturation rails. `golden_vector_is_stable` is the end-to-end half of
    /// the same claim.
    #[test]
    fn spine_narrowing_reproduces_the_pre_spine_i16_rounding_exactly() {
        let accs = [
            0.0, 0.5, -0.5, 1.5, -1.5, 0.4999999, -0.4999999, 123.456, -123.456, 32_766.5,
            -32_766.5, 32_767.0, -32_768.0,
            // Past both rails: the clamp must engage identically.
            40_000.0, -40_000.0, 1.0e12, -1.0e12,
        ];
        for acc in accs {
            assert_eq!(
                spine_acc_to_i16(acc * SPINE_SCALE_F64),
                clamp_i16(acc),
                "spine narrowing diverged from the pre-spine rounding at {acc}"
            );
        }
    }

    /// `push_interleaved_narrow` is exactly `widen_i16_to_i32` applied
    /// per-sample — the same conversion an S16 lane would have done in a
    /// caller-owned scratch, just without the scratch.
    #[test]
    fn the_narrow_push_widens_every_sample_by_the_shared_primitive() {
        let block: [i16; 6] = [0, 1, -1, i16::MAX, i16::MIN, -12_345];
        let mut ring = AudioRing::new(16, 2).unwrap();
        ring.push_interleaved_narrow(&block);
        assert_eq!(ring.fill_frames(), 3);
        for (frame, pair) in block.chunks_exact(2).enumerate() {
            for (channel, &sample) in pair.iter().enumerate() {
                assert_eq!(
                    ring.sample(frame as i64, channel),
                    widen_i16_to_i32(sample),
                    "frame {frame} channel {channel}"
                );
            }
        }
    }

    /// `clamp_i32` is `clamp_i16`'s rounding at the i32 rails: same
    /// half-away-from-zero `f64::round`, different saturation.
    #[test]
    fn clamp_i32_rounds_half_away_from_zero_and_saturates_at_the_i32_rails() {
        assert_eq!(clamp_i32(0.0), 0);
        assert_eq!(clamp_i32(0.5), 1);
        assert_eq!(clamp_i32(-0.5), -1);
        assert_eq!(clamp_i32(1.4), 1);
        assert_eq!(clamp_i32(-1.4), -1);
        assert_eq!(clamp_i32(i32::MAX as f64), i32::MAX);
        assert_eq!(clamp_i32(i32::MIN as f64), i32::MIN);
        assert_eq!(clamp_i32(1.0e12), i32::MAX);
        assert_eq!(clamp_i32(-1.0e12), i32::MIN);
    }

    /// The wide RMS reports the SAME dBFS as the narrow one for the same
    /// acoustic signal — the property that lets STATUS keep one meaning for
    /// `rms_dbfs` across lane widths.
    #[test]
    fn the_wide_rms_agrees_with_the_narrow_one_on_a_widened_signal() {
        let narrow: Vec<i16> = (0..512)
            .map(|n| clamp_i16(9000.0 * ((n as f64) * 0.031).sin()))
            .collect();
        let wide: Vec<i32> = narrow.iter().copied().map(widen_i16_to_i32).collect();
        let narrow_dbfs = rms_dbfs_i16(&narrow);
        let wide_dbfs = rms_dbfs_i32(&wide);
        assert!(
            (narrow_dbfs - wide_dbfs).abs() < 1.0e-9,
            "narrow {narrow_dbfs} vs wide {wide_dbfs}"
        );
        // Both ends of the scale, too.
        assert_eq!(rms_dbfs_i32(&[]), RMS_DBFS_FLOOR);
        assert_eq!(rms_dbfs_i32(&[0; 64]), RMS_DBFS_FLOOR);
        assert!((rms_dbfs_i32(&[i32::MIN; 64])).abs() < 1.0e-9, "full scale");
    }

    // ---- The output spine's width conversions ----------------------------
    // These four tests came over from jasper-outputd's `alsa_backend` with
    // `widen_i16_to_i32` and gained the narrowing direction. They are the
    // primitive-level half of the wide-path transparency argument; the
    // pipeline-level half lives in `jasper_outputd::core`.

    #[test]
    fn widening_preserves_sign_full_scale_and_silence() {
        // Left-justified widening: the same mapping ALSA's own S16->S32 plug
        // performs, which is why retiring that plug is bit-transparent.
        let samples = [i16::MAX, i16::MIN, 0, 1, -1, 256, -256];
        let mut out = vec![0i32; samples.len()];

        assert!(widen_i16_to_i32_slice(&samples, &mut out));

        assert_eq!(
            out,
            vec![
                0x7FFF_0000,
                i32::MIN,
                0,
                0x0001_0000,
                -0x0001_0000,
                0x0100_0000,
                -0x0100_0000,
            ]
        );
    }

    #[test]
    fn widening_the_i16_min_edge_neither_wraps_nor_saturates() {
        // `i16::MIN << 16` is the one input that could overflow if the shift
        // happened before the widen. Widened first it is exactly `i32::MIN` —
        // still full-scale negative, still monotonic against `i16::MIN + 1`.
        let mut out = [0i32; 2];
        assert!(widen_i16_to_i32_slice(&[i16::MIN, i16::MIN + 1], &mut out));

        assert_eq!(out[0], i32::MIN);
        assert_eq!(out[0], (i16::MIN as i32) * 65_536);
        assert!(out[0] < out[1], "widening must stay monotonic at the floor");
    }

    #[test]
    fn widening_is_exactly_a_scale_change_not_a_gain_change() {
        // Every sample must land on `value * 65536` — no dither, no rounding,
        // no headroom trim. A gain change here would be inaudible in a unit
        // test and very audible in a room.
        let samples: Vec<i16> = (-32_768..=32_767).step_by(97).map(|v| v as i16).collect();
        let mut out = vec![0i32; samples.len()];

        assert!(widen_i16_to_i32_slice(&samples, &mut out));

        for (i, &sample) in samples.iter().enumerate() {
            assert_eq!(out[i], i32::from(sample) * 65_536, "sample {sample}");
        }
    }

    #[test]
    fn widening_rejects_a_staging_length_that_does_not_match_the_period() {
        // Without the check the `zip` would quietly widen 2 of 4 samples and
        // leave the rest stale — a short/torn period at the speaker with
        // nothing in any counter to show for it. Both directions are wrong:
        // staging shorter than the period truncates it, staging longer writes
        // a tail the period never produced. And the refusal must leave the
        // output UNTOUCHED, not half-written.
        let mut short = [9i32; 2];
        assert!(!widen_i16_to_i32_slice(&[1, 2, 3, 4], &mut short));
        assert_eq!(short, [9, 9], "a refused widen must not write anything");

        let mut long = [9i32; 8];
        assert!(!widen_i16_to_i32_slice(&[1, 2], &mut long));
        assert_eq!(long, [9i32; 8], "a refused widen must not write anything");
    }

    #[test]
    fn narrowing_rounds_to_nearest_and_saturates_at_the_rails() {
        // The speaker-edge quantizer's pinned contract. Full scale both signs,
        // the i32 rail that has to clamp, and the half-step boundaries.
        assert_eq!(narrow_i32_to_i16_round(0), 0, "zeros round to zeros");
        assert_eq!(narrow_i32_to_i16_round(0x7FFF_0000), i16::MAX);
        assert_eq!(narrow_i32_to_i16_round(i32::MIN), i16::MIN);
        // `i32::MAX` is half an LSB ABOVE widened `i16::MAX`, so the round
        // pushes it past the rail and the clamp is what keeps it in range.
        // Without the clamp this wraps to `i16::MIN` — full-scale positive
        // becoming full-scale negative, the loudest possible defect.
        assert_eq!(narrow_i32_to_i16_round(i32::MAX), i16::MAX);
        // Just under / just over half a step: the rounding decision itself.
        assert_eq!(narrow_i32_to_i16_round(32_767), 0);
        assert_eq!(narrow_i32_to_i16_round(32_768), 1);
        assert_eq!(narrow_i32_to_i16_round(98_303), 1); // 1.49999 steps
        assert_eq!(narrow_i32_to_i16_round(98_304), 2); // exactly 1.5 steps
                                                        //
                                                        // Halves go toward +inf, so `-0.5` steps land on 0, NOT on -1. This is
                                                        // where the integer form diverges from `clamp_i16`'s f64::round.
        assert_eq!(narrow_i32_to_i16_round(-32_768), 0);
        assert_eq!(clamp_i16(-0.5), -1);
        assert_eq!(narrow_i32_to_i16_round(-32_769), -1);
    }

    #[test]
    fn narrowing_is_not_the_truncating_uac2_capture_conversion() {
        // The whole point of adding a second i32->i16 primitive. On a sample
        // half an LSB below zero the capture conversion steps DOWN and this one
        // rounds to silence; that per-sample downward bias, applied to a decay
        // tail, is the audible defect the wide path removes. If these two ever
        // agree on this vector, one of them has been changed into the other.
        assert_eq!(s32_high_word_to_s16(-65_537), -2);
        assert_eq!(narrow_i32_to_i16_round(-65_537), -1);
        assert_eq!(s32_high_word_to_s16(-1), -1);
        assert_eq!(narrow_i32_to_i16_round(-1), 0);
        assert_eq!(s32_high_word_to_s16(32_768), 0);
        assert_eq!(narrow_i32_to_i16_round(32_768), 1);
    }

    #[test]
    fn narrowing_rejects_a_staging_length_that_does_not_match_the_period() {
        let mut short = [9i16; 2];
        assert!(!narrow_i32_to_i16_round_slice(&[1, 2, 3, 4], &mut short));
        assert_eq!(short, [9, 9], "a refused narrow must not write anything");

        let mut long = [9i16; 8];
        assert!(!narrow_i32_to_i16_round_slice(&[1, 2], &mut long));
        assert_eq!(long, [9i16; 8], "a refused narrow must not write anything");
    }

    /// **The transparency proof, primitive level.** S16 in → widen → narrow is
    /// bit-identical, sample for sample, over full scale both signs, ±1 LSB, a
    /// ramp, and silence.
    ///
    /// This is what makes an i32 program spine safe to ship on the S16-edge
    /// fleet: every live box today reads S16 content and writes an S16 edge, so
    /// at unity gain the whole wide path collapses to exactly this round trip.
    /// It holds because a widened sample is a multiple of 65536 and therefore
    /// never sits on a half-step for the round to move.
    #[test]
    fn s16_widened_then_narrowed_is_bit_identical() {
        let mut fixture: Vec<i16> = vec![
            i16::MAX,
            i16::MIN,
            i16::MAX - 1,
            i16::MIN + 1,
            0,
            1,
            -1,
            32_766,
            -32_767,
        ];
        // A ramp across the whole range, plus a run of silence.
        fixture.extend((-32_768..=32_767).step_by(37).map(|v| v as i16));
        fixture.extend(std::iter::repeat_n(0i16, 64));

        let mut wide = vec![0i32; fixture.len()];
        assert!(widen_i16_to_i32_slice(&fixture, &mut wide));
        let mut back = vec![0i16; fixture.len()];
        assert!(narrow_i32_to_i16_round_slice(&wide, &mut back));

        assert_eq!(back, fixture, "the S16 round trip must be bit-identical");
        // And the intermediate really was wide — a round trip through a
        // no-op pair of functions would also pass the assertion above.
        for (i, &sample) in fixture.iter().enumerate() {
            assert_eq!(wide[i], i32::from(sample) * 65_536, "sample {i}");
        }
    }

    /// Exhaustive companion to the fixture above: EVERY i16 value round-trips.
    /// Cheap (65536 iterations) and it removes "the fixture missed a case" as a
    /// possible reading of the transparency claim.
    #[test]
    fn every_i16_value_survives_the_widen_narrow_round_trip() {
        for value in i16::MIN..=i16::MAX {
            let wide = widen_i16_to_i32(value);
            assert_eq!(
                narrow_i32_to_i16_round(wide),
                value,
                "round trip failed at {value}"
            );
        }
    }

    // ---- 24-bit narrowing + LE24 packing (the S24_3LE speaker edge) -------

    #[test]
    fn narrowing_to_24_bits_rounds_to_nearest_and_saturates_at_the_rails() {
        // The 24-bit edge quantizer's pinned contract, in the same shape as the
        // i16 sibling's: full scale both signs, the i32 rail that has to clamp,
        // and the half-step boundaries. The step here is 256, not 65536.
        assert_eq!(narrow_i32_to_i24_round(0), 0, "zeros round to zeros");
        assert_eq!(narrow_i32_to_i24_round(0x7FFF_FF00), 8_388_607);
        assert_eq!(narrow_i32_to_i24_round(i32::MIN), -8_388_608);
        // `i32::MAX` is half an LSB ABOVE the largest representable 24-bit
        // value, so the round pushes it past the rail and the clamp is what
        // keeps it in range. Without the clamp `(i32::MAX + 128) >> 8` is
        // 8_388_608 — one past the rail, whose low three bytes are 00 00 80,
        // i.e. full-scale NEGATIVE on the wire. Loudest possible defect.
        assert_eq!(narrow_i32_to_i24_round(i32::MAX), 8_388_607);
        // Just under / just over half a step: the rounding decision itself.
        assert_eq!(narrow_i32_to_i24_round(127), 0);
        assert_eq!(narrow_i32_to_i24_round(128), 1);
        assert_eq!(narrow_i32_to_i24_round(383), 1); // 1.49609 steps
        assert_eq!(narrow_i32_to_i24_round(384), 2); // exactly 1.5 steps
                                                     //
                                                     // Halves go toward +inf, so `-0.5` steps land on 0, NOT on -1 — the
                                                     // same documented divergence from `clamp_i16`'s f64::round the i16
                                                     // sibling carries.
        assert_eq!(narrow_i32_to_i24_round(-128), 0);
        assert_eq!(narrow_i32_to_i24_round(-129), -1);
    }

    #[test]
    fn narrowing_to_24_bits_is_not_a_truncating_shift() {
        // The 24-bit twin of `narrowing_is_not_the_truncating_uac2_capture_conversion`.
        // There is no shipped truncating i32->i24 to compare against, so the
        // contrast is MEASURED here against the shift a careless implementation
        // would use, rather than asserted from memory. If these ever agree on
        // these vectors, the rounding term has been dropped.
        //
        // Every vector here is a value where the two genuinely DIVERGE, which is
        // not every off-grid sample: `-129` rounds to `-1` and `-129 >> 8` is
        // also `-1`, so it proves nothing and is deliberately absent. (Found by
        // running this test, not by reading it.)
        for sample in [-1i32, -257, -384, 128, 255] {
            let truncated = sample >> 8;
            let rounded = narrow_i32_to_i24_round(sample);
            assert_ne!(
                rounded, truncated,
                "sample {sample}: rounding must differ from `>> 8`"
            );
        }
        assert_eq!(narrow_i32_to_i24_round(-1), 0);
        assert_eq!(-1i32 >> 8, -1);
        assert_eq!(narrow_i32_to_i24_round(-257), -1);
        assert_eq!(-257i32 >> 8, -2);
        assert_eq!(narrow_i32_to_i24_round(255), 1);
        assert_eq!(255i32 >> 8, 0);
    }

    #[test]
    fn a_spine_sample_already_at_24_bit_scale_survives_the_narrowing_exactly() {
        // The 24-bit analogue of the S16 round-trip identity: a multiple of 256
        // sits on no half-step, so the round is a no-op and the value is carried
        // through untouched. This is what makes a 24-bit edge lossless for
        // content that never had more than 24 bits.
        for value in [0i32, 256, -256, 65_536, -65_536, 0x7FFF_FF00u32 as i32] {
            assert_eq!(
                narrow_i32_to_i24_round(value),
                value >> 8,
                "multiple of 256 must not be rounded: {value}"
            );
        }
    }

    #[test]
    fn the_le24_pack_writes_three_little_endian_bytes_per_sample() {
        // The wire layout itself. ALSA's S24_3LE is three bytes, least
        // significant first, sign in bit 7 of the third — so a byte-order
        // mistake here is not a subtle quality change, it is garbage at the
        // speaker. Vectors cover both rails, ±1 LSB, and silence.
        let input = [
            0x7FFF_FF00u32 as i32, // -> +8_388_607 : FF FF 7F
            i32::MIN,              // -> -8_388_608 : 00 00 80
            0,                     // ->          0 : 00 00 00
            256,                   // ->         +1 : 01 00 00
            -256,                  // ->         -1 : FF FF FF
            0x0012_3400,           // ->    0x001234 : 34 12 00
        ];
        let mut packed = vec![0u8; input.len() * I24_LE_BYTES_PER_SAMPLE];
        assert!(narrow_i32_to_i24_le_slice(&input, &mut packed));
        assert_eq!(
            packed,
            vec![
                0xFF, 0xFF, 0x7F, //
                0x00, 0x00, 0x80, //
                0x00, 0x00, 0x00, //
                0x01, 0x00, 0x00, //
                0xFF, 0xFF, 0xFF, //
                0x34, 0x12, 0x00, //
            ]
        );
    }

    #[test]
    fn the_le24_pack_drops_only_sign_extension_at_both_rails() {
        // The load-bearing invariant behind `to_le_bytes()[..3]`: because the
        // narrowing CLAMPS into the 24-bit range, byte 3 is always pure sign
        // extension of bit 23, so dropping it loses nothing. Measured over both
        // rails and the i32 extremes that reach them.
        for sample in [i32::MIN, i32::MAX, 0x7FFF_FF00u32 as i32, 0, -1, 1] {
            let value = narrow_i32_to_i24_round(sample);
            let bytes = value.to_le_bytes();
            let expected_high = if value < 0 { 0xFF } else { 0x00 };
            assert_eq!(
                bytes[3], expected_high,
                "sample {sample} narrowed to {value}: byte 3 must be sign only"
            );
            // And the three kept bytes really do reconstruct the value.
            let round_trip = i32::from_le_bytes([bytes[0], bytes[1], bytes[2], expected_high]);
            assert_eq!(round_trip, value, "sample {sample}");
        }
    }

    #[test]
    fn the_le24_pack_rejects_a_staging_length_that_is_not_three_bytes_per_sample() {
        // Same all-or-nothing refusal as the i16 slice pair, and the mis-sizing
        // this catches is the specific one a 3-byte format invites: a staging
        // sized in SAMPLES (or at 4 bytes each, the S24_LE stride) rather than
        // at three bytes each.
        let input = [1i32, 2, 3, 4];

        let mut sample_sized = [9u8; 4];
        assert!(!narrow_i32_to_i24_le_slice(&input, &mut sample_sized));
        assert_eq!(sample_sized, [9u8; 4], "a refused pack writes nothing");

        let mut four_byte_stride = [9u8; 16];
        assert!(!narrow_i32_to_i24_le_slice(&input, &mut four_byte_stride));
        assert_eq!(four_byte_stride, [9u8; 16], "a refused pack writes nothing");

        let mut off_by_one = [9u8; 11];
        assert!(!narrow_i32_to_i24_le_slice(&input, &mut off_by_one));
        assert_eq!(off_by_one, [9u8; 11], "a refused pack writes nothing");

        // The exact size is accepted, so the refusals above are not vacuous.
        let mut exact = [9u8; 12];
        assert!(narrow_i32_to_i24_le_slice(&input, &mut exact));
        assert_ne!(exact, [9u8; 12]);
    }

    #[test]
    fn the_le24_byte_stride_is_three_not_four() {
        // The constant every staging size in the tree derives from. S24_3LE is
        // the packed spelling; ALSA's S24_LE carries the same 24 bits in FOUR
        // bytes, and a staging built at that stride would hand ALSA 4/3 of a
        // period.
        assert_eq!(I24_LE_BYTES_PER_SAMPLE, 3);
    }

    // ---- Per-lane RMS level (USB combo silence gate) ---------------------
    // The SINGLE definition of the USB path's dBFS level metric. jasper-fanin
    // consumes `rms_dbfs_i16` / `RMS_DBFS_FLOOR` from this crate; the mux's
    // activity gate consumes that telemetry. These pure-math vectors used to live in
    // jasper-fanin's mixer tests; they moved here with the helper.

    #[test]
    fn rms_dbfs_i16_silence_is_the_floor() {
        // Empty and all-zero slices both describe silence at the -120 floor —
        // the value a muxed-out / gadget-absent / digitally-silent lane reports.
        assert_eq!(rms_dbfs_i16(&[]), RMS_DBFS_FLOOR);
        assert_eq!(rms_dbfs_i16(&[0i16; 512]), RMS_DBFS_FLOOR);
    }

    #[test]
    fn rms_dbfs_i16_full_scale_is_zero_dbfs() {
        // A constant full-scale magnitude signal is 0 dBFS by definition
        // (rms == 32768/32768 == 1.0). Use -i16::MAX so |sample|/32768 == 1.0.
        let full = vec![-32768i16; 256];
        assert!(
            (rms_dbfs_i16(&full) - 0.0).abs() < 1e-6,
            "full-scale ⇒ 0 dBFS"
        );
    }

    #[test]
    fn rms_dbfs_i16_known_sine_matches_expected_dbfs() {
        // A full-scale sine has RMS = amplitude/√2 ⇒ -3.01 dBFS regardless of
        // frequency. Build one cycle at amplitude 32767 and assert the level.
        let n = 480usize; // whole number of samples over one cycle
        let sine: Vec<i16> = (0..n)
            .map(|i| {
                let phase = 2.0 * std::f64::consts::PI * (i as f64) / (n as f64);
                (32767.0 * phase.sin()).round() as i16
            })
            .collect();
        let dbfs = rms_dbfs_i16(&sine);
        assert!(
            (dbfs - (-3.01)).abs() < 0.1,
            "full-scale sine ⇒ ~-3.01 dBFS, got {dbfs}"
        );
    }

    #[test]
    fn rms_dbfs_i16_low_level_is_below_the_combo_gate() {
        // A -60 dBFS gate rejects a very quiet lane (a host emitting near-silence
        // / dither). A single-LSB square (±1) sits at ~-90 dBFS — well under any
        // reasonable playing threshold, so it never reads as "playing".
        let quiet: Vec<i16> = (0..512).map(|i| if i % 2 == 0 { 1 } else { -1 }).collect();
        assert!(
            rms_dbfs_i16(&quiet) < -60.0,
            "a ±1-LSB lane must sit under the -60 dBFS combo gate"
        );
    }

    /// Overflow drops the oldest complete frames, reports the exact overrun,
    /// and leaves the newest stereo window readable in channel order.
    #[test]
    fn audio_ring_overflow_drops_oldest_and_keeps_newest_frames() {
        let mut ring = AudioRing::new(3, 2).unwrap();
        let samples = [10, -10, 20, -20, 30, -30, 40, -40, 50, -50];

        let dropped = ring.push_interleaved(&samples);

        assert_eq!(dropped, 2);
        assert_eq!(ring.fill_frames(), 3);
        assert_eq!(ring.read_frame(), 2);
        assert_eq!(ring.write_frame(), 5);
        assert_eq!(ring.sample(1, 0), 0, "dropped history is unreadable");
        assert_eq!(ring.sample(2, 0), 30);
        assert_eq!(ring.sample(2, 1), -30);
        assert_eq!(ring.sample(3, 0), 40);
        assert_eq!(ring.sample(3, 1), -40);
        assert_eq!(ring.sample(4, 0), 50);
        assert_eq!(ring.sample(4, 1), -50);
    }

    /// `trim_to` drops the OLDEST frames down to the target fill and keeps the
    /// NEWEST — the standing-fill trim primitive. The retained window is the
    /// most-recently-written frames; the dropped count is `fill - target`.
    #[test]
    fn trim_to_keeps_newest_frames_down_to_target() {
        let mut ring = AudioRing::new(4096, 2).unwrap();
        // Write 1000 distinct frames: left channel = frame index, so we can
        // prove WHICH frames survive.
        let mut samples = Vec::with_capacity(2000);
        for n in 0..1000i16 {
            samples.push(n); // L = frame index
            samples.push(-n); // R
        }
        ring.push_interleaved_narrow(&samples);
        assert_eq!(ring.fill_frames(), 1000);
        let write_before = ring.write_frame();

        // Trim down to 256: drops the oldest 744.
        let dropped = ring.trim_to(256);
        assert_eq!(dropped, 744);
        assert_eq!(ring.fill_frames(), 256);
        // write_frame is untouched — the newest frame is preserved.
        assert_eq!(ring.write_frame(), write_before);
        // read_frame advanced to keep exactly the newest 256 frames: frames
        // [744, 1000). Sample the oldest surviving and newest frames by index.
        let oldest_kept = ring.read_frame();
        assert_eq!(oldest_kept, 744);
        // Spine scale: the ring widened each i16 on the way in, so a frame
        // whose L channel was written as `n` reads back as `widen_i16_to_i32(n)`.
        assert_eq!(
            ring.sample(744, 0),
            widen_i16_to_i32(744),
            "oldest kept frame is index 744"
        );
        assert_eq!(
            ring.sample(999, 0),
            widen_i16_to_i32(999),
            "newest frame preserved"
        );
        // Dropped frames read as 0 (outside the live window).
        assert_eq!(ring.sample(743, 0), 0, "dropped frame is gone");
    }

    #[test]
    fn trim_to_is_noop_when_at_or_below_target() {
        let mut ring = AudioRing::new(1024, 2).unwrap();
        let block: Vec<i16> = (0..100).flat_map(|n| [n as i16, n as i16]).collect();
        ring.push_interleaved_narrow(&block); // 100 frames
        assert_eq!(ring.fill_frames(), 100);
        // Target above current fill: nothing dropped.
        assert_eq!(ring.trim_to(256), 0);
        assert_eq!(ring.fill_frames(), 100);
        // Target exactly equal: still nothing dropped.
        assert_eq!(ring.trim_to(100), 0);
        assert_eq!(ring.fill_frames(), 100);
        // Target 0 drops everything.
        assert_eq!(ring.trim_to(0), 100);
        assert_eq!(ring.fill_frames(), 0);
    }

    /// After a trim, the streaming resampler's read cursor (which lives in the
    /// SAME monotonic frame space) can be re-seated past the new `read_frame`
    /// and interpolation still reads live samples — proving the retained window
    /// is intact and usable, not just accounted for.
    #[test]
    fn trim_to_leaves_a_usable_window_for_the_cursor() {
        let table = SincTable::new();
        let mut ring = AudioRing::new(8192, 2).unwrap();
        let signal = stereo_signal(4096);
        ring.push_interleaved_narrow(&signal);
        let dropped = ring.trim_to(512);
        assert_eq!(dropped, 4096 - 512);
        // Seat a cursor RADIUS_FRAMES into the retained window and interpolate:
        // must read real (non-zero-padded) audio, i.e. the window is live.
        let pos = ring.read_frame() as f64 + RADIUS_FRAMES as f64 + 1.0;
        let sample = table.interpolate(&ring, pos, 0);
        // Compare against the untrimmed reference at the same absolute frame:
        // trimming the oldest frames must not perturb the retained samples.
        let mut ref_ring = AudioRing::new(8192, 2).unwrap();
        ref_ring.push_interleaved_narrow(&signal);
        let ref_sample = table.interpolate(&ref_ring, pos, 0);
        assert_eq!(
            sample, ref_sample,
            "retained-window interpolation must match the untrimmed ring at the same frame"
        );
    }

    /// The committed golden fixture. A short deterministic stereo signal
    /// resampled one-shot at ratio 1.0001, pinned as a regression tripwire
    /// against silent math drift. Printed (with `--nocapture`) so the fixture
    /// can be regenerated via `cargo run --example golden_vector` if the math
    /// is ever *intentionally* changed.
    #[test]
    fn golden_vector_is_stable() {
        let table = SincTable::new();
        // The single canonical fixture input — shared with the `golden_vector`
        // example.
        let input = golden::canonical_input();
        let out = resample_i16(&input, golden::CHANNELS, 1.0001, &table);
        // The fixture is committed as the first/last few samples + length so a
        // silent math drift fails here. These values were produced by this
        // exact code; regenerate deliberately.
        assert_eq!(out.len(), GOLDEN_1_0001_LEN, "golden length drift");
        for (i, &(idx, l, r)) in GOLDEN_1_0001_SPOT.iter().enumerate() {
            let got_l = out[idx * 2];
            let got_r = out[idx * 2 + 1];
            assert!(
                (got_l as i32 - l as i32).abs() <= 1 && (got_r as i32 - r as i32).abs() <= 1,
                "golden spot {i} (frame {idx}) drift: got ({got_l},{got_r}) want ({l},{r})"
            );
        }
    }

    // Golden fixture for ratio 1.0001 over the 256-frame deterministic input in
    // `golden_vector_is_stable`. These spot values are the in-crate tripwire
    // for silent output drift in this crate's resampler math.
    // 223 output frames × 2 channels interleaved = 446 i16 samples (256 input
    // frames, cursor seated at RADIUS_FRAMES, ratio 1.0001).
    const GOLDEN_1_0001_LEN: usize = 446;
    // (output frame index, left, right) at a few stable positions past the
    // cursor warm-up. Values are produced by this crate's math (regenerate via
    // `cargo run --example golden_vector` if the math is intentionally changed
    // on BOTH languages in lockstep).
    const GOLDEN_1_0001_SPOT: [(usize, i16, i16); 4] = [
        (32, 3138, -4881),
        (64, -5874, 3879),
        (128, 3381, -3962),
        (200, -4413, -4164),
    ];
}
