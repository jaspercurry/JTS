// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Optional DAC-content FIFO source — the multi-room round-trip lane
//! (Increment 3 of docs/HANDOFF-multiroom.md §2 "Canonical signal flow").
//!
//! On a grouping LEADER, the music the DAC plays must come back from the
//! sync engine (leader's localhost snapclient `--player file:<FIFO>`), so
//! the leader is sample-locked with its followers. snd-aloop substreams
//! are exhausted (8/8), so that lane is a raw-PCM FIFO, not a loopback —
//! which also dodges the documented snd_pcm_delay-lies-on-snd-aloop trap.
//! This module is the READER side: `DacContentSource` feeds the DAC loop
//! one period at a time from the FIFO.
//!
//! ## The two contracts this module exists to keep
//!
//! **Solo-impact contract:** the source is constructed only when
//! `JASPER_OUTPUTD_DAC_CONTENT_FIFO` is set. Unset ⇒ this module does not
//! run at all — no open, no syscalls, no per-period work; the DAC loop is
//! byte-identical to today.
//!
//! **inv-B (never-silent leader):** a starving FIFO must NOT silence the
//! leader's own music. `try_fill_period` returns `false` the moment a
//! full period is not available, and the caller (the DAC loop) reads the
//! DIRECT content PCM for that period instead — zero periods of silence,
//! at the cost of a bounded content jump (the direct path is ~one playout
//! buffer ahead of the round-trip; "a momentarily-unsynced pair beats a
//! silent leader"). Returning to the FIFO is DAMPED (`RECOVERY_*` below)
//! so a flapping writer cannot oscillate the DAC between two time-offset
//! copies of the program every other period. Health is self-reported on
//! the STATUS surface (`DacContentMetrics` → the `dac_content` block) —
//! daemon truth, never a Python mirror of env intent (the removed
//! `SNAPFIFO_PRODUCER_WIRED` lesson).
//!
//! ## Timing
//!
//! All FIFO I/O is non-blocking and happens on the DAC loop thread; the
//! DAC write remains the sole pacer (inv-1). Worst case per period is one
//! `open(2)` attempt (FIFO missing) or a few bounded `read(2)` calls —
//! never a blocking wait on the producer.
//!
//! ## Channel pick
//!
//! The FIFO carries the bond's SHARED stereo program (L = leader-seat
//! corrected, R = follower-seat corrected). A stereo-pair leader plays
//! only ITS channel, and — unlike a follower, whose snapclient plays
//! through an ALSA `ttable` plug — this lane has no ALSA hop to do the
//! drop. `ChannelPick` therefore mirrors the channel-split vocabulary
//! (docs/HANDOFF-multiroom.md §4): `left`/`right` duplicate that program
//! channel onto both DAC channels; `mono` averages (the clip-safe L+R sum
//! at −6.02 dB, matching `channel_split.py`); `stereo` is passthrough.
//!
//! **The pick applies to FIFO periods ONLY — a deliberate decision, not
//! an oversight.** The pick is a property of the shared-STREAM format
//! (which program channel this speaker takes from the bond's stereo
//! stream); inv-B fallback periods play the DIRECT content lane, which
//! carries this speaker's own already-correct local format. Increment 5
//! owns the contract for what feeds that lane on a bonded member; if it
//! ever feeds the shared-stream format there instead, the pick moves
//! with that decision.

use std::io;
use std::os::fd::RawFd;

/// Sample rate of the round-trip lane. Pinned to the SNAPFIFO stream
/// format (48000:16:2) — the FIFO never carries any other rate, so the
/// sub low-pass coefficients can be precomputed against this constant.
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

