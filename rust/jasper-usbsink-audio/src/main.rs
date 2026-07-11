// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

#![cfg_attr(not(feature = "alsa-runtime"), allow(dead_code, unused_imports))]

//! Rust USB audio bridge — STANDBY-ONLY liveness + state publisher.
//!
//! Since the single-USB-pipeline convergence (2026-07-10) this daemon owns NO
//! audio path. jasper-fanin DIRECT-captures `hw:UAC2Gadget` itself (the "combo"
//! topology); this binary exists only to carry the household's USB-audio INTENT
//! (`jasper-usbsink.service` enabled/disabled — read by the coupling reconciler
//! and the `/sources/` wizard) and to satisfy the `Type=notify` +
//! `WatchdogSec=15s` liveness contract while publishing a small `state.json` the
//! doctor / `/state` read.
//!
//! What it does, and ONLY this:
//! - open NO PCM (fan-in owns the gadget capture)
//! - send `READY=1` once and pat the systemd watchdog on a 1 s cadence
//! - derive `host_connected` best-effort from `/sys/class/udc/*/state`
//! - publish `state.json` (`standby:true`, idle counters) atomically
//!
//! The old aloop "solo" capture/delivery path (a private capture → 2-3 period
//! ring → `usbsink_substream` playback, plus the `:8781` preempt/tap HTTP
//! listener and the solo host-slaved pitch ladder) was deleted when the combo
//! direct-capture path became the sole USB pipeline. See
//! docs/HANDOFF-usbsink.md and docs/HANDOFF-usb-low-latency.md.

use std::env;
use std::fs;
use std::mem::MaybeUninit;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
#[cfg(feature = "alsa-runtime")]
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
#[cfg(feature = "alsa-runtime")]
use log::{info, warn};
#[cfg(feature = "alsa-runtime")]
use sd_notify::NotifyState;
#[cfg(feature = "alsa-runtime")]
use signal_hook::consts::signal::{SIGINT, SIGTERM};
#[cfg(feature = "alsa-runtime")]
use signal_hook::flag;

const DEFAULT_CAPTURE_DEVICE: &str = "hw:UAC2Gadget";
const DEFAULT_STATE_PATH: &str = "/run/jasper-usbsink/state.json";
const SAMPLE_RATE: u32 = 48_000;
const CHANNELS: u32 = 2;
const DEFAULT_PERIOD_FRAMES: u32 = 256;
const DEFAULT_RING_PERIODS: usize = 3;
const MIN_RING_PERIODS: usize = 2;
const MAX_RING_PERIODS: usize = 3;
const STATE_INTERVAL: Duration = Duration::from_millis(1000);
const WATCHDOG_INTERVAL: Duration = Duration::from_millis(5000);
// The -60 dBFS "playing" gate. The standby bridge no longer computes `playing`
// (fan-in's DIRECT lane owns it — see jasper.source_state.USBSINK_PLAYING_RMS_DBFS),
// so this constant is no longer applied here; it is retained as the Rust-side
// anchor for the cross-language drift pin in
// tests/test_usbsink_playing_rms_contract.py, which greps this exact line.
#[allow(dead_code)]
const PLAYING_RMS_DBFS: f64 = -60.0;
static STATE_WRITE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug)]
struct Config {
    capture_device: String,
    sample_rate: u32,
    channels: u32,
    period_frames: u32,
    ring_periods: usize,
    state_path: PathBuf,
}

impl Config {
    fn from_env() -> Result<Self> {
        let cfg = Self {
            capture_device: env_string("JASPER_USBSINK_CAPTURE_DEVICE", DEFAULT_CAPTURE_DEVICE),
            sample_rate: env_u32("JASPER_USBSINK_SAMPLE_RATE", SAMPLE_RATE)?,
            channels: env_u32("JASPER_USBSINK_CHANNELS", CHANNELS)?,
            period_frames: env_u32("JASPER_USBSINK_BLOCK_FRAMES", DEFAULT_PERIOD_FRAMES)?,
            ring_periods: env_usize("JASPER_USBSINK_RING_PERIODS", DEFAULT_RING_PERIODS)?,
            state_path: PathBuf::from(env_string("JASPER_USBSINK_STATE_PATH", DEFAULT_STATE_PATH)),
        };
        cfg.validate()?;
        Ok(cfg)
    }

