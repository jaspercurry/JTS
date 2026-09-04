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
//! Lane inputs are interleaved stereo, S16_LE or (on a box whose wire resolves
//! wide) S32_LE. The sum accumulates into an **i64** scratch with
//! `saturating_add`, so simultaneous full-scale lanes keep real headroom above
//! full scale at either numeric scale — see [`ProgramWidth`] — before the
//! consumer clamps.

mod direct_capture;
mod dsp;
mod lane_fade;
mod ring_capture;

use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicI64, AtomicU64, Ordering};
use std::sync::mpsc::{Sender, SyncSender};
use std::sync::{Arc, Mutex};

use alsa::pcm::{Access, Format, Frames, HwParams, State, IO, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};
use log::{info, warn};

use jasper_ring::{Geometry, PublishOutcome, RingWriter, SAMPLE_FORMAT_S32LE};

use jasper_resampler::{rms_dbfs_i16, RMS_DBFS_FLOOR};

use crate::config::{Config, RingWireFormat, MEASUREMENT_LANE, RING_SLOT_FRAMES};
use crate::impulse_tap::{ImpulseDetector, TapConfig, TapEvent, TapState};
use crate::lane_resampler::{LaneResampler, LaneResamplerObservability};
use crate::tts::{TtsInput, TtsMixer};
use crate::watchdog::Heartbeat;
use crate::xrun_log::{XrunEvent, XrunSource};

use direct_capture::{read_direct_and_render, DirectCapture};
pub use direct_capture::{DirectObservability, DrainStats};
use dsp::{
    apply_gain_to_sum, duck_step_per_frame, fill_wide_ring_payload, mix_into, mix_into_wide,
    ramp_program_duck, saturate_to_i16, WIDE_BYTES_PER_SAMPLE,
};
use lane_fade::LaneFade;
use ring_capture::read_ring_and_render;
pub use ring_capture::RingLaneObservability;

/// Stereo, on both ends: the renderer ingress lanes carry 2 channels and so
/// does the ring this daemon publishes to. Not configurable.
pub const CHANNELS: u32 = 2;

/// PCM sample format for this daemon's snd-aloop capture lanes — the
/// per-renderer inputs, the only aloop lanes left since ADR-0100.
///
/// Lane ingress is NOT its own width axis: it is this box's one resolved wire
/// ([`Config::program_wire_is_wide`]), the same fact that decides the program
/// ring's payload and the assistant wire.
///
/// snd-aloop pins both halves of a cable to one format, so the renderer
/// aliases' slaves in `deploy/alsa/asoundrc.jasper` declare the same width and
/// move with this; `tests/test_fanin_wiring.py` and `check_fanin_asound_wiring`
/// pin that side.
fn lane_capture_format(program_wire_is_wide: bool) -> Format {
    if program_wire_is_wide {
        Format::S32LE
    } else {
        Format::S16LE
    }
}

/// Sentinel for "no ALSA playback delay sample has landed", which since
/// ADR-0100 is every period: this daemon opens no playback PCM to sample. It
/// renders `/state.output.snd_pcm_delay_frames` as `null`, which is what the
/// AirPlay latency derivation already reads on a ring box.
pub const OUTPUT_DELAY_UNAVAILABLE: u64 = u64::MAX;

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

/// Compute the direct capture buffer for a given open period, honoring the
/// deep-buffer safety floor (≥ `DIRECT_BUFFER_MIN_PERIODS` periods AND ≥
/// `DIRECT_BUFFER_MIN_FRAMES`), then rounded UP to a whole period multiple so
/// the negotiated geometry is period-aligned (a fractional buffer would shear;
/// `direct_open_params_ok` rejects it). Pure so the floor math is unit-testable
/// without ALSA.
fn resolve_direct_buffer_frames(period: u32) -> u32 {
    let by_periods = period.saturating_mul(DIRECT_BUFFER_MIN_PERIODS);
    let floor = by_periods.max(DIRECT_BUFFER_MIN_FRAMES);
    // Round up to the next whole period so buffer % period == 0.
    let period = period.max(1);
    floor.div_ceil(period).saturating_mul(period)
}

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

/// Length of the S16 narrowing scratch the direct drain uses per chunk read.
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

/// Bounded impulse-tap channel capacity. The single detector fires at most once
/// per refractory window (~4/s at the 250 ms default), so this can never fill
/// under the harness; it is a drop-and-count safety net that keeps the mixer
/// thread's `try_send` non-blocking.
const TAP_CHANNEL_CAPACITY: usize = 256;

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

/// Post-lock cushion-decay warm-up window: the continuous locked +
/// DLL-`l0_locked` + calm duration required before the FIRST decay step. 10 s
/// gives the outer host-clock DLL time to finish its per-session probe (up to
/// 14 s worst case, but the fill is pinned well before that) and prove the
/// steady regime before latency is reclaimed. Not an env knob — the three
/// tunable decay knobs (floor/step/interval) are the operator surface.
/// Converted to render periods by the lane from the live sample rate.
const CUSHION_DECAY_STABILITY_MS: u64 = 10_000;

/// Cascade-stability guard: decay pauses while the outer DLL's |commanded_ppm|
/// exceeds this. Above it the DLL is working hard and the fill is in transient,
/// so lowering the setpoint would fight the loop — the two-controller
/// oscillation class the cascade design avoids. 400 ppm is well inside the
/// ±1000 ppm servo authority: it flags "actively correcting" without tripping on
/// the small steady-state trims a settled loop makes. The command compared here
/// reflects genuine host-clock correction, not the descent — the decay's own
/// demand is subtracted from the servo's observable at the source (#3466). A
/// host whose genuine standing offset keeps |cmd| above the guard holds decay
/// frozen for the session, by design: that host is being corrected hard and the
/// cushion is load-bearing.
const CUSHION_DECAY_CASCADE_GUARD_PPM: f64 = 400.0;

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
            target_ns: period_ns * (100 - PACE_HEADROOM_PERCENT) / 100,
            deadline_ns: None,
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

