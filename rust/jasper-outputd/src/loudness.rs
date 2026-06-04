//! Streaming assistant/content loudness policy.
//!
//! The control target is K-weighted loudness, not raw PCM RMS. The
//! implementation intentionally keeps the state small: two biquads per
//! channel, period-level rolling windows, and no heap churn in the audio
//! loop beyond the bounded deques.

use std::collections::VecDeque;

use crate::mixer::clamp_tts_gain_db;
use crate::types::{AssistantProfile, SegmentKind, CHANNELS, SAMPLE_RATE};

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
}

pub struct AssistantLoudness {
    config: AssistantLoudnessConfig,
    content: KWeightedWindow,
    pending_context: Option<AssistantContext>,
    last_decision: Option<AssistantGainDecision>,
}

impl AssistantLoudness {
    pub fn new(config: AssistantLoudnessConfig) -> Self {
        Self {
            config,
            content: KWeightedWindow::new(CONTENT_ANCHOR_FRAMES),
            pending_context: None,
            last_decision: None,
        }
    }

    pub fn observe_content_period(&mut self, samples: &[i16]) {
        self.content.push_interleaved(samples);
    }

    pub fn prepare_context(
        &mut self,
        provider: String,
        model: String,
        voice: String,
        silence_target_lufs: f32,
    ) {
        let baseline_lufs = self.observed_content_lufs();
        self.pending_context = Some(AssistantContext {
            provider,
            model,
            voice,
            baseline_lufs,
            silence_target_lufs,
        });
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
        let observed_baseline_lufs = context
            .as_ref()
            .and_then(|ctx| ctx.baseline_lufs)
            .or_else(|| self.observed_content_lufs());
        let baseline_lufs = observed_baseline_lufs.unwrap_or_else(|| {
            context
                .as_ref()
                .map_or(self.config.default_silence_target_lufs, |ctx| {
                    ctx.silence_target_lufs
                })
        });
        let target_lufs = baseline_lufs + self.config.assistant_offset_lu;
        let confidence = profile.as_ref().map_or(0.0, |p| p.confidence);
        let source_lufs = profile
            .as_ref()
            .and_then(|p| p.source_lufs)
            .filter(|v| v.is_finite())
            .unwrap_or(self.config.fallback_source_lufs);
        let source_peak_dbfs = profile
            .as_ref()
            .and_then(|p| p.source_peak_dbfs)
            .filter(|v| v.is_finite())
            .unwrap_or(self.config.fallback_source_peak_dbfs);
        let requested_gain = target_lufs - source_lufs;
        let peak_cap_gain = self.config.max_peak_dbfs - source_peak_dbfs;
        let limited_gain = requested_gain.min(peak_cap_gain);
        let final_gain = clamp_tts_gain_db(limited_gain);
        let clamp_reason = if final_gain != limited_gain {
            "gain_clamp"
        } else if limited_gain != requested_gain {
            "peak_cap"
        } else if profile.as_ref().and_then(|p| p.source_lufs).is_none() {
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
            calibrated: profile.as_ref().and_then(|p| p.source_lufs).is_some(),
            profile_confidence: confidence,
            baseline_lufs,
            target_lufs,
            source_lufs,
            source_peak_dbfs,
            requested_gain_db: requested_gain,
            peak_cap_gain_db: peak_cap_gain,
            final_gain_db: final_gain,
            clamp_reason,
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

    fn observed_content_lufs(&self) -> Option<f32> {
        self.content
            .short_lufs()
            .or_else(|| self.content.anchor_lufs())
            .filter(|v| v.is_finite() && *v >= self.config.content_silence_lufs)
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

    fn push_interleaved(&mut self, samples: &[i16]) {
        debug_assert_eq!(samples.len() % (CHANNELS as usize), 0);
        if samples.is_empty() {
            return;
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
    }

    fn short_lufs(&self) -> Option<f32> {
        self.window_lufs(SHORT_TERM_FRAMES)
            .or_else(|| self.window_lufs(MOMENTARY_FRAMES))
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
            rlb: Biquad::new(
                1.0,
                -2.0,
                1.0,
                -1.99004745483398,
                0.99007225036621,
            ),
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
            let phase =
                2.0 * std::f32::consts::PI * 1000.0 * (n as f32) / (SAMPLE_RATE as f32);
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
        // absolute offset applies — landing at ~-12.03 LUFS
        // (-15.05 + 0.70 + 3.01 - 0.69 = -12.03). Bracket that true value tightly.
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
        assert_eq!(decision.final_gain_db, -6.0);
        assert_eq!(decision.clamp_reason, "gain_clamp");
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
}
