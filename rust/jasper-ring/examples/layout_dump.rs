// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Regenerate `rust/jasper-ring/layout.json`, the ring ABI the C ioplug and
//! `jasper.ring_assets` are pinned against:
//!
//! ```text
//! cd rust && cargo run -q -p jasper-ring --example layout_dump \
//!     > jasper-ring/layout.json
//! ```
//!
//! `jasper_ring::layout::tests::layout_json_is_committed` fails until the file
//! matches, so this is never something a reader has to remember to run.

fn main() {
    print!("{}", jasper_ring::layout_json());
}
