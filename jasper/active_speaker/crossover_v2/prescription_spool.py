# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE accepted prescription, waiting for the round it was written for.

A mailbox: an operator PLACES one already-validated prescription, the next
round TAKES it — one file, one writer (``save_v2_state``), staged from a CLI
in another process. Read and consume are one call, before validation, so a
refused document cannot refuse round after round. Two staged classes share
ONE slot via :data:`ENVELOPE_KIND_FIELD`; ``accepts`` is fail-closed, and
staleness is the round ordinal, re-derived at the take.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event
from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

from .blend_prescription import (
    PRESCRIPTION_KIND,
    PRESCRIPTION_MAX_BYTES,
    BlendPrescription,
    BlendPrescriptionRefused,
    prescription_sha256,
    read_blend_prescription,
    read_prescription_bytes,
)
from .driver_prescription import (
    DRIVER_PRESCRIPTION_KIND,
    MAX_SPL_SPEND_BOUND_DB,
    DriverPrescription,
    check_driver_document_size,
    read_driver_prescription,
)
from .feature_classification import FeatureVerdict, read_feature_verdicts

logger = logging.getLogger(__name__)

__all__ = [
    "BLEND_ONLY",
    "CONSUMED_SUFFIX",
    "DEFAULT_PRESCRIPTION_SPOOL_PATH",
    "ENVELOPE_KIND_FIELD",
    "PRESCRIPTION_CLASS_NOT_ACCEPTED",
    "PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND",
    "PRESCRIPTION_SPOOL_REFUSAL_REASONS",
    "SPOOL_KIND",
    "SPOOL_MALFORMED",
    "SPOOL_MAX_BYTES",
    "SPOOL_SCHEMA_VERSION",
    "SPOOL_TOO_LARGE",
    "STAGEABLE_KINDS",
    "StagedPrescription",
    "prescription_spool_path",
    "set_prescription_spool_path_for_tests",
    "stage_prescription",
    "staged_prescription_pending",
    "take_staged_prescription",
]


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

#: The pending slot. One file rather than a directory: at most ONE staged
#: prescription exists at a time, and a directory would invite a queue nothing
#: here knows how to order.
DEFAULT_PRESCRIPTION_SPOOL_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_v2_prescription.json"
)

#: Where a taken document lands, so an operator can read a refusal beside the
#: document that earned it. Overwritten each take, so it stays one file.
CONSUMED_SUFFIX = ".consumed"

#: The envelope's ``kind``, distinct from
#: :data:`~.blend_prescription.PRESCRIPTION_KIND`: this is a prescription plus
#: the anchors it was checked against and the round it is for.
SPOOL_KIND = "jts_crossover_blend_prescription_staged"

#: An envelope naming a version this build does not speak is refused, never
#: best-effort parsed. Not bumped by the two-class change: an older reader
#: unwraps a per-driver document and refuses it on the document's own ``kind``.
SPOOL_SCHEMA_VERSION = 1

#: Which class the staged document is, on the envelope so a taker can refuse a
#: class it cannot run WITHOUT parsing the document. Absent means the blend
#: class — every envelope written before the per-driver class carries one.
ENVELOPE_KIND_FIELD = "prescription_kind"

#: The classes this slot can carry.
STAGEABLE_KINDS = frozenset({PRESCRIPTION_KIND, DRIVER_PRESCRIPTION_KIND})

#: The default ``accepts`` set: the blend class alone, so a caller that has not
#: learned the per-driver class keeps the fail-closed answer unedited.
BLEND_ONLY = frozenset({PRESCRIPTION_KIND})

