# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The kept speaker takes classification reads, as the impulses their analyses kept.

See ADR-0392: no recording or program is opened, and nothing is deconvolved again.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_reasons import (
    NO_ADMISSIBLE_CAPTURES,
    NO_KEPT_TAKES,
    ROUND_SHAPE_INADMISSIBLE,
    TAKE_CURVES_NOT_BANKED,
    EvidenceUnavailable,
)

from ...measurement_programs import PURPOSE_SPEAKER
from ...run_manifest import kept_measurements
from ..record_index import Measurement, measurement_documents, record_path
from ..round_captures import PoseCapture, document_capture_id, radiated_band_of, record_captures
from ..take_impulses import IMPULSES_KEY
from .captures import ADMISSIBLE_PHASES


def load_kept_captures(bundle_dir: Path) -> tuple[PoseCapture, ...]:
    """Every kept speaker take of an admissible phase, earliest first.

    A kept take is one its verdict accepted and its run manifest selected. Each
    reads its summed impulse, else the one driver role it kept, at repeat 0, on
    its own clock (ADR-0355). Raises :class:`EvidenceUnavailable` and never
    returns empty: a take that banked no band or kept no impulse refuses by
    that field, and a bundle with no kept take refuses by why it has none.
    """
    root = Path(bundle_dir)
    kept = sorted(kept_measurements(root, phases=ADMISSIBLE_PHASES, purposes=(PURPOSE_SPEAKER,)),
                  key=lambda item: (item[0].captured_at or "", item[0].path))
    if not kept:
        documents = list(measurement_documents(root))
        admissible = [document_capture_id(document) or Path(row.path).stem
                      for row, document in documents if row.phase in ADMISSIBLE_PHASES]
        reason = NO_KEPT_TAKES if admissible else ROUND_SHAPE_INADMISSIBLE if documents else NO_ADMISSIBLE_CAPTURES
        raise EvidenceUnavailable(reason, {
            "admissible_phases": sorted(ADMISSIBLE_PHASES), "purpose": PURPOSE_SPEAKER,
            "phases_seen": dict(Counter(row.phase for row, _document in documents)),
            **({"takes_not_kept": admissible} if admissible else {}),
        })
    return tuple(_capture(root, row, document) for row, document in kept)


def _capture(root: Path, row: Measurement, document: Mapping[str, Any]) -> PoseCapture:
    take = {"record": record_path(row), "take_id": document_capture_id(document)}
    if radiated_band_of(document) is None:
        raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {**take, "field": "curves"})
    block = document.get(IMPULSES_KEY)
    rows = block.get("responses") if isinstance(block, Mapping) else None
    roles = sorted({str(one.get("role")) for one in rows if isinstance(one, Mapping)}) if isinstance(rows, list) else []
    role = "summed" if "summed" in roles else roles[0] if len(roles) == 1 else None
    if role is None:
        raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {**take, "field": IMPULSES_KEY, "roles": roles})
    capture, = record_captures(document, (role,), root, None, record_path=root / record_path(row),
                               wav=root / str(document.get("wav_path") or ""), clocked=True)
    return capture
