# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Route raw records and the live run manifest into a commissioning bundle.

Raw evidence is write-once; the manifest is an atomic snapshot. Record ids are
store-relative paths (ADR-0198). Store errors propagate to the host.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from jasper.active_speaker.restore_wait import resilient_restore
from jasper.audio_measurement.bundles import record_artifact

from jasper.attribution.findings import FINDING_SET_SCHEMA
from jasper.attribution.session_identity import (
    ALIAS_CAPTURE_SESSION_ID,
    SessionIdentity,
    stamp_session_identity,
)
from jasper.attribution.storage import findings_relative_path

from ..commissioning_evidence_store import CommissioningEvidenceStore
from ..run_manifest import RUN_MANIFEST_KIND, RUN_MANIFEST_FILENAME
from ..measured_crossover_candidate import CANDIDATE_KIND, MeasuredCrossoverCandidate
from .contracts import (
    MEASURE_KIND_KEY,
    MEASURE_KINDS,
    POSITION_EVIDENCE_KIND,
    ROUND_RECEIPT_KIND,
)

__all__ = [
    "CHECK_EVIDENCE_KIND",
    "CLOUD_EVIDENCE_KIND",
    "BankedRecordStore",
]

#: The two artifact kinds no producer names for itself: a check bundle and a
#: cloud group are plain dicts, so their discriminator is spelled here.
CHECK_EVIDENCE_KIND = "jts_crossover_v2_check_evidence"
CLOUD_EVIDENCE_KIND = "jts_crossover_v2_cloud_evidence"

#: The keys the STORE owns on an enveloped record. A record that arrives
#: carrying one is refused rather than overwritten.
_SCHEMA_VERSION = 1
_ENVELOPE_KEYS = ("schema_version", "capture_session_id")


@dataclass(frozen=True)
class _Route:
    """Path, envelope and publication policy for one artifact kind."""

    relative_path: Callable[[str, Mapping[str, Any]], str]
    enveloped: bool
    live: bool = False
    #: Keys a caller supplies to ROUTE the record and that the file does not
    #: carry, taken back off the way ``kind`` is.
    routing_keys: tuple[str, ...] = ()
    stamp_identity: bool = False
    verify: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None


def _round_dir(capture_session_id: str) -> str:
    """The one directory every reader globs, spelled once."""
    return f"crossover_v2/{capture_session_id}"


def _verify_candidate(
    written: Mapping[str, Any], reopened: Mapping[str, Any],
) -> None:
    """The apply path's own tamper check: a candidate must survive exact reopen."""
    if MeasuredCrossoverCandidate.from_mapping(
        reopened
    ).fingerprint != written.get("fingerprint"):
        raise RuntimeError("published measured candidate changed on exact readback")


def _verify_receipt(
    written: Mapping[str, Any], reopened: Mapping[str, Any],
) -> None:
    """R21's accept-receipt pattern: a receipt is what it says it is."""
    if reopened != dict(written):
        raise RuntimeError("published round receipt changed on exact readback")


def _required(record: Mapping[str, Any], field: str) -> str:
    value = str(record.get(field) or "")
    if not value:
        raise ValueError(
            f"a banked {_discriminator(record)!r} record needs {field}"
        )
    return value


_ROUTES: dict[str, _Route] = {
    RUN_MANIFEST_KIND: _Route(
        lambda capture, _r: f"{_round_dir(capture)}/{RUN_MANIFEST_FILENAME}",
        enveloped=False, live=True,
    ),
    # ``take_id`` is REQUIRED and never re-minted here: a geometry retake
    # reuses its position id, so two takes would collide on one path.
    POSITION_EVIDENCE_KIND: _Route(
        lambda capture, r: (
            f"{_round_dir(capture)}/positions/{_required(r, 'take_id')}.json"
        ),
        enveloped=True,
    ),
    CHECK_EVIDENCE_KIND: _Route(
        lambda capture, _r: f"{_round_dir(capture)}/check.json", enveloped=True,
    ),
    CLOUD_EVIDENCE_KIND: _Route(
        lambda capture, r: f"{_round_dir(capture)}/{_required(r, 'phase')}.json",
        enveloped=True,
        stamp_identity=True,
    ),
    CANDIDATE_KIND: _Route(
        lambda capture, _r: f"{_round_dir(capture)}/candidate.json",
        enveloped=False,
        verify=_verify_candidate,
    ),
    ROUND_RECEIPT_KIND: _Route(
        lambda capture, _r: f"{_round_dir(capture)}/round_receipt.json",
        enveloped=False,
        verify=_verify_receipt,
    ),
    # The CALLER injects ``phase`` to route on; the file is exactly
    # ``FindingSet.to_dict()``, which carries none.
    FINDING_SET_SCHEMA: _Route(
        lambda capture, r: findings_relative_path(capture, _required(r, "phase")),
        enveloped=False,
        routing_keys=("phase",),
    ),
}


