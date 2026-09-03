// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Renderer-ingress ring capture: the third fan-in lane source.
//!
//! A renderer-ingress lane reads a per-renderer SHM slot ring
//! (`/dev/shm/jts-ring/lane-<label>.ring`) that the renderer's own `jts_ring`
//! ioplug writes, instead of reading that lane's snd-aloop capture substream.
//! Only the transport changes: the lane keeps its position in the sum, its
//! selection/mute gate, its RMS meter, and (where it has one) its
//! `LaneResampler`.
//!
//! ## Presence model
//!
//! The same two-state machine as [`super::direct_capture::DirectCapture`]:
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
//! lane, so a renderer that is off, a ring that has not been created yet, and a
//! stale ring from a previous boot all render silence and self-heal rather than
//! taking the whole summed music path down with them.
//!
//! ## A dead writer is not a detached ring
//!
//! `jasper_ring`'s reader owns writer liveness: an empty ring zero-fills the
//! caller's buffer and returns [`SlotRead::Empty`], and
//! [`RingMetrics::writer_alive`] reports a pid-plus-heartbeat view that goes
//! false within [`jasper_ring::WRITER_LIVENESS_TIMEOUT_NS`] of the writer
//! stopping. The ring is still perfectly attached, which is why the retry latch
//! below is armed ONLY by an attach failure, never by an empty read: arming it on
//! empty reads would tear down and rebuild a healthy mapping every time a
//! renderer paused.
//!
//! ## Allocation and blocking
//!
//! Zero allocation in the steady path: [`read_ring_and_render`] consumes directly
//! into the lane's existing period buffer, and the retry path formats no strings
//! (the detach reason is a `&'static str` on a copy `enum`). Neither path blocks.
//! `try_consume_slot` is a non-blocking memcpy-or-zero-fill, and the attach —
//! `RingReader::create_or_attach`, which takes an inter-process `flock` bounded
//! at `OPEN_LOCK_WAIT_TIMEOUT_MS` = 500 ms (`jasper-ring/src/lib.rs`) plus the
//! open/mmap/header-validate work — runs on a `fanin-ring-attacher` thread
//! ([`RingAttacher`], #2538) rather than INLINE once per
//! [`RING_REATTACH_RETRY_PERIODS`] (≈2 s) per detached lane. Against a 5.33 ms
//! render period and two 128-frame slots of downstream cushion, inline was a
//! worst case of ~187 slots of audio — the #2533 defect class, on a path that
//! needs no USB host to fire.
//!
//! **How often is a lane detached?** Not "whenever the renderer is idle" — that
//! is the ATTACHED-with-`writer_alive:false` state above, because
//! `create_or_attach` CREATES the ring when it is absent and fan-in (root) can
//! always do so, `ensure_parent_dir` creating the directory first. Detached means
//! a genuine fault (a geometry shear, a permission refusal, a full tmpfs) or a
//! transient (`.open.lock` contention against the writer's own open, the single
//! period an orphan re-latch spends detached). A shear or a permissions fault
//! persists until an operator fixes it, so an affected box paid the inline cost
//! every ~2 s for as long as it lasted.
//!
//! The retry latches are also PHASE-SEEDED per lane ([`reattach_phase`]) so a box
//! with several detached lanes spreads its attempts across the window.

use super::*;

use jasper_ring::{Geometry, RingMetrics, RingReader, SlotRead, SAMPLE_FORMAT_S32LE};

/// Render periods between reattach attempts while `Detached`, and between
/// orphaned-inode probes while `Attached`.
///
/// At the shipped geometry one period is 256 frames @ 48 kHz = 5.333 ms, so 384
/// periods is 2.048 s. (375 would be the exact 2 s; 384 is chosen instead
/// because it is a power-of-two multiple, which keeps the arithmetic exact
/// across the period sizes fan-in actually runs and costs 48 ms of latency
/// nobody can perceive on a reattach.) Same cadence as the USB DIRECT lane's
/// `DIRECT_REOPEN_RETRY_PERIODS`: frequent enough that a renderer coming up is
/// picked up within a couple of seconds, rare enough that a box whose renderer
/// is switched OFF at `/sources/` is not opening a file 187 times a second
/// forever.
///
/// Keep this cadence below any ring-writing renderer's `RestartSec` (librespot
/// ships `RestartSec=5`) so a crash-looping renderer is found by the FIRST retry
/// after each respawn rather than missed between probes; if a renderer ever
/// restarts faster than ~2 s, this constant is what has to move, not the unit.
pub(super) const RING_REATTACH_RETRY_PERIODS: u64 = 384;

