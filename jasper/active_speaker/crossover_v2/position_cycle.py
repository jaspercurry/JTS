# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Derive and read the pose index from banked measurement evidence."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, NamedTuple, overload

import numpy as np

from jasper.platform.atomic_io import atomic_write_text
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable

from ..commissioning_evidence_store import EVIDENCE_ROOT
from ..measurement_programs import PURPOSE_SPEAKER
from ..run_manifest import kept_measurements
from .contracts import BANKED_TAKE_GLOB, POSITION_EVIDENCE_KIND
from .journey import PHASE_LATERAL
from .pose_curve import WINDOW_GATED, WINDOW_UNGATED
from .record_index import Measurement, bundle_measurements

#: The index's own name, so a reader that finds this document anywhere knows
#: what it is holding without knowing which tool wrote it.
POSITION_CYCLE_KIND = "jts_crossover_v2_position_cycle"
SCHEMA_VERSION = 2

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

#: What each take contributes to the index: the identity, the pose, the
#: verifier and the candidate. The banked record holds the rest, which is why
#: the document names its ``sources``. ``candidate_id`` tells apart the takes a
#: cycled pose measures at ONE bearing.
_TAKE_FIELDS = ("index", "attempt", "take_id", "position_deg", "role",
                "regime", "wav_sha256", "vertical_deg", "candidate_id")

#: The keys :func:`read_position_cycle` accepts. Strict in both directions: a
#: key this module does not know is either a newer schema or a hand edit, and
#: both are worth an error over a silent drop.
_DOCUMENT_FIELDS = frozenset({
    "kind", "schema_version", "derived_at", "sources", "takes",
})


class PositionCycleError(ValueError):
    """The index cannot be derived, or cannot be read."""


def take_artifact_path(bundle_dir: str | Path, take_path: str) -> Path:
    """Where one banked take lives, from the bundle and the row's own pointer.

    The ONE composition of that path. ``take_path`` is bundle-relative BELOW
    ``{EVIDENCE_ROOT}/artifacts/`` — the form
    :func:`~.record_index.bundle_measurements` rows carry — so a caller
    holding one must not prepend the prefix itself.
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
    last is the ordinary case rather than an error — both groups bank into
    the same directory, and this reader wants one of them.

    **The rule is phase, not bearing presence** — a banked cloud position also
    stamps ``position_deg``, so a cloud seat would pass a bearing-shaped filter
    too. What separates them is what they ARE: a lateral pose is a per-driver
    measurement, a cloud seat is a summed sweep judged by gating and ripple,
    and they carry different columns (:data:`_TAKE_FIELDS` names a ``regime``
    no cloud record has).
    Filtering on :data:`~.journey.PHASE_LATERAL` says that directly.

    One corrupt sidecar must not cost a reader the takes that are fine, so
    nothing here raises; what is MISSING is decided by the caller, from what
    came back.

    Public because it is the accept rule two readers share:
    :func:`position_cycle_document` below, and
    :func:`~.evidence_packet.build_crossover_evidence_packet`'s
    ``lateral_poses`` block. A second reader with its own idea of what a
    lateral take is would disagree with this one silently.

    Returns the record narrowed to :data:`_TAKE_FIELDS`, as banked.
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


def take_window(record: Mapping[str, Any]) -> str:
    """The window a take's own analysis graded: gated wherever its gate
    windowed a response, which banks a gated curve (ADR-0383 §2)."""
    curves = record.get("curves") or ()
    return WINDOW_GATED if any(curve.get("window") == WINDOW_GATED for curve in curves) else WINDOW_UNGATED


def take_curves(raw: Mapping[str, Any], window: str) -> list[Mapping[str, Any]] | None:
    """The curves one take record banked through ``window``, or ``None`` when it banked none."""
    curves = raw.get("curves")
    if not isinstance(curves, list):
        return None
    return [curve for curve in curves if isinstance(curve, Mapping) and curve.get("window") == window] or None


@overload
def take_curve(
    record: Mapping[str, Any], role: str, window: str | None = None, *, required: Literal[True],
) -> Mapping[str, Any]: ...
@overload
def take_curve(
    record: Mapping[str, Any], role: str, window: str | None = None, *, required: bool = False,
) -> Mapping[str, Any] | None: ...
def take_curve(
    record: Mapping[str, Any], role: str, window: str | None = None, *, required: bool = False,
) -> Mapping[str, Any] | None:
    """The curve a take record banked for ``role`` through ``window``, else
    through :func:`take_window`; ``None`` when it banked none for it, as a take
    whose analysis failed banks none (ADR-0383). A record that banked neither,
    or none for a ``required`` role, refuses by that field, naming the role and
    window (#2902)."""
    window = window or take_window(record)
    curve = next((curve for curve in record.get("curves") or ()
                  if curve.get("role") == role and curve.get("window") == window), None)
    if curve is None and (required or ("curves" not in record and "analysis_error" not in record)):
        raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {
            "record": record.get("record_id"), "take_id": record.get("take_id"), "field": "curves",
            "role": role, "window": window})
    return curve


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


