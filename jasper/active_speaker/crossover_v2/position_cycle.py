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
publishes it through the evidence store, which lands it at
``{EVIDENCE_ROOT}/artifacts/crossover_v2/{relay}/positions/{take_id}.json``
inside the bundle; and ``bank-crossover-round.sh`` tars that whole bundle tree
into the round directory.  A laptop-written mapping would therefore be a SECOND
writer of one fact — the runner's *intent* beside the speaker's *record* — and
the two disagree exactly when it matters (a rejected take, a retake, a walk the
session refused at take time and ran its ordinary shape instead).

So :func:`position_cycle_document` reads those banked records and projects them.
Every value in the document it returns can be found under
:data:`_BANKED_POSITIONS_GLOB`; nothing in it is computed from what the round
MEANT to stage.  What the document adds is convenience: one sorted
index at the round root, instead of a glob over a nested per-take tree that also
holds the cloud group's positions.  When the banked evidence cannot support the
index, this refuses and names exactly what was missing — it never falls back to
intent.

**Why an index is worth having at all.**  The pose IS banked; nothing SURFACED
it.  ``round_views`` reads the *cloud* positions block, so a lateral walk's
bearings — the numbers a take-per-pose comparison is made of — sat on disk in
per-take sidecars that no view opened.  The evidence packet now opens them
through :func:`read_lateral_take` below, which is this module's own accept rule
rather than a second reading of it; this index remains the convenient sorted
form of the same records.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..commissioning_evidence_store import EVIDENCE_ROOT
from .journey import PHASE_ENTRY_BASELINE, PHASE_LATERAL

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
#:
#: **The store's namespace is imported, never respelled.** A record does not land
#: at the relative path its writer passes: ``publish_json_artifact`` runs it
#: through ``_artifact_path``, which prefixes
#: ``{EVIDENCE_ROOT}/artifacts/`` — so the writer's
#: ``crossover_v2/{relay}/positions/{take_id}.json`` becomes
#: ``evidence/v1/artifacts/crossover_v2/…`` inside the bundle. Getting that
#: wrong is not a loud failure: the glob simply matches nothing and this module
#: reports a walk that was never refused. It is what
#: ``test_the_glob_matches_a_record_the_REAL_store_wrote`` exists for — that test
#: publishes through the real store and derives from the result, so the segments
#: still written as literals here are pinned to the actual writer rather than to
#: a second reading of it.
_BANKED_POSITIONS_GLOB = (
    f"bundle/*/{EVIDENCE_ROOT}/artifacts/crossover_v2/*/positions/*.json"
)

#: ``kind`` on the speaker's own per-take record
#: (``correction_crossover_v2``'s ``bank_take``). Records that do not
#: carry it are not this document's input, whatever else is in the directory.
POSITION_EVIDENCE_KIND = "jts_crossover_v2_position_evidence"

#: What each take contributes to the index — the identity, the pose, and the
#: verifier. Every one is a field ``lateral_pose_record`` writes; the banked
#: record stays the place to go for the rest (``offset_cm``, ``at_mark``,
#: ``prompt``, ``lateral_consumer``), which is why the document names its
#: ``sources``.
_TAKE_FIELDS = ("index", "attempt", "take_id", "position_deg", "role",
                "regime", "wav_sha256")

#: The three arrays that make a retained entry-baseline take the durable copy.
#: A take without all three predates the curve riding here and is not readable
#: as a baseline.
_ENTRY_CURVE_FIELDS = ("freqs_hz", "magnitude_db", "excluded")

