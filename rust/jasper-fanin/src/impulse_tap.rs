// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Default-off impulse tap on the USB DIRECT capture ingress stream.
//!
//! # Why it lives here
//!
//! Route-latency evidence is only trustworthy if the ingress timestamp is
//! taken *on the claiming route's own audio path*. Ported verbatim from
//! `jasper-usbsink-audio`'s tap (same JSONL schema, same detector, same arm
//! validation): when `JASPER_FANIN_USB_DIRECT=enabled`, the certified route's
//! ingress is fan-in's `hw:UAC2Gadget` capture — the usbsink bridge hop is
//! gone — so the tap must run HERE, inline in `read_direct_and_render` over the
//! already-converted S16 slice, before it is pushed into the lane resampler. It
//! is the Pi-side ingress half of the click/capture harness; the Python
//! `jasper-route-latency-harness` owns the egress (mic) half and pairs the two
//! timelines. Both timestamps are `CLOCK_MONOTONIC` on the same Pi — the only
//! reason the cross-timeline subtraction the harness does is valid, and why
//! this tap emits monotonic ns, never epoch ns.
//!
//! The bridge's own tap is DEAD in direct mode (the bridge runs in standby and
//! opens no capture, so its detector never fires); this JSONL under
//! `/run/jasper-fanin/` is the only ingress evidence for the direct route.
//!
//! # Cost model (COAH resilience)
//!
//! - **Disarmed: one relaxed atomic load per period, nothing else.** The audio
//!   loop calls [`TapState::armed`] first; when false it does no work.
//! - **Armed: pure inline arithmetic in the audio thread.** Detection is a peak
//!   scan over the S16 slice plus a hysteresis/refractory state machine — no
//!   allocation, no syscall, no lock. On a detection the audio thread builds a
//!   fixed-size [`TapEvent`] and pushes it through a bounded [`SyncSender`]
//!   with `try_send`; on `Full` it bumps a dropped counter and continues
//!   (drop-and-count). JSONL bytes are written only by the existing state
//!   publisher thread draining the channel — the audio thread never touches
//!   tap I/O.
//! - **Bounded artifact.** The JSONL file is truncated on arm; the publisher
//!   stops appending past `max_events` (still counting further events as
//!   dropped) and auto-disarms past the `auto_disarm` deadline, so a forgotten
//!   tap costs nothing after its window closes.
//!
//! # Sample-accurate `monotonic_ns`
//!
//! The audio loop timestamps each capture read with `clock_gettime(
//! CLOCK_MONOTONIC)` **immediately after** `read_capture_frames` returns
//! `frames` (so all `frames` samples are already in hand at `period_read_ns`).
//! A detection at `sample_offset` within that period therefore happened
//! `(frames - sample_offset)` frames *before* the read timestamp:
//!
//! ```text
//! monotonic_ns = period_read_ns - (frames - sample_offset) * 1e9 / 48_000
//! ```
//!
//! Uncertainty is one ALSA read granularity (a 256-frame period ≈ 5.3 ms at
//! 48 kHz) plus the scheduling jitter between the last DMA sample and the
//! syscall return. That bound is the tap's contribution to the harness's
//! per-impulse latency error budget; the mic leg adds its own (documented in
//! the Python harness).
//!
//! # Host-testability
//!
//! Everything in this module is ALSA-independent: the detector, the offset
//! math, the arm-body parsing, and the JSONL line formatting. `jasper-fanin`
//! links libasound unconditionally (unlike the usbsink crate's feature gate),
//! so this module is covered on a non-Linux host via the macOS-ALSA-scratch
//! convention (copy the pure modules into a scratch crate). The wiring in
//! `mixer.rs` (`clock_gettime`, the direct-read hook) and `state.rs`/`main.rs`
//! (control verbs, the writer thread) is where ALSA lives.

use std::fs;
use std::io::Write as _;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc::Receiver;
use std::sync::Mutex;

use serde_json::Value;

use crate::json::json_string;

/// Nanoseconds per second (for the frame → time mapping).
const NANOS_PER_SEC: i128 = 1_000_000_000;

/// The ONLY directory a `TAP_ARM` request may place its JSONL artifact in.
///
/// The `TAP_ARM {json}` body arrives on fan-in's control UDS
/// (`/run/jasper-fanin/control.sock`), which is local-only but still
/// truncates+writes this file. Constraining the path to this tmpfs dir — the
/// same one that holds `control.sock` / `tts.sock` — turns "arm the tap" from
/// an arbitrary-file-truncate primitive into a scoped one: a caller can only
/// clobber files inside a directory that already belongs to this daemon. The
/// harness's own `DEFAULT_TAP_PATH` is under here, and `--tap-path` can still
/// choose any filename within it (except the reserved daemon files below). See
/// `path_is_allowed`.
pub const TAP_PATH_DIR: &str = "/run/jasper-fanin";

/// Default amplitude threshold (normalized 0..1 abs peak) a period must reach
/// to arm a rising edge. Chosen so the harness's ~-12 dBFS click (peak ≈ 0.25)
/// clears it with margin while room noise / music transients on the raw ingress
/// stay below it. Operators can lower it for quieter clicks.
pub const DEFAULT_THRESHOLD: f64 = 0.2;

/// Default hysteresis (normalized). The latched edge only releases once the
/// peak falls below `threshold - hysteresis`, so a click's decay tail does not
/// re-trigger within the same impulse.
pub const DEFAULT_HYSTERESIS: f64 = 0.05;

/// Default refractory window in milliseconds. After a detection, no new edge
/// fires for this long — long enough to swallow the ringing of one physical
/// click, short enough that the harness's jittered spacing (seconds apart) is
/// never merged.
pub const DEFAULT_REFRACTORY_MS: u64 = 250;

/// Default cap on JSONL events per arm. A promotion run is ~1100 impulses; the
/// default leaves generous headroom while bounding a runaway file.
pub const DEFAULT_MAX_EVENTS: u64 = 4000;

/// Hard ceiling on `max_events` a `TAP_ARM` request may set. The arm body
/// arrives as `TAP_ARM {json}` over fan-in's local control UDS
/// (`/run/jasper-fanin/control.sock`), gated only by socket-file
/// permissions (no TCP port, no auth token), and each JSONL line is ~100
/// bytes on a tmpfs the unit caps at `MemoryMax=64M`. Without a ceiling a
/// caller (or an operator typo of
/// `10^18`) could grow the file until the memcg OOM-kills the audio daemon.
/// 100_000 events ≈ 10 MB — ample for any real run (a promotion run is ~1100
/// impulses), safely under the memcg cap. Requests above this are rejected at
/// parse time.
pub const MAX_EVENTS_CEILING: u64 = 100_000;

/// Default auto-disarm horizon in minutes. Longer than a 33-minute promotion
/// run with margin; a forgotten tap disarms itself and stops all cost.
pub const DEFAULT_AUTO_DISARM_MIN: u64 = 45;

/// Hard ceiling on `auto_disarm_min` a `TAP_ARM` request may set (24 h). The
/// whole point of auto-disarm is that a forgotten tap costs nothing; a caller
/// asking for a multi-year horizon defeats that. A day is far longer than any
/// legitimate measurement window and keeps the self-healing guarantee real.
pub const AUTO_DISARM_MIN_CEILING: u64 = 24 * 60;

