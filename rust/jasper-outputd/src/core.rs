//! Output-owner core with fake transports for tests and developer runs.

use crate::fake::{FakeAssistantSource, FakeContentSource, FakeDacSink, SegmentWrite};
use crate::ledger::{PlayoutEvent, PlayoutLedger, SegmentId, DEFAULT_TERMINAL_SEGMENT_RETENTION};
use crate::mixer::{clamp_tts_gain_db, gain_db_to_linear, mix_i16_saturating};
use crate::reference::{ConsumerId, ReferenceFanout};
use crate::types::{AudioFormat, SegmentKind, CHANNELS, SAMPLE_RATE};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeriodReport {
    pub frames_written: u64,
    pub clipped_samples: u32,
    pub reference_sequence: u64,
}

pub struct OutputCore {
    period_frames: u32,
    format: AudioFormat,
    content: FakeContentSource,
    assistant: FakeAssistantSource,
    dac: FakeDacSink,
    reference: ReferenceFanout,
    ledger: PlayoutLedger,
    content_buf: Vec<i16>,
    assistant_buf: Vec<i16>,
    output_buf: Vec<i16>,
    segment_writes: Vec<SegmentWrite>,
    pending_clipped_samples: u32,
    prepared_period_ready: bool,
    frames_written: u64,
    monotonic_ns: u64,
}

impl OutputCore {
    pub fn new(period_frames: u32, stream_id: u64) -> Self {
        Self::with_dac(period_frames, stream_id, FakeDacSink::new())
    }

    pub fn new_for_daemon(period_frames: u32, stream_id: u64) -> Self {
        Self::with_dac(period_frames, stream_id, FakeDacSink::discarding())
    }

    fn with_dac(period_frames: u32, stream_id: u64, dac: FakeDacSink) -> Self {
        assert!(period_frames > 0, "period frames must be > 0");
        let format = AudioFormat::default();
        let period_samples = format.samples_for_frames(period_frames);
        Self {
            period_frames,
            format,
            content: FakeContentSource::new(),
            assistant: FakeAssistantSource::new(CHANNELS as usize),
            dac,
            reference: ReferenceFanout::new(stream_id, format, period_frames),
            ledger: PlayoutLedger::new(SAMPLE_RATE),
            content_buf: vec![0; period_samples],
            assistant_buf: vec![0; period_samples],
            output_buf: vec![0; period_samples],
            segment_writes: Vec::with_capacity(4),
            pending_clipped_samples: 0,
            prepared_period_ready: false,
            frames_written: 0,
            monotonic_ns: 0,
        }
    }

    pub fn add_reference_consumer(
        &mut self,
        name: impl Into<String>,
        capacity_packets: usize,
    ) -> ConsumerId {
        self.reference.add_consumer(name, capacity_packets)
    }

    pub fn push_content_period(&mut self, samples: Vec<i16>) {
        assert_eq!(samples.len(), self.output_buf.len());
        self.content.push_period(samples);
    }

    pub fn enqueue_assistant_segment(
        &mut self,
        provider_item_id: Option<String>,
        kind: SegmentKind,
        gain: f32,
        samples: Vec<i16>,
    ) -> SegmentId {
        assert_eq!(samples.len() % (self.format.channels as usize), 0);
        let clamped_gain = clamp_tts_gain_db(gain);
        let id = self
            .ledger
            .start_segment(provider_item_id, kind, clamped_gain, self.monotonic_ns);
        let frames = (samples.len() / (self.format.channels as usize)) as u64;
        self.ledger.queue_frames(id, frames);
        self.assistant
            .enqueue_segment(id, samples, gain_db_to_linear(clamped_gain));
        id
    }

    pub fn step(&mut self) -> PeriodReport {
        self.prepare_period();
        self.commit_prepared_period()
    }

    pub fn step_with_content_period(&mut self, samples: &[i16]) -> PeriodReport {
        self.prepare_period_with_content(samples);
        self.commit_prepared_period()
    }

    pub fn prepare_period(&mut self) -> u32 {
        self.assert_no_prepared_period();
        self.content.read_period(&mut self.content_buf);
        self.prepare_from_buffered_content()
    }

