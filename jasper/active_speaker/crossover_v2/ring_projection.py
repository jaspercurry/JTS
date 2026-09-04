# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Re-project a banked round into the capture-ring layout its two readers want.

``jasper-classify-features`` and ``jasper-round-views distortion`` find captures
through :data:`~.evidence_packet.RING_SIDECAR_GLOB` — a sidecar JSON beside
its WAV in a sibling ``wav/`` — the one thing #3285's bank does not carry.
Two facts the bank spells differently are restated here: stems are
``<microseconds>_<take_id>``, and the ring is scoped by ``info.json``'s
``session_id`` (the BUNDLE id), never the take record's capture ``session_id``,
which rides along as an alias. Program WAVs are not projected — the CLIs
resolve those from the bundle themselves.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jasper.attribution.session_identity import (
    ALIAS_CAPTURE_SESSION_ID,
    SessionIdentity,
    SessionIdentityError,
    stamp_session_identity,
)

from ..commissioning_evidence_store import EVIDENCE_ROOT
from .evidence_packet import round_artifact_dir
from .record_index import bundle_measurements

__all__ = [
    "NOTHING_TO_PROJECT",
    "SKIP_NO_CAPTURED_AT",
    "SKIP_NO_PHASE",
    "SKIP_NO_WAV_PATH",
    "SKIP_UNREADABLE_RECORD",
    "SKIP_WAV_ESCAPES_BUNDLE",
    "SKIP_WAV_MISSING",
    "ProjectedTake",
    "RingProjection",
    "RingProjectionRefused",
    "SkippedTake",
    "project_ring",
]

#: The ring's two halves. :data:`~.evidence_packet.RING_SIDECAR_GLOB` finds
#: the sidecar; both readers pair it at ``sidecar.parent.parent / "wav"``.
_SIDECAR_SUBDIR = "sidecar"
_WAV_SUBDIR = "wav"

#: Refusal: the bundle is readable and holds no take this can project.
NOTHING_TO_PROJECT = "nothing_to_project"

#: Per-take skip reasons, reported by name.
SKIP_UNREADABLE_RECORD = "unreadable_record"
SKIP_NO_PHASE = "no_phase"
SKIP_NO_CAPTURED_AT = "no_captured_at"
SKIP_NO_WAV_PATH = "no_wav_path"
SKIP_WAV_ESCAPES_BUNDLE = "wav_escapes_bundle"
SKIP_WAV_MISSING = "wav_missing"