#: Byte ceiling on the envelope FILE, read before it is parsed.
#:
#: The document is stored verbatim as a JSON string and JSON escaping is at
#: worst six characters per byte (``\uXXXX``), so an envelope wrapping a
#: document at :data:`.PRESCRIPTION_MAX_BYTES` cannot exceed six times it plus
#: the anchors; eight is that rounded up. The document's own cap is re-applied
#: by :func:`~.blend_prescription.read_prescription_bytes` at the unwrap, so
#: exactly one number polices a prescription's size and it is not this one.
#:
#: The anchors are NOT bounded by the document — the per-driver class banks the
#: round's whole classification, at roughly 244 bytes per verdict row, leaving
#: room for ~500 rows. Re-derived rather than trusted, by staging the same
#: document with and without extra rows, in
#: ``test_the_banked_classification_stays_far_inside_the_envelope_cap``.
SPOOL_MAX_BYTES = 8 * PRESCRIPTION_MAX_BYTES

#: The highest frequency the gate's biquad evaluator is defined for: half of
#: :data:`~jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`, imported from the
#: evaluator's own owner rather than restated. See :func:`_band`.
_EVALUABLE_MAX_HZ = RESPONSE_SAMPLE_RATE_HZ / 2.0


# --------------------------------------------------------------------------- #
# the refusal vocabulary — LIFECYCLE, beside the gates' CONTENT vocabulary
# --------------------------------------------------------------------------- #

#: The envelope is not a readable staged prescription: absent required fields,
#: a wrong ``kind``, a digest that does not match, an unparseable file. A digest
#: mismatch is deliberately this and not a class of its own — the operator's
#: action is "stage it again" either way.
SPOOL_MALFORMED = "spool_malformed"

#: The envelope file is larger than :data:`SPOOL_MAX_BYTES`.
SPOOL_TOO_LARGE = "spool_too_large"

#: This document was staged for a different round. Fires on the ordinal, which
#: neither the staging step nor this module invents — both read it from the
#: round receipt the flow banked.
PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND = "prescription_not_staged_for_this_round"

#: This document's class is not one the taker can run — a LIFECYCLE refusal:
#: the prescription may be perfectly valid for a caller that has a route.
PRESCRIPTION_CLASS_NOT_ACCEPTED = "prescription_class_not_accepted"

#: The lifecycle vocabulary, beside rather than inside
#: :data:`~.blend_prescription.BLEND_PRESCRIPTION_REFUSAL_REASONS`: those slugs
#: say whether a document is a correction this system may apply, these say
#: whether it is the instruction THIS round asked for. The exception TYPE is
#: shared (:class:`.BlendPrescriptionRefused`). Prefixed because an unprefixed
#: ``SPOOL_REFUSAL_REASONS`` collides with :mod:`.angle_capture_spool`'s.
PRESCRIPTION_SPOOL_REFUSAL_REASONS = frozenset({
    SPOOL_MALFORMED,
    SPOOL_TOO_LARGE,
    PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND,
    PRESCRIPTION_CLASS_NOT_ACCEPTED,
})


# --------------------------------------------------------------------------- #
# the path, and its test seam
# --------------------------------------------------------------------------- #

_spool_path_override: Path | None = None


def prescription_spool_path() -> Path:
    """The pending slot's resolved path."""
    return _spool_path_override or DEFAULT_PRESCRIPTION_SPOOL_PATH


def set_prescription_spool_path_for_tests(path: Path | None) -> None:
    """Point the spool at a temporary directory."""
    global _spool_path_override
    _spool_path_override = path


def _consumed_path(pending: Path) -> Path:
    return pending.with_suffix(CONSUMED_SUFFIX + pending.suffix)


def _refuse(reason: str, detail: str, **evidence: Any) -> NoReturn:
    """``NoReturn``, matching :mod:`.blend_prescription`'s own ``_refuse``.

    Typed ``NoReturn`` so each call site reads as a guard rather than a
    fall-through that widens every downstream type.
    """
    raise BlendPrescriptionRefused(reason, detail, evidence=evidence)


