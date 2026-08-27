// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Configuration for the outputd daemon.
//!
//! Defaults keep `jasper-outputd --once` safe in a developer shell:
//! fake backend, no sockets unless the caller sets them. The systemd
//! unit opts into the real ALSA backend and runtime sockets with
//! explicit `JASPER_OUTPUTD_*` environment lines.

use anyhow::{Context, Result};
use jasper_env::{env_f32, env_parse, env_str};

use crate::dac_content::{
    ChannelPick, SUB_DEFAULT_CORNER_HZ, SUB_MAX_CORNER_HZ, SUB_MIN_CORNER_HZ,
};
use crate::types::{SampleFormat, SAMPLE_RATE};

pub const DEFAULT_PERIOD_FRAMES: u32 = 1024;
pub const DEFAULT_DAC_BUFFER_FRAMES: u32 = 3072;
pub const DEFAULT_CHIP_REF_SAMPLE_RATE: u32 = 16_000;
pub const DEFAULT_CHIP_REF_PERIOD_FRAMES: u32 = 128;
pub const DEFAULT_CHIP_REF_BUFFER_FRAMES: u32 = 256;
pub const DEFAULT_DUAL_MAX_DELAY_DELTA_FRAMES: i64 = 48;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendMode {
    Fake,
    Alsa,
}

impl BackendMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Fake => "fake",
            Self::Alsa => "alsa",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContentBridgeMode {
    /// The RETIRED snd-aloop route's name, kept only so the refusals that park
    /// a box carrying it can say which route it asked for. Nothing serves it:
    /// the composite park, the round-trip lane's own bridge requirement and —
    /// for every other shape — `main`'s run loop, which finds no ring to read
    /// and parks at the first period, are the three exits it reaches.
    Direct,
    /// The one transport (ADR-0100): read the post-DSP program from an n-slot
    /// SHM ping-pong ring the CamillaDSP-playback ALSA ioplug writes. This is
    /// what an UNDECLARED `JASPER_OUTPUTD_CONTENT_BRIDGE` resolves to. See
    /// `jasper_ring` and `shm_ring_source.rs`.
    ShmRing,
}

/// SHM ring reader settings (PROTOTYPE — only meaningful when
/// `content_bridge_mode == ShmRing`). Slot frames are NOT a separate env: the
/// ring's `period_frames` is always outputd's `period_frames`, one less drift
/// axis. `n_slots` defaults to 2 (ping-pong); 3 is the documented degraded
/// widening, 4..=16 is negotiation headroom. The ceiling is 16 because
/// CamillaDSP's playback BufferManager needs an ALSA buffer
/// (== `n_slots * period_frames`) that clears both its negotiated size
/// (next_pow2(3*chunksize)) and its `target_level`; at 4 slots the 512-frame
/// buffer was below both and the rate controller wound up into stall flapping.
/// Kept in lockstep with `MAX_N_SLOTS` (rust/jasper-ring/src/layout.rs) and
/// `JTS_RING_MAX_SLOTS` (c/jts-ring-ioplug/jts_ring_shm.h).
pub const DEFAULT_SHM_RING_PATH: &str = "/dev/shm/jts-ring/content.ring";
/// The ACTIVE ring's file — a roleful (crossover) box's POST-crossover
/// per-driver hop, distinct from `DEFAULT_SHM_RING_PATH`'s full-range stereo
/// program. Mirrored by `jasper.fanin_coupling.DEFAULT_OUTPUTD_ACTIVE_RING_PATH`
/// and by `pcm.jts_ring_active_playback`'s `path` in the ring conf.d; the three
/// literals are pinned equal by `tests/test_ring_active_endpoint.py`.
///
/// This constant is READ BY THE ALLOWLIST in `Config::from_env`, which is the
/// only reason outputd knows the name at all — it never defaults to this path.
pub const DEFAULT_ACTIVE_SHM_RING_PATH: &str = "/dev/shm/jts-ring/active-content.ring";
pub const DEFAULT_SHM_RING_SLOTS: u32 = 2;
pub const MIN_SHM_RING_SLOTS: u32 = 2;
pub const MAX_SHM_RING_SLOTS: u32 = 16;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShmRingConfig {
    pub path: String,
    pub n_slots: u32,
}

/// Final-output transport SHAPE — clock-domain shape, not DAC id. The
/// transport dispatches on this; channel width + map ride as data, so a new
/// DAC of an established shape adds no variant here.
///
/// `Composite` was named `DualApple` before the DAC-agnostic generalization;
/// the `dual_apple` wire string is still accepted on input (parse alias) for
/// one release while reconciler env catches up.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SinkMode {
    /// One coherent ALSA device at any width (single Apple 2ch, DAC8x 8ch, …).
    SingleAlsa,
    /// Two clock-independent child DACs driven as one composite (dual Apple).
    Composite,
}

impl SinkMode {
    /// The `/state` wire value. The composite shape KEEPS the stable
    /// `dual_apple` wire string through this type rename — the documented
    /// lower-risk option (HANDOFF-speaker-output-reference.md Observability:
    /// "keep the wire value stable while the type is renamed"), so the doctor,
    /// `/state` consumers, and snapshot contracts are untouched here. The wire
    /// migration to a width-agnostic `composite` block is a separate change.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SingleAlsa => "single_alsa",
            Self::Composite => "dual_apple",
        }
    }
}

impl ContentBridgeMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Direct => "direct",
            Self::ShmRing => "shm_ring",
        }
    }
}

/// Every spelling the REMOVED `rate_match` content bridge answered to.
///
/// The bridge itself is gone (git history holds it); `shm_ring` is the
/// frame-bounded transport that replaced it, and since ADR-0100 it is the ONLY
/// one. Kept so the parse can name what a migrating box's
/// `/var/lib/jasper/outputd.env` still asks for.
///
/// These used to fail SAFE to [`ContentBridgeMode::Direct`] rather than bail,
/// because bailing would have parked a box whose only sin was a stale env line
/// while `direct` still carried audio. `direct` carries nothing now, so that
/// trade is gone: fail-safe would buy the box no transport, only a park labelled
/// as the composite/round-trip/stereo-sink refusal that happened to catch it
/// next — or, on a plain stereo box, the first-period refusal, three layers from
/// the stale line that caused it. Bailing HERE parks at the same exit 78 and
/// names the key.
pub const REMOVED_RATE_MATCH_BRIDGE_SPELLINGS: &[&str] =
    &["rate_match", "ratematch", "rate-matched", "rate_matched"];

