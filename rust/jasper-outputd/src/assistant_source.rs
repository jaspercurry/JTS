// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The real assistant-audio render/gain engine, plus the DAC-write sink
//! `OutputCore` calls unconditionally each period.
//!
//! `AssistantSource` is not a test double despite the `Fake*` company it keeps
//! here: it is the per-period gain-ramp/mute/reference render path
//! `OutputCore` runs on every real assistant reply, on both the
//! ALSA daemon path (`OutputCore::new_for_daemon`, `main.rs::run_alsa`) and
//! the safe-developer-run path (`main.rs::run_fake`).
//!
//! `FakeDacSink` keeps its name (deliberately — the next reader shouldn't
//! need to re-derive this): the only daemon-reachable construction
//! (`OutputCore::new_for_daemon`, used by both `run_alsa` and `run_fake`)
//! always passes `discarding()`, which sets `max_periods = Some(0)` — so
//! `write_period` returns immediately without storing anything, behaviorally
//! inert in every configuration the real binary can reach. The plain
//! accumulating constructor (`FakeDacSink::new()`, `max_periods = None`) is
//! reachable only from direct unit-test construction via `OutputCore::new()`,
//! never from `main.rs`. It moved here purely because
//! `OutputCore::commit_prepared_period_with_dac_delay` calls
//! `dac.write_period(..)` unconditionally, including on the daemon path —
//! not because its behavior changed. See #1717.

use std::collections::VecDeque;
use std::sync::Arc;

use crate::ledger::SegmentId;
use crate::loudness::{
    apply_gain, gain_db_to_linear, sanitize_tts_gain_db, AssistantGainDecision, AssistantLoudness,
    GainRamp, MIN_TTS_GAIN_DB,
};
use crate::types::ProgramSample;
use jasper_tts_protocol::{TtsAudioSamples, VolumeContext};

/// The playout facts captured when an assistant segment finishes rendering,
/// needed to complete its learned quiet-room reference: the decision, the
/// linear gain actually applied to its last frame (post any mid-segment live
/// re-gain), and the volume context in force at that frame.
#[derive(Debug, Clone)]
pub struct SegmentPlayback {
    pub decision: Arc<AssistantGainDecision>,
    pub last_gain_linear: f32,
    pub context: VolumeContext,
}

/// Emitted by `read_period_into` when an assistant-reference-eligible segment's
/// audio fully drains this period. `playback` is `None` when the segment must
/// NOT learn a reference — it was muted at some point, or had no volume context
/// to anchor to. The owner (`OutputCore`) pairs this with SEGMENT_END before
/// committing the reference.
#[derive(Debug, Clone)]
pub struct DrainedPlayback {
    pub id: SegmentId,
    pub playback: Option<SegmentPlayback>,
}

/// The assistant playout source. It no longer bakes a fixed linear gain at
/// enqueue time: each segment carries its gain *policy* (base gain, peak-cap
/// ceiling, and the loudness decision) and the gain is resolved PER PERIOD in
/// `read_period_into` through a shared [`GainRamp`], mute-force-silence, and
/// the live re-gain residual — mirroring fan-in's `mix_period`. This is what
/// gives outputd the same mute, live re-gain, and learned-envelope behaviour
/// fan-in has, so a grouped follower's replies track volume and mute exactly
/// like a solo speaker's.
pub struct AssistantSource {
    segments: VecDeque<AssistantSegment>,
    channels: usize,
    /// One continuous ramp across segment boundaries, so a mid-turn volume
    /// change glides and mute→unmute always ramps from silence.
    gain_ramp: GainRamp,
}

