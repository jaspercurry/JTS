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

import numpy as np

from jasper.audio_measurement.evidence_reasons import (
    CAPTURES_UNREADABLE,
    NO_ADMISSIBLE_CAPTURES,
    ROUND_SHAPE_INADMISSIBLE,
    TAKE_CURVES_NOT_BANKED,
    EvidenceUnavailable,
)

from ...measurement_programs import PURPOSE_SPEAKER
from ...run_manifest import kept_measurements
from ..record_index import Measurement, measurement_documents, record_path
from ..round_captures import PoseCapture, document_capture_id, radiated_band_of
from ..take_impulses import IMPULSES_KEY, TakeImpulsesUnreadable, impulse_for, take_impulses
from .captures import ADMISSIBLE_PHASES


def load_kept_captures(bundle_dir: Path) -> tuple[PoseCapture, ...]:
    """Every kept speaker take of an admissible phase, earliest first.

    A kept take is one its verdict accepted and its run manifest selected. Each
    reads its summed impulse, else the one driver role it kept, at repeat 0, on
    its own clock (ADR-0355). Raises :class:`EvidenceUnavailable` and never
    returns empty: a take that banked no band or kept no impulse refuses by
    that field, and a bundle with no such take refuses by its shape.
    """
    root = Path(bundle_dir)
    kept = sorted(kept_measurements(root, phases=ADMISSIBLE_PHASES, purposes=(PURPOSE_SPEAKER,)),
                  key=lambda item: (item[0].captured_at or "", item[0].path))
    if not kept:
        seen = Counter(row.phase for row, _document in measurement_documents(root))
        raise EvidenceUnavailable(ROUND_SHAPE_INADMISSIBLE if seen else NO_ADMISSIBLE_CAPTURES, {
            "admissible_phases": sorted(ADMISSIBLE_PHASES), "purpose": PURPOSE_SPEAKER,
            "phases_seen": dict(seen),
        })
    return tuple(_capture(root, row, document) for row, document in kept)


def _capture(root: Path, row: Measurement, document: Mapping[str, Any]) -> PoseCapture:
    take = {"record": record_path(row), "take_id": document_capture_id(document)}
    band = radiated_band_of(document)
    if band is None:
        raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {**take, "field": "curves"})
    try:
        kept = take_impulses(root, document)
    except TakeImpulsesUnreadable as exc:
        raise EvidenceUnavailable(CAPTURES_UNREADABLE, {**take, "detail": str(exc)}) from exc
    roles = sorted({one.role for one in kept})
    role = "summed" if "summed" in roles else roles[0] if len(roles) == 1 else None
    impulse = None if role is None else impulse_for(kept, role)
    if impulse is None:
        raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {**take, "field": IMPULSES_KEY, "roles": roles})
    wav = document.get("wav_path")
    return PoseCapture(
        capture_id=take["take_id"] or Path(row.path).stem,
        phase=row.phase,
        wav=root / wav if isinstance(wav, str) and wav else None,
        program=None,
        program_sha256="",
        azimuth_deg=None if row.position_deg is None else float(row.position_deg),
        vertical_deg=None,
        mark_distance_m=None,
        radiated_band_hz=band,
        sample_rate=impulse.sample_rate_hz,
        ir=impulse.samples,
        peak_idx=int(np.argmax(np.abs(impulse.samples))),
        preprocessing={
            "role": role, "impulse_source": "kept", "segment_id": impulse.segment_id,
            "pre_guard_samples": impulse.origin_index, "clock_shift_samples": impulse.clock_shift_samples,
            "microphone_correction": False,
        },
        record_path=root / record_path(row),
        record_document=document,
    )
