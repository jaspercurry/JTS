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
// NOTE: the former `snapfifo` module (`SnapfifoSink`, the outputd-as-producer
// tap) was REMOVED 2026-06-11: the canonical multi-room design has CamillaDSP
// feed the snapserver pipe, not outputd — see HANDOFF-multiroom.md §2
// "Canonical signal flow" + "Stranded by this design". outputd's multi-room
// role is the Increment 3 `dac_content` reader (self-reported on STATUS).
pub mod state;
pub mod types;

pub use types::{CHANNELS, SAMPLE_RATE};
