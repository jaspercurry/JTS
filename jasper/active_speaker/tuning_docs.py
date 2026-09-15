# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered documents for tuning operators."""

from __future__ import annotations

from pathlib import Path
from typing import Any

READING_ORDER: tuple[tuple[str, str, str], ...] = (
    (
        "entry and tool menu",
        "tuning-operator-runbook.md",
        "short entry contract, tool discovery and optional examples",
    ),
    (
        "optional methodology",
        "tuning-methodology.md",
        "measurement science and traps",
    ),
    (
        "optional doctrine",
        "measurement-loop-doctrine.md",
        "roles and physical constraints",
    ),
)

_INSTALLED_DOCS_DIR = Path("/opt/jasper/docs")
_REPO_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def doc_path(name: str) -> str:
    for candidate in (_INSTALLED_DOCS_DIR / name, _REPO_DOCS_DIR / name):
        if candidate.exists():
            return str(candidate)
    return f"docs/{name}"


def reading_order() -> list[dict[str, Any]]:
    order: list[dict[str, Any]] = []
    for label, name, gives in READING_ORDER:
        path = doc_path(name)
        try:
            blob = Path(path).read_bytes()
        except OSError:
            size, lines = None, None
        else:
            size, lines = len(blob), blob.count(b"\n")
        order.append(
            {
                "name": name,
                "label": label,
                "path": path,
                "gives": gives,
                "bytes": size,
                "lines": lines,
            }
        )
    return order
