// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! JTS Ring B — SPSC ping-pong SHM frame ring (reader + shared header/seq logic).
//!
//! # What this is
//!
//! The Ring B prototype replaces the outputd *content* snd-aloop hop
//! (CamillaDSP playback -> outputd content) — a free-running ~1536-frame
//! (~32 ms) loopback buffer — with a bounded N-slot ping-pong ring in shared
//! memory. CamillaDSP writes into the ring through a custom ALSA ioplug
//! (`c/jts-ring-ioplug/`, the WRITER, C); `jasper-outputd` reads one slot per
//! DAC period (the READER, this crate) with empty->silence semantics. The DAC
//! blocking write is the pacer; the reader never blocks on the ring.
//!
//! This crate owns the READER and the *shared* header/seq/geometry logic. The
//! golden-layout test ([`layout::tests`]) pins every header offset against the
//! constants the C header (`c/jts-ring-ioplug/jts_ring_shm.h`) `_Static_assert`s
//! — the cross-language drift guard. The golden test asserts numeric offsets
//! directly, so it runs standalone: this crate compiles and passes with no
//! dependency on the C side being present. The C writer half + that header live
//! in the `c/jts-ring-ioplug/` ring-consumers change stacked alongside this
//! crate; the `c/jts-ring-ioplug/*` cross-references throughout this crate point
//! at it (and this crate is inert until a ring lab flag is armed regardless).
//!
//! **This is a prototype and flag-gated everywhere.** Nothing here runs in a
//! product path unless a lab flag is set: `JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`
//! for the outputd (Ring B) reader (default `direct`), or
//! `JASPER_FANIN_CAMILLA_COUPLING=shm_ring` for the fan-in (Ring A) writer
//! ([`RingWriter`], called from `jasper-fanin`'s mixer only under
//! `Coupling::ShmRing`; default `loopback`). With both flags unset the crate is
//! compiled-in but inert.
//!
//! # SHM contract v1 (`/dev/shm/jts-ring/content.ring`)
//!
//! One file per ring, under `/dev/shm/jts-ring/` — deliberately NOT under a
//! systemd `RuntimeDirectory` (the design review killed that placement): the
//! file must survive `jasper-outputd` restarts. tmpfs means it is recreated
//! after a reboot by whichever side opens first.
//!
//! `file_size = HEADER_BYTES + n_slots * slot_bytes`,
//! `slot_bytes = period_frames * channels * bytes_per_sample`. Validated with
//! `fstat` before use.
//!
//! ## Header ([`HEADER_BYTES`] = 128, all fields little-endian, 8-byte aligned)
//!
//! | offset | field | type | semantics |
//! |---|---|---|---|
//! | 0  | magic | u32 | [`MAGIC`] `0x4A52494E` ("JRIN" LE). Written LAST during init, Release. Attach validity gate. |
//! | 4  | version | u32 | [`VERSION`] = 1 |
//! | 8  | rate | u32 | 48000 |
//! | 12 | channels | u32 | 2 |
//! | 16 | sample_format | u32 | 1 = S16LE ([`SAMPLE_FORMAT_S16LE`]). 2 = S32LE reserved. |
//! | 20 | period_frames | u32 | frames per slot |
//! | 24 | n_slots | u32 | 2 (min ping-pong)..=16 ([`MAX_N_SLOTS`]); 16 is the validated camilla geometry |
//! | 28 | _pad | u32 | zero |
//! | 32 | writer_epoch | atomic u64 | ++ (Release) per writer attach; reader counts `epoch_resets` on change |
//! | 40 | write_seq | atomic u64 | total slots PUBLISHED, monotonic across epochs for the file's lifetime |
//! | 48 | read_seq | atomic u64 | total slots CONSUMED; reader owns it WHILE LIVE — the writer may advance it only on the no-live-reader free-run path (see below) |
//! | 56 | writer_pid | atomic u64 | 0 = detached; set on attach, cleared on clean close |
//! | 64 | writer_heartbeat_ns | atomic u64 | CLOCK_MONOTONIC ns, relaxed store per publish/wait tick |
//! | 72 | reader_pid | atomic u64 | 0 = detached |
//! | 80 | reader_heartbeat_ns | atomic u64 | relaxed store once per DAC period (even on empty reads) |
//! | 88 | futex_word | u32 (reserved) | zero in v1 (productization note below) |
//! | 92..127 | reserved | bytes | zero |
//! | 128 | slots[0..n_slots] | payload | slot i at `128 + i*slot_bytes` |
//!
//! ## Ownership & transfer discipline (SPSC ping-pong)
//!
//! `slot_index(seq) = seq % n_slots`. Invariant:
//! `read_seq <= write_seq <= read_seq + n_slots`.
//!
//! **Writer publish** (slot `W = write_seq`; implemented in both the C writer
//! (`jts_ring_writer_publish`) and this crate's Rust [`writer::RingWriter`],
//! which mirror each other op-for-op so the two are interchangeable across the
//! SPSC boundary; documented here because the ordering is the shared contract):
//! 1. `R = load(read_seq, Acquire)`; require `W - R < n_slots` (space). The
//!    Acquire pairs with the reader's Release of `read_seq`, so the writer's
//!    payload stores into slot `W % n_slots` cannot be reordered before the
//!    reader has finished copying that slot out.
//! 2. memcpy payload into slot `W % n_slots` (plain stores).
//! 3. `store(write_seq, W+1, Release)` — publishes: a reader whose Acquire load
//!    observes `write_seq > W` observes the complete slot payload.
//!
//! **Writer free-run (no live reader).** When step 1 finds the ring full AND the
//! reader is heartbeat-dead (`reader_pid == 0` or heartbeat older than
//! [`WRITER_LIVENESS_TIMEOUT_NS`]), the writer drops the OLDEST slot: it
//! `store(read_seq, R+1, Release)` on the absent reader's behalf, then publishes
//! over the freed lap. This is the ONLY path on which the writer touches
//! `read_seq`, and it keeps occupancy bounded so a readerless ring cannot wedge
//! the writer (see the ioplug's dual-mode `avail` in `pcm_jts_ring.c`). It is
//! why the "read_seq written only by the reader" statement is qualified above —
//! the reader owns `read_seq` while live; the writer borrows it only when no live
//! reader exists.
//!
//! **Reader consume** (once per DAC period, NEVER blocks — [`RingReader::try_consume_slot`]):
//! 1. `W = load(write_seq, Acquire)`; `R = local read_seq`.
//! 2. `W == R` -> [`SlotRead::Empty`] (caller emits silence).
//! 3. `W - R > n_slots` (defensive; unreachable with a correct writer) ->
//!    `R = W`, `reader_resyncs++`.
//! 4. copy slot `R % n_slots` out (plain loads — safe: the Acquire on
//!    `write_seq` ordered the payload before this read).
//! 5. `store(read_seq, R+1, Release)` — releases the slot: the copy-out cannot
//!    be reordered after the writer sees the slot free.
//!
//! **Torn-write safety:** while a reader is LIVE, a slot is only ever touched by
//! one side at a time (writer needs `W - R < n_slots`; reader needs `W > R`) and
//! the writer never touches `read_seq`, so the two-sided discipline is exact. A
//! writer crash mid-memcpy leaves `write_seq` unbumped — the garbage slot is
//! never readable. Every cooperating C or Rust opener first takes the persistent
//! adjacent `<ring path>.open.lock` transaction flock (0660, bounded 500 ms
//! acquisition). It holds that lock across existing-inode classification, the
//! O_EXCL creator's `ftruncate` + magic-last publish, conditional torn-inode
//! reclaim, and a final fd-versus-linked-path identity proof. Lock contention
//! times out without touching the ring. Only while holding the lock may an
//! opener's <=100 ms size+magic budget classify a magic-invalid inode as
//! crashed mid-init and unlink/recreate it under the narrow owned
//! `/dev/shm/jts-ring/` path. This prevents a stale reclaimer from deleting a
//! replacement another opener already initialized.
//!
//! There is ONE narrow window where writer and reader may store `read_seq`
//! concurrently: a reader whose heartbeat has gone stale (wedged > liveness
//! timeout) but which then resumes. During the stall the writer took the free-run
//! path and advanced `read_seq`; if the reader wakes in the exact window where
//! its stale local `read_seq` mirror satisfies `W - R_local == n_slots` (so its
//! defensive `W - R_local > n_slots` resync does NOT fire) while the writer is
//! mid-memcpy of that same slot index, the reader can copy out one torn 128-frame
//! slot. This is bounded (at most one slot), self-healing (the next period's
//! Acquire load of `write_seq` re-establishes ordering, and a real drift trips
//! the `> n_slots` resync), and acceptable for the prototype — but it means the
//! planned futex productization, which builds `FUTEX_WAKE` semantics directly on
//! `read_seq`, must account for the writer as a possible `read_seq` writer on the
//! no-live-reader path, not assume reader-exclusive ownership.
//!
//! ## Productization note (why `futex_word` is reserved, not used)
//!
//! In v1 the writer polls (clamped nanosleep) when the ring is full; the
//! reader never blocks. Productization replaces the writer's poll with a
//! 32-bit `FUTEX_WAIT` on `futex_word` that the reader `FUTEX_WAKE`s after
//! advancing `read_seq`. The seqs are u64 and futexes are 32-bit, so the
//! separate `futex_word` is reserved *now* to keep the header layout stable
//! across that change. The reader half of that (bump + wake) is out of scope
//! for the prototype — outputd's reader is the pacer's slave and does not need
//! to wake anyone.
//!
//! ## The eight questions (design answers)
//!
//! 1. **What breaks if the writer dies?** `write_seq` stops advancing; the
//!    reader sees [`SlotRead::Empty`] every period and emits silence
//!    (`empty_reads++`). `writer_pid`/`writer_heartbeat_ns` go stale so
//!    `/state` reports `writer_alive:false`. No crash, no wedge.
//! 2. **What breaks if the reader dies?** `read_seq` stops advancing; the
//!    writer's space check fails and — because `reader_heartbeat_ns` is stale —
//!    it free-runs and drops frames instead of blocking (writer side). The ring
//!    file survives (tmpfs, not RuntimeDirectory), so a restarted reader
//!    reattaches and resyncs `read_seq = write_seq`.
//! 3. **What's the steady-state latency?** `<= n_slots * period_frames` frames
//!    of buffering (2*128 = 256 frames ~= 5.3 ms at 48 kHz).
//! 4. **How is it observable?** [`RingMetrics`] -> outputd `/state.shm_ring`:
//!    occupancy, empty_reads (startup vs steady split), epoch_resets,
//!    reader_resyncs, writer_alive, frames_read.
//! 5. **How does it fail closed?** Geometry/version/format mismatch on attach
//!    is a hard error (the reader surfaces it as a config-class startup
//!    failure so systemd parks, not reboot-loops). A magic-invalid owned file
//!    is unlinked and recreated. A transient empty ring is silence, never a
//!    crash.
//! 6. **Is it default-off?** Yes — no caller exists unless the flag is set.
//! 7. **What's the memory-ordering argument?** Acquire/Release on the two seqs
//!    (documented per step above); C11 `atomic_*_explicit` and Rust
//!    `AtomicU64` both lower to aarch64 `ldar`/`stlr`. Golden-layout test pins
//!    the offsets so both sides read the same bytes.
//! 8. **What's the productization delta?** The writer's poll becomes a futex
//!    wait (reserved word already in the header); the reader gains a
//!    wake-after-advance; the lab asound drop-in becomes a reconciler-owned
//!    device. No header change.