    fn validate(&self) -> Result<()> {
        validate_audio_config(
            self.sample_rate,
            self.channels,
            self.period_frames,
            self.ring_periods,
        )
    }
}

// Route-geometry validation is retained even though this daemon opens no PCM:
// the fields are the DESCRIPTIVE route geometry echoed into state.json (read by
// the doctor / `/state`), and validating them still catches a mis-set
// JASPER_USBSINK_* env early rather than silently publishing a nonsense route.
fn validate_audio_config(
    sample_rate: u32,
    channels: u32,
    period_frames: u32,
    ring_periods: usize,
) -> Result<()> {
    if sample_rate != SAMPLE_RATE {
        bail!("JASPER_USBSINK_SAMPLE_RATE must be 48000 for usb_low_latency_48k");
    }
    if channels != CHANNELS {
        bail!("JASPER_USBSINK_CHANNELS must be stereo (2)");
    }
    if period_frames == 0 || period_frames % 2 != 0 {
        bail!("JASPER_USBSINK_BLOCK_FRAMES must be a positive period-aligned even frame count");
    }
    if !(MIN_RING_PERIODS..=MAX_RING_PERIODS).contains(&ring_periods) {
        bail!("JASPER_USBSINK_RING_PERIODS must be 2 or 3");
    }
    Ok(())
}

