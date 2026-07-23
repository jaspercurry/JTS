// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Ring A — the production SPSC ring **writer** (`RingWriter`).
//!
//! # What this is
//!
//! Ring A flips the roles of Ring B: the WRITER is `jasper-fanin` (this Rust
//! `RingWriter`) and the READER is CamillaDSP via a new capture direction of the
//! `jts_ring` ALSA ioplug (C). The SHM contract v1 header, offsets, seqs,
//! heartbeats, and epoch are byte-identical to Ring B (see the [`crate`] module
//! doc); only the direction of the data flow and which process owns each seq
//! differ. This module adds the production writer half to the crate, which
//! previously shipped only the reader ([`crate::RingReader`]) plus a test-only
//! [`crate::TestRingWriter`].
//!
//! `RingWriter` implements the EXACT publish discipline the C writer
//! (`c/jts-ring-ioplug/jts_ring_shm.c` `jts_ring_writer_publish`) implements, so
//! the two are interchangeable across the SPSC boundary: a `RingWriter` paired
//! with the C reader, or the C writer paired with [`crate::RingReader`], produce
//! and consume the same on-disk bytes with the same memory ordering. The
//! SPSC-pairing tests below exercise `RingWriter` <-> [`crate::RingReader`]
//! in-process; the C-reader interop is proven on-Pi by the ioplug bench.
//!
//! # Publish discipline (mirrors the C writer)
//!
//! Attach = `writer_epoch`++ (Release) + `writer_pid`/`writer_heartbeat_ns`
//! stamp + resume the stored `write_seq` (file-lifetime monotonic). Then per
//! [`RingWriter::publish`]:
//!
//! 1. Stamp `writer_heartbeat_ns` (Relaxed) so the reader's liveness view is
//!    fresh even while we block.
//! 2. Load `read_seq` (Acquire); if `W - R < n_slots` there is space -> memcpy
//!    the payload into slot `W % n_slots` (plain stores), then store
//!    `write_seq = W+1` (Release). The reader's Acquire load of `write_seq`
//!    synchronizes-with this and observes the complete payload.
//! 3. **Full + live reader** (`reader_pid != 0` AND heartbeat younger than
//!    [`crate::WRITER_LIVENESS_TIMEOUT_NS`]): clamped nanosleep (period/4, capped
//!    at 2 ms), re-check up to [`MAX_FULL_WAIT_TICKS`] times. The tick is
//!    `min(period/4, 2 ms)`, so the cap is slot-size dependent: with the fanin
//!    Ring A 128-frame slot at 48 kHz the tick is ~0.667 ms and the cap is
//!    ~21 ms; the ~64 ms figure is only the ≥2 ms-tick worst case (32 × 2 ms).
//!    This is the back-pressure path — the DAC-paced reader drains and frees a
//!    slot.
//!    If the reader heartbeats but never advances `read_seq` past the tick cap,
//!    give up: drop this period and count `stuck_reader_drops`
//!    ([`PublishOutcome::DroppedStuck`]).
//! 4. **Full + dead reader** (`reader_pid == 0` OR stale heartbeat): FREE-RUN by
//!    dropping the OLDEST slot — store `read_seq = R+1` (Release) on the absent
//!    reader's behalf so the new slot has room, then publish over the freed lap
//!    and count `drop_no_reader` ([`PublishOutcome::DroppedNoReader`]). This is
//!    the ONLY path on which the writer touches `read_seq`, keeping occupancy
//!    bounded so a readerless ring never wedges the writer. Identical to the C
//!    writer's b70b22d3 free-run semantics (see the module doc's "Writer
//!    free-run" and "Torn-write safety" for the exactly-one-slot bounded race
//!    with a resuming stale reader).
//!
//! **Sticky-stuck demotion (self-recovery — issue #1524).** A reader that keeps
//! stamping its heartbeat (so it looks live) but never advances `read_seq`
//! (CamillaDSP wedged in Prepared, polling but not calling `readi`) would
//! otherwise pin the writer in the step-3 bounded wait forever, running fan-in at
//! ~1/9 real time and back-pressuring the input lanes until a downstream aplay
//! times out — with no fan-in-side event. So once `read_seq` has not advanced for
//! longer than [`STUCK_READER_GRACE_NS`] we STOP honoring the heartbeat for the
//! block-vs-drop decision and take the step-4 free-run drop-oldest branch instead
//! of waiting ([`PublishOutcome::DroppedStuckDemoted`]). This is one-way and
//! derived, not latched: `last_read_seq_advance_ns` only moves forward on a
//! *reader* advance (never on the writer's own drop-oldest store — see
//! [`RingWriter::note_reader_progress`]), so the age grows monotonically while the
//! reader is frozen and demotion persists; the instant the reader (or a
//! reattached+resynced reader) advances `read_seq` the age resets, the writer
//! resumes honoring the heartbeat, and publishing returns to normal
//! [`PublishOutcome::Published`]. The grace is the false-positive guard: a
//! genuinely DAC-paced reader that back-pressures under a full ring still advances
//! `read_seq` every period, so its age never crosses the grace and it is never
//! demoted. Torn-write safety is unchanged: advancing `read_seq` on a
//! heartbeat-live-but-stuck reader's behalf hits the SAME bounded
//! (at-most-one-slot), self-healing window the crate module doc analyses for a
//! resuming stale reader — the discipline is identical; only the trigger
//! (age-past-grace vs dead-heartbeat) differs.
//!
//! # Counters
//!
//! [`WriterMetrics`] mirrors the C writer's `published_slots` / `drop_no_reader`
//! / `full_waits`, and adds `stuck_reader_drops` (the plan's split of the
//! heartbeat-but-stuck timeout out of `drop_no_reader`; both the bounded-wait
//! give-up [`PublishOutcome::DroppedStuck`] and the sticky demotion
//! [`PublishOutcome::DroppedStuckDemoted`] count here — both mean "a live reader
//! stamped a heartbeat but did not consume this slot"). The daemon reads these
//! for `/state.shm_ring`; occupancy is derived (`write_seq - read_seq`).

