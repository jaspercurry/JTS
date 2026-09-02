# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured per-driver BASE TRIM — one writer, one reader.

The relative level a driver needs so the acoustic sum is level across every
declared crossover. Until this artifact exists a speaker ships the trim
``baseline_profile._derive_corrections`` derives from the drivers' DECLARED
sensitivities, which is a datasheet claim about some other cabinet: on
2026-08-23 a DE250 rated on B&C's ME45 horn but installed on an R-OSSE
waveguide seeded a −10.8 dB tweeter trim nobody had measured. This module is
where that estimate becomes an observation.

Ownership, deliberately narrow:

* **one writer** — the apply seam,
  ``baseline_profile.persist_applied_baseline_profile``, which banks the trim a
  successfully applied profile is actually playing (and clears the record when
  the applied profile is not level-matched by measurement);
* **one reader** — ``baseline_profile._measured_level_trims``, which prefers a
  banked base trim over the guided-capture derivation — unless the captures
  are newer than the record (ruling S20) — and falls back to it;
* **absent is normal.** A speaker that has never applied a measured level match
  behaves exactly as it did before this module existed — the guided captures,
  then the datasheet estimate.

**No estimator lives here, and no solver either.** The trims are whatever the
applied profile resolved — the crossover-v2 measured candidate's own
``role_attenuations_db``, or the guided captures chained through
``level_trim.attenuation_from_group_deltas`` — and this module only banks that
answer under the declaration it was measured against. Minting another way to
measure an inter-driver level gap is the defect
``intervention.compare_level_definitions`` and
``baseline_profile._compare_level_sittings`` exist to disclose, and it is not
reopened here.

**The scalar is not portable without its frame.** A trim is degenerate with
the correction chain it was co-fitted with, so the record also names that chain
(``chain_fingerprint``) and every reader gets it back beside the number — see
:func:`write_base_trim`.

