// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Configuration loaded from `JASPER_FANIN_*` environment variables.
//!
//! This module owns the defaults; every default here must agree with its
//! `.env.example` entry.
//!
//! Operator overrides go in `/etc/jasper/jasper.env` (system-wide);
//! `/var/lib/jasper/fanin.env` is single-writer, owned by
//! `jasper.fanin.coupling_reconcile`.

use anyhow::Result;
use jasper_env::{env_f32, env_parse, env_str};

use crate::loudness::AssistantLoudnessConfig;

/// The SHM ring's pinned slot size in frames (Ring A), re-exported from the
/// crate that owns the ring geometry so fan-in and outputd read one constant.
/// Matches the outputd DAC-period contract and the ring header geometry; fan-in
/// publishes `period_frames / RING_SLOT_FRAMES` slots per mixer step, and the
/// `period_frames % RING_SLOT_FRAMES == 0` config guard is the drift catch.
pub use jasper_ring::RING_SLOT_FRAMES;

/// The ring's `n_slots` bounds (Ring A). Mirrors `jasper_ring::MIN_N_SLOTS` /
/// `MAX_N_SLOTS`; the ring header validates the same range at attach. A present
/// out-of-range value FAILS LOUD here (`Config::from_env` bails) — Python's
/// `fanin_coupling.resolve_ring_slots` raises on the same range, so the two
/// normalizers agree on the drift axis (unset => default 2; out-of-range =>
/// error on BOTH sides, never a silent clamp).
pub const RING_SLOTS_MIN: u32 = 2;
pub const RING_SLOTS_MAX: u32 = 16;

/// The directory every SHM slot ring lives in. Mirrors
/// `jasper.ring_assets.RING_SHM_DIR` by VALUE (the tmpfiles.d entry
/// `deploy/tmpfiles/jts-ring.conf` is the authority that creates it);
/// `tests/test_renderer_ring_lanes.py` pins the two spellings against each other.
///
/// Ring A (`program.ring`) and Ring B (`content.ring`) name their own files
/// through `JASPER_FANIN_RING_PATH` / outputd's default. The RENDERER-ingress
/// rings do NOT get a per-ring env key: their path is DERIVED from the lane
/// label by [`renderer_ring_path`], because a lane's ring is an attribute of the
/// lane, and a second env key naming it could disagree with the label that
/// selected it.
pub const RING_SHM_DIR: &str = "/dev/shm/jts-ring";

/// Filename prefix for a RENDERER-ingress ring, so `ls /dev/shm/jts-ring/` tells
/// an operator which files are renderer lanes and which are the program/content
/// hops. Mirrored in Python by `jasper.renderer_lanes.RENDERER_RING_PREFIX`.
pub const RENDERER_RING_PREFIX: &str = "lane-";

/// Label of the measurement / diagnostic injection lane — the one lane that
/// carries stimuli rather than program.
///
/// Two behaviours key off this identity and must agree about it: the lane is
/// always mixed regardless of selection, so diagnostics keep working when the
/// household has pinned a source ([`crate::mixer`]'s selection gate), and its
/// stimuli are never onset-shaped, because the measurement loop deconvolves
/// against the signal it believes it played (`mixer::lane_fade`).
pub const MEASUREMENT_LANE: &str = "correction";

/// The ring file a renderer-ingress lane reads, derived from the fan-in lane
/// LABEL — the one identity fan-in already has for that lane. This is the whole
/// path rule; there is no env override, so the reader's path and the conf.d
/// writer's path cannot drift through a second knob (they drift only if the two
/// language mirrors disagree, which the contract test catches).
pub fn renderer_ring_path(label: &str) -> String {
    format!("{RING_SHM_DIR}/{RENDERER_RING_PREFIX}{label}.ring")
}

/// Whether a lane gets a `LaneResampler`, from the four loose values rather than
/// a built `Config`.
///
/// A free function so `Config::from_env` can ask the question BEFORE the
/// `Config` is built: it must refuse the resampler+ring combination at config
/// time, and re-spelling the predicate there would give the refusal and the
/// construction two rules that could drift apart. `Config::lane_wants_resampler`
/// is the one caller that has a `Config`; both go through here.
pub fn lane_wants_resampler_for(
    label: &str,
    resampler_lane_label: &str,
    input_resampler_enabled: bool,
    usb_direct_enabled: bool,
) -> bool {
    (input_resampler_enabled || usb_direct_enabled) && label == resampler_lane_label
}

/// The slot count a renderer-ingress ring is built with, DERIVED from the lane
/// buffer geometry fan-in already owns: `input_buffer_frames / period_frames`,
/// with the ring's slot equal to one fan-in period.
///
/// Derived rather than a constant because the aloop lane this replaces gives the
/// renderer exactly `input_buffer_frames` of cushion (fan-in opens the capture
/// side with that buffer and snd-aloop locks the pair to it). A renderer is a
/// network player whose ALSA write blocks when its buffer is full, so that
/// cushion is how much network jitter it absorbs before it underruns; deriving
/// the ring's depth from the same number leaves the flow-control regime
/// unchanged.
///
/// `None` when the geometry cannot be expressed as whole slots inside the ring
/// header's `n_slots` range — the caller FAILS LOUD rather than handing the
/// renderer a different cushion than the aloop lane gave it.
pub fn renderer_ring_slots(input_buffer_frames: u32, period_frames: u32) -> Option<u32> {
    if period_frames == 0 || input_buffer_frames % period_frames != 0 {
        return None;
    }
    let slots = input_buffer_frames / period_frames;
    if !(RING_SLOTS_MIN..=RING_SLOTS_MAX).contains(&slots) {
        return None;
    }
    Some(slots)
}

/// The frames the post-lock cushion decay floor keeps ABOVE the base resampler
/// target — a small working cushion the outer DLL always has to steer within.
/// The decay never descends below `input_resampler_target_frames + this` (the
/// hard MINIMUM the arm-time guard enforces). 32 frames ≈ 0.67 ms at 48 kHz:
/// enough for the DLL's ±adjust authority to hold the fill without underrunning,
/// but the tightest safe reclaim of the standing cushion.
pub const CUSHION_DECAY_FLOOR_MARGIN_FRAMES: u32 = 32;

/// The SHIPPED decay-floor default: the hardware-VALIDATED floor from the
/// jts.local combo-armed gate (Apple USB-C dongle, target 512 / period 256 /
/// ±500 ppm), NOT a bare `target + margin`. The tighter derived minimum
/// `max(target, minimum_safe_fill) + 32` (= 544 at the default geometry) stays
/// the HARD floor the armed guard rejects below; this constant is only what an
/// out-of-box combo box descends TO when it sets no explicit
/// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES`. Clamped into
/// `[derived_min, ceiling]` at parse time so a small-target geometry (ceiling <
/// 576) still constructs. MUST agree with `.env.example`'s documented default.
pub const DEFAULT_CUSHION_DECAY_FLOOR_FRAMES: u32 = 576;

/// The jitter headroom the STATIC held target (`target + warm-up cushion`) must
/// keep above the post-render underfill-unlock threshold. Same 32-frame DLL
/// working margin the decay floor uses (they guard the same physical floor from
/// two directions — decay from above at steady state, this from the static knobs
/// at config time), so an operator has ONE number for "the safe headroom above
/// the physical floor." After rendering one period the cursor-relative fill
/// drops by ~`period` frames, so the held target must sit at least
/// `period + this` above `minimum_safe_fill_frames` or ordinary USB delivery
/// coalescing (arrivals clustering below the deficit in one render interval)
/// underfill-unlocks the lane every burst — churn-by-construction. See
/// `Config::from_env`'s static-cushion validation.
pub const STATIC_CUSHION_JITTER_MARGIN_FRAMES: u32 = 32;

#[derive(Debug, Clone)]
pub struct Config {
    /// Per-input PCMs — the capture side of each renderer or internal
    /// test lane's dedicated snd-aloop substream. Order matters: the STATUS
    /// endpoint reports inputs in this order, and `input_renderers`
    /// labels align positionally.
    ///
    /// Pipe-delimited in `JASPER_FANIN_INPUT_PCMS` (see [`env_list`] for why
    /// the delimiter is a pipe).
    pub input_pcms: Vec<String>,

    /// Human-readable labels for each input PCM, in the same order. Surfaced via
    /// the STATUS endpoint and the structured `event=` log lines; no effect on
    /// audio. Pipe-delimited in the env var to match `input_pcms`.
    pub input_renderers: Vec<String>,

    /// PCM sample rate. All inputs and the output use this rate; the
    /// per-renderer plug wrappers in `/etc/asound.conf` convert each renderer's
    /// native rate to 48 kHz before the substream.
    pub sample_rate: u32,

    /// ALSA period size in frames — the cadence of mixer-loop wakeups. Default
    /// 256 frames ≈ 5.3 ms at 48 kHz, tight enough to keep the watchdog
    /// sentinel fresh on every wake.
    pub period_frames: u32,

    /// ALSA input buffer size in frames — the burst-absorption margin for each
    /// renderer lane. Default 4096 ≈ 85 ms, enough to absorb the observed WiFi
    /// A-MPDU AirPlay burst gaps without input xruns.
    pub input_buffer_frames: u32,

    /// UDS socket exposing the STATUS command, queried by jasper-control's
    /// `/state` aggregator and by jasper-doctor. Under `/run` so it is tmpfs and
    /// recreated on each daemon start.
    pub control_socket_path: String,

    /// Append-only xrun event log, persisted across reboots. Ring-truncated at
    /// ~10 KB.
    pub xrun_log_path: String,

    /// Outputd-compatible TTS socket. Python's TTS transport points here so
    /// speech/cues enter before CamillaDSP crossover/protection. `disabled` is a
    /// rollback/lab value.
    pub tts_socket_path: Option<String>,

    /// Bounded pre-DSP TTS queue budget. Chunks over this limit are DROPPED —
    /// an unbounded queue would add seconds of stale assistant speech.
    pub tts_max_pending_frames: u64,

    /// Program-lane attenuation while queued TTS/cue audio is being mixed. This
    /// ducks renderer lanes only; TTS stays unattenuated before CamillaDSP
    /// crossover/protection.
    pub tts_program_duck_db: f32,
    pub tts_cue_duck_db: f32,

    /// Attack and release times, in ms, for the program-lane duck. The mixer
    /// glides the applied gain over these times per sample rather than stepping
    /// it at a period boundary, so a ~25 dB duck around a short earcon does not
    /// click or pump. Attack is short so speech/cues are not masked; release is
    /// longer so the music swells back gently.
    pub tts_duck_attack_ms: u32,
    pub tts_duck_release_ms: u32,

    /// Assistant loudness policy for the pre-DSP TTS socket.
    pub assistant_loudness: AssistantLoudnessConfig,

    /// Versioned last-achieved assistant loudness. Separate from the
    /// canonical speaker-volume record because fan-in is the sole writer.
    pub assistant_reference_path: String,

    /// The SPSC SHM ring file fan-in writes toward CamillaDSP (Ring A). Default
    /// `/dev/shm/jts-ring/program.ring` (the owned tmpfs root, so a
    /// magic-invalid file is reclaimable). Env:
    /// `JASPER_FANIN_RING_PATH`. Python `fanin_coupling.resolve_ring_path` uses
    /// the same default.
    pub ring_path: String,

    /// The ring's slot count (Ring A). Buffer depth is
    /// `ring_slots * RING_SLOT_FRAMES` — the only latency axis, since the slot
    /// itself is pinned at 128 by the outputd DAC-period contract. Default 2
    /// (256 frames ≈ 5.3 ms). A present value outside
    /// [`RING_SLOTS_MIN`]..=[`RING_SLOTS_MAX`] FAILS LOUD in `Config::from_env`
    /// — never clamped; Python `fanin_coupling.resolve_ring_slots` defaults and
    /// raises identically, so the two normalizers agree. The `n_slots` <->
    /// `JASPER_FANIN_RING_SLOTS` pairing is the drift axis with the ioplug
    /// conf.d geometry; the ring header's attach-time validation is the runtime
    /// backstop. Env: `JASPER_FANIN_RING_SLOTS`.
    pub ring_slots: u32,

    /// The sample format Ring A's wire carries. Default
    /// [`RingWireFormat::S32Le`] — narrow is a width regression on the hop the
    /// ring replaces, so the wide wire is what an undeclared box gets and
    /// `S16_LE` is an operator's rollback pin. Env:
    /// `JASPER_FANIN_RING_WIRE_FORMAT` (`S16_LE` | `S32_LE`); a present but
    /// unrecognized value fails loud in `Config::from_env` as a config-class
    /// fault (exit 78, the unit parks) rather than resolving to a default the
    /// operator did not ask for.
    ///
    /// Ring A's wire is declared independently by each end that touches it —
    /// fan-in through this key, the ioplug through its conf.d `format` field,
    /// and the emitted CamillaDSP capture stanza. Attach compares
    /// `sample_format` field-by-field, so a value here that the other ends do
    /// not also declare makes the ring open fail loudly rather than misreading
    /// bytes.
    pub ring_wire_format: RingWireFormat,

    /// DEFAULT-OFF: arm the per-input adaptive resampler on the clock-crossing
    /// (USB) lane (`src/lane_resampler.rs`). When off, the per-lane read path is
    /// the strict one-period read + catch-up drain. When on, the lane named by
    /// `input_resampler_lane_label` is DLL-steered to the DAC clock — drop-free
    /// reconciliation in place of the catch-up sawtooth on that lane. Env:
    /// `JASPER_FANIN_INPUT_RESAMPLER` (only the literal `enabled` arms it).
    pub input_resampler_enabled: bool,

    /// The lane LABEL (matched against `input_renderers`) the input resampler
    /// arms on when enabled. Only ONE lane crosses a foreign clock (USB), so
    /// this is a single label, not a set. A label with no matching input is a
    /// no-op (logged once). Env: `JASPER_FANIN_INPUT_RESAMPLER_LANE`
    /// (default `usbsink`).
    pub input_resampler_lane_label: String,

    /// Target buffered frames the input resampler holds the armed lane's ring
    /// at — the small fixed fill that replaces the catch-up sawtooth. Smaller =
    /// lower latency but less jitter headroom before an underfill→silence.
    /// Default 512 frames (~10.7 ms at 48 kHz, two periods at 256). Env:
    /// `JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES`.
    pub input_resampler_target_frames: u32,

    /// Output ppm clamp on the input resampler's pitch warp — the hard safety
    /// bound on how far the host↔DAC rate gap may ever be corrected. Env:
    /// `JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM`.
    pub input_resampler_max_adjust_ppm: u32,

    /// Warm-up cushion: extra frames the input resampler adds to the DLL hold
    /// target for the armed lane. The cushion is HELD rather than drained back
    /// to the base target, so the steady setpoint is
    /// `input_resampler_target_frames + cushion`; hardware showed that draining
    /// it — intentional startup over-consumption — can lock/unlock-thrash on the
    /// real bursty USB feed. Default 2048 frames ≈ 42.7 ms at 48 kHz. Env:
    /// `JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES`.
    pub input_resampler_warmup_cushion_frames: u32,

