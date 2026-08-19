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
region it was checked against, add a third filter, add a prohibited key, or turn
a cut into a boost — every one of which is refused by name.  What it cannot do
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

**Cuts only.**  The route has not changed: a boost still has no seam to land in
and :func:`~.blend_prescription.prescription_route` still refuses one.  This
module opens no route; it carries the class the seam already accepts.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event
from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

from .blend_prescription import (
    PRESCRIPTION_MAX_BYTES,
    BlendPrescription,
    BlendPrescriptionRefused,
    prescription_sha256,
    read_blend_prescription,
    read_prescription_bytes,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONSUMED_SUFFIX",
    "DEFAULT_PRESCRIPTION_SPOOL_PATH",
    "PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND",
    "SPOOL_KIND",
    "SPOOL_MALFORMED",
    "SPOOL_MAX_BYTES",
    "SPOOL_REFUSAL_REASONS",
    "SPOOL_SCHEMA_VERSION",
    "SPOOL_TOO_LARGE",
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
SPOOL_SCHEMA_VERSION = 1

#: Byte ceiling on the envelope FILE, read before it is parsed.
#:
#: Derived from the document cap this module does not own and does not restate.
#: The document is stored verbatim as a JSON string, and JSON string escaping is
#: at worst six characters per byte (``\uXXXX`` for a control character), so an
#: envelope wrapping a document at :data:`.PRESCRIPTION_MAX_BYTES` cannot exceed
#: six times it plus a handful of flat fields. Eight is that bound rounded up.
#: The document's own cap is re-applied by its owner —
#: :func:`~.blend_prescription.read_prescription_bytes` — when the take unwraps
#: it, so there is exactly one number policing a prescription's size and it is
#: not this one.
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

#: The lifecycle vocabulary, beside rather than inside
#: :data:`~.blend_prescription.PRESCRIPTION_REFUSAL_REASONS`.
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
SPOOL_REFUSAL_REASONS = frozenset({
    SPOOL_MALFORMED,
    SPOOL_TOO_LARGE,
    PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND,
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
    """One validated prescription, and the two facts the round needs about it."""

    #: Re-validated at take time, never rehydrated from the banked class.
    prescription: BlendPrescription
    #: The digest of the document bytes, carried from staging and re-proved
    #: against the stored document before this object exists.
    prescription_sha256: str
    #: The round this was staged for, and the round that took it — equal by
    #: construction, since a mismatch refuses instead of returning.
    for_round_ordinal: int

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


def stage_prescription(
    document: bytes,
    prescription: BlendPrescription,
    *,
    for_round_ordinal: int,
) -> Path:
    """Bank one ALREADY-VALIDATED prescription for the next round.

    ``prescription`` must be the object
    :func:`~.blend_prescription.read_blend_prescription` returned for
    ``document`` against the round's real evidence packet. This function does
    not re-run that gate and deliberately cannot: it has no packet, and a
    staging step that validated with anything weaker than the packet would be
    the second, laxer reader this design exists to avoid.

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
    payload = {
        "artifact_schema_version": SPOOL_SCHEMA_VERSION,
        "kind": SPOOL_KIND,
        "for_round_ordinal": int(for_round_ordinal),
        # The two anchors the take cannot re-derive, from the step that could.
        "packet_fingerprint": prescription.packet_fingerprint,
        "band_hz": [prescription.band_hz[0], prescription.band_hz[1]],
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
        replaced=replaced,
    )
    return path


def staged_prescription_pending() -> bool:
    """Is a document waiting? A TEST predicate, with no production caller.

    Named rather than left as a bare ``.is_file()`` in a dozen assertions,
    because "is one pending" is the concept the lifecycle tests are actually
    checking and a second spelling of it in each test is how the two drift. It
    is deliberately NOT offered as a surface for a screen or a status line: no
    such caller exists, and inventing one here would be the speculative
    flexibility this repository trims.

    It answers what a stat answers and no more. Anything that wants the
    prescription itself goes through :func:`take_staged_prescription`, which is
    the only reader and always consumes.
    """
    return prescription_spool_path().is_file()


# --------------------------------------------------------------------------- #
# take
# --------------------------------------------------------------------------- #


def take_staged_prescription(*, round_ordinal: int) -> StagedPrescription | None:
    """THE reader. Consumes first, then validates, and never returns a reused one.

    ``None`` — no instruction, the deterministic path untouched — when no
    document is staged, which is every ordinary round.

    Raises :class:`~.blend_prescription.BlendPrescriptionRefused` when a
    document IS staged and this round may not run it, with a slug from either
    vocabulary: :data:`SPOOL_REFUSAL_REASONS` for a lifecycle refusal, or
    :data:`~.blend_prescription.PRESCRIPTION_REFUSAL_REASONS` for a content one
    raised by the gate this re-runs.

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
    return _validate(raw, round_ordinal=round_ordinal)


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


def _validate(raw: bytes, *, round_ordinal: int) -> StagedPrescription:
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
    band = _band(envelope.get("band_hz"))
    # The gate itself, re-run — same function, same bounds, same closed
    # vocabulary. `positional_evidence` is None because the packet is gone and
    # inventing one would be the self-certifying read this module refuses to be:
    # a boost tampered into the document therefore refuses on
    # `insufficient_positional_evidence` before it ever reaches the route, which
    # is the correct answer rather than a lucky one.
    prescription = read_blend_prescription(
        read_prescription_bytes(payload),
        packet_fingerprint=envelope.get("packet_fingerprint"),
        band_hz=band,
        positional_evidence=None,
    )
    if prescription is None:  # pragma: no cover - `read_prescription_bytes` refuses
        _refuse(SPOOL_MALFORMED, "the staged prescription document was empty")
    return StagedPrescription(
        prescription=prescription,
        prescription_sha256=actual,
        for_round_ordinal=staged_for,
    )


def _band(raw: Any) -> tuple[float, float] | None:
    """The banked region, or ``None`` so the gate refuses it by name.

    ``None`` rather than a raise here: ``read_blend_prescription`` already owns
    the sentence for a prescription with no region to be checked against
    (:data:`~.blend_prescription.REGION_UNAVAILABLE`), and a second spelling of
    it here would be a second owner of the same refusal.

    **Two guards, and both were escapes rather than tidiness.** A band read off
    disk is handed straight to the gate, which EVALUATES biquads across it —
    so an edge this reader lets through is an edge ``chain_response`` has to
    survive, and it does not survive either of these:

    * ``isfinite``. ``0.0 < lo < hi`` is true for ``(1.0, inf)``, and a NaN edge
      is false everywhere so it escaped as ``None`` by luck rather than by rule.
      An infinite upper edge reached ``np.geomspace(lo, inf, 512)`` and produced
      a math domain error. The stricter of the two readers of this shape,
      :func:`~.blend_prescription.blend_prescription_from_mapping`, already
      guarded exactly this and this one had copied the laxer; they now agree
      instead of differing by which reader you happened to reach.
    * **Nyquist.** ``isfinite`` alone is not enough: ``(1.0, 1e308)`` is finite,
      passes every ordering test, and raises the SAME math domain error one
      layer down, in ``math.cos(2πf/fs)``. The bound is the evaluator's own —
      :data:`~jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ` divided by two,
      IMPORTED rather than restated, on the rule the cut ceilings in
      :mod:`.blend_prescription` follow. A response above half the sample rate
      is not a conservative refusal, it is an undefined quantity. It is also a
      very loose bound in practice — a real crossover region is a few hundred to
      a few thousand Hz — so it constrains no legitimate document and exists
      only to keep an unnamed exception out of a refusal path.

    Both edges are checked, not just the upper one: a band sitting entirely
    above Nyquist is the same nonsense arriving in a different order.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(lo) and math.isfinite(hi)):
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
