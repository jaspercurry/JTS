// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Shared assistant/content loudness policy.
//!
//! The control target is K-weighted loudness, not raw PCM RMS. The
//! implementation intentionally keeps the state small: two biquads per
//! channel, period-level rolling windows, and no heap churn in the audio
//! loop beyond the bounded deques.

use std::collections::VecDeque;

use crate::{
    assistant_profile_confidence_in_range, assistant_profile_db_in_range, VolumeContext, CHANNELS,
};
pub use crate::{AssistantProfile, SegmentKind};

pub const SAMPLE_RATE: u32 = 48_000;
pub const DEFAULT_TTS_GAIN_DB: f32 = 0.0;
pub const MIN_TTS_GAIN_DB: f32 = -60.0;

const FULL_SCALE: f64 = 32768.0;
const FULL_SCALE_SQ: f64 = FULL_SCALE * FULL_SCALE;
const BS1770_OFFSET_DB: f64 = -0.691;
const MOMENTARY_FRAMES: u64 = (SAMPLE_RATE as u64) * 400 / 1000;
const SHORT_TERM_FRAMES: u64 = (SAMPLE_RATE as u64) * 3;
const CONTENT_ANCHOR_FRAMES: u64 = (SAMPLE_RATE as u64) * 12;
const MAX_SAFE_ASSISTANT_CALIBRATION_LU: f32 = 24.0;

#[derive(Debug, Clone, Copy)]
pub struct AssistantLoudnessConfig {
    pub assistant_offset_lu: f32,
    pub max_peak_dbfs: f32,
    pub fallback_source_lufs: f32,
    pub fallback_source_peak_dbfs: f32,
    pub default_tts_envelope_lufs: f32,
    pub content_silence_lufs: f32,
    pub held_content_ttl_sec: f32,
    pub assistant_envelope_offset_limit_lu: f32,
}

