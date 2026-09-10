# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Select banked takes from their saved identities and graph scopes.

No index file exists (ADR-0198): every read rescans the banked takes and filters
them in Python, so the take files are the single source of truth at the read
side as well as the write side.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from jasper.audio_measurement.bundles import read_artifact_manifest, relative_artifact_path
from jasper.audio_measurement.evidence_identity import ArtifactIdentity

from ..bundles import BUNDLE_KIND
from ..commissioning_evidence_store import CommissioningEvidenceStore, EVIDENCE_ROOT
from .contracts import (
    BANKED_TAKE_GLOB,
    MEASURE_KIND_KEY,
    POSITION_EVIDENCE_KIND,
)

__all__ = [
    "Measurement",
    "MeasurementCaptureIdentityError",
    "bundle_measurements",
    "measurement_documents",
    "reopen_measurement_capture",
]


@dataclass(frozen=True)
class Measurement:
    """One selected take. ``path`` is the id ``bank`` returned for it."""

    path: str
    session_id: str
    kind: str
    phase: str
    position_deg: int | None
    vertical_deg: int
    candidate_id: str
    captured_at: str | None
    graph_scope: str = ""
    graph_fingerprint: str = ""


class MeasurementCaptureIdentityError(ValueError):
    """A captured take does not name its exact dependent WAV."""


def reopen_measurement_capture(
    bundle_dir: Path, record_path: str | Path,
) -> tuple[dict[str, Any], bytes | None]:
    """Verify a banked take and its WAV; incomplete takes have no capture bytes."""
    info = json.loads((bundle_dir / "info.json").read_text())
    store = CommissioningEvidenceStore.open(bundle_dir, expected_session_id=info["session_id"])
    artifacts = {row["path"]: row for row in read_artifact_manifest(bundle_dir)["artifacts"]}

    def identity(path: str | Path) -> ArtifactIdentity:
        relative = relative_artifact_path(bundle_dir, path)
        recorded = artifacts[relative]
        return ArtifactIdentity(BUNDLE_KIND, store.session_id, relative,
                                recorded["sha256"], recorded["byte_size"])

    record_identity = identity(record_path)
    record = store.reopen_json_artifact(record_identity)
    if record.get("measurement_status") != "captured" or record.get("incident"):
        return record, None
    wav_identity = identity(record["wav_path"])
    if (
        wav_identity.byte_size <= 0
        or record.get("wav_sha256") != wav_identity.sha256
        or wav_identity.relative_path not in artifacts[record_identity.relative_path].get("dependencies", [])
    ):
        raise MeasurementCaptureIdentityError("measurement_capture_identity_mismatch")
    return record, store.reopen_artifact(wav_identity)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def played_graph_fingerprint(document: Mapping[str, Any]) -> str:
    provenance = document.get("provenance") or {}
    return str((provenance.get("graph") or {}).get("fingerprint") or document.get("graph_fingerprint") or "")


def _position_deg(value: Any) -> int | None:
    """The signed whole-degree bearing, or ``None`` where none was commanded.

    ``bool`` is an ``int`` subclass, so it is excluded rather than read as a
    bearing of 0 or 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _captured_at(value: Any) -> str | None:
    """The take's own capture time as ISO-8601 UTC, or ``None``.

    The builders disagree about the type: the cloud position emits a Unix epoch
    ``float`` where the lateral pose, the entry baseline and the phase capture
    emit ``%Y-%m-%dT%H:%M:%SZ``.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
    except (OverflowError, ValueError, OSError):
        # ``json.loads`` accepts a bare NaN, and a number outside the
        # platform's ``time_t`` lands here too.
        return None


def _row(path: str, document: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The identity fields from one banked file, or ``None`` if it is not a take."""
    if document.get("kind") != POSITION_EVIDENCE_KIND:
        return None
    return (
        path,
        _text(document.get("session_id")),
        _text(document.get(MEASURE_KIND_KEY)),
        _text(document.get("phase")),
        _position_deg(document.get("position_deg")),
        # A pose is always at SOME height: absent or malformed reads as 0.
        _position_deg(document.get("vertical_deg")) or 0,
        _text(document.get("candidate_id")),
        _captured_at(document.get("captured_at")),
        _text(document.get("graph_scope")),
        _text(document.get("graph_fingerprint")),
    )


def _load(take: Path) -> Mapping[str, Any]:
    """One banked file's JSON, or empty when it is not readable."""
    try:
        document = json.loads(take.read_text())
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def measurement_documents(bundle_dir: Path) -> Iterator[tuple[Measurement, Mapping[str, Any]]]:
    """Canonical takes and their metadata, read once and sorted by relative path."""
    artifacts = Path(bundle_dir) / EVIDENCE_ROOT / "artifacts"
    for take in sorted(artifacts.glob(BANKED_TAKE_GLOB), key=lambda path: path.as_posix()):
        document = _load(take)
        row = _row(take.relative_to(artifacts).as_posix(), document)
        if row is not None:
            yield Measurement(*row), document


def bundle_measurements(
    bundle_dir: Path,
    *,
    kind: str | None = None,
    phase: str | None = None,
    position_deg: int | None = None,
    vertical_deg: int | None = None,
    candidate_id: str | None = None,
) -> tuple[Measurement, ...]:
    """One bundle's takes, matching every filter — the offline reader's door.

    ``phase`` is what a take IS (the walk pose, the entry baseline, a CHECK);
    ``kind`` is what it MEASURES (baseline / candidate / verify).
    A pose is a bearing AND a height, so a caller naming only ``position_deg``
    is handed raised seats too; every axis is ``None``-means-no-filter, and it
    is the pose readers above this that pin the height they mean. The rows
    select; the take files still decide — every caller re-reads the file it was
    pointed at through its own accept rule.
    """
    return tuple(
        row for row, _ in measurement_documents(bundle_dir)
        if (kind is None or row.kind == kind)
        and (phase is None or row.phase == phase)
        and (position_deg is None or row.position_deg == position_deg)
        and (vertical_deg is None or row.vertical_deg == vertical_deg)
        and (candidate_id is None or row.candidate_id == candidate_id)
    )