/// Default JSONL artifact path (tmpfs, same dir as the fan-in control socket).
pub const DEFAULT_TAP_PATH: &str = "/run/jasper-fanin/impulse-tap.jsonl";

/// Basenames fan-in itself owns inside [`TAP_PATH_DIR`]; a `TAP_ARM` may NOT
/// target them even though they are in-directory. `control.sock` is the daemon's
/// own UDS (truncating it would corrupt the live control plane) and `tts.sock` is
/// the pre-DSP TTS ingress socket — clobbering either would break a live audio
/// surface. Kept in sync by hand with the config defaults in `config.rs`
/// (`control_socket_path` / `tts_socket_path`), both of
/// which live under this same dir; a pin test in `config.rs` ties them together.
pub const RESERVED_TAP_DIR_BASENAMES: &[&str] = &["control.sock", "tts.sock"];

/// One ingress detection, serialized as a single JSONL line by the publisher.
///
/// Field meanings are pinned by the harness contract:
/// - `monotonic_ns`: sample-accurate ingress time (see module header math).
/// - `frame_index`: cumulative captured-frame counter at detection (monotonic
///   per process; the audio loop bumps it by `frames` each read).
/// - `ring_fill_frames`: `ring.fill_periods() * period_frames` at detection —
///   the pre-read backlog the click still had to drain through, recorded as
///   diagnostic context for reading a run's per-impulse spread. It is NOT
///   added to the harness's latency: the tap timestamps ingress before the
///   click enters the ring, so the ring dwell already elapses inside the
///   `t_mic - t_tap` subtraction (adding it too would double-count). The live
///   `state.json` reports fill in *periods*; the tap records *frames*.
/// - `peak`: normalized abs peak of the detected period (0..1).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TapEvent {
    pub monotonic_ns: i128,
    pub frame_index: u64,
    pub ring_fill_frames: u64,
    pub peak: f64,
}

impl TapEvent {
    /// Serialize as one compact JSONL line (no trailing newline).
    ///
    /// `peak` is emitted with fixed precision so the line width is bounded and
    /// the file stays grep/parse-friendly; the harness reads it as a float.
    /// Takes `self` by value — `TapEvent` is `Copy` and small.
    pub fn to_jsonl(self) -> String {
        format!(
            "{{\"monotonic_ns\":{},\"frame_index\":{},\"ring_fill_frames\":{},\"peak\":{:.6}}}",
            self.monotonic_ns, self.frame_index, self.ring_fill_frames, self.peak,
        )
    }
}

/// Publisher/listener-owned tap parameters (never read by the audio thread).
///
/// The audio thread reads only the atomic detector knobs on [`TapState`]; this
/// struct carries the values that gate I/O — the file path, the event cap, and
/// the auto-disarm deadline — which only the publisher and listener touch.
#[derive(Clone, Debug, PartialEq)]
pub struct TapConfig {
    pub path: PathBuf,
    pub threshold: f64,
    pub hysteresis: f64,
    pub refractory_ms: u64,
    pub max_events: u64,
    pub auto_disarm_min: u64,
}

impl Default for TapConfig {
    fn default() -> Self {
        Self {
            path: PathBuf::from(DEFAULT_TAP_PATH),
            threshold: DEFAULT_THRESHOLD,
            hysteresis: DEFAULT_HYSTERESIS,
            refractory_ms: DEFAULT_REFRACTORY_MS,
            max_events: DEFAULT_MAX_EVENTS,
            auto_disarm_min: DEFAULT_AUTO_DISARM_MIN,
        }
    }
}

impl TapConfig {
    /// Parse a `TAP_ARM {json}` body onto defaults. All fields optional; unknown
    /// keys ignored. Mirrors `parse_preempt_silenced`'s `serde_json::from_str`
    /// shape. Rejects non-finite / non-positive numeric knobs so a bad request
    /// can never install a detector that never fires or never releases, and
    /// rejects any `path` outside [`TAP_PATH_DIR`] (see [`path_is_allowed`]) so
    /// the unauthenticated arm command can't be used to truncate an arbitrary
    /// root-owned file.
    pub fn from_arm_body(body: &str) -> Option<Self> {
        let value: Value = serde_json::from_str(body.trim()).ok()?;
        let obj = value.as_object()?;
        let mut cfg = TapConfig::default();
        if let Some(path) = obj.get("path") {
            let path = path.as_str()?;
            if path.is_empty() {
                return None;
            }
            let candidate = PathBuf::from(path);
            if !path_is_allowed(&candidate) {
                return None;
            }
            cfg.path = candidate;
        }
        if let Some(threshold) = obj.get("threshold") {
            cfg.threshold = finite_positive(threshold)?;
        }
        if let Some(hysteresis) = obj.get("hysteresis") {
            // Hysteresis may legitimately be 0 (no dead-band); reject only NaN /
            // negative.
            let h = hysteresis.as_f64()?;
            if !h.is_finite() || h < 0.0 {
                return None;
            }
            cfg.hysteresis = h;
        }
        if let Some(refractory_ms) = obj.get("refractory_ms") {
            cfg.refractory_ms = positive_u64(refractory_ms)?;
        }
        if let Some(max_events) = obj.get("max_events") {
            cfg.max_events = positive_u64_capped(max_events, MAX_EVENTS_CEILING)?;
        }
        if let Some(auto_disarm_min) = obj.get("auto_disarm_min") {
            cfg.auto_disarm_min = positive_u64_capped(auto_disarm_min, AUTO_DISARM_MIN_CEILING)?;
        }
        Some(cfg)
    }
}

fn finite_positive(value: &Value) -> Option<f64> {
    let f = value.as_f64()?;
    if f.is_finite() && f > 0.0 {
        Some(f)
    } else {
        None
    }
}

/// A positive `u64` knob from a JSON number. Accepts a native JSON integer
/// (`300`) OR an integral float (`300.0`) — the latter defends the boundary
/// against a client that serializes an integer-valued knob as a float (a
/// `type=float` CLI arg is the motivating case; the Python side also coerces
/// these to int, so this is defense on both sides). A non-integral float
/// (`300.5`), a non-finite float, a negative, or a zero is rejected: these
/// knobs (refractory_ms/max_events/auto_disarm_min) have no meaningful
/// fractional or non-positive value.
fn positive_u64(value: &Value) -> Option<u64> {
    let n = value.as_u64().or_else(|| {
        let f = value.as_f64()?;
        // Integral, finite, in u64 range, and > 0 (checked below via the same
        // `n > 0` gate). `fract() == 0.0` rejects `300.5`; the range check
        // rejects a float too large for u64 (which would wrap on `as u64`).
        if f.is_finite() && f.fract() == 0.0 && f >= 0.0 && f <= u64::MAX as f64 {
            Some(f as u64)
        } else {
            None
        }
    })?;
    if n > 0 {
        Some(n)
    } else {
        None
    }
}

