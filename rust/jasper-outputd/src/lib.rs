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
// ⚠️ DEAD CODE: `snapfifo` is unwired (no `main.rs`/`config.rs` reference).
// Re-wiring it without a jasper-fanin music-only stream re-introduces the inv-3
// TTS-to-followers leak — see the module-level warning in `snapfifo.rs` and the
// BLOCKER in HANDOFF-multiroom.md §2.
pub mod snapfifo;
pub mod state;
pub mod types;

pub use types::{CHANNELS, SAMPLE_RATE};
