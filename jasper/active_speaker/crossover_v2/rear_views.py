# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The rear comparison over one banked batch of rear takes (issue #5330).

A SUMMED batch plays, at the same microphone positions and the same session
level, the incumbent tune, the same tune with its rear muted, and one to three
variants that each change one control family. This module selects those takes,
freezes the batch's comparison band and per-position reference curve ONCE, and
hands :mod:`jasper.audio_measurement.rear_evidence` the arrays.

A PAIR batch plays ONE candidate the run composed itself — the applied tune
with its rear calibration cleared, so the two woofers are raw — and banks each
woofer alone beside their sum at every bearing. It carries no candidate
comparison because there is only one played candidate: it says what the two
woofers do separately and how far their superposition may be trusted, which is
what the no-sound preview predicts from.

Code computes, the LLM judges — see ADR-0325 for what a comparison does and
does not claim. Missing evidence carries a reason code and never a
filled-in figure, and nothing here reads which mover placed the microphone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from jasper.active_speaker.baseline_profile import applied_layer_names
from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate
from jasper.active_speaker.measurement_programs import (
    BRANCH_PAIR_FRONT_REAR, POSE_KIND_BEARING, PURPOSE_REAR,
)
from jasper.active_speaker.rear_calibration import (
    changed_section_paths, rear_operating_facts, section_change_family,
)
from jasper.active_speaker.run_manifest import view_sets
from jasper.audio_measurement.measurement_geometry import boundary_prior, load_declared_geometry
from jasper.audio_measurement.rear_evidence import (
    BAND_SOURCE_COVERAGE, IMPULSE_FFT_SIZE, REASON_COVERAGE_SHORT, REASON_NO_COMPARISON, across_positions,
    arrival_gap_ms, comparison_band, confident_arrival_gap_s, gradient_residual_db,
    late_energy_change, pair_band_levels, position_figures, rear_polarity, reference_curve_db,
    repeat_spread, shared_radiating_band_hz, superposition_residual_db, upper_band_levels,
)
from jasper.json_fields import finite_float

from .evidence_packet import applied_profile_source
from .measure_spec import branch_target_ids_for
from .measurement_context import capture_basis
from .position_cycle import curves_for_take, parse_curve_complex
from .room_selection import SeatTake, analyzed_purpose_takes, purpose_take_records
from .room_views import room_ceiling
from .round_captures import RoundCapturesRefused, doc_pose_key
from .round_inputs import RoundInputs, banked_round_of

REFUSE_NO_REAR_TAKES = "rear_no_summed_takes"
REFUSE_NO_INCUMBENT = "rear_incumbent_set_unavailable"
#: A pair round whose takes banked no branch segments. Its OWN reason: falling
#: through to the summed path would report a missing incumbent, which is a
#: question a pair round never asked.
REFUSE_NO_BRANCH_DIAGNOSTIC = "rear_pair_branch_diagnostic_missing"

ROLE_INCUMBENT = "incumbent"
ROLE_REAR_MUTED = "rear_muted"
ROLE_VARIANT = "variant"
ROLE_PAIR = "pair"

#: A pose some candidate measured that the batch could not score: the
#: reference take is missing there, so the position has no frozen zero and no
#: candidate may be read at it. Disclosed, never dropped.
REASON_NO_REFERENCE_TAKE = "no_reference_take"

#: A pose whose pair take did not bank all three segments on one grid, so
#: neither woofer alone nor their sum can be read there.
REASON_SEGMENT_MISSING = "pair_segment_missing"

#: A pair take's three segments: the front woofer alone, the rear woofer alone,
#: then both together. The two solo identities come from the ONE owner of what a
#: ``front_rear`` branch pair excites, in the channel order it plays them.
PAIR_ROLES = (*branch_target_ids_for(BRANCH_PAIR_FRONT_REAR, ()), "summed")

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