**Re-keying is a loud refusal, never a migration.** The record names the
declaration it was measured against (the crossover preview's own fingerprint).
A speaker whose declaration has moved — a different Fc, a different driver, a
re-save at ``/sound`` — has a banked trim that answers a question nobody is
asking any more, so the reader refuses it with one remediation and falls back.
Under the owner's 2026-08-23 no-legacy-config ruling this file grows no
tolerant readers for older stored shapes: a breaking change to the payload
refuses the same way, and re-measuring is the accepted cost.
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
#: Stamped by the resolver (``baseline_profile._measured_level_trims``), never
#: by :func:`banked_base_trims` — this reader cannot see the guided captures.
#: The record validated, but guided captures newer than its ``measured_at``
#: answered instead (ruling S20: the newest measurement wins). Deliberately not
#: in :data:`REFUSED_STATUSES`: a refusal demands a re-measure, and here the
#: re-measure is what already happened.
STATUS_SUPERSEDED = "superseded"

#: The statuses that mean "a trim was banked and this speaker is NOT using it".
#: ``absent`` is not one of them — a box that never measured is the ordinary
#: case — but every member here is a measurement being discarded, which the
#: profile must say out loud rather than leaving indistinguishable from never
#: having measured at all.
REFUSED_STATUSES = frozenset({
    STATUS_DECLARATION_CHANGED,
    STATUS_ROLES_CHANGED,
    STATUS_UNUSABLE,
})

#: The single remediation string. One sentence, one verb, in every surface that
#: refuses a banked trim, so an operator never has to reconcile two wordings.
REMEASURE_REMEDIATION = (
    "measure and apply this speaker's crossover again to re-bank the trim"
)


#: Why the writer refused. A closed vocabulary rather than prose, because the
#: apply seam logs the reason and a test pins it.
REFUSE_NO_DECLARATION = "base_trim_no_declaration"
REFUSE_NO_TRIM_SOURCE = "base_trim_no_trim_source"
REFUSE_NO_SPEAKER_GROUP = "base_trim_no_speaker_group"
REFUSE_ROLES_INCOMPLETE = "base_trim_roles_incomplete"
REFUSE_NOT_ATTENUATION = "base_trim_not_attenuation"
#: Fewer than two roles to level against each other. A base trim is a FRAME —
#: one role's level relative to the others — so on a ``full_range_passive``
#: (way-1) speaker there is nothing to be relative to, and the only value the
#: writer could bank is the vacuous ``{"full_range": 0.0}``. Banking that would
#: make an unlevelled speaker indistinguishable from a levelled one on every
#: surface that reads this record.
REFUSE_NO_FRAME = "base_trim_no_frame"

#: What the APPLY SEAM (``baseline_profile._bank_applied_base_trim``) did and
#: why. A second closed vocabulary, deliberately separate from ``REFUSE_*``
#: above: those name a writer envelope this module enforces, these name the
#: seam's own reading of the applied profile. Both live here so one file holds
#: every word an operator can see about this artifact.
#:
#: The ONE crossing is deliberate: a way-1 apply is left standing under
#: :data:`REFUSE_NO_FRAME` itself, because there the seam and the writer are
#: naming the identical fact about the same speaker, and minting a second slug
#: for it would be the one-slug-per-fact rule inverted.
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

    A frame nobody can name is not a frame, so anything that is not a
    fingerprint reads as absent rather than as a chain: the two are opposite
    claims, and only the second one licenses a comparison.
    """
    try:
        return require_sha256_hex(value, "chain_fingerprint", ValueError)
    except ValueError:
        return None


def _group_ids(value: Any) -> list[str]:
    """The record's speaker groups, or ``[]`` when it names none readably.

    Fail-closed on any unreadable member: a record whose group list is partly
    garbage would otherwise claim to have levelled a set it cannot name, and
    ``crossover_contract.automatic_candidate_readiness`` gates on that set.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return []
    if not all(isinstance(item, str) and item for item in value):
        return []
    return sorted(set(value))


def load_base_trim(*, state_path: str | Path | None = None) -> dict[str, Any] | None:
    """The persisted record, or ``None`` when there is none to read.

    Absent-tolerant and never raises: an unreadable, malformed, wrong-kind, or
    wrong-schema file is indistinguishable from no file at all, because the
    consumer's fallback — the guided captures, then the datasheet estimate — is
    the conservative answer in every one of those cases.
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

    Every rejection is reported rather than swallowed: a banked trim that is
    silently ignored looks exactly like a speaker that was never measured, and
    the operator would have no way to tell the two apart.

    A caller that hands over NO declaration gets
    :data:`STATUS_DECLARATION_CHANGED` too, sharing the status because it earns
    the same answer: a record that cannot be keyed to the declaration in front
    of us is not evidence about this speaker.

    The trims are re-validated against the writer's own envelope on the way out
    (finite, attenuation-only, at or above
    :data:`~jasper.active_speaker.level_trim.MAX_ATTENUATION_DB`). That envelope
    is relative to UNITY and that is the whole of its guarantee: no accepted
    trim is a boost, and none is deeper than the floor the solver clamps to. It
    says nothing about the datasheet estimate the record replaces — a
    hand-edited tweeter trim of −5.0 dB where the estimate held −10.8 dB is
    inside the envelope and runs that driver 5.8 dB louder than the unmeasured
    speaker would have.
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
        # re-derived: the frame exists at fit time and nowhere else, and a
        # reader comparing two trims has no way to reconstruct it. ``None``
        # means the frame is unknown, which is a refusal to compare rather
        # than a licence to.
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
    # WHICH speaker groups this trim covers, not merely how many.
    # ``crossover_contract.automatic_candidate_readiness`` gates on the
    # measured-group SET against the topology's required one, so a record that
    # levelled only the left cabinet of a stereo pair must not read as having
    # levelled both.
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
        # WHICH evidence the applied profile levelled by, carried through to
        # the profile's own ``level_match`` ledger so a receipt reading the
        # banked trim still names the measurement behind it (ruling S16 (d)).
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
    against — the resolving candidate's own fingerprint, which the apply seam
    reads off the profile's ``source.measured_candidate_fingerprint`` (#3479).
    A trim is DEGENERATE with that chain: a flat trim plus a shelf is the same
    branch-gain profile as a deeper flat trim and no shelf, so on 2026-09-01 a
    −1.54 dB banked against a −6.32 dB Lowshelf and a −12.48 dB resolved on the
    bare graph were read as two estimates of one quantity, disagreeing by
    10.9 dB. They are one physics in two decompositions, and only the chain
    tells them apart. Absent (``None``) is legitimate — a profile levelled by
    the guided captures names no resolving candidate — and so is a value this
    module cannot read: a frame nobody can name is banked as no frame, because
    "unknown" refuses a comparison while a plausible-looking wrong name would
    license one.

    ``measured_at`` is WHEN THE EVIDENCE WAS MEASURED (the newest capture that
    fed the trim), not when this record was written — the S20 supersede
    compares capture times against it, so stamping write time here would let a
    re-persist of a frozen candidate re-date old evidence past newer captures.
    Minted as now only when the caller has no dated evidence at all.

    **Attenuation-only by construction, and REFUSED rather than clamped.** No
    path that reaches this writer can legitimately produce a positive per-role
    trim: both measured-candidate types refuse one at construction
    (``role_attenuations_db``), ``level_trim.attenuation_from_group_deltas``
    shifts its vector so the maximum is exactly 0 dB, and a positive research
    or operator gain is zeroed upstream and never reaches a ``measured``
    correction source. A positive value here is therefore a fault, not a
    reading, and clamping it would bank a trim no measurement produced — so it
    earns :data:`REFUSE_NOT_ATTENUATION` and nothing is written. That keeps the
    reverse-null door's branch-gap depth ceiling and the per-role caps
    argument two independent legs: a positive trim in this artifact would
    couple them.
    """
    ordered = tuple(roles)
    if not isinstance(declaration_fingerprint, str) or not declaration_fingerprint:
        raise DriverBaseTrimError(
            REFUSE_NO_DECLARATION, "a base trim must name the declaration it measured"
        )
    # The reader keys on this string by EQUALITY, so a value that is not a
    # fingerprint at all can never match and banks a record nothing can ever
    # read back. Validated against the shape the six sibling artifacts share.
    # Re-raised rather than passed as ``exc_type`` because this module's error
    # carries the (reason, detail) pair the apply seam logs, unlike the
    # single-message ValueError subclasses those siblings hand it.
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
    # Structural, so it is asked before coverage: a way-1 speaker's trims DO
    # cover its declared roles, and answering it with a coverage complaint
    # would send an operator to re-measure something that cannot exist.
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
        # FULL precision. The graph is playing the unrounded number (the v2
        # path's ``committed_db`` comes off numpy), so banking a rounded one
        # banks a trim nothing applies — the very divergence this artifact
        # exists to close. Rounding belongs in log rendering, not on disk.
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
    # Durable for the reason the SSOT write in the same seam
    # (``persist_applied_baseline_profile``) is: this record and that profile
    # are two halves of one apply, and a power cut that keeps one but not the
    # other leaves the box levelling by numbers its graph is not playing.
    atomic_write_json(
        path, payload, mode=0o640, group_from_parent=True, durable=True
    )
    return payload


def clear_base_trim(*, state_path: str | Path | None = None) -> bool:
    """Drop the banked record. ``True`` when the box is left carrying none.

    The other half of single ownership. A profile applied WITHOUT a measured
    level match — a datasheet seed, an operator pin, a preserved manual
    crossover — is not levelled by the banked trim, so leaving that record in
    place would let a ``--level-matched`` walk level its graph by numbers the
    box is not playing. That is the S12 lie in the direction this artifact's
    own writer would otherwise create.

    **Nothing to drop is SUCCESS; a drop that could not HAPPEN is not.** The
    two cases were one return value until this split, and they are opposites:
    an absent record is the state the clear exists to reach, while a record
    that survived the clear (EACCES, a read-only ``/var/lib``) is exactly the
    stale record the clear exists to prevent. Fail-soft either way — the graph
    is applied by the time this runs — so the caller LOGS the ``False``
    rather than failing the apply on it.
    """
    path = base_trim_state_path(state_path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    # The unlink is metadata, so without this the record can come back after a
    # dirty shutdown — a trim the box has stopped playing, resurrected. The
    # write side is ``durable=True`` for the mirror of this reason.
    # Deliberately NOT part of the verdict: by here the record is gone from
    # the live filesystem, and a parent that cannot even be opened (the
    # directory itself is absent) is the nothing-to-drop case, which is
    # success. Only durability is at stake, so a failure here must not be
    # reported as a banked trim surviving the clear.
    try:
        fsync_directory(path.parent)
    except OSError:
        pass
    return True
