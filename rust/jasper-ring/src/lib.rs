// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! JTS Ring B — SPSC ping-pong SHM frame ring (reader + shared header/seq logic).
//!
//! # What this is
//!
//! Ring B replaces the outputd *content* snd-aloop hop
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
//! at it.
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
//! | 12 | channels | u32 | 2..=8 ([`MAX_RING_CHANNELS`]) |
//! | 16 | sample_format | u32 | 1 = S16LE ([`SAMPLE_FORMAT_S16LE`]), 2 = S32LE ([`SAMPLE_FORMAT_S32LE`]) |
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
//!    is a hard error: a fatal attach failure out of the field-by-field header
//!    compare, surfaced to the caller as an `io::Error`. This crate's contract
//!    ends there — each daemon owns how it maps that error to an exit code, and
//!    each unit owns what that exit code does. A magic-invalid owned file is
//!    unlinked and recreated. A transient empty ring is silence, never a crash.
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
    Geometry, HEADER_BYTES, MAGIC, MAX_N_SLOTS, MAX_RING_CHANNELS, MAX_SLOT_BYTES, MIN_N_SLOTS,
    RING_SLOT_FRAMES, SAMPLE_FORMAT_S16LE, SAMPLE_FORMAT_S32LE, VERSION,
};
pub use writer::{
    PublishOutcome, ReaderLiveness, RingWriter, WriterMetrics, MAX_FULL_WAIT_TICKS,
    STUCK_READER_GRACE_NS,
};

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
///
/// **The heartbeat owns OBSERVABILITY; the lock owns EXCLUSIVITY** (U3/P6a).
/// The C ioplug's writer holds an exclusive `flock` on `<ring>.writer.lock` for
/// the life of its mapping, so this window decides only what a reader REPORTS
/// and when a blocked writer gives up — never who may write a ring. The split
/// matters because a heartbeat is stamped on PUBLISH: it correctly reports
/// "nothing is flowing" the instant a renderer pauses, which is exactly what
/// makes it wrong as an ownership test, since a paused renderer still owns its
/// device. So `writer_alive:false` on an attached ring is an ordinary paused
/// source, NOT an invitation to take the ring.
///
/// This crate's [`RingWriter`] deliberately takes no such lock: fan-in owns
/// Ring A by construction and has no second opener, so there is nothing to
/// exclude. A ring whose writer is this crate is therefore guarded by the
/// heartbeat alone, which is why the C reader-side guard still consults it.
///
/// **The window moved; it did not vanish.** A SIGKILLed writer's flock drops
/// with its fd, but SIGKILL leaves `writer_pid` stamped and the heartbeat frozen
/// at its last publish — so for up to this window the C secondary guard still
/// refuses a fresh writer. A ring-writing renderer's `RestartSec` must therefore
/// exceed it: librespot's 5 s clears it, and `jasper-camilla`'s 2 s sits on the
/// boundary for Ring B.
pub const WRITER_LIVENESS_TIMEOUT_NS: u64 = 2_000_000_000;

/// One bounded attach budget for the creator's ftruncate + magic publish.
const MAGIC_WAIT_TIMEOUT_MS: u64 = 100;
const MAGIC_WAIT_STEP_US: u64 = 200;
const OPEN_LOCK_SUFFIX: &str = ".open.lock";
const OPEN_LOCK_MODE: u32 = 0o660;
const OPEN_LOCK_WAIT_TIMEOUT_MS: u64 = 500;
const OPEN_LOCK_WAIT_STEP_US: u64 = 1_000;
const OPEN_MAX_ATTEMPTS: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RingRole {
    Reader,
    Writer,
}

impl RingRole {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Reader => "reader",
            Self::Writer => "writer",
        }
    }
}

fn ring_event(role: RingRole, suffix: &str) -> String {
    format!("jts_ring.{}.{suffix}", role.as_str())
}

/// Adjacent, persistent advisory lock for one complete open transaction.
///
/// This is deliberately a separate inode from the replaceable ring path. C and
/// Rust both hold `<ring path>.open.lock` across classification, conditional
/// reclaim, create, initialization, and final linked-path ownership proof.
struct OpenTransactionLock {
    fd: RawFd,
}

impl OpenTransactionLock {
    fn acquire_with_wait_hook<F>(path: &str, role: RingRole, mut on_wait: F) -> io::Result<Self>
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
                eprintln!(
                    "event={} path={path}",
                    ring_event(role, "open_lock_exhausted")
                );
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

fn stat_value_as_u64<T>(value: T, field: &'static str) -> io::Result<u64>
where
    T: TryInto<u64>,
    T::Error: std::fmt::Display,
{
    value.try_into().map_err(|e| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("fstat {field} cannot be represented as u64: {e}"),
        )
    })
}