use std::io;
use std::os::fd::RawFd;
use std::os::unix::fs::MetadataExt;
use std::sync::atomic::{AtomicU64, Ordering};

pub mod layout;
pub mod writer;

pub use layout::{
    Geometry, HEADER_BYTES, MAGIC, MAX_N_SLOTS, MIN_N_SLOTS, SAMPLE_FORMAT_S16LE,
    SAMPLE_FORMAT_S32LE, VERSION,
};
pub use writer::{PublishOutcome, RingWriter, WriterMetrics, MAX_FULL_WAIT_TICKS};

/// Result of a single non-blocking [`RingReader::try_consume_slot`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlotRead {
    /// A full slot was copied into the caller's output buffer.
    Filled,
    /// The ring was empty this period; the caller must emit silence.
    Empty,
}

/// Snapshot of the reader-side counters for `/state.shm_ring`.
///
/// Mirrors the shape [`crate::layout`] pins: `occupancy = write_seq - read_seq`
/// is derived, the rest are reader-owned running counts. Writer-side counters
/// (published_slots, drop_no_reader) live in the writer (the bench prints them,
/// the ioplug logs them at close) and are read from the header where the reader
/// needs them (`writer_pid`, `writer_heartbeat_ns`).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RingMetrics {
    /// The ring is attached and readable.
    pub attached: bool,
    /// `write_seq - read_seq` at the last read (0..=n_slots).
    pub occupancy: u64,
    /// Total slots the reader has consumed (== `read_seq`).
    pub frames_read_slots: u64,
    /// Slots-worth of frames the reader has consumed.
    pub frames_read: u64,
    /// Empty reads before the first-ever filled slot (startup priming).
    pub startup_empty_reads: u64,
    /// Empty reads after at least one filled slot (steady-state slips).
    pub empty_reads: u64,
    /// Times the observed `writer_epoch` changed (writer reattached).
    pub epoch_resets: u64,
    /// Defensive resyncs when `write_seq - read_seq > n_slots` (should be 0).
    pub reader_resyncs: u64,
    /// Resyncs performed at attach time (`read_seq = write_seq`).
    pub attach_resyncs: u64,
    /// Last-observed writer pid (0 = detached).
    pub writer_pid: u64,
    /// Age of the writer heartbeat in ms at the last read (u64::MAX = never).
    pub writer_heartbeat_age_ms: u64,
    /// The writer looked alive at the last read (pid != 0 AND heartbeat < 2 s).
    pub writer_alive: bool,
    /// n_slots the ring was created/attached with (echoed for /state).
    pub n_slots: u32,
    /// period_frames per slot (echoed for /state).
    pub slot_frames: u32,
}

/// Writer liveness window: past this heartbeat age the writer is treated as
/// dead (reader reports `writer_alive:false`; the writer side free-runs).
pub const WRITER_LIVENESS_TIMEOUT_NS: u64 = 2_000_000_000;

/// One bounded attach budget for the creator's ftruncate + magic publish.
const MAGIC_WAIT_TIMEOUT_MS: u64 = 100;
const MAGIC_WAIT_STEP_US: u64 = 200;
const OPEN_LOCK_SUFFIX: &str = ".open.lock";
const OPEN_LOCK_MODE: u32 = 0o660;
const OPEN_LOCK_WAIT_TIMEOUT_MS: u64 = 500;
const OPEN_LOCK_WAIT_STEP_US: u64 = 1_000;
const OPEN_MAX_ATTEMPTS: usize = 8;

/// Adjacent, persistent advisory lock for one complete open transaction.
///
/// This is deliberately a separate inode from the replaceable ring path. C and
/// Rust both hold `<ring path>.open.lock` across classification, conditional
/// reclaim, create, initialization, and final linked-path ownership proof.
struct OpenTransactionLock {
    fd: RawFd,
}

impl OpenTransactionLock {
    fn acquire_with_wait_hook<F>(path: &str, mut on_wait: F) -> io::Result<Self>
    where
        F: FnMut(),
    {
        let lock_path = format!("{path}{OPEN_LOCK_SUFFIX}");
        let c_lock_path = std::ffi::CString::new(lock_path).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "ring lock path contains NUL")
        })?;
        let fd = unsafe {
            libc::open(
                c_lock_path.as_ptr(),
                libc::O_RDWR | libc::O_CREAT | libc::O_CLOEXEC,
                OPEN_LOCK_MODE as libc::c_uint,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        if unsafe { libc::fchmod(fd, OPEN_LOCK_MODE as libc::mode_t) } < 0 {
            let e = io::Error::last_os_error();
            if e.raw_os_error() != Some(libc::EPERM) {
                unsafe { libc::close(fd) };
                return Err(e);
            }
        }
        let deadline_ns = monotonic_ns() + OPEN_LOCK_WAIT_TIMEOUT_MS * 1_000_000;
        let mut wait_reported = false;
        loop {
            if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                return Ok(Self { fd });
            }
            let e = io::Error::last_os_error();
            let retryable = matches!(
                e.raw_os_error(),
                Some(code) if code == libc::EWOULDBLOCK || code == libc::EAGAIN || code == libc::EINTR
            );
            if !retryable {
                unsafe { libc::close(fd) };
                return Err(e);
            }
            if !wait_reported {
                on_wait();
                wait_reported = true;
            }
            if monotonic_ns() >= deadline_ns {
                eprintln!("event=outputd.shm_ring.open_lock_exhausted path={path}");
                unsafe { libc::close(fd) };
                return Err(io::Error::from_raw_os_error(libc::EAGAIN));
            }
            open_lock_sleep();
        }
    }
}

impl Drop for OpenTransactionLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.fd, libc::LOCK_UN);
            libc::close(self.fd);
        }
    }
}

#[derive(Clone, Copy)]
struct FileIdentity {
    dev: u64,
    ino: u64,
}

fn fd_identity(fd: RawFd) -> io::Result<FileIdentity> {
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut st) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(FileIdentity {
        dev: st.st_dev as u64,
        ino: st.st_ino as u64,
    })
}

fn identity_matches_linked_path(path: &str, identity: FileIdentity) -> io::Result<bool> {
    let metadata = std::fs::metadata(path)?;
    Ok(metadata.dev() == identity.dev && metadata.ino() == identity.ino)
}

