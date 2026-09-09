# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared loader for the generated ring ABI.

``rust/jasper-ring/layout.json`` is rendered from ``jasper_ring::layout`` (its
``layout_dump`` example) and OWNS every number the SHM header carries. Every
ring test pins its own spelling against this file instead of re-deriving the
repo root and re-parsing it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_LAYOUT_JSON = Path(__file__).resolve().parents[1] / "rust" / "jasper-ring" / "layout.json"


@lru_cache(maxsize=1)
def ring_abi() -> dict:
    return json.loads(_LAYOUT_JSON.read_text(encoding="utf-8"))