fn env_string(key: &str, default: &str) -> String {
    env::var(key)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_u32(key: &str, default: u32) -> Result<u32> {
    match env::var(key) {
        Ok(value) if !value.trim().is_empty() => value
            .trim()
            .parse::<u32>()
            .with_context(|| format!("parsing {key}")),
        _ => Ok(default),
    }
}

fn env_usize(key: &str, default: usize) -> Result<usize> {
    match env::var(key) {
        Ok(value) if !value.trim().is_empty() => value
            .trim()
            .parse::<usize>()
            .with_context(|| format!("parsing {key}")),
        _ => Ok(default),
    }
}

/// Cross-thread observable state published into `state.json`.
///
/// In standby-only operation the audio-path fields (`playing`, `rms_dbfs`, ring
/// fill, and every counter) are never written — they stay at their idle defaults
/// exactly as a combo box has always published them, because the audio loop that
/// once populated them is gone. `host_connected` (derived from sysfs) and
/// `last_progress_epoch_ms` (the watchdog sentinel) are the only fields written
/// at runtime. The idle fields are retained so the `state.json` shape the doctor
/// and `/state` read is unchanged.
#[derive(Default)]
struct SharedState {
    preempted: AtomicBool,
    playing: AtomicBool,
    host_connected: AtomicBool,
    rms_dbfs_x100: AtomicI32,
    ring_fill_periods: AtomicUsize,
    ring_capacity_periods: AtomicUsize,
    capture_xruns: AtomicU64,
    capture_partial_reads: AtomicU64,
    playback_xruns: AtomicU64,
    underflow_periods: AtomicU64,
    overflow_events: AtomicU64,
    dropped_periods: AtomicU64,
    preempt_silence_periods: AtomicU64,
    preempt_dropped_periods: AtomicU64,
    capture_frames: AtomicU64,
    playback_frames: AtomicU64,
    last_progress_epoch_ms: AtomicU64,
}

impl SharedState {
    fn new(ring_capacity_periods: usize) -> Self {
        let state = Self::default();
        state
            .ring_capacity_periods
            .store(ring_capacity_periods, Ordering::Relaxed);
        state.rms_dbfs_x100.store(
            (jasper_resampler::RMS_DBFS_FLOOR * 100.0) as i32,
            Ordering::Relaxed,
        );
        state
    }

    fn mark_progress(&self) {
        self.last_progress_epoch_ms
            .store(epoch_millis(), Ordering::Relaxed);
    }
}

/// True iff any USB device controller reports `configured` — the host has
/// enumerated the gadget. Best-effort: any read failure reads as not-connected.
/// This is the only live signal a standby daemon derives, since it opens no PCM.
fn udc_state_configured() -> bool {
    let dir = match fs::read_dir("/sys/class/udc") {
        Ok(d) => d,
        Err(_) => return false,
    };
    for entry in dir.flatten() {
        let state_path = entry.path().join("state");
        if let Ok(raw) = fs::read_to_string(&state_path) {
            if raw.trim() == "configured" {
                return true;
            }
        }
    }
    false
}

/// The STANDBY liveness loop — the daemon's ONLY runtime shape now.
///
/// It opens NO PCM (fan-in owns `hw:UAC2Gadget`), but MUST satisfy the same
/// `Type=notify` + `WatchdogSec=15s` liveness contract the old audio loop did: it
/// sends `READY=1` once (systemd blocks unit start until this arrives), and drives
/// `state.mark_progress()` on a cadence < the watchdog interval so the
/// `start_watchdog` thread keeps patting `WATCHDOG=1` — without this the unit
/// would kill-loop. The state publisher and watchdog threads run alongside.
#[cfg(feature = "alsa-runtime")]
fn run_standby_loop(state: &Arc<SharedState>, shutdown: &Arc<AtomicBool>) -> Result<()> {
    info!("event=usbsink_audio.standby active=true (fan-in owns hw:UAC2Gadget; no PCM opened)");
    // host_connected is published best-effort from sysfs by the state publisher;
    // here we only own liveness. Send READY immediately so systemd unblocks.
    let _ = sd_notify::notify(&[NotifyState::Ready]);
    state.mark_progress();
    // Pat cadence well under WatchdogSec (15 s): 1 s matches the state-write
    // rhythm and keeps the sentinel fresh with wide margin.
    while !shutdown.load(Ordering::Relaxed) {
        state.mark_progress();
        thread::sleep(STATE_INTERVAL);
    }
    let _ = sd_notify::notify(&[NotifyState::Stopping]);
    Ok(())
}

/// The state-publisher thread. Refreshes `host_connected` from sysfs and writes
/// `state.json` on a 1 s cadence; a transient `/run` (tmpfs) write failure is
/// logged and retried on the next tick rather than tearing the thread down (the
/// pre-loop and shutdown writes keep their `?` — those are one-shot).
#[cfg(feature = "alsa-runtime")]
fn run_state_publisher(
    config: Config,
    state: Arc<SharedState>,
    shutdown: Arc<AtomicBool>,
) -> Result<()> {
    while !shutdown.load(Ordering::Relaxed) {
        // Standby: the audio loop that would set host_connected never runs, so
        // derive it best-effort from sysfs on the state-write cadence
        // (`/sys/class/udc/*/state == "configured"`), false on any read failure.
        state
            .host_connected
            .store(udc_state_configured(), Ordering::Relaxed);
        if let Err(e) = write_state_json(&config, &state) {
            warn!("event=usbsink_audio.state_write_error detail={e}");
        }
        thread::sleep(STATE_INTERVAL);
    }
    // Final state write on shutdown so the last-published snapshot is coherent.
    state
        .host_connected
        .store(udc_state_configured(), Ordering::Relaxed);
    write_state_json(&config, &state)?;
    Ok(())
}

fn write_state_json(config: &Config, state: &SharedState) -> Result<()> {
    if let Some(parent) = config.state_path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    }
    let rms_dbfs = (state.rms_dbfs_x100.load(Ordering::Relaxed) as f64) / 100.0;
    let body = status_json(config, state, rms_dbfs);
    let tmp = unique_state_tmp_path(&config.state_path);
    fs::write(&tmp, body).with_context(|| format!("writing {}", tmp.display()))?;
    fs::rename(&tmp, &config.state_path).with_context(|| {
        format!(
            "renaming {} to {}",
            tmp.display(),
            config.state_path.display()
        )
    })
}

fn unique_state_tmp_path(path: &Path) -> PathBuf {
    let seq = STATE_WRITE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("state.json");
    path.with_file_name(format!(".{file_name}.{}.{}.tmp", std::process::id(), seq))
}

