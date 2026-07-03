# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-file pin for the SHM-ring slot-count CEILING (P3 review Nit 3).

The maximum ``n_slots`` for the jts SHM ring is defined in THREE places that
must stay in lockstep by hand:

- the C ioplug header ``c/jts-ring-ioplug/jts_ring_shm.h`` (``JTS_RING_MAX_SLOTS``),
- the Rust reader crate ``rust/jasper-ring/src/layout.rs`` (``MAX_N_SLOTS``),
- the Rust outputd config ``rust/jasper-outputd/src/config.rs`` (``MAX_SHM_RING_SLOTS``).

Unlike the header OFFSETS — which the golden-layout ``_Static_assert`` and the
Rust layout test pin bit-for-bit — nothing tied these three MAX constants
together, so a mismatch was caught only at RUNTIME on the Pi (the reader's
geometry validation rejects an ``n_slots`` the writer created, failing loud on
arm). This test moves that failure to CI: if the three ceilings drift, it fails
here instead of on-device.

If you intentionally raise/lower the ceiling, change all three in the same
commit and this test keeps passing (it asserts they are EQUAL, not a specific
literal).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_C_HEADER = _REPO_ROOT / "c" / "jts-ring-ioplug" / "jts_ring_shm.h"
_RING_LAYOUT_RS = _REPO_ROOT / "rust" / "jasper-ring" / "src" / "layout.rs"
_OUTPUTD_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-outputd" / "src" / "config.rs"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"source not present: {path}")
    return path.read_text(encoding="utf-8")


def _extract(pattern: str, text: str, label: str) -> int:
    m = re.search(pattern, text)
    assert m is not None, f"could not find {label} definition (pattern {pattern!r})"
    return int(m.group(1))


def test_ring_slot_ceiling_agrees_across_c_and_both_rust_crates():
    c_max = _extract(
        r"#define\s+JTS_RING_MAX_SLOTS\s+(\d+)u", _read(_C_HEADER), "JTS_RING_MAX_SLOTS"
    )
    ring_max = _extract(
        r"pub const MAX_N_SLOTS:\s*u32\s*=\s*(\d+);",
        _read(_RING_LAYOUT_RS),
        "MAX_N_SLOTS",
    )
    outputd_max = _extract(
        r"pub const MAX_SHM_RING_SLOTS:\s*u32\s*=\s*(\d+);",
        _read(_OUTPUTD_CONFIG_RS),
        "MAX_SHM_RING_SLOTS",
    )
    assert c_max == ring_max == outputd_max, (
        "SHM-ring slot-count ceiling drifted across the three source-of-truth "
        f"definitions: JTS_RING_MAX_SLOTS={c_max} (C header), "
        f"MAX_N_SLOTS={ring_max} (jasper-ring layout.rs), "
        f"MAX_SHM_RING_SLOTS={outputd_max} (jasper-outputd config.rs). "
        "Change all three in the same commit — the reader rejects a writer's "
        "out-of-range n_slots at RUNTIME, so a mismatch only surfaces on-Pi arm."
    )