# --------------------------------------------------------------------------- #
# what a taken prescription is
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StagedPrescription:
    """One validated prescription, and the three facts the round needs about it."""

    #: Re-validated at take time, never rehydrated from the banked class. Which
    #: of the two types it is, is :attr:`prescription_kind`.
    prescription: BlendPrescription | DriverPrescription
    #: The digest of the document bytes, re-proved against the stored document
    #: before this object exists.
    prescription_sha256: str
    #: The round this was staged for, and the round that took it — equal by
    #: construction, since a mismatch refuses instead of returning.
    for_round_ordinal: int
    #: The document's own ``kind``, one of :data:`STAGEABLE_KINDS`.
    prescription_kind: str = PRESCRIPTION_KIND

    def record(self) -> dict[str, Any]:
        """The provenance a round receipt carries.

        The prescription's own view plus the digest: the prescription is WHAT
        was asked for, the digest names the document that asked.
        """
        return {
            **self.prescription.to_dict(),
            "prescription_sha256": self.prescription_sha256,
            "for_round_ordinal": self.for_round_ordinal,
        }


# --------------------------------------------------------------------------- #
# place
# --------------------------------------------------------------------------- #


def _anchors(
    prescription: BlendPrescription | DriverPrescription,
    classifications: Sequence[FeatureVerdict] | None,
) -> dict[str, Any]:
    """The evidence anchors the take cannot re-derive, from the step that could.

    One per class: the blend document is bounded by ONE region, the per-driver
    document by a band per role plus the round's WHOLE banked classification.
    The whole row set, not the vouching subset — the vouch is
    ``defect_cuttable_at``/``defect_boostable_at``'s nearest-verdict-decides
    rule, so a subset would report a different count from the one the operator
    read at staging.

    Neither these anchors nor the digest beside them is a tamper defence: the
    envelope is one operator-writable 0640 file, so whoever can edit the
    document can edit the verdicts and recompute the digest in the same pass.
    """
    if isinstance(prescription, DriverPrescription):
        return {
            ENVELOPE_KIND_FIELD: DRIVER_PRESCRIPTION_KIND,
            "passbands_hz": [
                [role, lo, hi] for role, lo, hi in prescription.passbands_hz
            ],
            "classifications": [
                verdict.to_dict() for verdict in (classifications or ())
            ],
        }
    return {
        ENVELOPE_KIND_FIELD: PRESCRIPTION_KIND,
        "band_hz": [prescription.band_hz[0], prescription.band_hz[1]],
    }


def stage_prescription(
    document: bytes,
    prescription: BlendPrescription | DriverPrescription,
    *,
    for_round_ordinal: int,
    classifications: Sequence[FeatureVerdict] | None,
) -> Path:
    """Bank one ALREADY-VALIDATED prescription for the next round.

    ``prescription`` must be what the gate returned for ``document`` against
    the round's real evidence packet; this function has no packet and cannot
    re-run it. ``classifications`` is the same round's banked verdicts,
    unfiltered, and is REQUIRED rather than defaulted — a caller that forgot it
    would look fine at staging and disagree with the operator's vouch count a
    round later. The blend class has no such evidence and passes ``None``.

    ``document`` is stored VERBATIM: the digest is over these bytes, and a
    re-serialized copy would hash differently on every formatting difference.

    The write is atomic, mode ``0o640`` with the parent's group, and the group
    assignment is STRICT — the CLI writes this file for another user to read,
    so a silent fallback to the writer's own group would publish a document
    ``jasper-web`` cannot open. Staging twice is last-wins, logged with
    ``replaced``.
    """
    replaced = prescription_spool_path().is_file()
    driver = prescription if isinstance(prescription, DriverPrescription) else None
    payload = {
        "artifact_schema_version": SPOOL_SCHEMA_VERSION,
        "kind": SPOOL_KIND,
        "for_round_ordinal": int(for_round_ordinal),
        # The anchors the take cannot re-derive, from the step that could.
        "packet_fingerprint": prescription.packet_fingerprint,
        **_anchors(prescription, classifications),
        "prescription_sha256": prescription_sha256(document),
        "staged_at": time.time(),
        "document": document.decode("utf-8"),
    }
    path = prescription_spool_path()
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        mode=0o640,
        group_from_parent=True,
        durable=True,
    )
    log_event(
        logger, "crossover_v2.prescription_staged",
        for_round_ordinal=int(for_round_ordinal),
        prescription_sha256=payload["prescription_sha256"],
        prescription_kind=payload[ENVELOPE_KIND_FIELD],
        # Read off the PAYLOAD, not the argument: the one dimension of this file
        # the document's own cap does not bound (see :data:`SPOOL_MAX_BYTES`),
        # so a spool creeping towards `spool_too_large` is visible in the log.
        classifications=len(payload.get("classifications") or ()),
        # What this document costs. `None` — never `0.0` — on the blend class,
        # which has no per-driver seam to spend: "not applicable" and "measured
        # nothing" are different facts, and that holds for every field below.
        prescription_class=prescription.prescription_class,
        boost_filters=sum(
            1 for entry in prescription.filters if float(entry["gain"]) > 0.0
        ),
        composed_boost_db=driver.composed_boost_db if driver else None,
        composed_boost_role=driver.composed_boost_role if driver else None,
        max_spl_spend_bound_db=MAX_SPL_SPEND_BOUND_DB if driver else None,
        # …and what it DELETES: a per-driver document is a TOTAL for every role
        # it names, so incumbent filters it does not repeat go away.
        displaced_filters=driver.displaced_filters if driver else None,
        displaced_boost_db=driver.displaced_boost_db if driver else None,
        displaced_boost_role=driver.displaced_boost_role if driver else None,
        # …and how much of it the round's own evidence backs. It BOUNDS nothing
        # — the classification vouch discloses and never refuses — so this is
        # the line an operator greps to see what a staged document bets on.
        unvouched_filters=driver.unvouched_filters if driver else None,
        replaced=replaced,
    )
    return path


