// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The daemon's one off-thread log writer, `fanin-ring-log`. A SCHED_FIFO
//! thread hands its log events here instead of formatting or writing them.

use std::sync::mpsc::{Receiver, SyncSender};
use std::sync::Arc;

use jasper_tts_protocol::loudness::{AssistantGainDecision, SegmentKind};
use log::{info, warn};

use crate::mixer::ring_output::{format_ring_stall_event, RingStallEvent};
use crate::tts::log_assistant_loudness_decision;

/// Bounded capacity of the `fanin-ring-log` channel (`RingOutput::stall_log`
/// and `TtsMixer`'s clone of the same sender). Sized for segment, flush and
/// starvation events alongside ring-stall edges during response bursts.
/// Overflow past this is drop-and-count, never a block (ADR-0254).
pub(crate) const FANIN_LOG_CHANNEL_CAPACITY: usize = 64;

/// Forward one event to an off-thread writer with `try_send`, calling
/// `note_dropped` instead of blocking the SCHED_FIFO work loop on the writer's
/// I/O. EVERY failure counts, `Disconnected` included: a writer thread that
/// returned early (its artifact would not open) leaves the gauge as the only
/// evidence, and a gauge reading 0 while 100% of events are lost is the wrong
/// answer. See ADR-0254.
///
/// `pub(crate)`: `tts.rs` sends [`FaninLogEvent`]s over the same shape from
/// the TTS mixer thread side (issue #4787).
pub(crate) fn send_drop_counted<T>(tx: &SyncSender<T>, event: T, note_dropped: impl FnOnce()) {
    if tx.try_send(event).is_err() {
        note_dropped();
    }
}

/// Everything this daemon's SCHED_FIFO mixer thread hands to `fanin-ring-log`
/// instead of formatting or writing itself (issue #4787). `RingOutput` and
/// `TtsMixer` share one sender and one writer thread.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum FaninLogEvent {
    RingStall(RingStallEvent),
    /// From `begin_segment_gain`, once per TTS segment start. `decision` is
    /// the SAME `Arc` `TtsMixer` keeps as `active_segment_decision` — sending
    /// it is an atomic refcount bump, not a clone of its `String` fields.
    AssistantLoudness {
        kind: SegmentKind,
        decision: Arc<AssistantGainDecision>,
    },
    /// From `drain_flushes`, once per batch of FLUSH_SYNC requests.
    TtsFlush {
        requests: usize,
        pending_frames: u64,
        flushed_frames: u64,
        segments: usize,
        max_audio_played_ms: u64,
    },
    TtsStarved {
        frames: u64,
        ms: u64,
        segment: u64,
        queued_frames_at_resume: u64,
    },
}

/// Drain `RingOutput::stall_log` off the SCHED_FIFO mixer thread: format and
/// log every [`FaninLogEvent`] the mixer or TTS thread hands off, so the
/// allocation in [`format_ring_stall_event`]/`log_assistant_loudness_decision`
/// and the synchronous write to journald's socket both happen here instead of
/// on the audio thread (issue #4787).
pub(crate) fn run_ring_stall_log_writer(receiver: Receiver<FaninLogEvent>) {
    for event in receiver {
        match event {
            FaninLogEvent::RingStall(
                stall @ (RingStallEvent::Detected { .. } | RingStallEvent::Unrecovered { .. }),
            ) => {
                warn!("{}", format_ring_stall_event(&stall));
            }
            FaninLogEvent::RingStall(stall @ RingStallEvent::Cleared { .. }) => {
                info!("{}", format_ring_stall_event(&stall));
            }
            FaninLogEvent::AssistantLoudness { kind, decision } => {
                log_assistant_loudness_decision(kind, &decision);
            }
            FaninLogEvent::TtsFlush {
                requests,
                pending_frames,
                flushed_frames,
                segments,
                max_audio_played_ms,
            } => {
                info!(
                    "event=fanin.tts_flush requests={} pending_frames={} flushed_frames={} segments={} max_audio_played_ms={}",
                    requests, pending_frames, flushed_frames, segments, max_audio_played_ms,
                );
            }
            FaninLogEvent::TtsStarved {
                frames,
                ms,
                segment,
                queued_frames_at_resume,
            } => {
                warn!(
                    "event=fanin.tts_starved frames={} ms={} segment={} queued_frames_at_resume={}",
                    frames, ms, segment, queued_frames_at_resume,
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::atomic::{AtomicU64, Ordering};

    use crate::impulse_tap::TapEvent;

    // ---- Bounded tap channel (must never block the SCHED_FIFO work loop) ----

    #[test]
    fn send_drop_counted_counts_a_full_channel_and_a_gone_writer_alike() {
        // Nothing draining: the bound-2 channel fills on the first two sends,
        // so the third must drop-and-count rather than block this (the
        // calling) thread. Then the receiver goes, which is what a writer
        // thread that could not open its artifact leaves behind — every event
        // is lost from there on, and the gauge is the only place that shows it.
        let (tx, rx) = std::sync::mpsc::sync_channel::<TapEvent>(2);
        let dropped = AtomicU64::new(0);
        let event = || TapEvent {
            monotonic_ns: 1,
            frame_index: 2,
            ring_fill_frames: 3,
            peak: 0.5,
        };
        let bump = || {
            dropped.fetch_add(1, Ordering::Relaxed);
        };
        send_drop_counted(&tx, event(), bump);
        send_drop_counted(&tx, event(), bump);
        send_drop_counted(&tx, event(), bump);
        assert_eq!(dropped.load(Ordering::Relaxed), 1);

        drop(rx);
        send_drop_counted(&tx, event(), bump);
        assert_eq!(dropped.load(Ordering::Relaxed), 2);
    }
}