impl Default for AssistantLoudnessConfig {
    fn default() -> Self {
        Self {
            assistant_offset_lu: 1.5,
            max_peak_dbfs: -3.0,
            fallback_source_lufs: -24.0,
            fallback_source_peak_dbfs: -6.0,
            default_tts_envelope_lufs: -41.0,
            content_silence_lufs: -60.0,
            held_content_ttl_sec: 600.0,
            assistant_envelope_offset_limit_lu: 8.0,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct AssistantContext {
    pub provider: String,
    pub model: String,
    pub voice: String,
    pub baseline_lufs: Option<f32>,
    pub tts_envelope_lufs: f32,
    pub volume_context: Option<VolumeContext>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReferenceKind {
    LiveContent,
    HeldContent,
    HeldAssistant,
    FirstUseFallback,
}

impl ReferenceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::LiveContent => "live_content",
            Self::HeldContent => "held_content",
            Self::HeldAssistant => "held_assistant",
            Self::FirstUseFallback => "first_use_fallback",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct HeldLoudnessReference {
    /// Achieved loudness after downstream Camilla attenuation.
    pub speaker_lufs: f32,
    /// Canonical user-volume dB when ``speaker_lufs`` was achieved.
    pub canonical_db: f32,
    /// Bounded correction to the deterministic no-music envelope.
    pub calibration_offset_lu: f32,
}

/// Where in the output chain the assistant lane this engine governs is mixed.
///
/// The one structural difference between fan-in (solo/leader, pre-DSP) and
/// outputd (passive grouped follower, post-DSP) is whether CamillaDSP's gain
/// is *downstream* of the assistant lane. Fan-in mixes assistant audio
/// **before** CamillaDSP, so ``VolumeContext::downstream_db`` (Camilla's gain)
/// genuinely attenuates the assistant lane and the mixer must pre-compensate
/// for it. Outputd mixes assistant audio **after** CamillaDSP, so nothing
/// applies volume after the mix — the effective downstream attenuation of the
/// assistant lane is structurally **zero**. Every difference between the two
/// daemons' loudness behaviour collapses to that single fact, so it is the
/// single parameter this engine takes: `PostDsp` treats `downstream_db` as
/// 0.0 wherever the code converts between mixer-lane loudness and achieved
/// speaker loudness (the "subtract/add downstream" positions), while still
/// honouring `canonical_db`, `tts_envelope_lufs`, and `muted` unchanged.
/// `PreDsp` (the default) is byte-identical to the pre-`MixStage` engine.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum MixStage {
    /// Assistant audio is mixed before CamillaDSP (fan-in solo/leader). The
    /// downstream Camilla gain attenuates the assistant lane and is
    /// compensated for.
    #[default]
    PreDsp,
    /// Assistant audio is mixed after CamillaDSP (outputd passive grouped
    /// follower). No stage applies volume after the mix, so `downstream_db`
    /// is treated as 0.0 in every mixer-to-speaker conversion.
    PostDsp,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AssistantGainDecision {
    pub provider: Option<String>,
    pub model: Option<String>,
    pub voice: Option<String>,
    pub calibrated: bool,
    pub profile_confidence: f32,
    pub baseline_lufs: f32,
    pub target_lufs: f32,
    pub source_lufs: f32,
    pub source_peak_dbfs: f32,
    pub requested_gain_db: f32,
    pub peak_cap_gain_db: f32,
    pub final_gain_db: f32,
    pub clamp_reason: &'static str,
    pub reference_kind: ReferenceKind,
    pub target_speaker_lufs: Option<f32>,
    pub envelope_offset_lu: Option<f32>,
    pub volume_context: Option<VolumeContext>,
}

pub struct AssistantLoudness {
    config: AssistantLoudnessConfig,
    mix_stage: MixStage,
    content: KWeightedWindow,
    pending_context: Option<AssistantContext>,
    last_decision: Option<AssistantGainDecision>,
    current_volume_context: Option<VolumeContext>,
    held_content: Option<HeldLoudnessReference>,
    held_assistant: Option<HeldLoudnessReference>,
    content_currently_audible: bool,
    content_audible_frames: u64,
    content_silence_frames: u64,
}

impl AssistantLoudness {
    /// Construct a pre-DSP engine (fan-in solo/leader). Byte-identical to the
    /// pre-`MixStage` engine — the default.
    pub fn new(config: AssistantLoudnessConfig) -> Self {
        Self::new_with_stage(config, MixStage::PreDsp)
    }

    /// Construct an engine for an explicit mix stage. `PostDsp` is the
    /// outputd passive-follower path: `downstream_db` is treated as 0.0 in
    /// every mixer-to-speaker conversion (see [`MixStage`]).
    pub fn new_with_stage(config: AssistantLoudnessConfig, mix_stage: MixStage) -> Self {
        Self {
            config,
            mix_stage,
            content: KWeightedWindow::new(CONTENT_ANCHOR_FRAMES),
            pending_context: None,
            last_decision: None,
            current_volume_context: None,
            held_content: None,
            held_assistant: None,
            content_currently_audible: false,
            content_audible_frames: 0,
            content_silence_frames: 0,
        }
    }

    /// The downstream attenuation to apply for `context` at this mix stage.
    ///
    /// Pre-DSP, CamillaDSP sits after the assistant mixer, so its gain
    /// (`downstream_db`) genuinely attenuates the assistant lane and every
    /// mixer-to-speaker conversion must account for it. Post-DSP, nothing
    /// applies volume after the mix, so the effective downstream attenuation
    /// is structurally 0.0 — the single fact that distinguishes the two
    /// daemons. The stored wire value is left untouched; only its *use* here
    /// is stage-aware, so `/state` still reports the honest Camilla gain.
    fn effective_downstream_db(&self, context: &VolumeContext) -> f32 {
        match self.mix_stage {
            MixStage::PreDsp => context.downstream_db,
            MixStage::PostDsp => 0.0,
        }
    }

    pub fn mix_stage(&self) -> MixStage {
        self.mix_stage
    }

    pub fn observe_content_period(&mut self, samples: &[i16]) {
        let period_lufs = self.content.push_interleaved(samples);
        self.content_currently_audible = period_lufs
            .is_some_and(|value| value.is_finite() && value >= self.config.content_silence_lufs);
        if !self.content_currently_audible {
            self.content_audible_frames = 0;
            self.content_silence_frames = self
                .content_silence_frames
                .saturating_add((samples.len() / (CHANNELS as usize)) as u64);
            let expiry_frames =
                (self.config.held_content_ttl_sec.max(0.0) * (SAMPLE_RATE as f32)) as u64;
            if self.content_silence_frames >= expiry_frames {
                self.held_content = None;
            }
            return;
        }
        self.content_silence_frames = 0;
        self.content_audible_frames = self
            .content_audible_frames
            .saturating_add((samples.len() / (CHANNELS as usize)) as u64);
        if self.content_audible_frames < SHORT_TERM_FRAMES {
            return;
        }
        let (Some(context), Some(content_lufs)) =
            (self.current_volume_context, self.content.full_short_lufs())
        else {
            return;
        };
        if context.muted {
            return;
        }
        // `speaker_lufs` is the observed content loudness converted to the
        // achieved-at-the-speaker scale. Pre-DSP the mixer's output is later
        // attenuated by `downstream_db`, so add it back; post-DSP the content
        // outputd observes is ALREADY at speaker level (post-Camilla), so the
        // effective downstream is 0.0 and the two representations coincide.
        // This must stay consistent with the HeldContent branch of
        // `decide_gain`, which subtracts the same effective downstream — the
        // round trip cancels, so a stale Camilla gain can never inflate the
        // held-content target post-DSP (the double-compensation this fix
        // exists to prevent).
        self.held_content = Some(HeldLoudnessReference {
            speaker_lufs: content_lufs + self.effective_downstream_db(&context),
            canonical_db: context.canonical_db,
            calibration_offset_lu: 0.0,
        });
    }

    pub fn prepare_context(
        &mut self,
        provider: String,
        model: String,
        voice: String,
        tts_envelope_lufs: f32,
    ) {
        self.prepare_context_with_volume(provider, model, voice, tts_envelope_lufs, None);
    }

    pub fn prepare_context_with_volume(
        &mut self,
        provider: String,
        model: String,
        voice: String,
        tts_envelope_lufs: f32,
        volume_context: Option<VolumeContext>,
    ) {
        if let Some(context) = volume_context {
            self.update_volume_context(context);
        }
        let baseline_lufs = if self.content_is_qualified() {
            self.observed_content_lufs()
        } else {
            None
        };
        self.pending_context = Some(AssistantContext {
            provider,
            model,
            voice,
            baseline_lufs,
            tts_envelope_lufs,
            volume_context: self.current_volume_context,
        });
    }

    /// Accept an absolute context unless a newer boot-clock stamp already won.
    pub fn update_volume_context(&mut self, context: VolumeContext) -> bool {
        if context.canonical_db.is_finite()
            && context.downstream_db.is_finite()
            && context.tts_envelope_lufs.is_finite()
            && self.current_volume_context.map_or(true, |current| {
                context.stamp_boot_ns >= current.stamp_boot_ns
            })
        {
            self.current_volume_context = Some(context);
            true
        } else {
            false
        }
    }

    pub fn set_held_assistant(&mut self, reference: Option<HeldLoudnessReference>) {
        self.held_assistant = reference.filter(|value| {
            value.speaker_lufs.is_finite()
                && value.canonical_db.is_finite()
                && value.calibration_offset_lu.is_finite()
                && value.calibration_offset_lu.abs() <= MAX_SAFE_ASSISTANT_CALIBRATION_LU
        });
    }

    pub fn held_assistant(&self) -> Option<HeldLoudnessReference> {
        self.held_assistant
    }

    pub fn held_content(&self) -> Option<HeldLoudnessReference> {
        self.held_content
    }

    pub fn current_volume_context(&self) -> Option<VolumeContext> {
        self.current_volume_context
    }

    pub fn clear_context(&mut self) {
        self.pending_context = None;
    }

    pub fn decide_gain(
        &mut self,
        _kind: SegmentKind,
        _fallback_gain_db: f32,
        profile: Option<AssistantProfile>,
    ) -> AssistantGainDecision {
        let context = self.pending_context.clone();
        let observed_baseline_lufs =
            context
                .as_ref()
                .and_then(|ctx| ctx.baseline_lufs)
                .or_else(|| {
                    if self.content_is_qualified() {
                        self.observed_content_lufs()
                    } else {
                        None
                    }
                });
        let volume_context = context
            .as_ref()
            .and_then(|ctx| ctx.volume_context)
            .or(self.current_volume_context);
        let (baseline_lufs, target_lufs, target_speaker_lufs, reference_kind, envelope_offset_lu) =
            if let Some(baseline) = observed_baseline_lufs {
                let target = baseline + self.config.assistant_offset_lu;
                (
                    baseline,
                    target,
                    volume_context.map(|ctx| target + self.effective_downstream_db(&ctx)),
                    ReferenceKind::LiveContent,
                    None,
                )
            } else if let (Some(reference), Some(current)) = (self.held_content, volume_context) {
                let target_speaker = reference.speaker_lufs
                    + (current.canonical_db - reference.canonical_db)
                    + self.config.assistant_offset_lu;
                let target = target_speaker - self.effective_downstream_db(&current);
                (
                    target - self.config.assistant_offset_lu,
                    target,
                    Some(target_speaker),
                    ReferenceKind::HeldContent,
                    None,
                )
            } else if let (Some(reference), Some(current)) = (self.held_assistant, volume_context) {
                // Quiet-room speech follows one product curve at every knob
                // position. The last achieved turn contributes only a bounded
                // calibration correction; it never becomes a second curve.
                let baseline = current.tts_envelope_lufs;
                let offset = reference.calibration_offset_lu.clamp(
                    -self.config.assistant_envelope_offset_limit_lu,
                    self.config.assistant_envelope_offset_limit_lu,
                );
                let target_speaker = baseline + offset;
                let target = target_speaker - self.effective_downstream_db(&current);
                (
                    baseline,
                    target,
                    Some(target_speaker),
                    ReferenceKind::HeldAssistant,
                    Some(offset),
                )
            } else {
                let baseline = volume_context.map_or_else(
                    || {
                        context
                            .as_ref()
                            .map_or(self.config.default_tts_envelope_lufs, |ctx| {
                                ctx.tts_envelope_lufs
                            })
                    },
                    |current| current.tts_envelope_lufs,
                );
                let target_speaker = baseline;
                let target = volume_context.map_or(target_speaker, |current| {
                    target_speaker - self.effective_downstream_db(&current)
                });
                (
                    baseline,
                    target,
                    volume_context.map(|_| target_speaker),
                    ReferenceKind::FirstUseFallback,
                    Some(0.0),
                )
            };
        let confidence = profile.as_ref().map_or(0.0, |p| {
            if assistant_profile_confidence_in_range(p.confidence) {
                p.confidence
            } else {
                0.0
            }
        });
        let profile_source_lufs = profile
            .as_ref()
            .and_then(|p| p.source_lufs)
            .filter(|v| assistant_profile_db_in_range(*v));
        let source_lufs = profile_source_lufs.unwrap_or(self.config.fallback_source_lufs);
        let profile_source_peak_dbfs = profile
            .as_ref()
            .and_then(|p| p.source_peak_dbfs)
            .filter(|v| assistant_profile_db_in_range(*v));
        let source_peak_dbfs =
            profile_source_peak_dbfs.unwrap_or(self.config.fallback_source_peak_dbfs);
        let requested_gain = target_lufs - source_lufs;
        let peak_cap_gain = self.config.max_peak_dbfs - source_peak_dbfs;
        let limited_gain = requested_gain.min(peak_cap_gain);
        let final_gain = sanitize_tts_gain_db(limited_gain);
        let clamp_reason = if final_gain != limited_gain {
            "gain_floor"
        } else if limited_gain != requested_gain {
            "peak_cap"
        } else if profile_source_lufs.is_none() {
            "fallback_profile"
        } else {
            "target"
        };
        let decision = AssistantGainDecision {
            provider: profile
                .as_ref()
                .map(|p| p.provider.clone())
                .or_else(|| context.as_ref().map(|ctx| ctx.provider.clone())),
            model: profile
                .as_ref()
                .map(|p| p.model.clone())
                .or_else(|| context.as_ref().map(|ctx| ctx.model.clone())),
            voice: profile
                .as_ref()
                .map(|p| p.voice.clone())
                .or_else(|| context.as_ref().map(|ctx| ctx.voice.clone())),
            calibrated: profile_source_lufs.is_some(),
            profile_confidence: confidence,
            baseline_lufs,
            target_lufs,
            source_lufs,
            source_peak_dbfs,
            requested_gain_db: requested_gain,
            peak_cap_gain_db: peak_cap_gain,
            final_gain_db: final_gain,
            clamp_reason,
            reference_kind,
            target_speaker_lufs,
            envelope_offset_lu,
            volume_context,
        };
        self.last_decision = Some(decision.clone());
        decision
    }

    pub fn content_short_lufs(&self) -> Option<f32> {
        self.content.short_lufs()
    }

    pub fn content_anchor_lufs(&self) -> Option<f32> {
        self.content.anchor_lufs()
    }

    pub fn last_decision(&self) -> Option<&AssistantGainDecision> {
        self.last_decision.as_ref()
    }

    /// Build the STATUS `assistant_loudness` snapshot directly from engine
    /// state (outputd's path; fan-in derives an equivalent snapshot from its
    /// seqlock'd atomics). `volume_context_rejected` is the daemon-owned reject
    /// counter (the engine does not count rejections). Decision-derived fields
    /// are `None` until the first decision, matching fan-in's gating.
    pub fn loudness_snapshot(&self, volume_context_rejected: u64) -> TtsLoudnessSnapshot {
        let decision = self.last_decision.as_ref();
        TtsLoudnessSnapshot {
            content_short_lufs: self.content.short_lufs().map(|v| v as f64),
            content_anchor_lufs: self.content.anchor_lufs().map(|v| v as f64),
            decision_seen: decision.is_some(),
            calibrated: decision.is_some_and(|d| d.calibrated),
            profile_confidence: decision.map_or(0.0, |d| d.profile_confidence as f64),
            baseline_lufs: decision.map(|d| d.baseline_lufs as f64),
            target_lufs: decision.map(|d| d.target_lufs as f64),
            source_lufs: decision.map(|d| d.source_lufs as f64),
            source_peak_dbfs: decision.map(|d| d.source_peak_dbfs as f64),
            requested_gain_db: decision.map(|d| d.requested_gain_db as f64),
            peak_cap_gain_db: decision.map(|d| d.peak_cap_gain_db as f64),
            final_gain_db: decision.map(|d| d.final_gain_db as f64),
            target_speaker_lufs: decision
                .and_then(|d| d.target_speaker_lufs)
                .map(|v| v as f64),
            envelope_offset_lu: decision
                .and_then(|d| d.envelope_offset_lu)
                .map(|v| v as f64),
            reference_kind: decision.map(|d| d.reference_kind.as_str()),
            volume_context: self.current_volume_context,
            volume_context_rejected,
            held_content: self.held_content,
            held_assistant: self.held_assistant,
        }
    }

    /// Residual mixer gain needed after an absolute user-volume update.
    ///
    /// If Camilla already carried the user change, canonical and downstream
    /// deltas cancel to zero. Push-mode sources leave downstream at 0 dB, so
    /// fan-in carries the canonical delta while TTS is active.
    pub fn live_gain_delta_db(&self, decision: &AssistantGainDecision) -> f32 {
        let (Some(initial), Some(current)) = (decision.volume_context, self.current_volume_context)
        else {
            return 0.0;
        };
        // Post-DSP the downstream (Camilla) delta is not applied to the
        // assistant lane at all, so it cannot cancel a canonical/envelope
        // change — the mixer itself must carry the full user delta. Zeroing
        // the effective downstream at both endpoints makes that term vanish.
        let downstream_delta =
            self.effective_downstream_db(&current) - self.effective_downstream_db(&initial);
        match decision.reference_kind {
            ReferenceKind::LiveContent | ReferenceKind::HeldContent => {
                (current.canonical_db - initial.canonical_db) - downstream_delta
            }
            ReferenceKind::HeldAssistant | ReferenceKind::FirstUseFallback => {
                (current.tts_envelope_lufs - initial.tts_envelope_lufs) - downstream_delta
            }
        }
    }

    /// Capture only completed assistant speech as the no-music reference.
    /// Cues and chirps never call this method.
    pub fn complete_assistant_segment(
        &mut self,
        decision: &AssistantGainDecision,
        effective_gain_db: f32,
    ) -> Option<HeldLoudnessReference> {
        let current = self.current_volume_context?;
        self.complete_assistant_segment_at(decision, effective_gain_db, current)
    }

    pub fn complete_assistant_segment_at(
        &mut self,
        decision: &AssistantGainDecision,
        effective_gain_db: f32,
        playout_context: VolumeContext,
    ) -> Option<HeldLoudnessReference> {
        if !matches!(
            decision.reference_kind,
            ReferenceKind::FirstUseFallback | ReferenceKind::HeldAssistant
        ) {
            return None;
        }
        if playout_context.muted || !effective_gain_db.is_finite() {
            return None;
        }
        let speaker_lufs = decision.source_lufs
            + effective_gain_db
            + self.effective_downstream_db(&playout_context);
        let deterministic_target = playout_context.tts_envelope_lufs;
        let reference = HeldLoudnessReference {
            speaker_lufs,
            canonical_db: playout_context.canonical_db,
            calibration_offset_lu: (speaker_lufs - deterministic_target).clamp(
                -self.config.assistant_envelope_offset_limit_lu,
                self.config.assistant_envelope_offset_limit_lu,
            ),
        };
        if !reference.speaker_lufs.is_finite() {
            return None;
        }
        self.held_assistant = Some(reference);
        Some(reference)
    }

    fn observed_content_lufs(&self) -> Option<f32> {
        if !self.content_is_qualified() {
            return None;
        }
        self.content
            .short_lufs()
            .or_else(|| self.content.anchor_lufs())
            .filter(|v| v.is_finite() && *v >= self.config.content_silence_lufs)
    }

    fn content_is_qualified(&self) -> bool {
        self.content_currently_audible && self.content_audible_frames >= SHORT_TERM_FRAMES
    }
}

pub fn sanitize_tts_gain_db(gain_db: f32) -> f32 {
    if !gain_db.is_finite() {
        return MIN_TTS_GAIN_DB;
    }
    gain_db.max(MIN_TTS_GAIN_DB)
}

pub fn gain_db_to_linear(gain_db: f32) -> f32 {
    10.0_f32.powf(sanitize_tts_gain_db(gain_db) / 20.0)
}

pub fn linear_to_db(gain_linear: f32) -> f32 {
    if gain_linear <= 0.0 {
        MIN_TTS_GAIN_DB
    } else {
        20.0 * gain_linear.log10()
    }
}

pub fn apply_gain_i16(sample: i16, gain_linear: f32) -> i16 {
    let scaled = (sample as f32) * gain_linear;
    scaled.round().clamp(i16::MIN as f32, i16::MAX as f32) as i16
}

/// The one snapshot of assistant-loudness state both daemons surface under
/// `tts.assistant_loudness` in STATUS. fan-in derives it from its seqlock'd
/// atomics; outputd derives it directly from the engine. The struct and its
/// renderer ([`render_assistant_loudness`]) live here so the two daemons'
/// `/state` shapes cannot drift — the same "one wire vocabulary, per-daemon
/// values" rule the FLUSH_SYNC ack follows.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct TtsLoudnessSnapshot {
    pub content_short_lufs: Option<f64>,
    pub content_anchor_lufs: Option<f64>,
    pub decision_seen: bool,
    pub calibrated: bool,
    pub profile_confidence: f64,
    pub baseline_lufs: Option<f64>,
    pub target_lufs: Option<f64>,
    pub source_lufs: Option<f64>,
    pub source_peak_dbfs: Option<f64>,
    pub requested_gain_db: Option<f64>,
    pub peak_cap_gain_db: Option<f64>,
    pub final_gain_db: Option<f64>,
    pub target_speaker_lufs: Option<f64>,
    pub envelope_offset_lu: Option<f64>,
    pub reference_kind: Option<&'static str>,
    pub volume_context: Option<VolumeContext>,
    pub volume_context_rejected: u64,
    pub held_content: Option<HeldLoudnessReference>,
    pub held_assistant: Option<HeldLoudnessReference>,
}

/// Canonical top-level JSON keys of the `tts.assistant_loudness` STATUS object.
///
/// Both fan-in and outputd render this object through
/// [`render_assistant_loudness`], and each daemon's state test asserts the
/// rendered object carries every key here — so the two `/state` surfaces cannot
/// drift, the same contract [`crate::FLUSH_SYNC_ACK_KEYS`] enforces for the ack.
pub const ASSISTANT_LOUDNESS_STATUS_KEYS: &[&str] = &[
    "content_short_lufs",
    "content_anchor_lufs",
    "decision_seen",
    "calibrated",
    "profile_confidence",
    "baseline_lufs",
    "target_lufs",
    "source_lufs",
    "source_peak_dbfs",
    "requested_gain_db",
    "peak_cap_gain_db",
    "final_gain_db",
    "target_speaker_lufs",
    "envelope_offset_lu",
    "reference_kind",
    "volume_context",
    "volume_context_rejected",
    "held_content",
    "held_assistant",
];

/// Canonical JSON keys of the nested `volume_context` object (when present).
pub const ASSISTANT_LOUDNESS_VOLUME_CONTEXT_KEYS: &[&str] = &[
    "canonical_db",
    "downstream_db",
    "tts_envelope_lufs",
    "muted",
    "stamp_boot_ns",
];

/// Canonical JSON keys of a nested held-reference object (`held_content` /
/// `held_assistant`, when present).
pub const ASSISTANT_LOUDNESS_REFERENCE_KEYS: &[&str] =
    &["speaker_lufs", "canonical_db", "calibration_offset_lu"];

/// Render `snapshot` as the `tts.assistant_loudness` JSON object (including the
/// enclosing braces) into `buf`. Both daemons call this so their STATUS shapes
/// are byte-identical — the single writer of these keys.
pub fn render_assistant_loudness(buf: &mut String, snapshot: &TtsLoudnessSnapshot) {
    buf.push('{');
    push_json_f64_opt(buf, "content_short_lufs", snapshot.content_short_lufs, 1);
    buf.push(',');
    push_json_f64_opt(buf, "content_anchor_lufs", snapshot.content_anchor_lufs, 1);
    buf.push(',');
    push_json_bool(buf, "decision_seen", snapshot.decision_seen);
    buf.push(',');
    push_json_bool(buf, "calibrated", snapshot.calibrated);
    buf.push(',');
    push_json_f64(buf, "profile_confidence", snapshot.profile_confidence, 2);
    buf.push(',');
    push_json_f64_opt(buf, "baseline_lufs", snapshot.baseline_lufs, 1);
    buf.push(',');
    push_json_f64_opt(buf, "target_lufs", snapshot.target_lufs, 1);
    buf.push(',');
    push_json_f64_opt(buf, "source_lufs", snapshot.source_lufs, 1);
    buf.push(',');
    push_json_f64_opt(buf, "source_peak_dbfs", snapshot.source_peak_dbfs, 1);
    buf.push(',');
    push_json_f64_opt(buf, "requested_gain_db", snapshot.requested_gain_db, 1);
    buf.push(',');
    push_json_f64_opt(buf, "peak_cap_gain_db", snapshot.peak_cap_gain_db, 1);
    buf.push(',');
    push_json_f64_opt(buf, "final_gain_db", snapshot.final_gain_db, 1);
    buf.push(',');
    push_json_f64_opt(buf, "target_speaker_lufs", snapshot.target_speaker_lufs, 1);
    buf.push(',');
    push_json_f64_opt(buf, "envelope_offset_lu", snapshot.envelope_offset_lu, 1);
    buf.push(',');
    match snapshot.reference_kind {
        Some(kind) => {
            buf.push_str(r#""reference_kind":""#);
            buf.push_str(kind);
            buf.push('"');
        }
        None => buf.push_str(r#""reference_kind":null"#),
    }
    buf.push(',');
    buf.push_str(r#""volume_context":"#);
    match snapshot.volume_context {
        Some(context) => {
            buf.push('{');
            push_json_f64(buf, "canonical_db", context.canonical_db as f64, 1);
            buf.push(',');
            push_json_f64(buf, "downstream_db", context.downstream_db as f64, 1);
            buf.push(',');
            push_json_f64(
                buf,
                "tts_envelope_lufs",
                context.tts_envelope_lufs as f64,
                1,
            );
            buf.push(',');
            push_json_bool(buf, "muted", context.muted);
            buf.push(',');
            push_json_u64(buf, "stamp_boot_ns", context.stamp_boot_ns);
            buf.push('}');
        }
        None => buf.push_str("null"),
    }
    buf.push(',');
    push_json_u64(
        buf,
        "volume_context_rejected",
        snapshot.volume_context_rejected,
    );
    buf.push(',');
    push_json_reference(buf, "held_content", snapshot.held_content);
    buf.push(',');
    push_json_reference(buf, "held_assistant", snapshot.held_assistant);
    buf.push('}');
}

fn push_json_key(buf: &mut String, key: &str) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str("\":");
}

fn push_json_bool(buf: &mut String, key: &str, value: bool) {
    push_json_key(buf, key);
    buf.push_str(if value { "true" } else { "false" });
}

fn push_json_u64(buf: &mut String, key: &str, value: u64) {
    push_json_key(buf, key);
    buf.push_str(&value.to_string());
}

fn push_json_f64(buf: &mut String, key: &str, value: f64, decimals: usize) {
    push_json_key(buf, key);
    buf.push_str(&format!("{value:.decimals$}"));
}

fn push_json_f64_opt(buf: &mut String, key: &str, value: Option<f64>, decimals: usize) {
    push_json_key(buf, key);
    match value {
        Some(value) => buf.push_str(&format!("{value:.decimals$}")),
        None => buf.push_str("null"),
    }
}

fn push_json_reference(buf: &mut String, key: &str, reference: Option<HeldLoudnessReference>) {
    push_json_key(buf, key);
    let Some(reference) = reference else {
        buf.push_str("null");
        return;
    };
    buf.push('{');
    push_json_f64(buf, "speaker_lufs", reference.speaker_lufs as f64, 1);
    buf.push(',');
    push_json_f64(buf, "canonical_db", reference.canonical_db as f64, 1);
    buf.push(',');
    push_json_f64(
        buf,
        "calibration_offset_lu",
        reference.calibration_offset_lu as f64,
        1,
    );
    buf.push('}');
}

/// Frames over which a live gain change ramps to its new target (100 ms).
pub const LIVE_VOLUME_RAMP_FRAMES: u32 = SAMPLE_RATE / 10;

/// A per-frame linear gain ramp shared by the fan-in and outputd mix loops.
///
/// The first target snaps in (no ramp from zero); later targets glide over
/// [`LIVE_VOLUME_RAMP_FRAMES`] so a mid-turn volume change is inaudible.
/// `force_silent` collapses to zero and re-arms so the next non-muted target
/// always ramps back up from silence — even at the gain floor. This is the
/// extraction of fan-in's private ramp so outputd's post-DSP mix loop applies
/// live re-gain and mute identically; the ramp math is now unit-tested once
/// here in the hardware-free crate.
#[derive(Debug, Default, Clone, Copy)]
pub struct GainRamp {
    initialized: bool,
    current_linear: f32,
    target_linear: f32,
    step_linear: f32,
    remaining_frames: u32,
    target_db: f32,
}

impl GainRamp {
    pub fn new() -> Self {
        Self::default()
    }

    /// Force the ramp to silence and re-arm, so the next non-muted `retarget`
    /// always ramps up from zero (mute → unmute never snaps loud).
    pub fn force_silent(&mut self) {
        self.initialized = true;
        self.current_linear = 0.0;
        self.target_linear = 0.0;
        self.step_linear = 0.0;
        self.remaining_frames = 0;
        // NaN forces the next `retarget` through, even when the new target is
        // the -60 dB floor, so unmute always ramps from zero.
        self.target_db = f32::NAN;
    }

    /// Point the ramp at a new target. The first ever target snaps in; a
    /// changed target glides over `LIVE_VOLUME_RAMP_FRAMES`.
    pub fn retarget(&mut self, target_db: f32) {
        if self.initialized && (target_db - self.target_db).abs() < 0.01 {
            return;
        }
        let target_linear = gain_db_to_linear(target_db);
        if !self.initialized {
            self.initialized = true;
            self.current_linear = target_linear;
            self.target_linear = target_linear;
            self.target_db = target_db;
            return;
        }
        self.target_linear = target_linear;
        self.target_db = target_db;
        self.remaining_frames = LIVE_VOLUME_RAMP_FRAMES;
        self.step_linear = (target_linear - self.current_linear) / (LIVE_VOLUME_RAMP_FRAMES as f32);
    }

    /// Advance one frame and return the current linear gain.
    pub fn next_frame(&mut self) -> f32 {
        if self.remaining_frames > 0 {
            self.current_linear += self.step_linear;
            self.remaining_frames -= 1;
            if self.remaining_frames == 0 {
                self.current_linear = self.target_linear;
            }
        }
        self.current_linear
    }

    /// The current linear gain without advancing (diagnostics/tests).
    pub fn current_linear(&self) -> f32 {
        self.current_linear
    }
}

struct KWeightedWindow {
    filters: [KWeightingChannel; CHANNELS as usize],
    periods: VecDeque<EnergyPeriod>,
    max_frames: u64,
    total_frames: u64,
    total_energy: f64,
}

impl KWeightedWindow {
    fn new(max_frames: u64) -> Self {
        Self {
            filters: [KWeightingChannel::new(), KWeightingChannel::new()],
            periods: VecDeque::new(),
            max_frames,
            total_frames: 0,
            total_energy: 0.0,
        }
    }

    fn push_interleaved(&mut self, samples: &[i16]) -> Option<f32> {
        debug_assert_eq!(samples.len() % (CHANNELS as usize), 0);
        if samples.is_empty() {
            return None;
        }
        let mut energy = 0.0f64;
        for frame in samples.chunks_exact(CHANNELS as usize) {
            for (ch, sample) in frame.iter().enumerate() {
                let weighted = self.filters[ch].process(*sample as f64);
                energy += weighted * weighted;
            }
        }
        let frames = (samples.len() / (CHANNELS as usize)) as u64;
        self.periods.push_back(EnergyPeriod { frames, energy });
        self.total_frames = self.total_frames.saturating_add(frames);
        self.total_energy += energy;
        while self.total_frames > self.max_frames {
            let Some(oldest) = self.periods.pop_front() else {
                break;
            };
            self.total_frames = self.total_frames.saturating_sub(oldest.frames);
            self.total_energy -= oldest.energy;
        }
        lufs_from_energy(energy, frames)
    }

    fn short_lufs(&self) -> Option<f32> {
        self.window_lufs(SHORT_TERM_FRAMES)
            .or_else(|| self.window_lufs(MOMENTARY_FRAMES))
    }

    fn full_short_lufs(&self) -> Option<f32> {
        if self.total_frames < SHORT_TERM_FRAMES {
            return None;
        }
        self.window_lufs(SHORT_TERM_FRAMES)
    }

    fn anchor_lufs(&self) -> Option<f32> {
        lufs_from_energy(self.total_energy, self.total_frames)
    }

    fn window_lufs(&self, target_frames: u64) -> Option<f32> {
        let mut frames = 0u64;
        let mut energy = 0.0f64;
        for period in self.periods.iter().rev() {
            frames = frames.saturating_add(period.frames);
            energy += period.energy;
            if frames >= target_frames {
                break;
            }
        }
        if frames < MOMENTARY_FRAMES {
            return None;
        }
        lufs_from_energy(energy, frames)
    }
}

#[derive(Debug, Clone, Copy)]
struct EnergyPeriod {
    frames: u64,
    energy: f64,
}

#[derive(Debug, Clone, Copy)]
struct KWeightingChannel {
    pre: Biquad,
    rlb: Biquad,
}

impl KWeightingChannel {
    fn new() -> Self {
        Self {
            pre: Biquad::new(
                1.53512485958697,
                -2.69169618940638,
                1.19839281085285,
                -1.69065929318241,
                0.73248077421585,
            ),
            rlb: Biquad::new(1.0, -2.0, 1.0, -1.99004745483398, 0.99007225036621),
        }
    }

    fn process(&mut self, sample: f64) -> f64 {
        self.rlb.process(self.pre.process(sample))
    }
}

#[derive(Debug, Clone, Copy)]
struct Biquad {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl Biquad {
    fn new(b0: f64, b1: f64, b2: f64, a1: f64, a2: f64) -> Self {
        Self {
            b0,
            b1,
            b2,
            a1,
            a2,
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        }
    }

    fn process(&mut self, x0: f64) -> f64 {
        let y0 = self.b0 * x0 + self.b1 * self.x1 + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x0;
        self.y2 = self.y1;
        self.y1 = y0;
        y0
    }
}

fn lufs_from_energy(energy: f64, frames: u64) -> Option<f32> {
    if frames == 0 || energy <= 0.0 || !energy.is_finite() {
        return None;
    }
    let mean_square_sum = energy / (frames as f64);
    let relative = mean_square_sum / FULL_SCALE_SQ;
    if relative <= 0.0 || !relative.is_finite() {
        return None;
    }
    Some((BS1770_OFFSET_DB + 10.0 * relative.log10()) as f32)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stereo_sine(amplitude: f32, frames: usize) -> Vec<i16> {
        let mut samples = Vec::with_capacity(frames * (CHANNELS as usize));
        for n in 0..frames {
            let phase = 2.0 * std::f32::consts::PI * 1000.0 * (n as f32) / (SAMPLE_RATE as f32);
            let sample = (amplitude * 32767.0 * phase.sin()).round() as i16;
            samples.push(sample);
            samples.push(sample);
        }
        samples
    }

    #[test]
    fn silence_has_no_loudness() {
        let mut meter = KWeightedWindow::new(SHORT_TERM_FRAMES);
        let silence = vec![0i16; (SAMPLE_RATE as usize) * (CHANNELS as usize)];
        meter.push_interleaved(&silence);
        assert!(meter.short_lufs().is_none());
    }

    #[test]
    fn steady_tone_reports_plausible_loudness() {
        let mut meter = KWeightedWindow::new(SHORT_TERM_FRAMES);
        for _ in 0..3 {
            meter.push_interleaved(&stereo_sine(0.25, SAMPLE_RATE as usize));
        }
        let lufs = meter.short_lufs().unwrap();
        // A 0.25-amplitude sine is ~-15.05 dBFS RMS per channel; the 1 kHz
        // K-weighting adds ~+0.7 dB, the BS.1770 channel sum of two identical
        // channels adds +10*log10(2) = +3.01 dB, and the BS.1770 -0.691 dB
        // absolute offset applies, landing at ~-12.03 LUFS.
        assert!((-13.0..-11.0).contains(&lufs), "lufs={lufs}");
    }

    #[test]
    fn calibrated_profile_targets_first_use_envelope_exactly() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 2.0,
            max_peak_dbfs: -3.0,
            ..AssistantLoudnessConfig::default()
        });
        loudness.prepare_context(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -38.0,
        );
        let decision = loudness.decide_gain(
            SegmentKind::Assistant,
            -12.0,
            Some(AssistantProfile {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                source_lufs: Some(-25.0),
                source_peak_dbfs: Some(-8.0),
                confidence: 1.0,
            }),
        );
        assert_eq!(decision.target_lufs, -38.0);
        assert_eq!(decision.requested_gain_db, -13.0);
        assert_eq!(decision.final_gain_db, -13.0);
        assert_eq!(decision.clamp_reason, "target");
    }

    #[test]
    fn peak_cap_wins_over_loudness_target() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 2.0,
            max_peak_dbfs: -6.0,
            ..AssistantLoudnessConfig::default()
        });
        loudness.prepare_context(
            "gemini".to_string(),
            "gemini-3.1".to_string(),
            "Aoede".to_string(),
            -30.0,
        );
        let decision = loudness.decide_gain(
            SegmentKind::Assistant,
            -12.0,
            Some(AssistantProfile {
                provider: "gemini".to_string(),
                model: "gemini-3.1".to_string(),
                voice: "Aoede".to_string(),
                source_lufs: Some(-35.0),
                source_peak_dbfs: Some(-2.0),
                confidence: 1.0,
            }),
        );
        assert_eq!(decision.peak_cap_gain_db, -4.0);
        assert_eq!(decision.final_gain_db, -4.0);
        assert_eq!(decision.clamp_reason, "peak_cap");
    }

