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
= true`` override, it compiles to a no-op in that release profile — but it
still panics in ``cargo test`` and any debug-build developer run (see
``fake.rs``'s "safe developer runs"), which is the same not-test-gated gap
this guard exists to close, so it stays in scope.

CI builds and ``cargo test``s these crates, but cargo cannot run in
every dev environment and nothing in cargo's gate distinguishes a
test-only ``unwrap`` from a runtime one. This guard is the
static-source twin (same technique as ``tests/test_outputd_wiring.py``):

- ``.unwrap()`` and ``panic!`` are banned outright in runtime code
  (zero current uses — the allowlist for them is intentionally empty).
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

``rust/jasper-dual-dac-lab`` is deliberately out of scope: it is a lab
measurement tool, not a production daemon.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[1]

RUNTIME_CRATES = (
    "jasper-fanin",
    "jasper-outputd",
    "jasper-tts-protocol",
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
        "jasper-fanin/src/mixer.rs",
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
        "jasper-outputd/src/fake.rs",
        "channels must be > 0",
    ): (
        "Construction invariant: FakeAssistantSource::new's channels argument "
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
    # Issue #1718 listed 7 illustrative core.rs/fake.rs sites; running the
    # widened scanner against the current tree surfaced 21 total across all
    # three runtime crates. These are that full, re-derived set.
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
        "jasper-fanin/src/mixer.rs",
        "sum.len(), input.len()",
    ): (
        "Buffer-sizing invariant on the per-period mix accumulator "
        "(\"Pulled out for unit testability — no ALSA needed\" per its own "
        "comment); the daemon allocates both buffers once at a fixed "
        "period size."
    ),
    (
        "jasper-fanin/src/mixer.rs",
        "channels >= 1",
    ): (
        "ramp_program_duck's channels argument is the daemon's own fixed "
        "channel-count constant at its only call site, never externally "
        "supplied."
    ),
    (
        "jasper-fanin/src/mixer.rs",
        "sum.len(), out.len()",
    ): (
        "saturate_to_i16, same shape as mix_into above (\"Pulled out for "
        "unit testability\"): buffer-sizing invariant on fixed-size "
        "period buffers."
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
        "jasper-outputd/src/content_bridge.rs",
        "content bridge output buffer must be exactly one period",
    ): (
        "Caller-contract invariant: render_period's only real caller (the "
        "daemon's own per-period output loop) always passes a buffer sized "
        "to exactly one period."
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
        "file's own try_fill_period, which always sizes the buffer from "
        "the same period_bytes field."
    ),
    (
        "jasper-outputd/src/dac_content.rs",
        "policy granted FIFO serve without a staged period",
    ): (
        "Same fail-closed trap pattern as ChannelPick::Sub above: the "
        "comment at the call site says \"Structurally impossible ... but "
        "on a reboot-on-fail daemon an invariant break must degrade to a "
        "clean direct-path period — never a stale-buffer glitch,\" and the "
        "code right after the debug_assert! does exactly that (returns "
        "false to signal degrade-to-direct-path)."
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
        "mix_i16_saturating (module doc: \"the same intentionally boring "
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
        "out.len(), self.samples_per_slot",
    ): (
        "read_period's own doc comment states the exact contract "
        "(\"out.len() must be period_frames * channels\"); this checks the "
        "CALLER's buffer-sizing contract, distinct from the function's "
        "actual runtime-fault handling right below (ring empty/corrupt), "
        "which the same doc comment says already degrades to silence + "
        "counters rather than panicking."
    ),
    (
        "jasper-tts-protocol/src/loudness.rs",
        "samples.len() % (CHANNELS as usize), 0",
    ): (
        "KWeightedWindow::push_interleaved: frame-alignment invariant, "
        "same shape as the others above — every real producer emits whole "
        "interleaved frames."
    ),
}