fn fd_identity(fd: RawFd) -> io::Result<FileIdentity> {
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut st) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(FileIdentity {
        dev: stat_value_as_u64(st.st_dev, "st_dev")?,
        ino: stat_value_as_u64(st.st_ino, "st_ino")?,
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
        // PANIC-AUDITED: every call site passes a fixed layout::OFF_* constant
        debug_assert!(offset + 8 <= HEADER_BYTES);
        // PANIC-AUDITED: the layout::OFF_* constants are 8-byte aligned by construction
        debug_assert_eq!(offset % 8, 0);
        // SAFETY: offset is within the header (< HEADER_BYTES), 8-byte aligned,
        // and the mmap base is page-aligned, so the pointer is valid and
        // aligned for an 8-byte atomic. The mapping outlives the reference.
        unsafe { AtomicU64::from_ptr(self.base.add(offset) as *mut u64) }
    }

    /// Read a plain u32 header field (rate/channels/etc are init-only; the
    /// reader validated them at attach and never mutates them).
    fn header_u32(&self, offset: usize) -> u32 {
        // PANIC-AUDITED: every call site passes a fixed layout::OFF_* constant
        debug_assert!(offset + 4 <= HEADER_BYTES);
        // SAFETY: offset within header, 4-byte read from a valid mapping.
        unsafe { std::ptr::read_unaligned(self.base.add(offset) as *const u32) }
    }

    /// Debug-only tripwire for the `i16`-typed wrappers: each one measures its
    /// buffer in `samples_per_slot` 2-byte samples, which is exactly one slot on
    /// an S16LE ring and the wrong size on any other. Stated once here and
    /// called from all three wrappers rather than repeated at each.
    pub(crate) fn debug_assert_s16_typed_view(&self) {
        // PANIC-AUDITED: debug-only tripwire; fanin's wire-format tests are the real guard
        debug_assert_eq!(
            self.geometry.sample_format,
            layout::SAMPLE_FORMAT_S16LE,
            "the i16-typed slot view is only valid on an S16LE ring"
        );
    }

    /// The same tripwire for the `i32`-typed wrapper: `samples_per_slot` 4-byte
    /// samples is one slot on an S32LE ring and the wrong size on any other.
    pub(crate) fn debug_assert_s32_typed_view(&self) {
        // PANIC-AUDITED: debug-only tripwire; fanin's wide wire-format tests are the real guard
        debug_assert_eq!(
            self.geometry.sample_format,
            layout::SAMPLE_FORMAT_S32LE,
            "the i32-typed slot view is only valid on an S32LE ring"
        );
    }

    /// One slot's payload size in bytes. Infallible here: every path that
    /// produces a `RingMapping` validated the geometry first.
    fn slot_bytes(&self) -> usize {
        self.geometry
            .slot_bytes()
            // PANIC-AUDITED: the geometry is validated at attach, before any RingMapping can exist
            .expect("mapped ring geometry was validated before slot access")
    }

    fn slot_ptr(&self, slot_index: u32) -> *const u8 {
        let slot_bytes = self.slot_bytes();
        let off = HEADER_BYTES + (slot_index as usize) * slot_bytes;
        // PANIC-AUDITED: slot_index is seq % n_slots and the mapping is sized for n_slots
        debug_assert!(off + slot_bytes <= self.len);
        // SAFETY: slot_index < n_slots (caller guarantees via seq % n_slots)
        // and the mapping is sized HEADER_BYTES + n_slots*slot_bytes.
        unsafe { self.base.add(off) }
    }

    /// Stamp a writer attach and return the file-lifetime write sequence.
    /// Both the production writer and the non-blocking test writer use this
    /// exact epoch/pid/heartbeat ordering.
    pub(crate) fn attach_writer(&self) -> u64 {
        let write_seq = self
            .header_atomic(layout::OFF_WRITE_SEQ)
            .load(Ordering::Acquire);
        let epoch = self
            .header_atomic(layout::OFF_WRITER_EPOCH)
            .load(Ordering::Acquire);
        self.header_atomic(layout::OFF_WRITER_EPOCH)
            .store(epoch + 1, Ordering::Release);
        self.header_atomic(layout::OFF_WRITER_PID)
            .store(std::process::id() as u64, Ordering::Relaxed);
        self.header_atomic(layout::OFF_WRITER_HEARTBEAT_NS)
            .store(monotonic_ns(), Ordering::Relaxed);
        write_seq
    }

    /// Copy one complete slot payload at `write_seq`. `payload.len()` must equal
    /// [`RingMapping::slot_bytes`]. The ring core never interprets samples — it
    /// memcpys — so this is the single write primitive for every geometry, and
    /// the sample format only ever decides how many bytes a slot holds.
    /// Publication of `OFF_WRITE_SEQ` remains the caller's Release-store
    /// responsibility.
    pub(crate) fn write_slot_bytes(&self, write_seq: u64, payload: &[u8]) {
        // PANIC-AUDITED: both callers size the payload from the attached geometry
        assert_eq!(
            payload.len(),
            self.slot_bytes(),
            "ring publish requires exactly one complete slot"
        );
        let slot_index = (write_seq % self.geometry.n_slots as u64) as u32;
        // SAFETY: slot_index is modulo validated n_slots, so slot_ptr points at
        // a mapped slot of exactly slot_bytes; the length check above pins the
        // copy to that slot. The mapping and `payload` are distinct allocations,
        // so the regions cannot overlap.
        unsafe {
            let dst = self.slot_ptr(slot_index) as *mut u8;
            std::ptr::copy_nonoverlapping(payload.as_ptr(), dst, payload.len());
        }
    }

    /// S16-typed view of [`RingMapping::write_slot_bytes`], kept so existing
    /// callers compile unchanged; it copies byte-for-byte what the byte path
    /// copies. Valid ONLY on an S16LE geometry — on any other format an `i16`
    /// slice of `samples_per_slot` is the wrong size for a slot. Callers move to
    /// the byte path; this wrapper goes away with the last of them.
    pub(crate) fn write_i16_slot(&self, write_seq: u64, samples: &[i16]) {
        self.debug_assert_s16_typed_view();
        self.write_slot_bytes(write_seq, i16_samples_as_bytes(samples));
    }
}