/// A positive `u64` that also must not exceed `ceiling` (inclusive). Rejecting
/// out-of-range values at parse time keeps an unauthenticated arm request from
/// installing a resource-unbounded tap (see [`MAX_EVENTS_CEILING`] /
/// [`AUTO_DISARM_MIN_CEILING`]).
fn positive_u64_capped(value: &Value, ceiling: u64) -> Option<u64> {
    let n = positive_u64(value)?;
    if n <= ceiling {
        Some(n)
    } else {
        None
    }
}

/// True iff `path` is a direct child file of [`TAP_PATH_DIR`] with no traversal.
///
/// The check is purely lexical (it never touches the filesystem, so it can't be
/// raced) and deliberately strict:
/// - the path must be absolute and start with exactly the [`TAP_PATH_DIR`]
///   components (an attacker can't pick `/run/jasper-fanin-evil/...`);
/// - no `..` (parent) or `.` (curdir) components anywhere, so
///   `/run/jasper-fanin/../etc/passwd` is rejected before any normalization
///   could resolve it out of the dir;
/// - exactly one trailing file-name component after the dir (no nested
///   subdirs the daemon would then `create_dir_all` as root);
/// - the file-name is not one of the daemon's own reserved basenames
///   ([`RESERVED_TAP_DIR_BASENAMES`]), so the tap can't clobber `control.sock` /
///   `tts.sock`.
///
/// This scopes the unauthenticated arm endpoint's file write to a directory
/// that already belongs to the daemon (see [`TAP_PATH_DIR`]).
pub fn path_is_allowed(path: &Path) -> bool {
    let allowed = Path::new(TAP_PATH_DIR);
    // Reject any `.`/`..` component outright — no lexical traversal games.
    if path
        .components()
        .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
    {
        return false;
    }
    // Never let the tap target the daemon's own reserved files.
    if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
        if RESERVED_TAP_DIR_BASENAMES.contains(&name) {
            return false;
        }
    }
    match path.parent() {
        Some(parent) => parent == allowed,
        None => false,
    }
}

/// Cross-thread tap state.
///
/// - `armed` / detector knobs (`threshold_milli`, `hysteresis_milli`,
///   `refractory_frames`) are read by the audio thread lock-free.
/// - `generation` bumps on every arm so the audio thread can cheaply notice a
///   fresh arm and reload its local detector without polling params each period.
/// - `events_written` / `events_dropped` are the observable counters.
///
/// The audio thread never reads [`TapConfig`]; it uses only these atomics.
#[derive(Debug)]
pub struct TapState {
    armed: AtomicBool,
    generation: AtomicU64,
    threshold_milli: AtomicU32,
    hysteresis_milli: AtomicU32,
    refractory_frames: AtomicU64,
    events_written: AtomicU64,
    events_dropped: AtomicU64,
    // Observability-only wall-clock deadline (0 when disarmed). Enforcement uses
    // the publisher's monotonic clock; this epoch value is for humans/doctor.
    auto_disarm_at_epoch_ms: AtomicU64,
}

impl Default for TapState {
    fn default() -> Self {
        Self {
            armed: AtomicBool::new(false),
            generation: AtomicU64::new(0),
            threshold_milli: AtomicU32::new(f64_to_milli(DEFAULT_THRESHOLD)),
            hysteresis_milli: AtomicU32::new(f64_to_milli(DEFAULT_HYSTERESIS)),
            refractory_frames: AtomicU64::new(0),
            events_written: AtomicU64::new(0),
            events_dropped: AtomicU64::new(0),
            auto_disarm_at_epoch_ms: AtomicU64::new(0),
        }
    }
}

impl TapState {
    /// One relaxed load — the disarmed fast path in the audio loop.
    #[inline]
    pub fn armed(&self) -> bool {
        self.armed.load(Ordering::Relaxed)
    }

    /// Load the detector knobs the audio thread needs to (re)build its local
    /// [`ImpulseDetector`]. Called only when the audio thread observes a new
    /// generation, so it is off the per-period hot path.
    pub fn detector_knobs(&self) -> (f64, f64, u64) {
        (
            milli_to_f64(self.threshold_milli.load(Ordering::Relaxed)),
            milli_to_f64(self.hysteresis_milli.load(Ordering::Relaxed)),
            self.refractory_frames.load(Ordering::Relaxed),
        )
    }

    /// Arm the tap with `cfg`, resolving the refractory window into frames at
    /// `sample_rate` and the auto-disarm horizon into a wall-clock deadline
    /// from `arm_epoch_ms` (observability only).
    ///
    /// Ordering: the knob stores are Relaxed, then `armed` is set Relaxed, then
    /// `generation` is bumped with Release LAST. The audio thread's knob
    /// visibility comes from its Acquire load of `generation` (see
    /// `generation_acquire` / `tap_over_read`), which pairs with the Release
    /// bump here — NOT from the `armed` load, which is Relaxed on both sides.
    /// So `armed==true` becoming visible does not by itself guarantee the new
    /// knobs are visible; the audio thread only rebuilds its detector after it
    /// Acquire-observes the new generation. The bounded consequence: for the
    /// first period or two after arm, the audio thread may run on the previous
    /// generation's (or default) knobs before it notices the generation
    /// changed — harmless because arm precedes playback by human-seconds.
    ///
    /// `armed` is stored BEFORE the generation bump on purpose. The publisher
    /// (`TapPublisher::poll`) reads the generation first, then `tap.armed()`,
    /// and only (re)opens the sink when the generation advanced. If `armed`
    /// were set AFTER the generation bump, a publisher poll landing in the
    /// instruction-scale window between the two stores would consume the new
    /// generation while still seeing `armed==false`, leave `sink=None`, and
    /// then never re-open until the NEXT arm — the JSONL would stay empty
    /// (`armed:true, events_written:0`) with dropped climbing once the channel
    /// filled. Setting `armed` first closes that race: a poll that observes the
    /// new generation is guaranteed to also observe `armed==true`.
    pub fn arm(&self, cfg: &TapConfig, sample_rate: u32, arm_epoch_ms: u64) {
        let refractory_frames = ((cfg.refractory_ms as u128) * (sample_rate as u128) / 1000) as u64;
        let auto_disarm_at =
            arm_epoch_ms.saturating_add((cfg.auto_disarm_min).saturating_mul(60_000));
        self.threshold_milli
            .store(f64_to_milli(cfg.threshold), Ordering::Relaxed);
        self.hysteresis_milli
            .store(f64_to_milli(cfg.hysteresis), Ordering::Relaxed);
        self.refractory_frames
            .store(refractory_frames, Ordering::Relaxed);
        self.auto_disarm_at_epoch_ms
            .store(auto_disarm_at, Ordering::Relaxed);
        self.events_written.store(0, Ordering::Relaxed);
        self.events_dropped.store(0, Ordering::Relaxed);
        self.armed.store(true, Ordering::Relaxed);
        // Bump generation with Release LAST so any thread that Acquire-loads the
        // new generation also sees the knob stores AND armed==true above (closes
        // the publisher arm race — see the ordering note in this method's doc).
        self.generation.fetch_add(1, Ordering::Release);
    }

