// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! ALSA fan-in mixer — the core work loop.
//!
//! Reads from N capture PCMs (one per renderer's snd-aloop substream
//! pair), sums sample-wise, writes the summed stream to one playback
//! PCM (the "summed music" substream that CamillaDSP + AEC bridge
//! dsnoop on).
//!
//! ## Pacing
//!
//! The OUTPUT PCM is the metronome. We open it in blocking mode;
//! `writei()` blocks until the kernel has room in the output ring
//! (which empties at the system sample rate). That's what gates the
//! work loop to the right cadence.
//!
//! INPUTS are opened in non-blocking mode. Each iteration we read
//! one period from each input. If a renderer isn't producing audio
//! right now (the substream's writer hasn't opened, or is paused),
//! the non-blocking read returns -EAGAIN and we substitute silence
//! for that input. If a renderer produces faster than we drain
//! (shouldn't happen at matched 48 kHz steady-state, but possible
//! during a burst), the input substream overruns; we `try_recover`
//! and treat the affected period as silence.
//!
//! ## Mix math
//!
//! Inputs are S16_LE interleaved stereo. We accumulate into an i32
//! scratch buffer (using `saturating_add`) so simultaneous full-scale
//! inputs don't wrap, then clamp back to i16 for the output. Matches
//! ALSA dmix's clip behavior — audio sounds identical to today during
//! the Tier 2A transition (saturating clipping is louder than scaled
//! averaging when sources are simultaneous, but mux normally enforces
//! single-active anyway, so simultaneous is the brief handover case
//! only).
//!
//! ## Per-frame discipline
//!
//! `step()` does one period's worth of work: read all inputs, sum,
//! write output. `run()` calls `heartbeat.bump_progress()` after
//! every successful `step()`, satisfying the JTS progress-sentinel
//! contract documented in `src/watchdog.rs`.

use std::mem::MaybeUninit;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicI64, AtomicU64, Ordering};
use std::sync::mpsc::{Sender, SyncSender};
use std::sync::{Arc, Mutex};

use alsa::pcm::{Access, Format, Frames, HwParams, State, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};
use log::{info, warn};

use jasper_ring::{Geometry, PublishOutcome, RingWriter, SAMPLE_FORMAT_S16LE};

use crate::config::{Config, Coupling, RING_SLOT_FRAMES};
use crate::fifo::{FifoWriteOutcome, FifoWriter};
use crate::impulse_tap::{ImpulseDetector, TapConfig, TapEvent, TapState};
use crate::lane_resampler::{LaneResampler, LaneResamplerObservability};
use crate::tts::{TtsInput, TtsMixer};
use crate::watchdog::Heartbeat;
use crate::xrun_log::{XrunEvent, XrunSource};

/// Stereo. The CamillaDSP capture + AEC bridge tap both expect 2
/// channels (matches the dmix's declared shape). Not configurable.
pub const CHANNELS: u32 = 2;

/// PCM sample format. Matches the dmix's declared format and the
/// dsnoop slave's format. Changing this would cascade through the
/// asoundrc, CamillaDSP, and the AEC bridge — out of scope for the
/// daemon.
pub const FORMAT: Format = Format::S16LE;

/// Sentinel for "no ALSA playback delay sample has landed yet".
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
/// periods on a stressed stock Pi 5, PREEMPT_RT not yet in; see
/// HANDOFF-fan-in-daemon.md) — so a stall coinciding with a burst is ~5.5 + 6.9
/// ≈ 12.4 periods of peak occupancy on a healthy lane. Upper bound: it MUST sit
/// below the input buffer depth (16 periods / 4096 frames, the "0 xruns over
/// 4.5 min" sizing) so the resync fires before overrun. 14 periods (~75 ms)
/// clears the ~12.4-period healthy burst+stall peak with ~1.6-period margin and
/// still leaves 2 periods under the 16-period buffer. A free-running lane grows
/// MONOTONICALLY (its producer's average rate exceeds ours), so it always
/// crosses this; a healthy lane's burst+stall peak stays below it.
///
/// NOT drift correction: this is a controlled, occasional drop-resync at the
/// residual drift rate, not a drop-FREE resampler (that is the later per-lane
/// adaptive resampler). Honest tradeoff: a backed-up lane loses a bounded
/// chunk of audio at each resync instead of cascading into an upstream
/// producer overflow.
const CATCHUP_HIGH_WATER_PERIODS: i64 = 14;

/// Hard cap on whole periods discarded in a single resync, so a pathological
/// `avail` (driver fault, or a huge buffer) can't turn the bounded
/// read-and-drop into an unbounded syscall spin inside the hot loop. A lane
/// further behind than this finishes resyncing over the next few periods —
/// still bounded per period.
const CATCHUP_MAX_DRAIN_PERIODS: i64 = 64;

/// Emit the rate-limited `event=fanin.input.catchup` log on the 1st resync
/// for a lane and then every Nth, so a chronically free-running lane can't
/// spam the journal. A resync only fires when a lane crosses the high-water,
/// which is already infrequent; this is defense-in-depth against a wedged
/// producer. Count-based (not time-based) so the hot loop never reads a clock.
const CATCHUP_LOG_EVERY: u64 = 64;

/// USB DIRECT capture open envelope (C1) — the bridge's PROVEN params, NOT
/// fanin's aloop-tuned `configure_pcm`. S32_LE 2ch 48k, period 256, buffer
/// ~768 (near). These MUST match `jasper-usbsink-audio`'s
/// `open_capture`/`configure_pcm` so the direct lane inherits the exact
/// negotiation the bridge validated on the UAC2 gadget.
///
/// `DIRECT_PERIOD_FRAMES` is the DEFAULT gadget open period; the actual open
/// period is overridable via `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES` (lever 2 —
/// the H1 "hw-pointer/period granularity" test knob). Unset ⇒ this default ⇒
/// byte-identical to today. The chunk-read cap and the narrowing scratch below
/// stay pinned to this default regardless of the open period: they bound the
/// per-`readi` granularity (256 frames), which is independent of the gadget's
/// period IRQ cadence, so a larger open period never overflows the fixed
/// scratch and a smaller one never under-reads.
const DIRECT_PERIOD_FRAMES: u32 = 256;

/// Deep-buffer safety floor for the (tunable) direct open period (lever 2). The
/// negotiated capture buffer must clear BOTH bounds so the H1 experiment (open
/// period 64) rides a DEEP buffer (12 periods) rather than the refuted shallow
/// 2-period URB-headroom class: at least three whole periods, and at least the
/// proven 768-frame floor. At the default period 256 this reproduces the old
/// fixed 768-frame envelope exactly (3×256 = 768). `resolve_direct_buffer_frames`
/// is the single owner of this rule (pure, scratch-crate tested).
///
/// Identity caveat (lever 2): the direct lane's ACCEPTANCE floor is this
/// `max(3×period, 768)`, tightened from the earlier validator's `2×period`. The
/// open REQUEST is byte-identical when the period knob is unset, but a
/// hypothetical negotiation to 512..767 frames that the old floor would have
/// accepted (with a `buffer_near` warn) now fails validation and sends the lane
/// Absent. Deliberate — fail-loud beats running the refuted shallow class — and
/// inert in practice (u_audio negotiates exactly 768 at period 256).
const DIRECT_BUFFER_MIN_PERIODS: u32 = 3;
const DIRECT_BUFFER_MIN_FRAMES: u32 = 768;

/// Compute the direct capture buffer for a given open period, honoring the
/// deep-buffer safety floor (≥ `DIRECT_BUFFER_MIN_PERIODS` periods AND ≥
/// `DIRECT_BUFFER_MIN_FRAMES`), then rounded UP to a whole period multiple so
/// the negotiated geometry is period-aligned (a fractional buffer would shear;
/// `direct_open_params_ok` rejects it). At the default period 256 this yields
/// exactly 768 (byte-identical to today: 3×256 = 768 ≥ 768). Pure so the floor
/// math is unit-testable without ALSA.
fn resolve_direct_buffer_frames(period: u32) -> u32 {
    let by_periods = period.saturating_mul(DIRECT_BUFFER_MIN_PERIODS);
    let floor = by_periods.max(DIRECT_BUFFER_MIN_FRAMES);
    // Round up to the next whole period so buffer % period == 0.
    let period = period.max(1);
    floor.div_ceil(period).saturating_mul(period)
}

/// Number of fixed histogram buckets for the drain-entry avail distribution
/// (lever 2 observability). Boundaries at 64-frame steps: `[0,64) [64,128)
/// [128,192) [192,256) [256,320) [320,+)`. Chosen so the measured ~186-frame
/// standing gadget avail lands mid-histogram and the H1 signature (avail
/// quantized near 0/256 — bimodal in bucket 0 and buckets 3/4) is visible.
const DRAIN_AVAIL_BUCKETS: usize = 6;

/// Classify a drain-entry `avail` (frames) into one of [`DRAIN_AVAIL_BUCKETS`]
/// fixed buckets. Pure so the bucketing is scratch-crate testable without ALSA.
/// Negative avail (never observed at a real `avail_update` Ok, but the ALSA
/// `Frames` type is `i64`) and 0 both land in bucket 0; anything ≥ 320 saturates
/// into the top bucket.
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
/// the drain counter itself (no wall clock, no extra state) so the log cadence
/// is O(1) and self-throttling on the hot path. `2^15` ≈ one line per ~3 min at
/// the default 256-frame render period (5.33 ms/cycle) — deliberately coarse:
/// one drain is recorded every render cycle the gadget PCM is open, INCLUDING
/// while the host is attached but idle, so a tighter cadence would spam the
/// persistent journal 24/7 on a direct-enabled box (this is a permanent surface,
/// not a debug trace). The since-boot STATUS block is the fine-grained read; this
/// line is just a periodic journal breadcrumb.
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

/// Bounded impulse-tap channel capacity (C4). The single detector fires at most
/// once per refractory window (~4/s at the 250 ms default), so this can never
/// fill under the harness; it exists as a drop-and-count safety net so the
/// mixer thread's `try_send` is always non-blocking.
const TAP_CHANNEL_CAPACITY: usize = 256;

/// USB DIRECT reopen retry cadence, in render PERIODS (~2 s at 256/48k = 375).
/// While the gadget is Absent, the lane attempts a reopen at most once per this
/// many periods it renders — a period-counted cadence so the hot loop never
/// reads a wall clock (same discipline as the auto-trim frames-delta latch, C3).
const DIRECT_REOPEN_RETRY_PERIODS: u64 = 375;

/// Consecutive zero-avail drains that mark a ZOMBIE capture handle (C, defect
/// 2026-07-05). When the gadget function is REBUILT underneath fan-in (a UDC
/// rebind / usbsink stop-start), fan-in's open `hw:UAC2Gadget` PCM stays attached
/// to a DESTROYED instance: `avail_update()` returns `Ok(0)` forever — NOT an
/// errno, so `classify_direct_errno` never fires and the DeviceLost path never
/// triggers. The drain just sees avail=0, reads nothing, and the lane goes deaf
/// (observed: drain_count 71k / 6.4k with zero frames, opens=1 retries=0, /proc
/// pcm0c 'closed'). This threshold (~2 s at the default 256/48k period, matching
/// `DIRECT_REOPEN_RETRY_PERIODS`) is how many consecutive render periods of
/// exactly-zero avail — while the handle is Present — trip a forced close +
/// bounded re-open. A genuinely idle-but-healthy host still streams silence
/// frames (avail > 0), so sustained EXACTLY-zero avail means the gadget is no
/// longer feeding this handle at all: either a zombie (reopen fixes it) or a
/// clean host-stream-stop (reopen re-establishes an identical handle, harmless
/// since no audio was flowing). Either way a bounded reopen is the safe recovery.
const DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS: u64 = 375;

/// Pure zombie-handle predicate (C): does a run of `zero_avail_streak` consecutive
/// zero-avail drains at or beyond `threshold` mark the Present handle as a zombie
/// to force-reopen? Extracted so the detection is scratch-crate testable without
/// ALSA (the `Ok(0)`-forever gadget-rebuild condition can't be reproduced in a
/// unit test otherwise).
fn zombie_handle_suspected(zero_avail_streak: u64, threshold: u64) -> bool {
    threshold > 0 && zero_avail_streak >= threshold
}

/// Delay, in whole seconds, from a lane's idle→active transition to its
/// one-shot AUTO-TRIM fire. Gives the chain time to warm up and establish its
/// standing fill before the trim drops it — trimming at t=0 (before the fill
/// has accumulated) would be a no-op. Converted to a `frames_read` budget at
/// the live sample rate (`sample_rate × seconds`) so the wall-clock delay is
/// stable across period geometries. Only consulted when
/// `JASPER_FANIN_AUTO_TRIM=enabled`.
const AUTO_TRIM_DELAY_SECONDS: u64 = 2;

/// Post-lock cushion-decay warm-up window (latency lever 1): the continuous
/// locked + DLL-`l0_locked` + calm duration required before the FIRST decay
/// step. 10 s gives the outer host-clock DLL time to finish its per-session
/// probe (up to 14 s worst case, but the fill is pinned well before that) and
/// prove the steady regime before latency is reclaimed. Not an env knob — the
/// three tunable decay knobs (floor/step/interval) are the operator surface;
/// this is the internal "is it stable yet" gate. Converted to render periods by
/// the lane from the live sample rate.
const CUSHION_DECAY_STABILITY_MS: u64 = 10_000;

/// Cascade-stability guard (latency lever 1): decay pauses while the outer DLL's
/// |commanded_ppm| exceeds this. Above it the DLL is working hard and the fill
/// is in transient, so lowering the setpoint would fight the loop — the exact
/// two-controller oscillation class the cascade design avoids. 400 ppm is well
/// inside the ±1000 ppm servo authority: it flags "actively correcting" without
/// tripping on the small steady-state trims a settled loop makes.
///
/// `pub(crate)` so the config test can pin the derived-margin invariant: the
/// default decay step demand (step_frames / interval → ppm) must sit inside this
/// guard, or a settled decay step could perturb the DLL cascade.
pub(crate) const CUSHION_DECAY_CASCADE_GUARD_PPM: f64 = 400.0;

/// The host-compliance proof's settle window — how long the decay must hold at
/// the floor with the DLL `l0_locked` and zero unlock churn before the proof is
/// persisted. REUSES the same [`CUSHION_DECAY_STABILITY_MS`] window the decay's
/// own warm-up uses: "stable long enough to trust" is one number across the
/// feature. Converted to render periods at the live lane geometry.
const HOST_COMPLIANCE_SETTLE_MS: u64 = CUSHION_DECAY_STABILITY_MS;

/// The host-compliance EARLY-REVALIDATION window — a floor-primed session runs
/// the aggressive one-strike unlock revocation only for this long after it locks.
/// The per-session probe (which revalidates on EVERY session, floor-primed or
/// not) is the primary revalidation; this window narrows the *underfill-unlock*
/// trigger to the acquisition-adjacent phase where a floor prime against a
/// now-incompatible host would first thrash. 60 s comfortably covers the probe
/// (≤14 s) plus the first steady-state minute. Probe-fail / L2 demotion revoke
/// regardless of this window (they are direct host-non-compliance evidence).
const HOST_COMPLIANCE_EARLY_REVALIDATION_SECS: u64 = 60;

/// The host-compliance CHURN-CONFIRMATION horizon — an early-window underfill
/// unlock only ARMS a pending EarlyUnlock strike; the strike CONFIRMS (revoke)
/// only if a RELOCK arrives within this many seconds of the arming unlock. Converted
/// to render periods at the live geometry and compared purely in ticks by the pure
/// `RevalidationTracker` (never a wall clock).
///
/// Why a relock is required (the terminal-stream-end fix, hardware-diagnosed on
/// jts.local 2026-07-03): EVERY session end presents as an underfill unlock
/// (deliveries stop → the fill drains below `minimum_safe_fill` within ms, long
/// before any idle classification), so the old "any early-window unlock revokes"
/// rule burned the proof on every sub-60 s session. macOS CoreAudio stops the
/// device stream seconds after the last client, making short sessions (notification
/// dings / previews) the COMMON case. Only unlock→relock CYCLING proves the host is
/// still present and the floor is genuinely failing — that is churn worth revoking.
///
/// 5 s comfortably covers a real re-acquisition after a floor-fatal underfill (the
/// lane re-primes and relocks in well under a second) while expiring a terminal
/// stream-end's pending strike well before the usual gap to the next macOS stream
/// (seconds-to-minutes later). The honest bound, NOT an absolute "never": a strike
/// survives only into a relock arriving ≤ `HOST_COMPLIANCE_CHURN_CONFIRM_SECS` after
/// the arming unlock. The tracker has NO signal to tell a genuinely-new stream's
/// first lock (a fresh clip started ≤ 5 s after the prior stopped) apart from a churn
/// relock — both are "armed strike + rising edge inside the horizon" — so such a
/// restart WILL confirm the dead session's strike: one spurious revoke, self-healing
/// via re-prove on that session's ~2.5-min descent. That residual is accepted (the
/// horizon cannot shrink below ~2× the bounded-prime fall-through without missing
/// genuine bursty-host churn); see the revalidation section of
/// docs/HANDOFF-usb-low-latency.md.
const HOST_COMPLIANCE_CHURN_CONFIRM_SECS: u64 = 5;

/// Per-lane TRIM control + counters, shared (`Arc`) between the mixer work
/// thread (which owns the `LaneResampler` and performs the actual ring trim)
/// and the state-server thread (which requests trims and reads the counters for
/// STATUS). Mirrors the `selected_input_index` cross-thread atomic idiom: the
/// control endpoint cannot touch the mixer-owned resampler directly, so it sets
/// `pending` and the work loop does the trim at its next period boundary.
#[derive(Debug)]
pub struct TrimControl {
    /// Set by a `TRIM` control command; consumed (cleared) by the work loop at
    /// the next period boundary, which then performs the trim. Idempotent — a
    /// second `TRIM` before the loop consumed the first just re-sets the same
    /// flag (one trim results).
    pub pending: AtomicBool,
    /// Cumulative TRIM operations that actually dropped ≥1 frame on this lane.
    pub trims: AtomicU64,
    /// Cumulative frames dropped by TRIM from this lane's resampler ring. Paired
    /// with `trims` so STATUS shows both how often and how much (like the
    /// catch-up pair).
    pub trimmed_frames: AtomicU64,
    /// AUTO-TRIM one-shot latch. `false` while the lane is idle (armed to fire);
    /// set `true` once the auto-trim has fired for the current active session so
    /// it fires exactly once per idle→active→…→idle cycle. Re-armed (set back to
    /// `false`) when the lane goes idle. Only used when auto-trim is enabled.
    pub auto_fired: AtomicBool,
}