def _level_facts(manifest: Mapping[str, Any],
                 observed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The capture facts every take in one batch must share, echoed from the
    takes' own basis, with the session's asked-for level beside them."""
    return {
        "session_db": (manifest.get("level") or {}).get("session"),
        **{field: _shared([basis.get(field) for basis in observed]) for field in LEVEL_FIELDS},
        "levels_differ": len({basis.get("level_db") for basis in observed}) > 1,
    }


def _declared_geometry(inputs: RoundInputs) -> tuple[Any, Mapping[str, float], str]:
    """The round's declared geometry, its boundary walls and why it has none."""
    geometry = (load_declared_geometry(inputs.declared_geometry_path)
                if inputs.declared_geometry_path else None)
    walls, reason = geometry.boundary_walls() if geometry else ({}, "geometry_undeclared")
    return geometry, walls, reason


def _applied_stack(profile: Mapping[str, Any] | None) -> dict[str, bool]:
    """Which layers the played candidates carry, from the packet's own sources."""
    return applied_layer_names(profile)


def _position_rows(
    poses: Mapping[str, Sequence[SeatTake]], zeros: Mapping[str, tuple[np.ndarray, np.ndarray]],
    reference_late: Mapping[str, Sequence[Mapping[str, float]]],
    reference_curve: Mapping[str, np.ndarray],
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
        rows[key]["late_energy"] = late_energy_change(
            [take.late_energy for take in takes if take.late_energy], reference_late.get(key, []),
        )
        rows[key]["upper_bands"] = upper_band_levels(
            grid, curve_db, reference_db=reference_curve[key], coverage_hz=coverage_hz,
        ) if key in reference_curve else []
    return rows


def rear_document(
    inputs: RoundInputs, *, manifest: Mapping[str, Any], calibration_root: Path | None = None,
) -> dict[str, Any]:
    """The rear comparison one finished ``rear`` round carries in its packet.

    A SUMMED batch spans one manifest set per played candidate, so the document
    is keyed on the INCUMBENT's set and every candidate carries its own
    ``set_id``. Every advertised position is either scored for a candidate or
    disclosed with a reason: ``positions_unscored`` names one the reference
    take missed, and ``across_positions.positions_unavailable`` one a
    candidate itself missed. A round the manifest advertises as a PAIR batch
    (:func:`_pair_set`) answers :func:`_pair_document` instead, BEFORE the
    summed analyzer is asked for anything: a branch take's program is one the
    analyzer refuses, so running it first would refuse the whole round.
    Predictions belong to a later preview: nothing here is a modelled value.
    """
    pair_set = _pair_set(manifest)
    if pair_set is not None:
        return _pair_document(inputs, manifest=manifest, pair_set=pair_set)
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
        # An on-axis reference must be a bearing pose: a non-bearing pose at
        # azimuth 0 (e.g. behind the cabinet) is never the front curve the
        # measured-dip search assumes.
        if (row.position_deg == 0 and row.vertical_deg == 0
                and (record.get("pose_kind") or POSE_KIND_BEARING) == POSE_KIND_BEARING):
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
                   min(take.band_hz[1] for take in takes)]
    captured = sorted({key for poses in batch.values() for key in poses})

    muted = sorted(name for name, (section, _) in sections.items()
                   if section.get("rear_muted") is True)
    reference_id = muted[0] if muted else incumbent_id
    zeros: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    reference_late: dict[str, list[Mapping[str, float]]] = {}
    reference_curve: dict[str, np.ndarray] = {}
    reference_on_axis: tuple[np.ndarray, np.ndarray] | None = None
    for key in captured:
        group = batch[reference_id].get(key)
        if not group:
            continue
        grid, mean_db = _mean_curve_db(group)
        zeros[key] = (grid, reference_curve_db(grid, mean_db))
        if muted:
            reference_late[key] = [take.late_energy for take in group if take.late_energy]
            reference_curve[key] = mean_db
        if key in on_axis and reference_on_axis is None:
            reference_on_axis = (grid, mean_db)
    # A position is advertised only once the batch froze a zero for it, so the
    # advertised list IS the key set of every candidate's own figures; a
    # position the reference missed is named with its reason instead.
    positions = sorted(zeros)
    unscored = {key: REASON_NO_REFERENCE_TAKE for key in captured if key not in zeros}

    geometry, walls, geometry_reason = _declared_geometry(inputs)
    band = comparison_band(
        coverage_hz=coverage_hz, ceiling_hz=ceiling.ceiling_hz,
        reference_take=reference_on_axis, geometric_dip_hz=_wall_dip_hz(walls),
        section_band_hz=stage["band_hz"], handover_hz=stage["handover_hz"],
    )
    figures = {
        "band_hz": band["band_hz"], "coverage_hz": coverage_hz, "handover_hz": stage["handover_hz"],
    }
    incumbent_rows = _position_rows(
        batch[incumbent_id], zeros, reference_late, reference_curve, **figures)
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
            batch[name], zeros, reference_late, reference_curve, incumbent=incumbent_rows, **figures)
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
            "level": _level_facts(manifest, observed),
            "repeat_spread": spread,
        },
        "candidates": candidates,
        "stage": stage,
        "geometry": geometry.to_dict() if geometry else None,
        "geometry_reason": geometry_reason,
        "stack": _applied_stack(profile),
    }