    /// Disarm the tap (idempotent). Leaves counters intact for the disarm reply
    /// and clears the wall-clock deadline.
    pub fn disarm(&self) {
        self.armed.store(false, Ordering::Relaxed);
        self.auto_disarm_at_epoch_ms.store(0, Ordering::Relaxed);
    }

    #[inline]
    pub fn auto_disarm_at_epoch_ms(&self) -> u64 {
        self.auto_disarm_at_epoch_ms.load(Ordering::Relaxed)
    }

    #[inline]
    pub fn note_written(&self) {
        self.events_written.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn note_dropped(&self) {
        self.events_dropped.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn events_written(&self) -> u64 {
        self.events_written.load(Ordering::Relaxed)
    }

    #[inline]
    pub fn events_dropped(&self) -> u64 {
        self.events_dropped.load(Ordering::Relaxed)
    }

    /// Acquire-load the generation so a thread that just observed `armed==true`
    /// also observes the matching knob stores (pairs with `arm`'s Release).
    #[inline]
    pub fn generation_acquire(&self) -> u64 {
        self.generation.load(Ordering::Acquire)
    }

    /// Render just the tap object (no outer key, no trailing newline) for
    /// embedding in the control-socket `STATUS` reply under `"tap"`.
    pub fn status_fragment(&self, cfg: &TapConfig) -> String {
        format!(
            concat!(
                "{{",
                "\"armed\":{},",
                "\"events_written\":{},",
                "\"events_dropped\":{},",
                "\"threshold\":{:.3},",
                "\"refractory_ms\":{},",
                "\"max_events\":{},",
                "\"auto_disarm_at_epoch_ms\":{},",
                "\"path\":{}",
                "}}"
            ),
            if self.armed() { "true" } else { "false" },
            self.events_written(),
            self.events_dropped(),
            milli_to_f64(self.threshold_milli.load(Ordering::Relaxed)),
            cfg.refractory_ms,
            cfg.max_events,
            self.auto_disarm_at_epoch_ms(),
            json_string(&cfg.path.to_string_lossy()),
        )
    }
}

// The audio thread reads detector knobs as atomics. f64 has no stable atomic
// on all targets, so knobs are stored as fixed-point milli-units (0.001
// resolution) — ample for a 0..1 normalized amplitude threshold.
fn f64_to_milli(value: f64) -> u32 {
    (value.clamp(0.0, 4_000_000.0) * 1000.0).round() as u32
}

fn milli_to_f64(milli: u32) -> f64 {
    (milli as f64) / 1000.0
}

/// Peak + hysteresis + refractory impulse detector over S16 periods.
///
/// Lives entirely in the audio thread (no sharing). A rising edge fires when a
/// period's normalized abs peak reaches `threshold` while the detector is not
/// latched and not within the refractory window; the latch releases once a
/// later period's peak falls below `threshold - hysteresis`. The refractory
/// window (in frames) suppresses re-fire within one physical click.
///
/// Detection granularity is one period: `detect` reports the frame offset of
/// the peak sample within the period so the caller can compute the
/// sample-accurate `monotonic_ns`.
#[derive(Clone, Debug)]
pub struct ImpulseDetector {
    threshold: f64,
    release_level: f64,
    refractory_frames: u64,
    channels: usize,
    latched: bool,
    // Cumulative captured-frame index of the last detection; `u64::MAX/2`
    // sentinel means "no prior detection" so the first impulse is never
    // suppressed by an unset refractory anchor.
    last_detect_frame: u64,
}

/// A single detection within a period: the frame offset of the peak sample and
/// the normalized peak value.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Detection {
    pub sample_offset_frames: usize,
    pub peak: f64,
}

const NO_PRIOR_DETECT: u64 = u64::MAX / 2;

impl ImpulseDetector {
    /// Build a detector. `channels` de-interleaves the S16 period so the peak
    /// scan and the reported `sample_offset_frames` are frame-aligned.
    pub fn new(threshold: f64, hysteresis: f64, refractory_frames: u64, channels: usize) -> Self {
        Self {
            threshold,
            release_level: (threshold - hysteresis).max(0.0),
            refractory_frames,
            channels: channels.max(1),
            latched: false,
            last_detect_frame: NO_PRIOR_DETECT,
        }
    }

    /// Peak of one interleaved S16 period, returning `(peak, frame_offset)`.
    /// `frame_offset` is the interleaved sample's frame index (sample index /
    /// channels), i.e. the position in time within the period.
    fn scan_peak(&self, period: &[i16]) -> (f64, usize) {
        let mut best_abs: i32 = -1;
        let mut best_sample_idx: usize = 0;
        for (idx, sample) in period.iter().enumerate() {
            // abs of i16 without overflow on i16::MIN.
            let magnitude = (*sample as i32).unsigned_abs() as i32;
            if magnitude > best_abs {
                best_abs = magnitude;
                best_sample_idx = idx;
            }
        }
        let peak = (best_abs.max(0) as f64) / 32768.0;
        (peak, best_sample_idx / self.channels)
    }

    /// Run detection over one period whose first frame has cumulative index
    /// `period_start_frame`. Returns `Some(Detection)` on a rising edge, else
    /// `None`. Advances internal latch/refractory state.
    pub fn detect(&mut self, period: &[i16], period_start_frame: u64) -> Option<Detection> {
        let (peak, offset_frames) = self.scan_peak(period);

        // Release the latch once the signal has decayed below the release
        // level, so a subsequent (post-refractory) impulse can fire.
        if self.latched && peak < self.release_level {
            self.latched = false;
        }

        if self.latched || peak < self.threshold {
            return None;
        }

        let detect_frame = period_start_frame.saturating_add(offset_frames as u64);
        if self.last_detect_frame != NO_PRIOR_DETECT {
            let elapsed = detect_frame.saturating_sub(self.last_detect_frame);
            if elapsed < self.refractory_frames {
                // Within refractory: latch to avoid multiple fires off one click
                // but do not emit an event.
                self.latched = true;
                return None;
            }
        }

        self.latched = true;
        self.last_detect_frame = detect_frame;
        Some(Detection {
            sample_offset_frames: offset_frames,
            peak,
        })
    }
}

/// Sample-accurate ingress time for a detection.
///
/// `period_read_ns` is the `CLOCK_MONOTONIC` timestamp taken right after the
/// capture read of `frames_in_period` frames returned; `sample_offset_frames`
/// is the detection's frame offset within that period. The detection happened
/// `(frames_in_period - sample_offset_frames)` frames before the read
/// timestamp. Returns ns as `i128` to match `TapEvent::monotonic_ns` and to
/// keep the intermediate frame×1e9 product exact.
pub fn detection_monotonic_ns(
    period_read_ns: i128,
    frames_in_period: usize,
    sample_offset_frames: usize,
    sample_rate: u32,
) -> i128 {
    let remaining = (frames_in_period.saturating_sub(sample_offset_frames)) as i128;
    let back_ns = remaining * NANOS_PER_SEC / (sample_rate as i128);
    period_read_ns - back_ns
}

/// What the publisher should do with a drained [`TapEvent`], decided by
/// [`TapSink`]. Keeps the append/cap/auto-disarm policy testable without file
/// I/O or a real clock.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum SinkAction {
    /// Append the event's JSONL line (and count it written).
    Append,
    /// Cap reached — do not append; count as dropped.
    DropAtCap,
    /// Auto-disarm deadline passed — disarm and drop; count as dropped.
    AutoDisarm,
}

