// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Core for the JTS outputd final-output owner.
//!
//! Production audio is already mixed and processed before outputd. Outputd writes
//! the final electrical samples to the selected sink and publishes bounded monitor/
//! reference taps. SOLO assistant/TTS ingress is owned by `jasper-fanin`
//! (pre-CamillaDSP). On a BONDED multiroom member, outputd itself serves
//! the TTS socket (`tts` module — fanin's wire-protocol twin) so the
//! member's own assistant voice mixes locally, post-round-trip, instead of
//! riding the synced stream.
//! The outputd systemd unit enables the ALSA transport.

pub use jasper_daemon::HELPER_STACK_BYTES;

pub mod aec_clock;
pub mod alsa_backend;
// The real per-period assistant render/gain engine (`AssistantSource`) plus
// the DAC-write sink `OutputCore` calls unconditionally each period
// (`FakeDacSink`) — both reachable from the real ALSA daemon path, not just
// tests. See the module doc and #1717.
pub mod assistant_source;
pub mod config;
// Edge-triggered "the output stage is emitting silence it did not intend"
// detector over whichever content source is live (#3458).
pub mod content_fill;
pub mod core;
// outputd's multi-room role: the `dac_content` reader (Increment 3) — the
// round-trip lane a grouping member's snapclient feeds. The canonical design
// has CamillaDSP feed the snapserver pipe, not outputd.
pub mod dac_content;
pub mod fake;
pub mod ledger;
pub mod loudness;
pub mod mixer;
// Ring B: the SHM ping-pong ring content-source reader — the one central
// transport from CamillaDSP to the DAC (ADR-0100).
pub mod shm_ring_source;
// Observe-only software-AEC reference clock drift estimator (research-doc
// increment 2): composes the shared jasper-clock DLL to measure :9891-reference
// vs DAC-playout drift in ppm. Never warps audio.
pub mod dac_clock;
pub mod state;
pub mod tts;
pub mod types;

pub use types::{CHANNELS, SAMPLE_RATE};
