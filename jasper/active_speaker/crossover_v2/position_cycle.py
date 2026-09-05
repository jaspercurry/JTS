# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""N takes at ONE pose: how they are staged, and how they read back.

A staged walk repeats a pose's stop ADJACENTLY rather than as a separate walk,
so the microphone moves once per angle (:func:`.angle_capture.both_at` ships
the same pairing). :func:`expand_angle_spec` / :func:`staged_stops` build and
count that staged list; :func:`position_cycle_document` /
:func:`read_position_cycle` derive the round's index from banked evidence,
never from what the round meant to stage — a laptop-written mapping would be
a second writer that could disagree with the speaker's record.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.atomic_io import atomic_write_text

from ..commissioning_evidence_store import EVIDENCE_ROOT
from .contracts import BANKED_TAKE_GLOB, POSITION_EVIDENCE_KIND
from .journey import PHASE_ENTRY_BASELINE, PHASE_LATERAL
from .record_index import bundle_measurements

#: The index's own name, so a reader that finds this document anywhere knows
#: what it is holding without knowing which tool wrote it.
POSITION_CYCLE_KIND = "jts_crossover_v2_position_cycle"
SCHEMA_VERSION = 1

#: The file a round banks it as, inside the round directory.
POSITION_CYCLE_FILENAME = "position_cycle.json"

#: Where ``bank-crossover-round.sh`` untars each bundle inside the round
#: directory. This is what :func:`_banked_take_records` walks; the takes
#: THEMSELVES are selected out of each bundle's measurement index, which
#: rescans :data:`BANKED_TAKE_GLOB` from the same tree.
_BANKED_BUNDLE_GLOB = "bundle/*"

#: Where a take lives, spelled whole for the refusal that names it. Composed,
#: never a second literal.
_BANKED_POSITIONS_GLOB = (
    f"{_BANKED_BUNDLE_GLOB}/{EVIDENCE_ROOT}/artifacts/{BANKED_TAKE_GLOB}"
)

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
    """``"0,7"`` at 3 takes -> ``"0,0,0,7,7,7"`` — tokens repeated VERBATIM, never parsed.

    Adjacent rather than interleaved: the whole value of N takes at one pose is
    that nothing moved between them, so ``0,7,0,7,0,7`` would walk the arm six
    times and measure the drift this exists to hold still. Tokens are repeated
    as text rather than parsed to floats — the angle vocabulary has exactly one
    validator (:func:`~jasper.active_speaker.angle_capture._validated_angle`)
    and a laptop-side parse could silently disagree with it (``0.4`` truncating
    to ``0`` turns an off-axis pose into an on-axis one).

    ``per_position=1`` returns the surviving tokens rejoined, so this is safe to
    run on every staged round, not only cycled ones. Empty fields are dropped
    and whitespace stripped, matching
    :func:`jasper.cli.angle_capture._parse_angles`.

    Raises :class:`PositionCycleError` for ``per_position < 1``. No upper
    bound: the ceiling is the plan's own
    (``angle_capture.session_lateral_walk``'s ``WALK_OVER_CAPTURE_CAPACITY``),
    and a second, lower bound invented on the laptop would refuse walks the
    speaker would have taken.
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


def take_artifact_path(bundle_dir: str | Path, take_path: str) -> Path:
    """Where one banked take lives, from the bundle and the row's own pointer.

    The ONE composition of that path. ``take_path`` is bundle-relative BELOW
    ``{EVIDENCE_ROOT}/artifacts/`` — the form
    :func:`~.record_index.bundle_measurements` rows carry and
    :func:`read_pose_curve_pair` returns — so a caller holding one must not
    prepend the prefix itself.
    """
    return Path(bundle_dir) / EVIDENCE_ROOT / "artifacts" / take_path


def take_phase_composition(bundle_dir: str | Path, take_path: str) -> str | None:
    """Which composition the take's curves carry, or ``None`` on a legacy take.

    Read off the record (``phase_composition``), never re-derived from the
    phase that was commanded: which phase ran and whether the analysis composed
    the configured crossover in are two facts, and
    docs/tuning-methodology.md section 4 step 1 turns on the second. A take
    that states neither — banked before the field, or captured with no
    protection to retain — reads ``None``, never one of the two.
    """

    try:
        raw = json.loads(take_artifact_path(bundle_dir, take_path).read_text())
    except (OSError, ValueError):
        return None
    stated = raw.get("phase_composition") if isinstance(raw, Mapping) else None
    return stated if isinstance(stated, str) and stated else None


def read_lateral_take(path: Path) -> dict[str, Any] | None:
    """One banked ``positions/{take_id}.json`` as a lateral take, or ``None``.

    ``None`` for everything that is not one, and the four ways that happens are
    deliberately indistinguishable to the caller: unreadable, not a JSON
    object, not a position-evidence record at all, or a CLOUD position. The
    last is the ordinary case rather than an error — the web host's
    ``bank_take`` serves both groups into the same directory, and this reader
    wants one of them.

    **The rule is phase, not bearing presence** —
    :func:`~.spatial.cloud_position_record` also stamps ``position_deg``, so a
    cloud seat would pass a bearing-shaped filter too. What separates them is
    what they ARE: a lateral pose is a per-driver measurement, a cloud seat is
    a summed sweep judged by gating and ripple, and they carry different
    columns (:data:`_TAKE_FIELDS` names a ``regime`` no cloud record has).
    Filtering on :data:`~.journey.PHASE_LATERAL` says that directly.

    One corrupt sidecar must not cost a reader the takes that are fine, so
    nothing here raises; what is MISSING is decided by the caller, from what
    came back.

    Public because it is the accept rule two readers share:
    :func:`position_cycle_document` below, and
    :func:`~.evidence_packet.build_crossover_evidence_packet`'s
    ``lateral_poses`` block. A second reader with its own idea of what a
    lateral take is would disagree with this one silently.

    Returns the record narrowed to :data:`_TAKE_FIELDS` plus ``vertical_deg``
    — the identity, the pose, and the verifier. The banked record stays the
    place to go for the rest. A take banked before ``vertical_deg`` existed
    reads back as 0, the elevation a walk that could not state a rise took.
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
    take: dict[str, Any] = {field: raw.get(field) for field in _TAKE_FIELDS}
    take["vertical_deg"] = raw.get("vertical_deg") or 0
    return take


