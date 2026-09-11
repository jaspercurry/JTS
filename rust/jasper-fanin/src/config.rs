// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Configuration loaded from `JASPER_FANIN_*` environment variables.
//!
//! This module owns the defaults. `.env.example` documents the operator-facing
//! subset as prose rather than seeded literals: install.sh copies that file to
//! `/etc/jasper/jasper.env` once and never re-syncs it, so a literal there
//! would pin the default on every existing Pi. Where a key appears in both, the
//! two must agree.
//!
//! Operator overrides go in `/etc/jasper/jasper.env` (system-wide);
//! `/var/lib/jasper/fanin.env` is single-writer, owned by
//! `jasper.fanin.coupling_reconcile`.

use anyhow::Result;
use jasper_env::{env_f32, env_parse, env_str};

use jasper_tts_protocol::loudness::AssistantLoudnessConfig;

/// The SHM ring's pinned slot size in frames (Ring A), re-exported from the
/// crate that owns the ring geometry so fan-in and outputd read one constant.
/// Matches the outputd DAC-period contract and the ring header geometry; fan-in
/// publishes `period_frames / RING_SLOT_FRAMES` slots per mixer step, and the
/// `period_frames % RING_SLOT_FRAMES == 0` config guard is the drift catch.
pub use jasper_ring::RING_SLOT_FRAMES;

/// The ring's `n_slots` bounds (Ring A), re-exported from the crate that owns
/// the ring geometry so the header's own validation at attach cannot disagree.
/// A present out-of-range value FAILS LOUD here (`Config::from_env` bails) —
/// Python's `fanin_coupling.resolve_ring_slots` raises on the same range, so
/// the two normalizers agree on the drift axis (unset => default 2;
/// out-of-range => error on BOTH sides, never a silent clamp).
pub use jasper_ring::{MAX_N_SLOTS as RING_SLOTS_MAX, MIN_N_SLOTS as RING_SLOTS_MIN};

/// Compile-time render geometry: the defaults `JASPER_FANIN_PERIOD_FRAMES` and
/// `JASPER_FANIN_SAMPLE_RATE` resolve to. Named because the mixer's
/// period-counted cadences convert their millisecond intent at this geometry
/// ([`periods_for_ms`]) — an operator override drifts them in wall time.
pub(crate) const DEFAULT_PERIOD_FRAMES: u32 = 256;
pub(crate) const DEFAULT_SAMPLE_RATE: u32 = 48_000;

/// Render periods spanning `ms` at a lane geometry, floored at 1 so a
/// sub-period interval still ticks. The crate's ONE ms→periods conversion:
/// every period-counted cadence states its wall-clock intent and derives the
/// count here rather than shipping a hand-multiplied literal. `sample_rate` is
/// the caller's guarantee (`env_u32_positive` refuses 0); `period_frames` is
/// guarded here because the constants call this before that parse runs.
pub(crate) const fn periods_for_ms(ms: u64, period_frames: u32, sample_rate: u32) -> u64 {
    let period_frames = if period_frames == 0 {
        1
    } else {
        period_frames as u64
    };
    let sample_rate = sample_rate as u64;
    let periods = ms.saturating_mul(sample_rate) / (1000 * period_frames);
    if periods == 0 {
        1
    } else {
        periods
    }
}