    /// Input-ring capacity (frames) for the input resampler's burst buffer — the
    /// headroom ABOVE the target setpoint that absorbs input bursts before they
    /// overflow. Distinct from `input_resampler_target_frames` (the latency
    /// setpoint): raising THIS does not add latency, it only adds burst
    /// absorption. `0` (the default) means "derive 2x the lane's ALSA input
    /// buffer" (`input_buffer_frames * 2`), floored to the resampler's
    /// structural minimum; a non-zero value pins an explicit capacity. Env:
    /// `JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES`.
    pub input_resampler_ring_frames: u32,

    /// DEFAULT-OFF post-lock cushion DECAY. When `true`, once the armed
    /// resampler lane is locked AND its outer host-clock DLL is `l0_locked` AND
    /// stable, the held target decays from the acquisition ceiling
    /// (`target + warmup cushion`) toward `input_resampler_cushion_decay_floor_frames`
    /// — reclaiming the standing resampler fill (~10 ms) that only the cold-start
    /// burst needs. Snaps back to the ceiling on any unlock / DLL demotion /
    /// stream stop. Fail-safe: only the exact literal `enabled` (case-insensitive)
    /// arms it. Env: `JASPER_FANIN_RESAMPLER_CUSHION_DECAY`. Meaningful only with
    /// the host-clock DLL armed (decay requires `l0_locked`).
    pub input_resampler_cushion_decay_enabled: bool,
    /// The total held-target floor (frames) the decay descends to. Must be at
    /// least `max(target, minimum_safe_fill_frames)` plus
    /// [`CUSHION_DECAY_FLOOR_MARGIN_FRAMES`], and at most the acquisition
    /// ceiling (`target + warmup cushion`); `from_env` validates both fail-loud
    /// once the decay is armed. Defaults to
    /// [`DEFAULT_CUSHION_DECAY_FLOOR_FRAMES`] clamped into that range. Env:
    /// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES`.
    pub input_resampler_cushion_decay_floor_frames: u32,
    /// Frames dropped from the held target per decay step. Fail-loud range
    /// `1..=64`; default 6. The EXECUTED demand is the periods-space arithmetic
    /// (`DecayParams::step_demand_ppm`; ms-space `step × 20833 / interval_ms`
    /// approximates it): default 6 / 1000 ms at 48 kHz / 256 ⇒ ~125.3 ppm,
    /// ~4x under the inner resampler's ±500 ppm authority
    /// (`input_resampler_max_adjust_ppm`) — the ONLY budget the demand
    /// consumes, because the host-clock observable subtracts the published
    /// demand (#3466 — rationale at `host_clock::build_obs`). An ARMED pair
    /// must pass the demand-vs-authority margin check in `from_env`
    /// (demand × 2 ≤ authority). Env:
    /// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_STEP_FRAMES`.
    pub input_resampler_cushion_decay_step_frames: u32,
    /// Wall interval between decay steps, in ms (converted to render periods by
    /// the lane). Fail-loud range `250..=10000`; default 1000. Env:
    /// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_INTERVAL_MS`.
    pub input_resampler_cushion_decay_interval_ms: u32,

    /// DEFAULT-OFF one-shot AUTO-TRIM. When `true`, the mixer schedules ONE
    /// `TRIM` per armed resampler lane a couple of seconds after that lane goes
    /// active, dropping the accumulated standing head-start (the cursor-relative
    /// fill excess above the held target). Manual `TRIM` over the control socket
    /// works regardless of this flag. Env: `JASPER_FANIN_AUTO_TRIM` (only the
    /// literal `enabled` arms it).
    pub auto_trim_enabled: bool,

    /// DEFAULT-OFF USB DIRECT capture. When `true`, the lane labelled
    /// `input_resampler_lane_label` (the usbsink lane) does NOT read its
    /// snd-aloop substream; the mixer opens `usb_direct_device`
    /// (`hw:UAC2Gadget`) as an S32_LE capture and feeds the SAME
    /// `LaneResampler`. A wide-wire box hands the resampler the gadget's `i32`
    /// untouched; only a box an operator has PINNED narrow (see
    /// [`Config::ring_wire_format`], whose default is `S32Le`) narrows to S16
    /// first. Either way this deletes the usbsink bridge hop and the aloop cable
    /// — ~25 ms measured — from the USB path. Direct mode IMPLIES a resampler on
    /// that lane regardless of `input_resampler_enabled` (see
    /// [`Config::lane_wants_resampler`]). Env: `JASPER_FANIN_USB_DIRECT` (only
    /// the literal `enabled` arms it).
    pub usb_direct_enabled: bool,

    /// The ALSA capture device the USB DIRECT lane opens when `usb_direct_enabled`.
    /// Default `hw:UAC2Gadget` (the UAC2 gadget card fan-in owns while the USB
    /// source is armed). Unused when direct is off. Env:
    /// `JASPER_FANIN_USB_DIRECT_DEVICE`.
    pub usb_direct_device: String,

    /// The gadget capture OPEN period (frames) the USB DIRECT lane negotiates.
    /// Default 256, the hardware-validated direct-capture envelope; fail-loud
    /// range 32..=1024. Shrinking it (e.g. 64) exposes ready frames sooner if the
    /// gadget's readable `avail` advances in period-sized steps. The capture
    /// BUFFER stays DEEP regardless (`mixer::resolve_direct_buffer_frames`:
    /// ≥ 3 periods AND ≥ 768 frames), so a small period rides a deep buffer
    /// rather than the refuted shallow 2-period URB headroom. Unused when direct
    /// is off. Env: `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES`.
    pub usb_direct_period_frames: u32,

    /// DEFAULT-EMPTY renderer-ingress lane set: the fan-in LABELS whose audio
    /// arrives over a per-renderer SHM slot ring instead of that lane's
    /// snd-aloop capture substream.
    ///
    /// A label listed here makes that lane open [`renderer_ring_path`] with the
    /// [`renderer_ring_slots`] geometry and IGNORE its `input_pcms` entry — the
    /// aloop substream is never opened on a ring lane, exactly as the USB DIRECT
    /// lane never opens its own (`Input::pcm` is `None` on both).
    ///
    /// Membership is the ONLY input: fan-in does not consult the CamillaDSP
    /// coupling, because a renderer ring and the fan-in -> CamillaDSP hop are
    /// independent transports (a loopback-coupled box can ring-ingress a renderer
    /// and vice versa). The policy that decides WHICH labels land here lives in
    /// one place on the Python side (`jasper.renderer_lanes`), which is also the
    /// single writer of the env file both fan-in and the renderer read.
    /// Env: `JASPER_FANIN_RENDERER_RING_LANES` (comma-separated labels).
    pub renderer_ring_lanes: Vec<String>,

    /// DEFAULT-OFF combo-mode host-slaved USB clock (`JASPER_FANIN_HOST_CLOCK`).
    /// When `true` AND `usb_direct_enabled`, a dedicated `fanin-host-clock`
    /// thread steers the gadget's `Capture Pitch 1000000` ctl so the host tracks
    /// the DAC clock through the shared [`jasper_host_clock`] ladder. Fail-safe:
    /// only the exact literal
    /// `enabled` (case-insensitive) arms it; any other non-empty value warns
    /// once (`event=fanin.host_clock_config_ignored`) and stays OFF. Meaningful
    /// ONLY with `usb_direct_enabled`: fan-in must own the gadget capture to own
    /// the pitch ctl. `enabled` + direct-off resolves to a fully-inert warn (no
    /// ctl writes ever). Env:
    /// `JASPER_FANIN_HOST_CLOCK` (`enabled` to arm).
    pub host_clock_enabled: bool,

    /// The commanded pitch step (in ppm) for the host-clock per-session
    /// compliance probe. Default 300; fail-fast range 200..=800 — the floor
    /// clears the ~163 ppm Windows usbaudio2.sys reaction deadband (a probe at
    /// or below it would falsely fail every session), the ceiling keeps the
    /// probe inside the ±1000 ppm validity window. Env:
    /// `JASPER_FANIN_HOST_CLOCK_PROBE_PPM`. Unused when host-clock is off.
    pub host_clock_probe_ppm: u32,
}

impl Config {
    /// Whether the lane labelled `label` should be constructed with a
    /// `LaneResampler`. True when EITHER the DEFAULT-OFF input resampler is
    /// enabled OR USB direct capture is enabled — both steer the same lane
    /// (`input_resampler_lane_label`) to the DAC clock, and direct capture has
    /// no aloop catch-up fallback to reconcile the host↔DAC rate gap, so it
    /// MUST own a resampler. A label that doesn't match the resampler lane never
    /// gets one.
    pub fn lane_wants_resampler(&self, label: &str) -> bool {
        lane_wants_resampler_for(
            label,
            &self.input_resampler_lane_label,
            self.input_resampler_enabled,
            self.usb_direct_enabled,
        )
    }

    /// Whether the lane labelled `label` ingresses over a per-renderer SHM slot
    /// ring instead of its snd-aloop capture substream. Membership in
    /// [`Config::renderer_ring_lanes`] is the WHOLE rule on this side — see that
    /// field's docs for why the coupling is deliberately not consulted.
    pub fn lane_is_renderer_ring(&self, label: &str) -> bool {
        self.renderer_ring_lanes.iter().any(|l| l == label)
    }

    /// The ring geometry a renderer-ingress lane attaches with: one slot per
    /// fan-in period, depth derived from the aloop cushion this lane replaces.
    /// `None` when the geometry is not expressible in whole slots —
    /// `Config::from_env` already refuses that combination when any lane is
    /// armed, so a live ring lane always resolves `Some`.
    pub fn renderer_ring_slots(&self) -> Option<u32> {
        renderer_ring_slots(self.input_buffer_frames, self.period_frames)
    }

    /// Whether the `fanin-host-clock` servo thread is CONFIGURED to run — the
    /// combo-mode host-slaved USB clock. True only when the host-clock DLL is
    /// armed AND USB direct capture is on, because fan-in must own the gadget
    /// capture to own the pitch ctl (`enabled` + direct-off is a fully-inert
    /// warn because no process owns direct gadget clock control). This is the
    /// SINGLE source of truth for that coupling: `main` derives the servo-spawn
    /// gate (`host_clock_enabled_effective`) from it. The runtime servo ALSO
    /// needs a live direct-lane resampler (signals present); this is the
    /// config-level predicate.
    pub fn host_clock_servo_armed(&self) -> bool {
        self.host_clock_enabled && self.usb_direct_enabled
    }
}

/// The sample format Ring A's wire carries — the ONE place in this daemon that
/// owns the wire-format vocabulary. Both directions live here (token → header
/// id for the geometry fan-in builds, header id → token for what STATUS
/// reports), so the spelling fan-in accepts and the spelling it publishes can
/// never drift apart.
///
/// The tokens are the SAME ones the other ends of the wire spell: the ioplug's
/// conf.d `format` field (`c/jts-ring-ioplug/pcm_jts_ring.c`) and Python's
/// `jasper.fanin_coupling.RING_WIRE_FORMAT`. The match is EXACT (not
/// case-folded) because the ioplug's own `strcmp` is exact: accepting a
/// spelling the ioplug rejects would let fan-in build a geometry no reader can
/// declare.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RingWireFormat {
    /// Interleaved signed 16-bit little-endian — `jasper_ring`'s
    /// `SAMPLE_FORMAT_S16LE`.
    S16Le,
    /// Interleaved signed 32-bit little-endian — `jasper_ring`'s
    /// `SAMPLE_FORMAT_S32LE`.
    S32Le,
}

impl RingWireFormat {
    /// Normalize a raw `JASPER_FANIN_RING_WIRE_FORMAT` value.
    ///
    /// Unset or empty resolves to [`RingWireFormat::S32Le`] — empty is how the
    /// env-file writers in this repo clear a key (disable-clears-stale), so a
    /// cleared key and an absent key mean the same thing. A present but
    /// unrecognized value is a config-class fault and FAILS LOUD: silently
    /// resolving a typo to the default would arm a wire the operator did not
    /// ask for, and the ring's own attach-time validation could not tell the
    /// difference.
    ///
    /// THE DEFAULT IS WIDE because narrow is a width REGRESSION on the hop the
    /// ring replaces: the loopback CamillaDSP -> outputd hop already carries
    /// S32_LE, so arming a ring at S16_LE would narrow a hop that was wide
    /// before the arm. Nothing WRITES `JASPER_FANIN_RING_WIRE_FORMAT`, so an
    /// operator's `S16_LE` is a rollback lever no boot, deploy or udev pass can
    /// overwrite. Python's
    /// `jasper.fanin_coupling.resolve_ring_wire_format` defaults identically and
    /// `tests/test_ring_wire_format_contract.py` reads this arm to pin it.
    pub fn from_env_value(raw: Option<&str>) -> Result<Self> {
        match raw.map(str::trim) {
            None | Some("") => Ok(RingWireFormat::S32Le),
            Some("S16_LE") => Ok(RingWireFormat::S16Le),
            Some("S32_LE") => Ok(RingWireFormat::S32Le),
            Some(other) => Err(anyhow::anyhow!(
                "JASPER_FANIN_RING_WIRE_FORMAT={} unsupported (S16_LE|S32_LE) — \
                 the token must match the ioplug conf.d `format` field exactly",
                other,
            )
            .context(crate::ConfigClassError)),
        }
    }

    /// The `jasper_ring` header id for this wire — what the geometry fan-in
    /// creates or attaches against declares in `sample_format`.
    pub fn sample_format_id(self) -> u32 {
        match self {
            RingWireFormat::S16Le => jasper_ring::SAMPLE_FORMAT_S16LE,
            RingWireFormat::S32Le => jasper_ring::SAMPLE_FORMAT_S32LE,
        }
    }

    /// The reverse of [`RingWireFormat::sample_format_id`]: `None` for an id
    /// this daemon has no token for.
    pub fn from_sample_format_id(id: u32) -> Option<Self> {
        if id == jasper_ring::SAMPLE_FORMAT_S16LE {
            Some(RingWireFormat::S16Le)
        } else if id == jasper_ring::SAMPLE_FORMAT_S32LE {
            Some(RingWireFormat::S32Le)
        } else {
            None
        }
    }

    /// The wire vocabulary token — the spelling every end of the ring uses.
    pub fn as_str(self) -> &'static str {
        match self {
            RingWireFormat::S16Le => "S16_LE",
            RingWireFormat::S32Le => "S32_LE",
        }
    }
}