impl TrimControl {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            pending: AtomicBool::new(false),
            trims: AtomicU64::new(0),
            trimmed_frames: AtomicU64::new(0),
            auto_fired: AtomicBool::new(false),
        })
    }

    /// Construct a `TrimControl` seeded with explicit counter values, for the
    /// state-server STATUS/command tests (which build `InputSnapshotSource`
    /// fixtures directly, without a live mixer). Not compiled into the daemon.
    #[cfg(test)]
    pub fn test_fixture(trims: u64, trimmed_frames: u64, pending: bool) -> Self {
        Self {
            pending: AtomicBool::new(pending),
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
/// State machine (one latch per lane, re-armed each idle→active cycle):
///   - Lane read audio this period iff `frames_read > state.last_frames_read`.
///   - idle→active (was `None`, now active): record `active_since = frames_read`
///     and do NOT fire yet (the standing fill hasn't accumulated).
///   - active and `frames_read - active_since >= delay_frames`: report `fire`.
///     This function reports `fire` on EVERY period past the delay; the caller's
///     `TrimControl::auto_fired` latch makes it one-shot and survives across
///     periods (encoding "already fired this session" as an atomic the pure
///     function cannot see).
///   - active→idle (no read this period, was active): clear `active_since` to
///     `None` so the NEXT activation re-arms.
///
/// `last_frames_read` is always advanced to the current value.
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
                // activation period itself.
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

/// The final-output transport. `Alsa` (the default) writes the snd-aloop
/// substream and is paced by the blocking ALSA `writei` — byte-identical to the
/// pre-coupling daemon. `Fifo` is the writer primitive used by the public
/// `transport_pipe` mode: it writes a bounded named pipe that CamillaDSP
/// RawFile-captures. `Ring` is the Ring A (PROTOTYPE) SPSC SHM ring writer;
/// CamillaDSP reads it via a capture-direction ioplug. Each is the sole timing
/// owner of the fan-in work loop in its mode; only one is ever active.
enum Output {
    Alsa(PCM),
    Fifo(FifoWriter),
    /// The SPSC SHM ring writer plus its lossy aloop MIRROR PCM. The blocking
    /// ring publish (bounded, on a full-ring-with-live-reader) is the pacer; the
    /// mirror is a `write_music_only`-shaped non-blocking side-tap so the AEC
    /// fallback dsnoop and any aloop diagnostics stay live. `RingOutput` owns the
    /// per-step slot fan-out and the reader-absent self-pacing.
    Ring(RingOutput),
}

/// The Ring A output: the SPSC ring writer, its shared observability counters,
/// the lossy aloop mirror PCM (never the pacer), and the derived self-pacing
/// period (used only when the reader is absent — one period's sleep per dropped
/// publish so a readerless ring does not hot-spin the loop).
struct RingOutput {
    writer: RingWriter,
    counters: RingCounters,
    /// The lossy aloop mirror (non-blocking `hw:Loopback,0,7`). `None` if the
    /// mirror PCM could not be opened — the ring still runs (the mirror is a
    /// diagnostic side-tap, never load-bearing for the primary path).
    mirror: Option<PCM>,
    /// One period in nanoseconds (period_frames / 48000). The reader-absent
    /// self-pacing sleep, precomputed so the hot loop never divides.
    self_pace_period_ns: u64,
}

pub struct Mixer {
    inputs: Vec<Input>,
    output: Output,
    /// Per-period scratch: i32 sum buffer absorbs the
    /// saturating-add accumulation before clamping back to i16
    /// in the output buffer. Holds `period_frames * CHANNELS` samples.
    sum_buf: Vec<i32>,
    /// Per-period output buffer (i16 interleaved). Same length as
    /// sum_buf.
    output_buf: Vec<i16>,
    /// Per-period pre-duck program buffer for the assistant loudness
    /// meter. Same length as sum_buf.
    content_meter_buf: Vec<i16>,
    /// Cumulative output frames written since startup. Surfaced via
    /// the STATUS endpoint.
    pub frames_written: Arc<AtomicU64>,
    /// Cumulative output xrun events.
    pub output_xrun_count: Arc<AtomicU64>,
    /// Last observed ALSA playback delay for the primary output PCM.
    /// `OUTPUT_DELAY_UNAVAILABLE` until the first successful sample.
    pub output_delay_frames: Arc<AtomicU64>,
    /// Selected input index. -1 means auto/mix all active inputs;
    /// -2 means pass no renderer lanes; non-negative means pass only
    /// that source's lane. The correction/test lane is always mixed so
    /// diagnostics keep working even if the household selected a
    /// renderer manually or mux temporarily selected NONE.
    selected_input_index: Arc<AtomicI32>,
    /// Channel for forwarding xrun events to the off-thread log
    /// writer. `try_send` is non-blocking on an unbounded channel
    /// (std::sync::mpsc::Sender::send only fails when the receiver
    /// is dropped, which happens at shutdown). Keeps the work loop's
    /// hot path off of disk I/O — the writer thread is the one
    /// stuck on fdatasync.
    xrun_tx: Sender<XrunEvent>,
    period_frames: u32,
    tts: Option<TtsMixer>,
    /// OPTIONAL music-only (pre-TTS) side-output — the multi-room sync
    /// tap (`docs/HANDOFF-multiroom.md` §2 "inv-2 realization"). `None`
    /// on a solo speaker (zero added work). `write_music_only` keeps it a
    /// LOSSY tap so `output` stays the SOLE timing owner (inv-1).
    music_output: Option<PCM>,
    /// Per-period i16 scratch for the music-only output (post-duck,
    /// pre-TTS). Same length as `output_buf`.
    music_only_buf: Vec<i16>,
    /// Cumulative frames written to the music-only output. STATUS.
    pub music_frames_written: Arc<AtomicU64>,
    /// Cumulative periods DROPPED on the music-only output — ring full
    /// (consumer behind) or xrun. A growing value means the snapserver
    /// consumer is behind; surfaced via STATUS, NEVER escalated (inv-1).
    pub music_output_drops: Arc<AtomicU64>,
    /// Coupling transport + (under `transport_pipe`) the shared pipe observability
    /// counters, cloned for the STATUS endpoint. `None` of the pipe fields under
    /// `Loopback` (the default), so STATUS reports `transport=loopback` with no
    /// pipe block — byte-identical to the pre-coupling snapshot.
    pub coupling: CouplingObservability,
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
    /// The relocated impulse tap over the USB DIRECT capture ingress (C4). The
    /// tap detector + the per-lane cumulative capture cursor + the channel to
    /// the writer thread live here; it runs inline over the converted S16 slice
    /// in `read_direct_and_render`, before `push_input`. Present regardless of
    /// the direct flag (a non-direct build simply never calls into it); its
    /// disarmed cost is one relaxed atomic load per direct read (C4).
    direct_tap: DirectTapHook,
    /// The receiver half of the tap channel, taken by `main` to drive the
    /// `fanin-tap-writer` thread (the single JSONL writer). `None` after
    /// `take_direct_tap_receiver`.
    direct_tap_receiver: Option<std::sync::mpsc::Receiver<TapEvent>>,
    /// REVERSE host-clock signals (servo thread → mixer) for the DEFAULT-OFF
    /// post-lock cushion decay. The `fanin-host-clock` thread WRITES these every
    /// servo tick (via the `Arc` clones it takes in `host_clock_signals`); the
    /// mixer's per-period decay tick READS them. `ladder_l0` = the DLL is
    /// `l0_locked` (decay's steady-state gate); `commanded_milli_ppm` = the DLL's
    /// last commanded bias (× 1000) for the cascade guard. When the servo thread
    /// is not running (host-clock off / no direct lane), these stay at their
    /// init (`false` / 0), so decay never leaves the ceiling — decay REQUIRES the
    /// DLL, which is correct.
    host_clock_ladder_l0: Arc<AtomicBool>,
    host_clock_commanded_milli_ppm: Arc<AtomicI64>,
    /// REVERSE host-clock signals for host-compliance REVALIDATION (servo thread →
    /// mixer). `ladder_l2` = the DLL demoted to L2 (probe fail or mid-stream
    /// demotion); `probe_result_code` = the servo's last probe verdict (0 none / 1
    /// pass / 2 fail / 3 aborted). The mixer's per-period compliance tick reads
    /// these to decide a one-strike revocation of a floor-primed proof. Stay at
    /// their init (`false` / 0) when the servo is not running, so revalidation
    /// never fires without a live DLL — same dependency as the decay tick.
    host_clock_ladder_l2: Arc<AtomicBool>,
    host_clock_probe_result_code: Arc<AtomicU64>,
    /// REVERSE host-clock signal: the servo's last probe RESPONSE RATIO ×1000
    /// (i64-bits-in-u64, `PROBE_RATIO_NONE` sentinel = no verdict). The mixer
    /// records it into a persisted proof as evidence of host compliance.
    host_clock_probe_response_ratio_milli: Arc<AtomicU64>,
    /// DEFAULT-OFF (gated behind the cushion-decay flag) host-compliance
    /// persistence state, `Some` only when decay is armed on a resampler lane.
    /// Owns the per-session proof machine, the early-revalidation window, the
    /// on-disk path, the "this session was floor-primed" flag, and the last revoke
    /// reason (for STATUS). `None` (inert, zero work) when the feature is off.
    host_compliance: Option<HostComplianceState>,
}

/// The mixer-thread bookkeeping for host-compliance persistence — the impure
/// shell around the pure [`host_compliance::ComplianceProof`]. `Some` on the
/// mixer only when the cushion-decay feature is armed on a resampler lane
/// (persistence rides that flag; no separate top-level gate). All the actual
/// gate/revoke DECISIONS live in the pure state machine; this struct only holds
/// the wiring the mixer needs to enact them: the path, the per-session proof
/// machine, the floor-primed flag, the early-window budget, and the last revoke
/// reason for STATUS.
struct HostComplianceState {
    /// The on-disk persistence path (`JASPER_FANIN_HOST_COMPLIANCE_PATH` or the
    /// default under the fan-in state dir).
    path: PathBuf,
    /// The pure per-session proof machine. Reset on every lock edge so a fresh
    /// session re-earns the proof.
    proof: crate::host_compliance::ComplianceProof,
    /// The pure lock-edge + one-strike revalidation tracker. Owns the session
    /// bookkeeping (lock edges, early window, unlock baseline, per-lock revoke
    /// latch) and decides — via `step()` — whether a floor-primed session revokes
    /// this period. Extracted so the early-unlock reachability is testable without
    /// ALSA (driven by a real resampler's lock/unlock sequence).
    revalidation: crate::host_compliance::RevalidationTracker,
    /// The shared STATUS observability handles (`resampler.compliance`). The mixer
    /// is the sole writer; the resampler holds a clone for STATUS rendering. This is
    /// the ONLY place the surfaced state (flag_present / proved_at /
    /// revoked_reason_last / consecutive_failures) lives — updated via `on_written`
    /// / `on_revoked` / `on_strike_retained` / `on_pass_reset`.
    obs: crate::host_compliance::HostComplianceObservability,
    /// The proof record the mixer BELIEVES is on disk right now (the last thing it
    /// wrote / loaded), or `None` when no proof is present (never written, or
    /// revoked+deleted). The two-strike `ProbeFail` RETAIN write re-serialises THIS
    /// record with a bumped `consecutive_failures` (preserving the original
    /// `proved_at` / `probe_response_ratio` / `floor_frames`), so the retained
    /// proof still primes the next session at the same floor. Kept in lock-step
    /// with `obs.flag_present`: `Some` iff `flag_present` is true.
    record: Option<crate::host_compliance::HostCompliance>,
    /// Write-once-per-lock guard for the probe-PASS strike-counter reset: a live
    /// probe PASS on a floor-primed session with a nonzero counter persists a
    /// counter=0 record exactly once, not every settled period. Reset on each lock
    /// edge (mirrors the tracker's per-lock latches).
    pass_reset_done_this_lock: bool,
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

/// Coupling transport echo + the shared observability block for the STATUS
/// endpoint. Under `Loopback` (default), both `pipe` and `ring` are `None` and
/// STATUS reports only `transport:"loopback"`. Under `transport_pipe`, `pipe`
/// carries the pipe counters; under `shm_ring`, `ring` carries the ring
/// counters. At most one of `pipe`/`ring` is ever `Some`.
#[derive(Clone)]
pub struct CouplingObservability {
    pub transport: &'static str,
    pub pipe: Option<PipeObservability>,
    pub ring: Option<RingObservability>,
}

/// The shared pipe counters (cloned Arcs from the live `FifoWriter`).
#[derive(Clone)]
pub struct PipeObservability {
    pub path: String,
    pub requested_pipe_bytes: u32,
    pub reopen_count: Arc<AtomicU64>,
    pub dropped_periods: Arc<AtomicU64>,
    pub actual_pipe_bytes: Arc<AtomicU64>,
}

/// The live SPSC ring counters the mixer step updates each period (from the
/// writer's [`jasper_ring::WriterMetrics`] + the mirror side-tap). Cloned into
/// [`RingObservability`] for STATUS so the endpoint reads the same atomics the
/// work loop writes. Distinct from `WriterMetrics` (a value snapshot): these are
/// the shared atomics.
#[derive(Clone)]
struct RingCounters {
    published: Arc<AtomicU64>,
    full_waits: Arc<AtomicU64>,
    drops: Arc<AtomicU64>,
    mirror_frames: Arc<AtomicU64>,
    mirror_drops: Arc<AtomicU64>,
    occupancy: Arc<AtomicU64>,
}

impl RingCounters {
    fn new() -> Self {
        Self {
            published: Arc::new(AtomicU64::new(0)),
            full_waits: Arc::new(AtomicU64::new(0)),
            drops: Arc::new(AtomicU64::new(0)),
            mirror_frames: Arc::new(AtomicU64::new(0)),
            mirror_drops: Arc::new(AtomicU64::new(0)),
            occupancy: Arc::new(AtomicU64::new(0)),
        }
    }
}

/// The shared ring counters (cloned Arcs) for the STATUS endpoint's `ring`
/// block: `{path, slots, occupancy, published, full_waits, drops, mirror_frames,
/// mirror_drops}`. `drops` folds the writer's no-reader + stuck-reader drops
/// (both mean "a live reader did not consume this slot"); `mirror_frames` /
/// `mirror_drops` are the lossy aloop side-tap's written-frame and drop counts
/// (parity with the music-only tap's `music_frames_written` / drops).
#[derive(Clone)]
pub struct RingObservability {
    pub path: String,
    pub slots: u32,
    pub occupancy: Arc<AtomicU64>,
    pub published: Arc<AtomicU64>,
    pub full_waits: Arc<AtomicU64>,
    pub drops: Arc<AtomicU64>,
    pub mirror_frames: Arc<AtomicU64>,
    pub mirror_drops: Arc<AtomicU64>,
}

/// One direct USB capture lane's runtime state (DEFAULT-OFF; only the usbsink
/// lane when `JASPER_FANIN_USB_DIRECT=enabled`). Owns the `hw:UAC2Gadget`
/// S32_LE capture PCM directly — the usbsink bridge hop + aloop cable are gone
/// on this lane. The lane's audio is narrowed to S16 and fed the SAME
/// `LaneResampler` the aloop path would use.
///
/// Presence is dynamic (a UAC2 gadget comes and goes with the host cable), so
/// this is a small state machine: `Present` while the capture is open and
/// reading, `Absent` while the device is unplugged/held-by-the-bridge with a
/// bounded reopen retry counted in periods (C3). No wall clock in the hot loop
/// — the retry cadence is measured in render periods like the auto-trim latch.
enum DirectCapture {
    /// The gadget capture is open; the lane reads it every period.
    Present(PCM),
    /// The gadget is absent (never opened, unplugged, or a runtime loss). Reopen
    /// is retried at most once per `DIRECT_REOPEN_RETRY_PERIODS`; `periods_until_retry`
    /// counts down each period the lane renders (silence).
    Absent { periods_until_retry: u64 },
}

pub struct Input {
    /// The aloop capture PCM for this lane. `None` ONLY on the USB DIRECT lane
    /// (`direct.is_some()`), which does not open its aloop substream at all —
    /// its audio comes from the `hw:UAC2Gadget` capture in `direct`. Every other
    /// lane always has `Some` (the byte-identical-to-today path).
    pcm: Option<PCM>,
    /// DEFAULT-OFF USB DIRECT capture. `Some` only on the usbsink lane when
    /// `JASPER_FANIN_USB_DIRECT=enabled`; the lane then reads `hw:UAC2Gadget`
    /// directly instead of its aloop substream, deleting the bridge+aloop hop.
    /// `None` (and `pcm.is_some()`) on every other lane and on this lane when
    /// the flag is off.
    direct: Option<DirectCapture>,
    pub label: String,
    pub pcm_name: String,
    /// Per-input read buffer (i16 interleaved stereo). Reused as the
    /// discard scratch by the catch-up drain — no per-period allocation.
    read_buf: Vec<i16>,
    pub xrun_count: Arc<AtomicU64>,
    pub frames_read: Arc<AtomicU64>,
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
    /// (drop-free) instead of catch-up-drained; when `None` (the default for
    /// every lane), the read path is byte-for-byte today's behaviour.
    resampler: Option<LaneResampler>,
    /// Per-lane TRIM control + counters, shared with the state-server thread.
    /// The control endpoint sets `pending`; the work loop trims the resampler
    /// ring at the next period boundary (see `maybe_trim` / `trim_input`).
    trim: Arc<TrimControl>,
    /// OPTIONAL USB DIRECT observability, shared with the state-server thread.
    /// `Some` only on the USB DIRECT lane (`direct.is_some()`); STATUS renders a
    /// `direct{}` block from it (C7). `None` (and absent from STATUS) for every
    /// other lane.
    direct_obs: Option<DirectObservability>,
}

/// Shared USB DIRECT counters for the STATUS `direct{}` block (C7). The mixer
/// work thread writes them from the direct-capture state machine; the
/// state-server thread reads them lock-free. Cloned into
/// [`crate::state::InputSnapshotSource`] at construction.
#[derive(Clone)]
pub struct DirectObservability {
    /// The capture device the direct lane opens (`hw:UAC2Gadget` or override).
    pub device: String,
    /// The gadget open period this lane negotiated (frames). Default 256 unless
    /// `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES` overrides it — surfaced in STATUS
    /// so an operator can confirm which geometry the H1 experiment is running.
    pub period_frames: u32,
    /// The gadget capture buffer this lane ACTUALLY negotiated (frames) — the
    /// live `hwp.get_buffer_size()` from the open, not the requested
    /// `resolve_direct_buffer_frames(period)`. The kernel may round the
    /// `set_buffer_size_near` request up (still period-aligned + ≥ floor, so the
    /// open is accepted with a `buffer_near` warn), and this field reports what
    /// the PCM is really running so the STATUS geometry can't overclaim. Atomic
    /// (not a plain `u32`) so a reopen after unplug can re-store the freshly
    /// negotiated size, mirroring the `opens`/`retries`/`present` idiom.
    pub buffer_frames: Arc<AtomicU64>,
    /// Whether the gadget capture is currently open (`Present`) — the live
    /// "is the USB host attached and captured" gauge.
    pub present: Arc<AtomicBool>,
    /// Cumulative successful opens of the gadget capture (climbs on first open
    /// and on every reopen after an unplug/loss).
    pub opens: Arc<AtomicU64>,
    /// Cumulative reopen attempts made while Absent (a growing value with
    /// `present=false` means the gadget is not attachable — bridge holding it,
    /// or no host).
    pub retries: Arc<AtomicU64>,
    /// Cumulative ZOMBIE-handle forced reopens (C, defect 2026-07-05): a run of
    /// `DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS` consecutive zero-avail drains while
    /// Present tripped a close + bounded re-open of the gadget capture. A growing
    /// value means the gadget function is being rebuilt underneath fan-in (UDC
    /// rebind / usbsink stop-start) and this lane is self-healing the deaf handle
    /// instead of needing a manual fan-in restart. Surfaced via STATUS.
    pub reopens: Arc<AtomicU64>,
    /// Consecutive zero-avail drain count (C). Incremented each drain entry where
    /// `avail_update()` reported exactly 0 while Present; reset to 0 the moment any
    /// avail > 0 is seen or the handle transitions to Absent/reopened. When it
    /// reaches `DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS` the zombie-reopen fires. This is
    /// live state (not since-boot), so it lives in `direct_obs` alongside the other
    /// mixer-thread-written / state-thread-read atomics.
    pub zero_avail_streak: Arc<AtomicU64>,
    /// Drain-entry avail dwell stats (lever 2). SINCE-BOOT cumulative (matches
    /// the `opens`/`retries` idiom in this block — no reset-on-read state to
    /// carry, and a monotonic denominator makes the STATUS `mean` a lifetime
    /// average rather than a since-last-poll one). Written lock-free by the
    /// mixer work thread on each drain entry; read lock-free by the state-server
    /// thread for the STATUS `drain_avail{}` sub-block.
    pub drain_stats: DrainStats,
}

/// Since-boot drain-entry avail dwell accumulators (lever 2). One sample per
/// `drain_direct_capture` call: the `avail_update()` reading at drain entry,
/// which is the standing gadget-capture dwell the ~186-frame symptom measures.
/// All fields are lock-free atomics so the mixer work thread can record without
/// a mutex and the state-server thread can read a consistent-enough snapshot for
/// STATUS (each field is independently monotonic; a torn read across fields at
/// most skews one poll's mean by one sample — acceptable for observability).
#[derive(Clone)]
pub struct DrainStats {
    /// Number of drain-entry samples recorded (the histogram/mean denominator).
    pub count: Arc<AtomicU64>,
    /// Running sum of drain-entry avail (frames). `mean = sum / count`.
    pub sum: Arc<AtomicU64>,
    /// Maximum drain-entry avail observed (frames).
    pub max: Arc<AtomicU64>,
    /// Fixed 64-frame-step histogram of drain-entry avail (see
    /// [`drain_avail_bucket`]). Index i counts samples in that bucket.
    pub hist: [Arc<AtomicU64>; DRAIN_AVAIL_BUCKETS],
}

impl Default for DrainStats {
    fn default() -> Self {
        DrainStats::new()
    }
}

impl DrainStats {
    /// Fresh zeroed accumulators. `pub` so the state-server fixtures can build a
    /// direct-lane snapshot without reaching into the atomics field-by-field.
    pub fn new() -> Self {
        DrainStats {
            count: Arc::new(AtomicU64::new(0)),
            sum: Arc::new(AtomicU64::new(0)),
            max: Arc::new(AtomicU64::new(0)),
            hist: std::array::from_fn(|_| Arc::new(AtomicU64::new(0))),
        }
    }

    /// Record one drain-entry avail sample (frames). Lock-free, allocation-free,
    /// syscall-free — safe to call every render cycle on the hot path. Negative
    /// avail (never seen at a real `Ok` reading) is clamped to 0. Returns the
    /// post-increment count so the caller can rate-limit its INFO log off it
    /// without a second load.
    fn record(&self, avail: i64) -> u64 {
        let a = avail.max(0) as u64;
        let count = self.count.fetch_add(1, Ordering::Relaxed) + 1;
        self.sum.fetch_add(a, Ordering::Relaxed);
        self.max.fetch_max(a, Ordering::Relaxed);
        self.hist[drain_avail_bucket(avail)].fetch_add(1, Ordering::Relaxed);
        count
    }
}

impl Mixer {
    /// Open all configured inputs and the output. Every configured input
    /// is required: a missing lane means one renderer silently drops out
    /// of the summed music reference. `xrun_tx` is the
    /// non-blocking channel to the off-thread xrun log writer.
    pub fn new(config: &Config, xrun_tx: Sender<XrunEvent>, tts: Option<TtsInput>) -> Result<Self> {
        let period_samples = (config.period_frames as usize) * (CHANNELS as usize);

        // DEFAULT-OFF host-compliance persistence state, seeded when the cushion
        // decay is armed on the resampler lane (persistence rides that flag). Only
        // one lane ever owns a resampler, so this is set at most once.
        let mut host_compliance: Option<HostComplianceState> = None;

        let mut inputs = Vec::with_capacity(config.input_pcms.len());
        for (label, pcm_name) in config.input_renderers.iter().zip(&config.input_pcms) {
            // DEFAULT-OFF: build a per-input resampler on the configured
            // clock-crossing lane when EITHER the input resampler OR USB DIRECT
            // is enabled (both steer this lane to the DAC clock; direct has no
            // aloop catch-up fallback so it MUST own a resampler — C6). Every
            // other lane (and every lane when both flags are off) gets `None` —
            // the byte-identical-to-today path. A construction failure degrades
            // to `None` with a warning rather than failing the daemon.
            let mut resampler = if config.lane_wants_resampler(label) {
                build_lane_resampler(label, config)
            } else {
                None
            };
            // DEFAULT-OFF host-compliance PRIME-AT-FLOOR: when the cushion decay is
            // armed on this resampler lane, seed the per-session compliance state
            // and — if a VALID persisted proof is on disk whose recorded floor
            // matches this lane's live floor — prime the resampler AT the decay
            // floor so it skips the ~2.5-min descent this session. The per-session
            // servo probe revalidates the prime (a mismatch/regression revokes it).
            if let Some(r) = resampler.as_mut() {
                if config.input_resampler_cushion_decay_enabled {
                    host_compliance = Some(build_host_compliance_state(label, config, r));
                }
            }
            let resampler = resampler;
            // USB DIRECT: this lane reads hw:UAC2Gadget directly instead of its
            // aloop substream (DEFAULT-OFF; only the resampler lane label). The
            // open is best-effort — a gadget-absent lane starts Absent and
            // renders silence with a bounded reopen retry (C3), never failing
            // the daemon (the fail-hard "every input required" contract is
            // exempted ONLY for this lane).
            let is_direct =
                config.usb_direct_enabled && label == &config.input_resampler_lane_label;
            let input = if is_direct {
                open_direct_input(label, pcm_name, config, resampler)
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
                "event=fanin.input.opened label={} pcm={} period_frames={} buffer_frames={} direct={}",
                label,
                pcm_name,
                config.period_frames,
                config.input_buffer_frames,
                is_direct,
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

        // DEFAULT-OFF feature: if the resampler is armed by env but its
        // configured lane label matched no live input, NO `LaneResampler` was
        // constructed above — the feature silently no-ops. Surface that ONCE
        // (the review's flagged-missing diagnostic) so an operator who set the
        // env var can see WHY they observed no effect, with the available
        // labels to fix the typo.
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

        // Final-output transport. Loopback (default) opens the ALSA snd-aloop
        // substream — byte-identical to today. Fifo ensures + lazily opens the
        // bounded named pipe CamillaDSP RawFile-captures (the lower-latency
        // coupling). Exactly one is active.
        let (output, coupling) = match config.camilla_coupling {
            Coupling::Loopback => {
                let pcm = open_output(&config.output_pcm, config)
                    .with_context(|| format!("opening output PCM {}", config.output_pcm))?;
                info!(
                    "event=fanin.output.opened transport=alsa pcm={} period_frames={} buffer_frames={}",
                    config.output_pcm, config.period_frames, config.output_buffer_frames,
                );
                (
                    Output::Alsa(pcm),
                    CouplingObservability {
                        transport: "loopback",
                        pipe: None,
                        ring: None,
                    },
                )
            }
            Coupling::TransportPipe => {
                // The pipe is created here (producer owns it); the write end is
                // opened reader-first lazily on the first period so startup is
                // never gated on CamillaDSP being up.
                let writer = FifoWriter::new(
                    &config.camilla_pipe_path,
                    config.period_frames,
                    config.camilla_pipe_bytes,
                )
                .with_context(|| {
                    format!("ensuring fan-in→camilla pipe {}", config.camilla_pipe_path)
                })?;
                info!(
                    "event=fanin.output.opened transport=transport_pipe path={} period_frames={} requested_pipe_bytes={}",
                    config.camilla_pipe_path, config.period_frames, config.camilla_pipe_bytes,
                );
                // Capture the shared counters before the writer moves into Output.
                let (reopen_count, dropped_periods, actual_pipe_bytes) = writer.observability();
                (
                    Output::Fifo(writer),
                    CouplingObservability {
                        transport: "transport_pipe",
                        pipe: Some(PipeObservability {
                            path: config.camilla_pipe_path.clone(),
                            requested_pipe_bytes: config.camilla_pipe_bytes,
                            reopen_count,
                            dropped_periods,
                            actual_pipe_bytes,
                        }),
                        ring: None,
                    },
                )
            }
            Coupling::ShmRing => {
                // Ring A (PROTOTYPE). Create-or-attach the SPSC ring as the
                // WRITER. Geometry: S16LE / 2ch / 48k, slot = 128 frames pinned
                // (the outputd DAC-period contract; period_frames % 128 == 0 was
                // validated at config parse), n_slots = ring_slots. A geometry
                // mismatch against an already-created ring is fail-loud (systemd
                // parks, not reboot-loops).
                let geometry = Geometry {
                    rate: config.sample_rate,
                    channels: CHANNELS,
                    sample_format: SAMPLE_FORMAT_S16LE,
                    period_frames: RING_SLOT_FRAMES,
                    n_slots: config.ring_slots,
                };
                let writer = RingWriter::create_or_attach(&config.ring_path, geometry)
                    .with_context(|| {
                        format!("opening fan-in→camilla SHM ring {}", config.ring_path)
                    })?;
                // The lossy aloop MIRROR keeps the AEC-fallback dsnoop + aloop
                // diagnostics live. BEST-EFFORT (non-blocking open): a failure
                // to open it must NOT take down the ring — the mirror is a
                // diagnostic side-tap, never the pacer.
                let mirror = match open_music_output(&config.output_pcm, config) {
                    Ok(pcm) => {
                        info!(
                            "event=fanin.ring.mirror_opened pcm={} (lossy aloop mirror)",
                            config.output_pcm,
                        );
                        Some(pcm)
                    }
                    Err(e) => {
                        warn!(
                            "event=fanin.ring.mirror_open_failed pcm={} detail={:#} — \
                             continuing WITHOUT the aloop mirror (ring path unaffected)",
                            config.output_pcm, e,
                        );
                        None
                    }
                };
                let counters = RingCounters::new();
                let self_pace_period_ns =
                    (config.period_frames as u64) * 1_000_000_000 / (config.sample_rate as u64);
                info!(
                    "event=fanin.ring.opened path={} slots={} slot_frames={} period_frames={} slots_per_step={}",
                    config.ring_path,
                    config.ring_slots,
                    RING_SLOT_FRAMES,
                    config.period_frames,
                    config.period_frames / RING_SLOT_FRAMES,
                );
                let observability = RingObservability {
                    path: config.ring_path.clone(),
                    slots: config.ring_slots,
                    occupancy: Arc::clone(&counters.occupancy),
                    published: Arc::clone(&counters.published),
                    full_waits: Arc::clone(&counters.full_waits),
                    drops: Arc::clone(&counters.drops),
                    mirror_frames: Arc::clone(&counters.mirror_frames),
                    mirror_drops: Arc::clone(&counters.mirror_drops),
                };
                (
                    Output::Ring(RingOutput {
                        writer,
                        counters,
                        mirror,
                        self_pace_period_ns,
                    }),
                    CouplingObservability {
                        transport: "shm_ring",
                        pipe: None,
                        ring: Some(observability),
                    },
                )
            }
        };

        // OPTIONAL music-only side-output (multi-room sync tap). Opened
        // BEST-EFFORT: a configured-but-unopenable music PCM must NEVER
        // take down the primary audio path, so on failure we log and run
        // as a solo speaker (music_output = None). Non-blocking so the
        // lossy-tap write can drop-on-full without ever blocking the work
        // loop (inv-1: `output` stays the sole timing owner).
        let music_output = match &config.music_output_pcm {
            Some(pcm_name) => match open_music_output(pcm_name, config) {
                Ok(pcm) => {
                    info!(
                        "event=fanin.music_output.opened pcm={} (multi-room sync tap)",
                        pcm_name,
                    );
                    Some(pcm)
                }
                Err(e) => {
                    warn!(
                        "event=fanin.music_output.open_failed pcm={} detail={:#} — \
                         continuing WITHOUT the music-only tap (primary output unaffected)",
                        pcm_name, e,
                    );
                    None
                }
            },
            None => {
                info!("event=fanin.music_output.disabled (solo speaker; no sync tap)");
                None
            }
        };

        let input_count = inputs.len();
        // AUTO-TRIM delay in frames: `AUTO_TRIM_DELAY_SECONDS` at the live rate.
        // Only consulted when the DEFAULT-OFF flag is set.
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
        // Impulse-tap channel (C4). Default-disarmed: the tap state starts
        // unarmed so the direct read pays one relaxed atomic load until a
        // TAP_ARM verb arrives. The bounded channel keeps the mixer thread's
        // hand-off non-blocking (drop-and-count on Full); the fanin-tap-writer
        // thread (spawned in main) is the sole JSONL writer.
        let (tap_sender, tap_receiver) =
            std::sync::mpsc::sync_channel::<TapEvent>(TAP_CHANNEL_CAPACITY);
        let direct_tap = DirectTapHook::new(
            Arc::new(TapState::default()),
            Arc::new(Mutex::new(TapConfig::default())),
            tap_sender,
        );
        Ok(Self {
            inputs,
            output,
            sum_buf: vec![0i32; period_samples],
            output_buf: vec![0i16; period_samples],
            content_meter_buf: vec![0i16; period_samples],
            frames_written: Arc::new(AtomicU64::new(0)),
            output_xrun_count: Arc::new(AtomicU64::new(0)),
            output_delay_frames: Arc::new(AtomicU64::new(OUTPUT_DELAY_UNAVAILABLE)),
            selected_input_index: Arc::new(AtomicI32::new(-2)),
            xrun_tx,
            period_frames: config.period_frames,
            tts: tts.map(TtsMixer::new),
            music_output,
            music_only_buf: vec![0i16; period_samples],
            music_frames_written: Arc::new(AtomicU64::new(0)),
            music_output_drops: Arc::new(AtomicU64::new(0)),
            coupling,
            auto_trim_enabled: config.auto_trim_enabled,
            auto_trim_delay_frames,
            auto_trim_lane_state: vec![AutoTrimLaneState::default(); input_count],
            direct_tap,
            direct_tap_receiver: Some(tap_receiver),
            // Reverse host-clock signals for the DEFAULT-OFF cushion decay. Init
            // to the inert state (not-l0, 0 ppm) so decay never leaves the
            // ceiling until the servo thread actually reports `l0_locked`.
            host_clock_ladder_l0: Arc::new(AtomicBool::new(false)),
            host_clock_commanded_milli_ppm: Arc::new(AtomicI64::new(0)),
            // Reverse host-clock signals for host-compliance REVALIDATION. Init to
            // the inert state (not-l2, no probe verdict) so a floor-primed session
            // only revokes on a LIVE demotion/probe-fail from the servo.
            host_clock_ladder_l2: Arc::new(AtomicBool::new(false)),
            host_clock_probe_result_code: Arc::new(AtomicU64::new(0)),
            // Init to the None sentinel (no probe verdict) so a pre-servo period
            // records `None` rather than a stale zero ratio.
            host_clock_probe_response_ratio_milli: Arc::new(AtomicU64::new(
                crate::host_clock::PROBE_RATIO_NONE as u64,
            )),
            host_compliance,
        })
    }

    /// Number of configured inputs. Mixer construction fails if any
    /// configured input cannot be opened.
    pub fn input_count(&self) -> usize {
        self.inputs.len()
    }

    /// Read-only access to per-input counters for the STATUS endpoint
    /// (chunk 3 will use this).
    pub fn inputs(&self) -> &[Input] {
        &self.inputs
    }

    /// Clone the cross-thread signals the combo-mode `fanin-host-clock` thread
    /// reads (C5). Returns `Some` ONLY when the USB DIRECT lane exists AND owns a
    /// resampler (the normal combo-mode shape); `None` when direct is off, or
    /// when resampler construction failed and the lane fell back to no resampler
    /// (fail-soft — the caller then runs inert, warn-once). The `HostClock`
    /// thread holds only these `Arc` atomics; it never touches the mixer.
    ///
    /// The signals ride the atomics the mixer already publishes for STATUS
    /// (resampler `fill_frames`/`input_frames`/`output_frames`/`locked`, direct
    /// `present`), so this adds no new hot-path work — it only clones the
    /// existing `Arc`s. `main` calls this before `mixer.run`. Note: `input_frames`
    /// is passed RAW; the host-clock adapter must NOT trim-compensate it (a
    /// `trim_ring` moves only the read cursor, so the `capture − playback`
    /// divergence the ladder differences is already trim-invariant).
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
            // The resampler's LIVE correction ppm gauge (its `ratio_milli_ppm`,
            // milli-ppm i64-bits-in-u64) — the COMBO-mode probe/servo observable.
            // Owned/written by the resampler on the mixer thread; the servo thread
            // only ever READS it (single source of truth, no new hot-path work —
            // just clones the existing Arc).
            correction_milli_ppm: Arc::clone(&resampler.ratio_milli_ppm),
            // The LIVE held-target gauge — the single source of truth the servo
            // thread re-pins its setpoint to each tick (tracks the cushion decay).
            held_target_frames: Arc::clone(&resampler.held_target_frames),
            // The REVERSE signals the servo thread writes and the mixer's decay
            // tick reads. Owned here (created with the mixer) so both sides share
            // the same atomics; the servo thread only ever WRITES these two.
            ladder_l0: Arc::clone(&self.host_clock_ladder_l0),
            commanded_milli_ppm: Arc::clone(&self.host_clock_commanded_milli_ppm),
            // The revalidation reverse signals — written by the servo, read by the
            // mixer's per-period compliance tick.
            ladder_l2: Arc::clone(&self.host_clock_ladder_l2),
            probe_result_code: Arc::clone(&self.host_clock_probe_result_code),
            probe_response_ratio_milli: Arc::clone(&self.host_clock_probe_response_ratio_milli),
        })
    }

    /// The shared impulse-tap state (armed + counters + knobs), cloned for the
    /// state-server thread's `TAP_ARM`/`TAP_DISARM`/STATUS handling (C4).
    pub fn direct_tap_state(&self) -> Arc<TapState> {
        self.direct_tap.state()
    }

    /// The last-armed impulse-tap config, cloned for the state-server thread
    /// (published on arm, read on STATUS) and the writer thread (C4).
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
        // Prime + start is ALSA-specific. The FIFO transport has no kernel ring
        // to prime and no PREPARED→RUNNING transition; its write end opens
        // reader-first lazily inside step() and paces on the pipe.
        if let Output::Alsa(pcm) = &self.output {
            // Prime the output: write one period of zeros so the kernel
            // ring is non-empty when CamillaDSP / AEC bridge start reading.
            // Without this prime, the first writei could see -EPIPE
            // (underrun) before any data has been queued.
            self.output_buf.fill(0);
            write_output(
                pcm,
                &self.output_buf,
                &self.output_xrun_count,
                &self.xrun_tx,
            )?;

            // Start the output stream now that it's primed. (PCM::new
            // with the default access creates the stream in PREPARED state;
            // explicit start() puts it in RUNNING.)
            if pcm.state() != State::Running {
                pcm.start().context("starting output PCM")?;
            }
        }

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
        // 1. Clear the i32 sum scratch.
        self.sum_buf.fill(0);

        // 2. Drain TTS/control commands once at the period boundary.
        // When voice ducking is routed through fan-in, attenuate only
        // renderer/program lanes. TTS is mixed after this step so it
        // remains audible and then flows through CamillaDSP crossover.
        let mut program_gain = 1.0f32;
        if let Some(tts) = self.tts.as_mut() {
            if tts.prepare_period() {
                program_gain = tts.program_duck_gain();
            }
        }

        // 2b. Service TRIM requests (manual control-endpoint `pending` flags +
        // the DEFAULT-OFF one-shot AUTO-TRIM latch) at the period boundary,
        // before the read loop, so the render below sees the trimmed ring. A
        // no-request period does one atomic load per lane and nothing else.
        let period_frames = self.period_frames as usize;
        self.maybe_trim();

        // 2c. Snapshot the REVERSE host-clock signals ONCE per period for the
        // DEFAULT-OFF cushion-decay tick below (avoids a self-borrow inside the
        // per-input loop). `l0` gates decay to the DLL's steady state;
        // `commanded_ppm_abs` drives the cascade guard. Both are inert (false / 0)
        // when the servo thread is not running, so decay never leaves the ceiling
        // without the DLL — the correct dependency.
        let decay_l0 = self.host_clock_ladder_l0.load(Ordering::Relaxed);
        let decay_commanded_ppm_abs =
            (self.host_clock_commanded_milli_ppm.load(Ordering::Relaxed) as f64 / 1000.0).abs();
        // 2d. Snapshot the REVERSE host-clock revalidation signals ONCE per period
        // for the host-compliance service below (same self-borrow avoidance as the
        // decay signals). `ladder_l2` = probe-fail / mid-stream demotion;
        // `probe_result_code` distinguishes a fresh probe FAIL from a later L2.
        // Inert (false / 0) when the servo is not running.
        let compliance_ladder_l2 = self.host_clock_ladder_l2.load(Ordering::Relaxed);
        let compliance_probe_code = self.host_clock_probe_result_code.load(Ordering::Relaxed);
        let compliance_probe_ratio = crate::host_clock::decode_response_ratio_milli(
            self.host_clock_probe_response_ratio_milli
                .load(Ordering::Relaxed) as i64,
        );

        // 3. Read from each input, accumulate into sum_buf.
        let selected_input = self.selected_input_index.load(Ordering::Relaxed);
        for (idx, input) in self.inputs.iter_mut().enumerate() {
            let frames = if input.direct.is_some() {
                // USB DIRECT lane (DEFAULT-OFF): read hw:UAC2Gadget directly,
                // narrow S32→S16, feed the SAME resampler, render one DAC-paced
                // period. Gadget-absent → silence + bounded reopen retry (C3).
                // The aloop substream is never touched (`pcm` is None). The tap
                // (C4) runs inline over the converted slice inside this call.
                read_direct_and_render(input, period_frames, &mut self.direct_tap, &self.xrun_tx)
            } else if input.resampler.is_some() {
                // ARMED clock-crossing lane (DEFAULT-OFF; only the USB lane when
                // enabled). The resampler OWNS rate reconciliation: read ALL
                // available frames into it (DLL-steered to the DAC clock) and
                // render exactly one DAC-paced period. The catch-up drain is
                // bypassed here on purpose — the resampler holds the ring at a
                // small fixed fill (no sawtooth), which is the whole point.
                read_into_resampler_and_render(input, period_frames, &self.xrun_tx)?
            } else {
                // DEFAULT path — byte-for-byte today's behaviour.
                //
                // Bounded catch-up resync BEFORE the period read, for EVERY lane
                // regardless of selection. A free-running lane (the USB host-clock
                // lane) backs its capture ring up past the high-water; we discard
                // the excess down to one period here so the upstream producer never
                // overflows and back-pressure can reach the host. A DAC-locked lane
                // sits at one period and this is a single `avail_update` no-op.
                // INTENTIONALLY independent of `input_selected` below: a de-selected
                // (muxed-out) free-running lane STILL backs up and must be drained,
                // so do NOT move this under the selection gate. Drop-controlled,
                // not drop-free — see the constant docs.
                drain_input_excess(input, period_frames);
                read_input(input, period_frames, &self.xrun_tx)?
            };
            // 3b. Advance the DEFAULT-OFF post-lock cushion decay one render
            // period on this lane's resampler (if armed). Done AFTER the render
            // so the tick sees this period's fresh lock state, and independent of
            // the selection gate below (a de-selected but locked lane must keep
            // decaying — the held target is a property of the lane's clock
            // reconciliation, not of which source is passed to the sum). No-op
            // when no resampler / decay disabled.
            if let Some(r) = input.resampler.as_mut() {
                r.tick_decay(decay_l0, decay_commanded_ppm_abs);
            }
            if !input_selected(selected_input, idx, &input.label) {
                continue;
            }
            // Only sum the samples we actually got. `read_input`
            // zero-pads the tail of input.read_buf so reading the
            // full period is also safe; explicit bounds save a few
            // unnecessary saturating_add calls when an input is
            // silent.
            let active = frames * (CHANNELS as usize);
            mix_into(&mut self.sum_buf[..active], &input.read_buf[..active]);
        }
        // 3c. Service the DEFAULT-OFF host-compliance persistence — write the proof
        // once the descent settles clean at the floor, and run the one-strike
        // revalidation on a floor-primed session. No-op (a single `Option::is_none`
        // check) when the feature is off. Done after the decay tick so it sees this
        // period's fresh held-target / lock state.
        self.service_host_compliance(
            decay_l0,
            compliance_ladder_l2,
            compliance_probe_code,
            compliance_probe_ratio,
        );
        if let Some(tts) = self.tts.as_mut() {
            saturate_to_i16(&self.sum_buf, &mut self.content_meter_buf);
            tts.observe_content_period(&self.content_meter_buf);
        }
        if program_gain != 1.0 {
            apply_gain_to_sum(&mut self.sum_buf, program_gain);
        }
        // Music-only side-tap (multi-room sync): the program AS PLAYED
        // minus the assistant — taken POST-duck (so a synced follower
        // hears the music dip under the leader's local TTS, matching the
        // room) and PRE-TTS (so the leader's assistant NEVER leaks to
        // followers — the inv-3 guarantee). Lossy: drop-on-full, never
        // blocks, never escalates — the primary `output` below stays the
        // sole timing owner (inv-1). `None` on a solo speaker → no work.
        if let Some(music_out) = self.music_output.as_ref() {
            saturate_to_i16(&self.sum_buf, &mut self.music_only_buf);
            write_music_only(
                music_out,
                &self.music_only_buf,
                &self.music_frames_written,
                &self.music_output_drops,
            );
        }
        if let Some(tts) = self.tts.as_mut() {
            tts.mix_period(&mut self.sum_buf);
        }

        // 4. Clamp i32 sum -> i16 output.
        saturate_to_i16(&self.sum_buf, &mut self.output_buf);

        // 5. Write to output (blocks; paces the loop). Dispatch on transport:
        //    - Alsa: blocking writei, returns when the loopback ring has room
        //      (DAC-paced via the dsnoop consumer). Counts every period.
        //    - Fifo: blocking pipe write, returns when the pipe has room
        //      (DAC-paced via CamillaDSP's RawFile capture). A reader-gone /
        //      no-reader turn returns Waited (the bounded reopen-wait already
        //      slept), dropping this period; we still return Ok so run() bumps
        //      the heartbeat — the loop is alive and bounded, never wedged.
        //    - Ring: publish period_frames/128 slots into the SHM ring. The
        //      blocking-on-full publish (bounded, live reader) is the pacer;
        //      reader-absent self-paces (one period's sleep per dropped publish)
        //      so a readerless ring never hot-spins. The mixed sum_buf (post-duck,
        //      post-TTS) is what enters — TTS/duck ride along with zero special
        //      handling. The lossy aloop mirror is written (never the pacer).
        match &mut self.output {
            Output::Alsa(pcm) => {
                write_output(
                    pcm,
                    &self.output_buf,
                    &self.output_xrun_count,
                    &self.xrun_tx,
                )?;
                store_output_delay(pcm, &self.output_delay_frames);
                self.frames_written
                    .fetch_add(self.period_frames as u64, Ordering::Relaxed);
            }
            Output::Fifo(writer) => {
                match writer.write_period(&self.output_buf) {
                    FifoWriteOutcome::Wrote => {
                        self.frames_written
                            .fetch_add(self.period_frames as u64, Ordering::Relaxed);
                    }
                    FifoWriteOutcome::Waited => {
                        // No reader / reader-gone: the writer already waited a
                        // bounded REOPEN_WAIT. Drop this period (CamillaDSP is
                        // reloading or not yet up) — do NOT count frames. The
                        // loop stays alive; the heartbeat is bumped by run().
                    }
                }
            }
            Output::Ring(ring) => {
                // Count only frames that actually ENTERED the ring — a
                // fully-dropped period (reader absent / stuck) adds nothing,
                // matching the Fifo arm's Waited (which deliberately doesn't
                // count). `drops` disambiguates, so the top-line counter stays
                // honest rather than optimistic.
                let published_frames =
                    write_ring_period(ring, &self.output_buf, self.period_frames);
                self.frames_written
                    .fetch_add(published_frames as u64, Ordering::Relaxed);
            }
        }
        Ok(())
    }

    /// Service TRIM at the period boundary, before the render loop. Two
    /// triggers, both funnel through the single `trim_input` path:
    ///   - MANUAL: a control-endpoint `TRIM` set the lane's `pending` flag.
    ///     Consumed (cleared) here with an `Acquire` swap so the request is
    ///     handled exactly once even if two `TRIM`s raced in.
    ///   - AUTO (DEFAULT-OFF): the one-shot latch, ~`AUTO_TRIM_DELAY_SECONDS`
    ///     after a lane goes active, guarded by `TrimControl::auto_fired` so it
    ///     fires at most once per idle→active session.
    ///
    /// Service the DEFAULT-OFF host-compliance persistence once per render period.
    /// No-op (a single `Option::is_none`) when the feature is off. When armed:
    ///
    /// 1. Drive the pure `RevalidationTracker`: it runs the one-strike revalidation
    ///    of a floor-primed session against the pre-reset lock baseline. Immediate
    ///    triggers (a LIVE probe FAIL, a DLL demotion to L2) revoke the period the
    ///    evidence appears; the EarlyUnlock churn trigger is two-phase — an
    ///    early-window underfill unlock ARMS a pending strike that CONFIRMS (revoke)
    ///    only if a RELOCK follows within the churn-confirm horizon, so a terminal
    ///    stream-end (unlock with no relock — the macOS short-session norm) expires
    ///    harmlessly and does NOT burn the proof. The tracker applies the lock-edge
    ///    bookkeeping and returns the decision + the edges. On a returned revoke,
    ///    snap the held target back to the ceiling and delete the persisted proof;
    ///    `on_revoked` clears `flag_present` so the relocked lock is not floor-primed
    ///    (the revoke wins the relock's floor consideration). Reset the pure proof
    ///    machine on either lock edge.
    /// 2. TICK the pure proof machine; on its `Write` outcome, persist a fresh
    ///    record (atomic tempfile+rename). Written at most once per session.
    ///
    /// RENDER-THREAD I/O CAVEAT. The three `HostCompliance::store` paths here (the
    /// settle-write in (2), plus the two-strike `strike_retained` retain-write and
    /// the `pass_reset` clear-write in (1)) each do a synchronous
    /// `File::create`+`sync_all`+`rename` on THIS render thread — SD-card I/O inside
    /// the ~5.3 ms period budget. Each is bounded to at most once per lock (settle
    /// once per descent; strike/pass-reset gated by `pass_reset_done_this_lock` and
    /// the strike edge), so the steady-state period does ZERO proof I/O, and a slow
    /// fsync only risks a self-inflicted underfill at the exact rare moment the
    /// machinery is judging underfills. Acceptable for now (same class as the
    /// pre-existing settle-write, kept pattern-consistent); hoisting proof I/O onto a
    /// dedicated writer thread is a clean follow-up if a slow-fsync underfill is ever
    /// observed.
    ///
    /// Uses `Option::take` on `self.host_compliance` so it can freely borrow
    /// `self.inputs` (the resampler lane) without a double-mutable-borrow; the
    /// state is put back before returning. All the gate DECISIONS are in the pure
    /// `ComplianceProof` / `RevalidationTracker` / `HostCompliance`; this method is
    /// only the wiring.
    fn service_host_compliance(
        &mut self,
        dll_l0: bool,
        ladder_l2: bool,
        probe_code: u64,
        probe_response_ratio: Option<f64>,
    ) {
        use crate::host_compliance::{classify_strike, HostCompliance, ProofOutcome, ProofSignals};
        // The servo probe-result code for a PASS (see
        // `host_clock::probe_result_code`: 0 None, 1 Pass, 2 Fail, 3 Aborted).
        // Kept a plain literal so this method stays independent of the host_clock
        // adapter (the pure module already inlines the FAIL value the same way).
        const PROBE_RESULT_PASS: u64 = 1;
        let Some(mut hc) = self.host_compliance.take() else {
            return;
        };
        // The resampler lane is the single lane that owns a resampler.
        let Some(resampler) = self
            .inputs
            .iter_mut()
            .find_map(|inp| inp.resampler.as_mut())
        else {
            // Resampler gone (should not happen once seeded); park the state.
            self.host_compliance = Some(hc);
            return;
        };

        let locked = resampler.is_locked();
        let unlock_count = resampler.unlock_count();
        let decay_at_floor = resampler.decay_at_floor();
        // The LIVE proof-present signal — the SAME `flag_present` atomic the
        // resampler's session-boundary snap-back reads to pick floor-vs-ceiling.
        // Passing it into the tracker makes the per-lock `floor_primed`
        // revalidation gate and the snap destination share one source of truth: a
        // lock that primed at the floor (proof live) is revalidated; a lock after a
        // revoke (proof cleared) is not.
        let floor_primed_now = hc.obs.flag_present.load(Ordering::Relaxed);

        // 1. Lock-edge bookkeeping + one-strike revalidation, in the pure tracker.
        // `step` runs the revalidation against the PRE-reset baseline (so the
        // falling-edge underfill unlock is caught — the ONLY period where an
        // underfill unlock presents, since `unlock_for_underfill` sets `locked=false`
        // in the same render period it bumps `unlock_count`), then applies the
        // lock-edge bookkeeping. It re-samples `floor_primed` from the live proof at
        // the rising edge and returns the revoke decision plus the observed edges so
        // the proof machine's reset stays in lock-step with the tracker.
        let step = hc.revalidation.step(
            locked,
            unlock_count,
            probe_code,
            ladder_l2,
            floor_primed_now,
        );
        if let Some(reason) = step.revoke {
            // A floor-primed revalidation failure. EVERY strike snaps the held
            // target back to the full ceiling so THIS session re-acquires deep and
            // re-descends — identical user-visible behaviour regardless of whether
            // the proof is deleted or retained. The two-strike policy
            // (`classify_strike`) then decides the ON-DISK outcome: a DLL demotion
            // or confirmed churn deletes immediately (one strike); a probe FAIL
            // RETAINS the proof (bumping `consecutive_failures`) the first time and
            // deletes only on the second, so one spurious probe read (a lock-gated
            // probe firing during a railed acquisition) never costs the household
            // the ~2.5-min descent on the NEXT session.
            resampler.snap_decay_to_ceiling();
            // A strike only fires on a floor-primed lock, which requires a live
            // proof, so `record` is normally `Some`. Bind it directly (no
            // unwrap/expect): a `None` here (proof already gone) means there is
            // nothing to retain — do a plain revoke, which is idempotent on an
            // absent file. Take the record out so the retain branch can move it
            // into the updated copy without a borrow tangle; the branches put back
            // whatever the outcome leaves (`Some` on a retain, `None` on a delete).
            let record = hc.record.take();
            let current_failures = record.as_ref().map(|r| r.consecutive_failures).unwrap_or(0);
            let action = match &record {
                Some(_) => classify_strike(reason, current_failures),
                None => crate::host_compliance::StrikeAction::Revoke,
            };
            match (action, record) {
                (
                    crate::host_compliance::StrikeAction::RetainWithStrike {
                        consecutive_failures,
                    },
                    Some(rec),
                ) => {
                    // First probe-fail strike: keep the proof, persist the bumped
                    // counter, and leave `flag_present` TRUE so the NEXT session
                    // still primes at the floor.
                    let updated = rec.with_consecutive_failures(consecutive_failures);
                    match updated.store(&hc.path) {
                        Ok(()) => {
                            hc.obs.on_strike_retained(reason, consecutive_failures);
                            hc.record = Some(updated);
                            warn!(
                                "event=fanin.host_compliance.strike_retained reason={} \
                                 consecutive_failures={} path={} — snapped held target back to \
                                 the ceiling for THIS session; proof RETAINED (one bad \
                                 measurement does not cost the floor), next session still primes",
                                reason.as_str(),
                                consecutive_failures,
                                hc.path.display(),
                            );
                        }
                        Err(e) => {
                            // The retain write failed. The proof on disk is
                            // unchanged (still the pre-strike counter), and
                            // `flag_present` stays true, so the next session still
                            // primes — the strike is simply not recorded this time.
                            // Audio is unaffected (this session already snapped to
                            // the ceiling). Keep the record so `flag_present` and
                            // the in-memory proof stay coherent (fail toward "keep
                            // priming", matching the retain intent).
                            hc.record = Some(updated);
                            warn!(
                                "event=fanin.host_compliance.strike_write_io_failed reason={} \
                                 path={} detail={} — strike not persisted (proof retained at the \
                                 prior counter; audio unaffected)",
                                reason.as_str(),
                                hc.path.display(),
                                e,
                            );
                        }
                    }
                }
                // Every other case is a DELETE revoke: a `Revoke` action (DLL
                // demotion, confirmed churn, or the 2nd consecutive probe fail), or
                // the defensive `None`-record path. `hc.record` was already taken
                // (left `None`), which is exactly the post-delete state.
                (_, _record) => {
                    match HostCompliance::revoke(&hc.path) {
                        Ok(()) => {}
                        Err(e) => warn!(
                            "event=fanin.host_compliance.revoke_io_failed reason={} path={} detail={}",
                            reason.as_str(),
                            hc.path.display(),
                            e,
                        ),
                    }
                    hc.obs.on_revoked(reason);
                    // `consecutive_failures` in this log is the PROBE-FAIL counter,
                    // which only `ProbeFail` increments. A `ProbeFail` delete is the
                    // 2nd consecutive fail, so `current + 1` (= the limit) is the
                    // honest count. A `DllDemotion` / confirmed `EarlyUnlock` delete
                    // is a ONE-strike revoke that does NOT touch the probe-fail
                    // counter (classify_strike returns Revoke regardless of it), so
                    // log the counter UNCHANGED — reporting `current + 1` there would
                    // fabricate a probe-fail count that never happened (e.g.
                    // `consecutive_failures=1` on a clean proof), misleading incident
                    // forensics on this very event stream.
                    let logged_consecutive_failures = match reason {
                        crate::host_compliance::RevokeReason::ProbeFail => {
                            current_failures.saturating_add(1)
                        }
                        crate::host_compliance::RevokeReason::DllDemotion
                        | crate::host_compliance::RevokeReason::EarlyUnlock => current_failures,
                    };
                    warn!(
                        "event=fanin.host_compliance.revoked reason={} consecutive_failures={} \
                         path={} — deleted the proof, snapped held target back to the ceiling; \
                         the normal descent will re-prove",
                        reason.as_str(),
                        logged_consecutive_failures,
                        hc.path.display(),
                    );
                }
            }
        } else if hc.record.is_some()
            && !hc.pass_reset_done_this_lock
            && floor_primed_now
            && probe_code == PROBE_RESULT_PASS
            && dll_l0
        {
            // Probe-PASS strike-counter reset. A LIVE probe pass on a floor-primed
            // session (verdict PASS *and* the DLL sitting at L0 — the pass promoted
            // it there, so a STALE carried-over pass, which reads while the ladder
            // is still Probing with `dll_l0 == false`, is ignored) forgives an
            // earlier spurious probe fail: re-persist the proof with
            // `consecutive_failures == 0`. Only when the current counter is nonzero
            // (nothing to reset otherwise) and at most once per lock (the
            // `pass_reset_done_this_lock` latch), so a settled L0 session does not
            // re-write every period. This is the explicit companion to the natural
            // reset the full-descent re-write already performs via `on_written`.
            // Bind the record directly (the outer `hc.record.is_some()` guard is
            // this branch's condition; no unwrap/expect). Only rewrite when the
            // counter is nonzero — otherwise there is nothing to clear, just latch
            // done so we don't re-check every settled period.
            let cleared = hc
                .record
                .as_ref()
                .filter(|r| r.consecutive_failures != 0)
                .map(|r| r.with_consecutive_failures(0));
            match cleared {
                Some(cleared) => match cleared.store(&hc.path) {
                    Ok(()) => {
                        hc.obs.on_pass_reset();
                        hc.record = Some(cleared);
                        hc.pass_reset_done_this_lock = true;
                        info!(
                            "event=fanin.host_compliance.pass_reset path={} — live probe pass on \
                             a floor-primed session cleared the strike counter to 0",
                            hc.path.display(),
                        );
                    }
                    Err(e) => warn!(
                        "event=fanin.host_compliance.pass_reset_io_failed path={} detail={} — \
                         strike counter not cleared this session (proof otherwise intact)",
                        hc.path.display(),
                        e,
                    ),
                },
                None => {
                    // Counter already 0 — nothing to persist, but mark done so we
                    // do not re-check every settled period.
                    hc.pass_reset_done_this_lock = true;
                }
            }
        }
        // Reset the pure proof machine on either lock edge so a fresh session
        // re-earns the proof (rising: a new session begins; falling: the current
        // session ended). The per-lock pass-reset latch clears on the SAME edges
        // so each fresh lock can reset its counter once.
        if step.rising_edge || step.falling_edge {
            hc.proof.reset();
            hc.pass_reset_done_this_lock = false;
        }

        // 2. Proof tick + persist. A just-revoked lane still ticks (it is
        // back at the ceiling, so the proof stays Pending until the normal descent
        // lands it at the floor again — the re-prove path).
        let outcome = hc.proof.tick(ProofSignals {
            decay_at_floor,
            dll_l0_locked: dll_l0,
            unlock_count,
            probe_response_ratio,
        });
        if let ProofOutcome::Write {
            probe_response_ratio,
        } = outcome
        {
            let floor = resampler.decay_floor_frames();
            let now_epoch_s = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let rec = HostCompliance::new(now_epoch_s, probe_response_ratio, floor);
            match rec.store(&hc.path) {
                Ok(()) => {
                    hc.obs.on_written(now_epoch_s);
                    // Track the fresh clean proof (counter 0) as the believed
                    // on-disk record so a later probe-fail RETAIN write re-serialises
                    // THIS proof's evidence with a bumped counter.
                    hc.record = Some(rec);
                    info!(
                        "event=fanin.host_compliance.written path={} floor_frames={} \
                         probe_response_ratio={:.3} — future sessions prime at the floor",
                        hc.path.display(),
                        floor,
                        probe_response_ratio,
                    );
                }
                Err(e) => warn!(
                    "event=fanin.host_compliance.write_io_failed path={} detail={} — proof not \
                     persisted this session (audio unaffected; descent already ran)",
                    hc.path.display(),
                    e,
                ),
            }
        }

        self.host_compliance = Some(hc);
    }

    /// The common no-request period is one `pending` load per lane (plus, when
    /// auto-trim is enabled, one pure latch update) and nothing else.
    fn maybe_trim(&mut self) {
        for (idx, input) in self.inputs.iter_mut().enumerate() {
            // MANUAL: consume the pending flag with an Acquire swap. `Acquire`
            // pairs with the control thread's `Release` store so we observe the
            // request; the actual counters are Relaxed (staleness across the
            // STATUS read is fine, same as every other fan-in counter).
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

            // AUTO: advance the pure latch on this lane's cumulative frames_read.
            let frames_read = input.frames_read.load(Ordering::Relaxed);
            let decision = auto_trim_decision(
                frames_read,
                self.auto_trim_lane_state[idx],
                self.auto_trim_delay_frames,
            );
            self.auto_trim_lane_state[idx] = decision.next;

            // Re-arm the one-shot guard when the lane returns to idle so the next
            // activation can fire again. `active_since == None` after the update
            // means "idle right now".
            if decision.next.active_since.is_none() {
                input.trim.auto_fired.store(false, Ordering::Relaxed);
                continue;
            }

            // Fire exactly once per active session: the pure decision says the
            // delay elapsed AND we have not already fired this session.
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
/// slots, then mirror the same period to the lossy aloop side-tap. Returns the
/// number of frames that actually ENTERED the ring this period (published slots
/// × `RING_SLOT_FRAMES`) so the caller counts only real throughput — a
/// fully-dropped period returns 0, matching the Fifo arm's `Waited`.
///
/// **Pacing (the Ring A contract).** Each `RingWriter::publish` BLOCKS (bounded:
/// 32 ticks × the clamped `min(period/4, 2 ms)` sleep — with the pinned 128-frame
/// slot that tick is ~0.667 ms, so the cap is ~21 ms per full slot) while the
/// ring is full AND a live reader (CamillaDSP) is draining — that block is the
/// loop's pacer, transitively DAC-paced through Ring B. When the reader is
/// ABSENT/stale the writer free-run-drops instead of blocking, so the daemon must
/// self-pace: sleep one period per dropped publish so a readerless ring settles
/// to ~48 kHz instead of hot-spinning. The bounded publish wait plus at most one
/// period sleep keeps `step()` well under the 5 s watchdog threshold; the
/// writer's heartbeat is bumped inside each publish.
///
/// The mirror is a `write_music_only`-shaped non-blocking side-tap: it is
/// avail-checked and drop-on-full, so it can NEVER back-pressure the loop (that
/// would re-couple to the aloop timer and silently reintroduce the hop being
/// removed). It exists only to keep the AEC-fallback dsnoop + aloop diagnostics
/// live.
fn write_ring_period(ring: &mut RingOutput, output_buf: &[i16], period_frames: u32) -> u32 {
    let slots_per_step = period_frames / RING_SLOT_FRAMES;
    let samples_per_slot = (RING_SLOT_FRAMES as usize) * (CHANNELS as usize);
    let mut dropped_this_period = false;
    let mut published_slots: u32 = 0;
    for slot in 0..slots_per_step as usize {
        let start = slot * samples_per_slot;
        let slot_samples = &output_buf[start..start + samples_per_slot];
        match ring.writer.publish(slot_samples) {
            PublishOutcome::Published => {
                published_slots += 1;
            }
            PublishOutcome::DroppedNoReader | PublishOutcome::DroppedStuck => {
                dropped_this_period = true;
            }
        }
    }

    // Publish the writer's counter snapshot into the shared atomics for STATUS.
    let m = ring.writer.metrics();
    ring.counters
        .published
        .store(m.published_slots, Ordering::Relaxed);
    ring.counters
        .full_waits
        .store(m.full_waits, Ordering::Relaxed);
    // `drops` folds no-reader + stuck-reader drops — both mean a live reader did
    // not consume the slot.
    ring.counters.drops.store(
        m.drop_no_reader.saturating_add(m.stuck_reader_drops),
        Ordering::Relaxed,
    );
    ring.counters
        .occupancy
        .store(m.occupancy, Ordering::Relaxed);

    // Lossy aloop mirror (never the pacer). `write_music_only`-shaped:
    // avail-check + drop-on-full so it can never block the loop.
    if let Some(mirror) = ring.mirror.as_ref() {
        write_music_only(
            mirror,
            output_buf,
            &ring.counters.mirror_frames,
            &ring.counters.mirror_drops,
        );
    }

    // Reader-absent self-pacing: if ANY slot free-run-dropped this period, sleep
    // one period so a readerless ring settles to ~48 kHz instead of hot-spinning
    // (the blocking publish is the pacer only while a live reader drains). A
    // live-reader period never sleeps here — the publish block already paced it.
    if dropped_this_period {
        let ts = libc::timespec {
            tv_sec: 0,
            tv_nsec: ring.self_pace_period_ns as _,
        };
        // SAFETY: a valid timespec pointer; NULL remainder is fine (a signal-
        // interrupted sleep just shortens this one self-pace tick — the next
        // period re-evaluates).
        unsafe {
            libc::nanosleep(&ts, std::ptr::null_mut());
        }
    }

    // Frames that actually entered the ring this period — the caller counts only
    // these toward `frames_written` (a fully-dropped period returns 0).
    published_slots * RING_SLOT_FRAMES
}

fn store_output_delay(pcm: &PCM, delay_frames: &AtomicU64) {
    if let Ok(delay) = pcm.delay() {
        delay_frames.store(delay.max(0) as u64, Ordering::Relaxed);
    }
}

impl Input {
    /// The lane's resampler observability handles for STATUS, or `None` when no
    /// resampler is armed on this lane (the default — DEFAULT-OFF feature).
    pub fn resampler_observability(&self) -> Option<LaneResamplerObservability> {
        self.resampler.as_ref().map(|r| r.observability())
    }

    /// The lane's USB DIRECT observability handles for the STATUS `direct{}`
    /// block, or `None` when this is not the direct lane (C7).
    pub fn direct_observability(&self) -> Option<DirectObservability> {
        self.direct_obs.clone()
    }

    /// Whether this lane is the USB DIRECT lane (its `source` in STATUS is
    /// `"direct"`; every other lane is `"lane"`). C7.
    pub fn is_direct(&self) -> bool {
        self.direct.is_some()
    }

    /// The lane's shared TRIM control + counters, cloned for the state-server
    /// thread. The control endpoint sets `pending` here; the work loop trims
    /// this lane's resampler ring at its next period boundary and bumps the
    /// counters.
    pub fn trim_control(&self) -> Arc<TrimControl> {
        Arc::clone(&self.trim)
    }
}

/// Sum input samples into the running i32 accumulator with saturating
/// arithmetic. Pulled out for unit testability — no ALSA needed.
fn mix_into(sum: &mut [i32], input: &[i16]) {
    debug_assert_eq!(sum.len(), input.len());
    for (s, &i) in sum.iter_mut().zip(input) {
        *s = s.saturating_add(i as i32);
    }
}

/// Apply a period-stable gain to the accumulated program sum. Used
/// after pre-duck content metering so the assistant loudness baseline
/// tracks the listener-facing content, not the temporary ducked level.
fn apply_gain_to_sum(sum: &mut [i32], gain: f32) {
    for sample in sum {
        *sample = ((*sample as f32) * gain)
            .round()
            .clamp(i32::MIN as f32, i32::MAX as f32) as i32;
    }
}

/// Clamp i32 sum back to i16 for output. Pulled out for unit testability.
fn saturate_to_i16(sum: &[i32], out: &mut [i16]) {
    debug_assert_eq!(sum.len(), out.len());
    for (o, &s) in out.iter_mut().zip(sum) {
        *o = s.clamp(i16::MIN as i32, i16::MAX as i32) as i16;
    }
}

fn input_selected(selected_input: i32, input_index: usize, label: &str) -> bool {
    selected_input == -1 || selected_input == input_index as i32 || label == "correction"
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
/// in which the feature silently does nothing because `Mixer::new` constructs
/// no `LaneResampler`. Returns `None` (no warning) when the feature is off, or
/// when the label DOES match a live lane (the normal armed path). The returned
/// CSV is the human-facing "here are the labels you could have meant" hint.
///
/// Pulled out as a pure function (no ALSA) so the once-only warning decision is
/// unit-testable on a non-Linux host via the macOS-ALSA-scratch convention.
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
/// Pure over primitives (no ALSA, no `Config`) so it is unit-testable on a
/// non-Linux host via the macOS-ALSA-scratch convention.
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
/// construction failure (which we log and degrade past — the lane just runs the
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
    // DEFAULT-OFF post-lock cushion decay (latency lever 1). The knobs are
    // validated fail-loud in Config::from_env; the lane derives the ceiling
    // (target + cushion) and the render-period intervals itself.
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
            // Canonical arming line the operator greps for to confirm the
            // DEFAULT-OFF feature engaged on this lane. Keep the event name and
            // the lane/base target/held target/max-ppm fields stable —
            // jasper-trace / doc point at them. warmup_cushion + ring_frames
            // are extra diagnostic detail.
            let held_target = target + cushion;
            // Post-lock cushion decay breadcrumb (DEFAULT-OFF): when armed, the
            // held target above is the acquisition CEILING it decays FROM toward
            // the floor once locked + DLL-l0 + stable. `off` when disabled.
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

/// Convert a wall-time `ms` to a render-period count at the lane geometry, `>= 1`
/// so a small value still ticks. Mirrors `decay::DecayParams::ms_to_periods` — the
/// compliance settle/early-window clocks are render periods, same as the decay's.
/// Pure over primitives (unit-testable on any host).
fn ms_to_periods(ms: u64, period_frames: u32, sample_rate: u32) -> u64 {
    let period_frames = period_frames.max(1) as u64;
    let sample_rate = sample_rate.max(1) as u64;
    ((ms.saturating_mul(sample_rate)) / (1000 * period_frames)).max(1)
}

/// Seed the host-compliance persistence state for the resampler lane and, when a
/// VALID persisted proof is on disk, PRIME the resampler at the decay floor. The
/// proof is valid iff its schema matches AND its recorded floor equals this lane's
/// live decay floor (an operator floor retune between sessions invalidates the old
/// geometry — descend normally). A missing / corrupt / stale file leaves the lane
/// descending from the ceiling as today (fail toward safety). Only the write/read
/// I/O touches disk; all gate DECISIONS live in the pure `ComplianceProof`.
fn build_host_compliance_state(
    label: &str,
    config: &Config,
    resampler: &mut LaneResampler,
) -> HostComplianceState {
    use crate::host_compliance::HostCompliance;
    let path = PathBuf::from(&config.host_compliance_path);
    let live_floor = resampler.decay_floor_frames();
    // Load the persisted proof (None on missing/corrupt/schema-mismatch — safe).
    let loaded = HostCompliance::load(&path);
    // The believed-on-disk record + strike counter the mixer tracks (Some iff a
    // valid proof primed this session; the two-strike RETAIN write re-serialises
    // it). A present-but-stale-geometry file is NOT a live prime authority, so it
    // reads as `None` here just as it does for `flag_present`.
    let mut record: Option<HostCompliance> = None;
    let mut consecutive_failures: u32 = 0;
    // The prime-at-floor is only SAFE to arm when the host-clock DLL that
    // revalidates it is itself armed. The prime skips the ~2.5-min descent on the
    // strength of a PRIOR session's host-clock compliance proof, and its entire
    // safety story ("the per-session servo probe revalidates the prime, revoking a
    // regressed host") is enacted by that DLL: `dll_l0_locked`, the probe result,
    // the two-strike ProbeFail (#1160), and the `PrimeHold` exit all ride the
    // `fanin-host-clock` servo thread. That thread runs ONLY when
    // `host_clock_enabled && usb_direct_enabled` (main.rs `host_clock_enabled_effective`
    // + a live direct-lane resampler). Without it, `ladder_l0` is pinned false
    // forever, so a primed session would sit in `PrimeHold` at the floor with NO
    // ladder to reach l0, NO probe to revalidate, and NO demotion — the held target
    // held FOREVER on stale evidence (only the underfill/churn net could catch an
    // unsustainable floor). #1145 proved this exact misconfig inert (armed-but-frozen
    // decay == disabled: `NotL0` snaps the held target back to the ceiling every
    // tick); priming without the DLL would silently convert that into a permanent
    // unvalidated divergence. So gate the prime on the DLL being armed. When it is
    // NOT, we leave the proof untouched on disk and descend from the ceiling — decay
    // is inert (`NotL0` pins the ceiling with no l0), i.e. exactly the #1145 behaviour
    // — and the prime resumes automatically the next boot the DLL is re-armed.
    // `host_clock_servo_armed()` is the SAME predicate `main` derives the
    // servo-spawn gate from, so the prime and the servo can never disagree.
    let host_clock_armed = config.host_clock_servo_armed();
    let (floor_primed, proved_at_epoch_s) = match &loaded {
        Some(rec) if rec.valid_for(live_floor) && host_clock_armed => {
            resampler.prime_decay_at_floor();
            info!(
                "event=fanin.host_compliance.prime_at_floor lane={} floor_frames={} \
                 proved_at={} probe_response_ratio={:.3} consecutive_failures={} — skipping \
                 the cushion descent (per-session probe revalidates)",
                label,
                live_floor,
                rec.proved_at_epoch_s,
                rec.probe_response_ratio,
                rec.consecutive_failures,
            );
            record = Some(rec.clone());
            consecutive_failures = rec.consecutive_failures;
            (true, rec.proved_at_epoch_s)
        }
        Some(rec) if rec.valid_for(live_floor) => {
            // Valid proof, but the host-clock DLL that revalidates the prime is NOT
            // armed (host-clock or USB-direct off). Do NOT prime — an un-revalidated
            // floor prime would hold forever on stale evidence (see `host_clock_armed`
            // above). Leave the proof untouched on disk and descend from the ceiling;
            // with no `dll_l0_locked` the decay's `NotL0` branch pins the ceiling, so
            // the lane is inert (the #1145 armed-but-frozen == disabled invariant).
            // STATUS reads absent (`flag_present=false`, `proved_at=0`), matching the
            // stale-geometry arm. The prime resumes the next boot the DLL is re-armed.
            info!(
                "event=fanin.host_compliance.prime_suppressed lane={} floor_frames={} \
                 proved_at={} reason=host_clock_disarmed host_clock_enabled={} \
                 usb_direct_enabled={} — descending from ceiling (no DLL to revalidate \
                 the prime; proof preserved)",
                label,
                live_floor,
                rec.proved_at_epoch_s,
                config.host_clock_enabled,
                config.usb_direct_enabled,
            );
            (false, 0)
        }
        Some(rec) => {
            // Present but stale geometry: descend normally, do NOT prime. STATUS
            // reads this as absent — `flag_present=false` AND `proved_at=0` — so it
            // matches the "proved_at is 0 when the flag is absent" contract in
            // `state.rs` and does not read to an operator like a REVOKED proof (a
            // present-but-stale file is not a live prime authority). The recorded
            // timestamp is still logged above for the retune diagnostic.
            info!(
                "event=fanin.host_compliance.stale lane={} recorded_floor={} live_floor={} — \
                 descending from ceiling (floor retuned since the proof)",
                label, rec.floor_frames, live_floor,
            );
            (false, 0)
        }
        None => {
            info!(
                "event=fanin.host_compliance.no_flag lane={} — descending from ceiling \
                 (no persisted proof; will prove + persist this session)",
                label,
            );
            (false, 0)
        }
    };
    let settle_periods = ms_to_periods(
        HOST_COMPLIANCE_SETTLE_MS,
        config.period_frames,
        config.sample_rate,
    );
    let early_window_periods = ms_to_periods(
        HOST_COMPLIANCE_EARLY_REVALIDATION_SECS.saturating_mul(1000),
        config.period_frames,
        config.sample_rate,
    );
    let churn_confirm_periods = ms_to_periods(
        HOST_COMPLIANCE_CHURN_CONFIRM_SECS.saturating_mul(1000),
        config.period_frames,
        config.sample_rate,
    );
    // STATUS observability: `flag_present` reflects whether a VALID proof primed
    // this session (a present-but-stale file is not an authority, so it reads as
    // absent for STATUS purposes). `consecutive_failures` seeds from the loaded
    // proof (0 unless a prior spurious probe fail retained a strike). Inject a
    // clone into the resampler so `resampler.compliance` renders.
    let obs = crate::host_compliance::HostComplianceObservability::new(
        floor_primed,
        proved_at_epoch_s,
        consecutive_failures,
    );
    resampler.set_compliance_observability(obs.clone_handles());
    HostComplianceState {
        path,
        proof: crate::host_compliance::ComplianceProof::new(settle_periods),
        revalidation: crate::host_compliance::RevalidationTracker::new(
            floor_primed,
            early_window_periods,
            churn_confirm_periods,
        ),
        obs,
        record,
        pass_reset_done_this_lock: false,
    }
}

fn open_input(
    pcm_name: &str,
    label: &str,
    config: &Config,
    resampler: Option<LaneResampler>,
) -> Result<Input> {
    // Non-blocking so a silent renderer's substream doesn't stall
    // the work loop. read_input handles -EAGAIN as "no data; treat
    // as silence."
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
        label: label.to_string(),
        pcm_name: pcm_name.to_string(),
        read_buf: vec![0i16; period_samples],
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
        catchup_resync_frames: Arc::new(AtomicU64::new(0)),
        catchup_events: Arc::new(AtomicU64::new(0)),
        resampler,
        trim: TrimControl::new(),
        direct_obs: None,
    })
}

/// Build the USB DIRECT lane. Opens `hw:UAC2Gadget` (or the override) with the
/// bridge's proven envelope (C1); on failure the lane starts `Absent` and will
/// render silence with a bounded reopen retry (C3) — a gadget-absent box never
/// fails the daemon. The aloop substream is NOT opened (`pcm: None`): this
/// lane's audio comes only from the gadget capture. Never returns `Err` — the
/// fail-hard "every input required" contract is exempted for this lane alone.
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
            // Gadget absent at startup (unplugged host, or the usbsink bridge
            // is holding hw:UAC2Gadget in a misconfig). Start Absent; the lane
            // renders silence and retries on its own cadence (C3). Not fatal.
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
    Input {
        // The direct lane does NOT open its aloop substream — its only source
        // is the gadget capture in `direct` (C6).
        pcm: None,
        direct: Some(direct),
        label: label.to_string(),
        pcm_name: pcm_name.to_string(),
        read_buf: vec![0i16; period_samples],
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
        catchup_resync_frames: Arc::new(AtomicU64::new(0)),
        catchup_events: Arc::new(AtomicU64::new(0)),
        resampler,
        trim: TrimControl::new(),
        direct_obs: Some(DirectObservability {
            device,
            period_frames: open_period,
            buffer_frames,
            present,
            opens,
            retries,
            reopens: Arc::new(AtomicU64::new(0)),
            zero_avail_streak: Arc::new(AtomicU64::new(0)),
            drain_stats: DrainStats::new(),
        }),
    }
}

/// Open the USB DIRECT capture PCM with the usbsink BRIDGE's proven envelope
/// (C1) — deliberately NOT fanin's aloop-tuned `configure_pcm` (which sets an
/// exact buffer). S32_LE 2ch 48k, `set_period_size(open_period, Nearest)`,
/// `set_buffer_size_near(resolve_direct_buffer_frames(open_period))`; then the
/// bridge-parity post-negotiation bails. `open_period` is 256 by default
/// (byte-identical to today) or the `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES`
/// override (lever 2 H1 knob). Non-blocking (the direct lane rides the resampler
/// read slot like the aloop lane) and `start()`ed so reads return data / EAGAIN.
/// Returns `(open PCM, negotiated buffer frames)` — the second element is the
/// live `hwp.get_buffer_size()`, which the caller stores into
/// `DirectObservability.buffer_frames` and logs so STATUS reports the buffer the
/// PCM is really running rather than the requested size. On failure returns an
/// `alsa::Error` the caller maps to the `Absent` state.
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
        // Bridge-parity validation. Rate/period MUST land exactly (the bridge
        // bails otherwise); buffer is warn-on-near-mismatch but must clear the
        // deep-buffer + alignment structural floor. A validation failure closes
        // the PCM (drop) and returns an error → the lane goes Absent.
        if let Err(reason) = direct_open_params_ok(rate, period, buffer, open_period) {
            warn!(
                "event=fanin.usb_direct.open_rejected device={} rate={} period={} buffer={} reason={}",
                device, rate, period, buffer, reason,
            );
            // Manufacture an errno-bearing alsa::Error so the caller's Absent
            // path logs a consistent shape. EINVAL = "negotiated an unusable
            // geometry".
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

/// Pure post-negotiation validation of the direct capture geometry (C1),
/// unit-testable without ALSA. Rate must be exactly 48000 and period exactly
/// `want_period` (the requested open period — the bridge bails on any drift);
/// buffer must clear the deep-buffer floor (≥ `DIRECT_BUFFER_MIN_PERIODS`
/// periods AND ≥ `DIRECT_BUFFER_MIN_FRAMES`) and be a whole multiple of the
/// period (a fractional buffer would shear). Returns the rejection reason string
/// on failure.
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

/// Classify a direct-capture read/query errno (C1). `EAGAIN` stops the drain;
/// `EPIPE`/`ESTRPIPE` is an xrun to recover; ANYTHING else (notably `ENODEV`
/// on unplug) is a device loss → the lane transitions to `Absent`, never a
/// daemon error. Pure so the classification is scratch-crate testable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DirectReadFate {
    /// `EAGAIN` — no data ready right now; stop draining this period.
    WouldBlock,
    /// `EPIPE`/`ESTRPIPE` — an overrun; recover the PCM and reset the resampler.
    Xrun,
    /// Any other errno (ENODEV on unplug, etc.) — the device is gone; go Absent.
    DeviceLost,
}

fn classify_direct_errno(errno: i32) -> DirectReadFate {
    if errno == libc::EAGAIN {
        DirectReadFate::WouldBlock
    } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
        DirectReadFate::Xrun
    } else {
        DirectReadFate::DeviceLost
    }
}

/// The nominal sample rate the direct lane opens at. Kept as a named const so
/// the open envelope and the pure validator agree (C1). Distinct from
/// `config.sample_rate` on purpose: the gadget capture is a FIXED 48 kHz
/// endpoint (the bridge's contract), not a configurable fan-in knob.
const SAMPLE_RATE_HZ: u32 = 48_000;

/// Pull the errno out of an `alsa::Error` for logging. Small helper so the
/// Absent-path log lines stay terse. (`alsa::Error::errno()` already returns the
/// `i32` errno, matching the `libc::E*` constants the read paths compare against.)
fn errno_of(e: &alsa::Error) -> i32 {
    e.errno()
}

fn open_output(pcm_name: &str, config: &Config) -> Result<PCM> {
    // Blocking. The blocking writei() is what paces the work loop —
    // it returns when the kernel has consumed enough of the output
    // ring to make room for the next period.
    let pcm = PCM::new(pcm_name, Direction::Playback, false)
        .with_context(|| format!("opening playback PCM {}", pcm_name))?;
    configure_pcm(&pcm, config, config.output_buffer_frames)
        .with_context(|| format!("configuring playback PCM {}", pcm_name))?;
    Ok(pcm)
}

/// Open the OPTIONAL music-only side-output. **Non-blocking** — unlike
/// the primary `open_output`, this PCM must NEVER pace the work loop
/// (`write_music_only` drops on a full ring instead of blocking), so the
/// primary output stays the sole timing owner (inv-1). Same format / rate
/// / period / buffer as the primary output.
fn open_music_output(pcm_name: &str, config: &Config) -> Result<PCM> {
    let pcm = PCM::new(pcm_name, Direction::Playback, true)
        .with_context(|| format!("opening music-only output PCM {}", pcm_name))?;
    configure_pcm(&pcm, config, config.output_buffer_frames)
        .with_context(|| format!("configuring music-only output PCM {}", pcm_name))?;
    Ok(pcm)
}

fn configure_pcm(pcm: &PCM, config: &Config, buffer_frames: u32) -> Result<()> {
    // HwParams must be dropped before pcm.hw_params() is called.
    // The alsa-rs API: build the params, install them, drop the
    // handle in this nested scope.
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(CHANNELS)
            .with_context(|| format!("set_channels({})", CHANNELS))?;
        hwp.set_rate(config.sample_rate, ValueOr::Nearest)
            .with_context(|| format!("set_rate({})", config.sample_rate))?;
        hwp.set_format(FORMAT)
            .with_context(|| format!("set_format({:?})", FORMAT))?;
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

/// Bounded per-input catch-up resync. Called once per lane per period,
/// BEFORE the normal `read_input`.
///
/// ## Why this exists
///
/// Every lane is read exactly one period per work-loop iteration, and the
/// loop is paced by the blocking OUTPUT write (the local DAC clock). A lane
/// whose producer is clocked off the *same* DAC (every networked renderer:
/// AirPlay / Spotify / Bluetooth / TTS) keeps its capture ring at ~one
/// period forever — it can't outrun a consumer on its own clock. The USB
/// lane is different: its producer is the host (Mac) clock, and the gadget's
/// async feedback currently tracks the snd-aloop jiffies timer, not the DAC,
/// so a small residual rate gap accumulates. With a strict one-period read
/// and no catch-up, that excess never drains — the ring fills monotonically
/// until it overruns, by which point the *upstream* usbsink producer queue
/// has already overflowed (dropped_full) because back-pressure never reached
/// the host.
///
/// This drains the excess down to one period when a lane's readable backlog
/// crosses the high-water, so the ring stays bounded and back-pressure can
/// propagate. It is GENERIC per-input: it only ever fires for a lane that
/// actually backs up. A DAC-locked lane sits at one period and this is a
/// single non-blocking `avail_update` — no reads, no effect.
///
/// ## Honesty
///
/// Drop-CONTROLLED, not drop-FREE: a backed-up lane loses a few ms of audio
/// at each resync (an occasional discard at the residual drift rate), traded
/// against a cascading upstream overflow. True drop-free for the mixed path
/// is the later per-lane adaptive resampler; this does NOT resample.
///
/// ## RT-safety
///
/// No allocation (discards into the lane's existing `read_buf` scratch) and
/// no blocking (`avail_update` is a non-blocking query; the discard `readi`
/// only ever reads frames `avail_update` already reported ready). The number
/// of discard reads is capped per call (`CATCHUP_MAX_DRAIN_PERIODS`). The
/// log is count-gated, so the common no-resync path touches no clock and
/// emits nothing.
fn drain_input_excess(input: &mut Input, period_frames: usize) {
    // Non-direct lanes always have Some(pcm); the direct lane never reaches
    // this path (it routes to read_direct_and_render). Guard defensively.
    let Some(pcm) = input.pcm.as_ref() else {
        return;
    };
    // Non-blocking query of how many frames are readable right now.
    // EAGAIN/error here just means "no usable reading right now" — leave
    // the normal read_input path to handle recovery; never block or panic.
    let avail = match pcm.avail_update() {
        Ok(a) => a,
        Err(_) => return,
    };
    let to_drain = catchup_drain_periods(avail, period_frames as i64);
    if to_drain == 0 {
        return; // healthy lane — the overwhelmingly common path.
    }

    let io = match pcm.io_i16() {
        Ok(io) => io,
        Err(_) => return,
    };
    // Discard whole periods into the existing read_buf scratch (reused; no
    // allocation). read_input overwrites read_buf next, so trashing it here
    // is safe. On non-blocking capture, readi returns Err(EAGAIN) the instant
    // the ring drops below one period (it drained faster than avail claimed) —
    // that Err arm is the normal early-stop. Ok(0) is a defensive guard for a
    // 0-frame return that shouldn't occur here. The 0..to_drain bound (≤ MAX)
    // means it can never spin regardless.
    let mut discarded_frames: u64 = 0;
    for _ in 0..to_drain {
        match io.readi(&mut input.read_buf) {
            Ok(0) => break,
            Ok(n) => discarded_frames += n as u64,
            Err(_) => break,
        }
    }
    if discarded_frames == 0 {
        return;
    }

    input
        .catchup_resync_frames
        .fetch_add(discarded_frames, Ordering::Relaxed);
    let events = input.catchup_events.fetch_add(1, Ordering::Relaxed) + 1;
    // Rate-limited: 1st event for this lane, then every Nth. Count-based so
    // the hot loop reads no clock. Logged outside any tight inner loop.
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
/// ## Why the reservoir is the resampler ring (v2)
///
/// The full-ring-graph standing head-start does NOT live in the ALSA readable
/// backlog on the armed lane: `read_into_resampler_and_render` already drains
/// every frame ALSA reports ready each period, so the kernel ring is held
/// shallow by design. The reservoir is the resampler's CURSOR-RELATIVE fill
/// (`write_frame - next_input_frame`) — observed on-device at ~1919 frames
/// against a 512-held target with lock churn. This trim drops THAT in place via
/// [`LaneResampler::trim_ring`]: the cursor skips forward over the oldest
/// buffered frames (one discontinuity at the skip) while lock and the DLL loop
/// state survive — the exact opposite of the v1 `reset()` path, which was the
/// unlock/reprime churn we are eliminating.
///
/// An UNARMED lane (no resampler) has no such userspace reservoir — its
/// standing fill would be the ALSA backlog, which the catch-up drain already
/// bounds and which is not the full-ring-graph carrier this trim targets — so
/// TRIM on an unarmed lane is a documented 0-frame no-op. It still clears its
/// `pending` flag so the control command completes cleanly.
///
/// RT-safety: pure host-memory work inside the resampler (one fill compute, one
/// cursor advance, one `drop_before`), no syscalls, no allocation, no blocking.
/// Runs on the WORK thread (which owns the mixer's `LaneResampler`), triggered
/// by a control-endpoint `pending` flag or the AUTO-TRIM latch — never on the
/// state-server thread.
fn trim_input(input: &mut Input) -> u64 {
    let dropped = match input.resampler.as_mut() {
        Some(r) => r.trim_ring(),
        // No resampler on this lane: the standing-fill reservoir this PoC
        // targets does not exist here. A documented no-op (see fn docs).
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
    // One log line per trim (trims are operator/auto events, not per-period —
    // no spam gate needed).
    info!(
        "event=fanin.trim label={} dropped_ring_frames={} trims={} total_trimmed_frames={}",
        input.label, dropped, trims, total,
    );
    dropped
}

/// The mixer-thread side of the relocated impulse tap (C4). Holds the shared
/// [`TapState`] (armed + detector knobs, read lock-free), the last-armed
/// [`TapConfig`] (read only on an arm-generation change), the bounded channel to
/// the `fanin-tap-writer` thread, and the mixer-local detector state + per-lane
/// cumulative capture cursor. Constructed once in `Mixer::new`; runs inline in
/// `read_direct_and_render` over the converted S16 slice BEFORE `push_input`.
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
    /// detector's `read_start_frame`, so refractory anchoring is stable across
    /// reads of any size — the bridge's `capture_frames_cursor` idiom).
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

    /// Run the tap over one converted S16 read, BEFORE it enters the resampler
    /// (the route's own ingress). Mirrors usbsink's `tap_over_read`: reloads the
    /// detector only on a fresh arm generation, timestamps with the caller's
    /// post-read `read_ns`, and non-blocking `try_send`s the event
    /// (drop-and-count on Full). Only called from the armed branch; the
    /// disarmed fast path is the caller's `state.armed()` check.
    ///
    /// - `converted`: the S16 slice just narrowed from S32 (this read only).
    /// - `read_frames`: frames in this read.
    /// - `read_ns`: `CLOCK_MONOTONIC` ns taken immediately after `readi`.
    /// - `ring_fill_periods` / `period_frames`: the lane resampler fill BEFORE
    ///   `push_input`, recorded (as frames) for the JSONL diagnostic field.
    #[allow(clippy::too_many_arguments)]
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

/// `CLOCK_MONOTONIC` in nanoseconds — the direct tap's ingress timeline (C4).
/// The tap and the Python mic harness both read `CLOCK_MONOTONIC` on the same
/// Pi; that shared timeline is the only reason their cross-process subtraction
/// is valid. On the (never-observed) syscall failure returns 0 rather than
/// crashing the work loop; a stray 0-anchored event is dropped by the harness's
/// pairing window.
fn monotonic_ns() -> i128 {
    let mut ts = MaybeUninit::<libc::timespec>::uninit();
    let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, ts.as_mut_ptr()) };
    if rc != 0 {
        return 0;
    }
    let ts = unsafe { ts.assume_init() };
    (ts.tv_sec as i128) * 1_000_000_000 + (ts.tv_nsec as i128)
}

/// Read the USB DIRECT lane (C1/C3/C4): drain everything the gadget capture
/// reports ready into the lane resampler (narrowing S32→S16, tapping the
/// converted slice on the way), then render exactly one DAC-paced period into
/// `read_buf`. Returns the number of real (non-silence) frames rendered —
/// `period_frames` when the resampler is locked, `0` while priming/absent.
///
/// Never returns `Err`: a device loss (ENODEV on unplug, or a rejected reopen)
/// transitions the lane to `Absent` and renders silence with a bounded reopen
/// retry (C3), so the daemon keeps running. Xruns recover exactly like the
/// aloop resampler lane (`recover_resampler_input_xrun`, but device-open aware).
fn read_direct_and_render(
    input: &mut Input,
    period_frames: usize,
    tap: &mut DirectTapHook,
    xrun_tx: &Sender<XrunEvent>,
) -> usize {
    // The lane's resampler fill BEFORE this period's push — the diagnostic
    // `ring_fill_frames` the tap records (not added to harness latency). Read via
    // the single-atomic gauge, NOT observability() (which clones Arcs — never on
    // the hot path).
    let ring_fill_before = input
        .resampler
        .as_ref()
        .map(|r| r.fill_frames_gauge())
        .unwrap_or(0);

    // Take ownership of the state machine so we can mutate `input` (resampler,
    // counters) inside the read without a double borrow. Restored at the end.
    let mut direct = input
        .direct
        .take()
        .expect("read_direct_and_render only called on a direct lane");

    match &direct {
        DirectCapture::Present(_) => {
            let outcome = drain_direct_capture(
                &direct,
                input,
                period_frames,
                tap,
                ring_fill_before,
                xrun_tx,
            );
            match outcome {
                DirectDrainOutcome::Ok => {}
                DirectDrainOutcome::DeviceLost => {
                    // Runtime loss (errno-driven): close the PCM, reset the
                    // resampler, go Absent — the reopen retry re-establishes it.
                    if let Some(r) = input.resampler.as_mut() {
                        r.reset();
                    }
                    if let Some(obs) = &input.direct_obs {
                        obs.present.store(false, Ordering::Relaxed);
                        obs.zero_avail_streak.store(0, Ordering::Relaxed);
                        warn!(
                            "event=fanin.usb_direct.absent device={} reason=runtime_loss (will retry ~every {} periods)",
                            obs.device, DIRECT_REOPEN_RETRY_PERIODS,
                        );
                    }
                    direct = DirectCapture::Absent {
                        periods_until_retry: DIRECT_REOPEN_RETRY_PERIODS,
                    };
                }
                DirectDrainOutcome::ZombieReopen => {
                    // Zombie handle (C): Present but deaf for ~2 s (gadget rebuilt
                    // underneath us — no errno). Force the SAME close→Absent→reopen
                    // recovery as a device loss, but log + count it distinctly so a
                    // silent gadget rebuild is visible. periods_until_retry=0 so the
                    // very next render period attempts the reopen (a zombie is a
                    // live rebuild, not a truly-absent host — no need to wait 2 s
                    // more on top of the 2 s we already spent detecting it).
                    if let Some(r) = input.resampler.as_mut() {
                        r.reset();
                    }
                    if let Some(obs) = &input.direct_obs {
                        obs.present.store(false, Ordering::Relaxed);
                        obs.zero_avail_streak.store(0, Ordering::Relaxed);
                        let reopens = obs.reopens.fetch_add(1, Ordering::Relaxed) + 1;
                        warn!(
                            "event=fanin.usb_direct.reopen device={} reason=zombie_handle reopens={} (avail=0 for ~{} periods; gadget rebuilt underneath — closing + re-opening the capture)",
                            obs.device, reopens, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
                        );
                    }
                    direct = DirectCapture::Absent {
                        periods_until_retry: 0,
                    };
                }
            }
        }
        DirectCapture::Absent { .. } => {
            // Try to reopen at most once per retry window (period-counted).
            direct = maybe_reopen_direct(direct, input);
        }
    }

    // Render one DAC-paced period from whatever the resampler holds (silence
    // while Absent / priming). Advance the tap's capture cursor only by frames
    // actually read this period (done inside drain_direct_capture).
    let real_frames = match input.resampler.as_mut() {
        Some(r) => r.render_period(&mut input.read_buf),
        None => {
            input.read_buf.fill(0);
            0
        }
    };
    input.direct = Some(direct);
    real_frames
}

/// The outcome of one direct-capture drain: normal (kept Present), a device loss
/// (an errno the drain classified as DeviceLost), or a ZOMBIE handle (Present but
/// `avail_update` has returned exactly 0 for `DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS`
/// consecutive drains — a gadget rebuilt underneath us). Both non-Ok outcomes drive
/// the SAME close→Absent→bounded-reopen recovery; they differ only in the log line
/// + which counter increments, so an operator can tell a clean unplug (DeviceLost,
/// errno-driven) from a silent gadget rebuild (ZombieReopen, avail-0-driven) apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DirectDrainOutcome {
    Ok,
    DeviceLost,
    ZombieReopen,
}

/// Drain all currently-available frames from the gadget capture into the lane
/// resampler, narrowing S32→S16 and tapping each read (C1/C4). Bounded by
/// `RESAMPLER_MAX_READ_PERIODS`. EAGAIN stops the drain; EPIPE/ESTRPIPE recovers
/// the PCM + resets the resampler; any other errno is a device loss.
fn drain_direct_capture(
    direct: &DirectCapture,
    input: &mut Input,
    period_frames: usize,
    tap: &mut DirectTapHook,
    ring_fill_before: u64,
    xrun_tx: &Sender<XrunEvent>,
) -> DirectDrainOutcome {
    let DirectCapture::Present(pcm) = direct else {
        return DirectDrainOutcome::Ok;
    };
    let channels = CHANNELS as usize;
    // Preallocated i32 scratch (256×2) — no allocation in the hot path. Same
    // length as `narrow_scratch` below: the i32 read fills `scratch[..samples]`
    // and the narrow fills `narrow_scratch[..got]` with `got == samples`.
    let mut scratch = [0i32; direct_narrow_scratch_samples()];
    // Dedicated i16 narrowing scratch, sized to match the i32 scratch. MUST NOT
    // reuse `input.read_buf` (sized `period_frames × CHANNELS`) — see
    // `direct_narrow_scratch_samples` for the OOB-on-small-period hazard. A
    // single chunk read is capped at DIRECT_PERIOD_FRAMES frames (`to_read`
    // below), so this fixed size always bounds `got`.
    let mut narrow_scratch = [0i16; direct_narrow_scratch_samples()];
    let mut read_budget_remaining =
        period_frames.saturating_mul(RESAMPLER_MAX_READ_PERIODS as usize);
    let armed = tap.state.armed();
    // Sample the drain-ENTRY avail exactly once per drain call (lever 2). The
    // first `avail_update()` reading is the standing gadget-capture dwell — the
    // frames sitting readable when the mixer render cycle reaches this lane,
    // which is the ~186-frame (3.9 ms) latency the symptom measures. Later
    // in-loop `avail_update`s reflect drain progress, not the standing dwell, so
    // they are NOT recorded (recording every iteration would multi-count).
    let mut drain_entry_recorded = false;

    while read_budget_remaining > 0 {
        let avail = match pcm.avail_update() {
            Ok(a) => a,
            Err(e) => match classify_direct_errno(e.errno()) {
                DirectReadFate::WouldBlock => break,
                DirectReadFate::Xrun => {
                    recover_direct_xrun(pcm, input, e, period_frames, xrun_tx, "avail_update");
                    break;
                }
                DirectReadFate::DeviceLost => return DirectDrainOutcome::DeviceLost,
            },
        };
        if !drain_entry_recorded {
            drain_entry_recorded = true;
            record_drain_entry(input, avail);
            // Zombie-handle detection (C): a Present handle whose avail_update has
            // returned exactly 0 for DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS consecutive
            // drains is attached to a destroyed gadget instance (UDC rebind /
            // usbsink stop-start) — deaf forever with no errno. Track the streak on
            // the drain-ENTRY sample only (once per drain call, like the dwell
            // stats). A single avail > 0 resets it; crossing the threshold returns
            // ZombieReopen so read_direct_and_render force-reopens the capture.
            if let Some(obs) = &input.direct_obs {
                if avail == 0 {
                    let streak = obs.zero_avail_streak.fetch_add(1, Ordering::Relaxed) + 1;
                    if zombie_handle_suspected(streak, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS) {
                        return DirectDrainOutcome::ZombieReopen;
                    }
                } else {
                    obs.zero_avail_streak.store(0, Ordering::Relaxed);
                }
            }
        }
        let want = resampler_read_budget_frames(avail, period_frames).min(read_budget_remaining);
        if want == 0 {
            break;
        }
        // Read in ≤256-frame chunks (the scratch size) via io_i32().readi.
        let mut remaining = want;
        let mut stop = false;
        while remaining > 0 && !stop {
            let to_read = remaining.min(DIRECT_PERIOD_FRAMES as usize);
            let samples = to_read * channels;
            let read_result = {
                let io = match pcm.io_i32() {
                    Ok(io) => io,
                    Err(_) => return DirectDrainOutcome::DeviceLost,
                };
                io.readi(&mut scratch[..samples])
            };
            match read_result {
                Ok(0) => {
                    stop = true;
                }
                Ok(n) => {
                    let got = n * channels;
                    // Narrow S32→S16 into the dedicated narrowing scratch (NOT
                    // input.read_buf — see the declaration comment for the OOB
                    // hazard on small period geometries). `got` ≤ scratch len
                    // because `to_read` ≤ DIRECT_PERIOD_FRAMES.
                    let converted = &mut narrow_scratch[..got];
                    let _ = jasper_resampler::convert_s32_to_s16(&scratch[..got], converted);
                    // Tap the converted slice BEFORE push_input (armed only). The
                    // read_ns is taken immediately after readi returned above.
                    if armed {
                        let read_ns = monotonic_ns();
                        tap.tap_over_read(&narrow_scratch[..got], n, read_ns, ring_fill_before);
                    }
                    tap.capture_frames_cursor = tap.capture_frames_cursor.saturating_add(n as u64);
                    input.frames_read.fetch_add(n as u64, Ordering::Relaxed);
                    if let Some(r) = input.resampler.as_mut() {
                        r.push_input(&narrow_scratch[..got]);
                    }
                    remaining = remaining.saturating_sub(n);
                    read_budget_remaining = read_budget_remaining.saturating_sub(n);
                    if n < to_read {
                        stop = true;
                    }
                }
                Err(e) => match classify_direct_errno(e.errno()) {
                    DirectReadFate::WouldBlock => stop = true,
                    DirectReadFate::Xrun => {
                        recover_direct_xrun(pcm, input, e, period_frames, xrun_tx, "readi");
                        stop = true;
                    }
                    DirectReadFate::DeviceLost => return DirectDrainOutcome::DeviceLost,
                },
            }
        }
        if stop {
            break;
        }
    }
    // Reset the tap detector across a disarm transition (mirrors the aloop tap's
    // arm-boundary reset) so a fresh arm starts clean.
    if !armed && tap.detector.is_some() {
        tap.detector = None;
    }
    DirectDrainOutcome::Ok
}

/// Record one drain-ENTRY avail sample into the lane's since-boot drain stats
/// (lever 2) and, every [`DRAIN_STATS_LOG_EVERY`] drains, emit a rate-limited
/// summary INFO line. Lock-free, allocation-free, syscall-free apart from the
/// throttled log — safe on the hot path. A `None` `direct_obs` (never true on a
/// direct lane) is a silent no-op.
fn record_drain_entry(input: &Input, avail: i64) {
    let Some(obs) = &input.direct_obs else {
        return;
    };
    let stats = &obs.drain_stats;
    let count = stats.record(avail);
    // The counter itself is the rate limiter: log only on the exact multiple so
    // there is no separate "last logged" state and the cadence is O(1).
    if count % DRAIN_STATS_LOG_EVERY == 0 {
        let sum = stats.sum.load(Ordering::Relaxed);
        let max = stats.max.load(Ordering::Relaxed);
        let mean = (sum as f64) / (count as f64);
        info!(
            "event=fanin.direct.drain_stats device={} drains={} mean_avail={:.1} max_avail={} \
             hist=[{},{},{},{},{},{}] (frames; buckets [0,64,128,192,256,320,+))",
            obs.device,
            count,
            mean,
            max,
            stats.hist[0].load(Ordering::Relaxed),
            stats.hist[1].load(Ordering::Relaxed),
            stats.hist[2].load(Ordering::Relaxed),
            stats.hist[3].load(Ordering::Relaxed),
            stats.hist[4].load(Ordering::Relaxed),
            stats.hist[5].load(Ordering::Relaxed),
        );
    }
}

/// Recover a direct-capture xrun (EPIPE/ESTRPIPE): count it, forward the xrun
/// event, `try_recover` the PCM, restart it if not Running, and reset the
/// resampler (a discontinuity). Mirrors `recover_resampler_input_xrun` for the
/// direct lane. Best-effort — a failed recover just leaves the PCM for the next
/// period's `avail_update` to re-observe (which will classify a hard failure as
/// a device loss).
fn recover_direct_xrun(
    pcm: &PCM,
    input: &mut Input,
    error: alsa::Error,
    period_frames: usize,
    xrun_tx: &Sender<XrunEvent>,
    operation: &str,
) {
    let count = input.xrun_count.fetch_add(1, Ordering::Relaxed) + 1;
    warn!(
        "event=fanin.xrun source=input label={} count={} op={} (usb_direct lane)",
        input.label, count, operation,
    );
    let _ = xrun_tx.send(XrunEvent {
        source: XrunSource::Input,
        label: input.label.clone(),
        frames: period_frames as u32,
        count,
    });
    if pcm.try_recover(error, true).is_ok() && pcm.state() != State::Running {
        let _ = pcm.start();
    }
    if let Some(r) = input.resampler.as_mut() {
        r.reset();
    }
}

/// While `Absent`, count down the period-based retry latch and attempt a reopen
/// when it reaches 0 (C3). No wall clock — the countdown is one decrement per
/// render period. A successful reopen transitions to `Present` and re-primes the
/// resampler from fresh input; a failed reopen re-arms the latch (one retry per
/// ~2 s) and stays Absent. Exactly one `present`/`absent` transition log line.
fn maybe_reopen_direct(direct: DirectCapture, input: &mut Input) -> DirectCapture {
    let DirectCapture::Absent {
        periods_until_retry,
    } = direct
    else {
        return direct;
    };
    if periods_until_retry > 0 {
        return DirectCapture::Absent {
            periods_until_retry: periods_until_retry - 1,
        };
    }
    // Retry window elapsed: attempt one reopen. The open period is the one this
    // lane negotiated at construction (stashed in direct_obs) so a reopen uses
    // the same geometry as the initial open, not a hardcoded default.
    let device = input
        .direct_obs
        .as_ref()
        .map(|o| o.device.clone())
        .unwrap_or_default();
    let open_period = input
        .direct_obs
        .as_ref()
        .map(|o| o.period_frames)
        .unwrap_or(DIRECT_PERIOD_FRAMES);
    if let Some(obs) = &input.direct_obs {
        obs.retries.fetch_add(1, Ordering::Relaxed);
    }
    match open_direct_capture(&device, open_period) {
        Ok((pcm, negotiated_buffer)) => {
            if let Some(r) = input.resampler.as_mut() {
                r.reset();
            }
            if let Some(obs) = &input.direct_obs {
                obs.present.store(true, Ordering::Relaxed);
                // Re-store the freshly negotiated buffer: a device re-enumeration
                // could in principle land a different (still valid) geometry, so
                // STATUS tracks the live PCM, not the initial open's number.
                obs.buffer_frames
                    .store(negotiated_buffer as u64, Ordering::Relaxed);
                let opens = obs.opens.fetch_add(1, Ordering::Relaxed) + 1;
                info!(
                    "event=fanin.usb_direct.present device={} buffer_frames={} opens={} retries={} (reopened)",
                    obs.device,
                    negotiated_buffer,
                    opens,
                    obs.retries.load(Ordering::Relaxed),
                );
            }
            DirectCapture::Present(pcm)
        }
        Err(_) => {
            // Still absent — re-arm the retry latch. No per-retry log (only the
            // present/absent transitions log, C3).
            DirectCapture::Absent {
                periods_until_retry: DIRECT_REOPEN_RETRY_PERIODS,
            }
        }
    }
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
        input.read_buf.fill(0);
        return Ok(0);
    };
    let io = pcm.io_i16().context("getting i16 IO handle for input")?;
    match io.readi(&mut input.read_buf) {
        Ok(frames) => {
            input
                .frames_read
                .fetch_add(frames as u64, Ordering::Relaxed);
            // Zero the tail of read_buf if we got less than a full
            // period. The mixer's sum loop bounds the read region by
            // `frames`, but defense-in-depth: future code paths that
            // read the whole buffer (e.g., RMS for active detection)
            // should see zeros, not stale data, in the unfilled tail.
            if frames < requested_frames {
                let active = frames * (CHANNELS as usize);
                for s in &mut input.read_buf[active..] {
                    *s = 0;
                }
            }
            Ok(frames)
        }
        Err(e) => {
            let errno = e.errno();
            if errno == libc::EAGAIN {
                // Non-blocking read with no data ready. Renderer is
                // idle (or hasn't opened its substream yet). Treat
                // as silence.
                input.read_buf.fill(0);
                Ok(0)
            } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                // Input overrun: renderer produced faster than we
                // drained. snd_pcm_recover restarts the stream.
                let count = input.xrun_count.fetch_add(1, Ordering::Relaxed) + 1;
                warn!(
                    "event=fanin.xrun source=input label={} count={}",
                    input.label, count,
                );
                // Best-effort forward to the off-thread xrun log
                // writer. Send error means the receiver was dropped
                // (shutdown in progress); fine to ignore.
                let _ = xrun_tx.send(XrunEvent {
                    source: XrunSource::Input,
                    label: input.label.clone(),
                    frames: requested_frames as u32,
                    count,
                });
                pcm.try_recover(e, true).context("recovering input xrun")?;
                input.read_buf.fill(0);
                Ok(0)
            } else {
                Err(e).context(format!(
                    "reading from input {} ({})",
                    input.label, input.pcm_name
                ))
            }
        }
    }
}