use std::io;
use std::sync::atomic::Ordering;

use crate::layout::{self, Geometry};
use crate::{monotonic_ns, RingMapping, RingRole, WRITER_LIVENESS_TIMEOUT_NS};

/// Bounded number of full-ring wait ticks before a live-reader publish gives up
/// and drops (defends against a reader that stamps a heartbeat but never
/// advances `read_seq`). The tick is `min(period/4, 2 ms)`, so the stall cap is
/// slot-size dependent: ~64 ms is only the ≥2 ms-tick worst case (32 × 2 ms);
/// with the fanin 128-frame slot at 48 kHz the tick is ~0.667 ms and the cap is
/// ~21 ms. Same tick-count bound as the C writer's
/// `JTS_RING_MAX_FULL_WAIT_TICKS`.
pub const MAX_FULL_WAIT_TICKS: u32 = 32;

/// The upper clamp on one full-ring wait tick: 1/4 period, never longer than
/// 2 ms. Mirrors the C writer's `clamped_nanosleep`.
const MAX_TICK_NS: u64 = 2_000_000;

/// How long `read_seq` may go without a *reader* advance, while the ring is full
/// and the reader's heartbeat still looks live, before the writer STOPS honoring
/// that heartbeat and free-runs (drop-oldest) instead of back-pressuring — the
/// sticky-stuck demotion (issue #1524). 1 s is chosen to sit well above a normal
/// CamillaDSP reload/reattach turn (sub-second), 5× below the fan-in 5 s progress
/// watchdog, and 12× below the ~12 s downstream correction-lane aplay timeout the
/// old unbounded back-pressure used to trip. It doubles as the fan-in stall
/// EVENT threshold (`RING_STALL_EVENT_NS` in the mixer), so demotion and the
/// edge-triggered observability event fire at the same instant.
pub const STUCK_READER_GRACE_NS: u64 = 1_000_000_000;

/// Outcome of a single [`RingWriter::publish`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublishOutcome {
    /// The slot was published into the ring for a live reader to consume.
    Published,
    /// The ring was full with NO live reader: the writer free-ran, dropping the
    /// oldest slot to stay bounded. The frames were written (so the pointer
    /// stays honest) but no live reader will consume them.
    DroppedNoReader,
    /// The ring was full WITH a live reader that heartbeats but never advanced
    /// `read_seq` within the bounded wait: the writer gave up and dropped this
    /// period rather than stall unboundedly. This is the pre-grace back-pressure
    /// path (each such publish still paid the bounded ~21 ms wait). Sustained
    /// occurrence promotes to [`PublishOutcome::DroppedStuckDemoted`].
    DroppedStuck,
    /// The ring was full WITH a live reader whose `read_seq` has not advanced for
    /// longer than [`STUCK_READER_GRACE_NS`]: the writer DEMOTED the stuck reader
    /// and free-ran (drop-oldest) WITHOUT the ~21 ms wait, restoring real-time
    /// pacing and relieving input back-pressure (issue #1524). Distinct from
    /// [`Self::DroppedStuck`] so the caller can attribute the recovery; counts
    /// toward `stuck_reader_drops` like `DroppedStuck` (both are live-but-stuck).
    DroppedStuckDemoted,
}

/// Writer-side counters for `/state.shm_ring`. Mirrors the C writer's fields
/// plus the plan's `stuck_reader_drops` split.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct WriterMetrics {
    /// Slots published into the ring for a live reader (`PublishOutcome::Published`).
    pub published_slots: u64,
    /// Slots discarded because no live reader was present (free-run drop-oldest).
    pub drop_no_reader: u64,
    /// Slots dropped after a live reader stamped a heartbeat but never advanced
    /// `read_seq` within the bounded wait (`PublishOutcome::DroppedStuck`).
    pub stuck_reader_drops: u64,
    /// Publish attempts that had to wait at least one tick for space (the
    /// live-reader back-pressure path). Counted ONCE per waited publish.
    pub full_waits: u64,
    /// `write_seq - read_seq` at the last publish (0..=n_slots).
    pub occupancy: u64,
    /// n_slots the ring was created/attached with (echoed for /state).
    pub n_slots: u32,
    /// period_frames per slot (echoed for /state).
    pub slot_frames: u32,
}

