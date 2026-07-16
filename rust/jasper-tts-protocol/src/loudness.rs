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

#[derive(Debug, Clone, Copy)]
pub struct AssistantLoudnessConfig {
    pub assistant_offset_lu: f32,
    pub max_peak_dbfs: f32,
    pub fallback_source_lufs: f32,
    pub fallback_source_peak_dbfs: f32,
    pub default_silence_target_lufs: f32,
    pub content_silence_lufs: f32,
}

impl Default for AssistantLoudnessConfig {
    fn default() -> Self {
        Self {
            assistant_offset_lu: 1.5,
            max_peak_dbfs: -3.0,
            fallback_source_lufs: -24.0,
            fallback_source_peak_dbfs: -6.0,
            default_silence_target_lufs: -41.0,
            content_silence_lufs: -60.0,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct AssistantContext {
    pub provider: String,
    pub model: String,
    pub voice: String,
    pub baseline_lufs: Option<f32>,
    pub silence_target_lufs: f32,
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
    pub volume_context: Option<VolumeContext>,
}

pub struct AssistantLoudness {
    config: AssistantLoudnessConfig,
    content: KWeightedWindow,
    pending_context: Option<AssistantContext>,
    last_decision: Option<AssistantGainDecision>,
    current_volume_context: Option<VolumeContext>,
    held_content: Option<HeldLoudnessReference>,
    held_assistant: Option<HeldLoudnessReference>,
    content_currently_audible: bool,
}

impl AssistantLoudness {
    pub fn new(config: AssistantLoudnessConfig) -> Self {
        Self {
            config,
            content: KWeightedWindow::new(CONTENT_ANCHOR_FRAMES),
            pending_context: None,
            last_decision: None,
            current_volume_context: None,
            held_content: None,
            held_assistant: None,
            content_currently_audible: false,
        }
    }

    pub fn observe_content_period(&mut self, samples: &[i16]) {
        let period_lufs = self.content.push_interleaved(samples);
        self.content_currently_audible = period_lufs
            .is_some_and(|value| value.is_finite() && value >= self.config.content_silence_lufs);
        if !self.content_currently_audible {
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
        self.held_content = Some(HeldLoudnessReference {
            speaker_lufs: content_lufs + context.downstream_db,
            canonical_db: context.canonical_db,
        });
    }

    pub fn prepare_context(
        &mut self,
        provider: String,
        model: String,
        voice: String,
        silence_target_lufs: f32,
    ) {
        self.prepare_context_with_volume(provider, model, voice, silence_target_lufs, None);
    }

    pub fn prepare_context_with_volume(
        &mut self,
        provider: String,
        model: String,
        voice: String,
        silence_target_lufs: f32,
        volume_context: Option<VolumeContext>,
    ) {
        if let Some(context) = volume_context {
            self.update_volume_context(context);
        }
        let baseline_lufs = if self.content_currently_audible {
            self.observed_content_lufs()
        } else {
            None
        };
        self.pending_context = Some(AssistantContext {
            provider,
            model,
            voice,
            baseline_lufs,
            silence_target_lufs,
            volume_context: self.current_volume_context,
        });
    }

    pub fn update_volume_context(&mut self, context: VolumeContext) {
        if context.canonical_db.is_finite() && context.downstream_db.is_finite() {
            self.current_volume_context = Some(context);
        }
    }

    pub fn set_held_assistant(&mut self, reference: Option<HeldLoudnessReference>) {
        self.held_assistant = reference
            .filter(|value| value.speaker_lufs.is_finite() && value.canonical_db.is_finite());
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
                    if self.content_currently_audible {
                        self.observed_content_lufs()
                    } else {
                        None
                    }
                });
        let volume_context = context
            .as_ref()
            .and_then(|ctx| ctx.volume_context)
            .or(self.current_volume_context);
        let (baseline_lufs, target_lufs, target_speaker_lufs, reference_kind) =
            if let Some(baseline) = observed_baseline_lufs {
                let target = baseline + self.config.assistant_offset_lu;
                (
                    baseline,
                    target,
                    volume_context.map(|ctx| target + ctx.downstream_db),
                    ReferenceKind::LiveContent,
                )
            } else if let (Some(reference), Some(current)) = (self.held_content, volume_context) {
                let target_speaker = reference.speaker_lufs
                    + (current.canonical_db - reference.canonical_db)
                    + self.config.assistant_offset_lu;
                let target = target_speaker - current.downstream_db;
                (
                    target - self.config.assistant_offset_lu,
                    target,
                    Some(target_speaker),
                    ReferenceKind::HeldContent,
                )
            } else if let (Some(reference), Some(current)) = (self.held_assistant, volume_context) {
                // The held assistant value is already the achieved speaker
                // loudness. Do not re-apply the content-relative offset.
                let target_speaker =
                    reference.speaker_lufs + (current.canonical_db - reference.canonical_db);
                let target = target_speaker - current.downstream_db;
                (
                    target,
                    target,
                    Some(target_speaker),
                    ReferenceKind::HeldAssistant,
                )
            } else {
                let baseline = context
                    .as_ref()
                    .map_or(self.config.default_silence_target_lufs, |ctx| {
                        ctx.silence_target_lufs
                    });
                let target_speaker = baseline + self.config.assistant_offset_lu;
                let target = volume_context.map_or(target_speaker, |current| {
                    target_speaker - current.downstream_db
                });
                (
                    baseline,
                    target,
                    volume_context.map(|_| target_speaker),
                    ReferenceKind::FirstUseFallback,
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
        (current.canonical_db - initial.canonical_db)
            - (current.downstream_db - initial.downstream_db)
    }

    /// Capture only completed assistant speech as the no-music reference.
    /// Cues and chirps never call this method.
    pub fn complete_assistant_segment(
        &mut self,
        decision: &AssistantGainDecision,
        effective_gain_db: f32,
    ) -> Option<HeldLoudnessReference> {
        let current = self.current_volume_context?;
        if current.muted || !effective_gain_db.is_finite() {
            return None;
        }
        let reference = HeldLoudnessReference {
            speaker_lufs: decision.source_lufs + effective_gain_db + current.downstream_db,
            canonical_db: current.canonical_db,
        };
        if !reference.speaker_lufs.is_finite() {
            return None;
        }
        self.held_assistant = Some(reference);
        Some(reference)
    }

    fn observed_content_lufs(&self) -> Option<f32> {
        self.content
            .short_lufs()
            .or_else(|| self.content.anchor_lufs())
            .filter(|v| v.is_finite() && *v >= self.config.content_silence_lufs)
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
    fn calibrated_profile_targets_baseline_plus_offset() {
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
        assert_eq!(decision.target_lufs, -36.0);
        assert_eq!(decision.requested_gain_db, -11.0);
        assert_eq!(decision.final_gain_db, -11.0);
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
    fn cue_without_context_uses_default_silence_target_not_fallback_gain() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_silence_target_lufs: -41.0,
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
        assert_eq!(decision.target_lufs, -39.5);
        assert_eq!(decision.requested_gain_db, -15.5);
        assert_eq!(decision.final_gain_db, -15.5);
        assert_eq!(decision.clamp_reason, "target");
    }

    #[test]
    fn cue_without_profile_uses_fallback_profile_not_fallback_gain() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_silence_target_lufs: -41.0,
            fallback_source_lufs: -24.0,
            fallback_source_peak_dbfs: -6.0,
            ..AssistantLoudnessConfig::default()
        });

        let decision = loudness.decide_gain(SegmentKind::Cue, 0.0, None);

        assert_eq!(decision.baseline_lufs, -41.0);
        assert_eq!(decision.target_lufs, -39.5);
        assert_eq!(decision.requested_gain_db, -15.5);
        assert_eq!(decision.final_gain_db, -15.5);
        assert_eq!(decision.clamp_reason, "fallback_profile");
    }

    #[test]
    fn invalid_direct_profile_values_fall_back_before_gain_math() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_silence_target_lufs: -41.0,
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
        assert_eq!(decision.requested_gain_db, -15.5);
        assert_eq!(decision.peak_cap_gain_db, 3.0);
        assert_eq!(decision.final_gain_db, -15.5);
        assert_eq!(decision.clamp_reason, "fallback_profile");
    }

    #[test]
    fn chirp_with_profile_uses_target_loudness_not_fallback_gain() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_silence_target_lufs: -41.0,
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
        assert_eq!(decision.target_lufs, -39.5);
        assert_eq!(decision.requested_gain_db, -24.5);
        assert_eq!(decision.final_gain_db, -24.5);
        assert_eq!(decision.clamp_reason, "target");
    }

