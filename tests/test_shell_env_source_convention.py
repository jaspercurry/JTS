# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Repo-wide drift guard: JTS env files are READ, never shell-sourced.

``/etc/jasper/jasper.env`` is where an operator-pasted provider key lands
until the next deploy sweeps it into its compartment, and
``/var/lib/jasper*/`` holds the wizard- and reconciler-owned files (speaker
name, WiFi PSK, AirPlay mode). Every value in them is operator or wizard
text. ``source``-ing such a file hands that text to bash: ``$(…)`` and
backticks EXECUTE (as root, on boot and ExecStartPre paths), a space splits
the assignment and runs the tail as a command, and ``#`` truncates. ``set -a``
then re-exports whatever survived into every child process.

The sanctioned readers are ``jasper_env_file_get`` (one key) and
``jasper_env_file_export`` (whole file, into the environment) from
deploy/lib/jasper-env-file.sh: one awk parse, nothing evaluated.

This is a tree scan by necessity — bash offers no structural altitude at
which "an env file was sourced" can be asserted once. It flags two shapes:

  1. a ``source``/``.`` whose target names a path under ``/etc/jasper`` or
     ``/var/lib/jasper``, or carries a JTS env-file basename — literally, or
     through a variable or ``for``-list this same file assigns; and
  2. a ``source``/``.`` of a path the file takes as a POSITIONAL PARAMETER
     while ``set -a`` is in effect — the generic ``load_env_file FILE``
     loader shape, whose target no scanner can resolve.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

from jasper.env_load import ENV_FILES

from ._shell_corpus import shell_files

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("deploy", "scripts")

# The two trees whose env files carry operator/wizard text. `/var/lib/jasper`
# is a prefix, so `-secrets` and `-intsecrets` are covered by the same string.
JASPER_ENV_ROOTS = ("/etc/jasper", "/var/lib/jasper")

# Basenames catch the same files when the directory came from a variable this
# file never assigns — `. "${ENV_DIR}/jasper.env"` inside deploy/lib/install/*,
# which runs with install.sh's variables. jasper.env_load.ENV_FILES is the
# union of every unit's persistent EnvironmentFile=, so it tracks new wizard
# files for free; the three added here are read by scripts and systemd
# drop-ins rather than by a unit's EnvironmentFile=, so they are not in it.
JASPER_ENV_BASENAMES = tuple(
    sorted(
        {PurePosixPath(path).name for path in ENV_FILES}
        | {"wifi_guardian.env", "airplay_mode.env", "grouping-airplay.env"}
    )
)

# Empty by design, and it stays that way: a sourcing site is a bug, not a
# style preference, so there is nothing to grandfather.
# REMOVAL CONDITION: delete this guard when the last bash consumer of a JTS
# env file is gone (every reader in deploy/ and scripts/ goes through
# jasper_env_file_get / jasper_env_file_export, or the file is read only by
# Python and systemd) — with no consumer left, there is nothing to source.
ALLOWLIST: frozenset[str] = frozenset()

# Deliberately out of scope: shapes that reach an env file without a
# `source`/`.` at all — `eval "$(cat FILE)"`, `export $(grep … | xargs)`,
# `source /dev/stdin <<EOF`, array-element loops. None appears in these trees,
# and a substring scanner cannot model them; this guard covers source/. only.