def measured_curve_band(
    curve: Mapping[str, Any],
) -> tuple[np.ndarray, np.ndarray, tuple[float, float]] | None:
    """:func:`parse_curve_magnitude`, its band narrowed to what the take can
    speak for: its own grid and driven band, above its trusted floor (else its
    validity floor)."""
    parsed = parse_curve_magnitude(curve)
    if parsed is None:
        return None
    freqs, magnitude, band = parsed
    floor = curve.get("trusted_floor_hz") or curve.get("validity_floor_hz") or 0
    return freqs, magnitude, (float(max(freqs[0], band[0], floor)), float(min(freqs[-1], band[1])))


class PoseCurvePair(NamedTuple):
    lower: Mapping[str, Any]
    upper: Mapping[str, Any]
    take: Measurement
    document: Mapping[str, Any]


def select_pose_curve_pair(
    bundle_dir: Path, *, phases: tuple[str, ...], position_deg: int | None,
    roles: tuple[str, str], vertical_deg: int = 0, take_id: str | None = None,
    search_detail: dict[str, Any] | None = None,
) -> PoseCurvePair | None:
    """Newest matching speaker take the round kept, with both gated curves and
    their recorded request facts.

    Both roles must ride ONE take: combining transfers from different captures
    would sum across whatever moved between them. Take ids are zero-padded
    ordinals, so the index's path order is capture order. Height stays part of
    the pose even when the bearing is unspecified: a newer raised take cannot
    stand in for a measurement at mark height.
    """
    purposes = (PURPOSE_SPEAKER,)
    if search_detail is not None:
        search_detail.update(bundle_dir=str(bundle_dir), phases_searched=list(phases),
                             purposes_searched=list(purposes), roles_required=list(roles),
                             takes_seen=0, roles_per_take={}, poses=[])
    for row, document in reversed(list(kept_measurements(bundle_dir, phases=phases, purposes=purposes))):
        if (row.vertical_deg != vertical_deg
            or (position_deg is not None and row.position_deg != position_deg)
            or (take_id is not None and document.get("take_id") != take_id)):
            continue
        curves = take_curves(document, WINDOW_GATED)
        if search_detail is not None:
            search_detail["takes_seen"] += 1
            search_detail["roles_per_take"][row.path] = dict(Counter(
                str(curve.get("role") or "") for curve in curves or []))
            pose = {"position_deg": row.position_deg, "vertical_deg": row.vertical_deg}
            if pose not in search_detail["poses"]:
                search_detail["poses"].append(pose)
        if curves is None:
            continue
        by_role = {str(curve.get("role")): curve for curve in curves}
        if roles[0] in by_role and roles[1] in by_role:
            return PoseCurvePair(by_role[roles[0]], by_role[roles[1]], row, document)
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
    lacking = sorted(str(take["take_id"]) for take in records if type(take["candidate_id"]) is not str)
    if lacking:
        raise PositionCycleError(f"{root}: takes {lacking} carry no candidate_id")
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
        if not isinstance(take, Mapping) or set(take) != set(_TAKE_FIELDS):
            raise PositionCycleError(
                f"{path}: take {offset} must carry exactly {sorted(_TAKE_FIELDS)}"
            )
    return dict(raw)


def takes_by_position(
    document: Mapping[str, Any],
) -> dict[tuple[int, int], tuple[str, ...]]:
    """``{(position_deg, vertical_deg): (take_id, …)}`` — one pose's takes.

    The key is the POSE PAIR, not the bearing alone: a walk that raises the
    microphone measures a different pose at the same bearing, and folding the
    two together would put curves from two poses in one comparison.

    The split a comparison reads: every take measured at one pose, in walk
    order, so per-take curves at that pose can be put beside each other. What
    DISTINGUISHES those takes — a different applied graph, or nothing at all —
    is the take's own banked ``graph_fingerprint`` — WHICH CANDIDATE WAS
    APPLIED, stamped onto every take record at bank time. NOT the capture's
    ``provenance.graph.fingerprint``: a per-driver take plays through the
    transient routing graph, whose running hash is identical before and after
    an apply.
    """
    grouped: dict[tuple[int, int], list[str]] = {}
    for take in document["takes"]:
        pose = (int(take["position_deg"]), int(take["vertical_deg"]))
        grouped.setdefault(pose, []).append(str(take["take_id"]))
    return {pose: tuple(ids) for pose, ids in sorted(grouped.items())}
