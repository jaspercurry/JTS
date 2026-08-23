# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE accepted prescription, waiting for the round it was written for.

:mod:`.blend_prescription` turns an outside opinion into a validated
:class:`~.blend_prescription.BlendPrescription`.  It deliberately stops there —
"the model proposes; the harness disposes" — and until this module existed,
nothing disposed: ``jasper-crossover-prescriber propose`` accepted a
prescription and printed what it *would* become, and the live flow's blend
reader (``CrossoverV2Session._blend_prescription``) took instructions only from
the prior round's receipt or the graph already playing.  A validated
prescription had no route into a round.  The 2026-08-19 wired night hit exactly
that wall with an accepted cut in hand and recorded it as finding A9.

**This module is the door, and it is a mailbox rather than a mechanism.**  An
operator PLACES one accepted prescription; the next round TAKES it.  Everything
else — how it is graded, how it reaches the candidate, how it is undone — is
machinery that already exists and is deliberately not duplicated here.

**Why a file of its own rather than a key in the flow state.**  The flow's
durable state (``correction_crossover_v2.DEFAULT_V2_STATE_PATH``) has exactly
one writer, ``save_v2_state``, and every one of its callers lives in that one
web module — a rule a test names writer-by-writer.  The staging step runs from
a CLI, in another process, as another user, possibly while a session is live.
Teaching that CLI to write the flow state would make it a second writer of the
one file whose single-writer property the whole flow rests on.  So the
instruction gets its OWN path with its OWN owner: this module.  Three callers
use its API and none of them touch the bytes — :func:`stage_prescription` from
the CLI, :func:`take_staged_prescription` from the round's preparer, and
:func:`withdraw_staged_prescription` from Undo.

**What "taking" means, and why reading and consuming are one call.**  There is
no way to read a staged prescription without consuming it.
:func:`take_staged_prescription` moves the document out of the pending slot
BEFORE it validates it, so "consumed on the round starting, never reused" is a
property of the function rather than of a caller remembering to clean up — and
a refused document is consumed too, because a document that was wrong for this
round would otherwise refuse every round after it as well.  The consumed
document survives at :data:`CONSUMED_SUFFIX` for the operator to look at.

**What the second validation can and cannot prove, stated plainly.**  The
staging step holds the real evidence packet, so it validates against that
packet's own numbers — the full strength of
:func:`~.blend_prescription.read_blend_prescription`.  The round that takes the
document does NOT have the packet: at candidate-build time this round's region
has not been measured yet (that is why decision 10 banks an instruction at all).
So the take re-runs the same gate against the region the staging step banked
beside the document.  What that re-run genuinely catches: a corrupt or truncated
file, a document edited to deepen a cut, widen a Q, move a filter outside the
region it was checked against, add a third filter, or add a prohibited key —
every one of which is refused by name.  A tampered SIGN is judged by the same
bar the staging gate applied, not by the sign itself: on the blend class a boost
still has no route, and on the per-driver class a boost edited onto a feature
that cannot vouch for it refuses by name, while one that clears every bar is
correctly TAKEN.  What it cannot do
is re-derive the packet, so the banked region and fingerprint are anchors it
takes on trust from the step that had the evidence.  The document itself is not
taken on trust: it is stored VERBATIM and re-hashed, so
:data:`~.blend_prescription.prescription_sha256` names the bytes that actually
ran rather than the bytes somebody meant to run.

**Staleness is the ordinal, and the ordinal is the one fact this module does not
own.**  A prescription answers round N's evidence and is therefore an
instruction for round N+1 and no other round.  The staging step records that
ordinal from ``series_position_from_state``; the take re-derives it the same way
from the same durable state and refuses a mismatch as
:data:`PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND`.  Because the receipt's ordinal
advances whenever a round completes, a document left behind by a round that
already ran can never be picked up by the next one — the belt to consumption's
braces, and the half that still works if a process died between them.

