# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-band controllability: the raw numbers banked rounds already carry.

Read-only, and derives nothing. Every number here was banked by a round that
already ran; this module opens no capture, writes no state, and gates nothing.
It is the door onto two blocks of each ``round_receipt.json``, passed through
in the shape the writers banked them:

* ``round_measurements.realization.bands`` — the delta probe's own per-band
  rows, ``{band_hz, n_bins, ratio, graded}``, banked by
  :func:`~jasper.active_speaker.crossover_v2.coordinator._round_measurements`.
  ``ratio`` is the fitted realized/commanded slope; ``None`` where the probe
  had too few bins to fit one.
* ``round_axes.quality.evidence.spec_bands`` beside ``verification.spec`` —
  the grader's per-round misses, and the round verdict.

**Pooling is the reader's, not this module's** (ADR-0198). A mean across
rounds, a spread, a confidence label and a spec pass fraction are all
statistics over these rows, and the reader asking for them is the party that
knows which rounds are comparable. The numbers are the speaker's; the
statistics are whoever is asking.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["ROUND_RECEIPT_GLOB", "read_controllability_ledger"]

#: ``*`` one is the bundle id, ``*`` two the minting capture session id — two
#: distinct namespaces, as
#: :data:`~jasper.active_speaker.candidate_bank.CANDIDATE_ARTIFACT_GLOB` states
#: for the sibling artifact in the same directory.
ROUND_RECEIPT_GLOB = "*/evidence/v1/artifacts/crossover_v2/*/round_receipt.json"

#: Receipts PARSED before giving up. The bundle root is retention-capped
#: (``DEFAULT_SESSIONS_MAX_BUNDLES = 12``), so a healthy box is far under this
#: and a pathological directory cannot turn one poll into unbounded work. Path
#: order is not chronological (a bundle dir is ``uuid4().hex[:12]``), so this
#: BOUNDS work rather than selecting recent history.
MAX_RECEIPTS_SCANNED = 64

#: A receipt banks identities and verdicts, never curves, so the writer's own
#: artifact is orders of magnitude under this. Defensive ceiling, not a
#: contract.
MAX_RECEIPT_BYTES = 1024 * 1024


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _load(path: Path) -> Mapping[str, Any] | None:
    """Parse one receipt, or ``None`` when it is not usable. Never raises.

    An unreadable receipt costs that round; losing the whole read to one bad
    file would be the trade this module refuses everywhere else.
    """
    try:
        if path.stat().st_size > MAX_RECEIPT_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, Mapping) else None


def _round(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """One receipt's two blocks, as banked. Absent blocks read as empty."""
    realization = _mapping(
        _mapping(receipt.get("round_measurements")).get("realization")
    )
    evidence = _mapping(
        _mapping(_mapping(receipt.get("round_axes")).get("quality")).get("evidence")
    )
    misses = evidence.get("spec_bands")
    return {
        "bands": dict(_mapping(realization.get("bands"))),
        "spec": str(_mapping(receipt.get("verification")).get("spec") or ""),
        "spec_misses": list(misses) if isinstance(misses, (list, tuple)) else [],
    }


def read_controllability_ledger(root: Path | None = None) -> dict[str, Any]:
    """Every banked round's raw controllability rows, in a stable path order.

    ``root`` defaults to :func:`~jasper.active_speaker.bundles.sessions_dir`,
    the bundle root the sibling banked-artifact readers walk. A box with no
    banked rounds yields ``rounds: []`` — the absence is the answer, and it has
    the same shape as every other answer.

    Sorted for determinism only. **Not chronological**: a bundle directory is
    named ``uuid4().hex[:12]``, so path order carries no time information.
    """
    from jasper.active_speaker.bundles import sessions_dir

    bundle_root = Path(root) if root is not None else sessions_dir()
    try:
        found = sorted(bundle_root.glob(ROUND_RECEIPT_GLOB))
    except OSError:
        found = []
    rounds = [
        _round(receipt)
        for receipt in (_load(path) for path in found[-MAX_RECEIPTS_SCANNED:])
        if receipt is not None
    ]
    return {"n_rounds": len(rounds), "rounds": rounds}