/// This lane's PHASE within the retry window, in render periods: the countdown a
/// ring lane starts life with, so N lanes spread their FIRST attempts evenly
/// across [`RING_REATTACH_RETRY_PERIODS`] rather than firing in one period
/// (#2538). Nothing here reads a wall clock: the phase is decided once and then
/// carried by the ordinary latch. What it spreads is the WORKER side — several
/// lanes detached at once (a geometry shear across the conf.d, a tmpfs that
/// filled) each take their own bounded `flock` and contend for
/// `/dev/shm/jts-ring/`.
///
/// It is NOT a standing guarantee that two lanes never retry in the same period:
///
/// * the cycle is not exactly this constant. A queue re-arms the latch to
///   [`RING_REATTACH_RETRY_PERIODS`] and so does ADOPTING the result, so a lane
///   whose attach finishes one period later runs a 386-period cycle. Separation
///   holds while lanes' attach latencies match and drifts when they differ.
/// * the orphan re-latch in [`read_ring_and_render`] sets the latch to 0
///   unconditionally — deliberately, since that is a live file waiting to be
///   opened — so lanes orphaned in the same period are in lockstep from then on.
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
    /// classification, and a genuinely NARROW one: a RACE, the ring file or its
    /// directory disappearing between the create/attach steps (an arm/disarm or a
    /// geometry-change clear running underneath this open).
    ///
    /// It is NOT "fan-in started before the renderer": [`attach_ring`] creates
    /// the ring when it is absent (`O_CREAT|O_EXCL`, with `ensure_parent_dir`
    /// creating the directory first) and fan-in runs as root, so starting before
    /// — or entirely without — the renderer produces an ATTACH. Nor is it the
    /// other modes people reach for: `.open.lock` exhaustion returns `EAGAIN`
    /// (`WouldBlock`), a full tmpfs returns `ENOSPC` (`StorageFull`), and a
    /// permission refusal returns `EACCES` (`PermissionDenied`); all three
    /// classify as [`RingDetachReason::Refused`], whose remediation text fits
    /// them.
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
    /// indistinguishable from an idle source on every other counter. Self-heals
    /// by re-latching onto the live file, so it needs no operator; it is a
    /// distinct token only so the journal shows the re-latch happened.
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

/// Shared renderer-ring counters for the STATUS `ring{}` block: the mixer work
/// thread writes them, the state-server thread reads them lock-free, and they are
/// cloned into [`crate::state::InputSnapshotSource`] at construction.
///
/// Everything `jasper_ring` already counts is MIRRORED rather than re-derived —
/// re-deriving occupancy or empty-read counts in fan-in would be a second source
/// of truth for numbers the ring already owns.
#[derive(Clone)]
pub struct RingLaneObservability {
    /// The ring file this lane reads (`/dev/shm/jts-ring/lane-<label>.ring`).
    pub path: String,
    /// The geometry this lane attaches with, held whole rather than as loose
    /// scalars: the reattach path must present the SAME tuple the initial attach
    /// used or the header comparison rejects it. STATUS reads `period_frames` /
    /// `n_slots` off it.
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
    /// or the geometry/permissions are wrong (read `detach_reason`). An attempt
    /// is counted when it is HANDED to the `fanin-ring-attacher` thread, not when
    /// it runs, and `RingAttacher::request` refuses while one is outstanding, so
    /// this stays flat for as long as an attach is executing.
    ///
    /// **The dead-worker tell.** This value FROZEN alongside `attached=false`
    /// and `attach_pending=false` means the attacher thread is gone: the lane is
    /// detached, nothing is queued, and nothing is being attempted — a silent
    /// wedge that no other counter separates from a lane nobody is looking at.
    pub retries: Arc<AtomicU64>,
    /// Whether an attach is currently QUEUED on this lane's
    /// `fanin-ring-attacher` thread (#2538). Stuck `true` alongside a frozen
    /// `retries` says the attach itself is hanging (a held `.open.lock`, a wedged
    /// tmpfs), which costs this lane silence rather than the speaker's slots.
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
    /// Times the observed writer epoch changed — the renderer reattached. The
    /// DISCRIMINATOR for a drain: `empty_reads` rising with `epoch_resets` flat
    /// is a drain; both rising is a writer restart artefact.
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
    /// safe on the hot path.
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
    /// [`RingDetachReason::Orphaned`]), on the reattach cadence.
    Attached {
        reader: Box<RingReader>,
        periods_until_check: u64,
    },
    /// No ring. The lane renders silence and retries the attach at most once per
    /// [`RING_REATTACH_RETRY_PERIODS`], counted down one per render period.
    Detached { periods_until_retry: u64 },
}