def staged_prescription_pending() -> bool:
    """Is a document waiting? The stat, and only the stat.

    Never widened to peek at the contents: :func:`take_staged_prescription`
    stays the only reader, so "consumed on the round starting, never reused"
    is a property rather than a convention callers honour.
    """
    return prescription_spool_path().is_file()


# --------------------------------------------------------------------------- #
# take
# --------------------------------------------------------------------------- #


def take_staged_prescription(
    *,
    round_ordinal: int,
    accepts: frozenset[str] = BLEND_ONLY,
) -> StagedPrescription | None:
    """THE reader. Consumes first, then validates, and never returns a reused one.

    ``accepts`` names the classes THIS caller can route, defaulting to
    :data:`BLEND_ONLY` — fail-closed. ``None`` when no document is staged,
    which is every ordinary round. Raises
    :class:`~.blend_prescription.BlendPrescriptionRefused` with a slug from
    either vocabulary when a document IS staged and this round may not run it.

    The consume happens BEFORE the validation, and that ordering is the
    contract: a refused document has still had its round, and left in the slot
    it would refuse every round after it. An unreadable spool file answers
    :data:`SPOOL_MALFORMED` rather than raising ``OSError`` into the round.
    """
    pending = prescription_spool_path()
    try:
        stat = pending.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _refuse(SPOOL_MALFORMED, f"the staged prescription cannot be read: {exc}")
    if stat.st_size > SPOOL_MAX_BYTES:
        # Refused on the STAT, before the bytes are read: a cap applied after
        # the read has already paid what it exists to avoid.
        _consume(pending)
        _refuse(
            SPOOL_TOO_LARGE,
            f"a staged prescription may be at most {SPOOL_MAX_BYTES} bytes, got "
            f"{stat.st_size}",
            max_bytes=SPOOL_MAX_BYTES,
            got_bytes=stat.st_size,
        )
    try:
        raw = pending.read_bytes()
    except OSError as exc:
        _refuse(SPOOL_MALFORMED, f"the staged prescription cannot be read: {exc}")
    _consume(pending)
    if len(raw) > SPOOL_MAX_BYTES:
        # The stat stops a huge file being LOADED; this makes the cap a
        # property of the BYTES, for a file that grew between the two calls.
        _refuse(
            SPOOL_TOO_LARGE,
            f"a staged prescription may be at most {SPOOL_MAX_BYTES} bytes, got "
            f"{len(raw)}",
            max_bytes=SPOOL_MAX_BYTES,
            got_bytes=len(raw),
        )
    return _validate(raw, round_ordinal=round_ordinal, accepts=accepts)


