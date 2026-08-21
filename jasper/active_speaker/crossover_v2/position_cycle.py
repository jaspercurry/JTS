# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""N takes at ONE pose: how they are staged, and how they read back.

A staged arm walk is an ORDERED list of stops, and two stops at the same angle
are two ADJACENT stops rather than two walks — the microphone moves once per
angle.  That is not a new property: :func:`.angle_capture.both_at` already ships
it ("Both regimes at each angle, PAIRED so the microphone moves once per
angle"), and :class:`~jasper.active_speaker.angle_capture.AngleCaptureRequest`
holds its stops as an ordered tuple with no uniqueness rule.  So *taking N
captures at one pose in one walk* needs no new machinery on the speaker at all —
it needs the staged list to say so.

Two halves, and nothing else:

* :func:`expand_angle_spec` / :func:`staged_stops` — the staged list, with each
  angle repeated N times adjacently, and how many stops that is.
* :func:`position_cycle_document` / :func:`read_position_cycle` — the index a
  round banks beside its evidence, DERIVED from that evidence, and its strict
  reader.

**Why the expansion is a STRING transform.**  The angle vocabulary has exactly
one validator (:func:`~jasper.active_speaker.angle_capture._validated_angle`,
reached through ``jasper-angle-capture stage``), and its whole point is that
nothing coerces on the way there — ``0.4`` truncating to ``0`` turns a pose just
off the axis into an on-axis capture.  A laptop-side expansion that parsed the
angles to repeat them would be a second reader of that vocabulary, and the day
it disagreed it would disagree silently.  So each comma-separated token is
repeated VERBATIM, and whatever the operator wrote still reaches the one
validator that judges it.  Empty fields are DROPPED rather than refused, which
is :func:`jasper.cli.angle_capture._parse_angles`' own rule (``for field in
raw.split(",") if field.strip()``) — a trailing comma is tolerated there, and a
laptop that refused it would be that second, stricter reader by another route.

**The index is DERIVED, never authored — this file writes down no fact of its
own.**  The speaker already stamps the true pose on every accepted take:
:func:`~jasper.active_speaker.crossover_v2.spatial.lateral_pose_record` carries
the signed ``position_deg``, the ``index``/``attempt``/``take_id`` identity, the
``role``, the ``regime`` and the ``wav_sha256`` verifier;
``crossover_v2_flow._retain_lateral_pose`` hands it to retention; the web host
publishes it as ``crossover_v2/{session}/positions/{take_id}.json`` under the
evidence bundle; and ``bank-crossover-round.sh`` tars that whole bundle tree into
the round directory.  A laptop-written mapping would therefore be a SECOND
writer of one fact — the runner's *intent* beside the speaker's *record* — and
the two disagree exactly when it matters (a rejected take, a retake, a walk the
session refused at take time and ran its ordinary shape instead).