/// Publisher-side sink policy for the tap channel.
///
/// Owns the per-arm append count so it can enforce `max_events` and the
/// `auto_disarm` deadline. It is a pure decision function: the caller supplies
/// "now" (monotonic ms) and applies the returned [`SinkAction`] (file append,
/// counter bump, disarm). Rebuilt on each arm via [`TapSink::armed`].
#[derive(Clone, Debug)]
pub struct TapSink {
    max_events: u64,
    auto_disarm_at_ms: i128,
    appended: u64,
}

impl TapSink {
    /// Build a sink for an arm at `arm_now_ms` (monotonic ms) with the config's
    /// cap and auto-disarm horizon.
    pub fn armed(cfg: &TapConfig, arm_now_ms: i128) -> Self {
        let horizon_ms = (cfg.auto_disarm_min as i128) * 60_000;
        Self {
            max_events: cfg.max_events,
            auto_disarm_at_ms: arm_now_ms.saturating_add(horizon_ms),
            appended: 0,
        }
    }

    /// Decide how to handle one drained event given the current monotonic time.
    /// Auto-disarm takes precedence over the cap so a forgotten tap always
    /// stops. On [`SinkAction::Append`] the internal append count advances.
    pub fn decide(&mut self, now_ms: i128) -> SinkAction {
        if now_ms >= self.auto_disarm_at_ms {
            return SinkAction::AutoDisarm;
        }
        if self.appended >= self.max_events {
            return SinkAction::DropAtCap;
        }
        self.appended += 1;
        SinkAction::Append
    }

    /// Whether the auto-disarm deadline has passed at `now_ms` (used by the
    /// publisher to disarm even when no events are arriving).
    #[inline]
    pub fn expired(&self, now_ms: i128) -> bool {
        now_ms >= self.auto_disarm_at_ms
    }
}

/// Cadence the `fanin-tap-writer` thread drains the tap channel at. Short enough
/// that a detection burst can never back up the bounded channel, long enough to
/// stay a negligible idle wakeup when disarmed.
const TAP_DRAIN_INTERVAL_MS: u64 = 100;

/// The single JSONL writer for the relocated tap (C4). Ported verbatim from
/// `jasper-usbsink-audio`'s `TapPublisher`: it is the SOLE writer of the JSONL
/// file (the mixer thread only hands events over the channel). On each new arm
/// generation it re-opens the file with truncate so a fresh arm always starts
/// clean; it then appends one line per drained event until the [`TapSink`]
/// policy says to stop (`max_events` cap or `auto_disarm` deadline). On the poll
/// where the deadline passes it disarms the shared state so a forgotten tap
/// costs nothing.
#[derive(Default)]
pub struct TapWriter {
    generation: u64,
    file: Option<fs::File>,
    sink: Option<TapSink>,
    initialized: bool,
    /// Logged-once-per-arm latch so hitting `max_events` emits a single journal
    /// breadcrumb, not one line per dropped event. Reset on each re-arm.
    cap_hit_logged: bool,
}

impl TapWriter {
    /// Drain and process all currently-queued tap events, plus enforce the
    /// auto-disarm deadline even when the channel is idle. `now_ms` is the
    /// monotonic clock the sink deadline is measured against.
    pub fn poll(
        &mut self,
        receiver: &Receiver<TapEvent>,
        tap: &TapState,
        tap_config: &Mutex<TapConfig>,
        now_ms: i128,
    ) {
        // A fresh arm bumps the generation; (re)open the file with truncate and
        // build a new sink. The channel is drained here so a re-arm starts truly
        // clean — this discards any events still queued from a PRIOR arm (they
        // would otherwise land in the freshly-truncated file), and any events
        // enqueued in the sub-100ms window between this arm and this first
        // post-arm poll (harmless: the operator arms human-seconds before the
        // first real click).
        let generation = tap.generation_acquire();
        if !self.initialized || generation != self.generation {
            self.initialized = true;
            self.generation = generation;
            self.cap_hit_logged = false;
            while receiver.try_recv().is_ok() {}
            if tap.armed() {
                let cfg = tap_config_snapshot(tap_config);
                self.file = open_tap_file(&cfg.path);
                self.sink = Some(TapSink::armed(&cfg, now_ms));
            } else {
                self.file = None;
                self.sink = None;
            }
        }

        // Idle auto-disarm: even with no events, a passed deadline disarms.
        if tap.armed() && self.sink.as_ref().is_some_and(|sink| sink.expired(now_ms)) {
            self.finish_disarm(tap, "auto_disarm_deadline_idle");
        }

        while let Ok(event) = receiver.try_recv() {
            // Events can arrive after disarm (channel not drained yet); ignore.
            if !tap.armed() {
                continue;
            }
            let Some(sink) = self.sink.as_mut() else {
                continue;
            };
            match sink.decide(now_ms) {
                SinkAction::Append => {
                    let wrote = self
                        .file
                        .as_mut()
                        .is_some_and(|file| writeln!(file, "{}", event.to_jsonl()).is_ok());
                    if wrote {
                        tap.note_written();
                    } else {
                        tap.note_dropped();
                    }
                }
                SinkAction::DropAtCap => {
                    tap.note_dropped();
                    if !self.cap_hit_logged {
                        self.cap_hit_logged = true;
                        log::info!(
                            "event=fanin.tap_cap_hit events_written={} events_dropped={}",
                            tap.events_written(),
                            tap.events_dropped(),
                        );
                    }
                }
                SinkAction::AutoDisarm => {
                    tap.note_dropped();
                    self.finish_disarm(tap, "auto_disarm_deadline");
                    break;
                }
            }
        }
    }

    /// Disarm the shared state and release the writer-owned file/sink so a
    /// forgotten or capped tap stops costing anything. `reason` names the
    /// self-healing trigger for the journal breadcrumb (manual `TAP_DISARM`
    /// logs its own line; this covers the automatic paths).
    fn finish_disarm(&mut self, tap: &TapState, reason: &str) {
        log::info!(
            "event=fanin.tap_auto_disarmed reason={} events_written={} events_dropped={}",
            reason,
            tap.events_written(),
            tap.events_dropped(),
        );
        tap.disarm();
        self.file = None;
        self.sink = None;
    }
}

