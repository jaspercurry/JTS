# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve one round's captures, matching state and separate bank-time context.

The capture owner's snapshot lives in the bundle. Legacy live or banked state
is usable only when its capture ID matches the round's artifact directory.
Design, applied profile, repeat floor, declared geometry and the CamillaDSP
statefile retain their own current/bank-time meanings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Iterator, Mapping, NamedTuple, Sequence

from jasper.platform.json_fields import finite_float, parse_utc_iso
from jasper.audio_measurement.evidence_reasons import (
    CAPTURE_UNREADABLE_SIDECAR, EVIDENCE_NOT_BANKED, ROOM_NOT_BANKED, SET_REQUIRED, unavailable,
)
from jasper.active_speaker.applied_identity import BASE_LAYER
from jasper.active_speaker.measurement_programs import (
    CANDIDATE_LAYERS, POSE_KIND_BEARING, POSE_KIND_SEAT, PROGRAM_ROWS, PURPOSE_BASS, PURPOSE_REFERENCE, PURPOSE_ROOM,
    PURPOSE_SPEAKER, RUNNABLE_PROGRAMS, run_purpose, run_purposes,
)
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME, RoundSetRefused, pointer_rows, row_record_id, view_sets
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from .contracts import on_design_axis
from .journey import PHASE_TIMING
from .position_cycle import take_artifact_path
from jasper.active_speaker import bundles
from jasper.active_speaker.candidate_bank import _candidate_roots, _directories
from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

from jasper.active_speaker.state_paths import (
    DEFAULT_BASELINE_PROFILE_STATE_PATH as APPLIED_PROFILE_DEFAULT_PATH,
)
from jasper.active_speaker.crossover_v2.durable_state import (
    DEFAULT_V2_STATE_PATH as STATE_DEFAULT_PATH,
)
from jasper.active_speaker.design_draft import (
    DEFAULT_DESIGN_DRAFT_PATH as DRIVERS_DEFAULT_PATH,
)
from jasper.audio_measurement.measurement_geometry import (
    DEFAULT_PATH as _DECLARED_GEOMETRY_DEFAULT_PATH, read_declared_geometry,
)
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as REPEAT_FLOOR_DEFAULT_PATH,
)
from jasper.platform.paths import camilla_statefile

__all__ = [
    'APPLIED_PROFILE_DEFAULT_PATH', 'APPLIED_PROFILE_FILENAME', 'CAPTURE_STATE_FILENAME',
    'DECLARED_GEOMETRY_DEFAULT_PATH', 'DECLARED_GEOMETRY_FILENAME', 'DESIGN_DRAFT_FILENAME',
    'DRIVERS_DEFAULT_PATH', 'REPEAT_FLOOR_DEFAULT_PATH', 'REPEAT_FLOOR_FILENAME',
    'RoundInputs', 'RoundViewsError', 'STATE_DEFAULT_PATH',
    'STATE_FILENAME', 'STATE_SESSION_UNKNOWN',
    'STATEFILE_FILENAME', 'banked_round_of', 'banked_rounds', 'packet_purposes',
    'matching_state_path', 'read_banked_round', 'recent_round_sessions', 'latest_banked_rounds', 'round_stores',
    'state_matches_capture',
    'round_inputs', 'bank_of', 'banked_packet', 'contract_sources', 'prescription_sources', 'BASS_PACKET_ROUND_MISMATCH',
    'default_out', 'view_path', 'files_by_set', 'set_view_path', 'set_view_out', 'set_choices',
    'ROUND_INPUT_ERRORS', 'RoundSetRefused', 'SetTakes', 'read_run_manifest', 'resolve_set', 'latest_measure_takes',
    'subject', 'COMPARAND_EARLIER_ROUND', 'COMPARAND_SAME_ROUND', 'Comparand', 'comparand', 'comparands',
]

STATE_FILENAME = "state.json"
CAPTURE_STATE_FILENAME = "crossover-v2-state.json"
DESIGN_DRAFT_FILENAME = "design-draft.json"
APPLIED_PROFILE_FILENAME = "applied-profile.json"
REPEAT_FLOOR_FILENAME = "repeat-floor.json"
DECLARED_GEOMETRY_FILENAME = "declared-geometry.json"
STATEFILE_FILENAME = "camilla-statefile.yml"
PACKET_FILENAME = "packet.json"
#: The ``schema`` of the ``packet.json`` this build writes. A packet of any other is stale (#2902).
ROUND_PACKET_SCHEMA = "jts_round_packet/7"
PICTURE_FILENAME = "frequency.png"
INDEX_FILENAME = "index.md"
ROOM_ARTIFACT = "room.json"
REAR_ARTIFACT = "rear_view.json"

DECLARED_GEOMETRY_DEFAULT_PATH = Path(_DECLARED_GEOMETRY_DEFAULT_PATH)