fn status_json(config: &Config, state: &SharedState, rms_dbfs: f64) -> String {
    // `standby` is hardcoded true: this daemon has no other shape. The
    // device/geometry fields describe the intended USB route (fan-in captures
    // `capture_device` directly); the audio-path fields (`playing`, `rms_dbfs`,
    // ring, counters) are idle defaults because no PCM is opened here.
    format!(
        concat!(
            "{{",
            "\"schema_version\":1,",
            "\"implementation\":\"rust\",",
            "\"standby\":true,",
            "\"updated_at\":\"{}\",",
            "\"playing\":{},",
            "\"preempted\":{},",
            "\"host_connected\":{},",
            "\"rms_dbfs\":{:.2},",
            "\"capture_device\":\"{}\",",
            "\"sample_rate\":{},",
            "\"channels\":{},",
            "\"period_frames\":{},",
            "\"ring\":{{\"fill_periods\":{},\"capacity_periods\":{}}},",
            "\"counters\":{{",
            "\"capture_xruns\":{},",
            "\"capture_partial_reads\":{},",
            "\"playback_xruns\":{},",
            "\"underflow_periods\":{},",
            "\"overflow_events\":{},",
            "\"dropped_periods\":{},",
            "\"preempt_silence_periods\":{},",
            "\"preempt_dropped_periods\":{},",
            "\"capture_frames\":{},",
            "\"playback_frames\":{}",
            "}},",
            "\"last_progress_epoch_ms\":{}",
            "}}\n"
        ),
        iso8601_now(),
        json_bool(state.playing.load(Ordering::Relaxed)),
        json_bool(state.preempted.load(Ordering::Relaxed)),
        json_bool(state.host_connected.load(Ordering::Relaxed)),
        rms_dbfs,
        json_escape(&config.capture_device),
        config.sample_rate,
        config.channels,
        config.period_frames,
        state.ring_fill_periods.load(Ordering::Relaxed),
        state.ring_capacity_periods.load(Ordering::Relaxed),
        state.capture_xruns.load(Ordering::Relaxed),
        state.capture_partial_reads.load(Ordering::Relaxed),
        state.playback_xruns.load(Ordering::Relaxed),
        state.underflow_periods.load(Ordering::Relaxed),
        state.overflow_events.load(Ordering::Relaxed),
        state.dropped_periods.load(Ordering::Relaxed),
        state.preempt_silence_periods.load(Ordering::Relaxed),
        state.preempt_dropped_periods.load(Ordering::Relaxed),
        state.capture_frames.load(Ordering::Relaxed),
        state.playback_frames.load(Ordering::Relaxed),
        state.last_progress_epoch_ms.load(Ordering::Relaxed),
    )
}

fn json_bool(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn json_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c.is_control() => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn epoch_millis() -> u64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => duration.as_millis() as u64,
        Err(_) => 0,
    }
}

fn iso8601_now() -> String {
    let duration = match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(value) => value,
        Err(_) => return "1970-01-01T00:00:00.000Z".to_string(),
    };
    let secs = duration.as_secs() as libc::time_t;
    let millis = duration.subsec_millis();
    let mut tm = MaybeUninit::<libc::tm>::uninit();
    let tm_ptr = unsafe { libc::gmtime_r(&secs, tm.as_mut_ptr()) };
    if tm_ptr.is_null() {
        return format!("{}Z", epoch_millis());
    }
    let tm = unsafe { tm.assume_init() };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        millis,
    )
}

#[cfg(feature = "alsa-runtime")]
fn start_watchdog(state: Arc<SharedState>, shutdown: Arc<AtomicBool>) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        while !shutdown.load(Ordering::Relaxed) {
            let last = state.last_progress_epoch_ms.load(Ordering::Relaxed);
            let age = epoch_millis().saturating_sub(last);
            if last > 0 && age < 10_000 {
                let _ = sd_notify::notify(&[NotifyState::Watchdog]);
            }
            thread::sleep(WATCHDOG_INTERVAL);
        }
    })
}

#[cfg(feature = "alsa-runtime")]
fn install_signal_handlers(shutdown: &Arc<AtomicBool>) -> Result<()> {
    flag::register(SIGTERM, Arc::clone(shutdown)).context("registering SIGTERM")?;
    flag::register(SIGINT, Arc::clone(shutdown)).context("registering SIGINT")?;
    Ok(())
}

#[cfg(feature = "alsa-runtime")]
fn main() -> Result<()> {
    env_logger::init();
    let config = Config::from_env().context("loading jasper-usbsink-audio config")?;
    let shutdown = Arc::new(AtomicBool::new(false));
    install_signal_handlers(&shutdown)?;
    let shared = Arc::new(SharedState::new(config.ring_periods));
    // Pre-write state.json (with an initial sysfs-derived host_connected) so the
    // file exists before READY=1 — a consumer that reads immediately after start
    // never sees a missing file.
    shared
        .host_connected
        .store(udc_state_configured(), Ordering::Relaxed);
    write_state_json(&config, &shared)?;

    let state_thread = {
        let cfg = config.clone();
        let state = Arc::clone(&shared);
        let stop = Arc::clone(&shutdown);
        thread::spawn(move || run_state_publisher(cfg, state, stop))
    };
    let watchdog_thread = start_watchdog(Arc::clone(&shared), Arc::clone(&shutdown));

    let result = run_standby_loop(&shared, &shutdown);
    shutdown.store(true, Ordering::Relaxed);

    join_result_thread(state_thread, "state publisher")?;
    let _ = watchdog_thread.join();
    result
}

