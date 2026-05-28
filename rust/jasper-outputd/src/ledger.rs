//! Assistant/cue playout ledger.
//!
//! The ledger records what Python asked to play and what the output
//! clock actually wrote/drained. Provider-specific truncation can stay
//! in Python as long as it receives the core datum here:
//! `audio_played_ms`.

use crate::types::SegmentKind;

pub const DEFAULT_TERMINAL_SEGMENT_RETENTION: usize = 128;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct SegmentId(pub u64);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SegmentStatus {
    Queued,
    Playing,
    Drained,
    Flushed,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PlayoutSegment {
    pub local_segment_id: SegmentId,
    pub provider_item_id: Option<String>,
    pub kind: SegmentKind,
    pub gain: f32,
    pub queued_frames: u64,
    pub written_frames: u64,
    pub estimated_drained_frames: u64,
    pub flushed_frames: u64,
    pub audio_played_ms: u64,
    pub output_start_frame: Option<u64>,
    pub output_end_frame: u64,
    pub ended: bool,
    pub status: SegmentStatus,
    pub start_monotonic_ns: u64,
    pub end_monotonic_ns: Option<u64>,
    pub flush_monotonic_ns: Option<u64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PlayoutEvent {
    pub local_segment_id: SegmentId,
    pub provider_item_id: Option<String>,
    pub kind: SegmentKind,
    pub gain: f32,
    pub queued_frames: u64,
    pub written_frames: u64,
    pub estimated_drained_frames: u64,
    pub flushed_frames: u64,
    pub audio_played_ms: u64,
    pub output_start_frame: Option<u64>,
    pub output_end_frame: u64,
    pub ended: bool,
    pub status: SegmentStatus,
    pub start_monotonic_ns: u64,
    pub end_monotonic_ns: Option<u64>,
    pub flush_monotonic_ns: Option<u64>,
}

pub struct PlayoutLedger {
    sample_rate: u32,
    next_id: u64,
    segments: Vec<PlayoutSegment>,
}

impl PlayoutLedger {
    pub fn new(sample_rate: u32) -> Self {
        assert!(sample_rate > 0, "sample rate must be > 0");
        Self {
            sample_rate,
            next_id: 1,
            segments: Vec::new(),
        }
    }

    pub fn start_segment(
        &mut self,
        provider_item_id: Option<String>,
        kind: SegmentKind,
        gain: f32,
        start_monotonic_ns: u64,
    ) -> SegmentId {
        let id = SegmentId(self.next_id);
        self.next_id += 1;
        self.segments.push(PlayoutSegment {
            local_segment_id: id,
            provider_item_id,
            kind,
            gain,
            queued_frames: 0,
            written_frames: 0,
            estimated_drained_frames: 0,
            flushed_frames: 0,
            audio_played_ms: 0,
            output_start_frame: None,
            output_end_frame: 0,
            ended: false,
            status: SegmentStatus::Queued,
            start_monotonic_ns,
            end_monotonic_ns: None,
            flush_monotonic_ns: None,
        });
        id
    }

    pub fn queue_frames(&mut self, id: SegmentId, frames: u64) {
        let segment = self.segment_mut(id);
        segment.queued_frames = segment.queued_frames.saturating_add(frames);
    }

    pub fn mark_written_frames(&mut self, id: SegmentId, frames: u64) {
        self.mark_written_frames_at(id, frames, 0);
    }

    pub fn mark_written_frames_at(&mut self, id: SegmentId, frames: u64, output_start_frame: u64) {
        let segment = self.segment_mut(id);
        if frames == 0 {
            return;
        }
        if segment.output_start_frame.is_none() {
            segment.output_start_frame = Some(output_start_frame);
        }
        segment.output_end_frame = segment
            .output_end_frame
            .max(output_start_frame.saturating_add(frames));
        segment.written_frames = segment.written_frames.saturating_add(frames);
        if segment.written_frames > 0 && segment.status == SegmentStatus::Queued {
            segment.status = SegmentStatus::Playing;
        }
    }

    pub fn mark_drained_frames(&mut self, id: SegmentId, frames: u64) {
        let sample_rate = self.sample_rate;
        let segment = self.segment_mut(id);
        let capped = frames.min(segment.written_frames);
        segment.estimated_drained_frames = segment.estimated_drained_frames.max(capped);
        segment.audio_played_ms = frames_to_ms(segment.estimated_drained_frames, sample_rate);
        maybe_mark_drained(segment);
    }

    pub fn mark_drained_through(&mut self, output_frame: u64) {
        let sample_rate = self.sample_rate;
        for segment in &mut self.segments {
            if segment.status == SegmentStatus::Flushed {
                continue;
            }
            let Some(start) = segment.output_start_frame else {
                continue;
            };
            let drained = output_frame
                .saturating_sub(start)
                .min(segment.written_frames);
            segment.estimated_drained_frames = segment.estimated_drained_frames.max(drained);
            segment.audio_played_ms = frames_to_ms(segment.estimated_drained_frames, sample_rate);
            maybe_mark_drained(segment);
        }
    }

    pub fn end_segment(&mut self, id: SegmentId, end_monotonic_ns: u64) {
        let segment = self.segment_mut(id);
        if segment.status == SegmentStatus::Flushed {
            return;
        }
        segment.ended = true;
        segment.end_monotonic_ns = Some(end_monotonic_ns);
        maybe_mark_drained(segment);
    }

    pub fn flush_open_segments(&mut self, flush_monotonic_ns: u64) -> Vec<PlayoutEvent> {
        let sample_rate = self.sample_rate;
        let mut events = Vec::new();
        for segment in &mut self.segments {
            if matches!(
                segment.status,
                SegmentStatus::Drained | SegmentStatus::Flushed
            ) {
                continue;
            }
            segment.flushed_frames = segment
                .queued_frames
                .saturating_sub(segment.estimated_drained_frames);
            segment.status = SegmentStatus::Flushed;
            segment.flush_monotonic_ns = Some(flush_monotonic_ns);
            segment.end_monotonic_ns = Some(flush_monotonic_ns);
            segment.ended = true;
            segment.audio_played_ms = frames_to_ms(segment.estimated_drained_frames, sample_rate);
            events.push(segment.as_event());
        }
        events
    }

    pub fn prune_terminal_segments(&mut self, retain_terminal: usize) {
        let terminal_count = self
            .segments
            .iter()
            .filter(|segment| segment.is_terminal())
            .count();
        if terminal_count <= retain_terminal {
            return;
        }

        let mut to_drop = terminal_count - retain_terminal;
        self.segments.retain(|segment| {
            if to_drop > 0 && segment.is_terminal() {
                to_drop -= 1;
                false
            } else {
                true
            }
        });
    }

    pub fn segment(&self, id: SegmentId) -> &PlayoutSegment {
        self.segments
            .iter()
            .find(|segment| segment.local_segment_id == id)
            .expect("unknown playout segment id")
    }

    fn segment_mut(&mut self, id: SegmentId) -> &mut PlayoutSegment {
        self.segments
            .iter_mut()
            .find(|segment| segment.local_segment_id == id)
            .expect("unknown playout segment id")
    }
}

impl PlayoutSegment {
    fn is_terminal(&self) -> bool {
        matches!(self.status, SegmentStatus::Drained | SegmentStatus::Flushed)
    }

    fn as_event(&self) -> PlayoutEvent {
        PlayoutEvent {
            local_segment_id: self.local_segment_id,
            provider_item_id: self.provider_item_id.clone(),
            kind: self.kind,
            gain: self.gain,
            queued_frames: self.queued_frames,
            written_frames: self.written_frames,
            estimated_drained_frames: self.estimated_drained_frames,
            flushed_frames: self.flushed_frames,
            audio_played_ms: self.audio_played_ms,
            output_start_frame: self.output_start_frame,
            output_end_frame: self.output_end_frame,
            ended: self.ended,
            status: self.status,
            start_monotonic_ns: self.start_monotonic_ns,
            end_monotonic_ns: self.end_monotonic_ns,
            flush_monotonic_ns: self.flush_monotonic_ns,
        }
    }
}

fn frames_to_ms(frames: u64, sample_rate: u32) -> u64 {
    frames.saturating_mul(1000) / (sample_rate as u64)
}

fn maybe_mark_drained(segment: &mut PlayoutSegment) {
    if segment.ended
        && segment.queued_frames > 0
        && segment.estimated_drained_frames >= segment.queued_frames
        && segment.status != SegmentStatus::Flushed
    {
        segment.status = SegmentStatus::Drained;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ledger_tracks_written_drained_and_audio_played_ms() {
        let mut ledger = PlayoutLedger::new(48_000);
        let id = ledger.start_segment(Some("item-1".to_string()), SegmentKind::Assistant, 0.8, 100);

        ledger.queue_frames(id, 96_000);
        ledger.mark_written_frames(id, 48_000);
        ledger.mark_drained_frames(id, 24_000);

        let segment = ledger.segment(id);
        assert_eq!(segment.status, SegmentStatus::Playing);
        assert_eq!(segment.written_frames, 48_000);
        assert_eq!(segment.estimated_drained_frames, 24_000);
        assert_eq!(segment.audio_played_ms, 500);
    }

    #[test]
    fn drained_frames_are_capped_at_written_frames() {
        let mut ledger = PlayoutLedger::new(48_000);
        let id = ledger.start_segment(None, SegmentKind::Cue, 1.0, 100);

        ledger.queue_frames(id, 96_000);
        ledger.mark_written_frames(id, 24_000);
        ledger.mark_drained_frames(id, 96_000);

        let segment = ledger.segment(id);
        assert_eq!(segment.estimated_drained_frames, 24_000);
        assert_eq!(segment.audio_played_ms, 500);
    }

    #[test]
    fn drain_estimate_tracks_output_clock_but_waits_for_segment_end() {
        let mut ledger = PlayoutLedger::new(48_000);
        let id = ledger.start_segment(Some("item-1".to_string()), SegmentKind::Assistant, 0.7, 100);

        ledger.queue_frames(id, 48_000);
        ledger.mark_written_frames_at(id, 48_000, 96_000);
        ledger.mark_drained_through(120_000);

        let segment = ledger.segment(id);
        assert_eq!(segment.output_start_frame, Some(96_000));
        assert_eq!(segment.output_end_frame, 144_000);
        assert_eq!(segment.estimated_drained_frames, 24_000);
        assert_eq!(segment.audio_played_ms, 500);
        assert_eq!(segment.status, SegmentStatus::Playing);

        ledger.mark_drained_through(144_000);
        assert_eq!(ledger.segment(id).status, SegmentStatus::Playing);

        ledger.end_segment(id, 200);
        assert_eq!(ledger.segment(id).status, SegmentStatus::Drained);
        assert!(ledger.segment(id).ended);
    }

    #[test]
    fn flush_reports_unheard_frames_and_played_duration() {
        let mut ledger = PlayoutLedger::new(48_000);
        let id = ledger.start_segment(Some("item-1".to_string()), SegmentKind::Assistant, 0.7, 100);

        ledger.queue_frames(id, 96_000);
        ledger.mark_written_frames(id, 48_000);
        ledger.mark_drained_frames(id, 48_000);

        let events = ledger.flush_open_segments(200);

        assert_eq!(events.len(), 1);
        assert_eq!(events[0].local_segment_id, id);
        assert_eq!(events[0].status, SegmentStatus::Flushed);
        assert_eq!(events[0].audio_played_ms, 1000);
        assert_eq!(events[0].flushed_frames, 48_000);
        assert_eq!(events[0].flush_monotonic_ns, Some(200));
    }

    #[test]
    fn prune_terminal_segments_keeps_active_and_recent_terminal_segments() {
        let mut ledger = PlayoutLedger::new(48_000);
        let old_drained = ledger.start_segment(None, SegmentKind::Assistant, -6.0, 1);
        ledger.queue_frames(old_drained, 48);
        ledger.mark_written_frames(old_drained, 48);
        ledger.mark_drained_frames(old_drained, 48);
        ledger.end_segment(old_drained, 2);

        let old_flushed = ledger.start_segment(None, SegmentKind::Assistant, -6.0, 2);
        ledger.queue_frames(old_flushed, 48);
        let _ = ledger.flush_open_segments(3);

        let active = ledger.start_segment(None, SegmentKind::Assistant, -6.0, 4);
        ledger.queue_frames(active, 48);

        let recent_a = ledger.start_segment(None, SegmentKind::Assistant, -6.0, 5);
        ledger.queue_frames(recent_a, 48);
        ledger.mark_written_frames(recent_a, 48);
        ledger.mark_drained_frames(recent_a, 48);
        ledger.end_segment(recent_a, 6);

        let recent_b = ledger.start_segment(None, SegmentKind::Assistant, -6.0, 6);
        ledger.queue_frames(recent_b, 48);
        ledger.mark_written_frames(recent_b, 48);
        ledger.mark_drained_frames(recent_b, 48);
        ledger.end_segment(recent_b, 7);

        ledger.prune_terminal_segments(2);

        assert!(ledger.segments.iter().all(|segment| {
            segment.local_segment_id != old_drained && segment.local_segment_id != old_flushed
        }));
        assert_eq!(ledger.segment(active).status, SegmentStatus::Queued);
        assert_eq!(ledger.segment(recent_a).status, SegmentStatus::Drained);
        assert_eq!(ledger.segment(recent_b).status, SegmentStatus::Drained);
    }
}