/// The geometry a renderer-ingress lane attaches with. Stereo at the box's
/// sample rate, one slot per fan-in render period, depth derived from the aloop
/// cushion the lane replaced.
///
/// The WIRE is this box's one resolved width ([`Config::program_wire_is_wide`]) —
/// a renderer lane is not a second width axis. The write end declares the same
/// width in `deploy/alsa/conf.d/61-jts-renderer-lanes.conf`; fan-in creates the
/// ring, so a box whose declarations have sheared fails the RENDERER's
/// `snd_pcm_open` loudly rather than narrowing anything silently.
pub(super) fn renderer_ring_geometry(config: &Config) -> Option<Geometry> {
    Some(Geometry {
        rate: config.sample_rate,
        channels: CHANNELS,
        sample_format: config.ring_wire_format.sample_format_id(),
        period_frames: config.period_frames,
        n_slots: config.renderer_ring_slots()?,
    })
}

/// Attempt one attach of a renderer-ingress ring, returning the reader or the
/// classified reason it could not be had.
///
/// `RingReader::create_or_attach` CREATES the ring when it is absent: whichever
/// end starts first materializes the file, and the directory's setgid bit plus
/// both ends' `UMask=0007` make it group-writable either way (both ends write the
/// header — the reader stamps `read_seq` and its heartbeat). Creating from the
/// reader side also makes fan-in's derived geometry the one that lands in a fresh
/// header, so a renderer whose conf.d has sheared fails ITS open loudly instead
/// of silently establishing a ring fan-in then refuses.
fn attach_ring(path: &str, geometry: Geometry) -> Result<RingReader, RingDetachReason> {
    RingReader::create_or_attach(path, geometry).map_err(|e| RingDetachReason::classify(&e))
}

/// One reattach for the `fanin-ring-attacher` thread to perform OFF the render
/// thread. The geometry travels with the request so the worker presents the SAME
/// tuple the initial attach used — the header comparison rejects a
/// rebuilt-from-parts one.
struct RingAttachRequest {
    path: String,
    geometry: Geometry,
}

/// The result of one queued attach. The `io::Error` is not carried across the
/// channel — it is classified on the worker thread, so the render thread receives
/// the same `Copy` token it stores in `/state` and nothing more.
enum RingAttachOutcome {
    Attached(Box<RingReader>),
    Failed(RingDetachReason),
}