/// Label of the measurement / diagnostic injection lane — the one lane that
/// carries stimuli rather than program.
///
/// Two behaviours key off this identity and must agree about it: the lane is
/// always mixed regardless of selection, so diagnostics keep working when the
/// household has pinned a source ([`crate::mixer`]'s selection gate), and its
/// stimuli are never onset-shaped, because the measurement loop deconvolves
/// against the signal it believes it played (`mixer::lane_fade`).
pub const MEASUREMENT_LANE: &str = "correction";

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

    /// The lane LABEL (matched against `input_renderers`) that crosses the
    /// foreign USB clock: the one lane that reads no aloop substream, gets a
    /// `LaneResampler` (`src/lane_resampler.rs`), and is either the
    /// `hw:UAC2Gadget` direct capture (`usb_direct_enabled`) or absent —
    /// rendered as silence. Only ONE lane crosses a foreign clock, so this is a
    /// single label, not a set. Env: `JASPER_FANIN_INPUT_RESAMPLER_LANE`
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

    /// Adaptive USB input buffer. Requires the host-clock DLL.
    pub input_resampler_cushion_decay_enabled: bool,
    /// The total held-target floor (frames) the decay descends to. Must be at
    /// least `max(target, minimum_safe_fill_frames)` plus
    /// [`CUSHION_DECAY_FLOOR_MARGIN_FRAMES`], and at most the acquisition
    /// ceiling (`target + warmup cushion`); `from_env` validates both fail-loud
    /// once the decay is armed. Defaults to
    /// [`DEFAULT_CUSHION_DECAY_FLOOR_FRAMES`] clamped into that range. Env:
    /// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES`.
    pub input_resampler_cushion_decay_floor_frames: u32,

    /// DEFAULT-OFF USB DIRECT capture. When `true`, the lane labelled
    /// `input_resampler_lane_label` (the usbsink lane) does NOT read its
    /// snd-aloop substream; the mixer opens `usb_direct_device`
    /// (`hw:UAC2Gadget`) as an S32_LE capture and feeds the SAME
    /// `LaneResampler` the gadget's `i32` untouched. This deletes the usbsink
    /// bridge hop and the aloop cable — ~25 ms measured — from the USB path.
    /// Direct mode IMPLIES a resampler on that lane (see
    /// [`Config::lane_wants_resampler`]); with direct off the lane opens
    /// nothing at all. Env: `JASPER_FANIN_USB_DIRECT` (only the literal
    /// `enabled` arms it).
    pub usb_direct_enabled: bool,

    /// The ALSA capture device the USB DIRECT lane opens when `usb_direct_enabled`.
    /// Default `hw:UAC2Gadget` (the UAC2 gadget card fan-in owns while the USB
    /// source is armed). Unused when direct is off. Env:
    /// `JASPER_FANIN_USB_DIRECT_DEVICE`.
    pub usb_direct_device: String,

    /// The gadget capture OPEN period (frames) the USB DIRECT lane negotiates.
    /// Defaults to [`crate::mixer::DIRECT_PERIOD_FRAMES`], the
    /// hardware-validated direct-capture envelope; fail-loud range 32..=1024.
    /// Shrinking it (e.g. 64) exposes ready frames sooner if the gadget's
    /// readable `avail` advances in period-sized steps. The capture
    /// BUFFER stays DEEP regardless (`mixer::resolve_direct_buffer_frames`:
    /// ≥ 3 periods AND ≥ 768 frames), so a small period rides a deep buffer
    /// rather than the refuted shallow 2-period URB headroom. Unused when direct
    /// is off. Env: `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES`.
    pub usb_direct_period_frames: u32,

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
    /// `LaneResampler`. Only the USB DIRECT lane: it has no aloop catch-up
    /// fallback to reconcile the host↔DAC rate gap, so it MUST own a
    /// resampler. Off with direct disabled — that lane then opens nothing and
    /// renders silence.
    pub fn lane_wants_resampler(&self, label: &str) -> bool {
        self.usb_direct_enabled && label == self.input_resampler_lane_label
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

    /// Read JASPER_FANIN_* env vars, falling back to documented defaults.
    /// Returns `Err` only on structural misconfiguration (e.g., input
    /// PCM list length != renderer label list length).
    pub fn from_env() -> Result<Self> {
        // snd-aloop pair 3 is deliberately absent: the USB lane
        // (`input_resampler_lane_label`) reads the gadget capture directly or
        // nothing at all, so it takes no aloop substream and the surviving
        // pairs do not renumber.
        let input_pcms = env_list(
            "JASPER_FANIN_INPUT_PCMS",
            &[
                "hw:Loopback,1,0",
                "hw:Loopback,1,1",
                "hw:Loopback,1,2",
                "hw:Loopback,1,4",
            ],
        );
        // `from_env_uses_documented_defaults` pins the parsed
        // `input_renderers[4] == MEASUREMENT_LANE`.
        let input_renderers = env_list(
            "JASPER_FANIN_INPUT_RENDERERS",
            &["spotify", "airplay", "bluealsa", "usbsink", "correction"],
        );
        let input_resampler_lane_label = env_str("JASPER_FANIN_INPUT_RESAMPLER_LANE", "usbsink");
        // The USB lane is the one label with no aloop PCM; the rest pair
        // positionally in order.
        let aloop_lanes = input_renderers
            .iter()
            .filter(|label| *label != &input_resampler_lane_label)
            .count();
        if input_pcms.len() != aloop_lanes {
            anyhow::bail!(
                "JASPER_FANIN_INPUT_PCMS has {} entries but JASPER_FANIN_INPUT_RENDERERS has {} \
                 aloop lanes ({} labels, minus the USB lane '{}', which reads no aloop \
                 substream) — must match positionally",
                input_pcms.len(),
                aloop_lanes,
                input_renderers.len(),
                input_resampler_lane_label,
            );
        }
        if input_pcms.is_empty() {
            anyhow::bail!(
                "JASPER_FANIN_INPUT_PCMS is empty — daemon needs at least \
                 one input substream to mix"
            );
        }

        let sample_rate = env_u32_positive("JASPER_FANIN_SAMPLE_RATE", DEFAULT_SAMPLE_RATE)?;
        let period_frames = env_u32_positive("JASPER_FANIN_PERIOD_FRAMES", DEFAULT_PERIOD_FRAMES)?;
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

        // Fan-in creates the ring S32_LE unconditionally, so this key selects
        // nothing either — but the Python reconciler still reads it to render
        // the ioplug conf.d, so a stale `S16_LE` would leave the two halves of
        // the box describing different wires. Refuse the declaration instead.
        // Unset / empty is "no declaration" (empty is how this repo's env
        // writers clear a key). The token is compared exactly, as spelled in
        // the ALSA `format` field.
        let ring_wire_format = std::env::var("JASPER_FANIN_RING_WIRE_FORMAT").ok();
        match ring_wire_format.as_deref().map(str::trim) {
            None | Some("") | Some("S32_LE") => {}
            Some(other) => {
                return Err(anyhow::anyhow!(
                    "JASPER_FANIN_RING_WIRE_FORMAT={other} unsupported (S32_LE) — \
                     fan-in publishes the program wire S32_LE unconditionally, so \
                     a narrower declaration would shear against the ring header \
                     rather than narrow the program",
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
            input_resampler_max_adjust_ppm as f64 + crate::lane_resampler::BUFFER_ADJUST_PPM,
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
        let usb_direct_enabled = env_enabled("JASPER_FANIN_USB_DIRECT");
        let usb_direct_device = env_str("JASPER_FANIN_USB_DIRECT_DEVICE", "hw:UAC2Gadget");
        // Range 32..=1024: below 32 the period IRQ storms the mixer thread, above
        // 1024 the open period exceeds the deep-buffer floor's own headroom and
        // defeats the low-latency intent. Only consulted on the direct lane, but
        // parsed unconditionally so a typo fails loud on any boot, not only on an
        // armed box.
        let usb_direct_period_frames = env_u32(
            "JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES",
            crate::mixer::DIRECT_PERIOD_FRAMES,
        )?;
        if !(32..=1024).contains(&usb_direct_period_frames) {
            anyhow::bail!(
                "JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES={} out of range 32..=1024 (the gadget \
                 open period; 256 is the bridge-proven default, 64 is the lever-2 H1 test knob)",
                usb_direct_period_frames,
            );
        }

        // STATIC held-target churn guard — the symmetric sibling of the
        // decay-floor validation above, entered through the static cushion knobs.
        // The armed lane (JASPER_FANIN_USB_DIRECT=enabled — the direct lane has
        // no aloop catch-up fallback, so it always builds a resampler) holds the
        // ring at `target + cushion` and renders ONE `period_frames` each step, so the
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
        if usb_direct_enabled {
            let min_safe = cushion_decay_min_safe_fill;
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
                    "resampler held target (target {} + warm-up cushion {} \
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

        // The JASPER_OUTPUTD_* spellings of the assistant loudness keys are
        // read as fallbacks for one release; drop the fallback names next.
        let max_peak_dbfs = env_f32_fallback(
            "JASPER_FANIN_ASSISTANT_MAX_PEAK_DBFS",
            "JASPER_OUTPUTD_ASSISTANT_MAX_PEAK_DBFS",
            loudness_defaults.max_peak_dbfs,
        )?;
        if max_peak_dbfs > 0.0 {
            anyhow::bail!(
                "JASPER_FANIN_ASSISTANT_MAX_PEAK_DBFS (or its JASPER_OUTPUTD_ fallback)={} \
                 must be <= 0 (a peak ceiling above full scale is never allowed)",
                max_peak_dbfs
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
                assistant_offset_lu: env_f32_fallback(
                    "JASPER_FANIN_ASSISTANT_OFFSET_LU",
                    "JASPER_OUTPUTD_ASSISTANT_OFFSET_LU",
                    loudness_defaults.assistant_offset_lu,
                )?,
                max_peak_dbfs,
                fallback_source_lufs: env_f32_fallback(
                    "JASPER_FANIN_ASSISTANT_FALLBACK_SOURCE_LUFS",
                    "JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_LUFS",
                    loudness_defaults.fallback_source_lufs,
                )?,
                fallback_source_peak_dbfs: env_f32_fallback(
                    "JASPER_FANIN_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS",
                    "JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS",
                    loudness_defaults.fallback_source_peak_dbfs,
                )?,
                default_tts_envelope_lufs: env_f32_fallback(
                    "JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS",
                    "JASPER_OUTPUTD_ASSISTANT_DEFAULT_SILENCE_TARGET_LUFS",
                    loudness_defaults.default_tts_envelope_lufs,
                )?,
                content_silence_lufs: env_f32_fallback(
                    "JASPER_FANIN_CONTENT_SILENCE_LUFS",
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
            input_resampler_lane_label,
            input_resampler_target_frames,
            input_resampler_max_adjust_ppm,
            input_resampler_warmup_cushion_frames,
            input_resampler_ring_frames,
            input_resampler_cushion_decay_enabled,
            input_resampler_cushion_decay_floor_frames,
            usb_direct_enabled,
            usb_direct_device,
            usb_direct_period_frames,
            host_clock_enabled,
            host_clock_probe_ppm,
        })
    }
}

// ---- env var helpers ------------------------------------------------

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

    /// Whether an error carries the config-class marker. That marker is the only
    /// externally observable difference between the two failure classes: a marked
    /// error exits 78 and `RestartPreventExitStatus=78` parks the unit, an
    /// unmarked one takes the ordinary restart ladder into
    /// `StartLimitAction=reboot`.
    fn parks_the_unit(err: &anyhow::Error) -> bool {
        err.downcast_ref::<crate::ConfigClassError>().is_some()
    }

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
    fn periods_for_ms_converts_at_the_shipped_geometry() {
        // 1000 ms at 48 kHz / 256 ≈ 187.5 periods, truncated.
        assert_eq!(periods_for_ms(1000, 256, 48_000), 187);
        assert_eq!(periods_for_ms(10_000, 256, 48_000), 1875);
        // Sub-period intent still ticks: floored at one period.
        assert_eq!(periods_for_ms(1, 256, 48_000), 1);
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
                ("JASPER_FANIN_RING_WIRE_FORMAT", None),
                ("JASPER_FANIN_TTS_SOCKET", None),
                ("JASPER_FANIN_TTS_MAX_PENDING_FRAMES", None),
                ("JASPER_FANIN_TTS_PROGRAM_DUCK_DB", None),
                ("JASPER_FANIN_TTS_CUE_DUCK_DB", None),
                ("JASPER_FANIN_ASSISTANT_OFFSET_LU", None),
                ("JASPER_FANIN_ASSISTANT_MAX_PEAK_DBFS", None),
                ("JASPER_FANIN_ASSISTANT_FALLBACK_SOURCE_LUFS", None),
                ("JASPER_FANIN_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS", None),
                ("JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS", None),
                ("JASPER_FANIN_CONTENT_SILENCE_LUFS", None),
                ("JASPER_FANIN_ASSISTANT_REFERENCE_PATH", None),
                ("JASPER_DUCK_DB", None),
            ],
            || {
                let cfg = Config::from_env().expect("defaults must parse");
                // FOUR aloop PCMs for FIVE labels: the usbsink lane reads the
                // gadget capture or nothing, never an aloop substream, and the
                // surviving pairs do not renumber around the gap.
                assert_eq!(cfg.input_pcms.len(), 4);
                assert!(!cfg.input_pcms.iter().any(|p| p == "hw:Loopback,1,3"));
                assert_eq!(cfg.input_pcms[3], "hw:Loopback,1,4");
                assert_eq!(cfg.input_renderers.len(), 5);
                assert_eq!(cfg.input_renderers[0], "spotify");
                assert_eq!(cfg.input_renderers[3], "usbsink");
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
        with_env(&[("JASPER_FANIN_USB_DIRECT", None)], || {
            let cfg = Config::from_env().unwrap();
            assert!(!cfg.usb_direct_enabled);
            assert!(
                !cfg.lane_wants_resampler("usbsink"),
                "no resampler on any lane with direct off"
            );
        });
    }

    #[test]
    fn usb_direct_implies_resampler_on_the_usbsink_lane() {
        // Direct capture has no aloop catch-up fallback, so its lane owns a
        // resampler — and only that lane does.
        with_env(&[("JASPER_FANIN_USB_DIRECT", Some("enabled"))], || {
            let cfg = Config::from_env().unwrap();
            assert!(cfg.usb_direct_enabled);
            assert!(
                cfg.lane_wants_resampler("usbsink"),
                "direct mode must imply a resampler on the usbsink lane"
            );
            assert!(
                !cfg.lane_wants_resampler("airplay"),
                "only the resampler lane label gets one"
            );
        });
    }

    #[test]
    fn planned_lane_source_follows_the_resampler_predicate() {
        use crate::mixer::{planned_lane_source, LaneSource};

        // The three transports a lane can be planned with. Pinned here rather
        // than in mixer.rs: the decision reads a `Config`, which only these
        // env-backed tests can build.
        for (direct, label, expected) in [
            (None, "usbsink", LaneSource::Disabled),
            (Some("enabled"), "usbsink", LaneSource::Direct),
            (None, "airplay", LaneSource::Lane),
            (Some("enabled"), "airplay", LaneSource::Lane),
        ] {
            with_env(&[("JASPER_FANIN_USB_DIRECT", direct)], || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(
                    planned_lane_source(&cfg, label),
                    expected,
                    "lane {label} with JASPER_FANIN_USB_DIRECT={direct:?}"
                );
            });
        }
    }

    #[test]
    fn input_resampler_knobs_parse_overrides() {
        with_env(
            &[
                // The lane label picks WHICH label reads no aloop substream, so
                // the roster moves with it.
                ("JASPER_FANIN_INPUT_RENDERERS", Some("spotify|usbsink2")),
                ("JASPER_FANIN_INPUT_PCMS", Some("hw:Loopback,1,0")),
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
                assert!(
                    !parks_the_unit(&err),
                    "must restart-loop, not park at 78: {err:#}"
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
    fn assistant_loudness_keys_read_fanin_spelling_then_outputd_fallback() {
        type Read = fn(&AssistantLoudnessConfig) -> f32;
        let pairs: [(&str, &str, Read); 6] = [
            (
                "JASPER_FANIN_ASSISTANT_OFFSET_LU",
                "JASPER_OUTPUTD_ASSISTANT_OFFSET_LU",
                |c| c.assistant_offset_lu,
            ),
            (
                "JASPER_FANIN_ASSISTANT_MAX_PEAK_DBFS",
                "JASPER_OUTPUTD_ASSISTANT_MAX_PEAK_DBFS",
                |c| c.max_peak_dbfs,
            ),
            (
                "JASPER_FANIN_ASSISTANT_FALLBACK_SOURCE_LUFS",
                "JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_LUFS",
                |c| c.fallback_source_lufs,
            ),
            (
                "JASPER_FANIN_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS",
                "JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS",
                |c| c.fallback_source_peak_dbfs,
            ),
            (
                "JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS",
                "JASPER_OUTPUTD_ASSISTANT_DEFAULT_SILENCE_TARGET_LUFS",
                |c| c.default_tts_envelope_lufs,
            ),
            (
                "JASPER_FANIN_CONTENT_SILENCE_LUFS",
                "JASPER_OUTPUTD_CONTENT_SILENCE_LUFS",
                |c| c.content_silence_lufs,
            ),
        ];
        for (new_key, old_key, read) in pairs {
            with_env(&[(new_key, None), (old_key, Some("-37.5"))], || {
                let cfg = Config::from_env().expect("legacy spelling must parse");
                assert_eq!(read(&cfg.assistant_loudness), -37.5, "{old_key}");
            });
            with_env(
                &[(new_key, Some("-39.0")), (old_key, Some("-37.5"))],
                || {
                    let cfg = Config::from_env().expect("new spelling must parse");
                    assert_eq!(read(&cfg.assistant_loudness), -39.0, "{new_key}");
                },
            );
        }
    }

    #[test]
    fn positive_assistant_peak_ceiling_is_rejected() {
        with_env(
            &[("JASPER_FANIN_ASSISTANT_MAX_PEAK_DBFS", Some("0.5"))],
            || assert!(Config::from_env().is_err()),
        );
        with_env(
            &[("JASPER_FANIN_ASSISTANT_MAX_PEAK_DBFS", Some("0"))],
            || {
                assert_eq!(
                    Config::from_env().unwrap().assistant_loudness.max_peak_dbfs,
                    0.0
                )
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
                assert!(
                    !parks_the_unit(&err),
                    "must restart-loop, not park at 78: {err:#}"
                );
            },
        );
    }

    #[test]
    fn usb_direct_period_defaults_to_the_proven_open_envelope() {
        with_env(&[("JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES", None)], || {
            let cfg = Config::from_env().expect("defaults must parse");
            assert_eq!(
                cfg.usb_direct_period_frames,
                crate::mixer::DIRECT_PERIOD_FRAMES
            );
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
                    assert!(
                        !parks_the_unit(&err),
                        "must restart-loop, not park at 78: {err:#}"
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
                assert!(
                    !parks_the_unit(&err),
                    "must restart-loop, not park at 78: {err:#}"
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
                assert!(
                    !parks_the_unit(&err),
                    "must restart-loop, not park at 78: {err:#}"
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
                assert!(
                    !parks_the_unit(&err),
                    "must restart-loop, not park at 78: {err:#}"
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_default_respects_minimum_safe_fill() {
        for (period, expected_floor) in [("256", 576), ("1024", 1076)] {
            with_env(
                &[
                    ("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", Some("200")),
                    ("JASPER_FANIN_PERIOD_FRAMES", Some(period)),
                    ("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", Some("500")),
                    ("JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES", None),
                    ("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES", None),
                ],
                || {
                    let cfg = Config::from_env().expect("defaults must parse");
                    assert_eq!(
                        cfg.input_resampler_cushion_decay_floor_frames,
                        expected_floor
                    );
                },
            );
        }
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
    fn static_cushion_fails_loud_on_churny_lab_geometry_when_resampler_armed() {
        // The lab geometry that produced the observed unlock churn, in the mode
        // that produced the evidence (USB DIRECT — the only mode that arms a
        // resampler): target 256 + cushion 256 = 512 held, period 256, max_ppm
        // 500. min_safe = 274, so the required held is 274 + 256 + 32 = 562 > 512.
        with_env(
            &[
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
    fn static_cushion_production_default_passes_the_churn_guard() {
        // The production default held target is 512 + 2048 = 2560.
        with_env(
            &[
                ("JASPER_FANIN_USB_DIRECT", Some("enabled")),
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
                ("JASPER_FANIN_USB_DIRECT", Some("enabled")),
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
                ("JASPER_FANIN_USB_DIRECT", Some("enabled")),
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
        // With direct off no resampler is built, so no churn is possible and a
        // churny cushion must not block boot.
        with_env(
            &[
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
                            parks_the_unit(&err),
                            "{raw:?} must park the unit (exit 78), not restart-loop it",
                        );
                    }
                },
            );
        }
    }

    /// Which `JASPER_FANIN_RING_WIRE_FORMAT` declarations this daemon will
    /// serve, now that fan-in creates the ring S32_LE unconditionally.
    ///
    /// The REFUSAL is the load-bearing half: the Python reconciler still reads
    /// this key to render the ioplug conf.d, so a box still carrying `S16_LE`
    /// must PARK — exit 78 via [`crate::ConfigClassError`] — rather than let the
    /// two halves of the box describe different wires.
    #[test]
    fn only_an_s32_wire_declaration_or_none_is_served() {
        for (raw, served) in [
            (None, true),
            (Some(""), true),
            (Some(" S32_LE "), true),
            (Some("S16_LE"), false),
            (Some("s32_le"), false),
        ] {
            with_env(
                &[("JASPER_FANIN_RING_WIRE_FORMAT", raw)],
                || match Config::from_env() {
                    Ok(_) => assert!(served, "{raw:?} must be refused"),
                    Err(err) => {
                        assert!(!served, "{raw:?} must be served: {err:#}");
                        assert!(
                            parks_the_unit(&err),
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
                    assert!(
                        parks_the_unit(&err),
                        "a bad ring geometry must park at 78, not restart-loop \
                         into StartLimitAction=reboot: {err:#}",
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
                assert!(
                    parks_the_unit(&err),
                    "a sheared ring geometry must park at 78, not restart-loop \
                     into StartLimitAction=reboot: {err:#}",
                );
            },
        );
    }

    #[test]
    fn bad_integer_env_var_takes_the_restart_ladder() {
        with_env(
            &[("JASPER_FANIN_SAMPLE_RATE", Some("not-a-number"))],
            || {
                let err = Config::from_env().expect_err("bad integer must error");
                assert!(
                    !parks_the_unit(&err),
                    "must restart-loop, not park at 78: {err:#}"
                );
            },
        );
    }
}
