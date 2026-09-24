# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured per-driver BASE TRIM — one writer, one reader.

The relative level a driver needs so the acoustic sum is level across every
declared crossover, replacing the datasheet-sensitivity estimate. One writer
(``baseline_apply.persist_applied_baseline_profile``), one reader
(:func:`measured_level_trims`); absent is normal. No estimator
and no solver live here. A trim is degenerate with the correction chain it was
co-fitted with, so the record names that chain and the declaration it was
measured against; a moved declaration is a loud refusal with a fallback, never
a migration, and there are no tolerant readers for older stored shapes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.atomic_io import atomic_write_json, fsync_directory
from jasper.json_fields import finite_float as _finite, utc_now_iso as _utc_now
from jasper.log_event import log_event
from jasper.paths import resolve_state_path

from . import passive_profile as _passive
from ._common import coerce_finite_float, require_sha256_hex
from .crossover_contract import measured_level_match_applied
from .crossover_preview import crossover_preview_fingerprint
from .level_trim import MAX_ATTENUATION_DB
from .profile import ActiveSpeakerPreset, required_driver_roles, snapshot_declares_single_branch

logger = logging.getLogger(__name__)

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

#: What the apply seam (``baseline_profile.bank_applied_base_trim``) did.
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
    return resolve_state_path(path, STATE_PATH_ENV, DEFAULT_STATE_PATH)


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
    fed the trim), not when this record was written, so a re-persist of a frozen
    candidate never re-dates old evidence. Minted as now only when the caller
    has no dated evidence at all.

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


#: ``corrections_source`` values an operator set by hand. Every other source
#: that is not ``measured`` fell back to weaker evidence (the datasheet, an
#: estimate, a preserved manual crossover).
_PINNED_GAIN_SOURCES = frozenset({"operator_pinned", "explicit"})


