# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""JSON artifacts and answers for tuning CLIs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jasper.atomic_io import atomic_write_text


def _jsonable(value: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def render_report(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=_jsonable, allow_nan=False)


def report_answer(view: str, out: Path | None, **fields: Any) -> dict[str, Any]:
    if out is not None:
        fields.update(out=str(out), bytes=out.stat().st_size)
    return {"view": view, **fields}


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