_ASSERT_FAMILY_PAT = re.compile(r"\b(?:debug_)?assert(?:_eq|_ne)?!\(")
_PANIC_PAT = re.compile(
    r"\.unwrap\(\)|\.expect\(|panic!|" + _ASSERT_FAMILY_PAT.pattern
)
_EXPECT_MSG_PAT = re.compile(r'\.expect\(\s*"((?:[^"\\]|\\.)*)"')
_STRING_PAT = re.compile(r'"(?:[^"\\]|\\.)*"')
_QUOTED_STRING_PAT = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _strip_strings(line: str) -> str:
    """Blank out string literals so brace counting and comment
    detection aren't confused by braces / ``//`` inside strings."""
    return _STRING_PAT.sub('""', line)


def _strip_comments(line: str) -> str:
    stripped = _strip_strings(line)
    idx = stripped.find("//")
    return stripped[:idx] if idx >= 0 else stripped


def _cfg_test_spans(lines: list[str]) -> list[tuple[int, int]]:
    """0-based inclusive line spans of ``#[cfg(test)]``-attributed items
    (modules and functions), found by brace counting with string
    literals stripped."""
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if "#[cfg(test)]" not in _strip_comments(lines[i]):
            i += 1
            continue
        depth = 0
        opened = False
        j = i
        while j < len(lines):
            code = _strip_comments(lines[j])
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


def _statement_span(lines: list[str], start: int) -> str:
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
        code_for_depth = _strip_comments(lines[j])
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


def _runtime_findings() -> _Findings:
    """Scan the runtime crates for panic-capable macros outside
    ``#[cfg(test)]`` code, classifying each hit against the two
    allowlists."""
    violations: list[str] = []
    seen_expects: set[tuple[str, str]] = set()
    seen_asserts: set[tuple[str, str]] = set()
    for crate in RUNTIME_CRATES:
        for path in sorted((REPO / "rust" / crate / "src").glob("*.rs")):
            rel = str(path.relative_to(REPO / "rust"))
            lines = path.read_text().splitlines()
            spans = _cfg_test_spans(lines)

            def in_test(n: int) -> bool:
                return any(a <= n <= b for a, b in spans)

            for n, raw in enumerate(lines):
                # Scanner soundness: every #[test] fn must sit inside a
                # #[cfg(test)] span, or the classifier would mislabel
                # its body as runtime code.
                if "#[test]" in _strip_comments(raw) and not in_test(n):
                    violations.append(
                        f"{rel}:{n + 1}: #[test] outside a #[cfg(test)] "
                        "module — move it inside one (or teach this "
                        "scanner about the new shape)"
                    )
                    continue
                code = _strip_comments(raw)
                if not _PANIC_PAT.search(code) or in_test(n):
                    continue
                expect_match = _EXPECT_MSG_PAT.search(raw)
                if expect_match:
                    expect_key = (rel, expect_match.group(1))
                    if expect_key in ALLOWED_EXPECTS:
                        seen_expects.add(expect_key)
                        continue
                    violations.append(f"{rel}:{n + 1}: {raw.strip()}")
                    continue
                if _ASSERT_FAMILY_PAT.search(code):
                    statement = _statement_span(lines, n)
                    assert_key = (rel, _assert_key(statement))
                    if assert_key in ALLOWED_ASSERTS:
                        seen_asserts.add(assert_key)
                        continue
                    violations.append(f"{rel}:{n + 1}: {raw.strip()}")
                    continue
                # Bare .unwrap() or panic! -- the allowlist for these is
                # intentionally empty (module docstring).
                violations.append(f"{rel}:{n + 1}: {raw.strip()}")
    return _Findings(violations, seen_expects, seen_asserts)


def test_no_new_panics_in_rust_runtime_code() -> None:
    findings = _runtime_findings()
    assert not findings.violations, (
        "unwrap()/expect()/panic!/assert!-family in runtime "
        "(non-#[cfg(test)]) code of the production audio daemons:\n  "
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