struct AssistantSegment {
    id: SegmentId,
    /// The TTS wire's own samples, kept at the width the wire declared.
    ///
    /// Deliberately NOT promoted at enqueue: a queued reply can be seconds
    /// long, and widening an S16 payload here would double the queue's resident
    /// bytes under `mlockall` for no resolution gain — nothing between the
    /// socket and here touches the samples. The widen happens once per sample
    /// at the gain application in `read_period_into`, which is the first stage
    /// that produces a spine sample at all. A box whose wire is already wide
    /// (`AUDIO32`) queues `Vec<i32>` and has nothing to widen; that box pays the
    /// doubled bytes because its samples genuinely carry them.
    samples: TtsAudioSamples,
    cursor_samples: usize,
    /// Loudness-decided gain for this segment before any live adjustment.
    base_gain_db: f32,
    /// Hearing/clip-safety ceiling (dB) — the ramp target is clamped to it.
    peak_cap_gain_db: f32,
    /// Precomputed linear form of `peak_cap_gain_db`, applied as a hard
    /// per-frame ceiling so a ramp can never overshoot the cap.
    peak_cap_linear: f32,
    /// The gain decision, used to compute the live re-gain residual for an
    /// absolute volume change since the segment started. `None` on the legacy
    /// GAIN+AUDIO path (no live tracking).
    decision: Option<Arc<AssistantGainDecision>>,
    /// Whether this segment may train the learned quiet-room reference (true
    /// only for Assistant-kind segments — cues/chirps never learn).
    reference_eligible: bool,
    /// Linear gain applied to the most recently rendered frame — the effective
    /// gain the reference is computed from at completion.
    last_gain_linear: f32,
    /// Volume context in force at the last rendered frame (`None` until a
    /// context exists — no reference is learned without one).
    last_context: Option<VolumeContext>,
    /// Set if any rendered frame was muted; disqualifies the reference so a
    /// silenced reply never trains loudness.
    disqualified: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SegmentWrite {
    pub id: SegmentId,
    pub frames: u64,
}

impl AssistantSource {
    pub fn new(channels: usize) -> Self {
        // PANIC-AUDITED: channels is the daemon's own CHANNELS constant at every real call site
        assert!(channels > 0, "channels must be > 0");
        Self {
            segments: VecDeque::new(),
            channels,
            gain_ramp: GainRamp::new(),
        }
    }

    pub fn enqueue_segment(
        &mut self,
        id: SegmentId,
        samples: impl Into<TtsAudioSamples>,
        base_gain_db: f32,
        peak_cap_gain_db: f32,
        decision: Option<Arc<AssistantGainDecision>>,
        reference_eligible: bool,
    ) {
        self.segments.push_back(AssistantSegment {
            id,
            samples: samples.into(),
            cursor_samples: 0,
            base_gain_db,
            peak_cap_gain_db,
            peak_cap_linear: gain_db_to_linear(peak_cap_gain_db),
            decision,
            reference_eligible,
            last_gain_linear: 0.0,
            last_context: None,
            disqualified: false,
        });
    }