#[derive(Debug, Clone)]
pub struct Config {
    pub backend: BackendMode,
    pub sink_mode: SinkMode,
    pub content_channels: u16,
    /// The DECLARED wire format of the post-DSP CONTENT hop — the SHM ring
    /// between CamillaDSP and outputd. [`crate::shm_ring_source::ShmRingSource`]
    /// builds the ring geometry from it, and the ring's attach validates it
    /// against the header the writer created, so a declaration that disagrees
    /// with the live ring parks at startup rather than being read at the wrong
    /// width.
    ///
    /// A SEPARATE HOP from [`Self::declared_dac_format`], deliberately not a
    /// copy of it: this is the ring's wire, the DAC format is the hardware
    /// edge, and a box can legitimately run one wide and the other narrow — a
    /// wide content hop into a USB dongle that only offers S16 is a real
    /// configuration. Reading one axis off the other would silently mis-declare
    /// whichever hop the box did not name.
    ///
    /// Unset or blank falls back to `S16_LE`.
    /// `deploy/bin/jasper-audio-hardware-reconcile` emits this key on every
    /// box, ahead of the per-hardware branches, from
    /// `jasper.fanin_coupling.content_lane_format_for_coupling`: `S32_LE` by
    /// default, because the ring wire's resolver defaults wide. `S16_LE` on a
    /// ring box means an explicit operator rollback pin
    /// (`JASPER_FANIN_RING_WIRE_FORMAT=S16_LE`), not the ordinary case.
    /// Absence means a box that has not yet reconciled (fresh install) or
    /// whose probe could not run — not "every box".
    pub content_format: SampleFormat,
    pub dac_pcm: String,
    /// The registry-DECLARED format of the final hardware edge behind
    /// `dac_pcm` — the format the coherent single-DAC ALSA backend REQUESTS
    /// (`set_format`) and then proves it got, by reading the installed
    /// `hw_params` back. It also selects the edge's write path (see
    /// [`crate::types::ProgramSample`]): the internal program is i32, so an
    /// `S32Le` edge is already the spine's own width and converts nothing, while
    /// an `S16Le` edge is where the single output-path quantization happens.
    ///
    /// Declared by the DAC registry (`DacProfile.final_edge_format`) and
    /// emitted by `jasper-audio-hardware-reconcile` as
    /// `JASPER_OUTPUTD_DAC_FORMAT`, from the profile of whichever sink is armed
    /// — so on a composite box this is the COMPOSITE profile's declaration, and
    /// BOTH of its children request it. One value for the pair on purpose:
    /// per-child divergence is not modelled, because the one registered
    /// composite profile pairs two of the same dongle. The fake backend opens no
    /// edge at all and converts nothing.
    pub declared_dac_format: SampleFormat,
    pub dual_dac_a_pcm: Option<String>,
    pub dual_dac_b_pcm: Option<String>,
    pub dual_max_delay_delta_frames: i64,
    pub sample_rate: u32,
    pub period_frames: u32,
    pub dac_buffer_frames: u32,
    pub content_bridge_mode: ContentBridgeMode,
    /// PROTOTYPE SHM ring reader settings. `Some` iff
    /// `content_bridge_mode == ShmRing`; `None` (the default) is byte-identical
    /// to today. See `ShmRingConfig`.
    pub shm_ring: Option<ShmRingConfig>,
    pub chip_ref_pcm: Option<String>,
    pub chip_ref_sample_rate: u32,
    pub chip_ref_period_frames: u32,
    pub chip_ref_buffer_frames: u32,
    /// Observe-only label (chip-AEC Layer 0): when the reconciler armed the
    /// chip-ref writer FOR DRIFT MEASUREMENT on the software-AEC3 path (not
    /// for production chip-AEC), this is true. It changes NO outputd
    /// behavior — the chip-ref writer already keys off `chip_ref_pcm`; this
    /// just lets `/state` self-describe why the writer is running.
    pub chip_ref_observe: bool,
    pub chip_ref_tee_path: Option<String>,
    pub reference_udp_target: Option<String>,
    pub control_socket_path: Option<String>,
    /// OPTIONAL multi-room round-trip lane (Increment 3,
    /// HANDOFF-multiroom.md §2): a raw-PCM FIFO a grouping member's
    /// snapclient writes (`--player file:`). When set, the DAC loop is
    /// fed from it via `dac_content::DacContentSource`, falling back to
    /// the direct content PCM whenever the FIFO starves (inv-B — never
    /// silence). `None` (default — solo) is byte-identical to today.
    pub dac_content_fifo: Option<String>,
    /// Which channel of the shared stereo program this speaker plays
    /// from the round-trip lane (channel-split vocabulary; default
    /// stereo = passthrough). Only meaningful with `dac_content_fifo`.
    pub dac_content_channel: ChannelPick,
    /// Optional LR4 high-pass corner for MAIN channels in a wireless-sub
    /// bond. `None` means full-range mains. Invalid env values resolve to
    /// `None` (fail-closed to full-range, never startup crash / stuck
    /// bass-light). Ignored for `ChannelPick::Sub`.
    pub dac_content_highpass_hz: Option<f64>,
    /// Per-member level trim on the round-trip lane (dB, ALWAYS <= 0 —
    /// pair balancing attenuates the LOUDER speaker, never boosts;
    /// positive values fail closed like the duck knob). Applied to the
    /// whole dac_content-armed content path including inv-B fallback
    /// periods, so a starvation transition never jumps in level.
    /// Reconciler-derived from JASPER_GROUPING_TRIM_DB; 0.0 = no trim.
    pub dac_content_trim_db: f32,
    /// OPTIONAL bonded-member TTS socket (Increment 5 PR-2,
    /// HANDOFF-multiroom.md §2): when set, outputd listens for the
    /// jasper-voice TTS protocol and mixes assistant audio at the final
    /// output stage via OutputCore — downstream of the round-trip,
    /// upstream of the reference publish (inv-A). `None` (default —
    /// solo, where fanin owns TTS) leaves the DAC loop byte-identical.
    pub tts_socket_path: Option<String>,
    /// Pending-audio budget for the TTS lane (frames).
    pub tts_max_pending_frames: u64,
    /// Program duck applied to CONTENT while voice requests it
    /// (PROGRAM_DUCK_ON). Negative dB; mirrors fanin's knob + fallback.
    pub tts_program_duck_db: f32,
    /// Versioned last-achieved assistant loudness for the bonded-member
    /// post-DSP mix. Separate file from fan-in's so a box that flips between
    /// solo and bonded never cross-contaminates the two engines' learned
    /// quiet-room offsets. Mirrors JASPER_FANIN_ASSISTANT_REFERENCE_PATH.
    pub assistant_reference_path: String,
    /// Set by the reconciler on a 2-channel active-crossover sink — the one
    /// active case the bare `content_channels == 2` check cannot tell apart
    /// from a full-range stereo L/R sink (distributed-active Stage B). The real
    /// invariant for outputd's stereo-only features (the TTS mixer and the
    /// dac_content round-trip lane) is "full-range stereo L/R sink," NOT
    /// "exactly 2 channels": an active 2-way speaker (woofer/tweeter) is also
    /// 2-channel, so without this marker those features would WRONGLY arm on
    /// it — mixing / channel-picking post-crossover sends full-range audio to
    /// the tweeter (unsafe).
    /// Wider active sinks (composite, >2ch) are already excluded from the
    /// stereo-only FEATURES by their channel width. That is why the reconciler
    /// historically did not set this for them — but since P8b item 1 it DOES,
    /// on a composite whose active-ring endpoint it accepts, because this
    /// marker is also half of the `ring_active_ok` pair. Setting it costs the
    /// composite nothing on the feature side (width already excluded it) and is
    /// what lets `from_env` admit the ACTIVE ring. Default false (solo/passive)
    /// is byte-identical to today.
    pub active_lane: bool,
    /// The reconciler's declaration that the post-DSP endpoint is the ACTIVE
    /// ring (`DEFAULT_ACTIVE_SHM_RING_PATH`) rather than the full-range stereo
    /// Ring B. Written in the SAME helper, from the SAME decision, as
    /// `active_lane`; `from_env` bails on the incoherent pair and, under
    /// `ShmRing`, enforces the ring-path allowlist both ways.
    ///
    /// Recorded on the parsed config so the pairing that was ACCEPTED is
    /// inspectable, rather than surviving only as a control-flow decision
    /// inside `from_env`. Default false is byte-identical to today.
    pub ring_active_endpoint: bool,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let backend = match env_str("JASPER_OUTPUTD_BACKEND", "fake")
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "fake" => BackendMode::Fake,
            "alsa" => BackendMode::Alsa,
            other => {
                anyhow::bail!(
                    "JASPER_OUTPUTD_BACKEND must be one of fake, alsa; got {:?}",
                    other
                )
            }
        };
        let sample_rate = env_u32("JASPER_OUTPUTD_SAMPLE_RATE", SAMPLE_RATE)?;
        if sample_rate != SAMPLE_RATE {
            anyhow::bail!(
                "JASPER_OUTPUTD_SAMPLE_RATE={} is unsupported; outputd core is fixed at {} Hz",
                sample_rate,
                SAMPLE_RATE
            );
        }

        let sink_mode = match env_str("JASPER_OUTPUTD_SINK", "single_alsa")
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "single" | "single_alsa" | "alsa" => SinkMode::SingleAlsa,
            // `dual_apple` / `dual_apple_usb_c_dac_4ch` are accepted parse
            // aliases for one release while reconciler env migrates to the
            // shape-named `composite`.
            "composite" | "dual_apple" | "dual_apple_usb_c_dac_4ch" => SinkMode::Composite,
            other => {
                anyhow::bail!(
                    "JASPER_OUTPUTD_SINK must be one of single_alsa, composite (alias dual_apple); got {:?}",
                    other
                )
            }
        };
        let period_frames = env_u32("JASPER_OUTPUTD_PERIOD_FRAMES", DEFAULT_PERIOD_FRAMES)?;
        let dac_buffer_frames = env_u32(
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
            DEFAULT_DAC_BUFFER_FRAMES,
        )?;
        // UNDECLARED == the one transport. ADR-0100 makes `shm_ring` the only
        // upstream outputd serves, so a box that names nothing gets it; the
        // `direct` spellings survive only to be REFUSED downstream — by the
        // composite park, by the round-trip lane's own requirement, and (for
        // every other shape) at the first period, where there is no ring to
        // read. Mirrors `jasper-fanin`'s coupling accept-set.
        let raw_content_bridge = env_str("JASPER_OUTPUTD_CONTENT_BRIDGE", "shm_ring")
            .trim()
            .to_ascii_lowercase();
        let content_bridge_mode = match raw_content_bridge.as_str() {
            "direct" | "off" | "disabled" => ContentBridgeMode::Direct,
            "shm_ring" | "shmring" | "ring" => ContentBridgeMode::ShmRing,
            other if REMOVED_RATE_MATCH_BRIDGE_SPELLINGS.contains(&other) => {
                // The deleted rate_match bridge, named rather than lumped in
                // with a typo — see REMOVED_RATE_MATCH_BRIDGE_SPELLINGS for why
                // this bails now instead of resolving `direct`.
                eprintln!(
                    "event=outputd.content_bridge.removed_value requested={other} \
                     action=park"
                );
                anyhow::bail!(
                    "PARKED: JASPER_OUTPUTD_CONTENT_BRIDGE={:?} is the rate_match \
                     content bridge, which was deleted. The SHM ring is the one \
                     transport (ADR-0100), so there is nothing behind this value \
                     to fall back to. Remove the stale \
                     JASPER_OUTPUTD_CONTENT_BRIDGE line from \
                     /var/lib/jasper/outputd.env — an UNDECLARED bridge is the \
                     ring — or set it to shm_ring.",
                    other
                )
            }
            other => {
                anyhow::bail!(
                    "JASPER_OUTPUTD_CONTENT_BRIDGE must be one of shm_ring (the one \
                     transport, and what an UNDECLARED key resolves to) or direct \
                     (the retired route, parsed only so the park refusals can name \
                     it); got {:?}",
                    other
                )
            }
        };
        let chip_ref_buffer_frames = env_u32(
            "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES",
            DEFAULT_CHIP_REF_BUFFER_FRAMES,
        )?;
        let chip_ref_sample_rate = env_u32(
            "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE",
            DEFAULT_CHIP_REF_SAMPLE_RATE,
        )?;
        let chip_ref_period_frames = env_u32(
            "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES",
            DEFAULT_CHIP_REF_PERIOD_FRAMES,
        )?;
        if sample_rate % chip_ref_sample_rate != 0 {
            anyhow::bail!(
                "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE={} must divide JASPER_OUTPUTD_SAMPLE_RATE={} for exact chip-reference downsampling",
                chip_ref_sample_rate,
                sample_rate
            );
        }
        validate_buffer(
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
            dac_buffer_frames,
            period_frames,
            "JASPER_OUTPUTD_PERIOD_FRAMES",
        )?;
        validate_buffer(
            "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES",
            chip_ref_buffer_frames,
            chip_ref_period_frames,
            "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES",
        )?;

        let default_dac_pcm = match sink_mode {
            SinkMode::SingleAlsa => "outputd_dac",
            SinkMode::Composite => "dual_apple_usb_c_dac_4ch",
        };
        // Active-lane width carried as DATA (not a per-DAC branch): the
        // reconciler emits JASPER_OUTPUTD_ACTIVE_CHANNELS from the DacProfile's
        // active_outputd_lane_channels. A coherent single DAC reads + writes
        // this width end-to-end (single Apple 2ch == today; DAC8x 8ch); the
        // composite shape is fixed at 4 (two stereo children).
        let active_channels = env_optional_u16("JASPER_OUTPUTD_ACTIVE_CHANNELS", 2, 8)?;
        let content_channels = match sink_mode {
            SinkMode::SingleAlsa => active_channels.unwrap_or(2),
            SinkMode::Composite => {
                if let Some(width) = active_channels {
                    if width != 4 {
                        anyhow::bail!(
                            "JASPER_OUTPUTD_ACTIVE_CHANNELS={} is invalid for the \
                             composite sink, which is fixed at 4 (two stereo children)",
                            width
                        );
                    }
                }
                4
            }
        };
        // The final HARDWARE edge's declared format, emitted by
        // jasper-audio-hardware-reconcile from the DAC registry's
        // DacProfile.final_edge_format. The coherent single-DAC ALSA backend
        // REQUESTS this format at the edge and verifies the installed
        // hw_params match. It does not change the internal program's width —
        // that is always `ProgramSample` (i32) — it decides whether the edge
        // converts at all, and how: S32 writes the spine through untouched, S16
        // narrows to 16 bits, S24_3LE narrows to 24 significant bits and packs
        // three little-endian bytes per sample.
        //
        // Unset or blank == the historical S16_LE default: the reconciler
        // writes an explicit EMPTY value for an unrecognized DAC (it has no
        // profile to query), and a box that predates the emit has the name
        // unset. Any OTHER value can only mean registry/reconciler drift, so
        // bail into the EX_CONFIG park rather than guess an edge format. Match
        // is exact (trim only, no case folding): ALSA spells these uppercase
        // and the only writer emits the registry literal, so a case variant is
        // itself drift worth surfacing.
        //
        // `S24_3LE` is accepted and has a full write path (a packed 3-byte edge,
        // `alsa_backend::write_dac_period`'s third arm). The single Apple USB-C
        // dongle profile declares it, so the reconciler emits it and a
        // single-dongle box resolves it here. The `dual_apple_usb_c_dac_4ch`
        // composite does NOT: the paired sink has no packed-24 child write path
        // and refuses that width at open (jaspercurry/JTS#2257 is the unlock),
        // so this name reaching a composite is registry/transport drift.
        let declared_dac_format = match env_str("JASPER_OUTPUTD_DAC_FORMAT", "").trim() {
            "" | "S16_LE" => SampleFormat::S16Le,
            "S24_3LE" => SampleFormat::S24_3Le,
            "S32_LE" => SampleFormat::S32Le,
            other => {
                anyhow::bail!(
                    "JASPER_OUTPUTD_DAC_FORMAT must be one of S16_LE, S24_3LE, S32_LE \
                     (or empty for the S16_LE default); got {:?}",
                    other
                )
            }
        };
        // The CONTENT lane's declared format — the OTHER hop, parsed with the
        // same exact-match/park-on-garbage semantics as the DAC edge above and
        // deliberately from its OWN name. Two hops, two declarations: reusing
        // the DAC value here would tie the ring wire's width to the hardware
        // edge's, and the two are independent by design (an S32 content hop
        // into an S16-only dongle edge is a supported shape).
        //
        // Unset or blank falls back to the historical S16_LE, but that is
        // no longer every live box: jasper-audio-hardware-reconcile emits this
        // name on every box now (S32_LE — the ring wire's resolver defaults
        // wide — see
        // jasper.fanin_coupling.content_lane_format_for_coupling). S16_LE on
        // a ring box means an explicit operator rollback pin
        // (JASPER_FANIN_RING_WIRE_FORMAT=S16_LE), not the default. The axis is
        // no longer byte-identical on deploy; absence now means unreconciled
        // or probe-unavailable, not "nothing writes it".
        //
        // `S24_3LE` parses on this axis too, for ONE vocabulary rather than two:
        // both hops read the same `SampleFormat`, and an axis-asymmetric parser
        // would be a second, silently narrower spelling of the same enum. It is
        // NOT an ingestible lane width — outputd has no packed-24 READ path, and
        // `alsa_backend`'s ingest refuses it park-class, at the same exit 78 an
        // unknown value takes here. Nor can the reconciler emit it: the
        // per-coupling policy named above answers only S16_LE or S32_LE, so a
        // hand-set value is the only way to reach that refusal.
        let content_format = match env_str("JASPER_OUTPUTD_CONTENT_FORMAT", "").trim() {
            "" | "S16_LE" => SampleFormat::S16Le,
            "S24_3LE" => SampleFormat::S24_3Le,
            "S32_LE" => SampleFormat::S32Le,
            other => {
                anyhow::bail!(
                    "JASPER_OUTPUTD_CONTENT_FORMAT must be one of S16_LE, S24_3LE, S32_LE \
                     (or empty for the S16_LE default); got {:?}",
                    other
                )
            }
        };
        let dual_dac_a_pcm = env_optional("JASPER_OUTPUTD_DUAL_DAC_A_PCM");
        let dual_dac_b_pcm = env_optional("JASPER_OUTPUTD_DUAL_DAC_B_PCM");
        if sink_mode == SinkMode::Composite
            && (dual_dac_a_pcm.is_none() || dual_dac_b_pcm.is_none())
        {
            anyhow::bail!(
                "JASPER_OUTPUTD_SINK=composite requires JASPER_OUTPUTD_DUAL_DAC_A_PCM and JASPER_OUTPUTD_DUAL_DAC_B_PCM"
            );
        }
        if sink_mode == SinkMode::Composite && dual_dac_a_pcm == dual_dac_b_pcm {
            anyhow::bail!(
                "JASPER_OUTPUTD_SINK=composite requires distinct JASPER_OUTPUTD_DUAL_DAC_A_PCM and JASPER_OUTPUTD_DUAL_DAC_B_PCM"
            );
        }
        let dual_max_delay_delta_frames = env_i64(
            "JASPER_OUTPUTD_DUAL_MAX_DELAY_DELTA_FRAMES",
            DEFAULT_DUAL_MAX_DELAY_DELTA_FRAMES,
        )?;
        if dual_max_delay_delta_frames < 0 {
            anyhow::bail!(
                "JASPER_OUTPUTD_DUAL_MAX_DELAY_DELTA_FRAMES={} must be >= 0",
                dual_max_delay_delta_frames
            );
        }

        // Multi-room round-trip lane (Increment 3). Fail-loud contract
        // guards, written as ALLOWLISTS (not denylists): DacContentSource
        // is structurally stereo (2-channel periods — see period_bytes),
        // and it IS the content source, so the lane is valid ONLY on the
        // single-ALSA Direct content path. Rejecting "anything that is not
        // the supported mode" — rather than "the one unsupported mode that
        // exists today" — makes a future sink / bridge mode fail CLOSED
        // (loud at startup) instead of silently mis-sizing content_buf.
        let dac_content_fifo = env_optional("JASPER_OUTPUTD_DAC_CONTENT_FIFO");
        let dac_content_trim_db = env_f32("JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB", 0.0)?;
        if dac_content_trim_db > 0.0 {
            anyhow::bail!(
                "JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB={} must be <= 0 (pair \
                 balancing trims the louder speaker down; a boost would \
                 cost headroom and risk hearing safety)",
                dac_content_trim_db
            );
        }
        if dac_content_trim_db < -24.0 {
            anyhow::bail!(
                "JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB={} is below the -24 dB \
                 floor — a trim that deep means the pair is misconfigured, \
                 not unbalanced",
                dac_content_trim_db
            );
        }
        let dac_content_channel =
            ChannelPick::parse(&env_str("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", "stereo"))
                .map_err(anyhow::Error::msg)?;
        // The receiver-side dumb-subwoofer corner. Only meaningful when the
        // channel is "sub"; we still resolve it unconditionally (cheap) so
        // the construction below is a simple match. A sub MUST NEVER play
        // full-range, so a missing/blank/out-of-range value resolves to a
        // safe low-pass (default 80 Hz, clamped to 40..200) with a warn —
        // never a bypass. Mirrors GroupingConfig.crossover_hz's 40..200.
        let dac_content_channel = if let ChannelPick::Sub(_) = dac_content_channel {
            let raw = env_f64("JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ", SUB_DEFAULT_CORNER_HZ)?;
            let corner = if !(SUB_MIN_CORNER_HZ..=SUB_MAX_CORNER_HZ).contains(&raw) {
                let clamped = raw.clamp(SUB_MIN_CORNER_HZ, SUB_MAX_CORNER_HZ);
                eprintln!(
                    "event=outputd.dac_content.sub_corner_clamped requested={raw} \
                     clamped={clamped} range={SUB_MIN_CORNER_HZ}..{SUB_MAX_CORNER_HZ}"
                );
                clamped
            } else {
                raw
            };
            ChannelPick::Sub(corner)
        } else {
            dac_content_channel
        };
        let dac_content_highpass_hz = match (
            dac_content_channel,
            env_optional("JASPER_OUTPUTD_DAC_CONTENT_HP_HZ"),
        ) {
            (ChannelPick::Sub(_), _) | (_, None) => None,
            (_, Some(raw)) => match raw.trim().parse::<f64>() {
                Ok(v) if v.is_finite() && (SUB_MIN_CORNER_HZ..=SUB_MAX_CORNER_HZ).contains(&v) => {
                    Some(v)
                }
                _ => {
                    eprintln!(
                        "event=outputd.dac_content.main_highpass_invalid \
                         requested={raw:?} action=play_full_range"
                    );
                    None
                }
            },
        };
        if dac_content_fifo.is_some() {
            if content_bridge_mode != ContentBridgeMode::Direct {
                anyhow::bail!(
                    "JASPER_OUTPUTD_DAC_CONTENT_FIFO requires \
                     JASPER_OUTPUTD_CONTENT_BRIDGE=direct (the round-trip lane \
                     is itself the content source; it cannot share the DAC \
                     with another content-source policy)"
                );
            }
            if sink_mode != SinkMode::SingleAlsa {
                anyhow::bail!(
                    "JASPER_OUTPUTD_DAC_CONTENT_FIFO requires \
                     JASPER_OUTPUTD_SINK=single_alsa (the round-trip lane is a \
                     stereo single-DAC grouping-member path)"
                );
            }
        }

        let tts_socket_path = env_optional("JASPER_OUTPUTD_TTS_SOCKET");
        let tts_max_pending_frames = env_u64(
            "JASPER_OUTPUTD_TTS_MAX_PENDING_FRAMES",
            crate::tts::DEFAULT_MAX_PENDING_FRAMES,
        )?;
        // Mirrors fanin's duck knob shape: dedicated env first, the
        // shared legacy JASPER_DUCK_DB as fallback, -25 dB default.
        let tts_program_duck_db = match std::env::var("JASPER_OUTPUTD_TTS_PROGRAM_DUCK_DB") {
            Ok(s) if !s.trim().is_empty() => env_f32("JASPER_OUTPUTD_TTS_PROGRAM_DUCK_DB", -25.0)?,
            _ => env_f32("JASPER_DUCK_DB", -25.0)?,
        };
        if tts_program_duck_db > 0.0 {
            anyhow::bail!(
                "JASPER_OUTPUTD_TTS_PROGRAM_DUCK_DB={} must be <= 0 (a duck \
                 attenuates; positive gain on the program is never allowed)",
                tts_program_duck_db
            );
        }

        // Distributed-active belt-and-suspenders: the reconciler marks a
        // 2-channel active-crossover sink — the one active case channel width
        // cannot distinguish from a full-range stereo L/R sink. See the
        // latent-guard hazard in docs/HANDOFF-distributed-active.md.
        let active_lane = env_bool("JASPER_OUTPUTD_ACTIVE_LANE", false);

        // The reconciler's declaration that this box's post-DSP endpoint is the
        // ACTIVE ring. Written by the SAME helper, from the SAME decision, as
        // ACTIVE_LANE above — the two are one fact, so the pair is coherent or
        // it is a writer bug. Mode-INDEPENDENT because an incoherent pair means
        // the writer is broken regardless of which content bridge is selected;
        // there is no bridge under which "active endpoint, no active lane" is a
        // state a healthy box can be in.
        let ring_active_endpoint = env_bool("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", false);
        if ring_active_endpoint && !active_lane {
            anyhow::bail!(
                "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT is set without \
                 JASPER_OUTPUTD_ACTIVE_LANE; jasper-audio-hardware-reconcile writes \
                 both from one decision, so an incoherent pair is a writer fault, \
                 never a state — refusing to start rather than guess which half is \
                 right"
            );
        }
        // The ONE condition under which reading the active ring is legal: a
        // marked active lane on a sink that outputd itself drives — SingleAlsa
        // or the dual-Apple Composite. The marker alone is not enough; the lane
        // must be armed too.
        //
        // COMPOSITE WAS ADMITTED IN P8b ITEM 1. The old term read
        // `sink_mode == SinkMode::SingleAlsa`, excluding Composite because "the
        // ring ioplug is one device on one clock domain." That reason did not
        // survive re-derivation: the ring is the CamillaDSP -> outputd hop, and
        // the composite split lives entirely DOWNSTREAM of it, inside this
        // daemon, which reads ONE interleaved 4-channel period and calls
        // `deinterleave_4ch_to_dual_stereo`. The ring never sees a child. The
        // exclusion was right as a ring-v2 SCOPE call and wrong as a physics
        // claim.
        //
        // What still holds a composite off the ring: `link=ok` is a precondition
        // the composite sink enforces for itself (alsa_backend.rs), and the
        // household-facing arm is an explicit operator action gated by the
        // Python preflights. Widening this term ENABLES that arm; it does not
        // perform one.
        //
        // The two stereo-only features below are NOT widened with it and must
        // not be: `is_full_range_stereo_lr_sink` still requires SingleAlsa with
        // exactly 2 content channels, so the outputd TTS mixer and the
        // dac_content ChannelPick lane stay off a composite exactly as before.
        let ring_active_ok = active_lane
            && ring_active_endpoint
            && matches!(sink_mode, SinkMode::SingleAlsa | SinkMode::Composite);

        // PARKED: the passive composite (dual-DAC) shape.
        //
        // The park the (Composite, Direct) content-PCM arm used to carry, re-keyed
        // onto what survives the loopback route's deletion. The discriminator is
        // the ACTIVE-ring endpoint marker, not the content bridge: a ROLEFUL
        // composite's post-crossover program rides the ACTIVE ring and is the
        // shape jts.local runs today, while the PASSIVE dual-DAC shape resolves
        // no ring of either kind and has no transport at all.
        //
        // Ahead of the stereo-sink predicate below on purpose — that one would
        // also refuse this box, but as a generic "not a full-range stereo sink",
        // which is the wrong account of a composite and names no issue.
        //
        // Failing HERE is the whole point: a `Config::from_env` error exits 78
        // (EX_CONFIG) before any ALSA device is touched, so the box parks
        // immediately with the reason named instead of running on a guess it can
        // never satisfy. No command is recommended: none can clear it.
        if sink_mode == SinkMode::Composite && !ring_active_endpoint {
            anyhow::bail!(
                "PARKED: a passive composite (dual-DAC) sink has no transport. \
                 The shm_ring transport carries a composite only for a ROLEFUL \
                 layout, whose post-crossover program rides the ACTIVE ring. \
                 Composite-on-ring for the passive shape is tracked as #2982; \
                 until it lands this box stays parked."
            );
        }

        // The shared safety predicate for outputd's stereo-only features: they
        // may arm ONLY on a full-range stereo L/R sink — single-ALSA, exactly
        // two channels, and NOT an active-crossover lane. Composite and
        // wide-active single sinks are excluded by width; a 2-channel active
        // sink is excluded by the explicit active_lane marker. Mixing a
        // stereo feed on any of those mis-sizes buffers on live drivers, or
        // (on an active lane) sends full-range audio to the tweeter, so fail
        // closed at startup.
        let is_full_range_stereo_lr_sink =
            sink_mode == SinkMode::SingleAlsa && content_channels == 2 && !active_lane;

        if content_bridge_mode != ContentBridgeMode::Direct
            && !is_full_range_stereo_lr_sink
            && !ring_active_ok
        {
            anyhow::bail!(
                "JASPER_OUTPUTD_CONTENT_BRIDGE={} requires a full-range stereo \
                 L/R sink: JASPER_OUTPUTD_SINK=single_alsa, JASPER_OUTPUTD_ACTIVE_CHANNELS=2, \
                 and JASPER_OUTPUTD_ACTIVE_LANE unset (a content-bridge lane is a stereo-only \
                 path; on an active-crossover lane it would feed full-range audio that \
                 is then split to the tweeter) — OR an armed ACTIVE-ring endpoint \
                 (JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT with JASPER_OUTPUTD_ACTIVE_LANE on a \
                 single_alsa or dual_apple sink), whose ring carries the POST-crossover \
                 per-driver program and therefore is not the stereo path this predicate \
                 guards",
                content_bridge_mode.as_str()
            );
        }
        if tts_socket_path.is_some() && !is_full_range_stereo_lr_sink {
            anyhow::bail!(
                "JASPER_OUTPUTD_TTS_SOCKET requires a full-range stereo L/R sink: \
                 JASPER_OUTPUTD_SINK=single_alsa, JASPER_OUTPUTD_ACTIVE_CHANNELS=2, and \
                 JASPER_OUTPUTD_ACTIVE_LANE unset (the outputd TTS mixer is stereo-only and \
                 sits post-crossover; on an active-crossover lane — a 2-way speaker is also \
                 2-channel — it would send full-range speech to the tweeter. Active-mode \
                 voice rides fanin, upstream of the crossover, instead)"
            );
        }
        // The dumb round-trip dac_content ChannelPick lane is the third
        // stereo-L/R-only feature: it must never run on an active-crossover
        // lane, where picking a full-range channel straight to the DAC would
        // reach the tweeter post-crossover. (It does not gate on sink_mode here
        // — single-ALSA is enforced for this lane in the dac_content block
        // above — so it keeps its own shape rather than the shared predicate.)
        if dac_content_fifo.is_some() && (content_channels != 2 || active_lane) {
            anyhow::bail!(
                "JASPER_OUTPUTD_DAC_CONTENT_FIFO requires JASPER_OUTPUTD_ACTIVE_CHANNELS=2 \
                 and JASPER_OUTPUTD_ACTIVE_LANE unset (the round-trip lane is a stereo \
                 grouping-member path; on an active-crossover lane its ChannelPick would \
                 send a full-range channel to the tweeter)"
            );
        }

        // The SHM ring reader's settings. `Some` iff the ring is the resolved
        // transport; the predicate above already rejected it on any sink that is
        // neither a full-range stereo L/R sink nor an armed ACTIVE-ring endpoint
        // (which since P8b item 1 includes a composite), and the dac-content
        // guard (which requires Direct) already made the ring mutually exclusive
        // with the round-trip lane. The remaining validation is the slot count.
        let shm_ring = match content_bridge_mode {
            ContentBridgeMode::ShmRing => {
                let n_slots = env_u32("JASPER_OUTPUTD_SHM_RING_SLOTS", DEFAULT_SHM_RING_SLOTS)?;
                if !(MIN_SHM_RING_SLOTS..=MAX_SHM_RING_SLOTS).contains(&n_slots) {
                    anyhow::bail!(
                        "JASPER_OUTPUTD_SHM_RING_SLOTS={} must be between {} and {} \
                         (2 = ping-pong prototype, 3 = degraded widening, 4 = headroom)",
                        n_slots,
                        MIN_SHM_RING_SLOTS,
                        MAX_SHM_RING_SLOTS
                    );
                }
                Some(ShmRingConfig {
                    path: env_str("JASPER_OUTPUTD_SHM_RING_PATH", DEFAULT_SHM_RING_PATH),
                    n_slots,
                })
            }
            ContentBridgeMode::Direct => None,
        };

        // THE ALLOWLIST — positive equality in BOTH directions, never a
        // denylist. It refuses two distinct crossings:
        //
        //   1. an armed active endpoint reading the STEREO ring, and
        //   2. anything that is NOT an armed active endpoint reading the ACTIVE
        //      ring.
        //
        // (2) is the load-bearing one and the reason this exists. Without it, a
        // transient `output_topology.json` read failure clears ACTIVE_LANE (and
        // the marker), `content_channels` falls back to its default of 2, and
        // `is_full_range_stereo_lr_sink` goes TRUE — so outputd would attach the
        // ACTIVE ring as an ordinary stereo sink and unlock the post-crossover
        // TTS mixer onto a compression driver. On a 2-way box the widths are
        // equal, so no channel-count check can catch it, and there is no
        // content-PCM open left to fail loudly on a mis-declaration. Positive
        // equality against a NAMED path
        // constant is what keeps a future third ring from slipping through the
        // way a denylist would let it.
        //
        // SCOPED TO ShmRing, deliberately. A roleful box carrying a persisted
        // `direct` has no ring to read, so the "is the active path" side is
        // structurally false while `ring_active_ok` stays TRUE — an unscoped
        // biconditional would refuse it HERE, naming the ring-path allowlist for
        // a box whose actual fault is that it declared no transport. It parks
        // either way; this keeps the account of WHY correct, which is the one
        // thing the doctor and the park record read. The incoherent-pair bail
        // above stays mode-independent, because a broken writer is broken under
        // every bridge.
        if content_bridge_mode == ContentBridgeMode::ShmRing {
            let is_active_path = shm_ring
                .as_ref()
                .is_some_and(|r| r.path == DEFAULT_ACTIVE_SHM_RING_PATH);
            if is_active_path != ring_active_ok {
                anyhow::bail!(
                    "the active ring path ({}) may be read ONLY by an armed active \
                     endpoint, and an armed active endpoint may read ONLY that path: \
                     JASPER_OUTPUTD_SHM_RING_PATH={:?} (active_path={}), \
                     JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT={}, \
                     JASPER_OUTPUTD_ACTIVE_LANE={}, JASPER_OUTPUTD_SINK={}",
                    DEFAULT_ACTIVE_SHM_RING_PATH,
                    shm_ring.as_ref().map(|r| r.path.as_str()).unwrap_or(""),
                    is_active_path,
                    ring_active_endpoint,
                    active_lane,
                    sink_mode.as_str(),
                );
            }
        }

        let config = Self {
            backend,
            sink_mode,
            content_channels,
            content_format,
            dac_pcm: env_str("JASPER_OUTPUTD_DAC_PCM", default_dac_pcm),
            declared_dac_format,
            dual_dac_a_pcm,
            dual_dac_b_pcm,
            dual_max_delay_delta_frames,
            sample_rate,
            period_frames,
            dac_buffer_frames,
            content_bridge_mode,
            shm_ring,
            chip_ref_pcm: env_optional("JASPER_OUTPUTD_CHIP_REF_PCM"),
            chip_ref_sample_rate,
            chip_ref_period_frames,
            chip_ref_buffer_frames,
            chip_ref_observe: env_bool("JASPER_OUTPUTD_CHIP_REF_OBSERVE", false),
            chip_ref_tee_path: env_optional("JASPER_OUTPUTD_CHIP_REF_TEE_PATH"),
            reference_udp_target: env_optional("JASPER_OUTPUTD_REFERENCE_UDP_TARGET"),
            control_socket_path: env_optional("JASPER_OUTPUTD_CONTROL_SOCKET"),
            dac_content_fifo,
            dac_content_channel,
            dac_content_highpass_hz,
            dac_content_trim_db,
            tts_socket_path,
            tts_max_pending_frames,
            tts_program_duck_db,
            assistant_reference_path: env_str(
                "JASPER_OUTPUTD_ASSISTANT_REFERENCE_PATH",
                "/var/lib/jasper/outputd_assistant_volume_reference.json",
            ),
            active_lane,
            ring_active_endpoint,
        };
        if config.chip_ref_tee_path.is_some() && config.chip_ref_pcm.is_none() {
            anyhow::bail!("JASPER_OUTPUTD_CHIP_REF_TEE_PATH requires JASPER_OUTPUTD_CHIP_REF_PCM");
        }
        Ok(config)
    }
}

