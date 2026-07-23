// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Fake transports for outputd unit tests and safe developer runs.

use std::collections::VecDeque;
use std::sync::Arc;

use crate::ledger::SegmentId;
use crate::loudness::{
    apply_gain_i16, gain_db_to_linear, sanitize_tts_gain_db, AssistantGainDecision,
    AssistantLoudness, GainRamp, MIN_TTS_GAIN_DB,
};

pub struct FakeContentSource {
    periods: VecDeque<Vec<i16>>,
}

impl FakeContentSource {
    pub fn new() -> Self {
        Self {
            periods: VecDeque::new(),
        }
    }

    pub fn push_period(&mut self, samples: Vec<i16>) {
        self.periods.push_back(samples);
    }

    pub fn read_period(&mut self, out: &mut [i16]) {
        out.fill(0);
        if let Some(samples) = self.periods.pop_front() {
            let copied = samples.len().min(out.len());
            out[..copied].copy_from_slice(&samples[..copied]);
        }
    }
}

impl Default for FakeContentSource {
    fn default() -> Self {
        Self::new()
    }
}

/// The assistant playout source. It no longer bakes a fixed linear gain at
/// enqueue time: each segment carries its gain *policy* (base gain, peak-cap
/// ceiling, and the loudness decision) and the gain is resolved PER PERIOD in
/// `read_period_into` through a shared [`GainRamp`], mute-force-silence, and
/// the live re-gain residual — mirroring fan-in's `mix_period`. This is what
/// gives outputd the same mute, live re-gain, and learned-envelope behaviour
/// fan-in has, so a grouped follower's replies track volume and mute exactly
/// like a solo speaker's.
pub struct FakeAssistantSource {
    segments: VecDeque<FakeAssistantSegment>,
    channels: usize,
    /// One continuous ramp across segment boundaries, so a mid-turn volume
    /// change glides and mute→unmute always ramps from silence.
    gain_ramp: GainRamp,
}

struct FakeAssistantSegment {
    id: SegmentId,
    samples: Vec<i16>,
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
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SegmentWrite {
    pub id: SegmentId,
    pub frames: u64,
}

impl FakeAssistantSource {
    pub fn new(channels: usize) -> Self {
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
        samples: Vec<i16>,
        base_gain_db: f32,
        peak_cap_gain_db: f32,
        decision: Option<Arc<AssistantGainDecision>>,
    ) {
        self.segments.push_back(FakeAssistantSegment {
            id,
            samples,
            cursor_samples: 0,
            base_gain_db,
            peak_cap_gain_db,
            peak_cap_linear: gain_db_to_linear(peak_cap_gain_db),
            decision,
        });
    }

    /// Render one period of gained assistant audio into `out`, recording the
    /// per-segment written-frame counts in `writes`. Gain is applied HERE,
    /// before the caller mixes `out` with the content period (so both the DAC
    /// and the AEC reference carry the gained speech — inv-A). The volume
    /// context is drained once per period before this call, so `muted` and the
    /// live residual are constant across the period; only the ramp advances
    /// per frame, exactly as fan-in's `mix_period` does.
    pub fn read_period_into(
        &mut self,
        out: &mut [i16],
        writes: &mut Vec<SegmentWrite>,
        loudness: &AssistantLoudness,
    ) {
        out.fill(0);
        writes.clear();
        let channels = self.channels;
        let muted = loudness
            .current_volume_context()
            .is_some_and(|context| context.muted);

        let mut current_write: Option<SegmentWrite> = None;
        for frame in out.chunks_exact_mut(channels) {
            // Advance to the front segment that still has samples, resolving
            // its (period-constant) target gain and peak-cap ceiling.
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

            let front = self
                .segments
                .front_mut()
                .expect("front segment present after gain resolution");
            for (channel, slot) in frame.iter_mut().enumerate() {
                *slot = apply_gain_i16(front.samples[front.cursor_samples + channel], gain);
            }
            front.cursor_samples += channels;

            // Record this frame against the current run of writes. SegmentWrite
            // is Copy, so we compute the "extend the current run" decision from
            // a short-lived immutable borrow before mutating — a `match
            // current_write { Some(ref mut ..) .. }` would mutate a COPY.
            let extend_current = matches!(&current_write, Some(write) if write.id == seg_id);
            if extend_current {
                current_write
                    .as_mut()
                    .expect("extend_current implies a current write")
                    .frames += 1;
            } else {
                if let Some(write) = current_write.take() {
                    writes.push(write);
                }
                current_write = Some(SegmentWrite {
                    id: seg_id,
                    frames: 1,
                });
            }
        }
        if let Some(write) = current_write.take() {
            writes.push(write);
        }
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
    pub periods: VecDeque<Vec<i16>>,
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

    pub fn write_period(&mut self, samples: &[i16]) {
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
