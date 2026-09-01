// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Renderer-ingress ring capture: the third fan-in lane source (U3 / P6).
//!
//! A renderer-ingress lane reads a per-renderer SHM slot ring
//! (`/dev/shm/jts-ring/lane-<label>.ring`) that the renderer's own
//! `jts_ring` ioplug writes, instead of reading that lane's snd-aloop capture
//! substream. The transport changes; nothing else about the lane does — it keeps
//! its position in the sum, its selection/mute gate, its RMS meter, and (where a
//! lane has one) its `LaneResampler` at exactly the same place in the read path.
//! The ioplug is a dumb frame carrier.
//!
//! ## Presence model — the USB DIRECT precedent, applied to a file
//!
//! `Input::pcm` is already `Option<PCM>` and `None` on the USB DIRECT lane, whose
//! device comes and goes with a host cable. A renderer ring has the same shape of
//! dynamism for a different reason: the ring FILE exists only once some writer has
//! created it, `/dev/shm` is tmpfs (so the file is gone after a reboot until a
//! writer returns), and a renderer can be stopped, disabled by `/sources/`, or
//! crash-looping. So this lane is the same small state machine as
//! [`super::direct_capture::DirectCapture`]:
//!
//! * [`RingCapture::Attached`] — the ring is mapped; the lane consumes one slot
//!   per render period.
//! * [`RingCapture::Detached`] — no ring (never attached, or a geometry/permission
//!   refusal). The lane renders SILENCE and retries the attach at most once per
//!   [`RING_REATTACH_RETRY_PERIODS`], counted in render periods so the hot loop
//!   reads no wall clock.
//!
//! Neither state can fail the daemon: the fail-hard "every configured input is
//! required" contract is exempted for a ring lane exactly as it is for the direct
//! lane. That is deliberate and it is the resilience property this module exists
//! to provide — a renderer that is off, a ring that has not been created yet, and
//! a stale ring from a previous boot all render silence and self-heal, rather than
//! taking the whole summed music path down with them.
//!
//! ## What a DEAD WRITER looks like, and why nothing here has to detect it
//!
//! `jasper_ring`'s reader already owns writer liveness: an empty ring zero-fills
//! the caller's buffer and returns [`SlotRead::Empty`], and
//! [`RingMetrics::writer_alive`] reports a pid-plus-heartbeat view that goes false
//! within [`jasper_ring::WRITER_LIVENESS_TIMEOUT_NS`] of the writer stopping. So a
//! writer that dies mid-stream needs no detection here at all: the lane simply
//! reads empty slots and mixes silence, and `/state` says `writer_alive:false`
//! next to a climbing `empty_reads`. There is no reattach for that case, because
//! the ring is still perfectly attached — which is why the retry latch below is
//! armed ONLY by an attach failure, never by an empty read. Arming it on empty
//! reads would tear down and rebuild a healthy mapping every time a renderer
//! paused.
//!
//! ## Allocation and blocking
//!
//! Zero allocation in the steady path: [`read_ring_and_render`] consumes directly
//! into the lane's existing `read_buf`, and the retry path formats no strings
//! (the detach reason is a `&'static str` on a copy `enum`). The ATTACHED path
//! does not block — `try_consume_slot` is a non-blocking memcpy-or-zero-fill, and
//! the only syscalls are the ones `jasper_ring` makes on the mapping itself.
//!
//! **The DETACHED path does not block either, since #2538.** The attach it needs
//! is `RingReader::create_or_attach`, which takes an inter-process `flock`
//! bounded at `OPEN_LOCK_WAIT_TIMEOUT_MS` = 500 ms (`jasper-ring/src/lib.rs`)
//! plus the open/mmap/header-validate work. That used to run INLINE here, once
//! per [`RING_REATTACH_RETRY_PERIODS`] (≈2 s) per detached lane; against a
//! 5.33 ms render period and two 128-frame slots of downstream cushion, the
//! worst case was ~187 slots of audio — the #2533 defect class, on a path that
//! needs no USB host to fire. [`RingAttacher`] moves it to a
//! `fanin-ring-attacher` thread, the sibling of `fanin-direct-opener`: the
//! detached lane costs one non-blocking `try_recv`
//! per period and one non-blocking `send` per retry window, and keeps rendering
//! silence until a reader comes back.
//!
//! **How often is a lane detached?** Not "whenever the renderer is idle" — that
//! is the ATTACHED-with-`writer_alive:false` state described above, because
//! `create_or_attach` CREATES the ring when it is absent and fan-in (root) can
//! always do so. Detached means a genuine fault (a geometry shear, a permission
//! refusal, a full tmpfs) or a transient (`.open.lock` contention against the
//! writer's own open, the single period an orphan re-latch spends detached).
//! Note the ring DIRECTORY is not on that list: `ensure_parent_dir` creates it,
//! so a missing directory is not a startup state a root fan-in can observe. So
//! this is not a most-of-the-time cost — but for as long as a lane IS detached,
//! the bounded `flock` really did run inside the render period, and a shear or a
//! permissions fault persists until an operator fixes it. `jasper-doctor` reads
//! it the same way: a detached lane is a PROBLEM there, while a lane that is
//! attached with a PAUSED renderer (frames read, `writer_alive:false`) falls
//! through to healthy — which is the idle case, and is not detached.
//!
//! The retry latches are also PHASE-SEEDED per lane ([`reattach_phase`]) so a box
//! with several detached lanes spreads its attempts across the window instead of
//! firing every lane in one period.

use super::*;

use jasper_ring::{Geometry, RingMetrics, RingReader, SlotRead, SAMPLE_FORMAT_S16LE};

/// Render periods between reattach attempts while `Detached`, and between
/// orphaned-inode probes while `Attached`.
///
/// At the shipped geometry one period is 256 frames @ 48 kHz = 5.333 ms, so 384
/// periods is 2.048 s. (375 would be the exact 2 s; 384 is chosen instead
/// because it is a power-of-two multiple, which keeps the arithmetic exact
/// across the period sizes fan-in actually runs and costs 48 ms of latency
/// nobody can perceive on a reattach.) Same cadence and same reasoning as the
/// USB DIRECT lane's `DIRECT_REOPEN_RETRY_PERIODS`: frequent enough that a
/// renderer coming up is picked up within a couple of seconds, rare enough that
/// a box whose renderer is switched OFF at `/sources/` is not opening a file
/// 187 times a second forever.
///
/// **Relationship to the renderer's own `RestartSec`.** librespot restarts at
/// `RestartSec=5`, comfortably longer than this window, so a crash-looping
/// renderer is always found by the FIRST retry after each respawn rather than
/// being missed between probes. Keep this cadence below any ring-writing
/// renderer's `RestartSec`; if a renderer ever restarts faster than ~2 s, this
/// constant is what has to move, not the unit.
pub(super) const RING_REATTACH_RETRY_PERIODS: u64 = 384;

/// This lane's PHASE within the retry window, in render periods: the countdown a
/// ring lane starts life with, so N lanes spread their FIRST attempts evenly
/// across [`RING_REATTACH_RETRY_PERIODS`] rather than firing in one period
/// (#2538).
///
/// **What the seed does, and what it does not promise.** It spreads the FIRST
/// retry of each lane across the window, and nothing here reads a wall clock —
/// the phase is decided once and then carried by the ordinary latch. It is not a
/// standing guarantee that two lanes never retry in the same period, and it
/// should not be read as one:
///
/// * the cycle is not exactly this constant. A queue re-arms the latch to
///   [`RING_REATTACH_RETRY_PERIODS`] and so does ADOPTING the result, so a lane
///   whose attach finishes one period later runs a 386-period cycle. Separation
///   holds while lanes' attach latencies match and drifts when they differ.
/// * the orphan re-latch in [`read_ring_and_render`] sets the latch to 0
///   unconditionally — deliberately, since that is a live file waiting to be
///   opened — so lanes orphaned in the same period are in lockstep from then on.
///
/// **What it buys now that the attach is off-thread.** Not render-thread time —
/// [`RingAttacher`] already removed that, and a queued attach costs one
/// non-blocking `send`. It spreads the WORKER side: when several lanes are
/// detached at once (a geometry shear across the conf.d, a tmpfs that filled),
/// their `create_or_attach` calls each take their own bounded `flock` and
/// contend for `/dev/shm/jts-ring/`. Spreading them keeps that work off one
/// instant, and keeps the property that made lockstep dangerous from ever being
/// reintroduced by a future inline call.
///
/// `lane_count == 0` cannot occur (a lane index implies a lane) but returns 0
/// rather than dividing by it — a phase of 0 is a legal, if unspread, answer.
pub(super) const fn reattach_phase(lane_index: usize, lane_count: usize) -> u64 {
    if lane_count == 0 {
        return 0;
    }
    RING_REATTACH_RETRY_PERIODS * (lane_index % lane_count) as u64 / lane_count as u64
}