/// A renderer-ingress lane's deferred attach channel (#2538): [`attach_ring`]
/// runs here instead of in the mixer's render loop. One `Sender::send` per retry
/// window and one `try_recv` per detached period; nothing blocks.
///
/// The render loop's budget is one period (5.33 ms at the shipped 256 frames) and
/// the downstream pipeline holds two 128-frame slots of cushion, so anything over
/// ~2.7 ms costs a whole slot: CamillaDSP reads an empty Ring A (a 128-frame
/// silence INSERTION) or fan-in free-run-drops a slot it could not publish in
/// time (a 128-frame DELETION). The 500 ms `.open.lock` deadline
/// (`OPEN_LOCK_WAIT_TIMEOUT_MS` in `jasper-ring`) is ~187 of those slots, and
/// fan-in's own `RingStallTracker` has a 1 s floor and cannot see any of it.
///
/// **One attacher per lane, deliberately.** Each lane owns its own in-flight
/// latch; a shared worker would serialize four lanes' bounded waits behind one
/// another, so a fourth lane could sit 1.5 s behind a first lane's slow attach.
/// The threads block in `recv()` when idle and the rings they lock are different
/// inodes, so they do not contend. The cost that is NOT free is MEMORY: one
/// extra thread per armed ring lane at `crate::HELPER_STACK_BYTES`, and because
/// they are spawned in `Mixer::new` — before `main` calls `lock_memory()` —
/// their stacks are inside the `mlockall` and stay resident. Small against a
/// 1 GB Pi, paid only on a box with armed ring lanes, and the reason this is a
/// per-lane thread rather than a per-lane thread POOL.
pub(super) struct RingAttacher {
    req_tx: Sender<RingAttachRequest>,
    res_rx: std::sync::mpsc::Receiver<RingAttachOutcome>,
    /// A request is queued and its result has not been collected. At most one is
    /// ever outstanding on this lane, so a slow or hung attach cannot build a
    /// backlog of retries behind it.
    in_flight: bool,
    /// The STATUS mirror of `in_flight` (`ring.attach_pending`), shared with the
    /// state-server thread.
    pending_gauge: Arc<AtomicBool>,
}

