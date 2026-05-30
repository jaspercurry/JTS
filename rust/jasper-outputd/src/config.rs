//! Configuration for the outputd daemon.
//!
//! Defaults keep `jasper-outputd --once` safe in a developer shell:
//! fake backend, no sockets unless the caller sets them. The systemd
//! unit opts into the real ALSA backend and runtime sockets with
//! explicit `JASPER_OUTPUTD_*` environment lines.

use anyhow::{Context, Result};

use crate::types::SAMPLE_RATE;

pub const DEFAULT_PERIOD_FRAMES: u32 = 1024;
pub const DEFAULT_CONTENT_BUFFER_FRAMES: u32 = 4096;
pub const DEFAULT_DAC_BUFFER_FRAMES: u32 = 3072;
pub const DEFAULT_CHIP_REF_BUFFER_FRAMES: u32 = 4096;
pub const DEFAULT_STREAM_ID: u64 = 1;

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

#[derive(Debug, Clone)]
pub struct Config {
    pub backend: BackendMode,
    pub content_pcm: String,
    pub dac_pcm: String,
    pub sample_rate: u32,
    pub period_frames: u32,
    pub content_buffer_frames: u32,
    pub dac_buffer_frames: u32,
    pub chip_ref_pcm: Option<String>,
    pub chip_ref_buffer_frames: u32,
    pub reference_udp_target: Option<String>,
    pub stream_id: u64,
    pub tts_socket_path: Option<String>,
    pub control_socket_path: Option<String>,
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

        let period_frames = env_u32("JASPER_OUTPUTD_PERIOD_FRAMES", DEFAULT_PERIOD_FRAMES)?;
        let content_buffer_frames = env_u32(
            "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES",
            DEFAULT_CONTENT_BUFFER_FRAMES,
        )?;
        let dac_buffer_frames = env_u32(
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
            DEFAULT_DAC_BUFFER_FRAMES,
        )?;
        let chip_ref_buffer_frames = env_u32(
            "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES",
            DEFAULT_CHIP_REF_BUFFER_FRAMES,
        )?;
        validate_buffer(
            "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES",
            content_buffer_frames,
            period_frames,
        )?;
        validate_buffer(
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
            dac_buffer_frames,
            period_frames,
        )?;
        validate_buffer(
            "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES",
            chip_ref_buffer_frames,
            period_frames,
        )?;

        Ok(Self {
            backend,
            content_pcm: env_str("JASPER_OUTPUTD_CONTENT_PCM", "outputd_content_capture"),
            dac_pcm: env_str("JASPER_OUTPUTD_DAC_PCM", "outputd_dac"),
            sample_rate,
            period_frames,
            content_buffer_frames,
            dac_buffer_frames,
            chip_ref_pcm: env_optional("JASPER_OUTPUTD_CHIP_REF_PCM"),
            chip_ref_buffer_frames,
            reference_udp_target: env_optional("JASPER_OUTPUTD_REFERENCE_UDP_TARGET"),
            stream_id: env_u64("JASPER_OUTPUTD_STREAM_ID", DEFAULT_STREAM_ID)?,
            tts_socket_path: env_optional("JASPER_OUTPUTD_TTS_SOCKET"),
            control_socket_path: env_optional("JASPER_OUTPUTD_CONTROL_SOCKET"),
        })
    }
}

fn validate_buffer(name: &str, buffer_frames: u32, period_frames: u32) -> Result<()> {
    let min_buffer_frames = period_frames.saturating_mul(2);
    if buffer_frames < min_buffer_frames {
        anyhow::bail!(
            "{}={} must be >= 2 x JASPER_OUTPUTD_PERIOD_FRAMES={} (minimum ALSA jitter margin)",
            name,
            buffer_frames,
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

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn with_env<F: FnOnce()>(vars: &[(&str, Option<&str>)], f: F) {
        let _guard = ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let snapshot: Vec<(String, String)> = std::env::vars()
            .filter(|(k, _)| k.starts_with("JASPER_OUTPUTD_"))
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
            assert_eq!(cfg.content_pcm, "outputd_content_capture");
            assert_eq!(cfg.dac_pcm, "outputd_dac");
            assert_eq!(cfg.sample_rate, SAMPLE_RATE);
            assert_eq!(cfg.period_frames, DEFAULT_PERIOD_FRAMES);
            assert_eq!(cfg.content_buffer_frames, DEFAULT_CONTENT_BUFFER_FRAMES);
            assert_eq!(cfg.dac_buffer_frames, DEFAULT_DAC_BUFFER_FRAMES);
            assert!(cfg.chip_ref_pcm.is_none());
            assert!(cfg.reference_udp_target.is_none());
            assert!(cfg.tts_socket_path.is_none());
            assert!(cfg.control_socket_path.is_none());
        });
    }

    #[test]
    fn systemd_alsa_backend_env_parses() {
        with_env(
            &[
                ("JASPER_OUTPUTD_BACKEND", Some("alsa")),
                (
                    "JASPER_OUTPUTD_TTS_SOCKET",
                    Some("/run/jasper-outputd/tts.sock"),
                ),
                (
                    "JASPER_OUTPUTD_CONTROL_SOCKET",
                    Some("/run/jasper-outputd/control.sock"),
                ),
                ("JASPER_OUTPUTD_CHIP_REF_PCM", Some("plughw:CARD=Array,DEV=0")),
                ("JASPER_OUTPUTD_REFERENCE_UDP_TARGET", Some("127.0.0.1:9891")),
            ],
            || {
                let cfg = Config::from_env().unwrap();
                assert_eq!(cfg.backend, BackendMode::Alsa);
                assert_eq!(
                    cfg.tts_socket_path.as_deref(),
                    Some("/run/jasper-outputd/tts.sock")
                );
                assert_eq!(
                    cfg.control_socket_path.as_deref(),
                    Some("/run/jasper-outputd/control.sock")
                );
                assert_eq!(
                    cfg.chip_ref_pcm.as_deref(),
                    Some("plughw:CARD=Array,DEV=0")
                );
                assert_eq!(
                    cfg.reference_udp_target.as_deref(),
                    Some("127.0.0.1:9891")
                );
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
}
