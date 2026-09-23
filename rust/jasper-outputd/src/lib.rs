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
// `OutputCore`'s assistant engine and DAC sink: daemon code in both the ALSA
// and the parked runtime (`OutputCore::new_for_daemon`), not test doubles.
pub mod assistant_source;
pub mod chip_ref;
pub mod config;
// Edge-triggered "the output stage is emitting silence it did not intend"
// detector over whichever content source is live (#3458).
pub mod content_fill;
pub mod core;
// outputd's multi-room role: the `dac_content` reader — the round-trip lane
// a grouping member's snapclient feeds. The canonical design has CamillaDSP
// feed the snapserver pipe, not outputd.
pub mod dac_content;
pub mod fake;
pub mod ledger;
pub mod mixer;
// Ring B: the SHM ping-pong ring content-source reader — the one central
// transport from CamillaDSP to the DAC (ADR-0100).
pub mod shm_ring_source;
pub mod state;
pub mod tts;
pub mod types;

pub use types::{CHANNELS, SAMPLE_RATE};