/// Clone the last-armed tap config under its lock (writer thread only — never
/// the mixer thread). A poisoned lock is recovered into rather than crashing.
fn tap_config_snapshot(tap_config: &Mutex<TapConfig>) -> TapConfig {
    match tap_config.lock() {
        Ok(guard) => guard.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

/// Open (create + truncate) the tap JSONL file for the writer to own. Returns
/// `None` on failure — the arm preflight already surfaced any path error to the
/// operator, so here we just skip appends (counting drops).
fn open_tap_file(path: &Path) -> Option<fs::File> {
    if let Some(parent) = path.parent() {
        if fs::create_dir_all(parent).is_err() {
            return None;
        }
    }
    fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
        .ok()
}

/// Run the `fanin-tap-writer` thread loop until `shutdown` is set: drain the tap
/// channel on the `TAP_DRAIN_INTERVAL_MS` cadence (via `recv_timeout`),
/// enforcing the sink cap + auto-disarm. A final drain runs on shutdown. This is
/// the sole JSONL writer (the xrun-writer-thread shape).
pub fn run_tap_writer(
    receiver: Receiver<TapEvent>,
    tap: std::sync::Arc<TapState>,
    tap_config: std::sync::Arc<Mutex<TapConfig>>,
    shutdown: std::sync::Arc<AtomicBool>,
) {
    let mut writer = TapWriter::default();
    let interval = std::time::Duration::from_millis(TAP_DRAIN_INTERVAL_MS);
    while !shutdown.load(Ordering::Relaxed) {
        // `poll` drains ALL queued events via try_recv and enforces the idle
        // auto-disarm deadline; the fixed sleep is the drain cadence (short
        // enough that a burst can't back up the bounded channel, negligible when
        // disarmed). A plain sleep — not recv_timeout — because poll owns the
        // draining and recv_timeout would consume one event out from under it.
        writer.poll(&receiver, &tap, &tap_config, monotonic_millis());
        std::thread::sleep(interval);
    }
    // Final drain so any queued events land before exit.
    writer.poll(&receiver, &tap, &tap_config, monotonic_millis());
}

/// `CLOCK_MONOTONIC` in milliseconds — the writer's auto-disarm clock.
fn monotonic_millis() -> i128 {
    let mut ts = std::mem::MaybeUninit::<libc::timespec>::uninit();
    let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, ts.as_mut_ptr()) };
    if rc != 0 {
        return 0;
    }
    let ts = unsafe { ts.assume_init() };
    ((ts.tv_sec as i128) * 1_000_000_000 + (ts.tv_nsec as i128)) / 1_000_000
}

#[cfg(test)]
mod tests {
    use super::*;

    fn interleaved_impulse(
        period_frames: usize,
        channels: usize,
        at_frame: usize,
        amp: i16,
    ) -> Vec<i16> {
        let mut buf = vec![0i16; period_frames * channels];
        for ch in 0..channels {
            buf[at_frame * channels + ch] = amp;
        }
        buf
    }

    #[test]
    fn detector_fires_on_rising_edge_and_reports_frame_offset() {
        // threshold 0.2 → i16 amp ≈ 0.5 * 32768 = 16384 clears it.
        let mut det = ImpulseDetector::new(0.2, 0.05, 0, 2);
        let period = interleaved_impulse(8, 2, 3, 16384);
        let hit = det.detect(&period, 0).expect("impulse should fire");
        assert_eq!(hit.sample_offset_frames, 3);
        assert!((hit.peak - 0.5).abs() < 1e-6);
    }

    #[test]
    fn detector_does_not_fire_below_threshold() {
        let mut det = ImpulseDetector::new(0.2, 0.05, 0, 2);
        // amp 4096 → 0.125 normalized, below 0.2.
        let period = interleaved_impulse(8, 2, 3, 4096);
        assert!(det.detect(&period, 0).is_none());
    }

    #[test]
    fn hysteresis_latch_blocks_refire_until_signal_decays() {
        let mut det = ImpulseDetector::new(0.2, 0.05, 0, 1);
        // First loud period fires.
        let loud = vec![20000i16; 4];
        assert!(det.detect(&loud, 0).is_some());
        // A still-above-release period (0.16 > release 0.15) must NOT refire.
        let mid = vec![(0.16 * 32768.0) as i16; 4]; // ~5242
        assert!(det.detect(&mid, 4).is_none());
        // Drop below release (0.15) to clear the latch...
        let quiet = vec![(0.10 * 32768.0) as i16; 4];
        assert!(det.detect(&quiet, 8).is_none());
        // ...now a fresh loud period fires again.
        assert!(det.detect(&loud, 12).is_some());
    }

    #[test]
    fn refractory_suppresses_second_click_within_window() {
        // refractory 16 frames.
        let mut det = ImpulseDetector::new(0.2, 0.05, 16, 1);
        let loud = vec![20000i16; 4];
        // Frame 0: fires.
        assert!(det.detect(&loud, 0).is_some());
        // Let the latch release (quiet), still within refractory (frame 4..8).
        let quiet = vec![0i16; 4];
        assert!(det.detect(&quiet, 4).is_none());
        // Frame 8: loud again but only 8 frames since last detect (<16) → no fire.
        assert!(det.detect(&loud, 8).is_none());
        // release the latch again
        assert!(det.detect(&quiet, 12).is_none());
        // Frame 20: 20 frames since last detect (≥16) → fires.
        assert!(det.detect(&loud, 20).is_some());
    }

    #[test]
    fn first_impulse_never_suppressed_by_unset_refractory() {
        // Even with a large refractory, the very first detection fires because
        // there is no prior anchor.
        let mut det = ImpulseDetector::new(0.2, 0.05, 1_000_000, 2);
        let period = interleaved_impulse(8, 2, 0, 30000);
        assert!(det.detect(&period, 0).is_some());
    }

    #[test]
    fn detector_handles_i16_min_without_overflow() {
        let mut det = ImpulseDetector::new(0.2, 0.05, 0, 1);
        let period = vec![i16::MIN; 4];
        let hit = det.detect(&period, 0).expect("i16::MIN is full scale");
        assert!(hit.peak > 0.999);
    }

    #[test]
    fn monotonic_ns_offset_maps_last_sample_to_read_timestamp() {
        // Detection at the LAST frame of the period (offset == frames-1) is one
        // frame before the read timestamp.
        let read_ns = 1_000_000_000i128;
        let ns = detection_monotonic_ns(read_ns, 256, 255, 48_000);
        // remaining = 1 frame → 1 * 1e9/48000 ≈ 20833 ns back.
        assert_eq!(ns, read_ns - (NANOS_PER_SEC / 48_000));
    }

    #[test]
    fn monotonic_ns_offset_maps_first_sample_full_period_back() {
        // Detection at the FIRST frame is a full period behind the read.
        let read_ns = 5_000_000_000i128;
        let ns = detection_monotonic_ns(read_ns, 256, 0, 48_000);
        let expected_back = 256i128 * NANOS_PER_SEC / 48_000;
        assert_eq!(ns, read_ns - expected_back);
    }

    #[test]
    fn tap_event_jsonl_shape_is_stable() {
        let ev = TapEvent {
            monotonic_ns: 123_456_789_012,
            frame_index: 4096,
            ring_fill_frames: 512,
            peak: 0.83,
        };
        assert_eq!(
            ev.to_jsonl(),
            "{\"monotonic_ns\":123456789012,\"frame_index\":4096,\"ring_fill_frames\":512,\"peak\":0.830000}"
        );
    }

