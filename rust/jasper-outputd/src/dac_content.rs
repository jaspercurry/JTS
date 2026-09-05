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
//! drop. `ChannelPick` therefore mirrors the channel-split vocabulary:
//! `left`/`right` duplicate that program channel onto both DAC channels;
//! `mono` averages (the clip-safe L+R sum at −6.02 dB, matching
//! `jasper.camilla_emit.MONO_SUM_GAIN_DB`); `stereo` is passthrough. Both
//! transports carry the same shared-stream format, so the pick is applied
//! identically on either.

use std::io;
use std::os::fd::RawFd;

use anyhow::Result;

use crate::shm_ring_source::ShmRingSource;
use crate::types::{ProgramSample, SampleFormat};

/// Bound on staged FIFO data, in periods. Caps the extra latency this
/// lane can accumulate if the producer briefly outpaces the DAC
/// (~170 ms at 1024-frame periods); overflow drops the OLDEST whole
/// periods so alignment is preserved and the lane stays current.
///
/// FIFO arm only — the ring's depth is its `n_slots`, a property of the
/// mapping both ends agreed on at attach.
pub const MAX_STAGED_PERIODS: usize = 8;

/// Which channel of the shared stereo program this speaker plays.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChannelPick {
    /// Passthrough — both program channels as-is (solo / lab use).
    Stereo,
    /// Program channel 0 duplicated to both DAC channels (a LEFT member).
    Left,
    /// Program channel 1 duplicated to both DAC channels (a RIGHT member).
    Right,
    /// Clip-safe average of both program channels (a mono member).
    Mono,
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
        }
    }

    /// Parse the channel-split vocabulary. Unknown values are a
    /// configuration error — fail loud at startup, never guess a channel
    /// (playing the WRONG channel is the silent failure class
    /// `check_grouping_channel_pick` exists for).
    pub fn parse(raw: &str) -> Result<Self, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "" | "stereo" => Ok(Self::Stereo),
            "left" => Ok(Self::Left),
            "right" => Ok(Self::Right),
            "mono" => Ok(Self::Mono),
            other => Err(format!(
                "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL must be one of \
                 stereo|left|right|mono, got {other:?}"
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
    fn apply(self, period: &mut [ProgramSample]) {
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
    served_periods: u64,
    starved_periods: u64,
    last_period_served: bool,
    logged_first_starvation: bool,
}

impl DacContentSource {
    /// The FIFO transport. No I/O here — see [`FifoReader::new`].
    pub fn fifo(path: &str, channel: ChannelPick, period_frames: u32) -> Self {
        let period_bytes = (period_frames as usize) * 2 /* channels */ * 2 /* bytes */;
        Self::with_reader(Reader::Fifo(FifoReader::new(path, period_bytes)), channel)
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
    ) -> io::Result<Self> {
        let ring = ShmRingSource::new(path, period_frames, 2, SampleFormat::S16Le, n_slots)?;
        Ok(Self::with_reader(Reader::Ring(ring), channel))
    }

    fn with_reader(reader: Reader, channel: ChannelPick) -> Self {
        Self {
            reader,
            channel,
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
        self.channel.apply(out);
        Ok(())
    }

    fn transport(&self) -> &'static str {
        match self.reader {
            Reader::Fifo(_) => "fifo",
            Reader::Ring(_) => "ring",
        }
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
        assert!(ChannelPick::parse("sub").is_err());
        assert!(ChannelPick::parse("both").is_err());
    }

    #[test]
    fn channel_pick_left_right_duplicate_and_mono_averages_clip_safe() {
        let mut p = wv(&[100, -200, 1000, 2000]);
        ChannelPick::Left.apply(&mut p);
        assert_eq!(p, wv(&[100, 100, 1000, 1000]));

        let mut p = wv(&[100, -200, 1000, 2000]);
        ChannelPick::Right.apply(&mut p);
        assert_eq!(p, wv(&[-200, -200, 2000, 2000]));

        let mut p = wv(&[100, -200, i16::MAX, i16::MAX]);
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p[0], w(-50));
        assert_eq!(p[1], w(-50));
        // Full-scale L==R averages back to full scale, no overflow.
        assert_eq!(p[2], w(i16::MAX));
        assert_eq!(p[3], w(i16::MAX));

        let mut p = wv(&[1, 2, 3, 4]);
        ChannelPick::Stereo.apply(&mut p);
        assert_eq!(p, wv(&[1, 2, 3, 4]));
    }

    #[test]
    fn mono_average_cannot_overflow_at_the_spine_rails() {
        // The i64 accumulator's reason to exist: two i32 samples sum past the
        // i32 rail. In i32 this wraps — a correlated full-scale pair would come
        // out full-scale OPPOSITE polarity, the loudest defect a mono fold can
        // produce. The old i16 version had the same argument one width down.
        let mut p = [ProgramSample::MAX, ProgramSample::MAX];
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p, [ProgramSample::MAX, ProgramSample::MAX]);

        let mut p = [ProgramSample::MIN, ProgramSample::MIN];
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p, [ProgramSample::MIN, ProgramSample::MIN]);

        // And the -6.02 dB sum of an anti-correlated full-scale pair is silence,
        // not a wrap to a rail.
        let mut p = [ProgramSample::MAX, ProgramSample::MIN];
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p, [0, 0]);
    }

    /// A starved period REPLACES whatever the caller left in the buffer.
    ///
    /// Unfiltered pick, so the lane's silence is exactly zeros: the caller's
    /// stale content must not survive, which is the D4 half that says an outage
    /// is silence rather than a replay.
    #[test]
    fn a_starved_period_replaces_the_callers_buffer_with_silence() {
        let fifo = TempFifo::create("starved-silence");
        let mut src =
            DacContentSource::fifo(fifo.path_str(), ChannelPick::Stereo, TEST_PERIOD_FRAMES);
        let mut out = vec![w(12_345); (TEST_PERIOD_FRAMES as usize) * 2];
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![0 as ProgramSample; 8]);
        let m = src.metrics();
        assert!(!m.serving_fifo);
        assert_eq!(m.starved_periods, 1);
        assert_eq!(m.fifo_periods, 0);
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
            DacContentSource::fifo(fifo.path_str(), ChannelPick::Left, TEST_PERIOD_FRAMES);
        let mut out = vec![0 as ProgramSample; 8];

        // No writer: silence, honest counters, no panic, no block.
        for _ in 0..3 {
            src.fill_period(&mut out).unwrap();
            assert!(out.iter().all(|&s| s == 0));
        }
        let m = src.metrics();
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
        let mut src =
            DacContentSource::fifo(fifo.path_str(), ChannelPick::Stereo, TEST_PERIOD_FRAMES);
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
        let mut src =
            DacContentSource::fifo(fifo.path_str(), ChannelPick::Stereo, TEST_PERIOD_FRAMES);
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
        DacContentSource::ring(ring.path_str(), channel, TEST_PERIOD_FRAMES, 2).unwrap()
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
        ] {
            // FIFO arm.
            let fifo = TempFifo::create(&format!("pick-fifo-{}", pick.as_str()));
            let mut fifo_src = DacContentSource::fifo(fifo.path_str(), pick, TEST_PERIOD_FRAMES);
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