STATE_SESSION_UNKNOWN = "state_session_unknown"


class CrossoverEvidencePacketError(ValueError):
    """The named directory is not a crossover-v2 session bundle."""


NO_ROUND_ARTIFACTS_REASON = "no crossover_v2 round artifacts under evidence/v1"


def round_artifact_dir(session_dir: Path) -> tuple[Path | None, str]:
    matches = sorted(
        path for path in session_dir.glob(f"{EVIDENCE_ROOT}/artifacts/crossover_v2/*")
        if path.is_dir()
    )
    if not matches:
        return None, NO_ROUND_ARTIFACTS_REASON
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        return None, f"bundle carries more than one round ({names})"
    return matches[0], ""


class RoundViewsError(CrossoverEvidencePacketError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


@dataclass(frozen=True)
class RoundInputs:
    """Paths for a round; unavailable matching state carries a reason code."""

    session_dir: Path
    state_path: Path | None
    design_draft_path: Path | None
    applied_profile_path: Path | None
    repeat_floor_path: Path | None
    declared_geometry_path: Path | None
    statefile_path: Path | None
    banked: bool
    state_reason: str = ""


def state_matches_capture(state: object, capture_id: str) -> bool:
    return isinstance(state, Mapping) and state.get("session_id") == capture_id


def _read_json_mapping(path: Path) -> dict[str, Any] | None:
    # Deliberately NOT atomic_io.read_json_mapping: that owner opens via
    # builtin open(), not Path.open, and
    # test_prescription_contract.py::test_round_context_is_read_once
    # instruments Path.open to pin each evidence file read exactly once.
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def matching_state_path(
    session_dir: Path, fallback: Path | None,
) -> tuple[Path | None, str]:
    """Prefer the capture owner's snapshot; accept older state only by capture ID."""
    round_dir, _reason = round_artifact_dir(session_dir)
    if round_dir is None:
        return None, STATE_SESSION_UNKNOWN
    reason = ""
    for path in (session_dir / CAPTURE_STATE_FILENAME, fallback):
        if path is None or not path.is_file():
            continue
        state = _read_json_mapping(path)
        if state_matches_capture(state, round_dir.name):
            return path, ""
        reason = STATE_SESSION_UNKNOWN
    return None, reason


def _sibling(round_dir: Path, name: str) -> Path | None:
    path = round_dir / name
    return path if path.is_file() else None


def round_inputs(path: Path) -> RoundInputs:
    """Read banked or live round paths."""
    path = Path(path)
    bundle_dir = path / "bundle"
    if bundle_dir.is_dir():
        children = sorted(child for child in bundle_dir.iterdir() if child.is_dir())
        if len(children) != 1:
            raise RoundViewsError(
                f"{bundle_dir}: expected exactly one session directory, "
                f"found {len(children)}"
            )
        state_path, state_reason = matching_state_path(children[0], _sibling(path, STATE_FILENAME))
        return RoundInputs(
            session_dir=children[0],
            state_path=state_path,
            design_draft_path=_sibling(path, DESIGN_DRAFT_FILENAME),
            applied_profile_path=_sibling(path, APPLIED_PROFILE_FILENAME),
            repeat_floor_path=_sibling(path, REPEAT_FLOOR_FILENAME),
            declared_geometry_path=_sibling(path, DECLARED_GEOMETRY_FILENAME),
            statefile_path=_sibling(path, STATEFILE_FILENAME),
            banked=True,
            state_reason=state_reason,
        )
    if (path / "info.json").is_file():
        state_path, state_reason = matching_state_path(path, STATE_DEFAULT_PATH)
        return RoundInputs(
            session_dir=path,
            state_path=state_path,
            design_draft_path=DRIVERS_DEFAULT_PATH,
            applied_profile_path=APPLIED_PROFILE_DEFAULT_PATH,
            repeat_floor_path=REPEAT_FLOOR_DEFAULT_PATH,
            declared_geometry_path=DECLARED_GEOMETRY_DEFAULT_PATH,
            statefile_path=camilla_statefile(),
            banked=False,
            state_reason=state_reason,
        )
    raise RoundViewsError(
        f"{path}: neither a banked round (no bundle/ directory) nor a live "
        f"session bundle (no info.json)", code="round_not_found"
    )


def banked_round_of(session_dir: Path) -> Path | None:
    """Find the bank containing this bundle."""
    candidate = session_dir.parent.parent
    try:
        inputs = round_inputs(candidate)
        return candidate if inputs.banked and inputs.session_dir == session_dir else None
    except RoundViewsError:
        return None


def round_stores(session_dir: Path | None = None) -> tuple[Path, ...]:
    """The live session store and the campaign store ``session_dir`` belongs to; this box's by default."""
    bank = banked_round_of(session_dir) if session_dir is not None else None
    root = (bank.parent if bank else session_dir.parent) if session_dir is not None else bundles.sessions_dir()
    return _candidate_roots(root)


def _recent_round_directories(session_dir: Path | None, *, limit: int | None) -> list[tuple[float, Path]]:
    directories = []
    for store in round_stores(session_dir):
        directories.extend(sorted(
            ((path.stat().st_mtime, path) for path in _directories(store)), reverse=True,
        )[:limit])
    return sorted(directories, reverse=True)


def recent_round_sessions(session_dir: Path | None = None, *, limit: int = 32) -> list[Path]:
    """Read recent live and banked rounds."""
    sessions: dict[str, tuple[float, Path]] = {}
    for _modified_at, directory in _recent_round_directories(session_dir, limit=max(0, limit)):
        try:
            bundle = round_inputs(directory).session_dir
        except (OSError, CrossoverEvidencePacketError):
            continue
        info = _read_json_mapping(bundle / "info.json")
        if info is None:
            continue
        sessions.setdefault(str(info.get("session_id") or bundle.name), (
            finite_float(info.get("started_at")) or 0.0, bundle,
        ))
    return [bundle for _started_at, bundle in sorted(sessions.values(), reverse=True)][:max(0, limit)]


def read_banked_round(directory: Path, modified_at: float) -> tuple[dict[str, Any], float] | None:
    """A banked round's packet and when it was banked; ``None`` for any other directory.

    ``modified_at`` dates only a round whose provenance and packet name no time.
    """
    if not (directory / "bundle").is_dir():
        return None
    packet = _read_json_mapping(directory / PACKET_FILENAME) or {}
    provenance = _read_json_mapping(directory / "provenance.json") or {}
    # Packet/view rewrites change directory mtime; the bank owns this timestamp.
    return packet, next((value for value in (
        parse_utc_iso(str(provenance.get("banked_at_utc") or "")),
        finite_float(packet.get("finalized_at")), finite_float(packet.get("started_at")),
        finite_float((packet.get("session") or {}).get("started_at")),
    ) if value is not None), modified_at)


def banked_rounds(
    session_dir: Path | None = None, *, limit: int | None = None,
) -> Iterator[tuple[Path, dict[str, Any], float]]:
    """Each banked round among the stores' ``limit`` latest-modified directories (all when ``None``)."""
    stop = None if limit is None else max(0, limit)
    for modified_at, directory in _recent_round_directories(session_dir, limit=stop)[:stop]:
        read = read_banked_round(directory, modified_at)
        if read is not None:
            packet, banked_at = read
            yield directory, packet, banked_at


def packet_purposes(packet: Mapping[str, Any]) -> tuple[str, ...]:
    """The programs a banked packet counts for: its own, and room and bass when it carries their
    views, as the in-room round carries both (ADR-0429). A round that kept no take counts for
    none. A round whose every take plays one driver alone counts for none but reference: it
    holds no take its program's next step reads, until #5696 (ADR-0360 §2)."""
    try:
        purpose = run_purpose(packet.get("preset"))
    except ValueError:
        return ()
    takes = [take for group in packet.get("sets") or () for take in group.get("takes") or ()]
    if not any(take.get("selected") for take in takes) or (
            purpose != PURPOSE_REFERENCE and all((take.get("pose") or {}).get("driver") for take in takes)):
        return ()
    carried = (name for name in (PURPOSE_ROOM, PURPOSE_BASS) if packet.get(name))
    return tuple(name for name in dict.fromkeys((purpose, *carried)) if name)


#: Each applied layer and the program that owns it, in stack order: the base and the speaker's own
#: layer lie under every program, and each program's own layer under the programs after it (ADR-0420).
_LAYER_OWNERS = ((BASE_LAYER, PURPOSE_SPEAKER), *((row.candidate_fields[0].name, row.purpose) for row in PROGRAM_ROWS))


def _stale_by(played: Mapping[str, Any], current: Mapping[str, Any], under: tuple[str, ...],
              cleared: Collection[str] = ()) -> list[str]:
    """The programs ``under`` whose applied layer changed since ``played`` were the layers, but for a layer
    in ``cleared`` (ADR-0420)."""
    return list(dict.fromkeys(owner for layer, owner in _LAYER_OWNERS if owner in under and layer not in cleared
                              and played.get(layer) != current.get(layer)))


def _judged(packet: Mapping[str, Any], identity: Mapping[str, Any], purpose: str) -> dict[str, Any]:
    """A round of ``purpose`` judged two ways (ADR-0437). ``stale`` and ``stale_by`` judge one of its sets that
    kept a take and names its layers, each by the layers it played but those each of its kept takes played
    cleared: a current set first, then one the program can design on (each kept take played the program's own
    layer cleared, and for room and bass stood at a seat), then the newest (the last the packet lists). ``set_id`` names that set when it is current
    and the program can design on it. ``base_stale_by`` judges the stack the round was banked on, the applied
    tune at its bank. A layer above the program counts for neither, and a purpose outside the stack plays none."""
    under = RUNNABLE_PROGRAMS[:RUNNABLE_PROGRAMS.index(purpose) + 1] if purpose in RUNNABLE_PROGRAMS else ()
    own = {owner: layer for layer, owner in _LAYER_OWNERS}.get(purpose)
    current = identity.get("layer_fingerprints") or {}
    sets = []
    for group in reversed(packet.get("sets") or ()):
        kept = [take for take in group["takes"] if take.get("selected")]
        if kept and "layer_fingerprints" in group:
            cleared = set(CANDIDATE_LAYERS).intersection(*(take.get("cleared_layers") or () for take in kept))
            seated = purpose not in (PURPOSE_ROOM, PURPOSE_BASS) or all(
                (take.get("pose") or {}).get("kind") == POSE_KIND_SEAT for take in kept)
            sets.append((_stale_by(group["layer_fingerprints"], current, under, cleared), own in cleared and seated,
                         group["set_id"]))
    stale_by, designs, set_id = min(sets, key=lambda row: (bool(row[0]), not row[1]), default=(list(under), False, None))
    return {"set_id": set_id if designs and not stale_by else None, "stale": bool(stale_by), "stale_by": stale_by,
            "base_stale_by": _stale_by((packet.get("applied") or {}).get("layer_fingerprints") or {}, current, under)}


def latest_banked_rounds(
    identity: Mapping[str, Any], session_dir: Path | None = None, *, limit: int = 32,
    programs: tuple[str, ...] = RUNNABLE_PROGRAMS, include_stale: bool = False,
) -> dict[str, dict[str, Any]]:
    """Latest packet per program within a bounded window, judged by :func:`_judged`: by default only a current
    one, and with ``include_stale`` a current one before a newer stale one; among current ones, one that names a
    set to design on before a newer one that names none."""
    found: dict[str, dict[str, Any]] = {}
    for directory, packet, banked_at in banked_rounds(session_dir, limit=limit):
        for name in (name for name in packet_purposes(packet) if name in programs):
            judged = _judged(packet, identity, name)
            prior = found.get(name)
            rank = (not judged["stale"], judged["set_id"] is not None, banked_at, str(directory))
            if (include_stale or not judged["stale"]) and (prior is None or rank > (
                    not prior["stale"], prior["set_id"] is not None, prior["started_at"], prior["round_dir"])):
                found[name] = {"round_dir": str(directory), "started_at": banked_at, "round_id": directory.name,
                               "banked_at": banked_at, "status": packet.get("result"), **judged,
                               **({"alignment_verdict": packet.get("alignment_verdict"),
                                   "next_action": packet.get("next_action")} if name == PURPOSE_SPEAKER else {})}
    return dict(sorted(found.items(), key=lambda item: (item[1]["started_at"], item[1]["round_dir"]), reverse=True))


def set_artifact_name(name: str, set_id: str | None = None) -> str:
    path = Path(name)
    return f"{path.stem}-{set_id[:12]}{path.suffix}" if set_id else name


def take_artifact_name(name: str, take_id: str, role: str) -> str:
    """A per-take view's file name; the whole take id, since a run's takes share
    its prefix. A target's colon (``woofer:rear``) becomes ``_`` for portability."""
    path = Path(name)
    return f"{path.stem}-{take_id}-{role}{path.suffix}".replace(":", "_")


def default_out(inputs: RoundInputs, round_dir: Path, name: str, set_id: str | None = None) -> Path:
    """Where a view lands when the operator named no ``--out``.

    A BANKED round tree is the operator's own directory, so its views stay
    beside the evidence they were computed from — including a view pointed at
    the bundle INSIDE that tree, which is the only way the bundle-taking verbs
    can be called: filing beside the caller there would leave every artifact
    where ``catalog`` never looks. A LIVE session bundle is the daemon's
    (``/var/lib/jasper/active_speaker/sessions/<id>``, written by the web host
    as its own user): defaulting inside it made the ordinary invocation —
    grade the round I just ran — raise ``PermissionError`` for the operator
    this door was added for (#3498). So a live round's view lands beside the
    caller instead, named by the session it came from so two sessions graded
    in one directory do not overwrite each other.
    """
    name = set_artifact_name(name, set_id)
    root = round_dir if inputs.banked else banked_round_of(inputs.session_dir)
    return root / name if root else Path.cwd() / f"{inputs.session_dir.name}-{name}"


def view_path(inputs: RoundInputs, name: str, set_id: str | None = None) -> Path:
    """Where a view of this round files ``name`` (:func:`default_out`), for a reader holding only its inputs."""
    return default_out(inputs, banked_round_of(inputs.session_dir) or inputs.session_dir, name, set_id)


def files_by_set(sets: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a round names its sets (``sets`` is :func:`view_sets`): a round of several does. A call must then
    name one (:func:`resolve_set`), and each per-set view of the bank is filed under its set's name, by the bank
    and by the verbs that re-run it (:func:`set_view_out`). A round of one files each view under none."""
    return len(sets) > 1


def set_view_path(inputs: RoundInputs, name: str, set_id: str | None, sets: Sequence[Mapping[str, Any]]) -> Path:
    """Where set ``set_id``'s view ``name`` is filed: under the set's name in a round of several view sets,
    else under none (:func:`files_by_set`)."""
    return view_path(inputs, name, set_id if files_by_set(sets) else None)


def set_view_out(inputs: RoundInputs, name: str, set_id: str | None) -> Path:
    """Where a verb files set ``set_id``'s view ``name`` when the operator named no ``--out``: where the bank files it."""
    return set_view_path(inputs, name, set_id, view_sets(read_run_manifest(inputs)))


def bank_of(inputs: RoundInputs) -> Path | None:
    """The bank holding the round, whether it is named by its bank or by its bundle."""
    return inputs.session_dir.parent.parent if inputs.banked else banked_round_of(inputs.session_dir)


def banked_packet(inputs: RoundInputs) -> dict[str, Any]:
    """The ``packet.json`` the round's bank wrote, or ``{}`` for a round banked without one.

    A packet another schema wrote refuses by name: its blocks mean what that schema meant (#2902).
    """
    round_dir = bank_of(inputs)
    packet = (_read_json_mapping(round_dir / PACKET_FILENAME) or {}) if round_dir else {}
    if packet and packet.get("schema") != ROUND_PACKET_SCHEMA:
        raise RoundViewsError(
            f"{round_dir}: packet.json field schema is {packet.get('schema')!r}, not {ROUND_PACKET_SCHEMA!r}; "
            "bank the round again from its session", code=EVIDENCE_NOT_BANKED)
    return packet


def _banked_room(rows: list[Any], sets: Sequence[Mapping[str, Any]], set_id: str | None) -> dict[str, Any]:
    """The bank's copy of set ``set_id``'s room view, or ``{}``: each row names its set, in whatever file the bank
    filed it. A round of one view set needs none named."""
    wanted = set_id or (sets[0]["set_id"] if len(sets) == 1 else None)
    return next((row for row in rows if wanted and isinstance(row, dict) and row.get("set_id") == wanted), {})


def _banks_room(manifest: Mapping[str, Any] | None) -> bool:
    """Whether the round's bank runs a room view for each of its sets: its preset's purposes include room."""
    try:
        return PURPOSE_ROOM in run_purposes(str((manifest or {}).get("preset") or ""))
    except ValueError:
        return False


def contract_sources(round_: Path | RoundInputs, *, set_id: str | None = None) -> dict[str, Any]:
    inputs = round_ if isinstance(round_, RoundInputs) else round_inputs(round_)
    artifact_dir, reason = round_artifact_dir(inputs.session_dir)
    if artifact_dir is None:
        raise CrossoverEvidencePacketError(reason)
    manifest = _read_json_mapping(artifact_dir / RUN_MANIFEST_FILENAME)
    sets = view_sets(manifest or {})
    packet = banked_packet(inputs)
    # A re-run room view is a view: a banked round's contract reads its bank's copy (ADR-0371).
    banked_rooms = packet.get("room")
    room = (_banked_room(banked_rooms, sets, set_id) if isinstance(banked_rooms, list)
            else _read_json_mapping(set_view_path(inputs, ROOM_ARTIFACT, set_id, sets)) or {})
    if not room and (inputs.banked or isinstance(banked_rooms, list)):
        # Each set of a room round banks a room view, so a call that names none has no one view to serve.
        ambiguous = set_id is None and files_by_set(sets) and _banks_room(manifest)
        room = {"median": {"code": SET_REQUIRED, "sets": set_choices(sets)} if ambiguous else {"code": ROOM_NOT_BANKED}}
    path = artifact_dir / "candidate.json"
    # A banked file that is not one JSON object is still the round's candidate: no judge reopens it,
    # so each refuses it by this code. Only a round that banked none has no base.
    candidate = (_read_json_mapping(path) or {"code": "candidate_malformed"}) if path.is_file() else {}
    # The rear seed reads the round's rear views: its bank's copy, or the file a bank reads while it banks (ADR-0425).
    rear = packet.get("rear")
    if not isinstance(rear, list):
        rear = [view] if (view := _read_json_mapping(view_path(inputs, REAR_ARTIFACT))) else []
    return {"candidate": candidate,
            "manifest": with_records(inputs.session_dir, manifest) if manifest else {},
            **{f"room_{section}": room.get(section, {})
               for section in ("median", "persistence", "ceiling")},
            # An unreadable file reads as undeclared here; the evidence packet's block names it (ADR-0388).
            "rear_views": rear, "declared_geometry": read_declared_geometry(inputs.declared_geometry_path)[0]}


#: The round directory's packet names another round (or none), so its bass evidence is not this round's.
BASS_PACKET_ROUND_MISMATCH = "bass_packet_round_mismatch"


def prescription_sources(inputs: RoundInputs | None, *, set_id: str | None = None) -> dict[str, Any]:
    if inputs is None:
        return {"bass_evidence": {}}
    if set_id is not None:
        resolve_set(inputs, set_id)
    sources = contract_sources(inputs, set_id=set_id)
    packet = banked_packet(inputs)
    return {**sources,
            "bass_evidence": (packet if packet.get("round_id") == inputs.session_dir.parent.parent.name
                              else {"code": BASS_PACKET_ROUND_MISMATCH} if packet else {}),
            "draft": (_read_json_mapping(inputs.design_draft_path) or {}) if inputs.design_draft_path else {},
            "applied_profile": load_applied_baseline_profile_state(inputs.applied_profile_path) if inputs.applied_profile_path else None}


ROUND_INPUT_ERRORS = (OSError, EOFError, ValueError, KeyError, TypeError)


def capture_identity(capture_basis: Mapping[str, Any], *, set_id: str) -> tuple[Any, ...]:
    identity = (capture_basis.get("candidate_id"), capture_basis.get("graph_fingerprint"))
    return (*identity, capture_basis.get("side"), set_id if not any(identity) else None)


def take_order(take: Mapping[str, Any]) -> tuple[str, int]:
    """A joined take's place in capture order: its record's wall-clock stamp,
    then its attempt, since stamps tie within a second."""
    return take.get("captured_at") or "", take.get("attempt", 0)


def latest_measure_takes(
    rows: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    key: Callable[[Mapping[str, Any], Mapping[str, Any]], tuple[Any, ...] | None],
) -> dict[tuple[Any, ...], tuple[Mapping[str, Any], Mapping[str, Any]]]:
    latest: dict[tuple[Any, ...], tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for group, take in rows:
        if not take["selected"] or take.get("phase") != "measure":
            continue
        identity = key(group, take)
        if identity is None:
            continue
        previous = latest.get(identity)
        if previous is None or take_order(take) >= take_order(previous[1]):
            latest[identity] = group, take
    return latest


class SetTakes(NamedTuple):
    set_id: str
    capture_basis: Mapping[str, Any]
    takes: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SetTakes:
        """A joined timing take (ADR-0319) is no take of its set: the packet index
        walks every set of its joined manifest, timing sets too."""
        takes = tuple(take for take in row["takes"] if take.get("phase") != PHASE_TIMING)
        return cls(row["set_id"], row["capture_basis"], takes)

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(take["take_id"] for take in self.takes if take["selected"])

    @property
    def role(self) -> str:
        """The response the set measured: a driver target, or ``summed``."""
        return str(self.capture_basis.get("role") or "summed")

    @property
    def on_axis(self) -> tuple[Mapping[str, Any], ...]:
        """The kept on-axis bearing takes; a pose is on its record, so these read
        joined takes (:meth:`with_records`)."""
        return tuple(take for take in self.takes if take["selected"]
                     and take["pose"].get("kind") == POSE_KIND_BEARING
                     and on_design_axis(take["pose"].get("azimuth_deg"), take["pose"].get("elevation_deg")))

    def take_id(self, requested: str | None = None) -> str:
        ids = self.selected_ids
        if requested is not None:
            if requested in ids:
                return requested
            if held := next((take for take in self.takes if take["take_id"] == requested), None):
                # A joined take names its record's status and verdict; a row alone names none (ADR-0395).
                verdict = held.get("verdict") or {}
                raise RoundSetRefused("round_take_not_kept", set_id=self.set_id, take_id=requested, take_ids=ids,
                                      status=held.get("measurement_status"),
                                      fault=verdict.get("fault") or held.get("incident") or None, next=verdict.get("next"))
            raise RoundSetRefused("round_take_unknown", set_id=self.set_id, take_id=requested, take_ids=ids)
        if len(ids) == 1:
            return ids[0]
        on_axis = [take["take_id"] for take in self.on_axis]
        if len(on_axis) == 1:
            return on_axis[0]
        raise RoundSetRefused("round_take_selection_required", set_id=self.set_id, take_ids=ids)

    def with_records(self, bundle_dir: Path, *, every_take: bool = False) -> SetTakes:
        """This set, each selected take, or ``every_take``, read with its record (:func:`take_records`)."""
        return self._replace(takes=tuple(map(take_records(bundle_dir, every_take=every_take), self.takes)))


def take_records(
    bundle_dir: Path, *, disclose: bool = False, every_take: bool = False,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """One reader's join of a kept take's row with the record it points at, as
    ``{**row, **record}``, each record read once: a reader reads a take's own
    record, and only for the takes it reads (ADR-0395). A take the run did not
    select keeps its row, unless ``every_take``, and so does a take that banked
    no record. A record that cannot be read refuses by name, or with
    ``disclose`` leaves its take unselected, with the gap as its ``record``."""
    records: dict[str, Mapping[str, Any]] = {}

    def joined(row: Mapping[str, Any]) -> dict[str, Any]:
        record_id = row_record_id(row)
        if not (record_id and (row["selected"] or every_take)):
            return dict(row)
        if record_id not in records:
            record = _read_json_mapping(take_artifact_path(bundle_dir, record_id))
            if record is None and not disclose:
                raise RoundSetRefused(CAPTURE_UNREADABLE_SIDECAR, record=record_id, take_id=row["take_id"])
            records[record_id] = record if record is not None else {
                "selected": False, "record": unavailable(CAPTURE_UNREADABLE_SIDECAR, {"record": record_id})}
        return {**row, **records[record_id]}

    return joined


def with_records(
    bundle_dir: Path, manifest: Mapping[str, Any], *, disclose: bool = False, every_take: bool = False,
) -> dict[str, Any]:
    """``manifest`` with every set's takes read by one :func:`take_records`."""
    joined = take_records(bundle_dir, disclose=disclose, every_take=every_take)
    return {**manifest, "sets": [{**group, "takes": [joined(take) for take in group["takes"]]}
                                 for group in manifest.get("sets", ())]}


def read_run_manifest(
    inputs: RoundInputs, *, manifest: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if manifest is None:
        directory, _ = round_artifact_dir(inputs.session_dir)
        path = directory / RUN_MANIFEST_FILENAME if directory else inputs.session_dir / RUN_MANIFEST_FILENAME
        if directory is None or not path.is_file():
            raise RoundSetRefused("round_manifest_missing", path=str(path))
        manifest = json.loads(path.read_text())
    assert manifest is not None
    if manifest.get("finalized") is not True:
        raise RoundSetRefused("round_manifest_unfinalized", run_id=manifest.get("run_id"))
    return pointer_rows(manifest)


def set_choices(sets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The sets a call may name, each with what tells it from the others: the list ``set_required`` carries."""
    return [{"set_id": group.set_id, "candidate_id": group.capture_basis.get("candidate_id"),
             "role": group.role, "take_count": len(group.selected_ids)}
            for group in map(SetTakes.from_row, sets)]


def resolve_set(
    inputs: RoundInputs, set_id: str | None = None, *, take: str | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> SetTakes:
    """Resolve the executor's set without rebuilding its identity (ADR-0299).
    Its takes are the manifest's rows, as given; a reader of a take's facts
    joins them (:meth:`SetTakes.with_records`, ADR-0395). With no ``set_id``, a
    ``take`` names the set that holds it; a take two sets hold (one per role it
    recorded) still needs the set named, and a take no set holds is unknown."""
    sets = view_sets(read_run_manifest(inputs, manifest=manifest))
    if set_id is None and take is not None:
        held = [row for row in sets if any(one["take_id"] == take for one in row["takes"])]
        if not held:
            raise RoundSetRefused("round_take_unknown", take_id=take, take_ids=tuple(dict.fromkeys(
                kept for row in sets for kept in SetTakes.from_row(row).selected_ids)))
        sets = held
    if set_id is None and files_by_set(sets):
        raise RoundSetRefused(SET_REQUIRED, sets=set_choices(sets))
    matches = [row for row in sets if set_id is None or row["set_id"] == set_id]
    if len(matches) != 1:
        raise RoundSetRefused("round_set_unknown", set_id=set_id, sets=[row["set_id"] for row in sets])
    row, = matches
    return SetTakes.from_row(row)


def subject(
    inputs: RoundInputs | None, selected: SetTakes | None = None, *, set_id: str | None = None,
    take_ids: Iterable[str] | None = None, candidate_id: str | None = None,
) -> dict[str, Any]:
    """What an answer read, by the catalog's ids (``jasper-round list``); an id
    that does not apply, or a live bundle no bank holds, is absent (ADR-0344 §2)."""
    banked = bank_of(inputs) if inputs is not None else None
    if selected is not None:
        set_id = selected.set_id
        candidate_id = candidate_id or selected.capture_basis.get("candidate_id")
    return {key: value for key, value in (
        ("round_id", banked.name if banked else None), ("set_id", set_id),
        ("take_ids", None if take_ids is None else list(take_ids)), ("candidate_id", candidate_id),
    ) if value is not None}


#: Where a take's comparand came from (ADR-0391).
COMPARAND_SAME_ROUND = "same_round_base"
COMPARAND_EARLIER_ROUND = "earlier_round"


class Comparand(NamedTuple):
    source: str
    round_dir: Path
    set_id: str
    take_ids: tuple[str, ...]  # the set's selected takes at the key, newest first
    role: str

    @property
    def take_id(self) -> str:
        return self.take_ids[0]


def _comparand_key(group: SetTakes, take: Mapping[str, Any], role: str | None = None) -> tuple[Any, ...]:
    """The place a take's pose names, the drivers it reads (its side's, the
    response ``role``), and the graph scope that played."""
    pose = {key: value for key, value in (take.get("pose") or {}).items() if key != "place"}
    basis = group.capture_basis
    return json.dumps(pose, sort_keys=True), basis.get("side"), role or group.role, basis.get("graph_scope")


def _kept_view_sets(inputs: RoundInputs, named: Collection[str] | None = None) -> list[dict[str, Any]]:
    """A round's view sets, or only those ``named``, each kept take read with its
    record; a record that cannot be read leaves its take unkept, never the
    reason the rule fails (ADR-0101)."""
    joined = take_records(inputs.session_dir, disclose=True)
    return [{**row, "takes": [joined(take) for take in row["takes"]]}
            for row in view_sets(read_run_manifest(inputs)) if named is None or row["set_id"] in named]


def _newest(rows: Iterable[Mapping[str, Any]], key: tuple[Any, ...],
            order: Callable[[Mapping[str, Any]], tuple[Any, ...]]) -> tuple[str, tuple[str, ...]] | None:
    """The set holding the newest selected take at ``key``, and its selected takes there, newest first."""
    matches = sorted(((order(other), other["take_id"], group.set_id) for group in map(SetTakes.from_row, rows)
                      for other in group.takes if other["selected"] and _comparand_key(group, other) == key),
                     reverse=True)
    if not matches:
        return None
    return matches[0][2], tuple(take_id for _, take_id, set_id in matches if set_id == matches[0][2])


def comparands(
    round_dir: Path, wanted: Sequence[tuple[str, str, str]], *,
    sets_of: Callable[[Mapping[str, Any]], Collection[str]] | None = None, limit: int = 32,
) -> list[Comparand | None]:
    """The one comparand rule (ADR-0391) for each ``(set_id, take_id, role)`` of
    one round, in one walk of the banks: the round's base take at the take's
    place, preferring the take's own run; else the newest selected take banked
    before the round at the same place, drivers and graph scope, among the
    banked rounds in :func:`banked_rounds`' window of ``limit`` directories. A
    live bundle dates by its bank copy when the window holds one, else by when
    it started. ``sets_of`` names, from an earlier round's packet, the only sets
    of it that count, for a role a round holds once (a rear round's reference):
    such a take's comparand is never in its own round. The same-round A/B is the
    decision evidence and an earlier take is context: a comparison over the pair
    discloses :func:`~.measurement_context.compare_capture_basis`."""
    inputs = round_inputs(round_dir)
    sets = _kept_view_sets(inputs)
    rows = {row["set_id"]: row for row in sets}
    keys: list[tuple[Any, ...]] = []
    found: list[Comparand | None] = []
    for set_id, take_id, role in wanted:
        group = SetTakes.from_row(rows[set_id])
        take = next(take for take in group.takes if take["take_id"] == take_id)
        keys.append(_comparand_key(group, take, role))
        hit = None if sets_of is not None or rows[set_id].get("base") else _newest(
            [row for row in sets if row.get("base")], keys[-1],
            lambda other: (other.get("run_id") == take.get("run_id"), *take_order(other)))
        found.append(Comparand(COMPARAND_SAME_ROUND, round_dir, hit[0], hit[1], role) if hit else None)
    if all(found):
        return found
    rounds = sorted(((banked_at, str(path), None if sets_of is None else frozenset(sets_of(packet)))
                     for path, packet, banked_at in banked_rounds(inputs.session_dir, limit=limit)),
                    key=lambda entry: entry[:2], reverse=True)
    # round_bank copies a live bundle under the bundle's own name.
    own = bank_of(inputs) or next((Path(path) for _, path, _ in rounds
                                   if (Path(path) / "bundle" / inputs.session_dir.name).is_dir()), None)
    dated = read_banked_round(own, own.stat().st_mtime) if own else None
    before = dated[1] if dated else finite_float(
        (_read_json_mapping(inputs.session_dir / "info.json") or {}).get("started_at")) or 0.0
    for banked_at, directory, named in rounds:
        if banked_at >= before or named == frozenset():
            continue
        try:
            earlier = _kept_view_sets(round_inputs(Path(directory)), named)
            for index, key in enumerate(keys):
                if found[index] is None and (hit := _newest(earlier, key, take_order)):
                    found[index] = Comparand(COMPARAND_EARLIER_ROUND, Path(directory), hit[0], hit[1], wanted[index][2])
        except ROUND_INPUT_ERRORS:
            continue
        if all(found):
            break
    return found


def comparand(round_dir: Path, set_id: str, take_id: str, role: str, *, limit: int = 32) -> Comparand | None:
    """:func:`comparands` for one take."""
    return comparands(round_dir, [(set_id, take_id, role)], limit=limit)[0]