impl Config {
    /// Whether THIS BOX's resolved final-output wire is wide (S32LE) — the ONE
    /// per-box width decision the whole daemon reads (#2223).
    ///
    /// The TRANSPORT half of the conjunction is `true` on every box: the ring is
    /// the only fan-in → CamillaDSP transport (ADR-0100). What decides the width
    /// is the WIRE FORMAT half, which `JASPER_FANIN_RING_WIRE_FORMAT` resolves to
    /// `S32_LE` when unset ([`RingWireFormat::from_env_value`]). The ring's own
    /// attached header is the RUNTIME authority for what
    /// `write_ring_period` publishes; this is the same fact resolved from config
    /// at construction, which is when the lane buffers and the direct lane's
    /// render width have to be sized. The two cannot drift: `create_or_attach`
    /// validates the header field-by-field against the geometry built from this
    /// same config and fails the open on a mismatch, and `Mixer::new`
    /// cross-checks them explicitly before mixing a single period.
    ///
    /// THE CONJUNCTION ITSELF LIVES IN THE SHARED CRATE
    /// ([`jasper_tts_protocol::TtsWireWidth::from_box_declaration`]) and this
    /// calls it rather than restating it. The same rule decides the ASSISTANT
    /// wire's width, and the Python control plane
    /// (`jasper.fanin_coupling.assistant_wire_is_wide`) mirrors that one
    /// function — so "both ends derive the same answer" is a call graph, not a
    /// claim. `tests/test_ring_wire_format_contract.py` pins the verdict table
    /// across the two languages.
    pub fn program_wire_is_wide(&self) -> bool {
        matches!(
            jasper_tts_protocol::TtsWireWidth::from_box_declaration(
                matches!(self.ring_wire_format, RingWireFormat::S32Le),
                true,
            ),
            jasper_tts_protocol::TtsWireWidth::Wide,
        )
    }

    /// The `event=fanin.tts_wire.resolved` startup line — this box's resolved
    /// ASSISTANT wire width and the declared wire format that produced it.
    ///
    /// The mismatch warn fires at most once for the daemon's lifetime and may
    /// have scrolled out of the journal window; this line is always emitted,
    /// even with the TTS socket disabled, so it is the durable half of "is a
    /// mismatch converting right now, and which half made it narrow".
    /// `jasper-voice` publishes its own `event=tts_wire.resolved`; the two
    /// compared are the whole diagnosis.
    ///
    /// Rendered here rather than formatted at the call site so the fields are
    /// reachable from a test — an inline `info!` in `main()` is unguardable.
    pub fn assistant_wire_resolved_line(&self) -> String {
        let width = if self.program_wire_is_wide() {
            jasper_tts_protocol::TtsWireWidth::Wide
        } else {
            jasper_tts_protocol::TtsWireWidth::Narrow
        };
        format!(
            "event=fanin.tts_wire.resolved verb={} wire_format={} sample_bytes={}",
            width.verb(),
            self.ring_wire_format.as_str(),
            width.sample_bytes(),
        )
    }

