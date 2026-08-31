# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The record seam filled: where a session's evidence lands.

:class:`~.session_seams.RecordStore` declares one method, and it goes to the
write-once commissioning evidence bundle — a path that takes different bytes
twice is a ``PATH_CONFLICT`` refusal, which is exactly right for a capture.

**This is the writer that already exists, not a second one.** Every path here
is the one a ``V2FlowSeams`` publisher writes today, so the readers that glob
the bundle — ``position_cycle``, ``evidence_packet``, ``candidate_bank`` —
find the same files whichever writer produced them. The kind table below is
the whole map.

**The id IS the store-relative path.** A counter would need a second index to
rebuild the ordering; a path resolves itself against the bundle, which is what
lets a later reader fetch a record this store never hands back (ADR-0198: the
reading is the doors-and-banks tools', over the files). The engine never parses
one — it travels ``bank``'s return into ``record_ids`` untouched — so it stays
opaque in the sense the protocol means.

**Strict, on purpose.** ``CommissioningEvidenceStoreError`` and ``OSError``
propagate unwrapped, and every check the shipped publishers ran at the write —
the cloud payload's session-identity stamp, the reopen-and-compare a candidate
and a receipt owe — comes with them. **Fail-soft is the only thing the fold
leaves at the caller**, in a named wrapper (``correction_crossover_v2``'s
``bank_take`` binding is the shape), never here and never a flag, so a
publisher that must not fail quietly and one that deliberately fail-softs stay
visibly different in the code that calls them.

**Every banked record carries its own ``take_id``.** The store requires it and
never re-derives one: a geometry retake reuses its position id, so the position
id alone does not name a take, and a store that guessed would collide two takes
on one write-once path.
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

#: The two artifact discriminators that exist only as bare literals in
#: ``jasper.web.correction_crossover_v2``'s publisher bindings. Named here
#: because this store is their new writer; the bindings keep their literals
#: until the cutover deletes them.
CHECK_EVIDENCE_KIND = "jts_crossover_v2_check_evidence"
CLOUD_EVIDENCE_KIND = "jts_crossover_v2_cloud_evidence"

#: The keys the STORE owns on an enveloped record, written by :meth:`bank`. A
#: record that arrives already carrying one is refused rather than overwritten,
#: so a reader can tell the store's keys from its author's.
_SCHEMA_VERSION = 1
_ENVELOPE_KEYS = ("schema_version", "relay_session_id")


@dataclass(frozen=True)
class _Route:
    """Where one artifact kind lands, and what the store does around it.

    ``enveloped`` is not a style choice: three of the six shipped payloads
    carry ``schema_version``/``kind``/``relay_session_id`` and three do not,
    and ``MeasuredCrossoverCandidate.from_mapping`` refuses a candidate with
    any unknown key. Adding an envelope where the shipped writer adds none
    would make the file unreadable by its own reader.

    ``stamp`` and ``verify`` carry the two things the shipped publishers do
    besides writing: the cloud payload's session-identity stamp, and the
    reopen-and-compare that a candidate and a receipt owe. Both are
    STRICTNESS, so they belong here — the only thing the fold leaves at the
    caller is fail-soft.
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
    """The apply path's own tamper check, run at the write.

    A candidate that cannot survive exact reopen never becomes reviewable —
    ``bind_evidence_publishers``' contract, kept rather than dropped by the
    fold.
    """
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


#: kind -> where it lands. The six artifact kinds the five ``V2FlowSeams``
#: publishers wrote, folded onto one seam (the 2026-08-26 FOLD ruling).
_ROUTES: dict[str, _Route] = {
    # ``take_id`` is REQUIRED and never re-minted here. Every producer of a
    # banked record mints it — through ``spatial.take_id_for`` where a
    # prompted position exists — because a geometry retake reuses its position
    # id, so the position id alone does not name a take and two takes would
    # collide on one write-once path.
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
    # The CALLER injects ``phase`` into the record before banking.
    # ``FindingSet.to_dict()`` carries no phase — the shipped
    # ``publish_finding_set`` takes it as a separate argument — and ``bank``
    # has one parameter, so the shipped call shape moves into the record.
    # ``FindingSet.from_mapping`` ignores top-level keys it does not know, so
    # the extra key costs its reader nothing.
    FINDING_SET_SCHEMA: _Route(
        lambda relay, r: findings_relative_path(relay, _required(r, "phase")),
        enveloped=False,
    ),
}


def _measure_kind(record: Mapping[str, Any]) -> str | None:
    """This record's MEASUREMENT kind under either spelling, or ``None``.

    ``None`` means *"not a capture record"* — it is NOT the same as ``""``.
    ``spatial.take_kind`` returns ``""`` for a take whose graph names neither
    fingerprint, and says so in as many words: *"unresolvable is `""`, never a
    guess"*, the same honest-empty ``baseline_record_id`` uses. So the KEY's
    presence decides that this is a position take, and its value is carried
    through whatever it says — a truthiness test here would file an
    unresolved-kind take as unroutable and refuse a record the builders are
    contracted to produce.
    """
    kind = record.get("kind")
    if isinstance(kind, str) and kind in MEASURE_KINDS:
        return kind
    if MEASURE_KIND_KEY in record:
        return str(record.get(MEASURE_KIND_KEY) or "")
    return None


def _classify(record: Mapping[str, Any]) -> tuple[str | None, str]:
    """This record's measurement kind, and the artifact kind that routes it.

    Answered together and ONCE per :meth:`BankedRecordStore.bank`: the route,
    the envelope's ``measure_kind`` and the file's own ``kind`` are three
    readings of one classification, and a reader that re-derives it is a
    second place for it to answer differently.

    The artifact kind is the record's own type tag under whichever of the
    three spellings it takes: ``kind`` is the engine's and the shipped
    payloads', ``schema`` is a finding set's, and a capture record's is the
    store's to supply, because a capture names its MEASUREMENT kind and leaves
    the artifact kind here.
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

    ``relay_session_id`` and not the bundle id: the bundle id is canonical
    identity, but every reader globs
    ``evidence/v1/artifacts/crossover_v2/{relay}/…`` and
    ``evidence_packet.round_artifact_dir`` reports that directory's name AS the
    relay id. A store minting its directory from ``record["session_id"]`` files
    the record where nothing looks for it.
    """

    evidence: CommissioningEvidenceStore
    relay_session_id: str

    async def bank(self, record: Mapping[str, Any]) -> str:
        """Write one record; return the id that finds it again.

        Re-banking identical bytes is idempotent and returns the same id —
        ``_write_once``'s own contract. Different bytes at one path is a
        ``PATH_CONFLICT`` refusal, and it propagates.

        A candidate and a receipt are re-opened and compared before this
        returns, because both shipped publishers did: a candidate that cannot
        survive exact reopen must never become reviewable, and a receipt that
        changed on readback is not the receipt anything cited.
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
        the relay id is minted after it and is not derivable from it, so it
        rides as an alias.
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
