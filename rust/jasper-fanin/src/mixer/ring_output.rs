// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Ring A's publish side: the slot publish, the pacing floor under it, and the
//! ring-stall edge. Nothing here holds an ALSA handle.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::SyncSender;
use std::sync::Arc;

use jasper_ring::{PublishOutcome, RingWriter};
use log::warn;

use super::dsp::BYTES_PER_SAMPLE;
use super::CHANNELS;
use crate::config::RING_SLOT_FRAMES;
use crate::log_writer::{send_drop_counted, FaninLogEvent};

/// How far SHORT of one nominal period the pacer aims, in percent — i.e. how
/// far fan-in may RUN AHEAD of real time while nothing downstream blocks.
///
/// Two things bound this. It must be well clear of any crystal error (real pairs
/// sit within a few hundred ppm), because the pacer must never govern the rate
/// below the DAC's: the DAC clock reaches this loop ONLY as ring back-pressure
/// (CamillaDSP's rate adjuster is off for a ring sink — ADR-0218 — and
/// jasper-outputd owns no resampler), so a pacer slower than the DAC would drain
/// the ring until CamillaDSP short-reads. And it must refill the ring quickly
/// after a drain (cold start, a CamillaDSP reattach): the fill rate is
/// `h/(1-h)` of nominal, so 25% refills the deepest supported ring
/// ([`crate::config::RING_SLOTS_MAX`] slots, 42.7 ms) in ~128 ms and the
/// 4-slot default in ~32 ms, against ~3 s and ~1 s at 1%. The cost of running
/// ahead is bounded the same way: a free-run tops out at 1.33x real time.
const PACE_HEADROOM_PERCENT: u64 = 25;

/// The shortest sleep the pacer will take on a CLOCKLESS period (see
/// [`PeriodPacer`]).
///
/// `RLIMIT_RTTIME` counts RT CPU BETWEEN blocking calls, not wall time, so a
/// clockless period that overran its deadline (computing a zero sleep) would
/// still leave the loop un-blocked. This floor keeps every clockless period
/// ending in a real `nanosleep` however long the work took.
const PACE_MIN_SLEEP_NS: u64 = 100_000;

/// Absolute-deadline floor under the work loop's period.
///
/// WHY the loop needs a floor at all: jasper-fanin runs SCHED_FIFO under
/// `LimitRTTIME=200000` with soft == hard (deploy/systemd/jasper-fanin.service),
/// so once the loop burns 200 ms of RT CPU without a blocking syscall the kernel
/// raises SIGXCPU and, the limit being hard, SIGKILL with it — the journal shows
/// `status=9/KILL`. Every input is opened NON-BLOCKING, so the loop's only other
/// candidates for a blocking call are the ring publish — which blocks only while
/// the ring is FULL and a live reader drains it — and the drop path. A
/// downstream reader that FREE-RUNS (CamillaDSP still up and reading after
/// jasper-outputd stops) drains the ring faster than real time forever: it never
/// fills, no slot is ever dropped, nothing blocks, and the daemon is killed
/// within a second. An ABSENT reader is safe (it drops); a live-but-unpaced one
/// is fatal.
pub(super) struct PeriodPacer {
    nominal_ns: u64,
    /// The targeted period in nanoseconds — one nominal period
    /// (`period_frames / sample_rate`) less [`PACE_HEADROOM_PERCENT`],
    /// precomputed so the hot loop never divides.
    target_ns: u64,
    /// End of the period in flight; `None` until the first period anchors it.
    deadline_ns: Option<u64>,
}

impl PeriodPacer {
    pub(super) fn new(period_ns: u64) -> Self {
        Self {
            nominal_ns: period_ns,
            target_ns: period_ns * (100 - PACE_HEADROOM_PERCENT) / 100,
            deadline_ns: None,
        }
    }

    fn set_nominal(&mut self, nominal: bool) {
        // Snapcast consumes at wall-clock rate. DAC refill headroom would fill
        // its FIFO, then stall USB capture whenever a whole pipe page drains.
        let target = if nominal {
            self.nominal_ns
        } else {
            self.nominal_ns * (100 - PACE_HEADROOM_PERCENT) / 100
        };
        if target != self.target_ns {
            self.target_ns = target;
            self.deadline_ns = None;
        }
    }

    /// Close the period ending at `now_ns` (`CLOCK_MONOTONIC`), open the next,
    /// and return the nanoseconds to sleep. Zero when the period already spent
    /// its own wall time downstream, so the pacer never double-paces a period the
    /// ring publish already paced.
    ///
    /// Deadlines are ABSOLUTE (`deadline += target`) so per-period scheduling
    /// jitter cannot accumulate into a rate error. A period that OVERRAN its
    /// deadline re-anchors on `now` instead of owing the difference: the
    /// back-to-back catch-up periods that a carried backlog would produce are
    /// themselves unpaced, which is the burst `LimitRTTIME` kills.
    fn pace(&mut self, now_ns: u64) -> u64 {
        match self.deadline_ns {
            Some(deadline_ns) if now_ns < deadline_ns => {
                self.deadline_ns = Some(deadline_ns + self.target_ns);
                deadline_ns - now_ns
            }
            _ => {
                self.deadline_ns = Some(now_ns + self.target_ns);
                0
            }
        }
    }
}

/// The Ring A output — the daemon's ONLY final-output transport (ADR-0100).
///
/// The blocking ring publish (bounded, on a full-ring-with-live-reader) is the
/// timing owner of the fan-in work loop whenever a paced reader is draining;
/// [`PeriodPacer`] is the floor under the periods where nothing downstream
/// blocks. NO ALSA playback PCM is opened: the ring is the whole output, and
/// CamillaDSP reads it via a capture-direction ioplug.
pub(super) struct RingOutput {
    pub(super) writer: RingWriter,
    pub(super) counters: RingCounters,
    /// Wall-time floor under one period.
    pub(super) pace: PeriodPacer,
    /// Edge-detection state machine for the ring stall event (issue #1524).
    pub(super) stall: RingStallTracker,
    /// Off-thread sink for a [`FaninLogEvent`]: `write_ring_period` runs on the
    /// SCHED_FIFO mixer thread and must never format or log one itself (issue
    /// #4787). `run_ring_stall_log_writer` drains it — the TTS mixer holds a
    /// clone of the same sender for its own `AssistantLoudness`/`TtsFlush`
    /// events, so every off-thread log line in this daemon goes through the
    /// one writer thread.
    pub(super) stall_log: SyncSender<FaninLogEvent>,
}

