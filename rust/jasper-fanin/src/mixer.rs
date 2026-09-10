// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! ALSA fan-in mixer — the core work loop.
//!
//! Reads N capture lanes (snd-aloop substream, renderer SHM ring, or the USB
//! gadget), sums them sample-wise, and publishes the sum to the program SHM
//! ring, this daemon's only final-output transport (ADR-0100).
//!
//! PACING: the blocking ring publish is the metronome, with [`PeriodPacer`] as
//! the floor under it when nothing downstream blocks — see
//! [`write_ring_period`]. INPUTS are opened NON-BLOCKING: a renderer that is
//! not producing returns `-EAGAIN` and the lane renders silence for that
//! period, and an overrun is `try_recover`ed and rendered as silence too.
//!
//! Lane inputs are interleaved stereo S32_LE, the program wire's own scale. The
//! sum accumulates into an **i64** scratch with `saturating_add`, so
//! simultaneous full-scale lanes keep real headroom above full scale before the
//! consumer clamps.

mod direct_capture;
mod dsp;
mod lane_fade;
mod pcm_open;

use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::mpsc::{Sender, SyncSender};
use std::sync::{Arc, Mutex};

use alsa::pcm::{Access, Format, Frames, HwParams, State, IO, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};
use log::{info, warn};

use jasper_ring::{Geometry, PublishOutcome, RingWriter, SAMPLE_FORMAT_S32LE};

use jasper_resampler::RMS_DBFS_FLOOR;

use crate::config::{Config, MEASUREMENT_LANE, RING_SLOT_FRAMES};
use crate::impulse_tap::{ImpulseDetector, TapConfig, TapEvent, TapState};
use crate::lane_resampler::{LaneResampler, LaneResamplerObservability};
use crate::tts::{TtsInput, TtsMixer};
use crate::watchdog::Heartbeat;

use direct_capture::{read_direct_and_render, DirectCapture};
pub use direct_capture::{DirectObservability, DrainStats};
use dsp::{
    apply_gain_to_sum, duck_step_per_frame, fill_ring_payload, mix_into, ramp_program_duck,
    saturate_to_i16, BYTES_PER_SAMPLE,
};
use lane_fade::LaneFade;
use pcm_open::{disabled_input, errno_of, open_direct_capture, open_direct_input, open_input};

/// Stereo, on both ends: the renderer ingress lanes carry 2 channels and so
/// does the ring this daemon publishes to. Not configurable.
pub const CHANNELS: u32 = 2;

/// Per-input catch-up target, in WHOLE periods. The fill we want a lane's
/// capture ring to sit at right before the per-period read. One period is
/// the steady state for a lane clocked off the local DAC (its producer and
/// our consumer share the DAC clock, so its ring never grows).
const CATCHUP_TARGET_PERIODS: i64 = 1;

/// Per-input catch-up high-water, in WHOLE periods. A lane whose readable
/// backlog exceeds this is treated as FREE-RUNNING relative to our DAC-paced
/// drain (today only the USB lane: the host clock feeds it, while we read at
/// the DAC rate) and bounded-resynced down to TARGET.
///
/// The tuning constraint is two-sided, reasoned on ring OCCUPANCY (what
/// `avail_update` reports on a capture PCM — frames readable), NOT inter-burst
/// gap time. Lower bound: it MUST sit above the worst-case peak occupancy of a
/// HEALTHY networked lane, or we would clip legitimately-buffered audio. Two
/// effects stack — a WiFi-bursty AirPlay lane deposits an A-MPDU burst of ~4
/// packets (~5.5 periods) into its ring at once (then drains back at the DAC
/// rate), and a scheduling stall delays OUR drain (worst-case ~36.8 ms ≈ 6.9
/// periods on a stressed stock Pi 5, PREEMPT_RT not yet in) — so a stall
/// coinciding with a burst is ~5.5 + 6.9
/// ≈ 12.4 periods of peak occupancy on a healthy lane. Upper bound: it MUST sit
/// below the input buffer depth (16 periods / 4096 frames, the "0 xruns over
/// 4.5 min" sizing) so the resync fires before overrun. 14 periods (~75 ms)
/// clears the ~12.4-period healthy burst+stall peak with ~1.6-period margin and
/// still leaves 2 periods under the 16-period buffer. A free-running lane grows
/// MONOTONICALLY (its producer's average rate exceeds ours), so it always
/// crosses this; a healthy lane's burst+stall peak stays below it.
///
/// NOT drift correction and NOT drop-free: a controlled, occasional drop-resync
/// at the residual drift rate. A backed-up lane loses a bounded chunk of audio
/// at each resync instead of cascading into an upstream producer overflow.
const CATCHUP_HIGH_WATER_PERIODS: i64 = 14;

/// Hard cap on whole periods discarded in a single resync, so a pathological
/// `avail` (driver fault, or a huge buffer) can't turn the bounded
/// read-and-drop into an unbounded syscall spin inside the hot loop. A lane
/// further behind than this finishes resyncing over the next few periods —
/// still bounded per period.
const CATCHUP_MAX_DRAIN_PERIODS: i64 = 64;

/// Emit the rate-limited `event=fanin.input.catchup` log on the 1st resync
/// for a lane and then every Nth, so a chronically free-running lane can't
/// spam the journal. Count-based (not time-based) so the hot loop never
/// reads a clock.
const CATCHUP_LOG_EVERY: u64 = 64;

/// USB DIRECT capture open envelope, hardware-proven on the UAC2 gadget and
/// deliberately NOT fan-in's aloop-tuned `configure_pcm`: S32_LE 2ch 48k,
/// period 256, buffer ~768 (near).
///
/// This is the DEFAULT gadget open period; the actual open period is
/// overridable via `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES`. The chunk-read cap
/// and the narrowing scratch below stay pinned to this default regardless of
/// the open period: they bound the per-`readi` granularity (256 frames), which
/// is independent of the gadget's period IRQ cadence, so a larger open period
/// never overflows the fixed scratch and a smaller one never under-reads.
const DIRECT_PERIOD_FRAMES: u32 = 256;

/// Deep-buffer safety floor for the (tunable) direct open period: the
/// negotiated capture buffer must clear BOTH bounds — at least three whole
/// periods, and at least the proven 768-frame floor. At the default period 256
/// the two coincide (3×256 = 768). `resolve_direct_buffer_frames` is the single
/// owner of this rule.
///
/// What the lane depends on is this ACCEPTANCE rule — `≥ max(3×period, 768)`
/// and period-aligned — not on the kernel granting the exact request. Observed
/// with the lane open on one Pi, `/proc/asound/UAC2Gadget/pcm0c/sub0/hw_params`
/// read `period_size: 256`, `buffer_size: 1024`, `format: S32_LE`: `u_audio`
/// rounded the 768-frame request UP to four periods and the open was accepted
/// with the `buffer_near` warn. That is one box on one kernel, not a
/// negotiation contract — which is why the acceptance rule is a range and
/// STATUS reports the buffer the PCM is really running rather than the one
/// that was asked for.
const DIRECT_BUFFER_MIN_PERIODS: u32 = 3;
const DIRECT_BUFFER_MIN_FRAMES: u32 = 768;

/// Number of fixed histogram buckets for the drain-entry avail distribution.
/// Boundaries at 64-frame steps: `[0,64) [64,128) [128,192) [192,256)
/// [256,320) [320,+)`. Chosen so the measured ~186-frame standing gadget avail
/// lands mid-histogram and a bimodal avail (quantized near 0/256) is visible.
const DRAIN_AVAIL_BUCKETS: usize = 6;

/// Classify a drain-entry `avail` (frames) into one of [`DRAIN_AVAIL_BUCKETS`]
/// fixed buckets. Negative avail is never observed at a real `avail_update` Ok,
/// but the ALSA `Frames` type is `i64`, so it lands in bucket 0 with 0.
fn drain_avail_bucket(avail: i64) -> usize {
    if avail < 64 {
        0
    } else if avail < 128 {
        1
    } else if avail < 192 {
        2
    } else if avail < 256 {
        3
    } else if avail < 320 {
        4
    } else {
        5
    }
}

/// Emit the rate-limited drain-stats INFO line every this many drains. Gated by
/// the drain counter itself (no wall clock) so the cadence costs nothing on the
/// hot path. `2^15` ≈ one line per ~3 min at the default 256-frame render period
/// (5.33 ms/cycle) — deliberately coarse: one drain is recorded every render
/// cycle the gadget PCM is open, INCLUDING while the host is attached but idle,
/// so a tighter cadence would spam the persistent journal 24/7 on a
/// direct-enabled box.
const DRAIN_STATS_LOG_EVERY: u64 = 1 << 15;

/// Length of the S16 narrowing scratch the direct drain's TAP uses per chunk
/// read (the audio path is never narrowed).
///
/// The drain reads the gadget in chunks of at most [`DIRECT_PERIOD_FRAMES`]
/// frames (`to_read` in `drain_direct_capture`), so one chunk yields at most
/// `DIRECT_PERIOD_FRAMES × CHANNELS` interleaved S16 samples. This sizing is
/// INDEPENDENT of `config.period_frames`: the lane's `read_buf` is
/// `period_frames × CHANNELS` (the render-period contract), and reusing it for
/// the narrowing would slice out of bounds whenever `period_frames <
/// DIRECT_PERIOD_FRAMES` (e.g. `JASPER_FANIN_PERIOD_FRAMES=128`). That OOB is a
/// `panic=abort` in the hot loop → the `jasper-fanin` `StartLimitAction=reboot`
/// ladder, so the narrowing scratch is deliberately its own fixed buffer.
const fn direct_narrow_scratch_samples() -> usize {
    (DIRECT_PERIOD_FRAMES as usize) * (CHANNELS as usize)
}

/// Bounded capacity of the impulse tap's off-thread event channel. Its producer
/// cannot fill it in practice (the single tap detector fires at most once per
/// refractory window, ~4/s at the 250 ms default); the bound is a drop-and-count
/// safety net that keeps the mixer thread's `try_send` non-blocking.
pub(crate) const EVENT_CHANNEL_CAPACITY: usize = 256;

/// Forward one event to an off-thread writer with `try_send`, calling
/// `note_dropped` instead of blocking the SCHED_FIFO work loop on the writer's
/// I/O. EVERY failure counts, `Disconnected` included: a writer thread that
/// returned early (its artifact would not open) leaves the gauge as the only
/// evidence, and a gauge reading 0 while 100% of events are lost is the wrong
/// answer. See ADR-0254.
fn send_drop_counted<T>(tx: &SyncSender<T>, event: T, note_dropped: impl FnOnce()) {
    if tx.try_send(event).is_err() {
        note_dropped();
    }
}

/// USB DIRECT reopen retry cadence, in render PERIODS (~2 s at 256/48k = 375).
/// While the gadget is Absent, the lane attempts a reopen at most once per this
/// many periods it renders — a period-counted cadence so the hot loop never
/// reads a wall clock.
const DIRECT_REOPEN_RETRY_PERIODS: u64 = 375;

/// Consecutive zero-avail drains that mark a ZOMBIE capture handle. When the
/// gadget function is REBUILT underneath fan-in (a UDC rebind / usbsink
/// stop-start), fan-in's open `hw:UAC2Gadget` PCM stays attached to a DESTROYED
/// instance: `avail_update()` returns `Ok(0)` forever — NOT an errno, so
/// `classify_pcm_errno` never fires and the DeviceLost path never triggers, and
/// the lane goes deaf. This threshold (~2 s at the default 256/48k period,
/// matching `DIRECT_REOPEN_RETRY_PERIODS`) is how many consecutive render
/// periods of exactly-zero avail — while the handle is Present — trip a forced
/// close + bounded re-open. A genuinely idle-but-healthy host still streams
/// silence frames (avail > 0), so sustained EXACTLY-zero avail means the gadget
/// is no longer feeding this handle at all: either a zombie (reopen fixes it) or
/// a clean host-stream-stop (reopen re-establishes an identical handle, harmless
/// since no audio was flowing).
const DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS: u64 = 375;

/// Pure zombie-handle predicate. Both conditions must hold: frames have
/// actually flowed on this handle since the last open, AND the zero-avail run
/// has reached `threshold`.
///
/// The `frames_flowed` arming gate is what distinguishes a real gadget rebuild
/// from an ordinary attached-idle host: a host wired but silent streams avail≈0
/// drains forever without ever latching `frames_flowed`, so it accumulates the
/// streak but never trips. Extracted so that arming condition is testable
/// without ALSA — neither the `Ok(0)`-forever rebuild nor the attached-idle
/// stream is reproducible in a unit test otherwise.
fn zombie_handle_suspected(frames_flowed: bool, zero_avail_streak: u64, threshold: u64) -> bool {
    frames_flowed && threshold > 0 && zero_avail_streak >= threshold
}

/// Coarse per-direct-lane capture health for the STATUS `direct.health` field.
/// A pure classification over the direct lane's EXISTING atomics — no new
/// hot-path state — and no authority over gadget composition or source intent.
///
/// `health` is the INSTANTANEOUS classification: the zombie streak is reset by
/// the self-heal reopen the moment it trips, so `Broken` is a brief live window.
/// The cumulative `reopens` / `card_gen_reopens` counters are what carry durable
/// evidence for `/state`, doctor, and journal correlation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DirectHealth {
    /// Present, frames have flowed, handle not deaf — actively capturing.
    Capturing,
    /// Present-but-never-flowed, or Absent (no host / (re)opening). Healthy.
    Idle,
    /// Flowing→dead deaf handle (the zombie signature) — a real capture break.
    Broken,
}

/// Pure health classifier over the direct lane's existing atomics. See
/// [`DirectHealth`]. `threshold` is [`DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS`] in
/// production; injected for the unit tests.
pub(crate) fn direct_health(
    present: bool,
    frames_flowed_since_open: bool,
    zero_avail_streak: u64,
    threshold: u64,
) -> DirectHealth {
    if zombie_handle_suspected(frames_flowed_since_open, zero_avail_streak, threshold) {
        DirectHealth::Broken
    } else if present && frames_flowed_since_open {
        DirectHealth::Capturing
    } else {
        DirectHealth::Idle
    }
}

/// Stable STATUS token for a [`DirectHealth`]. Kept in lock-step with the Python
/// fan-in status reader (`DIRECT_HEALTH_BROKEN`, etc.).
pub(crate) fn direct_health_str(h: DirectHealth) -> &'static str {
    match h {
        DirectHealth::Capturing => "capturing",
        DirectHealth::Idle => "idle",
        DirectHealth::Broken => "broken",
    }
}

/// Read the live atomics of a [`DirectObservability`] and classify, using the
/// production zombie threshold.
pub(crate) fn direct_health_str_from_obs(d: &DirectObservability) -> &'static str {
    direct_health_str(direct_health(
        d.present.load(Ordering::Relaxed),
        d.frames_flowed_since_open.load(Ordering::Relaxed),
        d.zero_avail_streak.load(Ordering::Relaxed),
        DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
    ))
}

/// Cadence for the HANDLE-LIVENESS probe, in render PERIODS between one
/// `snd_pcm_status(2)` ioctl on the open capture handle. 187 is the whole-period
/// count nearest one second at the default 256/48k geometry. The probe is a real
/// syscall (unlike the mmap-served `avail_update`), so it rides the drain-stats
/// housekeeping cadence rather than the per-period hot path. The cadence is
/// advisory — detection latency, not correctness — so drift under a non-default
/// period override is harmless.
const DIRECT_LIVENESS_PROBE_EVERY_PERIODS: u64 = 187;

/// Pure handle-liveness decision: map the result of one `snd_pcm_status` ioctl
/// on the open capture handle to "is this handle dead (force a reopen) or live
/// (no-op)?". The argument is the PCM `State` the ioctl reported, or `None` when
/// the ioctl ITSELF errored. Split from the impure [`probe_direct_liveness`] so
/// the decision is unit-testable without ALSA.
///
/// Why this is the deterministic discriminator: when the UAC2 gadget FUNCTION is
/// rebuilt underneath the open handle (a UDC unbind/rebind or a usbsink
/// stop-start), the kernel runs `snd_card_disconnect`, which swaps the stale
/// file's fops to the shutdown set. `snd_pcm_status` is a real
/// `SNDRV_PCM_IOCTL_STATUS` ioctl — the hw plugin never serves it from the mmap'd
/// control page — so on a disconnected card it deterministically returns
/// `-ENODEV` or reports `State::Disconnected`. That is exactly why `avail_update`
/// cannot see the zombie: it reads the frozen mmap status page and keeps
/// returning `Ok(0)`.
///
/// An ioctl error is treated as dead: the handle cannot be confirmed live, and a
/// reopen that lands on a healthy handle is a cheap no-op re-establish. There is
/// no false-positive on an attached-IDLE host — an idle-but-attached host keeps
/// the capture stream `Prepared`/`Running`, so this returns `false` however long
/// it sits silent.
fn liveness_probe_dead(state: Option<State>) -> bool {
    match state {
        None => true,
        Some(State::Disconnected) => true,
        Some(_) => false,
    }
}

