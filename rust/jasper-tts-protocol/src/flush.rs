// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! FLUSH_SYNC transport; ledger accounting stays with each playout owner.

use std::io::{BufReader, Write};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::Arc;
use std::time::Duration;

use jasper_daemon::json::json_string;

use crate::{QueuedTtsCommand, SegmentKind, SAMPLE_RATE};

const TTS_COMMAND_QUEUE_CAPACITY: usize = 128;
/// Two seconds of queued-but-unplayed audio, in stereo wire frames.
pub const DEFAULT_MAX_PENDING_FRAMES: u64 = SAMPLE_RATE as u64 * 2;
const FLUSH_ACK_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug)]
pub struct QueuedFlush {
    pub epoch: u64,
    pub ack: Option<SyncSender<FlushSummary>>,
}

#[derive(Debug, Clone)]
pub struct FlushEvent {
    pub segment: u64,
    pub kind: SegmentKind,
    pub provider_item_id: Option<String>,
    pub queued_frames: u64,
    pub written_frames: u64,
    pub drained_frames: u64,
    pub flushed_frames: u64,
}

#[derive(Debug, Clone)]
pub struct FlushSummary {
    pub requests: u64,
    pub pending_frames: u64,
    pub flushed_frames: u64,
    pub segments: usize,
    pub max_audio_played_ms: u64,
    // JSON formatting belongs on the socket thread, never fan-in's mixer (R-023).
    events: Vec<FlushEvent>,
}

impl FlushSummary {
    pub fn new(
        requests: u64,
        pending_frames: u64,
        flushed_frames: u64,
        max_audio_played_ms: u64,
        events: Vec<FlushEvent>,
    ) -> Self {
        Self {
            requests,
            pending_frames,
            flushed_frames,
            segments: events.len(),
            max_audio_played_ms,
            events,
        }
    }

    pub fn to_json_line(&self) -> String {
        format!(
            "{{\"ok\":true,\"requests\":{},\"pending_frames\":{},\"segments\":{},\"flushed_frames\":{},\"max_audio_played_ms\":{},\"events\":{}}}\n",
            self.requests,
            self.pending_frames,
            self.segments,
            self.flushed_frames,
            self.max_audio_played_ms,
            render_events_json(&self.events),
        )
    }
}

fn render_events_json(events: &[FlushEvent]) -> String {
    let mut json = String::from("[");
    for (i, event) in events.iter().enumerate() {
        if i > 0 {
            json.push(',');
        }
        let provider_item_id = match &event.provider_item_id {
            Some(id) => json_string(id),
            None => "null".to_string(),
        };
        json.push_str(&format!(
            "{{\"segment\":{},\"kind\":\"{}\",\"provider_item_id\":{},\"queued_frames\":{},\"written_frames\":{},\"drained_frames\":{},\"flushed_frames\":{}}}",
            event.segment,
            event.kind.as_str(),
            provider_item_id,
            event.queued_frames,
            event.written_frames,
            event.drained_frames,
            event.flushed_frames,
        ));
    }
    json.push(']');
    json
}

pub type TtsChannels = (
    SyncSender<QueuedTtsCommand>,
    Receiver<QueuedTtsCommand>,
    SyncSender<QueuedFlush>,
    Receiver<QueuedFlush>,
    Arc<AtomicU64>,
);

pub fn channels() -> TtsChannels {
    let (tx, rx) = mpsc::sync_channel(TTS_COMMAND_QUEUE_CAPACITY);
    let (flush_tx, flush_rx) = mpsc::sync_channel(TTS_COMMAND_QUEUE_CAPACITY);
    (tx, rx, flush_tx, flush_rx, Arc::new(AtomicU64::new(0)))
}