class RingProjectionRefused(RuntimeError):
    """This bundle cannot be projected, and ``reason`` says why by name."""

    def __init__(self, reason: str, detail: Mapping[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail: dict[str, Any] = dict(detail or {})


@dataclass(frozen=True)
class ProjectedTake:
    """One take, in the ring. ``linked`` is False when the WAV was copied."""

    take_id: str
    phase: str
    stem: str
    sidecar: Path
    wav: Path
    linked: bool


@dataclass(frozen=True)
class SkippedTake:
    """One take that did not reach the ring, and the named reason it did not."""

    path: str
    reason: str


@dataclass(frozen=True)
class RingProjection:
    """What one :func:`project_ring` run put on disk."""

    dumps_dir: Path
    session_id: str
    capture_session_id: str
    projected: tuple[ProjectedTake, ...]
    skipped: tuple[SkippedTake, ...]


def _bundle_session_id(bundle_dir: Path) -> str:
    """The bundle's own ``session_id`` — the id both readers scope the ring by.

    Raises ``OSError``/``ValueError``: unreadable is a different answer from
    "readable, and holds nothing to project", and the CLI exits differently.
    """
    info = json.loads((bundle_dir / "info.json").read_text())
    session_id = info.get("session_id") if isinstance(info, Mapping) else None
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(
            f"{bundle_dir / 'info.json'} carries no string session_id, so the "
            "projected ring could not be scoped to this round"
        )
    return session_id


def _microseconds(captured_at: str) -> int | None:
    """``captured_at`` as a microsecond epoch, or ``None`` if it will not parse.

    :func:`~.record_index.bundle_measurements` has already reconciled the
    builders' two shapes to ``%Y-%m-%dT%H:%M:%SZ``; a naive stamp is read as UTC.
    """
    try:
        moment = datetime.fromisoformat(captured_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1e6)


def _place(source: Path, destination: Path, *, copy: bool) -> bool:
    """Put the capture WAV at ``destination``. True when it was hardlinked.

    Hardlinked by default — a round's captures run to tens of megabytes — and
    ``os.link`` raises ``EXDEV`` when bundle and ring sit on different devices.
    """
    if not copy:
        try:
            os.link(source, destination)
            return True
        except OSError:
            pass
    shutil.copyfile(source, destination)
    return False


def project_ring(
    bundle_dir: Path,
    dumps_dir: Path,
    *,
    copy: bool = False,
    setup_calibration_id: str | None = None,
) -> RingProjection:
    """Write ``bundle_dir``'s banked takes into ``dumps_dir`` as a capture ring.

    An existing ``dumps_dir`` is added to, never emptied. ``setup_calibration_id``
    names the measurement mic: the bank does not carry it, and
    :func:`~.harmonic_evidence._calibration_for` reads it off the sidecar to pick
    the sign convention a supplied calibration file is parsed under.

    Raises :class:`RingProjectionRefused` when no take is projectable;
    :class:`OSError`/:class:`ValueError` mean the bundle itself is unreadable,
    which the CLI exits differently on.
    """
    bundle_dir = Path(bundle_dir)
    dumps_dir = Path(dumps_dir)

    # One round per bundle: two would project under ONE bundle id, the pooling
    # `harmonic_evidence._scope_captures` refuses but could not detect here.
    round_dir, why = round_artifact_dir(bundle_dir)
    if round_dir is None:
        raise ValueError(f"cannot read the round: {why}")

    session_id = _bundle_session_id(bundle_dir)
    sidecar_dir = dumps_dir / _SIDECAR_SUBDIR
    wav_dir = dumps_dir / _WAV_SUBDIR
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    wav_dir.mkdir(parents=True, exist_ok=True)

    artifacts = bundle_dir / EVIDENCE_ROOT / "artifacts"
    projected: list[ProjectedTake] = []
    skipped: list[SkippedTake] = []
    for row in bundle_measurements(bundle_dir):
        record_path = artifacts / row.path
        take = _read_take(record_path, row.captured_at)
        if isinstance(take, str):
            skipped.append(SkippedTake(row.path, take))
            continue
        stamp, phase, document = take

        source = _resolve_wav(bundle_dir, document)
        if isinstance(source, str):
            skipped.append(SkippedTake(row.path, source))
            continue

        take_id = str(document.get("take_id") or record_path.stem)
        # The take id keeps the stem unique when two takes share a second (the index
        # publishes second resolution); ties then order by take id.
        stem = f"{stamp}_{take_id}"
        sidecar = sidecar_dir / f"{stem}.json"
        wav = wav_dir / f"{stem}.wav"
        wav.unlink(missing_ok=True)
        linked = _place(source, wav, copy=copy)
        sidecar.write_text(
            json.dumps(
                _sidecar_document(
                    document,
                    session_id=session_id,
                    capture_session_id=row.session_id,
                    setup_calibration_id=setup_calibration_id,
                ),
                indent=1,
            )
        )
        projected.append(ProjectedTake(take_id, phase, stem, sidecar, wav, linked))

    if not projected:
        raise RingProjectionRefused(
            NOTHING_TO_PROJECT,
            {
                "bundle_dir": bundle_dir.name,
                "session_id": session_id,
                "n_takes_seen": len(skipped),
                "skipped": [
                    {"path": take.path, "reason": take.reason} for take in skipped
                ],
            },
        )
    return RingProjection(
        dumps_dir=dumps_dir,
        session_id=session_id,
        capture_session_id=round_dir.name,
        projected=tuple(projected),
        skipped=tuple(skipped),
    )


def _read_take(
    record_path: Path, captured_at: str | None
) -> tuple[int, str, dict[str, Any]] | str:
    """``(stamp_us, phase, record)``, or a named skip reason."""
    try:
        document = json.loads(record_path.read_text())
    except (OSError, ValueError):
        return SKIP_UNREADABLE_RECORD
    if not isinstance(document, dict):
        return SKIP_UNREADABLE_RECORD
    phase = document.get("phase")
    if not isinstance(phase, str) or not phase:
        return SKIP_NO_PHASE
    if captured_at is None:
        return SKIP_NO_CAPTURED_AT
    stamp = _microseconds(captured_at)
    if stamp is None:
        return SKIP_NO_CAPTURED_AT
    return stamp, phase, document


def _resolve_wav(bundle_dir: Path, document: Mapping[str, Any]) -> Path | str:
    """The banked capture WAV this record names, or a named skip reason.

    ``wav_path`` is read off the record, never re-derived, and is
    bundle-relative; a path that leaves the bundle is refused, not followed.
    """
    raw = document.get("wav_path")
    if not isinstance(raw, str) or not raw:
        return SKIP_NO_WAV_PATH
    candidate = (bundle_dir / raw).resolve()
    if not candidate.is_relative_to(bundle_dir.resolve()):
        return SKIP_WAV_ESCAPES_BUNDLE
    if not candidate.is_file():
        return SKIP_WAV_MISSING
    return candidate


def _sidecar_document(
    document: Mapping[str, Any],
    *,
    session_id: str,
    capture_session_id: str,
    setup_calibration_id: str | None,
) -> dict[str, Any]:
    """The take record, plus the two fields the ring's layout carried.

    The record travels WHOLE: the readers' key union is wider than any subset
    this module could pick without going stale against them.
    """
    projected = dict(document)
    identity = SessionIdentity(session_id=session_id)
    try:
        identity = identity.with_alias(ALIAS_CAPTURE_SESSION_ID, capture_session_id)
    except SessionIdentityError:
        # A capture id failing the identity charset is dropped rather than taking the
        # projection with it: the alias is an audit join, `session_id` is the scope.
        pass
    stamp_session_identity(projected, identity)
    if setup_calibration_id is not None:
        projected["setup_calibration_id"] = setup_calibration_id
    return projected