/// Issue one `snd_pcm_status` ioctl on the open capture handle and reduce it to
/// the [`liveness_probe_dead`] input: `Some(state)` on success, `None` when the
/// ioctl errored (`-ENODEV` on a rebuilt/disconnected card). Called on the
/// housekeeping cadence, never the per-period hot path.
fn probe_direct_liveness(pcm: &PCM) -> Option<State> {
    pcm.status().ok().map(|s| s.get_state())
}

/// Delay, in whole seconds, from a lane's idle→active transition to its
/// one-shot AUTO-TRIM fire. Gives the chain time to warm up and establish its
/// standing fill before the trim drops it — trimming at t=0 (before the fill
/// has accumulated) would be a no-op. Converted to a `frames_read` budget at
/// the live sample rate (`sample_rate × seconds`) so the wall-clock delay is
/// stable across period geometries. Only consulted when
/// `JASPER_FANIN_AUTO_TRIM=enabled`.
const AUTO_TRIM_DELAY_SECONDS: u64 = 2;

const CUSHION_DECAY_STABILITY_MS: u64 = 2000;

/// Per-lane TRIM control + counters, shared (`Arc`) between the mixer work
/// thread — which OWNS the `LaneResampler` and performs the actual ring trim —
/// and the state-server thread, which only requests trims and reads the
/// counters for STATUS. The control endpoint cannot touch the mixer-owned
/// resampler directly, so it sets `pending` and the work loop does the trim at
/// its next period boundary.
#[derive(Debug)]
pub struct TrimControl {
    /// Set by a `TRIM` control command; consumed (cleared) by the work loop at
    /// the next period boundary. Idempotent — a second `TRIM` before the loop
    /// consumed the first just re-sets the same flag (one trim results).
    pub pending: AtomicBool,
    /// Set by a `DECAY_SNAP` control command; consumed by the work loop the same
    /// way. The ONLY lever that opens a cushion-refill window on demand — a
    /// still-locked snap-back otherwise needs a real ladder demotion, so the
    /// hold this repo ships (ADR-0214) is unprovable on hardware without it.
    pub decay_snap_pending: AtomicBool,
    /// Cumulative TRIM operations that actually dropped ≥1 frame on this lane.
    pub trims: AtomicU64,
    /// Cumulative frames dropped by TRIM from this lane's resampler ring. Paired
    /// with `trims` so STATUS shows both how often and how much.
    pub trimmed_frames: AtomicU64,
    /// AUTO-TRIM one-shot latch: `true` once the auto-trim has fired for the
    /// current active session, so it fires exactly once per idle→active→…→idle
    /// cycle. Re-armed when the lane goes idle. Only used when auto-trim is on.
    pub auto_fired: AtomicBool,
}

impl TrimControl {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            pending: AtomicBool::new(false),
            decay_snap_pending: AtomicBool::new(false),
            trims: AtomicU64::new(0),
            trimmed_frames: AtomicU64::new(0),
            auto_fired: AtomicBool::new(false),
        })
    }

    /// Construct a `TrimControl` seeded with explicit counter values, for the
    /// state-server STATUS/command tests. Not compiled into the daemon.
    #[cfg(test)]
    pub fn test_fixture(trims: u64, trimmed_frames: u64, pending: bool) -> Self {
        Self {
            pending: AtomicBool::new(pending),
            decay_snap_pending: AtomicBool::new(false),
            trims: AtomicU64::new(trims),
            trimmed_frames: AtomicU64::new(trimmed_frames),
            auto_fired: AtomicBool::new(false),
        }
    }
}

/// The outcome of the pure AUTO-TRIM latch update for one lane in one period.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct AutoTrimDecision {
    /// The lane's updated latch state to store back.
    next: AutoTrimLaneState,
    /// `true` iff this lane's one-shot auto-trim should fire THIS period.
    fire: bool,
}

/// Pure AUTO-TRIM latch update for one lane. Given the lane's cumulative
/// `frames_read` now, its previous latch `state`, and the post-activation
/// `delay_frames`, decide whether the one-shot trim fires and produce the next
/// latch state. No ALSA, no clock, no atomics — unit-testable on any host.
///
/// This reports `fire` on EVERY period past the delay; ONE-SHOT is the caller's
/// `TrimControl::auto_fired` latch, which encodes "already fired this session"
/// as an atomic this pure function cannot see.
fn auto_trim_decision(
    frames_read: u64,
    state: AutoTrimLaneState,
    delay_frames: u64,
) -> AutoTrimDecision {
    let active_this_period = frames_read > state.last_frames_read;
    let mut next = AutoTrimLaneState {
        last_frames_read: frames_read,
        active_since: state.active_since,
    };
    if active_this_period {
        match state.active_since {
            None => {
                // idle→active: arm the delay from here; never fire on the
                // activation period itself (the standing fill has not
                // accumulated yet).
                next.active_since = Some(frames_read);
                AutoTrimDecision { next, fire: false }
            }
            Some(since) => {
                let elapsed = frames_read.saturating_sub(since);
                let fire = elapsed >= delay_frames;
                AutoTrimDecision { next, fire }
            }
        }
    } else {
        // No read this period. If the lane was active, it just went idle —
        // re-arm for the next activation. An already-idle lane stays idle.
        next.active_since = None;
        AutoTrimDecision { next, fire: false }
    }
}

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
/// 2-slot default in ~16 ms, against ~3 s and ~0.4 s at 1%. The cost of running
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
struct PeriodPacer {
    nominal_ns: u64,
    /// The targeted period in nanoseconds — one nominal period
    /// (`period_frames / sample_rate`) less [`PACE_HEADROOM_PERCENT`],
    /// precomputed so the hot loop never divides.
    target_ns: u64,
    /// End of the period in flight; `None` until the first period anchors it.
    deadline_ns: Option<u64>,
}

