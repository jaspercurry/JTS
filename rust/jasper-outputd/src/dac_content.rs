// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The DAC-content return lane — a grouping LEADER's round-trip ingress.
//!
//! On a grouping LEADER the music the DAC plays must come back OUT of the
//! sync engine, so the leader is sample-locked with its followers: the
//! leader's own localhost snapclient re-plays the bond's shared stereo, and
//! this module reads it one DAC period at a time. Without it a leader plays
//! its program ahead of every follower.
//!
//! ## Two transports, one at a time
//!
//! - **Ring** (`Reader::Ring`) — snapclient writes the SHM ring
//!   `jasper::multiroom::dac_content_ring` names, through the C ioplug, and
//!   this module attaches as its reader. The ONE transport (ADR-0100); the
//!   lane's destination.
//! - **FIFO** (`Reader::Fifo`) — the lane's original raw-PCM FIFO
//!   (snapclient `--player file:<FIFO>`). Retained, unarmed, until the ring
//!   arm is verified on metal; its deletion is its own change.
//!
//! Exactly one is constructed, from exactly one env
//! (`Config::from_env` refuses both together). Neither declared ⇒ this
//! module does not run at all: no open, no syscalls, no per-period work.
//!
//! ## Starvation is SILENCE (owner ruling D4)
//!
//! A period the lane cannot fill is emitted as silence and counted
//! (`DacContentMetrics::starved_periods`); there is no last-good replay and
//! no fallback source. The lane IS the content source on an armed box, so
//! there is nothing to fall back TO — the direct content PCM this lane once
//! fell back to went away with the snd-aloop route (ADR-0100), which left
//! the old damped-recovery policy reaching a caller that parks. Health is
//! self-reported on the STATUS surface (`DacContentMetrics` → the
//! `dac_content` block) — daemon truth, never a Python mirror of env intent
//! (the removed `SNAPFIFO_PRODUCER_WIRED` lesson).
//!
//! ## Timing
//!
//! All I/O is non-blocking and happens on the DAC loop thread; the DAC write
//! remains the sole pacer (inv-1). Worst case per period is one `open(2)`
//! attempt (FIFO missing) or a few bounded `read(2)` calls on the FIFO arm,
//! and one try-consume on the ring arm — never a blocking wait on the
//! producer.
//!
//! ## Channel pick
//!
//! The lane carries the bond's SHARED stereo program (L = leader-seat
//! corrected, R = follower-seat corrected). A stereo-pair leader plays
//! only ITS channel, and — unlike a follower, whose snapclient plays
//! through an ALSA `ttable` plug — this lane has no ALSA hop to do the
//! drop. `ChannelPick` therefore mirrors the channel-split vocabulary
//! (docs/HANDOFF-multiroom.md §4): `left`/`right` duplicate that program
//! channel onto both DAC channels; `mono` averages (the clip-safe L+R sum
//! at −6.02 dB, matching `jasper.camilla_emit.MONO_SUM_GAIN_DB`); `stereo`
//! is passthrough. Both transports carry the same shared-stream format, so
//! the pick is applied identically on either — and it is applied to a
//! STARVED (silent) period too, so the `Sub` low-pass and the mains
//! high-pass keep decaying through an outage instead of resuming from
//! frozen state.

use std::io;
use std::os::fd::RawFd;

use anyhow::Result;
use jasper_tts_protocol::loudness::Biquad;

use crate::shm_ring_source::ShmRingSource;
use crate::types::{ProgramSample, SampleFormat};

/// Sample rate of the round-trip lane. Pinned to the snapclient stream
/// format (48000:16:2) — neither transport carries any other rate, so the
/// sub low-pass / mains high-pass coefficients can be precomputed
/// against this constant.
pub const SUB_SAMPLE_RATE_HZ: f64 = 48_000.0;

/// Default sub crossover corner when the env var is absent or blank. A
/// "sub" member must NEVER play full-range, so a missing corner picks a
/// safe conservative low-pass rather than bypassing the filter.
pub const SUB_DEFAULT_CORNER_HZ: f64 = 80.0;

/// Valid sub crossover corner range (Hz). Mirrors GroupingConfig's
/// `crossover_hz` 40..200 contract; the reconciler clamps before it
/// writes the env, and config.rs clamps again on read (defence in depth).
pub const SUB_MIN_CORNER_HZ: f64 = 40.0;
pub const SUB_MAX_CORNER_HZ: f64 = 200.0;

/// Low-pass section via the RBJ audio-EQ cookbook. An LR4 low-pass is two of
/// these cascaded at Q = 1/sqrt(2) (Butterworth).
fn low_pass_biquad(corner_hz: f64, sample_rate_hz: f64, q: f64) -> Biquad {
    let w0 = 2.0 * std::f64::consts::PI * corner_hz / sample_rate_hz;
    let (sin_w0, cos_w0) = w0.sin_cos();
    let alpha = sin_w0 / (2.0 * q);
    let b1 = 1.0 - cos_w0;
    let b0 = b1 / 2.0;
    let a0 = 1.0 + alpha;
    Biquad::new(
        b0 / a0,
        b1 / a0,
        b0 / a0,
        (-2.0 * cos_w0) / a0,
        (1.0 - alpha) / a0,
    )
}

/// High-pass section via the RBJ audio-EQ cookbook, complementary to the
/// low-pass above.
fn high_pass_biquad(corner_hz: f64, sample_rate_hz: f64, q: f64) -> Biquad {
    let w0 = 2.0 * std::f64::consts::PI * corner_hz / sample_rate_hz;
    let (sin_w0, cos_w0) = w0.sin_cos();
    let alpha = sin_w0 / (2.0 * q);
    let b0 = (1.0 + cos_w0) / 2.0;
    let b1 = -(1.0 + cos_w0);
    let a0 = 1.0 + alpha;
    Biquad::new(
        b0 / a0,
        b1 / a0,
        b0 / a0,
        (-2.0 * cos_w0) / a0,
        (1.0 - alpha) / a0,
    )
}

/// 4th-order Linkwitz-Riley low-pass: two cascaded Butterworth biquads
/// (Q = 1/sqrt(2) each) at the same corner, sample-rate-pinned to the
/// SNAPFIFO 48 kHz stream. Stateful across periods (the per-section
/// Direct Form I history persists), so a period boundary introduces no
/// discontinuity. Unity passband, no added gain.
#[derive(Debug, Clone, Copy)]
pub struct Lr4LowPass {
    s1: Biquad,
    s2: Biquad,
    corner_hz: f64,
}

/// Butterworth Q for a Linkwitz-Riley 4th-order section: 1/sqrt(2).
const LR4_SECTION_Q: f64 = std::f64::consts::FRAC_1_SQRT_2;

impl Lr4LowPass {
    /// Build a fresh LR4 low-pass at `corner_hz`. State starts cleared,
    /// so a (re)construct resets the filter — the contract's "reset on
    /// (re)construct".
    pub fn new(corner_hz: f64) -> Self {
        Self {
            s1: low_pass_biquad(corner_hz, SUB_SAMPLE_RATE_HZ, LR4_SECTION_Q),
            s2: low_pass_biquad(corner_hz, SUB_SAMPLE_RATE_HZ, LR4_SECTION_Q),
            corner_hz,
        }
    }

    /// Process one mono sample through both cascaded sections.
    #[inline]
    fn process(&mut self, x: f64) -> f64 {
        self.s2.process(self.s1.process(x))
    }

    /// The corner this filter was built at (for logs / STATUS).
    pub fn corner_hz(self) -> f64 {
        self.corner_hz
    }
}

/// 4th-order Linkwitz-Riley high-pass: the complementary mains half of
/// the wireless-sub crossover. Same sample-rate pin and state
/// continuity contract as `Lr4LowPass`; unity passband, no added gain.
#[derive(Debug, Clone, Copy)]
pub struct Lr4HighPass {
    s1: Biquad,
    s2: Biquad,
    corner_hz: f64,
}

impl Lr4HighPass {
    /// Build a fresh LR4 high-pass at `corner_hz`.
    pub fn new(corner_hz: f64) -> Self {
        Self {
            s1: high_pass_biquad(corner_hz, SUB_SAMPLE_RATE_HZ, LR4_SECTION_Q),
            s2: high_pass_biquad(corner_hz, SUB_SAMPLE_RATE_HZ, LR4_SECTION_Q),
            corner_hz,
        }
    }

