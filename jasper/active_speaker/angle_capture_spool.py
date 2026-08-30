# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE requested angle walk, waiting for the session that will run it.

:mod:`~jasper.active_speaker.angle_capture` (#2732) turns an operator's
intent -- ``{per-driver | summed} x {angles} x {arm | human-guided}`` -- into
resolved stops over the shipped program, pose and gate machinery.  It
deliberately stops there, and its own scope note says so: *"No session calls it
yet."*  A request had no durable home and no door.

**This module is the door, and it is a mailbox rather than a mechanism.**  An
operator PLACES one request; the next session TAKES it.  Everything else --
which program each stop plays, what pose an angle names, which advance policy a
mover implies -- is :mod:`.angle_capture`'s, and is deliberately not duplicated
here.

**Why a file of its own rather than a key in the flow state.**  The same reason
:mod:`.crossover_v2.prescription_spool` gives, and the same shape: the flow's
durable state (``correction_crossover_v2.DEFAULT_V2_STATE_PATH``) has exactly
one writer, ``save_v2_state``, and every one of its callers lives in that one
web module -- a rule a test names writer-by-writer.  The staging step runs from
a CLI, in another process, as another user.  Teaching that CLI to write the flow
state would make it a second writer of the one file whose single-writer property
the whole flow rests on.  So the request gets its OWN path with its OWN owner.

**No second validator, at either end.**  Staging parses the operator's angles
through :mod:`.angle_capture`'s own constructors and stores what they returned;
the take rebuilds the request through those same constructors from the banked
fields.  A malformed angle is refused by ``_validated_angle`` and an unknown
regime or mover by ``AngleStop`` / ``AngleCaptureRequest``, in both directions.
The only rule this module adds is the one :mod:`.angle_capture` cannot know
about because it is about the BOX rather than the request: a walk may not be
staged while a measurement session already holds the speaker (see
:func:`live_measurement_session`).

**What "taking" means, and why reading and consuming are one call.**  There is
no way to read a staged request without consuming it.
:func:`take_staged_angle_request` moves the document out of the pending slot
BEFORE it validates it, so "consumed on the session starting, never reused" is a
property of the function rather than of a caller remembering to clean up -- and
a refused document is consumed too, because a document that was wrong for this
session would otherwise refuse every session after it as well.  The consumed
document survives at :data:`CONSUMED_SUFFIX` for the operator to look at.

Both of those paragraphs are lifted in shape, not in code, from
:mod:`.crossover_v2.prescription_spool`, which landed on 2026-08-19 for the same
operator (a conductor over SSH), the same constraint (single-writer durable
state, another process) and the same lifecycle (place once, take once).  Sharing
its *reasoning* while keeping a separate slot is deliberate: the two documents
are consumed by different steps of a session and a queue that ordered them would
be a mechanism neither design has.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Mapping, NoReturn

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

from .angle_capture import AngleCaptureRequest, AngleStop
from .crossover_v2.contracts import POLARITY_NORMAL
from .crossover_v2_flow import CrossoverV2FlowError

logger = logging.getLogger(__name__)

__all__ = [
    "CONSUMED_SUFFIX",
    "DEFAULT_ANGLE_REQUEST_SPOOL_PATH",
    "MAX_STOPS",
    "SPOOL_KIND",
    "SPOOL_MALFORMED",
    "SPOOL_MAX_BYTES",
    "SPOOL_SCHEMA_VERSION",
    "SPOOL_TOO_LARGE",
    "SESSION_ALREADY_LIVE",
    "AngleRequestRefused",
    "angle_request_spool_path",
    "live_measurement_session",
    "set_angle_request_spool_path_for_tests",
    "stage_angle_request",
    "staged_angle_request_pending",
    "take_staged_angle_request",
    "withdraw_staged_angle_request",
]


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

#: The pending slot. A sibling of the flow state file and of the prescription
#: spool rather than a directory of its own: there is at most ONE staged walk at
#: a time by construction (a session takes a request or it does not), and a
#: directory would invite a queue nothing in this design knows how to order.
DEFAULT_ANGLE_REQUEST_SPOOL_PATH = Path(
    "/var/lib/jasper/active_speaker_angle_capture_request.json"
)

#: Where a taken document goes, so a refusal can be read after the fact.
CONSUMED_SUFFIX = ".consumed"

SPOOL_KIND = "jts_active_speaker_angle_capture_request_staged"

SPOOL_SCHEMA_VERSION = 1

#: A generous ceiling on the document, so a corrupt or hostile file is refused by
#: SIZE before it is parsed. :data:`MAX_STOPS` bounds the walk itself; this
#: bounds what is read off disk at all, which is the earlier question.
SPOOL_MAX_BYTES = 64 * 1024

#: How many stops one staged walk may carry.
#:
#: **This is a session-length bound, not a second angle validator.** Each stop is
#: one microphone position at one program, so a walk is a wall-clock commitment:
#: the campaign's own five-angle per-driver walk is 5 stops against a ~6 min
#: projection, and ``both_at`` doubles the stop count for the same angles. 24
#: leaves room for a dense sweep (every 7 deg across the accepted domain, both
#: regimes) while refusing the shape that is always a mistake -- a generated list
#: of hundreds that would outlive the session's own wall-clock ceiling and be
#: discovered as a timeout rather than as a refusal.
MAX_STOPS = 24

SPOOL_MALFORMED = "angle_request_spool_malformed"

SPOOL_TOO_LARGE = "angle_request_spool_too_large"

SPOOL_TOO_MANY_STOPS = "angle_request_too_many_stops"

#: Refused because the speaker is already measuring. Not a property of the
#: request -- the same request is fine ten minutes later -- which is why it is
#: its own slug rather than a malformed-document reason.
SESSION_ALREADY_LIVE = "measurement_session_already_live"

#: ``ANGLE_SPOOL_`` prefixed: an unprefixed ``SPOOL_REFUSAL_REASONS`` would
#: collide with :mod:`.crossover_v2.prescription_spool`'s own bare
#: ``SPOOL_REFUSAL_REASONS`` -- two different closed vocabularies for two
#: different mailboxes, sharing one name. Values are unchanged; only the
#: Python identifier moved.
ANGLE_SPOOL_REFUSAL_REASONS = frozenset({
    SPOOL_MALFORMED,
    SPOOL_TOO_LARGE,
    SPOOL_TOO_MANY_STOPS,
    SESSION_ALREADY_LIVE,
})


class AngleRequestRefused(CrossoverV2FlowError):
    """A staged walk may not be placed or taken, with a machine-readable slug.

    Subclasses the flow's own error so a caller that already handles
    ``CrossoverV2FlowError`` -- which is every caller of
    :mod:`.angle_capture`'s constructors -- keeps handling this one. ``reason``
    is from :data:`ANGLE_SPOOL_REFUSAL_REASONS`; ``detail`` is the sentence a
    person reads.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# ``live_measurement_session`` moved to ``session_volume_plan`` when the
# seat-SPL leveling door became its second consumer: the question it answers
# is the volume plan's, not angle capture's. Re-exported so this module's
# own name for it (and ``__all__``) keeps working.
from .session_volume_plan import (  # noqa: E402
    live_measurement_session as live_measurement_session,
)


def _refuse(reason: str, detail: str) -> NoReturn:
    raise AngleRequestRefused(reason, detail)


_spool_path_override: Path | None = None


def angle_request_spool_path() -> Path:
    """The pending slot, honoring a test override."""
    return _spool_path_override or DEFAULT_ANGLE_REQUEST_SPOOL_PATH


def set_angle_request_spool_path_for_tests(path: Path | None) -> None:
    """Point the slot somewhere writable. Tests only.

    Mirrors ``set_prescription_spool_path_for_tests``: the production path is
    under ``/var/lib/jasper``, which a hardware-free test cannot write, and
    threading a path through every function would put an override in a
    production signature for a test's benefit.
    """
    global _spool_path_override
    _spool_path_override = path


def _consumed_path(pending: Path) -> Path:
    return pending.with_name(pending.name + CONSUMED_SUFFIX)


# --------------------------------------------------------------------------- #
# the one rule this module owns: is the speaker already measuring?
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# place
# --------------------------------------------------------------------------- #


def stage_angle_request(request: AngleCaptureRequest) -> Path:
    """Bank one resolved walk for the next session to take.

    ``request`` is an :class:`~.angle_capture.AngleCaptureRequest`, which means
    it has ALREADY passed that module's validation -- there is no way to
    construct one that has not. This function adds exactly two checks it can
    make and that module cannot: the walk is not longer than :data:`MAX_STOPS`,
    and no measurement session is currently live.

    **Staging twice is last-wins**, for the reason ``stage_prescription`` gives
    for its own slot: the slot holds ONE walk because a session takes one, and
    an operator who re-states the angles after looking at the plan means the
    second walk. The overwrite is logged
    (``event=angle_capture.request_staged`` carries ``replaced``) so it is never
    silent, and the atomic rename means a concurrent take sees one whole
    document or the other, never a splice.

    The write is atomic and mode ``0o640`` with the parent's group, matching the
    flow state file and the prescription spool it sits beside, and for the same
    reason: the operator's CLI writes it and the web user reads it. The group
    assignment is STRICT -- a write that silently fell back to the writer's own
    group would publish a document ``jasper-web`` cannot open, and the failure
    would surface as a walk that mysteriously did not run.
    """
    if len(request.stops) > MAX_STOPS:
        _refuse(
            SPOOL_TOO_MANY_STOPS,
            f"a staged walk may carry at most {MAX_STOPS} stops, got "
            f"{len(request.stops)}",
        )
    busy = live_measurement_session()
    if busy is not None:
        _refuse(SESSION_ALREADY_LIVE, busy)

    path = angle_request_spool_path()
    replaced = path.is_file()
    payload = {
        "artifact_schema_version": SPOOL_SCHEMA_VERSION,
        "kind": SPOOL_KIND,
        "mover": request.mover,
        # Walk-level, beside ``mover``, because the reverse-null is one act at
        # one place -- see :class:`~.angle_capture.AngleCaptureRequest`. Written
        # unconditionally and read back with a default, so a document staged
        # before these keys existed still reads as a normal-polarity walk and
        # the schema version does not move.
        "polarity": request.polarity,
        "inverted_role": request.inverted_role,
        "delayed_role": request.delayed_role,
        "delay_us": request.delay_us,
        "level_matched": request.level_matched,
        # Position-major and ORDERED, exactly as the request carries them: the
        # walk order is the measurement's (``both_at`` pairs regimes at one
        # angle so the microphone moves once per angle), so a set or a
        # sorted-by-angle rewrite here would silently re-plan the walk.
        "stops": [
            {"angle_deg": stop.angle_deg, "regime": stop.regime}
            for stop in request.stops
        ],
        "staged_at": time.time(),
    }
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        mode=0o640,
        group_from_parent=True,
        durable=True,
    )
    log_event(
        logger, "angle_capture.request_staged",
        stops=len(request.stops),
        mover=request.mover,
        regimes=",".join(sorted({stop.regime for stop in request.stops})),
        polarity=request.polarity,
        inverted_role=request.inverted_role,
        delayed_role=request.delayed_role,
        delay_us=request.delay_us,
        level_matched=request.level_matched,
        replaced=replaced,
    )
    return path


def staged_angle_request_pending() -> bool:
    """Is a walk waiting?

    Named rather than left as a bare ``.is_file()`` in a dozen assertions,
    because "is one pending" is the concept the lifecycle tests are actually
    checking and a second spelling of it in each test is how the two drift.

    Its one production caller reads it AFTER a refused
    :func:`take_staged_angle_request` to find out whether that refusal consumed
    the document — the two unreadable arms deliberately do not — so a journal
    line can state what happened instead of asserting it.

    It answers what a stat answers and no more: anything that wants the request
    itself goes through :func:`take_staged_angle_request`, which is the only
    reader and always consumes what it could read.
    """
    return angle_request_spool_path().is_file()


# --------------------------------------------------------------------------- #
# take
# --------------------------------------------------------------------------- #


def take_staged_angle_request() -> AngleCaptureRequest | None:
    """THE reader. Consumes first, then validates, and never returns a reused one.

    ``None`` -- no walk staged, the session's ordinary shape untouched -- which
    is every ordinary session.

    Raises :class:`AngleRequestRefused` when a document IS staged and cannot be
    run, with a slug from :data:`ANGLE_SPOOL_REFUSAL_REASONS`, or the underlying
    ``CrossoverV2FlowError`` when the banked stops no longer satisfy
    :mod:`.angle_capture`'s own contract (an angle edited out of bounds, an
    unknown regime, an unknown mover). **That second class is deliberately not
    re-wrapped**: the sentence ``_validated_angle`` writes is better than
    anything this module could say about the same byte, and re-wrapping it would
    put a second vocabulary over one fact.

    The move-before-validate order is what makes single-use a property of this
    function: a document that refuses is consumed too, so a walk staged with a
    bad angle refuses once rather than refusing every session until someone
    deletes it by hand. The one exception -- a slot that cannot be READ at all,
    where there is no document to consume -- is argued at its own arm below.
    """
    pending = angle_request_spool_path()
    try:
        stat = pending.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _refuse(SPOOL_MALFORMED, f"the staged walk could not be read: {exc}")
    if stat.st_size > SPOOL_MAX_BYTES:
        # Refused on the STAT, before the bytes are read. **A cap applied after
        # the read has already paid what it exists to avoid** -- and this runs
        # on a 1 GB Pi, at a gate whose whole purpose is to fail closed, so a
        # pathological or hostile file would otherwise be loaded into memory in
        # full precisely when the system can least afford it. The size reported
        # is ``st_size`` because that is the number this arm actually judged.
        # Consumed first, for the reason every other document refusal here is:
        # it has had its session.
        _consume(pending)
        _refuse(
            SPOOL_TOO_LARGE,
            f"the staged walk is {stat.st_size} bytes, over the "
            f"{SPOOL_MAX_BYTES}-byte ceiling",
        )
    try:
        raw = pending.read_bytes()
    except OSError as exc:
        # Unreadable is not absent: an unreadable slot must not look like an
        # ordinary session, or a permissions mistake becomes a walk that
        # silently never runs.
        #
        # **The two unreadable arms are the only refusals that do NOT consume,
        # and that is deliberate rather than an oversight of the rule below.**
        # Consuming exists so a bad DOCUMENT refuses once instead of every
        # session; these arms have no document -- the bytes were never read --
        # and the fault is in the filesystem, not in what somebody staged. A
        # process that cannot read the file almost certainly cannot rename or
        # unlink it either, and a best-effort removal that happened to succeed
        # would destroy the only evidence of a permissions mistake while leaving
        # the operator with a walk that never ran and no file to look at. So
        # these refuse loudly and repeatedly until the permissions are fixed.
        _refuse(SPOOL_MALFORMED, f"the staged walk could not be read: {exc}")
    _consume(pending)
    if len(raw) > SPOOL_MAX_BYTES:
        # The stat above is what stops a huge file being LOADED; this is what
        # makes the cap a property of the BYTES. A file that grew between the
        # two calls would otherwise be capped on a size it no longer had, and
        # "the number I checked is not the number I used" is the whole failure
        # mode a cap exists to close.
        _refuse(
            SPOOL_TOO_LARGE,
            f"the staged walk is {len(raw)} bytes, over the "
            f"{SPOOL_MAX_BYTES}-byte ceiling",
        )
    return _validate(raw)


def _consume(pending: Path) -> None:
    """Empty the slot, atomically, and never leave it filled quietly.

    ``replace`` rather than ``unlink`` first, so the document survives for the
    operator and the slot is emptied in one step a concurrent reader cannot
    observe half-done.

    **Then ``unlink`` when the rename fails, and this spool needs that fallback
    more than the pattern it is modelled on.** ``prescription_spool._consume``
    can afford a purely best-effort consume because a document its rename left
    behind is refused anyway by the ordinal check —
    ``prescription_not_staged_for_this_round`` — so single-use there does not
    rest on the rename at all. **This slot has no ordinal analog**: a walk is
    not stamped for a particular session, so if the slot is still full the next
    session takes the same walk again. Single-use would rest entirely on
    ``replace`` succeeding. The unlink is therefore the backstop that makes
    single-use a property rather than a hope, and losing the document to look at
    is the right trade against silently re-running a measurement.

    Still best-effort at the end: a failed consume must not turn a walk that WAS
    read into a refusal. But unlike the reference it is **logged at WARNING**,
    because with no ordinal check behind it a slot that will not clear is the
    one condition that can re-run a session's worth of captures, and nothing
    else in this design would say so.
    """
    try:
        pending.replace(_consumed_path(pending))
        return
    except OSError as exc:
        # Bound to a plain local INSIDE the block on purpose: Python deletes
        # the ``as`` name when the except block exits, so reading it after the
        # block is an ``UnboundLocalError`` on the one path that only runs when
        # something has already gone wrong.
        replace_error = str(exc)
    try:
        pending.unlink()
    except OSError as exc:
        log_event(
            logger, "angle_capture.request_consume_failed",
            level=logging.WARNING,
            error=replace_error, unlink_error=str(exc),
        )
        return
    log_event(
        logger, "angle_capture.request_consume_unlinked",
        level=logging.WARNING, error=replace_error,
    )


def _validate(raw: bytes) -> AngleCaptureRequest:
    """Rebuild the request from the banked fields, through its own constructors.

    Every angle, regime and mover check is :mod:`.angle_capture`'s. What this
    function checks is the document's own shape -- that it is JSON, that it is
    this kind and schema, that it has an ordered list of stops -- and then hands
    the values straight to :class:`~.angle_capture.AngleStop` and
    :class:`~.angle_capture.AngleCaptureRequest`.

    **The angles are handed over UNCOERCED**, which is the same rule
    ``per_driver_at`` follows and for the same reason: ``json.loads`` turns
    ``7.0`` into a ``float`` and ``"7"`` into a ``str``, and an ``int()`` here
    would truncate ``0.4`` to an on-axis capture the operator never asked for
    (see ``_validated_angle``). A document whose angles are not whole numbers is
    refused by the constructor, in the constructor's own words.

    **R-1's two pairs are ADDITIVE and defaulted** — the delay coordinate reads
    back exactly as the polarity pair does, so a document spooled before either
    existed still reads as a normal, undelayed walk. Neither is judged here.

    ``level_matched`` reads back on the same terms and is a BOOLEAN, never
    numbers: an absent (or false) key is a walk whose graph carries no level
    match, and the trims a true one asks for are the box's own, resolved when
    the host adopts the walk.

    **The polarity pair is ADDITIVE and defaulted**, which is what keeps a
    document staged before it existed valid at the same schema version: an
    absent (or null) ``polarity`` is a normal-polarity walk, and an absent
    ``inverted_role`` names no branch. Neither is checked here -- the pair is
    judged by ``MeasureSpec`` when the host adopts the walk, in the spec's own
    words, for the reason :class:`~.angle_capture.AngleCaptureRequest` records.
    """
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _refuse(SPOOL_MALFORMED, f"the staged walk is not valid JSON: {exc}")
    if not isinstance(doc, Mapping):
        _refuse(SPOOL_MALFORMED, "the staged walk is not a JSON object")
    if doc.get("kind") != SPOOL_KIND:
        _refuse(
            SPOOL_MALFORMED,
            f"the staged walk is not a {SPOOL_KIND} document",
        )
    if doc.get("artifact_schema_version") != SPOOL_SCHEMA_VERSION:
        _refuse(
            SPOOL_MALFORMED,
            "the staged walk is schema version "
            f"{doc.get('artifact_schema_version')!r}, expected "
            f"{SPOOL_SCHEMA_VERSION}",
        )
    stops_raw = doc.get("stops")
    if not isinstance(stops_raw, list) or not stops_raw:
        _refuse(SPOOL_MALFORMED, "the staged walk carries no stops")
    if len(stops_raw) > MAX_STOPS:
        _refuse(
            SPOOL_TOO_MANY_STOPS,
            f"a staged walk may carry at most {MAX_STOPS} stops, got "
            f"{len(stops_raw)}",
        )
    stops: list[AngleStop] = []
    for entry in stops_raw:
        if not isinstance(entry, Mapping):
            _refuse(SPOOL_MALFORMED, "a staged stop is not a JSON object")
        stops.append(
            AngleStop(entry.get("angle_deg"), str(entry.get("regime")))  # type: ignore[arg-type]
        )
    return AngleCaptureRequest(
        stops=tuple(stops),
        mover=str(doc.get("mover")),
        polarity=str(doc.get("polarity") or POLARITY_NORMAL),
        inverted_role=str(doc.get("inverted_role") or ""),
        delayed_role=str(doc.get("delayed_role") or ""),
        delay_us=float(doc.get("delay_us") or 0.0),
        level_matched=bool(doc.get("level_matched")),
    )


def withdraw_staged_angle_request() -> bool:
    """Remove a pending walk without running it. ``True`` if one was there.

    The operator's own undo, and the only way to clear the slot that is not a
    take. It does NOT write a ``.consumed`` copy: a withdrawn walk was never
    handed to a session, so filing it beside the ones that were would make the
    consumed slot ambiguous about what actually ran.
    """
    pending = angle_request_spool_path()
    try:
        pending.unlink()
    except FileNotFoundError:
        return False
    log_event(logger, "angle_capture.request_withdrawn")
    return True