impl PeriodPacer {
    fn new(period_ns: u64) -> Self {
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
struct RingOutput {
    writer: RingWriter,
    counters: RingCounters,
    /// Wall-time floor under one period.
    pace: PeriodPacer,
    /// Edge-detection state machine for the ring stall event (issue #1524).
    stall: RingStallTracker,
}

/// Whether a `RingWriter::create_or_attach` failure is CONFIG-class — the
/// question that decides between an exit-78 PARK and the ordinary
/// `Restart=on-failure` ladder.
///
/// Only two `io::ErrorKind`s qualify, and `jasper_ring` sets both DELIBERATELY
/// for exactly this purpose:
///   - [`io::ErrorKind::InvalidInput`] — `Geometry::validate_self` rejecting the
///     geometry fan-in built from its own env (an unsupported sample format, an
///     out-of-range `n_slots`, an over-large slot), plus a ring path containing
///     a NUL.
///   - [`io::ErrorKind::InvalidData`] — the attach-time field-by-field header
///     mismatch, the header/file-size cross-check, and an unreclaimable
///     magic-less file. A stale ring from a prior geometry lands here.
///
/// Everything else is TRANSIENT and must keep the restart ladder: `WouldBlock`
/// (another process still holds the `.open.lock`), `PermissionDenied` (tmpfs
/// mode/group not yet applied by systemd-tmpfiles), `StorageFull` /
/// `OutOfMemory`, `AlreadyExists`, `IsADirectory`, and any raw OS error. Parking
/// on those would take the speaker's audio down over faults that clear
/// themselves.
///
/// Deliberately matched on the CLOSED accept-set rather than an open
/// "everything but X" list: a kind this daemon has not reasoned about defaults
/// to the recoverable path.
fn ring_open_error_is_config_class(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::InvalidInput | std::io::ErrorKind::InvalidData
    )
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
    if ring_open_error_is_config_class(&error) {
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

pub struct Mixer {
    inputs: Vec<Input>,
    output: RingOutput,
    /// Per-period scratch: `period_frames * CHANNELS` samples at the i32 spine
    /// scale. `i64` so the mix keeps real headroom above full scale: two
    /// full-scale lanes would saturate in an `i32`, and the program duck's job
    /// is to pull such a sum back into range before the write. Saturating back
    /// into i32 is the consumer's job (`dsp::fill_ring_payload`).
    sum_buf: Vec<i64>,
    /// Per-period Ring A payload: `sum_buf` saturated into the i32 spine range,
    /// little-endian. Bytes rather than `Vec<i32>` because the wire is
    /// explicitly LITTLE-endian: `to_le_bytes` states that, where reinterpreting
    /// an `i32` slice would silently depend on the host's endianness.
    /// `4 * sum_buf.len()` bytes.
    ring_payload: Vec<u8>,
    /// Per-period pre-duck program buffer for the assistant loudness
    /// meter. Same length as sum_buf.
    content_meter_buf: Vec<i16>,
    /// Cumulative output frames written since startup. Surfaced via
    /// the STATUS endpoint.
    pub frames_written: Arc<AtomicU64>,
    /// Selected input index. -2 means pass no renderer lanes;
    /// non-negative means pass only that source's lane. The
    /// correction/test lane is always mixed so diagnostics keep working
    /// even if the household selected a renderer manually or mux
    /// temporarily selected NONE.
    selected_input_index: Arc<AtomicI32>,
    period_frames: u32,
    tts: Option<TtsMixer>,
    /// Smoothed program-lane duck gain (linear), persisted across periods.
    /// 1.0 = no duck. `step()` glides this toward the per-period target
    /// (1.0, or the configured duck level while TTS/cue/chirp audio is
    /// queued) at `program_duck_attack_step` / `program_duck_release_step`
    /// per frame. Applying the ~25 dB duck as a hard per-period step instead
    /// clicks and pumps the music around every earcon/cue.
    program_duck_current: f32,
    /// Per-frame linear-gain decrement while ducking DOWN (attack).
    program_duck_attack_step: f32,
    /// Per-frame linear-gain increment while releasing UP toward 1.0.
    program_duck_release_step: f32,
    /// Ring A's shared counters and attached-header echo, cloned for the STATUS
    /// endpoint.
    pub ring_observability: RingObservability,
    /// DEFAULT-OFF one-shot AUTO-TRIM (`JASPER_FANIN_AUTO_TRIM=enabled`). When
    /// set, the work loop schedules ONE trim per lane ~`AUTO_TRIM_DELAY_SECONDS`
    /// after that lane transitions idle→active, latched via
    /// `TrimControl::auto_fired`. Manual `TRIM` works regardless of this flag.
    auto_trim_enabled: bool,
    /// Frames-active gate for the AUTO-TRIM delay: a lane must have read this
    /// many real frames since going active before its one-shot auto-trim fires
    /// (`AUTO_TRIM_DELAY_SECONDS` worth at the live sample rate). Derived once at
    /// construction so the work loop compares against a plain integer.
    auto_trim_delay_frames: u64,
    /// Per-lane AUTO-TRIM latch state, indexed parallel to `inputs`. Only
    /// maintained when `auto_trim_enabled`. Uses cumulative `frames_read` deltas
    /// (no wall clock in the hot loop) to detect idle↔active transitions and to
    /// measure the post-activation delay.
    auto_trim_lane_state: Vec<AutoTrimLaneState>,
    /// The impulse tap over the USB DIRECT capture ingress. Runs inline in
    /// `read_direct_and_render`, before `push_input`, over its own S16 view of
    /// the read (the marker detector is an S16 contract; the audio path is not
    /// narrowed).
    /// Present regardless of the direct flag; its disarmed cost is one relaxed
    /// atomic load per direct read.
    direct_tap: DirectTapHook,
    /// The receiver half of the tap channel, taken by `main` to drive the
    /// `fanin-tap-writer` thread (the single JSONL writer). `None` after
    /// `take_direct_tap_receiver`.
    direct_tap_receiver: Option<std::sync::mpsc::Receiver<TapEvent>>,
    host_clock_ladder_l0: Arc<AtomicBool>,
    usb_connection_epoch: Arc<AtomicU64>,
    host_clock_timing_failed: Arc<AtomicBool>,
}

/// Per-lane AUTO-TRIM bookkeeping. Tracks the cumulative `frames_read` value
/// seen last period (to detect this-period activity) and the value at the most
/// recent idle→active transition (to measure the post-activation delay).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct AutoTrimLaneState {
    /// `frames_read` observed at the previous `maybe_trim` call. The lane read
    /// audio this period iff the current value exceeds this.
    last_frames_read: u64,
    /// `frames_read` at the lane's most recent idle→active transition. The
    /// one-shot trim fires once `frames_read - active_since >= delay_frames`.
    /// `None` while the lane is idle (nothing active to delay from).
    active_since: Option<u64>,
}

/// The live SPSC ring counters the mixer step updates each period (from the
/// writer's [`jasper_ring::WriterMetrics`]). Cloned into
/// [`RingObservability`] for STATUS so the endpoint reads the same atomics the
/// work loop writes. Distinct from `WriterMetrics`, which is a value snapshot.
#[derive(Clone)]
struct RingCounters {
    nominal_clock: Arc<AtomicBool>,
    published: Arc<AtomicU64>,
    full_waits: Arc<AtomicU64>,
    /// Live-but-STUCK reader drops (issue #1524) — the bounded-wait give-ups
    /// (`DroppedStuck`) plus the sticky demotions (`DroppedStuckDemoted`). Kept
    /// separate from `drop_no_reader` so a heartbeat-live wedge is
    /// distinguishable from a benign no-reader reload.
    stuck_reader_drops: Arc<AtomicU64>,
    /// Dead/absent-reader free-run drops (`DroppedNoReader`) — the normal
    /// CamillaDSP-reload transient.
    drop_no_reader: Arc<AtomicU64>,
    occupancy: Arc<AtomicU64>,
    /// A ring stall episode (full + reader heartbeat-live + `read_seq` frozen
    /// past the grace, OR a >1 s no-reader hold) is CURRENTLY in progress.
    stall_active: Arc<AtomicBool>,
    /// Duration in ms of the current (if `stall_active`) or most-recent stall
    /// episode; 0 if none has ever occurred.
    last_stall_ms: Arc<AtomicU64>,
    /// Periods whose ONLY pacer was [`PeriodPacer`] — neither a blocking publish
    /// nor a dropped slot spent the period's wall time.
    ///
    /// It climbs in bursts whenever the ring has room (a cold start, a CamillaDSP
    /// reattach) and stops once back-pressure resumes. Climbing at the PERIOD
    /// RATE while `full_waits` stays flat is the diagnostic: it means the pacer,
    /// not the DAC, is the metronome — the free-running-reader shape
    /// [`PeriodPacer`] exists for.
    clockless_paces: Arc<AtomicU64>,
}

impl RingCounters {
    fn new() -> Self {
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
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StallReason {
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
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RingStallEvent {
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
struct RingStallTracker {
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
    fn new() -> Self {
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
fn format_ring_stall_event(event: &RingStallEvent) -> String {
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

/// Which transport a fan-in lane's audio arrives over — the vocabulary STATUS
/// publishes as each input's `source`, and the ONE place those tokens are spelled.
///
/// The three are alternatives, not layers: exactly one applies to a lane, and
/// the lane's read path, its `pcm`/`direct` fields' emptiness, and this token
/// all follow from the same `Option`s (see [`Input::lane_source`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LaneSource {
    /// An snd-aloop capture substream — the shipped default for every renderer.
    Lane,
    /// The USB gadget capture, read directly (`hw:UAC2Gadget`).
    Direct,
    /// No transport at all: the lane opens nothing and renders silence. The USB
    /// lane with `JASPER_FANIN_USB_DIRECT` off — it keeps its roster label (mux
    /// still addresses it by SELECT/MUTE) but has no device to read. Distinct
    /// from an armed direct lane whose gadget is unplugged
    /// (`direct.present=false`).
    Disabled,
}

impl LaneSource {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Lane => "lane",
            Self::Direct => "direct",
            Self::Disabled => "disabled",
        }
    }
}

/// The transport a lane will be built with, decided BEFORE anything is opened.
/// The USB lane reads the gadget capture when DIRECT is armed and nothing at all
/// otherwise: its aloop substream has had no writer since the usbsink bridge was
/// deleted, so opening it would only sum silence from a lane that can never
/// carry audio. Every other lane reads its aloop substream and is required.
/// [`Input::lane_source`] re-derives the same token from what was actually
/// opened, so STATUS cannot disagree with this plan.
pub(crate) fn planned_lane_source(config: &Config, label: &str) -> LaneSource {
    if config.lane_wants_resampler(label) {
        // "USB lane AND direct armed" is spelled once, by the same predicate
        // that arms this lane's resampler.
        LaneSource::Direct
    } else if config.input_resampler_lane_label == label {
        LaneSource::Disabled
    } else {
        LaneSource::Lane
    }
}

pub struct Input {
    /// The aloop capture PCM for this lane. `None` on the one lane that reads no
    /// aloop substream at all: the USB lane, whose audio comes from the
    /// `hw:UAC2Gadget` capture in `direct` — or from nowhere, when direct is
    /// off. Every other lane has `Some`.
    pcm: Option<PCM>,
    /// DEFAULT-OFF USB DIRECT capture. `Some` only on the usbsink lane when
    /// `JASPER_FANIN_USB_DIRECT=enabled`; the lane then reads `hw:UAC2Gadget`.
    /// `None` on every other lane (which have `pcm.is_some()`) and on this lane
    /// when the flag is off, where BOTH are `None` and the lane renders silence.
    direct: Option<DirectCapture>,
    /// The DIRECT lane's deferred device-open channel (#2533). `Some` alongside
    /// `direct`; `None` if the opener thread could not be spawned. The render
    /// loop hands every `snd_pcm_open` / `snd_pcm_close` to it rather than
    /// running one inside the period budget.
    direct_opener: Option<direct_capture::DirectOpener>,
    pub label: String,
    pub pcm_name: String,
    /// Per-input read buffer (i32 interleaved stereo, the program wire's spine
    /// scale). Reused as the discard scratch by the catch-up drain — no
    /// per-period allocation.
    read_buf: Vec<i32>,
    pub xrun_count: Arc<AtomicU64>,
    /// `CLOCK_MONOTONIC` milliseconds of this lane's last xrun, or
    /// [`jasper_daemon::json::NEVER_MS`] until the first one. Bumped with `xrun_count` by
    /// [`Input::note_xrun`]; STATUS renders it as the lane's `last_xrun_age_ms`.
    pub last_xrun_ms: Arc<AtomicU64>,
    pub frames_read: Arc<AtomicU64>,
    /// Per-lane content level: the most recent period's RMS in dBFS, ×100 and
    /// rounded into an `i32`. Overwritten EVERY period from exactly the samples
    /// this lane read; silence / gadget-absent renders the `RMS_DBFS_FLOOR`
    /// (-120 dBFS). STATUS surfaces it as the lane's `rms_dbfs`; mux's
    /// combo-liveness gate reads the USB DIRECT lane's value to reject a host
    /// streaming digital silence.
    pub rms_dbfs_x100: Arc<AtomicI32>,
    /// Cumulative frames DISCARDED by the bounded catch-up resync on this
    /// lane (see `drain_input_excess`). Non-zero only on a free-running
    /// lane (the USB host-clock lane); stays 0 forever on DAC-locked lanes.
    /// A growing value is the operator's "this lane is drifting and we are
    /// drop-resyncing it" signal — surfaced via STATUS, never escalated.
    pub catchup_resync_frames: Arc<AtomicU64>,
    /// Cumulative catch-up resync EVENTS (each is one high-water crossing
    /// that discarded ≥1 period). Paired with `catchup_resync_frames` so
    /// STATUS shows both how often and how much.
    pub catchup_events: Arc<AtomicU64>,
    /// OPTIONAL per-input adaptive resampler (DEFAULT-OFF). `Some` only on the
    /// USB DIRECT lane (see [`Config::lane_wants_resampler`]). When `Some`,
    /// this lane is rate-reconciled to the DAC clock (drop-free) instead of
    /// catch-up-drained.
    resampler: Option<LaneResampler>,
    /// Per-lane TRIM control + counters, shared with the state-server thread.
    /// The control endpoint sets `pending`; the work loop trims the resampler
    /// ring at the next period boundary (see `maybe_trim` / `trim_input`).
    trim: Arc<TrimControl>,
    /// Per-lane MIX MUTE, shared with the state-server thread. The control
    /// endpoint (`MUTE`/`UNMUTE <label>`) flips it; the work loop reads it at
    /// the SUM stage (`lane_mix_contributes`) and takes this lane's contribution
    /// to zero across `lane_fade`'s 10 ms window — silent within a period or
    /// two, not within a sample — WITHOUT touching this lane's capture,
    /// `frames_read`, or `rms_dbfs_x100` telemetry, which are accounted BEFORE
    /// the gate. This is mux's latest-source-wins arbitration primitive for the
    /// USB lane — the sole USB-silencing mechanism in the one-pipeline design.
    /// NOT persisted: a fan-in restart comes up unmuted and mux reasserts.
    muted: Arc<AtomicBool>,
    /// OPTIONAL USB DIRECT observability, shared with the state-server thread.
    /// `Some` only on the USB DIRECT lane (`direct.is_some()`); STATUS renders a
    /// `direct{}` block from it.
    direct_obs: Option<DirectObservability>,
    /// Per-lane WAKE FADE-IN and SELECTION FADE state (issue #3443). Held by
    /// every lane and usable at both lane scales, because it runs over this
    /// lane's rendered period — the one thing every read arm produces.
    /// Silence is tracked before the selection gate and both windows are applied
    /// after it; inert on [`MEASUREMENT_LANE`].
    lane_fade: LaneFade,
}

impl Mixer {
    /// Open all configured inputs and the output. Every aloop lane is required:
    /// a missing lane means one renderer silently drops out of the summed music
    /// reference. The USB lane is the sole exception (see
    /// [`planned_lane_source`]).
    pub fn new(config: &Config, tts: Option<TtsInput>) -> Result<Self> {
        let period_samples = (config.period_frames as usize) * (CHANNELS as usize);

        let mut inputs = Vec::with_capacity(config.input_renderers.len());
        // The USB lane takes no aloop PCM (config pins the list one shorter), so
        // the remaining labels consume `input_pcms` in order.
        let mut aloop_pcms = config.input_pcms.iter();
        for label in &config.input_renderers {
            // The USB lane is the only clock-crossing lane, and it owns a
            // resampler whenever it has a device to read: direct capture has no
            // aloop catch-up fallback. A construction failure degrades to `None`
            // with a warning rather than failing the daemon.
            let resampler = if config.lane_wants_resampler(label) {
                build_lane_resampler(label, config)
            } else {
                None
            };
            let input = match planned_lane_source(config, label) {
                // USB DIRECT: reads hw:UAC2Gadget. Best-effort — a gadget-absent
                // lane starts `DirectCapture::Absent` and renders silence with a
                // bounded reopen retry. The fail-hard "every input required" contract is
                // exempted ONLY here.
                LaneSource::Direct => open_direct_input(label, config, resampler),
                LaneSource::Disabled => disabled_input(label, config, resampler),
                LaneSource::Lane => {
                    let Some(pcm_name) = aloop_pcms.next() else {
                        anyhow::bail!(
                            "fan-in input '{}' has no JASPER_FANIN_INPUT_PCMS entry",
                            label,
                        );
                    };
                    match open_input(pcm_name, label, config, resampler) {
                        Ok(input) => input,
                        Err(e) => {
                            anyhow::bail!(
                                "required fan-in input '{}' ({}) failed to open: {:#}",
                                label,
                                pcm_name,
                                e,
                            );
                        }
                    }
                }
            };
            info!(
                "event=fanin.input.opened label={} pcm={} period_frames={} buffer_frames={} source={}",
                label,
                input.pcm_name,
                config.period_frames,
                config.input_buffer_frames,
                input.lane_source().as_str(),
            );
            inputs.push(input);
        }

        if inputs.is_empty() {
            anyhow::bail!(
                "no input PCMs opened successfully — daemon has nothing to mix. \
                 Check /etc/asound.conf for the per-renderer substream aliases \
                 (librespot_substream / shairport_substream / etc.) and snd-aloop \
                 module status (lsmod | grep snd_aloop)."
            );
        }

        // If the resampler is armed by env but its configured lane label matched
        // no live input, the feature silently no-ops. Warn ONCE with the
        // available labels so an operator can see why they observed no effect.
        if let Some(available) = resampler_lane_not_found(
            config.usb_direct_enabled,
            &config.input_resampler_lane_label,
            &config.input_renderers,
        ) {
            warn!(
                "event=fanin.resampler.noop reason=lane_not_found requested={} available=[{}]",
                config.input_resampler_lane_label, available,
            );
        }

        // Ring A — the only final-output transport (ADR-0100). Slot is pinned to
        // 128 frames by the outputd DAC-period contract; `period_frames % 128 ==
        // 0` was validated at config parse. A geometry mismatch against an
        // already-created ring is a config-class fault (main() exits 78,
        // `RestartPreventExitStatus=78` parks the unit); everything else this
        // open can fail with is TRANSIENT and keeps the restart ladder — see
        // `ring_open_error_is_config_class`.
        let geometry = Geometry {
            rate: config.sample_rate,
            channels: CHANNELS,
            sample_format: SAMPLE_FORMAT_S32LE,
            period_frames: RING_SLOT_FRAMES,
            n_slots: config.ring_slots,
        };
        let writer = RingWriter::create_or_attach(&config.ring_path, geometry)
            .map_err(|e| ring_open_error(&config.ring_path, e))
            .with_context(|| format!("opening fan-in→camilla SHM ring {}", config.ring_path))?;
        // NOTHING else is opened. The ring IS the program path: no ALSA playback
        // PCM is opened or fed (ADR-0100).
        let counters = RingCounters::new();
        let period_ns = (config.period_frames as u64) * 1_000_000_000 / (config.sample_rate as u64);
        // The geometry this ring OBSERVABLY carries, read back from the header
        // the writer attached against — not echoed from config, so it is what
        // the reader on the other end sees.
        let attached = writer.geometry();
        info!(
            "event=fanin.ring.opened path={} slots={} slot_frames={} period_frames={} slots_per_step={} channels={}",
            config.ring_path,
            config.ring_slots,
            RING_SLOT_FRAMES,
            config.period_frames,
            config.period_frames / RING_SLOT_FRAMES,
            attached.channels,
        );
        let ring_observability = RingObservability {
            nominal_clock: Arc::clone(&counters.nominal_clock),
            path: config.ring_path.clone(),
            slots: config.ring_slots,
            channels: attached.channels,
            occupancy: Arc::clone(&counters.occupancy),
            published: Arc::clone(&counters.published),
            full_waits: Arc::clone(&counters.full_waits),
            stuck_reader_drops: Arc::clone(&counters.stuck_reader_drops),
            drop_no_reader: Arc::clone(&counters.drop_no_reader),
            stall_active: Arc::clone(&counters.stall_active),
            last_stall_ms: Arc::clone(&counters.last_stall_ms),
            clockless_paces: Arc::clone(&counters.clockless_paces),
        };
        let output = RingOutput {
            writer,
            counters,
            pace: PeriodPacer::new(period_ns),
            stall: RingStallTracker::new(),
        };

        let input_count = inputs.len();
        // AUTO-TRIM delay in frames: `AUTO_TRIM_DELAY_SECONDS` at the live rate.
        let auto_trim_delay_frames = (config.sample_rate as u64) * AUTO_TRIM_DELAY_SECONDS;
        if config.auto_trim_enabled {
            info!(
                "event=fanin.auto_trim.armed delay_seconds={} delay_frames={}",
                AUTO_TRIM_DELAY_SECONDS, auto_trim_delay_frames,
            );
        }
        if config.usb_direct_enabled {
            info!(
                "event=fanin.usb_direct.armed lane={} device={} (bridge hop + aloop cable removed on this lane)",
                config.input_resampler_lane_label, config.usb_direct_device,
            );
        }
        // Impulse-tap channel. Default-disarmed: the tap state starts unarmed so
        // the direct read pays one relaxed atomic load until a TAP_ARM verb
        // arrives. The bounded channel keeps the mixer thread's hand-off
        // non-blocking (drop-and-count on Full); the fanin-tap-writer thread is
        // the sole JSONL writer.
        let (tap_sender, tap_receiver) =
            std::sync::mpsc::sync_channel::<TapEvent>(EVENT_CHANNEL_CAPACITY);
        let direct_tap = DirectTapHook::new(
            Arc::new(TapState::default()),
            Arc::new(Mutex::new(TapConfig::default())),
            tap_sender,
        );
        Ok(Self {
            inputs,
            output,
            sum_buf: vec![0i64; period_samples],
            ring_payload: vec![0u8; period_samples * BYTES_PER_SAMPLE],
            content_meter_buf: vec![0i16; period_samples],
            frames_written: Arc::new(AtomicU64::new(0)),
            selected_input_index: Arc::new(AtomicI32::new(-2)),
            period_frames: config.period_frames,
            tts: tts.map(TtsMixer::new),
            program_duck_current: 1.0,
            program_duck_attack_step: duck_step_per_frame(
                config.tts_duck_attack_ms,
                config.sample_rate,
            ),
            program_duck_release_step: duck_step_per_frame(
                config.tts_duck_release_ms,
                config.sample_rate,
            ),
            ring_observability,
            auto_trim_enabled: config.auto_trim_enabled,
            auto_trim_delay_frames,
            auto_trim_lane_state: vec![AutoTrimLaneState::default(); input_count],
            direct_tap,
            direct_tap_receiver: Some(tap_receiver),
            // Init to the inert state (not-l0, 0 ppm) so decay never leaves the
            // ceiling until the servo thread actually reports `l0_locked`.
            host_clock_ladder_l0: Arc::new(AtomicBool::new(false)),
            usb_connection_epoch: Arc::new(AtomicU64::new(0)),
            host_clock_timing_failed: Arc::new(AtomicBool::new(false)),
        })
    }

    /// Number of configured inputs. Mixer construction fails if any
    /// configured input cannot be opened.
    pub fn input_count(&self) -> usize {
        self.inputs.len()
    }

    /// Read-only access to per-input counters for the STATUS endpoint.
    pub fn inputs(&self) -> &[Input] {
        &self.inputs
    }

    /// Clone the cross-thread signals the combo-mode `fanin-host-clock` thread
    /// reads. Returns `Some` ONLY when the USB DIRECT lane exists AND owns a
    /// resampler; `None` when direct is off, or when resampler construction
    /// failed and the lane fell back to no resampler. The `HostClock` thread
    /// holds only these `Arc` atomics; it never touches the mixer.
    ///
    /// The signals ride atomics the mixer already publishes for STATUS, so this
    /// adds no hot-path work. `input_frames` is passed RAW: the host-clock
    /// adapter must NOT trim-compensate it — a `trim_ring` moves only the read
    /// cursor, so the `capture − playback` divergence the ladder differences is
    /// already trim-invariant.
    pub fn host_clock_signals(&self) -> Option<crate::host_clock::HostClockSignals> {
        let direct = self.inputs.iter().find(|inp| inp.is_direct())?;
        let resampler = direct.resampler_observability()?;
        let direct_obs = direct.direct_observability()?;
        Some(crate::host_clock::HostClockSignals {
            fill_frames: Arc::clone(&resampler.fill_frames),
            input_frames: Arc::clone(&resampler.input_frames),
            output_frames: Arc::clone(&resampler.output_frames),
            locked: Arc::clone(&resampler.locked),
            present: Arc::clone(&direct_obs.present),
            // The direct capture's monotonic successful-open counter is the sole
            // capture-generation source of truth.
            capture_generation: Arc::clone(&direct_obs.opens),
            // The resampler's LIVE correction ppm gauge (milli-ppm,
            // i64-bits-in-u64). Written by the resampler on the mixer thread; the
            // servo thread only ever READS it.
            correction_milli_ppm: Arc::clone(&resampler.ratio_milli_ppm),
            // The decay's declared refill window — `build_obs` clears
            // `Obs::steady` on it (ADR-0214).
            decay_refilling: Arc::clone(&resampler.decay_refilling),
            // The static acquisition ceiling — build_obs's anchor for
            // descent-compensating the fill it feeds the ladder (#3466).
            ceiling_fill_frames: resampler.target_fill_frames,
            // The LIVE held-target gauge the servo thread re-pins its setpoint to
            // each tick (tracks the cushion decay).
            held_target_frames: Arc::clone(&resampler.held_target_frames),
            // The REVERSE signals: owned here so both sides share the same
            // atomics, and the servo thread only ever WRITES these two.
            ladder_l0: Arc::clone(&self.host_clock_ladder_l0),
            connection_epoch: Arc::clone(&self.usb_connection_epoch),
            timing_failed: Arc::clone(&self.host_clock_timing_failed),
        })
    }

    /// Clone the USB DIRECT lane's existing frame counter plus the atomics owned
    /// by the lightweight source-notification helper. No host/policy object is
    /// exposed: fan-in reports only whether frames are flowing; mux remains the
    /// sole routing authority.
    pub fn source_notify_signals(&self) -> Option<crate::source_notify::SourceNotifySignals> {
        let direct = self.inputs.iter().find(|inp| inp.is_direct())?;
        let direct_obs = direct.direct_observability()?;
        let input_frames = direct
            .resampler_observability()
            .map(|resampler| Arc::clone(&resampler.input_frames))
            .unwrap_or_else(|| Arc::clone(&direct.frames_read));
        Some(crate::source_notify::SourceNotifySignals {
            input_frames,
            streaming: Arc::clone(&direct_obs.streaming),
            stream_starts: Arc::clone(&direct_obs.stream_starts),
            stream_stops: Arc::clone(&direct_obs.stream_stops),
            notify_attempts: Arc::clone(&direct_obs.notify_attempts),
            notify_failures: Arc::clone(&direct_obs.notify_failures),
        })
    }

    /// The shared impulse-tap state (armed + counters + knobs), cloned for the
    /// state-server thread's `TAP_ARM`/`TAP_DISARM`/STATUS handling.
    pub fn direct_tap_state(&self) -> Arc<TapState> {
        self.direct_tap.state()
    }

    /// The last-armed impulse-tap config, cloned for the state-server thread
    /// (published on arm, read on STATUS) and the writer thread.
    pub fn direct_tap_config(&self) -> Arc<Mutex<TapConfig>> {
        self.direct_tap.config()
    }

    /// Take the impulse-tap channel receiver so `main` can drive the
    /// `fanin-tap-writer` thread (the single JSONL writer). Returns `None` if
    /// already taken.
    pub fn take_direct_tap_receiver(&mut self) -> Option<std::sync::mpsc::Receiver<TapEvent>> {
        self.direct_tap_receiver.take()
    }

    /// Shared selected-input index for the STATUS/control endpoint.
    /// The audio loop reads this atomically once per period.
    pub fn selected_input_index(&self) -> Arc<AtomicI32> {
        Arc::clone(&self.selected_input_index)
    }

    /// Drive the work loop until `shutdown` is set. Bumps the
    /// heartbeat sentinel after every successful frame.
    ///
    /// Errors here are escalated to the daemon main, which returns
    /// non-zero so systemd's `Restart=on-failure` brings us back.
    /// Transient errors (xruns) are handled inside `step()` without
    /// escalation.
    pub fn run(&mut self, shutdown: &AtomicBool, heartbeat: &Heartbeat) -> Result<()> {
        // No prime + start: the SHM ring transport has no kernel ring to prime
        // and no PREPARED→RUNNING transition.
        info!("event=fanin.mixer.running inputs={}", self.inputs.len());

        while !shutdown.load(Ordering::Relaxed) {
            self.step()?;
            heartbeat.bump_progress();
        }

        info!(
            "event=fanin.mixer.stopped frames_written={}",
            self.frames_written.load(Ordering::Relaxed),
        );

        Ok(())
    }

    /// One period of work: read all inputs, sum, write output.
    fn step(&mut self) -> Result<()> {
        self.sum_buf.fill(0);

        // Drain TTS/control commands once at the period boundary. Voice ducking
        // attenuates only renderer/program lanes; TTS is mixed after the duck so
        // it stays unattenuated.
        let mut program_target = 1.0f32;
        if let Some(tts) = self.tts.as_mut() {
            if tts.prepare_period() {
                program_target = tts.program_duck_gain();
            }
        }

        // Service TRIM at the period boundary, BEFORE the read loop, so the
        // render below sees the trimmed ring.
        let period_frames = self.period_frames as usize;
        self.maybe_trim();

        let decay_l0 = self.host_clock_ladder_l0.load(Ordering::Relaxed);
        let connection_epoch = self.usb_connection_epoch.load(Ordering::Relaxed);
        let timing_failed = self.host_clock_timing_failed.load(Ordering::Relaxed);
        let selected_input = self.selected_input_index.load(Ordering::Relaxed);
        for (idx, input) in self.inputs.iter_mut().enumerate() {
            if let Some(r) = input.resampler.as_mut() {
                r.latency_context(connection_epoch, timing_failed);
            }
            let frames = if input.direct.is_some() {
                // USB DIRECT lane: read hw:UAC2Gadget directly, feed the SAME
                // resampler untouched, render one DAC-paced period. The aloop
                // substream is never touched (`pcm` is None). The tap runs inline
                // over its own narrowed view inside this call.
                read_direct_and_render(input, period_frames, &mut self.direct_tap)
            } else {
                // Bounded catch-up resync BEFORE the period read, for EVERY lane
                // regardless of selection. A free-running lane (the USB host-clock
                // lane) backs its capture ring up past the high-water; discarding
                // the excess down to one period here keeps the upstream producer
                // from overflowing so back-pressure can reach the host. A
                // DAC-locked lane sits at one period and this is a single
                // `avail_update` no-op. INTENTIONALLY independent of
                // `input_selected` below: a de-selected (muxed-out) free-running
                // lane STILL backs up and must be drained, so do NOT move this
                // under the selection gate.
                drain_input_excess(input, period_frames);
                read_input(input, period_frames)?
            };
            // Per-lane content level (RMS dBFS ×100), computed for EVERY lane
            // BEFORE the selection gate below so a muxed-out lane still reports
            // its true level to STATUS. `active` is the exact slice this lane
            // contributes to the sum (0 when it read nothing).
            let active = frames * (CHANNELS as usize);
            // Read the level off whichever buffer actually holds this lane's
            // period. The two RMS functions report the SAME dBFS for the same
            // signal — they differ only in the full-scale normalizer — so mux's
            // activity gate and the STATUS `rms_dbfs` keep one meaning across
            // widths.
            let lane_rms_dbfs = jasper_resampler::rms_dbfs_i32(&input.read_buf[..active]);
            input
                .rms_dbfs_x100
                .store((lane_rms_dbfs * 100.0).round() as i32, Ordering::Relaxed);
            // WAKE FADE-IN, tracking half (issue #3443). Read-only, and
            // deliberately for EVERY lane regardless of selection: silence is a
            // property of what the lane CAPTURES, so the run has to be counted on
            // the same periods the lane reads. Nothing is shaped here — the window
            // itself is applied after the gate below. The INFO line is bounded by
            // the state machine's own ARM_SILENCE_MS re-arm requirement.
            let woke = input
                .lane_fade
                .observe(&input.read_buf[..active], self.period_frames);
            if woke {
                info!("event=fanin.lane_wake_ramp label={}", input.label);
            }
            // Advance the post-lock cushion decay one render period on this
            // lane's resampler. AFTER the render, so the tick sees this period's
            // fresh lock state, and independent of the selection gate below: the
            // held target is a property of the lane's clock reconciliation, not
            // of which source is passed to the sum.
            if let Some(r) = input.resampler.as_mut() {
                // The DECAY_SNAP lever, consumed on the thread that OWNS the
                // resampler (the control thread cannot touch it), before the
                // tick so the window is open by the time the gauges publish.
                if input.trim.decay_snap_pending.swap(false, Ordering::Acquire) {
                    r.force_decay_snap_back();
                }
                r.tick_decay(decay_l0);
            }
            // Selection AND mute gate, applied at the SUM only: the per-lane
            // telemetry above is already accounted, so a de-selected OR muted
            // lane still reports its true captured level/liveness to STATUS. mux
            // reads the USB DIRECT lane's pre-mute level to keep a
            // muted-but-streaming host "playing" (no mute→release→mute flap).
            let contributes = lane_mix_contributes(
                selected_input,
                idx,
                &input.label,
                input.muted.load(Ordering::Relaxed),
            );
            // PER-LANE FADES, application half (issue #3443). The gate's verdict
            // is an INPUT here rather than a `continue`: a lane mux has just
            // dropped keeps reaching the sum until its window closes, which is
            // what stops a switch away from an already-PLAYING source removing a
            // full-amplitude waveform between one sample and the next. Only a
            // period that enters the sum spends the wake window, so a lane that
            // woke while mux still had it out keeps that ramp owed until `SELECT`
            // lands (mux arbitrates at 1 Hz). `false` means the lane is fully out
            // and contributes nothing this period.
            let reaches_sum = input.lane_fade.shape_period(
                &mut input.read_buf[..active],
                self.period_frames,
                contributes,
            );
            if !reaches_sum {
                continue;
            }
            // Only sum the samples actually read. `read_input` zero-pads the tail
            // so reading the full period would also be safe; the explicit bound
            // just saves saturating_add calls on a silent input.
            // A lane enters with no shift and no narrowing, so the low bits a
            // hi-res source sent reach the summed write.
            mix_into(&mut self.sum_buf[..active], &input.read_buf[..active]);
        }
        if let Some(tts) = self.tts.as_mut() {
            saturate_to_i16(&self.sum_buf, &mut self.content_meter_buf);
            tts.observe_content_period(&self.content_meter_buf);
        }
        // Apply the program-lane duck as a per-sample ramp toward the period
        // target, so a ~25 dB duck engages/releases smoothly rather than stepping
        // at the period boundary (the click/pump artifact).
        if program_target == 1.0 && self.program_duck_current == 1.0 {
            // no duck — common path, nothing to do
        } else if program_target == self.program_duck_current {
            apply_gain_to_sum(&mut self.sum_buf, self.program_duck_current);
        } else {
            self.program_duck_current = ramp_program_duck(
                &mut self.sum_buf,
                CHANNELS as usize,
                self.program_duck_current,
                program_target,
                self.program_duck_attack_step,
                self.program_duck_release_step,
            );
        }
        if let Some(tts) = self.tts.as_mut() {
            tts.mix_period(&mut self.sum_buf);
        }

        // Build the slot payload from the SAME post-duck post-TTS sum. No scale
        // change happens here — only the i64→i32 saturation and the explicit
        // little-endian order.
        fill_ring_payload(&self.sum_buf, &mut self.ring_payload);
        // Count only frames that actually ENTERED the ring — a fully-dropped
        // period (reader absent / stuck) adds nothing.
        let published_frames =
            write_ring_period(&mut self.output, &self.ring_payload, self.period_frames);
        for input in &mut self.inputs {
            if let Some(resampler) = &mut input.resampler {
                resampler.output_published(published_frames);
            }
        }
        self.frames_written
            .fetch_add(published_frames as u64, Ordering::Relaxed);
        Ok(())
    }

    /// Service TRIM at the period boundary, before the render loop. Two
    /// triggers, both funnelling through the single `trim_input` path: a
    /// control-endpoint `TRIM` that set the lane's `pending` flag, and the
    /// one-shot AUTO latch ~`AUTO_TRIM_DELAY_SECONDS` after a lane goes active.
    ///
    /// The common no-request period is one `pending` load per lane (plus, when
    /// auto-trim is enabled, one pure latch update) and nothing else.
    fn maybe_trim(&mut self) {
        for (idx, input) in self.inputs.iter_mut().enumerate() {
            // `Acquire` pairs with the control thread's `Release` store so the
            // request is observed and handled exactly once even if two `TRIM`s
            // raced in. The counters stay Relaxed — staleness across the STATUS
            // read is fine, as for every other fan-in counter.
            let manual = input.trim.pending.swap(false, Ordering::Acquire);
            if manual {
                trim_input(input);
                // A manual trim also satisfies this session's auto-trim latch —
                // no point double-trimming a lane the operator just trimmed.
                if self.auto_trim_enabled {
                    input.trim.auto_fired.store(true, Ordering::Relaxed);
                }
                // Fall through: the auto latch below still advances its
                // frames_read bookkeeping so a later idle→active re-arms.
            }

            if !self.auto_trim_enabled {
                continue;
            }

            let frames_read = input.frames_read.load(Ordering::Relaxed);
            let decision = auto_trim_decision(
                frames_read,
                self.auto_trim_lane_state[idx],
                self.auto_trim_delay_frames,
            );
            self.auto_trim_lane_state[idx] = decision.next;

            // Re-arm the one-shot guard when the lane returns to idle
            // (`active_since == None` after the update) so the next activation
            // can fire again.
            if decision.next.active_since.is_none() {
                input.trim.auto_fired.store(false, Ordering::Relaxed);
                continue;
            }

            // Fire exactly once per active session.
            if decision.fire && !input.trim.auto_fired.swap(true, Ordering::Relaxed) {
                let dropped = trim_input(input);
                info!(
                    "event=fanin.auto_trim.fired label={} dropped_frames={} \
                     delay_frames={}",
                    input.label, dropped, self.auto_trim_delay_frames,
                );
            }
        }
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
fn write_ring_period(ring: &mut RingOutput, payload: &[u8], period_frames: u32) -> u32 {
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
        match event {
            RingStallEvent::Detected { .. } | RingStallEvent::Unrecovered { .. } => {
                warn!("{}", format_ring_stall_event(&event));
            }
            RingStallEvent::Cleared { .. } => {
                info!("{}", format_ring_stall_event(&event));
            }
        }
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

impl Input {
    /// Count one xrun on this lane and stamp when it happened, returning the new
    /// cumulative count. The two gauges move together so `last_xrun_age_ms` can
    /// never age out of step with `xrun_count`.
    pub fn note_xrun(&self) -> u64 {
        self.last_xrun_ms.store(event_stamp_ms(), Ordering::Relaxed);
        self.xrun_count.fetch_add(1, Ordering::Relaxed) + 1
    }

    /// The lane's resampler observability handles for STATUS, or `None` when no
    /// resampler is armed on this lane.
    pub fn resampler_observability(&self) -> Option<LaneResamplerObservability> {
        self.resampler.as_ref().map(|r| r.observability())
    }

    /// The lane's USB DIRECT observability handles for the STATUS `direct{}`
    /// block, or `None` when this is not the direct lane.
    pub fn direct_observability(&self) -> Option<DirectObservability> {
        self.direct_obs.clone()
    }

    /// Whether this lane is the USB DIRECT lane.
    pub fn is_direct(&self) -> bool {
        self.direct.is_some()
    }

    /// This lane's transport — the ONE derivation of STATUS's `source` field.
    ///
    /// Read off the lane-source `Option` itself rather than a separate stored
    /// flag, so "which arm of the read dispatch does this lane take" and "what
    /// does `/state` say it takes" cannot disagree — `step()` matches on the
    /// same `Option`.
    pub fn lane_source(&self) -> LaneSource {
        match (&self.direct, &self.pcm) {
            (Some(_), _) => LaneSource::Direct,
            (None, Some(_)) => LaneSource::Lane,
            (None, None) => LaneSource::Disabled,
        }
    }

    /// The lane's shared TRIM control + counters, cloned for the state-server
    /// thread, which may set `pending` but never touches the resampler itself.
    pub fn trim_control(&self) -> Arc<TrimControl> {
        Arc::clone(&self.trim)
    }

    /// The lane's shared MIX-MUTE flag, cloned for the state-server thread. The
    /// work loop reads it at the SUM stage via `lane_mix_contributes`.
    pub fn muted_flag(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.muted)
    }
}

fn input_selected(selected_input: i32, input_index: usize, label: &str) -> bool {
    selected_input == input_index as i32 || label == MEASUREMENT_LANE
}

/// Pure per-lane MIX-contribution decision: a lane's freshly-read samples are
/// wanted in the sum this period iff it is selected AND not muted.
///
/// This is the WANT, not the gate. `mixer::lane_fade` consumes it and carries
/// the lane to or from that state across a 10 ms window, so a lane this returns
/// `false` for still reaches the sum, decaying, until its window closes.
///
/// The mute is mux's latest-source-wins arbitration primitive for the USB lane —
/// the only USB-silencing mechanism — and is applied at the SUM ONLY: the caller
/// stores this lane's `rms_dbfs_x100` and bumps `frames_read` BEFORE consulting
/// this gate, so a muted lane still reports its true captured level and liveness
/// to STATUS. mux depends on that decoupling: it reads the USB DIRECT lane's
/// pre-mute level/frames to keep seeing a muted-but-streaming host as "playing",
/// so silencing the lane never makes mux think the host stopped (which would
/// flap mute→release→mute).
///
/// Takes only values (no atomics, no side effects) so the decision is
/// exhaustively unit-testable without ALSA and can never perturb telemetry.
fn lane_mix_contributes(selected_input: i32, input_index: usize, label: &str, muted: bool) -> bool {
    input_selected(selected_input, input_index, label) && !muted
}

/// Pure decision for the bounded catch-up resync: given the frames a lane
/// currently has readable (`avail`) and the period size, how many WHOLE
/// periods should be discarded to bring the ring down to `CATCHUP_TARGET_PERIODS`?
///
/// Returns 0 unless `avail` exceeds `CATCHUP_HIGH_WATER_PERIODS` — so a
/// healthy DAC-locked lane (ring ~1 period) never drains. When it does fire:
///   - WHOLE periods only — discarding a fractional period would shear the
///     stream and desync this lane from its siblings in the per-period sum.
///   - Leaves at least `CATCHUP_TARGET_PERIODS` readable, so the immediately
///     following normal read in `step()` still gets a full period (the
///     resync never induces an underrun).
///   - Capped at `CATCHUP_MAX_DRAIN_PERIODS` so a bogus `avail` can't spin
///     the hot loop on syscalls.
///
/// Pure (no ALSA) for unit testability — `drain_input_excess` does the I/O.
fn catchup_drain_periods(avail: i64, period_frames: i64) -> i64 {
    // PANIC-AUDITED: period_frames is the daemon's fixed period-size config, not external input
    debug_assert!(period_frames > 0);
    let high_water = period_frames * CATCHUP_HIGH_WATER_PERIODS;
    if avail <= high_water {
        return 0;
    }
    let target = period_frames * CATCHUP_TARGET_PERIODS;
    // avail > high_water >= target ⇒ (avail - target) > 0.
    let excess_periods = (avail - target) / period_frames; // floor
    excess_periods.min(CATCHUP_MAX_DRAIN_PERIODS)
}

/// Pure decision for the "armed but lane label not found" no-op warning.
///
/// Returns `Some(available_labels_csv)` when the resampler is ENABLED but its
/// configured `lane_label` matches NONE of the live `input_labels` — the state
/// in which the feature silently does nothing because `Mixer::new` constructs no
/// `LaneResampler`. The CSV is the "here are the labels you could have meant"
/// hint. Pure (no ALSA) so the warning decision is unit-testable.
fn resampler_lane_not_found(
    enabled: bool,
    lane_label: &str,
    input_labels: &[String],
) -> Option<String> {
    if !enabled {
        return None;
    }
    if input_labels.iter().any(|l| l == lane_label) {
        return None;
    }
    Some(input_labels.join(","))
}

/// Resolve the input resampler's burst-ring capacity (frames) from the scalar
/// knobs.
///
/// `requested` is the explicit `input_resampler_ring_frames` env override
/// (non-zero pins it) OR, when `0`, twice the lane's ALSA
/// `input_buffer_frames`. The 2x derived default is deliberate: hardware USB
/// testing showed a 4096-frame ring could stay locked but still overrun on
/// snd-aloop burst arrivals, while an 8192-frame ring absorbed the same bursts
/// without adding steady latency (the hold target controls latency; ring
/// capacity is just headroom). The result is floored to the resampler's
/// STRUCTURAL minimum (`target + warm-up cushion + period + radius + 1`) so a
/// tiny configured value can never make `LaneResampler::new` reject the ring.
///
/// Pure over primitives (no ALSA, no `Config`) so it is unit-testable.
fn resampler_ring_frames(
    requested_ring_frames: u32,
    input_buffer_frames: u32,
    target_frames: u32,
    warmup_cushion_frames: u32,
    period_frames: u32,
) -> usize {
    let radius = jasper_resampler::RADIUS_FRAMES as usize;
    let min_ring = target_frames as usize
        + warmup_cushion_frames as usize
        + period_frames as usize
        + radius
        + 1;
    let requested = if requested_ring_frames > 0 {
        requested_ring_frames as usize
    } else {
        (input_buffer_frames as usize).saturating_mul(2)
    };
    requested.max(min_ring)
}

/// Build the per-input resampler for the clock-crossing lane, or `None` on a
/// construction failure (logged and degraded past — the lane then runs the
/// catch-up fallback). Sizes the resampler's input ring for burst headroom and
/// holds a warm-up cushion above the base target during acquisition/steady
/// state (see `lane_resampler.rs`).
fn build_lane_resampler(label: &str, config: &Config) -> Option<LaneResampler> {
    let ring_frames = resampler_ring_frames(
        config.input_resampler_ring_frames,
        config.input_buffer_frames,
        config.input_resampler_target_frames,
        config.input_resampler_warmup_cushion_frames,
        config.period_frames,
    );
    let cushion = config.input_resampler_warmup_cushion_frames as usize;
    let target = config.input_resampler_target_frames as usize;
    // The decay knobs are validated fail-loud in Config::from_env; the lane
    // derives the ceiling (target + cushion) and the render-period intervals.
    let decay_params = crate::lane_resampler::DecayParams {
        enabled: config.input_resampler_cushion_decay_enabled,
        floor_frames: config.input_resampler_cushion_decay_floor_frames as u64,
        stability_ms: CUSHION_DECAY_STABILITY_MS,
    };
    match LaneResampler::new(
        CHANNELS as usize,
        config.period_frames,
        config.sample_rate,
        target,
        cushion,
        config.input_resampler_max_adjust_ppm as f64,
        ring_frames,
        decay_params,
    ) {
        Ok(r) => {
            // Keep this event name and its lane / base target / held target /
            // max-ppm fields STABLE: jasper-trace and the docs point at them.
            let held_target = target + cushion;
            // When decay is armed, the held target above is the acquisition
            // CEILING it decays FROM toward the floor once locked + DLL-l0 +
            // stable.
            let decay_note = if config.input_resampler_cushion_decay_enabled {
                format!(
                    "decay=on floor={}",
                    config.input_resampler_cushion_decay_floor_frames
                )
            } else {
                "decay=off".to_string()
            };
            info!(
                "event=fanin.resampler.armed lane={} target_frames={} held_target_frames={} \
                 warmup_cushion_frames={} max_adjust_ppm={} ring_frames={} {} \
                 (DLL-steered to DAC clock; catch-up drain bypassed on this lane)",
                label,
                target,
                held_target,
                cushion,
                config.input_resampler_max_adjust_ppm,
                ring_frames,
                decay_note,
            );
            Some(r)
        }
        Err(e) => {
            warn!(
                "event=fanin.resampler.noop reason=construction_failed lane={} detail={} — \
                 falling back to catch-up drain on this lane",
                label, e,
            );
            None
        }
    }
}

/// Shared taxonomy for every non-blocking ALSA read/query in this mixer.
/// Callers decide whether a fatal error propagates (ordinary lanes) or means a
/// hot-pluggable device disappeared (USB direct); the errno classification is
/// identical in both cases.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PcmIoFate {
    /// `EAGAIN` — no data ready right now; stop draining this period.
    WouldBlock,
    /// `EPIPE`/`ESTRPIPE` — an overrun; recover the PCM and reset the resampler.
    Xrun,
    /// Any other errno (ENODEV on unplug, etc.) — the device is gone; go Absent.
    Fatal,
}

fn classify_pcm_errno(errno: i32) -> PcmIoFate {
    if errno == libc::EAGAIN {
        PcmIoFate::WouldBlock
    } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
        PcmIoFate::Xrun
    } else {
        PcmIoFate::Fatal
    }
}

/// The nominal sample rate the direct lane opens at, named so the open envelope
/// and the pure validator agree. Distinct from `config.sample_rate` on purpose:
/// the gadget capture is a FIXED 48 kHz endpoint, not a configurable fan-in knob.
const SAMPLE_RATE_HZ: u32 = 48_000;

/// Read and throw away up to `periods` whole periods, returning the frames
/// discarded. On non-blocking capture, `readi` returns `Err(EAGAIN)` the instant
/// the ring drops below one period — that `Err` arm is the normal early stop;
/// `Ok(0)` is a defensive guard. The `0..periods` bound means it can never spin
/// regardless. Generic over the sample type so the lane's width picks the IO
/// handle and the scratch together.
fn discard_periods<S: Copy>(io: &IO<'_, S>, scratch: &mut [S], periods: i64) -> u64 {
    let mut discarded: u64 = 0;
    for _ in 0..periods {
        match io.readi(scratch) {
            Ok(0) => break,
            Ok(n) => discarded += n as u64,
            Err(_) => break,
        }
    }
    discarded
}

/// Bounded per-input catch-up resync. Called once per lane per period,
/// BEFORE the normal `read_input`.
///
/// Every lane is read exactly one period per work-loop iteration, and the loop
/// is paced by the local DAC clock. A lane whose producer is clocked off the
/// *same* DAC (every networked renderer: AirPlay / Spotify / Bluetooth / TTS)
/// keeps its capture ring at ~one period forever — it cannot outrun a consumer
/// on its own clock. The USB lane is different: its producer is the host clock,
/// and the gadget's async feedback tracks the snd-aloop jiffies timer rather
/// than the DAC, so a small residual rate gap accumulates. With a strict
/// one-period read and no catch-up, that excess never drains — the ring fills
/// monotonically until it overruns, by which point the *upstream* usbsink
/// producer queue has already overflowed because back-pressure never reached the
/// host.
///
/// This drains the excess down to one period when a lane's readable backlog
/// crosses the high-water. A DAC-locked lane sits at one period and this is a
/// single non-blocking `avail_update` — no reads, no effect.
///
/// Drop-CONTROLLED, not drop-FREE: a backed-up lane loses a few ms of audio at
/// each resync, traded against a cascading upstream overflow. This does NOT
/// resample.
///
/// RT-safety: no allocation (discards into the lane's existing `read_buf`
/// scratch) and no blocking (`avail_update` is a non-blocking query; the discard
/// `readi` only ever reads frames it already reported ready). Discard reads are
/// capped per call (`CATCHUP_MAX_DRAIN_PERIODS`), and the log is count-gated, so
/// the common no-resync path touches no clock and emits nothing.
fn drain_input_excess(input: &mut Input, period_frames: usize) {
    // `None` on the DISABLED USB lane (direct off), which takes this arm every
    // period with nothing to drain. Every aloop lane has Some(pcm), and the
    // direct lane routes to read_direct_and_render instead.
    let Some(pcm) = input.pcm.as_ref() else {
        return;
    };
    // An error here means "no usable reading right now" — leave recovery to the
    // normal read_input path; never block or panic.
    let avail = match pcm.avail_update() {
        Ok(a) => a,
        Err(_) => return,
    };
    let to_drain = catchup_drain_periods(avail, period_frames as i64);
    if to_drain == 0 {
        return; // healthy lane — the overwhelmingly common path.
    }

    // Discard whole periods into the lane's existing period scratch. read_input
    // overwrites the same buffer next, so trashing it here is safe.
    let discarded_frames = match pcm.io_i32() {
        Ok(io) => discard_periods(&io, &mut input.read_buf, to_drain),
        Err(_) => return,
    };
    if discarded_frames == 0 {
        return;
    }

    input
        .catchup_resync_frames
        .fetch_add(discarded_frames, Ordering::Relaxed);
    let events = input.catchup_events.fetch_add(1, Ordering::Relaxed) + 1;
    if events == 1 || events % CATCHUP_LOG_EVERY == 0 {
        warn!(
            "event=fanin.input.catchup label={} discarded_frames={} avail_frames={} \
             target_frames={} events={} total_resync_frames={} \
             (free-running lane drop-resync; not drop-free)",
            input.label,
            discarded_frames,
            avail,
            period_frames * (CATCHUP_TARGET_PERIODS as usize),
            events,
            input.catchup_resync_frames.load(Ordering::Relaxed),
        );
    }
}

/// Perform ONE lock-preserving TRIM on `input`: drop the lane's standing
/// latency down to the resampler's held target by discarding the OLDEST
/// buffered input, keeping the newest and keeping lock. Returns the number of
/// frames dropped (0 when the lane has no armed resampler, is unlocked, or is
/// already at/below its held target — never panics, never blocks, never does
/// ALSA I/O).
///
/// The standing head-start does NOT live in the ALSA readable backlog on an
/// armed lane: `drain_direct_capture` already drains every frame ALSA reports
/// ready each period, so the kernel ring is held shallow by design. The
/// reservoir is the resampler's CURSOR-RELATIVE fill (`write_frame -
/// next_input_frame`) — observed on-device at ~1919 frames against a 512-frame
/// held target with lock churn. This trim drops THAT in place via
/// [`LaneResampler::trim_ring`]: the cursor skips forward over the oldest
/// buffered frames (one discontinuity at the skip) while lock and the DLL loop
/// state survive, so it costs no unlock/reprime churn.
///
/// An UNARMED lane has no such userspace reservoir — its standing fill would be
/// the ALSA backlog, which the catch-up drain already bounds — so TRIM there is
/// a 0-frame no-op. It still clears its `pending` flag so the control command
/// completes cleanly.
///
/// RT-safety: pure host-memory work inside the resampler (one fill compute, one
/// cursor advance, one `drop_before`), no syscalls, no allocation, no blocking.
/// Runs on the WORK thread, which OWNS the mixer's `LaneResampler` — never on
/// the state-server thread.
fn trim_input(input: &mut Input) -> u64 {
    let dropped = match input.resampler.as_mut() {
        Some(r) => r.trim_ring(),
        // No resampler on this lane: no standing-fill reservoir to trim.
        None => 0,
    };
    if dropped == 0 {
        return 0;
    }
    let trims = input.trim.trims.fetch_add(1, Ordering::Relaxed) + 1;
    let total = input
        .trim
        .trimmed_frames
        .fetch_add(dropped, Ordering::Relaxed)
        + dropped;
    // Trims are operator/auto events, not per-period, so this needs no spam gate.
    info!(
        "event=fanin.trim label={} dropped_ring_frames={} trims={} total_trimmed_frames={}",
        input.label, dropped, trims, total,
    );
    dropped
}

/// The mixer-thread side of the impulse tap. Holds the shared [`TapState`]
/// (armed + detector knobs, read lock-free), the last-armed [`TapConfig`] (read
/// only on an arm-generation change), the bounded channel to the
/// `fanin-tap-writer` thread, and the mixer-local detector state + cumulative
/// capture cursor. Runs inline in `read_direct_and_render` BEFORE `push_input`,
/// over an S16 view narrowed for the detector alone.
///
/// Disarmed cost: one relaxed atomic load per direct read
/// ([`TapState::armed`]) and nothing else.
pub struct DirectTapHook {
    state: Arc<TapState>,
    config: Arc<Mutex<TapConfig>>,
    sender: SyncSender<TapEvent>,
    /// The mixer-thread-local detector, rebuilt on each arm generation.
    detector: Option<ImpulseDetector>,
    last_generation: u64,
    /// Cumulative direct-capture frames read BEFORE the current read (the
    /// detector's `read_start_frame`), so refractory anchoring is stable across
    /// reads of any size.
    capture_frames_cursor: u64,
}

impl DirectTapHook {
    fn new(
        state: Arc<TapState>,
        config: Arc<Mutex<TapConfig>>,
        sender: SyncSender<TapEvent>,
    ) -> Self {
        Self {
            state,
            config,
            sender,
            detector: None,
            last_generation: 0,
            capture_frames_cursor: 0,
        }
    }

    /// Clone the shared state + config for the state-server/writer threads.
    fn state(&self) -> Arc<TapState> {
        Arc::clone(&self.state)
    }

    fn config(&self) -> Arc<Mutex<TapConfig>> {
        Arc::clone(&self.config)
    }

    /// Run the tap over one converted S16 read, BEFORE it enters the resampler.
    /// Reloads the detector only on a fresh arm generation, timestamps with the
    /// caller's post-read `read_ns`, and non-blocking `try_send`s the event
    /// (drop-and-count on Full). Only called from the armed branch; the disarmed
    /// fast path is the caller's `state.armed()` check.
    ///
    /// - `converted`: the S16 slice just narrowed from S32 (this read only).
    /// - `read_frames`: frames in this read.
    /// - `read_ns`: `CLOCK_MONOTONIC` ns taken immediately after `readi`.
    /// - `ring_fill_frames`: the lane resampler fill BEFORE `push_input`,
    ///   recorded (as frames) for the JSONL diagnostic field.
    fn tap_over_read(
        &mut self,
        converted: &[i16],
        read_frames: usize,
        read_ns: i128,
        ring_fill_frames: u64,
    ) {
        let generation = self.state.generation_acquire();
        if self.detector.is_none() || generation != self.last_generation {
            let (threshold, hysteresis, refractory_frames) = self.state.detector_knobs();
            self.detector = Some(ImpulseDetector::new(
                threshold,
                hysteresis,
                refractory_frames,
                CHANNELS as usize,
            ));
            self.last_generation = generation;
        }
        let Some(detector) = self.detector.as_mut() else {
            return;
        };
        let Some(hit) = detector.detect(
            &converted[..read_frames * (CHANNELS as usize)],
            self.capture_frames_cursor,
        ) else {
            return;
        };
        let event = TapEvent {
            monotonic_ns: crate::impulse_tap::detection_monotonic_ns(
                read_ns,
                read_frames,
                hit.sample_offset_frames,
                SAMPLE_RATE_HZ,
            ),
            frame_index: self
                .capture_frames_cursor
                .saturating_add(hit.sample_offset_frames as u64),
            ring_fill_frames,
            peak: hit.peak,
        };
        send_drop_counted(&self.sender, event, || self.state.note_dropped());
    }
}

/// `CLOCK_MONOTONIC` in nanoseconds — the direct tap's ingress timeline. The
/// tap and the Python mic harness both read `CLOCK_MONOTONIC` on the same Pi;
/// that shared timeline is the only reason their cross-process subtraction is
/// valid. On a syscall failure this returns 0 rather than crashing the work
/// loop; a stray 0-anchored event is dropped by the harness's pairing window.
fn monotonic_ns() -> i128 {
    let mut ts = MaybeUninit::<libc::timespec>::uninit();
    let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, ts.as_mut_ptr()) };
    if rc != 0 {
        return 0;
    }
    let ts = unsafe { ts.assume_init() };
    (ts.tv_sec as i128) * 1_000_000_000 + (ts.tv_nsec as i128)
}

/// `CLOCK_MONOTONIC` milliseconds for a `NEVER_MS`-sentinel recency stamp,
/// on the same timeline `state` ages it against.
pub(crate) fn event_stamp_ms() -> u64 {
    jasper_ring::monotonic_ns() / 1_000_000
}

/// Read up to `requested_frames` from `input`. Returns the number of
/// frames actually read (may be less than requested if the kernel
/// has less ready, or 0 if non-blocking and no data).
///
/// Failure modes handled in-band:
///   - `EAGAIN` (no data right now): substitute silence; return 0.
///   - `EPIPE` / `ESTRPIPE` (overrun): `try_recover`, log, substitute
///     silence; return 0.
///
/// All other errors propagate up — they indicate a structural
/// problem (PCM closed, driver fault) that the daemon can't handle
/// at this layer.
fn read_input(input: &mut Input, requested_frames: usize) -> Result<usize> {
    // `None` on the DISABLED USB lane (direct off), which takes this arm every
    // period and renders silence. Every aloop lane has Some(pcm), and the
    // direct lane never reaches this path.
    let Some(pcm) = input.pcm.as_ref() else {
        input.read_buf.fill(0);
        return Ok(0);
    };
    // The PCM was opened S32_LE (`configure_pcm`), so the typed IO handle can
    // never disagree with the wire.
    let read = {
        let io = pcm.io_i32().context("getting i32 IO handle for input")?;
        io.readi(&mut input.read_buf)
    };
    match read {
        Ok(frames) => {
            input
                .frames_read
                .fetch_add(frames as u64, Ordering::Relaxed);
            // Zero the tail on a short read: the sum loop bounds its region by
            // `frames`, but a path that reads the whole buffer (RMS, the fade
            // tracker) must see zeros rather than stale data there.
            if frames < requested_frames {
                input.read_buf[frames * (CHANNELS as usize)..].fill(0);
            }
            Ok(frames)
        }
        Err(e) => match classify_pcm_errno(e.errno()) {
            PcmIoFate::WouldBlock => {
                // No data ready: the renderer is idle, or has not opened its
                // substream yet.
                input.read_buf.fill(0);
                Ok(0)
            }
            PcmIoFate::Xrun => {
                // Overrun: the renderer produced faster than we drained.
                let count = input.note_xrun();
                warn!(
                    "event=fanin.xrun source=input label={} count={}",
                    input.label, count,
                );
                pcm.try_recover(e, true).context("recovering input xrun")?;
                input.read_buf.fill(0);
                Ok(0)
            }
            PcmIoFate::Fatal => Err(e).context(format!(
                "reading from input {} ({})",
                input.label, input.pcm_name
            )),
        },
    }
}

/// Cap on period-equivalent work for the DIRECT lane drain in one `step()` call.
/// Like `CATCHUP_MAX_DRAIN_PERIODS`, this bounds syscall work per period so a
/// pathological `avail` (driver fault) cannot spin the hot loop. Frames beyond
/// the cap stay in the kernel ring and are read next period — the resampler's
/// own ring is the rate buffer, so leaving a little behind is harmless.
const RESAMPLER_MAX_READ_PERIODS: i64 = 64;

/// Return the bounded number of currently readable frames that the direct-lane
/// drain should pull into the resampler this period. Pure helper for the
/// real-time cap math; ALSA I/O happens in `drain_direct_capture`.
fn resampler_read_budget_frames(avail: Frames, period_frames: usize) -> usize {
    if avail <= 0 {
        return 0;
    }
    let max_frames = period_frames.saturating_mul(RESAMPLER_MAX_READ_PERIODS as usize);
    (avail as usize).min(max_frames)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Lane gates, drain and PCM error classification, the ring publish path,
    // stall, pacing and trim. The pure mix math has its tests in dsp.rs and the
    // ALSA open helpers theirs in pcm_open.rs.
    //
    // The per-lane RMS level helper (`rms_dbfs_i16` / `RMS_DBFS_FLOOR`) is now
    // shared from jasper-resampler; its pure-math vectors live in that crate's
    // suite. The combo-gate integration behaviour is still asserted here (and in
    // tests/test_usbsink_playing_rms_contract.py) via the level plumbed onto the
    // lane and the STATUS surface.

    // ---- Lane transport tokens -------------------------------------------

    #[test]
    fn lane_source_tokens_are_the_status_vocabulary() {
        // Read cross-language (jasper/fanin/status.py), so the spellings are
        // the contract; which lane gets which is pinned in config.rs.
        assert_eq!(LaneSource::Lane.as_str(), "lane");
        assert_eq!(LaneSource::Direct.as_str(), "direct");
        assert_eq!(LaneSource::Disabled.as_str(), "disabled");
    }

    // ---- Per-lane mix gate: selection + mute -----------------------------

    #[test]
    fn lane_mix_contributes_selection_only_when_unmuted() {
        // Single-select: only the selected index contributes.
        assert!(lane_mix_contributes(2, 2, "usbsink", false));
        assert!(!lane_mix_contributes(2, 0, "spotify", false));
        // NONE (-2): no lane contributes (except correction, below).
        assert!(!lane_mix_contributes(-2, 2, "usbsink", false));
        // The correction/test lane always passes selection.
        assert!(lane_mix_contributes(-2, 3, "correction", false));
    }

    #[test]
    fn lane_mix_contributes_mute_overrides_selection() {
        // A muted lane never contributes, no matter how it is selected — this is
        // the arbitration primitive: even an explicitly-selected USB lane is
        // silenced when mux mutes it.
        assert!(!lane_mix_contributes(2, 2, "usbsink", true));
        assert!(!lane_mix_contributes(-2, 2, "usbsink", true));
        // Mute wins even over the always-pass correction lane, so the primitive
        // is total (mux only ever mutes usbsink, but the rule is not lane-special).
        assert!(!lane_mix_contributes(-2, 3, "correction", true));
    }

    #[test]
    fn lane_mix_contributes_is_side_effect_free() {
        // The gate takes only values — it CANNOT touch a lane's rms_dbfs_x100 or
        // frames_read. That is the structural guarantee behind the telemetry-
        // stays-pre-mute invariant: the caller stores telemetry for EVERY lane
        // before this decision, so muting a lane silences the sum without
        // perturbing the level/liveness mux reads. Calling it twice with the same
        // inputs yields the same answer with nothing observable changed.
        assert_eq!(
            lane_mix_contributes(2, 2, "usbsink", true),
            lane_mix_contributes(2, 2, "usbsink", true),
        );
        assert!(lane_mix_contributes(2, 2, "usbsink", false));
    }

    // ---- USB DIRECT pure helpers -----------------------------------------

    #[test]
    fn drain_avail_bucket_boundaries() {
        // 64-frame step buckets: [0,64) [64,128) [128,192) [192,256) [256,320) [320,+)
        assert_eq!(drain_avail_bucket(-5), 0); // negative clamps into bucket 0
        assert_eq!(drain_avail_bucket(0), 0);
        assert_eq!(drain_avail_bucket(63), 0);
        assert_eq!(drain_avail_bucket(64), 1);
        assert_eq!(drain_avail_bucket(127), 1);
        assert_eq!(drain_avail_bucket(128), 2);
        assert_eq!(drain_avail_bucket(191), 2);
        assert_eq!(drain_avail_bucket(192), 3);
        assert_eq!(drain_avail_bucket(255), 3);
        assert_eq!(drain_avail_bucket(256), 4);
        assert_eq!(drain_avail_bucket(319), 4);
        assert_eq!(drain_avail_bucket(320), 5);
        assert_eq!(drain_avail_bucket(100_000), 5); // saturates in top bucket
                                                    // The measured ~186-frame standing dwell lands in bucket 2 ([128,192)).
        assert_eq!(drain_avail_bucket(186), 2);
    }

    #[test]
    fn drain_stats_record_accumulates() {
        let stats = DrainStats::new();
        // Record three samples across three buckets.
        assert_eq!(stats.record(64), 1); // bucket 1
        assert_eq!(stats.record(186), 2); // bucket 2
        assert_eq!(stats.record(320), 3); // bucket 5
        assert_eq!(stats.count.load(Ordering::Relaxed), 3);
        assert_eq!(stats.sum.load(Ordering::Relaxed), 64 + 186 + 320);
        assert_eq!(stats.max.load(Ordering::Relaxed), 320);
        assert_eq!(stats.hist[1].load(Ordering::Relaxed), 1);
        assert_eq!(stats.hist[2].load(Ordering::Relaxed), 1);
        assert_eq!(stats.hist[5].load(Ordering::Relaxed), 1);
        // Untouched buckets stay 0.
        assert_eq!(stats.hist[0].load(Ordering::Relaxed), 0);
        // A negative avail records as 0 into bucket 0 and does not raise max.
        stats.record(-1);
        assert_eq!(stats.hist[0].load(Ordering::Relaxed), 1);
        assert_eq!(stats.max.load(Ordering::Relaxed), 320);
        assert_eq!(stats.sum.load(Ordering::Relaxed), 64 + 186 + 320);
    }

    #[test]
    fn classify_pcm_errno_maps_c1_fates() {
        assert_eq!(classify_pcm_errno(libc::EAGAIN), PcmIoFate::WouldBlock);
        assert_eq!(classify_pcm_errno(libc::EPIPE), PcmIoFate::Xrun);
        assert_eq!(classify_pcm_errno(libc::ESTRPIPE), PcmIoFate::Xrun);
        // ENODEV (and any other structural errno) is caller-specific: the
        // hot-pluggable direct lane parks, while ordinary lanes propagate it.
        assert_eq!(classify_pcm_errno(libc::ENODEV), PcmIoFate::Fatal);
        assert_eq!(classify_pcm_errno(libc::EIO), PcmIoFate::Fatal);
    }

    // ---- Bounded tap channel (must never block the SCHED_FIFO work loop) ----

    #[test]
    fn send_drop_counted_counts_a_full_channel_and_a_gone_writer_alike() {
        // Nothing draining: the bound-2 channel fills on the first two sends,
        // so the third must drop-and-count rather than block this (the
        // calling) thread. Then the receiver goes, which is what a writer
        // thread that could not open its artifact leaves behind — every event
        // is lost from there on, and the gauge is the only place that shows it.
        let (tx, rx) = std::sync::mpsc::sync_channel::<TapEvent>(2);
        let dropped = AtomicU64::new(0);
        let event = || TapEvent {
            monotonic_ns: 1,
            frame_index: 2,
            ring_fill_frames: 3,
            peak: 0.5,
        };
        let bump = || {
            dropped.fetch_add(1, Ordering::Relaxed);
        };
        send_drop_counted(&tx, event(), bump);
        send_drop_counted(&tx, event(), bump);
        send_drop_counted(&tx, event(), bump);
        assert_eq!(dropped.load(Ordering::Relaxed), 1);

        drop(rx);
        send_drop_counted(&tx, event(), bump);
        assert_eq!(dropped.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn direct_reopen_cadence_is_about_two_seconds() {
        // 375 periods × 256 frames / 48000 Hz = 2.0 s.
        let seconds = (DIRECT_REOPEN_RETRY_PERIODS as f64) * (DIRECT_PERIOD_FRAMES as f64)
            / (SAMPLE_RATE_HZ as f64);
        assert!(
            (seconds - 2.0).abs() < 1e-9,
            "reopen cadence must be ~2 s, got {seconds}"
        );
    }

    #[test]
    fn zombie_handle_suspected_fires_only_at_threshold() {
        // Frames have flowed on this handle (the normal case a real gadget rebuild
        // hits): a zero-avail run below the threshold is not yet a zombie.
        assert!(!zombie_handle_suspected(
            true,
            0,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(!zombie_handle_suspected(
            true,
            1,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(!zombie_handle_suspected(
            true,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS - 1,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        // At or beyond the threshold, with frames having flowed: the handle went
        // deaf after feeding the lane — a zombie.
        assert!(zombie_handle_suspected(
            true,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(zombie_handle_suspected(
            true,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS + 100,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        // A zero threshold disables the detector (belt-and-braces: never fire).
        assert!(!zombie_handle_suspected(true, u64::MAX, 0));
    }

    #[test]
    fn zombie_handle_never_fires_on_attached_idle_host() {
        // An ordinary attached-but-silent host (wired 24/7, music paused or
        // asleep) streams avail≈0 drains forever with frames NEVER having flowed
        // on the handle. The flowing→dead gate MUST hold the detector off no
        // matter how long the zero-avail streak grows, or the box churns a
        // reopen + WARN every ~2 s with no gadget rebuild.
        // No amount of accumulated zero-avail can trip while frames_flowed=false:
        assert!(!zombie_handle_suspected(
            false,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(!zombie_handle_suspected(
            false,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS * 1000,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(!zombie_handle_suspected(
            false,
            u64::MAX,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        // The distinguishing pair: same streak at threshold, only the flowed latch
        // differs — idle (never fed) stays quiet, real-rebuild (fed then dead) fires.
        assert!(!zombie_handle_suspected(
            false,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(zombie_handle_suspected(
            true,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
    }

    #[test]
    fn zombie_zero_avail_window_is_about_two_seconds() {
        // The zombie detection window matches the reopen cadence (~2 s at the
        // default 256/48k period) — enough dead time to be sure the gadget stopped
        // feeding, not a transient.
        let seconds = (DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS as f64) * (DIRECT_PERIOD_FRAMES as f64)
            / (SAMPLE_RATE_HZ as f64);
        assert!(
            (seconds - 2.0).abs() < 1e-9,
            "zombie window must be ~2 s, got {seconds}"
        );
    }

    // ---- direct.health classifier (capture recovery observability) ------------

    #[test]
    fn direct_health_broken_only_on_flowing_then_dead() {
        // The Broken classification IS the zombie signature: frames flowed on this
        // handle, then it went deaf for >= the threshold. It must classify exactly
        // when a real capture break happened (not idle, not merely absent).
        assert_eq!(
            direct_health(
                true,
                true,
                DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
                DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
            ),
            DirectHealth::Broken
        );
        // Below threshold with flow: still capturing, not yet broken.
        assert_eq!(
            direct_health(
                true,
                true,
                DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS - 1,
                DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
            ),
            DirectHealth::Capturing
        );
    }

    #[test]
    fn direct_health_idle_host_and_unplug_never_broken() {
        // The binding constraint: an attached-but-silent Mac (present, never flowed,
        // avail≈0 streak growing forever) and a fully unplugged host (not present)
        // are IDLE, never Broken. This mirrors
        // zombie_handle_never_fires_on_attached_idle_host at the health layer.
        // Attached-idle: present, never flowed, huge zero-avail streak.
        assert_eq!(
            direct_health(true, false, u64::MAX, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS),
            DirectHealth::Idle
        );
        // Unplugged host / (re)opening: not present. A Mac unplug is an idle
        // transition, not a failure — even if frames had flowed before (the latch
        // resets to false on going Absent, so this input shape is what the lane
        // actually presents).
        assert_eq!(
            direct_health(false, false, 0, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS),
            DirectHealth::Idle
        );
        assert_eq!(
            direct_health(false, false, u64::MAX, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS),
            DirectHealth::Idle
        );
    }

    #[test]
    fn direct_health_capturing_when_present_and_flowing() {
        // Present + flowed + not deaf = actively capturing (the steady healthy state
        // while a host streams music).
        assert_eq!(
            direct_health(true, true, 0, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS),
            DirectHealth::Capturing
        );
        // Present + not-yet-flowed (host attached, about to stream) = idle, not
        // capturing.
        assert_eq!(
            direct_health(true, false, 0, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS),
            DirectHealth::Idle
        );
    }

    #[test]
    fn direct_health_str_tokens_are_stable() {
        // The Python jasper.fanin.status reader matches these exact tokens.
        assert_eq!(direct_health_str(DirectHealth::Capturing), "capturing");
        assert_eq!(direct_health_str(DirectHealth::Idle), "idle");
        assert_eq!(direct_health_str(DirectHealth::Broken), "broken");
    }

    // ---- handle-liveness probe / card-generation signal ------------------

    #[test]
    fn liveness_probe_dead_when_ioctl_errored() {
        // `None` = the `snd_pcm_status` ioctl ITSELF returned Err. On a rebuilt or
        // disconnected card that is `-ENODEV`; any query error likewise means the
        // handle cannot be confirmed live. Fail toward the bounded reopen (safe: a
        // reopen onto a healthy handle is a cheap no-op re-establish). This is the
        // routine post-deploy shape — a fresh handle that never flowed a frame,
        // rebuilt underneath us — that `avail_update`'s frozen mmap page cannot see.
        assert!(liveness_probe_dead(None));
    }

    #[test]
    fn liveness_probe_dead_when_state_disconnected() {
        // The kernel explicitly reports the stream Disconnected under the open
        // handle → dead → force the bounded reopen.
        assert!(liveness_probe_dead(Some(State::Disconnected)));
    }

    #[test]
    fn liveness_probe_alive_on_every_live_state() {
        // Any live state means the handle still refers to a live kernel object, so
        // the probe must NOT trip. This is the attached-idle safety property the
        // signal is built around: an idle-but-attached Mac keeps the capture stream
        // in a live state (Prepared/Running) no matter how long it sits silent, so
        // the probe stays quiet WITHOUT any frames-flowed gate — an idle host cannot
        // make a live handle report Disconnected. Enumerated (not a loop over one
        // value) so a future kernel/state addition that should be treated as live is
        // a conscious edit here.
        //
        // That the STATUS ioctl actually returns Err/Disconnected across a real gadget rebuild
        // (vs `avail_update` continuing to return Ok(0)) is kernel behavior no unit test can
        // pin; it is the on-device obligation — `curl .../state | jq
        // .fanin.inputs[].direct.card_gen_reopens` must tick across a `systemctl
        // restart jasper-usbsink` on jts.local.
        for state in [
            State::Open,
            State::Setup,
            State::Prepared,
            State::Running,
            State::XRun,
            State::Draining,
            State::Paused,
            State::Suspended,
        ] {
            assert!(
                !liveness_probe_dead(Some(state)),
                "live state {state:?} must not trip the liveness probe"
            );
        }
    }

    #[test]
    fn liveness_probe_cadence_is_about_one_second() {
        // The probe rides the drain housekeeping cadence gated to ~1 s (a
        // `snd_pcm_status` ioctl is a real syscall — kept off the per-period hot
        // path). 187 periods × 256 frames / 48000 Hz ≈ 0.997 s. The cadence is
        // advisory (detection latency, not correctness), so "within ~1 s" is the
        // contract, not exactness.
        let seconds = (DIRECT_LIVENESS_PROBE_EVERY_PERIODS as f64) * (DIRECT_PERIOD_FRAMES as f64)
            / (SAMPLE_RATE_HZ as f64);
        assert!(
            (seconds - 1.0).abs() < 0.05,
            "liveness-probe cadence must be ~1 s, got {seconds}"
        );
    }

    // ---- B2: direct-drain narrowing scratch never overflows (OOB panic) ---

    #[test]
    fn direct_narrow_scratch_bounds_max_chunk_regardless_of_period() {
        // The drain reads in chunks of at most DIRECT_PERIOD_FRAMES frames and
        // narrows `got = n × CHANNELS` samples into the narrowing scratch. The
        // largest `got` a single chunk can produce:
        let max_chunk_samples = (DIRECT_PERIOD_FRAMES as usize) * (CHANNELS as usize);
        // The narrowing scratch must bound it, and its size must NOT depend on
        // the lane's period geometry.
        assert_eq!(
            direct_narrow_scratch_samples(),
            max_chunk_samples,
            "narrowing scratch must fit one full DIRECT_PERIOD_FRAMES chunk"
        );
        assert!(
            max_chunk_samples <= direct_narrow_scratch_samples(),
            "a full chunk read must never slice past the narrowing scratch"
        );
    }

    #[test]
    fn small_period_would_overflow_the_render_buf_but_not_the_narrow_scratch() {
        // The regression: `read_buf` is sized `period_frames × CHANNELS` for the
        // `render_period` contract. Reusing it as the narrowing target (the pre-
        // fix code) slices out of bounds whenever a single chunk yields more
        // frames than `period_frames` — reachable within seconds of real
        // streaming at any legal small geometry. `panic=abort` in this hot loop
        // escalates to the jasper-fanin StartLimitAction=reboot ladder.
        let channels = CHANNELS as usize;
        let max_chunk_samples = (DIRECT_PERIOD_FRAMES as usize) * channels;
        // Every legal period at/under the chunk size is a hazard for the OLD
        // (read_buf-reuse) sizing; the fixed narrowing scratch is safe for all.
        for period_frames in [1usize, 32, 64, 128, 200, 255, 256] {
            let old_read_buf_len = period_frames * channels;
            if period_frames < DIRECT_PERIOD_FRAMES as usize {
                assert!(
                    old_read_buf_len < max_chunk_samples,
                    "pre-fix read_buf ({old_read_buf_len}) would overflow on a \
                     {max_chunk_samples}-sample chunk at period {period_frames}"
                );
            }
            // The fix's dedicated scratch fits the worst-case chunk at EVERY
            // period, small or large.
            assert!(
                max_chunk_samples <= direct_narrow_scratch_samples(),
                "narrowing scratch must bound a full chunk at period {period_frames}"
            );
        }
        // Behavioral pin: actually run the hot-loop slice ops the drain does
        // (`narrow_scratch[..got]` with `got == max_chunk_samples`) against a
        // scratch sized the way the real code sizes it. This panics if the
        // sizing ever regresses to a period-dependent length.
        let mut narrow_scratch = [0i16; direct_narrow_scratch_samples()];
        let _convert_target = &mut narrow_scratch[..max_chunk_samples];
        let _tap_view = &narrow_scratch[..max_chunk_samples];
    }

    #[test]
    fn direct_i32_and_narrow_scratches_are_equal_length() {
        // The i32 read fills `scratch[..samples]` and the narrow fills
        // `narrow_scratch[..got]` with `got == samples`; both must be the same
        // fixed length so neither read nor narrow can slice out of bounds.
        let i32_len = (DIRECT_PERIOD_FRAMES as usize) * (CHANNELS as usize);
        assert_eq!(i32_len, direct_narrow_scratch_samples());
    }

    #[test]
    fn resampler_lane_not_found_only_warns_when_armed_and_missing() {
        let labels = vec![
            "spotify".to_string(),
            "airplay".to_string(),
            "usbsink".to_string(),
            "correction".to_string(),
        ];
        // Disabled → never warn, regardless of label.
        assert_eq!(resampler_lane_not_found(false, "usbsink", &labels), None);
        assert_eq!(resampler_lane_not_found(false, "nope", &labels), None);
        // Enabled + label present → armed normally, no warning.
        assert_eq!(resampler_lane_not_found(true, "usbsink", &labels), None);
        assert_eq!(resampler_lane_not_found(true, "spotify", &labels), None);
        // Enabled + label absent → warn, returning the available-labels CSV the
        // operator can use to fix the typo.
        assert_eq!(
            resampler_lane_not_found(true, "usbsink_typo", &labels),
            Some("spotify,airplay,usbsink,correction".to_string()),
        );
        // The match is exact (a substring must NOT count as found).
        assert_eq!(
            resampler_lane_not_found(true, "usb", &labels),
            Some("spotify,airplay,usbsink,correction".to_string()),
        );
    }

    #[test]
    fn resampler_ring_frames_derives_floors_and_overrides() {
        let radius = jasper_resampler::RADIUS_FRAMES as usize;
        let min_ring = |target: u32, cushion: u32, period: u32| {
            target as usize + cushion as usize + period as usize + radius + 1
        };

        // requested=0 → derive a 2x burst ring from the ALSA input buffer
        // when that exceeds the structural minimum. The extra capacity is
        // headroom only; it does not change the resampler's held latency target.
        assert_eq!(
            resampler_ring_frames(0, 4096, 512, 256, 256),
            8192,
            "0 derives a 2x burst ring from input_buffer_frames"
        );

        // A non-zero override pins the capacity (the Fix-2 burst-headroom knob),
        // independent of the ALSA input buffer.
        assert_eq!(
            resampler_ring_frames(8192, 4096, 512, 256, 256),
            8192,
            "explicit ring_frames overrides the derived value"
        );

        // Both the derived and the override path floor to the structural minimum
        // so LaneResampler::new can never reject the ring.
        let floor = min_ring(512, 256, 256);
        assert_eq!(
            resampler_ring_frames(0, 64, 512, 256, 256),
            floor,
            "a tiny input buffer floors to the structural minimum"
        );
        assert_eq!(
            resampler_ring_frames(100, 64, 512, 256, 256),
            floor,
            "a tiny explicit override also floors to the structural minimum"
        );

        // The warm-up cushion is part of the minimum (Fix-1 ↔ Fix-2 coupling):
        // a bigger cushion raises the floor.
        assert!(
            resampler_ring_frames(0, 0, 512, 512, 256) > resampler_ring_frames(0, 0, 512, 256, 256),
            "a larger cushion raises the ring floor"
        );
    }

    #[test]
    fn resampler_read_budget_drains_partials_and_caps_pathological_backlog() {
        // The armed lane must pull the final partial period too. A one-period
        // read loop leaves this residue behind and lets the USB snd-aloop lane
        // fill even though the resampler's own ring has room.
        assert_eq!(
            resampler_read_budget_frames(TEST_PERIOD + 17, TEST_PERIOD as usize),
            (TEST_PERIOD + 17) as usize,
        );
        assert_eq!(
            resampler_read_budget_frames(TEST_PERIOD - 1, TEST_PERIOD as usize),
            (TEST_PERIOD - 1) as usize,
        );
        assert_eq!(resampler_read_budget_frames(0, TEST_PERIOD as usize), 0);
        assert_eq!(resampler_read_budget_frames(-1, TEST_PERIOD as usize), 0);

        let cap = (TEST_PERIOD as usize) * (RESAMPLER_MAX_READ_PERIODS as usize);
        assert_eq!(
            resampler_read_budget_frames(10_000 * TEST_PERIOD, TEST_PERIOD as usize),
            cap,
            "read budget must stay bounded on bogus/pathological avail"
        );
    }

    #[test]
    fn selected_input_passes_selected_and_correction() {
        assert!(input_selected(1, 1, "airplay"));
        assert!(!input_selected(1, 0, "spotify"));
        assert!(input_selected(1, 4, "correction"));
        assert!(!input_selected(-2, 0, "spotify"));
        assert!(input_selected(-2, 4, "correction"));
    }

    // ---- Catch-up resync decision (pure; no ALSA). The production default
    //      period is 256 frames. These pin the constants + the floor/cap math
    //      so a healthy lane never drains and a free-running lane resyncs to
    //      exactly one period without inducing an underrun.

    const TEST_PERIOD: i64 = 256;

    #[test]
    fn catchup_no_drain_at_or_below_high_water() {
        // A DAC-locked lane sits ~1 period; jitter up to (and including) the
        // high-water must NEVER drain — that is the invariant that keeps the
        // networked lanes' behavior unchanged.
        for periods in 0..=CATCHUP_HIGH_WATER_PERIODS {
            assert_eq!(
                catchup_drain_periods(periods * TEST_PERIOD, TEST_PERIOD),
                0,
                "avail={} periods must not drain",
                periods,
            );
        }
    }

    #[test]
    fn catchup_drains_excess_down_to_one_period() {
        // A resync only fires ABOVE the high-water (14 periods); once it does,
        // the WHOLE excess over TARGET is discarded, leaving exactly one period.
        // 15 periods (one over the high-water) → discard 14, leave 1.
        assert_eq!(catchup_drain_periods(15 * TEST_PERIOD, TEST_PERIOD), 14);
        // 16 periods (the full 4096-frame input buffer) → discard 15, leave 1.
        assert_eq!(catchup_drain_periods(16 * TEST_PERIOD, TEST_PERIOD), 15);
    }

    #[test]
    fn catchup_leaves_at_least_target_and_makes_progress() {
        // For every avail above the high-water: after discarding the planned
        // whole periods the remainder is >= target (never an induced underrun)
        // and strictly less than avail (we always make progress).
        let target = CATCHUP_TARGET_PERIODS * TEST_PERIOD;
        for periods in (CATCHUP_HIGH_WATER_PERIODS + 1)..200 {
            let avail = periods * TEST_PERIOD;
            let drained = catchup_drain_periods(avail, TEST_PERIOD);
            assert!(drained > 0, "avail={} must drain", avail);
            let remaining = avail - drained * TEST_PERIOD;
            assert!(
                remaining >= target,
                "avail={} drained={} remaining={} < target={}",
                avail,
                drained,
                remaining,
                target,
            );
        }
    }

    #[test]
    fn catchup_fractional_excess_is_floored() {
        // Just over the high-water by less than a period: the excess over
        // target floors, so we never discard a period we don't fully have
        // and never dip below target.
        let target = CATCHUP_TARGET_PERIODS * TEST_PERIOD;
        let avail = CATCHUP_HIGH_WATER_PERIODS * TEST_PERIOD + (TEST_PERIOD - 1);
        let drained = catchup_drain_periods(avail, TEST_PERIOD);
        let remaining = avail - drained * TEST_PERIOD;
        assert!(
            remaining >= target,
            "remaining={} < target={}",
            remaining,
            target
        );
    }

    #[test]
    fn catchup_is_bounded_by_max() {
        // A pathological backlog caps at MAX so the hot loop can't spin on
        // discard syscalls; the rest finishes over subsequent periods.
        assert_eq!(
            catchup_drain_periods(10_000 * TEST_PERIOD, TEST_PERIOD),
            CATCHUP_MAX_DRAIN_PERIODS,
        );
    }

    #[test]
    fn catchup_zero_or_negative_avail_never_drains() {
        // avail_update can momentarily report 0; a negative (odd driver
        // state) must also be a clean no-op rather than underflow.
        assert_eq!(catchup_drain_periods(0, TEST_PERIOD), 0);
        assert_eq!(catchup_drain_periods(-1, TEST_PERIOD), 0);
        assert_eq!(catchup_drain_periods(-10_000, TEST_PERIOD), 0);
    }

    #[test]
    // The asserts compare named const tuning parameters — that IS the regression
    // guard (a future edit that violates the bracket makes assert!(false) panic).
    // clippy::assertions_on_constants would otherwise flag the const comparison.
    #[allow(clippy::assertions_on_constants)]
    fn catchup_high_water_brackets_burst_stall_occupancy_and_buffer() {
        // Guard the two-sided tuning relationship that keeps the catch-up from
        // (a) clipping a healthy networked lane's peak ring OCCUPANCY, or
        // (b) firing too late to prevent an overrun.
        //
        // Lower bound: reasoned on OCCUPANCY (avail = frames readable on a
        // capture PCM), NOT inter-burst gap time. A healthy AirPlay lane's
        // worst-case peak fill STACKS two effects: an A-MPDU burst deposit
        // (~4 packets ≈ 5.5 periods at 256/48 kHz) plus a scheduling stall that
        // delays our drain (~36.8 ms ≈ 6.9 periods, stressed stock Pi 5;
        // PREEMPT_RT not yet in). Peak ≈ 5.5 + 6.9 ≈ 12.4 periods. The
        // high-water must sit ABOVE that so a healthy burst+stall never trips a
        // resync. Use ceil = 13 periods as the documented ceiling.
        const AIRPLAY_BURST_PERIODS: i64 = 6; // ~5.5, ceil
        const SCHED_STALL_PERIODS: i64 = 7; // ~6.9, ceil (36.8 ms stressed Pi 5)
        const HEALTHY_PEAK_OCCUPANCY_PERIODS: i64 = AIRPLAY_BURST_PERIODS + SCHED_STALL_PERIODS; // 13
        assert!(
            CATCHUP_HIGH_WATER_PERIODS > HEALTHY_PEAK_OCCUPANCY_PERIODS,
            "high_water={} must clear the healthy burst+stall peak occupancy ({} periods)",
            CATCHUP_HIGH_WATER_PERIODS,
            HEALTHY_PEAK_OCCUPANCY_PERIODS,
        );
        // Occupancy at exactly the healthy peak must NOT drain.
        assert_eq!(
            catchup_drain_periods(HEALTHY_PEAK_OCCUPANCY_PERIODS * TEST_PERIOD, TEST_PERIOD),
            0,
            "a healthy burst+stall occupancy peak must never be drop-resynced",
        );

        // Upper bound: the high-water must sit below the default input buffer
        // depth (4096 frames = 16 periods at 256) with margin, so the resync
        // fires before the ring overruns.
        const DEFAULT_INPUT_BUFFER_PERIODS: i64 = 16; // 4096 / 256
        assert!(
            CATCHUP_HIGH_WATER_PERIODS < DEFAULT_INPUT_BUFFER_PERIODS,
            "high_water={} must stay under the input buffer ({} periods)",
            CATCHUP_HIGH_WATER_PERIODS,
            DEFAULT_INPUT_BUFFER_PERIODS,
        );
    }

    // Ring A output path. These construct a real SPSC ring (via jasper_ring)
    // under the OS temp dir and drive `write_ring_period` directly, so they run
    // on any host that can build the crate (CI Linux). `RingOutput` holds no
    // ALSA handle at all, so the ring publish + reader roundtrip is the whole
    // contract — there is nothing else for a test to stub out.

    use jasper_ring::{RingReader, SlotRead};
    use jasper_tts_protocol::loudness::{gain_db_to_linear, AssistantLoudnessConfig};
    use jasper_tts_protocol::{QueuedTtsCommand, TtsCommand};
    use std::sync::atomic::AtomicU64 as TestAtomicU64;

    use crate::tts::{tts_channels, QueuedFlush};

    static RING_MIXER_TEST_SEQ: TestAtomicU64 = TestAtomicU64::new(0);

    fn ring_geometry(n_slots: u32) -> Geometry {
        Geometry {
            rate: 48_000,
            channels: CHANNELS,
            sample_format: SAMPLE_FORMAT_S32LE,
            period_frames: RING_SLOT_FRAMES,
            n_slots,
        }
    }

    fn tmp_ring_output(n_slots: u32, tag: &str) -> (RingOutput, String) {
        let dir = std::env::temp_dir().join(format!(
            "jts-fanin-ring-{}-{}-{}",
            tag,
            std::process::id(),
            RING_MIXER_TEST_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("program.ring").to_string_lossy().into_owned();
        let writer = RingWriter::create_or_attach(&path, ring_geometry(n_slots)).unwrap();
        let counters = RingCounters::new();
        let ring = RingOutput {
            writer,
            counters,
            // One 256-frame period at 48k, in ns.
            pace: PeriodPacer::new(256 * 1_000_000_000 / 48_000),
            stall: RingStallTracker::new(),
        };
        (ring, path)
    }

    /// One period's mix sum rendered into the bytes `step()` publishes.
    fn payload_of(sum: &[i64]) -> Vec<u8> {
        let mut payload = vec![0u8; sum.len() * BYTES_PER_SAMPLE];
        fill_ring_payload(sum, &mut payload);
        payload
    }

    /// The i32 samples a reader takes off one slot of published bytes.
    fn samples_of(slot: &[u8]) -> Vec<i32> {
        slot.chunks_exact(BYTES_PER_SAMPLE)
            .map(|c| i32::from_le_bytes(c.try_into().unwrap()))
            .collect()
    }

    fn cleanup_ring(path: &str) {
        let _ = std::fs::remove_file(path);
        if let Some(parent) = std::path::Path::new(path).parent() {
            let _ = std::fs::remove_dir(parent);
        }
    }

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

    /// A `TtsMixer` with an ACTIVE program duck and one queued TTS period.
    /// Returns it with its senders, which the caller must keep alive for the
    /// mixer's whole life.
    fn ducking_tts_mixer(
        payload: &[i16],
        program_duck_db: f32,
    ) -> (
        TtsMixer,
        SyncSender<QueuedTtsCommand>,
        SyncSender<QueuedFlush>,
    ) {
        let (tx, rx, flush_tx, flush_rx, metrics, _epoch) = tts_channels(48_000);
        let mixer = TtsMixer::new(TtsInput {
            rx,
            flush_rx,
            metrics,
            max_pending_frames: 48_000,
            program_duck_db,
            cue_duck_db: -6.0,
            assistant_loudness: AssistantLoudnessConfig::default(),
            assistant_reference: None,
            assistant_reference_tx: None,
        });
        for command in [
            TtsCommand::ProgramDuckOn,
            TtsCommand::Audio(payload.to_vec()),
        ] {
            tx.send(QueuedTtsCommand { epoch: 0, command }).unwrap();
        }
        (mixer, tx, flush_tx)
    }

    /// Q2 (duck ORDER): `step()` applies the program duck to the summed
    /// renderer lanes and mixes the TTS period on top of that ducked sum, so a
    /// voice turn is never attenuated by its own duck. Driven through `step()`
    /// and read back off the ring, so swapping the two stages inside `step()`
    /// fails HERE rather than nowhere.
    ///
    /// The mixer runs with NO program lane: the sum `step()` ducks is silence,
    /// which still separates the two orders — a duck applied AFTER the TTS mix
    /// scales the published TTS period by `duck_gain`, which the last assertion
    /// rejects. The ducked-program half of the order is pinned by
    /// `ring_output_carries_post_duck_post_tts_period`.
    #[test]
    fn step_mixes_the_tts_period_after_the_program_duck() {
        const PROGRAM_DUCK_DB: f32 = -25.0;
        // 96 of the period's 128 frames, so the tail carries no TTS at all.
        const TTS_FRAMES: usize = 96;

        let period_frames = RING_SLOT_FRAMES;
        let period_samples = (period_frames as usize) * (CHANNELS as usize);

        let (output, out_path) = tmp_ring_output(8, "duck_order");
        let mut reader = RingReader::create_or_attach(&out_path, ring_geometry(8)).unwrap();
        let mut slot = vec![0u8; period_samples * BYTES_PER_SAMPLE];
        // Prime the reader heartbeat so the writer takes the publish path.
        assert_eq!(reader.try_consume_slot_bytes(&mut slot), SlotRead::Empty);

        // A loud TTS period: it must ride above anything a duck applied AFTER
        // the TTS mix could produce (asserted on the reference below).
        let payload = vec![30_000i16; TTS_FRAMES * (CHANNELS as usize)];
        let (tts, _tx, _flush_tx) = ducking_tts_mixer(&payload, PROGRAM_DUCK_DB);
        let duck_gain = gain_db_to_linear(PROGRAM_DUCK_DB);

        let (tap_sender, tap_receiver) =
            std::sync::mpsc::sync_channel::<TapEvent>(EVENT_CHANNEL_CAPACITY);
        let counters = RingCounters::new();
        let ring_observability = RingObservability {
            path: out_path.clone(),
            nominal_clock: Arc::clone(&counters.nominal_clock),
            slots: 8,
            channels: CHANNELS,
            occupancy: Arc::clone(&counters.occupancy),
            published: Arc::clone(&counters.published),
            full_waits: Arc::clone(&counters.full_waits),
            stuck_reader_drops: Arc::clone(&counters.stuck_reader_drops),
            drop_no_reader: Arc::clone(&counters.drop_no_reader),
            stall_active: Arc::clone(&counters.stall_active),
            last_stall_ms: Arc::clone(&counters.last_stall_ms),
            clockless_paces: Arc::clone(&counters.clockless_paces),
        };
        let mut mixer = Mixer {
            inputs: Vec::new(),
            output,
            sum_buf: vec![0i64; period_samples],
            ring_payload: vec![0u8; period_samples * BYTES_PER_SAMPLE],
            content_meter_buf: vec![0i16; period_samples],
            frames_written: Arc::new(AtomicU64::new(0)),
            selected_input_index: Arc::new(AtomicI32::new(-2)),
            period_frames,
            tts: Some(tts),
            // A SETTLED duck — the steady state of a voice turn, where the
            // per-period target and the persisted gain already agree.
            program_duck_current: duck_gain,
            program_duck_attack_step: duck_step_per_frame(20, 48_000),
            program_duck_release_step: duck_step_per_frame(200, 48_000),
            ring_observability,
            auto_trim_enabled: false,
            auto_trim_delay_frames: 0,
            auto_trim_lane_state: Vec::new(),
            direct_tap: DirectTapHook::new(
                Arc::new(TapState::default()),
                Arc::new(Mutex::new(TapConfig::default())),
                tap_sender,
            ),
            direct_tap_receiver: Some(tap_receiver),
            host_clock_ladder_l0: Arc::new(AtomicBool::new(false)),
            usb_connection_epoch: Arc::new(AtomicU64::new(0)),
            host_clock_timing_failed: Arc::new(AtomicBool::new(false)),
        };

        // The UNATTENUATED TTS period: the same fixture, the same commands and
        // the same (silent) content period, mixed into a ZERO sum. It is what
        // `tts.mix_period` contributes with no duck in front of it — nothing
        // about where `step()` applies the duck is modelled here.
        let (mut reference, _ref_tx, _ref_flush_tx) = ducking_tts_mixer(&payload, PROGRAM_DUCK_DB);
        let mut tts_only = vec![0i64; period_samples];
        assert!(reference.prepare_period());
        reference.observe_content_period(&vec![0i16; period_samples]);
        reference.mix_period(&mut tts_only);
        assert!(
            tts_only
                .iter()
                .any(|&t| t
                    > ((jasper_resampler::widen_i16_to_i32(i16::MAX) as f32) * duck_gain) as i64),
            "fixture: the TTS period must ride above a ducked full-scale sample, or a \
             duck applied after the TTS mix would be indistinguishable from one applied \
             before it"
        );

        mixer.step().unwrap();

        assert_eq!(reader.try_consume_slot_bytes(&mut slot), SlotRead::Filled);
        assert_eq!(
            slot,
            payload_of(&tts_only),
            "the ring must carry the UNATTENUATED TTS period"
        );
        assert!(
            samples_of(&slot)[TTS_FRAMES * (CHANNELS as usize)..]
                .iter()
                .all(|&s| s == 0),
            "past the TTS period nothing but the (silent) ducked sum is published"
        );
        // What a duck applied AFTER the TTS mix would have published instead.
        let ducked_tts: Vec<i64> = tts_only
            .iter()
            .map(|&t| ((t as f32) * duck_gain).round() as i64)
            .collect();
        assert_ne!(
            slot,
            payload_of(&ducked_tts),
            "the TTS period must not carry the program duck"
        );

        cleanup_ring(&out_path);
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

    // ---- AUTO-TRIM: one-shot latch decision (pure) ------------------------

    const TEST_DELAY: u64 = 96_000; // 2 s @ 48 kHz

    #[test]
    fn auto_trim_activation_period_never_fires() {
        // idle (default) -> reads audio this period: arm the delay, never fire
        // on the activation period itself (the standing fill hasn't accumulated).
        let d = auto_trim_decision(128, AutoTrimLaneState::default(), TEST_DELAY);
        assert!(!d.fire);
        assert_eq!(d.next.active_since, Some(128));
        assert_eq!(d.next.last_frames_read, 128);
    }

    #[test]
    fn auto_trim_fires_once_delay_elapsed() {
        // active_since=128; fires the first period frames_read - since >= delay.
        let state = AutoTrimLaneState {
            last_frames_read: 95_000,
            active_since: Some(128),
        };
        let d = auto_trim_decision(96_128, state, TEST_DELAY); // 96000 elapsed
        assert!(d.fire);
        assert_eq!(d.next.active_since, Some(128));
    }

    #[test]
    fn auto_trim_does_not_fire_before_delay() {
        let state = AutoTrimLaneState {
            last_frames_read: 1000,
            active_since: Some(128),
        };
        let d = auto_trim_decision(5000, state, TEST_DELAY); // only ~4872 elapsed
        assert!(!d.fire);
    }

    #[test]
    fn auto_trim_rearms_on_idle() {
        // Active then no read this period => active_since cleared (re-armed) so
        // the NEXT idle->active session fires again.
        let state = AutoTrimLaneState {
            last_frames_read: 5000,
            active_since: Some(128),
        };
        let d = auto_trim_decision(5000, state, TEST_DELAY); // no advance => idle
        assert!(!d.fire);
        assert_eq!(d.next.active_since, None);
        let d2 = auto_trim_decision(5128, d.next, TEST_DELAY); // fresh activation
        assert!(!d2.fire);
        assert_eq!(d2.next.active_since, Some(5128));
    }

    #[test]
    fn auto_trim_delay_measured_from_activation_not_stream_start() {
        // A lane already deep into playback when auto-trim arms: active_since
        // captures the CURRENT frames_read, so the delay is relative to
        // activation, not to the absolute frame count.
        let state = AutoTrimLaneState {
            last_frames_read: 1_000_000,
            active_since: None,
        };
        let d = auto_trim_decision(1_000_128, state, TEST_DELAY);
        assert_eq!(d.next.active_since, Some(1_000_128));
        assert!(!d.fire);
        let d2 = auto_trim_decision(1_096_128, d.next, TEST_DELAY);
        assert!(d2.fire);
    }

    // ---- U2 / #2223: the widened DIRECT lane, end of the route -------------

    /// The gadget-shaped input stream the byte-identity golden runs on.
    ///
    /// Deterministic and NOT constant: a constant survives any normalised
    /// interpolation kernel unchanged, so a DC golden would pin the conversions
    /// but not the resampling. This is a two-tone stereo signal at genuine
    /// 24-bit depth (the low bits are populated, so the capture narrowing has
    /// something to discard), phase-continuous across the whole buffer.
    fn golden_gadget_stream(frames: usize) -> Vec<i32> {
        let mut out = Vec::with_capacity(frames * (CHANNELS as usize));
        for n in 0..frames {
            let t = n as f64;
            // Amplitudes well inside full scale so nothing clips; the `+ 0x5a`
            // style offsets keep the bottom byte busy.
            let l = (0.31 * (t * 0.013).sin() + 0.11 * (t * 0.211).sin()) * 2_000_000_000.0;
            let r = (0.27 * (t * 0.019).cos() + 0.09 * (t * 0.077).sin()) * 2_000_000_000.0;
            out.push(jasper_resampler::clamp_i32(l));
            out.push(jasper_resampler::clamp_i32(r));
        }
        out
    }

    /// THE BYTE-IDENTITY GOLDEN for the program wire.
    ///
    /// The gadget-shaped stream above driven through the DIRECT lane's whole
    /// route — resampler push, render, sum entry, payload fill — pinned to
    /// committed samples. Every box in the fleet runs this path.
    ///
    /// The golden is committed evidence about the route's samples, not a
    /// printout of what this code happens to produce, so a change to the route
    /// has to meet it rather than restate it.
    ///
    /// What moves them: any narrowing or shift reintroduced at the capture
    /// boundary or the render; the resampler ring's storage scale; the sum's
    /// scale; the i64→i32 saturation; the payload's byte order.
    #[test]
    fn the_direct_route_is_byte_identical_to_its_committed_golden() {
        let output = direct_route_golden_output();
        // Committed samples for this fixture.
        let expected: [i32; 24] = [
            453583763, 143264454, 503897903, 136160263, 554607748, 130001019, 603660574, 124776517,
            649073279, 120470397, 689020309, 117060255, 721914606, 114517801, 746477971, 112809037,
            761797728, 111894479, 767367178, 111729406, 763108045, 112264149, 749373919, 113444399,
        ];
        assert_eq!(
            &output[..expected.len()],
            &expected,
            "the DIRECT route's published period drifted from its committed golden"
        );
        // A second window, deeper into the period, so the pin is not only on the
        // ramp-adjacent leading samples.
        let tail: [i32; 8] = [
            676817580, -533234671, 648401174, -522469089, 612021137, -511484259, 569057559,
            -500361258,
        ];
        assert_eq!(&output[200..208], &tail, "golden tail window drifted");
    }

    /// Drive the DIRECT lane over [`golden_gadget_stream`] and return the
    /// samples a steady-state period publishes.
    ///
    /// Deliberately built from the SAME primitives the daemon uses in the same
    /// order — `push_input`, then `render_period`, then `mix_into`, then
    /// `fill_ring_payload` — so the golden it feeds pins the real route rather
    /// than a paraphrase of it.
    fn direct_route_golden_output() -> Vec<i32> {
        const PERIOD: u32 = 256;
        const CH: usize = CHANNELS as usize;
        let mut lane = LaneResampler::new(
            CH,
            PERIOD,
            48_000,
            512,
            PERIOD as usize,
            500.0,
            8_192,
            crate::lane_resampler::DecayParams::disabled(),
        )
        .expect("lane resampler builds");
        let mut rendered = vec![0i32; PERIOD as usize * CH];
        let mut phase = 0usize;
        // Feed a period per render, as the gadget does; prime deep enough to lock.
        let feed = |lane: &mut LaneResampler, phase: &mut usize, frames: usize| {
            let gadget = golden_gadget_stream(*phase + frames);
            lane.push_input(&gadget[*phase * CH..]);
            *phase += frames;
        };
        feed(
            &mut lane,
            &mut phase,
            512 + PERIOD as usize + 17 + PERIOD as usize,
        );
        for _ in 0..3 {
            feed(&mut lane, &mut phase, PERIOD as usize);
            assert_eq!(lane.render_period(&mut rendered), PERIOD as usize);
        }
        let mut sum = vec![0i64; rendered.len()];
        mix_into(&mut sum, &rendered);
        let payload = payload_of(&sum);
        samples_of(&payload)
    }

    /// Printer for [`the_direct_route_is_byte_identical_to_its_committed_golden`]'s
    /// fixture, so the golden can be RE-captured against a known commit rather
    /// than hand-transcribed. Ignored by default; run with
    /// `cargo test print_direct_golden -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn print_direct_golden() {
        let out = direct_route_golden_output();
        println!("HEAD24 {:?}", &out[..24]);
        println!("TAIL8 {:?}", &out[200..208]);
    }
}
