// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Fake transports for outputd unit tests and safe developer runs.
//!
//! `FakeAssistantSource` (renamed `AssistantSource`) and `FakeDacSink` used to
//! live here too. Both are reachable from the real ALSA daemon path, not just
//! tests/dev runs, so #1717 moved them to `assistant_source` — that misled a
//! reader grepping for the assistant render path into skipping this file.
//! `FakeContentSource` genuinely is test/dev-only: `OutputCore`'s daemon path
//! (`run_alsa`) always calls `prepare_period_with_content`, which bypasses
//! `self.content` entirely, so `read_period` below only ever executes from
//! `OutputCore::step()` — the safe-developer-run backend (`main.rs::run_fake`)
//! and unit tests.

use std::collections::VecDeque;

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