/// One 2nd-order IIR section (RBJ biquad), Direct Form I, f64 state.
///
/// Direct Form I keeps two input-history and two output-history taps;
/// it is the numerically-robust choice for a low-Q audio biquad and
/// makes the state continuity contract (a period boundary is just two
/// remembered samples, identical to processing one big buffer) obvious.
#[derive(Debug, Clone, Copy)]
struct Biquad {
    // Normalized coefficients (a0 folded out).
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
    // Direct Form I state: last two inputs and last two outputs.
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl Biquad {
    /// Low-pass section via the RBJ audio-EQ cookbook, parameterised by
    /// corner frequency and Q. An LR4 low-pass is two of these cascaded
    /// at Q = 1/sqrt(2) (Butterworth), giving a 4th-order, −24 dB/octave
    /// roll-off that is −6 dB at the corner per section (−12 dB summed at
    /// the LR crossover point, the LR4 signature).
    fn low_pass(corner_hz: f64, sample_rate_hz: f64, q: f64) -> Self {
        let w0 = 2.0 * std::f64::consts::PI * corner_hz / sample_rate_hz;
        let (sin_w0, cos_w0) = w0.sin_cos();
        let alpha = sin_w0 / (2.0 * q);

        let b1 = 1.0 - cos_w0;
        let b0 = b1 / 2.0;
        let b2 = b0;
        let a0 = 1.0 + alpha;
        let a1 = -2.0 * cos_w0;
        let a2 = 1.0 - alpha;

        Self {
            b0: b0 / a0,
            b1: b1 / a0,
            b2: b2 / a0,
            a1: a1 / a0,
            a2: a2 / a0,
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        }
    }

    /// Process one sample, advancing the Direct Form I state.
    #[inline]
    fn process(&mut self, x0: f64) -> f64 {
        let y0 = self.b0 * x0 + self.b1 * self.x1 + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x0;
        self.y2 = self.y1;
        self.y1 = y0;
        y0
    }
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
            s1: Biquad::low_pass(corner_hz, SUB_SAMPLE_RATE_HZ, LR4_SECTION_Q),
            s2: Biquad::low_pass(corner_hz, SUB_SAMPLE_RATE_HZ, LR4_SECTION_Q),
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
pub const MAX_STAGED_PERIODS: usize = 8;

/// Recovery hysteresis: how many periods must be staged for the FIFO to
/// count as "ready" again after a fallback…
pub const RECOVERY_READY_PERIODS: usize = 2;

/// …and for how many CONSECUTIVE DAC periods it must stay ready before
/// we switch back. Together ≈ 210 ms of demonstrated producer health at
/// 1024-frame periods — one clean transition out and one back per real
/// event, never per-period flapping between two time-offset copies.
pub const RECOVERY_STREAK_PERIODS: u32 = 10;

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
    /// i32 then truncate to i16 — the same −6.02 dB sum `Mono` uses, so a
    /// full-scale-correlated pair stays full scale with no overflow.
    #[inline]
    fn mono_avg(frame: &[i16]) -> i16 {
        (((frame[0] as i32) + (frame[1] as i32)) / 2) as i16
    }

    /// Apply the pick in place to one interleaved-stereo period.
    ///
    /// `sub_filter` carries the stateful LR4 low-pass and MUST be `Some`
    /// when `self` is `Sub` (the caller — `DacContentSource` — owns it so
    /// it persists across periods). It is unused for every other pick.
    fn apply(self, period: &mut [i16], sub_filter: Option<&mut Lr4LowPass>) {
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
                    // Saturate to i16 — the LP passband is unity and the
                    // input is already ≤ full scale, so this only guards
                    // the tiny biquad transient ripple at a step edge.
                    let s = lp.round().clamp(i16::MIN as f64, i16::MAX as f64) as i16;
                    frame[0] = s;
                    frame[1] = s;
                }
            }
        }
    }
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

    /// Pop one period into `out` (i16 interleaved). Returns false when a
    /// full period is not staged. `out.len() * 2 == period_bytes`.
    fn pop_period(&mut self, out: &mut [i16]) -> bool {
        debug_assert_eq!(out.len() * 2, self.period_bytes);
        if self.staging.len() < self.period_bytes {
            return false;
        }
        for (sample, bytes) in out
            .iter_mut()
            .zip(self.staging[..self.period_bytes].chunks_exact(2))
        {
            *sample = i16::from_le_bytes([bytes[0], bytes[1]]);
        }
        self.staging.drain(..self.period_bytes);
        true
    }
}

