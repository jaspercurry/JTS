//! Configuration for the outputd daemon.
//!
//! Defaults keep `jasper-outputd --once` safe in a developer shell:
//! fake backend, no sockets unless the caller sets them. The systemd
//! unit opts into the real ALSA backend and runtime sockets with
//! explicit `JASPER_OUTPUTD_*` environment lines.

use anyhow::{Context, Result};

use crate::dac_content::ChannelPick;
use crate::types::SAMPLE_RATE;

pub const DEFAULT_PERIOD_FRAMES: u32 = 1024;
pub const DEFAULT_CONTENT_BUFFER_FRAMES: u32 = 4096;
pub const DEFAULT_DAC_BUFFER_FRAMES: u32 = 3072;
pub const DEFAULT_CONTENT_BRIDGE_RING_FRAMES: u32 = 16_384;
pub const DEFAULT_CONTENT_BRIDGE_TARGET_FRAMES: u32 = 4096;
pub const DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM: u32 = 500;
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SinkMode {
    SingleAlsa,
    DualApple,
}

impl SinkMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SingleAlsa => "single_alsa",
            Self::DualApple => "dual_apple",
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
    pub chip_ref_pcm: Option<String>,
    pub chip_ref_sample_rate: u32,
    pub chip_ref_period_frames: u32,
    pub chip_ref_buffer_frames: u32,
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
            "dual_apple" | "dual_apple_usb_c_dac_4ch" => SinkMode::DualApple,
            other => {
                anyhow::bail!(
                    "JASPER_OUTPUTD_SINK must be one of single_alsa, dual_apple; got {:?}",
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
            SinkMode::DualApple => "outputd_active_content_capture",
        };
        let default_dac_pcm = match sink_mode {
            SinkMode::SingleAlsa => "outputd_dac",
            SinkMode::DualApple => "dual_apple_usb_c_dac_4ch",
        };
        let content_channels = match sink_mode {
            SinkMode::SingleAlsa => 2,
            SinkMode::DualApple => 4,
        };
        let dual_dac_a_pcm = env_optional("JASPER_OUTPUTD_DUAL_DAC_A_PCM");
        let dual_dac_b_pcm = env_optional("JASPER_OUTPUTD_DUAL_DAC_B_PCM");
        if sink_mode == SinkMode::DualApple
            && (dual_dac_a_pcm.is_none() || dual_dac_b_pcm.is_none())
        {
            anyhow::bail!(
                "JASPER_OUTPUTD_SINK=dual_apple requires JASPER_OUTPUTD_DUAL_DAC_A_PCM and JASPER_OUTPUTD_DUAL_DAC_B_PCM"
            );
        }
        if sink_mode == SinkMode::DualApple && dual_dac_a_pcm == dual_dac_b_pcm {
            anyhow::bail!(
                "JASPER_OUTPUTD_SINK=dual_apple requires distinct JASPER_OUTPUTD_DUAL_DAC_A_PCM and JASPER_OUTPUTD_DUAL_DAC_B_PCM"
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
        let dac_content_channel = ChannelPick::parse(&env_str(
            "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL",
            "stereo",
        ))
        .map_err(anyhow::Error::msg)?;
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
            chip_ref_pcm: env_optional("JASPER_OUTPUTD_CHIP_REF_PCM"),
            chip_ref_sample_rate,
            chip_ref_period_frames,
            chip_ref_buffer_frames,
            reference_udp_target: env_optional("JASPER_OUTPUTD_REFERENCE_UDP_TARGET"),
            stream_id: env_u64("JASPER_OUTPUTD_STREAM_ID", DEFAULT_STREAM_ID)?,
            control_socket_path: env_optional("JASPER_OUTPUTD_CONTROL_SOCKET"),
            dac_content_fifo,
            dac_content_channel,
            tts_socket_path,
            tts_max_pending_frames,
            tts_program_duck_db,
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
            assert_eq!(cfg.chip_ref_sample_rate, DEFAULT_CHIP_REF_SAMPLE_RATE);
            assert_eq!(cfg.chip_ref_period_frames, DEFAULT_CHIP_REF_PERIOD_FRAMES);
            assert_eq!(cfg.chip_ref_buffer_frames, DEFAULT_CHIP_REF_BUFFER_FRAMES);
            assert!(cfg.chip_ref_pcm.is_none());
            assert!(cfg.reference_udp_target.is_none());
            assert!(cfg.control_socket_path.is_none());
            // Multi-room round-trip lane is OFF by default (solo contract).
            assert!(cfg.dac_content_fifo.is_none());
            assert_eq!(cfg.dac_content_channel, ChannelPick::Stereo);
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
                assert_eq!(cfg.sink_mode, SinkMode::DualApple);
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
                ("JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES", Some("not-a-number")),
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
                (
                    "JASPER_OUTPUTD_CONTENT_BRIDGE_RING_FRAMES",
                    Some("262145"),
                ),
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
                (
                    "JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES",
                    Some("65537"),
                ),
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
                ("JASPER_OUTPUTD_TTS_SOCKET", Some("/run/jasper-outputd/tts.sock")),
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