**This module opens no route.**  It carries whatever class the seam already
accepts, and the two seams differ: :func:`~.blend_prescription.prescription_route`
still refuses every boost, while :mod:`.driver_prescription` admits one against
a banked ``defect-boostable`` verdict.  Neither answer is decided here.

**Two STAGED prescription classes, ONE door.**  Since the per-driver class
(:mod:`.driver_prescription`) there are two shapes an operator may stage — a
blend-region correction and one driver's own full-band shape — and they share
this slot rather than each getting one.  "Staged" is the load-bearing word:
two further prescriptions (:mod:`.alignment_prescription`,
:mod:`.topology_prescription`) never reach this module at all, because they
arrive as request-body keys at session open and are judged there (#2773).  A round takes AN instruction, and two
slots would be two things to keep consistent, two things Undo has to remember,
and an ordering question ("which wins?") nothing in this design answers.  The
envelope therefore names its document's class in :data:`ENVELOPE_KIND_FIELD`,
and every taker says which classes it can actually run.

**The take is fail-closed about the class, and that is deliberate.**
:func:`take_staged_prescription`'s ``accepts`` defaults to the blend class
alone.  A caller that has not been taught to route a per-driver prescription
therefore cannot be handed one by accident — it refuses by name instead.  The
direction matters: a permissive default here would hand an unrecognised class
to a caller whose next line assumes the other one.  The round's own preparer
HAS been taught both since PR-B and passes :data:`STAGEABLE_KINDS`; the default
still protects every caller that has not.
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
    "withdraw_staged_prescription",
]


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

#: The pending slot. A sibling of the flow state file rather than a directory of
#: its own: there is at most ONE staged prescription at a time by construction
#: (a round takes an instruction or it does not), and a directory would invite a
#: queue nothing in this design knows how to order.
DEFAULT_PRESCRIPTION_SPOOL_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_v2_prescription.json"
)

#: Where a taken document lands. Kept rather than unlinked so an operator whose
#: prescription was refused can read the refusal beside the document that earned
#: it; overwritten each take, so it is bounded at one file rather than growing a
#: history nothing prunes.
CONSUMED_SUFFIX = ".consumed"

#: The envelope's ``kind``. Distinct from
#: :data:`~.blend_prescription.PRESCRIPTION_KIND` on purpose — this is not a
#: prescription, it is a prescription plus the anchors it was checked against
#: and the round it is for, and a reader handed one where it expected the other
#: should say so rather than half-parse it.
SPOOL_KIND = "jts_crossover_blend_prescription_staged"

#: Bumped when an older reader would misread a newer envelope. Load-bearing for
#: the same reason the packet's is: an envelope naming a version this build does
#: not speak is refused, never best-effort parsed.
#:
#: **NOT bumped by the two-class change**, and the rule is what says so rather
#: than convenience. An older reader handed a per-driver envelope ignores
#: :data:`ENVELOPE_KIND_FIELD` (this reader has never rejected unknown envelope
#: fields), unwraps the document, and hands it to
#: :func:`~.blend_prescription.read_blend_prescription` — which refuses on the
#: document's own ``kind`` before it looks at a single number. That is a named
#: refusal, not a misread, and it is exactly what the document naming itself has
#: always been for. A bump would buy nothing and would invalidate any envelope
#: staged across a deploy.
SPOOL_SCHEMA_VERSION = 1

#: Which class the staged document is, named on the envelope so a taker can
#: refuse a class it cannot run WITHOUT unwrapping and parsing the document
#: first. Absent means the blend class: every envelope written before the
#: per-driver class existed carries a blend document, and reading absence as
#: anything else would misdate the corpus.
ENVELOPE_KIND_FIELD = "prescription_kind"

#: The classes this slot can carry.
STAGEABLE_KINDS = frozenset({PRESCRIPTION_KIND, DRIVER_PRESCRIPTION_KIND})

#: The default ``accepts`` set: the blend class alone.
#:
#: Named rather than spelled inline at the default, because it is a statement
#: about which classes a caller can route rather than about which classes
#: exist. The round's preparer passes :data:`STAGEABLE_KINDS` (PR-B); every
#: other caller keeps the fail-closed answer without being edited, which is
#: what the name buys.
BLEND_ONLY = frozenset({PRESCRIPTION_KIND})