/// Cap on period-equivalent work for the ARMED lane drain in one `step()` call.
/// Like `CATCHUP_MAX_DRAIN_PERIODS`, this bounds syscall work per period so a
/// pathological `avail` (driver fault) can't spin the hot loop. Frames beyond
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
    // The aloop resampler lane always has Some(pcm) (only the direct lane is
    // None, and it uses recover_direct_xrun instead).
    let Some(pcm) = input.pcm.as_ref() else {
        input.read_buf.fill(0);
        return Ok(());
    };
    pcm.try_recover(error, true)
        .context("recovering resampler input xrun")?;
    // `try_recover` can leave a capture PCM in PREPARED. The ordinary
    // read_input path will kick that forward with the next readi(), but the
    // resampler path polls avail_update() before reading; without an explicit
    // restart it can sit at avail=0 forever after a startup xrun.
    if pcm.state() != State::Running {
        pcm.start()
            .with_context(|| format!("restarting resampler input {} after xrun", input.label))?;
    }
    if let Some(r) = input.resampler.as_mut() {
        r.reset();
    }
    input.read_buf.fill(0);
    Ok(())
}

/// Read all currently-available frames from an ARMED lane into its resampler,
/// then render exactly one DAC-paced period into `read_buf`. Returns the number
/// of real (non-silence) frames the render produced — `period_frames` when the
/// resampler is locked, `0` while it is priming or underfilled (the lane is
/// silent, exactly as an idle renderer's substream is today).
///
/// This REPLACES the `drain_input_excess` + strict-one-period `read_input` pair
/// for the armed lane. Rate reconciliation lives in the resampler (DLL-steered
/// to the DAC clock); the catch-up drain is intentionally bypassed here.
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
        input.read_buf.fill(0);
        return Ok(0);
    }
    let mut read_budget_remaining =
        period_frames.saturating_mul(RESAMPLER_MAX_READ_PERIODS as usize);
    if read_budget_remaining > 0 {
        // Drain every frame ALSA reports ready, including final partial periods.
        // Re-check after each drained snapshot so frames that arrive during this
        // step do not sit in the kernel ring for a full extra render period.
        // The total work remains bounded by `read_budget_remaining`.
        let mut stop_drain = false;
        while read_budget_remaining > 0 && !stop_drain {
            let avail = match input
                .pcm
                .as_ref()
                .expect("aloop resampler lane always has Some(pcm)")
                .avail_update()
            {
                Ok(avail) => avail,
                Err(e) => {
                    let errno = e.errno();
                    if errno == libc::EAGAIN {
                        break;
                    } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                        recover_resampler_input_xrun(
                            input,
                            e,
                            period_frames,
                            xrun_tx,
                            "avail_update",
                        )?;
                        break;
                    } else {
                        return Err(e).context(format!(
                            "querying resampler input {} ({})",
                            input.label, input.pcm_name
                        ));
                    }
                }
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
                    let io = input
                        .pcm
                        .as_ref()
                        .expect("aloop resampler lane always has Some(pcm)")
                        .io_i16()
                        .context("getting i16 IO handle for resampler input")?;
                    io.readi(&mut input.read_buf[..samples_to_read])
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
                            r.push_input(&input.read_buf[..samples]);
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
                    Err(e) => {
                        let errno = e.errno();
                        if errno == libc::EAGAIN {
                            stop_drain = true;
                            break;
                        } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                            // Lane overrun: a discontinuity. Recover the PCM and
                            // reset the resampler so it re-primes from fresh input
                            // rather than interpolating across the gap.
                            recover_resampler_input_xrun(
                                input,
                                e,
                                period_frames,
                                xrun_tx,
                                "readi",
                            )?;
                            stop_drain = true;
                            break;
                        } else {
                            return Err(e).context(format!(
                                "reading from resampler input {} ({})",
                                input.label, input.pcm_name
                            ));
                        }
                    }
                }
            }
        }
    }

    // Render exactly one DAC-paced period into read_buf for the mixer to sum.
    let real_frames = match input.resampler.as_mut() {
        Some(r) => r.render_period(&mut input.read_buf),
        None => {
            // Unreachable in practice (only called when resampler.is_some()),
            // but stay safe: emit silence.
            input.read_buf.fill(0);
            0
        }
    };
    Ok(real_frames)
}