def _measure_kind(record: Mapping[str, Any]) -> str | None:
    """This record's MEASUREMENT kind under either spelling, or ``None``.

    ``None`` means *not a capture record* and is NOT ``""``:
    ``spatial.take_kind`` returns ``""`` for a take whose graph resolves
    neither fingerprint, so the KEY's presence decides, not its truthiness.
    """
    kind = record.get("kind")
    if isinstance(kind, str) and kind in MEASURE_KINDS:
        return kind
    if MEASURE_KIND_KEY in record:
        return str(record.get(MEASURE_KIND_KEY) or "")
    return None


def _classify(record: Mapping[str, Any]) -> tuple[str | None, str]:
    """This record's measurement kind, and the artifact kind that routes it.

    Answered together and ONCE per :meth:`BankedRecordStore.bank`, since the
    route, the envelope's ``measure_kind`` and the file's ``kind`` are three
    readings of one classification. A capture names its MEASUREMENT kind only;
    the artifact kind is the store's to supply.
    """
    measure = _measure_kind(record)
    if measure is not None:
        return measure, POSITION_EVIDENCE_KIND
    return None, str(record.get("kind") or record.get("schema") or "")


def _discriminator(record: Mapping[str, Any]) -> str:
    """Which artifact kind this record IS — the key its route is filed under."""
    return _classify(record)[1]


@dataclass(frozen=True)
class BankedRecordStore:
    """:class:`~.session_seams.RecordStore` over the evidence bundle.

    Keyed on ``capture_session_id`` and not the bundle id: every reader globs
    ``evidence/v1/artifacts/crossover_v2/{capture}/…`` and
    ``evidence_packet.round_artifact_dir`` reports that directory's name AS the
    capture id.
    """

    evidence: CommissioningEvidenceStore
    capture_session_id: str

    async def bank(self, record: Mapping[str, Any]) -> str:
        """Publish a raw record once, or atomically update the run manifest."""
        measure, discriminator = _classify(record)
        route = self._route(discriminator)
        relative = route.relative_path(self.capture_session_id, record)
        payload = self._payload(record, route, discriminator, measure)
        await resilient_restore(asyncio.to_thread(self._publish, relative, payload, route))
        return relative

    # --------------------------------------------------------------- internals

    def _route(self, discriminator: str) -> _Route:
        route = _ROUTES.get(discriminator)
        if route is None:
            raise ValueError(
                f"no banked artifact kind for record {discriminator!r}"
            )
        return route

    def _payload(
        self,
        record: Mapping[str, Any],
        route: _Route,
        discriminator: str,
        measure: str | None,
    ) -> Mapping[str, Any]:
        record = {k: v for k, v in record.items() if k not in route.routing_keys}
        if not route.enveloped:
            return record
        owned = [key for key in _ENVELOPE_KEYS if key in record]
        if owned:
            raise ValueError(
                f"a banked {discriminator!r} record must not carry "
                f"{owned} — the store writes those and takes them back off"
            )
        payload = {
            key: value for key, value in record.items()
            if key not in ("kind", MEASURE_KIND_KEY)
        }
        if measure is not None:
            payload[MEASURE_KIND_KEY] = measure
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "kind": discriminator,
            "capture_session_id": self.capture_session_id,
            **payload,
        }
        if route.stamp_identity:
            payload = stamp_session_identity(payload, self._identity())
        return payload

    def _identity(self) -> SessionIdentity:
        """This session across two namespaces, as the cloud payload records it.

        The bundle id is canonical because the bundle is the retention unit;
        the capture id is minted after it and is not derivable from it.
        """
        return SessionIdentity(
            session_id=str(self.evidence.session_id),
            aliases={ALIAS_CAPTURE_SESSION_ID: str(self.capture_session_id)},
        )

    def _publish(
        self, relative: str, payload: Mapping[str, Any], route: _Route,
    ) -> None:
        if route.live:
            self.evidence.write_live(relative, payload)
            return
        artifact = self.evidence.publish_json_artifact(relative, payload)
        if _measure_kind(payload) is not None and payload.get("wav_path"):
            record_artifact(
                self.evidence.bundle_dir, artifact.relative_path,
                kind=POSITION_EVIDENCE_KIND, sensitivity="derived",
                recomputable=False, generated_by=__name__,
                dependencies=(str(payload["wav_path"]),),
            )
        if route.verify is not None:
            # The PAYLOAD, not the record it came from: a route's verify asks
            # whether what was written comes back, and the two differ wherever
            # the store owns keys the caller supplied.
            route.verify(payload, self.evidence.reopen_json_artifact(artifact))
