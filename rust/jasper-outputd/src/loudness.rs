//! Compatibility shim for the shared assistant/content loudness policy.
//!
//! Outputd still owns final-output assistant mixing for bonded members.
//! The K-weighted loudness engine itself is shared with fan-in through
//! `jasper-tts-protocol` so the two daemon paths cannot drift.

pub use jasper_tts_protocol::loudness::*;