    pub fn prepare_period_with_content(&mut self, samples: &[i16]) -> u32 {
        self.assert_no_prepared_period();
        assert_eq!(samples.len(), self.content_buf.len());
        self.content_buf.copy_from_slice(samples);
        self.prepare_from_buffered_content()
    }

    fn assert_no_prepared_period(&self) {
        assert!(
            !self.prepared_period_ready,
            "prepared output period was not committed"
        );
    }

    fn prepare_from_buffered_content(&mut self) -> u32 {
        self.assistant
            .read_period_into(&mut self.assistant_buf, &mut self.segment_writes);

        let mix_stats =
            mix_i16_saturating(&self.content_buf, &self.assistant_buf, &mut self.output_buf);
        self.pending_clipped_samples = mix_stats.clipped_samples;
        self.prepared_period_ready = true;
        mix_stats.clipped_samples
    }

    pub fn commit_prepared_period(&mut self) -> PeriodReport {
        assert!(
            self.prepared_period_ready,
            "output period must be prepared before commit"
        );
        self.dac.write_period(&self.output_buf);

        for write in &self.segment_writes {
            self.ledger.mark_written_frames(write.id, write.frames);
            let drained = self.ledger.segment(write.id).written_frames;
            self.ledger.mark_drained_frames(write.id, drained);
        }
        self.ledger
            .prune_terminal_segments(DEFAULT_TERMINAL_SEGMENT_RETENTION);

        let published = self.reference.publish(
            &self.output_buf,
            self.period_frames,
            self.pending_clipped_samples,
            self.monotonic_ns,
        );
        self.frames_written += self.period_frames as u64;
        self.monotonic_ns +=
            (self.period_frames as u64) * 1_000_000_000u64 / (self.format.sample_rate as u64);
        self.prepared_period_ready = false;

        PeriodReport {
            frames_written: self.frames_written,
            clipped_samples: self.pending_clipped_samples,
            reference_sequence: published.sequence,
        }
    }

    pub fn flush_assistant(&mut self) -> Vec<PlayoutEvent> {
        self.assistant.flush();
        let events = self.ledger.flush_open_segments(self.monotonic_ns);
        self.ledger
            .prune_terminal_segments(DEFAULT_TERMINAL_SEGMENT_RETENTION);
        events
    }

    pub fn drain_reference_consumer(
        &mut self,
        id: ConsumerId,
    ) -> Vec<crate::types::ReferencePacket> {
        self.reference.drain_consumer(id)
    }

    pub fn dac(&self) -> &FakeDacSink {
        &self.dac
    }

    pub fn output_period(&self) -> &[i16] {
        &self.output_buf
    }

    pub fn period_samples(&self) -> usize {
        self.output_buf.len()
    }

    pub fn frames_written(&self) -> u64 {
        self.frames_written
    }

    pub fn pending_assistant_frames(&self) -> u64 {
        self.assistant.pending_frames()
    }

    #[cfg(test)]
    fn segment_write_capacity(&self) -> usize {
        self.segment_writes.capacity()
    }

    pub fn ledger(&self) -> &PlayoutLedger {
        &self.ledger
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stereo(value: i16, frames: usize) -> Vec<i16> {
        vec![value; frames * (CHANNELS as usize)]
    }

    #[test]
    fn step_mixes_content_and_assistant_to_dac_and_reference() {
        let mut core = OutputCore::new(4, 99);
        let consumer = core.add_reference_consumer("aec", 4);
        core.push_content_period(stereo(10_000, 4));
        core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            -6.0,
            stereo(5000, 4),
        );

        let report = core.step();

        assert_eq!(report.reference_sequence, 0);
        assert_eq!(report.clipped_samples, 0);
        assert_eq!(core.dac().periods[0], stereo(12_506, 4));
        let reference = core.drain_reference_consumer(consumer);
        assert_eq!(reference.len(), 1);
        assert_eq!(reference[0].samples, stereo(12_506, 4));
    }