    /// Read JASPER_FANIN_* env vars, falling back to documented defaults.
    /// Returns `Err` only on structural misconfiguration (e.g., input
    /// PCM list length != renderer label list length).
    pub fn from_env() -> Result<Self> {
        let input_pcms = env_list(
            "JASPER_FANIN_INPUT_PCMS",
            &[
                "hw:Loopback,1,0",
                "hw:Loopback,1,1",
                "hw:Loopback,1,2",
                "hw:Loopback,1,3",
                "hw:Loopback,1,4",
            ],
        );
        // Spelled out rather than built from MEASUREMENT_LANE: this array is
        // read as TEXT by tests/test_renderer_ring_lanes.py, which scrapes the
        // quoted labels straight out of this file to check that every Python
        // renderer-lane label is one fan-in will accept. A named constant is
        // invisible to that scrape. The agreement is pinned behaviourally by
        // `from_env_uses_documented_defaults` below, which asserts the parsed
        // `input_renderers[4] == MEASUREMENT_LANE`.
        let input_renderers = env_list(
            "JASPER_FANIN_INPUT_RENDERERS",
            &["spotify", "airplay", "bluealsa", "usbsink", "correction"],
        );
        if input_pcms.len() != input_renderers.len() {
            anyhow::bail!(
                "JASPER_FANIN_INPUT_PCMS has {} entries but JASPER_FANIN_INPUT_RENDERERS has {} \
                 — must match positionally",
                input_pcms.len(),
                input_renderers.len(),
            );
        }
        if input_pcms.is_empty() {
            anyhow::bail!(
                "JASPER_FANIN_INPUT_PCMS is empty — daemon needs at least \
                 one input substream to mix"
            );
        }

        let sample_rate = env_u32_positive("JASPER_FANIN_SAMPLE_RATE", 48_000)?;
        let period_frames = env_u32_positive("JASPER_FANIN_PERIOD_FRAMES", 256)?;
        let input_buffer_frames = env_u32_fallback(
            "JASPER_FANIN_INPUT_BUFFER_FRAMES",
            "JASPER_FANIN_BUFFER_FRAMES",
            4096,
        )?;

        // The input buffer must be >= 2 × period_frames per the standard ALSA
        // convention: the period wakes the reader/writer, the buffer absorbs
        // jitter between wakeups. The capture lanes are the only ALSA edge left
        // — the program leaves over Ring A (ADR-0100), sized by `ring_slots`
        // rather than by an ALSA buffer.
        let min_buffer_frames = period_frames.saturating_mul(2);
        if input_buffer_frames < min_buffer_frames {
            anyhow::bail!(
                "JASPER_FANIN_INPUT_BUFFER_FRAMES={} must be >= 2 × JASPER_FANIN_PERIOD_FRAMES={} \
                 (minimum ALSA jitter-absorption convention)",
                input_buffer_frames,
                period_frames,
            );
        }

        let loudness_defaults = AssistantLoudnessConfig::default();

        // The ring is the ONLY fan-in → CamillaDSP transport (ADR-0100), so this
        // key selects nothing; it exists to REFUSE a declaration this daemon
        // cannot serve. Unset / empty means "no declaration" (empty is how the
        // env-file writers clear a key), the same reading
        // `JASPER_FANIN_RING_WIRE_FORMAT` gives it. Anything other than
        // `shm_ring` — a persisted `loopback` above all — is a config-class
        // fault: exit 78, the unit parks visibly rather than playing over a
        // transport the operator did not ask for.
        match std::env::var("JASPER_FANIN_CAMILLA_COUPLING")
            .ok()
            .as_deref()
            .map(|s| s.trim().to_ascii_lowercase())
            .as_deref()
        {
            None | Some("") | Some("shm_ring") => {}
            Some(other) => {
                return Err(anyhow::anyhow!(
                    "JASPER_FANIN_CAMILLA_COUPLING={} unsupported (shm_ring) — the \
                     SHM ring is this daemon's only transport toward CamillaDSP \
                     (ADR-0100); a box that cannot be served by it parks instead \
                     of falling back",
                    other,
                )
                .context(crate::ConfigClassError));
            }
        }

        // Every rejection in this Ring A block carries `ConfigClassError`, so main()
        // exits 78 and the unit PARKS (RestartPreventExitStatus=78). A bad ring
        // geometry is identical on every restart, and the restart burst on this
        // unit escalates to StartLimitAction=reboot — a typo here would
        // otherwise reboot the speaker every few minutes.
        let ring_path = env_str("JASPER_FANIN_RING_PATH", "/dev/shm/jts-ring/program.ring");
        let ring_wire_format = RingWireFormat::from_env_value(
            std::env::var("JASPER_FANIN_RING_WIRE_FORMAT")
                .ok()
                .as_deref(),
        )?;
        let ring_slots = env_u32("JASPER_FANIN_RING_SLOTS", 2)
            .map_err(|e| e.context(crate::ConfigClassError))?;
        if !(RING_SLOTS_MIN..=RING_SLOTS_MAX).contains(&ring_slots) {
            return Err(anyhow::anyhow!(
                "JASPER_FANIN_RING_SLOTS={} out of range {}..={} — the SHM ring \
                 header validates this at attach; a shear-prone geometry must \
                 fail loud at config, not at runtime",
                ring_slots,
                RING_SLOTS_MIN,
                RING_SLOTS_MAX,
            )
            .context(crate::ConfigClassError));
        }
        // The slot is pinned at RING_SLOT_FRAMES (128, the outputd DAC-period
        // contract) and fan-in publishes period_frames/128 slots per step, so
        // period_frames must be a whole multiple of it or a step shears a slot.
        if period_frames % RING_SLOT_FRAMES != 0 {
            return Err(anyhow::anyhow!(
                "JASPER_FANIN_PERIOD_FRAMES={} must be a whole multiple of the \
                 pinned SHM ring slot size ({} frames) — a fractional slot count \
                 would shear the ring",
                period_frames,
                RING_SLOT_FRAMES,
            )
            .context(crate::ConfigClassError));
        }

        let input_resampler_enabled = env_enabled("JASPER_FANIN_INPUT_RESAMPLER");
        let input_resampler_lane_label = env_str("JASPER_FANIN_INPUT_RESAMPLER_LANE", "usbsink");
        let input_resampler_target_frames =
            env_u32("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", 512)?;
        let input_resampler_max_adjust_ppm =
            env_u32("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", 500)?;
        // Eight render periods of held cushion (2048 frames ≈ 42.7 ms). Hardware
        // USB testing showed a four-period cushion lock/unlock-thrashing on the
        // real snd-aloop burst feed; the deeper held cushion stayed locked.
        let input_resampler_warmup_cushion_frames =
            env_u32("JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES", 2048)?;
        let input_resampler_ring_frames = env_u32("JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES", 0)?;

        let input_resampler_cushion_decay_enabled =
            env_enabled("JASPER_FANIN_RESAMPLER_CUSHION_DECAY");
        // The tightest safe floor is the LARGER of two constraints, both a DLL
        // working margin above their anchor:
        //   1. `target + DLL margin` — a working cushion above the base target
        //      the DLL always has to steer within.
        //   2. `minimum_safe_fill_frames + DLL margin` — the PHYSICAL floor. The
        //      resampler underfill-unlocks the moment the cursor-relative fill
        //      drops below `minimum_safe_fill_frames` (= ceil(period × max_ratio)
        //      + kernel radius + 1), so a held target at/below that value sits on
        //      the unlock threshold: audible gap → snap-back → relock → warm-up →
        //      re-descend, on repeat. Constraint 1 alone does NOT imply
        //      constraint 2 — for a base target below ~period, `target + margin`
        //      can land below the physical floor.
        // `min_safe` comes from the same shared `jasper_resampler` helper the
        // lane's underfill gate uses, so the two cannot disagree about the
        // physical threshold.
        let cushion_decay_min_safe_fill = jasper_resampler::minimum_safe_fill_frames(
            period_frames,
            input_resampler_max_adjust_ppm as f64,
        ) as u32;
        let cushion_decay_floor_min = (input_resampler_target_frames
            + CUSHION_DECAY_FLOOR_MARGIN_FRAMES)
            .max(cushion_decay_min_safe_fill + CUSHION_DECAY_FLOOR_MARGIN_FRAMES);
        // The acquisition ceiling the decay descends FROM. The floor must sit in
        // [floor_min, ceiling]: above the ceiling there is nothing to decay.
        // Computed BEFORE the default so the default can clamp under it.
        let cushion_decay_ceiling =
            input_resampler_target_frames + input_resampler_warmup_cushion_frames;
        // The out-of-box default is the hardware-VALIDATED floor (576), not the
        // tighter unvalidated derived minimum (544 at the default geometry).
        // Clamped into [floor_min, ceiling]: never below the physical/DLL-margin
        // hard floor, never above the acquisition ceiling — a small-target
        // geometry whose ceiling < 576 constructs at its ceiling.
        let cushion_decay_floor_default = if cushion_decay_floor_min <= cushion_decay_ceiling {
            DEFAULT_CUSHION_DECAY_FLOOR_FRAMES.clamp(cushion_decay_floor_min, cushion_decay_ceiling)
        } else {
            // Preserve parseability while decay is disabled. If it is armed,
            // the range check below reports the invalid geometry instead of
            // panicking inside `u32::clamp`.
            cushion_decay_ceiling
        };
        let input_resampler_cushion_decay_floor_frames = env_u32(
            "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES",
            cushion_decay_floor_default,
        )?;
        // Armed-only, so a stale floor on a decay-off box never blocks boot.
        if input_resampler_cushion_decay_enabled
            && !(cushion_decay_floor_min..=cushion_decay_ceiling)
                .contains(&input_resampler_cushion_decay_floor_frames)
        {
            anyhow::bail!(
                "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES={} out of range {}..={} \
                 (>= max(target {} , minimum_safe_fill {}) + {}-frame DLL margin — a floor \
                 at/below minimum_safe_fill would underfill-unlock every period; \
                 <= the acquisition ceiling target+cushion {})",
                input_resampler_cushion_decay_floor_frames,
                cushion_decay_floor_min,
                cushion_decay_ceiling,
                input_resampler_target_frames,
                cushion_decay_min_safe_fill,
                CUSHION_DECAY_FLOOR_MARGIN_FRAMES,
                cushion_decay_ceiling,
            );
        }
        // Default 6: an executed demand of ≈ 125.3 ppm, ~4x under the inner
        // resampler's ±500 ppm authority. A step of 18 (≈ 375 ppm, 3/4 of that
        // authority) rails the ratio against a real ~190 ppm host offset,
        // live-verified on jts3, and the ±400 ppm cascade guard does not budget
        // for decay demand so it does not catch that (#3466). At 6 the descent
        // from ceiling to floor takes ~331 s; settled latency is unaffected. The
        // 1..=64 range bounds a gentle step; an ARMED pair must also pass the
        // demand-vs-authority margin check below.
        let input_resampler_cushion_decay_step_frames =
            env_u32("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_STEP_FRAMES", 6)?;
        if !(1..=64).contains(&input_resampler_cushion_decay_step_frames) {
            anyhow::bail!(
                "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_STEP_FRAMES={} out of range 1..=64 \
                 (a gentle per-step frame drop; default 6 ≈ 125 ppm demand — see issue #3466; \
                 an armed decay must also pass the demand-vs-authority margin check)",
                input_resampler_cushion_decay_step_frames,
            );
        }
        let input_resampler_cushion_decay_interval_ms =
            env_u32("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_INTERVAL_MS", 1000)?;
        if !(250..=10_000).contains(&input_resampler_cushion_decay_interval_ms) {
            anyhow::bail!(
                "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_INTERVAL_MS={} out of range 250..=10000 \
                 (wall interval between decay steps; 1000 ms is the default)",
                input_resampler_cushion_decay_interval_ms,
            );
        }
        // Decontamination precondition (#3466), armed-only like the floor
        // check: the EXECUTED demand (`DecayParams::step_demand_ppm`) is
        // subtracted from the host-clock observable, so it must leave real
        // margin inside the inner ±max_adjust_ppm authority — demand a railed
        // ratio cannot deliver would be subtracted anyway, fabricating
        // inverted-sign clock error for the L0 servo. Require at least half
        // the authority left for genuine host offset.
        if input_resampler_cushion_decay_enabled {
            let executed_demand_ppm = crate::lane_resampler::DecayParams::step_demand_ppm(
                input_resampler_cushion_decay_step_frames as u64,
                input_resampler_cushion_decay_interval_ms as u64,
                period_frames,
                sample_rate,
            );
            if executed_demand_ppm * 2.0 > input_resampler_max_adjust_ppm as f64 {
                anyhow::bail!(
                    "cushion-decay demand {:.1} ppm (STEP_FRAMES={} / INTERVAL_MS={} at \
                     {} frames / {} Hz) exceeds half the inner ±{} ppm authority \
                     (JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM): the demand is subtracted \
                     from the host-clock observable (#3466), so it must leave at least as much \
                     authority for genuine host offset as it consumes — lower the step or \
                     lengthen the interval",
                    executed_demand_ppm,
                    input_resampler_cushion_decay_step_frames,
                    input_resampler_cushion_decay_interval_ms,
                    period_frames,
                    sample_rate,
                    input_resampler_max_adjust_ppm,
                );
            }
        }
        let auto_trim_enabled = env_enabled("JASPER_FANIN_AUTO_TRIM");

        let usb_direct_enabled = env_enabled("JASPER_FANIN_USB_DIRECT");
        let usb_direct_device = env_str("JASPER_FANIN_USB_DIRECT_DEVICE", "hw:UAC2Gadget");
        // Range 32..=1024: below 32 the period IRQ storms the mixer thread, above
        // 1024 the open period exceeds the deep-buffer floor's own headroom and
        // defeats the low-latency intent. Only consulted on the direct lane, but
        // parsed unconditionally so a typo fails loud on any boot, not only on an
        // armed box.
        let usb_direct_period_frames = env_u32("JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES", 256)?;
        if !(32..=1024).contains(&usb_direct_period_frames) {
            anyhow::bail!(
                "JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES={} out of range 32..=1024 (the gadget \
                 open period; 256 is the bridge-proven default, 64 is the lever-2 H1 test knob)",
                usb_direct_period_frames,
            );
        }

        // Parsed and VALIDATED unconditionally so a typo fails on any boot rather
        // than only on an armed box.
        //
        // EVERY rejection below carries `ConfigClassError`, for the reason the
        // Ring A block above states and this block reaches sooner: a bad lane map
        // is IDENTICAL on every restart, so without the marker `main` exits 1, the
        // unit burns StartLimitBurst, and `StartLimitAction=reboot` reboots the
        // speaker every few minutes forever. A box on
        // `JASPER_FANIN_PERIOD_FRAMES=128` derives 4096/128 = 32 slots, above
        // `RING_SLOTS_MAX`, so any armed lane there hits the geometry rejection on
        // every boot.
        let renderer_ring_lanes = env_csv_labels("JASPER_FANIN_RENDERER_RING_LANES");
        for label in &renderer_ring_lanes {
            if !input_renderers.iter().any(|l| l == label) {
                return Err(anyhow::anyhow!(
                    "JASPER_FANIN_RENDERER_RING_LANES names '{}', which is not one of this \
                     daemon's lanes [{}] — a ring lane is selected by its fan-in LABEL, and an \
                     unmatched label would silently arm nothing",
                    label,
                    input_renderers.join(", "),
                )
                .context(crate::ConfigClassError));
            }
        }
        if !renderer_ring_lanes.is_empty()
            && renderer_ring_slots(input_buffer_frames, period_frames).is_none()
        {
            return Err(anyhow::anyhow!(
                "JASPER_FANIN_RENDERER_RING_LANES is armed ([{}]) but this box's lane geometry \
                 cannot be expressed as whole ring slots: \
                 JASPER_FANIN_INPUT_BUFFER_FRAMES={} / JASPER_FANIN_PERIOD_FRAMES={} must divide \
                 evenly into {}..={} slots. A renderer ring's depth is DERIVED from the aloop \
                 cushion it replaces so the renderer's flow control is unchanged; refusing here \
                 rather than rounding keeps that promise honest",
                renderer_ring_lanes.join(", "),
                input_buffer_frames,
                period_frames,
                RING_SLOTS_MIN,
                RING_SLOTS_MAX,
            )
            .context(crate::ConfigClassError));
        }
        // A ring lane that ALSO wants a `LaneResampler` is REFUSED.
        // `read_ring_and_render` consumes one slot per period straight into
        // `read_buf` and never touches `input.resampler`, so a lane holding both
        // would build a resampler, feed it nothing, and render the ring directly
        // — rate reconciliation silently absent while `/state` reported it armed.
        // Nothing needs the combination: the resampler lane is the USB one, which
        // is `direct`, and a ring lane's producer is DAC-paced through its own
        // blocking write.
        if let Some(label) = renderer_ring_lanes.iter().find(|label| {
            lane_wants_resampler_for(
                label,
                &input_resampler_lane_label,
                input_resampler_enabled,
                usb_direct_enabled,
            )
        }) {
            return Err(anyhow::anyhow!(
                "lane '{}' is in JASPER_FANIN_RENDERER_RING_LANES and is also the armed \
                 resampler lane (JASPER_FANIN_INPUT_RESAMPLER / JASPER_FANIN_USB_DIRECT with \
                 JASPER_FANIN_INPUT_RESAMPLER_LANE={}). That combination is not designed: the \
                 ring read path renders slots directly and never feeds the resampler, so the \
                 lane would report a resampler that reconciles nothing. Pick one transport for \
                 this lane",
                label,
                input_resampler_lane_label,
            )
            .context(crate::ConfigClassError));
        }

        // STATIC held-target churn guard — the symmetric sibling of the
        // decay-floor validation above, entered through the static cushion knobs.
        // An armed lane (JASPER_FANIN_INPUT_RESAMPLER=enabled, or implied by
        // JASPER_FANIN_USB_DIRECT=enabled — the direct lane has no aloop catch-up
        // fallback, so it always builds a resampler) holds the ring at
        // `target + cushion` and renders ONE `period_frames` each step, so the
        // steady-state post-render cursor-relative fill sits at `held - period`.
        // The lane underfill-unlocks the instant that fill drops below
        // `minimum_safe_fill_frames` (= ceil(period × max_ratio) + radius + 1), so
        // the held target must sit at least `period + jitter margin` above
        // min_safe or ordinary USB delivery coalescing (arrivals clustering below
        // the per-render deficit — the max_avail ≈ 2×period gadget signature the
        // drain-stats histogram shows) trips lock→silence→relock every burst.
        // Arm-gated so a stale cushion on a resampler-OFF box never blocks boot.
        // The production defaults (512 + 2048 = 2560 held) clear this by ~2030
        // frames; only a hand-tuned lab geometry (the observed churn came from
        // 256 + 256 = 512 held) can trip it.
        let resampler_armed_on_a_lane = input_resampler_enabled || usb_direct_enabled;
        if resampler_armed_on_a_lane {
            let min_safe = jasper_resampler::minimum_safe_fill_frames(
                period_frames,
                input_resampler_max_adjust_ppm as f64,
            ) as u32;
            let held_target = input_resampler_target_frames + input_resampler_warmup_cushion_frames;
            let required_held = min_safe + period_frames + STATIC_CUSHION_JITTER_MARGIN_FRAMES;
            if held_target < required_held {
                // The steady post-render cursor fill (`held - period`) vs the
                // underfill-unlock threshold (`min_safe`) — reported as an i64 so a
                // fill already AT/BELOW the threshold shows a negative headroom
                // rather than a misleading clamped 0.
                let post_render_headroom =
                    held_target as i64 - period_frames as i64 - min_safe as i64;
                anyhow::bail!(
                    "JASPER_FANIN_INPUT_RESAMPLER held target (target {} + warm-up cushion {} \
                     = {}) is too shallow for the armed clock-crossing lane: it must be >= \
                     minimum_safe_fill {} + one render period {} + {}-frame jitter margin = {}. \
                     The steady post-render cursor fill would sit only {} frames above the \
                     underfill-unlock threshold (negative = already at/below it), so ordinary \
                     USB delivery coalescing thrashes lock->silence->relock \
                     (churn-by-construction). Raise \
                     JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES (or _TARGET_FRAMES) so \
                     target+cushion >= {}, or lower JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM \
                     / JASPER_FANIN_PERIOD_FRAMES.",
                    input_resampler_target_frames,
                    input_resampler_warmup_cushion_frames,
                    held_target,
                    min_safe,
                    period_frames,
                    STATIC_CUSHION_JITTER_MARGIN_FRAMES,
                    required_held,
                    post_render_headroom,
                    required_held,
                );
            }
        }

        // Unlike the sibling `enabled` flags above, which stay off silently on any
        // other value, this one WARNS on a non-empty non-`enabled` value — the
        // usbsink literal idiom (`JASPER_USBSINK_HOST_CLOCK`), so a typo like
        // `on`/`1` leaves a breadcrumb rather than silently disabling the feature.
        let host_clock_enabled = match std::env::var("JASPER_FANIN_HOST_CLOCK") {
            Ok(raw) => {
                let v = raw.trim();
                if v.is_empty() {
                    false
                } else if v.eq_ignore_ascii_case("enabled") {
                    true
                } else {
                    log::warn!(
                        "event=fanin.host_clock_config_ignored key=JASPER_FANIN_HOST_CLOCK value={v:?} reason=not_literal_enabled"
                    );
                    false
                }
            }
            Err(_) => false,
        };
        let host_clock_probe_ppm = env_u32("JASPER_FANIN_HOST_CLOCK_PROBE_PPM", 300)?;
        if !(200..=800).contains(&host_clock_probe_ppm) {
            anyhow::bail!(
                "JASPER_FANIN_HOST_CLOCK_PROBE_PPM={} out of range 200..=800 (a probe \
                 at/below the ~163 ppm Windows usbaudio2.sys deadband would falsely \
                 fail every session; the ceiling keeps it inside the ±1000 ppm \
                 validity window)",
                host_clock_probe_ppm,
            );
        }
        let tts_program_duck_db =
            env_f32_fallback("JASPER_FANIN_TTS_PROGRAM_DUCK_DB", "JASPER_DUCK_DB", -25.0)?;
        if tts_program_duck_db > 0.0 {
            anyhow::bail!(
                "JASPER_FANIN_TTS_PROGRAM_DUCK_DB={} must be <= 0 (a duck \
                 attenuates; positive gain on the program is never allowed)",
                tts_program_duck_db
            );
        }
        // The shallower duck for the segment-driven auto-duck that fires while a
        // standalone short earcon/cue is queued (mute/unmute sparkle, wake chirp)
        // — see `TtsMixer::program_duck_gain`. Deliberately does NOT fall back to
        // JASPER_DUCK_DB: a cue must duck less than a conversation.
        let tts_cue_duck_db = env_f32("JASPER_FANIN_TTS_CUE_DUCK_DB", -6.0)?;
        if tts_cue_duck_db > 0.0 {
            anyhow::bail!(
                "JASPER_FANIN_TTS_CUE_DUCK_DB={} must be <= 0 (a duck \
                 attenuates; positive gain on the program is never allowed)",
                tts_cue_duck_db
            );
        }
        let held_content_ttl_sec = env_f32(
            "JASPER_FANIN_HELD_CONTENT_TTL_SEC",
            AssistantLoudnessConfig::default().held_content_ttl_sec,
        )?;
        if !(1.0..=86_400.0).contains(&held_content_ttl_sec) {
            anyhow::bail!(
                "JASPER_FANIN_HELD_CONTENT_TTL_SEC={} out of range 1..=86400",
                held_content_ttl_sec
            );
        }
        let assistant_envelope_offset_limit_lu = env_f32(
            "JASPER_FANIN_ASSISTANT_ENVELOPE_OFFSET_LIMIT_LU",
            AssistantLoudnessConfig::default().assistant_envelope_offset_limit_lu,
        )?;
        if !(0.0..=24.0).contains(&assistant_envelope_offset_limit_lu) {
            anyhow::bail!(
                "JASPER_FANIN_ASSISTANT_ENVELOPE_OFFSET_LIMIT_LU={} out of range 0..=24",
                assistant_envelope_offset_limit_lu
            );
        }

        let tts_duck_attack_ms = env_u32("JASPER_FANIN_TTS_DUCK_ATTACK_MS", 15)?;
        if !(1..=200).contains(&tts_duck_attack_ms) {
            anyhow::bail!(
                "JASPER_FANIN_TTS_DUCK_ATTACK_MS={} out of range 1..=200",
                tts_duck_attack_ms
            );
        }
        let tts_duck_release_ms = env_u32("JASPER_FANIN_TTS_DUCK_RELEASE_MS", 150)?;
        if !(1..=2000).contains(&tts_duck_release_ms) {
            anyhow::bail!(
                "JASPER_FANIN_TTS_DUCK_RELEASE_MS={} out of range 1..=2000",
                tts_duck_release_ms
            );
        }

        Ok(Self {
            input_pcms,
            input_renderers,
            sample_rate,
            period_frames,
            input_buffer_frames,
            control_socket_path: "/run/jasper-fanin/control.sock".to_string(),
            xrun_log_path: env_str(
                "JASPER_FANIN_XRUN_LOG_PATH",
                "/var/lib/jasper/fanin/xrun_history.jsonl",
            ),
            tts_socket_path: env_optional_with_default(
                "JASPER_FANIN_TTS_SOCKET",
                "/run/jasper-fanin/tts.sock",
            ),
            tts_max_pending_frames: env_u64(
                "JASPER_FANIN_TTS_MAX_PENDING_FRAMES",
                crate::tts::DEFAULT_MAX_PENDING_FRAMES,
            )?,
            tts_program_duck_db,
            tts_cue_duck_db,
            tts_duck_attack_ms,
            tts_duck_release_ms,
            assistant_loudness: AssistantLoudnessConfig {
                assistant_offset_lu: env_f32(
                    "JASPER_OUTPUTD_ASSISTANT_OFFSET_LU",
                    loudness_defaults.assistant_offset_lu,
                )?,
                max_peak_dbfs: env_f32(
                    "JASPER_OUTPUTD_ASSISTANT_MAX_PEAK_DBFS",
                    loudness_defaults.max_peak_dbfs,
                )?,
                fallback_source_lufs: env_f32(
                    "JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_LUFS",
                    loudness_defaults.fallback_source_lufs,
                )?,
                fallback_source_peak_dbfs: env_f32(
                    "JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS",
                    loudness_defaults.fallback_source_peak_dbfs,
                )?,
                default_tts_envelope_lufs: env_f32_fallback(
                    "JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS",
                    "JASPER_OUTPUTD_ASSISTANT_DEFAULT_SILENCE_TARGET_LUFS",
                    loudness_defaults.default_tts_envelope_lufs,
                )?,
                content_silence_lufs: env_f32(
                    "JASPER_OUTPUTD_CONTENT_SILENCE_LUFS",
                    loudness_defaults.content_silence_lufs,
                )?,
                held_content_ttl_sec,
                assistant_envelope_offset_limit_lu,
            },
            assistant_reference_path: env_str(
                "JASPER_FANIN_ASSISTANT_REFERENCE_PATH",
                "/var/lib/jasper/assistant_volume_reference.json",
            ),
            ring_path,
            ring_slots,
            ring_wire_format,
            input_resampler_enabled,
            input_resampler_lane_label,
            input_resampler_target_frames,
            input_resampler_max_adjust_ppm,
            input_resampler_warmup_cushion_frames,
            input_resampler_ring_frames,
            input_resampler_cushion_decay_enabled,
            input_resampler_cushion_decay_floor_frames,
            input_resampler_cushion_decay_step_frames,
            input_resampler_cushion_decay_interval_ms,
            auto_trim_enabled,
            usb_direct_enabled,
            usb_direct_device,
            usb_direct_period_frames,
            renderer_ring_lanes,
            host_clock_enabled,
            host_clock_probe_ppm,
        })
    }
}