/// Turn a `RingWriter::create_or_attach` failure into the error `Mixer::new`
/// returns, logging the matching journal event.
///
/// The WHOLE decision lives here — classify, log, and (only for the config
/// class) attach the [`crate::ConfigClassError`] marker — so the park behaviour
/// is reachable from a hardware-free test: `Mixer::new` cannot be constructed
/// without ALSA, so a decision left inline in its `map_err` closure would be
/// untestable.
///
/// The two events are distinct on purpose. `config_error` means "the geometry
/// you declared cannot work"; `open_error` means "the ring could not be opened
/// right now". Logging an EACCES as `config_error` sends an operator to audit a
/// geometry that was never wrong.
pub(crate) fn ring_open_error(path: &str, error: std::io::Error) -> anyhow::Error {
    if jasper_ring::ring_open_error_is_config_class(&error) {
        warn!(
            "event=fanin.ring.config_error path={} detail={}",
            path, error,
        );
        anyhow::Error::new(error).context(crate::ConfigClassError)
    } else {
        warn!(
            "event=fanin.ring.open_error path={} kind={:?} detail={} — \
             transient class, the unit restarts (not a config park)",
            path,
            error.kind(),
            error,
        );
        anyhow::Error::new(error)
    }
}

/// The live SPSC ring counters the mixer step updates each period (from the
/// writer's [`jasper_ring::WriterMetrics`]). Cloned into
/// [`RingObservability`] for STATUS so the endpoint reads the same atomics the
/// work loop writes. Distinct from `WriterMetrics`, which is a value snapshot.
#[derive(Clone)]
pub(super) struct RingCounters {
    pub(super) nominal_clock: Arc<AtomicBool>,
    pub(super) published: Arc<AtomicU64>,
    pub(super) full_waits: Arc<AtomicU64>,
    /// Live-but-STUCK reader drops (issue #1524) — the bounded-wait give-ups
    /// (`DroppedStuck`) plus the sticky demotions (`DroppedStuckDemoted`). Kept
    /// separate from `drop_no_reader` so a heartbeat-live wedge is
    /// distinguishable from a benign no-reader reload.
    pub(super) stuck_reader_drops: Arc<AtomicU64>,
    /// Dead/absent-reader free-run drops (`DroppedNoReader`) — the normal
    /// CamillaDSP-reload transient.
    pub(super) drop_no_reader: Arc<AtomicU64>,
    pub(super) occupancy: Arc<AtomicU64>,
    /// A ring stall episode (full + reader heartbeat-live + `read_seq` frozen
    /// past the grace, OR a >1 s no-reader hold) is CURRENTLY in progress.
    pub(super) stall_active: Arc<AtomicBool>,
    /// Duration in ms of the current (if `stall_active`) or most-recent stall
    /// episode; 0 if none has ever occurred.
    pub(super) last_stall_ms: Arc<AtomicU64>,
    /// Periods whose ONLY pacer was [`PeriodPacer`] — neither a blocking publish
    /// nor a dropped slot spent the period's wall time.
    ///
    /// It climbs in bursts whenever the ring has room (a cold start, a CamillaDSP
    /// reattach) and stops once back-pressure resumes. Climbing at the PERIOD
    /// RATE while `full_waits` stays flat is the diagnostic: it means the pacer,
    /// not the DAC, is the metronome — the free-running-reader shape
    /// [`PeriodPacer`] exists for.
    pub(super) clockless_paces: Arc<AtomicU64>,
    /// Ring-stall log events (see [`RingOutput::stall_log`]) dropped because
    /// `fanin-ring-log` was not keeping up or had exited. Every `try_send`
    /// failure counts (ADR-0254): a stall going undetected because its log
    /// line was silently lost is worse than a gauge that occasionally ticks.
    pub(super) stall_log_dropped: Arc<AtomicU64>,
}

impl RingCounters {
    pub(super) fn new() -> Self {
        Self {
            nominal_clock: Arc::new(AtomicBool::new(false)),
            published: Arc::new(AtomicU64::new(0)),
            full_waits: Arc::new(AtomicU64::new(0)),
            stuck_reader_drops: Arc::new(AtomicU64::new(0)),
            drop_no_reader: Arc::new(AtomicU64::new(0)),
            occupancy: Arc::new(AtomicU64::new(0)),
            stall_active: Arc::new(AtomicBool::new(false)),
            last_stall_ms: Arc::new(AtomicU64::new(0)),
            clockless_paces: Arc::new(AtomicU64::new(0)),
            stall_log_dropped: Arc::new(AtomicU64::new(0)),
        }
    }
}

/// The shared ring counters (cloned Arcs) plus the observed wire geometry, for
/// the STATUS endpoint's `ring` block.
///
/// `channels` is a plain value, not an atomic: it comes off the header the
/// writer attached against, which is immutable for the ring file's life, so
/// there is nothing for a later period to update.
#[derive(Clone)]
pub struct RingObservability {
    pub nominal_clock: Arc<AtomicBool>,
    pub path: String,
    pub slots: u32,
    /// The OBSERVED channel count from the attached header.
    pub channels: u32,
    pub occupancy: Arc<AtomicU64>,
    pub published: Arc<AtomicU64>,
    pub full_waits: Arc<AtomicU64>,
    pub stuck_reader_drops: Arc<AtomicU64>,
    pub drop_no_reader: Arc<AtomicU64>,
    pub stall_active: Arc<AtomicBool>,
    pub last_stall_ms: Arc<AtomicU64>,
    /// See [`RingCounters::clockless_paces`].
    pub clockless_paces: Arc<AtomicU64>,
    /// See [`RingCounters::stall_log_dropped`].
    pub stall_log_dropped: Arc<AtomicU64>,
}

/// The fan-in ring-stall EVENT threshold (issue #1524): how long the
/// fan-in→CamillaDSP ring must stay full-and-not-draining before the mixer emits
/// ONE edge-triggered `event=fanin.ring.stall_detected`. Deliberately EQUAL to
/// the writer's [`jasper_ring::STUCK_READER_GRACE_NS`] (1 s) so the writer's
/// sticky-stuck demotion and this observability edge fire together — above a
/// normal reload/reattach turn, 5× below the 5 s fan-in progress watchdog, and
/// 12× below the ~12 s downstream correction-lane aplay timeout.
const RING_STALL_EVENT_NS: u64 = jasper_ring::STUCK_READER_GRACE_NS;

