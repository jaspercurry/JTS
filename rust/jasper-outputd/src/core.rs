// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Output-owner core. `assistant` is the real production render/gain engine
//! (`assistant_source::AssistantSource`); `content`/`dac` are fake transports
//! for tests and safe developer runs.

use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};

use crate::assistant_source::{
    AssistantSource, DrainedPlayback, FakeDacSink, SegmentPlayback, SegmentWrite,
};
use crate::fake::FakeContentSource;
use crate::ledger::{PlayoutEvent, PlayoutLedger, SegmentId, DEFAULT_TERMINAL_SEGMENT_RETENTION};
use crate::loudness::{
    linear_to_db, AssistantGainDecision, AssistantLoudness, AssistantLoudnessConfig,
    HeldLoudnessReference, MixStage, TtsLoudnessSnapshot,
};
use crate::mixer::{mix_saturating, sanitize_tts_gain_db};
use crate::types::{
    narrow_period, AssistantProfile, AudioFormat, ProgramSample, SegmentKind, CHANNELS, SAMPLE_RATE,
};
use jasper_tts_protocol::{TtsAudioSamples, VolumeContext};

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
    assistant: AssistantSource,
    dac: FakeDacSink,
    next_reference_sequence: u64,
    ledger: PlayoutLedger,
    loudness: AssistantLoudness,
    /// The most recent segment's gain decision, kept so appended audio for the
    /// currently-open segment can carry its peak-cap ceiling and live re-gain
    /// residual into the per-period mix (matched by segment id).
    active_assistant_decision: Option<(SegmentId, Arc<AssistantGainDecision>)>,
    /// Persistence sink for the learned quiet-room assistant reference. `None`
    /// (the default / fake-run case) simply drops learned references.
    assistant_reference_tx: Option<Sender<HeldLoudnessReference>>,
    /// Reusable scratch for the per-period drained-segment completions from the
    /// assistant source (kept to keep the audio loop allocation-free).
    drained_playbacks: Vec<DrainedPlayback>,
    /// Segments whose audio drained before SEGMENT_END arrived: their playout
    /// facts wait here until END fires, then the reference is committed.
    drained_pending_end: Vec<(SegmentId, Option<SegmentPlayback>)>,
    /// Shared STATUS snapshot cell (the daemon's TtsMetrics), published each
    /// period. `None` on the fake/developer path.
    loudness_sink: Option<Arc<Mutex<TtsLoudnessSnapshot>>>,
    /// Count of stale/invalid VolumeContext updates the engine rejected,
    /// surfaced in the snapshot like fan-in's `volume_context_rejected`.
    volume_context_rejected: u64,
    content_buf: Vec<ProgramSample>,
    assistant_buf: Vec<ProgramSample>,
    output_buf: Vec<ProgramSample>,
    /// S16 scratch for the loudness meter's content observation, sized once.
    ///
    /// `AssistantLoudness::observe_content_period` is shared with jasper-fanin,
    /// whose mixer is i16, so its signature stays `&[i16]` — outputd narrows into
    /// this buffer instead of changing a cross-daemon interface. The narrowing is
    /// only ever read by a loudness METER (an LUFS estimate over ~3 s), never
    /// written to a speaker, so the half-LSB it costs is far below the
    /// measurement's own noise floor.
    content_meter_buf: Vec<i16>,
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
        // PANIC-AUDITED: period_frames is the daemon's own period-size config, not external input
        assert!(period_frames > 0, "period frames must be > 0");
        let format = AudioFormat::default();
        let period_samples = format.samples_for_frames(period_frames);
        Self {
            period_frames,
            format,
            content: FakeContentSource::new(),
            assistant: AssistantSource::new(CHANNELS as usize),
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
            assistant_reference_tx: None,
            drained_playbacks: Vec::new(),
            drained_pending_end: Vec::new(),
            loudness_sink: None,
            volume_context_rejected: 0,
            content_buf: vec![0; period_samples],
            assistant_buf: vec![0; period_samples],
            output_buf: vec![0; period_samples],
            content_meter_buf: vec![0i16; period_samples],
            segment_writes: Vec::with_capacity(4),
            pending_clipped_samples: 0,
            prepared_period_ready: false,
            content_meter_paused: false,
            frames_written: 0,
            monotonic_ns: 0,
        }
    }

    pub fn push_content_period(&mut self, samples: Vec<ProgramSample>) {
        // PANIC-AUDITED: output_buf is sized once at construction from the same period config
        assert_eq!(samples.len(), self.output_buf.len());
        self.content.push_period(samples);
    }

    pub fn enqueue_assistant_segment(
        &mut self,
        provider_item_id: Option<String>,
        kind: SegmentKind,
        samples: impl Into<TtsAudioSamples>,
    ) -> SegmentId {
        let id = self.start_assistant_segment(provider_item_id, kind);
        self.append_assistant_audio_with_segment_gain(id, samples);
        self.end_assistant_segment(id);
        id
    }

    pub fn start_assistant_segment(
        &mut self,
        provider_item_id: Option<String>,
        kind: SegmentKind,
    ) -> SegmentId {
        self.start_assistant_segment_with_profile(provider_item_id, kind, None)
    }

    pub fn start_assistant_segment_with_profile(
        &mut self,
        provider_item_id: Option<String>,
        kind: SegmentKind,
        profile: Option<AssistantProfile>,
    ) -> SegmentId {
        let decision = self.loudness.decide_gain(profile);
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

    pub fn append_assistant_audio_with_segment_gain(
        &mut self,
        id: SegmentId,
        samples: impl Into<TtsAudioSamples>,
    ) {
        let segment = self.ledger.segment(id);
        let base_gain_db = segment.gain;
        // Only Assistant-kind segments train the learned quiet-room reference;
        // cues and chirps never do.
        let reference_eligible = segment.kind == SegmentKind::Assistant;
        let decision = match &self.active_assistant_decision {
            Some((decision_id, decision)) if *decision_id == id => Some(Arc::clone(decision)),
            _ => None,
        };
        self.append_assistant_audio_planned(
            id,
            base_gain_db,
            decision,
            reference_eligible,
            samples,
        );
    }

    fn append_assistant_audio_planned(
        &mut self,
        id: SegmentId,
        base_gain_db: f32,
        decision: Option<Arc<AssistantGainDecision>>,
        reference_eligible: bool,
        samples: impl Into<TtsAudioSamples>,
    ) {
        let samples = samples.into();
        // PANIC-AUDITED: every real producer (the daemon's TTS/cue rendering) emits whole frames
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
        self.assistant.enqueue_segment(
            id,
            samples,
            base_gain_db,
            peak_cap_gain_db,
            decision,
            reference_eligible,
        );
    }

    pub fn end_assistant_segment(&mut self, id: SegmentId) {
        self.ledger.end_segment(id, self.monotonic_ns);
        // If this segment's audio already drained (END arrived after playout),
        // its playout facts are waiting — commit the learned reference now.
        if let Some(pos) = self
            .drained_pending_end
            .iter()
            .position(|(pending_id, _)| *pending_id == id)
        {
            let (_, playback) = self.drained_pending_end.remove(pos);
            self.complete_assistant_reference(playback);
        }
    }

    pub fn step(&mut self) -> PeriodReport {
        self.prepare_period();
        self.commit_prepared_period()
    }

    pub fn step_with_content_period(&mut self, samples: &[ProgramSample]) -> PeriodReport {
        self.prepare_period_with_content(samples);
        self.commit_prepared_period()
    }

    pub fn prepare_period(&mut self) -> u32 {
        self.assert_no_prepared_period();
        self.content.read_period(&mut self.content_buf);
        self.prepare_from_buffered_content()
    }

    pub fn prepare_period_with_content(&mut self, samples: &[ProgramSample]) -> u32 {
        self.assert_no_prepared_period();
        // PANIC-AUDITED: content_buf is sized once at construction from the same period config
        assert_eq!(samples.len(), self.content_buf.len());
        self.content_buf.copy_from_slice(samples);
        self.prepare_from_buffered_content()
    }

    fn assert_no_prepared_period(&self) {
        // PANIC-AUDITED: the daemon's period loop calls prepare then commit in strict alternation
        assert!(
            !self.prepared_period_ready,
            "prepared output period was not committed"
        );
    }

    fn prepare_from_buffered_content(&mut self) -> u32 {
        if !self.content_meter_paused {
            // The shared (fan-in + outputd) loudness meter is i16; narrow into the
            // pre-sized scratch rather than change a cross-daemon signature. See
            // `content_meter_buf`. A length mismatch here cannot happen without a
            // resize of `content_buf` that skipped the scratch, and the meter is
            // observe-only, so degrade by SKIPPING this period's observation
            // rather than failing the audio path: a missed LUFS sample changes a
            // future TTS gain decision by a hair, while returning an error here
            // would silence the speaker.
            if narrow_period(&self.content_buf, &mut self.content_meter_buf).is_ok() {
                self.loudness
                    .observe_content_period(&self.content_meter_buf);
            }
        }
        // The loudness engine is passed in so the mix applies mute + live
        // re-gain per period (the volume context was drained before this).
        // Assistant segments that finish draining this period are reported in
        // `drained_playbacks` for reference learning.
        self.assistant.read_period_into(
            &mut self.assistant_buf,
            &mut self.segment_writes,
            &self.loudness,
            &mut self.drained_playbacks,
        );
        self.process_drained_playbacks();
        // Publish the loudness snapshot for STATUS once per period (after
        // observe + any completions this period). No-op without a sink.
        self.publish_loudness_snapshot();

        let mix_stats =
            mix_saturating(&self.content_buf, &self.assistant_buf, &mut self.output_buf);
        self.pending_clipped_samples = mix_stats.clipped_samples;
        self.prepared_period_ready = true;
        mix_stats.clipped_samples
    }

    pub fn commit_prepared_period(&mut self) -> PeriodReport {
        self.commit_prepared_period_with_dac_delay(0)
    }

    pub fn commit_prepared_period_with_dac_delay(&mut self, dac_delay_frames: u64) -> PeriodReport {
        // PANIC-AUDITED: the daemon's period loop calls prepare then commit in strict alternation
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
        // A barge-in discards the reply: no reference is learned from truncated
        // or in-flight speech.
        self.drained_playbacks.clear();
        self.drained_pending_end.clear();
        let events = self.ledger.flush_open_segments(self.monotonic_ns);
        self.ledger
            .prune_terminal_segments(DEFAULT_TERMINAL_SEGMENT_RETENTION);
        events
    }

    pub fn dac(&self) -> &FakeDacSink {
        &self.dac
    }

    pub fn output_period(&self) -> &[ProgramSample] {
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

    /// Accept an absolute speaker-volume context from the voice daemon.
    ///
    /// Returns whether the update won the boot-clock stale-stamp guard (the
    /// same guard fan-in uses). Post-DSP, the stored downstream_db is retained
    /// verbatim for observability; only its *use* inside the engine is zeroed
    /// (see MixStage) so this lane never inherits pre-DSP compensation.
    pub fn update_volume_context(&mut self, context: VolumeContext) -> bool {
        let accepted = self.loudness.update_volume_context(context);
        if !accepted {
            self.volume_context_rejected = self.volume_context_rejected.saturating_add(1);
        }
        accepted
    }

    /// Arm the shared STATUS snapshot cell (from the daemon's `TtsMetrics`).
    /// Once set, the audio loop publishes the loudness snapshot each period.
    pub fn set_loudness_sink(&mut self, sink: Arc<Mutex<TtsLoudnessSnapshot>>) {
        self.loudness_sink = Some(sink);
    }

    /// Publish the current engine snapshot into the shared STATUS cell. Called
    /// once per period; `try_lock` so the audio loop never blocks on a state
    /// read. No-op when no sink is armed (fake/developer path).
    fn publish_loudness_snapshot(&self) {
        let Some(sink) = &self.loudness_sink else {
            return;
        };
        if let Ok(mut slot) = sink.try_lock() {
            *slot = self
                .loudness
                .loudness_snapshot(self.volume_context_rejected);
        }
    }

    pub fn current_volume_context(&self) -> Option<VolumeContext> {
        self.loudness.current_volume_context()
    }

    pub fn clear_volume_context(&mut self) {
        self.loudness.clear_volume_context();
    }

    /// Arm the persistence sink for the learned quiet-room assistant reference.
    /// The daemon spawns the writer thread and hands its sender here; the fake
    /// developer/test path leaves it unset (learned references are dropped).
    pub fn set_assistant_reference_sink(&mut self, tx: Sender<HeldLoudnessReference>) {
        self.assistant_reference_tx = Some(tx);
    }

    /// Seed the learned quiet-room reference at startup (from disk). Validated
    /// and ±24 LU-clamped inside the engine.
    pub fn set_held_assistant_reference(&mut self, reference: Option<HeldLoudnessReference>) {
        self.loudness.set_held_assistant(reference);
    }

    #[cfg(test)]
    pub fn held_assistant_reference(&self) -> Option<HeldLoudnessReference> {
        self.loudness.held_assistant()
    }

    /// Pair each drained assistant segment with SEGMENT_END: complete the
    /// reference now if END already fired, else defer until it does.
    fn process_drained_playbacks(&mut self) {
        if self.drained_playbacks.is_empty() {
            return;
        }
        // Take the scratch out to satisfy the borrow checker, then restore it
        // (empty) so its capacity is reused across periods.
        let mut drained = std::mem::take(&mut self.drained_playbacks);
        for completion in drained.drain(..) {
            if self.ledger.is_ended(completion.id) {
                self.complete_assistant_reference(completion.playback);
            } else {
                self.drained_pending_end
                    .push((completion.id, completion.playback));
            }
        }
        self.drained_playbacks = drained;
    }

    /// Commit one completed segment's learned reference. `None` playback means
    /// the segment was disqualified (muted, or no volume context) and must not
    /// learn. The engine itself returns `None` for music-anchored references
    /// (only first-use / held-assistant quiet-room speech trains the offset).
    fn complete_assistant_reference(&mut self, playback: Option<SegmentPlayback>) {
        let Some(playback) = playback else {
            return;
        };
        let effective_gain_db = linear_to_db(playback.last_gain_linear);
        let reference = self.loudness.complete_assistant_segment_at(
            &playback.decision,
            effective_gain_db,
            playback.context,
        );
        if let (Some(reference), Some(tx)) = (reference, &self.assistant_reference_tx) {
            let _ = tx.send(reference); // writer gone = degrade to no persistence
        }
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

    /// One S16 sample at the program spine's scale.
    fn w(value: i16) -> ProgramSample {
        jasper_resampler::widen_i16_to_i32(value)
    }

    /// A period of PROGRAM (spine-width) samples, written as the S16 value it
    /// represents so these fixtures read the way they did before the widening.
    fn stereo(value: i16, frames: usize) -> Vec<ProgramSample> {
        vec![w(value); frames * (CHANNELS as usize)]
    }

    /// A period of ASSISTANT/TTS samples — the wire is S16 and stays S16.
    fn tts(value: i16, frames: usize) -> Vec<i16> {
        vec![value; frames * (CHANNELS as usize)]
    }

    /// One S16 LSB expressed at the spine's scale — the natural unit for "the
    /// wide path changed the resolution, not the level".
    const S16_LSB: ProgramSample = 1 << 16;

    /// Assert every sample of `period` is the spine representation of the value
    /// the pre-widening S16 path produced here, to within `lsbs` S16 LSBs.
    ///
    /// This is how the pre-widening expectations are CARRIED rather than
    /// replaced: the old literal is the claim, and the tolerance is the honest
    /// consequence of the change — the S16 path rounded each gained sample onto
    /// the S16 grid, so it can differ from the wide path by at most half an S16
    /// LSB per sample. A wrong gain, a wrong mix, or a wrong width all break it.
    fn assert_period_matches_s16_level(period: &[ProgramSample], old_s16: i16, lsbs: i64) {
        let want = w(old_s16) as i64;
        let bound = lsbs * (S16_LSB as i64);
        for (i, &sample) in period.iter().enumerate() {
            let delta = (sample as i64) - want;
            assert!(
                delta.abs() <= bound,
                "sample {i} = {sample} is {delta} spine LSBs from the widened \
                 pre-i32 expectation {old_s16} ({want}); bound is {bound}"
            );
        }
    }

    fn authorize_unmuted_assistant(core: &mut OutputCore) {
        assert!(core.update_volume_context(vc(-30.0, -30.0, -41.0, false, 1)));
    }

    #[test]
    fn step_mixes_content_and_assistant_and_advances_reference_sequence() {
        let mut core = OutputCore::new(4);
        authorize_unmuted_assistant(&mut core);
        core.push_content_period(stereo(10_000, 4));
        core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            tts(5000, 4),
        );

        let report = core.step();

        assert_eq!(report.reference_sequence, 0);
        assert_eq!(report.clipped_samples, 0);
        // The pre-widening path produced 10_706 here (content 10_000 plus the
        // assistant's 5000 at the decided -17 dB). Same level, finer grid.
        assert_period_matches_s16_level(&core.dac().periods[0], 10_706, 1);
    }

    #[test]
    fn step_reports_clipping_and_preserves_sequence_numbers() {
        let mut core = OutputCore::new(2);
        authorize_unmuted_assistant(&mut core);
        core.push_content_period(stereo(30_000, 2));
        core.enqueue_assistant_segment(None, SegmentKind::Cue, tts(30_000, 2));

        let first = core.step();
        let second = core.step();

        // Still all four samples: 30_000 of content leaves 2_767 S16 LSBs of
        // headroom and the assistant contributes ~4_238 at the decided -17 dB,
        // so the sum is past full scale at ANY width — the wide spine adds
        // resolution below the rail, not headroom above it.
        assert_eq!(first.clipped_samples, 4);
        assert_eq!(first.reference_sequence, 0);
        assert_eq!(second.reference_sequence, 1);
    }

    #[test]
    fn outputd_uses_loudness_decided_assistant_gain_before_mixing() {
        let mut core = OutputCore::new(2);
        authorize_unmuted_assistant(&mut core);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            tts(10_000, 2),
        );

        core.step();

        // With no observed content and no profile, the decision falls back
        // to the quiet-room envelope: baseline_lufs=target_lufs=-41.0,
        // fallback source_lufs=-24.0, so requested_gain=-17.0. The ordinary
        // music-relative +1.5 LU offset does not apply without music. The fixed
        // max-gain ceiling is gone; this helper now scales with the same
        // decided segment gain that the runtime TTS bridge uses.
        //
        // The decision itself is width-independent — it is f32 dB arithmetic over
        // the volume context and the observed content, and the content the meter
        // observes is the narrowed spine, bit-identical to the old i16 buffer for
        // widened S16 content. So the ledger gain is UNCHANGED, exactly.
        assert_eq!(core.ledger().segment(segment).gain, -17.0);
        assert_period_matches_s16_level(&core.dac().periods[0], 1413, 1);
    }

    #[test]
    fn step_with_content_period_uses_caller_supplied_content() {
        let mut core = OutputCore::new(2);

        let report = core.step_with_content_period(&stereo(123, 2));

        // No assistant audio: content reaches the sink EXACTLY, bit for bit.
        // This is the pipeline half of the transparency proof — see
        // `the_content_path_is_bit_transparent_from_spine_ingress_to_sink`.
        assert_eq!(report.frames_written, 2);
        assert_eq!(core.output_period(), stereo(123, 2).as_slice());
        assert_eq!(core.dac().periods[0], stereo(123, 2));
    }

    /// **The transparency proof, pipeline level.** An S16 content period widened
    /// at ingest passes observe → mix → output and narrows back to the SAME S16
    /// bytes, sample for sample, with no assistant audio.
    ///
    /// Together with
    /// `jasper_resampler::s16_widened_then_narrowed_is_bit_identical` (the
    /// primitive round trip) this is the whole argument that the i32 spine ships
    /// byte-identically on the S16-edge fleet: every stage between the two
    /// conversions is exercised here, and the conversions themselves are exact.
    /// The fixture covers full scale both signs, ±1 LSB, a ramp, and silence.
    #[test]
    fn the_content_path_is_bit_transparent_from_spine_ingress_to_sink() {
        let mut fixture: Vec<i16> = vec![i16::MAX, i16::MIN, 1, -1, 0, 0, 32_766, -32_767];
        fixture.extend((-32_768..=32_767).step_by(4_099).map(|v| v as i16));
        // Whole stereo frames only.
        if fixture.len() % 2 != 0 {
            fixture.push(0);
        }
        let frames = fixture.len() / (CHANNELS as usize);

        let mut core = OutputCore::new(frames as u32);
        // A volume context is present and unmuted, so nothing about this run is
        // a special "no assistant configured" shortcut — the mix stage runs.
        authorize_unmuted_assistant(&mut core);

        let mut wide = vec![0 as ProgramSample; fixture.len()];
        crate::types::widen_period(&fixture, &mut wide).unwrap();
        let report = core.step_with_content_period(&wide);

        assert_eq!(report.clipped_samples, 0, "unity content must not clip");
        let written = &core.dac().periods[0];
        assert_eq!(
            written, &wide,
            "the spine period must reach the sink intact"
        );

        let mut back = vec![0i16; fixture.len()];
        crate::types::narrow_period(written, &mut back).unwrap();
        assert_eq!(
            back, fixture,
            "S16 content in -> S16 edge out must be bit-identical"
        );
    }

    /// The loudness meter must actually SEE the spine's content, and see it as the
    /// same samples the pre-spine i16 buffer carried.
    ///
    /// This exists because a mutation audit found the gap: deleting the content
    /// observation from `prepare_from_buffered_content` outright left every other
    /// test in the crate green. Nothing pinned the wiring — and the wide spine
    /// added a NEW way for it to break, since the meter is now fed through a
    /// narrowing scratch rather than the buffer itself. A silently blind meter
    /// would mis-decide every assistant gain (too loud or too quiet replies) with
    /// no failing test and no log line.
    ///
    /// The assertion is equality against the shared meter fed the SAME audio at
    /// i16 directly, so it pins not just "the meter ran" but "the narrowing
    /// delivered the right samples".
    #[test]
    fn the_content_meter_observes_the_spine_content_as_its_i16_equivalent() {
        use crate::loudness::{AssistantLoudness, AssistantLoudnessConfig, MixStage};

        // The short-term window is 3 s; step a 1 kHz tone through enough periods
        // to fill it, then one more so the window is genuinely complete.
        const PERIOD: u32 = 4_800; // 100 ms
        const PERIODS: usize = 31;
        let tone = |i: usize| -> i16 {
            let t = (i as f64) / f64::from(SAMPLE_RATE);
            (8_000.0 * (2.0 * std::f64::consts::PI * 1_000.0 * t).sin()) as i16
        };

        let mut core = OutputCore::new(PERIOD);
        let mut reference = AssistantLoudness::new_with_stage(
            AssistantLoudnessConfig::default(),
            MixStage::PostDsp,
        );

        let mut frame = 0usize;
        for _ in 0..PERIODS {
            let mut i16_period = Vec::with_capacity((PERIOD as usize) * (CHANNELS as usize));
            for _ in 0..PERIOD {
                let s = tone(frame);
                i16_period.push(s);
                i16_period.push(s);
                frame += 1;
            }
            let mut spine = vec![0 as ProgramSample; i16_period.len()];
            crate::types::widen_period(&i16_period, &mut spine).unwrap();
            // The reference meter gets the ORIGINAL i16; the core gets the spine.
            reference.observe_content_period(&i16_period);
            core.step_with_content_period(&spine);
        }

        let observed = core
            .content_short_lufs()
            .expect("the content meter must have observed audible content");
        assert!(
            observed.is_finite() && observed < 0.0 && observed > -60.0,
            "implausible content loudness: {observed}"
        );
        assert_eq!(
            Some(observed),
            reference.content_short_lufs(),
            "the narrowed spine must meter identically to the same audio at i16"
        );
    }

    #[test]
    fn pending_assistant_frames_tracks_queue_depth() {
        let mut core = OutputCore::new(2);
        core.enqueue_assistant_segment(None, SegmentKind::Assistant, tts(100, 4));

        assert_eq!(core.pending_assistant_frames(), 4);

        core.step();

        assert_eq!(core.pending_assistant_frames(), 2);
    }

    #[test]
    fn prepare_does_not_publish_or_mark_playout_until_commit() {
        let mut core = OutputCore::new(2);
        authorize_unmuted_assistant(&mut core);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            tts(5000, 2),
        );

        let clipped = core.prepare_period_with_content(&stereo(100, 2));

        assert_eq!(clipped, 0);
        assert_period_matches_s16_level(core.output_period(), 806, 1);
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
        assert_period_matches_s16_level(&core.dac().periods[0], 806, 1);
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
            tts(1000, 96),
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
        core.enqueue_assistant_segment(None, SegmentKind::Assistant, tts(1000, 8));

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
        // cap (-3.0-(-18.0)=+15.0) leaves -11.0 unchanged. Had the context not
        // been cleared, baseline=-20.0 would drive the gain to the dynamic peak
        // cap instead of the quiet-room target.
        assert_eq!(core.ledger().segment(segment).gain, -11.0);
    }

    #[test]
    fn flush_clears_queued_assistant_audio_and_reports_played_ms() {
        let mut core = OutputCore::new(48);
        let segment = core.enqueue_assistant_segment(
            Some("item-1".to_string()),
            SegmentKind::Assistant,
            tts(1000, 96),
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
            Some(profile(-41.0, -3.0)),
        );
        assert_eq!(core.ledger().segment(id).gain, 0.0);
        core.append_assistant_audio_with_segment_gain(id, tts(8000, 12));
        core.end_assistant_segment(id);

        core.push_content_period(stereo(0, 4));
        core.step();
        // At 0 dB the gain is exactly unity, so the widened wire sample reaches
        // the sink EXACTLY — no tolerance needed, and the equality is the proof
        // that the widen-then-gain order does not perturb an unattenuated reply.
        assert!(
            core.dac().periods[0].iter().all(|&s| s == w(8000)),
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
            unmuted[0] > 0 && unmuted[0] < w(8000),
            "unmute ramps from silence: {}",
            unmuted[0]
        );
    }

    #[test]
    fn post_dsp_live_volume_change_regains_queued_speech() {
        use crate::loudness::{apply_gain, gain_db_to_linear, LIVE_VOLUME_RAMP_FRAMES};

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
            Some(profile(-41.0, -20.0)),
        );
        assert_eq!(core.ledger().segment(id).gain, 0.0);
        core.append_assistant_audio_with_segment_gain(id, tts(8000, (PERIOD as usize) * 2));
        core.end_assistant_segment(id);

        core.push_content_period(stereo(0, PERIOD as usize));
        core.step();
        assert!(core.dac().periods[0].iter().all(|&s| s == w(8000)));

        // Raise the room target +6 dB (envelope -41 → -35) mid-turn. Post-DSP
        // the downstream is ignored, so the mixer carries the full +6 dB.
        assert!(core.update_volume_context(vc(-30.0, -30.0, -35.0, false, 2)));
        core.push_content_period(stereo(0, PERIOD as usize));
        core.step();
        let regained = &core.dac().periods[1];
        // "No step discontinuity at the ramp start" — the bound is RE-DERIVED at
        // the new width, not the old i16 bound scaled. `GainRamp::retarget` sets
        // step_linear = (target - current) / LIVE_VOLUME_RAMP_FRAMES, so the
        // first frame's gain is 1 + (10^(6/20) - 1)/4800 ≈ 1 + 2.074e-4, and the
        // sample moves by w(8000) * 2.074e-4 ≈ 1.087e5 spine LSBs ≈ 1.66 S16
        // LSBs. Two S16 LSBs is that derivation plus rounding margin. (The old
        // i16 assertion's bound of 4 was the same derivation at 1/65536 the
        // scale: 8000 * 2.074e-4 ≈ 1.66, asserted as <= 4.)
        let one_ramp_step = (f64::from(gain_db_to_linear(6.0)) - 1.0)
            / f64::from(crate::loudness::LIVE_VOLUME_RAMP_FRAMES);
        let derived_first_frame_delta = (f64::from(w(8000)) * one_ramp_step).abs();
        assert!(
            derived_first_frame_delta < 2.0 * f64::from(S16_LSB),
            "the derivation itself must fit the bound: {derived_first_frame_delta}"
        );
        assert!(
            i64::from(regained[0] - w(8000)).abs() <= 2 * i64::from(S16_LSB),
            "ramp starts without a step discontinuity: {}",
            regained[0]
        );
        // The ramp completes on the period's LAST frame (PERIOD ==
        // LIVE_VOLUME_RAMP_FRAMES, and `next_frame` assigns the exact target when
        // the countdown hits zero), so the final sample is EXACTLY the target
        // gain — the old `<= 2` slack is not needed at any width.
        let expected = apply_gain(w(8000), gain_db_to_linear(6.0));
        let last = *regained.last().unwrap();
        assert_eq!(last, expected, "ramp reaches +6 dB louder exactly");
        // Independent arithmetic on the same claim, so the line above cannot pass
        // by both sides sharing one wrong gain: +6 dB of 8000 is ~15_962 in S16
        // terms.
        assert!(
            last > w(15_900) && last < w(16_020),
            "+6 dB of 8000 is ~15_962 in S16 terms: {last}"
        );
    }

    #[test]
    fn post_dsp_learns_and_persists_reference_on_segment_completion() {
        let mut core = OutputCore::new(4);
        let (tx, rx) = std::sync::mpsc::channel();
        core.set_assistant_reference_sink(tx);
        // Nonzero downstream that must be ignored when learning post-DSP.
        assert!(core.update_volume_context(vc(-30.0, -30.0, -41.0, false, 1)));
        core.prepare_assistant_context(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
        );
        // FirstUseFallback with source -25 → decided gain -16 dB; ample peak
        // headroom so nothing clamps.
        let id = core.start_assistant_segment_with_profile(
            Some("s".to_string()),
            SegmentKind::Assistant,
            Some(profile(-25.0, -20.0)),
        );
        assert_eq!(core.ledger().segment(id).gain, -16.0);
        core.append_assistant_audio_with_segment_gain(id, tts(8000, 4));
        // END arrives before the audio drains; the reference commits once the
        // audio finishes playing this period.
        core.end_assistant_segment(id);

        core.push_content_period(stereo(0, 4));
        core.step();

        let reference = rx.try_recv().expect("a completed reply learns a reference");
        // speaker = source(-25) + gain(-16) + downstream(ignored, 0) = -41,
        // which equals the envelope, so the calibration offset is ~0.
        assert!((reference.speaker_lufs - -41.0).abs() < 0.05);
        assert_eq!(reference.canonical_db, -30.0);
        assert!((reference.calibration_offset_lu - 0.0).abs() < 0.05);
        assert_eq!(core.held_assistant_reference(), Some(reference));
    }

    #[test]
    fn post_dsp_muted_reply_does_not_learn_a_reference() {
        let mut core = OutputCore::new(4);
        let (tx, rx) = std::sync::mpsc::channel();
        core.set_assistant_reference_sink(tx);
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
            Some(profile(-25.0, -20.0)),
        );
        core.append_assistant_audio_with_segment_gain(id, tts(8000, 8));
        core.end_assistant_segment(id);

        // First period unmuted, then mute before the reply finishes: a silenced
        // reply must never train the learned reference.
        core.push_content_period(stereo(0, 4));
        core.step();
        assert!(core.update_volume_context(vc(-30.0, -30.0, -41.0, true, 2)));
        core.push_content_period(stereo(0, 4));
        core.step();

        assert!(
            rx.try_recv().is_err(),
            "a muted reply must not learn a reference"
        );
        assert_eq!(core.held_assistant_reference(), None);
    }
}