def _consume(pending: Path) -> None:
    """Move the pending document out of the slot, atomically.

    ``os.replace`` rather than unlink, so the document survives for the operator
    and a concurrent reader cannot observe the slot half-emptied. Best-effort:
    the ordinal check still refuses a document a failed consume left behind.
    """
    try:
        os.replace(pending, _consumed_path(pending))
    except OSError:
        try:
            pending.unlink()
        except OSError:
            pass


def _validate(
    raw: bytes, *, round_ordinal: int, accepts: frozenset[str]
) -> StagedPrescription:
    """The envelope's own gate, then the prescription gate re-run on top."""
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        _refuse(SPOOL_MALFORMED, f"the staged prescription is not readable JSON: {exc}")
    if not isinstance(envelope, dict):
        _refuse(
            SPOOL_MALFORMED,
            f"a staged prescription must be a JSON object, got "
            f"{type(envelope).__name__}",
        )
    if envelope.get("kind") != SPOOL_KIND:
        _refuse(
            SPOOL_MALFORMED,
            f"a staged prescription must name kind={SPOOL_KIND!r}, got "
            f"{envelope.get('kind')!r}",
        )
    if envelope.get("artifact_schema_version") != SPOOL_SCHEMA_VERSION:
        _refuse(
            SPOOL_MALFORMED,
            f"this build speaks staged-prescription schema "
            f"{SPOOL_SCHEMA_VERSION}, got "
            f"{envelope.get('artifact_schema_version')!r}",
        )
    # BEFORE the ordinal: the class decides which anchors the rest of this
    # function may read. Absent means the blend class.
    staged_kind = envelope.get(ENVELOPE_KIND_FIELD, PRESCRIPTION_KIND)
    # The `isinstance` is load-bearing: `x in frozenset` HASHES `x`, so a list
    # or dict here raises `TypeError: unhashable type` out of a refusal path.
    if not isinstance(staged_kind, str) or staged_kind not in STAGEABLE_KINDS:
        _refuse(
            SPOOL_MALFORMED,
            f"a staged prescription must name a class this build stages "
            f"({', '.join(sorted(STAGEABLE_KINDS))}), got {staged_kind!r}",
        )
    if staged_kind not in accepts:
        _refuse(
            PRESCRIPTION_CLASS_NOT_ACCEPTED,
            f"this prescription is a {staged_kind!r} and the round taking it "
            f"can run {', '.join(sorted(accepts)) or 'no class'}; the "
            "prescription may be perfectly valid, but nothing here has a route "
            "for it",
            staged_kind=staged_kind,
            accepts=sorted(accepts),
        )
    staged_for = envelope.get("for_round_ordinal")
    if not isinstance(staged_for, int) or isinstance(staged_for, bool):
        _refuse(
            SPOOL_MALFORMED,
            "a staged prescription must name the round ordinal it was staged "
            f"for, got {staged_for!r}",
        )
    # BEFORE the document is unwrapped: a bound failure reported on evidence
    # this round was never going to use would send an operator to fix numbers
    # that were right for the round they answered.
    if staged_for != round_ordinal:
        _refuse(
            PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND,
            f"this prescription was staged for round {staged_for} and this is "
            f"round {round_ordinal}; a prescription answers one round's evidence "
            "and is an instruction for the round after it and no other",
            staged_for_round=staged_for,
            this_round=round_ordinal,
        )
    document = envelope.get("document")
    if not isinstance(document, str):
        _refuse(
            SPOOL_MALFORMED,
            f"a staged prescription must carry its document, got "
            f"{type(document).__name__}",
        )
    try:
        payload = document.encode("utf-8")
    except UnicodeEncodeError as exc:
        # A JSON string may hold a LONE SURROGATE — `json.loads` accepts
        # ``"\ud800"`` and produces a `str` no UTF-8 encoder will take. The
        # encode is a parse step, not a formality.
        _refuse(
            SPOOL_MALFORMED,
            f"the staged document is not encodable UTF-8 text: {exc}",
        )
    digest = envelope.get("prescription_sha256")
    actual = prescription_sha256(payload)
    if digest != actual:
        _refuse(
            SPOOL_MALFORMED,
            "the staged document does not match the digest banked beside it, so "
            "this is not the document that was accepted; stage it again",
            staged_sha256=digest,
            actual_sha256=actual,
        )
    # The gate itself, re-run — same function, same bounds, one per class.
    prescription: BlendPrescription | DriverPrescription | None
    if staged_kind == DRIVER_PRESCRIPTION_KIND:
        # The stat above bounded the envelope; this bounds the document inside
        # it, before it is parsed at all.
        check_driver_document_size(payload)
        prescription = read_driver_prescription(
            read_prescription_bytes(payload),
            packet_fingerprint=envelope.get("packet_fingerprint"),
            passbands_hz=_passbands(envelope.get("passbands_hz")),
            classifications=read_feature_verdicts(envelope.get("classifications")),
            # The packet is gone, and this argument buys only a disclosure the
            # `stage_prescription` log line already made.
            incumbent_filters=None,
        )
    else:
        # `positional_evidence` is None because the packet is gone and
        # inventing one would be a self-certifying read. A boost tampered into
        # the document then refuses at the ROUTE on `boost_route_unavailable`.
        prescription = read_blend_prescription(
            read_prescription_bytes(payload),
            packet_fingerprint=envelope.get("packet_fingerprint"),
            band_hz=_band(envelope.get("band_hz")),
            positional_evidence=None,
        )
    if prescription is None:  # pragma: no cover - `read_prescription_bytes` refuses
        _refuse(SPOOL_MALFORMED, "the staged prescription document was empty")
    return StagedPrescription(
        prescription=prescription,
        prescription_sha256=actual,
        for_round_ordinal=staged_for,
        prescription_kind=staged_kind,
    )