/// Minimum gap between one stall episode CLEARING and arming the next
/// `stall_detected` — the re-arm rate limit that keeps a flapping reader from
/// spamming the journal. It bounds only the frequency of NEW episodes: a single
/// sustained stall is one detected + one cleared regardless. The first-ever
/// episode always arms.
const RING_STALL_REARM_MIN_GAP_NS: u64 = 10_000_000_000; // 10 s

/// Bounded escalation threshold: if a logged stall episode persists this long
/// the mixer emits ONE `event=fanin.ring.stall_unrecovered` (issue #1524). It is
/// pure observability — fan-in owns the ring, not the CamillaDSP lifecycle, so it
/// NEVER restarts the DSP; the writer's demotion has already returned fan-in to
/// real time regardless.
const RING_STALL_UNRECOVERED_NS: u64 = 10_000_000_000; // 10 s

/// Why the ring is not draining, for the stall event's `reason=` field.
///
/// `pub(crate)`: reachable through [`RingStallEvent`], itself wrapped in the
/// `pub(crate)` [`FaninLogEvent::RingStall`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum StallReason {
    /// Reader heartbeat is live but `read_seq` is frozen (the #1524 wedge —
    /// CamillaDSP polling in Prepared without calling `readi`).
    StuckReader,
    /// Reader is dead/absent and the free-run has been held past the threshold
    /// (e.g. CamillaDSP down for > 1 s, not a normal reload transient).
    NoReader,
}

impl StallReason {
    fn as_str(self) -> &'static str {
        match self {
            StallReason::StuckReader => "stuck_reader",
            StallReason::NoReader => "no_reader",
        }
    }
}

/// One period's stall signals, fed to [`RingStallTracker::observe`]. `now_ns` and
/// `stall_ns` are injected (not read from a clock inside the tracker) so the edge
/// state machine is a PURE, deterministically-testable function of its inputs.
#[derive(Debug, Clone, Copy)]
struct RingStallInput {
    /// Monotonic ns (production: `jasper_ring::monotonic_ns()`), for the re-arm
    /// rate limit only.
    now_ns: u64,
    /// `writer.ns_since_read_seq_advance()` — ns since the READER last advanced
    /// `read_seq`. The authoritative stall duration.
    stall_ns: u64,
    /// This period had at least one full-ring drop (the ring was not draining).
    dropped_this_period: bool,
    /// Reader heartbeat looked live this period → `stuck_reader`, else `no_reader`.
    reader_live: bool,
    /// Cumulative `full_waits` (event field).
    full_waits: u64,
    /// Ring occupancy (event field).
    occupancy: u64,
    /// Reader pid (event field).
    reader_pid: u64,
    /// Reader heartbeat age in ms (`u64::MAX` = never) (event field).
    reader_heartbeat_age_ms: u64,
}

/// An edge-triggered ring-stall event the mixer logs. Emitted at most once per
/// transition — never per period.
///
/// `pub(crate)`: wrapped in [`FaninLogEvent::RingStall`], which is
/// `pub(crate)` itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RingStallEvent {
    Detected {
        reason: StallReason,
        duration_ms: u64,
        dropped_periods: u64,
        occupancy: u64,
        reader_pid: u64,
        reader_heartbeat_age_ms: u64,
        full_waits: u64,
    },
    /// A logged episode has persisted past [`RING_STALL_UNRECOVERED_NS`]. Pure
    /// observability — emitted once per episode; fan-in does NOT restart the DSP.
    Unrecovered {
        reason: StallReason,
        duration_ms: u64,
        dropped_periods: u64,
    },
    Cleared {
        reason: StallReason,
        duration_ms: u64,
        dropped_periods: u64,
    },
}

/// Edge-detection state machine for the fan-in ring stall event (issue #1524).
///
/// PURE: [`Self::observe`] takes explicit `now_ns` / `stall_ns` so it is a
/// deterministic function of its inputs (no internal clock) and can be unit
/// tested without any real waiting. `write_ring_period` owns the wiring.
///
/// A "drop run" is a contiguous sequence of dropped periods. Its duration is
/// the writer's `stall_ns` — time since the READER last advanced — not a count
/// of periods. Each of `Detected` / `Unrecovered` / `Cleared` fires at most once
/// per run; steady state and normal sub-threshold reloads emit NOTHING.
#[derive(Debug, Clone)]
pub(super) struct RingStallTracker {
    run_active: bool,
    run_reason: StallReason,
    run_dropped_periods: u64,
    logged: bool,
    unrecovered_logged: bool,
    last_stall_ms: u64,
    /// `now_ns` of the last emitted `Detected` — the re-arm rate limit anchor.
    last_detected_ns: u64,
    ever_detected: bool,
}

impl RingStallTracker {
    pub(super) fn new() -> Self {
        Self {
            run_active: false,
            run_reason: StallReason::StuckReader,
            run_dropped_periods: 0,
            logged: false,
            unrecovered_logged: false,
            last_stall_ms: 0,
            last_detected_ns: 0,
            ever_detected: false,
        }
    }

    /// True while a logged stall episode is in progress (drives
    /// `/state.shm_ring.stall_active`).
    fn stall_active(&self) -> bool {
        self.logged
    }

    /// Duration in ms of the current (if active) or most-recent logged episode.
    fn last_stall_ms(&self) -> u64 {
        self.last_stall_ms
    }