/// View `i16` samples as the bytes they occupy — the exact in-memory
/// representation the ring stores, so a typed publish and a byte publish of the
/// same samples produce identical slot bytes. Never copies.
fn i16_samples_as_bytes(samples: &[i16]) -> &[u8] {
    // SAFETY: `i16` is a plain integer with no padding and no invalid bit
    // patterns, and `u8` has alignment 1, so any initialized `[i16]` is also a
    // valid `[u8]` of `size_of_val` bytes. The returned borrow is tied to
    // `samples`, so the view cannot outlive it.
    unsafe {
        std::slice::from_raw_parts(
            samples.as_ptr() as *const u8,
            std::mem::size_of_val(samples),
        )
    }
}

/// Mutable counterpart of [`i16_samples_as_bytes`]: a byte-addressable view of a
/// caller's `i16` output buffer, so the byte consume path can fill it directly.
fn i16_samples_as_bytes_mut(samples: &mut [i16]) -> &mut [u8] {
    let bytes = std::mem::size_of_val(samples);
    // SAFETY: as above, plus exclusivity — the `&mut` borrow of `samples` is
    // consumed for the lifetime of the returned view, so no aliasing `[i16]`
    // reference exists while it lives. Every byte pattern is a valid `i16`, so
    // writing arbitrary bytes through the view leaves `samples` initialized.
    unsafe { std::slice::from_raw_parts_mut(samples.as_mut_ptr() as *mut u8, bytes) }
}