pub fn queue_flush(
    reader: &mut BufReader<UnixStream>,
    flush_tx: &SyncSender<QueuedFlush>,
    epoch: &AtomicU64,
) -> bool {
    let next_epoch = epoch.fetch_add(1, Ordering::SeqCst) + 1;
    let (ack_tx, ack_rx) = mpsc::sync_channel(1);
    if flush_tx
        .send(QueuedFlush {
            epoch: next_epoch,
            ack: Some(ack_tx),
        })
        .is_err()
    {
        return false;
    }
    let response = match ack_rx.recv_timeout(FLUSH_ACK_TIMEOUT) {
        Ok(summary) => summary.to_json_line(),
        Err(_) => "{\"ok\":false,\"error\":\"flush_ack_timeout\"}\n".to_string(),
    };
    reader.get_mut().write_all(response.as_bytes()).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{FLUSH_SYNC_ACK_EVENT_KEYS, FLUSH_SYNC_ACK_KEYS};
    use serde_json::{json, Value};
    use std::io::BufRead;
    use std::thread;

    #[test]
    fn flush_ack_preserves_keys_values_and_escaped_ids() {
        for id in [None, Some("cut-\"short\\\n雪")] {
            let summary = FlushSummary::new(
                7,
                12,
                9,
                23,
                vec![FlushEvent {
                    segment: 4,
                    kind: SegmentKind::Assistant,
                    provider_item_id: id.map(str::to_owned),
                    queued_frames: 12,
                    written_frames: 5,
                    drained_frames: 3,
                    flushed_frames: 9,
                }],
            );
            let line = summary.to_json_line();
            assert!(line.ends_with('\n'));
            assert_eq!(line.lines().count(), 1);
            let parsed: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(
                parsed,
                json!({
                    "ok": true, "requests": 7, "pending_frames": 12, "segments": 1,
                    "flushed_frames": 9, "max_audio_played_ms": 23,
                    "events": [{"segment": 4, "kind": "assistant", "provider_item_id": id,
                        "queued_frames": 12, "written_frames": 5, "drained_frames": 3,
                        "flushed_frames": 9}],
                })
            );
            for key in FLUSH_SYNC_ACK_KEYS {
                assert!(parsed.get(key).is_some());
            }
            for key in FLUSH_SYNC_ACK_EVENT_KEYS {
                assert!(parsed["events"][0].get(key).is_some());
            }
        }
        let empty = FlushSummary::new(1, 0, 0, 0, vec![]).to_json_line();
        assert_eq!(
            serde_json::from_str::<Value>(&empty).unwrap(),
            json!({
                "ok": true, "requests": 1, "pending_frames": 0, "segments": 0,
                "flushed_frames": 0, "max_audio_played_ms": 0, "events": [],
            })
        );
    }

    #[test]
    fn flush_request_bumps_epoch_before_acknowledging_on_the_socket() {
        let (_, _, flush_tx, flush_rx, epoch) = channels();
        let (server, client) = UnixStream::pair().unwrap();
        let server_epoch = Arc::clone(&epoch);
        let server = thread::spawn(move || {
            queue_flush(&mut BufReader::new(server), &flush_tx, &server_epoch)
        });
        let request = flush_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(request.epoch, 1);
        assert_eq!(epoch.load(Ordering::SeqCst), 1);
        request
            .ack
            .unwrap()
            .send(FlushSummary::new(1, 8, 8, 0, vec![]))
            .unwrap();
        let mut line = String::new();
        BufReader::new(client).read_line(&mut line).unwrap();
        assert!(server.join().unwrap());
        assert_eq!(
            serde_json::from_str::<Value>(&line).unwrap()["pending_frames"],
            8
        );
    }

    #[test]
    fn missing_ack_returns_the_same_wire_error_on_disconnect_or_timeout() {
        for disconnect in [true, false] {
            let (_, _, flush_tx, flush_rx, epoch) = channels();
            let (server, client) = UnixStream::pair().unwrap();
            let server =
                thread::spawn(move || queue_flush(&mut BufReader::new(server), &flush_tx, &epoch));
            let request = flush_rx.recv_timeout(Duration::from_secs(1)).unwrap();
            let ack = request.ack;
            let _held_ack = if disconnect {
                drop(ack);
                None
            } else {
                ack
            };
            let mut line = String::new();
            BufReader::new(client).read_line(&mut line).unwrap();
            assert!(server.join().unwrap());
            assert_eq!(line, "{\"ok\":false,\"error\":\"flush_ack_timeout\"}\n");
        }
    }
}