    fn observe(&mut self, input: RingStallInput) -> Option<RingStallEvent> {
        if !input.dropped_this_period {
            // Only emit Cleared if a Detected was emitted for this run.
            if self.run_active {
                self.run_active = false;
            }
            let dropped = self.run_dropped_periods;
            self.run_dropped_periods = 0;
            self.unrecovered_logged = false;
            if self.logged {
                self.logged = false;
                let reason = self.run_reason;
                return Some(RingStallEvent::Cleared {
                    reason,
                    duration_ms: self.last_stall_ms,
                    dropped_periods: dropped,
                });
            }
            return None;
        }

        let reason = if input.reader_live {
            StallReason::StuckReader
        } else {
            StallReason::NoReader
        };
        if !self.run_active {
            self.run_active = true;
            self.run_dropped_periods = 0;
        }
        self.run_reason = reason;
        self.run_dropped_periods = self.run_dropped_periods.saturating_add(1);

        let stall_ms = input.stall_ns / 1_000_000;
        if input.stall_ns < RING_STALL_EVENT_NS {
            // Sub-threshold (a normal reload/reattach transient): stay silent.
            return None;
        }

        if !self.logged {
            let armed = !self.ever_detected
                || input.now_ns.saturating_sub(self.last_detected_ns)
                    >= RING_STALL_REARM_MIN_GAP_NS;
            if !armed {
                return None;
            }
            self.logged = true;
            self.ever_detected = true;
            self.last_detected_ns = input.now_ns;
            self.unrecovered_logged = false;
            self.last_stall_ms = stall_ms;
            return Some(RingStallEvent::Detected {
                reason,
                duration_ms: stall_ms,
                dropped_periods: self.run_dropped_periods,
                occupancy: input.occupancy,
                reader_pid: input.reader_pid,
                reader_heartbeat_age_ms: input.reader_heartbeat_age_ms,
                full_waits: input.full_waits,
            });
        }

        // Keep the live duration fresh for /state.
        self.last_stall_ms = stall_ms;
        if !self.unrecovered_logged && input.stall_ns >= RING_STALL_UNRECOVERED_NS {
            self.unrecovered_logged = true;
            return Some(RingStallEvent::Unrecovered {
                reason,
                duration_ms: stall_ms,
                dropped_periods: self.run_dropped_periods,
            });
        }
        None
    }
}

/// Format a ring-stall event as one structured `event=` log line (issue #1524).
pub(crate) fn format_ring_stall_event(event: &RingStallEvent) -> String {
    match event {
        RingStallEvent::Detected {
            reason,
            duration_ms,
            dropped_periods,
            occupancy,
            reader_pid,
            reader_heartbeat_age_ms,
            full_waits,
        } => format!(
            "event=fanin.ring.stall_detected reason={} duration_ms={} \
             dropped_periods={} occupancy={} reader_pid={} \
             reader_heartbeat_age_ms={} full_waits={}",
            reason.as_str(),
            duration_ms,
            dropped_periods,
            occupancy,
            reader_pid,
            reader_heartbeat_age_ms,
            full_waits,
        ),
        RingStallEvent::Unrecovered {
            reason,
            duration_ms,
            dropped_periods,
        } => format!(
            "event=fanin.ring.stall_unrecovered reason={} duration_ms={} \
             dropped_periods={}",
            reason.as_str(),
            duration_ms,
            dropped_periods,
        ),
        RingStallEvent::Cleared {
            reason,
            duration_ms,
            dropped_periods,
        } => format!(
            "event=fanin.ring.stall_cleared reason={} duration_ms={} \
             dropped_periods={}",
            reason.as_str(),
            duration_ms,
            dropped_periods,
        ),
    }
}

/// Publish one mixer period into the SPSC SHM ring as `period_frames / 128`
/// slots. Returns the number of frames that actually ENTERED the ring this
/// period (published slots × `RING_SLOT_FRAMES`) so the caller counts only real
/// throughput — a fully-dropped period returns 0.
///
/// **Pacing (the Ring A contract).** Each `RingWriter::publish` BLOCKS (bounded:
/// 32 ticks × the clamped `min(period/4, 2 ms)` sleep — with the pinned 128-frame
/// slot that tick is ~0.667 ms, so the cap is ~21 ms per full slot) while the
/// ring is full AND a live reader (CamillaDSP) is draining — that block is the
/// loop's pacer, transitively DAC-paced through Ring B. Every OTHER period —
/// reader absent (free-run drops), reader stale, or a reader draining faster than
/// real time so the ring never fills — is paced by [`PeriodPacer`] instead, whose
/// deadline sleep is what keeps the loop off `LimitRTTIME`. The bounded publish
/// wait plus at most one period sleep keeps `step()` well under the 5 s watchdog
/// threshold; the writer's heartbeat is bumped inside each publish.
///
/// **Ring stall self-recovery + observability (issue #1524).** A reader that
/// stays heartbeat-live but stops advancing `read_seq` (CamillaDSP wedged in
/// Prepared, still polling) would otherwise pin every publish in the bounded
/// wait, running fan-in at ~1/9 real time and back-pressuring the input lanes.
/// The writer DEMOTES such a reader past its grace and free-runs
/// (`DroppedStuckDemoted`), which the pacer below turns back into real-time
/// pacing. This function additionally feeds the writer's per-period stall signals
/// to a [`RingStallTracker`] and logs ONE edge-triggered
/// `event=fanin.ring.stall_detected` / `stall_cleared`.
///
/// **Which payload the ring gets.** The ring publishes `payload`, the S32LE
/// bytes `fill_ring_payload` built from this period's sum. The ring publish is
/// the whole of this function's output.
pub(super) fn write_ring_period(ring: &mut RingOutput, payload: &[u8], period_frames: u32) -> u32 {
    let slots_per_step = period_frames / RING_SLOT_FRAMES;
    let samples_per_slot = (RING_SLOT_FRAMES as usize) * (CHANNELS as usize);
    let mut dropped_this_period = false;
    let mut published_slots: u32 = 0;
    for slot in 0..slots_per_step as usize {
        let byte_start = slot * samples_per_slot * BYTES_PER_SAMPLE;
        let slot_bytes = samples_per_slot * BYTES_PER_SAMPLE;
        let outcome = ring
            .writer
            .publish_bytes(&payload[byte_start..byte_start + slot_bytes]);
        match outcome {
            PublishOutcome::Published => {
                published_slots += 1;
            }
            // A demoted publish (issue #1524) is a drop like the others, so
            // the pacer below returns fan-in to real time.
            PublishOutcome::DroppedNoReader
            | PublishOutcome::DroppedStuck
            | PublishOutcome::DroppedStuckDemoted => {
                dropped_this_period = true;
            }
        }
    }

    let m = ring.writer.metrics();
    // Read BEFORE the store below overwrites it: the counter still holds last
    // period's value, so this is the delta. `full_waits` ticks once per publish
    // that entered the bounded back-pressure wait, and that wait is a
    // `nanosleep` — so a nonzero delta means this period already blocked.
    let publish_blocked = m.full_waits > ring.counters.full_waits.load(Ordering::Relaxed);
    ring.counters
        .published
        .store(m.published_slots, Ordering::Relaxed);
    ring.counters
        .full_waits
        .store(m.full_waits, Ordering::Relaxed);
    ring.counters
        .stuck_reader_drops
        .store(m.stuck_reader_drops, Ordering::Relaxed);
    ring.counters
        .drop_no_reader
        .store(m.drop_no_reader, Ordering::Relaxed);
    ring.counters
        .occupancy
        .store(m.occupancy, Ordering::Relaxed);

    // The demotion self-recovery lives in the writer; this is pure
    // observability.
    let liveness = ring.writer.reader_liveness();
    let stall_ns = ring.writer.ns_since_read_seq_advance();
    let now_ns = jasper_ring::monotonic_ns();
    if let Some(event) = ring.stall.observe(RingStallInput {
        now_ns,
        stall_ns,
        dropped_this_period,
        reader_live: liveness.live,
        full_waits: m.full_waits,
        occupancy: m.occupancy,
        reader_pid: liveness.pid,
        reader_heartbeat_age_ms: liveness.heartbeat_age_ms,
    }) {
        // No format!/log here: `event` is `Copy` (no allocation), and
        // `run_ring_stall_log_writer` does the formatting and the journald
        // write off this SCHED_FIFO thread. `try_send` never blocks; a full
        // or disconnected channel (the writer thread wedged or exited) drops
        // the log line, not a period — `stall_log_dropped` counts it (ADR-0254).
        let stall_log_dropped = &ring.counters.stall_log_dropped;
        send_drop_counted(&ring.stall_log, FaninLogEvent::RingStall(event), || {
            stall_log_dropped.fetch_add(1, Ordering::Relaxed);
        });
    }
    ring.counters
        .stall_active
        .store(ring.stall.stall_active(), Ordering::Relaxed);
    ring.counters
        .last_stall_ms
        .store(ring.stall.last_stall_ms(), Ordering::Relaxed);

    // Wall-time floor under the period. A CLOCKLESS period — neither the publish
    // nor a drop spent its wall time — is the one whose only blocking syscall is
    // this sleep, so it is floored at `PACE_MIN_SLEEP_NS` even when the deadline
    // has already passed. Every other period is left exactly as the pacer found
    // it: a zero sleep after back-pressure, so the DAC keeps owning the rate.
    ring.pace
        .set_nominal(ring.counters.nominal_clock.load(Ordering::Relaxed));
    let mut sleep_ns = ring.pace.pace(now_ns);
    let clockless = !publish_blocked && !dropped_this_period;
    if clockless {
        ring.counters
            .clockless_paces
            .fetch_add(1, Ordering::Relaxed);
        sleep_ns = sleep_ns.max(PACE_MIN_SLEEP_NS);
    }
    if sleep_ns > 0 {
        let ts = libc::timespec {
            tv_sec: (sleep_ns / 1_000_000_000) as _,
            tv_nsec: (sleep_ns % 1_000_000_000) as _,
        };
        // SAFETY: a valid timespec pointer; NULL remainder is fine (a signal-
        // interrupted sleep just shortens this one period — the absolute
        // deadline puts the next one back on phase).
        unsafe {
            libc::nanosleep(&ts, std::ptr::null_mut());
        }
    }

    published_slots * RING_SLOT_FRAMES
}