/// The `i32` counterpart of [`i16_samples_as_bytes_mut`], for a consumer whose
/// period buffer is spine-scale on an S32LE ring.
fn i32_samples_as_bytes_mut(samples: &mut [i32]) -> &mut [u8] {
    let bytes = std::mem::size_of_val(samples);
    // SAFETY: identical to the `i16` case — `i32` is a plain integer with no
    // padding and no invalid bit patterns, `u8` has alignment 1, and the `&mut`
    // borrow makes the view exclusive for its lifetime.
    unsafe { std::slice::from_raw_parts_mut(samples.as_mut_ptr() as *mut u8, bytes) }
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

// A `RingReader` may be MOVED BETWEEN THREADS by a single owner. It is `Send`
// automatically — every field is (`RingMapping` carries its own hand-written
// `unsafe impl Send`, and the rest are owned scalars and a `String`) — and this
// assertion exists so that stays true across refactors, because a consumer in
// another crate now depends on it: fan-in's `fanin-ring-attacher` thread
// (#2538) performs `create_or_attach` off the render thread and sends the
// finished reader back over an `mpsc` channel. Adding a non-`Send` field here
// would break that build with a trait error pointing at the channel rather than
// at the field that caused it; this fails at the field instead.
//
// It is deliberately NOT a `Sync` claim: the mapping is single-consumer, and
// nothing shares a reader between threads. Handover, not sharing.
const _: fn() = || {
    fn assert_send<T: Send>() {}
    assert_send::<RingReader>();
};

/// The incumbent `reader_pid` iff a LIVE FOREIGN reader already owns this ring:
/// pid stamped, pid not ours, heartbeat younger than
/// [`WRITER_LIVENESS_TIMEOUT_NS`]. A zero pid, a never-stamped heartbeat, a
/// stale one, or our own pid (re-attach) all read as free.
///
/// Predicate, window, and BEST-EFFORT/TOCTOU caveat mirror
/// `foreign_reader_is_live` in `c/jts-ring-ioplug/jts_ring_shm.c`.
fn foreign_reader_is_live(map: &RingMapping, now_ns: u64) -> Option<u64> {
    let pid = map
        .header_atomic(layout::OFF_READER_PID)
        .load(Ordering::Relaxed);
    if pid == 0 || pid == std::process::id() as u64 {
        return None;
    }
    let heartbeat_ns = map
        .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
        .load(Ordering::Relaxed);
    if heartbeat_ns == 0 {
        return None;
    }
    (now_ns.saturating_sub(heartbeat_ns) < WRITER_LIVENESS_TIMEOUT_NS).then_some(pid)
}

impl RingReader {
    /// Attach to an existing ring, or create it if absent, validating against
    /// `expected`. `O_EXCL` create races are resolved by attaching instead.
    ///
    /// On attach the reader resyncs `read_seq = write_seq` (drops the <=
    /// `n_slots` stale slots accumulated while the reader was down; counted
    /// `attach_resyncs`) and stamps `reader_pid`.
    ///
    /// Refuses with `EBUSY` — and stamps nothing — when a live FOREIGN reader
    /// already owns the ring: it is SPSC and tolerates exactly one reader.
    pub fn create_or_attach(path: &str, expected: Geometry) -> io::Result<Self> {
        expected.validate_self()?;
        let map = attach_or_create(path, expected, RingRole::Reader)?;

        // SPSC GUARD: refuse before ANY header store, so a refused attach leaves
        // the incumbent's read_seq + reader_pid exactly as it left them. `EBUSY`
        // is the code the C reader returns for this same refusal — see
        // `jts_ring_reader_open` in `c/jts-ring-ioplug/jts_ring_shm.c`.
        if let Some(other) = foreign_reader_is_live(&map, monotonic_ns()) {
            eprintln!(
                "event={} path={path} existing_reader_pid={other}",
                ring_event(RingRole::Reader, "busy")
            );
            return Err(io::Error::from_raw_os_error(libc::EBUSY));
        }

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

    /// Whether this reader's MAPPING is still the file its path names.
    ///
    /// A ring file can be replaced underneath a live reader — unlinked and
    /// recreated by a writer whose geometry changed, or cleared by the
    /// arm/disarm path — and an mmap survives the unlink. The reader then holds
    /// a perfectly valid mapping of an ORPHANED inode: it reports attached,
    /// reads happily, and receives nothing forever, because the writer is
    /// publishing into a different file at the same name. No counter
    /// distinguishes that from an idle source.
    ///
    /// Compares this mapping's `(dev, ino)` against the path's. `Err` when the
    /// path cannot be stat'd (typically: the file is gone entirely), which the
    /// caller treats the same as a mismatch.
    ///
    /// Costs an `fstat` + a `stat`, so callers sample it on a slow cadence
    /// rather than per period.
    pub fn owns_linked_path(&self) -> io::Result<bool> {
        mapping_owns_linked_path(&self.path, &self.map)
    }

    pub fn metrics(&self) -> RingMetrics {
        self.metrics
    }

    pub fn geometry(&self) -> Geometry {
        self.map.geometry
    }

    /// S16-typed view of [`RingReader::try_consume_slot_bytes`], kept so
    /// existing callers compile unchanged; it copies byte-for-byte what the byte
    /// path copies. `out.len()` must equal `period_frames * channels`. Valid
    /// ONLY on an S16LE geometry — on any other format an `i16` slice of
    /// `samples_per_slot` is the wrong size for a slot. Callers move to the byte
    /// path; this wrapper goes away with the last of them.
    pub fn try_consume_slot(&mut self, out: &mut [i16]) -> SlotRead {
        self.map.debug_assert_s16_typed_view();
        // The byte path checks the length; on an S16LE ring
        // `samples_per_slot * 2 == slot_bytes`, so a second typed check here
        // would assert the same fact twice.
        self.try_consume_slot_bytes(i16_samples_as_bytes_mut(out))
    }

    /// Spine-scale (`i32`) view of [`RingReader::try_consume_slot_bytes`], the
    /// wide sibling of [`RingReader::try_consume_slot`]: it copies byte-for-byte
    /// what the byte path copies. `out.len()` must equal
    /// `period_frames * channels`. Valid ONLY on an S32LE geometry — on any
    /// other format an `i32` slice of `samples_per_slot` is the wrong size for a
    /// slot.
    pub fn try_consume_slot_wide(&mut self, out: &mut [i32]) -> SlotRead {
        self.map.debug_assert_s32_typed_view();
        self.try_consume_slot_bytes(i32_samples_as_bytes_mut(out))
    }

    /// Try to consume exactly one slot into `out` (`out.len()` must equal
    /// [`Geometry::slot_bytes`]). The ring core never interprets samples — it
    /// memcpys — so this one entry point serves every geometry. NEVER blocks:
    /// - slot available -> copies it, advances `read_seq`, returns
    ///   [`SlotRead::Filled`];
    /// - ring empty -> zero-fills `out`, returns [`SlotRead::Empty`].
    ///
    /// Always updates the reader heartbeat and refreshes the writer-liveness
    /// view (so `/state` is honest even on empty periods).
    pub fn try_consume_slot_bytes(&mut self, out: &mut [u8]) -> SlotRead {
        let g = self.map.geometry;
        // PANIC-AUDITED: outputd's only caller sizes out from the attached geometry
        debug_assert_eq!(
            out.len(),
            self.map.slot_bytes(),
            "ring consume requires exactly one complete slot"
        );

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
        let slot_bytes = self.map.slot_bytes();
        // SAFETY: slot_index < n_slots, so slot_ptr points at a mapped slot of
        // exactly slot_bytes valid bytes; `out` is a caller buffer in a distinct
        // allocation, so the regions cannot overlap.
        unsafe {
            copy_slot_bytes(self.map.slot_ptr(slot_index), out, slot_bytes);
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
        let _ = slot.compare_exchange(mine, 0, Ordering::Relaxed, Ordering::Relaxed);
    }
}

/// Copy `bytes` of slot payload from a raw slot pointer into `out`.
///
/// This is the memcpy every consume path bottoms out in. The ring core never
/// interprets samples, so nothing here knows or cares about the sample format;
/// the slot is opaque bytes, laid out by the writer and handed to the caller
/// unchanged.
///
/// # Safety
/// `src` must point to at least `min(bytes, out.len())` valid bytes (the slot
/// payload), and that region must not overlap `out`.
unsafe fn copy_slot_bytes(src: *const u8, out: &mut [u8], bytes: usize) {
    let n = bytes.min(out.len());
    std::ptr::copy_nonoverlapping(src, out.as_mut_ptr(), n);
}

/// `O_EXCL` create (init + magic-last) or attach (bounded size+magic wait +
/// geometry validation). A magic-invalid file under the owned
/// `/dev/shm/jts-ring/` root is unlinked and recreated.
fn attach_or_create(path: &str, expected: Geometry, role: RingRole) -> io::Result<RingMapping> {
    attach_or_create_with_hooks(path, expected, role, || {}, |_| {}, || {})
}

fn attach_or_create_with_hooks<F, G, H>(
    path: &str,
    expected: Geometry,
    role: RingRole,
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
    let _open_lock = OpenTransactionLock::acquire_with_wait_hook(path, role, on_lock_wait)?;

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
                            eprintln!(
                                "event={} path={path}",
                                ring_event(role, "creator_path_lost")
                            );
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
                        "event={} errno={} path={path}",
                        ring_event(role, "reclaim_failed"),
                        e.raw_os_error().unwrap_or(-1)
                    );
                    return Err(e);
                }
                eprintln!(
                    "event={} path={path}",
                    ring_event(role, "reclaimed_magic_invalid")
                );
                // Loop back and re-create.
            }
        }
    }
    eprintln!("event={} path={path}", ring_event(role, "attach_exhausted"));
    Err(io::Error::from_raw_os_error(libc::EAGAIN))
}

