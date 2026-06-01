//! Core for the JTS outputd final-output owner.
//!
//! This crate models the contracts from
//! `docs/HANDOFF-speaker-output-reference.md`: content plus assistant
//! audio are mixed once, written to the output sink, copied to bounded
//! reference consumers, and accounted for in a playout ledger. The
//! outputd systemd unit enables the ALSA transport.

pub mod alsa_backend;
pub mod config;
pub mod core;
pub mod fake;
pub mod ledger;
pub mod loudness;
pub mod mixer;
pub mod protocol;
pub mod reference;
pub mod state;
pub mod types;

pub use types::{CHANNELS, SAMPLE_RATE};