/// Reader liveness + identity snapshot for stall observability. Reads the header
/// fresh (Relaxed) — the same fields [`RingWriter::reader_is_live`] consults —
/// so the fan-in mixer can label an edge-triggered stall event
/// (`reason=stuck_reader|no_reader`, `reader_pid`, `reader_heartbeat_age_ms`)
/// without duplicating the header layout knowledge.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReaderLiveness {
    /// `reader_pid` (0 = detached).
    pub pid: u64,
    /// Age of `reader_heartbeat_ns` in ms (`u64::MAX` = never stamped).
    pub heartbeat_age_ms: u64,
    /// The reader looks live: `pid != 0` AND heartbeat younger than
    /// [`crate::WRITER_LIVENESS_TIMEOUT_NS`].
    pub live: bool,
}

/// The production writer half of the ring: attaches to (or creates) the SHM
/// file, then publishes one slot per call with the SPSC discipline the C writer
/// and [`crate::RingReader`] agree on. Owns a local `write_seq` mirror and the
/// running counters.
pub struct RingWriter {
    map: RingMapping,
    /// Local mirror of the header `write_seq` (file-lifetime monotonic).
    write_seq: u64,
    /// The highest `read_seq` the writer has already accounted for. Advanced
    /// both by observed READER progress (via [`RingWriter::note_reader_progress`],
    /// which also stamps `last_read_seq_advance_ns`) AND by the writer's own
    /// free-run/demote drop-oldest stores (which do NOT stamp the timestamp), so
    /// the writer never mistakes its own `read_seq` store for reader progress.
    last_read_seq: u64,
    /// `monotonic_ns()` of the last time an observed `read_seq` advance was
    /// attributed to the READER. Seeded at attach; moved forward only by
    /// [`RingWriter::note_reader_progress`]. `now - last_read_seq_advance_ns` is
    /// the "time since the reader last made progress" that drives the
    /// sticky-stuck demotion ([`STUCK_READER_GRACE_NS`]) and the mixer's stall
    /// event.
    last_read_seq_advance_ns: u64,
    metrics: WriterMetrics,
}

// SAFETY: `RingWriter` owns its `RingMapping` (which is `Send`), and the SPSC
// discipline makes this the sole producer; the atomics carry cross-process
// synchronization. A single owner may move it between threads.
unsafe impl Send for RingWriter {}

impl RingWriter {
    /// Attach to (or create) the ring as the WRITER: bump `writer_epoch`
    /// (Release), stamp `writer_pid` + `writer_heartbeat_ns`, and continue from
    /// the stored `write_seq`. Validates the geometry before touching the
    /// filesystem (a mismatch is a fail-loud config error).
    pub fn create_or_attach(path: &str, expected: Geometry) -> io::Result<Self> {
        expected.validate_self()?;
        let map = crate::attach_or_create(path, expected, RingRole::Writer)?;

        // Writer attach: continue from the stored write_seq, epoch++ (Release),
        // stamp pid + heartbeat. Identical to the C writer's attach stamp.
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

        // Seed the stall-tracking state: the read_seq we attach against, and a
        // fresh "reader last advanced" stamp so a freshly-attached writer is
        // never considered stuck (the grace clock starts now).
        let read_seq = map
            .header_atomic(layout::OFF_READ_SEQ)
            .load(Ordering::Acquire);

        let metrics = WriterMetrics {
            n_slots: expected.n_slots,
            slot_frames: expected.period_frames,
            ..WriterMetrics::default()
        };
        Ok(Self {
            map,
            write_seq,
            last_read_seq: read_seq,
            last_read_seq_advance_ns: monotonic_ns(),
            metrics,
        })
    }

    pub fn metrics(&self) -> WriterMetrics {
        self.metrics
    }

    pub fn geometry(&self) -> Geometry {
        self.map.geometry
    }

    /// The writer's local `write_seq` mirror (total slots published across the
    /// file's lifetime).
    pub fn write_seq(&self) -> u64 {
        self.write_seq
    }