#[cfg(test)]
mod tests {
    use super::*;

    use jasper_ring::{RingReader, SlotRead};

    use crate::mixer::dsp::{apply_gain_to_sum, mix_into};
    use crate::mixer::tests::{
        cleanup_ring, payload_of, ring_geometry, samples_of, tmp_ring_output,
    };

    // Ring A output path. These construct a real SPSC ring (via jasper_ring)
    // under the OS temp dir and drive `write_ring_period` directly, so they run
    // on any host that can build the crate (CI Linux). `RingOutput` holds no
    // ALSA handle at all, so the ring publish + reader roundtrip is the whole
    // contract — there is nothing else for a test to stub out.

    /// Q2 (TTS/duck ride-along): the ring output receives the FINAL mixed period
    /// — post-duck AND post-TTS — verbatim. `step()` mixes TTS and applies the
    /// duck into sum_buf BEFORE filling the payload, and `write_ring_period`
    /// publishes exactly that payload, so whatever the mix produced is what the
    /// ring reader sees. This test stands in a post-TTS-mixed period and asserts
    /// the reader reads back those exact bytes.
    #[test]
    fn ring_output_carries_post_duck_post_tts_period() {
        let period_frames = 256u32; // 2 slots of 128 frames
        let (mut ring, path) = tmp_ring_output(8, "tts_ridealong");
        let mut reader = RingReader::create_or_attach(&path, ring_geometry(8)).unwrap();
        // Prime the reader heartbeat so the writer takes the publish path.
        let slot_bytes = (RING_SLOT_FRAMES as usize) * (CHANNELS as usize) * BYTES_PER_SAMPLE;
        let mut slot_out = vec![0u8; slot_bytes];
        assert_eq!(
            reader.try_consume_slot_bytes(&mut slot_out),
            SlotRead::Empty
        );

        // Model step(): build a summed program, apply a duck, then add a TTS
        // contribution — the SAME order step() uses — and fill the payload.
        let total = (period_frames as usize) * (CHANNELS as usize);
        let lane = jasper_resampler::widen_i16_to_i32(10_000);
        let mut sum = vec![0i64; total];
        mix_into(&mut sum, &vec![lane; total]); // program lane
        apply_gain_to_sum(&mut sum, 0.5); // duck (TTS active)
        let tts = jasper_resampler::widen_i16_to_i32(4_000) as i64;
        for s in sum.iter_mut() {
            *s = s.saturating_add(tts); // stand-in for tts.mix_period
        }
        let payload = payload_of(&sum);

        let published_frames = write_ring_period(&mut ring, &payload, period_frames);
        // Two slots reached a live reader -> the full period is counted.
        assert_eq!(published_frames, period_frames);

        // The reader reads the two published slots back — byte-identical to the
        // post-duck post-TTS payload.
        let mut got: Vec<u8> = Vec::with_capacity(payload.len());
        for _ in 0..(period_frames / RING_SLOT_FRAMES) {
            assert_eq!(
                reader.try_consume_slot_bytes(&mut slot_out),
                SlotRead::Filled
            );
            got.extend_from_slice(&slot_out);
        }
        assert_eq!(got, payload, "ring must carry the final mixed period");
        // 5000 + 4000 = 9000 on the i16 grid, at the wire's own scale.
        let expected = jasper_resampler::widen_i16_to_i32(9_000);
        assert!(
            samples_of(&got).iter().all(|&s| s == expected),
            "post-duck+TTS value",
        );
        // Counters reflect two published slots (a live reader, no drops).
        assert_eq!(ring.counters.published.load(Ordering::Relaxed), 2);
        assert_eq!(ring.counters.stuck_reader_drops.load(Ordering::Relaxed), 0);
        assert_eq!(ring.counters.drop_no_reader.load(Ordering::Relaxed), 0);
        assert!(!ring.counters.stall_active.load(Ordering::Relaxed));
        cleanup_ring(&path);
    }