fn fd_matches_linked_path(path: &str, fd: RawFd) -> io::Result<bool> {
    identity_matches_linked_path(path, fd_identity(fd)?)
}

fn mapping_owns_linked_path(path: &str, map: &RingMapping) -> io::Result<bool> {
    fd_matches_linked_path(path, map.fd)
}

fn open_lock_sleep() {
    let ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: (OPEN_LOCK_WAIT_STEP_US * 1000) as _,
    };
    unsafe { libc::nanosleep(&ts, std::ptr::null_mut()) };
}

/// A mmap'd view of the shared header + slots.
///
/// Owns the mapping and the fd for its lifetime; unmaps + closes on drop. The
/// header atomics are accessed through raw pointers into the mmap via
/// `AtomicU64::from_ptr` (stable since 1.75) — the same lock-free 8-byte
/// atomics the C side uses, page-aligned by mmap so every field is aligned.
struct RingMapping {
    base: *mut u8,
    len: usize,
    fd: RawFd,
    geometry: Geometry,
}

// SAFETY: the mapping is shared SPSC; this reader is the sole consumer and its
// atomics carry the cross-process synchronization. The struct is not Sync (no
// concurrent access within the process); Send is fine — a single owner may
// move it between threads.
unsafe impl Send for RingMapping {}

impl RingMapping {
    fn header_atomic(&self, offset: usize) -> &AtomicU64 {
        debug_assert!(offset + 8 <= HEADER_BYTES);
        debug_assert_eq!(offset % 8, 0);
        // SAFETY: offset is within the header (< HEADER_BYTES), 8-byte aligned,
        // and the mmap base is page-aligned, so the pointer is valid and
        // aligned for an 8-byte atomic. The mapping outlives the reference.
        unsafe { AtomicU64::from_ptr(self.base.add(offset) as *mut u64) }
    }

    /// Read a plain u32 header field (rate/channels/etc are init-only; the
    /// reader validated them at attach and never mutates them).
    fn header_u32(&self, offset: usize) -> u32 {
        debug_assert!(offset + 4 <= HEADER_BYTES);
        // SAFETY: offset within header, 4-byte read from a valid mapping.
        unsafe { std::ptr::read_unaligned(self.base.add(offset) as *const u32) }
    }

    fn slot_ptr(&self, slot_index: u32) -> *const u8 {
        let off = HEADER_BYTES + (slot_index as usize) * self.geometry.slot_bytes();
        debug_assert!(off + self.geometry.slot_bytes() <= self.len);
        // SAFETY: slot_index < n_slots (caller guarantees via seq % n_slots)
        // and the mapping is sized HEADER_BYTES + n_slots*slot_bytes.
        unsafe { self.base.add(off) }
    }
}

impl Drop for RingMapping {
    fn drop(&mut self) {
        // SAFETY: base/len came from a successful mmap; fd from open.
        unsafe {
            libc::munmap(self.base as *mut libc::c_void, self.len);
            libc::close(self.fd);
        }
    }
}

/// The reader half of the ring: attaches to (or creates) the SHM file, then
/// serves one slot per DAC period, never blocking.
pub struct RingReader {
    map: RingMapping,
    path: String,
    /// The reader's local view of how many slots it has consumed. Authoritative
    /// mirror of `read_seq` in the header, which this reader owns WHILE LIVE — the
    /// writer advances it only on its no-live-reader free-run path (see the
    /// module doc's "Writer free-run"), so a live reader is the sole writer of
    /// this field.
    read_seq: u64,
    /// Last-observed writer epoch; a change means the writer reattached.
    last_epoch: u64,
    saw_filled: bool,
    metrics: RingMetrics,
}

impl RingReader {
    /// Attach to an existing ring, or create it if absent, validating against
    /// `expected`. `O_EXCL` create races are resolved by attaching instead.
    ///
    /// On attach the reader resyncs `read_seq = write_seq` (drops the <=
    /// `n_slots` stale slots accumulated while the reader was down; counted
    /// `attach_resyncs`) and stamps `reader_pid`.
    pub fn create_or_attach(path: &str, expected: Geometry) -> io::Result<Self> {
        expected.validate_self()?;
        let map = attach_or_create(path, expected)?;

        // Resync to the writer's current tip: the reader is joining a
        // possibly-running writer, and stale slots are worthless to a pacer.
        let write_seq = map
            .header_atomic(layout::OFF_WRITE_SEQ)
            .load(Ordering::Acquire);
        let last_epoch = map
            .header_atomic(layout::OFF_WRITER_EPOCH)
            .load(Ordering::Acquire);
        // Publish the resynced read_seq so the writer's space check is correct.
        map.header_atomic(layout::OFF_READ_SEQ)
            .store(write_seq, Ordering::Release);
        // Stamp reader presence for the writer's liveness check.
        map.header_atomic(layout::OFF_READER_PID)
            .store(std::process::id() as u64, Ordering::Relaxed);
        map.header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .store(monotonic_ns(), Ordering::Relaxed);

        let attach_resyncs = if write_seq > 0 { 1 } else { 0 };
        let metrics = RingMetrics {
            attached: true,
            attach_resyncs,
            n_slots: expected.n_slots,
            slot_frames: expected.period_frames,
            ..RingMetrics::default()
        };
        Ok(Self {
            map,
            path: path.to_string(),
            read_seq: write_seq,
            last_epoch,
            saw_filled: false,
            metrics,
        })
    }

    pub fn path(&self) -> &str {
        &self.path
    }

    pub fn metrics(&self) -> RingMetrics {
        self.metrics
    }

    pub fn geometry(&self) -> Geometry {
        self.map.geometry
    }

    /// Try to consume exactly one slot into `out` (`out.len()` must equal
    /// `period_frames * channels`). NEVER blocks:
    /// - slot available -> copies it, advances `read_seq`, returns
    ///   [`SlotRead::Filled`];
    /// - ring empty -> zero-fills `out`, returns [`SlotRead::Empty`].
    ///
    /// Always updates the reader heartbeat and refreshes the writer-liveness
    /// view (so `/state` is honest even on empty periods).
    pub fn try_consume_slot(&mut self, out: &mut [i16]) -> SlotRead {
        let g = self.map.geometry;
        debug_assert_eq!(out.len(), g.samples_per_slot());

        // Heartbeat + writer-liveness refresh happen every period, filled or not.
        let now = monotonic_ns();
        self.map
            .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .store(now, Ordering::Relaxed);
        self.refresh_writer_liveness(now);
        self.observe_epoch();

        let write_seq = self
            .map
            .header_atomic(layout::OFF_WRITE_SEQ)
            .load(Ordering::Acquire);
        let mut r = self.read_seq;

        // Defensive: a correct writer never lets W - R exceed n_slots. If it
        // somehow did, fast-forward to the tip and count it (never read a slot
        // the writer may be mid-overwriting).
        if write_seq.wrapping_sub(r) > g.n_slots as u64 {
            r = write_seq;
            self.read_seq = r;
            self.map
                .header_atomic(layout::OFF_READ_SEQ)
                .store(r, Ordering::Release);
            self.metrics.reader_resyncs = self.metrics.reader_resyncs.saturating_add(1);
        }

        if write_seq == r {
            // Empty: silence. Split startup priming from steady-state slips.
            out.fill(0);
            if self.saw_filled {
                self.metrics.empty_reads = self.metrics.empty_reads.saturating_add(1);
            } else {
                self.metrics.startup_empty_reads =
                    self.metrics.startup_empty_reads.saturating_add(1);
            }
            self.metrics.occupancy = 0;
            return SlotRead::Empty;
        }

        // A slot is available. Copy slot (r % n_slots) out with plain loads —
        // safe because the Acquire load of write_seq above ordered the writer's
        // payload stores before this read.
        let slot_index = (r % g.n_slots as u64) as u32;
        // SAFETY: slot_index < n_slots; out.len() == samples_per_slot; the slot
        // payload is exactly slot_bytes == samples_per_slot * 2 bytes.
        unsafe {
            copy_slot_to_i16(self.map.slot_ptr(slot_index), out, g.samples_per_slot());
        }

        // Release the slot: store read_seq = r+1 with Release so the copy-out
        // cannot be reordered after the writer observes the slot as free.
        let next = r.wrapping_add(1);
        self.read_seq = next;
        self.map
            .header_atomic(layout::OFF_READ_SEQ)
            .store(next, Ordering::Release);

        self.saw_filled = true;
        self.metrics.frames_read_slots = self.metrics.frames_read_slots.saturating_add(1);
        self.metrics.frames_read = self
            .metrics
            .frames_read
            .saturating_add(g.period_frames as u64);
        self.metrics.occupancy = write_seq.wrapping_sub(next);
        SlotRead::Filled
    }

    fn observe_epoch(&mut self) {
        let epoch = self
            .map
            .header_atomic(layout::OFF_WRITER_EPOCH)
            .load(Ordering::Acquire);
        if epoch != self.last_epoch {
            self.last_epoch = epoch;
            self.metrics.epoch_resets = self.metrics.epoch_resets.saturating_add(1);
        }
    }