    #[test]
    fn step_reports_clipping_and_preserves_sequence_numbers() {
        let mut core = OutputCore::new(2, 99);
        core.push_content_period(stereo(30_000, 2));
        core.enqueue_assistant_segment(None, SegmentKind::Cue, 12.0, stereo(30_000, 2));

        let first = core.step();
        let second = core.step();

        assert_eq!(first.clipped_samples, 4);
        assert_eq!(first.reference_sequence, 0);
        assert_eq!(second.reference_sequence, 1);
    }

    #[test]
    fn outputd_clamps_assistant_gain_before_mixing() {
        let mut core = OutputCore::new(2, 99);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            20.0,
            stereo(10_000, 2),
        );

        core.step();

        assert_eq!(core.ledger().segment(segment).gain, -6.0);
        assert_eq!(core.dac().periods[0], stereo(5012, 2));
    }

    #[test]
    fn step_with_content_period_uses_caller_supplied_content() {
        let mut core = OutputCore::new(2, 99);

        let report = core.step_with_content_period(&stereo(123, 2));

        assert_eq!(report.frames_written, 2);
        assert_eq!(core.output_period(), stereo(123, 2).as_slice());
        assert_eq!(core.dac().periods[0], stereo(123, 2));
    }

    #[test]
    fn pending_assistant_frames_tracks_queue_depth() {
        let mut core = OutputCore::new(2, 99);
        core.enqueue_assistant_segment(None, SegmentKind::Assistant, -6.0, stereo(100, 4));

        assert_eq!(core.pending_assistant_frames(), 4);

        core.step();

        assert_eq!(core.pending_assistant_frames(), 2);
    }

    #[test]
    fn prepare_does_not_publish_or_mark_playout_until_commit() {
        let mut core = OutputCore::new(2, 99);
        let consumer = core.add_reference_consumer("aec", 4);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            -6.0,
            stereo(5000, 2),
        );

        let clipped = core.prepare_period_with_content(&stereo(100, 2));

        assert_eq!(clipped, 0);
        assert_eq!(core.output_period(), stereo(2606, 2).as_slice());
        assert_eq!(core.frames_written(), 0);
        assert!(core.dac().periods.is_empty());
        assert!(core.drain_reference_consumer(consumer).is_empty());
        assert_eq!(core.ledger().segment(segment).written_frames, 0);
        assert_eq!(
            core.ledger().segment(segment).status,
            crate::ledger::SegmentStatus::Queued
        );

        let report = core.commit_prepared_period();

        assert_eq!(report.frames_written, 2);
        assert_eq!(report.reference_sequence, 0);
        assert_eq!(core.dac().periods[0], stereo(2606, 2));
        assert_eq!(core.drain_reference_consumer(consumer).len(), 1);
        assert_eq!(core.ledger().segment(segment).written_frames, 2);
        assert_eq!(
            core.ledger().segment(segment).status,
            crate::ledger::SegmentStatus::Drained
        );
    }

    #[test]
    fn steady_state_reuses_segment_write_buffer_capacity() {
        let mut core = OutputCore::new(2, 99);
        core.enqueue_assistant_segment(None, SegmentKind::Assistant, -6.0, stereo(1000, 8));

        core.step();
        let capacity = core.segment_write_capacity();
        core.step();
        core.step();

        assert_eq!(core.segment_write_capacity(), capacity);
    }

    #[test]
    fn daemon_core_does_not_retain_fake_dac_history() {
        let mut core = OutputCore::new_for_daemon(2, 99);

        core.step();
        core.step();

        assert!(core.dac().periods.is_empty());
    }

    #[test]
    fn flush_clears_queued_assistant_audio_and_reports_played_ms() {
        let mut core = OutputCore::new(48, 99);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            -6.0,
            stereo(1000, 96),
        );

        core.step();
        let events = core.flush_assistant();
        core.step();

        assert_eq!(events.len(), 1);
        assert_eq!(events[0].local_segment_id, segment);
        assert_eq!(events[0].audio_played_ms, 1);
        assert_eq!(events[0].flushed_frames, 48);
        assert_eq!(core.dac().periods[1], stereo(0, 48));
        assert_eq!(
            core.ledger().segment(segment).status,
            crate::ledger::SegmentStatus::Flushed
        );
    }
}
