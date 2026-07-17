// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Configuration loaded from `JASPER_FANIN_*` environment variables.
//!
//! Source of truth for defaults: `docs/HANDOFF-fan-in-daemon.md`
//! "Configuration" section for the original knobs, plus
//! `docs/HANDOFF-usb-low-latency.md` for the camilla_coupling/ring/
//! cushion-decay/host-compliance/auto-trim/usb-direct/host-clock knobs
//! (each field's own comment below points at the doc that actually
//! documents it). If you change a default here, update the matching
//! HANDOFF too — the doc is what operators read.
//!
//! All knobs have sensible defaults so a fresh deploy works without
//! any wizard interaction. Operator overrides go in
//! `/etc/jasper/jasper.env` (system-wide) or
//! `/var/lib/jasper/fanin.env` (wizard-owned, if a wizard is ever
//! added).

use anyhow::{Context, Result};

use crate::loudness::AssistantLoudnessConfig;

/// The SHM ring's pinned slot size in frames (Ring A). Matches the outputd
/// DAC-period contract and the ring header geometry; fan-in publishes
/// `period_frames / RING_SLOT_FRAMES` slots per mixer step. Kept in lockstep by
/// value (not import) with the ring's 128-frame slots — the `period_frames %
/// RING_SLOT_FRAMES == 0` config guard is the drift catch.
pub const RING_SLOT_FRAMES: u32 = 128;

/// The ring's `n_slots` bounds (Ring A). Mirrors `jasper_ring::MIN_N_SLOTS` /
/// `MAX_N_SLOTS`; the ring header validates the same range at attach. A present
/// out-of-range value FAILS LOUD here (`Config::from_env` bails) — Python's
/// `fanin_coupling.resolve_ring_slots` raises on the same range, so the two
/// normalizers agree on the drift axis (unset => default 2; out-of-range =>
/// error on BOTH sides, never a silent clamp).
pub const RING_SLOTS_MIN: u32 = 2;
pub const RING_SLOTS_MAX: u32 = 16;

/// The frames the post-lock cushion decay floor keeps ABOVE the base resampler
/// target — a small working cushion the outer DLL always has to steer within.
/// The decay never descends below `input_resampler_target_frames + this` (the
/// hard MINIMUM the arm-time guard enforces). 32 frames ≈ 0.67 ms at 48 kHz:
/// enough for the DLL's ±adjust authority to hold the fill without underrunning,
/// but the tightest safe reclaim of the standing cushion.
pub const CUSHION_DECAY_FLOOR_MARGIN_FRAMES: u32 = 32;

/// The SHIPPED decay-floor default (the P3/P4 default-flip value). This is the
/// hardware-VALIDATED floor from the jts.local combo-armed product-path gate
/// (2026-07: Apple USB-C dongle, target 512 / period 256 / ±500 ppm — see
/// `docs/HANDOFF-usb-low-latency.md`), NOT a bare `target + margin`. The tighter
/// derived minimum `max(target, minimum_safe_fill) + 32` (= 544 at the default
/// geometry) stays the HARD floor the armed guard rejects below; this constant is
/// only what an out-of-box combo box descends TO when it sets no explicit
/// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES`. Clamped into
/// `[derived_min, ceiling]` at parse time so a small-target geometry (ceiling <
/// 576) still constructs. Shipping the validated 576 (not 544) means a
/// default-flip box that arms the combo lands on the exact floor the gate proved
/// stable — the campaign default-coherence fix (P3). MUST agree with
/// `.env.example`'s documented default and the HANDOFF's floor value.
pub const DEFAULT_CUSHION_DECAY_FLOOR_FRAMES: u32 = 576;

/// The jitter headroom the STATIC held target (`target + warm-up cushion`) must
/// keep above the post-render underfill-unlock threshold. Same 32-frame DLL
/// working margin the decay floor uses (they guard the same physical floor from
/// two directions — decay from above at steady state, this from the static knobs
/// at config time), so an operator has ONE number for "the safe headroom above
/// the physical floor." See `Config::from_env`'s static-cushion validation for
/// why: after rendering one period the cursor-relative fill drops by ~`period`
/// frames, so the held target must sit at least `period + this` above
/// `minimum_safe_fill_frames` or ordinary USB delivery coalescing (arrivals
/// clustering below the deficit in one render interval) underfill-unlocks the
/// lane every burst — churn-by-construction (PR #1141's decay-floor guard, but
/// entered here through the static cushion knobs, which had no equivalent
/// `min_safe`-relative check).
pub const STATIC_CUSHION_JITTER_MARGIN_FRAMES: u32 = 32;

#[derive(Debug, Clone)]
pub struct Config {
    /// ALSA PCM name (or `hw:Card,Dev,Sub`) for the summed output.
    /// The daemon writes mixed audio here. CamillaDSP and the AEC
    /// bridge dsnoop on the corresponding capture side of this
    /// substream pair.
    pub output_pcm: String,

    /// OPTIONAL second output PCM for the **music-only** (pre-TTS) stream
    /// — the multi-room sync tap (see `docs/HANDOFF-multiroom.md` §2
    /// "inv-2 realization"). When set, the mixer writes the post-duck,
    /// **pre-TTS** program here every period, *in addition to* the
    /// primary `output_pcm` (which still carries music+TTS to the local
    /// DAC). `None` — the default, when the env is unset / empty /
    /// `disabled` — means a solo speaker: zero extra work, byte-for-byte
    /// today's behaviour. The write is a LOSSY side-tap (non-blocking,
    /// drop-on-full) so it can NEVER back-pressure the primary output,
    /// which stays the sole timing owner (inv-1). Keeping the assistant
    /// OFF this stream is the inv-3 leak fix: followers hear the room's
    /// music, never the leader's TTS. Env: `JASPER_FANIN_MUSIC_OUTPUT_PCM`.
    pub music_output_pcm: Option<String>,

    /// Per-input PCMs — the capture side of each renderer or internal
    /// test lane's dedicated snd-aloop substream. Order matters: the STATUS
    /// endpoint reports inputs in this order, and `input_renderers`
    /// labels align positionally.
    ///
    /// The list is **pipe-delimited** in the env var
    /// (`JASPER_FANIN_INPUT_PCMS`). Pipe rather than comma because
    /// ALSA hw PCM names contain commas (`hw:Loopback,1,0`); the
    /// previous comma-delimited shape silently split one PCM name
    /// into three entries.
    pub input_pcms: Vec<String>,

    /// Human-readable labels for each input PCM, in the same order.
    /// Surfaced via the STATUS endpoint and the structured event=
    /// log lines. Doesn't affect audio behavior. Pipe-delimited in
    /// the env var to match `input_pcms`.
    pub input_renderers: Vec<String>,

    /// PCM sample rate. All inputs and the output use this rate
    /// (the per-renderer plug wrappers in /etc/asound.conf handle
    /// each renderer's native-rate → 48 kHz conversion before the
    /// substream).
    pub sample_rate: u32,

    /// ALSA period size in frames. Sets the cadence of mixer-loop
    /// wakeups. Default 256 frames ≈ 5.3 ms at 48 kHz — tight enough
    /// to keep the watchdog sentinel fresh on every wake.
    pub period_frames: u32,

    /// ALSA input buffer size in frames. Sets the burst-absorption
    /// margin for each renderer lane. Default 4096 ≈ 85 ms — enough to
    /// absorb observed WiFi A-MPDU AirPlay burst gaps without input
    /// xruns.
    pub input_buffer_frames: u32,

    /// ALSA output buffer size in frames. Keep this latency-bounded
    /// but large enough that CamillaDSP can consistently read a full
    /// 1024-frame chunk from the dsnoop capture side.
    pub output_buffer_frames: u32,