/// Write a full period to the output. Retries on transient xrun via
/// `try_recover`; propagates structural errors.
fn write_output(
    pcm: &PCM,
    buf: &[i16],
    xrun_counter: &Arc<AtomicU64>,
    xrun_tx: &Sender<XrunEvent>,
) -> Result<()> {
    let io = pcm.io_i16().context("getting i16 IO handle for output")?;
    let frames_total = buf.len() / (CHANNELS as usize);
    let mut frames_done = 0;
    // Limit recovery attempts per period to avoid an infinite loop
    // if the device is structurally broken.
    let mut recoveries = 0;
    const MAX_RECOVERIES_PER_PERIOD: u32 = 3;

    while frames_done < frames_total {
        let offset = frames_done * (CHANNELS as usize);
        match io.writei(&buf[offset..]) {
            Ok(n) => {
                frames_done += n;
                if n == 0 {
                    // Defensive: a zero-frame write that didn't error
                    // would spin. Treat as transient and back off
                    // one iteration via a recovery attempt.
                    recoveries += 1;
                    if recoveries > MAX_RECOVERIES_PER_PERIOD {
                        anyhow::bail!("output writei returned 0 frames repeatedly");
                    }
                }
            }
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    let count = xrun_counter.fetch_add(1, Ordering::Relaxed) + 1;
                    let pending = frames_total - frames_done;
                    warn!(
                        "event=fanin.xrun source=output count={} frames_pending={}",
                        count, pending,
                    );
                    let _ = xrun_tx.send(XrunEvent {
                        source: XrunSource::Output,
                        label: "output".to_string(),
                        frames: pending as u32,
                        count,
                    });
                    pcm.try_recover(e, true).context("recovering output xrun")?;
                    recoveries += 1;
                    if recoveries > MAX_RECOVERIES_PER_PERIOD {
                        anyhow::bail!(
                            "output xrun recovery exceeded {} attempts in one period",
                            MAX_RECOVERIES_PER_PERIOD,
                        );
                    }
                    // Loop continues; retry the write from `frames_done`.
                } else {
                    return Err(e).context("writing to output PCM");
                }
            }
        }
    }
    Ok(())
}