    #[test]
    fn cue_without_context_uses_default_tts_envelope_not_fallback_gain() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_tts_envelope_lufs: -41.0,
            max_peak_dbfs: -3.0,
            ..AssistantLoudnessConfig::default()
        });

        let decision = loudness.decide_gain(
            SegmentKind::Cue,
            0.0,
            Some(AssistantProfile {
                provider: "openai".to_string(),
                model: "gpt-4o-mini-tts".to_string(),
                voice: "marin".to_string(),
                source_lufs: Some(-24.0),
                source_peak_dbfs: Some(-8.0),
                confidence: 0.65,
            }),
        );

        assert_eq!(decision.baseline_lufs, -41.0);
        assert_eq!(decision.target_lufs, -41.0);
        assert_eq!(decision.requested_gain_db, -17.0);
        assert_eq!(decision.final_gain_db, -17.0);
        assert_eq!(decision.clamp_reason, "target");
    }

    #[test]
    fn cue_without_profile_uses_fallback_profile_not_fallback_gain() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_tts_envelope_lufs: -41.0,
            fallback_source_lufs: -24.0,
            fallback_source_peak_dbfs: -6.0,
            ..AssistantLoudnessConfig::default()
        });

        let decision = loudness.decide_gain(SegmentKind::Cue, 0.0, None);

        assert_eq!(decision.baseline_lufs, -41.0);
        assert_eq!(decision.target_lufs, -41.0);
        assert_eq!(decision.requested_gain_db, -17.0);
        assert_eq!(decision.final_gain_db, -17.0);
        assert_eq!(decision.clamp_reason, "fallback_profile");
    }

    #[test]
    fn invalid_direct_profile_values_fall_back_before_gain_math() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_tts_envelope_lufs: -41.0,
            fallback_source_lufs: -24.0,
            fallback_source_peak_dbfs: -6.0,
            max_peak_dbfs: -3.0,
            ..AssistantLoudnessConfig::default()
        });

        let decision = loudness.decide_gain(
            SegmentKind::Assistant,
            0.0,
            Some(AssistantProfile {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                source_lufs: Some(-1000.0),
                source_peak_dbfs: Some(-1000.0),
                confidence: 99.0,
            }),
        );

        assert!(!decision.calibrated);
        assert_eq!(decision.profile_confidence, 0.0);
        assert_eq!(decision.source_lufs, -24.0);
        assert_eq!(decision.source_peak_dbfs, -6.0);
        assert_eq!(decision.requested_gain_db, -17.0);
        assert_eq!(decision.peak_cap_gain_db, 3.0);
        assert_eq!(decision.final_gain_db, -17.0);
        assert_eq!(decision.clamp_reason, "fallback_profile");
    }

    #[test]
    fn chirp_with_profile_uses_target_loudness_not_fallback_gain() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_tts_envelope_lufs: -41.0,
            max_peak_dbfs: -3.0,
            ..AssistantLoudnessConfig::default()
        });

        let decision = loudness.decide_gain(
            SegmentKind::Chirp,
            0.0,
            Some(AssistantProfile {
                provider: "jts".to_string(),
                model: "synthetic-listening-chirp".to_string(),
                voice: "wake_start".to_string(),
                source_lufs: Some(-15.0),
                source_peak_dbfs: Some(-14.9),
                confidence: 1.0,
            }),
        );

        assert_eq!(decision.provider, Some("jts".to_string()));
        assert_eq!(
            decision.model,
            Some("synthetic-listening-chirp".to_string())
        );
        assert_eq!(decision.voice, Some("wake_start".to_string()));
        assert_eq!(decision.baseline_lufs, -41.0);
        assert_eq!(decision.target_lufs, -41.0);
        assert_eq!(decision.requested_gain_db, -26.0);
        assert_eq!(decision.final_gain_db, -26.0);
        assert_eq!(decision.clamp_reason, "target");
    }

    #[test]
    fn chirp_without_profile_uses_fallback_profile_not_fallback_gain() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_tts_envelope_lufs: -41.0,
            fallback_source_lufs: -24.0,
            fallback_source_peak_dbfs: -6.0,
            ..AssistantLoudnessConfig::default()
        });

        let decision = loudness.decide_gain(SegmentKind::Chirp, 0.0, None);

        assert_eq!(decision.baseline_lufs, -41.0);
        assert_eq!(decision.target_lufs, -41.0);
        assert_eq!(decision.requested_gain_db, -17.0);
        assert_eq!(decision.final_gain_db, -17.0);
        assert_eq!(decision.clamp_reason, "fallback_profile");
    }

    #[test]
    fn tts_gain_sanitize_allows_positive_and_rejects_nonfinite_values() {
        assert_eq!(sanitize_tts_gain_db(0.0), 0.0);
        assert_eq!(sanitize_tts_gain_db(12.0), 12.0);
        assert_eq!(sanitize_tts_gain_db(f32::NAN), MIN_TTS_GAIN_DB);
        assert_eq!(sanitize_tts_gain_db(f32::INFINITY), MIN_TTS_GAIN_DB);
    }

    #[test]
    fn tts_gain_sanitize_preserves_safe_range_and_floor() {
        assert_eq!(sanitize_tts_gain_db(-12.5), -12.5);
        assert_eq!(sanitize_tts_gain_db(-100.0), MIN_TTS_GAIN_DB);
    }

    fn profile(source_lufs: f32, source_peak_dbfs: f32) -> AssistantProfile {
        AssistantProfile {
            provider: "openai".to_string(),
            model: "gpt-realtime-2".to_string(),
            voice: "marin".to_string(),
            source_lufs: Some(source_lufs),
            source_peak_dbfs: Some(source_peak_dbfs),
            confidence: 1.0,
        }
    }

    fn volume_context(
        canonical_db: f32,
        downstream_db: f32,
        tts_envelope_lufs: f32,
        stamp_boot_ns: u64,
    ) -> VolumeContext {
        VolumeContext {
            canonical_db,
            downstream_db,
            tts_envelope_lufs,
            muted: false,
            stamp_boot_ns,
        }
    }

    #[test]
    fn first_use_fallback_compensates_downstream_at_the_speaker_boundary() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        let context = volume_context(-36.4, -36.4, -46.7, 1);
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -46.7,
            Some(context),
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));

        assert_eq!(decision.reference_kind, ReferenceKind::FirstUseFallback);
        assert!((decision.target_speaker_lufs.unwrap() - -46.7).abs() < 0.01);
        assert!((decision.target_lufs - -10.3).abs() < 0.01);
        let achieved = decision.source_lufs + decision.final_gain_db + context.downstream_db;
        assert!((achieved - -46.7).abs() < 0.01);
    }

    #[test]
    fn held_assistant_repeats_without_reapplying_offset_and_tracks_user_delta() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        loudness.set_held_assistant(Some(HeldLoudnessReference {
            speaker_lufs: -39.0,
            canonical_db: -30.0,
            calibration_offset_lu: 0.5,
        }));
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -41.0,
            Some(volume_context(-24.0, 0.0, -39.0, 1)),
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-24.0, -30.0)));

        assert_eq!(decision.reference_kind, ReferenceKind::HeldAssistant);
        assert_eq!(decision.target_speaker_lufs, Some(-38.5));
        assert_eq!(decision.target_lufs, -38.5);
    }

    #[test]
    fn content_reference_requires_three_seconds_and_silence_cannot_overwrite_it() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        loudness.update_volume_context(volume_context(-20.0, -20.0, -38.0, 1));
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) * 29 / 10));
        assert_eq!(loudness.held_content(), None);
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) / 10));
        let held = loudness.held_content().expect("qualified music reference");

        let silence = vec![0i16; (SAMPLE_RATE as usize) * 12 * (CHANNELS as usize)];
        loudness.observe_content_period(&silence);
        assert_eq!(loudness.held_content(), Some(held));
    }

    #[test]
    fn held_content_expires_after_sustained_silence() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            held_content_ttl_sec: 1.0,
            ..AssistantLoudnessConfig::default()
        });
        loudness.update_volume_context(volume_context(-20.0, -20.0, -38.0, 1));
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) * 3));
        assert!(loudness.held_content().is_some());

        loudness.observe_content_period(&vec![
            0i16;
            (SAMPLE_RATE as usize) * 2 * (CHANNELS as usize)
        ]);
        assert_eq!(loudness.held_content(), None);
        loudness.prepare_context(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -38.0,
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-24.0, -12.0)));
        assert_eq!(decision.reference_kind, ReferenceKind::FirstUseFallback);
    }

    #[test]
    fn brief_sound_after_silence_does_not_qualify_as_music() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        loudness.update_volume_context(volume_context(-20.0, -20.0, -38.0, 1));
        loudness.observe_content_period(&vec![
            0i16;
            (SAMPLE_RATE as usize) * 3 * (CHANNELS as usize)
        ]);
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) / 10));
        assert_eq!(loudness.held_content(), None);
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) * 29 / 10));
        assert!(loudness.held_content().is_some());
    }

    #[test]
    fn reference_priority_is_live_then_held_content_then_held_assistant() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            held_content_ttl_sec: 1.0,
            ..AssistantLoudnessConfig::default()
        });
        loudness.set_held_assistant(Some(HeldLoudnessReference {
            speaker_lufs: -39.5,
            canonical_db: -30.0,
            calibration_offset_lu: 0.0,
        }));
        loudness.update_volume_context(volume_context(-30.0, -30.0, -41.0, 1));
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) * 3));
        loudness.prepare_context(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
        );
        assert_eq!(
            loudness
                .decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-24.0, -12.0)))
                .reference_kind,
            ReferenceKind::LiveContent
        );

        let short_silence = vec![0i16; (SAMPLE_RATE as usize) / 2 * (CHANNELS as usize)];
        loudness.observe_content_period(&short_silence);
        loudness.prepare_context(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
        );
        assert_eq!(
            loudness
                .decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-24.0, -12.0)))
                .reference_kind,
            ReferenceKind::HeldContent
        );

        loudness.observe_content_period(&vec![0i16; (SAMPLE_RATE as usize) * (CHANNELS as usize)]);
        loudness.prepare_context(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
        );
        assert_eq!(
            loudness
                .decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-24.0, -12.0)))
                .reference_kind,
            ReferenceKind::HeldAssistant
        );
    }

    #[test]
    fn learned_envelope_offset_is_clamped_and_reused_without_progressive_quieting() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_envelope_offset_limit_lu: 8.0,
            ..AssistantLoudnessConfig::default()
        });
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
            Some(volume_context(-30.0, 0.0, -41.0, 1)),
        );
        let first = loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(first.envelope_offset_lu, Some(0.0));
        assert_eq!(first.target_speaker_lufs, Some(-41.0));

        let clamped = loudness
            .complete_assistant_segment(&first, first.final_gain_db - 20.0)
            .expect("completed turn");
        assert_eq!(clamped.calibration_offset_lu, -8.0);

        loudness.prepare_context(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
        );
        let second = loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(second.envelope_offset_lu, Some(-8.0));
        assert_eq!(second.target_speaker_lufs, Some(-49.0));

        for _ in 0..3 {
            let reference = loudness
                .complete_assistant_segment(&second, second.final_gain_db)
                .expect("completed replay");
            assert_eq!(reference.calibration_offset_lu, -8.0);
        }
    }

    #[test]
    fn stale_volume_context_cannot_overwrite_newer_absolute_state() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        let newer = volume_context(-20.0, -20.0, -38.0, 20);
        let older = volume_context(-40.0, -40.0, -48.0, 10);
        assert!(loudness.update_volume_context(newer));
        assert!(!loudness.update_volume_context(older));
        assert_eq!(loudness.current_volume_context(), Some(newer));
    }

    #[test]
    fn muted_completion_never_learns_an_assistant_reference() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        let mut muted = volume_context(-30.0, -30.0, -41.0, 1);
        muted.muted = true;
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
            Some(muted),
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(
            loudness.complete_assistant_segment(&decision, decision.final_gain_db),
            None
        );
    }

    #[test]
    fn music_anchored_completion_does_not_poison_quiet_envelope_offset() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        loudness.set_held_assistant(Some(HeldLoudnessReference {
            speaker_lufs: -39.5,
            canonical_db: -30.0,
            calibration_offset_lu: 0.0,
        }));
        loudness.update_volume_context(volume_context(-30.0, -30.0, -41.0, 1));
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) * 3));
        loudness.prepare_context(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(decision.reference_kind, ReferenceKind::LiveContent);
        assert_eq!(
            loudness.complete_assistant_segment(&decision, decision.final_gain_db),
            None
        );
        assert_eq!(
            loudness.held_assistant().unwrap().calibration_offset_lu,
            0.0
        );
    }

    #[test]
    fn quiet_live_gain_delta_follows_the_same_silence_curve() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -41.0,
            Some(volume_context(-30.0, -30.0, -41.0, 1)),
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-24.0, -12.0)));
        loudness.update_volume_context(volume_context(-24.0, -24.0, -39.44, 2));
        assert!((loudness.live_gain_delta_db(&decision) - -4.44).abs() < 0.01);

        loudness.update_volume_context(volume_context(-18.0, -24.0, -37.88, 3));
        assert!((loudness.live_gain_delta_db(&decision) - -2.88).abs() < 0.01);
    }

    fn post_dsp() -> AssistantLoudness {
        AssistantLoudness::new_with_stage(AssistantLoudnessConfig::default(), MixStage::PostDsp)
    }

    #[test]
    fn post_dsp_decide_gain_zeroes_downstream_but_honors_canonical_envelope_muted() {
        // Downstream is structurally zero post-DSP: a large Camilla gain is
        // NOT compensated, so first-use speech lands on the envelope itself
        // (a PRE-DSP engine would add |downstream| back and target -11 LUFS).
        let mut loudness = post_dsp();
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
            Some(volume_context(-30.0, -30.0, -41.0, 1)),
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(decision.reference_kind, ReferenceKind::FirstUseFallback);
        assert_eq!(decision.target_speaker_lufs, Some(-41.0));
        assert_eq!(decision.target_lufs, -41.0);
        assert_eq!(decision.requested_gain_db, -16.0);

        // The envelope (tts_envelope_lufs) still drives the target one-for-one.
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "m".to_string(),
            "v".to_string(),
            -35.0,
            Some(volume_context(-30.0, -30.0, -35.0, 2)),
        );
        let louder = loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(louder.target_lufs, -35.0);

        // Canonical user-delta is still tracked through a held-content
        // reference: raising the knob +6 dB raises the target +6 dB, and the
        // downstream term is zeroed at BOTH the store (observe) and the decide,
        // so it cannot inflate the held target (the double-compensation guard).
        let mut held = post_dsp();
        held.update_volume_context(volume_context(-30.0, -30.0, -41.0, 1));
        held.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) * 3));
        // A brief silence makes content no longer "currently audible", so the
        // next decision uses HeldContent (not LiveContent) — but never expires.
        held.observe_content_period(&vec![
            0i16;
            (SAMPLE_RATE as usize) / 2 * (CHANNELS as usize)
        ]);
        held.prepare_context("o".to_string(), "m".to_string(), "v".to_string(), -41.0);
        let at_low = held.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(at_low.reference_kind, ReferenceKind::HeldContent);

        held.update_volume_context(volume_context(-24.0, -30.0, -41.0, 2));
        held.observe_content_period(&vec![
            0i16;
            (SAMPLE_RATE as usize) / 2 * (CHANNELS as usize)
        ]);
        held.prepare_context("o".to_string(), "m".to_string(), "v".to_string(), -41.0);
        let at_high = held.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(at_high.reference_kind, ReferenceKind::HeldContent);
        assert!(
            (at_high.target_lufs - at_low.target_lufs - 6.0).abs() < 0.01,
            "canonical +6 must move the held-content target +6: {} vs {}",
            at_high.target_lufs,
            at_low.target_lufs,
        );

        // Mute still blocks learning post-DSP — the follower safety guard.
        let mut muted_ctx = volume_context(-30.0, -30.0, -41.0, 3);
        muted_ctx.muted = true;
        loudness.update_volume_context(muted_ctx);
        assert_eq!(
            loudness.complete_assistant_segment(&decision, decision.final_gain_db),
            None
        );
    }

    #[test]
    fn post_dsp_and_pre_dsp_decide_differently_with_nonzero_downstream() {
        // The core "no double-compensation" property at the engine level: the
        // SAME VolumeContext with a nonzero downstream yields a different gain
        // pre- vs post-DSP. Pre-DSP compensates for Camilla (+14 dB); post-DSP
        // must not (-16 dB).
        let ctx = volume_context(-30.0, -30.0, -41.0, 1);
        let decide = |stage| {
            let mut loudness =
                AssistantLoudness::new_with_stage(AssistantLoudnessConfig::default(), stage);
            loudness.prepare_context_with_volume(
                "o".to_string(),
                "m".to_string(),
                "v".to_string(),
                -41.0,
                Some(ctx),
            );
            loudness
                .decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)))
                .final_gain_db
        };
        let pre = decide(MixStage::PreDsp);
        let post = decide(MixStage::PostDsp);
        assert_eq!(pre, 14.0);
        assert_eq!(post, -16.0);
        assert_ne!(pre, post);
    }

    #[test]
    fn post_dsp_learns_and_reuses_clamped_envelope_offset_without_progressive_quieting() {
        let mut loudness = post_dsp();
        // Nonzero Camilla downstream that must be IGNORED when learning.
        loudness.prepare_context_with_volume(
            "o".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
            Some(volume_context(-30.0, -30.0, -41.0, 1)),
        );
        let first = loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(first.reference_kind, ReferenceKind::FirstUseFallback);
        assert_eq!(first.target_speaker_lufs, Some(-41.0));

        // Achieved gain -20 dB → speaker = source(-25) + (-20) + downstream(0)
        // = -45 LUFS vs envelope -41 → offset -4, comfortably inside the ±8
        // clamp. A PRE-DSP engine would fold the -30 downstream in and saturate
        // the clamp at -8 instead — so this value proves downstream is ignored.
        let learned = loudness
            .complete_assistant_segment(&first, -20.0)
            .expect("completed turn");
        assert!((learned.calibration_offset_lu - -4.0).abs() < 1e-4);

        // Reuse: the next turn targets envelope+offset, no re-application.
        loudness.prepare_context_with_volume(
            "o".to_string(),
            "m".to_string(),
            "v".to_string(),
            -41.0,
            Some(volume_context(-30.0, -30.0, -41.0, 2)),
        );
        let second = loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-25.0, -30.0)));
        assert_eq!(second.reference_kind, ReferenceKind::HeldAssistant);
        assert_eq!(second.envelope_offset_lu, Some(-4.0));
        assert_eq!(second.target_speaker_lufs, Some(-45.0));
        assert_eq!(second.target_lufs, -45.0);

        // Replaying the achieved gain does not drift the offset (no
        // progressive quieting).
        for _ in 0..3 {
            let replay = loudness
                .complete_assistant_segment(&second, second.final_gain_db)
                .expect("completed replay");
            assert!((replay.calibration_offset_lu - -4.0).abs() < 1e-4);
        }
    }

    #[test]
    fn gain_ramp_snaps_first_target_then_ramps_from_silence_on_unmute() {
        // First target snaps in — no ramp-up from zero on the first frame, so a
        // steady segment renders at its decided gain from sample one.
        let mut ramp = GainRamp::new();
        ramp.retarget(0.0);
        assert_eq!(ramp.next_frame(), 1.0);

        // Mute collapses to silence and re-arms; unmute ramps up from zero even
        // when the new target is the -60 dB floor (never a loud snap).
        ramp.force_silent();
        ramp.retarget(MIN_TTS_GAIN_DB);
        let first = ramp.next_frame();
        assert!(first > 0.0);
        assert!(first < gain_db_to_linear(MIN_TTS_GAIN_DB));
        for _ in 1..LIVE_VOLUME_RAMP_FRAMES {
            ramp.next_frame();
        }
        assert!((ramp.current_linear() - gain_db_to_linear(MIN_TTS_GAIN_DB)).abs() < 1e-9);
    }

    #[test]
    fn assistant_loudness_status_keys_are_stable() {
        // The shared STATUS wire shape. Both daemons render through
        // `render_assistant_loudness` and each asserts its rendered block
        // carries these keys — changing this list is a deliberate wire change.
        assert_eq!(
            ASSISTANT_LOUDNESS_STATUS_KEYS,
            [
                "content_short_lufs",
                "content_anchor_lufs",
                "decision_seen",
                "calibrated",
                "profile_confidence",
                "baseline_lufs",
                "target_lufs",
                "source_lufs",
                "source_peak_dbfs",
                "requested_gain_db",
                "peak_cap_gain_db",
                "final_gain_db",
                "target_speaker_lufs",
                "envelope_offset_lu",
                "reference_kind",
                "volume_context",
                "volume_context_rejected",
                "held_content",
                "held_assistant",
            ]
        );
        assert_eq!(
            ASSISTANT_LOUDNESS_VOLUME_CONTEXT_KEYS,
            [
                "canonical_db",
                "downstream_db",
                "tts_envelope_lufs",
                "muted",
                "stamp_boot_ns"
            ]
        );
        assert_eq!(
            ASSISTANT_LOUDNESS_REFERENCE_KEYS,
            ["speaker_lufs", "canonical_db", "calibration_offset_lu"]
        );
    }

    #[test]
    fn render_assistant_loudness_emits_every_status_key() {
        let populated = TtsLoudnessSnapshot {
            content_short_lufs: Some(-18.0),
            content_anchor_lufs: Some(-19.0),
            decision_seen: true,
            calibrated: true,
            profile_confidence: 0.9,
            baseline_lufs: Some(-41.0),
            target_lufs: Some(-40.0),
            source_lufs: Some(-25.0),
            source_peak_dbfs: Some(-8.0),
            requested_gain_db: Some(-15.0),
            peak_cap_gain_db: Some(5.0),
            final_gain_db: Some(-15.0),
            target_speaker_lufs: Some(-40.0),
            envelope_offset_lu: Some(0.5),
            reference_kind: Some("held_assistant"),
            volume_context: Some(VolumeContext {
                canonical_db: -30.0,
                downstream_db: -30.0,
                tts_envelope_lufs: -41.0,
                muted: false,
                stamp_boot_ns: 7,
            }),
            volume_context_rejected: 2,
            held_content: Some(HeldLoudnessReference {
                speaker_lufs: -20.0,
                canonical_db: -30.0,
                calibration_offset_lu: 0.0,
            }),
            held_assistant: Some(HeldLoudnessReference {
                speaker_lufs: -40.0,
                canonical_db: -30.0,
                calibration_offset_lu: 0.5,
            }),
        };
        let mut buf = String::new();
        render_assistant_loudness(&mut buf, &populated);
        for key in ASSISTANT_LOUDNESS_STATUS_KEYS {
            assert!(
                buf.contains(&format!("\"{key}\":")),
                "missing key {key}: {buf}"
            );
        }
        for key in ASSISTANT_LOUDNESS_VOLUME_CONTEXT_KEYS {
            assert!(
                buf.contains(&format!("\"{key}\":")),
                "missing volume_context key {key}: {buf}"
            );
        }
        for key in ASSISTANT_LOUDNESS_REFERENCE_KEYS {
            assert!(
                buf.contains(&format!("\"{key}\":")),
                "missing reference key {key}: {buf}"
            );
        }
        assert!(buf.starts_with('{') && buf.ends_with('}'));

        // The empty snapshot still emits every top-level key (nulls/false).
        let mut empty = String::new();
        render_assistant_loudness(&mut empty, &TtsLoudnessSnapshot::default());
        for key in ASSISTANT_LOUDNESS_STATUS_KEYS {
            assert!(
                empty.contains(&format!("\"{key}\":")),
                "empty missing key {key}: {empty}"
            );
        }
        assert!(empty.contains(r#""volume_context":null"#));
        assert!(empty.contains(r#""held_assistant":null"#));
    }

    #[test]
    fn gain_helpers_preserve_existing_scaling_and_clipping() {
        assert_eq!(apply_gain_i16(10_000, 0.5), 5000);
        assert_eq!(apply_gain_i16(i16::MAX, 2.0), i16::MAX);
        assert_eq!(apply_gain_i16(i16::MIN, 2.0), i16::MIN);
        assert_eq!(linear_to_db(0.0), MIN_TTS_GAIN_DB);
        assert!((gain_db_to_linear(-6.0) - 0.5011872).abs() < 0.000001);
    }

    fn representative_sequence_decisions() -> Vec<AssistantGainDecision> {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        let content = stereo_sine(0.08, (SAMPLE_RATE as usize) / 2);
        loudness.observe_content_period(&content);
        loudness.prepare_context(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -41.0,
        );
        let first = loudness.decide_gain(
            SegmentKind::Assistant,
            -6.0,
            Some(AssistantProfile {
                provider: "openai".to_string(),
                model: "gpt-realtime-2".to_string(),
                voice: "marin".to_string(),
                source_lufs: Some(-25.0),
                source_peak_dbfs: Some(-8.0),
                confidence: 1.0,
            }),
        );
        loudness.clear_context();
        loudness.observe_content_period(&vec![0i16; content.len()]);
        let second = loudness.decide_gain(SegmentKind::Cue, 0.0, None);
        vec![first, second]
    }

    #[test]
    fn representative_gain_sequence_is_deterministic() {
        let first_path = representative_sequence_decisions();
        let second_path = representative_sequence_decisions();

        assert_eq!(first_path, second_path);
    }
}
