# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""JSON report writer for the tuning CLIs: sort_keys, no NaN, one write rule."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jasper.atomic_io import atomic_write_text

from ._refusal import EXIT_WRITE_FAILED, failed


def render_report(payload: Any) -> str:
    """``payload`` as the one JSON text every one of these tools publishes."""

    return json.dumps(payload, indent=2, sort_keys=True, default=float, allow_nan=False)


def write_report(
    payload: Any, out: str | None, default_path: Path, *, make_parents: bool = False,
) -> Path | None:
    """Write ``payload`` to ``out`` (``-`` = stdout) or ``default_path``.

    Returns the path, ``None`` for stdout. Without ``make_parents`` a missing
    parent is a ``FileNotFoundError``, not a directory this invented.
    """
    text = render_report(payload)
    if out == "-":
        print(text)
        return None
    target = Path(out) if out else default_path
    if not make_parents and not target.parent.is_dir():
        raise FileNotFoundError(f"no such directory: {target.parent}")
    atomic_write_text(target, text + "\n")
    return target


def file_report(
    payload: Any,
    out: str | None,
    default_path: Path,
    *,
    reason: str,
    make_parents: bool = False,
) -> Path | None | int:
    """:func:`write_report`, or the exit code a failed filing publishes.

    The one place an ``OSError`` while filing an artifact becomes
    ``EXIT_WRITE_FAILED``: the work was done and only the filing failed, so
    reporting it as an unreadable input sends the operator to the wrong place.
    A ``ValueError`` out of the strict writer is deliberately not caught -- a
    payload that will not serialise is not a filesystem problem.
    """
    try:
        return write_report(payload, out, default_path, make_parents=make_parents)
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, reason, str(exc))