So :func:`position_cycle_document` reads those banked records and projects them.
Every value in the document it returns can be found in
``bundle/*/crossover_v2/*/positions/*.json``; nothing in it is computed from what
the round MEANT to stage.  What the document adds is convenience: one sorted
index at the round root, instead of a glob over a nested per-take tree that also
holds the cloud group's positions.  When the banked evidence cannot support the
index, this refuses and names exactly what was missing — it never falls back to
intent.

**Why an index is worth having at all.**  The pose IS banked; nothing SURFACES
it.  ``round_views`` and the evidence packet read the *cloud* positions block,
so a lateral walk's bearings — the numbers a take-per-pose comparison is made of
— are on disk in per-take sidecars that no view opens.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .journey import PHASE_LATERAL

#: The index's own name, so a reader that finds this document anywhere knows
#: what it is holding without knowing which tool wrote it.
POSITION_CYCLE_KIND = "jts_crossover_v2_position_cycle"
SCHEMA_VERSION = 1

#: The file a round banks it as, inside the round directory.
POSITION_CYCLE_FILENAME = "position_cycle.json"

#: Where ``bank-crossover-round.sh`` untars the evidence bundle, and where the
#: web host publishes one JSON record per accepted take inside it. Stated as a
#: glob because both the session id and the relay session id are minted at run
#: time.
_BANKED_POSITIONS_GLOB = "bundle/*/crossover_v2/*/positions/*.json"

#: ``kind`` on the speaker's own per-take record
#: (``correction_crossover_v2``'s ``retain_position``). Records that do not
#: carry it are not this document's input, whatever else is in the directory.
POSITION_EVIDENCE_KIND = "jts_crossover_v2_position_evidence"

#: What each take contributes to the index — the identity, the pose, and the
#: verifier. Every one is a field ``lateral_pose_record`` writes; the banked
#: record stays the place to go for the rest (``offset_cm``, ``at_mark``,
#: ``prompt``, ``lateral_consumer``), which is why the document names its
#: ``sources``.
_TAKE_FIELDS = ("index", "attempt", "take_id", "position_deg", "role",
                "regime", "wav_sha256")

#: The keys :func:`read_position_cycle` accepts. Strict in both directions: a
#: key this module does not know is either a newer schema or a hand edit, and
#: both are worth an error over a silent drop.
_DOCUMENT_FIELDS = frozenset({
    "kind", "schema_version", "derived_at", "sources", "takes",
})


class PositionCycleError(ValueError):
    """The index cannot be derived, or cannot be read."""


# --------------------------------------------------------------------------- #
# staging — N stops at one angle
# --------------------------------------------------------------------------- #


def expand_angle_spec(angles: str, per_position: int) -> str:
    """``"0,7"`` at 3 takes -> ``"0,0,0,7,7,7"`` — tokens repeated VERBATIM.

    Adjacent rather than interleaved, because the whole value of N takes at one
    pose is that nothing moved between them: ``0,7,0,7,0,7`` would walk the arm
    six times and measure the drift this exists to hold still.

    ``per_position=1`` returns the surviving tokens rejoined, which is what makes
    the expansion safe to run on every staged round rather than only on cycled
    ones. Empty fields are dropped and surrounding whitespace stripped — the
    seam's own rule, see the module docstring — and the token's own text is
    otherwise untouched.

    Raises :class:`PositionCycleError` for ``per_position < 1``. There is
    deliberately no upper bound: how many stops a session can carry is the
    relay's own ceiling (``angle_capture.session_lateral_walk``'s
    ``WALK_OVER_RELAY_CAPACITY``), and a second, lower bound invented on the
    laptop would refuse walks the speaker would have taken.
    """
    if per_position < 1:
        raise PositionCycleError(
            f"takes per position must be at least 1, got {per_position}"
        )
    tokens = [token.strip() for token in angles.split(",") if token.strip()]
    return ",".join(token for token in tokens for _ in range(per_position))


def staged_stops(angles: str) -> int:
    """How many stops ``angles`` stages — the walk's own release count.

    True only for the ``per_driver`` regime, and that is why the runner refuses
    ``--per-position`` for any other one: ``jasper-angle-capture`` composes stops
    as ``angle x _REGIME_STOPS[regime]``, so ``both`` is TWO stops per token and
    a count taken from the tokens alone would be half the real one.

    Its caller compares it against ``--complete-after``, which counts RELEASES
    (``arm_walk._complete_due``): a walk told to complete on fewer releases than
    it has stops posts its all-spots-measured signal partway through and exits
    ``ok``.
    """
    return len([token for token in angles.split(",") if token.strip()])


# --------------------------------------------------------------------------- #
# reading back — the index, derived from the banked bundle
# --------------------------------------------------------------------------- #


def _banked_take_records(round_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Every lateral take the bundle banked, with the directories they came from.

    Unreadable and non-lateral records are skipped rather than raised on: the
    same directory legitimately holds the CLOUD group's positions (the web
    host's ``retain_position`` serves both), and one corrupt sidecar must not
    cost the index the takes that are fine. What is missing is decided by the
    caller, from what came back.
    """
    records: list[dict[str, Any]] = []
    sources: set[str] = set()
    for path in sorted(round_dir.glob(_BANKED_POSITIONS_GLOB)):
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(raw, Mapping):
            continue
        if raw.get("kind") != POSITION_EVIDENCE_KIND:
            continue
        if raw.get("phase") != PHASE_LATERAL:
            continue
        records.append(dict(raw))
        sources.add(path.parent.relative_to(round_dir).as_posix())
    return records, sorted(sources)


def position_cycle_document(
    round_dir: str | Path, *, derived_at: datetime | None = None,
) -> dict[str, Any]:
    """The index for one banked round, derived from its own evidence.

    Raises :class:`PositionCycleError` naming exactly what the bundle did not
    carry — never a document assembled from what the round meant to stage.

    ``takes`` is sorted by ``(index, attempt)``, the order the walk served them
    and the order a retake follows the take it replaced. Both survivors and
    superseded takes are listed, because the speaker keeps both on disk
    deliberately ("the superseded one stays on disk as the honest walk record")
    and an index that hid one would be a third opinion about which take counted.
    """
    root = Path(round_dir)
    if not (root / "bundle").is_dir():
        raise PositionCycleError(
            f"{root}: no bundle/ was banked, so no take records exist to index"
        )
    records, sources = _banked_take_records(root)
    if not records:
        raise PositionCycleError(
            f"{root}: the banked bundle carries no {PHASE_LATERAL} take records "
            f"under {_BANKED_POSITIONS_GLOB} — this round's walk was refused at "
            f"take time, or its poses were never accepted"
        )
    takes = sorted(
        ({field: record.get(field) for field in _TAKE_FIELDS} for record in records),
        key=lambda take: (int(take["index"] or 0), int(take["attempt"] or 0)),
    )
    stamp = derived_at or datetime.now(timezone.utc)
    return {
        "kind": POSITION_CYCLE_KIND,
        "schema_version": SCHEMA_VERSION,
        "derived_at": stamp.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "sources": sources,
        "takes": takes,
    }


def read_position_cycle(path: str | Path) -> dict[str, Any]:
    """The index at ``path``, or :class:`PositionCycleError`.

    Strict in both directions — an unknown key and a missing one are both
    errors — for :mod:`.alignment_prescription`'s reason: a reader that ignored
    a key it did not know would read a NEWER document as an older one and say
    nothing.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise PositionCycleError(f"{path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise PositionCycleError(f"{path}: not a JSON object")
    unknown = sorted(set(raw) - _DOCUMENT_FIELDS)
    if unknown:
        raise PositionCycleError(f"{path}: unknown keys {unknown}")
    missing = sorted(_DOCUMENT_FIELDS - set(raw))
    if missing:
        raise PositionCycleError(f"{path}: missing keys {missing}")
    if raw["kind"] != POSITION_CYCLE_KIND:
        raise PositionCycleError(
            f"{path}: kind is {raw['kind']!r}, not {POSITION_CYCLE_KIND!r}"
        )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise PositionCycleError(
            f"{path}: schema_version {raw['schema_version']!r} is not "
            f"{SCHEMA_VERSION}"
        )
    takes = raw["takes"]
    if not isinstance(takes, list) or not takes:
        raise PositionCycleError(f"{path}: takes must be a non-empty list")
    for offset, take in enumerate(takes, start=1):
        if not isinstance(take, Mapping) or set(take) != set(_TAKE_FIELDS):
            raise PositionCycleError(
                f"{path}: take {offset} must carry exactly "
                f"{sorted(_TAKE_FIELDS)}"
            )
    return dict(raw)


def takes_by_position(document: Mapping[str, Any]) -> dict[int, tuple[str, ...]]:
    """``{position_deg: (take_id, …)}`` — the takes that share one pose.

    The split a comparison reads: every take measured at one bearing, in walk
    order, so per-take curves at that pose can be put beside each other. What
    DISTINGUISHES those takes — a different applied graph, or nothing at all —
    is not this document's to say; the retained capture's own
    ``provenance.graph.fingerprint`` is.
    """
    grouped: dict[int, list[str]] = {}
    for take in document["takes"]:
        grouped.setdefault(int(take["position_deg"]), []).append(str(take["take_id"]))
    return {degrees: tuple(ids) for degrees, ids in sorted(grouped.items())}
