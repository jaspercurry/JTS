# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE requested angle walk, waiting for the session that will run it.

:mod:`~jasper.active_speaker.angle_capture` (#2732) turns an operator's intent into
resolved stops but deliberately stops there ("no session calls it yet"). This module is
the door: a mailbox, not a mechanism -- an operator PLACES one request, the next session
TAKES it. Everything else (program, pose, advance policy) stays :mod:`.angle_capture`'s.

A file of its own, not a key in the flow state, for the same reason
:mod:`.crossover_v2.prescription_spool` gives: the flow's durable state has exactly one
writer, and the staging CLI runs in another process as another user, so the request gets
its OWN path with its OWN owner.

No second validator at either end: staging and taking both rebuild the request through
:mod:`.angle_capture`'s own constructors. The only rule this module adds is about the
BOX, not the request: a walk may not be staged while a measurement session already holds
the speaker (:func:`live_measurement_session`).

:func:`take_staged_angle_request` is the only way to GET a staged walk; it moves the
document out of the pending slot BEFORE validating it, so "consumed on take, never
reused" is a property of the function. A refused document is consumed too (else it would
refuse every session after it), surviving at :data:`CONSUMED_SUFFIX` for the operator to
inspect.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping, NoReturn

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
    "peek_staged_angle_request",
    "set_angle_request_spool_path_for_tests",
    "stage_angle_request",
    "staged_angle_request_pending",
    "take_staged_angle_request",
    "withdraw_staged_angle_request",
]


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

#: The pending slot. At most ONE staged walk at a time by construction (a
#: session takes a request or it does not); a directory would invite a queue
#: nothing in this design knows how to order.
DEFAULT_ANGLE_REQUEST_SPOOL_PATH = Path(
    "/var/lib/jasper/active_speaker_angle_capture_request.json"
)

#: Where a taken document goes, so a refusal can be read after the fact.
CONSUMED_SUFFIX = ".consumed"

SPOOL_KIND = "jts_active_speaker_angle_capture_request_staged"

SPOOL_SCHEMA_VERSION = 1

#: A generous ceiling on the document, so a corrupt or hostile file is
#: refused by SIZE before it is parsed. :data:`MAX_STOPS` bounds the walk
#: itself; this bounds what is read off disk at all.
SPOOL_MAX_BYTES = 64 * 1024

#: How many stops one staged walk may carry: a session-length bound, not a
#: second angle validator. Each stop is one wall-clock mic position (a
#: five-angle per-driver walk is 5 stops, ~6 min); 24 leaves room for a
#: dense sweep while refusing a generated list of hundreds that would
#: outlive the session's own wall-clock ceiling.
MAX_STOPS = 24

SPOOL_MALFORMED = "angle_request_spool_malformed"

SPOOL_TOO_LARGE = "angle_request_spool_too_large"

SPOOL_TOO_MANY_STOPS = "angle_request_too_many_stops"

#: Refused because the speaker is already measuring; not a property of the
#: request (the same request is fine ten minutes later), so its own slug
#: rather than a malformed-document reason.
SESSION_ALREADY_LIVE = "measurement_session_already_live"

#: ``ANGLE_SPOOL_`` prefixed to avoid colliding with
#: :mod:`.crossover_v2.prescription_spool`'s own bare
#: ``SPOOL_REFUSAL_REASONS``; values unchanged.
ANGLE_SPOOL_REFUSAL_REASONS = frozenset({
    SPOOL_MALFORMED,
    SPOOL_TOO_LARGE,
    SPOOL_TOO_MANY_STOPS,
    SESSION_ALREADY_LIVE,
})