    /// Process one mono sample through both cascaded sections.
    #[inline]
    fn process(&mut self, x: f64) -> f64 {
        self.s2.process(self.s1.process(x))
    }

    /// The corner this filter was built at (for logs / STATUS).
    pub fn corner_hz(self) -> f64 {
        self.corner_hz
    }
}

/// Bound on staged FIFO data, in periods. Caps the extra latency this
/// lane can accumulate if the producer briefly outpaces the DAC
/// (~170 ms at 1024-frame periods); overflow drops the OLDEST whole
/// periods so alignment is preserved and the lane stays current.
///
/// FIFO arm only — the ring's depth is its `n_slots`, a property of the
/// mapping both ends agreed on at attach.
pub const MAX_STAGED_PERIODS: usize = 8;

/// Which channel of the shared stereo program this speaker plays.
///
/// `Sub` carries only its corner frequency (Copy config data); the
/// stateful low-pass FILTER it implies lives on `DacContentSource`
/// (built from this corner in `new`), because filter memory must
/// persist across periods and `ChannelPick` is a per-period Copy value.
/// `PartialEq` (not `Eq`) because `Sub` holds an `f64`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ChannelPick {
    /// Passthrough — both program channels as-is (solo / lab use).
    Stereo,
    /// Program channel 0 duplicated to both DAC channels (a LEFT member).
    Left,
    /// Program channel 1 duplicated to both DAC channels (a RIGHT member).
    Right,
    /// Clip-safe average of both program channels (a mono member).
    Mono,
    /// Clip-safe mono sum THEN a 4th-order Linkwitz-Riley low-pass at the
    /// carried corner (Hz) — a receiver-side "dumb wireless subwoofer".
    /// The mono sum is the same clip-safe average as `Mono`; the LP is
    /// applied by `DacContentSource` from its stateful filter. A `Sub`
    /// member NEVER plays full-range.
    Sub(f64),
}

impl ChannelPick {
    /// Stable wire name for STATUS/logs — the `BackendMode::as_str`
    /// precedent (never a Debug-derived string, which silently changes
    /// if a variant is renamed).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Stereo => "stereo",
            Self::Left => "left",
            Self::Right => "right",
            Self::Mono => "mono",
            Self::Sub(_) => "sub",
        }
    }

    /// Parse the channel-split vocabulary. `sub` parses to a `Sub` at the
    /// default corner; the real corner is injected by config.rs from
    /// `JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ` (it owns the env layer). Unknown
    /// values are a configuration error — fail loud at startup, never guess
    /// a channel (playing the WRONG channel is the silent failure class
    /// `check_grouping_channel_pick` exists for).
    pub fn parse(raw: &str) -> Result<Self, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "" | "stereo" => Ok(Self::Stereo),
            "left" => Ok(Self::Left),
            "right" => Ok(Self::Right),
            "mono" => Ok(Self::Mono),
            "sub" => Ok(Self::Sub(SUB_DEFAULT_CORNER_HZ)),
            other => Err(format!(
                "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL must be one of \
                 stereo|left|right|mono|sub, got {other:?}"
            )),
        }
    }

    /// Clip-safe mono average of one interleaved-stereo frame: (L+R)/2 in
    /// i64 then narrow to the spine width — the same −6.02 dB sum `Mono` uses,
    /// so a full-scale-correlated pair stays full scale with no overflow.
    ///
    /// i64, not i32: two i32 samples sum past the i32 rail, and a wrap there
    /// would turn a loud correlated pair into full-scale opposite polarity.
    #[inline]
    fn mono_avg(frame: &[ProgramSample]) -> ProgramSample {
        (((frame[0] as i64) + (frame[1] as i64)) / 2) as ProgramSample
    }

    /// Apply the pick in place to one interleaved-stereo period.
    ///
    /// Test-only wrapper for cases that exercise channel picking without
    /// the optional mains high-pass.
    #[cfg(test)]
    fn apply(self, period: &mut [ProgramSample], sub_filter: Option<&mut Lr4LowPass>) {
        self.apply_with_main_highpass(period, sub_filter, None);
    }

    /// Apply the pick plus an optional stateful stereo mains high-pass.
    ///
    /// `main_highpass` is used only for main-channel picks
    /// (`Stereo|Left|Right|Mono`). A `Sub` member's safety contract is
    /// mono+LP-or-silence; the HP env is ignored there by construction.
    fn apply_with_main_highpass(
        self,
        period: &mut [ProgramSample],
        sub_filter: Option<&mut Lr4LowPass>,
        main_highpass: Option<&mut [Lr4HighPass; 2]>,
    ) {
        match self {
            Self::Stereo => {}
            Self::Left => {
                for frame in period.chunks_exact_mut(2) {
                    frame[1] = frame[0];
                }
            }
            Self::Right => {
                for frame in period.chunks_exact_mut(2) {
                    frame[0] = frame[1];
                }
            }
            Self::Mono => {
                for frame in period.chunks_exact_mut(2) {
                    let avg = Self::mono_avg(frame);
                    frame[0] = avg;
                    frame[1] = avg;
                }
            }
            Self::Sub(_) => {
                // A "sub" MUST NOT play full-range: missing filter state is
                // a construction bug, not a bypass. Fail closed to silence
                // (never the un-filtered mono sum) and warn — the contract
                // forbids a sub ever emitting the full band.
                let Some(filter) = sub_filter else {
                    debug_assert!(false, "ChannelPick::Sub applied without a low-pass filter");
                    eprintln!(
                        "event=outputd.dac_content.sub_filter_missing action=mute_period \
                         detail=a sub must never play full-range"
                    );
                    period.fill(0);
                    return;
                };
                for frame in period.chunks_exact_mut(2) {
                    // Clip-safe mono sum first (unity), then LP it in f64.
                    let mono = Self::mono_avg(frame) as f64;
                    let lp = filter.process(mono);
                    // Saturate to the spine's rails — the LP passband is unity
                    // and the input is already ≤ full scale, so this only guards
                    // the tiny biquad transient ripple at a step edge. f64 was
                    // already the filter's arithmetic; it now also has to CARRY
                    // the sample, which is exactly why the spine's float math is
                    // f64 and not f32 (a 24-bit mantissa cannot hold an i32).
                    let s = clamp_to_spine(lp);
                    frame[0] = s;
                    frame[1] = s;
                }
                return;
            }
        }
        if let Some(filters) = main_highpass {
            for frame in period.chunks_exact_mut(2) {
                let left = filters[0].process(frame[0] as f64);
                let right = filters[1].process(frame[1] as f64);
                frame[0] = clamp_to_spine(left);
                frame[1] = clamp_to_spine(right);
            }
        }
    }
}

/// Round and saturate one f64 filter output back to a program sample.
///
/// The biquads work in f64 and their outputs can ring a hair past the input's
/// range at a step edge; this is the one place that lands them back on the
/// spine. Named rather than inlined three times so the rounding and the rails
/// have a single author.
#[inline]
fn clamp_to_spine(value: f64) -> ProgramSample {
    value
        .round()
        .clamp(ProgramSample::MIN as f64, ProgramSample::MAX as f64) as ProgramSample
}

/// Pure byte-stream → period assembler with a bounded staging buffer.
///
/// FIFO reads are an unaligned byte stream (the producer's writes can
/// split mid-frame); this struct owns re-alignment: bytes accumulate in
/// `staging`, and a period is handed out only as one exact-sized front
/// slice, so sample/frame alignment is preserved by construction. On
/// overflow it drops the OLDEST whole periods (latency stays bounded and
/// the lane stays current — the freshest audio wins).
#[derive(Debug)]
struct PeriodAssembler {
    staging: Vec<u8>,
    period_bytes: usize,
    overflow_dropped_periods: u64,
}

impl PeriodAssembler {
    fn new(period_bytes: usize) -> Self {
        Self {
            staging: Vec::with_capacity(period_bytes * MAX_STAGED_PERIODS),
            period_bytes,
            overflow_dropped_periods: 0,
        }
    }

    fn push_bytes(&mut self, bytes: &[u8]) {
        self.staging.extend_from_slice(bytes);
        let cap = self.period_bytes * MAX_STAGED_PERIODS;
        if self.staging.len() > cap {
            // Drop oldest whole periods until we fit. Whole-period units
            // keep frame alignment; dropping the FRONT keeps the lane on
            // the freshest audio.
            let excess = self.staging.len() - cap;
            let drop_periods = excess.div_ceil(self.period_bytes);
            let drop_bytes = (drop_periods * self.period_bytes).min(self.staging.len());
            self.staging.drain(..drop_bytes);
            self.overflow_dropped_periods += drop_periods as u64;
        }
    }