    fn refresh_writer_liveness(&mut self, now_ns: u64) {
        let pid = self
            .map
            .header_atomic(layout::OFF_WRITER_PID)
            .load(Ordering::Relaxed);
        let hb = self
            .map
            .header_atomic(layout::OFF_WRITER_HEARTBEAT_NS)
            .load(Ordering::Relaxed);
        self.metrics.writer_pid = pid;
        if hb == 0 {
            self.metrics.writer_heartbeat_age_ms = u64::MAX;
            self.metrics.writer_alive = false;
        } else {
            let age_ns = now_ns.saturating_sub(hb);
            self.metrics.writer_heartbeat_age_ms = age_ns / 1_000_000;
            self.metrics.writer_alive = pid != 0 && age_ns < WRITER_LIVENESS_TIMEOUT_NS;
        }
    }
}

impl Drop for RingReader {
    fn drop(&mut self) {
        // Clear reader presence so the writer's liveness check sees us gone and
        // free-runs (drops frames) rather than blocking on a dead reader — but
        // only if reader_pid is still OURS. A second reader attaching (which
        // stamps its own pid) then this instance dropping must not clear the new
        // reader's presence. Mirrors the C writer_close `cur == mine` guard.
        let slot = self.map.header_atomic(layout::OFF_READER_PID);
        let mine = std::process::id() as u64;
        if slot.load(Ordering::Relaxed) == mine {
            slot.store(0, Ordering::Relaxed);
        }
    }
}

/// Copy `samples` little-endian i16 samples from a raw slot pointer into `out`.
///
/// # Safety
/// `src` must point to at least `samples * 2` valid bytes (the slot payload);
/// `out.len()` must be `>= samples`.
unsafe fn copy_slot_to_i16(src: *const u8, out: &mut [i16], samples: usize) {
    // The slot is native-endian i16 written by the C memcpy from the ioplug's
    // S16_LE interleaved staging; on the little-endian aarch64 / x86 targets
    // this is a straight copy. Read unaligned to be defensive (the slot base is
    // HEADER_BYTES + k*slot_bytes; HEADER_BYTES=128 and slot_bytes is a
    // multiple of 4, so it is in fact 2-aligned, but read_unaligned costs
    // nothing and documents that we do not rely on it).
    for (i, dst) in out.iter_mut().take(samples).enumerate() {
        let p = src.add(i * 2) as *const i16;
        *dst = std::ptr::read_unaligned(p);
    }
}

/// `O_EXCL` create (init + magic-last) or attach (bounded size+magic wait +
/// geometry validation). A magic-invalid file under the owned
/// `/dev/shm/jts-ring/` root is unlinked and recreated.
fn attach_or_create(path: &str, expected: Geometry) -> io::Result<RingMapping> {
    attach_or_create_with_hooks(path, expected, || {}, |_| {}, || {})
}

fn attach_or_create_with_hooks<F, G, H>(
    path: &str,
    expected: Geometry,
    on_lock_wait: F,
    mut on_created: G,
    mut on_before_reclaim: H,
) -> io::Result<RingMapping>
where
    F: FnMut(),
    G: FnMut(&RingMapping),
    H: FnMut(),
{
    ensure_parent_dir(path)?;
    let c_path = std::ffi::CString::new(path)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "ring path contains NUL"))?;
    let _open_lock = OpenTransactionLock::acquire_with_wait_hook(path, on_lock_wait)?;

    for _attempt in 0..OPEN_MAX_ATTEMPTS {
        #[cfg(test)]
        if TEST_FORCE_OPEN_RETRY.with(|slot| slot.get()) {
            continue;
        }
        // Try to create exclusively; the creator inits the header.
        let create_fd = unsafe {
            libc::open(
                c_path.as_ptr(),
                libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                0o660,
            )
        };
        if create_fd >= 0 {
            match init_created(create_fd, expected) {
                Ok(map) => {
                    on_created(&map);
                    match mapping_owns_linked_path(path, &map) {
                        Ok(true) => return Ok(map),
                        Ok(false) => {
                            eprintln!("event=outputd.shm_ring.creator_path_lost path={path}");
                            drop(map);
                            continue;
                        }
                        Err(e) if e.kind() == io::ErrorKind::NotFound => {
                            drop(map);
                            continue;
                        }
                        Err(e) => return Err(e),
                    }
                }
                Err(e) => {
                    // Creation failed mid-init; drop the half-baked file so the
                    // next opener does not attach to a magic-less carcass. Do
                    // not unlink a pathname that no longer names our fd.
                    let still_linked = fd_matches_linked_path(path, create_fd);
                    unsafe { libc::close(create_fd) };
                    if matches!(still_linked, Ok(true)) {
                        let _ = std::fs::remove_file(path);
                    }
                    return Err(e);
                }
            }
        }
        let err = io::Error::last_os_error();
        if err.raw_os_error() != Some(libc::EEXIST) {
            return Err(err);
        }

        // The file exists — attach to it.
        let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDWR | libc::O_CLOEXEC) };
        if fd < 0 {
            let err = io::Error::last_os_error();
            // Lost a race where the file was unlinked between EEXIST and open;
            // retry the create.
            if err.raw_os_error() == Some(libc::ENOENT) {
                continue;
            }
            return Err(err);
        }
        let opened_identity = match fd_identity(fd) {
            Ok(identity) => identity,
            Err(e) => {
                unsafe { libc::close(fd) };
                return Err(e);
            }
        };
        match attach_existing(fd, expected) {
            Ok(map) => match mapping_owns_linked_path(path, &map) {
                Ok(true) => return Ok(map),
                Ok(false) => {
                    drop(map);
                    continue;
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => {
                    drop(map);
                    continue;
                }
                Err(e) => return Err(e),
            },
            Err(AttachError::Fatal(e)) => {
                return Err(e);
            }
            Err(AttachError::MagicInvalid) => {
                // A creator crashed mid-init (magic never appeared). Only the
                // owner may reclaim, and only under the narrow owned path.
                if !is_owned_ring_path(path) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("ring {path:?} has no valid magic and is not reclaimable"),
                    ));
                }
                match identity_matches_linked_path(path, opened_identity) {
                    Ok(true) => {}
                    Ok(false) => continue,
                    Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                    Err(e) => return Err(e),
                }
                on_before_reclaim();
                if let Err(e) = remove_owned_ring(path) {
                    if e.kind() == io::ErrorKind::NotFound {
                        continue; // another reclaimer won; retry create
                    }
                    eprintln!(
                        "event=outputd.shm_ring.reclaim_failed errno={} path={path}",
                        e.raw_os_error().unwrap_or(-1)
                    );
                    return Err(e);
                }
                eprintln!("event=outputd.shm_ring.reclaimed_magic_invalid path={path}");
                // Loop back and re-create.
            }
        }
    }
    eprintln!("event=outputd.shm_ring.attach_exhausted path={path}");
    Err(io::Error::from_raw_os_error(libc::EAGAIN))
}

enum AttachError {
    Fatal(io::Error),
    /// The creator did not complete ftruncate + magic publication within the
    /// bounded wait. Reclaimable under the owned path.
    MagicInvalid,
}

