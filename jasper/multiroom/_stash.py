# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Prior-CamillaDSP-config-path stash mechanics shared by the grouping
reconciler's apply arms (`leader_config`, `follower_config`): each arm
persists the config path to restore on unbond, keyed by its own stash
file so the arms never fight over one file. Parametrised on that path;
callers keep their own module-level names as thin delegations (a test
seam — `read_stash`/`_write_stash`/`_clear_stash` are monkeypatched
per-module in the arm's own tests)."""
from __future__ import annotations

import os
from pathlib import Path

from .. import atomic_io


def camilla():
    """Return camilla#1 without coupling an apply arm to a web module."""
    from jasper.camilla import primary_controller

    return primary_controller()


def read_stash(path: str) -> str | None:
    """The stashed prior config path at ``path``, or None (no stash /
    unreadable)."""
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    return text or None


def write_stash(value: str, path: str) -> None:
    atomic_io.atomic_write_text(path, value + "\n", mode=0o644)


def clear_stash(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