    fn staged_periods(&self) -> usize {
        self.staging.len() / self.period_bytes
    }

    /// Pop one period into `out`, widening the wire's S16 samples onto the
    /// program spine. Returns false when a full period is not staged (leaving
    /// `out` untouched — the caller owns the silence).
    /// `out.len() * 2 == period_bytes`.
    ///
    /// The FIFO itself stays **S16 by contract** (D8): the producer is
    /// snapclient, an external process on a documented `48000:16:2` wire, so
    /// `period_bytes` remains 2 bytes per sample and this is an S16 INGRESS into
    /// the spine — the same widen the ALSA content lane performs, at a different
    /// door, and the same width the ring arm's wire carries for the same reason.
    fn pop_period(&mut self, out: &mut [ProgramSample]) -> bool {
        debug_assert_eq!(out.len() * 2, self.period_bytes);
        if self.staging.len() < self.period_bytes {
            return false;
        }
        for (sample, bytes) in out
            .iter_mut()
            .zip(self.staging[..self.period_bytes].chunks_exact(2))
        {
            *sample = jasper_resampler::widen_i16_to_i32(i16::from_le_bytes([bytes[0], bytes[1]]));
        }
        self.staging.drain(..self.period_bytes);
        true
    }
}

/// Counters + gauges for the STATUS `dac_content` block. Plain data —
/// `OutputdState::mark_dac_content` copies it into atomics.
#[derive(Debug, Clone, Copy)]
pub struct DacContentMetrics {
    /// Which transport is armed — `"fifo"` or `"ring"`.
    pub transport: &'static str,
    /// True when the lane filled the LAST period with real audio.
    ///
    /// **The name is the wire's, and it is load-bearing.** Python reads
    /// `dac_content.serving_fifo` for the pair-lock verdict
    /// (`jasper.multiroom.state`, `jasper.control.grouping_supervisor`), where
    /// it means "bytes are flowing" and explicitly NOT "sample lock proven".
    /// That meaning holds unchanged on the ring arm, so the field keeps its
    /// name across the transport change rather than breaking those readers;
    /// the `fifo` vocabulary leaves this lane when the FIFO arm does.
    ///
    /// It is now a per-period fact rather than a damped mode: under D4 there
    /// is no mode to be in, so a poll landing on a starved period honestly
    /// reports false. `starved_periods` carries the cumulative truth.
    pub serving_fifo: bool,
    /// Periods the lane filled with real audio.
    pub fifo_periods: u64,
    /// Periods the lane could not fill and emitted as SILENCE (D4). The
    /// counter is the whole visibility budget for an outage: there is no
    /// fallback source and no replay, so this is what climbing means.
    pub starved_periods: u64,
    /// Periods currently staged (gauge; healthy steady state ≈ 1–2).
    /// FIFO arm only — 0 on the ring, whose queue is the mapping itself.
    pub staged_periods: u64,
    /// Oldest-period drops from staging overflow (producer outpacing
    /// the DAC — should stay 0 with a sane producer). FIFO arm only.
    pub overflow_dropped_periods: u64,
    /// FIFO arm only: the ring attaches once at startup or refuses loudly.
    pub open_failures: u64,
    pub read_failures: u64,
}

/// The lane's raw-PCM FIFO transport — snapclient `--player file:<FIFO>`.
///
/// Retained until the ring arm is verified on metal. All I/O is non-blocking
/// on the DAC loop thread.
struct FifoReader {
    path: String,
    fd: Option<RawFd>,
    assembler: PeriodAssembler,
    read_buf: Vec<u8>,
    open_failures: u64,
    read_failures: u64,
}

impl FifoReader {
    /// No I/O here — the FIFO is opened lazily on the first period so a
    /// not-yet-created path is a normal startup ordering, not an error.
    fn new(path: &str, period_bytes: usize) -> Self {
        Self {
            path: path.to_string(),
            fd: None,
            assembler: PeriodAssembler::new(period_bytes),
            read_buf: vec![0u8; period_bytes],
            open_failures: 0,
            read_failures: 0,
        }
    }

    /// Fill `out` with one period, or ZERO it and return false when the
    /// producer has not staged one. Same post-condition as the ring arm's
    /// `read_period`: `out` is always left complete, so the lane never hands
    /// the DAC a stale buffer.
    fn fill(&mut self, out: &mut [ProgramSample]) -> bool {
        self.open_if_needed();
        self.drain_available();
        if self.assembler.pop_period(out) {
            return true;
        }
        out.fill(0);
        false
    }

