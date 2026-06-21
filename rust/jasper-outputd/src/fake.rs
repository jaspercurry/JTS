// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Fake transports for outputd unit tests and safe developer runs.

use std::collections::VecDeque;

use crate::ledger::SegmentId;
use crate::mixer::apply_gain_i16;

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

pub struct FakeAssistantSource {
    segments: VecDeque<FakeAssistantSegment>,
    channels: usize,
}

struct FakeAssistantSegment {
    id: SegmentId,
    samples: Vec<i16>,
    cursor_samples: usize,
    gain_linear: f32,
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
        }
    }

    pub fn enqueue_segment(&mut self, id: SegmentId, samples: Vec<i16>, gain_linear: f32) {
        self.segments.push_back(FakeAssistantSegment {
            id,
            samples,
            cursor_samples: 0,
            gain_linear,
        });
    }

    pub fn read_period_into(&mut self, out: &mut [i16], writes: &mut Vec<SegmentWrite>) {
        out.fill(0);
        writes.clear();
        let mut out_cursor = 0usize;

        while out_cursor < out.len() {
            let Some(front) = self.segments.front_mut() else {
                break;
            };
            let remaining = front.samples.len() - front.cursor_samples;
            if remaining == 0 {
                self.segments.pop_front();
                continue;
            }

            let available = out.len() - out_cursor;
            let copied = remaining.min(available);
            let src_start = front.cursor_samples;
            let src_end = src_start + copied;
            for (dst, &src) in out[out_cursor..out_cursor + copied]
                .iter_mut()
                .zip(&front.samples[src_start..src_end])
            {
                *dst = apply_gain_i16(src, front.gain_linear);
            }
            front.cursor_samples += copied;
            out_cursor += copied;

            writes.push(SegmentWrite {
                id: front.id,
                frames: (copied / self.channels) as u64,
            });

            if front.cursor_samples == front.samples.len() {
                self.segments.pop_front();
            }
        }
    }

    pub fn read_period(&mut self, out: &mut [i16]) -> Vec<SegmentWrite> {
        let mut writes = Vec::new();
        self.read_period_into(out, &mut writes);
        writes
    }

    pub fn flush(&mut self) {
        self.segments.clear();
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
