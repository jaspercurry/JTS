# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""N takes at ONE pose, and the record of which take was which.

A staged arm walk is an ORDERED list of stops, and two stops at the same angle
are two ADJACENT stops rather than two walks — the microphone moves once per
angle.  That is not a new property: :func:`.angle_capture.both_at` already ships
it ("Both regimes at each angle, PAIRED so the microphone moves once per
angle"), and :class:`~jasper.active_speaker.angle_capture.AngleCaptureRequest`
holds its stops as an ordered tuple with no uniqueness rule.  So *taking N
captures at one pose in one walk* needs no new machinery on the speaker at all
— it needs the staged list to say so, and something to write down which stop was
which take.

This module is those two halves, and nothing else:

* :func:`expand_angle_spec` — the staged list, with each angle repeated N times
  adjacently.
* :func:`position_cycle_document` / :func:`read_position_cycle` — the manifest a
  round banks beside its evidence, and its strict reader.

**Why the expansion is a STRING transform.**  The angle vocabulary has exactly
one validator (:func:`~jasper.active_speaker.angle_capture._validated_angle`,
reached through ``jasper-angle-capture stage``), and its whole point is that
nothing coerces on the way there — ``0.4`` truncating to ``0`` turns a pose just
off the axis into an on-axis capture.  A laptop-side expansion that parsed the
angles to repeat them would be a second reader of that vocabulary, and the day
it disagreed it would disagree silently.  So each comma-separated token is
repeated VERBATIM, and whatever the operator wrote still reaches the one
validator that judges it.

**Why the manifest maps a stop to a POSE and a TAKE, and not to a config.**
Nothing on the laptop knows what graph a capture actually played through — and
the speaker already records that, per capture, in the retained capture's
``provenance.graph.fingerprint``
(:mod:`jasper.active_speaker.capture_provenance`, whose own docstring is the
argument: *the config label is not the graph*).  A fingerprint written here
would be the runner's INTENT, which is the wrong fact and the one that is wrong
when it matters.  What no artifact holds today is the pose: a banked round
records ``position_id`` (``{phase}_{index:02d}``) and a coarse ``onax``/``offax``
role, and ``round_views``' own docstring states it plainly — "no numeric
microphone angle is recovered for any position this module reads".  The staged
walk is the only place that number exists, and it lives in a single-use spool on
the speaker that nothing banks.  So this manifest writes down the one fact that
would otherwise be lost, for every staged round, whether or not it cycles.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

#: The manifest's own name, so a reader that finds this document anywhere knows
#: what it is holding without knowing which tool wrote it.
POSITION_CYCLE_KIND = "jts_crossover_v2_position_cycle"
SCHEMA_VERSION = 1

#: The file a round banks it as, inside the round directory.
POSITION_CYCLE_FILENAME = "position_cycle.json"

#: The keys :func:`read_position_cycle` accepts. Stated as a set and enforced,
#: rather than read leniently: a key this module does not know is either a newer
#: schema or a hand edit, and both are worth an error over a silent drop.
_DOCUMENT_FIELDS = frozenset({
    "kind", "schema_version", "created_at", "per_position", "regime",
    "angles", "staged_angles", "stops",
})
_STOP_FIELDS = frozenset({"stop", "angle", "take"})


class PositionCycleError(ValueError):
    """A manifest that may not be written, or may not be read."""


def expand_angle_spec(angles: str, per_position: int) -> str:
    """``"0,7"`` at 3 takes -> ``"0,0,0,7,7,7"`` — tokens repeated VERBATIM.

    Adjacent rather than interleaved, because the whole value of N takes at one
    pose is that nothing moved between them: ``0,7,0,7,0,7`` would walk the arm
    six times and measure the drift this exists to hold still.

    ``per_position=1`` returns the tokens rejoined unchanged, which is what makes
    the expansion safe to run on every staged round rather than only on cycled
    ones.  Whitespace around a token is stripped, so ``"0, 7"`` and ``"0,7"``
    stage the same walk; the token's own text is otherwise untouched, so the
    angle vocabulary keeps its single validator (see the module docstring).

    Raises :class:`PositionCycleError` for ``per_position < 1`` and for an empty
    token, which is a typed comma the seam would otherwise read as an angle.
    There is deliberately no upper bound here: how many stops a session can
    carry is the relay's own ceiling
    (``angle_capture.session_lateral_walk``'s ``WALK_OVER_RELAY_CAPACITY``),
    and a second, lower bound invented on the laptop would refuse walks the
    speaker would have taken.
    """
    if per_position < 1:
        raise PositionCycleError(
            f"takes per position must be at least 1, got {per_position}"
        )
    tokens = [token.strip() for token in angles.split(",")]
    if any(not token for token in tokens):
        raise PositionCycleError(
            f"an empty angle is not a stop: {angles!r}"
        )
    return ",".join(token for token in tokens for _ in range(per_position))


def staged_stops(angles: str) -> int:
    """How many stops ``angles`` stages — the walk's own release count.

    Its one caller compares it against ``--complete-after``, which counts
    RELEASES (``arm_walk._complete_due``): a walk told to complete on fewer
    releases than the walk has stops posts its all-spots-measured signal partway
    through and exits ``ok``, having measured a walk nobody asked for.
    """
    return len([token for token in angles.split(",") if token.strip()])


def position_cycle_document(
    *,
    angles: str,
    per_position: int,
    regime: str,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """The manifest for one staged walk: stop ordinal -> pose and take.

    ``angles`` is what the OPERATOR wrote and ``staged_angles`` is what was
    staged, both recorded because a reader asking "was this round cycled" and a
    reader asking "what did stop 4 measure" want different halves and neither
    should have to re-derive the other.

    ``stops`` is the mapping itself, 1-based in the order the walk serves them —
    the same 1-based running order ``angle_capture.ResolvedStop.index`` is built
    in, so a stop ordinal here and a stop ordinal there are the same number.
    """
    staged = expand_angle_spec(angles, per_position)
    tokens = [token.strip() for token in angles.split(",")]
    if not tokens or tokens == [""]:
        raise PositionCycleError("a staged walk needs at least one angle")
    stops: list[dict[str, Any]] = []
    for token in tokens:
        for take in range(1, per_position + 1):
            stops.append({"stop": len(stops) + 1, "angle": token, "take": take})
    stamp = created_at or datetime.now(timezone.utc)
    return {
        "kind": POSITION_CYCLE_KIND,
        "schema_version": SCHEMA_VERSION,
        "created_at": stamp.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "per_position": per_position,
        "regime": regime,
        "angles": ",".join(tokens),
        "staged_angles": staged,
        "stops": stops,
    }


def read_position_cycle(path: str | Path) -> dict[str, Any]:
    """The manifest at ``path``, or :class:`PositionCycleError`.

    Strict in both directions — an unknown key and a missing one are both
    errors — for :mod:`.alignment_prescription`'s reason: a reader that ignored
    a key it did not know would read a NEWER document as an older one and say
    nothing, and this document's whole job is to be believed later.
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
    stops = raw["stops"]
    if not isinstance(stops, list) or not stops:
        raise PositionCycleError(f"{path}: stops must be a non-empty list")
    for offset, stop in enumerate(stops, start=1):
        if not isinstance(stop, Mapping) or set(stop) != _STOP_FIELDS:
            raise PositionCycleError(
                f"{path}: stop {offset} must carry exactly "
                f"{sorted(_STOP_FIELDS)}"
            )
        if stop["stop"] != offset:
            raise PositionCycleError(
                f"{path}: stops are 1-based and in walk order; stop {offset} "
                f"says {stop['stop']!r}"
            )
    return dict(raw)


def stops_by_position(document: Mapping[str, Any]) -> dict[str, tuple[int, ...]]:
    """``{angle: (stop, stop, …)}`` — the takes that share one pose.

    The split a comparison reads: every stop ordinal measured at one pose, in
    walk order, so per-take curves at that pose can be put beside each other.
    What DISTINGUISHES those takes — a different applied graph, or nothing at
    all — is not this document's to say; the capture's own
    ``provenance.graph.fingerprint`` is (see the module docstring).
    """
    grouped: dict[str, list[int]] = {}
    for stop in document["stops"]:
        grouped.setdefault(str(stop["angle"]), []).append(int(stop["stop"]))
    return {angle: tuple(stops) for angle, stops in grouped.items()}