/// Why a renderer-ingress lane is not attached. A copy `enum` of `&'static str`
/// tokens rather than a formatted message, so the detached path allocates nothing
/// and the token is stable enough to grep for in the journal and to publish in
/// `/state`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum RingDetachReason {
    /// The ring path could not be resolved at all — the `NotFound`/`ENOENT`
    /// classification, and a genuinely NARROW one.
    ///
    /// **It is not "fan-in started before the renderer".** That was the original
    /// P6a text here and it is wrong in the direction that matters:
    /// [`attach_ring`] creates the ring when it is absent (`O_CREAT|O_EXCL`, with
    /// `ensure_parent_dir` creating the directory first) and fan-in runs as root,
    /// so starting before — or entirely without — the renderer produces an
    /// ATTACH, not this. A lane whose renderer never publishes sits ATTACHED with
    /// `writer_alive:false`; see the module docs' presence model.
    ///
    /// What actually lands here is a RACE: the ring file or its directory
    /// disappearing between the create/attach steps (an arm/disarm or a
    /// geometry-change clear running underneath this open). The other failure
    /// modes people reach for do NOT land here — `.open.lock` exhaustion returns
    /// `EAGAIN` (`WouldBlock`), a full tmpfs returns `ENOSPC` (`StorageFull`),
    /// and a permission refusal returns `EACCES` (`PermissionDenied`); all three
    /// classify as [`RingDetachReason::Refused`], whose remediation text is the
    /// one that fits them.
    Unavailable,
    /// The ring exists but its header declares a different geometry than this lane
    /// builds — the conf.d block and fan-in's derived geometry have sheared. This
    /// one is an OPERATOR fault, not a transient, so it is logged distinctly: the
    /// retry will keep failing until the conf.d or the lane geometry is fixed.
    Geometry,
    /// The ring exists but could not be mapped for a reason that is neither of the
    /// above (permissions on the file or its directory, an exhausted mapping, a
    /// torn file). Kept separate from `Unavailable` because the remediation is
    /// different: this one usually means the renderer user is not in the ring
    /// directory's group, or a unit is missing `UMask=0007`. The one exception to
    /// that remediation is `EBUSY` — a live FOREIGN reader already holds the ring
    /// (the journal's `event=jts_ring.reader.busy` line names the incumbent pid).
    Refused,
    /// The mapping outlived the FILE: the ring at this path was replaced (a
    /// geometry change cleared and recreated it, or an arm/disarm did) while
    /// this reader held it open. An mmap survives an unlink, so the lane would
    /// otherwise report `attached:true` and read an ORPHANED inode forever —
    /// indistinguishable from an idle source on every other counter. Detected
    /// on the slow check cadence and self-heals by re-latching onto the live
    /// file, so it needs no operator; it is a distinct token only so the
    /// journal shows the re-latch happened rather than an unexplained gap.
    Orphaned,
}

impl RingDetachReason {
    pub(super) const fn as_str(self) -> &'static str {
        match self {
            Self::Unavailable => "unavailable",
            Self::Geometry => "geometry",
            Self::Refused => "refused",
            Self::Orphaned => "orphaned",
        }
    }

    /// Classify an attach failure. `jasper_ring` surfaces a geometry mismatch as
    /// [`io::ErrorKind::InvalidData`] and a self-invalid geometry request as
    /// [`io::ErrorKind::InvalidInput`] (the same two kinds `Mixer::new` treats as
    /// CONFIG-class for Ring A — see `ring_attach_failure_is_config_class`), and a
    /// missing file as [`io::ErrorKind::NotFound`]. Everything else is a refusal.
    fn classify(err: &std::io::Error) -> Self {
        match err.kind() {
            std::io::ErrorKind::NotFound => Self::Unavailable,
            std::io::ErrorKind::InvalidData | std::io::ErrorKind::InvalidInput => Self::Geometry,
            _ => Self::Refused,
        }
    }
}

/// Shared renderer-ring counters for the STATUS `ring{}` block, mirroring
/// [`super::direct_capture::DirectObservability`]'s idiom exactly: the mixer work
/// thread writes them, the state-server thread reads them lock-free, and they are
/// cloned into [`crate::state::InputSnapshotSource`] at construction.
///
/// Everything here that `jasper_ring` already counts is MIRRORED rather than
/// re-derived: `RingReader::metrics()` returns a plain `Copy` snapshot, and the
/// read path stores the fields `/state` needs into these atomics on each period.
/// Re-deriving occupancy or empty-read counts in fan-in would be a second source
/// of truth for numbers the ring already owns.
#[derive(Clone)]
pub struct RingLaneObservability {
    /// The ring file this lane reads (`/dev/shm/jts-ring/lane-<label>.ring`).
    pub path: String,
    /// The geometry this lane attaches with, held whole rather than as loose
    /// scalars: the reattach path must present the SAME tuple the initial attach
    /// used or the header comparison rejects it, and a struct that cannot be
    /// partially rebuilt is the cheapest way to guarantee that. STATUS reads
    /// `period_frames` / `n_slots` off it.
    pub geometry: Geometry,
    /// Whether the ring is currently mapped. `false` renders silence.
    pub attached: Arc<AtomicBool>,
    /// Why the lane is detached, as a [`RingDetachReason`] token index. Only
    /// meaningful while `attached` is false; retained across a reattach so an
    /// operator reading `/state` after a self-heal can still see what it healed
    /// FROM.
    pub detach_reason: Arc<AtomicU64>,
    /// Cumulative successful attaches (climbs on the first attach and on every
    /// reattach). `attaches > 1` means this lane has self-healed.
    pub attaches: Arc<AtomicU64>,
    /// Cumulative reattach attempts QUEUED while detached. A growing value with
    /// `attached=false` means the ring is not attachable — the renderer is off,
    /// or the geometry/permissions are wrong (read `detach_reason`). Since #2538
    /// an attempt is counted when it is handed to the `fanin-ring-attacher`
    /// thread rather than when it runs, so this climbs on the retry cadence even
    /// while one attach is still executing.
    ///
    /// **The dead-worker tell.** This value FROZEN alongside `attached=false`
    /// and `attach_pending=false` means the attacher thread is gone: the lane is
    /// detached, nothing is queued, and nothing is being attempted — a silent
    /// wedge that no other counter separates from a lane nobody is looking at.
    /// (Byte-for-byte the direct lane's `retries`/`reopen_pending` shape.)
    pub retries: Arc<AtomicU64>,
    /// Whether an attach is currently QUEUED on this lane's
    /// `fanin-ring-attacher` thread (#2538), mirroring the direct lane's
    /// `reopen_pending`. Since the attach no longer runs in the render loop,
    /// this is how an operator sees one in progress — and a value stuck `true`
    /// alongside a climbing `retries` says the attach itself is hanging (a held
    /// `.open.lock`, a wedged tmpfs), which now costs this lane silence rather
    /// than costing the speaker slots.
    pub attach_pending: Arc<AtomicBool>,
    /// The writer looked alive at the last read (pid stamped AND heartbeat inside
    /// [`jasper_ring::WRITER_LIVENESS_TIMEOUT_NS`]). Mirrored from
    /// [`RingMetrics::writer_alive`]; false with `attached=true` is the ordinary
    /// "renderer is not playing" state, NOT a fault.
    pub writer_alive: Arc<AtomicBool>,
    /// Last-observed writer pid (0 = no writer). Mirrored from the ring header so
    /// `/state` and the doctor's EBUSY-owner check read the same fact.
    pub writer_pid: Arc<AtomicU64>,
    /// `write_seq - read_seq` at the last read.
    pub occupancy: Arc<AtomicU64>,
    /// Empty reads AFTER at least one filled slot — steady-state slips. Mirrored
    /// from [`RingMetrics::empty_reads`].
    pub empty_reads: Arc<AtomicU64>,
    /// Empty reads BEFORE the first-ever filled slot (startup priming). Split from
    /// `empty_reads` by the ring itself, and kept split here, because a lane that
    /// has never been written is a different fact from one that slipped.
    pub startup_empty_reads: Arc<AtomicU64>,
    /// Times the observed writer epoch changed — the renderer reattached. This is
    /// the DISCRIMINATOR the campaign's standing ring watch uses: `empty_reads`
    /// rising with `epoch_resets` flat is a drain; both rising is a writer
    /// restart artefact.
    pub epoch_resets: Arc<AtomicU64>,
}

impl RingLaneObservability {
    fn new(path: String, geometry: Geometry) -> Self {
        RingLaneObservability {
            path,
            geometry,
            attached: Arc::new(AtomicBool::new(false)),
            detach_reason: Arc::new(AtomicU64::new(RingDetachReason::Unavailable as u64)),
            attaches: Arc::new(AtomicU64::new(0)),
            retries: Arc::new(AtomicU64::new(0)),
            attach_pending: Arc::new(AtomicBool::new(false)),
            writer_alive: Arc::new(AtomicBool::new(false)),
            writer_pid: Arc::new(AtomicU64::new(0)),
            occupancy: Arc::new(AtomicU64::new(0)),
            empty_reads: Arc::new(AtomicU64::new(0)),
            startup_empty_reads: Arc::new(AtomicU64::new(0)),
            epoch_resets: Arc::new(AtomicU64::new(0)),
        }
    }

    /// Publish one period's ring metrics. Lock-free, allocation-free, no syscall —
    /// safe on the hot path. Mirrors, never re-derives: every field here is a
    /// field `jasper_ring` already maintains.
    fn publish(&self, m: &RingMetrics) {
        self.writer_alive.store(m.writer_alive, Ordering::Relaxed);
        self.writer_pid.store(m.writer_pid, Ordering::Relaxed);
        self.occupancy.store(m.occupancy, Ordering::Relaxed);
        self.empty_reads.store(m.empty_reads, Ordering::Relaxed);
        self.startup_empty_reads
            .store(m.startup_empty_reads, Ordering::Relaxed);
        self.epoch_resets.store(m.epoch_resets, Ordering::Relaxed);
    }