fn init_created(fd: RawFd, g: Geometry) -> io::Result<RingMapping> {
    let file_size = g.file_size();
    if unsafe { libc::ftruncate(fd, file_size as libc::off_t) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let map = mmap_fd(fd, file_size, g)?;

    // Init non-magic header fields first (zeroes from ftruncate cover the
    // atomics and slots, but be explicit for the config fields).
    write_u32(&map, layout::OFF_VERSION, VERSION);
    write_u32(&map, layout::OFF_RATE, g.rate);
    write_u32(&map, layout::OFF_CHANNELS, g.channels);
    write_u32(&map, layout::OFF_SAMPLE_FORMAT, g.sample_format);
    write_u32(&map, layout::OFF_PERIOD_FRAMES, g.period_frames);
    write_u32(&map, layout::OFF_N_SLOTS, g.n_slots);
    write_u32(&map, layout::OFF_PAD, 0);
    // Seqs/epoch/pids/heartbeats start at 0 (ftruncate zeroed them). Whichever
    // role wins the create race (the reader under Ring B, the writer under
    // Ring A) leaves the pids at 0 here; each side's own create_or_attach caller
    // stamps its pid (reader_pid on the reader path, writer attach on the writer
    // path). Publish magic LAST with Release so an attacher that observes the magic
    // observes the fully-initialized header (version already written above; the
    // Release store preserves it in the qword's high half).
    write_u32_release_magic(&map);
    Ok(map)
}

/// Consume `fd` and attach it to a validated mapping. On every error this
/// function closes the fd itself: either explicitly before mmap ownership is
/// established, or through `RingMapping::drop` afterward.
fn attach_existing(fd: RawFd, expected: Geometry) -> Result<RingMapping, AttachError> {
    attach_existing_with_size_wait_hook(fd, expected, |_, _| {})
}

fn attach_existing_with_size_wait_hook<F>(
    fd: RawFd,
    expected: Geometry,
    mut on_size_wait: F,
) -> Result<RingMapping, AttachError>
where
    F: FnMut(RawFd, &libc::stat),
{
    // One bounded budget covers both the creator's ftruncate and magic publish.
    // A zero/small file is not immediately torn: an O_EXCL winner may simply be
    // between open and ftruncate.
    let deadline_ns = monotonic_ns() + MAGIC_WAIT_TIMEOUT_MS * 1_000_000;
    let actual_size = match wait_for_mappable_size(fd, deadline_ns, &mut on_size_wait) {
        Ok(size) => size,
        Err(e) => {
            unsafe { libc::close(fd) };
            return Err(e);
        }
    };

    // Map the ACTUAL bytes with the expected geometry recorded only for slot
    // math; the header's own declared geometry is validated below before any
    // slot is indexed, so a mismatch fails loud before slot math runs.
    let map = match mmap_fd(fd, actual_size, expected) {
        Ok(m) => m,
        Err(e) => {
            unsafe { libc::close(fd) };
            return Err(AttachError::Fatal(e));
        }
    };

    // Bounded wait for the creator's magic. No magic within the window means
    // the creator crashed mid-init (or this is not a ring).
    if !wait_for_magic(&map, deadline_ns) {
        return Err(AttachError::MagicInvalid);
    }

    // The magic is present, so the header is fully written. Cross-check that the
    // file size the header's own declared geometry implies matches the actual
    // size on disk — a corrupt/truncated ring with valid magic is fatal, not
    // reclaimable-as-mid-init.
    let header_geometry = Geometry {
        rate: map.header_u32(layout::OFF_RATE),
        channels: map.header_u32(layout::OFF_CHANNELS),
        sample_format: map.header_u32(layout::OFF_SAMPLE_FORMAT),
        period_frames: map.header_u32(layout::OFF_PERIOD_FRAMES),
        n_slots: map.header_u32(layout::OFF_N_SLOTS),
    };
    if header_geometry.file_size() != actual_size {
        return Err(AttachError::Fatal(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "ring file size {} is inconsistent with its own header geometry \
                 (declares {} bytes: rate={} ch={} fmt={} period={} slots={})",
                actual_size,
                header_geometry.file_size(),
                header_geometry.rate,
                header_geometry.channels,
                header_geometry.sample_format,
                header_geometry.period_frames,
                header_geometry.n_slots,
            ),
        )));
    }

    // Validate every config field against the caller's expectation. Any
    // mismatch is fail-loud (the daemon maps this to a config-class exit).
    let version = map.header_u32(layout::OFF_VERSION);
    if version != VERSION {
        return Err(AttachError::Fatal(mismatch("version", version, VERSION)));
    }
    let rate = map.header_u32(layout::OFF_RATE);
    if rate != expected.rate {
        return Err(AttachError::Fatal(mismatch("rate", rate, expected.rate)));
    }
    let channels = map.header_u32(layout::OFF_CHANNELS);
    if channels != expected.channels {
        return Err(AttachError::Fatal(mismatch(
            "channels",
            channels,
            expected.channels,
        )));
    }
    let fmt = map.header_u32(layout::OFF_SAMPLE_FORMAT);
    if fmt != expected.sample_format {
        return Err(AttachError::Fatal(mismatch(
            "sample_format",
            fmt,
            expected.sample_format,
        )));
    }
    let period = map.header_u32(layout::OFF_PERIOD_FRAMES);
    if period != expected.period_frames {
        return Err(AttachError::Fatal(mismatch(
            "period_frames",
            period,
            expected.period_frames,
        )));
    }
    let n_slots = map.header_u32(layout::OFF_N_SLOTS);
    if n_slots != expected.n_slots {
        return Err(AttachError::Fatal(mismatch(
            "n_slots",
            n_slots,
            expected.n_slots,
        )));
    }
    Ok(map)
}

fn mismatch(field: &str, got: u32, want: u32) -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        format!("ring header {field} mismatch: file has {got}, expected {want}"),
    )
}

fn magic_wait_sleep() {
    let ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: (MAGIC_WAIT_STEP_US * 1000) as _,
    };
    unsafe { libc::nanosleep(&ts, std::ptr::null_mut()) };
}

fn wait_for_mappable_size<F>(
    fd: RawFd,
    deadline_ns: u64,
    on_size_wait: &mut F,
) -> Result<usize, AttachError>
where
    F: FnMut(RawFd, &libc::stat),
{
    let mut size_wait_reported = false;
    loop {
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstat(fd, &mut st) } < 0 {
            return Err(AttachError::Fatal(io::Error::last_os_error()));
        }
        if st.st_size as u64 >= HEADER_BYTES as u64 {
            return Ok(st.st_size as usize);
        }
        if !size_wait_reported {
            on_size_wait(fd, &st);
            size_wait_reported = true;
        }
        if monotonic_ns() >= deadline_ns {
            return Err(AttachError::MagicInvalid);
        }
        magic_wait_sleep();
    }
}

fn wait_for_magic(map: &RingMapping, deadline_ns: u64) -> bool {
    loop {
        let magic = map
            .header_atomic(layout::OFF_MAGIC_QWORD)
            .load(Ordering::Acquire) as u32;
        if magic == MAGIC {
            return true;
        }
        if monotonic_ns() >= deadline_ns {
            return false;
        }
        magic_wait_sleep();
    }
}

fn mmap_fd(fd: RawFd, len: usize, geometry: Geometry) -> io::Result<RingMapping> {
    let base = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            len,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_SHARED,
            fd,
            0,
        )
    };
    if base == libc::MAP_FAILED {
        return Err(io::Error::last_os_error());
    }
    Ok(RingMapping {
        base: base as *mut u8,
        len,
        fd,
        geometry,
    })
}

fn write_u32(map: &RingMapping, offset: usize, value: u32) {
    debug_assert!(offset + 4 <= HEADER_BYTES);
    // SAFETY: offset within header, 4-byte write into a valid, writable mapping.
    unsafe {
        std::ptr::write_unaligned(map.base.add(offset) as *mut u32, value);
    }
}

/// Publish the magic word LAST with Release ordering. Magic sits at offset 0
/// and `version` at offset 4; they share the 8-byte qword at
/// [`layout::OFF_MAGIC_QWORD`]. We do the release as a full qword store that
/// preserves the already-written `version` in the high 4 bytes.
fn write_u32_release_magic(map: &RingMapping) {
    let version = map.header_u32(layout::OFF_VERSION) as u64;
    let qword = (MAGIC as u64) | (version << 32);
    map.header_atomic(layout::OFF_MAGIC_QWORD)
        .store(qword, Ordering::Release);
}

fn ensure_parent_dir(path: &str) -> io::Result<()> {
    if let Some(parent) = std::path::Path::new(path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    Ok(())
}

#[cfg(test)]
thread_local! {
    // Per-test-thread hooks avoid process-global env races under Rust's parallel
    // test runner. Product builds compile both overrides out entirely.
    static TEST_OWNED_RING_DIR: std::cell::RefCell<Option<std::path::PathBuf>> =
        const { std::cell::RefCell::new(None) };
    static TEST_RECLAIM_ERRNO: std::cell::Cell<Option<i32>> =
        const { std::cell::Cell::new(None) };
    static TEST_FORCE_OPEN_RETRY: std::cell::Cell<bool> =
        const { std::cell::Cell::new(false) };
}

fn remove_owned_ring(path: &str) -> io::Result<()> {
    #[cfg(test)]
    if let Some(injected_errno) = TEST_RECLAIM_ERRNO.with(|slot| slot.replace(None)) {
        if injected_errno == libc::ENOENT {
            // Model another reclaimer winning before our unlink. The pathname
            // is genuinely removed, then this attempt observes NotFound.
            let _ = std::fs::remove_file(path);
        }
        return Err(io::Error::from_raw_os_error(injected_errno));
    }
    std::fs::remove_file(path)
}

/// The reader may only unlink-and-recreate a magic-invalid file directly under
/// the owned `/dev/shm/jts-ring/` root — a narrow-path check mirroring outputd's
/// `is_owned_runtime_pipe_path`. A nested or foreign path is never reclaimed.
fn is_owned_ring_path(path: &str) -> bool {
    #[cfg(test)]
    if let Some(is_owned) = TEST_OWNED_RING_DIR.with(|root| {
        root.borrow()
            .as_ref()
            .map(|root| std::path::Path::new(path).parent() == Some(root.as_path()))
    }) {
        return is_owned;
    }
    std::path::Path::new(path).parent() == Some(std::path::Path::new("/dev/shm/jts-ring"))
}

fn monotonic_ns() -> u64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: passing a valid timespec pointer to a well-known clock.
    unsafe {
        libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts);
    }
    (ts.tv_sec as u64) * 1_000_000_000 + (ts.tv_nsec as u64)
}