    #[test]
    fn tap_event_jsonl_round_trips_through_serde() {
        let ev = TapEvent {
            monotonic_ns: -42,
            frame_index: 7,
            ring_fill_frames: 0,
            peak: 0.5,
        };
        let value: Value = serde_json::from_str(&ev.to_jsonl()).unwrap();
        assert_eq!(value["monotonic_ns"].as_i64(), Some(-42));
        assert_eq!(value["frame_index"].as_u64(), Some(7));
        assert_eq!(value["ring_fill_frames"].as_u64(), Some(0));
        assert!((value["peak"].as_f64().unwrap() - 0.5).abs() < 1e-9);
    }

    #[test]
    fn arm_body_defaults_when_empty_object() {
        let cfg = TapConfig::from_arm_body("{}").unwrap();
        assert_eq!(cfg, TapConfig::default());
    }

    #[test]
    fn arm_body_overrides_all_fields() {
        // Keep this JSON on ONE line: the panic-freedom guard
        // (tests/test_rust_runtime_panic_freedom.py) counts braces with a
        // lexer that does not understand Rust raw-string literals, so a
        // multi-line raw string here leaks a `}` into the #[cfg(test)] span
        // count and misclassifies the rest of the module as runtime code.
        let body = r#"{"threshold":0.4,"hysteresis":0.1,"refractory_ms":300,"max_events":10,"auto_disarm_min":5,"path":"/run/jasper-fanin/x.jsonl"}"#;
        let cfg = TapConfig::from_arm_body(body).unwrap();
        assert!((cfg.threshold - 0.4).abs() < 1e-9);
        assert!((cfg.hysteresis - 0.1).abs() < 1e-9);
        assert_eq!(cfg.refractory_ms, 300);
        assert_eq!(cfg.max_events, 10);
        assert_eq!(cfg.auto_disarm_min, 5);
        assert_eq!(cfg.path, PathBuf::from("/run/jasper-fanin/x.jsonl"));
    }

    #[test]
    fn arm_body_rejects_path_outside_tap_dir() {
        // The arm endpoint truncates + writes its JSONL; a `path` outside
        // /run/jasper-fanin/ would be an arbitrary-file-truncate primitive.
        // These must all be rejected.
        for evil in [
            r#"{"path":"/etc/camilladsp/active.yml"}"#,
            r#"{"path":"/var/lib/jasper/build.txt"}"#,
            r#"{"path":"/run/jasper-fanin/../etc/passwd"}"#,
            r#"{"path":"/run/jasper-fanin/nested/deep.jsonl"}"#, // no subdirs
            r#"{"path":"/run/jasper-fanin-evil/x.jsonl"}"#,      // prefix trick
            r#"{"path":"relative.jsonl"}"#,                      // not absolute
            r#"{"path":"/run/jasper-fanin"}"#,                   // the dir itself
        ] {
            assert!(
                TapConfig::from_arm_body(evil).is_none(),
                "should reject arm path: {evil}"
            );
        }
    }