    /// Path to the UDS socket exposing the STATUS command. The
    /// `/state` aggregator in jasper-control queries it; jasper-doctor
    /// queries it. Located under /run so it's tmpfs and recreated on
    /// each daemon start.
    pub control_socket_path: String,

    /// Path to the append-only xrun event log. Persisted across
    /// reboots for forensics. Ring-truncated at ~10 KB.
    pub xrun_log_path: String,

    /// Outputd-compatible TTS socket. Production points Python's TTS
    /// transport here so speech/cues enter before CamillaDSP
    /// crossover/protection. Setting the env var to "disabled" is for
    /// rollback/lab use only.
    pub tts_socket_path: Option<String>,

    /// Bounded pre-DSP TTS queue budget. Audio chunks over this limit
    /// are dropped rather than allowing an unbounded queue to add
    /// seconds of stale assistant speech.
    pub tts_max_pending_frames: u64,

    /// Program-lane attenuation while queued TTS/cue audio is being
    /// mixed by fan-in. This ducks renderer lanes only; TTS remains
    /// unattenuated before CamillaDSP crossover/protection.
    pub tts_program_duck_db: f32,

    /// Assistant loudness policy for the pre-DSP TTS socket.
    pub assistant_loudness: AssistantLoudnessConfig,

    /// Versioned last-achieved assistant loudness. Separate from the
    /// canonical speaker-volume record because fan-in is the sole writer.
    pub assistant_reference_path: String,

    /// fan-in → CamillaDSP coupling transport. `Loopback` (the default) writes
    /// the ALSA snd-aloop substream `output_pcm` exactly as today; CamillaDSP
    /// dsnoop-captures it — byte-identical to the pre-coupling daemon. `ShmRing`
    /// (Ring A, PROTOTYPE) publishes an SPSC ping-pong SHM ring (`ring_path`,
    /// `ring_slots`) that CamillaDSP reads via a capture-direction ioplug. The
    /// Python config generator (`jasper.fanin_coupling`) is the cross-language
    /// source of truth; this normalization MUST agree with `resolve_coupling`
    /// there. Env: `JASPER_FANIN_CAMILLA_COUPLING` (`loopback` | `shm_ring`).
    pub camilla_coupling: Coupling,

    /// The SPSC SHM ring file written under `Coupling::ShmRing` (Ring A). Unused
    /// for `Loopback`. Default `/dev/shm/jts-ring/program.ring`
    /// (the owned tmpfs root, so a magic-invalid file is reclaimable). Env:
    /// `JASPER_FANIN_RING_PATH`. Python `fanin_coupling.resolve_ring_path` uses
    /// the same default.
    pub ring_path: String,

    /// The ring's slot count under `Coupling::ShmRing`. Buffer depth is
    /// `ring_slots * 128` frames — the ONLY latency axis with slot_frames pinned
    /// at 128 (the outputd DAC-period contract). Default 2 (256 frames ≈
    /// 5.3 ms, matching the hardware-validated chunk-128 ring graph);
    /// a present value outside 2..=16 (the ring header's MIN/MAX) FAILS LOUD in
    /// `Config::from_env` — NOT clamped. Env: `JASPER_FANIN_RING_SLOTS`. Python
    /// `fanin_coupling.resolve_ring_slots` uses the same default and likewise
    /// raises on the same range, so both normalizers agree on the drift axis. The
    /// n_slots <-> JASPER_FANIN_RING_SLOTS pairing is the drift axis with the
    /// ioplug conf.d geometry; the ring header's own validation is the runtime
    /// fail-loud backstop.
    pub ring_slots: u32,

    /// DEFAULT-OFF: arm the per-input adaptive resampler on the clock-crossing
    /// (USB) lane (`src/lane_resampler.rs`). When `false` (the default — env
    /// unset / empty / anything but `enabled`), the per-lane read path is
    /// byte-for-byte today's strict one-period read + catch-up drain. When
    /// `true`, the lane named `resampler_lane_label` is DLL-steered to the DAC
    /// clock (drop-free reconciliation, replacing the catch-up sawtooth on that
    /// lane). HIGH-RISK / real-time path: keep OFF until validated on-device.
    /// Env: `JASPER_FANIN_INPUT_RESAMPLER` (`enabled` to arm).
    pub input_resampler_enabled: bool,

    /// The lane LABEL (matched against `input_renderers`) the input resampler
    /// arms on when enabled. Only ONE lane crosses a foreign clock today (USB),
    /// so this is a single label, not a set. A label with no matching input is
    /// a no-op (logged once). Env: `JASPER_FANIN_INPUT_RESAMPLER_LANE`
    /// (default `usbsink`).
    pub input_resampler_lane_label: String,

    /// Target buffered frames the input resampler holds the armed lane's ring
    /// at — the small fixed fill that replaces the catch-up sawtooth. Smaller =
    /// lower latency but less jitter headroom before an underfill→silence.
    /// Default 512 frames (~10.7 ms at 48 kHz, two periods at 256). Env:
    /// `JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES`.
    pub input_resampler_target_frames: u32,

    /// Output ppm clamp on the input resampler's pitch warp — the hard safety
    /// bound on how far the host↔DAC rate gap may ever be corrected. Matches
    /// content_bridge's default. Env:
    /// `JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM`.
    pub input_resampler_max_adjust_ppm: u32,

    /// Warm-up cushion: extra frames the input resampler adds to the DLL hold
    /// target for the armed lane. The earlier c57 path seated the cursor above
    /// `input_resampler_target_frames` and then drained back to the base target;
    /// hardware showed that intentional startup over-consumption can lock/unlock
    /// on the real bursty USB feed. The cushion is now held, so the actual
    /// steady setpoint is `input_resampler_target_frames + cushion`. Default
    /// 2048 frames = ~42.7 ms of conservative extra headroom; this remains
    /// DEFAULT-OFF until the USB soak/cold-start/audibility hardware gate passes.
    /// Env: `JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES`.
    pub input_resampler_warmup_cushion_frames: u32,

    /// Input-ring capacity (frames) for the input resampler's burst buffer — the
    /// headroom ABOVE the target setpoint that absorbs input bursts before they
    /// overflow. Distinct from `input_resampler_target_frames` (the latency
    /// setpoint): raising THIS does not add latency, it only adds burst
    /// absorption. `0` (the default) means "derive 2x the lane's ALSA input
    /// buffer" (`input_buffer_frames * 2`), floored to the resampler's
    /// structural minimum; a non-zero value pins an explicit capacity. The 2x
    /// default gives the real USB burst feed headroom without changing the
    /// steady latency setpoint.
    /// Env: `JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES`.
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
    /// least `input_resampler_target_frames + 32` (a 32-frame margin the DLL
    /// always keeps above the base target) and at most the acquisition ceiling
    /// (`target + warmup cushion`); config validates both fail-loud. Default:
    /// `input_resampler_target_frames + 32` (the tightest safe floor, ~0.7 ms of
    /// cushion above the base target). Env:
    /// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES`.
    pub input_resampler_cushion_decay_floor_frames: u32,
    /// Frames dropped from the held target per decay step. Fail-loud range
    /// `1..=64`; default 18 (~0.375 ms / ~375 ppm demand — 25 ppm inside the
    /// ±400 ppm cascade guard, so a step never perturbs the DLL cascade; a touch
    /// faster than the old 16 to shorten the descent, well short of the
    /// hardware-refuted 64 that rails the ±500 ppm inner authority). Env:
    /// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_STEP_FRAMES`.
    pub input_resampler_cushion_decay_step_frames: u32,
    /// Wall interval between decay steps, in ms (converted to render periods by
    /// the lane). Fail-loud range `250..=10000`; default 1000. Env:
    /// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_INTERVAL_MS`.
    pub input_resampler_cushion_decay_interval_ms: u32,