class AngleRequestRefused(CrossoverV2FlowError):
    """A staged walk may not be placed or taken, with a machine-readable slug. Subclasses the
    flow's own error so a caller already handling ``CrossoverV2FlowError`` keeps
    handling this one. ``reason`` is from :data:`ANGLE_SPOOL_REFUSAL_REASONS`;
    ``detail`` is the sentence a person reads.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# Moved to ``session_volume_plan`` (that question is the volume plan's, not
# angle capture's); re-exported so this module's name and ``__all__`` still work.
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
    """Point the slot somewhere writable. Tests only. Mirrors
    ``set_prescription_spool_path_for_tests``: the production path is under
    ``/var/lib/jasper``, unwritable by a hardware-free test.
    """
    global _spool_path_override
    _spool_path_override = path


def _consumed_path(pending: Path) -> Path:
    return pending.with_name(pending.name + CONSUMED_SUFFIX)


def stage_angle_request(request: AngleCaptureRequest) -> Path:
    """Bank one resolved walk for the next session to take.

    ``request`` has ALREADY passed :class:`~.angle_capture.AngleCaptureRequest`'s
    validation; this adds exactly two checks it cannot make: not longer than
    :data:`MAX_STOPS`, and no measurement session currently live.

    Staging twice is last-wins: the slot holds ONE walk, logged on overwrite
    (``event=angle_capture.request_staged`` carries ``replaced``); the atomic rename
    means a concurrent take sees one whole document, never a splice.

    Write is atomic, mode ``0o640`` with the parent's group (matching the flow state
    file), STRICT: a silent fallback to the writer's own group would publish a document
    ``jasper-web`` cannot open, surfacing as a walk that mysteriously did not run.
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
        "program": request.program,
        # Position-major and ORDERED, exactly as the request carries them: the
        # walk order is the measurement's (``both_at`` pairs regimes at one
        # angle so the microphone moves once per angle), so a set or a
        # sorted-by-angle rewrite here would silently re-plan the walk.
        "stops": [
            {
                "angle_deg": stop.angle_deg,
                "regime": stop.regime,
                "elevation_deg": stop.elevation_deg,
                "candidate_id": stop.candidate_id,
            }
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
        program=request.program,
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
    """Is a walk waiting? Named rather than a bare ``.is_file()``, since "is one pending" is
    the concept the lifecycle tests are checking. The one production caller reads it
    AFTER a refused :func:`take_staged_angle_request` to see whether the refusal
    consumed the document (the two unreadable arms deliberately do not).
    """
    return angle_request_spool_path().is_file()


def take_staged_angle_request() -> AngleCaptureRequest | None:
    """THE reader. Consumes first, then validates, and never returns a reused one.

    ``None`` means no walk staged (every ordinary session). Raises
    :class:`AngleRequestRefused` (slug from :data:`ANGLE_SPOOL_REFUSAL_REASONS`) or the
    underlying ``CrossoverV2FlowError`` when banked stops no longer satisfy
    :mod:`.angle_capture`'s contract -- deliberately not re-wrapped, since
    ``_validated_angle``'s own sentence is better.

    Move-before-validate makes single-use a property of this function: a document that
    refuses is consumed too, so a bad angle refuses once, not forever. The one exception
    -- a slot that cannot be READ at all -- is argued at its own arm below.
    """
    raw = _read_staged(consume=True)
    return None if raw is None else _validate(raw)


def peek_staged_angle_request() -> AngleCaptureRequest | None:
    """The same request :func:`take_staged_angle_request` would take, unconsumed -- for a
    caller that must STATE what is staged without running it (the tier chooser prices
    the walk before Start). A caller that wants to run the walk must not use this.
    """
    raw = _read_staged(consume=False)
    return None if raw is None else _validate(raw)


def _read_staged(*, consume: bool) -> bytes | None:
    """The staged document's bytes, or ``None`` when the slot is empty. ONE reader behind both
    doors above, so the size ceiling, refusal slugs and consume order cannot drift.
    """
    pending = angle_request_spool_path()
    try:
        stat = pending.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _refuse(SPOOL_MALFORMED, f"the staged walk could not be read: {exc}")
    if stat.st_size > SPOOL_MAX_BYTES:
        # Refused on the STAT, before the bytes are read, on a 1 GB Pi where
        # loading a hostile file in full is exactly what this avoids. A peek
        # consumes nothing -- it has not had its session yet.
        if consume:
            _consume(pending)
        _refuse(
            SPOOL_TOO_LARGE,
            f"the staged walk is {stat.st_size} bytes, over the "
            f"{SPOOL_MAX_BYTES}-byte ceiling",
        )
    try:
        raw = pending.read_bytes()
    except OSError as exc:
        # Unreadable is not absent: must not look like an ordinary session.
        # These two arms deliberately do NOT consume -- the fault is in the
        # filesystem, not the document, and a process that cannot read the
        # file almost certainly cannot rename/unlink it either; refusing
        # loudly and repeatedly preserves the evidence until fixed.
        _refuse(SPOOL_MALFORMED, f"the staged walk could not be read: {exc}")
    if consume:
        _consume(pending)
    if len(raw) > SPOOL_MAX_BYTES:
        # The stat above stops a huge file being LOADED; this makes the cap
        # a property of the BYTES (a file that grew between calls).
        _refuse(
            SPOOL_TOO_LARGE,
            f"the staged walk is {len(raw)} bytes, over the "
            f"{SPOOL_MAX_BYTES}-byte ceiling",
        )
    return raw


def _consume(pending: Path) -> None:
    """Empty the slot, atomically, and never leave it filled quietly.

    ``replace``, not ``unlink`` first, so the document survives for the operator and the
    slot empties in one step. Then ``unlink`` when the rename fails: unlike
    ``prescription_spool._consume`` (backstopped by an ordinal check), this slot has no
    ordinal analog -- a walk not stamped for a session would otherwise be re-taken by
    the next one. The unlink backstop makes single-use a property, not a hope; losing
    the document is the right trade against silently re-running a measurement.

    Still best-effort at the end (a failed consume must not turn a walk that WAS read
    into a refusal), but **logged at WARNING**, since a slot that will not clear can
    re-run a session's worth of captures.
    """
    try:
        pending.replace(_consumed_path(pending))
        return
    except OSError as exc:
        # Bound INSIDE the block: Python deletes the `as` name on exit, so
        # reading it after would be UnboundLocalError.
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


def _coerced_delay_us(raw: Any) -> float:
    """``delay_us`` as a number, or the spool's own refusal naming the field."""
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        _refuse(
            SPOOL_MALFORMED,
            f"the staged walk's delay_us is not a number: {raw!r}",
        )
def _validate(raw: bytes) -> AngleCaptureRequest:
    """Rebuild the request from the banked fields, through its own constructors.

    Every angle, regime and mover check is :mod:`.angle_capture`'s; this checks only the
    document's own shape (JSON, kind, schema, an ordered stop list) before handing
    values straight to
    :class:`~.angle_capture.AngleStop`/:class:`~.angle_capture.AngleCaptureRequest`.

    Angles are handed over UNCOERCED, same rule as ``per_driver_at``: an ``int()`` here
    would truncate ``0.4`` to an on-axis capture nobody asked for. R-1's two pairs
    (delay, polarity) are ADDITIVE and defaulted, so a document spooled before either
    existed still reads as a normal walk; neither is judged here -- ``MeasureSpec``
    judges them when the host adopts the walk. ``delay_us`` is the one field COERCED
    through ``float``, refusing as :data:`SPOOL_MALFORMED` rather than a bare
    ``ValueError`` (the page's price peek catches only ``CrossoverV2FlowError``).
    ``level_matched`` is a BOOLEAN, never numbers.
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
            AngleStop(
                entry.get("angle_deg"),  # type: ignore[arg-type]
                str(entry.get("regime")),
                # Pre-existing documents are a walk at mark height as-is.
                entry.get("elevation_deg", 0),  # type: ignore[arg-type]
                str(entry.get("candidate_id") or ""),
            )
        )
    return AngleCaptureRequest(
        stops=tuple(stops),
        mover=str(doc.get("mover")),
        polarity=str(doc.get("polarity") or POLARITY_NORMAL),
        inverted_role=str(doc.get("inverted_role") or ""),
        delayed_role=str(doc.get("delayed_role") or ""),
        delay_us=_coerced_delay_us(doc.get("delay_us")),
        level_matched=bool(doc.get("level_matched")),
        program=str(doc.get("program") or ""),
    )


def withdraw_staged_angle_request() -> bool:
    """Remove a pending walk without running it. ``True`` if one was there. Does NOT write a
    ``.consumed`` copy: a withdrawn walk was never handed to a session, so filing it
    beside ones that were would make the consumed slot ambiguous.
    """
    pending = angle_request_spool_path()
    try:
        pending.unlink()
    except FileNotFoundError:
        return False
    log_event(logger, "angle_capture.request_withdrawn")
    return True