    /// The current detach reason token for STATUS.
    pub fn detach_reason_str(&self) -> &'static str {
        match self.detach_reason.load(Ordering::Relaxed) {
            x if x == RingDetachReason::Geometry as u64 => RingDetachReason::Geometry.as_str(),
            x if x == RingDetachReason::Refused as u64 => RingDetachReason::Refused.as_str(),
            x if x == RingDetachReason::Orphaned as u64 => RingDetachReason::Orphaned.as_str(),
            _ => RingDetachReason::Unavailable.as_str(),
        }
    }
}

/// One renderer-ingress lane's runtime state. See the module docs for the
/// presence model; see [`RingDetachReason`] for what each detached state means.
pub(super) enum RingCapture {
    /// The ring is mapped; the lane consumes one slot per render period.
    /// `periods_until_check` counts down to the next orphaned-inode probe (see
    /// [`RingDetachReason::Orphaned`]); it shares the reattach cadence, because
    /// both are "sample something slow, not every period".
    Attached {
        reader: Box<RingReader>,
        periods_until_check: u64,
    },
    /// No ring. The lane renders silence and retries the attach at most once per
    /// [`RING_REATTACH_RETRY_PERIODS`], counted down one per render period.
    Detached { periods_until_retry: u64 },
}

/// The geometry a renderer-ingress lane attaches with. Stereo `S16_LE` at the
/// box's sample rate, one slot per fan-in render period, depth derived from the
/// aloop cushion the lane replaced.
///
/// **The wire stays S16 here on purpose, and it is not the U2 width axis.** Every
/// renderer lane's producer is an ALSA `plug:` wrapper over a 48 kHz `S16_LE`
/// substream — the consolidation plan's boundary table pins it there — and U3
/// moves the transport WITHOUT moving the width, so a narrow renderer sums
/// exactly the bytes it summed before. That is a property of the lane class, not
/// of which lanes happen to be migrated, so it holds unchanged for each lane
/// P6b-d adds. Widening a renderer lane is a separate decision with its own
/// evidence, taken on the same `RingWireFormat` axis the program ring already
/// has.
pub(super) fn renderer_ring_geometry(config: &Config) -> Option<Geometry> {
    Some(Geometry {
        rate: config.sample_rate,
        channels: CHANNELS,
        sample_format: SAMPLE_FORMAT_S16LE,
        period_frames: config.period_frames,
        n_slots: config.renderer_ring_slots()?,
    })
}

/// Attempt one attach of a renderer-ingress ring, returning the reader or the
/// classified reason it could not be had.
///
/// `RingReader::create_or_attach` CREATES the ring when it is absent, which is
/// what we want: whichever end starts first materializes the file, and the
/// directory's setgid bit plus both ends' `UMask=0007` make it group-writable
/// either way (both ends write the header — the reader stamps `read_seq` and its
/// heartbeat). Creating from the reader side is also what makes fan-in's derived
/// geometry the one that lands in a fresh header, so a renderer whose conf.d has
/// sheared fails ITS open loudly instead of silently establishing a ring fan-in
/// then refuses.
fn attach_ring(path: &str, geometry: Geometry) -> Result<RingReader, RingDetachReason> {
    RingReader::create_or_attach(path, geometry).map_err(|e| RingDetachReason::classify(&e))
}

/// One reattach for the `fanin-ring-attacher` thread to perform OFF the render
/// thread. The geometry travels with the request precisely so the worker presents
/// the SAME tuple the initial attach used — the header comparison rejects a
/// rebuilt-from-parts one.
struct RingAttachRequest {
    path: String,
    geometry: Geometry,
}

/// The result of one queued attach: the live reader, or the classified reason it
/// could not be had. The `io::Error` is not carried across the channel — it is
/// classified on the worker thread, so the render thread receives the same
/// `Copy` token it stores in `/state` and nothing more.
enum RingAttachOutcome {
    Attached(Box<RingReader>),
    Failed(RingDetachReason),
}

/// A renderer-ingress lane's deferred attach channel (#2538).
///
/// **Why this exists.** [`attach_ring`] bottoms out in
/// `RingReader::create_or_attach`, which takes the ring's adjacent
/// `.open.lock` `flock` under a 500 ms deadline
/// (`OPEN_LOCK_WAIT_TIMEOUT_MS` in `jasper-ring`) and then opens, mmaps, and
/// header-validates the file. That used to run INLINE in the mixer's render
/// loop, once per [`RING_REATTACH_RETRY_PERIODS`] (≈2 s) for every detached
/// lane. The render loop's budget is one period (5.33 ms at the shipped 256
/// frames) and the downstream pipeline holds two 128-frame slots of cushion, so
/// anything over ~2.7 ms costs a whole slot: CamillaDSP reads an empty Ring A (a
/// 128-frame silence INSERTION) or fan-in free-run-drops a slot it could not
/// publish in time (a 128-frame DELETION). 500 ms is ~187 of those slots, and
/// fan-in's own `RingStallTracker` has a 1 s floor and cannot see any of it.
///
/// **How exposed is a box to that?** Less than a paused renderer suggests, and
/// the honest bound is worth stating rather than the dramatic one. A lane whose
/// renderer is merely not publishing is ATTACHED with `writer_alive:false`, not
/// detached — `create_or_attach` CREATES an absent ring and fan-in runs as root,
/// so the file materializes whichever end arrives first (see
/// [`open_ring_input`]). Detached is a FAULT-or-transient state: a geometry
/// shear, a permission refusal, a full tmpfs, `.open.lock` contention against
/// the writer's own open, or the single period an orphan re-latch spends
/// detached. (Not a missing ring directory — `ensure_parent_dir` creates
/// it.) Two things follow. It is
/// not a most-of-the-time cost — but a shear or a permissions fault does not
/// clear itself, so an affected box paid this every ~2 s until an operator
/// noticed, and unlike the DIRECT lane's opener it needs no host and no cable to
/// be exposed at all.
///
/// The lane now hands attaches to a dedicated thread and keeps rendering silence
/// until a reader comes back. One `Sender::send` per retry window and one
/// `try_recv` per detached period; nothing blocks.
///
/// **One attacher per lane, deliberately.** Each ring lane owns its own thread
/// and therefore its own in-flight latch — a shared worker would serialize four
/// lanes' bounded waits behind one another, so a fourth lane could sit 1.5 s
/// behind a first lane's slow attach for no benefit. The threads block in
/// `recv()` when idle and cost no CPU, and the rings they lock are different
/// inodes, so they do not contend with each other. The cost that is NOT free is
/// MEMORY: up to four extra threads at Rust's 2 MiB default stack, and because
/// they are spawned in `Mixer::new` — before `main` calls `lock_memory()` —
/// their stacks are inside the `mlockall` and stay resident. Bounded and small
/// against a 1 GB Pi, paid only on a box with armed ring lanes, and the reason
/// this is a per-lane thread rather than a per-lane thread POOL; but on this
/// hardware the saturation question is RAM, not CPU, so it is named rather than
/// left implied.
pub(super) struct RingAttacher {
    req_tx: Sender<RingAttachRequest>,
    res_rx: std::sync::mpsc::Receiver<RingAttachOutcome>,
    /// A request is queued and its result has not been collected. At most one is
    /// ever outstanding on this lane, so a slow or hung attach cannot build a
    /// backlog of retries behind it.
    in_flight: bool,
    /// The STATUS mirror of `in_flight` (`ring.attach_pending`), shared with the
    /// state-server thread — the same idiom as the direct lane's
    /// `reopen_pending`. Stuck `true` alongside a climbing `retries` means the
    /// attach itself is hanging, which now costs this lane silence rather than
    /// costing the speaker slots.
    pending_gauge: Arc<AtomicBool>,
}

impl RingAttacher {
    /// Spawn the attacher thread. Called at lane construction
    /// ([`open_ring_input`], which `Mixer::new` runs before `main` calls
    /// `mlockall`, like every other fan-in helper thread).
    pub(super) fn spawn(label: &str, pending_gauge: Arc<AtomicBool>) -> std::io::Result<Self> {
        let (req_tx, req_rx) = std::sync::mpsc::channel::<RingAttachRequest>();
        let (res_tx, res_rx) = std::sync::mpsc::channel::<RingAttachOutcome>();
        std::thread::Builder::new()
            // One thread per lane, so the LABEL rides the name and a thread dump
            // says which lane is attaching. Longer than Linux's 15-byte visible
            // comm, which is fine and is checked, not assumed: std's Linux
            // `set_name` truncates to `TASK_COMM_LEN` BEFORE calling
            // `pthread_setname_np`, so the call succeeds and its debug-build
            // assertion holds in a test binary too (read out of the pinned
            // toolchain's own `sys/pal/unix/thread.rs`). `fanin-direct-opener`
            // is already over the limit on the same mechanism. The full name is
            // what this file and the contract guard are written against.
            .name(format!("fanin-ring-attacher-{label}"))
            .spawn(move || {
                while let Ok(req) = req_rx.recv() {
                    let outcome = match attach_ring(&req.path, req.geometry) {
                        Ok(reader) => RingAttachOutcome::Attached(Box::new(reader)),
                        Err(reason) => RingAttachOutcome::Failed(reason),
                    };
                    if res_tx.send(outcome).is_err() {
                        break;
                    }
                }
            })?;
        Ok(Self {
            req_tx,
            res_rx,
            in_flight: false,
            pending_gauge,
        })
    }

