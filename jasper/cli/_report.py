# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""JSON report writer for the tuning CLIs: sort_keys, no NaN."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jasper.atomic_io import atomic_write_text


def _jsonable(value: Any) -> Any:
    """numpy scalars as numbers; paths and anything else as text."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def render_report(payload: Any) -> str:
    """``payload`` as the one JSON text every one of these tools publishes."""

    return json.dumps(
        payload, indent=2, sort_keys=True, default=_jsonable, allow_nan=False
    )


def output_path(value: str) -> Path:
    if value == "-":
        raise argparse.ArgumentTypeError("an artifact path is required")
    return Path(value)


def write_report(
    payload: Any, out: str | Path | None, default_path: Path, *, make_parents: bool = False,
) -> Path:
    """Write an artifact; a missing parent requires ``make_parents``."""
    target = output_path(str(out)) if out is not None else default_path
    if not make_parents and not target.parent.is_dir():
        raise FileNotFoundError(f"no such directory: {target.parent}")
    atomic_write_text(target, render_report(payload) + "\n")
    return target
