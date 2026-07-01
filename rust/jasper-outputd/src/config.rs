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

use crate::dac_content::{
    ChannelPick, SUB_DEFAULT_CORNER_HZ, SUB_MAX_CORNER_HZ, SUB_MIN_CORNER_HZ,
};
use crate::types::SAMPLE_RATE;

pub const DEFAULT_PERIOD_FRAMES: u32 = 1024;
pub const DEFAULT_CONTENT_BUFFER_FRAMES: u32 = 4096;
pub const DEFAULT_DAC_BUFFER_FRAMES: u32 = 3072;
pub const DEFAULT_CONTENT_BRIDGE_RING_FRAMES: u32 = 16_384;
pub const DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES: u32 = 4096;
pub const DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM: u32 = 500;
pub const DEFAULT_LOCAL_CONTENT_PIPE: &str = "/run/jasper-outputd/content.pipe";
pub const DEFAULT_LOCAL_CONTENT_PIPE_BYTES: u32 = 8192;
pub const MAX_LOCAL_CONTENT_PIPE_BYTES: u32 = 65_536;
pub const MAX_CONTENT_BRIDGE_RING_FRAMES: u32 = 262_144;
pub const MAX_CONTENT_BRIDGE_TARGET_FRAMES: u32 = 65_536;
pub const DEFAULT_CHIP_REF_SAMPLE_RATE: u32 = 16_000;
pub const DEFAULT_CHIP_REF_PERIOD_FRAMES: u32 = 320;
pub const DEFAULT_CHIP_REF_BUFFER_FRAMES: u32 = 1280;
pub const DEFAULT_STREAM_ID: u64 = 1;
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
    Direct,
    RateMatch,
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
            Self::RateMatch => "rate_match",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContentBridgeConfig {
    pub ring_frames: u32,
    pub target_fill_frames: u32,
    pub max_adjust_ppm: u32,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub backend: BackendMode,
    pub sink_mode: SinkMode,
    pub content_pcm: String,
    pub content_channels: u16,
    pub dac_pcm: String,
    pub dual_dac_a_pcm: Option<String>,
    pub dual_dac_b_pcm: Option<String>,
    pub dual_require_link: bool,
    pub dual_max_delay_delta_frames: i64,
    pub sample_rate: u32,
    pub period_frames: u32,
    pub content_buffer_frames: u32,
    pub dac_buffer_frames: u32,
    pub content_bridge_mode: ContentBridgeMode,
    pub content_bridge: ContentBridgeConfig,
    /// OPTIONAL local low-latency content pipe. When set, outputd reads
    /// CamillaDSP's post-DSP S32_LE stereo program from this FIFO at the DAC
    /// cadence instead of reading `content_pcm` from snd-aloop. This is the
    /// Camilla -> outputd half of the end-to-end transport-pipe topology.
    pub local_content_pipe: Option<String>,
    /// Requested kernel FIFO capacity for `local_content_pipe` (bytes). This
    /// is a latency budget, not a throughput cache: outputd reads once per DAC
    /// period and asks Linux to keep the Camilla->outputd pipe bounded.
    pub local_content_pipe_bytes: u32,
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
    pub stream_id: u64,
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
    /// Set by the reconciler on a 2-channel active-crossover sink — the one
    /// active case the bare `content_channels == 2` check cannot tell apart
    /// from a full-range stereo L/R sink (distributed-active Stage B). The real
    /// invariant for outputd's stereo-only features (the TTS mixer, the
    /// rate-match bridge, and the dac_content round-trip lane) is "full-range
    /// stereo L/R sink," NOT "exactly 2 channels": an active 2-way speaker
    /// (woofer/tweeter) is also 2-channel, so without this marker those
    /// features would WRONGLY arm on it — mixing / rate-matching / channel-
    /// picking post-crossover sends full-range audio to the tweeter (unsafe).
    /// Wider active sinks (composite, >2ch) are already excluded by their
    /// channel width, so the reconciler does NOT set this for them. Default
    /// false (solo/passive) is byte-identical to today.
    pub active_lane: bool,
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
        let content_buffer_frames = env_u32(
            "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES",
            DEFAULT_CONTENT_BUFFER_FRAMES,
        )?;
        let dac_buffer_frames = env_u32(
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
            DEFAULT_DAC_BUFFER_FRAMES,
        )?;
        let content_bridge_mode = match env_str("JASPER_OUTPUTD_CONTENT_BRIDGE", "direct")
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "direct" | "off" | "disabled" => ContentBridgeMode::Direct,
            "rate_match" | "ratematch" | "rate-matched" | "rate_matched" => {
                ContentBridgeMode::RateMatch
            }
            other => {
                anyhow::bail!(
                    "JASPER_OUTPUTD_CONTENT_BRIDGE must be one of direct, rate_match; got {:?}",
                    other
                )
            }
        };
        let content_bridge = match content_bridge_mode {
            ContentBridgeMode::Direct => default_content_bridge_config(),
            ContentBridgeMode::RateMatch => {
                let bridge = ContentBridgeConfig {
                    ring_frames: env_u32(
                        "JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES",
                        DEFAULT_CONTENT_BRIDGE_RING_FRAMES,
                    )?,
                    target_fill_frames: env_u32(
                        "JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES",
                        DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES,
                    )?,
                    max_adjust_ppm: env_u32(
                        "JASPER_OUTPUTD_CONTENT_BRIDGE_MAX_ADJUST_PPM",
                        DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM,
                    )?,
                };
                validate_content_bridge(bridge, period_frames)?;
                bridge
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
            "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES",
            content_buffer_frames,
            period_frames,
            "JASPER_OUTPUTD_PERIOD_FRAMES",
        )?;
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

        let default_content_pcm = match sink_mode {
            SinkMode::SingleAlsa => "outputd_content_capture",
            SinkMode::Composite => "outputd_active_content_capture",
        };
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
        let local_content_pipe = env_optional("JASPER_OUTPUTD_LOCAL_CONTENT_PIPE");
        let local_content_pipe_bytes = env_u32(
            "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE_BYTES",
            DEFAULT_LOCAL_CONTENT_PIPE_BYTES,
        )?;
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

        // The shared safety predicate for outputd's stereo-only features: they
        // may arm ONLY on a full-range stereo L/R sink — single-ALSA, exactly
        // two channels, and NOT an active-crossover lane. Composite and
        // wide-active single sinks are excluded by width; a 2-channel active
        // sink is excluded by the explicit active_lane marker. Mixing or
        // rate-matching a stereo feed on any of those mis-sizes buffers on live
        // drivers, or (on an active lane) sends full-range audio to the
        // tweeter, so fail closed at startup.
        let is_full_range_stereo_lr_sink =
            sink_mode == SinkMode::SingleAlsa && content_channels == 2 && !active_lane;

        if content_bridge_mode != ContentBridgeMode::Direct && !is_full_range_stereo_lr_sink {
            anyhow::bail!(
                "JASPER_OUTPUTD_CONTENT_BRIDGE=rate_match requires a full-range stereo \
                 L/R sink: JASPER_OUTPUTD_SINK=single_alsa, JASPER_OUTPUTD_ACTIVE_CHANNELS=2, \
                 and JASPER_OUTPUTD_ACTIVE_LANE unset (the rate-match bridge is a stereo-only \
                 path; on an active-crossover lane it would rate-match full-range audio that \
                 is then split to the tweeter)"
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
        if local_content_pipe.is_some() {
            let min_pipe_bytes = period_frames
                .saturating_mul(u32::from(content_channels))
                .saturating_mul(std::mem::size_of::<i32>() as u32);
            if local_content_pipe_bytes < min_pipe_bytes {
                anyhow::bail!(
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE_BYTES={} must be >= one local-pipe period ({} bytes)",
                    local_content_pipe_bytes,
                    min_pipe_bytes
                );
            }
            if local_content_pipe_bytes > MAX_LOCAL_CONTENT_PIPE_BYTES {
                anyhow::bail!(
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE_BYTES={} must be <= {} (the local pipe is a low-latency transport, not a staging buffer)",
                    local_content_pipe_bytes,
                    MAX_LOCAL_CONTENT_PIPE_BYTES
                );
            }
            if content_bridge_mode != ContentBridgeMode::Direct {
                anyhow::bail!(
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE requires \
                     JASPER_OUTPUTD_CONTENT_BRIDGE=direct (the local pipe is \
                     already the content source; do not insert another \
                     content-source policy)"
                );
            }
            if dac_content_fifo.is_some() {
                anyhow::bail!(
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE and \
                     JASPER_OUTPUTD_DAC_CONTENT_FIFO are mutually exclusive \
                     content sources"
                );
            }
            if tts_socket_path.is_some() {
                anyhow::bail!(
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE is incompatible with \
                     JASPER_OUTPUTD_TTS_SOCKET; solo TTS must enter through \
                     fan-in so it stays in the single pre-Camilla program pipe"
                );
            }
            if !is_full_range_stereo_lr_sink {
                anyhow::bail!(
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE requires a full-range \
                     stereo L/R sink: JASPER_OUTPUTD_SINK=single_alsa, \
                     JASPER_OUTPUTD_ACTIVE_CHANNELS=2, and \
                     JASPER_OUTPUTD_ACTIVE_LANE unset"
                );
            }
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

        Ok(Self {
            backend,
            sink_mode,
            content_pcm: env_str("JASPER_OUTPUTD_CONTENT_PCM", default_content_pcm),
            content_channels,
            dac_pcm: env_str("JASPER_OUTPUTD_DAC_PCM", default_dac_pcm),
            dual_dac_a_pcm,
            dual_dac_b_pcm,
            dual_require_link: env_bool("JASPER_OUTPUTD_DUAL_REQUIRE_LINK", false),
            dual_max_delay_delta_frames,
            sample_rate,
            period_frames,
            content_buffer_frames,
            dac_buffer_frames,
            content_bridge_mode,
            content_bridge,
            local_content_pipe,
            local_content_pipe_bytes,
            chip_ref_pcm: env_optional("JASPER_OUTPUTD_CHIP_REF_PCM"),
            chip_ref_sample_rate,
            chip_ref_period_frames,
            chip_ref_buffer_frames,
            chip_ref_observe: env_bool("JASPER_OUTPUTD_CHIP_REF_OBSERVE", false),
            chip_ref_tee_path: env_optional("JASPER_OUTPUTD_CHIP_REF_TEE_PATH"),
            reference_udp_target: env_optional("JASPER_OUTPUTD_REFERENCE_UDP_TARGET"),
            stream_id: env_u64("JASPER_OUTPUTD_STREAM_ID", DEFAULT_STREAM_ID)?,
            control_socket_path: env_optional("JASPER_OUTPUTD_CONTROL_SOCKET"),
            dac_content_fifo,
            dac_content_channel,
            dac_content_highpass_hz,
            dac_content_trim_db,
            tts_socket_path,
            tts_max_pending_frames,
            tts_program_duck_db,
            active_lane,
        })
    }
}

fn default_content_bridge_config() -> ContentBridgeConfig {
    ContentBridgeConfig {
        ring_frames: DEFAULT_CONTENT_BRIDGE_RING_FRAMES,
        target_fill_frames: DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES,
        max_adjust_ppm: DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM,
    }
}

fn validate_content_bridge(config: ContentBridgeConfig, period_frames: u32) -> Result<()> {
    if config.target_fill_frames < period_frames.saturating_mul(2) {
        anyhow::bail!(
            "JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES={} must be >= 2 x JASPER_OUTPUTD_PERIOD_FRAMES={} (rate matcher startup headroom)",
            config.target_fill_frames,
            period_frames
        );
    }
    if config.target_fill_frames > MAX_CONTENT_BRIDGE_TARGET_FRAMES {
        anyhow::bail!(
            "JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES={} must be <= {}",
            config.target_fill_frames,
            MAX_CONTENT_BRIDGE_TARGET_FRAMES
        );
    }
    let min_ring_frames = config
        .target_fill_frames
        .saturating_add(period_frames.saturating_mul(4));
    if config.ring_frames < min_ring_frames {
        anyhow::bail!(
            "JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES={} must be >= target + 4 periods ({} frames)",
            config.ring_frames,
            min_ring_frames
        );
    }
    if config.ring_frames > MAX_CONTENT_BRIDGE_RING_FRAMES {
        anyhow::bail!(
            "JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES={} must be <= {}",
            config.ring_frames,
            MAX_CONTENT_BRIDGE_RING_FRAMES
        );
    }
    if config.max_adjust_ppm == 0 || config.max_adjust_ppm > 5000 {
        anyhow::bail!(
            "JASPER_OUTPUTD_CONTENT_BRIDGE_MAX_ADJUST_PPM={} must be between 1 and 5000",
            config.max_adjust_ppm
        );
    }
    Ok(())
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

fn env_str(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn env_optional(name: &str) -> Option<String> {
    match std::env::var(name) {
        Ok(value) if !value.trim().is_empty() => Some(value),
        _ => None,
    }
}

fn env_u32(name: &str, default: u32) -> Result<u32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => {
            let parsed = s
                .trim()
                .parse::<u32>()
                .with_context(|| format!("{} must be a positive integer; got {:?}", name, s))?;
            if parsed == 0 {
                anyhow::bail!("{} must be > 0", name);
            }
            Ok(parsed)
        }
        _ => Ok(default),
    }
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
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => s
            .trim()
            .parse::<u64>()
            .with_context(|| format!("{} must be a non-negative integer; got {:?}", name, s)),
        _ => Ok(default),
    }
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

fn env_f32(name: &str, default: f32) -> Result<f32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => {
            let parsed = s
                .trim()
                .parse::<f32>()
                .with_context(|| format!("{} must be a number; got {:?}", name, s))?;
            if !parsed.is_finite() {
                anyhow::bail!("{} must be finite", name);
            }
            Ok(parsed)
        }
        _ => Ok(default),
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
            assert_eq!(cfg.content_pcm, "outputd_content_capture");
            assert_eq!(cfg.content_channels, 2);
            assert_eq!(cfg.dac_pcm, "outputd_dac");
            assert!(cfg.dual_dac_a_pcm.is_none());
            assert!(cfg.dual_dac_b_pcm.is_none());
            assert_eq!(cfg.sample_rate, SAMPLE_RATE);
            assert_eq!(cfg.period_frames, DEFAULT_PERIOD_FRAMES);
            assert_eq!(cfg.content_buffer_frames, DEFAULT_CONTENT_BUFFER_FRAMES);
            assert_eq!(cfg.dac_buffer_frames, DEFAULT_DAC_BUFFER_FRAMES);
            assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::Direct);
            assert_eq!(
                cfg.content_bridge,
                ContentBridgeConfig {
                    ring_frames: DEFAULT_CONTENT_BRIDGE_RING_FRAMES,
                    target_fill_frames: DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES,
                    max_adjust_ppm: DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM,
                }
            );
            assert!(cfg.local_content_pipe.is_none());
            assert_eq!(
                cfg.local_content_pipe_bytes,
                DEFAULT_LOCAL_CONTENT_PIPE_BYTES
            );
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
    fn parses_local_content_pipe() {
        with_env(
            &[(
                "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE",
                Some("/run/jasper-outputd/content.pipe"),
            )],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(
                    cfg.local_content_pipe.as_deref(),
                    Some("/run/jasper-outputd/content.pipe")
                );
                assert_eq!(
                    cfg.local_content_pipe_bytes,
                    DEFAULT_LOCAL_CONTENT_PIPE_BYTES
                );
            },
        );
        with_env(
            &[
                (
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE",
                    Some("/run/jasper-outputd/content.pipe"),
                ),
                ("JASPER_OUTPUTD_LOCAL_CONTENT_PIPE_BYTES", Some("16384")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.local_content_pipe_bytes, 16_384);
            },
        );
    }

    #[test]
    fn local_content_pipe_rejects_unbounded_latency_budget() {
        with_env(
            &[
                (
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE",
                    Some("/run/jasper-outputd/content.pipe"),
                ),
                ("JASPER_OUTPUTD_PERIOD_FRAMES", Some("256")),
                ("JASPER_OUTPUTD_LOCAL_CONTENT_PIPE_BYTES", Some("512")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("one local-pipe period"), "{err}");
            },
        );
        with_env(
            &[
                (
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE",
                    Some("/run/jasper-outputd/content.pipe"),
                ),
                ("JASPER_OUTPUTD_LOCAL_CONTENT_PIPE_BYTES", Some("262144")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("low-latency transport"), "{err}");
            },
        );
    }

    #[test]
    fn local_content_pipe_rejects_other_content_sources() {
        with_env(
            &[
                (
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE",
                    Some("/run/jasper-outputd/content.pipe"),
                ),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_LOCAL_CONTENT_PIPE"), "{err}");
                assert!(
                    err.contains("JASPER_OUTPUTD_CONTENT_BRIDGE=direct"),
                    "{err}"
                );
            },
        );
        with_env(
            &[
                (
                    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE",
                    Some("/run/jasper-outputd/content.pipe"),
                ),
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("mutually exclusive"), "{err}");
            },
        );
    }

    #[test]
    fn main_highpass_uses_valid_corner_for_non_sub_channels() {
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
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
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
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
    fn dac_content_lane_rejects_non_single_alsa_sink() {
        with_env(
            &[
                ("JASPER_OUTPUTD_DAC_CONTENT_FIFO", Some("/run/x.fifo")),
                ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
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
                // No JASPER_OUTPUTD_DAC_CONTENT_FIFO — camilla owns the round-trip.
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
    fn active_lane_rejects_rate_match_bridge_even_at_two_channels() {
        // Same invariant on the sibling stereo-only feature: the rate-match
        // content bridge must also refuse to arm on an active-crossover lane.
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("single_alsa")),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("2")),
                ("JASPER_OUTPUTD_ACTIVE_LANE", Some("1")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("CONTENT_BRIDGE=rate_match requires"), "{err}");
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
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_DAC_CONTENT_FIFO"), "{err}");
                assert!(err.contains("JASPER_OUTPUTD_ACTIVE_LANE unset"), "{err}");
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
    fn parses_dual_apple_sink_contract() {
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("dual_apple")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.sink_mode, SinkMode::Composite);
                assert_eq!(cfg.content_pcm, "outputd_active_content_capture");
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
    fn single_sink_defaults_to_stereo_width_byte_identical() {
        with_env(&[], || {
            let cfg = Config::from_env().unwrap();
            assert_eq!(cfg.sink_mode, SinkMode::SingleAlsa);
            assert_eq!(cfg.content_channels, 2);
        });
    }

    #[test]
    fn single_sink_takes_active_channels_width() {
        with_env(&[("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("8"))], || {
            let cfg = Config::from_env().unwrap();
            assert_eq!(cfg.sink_mode, SinkMode::SingleAlsa);
            assert_eq!(cfg.content_channels, 8);
        });
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
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("CONTENT_BRIDGE=rate_match requires"), "{err}");
            },
        );
        with_env(
            &[
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", Some("8")),
                ("JASPER_OUTPUTD_TTS_SOCKET", Some("/run/x.sock")),
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
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("JASPER_OUTPUTD_DAC_CONTENT_FIFO"), "{err}");
            },
        );
    }

    #[test]
    fn composite_rejects_stereo_only_bridge_and_tts() {
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("composite")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
            ],
            || {
                let err = Config::from_env().unwrap_err().to_string();
                assert!(err.contains("CONTENT_BRIDGE=rate_match requires"), "{err}");
            },
        );
        with_env(
            &[
                ("JASPER_OUTPUTD_SINK", Some("composite")),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", Some("hw:CARD=A,DEV=0")),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", Some("hw:CARD=B,DEV=0")),
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
    fn parses_rate_match_content_bridge() {
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES", Some("12288")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES", Some("4096")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_MAX_ADJUST_PPM", Some("750")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::RateMatch);
                assert_eq!(cfg.content_bridge.ring_frames, 12_288);
                assert_eq!(cfg.content_bridge.target_fill_frames, 4096);
                assert_eq!(cfg.content_bridge.max_adjust_ppm, 750);
            },
        );
    }

    #[test]
    fn rejects_tiny_content_bridge_ring() {
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES", Some("4096")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES", Some("4096")),
            ],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err
                    .to_string()
                    .contains("JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES"));
            },
        );
    }

    #[test]
    fn direct_content_bridge_ignores_stale_invalid_bridge_tuning() {
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("direct")),
                (
                    "JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES",
                    Some("not-a-number"),
                ),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES", Some("1")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_MAX_ADJUST_PPM", Some("0")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.content_bridge_mode, ContentBridgeMode::Direct);
                assert_eq!(cfg.content_bridge, default_content_bridge_config());
            },
        );
    }

    #[test]
    fn rejects_huge_content_bridge_allocations() {
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES", Some("262145")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES", Some("4096")),
            ],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err
                    .to_string()
                    .contains("JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES"));
            },
        );
        with_env(
            &[
                ("JASPER_OUTPUTD_CONTENT_BRIDGE", Some("rate_match")),
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES", Some("65537")),
            ],
            || {
                let err = Config::from_env().unwrap_err();
                assert!(err
                    .to_string()
                    .contains("JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES"));
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
}
