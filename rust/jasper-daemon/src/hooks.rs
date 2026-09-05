// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! What a hosting daemon supplies to shared code that has to speak in that
//! daemon's voice.

/// The `event=` / thread-name prefix, the writer thread's stack budget, and the
/// three emit paths — fan-in routes them through the `log` crate, outputd
/// writes stderr directly, so shared code cannot pick a logger of its own.
#[derive(Clone, Copy)]
pub struct DaemonHooks {
    pub event_prefix: &'static str,
    pub writer_stack_bytes: usize,
    pub info: fn(&str),
    pub warn: fn(&str),
    pub error: fn(&str),
}