/// Pure fallback policy: WHICH source serves this period, with damped
/// recovery. Mode transitions are single events per real producer
/// outage, never per-period oscillation (see `RECOVERY_*`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Fifo,
    Fallback,
}

#[derive(Debug)]
struct FallbackPolicy {
    mode: Mode,
    ready_streak: u32,
    /// True once the FIFO has served at least once. The FIRST entry into
    /// `Mode::Fifo` is an ENGAGEMENT (nothing was lost), not a recovery —
    /// keeping `recoveries` == completed outage cycles, symmetric with
    /// `fallback_transitions` (operator clarity: recoveries can never
    /// exceed transitions).
    engaged: bool,
    fallback_transitions: u64,
    recoveries: u64,
}

impl FallbackPolicy {
    fn new() -> Self {
        // Start in Fallback: serve the direct path until the producer
        // DEMONSTRATES health (the same damped criterion as recovery).
        // A leader whose producer never starts therefore plays direct
        // from the first period — configured-but-dry is never silent.
        Self {
            mode: Mode::Fallback,
            ready_streak: 0,
            engaged: false,
            fallback_transitions: 0,
            recoveries: 0,
        }
    }

    /// Decide for one period given how many periods are staged.
    /// Returns true when the FIFO should serve this period.
    fn serve_from_fifo(&mut self, staged_periods: usize) -> bool {
        match self.mode {
            Mode::Fifo => {
                if staged_periods >= 1 {
                    true
                } else {
                    // Immediate fallback: zero periods of silence (inv-B).
                    self.mode = Mode::Fallback;
                    self.ready_streak = 0;
                    self.fallback_transitions += 1;
                    false
                }
            }
            Mode::Fallback => {
                if staged_periods >= RECOVERY_READY_PERIODS {
                    self.ready_streak += 1;
                    if self.ready_streak >= RECOVERY_STREAK_PERIODS {
                        self.mode = Mode::Fifo;
                        if self.engaged {
                            self.recoveries += 1;
                        }
                        self.engaged = true;
                        return true;
                    }
                } else {
                    self.ready_streak = 0;
                }
                false
            }
        }
    }
}

/// Counters + gauges for the STATUS `dac_content` block. Plain data —
/// `OutputdState::mark_dac_content` copies it into atomics.
#[derive(Debug, Clone, Copy, Default)]
pub struct DacContentMetrics {
    /// True when the FIFO is currently serving the DAC (false = the
    /// inv-B direct fallback is serving, including the never-started
    /// producer case).
    pub serving_fifo: bool,
    pub fifo_periods: u64,
    pub fallback_periods: u64,
    /// FIFO→fallback transitions (each is one real producer outage).
    pub fallback_transitions: u64,
    /// Damped fallback→FIFO recoveries.
    pub recoveries: u64,
    /// Periods currently staged (gauge; healthy steady state ≈ 1–2).
    pub staged_periods: u64,
    /// Oldest-period drops from staging overflow (producer outpacing
    /// the DAC — should stay 0 with a sane producer).
    pub overflow_dropped_periods: u64,
    pub open_failures: u64,
    pub read_failures: u64,
}

/// The DAC-content FIFO source. One instance per daemon, owned by the
/// DAC loop; all I/O non-blocking on that thread.
pub struct DacContentSource {
    path: String,
    channel: ChannelPick,
    /// Stateful LR4 low-pass for a `Sub` channel — `Some` iff
    /// `channel` is `ChannelPick::Sub`. Owned here (not on the Copy
    /// `ChannelPick`) so its biquad memory persists across periods;
    /// (re)construct in `new` resets it.
    sub_filter: Option<Lr4LowPass>,
    fd: Option<RawFd>,
    assembler: PeriodAssembler,
    policy: FallbackPolicy,
    read_buf: Vec<u8>,
    fifo_periods: u64,
    fallback_periods: u64,
    open_failures: u64,
    read_failures: u64,
    logged_first_fallback: bool,
}