    /// Publish exactly one slot from `samples` (`samples.len()` must equal
    /// `period_frames * channels`). Blocks (bounded) on a full ring with a live
    /// reader; free-run drop-oldest on a full ring with a dead reader. ALWAYS
    /// returns within the bounded wait cap (`MAX_FULL_WAIT_TICKS` × the
    /// `min(period/4, 2 ms)` tick — ~21 ms with the fanin 128-frame slot, ~64 ms
    /// only at the ≥2 ms-tick worst case) so the caller's watchdog stays fresh.
    ///
    /// Returns the [`PublishOutcome`] so the caller can self-pace on a dropped
    /// publish (the reader-absent one-period sleep lives in the daemon, not
    /// here — see the plan's `Output::Ring` pacing rule).
    pub fn publish(&mut self, samples: &[i16]) -> PublishOutcome {
        let g = self.map.geometry;
        debug_assert_eq!(samples.len(), g.samples_per_slot());

        // Stamp the heartbeat up front so the reader sees us alive even if we
        // spend the whole call blocking on a full ring.
        let now = monotonic_ns();
        self.map
            .header_atomic(layout::OFF_WRITER_HEARTBEAT_NS)
            .store(now, Ordering::Relaxed);

        let w = self.write_seq;
        let mut waited = 0u32;
        let mut dropped_oldest = false;
        let mut demoted = false;

        loop {
            let now_check = monotonic_ns();
            let r = self
                .map
                .header_atomic(layout::OFF_READ_SEQ)
                .load(Ordering::Acquire);
            // Attribute any READER-driven advance so the stall/demotion clock
            // resets the instant the reader makes progress.
            self.note_reader_progress(r, now_check);
            if w.wrapping_sub(r) < g.n_slots as u64 {
                break; // space available
            }

            // Full. If no live reader, FREE-RUN by dropping the OLDEST slot:
            // advance read_seq on the absent reader's behalf (Release), then
            // publish over the freed lap. This is the only path on which the
            // writer touches read_seq (bounded, self-healing race with a
            // resuming stale reader — see the crate module doc).
            if !self.reader_is_live(now_check) {
                self.free_run_drop_oldest(r);
                dropped_oldest = true;
                break; // room made; publish the new slot over the dropped lap
            }

            // Full WITH a live reader whose read_seq has not advanced for longer
            // than the grace: it is stuck (heartbeat-live but not consuming —
            // issue #1524). DEMOTE it and free-run (drop-oldest) instead of paying
            // the ~21 ms back-pressure wait, restoring real-time pacing and
            // relieving the input back-pressure that wedges the correction-lane
            // aplay. One-way and derived: `last_read_seq_advance_ns` only moves
            // forward on a real reader advance (note_reader_progress), never on
            // the writer's own drop-oldest, so the age grows monotonically while
            // the reader is frozen and the demotion persists until it resumes.
            // Torn-write safety is the SAME bounded/self-healing window the crate
            // module doc analyses for a resuming stale reader.
            if now_check.saturating_sub(self.last_read_seq_advance_ns) > STUCK_READER_GRACE_NS {
                self.free_run_drop_oldest(r);
                dropped_oldest = true;
                demoted = true;
                break;
            }

            // Live and still within grace: back-pressure. Count the wait ONCE,
            // then clamped-nanosleep and re-check up to the bounded tick cap.
            if waited == 0 {
                self.metrics.full_waits = self.metrics.full_waits.saturating_add(1);
            }
            waited += 1;
            if waited > MAX_FULL_WAIT_TICKS {
                // A reader that heartbeats but has not advanced yet, still inside
                // the grace: drop this period rather than stall unboundedly. A
                // sustained stall promotes to the demotion branch above once the
                // grace elapses (removing this bounded wait).
                self.metrics.stuck_reader_drops = self.metrics.stuck_reader_drops.saturating_add(1);
                self.metrics.occupancy = w.wrapping_sub(r);
                return PublishOutcome::DroppedStuck;
            }
            clamped_nanosleep(g.period_frames);
            // Refresh the heartbeat while waiting so the reader keeps seeing us.
            self.map
                .header_atomic(layout::OFF_WRITER_HEARTBEAT_NS)
                .store(monotonic_ns(), Ordering::Relaxed);
        }

        // Space confirmed (or made by drop-oldest). memcpy the payload into slot
        // (w % n_slots) with plain stores, then store write_seq+1 (Release) so
        // the reader's Acquire load of write_seq sees the complete payload.
        let slot_index = (w % g.n_slots as u64) as u32;
        // SAFETY: slot_index < n_slots; samples.len() == samples_per_slot; the
        // slot payload is exactly slot_bytes == samples_per_slot * 2 bytes.
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

        // Occupancy after publish: write_seq - read_seq. Re-read read_seq (it may
        // have advanced while we wrote); a live reader only ever shrinks it.
        let r_after = self
            .map
            .header_atomic(layout::OFF_READ_SEQ)
            .load(Ordering::Acquire);
        self.metrics.occupancy = next.wrapping_sub(r_after);

        if demoted {
            // Sticky-stuck demotion: the frames were written (pointer stays
            // honest) but a stuck live reader will not consume them. Count as a
            // stuck-reader drop (like DroppedStuck — both are "a live reader
            // stamped a heartbeat but did not consume") and report the DISTINCT
            // outcome so the caller can attribute the self-recovery.
            self.metrics.stuck_reader_drops = self.metrics.stuck_reader_drops.saturating_add(1);
            PublishOutcome::DroppedStuckDemoted
        } else if dropped_oldest {
            // A free-run drop-oldest still WROTE the payload and advanced
            // write_seq (pointer stays honest), but the displaced frames will
            // never reach a reader — report it as a no-reader drop, not a
            // published-to-a-reader slot.
            self.metrics.drop_no_reader = self.metrics.drop_no_reader.saturating_add(1);
            PublishOutcome::DroppedNoReader
        } else {
            self.metrics.published_slots = self.metrics.published_slots.saturating_add(1);
            PublishOutcome::Published
        }
    }

    /// Record an observed `read_seq` value. If it advanced past the highest value
    /// the writer has accounted for, a live READER consumed a slot: move the
    /// high-water mark and stamp `last_read_seq_advance_ns`, resetting the
    /// stall/demotion clock. The writer's OWN drop-oldest stores are folded into
    /// `last_read_seq` by [`RingWriter::free_run_drop_oldest`] WITHOUT stamping,
    /// so they are never mistaken for reader progress — the guard that keeps the
    /// sticky demotion from self-clearing on its own free-run advances.
    fn note_reader_progress(&mut self, observed_read_seq: u64, now_ns: u64) {
        if observed_read_seq > self.last_read_seq {
            self.last_read_seq = observed_read_seq;
            self.last_read_seq_advance_ns = now_ns;
        }
    }

