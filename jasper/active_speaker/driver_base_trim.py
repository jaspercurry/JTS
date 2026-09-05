# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured per-driver BASE TRIM — one writer, one reader.

The relative level a driver needs so the acoustic sum is level across every
declared crossover, replacing the datasheet-sensitivity estimate. One writer
(``baseline_profile.persist_applied_baseline_profile``), one reader
(``baseline_profile._measured_level_trims``); absent is normal. No estimator
and no solver live here. A trim is degenerate with the correction chain it was
co-fitted with, so the record names that chain and the declaration it was
measured against; a moved declaration is a loud refusal with a fallback, never
a migration, and there are no tolerant readers for older stored shapes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.atomic_io import atomic_write_json, fsync_directory
from jasper.json_fields import finite_float as _finite

from ._common import require_sha256_hex
from .level_trim import MAX_ATTENUATION_DB

SCHEMA_VERSION = 1
BASE_TRIM_KIND = "jts_active_speaker_driver_base_trim"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_driver_base_trim.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_DRIVER_BASE_TRIM_STATE"

#: What the reader did with the banked record, for the level-match ledger.
STATUS_ABSENT = "absent"
STATUS_APPLIED = "applied"
STATUS_DECLARATION_CHANGED = "declaration_changed"
STATUS_ROLES_CHANGED = "roles_changed"
STATUS_UNUSABLE = "unusable"
#: The record validated, but guided captures newer than its ``measured_at``
#: answered instead (ruling S20: the newest measurement wins). Stamped by the
#: resolver, never by :func:`banked_base_trims`. Not in
#: :data:`REFUSED_STATUSES`: the re-measure a refusal would demand already
#: happened.
STATUS_SUPERSEDED = "superseded"

#: A trim was banked and this speaker is NOT using it. ``absent`` is not one of
#: them: a box that never measured is the ordinary case, while every member here
#: is a measurement being discarded and must be said out loud.
REFUSED_STATUSES = frozenset({
    STATUS_DECLARATION_CHANGED,
    STATUS_ROLES_CHANGED,
    STATUS_UNUSABLE,
})

#: The single remediation string, in every surface that refuses a banked trim.
REMEASURE_REMEDIATION = (
    "measure and apply this speaker's crossover again to re-bank the trim"
)


#: Why the writer refused. Closed vocabulary: the apply seam logs the reason.
REFUSE_NO_DECLARATION = "base_trim_no_declaration"
REFUSE_NO_TRIM_SOURCE = "base_trim_no_trim_source"
REFUSE_NO_SPEAKER_GROUP = "base_trim_no_speaker_group"
REFUSE_ROLES_INCOMPLETE = "base_trim_roles_incomplete"
REFUSE_NOT_ATTENUATION = "base_trim_not_attenuation"
#: Fewer than two roles to level against each other. A base trim is a FRAME, so
#: a way-1 speaker's only bankable value is the vacuous ``{"full_range": 0.0}``.
REFUSE_NO_FRAME = "base_trim_no_frame"

#: What the apply seam (``baseline_profile._bank_applied_base_trim``) did.
#: Separate from ``REFUSE_*`` above: those name the writer envelope this module
#: enforces, these the seam's own reading of the applied profile.
BANK_CORRECTIONS_UNREADABLE = "corrections_unreadable"
BANK_READINESS_UNREADABLE = "readiness_unreadable"
BANK_CORRECTION_ENTRY_UNREADABLE = "correction_entry_unreadable"
BANK_CLEAR_FAILED = "clear_failed"
BANK_PARTLY_MEASURED = "partly_measured"
BANK_UNMEASURED = "unmeasured"
BANK_WRITE_REFUSED = "write_refused"
BANK_WRITE_FAILED = "write_failed"


class DriverBaseTrimError(ValueError):
    """A base-trim record was asked to be written outside its own envelope."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def base_trim_state_path(path: str | Path | None = None) -> Path:
    """Where the base trim lives: an explicit path, the env override, or the
    default. One resolver, so every surface probes the file the reader reads."""
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _chain_fingerprint(value: Any) -> str | None:
    """The banked chain name, or ``None`` when nothing readable names one.

    Anything that is not a fingerprint reads as absent rather than as a chain:
    only a named frame licenses a comparison.
    """
    try:
        return require_sha256_hex(value, "chain_fingerprint", ValueError)
    except ValueError:
        return None


def _group_ids(value: Any) -> list[str]:
    """The record's speaker groups, or ``[]`` when it names none readably.

    Fail-closed on any unreadable member:
    ``crossover_contract.automatic_candidate_readiness`` gates on that set.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return []
    if not all(isinstance(item, str) and item for item in value):
        return []
    return sorted(set(value))


