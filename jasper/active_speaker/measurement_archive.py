# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read saved speaker measurements into the neutral frequency-view model.

The archive is an adapter over files, not part of a tuning flow. It reads a
bundle's banked take records and the identity its ``info.json`` states.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED
from jasper.json_fields import as_mapping

from . import bundles
from .frequency_view import FrequencyRun
from .measurement_document import frequency_run_from_documents
from .crossover_v2.record_index import has_banked_take, measurement_documents


@dataclass(frozen=True)
class ArchivedMeasurement:
    """The catalog facts needed to select one bundle."""

    id: str
    bundle_dir: Path
    started_at: Any = None
    state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "started_at": self.started_at, "state": self.state}


def list_measurements(sessions_dir: Path) -> tuple[ArchivedMeasurement, ...]:
    """List every bundle that banked a take."""

    runs = []
    for entry in bundles.list_bundles(sessions_dir):
        run_id = str(entry.get("session_id") or "")
        bundle_dir = Path(str(entry.get("bundle_dir") or ""))
        if not run_id or not has_banked_take(bundle_dir):
            continue
        runs.append(ArchivedMeasurement(
            id=run_id,
            bundle_dir=bundle_dir,
            started_at=entry.get("started_at"),
            state=str(entry.get("state") or "") or None,
        ))
    return tuple(runs)


def _bundle_identity(bundle_dir: Path) -> dict[str, Any]:
    """The build, topology and microphone calibration ``info.json`` names, or ``{}``."""

    try:
        info = json.loads((bundle_dir / "info.json").read_text())
    except (OSError, ValueError):
        return {}
    fingerprints = as_mapping(as_mapping(info).get("fingerprints"))
    return {
        "topology_id": fingerprints.get("topology_id"),
        "topology_fingerprint": fingerprints.get("topology_fingerprint"),
        "build_sha": fingerprints.get("build_sha"),
        "mic_calibration_id": as_mapping(fingerprints.get("mic")).get("calibration_id"),
    }


def load_measurement(run: ArchivedMeasurement) -> FrequencyRun:
    """Load one archive entry from its banked take records."""

    direct = frequency_run_from_documents(
        run_id=run.id,
        documents=[document for _, document in measurement_documents(run.bundle_dir)],
        started_at=run.started_at,
        state=run.state,
    )
    metadata = {**direct.metadata, **_bundle_identity(run.bundle_dir)}
    if not direct.series:
        # A run with no curve says why instead of drawing nothing (ADR-0373).
        metadata["curves"] = {"status": "unavailable", "reason": TAKE_CURVES_NOT_BANKED}
    return replace(direct, metadata=metadata)