#[cfg(feature = "alsa-runtime")]
fn join_result_thread(handle: thread::JoinHandle<Result<()>>, label: &str) -> Result<()> {
    match handle.join() {
        Ok(result) => result.with_context(|| format!("{label} failed")),
        Err(_) => bail!("{label} panicked"),
    }
}

#[cfg(not(feature = "alsa-runtime"))]
fn main() -> Result<()> {
    bail!("jasper-usbsink-audio was built without the alsa-runtime feature")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn playing_rms_gate_pins_minus_60_dbfs() {
        // The Rust-side anchor for the cross-language "playing" gate. The combo
        // path applies this in Python (jasper.source_state.USBSINK_PLAYING_RMS_DBFS);
        // tests/test_usbsink_playing_rms_contract.py pins the two equal. This
        // assertion documents the value from the Rust side.
        assert_eq!(PLAYING_RMS_DBFS, -60.0);
    }

    #[test]
    fn config_validation_rejects_non_48k_non_stereo_and_bad_ring() {
        assert!(validate_audio_config(44_100, 2, 256, 3).is_err());
        assert!(validate_audio_config(48_000, 1, 256, 3).is_err());
        assert!(validate_audio_config(48_000, 2, 0, 3).is_err());
        assert!(validate_audio_config(48_000, 2, 256, 4).is_err());
        assert!(validate_audio_config(48_000, 2, 256, 3).is_ok());
    }

    #[test]
    fn state_tmp_paths_are_unique_for_concurrent_publishers() {
        let path = PathBuf::from("/run/jasper-usbsink/state.json");

        let first = unique_state_tmp_path(&path);
        let second = unique_state_tmp_path(&path);

        assert_ne!(first, second);
        assert_eq!(first.parent(), path.parent());
        assert_eq!(second.parent(), path.parent());
        assert!(first
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with(".state.json.")));
    }

    /// A minimal Config fixture for the status_json shape tests.
    fn test_config() -> Config {
        Config {
            capture_device: "hw:UAC2Gadget".to_string(),
            sample_rate: SAMPLE_RATE,
            channels: CHANNELS,
            period_frames: DEFAULT_PERIOD_FRAMES,
            ring_periods: DEFAULT_RING_PERIODS,
            state_path: PathBuf::from(DEFAULT_STATE_PATH),
        }
    }

    #[test]
    fn status_json_is_always_standby_with_idle_route_health_fields() {
        let cfg = test_config();
        let state = SharedState::new(DEFAULT_RING_PERIODS);

        let body = status_json(&cfg, &state, -120.0);
        let parsed: serde_json::Value = serde_json::from_str(body.trim()).unwrap();

        // schema_version stays 1; the daemon is always standby (no other shape).
        assert_eq!(parsed["schema_version"].as_u64(), Some(1));
        assert_eq!(parsed["implementation"].as_str(), Some("rust"));
        assert_eq!(parsed["standby"].as_bool(), Some(true));
        // Idle audio-path defaults — a misdirected harness/probe run is diagnosable
        // from standby:true + the zeroed counters.
        assert_eq!(parsed["playing"].as_bool(), Some(false));
        assert!((parsed["rms_dbfs"].as_f64().unwrap() - (-120.0)).abs() < 1e-6);
        assert_eq!(parsed["ring"]["fill_periods"].as_u64(), Some(0));
        assert_eq!(parsed["counters"]["capture_frames"].as_u64(), Some(0));
        // The deleted subsystems' sub-objects are gone.
        assert!(parsed.get("tap").is_none());
        assert!(parsed.get("host_clock").is_none());
        assert!(parsed.get("playback_device").is_none());
    }

    #[test]
    fn status_json_host_connected_is_independent_of_rms_activity() {
        let cfg = test_config();
        let state = SharedState::new(DEFAULT_RING_PERIODS);
        state.host_connected.store(true, Ordering::Relaxed);
        state.playing.store(false, Ordering::Relaxed);

        let body = status_json(&cfg, &state, -120.0);

        assert!(body.contains("\"host_connected\":true"));
        assert!(body.contains("\"playing\":false"));
    }
}
