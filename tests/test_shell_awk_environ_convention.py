# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Repo-wide drift guard: env-file *values* go into awk via ENVIRON, never -v.

POSIX leaves ``awk -v var=value`` free to process escape sequences in the
assigned value, and real awks disagree on the unknown ones: gawk and mawk
handle e.g. the ``\\'`` inside a single-quote-wrapped env value differently,
which corrupted apostrophe-bearing lines on CI's mawk while passing
elsewhere. ``ENVIRON["..."]`` is escape-free on every awk, so the shared
writer (``jasper_env_file_set`` in deploy/lib/jasper-env-file.sh) pipes the
replacement ``KEY=VALUE`` line through an exported environment variable and
keeps ``-v`` for safe-charset *identifiers* only (env key names, numbers).

That convention lives in a comment, so nothing stopped the next reconciler
from re-growing ``awk -v line="${key}=${quoted}"`` (the exact pre-lib bug
shape). This guard greps every bash script under deploy/ and scripts/ and
fails on the dangerous pattern: an arbitrary *value* payload flowing into
``-v``. It is deliberately scoped so the benign, ubiquitous
``awk -v key="$key"`` / ``awk -v m="${memtotal_kb}"`` idioms stay legal —
a ``-v`` assignment is only flagged when one of three value-payload
signals is present:

  1. the awk variable is *named* like a value carrier (line/value/val/
     quoted/payload, underscore-delimited, case-insensitive);
  2. the payload contains a literal ``=`` — i.e. a ``KEY=VALUE`` env-file
     line is being assembled and spliced through ``-v``; or
  3. the payload expands a shell variable named like a value carrier
     (same word list), e.g. ``-v out="$quoted"``.

Guard style mirrors tests/test_reconciler_constants_match_python.py:
static text analysis only, failure message names file:line and the
sanctioned replacement.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("deploy", "scripts")

# `-v name=payload` where payload is double-quoted, single-quoted, or a
# bare token. Not anchored on `awk` so multi-line invocations
# (`awk -v a="$a" \` + continuation `-v b="$b" \`) are still scanned;
# no other tool in these trees uses `-v <name>=` syntax.
_AWK_V_TOKEN = re.compile(
    r"-v[ \t]+([A-Za-z_][A-Za-z0-9_]*)="
    r"(\"[^\"]*\"|'[^']*'|[^ \t'\"\\]+)"
)
# Value-carrier word, delimited by underscores / string edges so e.g.
# `outputd_content_bridge` or `target_level` never matches.
_VALUE_CARRIER = re.compile(
    r"(?:^|_)(?:line|value|val|quoted|payload)(?:_|$)", re.IGNORECASE
)
_SHELL_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def _is_bash_file(path: Path) -> bool:
    """Shebang-detected bash, plus the shebang-less source-only libs
    that mark themselves with a `# shellcheck shell=bash` first line
    (e.g. scripts/_lib.sh)."""
    try:
        first = path.open("rb").readline().decode("utf-8", "replace").strip()
    except OSError:
        return False
    if first.startswith("#!"):
        return bool(re.search(r"\b(?:ba)?sh\b", first))
    return first.replace(" ", "") == "#shellcheckshell=bash"


def _bash_files() -> list[Path]:
    files = []
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*")):
            if path.is_file() and _is_bash_file(path):
                files.append(path)
    return files


def _classify_dangerous(name: str, payload: str) -> str | None:
    """Return a human-readable reason when this `-v name=payload`
    carries a value, or None when it is a benign identifier/number."""
    if _VALUE_CARRIER.search(name):
        return f"awk -v variable {name!r} is named like a value carrier"
    body = payload.strip("\"'")
    if "=" in body:
        return "a KEY=VALUE line is being spliced through awk -v"
    for var in _SHELL_VAR.findall(body):
        if _VALUE_CARRIER.search(var):
            return f"value-carrying shell variable ${var} flows into awk -v"
    return None


def _violations_in(text: str) -> list[tuple[int, str, str]]:
    out = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):  # prose about the pattern is fine
            continue
        for match in _AWK_V_TOKEN.finditer(line):
            reason = _classify_dangerous(match.group(1), match.group(2))
            if reason:
                out.append((lineno, line.strip(), reason))
    return out


def test_scanner_sees_the_shell_corpus():
    """Meta-check: shebang detection actually finds the tree. If this
    shrinks dramatically the guard below is vacuously green."""
    files = _bash_files()
    assert len(files) >= 40, [str(p) for p in files]
    assert any(p.name == "install.sh" for p in files)
    assert any(p.name == "jasper-env-file.sh" for p in files)
    assert any(p.name == "_lib.sh" for p in files)


@pytest.mark.parametrize(
    "bad_line",
    [
        # The exact pre-lib bug shape from jasper_env_file_set's history.
        'awk -v line="${key}=${quoted}" \'{print}\'',
        # Value-carrying name, even with an innocent-looking payload.
        'awk -v replacement_line="$new" -f prog',
        # Innocent name, value-carrying payload variable.
        'awk -v out="$quoted" -f prog',
        "awk -v out=\"$JASPER_ENV_FILE_LINE\" -f prog",
        # Continuation line of a multi-line awk invocation.
        '    -v new_value="$v" \\',
    ],
)
def test_classifier_catches_known_bad_shapes(bad_line):
    assert _violations_in(bad_line), bad_line


@pytest.mark.parametrize(
    "good_line",
    [
        # The benign idioms shipped today.
        "awk -v key=\"$key\" '",
        'awk -v m="${memtotal_kb}" \'',
        '    -v outputd_content_bridge="$outputd_content_bridge" \\',
        "awk -F= -v key=\"$key\" '",
        "awk -F '\\t' -v provider=\"$provider\" -v column=\"$column\" '",
        # Static literals are fine; so is non-awk -v with no name=.
        "awk -v done=1 'END{print done}'",
        "grep -v wav",
    ],
)
def test_classifier_allows_benign_shapes(good_line):
    assert not _violations_in(good_line), good_line


def test_no_bash_script_pipes_values_into_awk_dash_v():
    """The repo-wide ratchet. On failure: move the value into an
    exported environment variable and read it inside awk with
    ENVIRON["..."], or call jasper_env_file_set from
    deploy/lib/jasper-env-file.sh, which already does this."""
    failures = []
    for path in _bash_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line, reason in _violations_in(text):
            failures.append(
                f"{path.relative_to(ROOT)}:{lineno}: {reason}\n    {line}"
            )
    assert not failures, (
        "awk -v must not carry value payloads (gawk/mawk escape-sequence "
        "divergence corrupts them); use the ENVIRON idiom from "
        "deploy/lib/jasper-env-file.sh:\n" + "\n".join(failures)
    )
