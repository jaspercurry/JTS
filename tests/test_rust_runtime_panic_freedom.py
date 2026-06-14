"""Pin the audited panic-freedom of the Rust audio daemons' runtime paths.

A 2026-06 audit manually verified that every ``.unwrap()`` / ``.expect(``
/ ``panic!`` in ``rust/jasper-fanin``, ``rust/jasper-outputd``, and the
shared ``rust/jasper-tts-protocol`` crate lives either in ``#[cfg(test)]``
code or at one of a handful of documented invariant sites. The two
daemons are the speaker's always-on audio path on a production Pi, and
``jasper-tts-protocol`` is a library compiled *into both* of them (the
TTS wire protocol + the shared loudness engine), so a panic there is a
panic in the audio runtime just the same — an unguarded panic in runtime
code kills audio output until systemd restarts the unit, so "no new
panics outside test code" is a safety invariant worth pinning, not a
style preference.

CI builds and ``cargo test``s these crates, but cargo cannot run in
every dev environment and nothing in cargo's gate distinguishes a
test-only ``unwrap`` from a runtime one. This guard is the
static-source twin (same technique as ``tests/test_outputd_wiring.py``):

- ``.unwrap()`` and ``panic!`` are banned outright in runtime code
  (zero current uses — the allowlist for them is intentionally empty).
- ``.expect("...")`` is allowed only for the audited invariant sites
  listed in ``ALLOWED_EXPECTS``, keyed by (file, message) so the pin
  survives line-number churn but a *new* expect still fails.
- Two-sided ratchet: a stale allowlist entry (the audited site was
  removed or its message changed) also fails, so the list only shrinks.

``rust/jasper-dual-dac-lab`` is deliberately out of scope: it is a lab
measurement tool, not a production daemon.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

RUNTIME_CRATES = ("jasper-fanin", "jasper-outputd", "jasper-tts-protocol")

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
}

_PANIC_PAT = re.compile(r"\.unwrap\(\)|\.expect\(|panic!")
_EXPECT_MSG_PAT = re.compile(r'\.expect\(\s*"((?:[^"\\]|\\.)*)"')
_STRING_PAT = re.compile(r'"(?:[^"\\]|\\.)*"')


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


def _runtime_findings() -> tuple[list[str], set[tuple[str, str]]]:
    """(violations, seen-allowlisted-expects) across the runtime crates."""
    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
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
                if not _PANIC_PAT.search(_strip_comments(raw)) or in_test(n):
                    continue
                msg_match = _EXPECT_MSG_PAT.search(raw)
                if msg_match and (rel, msg_match.group(1)) in ALLOWED_EXPECTS:
                    seen.add((rel, msg_match.group(1)))
                    continue
                violations.append(f"{rel}:{n + 1}: {raw.strip()}")
    return violations, seen


def test_no_new_panics_in_rust_runtime_code() -> None:
    violations, _ = _runtime_findings()
    assert not violations, (
        "unwrap()/expect()/panic! in runtime (non-#[cfg(test)]) code of "
        "the production audio daemons:\n  "
        + "\n  ".join(violations)
        + "\nReturn a Result (or log-and-degrade) instead. If this is a "
        "genuine construction invariant, use .expect(\"<invariant>\") "
        "and add an audited entry to ALLOWED_EXPECTS in "
        "tests/test_rust_runtime_panic_freedom.py with the rationale."
    )


def test_expect_allowlist_has_no_stale_entries() -> None:
    _, seen = _runtime_findings()
    stale = set(ALLOWED_EXPECTS) - seen
    assert not stale, (
        "ALLOWED_EXPECTS entries no longer present in the source "
        f"(remove them so the list only shrinks): {sorted(stale)}"
    )