def load_base_trim(*, state_path: str | Path | None = None) -> dict[str, Any] | None:
    """The persisted record, or ``None`` when there is none to read.

    Never raises: an unreadable, malformed, wrong-kind or wrong-schema file is
    indistinguishable from no file, because the consumer's fallback is the
    conservative answer in every one of those cases.
    """
    path = base_trim_state_path(state_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("kind") != BASE_TRIM_KIND
        or raw.get("artifact_schema_version") != SCHEMA_VERSION
    ):
        return None
    return raw


def banked_base_trims(
    declaration_fingerprint: str | None,
    roles: Sequence[str],
    *,
    state_path: str | Path | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """The banked trim for THIS declaration, or ``({}, why-not)``.

    Every rejection is reported rather than swallowed: a silently ignored trim
    looks exactly like a speaker that was never measured. A caller that hands
    over NO declaration gets :data:`STATUS_DECLARATION_CHANGED` too — a record
    that cannot be keyed to the declaration in hand is not evidence.

    The trims are re-validated on the way out against the writer's envelope
    (finite, attenuation-only, at or above
    :data:`~jasper.active_speaker.level_trim.MAX_ATTENUATION_DB`). That envelope
    is relative to UNITY and that is the whole of its guarantee; it says nothing
    about the datasheet estimate the record replaces.
    """
    ordered = tuple(roles)
    record = load_base_trim(state_path=state_path)
    if record is None:
        return {}, {"status": STATUS_ABSENT}
    banked_fingerprint = record.get("declaration_fingerprint")
    meta: dict[str, Any] = {
        "measured_at": record.get("measured_at"),
        "declaration_fingerprint": banked_fingerprint,
        # WHICH CHAIN this scalar was resolved WITH (#3479). Read back, never
        # re-derived: the frame exists at fit time and nowhere else. ``None``
        # means unknown, which refuses a comparison rather than licensing one.
        "chain_fingerprint": _chain_fingerprint(record.get("chain_fingerprint")),
        "state_path": str(base_trim_state_path(state_path)),
    }
    if (
        not isinstance(declaration_fingerprint, str)
        or not declaration_fingerprint
        or banked_fingerprint != declaration_fingerprint
    ):
        return {}, {
            **meta,
            "status": STATUS_DECLARATION_CHANGED,
            "expected_declaration_fingerprint": declaration_fingerprint,
            "remediation": REMEASURE_REMEDIATION,
        }
    raw_trims = record.get("trims_db")
    if not isinstance(raw_trims, Mapping) or set(raw_trims) != set(ordered):
        return {}, {
            **meta,
            "status": STATUS_ROLES_CHANGED,
            "roles": sorted(ordered),
            "remediation": REMEASURE_REMEDIATION,
        }
    trims: dict[str, float] = {}
    for role in ordered:
        value = _finite(raw_trims.get(role))
        if value is None or value > 0.0 or value < MAX_ATTENUATION_DB:
            return {}, {
                **meta,
                "status": STATUS_UNUSABLE,
                "detail": f"{role} trim is outside the attenuation-only envelope",
                "remediation": REMEASURE_REMEDIATION,
            }
        trims[role] = value
    # WHICH speaker groups this trim covers, not merely how many:
    # ``crossover_contract.automatic_candidate_readiness`` gates on the SET.
    measured = _group_ids(record.get("speaker_group_ids"))
    trim_source = str(record.get("trim_source") or "")
    if not measured or not trim_source:
        return {}, {
            **meta,
            "status": STATUS_UNUSABLE,
            "detail": "the record names no speaker group or no trim source",
            "remediation": REMEASURE_REMEDIATION,
        }
    return trims, {
        **meta,
        "status": STATUS_APPLIED,
        "trims": dict(trims),
        "speaker_group_ids": measured,
        # WHICH evidence the applied profile levelled by, carried into the
        # profile's ``level_match`` ledger so a receipt reading the banked trim
        # still names the measurement behind it (ruling S16 (d)).
        "trim_source": trim_source,
    }


def write_base_trim(
    *,
    trims_db: Mapping[str, float],
    roles: Sequence[str],
    speaker_group_ids: Sequence[str],
    declaration_fingerprint: str,
    trim_source: str,
    chain_fingerprint: Any = None,
    measured_at: str | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Publish one measured base trim. Called ONLY from the apply seam.

    Raises :class:`DriverBaseTrimError` when the record would not survive
    :func:`banked_base_trims` — writing a value the reader rejects is a silent
    no-op dressed up as success.

    ``chain_fingerprint`` is WHICH CORRECTION CHAIN the trim was co-fitted
    against (#3479). A trim is DEGENERATE with that chain — a flat trim plus a
    shelf is the same branch-gain profile as a deeper flat trim and no shelf, so
    the same physics reads as two estimates disagreeing by ~11 dB unless the
    chain tells them apart. ``None`` (no resolving candidate, or a value this
    module cannot read) is banked as no frame, which refuses a comparison.

    ``measured_at`` is WHEN THE EVIDENCE WAS MEASURED (the newest capture that
    fed the trim), not when this record was written: the S20 supersede compares
    capture times against it, so a write time would let a re-persist of a frozen
    candidate re-date old evidence past newer captures. Minted as now only when
    the caller has no dated evidence at all.

    Attenuation-only by construction and REFUSED rather than clamped: no path
    reaching this writer can legitimately produce a positive per-role trim, so a
    positive value is a fault, earns :data:`REFUSE_NOT_ATTENUATION`, and nothing
    is written. Clamping would bank a trim no measurement produced and couple
    the reverse-null door's depth ceiling to the per-role caps argument.
    """
    ordered = tuple(roles)
    if not isinstance(declaration_fingerprint, str) or not declaration_fingerprint:
        raise DriverBaseTrimError(
            REFUSE_NO_DECLARATION, "a base trim must name the declaration it measured"
        )
    # The reader keys on this string by EQUALITY, so a non-fingerprint value
    # banks a record nothing can read back. Re-raised rather than passed as
    # ``exc_type`` so the error carries the (reason, detail) pair the apply
    # seam logs.
    try:
        require_sha256_hex(
            declaration_fingerprint, "declaration_fingerprint", ValueError
        )
    except ValueError as exc:
        raise DriverBaseTrimError(REFUSE_NO_DECLARATION, str(exc)) from exc
    if not isinstance(trim_source, str) or not trim_source:
        raise DriverBaseTrimError(
            REFUSE_NO_TRIM_SOURCE, "a base trim must name the evidence behind it"
        )
    groups = _group_ids(speaker_group_ids)
    if not groups:
        raise DriverBaseTrimError(
            REFUSE_NO_SPEAKER_GROUP,
            "a base trim must name the speaker groups it covers",
        )
    # Asked before coverage: a way-1 speaker's trims DO cover its declared
    # roles, so a coverage complaint would name the wrong fact.
    if len(set(ordered)) < 2:
        raise DriverBaseTrimError(
            REFUSE_NO_FRAME,
            f"a base trim levels roles against each other; {sorted(set(ordered))!r} "
            "is not a frame",
        )
    if set(trims_db) != set(ordered):
        raise DriverBaseTrimError(
            REFUSE_ROLES_INCOMPLETE,
            f"trims cover {sorted(trims_db)!r}, not the declared roles "
            f"{sorted(ordered)!r}",
        )
    trims: dict[str, float] = {}
    for role in ordered:
        value = _finite(trims_db.get(role))
        if value is None or value > 0.0 or value < MAX_ATTENUATION_DB:
            raise DriverBaseTrimError(
                REFUSE_NOT_ATTENUATION,
                f"{role} trim {trims_db.get(role)!r} dB is outside the "
                f"[{MAX_ATTENUATION_DB:g}, 0.0] dB attenuation-only envelope",
            )
        # FULL precision: the graph plays the unrounded number, so banking a
        # rounded one banks a trim nothing applies.
        trims[role] = value
    path = base_trim_state_path(state_path)
    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BASE_TRIM_KIND,
        "measured_at": measured_at or _utc_now(),
        "state_path": str(path),
        "declaration_fingerprint": declaration_fingerprint,
        "roles": list(ordered),
        "trims_db": trims,
        "speaker_group_ids": groups,
        "trim_source": trim_source,
        "chain_fingerprint": _chain_fingerprint(chain_fingerprint),
    }
    # Durable because this record and the profile written by the same seam are
    # two halves of one apply: a power cut that keeps one but not the other
    # leaves the box levelling by numbers its graph is not playing.
    atomic_write_json(
        path, payload, mode=0o640, durable=True
    )
    return payload


def clear_base_trim(*, state_path: str | Path | None = None) -> bool:
    """Drop the banked record. ``True`` when the box is left carrying none.

    A profile applied WITHOUT a measured level match is not levelled by the
    banked trim, so leaving the record would let a ``--level-matched`` walk
    level its graph by numbers the box is not playing (the S12 lie).

    Nothing to drop is SUCCESS; a drop that could not HAPPEN is not — a record
    surviving the clear (EACCES, a read-only ``/var/lib``) is exactly the stale
    record the clear exists to prevent. Fail-soft either way: the graph is
    already applied, so the caller LOGS the ``False`` rather than failing on it.
    """
    path = base_trim_state_path(state_path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    # The unlink is metadata, so without this fsync the record can come back
    # after a dirty shutdown. NOT part of the verdict: by here the record is
    # gone from the live filesystem and only durability is at stake, so a
    # failure must not be reported as a banked trim surviving the clear.
    try:
        fsync_directory(path.parent)
    except OSError:
        pass
    return True
