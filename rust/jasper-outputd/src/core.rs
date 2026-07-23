// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Output-owner core with fake transports for tests and developer runs.

use std::sync::Arc;

use crate::fake::{FakeAssistantSource, FakeContentSource, FakeDacSink, SegmentWrite};
use crate::ledger::{PlayoutEvent, PlayoutLedger, SegmentId, DEFAULT_TERMINAL_SEGMENT_RETENTION};
use crate::loudness::{
    AssistantGainDecision, AssistantLoudness, AssistantLoudnessConfig, MixStage,
};
use crate::mixer::{mix_i16_saturating, sanitize_tts_gain_db};
use crate::types::{AssistantProfile, AudioFormat, SegmentKind, CHANNELS, SAMPLE_RATE};
use jasper_tts_protocol::VolumeContext;

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
    next_reference_sequence: u64,
    ledger: PlayoutLedger,
    loudness: AssistantLoudness,
    /// The most recent segment's gain decision, kept so appended audio for the
    /// currently-open segment can carry its peak-cap ceiling and live re-gain
    /// residual into the per-period mix (matched by segment id).
    active_assistant_decision: Option<(SegmentId, Arc<AssistantGainDecision>)>,
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
    pub fn new(period_frames: u32) -> Self {
        Self::with_dac(period_frames, FakeDacSink::new())
    }

    pub fn new_for_daemon(period_frames: u32) -> Self {
        Self::with_dac(period_frames, FakeDacSink::discarding())
    }

    fn with_dac(period_frames: u32, dac: FakeDacSink) -> Self {
        assert!(period_frames > 0, "period frames must be > 0");
        let format = AudioFormat::default();
        let period_samples = format.samples_for_frames(period_frames);
        Self {
            period_frames,
            format,
            content: FakeContentSource::new(),
            assistant: FakeAssistantSource::new(CHANNELS as usize),
            dac,
            next_reference_sequence: 0,
            ledger: PlayoutLedger::new(SAMPLE_RATE),
            // outputd is structurally post-DSP: its assistant mix is downstream
            // of CamillaDSP, so the loudness engine treats VolumeContext's
            // downstream_db as 0.0 (see MixStage). Applying fan-in's pre-DSP
            // downstream compensation here would double-compensate by the full
            // Camilla gain.
            loudness: AssistantLoudness::new_with_stage(
                AssistantLoudnessConfig::default(),
                MixStage::PostDsp,
            ),
            active_assistant_decision: None,
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
        let id = self
            .ledger
            .start_segment(provider_item_id, kind, clamped_gain, self.monotonic_ns);
        // Retain the decision so this segment's appended audio can carry its
        // peak cap + live re-gain residual into the per-period mix.
        self.active_assistant_decision = Some((id, Arc::new(decision)));
        id
    }

    pub fn append_assistant_audio(&mut self, id: SegmentId, gain: f32, samples: Vec<i16>) {
        // Legacy direct path: render at the given gain with no headroom
        // (peak cap == base) and no live tracking (no decision).
        self.append_assistant_audio_planned(id, gain, None, samples);
    }

    pub fn append_assistant_audio_with_segment_gain(&mut self, id: SegmentId, samples: Vec<i16>) {
        let base_gain_db = self.ledger.segment(id).gain;
        let decision = match &self.active_assistant_decision {
            Some((decision_id, decision)) if *decision_id == id => Some(Arc::clone(decision)),
            _ => None,
        };
        self.append_assistant_audio_planned(id, base_gain_db, decision, samples);
    }

    fn append_assistant_audio_planned(
        &mut self,
        id: SegmentId,
        base_gain_db: f32,
        decision: Option<Arc<AssistantGainDecision>>,
        samples: Vec<i16>,
    ) {
        assert_eq!(samples.len() % (self.format.channels as usize), 0);
        if samples.is_empty() {
            return;
        }
        let base_gain_db = sanitize_tts_gain_db(base_gain_db);
        // The peak-cap ceiling comes from the decision; without one, the
        // segment renders at exactly its base gain (cap == base).
        let peak_cap_gain_db = decision
            .as_deref()
            .map_or(base_gain_db, |decision| decision.peak_cap_gain_db);
        let frames = (samples.len() / (self.format.channels as usize)) as u64;
        self.ledger.queue_frames(id, frames);
        self.assistant
            .enqueue_segment(id, samples, base_gain_db, peak_cap_gain_db, decision);
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
        // The loudness engine is passed in so the mix applies mute + live
        // re-gain per period (the volume context was drained before this).
        self.assistant.read_period_into(
            &mut self.assistant_buf,
            &mut self.segment_writes,
            &self.loudness,
        );

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

        let reference_sequence = self.next_reference_sequence;
        self.next_reference_sequence = self.next_reference_sequence.saturating_add(1);
        self.frames_written = accepted_end_frame;
        self.monotonic_ns +=
            (self.period_frames as u64) * 1_000_000_000u64 / (self.format.sample_rate as u64);
        self.prepared_period_ready = false;

        PeriodReport {
            frames_written: self.frames_written,
            clipped_samples: self.pending_clipped_samples,
            reference_sequence,
        }
    }

    pub fn flush_assistant(&mut self) -> Vec<PlayoutEvent> {
        self.assistant.flush();
        self.active_assistant_decision = None;
        let events = self.ledger.flush_open_segments(self.monotonic_ns);
        self.ledger
            .prune_terminal_segments(DEFAULT_TERMINAL_SEGMENT_RETENTION);
        events
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
        // Preserve outputd's post-DSP mix stage across a config swap.
        self.loudness = AssistantLoudness::new_with_stage(config, MixStage::PostDsp);
    }

    /// Accept an absolute speaker-volume context from the voice daemon.
    ///
    /// Returns whether the update won the boot-clock stale-stamp guard (the
    /// same guard fan-in uses). Post-DSP, the stored downstream_db is retained
    /// verbatim for observability; only its *use* inside the engine is zeroed
    /// (see MixStage) so this lane never inherits pre-DSP compensation.
    pub fn update_volume_context(&mut self, context: VolumeContext) -> bool {
        self.loudness.update_volume_context(context)
    }

    pub fn current_volume_context(&self) -> Option<VolumeContext> {
        self.loudness.current_volume_context()
    }

    pub fn prepare_assistant_context(
        &mut self,
        provider: String,
        model: String,
        voice: String,
        tts_envelope_lufs: f32,
    ) {
        self.loudness
            .prepare_context(provider, model, voice, tts_envelope_lufs);
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
    fn step_mixes_content_and_assistant_and_advances_reference_sequence() {
        let mut core = OutputCore::new(4);
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
        assert_eq!(core.dac().periods[0], stereo(10_706, 4));
    }

    #[test]
    fn step_reports_clipping_and_preserves_sequence_numbers() {
        let mut core = OutputCore::new(2);
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
        let mut core = OutputCore::new(2);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            20.0,
            stereo(10_000, 2),
        );

        core.step();

        // With no observed content and no profile, the decision falls back
        // to the quiet-room envelope: baseline_lufs=target_lufs=-41.0,
        // fallback source_lufs=-24.0, so requested_gain=-17.0. The ordinary
        // music-relative +1.5 LU offset does not apply without music. The fixed
        // max-gain ceiling is gone; this helper now scales with the same
        // decided segment gain that the runtime TTS bridge uses.
        assert_eq!(core.ledger().segment(segment).gain, -17.0);
        assert_eq!(core.dac().periods[0], stereo(1413, 2));
    }

    #[test]
    fn step_with_content_period_uses_caller_supplied_content() {
        let mut core = OutputCore::new(2);

        let report = core.step_with_content_period(&stereo(123, 2));

        assert_eq!(report.frames_written, 2);
        assert_eq!(core.output_period(), stereo(123, 2).as_slice());
        assert_eq!(core.dac().periods[0], stereo(123, 2));
    }

    #[test]
    fn pending_assistant_frames_tracks_queue_depth() {
        let mut core = OutputCore::new(2);
        core.enqueue_assistant_segment(None, SegmentKind::Assistant, -6.0, stereo(100, 4));

        assert_eq!(core.pending_assistant_frames(), 4);

        core.step();

        assert_eq!(core.pending_assistant_frames(), 2);
    }

    #[test]
    fn prepare_does_not_publish_or_mark_playout_until_commit() {
        let mut core = OutputCore::new(2);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            -6.0,
            stereo(5000, 2),
        );

        let clipped = core.prepare_period_with_content(&stereo(100, 2));

        assert_eq!(clipped, 0);
        assert_eq!(core.output_period(), stereo(806, 2).as_slice());
        assert_eq!(core.frames_written(), 0);
        assert!(core.dac().periods.is_empty());
        assert_eq!(core.ledger().segment(segment).written_frames, 0);
        assert_eq!(
            core.ledger().segment(segment).status,
            crate::ledger::SegmentStatus::Queued
        );

        let report = core.commit_prepared_period();

        assert_eq!(report.frames_written, 2);
        assert_eq!(report.reference_sequence, 0);
        assert_eq!(core.dac().periods[0], stereo(806, 2));
        assert_eq!(core.ledger().segment(segment).written_frames, 2);
        assert_eq!(
            core.ledger().segment(segment).status,
            crate::ledger::SegmentStatus::Drained
        );
    }

    #[test]
    fn dac_delay_defers_playout_drain_accounting() {
        let mut core = OutputCore::new(48);
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
        let mut core = OutputCore::new(2);
        core.enqueue_assistant_segment(None, SegmentKind::Assistant, -6.0, stereo(1000, 8));

        core.step();
        let capacity = core.segment_write_capacity();
        core.step();
        core.step();

        assert_eq!(core.segment_write_capacity(), capacity);
    }

    #[test]
    fn daemon_core_does_not_retain_fake_dac_history() {
        let mut core = OutputCore::new_for_daemon(2);

        core.step();
        core.step();

        assert!(core.dac().periods.is_empty());
    }

    #[test]
    fn resuming_content_meter_clears_prepared_loudness_context() {
        let mut core = OutputCore::new(2);
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
        // default envelope. With no observed content:
        // baseline_lufs=target_lufs=-41.0; the profile supplies
        // source_lufs=-30.0, so requested_gain=-41.0-(-30.0)=-11.0; the peak
        // cap (-3.0-(-18.0)=+15.0) leaves -11.0 unchanged. The -12.0 fallback gain is
        // ignored once a profile yields a calibrated target (see the passing
        // `calibrated_profile_targets_baseline_plus_offset`). Had the context not
        // been cleared, baseline=-20.0 would drive the gain to the dynamic
        // peak cap instead of the quiet-room target.
        assert_eq!(core.ledger().segment(segment).gain, -11.0);
    }

    #[test]
    fn flush_clears_queued_assistant_audio_and_reports_played_ms() {
        let mut core = OutputCore::new(48);
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

    fn vc(
        canonical_db: f32,
        downstream_db: f32,
        tts_envelope_lufs: f32,
        muted: bool,
        stamp: u64,
    ) -> VolumeContext {
        VolumeContext {
            canonical_db,
            downstream_db,
            tts_envelope_lufs,
            muted,
            stamp_boot_ns: stamp,
        }
    }

    fn profile(source_lufs: f32, source_peak_dbfs: f32) -> AssistantProfile {
        AssistantProfile {
            provider: "openai".to_string(),
            model: "m".to_string(),
            voice: "v".to_string(),
            source_lufs: Some(source_lufs),
            source_peak_dbfs: Some(source_peak_dbfs),
            confidence: 1.0,
        }
    }

    #[test]
    fn post_dsp_muted_forces_silent_then_ramps_on_unmute() {
        let mut core = OutputCore::new(4);
        // Post-DSP: downstream is ignored, and envelope(-41) == source(-41) so
        // the decided gain is 0 dB — the assistant passes at full amplitude.
        assert!(core.update_volume_context(vc(-30.0, -30.0, -41.0, false, 1)));
        core.prepare_assistant_context(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
        );
        let id = core.start_assistant_segment_with_profile(
            Some("s".to_string()),
            SegmentKind::Assistant,
            0.0,
            Some(profile(-41.0, -3.0)),
        );
        assert_eq!(core.ledger().segment(id).gain, 0.0);
        core.append_assistant_audio_with_segment_gain(id, stereo(8000, 12));
        core.end_assistant_segment(id);

        core.push_content_period(stereo(0, 4));
        core.step();
        assert!(
            core.dac().periods[0].iter().all(|&s| s == 8000),
            "unmuted assistant passes at full amplitude: {:?}",
            core.dac().periods[0]
        );

        // Mute mid-turn: the follower reply is silenced downstream even though
        // frames remain queued — the real safety edge (a Camilla mute upstream
        // cannot silence a reply mixed after CamillaDSP).
        assert!(core.update_volume_context(vc(-30.0, -30.0, -41.0, true, 2)));
        core.push_content_period(stereo(0, 4));
        core.step();
        assert!(
            core.dac().periods[1].iter().all(|&s| s == 0),
            "muted assistant is silent: {:?}",
            core.dac().periods[1]
        );

        // Unmute: ramps back up from silence — audible but below full on the
        // first frame (never a loud snap).
        assert!(core.update_volume_context(vc(-30.0, -30.0, -41.0, false, 3)));
        core.push_content_period(stereo(0, 4));
        core.step();
        let unmuted = &core.dac().periods[2];
        assert!(
            unmuted[0] > 0 && unmuted[0] < 8000,
            "unmute ramps from silence: {}",
            unmuted[0]
        );
    }

    #[test]
    fn post_dsp_live_volume_change_regains_queued_speech() {
        use crate::loudness::{apply_gain_i16, gain_db_to_linear, LIVE_VOLUME_RAMP_FRAMES};

        const PERIOD: u32 = LIVE_VOLUME_RAMP_FRAMES; // ramp completes in one period
        let mut core = OutputCore::new(PERIOD);
        assert!(core.update_volume_context(vc(-30.0, -30.0, -41.0, false, 1)));
        core.prepare_assistant_context(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
        );
        // Ample peak headroom (source_peak -20 → cap +17 dB) so a +6 dB re-gain
        // is not clamped; decided gain is 0 dB (envelope == source).
        let id = core.start_assistant_segment_with_profile(
            Some("s".to_string()),
            SegmentKind::Assistant,
            0.0,
            Some(profile(-41.0, -20.0)),
        );
        assert_eq!(core.ledger().segment(id).gain, 0.0);
        core.append_assistant_audio_with_segment_gain(id, stereo(8000, (PERIOD as usize) * 2));
        core.end_assistant_segment(id);

        core.push_content_period(stereo(0, PERIOD as usize));
        core.step();
        assert!(core.dac().periods[0].iter().all(|&s| s == 8000));

        // Raise the room target +6 dB (envelope -41 → -35) mid-turn. Post-DSP
        // the downstream is ignored, so the mixer carries the full +6 dB.
        assert!(core.update_volume_context(vc(-30.0, -30.0, -35.0, false, 2)));
        core.push_content_period(stereo(0, PERIOD as usize));
        core.step();
        let regained = &core.dac().periods[1];
        assert!(
            (regained[0] - 8000).abs() <= 4,
            "ramp starts without a step discontinuity: {}",
            regained[0]
        );
        let expected = apply_gain_i16(8000, gain_db_to_linear(6.0)) as i32;
        let last = *regained.last().unwrap() as i32;
        assert!(
            (last - expected).abs() <= 2,
            "ramp reaches +6 dB louder: {last} vs {expected}"
        );
    }
}