# `source X` / `. X` at the start of a command. The prefix alternatives cover
# the shapes these trees actually use: line start, after a separator, after
# `then`/`else`/`do`, and inside a `$(` substitution.
_SOURCE = re.compile(
    r"(?:^|[;&|(){}]|\bthen\b|\belse\b|\bdo\b)[ \t]*"
    r"(?:source|\.)[ \t]+"
    r"(?P<arg>[^;&|\n]+)"
)
_ASSIGN = re.compile(
    r"^[ \t]*(?:local[ \t]+|export[ \t]+|declare[ \t]+(?:-\w+[ \t]+)?)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<rhs>.*)$"
)
# `for VAR in PATHS; do . "$VAR"; done` binds VAR to the list, so the list is
# an assignment for resolution purposes.
_FOR_IN = re.compile(
    r"^[ \t]*for[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]+in[ \t]+"
    r"(?P<rhs>[^;\n]+)"
)
_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_POSITIONAL = re.compile(r"\$\{?(?:[1-9][0-9]*|@|\*)\b|\$\{?[1-9][0-9]*\}")
# Unanchored: `set -a` and the source it wraps share a line in the one-line
# loader shape `f(){ set -a; . "$1"; set +a; }`.
_SET_PREFIX = r"(?:^|[;&|(){}])[ \t]*set[ \t]+"
_ALLEXPORT_ON = re.compile(_SET_PREFIX + r"(?:-a\b|-o[ \t]+allexport\b)")
_ALLEXPORT_OFF = re.compile(_SET_PREFIX + r"(?:\+a\b|\+o[ \t]+allexport\b)")

_MAX_RESOLVE_PASSES = 6


def _assignments(text: str) -> dict[str, list[str]]:
    """Every `NAME=RHS` in the file, keyed by name. All right-hand sides are
    kept: a loader assigns its lib path two or three ways and any one of them
    is a real target."""
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGN.match(line) or _FOR_IN.match(line)
        if match:
            found.setdefault(match.group("name"), []).append(match.group("rhs"))
    return found


def _resolve(arg: str, assignments: dict[str, list[str]]) -> str:
    """Splice in every assignment a referenced variable has, repeatedly.

    The result is not a shell expansion — it is the union of the literal text
    the target could carry, which is what a substring check against the JTS
    env roots needs.
    """
    resolved = arg
    # Each name is spliced at most once: a self-referential assignment
    # (`PATH="$PATH:/x"`) would otherwise regrow its own reference every pass.
    expanded: set[str] = set()
    for _ in range(_MAX_RESOLVE_PASSES):
        names = {
            name
            for name in _VAR.findall(resolved)
            if name in assignments and name not in expanded
        }
        if not names:
            break
        expanded |= names
        for name in names:
            spliced = " ".join(assignments[name])
            resolved = re.sub(
                r"\$\{" + name + r"(?:[:#%/^,-][^}]*)?\}|\$" + name + r"\b",
                lambda _match, value=spliced: value,
                resolved,
            )
    return resolved


