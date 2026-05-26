//! Configuration loaded from `JASPER_FANIN_*` environment variables.
//!
//! Source of truth for defaults: `docs/HANDOFF-fan-in-daemon.md`
//! "Configuration" section. If you change a default here, update the
//! HANDOFF too — the doc is what operators read.
//!
//! All knobs have sensible defaults so a fresh deploy works without
//! any wizard interaction. Operator overrides go in
//! `/etc/jasper/jasper.env` (system-wide) or
//! `/var/lib/jasper/fanin.env` (wizard-owned, if a wizard is ever
//! added).

use anyhow::{Context, Result};

#[derive(Debug, Clone)]
pub struct Config {
    /// ALSA PCM name (or `hw:Card,Dev,Sub`) for the summed output.
    /// The daemon writes mixed audio here. CamillaDSP and the AEC
    /// bridge dsnoop on the corresponding capture side of this
    /// substream pair.
    pub output_pcm: String,

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
}

impl Config {
    /// Read JASPER_FANIN_* env vars, falling back to documented defaults.
    /// Returns `Err` only on structural misconfiguration (e.g., input
    /// PCM list length != renderer label list length).
    pub fn from_env() -> Result<Self> {
        let output_pcm = env_str("JASPER_FANIN_OUTPUT_PCM", "hw:Loopback,0,7");
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

        let sample_rate = env_u32("JASPER_FANIN_SAMPLE_RATE", 48_000)?;
        let period_frames = env_u32("JASPER_FANIN_PERIOD_FRAMES", 256)?;
        let input_buffer_frames = env_u32_fallback(
            "JASPER_FANIN_INPUT_BUFFER_FRAMES",
            "JASPER_FANIN_BUFFER_FRAMES",
            4096,
        )?;
        let output_buffer_frames =
            env_u32("JASPER_FANIN_OUTPUT_BUFFER_FRAMES", 3072)?;

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

        Ok(Self {
            output_pcm,
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
        })
    }
}

// ---- env var helpers ------------------------------------------------

fn env_str(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
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
            .with_context(|| {
                format!("{} must be a non-negative integer; got {:?}", name, s)
            }),
        _ => Ok(default),
    }
}

fn env_u32_fallback(name: &str, fallback_name: &str, default: u32) -> Result<u32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => s
            .trim()
            .parse::<u32>()
            .with_context(|| {
                format!("{} must be a non-negative integer; got {:?}", name, s)
            }),
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
    /// `JASPER_FANIN_*` env vars, clear them, apply this test's
    /// per-var overrides, run the closure, restore.
    fn with_env<F: FnOnce()>(vars: &[(&str, Option<&str>)], f: F) {
        let _guard = ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());

        let snapshot: Vec<(String, String)> = std::env::vars()
            .filter(|(k, _)| k.starts_with("JASPER_FANIN_"))
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
            ],
            || {
                let cfg = Config::from_env().expect("defaults must parse");
                assert_eq!(cfg.output_pcm, "hw:Loopback,0,7");
                assert_eq!(cfg.input_pcms.len(), 5);
                assert_eq!(cfg.input_renderers.len(), 5);
                assert_eq!(cfg.input_renderers[0], "spotify");
                assert_eq!(cfg.input_renderers[4], "correction");
                assert_eq!(cfg.sample_rate, 48_000);
                assert_eq!(cfg.period_frames, 256);
                assert_eq!(cfg.input_buffer_frames, 4096);
                assert_eq!(cfg.output_buffer_frames, 3072);
            },
        );
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
                let err = Config::from_env()
                    .expect_err("mismatched lengths must error");
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
                (
                    "JASPER_FANIN_INPUT_RENDERERS",
                    Some("test_a|test_b"),
                ),
            ],
            || {
                let cfg = Config::from_env()
                    .expect("pipe-delimited hw names must parse");
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
                let err = Config::from_env()
                    .expect_err("whitespace-only PCM list must error");
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
                let err = Config::from_env()
                    .expect_err("buffer < 2×period must error");
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
                let err = Config::from_env()
                    .expect_err("output buffer < 2×period must error");
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
                assert_eq!(cfg.output_buffer_frames, 3072);
            },
        );
    }

    #[test]
    fn bad_integer_env_var_returns_clear_error() {
        with_env(
            &[("JASPER_FANIN_SAMPLE_RATE", Some("not-a-number"))],
            || {
                let err = Config::from_env()
                    .expect_err("bad integer must error");
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