    /// Mirror `in_flight` into the STATUS gauge. One relaxed store.
    fn publish_pending(&self) {
        self.pending_gauge.store(self.in_flight, Ordering::Relaxed);
    }

    /// Queue one attach. Returns whether it was queued: `false` when a request is
    /// already outstanding on this lane or the thread is gone. Never blocks.
    fn request(&mut self, path: &str, geometry: Geometry) -> bool {
        if self.in_flight {
            return false;
        }
        let queued = self
            .req_tx
            .send(RingAttachRequest {
                path: path.to_string(),
                geometry,
            })
            .is_ok();
        if queued {
            self.in_flight = true;
        }
        self.publish_pending();
        queued
    }

    /// Collect a finished attach, if one is ready. Never blocks.
    fn poll(&mut self) -> Option<RingAttachOutcome> {
        if !self.in_flight {
            return None;
        }
        let collected = match self.res_rx.try_recv() {
            Ok(outcome) => {
                self.in_flight = false;
                Some(outcome)
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => None,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                // The attacher thread is gone; stop expecting a result so the
                // lane can re-request (which fails fast) rather than latching
                // in-flight forever and never retrying again.
                self.in_flight = false;
                None
            }
        };
        self.publish_pending();
        collected
    }
}

/// Build a renderer-ingress lane. Attaches best-effort: a lane whose ring is not
/// yet available starts `Detached`, renders silence, and reattaches on its own
/// cadence — never failing the daemon. `pcm` is `None` (the aloop substream is not
/// opened at all), exactly as on the USB DIRECT lane.
///
/// The attach HERE is inline on purpose and is not the #2538 defect: this runs in
/// `Mixer::new`, on the constructing thread, before the render loop exists. Only
/// the retry inside the loop had to move ([`RingAttacher`]).
///
/// `lane_index` / `lane_count` position this lane inside the retry window
/// ([`reattach_phase`]) so detached lanes do not retry in lockstep.
pub(super) fn open_ring_input(
    label: &str,
    config: &Config,
    resampler: Option<LaneResampler>,
    lane_index: usize,
    lane_count: usize,
) -> Input {
    let path = crate::config::renderer_ring_path(label);
    let phase = reattach_phase(lane_index, lane_count);
    // `Config::from_env` refuses an armed lane whose geometry has no whole-slot
    // expression, so this is `Some` on every live ring lane. The fallback keeps
    // the function total rather than panicking on a config that cannot reach here.
    let geometry = renderer_ring_geometry(config).unwrap_or(Geometry {
        rate: config.sample_rate,
        channels: CHANNELS,
        sample_format: SAMPLE_FORMAT_S16LE,
        period_frames: config.period_frames,
        n_slots: crate::config::RING_SLOTS_MIN,
    });
    let obs = RingLaneObservability::new(path.clone(), geometry);

    let ring = match attach_ring(&path, geometry) {
        Ok(reader) => {
            obs.attached.store(true, Ordering::Relaxed);
            obs.attaches.fetch_add(1, Ordering::Relaxed);
            obs.publish(&reader.metrics());
            info!(
                "event=fanin.ring_lane.attached label={} path={} slot_frames={} n_slots={} attaches=1 (initial attach)",
                label, path, geometry.period_frames, geometry.n_slots,
            );
            RingCapture::Attached {
                reader: Box::new(reader),
                // Phase-seeded like the retry latch: this lane's orphan probe
                // shares the cadence constant, so seeding both from one value is
                // what keeps the lane de-phased whichever arm it starts in.
                periods_until_check: phase,
            }
        }
        Err(reason) => {
            obs.detach_reason.store(reason as u64, Ordering::Relaxed);
            warn!(
                "event=fanin.ring_lane.detached label={} path={} reason={} slot_frames={} n_slots={} (startup; renders silence, retries ~every {} periods, first in {} periods)",
                label,
                path,
                reason.as_str(),
                geometry.period_frames,
                geometry.n_slots,
                RING_REATTACH_RETRY_PERIODS,
                phase,
            );
            RingCapture::Detached {
                periods_until_retry: phase,
            }
        }
    };

    // The deferred attach channel (#2538). Spawned at construction, before `main`
    // calls `mlockall`, like every other fan-in helper thread. A spawn failure
    // leaves the lane WITHOUT self-heal rather than restoring the inline attach:
    // a 500 ms-bounded `flock` inside the render loop's 5.33 ms period budget,
    // against a two-slot downstream ring, is the defect being fixed.
    let ring_attacher = match RingAttacher::spawn(label, Arc::clone(&obs.attach_pending)) {
        Ok(attacher) => Some(attacher),
        Err(e) => {
            warn!(
                "event=fanin.ring_lane.attacher_unavailable label={} path={} detail={} — this \
                 ring will NOT be reattached after a loss until fan-in restarts (audio \
                 unaffected; the lane renders silence)",
                label, path, e,
            );
            None
        }
    };

    let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
    Input {
        // A ring lane does NOT open its aloop substream — its only source is the
        // SHM ring, the same way the DIRECT lane's only source is the gadget.
        pcm: None,
        direct: None,
        direct_opener: None,
        ring: Some(ring),
        ring_attacher,
        label: label.to_string(),
        // STATUS's `pcm` for this lane is the ring PATH, not an ALSA name: it is
        // where the lane's audio actually comes from, and a stale aloop name here
        // would tell an operator the opposite of the truth.
        pcm_name: path,
        read_buf: vec![0i16; period_samples],
        // A renderer ring lane is S16 at its wire (see `renderer_ring_geometry`),
        // so it has no spine-scale period to hold at any box width.
        read_buf_wide: Vec::new(),
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
        rms_dbfs_x100: Arc::new(AtomicI32::new((RMS_DBFS_FLOOR * 100.0) as i32)),
        catchup_resync_frames: Arc::new(AtomicU64::new(0)),
        catchup_events: Arc::new(AtomicU64::new(0)),
        resampler,
        trim: TrimControl::new(),
        muted: Arc::new(AtomicBool::new(false)),
        direct_obs: None,
        ring_obs: Some(obs),
        lane_fade: super::LaneFade::for_lane(label, config.sample_rate),
    }
}

/// Read one period from a renderer-ingress ring lane and render it into
/// `read_buf`. Returns the number of REAL (non-silence) frames — `period_frames`
/// on a filled slot, `0` on an empty ring or while detached.
///
/// Never returns `Err` and never blocks. One slot is consumed per call because the
/// ring's slot IS one fan-in render period by construction, so the whole read is a
/// single non-blocking `try_consume_slot` straight into the lane's existing
/// buffer: no allocation, no intermediate copy, no drain loop, and no catch-up
/// resync — a bounded ring cannot back up past its own depth the way an aloop
/// capture ring can, so `drain_input_excess` has nothing to do here and is not
/// called.
pub(super) fn read_ring_and_render(input: &mut Input, period_frames: usize) -> usize {
    // Take ownership of the state machine so the reattach path can mutate `input`
    // (observability, resampler) without a double borrow. Restored before return.
    //
    // A `None` here would mean the mixer dispatched a non-ring lane to this
    // function, which its `input.ring.is_some()` arm makes impossible. It is
    // still handled as SILENCE rather than a panic: this is a production audio
    // daemon, and the correct response to "a lane is somehow not what we thought"
    // is the same as the correct response to every other lane fault in this
    // module — contribute nothing and keep the mix running. A panic here would
    // take the whole summed music path down to enforce an invariant whose
    // violation costs one lane.
    let Some(mut ring) = input.ring.take() else {
        input.read_buf.fill(0);
        return 0;
    };

    // Set by the Attached arm's orphan probe; acted on after its borrow ends.
    let mut orphaned = false;
    let real_frames = match &mut ring {
        RingCapture::Attached {
            reader,
            periods_until_check,
        } => {
            // ORPHANED-INODE probe, on the slow cadence (an fstat + a stat is a
            // syscall pair, not something to pay per period). An mmap survives
            // an unlink, so a ring replaced underneath this reader leaves a
            // perfectly valid mapping of a file nothing writes any more — the
            // lane would report attached and read silence forever, which no
            // counter here distinguishes from an idle renderer. Detaching
            // re-latches onto the live file on the next period.
            if *periods_until_check == 0 {
                let owns = reader.owns_linked_path().unwrap_or(false);
                if !owns {
                    orphaned = true;
                } else {
                    *periods_until_check = RING_REATTACH_RETRY_PERIODS;
                }
            } else {
                *periods_until_check -= 1;
            }
            let want = period_frames * (CHANNELS as usize);
            // Guard the slot-shaped copy: `try_consume_slot` requires exactly one
            // slot's worth of samples. `read_buf` is built at
            // `period_frames * CHANNELS` and the geometry's slot is `period_frames`,
            // so these agree by construction; a mismatch would be a construction
            // bug, and rendering silence is the safe answer to one.
            if input.read_buf.len() < want {
                input.read_buf.fill(0);
                0
            } else {
                let read = reader.try_consume_slot(&mut input.read_buf[..want]);
                let metrics = reader.metrics();
                if let Some(obs) = &input.ring_obs {
                    obs.publish(&metrics);
                }
                match read {
                    SlotRead::Filled => {
                        input
                            .frames_read
                            .fetch_add(period_frames as u64, Ordering::Relaxed);
                        period_frames
                    }
                    // The ring zero-filled the buffer for us; the lane contributes
                    // silence. NOT a fault and NOT a reattach trigger — see the
                    // module docs on why the retry latch is armed only by an attach
                    // failure.
                    SlotRead::Empty => 0,
                }
            }
        }
        RingCapture::Detached { .. } => {
            input.read_buf.fill(0);
            ring = maybe_reattach_ring(ring, input);
            0
        }
    };

    if orphaned {
        if let Some(obs) = &input.ring_obs {
            obs.attached.store(false, Ordering::Relaxed);
            obs.detach_reason
                .store(RingDetachReason::Orphaned as u64, Ordering::Relaxed);
            warn!(
                "event=fanin.ring_lane.detached label={} path={} reason={} \
                 (the ring at this path was replaced while we held it open; \
                 re-latching onto the live file)",
                input.label,
                obs.path,
                RingDetachReason::Orphaned.as_str(),
            );
        }
        if let Some(r) = input.resampler.as_mut() {
            r.reset();
        }
        // periods_until_retry = 0: re-latch on the very next period. This is a
        // live file waiting to be opened, not an absent one to back off from.
        ring = RingCapture::Detached {
            periods_until_retry: 0,
        };
    }
    input.ring = Some(ring);
    real_frames
}

