# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The rear comparison over one banked batch of summed sweeps (issue #5330).

One batch plays, at the same microphone positions and the same session level,
the incumbent tune, the same tune with its rear muted, and one to three
variants that each change one control family. This module selects those takes,
freezes the batch's comparison band and per-position reference curve ONCE, and
hands :mod:`jasper.audio_measurement.rear_evidence` the arrays.

Code computes, the LLM judges: there is no score, no pass mark, no ranking and
no claim about rear rejection or a polar pattern. Missing evidence carries a
reason code and never a filled-in figure, and nothing here reads which mover
placed the microphone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.baseline_profile import profile_linearization
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate
from jasper.active_speaker.measurement_programs import PURPOSE_REAR
from jasper.active_speaker.rear_calibration import (
    changed_section_paths, rear_operating_facts, section_change_family,
)
from jasper.active_speaker.run_manifest import view_sets
from jasper.audio_measurement.measurement_geometry import boundary_prior, load_declared_geometry
from jasper.audio_measurement.rear_evidence import (
    across_positions, comparison_band, position_figures, reference_curve_db, repeat_spread,
)

from .evidence_packet import applied_profile_source
from .measurement_context import capture_basis
from .room_selection import SeatTake, analyzed_purpose_takes
from .room_views import room_ceiling
from .round_captures import RoundCapturesRefused
from .round_inputs import RoundInputs, banked_round_of

REFUSE_NO_REAR_TAKES = "rear_no_summed_takes"
REFUSE_NO_INCUMBENT = "rear_incumbent_set_unavailable"

ROLE_INCUMBENT = "incumbent"
ROLE_REAR_MUTED = "rear_muted"
ROLE_VARIANT = "variant"

#: A pose some candidate measured that the batch could not score: the
#: reference take is missing there, so the position has no frozen zero and no
#: candidate may be read at it. Disclosed, never dropped.
REASON_NO_REFERENCE_TAKE = "no_reference_take"

#: The capture facts every candidate in one batch must share for the figures to
#: mean anything, echoed from the takes' own basis rather than restated.
LEVEL_FIELDS = ("level_db", "program_id", "loudness_volume_db",
                "calibration_applied", "calibration_reference")


def _shared(values: Sequence[Any]) -> Any:
    """The one value every take agrees on, or ``None`` when they disagree."""
    return values[0] if values and all(value == values[0] for value in values) else None


def _candidate_key(value: Any) -> str:
    """One key for the candidate a record or a manifest set names.

    The manifest's own ``base`` flag names the incumbent, so the engine never
    reads the arm's base-candidate spelling: that vocabulary stays front-end
    side (ADR-0228). A record and its own capture basis therefore agree here by
    construction — the basis is derived from the record.
    """
    return str(value or "")


def _mean_curve_db(takes: Sequence[SeatTake], freqs_hz: Any = None) -> tuple[np.ndarray, np.ndarray]:
    """One candidate's repeats at one position, averaged onto ``freqs_hz``."""
    grid = takes[0].freqs_hz if freqs_hz is None else np.asarray(freqs_hz, dtype=float)
    rows = np.vstack([np.interp(grid, take.freqs_hz, take.magnitude_db) for take in takes])
    return grid, np.asarray(np.mean(rows, axis=0))


def _wall_dip_hz(walls: Mapping[str, float]) -> float | None:
    """The declared front-to-wall bounce's quarter-wave null, through the
    boundary prior's own derivation (ADR-0317) rather than a second copy."""
    if "front" not in walls:
        return None
    return boundary_prior((), walls={"front": walls["front"]})["walls"]["front"]["f_null_hz"]


def _rear_sections(
    inputs: RoundInputs, candidates: Sequence[str],
    *, base: str | None, profile: Mapping[str, Any] | None, profile_reason: str,
) -> dict[str, tuple[Mapping[str, Any], str]]:
    """Each played candidate's rear section, and why one is unreadable.

    The ``base`` candidate is the saved tune, so its section comes from the
    round's own banked applied profile — the snapshot every graph-safety proof
    recomposes from. Every other candidate is a fingerprint, read from the
    candidate bank rooted at this round's bank.
    """
    snapshot = (profile or {}).get("recomposition_snapshot") or {}
    bank = banked_round_of(inputs.session_dir)
    found: dict[str, tuple[Mapping[str, Any], str]] = {}
    for candidate in candidates:
        if candidate == base:
            found[candidate] = (snapshot.get("rear_calibration") or {}, profile_reason)
            continue
        try:
            banked = find_banked_candidate(candidate, root=bank.parent if bank else None)
        except CandidateBankRefusal as exc:
            found[candidate] = ({}, exc.code)
            continue
        found[candidate] = (dict(banked.candidate.rear_calibration), "")
    return found