/// A minimal in-process WRITER, used ONLY by tests and by the outputd cfg
/// tests to drive the reader without the C ioplug. It implements the exact
/// publish discipline the C writer and the production [`writer::RingWriter`]
/// implement (space check with Acquire, payload memcpy, Release publish), so the
/// cross-language interop the bench proves on-Pi is exercised in-process here
/// too.
///
/// This is the deliberate NON-BLOCKING test twin: `try_publish_slot` returns
/// `false` on a full ring, whereas the product writers block/poll or free-run —
/// keeping the reader-side tests simple. It is NOT a product path (the product
/// writers are the C ioplug under Ring B and [`writer::RingWriter`] under
/// Ring A). If the shared publish ordering ever changes, update BOTH this twin
/// and `writer::RingWriter`. (A future cleanup could retire this in favour of a
/// non-blocking mode on `RingWriter`; deferred to avoid rewriting the ALSA-gated
/// reader tests here.) Gated behind the public API but intended for test/bench
/// use only.
pub struct TestRingWriter {
    map: RingMapping,
    write_seq: u64,
}

impl TestRingWriter {
    /// Attach to (or create) the ring as the writer: bump `writer_epoch`, stamp
    /// `writer_pid`, and continue from the stored `write_seq`.
    pub fn create_or_attach(path: &str, expected: Geometry) -> io::Result<Self> {
        expected.validate_self()?;
        let map = attach_or_create(path, expected)?;
        // Writer attach: epoch++ (Release), pid, heartbeat, continue from
        // stored write_seq (file-lifetime monotonic).
        let write_seq = map
            .header_atomic(layout::OFF_WRITE_SEQ)
            .load(Ordering::Acquire);
        let epoch = map
            .header_atomic(layout::OFF_WRITER_EPOCH)
            .load(Ordering::Acquire);
        map.header_atomic(layout::OFF_WRITER_EPOCH)
            .store(epoch + 1, Ordering::Release);
        map.header_atomic(layout::OFF_WRITER_PID)
            .store(std::process::id() as u64, Ordering::Relaxed);
        map.header_atomic(layout::OFF_WRITER_HEARTBEAT_NS)
            .store(monotonic_ns(), Ordering::Relaxed);
        Ok(Self { map, write_seq })
    }

    /// Free slots available for publish (`n_slots - (W - R)`).
    pub fn free_slots(&self) -> u64 {
        let r = self
            .map
            .header_atomic(layout::OFF_READ_SEQ)
            .load(Ordering::Acquire);
        (self.map.geometry.n_slots as u64).saturating_sub(self.write_seq.wrapping_sub(r))
    }

    /// Publish one slot from `samples` (`samples.len()` == samples_per_slot).
    /// Returns `true` if published, `false` if the ring was full (no space).
    /// This is the non-blocking try-publish; the real writer blocks/polls or
    /// free-runs on full — see the module doc.
    pub fn try_publish_slot(&mut self, samples: &[i16]) -> bool {
        let g = self.map.geometry;
        assert_eq!(samples.len(), g.samples_per_slot());
        self.map
            .header_atomic(layout::OFF_WRITER_HEARTBEAT_NS)
            .store(monotonic_ns(), Ordering::Relaxed);
        let r = self
            .map
            .header_atomic(layout::OFF_READ_SEQ)
            .load(Ordering::Acquire);
        let w = self.write_seq;
        if w.wrapping_sub(r) >= g.n_slots as u64 {
            return false; // full
        }
        let slot_index = (w % g.n_slots as u64) as u32;
        // SAFETY: slot_index < n_slots; samples.len() == samples_per_slot; the
        // slot payload is exactly slot_bytes.
        unsafe {
            let dst = self.map.slot_ptr(slot_index) as *mut u8;
            for (i, &s) in samples.iter().enumerate() {
                std::ptr::write_unaligned(dst.add(i * 2) as *mut i16, s);
            }
        }
        let next = w.wrapping_add(1);
        self.write_seq = next;
        self.map
            .header_atomic(layout::OFF_WRITE_SEQ)
            .store(next, Ordering::Release);
        true
    }

    pub fn write_seq(&self) -> u64 {
        self.write_seq
    }
}

impl Drop for TestRingWriter {
    fn drop(&mut self) {
        // Clear writer pid so a subsequent reader sees the writer as detached.
        self.map
            .header_atomic(layout::OFF_WRITER_PID)
            .store(0, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::fd::IntoRawFd;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::sync::mpsc;

    fn tmp_ring_path(tag: &str) -> String {
        // Host-testable: not /dev/shm on macOS. Use the OS temp dir so the
        // reader/writer logic runs everywhere; the owned-path reclaim rule is
        // unit-tested separately with the real /dev/shm path string.
        let dir = std::env::temp_dir().join(format!(
            "jts-ring-test-{}-{}-{}",
            tag,
            std::process::id(),
            RING_TEST_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("content.ring").to_string_lossy().into_owned()
    }

    static RING_TEST_SEQ: AtomicU64 = AtomicU64::new(0);

    fn proto_geometry() -> Geometry {
        Geometry {
            rate: 48_000,
            channels: 2,
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames: 128,
            n_slots: 2,
        }
    }

    fn cleanup(path: &str) {
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(format!("{path}{OPEN_LOCK_SUFFIX}"));
        if let Some(parent) = std::path::Path::new(path).parent() {
            let _ = std::fs::remove_dir(parent);
        }
    }

    struct OwnedReclaimTestGuard;

    impl OwnedReclaimTestGuard {
        fn arm(path: &str, reclaim_errno: i32) -> Self {
            let parent = std::path::Path::new(path).parent().unwrap().to_path_buf();
            TEST_OWNED_RING_DIR.with(|root| *root.borrow_mut() = Some(parent));
            TEST_RECLAIM_ERRNO.with(|slot| slot.set(Some(reclaim_errno)));
            Self
        }
    }

    impl Drop for OwnedReclaimTestGuard {
        fn drop(&mut self) {
            TEST_RECLAIM_ERRNO.with(|slot| slot.set(None));
            TEST_OWNED_RING_DIR.with(|root| *root.borrow_mut() = None);
        }
    }

    fn create_full_size_torn_ring(path: &str, g: Geometry) -> std::fs::Metadata {
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)
            .unwrap();
        file.set_len(g.file_size() as u64).unwrap();
        file.metadata().unwrap()
    }

    #[test]
    fn empty_ring_reads_silence() {
        let path = tmp_ring_path("empty");
        let g = proto_geometry();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let mut out = vec![7i16; g.samples_per_slot()];
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Empty);
        assert!(out.iter().all(|&s| s == 0));
        assert_eq!(reader.metrics().startup_empty_reads, 1);
        assert_eq!(reader.metrics().empty_reads, 0);
        assert_eq!(reader.metrics().frames_read, 0);
        cleanup(&path);
    }

    #[test]
    fn publish_then_consume_roundtrips_payload() {
        let path = tmp_ring_path("roundtrip");
        let g = proto_geometry();
        let mut writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();

        let n = g.samples_per_slot();
        let payload: Vec<i16> = (0..n)
            .map(|i| (i as i16).wrapping_mul(3).wrapping_sub(5))
            .collect();
        assert!(writer.try_publish_slot(&payload));

        let mut out = vec![0i16; n];
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Filled);
        assert_eq!(out, payload);
        assert_eq!(reader.metrics().frames_read, g.period_frames as u64);
        assert_eq!(reader.metrics().frames_read_slots, 1);
        // After consuming the only slot, the ring is empty again (steady-state).
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Empty);
        assert_eq!(reader.metrics().empty_reads, 1);
        assert_eq!(reader.metrics().startup_empty_reads, 0);
        cleanup(&path);
    }