impl DacContentSource {
    /// No I/O here — the FIFO is opened lazily on the first period so a
    /// not-yet-created path is a normal startup ordering, not an error.
    pub fn new(path: &str, channel: ChannelPick, period_frames: u32) -> Self {
        let period_bytes = (period_frames as usize) * 2 /* channels */ * 2 /* bytes */;
        // A Sub channel owns a fresh (state-cleared) low-pass at its
        // carried corner; every other pick has no filter.
        let sub_filter = match channel {
            ChannelPick::Sub(corner_hz) => Some(Lr4LowPass::new(corner_hz)),
            _ => None,
        };
        Self {
            path: path.to_string(),
            channel,
            sub_filter,
            fd: None,
            assembler: PeriodAssembler::new(period_bytes),
            policy: FallbackPolicy::new(),
            read_buf: vec![0u8; period_bytes],
            fifo_periods: 0,
            fallback_periods: 0,
            open_failures: 0,
            read_failures: 0,
            logged_first_fallback: false,
        }
    }

    /// Try to serve one period from the FIFO into `out`. Returns true
    /// when `out` now holds round-trip audio; false means the caller
    /// must fill `out` from the DIRECT content path for this period
    /// (inv-B — never silence). Never blocks.
    pub fn try_fill_period(&mut self, out: &mut [i16]) -> bool {
        self.open_if_needed();
        self.drain_available();

        let was_fallback = self.policy.mode == Mode::Fallback;
        if self.policy.serve_from_fifo(self.assembler.staged_periods()) {
            let popped = self.assembler.pop_period(out);
            if !popped {
                // Structurally impossible (the policy only grants a serve
                // when >=1 period is staged), but on a reboot-on-fail
                // daemon an invariant break must degrade to a clean
                // direct-path period — never a stale-buffer glitch.
                debug_assert!(false, "policy granted FIFO serve without a staged period");
                eprintln!(
                    "event=outputd.dac_content.pop_underrun fifo={} action=serve_direct_content",
                    self.path,
                );
                self.fallback_periods += 1;
                return false;
            }
            if was_fallback {
                eprintln!(
                    "event=outputd.dac_content.{} fifo={} staged_periods={}",
                    if self.policy.recoveries == 0 {
                        "engaged"
                    } else {
                        "recovered"
                    },
                    self.path,
                    self.assembler.staged_periods(),
                );
            }
            self.channel.apply(out, self.sub_filter.as_mut());
            self.fifo_periods += 1;
            true
        } else {
            if !was_fallback {
                // A real FIFO→fallback transition. Log the first one
                // unconditionally; afterwards transitions stay visible
                // via the STATUS counters (recovery is damped, so a
                // flapping producer cannot spam the journal).
                if !self.logged_first_fallback {
                    eprintln!(
                        "event=outputd.dac_content.fallback reason=fifo_starved fifo={} \
                         action=serve_direct_content detail=inv-B: leader keeps playing \
                         the direct path; see HANDOFF-multiroom.md §2",
                        self.path,
                    );
                    self.logged_first_fallback = true;
                }
            }
            self.fallback_periods += 1;
            false
        }
    }

    /// Apply this source's channel pick to a period the caller filled
    /// from the DIRECT (inv-B fallback) content lane, sharing the SAME
    /// stateful filter as the FIFO path so a starvation transition is
    /// continuous.
    ///
    /// For most picks the direct lane already carries this speaker's own
    /// correct format, so the pick is a FIFO-only property and this is a
    /// no-op (matches the module-level "pick applies to FIFO periods
    /// ONLY" decision). But a `Sub` member MUST NEVER play full-range —
    /// the dumb-sub lane carries the bond's full-range stereo on the
    /// direct path too — so for `Sub` this collapses to the clip-safe
    /// mono sum and runs the LR4 low-pass, exactly as the FIFO path does.
    /// The DAC loop calls this on every fallback period; only `Sub`
    /// changes the buffer.
    pub fn apply_pick_to_fallback_period(&mut self, period: &mut [i16]) {
        if let ChannelPick::Sub(_) = self.channel {
            self.channel.apply(period, self.sub_filter.as_mut());
        }
    }