#: Byte ceiling on the envelope FILE, read before it is parsed.
#:
#: Derived from the document cap this module does not own and does not restate.
#: The document is stored verbatim as a JSON string, and JSON string escaping is
#: at worst six characters per byte (``\uXXXX`` for a control character), so an
#: envelope wrapping a document at :data:`.PRESCRIPTION_MAX_BYTES` cannot exceed
#: six times it plus the anchors. Eight is that bound rounded up. The document's
#: own cap is re-applied by its owner —
#: :func:`~.blend_prescription.read_prescription_bytes` — when the take unwraps
#: it, so there is exactly one number policing a prescription's size and it is
#: not this one.
#:
#: The anchors are NOT bounded by the document: the per-driver class banks the
#: round's whole classification, whose row count is the MEASUREMENT's fact
#: rather than the prescription's. A row's cost is not a constant — it is the
#: length of that verdict's own strings — so the figure below names its
#: derivation route rather than standing as a bare number, and a test re-derives
#: it instead of trusting this comment.
#:
#: **Route: stage the same document twice through this module's own writer —
#: once with N extra rows, once without — and diff the bytes on disk.** Not a
#: hand-built payload: the envelope's nesting and ``indent=2`` are part of the
#: answer, and a payload assembled by hand measures a shape this module does not
#: write. On the 2026-08-19 record's nine rows, rendered by the repo's own
#: fixture for them, that delta is **≈2.2 KiB — 244 bytes per row**, leaving
#: room for roughly 500 rows in the 128 KiB this cap keeps over even the
#: pathological all-escaped document above.
#:
#: Treat the row figure as an order of magnitude rather than a constant. It
#: moves a few percent with a verdict's own string lengths, and the whole-file
#: figure jitters by a byte or two with ``staged_at``'s serialization — while
#: the conclusion sits two orders of magnitude from caring. A classification
#: past it refuses the take by name (:data:`SPOOL_TOO_LARGE`) rather than
#: truncating, which is the direction that cannot admit a filter nobody judged.
#: Re-derived on every run, by this route, in
#: ``test_the_banked_classification_stays_far_inside_the_envelope_cap``.
SPOOL_MAX_BYTES = 8 * PRESCRIPTION_MAX_BYTES

#: The highest frequency the gate's biquad evaluator is defined for.
#:
#: Half of :data:`~jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`, imported from
#: the evaluator's own owner rather than written down here, on the rule
#: :mod:`.blend_prescription` follows for the cut ceilings it imports from the
#: deterministic solver. Used by :func:`_band` — see its docstring for why a
#: finite-but-absurd band edge is a refusal rather than a curiosity.
_EVALUABLE_MAX_HZ = RESPONSE_SAMPLE_RATE_HZ / 2.0


# --------------------------------------------------------------------------- #
# the refusal vocabulary — a SECOND set, and the split is the point
# --------------------------------------------------------------------------- #

#: The envelope is not a readable staged prescription: absent required fields,
#: a wrong ``kind``, a document whose digest does not match the one banked
#: beside it, an unparseable file.
#:
#: A digest mismatch is deliberately this rather than a class of its own: the
#: operator's action is identical for a truncated write and a hand-edit ("stage
#: it again"), and a slug that distinguished them would be inviting a reader to
#: treat one as benign.
SPOOL_MALFORMED = "spool_malformed"

#: The envelope file is larger than :data:`SPOOL_MAX_BYTES`.
SPOOL_TOO_LARGE = "spool_too_large"

#: This document was staged for a different round.
#:
#: The lifecycle refusal, and the only one this module can raise about a
#: document that is otherwise perfectly valid. It fires on the ordinal, which
#: neither the staging step nor this module invents — both read it from the
#: round receipt the flow banked.
PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND = "prescription_not_staged_for_this_round"

