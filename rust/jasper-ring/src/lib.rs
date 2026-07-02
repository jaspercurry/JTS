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
//! constants the C header (`jts_ring_shm.h`) `_Static_assert`s — the
//! cross-language drift guard.
//!
//! **This is a prototype and flag-gated everywhere.** Nothing here runs unless
//! `JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring` is set (outputd's default is
//! `direct`). The crate has no product callers.
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
//! | 24 | n_slots | u32 | 2 (prototype), 3-4 allowed |
//! | 28 | _pad | u32 | zero |
//! | 32 | writer_epoch | atomic u64 | ++ (Release) per writer attach; reader counts `epoch_resets` on change |
//! | 40 | write_seq | atomic u64 | total slots PUBLISHED, monotonic across epochs for the file's lifetime |
//! | 48 | read_seq | atomic u64 | total slots CONSUMED; written only by the reader |
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
//! **Writer publish** (slot `W = write_seq`, implemented in C; documented here
//! because the ordering is the shared contract):
//! 1. `R = load(read_seq, Acquire)`; require `W - R < n_slots` (space). The
//!    Acquire pairs with the reader's Release of `read_seq`, so the writer's
//!    payload stores into slot `W % n_slots` cannot be reordered before the
//!    reader has finished copying that slot out.
//! 2. memcpy payload into slot `W % n_slots` (plain stores).
//! 3. `store(write_seq, W+1, Release)` — publishes: a reader whose Acquire load
//!    observes `write_seq > W` observes the complete slot payload.
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
//! **Torn-write safety:** a slot is only ever touched by one side at a time
//! (writer needs `W - R < n_slots`; reader needs `W > R`). A writer crash
//! mid-memcpy leaves `write_seq` unbumped — the garbage slot is never
//! readable. A creator crash mid-init leaves `magic` unset — attachers spin
//! <=100 ms for magic then error; the reader unlinks-and-recreates a
//! magic-invalid file under its owned `/dev/shm/jts-ring/` path (narrow-path
//! check, mirroring outputd's `is_owned_runtime_pipe_path`).
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
use std::sync::atomic::{AtomicU64, Ordering};

pub mod layout;

pub use layout::{
    Geometry, HEADER_BYTES, MAGIC, MAX_N_SLOTS, MIN_N_SLOTS, SAMPLE_FORMAT_S16LE,
    SAMPLE_FORMAT_S32LE, VERSION,
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
pub const WRITER_LIVENESS_TIMEOUT_NS: u64 = 2_000_000_000;

/// Bounded spin for the creator's magic to appear during attach.
const MAGIC_WAIT_TIMEOUT_MS: u64 = 100;
const MAGIC_WAIT_STEP_US: u64 = 200;

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
    /// mirror of `read_seq` in the header (which only this reader writes).
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
        // free-runs (drops frames) rather than blocking on a dead reader.
        self.map
            .header_atomic(layout::OFF_READER_PID)
            .store(0, Ordering::Relaxed);
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

/// `O_EXCL` create (init + magic-last) or attach (bounded magic wait + geometry
/// validation). A magic-invalid file under the owned `/dev/shm/jts-ring/` root
/// is unlinked and recreated.
fn attach_or_create(path: &str, expected: Geometry) -> io::Result<RingMapping> {
    ensure_parent_dir(path)?;
    let c_path = std::ffi::CString::new(path)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "ring path contains NUL"))?;

    loop {
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
                Ok(map) => return Ok(map),
                Err(e) => {
                    // Creation failed mid-init; drop the half-baked file so the
                    // next opener does not attach to a magic-less carcass.
                    unsafe { libc::close(create_fd) };
                    let _ = std::fs::remove_file(path);
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
        match attach_existing(fd, expected) {
            Ok(map) => return Ok(map),
            Err(AttachError::Fatal(e)) => {
                unsafe { libc::close(fd) };
                return Err(e);
            }
            Err(AttachError::MagicInvalid) => {
                unsafe { libc::close(fd) };
                // A creator crashed mid-init (magic never appeared). Only the
                // owner may reclaim, and only under the narrow owned path.
                if !is_owned_ring_path(path) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("ring {path:?} has no valid magic and is not reclaimable"),
                    ));
                }
                std::fs::remove_file(path)?;
                eprintln!("event=outputd.shm_ring.reclaimed_magic_invalid path={path}");
                // Loop back and re-create.
            }
        }
    }
}

enum AttachError {
    Fatal(io::Error),
    /// The magic never appeared within the bounded wait (creator crashed
    /// mid-init). Reclaimable under the owned path.
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
    // Seqs/epoch/pids/heartbeats start at 0 (ftruncate zeroed them). The reader
    // is the creator here; its create_or_attach caller stamps reader_pid.
    // Publish magic LAST with Release so an attacher that observes the magic
    // observes the fully-initialized header (version already written above; the
    // Release store preserves it in the qword's high half).
    write_u32_release_magic(&map);
    Ok(map)
}

fn attach_existing(fd: RawFd, expected: Geometry) -> Result<RingMapping, AttachError> {
    // fstat the file to learn its ACTUAL size. We map the actual size, never
    // the expected size: mmapping past EOF would SIGBUS on access, and a
    // genuinely-smaller valid ring (a geometry mismatch) must be readable so we
    // can name the mismatch honestly rather than misreport it as "still
    // growing." A file too small to hold even the header is mid-init.
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut st) } < 0 {
        return Err(AttachError::Fatal(io::Error::last_os_error()));
    }
    let actual_size = st.st_size as u64;
    if actual_size < HEADER_BYTES as u64 {
        // The creator has not finished ftruncate/init yet (or it is not a ring
        // at all). Treat as magic-invalid: reclaimable under the owned path,
        // fatal otherwise.
        return Err(AttachError::MagicInvalid);
    }
    let actual_size = actual_size as usize;

    // Map the ACTUAL bytes with the expected geometry recorded only for slot
    // math; the header's own declared geometry is validated below before any
    // slot is indexed, so a mismatch fails loud before slot math runs.
    let map = match mmap_fd(fd, actual_size, expected) {
        Ok(m) => m,
        Err(e) => return Err(AttachError::Fatal(e)),
    };

    // Bounded wait for the creator's magic. No magic within the window means
    // the creator crashed mid-init (or this is not a ring).
    if !wait_for_magic(&map) {
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

fn wait_for_magic(map: &RingMapping) -> bool {
    let deadline_ns = monotonic_ns() + MAGIC_WAIT_TIMEOUT_MS * 1_000_000;
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
        let ts = libc::timespec {
            tv_sec: 0,
            tv_nsec: (MAGIC_WAIT_STEP_US * 1000) as _,
        };
        unsafe { libc::nanosleep(&ts, std::ptr::null_mut()) };
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

/// The reader may only unlink-and-recreate a magic-invalid file directly under
/// the owned `/dev/shm/jts-ring/` root — a narrow-path check mirroring outputd's
/// `is_owned_runtime_pipe_path`. A nested or foreign path is never reclaimed.
fn is_owned_ring_path(path: &str) -> bool {
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
/// publish discipline the C writer implements (space check with Acquire,
/// payload memcpy, Release publish), so the cross-language interop the bench
/// proves on-Pi is exercised in-process here too.
///
/// Not a product path — the real writer is the C ioplug. Gated behind the
/// public API but intended for test/bench use only.
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
        if let Some(parent) = std::path::Path::new(path).parent() {
            let _ = std::fs::remove_dir(parent);
        }
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
}
