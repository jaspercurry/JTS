// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The content source `OutputCore::step` reads. Not test-only:
//! `main.rs::run_fake` — the parked runtime `jasper-audio-hardware-reconcile`
//! selects with `JASPER_OUTPUTD_BACKEND=fake` when it recognizes no output DAC
//! — steps through it every period. `run_alsa` never reads it: it hands each
//! period in through `OutputCore::prepare_period_with_content`.

use std::collections::VecDeque;

use crate::types::ProgramSample;

pub struct FakeContentSource {
    periods: VecDeque<Vec<ProgramSample>>,
}

impl FakeContentSource {
    pub fn new() -> Self {
        Self {
            periods: VecDeque::new(),
        }
    }

    pub fn push_period(&mut self, samples: Vec<ProgramSample>) {
        self.periods.push_back(samples);
    }

    pub fn read_period(&mut self, out: &mut [ProgramSample]) {
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