enum AttachError {
    Fatal(io::Error),
    /// The creator did not complete ftruncate + magic publication within the
    /// bounded wait. Reclaimable under the owned path.
    MagicInvalid,
}

fn init_created(fd: RawFd, g: Geometry) -> io::Result<RingMapping> {
    let file_size = g.file_size()?;
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
    let declared_size = header_geometry.file_size().map_err(|error| {
        AttachError::Fatal(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("invalid ring header geometry: {error}"),
        ))
    })?;
    if declared_size != actual_size {
        return Err(AttachError::Fatal(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "ring file size {} is inconsistent with its own header geometry \
                 (declares {} bytes: rate={} ch={} fmt={} period={} slots={})",
                actual_size,
                declared_size,
                header_geometry.rate,
                header_geometry.channels,
                header_geometry.sample_format,
                header_geometry.period_frames,
                header_geometry.n_slots,
            ),
        )));
    }

    // Validate every config field against the caller's expectation. Any mismatch
    // is fail-loud: `AttachError::Fatal`, never a reclaim and never a silent
    // reinterpretation of the bytes. What a caller does with that error — an
    // exit code, a park, a fall back to another transport — is the caller's.
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
    // PANIC-AUDITED: every call site passes a fixed layout::OFF_* constant
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

/// `CLOCK_MONOTONIC` nanoseconds — the single monotonic source both ring halves
/// (and the fan-in mixer's stall tracker, which reads it via this re-export)
/// stamp against. Public so the mixer's edge-triggered stall event uses the same
/// clock the writer's `last_read_seq_advance_ns` is stamped with.
pub fn monotonic_ns() -> u64 {
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
/// implement (space check with Acquire, the shared [`RingMapping`] slot copy,
/// Release publish), so the cross-language interop the bench proves on-Pi is
/// exercised in-process here too.
///
/// This is the deliberate NON-BLOCKING test twin: `try_publish_slot` returns
/// `false` on a full ring, whereas the product writers block/poll or free-run —
/// keeping the reader-side tests simple. It is NOT a product path (the product
/// writers are the C ioplug under Ring B and [`writer::RingWriter`] under Ring
/// A). Attach stamping and payload copying are shared with `RingWriter`; only
/// full-ring policy differs. Gated behind the public API but intended for
/// test/bench use only.
pub struct TestRingWriter {
    map: RingMapping,
    write_seq: u64,
}

impl TestRingWriter {
    /// Attach to (or create) the ring as the writer: bump `writer_epoch`, stamp
    /// `writer_pid`, and continue from the stored `write_seq`.
    pub fn create_or_attach(path: &str, expected: Geometry) -> io::Result<Self> {
        expected.validate_self()?;
        let map = attach_or_create(path, expected, RingRole::Writer)?;
        let write_seq = map.attach_writer();
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
        // PANIC-AUDITED: test/bench-only writer with no product caller
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
        self.map.write_i16_slot(w, samples);
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
        // Clear writer_pid only if this process still owns it. A newer writer
        // may have attached and taken ownership before this test twin drops.
        let slot = self.map.header_atomic(layout::OFF_WRITER_PID);
        let mine = std::process::id() as u64;
        let _ = slot.compare_exchange(mine, 0, Ordering::Relaxed, Ordering::Relaxed);
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

    fn assert_open_event_vocabulary(role: RingRole, role_name: &str) {
        for suffix in [
            "open_lock_exhausted",
            "creator_path_lost",
            "reclaim_failed",
            "reclaimed_magic_invalid",
            "attach_exhausted",
        ] {
            assert_eq!(
                ring_event(role, suffix),
                format!("jts_ring.{role_name}.{suffix}")
            );
        }
    }

    #[test]
    fn reader_open_failure_and_exhaustion_events_are_role_qualified() {
        assert_open_event_vocabulary(RingRole::Reader, "reader");
    }

    #[test]
    fn writer_open_failure_and_exhaustion_events_are_role_qualified() {
        assert_open_event_vocabulary(RingRole::Writer, "writer");
    }

    #[test]
    fn stat_identity_values_convert_without_platform_specific_same_type_casts() {
        assert_eq!(stat_value_as_u64(42_u64, "st_ino").unwrap(), 42);
        assert_eq!(stat_value_as_u64(42_i32, "st_dev").unwrap(), 42);
        assert_eq!(
            stat_value_as_u64(-1_i32, "st_dev").unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
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

    fn create_full_size_torn_ring(path: &str, g: Geometry) -> (std::fs::File, std::fs::Metadata) {
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)
            .unwrap();
        file.set_len(g.file_size().unwrap() as u64).unwrap();
        let metadata = file.metadata().unwrap();
        (file, metadata)
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

    fn wide_geometry(sample_format: u32, channels: u32) -> Geometry {
        Geometry {
            rate: 48_000,
            channels,
            sample_format,
            period_frames: 128,
            n_slots: 2,
        }
    }

    /// A ring built S32 and attached expecting S16 must fail on the FIELD
    /// compare, not on the file-size cross-check. Both widened axes can produce
    /// a size difference too, and a size error would still fail the attach — but
    /// it would fail for the wrong reason and would stop naming the field that
    /// actually disagrees. Pin the field-mismatch class itself.
    #[test]
    fn attach_expecting_s16_against_an_s32_ring_is_a_field_mismatch() {
        let path = tmp_ring_path("fmt-mismatch");
        let built = wide_geometry(SAMPLE_FORMAT_S32LE, 2);
        drop(RingReader::create_or_attach(&path, built).unwrap());

        let err = match RingReader::create_or_attach(&path, wide_geometry(SAMPLE_FORMAT_S16LE, 2)) {
            Ok(_) => panic!("attaching S16 to an S32 ring must be fatal"),
            Err(e) => e,
        };
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        assert!(
            err.to_string().contains("sample_format mismatch"),
            "expected the field-by-field compare to name sample_format: {err}"
        );
        cleanup(&path);
    }

    /// Same pin on the channel axis: a 6ch ring attached expecting stereo fails
    /// on the `channels` field compare.
    #[test]
    fn attach_expecting_stereo_against_a_six_channel_ring_is_a_field_mismatch() {
        let path = tmp_ring_path("ch-mismatch");
        let built = wide_geometry(SAMPLE_FORMAT_S16LE, 6);
        drop(RingReader::create_or_attach(&path, built).unwrap());

        let err = match RingReader::create_or_attach(&path, wide_geometry(SAMPLE_FORMAT_S16LE, 2)) {
            Ok(_) => panic!("attaching 2ch to a 6ch ring must be fatal"),
            Err(e) => e,
        };
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        assert!(
            err.to_string().contains("channels mismatch"),
            "expected the field-by-field compare to name channels: {err}"
        );
        cleanup(&path);
    }

    /// The byte API carries a WIDE slot end to end: an S32 / 6ch payload
    /// published by the production writer and consumed by the production reader,
    /// byte-for-byte. The typed wrappers cannot express this geometry at all, so
    /// this is the only path that proves the widened accept-set is usable rather
    /// than merely accepted.
    #[test]
    fn byte_api_roundtrips_a_wide_slot() {
        let path = tmp_ring_path("wide-bytes");
        let g = wide_geometry(SAMPLE_FORMAT_S32LE, 6);
        let slot_bytes = g.slot_bytes().unwrap();
        assert_eq!(slot_bytes, 3072, "128 frames x 6 ch x 4 bytes");

        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let mut out = vec![0u8; slot_bytes];
        // Prime the reader heartbeat so the writer takes the publish path.
        assert_eq!(reader.try_consume_slot_bytes(&mut out), SlotRead::Empty);
        assert!(out.iter().all(|&b| b == 0), "empty must zero-fill");

        let payload: Vec<u8> = (0..slot_bytes).map(|i| (i % 251) as u8).collect();
        assert_eq!(writer.publish_bytes(&payload), PublishOutcome::Published);
        assert_eq!(reader.try_consume_slot_bytes(&mut out), SlotRead::Filled);
        assert_eq!(out, payload);
        assert_eq!(reader.metrics().frames_read, g.period_frames as u64);
        cleanup(&path);
    }

    /// The i16-typed wrappers are byte-identical to the byte path: samples
    /// published through `RingWriter::publish` land in the slot as exactly the
    /// bytes those samples occupy, and read back through
    /// `try_consume_slot_bytes` unchanged. This is what lets the fan-in and
    /// outputd call sites keep their typed signatures while the core goes
    /// byte-oriented.
    #[test]
    fn typed_publish_is_byte_identical_to_the_byte_path() {
        let path = tmp_ring_path("typed-identity");
        let g = proto_geometry();
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let mut out = vec![0u8; g.slot_bytes().unwrap()];
        assert_eq!(reader.try_consume_slot_bytes(&mut out), SlotRead::Empty);

        let samples: Vec<i16> = (0..n)
            .map(|i| (i as i16).wrapping_mul(613).wrapping_sub(7))
            .collect();
        assert_eq!(
            writer.publish(&samples),
            PublishOutcome::Published,
            "typed publish"
        );

        // `to_le_bytes` is the assertion, not `to_ne_bytes`: the ring stores the
        // host's in-memory `i16` bytes and every target this ships on (aarch64,
        // x86) is little-endian, which is what `SAMPLE_FORMAT_S16LE` names. A
        // big-endian host would fail here rather than emit a mislabelled wire.
        let expected: Vec<u8> = samples.iter().flat_map(|s| s.to_le_bytes()).collect();
        assert_eq!(reader.try_consume_slot_bytes(&mut out), SlotRead::Filled);
        assert_eq!(
            out, expected,
            "the typed wrapper must write the samples' own little-endian bytes"
        );
        cleanup(&path);
    }

    #[test]
    fn unsupported_header_format_returns_invalid_data_without_panicking() {
        use std::io::{Seek, SeekFrom, Write};

        let path = tmp_ring_path("unsupported-format");
        let g = proto_geometry();
        drop(TestRingWriter::create_or_attach(&path, g).unwrap());
        let mut file = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
        file.seek(SeekFrom::Start(layout::OFF_SAMPLE_FORMAT as u64))
            .unwrap();
        file.write_all(&u32::MAX.to_le_bytes()).unwrap();
        drop(file);

        let error = match RingReader::create_or_attach(&path, g) {
            Ok(_) => panic!("unsupported header format should be rejected"),
            Err(error) => error,
        };
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(
            error.to_string().contains("unsupported ring sample format"),
            "{error}"
        );
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
                RingRole::Reader,
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
            attach_or_create_with_hooks(
                &path_b,
                g,
                RingRole::Reader,
                || wait_b.send(()).unwrap(),
                |_| {},
                || {},
            )
        });
        let path_c = path.clone();
        let c = std::thread::spawn(move || {
            attach_or_create_with_hooks(
                &path_c,
                g,
                RingRole::Reader,
                || wait_tx.send(()).unwrap(),
                |_| {},
                || {},
            )
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
        let (_torn_file, torn) = create_full_size_torn_ring(&path, g);
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
                RingRole::Reader,
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
            attach_or_create_with_hooks(
                &path_b,
                g,
                RingRole::Reader,
                || wait_b.send(()).unwrap(),
                |_| {},
                || {},
            )
        });
        let path_c = path.clone();
        let c = std::thread::spawn(move || {
            attach_or_create_with_hooks(
                &path_c,
                g,
                RingRole::Reader,
                || wait_tx.send(()).unwrap(),
                |_| {},
                || {},
            )
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
            RingRole::Reader,
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
        let exhausted = attach_or_create(&path, g, RingRole::Reader);
        TEST_FORCE_OPEN_RETRY.with(|slot| slot.set(false));
        let err = match exhausted {
            Ok(_) => panic!("forced retries must exhaust"),
            Err(err) => err,
        };
        assert_eq!(err.raw_os_error(), Some(libc::EAGAIN));

        let map = attach_or_create(&path, g, RingRole::Reader)
            .expect("retry exhaustion must release the transaction lock");
        drop(map);
        cleanup(&path);
    }

    #[test]
    fn lock_timeout_touches_no_ring_and_recovers_after_release() {
        let path = tmp_ring_path("lock-timeout");
        let g = proto_geometry();
        let held =
            OpenTransactionLock::acquire_with_wait_hook(&path, RingRole::Reader, || {}).unwrap();
        let path_for_waiter = path.clone();
        let waiter =
            std::thread::spawn(move || attach_or_create(&path_for_waiter, g, RingRole::Reader));
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

        let map = attach_or_create(&path, g, RingRole::Reader)
            .expect("closing lock fd releases ownership");
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
        file.set_len(g.file_size().unwrap() as u64).unwrap();
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
        let (_torn_file, torn_metadata) = create_full_size_torn_ring(&path, g);
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
        let (_torn_file, torn_metadata) = create_full_size_torn_ring(&path, g);
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
        // Simulate a second reader taking over: stamp a foreign pid. Zero the
        // heartbeat with it — this test is about drop's `cur == mine` guard, and
        // a synthetic foreign pid with a LIVE heartbeat is what the attach
        // exclusivity guard refuses.
        let foreign = ours.wrapping_add(1);
        reader
            .map
            .header_atomic(layout::OFF_READER_PID)
            .store(foreign, Ordering::Relaxed);
        reader
            .map
            .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .store(0, Ordering::Relaxed);
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

    /// Stamp a synthetic incumbent reader into `map`'s header: `pid`, and a
    /// heartbeat `heartbeat_age` in the past (`None` = never stamped). The age is
    /// an INJECTED clock value, so the liveness boundary is exercised without
    /// sleeping. Clamped to 1 so an age exceeding the host's uptime still tests
    /// the age path rather than collapsing onto the never-stamped path.
    fn stamp_incumbent_reader(map: &RingMapping, pid: u64, heartbeat_age: Option<u64>) {
        map.header_atomic(layout::OFF_READER_PID)
            .store(pid, Ordering::Relaxed);
        let heartbeat_ns = match heartbeat_age {
            None => 0,
            Some(age) => monotonic_ns().saturating_sub(age).max(1),
        };
        map.header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .store(heartbeat_ns, Ordering::Relaxed);
    }

    #[test]
    fn reader_attach_refuses_only_a_live_foreign_reader() {
        let ours = std::process::id() as u64;
        let foreign = ours.wrapping_add(1);
        // Coarse ages only — the exact `<` boundary is pinned deterministically
        // in `foreign_reader_liveness_window_is_exclusive_at_the_boundary`, which
        // injects `now_ns` instead of racing the attach's own elapsed time.
        // (tag, incumbent reader_pid, heartbeat age, attach refused?)
        let cases = [
            ("fresh-foreign", foreign, Some(0), true),
            (
                "foreign-stale",
                foreign,
                Some(WRITER_LIVENESS_TIMEOUT_NS * 3),
                false,
            ),
            ("foreign-never-heartbeat", foreign, None, false),
            ("zero-pid-fresh-heartbeat", 0, Some(0), false),
            ("own-pid-fresh-heartbeat", ours, Some(0), false),
        ];

        for (tag, incumbent_pid, heartbeat_age, expect_refusal) in cases {
            let path = tmp_ring_path("foreignreader");
            let g = proto_geometry();
            // Creates the ring and gives us a mapping to stamp the synthetic
            // incumbent through; its own attach stamp is overwritten below.
            let holder = RingReader::create_or_attach(&path, g).unwrap();
            stamp_incumbent_reader(&holder.map, incumbent_pid, heartbeat_age);

            match RingReader::create_or_attach(&path, g) {
                Err(e) => {
                    assert!(expect_refusal, "{tag}: unexpected refusal: {e:?}");
                    assert_eq!(
                        e.raw_os_error(),
                        Some(libc::EBUSY),
                        "{tag}: refusal must carry the C reader's EBUSY"
                    );
                }
                Ok(reader) => {
                    assert!(!expect_refusal, "{tag}: attach should have been refused");
                    assert_eq!(
                        reader
                            .map
                            .header_atomic(layout::OFF_READER_PID)
                            .load(Ordering::Relaxed),
                        ours,
                        "{tag}: a permitted attach takes ownership of reader_pid"
                    );
                    drop(reader);
                }
            }
            drop(holder);
            cleanup(&path);
        }
    }

    #[test]
    fn foreign_reader_liveness_window_is_exclusive_at_the_boundary() {
        let path = tmp_ring_path("foreignwindow");
        let g = proto_geometry();
        let holder = RingReader::create_or_attach(&path, g).unwrap();
        let foreign = (std::process::id() as u64).wrapping_add(1);
        // A fixed heartbeat plus an injected `now_ns`: the age is exact, so the
        // `<` comparison is pinned without any dependence on elapsed real time.
        const HEARTBEAT_NS: u64 = 1_000_000_000_000;
        holder
            .map
            .header_atomic(layout::OFF_READER_PID)
            .store(foreign, Ordering::Relaxed);
        holder
            .map
            .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .store(HEARTBEAT_NS, Ordering::Relaxed);

        let at = |now_ns: u64| foreign_reader_is_live(&holder.map, now_ns);
        assert_eq!(
            at(HEARTBEAT_NS + WRITER_LIVENESS_TIMEOUT_NS - 1),
            Some(foreign),
            "one ns inside the window is still live"
        );
        assert_eq!(
            at(HEARTBEAT_NS + WRITER_LIVENESS_TIMEOUT_NS),
            None,
            "the window is exclusive: age == timeout is dead"
        );
        // A heartbeat stamped after `now_ns` was sampled must clamp to age 0
        // (definitely live), never underflow into a huge age.
        assert_eq!(at(HEARTBEAT_NS - 5), Some(foreign));

        drop(holder);
        cleanup(&path);
    }

    #[test]
    fn refused_attach_leaves_incumbent_reader_state_untouched() {
        // The guard must run before ANY header store: a refused attach that had
        // already resynced read_seq or stamped reader_pid would corrupt the live
        // reader it lost to. Mirrors the C reader, which munmaps and returns
        // -EBUSY before touching either field.
        let path = tmp_ring_path("busynostomp");
        let g = proto_geometry();
        let holder = RingReader::create_or_attach(&path, g).unwrap();
        let foreign = (std::process::id() as u64).wrapping_add(1);
        stamp_incumbent_reader(&holder.map, foreign, Some(0));
        // write_seq is 0, so an attach resync (read_seq = write_seq) would
        // visibly clobber this sentinel.
        const INCUMBENT_READ_SEQ: u64 = 7;
        holder
            .map
            .header_atomic(layout::OFF_READ_SEQ)
            .store(INCUMBENT_READ_SEQ, Ordering::Relaxed);

        let err = match RingReader::create_or_attach(&path, g) {
            Ok(_) => panic!("a live foreign reader must refuse the attach"),
            Err(e) => e,
        };
        assert_eq!(err.raw_os_error(), Some(libc::EBUSY));
        assert_eq!(
            holder
                .map
                .header_atomic(layout::OFF_READ_SEQ)
                .load(Ordering::Relaxed),
            INCUMBENT_READ_SEQ,
            "a refused attach must not resync the incumbent's read_seq"
        );
        assert_eq!(
            holder
                .map
                .header_atomic(layout::OFF_READER_PID)
                .load(Ordering::Relaxed),
            foreign,
            "a refused attach must not steal reader_pid"
        );
        drop(holder);
        cleanup(&path);
    }

    #[test]
    fn test_writer_drop_only_clears_its_own_pid() {
        let path = tmp_ring_path("testwriterpid");
        let g = proto_geometry();
        let writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        let checker = RingReader::create_or_attach(&path, g).unwrap();
        let ours = std::process::id() as u64;
        let foreign = ours.wrapping_add(1);
        checker
            .map
            .header_atomic(layout::OFF_WRITER_PID)
            .store(foreign, Ordering::Relaxed);

        drop(writer);

        assert_eq!(
            checker
                .map
                .header_atomic(layout::OFF_WRITER_PID)
                .load(Ordering::Relaxed),
            foreign,
            "dropping a test writer must not clear a newer writer's pid"
        );
        drop(checker);
        cleanup(&path);
    }
}
