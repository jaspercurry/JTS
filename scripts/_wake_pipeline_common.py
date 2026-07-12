# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared destructive-output guard for standalone wake pipeline tools."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


MarkerPredicate = Callable[[Mapping[str, Any]], bool]


def is_safe_wake_pipeline_output(
    path: Path,
    *,
    owned_root: Path,
    protected_paths: Sequence[Path],
    marker_name: str,
    marker_matches: MarkerPredicate,
) -> bool:
    """Return whether ``path`` is owned strongly enough for a caller to replace.

    This predicate never removes anything. A tool-owned root and its descendants
    are accepted directly. A custom location needs a valid tool marker whose
    recorded output directory resolves back to the candidate, preventing a copied
    marker from conferring ownership on an unrelated directory.
    """
    try:
        expanded = path.expanduser()
        # Resolve only after inspecting the final component: resolving first would
        # hide a symlink and let a caller's rmtree target its destination.
        if expanded.is_symlink():
            return False
        candidate = expanded.resolve()
        owner = owned_root.expanduser().resolve()
        protected = {
            Path("/").resolve(),
            Path.home().resolve(),
            Path.cwd().resolve(),
            *(item.expanduser().resolve() for item in protected_paths),
        }
    except (OSError, RuntimeError):
        return False

    # Reject each protected path and every candidate that contains one. This
    # keeps a force operation from replacing an input by targeting its ancestor.
    if any(candidate == item or candidate in item.parents for item in protected):
        return False
    if candidate == owner or owner in candidate.parents:
        return True

    marker = candidate / marker_name
    try:
        if marker.is_symlink() or not marker.is_file():
            return False
        with open(marker) as f:
            data = json.load(f)
        if not isinstance(data, dict) or not marker_matches(data):
            return False
        output_dir = data.get("output_dir")
        if not isinstance(output_dir, str) or not output_dir:
            return False
        return Path(output_dir).expanduser().resolve() == candidate
    except Exception:  # noqa: BLE001 - ownership validation must fail closed.
        return False