def _violations_in(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    assignments = _assignments(text)
    allexport = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        # A toggle can sit anywhere in the line, including before a source on
        # that same line; the last one on the line carries to the lines after.
        ons = [m.start() for m in _ALLEXPORT_ON.finditer(line)]
        offs = [m.start() for m in _ALLEXPORT_OFF.finditer(line)]
        line_allexport = allexport or bool(ons)
        if ons or offs:
            allexport = max(ons, default=-1) > max(offs, default=-1)
        if line.lstrip().startswith("#"):
            continue
        for match in _SOURCE.finditer(line):
            arg = match.group("arg").strip()
            resolved = _resolve(arg, assignments)
            named = next(
                (r for r in JASPER_ENV_ROOTS if r in resolved),
                None,
            ) or next(
                (b for b in JASPER_ENV_BASENAMES if b in resolved), None
            )
            if named:
                out.append((lineno, line.strip(), f"sources a JTS env file ({named})"))
            elif line_allexport and _POSITIONAL.search(resolved):
                out.append(
                    (
                        lineno,
                        line.strip(),
                        "sources a caller-supplied path under `set -a` "
                        "(a generic env-file loader)",
                    )
                )
    return out


def test_scanner_sees_the_shell_corpus():
    """Meta-check: shebang detection actually finds the tree. If this
    shrinks dramatically the guard below is vacuously green."""
    files = shell_files(*SCAN_DIRS)
    assert len(files) >= 40, [str(p) for p in files]
    assert any(p.name == "install.sh" for p in files)
    assert any(p.name == "jasper-aec-reconcile" for p in files)
    assert any(p.name == "jasper-apply-airplay-mode" for p in files)


@pytest.mark.parametrize(
    "bad",
    [
        # The literal shapes deleted from the two root scripts.
        'JASPER_ENV_FILE="${JASPER_ENV_FILE:-/etc/jasper/jasper.env}"\n'
        'source "$JASPER_ENV_FILE"\n',
        'ENV_FILE="${JASPER_AIRPLAY_MODE_ENV:-/var/lib/jasper/airplay_mode.env}"\n'
        '. "$ENV_FILE" 2>/dev/null || true\n',
        # Compartment files live under the same prefix.
        '. /var/lib/jasper-secrets/voice_keys.env\n',
        # Inside an if/then, and inside a command substitution.
        'if [ -r /etc/jasper/jasper.env ]; then . /etc/jasper/jasper.env; fi\n',
        'name="$(. /etc/jasper/jasper.env; printf %s "$X")"\n',
        # The generic loader: a caller-supplied path exported wholesale,
        # line-broken and collapsed onto one line.
        'load_env_file() {\n    local file="$1"\n    set -a\n'
        '    source "$file"\n    set +a\n}\n',
        'load_env_file() { set -a; . "$1"; set +a; }\n',
        # A directory the sourcing file never assigns (deploy/lib/install/*
        # runs with install.sh's variables) — only the basename is visible.
        '. "${ENV_DIR}/jasper.env"\n',
        'source "${STATE_DIR}/speaker_name.env"\n',
        # A glob loop over the whole wizard-owned directory.
        'for f in /var/lib/jasper/*.env; do . "$f"; done\n',
    ],
)
def test_classifier_catches_known_bad_shapes(bad):
    assert _violations_in(bad), bad


@pytest.mark.parametrize(
    "good",
    [
        # Shell LIBRARIES are code, not env files — sourcing them is the point.
        'ENV_FILE_LIB="/usr/local/lib/jasper/jasper-env-file.sh"\n'
        'source "$ENV_FILE_LIB"\n',
        'source "${REPO_DIR}/deploy/lib/install/env-migrations.sh"\n',
        '. "$(dirname "$0")/lib/jasper-sed-inplace.sh"\n',
        # Laptop-side state outside the JTS env trees, even under `set -a`.
        'REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"\n'
        'set -a\n. "${REPO_ROOT}/.env.local"\nset +a\n',
        # A caller-supplied path is fine when nothing is being exported.
        'read_lib() {\n    local file="$1"\n    source "$file"\n}\n',
        # Prose about the pattern is not the pattern.
        '# Never `source /etc/jasper/jasper.env` — read it instead.\n',
        # The sanctioned readers.
        'jasper_env_file_get /etc/jasper/jasper.env JASPER_MIC_DEVICE\n'
        'jasper_env_file_export /var/lib/jasper/aec_mode.env\n',
    ],
)
def test_classifier_allows_benign_shapes(good):
    assert not _violations_in(good), good


def test_no_bash_script_sources_a_jasper_env_file():
    """The repo-wide ratchet. On failure: read the keys you need with
    jasper_env_file_get from deploy/lib/jasper-env-file.sh, or the whole
    file with jasper_env_file_export when the values must reach child
    processes. Never source one."""
    failures = []
    for path in shell_files(*SCAN_DIRS):
        rel = str(path.relative_to(ROOT))
        if rel in ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line, reason in _violations_in(text):
            failures.append(f"{rel}:{lineno}: {reason}\n    {line}")
    assert not failures, (
        "JTS env files carry operator text and must never be shell-sourced "
        "(a `$(…)`, space or `#` in a value executes, splits or truncates); "
        "use jasper_env_file_get / jasper_env_file_export from "
        "deploy/lib/jasper-env-file.sh:\n" + "\n".join(failures)
    )
