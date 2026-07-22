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
        std::thread::sleep(SAMPLE_INTERVAL);
    }
    info!("event=fanin.source_notify.stopped");
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
}