    /// Path to the host-compliance persistence record (the prime-at-floor
    /// authority). A sibling of the xrun log under the fan-in state dir, which the
    /// daemon already owns and writes (root, `ReadWritePaths=/var/lib/jasper`) —
    /// no new privilege grant. Only CONSULTED when the cushion decay is armed
    /// (persistence rides that flag; there is no separate top-level gate). Env:
    /// `JASPER_FANIN_HOST_COMPLIANCE_PATH`. Default
    /// `/var/lib/jasper/fanin/host_compliance.json`.
    pub host_compliance_path: String,

    /// DEFAULT-OFF one-shot AUTO-TRIM (PoC standing-fill trim). When `true`, the
    /// mixer schedules ONE `TRIM` per armed resampler lane a couple seconds
    /// after that lane goes active, dropping the accumulated standing head-start
    /// (the cursor-relative fill excess above the held target). Fail-safe: only
    /// the exact literal `enabled` (case-insensitive) arms it. Env:
    /// `JASPER_FANIN_AUTO_TRIM`. Manual `TRIM` over the control socket works
    /// regardless of this flag.
    pub auto_trim_enabled: bool,

    /// DEFAULT-OFF USB DIRECT capture (PoC). When `true`, the lane labelled
    /// `input_resampler_lane_label` (the usbsink lane) does NOT read its
    /// snd-aloop substream; instead the mixer opens `usb_direct_device`
    /// (`hw:UAC2Gadget`) as an S32_LE capture, narrows to S16, and feeds the
    /// SAME `LaneResampler` — deleting the usbsink bridge hop + the aloop cable
    /// (~25 ms measured) from the USB path. Direct mode IMPLIES a resampler on
    /// that lane regardless of `input_resampler_enabled` (see
    /// [`Config::lane_wants_resampler`]). Fail-safe: only the exact literal
    /// `enabled` (case-insensitive) arms it; unset / empty / anything else stays
    /// OFF (byte-identical to today's aloop-reading lane). HIGH-RISK real-time
    /// path — keep OFF until validated on-device. Env:
    /// `JASPER_FANIN_USB_DIRECT` (`enabled` to arm).
    pub usb_direct_enabled: bool,

    /// The ALSA capture device the USB DIRECT lane opens when `usb_direct_enabled`.
    /// Default `hw:UAC2Gadget` (the UAC2 gadget card fan-in owns while the USB
    /// source is armed). Unused when direct is off. Env:
    /// `JASPER_FANIN_USB_DIRECT_DEVICE`.
    pub usb_direct_device: String,

    /// The gadget capture OPEN period (frames) the USB DIRECT lane negotiates
    /// (lever 2 — the H1 "hw-pointer/period granularity" test knob). Default 256
    /// (the hardware-validated direct-capture envelope); fail-loud range
    /// 32..=1024. Shrinking it (e.g. 64) is the H1 experiment: if the gadget's
    /// readable `avail` advances in period-sized steps, a smaller open period
    /// exposes ready frames sooner. The capture BUFFER stays DEEP regardless
    /// (`mixer::resolve_direct_buffer_frames`: ≥ 3 periods AND ≥ 768 frames) so a
    /// small period rides a deep buffer — NOT the refuted shallow 2-period URB-
    /// headroom failure. Unused when direct is off. Env:
    /// `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES`.
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
    /// `LaneResampler`. True when EITHER the DEFAULT-OFF input resampler is
    /// enabled OR USB direct capture is enabled — both steer the same lane
    /// (`input_resampler_lane_label`) to the DAC clock, and direct capture has
    /// no aloop catch-up fallback to reconcile the host↔DAC rate gap, so it
    /// MUST own a resampler (C6). A label that doesn't match the resampler lane
    /// never gets one.
    pub fn lane_wants_resampler(&self, label: &str) -> bool {
        (self.input_resampler_enabled || self.usb_direct_enabled)
            && label == self.input_resampler_lane_label
    }

    /// Whether the `fanin-host-clock` servo thread is CONFIGURED to run — the
    /// combo-mode host-slaved USB clock. True only when the host-clock DLL is
    /// armed AND USB direct capture is on, because fan-in must own the gadget
    /// capture to own the pitch ctl (`enabled` + direct-off is a fully-inert
    /// warn because no process owns direct gadget clock control). This is the
    /// SINGLE source of truth for that coupling: `main` derives the servo-spawn
    /// gate (`host_clock_enabled_effective`) from it, and the mixer gates the
    /// host-compliance PRIME-AT-FLOOR on it — the prime skips the cushion
    /// descent on the strength of a prior session's compliance proof that ONLY
    /// this servo (its l0/probe/two-strike revalidation) can re-verify, so
    /// priming without the servo would hold the floor forever on stale evidence.
    /// The runtime servo ALSO needs a live direct-lane resampler (signals
    /// present); this is the config-level predicate both call sites share.
    pub fn host_clock_servo_armed(&self) -> bool {
        self.host_clock_enabled && self.usb_direct_enabled
    }
}

/// fan-in → CamillaDSP coupling transport. Mirrors `jasper.fanin_coupling`'s
/// `loopback` / `shm_ring` selector. Fail-SAFE: an
/// unset/unrecognized env value resolves to `Loopback` (the
/// byte-identical-to-today path).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Coupling {
    /// ALSA snd-aloop substream output; CamillaDSP dsnoop-captures it. Default.
    Loopback,
    /// SPSC ping-pong SHM ring output (Ring A, PROTOTYPE); CamillaDSP reads it
    /// via a capture-direction ioplug.
    ShmRing,
}

impl Coupling {
    /// Normalize a raw `JASPER_FANIN_CAMILLA_COUPLING` value. Fail-safe to
    /// `Loopback` on unset/empty/unknown — matches Python's `resolve_coupling`
    /// so the daemon and the emitted config can never disagree on the transport.
    fn from_env_value(raw: Option<&str>) -> Self {
        match raw.map(|s| s.trim().to_ascii_lowercase()).as_deref() {
            Some("shm_ring") => Coupling::ShmRing,
            _ => Coupling::Loopback,
        }
    }
}