    /// Advance `read_seq` by one on the (dead or demoted) reader's behalf so a
    /// full ring frees a slot, and account for that store in `last_read_seq`
    /// WITHOUT stamping `last_read_seq_advance_ns` — the writer's own drop must
    /// never read as reader progress. `r` is the `read_seq` the caller just
    /// loaded under the Acquire.
    fn free_run_drop_oldest(&mut self, r: u64) {
        let advanced = r.wrapping_add(1);
        self.map
            .header_atomic(layout::OFF_READ_SEQ)
            .store(advanced, Ordering::Release);
        if advanced > self.last_read_seq {
            self.last_read_seq = advanced;
        }
    }

    /// Nanoseconds since a READER last advanced `read_seq` (or since attach if it
    /// never has). Grows monotonically while the reader is frozen; resets to ~0
    /// the instant the reader consumes a slot. The fan-in mixer reads this to
    /// drive the edge-triggered stall event; it is also what the sticky demotion
    /// compares against [`STUCK_READER_GRACE_NS`] internally.
    pub fn ns_since_read_seq_advance(&self) -> u64 {
        monotonic_ns().saturating_sub(self.last_read_seq_advance_ns)
    }

    /// The reader's live liveness/identity snapshot for stall observability — the
    /// `reader_pid`, heartbeat age, and live verdict the mixer stamps into a
    /// stall event's `reason` / `reader_pid` / `reader_heartbeat_age_ms` fields.
    pub fn reader_liveness(&self) -> ReaderLiveness {
        let now = monotonic_ns();
        let pid = self
            .map
            .header_atomic(layout::OFF_READER_PID)
            .load(Ordering::Relaxed);
        let hb = self
            .map
            .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .load(Ordering::Relaxed);
        let heartbeat_age_ms = if hb == 0 {
            u64::MAX
        } else {
            now.saturating_sub(hb) / 1_000_000
        };
        ReaderLiveness {
            pid,
            heartbeat_age_ms,
            live: self.reader_is_live(now),
        }
    }

    /// Test-only seam: pretend the reader's last `read_seq` advance happened
    /// `elapsed_ns` ago, so a caller in another crate (the fan-in mixer's
    /// realtime-recovery test) can drive the sticky demotion across
    /// [`STUCK_READER_GRACE_NS`] deterministically without a real wall-clock
    /// wait. Never called in production.
    #[doc(hidden)]
    pub fn set_read_seq_advance_age_for_test(&mut self, elapsed_ns: u64) {
        self.last_read_seq_advance_ns = monotonic_ns().saturating_sub(elapsed_ns);
    }

    /// Free slots available for a non-blocking publish (`n_slots - (W - R)`).
    /// Exposed for the daemon's poll/observability; publish itself never relies
    /// on this (it re-reads `read_seq` under the Acquire).
    pub fn free_slots(&self) -> u64 {
        let r = self
            .map
            .header_atomic(layout::OFF_READ_SEQ)
            .load(Ordering::Acquire);
        (self.map.geometry.n_slots as u64).saturating_sub(self.write_seq.wrapping_sub(r))
    }

    /// True iff a reader is currently live: `reader_pid != 0` AND its heartbeat
    /// is younger than [`crate::WRITER_LIVENESS_TIMEOUT_NS`]. Mirrors the C
    /// writer's `reader_is_live`, including the saturating age (a future
    /// heartbeat clamps to 0 = definitely live).
    pub fn reader_is_live_now(&self) -> bool {
        self.reader_is_live(monotonic_ns())
    }

    fn reader_is_live(&self, now_ns: u64) -> bool {
        let pid = self
            .map
            .header_atomic(layout::OFF_READER_PID)
            .load(Ordering::Relaxed);
        if pid == 0 {
            return false;
        }
        let hb = self
            .map
            .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .load(Ordering::Relaxed);
        if hb == 0 {
            return false;
        }
        // Saturating subtraction: the reader stamps its heartbeat concurrently,
        // so a heartbeat taken AFTER we sampled now_ns would underflow and
        // spuriously classify a live reader as dead. Mirrors the C writer and
        // the Rust reader's now_ns.saturating_sub(hb).
        let age = now_ns.saturating_sub(hb);
        age < WRITER_LIVENESS_TIMEOUT_NS
    }
}

impl Drop for RingWriter {
    fn drop(&mut self) {
        // Clear writer_pid only if it is ours — a re-attached writer with a
        // bumped epoch owns it now. Mirrors the C writer_close `cur == mine`
        // guard and the reader's Drop.
        let slot = self.map.header_atomic(layout::OFF_WRITER_PID);
        let mine = std::process::id() as u64;
        if slot.load(Ordering::Relaxed) == mine {
            slot.store(0, Ordering::Relaxed);
        }
    }
}

