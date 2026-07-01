// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Output-owner core with fake transports for tests and developer runs.

use crate::fake::{FakeAssistantSource, FakeContentSource, FakeDacSink, SegmentWrite};
use crate::ledger::{PlayoutEvent, PlayoutLedger, SegmentId, DEFAULT_TERMINAL_SEGMENT_RETENTION};
use crate::loudness::{AssistantGainDecision, AssistantLoudness, AssistantLoudnessConfig};
use crate::mixer::{gain_db_to_linear, mix_i16_saturating, sanitize_tts_gain_db};
use crate::reference::{ConsumerId, ReferenceFanout};
use crate::types::{AssistantProfile, AudioFormat, SegmentKind, CHANNELS, SAMPLE_RATE};

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
    loudness: AssistantLoudness,
    content_buf: Vec<i16>,
    assistant_buf: Vec<i16>,
    output_buf: Vec<i16>,
    segment_writes: Vec<SegmentWrite>,
    pending_clipped_samples: u32,
    prepared_period_ready: bool,
    content_meter_paused: bool,
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
            loudness: AssistantLoudness::new(AssistantLoudnessConfig::default()),
            content_buf: vec![0; period_samples],
            assistant_buf: vec![0; period_samples],
            output_buf: vec![0; period_samples],
            segment_writes: Vec::with_capacity(4),
            pending_clipped_samples: 0,
            prepared_period_ready: false,
            content_meter_paused: false,
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
        let id = self.start_assistant_segment(provider_item_id, kind, gain);
        self.append_assistant_audio_with_segment_gain(id, samples);
        self.end_assistant_segment(id);
        id
    }

    pub fn start_assistant_segment(
        &mut self,
        provider_item_id: Option<String>,
        kind: SegmentKind,
        gain: f32,
    ) -> SegmentId {
        self.start_assistant_segment_with_profile(provider_item_id, kind, gain, None)
    }

    pub fn start_assistant_segment_with_profile(
        &mut self,
        provider_item_id: Option<String>,
        kind: SegmentKind,
        fallback_gain: f32,
        profile: Option<AssistantProfile>,
    ) -> SegmentId {
        let decision = self.loudness.decide_gain(kind, fallback_gain, profile);
        log_assistant_loudness_decision(kind, &decision);
        let clamped_gain = decision.final_gain_db;
        self.ledger
            .start_segment(provider_item_id, kind, clamped_gain, self.monotonic_ns)
    }

    pub fn append_assistant_audio(&mut self, id: SegmentId, gain: f32, samples: Vec<i16>) {
        assert_eq!(samples.len() % (self.format.channels as usize), 0);
        if samples.is_empty() {
            return;
        }
        let sanitized_gain = sanitize_tts_gain_db(gain);
        let frames = (samples.len() / (self.format.channels as usize)) as u64;
        self.ledger.queue_frames(id, frames);
        self.assistant
            .enqueue_segment(id, samples, gain_db_to_linear(sanitized_gain));
    }

    pub fn append_assistant_audio_with_segment_gain(&mut self, id: SegmentId, samples: Vec<i16>) {
        let gain = self.ledger.segment(id).gain;
        self.append_assistant_audio(id, gain, samples);
    }

    pub fn end_assistant_segment(&mut self, id: SegmentId) {
        self.ledger.end_segment(id, self.monotonic_ns);
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
        if !self.content_meter_paused {
            self.loudness.observe_content_period(&self.content_buf);
        }
        self.assistant
            .read_period_into(&mut self.assistant_buf, &mut self.segment_writes);

        let mix_stats =
            mix_i16_saturating(&self.content_buf, &self.assistant_buf, &mut self.output_buf);
        self.pending_clipped_samples = mix_stats.clipped_samples;
        self.prepared_period_ready = true;
        mix_stats.clipped_samples
    }

    pub fn commit_prepared_period(&mut self) -> PeriodReport {
        self.commit_prepared_period_with_dac_delay(0)
    }

    pub fn commit_prepared_period_with_dac_delay(&mut self, dac_delay_frames: u64) -> PeriodReport {
        assert!(
            self.prepared_period_ready,
            "output period must be prepared before commit"
        );
        self.dac.write_period(&self.output_buf);

        let period_start_frame = self.frames_written;
        let mut segment_cursor_frame = period_start_frame;
        for write in &self.segment_writes {
            self.ledger
                .mark_written_frames_at(write.id, write.frames, segment_cursor_frame);
            segment_cursor_frame = segment_cursor_frame.saturating_add(write.frames);
        }
        let accepted_end_frame = self
            .frames_written
            .saturating_add(self.period_frames as u64);
        self.ledger
            .mark_drained_through(accepted_end_frame.saturating_sub(dac_delay_frames));
        self.ledger
            .prune_terminal_segments(DEFAULT_TERMINAL_SEGMENT_RETENTION);

        let published = self.reference.publish(
            &self.output_buf,
            self.period_frames,
            self.pending_clipped_samples,
            self.monotonic_ns,
        );
        self.frames_written = accepted_end_frame;
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

    pub fn set_assistant_loudness_config(&mut self, config: AssistantLoudnessConfig) {
        self.loudness = AssistantLoudness::new(config);
    }

    pub fn prepare_assistant_context(
        &mut self,
        provider: String,
        model: String,
        voice: String,
        silence_target_lufs: f32,
    ) {
        self.loudness
            .prepare_context(provider, model, voice, silence_target_lufs);
    }

    pub fn pause_content_meter(&mut self) {
        self.content_meter_paused = true;
    }

    pub fn content_meter_paused(&self) -> bool {
        self.content_meter_paused
    }

    pub fn resume_content_meter(&mut self) {
        self.content_meter_paused = false;
        self.loudness.clear_context();
    }

    pub fn content_short_lufs(&self) -> Option<f32> {
        self.loudness.content_short_lufs()
    }

    pub fn content_anchor_lufs(&self) -> Option<f32> {
        self.loudness.content_anchor_lufs()
    }

    pub fn last_assistant_loudness_decision(&self) -> Option<&AssistantGainDecision> {
        self.loudness.last_decision()
    }
}