/// Write one period to the OPTIONAL music-only side-output. This is a
/// LOSSY side-tap, NOT a paced output: it must never block the work loop
/// and never escalate an error — the primary `output` is the sole timing
/// owner (inv-1). On a full ring (`EAGAIN`/short avail: the consumer is
/// behind) or an underrun (`EPIPE`: the consumer hasn't started reading)
/// we DROP this whole period and count it — snapserver sees a brief gap,
/// exactly like a starved capture, never back-pressure on the DAC loop.
///
/// **Period-aligned by construction:** we only write when the ring has
/// room for a WHOLE period (checked via `avail_update`). Only this thread
/// writes this PCM and the consumer only frees space, so room observed is
/// room guaranteed — a partial write can't shear a period and desync the
/// stream. A non-zero, growing `drops` is the operator's "consumer behind"
/// signal (surfaced via STATUS).
fn write_music_only(
    pcm: &PCM,
    buf: &[i16],
    frames_written: &Arc<AtomicU64>,
    drops: &Arc<AtomicU64>,
) {
    let frames_total = (buf.len() / (CHANNELS as usize)) as Frames;
    match pcm.avail_update() {
        // Room for a full period → write below.
        Ok(avail) if avail >= frames_total => {}
        // Ring too full for a whole period (consumer behind) → drop.
        Ok(_) => {
            drops.fetch_add(1, Ordering::Relaxed);
            return;
        }
        // Underrun / error → recover for next period, drop this one.
        Err(e) => {
            let _ = pcm.try_recover(e, true);
            drops.fetch_add(1, Ordering::Relaxed);
            return;
        }
    }
    let io = match pcm.io_i16() {
        Ok(io) => io,
        Err(_) => {
            drops.fetch_add(1, Ordering::Relaxed);
            return;
        }
    };
    match io.writei(buf) {
        Ok(n) => {
            frames_written.fetch_add(n as u64, Ordering::Relaxed);
        }
        Err(e) => {
            // try_recover handles EPIPE/ESTRPIPE; any error → drop, never
            // propagate (a broken side-tap must not crash the daemon).
            let _ = pcm.try_recover(e, true);
            drops.fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Pure-function tests for the mix math. No ALSA needed.

    // ---- USB DIRECT pure helpers (C1/C2) ---------------------------------

    #[test]
    fn direct_open_params_accepts_bridge_envelope() {
        // Default geometry: period 256, buffer 768 = 3×period (the deep-buffer
        // floor), byte-identical to the pre-lever-2 envelope.
        assert!(direct_open_params_ok(48_000, 256, 768, 256).is_ok());
        // H1 geometry: open period 64 with a DEEP buffer (768 = 12 periods, well
        // over the 3-period + 768-frame floor).
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
        // A shallow 2-period buffer at period 64 (128 frames) is the REFUTED
        // shallow-buffer class — it clears 2×period but not the deep floor.
        assert!(direct_open_params_ok(48_000, 64, 128, 64).is_err());
    }

    #[test]
    fn resolve_direct_buffer_frames_holds_deep_floor() {
        // Default period reproduces the historical fixed 768-frame buffer.
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
    fn classify_direct_errno_maps_c1_fates() {
        assert_eq!(
            classify_direct_errno(libc::EAGAIN),
            DirectReadFate::WouldBlock
        );
        assert_eq!(classify_direct_errno(libc::EPIPE), DirectReadFate::Xrun);
        assert_eq!(classify_direct_errno(libc::ESTRPIPE), DirectReadFate::Xrun);
        // ENODEV on unplug (and any other errno) is a device loss, never a
        // daemon error.
        assert_eq!(
            classify_direct_errno(libc::ENODEV),
            DirectReadFate::DeviceLost
        );
        assert_eq!(classify_direct_errno(libc::EIO), DirectReadFate::DeviceLost);
    }

    #[test]
    fn direct_reopen_cadence_is_about_two_seconds() {
        // 375 periods × 256 frames / 48000 Hz = 2.0 s (C3).
        let seconds = (DIRECT_REOPEN_RETRY_PERIODS as f64) * (DIRECT_PERIOD_FRAMES as f64)
            / (SAMPLE_RATE_HZ as f64);
        assert!(
            (seconds - 2.0).abs() < 1e-9,
            "reopen cadence must be ~2 s, got {seconds}"
        );
    }

    #[test]
    fn zombie_handle_suspected_fires_only_at_threshold() {
        // Below the threshold: not a zombie (a healthy host can be briefly idle).
        assert!(!zombie_handle_suspected(
            0,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(!zombie_handle_suspected(
            1,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(!zombie_handle_suspected(
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS - 1,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        // At or beyond the threshold: the Present handle has been deaf too long.
        assert!(zombie_handle_suspected(
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        assert!(zombie_handle_suspected(
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS + 100,
            DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS
        ));
        // A zero threshold disables the detector (belt-and-braces: never fire).
        assert!(!zombie_handle_suspected(u64::MAX, 0));
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

    #[test]
    fn direct_lane_narrows_via_shared_conversion() {
        // The direct lane uses jasper_resampler's narrowing (C2). Re-assert the
        // pinned sign-boundary vector here too so a drift fails the fanin suite.
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
        let mut sum = vec![0i32; 4];
        mix_into(&mut sum, &[100, 200, 300, 400]);
        mix_into(&mut sum, &[50, 50, 50, 50]);
        assert_eq!(sum, vec![150, 250, 350, 450]);
    }

    #[test]
    fn mix_into_saturates_at_i32_bounds_but_stays_room_for_i16_saturation() {
        // Two max-i16 inputs sum to 2 × 32767 = 65534 — well within i32.
        // Only saturate_to_i16 should clip; mix_into just accumulates.
        let mut sum = vec![0i32; 1];
        mix_into(&mut sum, &[i16::MAX]);
        mix_into(&mut sum, &[i16::MAX]);
        assert_eq!(sum[0], 65534);
    }

    #[test]
    fn mix_into_cancels_positive_and_negative() {
        let mut sum = vec![0i32; 2];
        mix_into(&mut sum, &[5000, -3000]);
        mix_into(&mut sum, &[-5000, 3000]);
        assert_eq!(sum, vec![0, 0]);
    }

    #[test]
    fn apply_gain_to_sum_ducks_after_program_sum() {
        let mut sum = vec![20_000i32, -20_000, 1_500, -1_500];
        apply_gain_to_sum(&mut sum, 0.1);
        assert_eq!(sum, vec![2_000, -2_000, 150, -150]);
    }

    #[test]
    fn music_only_tap_is_post_duck_and_pre_tts() {
        // Mirrors step()'s tap point exactly: the music-only buffer is the
        // summed program AFTER the program duck and BEFORE TTS is mixed.
        // Two music lanes summed:
        let mut sum = vec![0i32; 4];
        mix_into(&mut sum, &[10_000, -10_000, 8_000, -8_000]);
        mix_into(&mut sum, &[2_000, -2_000, 1_000, -1_000]);
        // Program duck applies (TTS active): attenuate the program by 0.5.
        apply_gain_to_sum(&mut sum, 0.5);
        // TAP HERE — clamp to i16 for the music-only output.
        let mut music_only = vec![0i16; 4];
        saturate_to_i16(&sum, &mut music_only);
        // Post-duck (×0.5), pre-TTS: (12000,-12000,9000,-9000) × 0.5.
        assert_eq!(music_only, vec![6_000, -6_000, 4_500, -4_500]);

        // Now TTS would mix into the PRIMARY sum only — the tapped buffer
        // is already captured and is unaffected, which is the inv-3
        // guarantee: the assistant never reaches the synced (follower)
        // stream. Prove the tap is independent of the later TTS add:
        for s in sum.iter_mut() {
            *s = s.saturating_add(20_000); // stand-in for tts.mix_period
        }
        assert_eq!(music_only, vec![6_000, -6_000, 4_500, -4_500]);
    }

    #[test]
    fn saturate_to_i16_clamps_positive_overflow() {
        let mut out = vec![0i16; 1];
        saturate_to_i16(&[100_000], &mut out);
        assert_eq!(out[0], i16::MAX);
    }

    #[test]
    fn saturate_to_i16_clamps_negative_overflow() {
        let mut out = vec![0i16; 1];
        saturate_to_i16(&[-100_000], &mut out);
        assert_eq!(out[0], i16::MIN);
    }

    #[test]
    fn saturate_to_i16_passes_in_range_values() {
        let mut out = vec![0i16; 4];
        saturate_to_i16(&[0, 1000, -1000, 32767], &mut out);
        assert_eq!(out, vec![0, 1000, -1000, i16::MAX]);
    }

    #[test]
    fn mix_three_inputs_full_pipeline() {
        // Three inputs at ~1/3 max each: sum approaches max but
        // doesn't saturate. Models the realistic three-renderer
        // simultaneous-handover transient.
        let mut sum = vec![0i32; 4];
        mix_into(&mut sum, &[10_000, 10_000, 10_000, 10_000]);
        mix_into(&mut sum, &[10_000, 10_000, 10_000, 10_000]);
        mix_into(&mut sum, &[10_000, 10_000, 10_000, 10_000]);
        let mut out = vec![0i16; 4];
        saturate_to_i16(&sum, &mut out);
        assert_eq!(out, vec![30_000, 30_000, 30_000, 30_000]);
    }

    #[test]
    fn mix_three_max_inputs_saturates_output() {
        // Three max-positive inputs sum to 98_301, well above i16::MAX.
        // Saturation clips to 32767.
        let mut sum = vec![0i32; 2];
        mix_into(&mut sum, &[i16::MAX, i16::MAX]);
        mix_into(&mut sum, &[i16::MAX, i16::MAX]);
        mix_into(&mut sum, &[i16::MAX, i16::MAX]);
        let mut out = vec![0i16; 2];
        saturate_to_i16(&sum, &mut out);
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
    // on any host that can build the crate (CI Linux). `mirror: None` keeps ALSA
    // out of the test — the ring publish + reader roundtrip is the contract.

    use jasper_ring::{RingReader, SlotRead};
    use std::sync::atomic::AtomicU64 as TestAtomicU64;

    static RING_MIXER_TEST_SEQ: TestAtomicU64 = TestAtomicU64::new(0);

    fn ring_geometry(n_slots: u32) -> Geometry {
        Geometry {
            rate: 48_000,
            channels: CHANNELS,
            sample_format: SAMPLE_FORMAT_S16LE,
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
            mirror: None,
            // One 256-frame period at 48k, in ns — used only on the reader-absent
            // self-pace path (avoided in these live-reader tests).
            self_pace_period_ns: 256 * 1_000_000_000 / 48_000,
        };
        (ring, path)
    }

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
        let mut sum = vec![0i32; total];
        mix_into(&mut sum, &vec![10_000i16; total]); // program lane
        apply_gain_to_sum(&mut sum, 0.5); // duck (TTS active)
        for s in sum.iter_mut() {
            *s = s.saturating_add(4_000); // stand-in for tts.mix_period
        }
        let mut output_buf = vec![0i16; total];
        saturate_to_i16(&sum, &mut output_buf); // expected: 5000 + 4000 = 9000

        let published_frames = write_ring_period(&mut ring, &output_buf, period_frames);
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
        assert_eq!(ring.counters.drops.load(Ordering::Relaxed), 0);
        cleanup_ring(&path);
    }

    /// Reader-absent: `write_ring_period` free-run-drops and self-paces (never
    /// hot-spins). Counters reflect the drops; occupancy stays bounded. This
    /// stands in the "CamillaDSP not yet up / reloading" turn.
    #[test]
    fn ring_output_free_runs_and_self_paces_without_reader() {
        let period_frames = 256u32;
        let (mut ring, path) = tmp_ring_output(2, "no_reader");
        // No reader attached: reader_pid == 0.
        let total = (period_frames as usize) * (CHANNELS as usize);
        let output_buf = vec![7i16; total];

        // Fill the ring, then publish several more periods. Each free-run-drops
        // the oldest and self-paces one period; bound the wall time so the
        // self-pacing sleep can't wedge the loop.
        let start = std::time::Instant::now();
        let mut per_period_published = Vec::with_capacity(4);
        for _ in 0..4 {
            per_period_published.push(write_ring_period(&mut ring, &output_buf, period_frames));
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
        // 4 periods * (2 slots each) with a per-period self-pace sleep (~5.3 ms):
        // bounded well under the 5 s watchdog threshold.
        assert!(
            elapsed < std::time::Duration::from_secs(1),
            "self-pacing must stay bounded, got {elapsed:?}"
        );
        // Drops accrued (no live reader); occupancy bounded at n_slots.
        assert!(ring.counters.drops.load(Ordering::Relaxed) > 0);
        assert!(ring.counters.occupancy.load(Ordering::Relaxed) <= 2);
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
}