/// Clamped nanosleep for one full-ring wait tick: 1/4 period, capped at 2 ms,
/// never a hot spin. Mirrors the C writer's `clamped_nanosleep`.
fn clamped_nanosleep(period_frames: u32) {
    let period_ns = (period_frames as u64) * 1_000_000_000 / 48_000;
    let mut nap_ns = period_ns / 4;
    if nap_ns > MAX_TICK_NS {
        nap_ns = MAX_TICK_NS;
    }
    if nap_ns == 0 {
        nap_ns = 1_000; // never spin hot
    }
    let ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: nap_ns as _,
    };
    // SAFETY: a valid timespec pointer; a NULL remainder is fine (we do not need
    // to resume on EINTR — the caller re-loops and re-checks anyway).
    unsafe {
        libc::nanosleep(&ts, std::ptr::null_mut());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::layout::SAMPLE_FORMAT_S16LE;
    use crate::{RingReader, SlotRead};
    use std::sync::atomic::AtomicU64;

    static RING_TEST_SEQ: AtomicU64 = AtomicU64::new(0);

    fn tmp_ring_path(tag: &str) -> String {
        let dir = std::env::temp_dir().join(format!(
            "jts-ring-writer-test-{}-{}-{}",
            tag,
            std::process::id(),
            RING_TEST_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("program.ring").to_string_lossy().into_owned()
    }

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

    /// SPSC pairing: a `RingWriter` publishes and the production `RingReader`
    /// consumes the exact payload — the cross-half interop the C-reader bench
    /// proves on-Pi, exercised in-process here.
    #[test]
    fn writer_to_reader_roundtrips_payload() {
        let path = tmp_ring_path("roundtrip");
        let g = proto_geometry();
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        // The reader must be live (heartbeat stamped) so the writer takes the
        // publish path, not free-run. Prime the reader's heartbeat once.
        let n = g.samples_per_slot();
        let mut out = vec![0i16; n];
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Empty);

        let payload: Vec<i16> = (0..n)
            .map(|i| (i as i16).wrapping_mul(7).wrapping_sub(11))
            .collect();
        assert_eq!(writer.publish(&payload), PublishOutcome::Published);
        assert_eq!(writer.metrics().published_slots, 1);

        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Filled);
        assert_eq!(out, payload);
        // Consumed the only slot -> empty again (steady state).
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Empty);
        cleanup(&path);
    }

    /// Full ring with a LIVE reader: the writer blocks (bounded) and then
    /// succeeds once the reader drains a slot. full_waits climbs; no drop.
    #[test]
    fn full_ring_live_reader_back_pressures_then_publishes() {
        let path = tmp_ring_path("backpressure");
        let g = proto_geometry(); // n_slots = 2
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let mut out = vec![0i16; n];
        // Prime the reader heartbeat so the writer sees it live.
        reader.try_consume_slot(&mut out);
        let s = vec![3i16; n];

        // Fill both slots (no wait — space available).
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        assert_eq!(writer.free_slots(), 0);
        assert_eq!(writer.metrics().full_waits, 0);

        // Now the ring is full. A third publish with a live reader would block;
        // to keep the test deterministic and single-threaded, drain one slot
        // FIRST (as the DAC-paced reader would), then publish succeeds with no
        // wait. This proves the space-check path; the bounded-wait tick path is
        // covered by the stuck-reader test.
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Filled);
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        assert_eq!(writer.metrics().stuck_reader_drops, 0);
        assert_eq!(writer.metrics().drop_no_reader, 0);
        cleanup(&path);
    }

    /// Full ring with a live-but-STUCK reader (heartbeat fresh, read_seq never
    /// advances): the writer waits the bounded tick cap, then drops and counts
    /// stuck_reader_drops. With this 128-frame slot the tick is ~0.667 ms so the
    /// cap is ~21 ms (the loose 500 ms wall-time assert below is a sanity bound).
    #[test]
    fn full_ring_stuck_reader_drops_after_bounded_wait() {
        let path = tmp_ring_path("stuck");
        let g = proto_geometry(); // n_slots = 2
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        // Attach a reader to stamp reader_pid, then keep its heartbeat FRESH
        // without ever advancing read_seq — model a wedged reader.
        let reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let s = vec![5i16; n];
        // Fill both slots.
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        // Stamp a fresh reader heartbeat (reader is "alive") but do NOT consume.
        reader
            .map
            .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .store(monotonic_ns(), Ordering::Relaxed);

        // The third publish must wait the bounded ticks then drop. Bound the
        // wall time as a sanity check on the ~21 ms cap (128-frame slot tick).
        let start = std::time::Instant::now();
        assert_eq!(writer.publish(&s), PublishOutcome::DroppedStuck);
        let elapsed = start.elapsed();
        assert!(
            elapsed < std::time::Duration::from_millis(500),
            "bounded wait must be well under the watchdog threshold, got {elapsed:?}"
        );
        assert_eq!(writer.metrics().stuck_reader_drops, 1);
        assert_eq!(writer.metrics().full_waits, 1);
        assert_eq!(writer.metrics().drop_no_reader, 0);
        cleanup(&path);
    }

    /// Sticky-stuck demotion (issue #1524): a live reader that stamps a fresh
    /// heartbeat but NEVER advances `read_seq` first drops via the bounded
    /// back-pressure wait (pre-grace, `DroppedStuck` — the ~21 ms path), then
    /// once `read_seq` has been frozen past [`STUCK_READER_GRACE_NS`] the writer
    /// DEMOTES it and free-runs (`DroppedStuckDemoted`) with NO bounded wait,
    /// restoring real-time pacing. Occupancy stays bounded (drop-oldest). The
    /// grace crossing is simulated by backdating the reader-advance stamp so the
    /// test never sleeps a real second.
    #[test]
    fn stuck_reader_demotes_to_freerun_after_grace() {
        let path = tmp_ring_path("demote");
        let g = proto_geometry(); // n_slots = 2
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        let reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let s = vec![5i16; n];
        let stamp_reader_heartbeat = || {
            reader
                .map
                .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
                .store(monotonic_ns(), Ordering::Relaxed);
        };
        // Fill both slots, then wedge the reader: fresh heartbeat, never consume.
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        stamp_reader_heartbeat();

        // PRE-GRACE: within the grace the writer still pays the bounded wait.
        let start = std::time::Instant::now();
        assert_eq!(writer.publish(&s), PublishOutcome::DroppedStuck);
        let pre = start.elapsed();
        assert!(
            pre >= std::time::Duration::from_millis(3),
            "pre-grace publish must pay the bounded back-pressure wait, got {pre:?}",
        );
        assert_eq!(writer.metrics().stuck_reader_drops, 1);

        // Cross the grace WITHOUT a real wait: backdate the reader-advance stamp.
        // read_seq is still frozen (the reader never consumed), so the next
        // publish's note_reader_progress leaves this backdated stamp intact.
        stamp_reader_heartbeat();
        writer.last_read_seq_advance_ns =
            monotonic_ns().saturating_sub(STUCK_READER_GRACE_NS + 10_000_000);

        // POST-GRACE: demotion — free-run drop-oldest, NO ~21 ms wait.
        let start = std::time::Instant::now();
        assert_eq!(writer.publish(&s), PublishOutcome::DroppedStuckDemoted);
        let post = start.elapsed();
        assert!(
            post < std::time::Duration::from_millis(3),
            "demoted publish must NOT pay the bounded wait, got {post:?}",
        );
        // stuck_reader_drops folds the demoted drop; occupancy stays bounded.
        assert_eq!(writer.metrics().stuck_reader_drops, 2);
        assert_eq!(writer.metrics().drop_no_reader, 0);
        assert!(writer.metrics().occupancy <= g.n_slots as u64);
        cleanup(&path);
    }

    /// False-positive guard: a reader that KEEPS advancing `read_seq` (a
    /// legitimately DAC-paced reader that back-pressures a full ring but still
    /// drains one slot per period) resets the stall clock on every advance and is
    /// therefore NEVER demoted — even if the age momentarily looks old. This is
    /// the distinction between "wedged" and "merely back-pressuring".
    #[test]
    fn stuck_reader_resets_on_advance() {
        let path = tmp_ring_path("resets");
        let g = proto_geometry(); // n_slots = 2
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let mut out = vec![0i16; n];
        reader.try_consume_slot(&mut out); // prime the reader heartbeat
        let s = vec![7i16; n];
        // Fill both slots (ring full).
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        assert_eq!(writer.publish(&s), PublishOutcome::Published);

        // Even with the advance stamp backdated to LOOK past-grace, a reader that
        // advances read_seq resets it before the full/demotion check fires:
        // consume one slot (the DAC-paced drain), then publish. The loop-top
        // note_reader_progress sees the advance, resets the clock, and the freed
        // slot makes the publish succeed with NO demotion.
        writer.last_read_seq_advance_ns =
            monotonic_ns().saturating_sub(STUCK_READER_GRACE_NS + 10_000_000);
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Filled);
        assert_eq!(writer.publish(&s), PublishOutcome::Published);

        assert_eq!(
            writer.metrics().stuck_reader_drops,
            0,
            "a reader that keeps advancing must never be demoted/stuck-dropped",
        );
        assert_eq!(writer.metrics().drop_no_reader, 0);
        assert!(
            writer.ns_since_read_seq_advance() < STUCK_READER_GRACE_NS,
            "the reader advance must reset the stall clock",
        );
        cleanup(&path);
    }

    /// Once a demoted reader RESUMES (advances `read_seq` again — here via the
    /// reattach/drift resync), the writer resets the stall clock and returns to
    /// normal `Published` back to the now-live reader. Demotion is not latched:
    /// it self-clears on the first reader advance.
    #[test]
    fn demoted_reader_resumes_publishing_when_unstuck() {
        let path = tmp_ring_path("resume_unstuck");
        let g = proto_geometry();
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let mut out = vec![0i16; n];
        reader.try_consume_slot(&mut out); // prime the reader heartbeat
        let s = vec![3i16; n];
        // Fill, wedge (fresh heartbeat, no consume), and cross the grace.
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        reader
            .map
            .header_atomic(layout::OFF_READER_HEARTBEAT_NS)
            .store(monotonic_ns(), Ordering::Relaxed);
        writer.last_read_seq_advance_ns =
            monotonic_ns().saturating_sub(STUCK_READER_GRACE_NS + 10_000_000);
        assert_eq!(writer.publish(&s), PublishOutcome::DroppedStuckDemoted);
        assert_eq!(writer.publish(&s), PublishOutcome::DroppedStuckDemoted);
        assert!(writer.metrics().stuck_reader_drops >= 2);

        // Reader resumes: one consume drift-resyncs read_seq to the write tip and
        // stores it to the header. The next publish observes that advance, resets
        // the clock, and returns to normal publishing.
        let _ = reader.try_consume_slot(&mut out);
        assert_eq!(
            writer.publish(&s),
            PublishOutcome::Published,
            "once read_seq advances again the writer resumes normal publishing",
        );
        assert!(
            writer.ns_since_read_seq_advance() < STUCK_READER_GRACE_NS,
            "the reader advance reset the stall clock",
        );
        cleanup(&path);
    }

    /// Full ring with NO live reader: the writer free-runs (drop-oldest,
    /// advancing read_seq itself), stays bounded, and never blocks. Repeated
    /// publishes keep occupancy at n_slots and count drop_no_reader.
    #[test]
    fn full_ring_dead_reader_free_runs_bounded() {
        let path = tmp_ring_path("freerun");
        let g = proto_geometry(); // n_slots = 2
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        // No reader attached at all: reader_pid == 0 -> dead.
        let n = g.samples_per_slot();
        let s = vec![9i16; n];

        // Fill both slots, then publish many more — each free-run-drops the
        // oldest and stays bounded. No blocking, no unbounded growth.
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        assert_eq!(writer.publish(&s), PublishOutcome::Published);
        for _ in 0..10 {
            assert_eq!(writer.publish(&s), PublishOutcome::DroppedNoReader);
            // Occupancy stays pinned at n_slots (bounded).
            assert_eq!(writer.metrics().occupancy, g.n_slots as u64);
        }
        assert_eq!(writer.metrics().drop_no_reader, 10);
        assert_eq!(writer.metrics().published_slots, 2);
        cleanup(&path);
    }

    /// A reader that attaches AFTER the writer has free-run past the tip resyncs
    /// to write_seq (dropping the stale slots) and then the writer resumes
    /// normal publishing to it (drop-oldest stops).
    #[test]
    fn dead_reader_then_reattach_resumes_publishing() {
        let path = tmp_ring_path("reattach");
        let g = proto_geometry();
        let mut writer = RingWriter::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let s = vec![2i16; n];
        // Free-run a while with no reader.
        writer.publish(&s);
        writer.publish(&s);
        writer.publish(&s); // drop-oldest
        assert!(writer.metrics().drop_no_reader >= 1);

        // A reader attaches: it resyncs read_seq = write_seq (drops stale slots).
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        assert_eq!(reader.metrics().attach_resyncs, 1);
        let mut out = vec![0i16; n];
        // Reader is caught up: empty until a NEW publish.
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Empty);

        // Writer publishes to the now-live reader (no more free-run).
        let payload: Vec<i16> = (0..n).map(|i| (i as i16).wrapping_add(1)).collect();
        assert_eq!(writer.publish(&payload), PublishOutcome::Published);
        assert_eq!(reader.try_consume_slot(&mut out), SlotRead::Filled);
        assert_eq!(out, payload);
        cleanup(&path);
    }

    /// Writer reattach bumps the epoch, which the reader observes as an
    /// epoch_reset — the resync-safety signal across a fanin restart.
    #[test]
    fn writer_reattach_bumps_epoch_observed_by_reader() {
        let path = tmp_ring_path("epoch");
        let g = proto_geometry();
        let mut reader = RingReader::create_or_attach(&path, g).unwrap();
        let n = g.samples_per_slot();
        let mut out = vec![0i16; n];
        {
            let mut w1 = RingWriter::create_or_attach(&path, g).unwrap();
            assert_eq!(w1.publish(&vec![1i16; n]), PublishOutcome::Published);
        }
        reader.try_consume_slot(&mut out);
        let e1 = reader.metrics().epoch_resets;
        {
            let mut w2 = RingWriter::create_or_attach(&path, g).unwrap();
            assert_eq!(w2.publish(&vec![2i16; n]), PublishOutcome::Published);
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

    /// The writer resumes the stored write_seq across a reattach (file-lifetime
    /// monotonic), never restarting at 0.
    #[test]
    fn writer_resumes_write_seq_across_reattach() {
        let path = tmp_ring_path("resume");
        let g = proto_geometry();
        let n = g.samples_per_slot();
        let s = vec![4i16; n];
        {
            let mut w1 = RingWriter::create_or_attach(&path, g).unwrap();
            w1.publish(&s);
            w1.publish(&s);
            assert_eq!(w1.write_seq(), 2);
        }
        // A second writer attaches and continues from the stored write_seq.
        let w2 = RingWriter::create_or_attach(&path, g).unwrap();
        assert_eq!(w2.write_seq(), 2, "write_seq must resume, not reset to 0");
        cleanup(&path);
    }

    /// Geometry mismatch on attach is fail-loud (a retuned lab box with the
    /// wrong slot geometry must not shear slots).
    #[test]
    fn geometry_mismatch_on_writer_attach_is_fatal() {
        let path = tmp_ring_path("mismatch");
        let g = proto_geometry();
        let _reader = RingReader::create_or_attach(&path, g).unwrap();
        let wrong = Geometry {
            period_frames: 256,
            ..g
        };
        let err = match RingWriter::create_or_attach(&path, wrong) {
            Ok(_) => panic!("geometry mismatch must be fatal"),
            Err(e) => e,
        };
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        cleanup(&path);
    }
}
