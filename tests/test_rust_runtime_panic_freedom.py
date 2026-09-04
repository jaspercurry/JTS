# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the audited panic-freedom of the Rust audio daemons' runtime paths.

A 2026-06 audit manually verified that every ``.unwrap()`` / ``.expect(``
/ ``panic!`` in ``rust/jasper-fanin``, ``rust/jasper-outputd``, and the shared
``rust/jasper-tts-protocol`` crate lives either in ``#[cfg(test)]``
code or at one of a handful of documented invariant sites. The two
daemons are the speaker's always-on audio path on a production Pi, and
``jasper-tts-protocol`` is a library compiled *into both* of them (the
TTS wire protocol + the shared loudness engine), so a panic there is a
panic in the audio runtime just the same — an unguarded panic in runtime
code kills audio output until systemd restarts the unit, so "no new
panics outside test code" is a safety invariant worth pinning, not a
style preference.

Issue #1718: the original scan matched ``.unwrap()`` / ``.expect(`` /
``panic!`` but not the ``assert!`` family — ``assert!``, ``assert_eq!``,
``assert_ne!``, and ``debug_assert!`` (and, caught by the same regex,
``debug_assert_eq!`` / ``debug_assert_ne!``) — which is the same panic
mechanism under a different macro name. In this workspace's release profile
(``panic = "abort"``, ``rust/jasper-outputd/Cargo.toml``) ``assert!`` /
``assert_eq!`` / ``assert_ne!`` abort the daemon outright, same as an
unguarded ``.expect(``. The ``debug_assert!`` sub-family is the one place
this differs from the issue's original framing: with no ``debug-assertions
= true`` override, it compiles to a no-op in that release profile. This
repo's own CI (``.github/workflows/tests.yml``) runs ``cargo test --release
--locked`` for every crate, so CI's own test run does not exercise
``debug_assert!`` either — the coverage this guard adds for that
sub-family is an unqualified local ``cargo test`` (no ``--release``) or any
debug-profile developer run (see ``fake.rs``'s "safe developer runs"),
neither of which any existing gate checks. It stays in scope for that gap.

Issue #2251: the scan still missed ``unreachable!``, ``todo!``, and
``unimplemented!`` — all three expand to ``panic!`` at compile time, so a
static source scan that doesn't name them lets any of the three sail
through unmatched. Unlike ``.expect(`` / the ``assert!`` family, none of
the three is a conditional guard with a legitimate invariant-documenting
use: reaching any of them panics unconditionally, exactly like a bare
``panic!``. They join ``.unwrap()`` / ``panic!`` in the intentionally
empty-allowlist category rather than getting an allowlist of their own —
see ``_BARE_PANIC_PAT`` below. A full sweep of every crate in
``RUNTIME_CRATES`` (and, for completeness, the rest of ``rust/``) at the
time of this fix found zero actual call sites; the sole textual hit was a
``///`` doc-comment in ``jasper-outputd/src/alsa_backend.rs`` *naming* the
macro in prose (already outside this scanner's reach, since comment text
is stripped before matching), not an invocation.

CI builds and ``cargo test``s these crates, but cargo cannot run in
every dev environment and nothing in cargo's gate distinguishes a
test-only ``unwrap`` from a runtime one. This guard is the
static-source twin (same technique as ``tests/test_outputd_wiring.py``):

- ``.unwrap()``, ``panic!``, ``unreachable!``, ``todo!``, and
  ``unimplemented!`` are banned outright in runtime code (zero current
  uses — the allowlist for them is intentionally empty).
- ``.expect("...")`` is allowed only for the audited invariant sites
  listed in ``ALLOWED_EXPECTS``, keyed by (file, message) so the pin
  survives line-number churn but a *new* expect still fails.
- The ``assert!`` family is allowed only for the audited invariant sites
  listed in ``ALLOWED_ASSERTS``, keyed by (file, key) the same way. ``key``
  is the macro's own trailing string message when it has one (identical
  convention to ``ALLOWED_EXPECTS``); ``assert_eq!``/``assert_ne!`` often
  carry no message, so when there is none, ``key`` falls back to the
  macro's normalized argument text instead — still line-number-independent,
  just not a human-authored phrase. The statement is joined across lines
  first (paren-depth tracked), so a message on a later line
  (``assert!(\n    cond,\n    "message"\n);``) is still found.
- Two-sided ratchet for both allowlists: a stale entry (the audited site
  was removed, or its key text changed) also fails, so each list only
  shrinks.

``jasper-ring``, ``jasper-resampler``, ``jasper-clock``, and
``jasper-host-clock`` ARE real ``path`` dependencies of one or both daemons
and are scanned here for the same reason the daemons' own crates are:
their code is compiled into and executes as part of the shipped binaries.
"On the default runtime path" is deliberately not read narrowly as "doing
its feature's user-visible job" -- construction/status-seeding code that
runs unconditionally counts too, exactly like an always-constructed
``PlayoutLedger`` counts here even though it is a one-time startup call, not
a hot loop (issue #1718's own audit verified this per crate rather than
trusting a name):

- ``jasper-ring`` looks like an opt-in prototype from its own module doc
  ("Ring B prototype") but the ring is the ONLY central transport
  (``docs/adr/0100-one-audio-transport.md``) -- so this crate's
  ``RingReader``/``RingWriter`` code IS the default runtime path, not a
  corpus/lab-only affair.
- ``jasper-resampler`` is used unconditionally in ``jasper-fanin``'s mixer
  for per-lane rate matching (``LaneResampler``) and general RMS/format
  utilities (``rms_dbfs_i16``, ``convert_s32_to_s16``), independent of
  coupling mode.
- ``jasper-clock``'s ``Dll`` is constructed unconditionally as part of
  ``jasper-outputd``'s ``State`` (``sro_estimator``, ``dac_clock`` fields
  built on every daemon start) and is the control law inside
  ``jasper-resampler``'s ``RateController``.
- ``jasper-host-clock`` reads "Default OFF" in its own module doc, and the
  combo-mode servo THREAD genuinely is gated behind
  ``JASPER_FANIN_HOST_CLOCK=enabled`` AND USB Direct armed (both non-default)
  -- but ``jasper_host_clock::HostClock::new(...).status_fragment()`` runs
  unconditionally at fan-in startup to seed the disabled ``/state`` fragment
  even when the feature itself never arms, so the crate's code still
  executes on every boot.
- ``jasper-env`` (added per PR #2270's gate SF2) is a ``path`` dependency of
  BOTH daemons (``jasper-fanin/Cargo.toml``, ``jasper-outputd/Cargo.toml``)
  and its env-parsing helpers (``env_str``, ``env_parse``) run on every
  daemon startup to read config -- it was a real gap against this section's
  own inclusion criterion, not a deliberate exclusion. Its only panic-family
  constructs are ``assert!``/``assert_eq!`` calls inside ``#[cfg(test)] mod
  tests``, so
  adding it needed no new ``ALLOWED_EXPECTS``/``ALLOWED_ASSERTS`` entries.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[1]

RUNTIME_CRATES = (
    "jasper-fanin",
    "jasper-outputd",
    "jasper-tts-protocol",
    "jasper-ring",
    "jasper-resampler",
    "jasper-clock",
    "jasper-host-clock",
    "jasper-env",
)

# Audited runtime ``.expect("...")`` sites, keyed by
# (path relative to rust/, exact message). Each entry carries the
# audit rationale; extending this list requires the same justification.
ALLOWED_EXPECTS: dict[tuple[str, str], str] = {
    (
        "jasper-fanin/src/watchdog.rs",
        "heartbeat thread spawn failed",
    ): (
        "Startup-time thread spawn before READY=1; fail-fast at boot is "
        "the correct behaviour — systemd restarts the unit."
    ),
    (
        "jasper-outputd/src/ledger.rs",
        "unknown playout segment id",
    ): (
        "Internal-invariant lookup: SegmentIds are minted by the ledger "
        "itself, so a miss is ledger corruption (the documented known "
        "exception from the audit)."
    ),
    (
        "jasper-outputd/src/main.rs",
        "chip ref downsampler is present when chip_tx is present",
    ): (
        "Construction invariant: chip_tx and chip_downsampler are "
        "created together; the message states the invariant."
    ),
    (
        "jasper-outputd/src/main.rs",
        "abstract notify socket must start with @",
    ): (
        "Caller dispatches on the '@' prefix before calling "
        "notify_systemd_abstract, so the strip_prefix cannot miss."
    ),
    (
        "jasper-fanin/src/mixer.rs",
        "aloop resampler lane always has Some(pcm)",
    ): (
        "Routing invariant: read_into_resampler_and_render is reached only for "
        "an aloop resampler lane (input.direct.is_none()), which always opens "
        "Some(pcm); the USB DIRECT lane (pcm None) routes to "
        "read_direct_and_render instead. The function's own is_none() early "
        "return guards the top; the message states the invariant."
    ),
    (
        "jasper-fanin/src/mixer/direct_capture.rs",
        "read_direct_and_render only called on a direct lane",
    ): (
        "Routing invariant: step() calls read_direct_and_render only when "
        "input.direct.is_some(), so the take() cannot yield None. The message "
        "states the invariant."
    ),
    (
        "jasper-fanin/src/main.rs",
        "tap receiver taken exactly once",
    ): (
        "Startup-time construction invariant: the tap channel receiver is "
        "taken exactly once, immediately after Mixer::new, before READY=1. "
        "Fail-fast at boot is correct — systemd restarts the unit."
    ),
    (
        "jasper-fanin/src/json.rs",
        "serializing a string to JSON cannot fail",
    ): (
        "Serialization invariant: Rust str is valid UTF-8 and its Serialize "
        "implementation only calls serialize_str; serde_json::to_string uses "
        "an in-memory Vec writer whose writes cannot return an error. The "
        "other documented serde_json failure modes (custom Serialize errors "
        "and non-string map keys) cannot apply to &str."
    ),
    (
        "jasper-outputd/src/json.rs",
        "serializing a string cannot fail",
    ): (
        "Serialization invariant: Rust str is valid UTF-8 and its Serialize "
        "implementation only calls serialize_str; serde_json::to_string uses "
        "an in-memory Vec writer whose writes cannot return an error."
    ),
    # SF4 (issue #1718 gate round): jasper-ring joined RUNTIME_CRATES above.
    (
        "jasper-ring/src/lib.rs",
        "mapped ring geometry was validated before slot access",
    ): (
        "Construction invariant: RingMapping's geometry is validated once, "
        "at attach time, before any RingMapping value can exist at all -- "
        "slot_ptr can only be called on an already-attached mapping, so "
        "slot_bytes() cannot be None by the time this runs."
    ),
}

# Audited runtime ``assert!``-family sites, keyed by (path relative to
# rust/, key). ``key`` is the macro's own trailing string message where it
# has one (same convention as ALLOWED_EXPECTS above); where it does not
# (bare ``assert_eq!(a, b)`` with no message argument), ``key`` is the
# macro's normalized argument text instead — see _assert_key(). Surfaced by
# issue #1718, which found these were unmatched by the original
# ``.unwrap()``/``.expect(``/``panic!``-only pattern.
ALLOWED_ASSERTS: dict[tuple[str, str], str] = {
    (
        "jasper-ring/src/lib.rs",
        "ring publish requires exactly one complete slot",
    ): (
        "Caller-contract invariant: RingWriter validates every public write "
        "against the attached ring geometry before this mapping helper copies "
        "it. A mismatch here is an internal caller bug; continuing would risk "
        "a partial slot publication across the shared-memory boundary."
    ),
    (
        "jasper-outputd/src/assistant_source.rs",
        "channels must be > 0",
    ): (
        "Construction invariant: AssistantSource::new's channels argument "
        "is the daemon's own compile-time CHANNELS constant at every real call "
        "site (OutputCore::with_dac), never externally supplied input."
    ),
    (
        "jasper-outputd/src/core.rs",
        "period frames must be > 0",
    ): (
        "Construction invariant: OutputCore::with_dac's period_frames comes "
        "from the daemon's own config-derived period-size constant at every "
        "real call site, never externally supplied input."
    ),
    (
        "jasper-outputd/src/core.rs",
        "samples.len(), self.output_buf.len()",
    ): (
        "Caller-contract invariant: push_content_period's fixed-size "
        "output_buf is sized once at construction from the format/period "
        "config; a mismatch here is a caller bug (this fake source's only "
        "callers are the daemon's own content-bridge and tests), not a "
        "runtime condition external input can trigger."
    ),
    (
        "jasper-outputd/src/core.rs",
        "samples.len() % (self.format.channels as usize), 0",
    ): (
        "Frame-alignment invariant: assistant audio must arrive as whole "
        "interleaved frames. Every real producer (the daemon's own TTS/cue "
        "rendering) emits whole frames by construction; a remainder here is "
        "a caller bug, not a runtime condition."
    ),
    (
        "jasper-outputd/src/core.rs",
        "samples.len(), self.content_buf.len()",
    ): (
        "Same shape as push_content_period above: prepare_period_with_"
        "content's content_buf is fixed-size from period config, so a "
        "mismatch is a caller bug, not a runtime condition."
    ),
    (
        "jasper-outputd/src/core.rs",
        "prepared output period was not committed",
    ): (
        "Period-lifecycle invariant: prepare_period{,_with_content} must not "
        "be called again before the prior prepared period is committed "
        "(commit_prepared_period_with_dac_delay resets the ready flag). The "
        "daemon's own period loop calls prepare then commit in strict "
        "alternation; this is a reentrancy guard on that internal contract."
    ),
    (
        "jasper-outputd/src/core.rs",
        "output period must be prepared before commit",
    ): (
        "Mirror of the prepare-side guard above: commit_prepared_period_"
        "with_dac_delay must not run before a matching prepare_period call "
        "in the same period-loop iteration."
    ),
    # Issue #1718 listed 7 illustrative core.rs/fake.rs sites (above, part of
    # the 28 total); running the widened scanner against the three original
    # runtime crates (jasper-fanin, jasper-outputd, jasper-tts-protocol --
    # RUNTIME_CRATES above has since grown to seven, see the SF4 entries
    # further below) surfaced 28 total: the 7 illustrative sites above, plus
    # these 21 more below.
    (
        "jasper-fanin/src/lane_resampler.rs",
        "out.len(), self.period_frames * self.channels",
    ): (
        "Caller-contract invariant: render_period's own doc comment states "
        "the exact contract (length period_frames x channels); its only "
        "real caller is the daemon's own per-period render loop, which "
        "always sizes the buffer correctly."
    ),
    (
        "jasper-fanin/src/mixer/dsp.rs",
        "sum.len(), input.len()",
    ): (
        "Buffer-sizing invariant on the per-period mix accumulator "
        "(\"Pulled out for unit testability — no ALSA needed\" per its own "
        "comment); the daemon allocates both buffers once at a fixed "
        "period size."
    ),
    (
        "jasper-fanin/src/mixer/dsp.rs",
        "channels >= 1",
    ): (
        "ramp_program_duck's channels argument is the daemon's own fixed "
        "channel-count constant at its only call site, never externally "
        "supplied."
    ),
    (
        "jasper-fanin/src/mixer/dsp.rs",
        "sum.len(), out.len()",
    ): (
        "saturate_to_i16, same shape as mix_into above (\"Pulled out for "
        "unit testability\"): buffer-sizing invariant on fixed-size "
        "period buffers."
    ),
    (
        "jasper-fanin/src/mixer/dsp.rs",
        "a spine-scale lane may only enter a spine-scale sum",
    ): (
        "Numeric-scale invariant on mix_into_wide (U2 / #2223): a lane whose "
        "period is i32 spine-scale may only be added to a spine-scale sum. "
        "A mismatch is a wiring bug in the daemon's own code, not a runtime "
        "condition external input can produce, and it is stated as an assert "
        "because the failure it names is silent and severe -- one source "
        "contributing 96 dB louder than the rest of the mix. What actually "
        "prevents it is not this assert (the release profile compiles debug "
        "assertions out): the width is resolved ONCE, from one pure predicate "
        "(Config::program_wire_is_wide -> ProgramWidth::from_config), and the "
        "same value decides the lane's buffer (lane_wants_spine_buffer, unit-"
        "tested) and the sum entry. Mixer::new then cross-checks that single "
        "derivation against BOTH consumers -- the ring's attached header and "
        "the lanes' actual allocations -- in program_width_disagreement, and "
        "refuses to construct on any disagreement. This assert is the "
        "developer-run echo of that check, not the mechanism."
    ),
    (
        "jasper-fanin/src/mixer/direct_capture.rs",
        "narrow.map_or(true, |n| n.len() == raw.len())",
    ): (
        "Buffer-sizing invariant on push_capture_chunk (U2 / #2223), the same "
        "shape as the mixer's mix_into/saturate_to_i16 entries above: its one "
        "production caller passes the two views of a single just-read chunk "
        "(scratch[..got] and narrow_scratch[..got]) built from the same got, "
        "so the lengths cannot diverge at runtime. `narrow` is None exactly "
        "when no narrowed view was computed (a wide lane with the tap "
        "disarmed), which the map_or default admits; the debug_assert states the "
        "relationship for a developer run."
    ),
    (
        "jasper-fanin/src/mixer/direct_capture.rs",
        "the narrow route must supply its narrowed view",
    ): (
        "Routing invariant on push_capture_chunk's narrow arm (U2 / #2223): "
        "the caller computes the narrowed view whenever `armed || !wide`, so "
        "on the narrow route it is always Some. Deliberately a debug_assert "
        "next to an early RETURN rather than an unwrap: in a release build "
        "this degrades to dropping one already-read chunk (the resampler "
        "underfills and renders silence, and the lane recovers on the next "
        "period) instead of aborting the audio daemon over a wiring bug."
    ),
    (
        "jasper-fanin/src/mixer/dsp.rs",
        "out.len(), sum.len() * WIDE_BYTES_PER_SAMPLE",
    ): (
        "fill_wide_ring_payload, the S32LE-ring twin of saturate_to_i16 "
        "above: same buffer-sizing invariant, one sample wider. Both "
        "buffers are allocated once in Mixer::new from the same "
        "period_samples, so the sizes cannot diverge at runtime; the "
        "debug_assert states the relationship for a developer run."
    ),
    (
        "jasper-fanin/src/mixer.rs",
        "period_frames > 0",
    ): (
        "catchup_drain_periods (\"Pure (no ALSA) for unit testability\"): "
        "period_frames is the daemon's fixed period-size config, not "
        "external input."
    ),
    (
        "jasper-fanin/src/playout.rs",
        "sample rate must be > 0",
    ): (
        "Construction invariant, mirrors jasper-outputd/src/ledger.rs's "
        "own PlayoutLedger::new (below): sample_rate is the daemon's fixed "
        "SAMPLE_RATE constant at every real call site, never externally "
        "supplied."
    ),
    (
        "jasper-fanin/src/tts.rs",
        "ledger flushed-frame total must equal the cleared audio queue depth",
    ): (
        "Internal cross-check between two values computed from the SAME "
        "flush operation a few lines above (the ledger's own event totals "
        "vs the queue's actual cleared depth) within one function; a "
        "mismatch means the ledger and the queue disagree about what was "
        "just flushed — a bookkeeping bug in this function, not an "
        "external or runtime condition."
    ),
    (
        "jasper-outputd/src/dac_content.rs",
        "ChannelPick::Sub applied without a low-pass filter",
    ): (
        "A deliberate fail-closed contract trap, not a bare precondition: "
        "the surrounding code's own comment says \"missing filter state is "
        "a construction bug, not a bypass. Fail closed to silence ... and "
        "warn.\" debug_assert!(false, ...) crashes loudly in dev/test "
        "builds to catch the construction bug early; the eprintln! + "
        "mute-to-silence right after it is the release-build path (where "
        "debug_assert is a no-op) — both halves of one two-tier design, "
        "not an oversight."
    ),
    (
        "jasper-outputd/src/dac_content.rs",
        "out.len() * 2, self.period_bytes",
    ): (
        "pop_period's own doc comment states the exact contract "
        "(\"out.len() * 2 == period_bytes\"); its only real caller is this "
        "file's own FifoReader::fill, which always sizes the buffer from "
        "the same period_bytes field."
    ),
    (
        "jasper-outputd/src/ledger.rs",
        "sample rate must be > 0",
    ): (
        "Construction invariant, mirrors jasper-fanin/src/playout.rs's own "
        "PlayoutLedger::new (above): sample_rate is the daemon's fixed "
        "SAMPLE_RATE constant at every real call site, never externally "
        "supplied."
    ),
    (
        "jasper-outputd/src/main.rs",
        "samples_4ch.len() % 4, 0",
    ): (
        "fold_reference_pairwise_composite: internal fixed-shape "
        "invariant — the function is only ever called with a 4-channel "
        "composite buffer sized by the daemon's own fixed geometry, never "
        "an externally supplied length."
    ),
    (
        "jasper-outputd/src/main.rs",
        "out_stereo.len(), (samples_4ch.len() / 4) * (CHANNELS as usize)",
    ): (
        "Same function as above: output buffer-sizing invariant — "
        "out_stereo is allocated by the same caller from the same fixed "
        "geometry."
    ),
    (
        "jasper-outputd/src/main.rs",
        "channels >= 1",
    ): (
        "fold_reference: channels comes from the daemon's own fixed "
        "content-channel-count config at its only call site."
    ),
    (
        "jasper-outputd/src/main.rs",
        "content_nch.len() % channels, 0",
    ): (
        "Frame-alignment invariant: content_nch is produced by the "
        "daemon's own content pipeline, which always emits whole frames."
    ),
    (
        "jasper-outputd/src/main.rs",
        "out_stereo.len(), (content_nch.len() / channels) * (CHANNELS as usize)",
    ): (
        "Same function as above: output buffer-sizing invariant."
    ),
    (
        "jasper-outputd/src/mixer.rs",
        "content.len(), assistant.len()",
    ): (
        "mix_saturating (module doc: \"the same intentionally boring "
        "saturation behavior as jasper-fanin\"): both buffers are "
        "period-sized scratch the daemon allocates once at a fixed period "
        "size."
    ),
    (
        "jasper-outputd/src/mixer.rs",
        "content.len(), out.len()",
    ): (
        "Same function as above, same shape."
    ),
    (
        "jasper-outputd/src/shm_ring_source.rs",
        "the ring wire is LE",
    ): (
        "Compile-time const-context assert (`const _: () = assert!(...)`), "
        "same shape as jasper-host-clock's gain bound below: it evaluates "
        "at compile time on a cfg! literal and emits no runtime branch. It "
        "pins the one assumption read_period's wide arm makes -- that the "
        "host's native i32 byte order already is the S32LE wire's -- so a "
        "big-endian port fails to build instead of byte-swapping audio."
    ),
    (
        "jasper-tts-protocol/src/loudness.rs",
        "samples.len() % (CHANNELS as usize), 0",
    ): (
        "KWeightedWindow::push_interleaved: frame-alignment invariant, "
        "same shape as the others above — every real producer emits whole "
        "interleaved frames."
    ),
    # SF4 (issue #1718 gate round): jasper-ring and jasper-host-clock joined
    # RUNTIME_CRATES above, verified on the default runtime path (see the
    # module docstring's per-crate evidence) rather than assumed off. All
    # unsafe pointer/atomic accesses below are internal SPSC-ring plumbing:
    # every offset is a fixed layout::OFF_* constant pinned against the C
    # header by the golden-layout test, and every length comes from the
    # SAME geometry the mapping/writer/reader was constructed with -- never
    # externally supplied.
    (
        "jasper-ring/src/lib.rs",
        "offset + 8 <= HEADER_BYTES",
    ): (
        "header_atomic's bounds precondition for the unsafe atomic access "
        "right below it. Every call site passes a fixed layout::OFF_* "
        "constant, never a computed or externally supplied offset."
    ),
    (
        "jasper-ring/src/lib.rs",
        "offset % 8, 0",
    ): (
        "header_atomic's alignment precondition for the same unsafe atomic "
        "access: the fixed layout::OFF_* constants are 8-byte aligned by "
        "construction (the golden-layout test pins every header offset)."
    ),
    (
        "jasper-ring/src/lib.rs",
        "offset + 4 <= HEADER_BYTES",
    ): (
        "One entry deliberately covers TWO sites with byte-identical "
        "condition text: header_u32's read-side precondition and "
        "write_u32's write-side precondition (the reader and writer each "
        "have their own header accessor). Both are the same bounds check "
        "as header_atomic above, for the u32 (not atomic) header fields; "
        "both call sites pass a fixed layout::OFF_* constant."
    ),
    (
        "jasper-ring/src/lib.rs",
        "off + slot_bytes <= self.len",
    ): (
        "slot_ptr's bounds precondition for the unsafe pointer arithmetic "
        "right below it. The SAFETY comment states the caller guarantee: "
        "slot_index < n_slots via seq % n_slots, and the mapping is sized "
        "HEADER_BYTES + n_slots*slot_bytes, so off + slot_bytes cannot "
        "exceed self.len when that guarantee holds."
    ),
    (
        "jasper-ring/src/lib.rs",
        "ring consume requires exactly one complete slot",
    ): (
        "try_consume_slot_bytes's own doc comment states the exact contract "
        "(\"out.len() must equal Geometry::slot_bytes\"); its only real "
        "caller is outputd's shm_ring_source.rs -- through the "
        "try_consume_slot wrapper on an S16 ring, directly on an S32 one -- "
        "and both arms size the buffer from the same geometry the mapping "
        "was attached with. Replaces the pre-byte-API entry keyed on "
        "\"out.len(), g.samples_per_slot()\": the same check, moved onto the "
        "byte path and given a message, since a slot is measured in bytes "
        "once the ring carries formats other than S16."
    ),
    (
        "jasper-ring/src/lib.rs",
        "the i16-typed slot view is only valid on an S16LE ring",
    ): (
        "One site (RingMapping::debug_assert_s16_typed_view) deliberately "
        "covers all three i16-typed wrappers -- write_i16_slot, "
        "RingReader::try_consume_slot, RingWriter::publish -- which measure "
        "their buffers in 2-byte samples and are therefore correct only on "
        "an S16LE geometry. Debug-only tripwire on an internal caller "
        "contract: the geometry is the one the mapping was constructed "
        "with, never externally supplied, and the byte-path entry points "
        "carry no such restriction. It stays DEBUG-only deliberately: the "
        "guard against a typed wrapper being used on a wide ring is the "
        "wire-format contract test in jasper-fanin's mixer "
        "(wide_ring_slots_carry_the_left_justified_narrow_slots, which "
        "publishes and reads back both wires), not this tripwire -- the "
        "tripwire is developer feedback on a debug run, so promoting it to "
        "a release assert would add a panic site in the audio path without "
        "adding coverage."
    ),
    (
        "jasper-ring/src/lib.rs",
        "the i32-typed slot view is only valid on an S32LE ring",
    ): (
        "The wide twin of the i16 tripwire above, covering "
        "RingReader::try_consume_slot_wide -- which measures its buffer in "
        "4-byte samples and is therefore correct only on an S32LE geometry. "
        "Same audit in every respect: debug-only, on a geometry the mapping "
        "was constructed with rather than external input, with the real "
        "guard being the wire-format behaviour tests in jasper-fanin "
        "(a_wide_lane_carries_a_24_bit_sample_to_the_mix_bit_exact)."
    ),
    (
        "jasper-ring/src/lib.rs",
        "samples.len(), g.samples_per_slot()",
    ): (
        "try_publish_slot's own doc comment states the exact contract "
        "(\"samples.len() == samples_per_slot\"); its only real caller "
        "(fanin's RingOutput/mixer code) sizes samples from the same "
        "geometry the writer was constructed with."
    ),
    (
        "jasper-ring/src/writer.rs",
        "ring publish requires exactly one complete slot",
    ): (
        "The assert lives in publish_bytes, whose own doc comment states the "
        "exact contract (\"payload.len() must equal Geometry::slot_bytes\"). "
        "Both real callers are fanin's RingOutput/mixer code -- the typed "
        "publish wrapper on an S16LE ring and the byte path on an S32LE one "
        "-- and each slices its payload from a buffer sized off the same "
        "geometry the writer was constructed with."
    ),
    (
        "jasper-host-clock/src/lib.rs",
        "CORRECTION_INTEGRAL_GAIN > 0.0 && CORRECTION_INTEGRAL_GAIN <= 0.1",
    ): (
        "Compile-time const-context assert (`const _: () = assert!(...)`), "
        "not a runtime one: the Rust compiler evaluates this at build time "
        "-- if the condition is false the crate fails to COMPILE, so this "
        "can never execute (and therefore never panic) in a running "
        "daemon. Allowlisted for completeness rather than teaching the "
        "scanner a new const-context exemption for a single site."
    ),
}

_ASSERT_FAMILY_PAT = re.compile(r"\b(?:debug_)?assert(?:_eq|_ne)?!\(")
# The panic-family constructs with an intentionally empty allowlist: no
# (file, key) can ever clear them, so their bare presence on a line always
# disqualifies it, independent of what else is on that same line.
# unreachable!/todo!/unimplemented! (issue #2251) join .unwrap()/panic!
# here rather than getting an allowlist of their own -- see the module
# docstring's "Issue #2251" paragraph for why.
_BARE_PANIC_PAT = re.compile(
    r"\.unwrap\(\)|panic!|unreachable!|todo!|unimplemented!"
)
_PANIC_PAT = re.compile(
    _BARE_PANIC_PAT.pattern + r"|\.expect\(|" + _ASSERT_FAMILY_PAT.pattern
)
_EXPECT_MSG_PAT = re.compile(r'\.expect\(\s*"((?:[^"\\]|\\.)*)"')
_STRING_PAT = re.compile(r'"(?:[^"\\]|\\.)*"')
_QUOTED_STRING_PAT = re.compile(r'"((?:[^"\\]|\\.)*)"')
_RAW_STRING_START_PAT = re.compile(
    r'(?<![A-Za-z0-9_])(?:br|cr|r)(?P<hashes>#{0,255})"'
)
# A char literal, never a lifetime (issue #2274): both open with ``'``,
# but only a char literal closes with one, so requiring a closing quote
# after exactly one char or escape leaves ``<'a>`` / ``&'static`` /
# ``'outer:`` untouched. ``\u{..}`` escapes are matched whole because
# they carry braces of their own.
_CHAR_PAT = re.compile(r"'(?:\\u\{[0-9a-fA-F_]+\}|\\.|[^'\\])'")


def _strip_literals(line: str) -> str:
    """Blank out char and string literals so brace counting and comment
    detection aren't confused by braces / ``//`` / quotes inside them.
    Char literals go first: a ``'"'`` would otherwise read as the start
    of a string and blank the real code up to the next ``"``. Preserve
    delimiter quotes and width so a later raw-string delimiter still indexes
    the source line."""
    chars_stripped = _CHAR_PAT.sub(
        lambda match: "'" + " " * (len(match.group()) - 2) + "'", line
    )
    return _STRING_PAT.sub(
        lambda match: '"' + " " * (len(match.group()) - 2) + '"',
        chars_stripped,
    )


def _strip_comments(line: str) -> str:
    stripped = _strip_literals(line)
    idx = stripped.find("//")
    return stripped[:idx] if idx >= 0 else stripped


def _strip_source_lines(lines: list[str]) -> list[str]:
    """Strip literals and comments while tracking Rust raw strings."""
    result: list[str] = []
    raw_close: str | None = None
    block_comment_depth = 0
    for line in lines:
        code = ""
        cursor = 0
        while cursor < len(line):
            if raw_close is not None:
                close_at = line.find(raw_close, cursor)
                if close_at < 0:
                    code += " " * (len(line) - cursor)
                    cursor = len(line)
                    continue
                after_close = close_at + len(raw_close)
                code += " " * (after_close - cursor)
                cursor = after_close
                raw_close = None
                continue

            if block_comment_depth:
                nested_at = line.find("/*", cursor)
                close_at = line.find("*/", cursor)
                if nested_at >= 0 and (
                    close_at < 0 or nested_at < close_at
                ):
                    after_marker = nested_at + 2
                    block_comment_depth += 1
                elif close_at >= 0:
                    after_marker = close_at + 2
                    block_comment_depth -= 1
                else:
                    after_marker = len(line)
                code += " " * (after_marker - cursor)
                cursor = after_marker
                continue

            segment = line[cursor:]
            stripped = _strip_comments(segment)
            start = _RAW_STRING_START_PAT.search(stripped)
            block_start = stripped.find("/*")
            if block_start >= 0 and (
                start is None or block_start < start.start()
            ):
                code += stripped[:block_start] + "  "
                cursor += block_start + 2
                block_comment_depth = 1
                continue
            if start is None:
                code += stripped
                break
            code += stripped[: start.start()]
            raw_close = '"' + start.group("hashes")
            raw_start_end = cursor + start.end()
            close_at = line.find(raw_close, raw_start_end)
            if close_at < 0:
                code += " " * (len(line) - cursor - start.start())
                cursor = len(line)
                continue
            after_close = close_at + len(raw_close)
            code += " " * (after_close - cursor - start.start())
            cursor = after_close
            raw_close = None
        result.append(code)
    return result


def _cfg_test_spans(lines: list[str]) -> list[tuple[int, int]]:
    """0-based inclusive line spans of ``#[cfg(test)]``-attributed items
    (modules and functions), found by brace counting with char and
    string literals stripped."""
    code_lines = _strip_source_lines(lines)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if "#[cfg(test)]" not in code_lines[i]:
            i += 1
            continue
        depth = 0
        opened = False
        j = i
        while j < len(lines):
            code = code_lines[j]
            depth += code.count("{") - code.count("}")
            if "{" in code:
                opened = True
            if opened and depth <= 0:
                break
            if not opened and j > i and ";" in code:
                # `#[cfg(test)] use ...;` — single braceless item.
                break
            j += 1
        spans.append((i, j))
        i = j + 1
    return spans


def _statement_span(
    lines: list[str], code_lines: list[str], start: int
) -> str:
    """The (possibly multi-line) statement text starting at ``lines[start]``,
    joined with single spaces, through the line where the parenthesis nest
    opened on ``start`` closes. Depth is counted on string-and-comment-
    stripped text (so a paren inside a message literal or a trailing ``//``
    comment can't confuse it), but the returned text keeps each line's
    original content, so a message string is still there to find.
    """
    parts: list[str] = []
    depth = 0
    opened = False
    j = start
    while j < len(lines):
        code_for_depth = code_lines[j]
        depth += code_for_depth.count("(") - code_for_depth.count(")")
        if "(" in code_for_depth:
            opened = True
        parts.append(lines[j].strip())
        if opened and depth <= 0:
            break
        j += 1
    return " ".join(p for p in parts if p)


def _macro_args(statement: str) -> str:
    """Text between an assert-family macro's opening paren and its matching
    closing paren, found by depth counting (robust to nested parens in the
    arguments, e.g. ``assert_eq!(x % (y as usize), 0)``) rather than a
    backtracking regex."""
    anchor = _ASSERT_FAMILY_PAT.search(statement)
    if not anchor:
        return statement
    open_idx = anchor.end() - 1
    depth = 0
    for i in range(open_idx, len(statement)):
        char = statement[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return statement[open_idx + 1 : i]
    return statement[open_idx + 1 :]


def _assert_key(statement: str) -> str:
    """The ALLOWED_ASSERTS key for an assert-family statement: its trailing
    string message when it has one (same convention as ALLOWED_EXPECTS),
    else its normalized argument text (``assert_eq!``/``assert_ne!``
    frequently carry no message)."""
    messages = _QUOTED_STRING_PAT.findall(statement)
    if messages:
        return messages[-1]
    return " ".join(_macro_args(statement).split())


class _Findings(NamedTuple):
    violations: list[str]
    seen_expects: set[tuple[str, str]]
    seen_asserts: set[tuple[str, str]]


def _scan_source(rel: str, lines: list[str]) -> _Findings:
    """Classify one Rust source's panic-family constructs against the two
    allowlists, skipping ``#[cfg(test)]`` code."""
    violations: list[str] = []
    seen_expects: set[tuple[str, str]] = set()
    seen_asserts: set[tuple[str, str]] = set()
    code_lines = _strip_source_lines(lines)
    spans = _cfg_test_spans(lines)

    def in_test(n: int) -> bool:
        return any(a <= n <= b for a, b in spans)

    for n, raw in enumerate(lines):
        # Scanner soundness: every #[test] fn must sit inside a
        # #[cfg(test)] span, or the classifier would mislabel
        # its body as runtime code.
        if "#[test]" in code_lines[n] and not in_test(n):
            violations.append(
                f"{rel}:{n + 1}: #[test] outside a #[cfg(test)] "
                "module — move it inside one (or teach this "
                "scanner about the new shape)"
            )
            continue
        code = code_lines[n]
        if not _PANIC_PAT.search(code) or in_test(n):
            continue

        # Every panic-family construct on the line is checked
        # independently, not exclusively (issue #1718):
        # a line combining an ALLOWLISTED .expect() with an
        # UNREGISTERED assert used to short-circuit on the expect
        # branch's `continue` before the assert branch ever ran,
        # silently clearing the whole line. `accounted` only
        # reaches True if every construct present resolves to a
        # matched, allowlisted entry.
        accounted = True

        if _BARE_PANIC_PAT.search(code):
            # .unwrap()/panic!/unreachable!/todo!/unimplemented! --
            # the allowlist for these is intentionally empty (module
            # docstring), so their presence alone always
            # disqualifies the line, independent of whatever else is
            # on it.
            accounted = False

        expect_match = _EXPECT_MSG_PAT.search(raw)
        if expect_match:
            expect_key = (rel, expect_match.group(1))
            if expect_key in ALLOWED_EXPECTS:
                seen_expects.add(expect_key)
            else:
                accounted = False

        if _ASSERT_FAMILY_PAT.search(code):
            statement = _statement_span(lines, code_lines, n)
            assert_key = (rel, _assert_key(statement))
            if assert_key in ALLOWED_ASSERTS:
                seen_asserts.add(assert_key)
            else:
                accounted = False

        if not accounted:
            violations.append(f"{rel}:{n + 1}: {raw.strip()}")
    return _Findings(violations, seen_expects, seen_asserts)


def _runtime_findings() -> _Findings:
    """Scan the runtime crates for panic-capable macros outside
    ``#[cfg(test)]`` code, classifying each hit against the two
    allowlists."""
    violations: list[str] = []
    seen_expects: set[tuple[str, str]] = set()
    seen_asserts: set[tuple[str, str]] = set()
    for crate in RUNTIME_CRATES:
        # Rust modules can live below src/ (for example
        # jasper-fanin/src/mixer/direct_capture.rs). A shallow glob silently
        # stopped enforcing this contract when code was split into a module.
        for path in sorted((REPO / "rust" / crate / "src").rglob("*.rs")):
            found = _scan_source(
                str(path.relative_to(REPO / "rust")),
                path.read_text().splitlines(),
            )
            violations.extend(found.violations)
            seen_expects |= found.seen_expects
            seen_asserts |= found.seen_asserts
    return _Findings(violations, seen_expects, seen_asserts)


def test_bare_panic_pattern_covers_all_five_constructs() -> None:
    """Issue #2251 gate SF1: RUNTIME_CRATES currently has zero live
    unreachable!/todo!/unimplemented! occurrences, so
    test_no_new_panics_in_rust_runtime_code would stay green even if one of
    the five constructs -- or the whole _BARE_PANIC_PAT category -- were
    silently dropped. Pin the pattern object directly instead of relying on
    the corpus to self-detect a regression here."""
    for construct in (
        ".unwrap()",
        'panic!("boom")',
        "unreachable!()",
        'todo!("x")',
        "unimplemented!()",
        'unreachable!("x={}", x)',
    ):
        assert _BARE_PANIC_PAT.search(construct), construct

    # A comment mention and a string-literal mention both match the raw
    # regex -- proving the strip step below is load-bearing, not a no-op.
    comment_line = "/// see `unreachable!` for why this arm exists"
    string_line = 'log::warn!("todo!() called at {}", site);'
    assert _BARE_PANIC_PAT.search(comment_line)
    assert _BARE_PANIC_PAT.search(string_line)

    # _strip_comments is what _scan_source() calls before matching (it
    # strips char and string literals internally, then truncates at
    # `//`); once run through it, neither line reads as a panic.
    assert not _BARE_PANIC_PAT.search(_strip_comments(comment_line))
    assert not _BARE_PANIC_PAT.search(_strip_comments(string_line))


# Issue #2274: each entry is one line of a #[cfg(test)] module body,
# carrying a quote construct the brace counter used to misread. `'}'` ended
# the module early, so the test code below it scanned as runtime (the PR
# #2264 incident); `'{'` held it open past its closing brace, swallowing --
# and silently un-guarding -- the runtime code below; a stripper that
# paired lifetime quotes off against each other would eat the `{` between
# `&'a str>` and `' '`; and a char literal holding a double quote opens a
# phantom string unless char literals are stripped first.
_SPAN_FIXTURE_LINES = {
    "closing-brace-char": "    const CLOSE: char = '}';",
    "opening-brace-char": "    const OPEN: char = '{';",
    "lifetimes-and-char": (
        "    fn words<'a>(s: &'a str) -> Vec<&'a str> "
        "{ s.split(' ').collect() }"
    ),
    "double-quote-char": "    const Q: char = '\"'; const C: &str = \"}\";",
}

_SPAN_FIXTURE_TAIL = """\

    #[test]
    fn uses_the_construct() {
        let v: Option<u32> = Some(1);
        assert_eq!(v.unwrap(), 1);
    }
}

pub fn boom(x: Option<u32>) -> u32 {
    x.unwrap()
}
"""


@pytest.mark.parametrize("fixture", sorted(_SPAN_FIXTURE_LINES))
def test_quote_constructs_do_not_move_the_cfg_test_span(
    fixture: str,
) -> None:
    """Brace counting must read code only: whatever quote construct a
    ``#[cfg(test)]`` module's body holds, the span ends at that module's
    own closing brace. So the runtime ``.unwrap()`` below it is the only
    violation -- the test code above it is never flagged, and the runtime
    code below it is never swallowed."""
    lines = (
        "#[cfg(test)]\nmod tests {\n"
        + _SPAN_FIXTURE_LINES[fixture]
        + "\n"
        + _SPAN_FIXTURE_TAIL
    ).splitlines()
    runtime_unwrap = lines.index("    x.unwrap()") + 1

    findings = _scan_source("fixture/src/lib.rs", lines)

    assert [
        int(v.split(":")[1]) for v in findings.violations
    ] == [runtime_unwrap]


def test_multiline_raw_strings_do_not_move_the_cfg_test_span() -> None:
    lines = """\
#[cfg(test)]
mod tests {
    const BODY: &str = r#"
}
"#;
    #[test]
    fn uses_the_construct() {
        Some(1).unwrap();
    }
}

pub fn boom(x: Option<u32>) -> u32 {
    let _text = r##"
    panic!("not code");
    }
"##;
    x.unwrap()
}
""".splitlines()
    runtime_unwrap = lines.index("    x.unwrap()") + 1

    findings = _scan_source("fixture/src/lib.rs", lines)

    assert [int(v.split(":")[1]) for v in findings.violations] == [
        runtime_unwrap
    ]


def test_raw_string_marker_in_nested_block_comment_hides_no_panic() -> None:
    lines = '''\
/* outer
   /* raw literals start with r#" */
   panic!("commented out");
*/
pub fn boom() { panic!("live"); }
'''.splitlines()
    runtime_panic = lines.index('pub fn boom() { panic!("live"); }') + 1

    findings = _scan_source("fixture/src/lib.rs", lines)

    assert [int(v.split(":")[1]) for v in findings.violations] == [
        runtime_panic
    ]


def test_no_new_panics_in_rust_runtime_code() -> None:
    findings = _runtime_findings()
    assert not findings.violations, (
        "unwrap()/expect()/panic!/unreachable!/todo!/unimplemented!/"
        "assert!-family in runtime (non-#[cfg(test)]) code of the "
        "production audio daemons:\n  "
        + "\n  ".join(findings.violations)
        + "\nReturn a Result (or log-and-degrade) instead. If this is a "
        "genuine construction invariant, use .expect(\"<invariant>\") and "
        "add an audited entry to ALLOWED_EXPECTS, or keep the assert!/"
        "assert_eq!/assert_ne!/debug_assert! and add an audited entry to "
        "ALLOWED_ASSERTS, in tests/test_rust_runtime_panic_freedom.py with "
        "the rationale."
    )


def test_expect_allowlist_has_no_stale_entries() -> None:
    findings = _runtime_findings()
    stale = set(ALLOWED_EXPECTS) - findings.seen_expects
    assert not stale, (
        "ALLOWED_EXPECTS entries no longer present in the source "
        f"(remove them so the list only shrinks): {sorted(stale)}"
    )


def test_assert_allowlist_has_no_stale_entries() -> None:
    findings = _runtime_findings()
    stale = set(ALLOWED_ASSERTS) - findings.seen_asserts
    assert not stale, (
        "ALLOWED_ASSERTS entries no longer present in the source, or whose "
        "key text changed (remove/update them so the list only shrinks): "
        f"{sorted(stale)}"
    )