    pub fn metrics(&self) -> DacContentMetrics {
        DacContentMetrics {
            serving_fifo: self.policy.mode == Mode::Fifo,
            fifo_periods: self.fifo_periods,
            fallback_periods: self.fallback_periods,
            fallback_transitions: self.policy.fallback_transitions,
            recoveries: self.policy.recoveries,
            staged_periods: self.assembler.staged_periods() as u64,
            overflow_dropped_periods: self.assembler.overflow_dropped_periods,
            open_failures: self.open_failures,
            read_failures: self.read_failures,
        }
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
            eprintln!(
                "event=outputd.dac_content.opened fifo={} channel={}",
                self.path,
                self.channel.as_str(),
            );
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

impl Drop for DacContentSource {
    fn drop(&mut self) {
        if let Some(fd) = self.fd.take() {
            unsafe { libc::close(fd) };
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

        let mut out = [0i16; 4];
        assert!(a.pop_period(&mut out));
        assert_eq!(out, [100, -100, 2000, -2000]);
        assert!(a.pop_period(&mut out));
        assert_eq!(out, [7, 8, 9, 10]);
        assert!(!a.pop_period(&mut out)); // drained
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
        let mut out = [0i16; 4];
        assert!(a.pop_period(&mut out));
        assert_eq!(out, [2, 2, 2, 2]); // periods 0 and 1 were dropped
    }

    // ---------- pure: FallbackPolicy ----------

    #[test]
    fn policy_starts_in_fallback_and_needs_damped_health_to_serve() {
        let mut p = FallbackPolicy::new();
        // Dry producer: stays in fallback forever, no transition churn.
        for _ in 0..100 {
            assert!(!p.serve_from_fifo(0));
        }
        assert_eq!(p.fallback_transitions, 0);
        // Producer appears: must stay ready RECOVERY_STREAK_PERIODS long.
        for i in 0..(RECOVERY_STREAK_PERIODS - 1) {
            assert!(!p.serve_from_fifo(RECOVERY_READY_PERIODS), "period {i}");
        }
        assert!(p.serve_from_fifo(RECOVERY_READY_PERIODS));
        // The FIRST take-over is an ENGAGEMENT, not a recovery: nothing
        // was lost, so recoveries stays 0 (and can never exceed
        // fallback_transitions — the operator-clarity invariant).
        assert_eq!(p.recoveries, 0);
        assert_eq!(p.fallback_transitions, 0);
    }

    #[test]
    fn policy_falls_back_immediately_on_starvation_never_silence() {
        let mut p = FallbackPolicy::new();
        for _ in 0..RECOVERY_STREAK_PERIODS {
            p.serve_from_fifo(RECOVERY_READY_PERIODS);
        }
        assert!(p.serve_from_fifo(1)); // serving from FIFO
                                       // The very period the FIFO is dry, fall back (no silence gap).
        assert!(!p.serve_from_fifo(0));
        assert_eq!(p.fallback_transitions, 1);
    }

    #[test]
    fn policy_recovery_streak_resets_on_flap() {
        let mut p = FallbackPolicy::new();
        // Almost recover, then flap: streak must reset (damping).
        for _ in 0..(RECOVERY_STREAK_PERIODS - 1) {
            p.serve_from_fifo(RECOVERY_READY_PERIODS);
        }
        assert!(!p.serve_from_fifo(0)); // flap: not ready
        for i in 0..(RECOVERY_STREAK_PERIODS - 1) {
            assert!(!p.serve_from_fifo(RECOVERY_READY_PERIODS), "period {i}");
        }
        assert!(p.serve_from_fifo(RECOVERY_READY_PERIODS));
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
        let mut p = [100i16, -200, 1000, 2000];
        ChannelPick::Left.apply(&mut p, None);
        assert_eq!(p, [100, 100, 1000, 1000]);

        let mut p = [100i16, -200, 1000, 2000];
        ChannelPick::Right.apply(&mut p, None);
        assert_eq!(p, [-200, -200, 2000, 2000]);

        let mut p = [100i16, -200, i16::MAX, i16::MAX];
        ChannelPick::Mono.apply(&mut p, None);
        assert_eq!(p[0], -50);
        assert_eq!(p[1], -50);
        // Full-scale L==R averages back to full scale, no overflow.
        assert_eq!(p[2], i16::MAX);
        assert_eq!(p[3], i16::MAX);

        let mut p = [1i16, 2, 3, 4];
        ChannelPick::Stereo.apply(&mut p, None);
        assert_eq!(p, [1, 2, 3, 4]);
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

    // ---------- pure: ChannelPick::Sub apply ----------

    /// Run a Sub apply over `frames` frames of a steady stereo sine and
    /// return the per-output-channel sample buffers (ch0, ch1).
    fn sub_apply_run(corner_hz: f64, freq: f64, frames: usize) -> (Vec<i16>, Vec<i16>) {
        let mut filter = Lr4LowPass::new(corner_hz);
        let pick = ChannelPick::Sub(corner_hz);
        let amp = 10_000.0;
        let mut ch0 = Vec::with_capacity(frames);
        let mut ch1 = Vec::with_capacity(frames);
        for i in 0..frames {
            let t = i as f64 / SUB_SAMPLE_RATE_HZ;
            let s = (amp * (2.0 * std::f64::consts::PI * freq * t).sin()) as i16;
            // L == R so the clip-safe mono sum is the input amplitude.
            let mut period = [s, s];
            pick.apply(&mut period, Some(&mut filter));
            ch0.push(period[0]);
            ch1.push(period[1]);
        }
        (ch0, ch1)
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
        let peak = |v: &[i16]| v[2_000..].iter().map(|s| s.unsigned_abs()).max().unwrap();
        assert!(peak(&hi) < 200, "4 kHz leaked: peak {}", peak(&hi));
        assert!(
            peak(&lo) > 5_000,
            "40 Hz wrongly attenuated: peak {}",
            peak(&lo)
        );
    }

    #[test]
    fn sub_apply_full_scale_input_does_not_overflow_i16() {
        // Full-scale DC on both channels (mono sum = full scale). The LP
        // settles to full scale; the saturating cast must not wrap.
        let mut filter = Lr4LowPass::new(80.0);
        let pick = ChannelPick::Sub(80.0);
        let mut last = [0i16; 2];
        for _ in 0..48_000 {
            let mut period = [i16::MAX, i16::MAX];
            pick.apply(&mut period, Some(&mut filter));
            last = period;
        }
        // Settled near full scale, never wrapped to a negative value.
        assert!(
            last[0] > i16::MAX - 4,
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
            let mut period = [i16::MAX, i16::MAX];
            pick.apply(&mut period, Some(&mut filter));
            // A positive step can never legitimately produce a negative
            // output here; a negative value would be an integer wrap.
            assert!(period[0] >= 0, "full-scale step wrapped to {}", period[0]);
            if period[0] == i16::MAX {
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
        let sample = |i: usize| -> i16 {
            let t = i as f64 / SUB_SAMPLE_RATE_HZ;
            (amp * (2.0 * std::f64::consts::PI * freq * t).sin()) as i16
        };

        // One big buffer.
        let mut big_filter = Lr4LowPass::new(corner);
        let pick = ChannelPick::Sub(corner);
        let mut big = vec![0i16; total * 2];
        for i in 0..total {
            big[2 * i] = sample(i);
            big[2 * i + 1] = sample(i);
        }
        pick.apply(&mut big, Some(&mut big_filter));

        // Two halves through the same persistent filter.
        let mut split_filter = Lr4LowPass::new(corner);
        let half = total / 2;
        let mut a = vec![0i16; half * 2];
        let mut b = vec![0i16; half * 2];
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
            let mut p = [i16::MAX, i16::MAX, 1234, 1234];
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
        let src = DacContentSource::new(
            fifo.path_str(),
            ChannelPick::Sub(SUB_DEFAULT_CORNER_HZ),
            TEST_PERIOD_FRAMES,
        );
        assert!(
            src.sub_filter.is_some(),
            "Sub source must own a low-pass filter"
        );
        assert_eq!(src.sub_filter.unwrap().corner_hz(), 80.0);
    }

    #[test]
    fn source_sub_fallback_period_is_low_passed_never_full_range() {
        // inv-B fallback periods for a Sub member must also be collapsed
        // to mono + low-passed — the dumb-sub lane carries full-range
        // stereo on the direct path too, and a sub must NEVER play it.
        let fifo = TempFifo::create("sub-fallback");
        let mut src =
            DacContentSource::new(fifo.path_str(), ChannelPick::Sub(80.0), TEST_PERIOD_FRAMES);
        // A burst of high-frequency-ish full-scale content on the direct
        // (fallback) lane: alternating +/- full scale ≈ Nyquist content,
        // which the sub LP must crush.
        let mut total_peak = 0i32;
        for blk in 0..200 {
            let mut period = vec![0i16; (TEST_PERIOD_FRAMES as usize) * 2];
            for (i, s) in period.iter_mut().enumerate() {
                *s = if (blk + i) % 2 == 0 {
                    i16::MAX
                } else {
                    i16::MIN
                };
            }
            src.apply_pick_to_fallback_period(&mut period);
            // Both channels identical (mono).
            for frame in period.chunks_exact(2) {
                assert_eq!(frame[0], frame[1]);
            }
            if blk >= 50 {
                for &s in &period {
                    total_peak = total_peak.max((s as i32).abs());
                }
            }
        }
        assert!(
            total_peak < 1_000,
            "sub fallback leaked near-Nyquist content: peak {total_peak}"
        );
    }

    #[test]
    fn source_non_sub_fallback_period_is_untouched() {
        // For non-Sub picks the direct lane already carries the correct
        // local format, so the fallback helper is a no-op (the module's
        // "pick applies to FIFO periods ONLY" decision).
        let fifo = TempFifo::create("left-fallback");
        let mut src = DacContentSource::new(fifo.path_str(), ChannelPick::Left, TEST_PERIOD_FRAMES);
        let original = vec![10i16, 20, 30, 40, 50, 60, 70, 80];
        let mut period = original.clone();
        src.apply_pick_to_fallback_period(&mut period);
        assert_eq!(period, original, "non-Sub fallback must be untouched");
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
        let mut out = vec![0i16; (TEST_PERIOD_FRAMES as usize) * 2];
        // Prime the source's read end (a fallback period, no writer yet).
        let _ = src.try_fill_period(&mut out);
        debug_assert!(
            src.fd.is_some(),
            "read end must be open before the producer connects"
        );
        std::fs::OpenOptions::new()
            .write(true)
            .open(&fifo.path)
            .expect("producer open on a primed FIFO must not block")
    }

    #[test]
    fn source_serves_direct_until_producer_demonstrates_health() {
        let fifo = TempFifo::create("damped");
        let mut src =
            DacContentSource::new(fifo.path_str(), ChannelPick::Stereo, TEST_PERIOD_FRAMES);
        let mut out = vec![0i16; 8];

        // No writer: every period is served direct (inv-B), no panic,
        // no block, metrics honest.
        for _ in 0..3 {
            assert!(!src.try_fill_period(&mut out));
        }
        let m = src.metrics();
        assert!(!m.serving_fifo);
        assert_eq!(m.fallback_periods, 3);
        assert_eq!(m.fifo_periods, 0);

        // Producer connects and stays ahead: after the damped streak the
        // FIFO takes over.
        let mut writer = std::fs::OpenOptions::new()
            .write(true)
            .open(&fifo.path)
            .unwrap();
        let one_period = le_bytes(&[7i16; 8]);
        // Pre-fill enough for the whole recovery streak plus the served
        // periods that follow.
        for _ in 0..(RECOVERY_STREAK_PERIODS as usize + 4) {
            writer.write_all(&one_period).unwrap();
        }

        let mut served = 0;
        for _ in 0..(RECOVERY_STREAK_PERIODS as usize + 2) {
            if src.try_fill_period(&mut out) {
                served += 1;
                assert_eq!(out, vec![7i16; 8]);
            }
        }
        assert!(
            served >= 1,
            "FIFO never took over after demonstrated health"
        );
        let m = src.metrics();
        assert!(m.serving_fifo);
        // First take-over = engagement, not a recovery (no outage yet).
        assert_eq!(m.recoveries, 0);
        assert_eq!(m.fallback_transitions, 0);
        assert_eq!(m.open_failures, 0);
    }

    #[test]
    fn source_missing_fifo_path_counts_open_failures_and_serves_direct() {
        let path = temp_fifo_path("missing"); // never mkfifo'd
        let mut src = DacContentSource::new(
            path.to_str().unwrap(),
            ChannelPick::Stereo,
            TEST_PERIOD_FRAMES,
        );
        let mut out = vec![0i16; 8];
        for _ in 0..3 {
            assert!(!src.try_fill_period(&mut out));
        }
        let m = src.metrics();
        assert_eq!(m.open_failures, 3); // one retry per period, cheap
        assert!(!m.serving_fifo);
    }

    #[test]
    fn source_falls_back_immediately_when_writer_stops_then_recovers() {
        let fifo = TempFifo::create("outage");
        let mut src = DacContentSource::new(fifo.path_str(), ChannelPick::Left, TEST_PERIOD_FRAMES);
        let mut out = vec![0i16; 8];
        let one_period = le_bytes(&[3i16, -3, 3, -3, 3, -3, 3, -3]);

        // Healthy producer long enough to take over.
        let mut writer = connect_producer(&mut src, &fifo);
        for _ in 0..(RECOVERY_STREAK_PERIODS as usize + 4) {
            writer.write_all(&one_period).unwrap();
        }
        let mut took_over = false;
        for _ in 0..(RECOVERY_STREAK_PERIODS as usize + 2) {
            if src.try_fill_period(&mut out) {
                took_over = true;
                // ChannelPick::Left duplicated ch0 onto both channels.
                assert_eq!(out, vec![3i16; 8]);
            }
        }
        assert!(took_over);

        // Producer dies: serve every still-buffered period (staging PLUS
        // whatever the kernel FIFO held when the write end closed — EOF
        // arrives only after those are drained), then the next dry period
        // falls back — never silence, exactly one transition. The bound
        // generously exceeds the most the producer ever wrote, so the
        // assertion can't be brittle to drain/pop interleaving.
        drop(writer);
        let drain_bound = (RECOVERY_STREAK_PERIODS as usize + 4) + MAX_STAGED_PERIODS + 8;
        let mut fell_back = false;
        for _ in 0..drain_bound {
            if !src.try_fill_period(&mut out) {
                fell_back = true;
                break;
            }
        }
        assert!(
            fell_back,
            "source kept claiming FIFO audio after writer death"
        );
        let m = src.metrics();
        assert!(!m.serving_fifo);
        assert_eq!(m.fallback_transitions, 1);

        // New writer: damped recovery works again on the SAME fd (the
        // source kept its read fd open across the producer's death, so
        // the helper's prime is a no-op reopen — it does not churn fd).
        let mut writer = connect_producer(&mut src, &fifo);
        for _ in 0..(RECOVERY_STREAK_PERIODS as usize + 4) {
            writer.write_all(&one_period).unwrap();
        }
        let mut recovered = false;
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            if src.try_fill_period(&mut out) {
                recovered = true;
                break;
            }
        }
        assert!(
            recovered,
            "source never recovered after a new writer connected"
        );
        // One real outage cycle: one transition, one recovery (the
        // initial engagement does not count).
        assert_eq!(src.metrics().recoveries, 1);
        assert_eq!(src.metrics().fallback_transitions, 1);
    }

    #[test]
    fn source_never_blocks_with_a_writer_that_sends_nothing() {
        let fifo = TempFifo::create("idle-writer");
        let mut src =
            DacContentSource::new(fifo.path_str(), ChannelPick::Stereo, TEST_PERIOD_FRAMES);
        // Writer connected but silent: reads must be EAGAIN, not a hang.
        let _writer = connect_producer(&mut src, &fifo);
        let mut out = vec![0i16; 8];
        let start = Instant::now();
        for _ in 0..10 {
            assert!(!src.try_fill_period(&mut out));
        }
        assert!(
            start.elapsed() < Duration::from_millis(200),
            "non-blocking contract violated: {:?}",
            start.elapsed()
        );
        assert_eq!(src.metrics().read_failures, 0);
    }
}