def _applied_stack(profile: Mapping[str, Any] | None) -> dict[str, bool]:
    """Which layers the played candidates carry, from the packet's own sources."""
    profile = profile or {}
    snapshot = profile.get("recomposition_snapshot") or {}
    return {
        "driver": bool(profile_linearization(profile)),
        "room": bool(snapshot.get("room_correction", profile.get("room_correction"))),
        "bass": bool(snapshot.get("bass_extension")),
        "rear": bool(snapshot.get("rear_calibration")),
    }


def _position_rows(
    poses: Mapping[str, Sequence[SeatTake]], zeros: Mapping[str, tuple[np.ndarray, np.ndarray]],
    *, band_hz: Sequence[float] | None, coverage_hz: Sequence[float], handover_hz: float | None,
    incumbent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One candidate's figures at every position the batch froze a zero for."""
    rows: dict[str, Any] = {}
    for key, takes in poses.items():
        if key not in zeros:
            continue
        grid, reference_db = zeros[key]
        _, curve_db = _mean_curve_db(takes, grid)
        rows[key] = position_figures(
            grid, curve_db, reference_db=reference_db, band_hz=band_hz, coverage_hz=coverage_hz,
            handover_hz=handover_hz,
            incumbent=None if incumbent is None else incumbent.get(key),
        )
    return rows


def rear_document(
    inputs: RoundInputs, *, manifest: Mapping[str, Any], calibration_root: Path | None = None,
) -> dict[str, Any]:
    """The rear comparison one finished ``rear`` round carries in its packet.

    A batch spans one manifest set per played candidate, so the document is
    keyed on the INCUMBENT's set and every candidate carries its own
    ``set_id``. Every advertised position is either scored for a candidate or
    disclosed with a reason: ``positions_unscored`` names one the reference
    take missed, and ``across_positions.positions_unavailable`` one a
    candidate itself missed. Predictions belong to a later preview: nothing
    here is a modelled value.
    """
    batch: dict[str, dict[str, list[SeatTake]]] = {}
    bases: dict[str, list[Mapping[str, Any]]] = {}
    on_axis: set[str] = set()
    for row, record, take in analyzed_purpose_takes(
        inputs.session_dir, purpose=PURPOSE_REAR, calibration_root=calibration_root,
    ):
        if take is None:
            continue
        candidate = _candidate_key(record.get("candidate_id"))
        batch.setdefault(candidate, {}).setdefault(take.pose_key, []).append(take)
        bases.setdefault(candidate, []).append(capture_basis(record))
        if row.position_deg == 0 and row.vertical_deg == 0:
            on_axis.add(take.pose_key)
    if not batch:
        raise RoundCapturesRefused(REFUSE_NO_REAR_TAKES, {"purpose": PURPOSE_REAR})
    sets = {_candidate_key(row["capture_basis"].get("candidate_id")): row
            for row in view_sets(manifest)}
    incumbent_id = next((name for name, row in sets.items() if row.get("base")), None)
    if incumbent_id not in batch:
        raise RoundCapturesRefused(REFUSE_NO_INCUMBENT, {"candidates": sorted(batch)})
    for poses in batch.values():
        for takes in poses.values():
            takes.sort(key=lambda take: take.take_id)

    profile, profile_reason = applied_profile_source(inputs.applied_profile_path)
    sections = _rear_sections(inputs, sorted(batch), base=incumbent_id,
                              profile=profile, profile_reason=profile_reason)
    incumbent_section = sections[incumbent_id][0]
    stage = rear_operating_facts(incumbent_section)
    ceiling = room_ceiling(inputs.session_dir)
    takes = [take for poses in batch.values() for group in poses.values() for take in group]
    coverage_hz = [max(take.band_hz[0] for take in takes),
                   min(ceiling.ceiling_hz, min(take.band_hz[1] for take in takes))]
    captured = sorted({key for poses in batch.values() for key in poses})

    muted = sorted(name for name, (section, _) in sections.items()
                   if section.get("rear_muted") is True)
    reference_id = muted[0] if muted else incumbent_id
    zeros: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    reference_on_axis: tuple[np.ndarray, np.ndarray] | None = None
    for key in captured:
        group = batch[reference_id].get(key)
        if not group:
            continue
        grid, mean_db = _mean_curve_db(group)
        zeros[key] = (grid, reference_curve_db(grid, mean_db))
        if key in on_axis and reference_on_axis is None:
            reference_on_axis = (grid, mean_db)
    # A position is advertised only once the batch froze a zero for it, so the
    # advertised list IS the key set of every candidate's own figures; a
    # position the reference missed is named with its reason instead.
    positions = sorted(zeros)
    unscored = {key: REASON_NO_REFERENCE_TAKE for key in captured if key not in zeros}

    geometry = load_declared_geometry(inputs.declared_geometry_path) if inputs.declared_geometry_path else None
    walls, geometry_reason = geometry.boundary_walls() if geometry else ({}, "geometry_undeclared")
    band = comparison_band(
        coverage_hz=coverage_hz, ceiling_hz=ceiling.ceiling_hz,
        reference_take=reference_on_axis, geometric_dip_hz=_wall_dip_hz(walls),
        section_band_hz=stage["band_hz"],
    )
    figures = {
        "band_hz": band["band_hz"], "coverage_hz": coverage_hz, "handover_hz": stage["handover_hz"],
    }
    incumbent_rows = _position_rows(batch[incumbent_id], zeros, **figures)
    # The repeated pose is the only thing a difference may be called
    # inconclusive against, so the batch's spread is the incumbent's there.
    repeated = max(batch[incumbent_id], key=lambda key: (len(batch[incumbent_id][key]), key))
    repeats: list[Mapping[str, Any]] = []
    if repeated in zeros:
        grid, reference_db = zeros[repeated]
        repeats = [position_figures(grid, np.interp(grid, take.freqs_hz, take.magnitude_db),
                                    reference_db=reference_db, **figures)
                   for take in batch[incumbent_id][repeated]]
    spread = {**repeat_spread(repeats), "candidate_id": incumbent_id, "position": repeated}

    incumbent_charge = rear_branch_sum_headroom_db(incumbent_section) if incumbent_section else None
    candidates = []
    for name in sorted(batch):
        section, section_reason = sections[name]
        changed = ([] if name == incumbent_id or not section
                   else changed_section_paths(section, incumbent_section))
        charge = incumbent_charge if name == incumbent_id else (
            rear_branch_sum_headroom_db(section) if section else None)
        rows = incumbent_rows if name == incumbent_id else _position_rows(
            batch[name], zeros, incumbent=incumbent_rows, **figures)
        candidates.append({
            "candidate_id": name, "set_id": (sets.get(name) or {}).get("set_id"),
            "role": ROLE_INCUMBENT if name == incumbent_id
                    else ROLE_REAR_MUTED if section.get("rear_muted") is True else ROLE_VARIANT,
            "changed": changed, "change_family": section_change_family(changed),
            "section_reason": section_reason,
            "headroom_charge_db": charge,
            "headroom_change_db": None if charge is None or incumbent_charge is None
                                  else charge - incumbent_charge,
            "level_db": _shared([basis.get("level_db") for basis in bases[name]]),
            "repeats": {key: len(group) for key, group in sorted(batch[name].items())},
            "positions": rows,
            "across_positions": across_positions(
                rows, incumbent_rows=incumbent_rows, spread_db=spread["spread_db"]),
        })
    observed = [basis for rows in bases.values() for basis in rows]
    return {
        "set_id": sets[incumbent_id]["set_id"],
        "comparison": {
            "band_hz": band["band_hz"], "band_source": band["source"],
            "band_dip_hz": band["dip_hz"], "band_reason": band["reason"],
            "coverage_hz": coverage_hz, "ceiling": ceiling.to_dict(),
            "reference": {"candidate_id": reference_id,
                          "kind": ROLE_REAR_MUTED if reference_id in muted else ROLE_INCUMBENT,
                          "set_id": (sets.get(reference_id) or {}).get("set_id")},
            "positions": positions, "positions_unscored": unscored,
            "level": {
                "session_db": (manifest.get("level") or {}).get("session"),
                **{field: _shared([basis.get(field) for basis in observed]) for field in LEVEL_FIELDS},
                "levels_differ": len({basis.get("level_db") for basis in observed}) > 1,
            },
            "repeat_spread": spread,
        },
        "candidates": candidates,
        "stage": stage,
        "geometry": geometry.to_dict() if geometry else None,
        "geometry_reason": geometry_reason,
        "stack": _applied_stack(profile),
    }