/// While `Detached`: adopt a finished attach if this lane's attacher thread has
/// one ready, else count the period-based retry latch down and QUEUE the next
/// attempt when it reaches 0. No wall clock — one decrement per render period,
/// the same latch idiom as the USB DIRECT lane's reopen — and, since #2538, no
/// blocking: the `create_or_attach` (and the bounded `flock` inside it) runs on
/// `fanin-ring-attacher`, so a detached lane costs this loop one non-blocking
/// `try_recv` per period instead of a full attach attempt every ~2 s inside the
/// period budget.
///
/// A successful reattach resets the lane's resampler (the ring's own attach
/// resync drops stale slots, so the lane resumes from the writer's tip and any
/// resampler state from before the gap is a discontinuity); a failed one re-arms
/// the latch and logs nothing, so a box whose renderer is switched off does not
/// fill the journal.
fn maybe_reattach_ring(ring: RingCapture, input: &mut Input) -> RingCapture {
    let RingCapture::Detached {
        periods_until_retry,
    } = ring
    else {
        return ring;
    };
    // 1. Did a queued attach finish? One non-blocking `try_recv`.
    if let Some(outcome) = input.ring_attacher.as_mut().and_then(|a| a.poll()) {
        return adopt_attach_outcome(outcome, input);
    }
    // 2. Nothing ready: count down, then queue one attempt.
    if periods_until_retry > 0 {
        return RingCapture::Detached {
            periods_until_retry: periods_until_retry - 1,
        };
    }
    let Some(obs) = input.ring_obs.clone() else {
        // Unreachable on a ring lane (construction always sets it); re-arm rather
        // than spin.
        return RingCapture::Detached {
            periods_until_retry: RING_REATTACH_RETRY_PERIODS,
        };
    };
    // A `None` attacher (spawn failed) cannot self-heal without blocking the
    // render loop, and blocking it is the defect: stay Detached (silence) with
    // the latch re-armed. `request` is also a no-op while one is already in
    // flight, so a slow attach can never be re-queued into a backlog.
    //
    // The geometry handed over is the SAME tuple the initial attach used — held
    // whole precisely so a reattach cannot present a rebuilt-from-parts geometry
    // the header then rejects.
    let queued = match input.ring_attacher.as_mut() {
        Some(attacher) => attacher.request(&obs.path, obs.geometry),
        None => false,
    };
    if queued {
        obs.retries.fetch_add(1, Ordering::Relaxed);
    }
    RingCapture::Detached {
        periods_until_retry: RING_REATTACH_RETRY_PERIODS,
    }
}