def _pair_set(manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The set a pair round banked its SUM under, or ``None`` for a summed round.

    A round banks one manifest set per captured role, so a manifest carrying
    all of :data:`PAIR_ROLES` is the pair batch. The take's own ``regime`` is
    NOT the signal: the flow stamps ``branches`` only beside a branch
    diagnostic, so the very round that banked none reads as an ordinary axis
    take. The sum's set is the document's pointer, named rather than left to
    whichever row a candidate-keyed dict happened to iterate last.
    """
    by_role = {row["capture_basis"].get("role"): row for row in view_sets(manifest)}
    return by_role.get(PAIR_ROLES[-1]) if set(PAIR_ROLES) <= set(by_role) else None


@dataclass(frozen=True)
class PairTake:
    pose_key: str
    pose_kind: str
    sample_rate_hz: int
    freqs_hz: np.ndarray
    front: np.ndarray
    rear: np.ndarray
    coverage_hz: tuple[float, float]
    impulses: dict[str, np.ndarray]
    clock_shift_samples: dict[str, float]


def pair_takes(records: Iterable[Mapping[str, Any]]) -> list[PairTake]:
    takes = []
    for record in records:
        diagnostic = record.get("branch_diagnostic")
        if not isinstance(diagnostic, Mapping):
            continue
        responses = {row.get("role"): row for row in diagnostic.get("responses", ())}
        roles = PAIR_ROLES[:2]
        rate = finite_float(diagnostic.get("sample_rate_hz"))
        if rate is None or rate <= 0 or any(
            role not in responses or not {"pre_guard_samples", "impulse"} <= responses[role].keys()
            for role in roles
        ):
            continue
        start = max(0, int(responses[PAIR_ROLES[0]]["pre_guard_samples"]) - round(0.005 * rate))
        end = min(len(responses[role]["impulse"]) for role in roles)
        freqs = np.fft.rfftfreq(IMPULSE_FFT_SIZE, 1.0 / rate)
        impulses = {role: np.asarray(responses[role]["impulse"], dtype=float)[start:end]
                    for role in roles}
        shifts = {role: float(responses[role].get("clock_shift_samples", 0.0))
                  for role in roles}
        spectra = {role: np.fft.rfft(ir, n=IMPULSE_FFT_SIZE)
                   * np.exp(2j * np.pi * freqs * shifts[role] / rate)
                   for role, ir in impulses.items()}
        bands = [responses[role]["band_hz"] for role in roles]
        takes.append(PairTake(
            doc_pose_key(record), record.get("pose_kind") or POSE_KIND_BEARING, int(rate), freqs,
            spectra[PAIR_ROLES[0]], spectra[PAIR_ROLES[1]],
            (max(band[0] for band in bands), min(band[1] for band in bands)), impulses, shifts,
        ))
    return takes


def _pair_segments(
    record: Mapping[str, Any], manifest: Mapping[str, Any],
) -> tuple[np.ndarray, dict[str, np.ndarray], tuple[float, float]] | None:
    """One pair take's three segments as complex transfers on ONE grid, with
    the band all three were driven over.

    A branch take's segments are analyzed in one call at one FFT size and
    sampled at the nearest native bin, so the three banked curves stand on the
    same frequencies by construction and none is resampled here — a phase
    interpolated across a wrap is simply wrong.

    The MANIFEST is read too: a real pair take's sidecar carries an empty
    ``curves``, and each role's curve rides on that role's own set row
    (:func:`~.position_cycle.curves_for_take`).
    """
    banked = {str(curve.get("role")): curve
              for curve in curves_for_take(record, manifest)}
    parsed = {}
    for role in PAIR_ROLES:
        found = parse_curve_complex(banked[role]) if role in banked else None
        if found is None:
            return None
        parsed[role] = found
    return (parsed[PAIR_ROLES[0]][0],
            {role: transfer for role, (_, transfer, _) in parsed.items()},
            (max(band[0] for _, _, band in parsed.values()),
             min(band[1] for _, _, band in parsed.values())))


def _pair_position(
    records: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any], *, ceiling_hz: float,
) -> tuple[dict[str, Any], np.ndarray] | None:
    """One microphone position's pair evidence, and the grid it was read on.

    The band figures and the trust number come from the FIRST readable repeat
    and the arrival gap is pooled over every one of them: the residual is a
    complex sum, and averaging repeats would smooth away exactly the
    non-linearity it exists to report. The coverage is the band this take
    itself drove, under the room ceiling.
    """
    read = [found for record in records
            if (found := _pair_segments(record, manifest)) is not None]
    if not read:
        return None
    grid, transfers, swept_hz = read[0]
    front, rear, summed = (transfers[role] for role in PAIR_ROLES)
    coverage_hz = [swept_hz[0], min(ceiling_hz, swept_hz[1])]
    bands = pair_band_levels(grid, front_tf=front, rear_tf=rear, pair_tf=summed,
                             coverage_hz=coverage_hz)
    band_hz = [bands[0]["band_hz"][0], bands[-1]["band_hz"][1]] if bands else None
    takes = pair_takes(records)
    repeats = [(take.impulses[PAIR_ROLES[0]], take.impulses[PAIR_ROLES[1]],
                take.clock_shift_samples[PAIR_ROLES[1]] - take.clock_shift_samples[PAIR_ROLES[0]])
               for take in takes]
    rate = takes[0].sample_rate_hz if takes else 0
    gap = arrival_gap_ms(
        repeats if rate else (), sample_rate_hz=int(rate or 0),
        band_hz=shared_radiating_band_hz(grid, front_tf=front, rear_tf=rear, band_hz=swept_hz),
    )
    # The trust number has its own band gate, so a row whose residual is absent
    # is not a clean read however many bands the levels answered.
    residual = superposition_residual_db(grid, front_tf=front, rear_tf=rear, pair_tf=summed,
                                         band_hz=band_hz)
    return {
        "reason": "" if bands and residual is not None else REASON_COVERAGE_SHORT,
        "coverage_hz": coverage_hz, "band_hz": band_hz, "bands": bands, "arrival_gap": gap,
        "superposition_residual_db": residual,
        "rear_polarity": rear_polarity(grid, front_tf=front, rear_tf=rear, band_hz=band_hz,
                                       arrival_gap=gap),
    }, grid


def _composed_source(inputs: RoundInputs, candidate: str) -> dict[str, Any]:
    """Where the played candidate came from: the applied fingerprint the run
    recorded when it composed the cleared tune, and which section it cleared.
    A candidate the bank cannot answer carries that refusal code instead."""
    bank = banked_round_of(inputs.session_dir)
    try:
        found = find_banked_candidate(candidate, root=bank.parent if bank else None)
    except CandidateBankRefusal as exc:
        return {"candidate_id": None, "resolution": None, "reason": exc.code}
    analysis = found.candidate.analysis
    return {"candidate_id": (analysis.get("base") or {}).get("fingerprint"),
            "resolution": (analysis.get("resolution") or {}).get("rear_calibration"),
            "reason": ""}


def _pair_document(
    inputs: RoundInputs, *, manifest: Mapping[str, Any], pair_set: Mapping[str, Any],
) -> dict[str, Any]:
    """The pair take's evidence: each woofer alone, their sum, and the trust number.

    Reads the banked take RECORDS (:func:`purpose_take_records`) rather than
    asking the summed analyzer, which refuses a branch take's two-channel
    program outright. ONE played candidate, so there is no candidate comparison
    and no frozen reference curve — nothing has been changed yet to compare
    against. The document is keyed on the SUM's manifest set (``pair_set``),
    the one of the round's three role-scoped sets the pair figures are read
    from. ``stage`` is the APPLIED document's rear section, whose
    ``gradient_residual_db`` is a fact about that document at the measured gap
    rather than about this take.
    """
    records: dict[str, list[Mapping[str, Any]]] = {}
    observed: list[Mapping[str, Any]] = []
    for _row, record in purpose_take_records(inputs.session_dir, purpose=PURPOSE_REAR):
        records.setdefault(doc_pose_key(record), []).append(record)
        observed.append(capture_basis(record))
    if not records:
        raise RoundCapturesRefused(REFUSE_NO_REAR_TAKES, {"purpose": PURPOSE_REAR})
    every = [record for rows in records.values() for record in rows]
    candidate = _shared([_candidate_key(record.get("candidate_id")) for record in every]) or ""
    if not any(record.get("branch_diagnostic") for record in every):
        raise RoundCapturesRefused(REFUSE_NO_BRANCH_DIAGNOSTIC, {
            "candidates": sorted({_candidate_key(record.get("candidate_id"))
                                  for record in every}),
            "takes": len(every),
        })
    ceiling = room_ceiling(inputs.session_dir)
    positions: dict[str, Any] = {}
    unscored: dict[str, str] = {}
    grids: list[np.ndarray] = []
    for key, rows in sorted(records.items()):
        found = _pair_position(sorted(rows, key=lambda record: str(record.get("take_id") or "")),
                               manifest, ceiling_hz=ceiling.ceiling_hz)
        if found is None:
            unscored[key] = REASON_SEGMENT_MISSING
            continue
        positions[key], grid = found
        grids.append(grid)

    profile, _profile_reason = applied_profile_source(inputs.applied_profile_path)
    section = ((profile or {}).get("recomposition_snapshot") or {}).get("rear_calibration") or {}
    band_hz = _shared([row["band_hz"] for row in positions.values()])
    coverage_hz = _shared([row["coverage_hz"] for row in positions.values()])
    # One document-level figure, so it reads the MEDIAN of the BEARING
    # positions' gaps that may be built on: a non-bearing position (e.g. a mic
    # behind the cabinet) measures a different physical quantity and must not
    # blend into this median. With none of them confident, and with no
    # electrical chain to evaluate, it is simply absent.
    held = [gap for key, row in positions.items()
            if (records[key][0].get("pose_kind") or POSE_KIND_BEARING) == POSE_KIND_BEARING
            and (gap := confident_arrival_gap_s(row["arrival_gap"])) is not None]
    ratio = None
    if grids and section.get("case") == "electrical_dsp":
        summed, front = rear_stage_response(section, grids[0])
        ratio = None if not np.all(front) else summed / front
    geometry, _walls, geometry_reason = _declared_geometry(inputs)
    set_id = pair_set.get("set_id")
    return {
        "set_id": set_id,
        "comparison": {
            "band_hz": band_hz, "band_source": BAND_SOURCE_COVERAGE, "band_dip_hz": None,
            "band_reason": "" if band_hz else REASON_COVERAGE_SHORT,
            "coverage_hz": coverage_hz, "ceiling": ceiling.to_dict(),
            "reference": {"candidate_id": candidate, "kind": ROLE_PAIR, "set_id": set_id},
            "positions": sorted(positions), "positions_unscored": unscored,
            "level": _level_facts(manifest, observed),
            # The summed path's spread answers "how small a candidate
            # difference is real?" A pair batch compares no candidates, so it
            # reads none of those figures; its repeat evidence rides on each
            # position's own ``arrival_gap``.
            "repeat_spread": {**repeat_spread(()), "reason": REASON_NO_COMPARISON,
                              "candidate_id": candidate, "position": None},
        },
        "candidates": [],
        "pair": {"candidate_id": candidate, "source": _composed_source(inputs, candidate),
                 "positions": positions},
        "stage": {
            **rear_operating_facts(section),
            "gradient_residual_db": None if ratio is None else gradient_residual_db(
                grids[0], ratio, float(np.median(held)) if held else None, band_hz),
        },
        "geometry": geometry.to_dict() if geometry else None,
        "geometry_reason": geometry_reason,
        "stack": _applied_stack(profile),
    }