impl RingOutput {
    /// Whether this ring's wire is S32LE, read from the geometry the writer
    /// actually attached against rather than from a parallel flag. The header
    /// is immutable for the ring file's life and attach already compared it
    /// field-by-field, so this cannot drift from the bytes the reader expects.
    fn wire_is_wide(&self) -> bool {
        self.writer.geometry().sample_format == SAMPLE_FORMAT_S32LE
    }
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
/// wire format that was never wrong.
pub(crate) fn ring_open_error(
    path: &str,
    wire_format: &str,
    error: std::io::Error,
) -> anyhow::Error {
    if ring_open_error_is_config_class(&error) {
        warn!(
            "event=fanin.ring.config_error path={} wire_format={} detail={}",
            path, wire_format, error,
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

/// The three facts `program_width_disagreement` cross-checks — named so the
/// call site cannot transpose two booleans silently.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ProgramWidthAudit {
    /// The width the mixer resolved from config and will run every stage at.
    pub declared: ProgramWidth,
    /// Whether the OUTPUT the mixer is about to publish through is a wide ring
    /// (its own attached header, not config).
    pub output_is_wide: bool,
    /// Whether ANY constructed lane actually allocated a spine-scale period
    /// buffer — the lane-side half of the width, observed rather than assumed.
    pub any_lane_is_wide: bool,
}

/// Cross-check the program width against BOTH things that must agree with it,
/// returning the CONFIG-CLASS error to fail construction with, or `None`.
///
/// The declared width is one derivation ([`ProgramWidth::from_config`]), but it
/// is consumed in two places that could each drift from it independently — the
/// OUTPUT (the ring publishes from its own attached header) and the LANES (each
/// lane decides at construction whether to allocate a spine-scale period
/// buffer). The two axes are checked DIFFERENTLY, and the asymmetry is the
/// point:
///
/// * The OUTPUT axis is an EQUALITY. A narrow sum on a wide wire under-drives it
///   by 96 dB; a wide sum on a narrow wire over-drives it by the same.
/// * The LANE axis is a ONE-WAY IMPLICATION — `any_lane_is_wide` implies
///   `declared_wide`, never the reverse. A spine-scale lane feeding a
///   narrow-scale sum is the +96 dB fault worth parking for. The reverse, a
///   wide sum with no wide lane, is **legal and must construct**: a box with
///   no lanes at all still resolves a wide wire, and any i16 lane promotes at
///   `mix_into`'s Wide arm. Requiring equality here would park a CORRECT box —
///   exit 78, no audio path, no self-recovery.
///
/// The error carries the config-class marker because a width disagreement is
/// permanent: nothing about restarting changes which wire the box resolved or
/// which buffers its lanes allocated. Without [`crate::ConfigClassError`] the
/// unit would take the `Restart=on-failure` ladder into
/// `StartLimitAction=reboot`; with it, `park_on_config_class` exits 78 and the
/// unit parks visibly.
///
/// Extracted rather than left inline for the reason [`ring_open_error`]'s own
/// doc gives: `Mixer::new` cannot be constructed without ALSA, so a decision
/// left in its body is unreachable from a hardware-free test.
pub(crate) fn program_width_disagreement(audit: ProgramWidthAudit) -> Option<anyhow::Error> {
    let declared_wide = matches!(audit.declared, ProgramWidth::Wide);
    // Output: equality, both directions are faults.
    let output_disagrees = declared_wide != audit.output_is_wide;
    // Lane: implication only. A wide lane in a narrow sum is the +96 dB fault;
    // a wide sum with no wide lane is the ordinary USB-off configuration.
    let lane_disagrees = audit.any_lane_is_wide && !declared_wide;
    if !output_disagrees && !lane_disagrees {
        return None;
    }
    warn!(
        "event=fanin.width_disagreement declared_wide={} output_wide={} lane_wide={} \
         — refusing to mix lanes at mismatched numeric scales",
        declared_wide, audit.output_is_wide, audit.any_lane_is_wide,
    );
    Some(
        anyhow::anyhow!(
            "fan-in program width disagreement: declared wide={} but the attached ring \
             header says wide={} and the constructed lanes say wide={} — refusing to mix \
             lanes at mismatched numeric scales",
            declared_wide,
            audit.output_is_wide,
            audit.any_lane_is_wide,
        )
        .context(crate::ConfigClassError),
    )
}

pub struct Mixer {
    inputs: Vec<Input>,
    output: RingOutput,
    /// The numeric scale this run's program sum carries, resolved ONCE from the
    /// box's wire (see [`ProgramWidth`]). Every stage between a lane's read and
    /// the summed write consults this one value rather than re-deriving the
    /// width from the transport.
    program_width: ProgramWidth,
    /// Per-period scratch: `period_frames * CHANNELS` samples in the scale
    /// `program_width` names. `i64` so the mix keeps real headroom above full
    /// scale at BOTH scales — see [`ProgramWidth`] for why that headroom is
    /// audible rather than theoretical.
    sum_buf: Vec<i64>,
    /// Per-period output buffer (i16 interleaved). Same length as
    /// sum_buf.
    output_buf: Vec<i16>,
    /// Per-period payload for an S32LE Ring A wire: `sum_buf` saturated into the
    /// i32 spine range, little-endian. EMPTY (and therefore zero heap — a `Vec`
    /// with no capacity does not allocate) on a narrow wire.
    ///
    /// Bytes rather than `Vec<i32>` because the wire is explicitly
    /// LITTLE-endian: `to_le_bytes` states that, where reinterpreting an `i32`
    /// slice would silently depend on the host's endianness. `4 * sum_buf.len()`
    /// bytes when armed.
    ring_wide_payload: Vec<u8>,
    /// Per-period pre-duck program buffer for the assistant loudness
    /// meter. Same length as sum_buf.
    content_meter_buf: Vec<i16>,
    /// Cumulative output frames written since startup. Surfaced via
    /// the STATUS endpoint.
    pub frames_written: Arc<AtomicU64>,
    /// Cumulative output xrun events. Pinned at 0 since ADR-0100 — an SHM ring
    /// has no xrun; a wedged reader shows up in the ring's own stall counters.
    pub output_xrun_count: Arc<AtomicU64>,
    /// Last observed ALSA playback delay for the output PCM. Pinned at
    /// [`OUTPUT_DELAY_UNAVAILABLE`] since ADR-0100 — there is no playback PCM to
    /// sample. Both stay in STATUS because live Python readers
    /// (`jasper.control.airplay_health`, the AirPlay latency derivation) read
    /// them through their absent/zero shape on a ring box.
    pub output_delay_frames: Arc<AtomicU64>,
    /// Selected input index. -1 means auto/mix all active inputs;
    /// -2 means pass no renderer lanes; non-negative means pass only
    /// that source's lane. The correction/test lane is always mixed so
    /// diagnostics keep working even if the household selected a
    /// renderer manually or mux temporarily selected NONE.
    selected_input_index: Arc<AtomicI32>,
    /// Channel for forwarding xrun events to the off-thread log writer. Sending
    /// on this unbounded channel is non-blocking and only fails when the
    /// receiver is dropped (shutdown), which keeps the work loop's hot path off
    /// disk I/O — the writer thread is the one stuck on fdatasync.
    xrun_tx: Sender<XrunEvent>,
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
    /// The impulse tap over the USB DIRECT capture ingress. Runs inline over the
    /// converted S16 slice in `read_direct_and_render`, before `push_input`.
    /// Present regardless of the direct flag; its disarmed cost is one relaxed
    /// atomic load per direct read.
    direct_tap: DirectTapHook,
    /// The receiver half of the tap channel, taken by `main` to drive the
    /// `fanin-tap-writer` thread (the single JSONL writer). `None` after
    /// `take_direct_tap_receiver`.
    direct_tap_receiver: Option<std::sync::mpsc::Receiver<TapEvent>>,
    /// REVERSE host-clock signals (servo thread → mixer) for the DEFAULT-OFF
    /// post-lock cushion decay. The `fanin-host-clock` thread only ever WRITES
    /// these, every servo tick; the mixer's per-period decay tick only ever
    /// READS them. `ladder_l0` = the DLL is `l0_locked` (decay's steady-state
    /// gate); `commanded_milli_ppm` = the DLL's last commanded bias (× 1000) for
    /// the cascade guard. When the servo thread is not running (host-clock off /
    /// no direct lane) these stay at their init (`false` / 0), so decay never
    /// leaves the ceiling — decay REQUIRES the DLL.
    host_clock_ladder_l0: Arc<AtomicBool>,
    host_clock_commanded_milli_ppm: Arc<AtomicI64>,
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
/// `wire_format` / `channels` are plain values, not atomics: they come off the
/// header the writer attached against, which is immutable for the ring file's
/// life, so there is nothing for a later period to update.
#[derive(Clone)]
pub struct RingObservability {
    pub path: String,
    pub slots: u32,
    /// The OBSERVED wire vocabulary token (`S16_LE` / `S32_LE`), or `unknown`
    /// for a header id this daemon has no token for.
    pub wire_format: &'static str,
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

/// Render a ring header `sample_format` id as the wire vocabulary token
/// [`RingObservability::wire_format`] publishes. The vocabulary itself lives
/// once, in [`RingWireFormat`]. An unmappable id cannot reach a running ring
/// (attach validates the accept-set first), so `unknown` is a placeholder
/// rather than a state to act on.
fn ring_wire_format_label(sample_format: u32) -> &'static str {
    match RingWireFormat::from_sample_format_id(sample_format) {
        Some(format) => format.as_str(),
        None => "unknown",
    }
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
/// The three are alternatives, not layers: exactly one applies to a lane, and the
/// lane's read path, its `pcm` field's emptiness, and this token all follow from
/// the same pair of `Option`s (see [`Input::lane_source`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LaneSource {
    /// An snd-aloop capture substream — the shipped default for every renderer.
    Lane,
    /// The USB gadget capture, read directly (`hw:UAC2Gadget`).
    Direct,
    /// A per-renderer SHM slot ring written by the renderer's `jts_ring` ioplug.
    Ring,
}

impl LaneSource {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Lane => "lane",
            Self::Direct => "direct",
            Self::Ring => "ring",
        }
    }
}

pub struct Input {
    /// The aloop capture PCM for this lane. `None` on the two lane sources that
    /// do not read an aloop substream at all: the USB DIRECT lane
    /// (`direct.is_some()`), whose audio comes from the `hw:UAC2Gadget` capture in
    /// `direct`, and a renderer-ingress RING lane (`ring.is_some()`), whose audio
    /// comes from the SHM slot ring in `ring`. Every other lane has `Some`.
    ///
    /// At most one of `direct` / `ring` is ever `Some`: they are alternative lane
    /// SOURCES, and `Mixer::new` picks exactly one arm per lane.
    pcm: Option<PCM>,
    /// DEFAULT-OFF USB DIRECT capture. `Some` only on the usbsink lane when
    /// `JASPER_FANIN_USB_DIRECT=enabled`; the lane then reads `hw:UAC2Gadget`
    /// directly instead of its aloop substream. `None` (and `pcm.is_some()`) on
    /// every other lane and on this lane when the flag is off.
    direct: Option<DirectCapture>,
    /// The DIRECT lane's deferred device-open channel (#2533). `Some` alongside
    /// `direct`; `None` if the opener thread could not be spawned. The render
    /// loop hands every `snd_pcm_open` / `snd_pcm_close` to it rather than
    /// running one inside the period budget.
    direct_opener: Option<direct_capture::DirectOpener>,
    /// DEFAULT-EMPTY renderer-ingress ring capture. `Some` only on a lane whose
    /// label is in `JASPER_FANIN_RENDERER_RING_LANES`; the lane then reads
    /// `/dev/shm/jts-ring/lane-<label>.ring` instead of its aloop substream.
    /// `None` (and `pcm.is_some()`) on every unarmed lane.
    ring: Option<ring_capture::RingCapture>,
    /// The ring lane's deferred ATTACH channel (#2538). `Some` alongside `ring`;
    /// `None` if the attacher thread could not be spawned. The render loop hands
    /// every `RingReader::create_or_attach` — and the bounded inter-process
    /// `flock` inside it — to this thread rather than running one inside the
    /// period budget. One per lane, so each lane's in-flight latch is its own.
    ring_attacher: Option<ring_capture::RingAttacher>,
    pub label: String,
    pub pcm_name: String,
    /// Per-input read buffer (i16 interleaved stereo). Reused as the
    /// discard scratch by the catch-up drain — no per-period allocation.
    ///
    /// The lane's period lands HERE unless `read_buf_wide` is non-empty.
    read_buf: Vec<i16>,
    /// Per-input SPINE-SCALE read buffer (i32 interleaved stereo), allocated
    /// on a wide wire and EMPTY — so zero heap, a `Vec` with no capacity does
    /// not allocate — on a box pinned to the narrow wire (`spine_read_buf`).
    ///
    /// Non-empty is the lane's OWN width switch, and the mixer reads it exactly
    /// that way (`read_buf_wide.is_empty()` picks the sum entry). One allocation
    /// decision at construction is the whole mechanism; there is no second flag
    /// that could disagree with which buffer actually holds the period.
    read_buf_wide: Vec<i32>,
    pub xrun_count: Arc<AtomicU64>,
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
    /// configured clock-crossing lane when `JASPER_FANIN_INPUT_RESAMPLER` is
    /// `enabled`. When `Some`, this lane is rate-reconciled to the DAC clock
    /// (drop-free) instead of catch-up-drained.
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
    /// OPTIONAL renderer-ingress ring observability, shared with the state-server
    /// thread. `Some` only on a ring lane (`ring.is_some()`); STATUS renders a
    /// `ring{}` block from it.
    ring_obs: Option<ring_capture::RingLaneObservability>,
    /// Per-lane WAKE FADE-IN and SELECTION FADE state (issue #3443). Held by
    /// every lane and usable at both lane scales, because it runs over this
    /// lane's rendered period — the one thing all four read arms produce.
    /// Silence is tracked before the selection gate and both windows are applied
    /// after it; inert on [`MEASUREMENT_LANE`].
    lane_fade: LaneFade,
}

impl Mixer {
    /// Open all configured inputs and the output. Every configured input
    /// is required: a missing lane means one renderer silently drops out
    /// of the summed music reference. `xrun_tx` is the
    /// non-blocking channel to the off-thread xrun log writer.
    pub fn new(config: &Config, xrun_tx: Sender<XrunEvent>, tts: Option<TtsInput>) -> Result<Self> {
        let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
        // The ONE width derivation for this run. Resolved before any lane is
        // built because it decides the DIRECT lane's render width and whether it
        // allocates a spine-scale read buffer at all.
        let program_width = ProgramWidth::from_config(config);

        let mut inputs = Vec::with_capacity(config.input_pcms.len());
        // The lane's position, handed to the ring lanes so each seeds its reattach
        // latch at a different phase of the retry window (#2538) instead of every
        // detached lane retrying in the same render period. Counted over ALL
        // lanes, not ring lanes only — distinct indices are all the phase spread
        // needs.
        let lane_count = config.input_renderers.len();
        for (lane_index, (label, pcm_name)) in config
            .input_renderers
            .iter()
            .zip(&config.input_pcms)
            .enumerate()
        {
            // Build a per-input resampler on the configured clock-crossing lane
            // when EITHER the input resampler OR USB DIRECT is enabled: both
            // steer this lane to the DAC clock, and direct has no aloop catch-up
            // fallback so it MUST own a resampler. A construction failure
            // degrades to `None` with a warning rather than failing the daemon.
            let resampler = if config.lane_wants_resampler(label) {
                build_lane_resampler(label, config)
            } else {
                None
            };
            // USB DIRECT: this lane reads hw:UAC2Gadget directly instead of its
            // aloop substream. The open is best-effort — a gadget-absent lane
            // starts Absent and renders silence with a bounded reopen retry. The
            // fail-hard "every input required" contract is exempted ONLY here.
            let is_direct =
                config.usb_direct_enabled && label == &config.input_resampler_lane_label;
            // Renderer-ingress RING lane; best-effort like the direct lane. DIRECT
            // wins if a box somehow armed both on one lane, so at most one of
            // `direct`/`ring` is ever `Some` by construction.
            let is_ring = !is_direct && config.lane_is_renderer_ring(label);
            let input = if is_direct {
                open_direct_input(label, pcm_name, config, resampler)
            } else if is_ring {
                ring_capture::open_ring_input(label, config, resampler, lane_index, lane_count)
            } else {
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
            };
            info!(
                "event=fanin.input.opened label={} pcm={} period_frames={} buffer_frames={} direct={} source={}",
                label,
                // A ring lane's real source is its ring path, not the aloop name
                // it ignores; `Input::pcm_name` already carries that, so report it
                // rather than the config entry.
                input.pcm_name,
                config.period_frames,
                config.input_buffer_frames,
                is_direct,
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
            config.input_resampler_enabled || config.usb_direct_enabled,
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
            sample_format: config.ring_wire_format.sample_format_id(),
            period_frames: RING_SLOT_FRAMES,
            n_slots: config.ring_slots,
        };
        let writer = RingWriter::create_or_attach(&config.ring_path, geometry)
            .map_err(|e| ring_open_error(&config.ring_path, config.ring_wire_format.as_str(), e))
            .with_context(|| format!("opening fan-in→camilla SHM ring {}", config.ring_path))?;
        // NOTHING else is opened. The ring IS the program path: no ALSA playback
        // PCM is opened or fed (ADR-0100).
        let counters = RingCounters::new();
        let period_ns = (config.period_frames as u64) * 1_000_000_000 / (config.sample_rate as u64);
        // The wire this ring OBSERVABLY carries, read back from the header the
        // writer attached against — not echoed from config, so it is what the
        // reader on the other end sees.
        let attached = writer.geometry();
        let wire_format = ring_wire_format_label(attached.sample_format);
        info!(
            "event=fanin.ring.opened path={} slots={} slot_frames={} period_frames={} slots_per_step={} wire_format={} channels={}",
            config.ring_path,
            config.ring_slots,
            RING_SLOT_FRAMES,
            config.period_frames,
            config.period_frames / RING_SLOT_FRAMES,
            wire_format,
            attached.channels,
        );
        let ring_observability = RingObservability {
            path: config.ring_path.clone(),
            slots: config.ring_slots,
            wire_format,
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
            std::sync::mpsc::sync_channel::<TapEvent>(TAP_CHANNEL_CAPACITY);
        let direct_tap = DirectTapHook::new(
            Arc::new(TapState::default()),
            Arc::new(Mutex::new(TapConfig::default())),
            tap_sender,
        );
        // Allocated ONCE, and only for a ring whose attached header says S32LE.
        let output_is_wide = output.wire_is_wide();
        let ring_wide_payload = if output_is_wide {
            vec![0u8; period_samples * WIDE_BYTES_PER_SAMPLE]
        } else {
            Vec::new()
        };
        // FAIL CLOSED on a width disagreement, across BOTH axes (see
        // `program_width_disagreement`).
        let any_lane_is_wide = inputs.iter().any(|i| !i.read_buf_wide.is_empty());
        if let Some(error) = program_width_disagreement(ProgramWidthAudit {
            declared: program_width,
            output_is_wide,
            any_lane_is_wide,
        }) {
            return Err(error);
        }
        Ok(Self {
            inputs,
            output,
            program_width,
            sum_buf: vec![0i64; period_samples],
            output_buf: vec![0i16; period_samples],
            ring_wide_payload,
            content_meter_buf: vec![0i16; period_samples],
            frames_written: Arc::new(AtomicU64::new(0)),
            output_xrun_count: Arc::new(AtomicU64::new(0)),
            output_delay_frames: Arc::new(AtomicU64::new(OUTPUT_DELAY_UNAVAILABLE)),
            selected_input_index: Arc::new(AtomicI32::new(-2)),
            xrun_tx,
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
            host_clock_commanded_milli_ppm: Arc::new(AtomicI64::new(0)),
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
            // The cushion decay's live demand gauge, published by the decay
            // itself; `build_obs` subtracts it from the ratio above (#3466).
            decay_demand_milli_ppm: Arc::clone(&resampler.decay_demand_milli_ppm),
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
            commanded_milli_ppm: Arc::clone(&self.host_clock_commanded_milli_ppm),
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
        info!(
            "event=fanin.mixer.running inputs={} output_xruns=0",
            self.inputs.len(),
        );

        while !shutdown.load(Ordering::Relaxed) {
            self.step()?;
            heartbeat.bump_progress();
        }

        info!(
            "event=fanin.mixer.stopped frames_written={} output_xruns={}",
            self.frames_written.load(Ordering::Relaxed),
            self.output_xrun_count.load(Ordering::Relaxed),
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

        // Snapshot the REVERSE host-clock signals ONCE per period for the
        // cushion-decay tick below (a per-input read would self-borrow). `l0`
        // gates decay to the DLL's steady state; `commanded_ppm_abs` drives the
        // cascade guard. Both are inert (false / 0) when the servo thread is not
        // running, so decay never leaves the ceiling without the DLL.
        let decay_l0 = self.host_clock_ladder_l0.load(Ordering::Relaxed);
        let decay_commanded_ppm_abs =
            (self.host_clock_commanded_milli_ppm.load(Ordering::Relaxed) as f64 / 1000.0).abs();
        let selected_input = self.selected_input_index.load(Ordering::Relaxed);
        for (idx, input) in self.inputs.iter_mut().enumerate() {
            let frames = if input.direct.is_some() {
                // USB DIRECT lane: read hw:UAC2Gadget directly, narrow S32→S16,
                // feed the SAME resampler, render one DAC-paced period. The aloop
                // substream is never touched (`pcm` is None). The tap runs inline
                // over the converted slice inside this call.
                read_direct_and_render(input, period_frames, &mut self.direct_tap, &self.xrun_tx)
            } else if input.ring.is_some() {
                // Renderer-ingress RING lane: consume exactly one slot from this
                // lane's SHM ring. The slot IS one render period, and a bounded
                // ring cannot back up past its own depth the way an aloop capture
                // ring can, so this arm deliberately runs no catch-up resync.
                read_ring_and_render(input, period_frames)
            } else if input.resampler.is_some() {
                // ARMED clock-crossing lane. The resampler OWNS rate
                // reconciliation: read ALL available frames into it (DLL-steered
                // to the DAC clock) and render exactly one DAC-paced period. The
                // catch-up drain is bypassed on purpose — the resampler holds the
                // ring at a small fixed fill, with no sawtooth.
                read_into_resampler_and_render(input, period_frames, &self.xrun_tx)?
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
                read_input(input, period_frames, &self.xrun_tx)?
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
            let lane_rms_dbfs = if input.read_buf_wide.is_empty() {
                rms_dbfs_i16(&input.read_buf[..active])
            } else {
                jasper_resampler::rms_dbfs_i32(&input.read_buf_wide[..active])
            };
            input
                .rms_dbfs_x100
                .store((lane_rms_dbfs * 100.0).round() as i32, Ordering::Relaxed);
            // WAKE FADE-IN, tracking half (issue #3443). Read-only, and
            // deliberately for EVERY lane regardless of selection: silence is a
            // property of what the lane CAPTURES, so the run has to be counted on
            // the same periods the lane reads. Nothing is shaped here — the window
            // itself is applied after the gate below. The INFO line is bounded by
            // the state machine's own ARM_SILENCE_MS re-arm requirement.
            let woke = if input.read_buf_wide.is_empty() {
                input
                    .lane_fade
                    .observe(&input.read_buf[..active], self.period_frames)
            } else {
                input
                    .lane_fade
                    .observe(&input.read_buf_wide[..active], self.period_frames)
            };
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
                r.tick_decay(decay_l0, decay_commanded_ppm_abs);
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
            let reaches_sum = if input.read_buf_wide.is_empty() {
                input.lane_fade.shape_period(
                    &mut input.read_buf[..active],
                    self.period_frames,
                    contributes,
                )
            } else {
                input.lane_fade.shape_period(
                    &mut input.read_buf_wide[..active],
                    self.period_frames,
                    contributes,
                )
            };
            if !reaches_sum {
                continue;
            }
            // Only sum the samples actually read. `read_input` zero-pads the tail
            // so reading the full period would also be safe; the explicit bound
            // just saves saturating_add calls on a silent input.
            if input.read_buf_wide.is_empty() {
                mix_into(
                    &mut self.sum_buf[..active],
                    &input.read_buf[..active],
                    self.program_width,
                );
            } else {
                // A spine-scale lane enters with no shift and no narrowing, so
                // the low bits a hi-res source sent reach the summed write.
                mix_into_wide(
                    &mut self.sum_buf[..active],
                    &input.read_buf_wide[..active],
                    self.program_width,
                );
            }
        }
        if let Some(tts) = self.tts.as_mut() {
            saturate_to_i16(
                &self.sum_buf,
                &mut self.content_meter_buf,
                self.program_width,
            );
            tts.observe_content_period(&self.content_meter_buf);
        }
        // Apply the program-lane duck as a per-sample ramp toward the period
        // target, so a ~25 dB duck engages/releases smoothly rather than stepping
        // at the period boundary (the click/pump artifact).
        if program_target == 1.0 && self.program_duck_current == 1.0 {
            // no duck — common path, nothing to do
        } else if program_target == self.program_duck_current {
            apply_gain_to_sum(
                &mut self.sum_buf,
                self.program_duck_current,
                self.program_width,
            );
        } else {
            self.program_duck_current = ramp_program_duck(
                &mut self.sum_buf,
                CHANNELS as usize,
                self.program_duck_current,
                program_target,
                self.program_duck_attack_step,
                self.program_duck_release_step,
                self.program_width,
            );
        }
        if let Some(tts) = self.tts.as_mut() {
            tts.mix_period(&mut self.sum_buf, self.program_width);
        }

        saturate_to_i16(&self.sum_buf, &mut self.output_buf, self.program_width);

        // On an S32LE wire, build the wide slot payload from the SAME post-duck
        // post-TTS sum. No scale change happens here — the promotion is per lane,
        // in `mix_into`'s Wide arm — only the i64→i32 saturation and the explicit
        // little-endian order. The ring's own attached header is the ONE
        // predicate, the same `wire_is_wide()` that decides which payload
        // `write_ring_period` publishes, so the fill and the publish cannot
        // disagree about the wire.
        if self.output.wire_is_wide() {
            fill_wide_ring_payload(&self.sum_buf, &mut self.ring_wide_payload);
        }
        // Count only frames that actually ENTERED the ring — a fully-dropped
        // period (reader absent / stuck) adds nothing.
        let published_frames = write_ring_period(
            &mut self.output,
            &self.output_buf,
            &self.ring_wide_payload,
            self.period_frames,
        );
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
/// **Which payload the ring gets.** The ring publishes `output_buf` on an S16LE
/// wire and `wide_payload` on an S32LE one, chosen from the ring's OWN attached
/// header — never a parallel flag, so the bytes published cannot disagree with
/// the header the reader validated against. Exactly one of the two arguments is
/// read per call. The ring publish is the whole of this function's output.
fn write_ring_period(
    ring: &mut RingOutput,
    output_buf: &[i16],
    wide_payload: &[u8],
    period_frames: u32,
) -> u32 {
    let slots_per_step = period_frames / RING_SLOT_FRAMES;
    let samples_per_slot = (RING_SLOT_FRAMES as usize) * (CHANNELS as usize);
    // The ring's OWN attached geometry decides which payload is published.
    let wide = ring.wire_is_wide();
    let mut dropped_this_period = false;
    let mut published_slots: u32 = 0;
    for slot in 0..slots_per_step as usize {
        let start = slot * samples_per_slot;
        let outcome = if wide {
            let byte_start = start * WIDE_BYTES_PER_SAMPLE;
            let slot_bytes = samples_per_slot * WIDE_BYTES_PER_SAMPLE;
            ring.writer
                .publish_bytes(&wide_payload[byte_start..byte_start + slot_bytes])
        } else {
            ring.writer
                .publish(&output_buf[start..start + samples_per_slot])
        };
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

    /// The lane's renderer-ingress ring observability handles for the STATUS
    /// `ring{}` block, or `None` when this is not a ring lane.
    pub fn ring_observability(&self) -> Option<ring_capture::RingLaneObservability> {
        self.ring_obs.clone()
    }

    /// This lane's transport — the ONE derivation of STATUS's `source` field.
    ///
    /// Read off the lane-source `Option`s themselves rather than a separate
    /// stored flag, so "which arm of the read dispatch does this lane take" and
    /// "what does `/state` say it takes" cannot disagree — `step()` matches on
    /// the same two `Option`s in the same precedence order.
    pub fn lane_source(&self) -> LaneSource {
        if self.direct.is_some() {
            LaneSource::Direct
        } else if self.ring.is_some() {
            LaneSource::Ring
        } else {
            LaneSource::Lane
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

/// The numeric scale fan-in's program sum carries this run — the ONE width
/// decision every stage between a lane's read and the summed write consults
/// (#2223).
///
/// # What the two scales mean
///
/// * [`ProgramWidth::Narrow`] — the sum is in the **i16 numeric scale**: full
///   scale is ±32768, and every renderer lane's `i16` sample is added as-is.
///   This is what the shipped default resolves to, and what `saturate_to_i16`
///   writes out.
/// * [`ProgramWidth::Wide`] — the sum is in the **i32 spine scale**: full scale
///   is ±2^31, an `i16` lane is promoted by `widen_i16_to_i32` at its sum entry,
///   and the USB DIRECT lane contributes its gadget samples untouched.
///
/// Promoting at each lane's SUM ENTRY (rather than left-justifying the finished
/// narrow sum) is what lets a lane that HAS more than 16 significant bits keep
/// them. For a lane that does not, the promotion is the same `<< 16` applied
/// earlier, so its contribution is unchanged sample for sample.
///
/// # Why the accumulator is i64
///
/// The narrow sum has ~16 bits of headroom above full scale (two full-scale
/// lanes legitimately reach 65534 in an `i32`), and the program duck can bring
/// an over-full-scale sum back into range before the write — so that headroom is
/// audible, not theoretical. A spine-scale sum in an `i32` would have NO
/// headroom: two full-scale lanes would saturate at the mix, and a subsequent
/// 25 dB duck would then be attenuating an already-clipped value. `i64` keeps
/// the same headroom argument true at both widths (32 bits of it at spine
/// scale), at the cost of 4 more bytes per sample of per-period scratch —
/// 2 KiB at the default 256-frame stereo period, allocated once in
/// `Mixer::new` — and no measurable arithmetic on a 64-bit ARM core. It also
/// rules out clamp-before-shift: `widen_i16_to_i32` into an `i64` cannot wrap.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ProgramWidth {
    /// The i16 numeric scale — an S16 wire, and the shipped default.
    Narrow,
    /// The i32 spine scale — an S32LE ring wire.
    Wide,
}

fn input_selected(selected_input: i32, input_index: usize, label: &str) -> bool {
    selected_input == -1 || selected_input == input_index as i32 || label == MEASUREMENT_LANE
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
        step_frames: config.input_resampler_cushion_decay_step_frames as u64,
        interval_ms: config.input_resampler_cushion_decay_interval_ms as u64,
        stability_ms: CUSHION_DECAY_STABILITY_MS,
        cascade_guard_ppm: CUSHION_DECAY_CASCADE_GUARD_PPM,
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
                    "decay=on floor={} step={} interval_ms={}",
                    config.input_resampler_cushion_decay_floor_frames,
                    config.input_resampler_cushion_decay_step_frames,
                    config.input_resampler_cushion_decay_interval_ms,
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

fn open_input(
    pcm_name: &str,
    label: &str,
    config: &Config,
    resampler: Option<LaneResampler>,
) -> Result<Input> {
    // Non-blocking so a silent renderer's substream doesn't stall the work loop;
    // read_input handles -EAGAIN as "no data, treat as silence".
    let pcm = PCM::new(pcm_name, Direction::Capture, true)
        .with_context(|| format!("opening capture PCM {}", pcm_name))?;
    configure_pcm(&pcm, config, config.input_buffer_frames)
        .with_context(|| format!("configuring capture PCM {}", pcm_name))?;
    // Start the stream so reads return data (or EAGAIN) instead of
    // blocking forever in the PREPARED state.
    pcm.start()
        .with_context(|| format!("starting capture PCM {}", pcm_name))?;
    let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
    Ok(Input {
        pcm: Some(pcm),
        direct: None,
        direct_opener: None,
        ring: None,
        ring_attacher: None,
        label: label.to_string(),
        pcm_name: pcm_name.to_string(),
        read_buf: vec![0i16; period_samples],
        read_buf_wide: spine_read_buf(config.program_wire_is_wide(), period_samples),
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
        rms_dbfs_x100: Arc::new(AtomicI32::new((RMS_DBFS_FLOOR * 100.0) as i32)),
        catchup_resync_frames: Arc::new(AtomicU64::new(0)),
        catchup_events: Arc::new(AtomicU64::new(0)),
        resampler,
        trim: TrimControl::new(),
        muted: Arc::new(AtomicBool::new(false)),
        direct_obs: None,
        ring_obs: None,
        lane_fade: LaneFade::for_lane(label, config.sample_rate),
    })
}

/// Zero a lane's period buffer from `from_sample` on — whichever width the lane
/// holds its period in.
///
/// Takes the two buffers rather than the `Input` so a caller can silence its
/// lane while holding a borrow of another of the lane's fields; every read path
/// silences with its PCM or its ring reader borrowed.
pub(super) fn silence_period(narrow: &mut [i16], wide: &mut [i32], from_sample: usize) {
    if wide.is_empty() {
        narrow[from_sample..].fill(0);
    } else {
        wide[from_sample..].fill(0);
    }
}

/// A lane's SPINE-SCALE period buffer — allocated on a wide wire, empty on a
/// narrow one. The ONE place that decides, for every lane source (aloop, ring,
/// USB direct), whether that lane carries its period at spine scale.
///
/// Non-empty `read_buf_wide` is not merely a buffer — it is the lane's OWN
/// width switch, read by the drain (which side of the capture fork), by the
/// render (which `render_period`), and by the sum (which entry). Inverting it
/// points every one of those at the wrong scale at once, which is why the
/// decision is a testable pure helper rather than an inline `if` on a `&Config`.
/// An empty `Vec` has no capacity, so a narrow box allocates nothing.
fn spine_read_buf(program_wire_is_wide: bool, period_samples: usize) -> Vec<i32> {
    if program_wire_is_wide {
        vec![0i32; period_samples]
    } else {
        Vec::new()
    }
}

/// Build the USB DIRECT lane. Opens `hw:UAC2Gadget` (or the override) with the
/// proven envelope; on failure the lane starts `Absent` and renders silence with
/// a bounded reopen retry, so a gadget-absent box never fails the daemon. The
/// aloop substream is NOT opened (`pcm: None`): this lane's audio comes only
/// from the gadget capture. Never returns `Err` — the fail-hard "every input
/// required" contract is exempted for this lane alone.
fn open_direct_input(
    label: &str,
    pcm_name: &str,
    config: &Config,
    resampler: Option<LaneResampler>,
) -> Input {
    let device = config.usb_direct_device.clone();
    let open_period = config.usb_direct_period_frames;
    // The buffer the lane ACTUALLY negotiated at open; the request is
    // `resolve_direct_buffer_frames(open_period)`, but the kernel may round
    // `set_buffer_size_near` up, so seed from the request and overwrite with the
    // negotiated size on a successful open. Absent-at-startup keeps the request
    // as a best-effort placeholder (present=false makes the number advisory).
    let buffer_frames = Arc::new(AtomicU64::new(
        resolve_direct_buffer_frames(open_period) as u64
    ));
    let present = Arc::new(AtomicBool::new(false));
    let opens = Arc::new(AtomicU64::new(0));
    let retries = Arc::new(AtomicU64::new(0));
    let direct = match open_direct_capture(&device, open_period) {
        Ok((pcm, negotiated_buffer)) => {
            present.store(true, Ordering::Relaxed);
            opens.fetch_add(1, Ordering::Relaxed);
            buffer_frames.store(negotiated_buffer as u64, Ordering::Relaxed);
            info!(
                "event=fanin.usb_direct.present device={} period_frames={} buffer_frames={} (initial open) opens=1 retries=0",
                device, open_period, negotiated_buffer,
            );
            DirectCapture::Present(pcm)
        }
        Err(e) => {
            // Gadget absent at startup (source not yet advertised, unplugged
            // host, or another process holds hw:UAC2Gadget). Not fatal: the lane
            // renders silence and retries on its own cadence.
            warn!(
                "event=fanin.usb_direct.absent device={} errno={} detail={:#} (startup; will retry ~every {}s)",
                device,
                errno_of(&e),
                e,
                DIRECT_REOPEN_RETRY_PERIODS * (config.period_frames as u64) / (config.sample_rate.max(1) as u64),
            );
            DirectCapture::Absent {
                periods_until_retry: DIRECT_REOPEN_RETRY_PERIODS,
            }
        }
    };
    let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
    let read_buf_wide = spine_read_buf(config.program_wire_is_wide(), period_samples);
    // The deferred device-open channel (#2533). Spawned at construction, before
    // `main` calls `mlockall`, like every other fan-in helper thread. A spawn
    // failure leaves the lane WITHOUT self-heal rather than restoring an inline
    // `snd_pcm_open` inside the render loop's 5.33 ms period budget.
    let reopen_pending = Arc::new(AtomicBool::new(false));
    let direct_opener = match direct_capture::DirectOpener::spawn(Arc::clone(&reopen_pending)) {
        Ok(opener) => Some(opener),
        Err(e) => {
            warn!(
                "event=fanin.usb_direct.opener_unavailable device={} detail={} — the gadget \
                 capture will NOT be reopened after a loss until fan-in restarts (audio \
                 unaffected; the lane renders silence)",
                device, e,
            );
            None
        }
    };
    Input {
        // The direct lane does NOT open its aloop substream — its only source
        // is the gadget capture in `direct`.
        pcm: None,
        direct: Some(direct),
        direct_opener,
        ring: None,
        ring_attacher: None,
        label: label.to_string(),
        pcm_name: pcm_name.to_string(),
        read_buf: vec![0i16; period_samples],
        read_buf_wide,
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
        rms_dbfs_x100: Arc::new(AtomicI32::new((RMS_DBFS_FLOOR * 100.0) as i32)),
        catchup_resync_frames: Arc::new(AtomicU64::new(0)),
        catchup_events: Arc::new(AtomicU64::new(0)),
        resampler,
        trim: TrimControl::new(),
        muted: Arc::new(AtomicBool::new(false)),
        direct_obs: Some(DirectObservability {
            device,
            period_frames: open_period,
            buffer_frames,
            present,
            streaming: Arc::new(AtomicBool::new(false)),
            stream_starts: Arc::new(AtomicU64::new(0)),
            stream_stops: Arc::new(AtomicU64::new(0)),
            notify_attempts: Arc::new(AtomicU64::new(0)),
            notify_failures: Arc::new(AtomicU64::new(0)),
            opens,
            retries,
            reopen_pending,
            reopens: Arc::new(AtomicU64::new(0)),
            zero_avail_streak: Arc::new(AtomicU64::new(0)),
            frames_flowed_since_open: Arc::new(AtomicBool::new(false)),
            liveness_last_checked_drain: Arc::new(AtomicU64::new(0)),
            card_gen_reopens: Arc::new(AtomicU64::new(0)),
            drain_stats: DrainStats::new(),
        }),
        ring_obs: None,
        lane_fade: LaneFade::for_lane(label, config.sample_rate),
    }
}

/// Open the USB DIRECT capture PCM with the hardware-validated gadget envelope —
/// deliberately NOT fanin's aloop-tuned `configure_pcm`, which sets an exact
/// buffer. S32_LE 2ch 48k, `set_period_size(open_period, Nearest)`,
/// `set_buffer_size_near(resolve_direct_buffer_frames(open_period))`, then the
/// post-negotiation checks. `open_period` is 256 (the on-device-proven value) or
/// the `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES` override. Non-blocking and
/// `start()`ed so reads return data / EAGAIN.
///
/// Returns `(open PCM, negotiated buffer frames)`; the second element is the live
/// `hwp.get_buffer_size()`, so STATUS reports the buffer the PCM is really
/// running rather than the requested size. On failure returns an `alsa::Error`
/// the caller maps to the `Absent` state.
fn open_direct_capture(
    device: &str,
    open_period: u32,
) -> std::result::Result<(PCM, u32), alsa::Error> {
    let want_buffer = resolve_direct_buffer_frames(open_period);
    let pcm = PCM::new(device, Direction::Capture, true)?;
    let negotiated_buffer;
    {
        let hwp = HwParams::any(&pcm)?;
        hwp.set_channels(CHANNELS)?;
        hwp.set_rate(SAMPLE_RATE_HZ, ValueOr::Nearest)?;
        hwp.set_format(Format::S32LE)?;
        hwp.set_access(Access::RWInterleaved)?;
        hwp.set_period_size(open_period as i64, ValueOr::Nearest)?;
        hwp.set_buffer_size_near(want_buffer as i64)?;
        let rate = hwp.get_rate()?;
        let period = hwp.get_period_size()? as u32;
        let buffer = hwp.get_buffer_size()? as u32;
        pcm.hw_params(&hwp)?;
        // Rate/period MUST land exactly; the buffer is warn-on-near-mismatch but
        // must clear the deep-buffer + alignment floor. A validation failure
        // drops the PCM and returns an error, so the lane goes Absent.
        if let Err(reason) = direct_open_params_ok(rate, period, buffer, open_period) {
            warn!(
                "event=fanin.usb_direct.open_rejected device={} rate={} period={} buffer={} reason={}",
                device, rate, period, buffer, reason,
            );
            // An errno-bearing alsa::Error keeps the caller's Absent-path log
            // shape consistent. EINVAL = "negotiated an unusable geometry".
            return Err(alsa::Error::new("direct_open_params", libc::EINVAL));
        }
        if buffer != want_buffer {
            warn!(
                "event=fanin.usb_direct.buffer_near device={} requested_frames={} negotiated_frames={}",
                device, want_buffer, buffer,
            );
        }
        negotiated_buffer = buffer;
    }
    pcm.start()?;
    Ok((pcm, negotiated_buffer))
}

/// Pure post-negotiation validation of the direct capture geometry,
/// unit-testable without ALSA. Rate must be exactly 48000 and period exactly the
/// requested `want_period`; buffer must clear the deep-buffer floor (≥
/// `DIRECT_BUFFER_MIN_PERIODS` periods AND ≥ `DIRECT_BUFFER_MIN_FRAMES`) and be
/// a whole multiple of the period, since a fractional buffer would shear.
/// Returns the rejection reason string on failure.
fn direct_open_params_ok(
    rate: u32,
    period: u32,
    buffer: u32,
    want_period: u32,
) -> std::result::Result<(), String> {
    if rate != SAMPLE_RATE_HZ {
        return Err(format!("rate {rate} != 48000"));
    }
    if period != want_period {
        return Err(format!("period {period} != {want_period}"));
    }
    let min_buffer = period
        .saturating_mul(DIRECT_BUFFER_MIN_PERIODS)
        .max(DIRECT_BUFFER_MIN_FRAMES);
    if buffer < min_buffer {
        return Err(format!(
            "buffer {buffer} < deep-buffer floor ({min_buffer}: max({}×period, {}))",
            DIRECT_BUFFER_MIN_PERIODS, DIRECT_BUFFER_MIN_FRAMES,
        ));
    }
    if period == 0 || buffer % period != 0 {
        return Err(format!("buffer {buffer} not period-aligned to {period}"));
    }
    Ok(())
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

/// Pull the errno out of an `alsa::Error` for logging. It matches the `libc::E*`
/// constants the read paths compare against.
fn errno_of(e: &alsa::Error) -> i32 {
    e.errno()
}

fn configure_pcm(pcm: &PCM, config: &Config, buffer_frames: u32) -> Result<()> {
    // HwParams must be dropped before pcm.hw_params() is called, hence the
    // nested scope.
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(CHANNELS)
            .with_context(|| format!("set_channels({})", CHANNELS))?;
        hwp.set_rate(config.sample_rate, ValueOr::Nearest)
            .with_context(|| format!("set_rate({})", config.sample_rate))?;
        let format = lane_capture_format(config.program_wire_is_wide());
        hwp.set_format(format)
            .with_context(|| format!("set_format({:?})", format))?;
        hwp.set_access(Access::RWInterleaved)
            .context("set_access(RWInterleaved)")?;
        hwp.set_period_size(config.period_frames as i64, ValueOr::Nearest)
            .with_context(|| format!("set_period_size({})", config.period_frames))?;
        hwp.set_buffer_size(buffer_frames as i64)
            .with_context(|| format!("set_buffer_size({})", buffer_frames))?;
        pcm.hw_params(&hwp).context("installing HwParams")?;
    }
    Ok(())
}

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
    // Non-direct lanes always have Some(pcm); the direct lane never reaches
    // this path (it routes to read_direct_and_render). Guard defensively.
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

    // Discard whole periods into the lane's existing period scratch, at the
    // width that lane holds. read_input overwrites the same buffer next, so
    // trashing it here is safe.
    let discarded_frames = if input.read_buf_wide.is_empty() {
        match pcm.io_i16() {
            Ok(io) => discard_periods(&io, &mut input.read_buf, to_drain),
            Err(_) => return,
        }
    } else {
        match pcm.io_i32() {
            Ok(io) => discard_periods(&io, &mut input.read_buf_wide, to_drain),
            Err(_) => return,
        }
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
/// armed lane: `read_into_resampler_and_render` already drains every frame ALSA
/// reports ready each period, so the kernel ring is held shallow by design. The
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
/// capture cursor. Runs inline in `read_direct_and_render` over the converted
/// S16 slice BEFORE `push_input`.
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
        if self.sender.try_send(event).is_err() {
            self.state.note_dropped();
        }
    }
}

/// `CLOCK_MONOTONIC` in nanoseconds — the direct tap's ingress timeline. The tap
/// and the Python mic harness both read `CLOCK_MONOTONIC` on the same Pi; that
/// shared timeline is the only reason their cross-process subtraction is valid.
/// On a syscall failure this returns 0 rather than crashing the work loop; a
/// stray 0-anchored event is dropped by the harness's pairing window.
fn monotonic_ns() -> i128 {
    let mut ts = MaybeUninit::<libc::timespec>::uninit();
    let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, ts.as_mut_ptr()) };
    if rc != 0 {
        return 0;
    }
    let ts = unsafe { ts.assume_init() };
    (ts.tv_sec as i128) * 1_000_000_000 + (ts.tv_nsec as i128)
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
fn read_input(
    input: &mut Input,
    requested_frames: usize,
    xrun_tx: &Sender<XrunEvent>,
) -> Result<usize> {
    // Non-direct lanes always have Some(pcm); the direct lane never reaches
    // this path. A None here means a silent lane — render silence.
    let Some(pcm) = input.pcm.as_ref() else {
        silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
        return Ok(0);
    };
    // Read at the width this lane holds its period in. The PCM was opened at
    // that same width (`lane_capture_format`), so the typed IO handle can never
    // disagree with the wire.
    let read = if input.read_buf_wide.is_empty() {
        let io = pcm.io_i16().context("getting i16 IO handle for input")?;
        io.readi(&mut input.read_buf)
    } else {
        let io = pcm.io_i32().context("getting i32 IO handle for input")?;
        io.readi(&mut input.read_buf_wide)
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
                silence_period(
                    &mut input.read_buf,
                    &mut input.read_buf_wide,
                    frames * (CHANNELS as usize),
                );
            }
            Ok(frames)
        }
        Err(e) => match classify_pcm_errno(e.errno()) {
            PcmIoFate::WouldBlock => {
                // No data ready: the renderer is idle, or has not opened its
                // substream yet.
                silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
                Ok(0)
            }
            PcmIoFate::Xrun => {
                // Overrun: the renderer produced faster than we drained.
                let count = input.xrun_count.fetch_add(1, Ordering::Relaxed) + 1;
                warn!(
                    "event=fanin.xrun source=input label={} count={}",
                    input.label, count,
                );
                // A send error means the receiver was dropped (shutdown in
                // progress); fine to ignore.
                let _ = xrun_tx.send(XrunEvent {
                    source: XrunSource::Input,
                    label: input.label.clone(),
                    frames: requested_frames as u32,
                    count,
                });
                pcm.try_recover(e, true).context("recovering input xrun")?;
                silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
                Ok(0)
            }
            PcmIoFate::Fatal => Err(e).context(format!(
                "reading from input {} ({})",
                input.label, input.pcm_name
            )),
        },
    }
}

/// Cap on period-equivalent work for the ARMED lane drain in one `step()` call.
/// Like `CATCHUP_MAX_DRAIN_PERIODS`, this bounds syscall work per period so a
/// pathological `avail` (driver fault) cannot spin the hot loop. Frames beyond
/// the cap stay in the kernel ring and are read next period — the resampler's
/// own ring is the rate buffer, so leaving a little behind is harmless.
const RESAMPLER_MAX_READ_PERIODS: i64 = 64;

/// Return the bounded number of currently readable frames that the armed-lane
/// drain should pull into the resampler this period. Pure helper for the
/// real-time cap math; ALSA I/O happens in `read_into_resampler_and_render`.
fn resampler_read_budget_frames(avail: Frames, period_frames: usize) -> usize {
    if avail <= 0 {
        return 0;
    }
    let max_frames = period_frames.saturating_mul(RESAMPLER_MAX_READ_PERIODS as usize);
    (avail as usize).min(max_frames)
}

fn recover_resampler_input_xrun(
    input: &mut Input,
    error: alsa::Error,
    period_frames: usize,
    xrun_tx: &Sender<XrunEvent>,
    operation: &str,
) -> Result<()> {
    let count = input.xrun_count.fetch_add(1, Ordering::Relaxed) + 1;
    warn!(
        "event=fanin.xrun source=input label={} count={} op={} (resampler lane)",
        input.label, count, operation,
    );
    let _ = xrun_tx.send(XrunEvent {
        source: XrunSource::Input,
        label: input.label.clone(),
        frames: period_frames as u32,
        count,
    });
    // The aloop resampler lane always has Some(pcm); only the direct lane is
    // None, and it uses recover_direct_xrun instead.
    let Some(pcm) = input.pcm.as_ref() else {
        silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
        return Ok(());
    };
    pcm.try_recover(error, true)
        .context("recovering resampler input xrun")?;
    // `try_recover` can leave a capture PCM in PREPARED. The ordinary read_input
    // path kicks that forward with its next readi(), but the resampler path
    // polls avail_update() before reading; without an explicit restart it can
    // sit at avail=0 forever after a startup xrun.
    if pcm.state() != State::Running {
        pcm.start()
            .with_context(|| format!("restarting resampler input {} after xrun", input.label))?;
    }
    if let Some(r) = input.resampler.as_mut() {
        r.reset();
    }
    silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
    Ok(())
}

/// Read all currently-available frames from an ARMED lane into its resampler,
/// then render exactly one DAC-paced period into `read_buf`. Returns the number
/// of real (non-silence) frames the render produced — `period_frames` when the
/// resampler is locked, `0` while it is priming or underfilled, which renders
/// the lane silent exactly as an idle renderer's substream does.
///
/// Rate reconciliation lives in the resampler (DLL-steered to the DAC clock),
/// so the catch-up drain is intentionally bypassed on this path.
///
/// RT-safety: bounded syscalls (`avail_update` probes plus reads of frames
/// already reported ready, capped at `RESAMPLER_MAX_READ_PERIODS` periods
/// total), no allocation (reads into the existing `read_buf` scratch, pushes
/// into the resampler's pre-sized ring), no blocking (non-blocking capture). An
/// `EPIPE`/`ESTRPIPE` overrun recovers + resets the resampler (a discontinuity)
/// and renders silence for this period.
fn read_into_resampler_and_render(
    input: &mut Input,
    period_frames: usize,
    xrun_tx: &Sender<XrunEvent>,
) -> Result<usize> {
    // The aloop resampler lane always has Some(pcm); the direct lane routes to
    // read_direct_and_render and never reaches here.
    if input.pcm.is_none() {
        silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
        return Ok(0);
    }
    let wide = !input.read_buf_wide.is_empty();
    let mut read_budget_remaining =
        period_frames.saturating_mul(RESAMPLER_MAX_READ_PERIODS as usize);
    if read_budget_remaining > 0 {
        // Drain every frame ALSA reports ready, including final partial periods,
        // re-checking after each snapshot so frames arriving during this step do
        // not sit in the kernel ring for a full extra render period. The total
        // work stays bounded by `read_budget_remaining`.
        let mut stop_drain = false;
        while read_budget_remaining > 0 && !stop_drain {
            let avail = match input
                .pcm
                .as_ref()
                .expect("aloop resampler lane always has Some(pcm)")
                .avail_update()
            {
                Ok(avail) => avail,
                Err(e) => match classify_pcm_errno(e.errno()) {
                    PcmIoFate::WouldBlock => break,
                    PcmIoFate::Xrun => {
                        recover_resampler_input_xrun(
                            input,
                            e,
                            period_frames,
                            xrun_tx,
                            "avail_update",
                        )?;
                        break;
                    }
                    PcmIoFate::Fatal => {
                        return Err(e).context(format!(
                            "querying resampler input {} ({})",
                            input.label, input.pcm_name
                        ));
                    }
                },
            };
            let mut frames_remaining =
                resampler_read_budget_frames(avail, period_frames).min(read_budget_remaining);
            if frames_remaining == 0 {
                break;
            }
            while frames_remaining > 0 {
                let frames_to_read = frames_remaining.min(period_frames);
                let samples_to_read = frames_to_read * (CHANNELS as usize);
                let read_result = {
                    let pcm = input
                        .pcm
                        .as_ref()
                        .expect("aloop resampler lane always has Some(pcm)");
                    if wide {
                        let io = pcm
                            .io_i32()
                            .context("getting i32 IO handle for resampler input")?;
                        io.readi(&mut input.read_buf_wide[..samples_to_read])
                    } else {
                        let io = pcm
                            .io_i16()
                            .context("getting i16 IO handle for resampler input")?;
                        io.readi(&mut input.read_buf[..samples_to_read])
                    }
                };
                match read_result {
                    Ok(0) => {
                        stop_drain = true;
                        break;
                    }
                    Ok(n) => {
                        input.frames_read.fetch_add(n as u64, Ordering::Relaxed);
                        let samples = n * (CHANNELS as usize);
                        if let Some(r) = input.resampler.as_mut() {
                            if wide {
                                r.push_input_wide(&input.read_buf_wide[..samples]);
                            } else {
                                r.push_input(&input.read_buf[..samples]);
                            }
                        }
                        frames_remaining = frames_remaining.saturating_sub(n);
                        read_budget_remaining = read_budget_remaining.saturating_sub(n);
                        // Short read means the ring emptied earlier than
                        // `avail_update` claimed; stop rather than spin.
                        if n < frames_to_read {
                            stop_drain = true;
                            break;
                        }
                    }
                    Err(e) => match classify_pcm_errno(e.errno()) {
                        PcmIoFate::WouldBlock => {
                            stop_drain = true;
                            break;
                        }
                        PcmIoFate::Xrun => {
                            // A discontinuity: reset the resampler so it re-primes
                            // from fresh input rather than interpolating across
                            // the gap.
                            recover_resampler_input_xrun(
                                input,
                                e,
                                period_frames,
                                xrun_tx,
                                "readi",
                            )?;
                            stop_drain = true;
                            break;
                        }
                        PcmIoFate::Fatal => {
                            return Err(e).context(format!(
                                "reading from resampler input {} ({})",
                                input.label, input.pcm_name
                            ));
                        }
                    },
                }
            }
        }
    }

    // Same resampling kernel either way — only the emit tail's rails differ:
    // `render_period_wide` rounds at i32 instead of dividing the spine-scale
    // accumulator back down to i16.
    let real_frames = match input.resampler.as_mut() {
        Some(r) => {
            if wide {
                r.render_period_wide(&mut input.read_buf_wide)
            } else {
                r.render_period(&mut input.read_buf)
            }
        }
        None => {
            // Unreachable: only called when resampler.is_some().
            silence_period(&mut input.read_buf, &mut input.read_buf_wide, 0);
            0
        }
    };
    Ok(real_frames)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Pure-function tests for the mix math. No ALSA needed.
    //
    // The per-lane RMS level helper (`rms_dbfs_i16` / `RMS_DBFS_FLOOR`) is now
    // shared from jasper-resampler; its pure-math vectors live in that crate's
    // suite. The combo-gate integration behaviour is still asserted here (and in
    // tests/test_usbsink_playing_rms_contract.py) via the level plumbed onto the
    // lane and the STATUS surface.

    // ---- Per-lane mix gate: selection + mute -----------------------------

    #[test]
    fn lane_mix_contributes_selection_only_when_unmuted() {
        // AUTO (-1): every lane contributes when unmuted.
        assert!(lane_mix_contributes(-1, 0, "usbsink", false));
        assert!(lane_mix_contributes(-1, 2, "usbsink", false));
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
        // the arbitration primitive: even an AUTO-summed or explicitly-selected
        // USB lane is silenced when mux mutes it.
        assert!(!lane_mix_contributes(-1, 2, "usbsink", true));
        assert!(!lane_mix_contributes(2, 2, "usbsink", true));
        assert!(!lane_mix_contributes(-2, 2, "usbsink", true));
        // Mute wins even over the always-pass correction lane, so the primitive
        // is total (mux only ever mutes usbsink, but the rule is not lane-special).
        assert!(!lane_mix_contributes(-1, 3, "correction", true));
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
            lane_mix_contributes(-1, 2, "usbsink", true),
            lane_mix_contributes(-1, 2, "usbsink", true),
        );
        assert!(lane_mix_contributes(-1, 2, "usbsink", false));
    }

    // ---- USB DIRECT pure helpers -----------------------------------------

    #[test]
    fn direct_open_params_accepts_bridge_envelope() {
        // Default geometry: period 256, buffer 768 = 3×period (the deep-buffer
        // floor).
        assert!(direct_open_params_ok(48_000, 256, 768, 256).is_ok());
        // Open period 64 with a DEEP buffer (768 = 12 periods, well over the
        // 3-period + 768-frame floor).
        assert!(direct_open_params_ok(48_000, 64, 768, 64).is_ok());
        // A larger negotiated buffer at the default period is fine (period-aligned).
        assert!(direct_open_params_ok(48_000, 256, 1024, 256).is_ok());
    }

    #[test]
    fn direct_open_params_rejects_off_envelope() {
        // Baseline passes; each of the following fails for the noted reason.
        assert!(direct_open_params_ok(48_000, 256, 768, 256).is_ok());
        // Wrong rate.
        assert!(direct_open_params_ok(44_100, 256, 768, 256).is_err());
        // Negotiated period drifted from the requested one.
        assert!(direct_open_params_ok(48_000, 128, 768, 256).is_err());
        // Buffer below the 768-frame deep floor.
        assert!(direct_open_params_ok(48_000, 256, 512, 256).is_err());
        // Buffer not period-aligned.
        assert!(direct_open_params_ok(48_000, 256, 700, 256).is_err());
        // A shallow 2-period buffer at period 64 (128 frames) clears 2×period
        // but not the deep floor.
        assert!(direct_open_params_ok(48_000, 64, 128, 64).is_err());
    }

    #[test]
    fn resolve_direct_buffer_frames_holds_deep_floor() {
        // Default period lands exactly on the 768-frame floor.
        assert_eq!(resolve_direct_buffer_frames(256), 768);
        // Small period is floored to ≥768 AND ≥3 periods, period-aligned:
        // 64 → 768 (12 periods, 768 ≥ max(192, 768)).
        assert_eq!(resolve_direct_buffer_frames(64), 768);
        // A period whose 3× exceeds 768 is driven by the period floor:
        // 512 → 1536 (3×512), still period-aligned.
        assert_eq!(resolve_direct_buffer_frames(512), 1536);
        // 320 → max(960, 768) = 960, already a whole multiple of 320? 960/320=3.
        assert_eq!(resolve_direct_buffer_frames(320), 960);
        // A period where the 768 floor is NOT a whole multiple rounds UP:
        // 200 → max(600, 768)=768 → ceil(768/200)*200 = 4*200 = 800.
        assert_eq!(resolve_direct_buffer_frames(200), 800);
        // Every resolved buffer must pass its own validator at that period.
        for p in [32u32, 64, 128, 200, 256, 320, 512, 1024] {
            let b = resolve_direct_buffer_frames(p);
            assert!(
                direct_open_params_ok(48_000, p, b, p).is_ok(),
                "resolved buffer {b} must validate at period {p}",
            );
        }
    }

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
        // NOTE: that the STATUS ioctl actually returns Err/Disconnected across a
        // real gadget rebuild (vs `avail_update` continuing to return Ok(0)) is
        // kernel behavior no unit test can pin; it is the on-device obligation —
        // `curl .../state | jq .audio_graph.fanin ... card_gen_reopens` must tick
        // across a `systemctl restart jasper-usbsink` on jts.local.
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

    #[test]
    fn direct_lane_narrows_via_shared_conversion() {
        // The direct lane uses jasper_resampler's narrowing. The pinned
        // sign-boundary vector is re-asserted here so a drift fails this suite too.
        assert_eq!(jasper_resampler::s32_high_word_to_s16(0), 0);
        assert_eq!(jasper_resampler::s32_high_word_to_s16(0x7fff_ffff), 0x7fff);
        assert_eq!(jasper_resampler::s32_high_word_to_s16(i32::MIN), i16::MIN);
        assert_eq!(jasper_resampler::s32_high_word_to_s16(-1), -1);
        assert_eq!(jasper_resampler::s32_high_word_to_s16(-65_536), -1);
        assert_eq!(jasper_resampler::s32_high_word_to_s16(-65_537), -2);
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
    fn mix_into_sums_two_inputs() {
        let mut sum = vec![0i64; 4];
        mix_into(&mut sum, &[100, 200, 300, 400], ProgramWidth::Narrow);
        mix_into(&mut sum, &[50, 50, 50, 50], ProgramWidth::Narrow);
        assert_eq!(sum, vec![150, 250, 350, 450]);
    }

    #[test]
    fn mix_into_saturates_at_i32_bounds_but_stays_room_for_i16_saturation() {
        // Two max-i16 inputs sum to 2 × 32767 = 65534 — well within i32.
        // Only saturate_to_i16 should clip; mix_into just accumulates.
        let mut sum = vec![0i64; 1];
        mix_into(&mut sum, &[i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut sum, &[i16::MAX], ProgramWidth::Narrow);
        assert_eq!(sum[0], 65534);
    }

    #[test]
    fn mix_into_cancels_positive_and_negative() {
        let mut sum = vec![0i64; 2];
        mix_into(&mut sum, &[5000, -3000], ProgramWidth::Narrow);
        mix_into(&mut sum, &[-5000, 3000], ProgramWidth::Narrow);
        assert_eq!(sum, vec![0, 0]);
    }

    #[test]
    fn apply_gain_to_sum_ducks_after_program_sum() {
        let mut sum = vec![20_000i64, -20_000, 1_500, -1_500];
        apply_gain_to_sum(&mut sum, 0.1, ProgramWidth::Narrow);
        assert_eq!(sum, vec![2_000, -2_000, 150, -150]);
    }

    #[test]
    fn duck_step_per_frame_matches_requested_time() {
        // 15 ms at 48 kHz = 720 frames for a full 0->1 traversal.
        let step = duck_step_per_frame(15, 48_000);
        assert!((step - 1.0 / 720.0).abs() < 1e-9);
        // A misconfigured 0 floors to 1 ms rather than dividing by zero.
        assert!(duck_step_per_frame(0, 48_000).is_finite());
        assert!(duck_step_per_frame(15, 0).is_finite());
    }

    #[test]
    fn ramp_program_duck_glides_it_does_not_step() {
        // A constant program signal, one period long. Ducking DOWN toward
        // 0.5 must NOT drop every frame to 0.5 at once: early frames stay near
        // full level and the level descends monotonically.
        let channels = 2usize;
        let frames = 64usize;
        let mut sum = vec![10_000i64; frames * channels];
        // attack_step chosen so it takes ~the whole period to reach target.
        let attack = (1.0 - 0.5) / (frames as f32);
        let current = ramp_program_duck(
            &mut sum,
            channels,
            1.0,
            0.5,
            attack,
            1.0,
            ProgramWidth::Narrow,
        );
        // First frame is essentially un-ducked (no instantaneous 25 dB drop).
        assert!(
            sum[0] > 9_800,
            "onset stepped instead of ramping: {}",
            sum[0]
        );
        // Level descends monotonically frame to frame.
        let frame_val = |f: usize| sum[f * channels];
        for f in 1..frames {
            assert!(
                frame_val(f) <= frame_val(f - 1),
                "duck ramp not monotonic at frame {f}"
            );
        }
        // Landed at (or approaching) the target by the end.
        assert!(
            (current - 0.5).abs() < 0.02,
            "did not reach target: {current}"
        );
        assert!(frame_val(frames - 1) < 6_000);
    }

    #[test]
    fn ramp_program_duck_release_returns_to_unity_and_stops_scaling() {
        // Releasing UP from a ducked 0.5 back to 1.0: once it lands on 1.0
        // it must stop scaling entirely (samples pass through unchanged).
        let channels = 2usize;
        let frames = 8usize;
        let mut sum = vec![10_000i64; frames * channels];
        // release_step large enough to reach 1.0 within the first frame.
        let current =
            ramp_program_duck(&mut sum, channels, 0.5, 1.0, 1.0, 1.0, ProgramWidth::Narrow);
        assert_eq!(current, 1.0);
        // The last frame, fully released, is unscaled.
        assert_eq!(sum[(frames - 1) * channels], 10_000);
    }

    #[test]
    fn ramp_program_duck_steady_state_is_flat_multiply_equivalent() {
        // When current already equals target, every frame scales by the same
        // constant — identical to apply_gain_to_sum (the step() steady-state
        // fast path uses apply_gain_to_sum; this guards their equivalence).
        let channels = 2usize;
        let mut ramped = vec![20_000i64, -20_000, 1_500, -1_500];
        let mut flat = ramped.clone();
        let current = ramp_program_duck(
            &mut ramped,
            channels,
            0.1,
            0.1,
            0.01,
            0.01,
            ProgramWidth::Narrow,
        );
        apply_gain_to_sum(&mut flat, 0.1, ProgramWidth::Narrow);
        assert_eq!(current, 0.1);
        assert_eq!(ramped, flat);
    }

    #[test]
    fn saturate_to_i16_clamps_positive_overflow() {
        let mut out = vec![0i16; 1];
        saturate_to_i16(&[100_000], &mut out, ProgramWidth::Narrow);
        assert_eq!(out[0], i16::MAX);
    }

    #[test]
    fn saturate_to_i16_clamps_negative_overflow() {
        let mut out = vec![0i16; 1];
        saturate_to_i16(&[-100_000], &mut out, ProgramWidth::Narrow);
        assert_eq!(out[0], i16::MIN);
    }

    #[test]
    fn saturate_to_i16_passes_in_range_values() {
        let mut out = vec![0i16; 4];
        saturate_to_i16(&[0, 1000, -1000, 32767], &mut out, ProgramWidth::Narrow);
        assert_eq!(out, vec![0, 1000, -1000, i16::MAX]);
    }

    #[test]
    fn mix_three_inputs_full_pipeline() {
        // Three inputs at ~1/3 max each: sum approaches max but
        // doesn't saturate. Models the realistic three-renderer
        // simultaneous-handover transient.
        let mut sum = vec![0i64; 4];
        mix_into(
            &mut sum,
            &[10_000, 10_000, 10_000, 10_000],
            ProgramWidth::Narrow,
        );
        mix_into(
            &mut sum,
            &[10_000, 10_000, 10_000, 10_000],
            ProgramWidth::Narrow,
        );
        mix_into(
            &mut sum,
            &[10_000, 10_000, 10_000, 10_000],
            ProgramWidth::Narrow,
        );
        let mut out = vec![0i16; 4];
        saturate_to_i16(&sum, &mut out, ProgramWidth::Narrow);
        assert_eq!(out, vec![30_000, 30_000, 30_000, 30_000]);
    }

    #[test]
    fn mix_three_max_inputs_saturates_output() {
        // Three max-positive inputs sum to 98_301, well above i16::MAX.
        // Saturation clips to 32767.
        let mut sum = vec![0i64; 2];
        mix_into(&mut sum, &[i16::MAX, i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut sum, &[i16::MAX, i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut sum, &[i16::MAX, i16::MAX], ProgramWidth::Narrow);
        let mut out = vec![0i16; 2];
        saturate_to_i16(&sum, &mut out, ProgramWidth::Narrow);
        assert_eq!(out, vec![i16::MAX, i16::MAX]);
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
    fn selected_input_passes_auto_selected_and_correction() {
        assert!(input_selected(-1, 0, "spotify"));
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

    use jasper_ring::{RingReader, SlotRead, SAMPLE_FORMAT_S16LE};
    use std::sync::atomic::AtomicU64 as TestAtomicU64;

    static RING_MIXER_TEST_SEQ: TestAtomicU64 = TestAtomicU64::new(0);

    fn ring_geometry(n_slots: u32) -> Geometry {
        ring_geometry_with_wire(n_slots, RingWireFormat::S16Le)
    }

    fn ring_geometry_with_wire(n_slots: u32, wire: RingWireFormat) -> Geometry {
        Geometry {
            rate: 48_000,
            channels: CHANNELS,
            sample_format: wire.sample_format_id(),
            period_frames: RING_SLOT_FRAMES,
            n_slots,
        }
    }

    fn tmp_ring_output(n_slots: u32, tag: &str) -> (RingOutput, String) {
        tmp_ring_output_with_wire(n_slots, tag, RingWireFormat::S16Le)
    }

    fn tmp_ring_output_with_wire(
        n_slots: u32,
        tag: &str,
        wire: RingWireFormat,
    ) -> (RingOutput, String) {
        let dir = std::env::temp_dir().join(format!(
            "jts-fanin-ring-{}-{}-{}",
            tag,
            std::process::id(),
            RING_MIXER_TEST_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("program.ring").to_string_lossy().into_owned();
        let writer =
            RingWriter::create_or_attach(&path, ring_geometry_with_wire(n_slots, wire)).unwrap();
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

    /// The narrow ring publishes `output_buf`, so the wide payload argument is
    /// empty on every S16LE call site — exactly what `step()` passes when the
    /// wide buffer was never allocated.
    const NO_WIDE_PAYLOAD: &[u8] = &[];

    fn cleanup_ring(path: &str) {
        let _ = std::fs::remove_file(path);
        if let Some(parent) = std::path::Path::new(path).parent() {
            let _ = std::fs::remove_dir(parent);
        }
    }

    /// Q2 (TTS/duck ride-along): the ring output receives the FINAL mixed period
    /// — post-duck AND post-TTS — verbatim. `step()` mixes TTS and applies the
    /// duck into sum_buf BEFORE saturating into output_buf, and `write_ring_period`
    /// publishes exactly that output_buf, so whatever the mix produced is what the
    /// ring reader sees. This test stands in a post-TTS-mixed period and asserts
    /// the reader reads back those exact bytes.
    #[test]
    fn ring_output_carries_post_duck_post_tts_period() {
        let period_frames = 256u32; // 2 slots of 128 frames
        let (mut ring, path) = tmp_ring_output(8, "tts_ridealong");
        let mut reader = RingReader::create_or_attach(&path, ring_geometry(8)).unwrap();
        // Prime the reader heartbeat so the writer takes the publish path.
        let slot_samples = (RING_SLOT_FRAMES as usize) * (CHANNELS as usize);
        let mut slot_out = vec![0i16; slot_samples];
        assert_eq!(reader.try_consume_slot(&mut slot_out), SlotRead::Empty);

        // Model step()'s output_buf: build a summed program, apply a duck, then
        // add a TTS contribution — the SAME order step() uses — and saturate.
        let total = (period_frames as usize) * (CHANNELS as usize);
        let mut sum = vec![0i64; total];
        mix_into(&mut sum, &vec![10_000i16; total], ProgramWidth::Narrow); // program lane
        apply_gain_to_sum(&mut sum, 0.5, ProgramWidth::Narrow); // duck (TTS active)
        for s in sum.iter_mut() {
            *s = s.saturating_add(4_000); // stand-in for tts.mix_period
        }
        let mut output_buf = vec![0i16; total];
        saturate_to_i16(&sum, &mut output_buf, ProgramWidth::Narrow); // expected: 5000 + 4000 = 9000

        let published_frames =
            write_ring_period(&mut ring, &output_buf, NO_WIDE_PAYLOAD, period_frames);
        // Two slots reached a live reader -> the full period is counted.
        assert_eq!(published_frames, period_frames);

        // The reader reads the two published slots back — byte-identical to the
        // post-duck post-TTS output_buf.
        let mut got = Vec::with_capacity(total);
        for _ in 0..(period_frames / RING_SLOT_FRAMES) {
            assert_eq!(reader.try_consume_slot(&mut slot_out), SlotRead::Filled);
            got.extend_from_slice(&slot_out);
        }
        assert_eq!(got, output_buf, "ring must carry the final mixed period");
        assert!(got.iter().all(|&s| s == 9_000), "post-duck+TTS value");
        // Counters reflect two published slots (a live reader, no drops).
        assert_eq!(ring.counters.published.load(Ordering::Relaxed), 2);
        assert_eq!(ring.counters.stuck_reader_drops.load(Ordering::Relaxed), 0);
        assert_eq!(ring.counters.drop_no_reader.load(Ordering::Relaxed), 0);
        assert!(!ring.counters.stall_active.load(Ordering::Relaxed));
        cleanup_ring(&path);
    }

    // --- Ring A wide (S32LE) wire. The wide path keys on the ring's own
    // ATTACHED header, never on a config default, so these tests build such a
    // ring explicitly; everything above builds a narrow one just as explicitly.
    // (The resolver's default is wide since the wide-wire flip — these fixtures
    // do not inherit it either way, which is what keeps both paths covered.)

    /// A period's worth of NARROW-scale mix sum, spanning the values the narrow
    /// path has to get right: silence, both i16 rails, the 65534
    /// two-full-scale-lanes sum that
    /// `mix_into_saturates_at_i32_bounds_but_stays_room_for_i16_saturation`
    /// blesses, its negative twin, and ordinary program levels.
    fn representative_sum(total: usize) -> Vec<i64> {
        let pattern = [0i64, 65_534, -65_536, 32_767, -32_768, 9_000, -9_000, 1];
        (0..total).map(|i| pattern[i % pattern.len()]).collect()
    }

    /// The SAME period at the spine scale a wide box's sum would carry — every
    /// lane's contribution `widen_i16_to_i32`'d at its sum entry. The promotion
    /// is applied to the FINISHED narrow sum here only because these are
    /// pure-function tests with no lanes; per sample it is the identical
    /// `<< 16`, which is exactly why doing it per lane in `mix_into` changes
    /// nothing for a lane that had no low bits to keep.
    fn representative_wide_sum(total: usize) -> Vec<i64> {
        representative_sum(total)
            .into_iter()
            .map(|s| s * (1i64 << 16))
            .collect()
    }

    /// The overflow pin: two full-scale lanes land at exactly twice one lane, in
    /// the right sign, with no wrap, at BOTH widths. An `i32` accumulator could
    /// not hold that — `65534 << 16` wraps the sign bit and turns the loudest
    /// possible program into non-monotonic fold-over — which is why the
    /// promotion happens at each lane's sum entry into an `i64`.
    #[test]
    fn two_full_scale_lanes_do_not_wrap_at_either_width() {
        // Narrow: unchanged — 65534, comfortably inside the accumulator.
        let mut narrow = vec![0i64; 1];
        mix_into(&mut narrow, &[i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut narrow, &[i16::MAX], ProgramWidth::Narrow);
        assert_eq!(narrow[0], 65_534);
        assert!(narrow[0] > 0, "a full-scale POSITIVE sum stays positive");

        // Wide: the same sum promoted, and crucially NOT the wrapped value an
        // i32 accumulator would have produced.
        let mut wide = vec![0i64; 1];
        mix_into(&mut wide, &[i16::MAX], ProgramWidth::Wide);
        mix_into(&mut wide, &[i16::MAX], ProgramWidth::Wide);
        assert_eq!(wide[0], 65_534i64 << 16);
        assert!(wide[0] > 0);
        assert_ne!(
            wide[0],
            (65_534i32).wrapping_shl(16) as i64,
            "the i32-wrap value must not be reachable"
        );

        // The negative twin.
        let mut wide_neg = vec![0i64; 1];
        mix_into(&mut wide_neg, &[i16::MIN], ProgramWidth::Wide);
        mix_into(&mut wide_neg, &[i16::MIN], ProgramWidth::Wide);
        assert_eq!(wide_neg[0], -65_536i64 << 16);
        assert!(wide_neg[0] < 0);
    }

    /// THE HEADROOM PROPERTY, and the reason the accumulator is `i64` rather
    /// than `i32` (see [`ProgramWidth`]).
    ///
    /// Two full-scale lanes legitimately exceed full scale, and the program duck
    /// can bring them back into range before the write. With an `i32`
    /// accumulator at spine scale the mix would saturate FIRST and the duck
    /// would then be attenuating an already-clipped value — audible distortion,
    /// not a rounding difference. Both widths must recover the same ducked
    /// level.
    #[test]
    fn the_sum_keeps_duck_recoverable_headroom_above_full_scale_at_both_widths() {
        let duck = 0.1f32;

        let mut narrow = vec![0i64; 1];
        mix_into(&mut narrow, &[i16::MAX], ProgramWidth::Narrow);
        mix_into(&mut narrow, &[i16::MAX], ProgramWidth::Narrow);
        apply_gain_to_sum(&mut narrow, duck, ProgramWidth::Narrow);
        // 65534 * 0.1 — recovered cleanly, well inside i16.
        assert_eq!(narrow[0], 6_553);

        let mut wide = vec![0i64; 1];
        mix_into(&mut wide, &[i16::MAX], ProgramWidth::Wide);
        mix_into(&mut wide, &[i16::MAX], ProgramWidth::Wide);
        apply_gain_to_sum(&mut wide, duck, ProgramWidth::Wide);
        let mut out = vec![0i16; 1];
        saturate_to_i16(&wide, &mut out, ProgramWidth::Wide);
        assert_eq!(
            out[0], 6_553,
            "the wide path must recover the same ducked level, not a clipped one"
        );
        // Stated as the failure it guards: an accumulator that saturated at the
        // MIX would leave the duck attenuating i32::MAX instead.
        let clipped_first = ((i32::MAX as f32) * duck).round() as i64;
        assert_ne!(
            wide[0], clipped_first,
            "a saturating-at-the-mix accumulator would land here"
        );
    }

    /// The promotion is exact at both rails: `i16::MIN << 16` is precisely
    /// `i32::MIN` and `i16::MAX << 16` is `0x7FFF_0000`, so no rail overflows and
    /// none is silently rounded. Asserted through the sum entry that performs it.
    #[test]
    fn the_wide_sum_entry_left_justifies_the_whole_i16_range() {
        let lane = [0i16, i16::MAX, i16::MIN, 1, -1];
        let mut sum = vec![0i64; lane.len()];
        mix_into(&mut sum, &lane, ProgramWidth::Wide);
        assert_eq!(
            sum,
            vec![
                0,
                0x7FFF_0000i64,
                i32::MIN as i64,
                0x0001_0000,
                -0x0001_0000
            ]
        );
    }

    /// The value a period's `i`th sample must publish on a wide wire, given what
    /// the SAME period publishes on a narrow one.
    ///
    /// Below full scale the two wires are the same number 16 bits apart. AT the
    /// positive clip rail they differ by exactly one i16 LSB, and that is
    /// correct rather than a rounding slip: the narrow wire's positive full
    /// scale is `32767`, whose left-justification is `0x7FFF_0000`, while the
    /// wide wire's own positive full scale is `i32::MAX` — one i16 step higher,
    /// because two's complement has one more negative code than positive. A
    /// wide wire that stopped at `0x7FFF_0000` would be refusing to use its own
    /// top code. The negative rail has no such asymmetry (`i16::MIN << 16` IS
    /// `i32::MIN`), and the difference only exists on samples that are already
    /// clipping.
    fn expected_wide_sample(narrow_sum_sample: i64, narrow_out_sample: i16) -> i32 {
        if narrow_sum_sample > i16::MAX as i64 {
            i32::MAX
        } else {
            (narrow_out_sample as i32) << 16
        }
    }

    /// A wide period built ONLY from i16 lanes carries exactly the information
    /// the narrow one does — every wide sample is the narrow sample moved 16
    /// bits up (modulo the one-LSB clip-rail asymmetry
    /// [`expected_wide_sample`] names), and narrowing it back returns the narrow
    /// bytes with no drift at all. That is what makes the promotion a scale
    /// change rather than a content change, and it is why flipping a box's wire
    /// cannot alter how an S16-only program sounds.
    ///
    /// Where the wide path now DIFFERS is that a lane with more than 16
    /// significant bits keeps them —
    /// `a_hi_res_direct_lane_keeps_its_low_bits_all_the_way_to_the_wide_payload`
    /// is that half of the claim.
    #[test]
    fn wide_payload_is_information_equivalent_to_the_narrow_payload() {
        let narrow_sum = representative_sum(64);
        let wide_sum = representative_wide_sum(64);
        let mut narrow = vec![0i16; narrow_sum.len()];
        saturate_to_i16(&narrow_sum, &mut narrow, ProgramWidth::Narrow);
        let mut wide = vec![0u8; wide_sum.len() * WIDE_BYTES_PER_SAMPLE];
        fill_wide_ring_payload(&wide_sum, &mut wide);
        let mut saw_clip_rail = false;
        for (i, &n) in narrow.iter().enumerate() {
            let bytes: [u8; WIDE_BYTES_PER_SAMPLE] = wide
                [i * WIDE_BYTES_PER_SAMPLE..(i + 1) * WIDE_BYTES_PER_SAMPLE]
                .try_into()
                .unwrap();
            let published = i32::from_le_bytes(bytes);
            assert_eq!(
                published,
                expected_wide_sample(narrow_sum[i], n),
                "sample {i} (narrow sum {})",
                narrow_sum[i],
            );
            if narrow_sum[i] > i16::MAX as i64 {
                saw_clip_rail = true;
                assert_eq!(
                    (published as i64) - (((n as i32) << 16) as i64),
                    65_535,
                    "the clip-rail difference must be exactly one i16 LSB",
                );
            }
        }
        assert!(
            saw_clip_rail,
            "the representative period must exercise the positive clip rail"
        );
        // Narrowing the wide sum back returns the narrow bytes EXACTLY, clip
        // rail included — `narrow_i32_to_i16_round` inverts the promotion.
        let mut round_tripped = vec![0i16; wide_sum.len()];
        saturate_to_i16(&wide_sum, &mut round_tripped, ProgramWidth::Wide);
        assert_eq!(round_tripped, narrow);
    }

    /// End-to-end through the real ring: the SAME mix sum published onto an
    /// S32LE ring and an S16LE ring, read back by a real `RingReader`, and the
    /// wide slots must be exactly the narrow slots left-justified. This is the
    /// test the byte API's correctness rides on — it fails if the wide payload
    /// is sliced at the wrong stride, published with the wrong length, or
    /// computed in the wrong order.
    #[test]
    fn wide_ring_slots_carry_the_left_justified_narrow_slots() {
        let period_frames = 256u32; // 2 slots of 128 frames
        let total = (period_frames as usize) * (CHANNELS as usize);
        let samples_per_slot = (RING_SLOT_FRAMES as usize) * (CHANNELS as usize);
        let slots = period_frames / RING_SLOT_FRAMES;
        // The narrow box's sum and the SAME period as a wide box's sum would
        // carry it — the promotion happens at each lane's sum entry now, so the
        // two wires are fed from differently-scaled accumulators rather than
        // from one accumulator converted twice.
        let sum = representative_sum(total);
        let wide_sum = representative_wide_sum(total);
        let mut output_buf = vec![0i16; total];
        saturate_to_i16(&sum, &mut output_buf, ProgramWidth::Narrow);
        let mut wide_payload = vec![0u8; total * WIDE_BYTES_PER_SAMPLE];
        fill_wide_ring_payload(&wide_sum, &mut wide_payload);

        // Narrow ring: publish output_buf, read i16 slots back.
        let (mut narrow_ring, narrow_path) = tmp_ring_output(8, "wire_narrow");
        let mut narrow_reader =
            RingReader::create_or_attach(&narrow_path, ring_geometry(8)).unwrap();
        let mut narrow_slot = vec![0i16; samples_per_slot];
        assert_eq!(
            narrow_reader.try_consume_slot(&mut narrow_slot),
            SlotRead::Empty,
            "priming the reader heartbeat"
        );
        assert_eq!(
            write_ring_period(
                &mut narrow_ring,
                &output_buf,
                NO_WIDE_PAYLOAD,
                period_frames
            ),
            period_frames
        );
        let mut narrow_read = Vec::with_capacity(total);
        for _ in 0..slots {
            assert_eq!(
                narrow_reader.try_consume_slot(&mut narrow_slot),
                SlotRead::Filled
            );
            narrow_read.extend_from_slice(&narrow_slot);
        }
        assert_eq!(narrow_read, output_buf);

        // Wide ring: the ring's OWN header selects the wide payload. `output_buf`
        // is still passed (it is the narrow wire's payload) and must go unread.
        let (mut wide_ring, wide_path) =
            tmp_ring_output_with_wire(8, "wire_wide", RingWireFormat::S32Le);
        assert!(wide_ring.wire_is_wide());
        let mut wide_reader = RingReader::create_or_attach(
            &wide_path,
            ring_geometry_with_wire(8, RingWireFormat::S32Le),
        )
        .unwrap();
        let slot_bytes = samples_per_slot * WIDE_BYTES_PER_SAMPLE;
        let mut wide_slot = vec![0u8; slot_bytes];
        assert_eq!(
            wide_reader.try_consume_slot_bytes(&mut wide_slot),
            SlotRead::Empty,
            "priming the reader heartbeat"
        );
        assert_eq!(
            write_ring_period(&mut wide_ring, &output_buf, &wide_payload, period_frames),
            period_frames
        );
        let mut wide_read: Vec<i32> = Vec::with_capacity(total);
        for _ in 0..slots {
            assert_eq!(
                wide_reader.try_consume_slot_bytes(&mut wide_slot),
                SlotRead::Filled
            );
            for chunk in wide_slot.chunks_exact(WIDE_BYTES_PER_SAMPLE) {
                let bytes: [u8; WIDE_BYTES_PER_SAMPLE] = chunk.try_into().unwrap();
                wide_read.push(i32::from_le_bytes(bytes));
            }
        }

        assert_eq!(wide_read.len(), narrow_read.len());
        for (i, (&w, &n)) in wide_read.iter().zip(narrow_read.iter()).enumerate() {
            assert_eq!(
                w,
                expected_wide_sample(sum[i], n),
                "slot sample {i}: wide wire must carry the narrow wire's sample",
            );
        }
        // The 65534 over-rail sum actually appears in this period, so the clip
        // behaviour is exercised end-to-end and not only in the pure unit test
        // above.
        assert!(
            wide_read.contains(&i32::MAX),
            "the representative period must include the saturated full-scale rail"
        );
        cleanup_ring(&narrow_path);
        cleanup_ring(&wide_path);
    }

    /// A narrow ring stays on the typed publish and reports itself narrow, so
    /// the wide branch is unreachable on a narrow ring — whatever the box's
    /// resolver would have answered. The header is the authority here, and
    /// since the wide-wire flip that distinction is the whole point: a narrow
    /// ring is now an operator's pin (or a pre-flip file), not the default.
    #[test]
    fn narrow_ring_reports_its_wire_and_takes_the_typed_publish() {
        let (ring, path) = tmp_ring_output(2, "wire_default");
        assert!(!ring.wire_is_wide());
        assert_eq!(
            ring.writer.geometry().sample_format,
            SAMPLE_FORMAT_S16LE,
            "this fixture builds a NARROW ring explicitly; the writer must \
             report the header it attached, not a resolved default"
        );
        cleanup_ring(&path);
    }

    /// STATUS reports the OBSERVED wire, mapped through the one vocabulary
    /// owner. An id outside the accept-set cannot reach a live ring, so it
    /// renders as `unknown` rather than guessing.
    #[test]
    fn ring_wire_format_label_names_the_observed_header_id() {
        assert_eq!(ring_wire_format_label(SAMPLE_FORMAT_S16LE), "S16_LE");
        assert_eq!(ring_wire_format_label(SAMPLE_FORMAT_S32LE), "S32_LE");
        assert_eq!(ring_wire_format_label(99), "unknown");
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

    /// Reader-absent: `write_ring_period` free-run-drops and paces (never
    /// hot-spins). Counters reflect the drops; occupancy stays bounded. This
    /// stands in the "CamillaDSP not yet up / reloading" turn.
    #[test]
    fn ring_output_free_runs_and_paces_without_reader() {
        let period_frames = 256u32;
        let (mut ring, path) = tmp_ring_output(2, "no_reader");
        // No reader attached: reader_pid == 0.
        let total = (period_frames as usize) * (CHANNELS as usize);
        let output_buf = vec![7i16; total];

        // Fill the ring, then publish several more periods. Each free-run-drops
        // the oldest; the pacer sleeps each period out to its deadline. Bound
        // the wall time so that sleep can't wedge the loop.
        let start = std::time::Instant::now();
        let mut per_period_published = Vec::with_capacity(4);
        for _ in 0..4 {
            per_period_published.push(write_ring_period(
                &mut ring,
                &output_buf,
                NO_WIDE_PAYLOAD,
                period_frames,
            ));
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
        let output_buf = vec![9i16; total];
        // Fill the ring (first period publishes both slots to the live reader).
        assert_eq!(
            write_ring_period(&mut ring, &output_buf, NO_WIDE_PAYLOAD, period_frames),
            period_frames,
        );

        // PRE-GRACE: the ring is full and the reader is wedged but within grace →
        // each publish pays the bounded ~21 ms wait, so the period is slow.
        let pre = std::time::Instant::now();
        assert_eq!(
            write_ring_period(&mut ring, &output_buf, NO_WIDE_PAYLOAD, period_frames),
            0
        );
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
        assert_eq!(
            write_ring_period(&mut ring, &output_buf, NO_WIDE_PAYLOAD, period_frames),
            0
        );
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

    /// A known 24-bit sample in S24-in-S32 placement, and both 24-bit rails —
    /// the same vectors the lane-level fixture and the `jasper-resampler`
    /// contract test use, restated here because THIS is where the claim has to
    /// land: the summed write.
    const U2_HIRES_VECTORS: [i32; 3] = [0x1234_5600, 0x7fff_ff00, i32::MIN];

    /// THE EXIT-GATE FIXTURE: a hi-res sample injected where the DIRECT capture
    /// hands its period to the mixer survives — low bits and all — into the
    /// bytes published on a wide wire.
    ///
    /// Driven through the REAL sum entry (`mix_into_wide`) and the REAL payload
    /// fill, so it fails if either reintroduces a narrowing, a shift, or a clamp
    /// into the i16 range.
    ///
    /// The contrast is the point: the same sample taken through the NARROW sum
    /// entry loses its low byte, and the test asserts the exact number of bits
    /// each route keeps rather than only that they differ.
    #[test]
    fn a_hi_res_direct_lane_keeps_its_low_bits_all_the_way_to_the_wide_payload() {
        for pattern in U2_HIRES_VECTORS {
            // The wide route: the lane's spine-scale period enters the sum
            // untouched and is published as-is.
            let mut sum = vec![0i64; 4];
            mix_into_wide(&mut sum, &[pattern; 4], ProgramWidth::Wide);
            let mut payload = vec![0u8; sum.len() * WIDE_BYTES_PER_SAMPLE];
            fill_wide_ring_payload(&sum, &mut payload);
            for (i, chunk) in payload.chunks_exact(WIDE_BYTES_PER_SAMPLE).enumerate() {
                let bytes: [u8; WIDE_BYTES_PER_SAMPLE] = chunk.try_into().unwrap();
                assert_eq!(
                    i32::from_le_bytes(bytes),
                    pattern,
                    "published sample {i} must be {pattern:#010x} bit for bit",
                );
            }

            // The narrow route, for contrast: the capture narrowing runs first,
            // and everything below bit 16 is gone before the sum ever sees it.
            let narrowed = jasper_resampler::s32_high_word_to_s16(pattern);
            let mut narrow_sum = vec![0i64; 4];
            mix_into(&mut narrow_sum, &[narrowed; 4], ProgramWidth::Narrow);
            let mut narrow_out = vec![0i16; 4];
            saturate_to_i16(&narrow_sum, &mut narrow_out, ProgramWidth::Narrow);
            let survived_narrow = jasper_resampler::widen_i16_to_i32(narrow_out[0]);
            assert_eq!(
                pattern & !0xffff,
                survived_narrow,
                "the narrow route keeps exactly the high word and nothing below it",
            );
        }

        // Named explicitly on the one vector that HAS low bits, so this test
        // cannot pass on rails alone.
        let with_low_bits = U2_HIRES_VECTORS[0];
        assert_ne!(with_low_bits & 0xffff, 0, "the probe must carry low bits");
        let mut sum = vec![0i64; 1];
        mix_into_wide(&mut sum, &[with_low_bits], ProgramWidth::Wide);
        let mut payload = vec![0u8; WIDE_BYTES_PER_SAMPLE];
        fill_wide_ring_payload(&sum, &mut payload);
        let published = i32::from_le_bytes(payload[..].try_into().unwrap());
        assert_eq!(
            published & 0xffff,
            with_low_bits & 0xffff,
            "the low word must reach the wire, not just the high word"
        );
    }

    /// A hi-res lane MIXED WITH an ordinary S16 lane keeps both at the right
    /// level and keeps its own low bits — the promotion and the pass-through
    /// have to agree about the scale or one source is 96 dB off.
    #[test]
    fn a_wide_lane_and_an_s16_lane_sum_at_the_same_scale() {
        let hires = 0x0012_3456i32; // small, so the sum cannot saturate
        let s16 = 1_000i16;
        let mut sum = vec![0i64; 2];
        mix_into_wide(&mut sum, &[hires; 2], ProgramWidth::Wide);
        mix_into(&mut sum, &[s16; 2], ProgramWidth::Wide);
        let expected = (hires as i64) + (jasper_resampler::widen_i16_to_i32(s16) as i64);
        assert_eq!(sum, vec![expected; 2]);
        // The S16 lane still dominates by exactly the ratio of its level to the
        // hi-res sample's, i.e. the promotion did not scale it wrong.
        assert_eq!(
            jasper_resampler::widen_i16_to_i32(s16) as i64,
            (s16 as i64) << 16
        );
        // And the hi-res lane's low bits are still in the sum.
        assert_ne!(sum[0] & 0xffff, 0);
    }

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

    /// THE BYTE-IDENTITY GOLDEN for the narrow wire.
    ///
    /// The gadget-shaped stream above driven through the DIRECT lane's NARROW
    /// route — capture narrowing, resampler push, render, sum entry, summed
    /// write — pinned to committed bytes. Every box in the fleet runs this path.
    ///
    /// The golden is committed evidence about the narrow route's bytes, not a
    /// printout of what this code happens to produce, so a change to the route
    /// has to meet it rather than restate it.
    ///
    /// What moves them: the capture narrowing itself; the narrowing ORDER
    /// (narrow-then-resample — resampling first and narrowing after is
    /// better-rounded but DIFFERENT, and is the mutation this exists to catch);
    /// the resampler ring's storage scale or its narrowing back out; the sum's
    /// scale; the i16 saturation.
    #[test]
    fn the_narrow_direct_route_is_byte_identical_to_its_committed_golden() {
        let output = narrow_direct_route_golden_output();
        // Committed bytes for this fixture.
        let expected: [i16; 24] = [
            6921, 2186, 7688, 2077, 8462, 1983, 9211, 1903, 9904, 1838, 10513, 1786, 11015, 1747,
            11390, 1721, 11624, 1707, 11709, 1704, 11644, 1713, 11434, 1731,
        ];
        assert_eq!(
            &output[..expected.len()],
            &expected,
            "the narrow DIRECT route's summed write drifted from its pre-change golden"
        );
        // A second window, deeper into the period, so the pin is not only on the
        // ramp-adjacent leading samples.
        let tail: [i16; 8] = [10327, -8137, 9893, -7973, 9338, -7805, 8683, -7635];
        assert_eq!(&output[200..208], &tail, "golden tail window drifted");
    }

    /// Drive the DIRECT lane's narrow route over [`golden_gadget_stream`] and
    /// return the summed write of a steady-state period.
    ///
    /// Deliberately built from the SAME primitives the daemon uses in the same
    /// order — `convert_s32_to_s16` at the capture boundary, then `push_input`,
    /// then `render_period`, then `mix_into`, then `saturate_to_i16` — so the
    /// golden it feeds pins the real route rather than a paraphrase of it.
    fn narrow_direct_route_golden_output() -> Vec<i16> {
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
        let mut rendered = vec![0i16; PERIOD as usize * CH];
        let mut phase = 0usize;
        // Feed a period per render, as the gadget does; prime deep enough to lock.
        // The chunk goes in through the REAL width fork
        // (`direct_capture::push_capture_chunk`), so this pins the narrowing
        // ORDER the daemon uses rather than a restatement of it.
        let feed = |lane: &mut LaneResampler, phase: &mut usize, frames: usize| {
            let gadget = golden_gadget_stream(*phase + frames);
            let raw = &gadget[*phase * CH..];
            let mut narrowed = vec![0i16; raw.len()];
            assert!(jasper_resampler::convert_s32_to_s16(raw, &mut narrowed));
            direct_capture::push_capture_chunk(Some(lane), false, raw, Some(&narrowed));
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
        mix_into(&mut sum, &rendered, ProgramWidth::Narrow);
        let mut out = vec![0i16; rendered.len()];
        saturate_to_i16(&sum, &mut out, ProgramWidth::Narrow);
        out
    }

    /// The WIDTH FORK's two branches, asserted at the seam itself: the narrow
    /// route hands the resampler the NARROWED view (narrow-then-resample), the
    /// wide route hands it the raw gadget `i32`.
    ///
    /// Observed through what each lane then renders, because the fork's whole
    /// output is what the resampler received. A narrow lane fed a hi-res chunk
    /// renders the truncated high word; a wide lane fed the same chunk renders
    /// the sample intact.
    #[test]
    fn the_width_fork_narrows_before_the_resampler_and_only_on_the_narrow_route() {
        const PERIOD: u32 = 256;
        const CH: usize = CHANNELS as usize;
        // The probe is chosen so TRUNCATION AND ROUNDING DISAGREE: its low word
        // is 0xC000, i.e. three quarters of a step, so `>> 16` keeps 0x1234
        // while a round-to-nearest would give 0x1235. Without that the test
        // would pass whether the chunk was narrowed before the resampler or
        // resampled first and narrowed after — pinned for the wrong property.
        let hires = 0x1234_C000u32 as i32;
        assert_ne!(
            jasper_resampler::s32_high_word_to_s16(hires),
            jasper_resampler::narrow_i32_to_i16_round(hires),
            "the probe must distinguish truncation from rounding, or this test \
             cannot see the narrowing order at all",
        );
        let build = || {
            LaneResampler::new(
                CH,
                PERIOD,
                48_000,
                512,
                PERIOD as usize,
                500.0,
                8_192,
                crate::lane_resampler::DecayParams::disabled(),
            )
            .expect("lane resampler builds")
        };
        let raw = vec![hires; (512 + 3 * PERIOD as usize + 17) * CH];
        let mut narrowed = vec![0i16; raw.len()];
        assert!(jasper_resampler::convert_s32_to_s16(&raw, &mut narrowed));

        let mut narrow_lane = build();
        let mut wide_lane = build();
        direct_capture::push_capture_chunk(Some(&mut narrow_lane), false, &raw, Some(&narrowed));
        direct_capture::push_capture_chunk(Some(&mut wide_lane), true, &raw, None);

        let mut narrow_out = vec![0i16; PERIOD as usize * CH];
        let mut wide_out = vec![0i32; PERIOD as usize * CH];
        // Render twice: the first period after lock is scaled by the startup
        // de-click ramp, so the steady-state period is the one that shows what
        // the resampler was actually handed.
        for _ in 0..2 {
            assert_eq!(narrow_lane.render_period(&mut narrow_out), PERIOD as usize);
            assert_eq!(wide_lane.render_period_wide(&mut wide_out), PERIOD as usize);
        }

        assert_eq!(
            narrow_out[0],
            jasper_resampler::s32_high_word_to_s16(hires),
            "the narrow route must hand the resampler the narrowed chunk",
        );
        assert_eq!(
            wide_out[0], hires,
            "the wide route must hand the resampler the raw gadget sample",
        );
        // A `None` resampler is a silent no-op, not a panic — the lane can be
        // Absent with no resampler built.
        direct_capture::push_capture_chunk(None, false, &raw, Some(&narrowed));
    }

    /// Printer for [`the_narrow_direct_route_is_byte_identical_to_its_committed_golden`]'s
    /// fixture, so the golden can be RE-captured against a known commit rather
    /// than hand-transcribed. Ignored by default; run with
    /// `cargo test print_narrow_direct_golden -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn print_narrow_direct_golden() {
        let out = narrow_direct_route_golden_output();
        println!("HEAD24 {:?}", &out[..24]);
        println!("TAIL8 {:?}", &out[200..208]);
    }

    /// The width mapping itself: one boolean in, one scale out. The boolean's
    /// own derivation (ring transport AND S32LE wire — the FORMAT half is now
    /// true by default, so the transport is what gates it) is pinned where it
    /// lives, next to the env parse — see config.rs
    /// `the_program_width_needs_both_the_ring_transport_and_the_wide_format`
    /// and `the_wide_program_path_follows_the_transport_not_a_declaration`.
    #[test]
    fn the_program_width_maps_the_one_resolved_wire_boolean() {
        assert_eq!(ProgramWidth::from_wire_is_wide(true), ProgramWidth::Wide);
        assert_eq!(ProgramWidth::from_wire_is_wide(false), ProgramWidth::Narrow);
    }

    // ---- SF-B / SF-C: the width cross-check and the lane's width switch ----

    /// SF-C: the ONE decision that makes a lane spine-scale, and the ALSA open
    /// format that has to agree with it.
    ///
    /// Inverting the buffer points the capture fork, the render, and the sum
    /// entry at the wrong scale simultaneously, and the `debug_assert` in
    /// `mix_into_wide` cannot catch it on the Pi — the release profile compiles
    /// debug assertions out. Inverting the open format is just as silent at
    /// compile time and louder at runtime: `read_input` picks its typed IO
    /// handle off the buffer and alsa-lib verifies that handle against the
    /// PCM's negotiated format, so an aloop lane whose two halves disagreed
    /// would fail every read. Both are pinned here, where a mutation has to
    /// fail, and pinned together because they are one decision.
    #[test]
    fn a_lanes_wire_decides_its_open_format_and_its_period_buffer_together() {
        assert_eq!(spine_read_buf(true, 4), vec![0i32; 4]);
        assert_eq!(lane_capture_format(true), Format::S32LE);

        assert!(spine_read_buf(false, 4).is_empty());
        assert_eq!(lane_capture_format(false), Format::S16LE);
    }

    /// Every COHERENT shape constructs — including the one that has no wide lane
    /// at all on a wide wire.
    #[test]
    fn a_coherent_program_width_constructs() {
        let coherent = [
            // (declared, output_is_wide, any_lane_is_wide)
            (ProgramWidth::Narrow, false, false),
            (ProgramWidth::Wide, true, true),
            // A wide wire with NO wide lane. Legal, and the common case — see
            // the dedicated test below.
            (ProgramWidth::Wide, true, false),
        ];
        for (declared, output_is_wide, any_lane_is_wide) in coherent {
            assert!(
                program_width_disagreement(ProgramWidthAudit {
                    declared,
                    output_is_wide,
                    any_lane_is_wide,
                })
                .is_none(),
                "{declared:?}/out={output_is_wide}/lane={any_lane_is_wide} must construct",
            );
        }
    }

    /// A WIDE WIRE WITH USB AUDIO INPUT OFF MUST START. This is not a corner
    /// case — it is the shape an armed box has by default.
    ///
    /// The lane axis is an implication, not an equality, so a wide box with no
    /// wide lane — `declared=Wide, output_is_wide=true, any_lane_is_wide=false`
    /// — must construct: any i16 lane simply promotes at `mix_into`'s Wide arm.
    ///
    /// Requiring equality on the lane axis would exit 78 here: no audio path, no
    /// self-recovery, operator-only — on a correct configuration, and reachable
    /// by a household toggling USB off at `/sources/`. Fail-closed is right for
    /// a fault and a liveness hazard when pointed at a legal state.
    #[test]
    fn a_wide_wire_with_usb_audio_input_off_constructs() {
        assert!(
            program_width_disagreement(ProgramWidthAudit {
                declared: ProgramWidth::Wide,
                output_is_wide: true,
                any_lane_is_wide: false,
            })
            .is_none(),
            "a wide box with USB Audio Input off must start, not park",
        );
    }

    /// SF-B: every genuine disagreement fails construction — and does so as
    /// CONFIG-CLASS.
    ///
    /// The marker is the load-bearing half. Without it the error reaches `main`
    /// as an ordinary failure, the unit takes `Restart=on-failure`, and five
    /// starts in five minutes reach `StartLimitAction=reboot` — a permanent,
    /// unrepairable-by-restarting condition rebooting the Pi in a loop.
    /// Asserting only "it returns Some" would pass with the marker deleted.
    ///
    /// `(Wide, output wide, no wide lane)` is deliberately NOT here — it is the
    /// legal USB-off shape, pinned by
    /// `a_wide_wire_with_usb_audio_input_off_constructs`.
    #[test]
    fn every_program_width_disagreement_parks_as_config_class() {
        let cases = [
            // (declared, output_is_wide, any_lane_is_wide)
            // Header axis, both directions.
            (ProgramWidth::Narrow, true, false),
            (ProgramWidth::Narrow, true, true),
            (ProgramWidth::Wide, false, false),
            (ProgramWidth::Wide, false, true),
            // Lane axis, the +96 dB direction: a spine-scale lane feeding a
            // narrow-scale sum.
            (ProgramWidth::Narrow, false, true),
        ];
        for (declared, output_is_wide, any_lane_is_wide) in cases {
            let error = program_width_disagreement(ProgramWidthAudit {
                declared,
                output_is_wide,
                any_lane_is_wide,
            })
            .unwrap_or_else(|| {
                panic!("{declared:?}/out={output_is_wide}/lane={any_lane_is_wide} must fail")
            });
            assert!(
                error.downcast_ref::<crate::ConfigClassError>().is_some(),
                "{declared:?}/out={output_is_wide}/lane={any_lane_is_wide} must carry the \
                 config-class marker, or a permanent fault reboot-loops the Pi",
            );
        }
    }

    // ------------------------------------------------------------------
    // The `i32` rails and the `f32` mantissa on the `i64` accumulator.
    // ------------------------------------------------------------------

    /// THE NARROW DUCK'S BYTES, pinned as literals.
    ///
    /// The narrow arm of `apply_gain_to_sum` / `ramp_program_duck` must stay the
    /// same arithmetic down to the `f32` multiply and the (unreachable) `i32`
    /// clamp. These are the numbers a narrow box produces.
    #[test]
    fn the_narrow_duck_is_byte_identical_to_its_committed_golden() {
        let mut sum = vec![20_000i64, -20_000, 1_500, -1_500, 32_767, -32_768];
        apply_gain_to_sum(&mut sum, 0.1, ProgramWidth::Narrow);
        assert_eq!(sum, vec![2_000, -2_000, 150, -150, 3_277, -3_277]);

        let mut ramped = vec![20_000i64, -20_000, 1_500, -1_500, 32_767, -32_768];
        let current = ramp_program_duck(&mut ramped, 2, 0.1, 0.1, 0.01, 0.01, ProgramWidth::Narrow);
        assert_eq!(current, 0.1);
        assert_eq!(ramped, sum, "the ramp's steady state IS the flat multiply");

        // THE MANTISSA IS PART OF THOSE BYTES. The vectors above do not
        // distinguish an `f32` product from an `f64` one — mutation testing
        // showed an f64 narrow arm passing them — because the two agree on
        // very nearly every value. `50 * 0.01` is one of the few where they do
        // not: the f32 product rounds UP to exactly 0.5 and `round()` takes it
        // to 1, while f64 keeps 0.49999998... and rounds to 0. That one-step
        // difference is the shipped behaviour, quirk included.
        let mut edge = vec![50i64];
        apply_gain_to_sum(&mut edge, 0.01, ProgramWidth::Narrow);
        assert_eq!(edge[0], 1, "the shipped f32 product rounds up here");
        let via_f64 = (50.0_f64 * f64::from(0.01f32)).round() as i64;
        assert_eq!(via_f64, 0, "an f64 product would round down");
        assert_ne!(
            edge[0], via_f64,
            "the probe must distinguish f32 from f64, or this guards nothing",
        );
        let mut edge_ramped = vec![50i64];
        ramp_program_duck(
            &mut edge_ramped,
            1,
            0.01,
            0.01,
            0.001,
            0.001,
            ProgramWidth::Narrow,
        );
        assert_eq!(edge_ramped[0], 1, "the ramp keeps the same f32 product");
    }

    /// THE RAILS — the correctness half of n5.
    ///
    /// A spine-scale sum legitimately exceeds `i32::MAX`: that headroom above
    /// full scale is why the accumulator is `i64`, and the duck's job is to
    /// bring such a sum back into range. Clamping the ducked value to `i32`
    /// spent the headroom before anything downstream could use it. This drives
    /// `step()`'s real order — sum the lanes, duck the program, THEN add the
    /// assistant — because that is where the clamped and unclamped values stop
    /// agreeing at the speaker.
    #[test]
    fn the_wide_duck_keeps_the_i64_headroom_the_i32_rails_would_have_spent() {
        const FULL_SCALE_WIDE: i64 = (32_767i64) << 16;
        let duck = 0.5f32;
        // Three full-scale wide lanes: 6_442_254_336, three times over the
        // `i32` rail and entirely legitimate mid-chain.
        let mut sum = vec![0i64; 1];
        for _ in 0..3 {
            mix_into(&mut sum, &[i16::MAX], ProgramWidth::Wide);
        }
        assert_eq!(sum[0], 3 * FULL_SCALE_WIDE);
        assert!(
            sum[0] > i32::MAX as i64,
            "the probe must exceed the old rails, or this test guards nothing",
        );

        let ducked_probe = sum[0];
        apply_gain_to_sum(&mut sum, duck, ProgramWidth::Wide);
        assert_eq!(sum[0], 3_221_127_168, "the ducked sum keeps its headroom");

        // The clamped value is COMPUTED FROM THE CLAMPING EXPRESSION rather
        // than written down: `i32::MAX as f32` rounds UP to 2^31, so
        // `.clamp(_, i32::MAX as f32) as i64` lands on 2_147_483_648, not
        // `i32::MAX`.
        let spent_value = ((ducked_probe as f32) * duck)
            .round()
            .clamp(i32::MIN as f32, i32::MAX as f32) as i64;
        assert_eq!(
            spent_value,
            i32::MAX as i64 + 1,
            "the f32 upper rail is 2^31, one above i32::MAX",
        );
        assert_ne!(
            sum[0], spent_value,
            "the old i32 rails would have landed exactly here",
        );

        // Now the assistant enters, as it does in `step()`, pulling the sum
        // back into range. The clamped and unclamped paths differ by ~18 dB at
        // the speaker, not by a rounding step.
        let assistant = -2_000_000_000i64;
        let kept = vec![sum[0] + assistant];
        let spent = vec![spent_value + assistant];
        let mut kept_out = vec![0i16; 1];
        let mut spent_out = vec![0i16; 1];
        saturate_to_i16(&kept, &mut kept_out, ProgramWidth::Wide);
        saturate_to_i16(&spent, &mut spent_out, ProgramWidth::Wide);
        assert_eq!(kept_out[0], 18_633);
        // 2_250 on BOTH rail candidates (i32::MAX and the f32 clamp's 2^31):
        // the two differ by one spine LSB, far below one i16 step, so the
        // audible verdict is the same either way.
        assert_eq!(spent_out[0], 2_250);
        assert_eq!(
            {
                let alt = vec![(i32::MAX as i64) + assistant];
                let mut alt_out = vec![0i16; 1];
                saturate_to_i16(&alt, &mut alt_out, ProgramWidth::Wide);
                alt_out[0]
            },
            spent_out[0],
            "both old-rail candidates land on the same i16 code",
        );
        // ~18 dB, stated as a ratio rather than left for the reader to divide.
        assert!(
            (kept_out[0] as f64 / spent_out[0] as f64) > 8.0,
            "kept={} spent={}",
            kept_out[0],
            spent_out[0],
        );
    }

    /// The ramp is the same multiply with a per-frame gain, and `step()` reaches
    /// it on every duck transition.
    #[test]
    fn the_wide_ramp_keeps_the_same_headroom_as_the_flat_wide_multiply() {
        const FULL_SCALE_WIDE: i64 = (32_767i64) << 16;
        let mut ramped = vec![3 * FULL_SCALE_WIDE, 3 * FULL_SCALE_WIDE];
        let mut flat = ramped.clone();
        // current == target means every frame scales by the same constant.
        let current = ramp_program_duck(&mut ramped, 2, 0.5, 0.5, 0.01, 0.01, ProgramWidth::Wide);
        apply_gain_to_sum(&mut flat, 0.5, ProgramWidth::Wide);
        assert_eq!(current, 0.5);
        assert_eq!(ramped, flat);
        assert_eq!(ramped[0], 3_221_127_168);
        assert!(
            ramped[0] > i32::MAX as i64,
            "the ramp must not clamp to the i32 rails either",
        );
    }

    /// THE MANTISSA — the precision half of n5.
    ///
    /// `f32` carries 24 bits, so `sum as f32` at spine scale rounds the value
    /// before the multiply happens; near `2^31` the `f32` grid is 256 wide. The
    /// wide arm computes in `f64`, whose 53-bit mantissa holds every reachable
    /// sum exactly. This is a −144 dBFS-class correction, not a level fix —
    /// stated as what it is.
    #[test]
    fn the_wide_duck_multiplies_in_f64_because_f32_cannot_hold_a_spine_sum() {
        let probe = 2_147_483_000i64;
        let gain = 0.5f32;
        let mut wide = vec![probe];
        apply_gain_to_sum(&mut wide, gain, ProgramWidth::Wide);
        assert_eq!(wide[0], 1_073_741_500, "f64: the exact half of the probe");

        // What an f32 multiply would have produced, spelled out rather than
        // asserted by inequality alone, so the test names the value it rejects.
        let via_f32 = ((probe as f32) * gain).round() as i64;
        assert_eq!(via_f32, 1_073_741_504);
        assert_ne!(
            wide[0], via_f32,
            "the probe must distinguish f32 from f64, or this test guards nothing",
        );

        // And the narrow arm keeps f32 deliberately: a narrow sum is under
        // 2^24, where f32 is exact, and an f64 product can round differently in
        // the last place. Same value, both widths, when the sum is small.
        let small = 20_000i64;
        let mut narrow_small = vec![small];
        let mut wide_small = vec![small];
        apply_gain_to_sum(&mut narrow_small, 0.1, ProgramWidth::Narrow);
        apply_gain_to_sum(&mut wide_small, 0.1, ProgramWidth::Wide);
        assert_eq!(narrow_small, wide_small);
    }
}
