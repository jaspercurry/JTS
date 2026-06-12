//! Compatibility shim for the shared assistant/content loudness policy.
//!
//! Fan-in still owns pre-DSP TTS mixing. The K-weighted loudness engine
//! itself is shared with outputd through `jasper-tts-protocol` so the two
//! daemon paths cannot drift.

pub use jasper_tts_protocol::loudness::*;
