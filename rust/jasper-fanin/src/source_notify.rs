// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! USB frame-flow edge detector and best-effort mux wake adapter.
//!
//! The helper samples an existing atomic counter at 20 Hz. It never runs on the
//! audio thread and never chooses a source: an edge publishes `direct.streaming`
//! and sends `NOTIFY usbsink` to mux, whose one reconciler re-probes reality and
//! owns all policy. Failed/lost notifications are expected; mux's patrol repairs
//! them from the published state.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use log::{info, warn};

pub const SAMPLE_INTERVAL: Duration = Duration::from_millis(50);
const STOP_AFTER_SAMPLES: u32 = 40; // 2 s at the fixed 20 Hz cadence.
const SOCKET_TIMEOUT: Duration = Duration::from_millis(200);

pub struct SourceNotifySignals {
    pub input_frames: Arc<AtomicU64>,
    pub streaming: Arc<AtomicBool>,
    pub stream_starts: Arc<AtomicU64>,
    pub stream_stops: Arc<AtomicU64>,
    pub notify_attempts: Arc<AtomicU64>,
    pub notify_failures: Arc<AtomicU64>,
}

#[derive(Debug, Default)]
struct FlowDetector {
    previous_frames: Option<u64>,
    idle_samples: u32,
    streaming: bool,
}

impl FlowDetector {
    /// Return `Some(new_streaming_state)` only on an edge.
    fn observe(&mut self, frames: u64) -> Option<bool> {
        let Some(previous) = self.previous_frames.replace(frames) else {
            return None;
        };
        if frames > previous {
            self.idle_samples = 0;
            if !self.streaming {
                self.streaming = true;
                return Some(true);
            }
            return None;
        }

        if !self.streaming {
            self.idle_samples = 0;
            return None;
        }
        self.idle_samples = self.idle_samples.saturating_add(1);
        if self.idle_samples >= STOP_AFTER_SAMPLES {
            self.streaming = false;
            self.idle_samples = 0;
            return Some(false);
        }
        None
    }
}

pub fn run(signals: SourceNotifySignals, mux_socket: &Path, shutdown: Arc<AtomicBool>) {
    let mut detector = FlowDetector::default();
    info!(
        "event=fanin.source_notify.ready sample_ms={} stop_ms={}",
        SAMPLE_INTERVAL.as_millis(),
        SAMPLE_INTERVAL.as_millis() * u128::from(STOP_AFTER_SAMPLES),
    );
    while !shutdown.load(Ordering::Relaxed) {
        let frames = signals.input_frames.load(Ordering::Relaxed);
        if let Some(streaming) = detector.observe(frames) {
            publish_edge(&signals, streaming, mux_socket);
        }
        std::thread::sleep(SAMPLE_INTERVAL);
    }
    info!("event=fanin.source_notify.stopped");
}

fn publish_edge(signals: &SourceNotifySignals, streaming: bool, mux_socket: &Path) {
    signals.streaming.store(streaming, Ordering::Relaxed);
    if streaming {
        signals.stream_starts.fetch_add(1, Ordering::Relaxed);
    } else {
        signals.stream_stops.fetch_add(1, Ordering::Relaxed);
    }
    signals.notify_attempts.fetch_add(1, Ordering::Relaxed);
    let delivered = notify_mux(mux_socket);
    if !delivered {
        signals.notify_failures.fetch_add(1, Ordering::Relaxed);
        warn!(
            "event=fanin.source_notify.edge source=usbsink streaming={} delivered=false socket={}",
            streaming,
            mux_socket.display(),
        );
    } else {
        info!(
            "event=fanin.source_notify.edge source=usbsink streaming={} delivered=true",
            streaming,
        );
    }
}