def read_take_curves(path: Path, *, phase: str) -> list[Mapping[str, Any]] | None:
    """The measured curves one banked take carries, or ``None``.

    Ruling S3 banks magnitude AND phase for every measured curve
    (:func:`~.spatial.pose_curve_record`), which is what lets an offline reader
    reconstruct a transfer function exactly. The two readers above narrow a
    take to its identity and drop ``curves``, because their consumers index
    poses rather than re-analyse them; this returns the curves and nothing
    else, so neither of those records grows a payload its readers never asked
    for.

    ``phase`` is the caller's — a per-driver walk pose and a design-axis
    MEASURE capture both carry curves, and which one answers a question is the
    caller's to state. The rest of the accept rule is the shared one: a
    position-evidence record, readable, with a curve list on it. ``None`` for
    everything else, never a raise, exactly as the siblings above.
    """

    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    if raw.get("kind") != POSITION_EVIDENCE_KIND:
        return None
    if raw.get("phase") != phase:
        return None
    curves = raw.get("curves")
    if not isinstance(curves, list) or not curves:
        return None
    return [curve for curve in curves if isinstance(curve, Mapping)] or None


def parse_curve_magnitude(
    curve: Mapping[str, Any],
) -> tuple[np.ndarray, np.ndarray, tuple[float, float]] | None:
    """One banked curve's magnitude subset, coerced and validated, or ``None``.

    The shared step under every consumer of
    :func:`~.spatial.pose_curve_record`'s banked shape (ruling S3):
    ``freqs_hz`` and ``magnitude_db`` as equal-length float arrays with
    finite frequencies, and ``band_hz`` as an ordered ``(lo, hi)`` — falling
    back to the grid extent when the curve declares none. ``None`` when the
    mapping cannot supply that subset. A consumer's stricter requirement
    (phase, a role check, raising instead of skipping) layers on top; this is
    the one place "what a banked magnitude curve means" is decided, so the
    delay-landscape reader and the feature classifier cannot drift apart on it.
    """
    try:
        freqs = np.asarray([float(hz) for hz in curve["freqs_hz"]], dtype=float)
        magnitude = np.asarray(
            [float(db) for db in curve["magnitude_db"]], dtype=float
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not (freqs.size and freqs.size == magnitude.size):
        return None
    if not np.all(np.isfinite(freqs)):
        return None
    band = curve.get("band_hz")
    if (
        isinstance(band, (list, tuple))
        and len(band) == 2
        and all(isinstance(edge, (int, float)) for edge in band)
    ):
        swept = (float(band[0]), float(band[1]))
    else:
        swept = (float(freqs[0]), float(freqs[-1]))
    if not swept[0] < swept[1]:
        return None
    return freqs, magnitude, swept


def read_pose_curve_pair(
    bundle_dir: Path,
    *,
    phase: str,
    position_deg: int,
    vertical_deg: int = 0,
    roles: tuple[str, str],
) -> tuple[Mapping[str, Any], Mapping[str, Any], str] | None:
    """The latest banked take carrying BOTH roles, and the take it came from.

    Selected through the measurement index — :func:`~.record_index.
    bundle_measurements` narrows the candidates and :func:`read_take_curves`
    decides — so the delay landscape and the forward model share one answer to
    "which take speaks for this pose" instead of two that agree until they do
    not.

    Both roles must ride ONE take: the two transfers are summed against each
    other, so curves from two different captures would be summed across
    whatever moved between them.

    **Latest attempt wins.** A superseded take stays on disk as the honest walk
    record, and ``take_id`` is ``{position}_a{attempt:02d}`` zero-padded so the
    index's path order is also chronological. The rows are therefore walked
    newest-first and the first match returned, which is the retake rather than
    what it replaced.

    **A pose is a bearing AND a height.** ``vertical_deg`` is the signed
    whole-degree elevation above mark height, defaulting to the mark: a
    design-axis consumer asking for 0 deg gets the take measured at the mark,
    never a raised one banked later in the same walk. Without it "latest
    attempt wins" would walk right past the pose it was asked for.

    ``None`` when no take at this pose carries both roles, never a raise: a
    round that measured one driver is an ordinary shape.
    """

    for row in reversed(
        bundle_measurements(
            bundle_dir,
            phase=phase,
            position_deg=position_deg,
            vertical_deg=vertical_deg,
        )
    ):
        curves = read_take_curves(
            take_artifact_path(bundle_dir, row.path), phase=phase,
        )
        if curves is None:
            continue
        by_role = {str(curve.get("role")): curve for curve in curves}
        if roles[0] in by_role and roles[1] in by_role:
            return by_role[roles[0]], by_role[roles[1]], row.path
    return None


def parse_curve_complex(
    curve: Mapping[str, Any],
) -> tuple[np.ndarray, np.ndarray, tuple[float, float]] | None:
    """One banked curve's complex transfer, reconstructed exactly, or ``None``.

    :func:`parse_curve_magnitude` plus the phase half of ruling S3's banked
    pair, which is the inverse of :func:`~.spatial.pose_curve_record`'s
    serialization: ``10 ** (magnitude_db / 20) * exp(1j * radians(phase_deg))``.
    The one place a banked curve becomes a transfer function, so the delay
    landscape and the forward model cannot drift apart on what "the banked
    curve" means.

    ``None`` on everything :func:`parse_curve_magnitude` rejects, plus a curve
    carrying no ``phase_deg`` or one whose phase disagrees in length with the
    grid. A consumer that wants a raise, or a role check, layers it on top.

    Phase is banked WRAPPED to (-180, 180]; a consumer needing a continuous
    phase unwraps it itself, since the branch choice is the consumer's.
    """
    parsed = parse_curve_magnitude(curve)
    if parsed is None:
        return None
    freqs, magnitude_db, swept = parsed
    try:
        phase_deg = np.asarray(
            [float(deg) for deg in curve["phase_deg"]], dtype=float
        )
    except (KeyError, TypeError, ValueError):
        return None
    if phase_deg.size != freqs.size:
        return None
    tf = 10.0 ** (magnitude_db / 20.0) * np.exp(1j * np.radians(phase_deg))
    return freqs, tf, swept


def read_entry_baseline_take(path: Path) -> dict[str, Any] | None:
    """One banked ``positions/{take_id}.json`` as the round's "before", or ``None``.

    :func:`read_lateral_take`'s sibling, on the phase that is not a group
    member. Same directory, same accept rule shape, same reason for existing:
    the record is banked and nothing surfaced it. Filtering on
    :data:`~.journey.PHASE_ENTRY_BASELINE` says what this take IS, exactly as
    that reader's own note argues.

    **This is the DURABLE full copy of the entry baseline.** The flow state
    file carries the same arrays for the duration of one round and is
    rewritten on the next persist; this take is write-once, so a banked round
    can still be re-graded (ruling S3, ADR-0228).

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
    """Every lateral take the bundle banked, with the directories they came from.

    Selected through the measurement index rather than by globbing the tree:
    one place decides what a banked take is and where it lives, and it is the
    place the store writes. :func:`read_lateral_take` still opens every
    selected file and applies its own accept rule, so the index narrows the
    candidates and the record itself decides.
    """
    records: list[dict[str, Any]] = []
    sources: set[str] = set()
    for bundle in sorted(
        path for path in round_dir.glob(_BANKED_BUNDLE_GLOB) if path.is_dir()
    ):
        for row in bundle_measurements(bundle, phase=PHASE_LATERAL):
            path = take_artifact_path(bundle, row.path)
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
    try:
        takes = sorted(
            records,
            key=lambda take: (int(take["index"] or 0), int(take["attempt"] or 0)),
        )
    except (TypeError, ValueError) as exc:
        # Named, not coerced: this module's callers all handle its own error,
        # and one that escaped as a bare ValueError would unwind whatever the
        # caller was in the middle of (a bank, for one).
        raise PositionCycleError(
            f"{root}: a banked take carries a non-numeric index or attempt "
            f"({exc}), so the walk order cannot be derived"
        ) from exc
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


def write_position_cycle(
    round_dir: str | Path,
) -> tuple[Path, dict[str, Any]]:
    """Derive the index for a banked round and write it INTO that round.

    The ONE writer of :data:`POSITION_CYCLE_FILENAME`, shared by the on-box
    bank and the laptop transport, so a round carries the same index at the
    same name however it was banked. Returns the path written and the document
    written there, so a caller reporting on it never re-reads the file.

    Written atomically, so a reader never finds a torn index at a path
    ``provenance.json`` may be about to call absent.

    Raises exactly two things, which is what lets both callers treat it as
    best-effort with one handler: :class:`PositionCycleError` for a round with
    nothing to index (the ordinary shape of a round that ran no lateral walk,
    and of one whose records are corrupt) and :class:`OSError` for a
    destination that would not take the file. A round that measured is not
    un-measured by an index that could not be derived.
    """
    document = position_cycle_document(round_dir)
    path = Path(round_dir) / POSITION_CYCLE_FILENAME
    atomic_write_text(path, json.dumps(document, indent=2) + "\n")
    return path, document


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
        # ``vertical_deg`` is exempted at the MISSING end only: a document
        # written before it existed reads, a document inventing a key does not.
        if not isinstance(take, Mapping) or not (
            set(_TAKE_FIELDS) <= set(take) <= set(_TAKE_FIELDS) | {"vertical_deg"}
        ):
            raise PositionCycleError(
                f"{path}: take {offset} must carry exactly "
                f"{sorted(_TAKE_FIELDS)}, optionally with ['vertical_deg']"
            )
    return dict(raw)


def takes_by_position(
    document: Mapping[str, Any],
) -> dict[tuple[int, int], tuple[str, ...]]:
    """``{(position_deg, vertical_deg): (take_id, …)}`` — one pose's takes.

    The key is the POSE PAIR, not the bearing alone: a walk that raises the
    microphone measures a different pose at the same bearing, and folding the
    two together would put curves from two poses in one comparison.
    ``vertical_deg`` reads 0 when the take predates it.

    The split a comparison reads: every take measured at one pose, in walk
    order, so per-take curves at that pose can be put beside each other. What
    DISTINGUISHES those takes — a different applied graph, or nothing at all —
    is the take's own banked ``graph_fingerprint`` — WHICH CANDIDATE WAS
    APPLIED, per :func:`~.spatial.lateral_pose_record`. NOT the capture's
    ``provenance.graph.fingerprint``: a per-driver take plays through the
    transient routing graph, whose running hash is identical before and after
    an apply.
    """
    grouped: dict[tuple[int, int], list[str]] = {}
    for take in document["takes"]:
        pose = (int(take["position_deg"]), int(take.get("vertical_deg") or 0))
        grouped.setdefault(pose, []).append(str(take["take_id"]))
    return {pose: tuple(ids) for pose, ids in sorted(grouped.items())}
