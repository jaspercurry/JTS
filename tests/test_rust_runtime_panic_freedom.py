# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the audited panic-freedom of the Rust audio daemons' runtime paths.

The two daemons (``rust/jasper-fanin``, ``rust/jasper-outputd``) are the
speaker's always-on audio path on a production Pi, and every crate in
``RUNTIME_CRATES`` is compiled into one or both of them, so an unguarded
panic there kills audio output until systemd restarts the unit: "no new
panics outside test code" is a safety invariant worth pinning, not a style
preference.

The contract: every panic-capable construct outside ``#[cfg(test)]`` code
carries a ``// PANIC-AUDITED: <invariant>`` marker on its own line or on the
line directly above, naming the invariant that makes it unreachable. The
audit then lives at the site the next editor of that line reads, and this
test stores no message text of its own.

``.unwrap()``, ``panic!``, ``unreachable!``, ``todo!`` and ``unimplemented!``
are not markable: they are banned outright, and a line carrying one fails
even when a marker (or a marked ``.expect(``) sits on it, so constructs stay
judged independently rather than per line (issue #1718).

Issue #1718: the scan covers the ``assert!`` family — ``assert!``,
``assert_eq!``, ``assert_ne!``, and ``debug_assert!`` (and, caught by the
same regex, ``debug_assert_eq!`` / ``debug_assert_ne!``) — which is the same
panic mechanism under a different macro name. In this workspace's release
profile (``panic = "abort"``, ``rust/jasper-outputd/Cargo.toml``) ``assert!``
/ ``assert_eq!`` / ``assert_ne!`` abort the daemon outright, same as an
unguarded ``.expect(``. The ``debug_assert!`` sub-family is the one place
this differs from the issue's original framing: with no ``debug-assertions
= true`` override, it compiles to a no-op in that release profile. This
repo's own CI (``.github/workflows/tests.yml``) runs ``cargo test --release
--locked`` for every crate, so CI's own test run does not exercise
``debug_assert!`` either — the coverage this guard adds for that sub-family
is an unqualified local ``cargo test`` (no ``--release``) or any
debug-profile developer run, neither of which any existing gate checks. It
stays in scope for that gap.

Issue #2251: ``unreachable!``, ``todo!``, and ``unimplemented!`` all expand
to ``panic!`` at compile time, so a static source scan that doesn't name
them lets any of the three sail through unmatched. Unlike ``.expect(`` / the
``assert!`` family, none of the three is a conditional guard with a
legitimate invariant-documenting use: reaching any of them panics
unconditionally, exactly like a bare ``panic!`` — hence the unmarkable
category above rather than a marker of their own.

CI builds and ``cargo test``s these crates, but cargo cannot run in every
dev environment and nothing in cargo's gate distinguishes a test-only
``unwrap`` from a runtime one. This guard is the static-source twin (same
technique as ``tests/test_outputd_wiring.py``).

The non-daemon crates in ``RUNTIME_CRATES`` are scanned for the same reason
the daemons' own are: each is a ``path`` dependency compiled into and
executing as part of the shipped binaries. "On the default runtime path" is
deliberately not read narrowly as "doing its feature's user-visible job" —
construction/status-seeding code that runs unconditionally counts too:

- ``jasper-tts-protocol`` carries the TTS wire protocol and the shared
  loudness engine into BOTH daemons.
- ``jasper-ring`` looks like an opt-in prototype from its own module doc
  ("Ring B prototype") but the ring is the ONLY central transport
  (``docs/adr/0100-one-audio-transport.md``) -- so this crate's
  ``RingReader``/``RingWriter`` code IS the default runtime path, not a
  corpus/lab-only affair.
- ``jasper-resampler`` is used unconditionally in ``jasper-fanin``'s mixer
  for per-lane rate matching (``LaneResampler``) and general RMS/format
  utilities, independent of coupling mode.
- ``jasper-clock``'s ``Dll`` is constructed unconditionally as part of
  ``jasper-outputd``'s ``State`` (``sro_estimator``, ``dac_clock`` fields
  built on every daemon start) and is the control law inside
  ``jasper-resampler``'s ``RateController``.
- ``jasper-host-clock`` reads "Default OFF" in its own module doc, and the
  combo-mode servo THREAD genuinely is gated behind
  ``JASPER_FANIN_HOST_CLOCK=enabled`` AND USB Direct armed (both non-default)
  -- but ``jasper_host_clock::HostClock::new(...).status_fragment()`` runs
  unconditionally at fan-in startup to seed the disabled ``/state`` fragment
  even when the feature itself never arms.
- ``jasper-env``'s env-parsing helpers (``env_str``, ``env_parse``) run on
  every daemon startup to read config.
"""
from __future__ import annotations

import re
from pathlib import Path

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

_ASSERT_FAMILY_PAT = re.compile(r"\b(?:debug_)?assert(?:_eq|_ne)?!\(")
# The panic-family constructs no marker can clear: their bare presence on a
# line always disqualifies it, independent of what else is on that same
# line. unreachable!/todo!/unimplemented! (issue #2251) join
# .unwrap()/panic! here -- see the module docstring for why.
_BARE_PANIC_PAT = re.compile(
    r"\.unwrap\(\)|panic!|unreachable!|todo!|unimplemented!"
)
_PANIC_PAT = re.compile(
    _BARE_PANIC_PAT.pattern + r"|\.expect\(|" + _ASSERT_FAMILY_PAT.pattern
)
_MARKER_PAT = re.compile(r"//\s*PANIC-AUDITED:\s*\S")
_STRING_PAT = re.compile(r'"(?:[^"\\]|\\.)*"')
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


def _split_comment(line: str) -> tuple[str, str]:
    """The line's literal-blanked code and its raw ``//`` comment (or ""):
    blanking first means a ``//`` inside a string reads as code."""
    stripped = _strip_literals(line)
    idx = stripped.find("//")
    if idx < 0:
        return stripped, ""
    return stripped[:idx], line[idx:]


def _strip_comments(line: str) -> str:
    return _split_comment(line)[0]


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


def _is_audited(lines: list[str], n: int) -> bool:
    """Whether a ``// PANIC-AUDITED:`` marker covers line ``n``: on that
    line, or on the line directly above it."""
    return any(
        _MARKER_PAT.search(_split_comment(lines[i])[1])
        for i in (n, n - 1)
        if i >= 0
    )


def _scan_source(rel: str, lines: list[str]) -> list[str]:
    """One Rust source's unaudited panic-family constructs, as
    ``file:line: source`` strings, skipping ``#[cfg(test)]`` code."""
    violations: list[str] = []
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
        if _BARE_PANIC_PAT.search(code) or not _is_audited(lines, n):
            violations.append(f"{rel}:{n + 1}: {raw.strip()}")
    return violations


def _runtime_violations(rust_root: Path = REPO / "rust") -> list[str]:
    """Scan the runtime crates for panic-capable constructs outside
    ``#[cfg(test)]`` code that no audit marker covers."""
    violations: list[str] = []
    for crate in RUNTIME_CRATES:
        # Rust modules can live below src/ (for example
        # jasper-fanin/src/mixer/direct_capture.rs). A shallow glob silently
        # stopped enforcing this contract when code was split into a module.
        for path in sorted((rust_root / crate / "src").rglob("*.rs")):
            violations.extend(
                _scan_source(
                    str(path.relative_to(rust_root)),
                    path.read_text().splitlines(),
                )
            )
    return violations


def test_bare_panic_pattern_covers_all_five_constructs() -> None:
    """Issue #2251 gate SF1: RUNTIME_CRATES currently has zero live
    unreachable!/todo!/unimplemented! occurrences, so
    test_no_unaudited_panics_in_rust_runtime_code would stay green even if
    one of
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

    violations = _scan_source("fixture/src/lib.rs", lines)

    assert [int(v.split(":")[1]) for v in violations] == [runtime_unwrap]


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

    violations = _scan_source("fixture/src/lib.rs", lines)

    assert [int(v.split(":")[1]) for v in violations] == [runtime_unwrap]


def test_raw_string_marker_in_nested_block_comment_hides_no_panic() -> None:
    lines = '''\
/* outer
   /* raw literals start with r#" */
   panic!("commented out");
*/
pub fn boom() { panic!("live"); }
'''.splitlines()
    runtime_panic = lines.index('pub fn boom() { panic!("live"); }') + 1

    violations = _scan_source("fixture/src/lib.rs", lines)

    assert [int(v.split(":")[1]) for v in violations] == [runtime_panic]


# One runtime line each (with the marker placement under test), and whether
# the gate must flag it.
_MARKER_FIXTURES: dict[str, tuple[str, bool]] = {
    "unmarked-expect": ('    x.expect("minted by this ledger");', True),
    "expect-marked-above": (
        "    // PANIC-AUDITED: minted by this ledger\n"
        '    x.expect("minted by this ledger");',
        False,
    ),
    "expect-marked-inline": (
        '    x.expect("m"); // PANIC-AUDITED: minted by this ledger',
        False,
    ),
    "unmarked-debug-assert": ("    debug_assert!(x.is_some());", True),
    "debug-assert-marked": (
        "    // PANIC-AUDITED: fixed period config\n"
        "    debug_assert!(x.is_some());",
        False,
    ),
    "marked-unwrap-is-still-banned": (
        "    // PANIC-AUDITED: an unwrap is not markable\n    x.unwrap();",
        True,
    ),
    "marker-inside-a-string-is-not-a-marker": (
        '    log("// PANIC-AUDITED: x"); x.expect("m");',
        True,
    ),
}


@pytest.mark.parametrize("fixture", sorted(_MARKER_FIXTURES))
def test_the_marker_contract_is_what_clears_a_site(
    tmp_path: Path, fixture: str
) -> None:
    """The gate scans a crate tree, so drive it against a temp one: an
    unmarked site fails, the marker (on its own line or the line above)
    clears it, and neither placement clears an unmarkable construct."""
    site, flagged = _MARKER_FIXTURES[fixture]
    src = tmp_path / RUNTIME_CRATES[0] / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(f"pub fn f(x: Option<u32>) {{\n{site}\n}}\n")

    violations = _runtime_violations(tmp_path)

    assert bool(violations) is flagged, violations


def test_no_unaudited_panics_in_rust_runtime_code() -> None:
    violations = _runtime_violations()
    assert not violations, (
        "unwrap()/expect()/panic!/unreachable!/todo!/unimplemented!/"
        "assert!-family in runtime (non-#[cfg(test)]) code of the "
        "production audio daemons:\n  "
        + "\n  ".join(violations)
        + "\nReturn a Result (or log-and-degrade) instead. If this is a "
        "genuine invariant, keep the .expect(\"<invariant>\") or the "
        "assert!/assert_eq!/assert_ne!/debug_assert! and put a "
        "// PANIC-AUDITED: <one-clause invariant> marker on that line or "
        "the line above it. .unwrap()/panic!/unreachable!/todo!/"
        "unimplemented! cannot be marked — rewrite them."
    )