    /// A period's worth of mix sum spanning the values the publish has to get
    /// right: silence, both spine rails, the two-full-scale-lanes sum that
    /// legitimately exceeds the rails, its negative twin, and ordinary program
    /// levels.
    fn representative_sum(total: usize) -> Vec<i64> {
        let full = i32::MAX as i64;
        let pattern = [
            0i64,
            2 * full,
            2 * (i32::MIN as i64),
            full,
            i32::MIN as i64,
            9_000 << 16,
            -9_000 << 16,
            1,
        ];
        (0..total).map(|i| pattern[i % pattern.len()]).collect()
    }

    /// End-to-end through the real ring: a mix sum published slot by slot and
    /// read back by a real `RingReader`. This is the test the byte API's
    /// correctness rides on — it fails if the payload is sliced at the wrong
    /// stride, published with the wrong length, or computed in the wrong order.
    #[test]
    fn ring_slots_carry_the_published_period_sample_for_sample() {
        let period_frames = 256u32; // 2 slots of 128 frames
        let total = (period_frames as usize) * (CHANNELS as usize);
        let samples_per_slot = (RING_SLOT_FRAMES as usize) * (CHANNELS as usize);
        let slots = period_frames / RING_SLOT_FRAMES;
        let sum = representative_sum(total);
        let payload = payload_of(&sum);

        let (mut ring, path) = tmp_ring_output(8, "wire");
        let mut reader = RingReader::create_or_attach(&path, ring_geometry(8)).unwrap();
        let mut slot = vec![0u8; samples_per_slot * BYTES_PER_SAMPLE];
        assert_eq!(
            reader.try_consume_slot_bytes(&mut slot),
            SlotRead::Empty,
            "priming the reader heartbeat"
        );
        assert_eq!(
            write_ring_period(&mut ring, &payload, period_frames),
            period_frames
        );
        let mut read: Vec<i32> = Vec::with_capacity(total);
        for _ in 0..slots {
            assert_eq!(reader.try_consume_slot_bytes(&mut slot), SlotRead::Filled);
            read.extend_from_slice(&samples_of(&slot));
        }

        assert_eq!(read.len(), total);
        for (i, (&got, &s)) in read.iter().zip(sum.iter()).enumerate() {
            assert_eq!(
                got as i64,
                s.clamp(i32::MIN as i64, i32::MAX as i64),
                "slot sample {i}",
            );
        }
        // The over-rail sums actually appear in this period, so the saturation
        // is exercised end-to-end and not only in the pure unit test.
        assert!(
            read.contains(&i32::MAX) && read.contains(&i32::MIN),
            "the representative period must include both saturated rails"
        );
        cleanup_ring(&path);
    }

    /// The pacer is a FLOOR, not a rate governor. An instant period is slept out
    /// to its absolute deadline; a period that already spent its own wall time
    /// downstream (the blocking ring publish) sleeps zero and re-anchors, so
    /// back-pressure — the DAC clock — keeps owning the rate. Absolute deadlines
    /// mean a late wake does not push the grid out.
    #[test]
    fn period_pacer_floors_idle_periods_and_yields_to_backpressure() {
        let period_ns = 256 * 1_000_000_000 / 48_000;
        let mut pacer = PeriodPacer::new(period_ns);
        let target = pacer.target_ns;

        // The first period only anchors the deadline.
        let t0 = 1_000_000_000u64;
        assert_eq!(pacer.pace(t0), 0);

        // Instant periods are slept out to their own absolute deadlines; a period
        // that woke LATE by `jitter` still lands on the original grid rather than
        // pushing it out by the jitter.
        assert_eq!(pacer.pace(t0), target);
        let jitter = 1_000u64;
        assert_eq!(pacer.pace(t0 + target + jitter), target - jitter);

        // Back-pressure: this period spent many periods' worth of wall time
        // downstream. No sleep, and the next deadline re-anchors on now instead
        // of owing a backlog of unpaced catch-up periods.
        let blocked = t0 + 10 * target;
        assert_eq!(pacer.pace(blocked), 0);
        assert_eq!(pacer.pace(blocked), target);
    }

    #[test]
    fn nominal_clock_keeps_one_period_per_deadline_without_dac_headroom() {
        let period_ns = 256 * 1_000_000_000 / 48_000;
        let mut pacer = PeriodPacer::new(period_ns);
        pacer.set_nominal(true);
        let t0 = 1_000_000_000;
        assert_eq!(pacer.pace(t0), 0);
        for period in 0..1000 {
            assert_eq!(pacer.pace(t0 + period * period_ns), period_ns);
        }
        pacer.set_nominal(false);
        assert_eq!(
            pacer.target_ns,
            period_ns * (100 - PACE_HEADROOM_PERCENT) / 100
        );
        assert_eq!(pacer.deadline_ns, None);
    }

    /// Reader-absent: `write_ring_period` free-run-drops and paces (never
    /// hot-spins). Counters reflect the drops; occupancy stays bounded. This
    /// stands in the "CamillaDSP not yet up / reloading" turn.
    #[test]
    fn ring_output_free_runs_and_paces_without_reader() {
        let period_frames = 256u32;
        let (mut ring, path) = tmp_ring_output(2, "no_reader");
        // No reader attached: reader_pid == 0.
        let total = (period_frames as usize) * (CHANNELS as usize);
        let payload = vec![7u8; total * BYTES_PER_SAMPLE];

        // Fill the ring, then publish several more periods. Each free-run-drops
        // the oldest; the pacer sleeps each period out to its deadline. Bound
        // the wall time so that sleep can't wedge the loop.
        let start = std::time::Instant::now();
        let mut per_period_published = Vec::with_capacity(4);
        for _ in 0..4 {
            per_period_published.push(write_ring_period(&mut ring, &payload, period_frames));
        }
        let elapsed = start.elapsed();
        // Accounting (nit-2): the first period fills the empty 2-slot ring and
        // counts both slots (period_frames); once the ring is full every later
        // period free-run-drops entirely and counts 0 — the top-line
        // frames_written never over-counts a fully-dropped period.
        assert_eq!(per_period_published[0], period_frames);
        assert_eq!(
            *per_period_published.last().unwrap(),
            0,
            "a fully-dropped readerless period must count 0 frames"
        );
        // 4 periods * (2 slots each), each paced to the deadline: bounded well
        // under the 5 s watchdog threshold.
        assert!(
            elapsed < std::time::Duration::from_secs(1),
            "pacing must stay bounded, got {elapsed:?}"
        );
        // Drops accrued as NO-READER drops (dead reader); the stuck-reader
        // counter stays zero and no stall episode is logged (reason=no_reader
        // stays sub-episode here because the reader was never live). Occupancy
        // bounded at n_slots.
        assert!(ring.counters.drop_no_reader.load(Ordering::Relaxed) > 0);
        assert_eq!(ring.counters.stuck_reader_drops.load(Ordering::Relaxed), 0);
        assert!(ring.counters.occupancy.load(Ordering::Relaxed) <= 2);
        cleanup_ring(&path);
    }