// ---- env var helpers ------------------------------------------------

/// Parse a comma-separated LABEL set. Whitespace around each label is trimmed and
/// empty entries are dropped, so `""`, `" "`, and `",,"` all mean "no labels" —
/// the fail-safe direction for a key whose empty value must mean "nothing armed".
/// Order is preserved and duplicates are kept: the only consumer is a membership
/// test, and silently de-duplicating would hide a malformed write from the one
/// writer that produces this value.
fn env_csv_labels(name: &str) -> Vec<String> {
    std::env::var(name)
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

/// Fail-safe feature gate: only the exact `enabled` token, ignoring case and
/// surrounding whitespace, arms the feature.
fn env_enabled(name: &str) -> bool {
    std::env::var(name).is_ok_and(|value| value.trim().eq_ignore_ascii_case("enabled"))
}

fn env_optional_with_default(name: &str, default: &str) -> Option<String> {
    match std::env::var(name) {
        Ok(s) if s.trim().is_empty() || s.trim().eq_ignore_ascii_case("disabled") => None,
        Ok(s) => Some(s),
        Err(_) => Some(default.to_string()),
    }
}

/// Parse a pipe-delimited list env var. Pipe rather than comma
/// because ALSA hw PCM names contain commas (`hw:Loopback,1,0`);
/// a comma-delimited shape would silently split one PCM name into
/// three entries.
fn env_list(name: &str, default: &[&str]) -> Vec<String> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => s
            .split('|')
            .map(|e| e.trim().to_string())
            .filter(|e| !e.is_empty())
            .collect(),
        _ => default.iter().map(|s| s.to_string()).collect(),
    }
}

fn env_u32(name: &str, default: u32) -> Result<u32> {
    env_parse(name, default, "a non-negative integer")
}

/// Like `env_u32`, but for a load-bearing GEOMETRY DIMENSION that must be
/// strictly positive — `sample_rate` and `period_frames`. A parsed `0` is a
/// legal `u32` yet a nonsensical dimension: the mixer's per-period math divides
/// by both (`period_frames * 1e9 / sample_rate` for `mixer.rs`'s ShmRing
/// ns/period, `(avail - target) / period_frames` in `catchup_drain_periods`, and
/// the ms→periods conversion), all UNGUARDED, and release builds compile out the
/// `debug_assert!(period_frames > 0)`. With `panic = "abort"` and the unit's
/// `Restart=on-failure` a divide-by-zero panic is an infinite crash-restart loop
/// that takes ALL audio down, the audible-cue path with it. Bailing would be its
/// own config-parse restart loop, so a zero falls back to the documented default
/// with a WARN breadcrumb and the speaker keeps playing on a sane geometry. A
/// non-numeric or negative value still fails loud via `env_u32`; only a
/// valid-but-zero dimension is recovered here.
fn env_u32_positive(name: &str, default: u32) -> Result<u32> {
    let parsed = env_u32(name, default)?;
    if parsed == 0 {
        log::warn!(
            "event=fanin.config_ignored key={name} value=0 reason=dimension_must_be_positive default={default}"
        );
        return Ok(default);
    }
    Ok(parsed)
}

fn env_u64(name: &str, default: u64) -> Result<u64> {
    env_parse(name, default, "a non-negative integer")
}

fn env_f32_fallback(name: &str, fallback_name: &str, default: f32) -> Result<f32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => jasper_env::parse_f32(name, &s),
        _ => env_f32(fallback_name, default),
    }
}