#: What :func:`read_entry_baseline_take` returns — deliberately the field names
#: ``round_evidence.EntryBaseline.from_dict`` reads, so a caller rehydrates by
#: handing the result straight to it rather than re-spelling the mapping. The
#: take's own id is returned as ``artifact_ref``, which is the name that record
#: gives the artifact it came from.
_ENTRY_BASELINE_FIELDS = (
    "program_id", "reference_mark", "graph_fingerprint", "captured_at",
    *_ENTRY_CURVE_FIELDS,
)

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

    True for any regime that composes ONE stop per angle.
    ``jasper.cli.angle_capture._REGIME_STOPS`` maps every member of ``REGIMES``
    to a 1-tuple of itself, so a single-regime walk is one stop per token and
    ``both`` — the entry pairing two regimes — is the exception at two. That is
    the regime the runner refuses ``--per-position`` for, and it asks the table
    rather than naming regimes: stops are composed as
    ``angle x _REGIME_STOPS[regime]``, so a count taken from the tokens alone
    would be half the real one there.

    Its caller compares it against ``--complete-after``, which counts RELEASES
    (``arm_walk._complete_due``): a walk told to complete on fewer releases than
    it has stops posts its all-spots-measured signal partway through and exits
    ``ok``.
    """
    return len([token for token in angles.split(",") if token.strip()])


# --------------------------------------------------------------------------- #
# reading back — the index, derived from the banked bundle
# --------------------------------------------------------------------------- #


def read_lateral_take(path: Path) -> dict[str, Any] | None:
    """One banked ``positions/{take_id}.json`` as a lateral take, or ``None``.

    ``None`` for everything that is not one, and the four ways that happens are
    deliberately indistinguishable to the caller: unreadable, not a JSON
    object, not a position-evidence record at all, or a CLOUD position. The
    last is the ordinary case rather than an error — the web host's
    ``bank_take`` serves both groups into the same directory, and this reader
    wants one of them.

    **The rule is phase, and the reason is NOT "only a pose has a bearing".**
    It was, until the 2026-08-24 geometry ruling made
    :func:`~.spatial.cloud_position_record` stamp its own ``position_deg`` —
    so a cloud seat would now pass a bearing-shaped filter and land in a
    per-driver walk's take list. What separates them is what they ARE: a
    lateral pose is a per-driver measurement, a cloud seat is a summed sweep
    judged by gating and ripple, and they carry different columns
    (:data:`_TAKE_FIELDS` names a ``regime`` no cloud record has). Filtering on
    :data:`~.journey.PHASE_LATERAL` says that directly instead of resting on a
    field-presence coincidence that has already stopped being one.

    One corrupt sidecar must not cost a reader the takes that are fine, so
    nothing here raises; what is MISSING is decided by the caller, from what
    came back.

    Public because it is the accept rule two readers share:
    :func:`position_cycle_document` below, and
    :func:`~.evidence_packet.build_crossover_evidence_packet`'s
    ``lateral_poses`` block. A second reader with its own idea of what a
    lateral take is would disagree with this one silently.

    Returns the record narrowed to :data:`_TAKE_FIELDS` — the identity, the
    pose, and the verifier. The banked record stays the place to go for the
    rest.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    if raw.get("kind") != POSITION_EVIDENCE_KIND:
        return None
    if raw.get("phase") != PHASE_LATERAL:
        return None
    return {field: raw.get(field) for field in _TAKE_FIELDS}


def read_entry_baseline_take(path: Path) -> dict[str, Any] | None:
    """One banked ``positions/{take_id}.json`` as the round's "before", or ``None``.

    :func:`read_lateral_take`'s sibling, on the phase that is not a group
    member. Same directory, same accept rule shape, same reason for existing:
    the record is banked and nothing surfaced it. Filtering on
    :data:`~.journey.PHASE_ENTRY_BASELINE` says what this take IS, exactly as
    that reader's own note argues.

    **This is the DURABLE full copy of the entry baseline** (fragment ``02``
    duplication #2). The flow state file carries the same arrays for the
    duration of one round and is rewritten on the next persist; this take is
    write-once, so a banked round can still be re-graded — which is ruling S3's
    *"every analysis is re-runnable offline, forever, without re-measuring."*

    ``None`` for everything that is not one — unreadable, not a JSON object,
    not a position-evidence record, a different phase, or a record from before
    the curve rode here. That last case is why the three curve arrays are
    required rather than defaulted: a take with no curve cannot answer the
    question this reader is asked, and returning it half-filled would put a
    baseline-shaped record with no bins in front of a comparison.

    Returns the record narrowed to :data:`_ENTRY_BASELINE_FIELDS`, which are the
    field names ``round_evidence.EntryBaseline.from_dict`` reads — so a caller
    rehydrates by handing this straight to it.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    if raw.get("kind") != POSITION_EVIDENCE_KIND:
        return None
    if raw.get("phase") != PHASE_ENTRY_BASELINE:
        return None
    if not all(isinstance(raw.get(field), list) for field in _ENTRY_CURVE_FIELDS):
        return None
    take = {field: raw.get(field) for field in _ENTRY_BASELINE_FIELDS}
    take["artifact_ref"] = raw.get("take_id")
    return take


def _banked_take_records(round_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Every lateral take the bundle banked, with the directories they came from."""
    records: list[dict[str, Any]] = []
    sources: set[str] = set()
    for path in sorted(round_dir.glob(_BANKED_POSITIONS_GLOB)):
        take = read_lateral_take(path)
        if take is None:
            continue
        records.append(take)
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
        records,
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
    is the take's own banked ``graph_fingerprint`` — WHICH CANDIDATE WAS
    APPLIED, per :func:`~.spatial.lateral_pose_record`. NOT the capture's
    ``provenance.graph.fingerprint``: a per-driver take plays through the
    transient routing graph, whose running hash is identical before and after
    an apply.
    """
    grouped: dict[int, list[str]] = {}
    for take in document["takes"]:
        grouped.setdefault(int(take["position_deg"]), []).append(str(take["take_id"]))
    return {degrees: tuple(ids) for degrees, ids in sorted(grouped.items())}