    /// `write_ring_period` must hand a stall edge to `stall_log`'s channel,
    /// never format or log it inline (issue #4787) — the RT-thread half of
    /// the split `run_ring_stall_log_writer` implements. Seeds an
    /// already-`logged` episode directly (private-field access from this
    /// child module) so the `Cleared` edge fires on one successful publish,
    /// with no real stall-duration wait.
    #[test]
    fn ring_stall_cleared_event_ships_over_the_off_thread_channel() {
        let period_frames = RING_SLOT_FRAMES;
        let (mut ring, path) = tmp_ring_output(2, "stall_log_seam");
        let (tx, rx) = std::sync::mpsc::sync_channel(4);
        ring.stall_log = tx;
        ring.stall.logged = true;
        ring.stall.run_reason = StallReason::NoReader;
        ring.stall.run_dropped_periods = 5;
        ring.stall.last_stall_ms = 777;

        let total = (period_frames as usize) * (CHANNELS as usize);
        let payload = vec![0u8; total * BYTES_PER_SAMPLE];
        write_ring_period(&mut ring, &payload, period_frames);

        let event = rx.try_recv().expect(
            "write_ring_period must ship the Cleared event over stall_log, not log it inline",
        );
        assert_eq!(
            event,
            FaninLogEvent::RingStall(RingStallEvent::Cleared {
                reason: StallReason::NoReader,
                duration_ms: 777,
                dropped_periods: 5,
            })
        );
        cleanup_ring(&path);
    }

    /// A stall event that cannot reach `fanin-ring-log` (writer thread gone)
    /// must still be counted (ADR-0254) — never silently lost with no trace.
    #[test]
    fn ring_stall_log_drop_is_counted_when_the_writer_thread_is_gone() {
        let period_frames = RING_SLOT_FRAMES;
        let (mut ring, path) = tmp_ring_output(2, "stall_log_drop");
        let (tx, rx) = std::sync::mpsc::sync_channel(4);
        ring.stall_log = tx;
        drop(rx); // No writer thread draining it: every send is `Disconnected`.
        ring.stall.logged = true;
        ring.stall.run_reason = StallReason::NoReader;
        ring.stall.run_dropped_periods = 1;
        ring.stall.last_stall_ms = 42;

        let total = (period_frames as usize) * (CHANNELS as usize);
        let payload = vec![0u8; total * BYTES_PER_SAMPLE];
        write_ring_period(&mut ring, &payload, period_frames);

        assert_eq!(ring.counters.stall_log_dropped.load(Ordering::Relaxed), 1);
        cleanup_ring(&path);
    }

    // ---- Ring stall detection + self-recovery (issue #1524) ----------------

    fn stall_input(now_ns: u64, stall_ns: u64, dropped: bool, live: bool) -> RingStallInput {
        RingStallInput {
            now_ns,
            stall_ns,
            dropped_this_period: dropped,
            reader_live: live,
            full_waits: 32,
            occupancy: 16,
            reader_pid: 4321,
            reader_heartbeat_age_ms: 3,
        }
    }

    /// The stall tracker emits EXACTLY one `stall_detected reason=stuck_reader`
    /// (with the correct fields) when a full ring stops draining past the
    /// threshold, stays silent per-period while the stall persists, and emits
    /// EXACTLY one `stall_cleared` when the ring drains again.
    #[test]
    fn ring_stall_emits_event_once_and_clears() {
        let mut t = RingStallTracker::new();
        let below = RING_STALL_EVENT_NS - 1;
        // Sub-threshold buildup (a normal reload transient): silent.
        assert_eq!(t.observe(stall_input(1_000, below, true, true)), None);
        assert_eq!(t.observe(stall_input(2_000, below, true, true)), None);
        assert!(!t.stall_active());

        // Cross the threshold → exactly one Detected(stuck_reader), correct fields.
        match t.observe(stall_input(3_000, RING_STALL_EVENT_NS, true, true)) {
            Some(RingStallEvent::Detected {
                reason,
                duration_ms,
                dropped_periods,
                occupancy,
                reader_pid,
                reader_heartbeat_age_ms,
                full_waits,
            }) => {
                assert_eq!(reason, StallReason::StuckReader);
                assert_eq!(duration_ms, RING_STALL_EVENT_NS / 1_000_000);
                assert_eq!(dropped_periods, 3); // three dropped periods so far
                assert_eq!(occupancy, 16);
                assert_eq!(reader_pid, 4321);
                assert_eq!(reader_heartbeat_age_ms, 3);
                assert_eq!(full_waits, 32);
            }
            other => panic!("expected one Detected, got {other:?}"),
        }
        assert!(t.stall_active());

        // Further stalling periods: NO per-period spam.
        for i in 0..5 {
            assert_eq!(
                t.observe(stall_input(4_000 + i, RING_STALL_EVENT_NS + i, true, true)),
                None,
                "no per-period event spam while a stall stays active",
            );
        }

        // Drain → exactly one Cleared(stuck_reader).
        match t.observe(stall_input(9_000, 5, false, true)) {
            Some(RingStallEvent::Cleared {
                reason,
                dropped_periods,
                ..
            }) => {
                assert_eq!(reason, StallReason::StuckReader);
                assert!(dropped_periods >= 3);
            }
            other => panic!("expected one Cleared, got {other:?}"),
        }
        assert!(!t.stall_active());
        // Steady state after clear: silent.
        assert_eq!(t.observe(stall_input(10_000, 0, false, true)), None);

        // The log line matches the #1524 event contract.
        let line = format_ring_stall_event(&RingStallEvent::Detected {
            reason: StallReason::StuckReader,
            duration_ms: 1000,
            dropped_periods: 12,
            occupancy: 16,
            reader_pid: 4321,
            reader_heartbeat_age_ms: 3,
            full_waits: 32,
        });
        assert!(
            line.starts_with("event=fanin.ring.stall_detected reason=stuck_reader"),
            "{line}",
        );
        for frag in [
            "duration_ms=1000",
            "dropped_periods=12",
            "occupancy=16",
            "reader_pid=4321",
            "reader_heartbeat_age_ms=3",
            "full_waits=32",
        ] {
            assert!(line.contains(frag), "missing {frag} in: {line}");
        }
        let cleared = format_ring_stall_event(&RingStallEvent::Cleared {
            reason: StallReason::StuckReader,
            duration_ms: 5000,
            dropped_periods: 900,
        });
        assert!(
            cleared
                .starts_with("event=fanin.ring.stall_cleared reason=stuck_reader duration_ms=5000"),
            "{cleared}",
        );
    }