    #[test]
    fn ping_pong_bounded_at_n_slots() {
        let path = tmp_ring_path("pingpong");
        let g = proto_geometry();
        let mut writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let s = vec![1i16; n];

        // Fill both slots.
        assert!(writer.try_publish_slot(&s));
        assert!(writer.try_publish_slot(&s));
        // The ring is now full: the third publish must fail.
        assert!(!writer.try_publish_slot(&s));
        assert_eq!(writer.free_slots(), 0);

        // Consume one, then a publish succeeds again (ping-pong).
        let mut out = vec![0i16; n];
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Filled);
        assert_eq!(reader.metrics().occupancy, 1);
        assert!(writer.try_publish_slot(&s));
        assert_eq!(writer.free_slots(), 0);
        cleanup(&path);
    }

    #[test]
    fn occupancy_tracks_write_minus_read() {
        let path = tmp_ring_path("occ");
        let g = Geometry {
            n_slots: 4,
            ..proto_geometry()
        };
        let mut writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let s = vec![2i16; n];
        writer.try_publish_slot(&s);
        writer.try_publish_slot(&s);
        writer.try_publish_slot(&s);
        let mut out = vec![0i16; n];
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Filled);
        assert_eq!(reader.metrics().occupancy, 2); // 3 written, 1 read
        cleanup(&path);
    }

    #[test]
    fn attach_resyncs_reader_to_writer_tip() {
        let path = tmp_ring_path("resync");
        let g = proto_geometry();
        // Writer publishes into the ring BEFORE the reader ever attaches.
        let mut writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let s = vec![9i16; n];
        assert!(writer.try_publish_slot(&s));
        // Now the reader attaches; it must resync to the tip (drop the stale
        // slot) rather than replay it.
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        assert_eq!(reader.metrics().attach_resyncs, 1);
        let mut out = vec![0i16; n];
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Empty);
        cleanup(&path);
    }

    #[test]
    fn writer_reattach_bumps_epoch_reset() {
        let path = tmp_ring_path("epoch");
        let g = proto_geometry();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let mut out = vec![0i16; n];
        // First writer attaches, publishes, drops.
        {
            let mut w1 = TestRingWriter::create_or_attach(&path, g).unwrap();
            assert!(w1.try_publish_slot(&vec![1i16; n]));
        }
        reader.try_consume_slot(&mut out); // observes epoch 1
        let e1 = reader.metrics().epoch_resets;
        // Second writer attaches (epoch bumps again).
        {
            let mut w2 = TestRingWriter::create_or_attach(&path, g).unwrap();
            assert!(w2.try_publish_slot(&vec![2i16; n]));
        }
        reader.try_consume_slot(&mut out);
        assert!(
            reader.metrics().epoch_resets > e1,
            "epoch_resets should advance on writer reattach: {} !> {}",
            reader.metrics().epoch_resets,
            e1
        );
        cleanup(&path);
    }

    #[test]
    fn writer_liveness_reflected_in_metrics() {
        let path = tmp_ring_path("liveness");
        let g = proto_geometry();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let mut out = vec![0i16; n];
        // No writer: reader sees writer_alive=false, pid=0.
        reader.try_consume_slot(&mut out);
        assert!(!reader.metrics().writer_alive);
        assert_eq!(reader.metrics().writer_pid, 0);
        // Writer attaches and heartbeats: alive.
        let _writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        reader.try_consume_slot(&mut out);
        assert!(reader.metrics().writer_alive);
        assert_ne!(reader.metrics().writer_pid, 0);
        cleanup(&path);
    }

    #[test]
    fn geometry_mismatch_on_attach_is_fatal() {
        let path = tmp_ring_path("mismatch");
        let g = proto_geometry();
        let _writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        // A reader expecting a different period_frames must fail loud.
        let wrong = Geometry {
            period_frames: 256,
            ..g
        };
        let err = match RingReader::create_or_attach(&path, wrong) {
            Ok(_) => panic!("geometry mismatch should be fatal"),
            Err(e) => e,
        };
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        assert!(
            err.to_string().contains("period_frames") || err.to_string().contains("file size"),
            "{err}"
        );
        let _reader = RingReader::create_or_attach(&path, g)
            .expect("fatal attach releases the transaction lock");
        cleanup(&path);
    }

    #[test]
    fn open_transaction_lock_serializes_a_then_b_then_c() {
        let path = tmp_ring_path("open-lock-a-b-c");
        let g = proto_geometry();
        let (created_tx, created_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let path_a = path.clone();
        let a = std::thread::spawn(move || {
            attach_or_create_with_hooks(
                &path_a,
                g,
                || {},
                |_| {
                    created_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                },
                || {},
            )
        });
        created_rx
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("A must hold the lock after initialization");

        let (wait_tx, wait_rx) = mpsc::channel();
        let path_b = path.clone();
        let wait_b = wait_tx.clone();
        let b = std::thread::spawn(move || {
            attach_or_create_with_hooks(&path_b, g, || wait_b.send(()).unwrap(), |_| {}, || {})
        });
        let path_c = path.clone();
        let c = std::thread::spawn(move || {
            attach_or_create_with_hooks(&path_c, g, || wait_tx.send(()).unwrap(), |_| {}, || {})
        });
        wait_rx
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("B must contend on A's transaction lock");
        wait_rx
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("C must contend on A's transaction lock");
        release_tx.send(()).unwrap();

        let map_a = a.join().unwrap().unwrap();
        let map_b = b.join().unwrap().unwrap();
        let map_c = c.join().unwrap().unwrap();
        let identity = fd_identity(map_a.fd).unwrap();
        assert!(identity_matches_linked_path(&path, identity).unwrap());
        assert_eq!(fd_identity(map_b.fd).unwrap().dev, identity.dev);
        assert_eq!(fd_identity(map_b.fd).unwrap().ino, identity.ino);
        assert_eq!(fd_identity(map_c.fd).unwrap().dev, identity.dev);
        assert_eq!(fd_identity(map_c.fd).unwrap().ino, identity.ino);
        drop((map_a, map_b, map_c));

        let lock_metadata = std::fs::metadata(format!("{path}{OPEN_LOCK_SUFFIX}")).unwrap();
        assert_eq!(lock_metadata.permissions().mode() & 0o777, 0o660);
        cleanup(&path);
    }

    #[test]
    fn stale_reclaimer_a_cannot_delete_replacement_seen_by_b_and_c() {
        let path = tmp_ring_path("stale-reclaimer-a-b-c");
        let g = proto_geometry();
        let torn = create_full_size_torn_ring(&path, g);
        let (reclaim_tx, reclaim_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let path_a = path.clone();
        let a = std::thread::spawn(move || {
            let parent = std::path::Path::new(&path_a)
                .parent()
                .unwrap()
                .to_path_buf();
            TEST_OWNED_RING_DIR.with(|root| *root.borrow_mut() = Some(parent));
            let result = attach_or_create_with_hooks(
                &path_a,
                g,
                || {},
                |_| {},
                || {
                    reclaim_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                },
            );
            TEST_OWNED_RING_DIR.with(|root| *root.borrow_mut() = None);
            result
        });
        reclaim_rx
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("A must hold the lock after classifying the torn inode");

        let (wait_tx, wait_rx) = mpsc::channel();
        let path_b = path.clone();
        let wait_b = wait_tx.clone();
        let b = std::thread::spawn(move || {
            attach_or_create_with_hooks(&path_b, g, || wait_b.send(()).unwrap(), |_| {}, || {})
        });
        let path_c = path.clone();
        let c = std::thread::spawn(move || {
            attach_or_create_with_hooks(&path_c, g, || wait_tx.send(()).unwrap(), |_| {}, || {})
        });
        wait_rx
            .recv_timeout(std::time::Duration::from_secs(2))
            .unwrap();
        wait_rx
            .recv_timeout(std::time::Duration::from_secs(2))
            .unwrap();
        release_tx.send(()).unwrap();

        let map_a = a.join().unwrap().unwrap();
        let map_b = b.join().unwrap().unwrap();
        let map_c = c.join().unwrap().unwrap();
        let replacement = fd_identity(map_a.fd).unwrap();
        assert_ne!((replacement.dev, replacement.ino), (torn.dev(), torn.ino()));
        assert!(identity_matches_linked_path(&path, replacement).unwrap());
        for map in [&map_b, &map_c] {
            let identity = fd_identity(map.fd).unwrap();
            assert_eq!(
                (identity.dev, identity.ino),
                (replacement.dev, replacement.ino)
            );
        }
        drop((map_a, map_b, map_c));
        cleanup(&path);
    }

    #[test]
    fn creator_rejects_replaced_linked_path() {
        let path = tmp_ring_path("creator-path-replaced");
        let orphan = format!("{path}.orphan");
        let g = proto_geometry();
        let path_for_hook = path.clone();
        let orphan_for_hook = orphan.clone();
        let result = attach_or_create_with_hooks(
            &path,
            g,
            || {},
            move |_| {
                std::fs::rename(&path_for_hook, &orphan_for_hook).unwrap();
                std::fs::create_dir(&path_for_hook).unwrap();
            },
            || {},
        );
        assert!(
            result.is_err(),
            "creator must not return an unlinked mapping"
        );
        assert!(std::fs::metadata(&orphan).unwrap().is_file());
        std::fs::remove_dir(&path).unwrap();
        std::fs::remove_file(&orphan).unwrap();
        cleanup(&path);
    }

    #[test]
    fn retry_exhaustion_is_bounded_and_releases_lock() {
        let path = tmp_ring_path("retry-exhaustion");
        let g = proto_geometry();
        TEST_FORCE_OPEN_RETRY.with(|slot| slot.set(true));
        let exhausted = attach_or_create(&path, g);
        TEST_FORCE_OPEN_RETRY.with(|slot| slot.set(false));
        let err = match exhausted {
            Ok(_) => panic!("forced retries must exhaust"),
            Err(err) => err,
        };
        assert_eq!(err.raw_os_error(), Some(libc::EAGAIN));

        let map =
            attach_or_create(&path, g).expect("retry exhaustion must release the transaction lock");
        drop(map);
        cleanup(&path);
    }

    #[test]
    fn lock_timeout_touches_no_ring_and_recovers_after_release() {
        let path = tmp_ring_path("lock-timeout");
        let g = proto_geometry();
        let held = OpenTransactionLock::acquire_with_wait_hook(&path, || {}).unwrap();
        let path_for_waiter = path.clone();
        let waiter = std::thread::spawn(move || attach_or_create(&path_for_waiter, g));
        let err = match waiter.join().unwrap() {
            Ok(_) => panic!("waiter must not bypass the held transaction lock"),
            Err(err) => err,
        };
        assert_eq!(err.raw_os_error(), Some(libc::EAGAIN));
        assert!(
            !std::path::Path::new(&path).exists(),
            "lock timeout must not touch the ring pathname"
        );
        drop(held);

        let map = attach_or_create(&path, g).expect("closing lock fd releases ownership");
        drop(map);
        cleanup(&path);
    }

    #[test]
    fn torn_init_no_magic_is_rejected() {
        // Simulate a creator crash mid-init: a full-size file exists but magic
        // was never written. An attacher must NOT read it as a valid ring.
        let path = tmp_ring_path("torn");
        let g = proto_geometry();
        // Hand-build a zeroed, full-size file (no magic).
        let file = std::fs::OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(&path)
            .unwrap();
        file.set_len(g.file_size() as u64).unwrap();
        drop(file);
        // Attach must reject (magic never appears within the bounded wait).
        // Not an owned /dev/shm path, so it errors rather than reclaiming.
        let err = match RingReader::create_or_attach(&path, g) {
            Ok(_) => panic!("torn-init file (no magic) should be rejected"),
            Err(e) => e,
        };
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        cleanup(&path);
    }

    #[test]
    fn attacher_waits_for_live_creator_before_ftruncate() {
        // Hold a real O_EXCL-created inode at size zero. A test hook inside the
        // production size-wait loop reports the competitor fd's dev/inode/size
        // and blocks. Only after this thread proves that the competitor opened
        // the exact zero-size original inode does it initialize the creator and
        // release the competitor. No sleep establishes the race ordering.
        let path = tmp_ring_path("pre-ftruncate-race");
        let g = proto_geometry();
        let creator_file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        let creator_metadata = creator_file.metadata().unwrap();
        assert_eq!(creator_metadata.len(), 0);
        let creator_fd = creator_file.into_raw_fd();
        let attach_fd = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap()
            .into_raw_fd();

        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let attacher = std::thread::spawn(move || {
            attach_existing_with_size_wait_hook(attach_fd, g, move |_, st| {
                #[cfg(target_os = "macos")]
                let device = st.st_dev as u64;
                #[cfg(not(target_os = "macos"))]
                let device = st.st_dev;
                entered_tx.send((device, st.st_ino, st.st_size)).unwrap();
                release_rx.recv().unwrap();
            })
        });
        let (attach_dev, attach_ino, attach_size) = entered_rx
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("competitor must report entry into the production size-wait loop");
        assert_eq!(
            attach_size, 0,
            "competitor must observe the pre-ftruncate inode"
        );
        assert_eq!(attach_dev, creator_metadata.dev());
        assert_eq!(attach_ino, creator_metadata.ino());

        let creator_map = init_created(creator_fd, g).unwrap();
        release_tx
            .send(())
            .expect("release competitor after creator publishes magic");
        let attached_map = match attacher.join().unwrap() {
            Ok(map) => map,
            Err(_) => panic!("attacher must wait for the live creator"),
        };

        let final_metadata = std::fs::metadata(&path).unwrap();
        assert_eq!(final_metadata.dev(), creator_metadata.dev());
        assert_eq!(final_metadata.ino(), creator_metadata.ino());

        // A shared atomic round-trip proves both mappings still name the O_EXCL
        // winner's inode rather than a split-brain replacement.
        creator_map
            .header_atomic(layout::OFF_WRITE_SEQ)
            .store(37, Ordering::Release);
        assert_eq!(
            attached_map
                .header_atomic(layout::OFF_WRITE_SEQ)
                .load(Ordering::Acquire),
            37
        );
        drop(attached_map);
        drop(creator_map);
        cleanup(&path);
    }

    #[test]
    fn owned_reclaim_enoent_retries_after_concurrent_reclaimer() {
        // The injected ENOENT removes the torn inode first, exactly as another
        // reclaimer winning the race would. This opener must retry and create a
        // valid replacement rather than failing like the EACCES case below.
        let path = tmp_ring_path("reclaim-enoent");
        let g = proto_geometry();
        let torn_metadata = create_full_size_torn_ring(&path, g);
        let _hooks = OwnedReclaimTestGuard::arm(&path, libc::ENOENT);

        let reader = RingReader::create_or_attach(&path, g)
            .expect("concurrent-reclaimer ENOENT must retry create/attach");
        let replacement_metadata = std::fs::metadata(&path).unwrap();
        assert_ne!(
            (replacement_metadata.dev(), replacement_metadata.ino()),
            (torn_metadata.dev(), torn_metadata.ino()),
            "retry must map a replacement for the concurrently removed torn inode"
        );
        assert_eq!(
            reader
                .map
                .header_atomic(layout::OFF_MAGIC_QWORD)
                .load(Ordering::Acquire) as u32,
            MAGIC
        );
        drop(reader);
        cleanup(&path);
    }

    #[test]
    fn owned_reclaim_eacces_fails_closed_without_retry() {
        let path = tmp_ring_path("reclaim-eacces");
        let g = proto_geometry();
        let torn_metadata = create_full_size_torn_ring(&path, g);
        let _hooks = OwnedReclaimTestGuard::arm(&path, libc::EACCES);

        let err = match RingReader::create_or_attach(&path, g) {
            Ok(_) => panic!("permission-denied reclaim must fail closed"),
            Err(err) => err,
        };
        assert_eq!(err.raw_os_error(), Some(libc::EACCES));
        let preserved_metadata = std::fs::metadata(&path).unwrap();
        assert_eq!(preserved_metadata.dev(), torn_metadata.dev());
        assert_eq!(preserved_metadata.ino(), torn_metadata.ino());
        cleanup(&path);
    }

    #[test]
    fn owned_ring_path_reclaim_is_narrow() {
        assert!(is_owned_ring_path("/dev/shm/jts-ring/content.ring"));
        assert!(!is_owned_ring_path("/dev/shm/jts-ring/nested/content.ring"));
        assert!(!is_owned_ring_path("/tmp/jts-ring/content.ring"));
        assert!(!is_owned_ring_path("/dev/shm/content.ring"));
    }

    #[test]
    fn create_race_second_opener_attaches() {
        // Two create_or_attach on the same path: the first creates, the second
        // must attach (not error on EEXIST) and agree on geometry.
        let path = tmp_ring_path("race");
        let g = proto_geometry();
        let _reader = RingReader::create_or_attach(&path, g).unwrap();
        let writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        assert_eq!(writer.write_seq(), 0);
        cleanup(&path);
    }

    #[test]
    fn reader_drop_only_clears_its_own_pid() {
        // N2 regression: RingReader::drop must clear reader_pid ONLY if it is
        // still ours. If a second reader has attached (stamping its own pid),
        // this reader dropping must not clear the new reader's presence — else
        // the writer would wrongly free-run-drop against a live reader. Mirror
        // of the C writer_close `cur == mine` guard.
        let path = tmp_ring_path("readerpid");
        let g = proto_geometry();
        let reader = RingReader::create_or_attach(&path, g).unwrap();
        // Our own attach stamped reader_pid to this process id.
        let ours = std::process::id() as u64;
        assert_eq!(
            reader
                .map
                .header_atomic(layout::OFF_READER_PID)
                .load(Ordering::Relaxed),
            ours
        );
        // Simulate a second reader taking over: stamp a foreign pid.
        let foreign = ours.wrapping_add(1);
        reader
            .map
            .header_atomic(layout::OFF_READER_PID)
            .store(foreign, Ordering::Relaxed);
        // Read the header slot out before dropping (drop munmaps our mapping),
        // by attaching a second mapping to the same file.
        let checker = RingReader::create_or_attach(&path, g).unwrap();
        // checker's attach re-stamped reader_pid to `ours` again — reset it to
        // the foreign value to model "a different reader currently owns it".
        checker
            .map
            .header_atomic(layout::OFF_READER_PID)
            .store(foreign, Ordering::Relaxed);
        drop(reader); // must NOT clear reader_pid (it is `foreign`, not `ours`)
        assert_eq!(
            checker
                .map
                .header_atomic(layout::OFF_READER_PID)
                .load(Ordering::Relaxed),
            foreign,
            "dropping a reader must not clear a foreign reader_pid"
        );
        drop(checker);
        cleanup(&path);
    }
}
