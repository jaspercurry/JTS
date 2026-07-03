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
//! stateless contract reference that the cross-language C++ binding mirrors.
//!
//! # Provenance
//!
//! The interpolation math (the sinc/window coefficients, the table layout, the
//! per-frame interpolation, the `i16` rounding) is lifted verbatim from
//! `jasper-outputd`'s `content_bridge.rs`, which now *consumes* this crate
//! rather than carrying its own copy. Keeping the math here byte-for-byte is
//! load-bearing: the daemon path (Rust, via content_bridge) and the
//! Python/usbsink path (C++ pybind11) must produce bit-identical output, and a
//! cross-language contract test pins that to ≤1 LSB.
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
// Kernel math — lifted verbatim from content_bridge.rs so the daemon path and
// the C++ binding agree bit-for-bit. Do not "clean up" the f64 ops, the
// Blackman-Harris coefficients, the normalization, or the rounding: any change
// breaks cross-language byte-identity (tests/test_resampler_contract.py).
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

/// Narrow one S32_LE sample to S16 by keeping the high word — an arithmetic
/// right shift by 16, sign-preserving, no rounding, no dither.
///
/// This is the EXACT UAC2-gadget capture narrowing the JTS USB path uses. It
/// lives in this pure crate (rather than duplicated in the two ALSA daemons)
/// so that `jasper-usbsink-audio`'s bridge capture and `jasper-fanin`'s direct
/// capture share ONE definition — the conversion is bit-identical by
/// construction, not by a hand-synced test vector. The pinned sign-boundary
/// vector is asserted in this crate's tests AND re-asserted in both consuming
/// crates so a drift fails every suite.
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
/// Kept as a slice-map sibling of [`clamp_i16`] so the two ALSA daemons narrow
/// a captured period identically without either owning the primitive.
pub fn convert_s32_to_s16(input: &[i32], output: &mut [i16]) -> bool {
    if input.len() != output.len() {
        return false;
    }
    for (src, dst) in input.iter().zip(output.iter_mut()) {
        *dst = s32_high_word_to_s16(*src);
    }
    true
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

    /// Interpolate one channel of `ring` at fractional frame position `pos`.
    ///
    /// `pos` is an absolute frame index in the ring's monotonic frame space
    /// (the same space [`AudioRing::write_frame`] / [`AudioRing::read_frame`]
    /// report). Out-of-window taps read as zero (the ring returns 0 outside
    /// `[read_frame, write_frame)`), so the edges of a fresh stream ramp in.
    pub fn interpolate(&self, ring: &AudioRing, pos: f64, channel: usize) -> i16 {
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
        clamp_i16(acc)
    }
}

impl Default for SincTable {
    fn default() -> Self {
        Self::new()
    }
}

/// A fixed-capacity interleaved `i16` ring addressed by a monotonic frame
/// counter.
///
/// Lifted verbatim from `content_bridge.rs`: writes advance `write_frame`,
/// drops oldest-first on overflow (advancing `read_frame`), and
/// [`AudioRing::sample`] reads any frame in `[read_frame, write_frame)` (0
/// outside that window). The monotonic counters let a fractional read cursor
/// live in the *same* coordinate space as the writes, which is what makes the
/// streaming resampler phase-continuous across blocks.
#[derive(Debug, Clone)]
pub struct AudioRing {
    data: Vec<i16>,
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

    /// Push interleaved frames, dropping oldest-first on overflow. Returns the
    /// number of frames dropped (overrun).
    pub fn push_interleaved(&mut self, samples: &[i16]) -> u64 {
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

    /// Read one channel of one frame. Returns 0 for any frame outside the live
    /// window `[read_frame, write_frame)` (including negative indices), so a
    /// kernel reaching past the buffered edges reads silence there.
    pub fn sample(&self, frame: i64, channel: usize) -> i16 {
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
            self.ring.push_interleaved(input);
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
                out.push(self.table.interpolate(&self.ring, pos, channel));
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
/// edges, so this is the *contract reference* the C++/usbsink binding's
/// stateless `resample_block` mirrors — `tests/test_resampler_contract.py`
/// pins the two to ≤1 LSB. For streaming use, hold a [`BlockResampler`] instead
/// (this discards cross-call cursor continuity).
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

    /// The pinned S32→S16 sign-boundary vector (C2). This is the SINGLE
    /// definition of the UAC2 narrowing math; both `jasper-usbsink-audio`'s
    /// bridge capture and `jasper-fanin`'s direct capture consume
    /// [`s32_high_word_to_s16`] from this crate, and each re-asserts this exact
    /// vector in its own suite so a drift fails everywhere.
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
        ring.push_interleaved(&samples);
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
        assert_eq!(ring.sample(744, 0), 744, "oldest kept frame is index 744");
        assert_eq!(ring.sample(999, 0), 999, "newest frame preserved");
        // Dropped frames read as 0 (outside the live window).
        assert_eq!(ring.sample(743, 0), 0, "dropped frame is gone");
    }

    #[test]
    fn trim_to_is_noop_when_at_or_below_target() {
        let mut ring = AudioRing::new(1024, 2).unwrap();
        let block: Vec<i16> = (0..100).flat_map(|n| [n as i16, n as i16]).collect();
        ring.push_interleaved(&block); // 100 frames
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
        ring.push_interleaved(&signal);
        let dropped = ring.trim_to(512);
        assert_eq!(dropped, 4096 - 512);
        // Seat a cursor RADIUS_FRAMES into the retained window and interpolate:
        // must read real (non-zero-padded) audio, i.e. the window is live.
        let pos = ring.read_frame() as f64 + RADIUS_FRAMES as f64 + 1.0;
        let sample = table.interpolate(&ring, pos, 0);
        // Compare against the untrimmed reference at the same absolute frame:
        // trimming the oldest frames must not perturb the retained samples.
        let mut ref_ring = AudioRing::new(8192, 2).unwrap();
        ref_ring.push_interleaved(&signal);
        let ref_sample = table.interpolate(&ref_ring, pos, 0);
        assert_eq!(
            sample, ref_sample,
            "retained-window interpolation must match the untrimmed ring at the same frame"
        );
    }

    /// The committed cross-language golden fixture. A short deterministic stereo
    /// signal resampled one-shot at ratio 1.0001; the C++ binding must match
    /// this to ≤1 LSB. Printed (with `--nocapture`) so the fixture for
    /// `tests/test_resampler_contract.py` can be regenerated if the math is ever
    /// *intentionally* changed in lockstep on both sides.
    #[test]
    fn golden_vector_is_stable() {
        let table = SincTable::new();
        // The single canonical fixture input — shared with the `golden_vector`
        // example and the C++ contract test.
        let input = golden::canonical_input();
        let out = resample_i16(&input, golden::CHANNELS, 1.0001, &table);
        // The fixture is committed as the first/last few samples + length so a
        // silent math drift fails here AND in the cross-language test. These
        // values were produced by this exact code; regenerate deliberately.
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
    // `golden_vector_is_stable`. Cross-language contract: the C++ binding's
    // one-shot resample of the SAME input at the SAME ratio matches these to
    // ≤1 LSB. The Python contract test re-derives the full input and ratio and
    // compares element-by-element against the Rust output; these spot values
    // are the in-crate tripwire so Rust-side drift fails the Rust suite too.
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
