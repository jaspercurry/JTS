# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the audited panic-freedom of the Rust audio daemons' runtime paths.

The two daemons (``rust/jasper-fanin``, ``rust/jasper-outputd``) are the
speaker's always-on audio path on a production Pi, so an unguarded panic in
runtime code kills audio output until systemd restarts the unit: "no new
panics outside test code" is a safety invariant worth pinning, not a style
preference. CI builds and ``cargo test``s these crates, but cargo cannot run
in every dev environment and nothing in cargo's gate distinguishes a
test-only ``unwrap`` from a runtime one; this guard is the static-source twin
(same technique as ``tests/test_outputd_wiring.py``).

The contract, for every panic-capable construct outside ``#[cfg(test)]``
code:

- ``.expect(`` and the ``assert!`` family (``assert!``, ``assert_eq!``,
  ``assert_ne!``, ``debug_assert!`` and its ``_eq`` / ``_ne`` twins) carry a
  ``// PANIC-AUDITED: <invariant>`` comment naming what makes the site
  unreachable -- inline on the site's own line, or alone on the line
  directly above it. A marker with no such construct under it is itself a
  violation, so a marker cannot outlive the site it audits.
- ``.unwrap()``, ``panic!``, ``unreachable!``, ``todo!`` and
  ``unimplemented!`` are unmarkable: no comment clears them, so a line
  carrying one fails even beside a marked ``.expect(`` -- constructs are
  judged one by one, never per line (issue #1718).

``debug_assert!`` stays in scope even though this workspace's release
profile (``panic = "abort"``, ``rust/jasper-outputd/Cargo.toml``) compiles
it out and CI runs ``cargo test --release --locked``: an unqualified local
``cargo test`` or any debug-profile developer run does execute it, and no
other gate covers that.

``RUNTIME_CRATES`` is every crate compiled into the shipped binaries, not
just the daemons' own -- each is a ``path`` dependency of one or both, so
its code executes in the audio runtime just the same. "On the default
runtime path" is deliberately not read narrowly as "doing its feature's
user-visible job": construction- and status-seeding code that runs
unconditionally counts too. ``jasper-host-clock`` reads "Default OFF" in its
own module doc, yet ``HostClock::new(...).status_fragment()`` runs at every
fan-in startup to seed the disabled ``/state`` fragment; ``jasper-ring``
reads "Ring B prototype", yet the ring is the ONLY central transport
(``docs/adr/0100-one-audio-transport.md``).
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
    "jasper-daemon",
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
_MARKER_PAT = re.compile(r"(?<!/)//(?![/!])\s*PANIC-AUDITED:\s*\S")
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


class _SourceLine(NamedTuple):
    """One line split in two: ``code`` with literals blanked, and the
    ``//`` comment that actually opened one -- so a ``//`` inside a raw
    string or a ``/* */`` block leaves ``comment`` empty."""

    code: str
    comment: str


def _strip_source_lines(lines: list[str]) -> list[_SourceLine]:
    """Split each line while tracking Rust raw strings and block
    comments across lines."""
    result: list[_SourceLine] = []
    raw_close: str | None = None
    block_comment_depth = 0
    for line in lines:
        code = ""
        comment = ""
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
            stripped, comment_here = _split_comment(segment)
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
                comment = comment_here
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
        result.append(_SourceLine(code, comment))
    return result


def _cfg_test_spans(lines: list[str]) -> list[tuple[int, int]]:
    """0-based inclusive line spans of ``#[cfg(test)]``-attributed items
    (modules and functions), found by brace counting with char and
    string literals stripped."""
    source = _strip_source_lines(lines)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if "#[cfg(test)]" not in source[i].code:
            i += 1
            continue
        depth = 0
        opened = False
        j = i
        while j < len(lines):
            code = source[j].code
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


def _is_gated(line: _SourceLine) -> bool:
    return bool(_PANIC_PAT.search(line.code))


def _is_audited(source: list[_SourceLine], n: int) -> bool:
    """Whether a marker covers the site on line ``n``: inline on that line,
    or alone -- no code of its own -- on the line directly above. An inline
    marker audits only the site it shares a line with."""
    if _MARKER_PAT.search(source[n].comment):
        return True
    above = source[n - 1] if n else None
    if above is None or above.code.strip():
        return False
    return bool(_MARKER_PAT.search(above.comment))


def _marker_is_orphaned(source: list[_SourceLine], n: int) -> bool:
    """The mirror of _is_audited: a marker must have a gated construct on
    its own line, or -- standing alone -- on the next one."""
    if _is_gated(source[n]):
        return False
    if source[n].code.strip():
        return True
    below = source[n + 1] if n + 1 < len(source) else None
    return below is None or not _is_gated(below)


def _scan_source(rel: str, lines: list[str]) -> list[str]:
    """One Rust source's unaudited panic-family constructs and orphaned
    markers, as ``file:line: source`` strings, skipping ``#[cfg(test)]``
    code."""
    violations: list[str] = []
    source = _strip_source_lines(lines)
    spans = _cfg_test_spans(lines)

    def in_test(n: int) -> bool:
        return any(a <= n <= b for a, b in spans)

    for n, raw in enumerate(lines):
        # Scanner soundness: every #[test] fn must sit inside a
        # #[cfg(test)] span, or the classifier would mislabel
        # its body as runtime code.
        if "#[test]" in source[n].code and not in_test(n):
            violations.append(
                f"{rel}:{n + 1}: #[test] outside a #[cfg(test)] "
                "module — move it inside one (or teach this "
                "scanner about the new shape)"
            )
            continue
        if in_test(n):
            continue
        if _is_gated(source[n]) and (
            _BARE_PANIC_PAT.search(source[n].code)
            or not _is_audited(source, n)
        ):
            violations.append(f"{rel}:{n + 1}: {raw.strip()}")
        elif _MARKER_PAT.search(source[n].comment) and _marker_is_orphaned(
            source, n
        ):
            violations.append(f"{rel}:{n + 1}: orphaned {raw.strip()}")
    return violations


def _runtime_violations(rust_root: Path = REPO / "rust") -> list[str]:
    """Scan the runtime crates for panic-capable constructs outside
    ``#[cfg(test)]`` code that no audit marker covers, and for markers that
    cover no such construct."""
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
    unreachable!/todo!/unimplemented! occurrences, so the tree test would
    stay green even if one of the five constructs -- or the whole
    _BARE_PANIC_PAT category -- were silently dropped. Pin the pattern
    object directly instead of relying on the corpus to self-detect a
    regression here."""
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

    # _split_comment is the per-line step inside _strip_source_lines,
    # whose code half is what _scan_source() matches against; once run
    # through it, neither line reads as a panic.
    assert not _BARE_PANIC_PAT.search(_split_comment(comment_line)[0])
    assert not _BARE_PANIC_PAT.search(_split_comment(string_line)[0])


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


# Each fixture is a function body (so line 1 is the fn header) and the
# lines the gate must flag.
_MARKER_FIXTURES: dict[str, tuple[str, list[int]]] = {
    "unmarked-expect": ('    x.expect("minted by this ledger");', [2]),
    "expect-marked-above": (
        "    // PANIC-AUDITED: minted by this ledger\n"
        '    x.expect("minted by this ledger");',
        [],
    ),
    "expect-marked-inline": (
        '    x.expect("m"); // PANIC-AUDITED: minted by this ledger',
        [],
    ),
    "inline-marker-covers-only-its-own-line": (
        '    a.expect("m"); // PANIC-AUDITED: minted by this ledger\n'
        '    b.expect("m");',
        [3],
    ),
    "unmarked-debug-assert": ("    debug_assert!(x.is_some());", [2]),
    "debug-assert-marked": (
        "    // PANIC-AUDITED: fixed period config\n"
        "    debug_assert!(x.is_some());",
        [],
    ),
    "marked-unwrap-is-still-banned": (
        "    // PANIC-AUDITED: an unwrap is not markable\n    x.unwrap();",
        [3],
    ),
    "marker-inside-a-string-is-not-a-marker": (
        '    log("// PANIC-AUDITED: x"); x.expect("m");',
        [2],
    ),
    "marker-inside-a-block-comment-is-not-a-marker": (
        "    /* an aside\n       // PANIC-AUDITED: x */\n"
        '    x.expect("m");',
        [4],
    ),
    "marker-in-a-doc-comment-is-not-a-marker": (
        '    /// PANIC-AUDITED: x\n    x.expect("m");',
        [3],
    ),
    "orphaned-marker": (
        "    // PANIC-AUDITED: the site it audited is gone\n    let y = 1;",
        [2],
    ),
    "orphaned-inline-marker": (
        "    let y = 1; // PANIC-AUDITED: the site it audited is gone",
        [2],
    ),
}


@pytest.mark.parametrize("fixture", sorted(_MARKER_FIXTURES))
def test_the_marker_contract_is_what_clears_a_site(
    tmp_path: Path, fixture: str
) -> None:
    """The gate walks a crate tree, so drive it against a temp one: an
    unmarked site fails, a marker on the site's own line or alone directly
    above it clears exactly that site, no marker clears an unmarkable
    construct, and a marker over nothing is itself a violation."""
    body, flagged = _MARKER_FIXTURES[fixture]
    src = tmp_path / RUNTIME_CRATES[0] / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(f"pub fn f(x: Option<u32>) {{\n{body}\n}}\n")

    violations = _runtime_violations(tmp_path)

    assert [int(v.split(":")[1]) for v in violations] == flagged, violations


def test_no_unaudited_panics_in_rust_runtime_code() -> None:
    missing = [
        crate
        for crate in RUNTIME_CRATES
        if not (REPO / "rust" / crate / "src").is_dir()
    ]
    assert not missing, (
        "RUNTIME_CRATES names a crate with no rust/<crate>/src, so the gate "
        f"silently scans nothing for it: {missing}"
    )

    violations = _runtime_violations()
    assert not violations, (
        "unwrap()/expect()/panic!/unreachable!/todo!/unimplemented!/"
        "assert!-family in runtime (non-#[cfg(test)]) code of the "
        "production audio daemons, or a marker auditing nothing:\n  "
        + "\n  ".join(violations)
        + "\nReturn a Result (or log-and-degrade) instead. If this is a "
        "genuine invariant, keep the .expect(\"<invariant>\") or the "
        "assert!/assert_eq!/assert_ne!/debug_assert! and put a "
        "// PANIC-AUDITED: <one-clause invariant> marker on that line, or "
        "alone on the line above it. .unwrap()/panic!/unreachable!/todo!/"
        "unimplemented! cannot be marked — rewrite them."
    )
