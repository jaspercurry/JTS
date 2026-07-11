// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Core for the JTS outputd final-output owner.
//!
//! This crate models the contracts from
//! `docs/HANDOFF-speaker-output-reference.md`: production audio is already
//! mixed and processed before outputd, then outputd writes the final
//! electrical samples to the selected sink and publishes bounded monitor/
//! reference taps. SOLO assistant/TTS ingress is owned by `jasper-fanin`
//! (pre-CamillaDSP). On a BONDED multiroom member, outputd itself serves
//! the TTS socket (`tts` module — fanin's wire-protocol twin) so the
//! member's own assistant voice mixes locally, post-round-trip, instead of
//! riding the synced stream; see HANDOFF-multiroom.md Increment 5 PR-2.
//! The outputd systemd unit enables the ALSA transport.

pub mod aec_clock;
pub mod alsa_backend;
pub mod config;
pub mod content_bridge;
pub mod core;
// outputd's multi-room role: the `dac_content` reader (Increment 3) — the
// round-trip lane a grouping member's snapclient feeds. (The former
// `snapfifo` module — outputd-as-PRODUCER — was removed 2026-06-11: the
// canonical design has CamillaDSP feed the snapserver pipe, not outputd.
// See HANDOFF-multiroom.md §2 "Canonical signal flow".)
pub mod dac_content;
pub mod fake;
pub mod ledger;
pub mod loudness;
pub mod mixer;
pub mod reference;
// Ring B: the SHM ping-pong ring content-source reader, active only under
// JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring. Shipped default on eligible stereo
// topologies (P4 LANDED — docs/HANDOFF-audio-graph-consolidation.md); off
// elsewhere by resolved policy — nothing in the DAC loop touches it unless
// the flag selects it.
pub mod shm_ring_source;
// Observe-only software-AEC reference clock drift estimator (research-doc
// increment 2): composes the shared jasper-clock DLL to measure :9891-reference
// vs DAC-playout drift in ppm. Never warps audio.
pub mod dac_clock;
pub mod state;
pub mod tts;
pub mod types;

pub use types::{CHANNELS, SAMPLE_RATE};