fn validate_buffer(
    name: &str,
    buffer_frames: u32,
    period_frames: u32,
    period_name: &str,
) -> Result<()> {
    let min_buffer_frames = period_frames.saturating_mul(2);
    if buffer_frames < min_buffer_frames {
        anyhow::bail!(
            "{}={} must be >= 2 x {}={} (minimum ALSA jitter margin)",
            name,
            buffer_frames,
            period_name,
            period_frames
        );
    }
    Ok(())
}

fn env_optional(name: &str) -> Option<String> {
    match std::env::var(name) {
        Ok(value) if !value.trim().is_empty() => Some(value),
        _ => None,
    }
}

fn env_u32(name: &str, default: u32) -> Result<u32> {
    let parsed = env_parse(name, default, "a positive integer")?;
    if parsed == 0 {
        anyhow::bail!("{} must be > 0", name);
    }
    Ok(parsed)
}

/// Parse an optional channel-width env var, validated to `[lo, hi]` when set.
/// `None` (unset) lets the caller fall back to the per-shape default.
fn env_optional_u16(name: &str, lo: u16, hi: u16) -> Result<Option<u16>> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => {
            let parsed = s
                .trim()
                .parse::<u16>()
                .with_context(|| format!("{} must be an integer; got {:?}", name, s))?;
            if parsed < lo || parsed > hi {
                anyhow::bail!("{} must be between {} and {}; got {}", name, lo, hi, parsed);
            }
            Ok(Some(parsed))
        }
        _ => Ok(None),
    }
}