#: This document's class is not one the taker can run.
#:
#: A LIFECYCLE refusal and not a content one, which is why it lives in this
#: vocabulary: the prescription may be perfectly valid, and would be accepted by
#: a caller that had a route for it. What it says is "not this taker" — the same
#: shape as the ordinal refusal beside it, about a different axis. Since PR-B
#: the round's preparer accepts every stageable class, so this answers a
#: caller's narrower ``accepts``, not an unbuilt route.
PRESCRIPTION_CLASS_NOT_ACCEPTED = "prescription_class_not_accepted"

#: The lifecycle vocabulary, beside rather than inside
#: :data:`~.blend_prescription.BLEND_PRESCRIPTION_REFUSAL_REASONS`.
#:
#: Two sets because they answer two questions with two owners. That module's
#: slugs say whether a proposal is a correction this system may apply — a fact
#: about CONTENT, decided by the gate, and the same answer wherever the document
#: came from. These say whether a document that already passed that gate is the
#: instruction THIS round asked for — a fact about LIFECYCLE, which the gate has
#: no opinion about and could not check. Merging them would give the content
#: vocabulary a second owner and tell a prescriber its numbers were wrong when
#: its timing was.
#:
#: The exception TYPE is shared (:class:`.BlendPrescriptionRefused`), because
#: every caller already handles that shape and a second exception class would
#: buy a second ``except`` arm and nothing else.
#:
#: ``PRESCRIPTION_SPOOL_`` prefixed: an unprefixed ``SPOOL_REFUSAL_REASONS``
#: would collide with :mod:`.angle_capture_spool`'s own bare
#: ``SPOOL_REFUSAL_REASONS`` — two different closed vocabularies for two
#: different mailboxes, sharing one name. Values are unchanged; only the
#: Python identifier moved.
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
    """Point the spool at a temporary directory.

    The seam ``correction_crossover_v2.set_state_path_for_tests`` already
    established, and for its reason: the production callers name no path, so a
    per-call override would be a parameter nothing in production ever passes and
    every test would have to thread.
    """
    global _spool_path_override
    _spool_path_override = path


def _consumed_path(pending: Path) -> Path:
    return pending.with_suffix(CONSUMED_SUFFIX + pending.suffix)


def _refuse(reason: str, detail: str, **evidence: Any) -> NoReturn:
    """``NoReturn``, matching :mod:`.blend_prescription`'s own ``_refuse``.

    Every call site here is a guard, and the line after it assumes the guard
    passed. Typed as returning ``None`` the guards read as fall-throughs, so a
    checker widens everything downstream to include the failed case — which is
    how a refusal that stopped raising would surface as a type complaint on
    innocent code instead of on itself.
    """
    raise BlendPrescriptionRefused(reason, detail, evidence=evidence)