impl RingAttacher {
    /// Spawn the attacher thread. Called at lane construction
    /// ([`open_ring_input`]), which `Mixer::new` runs before `main` calls
    /// `mlockall`.
    pub(super) fn spawn(label: &str, pending_gauge: Arc<AtomicBool>) -> std::io::Result<Self> {
        let (req_tx, req_rx) = std::sync::mpsc::channel::<RingAttachRequest>();
        let (res_tx, res_rx) = std::sync::mpsc::channel::<RingAttachOutcome>();
        std::thread::Builder::new()
            // One thread per lane, so the LABEL rides the name. Longer than
            // Linux's 15-byte visible comm: std's Linux `set_name` truncates to
            // `TASK_COMM_LEN` BEFORE calling `pthread_setname_np`, so the call
            // succeeds and its debug-build assertion holds in a test binary too.
            // `fanin-direct-opener` is already over the limit the same way.
            .name(format!("fanin-ring-attacher-{label}"))
            .stack_size(crate::HELPER_STACK_BYTES)
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
/// `Mixer::new`, on the constructing thread, before the render loop exists.
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
    // expression, so this is `Some` on every live ring lane; the fallback keeps
    // the function total rather than panicking.
    let geometry = renderer_ring_geometry(config).unwrap_or(Geometry {
        rate: config.sample_rate,
        channels: CHANNELS,
        sample_format: config.ring_wire_format.sample_format_id(),
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
                // Phase-seeded like the retry latch, so the lane is de-phased
                // whichever arm it starts in.
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

    // A spawn failure leaves the lane WITHOUT self-heal rather than restoring the
    // inline attach: a 500 ms-bounded `flock` inside the render loop's 5.33 ms
    // period budget, against a two-slot downstream ring, is the #2538 defect.
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

    ring_lane_input(label, path, geometry, ring, ring_attacher, resampler, obs)
}

/// The one place a ring lane's `Input` is assembled: the lane's period width
/// follows the ring's own `sample_format`, so the slot geometry and the buffer it
/// is consumed into cannot disagree.
fn ring_lane_input(
    label: &str,
    path: String,
    geometry: Geometry,
    ring: RingCapture,
    ring_attacher: Option<RingAttacher>,
    resampler: Option<LaneResampler>,
    obs: RingLaneObservability,
) -> Input {
    let period_samples = (geometry.period_frames as usize) * (CHANNELS as usize);
    Input {
        // A ring lane does NOT open its aloop substream — its only source is the
        // SHM ring.
        pcm: None,
        direct: None,
        direct_opener: None,
        ring: Some(ring),
        ring_attacher,
        label: label.to_string(),
        // STATUS's `pcm` for this lane is the ring PATH, not an ALSA name: a
        // stale aloop name here would tell an operator the opposite of the truth.
        pcm_name: path,
        read_buf: vec![0i16; period_samples],
        read_buf_wide: super::spine_read_buf(
            geometry.sample_format == SAMPLE_FORMAT_S32LE,
            period_samples,
        ),
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
        lane_fade: super::LaneFade::for_lane(label, geometry.rate),
    }
}

/// Read one period from a renderer-ingress ring lane and render it into the
/// lane's period buffer. Returns the number of REAL (non-silence) frames —
/// `period_frames` on a filled slot, `0` on an empty ring or while detached.
///
/// Never returns `Err` and never blocks. One slot is consumed per call because the
/// ring's slot IS one fan-in render period by construction: no allocation, no
/// intermediate copy, no drain loop, and no catch-up resync — a bounded ring
/// cannot back up past its own depth the way an aloop capture ring can, so
/// `drain_input_excess` has nothing to do here and is not called.
pub(super) fn read_ring_and_render(input: &mut Input, period_frames: usize) -> usize {
    // Take ownership of the state machine so the reattach path can mutate `input`
    // (observability, resampler) without a double borrow. Restored before return.
    //
    // `None` is unreachable (the mixer's `input.ring.is_some()` arm dispatches
    // here) and is handled as SILENCE rather than a panic: a panic would take the
    // whole summed music path down to enforce an invariant whose violation costs
    // one lane.
    let Some(mut ring) = input.ring.take() else {
        super::silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
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
            // valid mapping of a file nothing writes any more, which no counter
            // here distinguishes from an idle renderer.
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
            // A consume requires exactly one slot's worth of samples. The lane's
            // period buffer is built at `period_frames * CHANNELS` and the
            // geometry's slot is `period_frames`, so these agree by construction;
            // a mismatch is a construction bug, answered with silence.
            let wide = !input.read_buf_wide.is_empty();
            let held = if wide {
                input.read_buf_wide.len()
            } else {
                input.read_buf.len()
            };
            if held < want {
                super::silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
                0
            } else {
                // Both entry points are typed views of the same byte copy in the
                // ring core; the buffer the lane allocated at construction picks
                // which.
                let read = if wide {
                    reader.try_consume_slot_wide(&mut input.read_buf_wide[..want])
                } else {
                    reader.try_consume_slot(&mut input.read_buf[..want])
                };
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
                    // The ring zero-filled the buffer. NOT a fault and NOT a
                    // reattach trigger — see the module docs.
                    SlotRead::Empty => 0,
                }
            }
        }
        RingCapture::Detached { .. } => {
            super::silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
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
        // Re-latch on the very next period: this is a live file waiting to be
        // opened, not an absent one to back off from.
        ring = RingCapture::Detached {
            periods_until_retry: 0,
        };
    }
    input.ring = Some(ring);
    real_frames
}

/// While `Detached`: adopt a finished attach if this lane's attacher thread has
/// one ready, else count the period-based retry latch down and QUEUE the next
/// attempt when it reaches 0. No wall clock — one decrement per render period —
/// and no blocking: the attach itself runs on `fanin-ring-attacher` (#2538), so a
/// detached lane costs this loop one non-blocking `try_recv` per period.
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
    // A `None` attacher (spawn failed) stays Detached (silence) with the latch
    // re-armed rather than attaching inline — blocking the render loop is the
    // defect. `request` is also a no-op while one is already in flight, so a slow
    // attach can never be re-queued into a backlog. The geometry handed over is
    // the SAME tuple the initial attach used; a rebuilt-from-parts one would be
    // rejected by the header comparison.
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
            // Record the CURRENT reason (it can change — an `unavailable` ring
            // that a renderer then creates at a sheared geometry becomes
            // `geometry`), but do not log per retry: only TRANSITIONS log.
            obs.detach_reason.store(reason as u64, Ordering::Relaxed);
            RingCapture::Detached {
                periods_until_retry: RING_REATTACH_RETRY_PERIODS,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    //! The renderer-ingress lane's PRESENCE model (absent ring, writer death,
    //! geometry shear, self-heal), exercised against a real `jasper_ring` writer
    //! on a real SHM file. Nothing here needs ALSA, a renderer, or a Pi.

    use super::*;
    use jasper_ring::{RingWriter, TestRingWriter, SAMPLE_FORMAT_S16LE};

    /// A lane geometry small enough to keep the fixtures fast and legible, and
    /// still a legal ring (`n_slots` inside 2..=16, one slot per period).
    const TEST_PERIOD: u32 = 64;
    const TEST_SLOTS: u32 = 4;

    fn geometry_at(sample_format: u32) -> Geometry {
        Geometry {
            rate: 48_000,
            channels: CHANNELS,
            sample_format,
            period_frames: TEST_PERIOD,
            n_slots: TEST_SLOTS,
        }
    }

    fn test_geometry() -> Geometry {
        geometry_at(SAMPLE_FORMAT_S16LE)
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
    /// not have to mutate process env.
    fn ring_lane(path: &str) -> Input {
        ring_lane_at(path, test_geometry())
    }

    fn ring_lane_at(path: &str, geometry: Geometry) -> Input {
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
        // A REAL attacher thread, like production: stubbing the worker would test
        // a lane shape no box ever runs.
        let ring_attacher =
            Some(RingAttacher::spawn("spotify", Arc::clone(&obs.attach_pending)).unwrap());
        ring_lane_input(
            "spotify",
            path.to_string(),
            geometry,
            ring,
            ring_attacher,
            None,
            obs,
        )
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
    /// clock passing between attempts. Bounded so a broken worker fails the test
    /// instead of hanging it.
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
    /// The attach happens on the lane's `fanin-ring-attacher` thread and the
    /// render side adopts the result on a LATER period, so a loop that runs
    /// periods back to back at test speed can outrun the worker and conclude the
    /// lane never healed. Production cannot: one period is 5.33 ms of wall clock.
    /// This yields, but only while an attach is actually queued.
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
    /// spin: at most one attach attempt per retry window. A lane that retried
    /// every period would `open()` a file ~187 times a second, forever, on any
    /// box whose renderer is switched off at `/sources/`.
    #[test]
    fn an_unattachable_ring_renders_silence_and_retries_at_a_bounded_rate() {
        // An unattachable path that is unattachable for EVERY privilege level:
        // the parent is a regular FILE, so `ensure_parent_dir`'s `mkdir` fails
        // EEXIST — measured, `ErrorKind::AlreadyExists` / errno 17 — and root
        // gets the same. A path under a MISSING directory is not deterministic:
        // `attach_or_create` calls `ensure_parent_dir` first, so the failure is
        // whatever creating the directory returns — ENOENT where the parent is
        // writable, EACCES on CI's non-root runner, SUCCESS as root.
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
        // EEXIST is neither "absent" nor "geometry", so it classifies `refused` —
        // the token whose remedy points at the environment (permissions /
        // directory shape). The full reason MAPPING is pinned by
        // `attach_failures_classify_onto_the_remedy_they_need`.
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

    /// A WIDE lane carries a renderer's low bits all the way to the mix: on a
    /// wide box the slot is S32 and the period lands in `read_buf_wide`, the
    /// spine-scale buffer the mixer sums without a shift. The payload is a
    /// 24-bit-in-S32 pattern whose low byte is what a narrow lane's truncation
    /// used to discard (#3460); publishing it as explicit little-endian bytes
    /// pins the wire layout too, not just the reader's own cast.
    #[test]
    fn a_wide_lane_carries_a_24_bit_sample_to_the_mix_bit_exact() {
        let path = ring_path("wide");
        cleanup(&path);
        let geometry = geometry_at(SAMPLE_FORMAT_S32LE);
        let mut input = ring_lane_at(&path, geometry);
        assert!(
            !is_detached(&input),
            "the reader creates the ring when absent"
        );

        let samples = (TEST_PERIOD as usize) * (CHANNELS as usize);
        // The high nibble varies per sample so a stride bug cannot pass.
        let payload: Vec<i32> = (0..samples)
            .map(|i| ((i as i32) << 16) | 0x0000_5600)
            .collect();
        let mut bytes = Vec::with_capacity(samples * 4);
        for s in &payload {
            bytes.extend_from_slice(&s.to_le_bytes());
        }
        let mut writer = RingWriter::create_or_attach(&path, geometry).unwrap();
        assert_eq!(writer.publish_bytes(&bytes), PublishOutcome::Published);

        let frames = read_ring_and_render(&mut input, TEST_PERIOD as usize);
        assert_eq!(
            frames, TEST_PERIOD as usize,
            "a filled slot is a full period"
        );
        assert_eq!(
            input.read_buf_wide, payload,
            "a wide lane's period must reach the sum bit for bit, low byte included",
        );
        assert!(
            input.read_buf.iter().all(|&s| s == 0),
            "the narrow buffer stays silent on a wide lane — one buffer holds the \
             period and the mixer picks it by which one is allocated",
        );

        input.read_buf_wide.fill(0x1234_5678);
        let frames = read_ring_and_render(&mut input, TEST_PERIOD as usize);
        assert_eq!(frames, 0, "an empty ring contributes no real frames");
        assert!(
            input.read_buf_wide.iter().all(|&s| s == 0),
            "an empty ring must zero-fill the lane's own period buffer",
        );
        cleanup(&path);
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
    /// `writer_alive` false and `empty_reads` climbing. The lane does NOT detach,
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
    /// conf.d-vs-fan-in shear; misreading it as a transient would leave an
    /// operator retrying forever.
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
        // spawn. A wholesale replacement would detach the gauge from the worker
        // that publishes it and leave this test reading a value nothing writes.
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
    /// allocator hook — the strongest hardware-free statement available.
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
    /// silence forever. An arm/disarm (which clears a stale ring) or a geometry
    /// change produces this on a running box.
    #[test]
    fn a_ring_replaced_underneath_the_reader_is_relatched_not_read_forever() {
        let path = ring_path("orphan");
        cleanup(&path);
        let mut input = ring_lane(&path);
        let samples = (TEST_PERIOD as usize) * (CHANNELS as usize);

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

        // The probe runs on the slow cadence, so this needs the full window plus
        // the re-latch period.
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

        // Publish AFTER the re-latch, not before: a fresh attach resyncs
        // `read_seq = write_seq` (stale slots are worthless to a pacer), so a
        // slot published before the re-latch is deliberately dropped by it.
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
    /// its own token; a collapsed spelling would send an operator to the wrong
    /// remedy.
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
    /// The retry window keeps expiring while an attach runs, and without the
    /// latch each expiry would queue another. The worker holds a `.open.lock`
    /// while it works, so a queue of them would serialize into an ever-growing
    /// tail of bounded waits, newest request last.
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
    /// releases the latch instead of pinning it in flight forever — a lane that
    /// latched `in_flight` on a disconnected channel would stop retrying for the
    /// life of the process, turning a recoverable ring into permanent silence.
    #[test]
    fn a_dead_attacher_thread_releases_the_latch_rather_than_pinning_it() {
        let gauge = Arc::new(AtomicBool::new(false));
        let mut attacher = RingAttacher::spawn("spotify", Arc::clone(&gauge)).unwrap();
        assert!(
            attacher.poll().is_none(),
            "polling with nothing queued must not block or invent a result"
        );

        // A worker that died with a request outstanding: latch in flight, then
        // swap in a receiver whose sender is already gone (`channel().1` drops
        // the `Sender` on the spot), which is what `try_recv` sees after the real
        // thread exits.
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
    /// never falls back to attaching inline — an inline fallback would restore a
    /// 500 ms-bounded `flock` inside a 5.33 ms render period on precisely the box
    /// where something is already wrong.
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
