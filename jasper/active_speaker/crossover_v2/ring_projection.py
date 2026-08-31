# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Re-project a banked round into the capture-ring layout its two readers want.

``jasper-classify-features`` and ``jasper-read-distortion`` are the §6 mechanism
discriminators, and both find their captures through one binding —
:data:`~.evidence_packet.RING_SIDECAR_GLOB`, a sidecar JSON beside its WAV in a
sibling ``wav/``. The speaker-side producer of that layout died with the
retention seam (#3250), so for a while the only rings that existed were corpora
pulled off a Pi before it.

**Nothing was actually lost.** Since #3285 every wired round banks the capture
WAV itself (``summed/summed_<take>_<uuid>.wav``) beside a per-take record
carrying ``wav_path``, ``wav_sha256``, ``captured_at``, ``phase``,
``diagnostic``, ``capture_integrity`` and ``frame_ledger`` — every field either
reader consumes. What the bank does not carry is the ring's *layout*, and that
is the whole of the gap. This module closes it laptop-side: it reads a banked
bundle and writes the ring the readers already know how to open, re-projecting
metadata that is already on disk rather than re-measuring anything.

Three facts have to be restated in the projection because the bank spells them
differently, and each is a place a silent skip lives if it is got wrong:

1. **The stem is the timestamp.** ``load_round_captures`` parses
   ``float(sidecar.stem.split("_")[0]) / 1e6`` as a microsecond epoch, and a
   non-numeric leading field makes it skip the capture without a word. Stems
   are minted ``<microseconds>_<take_id>``.
2. **The session identity is the BUNDLE id**, ``info.json``'s ``session_id``
   (``d0eca8f5a24d``-shaped) — never the take record's own ``session_id``,
   which is the RELAY namespace (``wired-DBq7UPOcyyuJwCfWwQVOwA``-shaped). Both
   readers scope the ring by the bundle id, so stamping the relay one would
   scope every capture out.
3. **Both ids travel together.** The relay id rides along as an alias under
   :data:`~jasper.attribution.session_identity.ALIAS_RELAY_SESSION_ID`. That
   pairing has never existed in a banked artifact before — the seam
   :func:`~.harmonic_evidence._scope_captures` names as the only unguarded one
   left is precisely that nothing maps between the two namespaces — so a
   projected ring is the first place a mis-scoped read is checkable rather than
   only auditable after the fact.

**Takes are SELECTED through the measurement index and DECIDED by their own
record**, the rule :func:`~.evidence_packet._banked_takes` already follows, so
"what a banked take is and where it lives" keeps one owner. That index also
owns the ``captured_at`` normalization: a cloud position banks a float epoch
where a phase capture banks ``%Y-%m-%dT%H:%M:%SZ``, and
:func:`~.record_index.bundle_measurements` already reconciles the two to
second-resolution ISO. Second resolution is what the phase captures carry
natively, so the projection publishes it uniformly rather than inventing
sub-second digits for half the ring.

**Nothing is skipped silently.** Every take this refuses to project comes back
in :attr:`RingProjection.skipped` with a named reason — the readers' own
skip-and-say-nothing behaviour is what makes a half-projected ring look like a
complete one.

Program WAVs are deliberately NOT projected. Both CLIs resolve them from the
bundle through :func:`~.evidence_packet.round_program_dir`, which already knows
the two shapes they are written in; a copy in the ring would be a second
answer to a question that has an owner.
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
    ALIAS_RELAY_SESSION_ID,
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

#: The reader-facing halves of the ring, spelled once here because
#: :data:`~.evidence_packet.RING_SIDECAR_GLOB` finds one and both readers pair
#: it with the other at ``sidecar.parent.parent / "wav"``.
_SIDECAR_SUBDIR = "sidecar"
_WAV_SUBDIR = "wav"

#: Refusal: the bundle is readable and holds no take this can project.
NOTHING_TO_PROJECT = "nothing_to_project"

#: Per-take skip reasons. Named rather than prose because the CLI reports them
#: and a test asserts on them; the readers' silent equivalents are what this
#: module exists to make visible.
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
    relay_session_id: str
    projected: tuple[ProjectedTake, ...]
    skipped: tuple[SkippedTake, ...]


def _bundle_session_id(bundle_dir: Path) -> str:
    """The bundle's own ``session_id`` — the id both readers scope the ring by.

    Raises :class:`OSError` or :class:`ValueError` rather than refusing: a
    bundle whose ``info.json`` will not parse is unreadable, which is a
    different answer from "readable, and holds nothing to project".
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

    :func:`~.record_index.bundle_measurements` has already reconciled the two
    shapes the builders emit into ``%Y-%m-%dT%H:%M:%SZ``, so this parses one
    shape. A record carrying a naive stamp is read as UTC, which is what the
    ``Z`` the index writes means.
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

    Hardlinked by default because a ring is a re-projection of bytes the bundle
    already holds, and a round's captures run to tens of megabytes. The
    fallback is not defensive: a bundle on an external disk and a scratch ring
    on the internal one is the ordinary case, and ``os.link`` raises ``EXDEV``
    across devices.
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

    ``dumps_dir`` is the path a caller then hands to ``--dumps``. It is created
    if absent; an existing one is added to, never emptied, because a caller
    projecting two rounds into one ring is doing the thing the readers' own
    session scoping exists for.

    ``setup_calibration_id`` names the measurement microphone. The bank does
    not carry it — it is only ever logged — and
    :func:`~.harmonic_evidence._calibration_for` reads it off the sidecar to
    choose the sign convention a supplied calibration file is parsed under, so
    an operator passing ``--calibration`` for a mic whose convention is not the
    default must say which mic it was. Omitted, the sidecar carries none and
    the reader publishes the empty id it actually had.

    Raises :class:`RingProjectionRefused` when the bundle holds no projectable
    take. :class:`OSError` and :class:`ValueError` mean the bundle itself could
    not be read — a different answer, and the CLI gives it a different exit
    code.
    """
    bundle_dir = Path(bundle_dir)
    dumps_dir = Path(dumps_dir)

    # The one-round rule the consumers already apply. A bundle carrying two
    # rounds would project both under ONE bundle id, which is exactly the
    # pooling `harmonic_evidence._scope_captures` refuses — and it could not
    # refuse it here, because every sidecar would carry the id it scoped by.
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
        # `<microseconds>_<take_id>`: the leading field is the only part a
        # reader parses, and the take id makes the stem unique even when two
        # takes share a second — which they can, the index publishing second
        # resolution. Ties then order by take id, in both the glob and the
        # readers' stable sort over it.
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
                    relay_session_id=row.session_id,
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
        relay_session_id=round_dir.name,
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

    ``wav_path`` is read OFF THE RECORD and never re-derived from the take id:
    the record is what the writer stamped beside the bytes it wrote, and a
    second rule for composing that name here could disagree with it.

    It is bundle-RELATIVE, and a path that leaves the bundle is refused rather
    than followed — this walks documents to decide what to hardlink.
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
    relay_session_id: str,
    setup_calibration_id: str | None,
) -> dict[str, Any]:
    """The take record, plus the two fields the ring's layout carried.

    The record travels WHOLE — ``diagnostic``, ``capture_integrity``,
    ``frame_ledger`` and the rest — because the readers' key union is wider
    than any subset this module could pick without going stale against them.
    """
    projected = dict(document)
    identity = SessionIdentity(session_id=session_id)
    try:
        identity = identity.with_alias(ALIAS_RELAY_SESSION_ID, relay_session_id)
    except SessionIdentityError:
        # A relay id that fails the identity charset is dropped rather than
        # taking the whole projection with it: the alias is an audit join,
        # where `session_id` is what the readers scope by.
        pass
    stamp_session_identity(projected, identity)
    if setup_calibration_id is not None:
        projected["setup_calibration_id"] = setup_calibration_id
    return projected
