# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The record seam filled: where a session's evidence lands.

:class:`~.session_seams.RecordStore`'s one method writes to the write-once
commissioning evidence bundle, on the paths the shipped ``V2FlowSeams``
publishers wrote, so ``position_cycle``, ``evidence_packet`` and
``candidate_bank`` find the same files. The id a record is banked under IS its
store-relative path (ADR-0198). Store errors propagate unwrapped: fail-soft
belongs in a named caller-side wrapper, never here and never a flag.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from jasper.attribution.findings import FINDING_SET_SCHEMA
from jasper.attribution.session_identity import (
    ALIAS_RELAY_SESSION_ID,
    SessionIdentity,
    stamp_session_identity,
)
from jasper.attribution.storage import findings_relative_path

from ..commissioning_evidence_store import CommissioningEvidenceStore
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

#: Also spelled as bare literals in ``jasper.web.correction_crossover_v2``'s
#: publisher bindings; this store is their writer.
CHECK_EVIDENCE_KIND = "jts_crossover_v2_check_evidence"
CLOUD_EVIDENCE_KIND = "jts_crossover_v2_cloud_evidence"

#: The keys the STORE owns on an enveloped record. A record that arrives
#: carrying one is refused rather than overwritten.
_SCHEMA_VERSION = 1
_ENVELOPE_KEYS = ("schema_version", "relay_session_id")


@dataclass(frozen=True)
class _Route:
    """Where one artifact kind lands, and what the store does around it.

    ``enveloped`` is per-kind because three of the six shipped payloads carry
    ``schema_version``/``kind``/``relay_session_id`` and three do not, and
    ``MeasuredCrossoverCandidate.from_mapping`` refuses any unknown key.
    ``stamp`` and ``verify`` are strictness the shipped publishers ran at the
    write, so they stay here.
    """

    relative_path: Callable[[str, Mapping[str, Any]], str]
    enveloped: bool
    stamp_identity: bool = False
    verify: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None


def _round_dir(relay_session_id: str) -> str:
    """The one directory every reader globs, spelled once."""
    return f"crossover_v2/{relay_session_id}"


def _verify_candidate(
    record: Mapping[str, Any], reopened: Mapping[str, Any],
) -> None:
    """The apply path's own tamper check: a candidate must survive exact reopen."""
    if MeasuredCrossoverCandidate.from_mapping(
        reopened
    ).fingerprint != record.get("fingerprint"):
        raise RuntimeError("published measured candidate changed on exact readback")


def _verify_receipt(
    record: Mapping[str, Any], reopened: Mapping[str, Any],
) -> None:
    """R21's accept-receipt pattern: a receipt is what it says it is."""
    if reopened != dict(record):
        raise RuntimeError("published round receipt changed on exact readback")


def _required(record: Mapping[str, Any], field: str) -> str:
    value = str(record.get(field) or "")
    if not value:
        raise ValueError(
            f"a banked {_discriminator(record)!r} record needs {field}"
        )
    return value


#: kind -> where it lands, for the six artifact kinds on this one seam.
_ROUTES: dict[str, _Route] = {
    # ``take_id`` is REQUIRED and never re-minted here: a geometry retake
    # reuses its position id, so two takes would collide on one path.
    POSITION_EVIDENCE_KIND: _Route(
        lambda relay, r: (
            f"{_round_dir(relay)}/positions/{_required(r, 'take_id')}.json"
        ),
        enveloped=True,
    ),
    CHECK_EVIDENCE_KIND: _Route(
        lambda relay, _r: f"{_round_dir(relay)}/check.json", enveloped=True,
    ),
    CLOUD_EVIDENCE_KIND: _Route(
        lambda relay, r: f"{_round_dir(relay)}/{_required(r, 'phase')}.json",
        enveloped=True,
        stamp_identity=True,
    ),
    CANDIDATE_KIND: _Route(
        lambda relay, _r: f"{_round_dir(relay)}/candidate.json",
        enveloped=False,
        verify=_verify_candidate,
    ),
    ROUND_RECEIPT_KIND: _Route(
        lambda relay, _r: f"{_round_dir(relay)}/round_receipt.json",
        enveloped=False,
        verify=_verify_receipt,
    ),
    # The CALLER injects ``phase``: ``FindingSet.to_dict()`` carries none and
    # ``from_mapping`` ignores top-level keys it does not know.
    FINDING_SET_SCHEMA: _Route(
        lambda relay, r: findings_relative_path(relay, _required(r, "phase")),
        enveloped=False,
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

    Keyed on ``relay_session_id`` and not the bundle id: every reader globs
    ``evidence/v1/artifacts/crossover_v2/{relay}/…`` and
    ``evidence_packet.round_artifact_dir`` reports that directory's name AS the
    relay id.
    """

    evidence: CommissioningEvidenceStore
    relay_session_id: str

    async def bank(self, record: Mapping[str, Any]) -> str:
        """Write one record; return the id that finds it again.

        Re-banking identical bytes is idempotent and returns the same id;
        different bytes at one path is a ``PATH_CONFLICT`` refusal, and it
        propagates.
        """
        measure, discriminator = _classify(record)
        route = self._route(discriminator)
        relative = route.relative_path(self.relay_session_id, record)
        payload = self._payload(record, route, discriminator, measure)
        await asyncio.to_thread(
            self._publish, relative, payload, record, route,
        )
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
        if not route.enveloped:
            return dict(record)
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
            "relay_session_id": self.relay_session_id,
            **payload,
        }
        if route.stamp_identity:
            payload = stamp_session_identity(payload, self._identity())
        return payload

    def _identity(self) -> SessionIdentity:
        """This session across two namespaces, as the cloud payload records it.

        The bundle id is canonical because the bundle is the retention unit;
        the relay id is minted after it and is not derivable from it.
        """
        return SessionIdentity(
            session_id=str(self.evidence.session_id),
            aliases={ALIAS_RELAY_SESSION_ID: str(self.relay_session_id)},
        )

    def _publish(
        self,
        relative: str,
        payload: Mapping[str, Any],
        record: Mapping[str, Any],
        route: _Route,
    ) -> None:
        artifact = self.evidence.publish_json_artifact(relative, payload)
        if route.verify is not None:
            route.verify(record, self.evidence.reopen_json_artifact(artifact))