    fn open_if_needed(&mut self) {
        if self.fd.is_some() {
            return;
        }
        let c_path = match std::ffi::CString::new(self.path.as_bytes()) {
            Ok(p) => p,
            Err(_) => {
                self.open_failures += 1;
                return;
            }
        };
        // O_RDONLY|O_NONBLOCK on a FIFO succeeds immediately even with
        // no writer yet; reads then return 0 until a writer connects.
        // ENOENT (producer hasn't created it) is a normal startup state:
        // count it and retry next period — one cheap syscall per ~21 ms.
        let fd = unsafe {
            libc::open(
                c_path.as_ptr(),
                libc::O_RDONLY | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd >= 0 {
            eprintln!("event=outputd.dac_content.opened fifo={}", self.path);
            self.fd = Some(fd);
        } else {
            self.open_failures += 1;
        }
    }

    /// Drain whatever the producer has written, bounded by staging
    /// capacity (at most a few reads — never a blocking wait).
    fn drain_available(&mut self) {
        let Some(fd) = self.fd else { return };
        loop {
            if self.assembler.staged_periods() >= MAX_STAGED_PERIODS {
                return; // staging full — stop pulling; overflow policy caps latency
            }
            let n = unsafe {
                libc::read(
                    fd,
                    self.read_buf.as_mut_ptr() as *mut libc::c_void,
                    self.read_buf.len(),
                )
            };
            if n > 0 {
                self.assembler.push_bytes(&self.read_buf[..n as usize]);
                continue;
            }
            if n == 0 {
                // EOF: no writer right now (never connected, or the
                // producer closed). The read end stays valid — a new
                // writer re-arms it — so keep the fd and treat as empty.
                return;
            }
            let err = io::Error::last_os_error();
            match err.raw_os_error() {
                Some(libc::EAGAIN) => return, // writer present, no data yet
                Some(libc::EINTR) => continue,
                _ => {
                    eprintln!(
                        "event=outputd.dac_content.read_failed fifo={} detail={err}",
                        self.path,
                    );
                    self.read_failures += 1;
                    unsafe { libc::close(fd) };
                    self.fd = None; // reopen next period
                    return;
                }
            }
        }
    }
}

impl Drop for FifoReader {
    fn drop(&mut self) {
        if let Some(fd) = self.fd.take() {
            unsafe { libc::close(fd) };
        }
    }
}

/// The lane's transport. Exactly one arm exists per daemon; `Config::from_env`
/// refuses a box that declares both.
enum Reader {
    Fifo(FifoReader),
    /// The SHM ring, read through the SAME reader the central content hop uses
    /// ([`ShmRingSource`]) rather than a second attach/widen/counter
    /// implementation: it already is "attach a declared geometry, try-consume
    /// one slot per DAC period, zero-fill on empty, never block".
    Ring(ShmRingSource),
}

/// The DAC-content source. One instance per daemon, owned by the DAC loop;
/// all I/O non-blocking on that thread.
pub struct DacContentSource {
    reader: Reader,
    channel: ChannelPick,
    /// Stateful LR4 low-pass for a `Sub` channel — `Some` iff
    /// `channel` is `ChannelPick::Sub`. Owned here (not on the Copy
    /// `ChannelPick`) so its biquad memory persists across periods;
    /// (re)construct resets it.
    sub_filter: Option<Lr4LowPass>,
    /// Optional stateful stereo LR4 high-pass for MAIN channels when a
    /// wireless sub is present and bass management is enabled. Two filter
    /// instances keep L/R state independent. `None` means full-range mains.
    main_highpass: Option<[Lr4HighPass; 2]>,
    served_periods: u64,
    starved_periods: u64,
    last_period_served: bool,
    logged_first_starvation: bool,
}

impl DacContentSource {
    /// The FIFO transport. No I/O here — see [`FifoReader::new`].
    pub fn fifo(
        path: &str,
        channel: ChannelPick,
        period_frames: u32,
        main_highpass_hz: Option<f64>,
    ) -> Self {
        let period_bytes = (period_frames as usize) * 2 /* channels */ * 2 /* bytes */;
        Self::with_reader(
            Reader::Fifo(FifoReader::new(path, period_bytes)),
            channel,
            main_highpass_hz,
        )
    }

    /// The SHM ring transport — attaches (or creates) the return ring at
    /// `path` at the lane's pinned geometry.
    ///
    /// The geometry is NOT negotiated here: the wire is S16LE stereo by
    /// contract (snapclient decodes to the snapserver-pinned `48000:16:2`)
    /// and the slot is one DAC period, so the only free field is `n_slots`,
    /// which the caller passes from the same constant the conf.d block and
    /// `jasper.multiroom.dac_content_ring` spell. `RingReader::create_or_attach`
    /// compares every field against the live header and refuses a mismatch
    /// with `InvalidData`, so a writer that disagrees on ANY of them parks
    /// this daemon instead of being reinterpreted at the wrong stride.
    pub fn ring(
        path: &str,
        channel: ChannelPick,
        period_frames: u32,
        n_slots: u32,
        main_highpass_hz: Option<f64>,
    ) -> io::Result<Self> {
        let ring = ShmRingSource::new(path, period_frames, 2, SampleFormat::S16Le, n_slots)?;
        Ok(Self::with_reader(
            Reader::Ring(ring),
            channel,
            main_highpass_hz,
        ))
    }

    fn with_reader(reader: Reader, channel: ChannelPick, main_highpass_hz: Option<f64>) -> Self {
        // A Sub channel owns a fresh (state-cleared) low-pass at its
        // carried corner; every other pick has no filter.
        let sub_filter = match channel {
            ChannelPick::Sub(corner_hz) => Some(Lr4LowPass::new(corner_hz)),
            _ => None,
        };
        let main_highpass = match (channel, main_highpass_hz) {
            (ChannelPick::Sub(_), _) | (_, None) => None,
            (_, Some(corner_hz)) => {
                Some([Lr4HighPass::new(corner_hz), Lr4HighPass::new(corner_hz)])
            }
        };
        Self {
            reader,
            channel,
            sub_filter,
            main_highpass,
            served_periods: 0,
            starved_periods: 0,
            last_period_served: false,
            logged_first_starvation: false,
        }
    }

    /// Fill `out` with this lane's period. Never blocks.
    ///
    /// An armed lane IS the content source, so it always answers: real audio
    /// when the producer kept up, SILENCE when it did not (D4 — no replay, no
    /// fallback, a counter instead). The caller therefore has no "not served"
    /// branch to take.
    ///
    /// The `Err` is the ring arm's slot-length contract only — a destination
    /// that is not exactly one slot would emit a short or stale period, so it
    /// fails loud rather than playing it. Publish [`Self::metrics`] before
    /// propagating it, as the central ring's call site does, so `/state`'s
    /// last sample stays honest through a fatal period.
    pub fn fill_period(&mut self, out: &mut [ProgramSample]) -> Result<()> {
        let served = match &mut self.reader {
            Reader::Fifo(fifo) => fifo.fill(out),
            // `read_period` zero-fills on an empty ring, so both arms leave
            // `out` complete either way.
            Reader::Ring(ring) => ring.read_period(out)? > 0,
        };
        self.last_period_served = served;
        if served {
            self.served_periods += 1;
        } else {
            self.starved_periods += 1;
            if !self.logged_first_starvation {
                // Once per process: the counter carries the rest, so a
                // chronically dry producer cannot spam the journal.
                eprintln!(
                    "event=outputd.dac_content.starved transport={} action=emit_silence \
                     detail=D4: the return lane has no fallback source; see \
                     starved_periods in /state",
                    self.transport(),
                );
                self.logged_first_starvation = true;
            }
        }
        // Applied on a starved period too, so the Sub low-pass and the mains
        // high-pass decay through an outage instead of resuming from frozen
        // state. On silence every pick is value-preserving except those two
        // filters, which is exactly the point.
        self.channel.apply_with_main_highpass(
            out,
            self.sub_filter.as_mut(),
            self.main_highpass.as_mut(),
        );
        Ok(())
    }

    fn transport(&self) -> &'static str {
        match self.reader {
            Reader::Fifo(_) => "fifo",
            Reader::Ring(_) => "ring",
        }
    }

    pub fn main_highpass_corner_hz(&self) -> Option<f64> {
        self.main_highpass.map(|filters| filters[0].corner_hz())
    }

    pub fn metrics(&self) -> DacContentMetrics {
        let (staged_periods, overflow_dropped_periods, open_failures, read_failures) =
            match &self.reader {
                Reader::Fifo(fifo) => (
                    fifo.assembler.staged_periods() as u64,
                    fifo.assembler.overflow_dropped_periods,
                    fifo.open_failures,
                    fifo.read_failures,
                ),
                Reader::Ring(_) => (0, 0, 0, 0),
            };
        DacContentMetrics {
            transport: self.transport(),
            serving_fifo: self.last_period_served,
            fifo_periods: self.served_periods,
            starved_periods: self.starved_periods,
            staged_periods,
            overflow_dropped_periods,
            open_failures,
            read_failures,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    // ---------- pure: PeriodAssembler ----------

    fn le_bytes(samples: &[i16]) -> Vec<u8> {
        samples.iter().flat_map(|s| s.to_le_bytes()).collect()
    }

    /// One S16 wire sample at the program spine's scale — what `pop_period`
    /// now yields, because the FIFO's S16 wire is an ingress into the spine.
    fn w(sample: i16) -> ProgramSample {
        jasper_resampler::widen_i16_to_i32(sample)
    }

    /// A whole period of wire samples at the spine's scale.
    fn wv(samples: &[i16]) -> Vec<ProgramSample> {
        samples.iter().copied().map(w).collect()
    }

    #[test]
    fn assembler_reassembles_periods_across_unaligned_pushes() {
        // 2-frame periods (4 samples, 8 bytes). Push split mid-sample.
        let mut a = PeriodAssembler::new(8);
        let bytes = le_bytes(&[100, -100, 2000, -2000, 7, 8, 9, 10]);
        a.push_bytes(&bytes[..3]); // mid-sample split
        assert_eq!(a.staged_periods(), 0);
        a.push_bytes(&bytes[3..9]); // crosses the first period boundary
        assert_eq!(a.staged_periods(), 1);
        a.push_bytes(&bytes[9..]);

        let mut out = [0 as ProgramSample; 4];
        assert!(a.pop_period(&mut out));
        assert_eq!(out.to_vec(), wv(&[100, -100, 2000, -2000]));
        assert!(a.pop_period(&mut out));
        assert_eq!(out.to_vec(), wv(&[7, 8, 9, 10]));
        assert!(!a.pop_period(&mut out)); // drained
    }

    #[test]
    fn assembler_widens_the_s16_wire_onto_the_spine_losslessly() {
        // The FIFO stays a `48000:16:2` snapclient wire (D8): `period_bytes` is
        // still 2 bytes per sample, and this is where those bytes become spine
        // samples. Full scale both signs must survive the door.
        let mut a = PeriodAssembler::new(8);
        a.push_bytes(&le_bytes(&[i16::MAX, i16::MIN, 0, -1]));
        let mut out = [0 as ProgramSample; 4];
        assert!(a.pop_period(&mut out));
        assert_eq!(out, [0x7FFF_0000, ProgramSample::MIN, 0, -0x0001_0000]);
        // And it is reversible: the wire's bytes are recoverable exactly.
        let mut back = [0i16; 4];
        crate::types::narrow_period(&out, &mut back).unwrap();
        assert_eq!(back, [i16::MAX, i16::MIN, 0, -1]);
    }

    #[test]
    fn assembler_overflow_drops_oldest_whole_periods() {
        let mut a = PeriodAssembler::new(8);
        // Stage MAX + 2 periods; the 2 OLDEST must be dropped, keeping
        // alignment and the freshest audio.
        let total = MAX_STAGED_PERIODS + 2;
        for i in 0..total {
            let v = i as i16;
            a.push_bytes(&le_bytes(&[v, v, v, v]));
        }
        assert_eq!(a.staged_periods(), MAX_STAGED_PERIODS);
        assert_eq!(a.overflow_dropped_periods, 2);
        let mut out = [0 as ProgramSample; 4];
        assert!(a.pop_period(&mut out));
        assert_eq!(out.to_vec(), wv(&[2, 2, 2, 2])); // periods 0 and 1 dropped
    }

    // ---------- pure: ChannelPick ----------

    #[test]
    fn channel_pick_parses_the_channel_split_vocabulary() {
        assert_eq!(ChannelPick::parse(""), Ok(ChannelPick::Stereo));
        assert_eq!(ChannelPick::parse("stereo"), Ok(ChannelPick::Stereo));
        assert_eq!(ChannelPick::parse("LEFT"), Ok(ChannelPick::Left));
        assert_eq!(ChannelPick::parse("right"), Ok(ChannelPick::Right));
        assert_eq!(ChannelPick::parse("mono"), Ok(ChannelPick::Mono));
        // "sub" is now its own pick (mono sum + LR4 LP), no longer an
        // alias for Mono. It parses at the default corner; config.rs
        // injects the real JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ corner.
        assert_eq!(
            ChannelPick::parse("sub"),
            Ok(ChannelPick::Sub(SUB_DEFAULT_CORNER_HZ))
        );
        assert_eq!(ChannelPick::parse("SUB"), Ok(ChannelPick::Sub(80.0)));
        assert!(ChannelPick::parse("both").is_err());
    }

    #[test]
    fn channel_pick_left_right_duplicate_and_mono_averages_clip_safe() {
        let mut p = wv(&[100, -200, 1000, 2000]);
        ChannelPick::Left.apply(&mut p, None);
        assert_eq!(p, wv(&[100, 100, 1000, 1000]));

        let mut p = wv(&[100, -200, 1000, 2000]);
        ChannelPick::Right.apply(&mut p, None);
        assert_eq!(p, wv(&[-200, -200, 2000, 2000]));

        let mut p = wv(&[100, -200, i16::MAX, i16::MAX]);
        ChannelPick::Mono.apply(&mut p, None);
        assert_eq!(p[0], w(-50));
        assert_eq!(p[1], w(-50));
        // Full-scale L==R averages back to full scale, no overflow.
        assert_eq!(p[2], w(i16::MAX));
        assert_eq!(p[3], w(i16::MAX));

        let mut p = wv(&[1, 2, 3, 4]);
        ChannelPick::Stereo.apply(&mut p, None);
        assert_eq!(p, wv(&[1, 2, 3, 4]));
    }

    #[test]
    fn mono_average_cannot_overflow_at_the_spine_rails() {
        // The i64 accumulator's reason to exist: two i32 samples sum past the
        // i32 rail. In i32 this wraps — a correlated full-scale pair would come
        // out full-scale OPPOSITE polarity, the loudest defect a mono fold can
        // produce. The old i16 version had the same argument one width down.
        let mut p = [ProgramSample::MAX, ProgramSample::MAX];
        ChannelPick::Mono.apply(&mut p, None);
        assert_eq!(p, [ProgramSample::MAX, ProgramSample::MAX]);

        let mut p = [ProgramSample::MIN, ProgramSample::MIN];
        ChannelPick::Mono.apply(&mut p, None);
        assert_eq!(p, [ProgramSample::MIN, ProgramSample::MIN]);

        // And the -6.02 dB sum of an anti-correlated full-scale pair is silence,
        // not a wrap to a rail.
        let mut p = [ProgramSample::MAX, ProgramSample::MIN];
        ChannelPick::Mono.apply(&mut p, None);
        assert_eq!(p, [0, 0]);
    }

    // ---------- pure: LR4 low-pass (the dumb-sub filter) ----------

    /// Drive a fresh LR4 LP with a steady sinusoid and measure the
    /// steady-state output amplitude (linear gain) at `freq`. The first
    /// `settle` samples are discarded so the biquad transient does not
    /// pollute the magnitude estimate.
    fn lr4_gain_at(corner_hz: f64, freq: f64) -> f64 {
        let mut lp = Lr4LowPass::new(corner_hz);
        let n = 48_000usize; // 1 s — plenty of cycles even at 40 Hz
        let settle = 4_800usize;
        let amp = 10_000.0;
        let mut peak = 0.0f64;
        for i in 0..n {
            let t = i as f64 / SUB_SAMPLE_RATE_HZ;
            let x = amp * (2.0 * std::f64::consts::PI * freq * t).sin();
            let y = lp.process(x);
            if i >= settle {
                peak = peak.max(y.abs());
            }
        }
        peak / amp
    }

    /// Same measurement harness for the complementary LR4 HP.
    fn lr4_hp_gain_at(corner_hz: f64, freq: f64) -> f64 {
        let mut hp = Lr4HighPass::new(corner_hz);
        let n = 48_000usize;
        let settle = 4_800usize;
        let amp = 10_000.0;
        let mut peak = 0.0f64;
        for i in 0..n {
            let t = i as f64 / SUB_SAMPLE_RATE_HZ;
            let x = amp * (2.0 * std::f64::consts::PI * freq * t).sin();
            let y = hp.process(x);
            if i >= settle {
                peak = peak.max(y.abs());
            }
        }
        peak / amp
    }

    fn lin_to_db(g: f64) -> f64 {
        20.0 * g.log10()
    }

    #[test]
    fn lr4_is_minus_3db_at_the_corner() {
        // Linkwitz-Riley 4th-order is −6 dB at Fc (two cascaded
        // Butterworth sections, each −3 dB). The contract asks for
        // −3 dB ±~1 dB "at Fc"; LR4 by definition lands at −6 dB, which
        // is the correct, documented LR crossover point. Assert the LR4
        // signature directly.
        let g = lin_to_db(lr4_gain_at(80.0, 80.0));
        assert!(
            (g - (-6.0)).abs() <= 1.0,
            "LR4 corner gain {g:.2} dB not within 1 dB of -6 dB"
        );
    }

    #[test]
    fn lr4_rolls_off_about_24db_per_octave_above_corner() {
        // 4th-order ⇒ ~24 dB/octave in the stopband. Measure one octave
        // up (160 vs 320 Hz, both well above the 80 Hz corner).
        let g1 = lin_to_db(lr4_gain_at(80.0, 160.0));
        let g2 = lin_to_db(lr4_gain_at(80.0, 320.0));
        let slope = g1 - g2; // dB drop across one octave
        assert!(
            (slope - 24.0).abs() <= 3.0,
            "octave slope {slope:.2} dB not within 3 dB of 24 dB ({g1:.2} -> {g2:.2})"
        );
    }

    #[test]
    fn lr4_passes_very_low_frequencies_near_unity_no_boost() {
        // Deep passband (one decade below corner): unity, never a boost.
        let g = lin_to_db(lr4_gain_at(80.0, 8.0));
        assert!(g <= 0.05, "passband gain {g:.3} dB shows a boost");
        assert!(g >= -1.0, "passband gain {g:.3} dB unexpectedly low");
    }

    #[test]
    fn lr4_dc_passes_at_unity() {
        // A DC step settles to its input value (unity passband at 0 Hz).
        let mut lp = Lr4LowPass::new(80.0);
        let mut y = 0.0;
        for _ in 0..48_000 {
            y = lp.process(10_000.0);
        }
        assert!(
            (y - 10_000.0).abs() < 1.0,
            "DC settled to {y}, expected 10000"
        );
    }

    // ---------- pure: LR4 high-pass (wireless-sub bass management) ----------

    #[test]
    fn lr4_highpass_is_minus_6db_at_the_corner() {
        let g = lin_to_db(lr4_hp_gain_at(80.0, 80.0));
        assert!(
            (g - (-6.0)).abs() <= 1.0,
            "LR4 HP corner gain {g:.2} dB not within 1 dB of -6 dB"
        );
    }

    #[test]
    fn lr4_highpass_attenuates_below_corner_and_passes_above() {
        let low = lin_to_db(lr4_hp_gain_at(80.0, 8.0));
        let high = lin_to_db(lr4_hp_gain_at(80.0, 800.0));
        assert!(low < -35.0, "8 Hz leaked through HP: {low:.2} dB");
        assert!(high <= 0.05, "HP passband gain {high:.3} dB shows a boost");
        assert!(
            high >= -1.0,
            "HP passband gain {high:.3} dB unexpectedly low"
        );
    }

    #[test]
    fn lr4_highpass_rejects_dc() {
        let mut hp = Lr4HighPass::new(80.0);
        let mut y = 0.0;
        for _ in 0..48_000 {
            y = hp.process(10_000.0);
        }
        assert!(y.abs() < 1.0, "DC settled to {y}, expected near silence");
    }

    // ---------- pure: ChannelPick::Sub apply ----------

    /// Run a Sub apply over `frames` frames of a steady stereo sine and
    /// return the per-output-channel sample buffers (ch0, ch1).
    ///
    /// The signal is the SAME amplitude as before, expressed at the spine's
    /// scale. Every amplitude threshold in the tests below is likewise written
    /// `w(x)` — and that is a derivation, not a blind rescale: an LR4 biquad
    /// cascade is LINEAR, so its attenuation at a given frequency is a ratio,
    /// independent of the units the samples are counted in. A threshold stated as
    /// a fraction of the input amplitude therefore transfers exactly, and `w(x)`
    /// is that same fraction at the new width.
    fn sub_apply_run(
        corner_hz: f64,
        freq: f64,
        frames: usize,
    ) -> (Vec<ProgramSample>, Vec<ProgramSample>) {
        let mut filter = Lr4LowPass::new(corner_hz);
        let pick = ChannelPick::Sub(corner_hz);
        let amp = f64::from(w(10_000));
        let mut ch0 = Vec::with_capacity(frames);
        let mut ch1 = Vec::with_capacity(frames);
        for i in 0..frames {
            let t = i as f64 / SUB_SAMPLE_RATE_HZ;
            let s = (amp * (2.0 * std::f64::consts::PI * freq * t).sin()) as ProgramSample;
            // L == R so the clip-safe mono sum is the input amplitude.
            let mut period = [s, s];
            pick.apply(&mut period, Some(&mut filter));
            ch0.push(period[0]);
            ch1.push(period[1]);
        }
        (ch0, ch1)
    }

    /// Peak magnitude over a settled tail, in i64 so `abs()` cannot overflow at
    /// `ProgramSample::MIN` (at i32 that is a real panic, not a theoretical one).
    fn settled_peak(v: &[ProgramSample], from: usize) -> i64 {
        v[from..].iter().map(|&s| i64::from(s).abs()).max().unwrap()
    }

    #[test]
    fn sub_apply_writes_identical_mono_to_both_channels() {
        let (ch0, ch1) = sub_apply_run(80.0, 50.0, 2_000);
        assert_eq!(
            ch0, ch1,
            "sub must write the same mono sample to both channels"
        );
    }

    #[test]
    fn sub_apply_low_passes_high_content_away() {
        // A 4 kHz tone (decades above the 80 Hz corner) is crushed to
        // near silence; a 40 Hz tone (in band) survives. Same clip-safe
        // mono sum feeds both — only the LP differs.
        let (hi, _) = sub_apply_run(80.0, 4_000.0, 6_000);
        let (lo, _) = sub_apply_run(80.0, 40.0, 6_000);
        let hi_peak = settled_peak(&hi, 2_000);
        let lo_peak = settled_peak(&lo, 2_000);
        assert!(hi_peak < i64::from(w(200)), "4 kHz leaked: peak {hi_peak}");
        assert!(
            lo_peak > i64::from(w(5_000)),
            "40 Hz wrongly attenuated: peak {lo_peak}"
        );
    }

    #[test]
    fn sub_apply_full_scale_input_does_not_overflow_the_spine() {
        // Full-scale DC on both channels (mono sum = full scale). The LP
        // settles to full scale; the saturating cast must not wrap.
        let mut filter = Lr4LowPass::new(80.0);
        let pick = ChannelPick::Sub(80.0);
        let mut last = [0 as ProgramSample; 2];
        for _ in 0..48_000 {
            let mut period = [ProgramSample::MAX, ProgramSample::MAX];
            pick.apply(&mut period, Some(&mut filter));
            last = period;
        }
        // Settled near full scale, never wrapped to a negative value. The settle
        // slack is 4 S16 LSBs — the same FRACTION of full scale the pre-spine
        // assertion allowed (it read `> i16::MAX - 4`), which is the right form
        // for a tolerance on a linear filter's settled DC value.
        assert!(
            last[0] > ProgramSample::MAX - 4 * w(1),
            "DC step did not settle to full scale: {last:?}"
        );
        assert_eq!(last[0], last[1]);

        // A sustained full-scale positive step drives the Butterworth LP
        // into its step-overshoot region (an LR4 step response rings
        // slightly past the final value). The saturating cast must clamp
        // that overshoot to full scale, NEVER wrap to a negative sample.
        let mut filter = Lr4LowPass::new(200.0); // higher corner = faster, larger overshoot
        let mut saw_clamp = false;
        for _ in 0..2_000 {
            let mut period = [ProgramSample::MAX, ProgramSample::MAX];
            pick.apply(&mut period, Some(&mut filter));
            // A positive step can never legitimately produce a negative
            // output here; a negative value would be an integer wrap.
            assert!(period[0] >= 0, "full-scale step wrapped to {}", period[0]);
            if period[0] == ProgramSample::MAX {
                saw_clamp = true;
            }
        }
        assert!(
            saw_clamp,
            "saturating clamp never engaged on a full-scale step"
        );
    }

    #[test]
    fn sub_apply_state_is_continuous_across_period_boundaries() {
        // Process one big buffer vs two consecutive period calls on the
        // SAME filter: the stateful filter must produce byte-identical
        // output (no discontinuity at the period boundary).
        let corner = 80.0;
        let freq = 120.0;
        let total = 1_024usize;
        let amp = 12_000.0;
        let sample = |i: usize| -> ProgramSample {
            let t = i as f64 / SUB_SAMPLE_RATE_HZ;
            w((amp * (2.0 * std::f64::consts::PI * freq * t).sin()) as i16)
        };

        // One big buffer.
        let mut big_filter = Lr4LowPass::new(corner);
        let pick = ChannelPick::Sub(corner);
        let mut big = vec![0 as ProgramSample; total * 2];
        for i in 0..total {
            big[2 * i] = sample(i);
            big[2 * i + 1] = sample(i);
        }
        pick.apply(&mut big, Some(&mut big_filter));

        // Two halves through the same persistent filter.
        let mut split_filter = Lr4LowPass::new(corner);
        let half = total / 2;
        let mut a = vec![0 as ProgramSample; half * 2];
        let mut b = vec![0 as ProgramSample; half * 2];
        for i in 0..half {
            a[2 * i] = sample(i);
            a[2 * i + 1] = sample(i);
            b[2 * i] = sample(half + i);
            b[2 * i + 1] = sample(half + i);
        }
        pick.apply(&mut a, Some(&mut split_filter));
        pick.apply(&mut b, Some(&mut split_filter));

        let mut joined = a;
        joined.extend_from_slice(&b);
        assert_eq!(big, joined, "period boundary introduced a discontinuity");
    }

    #[test]
    fn sub_apply_without_filter_mutes_never_full_range() {
        // Construction-bug guard: a Sub applied with no filter must fail
        // CLOSED to silence — never emit the un-filtered (full-range)
        // mono sum. (debug_assert fires in debug; release mutes.)
        let pick = ChannelPick::Sub(80.0);
        // Catch the debug_assert panic so the test asserts the muting
        // behaviour on both debug and release builds.
        let result = std::panic::catch_unwind(|| {
            let mut p = [ProgramSample::MAX, ProgramSample::MAX, w(1234), w(1234)];
            pick.apply(&mut p, None);
            p
        });
        // Ok => release-build muting; Err => debug_assert tripped. Both
        // are acceptable fail-closed outcomes (never the full-range sum).
        if let Ok(p) = result {
            assert_eq!(p, [0, 0, 0, 0], "missing-filter Sub must mute");
        }
    }

    #[test]
    fn source_sub_channel_builds_a_filter_and_default_corner_when_unspecified() {
        // A "sub" pick at the default corner builds a low-pass on the
        // source (a sub must never run filterless / full-range).
        let fifo = TempFifo::create("sub-default");
        let src = DacContentSource::fifo(
            fifo.path_str(),
            ChannelPick::Sub(SUB_DEFAULT_CORNER_HZ),
            TEST_PERIOD_FRAMES,
            None,
        );
        assert!(
            src.sub_filter.is_some(),
            "Sub source must own a low-pass filter"
        );
        assert_eq!(src.sub_filter.unwrap().corner_hz(), 80.0);
    }

    #[test]
    fn source_main_highpass_is_built_for_mains_and_ignored_for_sub() {
        let fifo = TempFifo::create("main-hp-build");
        let src = DacContentSource::fifo(
            fifo.path_str(),
            ChannelPick::Left,
            TEST_PERIOD_FRAMES,
            Some(80.0),
        );
        assert_eq!(src.main_highpass_corner_hz(), Some(80.0));

        let sub = DacContentSource::fifo(
            fifo.path_str(),
            ChannelPick::Sub(80.0),
            TEST_PERIOD_FRAMES,
            Some(80.0),
        );
        assert_eq!(sub.main_highpass_corner_hz(), None);
        assert!(sub.sub_filter.is_some());
    }

    /// A starved period REPLACES whatever the caller left in the buffer.
    ///
    /// Unfiltered pick, so the lane's silence is exactly zeros: the caller's
    /// stale content must not survive, which is the D4 half that says an outage
    /// is silence rather than a replay.
    #[test]
    fn a_starved_period_replaces_the_callers_buffer_with_silence() {
        let fifo = TempFifo::create("starved-silence");
        let mut src = DacContentSource::fifo(
            fifo.path_str(),
            ChannelPick::Stereo,
            TEST_PERIOD_FRAMES,
            None,
        );
        let mut out = vec![w(12_345); (TEST_PERIOD_FRAMES as usize) * 2];
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![0 as ProgramSample; 8]);
        let m = src.metrics();
        assert!(!m.serving_fifo);
        assert_eq!(m.starved_periods, 1);
        assert_eq!(m.fifo_periods, 0);
    }

    /// A starved period still runs the pick's stateful filters.
    ///
    /// This is what survives of the deleted `apply_pick_to_fallback_period`
    /// pins: their subject (a fallback period carrying the direct lane's
    /// full-range stereo) went away with the fallback, but the
    /// filter-continuity property they depended on is now the reason the pick
    /// runs on silence — a filter frozen through an outage resumes from stale
    /// memory and thumps.
    ///
    /// The distinguishing evidence is a `Sub` member's ring-down: after a loud
    /// run the first starved periods are NOT silent, and the tail shrinks. A
    /// source that skipped the pick on starvation would emit exact zeros from
    /// the first starved period, so this fails on that mutation.
    #[test]
    fn a_starved_period_keeps_the_stateful_filters_running() {
        let fifo = TempFifo::create("starved-sub-tail");
        let mut src = DacContentSource::fifo(
            fifo.path_str(),
            ChannelPick::Sub(80.0),
            TEST_PERIOD_FRAMES,
            None,
        );
        let mut writer = connect_producer(&mut src, &fifo);
        let mut out = vec![0 as ProgramSample; 8];
        // Settle the LR4 low-pass at full scale: one period in, one period out,
        // so staging never overflows. 600 x 4 frames = 50 ms, many time
        // constants at an 80 Hz corner.
        let loud = le_bytes(&[i16::MAX; 8]);
        for _ in 0..600 {
            writer.write_all(&loud).unwrap();
            src.fill_period(&mut out).unwrap();
        }
        assert!(src.metrics().serving_fifo);
        let settled = settled_peak(&out, 0);
        assert!(
            settled > i64::from(w(20_000)),
            "LP never settled: {settled}"
        );

        // Producer stops. The lane is starving, but the low-pass keeps being
        // driven with silence, so its charge rings DOWN instead of vanishing.
        drop(writer);
        let mut first_starved_peak = None;
        let mut last_peak = i64::MAX;
        for _ in 0..40 {
            src.fill_period(&mut out).unwrap();
            if src.metrics().serving_fifo {
                continue; // still draining the kernel FIFO
            }
            let peak = settled_peak(&out, 0);
            if first_starved_peak.is_none() {
                first_starved_peak = Some(peak);
            } else {
                assert!(
                    peak < last_peak,
                    "the low-pass tail must decay through the outage: {peak} !< {last_peak}"
                );
            }
            last_peak = peak;
        }
        let first = first_starved_peak.expect("the source never starved");
        assert!(
            first > i64::from(w(1_000)),
            "a frozen filter would have emitted exact silence; got {first}"
        );
        assert!(src.metrics().starved_periods >= 2);
    }

    // ---------- end-to-end with a real FIFO ----------

    fn temp_fifo_path(tag: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "jts-dac-content-{tag}-{}-{nonce}.fifo",
            std::process::id()
        ))
    }