fn notify_mux(socket: &Path) -> bool {
    let Ok(mut stream) = UnixStream::connect(socket) else {
        return false;
    };
    if stream.set_read_timeout(Some(SOCKET_TIMEOUT)).is_err()
        || stream.set_write_timeout(Some(SOCKET_TIMEOUT)).is_err()
        || stream.write_all(b"NOTIFY usbsink\n").is_err()
    {
        return false;
    }
    let mut response = String::new();
    if BufReader::new(stream).read_line(&mut response).is_err() {
        return false;
    }
    serde_json::from_str::<serde_json::Value>(&response)
        .ok()
        .and_then(|value| {
            value
                .get("accepted")
                .and_then(|accepted| accepted.as_bool())
        })
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;

    fn test_signals() -> SourceNotifySignals {
        SourceNotifySignals {
            input_frames: Arc::new(AtomicU64::new(0)),
            streaming: Arc::new(AtomicBool::new(false)),
            stream_starts: Arc::new(AtomicU64::new(0)),
            stream_stops: Arc::new(AtomicU64::new(0)),
            notify_attempts: Arc::new(AtomicU64::new(0)),
            notify_failures: Arc::new(AtomicU64::new(0)),
        }
    }

    fn unique_socket(label: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "jasper-fanin-notify-{}-{}-{}.sock",
            label,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        ))
    }

    fn ack_server(path: &Path, accepted: bool) -> std::thread::JoinHandle<String> {
        let listener = UnixListener::bind(path).unwrap();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut request)
                .unwrap();
            let response = format!("{{\"accepted\":{accepted}}}\n");
            stream.write_all(response.as_bytes()).unwrap();
            request
        })
    }

    #[test]
    fn flow_detector_starts_on_first_advance() {
        let mut detector = FlowDetector::default();
        assert_eq!(detector.observe(10), None);
        assert_eq!(detector.observe(11), Some(true));
        assert_eq!(detector.observe(12), None);
    }

    #[test]
    fn flow_detector_stops_only_after_full_hysteresis() {
        let mut detector = FlowDetector::default();
        assert_eq!(detector.observe(10), None);
        assert_eq!(detector.observe(11), Some(true));
        for _ in 0..(STOP_AFTER_SAMPLES - 1) {
            assert_eq!(detector.observe(11), None);
        }
        assert_eq!(detector.observe(11), Some(false));
    }

    #[test]
    fn resumed_frames_cancel_pending_stop() {
        let mut detector = FlowDetector::default();
        detector.observe(10);
        assert_eq!(detector.observe(11), Some(true));
        for _ in 0..(STOP_AFTER_SAMPLES - 1) {
            detector.observe(11);
        }
        assert_eq!(detector.observe(12), None);
        for _ in 0..(STOP_AFTER_SAMPLES - 1) {
            assert_eq!(detector.observe(12), None);
        }
        assert_eq!(detector.observe(12), Some(false));
    }

    #[test]
    fn publish_edge_sends_mux_contract_and_updates_success_counters() {
        let path = unique_socket("accepted");
        let server = ack_server(&path, true);
        let signals = test_signals();

        publish_edge(&signals, true, &path);

        assert_eq!(server.join().unwrap(), "NOTIFY usbsink\n");
        assert!(signals.streaming.load(Ordering::Relaxed));
        assert_eq!(signals.stream_starts.load(Ordering::Relaxed), 1);
        assert_eq!(signals.stream_stops.load(Ordering::Relaxed), 0);
        assert_eq!(signals.notify_attempts.load(Ordering::Relaxed), 1);
        assert_eq!(signals.notify_failures.load(Ordering::Relaxed), 0);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn notify_mux_rejects_negative_acknowledgement() {
        let path = unique_socket("rejected");
        let server = ack_server(&path, false);

        assert!(!notify_mux(&path));

        assert_eq!(server.join().unwrap(), "NOTIFY usbsink\n");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn publish_edge_counts_delivery_failure_without_losing_state() {
        let path = unique_socket("missing");
        let signals = test_signals();

        publish_edge(&signals, true, &path);

        assert!(signals.streaming.load(Ordering::Relaxed));
        assert_eq!(signals.stream_starts.load(Ordering::Relaxed), 1);
        assert_eq!(signals.notify_attempts.load(Ordering::Relaxed), 1);
        assert_eq!(signals.notify_failures.load(Ordering::Relaxed), 1);
    }
}