    #[test]
    fn chirp_without_profile_uses_fallback_profile_not_fallback_gain() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig {
            assistant_offset_lu: 1.5,
            default_silence_target_lufs: -41.0,
            fallback_source_lufs: -24.0,
            fallback_source_peak_dbfs: -6.0,
            ..AssistantLoudnessConfig::default()
        });

        let decision = loudness.decide_gain(SegmentKind::Chirp, 0.0, None);

        assert_eq!(decision.baseline_lufs, -41.0);
        assert_eq!(decision.target_lufs, -39.5);
        assert_eq!(decision.requested_gain_db, -15.5);
        assert_eq!(decision.final_gain_db, -15.5);
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

    #[test]
    fn first_use_fallback_compensates_downstream_at_the_speaker_boundary() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        let context = VolumeContext {
            canonical_db: -36.4,
            downstream_db: -36.4,
            muted: false,
        };
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
        assert!((decision.target_speaker_lufs.unwrap() - -45.2).abs() < 0.01);
        assert!((decision.target_lufs - -8.8).abs() < 0.01);
        let achieved = decision.source_lufs + decision.final_gain_db + context.downstream_db;
        assert!((achieved - -45.2).abs() < 0.01);
    }

    #[test]
    fn held_assistant_repeats_without_reapplying_offset_and_tracks_user_delta() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        loudness.set_held_assistant(Some(HeldLoudnessReference {
            speaker_lufs: -39.0,
            canonical_db: -30.0,
        }));
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -41.0,
            Some(VolumeContext {
                canonical_db: -24.0,
                downstream_db: 0.0,
                muted: false,
            }),
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-24.0, -30.0)));

        assert_eq!(decision.reference_kind, ReferenceKind::HeldAssistant);
        assert_eq!(decision.target_speaker_lufs, Some(-33.0));
        assert_eq!(decision.target_lufs, -33.0);
    }

    #[test]
    fn content_reference_requires_three_seconds_and_silence_cannot_overwrite_it() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        loudness.update_volume_context(VolumeContext {
            canonical_db: -20.0,
            downstream_db: -20.0,
            muted: false,
        });
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) * 29 / 10));
        assert_eq!(loudness.held_content(), None);
        loudness.observe_content_period(&stereo_sine(0.08, (SAMPLE_RATE as usize) / 10));
        let held = loudness.held_content().expect("qualified music reference");

        let silence = vec![0i16; (SAMPLE_RATE as usize) * 12 * (CHANNELS as usize)];
        loudness.observe_content_period(&silence);
        assert_eq!(loudness.held_content(), Some(held));
    }

    #[test]
    fn live_gain_delta_cancels_when_camilla_carries_the_user_change() {
        let mut loudness = AssistantLoudness::new(AssistantLoudnessConfig::default());
        loudness.prepare_context_with_volume(
            "openai".to_string(),
            "gpt-realtime-2".to_string(),
            "marin".to_string(),
            -41.0,
            Some(VolumeContext {
                canonical_db: -30.0,
                downstream_db: -30.0,
                muted: false,
            }),
        );
        let decision =
            loudness.decide_gain(SegmentKind::Assistant, 0.0, Some(profile(-24.0, -12.0)));
        loudness.update_volume_context(VolumeContext {
            canonical_db: -24.0,
            downstream_db: -24.0,
            muted: false,
        });
        assert_eq!(loudness.live_gain_delta_db(&decision), 0.0);

        loudness.update_volume_context(VolumeContext {
            canonical_db: -18.0,
            downstream_db: -24.0,
            muted: false,
        });
        assert_eq!(loudness.live_gain_delta_db(&decision), 6.0);
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
