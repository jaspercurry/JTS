// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Emit the canonical cross-language resampler fixture as machine-readable
//! lines, so the Python contract test (`tests/test_resampler_contract.py`) can
//! shell out to it for the *Rust* reference output and compare it against the
//! C++/usbsink `jasper_resampler.RateResampler` to ≤1 LSB.
//!
//! The fixture (input signal + ratios) is defined ONCE in
//! `jasper_resampler::golden`, so this example, the in-crate golden test, and
//! the C++ side cannot drift apart.
//!
//! Output format (stable; parse by prefix):
//!
//! ```text
//! CHANNELS 2
//! INPUT <s0> <s1> ...        # interleaved i16, the canonical input
//! RATIO 1.0001
//! OUTPUT <s0> <s1> ...       # interleaved i16, one-shot resample at that ratio
//! RATIO 0.9999
//! OUTPUT ...
//! ```
//!
//! Run: `cargo run --example golden_vector` (add `--release` for speed; output
//! is bit-identical either way — the math is f64 and deterministic).

use jasper_resampler::{golden, resample_i16, SincTable};

fn main() {
    let table = SincTable::new();
    let input = golden::canonical_input();

    println!("CHANNELS {}", golden::CHANNELS);
    print!("INPUT");
    for s in &input {
        print!(" {s}");
    }
    println!();

    for ratio in golden::RATIOS {
        let out = resample_i16(&input, golden::CHANNELS, ratio, &table);
        // Print the ratio with enough precision to round-trip exactly.
        println!("RATIO {ratio:.10}");
        print!("OUTPUT");
        for s in &out {
            print!(" {s}");
        }
        println!();
    }
}