fn env_u32_fallback(name: &str, fallback_name: &str, default: u32) -> Result<u32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => env_parse(name, default, "a non-negative integer"),
        _ => env_u32(fallback_name, default),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::Mutex;

    /// Process-global mutex serializing env-var-touching tests.
    /// `std::env::set_var` mutates process-global state, so even with careful
    /// save+restore these tests must run sequentially; this serializes them
    /// without forcing `--test-threads=1` across the whole crate.
    ///
    /// Poisoned-but-recoverable: a test that panics inside `with_env` leaves a
    /// PoisonError for the next acquirer, which takes the guard anyway — the
    /// panicked test's restoration did not run, but the next test's setup clears
    /// everything.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// Serialize on `ENV_LOCK`, snapshot ALL fan-in env vars, clear them, apply
    /// this test's per-var overrides, run the closure, restore.
    fn with_env<F: FnOnce()>(vars: &[(&str, Option<&str>)], f: F) {
        let _guard = ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());

        let snapshot: Vec<(String, String)> = std::env::vars()
            .filter(|(k, _)| {
                k.starts_with("JASPER_FANIN_")
                    || k.starts_with("JASPER_OUTPUTD_ASSISTANT_")
                    || k == "JASPER_OUTPUTD_CONTENT_SILENCE_LUFS"
                    || k == "JASPER_DUCK_DB"
            })
            .collect();
        for (k, _) in &snapshot {
            std::env::remove_var(k);
        }

        for (k, v) in vars {
            match v {
                Some(val) => std::env::set_var(k, val),
                None => std::env::remove_var(k),
            }
        }

        f();

        for (k, _) in vars {
            std::env::remove_var(k);
        }
        for (k, v) in snapshot {
            std::env::set_var(&k, v);
        }
    }

    #[test]
    fn from_env_uses_documented_defaults() {
        with_env(
            &[
                ("JASPER_FANIN_INPUT_PCMS", None),
                ("JASPER_FANIN_INPUT_RENDERERS", None),
                ("JASPER_FANIN_SAMPLE_RATE", None),
                ("JASPER_FANIN_PERIOD_FRAMES", None),
                ("JASPER_FANIN_BUFFER_FRAMES", None),
                ("JASPER_FANIN_INPUT_BUFFER_FRAMES", None),
                ("JASPER_FANIN_TTS_SOCKET", None),
                ("JASPER_FANIN_TTS_MAX_PENDING_FRAMES", None),
                ("JASPER_FANIN_TTS_PROGRAM_DUCK_DB", None),
                ("JASPER_FANIN_TTS_CUE_DUCK_DB", None),
                ("JASPER_OUTPUTD_ASSISTANT_OFFSET_LU", None),
                ("JASPER_OUTPUTD_ASSISTANT_MAX_PEAK_DBFS", None),
                ("JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_LUFS", None),
                ("JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS", None),
                ("JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS", None),
                ("JASPER_OUTPUTD_ASSISTANT_DEFAULT_SILENCE_TARGET_LUFS", None),
                ("JASPER_OUTPUTD_CONTENT_SILENCE_LUFS", None),
                ("JASPER_FANIN_ASSISTANT_REFERENCE_PATH", None),
                ("JASPER_DUCK_DB", None),
            ],
            || {
                let cfg = Config::from_env().expect("defaults must parse");
                assert_eq!(cfg.input_pcms.len(), 5);
                assert_eq!(cfg.input_renderers.len(), 5);
                assert_eq!(cfg.input_renderers[0], "spotify");
                assert_eq!(cfg.input_renderers[4], MEASUREMENT_LANE);
                assert_eq!(cfg.sample_rate, 48_000);
                assert_eq!(cfg.period_frames, 256);
                assert_eq!(cfg.input_buffer_frames, 4096);
                assert_eq!(
                    cfg.tts_socket_path.as_deref(),
                    Some("/run/jasper-fanin/tts.sock")
                );
                assert_eq!(cfg.tts_max_pending_frames, 96_000);
                assert_eq!(cfg.tts_program_duck_db, -25.0);
                assert_eq!(cfg.tts_cue_duck_db, -6.0);
                assert_eq!(cfg.assistant_loudness.assistant_offset_lu, 1.5);
                assert_eq!(cfg.assistant_loudness.max_peak_dbfs, -3.0);
                assert_eq!(cfg.assistant_loudness.default_tts_envelope_lufs, -41.0);
                assert_eq!(cfg.assistant_loudness.held_content_ttl_sec, 600.0);
                assert_eq!(
                    cfg.assistant_loudness.assistant_envelope_offset_limit_lu,
                    8.0
                );
                assert_eq!(
                    cfg.assistant_reference_path,
                    "/var/lib/jasper/assistant_volume_reference.json"
                );
                assert!(
                    !cfg.input_resampler_enabled,
                    "input resampler must default OFF"
                );
                assert_eq!(cfg.input_resampler_lane_label, "usbsink");
                assert_eq!(cfg.input_resampler_target_frames, 512);
                assert_eq!(cfg.input_resampler_max_adjust_ppm, 500);
                assert_eq!(cfg.input_resampler_warmup_cushion_frames, 2048);
                assert_eq!(cfg.input_resampler_ring_frames, 0);
                assert!(
                    !cfg.input_resampler_cushion_decay_enabled,
                    "cushion decay must default OFF"
                );
                assert_eq!(
                    cfg.input_resampler_cushion_decay_floor_frames,
                    DEFAULT_CUSHION_DECAY_FLOOR_FRAMES
                );
                assert_eq!(cfg.input_resampler_cushion_decay_step_frames, 6);
                assert_eq!(cfg.input_resampler_cushion_decay_interval_ms, 1000);
                // Demand-ppm pin in the EXECUTED periods-space the machine
                // publishes and the observable subtracts (#3466), via the ONE
                // shared derivation rather than a re-derived ms-space
                // approximation: 6 / 1000 ms at 48 kHz / 256 truncates to 187
                // periods ⇒ 125.33 ppm, not 125.0.
                let step_demand_ppm = crate::lane_resampler::DecayParams::step_demand_ppm(
                    cfg.input_resampler_cushion_decay_step_frames as u64,
                    cfg.input_resampler_cushion_decay_interval_ms as u64,
                    cfg.period_frames,
                    cfg.sample_rate,
                );
                assert!(
                    (step_demand_ppm - 125.334).abs() < 0.01,
                    "default executed step demand is ~125.33 ppm, got {step_demand_ppm}"
                );
                assert!(
                    step_demand_ppm * 2.0 <= cfg.input_resampler_max_adjust_ppm as f64,
                    "default decay step demand {step_demand_ppm} ppm must clear the armed \
                     demand-vs-authority bound (±{} ppm)",
                    cfg.input_resampler_max_adjust_ppm
                );
                assert!(!cfg.auto_trim_enabled, "auto-trim must default OFF");
                assert!(!cfg.usb_direct_enabled, "usb-direct must default OFF");
                assert_eq!(cfg.usb_direct_device, "hw:UAC2Gadget");
            },
        );
    }

    #[test]
    fn default_fanin_socket_paths_are_reserved_from_impulse_tap() {
        use crate::impulse_tap::{path_is_allowed, RESERVED_TAP_DIR_BASENAMES, TAP_PATH_DIR};

        with_env(&[], || {
            let cfg = Config::from_env().expect("defaults must parse");
            let tts_socket_path = cfg
                .tts_socket_path
                .as_deref()
                .expect("the default TTS socket must be enabled");

            for raw_path in [cfg.control_socket_path.as_str(), tts_socket_path] {
                let path = std::path::Path::new(raw_path);
                assert_eq!(
                    path.parent(),
                    Some(std::path::Path::new(TAP_PATH_DIR)),
                    "fan-in-owned socket must remain under the tap directory: {}",
                    path.display()
                );
                let basename = path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .expect("fan-in socket default must have a UTF-8 basename");
                assert!(
                    RESERVED_TAP_DIR_BASENAMES.contains(&basename),
                    "fan-in-owned socket basename must be reserved from TAP_ARM: {basename}"
                );
                assert!(
                    !path_is_allowed(path),
                    "TAP_ARM must reject fan-in-owned socket {}",
                    path.display()
                );
            }
        });
    }

    #[test]
    fn zero_dimension_falls_back_to_default_not_divide_by_zero() {
        // `sample_rate` and `period_frames` are divided by with no runtime guard
        // in release builds, so a valid-but-zero value must not construct a
        // Config that panic-aborts into a crash loop.
        with_env(&[("JASPER_FANIN_PERIOD_FRAMES", Some("0"))], || {
            let cfg = Config::from_env().expect("zero period_frames must not fail to parse");
            assert_eq!(
                cfg.period_frames, 256,
                "period_frames=0 must fall back to the 256 default, never 0"
            );
        });
        with_env(&[("JASPER_FANIN_SAMPLE_RATE", Some("0"))], || {
            let cfg = Config::from_env().expect("zero sample_rate must not fail to parse");
            assert_eq!(
                cfg.sample_rate, 48_000,
                "sample_rate=0 must fall back to the 48000 default, never 0"
            );
        });
        // Whitespace-wrapped zero is still zero.
        with_env(&[("JASPER_FANIN_PERIOD_FRAMES", Some("  0 "))], || {
            let cfg = Config::from_env().expect("parses");
            assert_eq!(cfg.period_frames, 256);
        });
    }

    #[test]
    fn positive_dimension_passes_valid_values_unchanged() {
        // period_frames must be a whole multiple of the 128-frame ring slot; 512
        // satisfies that.
        with_env(&[("JASPER_FANIN_PERIOD_FRAMES", Some("512"))], || {
            let cfg = Config::from_env().expect("parses");
            assert_eq!(cfg.period_frames, 512);
        });
        with_env(&[("JASPER_FANIN_SAMPLE_RATE", Some("44100"))], || {
            let cfg = Config::from_env().expect("parses");
            assert_eq!(cfg.sample_rate, 44_100);
        });
    }

    #[test]
    fn non_numeric_dimension_still_fails_loud() {
        // The boundary: 0 → default + warn, garbage → error.
        with_env(&[("JASPER_FANIN_PERIOD_FRAMES", Some("garbage"))], || {
            assert!(
                Config::from_env().is_err(),
                "a non-numeric period_frames must still fail loud, not fall back"
            );
        });
    }

    #[test]
    fn auto_trim_only_armed_by_exact_enabled_literal() {
        for raw in ["enabled", "ENABLED", " Enabled "] {
            with_env(&[("JASPER_FANIN_AUTO_TRIM", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(cfg.auto_trim_enabled, "{raw:?} should arm auto-trim");
            });
        }
        for raw in ["", "1", "true", "on", "yes", "disabled", "garbage"] {
            with_env(&[("JASPER_FANIN_AUTO_TRIM", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(
                    !cfg.auto_trim_enabled,
                    "{raw:?} must NOT arm auto-trim (only `enabled` does)"
                );
            });
        }
    }

    #[test]
    fn input_resampler_only_armed_by_exact_enabled_literal() {
        for raw in ["enabled", "ENABLED", " Enabled "] {
            with_env(&[("JASPER_FANIN_INPUT_RESAMPLER", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(
                    cfg.input_resampler_enabled,
                    "{raw:?} should arm the resampler"
                );
            });
        }
        for raw in ["", "1", "true", "on", "yes", "disabled", "garbage"] {
            with_env(&[("JASPER_FANIN_INPUT_RESAMPLER", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(
                    !cfg.input_resampler_enabled,
                    "{raw:?} must NOT arm the resampler (only `enabled` does)"
                );
            });
        }
    }

    #[test]
    fn usb_direct_only_armed_by_exact_enabled_literal() {
        for raw in ["enabled", "ENABLED", " Enabled "] {
            with_env(&[("JASPER_FANIN_USB_DIRECT", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(cfg.usb_direct_enabled, "{raw:?} should arm USB direct");
            });
        }
        for raw in ["", "1", "true", "on", "yes", "disabled", "garbage"] {
            with_env(&[("JASPER_FANIN_USB_DIRECT", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(
                    !cfg.usb_direct_enabled,
                    "{raw:?} must NOT arm USB direct (only `enabled` does)"
                );
            });
        }
    }

    #[test]
    fn usb_direct_device_default_and_override() {
        with_env(&[("JASPER_FANIN_USB_DIRECT_DEVICE", None)], || {
            assert_eq!(
                Config::from_env().unwrap().usb_direct_device,
                "hw:UAC2Gadget"
            );
        });
        with_env(
            &[("JASPER_FANIN_USB_DIRECT_DEVICE", Some("hw:UAC2Gadget,0,0"))],
            || {
                assert_eq!(
                    Config::from_env().unwrap().usb_direct_device,
                    "hw:UAC2Gadget,0,0"
                );
            },
        );
    }

    #[test]
    fn usb_direct_default_off_is_inert() {
        with_env(
            &[
                ("JASPER_FANIN_USB_DIRECT", None),
                ("JASPER_FANIN_INPUT_RESAMPLER", None),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert!(!cfg.usb_direct_enabled);
                assert!(!cfg.input_resampler_enabled);
                assert!(
                    !cfg.lane_wants_resampler("usbsink"),
                    "no resampler on any lane when both flags are off"
                );
            },
        );
    }

    #[test]
    fn usb_direct_implies_resampler_on_the_usbsink_lane() {
        // Direct capture has no aloop catch-up fallback, so its lane still wants
        // a resampler with the plain flag off — and only that lane does.
        with_env(
            &[
                ("JASPER_FANIN_USB_DIRECT", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER", None),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert!(cfg.usb_direct_enabled);
                assert!(!cfg.input_resampler_enabled);
                assert!(
                    cfg.lane_wants_resampler("usbsink"),
                    "direct mode must imply a resampler on the usbsink lane"
                );
                assert!(
                    !cfg.lane_wants_resampler("airplay"),
                    "only the resampler lane label gets one"
                );
            },
        );
    }

    #[test]
    fn input_resampler_alone_still_wants_resampler_on_its_lane() {
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER", Some("enabled")),
                ("JASPER_FANIN_USB_DIRECT", None),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert!(cfg.lane_wants_resampler("usbsink"));
            },
        );
    }

    #[test]
    fn input_resampler_knobs_parse_overrides() {
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_LANE", Some("usbsink2")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("768")),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("300")),
                (
                    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                    Some("384"),
                ),
                ("JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES", Some("8192")),
            ],
            || {
                let cfg = Config::from_env().expect("parses");
                assert!(cfg.input_resampler_enabled);
                assert_eq!(cfg.input_resampler_lane_label, "usbsink2");
                assert_eq!(cfg.input_resampler_target_frames, 768);
                assert_eq!(cfg.input_resampler_max_adjust_ppm, 300);
                assert_eq!(cfg.input_resampler_warmup_cushion_frames, 384);
                assert_eq!(cfg.input_resampler_ring_frames, 8192);
            },
        );
    }

    #[test]
    fn tts_socket_can_be_disabled() {
        with_env(&[("JASPER_FANIN_TTS_SOCKET", Some("disabled"))], || {
            let cfg = Config::from_env().expect("disabled TTS socket must parse");
            assert_eq!(cfg.tts_socket_path, None);
        });
    }

    // ---- combo-mode host-slaved USB clock ---------------------------------

    #[test]
    fn host_clock_only_armed_by_exact_enabled_literal() {
        for raw in ["enabled", "ENABLED", " Enabled "] {
            with_env(&[("JASPER_FANIN_HOST_CLOCK", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(cfg.host_clock_enabled, "{raw:?} should arm host-clock");
            });
        }
        for raw in ["", "1", "true", "on", "yes", "disabled", "garbage"] {
            with_env(&[("JASPER_FANIN_HOST_CLOCK", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(
                    !cfg.host_clock_enabled,
                    "{raw:?} must NOT arm host-clock (only `enabled` does)"
                );
            });
        }
    }

    #[test]
    fn host_clock_servo_armed_requires_both_host_clock_and_direct() {
        // The servo can only own the pitch ctl when fan-in owns the gadget
        // capture (`enabled` + direct-off is a fully-inert warn), so the predicate
        // is the AND of the two flags. It is the SINGLE source of truth `main`'s
        // servo-spawn gate reads.
        let cases = [
            (None, None, false),
            (Some("enabled"), None, false), // host-clock only → inert (no ctl owner)
            (None, Some("enabled"), false), // direct only → no host-clock servo
            (Some("enabled"), Some("enabled"), true), // both → servo configured
        ];
        for (host_clock, direct, want) in cases {
            with_env(
                &[
                    ("JASPER_FANIN_HOST_CLOCK", host_clock),
                    ("JASPER_FANIN_USB_DIRECT", direct),
                ],
                || {
                    let cfg = Config::from_env().expect("parses");
                    assert_eq!(
                        cfg.host_clock_servo_armed(),
                        want,
                        "host_clock={host_clock:?} usb_direct={direct:?} \
                         → servo_armed should be {want}"
                    );
                    // Exactly the AND, with no third hidden input.
                    assert_eq!(
                        cfg.host_clock_servo_armed(),
                        cfg.host_clock_enabled && cfg.usb_direct_enabled,
                    );
                },
            );
        }
    }

    #[test]
    fn host_clock_default_off_and_probe_defaults() {
        with_env(
            &[
                ("JASPER_FANIN_HOST_CLOCK", None),
                ("JASPER_FANIN_HOST_CLOCK_PROBE_PPM", None),
            ],
            || {
                let cfg = Config::from_env().expect("defaults must parse");
                assert!(!cfg.host_clock_enabled, "host-clock defaults OFF");
                assert_eq!(cfg.host_clock_probe_ppm, 300);
            },
        );
    }

    #[test]
    fn host_clock_probe_ppm_range_fails_fast() {
        for bad in ["50", "100", "199", "801", "1200"] {
            with_env(&[("JASPER_FANIN_HOST_CLOCK_PROBE_PPM", Some(bad))], || {
                let err = Config::from_env().expect_err("out-of-range probe ppm must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("JASPER_FANIN_HOST_CLOCK_PROBE_PPM"),
                    "expected probe-ppm range error, got: {msg}"
                );
            });
        }
        for ok in ["200", "300", "800"] {
            with_env(&[("JASPER_FANIN_HOST_CLOCK_PROBE_PPM", Some(ok))], || {
                assert!(Config::from_env().is_ok(), "{ok} must be accepted");
            });
        }
    }

    #[test]
    fn host_clock_enabled_without_direct_still_parses_the_intent() {
        // The direct-off gate is a RUNTIME resolution in main (it warns
        // `event=fanin.host_clock.noop reason=usb_direct_off` and never opens the
        // ctl). Config only records the raw intent, so an enabled host-clock with
        // direct off parses fine here — the inert resolution is main's job.
        with_env(
            &[
                ("JASPER_FANIN_HOST_CLOCK", Some("enabled")),
                ("JASPER_FANIN_USB_DIRECT", None),
            ],
            || {
                let cfg = Config::from_env().expect("parses");
                assert!(cfg.host_clock_enabled);
                assert!(!cfg.usb_direct_enabled);
            },
        );
    }

    #[test]
    fn tts_program_duck_defaults_to_voice_duck_db() {
        with_env(
            &[
                ("JASPER_FANIN_TTS_PROGRAM_DUCK_DB", None),
                ("JASPER_DUCK_DB", Some("-18.5")),
            ],
            || {
                let cfg = Config::from_env().expect("duck fallback must parse");
                assert_eq!(cfg.tts_program_duck_db, -18.5);
            },
        );
    }

    #[test]
    fn legacy_silence_target_migrates_to_default_tts_envelope() {
        with_env(
            &[
                ("JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS", None),
                (
                    "JASPER_OUTPUTD_ASSISTANT_DEFAULT_SILENCE_TARGET_LUFS",
                    Some("-37.5"),
                ),
            ],
            || {
                let cfg = Config::from_env().expect("legacy envelope must parse");
                assert_eq!(cfg.assistant_loudness.default_tts_envelope_lufs, -37.5);
            },
        );
    }

    #[test]
    fn new_default_tts_envelope_wins_over_legacy_silence_target() {
        with_env(
            &[
                (
                    "JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS",
                    Some("-39.0"),
                ),
                (
                    "JASPER_OUTPUTD_ASSISTANT_DEFAULT_SILENCE_TARGET_LUFS",
                    Some("-37.5"),
                ),
            ],
            || {
                let cfg = Config::from_env().expect("new envelope must parse");
                assert_eq!(cfg.assistant_loudness.default_tts_envelope_lufs, -39.0);
            },
        );
    }

    #[test]
    fn tts_program_duck_override_wins_over_voice_duck_db() {
        with_env(
            &[
                ("JASPER_FANIN_TTS_PROGRAM_DUCK_DB", Some("-30.0")),
                ("JASPER_DUCK_DB", Some("-18.5")),
            ],
            || {
                let cfg = Config::from_env().expect("duck override must parse");
                assert_eq!(cfg.tts_program_duck_db, -30.0);
            },
        );
    }

    #[test]
    fn rejects_positive_program_duck() {
        for (_name, vars) in [
            (
                "override",
                [
                    ("JASPER_FANIN_TTS_PROGRAM_DUCK_DB", Some("3.0")),
                    ("JASPER_DUCK_DB", Some("-25.0")),
                ],
            ),
            (
                "legacy fallback",
                [
                    ("JASPER_FANIN_TTS_PROGRAM_DUCK_DB", None),
                    ("JASPER_DUCK_DB", Some("3.0")),
                ],
            ),
        ] {
            with_env(&vars, || {
                let err = Config::from_env().unwrap_err();
                assert!(err.to_string().contains("must be <= 0"), "{err}");
            });
        }
    }

    #[test]
    fn mismatched_pcm_and_renderer_lengths_error() {
        with_env(
            &[
                (
                    "JASPER_FANIN_INPUT_PCMS",
                    Some("hw:Loopback,1,0|hw:Loopback,1,1"),
                ),
                (
                    "JASPER_FANIN_INPUT_RENDERERS",
                    Some("spotify|airplay|bluealsa"),
                ),
            ],
            || {
                let err = Config::from_env().expect_err("mismatched lengths must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("must match"),
                    "expected length-mismatch error, got: {}",
                    msg,
                );
            },
        );
    }

    /// hw PCM names contain commas (`hw:Loopback,1,0`), which a comma-delimited
    /// parser splits into three entries; the pipe delimiter avoids the collision.
    #[test]
    fn pipe_delimiter_preserves_commas_inside_hw_pcm_names() {
        with_env(
            &[
                (
                    "JASPER_FANIN_INPUT_PCMS",
                    Some("hw:Loopback,1,5|hw:Loopback,1,6"),
                ),
                ("JASPER_FANIN_INPUT_RENDERERS", Some("test_a|test_b")),
            ],
            || {
                let cfg = Config::from_env().expect("pipe-delimited hw names must parse");
                assert_eq!(cfg.input_pcms.len(), 2);
                assert_eq!(cfg.input_pcms[0], "hw:Loopback,1,5");
                assert_eq!(cfg.input_pcms[1], "hw:Loopback,1,6");
                assert_eq!(cfg.input_renderers.len(), 2);
            },
        );
    }

    #[test]
    fn whitespace_only_input_pcms_errors() {
        // `env_list` drops empty/whitespace entries, so a string of only
        // delimiters parses to an empty Vec.
        with_env(
            &[
                ("JASPER_FANIN_INPUT_PCMS", Some("||")),
                ("JASPER_FANIN_INPUT_RENDERERS", Some("||")),
            ],
            || {
                let err = Config::from_env().expect_err("whitespace-only PCM list must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("empty") || msg.contains("at least one"),
                    "expected empty-list error, got: {}",
                    msg,
                );
            },
        );
    }

    #[test]
    fn input_buffer_must_be_at_least_twice_period() {
        with_env(
            &[
                ("JASPER_FANIN_PERIOD_FRAMES", Some("512")),
                ("JASPER_FANIN_INPUT_BUFFER_FRAMES", Some("512")),
            ],
            || {
                let err = Config::from_env().expect_err("buffer < 2×period must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("JASPER_FANIN_INPUT_BUFFER_FRAMES"),
                    "expected buffer-frames error, got: {}",
                    msg,
                );
            },
        );
    }

    #[test]
    fn usb_direct_period_defaults_to_256() {
        with_env(&[("JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES", None)], || {
            let cfg = Config::from_env().expect("defaults must parse");
            assert_eq!(cfg.usb_direct_period_frames, 256);
        });
    }

    #[test]
    fn usb_direct_period_accepts_h1_knob() {
        with_env(
            &[("JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES", Some("64"))],
            || {
                let cfg = Config::from_env().expect("H1 period must parse");
                assert_eq!(cfg.usb_direct_period_frames, 64);
            },
        );
    }

    #[test]
    fn usb_direct_period_fails_loud_out_of_range() {
        for bad in ["31", "1025", "0"] {
            with_env(
                &[("JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES", Some(bad))],
                || {
                    let err =
                        Config::from_env().expect_err("out-of-range direct period must error");
                    let msg = format!("{:#}", err);
                    assert!(
                        msg.contains("JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES"),
                        "expected direct-period range error, got: {msg}",
                    );
                },
            );
        }
    }

    #[test]
    fn cushion_decay_arms_only_on_literal_enabled() {
        for (raw, want) in [
            (None, false),
            (Some(""), false),
            (Some("on"), false),
            (Some("1"), false),
            (Some("true"), false),
            (Some("enabled"), true),
            (Some("Enabled"), true),
            (Some(" ENABLED "), true),
        ] {
            with_env(&[("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", raw)], || {
                let cfg = Config::from_env().expect("decay flag must parse");
                assert_eq!(
                    cfg.input_resampler_cushion_decay_enabled, want,
                    "raw={raw:?} should arm={want}"
                );
            });
        }
    }

    #[test]
    fn cushion_decay_floor_defaults_to_validated_floor() {
        // The SHIPPED default is the hardware-validated 576, not the tighter
        // derived `target + margin` (544 at the default geometry).
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", None),
                ("JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES", None),
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES", None),
            ],
            || {
                let cfg = Config::from_env().expect("defaults must parse");
                assert_eq!(
                    cfg.input_resampler_cushion_decay_floor_frames,
                    DEFAULT_CUSHION_DECAY_FLOOR_FRAMES,
                    "default floor is the validated 576, not target+margin",
                );
                // Inside the hard bounds: >= derived min 544, <= ceiling 2560.
                assert!(
                    cfg.input_resampler_cushion_decay_floor_frames
                        >= cfg.input_resampler_target_frames + CUSHION_DECAY_FLOOR_MARGIN_FRAMES,
                );
                assert!(
                    cfg.input_resampler_cushion_decay_floor_frames
                        <= cfg.input_resampler_target_frames
                            + cfg.input_resampler_warmup_cushion_frames,
                );
            },
        );
    }

    #[test]
    fn combo_armed_default_config_constructs() {
        // A gadget box's auto pass arms the USB combo (USB_DIRECT + HOST_CLOCK +
        // CUSHION_DECAY, all `enabled`) with NO explicit floor / target / cushion,
        // so the shipped 576 default must sit in range for the armed guard or such
        // a box cannot construct a Config at all.
        with_env(
            &[
                ("JASPER_FANIN_USB_DIRECT", Some("enabled")),
                ("JASPER_FANIN_HOST_CLOCK", Some("enabled")),
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", Some("enabled")),
                // Everything geometry-related left at its default.
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", None),
                ("JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES", None),
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES", None),
                ("JASPER_FANIN_PERIOD_FRAMES", None),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", None),
            ],
            || {
                let cfg = Config::from_env()
                    .expect("combo-armed default config must construct (P3 floor coherence)");
                assert!(cfg.usb_direct_enabled, "combo arms USB direct");
                assert!(cfg.host_clock_enabled, "combo arms host clock");
                assert!(
                    cfg.input_resampler_cushion_decay_enabled,
                    "combo arms cushion decay"
                );
                assert_eq!(
                    cfg.input_resampler_cushion_decay_floor_frames,
                    DEFAULT_CUSHION_DECAY_FLOOR_FRAMES,
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_fails_loud_below_margin_when_armed() {
        // target 512 + 32 = 544, so 543 is one under the minimum.
        with_env(
            &[
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("512")),
                (
                    "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES",
                    Some("543"),
                ),
            ],
            || {
                let err = Config::from_env().expect_err("floor below margin must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES"),
                    "expected decay-floor range error, got: {msg}"
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_fails_loud_above_ceiling_when_armed() {
        // Above the acquisition ceiling (target + cushion) there is nothing to
        // decay.
        with_env(
            &[
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("512")),
                (
                    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                    Some("2048"),
                ),
                (
                    "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES",
                    Some("2561"), // ceiling is 512+2048=2560
                ),
            ],
            || {
                let err = Config::from_env().expect_err("floor above ceiling must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES"),
                    "expected decay-floor ceiling error, got: {msg}"
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_fails_loud_below_minimum_safe_fill_when_armed() {
        // A small base target makes `target + margin` land BELOW the physical
        // minimum-safe-fill floor, where a floor is churn-by-construction: it sits
        // on the underfill-unlock threshold.
        //
        // target 200, period 256, max_ppm 500 → min_safe = ceil(256*1.0005)+16+1
        // = 274. floor_min = max(200+32, 274+32) = 306. A floor of 240 is above
        // target+margin (232) but below floor_min (306).
        with_env(
            &[
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("200")),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("256")),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("500")),
                (
                    "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES",
                    Some("240"),
                ),
            ],
            || {
                let err = Config::from_env().expect_err(
                    "floor below minimum-safe-fill must error even above target+margin",
                );
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES")
                        && msg.contains("minimum_safe_fill"),
                    "expected minimum-safe-fill floor error, got: {msg}"
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_default_respects_minimum_safe_fill() {
        // The DEFAULT floor must never be churn-by-construction: it sits at or
        // above `minimum_safe_fill + margin`. At target 200 / period 256 /
        // max_ppm 500 → min_safe 274 → derived floor 306, and the validated 576
        // clears it (576 > 306, 576 < ceiling 2248).
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("200")),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("256")),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("500")),
                ("JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES", None),
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES", None),
            ],
            || {
                let cfg = Config::from_env().expect("defaults must parse");
                let min_safe = jasper_resampler::minimum_safe_fill_frames(256, 500.0) as u32;
                assert!(
                    cfg.input_resampler_cushion_decay_floor_frames
                        >= min_safe + CUSHION_DECAY_FLOOR_MARGIN_FRAMES,
                    "default floor for a small target must respect the physical floor"
                );
                assert_eq!(
                    cfg.input_resampler_cushion_decay_floor_frames,
                    DEFAULT_CUSHION_DECAY_FLOOR_FRAMES,
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_default_clamps_under_ceiling_for_small_cushion() {
        // target 512 + cushion 40 = 552 ceiling < 576, derived_min = 544: the
        // default clamps down to the ceiling so the armed guard still passes.
        with_env(
            &[
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("512")),
                (
                    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                    Some("40"),
                ),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("256")),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("500")),
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES", None),
            ],
            || {
                let cfg = Config::from_env()
                    .expect("small-ceiling geometry must construct (default clamps to ceiling)");
                assert_eq!(
                    cfg.input_resampler_cushion_decay_floor_frames,
                    512 + 40,
                    "default clamps to the acquisition ceiling when it is below 576",
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_out_of_range_ignored_when_disabled() {
        with_env(
            &[
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", None),
                (
                    "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES",
                    Some("1"),
                ),
            ],
            || {
                let cfg = Config::from_env().expect("disabled decay must ignore a bad floor");
                assert!(!cfg.input_resampler_cushion_decay_enabled);
                assert_eq!(cfg.input_resampler_cushion_decay_floor_frames, 1);
            },
        );
    }

    #[test]
    fn cushion_decay_demand_beyond_authority_margin_fails_loud_when_armed() {
        // Beyond half the inner ±max_adjust_ppm authority the subtraction from
        // the host-clock observable (#3466) fabricates clock error a railed ratio
        // can never deliver. Step 6 at the legal 250 ms interval floor is already
        // over the line: ≈509.5 ppm executed against the ±500 authority.
        with_env(
            &[
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", Some("enabled")),
                (
                    "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_INTERVAL_MS",
                    Some("250"),
                ),
            ],
            || {
                let err = Config::from_env().expect_err("over-authority demand must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM"),
                    "expected the demand-vs-authority error, got: {msg}"
                );
            },
        );
    }

    #[test]
    fn cushion_decay_demand_bound_ignored_when_disabled() {
        with_env(
            &[
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", None),
                (
                    "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_INTERVAL_MS",
                    Some("250"),
                ),
            ],
            || {
                let cfg =
                    Config::from_env().expect("disabled decay must ignore an aggressive demand");
                assert!(!cfg.input_resampler_cushion_decay_enabled);
                assert_eq!(cfg.input_resampler_cushion_decay_interval_ms, 250);
            },
        );
    }

    #[test]
    fn inverted_decay_default_range_never_panics() {
        // A cushion smaller than the 32-frame working margin makes
        // derived_min > acquisition ceiling — an inverted range `u32::clamp`
        // would panic on.
        let geometry = [
            (
                "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                Some("0"),
            ),
            ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES", None),
        ];
        with_env(&geometry, || {
            let cfg = Config::from_env().expect("disabled inverted geometry stays parseable");
            assert!(!cfg.input_resampler_cushion_decay_enabled);
            assert_eq!(
                cfg.input_resampler_cushion_decay_floor_frames,
                cfg.input_resampler_target_frames,
            );
        });

        with_env(
            &[
                geometry[0],
                geometry[1],
                ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY", Some("enabled")),
            ],
            || {
                let error = Config::from_env()
                    .expect_err("armed inverted geometry must fail through validation");
                assert!(
                    error
                        .to_string()
                        .contains("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES"),
                    "{error:#}",
                );
            },
        );
    }

    #[test]
    fn cushion_decay_step_fails_loud_out_of_range() {
        for bad in ["0", "65", "1000"] {
            with_env(
                &[(
                    "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_STEP_FRAMES",
                    Some(bad),
                )],
                || {
                    let err = Config::from_env().expect_err("out-of-range step must error");
                    let msg = format!("{:#}", err);
                    assert!(
                        msg.contains("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_STEP_FRAMES"),
                        "expected decay-step range error, got: {msg}"
                    );
                },
            );
        }
    }

    #[test]
    fn cushion_decay_interval_fails_loud_out_of_range() {
        for bad in ["249", "10001", "0"] {
            with_env(
                &[(
                    "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_INTERVAL_MS",
                    Some(bad),
                )],
                || {
                    let err = Config::from_env().expect_err("out-of-range interval must error");
                    let msg = format!("{:#}", err);
                    assert!(
                        msg.contains("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_INTERVAL_MS"),
                        "expected decay-interval range error, got: {msg}"
                    );
                },
            );
        }
    }

    // ---- static held-target churn guard -----------------------------------

    #[test]
    fn static_cushion_fails_loud_on_churny_lab_geometry_when_resampler_armed() {
        // The lab geometry that produced the observed unlock churn: target 256 +
        // cushion 256 = 512 held, period 256, max_ppm 500. min_safe = 274, so the
        // required held is 274 + 256 + 32 = 562 > 512.
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("256")),
                (
                    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                    Some("256"),
                ),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("256")),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("500")),
            ],
            || {
                let err = Config::from_env()
                    .expect_err("a held target below min_safe+period+margin must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("held target") && msg.contains("churn-by-construction"),
                    "expected static-cushion churn error, got: {msg}"
                );
            },
        );
    }

    #[test]
    fn static_cushion_churn_guard_also_fires_in_usb_direct_mode() {
        // The live churn was observed in USB DIRECT mode, which arms a resampler
        // on the usbsink lane WITHOUT JASPER_FANIN_INPUT_RESAMPLER (see
        // `lane_wants_resampler`), so gating on that flag alone would miss the
        // configuration that produced the evidence.
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER", None),
                ("JASPER_FANIN_USB_DIRECT", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("256")),
                (
                    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                    Some("256"),
                ),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("256")),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("500")),
            ],
            || {
                let err = Config::from_env()
                    .expect_err("USB DIRECT with a churny held target must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("held target") && msg.contains("churn-by-construction"),
                    "expected static-cushion churn error in direct mode, got: {msg}"
                );
            },
        );
    }

    #[test]
    fn static_cushion_production_default_passes_the_churn_guard() {
        // The production default held target is 512 + 2048 = 2560.
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", None),
                ("JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES", None),
                ("JASPER_FANIN_PERIOD_FRAMES", None),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", None),
            ],
            || {
                let cfg = Config::from_env().expect("production defaults must pass the guard");
                assert_eq!(
                    cfg.input_resampler_target_frames + cfg.input_resampler_warmup_cushion_frames,
                    2560,
                );
            },
        );
    }

    #[test]
    fn static_cushion_boundary_is_exact() {
        // `held >= min_safe + period + margin`. At period 256 / max_ppm 500,
        // min_safe = 274, so the required held is 274 + 256 + 32 = 562: target 306
        // + cushion 256 = 562 passes, and one under (cushion 255 → 561) fails.
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER", Some("enabled")),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("256")),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("500")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("306")),
                (
                    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                    Some("256"),
                ),
            ],
            || {
                let cfg = Config::from_env().expect("held == required must pass (>= boundary)");
                assert_eq!(
                    cfg.input_resampler_target_frames + cfg.input_resampler_warmup_cushion_frames,
                    562,
                );
            },
        );
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER", Some("enabled")),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("256")),
                ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("500")),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("306")),
                (
                    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                    Some("255"),
                ),
            ],
            || {
                let err = Config::from_env().expect_err("held one under required must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("held target"),
                    "expected churn error, got: {msg}"
                );
            },
        );
    }

    #[test]
    fn static_cushion_churn_guard_ignored_when_resampler_off() {
        // With neither flag armed no resampler is built, so no churn is possible
        // and a churny cushion must not block boot.
        with_env(
            &[
                ("JASPER_FANIN_INPUT_RESAMPLER", None),
                ("JASPER_FANIN_USB_DIRECT", None),
                ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("256")),
                (
                    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES",
                    Some("256"),
                ),
            ],
            || {
                let cfg =
                    Config::from_env().expect("resampler-off box must ignore a churny cushion");
                assert!(!cfg.input_resampler_enabled);
                assert!(!cfg.usb_direct_enabled);
            },
        );
    }

    #[test]
    fn legacy_buffer_env_var_still_sets_input_buffer() {
        with_env(
            &[
                ("JASPER_FANIN_BUFFER_FRAMES", Some("2048")),
                ("JASPER_FANIN_INPUT_BUFFER_FRAMES", None),
            ],
            || {
                let cfg = Config::from_env().expect("legacy env must parse");
                assert_eq!(cfg.input_buffer_frames, 2048);
            },
        );
    }

    /// Which `JASPER_FANIN_CAMILLA_COUPLING` declarations this daemon will
    /// serve, now that the ring is the only transport (ADR-0100).
    ///
    /// The REFUSAL is the load-bearing half: a box still carrying a persisted
    /// `loopback` must PARK — exit 78 via [`crate::ConfigClassError`], visible on
    /// /state and doctor — not silently play over the ring the operator did not
    /// ask for. Unset / empty is "no declaration" (empty is how this repo's env
    /// writers clear a key), which the single transport serves.
    #[test]
    fn only_a_ring_declaration_or_none_is_served() {
        for (raw, served) in [
            (None, true),
            (Some(""), true),
            (Some("   "), true),
            (Some("shm_ring"), true),
            (Some(" SHM_RING "), true),
            (Some("loopback"), false),
            (Some("pipe"), false),
            (Some("transport_pipe"), false),
            (Some("ring"), false),
            (Some("shm-ring"), false),
        ] {
            with_env(
                &[("JASPER_FANIN_CAMILLA_COUPLING", raw)],
                || match Config::from_env() {
                    Ok(_) => assert!(served, "{raw:?} must be refused"),
                    Err(err) => {
                        assert!(!served, "{raw:?} must be served: {err:#}");
                        assert!(
                            err.downcast_ref::<crate::ConfigClassError>().is_some(),
                            "{raw:?} must park the unit (exit 78), not restart-loop it",
                        );
                    }
                },
            );
        }
    }

    #[test]
    fn ring_defaults_parse() {
        with_env(
            &[
                ("JASPER_FANIN_RING_PATH", None),
                ("JASPER_FANIN_RING_SLOTS", None),
            ],
            || {
                let cfg = Config::from_env().expect("ring defaults must parse");
                assert_eq!(cfg.ring_path, "/dev/shm/jts-ring/program.ring");
                assert_eq!(cfg.ring_slots, 2);
                assert_eq!(cfg.period_frames, 256);
            },
        );
    }

    /// EVERY renderer-ring-lane rejection MUST carry `ConfigClassError`.
    ///
    /// A REBOOT-LOOP guard, not a tidiness one: a bad lane map is identical on
    /// every restart, so without the marker `main` exits 1 instead of 78, the
    /// unit's `Restart=` burns `StartLimitBurst`, and `StartLimitAction=reboot`
    /// reboots the speaker every few minutes forever.
    ///
    /// Asserted per rejection rather than in bulk so a NEW rejection that
    /// forgets the marker cannot hide behind its siblings.
    #[test]
    fn every_renderer_ring_lane_rejection_parks_instead_of_rebooting() {
        // 1. A label that matches no lane.
        with_env(
            &[("JASPER_FANIN_RENDERER_RING_LANES", Some("nosuchlane"))],
            || {
                let err = Config::from_env().expect_err("an unmatched label must fail");
                assert!(
                    err.downcast_ref::<crate::ConfigClassError>().is_some(),
                    "unmatched-label rejection must PARK (exit 78), not restart-loop \
                     into StartLimitAction=reboot: {err:#}"
                );
                // Bound to THIS rejection, not merely to "some config-class error
                // happened": reordering the checks so an earlier one fires first
                // would otherwise leave this case untested and still green.
                assert!(
                    format!("{err:#}").contains("not one of this"),
                    "expected the unmatched-label rejection specifically: {err:#}"
                );
            },
        );
        // 2. period 128 with the shipped 4096 buffer derives 32 slots, above
        //    RING_SLOTS_MAX.
        with_env(
            &[
                ("JASPER_FANIN_RENDERER_RING_LANES", Some("spotify")),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("128")),
                ("JASPER_FANIN_INPUT_BUFFER_FRAMES", Some("4096")),
            ],
            || {
                let err = Config::from_env().expect_err("32 slots is out of range");
                assert!(
                    err.downcast_ref::<crate::ConfigClassError>().is_some(),
                    "inexpressible-geometry rejection must PARK: {err:#}"
                );
                assert!(
                    format!("{err:#}").contains("whole ring slots"),
                    "expected the geometry rejection specifically: {err:#}"
                );
            },
        );
        // 3. A ring lane that is ALSO the armed resampler lane.
        with_env(
            &[
                ("JASPER_FANIN_RENDERER_RING_LANES", Some("spotify")),
                ("JASPER_FANIN_INPUT_RESAMPLER", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_LANE", Some("spotify")),
            ],
            || {
                let err = Config::from_env().expect_err("ring + resampler must fail");
                assert!(
                    err.downcast_ref::<crate::ConfigClassError>().is_some(),
                    "ring+resampler rejection must PARK: {err:#}"
                );
                assert!(
                    format!("{err:#}").contains("armed \nresampler lane")
                        || format!("{err:#}").contains("resampler lane"),
                    "expected the ring+resampler rejection specifically: {err:#}"
                );
            },
        );
    }

    /// A ring lane and a `LaneResampler` on the SAME lane is refused at config.
    ///
    /// The read path renders slots straight into `read_buf` and never feeds
    /// `input.resampler`, so the combination would build a resampler, starve it,
    /// and report it armed in `/state` while it reconciled nothing.
    #[test]
    fn a_ring_lane_may_not_also_be_the_resampler_lane() {
        // The refusal is bound to the SAME lane, not to the features existing: a
        // ring lane beside a DIFFERENT resampler lane is the shipped shape.
        with_env(
            &[
                ("JASPER_FANIN_RENDERER_RING_LANES", Some("spotify")),
                ("JASPER_FANIN_INPUT_RESAMPLER", Some("enabled")),
                ("JASPER_FANIN_INPUT_RESAMPLER_LANE", Some("usbsink")),
            ],
            || {
                let cfg = Config::from_env()
                    .expect("a ring lane beside a different resampler lane is fine");
                assert!(cfg.lane_is_renderer_ring("spotify"));
                assert!(cfg.lane_wants_resampler("usbsink"));
                assert!(!cfg.lane_wants_resampler("spotify"));
            },
        );
        with_env(
            &[
                ("JASPER_FANIN_RENDERER_RING_LANES", Some("usbsink")),
                ("JASPER_FANIN_USB_DIRECT", Some("enabled")),
            ],
            || {
                Config::from_env()
                    .expect_err("usb-direct implies a resampler on usbsink; ring must refuse");
            },
        );
    }

    /// DEFAULT BAR: with `JASPER_FANIN_RING_WIRE_FORMAT` unset the resolved wire
    /// is the WIDE one. Narrow is a width regression on the hop the ring
    /// replaces (the loopback CamillaDSP -> outputd hop already carries S32_LE),
    /// so the fleet converges without declaring anything and `S16_LE` becomes an
    /// operator's rollback pin. Cleared-to-empty means the same as unset (that
    /// is how this repo's env-file writers disable a key).
    ///
    /// Python's `resolve_ring_wire_format` answers identically;
    /// `tests/test_ring_wire_format_contract.py` reads this arm to pin it.
    #[test]
    fn ring_wire_format_defaults_to_wide() {
        assert_eq!(
            RingWireFormat::from_env_value(None).unwrap(),
            RingWireFormat::S32Le
        );
        assert_eq!(
            RingWireFormat::from_env_value(Some("")).unwrap(),
            RingWireFormat::S32Le
        );
        assert_eq!(
            RingWireFormat::from_env_value(Some("  ")).unwrap(),
            RingWireFormat::S32Le
        );
        assert_eq!(
            RingWireFormat::S32Le.sample_format_id(),
            jasper_ring::SAMPLE_FORMAT_S32LE
        );
        assert_eq!(
            RingWireFormat::from_env_value(Some("S16_LE")).unwrap(),
            RingWireFormat::S16Le
        );
    }

    #[test]
    fn ring_wire_format_accepts_both_wire_tokens() {
        assert_eq!(
            RingWireFormat::from_env_value(Some("S16_LE")).unwrap(),
            RingWireFormat::S16Le
        );
        assert_eq!(
            RingWireFormat::from_env_value(Some(" S32_LE ")).unwrap(),
            RingWireFormat::S32Le
        );
        assert_eq!(
            RingWireFormat::S32Le.sample_format_id(),
            jasper_ring::SAMPLE_FORMAT_S32LE
        );
    }

    /// An unknown token FAILS LOUD rather than silently resolving to the
    /// default — and carries the config-class marker, so the unit parks at
    /// exit 78 instead of restart-looping into StartLimitAction=reboot.
    ///
    /// The match is EXACT, matching the ioplug's own `strcmp`: a case-folded
    /// spelling fan-in accepted but the ioplug rejected would let fan-in build
    /// a geometry no reader on the other end can declare.
    #[test]
    fn ring_wire_format_rejects_unknown_and_mis_cased_tokens() {
        for bad in ["S24_3LE", "s32_le", "S32LE", "FLOAT_LE", "32", "yes"] {
            let err = RingWireFormat::from_env_value(Some(bad))
                .expect_err("an unsupported wire token must fail loud");
            let msg = format!("{:#}", err);
            assert!(
                msg.contains("JASPER_FANIN_RING_WIRE_FORMAT"),
                "expected a wire-format error naming the key, got: {}",
                msg,
            );
            assert!(
                err.downcast_ref::<crate::ConfigClassError>().is_some(),
                "an unparseable wire is config-class (park at 78), got: {}",
                msg,
            );
        }
    }

    /// The vocabulary has ONE owner: every token round-trips token → header id
    /// → token, so the spelling fan-in accepts is the spelling STATUS reports.
    #[test]
    fn ring_wire_format_round_trips_through_the_header_id() {
        for format in [RingWireFormat::S16Le, RingWireFormat::S32Le] {
            assert_eq!(
                RingWireFormat::from_sample_format_id(format.sample_format_id()),
                Some(format),
            );
            assert_eq!(
                RingWireFormat::from_env_value(Some(format.as_str())).unwrap(),
                format,
            );
        }
        assert_eq!(RingWireFormat::from_sample_format_id(0), None);
        assert_eq!(RingWireFormat::from_sample_format_id(3), None);
    }

    #[test]
    fn shm_ring_wire_format_reaches_the_config() {
        with_env(
            &[
                ("JASPER_FANIN_CAMILLA_COUPLING", Some("shm_ring")),
                ("JASPER_FANIN_RING_WIRE_FORMAT", None),
            ],
            || {
                let cfg = Config::from_env().expect("unset wire format must parse");
                assert_eq!(cfg.ring_wire_format, RingWireFormat::S32Le);
            },
        );
        with_env(
            &[
                ("JASPER_FANIN_CAMILLA_COUPLING", Some("shm_ring")),
                ("JASPER_FANIN_RING_WIRE_FORMAT", Some("S16_LE")),
            ],
            || {
                let cfg = Config::from_env().expect("the operator's narrow pin must parse");
                assert_eq!(cfg.ring_wire_format, RingWireFormat::S16Le);
            },
        );
        with_env(
            &[
                ("JASPER_FANIN_CAMILLA_COUPLING", Some("shm_ring")),
                ("JASPER_FANIN_RING_WIRE_FORMAT", Some("S32_LE")),
            ],
            || {
                let cfg = Config::from_env().expect("S32_LE must parse");
                assert_eq!(cfg.ring_wire_format, RingWireFormat::S32Le);
            },
        );
        with_env(
            &[
                ("JASPER_FANIN_CAMILLA_COUPLING", Some("shm_ring")),
                ("JASPER_FANIN_RING_WIRE_FORMAT", Some("bogus")),
            ],
            || {
                Config::from_env().expect_err("an unknown wire token must fail the whole parse");
            },
        );
    }

    #[test]
    fn shm_ring_ring_path_and_slots_override() {
        with_env(
            &[
                ("JASPER_FANIN_CAMILLA_COUPLING", Some("shm_ring")),
                ("JASPER_FANIN_RING_PATH", Some("/dev/shm/jts-ring/lab.ring")),
                ("JASPER_FANIN_RING_SLOTS", Some("16")),
            ],
            || {
                let cfg = Config::from_env().expect("shm_ring overrides must parse");
                assert_eq!(cfg.ring_path, "/dev/shm/jts-ring/lab.ring");
                assert_eq!(cfg.ring_slots, 16);
            },
        );
    }

    #[test]
    fn shm_ring_slots_out_of_range_fails_loud() {
        // Out-of-range values plus a non-numeric one: both reject the same key, so
        // both must carry the config-class marker or the parse failure
        // restart-loops.
        for bad in ["1", "17", "0", "100", "abc"] {
            with_env(
                &[
                    ("JASPER_FANIN_CAMILLA_COUPLING", Some("shm_ring")),
                    ("JASPER_FANIN_RING_SLOTS", Some(bad)),
                ],
                || {
                    let err = Config::from_env().expect_err("out-of-range ring slots must error");
                    let msg = format!("{:#}", err);
                    assert!(
                        msg.contains("JASPER_FANIN_RING_SLOTS"),
                        "expected ring-slots range error, got: {}",
                        msg,
                    );
                    assert!(
                        err.downcast_ref::<crate::ConfigClassError>().is_some(),
                        "a bad ring geometry must park at 78, not restart-loop \
                         into StartLimitAction=reboot: {}",
                        msg,
                    );
                },
            );
        }
    }

    #[test]
    fn ring_period_must_be_multiple_of_slot_frames() {
        // 200 is not a multiple of 128, so a step would shear a slot.
        with_env(
            &[
                ("JASPER_FANIN_PERIOD_FRAMES", Some("200")),
                // 4096 >= 2*200 input-buffer floor, so that guard passes and
                // the slot-shear guard is what fires.
                ("JASPER_FANIN_INPUT_BUFFER_FRAMES", Some("4096")),
            ],
            || {
                let err = Config::from_env().expect_err("non-128-multiple period must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("multiple") && msg.contains("slot"),
                    "expected slot-shear error, got: {}",
                    msg,
                );
                assert!(
                    err.downcast_ref::<crate::ConfigClassError>().is_some(),
                    "a sheared ring geometry must park at 78, not restart-loop \
                     into StartLimitAction=reboot: {}",
                    msg,
                );
            },
        );
    }

    #[test]
    fn bad_integer_env_var_returns_clear_error() {
        with_env(
            &[("JASPER_FANIN_SAMPLE_RATE", Some("not-a-number"))],
            || {
                let err = Config::from_env().expect_err("bad integer must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("JASPER_FANIN_SAMPLE_RATE"),
                    "error message should name the offending var, got: {}",
                    msg,
                );
            },
        );
    }

    /// The per-box program width (#2223) follows the WIRE FORMAT alone: the
    /// transport half of the conjunction is `true` on every box (ADR-0100).
    ///
    /// The unset row matters most. The wire resolver defaults WIDE, so an
    /// undeclared box arms the widened source path (spine-scale lane buffers, the
    /// `AUDIO32` assistant verb, the wide earcon bake) with no declaration at
    /// all, and an operator's `S16_LE` is the pin that narrows it.
    #[test]
    fn the_program_width_follows_the_declared_wire_format() {
        for (wire, expected) in [
            (None, true),
            (Some("S32_LE"), true),
            (Some("S16_LE"), false),
        ] {
            with_env(&[("JASPER_FANIN_RING_WIRE_FORMAT", wire)], || {
                let cfg = Config::from_env().expect("defaults must parse");
                assert_eq!(cfg.program_wire_is_wide(), expected, "wire={wire:?}");
            });
        }
    }

    /// The STARTUP WIDTH LINE, asserted by CONTENT rather than by presence: a
    /// line that said only "narrow" would leave a support read unable to tell a
    /// narrow-format box from a wide-format one, which is the distinction the
    /// line exists to draw.
    #[test]
    fn the_startup_width_line_names_the_verdict_and_the_declared_format() {
        for (wire, verb, sample_bytes) in [("S32_LE", "AUDIO32", "4"), ("S16_LE", "AUDIO", "2")] {
            with_env(&[("JASPER_FANIN_RING_WIRE_FORMAT", Some(wire))], || {
                let line = Config::from_env()
                    .expect("defaults must parse")
                    .assistant_wire_resolved_line();
                assert!(line.starts_with("event=fanin.tts_wire.resolved "), "{line}",);
                assert!(line.contains(&format!("verb={verb}")), "{line}");
                assert!(line.contains(&format!("wire_format={wire}")), "{line}");
                assert!(
                    line.contains(&format!("sample_bytes={sample_bytes}")),
                    "{line}",
                );
            });
        }
    }
}