# --------------------------------------------------------------------------- #
# what a taken prescription is
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StagedPrescription:
    """One validated prescription, and the three facts the round needs about it."""

    #: Re-validated at take time, never rehydrated from the banked class. Which
    #: of the two types it is, is :attr:`prescription_kind` — a caller that
    #: passed a single-class ``accepts`` already knows, and one that passed
    #: both must ask rather than isinstance-sniff a value object.
    prescription: BlendPrescription | DriverPrescription
    #: The digest of the document bytes, carried from staging and re-proved
    #: against the stored document before this object exists.
    prescription_sha256: str
    #: The round this was staged for, and the round that took it — equal by
    #: construction, since a mismatch refuses instead of returning.
    for_round_ordinal: int
    #: The document's own ``kind``, one of :data:`STAGEABLE_KINDS`. Defaulted to
    #: the blend class so every existing construction of this record — the
    #: tests' included — keeps meaning what it meant.
    prescription_kind: str = PRESCRIPTION_KIND

    def record(self) -> dict[str, Any]:
        """The provenance a round receipt carries.

        The prescription's own view plus the digest, which is not part of the
        prescription: the prescription is WHAT was asked for, the digest names
        the document that asked. A reader six weeks later needs both — the
        first to know what the round applied, the second to find the file and
        the model conversation that produced it.
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

    One per class, because the two gates are measured against different
    evidence. The blend document is bounded by ONE region, so its anchor is one
    band. The per-driver document is bounded by a band PER ROLE and, unlike its
    sibling, carries a second evidence bar — so its anchors are the role bands
    and the round's WHOLE banked classification, exactly as the staging gate
    read it.

    Banking the verdicts is what makes the driver class's re-validation as
    strong at take time as it was at staging time, which the blend class's is
    deliberately not: that one passes ``positional_evidence=None``, so a boost
    tampered into the document refuses on missing evidence rather than on the
    bar. Here the SAME evidence that judged the filters is offered back, so a
    document edited to move a filter onto an unclassified — or an
    interference-barred — feature refuses on the bar itself, by its own name.

    **The whole row set, not the vouching subset, and that is the fix for a
    hole the subset had.** ``classification_basis`` holds only the verdicts
    that PASSED the bar, so every verdict in it is ``defect-cuttable`` by
    construction and the minimum-phase DIPS are exactly what it drops. The bar
    is ``defect_cuttable_at``'s nearest-verdict-decides rule, and it needs the
    dips in order to say no: on the 2026-08-19 record a filter honestly aimed
    at the 4149 Hz peak, moved 0.143 octaves onto the 4582 Hz dip, found the
    peak still banked, no dip to outrank it, and was ACCEPTED — the precise
    proposal the bar exists to refuse.

    The property this buys is not "refuses more"; it is EQUALITY, **and it is
    equality on the classification bar alone.** Same function, same verdicts,
    so the take's answer to "may this filter be aimed here" is the answer the
    staging gate gave it — and the subset was wrong in both directions rather
    than merely lenient: it hid the dips (a false accept, the hole above) and
    it also hid every cuttable feature no filter happened to aim at, so a
    filter moved onto one of THOSE refused as unclassified when the staging
    step would have admitted it. A subset is an argument about how much
    evidence is enough; handing back what the gate actually read needs no
    argument.

    The OTHER anchor on this class, ``passbands_hz``, keeps the weaker property
    it has always had and that #2740's review examined and accepted: it is the
    envelope's own copy of the bands, bounded only by Nyquist, so it can be
    widened by whoever can write the file. That is inside the spool's threat
    model — a 0640 operator-written file — and nothing here changes it.
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

    ``prescription`` must be the object
    :func:`~.blend_prescription.read_blend_prescription` returned for
    ``document`` against the round's real evidence packet. This function does
    not re-run that gate and deliberately cannot: it has no packet, and a
    staging step that validated with anything weaker than the packet would be
    the second, laxer reader this design exists to avoid.

    ``classifications`` is the same round's banked verdicts, exactly as they
    were handed to that gate — the unfiltered result of
    :func:`~.evidence_packet.packet_feature_classifications` — and it is
    REQUIRED rather than defaulted for the reason ``fitted`` is on
    ``read_blend_prescription``'s rule: it is the input a caller can forget
    while everything still looks fine at staging time, and the damage lands a
    round later, at the take, on a bar quietly weaker than the one that
    accepted the document. The blend class has no such evidence and passes
    ``None``; :func:`_anchors` is where the two classes part.

    ``document`` is stored VERBATIM, not re-serialized from the parsed object.
    The digest banked beside it is over these bytes, so the take can prove the
    document it is about to run is the document that was accepted — a
    re-serialized copy would hash differently on every formatting difference and
    the digest would name nothing.

    The write is atomic and mode ``0o640`` with the parent's group, matching the
    flow state file it sits beside: the operator's CLI writes it, the web user
    reads and consumes it, and neither should be able to publish it wider. The
    group assignment is STRICT — no ``best_effort_group`` — which is
    ``save_v2_state``'s posture and is the right one here for a sharper reason
    than symmetry: this file exists to be read by another user. A write that
    silently fell back to the writer's own group would publish a document
    ``jasper-web`` cannot open, and the failure would surface a round later as a
    prescription that mysteriously did not apply. Failing the ``stage`` command
    loudly (exit ``3``) tells the operator at the moment they can fix it.

    **Staging twice is last-wins, and that is the decision rather than an
    accident.** The slot holds ONE instruction because a round takes one; an
    operator who re-prescribes after re-reading the evidence means the second
    document, and a refusal there would make them delete a file by hand to
    correct themselves. The overwrite is logged
    (``event=crossover_v2.prescription_staged`` carries ``replaced``) so it is
    never silent, and the atomic rename means a concurrent take sees one whole
    document or the other, never a splice.
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
        # The established event, extended rather than twinned: a second event
        # name for "a prescription was staged" would make an operator grepping
        # the journal for staging see half of them.
        prescription_kind=payload[ENVELOPE_KIND_FIELD],
        # Read off the PAYLOAD, not the argument: this reports what the envelope
        # actually carries. It is the one dimension of this file the document's
        # own cap does not bound (see :data:`SPOOL_MAX_BYTES`), so an operator
        # watching a spool creep towards `spool_too_large` can see it grow
        # without opening the file.
        classifications=len(payload.get("classifications") or ()),
        # What this document costs, on the line that records it being banked:
        # the class, how many filters spend maximum SPL, the evaluated
        # worst-role boost that decided, and the ceiling it was measured
        # against. The last two are `None` on the blend class, which has no
        # per-driver seam to spend — an absent number rather than a zero,
        # because "not applicable" and "measured nothing" are different facts.
        prescription_class=prescription.prescription_class,
        boost_filters=sum(
            1 for entry in prescription.filters if float(entry["gain"]) > 0.0
        ),
        composed_boost_db=driver.composed_boost_db if driver else None,
        composed_boost_role=driver.composed_boost_role if driver else None,
        max_spl_spend_bound_db=MAX_SPL_SPEND_BOUND_DB if driver else None,
        # …and what it DELETES, on the same line, because a per-driver document
        # is a total for every role it names and the incumbent filters it does
        # not repeat go away. `None` on the blend class, which replaces no
        # per-driver branch, and on a driver document whose evidence carried no
        # incumbent — an absent number rather than a zero, as above.
        displaced_filters=driver.displaced_filters if driver else None,
        displaced_boost_db=driver.displaced_boost_db if driver else None,
        displaced_boost_role=driver.displaced_boost_role if driver else None,
        replaced=replaced,
    )
    return path


def staged_prescription_pending() -> bool:
    """Is a document waiting? The stat, and only the stat.

    Named rather than left as a bare ``.is_file()`` in a dozen assertions,
    because "is one pending" is the concept the lifecycle tests are actually
    checking and a second spelling of it in each test is how the two drift.

    It has ONE production caller — ``jasper-crossover-prescriber status``,
    whose whole job is telling an operator where a speaker stands — and that
    caller gets exactly what a stat gets: yes or no, no document. That
    boundary is the point rather than an accident of scope. Widening this into
    a peek at the contents would make "consumed on the round starting, never
    reused" a convention a caller has to honour instead of a property of
    :func:`take_staged_prescription`, which remains the only reader and always
    consumes.
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

    ``accepts`` names the prescription classes THIS caller can route. It
    defaults to :data:`BLEND_ONLY` — fail-closed — so a caller that has not
    learned the per-driver class cannot be handed one; see the module docstring.

    ``None`` — no instruction, the deterministic path untouched — when no
    document is staged, which is every ordinary round.

    Raises :class:`~.blend_prescription.BlendPrescriptionRefused` when a
    document IS staged and this round may not run it, with a slug from either
    vocabulary: :data:`PRESCRIPTION_SPOOL_REFUSAL_REASONS` for a lifecycle
    refusal, or :data:`~.blend_prescription.BLEND_PRESCRIPTION_REFUSAL_REASONS`
    for a content one raised by the gate this re-runs.

    **The consume happens before the validation and that ordering is the
    contract.** A document this round refused is still a document that has had
    its round: left in the pending slot it would refuse the next round too, and
    the one after, on evidence that gets staler each time. The caller's
    obligation is therefore only to keep going without it — the failure
    direction every reader of this quantity takes, because not knowing what to
    prescribe must leave the graph alone rather than change it.

    An unreadable spool FILE (a permission error, a vanished directory) is
    reported as :data:`SPOOL_MALFORMED` rather than crashing the round: the
    caller's handling is identical either way, and a preparer that raised
    ``OSError`` out of an optional instruction would cost a household its
    session over a file it never asked for.
    """
    pending = prescription_spool_path()
    try:
        stat = pending.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _refuse(SPOOL_MALFORMED, f"the staged prescription cannot be read: {exc}")
    if stat.st_size > SPOOL_MAX_BYTES:
        # Refused on the STAT, before the bytes are read — the cap's whole job
        # is to stop a pathological file being loaded, and a cap applied after
        # the read has already paid what it exists to avoid. Consumed first for
        # the reason every other refusal here is: it has had its round.
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
        # The stat above is what stops a huge file being LOADED; this is what
        # makes the cap a property of the BYTES. A file that grew between the
        # two calls would otherwise be capped on a size it no longer had, and
        # "the number I checked is not the number I used" is the whole failure
        # mode a cap exists to close.
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

    ``os.replace`` rather than unlink so the document survives for the operator,
    and so the slot is emptied in one step a concurrent reader cannot observe
    half-done. Best-effort: a rename that fails must not turn a refusal into a
    crash, and the ordinal check still refuses a document a failed consume left
    behind, which is exactly why staleness does not rest on this call.
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
    # BEFORE the ordinal, because the class decides which anchors the rest of
    # this function is even entitled to read, and an envelope naming a class
    # this build does not stage is unreadable rather than merely mistimed.
    # Absent means the blend class: see `ENVELOPE_KIND_FIELD`.
    staged_kind = envelope.get(ENVELOPE_KIND_FIELD, PRESCRIPTION_KIND)
    # The `isinstance` is load-bearing, not decoration: `x in frozenset` HASHES
    # `x`, so a list or dict in this field raised `TypeError: unhashable type`
    # straight out of a refusal path — an unnamed exception reaching the
    # preparer as a programmer string in the wizard's 400. Caught by the
    # envelope fuzz on the first run after this field was added, which is what
    # that sweep is for.
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
    # BEFORE the document is unwrapped, because a document for another round is
    # not this round's business to judge: reporting a bound failure on evidence
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
        # encode is therefore a parse step, not a formality, and an escaping
        # `UnicodeEncodeError` reaches the preparer as a bare programmer string
        # in the wizard's 400 rather than as a named refusal. Every other
        # unreadable byte in this envelope answers `spool_malformed`; so does
        # this one.
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
    # The gate itself, re-run — same function, same bounds, same closed
    # vocabulary, one per class.
    prescription: BlendPrescription | DriverPrescription | None
    if staged_kind == DRIVER_PRESCRIPTION_KIND:
        # The class's own size bound, and this reader is the one place it can be
        # applied BEFORE the document is parsed at all: the envelope names the
        # class, so the take already knows what it is holding. The stat above
        # bounded the envelope; this bounds the document inside it.
        check_driver_document_size(payload)
        prescription = read_driver_prescription(
            read_prescription_bytes(payload),
            packet_fingerprint=envelope.get("packet_fingerprint"),
            passbands_hz=_passbands(envelope.get("passbands_hz")),
            # The round's WHOLE banked classification, as the staging gate read
            # it — see `_anchors`. Same evidence, same bar, so a document edited
            # to move a filter onto a dip refuses on the classification bar by
            # its own name rather than on a substitute; a subset that dropped
            # the dips could not, because it is the dips that say no.
            classifications=read_feature_verdicts(envelope.get("classifications")),
            # `None` for the same reason the blend arm below passes no
            # positional evidence: the packet is gone, and this argument buys
            # only a disclosure — one already made, on the line
            # `stage_prescription` logged, at the moment an operator could act
            # on it. Banking the incumbent in the envelope to re-report it here
            # would be a write with no reader.
            incumbent_filters=None,
        )
    else:
        # `positional_evidence` is None because the packet is gone and inventing
        # one would be the self-certifying read this module refuses to be: a
        # boost tampered into the document therefore refuses on
        # `insufficient_positional_evidence` before it ever reaches the route,
        # which is the correct answer rather than a lucky one.
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

    ``{}`` rather than a raise, on :func:`_band`'s rule:
    :data:`~.driver_prescription.PASSBAND_UNAVAILABLE` already owns the sentence
    for a prescription with no band to be checked against, and a second spelling
    of it here would be a second owner of the same refusal.

    The Nyquist bound :func:`_band` carries is here too and for its reason: a
    band read off disk is handed to a gate that EVALUATES biquads across it, so
    an edge this reader lets through is an edge ``chain_response`` has to
    survive. A role whose band does not clear it is DROPPED rather than
    failing the whole read, so one corrupt row cannot silently widen another
    role's — the gate then refuses that role by name.
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

    ``None`` rather than a raise here: ``read_blend_prescription`` already owns
    the sentence for a prescription with no region to be checked against
    (:data:`~.blend_prescription.REGION_UNAVAILABLE`), and a second spelling of
    it here would be a second owner of the same refusal.

    **The Nyquist bound is the guard, and it was a real escape.** A band read
    off disk is handed straight to the gate, which EVALUATES biquads across it,
    so an edge this reader lets through is an edge ``chain_response`` has to
    survive. The shipped version checked only ``0.0 < lo < hi``, which is true
    for ``(1.0, inf)`` — and an infinite upper edge reached
    ``np.geomspace(lo, inf, 512)`` and produced a math domain error, an unnamed
    exception out of a REFUSAL path, aborting session start with a programmer
    string in the wizard's 400.

    ``isfinite`` was the obvious repair and it is NOT what is written here,
    because it does not close the class: ``(1.0, 1e308)`` is finite, passes
    every ordering test, and raises the SAME error one layer down in
    ``math.cos(2πf/fs)``. The bound that does close it is the evaluator's own —
    :data:`~jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ` divided by two,
    IMPORTED rather than restated, on the rule :mod:`.blend_prescription`
    follows for the cut ceilings it takes from the deterministic solver. A
    response above half the sample rate is not a conservative refusal, it is an
    undefined quantity. It is also very loose in practice — a real crossover
    region is a few hundred to a few thousand Hz — so it constrains no
    legitimate document and exists only to keep an unnamed exception out of a
    refusal path.

    **And it subsumes ``isfinite``, which is why there is no isfinite call.** A
    guard was added here and then removed once the mutation battery showed it
    could not change an answer: ``inf`` fails ``hi <= _EVALUABLE_MAX_HZ``,
    ``-inf`` and a NaN edge fail ``0.0 < lo < hi`` (every NaN comparison is
    false), and a huge finite edge fails Nyquist. Two guards where one decides
    is not defence in depth, it is a line no test can justify. The sibling
    reader :func:`~.blend_prescription.blend_prescription_from_mapping` keeps
    its ``isfinite`` correctly: it has no Nyquist bound because it evaluates
    nothing, so there the check is the only thing standing between an infinity
    and a band nobody can use.
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


# --------------------------------------------------------------------------- #
# withdraw
# --------------------------------------------------------------------------- #


def withdraw_staged_prescription() -> bool:
    """Drop a pending prescription without running it. ``True`` if one was there.

    Undo's half of the lifecycle (#2699's rule, applied to this slot). When a
    household restores the graph a round applied, the banked blend instruction
    that round left is withdrawn, because the measuring stage would otherwise
    compose a correction onto the graph the household just removed. A staged
    prescription is the same instruction from a different author and is derived
    from the same discarded evidence, so it is withdrawn the same way.

    Unlinked rather than moved to the consumed slot: nothing consumed it, and a
    withdrawn document filed beside the taken ones would misreport which
    prescriptions ran.
    """
    try:
        prescription_spool_path().unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
