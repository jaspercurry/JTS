// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Developer-run tool to regenerate the `GOLDEN_*` constants pinned by
//! `golden_vector_is_stable` in `jasper_resampler::golden` — this crate's
//! own regression test that the resampler's output has not silently
//! changed. There is no cross-language contract test or C++ binding in
//! this repo; this is a Rust-only primitive.
//!
//! The fixture (input signal + ratios) is defined ONCE in
//! `jasper_resampler::golden`, so this example and the in-crate golden
//! test cannot drift apart.
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