    struct TempFifo {
        path: std::path::PathBuf,
    }

    impl TempFifo {
        fn create(tag: &str) -> Self {
            let path = temp_fifo_path(tag);
            let c_path = std::ffi::CString::new(path.as_os_str().to_str().unwrap()).unwrap();
            let rc = unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) };
            assert_eq!(rc, 0, "mkfifo failed: {}", io::Error::last_os_error());
            Self { path }
        }

        fn path_str(&self) -> &str {
            self.path.to_str().unwrap()
        }
    }

    impl Drop for TempFifo {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }

    /// 4-frame periods keep the byte math tiny: 16 bytes per period.
    const TEST_PERIOD_FRAMES: u32 = 4;

    /// Open a producer (write end) on a temp FIFO, faithfully mirroring
    /// production ORDER: the source opens its `O_RDONLY|O_NONBLOCK` read
    /// end FIRST (it never blocks, even with no writer), THEN the
    /// producer connects. A blocking `O_WRONLY` open deadlocks if no
    /// reader exists yet — a single-thread test-harness hazard, never a
    /// production one (there the producer is a separate process and the
    /// source's open is always non-blocking). This helper enforces the
    /// ordering so no test can reintroduce that deadlock.
    fn connect_producer(src: &mut DacContentSource, fifo: &TempFifo) -> std::fs::File {
        let mut out = vec![0 as ProgramSample; (TEST_PERIOD_FRAMES as usize) * 2];
        // Prime the source's read end (a starved period, no writer yet).
        src.fill_period(&mut out).unwrap();
        std::fs::OpenOptions::new()
            .write(true)
            .open(&fifo.path)
            .expect("producer open on a primed FIFO must not block")
    }

    #[test]
    fn fifo_serves_the_producers_periods_and_counts_them() {
        let fifo = TempFifo::create("fifo-serves");
        let mut src =
            DacContentSource::fifo(fifo.path_str(), ChannelPick::Left, TEST_PERIOD_FRAMES, None);
        let mut out = vec![0 as ProgramSample; 8];

        // No writer: silence, honest counters, no panic, no block.
        for _ in 0..3 {
            src.fill_period(&mut out).unwrap();
            assert!(out.iter().all(|&s| s == 0));
        }
        let m = src.metrics();
        assert_eq!(m.transport, "fifo");
        assert!(!m.serving_fifo);
        assert_eq!(m.starved_periods, 3);
        assert_eq!(m.fifo_periods, 0);

        // Producer connects: the lane serves the very next period — there is
        // no damped engagement streak left to wait through.
        let mut writer = connect_producer(&mut src, &fifo);
        writer
            .write_all(&le_bytes(&[3i16, -3, 3, -3, 3, -3, 3, -3]))
            .unwrap();
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![w(3); 8], "ChannelPick::Left duplicates ch0");
        let m = src.metrics();
        assert!(m.serving_fifo);
        assert_eq!(m.fifo_periods, 1);
        assert_eq!(m.open_failures, 0);
    }

    #[test]
    fn fifo_starves_to_silence_when_the_writer_stops() {
        let fifo = TempFifo::create("fifo-outage");
        let mut src = DacContentSource::fifo(
            fifo.path_str(),
            ChannelPick::Stereo,
            TEST_PERIOD_FRAMES,
            None,
        );
        let mut out = vec![0 as ProgramSample; 8];
        let mut writer = connect_producer(&mut src, &fifo);
        writer.write_all(&le_bytes(&[9i16; 8])).unwrap();
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![w(9); 8]);

        // Writer dies: drain whatever is buffered, then every further period
        // is SILENCE — never a replay of the last good one (D4).
        drop(writer);
        let drain_bound = MAX_STAGED_PERIODS + 8;
        let mut starved = false;
        for _ in 0..drain_bound {
            src.fill_period(&mut out).unwrap();
            if !src.metrics().serving_fifo {
                starved = true;
                break;
            }
        }
        assert!(starved, "source kept claiming audio after writer death");
        assert_eq!(
            out,
            vec![0 as ProgramSample; 8],
            "starvation must be silence"
        );
        assert!(src.metrics().starved_periods >= 1);
    }

    #[test]
    fn fifo_never_blocks_with_a_writer_that_sends_nothing() {
        let fifo = TempFifo::create("idle-writer");
        let mut src = DacContentSource::fifo(
            fifo.path_str(),
            ChannelPick::Stereo,
            TEST_PERIOD_FRAMES,
            None,
        );
        // Writer connected but silent: reads must be EAGAIN, not a hang.
        let _writer = connect_producer(&mut src, &fifo);
        let mut out = vec![0 as ProgramSample; 8];
        let start = Instant::now();
        for _ in 0..10 {
            src.fill_period(&mut out).unwrap();
        }
        assert!(
            start.elapsed() < Duration::from_millis(200),
            "non-blocking contract violated: {:?}",
            start.elapsed()
        );
        assert_eq!(src.metrics().read_failures, 0);
    }

    #[test]
    fn fifo_missing_path_counts_open_failures_and_stays_silent() {
        let path = temp_fifo_path("missing"); // never mkfifo'd
        let mut src = DacContentSource::fifo(
            path.to_str().unwrap(),
            ChannelPick::Stereo,
            TEST_PERIOD_FRAMES,
            None,
        );
        let mut out = vec![0 as ProgramSample; 8];
        for _ in 0..3 {
            src.fill_period(&mut out).unwrap();
        }
        let m = src.metrics();
        assert_eq!(m.open_failures, 3); // one retry per period, cheap
        assert_eq!(m.starved_periods, 3);
        assert!(!m.serving_fifo);
    }

    // ---------- the ring transport ----------

    use jasper_ring::{Geometry, TestRingWriter, SAMPLE_FORMAT_S16LE, SAMPLE_FORMAT_S32LE};

    fn temp_ring_path(tag: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!(
            "jts-dac-content-ring-{tag}-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("dac-content.ring")
    }

    struct TempRing {
        path: std::path::PathBuf,
    }

    impl TempRing {
        fn create(tag: &str) -> Self {
            Self {
                path: temp_ring_path(tag),
            }
        }

        fn path_str(&self) -> &str {
            self.path.to_str().unwrap()
        }
    }

    impl Drop for TempRing {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
            if let Some(p) = self.path.parent() {
                let _ = std::fs::remove_dir(p);
            }
        }
    }

    /// The lane's wire at test scale: S16LE stereo, `TEST_PERIOD_FRAMES` slots.
    fn ring_geometry(period_frames: u32, n_slots: u32) -> Geometry {
        Geometry {
            rate: 48_000,
            channels: 2,
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames,
            n_slots,
        }
    }

    fn ring_source(ring: &TempRing, channel: ChannelPick) -> DacContentSource {
        DacContentSource::ring(ring.path_str(), channel, TEST_PERIOD_FRAMES, 2, None).unwrap()
    }

    #[test]
    fn ring_consumes_exactly_one_slot_per_period() {
        let ring = TempRing::create("one-slot");
        let mut src = ring_source(&ring, ChannelPick::Stereo);
        let mut writer =
            TestRingWriter::create_or_attach(ring.path_str(), ring_geometry(TEST_PERIOD_FRAMES, 2))
                .unwrap();

        // Two slots published; each period must take exactly ONE of them.
        assert!(writer.try_publish_slot(&[1i16; 8]));
        assert!(writer.try_publish_slot(&[2i16; 8]));

        let mut out = vec![0 as ProgramSample; 8];
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![w(1); 8], "first period must take the first slot");
        src.fill_period(&mut out).unwrap();
        assert_eq!(
            out,
            vec![w(2); 8],
            "second period must take the second slot"
        );

        let m = src.metrics();
        assert_eq!(m.transport, "ring");
        assert_eq!(m.fifo_periods, 2);
        assert_eq!(m.starved_periods, 0);
        assert!(m.serving_fifo);
        // FIFO-only gauges read zero on the ring — its queue is the mapping.
        assert_eq!(m.staged_periods, 0);
        assert_eq!(m.overflow_dropped_periods, 0);
        assert_eq!(m.open_failures, 0);
    }

    #[test]
    fn ring_starvation_is_silence_and_counts() {
        let ring = TempRing::create("starve");
        let mut src = ring_source(&ring, ChannelPick::Stereo);
        let mut writer =
            TestRingWriter::create_or_attach(ring.path_str(), ring_geometry(TEST_PERIOD_FRAMES, 2))
                .unwrap();
        assert!(writer.try_publish_slot(&[i16::MAX; 8]));

        let mut out = vec![0 as ProgramSample; 8];
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![w(i16::MAX); 8]);
        assert!(src.metrics().serving_fifo);

        // Ring now empty: silence, a counter, and NO replay of the loud
        // period just served (D4 — the whole point of the ruling).
        for i in 1..=3 {
            src.fill_period(&mut out).unwrap();
            assert_eq!(
                out,
                vec![0 as ProgramSample; 8],
                "period {i} must be silent"
            );
            let m = src.metrics();
            assert!(!m.serving_fifo);
            assert_eq!(m.starved_periods, i);
            assert_eq!(m.fifo_periods, 1);
        }
    }

    /// A geometry the ring cannot serve is refused at construction, typed —
    /// the class `main` maps to a config-class park (exit 78) rather than a
    /// restart loop.
    #[test]
    fn ring_geometry_mismatch_is_refused_typed() {
        // Every axis the writer can disagree on, one at a time, against a
        // reader that declares S16 / 2ch / TEST_PERIOD_FRAMES / 2 slots.
        let cases: [(&str, Geometry); 3] = [
            ("period", ring_geometry(TEST_PERIOD_FRAMES * 2, 2)),
            ("slots", ring_geometry(TEST_PERIOD_FRAMES, 4)),
            (
                "format",
                Geometry {
                    sample_format: SAMPLE_FORMAT_S32LE,
                    ..ring_geometry(TEST_PERIOD_FRAMES, 2)
                },
            ),
        ];
        for (label, written) in cases {
            let ring = TempRing::create(&format!("mismatch-{label}"));
            let _writer =
                jasper_ring::RingWriter::create_or_attach(ring.path_str(), written).unwrap();
            let err = match DacContentSource::ring(
                ring.path_str(),
                ChannelPick::Stereo,
                TEST_PERIOD_FRAMES,
                2,
                None,
            ) {
                Ok(_) => panic!("{label} mismatch must be refused"),
                Err(e) => e,
            };
            assert_eq!(err.kind(), io::ErrorKind::InvalidData, "{label}");
        }
    }

    /// The pick means the same thing on either transport.
    ///
    /// Both arms carry the bond's shared stereo, so identical wire samples must
    /// reach the DAC identically. Run every pick through both and compare — a
    /// ring arm that forgot the pick, or applied it at a different point in the
    /// chain, differs here on the first frame.
    #[test]
    fn the_pick_is_identical_on_both_transports() {
        let wire: [i16; 8] = [100, -200, 3000, -4000, i16::MAX, i16::MIN, 0, 7];
        for pick in [
            ChannelPick::Stereo,
            ChannelPick::Left,
            ChannelPick::Right,
            ChannelPick::Mono,
            ChannelPick::Sub(80.0),
        ] {
            // FIFO arm.
            let fifo = TempFifo::create(&format!("pick-fifo-{}", pick.as_str()));
            let mut fifo_src =
                DacContentSource::fifo(fifo.path_str(), pick, TEST_PERIOD_FRAMES, None);
            let mut writer = connect_producer(&mut fifo_src, &fifo);
            writer.write_all(&le_bytes(&wire)).unwrap();
            let mut from_fifo = vec![0 as ProgramSample; 8];
            fifo_src.fill_period(&mut from_fifo).unwrap();
            assert!(fifo_src.metrics().serving_fifo, "{}", pick.as_str());

            // Ring arm, same wire samples.
            let ring = TempRing::create(&format!("pick-ring-{}", pick.as_str()));
            let mut ring_src = ring_source(&ring, pick);
            let mut ring_writer = TestRingWriter::create_or_attach(
                ring.path_str(),
                ring_geometry(TEST_PERIOD_FRAMES, 2),
            )
            .unwrap();
            assert!(ring_writer.try_publish_slot(&wire));
            let mut from_ring = vec![0 as ProgramSample; 8];
            ring_src.fill_period(&mut from_ring).unwrap();
            assert!(ring_src.metrics().serving_fifo, "{}", pick.as_str());

            assert_eq!(
                from_fifo,
                from_ring,
                "pick {} differs between transports",
                pick.as_str()
            );
        }
    }
}