    #[test]
    fn arm_body_accepts_any_filename_within_tap_dir() {
        let cfg = TapConfig::from_arm_body(r#"{"path":"/run/jasper-fanin/run-7.jsonl"}"#)
            .expect("a plain filename in the tap dir is allowed");
        assert_eq!(cfg.path, PathBuf::from("/run/jasper-fanin/run-7.jsonl"));
    }

    #[test]
    fn arm_body_rejects_reserved_daemon_basenames() {
        // The tap must not be armable at fan-in's own files (truncating
        // control.sock / tts.sock).
        for evil in [
            r#"{"path":"/run/jasper-fanin/control.sock"}"#,
            r#"{"path":"/run/jasper-fanin/tts.sock"}"#,
        ] {
            assert!(
                TapConfig::from_arm_body(evil).is_none(),
                "should reject reserved basename: {evil}"
            );
            let name = serde_json::from_str::<Value>(evil).unwrap()["path"]
                .as_str()
                .unwrap()
                .to_string();
            assert!(
                !path_is_allowed(Path::new(&name)),
                "path_is_allowed should reject: {name}"
            );
        }
    }

    #[test]
    fn arm_body_rejects_resource_unbounded_knobs() {
        // An unauthenticated arm request must not be able to install a
        // resource-unbounded tap that could grow tmpfs until the memcg OOM-kills
        // the audio daemon, or a forgotten-forever tap.
        assert!(TapConfig::from_arm_body(r#"{"max_events":1000000000000000000}"#).is_none());
        assert!(TapConfig::from_arm_body(&format!(
            r#"{{"max_events":{}}}"#,
            MAX_EVENTS_CEILING + 1
        ))
        .is_none());
        assert!(TapConfig::from_arm_body(&format!(
            r#"{{"auto_disarm_min":{}}}"#,
            AUTO_DISARM_MIN_CEILING + 1
        ))
        .is_none());
        // Exactly at the ceiling is accepted (inclusive bound).
        assert_eq!(
            TapConfig::from_arm_body(&format!(r#"{{"max_events":{MAX_EVENTS_CEILING}}}"#))
                .unwrap()
                .max_events,
            MAX_EVENTS_CEILING
        );
        assert_eq!(
            TapConfig::from_arm_body(&format!(
                r#"{{"auto_disarm_min":{AUTO_DISARM_MIN_CEILING}}}"#
            ))
            .unwrap()
            .auto_disarm_min,
            AUTO_DISARM_MIN_CEILING
        );
    }

    #[test]
    fn path_is_allowed_matches_default_tap_path() {
        assert!(path_is_allowed(Path::new(DEFAULT_TAP_PATH)));
    }

    #[test]
    fn arm_body_allows_zero_hysteresis_but_rejects_bad_knobs() {
        assert!(TapConfig::from_arm_body(r#"{"hysteresis":0}"#).is_some());
        // negative / zero / NaN-ish values for positive-only knobs are rejected
        assert!(TapConfig::from_arm_body(r#"{"threshold":0}"#).is_none());
        assert!(TapConfig::from_arm_body(r#"{"threshold":-0.1}"#).is_none());
        assert!(TapConfig::from_arm_body(r#"{"hysteresis":-0.1}"#).is_none());
        assert!(TapConfig::from_arm_body(r#"{"refractory_ms":0}"#).is_none());
        assert!(TapConfig::from_arm_body(r#"{"max_events":0}"#).is_none());
        assert!(TapConfig::from_arm_body(r#"{"auto_disarm_min":0}"#).is_none());
        assert!(TapConfig::from_arm_body(r#"{"path":""}"#).is_none());
    }

    #[test]
    fn arm_body_rejects_non_object() {
        assert!(TapConfig::from_arm_body("[]").is_none());
        assert!(TapConfig::from_arm_body("42").is_none());
        assert!(TapConfig::from_arm_body("not json").is_none());
    }

    #[test]
    fn arm_body_accepts_integral_float_u64_knobs() {
        // A client that serializes an integer-valued knob as a JSON float
        // (`300.0` from a `type=float` CLI arg) must still arm — `as_u64()`
        // returns None for a float, so without the integral-float fallback in
        // `positive_u64` every such request 400s for the whole measurement
        // window. This is the Rust half of the B1 cross-language fix.
        let body = r#"{"refractory_ms":300.0,"max_events":10.0,"auto_disarm_min":5.0}"#;
        let cfg = TapConfig::from_arm_body(body).expect("integral floats must arm");
        assert_eq!(cfg.refractory_ms, 300);
        assert_eq!(cfg.max_events, 10);
        assert_eq!(cfg.auto_disarm_min, 5);
    }

    #[test]
    fn arm_body_rejects_non_integral_float_u64_knobs() {
        // A fractional value for an integer-only knob is a caller bug, not a
        // roundable input at this layer — reject it rather than silently
        // truncate. (The Python side rounds before it ever reaches the wire.)
        assert!(TapConfig::from_arm_body(r#"{"refractory_ms":300.5}"#).is_none());
        assert!(TapConfig::from_arm_body(r#"{"max_events":10.5}"#).is_none());
        // A ceiling-capped knob still enforces its ceiling on an integral float.
        assert!(TapConfig::from_arm_body(&format!(
            r#"{{"max_events":{}.0}}"#,
            MAX_EVENTS_CEILING + 1
        ))
        .is_none());
    }

    #[test]
    fn tap_state_arm_publishes_knobs_and_resets_counters() {
        let state = TapState::default();
        state.note_written();
        state.note_dropped();
        assert_eq!(state.events_written(), 1);
        assert_eq!(state.events_dropped(), 1);

        let cfg = TapConfig {
            threshold: 0.3,
            hysteresis: 0.02,
            refractory_ms: 250,
            auto_disarm_min: 2,
            ..TapConfig::default()
        };
        let gen0 = state.generation_acquire();
        state.arm(&cfg, 48_000, 1_000_000);

        assert!(state.armed());
        assert_eq!(state.generation_acquire(), gen0 + 1);
        assert_eq!(state.events_written(), 0);
        assert_eq!(state.events_dropped(), 0);
        let (threshold, hysteresis, refractory_frames) = state.detector_knobs();
        assert!((threshold - 0.3).abs() < 1e-3);
        assert!((hysteresis - 0.02).abs() < 1e-3);
        // 250 ms @ 48 kHz = 12000 frames.
        assert_eq!(refractory_frames, 12_000);
        // 2 min horizon → deadline = arm epoch + 120_000 ms.
        assert_eq!(state.auto_disarm_at_epoch_ms(), 1_120_000);
    }

    #[test]
    fn tap_state_disarm_is_idempotent_and_keeps_counters() {
        let state = TapState::default();
        state.arm(&TapConfig::default(), 48_000, 0);
        state.note_written();
        assert_ne!(state.auto_disarm_at_epoch_ms(), 0);
        state.disarm();
        assert!(!state.armed());
        assert_eq!(state.auto_disarm_at_epoch_ms(), 0);
        state.disarm();
        assert!(!state.armed());
        assert_eq!(state.events_written(), 1);
    }

    #[test]
    fn status_fragment_reflects_armed_state_and_config() {
        let state = TapState::default();
        // Disarmed default.
        let cfg = TapConfig::default();
        let disarmed: Value = serde_json::from_str(&state.status_fragment(&cfg)).unwrap();
        assert_eq!(disarmed["armed"].as_bool(), Some(false));
        assert_eq!(disarmed["events_written"].as_u64(), Some(0));
        assert_eq!(disarmed["auto_disarm_at_epoch_ms"].as_u64(), Some(0));
        assert_eq!(disarmed["path"].as_str(), Some(DEFAULT_TAP_PATH));

        // Armed with an override config.
        let cfg = TapConfig {
            threshold: 0.4,
            refractory_ms: 300,
            max_events: 10,
            auto_disarm_min: 1,
            path: PathBuf::from("/run/jasper-fanin/x.jsonl"),
            ..TapConfig::default()
        };
        state.arm(&cfg, 48_000, 5_000);
        state.note_written();
        let armed: Value = serde_json::from_str(&state.status_fragment(&cfg)).unwrap();
        assert_eq!(armed["armed"].as_bool(), Some(true));
        assert_eq!(armed["events_written"].as_u64(), Some(1));
        assert!((armed["threshold"].as_f64().unwrap() - 0.4).abs() < 1e-3);
        assert_eq!(armed["refractory_ms"].as_u64(), Some(300));
        assert_eq!(armed["max_events"].as_u64(), Some(10));
        assert_eq!(armed["auto_disarm_at_epoch_ms"].as_u64(), Some(65_000));
        assert_eq!(armed["path"].as_str(), Some("/run/jasper-fanin/x.jsonl"));
    }

    #[test]
    fn status_fragment_escapes_untrusted_path() {
        let state = TapState::default();
        let path = "/tmp/a\"b\\c\nunit\u{0001}delete\u{007f}next-line\u{0085}.jsonl";
        let cfg = TapConfig {
            path: PathBuf::from(path),
            ..TapConfig::default()
        };
        let fragment = state.status_fragment(&cfg);
        // Must still parse as valid JSON with hostile/control content intact.
        let value: Value = serde_json::from_str(&fragment).unwrap();
        assert_eq!(value["path"].as_str(), Some(path));
    }

    #[test]
    fn milli_fixed_point_round_trips_within_resolution() {
        for v in [0.0, 0.05, 0.2, 0.333, 0.95, 1.0] {
            let back = milli_to_f64(f64_to_milli(v));
            assert!((back - v).abs() <= 0.001, "v={v} back={back}");
        }
    }

    #[test]
    fn sink_appends_until_cap_then_drops() {
        let cfg = TapConfig {
            max_events: 2,
            auto_disarm_min: 45,
            ..TapConfig::default()
        };
        let mut sink = TapSink::armed(&cfg, 0);
        assert_eq!(sink.decide(1), SinkAction::Append);
        assert_eq!(sink.decide(2), SinkAction::Append);
        assert_eq!(sink.decide(3), SinkAction::DropAtCap);
        assert_eq!(sink.decide(4), SinkAction::DropAtCap);
    }

    #[test]
    fn sink_auto_disarms_past_deadline_before_cap() {
        let cfg = TapConfig {
            max_events: 1000,
            auto_disarm_min: 1, // 60_000 ms
            ..TapConfig::default()
        };
        let mut sink = TapSink::armed(&cfg, 0);
        // 1 min horizon → deadline at 60_000 ms.
        assert!(!sink.expired(59_999));
        assert!(sink.expired(60_000));
        assert_eq!(sink.decide(59_999), SinkAction::Append);
        assert_eq!(sink.decide(60_000), SinkAction::AutoDisarm);
        // Still auto-disarm afterwards even though cap not hit.
        assert_eq!(sink.decide(120_000), SinkAction::AutoDisarm);
    }

    #[test]
    fn sink_auto_disarm_wins_when_both_cap_and_deadline_reached() {
        let cfg = TapConfig {
            max_events: 1,
            auto_disarm_min: 1,
            ..TapConfig::default()
        };
        let mut sink = TapSink::armed(&cfg, 0);
        assert_eq!(sink.decide(10), SinkAction::Append); // hits cap
                                                         // Past deadline AND over cap → auto-disarm takes precedence.
        assert_eq!(sink.decide(60_000), SinkAction::AutoDisarm);
    }
}