    /// Render one period of gained assistant audio into `out`, recording the
    /// per-segment written-frame counts in `writes` and any assistant segments
    /// that finished draining this period in `drained` (for reference
    /// learning). Gain is applied HERE, before the caller mixes `out` with the
    /// content period (so both the DAC and the AEC reference carry the gained
    /// speech — inv-A). The volume context is drained once per period before
    /// this call, so `muted` and the live residual are constant across the
    /// period; only the ramp advances per frame, exactly as fan-in's
    /// `mix_period` does.
    ///
    /// `out` is at the program spine's width: each S16 wire sample is widened and
    /// gained in one step (`apply_gain` on the widened value, in f64), so the
    /// gained result carries the full spine resolution instead of being rounded
    /// back to an S16 grid before the mix.
    pub fn read_period_into(
        &mut self,
        out: &mut [ProgramSample],
        writes: &mut Vec<SegmentWrite>,
        loudness: &AssistantLoudness,
        drained: &mut Vec<DrainedPlayback>,
    ) {
        out.fill(0);
        writes.clear();
        let channels = self.channels;
        let period_context = loudness.current_volume_context();
        let muted = period_context.map_or(true, |context| context.muted);

        let mut current_write: Option<SegmentWrite> = None;
        for frame in out.chunks_exact_mut(channels) {
            // Advance to the front segment that still has samples, resolving
            // its (period-constant) target gain and peak-cap ceiling. Finished
            // segments are eager-popped as their last frame renders, so a
            // finished front here only occurs defensively (skip it).
            let (target_gain_db, peak_cap_linear, seg_id) = loop {
                let Some(front) = self.segments.front() else {
                    if let Some(write) = current_write.take() {
                        writes.push(write);
                    }
                    return; // no more audio: the rest of `out` stays silent
                };
                if front.cursor_samples >= front.samples.len() {
                    self.segments.pop_front();
                    if let Some(write) = current_write.take() {
                        writes.push(write);
                    }
                    continue;
                }
                // Live re-gain residual for an absolute volume change since the
                // gain was decided. Post-DSP the downstream term is zeroed
                // inside `live_gain_delta_db`, so a canonical/envelope change is
                // carried IN FULL here (nothing downstream will apply it).
                let residual = front
                    .decision
                    .as_deref()
                    .map_or(0.0, |decision| loudness.live_gain_delta_db(decision));
                let target = sanitize_tts_gain_db(
                    (front.base_gain_db + residual).min(front.peak_cap_gain_db),
                )
                .max(MIN_TTS_GAIN_DB);
                break (target, front.peak_cap_linear, front.id);
            };

            // Mute forces silence and re-arms the ramp; otherwise glide to the
            // target and clamp to the hard peak-cap ceiling every frame so
            // attenuation for hearing/clip safety takes effect immediately.
            let gain = if muted {
                self.gain_ramp.force_silent();
                0.0
            } else {
                self.gain_ramp.retarget(target_gain_db);
                self.gain_ramp.next_frame().min(peak_cap_linear)
            };

            let finished = {
                let Some(front) = self.segments.front_mut() else {
                    if let Some(write) = current_write.take() {
                        writes.push(write);
                    }
                    return;
                };
                for (channel, slot) in frame.iter_mut().enumerate() {
                    // Promote the wire sample into the spine, THEN gain it in
                    // f64. Gaining first at i16 and widening after would round
                    // every gained sample onto the S16 grid — the requantization
                    // the wide spine exists to remove. `spine_sample` is
                    // `widen_i16_to_i32` for an S16 payload (the shipped path,
                    // unchanged) and the identity for an S32 one.
                    let wide = front.samples.spine_sample(front.cursor_samples + channel);
                    *slot = apply_gain(wide, gain);
                }
                front.cursor_samples += channels;
                // Reference-completion telemetry: remember the effective gain
                // and context of the last rendered frame; a muted frame
                // permanently disqualifies the segment from learning.
                if front.reference_eligible {
                    if muted {
                        front.disqualified = true;
                    } else {
                        front.last_gain_linear = gain;
                    }
                    front.last_context = period_context;
                }
                front.cursor_samples >= front.samples.len()
            };
            if finished {
                self.finalize_front_completion(drained);
            }

            // Record this frame against the current run of writes. Borrow the
            // option in place so the Copy `SegmentWrite` is not mutated only
            // in a temporary match value.
            match current_write.as_mut() {
                Some(write) if write.id == seg_id => write.frames += 1,
                _ => {
                    if let Some(write) = current_write.take() {
                        writes.push(write);
                    }
                    current_write = Some(SegmentWrite {
                        id: seg_id,
                        frames: 1,
                    });
                }
            }
        }
        if let Some(write) = current_write.take() {
            writes.push(write);
        }
    }

    /// Pop the finished front segment and, if it is reference-eligible, record
    /// its completion. `playback` is `None` (no learning) when the segment was
    /// muted, never had a volume context, or carried no decision.
    fn finalize_front_completion(&mut self, drained: &mut Vec<DrainedPlayback>) {
        let Some(front) = self.segments.pop_front() else {
            return;
        };
        if !front.reference_eligible {
            return;
        }
        let playback = match (front.decision, front.last_context, front.disqualified) {
            (Some(decision), Some(context), false) => Some(SegmentPlayback {
                decision,
                last_gain_linear: front.last_gain_linear,
                context,
            }),
            _ => None,
        };
        drained.push(DrainedPlayback {
            id: front.id,
            playback,
        });
    }

    pub fn flush(&mut self) {
        self.segments.clear();
        // Reset the ramp so the first reply after a barge-in snaps to its
        // decided gain instead of gliding from the flushed segment's level.
        self.gain_ramp = GainRamp::new();
    }

    pub fn pending_frames(&self) -> u64 {
        self.segments
            .iter()
            .map(|segment| {
                let remaining_samples = segment.samples.len() - segment.cursor_samples;
                (remaining_samples / self.channels) as u64
            })
            .sum()
    }
}

pub struct FakeDacSink {
    pub periods: VecDeque<Vec<ProgramSample>>,
    max_periods: Option<usize>,
}

impl FakeDacSink {
    pub fn new() -> Self {
        Self {
            periods: VecDeque::new(),
            max_periods: None,
        }
    }

    pub fn discarding() -> Self {
        Self {
            periods: VecDeque::new(),
            max_periods: Some(0),
        }
    }

    pub fn write_period(&mut self, samples: &[ProgramSample]) {
        if self.max_periods == Some(0) {
            return;
        }
        if let Some(max_periods) = self.max_periods {
            while self.periods.len() >= max_periods {
                self.periods.pop_front();
            }
        }
        self.periods.push_back(samples.to_vec());
    }
}

impl Default for FakeDacSink {
    fn default() -> Self {
        Self::new()
    }
}
