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
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChannelPick {
    /// Passthrough — both program channels as-is (solo / lab use).
    Stereo,
    /// Program channel 0 duplicated to both DAC channels (a LEFT member).
    Left,
    /// Program channel 1 duplicated to both DAC channels (a RIGHT member).
    Right,
    /// Clip-safe average of both program channels (a mono/sub member).
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
    /// configuration error — fail loud at startup, never guess a
    /// channel (playing the WRONG channel is the silent failure class
    /// `check_grouping_channel_pick` exists for).
    pub fn parse(raw: &str) -> Result<Self, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "" | "stereo" => Ok(Self::Stereo),
            "left" => Ok(Self::Left),
            "right" => Ok(Self::Right),
            "mono" | "sub" => Ok(Self::Mono),
            other => Err(format!(
                "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL must be one of \
                 stereo|left|right|mono|sub, got {other:?}"
            )),
        }
    }

    /// Apply the pick in place to one interleaved-stereo period.
    fn apply(self, period: &mut [i16]) {
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
                    let avg = (((frame[0] as i32) + (frame[1] as i32)) / 2) as i16;
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
        Self {
            path: path.to_string(),
            channel,
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
            self.channel.apply(out);
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
        assert_eq!(ChannelPick::parse("sub"), Ok(ChannelPick::Mono));
        assert!(ChannelPick::parse("both").is_err());
    }

    #[test]
    fn channel_pick_left_right_duplicate_and_mono_averages_clip_safe() {
        let mut p = [100i16, -200, 1000, 2000];
        ChannelPick::Left.apply(&mut p);
        assert_eq!(p, [100, 100, 1000, 1000]);

        let mut p = [100i16, -200, 1000, 2000];
        ChannelPick::Right.apply(&mut p);
        assert_eq!(p, [-200, -200, 2000, 2000]);

        let mut p = [100i16, -200, i16::MAX, i16::MAX];
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p[0], -50);
        assert_eq!(p[1], -50);
        // Full-scale L==R averages back to full scale, no overflow.
        assert_eq!(p[2], i16::MAX);
        assert_eq!(p[3], i16::MAX);

        let mut p = [1i16, 2, 3, 4];
        ChannelPick::Stereo.apply(&mut p);
        assert_eq!(p, [1, 2, 3, 4]);
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
            let c_path =
                std::ffi::CString::new(path.as_os_str().to_str().unwrap()).unwrap();
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
        debug_assert!(src.fd.is_some(), "read end must be open before the producer connects");
        std::fs::OpenOptions::new()
            .write(true)
            .open(&fifo.path)
            .expect("producer open on a primed FIFO must not block")
    }

    #[test]
    fn source_serves_direct_until_producer_demonstrates_health() {
        let fifo = TempFifo::create("damped");
        let mut src = DacContentSource::new(
            fifo.path_str(),
            ChannelPick::Stereo,
            TEST_PERIOD_FRAMES,
        );
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
        assert!(served >= 1, "FIFO never took over after demonstrated health");
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
        let mut src = DacContentSource::new(
            fifo.path_str(),
            ChannelPick::Left,
            TEST_PERIOD_FRAMES,
        );
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
        assert!(fell_back, "source kept claiming FIFO audio after writer death");
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
        assert!(recovered, "source never recovered after a new writer connected");
        // One real outage cycle: one transition, one recovery (the
        // initial engagement does not count).
        assert_eq!(src.metrics().recoveries, 1);
        assert_eq!(src.metrics().fallback_transitions, 1);
    }

    #[test]
    fn source_never_blocks_with_a_writer_that_sends_nothing() {
        let fifo = TempFifo::create("idle-writer");
        let mut src = DacContentSource::new(
            fifo.path_str(),
            ChannelPick::Stereo,
            TEST_PERIOD_FRAMES,
        );
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