impl Config {
    /// Read JASPER_FANIN_* env vars, falling back to documented defaults.
    /// Returns `Err` only on structural misconfiguration (e.g., input
    /// PCM list length != renderer label list length).
    pub fn from_env() -> Result<Self> {
        let output_pcm = env_str("JASPER_FANIN_OUTPUT_PCM", "hw:Loopback,0,7");
        // OFF unless explicitly configured (no default device): the
        // music-only multi-room tap only exists on a grouping leader.
        let music_output_pcm = env_optional("JASPER_FANIN_MUSIC_OUTPUT_PCM");
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
        let output_buffer_frames = env_u32("JASPER_FANIN_OUTPUT_BUFFER_FRAMES", 1024)?;

        // Sanity: buffer sizes must be >= 2 × period_frames per the
        // standard ALSA convention (the period is what wakes the
        // reader/writer; the buffer absorbs jitter between wakeups).
        // Floor of 2× catches the most common misconfig where someone
        // sets buffer_frames=period_frames.
        let min_buffer_frames = period_frames.saturating_mul(2);
        if input_buffer_frames < min_buffer_frames {
            anyhow::bail!(
                "JASPER_FANIN_INPUT_BUFFER_FRAMES={} must be >= 2 × JASPER_FANIN_PERIOD_FRAMES={} \
                 (minimum ALSA jitter-absorption convention)",
                input_buffer_frames,
                period_frames,
            );
        }
        if output_buffer_frames < min_buffer_frames {
            anyhow::bail!(
                "JASPER_FANIN_OUTPUT_BUFFER_FRAMES={} must be >= 2 × JASPER_FANIN_PERIOD_FRAMES={} \
                 (minimum ALSA jitter-absorption convention)",
                output_buffer_frames,
                period_frames,
            );
        }

        let loudness_defaults = AssistantLoudnessConfig::default();

        // fan-in → CamillaDSP coupling. Default Loopback (byte-identical to
        // today). Fail-safe normalization mirrors Python's resolve_coupling.
        let camilla_coupling = Coupling::from_env_value(
            std::env::var("JASPER_FANIN_CAMILLA_COUPLING")
                .ok()
                .as_deref(),
        );

        // Ring A (shm_ring) knobs. Parsed unconditionally (sane defaults) but
        // only USED under Coupling::ShmRing. Path default matches Python's
        // resolve_ring_path; slots default 2, checked against the ring header's
        // 2..=16 range (RING_SLOTS_MIN/MAX) — a fail-loud out-of-range value is
        // a config error, not a silent clamp, so a typo can't ship a geometry
        // the ring header would reject at attach.
        let ring_path = env_str("JASPER_FANIN_RING_PATH", "/dev/shm/jts-ring/program.ring");
        let ring_slots = env_u32("JASPER_FANIN_RING_SLOTS", 2)?;
        if !(RING_SLOTS_MIN..=RING_SLOTS_MAX).contains(&ring_slots) {
            anyhow::bail!(
                "JASPER_FANIN_RING_SLOTS={} out of range {}..={} — the SHM ring \
                 header validates this at attach; a shear-prone geometry must \
                 fail loud at config, not at runtime",
                ring_slots,
                RING_SLOTS_MIN,
                RING_SLOTS_MAX,
            );
        }
        // The ring's slot is pinned at RING_SLOT_FRAMES (128, the outputd
        // DAC-period contract): fan-in publishes period_frames/128 slots per
        // step, so period_frames MUST be a whole multiple of 128 or a step would
        // shear a slot. Fail LOUD at config (only when shm_ring is actually
        // selected — an odd period under loopback/pipe is fine).
        if camilla_coupling == Coupling::ShmRing && period_frames % RING_SLOT_FRAMES != 0 {
            anyhow::bail!(
                "JASPER_FANIN_PERIOD_FRAMES={} must be a whole multiple of the \
                 pinned SHM ring slot size ({} frames) under \
                 JASPER_FANIN_CAMILLA_COUPLING=shm_ring — a fractional slot count \
                 would shear the ring",
                period_frames,
                RING_SLOT_FRAMES,
            );
        }

        // DEFAULT-OFF per-input adaptive resampler (clock-crossing/USB lane).
        // Fail-safe: only the exact literal `enabled` (case-insensitive) arms
        // it; unset / empty / anything else stays OFF (byte-identical to today).
        let input_resampler_enabled = matches!(
            std::env::var("JASPER_FANIN_INPUT_RESAMPLER")
                .ok()
                .map(|s| s.trim().to_ascii_lowercase())
                .as_deref(),
            Some("enabled")
        );
        let input_resampler_lane_label = env_str("JASPER_FANIN_INPUT_RESAMPLER_LANE", "usbsink");
        let input_resampler_target_frames =
            env_u32("JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES", 512)?;
        let input_resampler_max_adjust_ppm =
            env_u32("JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM", 500)?;
        // Eight render periods of held warm-up cushion by default (2048 frames
        // ≈ 42.7 ms). Hardware USB testing showed the earlier four-period
        // cushion lock/unlock-thrashed on the real snd-aloop burst feed; the
        // deeper held cushion stayed locked while keeping the latency knob
        // explicit and DEFAULT-OFF.
        let input_resampler_warmup_cushion_frames =
            env_u32("JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES", 2048)?;
        // 0 = derive a 2x burst ring from the lane's ALSA input buffer; a
        // non-zero value pins an explicit capacity.
        let input_resampler_ring_frames = env_u32("JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES", 0)?;

        // DEFAULT-OFF post-lock cushion DECAY (latency lever 1). Fail-safe: only
        // the exact literal `enabled` (case-insensitive) arms it; unset / empty /
        // anything else stays OFF (held target pinned at the acquisition ceiling,
        // byte-identical to today). Meaningful only with the host-clock DLL armed.
        let input_resampler_cushion_decay_enabled = matches!(
            std::env::var("JASPER_FANIN_RESAMPLER_CUSHION_DECAY")
                .ok()
                .map(|s| s.trim().to_ascii_lowercase())
                .as_deref(),
            Some("enabled")
        );
        // The tightest safe floor is the LARGER of two constraints, both a DLL
        // working margin above their anchor:
        //   1. `target + DLL margin` — keep a working cushion above the base
        //      target the DLL always has to steer within.
        //   2. `minimum_safe_fill_frames + DLL margin` — the PHYSICAL floor. The
        //      resampler underfill-unlocks the moment the cursor-relative fill
        //      drops below `minimum_safe_fill_frames` (= ceil(period × max_ratio)
        //      + kernel radius + 1). A held target at/below that value sits on the
        //      unlock threshold — churn-by-construction (audible gap → snap-back →
        //      relock → warm-up → re-descend, on repeat). Constraint 1 alone does
        //      NOT imply constraint 2: for a small base target (below ~period)
        //      `target + margin` can land below the physical floor.
        // Default the floor to that bound so the out-of-box decay reclaims the
        // maximum SAFE cushion; an operator can raise it. `min_safe` is derived
        // from the same shared `jasper_resampler` helper the lane's underfill gate
        // uses, so the two can never disagree about the physical threshold.
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
        // Ship the hardware-VALIDATED floor (DEFAULT_CUSHION_DECAY_FLOOR_FRAMES =
        // 576, the jts.local combo-armed gate value) as the out-of-box default, not
        // the tighter unvalidated derived minimum (544 at the default geometry).
        // Clamp into [floor_min, ceiling]: never below the physical/DLL-margin hard
        // floor, never above the acquisition ceiling (a small-target geometry whose
        // ceiling < 576 constructs at its ceiling). This is the P3 default-coherence
        // fix — a default-flip box arming the combo with no explicit floor lands on
        // the validated 576 and constructs cleanly.
        let cushion_decay_floor_default = DEFAULT_CUSHION_DECAY_FLOOR_FRAMES
            .max(cushion_decay_floor_min)
            .min(cushion_decay_ceiling);
        let input_resampler_cushion_decay_floor_frames = env_u32(
            "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES",
            cushion_decay_floor_default,
        )?;
        // Validate fail-loud (the `validate_audio_config` idiom) — but only when
        // the feature is armed, so a stale floor on a decay-off box never blocks
        // boot.
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
        // Default 18 (not 16): the descent is paced by step / interval, so a
        // slightly larger step shortens the ~2.5-min floor descent proportionally
        // (18 vs 16 ≈ 12 % faster). 18 frames per 1000 ms ≈ 375 ppm of demanded
        // rate — 25 ppm INSIDE the ±400 ppm cascade-stability guard, so the decay
        // still pauses before it can perturb the DLL cascade. The hardware pass
        // (jts.local 2026-07-03) REFUTED 64 (rails the ±500 ppm inner authority);
        // 18 is the measured sweet spot that speeds the descent without touching
        // the inner loop. Range floor stays 1 (a gentle single-frame step);
        // ceiling stays 64 so a lab operator can still push it (at their own risk).
        let input_resampler_cushion_decay_step_frames =
            env_u32("JASPER_FANIN_RESAMPLER_CUSHION_DECAY_STEP_FRAMES", 18)?;
        if !(1..=64).contains(&input_resampler_cushion_decay_step_frames) {
            anyhow::bail!(
                "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_STEP_FRAMES={} out of range 1..=64 \
                 (a gentle per-step frame drop; 18 ≈ 0.375 ms / ~375 ppm demand stays \
                 25 ppm inside the ±400 ppm cascade guard so a step never perturbs the DLL)",
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
        // The host-compliance persistence record path (prime-at-floor authority).
        // No range/validation: a bad path simply fails the atomic write/read
        // best-effort (logged, never fatal), degrading to today's always-descend
        // behaviour.
        let host_compliance_path = env_str(
            "JASPER_FANIN_HOST_COMPLIANCE_PATH",
            crate::host_compliance::DEFAULT_COMPLIANCE_PATH,
        );

        // DEFAULT-OFF one-shot AUTO-TRIM (PoC standing-fill trim). Fail-safe:
        // only the exact literal `enabled` (case-insensitive) arms it; unset /
        // empty / anything else stays OFF (byte-identical to today).
        let auto_trim_enabled = matches!(
            std::env::var("JASPER_FANIN_AUTO_TRIM")
                .ok()
                .map(|s| s.trim().to_ascii_lowercase())
                .as_deref(),
            Some("enabled")
        );

        // DEFAULT-OFF USB DIRECT capture (PoC). Fail-safe: only the exact
        // literal `enabled` (case-insensitive) arms it — same idiom as the
        // resampler / auto-trim flags. Direct mode implies a resampler on the
        // usbsink lane (see Config::lane_wants_resampler).
        let usb_direct_enabled = matches!(
            std::env::var("JASPER_FANIN_USB_DIRECT")
                .ok()
                .map(|s| s.trim().to_ascii_lowercase())
                .as_deref(),
            Some("enabled")
        );
        let usb_direct_device = env_str("JASPER_FANIN_USB_DIRECT_DEVICE", "hw:UAC2Gadget");
        // The gadget OPEN period (frames). Default 256 = byte-identical to the
        // bridge-proven envelope. Fail-loud range 32..=1024: below 32 the period
        // IRQ storms the mixer thread, above 1024 the open period would exceed
        // the deep-buffer floor's own headroom and defeat the low-latency intent.
        // Only consulted on the direct lane, but parsed unconditionally (like the
        // other USB DIRECT knobs) so a typo fails loud on any boot, not only when
        // direct is armed.
        let usb_direct_period_frames = env_u32("JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES", 256)?;
        if !(32..=1024).contains(&usb_direct_period_frames) {
            anyhow::bail!(
                "JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES={} out of range 32..=1024 (the gadget \
                 open period; 256 is the bridge-proven default, 64 is the lever-2 H1 test knob)",
                usb_direct_period_frames,
            );
        }

        // STATIC held-target churn guard (the symmetric sibling of the decay-floor
        // validation above). When a resampler is armed on the clock-crossing lane
        // — either via JASPER_FANIN_INPUT_RESAMPLER=enabled OR implied by
        // JASPER_FANIN_USB_DIRECT=enabled (see `lane_wants_resampler`; the direct
        // lane has no aloop catch-up fallback, so it always builds one) — the lane
        // holds the ring at `target + cushion` (the acquisition ceiling) and
        // renders ONE render period (`period_frames`) each step. So the
        // steady-state POST-render cursor-relative fill sits at `held - period`.
        // The lane underfill-unlocks the instant that fill drops below
        // `minimum_safe_fill_frames` (= ceil(period × max_ratio) + radius + 1). The
        // held target must therefore sit at least `period + jitter margin` above
        // minimum_safe_fill, or ordinary USB delivery coalescing (arrivals
        // clustering below the per-render deficit — the max_avail≈2×period gadget
        // signature the drain-stats histogram shows) trips lock→silence→relock
        // every burst: churn-by-construction. This is the SAME failure class the
        // decay-floor guard above rejects, but entered through the STATIC cushion
        // knobs, which had no equivalent min_safe-relative check (the fan-in
        // unlock-churn diagnosis, 2026-07). Fail LOUD whenever a resampler is armed
        // so a churny knob-set can't ship a lane that diagnostic-visibly thrashes;
        // gated on the lane actually arming so a stale cushion on a resampler-OFF
        // box never blocks boot (mirrors the decay-floor guard's arm-gating). The
        // production defaults (512 + 2048 = 2560 held) clear this by ~2030 frames;
        // only a hand-tuned lab geometry (e.g. the 256+256=512 held that produced
        // the observed churn) can trip it.
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

        // DEFAULT-OFF combo-mode host-slaved USB clock. Fail-safe: only the
        // exact literal `enabled` (case-insensitive) arms it. Unlike the sibling
        // flags above (which silently stay off on any other value), this one
        // WARNS on a non-empty non-`enabled` value — mirroring the usbsink
        // literal idiom (`JASPER_USBSINK_HOST_CLOCK`) so a typo like `on`/`1`
        // leaves a breadcrumb rather than silently disabling a safety feature.
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
        // Probe command. Fail-fast on out-of-range: a probe below the ~163 ppm
        // Windows deadband would falsely fail every session.
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

        Ok(Self {
            output_pcm,
            music_output_pcm,
            input_pcms,
            input_renderers,
            sample_rate,
            period_frames,
            input_buffer_frames,
            output_buffer_frames,
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
            camilla_coupling,
            ring_path,
            ring_slots,
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
            host_compliance_path,
            auto_trim_enabled,
            usb_direct_enabled,
            usb_direct_device,
            usb_direct_period_frames,
            host_clock_enabled,
            host_clock_probe_ppm,
        })
    }
}

// ---- env var helpers ------------------------------------------------

fn env_str(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn env_optional_with_default(name: &str, default: &str) -> Option<String> {
    match std::env::var(name) {
        Ok(s) if s.trim().is_empty() || s.trim().eq_ignore_ascii_case("disabled") => None,
        Ok(s) => Some(s),
        Err(_) => Some(default.to_string()),
    }
}

/// Optional string env var with NO default: `None` when unset, empty, or
/// the literal `disabled` (case-insensitive); the trimmed value otherwise.
/// Unlike `env_optional_with_default`, an unset var yields `None` — for a
/// feature that is OFF unless explicitly configured (the music-only tap).
fn env_optional(name: &str) -> Option<String> {
    match std::env::var(name) {
        Ok(s) if s.trim().is_empty() || s.trim().eq_ignore_ascii_case("disabled") => None,
        Ok(s) => Some(s.trim().to_string()),
        Err(_) => None,
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
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => s
            .trim()
            .parse::<u32>()
            .with_context(|| format!("{} must be a non-negative integer; got {:?}", name, s)),
        _ => Ok(default),
    }
}

/// Like `env_u32`, but for a load-bearing GEOMETRY DIMENSION that must be
/// strictly positive — `sample_rate` and `period_frames`. A parsed `0` is a
/// legal `u32` yet a nonsensical dimension: the mixer's per-period math divides
/// by both (`period_frames * 1e9 / sample_rate` at `mixer.rs`'s ShmRing ns/period,
/// `(avail - target) / period_frames` in `catchup_drain_periods`, and the
/// ms→periods conversion), all UNGUARDED. Release builds compile out the
/// `debug_assert!(period_frames > 0)`, so a `0` divides-by-zero and panics; with
/// `panic = "abort"` + the unit's `Restart=on-failure` that is an infinite
/// crash-restart loop that takes ALL audio down (and the audible-cue path with
/// it). Rather than bail — which is its OWN config-parse restart loop — fall back
/// to the documented default with a WARN breadcrumb (the `JASPER_FANIN_HOST_CLOCK`
/// ignore idiom), so the speaker keeps playing on a sane geometry. A non-numeric
/// or negative value still fails loud via `env_u32` (an unambiguous operator typo,
/// the crate's fail-loud-on-garbage contract); only a valid-but-zero dimension is
/// recovered here.
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
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => s
            .trim()
            .parse::<u64>()
            .with_context(|| format!("{} must be a non-negative integer; got {:?}", name, s)),
        _ => Ok(default),
    }
}

fn env_f32(name: &str, default: f32) -> Result<f32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => parse_env_f32(name, &s),
        _ => Ok(default),
    }
}