def _passbands(raw: Any) -> dict[str, tuple[float, float]]:
    """The banked per-role bands, or ``{}`` so the gate refuses them by name.

    ``{}`` rather than a raise:
    :data:`~.driver_prescription.PASSBAND_UNAVAILABLE` owns that sentence. The
    Nyquist bound is :func:`_band`'s, for its reason. A role whose band does not
    clear it is DROPPED rather than failing the whole read, so one corrupt row
    cannot widen another role's band.
    """
    if not isinstance(raw, list):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 3:
            continue
        role = entry[0]
        if not isinstance(role, str) or not role.strip():
            continue
        try:
            lo, hi = float(entry[1]), float(entry[2])
        except (TypeError, ValueError, OverflowError):
            continue
        if 0.0 < lo < hi <= _EVALUABLE_MAX_HZ:
            out[role.strip()] = (lo, hi)
    return out


def _band(raw: Any) -> tuple[float, float] | None:
    """The banked region, or ``None`` so the gate refuses it by name.

    ``None`` rather than a raise:
    :data:`~.blend_prescription.REGION_UNAVAILABLE` owns that sentence.

    The Nyquist bound is the guard. This band goes straight to a gate that
    EVALUATES biquads across it, and ``isfinite`` does not close the class:
    ``(1.0, 1e308)`` is finite, orders correctly, and still raises a math
    domain error inside ``math.cos(2πf/fs)``. The evaluator's own half-sample-
    rate bound does, and it subsumes ``isfinite`` — ``inf`` fails
    ``hi <= _EVALUABLE_MAX_HZ`` and NaN fails ``0.0 < lo < hi`` — which is why
    there is no isfinite call here. (The sibling
    :func:`~.blend_prescription.blend_prescription_from_mapping` keeps its own:
    it evaluates nothing and has no Nyquist bound.)
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0.0 < lo < hi):
        return None
    return (lo, hi) if hi <= _EVALUABLE_MAX_HZ else None
