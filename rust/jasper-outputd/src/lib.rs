//! Core for the JTS outputd final-output owner.
//!
//! This crate models the contracts from
//! `docs/HANDOFF-speaker-output-reference.md`: production audio is already
//! mixed and processed before outputd, then outputd writes the final
//! electrical samples to the selected sink and publishes bounded monitor/
//! reference taps. Assistant/TTS ingress is owned by `jasper-fanin`; the
//! older outputd TTS IPC path has been retired. The outputd systemd unit
//! enables the ALSA transport.

pub mod alsa_backend;
pub mod config;
pub mod content_bridge;
pub mod core;
pub mod fake;
pub mod ledger;
pub mod loudness;
pub mod mixer;
pub mod reference;
pub mod state;
pub mod types;

pub use types::{CHANNELS, SAMPLE_RATE};