fn env_f32_fallback(name: &str, fallback_name: &str, default: f32) -> Result<f32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => parse_env_f32(name, &s),
        _ => env_f32(fallback_name, default),
    }
}

fn parse_env_f32(name: &str, raw: &str) -> Result<f32> {
    let parsed = raw
        .trim()
        .parse::<f32>()
        .with_context(|| format!("{} must be a number; got {:?}", name, raw))?;
    if !parsed.is_finite() {
        anyhow::bail!("{} must be finite", name);
    }
    Ok(parsed)
}

fn env_u32_fallback(name: &str, fallback_name: &str, default: u32) -> Result<u32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => s
            .trim()
            .parse::<u32>()
            .with_context(|| format!("{} must be a non-negative integer; got {:?}", name, s)),
        _ => env_u32(fallback_name, default),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::Mutex;

    /// Process-global mutex that serializes env-var-touching tests.
    /// `std::env::set_var` mutates process-global state, so even with
    /// careful save+restore the tests must run sequentially or they
    /// race. `cargo test` runs in parallel by default; this mutex
    /// gives us serialization without forcing `--test-threads=1`
    /// across the whole crate (other module's tests can still run
    /// in parallel).
    ///
    /// The mutex is poisoned-but-recoverable: if a test panics
    /// inside `with_env`, the next acquirer will get a PoisonError;
    /// we `into_inner()` to take the guard anyway (state restoration
    /// happens on drop; the panicked test's restoration didn't run
    /// but the next test's setup clears everything, so we're fine).
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// Test fixture: serialize on `ENV_LOCK`, snapshot ALL
    /// fan-in env vars, clear them, apply this test's
    /// per-var overrides, run the closure, restore.
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
                ("JASPER_FANIN_OUTPUT_PCM", None),
                ("JASPER_FANIN_INPUT_PCMS", None),
                ("JASPER_FANIN_INPUT_RENDERERS", None),
                ("JASPER_FANIN_SAMPLE_RATE", None),
                ("JASPER_FANIN_PERIOD_FRAMES", None),
                ("JASPER_FANIN_BUFFER_FRAMES", None),
                ("JASPER_FANIN_INPUT_BUFFER_FRAMES", None),
                ("JASPER_FANIN_OUTPUT_BUFFER_FRAMES", None),
                ("JASPER_FANIN_TTS_SOCKET", None),
                ("JASPER_FANIN_TTS_MAX_PENDING_FRAMES", None),
                ("JASPER_FANIN_TTS_PROGRAM_DUCK_DB", None),
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
                assert_eq!(cfg.output_pcm, "hw:Loopback,0,7");
                // Music-only multi-room tap is OFF by default (solo speaker).
                assert_eq!(cfg.music_output_pcm, None);
                assert_eq!(cfg.input_pcms.len(), 5);
                assert_eq!(cfg.input_renderers.len(), 5);
                assert_eq!(cfg.input_renderers[0], "spotify");
                assert_eq!(cfg.input_renderers[4], "correction");
                assert_eq!(cfg.sample_rate, 48_000);
                assert_eq!(cfg.period_frames, 256);
                assert_eq!(cfg.input_buffer_frames, 4096);
                assert_eq!(cfg.output_buffer_frames, 1024);
                assert_eq!(
                    cfg.tts_socket_path.as_deref(),
                    Some("/run/jasper-fanin/tts.sock")
                );
                assert_eq!(cfg.tts_max_pending_frames, 96_000);
                assert_eq!(cfg.tts_program_duck_db, -25.0);
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
                // Per-input adaptive resampler is DEFAULT-OFF — the whole point
                // of the feature flag (HIGH-RISK real-time path).
                assert!(
                    !cfg.input_resampler_enabled,
                    "input resampler must default OFF"
                );
                assert_eq!(cfg.input_resampler_lane_label, "usbsink");
                assert_eq!(cfg.input_resampler_target_frames, 512);
                assert_eq!(cfg.input_resampler_max_adjust_ppm, 500);
                // Warm-up cushion defaults to conservative held headroom; the
                // burst ring is derived downstream (0 → 2x the ALSA input
                // buffer) unless pinned.
                assert_eq!(cfg.input_resampler_warmup_cushion_frames, 2048);
                assert_eq!(cfg.input_resampler_ring_frames, 0);
                // Post-lock cushion DECAY is DEFAULT-OFF (latency lever 1); its
                // knobs default to the hardware-validated floor (576, the jts.local
                // combo-armed gate value — clamped into [derived_min, ceiling]), an
                // 18-frame gentle step, and a 1 s interval. (P3 default-coherence:
                // the shipped default is the validated 576, not the tighter derived
                // 544 = target 512 + 32-frame margin.)
                assert!(
                    !cfg.input_resampler_cushion_decay_enabled,
                    "cushion decay must default OFF"
                );
                assert_eq!(
                    cfg.input_resampler_cushion_decay_floor_frames,
                    DEFAULT_CUSHION_DECAY_FLOOR_FRAMES
                );
                assert_eq!(cfg.input_resampler_cushion_decay_step_frames, 18);
                assert_eq!(cfg.input_resampler_cushion_decay_interval_ms, 1000);
                // Cascade-margin invariant (pins the 375-vs-400 ppm claim documented
                // in config.rs, .env.example, and the HANDOFF): the default decay
                // step's demanded rate (step_frames dropped over interval_ms, as a
                // fraction of the frames that pass in that interval) must sit INSIDE
                // the DLL cascade-stability guard, or a settled decay step could
                // perturb the inner loop. 18 frames / 1000 ms @ 48 kHz = 375 ppm,
                // 25 ppm inside the 400 ppm guard. A future default-step bump or
                // guard lowering that inverts this margin fails here.
                let step_demand_ppm = (cfg.input_resampler_cushion_decay_step_frames as f64)
                    / ((cfg.input_resampler_cushion_decay_interval_ms as f64 / 1000.0)
                        * cfg.sample_rate as f64)
                    * 1_000_000.0;
                assert!(
                    (step_demand_ppm - 375.0).abs() < 1.0,
                    "default step demand is ~375 ppm, got {step_demand_ppm}"
                );
                assert!(
                    step_demand_ppm < crate::mixer::CUSHION_DECAY_CASCADE_GUARD_PPM,
                    "default decay step demand {step_demand_ppm} ppm must stay inside the \
                     {} ppm cascade guard",
                    crate::mixer::CUSHION_DECAY_CASCADE_GUARD_PPM
                );
                // Host-compliance persistence path defaults under the fan-in state
                // dir (sibling of the xrun log, already-owned write path).
                assert_eq!(
                    cfg.host_compliance_path,
                    "/var/lib/jasper/fanin/host_compliance.json"
                );
                // One-shot AUTO-TRIM is DEFAULT-OFF (manual TRIM is the PoC
                // path; auto is the opt-in convenience).
                assert!(!cfg.auto_trim_enabled, "auto-trim must default OFF");
                // USB DIRECT capture is DEFAULT-OFF; device defaults to the
                // UAC2 gadget card.
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
        // DA-0040: `sample_rate` and `period_frames` are load-bearing geometry
        // dimensions the mixer divides by (period ns/period, catchup-drain
        // periods, ms→periods) with NO runtime guard in release builds. A
        // valid-but-zero env value must NOT construct a Config that divides by
        // zero and panic-aborts into a crash loop — it falls back to the
        // documented default (warn breadcrumb emitted).
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
        // DA-0040 must not perturb valid inputs: a legal positive value flows
        // through env_u32_positive unchanged. (period_frames must stay a whole
        // multiple of the 128-frame ring slot under the default loopback
        // coupling only; 512 satisfies it regardless.)
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
        // env_u32_positive delegates to env_u32 for parsing, so a garbage
        // (non-numeric) value still FAILS LOUD — only the valid-but-zero case is
        // recovered. This pins the boundary: 0 → default+warn, garbage → error.
        with_env(&[("JASPER_FANIN_PERIOD_FRAMES", Some("garbage"))], || {
            assert!(
                Config::from_env().is_err(),
                "a non-numeric period_frames must still fail loud, not fall back"
            );
        });
    }

    #[test]
    fn auto_trim_only_armed_by_exact_enabled_literal() {
        // Fail-safe: ONLY the literal `enabled` (case-insensitive) arms it.
        for raw in ["enabled", "ENABLED", " Enabled "] {
            with_env(&[("JASPER_FANIN_AUTO_TRIM", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(cfg.auto_trim_enabled, "{raw:?} should arm auto-trim");
            });
        }
        // Anything else (including truthy-looking values) stays OFF.
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
        // Fail-safe: ONLY the literal `enabled` (case-insensitive) arms it.
        for raw in ["enabled", "ENABLED", " Enabled "] {
            with_env(&[("JASPER_FANIN_INPUT_RESAMPLER", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(
                    cfg.input_resampler_enabled,
                    "{raw:?} should arm the resampler"
                );
            });
        }
        // Anything else (including truthy-looking values) stays OFF.
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
        // Fail-safe: ONLY the literal `enabled` (case-insensitive) arms it.
        for raw in ["enabled", "ENABLED", " Enabled "] {
            with_env(&[("JASPER_FANIN_USB_DIRECT", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(cfg.usb_direct_enabled, "{raw:?} should arm USB direct");
            });
        }
        // Anything else (including truthy-looking values) stays OFF.
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
        // Default device is the UAC2 gadget card.
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
        // Unset direct + unset resampler: neither is on, and the usbsink lane
        // does NOT want a resampler (byte-identical to today).
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
        // Direct ON but the plain resampler flag OFF: the usbsink lane STILL
        // wants a resampler (direct capture has no aloop catch-up fallback), and
        // only that lane does.
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
        // The pre-existing path: resampler flag on, direct off — the lane still
        // wants a resampler (unchanged behavior).
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

    // ---- combo-mode host-slaved USB clock (C3) ----------------------------

    #[test]
    fn host_clock_only_armed_by_exact_enabled_literal() {
        // Fail-safe: ONLY the literal `enabled` (case-insensitive) arms it.
        for raw in ["enabled", "ENABLED", " Enabled "] {
            with_env(&[("JASPER_FANIN_HOST_CLOCK", Some(raw))], || {
                let cfg = Config::from_env().expect("parses");
                assert!(cfg.host_clock_enabled, "{raw:?} should arm host-clock");
            });
        }
        // Anything else (including truthy-looking values) stays OFF, warned.
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
        // The servo-armed predicate is the AND of host-clock and USB-direct: the
        // servo can only own the pitch ctl when fan-in owns the gadget capture
        // (`enabled` + direct-off is a fully-inert warn). This is the SINGLE
        // source of truth `main`'s servo-spawn gate AND the mixer's
        // host-compliance prime gate share — a floor prime whose revalidating
        // servo never runs would hold the floor forever on stale evidence (the
        // #1161 ladder-inert regime). A mutation dropping either conjunct fails
        // here.
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
                    // The predicate is exactly the AND of the two flags (no third
                    // hidden input), so it is the honest servo-configured signal.
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
        // Below the 200 floor (the ~163 ppm Windows deadband) is rejected.
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
        // The floor (200) and ceiling (800) are accepted.
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
    fn music_output_pcm_off_by_default_and_parses_when_set() {
        // Unset → None (solo speaker; no extra ALSA output).
        with_env(&[("JASPER_FANIN_MUSIC_OUTPUT_PCM", None)], || {
            assert_eq!(Config::from_env().unwrap().music_output_pcm, None);
        });
        // Explicit "disabled" sentinel → None (rollback parity with the
        // TTS socket knob).
        with_env(
            &[("JASPER_FANIN_MUSIC_OUTPUT_PCM", Some("disabled"))],
            || {
                assert_eq!(Config::from_env().unwrap().music_output_pcm, None);
            },
        );
        // A real PCM name → Some (and trimmed).
        with_env(
            &[("JASPER_FANIN_MUSIC_OUTPUT_PCM", Some("  hw:Loopback,0,6 "))],
            || {
                assert_eq!(
                    Config::from_env().unwrap().music_output_pcm.as_deref(),
                    Some("hw:Loopback,0,6"),
                );
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

    /// Regression test: smoke-test caught this in Phase 2 chunk 2 dev.
    /// hw PCM names contain commas (`hw:Loopback,1,0`); the previous
    /// comma-delimited parser silently split one PCM name into three
    /// entries, then erroneously failed length validation against a
    /// 4-entry renderer list. Pipe delimiter avoids the collision.
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
        // env_list filters out empty/whitespace entries, so a string
        // of only delimiters parses to an empty Vec — caught by the
        // is_empty() guard with a clear error message.
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
    fn output_buffer_must_be_at_least_twice_period() {
        with_env(
            &[
                ("JASPER_FANIN_PERIOD_FRAMES", Some("512")),
                ("JASPER_FANIN_OUTPUT_BUFFER_FRAMES", Some("512")),
            ],
            || {
                let err = Config::from_env().expect_err("output buffer < 2×period must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("JASPER_FANIN_OUTPUT_BUFFER_FRAMES"),
                    "expected output-buffer error, got: {}",
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
        // Fail-safe: unset / empty / non-`enabled` stays OFF; only the literal
        // `enabled` (case-insensitive) arms it.
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
        // P3 default-coherence: the SHIPPED default is the hardware-validated
        // jts.local floor (DEFAULT_CUSHION_DECAY_FLOOR_FRAMES = 576), not the
        // tighter derived `target + margin` (544 at the default geometry). It sits
        // above the derived minimum and below the acquisition ceiling.
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
                // Still inside the hard bounds: >= derived min (target 512 + 32 =
                // 544), <= ceiling (target 512 + cushion 2048 = 2560).
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
        // The P3/P4 default-flip regression: a gadget box's auto pass arms the USB
        // combo (USB_DIRECT + HOST_CLOCK + CUSHION_DECAY, all `enabled`) with NO
        // explicit floor / target / cushion. The whole point of shipping 576 as the
        // decay-floor default (instead of leaving the derived 544 that the campaign
        // brief flagged) is that this exact combo-armed default construction never
        // fails `Config::from_env`. Pin it: the validated default floor must sit in
        // range for the armed guard so a default-flip box boots.
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
                // The armed decay-floor guard passed with the shipped default.
                assert_eq!(
                    cfg.input_resampler_cushion_decay_floor_frames,
                    DEFAULT_CUSHION_DECAY_FLOOR_FRAMES,
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_fails_loud_below_margin_when_armed() {
        // Armed + a floor below target+margin must error. target 512 + 32 = 544;
        // 543 is one under the minimum.
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
        // Armed + a floor above the acquisition ceiling (target+cushion) must
        // error: there is nothing to decay above the ceiling.
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
        // minimum-safe-fill floor — a floor there is churn-by-construction (it
        // sits on the underfill-unlock threshold). The validation must reject it
        // fail-loud even though it is above `target + margin`.
        //
        // target 200, period 256, max_ppm 500 → min_safe = ceil(256*1.0005)+16+1
        // = 274. floor_min = max(200+32, 274+32) = 306. A floor of 240 is above
        // target+margin (232) but below floor_min (306) → must error.
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
        // The DEFAULT floor must NEVER be churn-by-construction: it must sit at or
        // above the physical underfill-unlock threshold `minimum_safe_fill +
        // margin` (the invariant this test guards). Since P3, the shipped default
        // is `max(DEFAULT_CUSHION_DECAY_FLOOR_FRAMES, derived_min).min(ceiling)`, so
        // for a small base target the validated 576 already clears the physical
        // floor with room to spare (target 200 / period 256 / max_ppm 500 →
        // min_safe 274 → derived floor 306; 576 > 306, and 576 < ceiling 2248).
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
                // The load-bearing invariant: the default is never below the
                // physical floor (would churn every period).
                assert!(
                    cfg.input_resampler_cushion_decay_floor_frames
                        >= min_safe + CUSHION_DECAY_FLOOR_MARGIN_FRAMES,
                    "default floor for a small target must respect the physical floor"
                );
                // At this small target the validated 576 default binds (it is above
                // the derived min and below the ceiling).
                assert_eq!(
                    cfg.input_resampler_cushion_decay_floor_frames,
                    DEFAULT_CUSHION_DECAY_FLOOR_FRAMES,
                );
            },
        );
    }

    #[test]
    fn cushion_decay_floor_default_clamps_under_ceiling_for_small_cushion() {
        // A geometry whose acquisition ceiling (target + warmup cushion) sits BELOW
        // the validated 576 default must clamp the default down to the ceiling (a
        // floor above the ceiling has nothing to decay and would fail the guard).
        // target 512 + cushion 40 = 552 ceiling < 576; derived_min = 544; so the
        // default clamps to 552 and the armed guard passes.
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
        // A stale/bad floor on a decay-OFF box must NOT block boot — the range
        // check is gated on the feature being armed.
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

    // ---- static held-target churn guard (the unlock-churn diagnosis fix) ----

    #[test]
    fn static_cushion_fails_loud_on_churny_lab_geometry_when_resampler_armed() {
        // The exact deployed lab geometry that produced the observed unlock
        // churn: target 256 + cushion 256 = 512 held, period 256, max_ppm 500.
        // min_safe = 274, required = 274 + 256 + 32 = 562 > 512 → must fail loud
        // so a churn-by-construction knob-set cannot ship silently.
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
        // lane_wants_resampler). The guard must fire for the direct-armed path
        // too — gating on input_resampler_enabled alone would miss exactly the
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
        // The production default held target (512 + 2048 = 2560) clears the guard
        // by a wide margin — the fix must not perturb the shipping geometry.
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
        // The guard is `held >= min_safe + period + margin`. At period 256 /
        // max_ppm 500, min_safe = 274, so required held = 274 + 256 + 32 = 562.
        // target 306 + cushion 256 = 562 (exactly required) must PASS; one under
        // (cushion 255 → 561) must FAIL. Pins the strict boundary.
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
        // A churny cushion on a resampler-OFF box (neither flag armed) must NOT
        // block boot — the guard is gated on the lane actually arming, mirroring
        // the decay-floor guard. No resampler is built, so no churn is possible.
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
                ("JASPER_FANIN_OUTPUT_BUFFER_FRAMES", None),
            ],
            || {
                let cfg = Config::from_env().expect("legacy env must parse");
                assert_eq!(cfg.input_buffer_frames, 2048);
                assert_eq!(cfg.output_buffer_frames, 1024);
            },
        );
    }

    #[test]
    fn coupling_defaults_to_loopback_when_unset() {
        with_env(&[("JASPER_FANIN_CAMILLA_COUPLING", None)], || {
            let cfg = Config::from_env().expect("defaults must parse");
            assert_eq!(cfg.camilla_coupling, Coupling::Loopback);
        });
    }

    #[test]
    fn coupling_unknown_value_fails_safe_to_loopback() {
        // A typo must NEVER silently flip the shared realtime capture. Mirrors
        // Python's resolve_coupling fail-safe.
        with_env(&[("JASPER_FANIN_CAMILLA_COUPLING", Some("pipe"))], || {
            let cfg = Config::from_env().expect("unknown coupling must parse");
            assert_eq!(cfg.camilla_coupling, Coupling::Loopback);
        });
    }

    #[test]
    fn coupling_loopback_value_is_loopback() {
        with_env(
            &[("JASPER_FANIN_CAMILLA_COUPLING", Some("loopback"))],
            || {
                let cfg = Config::from_env().expect("loopback coupling must parse");
                assert_eq!(cfg.camilla_coupling, Coupling::Loopback);
            },
        );
    }

    #[test]
    fn coupling_from_env_value_normalization() {
        // Direct unit test of the normalization, independent of the env plumbing.
        assert_eq!(Coupling::from_env_value(None), Coupling::Loopback);
        assert_eq!(Coupling::from_env_value(Some("")), Coupling::Loopback);
        assert_eq!(Coupling::from_env_value(Some("  ")), Coupling::Loopback);
        assert_eq!(Coupling::from_env_value(Some("pipe")), Coupling::Loopback);
        assert_eq!(
            Coupling::from_env_value(Some("loopback")),
            Coupling::Loopback
        );
        assert_eq!(
            Coupling::from_env_value(Some("garbage")),
            Coupling::Loopback
        );
        // Ring A (shm_ring) token — must agree with Python's resolve_coupling.
        assert_eq!(
            Coupling::from_env_value(Some("shm_ring")),
            Coupling::ShmRing
        );
        assert_eq!(
            Coupling::from_env_value(Some(" SHM_RING ")),
            Coupling::ShmRing
        );
        // A near-miss typo fails safe to loopback, never the shared ring.
        assert_eq!(Coupling::from_env_value(Some("ring")), Coupling::Loopback);
        assert_eq!(
            Coupling::from_env_value(Some("shm-ring")),
            Coupling::Loopback
        );
    }

    #[test]
    fn shm_ring_coupling_parses_with_ring_defaults() {
        with_env(
            &[
                ("JASPER_FANIN_CAMILLA_COUPLING", Some("shm_ring")),
                ("JASPER_FANIN_RING_PATH", None),
                ("JASPER_FANIN_RING_SLOTS", None),
            ],
            || {
                let cfg = Config::from_env().expect("shm_ring defaults must parse");
                assert_eq!(cfg.camilla_coupling, Coupling::ShmRing);
                assert_eq!(cfg.ring_path, "/dev/shm/jts-ring/program.ring");
                assert_eq!(cfg.ring_slots, 2);
                // Default period (256) is a multiple of the 128-frame slot.
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
        for bad in ["1", "17", "0", "100"] {
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
                },
            );
        }
    }

    #[test]
    fn shm_ring_period_must_be_multiple_of_slot_frames() {
        // 200 is not a multiple of 128 -> shear -> fail loud, but ONLY under
        // shm_ring (loopback/pipe tolerate any period).
        with_env(
            &[
                ("JASPER_FANIN_CAMILLA_COUPLING", Some("shm_ring")),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("200")),
                // 200*2 = 400 >= 2*200 buffer floor, so the buffer guard passes
                // and the slot-shear guard is what fires.
                ("JASPER_FANIN_INPUT_BUFFER_FRAMES", Some("4096")),
                ("JASPER_FANIN_OUTPUT_BUFFER_FRAMES", Some("1024")),
            ],
            || {
                let err = Config::from_env()
                    .expect_err("non-128-multiple period under shm_ring must error");
                let msg = format!("{:#}", err);
                assert!(
                    msg.contains("multiple") && msg.contains("slot"),
                    "expected slot-shear error, got: {}",
                    msg,
                );
            },
        );
    }

    #[test]
    fn non_shm_ring_tolerates_odd_period() {
        // The 128-multiple guard is scoped to shm_ring: loopback with a
        // non-128-multiple period must still parse (byte-identical to today).
        with_env(
            &[
                ("JASPER_FANIN_CAMILLA_COUPLING", Some("loopback")),
                ("JASPER_FANIN_PERIOD_FRAMES", Some("200")),
                ("JASPER_FANIN_INPUT_BUFFER_FRAMES", Some("4096")),
                ("JASPER_FANIN_OUTPUT_BUFFER_FRAMES", Some("1024")),
            ],
            || {
                let cfg = Config::from_env().expect("loopback tolerates odd period");
                assert_eq!(cfg.period_frames, 200);
                assert_eq!(cfg.camilla_coupling, Coupling::Loopback);
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
}