def measured_level_trims(
    preset: ActiveSpeakerPreset,
    crossover_preview: Mapping[str, Any] | None = None,
    *,
    design_draft: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """This box's banked per-driver level offsets for the declaration in hand.

    The one owner of *"what does this box's own evidence say the per-driver
    level offsets are?"*, which the crossover-v2 MEASUREMENT graph levels by.
    ``meta['source']`` names the evidence that answered, which is what a caller
    discloses beside the trims; empty trims mean none did (``meta['base_trim']``
    says why), and no caller may substitute an estimate for them.
    """
    roles = required_driver_roles(preset.way_count)
    declaration_fingerprint = (
        crossover_preview_fingerprint(crossover_preview, design_draft)
        if isinstance(crossover_preview, Mapping) and crossover_preview
        else None
    )
    base_trims, base_trim_meta = banked_base_trims(declaration_fingerprint, roles)
    if not base_trims:
        return {}, {"base_trim": base_trim_meta}
    banked_group_ids = base_trim_meta.get("speaker_group_ids") or []
    return base_trims, {
        "source": "banked_base_trim",
        "base_trim": base_trim_meta,
        "newest_capture_at": base_trim_meta.get("measured_at"),
        # The record's own trim source, not a second word for it: the
        # apply that banked it stamped WHICH evidence levelled the
        # graph, and this ledger repeats that rather than minting a
        # comparison of its own.
        "comparison": base_trim_meta.get("trim_source"),
        "groups_total": len(banked_group_ids),
        "groups_measured": len(banked_group_ids),
        "measured_group_ids": list(banked_group_ids),
        # Empty because the record banks an ALREADY-APPLIED level
        # match: the per-crossover evidence behind it lives with the
        # profile that was applied, which ``base_trim.trim_source``
        # names.
        "deltas": [],
        "incomparable_groups": [],
        "trims": dict(base_trims),
    }


def bank_applied_base_trim(candidate: Mapping[str, Any]) -> None:
    """Bank (or clear) the base trim the applied profile is actually playing.

    The one writer of this module's record. :func:`measured_level_trims` reads
    only the banked record, never a candidate's applied corrections, so banking
    the applied trim is what lets a ``--level-matched`` walk and the acoustic
    confirm see a measured level match.

    Three answers, not two:

    * every role sourced ``measured`` (beside ``level_match.applied``): bank.
    * some measured and every other role operator-pinned: leave the bank
      alone. A pin does not un-measure the speaker; the prior full measurement
      is still the best evidence.
    * anything else: clear. A role that fell back to the datasheet
      (``sensitivity``/``estimate``) or to a preserved manual crossover is
      weaker evidence, and a banked trim the box is not playing misleads the
      resolver.

    :data:`_PINNED_GAIN_SOURCES` draws the pin/fallback line, because
    ``level_match.applied`` alone only says some role was measured.

    Fail-soft, like
    :func:`~jasper.active_speaker.baseline_apply.promote_applied_baseline_candidate`:
    the graph is applied and read back by the time this runs, so a statefile
    that cannot be written never turns a successful apply into a failure.
    """
    def emit(result: str, reason: str, detail: str, *, level: int) -> None:
        log_event(
            logger,
            "dsp.baseline_base_trim_banked",
            level=level,
            result=result,
            reason=reason,
            detail=detail,
        )

    def refused(reason: str, detail: str) -> None:
        emit("failed", reason, detail, level=logging.WARNING)

    def left_standing(reason: str, detail: str) -> None:
        emit("left_standing", reason, detail, level=logging.INFO)

    def cleared(reason: str, detail: str) -> None:
        if clear_base_trim():
            # A successful clear is a state change an operator must be able to
            # see: without this, the only evidence that a bank was dropped was
            # the absence of the file.
            emit("cleared", reason, detail, level=logging.INFO)
            return
        # The clear could not HAPPEN (EACCES, a read-only /var/lib), which is
        # the opposite of nothing-to-clear: a banked trim survives an apply
        # that is not playing it, and a --level-matched walk would level its
        # graph by numbers nothing applies.
        refused(BANK_CLEAR_FAILED, "a banked trim survived an apply it does not match")

    # Grouping artifacts must not replace the solo trim record.
    snapshot = candidate.get("recomposition_snapshot")
    if isinstance(snapshot, Mapping) and snapshot.get("domain") == "driver":
        return

    # A base trim is a FRAME — one role's level relative to the others.
    if snapshot_declares_single_branch(snapshot):
        left_standing(REFUSE_NO_FRAME, "one driver declared, so no roles to level")
        return

    corrections = candidate.get("corrections")
    sources = candidate.get("corrections_source")
    level_match = candidate.get("level_match")
    if not isinstance(corrections, Mapping) or not isinstance(sources, Mapping):
        refused(
            BANK_CORRECTIONS_UNREADABLE,
            "the applied profile names no corrections",
        )
        return
    measured = (
        isinstance(level_match, Mapping)
        and level_match.get("applied") is True
        and bool(corrections)
        and all(sources.get(role) == "measured" for role in corrections)
    )
    if not measured:
        # The middle arm is narrower than "not every role measured":
        # `level_match.applied` only says some role was measured, so it cannot
        # tell an operator pin (the speaker stays measured) from a refused
        # measurement that fell back to the datasheet (the weaker evidence the
        # clear exists for).
        if (
            measured_level_match_applied(candidate)
            and all(str(sources.get(role) or "") in _PINNED_GAIN_SOURCES
                    for role in corrections if sources.get(role) != "measured")
        ):
            left_standing(
                BANK_PARTLY_MEASURED,
                "some roles are operator-pinned; the prior banked trim "
                "remains the best measurement of this speaker",
            )
            return
        cleared(
            BANK_UNMEASURED,
            "the applied profile is not level-matched by measurement",
        )
        return
    assert isinstance(level_match, Mapping)  # narrowed by `measured` above
    readiness = candidate.get("automatic_candidate")
    source = candidate.get("source")
    if (
        not isinstance(readiness, Mapping)
        or not readiness.get("measured_group_ids")
        or not isinstance(source, Mapping)
    ):
        # Leave the bank standing rather than clearing it. A frozen applied
        # profile persisted before `_frozen_applied_profile` carried
        # `automatic_candidate` reaches the restore leg in exactly this shape,
        # and it is a MEASURED profile whose readiness block simply was not
        # kept — not evidence that the speaker was never measured.
        left_standing(
            BANK_READINESS_UNREADABLE,
            "the applied profile names no readiness or source block",
        )
        return
    trims_db: dict[str, float] = {}
    for role, entry in corrections.items():
        gain = (
            coerce_finite_float(entry.get("gain_db"))
            if isinstance(entry, Mapping)
            else None
        )
        if gain is None:
            # A typed refusal, never an exception: a non-Mapping entry must not
            # escape this fail-soft seam and fail a successful apply.
            left_standing(
                BANK_CORRECTION_ENTRY_UNREADABLE,
                f"correction {str(role)!r} names no finite gain_db",
            )
            return
        trims_db[str(role)] = gain
    # The record's ``measured_at`` is the EVIDENCE time, never this persist's
    # wall clock: this seam re-runs on frozen candidates (the apply retry
    # before the idempotent early-return, any re-apply of an older candidate),
    # and stamping now would re-date old evidence. Candidates carry their own
    # recency in the ledger; a frozen candidate from before that field existed
    # inherits the standing record's time (never re-dated forward), and only a
    # box with no dated evidence and no record lets the writer mint now.
    evidence_at = str(level_match.get("newest_capture_at") or "") or None
    if evidence_at is None:
        existing = load_base_trim()
        if existing is not None:
            evidence_at = str(existing.get("measured_at") or "") or None
    try:
        record = write_base_trim(
            trims_db=trims_db,
            roles=sorted(trims_db),
            speaker_group_ids=readiness.get("measured_group_ids") or [],
            declaration_fingerprint=str(
                source.get("crossover_preview_fingerprint") or ""
            ),
            trim_source=str(level_match.get("comparison") or ""),
            # WHICH CHAIN this trim was co-fitted with (#3479). The resolving
            # candidate's own fingerprint, already on the profile's source
            # block — passed through rather than derived, because the frame
            # exists at fit time and no later reader can reconstruct it. A
            # profile that names none banks as "frame unknown" rather than as
            # the bare frame.
            chain_fingerprint=_passive.measured_candidate_fingerprint(source) or None,
            measured_at=evidence_at,
        )
    except (OSError, DriverBaseTrimError) as exc:
        refused(
            exc.reason if isinstance(exc, DriverBaseTrimError) else BANK_WRITE_FAILED,
            str(exc),
        )
        # A measured graph is now playing and could not be banked, so whatever
        # was banked before describes some OTHER apply. Absent beats wrong:
        # the resolver's empty answer is conservative, while a stale record
        # levels the graph by numbers nothing is playing.
        cleared(BANK_WRITE_REFUSED, "the measured trim could not be banked")
        return
    log_event(
        logger,
        "dsp.baseline_base_trim_banked",
        result="ok",
        trims=" ".join(
            f"{role}={value:.1f}"
            for role, value in sorted(record["trims_db"].items())
        ),
        trim_source=record["trim_source"],
        declaration=record["declaration_fingerprint"][:12],
    )
