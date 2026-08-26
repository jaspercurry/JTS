# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shell corpus the static drift guards scan.

git-TRACKED, never a directory walk: an untracked scratch script under
deploy/ is not what ships and not what the CI shell lane lints (it selects
with `git ls-files`), so walking the directory would let a developer's
working tree decide whether a guard is red.
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _is_shell_file(path: Path) -> bool:
    """Shebang-detected sh/bash, plus the shebang-less source-only libs that
    mark themselves with a `# shellcheck shell=bash` directive near the top
    (e.g. scripts/_lib.sh). That directive may sit just below an SPDX license
    header, so scan the first several lines rather than only line 1."""
    try:
        with path.open("rb") as fh:
            head = [fh.readline().decode("utf-8", "replace").strip() for _ in range(8)]
    except OSError:
        return False
    first = head[0] if head else ""
    if first.startswith("#!"):
        return bool(re.search(r"\b(?:ba)?sh\b", first))
    return any(ln.replace(" ", "") == "#shellcheckshell=bash" for ln in head)


@lru_cache(maxsize=None)
def shell_files(*subtrees: str) -> tuple[Path, ...]:
    """Every tracked shell file under the given repo-relative subtrees."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", *subtrees],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found = []
    for rel in listing.split("\0"):
        if not rel:
            continue
        path = REPO / rel
        if path.is_file() and _is_shell_file(path):
            found.append(path)
    return tuple(sorted(found))