/// Adopt (or discard) one finished attach from this lane's attacher thread. Pure
/// bookkeeping plus the one transition log line — no ring calls of its own.
fn adopt_attach_outcome(outcome: RingAttachOutcome, input: &mut Input) -> RingCapture {
    let Some(obs) = input.ring_obs.clone() else {
        // Unreachable on a ring lane; a reader with nowhere to record itself is
        // dropped rather than adopted silently.
        return RingCapture::Detached {
            periods_until_retry: RING_REATTACH_RETRY_PERIODS,
        };
    };
    match outcome {
        RingAttachOutcome::Attached(reader) => {
            if let Some(r) = input.resampler.as_mut() {
                r.reset();
            }
            obs.attached.store(true, Ordering::Relaxed);
            let attaches = obs.attaches.fetch_add(1, Ordering::Relaxed) + 1;
            obs.publish(&reader.metrics());
            info!(
                "event=fanin.ring_lane.attached label={} path={} slot_frames={} n_slots={} attaches={} retries={} (reattached)",
                input.label,
                obs.path,
                obs.geometry.period_frames,
                obs.geometry.n_slots,
                attaches,
                obs.retries.load(Ordering::Relaxed),
            );
            RingCapture::Attached {
                reader,
                periods_until_check: RING_REATTACH_RETRY_PERIODS,
            }
        }
        RingAttachOutcome::Failed(reason) => {
            // Record the CURRENT reason (it can change — an `unavailable` ring that
            // a renderer then creates at a sheared geometry becomes `geometry`), but
            // do not log per retry: only the attached/detached TRANSITIONS log, the
            // same discipline as the direct lane's reopen.
            obs.detach_reason.store(reason as u64, Ordering::Relaxed);
            RingCapture::Detached {
                periods_until_retry: RING_REATTACH_RETRY_PERIODS,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    //! The renderer-ingress lane's PRESENCE model, exercised against a real
    //! `jasper_ring` writer on a real SHM file. Nothing here needs ALSA, a
    //! renderer, or a Pi — which is the point: the lane's resilience contract
    //! (absent ring, writer death, geometry shear, self-heal) is the part most
    //! likely to be wrong on hardware and least likely to be reachable there.

    use super::*;
    use jasper_ring::TestRingWriter;

    /// A lane geometry small enough to keep the fixtures fast and legible, and
    /// still a legal ring (`n_slots` inside 2..=16, one slot per period).
    const TEST_PERIOD: u32 = 64;
    const TEST_SLOTS: u32 = 4;

    fn test_geometry() -> Geometry {
        Geometry {
            rate: 48_000,
            channels: CHANNELS,
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames: TEST_PERIOD,
            n_slots: TEST_SLOTS,
        }
    }

    fn ring_path(name: &str) -> String {
        format!(
            "{}/jts-fanin-ringlane-{}-{}.ring",
            std::env::temp_dir().display(),
            name,
            std::process::id()
        )
    }

    /// Build a ring lane `Input` directly, bypassing `Config` so the test does
    /// not have to mutate process env. Mirrors `open_ring_input`'s construction
    /// exactly, so what the tests exercise is what production builds.
    fn ring_lane(path: &str) -> Input {
        let geometry = test_geometry();
        let obs = RingLaneObservability::new(path.to_string(), geometry);
        let ring = match attach_ring(path, geometry) {
            Ok(reader) => {
                obs.attached.store(true, Ordering::Relaxed);
                obs.attaches.fetch_add(1, Ordering::Relaxed);
                RingCapture::Attached {
                    reader: Box::new(reader),
                    periods_until_check: RING_REATTACH_RETRY_PERIODS,
                }
            }
            Err(reason) => {
                obs.detach_reason.store(reason as u64, Ordering::Relaxed);
                RingCapture::Detached {
                    periods_until_retry: RING_REATTACH_RETRY_PERIODS,
                }
            }
        };
        let period_samples = (TEST_PERIOD as usize) * (CHANNELS as usize);
        // A REAL attacher thread, like production: the reattach path is what most
        // of this module's tests exercise, and stubbing the worker would test a
        // lane shape no box ever runs.
        let ring_attacher =
            Some(RingAttacher::spawn("spotify", Arc::clone(&obs.attach_pending)).unwrap());
        Input {
            pcm: None,
            direct: None,
            direct_opener: None,
            ring: Some(ring),
            ring_attacher,
            label: "spotify".to_string(),
            pcm_name: path.to_string(),
            read_buf: vec![0i16; period_samples],
            read_buf_wide: Vec::new(),
            xrun_count: Arc::new(AtomicU64::new(0)),
            frames_read: Arc::new(AtomicU64::new(0)),
            rms_dbfs_x100: Arc::new(AtomicI32::new((RMS_DBFS_FLOOR * 100.0) as i32)),
            catchup_resync_frames: Arc::new(AtomicU64::new(0)),
            catchup_events: Arc::new(AtomicU64::new(0)),
            resampler: None,
            trim: TrimControl::new(),
            muted: Arc::new(AtomicBool::new(false)),
            direct_obs: None,
            ring_obs: Some(obs),
            lane_fade: super::LaneFade::for_lane("spotify", geometry.rate),
        }
    }

    fn cleanup(path: &str) {
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(format!("{path}.open.lock"));
    }

    fn is_detached(input: &Input) -> bool {
        matches!(input.ring, Some(RingCapture::Detached { .. }))
    }

    /// Whether an attach is queued on this lane's attacher thread right now.
    fn attach_pending(input: &Input) -> bool {
        input
            .ring_obs
            .as_ref()
            .is_some_and(|o| o.attach_pending.load(Ordering::Relaxed))
    }

    /// Poll an attacher the way the render loop does — non-blocking, with wall
    /// clock passing between attempts — until its result lands. Bounded so a
    /// broken worker fails the test instead of hanging it.
    fn collect(attacher: &mut RingAttacher) -> Option<RingAttachOutcome> {
        for _ in 0..2_000 {
            if let Some(outcome) = attacher.poll() {
                return Some(outcome);
            }
            std::thread::sleep(std::time::Duration::from_micros(200));
        }
        None
    }

    /// Drive render periods until `done` holds, up to `max_periods`.
    ///
    /// **Why a helper and not a bare loop.** Since #2538 the attach happens on the
    /// lane's `fanin-ring-attacher` thread and the render side adopts the result
    /// on a LATER period, so a loop that runs periods back to back at test speed
    /// can outrun the worker and conclude the lane never healed. Production cannot:
    /// one period is 5.33 ms of wall clock, which is several orders of magnitude
    /// more than an attach needs. This restores that by yielding — but only while
    /// an attach is actually queued, so a test that never queues one pays nothing.
    fn drive_until(
        input: &mut Input,
        max_periods: u64,
        mut done: impl FnMut(&Input) -> bool,
    ) -> bool {
        for _ in 0..max_periods {
            read_ring_and_render(input, TEST_PERIOD as usize);
            if done(input) {
                return true;
            }
            if attach_pending(input) {
                std::thread::sleep(std::time::Duration::from_micros(200));
            }
        }
        false
    }

    /// A lane whose ring cannot be attached at all renders SILENCE and does not
    /// spin: it reports zero real frames and stays detached across many periods,
    /// making at most one attach attempt per retry window.
    ///
    /// The no-spin half is the load-bearing one. A lane that retried the attach
    /// every period would `open()` a file ~187 times a second, forever, on any
    /// box whose renderer is switched off at `/sources/` — the exact
    /// unbounded-work failure the Pi budget forbids.
    #[test]
    fn an_unattachable_ring_renders_silence_and_retries_at_a_bounded_rate() {
        // An unattachable path that is unattachable for EVERY privilege level:
        // the parent is a regular FILE, so `ensure_parent_dir`'s `mkdir` fails
        // EEXIST — measured, `ErrorKind::AlreadyExists` / errno 17 — and root
        // gets the same.
        //
        // The obvious choice — a path under a directory that does not exist —
        // is NOT deterministic here. `attach_or_create` calls `ensure_parent_dir`
        // FIRST, so that case's failure is whatever CREATING the directory
        // returns: ENOENT where the parent is writable, EACCES on CI's non-root
        // runner (which is what turned this test red on its first-ever Linux
        // run), and SUCCESS as root — which would attach and invert every
        // assertion below. Parent-is-a-file removes the privilege dependency.
        let blocker = ring_path("notdir-blocker");
        cleanup(&blocker);
        std::fs::write(&blocker, b"not a directory").unwrap();
        let owned_path = format!("{blocker}/lane-spotify.ring");
        let path = owned_path.as_str();
        let mut input = ring_lane(path);
        assert!(
            is_detached(&input),
            "a lane whose ring cannot be attached must start Detached"
        );

        let periods = (RING_REATTACH_RETRY_PERIODS as usize) * 3 + 5;
        for _ in 0..periods {
            input.read_buf.fill(0x5A5A_u16 as i16);
            let frames = read_ring_and_render(&mut input, TEST_PERIOD as usize);
            assert_eq!(frames, 0, "a detached lane contributes no real frames");
            assert!(
                input.read_buf.iter().all(|&s| s == 0),
                "a detached lane must render DIGITAL SILENCE, not stale buffer content"
            );
        }

        let obs = input.ring_obs.as_ref().unwrap();
        assert!(!obs.attached.load(Ordering::Relaxed));
        let retries = obs.retries.load(Ordering::Relaxed);
        assert!(
            retries <= 4,
            "at most one attach attempt per {RING_REATTACH_RETRY_PERIODS}-period window \
             over {periods} periods; got {retries} (a per-period retry would be ~{periods})"
        );
        assert_eq!(
            obs.attaches.load(Ordering::Relaxed),
            0,
            "no attach ever succeeded"
        );
        // EEXIST is neither "absent" nor "geometry", so it classifies
        // `refused` — the token whose remedy points at the environment
        // (permissions / directory shape), which is the right advice here.
        //
        // Note this test pins the token for THIS errno only. The full
        // reason MAPPING is pinned on synthetic errors by
        // `attach_failures_classify_onto_the_remedy_they_need`, which is where a
        // mapping belongs; what THIS test owns is the behaviour — silence, no
        // spin, and never falsely attached.
        assert_eq!(
            obs.detach_reason_str(),
            RingDetachReason::Refused.as_str(),
            "a parent-is-a-file path fails EEXIST, which is a refusal to map \
             rather than an absent ring"
        );
        assert_eq!(
            input.frames_read.load(Ordering::Relaxed),
            0,
            "a detached lane must not claim frames it never read"
        );

        cleanup(&blocker);
    }

    /// The steady path: a live writer's slots are consumed one per period, byte
    /// for byte, and counted; an empty ring renders silence WITHOUT detaching.
    #[test]
    fn an_attached_lane_consumes_one_slot_per_period_and_silences_when_empty() {
        let path = ring_path("steady");
        cleanup(&path);
        let mut input = ring_lane(&path);
        assert!(
            !is_detached(&input),
            "the reader creates the ring when absent"
        );

        let mut writer = TestRingWriter::create_or_attach(&path, test_geometry()).unwrap();
        let samples = (TEST_PERIOD as usize) * (CHANNELS as usize);
        let payload: Vec<i16> = (0..samples).map(|i| (i as i16).wrapping_mul(7)).collect();
        assert!(writer.try_publish_slot(&payload));

        let frames = read_ring_and_render(&mut input, TEST_PERIOD as usize);
        assert_eq!(
            frames, TEST_PERIOD as usize,
            "a filled slot is a full period"
        );
        assert_eq!(
            input.read_buf, payload,
            "the ring is a dumb frame carrier: what the writer published is what the \
             lane mixes, bit for bit"
        );
        assert_eq!(
            input.frames_read.load(Ordering::Relaxed),
            TEST_PERIOD as u64,
        );

        // Ring now empty — the writer paused. Silence, no detach, no counter lie.
        input.read_buf.fill(0x1234);
        let frames = read_ring_and_render(&mut input, TEST_PERIOD as usize);
        assert_eq!(frames, 0, "an empty ring contributes no real frames");
        assert!(
            input.read_buf.iter().all(|&s| s == 0),
            "an empty ring must zero-fill the lane buffer"
        );
        assert!(
            !is_detached(&input),
            "an EMPTY ring is still an ATTACHED ring — arming the reattach latch here \
             would tear down a healthy mapping every time a renderer paused"
        );
        assert_eq!(
            input.frames_read.load(Ordering::Relaxed),
            TEST_PERIOD as u64,
            "an empty read must not advance frames_read"
        );

        cleanup(&path);
    }

    /// A writer that DIES mid-stream leaves the lane attached and silent, with
    /// `writer_alive` false and `empty_reads` climbing — the observable shape the
    /// campaign's standing ring watch reads. Crucially the lane does NOT detach,
    /// and it resumes the instant a writer returns.
    #[test]
    fn a_writer_that_dies_mid_stream_silences_the_lane_and_it_recovers_on_return() {
        let path = ring_path("writerdeath");
        cleanup(&path);
        let mut input = ring_lane(&path);
        let samples = (TEST_PERIOD as usize) * (CHANNELS as usize);
        let payload = vec![0x0101_i16; samples];

        {
            let mut writer = TestRingWriter::create_or_attach(&path, test_geometry()).unwrap();
            assert!(writer.try_publish_slot(&payload));
            assert_eq!(
                read_ring_and_render(&mut input, TEST_PERIOD as usize),
                TEST_PERIOD as usize
            );
        } // writer dropped == the renderer process exiting

        for _ in 0..8 {
            input.read_buf.fill(0x7777);
            let frames = read_ring_and_render(&mut input, TEST_PERIOD as usize);
            assert_eq!(frames, 0, "a dead writer's lane contributes silence");
            assert!(input.read_buf.iter().all(|&s| s == 0));
        }
        let obs = input.ring_obs.as_ref().unwrap().clone();
        assert!(
            obs.attached.load(Ordering::Relaxed),
            "a dead WRITER is not a dead RING — the lane stays attached"
        );
        assert!(
            obs.empty_reads.load(Ordering::Relaxed) >= 8,
            "steady-state empty reads must be counted after the first filled slot"
        );
        assert!(
            !obs.writer_alive.load(Ordering::Relaxed),
            "a dead writer must READ as dead in /state — `writer_alive` is the \
             observability half of the split (the flock owns exclusivity), and \
             an operator seeing attached=true needs this to tell a silent lane \
             from a live one"
        );

        // A renderer returning re-publishes; the lane picks it up with no reattach.
        let mut writer2 = TestRingWriter::create_or_attach(&path, test_geometry()).unwrap();
        let payload2 = vec![0x0202_i16; samples];
        assert!(writer2.try_publish_slot(&payload2));
        assert_eq!(
            read_ring_and_render(&mut input, TEST_PERIOD as usize),
            TEST_PERIOD as usize,
            "the lane must resume the instant a writer returns"
        );
        assert_eq!(input.read_buf, payload2);
        assert_eq!(
            obs.attaches.load(Ordering::Relaxed),
            1,
            "recovery from a writer restart needs NO reattach — one attach for the \
             life of the lane"
        );

        cleanup(&path);
    }

    /// A ring whose header declares a different geometry than the lane builds is
    /// refused, classified `geometry` (not `unavailable`), and the lane renders
    /// silence rather than reading frames at the wrong shape. This is the
    /// conf.d-vs-fan-in shear, and misreading it as a transient would leave an
    /// operator retrying forever with no idea why.
    #[test]
    fn a_sheared_ring_geometry_is_refused_and_named_as_a_geometry_fault() {
        let path = ring_path("shear");
        cleanup(&path);
        // A foreign writer creates the ring at a DIFFERENT depth first.
        let foreign = Geometry {
            n_slots: TEST_SLOTS + 1,
            ..test_geometry()
        };
        let _writer = TestRingWriter::create_or_attach(&path, foreign).unwrap();

        let mut input = ring_lane(&path);
        assert!(is_detached(&input), "a sheared geometry must not attach");
        assert_eq!(
            input.ring_obs.as_ref().unwrap().detach_reason_str(),
            RingDetachReason::Geometry.as_str(),
            "a shear is an operator fault with its own remediation — it must not be \
             reported as a merely-absent ring"
        );
        input.read_buf.fill(0x4242);
        assert_eq!(read_ring_and_render(&mut input, TEST_PERIOD as usize), 0);
        assert!(input.read_buf.iter().all(|&s| s == 0));

        cleanup(&path);
    }

    /// The SELF-HEAL: a lane that starts with no ring attaches once one appears,
    /// and then carries audio. The retry latch is what makes this reachable, so
    /// the test drives real periods rather than calling the reattach directly.
    #[test]
    fn a_detached_lane_self_heals_when_a_ring_appears() {
        let path = ring_path("selfheal");
        cleanup(&path);
        // Start detached by pointing at an uncreatable path, then swap the
        // observability path to a creatable one — the same transition a real box
        // makes when its ring directory or renderer arrives.
        //
        // Parent-is-a-file (EEXIST) rather than parent-does-not-exist, for the
        // same privilege-independence reason as
        // `an_unattachable_ring_renders_silence_and_retries_at_a_bounded_rate`:
        // a missing directory is creatable by root, which would attach here and
        // make the test assert nothing.
        let blocker = ring_path("selfheal-blocker");
        cleanup(&blocker);
        std::fs::write(&blocker, b"not a directory").unwrap();
        let mut input = ring_lane(&format!("{blocker}/lane-spotify.ring"));
        assert!(is_detached(&input));
        // Repoint the PATH only, carrying every shared counter — above all the
        // `attach_pending` gauge, whose `Arc` the attacher thread captured at
        // spawn. Production never swaps a lane's observability at all (it is
        // built once in `open_ring_input`), so a wholesale replacement here would
        // silently detach the gauge from the worker that publishes it and leave
        // this test reading a value nothing writes.
        let healed = RingLaneObservability {
            path: path.clone(),
            ..input.ring_obs.clone().unwrap()
        };
        healed
            .detach_reason
            .store(RingDetachReason::Unavailable as u64, Ordering::Relaxed);
        input.ring_obs = Some(healed);

        let attached = drive_until(&mut input, RING_REATTACH_RETRY_PERIODS + 32, |i| {
            !is_detached(i)
        });
        assert!(
            attached,
            "a detached lane must reattach within one retry window once its ring is \
             obtainable — self-heal without an operator is the whole contract"
        );
        let obs = input.ring_obs.as_ref().unwrap();
        assert!(obs.attached.load(Ordering::Relaxed));
        assert_eq!(obs.attaches.load(Ordering::Relaxed), 1);

        let mut writer = TestRingWriter::create_or_attach(&path, test_geometry()).unwrap();
        let samples = (TEST_PERIOD as usize) * (CHANNELS as usize);
        assert!(writer.try_publish_slot(&vec![0x3333_i16; samples]));
        assert_eq!(
            read_ring_and_render(&mut input, TEST_PERIOD as usize),
            TEST_PERIOD as usize,
            "a self-healed lane must actually carry audio, not merely report attached"
        );

        cleanup(&blocker);
        cleanup(&path);
    }

    /// The steady read path allocates nothing: the lane's buffer is reused period
    /// after period and never grows. Asserted through capacity rather than an
    /// allocator hook, which is the strongest hardware-free statement available
    /// and pins the property the Pi budget actually cares about.
    #[test]
    fn the_steady_read_path_reuses_the_lane_buffer_and_never_grows_it() {
        let path = ring_path("noalloc");
        cleanup(&path);
        let mut input = ring_lane(&path);
        let mut writer = TestRingWriter::create_or_attach(&path, test_geometry()).unwrap();
        let samples = (TEST_PERIOD as usize) * (CHANNELS as usize);
        let cap = input.read_buf.capacity();
        let ptr = input.read_buf.as_ptr();

        for i in 0..64 {
            if i % 2 == 0 {
                assert!(writer.try_publish_slot(&vec![i as i16; samples]));
            }
            read_ring_and_render(&mut input, TEST_PERIOD as usize);
        }
        assert_eq!(input.read_buf.capacity(), cap, "no reallocation");
        assert_eq!(input.read_buf.as_ptr(), ptr, "the same buffer throughout");
        assert!(
            input.read_buf_wide.is_empty(),
            "a renderer ring lane is S16 at its wire and must hold no spine-scale \
             buffer — U3 moves the transport, not the width"
        );

        cleanup(&path);
    }

    /// The lane's transport, `Input::pcm`'s emptiness, and STATUS's `source`
    /// token all follow from the same field, so they cannot disagree.
    #[test]
    fn a_ring_lane_reports_source_ring_and_opens_no_aloop_pcm() {
        let path = ring_path("source");
        cleanup(&path);
        let input = ring_lane(&path);
        assert_eq!(input.lane_source(), LaneSource::Ring);
        assert_eq!(input.lane_source().as_str(), "ring");
        assert!(
            input.pcm.is_none(),
            "a ring lane must NOT hold an aloop substream — one lane, one source"
        );
        assert!(!input.is_direct());
        assert!(input.ring_observability().is_some());
        assert!(input.direct_observability().is_none());
        cleanup(&path);
    }

    /// A ring REPLACED underneath a live reader is detected and re-latched.
    ///
    /// An mmap survives an unlink, so without the orphan probe the lane would
    /// hold a valid mapping of a dead inode: `attached:true`, reads succeeding,
    /// silence forever, and no counter separating it from an idle renderer.
    /// This is the shape an arm/disarm (which clears a stale ring) or a
    /// geometry change produces on a running box.
    #[test]
    fn a_ring_replaced_underneath_the_reader_is_relatched_not_read_forever() {
        let path = ring_path("orphan");
        cleanup(&path);
        let mut input = ring_lane(&path);
        let samples = (TEST_PERIOD as usize) * (CHANNELS as usize);

        // Prove the lane works on the ORIGINAL file first.
        {
            let mut w = TestRingWriter::create_or_attach(&path, test_geometry()).unwrap();
            assert!(w.try_publish_slot(&vec![0x0A0A_i16; samples]));
            assert_eq!(
                read_ring_and_render(&mut input, TEST_PERIOD as usize),
                TEST_PERIOD as usize
            );
        }

        // Replace the file at the same path — exactly what clearing a stale ring
        // does. The reader still holds the OLD inode.
        std::fs::remove_file(&path).unwrap();
        let _ = std::fs::remove_file(format!("{path}.open.lock"));
        let mut fresh = TestRingWriter::create_or_attach(&path, test_geometry()).unwrap();

        // Drive periods until the orphan probe fires and the lane re-latches.
        // The probe runs on the slow cadence, so this needs the full window plus
        // the re-latch period — which is the point: the lane recovers on its own,
        // with no operator and no restart.
        let obs = input.ring_obs.as_ref().unwrap().clone();
        let relatched = drive_until(&mut input, RING_REATTACH_RETRY_PERIODS + 32, |_| {
            obs.attaches.load(Ordering::Relaxed) >= 2
        });
        assert!(
            relatched,
            "the lane must notice the replaced inode and re-latch; holding the \
             orphaned mapping would read silence forever while reporting attached"
        );
        assert!(obs.attached.load(Ordering::Relaxed));

        // Publish AFTER the re-latch, not before. A fresh attach resyncs
        // `read_seq = write_seq` (stale slots are worthless to a pacer), so a
        // slot published before the re-latch is deliberately dropped by it —
        // asserting on that slot would test the reader's resync, not the
        // re-latch. What matters is that the re-latched lane carries LIVE audio.
        assert!(fresh.try_publish_slot(&vec![0x0B0B_i16; samples]));
        let frames = read_ring_and_render(&mut input, TEST_PERIOD as usize);
        assert_eq!(
            frames, TEST_PERIOD as usize,
            "the re-latched lane must carry audio from the LIVE file"
        );
        assert_eq!(
            input.read_buf[0], 0x0B0B,
            "and it must be the new file's audio, not the orphaned mapping's"
        );

        cleanup(&path);
    }

    /// An orphan detach names ITSELF, not one of the other three reasons — the
    /// remedies differ and a mislabelled one sends an operator the wrong way.
    #[test]
    fn every_detach_reason_including_orphaned_has_its_own_token() {
        let obs = RingLaneObservability::new("/tmp/x.ring".to_string(), test_geometry());
        obs.detach_reason
            .store(RingDetachReason::Orphaned as u64, Ordering::Relaxed);
        assert_eq!(obs.detach_reason_str(), "orphaned");
    }

    /// Every detach reason round-trips through the atomic `/state` publishes as
    /// its own token. A reason that collapsed onto another's spelling would tell
    /// an operator to apply the wrong remedy.
    #[test]
    fn every_detach_reason_has_its_own_published_token() {
        let obs = RingLaneObservability::new("/tmp/x.ring".to_string(), test_geometry());
        let all = [
            RingDetachReason::Unavailable,
            RingDetachReason::Geometry,
            RingDetachReason::Refused,
            RingDetachReason::Orphaned,
        ];
        let mut seen = std::collections::BTreeSet::new();
        for reason in all {
            obs.detach_reason.store(reason as u64, Ordering::Relaxed);
            assert_eq!(obs.detach_reason_str(), reason.as_str());
            assert!(seen.insert(reason.as_str()), "duplicate token {reason:?}");
        }
        assert_eq!(seen.len(), all.len());
    }

    /// The classifier maps the ring's own error vocabulary onto the three
    /// remedies, rather than lumping everything into one.
    #[test]
    fn attach_failures_classify_onto_the_remedy_they_need() {
        use std::io::{Error, ErrorKind};
        assert_eq!(
            RingDetachReason::classify(&Error::from(ErrorKind::NotFound)),
            RingDetachReason::Unavailable
        );
        assert_eq!(
            RingDetachReason::classify(&Error::from(ErrorKind::InvalidData)),
            RingDetachReason::Geometry
        );
        assert_eq!(
            RingDetachReason::classify(&Error::from(ErrorKind::InvalidInput)),
            RingDetachReason::Geometry
        );
        assert_eq!(
            RingDetachReason::classify(&Error::from(ErrorKind::PermissionDenied)),
            RingDetachReason::Refused,
            "a permission refusal is the renderer-user/UMask class and needs its own \
             remedy, not 'the ring is missing'"
        );
    }

    /// #2538: at most ONE attach is in flight per lane, ever.
    ///
    /// The latch is the thing that keeps a slow attach from building a backlog:
    /// the retry window keeps expiring while an attach runs, and without the
    /// latch each expiry would queue another. The worker holds a `.open.lock`
    /// while it works, so a queue of them would serialize into an ever-growing
    /// tail of bounded waits — with the newest request, the only one anyone
    /// wants, at the back.
    #[test]
    fn at_most_one_attach_is_in_flight_per_lane() {
        let path = ring_path("inflight");
        cleanup(&path);
        let gauge = Arc::new(AtomicBool::new(false));
        let mut attacher = RingAttacher::spawn("spotify", Arc::clone(&gauge)).unwrap();

        assert!(
            attacher.request(&path, test_geometry()),
            "the first request queues"
        );
        assert!(
            gauge.load(Ordering::Relaxed),
            "a queued attach must be visible in /state as attach_pending"
        );
        for _ in 0..16 {
            assert!(
                !attacher.request(&path, test_geometry()),
                "a second request must be REFUSED while one is outstanding — that \
                 refusal is what bounds the queue at one"
            );
        }

        assert!(
            matches!(collect(&mut attacher), Some(RingAttachOutcome::Attached(_))),
            "a creatable path must attach"
        );
        assert!(
            !gauge.load(Ordering::Relaxed),
            "collecting the result must clear attach_pending"
        );
        assert!(
            attacher.request(&path, test_geometry()),
            "and the latch must REOPEN once the result is collected, or the lane \
             could never retry again"
        );

        // Drain the second attach before removing the file: `create_or_attach`
        // RE-CREATES a ring it does not find, so an in-flight worker racing the
        // cleanup would leave a stray file behind in the temp dir.
        let _ = collect(&mut attacher);
        cleanup(&path);
    }

    /// #2538: `poll` on an idle attacher is a no-op, and a DEAD worker thread
    /// releases the latch instead of pinning it in flight forever.
    ///
    /// The dead-worker direction is the one that matters: a lane that latched
    /// `in_flight` on a disconnected channel would stop retrying for the life of
    /// the process, turning a recoverable ring into permanent silence.
    #[test]
    fn a_dead_attacher_thread_releases_the_latch_rather_than_pinning_it() {
        let gauge = Arc::new(AtomicBool::new(false));
        let mut attacher = RingAttacher::spawn("spotify", Arc::clone(&gauge)).unwrap();
        assert!(
            attacher.poll().is_none(),
            "polling with nothing queued must not block or invent a result"
        );

        // Simulate a worker that died with a request outstanding: latch in flight,
        // then swap in a receiver whose sender is already gone (`channel().1`
        // drops the `Sender` on the spot), which is exactly what `try_recv` sees
        // after the real thread exits.
        attacher.in_flight = true;
        attacher.publish_pending();
        attacher.res_rx = std::sync::mpsc::channel().1;
        assert!(attacher.poll().is_none());
        assert!(
            !attacher.in_flight,
            "a disconnected result channel must RELEASE the in-flight latch — \
             holding it would stop this lane retrying forever"
        );
        assert!(
            !gauge.load(Ordering::Relaxed),
            "and /state must stop claiming an attach is pending"
        );
    }

    /// #2538: lanes are PHASE-SEEDED so they do not all retry in the same render
    /// period. Every lane gets a distinct offset, and every offset is inside the
    /// window (a phase at or past the window would delay the first attempt).
    #[test]
    fn reattach_phases_spread_lanes_across_the_retry_window() {
        for lane_count in 1..=8usize {
            let phases: Vec<u64> = (0..lane_count)
                .map(|i| reattach_phase(i, lane_count))
                .collect();
            let unique: std::collections::BTreeSet<u64> = phases.iter().copied().collect();
            assert_eq!(
                unique.len(),
                lane_count,
                "with {lane_count} lanes every lane needs its OWN phase, else two \
                 lanes retry in the same period: {phases:?}"
            );
            assert_eq!(
                phases[0], 0,
                "the first lane must not be delayed — phase is a spread, not a \
                 blanket startup penalty"
            );
            for p in &phases {
                assert!(
                    *p < RING_REATTACH_RETRY_PERIODS,
                    "a phase at or past the window would push the first attempt \
                     beyond one retry period: {phases:?}"
                );
            }
        }
        // The shipped four-lane box, spelled out: quarter-window steps.
        assert_eq!(
            (0..4).map(|i| reattach_phase(i, 4)).collect::<Vec<_>>(),
            vec![0, 96, 192, 288]
        );
        // Total on a degenerate count rather than dividing by zero.
        assert_eq!(reattach_phase(0, 0), 0);
        assert_eq!(reattach_phase(7, 0), 0);
    }

    /// #2538: a lane whose attacher thread could not be spawned stays SILENT and
    /// never falls back to attaching inline.
    ///
    /// The fail direction is deliberate and is the whole point of the change: an
    /// inline fallback would restore a 500 ms-bounded `flock` inside a 5.33 ms
    /// render period on precisely the box where something is already wrong.
    #[test]
    fn a_lane_with_no_attacher_stays_silent_instead_of_attaching_inline() {
        let path = ring_path("noattacher");
        cleanup(&path);
        let mut input = ring_lane(&path);
        // A ring exists at this path (the fixture attached to it), so an inline
        // fallback WOULD succeed — which is what makes the assertion meaningful.
        input.ring_attacher = None;
        input.ring = Some(RingCapture::Detached {
            periods_until_retry: 0,
        });
        let obs = input.ring_obs.as_ref().unwrap().clone();
        let attaches_before = obs.attaches.load(Ordering::Relaxed);

        for _ in 0..(RING_REATTACH_RETRY_PERIODS * 2) {
            input.read_buf.fill(0x5A5A_u16 as i16);
            assert_eq!(read_ring_and_render(&mut input, TEST_PERIOD as usize), 0);
            assert!(input.read_buf.iter().all(|&s| s == 0));
        }
        assert!(is_detached(&input), "the lane must stay detached");
        assert_eq!(
            obs.attaches.load(Ordering::Relaxed),
            attaches_before,
            "with no worker to hand the attach to, the render path must NOT attach \
             the ring itself"
        );
        assert_eq!(
            obs.retries.load(Ordering::Relaxed),
            0,
            "and it must not count attempts it never made"
        );

        cleanup(&path);
    }
}