fn log_assistant_loudness_decision(kind: SegmentKind, decision: &AssistantGainDecision) {
    eprintln!(
        "event=outputd.assistant_loudness kind={} provider={} model={} voice={} calibrated={} confidence={:.2} baseline_lufs={:.1} target_lufs={:.1} source_lufs={:.1} source_peak_dbfs={:.1} requested_gain_db={:.1} peak_cap_gain_db={:.1} final_gain_db={:.1} reason={}",
        kind.as_str(),
        decision.provider.as_deref().unwrap_or("-"),
        decision.model.as_deref().unwrap_or("-"),
        decision.voice.as_deref().unwrap_or("-"),
        decision.calibrated,
        decision.profile_confidence,
        decision.baseline_lufs,
        decision.target_lufs,
        decision.source_lufs,
        decision.source_peak_dbfs,
        decision.requested_gain_db,
        decision.peak_cap_gain_db,
        decision.final_gain_db,
        decision.clamp_reason,
    );
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
        assert_eq!(core.dac().periods[0], stereo(10_839, 4));
        let reference = core.drain_reference_consumer(consumer);
        assert_eq!(reference.len(), 1);
        assert_eq!(reference[0].samples, stereo(10_839, 4));
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
    fn outputd_uses_loudness_decided_assistant_gain_before_mixing() {
        let mut core = OutputCore::new(2, 99);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            20.0,
            stereo(10_000, 2),
        );

        core.step();

        // With no observed content and no profile, the decision falls back
        // to the silence target: baseline_lufs=-41.0, target_lufs=-39.5,
        // fallback source_lufs=-24.0, so requested_gain=-15.5. The fixed
        // max-gain ceiling is gone; this helper now scales with the same
        // decided segment gain that the runtime TTS bridge uses.
        assert_eq!(core.ledger().segment(segment).gain, -15.5);
        assert_eq!(core.dac().periods[0], stereo(1679, 2));
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
        assert_eq!(core.output_period(), stereo(939, 2).as_slice());
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
        assert_eq!(core.dac().periods[0], stereo(939, 2));
        assert_eq!(core.drain_reference_consumer(consumer).len(), 1);
        assert_eq!(core.ledger().segment(segment).written_frames, 2);
        assert_eq!(
            core.ledger().segment(segment).status,
            crate::ledger::SegmentStatus::Drained
        );
    }

    #[test]
    fn dac_delay_defers_playout_drain_accounting() {
        let mut core = OutputCore::new(48, 99);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            -6.0,
            stereo(1000, 96),
        );

        core.prepare_period();
        let _ = core.commit_prepared_period_with_dac_delay(48);
        assert_eq!(core.ledger().segment(segment).written_frames, 48);
        assert_eq!(core.ledger().segment(segment).estimated_drained_frames, 0);
        assert_eq!(
            core.ledger().segment(segment).status,
            crate::ledger::SegmentStatus::Playing
        );

        core.prepare_period();
        let _ = core.commit_prepared_period_with_dac_delay(48);
        assert_eq!(core.ledger().segment(segment).written_frames, 96);
        assert_eq!(core.ledger().segment(segment).estimated_drained_frames, 48);
        assert_eq!(core.ledger().segment(segment).audio_played_ms, 1);
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
    fn resuming_content_meter_clears_prepared_loudness_context() {
        let mut core = OutputCore::new(2, 99);
        core.prepare_assistant_context(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -20.0,
        );
        core.resume_content_meter();

        let segment = core.start_assistant_segment_with_profile(
            None,
            SegmentKind::Assistant,
            -12.0,
            Some(AssistantProfile {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                source_lufs: Some(-30.0),
                source_peak_dbfs: Some(-18.0),
                confidence: 1.0,
            }),
        );

        // `resume_content_meter()` cleared the prepared context, so the prepared
        // silence target (-20.0) is discarded and the decision falls back to the
        // default silence target. With no observed content: baseline_lufs=-41.0,
        // target_lufs=-41.0+1.5=-39.5; the profile supplies source_lufs=-30.0, so
        // requested_gain=-39.5-(-30.0)=-9.5; the peak cap (-3.0-(-18.0)=+15.0)
        // leaves -9.5 unchanged. The -12.0 fallback gain is
        // ignored once a profile yields a calibrated target (see the passing
        // `calibrated_profile_targets_baseline_plus_offset`). Had the context not
        // been cleared, baseline=-20.0 would drive the gain to the dynamic
        // peak cap instead of the quiet-room target.
        assert_eq!(core.ledger().segment(segment).gain, -9.5);
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