fn env_u64(name: &str, default: u64) -> Result<u64> {
    env_parse(name, default, "a non-negative integer")
}

fn env_i64(name: &str, default: i64) -> Result<i64> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => s
            .trim()
            .parse::<i64>()
            .with_context(|| format!("{} must be an integer; got {:?}", name, s)),
        _ => Ok(default),
    }
}

fn env_bool(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(s) => matches!(
            s.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        Err(_) => default,
    }
}

fn env_f64(name: &str, default: f64) -> Result<f64> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => {
            let parsed = s
                .trim()
                .parse::<f64>()
                .with_context(|| format!("{} must be a number; got {:?}", name, s))?;
            if !parsed.is_finite() {
                anyhow::bail!("{} must be finite", name);
            }
            Ok(parsed)
        }
        _ => Ok(default),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// What a composite fixture must declare since #2285 P2 retired the default.
    ///
    /// `default_content_pcm`'s `(Composite, Direct)` arm used to guess
    /// `outputd_active_content_capture`; #2534 deleted that PCM, so the arm now
    /// REFUSES rather than guessing (the refusal is pinned by
    /// `a_composite_sink_on_the_direct_bridge_refuses_an_undeclared_content_pcm`).
    ///
    /// **Why so many fixtures need it, and why setting it is not papering over
    /// anything.** That refusal is the FIRST composite guard — line order puts it
    /// ahead of the child-PCM, distinct-PCM, delay-budget, active-channel-width,
    /// TTS-socket and dac-content-lane guards. So a composite fixture that
    /// declares no content PCM now trips this refusal BEFORE reaching the guard
    /// it exists to exercise, and its assertion reads a message about the wrong
    /// thing. Declaring the PCM restores each test to the path it was written
    /// for; every one of them keeps its ORIGINAL assertion, so a target guard
    /// that stopped biting would still fail its own test.
    ///
    /// The value is deliberately a name no ALSA config defines: nothing here
    /// opens a PCM, and a plausible-looking real name would invite a reader to
    /// think the lane is resolved rather than merely declared.
    fn with_env<F: FnOnce()>(vars: &[(&str, Option<&str>)], f: F) {
        let _guard = ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        // JASPER_DUCK_DB is the one non-prefixed var from_env reads (the
        // shared duck fallback) — scrub it too, or an ambient value from a
        // sourced jasper.env flakes the default-asserting tests. Mirrors
        // fanin's twin harness, which lists it for the same reason.
        let snapshot: Vec<(String, String)> = std::env::vars()
            .filter(|(k, _)| k.starts_with("JASPER_OUTPUTD_") || k == "JASPER_DUCK_DB")
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
    fn defaults_are_safe_for_developer_once_runs() {
        with_env(&[], || {
            let cfg = Config::from_env().unwrap();
            assert_eq!(cfg.backend, BackendMode::Fake);
            assert_eq!(cfg.sink_mode, SinkMode::SingleAlsa);
            assert_eq!(cfg.content_channels, 2);
            assert_eq!(cfg.dac_pcm, "outputd_dac");
            assert!(cfg.dual_dac_a_pcm.is_none());
            assert!(cfg.dual_dac_b_pcm.is_none());
            assert_eq!(cfg.sample_rate, SAMPLE_RATE);
            assert_eq!(cfg.period_frames, DEFAULT_PERIOD_FRAMES);
            assert_eq!(cfg.dac_buffer_frames, DEFAULT_DAC_BUFFER_FRAMES);
            assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::ShmRing);
            assert_eq!(cfg.chip_ref_sample_rate, DEFAULT_CHIP_REF_SAMPLE_RATE);
            assert_eq!(cfg.chip_ref_period_frames, DEFAULT_CHIP_REF_PERIOD_FRAMES);
            assert_eq!(cfg.chip_ref_buffer_frames, DEFAULT_CHIP_REF_BUFFER_FRAMES);
            assert!(cfg.chip_ref_pcm.is_none());
            // Observe mode is opt-in; off by default (zero cost).
            assert!(!cfg.chip_ref_observe);
            assert!(cfg.chip_ref_tee_path.is_none());
            assert!(cfg.reference_udp_target.is_none());
            assert!(cfg.control_socket_path.is_none());
            // Multi-room round-trip lane is OFF by default (solo contract).
            assert!(cfg.dac_content_fifo.is_none());
            assert_eq!(cfg.dac_content_channel, ChannelPick::Stereo);
            assert_eq!(cfg.dac_content_highpass_hz, None);
            // Active-crossover lane marker is off by default (solo/passive).
            assert!(!cfg.active_lane);
            // Learned quiet-room reference persists to outputd's own file so a
            // solo/bonded flip never cross-contaminates fan-in's learned value.
            assert_eq!(
                cfg.assistant_reference_path,
                "/var/lib/jasper/outputd_assistant_volume_reference.json"
            );
        });
    }

    #[test]
    fn parses_dac_content_lane_with_channel_pick() {
        with_env(
            &[
                (
                    "JASPER_OUTPUTD_DAC_CONTENT_FIFO",
                    Some("/run/jasper-grouping/member-content.fifo"),
                ),
                ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("left")),
                // The round-trip lane still names the retired route, so under
                // the one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(
                    cfg.dac_content_fifo.as_deref(),
                    Some("/run/jasper-grouping/member-content.fifo")
                );
                assert_eq!(cfg.dac_content_channel, ChannelPick::Left);
                assert_eq!(cfg.dac_content_highpass_hz, None);
            },
        );
    }

    #[test]
    fn main_highpass_uses_valid_corner_for_non_sub_channels() {
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The round-trip lane still names the retired route, so under the
                // one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("left")),
                ("JASPER_OUTPUTD_DAC_CONTENT_HP_HZ", Some("120")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.dac_content_channel, ChannelPick::Left);
                assert_eq!(cfg.dac_content_highpass_hz, Some(120.0));
            },
        );
    }

    #[test]
    fn main_highpass_invalid_or_zero_fails_closed_to_full_range() {
        for raw in ["", "0", "-5", "nope", "5000"] {
            with_env(
                &[
                    ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                    // The round-trip lane still names the retired route, so under the
                    // one transport it PARKS (#3118). Pinned here so these knob
                    // contracts keep their own subject.
                    ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                    ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("left")),
                    ("JASPER_OUTPUTD_DAC_CONTENT_HP_HZ", Some(raw)),
                ],
                || {
                    let cfg = Config::from_env().unwrap();
                    assert_eq!(cfg.dac_content_channel, ChannelPick::Left);
                    assert_eq!(cfg.dac_content_highpass_hz, None);
                },
            );
        }
    }

    #[test]
    fn sub_channel_uses_the_configured_crossover_corner() {
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The round-trip lane still names the retired route, so under the
                // one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("sub")),
                ("JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ", Some("120")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.dac_content_channel, ChannelPick::Sub(120.0));
                assert_eq!(cfg.dac_content_highpass_hz, None);
            },
        );
    }

    #[test]
    fn sub_channel_defaults_to_80hz_when_corner_absent() {
        // A "sub" must NEVER play full-range, so an absent corner picks a
        // safe default low-pass, not a bypass.
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The round-trip lane still names the retired route, so under the
                // one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("sub")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(
                    cfg.dac_content_channel,
                    ChannelPick::Sub(SUB_DEFAULT_CORNER_HZ)
                );
                assert_eq!(cfg.dac_content_highpass_hz, None);
            },
        );
    }

    #[test]
    fn sub_corner_is_clamped_to_the_valid_range() {
        // Below the floor clamps up; above the ceiling clamps down. The
        // reconciler validates 40..200 too, but config defends in depth so
        // a hand-set out-of-range value can never bypass or mis-tune.
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The round-trip lane still names the retired route, so under the
                // one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("sub")),
                ("JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ", Some("5")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.dac_content_channel, ChannelPick::Sub(SUB_MIN_CORNER_HZ));
                assert_eq!(cfg.dac_content_highpass_hz, None);
            },
        );
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The round-trip lane still names the retired route, so under the
                // one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("sub")),
                ("JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ", Some("5000")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.dac_content_channel, ChannelPick::Sub(SUB_MAX_CORNER_HZ));
                assert_eq!(cfg.dac_content_highpass_hz, None);
            },
        );
    }

    #[test]
    fn main_highpass_is_ignored_for_sub_channels() {
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The round-trip lane still names the retired route, so under the
                // one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("sub")),
                ("JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ", Some("90")),
                ("JASPER_OUTPUTD_DAC_CONTENT_HP_HZ", Some("90")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.dac_content_channel, ChannelPick::Sub(90.0));
                assert_eq!(cfg.dac_content_highpass_hz, None);
            },
        );
    }

    #[test]
    fn sub_corner_is_ignored_for_non_sub_channels() {
        // The corner env only matters for "sub"; setting it on another
        // channel must not change the pick.
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The round-trip lane still names the retired route, so under the
                // one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                ("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("left")),
                ("JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ", Some("120")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.dac_content_channel, ChannelPick::Left);
                assert_eq!(cfg.dac_content_highpass_hz, None);
            },
        );
    }

    #[test]
    fn rejects_unknown_dac_content_channel() {
        with_env(
            &[("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL", Some("both"))],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err
                    .to_string()
                    .contains("JASPER_OUTPUTD_DAC_CONTENT_CHANNEL"));
            },
        );
    }

    // The next two tests pin the ALLOWLIST intent: the guard rejects any
    // non-supported mode by NAMING the one required mode, so the contract
    // fails closed when a future sink / bridge variant lands.
    #[test]
    fn dac_content_lane_rejects_non_direct_bridge() {
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
            ],
            || {
                let err = Config::from_env().unwrap_err();
                // Allowlist phrasing: names the REQUIRED mode, not the rejected one.
                assert!(
                    err.to_string().contains("CONTENT_BRIDGE=direct"),
                    "guard should name the required mode, got: {err}"
                );
            },
        );
    }

    #[test]
    fn the_round_trip_lane_parks_under_the_one_transport() {
        // ADR-0178 `grouped_dac_content_lane` (#3118). The bonded member's lane
        // pins the retired route, and nothing serves that route any more — so a
        // box that arms the FIFO and declares nothing else lands in THIS
        // refusal, exit 78, rather than starting and playing silence.
        with_env(
            &[("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo"))],
            || {
                let err = Config::from_env()
                    .expect_err("the round-trip lane has no transport and must park")
                    .to_string();
                assert!(err.contains("JASPER_OUTPUTD_DAC_CONTENT_FIFO"), "{err}");
                assert!(err.contains("JASPER_OUTPUTD_CONTENT_BRIDGE"), "{err}");
            },
        );
    }

    #[test]
    fn dac_content_lane_rejects_non_single_alsa_sink() {
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
                // The lane's bridge requirement bites first otherwise; the sink
                // fence behind it is what this test pins.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
            ],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(
                    err.to_string().contains("SINK=single_alsa"),
                    "guard should name the required mode, got: {err}"
                );
            },
        );
    }

    #[test]
    fn active_sink_is_a_legit_bonded_member_without_dac_content() {
        // distributed-active Slice 3 ("lift the dac_content fence for the active
        // follower sink"): an ACTIVE (composite/multi-driver) sink IS a legitimate
        // bonded member — but via CamillaDSP re-entry (Option B), NOT outputd's
        // dac_content lane. The active follower's reconciler clears the dac_content
        // env (camilla owns the channel-pick + the 2->N split), so outputd just
        // runs its normal active sink. The dac_content+single_alsa fence above is
        // KEPT (it still correctly guards the DUMB-follower dac_content lane) — it
        // simply never fires on the active-follower path because no FIFO is set.
        // This pins that the active-sink-while-bondable shape parses: the active
        // speaker is no longer categorically barred from a bond.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
                // The ACTIVE-ring endpoint marker pair: without it a composite
                // is the passive shape's #2982 park, which is a different
                // test's subject.
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
                (
                    "JASPER_OUTPUTD_SHM_RING_PATH",
                    Some(DEFAULT_ACTIVE_SHM_RING_PATH),
                ),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.sink_mode, SinkMode::Composite);
                assert!(cfg.dac_content_fifo.is_none());
            },
        );
    }

    #[test]
    fn active_lane_rejects_post_crossover_tts_mixer_even_at_two_channels() {
        // distributed-active Stage B belt-and-suspenders (the recorded latent
        // guard hazard, HANDOFF-distributed-active.md): an active 2-way speaker
        // (woofer/tweeter) is ALSO a 2-channel single-ALSA sink, so the bare
        // `content_channels == 2` check would WRONGLY permit the post-crossover
        // outputd TTS mixer on it — sending full-range speech to the tweeter.
        // With JASPER_OUTPUTD_ACTIVE_LANE=1 the TTS mixer must fail closed, and
        // the error names the full-range-stereo invariant + the active-lane var.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_TTS_SOCKET", Some("/run/x.sock")),
                // An unarmed active lane is refused by the ring predicate first;
                // this test is about the TTS guard behind it.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_TTS_SOCKET"), "{err}");
                assert!(err.contains("JASPER_OUTPUTD_ACTIVE_LANE unset"), "{err}");
                assert!(err.contains("full-range stereo L/R sink"), "{err}");
            },
        );
    }

    #[test]
    fn a_wide_content_wire_is_accepted_on_the_default_path() {
        // S32 is what the reconciler emits for the ring wire by default, so the
        // undeclared path — the one transport — has to take it.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                ("JASPER_OUTPUTD_CONTENT_FORMAT", Some("S32_LE")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_format, SampleFormat::S32Le);
                assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::ShmRing);
            },
        );
    }

    #[test]
    fn active_lane_rejects_a_content_bridge_even_at_two_channels() {
        // Same invariant on the sibling stereo-only feature: a content bridge
        // must refuse to arm on an active-crossover lane, where the full-range
        // program it carries would be split to the tweeter. Exercised through
        // `shm_ring` — the surviving non-direct bridge — so the gate keeps a
        // live witness now that `rate_match` fail-safes to `direct` and could
        // no longer reach it.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("CONTENT_BRIDGE=shm_ring requires"), "{err}");
                assert!(err.contains("JASPER_OUTPUTD_ACTIVE_LANE unset"), "{err}");
            },
        );
    }

    #[test]
    fn passive_stereo_sink_still_arms_the_outputd_tts_mixer() {
        // No dumb-follower / bonded-member regression: a PASSIVE full-range
        // stereo L/R sink (active_lane unset) with a TTS socket must STILL parse
        // — the ordinary bonded-member outputd TTS mixer keeps working. The
        // active-lane guard narrows ONLY the active case; it must not break the
        // existing 2-channel mixer the leader case is built on.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                (
                    "JASPER_OUTPUTD_TTS_SOCKET",
                    Some("/run/jasper-outputd/tts.sock"),
                ),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert!(!cfg.active_lane);
                assert_eq!(cfg.content_channels, 2);
                assert_eq!(
                    cfg.tts_socket_path.as_deref(),
                    Some("/run/jasper-outputd/tts.sock")
                );
            },
        );
    }

    #[test]
    fn active_lane_rejects_dac_content_round_trip_lane() {
        // The third stereo-L/R-only feature (the dumb dac_content ChannelPick
        // round-trip lane) must also fail closed on an active-crossover lane:
        // an active 2-way sink is 2-channel, so the bare ACTIVE_CHANNELS==2
        // check would otherwise permit the lane and pick a full-range channel
        // straight to the tweeter. (Structurally the reconciler never sets both
        // on one box; this is the belt-and-suspenders config-level backstop.)
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The lane's own bridge requirement bites first otherwise, and
                // that refusal is a different test's subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_DAC_CONTENT_FIFO"), "{err}");
                assert!(err.contains("JASPER_OUTPUTD_ACTIVE_LANE unset"), "{err}");
            },
        );
    }

    /// Env for an ARMED active-ring endpoint, the shape the reconciler writes.
    fn armed_active_ring_env() -> Vec<(&'static str, Option<&'static str>)> {
        vec![
            ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
            ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
            ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
            ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
            ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
            (
                "JASPER_OUTPUTD_SHM_RING_PATH",
                Some(DEFAULT_ACTIVE_SHM_RING_PATH),
            ),
        ]
    }

    #[test]
    fn an_armed_active_ring_endpoint_is_accepted_at_two_channels() {
        // The R7b happy path, and the one a width test cannot distinguish from a
        // full-range stereo sink: jts3's active lane is 2 channels. What admits
        // it is the MARKER, never the width.
        with_env(&armed_active_ring_env(), || {
            let cfg = Config::from_env().unwrap();
            assert!(cfg.active_lane);
            assert!(cfg.ring_active_endpoint);
            assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::ShmRing);
            assert_eq!(
                cfg.shm_ring.as_ref().unwrap().path,
                DEFAULT_ACTIVE_SHM_RING_PATH
            );
        });
    }

    #[test]
    fn the_active_ring_path_is_refused_without_the_endpoint_marker() {
        // SAFETY-B1, the deepest find, pinned. A transient output_topology.json
        // read failure clears ACTIVE_LANE (and the marker), content_channels
        // falls back to 2, and `is_full_range_stereo_lr_sink` goes TRUE — so
        // WITHOUT the allowlist outputd would attach the ACTIVE ring as an
        // ordinary stereo sink and unlock its post-crossover TTS mixer onto a
        // compression driver. There is no content-PCM open left to fail loudly
        // on a mis-declaration, so this bail is the only thing standing there.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
                (
                    "JASPER_OUTPUTD_SHM_RING_PATH",
                    Some(DEFAULT_ACTIVE_SHM_RING_PATH),
                ),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("active ring path"), "{err}");
                assert!(err.contains("armed active endpoint"), "{err}");
            },
        );
    }

    #[test]
    fn an_armed_active_endpoint_is_refused_on_the_stereo_ring_path() {
        // The allowlist's OTHER direction. Positive equality both ways, so a
        // crossed pair fails whichever way it is crossed.
        let mut env = armed_active_ring_env();
        env.retain(|(k, _)| *k != "JASPER_OUTPUTD_SHM_RING_PATH");
        env.push(("JASPER_OUTPUTD_SHM_RING_PATH", Some(DEFAULT_SHM_RING_PATH)));
        with_env(&env, || {
            let err = Config::from_env().unwrap_err().to_string();
            assert!(err.contains("may read ONLY that path"), "{err}");
        });
    }

    #[test]
    fn the_endpoint_marker_without_the_active_lane_is_a_writer_fault() {
        // MODE-INDEPENDENT: an incoherent pair means the reconciler is broken,
        // and that is true under every content bridge. Checked under BOTH so the
        // bail cannot quietly acquire a bridge-mode term (which would let a
        // direct-bridge box start with a half-written pair).
        for bridge in ["direct", "shm_ring"] {
            with_env(
                &[
                    ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                    ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                    ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
                    ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some(bridge)),
                ],
                || {
                    let err = Config::from_env().unwrap_err().to_string();
                    assert!(err.contains("incoherent pair"), "bridge={bridge}: {err}");
                },
            );
        }
    }

    #[test]
    fn a_roleful_box_declaring_no_ring_parses_past_the_allowlist() {
        // Why the biconditional is SCOPED to ShmRing. A roleful box carrying a
        // persisted `direct` has no ring, so `is_active_path` is false while
        // `ring_active_ok` stays true — an UNSCOPED biconditional would refuse
        // it here, naming the ring-path allowlist for a box whose actual fault
        // is that it declared no transport. It parses past this guard and parks
        // at the first period instead, where the account is correct.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::Direct);
                assert!(cfg.shm_ring.is_none());
                assert!(cfg.ring_active_endpoint);
            },
        );
    }

    #[test]
    fn an_armed_active_ring_still_refuses_the_post_crossover_tts_mixer() {
        // The extra door opens the CONTENT BRIDGE and nothing else. The TTS gate
        // keeps the UNTOUCHED `is_full_range_stereo_lr_sink` predicate, because
        // active-mode voice rides fan-in upstream of the crossover — mixing
        // speech in post-crossover would send full-range audio to the tweeter.
        let mut env = armed_active_ring_env();
        env.push(("JASPER_OUTPUTD_TTS_SOCKET", Some("/run/x.sock")));
        with_env(&env, || {
            let err = Config::from_env().unwrap_err().to_string();
            assert!(err.contains("JASPER_OUTPUTD_TTS_SOCKET"), "{err}");
            assert!(err.contains("full-range stereo L/R sink"), "{err}");
        });
    }

    /// The composite env set that differs ONLY in the axis each test varies.
    fn composite_active_ring_env() -> Vec<(&'static str, Option<&'static str>)> {
        vec![
            ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
            ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
            ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
            ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
            ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
            ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
            (
                "JASPER_OUTPUTD_SHM_RING_PATH",
                Some(DEFAULT_ACTIVE_SHM_RING_PATH),
            ),
        ]
    }

    #[test]
    fn an_armed_composite_reads_the_active_ring_at_four_channels() {
        // P8b item 1: `ring_active_ok` admits Composite. It used to carry a
        // `sink_mode == SingleAlsa` term justified as "the ring ioplug is ONE
        // coherent device on ONE clock domain, which a multi-child composite is
        // not" — but the ring is the CamillaDSP -> outputd hop and the composite
        // split lives downstream of it, inside this daemon. This test is the
        // INVERSE of the one it replaces.
        with_env(&composite_active_ring_env(), || {
            let cfg = Config::from_env().expect("armed composite must accept the active ring");
            assert_eq!(cfg.sink_mode, SinkMode::Composite);
            assert_eq!(cfg.content_channels, 4, "the composite ring is 4-channel");
            assert_eq!(
                cfg.shm_ring.as_ref().map(|r| r.path.as_str()),
                Some(DEFAULT_ACTIVE_SHM_RING_PATH),
                "it must read the ACTIVE ring, never the stereo one"
            );
        });
    }

    #[test]
    fn an_unarmed_composite_still_never_reads_the_active_ring() {
        // The marker is not decoration: widening the sink term must not open the
        // door for a composite whose active lane was never armed. Same env as
        // above minus ACTIVE_LANE/RING_ACTIVE_ENDPOINT.
        //
        // WHICH guard catches it is worth stating exactly, because it is NOT
        // the ring-path allowlist. A composite without the endpoint marker IS
        // the passive shape, so the #2982 park refuses first — the allowlist
        // never gets the chance. Asserting the allowlist's wording here would
        // pin a guard that does not fire.
        let mut env = composite_active_ring_env();
        env.retain(|(k, _)| {
            *k != "JASPER_OUTPUTD_ACTIVE_LANE" && *k != "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT"
        });
        with_env(&env, || {
            let err = Config::from_env().unwrap_err().to_string();
            assert!(err.contains("PARKED"), "{err}");
            assert!(err.contains("#2982"), "{err}");
        });
    }

    #[test]
    fn an_armed_composite_still_refuses_the_stereo_only_features() {
        // THE BOUNDARY. Widening `ring_active_ok` must NOT widen
        // `is_full_range_stereo_lr_sink`: the outputd TTS mixer sits
        // post-crossover and would send full-range speech to a tweeter.
        let mut env = composite_active_ring_env();
        env.push(("JASPER_OUTPUTD_TTS_SOCKET", Some("/run/x.sock")));
        with_env(&env, || {
            let err = Config::from_env().unwrap_err().to_string();
            assert!(err.contains("JASPER_OUTPUTD_TTS_SOCKET"), "{err}");
            assert!(err.contains("full-range stereo L/R sink"), "{err}");
        });
    }

    #[test]
    fn a_stereo_ring_box_is_completely_unaffected() {
        // THE INERTNESS ROW. Every box in the fleet today has no marker, so the
        // allowlist's two sides are both false and it is satisfied trivially —
        // the stereo ring arms exactly as it did before R7b.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert!(!cfg.ring_active_endpoint);
                assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::ShmRing);
                assert_eq!(cfg.shm_ring.as_ref().unwrap().path, DEFAULT_SHM_RING_PATH);
            },
        );
    }

    #[test]
    fn systemd_alsa_backend_env_parses() {
        with_env(
            &[
                ("JASPER_OUTPUTD_BACKEND", Some("alsa")),
                (
                    "JASPER_OUTPUTD_CONTROL_SOCKET",
                    Some("/run/jasper-outputd/control.sock"),
                ),
                (
                    "JASPER_OUTPUTD_CHIP_REF_PCM",
                    Some("plughw:CARD=Array,DEV=0"),
                ),
                ("JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE", Some("16000")),
                ("JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES", Some("320")),
                ("JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES", Some("1280")),
                ("JASPER_OUTPUTD_CHIP_REF_OBSERVE", Some("1")),
                (
                    "JASPER_OUTPUTD_CHIP_REF_TEE_PATH",
                    Some("/tmp/outputd-chip-ref.s16le"),
                ),
                (
                    "JASPER_OUTPUTD_REFERENCE_UDP_TARGET",
                    Some("127.0.0.1:9891"),
                ),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.backend, BackendMode::Alsa);
                assert_eq!(
                    cfg.control_socket_path.as_deref(),
                    Some("/run/jasper-outputd/control.sock")
                );
                assert_eq!(cfg.chip_ref_pcm.as_deref(), Some("plughw:CARD=Array,DEV=0"));
                assert_eq!(cfg.chip_ref_sample_rate, 16_000);
                assert_eq!(cfg.chip_ref_period_frames, 320);
                assert_eq!(cfg.chip_ref_buffer_frames, 1280);
                assert!(cfg.chip_ref_observe);
                assert_eq!(
                    cfg.chip_ref_tee_path.as_deref(),
                    Some("/tmp/outputd-chip-ref.s16le")
                );
                assert_eq!(cfg.reference_udp_target.as_deref(), Some("127.0.0.1:9891"));
            },
        );
    }

    #[test]
    fn chip_ref_tee_requires_chip_ref_pcm() {
        with_env(
            &[
                ("JASPER_OUTPUTD_CHIP_REF_PCM", None),
                (
                    "JASPER_OUTPUTD_CHIP_REF_TEE_PATH",
                    Some("/tmp/outputd-chip-ref.s16le"),
                ),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_CHIP_REF_TEE_PATH"), "{err}");
                assert!(err.contains("JASPER_OUTPUTD_CHIP_REF_PCM"), "{err}");
            },
        );
    }

    #[test]
    fn rejects_buffers_smaller_than_two_periods() {
        with_env(
            &[
                ("JASPER_OUTPUTD_PERIOD_FRAMES", Some("1024")),
                ("JASPER_OUTPUTD_DAC_BUFFER_FRAMES", Some("1024")),
            ],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err.to_string().contains("JASPER_OUTPUTD_DAC_BUFFER_FRAMES"));
            },
        );
    }

    #[test]
    fn rejects_unknown_backend() {
        with_env(&[("JASPER_OUTPUTD_BACKEND", Some("pipewire"))], || {
            let err = Config::from_env().unwrap_err();
            assert!(err.to_string().contains("JASPER_OUTPUTD_BACKEND"));
        });
    }

    #[test]
    fn a_passive_composite_parks_and_a_roleful_one_does_not() {
        // ADR-0178: the passive dual-DAC shape has no transport under
        // one-audio-transport, and it is a NAMED PARK carrying its tracked
        // rebuild issue rather than a lane an operator can re-arm. Refusing at
        // PARSE time is what turns it into an immediate, correctly-labelled
        // park (exit 78) instead of a later sink-open failure.
        //
        // The discriminator is the ACTIVE-ring endpoint marker: a ROLEFUL
        // composite rides the ACTIVE ring and is the shape jts.local runs, so
        // the same sink mode must NOT park with the marker armed.
        let composite = [
            ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
            ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
            ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
        ];
        with_env(&composite, || {
            let err =
                Config::from_env().expect_err("a passive composite has no transport and must park");
            let msg = format!("{err:#}");
            assert!(msg.contains("#2982"), "{msg}");
        });

        let mut roleful = composite.to_vec();
        roleful.extend_from_slice(&[
            ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
            ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
            (
                "JASPER_OUTPUTD_SHM_RING_PATH",
                Some(DEFAULT_ACTIVE_SHM_RING_PATH),
            ),
        ]);
        with_env(&roleful, || {
            let cfg = Config::from_env()
                .expect("a roleful composite rides the ACTIVE ring and must not park");
            assert_eq!(cfg.sink_mode, SinkMode::Composite);
            assert!(cfg.ring_active_endpoint);
        });
    }

    #[test]
    fn parses_dual_apple_sink_contract() {
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
                // The ACTIVE-ring endpoint marker: without it this shape is
                // the passive composite's park (see the test above), and the
                // rest of the contract never gets to be checked.
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
                (
                    "JASPER_OUTPUTD_SHM_RING_PATH",
                    Some(DEFAULT_ACTIVE_SHM_RING_PATH),
                ),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.sink_mode, SinkMode::Composite);
                assert_eq!(cfg.content_channels, 4);
                assert_eq!(cfg.dac_pcm, "dual_apple_usb_c_dac_4ch");
                assert_eq!(cfg.dual_dac_a_pcm.as_deref(), Some("hw:CARD=A,DEV=0"));
                assert_eq!(cfg.dual_dac_b_pcm.as_deref(), Some("hw:CARD=B,DEV=0"));
                assert_eq!(
                    cfg.dual_max_delay_delta_frames,
                    DEFAULT_DUAL_MAX_DELAY_DELTA_FRAMES
                );
            },
        );
    }

    #[test]
    fn composite_sink_string_parses_to_same_shape_as_dual_apple_alias() {
        // The shape-named `composite` and the legacy `dual_apple` alias must
        // resolve to one identical sink shape (the wire migration cannot drift).
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("composite")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
                // The ACTIVE-ring endpoint marker pair: without it a composite
                // is the passive shape's #2982 park, which is a different
                // test's subject.
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
                (
                    "JASPER_OUTPUTD_SHM_RING_PATH",
                    Some(DEFAULT_ACTIVE_SHM_RING_PATH),
                ),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.sink_mode, SinkMode::Composite);
                // Wire value stays the stable `dual_apple` string through the
                // type rename (the documented lower-risk migration option).
                assert_eq!(cfg.sink_mode.as_str(), "dual_apple");
                assert_eq!(cfg.content_channels, 4);
            },
        );
    }

    #[test]
    fn declared_dac_format_defaults_to_s16_when_unset_or_blank() {
        // Unset == a box that predates the reconciler emit; explicit-empty ==
        // the reconciler's own value for an UNRECOGNIZED DAC (no profile to
        // query). Both resolve to the historical S16_LE edge.
        for value in [None, Some(""), Some("   ")] {
            with_env(&[("JASPER_OUTPUTD_DAC_FORMAT", value)], || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.declared_dac_format, SampleFormat::S16Le);
                assert_eq!(cfg.declared_dac_format.as_str(), "S16_LE");
            });
        }
    }

    #[test]
    fn declared_dac_format_parses_every_registry_value() {
        // Every value the registry can emit, including the `S24_3LE` the single
        // Apple dongle profile declares. The round trip through `as_str` is
        // asserted too: that string is the STATUS wire value and a chip-AEC
        // identity input, so a respelling silently invalidates commissioned
        // alignment artifacts.
        for (raw, want) in [
            ("S16_LE", SampleFormat::S16Le),
            ("S24_3LE", SampleFormat::S24_3Le),
            ("S32_LE", SampleFormat::S32Le),
        ] {
            with_env(&[("JASPER_OUTPUTD_DAC_FORMAT", Some(raw))], || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.declared_dac_format, want);
                assert_eq!(cfg.declared_dac_format.as_str(), raw);
            });
        }
    }

    #[test]
    fn declared_dac_format_rejects_anything_else_loudly() {
        // A value outside the registry vocabulary can only be registry /
        // reconciler drift; guessing an edge format would silently mis-declare
        // what the alignment identity is commissioned against. Case variants
        // are drift too — the only writer emits the uppercase registry literal.
        //
        // `S24_LE` stays in this list DELIBERATELY, and it is the sharpest vector
        // here now: it is the 4-byte-word 24-bit ALSA format, one character from
        // the accepted packed `S24_3LE`, and accepting it would ask ALSA for a
        // stride the packed staging is not built at. `S24_3BE` is the
        // wrong-endian packed twin, refused for the same reason.
        for bad in [
            "S24_LE", "S24_3BE", "s24_3le", "s32_le", "S32_BE", "float32",
        ] {
            with_env(&[("JASPER_OUTPUTD_DAC_FORMAT", Some(bad))], || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_DAC_FORMAT"), "{err}");
                assert!(err.contains(bad), "{err}");
                // The message has to enumerate what IS accepted, or an operator
                // reading a parked unit's journal cannot tell what to write.
                assert!(err.contains("S16_LE, S24_3LE, S32_LE"), "{err}");
            });
        }
    }

    #[test]
    fn content_format_defaults_to_s16_when_unset_or_blank() {
        // Unset/explicit-empty is the pre-flip fallback, not every live box:
        // jasper-audio-hardware-reconcile now emits a definitive value (S32_LE
        // or S16_LE, per coupling) on every reconciled box. This default is
        // what an unreconciled box (fresh install) or a failed-probe pass (key
        // left alone — event=audio_hardware_reconcile.content_format_skip)
        // resolves to. The test itself is still correct — the default really
        // is S16 — only the old "every box" framing was wrong.
        for value in [None, Some(""), Some("   ")] {
            with_env(&[("JASPER_OUTPUTD_CONTENT_FORMAT", value)], || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_format, SampleFormat::S16Le);
                assert_eq!(cfg.content_format.as_str(), "S16_LE");
            });
        }
    }

    #[test]
    fn content_format_parses_every_vocabulary_value() {
        // ONE vocabulary for both hops: this axis parses `S24_3LE` because the
        // enum does, not because a content hop can be read at that width.
        // Neither transport can — `alsa_backend`'s ingest refuses it
        // park-class, `shm_ring_source` refuses it for the same reason at the
        // same exit code — and no writer can emit it (`jasper.fanin_coupling`
        // answers only S16_LE or S32_LE). The parse and the refusals are
        // separate layers on purpose; an axis-asymmetric parser would be a
        // second, silently narrower spelling of the same enum.
        for (raw, want) in [
            ("S16_LE", SampleFormat::S16Le),
            ("S24_3LE", SampleFormat::S24_3Le),
            ("S32_LE", SampleFormat::S32Le),
        ] {
            with_env(&[("JASPER_OUTPUTD_CONTENT_FORMAT", Some(raw))], || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_format, want);
                assert_eq!(cfg.content_format.as_str(), raw);
            });
        }
    }

    #[test]
    fn content_format_rejects_anything_else_loudly() {
        // Same reasoning as the DAC axis: a value outside the vocabulary can
        // only be writer drift, and guessing a format would silently
        // mis-declare the ring wire outputd attaches to. Case variants are drift
        // too — the only writer emits the uppercase ALSA literal.
        for bad in [
            "S24_LE", "S24_3BE", "s24_3le", "s32_le", "S32_BE", "float32",
        ] {
            with_env(&[("JASPER_OUTPUTD_CONTENT_FORMAT", Some(bad))], || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_CONTENT_FORMAT"), "{err}");
                assert!(err.contains(bad), "{err}");
                assert!(err.contains("S16_LE, S24_3LE, S32_LE"), "{err}");
            });
        }
    }

    #[test]
    fn content_and_dac_formats_are_independent_axes() {
        // The two hops must never be read off each other: a wide content lane
        // into an S16-only hardware edge (the Apple-dongle build) and the
        // reverse are both legitimate, so each axis has to survive the other
        // being different.
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_FORMAT", Some("S32_LE")),
                ("JASPER_OUTPUTD_DAC_FORMAT", Some("S16_LE")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_format, SampleFormat::S32Le);
                assert_eq!(cfg.declared_dac_format, SampleFormat::S16Le);
            },
        );
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_FORMAT", Some("S16_LE")),
                ("JASPER_OUTPUTD_DAC_FORMAT", Some("S32_LE")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_format, SampleFormat::S16Le);
                assert_eq!(cfg.declared_dac_format, SampleFormat::S32Le);
            },
        );
    }

    #[test]
    fn single_sink_defaults_to_stereo_width_byte_identical() {
        with_env(&[], || {
            let cfg = Config::from_env().unwrap();
            assert_eq!(cfg.sink_mode, SinkMode::SingleAlsa);
            assert_eq!(cfg.content_channels, 2);
        });
    }

    #[test]
    fn single_sink_takes_active_channels_width() {
        // A wide coherent sink (DAC8x) is a ROLEFUL box, so it carries the
        // ACTIVE-ring endpoint marker pair — without it the ring predicate
        // turns it away before the width is worth asserting.
        with_env(
            &[
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("8")),
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
                (
                    "JASPER_OUTPUTD_SHM_RING_PATH",
                    Some(DEFAULT_ACTIVE_SHM_RING_PATH),
                ),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.sink_mode, SinkMode::SingleAlsa);
                assert_eq!(cfg.content_channels, 8);
            },
        );
    }

    #[test]
    fn rejects_active_channels_out_of_range() {
        for bad in ["1", "9", "0"] {
            with_env(&[("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some(bad))], || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_ACTIVE_CHANNELS"), "{err}");
            });
        }
    }

    #[test]
    fn composite_rejects_active_channels_other_than_four() {
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("composite")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("8")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("fixed at 4"), "{err}");
            },
        );
    }

    #[test]
    fn wide_single_rejects_stereo_only_features() {
        // The wide passthrough cannot host the stereo-only bridge / fifo / tts.
        with_env(
            &[
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("8")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("CONTENT_BRIDGE=shm_ring requires"), "{err}");
            },
        );
        with_env(
            &[
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("8")),
                ("JASPER_OUTPUTD_TTS_SOCKET", Some("/run/x.sock")),
                // The ring predicate refuses an unarmed wide sink first; this
                // sub-case is about the TTS guard behind it.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_TTS_SOCKET"), "{err}");
            },
        );
        with_env(
            &[
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("4")),
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                // The round-trip lane still names the retired route, so under the
                // one transport it PARKS (#3118). Pinned here so these knob
                // contracts keep their own subject.
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_DAC_CONTENT_FIFO"), "{err}");
            },
        );
    }

    #[test]
    fn a_composite_never_hosts_the_stereo_only_tts_mixer() {
        // Width alone excludes it, and it must stay excluded on the ROLEFUL
        // composite too — the one composite shape that still runs. (The passive
        // shape never reaches this guard: it parks first, which is its own
        // test's subject.)
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("composite")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", Some("1")),
                (
                    "JASPER_OUTPUTD_SHM_RING_PATH",
                    Some(DEFAULT_ACTIVE_SHM_RING_PATH),
                ),
                ("JASPER_OUTPUTD_TTS_SOCKET", Some("/run/x.sock")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_TTS_SOCKET requires"), "{err}");
            },
        );
    }

    #[test]
    fn dual_apple_sink_requires_both_child_pcms() {
        with_env(&[("JASPER_OUTPUTD_SINK", Some("dual_apple"))], || {
            let err = Config::from_env().unwrap_err();
            assert!(err.to_string().contains("JASPER_OUTPUTD_DUAL_DAC_A_PCM"));
        });
    }

    #[test]
    fn dual_apple_sink_rejects_identical_child_pcms() {
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=A,DEV=0")),
            ],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err.to_string().contains("requires distinct"));
            },
        );
    }

    #[test]
    fn dual_apple_sink_rejects_negative_delay_delta_budget() {
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_MAX_DELAY_DELTA_FRAMES", Some("-1")),
            ],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err
                    .to_string()
                    .contains("JASPER_OUTPUTD_DUAL_MAX_DELAY_DELTA_FRAMES"));
            },
        );
    }

    #[test]
    fn the_removed_rate_match_bridge_parks_on_every_spelling() {
        // Legacy-cleanup contract: a box migrating with a persisted
        // `rate_match` value in outputd.env PARKS, naming the key. It used to
        // fail SAFE to `direct` because that still carried audio; `direct`
        // carries nothing now, so the fail-safe would only relabel the park.
        //
        // Pinned by CLASS, not prose: a `Config::from_env` error is the exit-78
        // (EX_CONFIG) park by construction — `main` exits `EXIT_CONFIG` on it —
        // and the one wording assertion is that the message names the KEY an
        // operator has to edit, not how the sentence reads.
        let mut checked = 0usize;
        for &spelling in REMOVED_RATE_MATCH_BRIDGE_SPELLINGS {
            with_env(&[("JASPER_OUTPUTD_CONTENT_BRIDGE", Some(spelling))], || {
                let err = Config::from_env()
                    .expect_err("a retired bridge has no transport and must park");
                let msg = format!("{err:#}");
                assert!(
                    msg.contains("JASPER_OUTPUTD_CONTENT_BRIDGE"),
                    "spelling {spelling} must name the key: {msg}"
                );
                assert!(msg.contains(spelling), "spelling {spelling}: {msg}");
            });
            checked += 1;
        }
        // Non-vacuity, and the drift pin against the Python side: the four
        // spellings here are the same four
        // tests/test_audio_hardware_reconcile.py parametrizes its
        // reconciler-no-longer-narrows test over. Shrinking this list silently
        // would leave a migrating box's spelling untested on one of the two
        // sides.
        assert_eq!(
            checked, 4,
            "all four removed spellings must be exercised, got {checked}"
        );
    }

    #[test]
    fn the_removed_rate_match_tuning_knobs_are_inert() {
        // The three `_RING_FRAMES` / `_TARGET_FRAMES` / `_MAX_ADJUST_PPM` keys
        // were deleted with the bridge. A stale — even unparseable — value must
        // be ignored, never re-read into a validation that no longer exists:
        // the park below is the BRIDGE's, and it must not become a parse error
        // about a knob nothing reads.
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
                (
                    "JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES",
                    Some("not-a-number"),
                ),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES", Some("1")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_MAX_ADJUST_PPM", Some("0")),
            ],
            || {
                let msg = format!(
                    "{:#}",
                    Config::from_env().expect_err("the retired bridge parks")
                );
                assert!(msg.contains("rate_match"), "{msg}");
                assert!(!msg.contains("RING_FRAMES"), "inert knob leaked: {msg}");
            },
        );
    }

    #[test]
    fn an_unknown_content_bridge_still_fails_loud() {
        // A typo is a hard bail too — the operator asked for something that
        // never existed. It takes the generic arm rather than the retired-bridge
        // one, and the advertised vocabulary names the ONE transport first.
        with_env(
            &[("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_matchh"))],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("shm_ring"), "{err}");
                assert!(err.contains("rate_matchh"), "must echo the value: {err}");
            },
        );
    }

    #[test]
    fn parses_tts_lane_and_defaults_off() {
        with_env(&[], || {
            let cfg = Config::from_env().unwrap();
            assert!(cfg.tts_socket_path.is_none()); // solo: fanin owns TTS
            assert_eq!(
                cfg.tts_max_pending_frames,
                crate::tts::DEFAULT_MAX_PENDING_FRAMES
            );
            assert_eq!(cfg.tts_program_duck_db, -25.0);
        });
        with_env(
            &[
                (
                    "JASPER_OUTPUTD_TTS_SOCKET",
                    Some("/run/jasper-outputd/tts.sock"),
                ),
                ("JASPER_OUTPUTD_TTS_MAX_PENDING_FRAMES", Some("48000")),
                ("JASPER_OUTPUTD_TTS_PROGRAM_DUCK_DB", Some("-18.5")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(
                    cfg.tts_socket_path.as_deref(),
                    Some("/run/jasper-outputd/tts.sock")
                );
                assert_eq!(cfg.tts_max_pending_frames, 48_000);
                assert_eq!(cfg.tts_program_duck_db, -18.5);
            },
        );
    }

    #[test]
    fn pair_trim_accepts_attenuation_rejects_boost_and_floor() {
        // Hearing safety: the trim can only attenuate (the LOUDER
        // speaker comes down); a boost or an absurd depth fails closed.
        with_env(
            &[("JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB", Some("-3.5"))],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.dac_content_trim_db, -3.5);
            },
        );
        with_env(&[], || {
            assert_eq!(Config::from_env().unwrap().dac_content_trim_db, 0.0);
        });
        with_env(
            &[("JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB", Some("2.0"))],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("must be <= 0"), "{err}");
            },
        );
        with_env(
            &[("JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB", Some("-30"))],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("-24 dB"), "{err}");
            },
        );
    }

    #[test]
    fn rejects_positive_program_duck() {
        // Hearing safety: a duck ATTENUATES; positive program gain is
        // never allowed, fail loud.
        with_env(
            &[("JASPER_OUTPUTD_TTS_PROGRAM_DUCK_DB", Some("3.0"))],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err.to_string().contains("must be <= 0"));
            },
        );
    }

    #[test]
    fn rejects_chip_ref_sample_rate_that_does_not_divide_core_rate() {
        with_env(
            &[("JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE", Some("22050"))],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err.to_string().contains("must divide"));
            },
        );
    }

    // ----- PROTOTYPE shm_ring (latency/ring-proto-shm) -----

    #[test]
    fn the_ring_is_the_undeclared_transport_and_direct_carries_none() {
        // ADR-0100: an unset content bridge IS the ring, with the ring settings
        // resolved. A persisted `direct` still parses — that is how the park
        // refusals get to name it — and it carries no ring.
        with_env(&[], || {
            let cfg = Config::from_env().unwrap();
            assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::ShmRing);
            assert_eq!(
                cfg.shm_ring.as_ref().map(|r| r.path.as_str()),
                Some(DEFAULT_SHM_RING_PATH)
            );
        });
        with_env(&[("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct"))], || {
            let cfg = Config::from_env().unwrap();
            assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::Direct);
            assert!(cfg.shm_ring.is_none());
        });
    }

    #[test]
    fn parses_shm_ring_with_defaults_and_overrides() {
        with_env(
            &[("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring"))],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::ShmRing);
                let ring = cfg.shm_ring.expect("shm_ring config present");
                assert_eq!(ring.path, DEFAULT_SHM_RING_PATH);
                assert_eq!(ring.n_slots, DEFAULT_SHM_RING_SLOTS);
            },
        );
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
                (
                    "JASPER_OUTPUTD_SHM_RING_PATH",
                    Some("/dev/shm/jts-ring/content.ring"),
                ),
                ("JASPER_OUTPUTD_SHM_RING_SLOTS", Some("3")),
            ],
            || {
                let ring = Config::from_env().unwrap().shm_ring.unwrap();
                assert_eq!(ring.path, "/dev/shm/jts-ring/content.ring");
                assert_eq!(ring.n_slots, 3);
            },
        );
    }

    #[test]
    fn shm_ring_accepts_aliases() {
        for alias in ["shm_ring", "shmring", "ring"] {
            with_env(&[("JASPER_OUTPUTD_CONTENT_BRIDGE", Some(alias))], || {
                assert_eq!(
                    Config::from_env().unwrap().content_bridge_mode,
                    ContentBridgeMode::ShmRing,
                    "alias {alias}"
                );
            });
        }
    }

    #[test]
    fn shm_ring_rejects_out_of_range_slots() {
        // 17 is one past the ceiling (16, so the ALSA playback buffer clears
        // CamillaDSP's target_level); 1 is below the floor; 0 trips the
        // generic env_u32 > 0 guard.
        for slots in ["1", "17", "0"] {
            with_env(
                &[
                    ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
                    ("JASPER_OUTPUTD_SHM_RING_SLOTS", Some(slots)),
                ],
                || {
                    let err = Config::from_env().unwrap_err().to_string();
                    // "0" trips the generic env_u32 > 0 guard; 1 and 17 trip the
                    // slot-range guard. Either way it must fail loud.
                    assert!(
                        err.contains("SHM_RING_SLOTS") || err.contains("must be > 0"),
                        "slots={slots}: {err}"
                    );
                },
            );
        }
    }

    #[test]
    fn shm_ring_accepts_deep_buffer_slot_counts() {
        // The counts that give camilla's playback buffer real depth
        // (>= target_level) must parse.
        for slots in ["4", "8", "12", "16"] {
            with_env(
                &[
                    ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
                    ("JASPER_OUTPUTD_SHM_RING_SLOTS", Some(slots)),
                ],
                || {
                    let ring = Config::from_env()
                        .unwrap_or_else(|e| panic!("slots={slots} should parse: {e}"))
                        .shm_ring
                        .expect("shm_ring config present");
                    assert_eq!(ring.n_slots, slots.parse::<u32>().unwrap(), "slots={slots}");
                },
            );
        }
    }

    #[test]
    fn shm_ring_requires_full_range_stereo_sink() {
        // On an active-crossover 2-channel lane the shared stereo-only predicate
        // must reject shm_ring by NAMING the required mode (allowlist phrasing),
        // exactly like the rate-match bridge — feeding the ring to a lane that
        // is split to the tweeter is unsafe.
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("shm_ring"), "{err}");
                assert!(err.contains("single_alsa"), "{err}");
            },
        );
    }

    #[test]
    fn shm_ring_is_mutually_exclusive_with_other_content_sources() {
        // dac_content_fifo requires CONTENT_BRIDGE=direct, so pairing it with
        // shm_ring fails loud (naming the required Direct mode).
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("shm_ring")),
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("CONTENT_BRIDGE=direct"), "{err}");
            },
        );
    }
}