    /// A sub-threshold drop burst (the designed normal-reload transient) emits
    /// NOTHING and never marks the episode active — the guard against journal
    /// spam on every CamillaDSP reload.
    #[test]
    fn ring_stall_silent_below_threshold() {
        let mut t = RingStallTracker::new();
        let below = RING_STALL_EVENT_NS - 1;
        for i in 0..50 {
            assert_eq!(
                t.observe(stall_input(i, below, true, true)),
                None,
                "a sub-threshold drop must not emit",
            );
            assert!(!t.stall_active());
            assert_eq!(t.last_stall_ms(), 0);
        }
        // Draining after a purely sub-threshold run stays silent (no Cleared for
        // an unlogged run).
        assert_eq!(t.observe(stall_input(100, 0, false, true)), None);
    }

    /// A flapping reader (rapid clear→re-stall) logs at most once per re-arm gap:
    /// a re-stall within the gap is suppressed (both its Detected and Cleared),
    /// and a fresh episode past the gap arms again.
    #[test]
    fn ring_stall_rate_limits_flapping() {
        let mut t = RingStallTracker::new();
        // Episode 1 at t=0: detect + clear.
        assert!(matches!(
            t.observe(stall_input(0, RING_STALL_EVENT_NS, true, true)),
            Some(RingStallEvent::Detected { .. }),
        ));
        assert!(matches!(
            t.observe(stall_input(1_000, 0, false, true)),
            Some(RingStallEvent::Cleared { .. }),
        ));
        // A re-stall < gap after the last DETECTED (t=0) is suppressed.
        let soon = RING_STALL_REARM_MIN_GAP_NS - 1;
        assert_eq!(
            t.observe(stall_input(soon, RING_STALL_EVENT_NS, true, true)),
            None,
            "a re-stall inside the re-arm gap is suppressed",
        );
        // ...and its unlogged clear is silent too (balanced).
        assert_eq!(t.observe(stall_input(soon + 100, 0, false, true)), None);
        // A fresh episode past the gap (measured from the last detected at t=0)
        // arms again.
        let later = RING_STALL_REARM_MIN_GAP_NS + 1;
        assert!(
            matches!(
                t.observe(stall_input(later, RING_STALL_EVENT_NS, true, true)),
                Some(RingStallEvent::Detected { .. }),
            ),
            "past the re-arm gap a new episode logs again",
        );
    }

    /// End-to-end through `write_ring_period`: while a live reader is wedged the
    /// per-period wall time is dominated by the bounded back-pressure waits;
    /// after the writer DEMOTES it (grace crossed) the period collapses back to
    /// one pacer sleep (real time), the drops are attributed to
    /// `stuck_reader_drops`, and the stall episode is surfaced as active.
    #[test]
    fn ring_step_returns_to_realtime_after_demotion() {
        let period_frames = 256u32; // 2 slots
        let (mut ring, path) = tmp_ring_output(2, "realtime_after_demote");
        // Attach a reader but NEVER consume: attach stamps reader_pid + a fresh
        // heartbeat, so it looks live for the ~2 s liveness window (the whole
        // test is sub-second) while read_seq stays frozen — the #1524 wedge. Kept
        // in scope so its Drop (which clears reader_pid) does not fire early.
        let _reader = RingReader::create_or_attach(&path, ring_geometry(2)).unwrap();
        let total = (period_frames as usize) * (CHANNELS as usize);
        let payload = vec![9u8; total * BYTES_PER_SAMPLE];
        // Fill the ring (first period publishes both slots to the live reader).
        assert_eq!(
            write_ring_period(&mut ring, &payload, period_frames),
            period_frames,
        );

        // PRE-GRACE: the ring is full and the reader is wedged but within grace →
        // each publish pays the bounded ~21 ms wait, so the period is slow.
        let pre = std::time::Instant::now();
        assert_eq!(write_ring_period(&mut ring, &payload, period_frames), 0);
        let pre = pre.elapsed();
        assert!(
            pre >= std::time::Duration::from_millis(5),
            "pre-grace period must pay the bounded back-pressure wait, got {pre:?}",
        );

        // Cross the grace deterministically (no real 1 s wait) via the writer's
        // test seam; the reader stays wedged so the backdated age survives.
        ring.writer
            .set_read_seq_advance_age_for_test(jasper_ring::STUCK_READER_GRACE_NS + 10_000_000);

        // POST-GRACE: demotion → the period is now just the pacer sleep, an
        // order of magnitude below the pre-grace wall time.
        let post = std::time::Instant::now();
        assert_eq!(write_ring_period(&mut ring, &payload, period_frames), 0);
        let post = post.elapsed();
        assert!(
            post * 2 < pre,
            "demotion must collapse per-period wall time back toward real time \
             (pre={pre:?}, post={post:?})",
        );
        // The demoted drops are attributed to stuck_reader_drops (not no-reader),
        // and the stall episode is surfaced as active.
        assert!(ring.counters.stuck_reader_drops.load(Ordering::Relaxed) > 0);
        assert_eq!(ring.counters.drop_no_reader.load(Ordering::Relaxed), 0);
        assert!(ring.counters.stall_active.load(Ordering::Relaxed));
        assert!(ring.counters.last_stall_ms.load(Ordering::Relaxed) >= 1000);
        cleanup_ring(&path);
    }
}
