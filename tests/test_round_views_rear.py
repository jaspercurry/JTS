# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The rear comparison a finished ``rear`` round carries in its packet.

A summed round's curves are synthesized from a direct sound plus one rigid
image source at the declared wall; a pair round's are two ideal woofers one
delay apart. These pins prove arithmetic and plumbing only — no figure here is
evidence about a real cabinet.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence
from unittest.mock import Mock

import numpy as np
import pytest

from jasper.active_speaker.measurement_programs import BASE_CANDIDATE
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
from jasper.active_speaker.candidate_bank import CandidateBankRefusal
from jasper.active_speaker import measurement_analysis
from jasper.active_speaker.crossover_v2 import rear_pair_round, rear_views, room_selection
from jasper.active_speaker.crossover_v2.rear_views import REAR_SCORE_CAP_DB
from jasper.active_speaker.crossover_v2.pose_curve import LateralPoseCurve, pose_curve_record
from jasper.active_speaker.crossover_v2.record_index import measurement_documents, record_path
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds, round_inputs
from jasper.active_speaker.crossover_v2.take_impulses import write_take_impulses
from jasper.active_speaker.measurement_programs import POSE_KIND_BEHIND
from jasper.active_speaker.round_bank import _bookkeeping, bank_round
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.round_packet import write_round_packet
from jasper.active_speaker.round_view_artifacts import ARTIFACT_BY_VIEW
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.band_ladders import (
    ARRIVAL_GAP_BAND_HZ, BAND_LADDERS, BASS_BANDS_HZ, FRONT_GUARD_BANDS_HZ, LEVEL_BANDS_HZ,
    THIRD_OCTAVE_BASS_BANDS_HZ, UPPER_BANDS_HZ,
)
from jasper.audio_measurement.evidence_reasons import (
    EvidenceUnavailable,
    REASON_COVERAGE_SHORT, REASON_NO_COMPARISON, REASON_NO_EARLIER_REFERENCE, REASON_NO_REPEATS,
    REASON_POLARITY_SNR_SHORT, REASON_TOO_FEW_POSITIONS, REASON_UNREADABLE, TAKE_CURVES_NOT_BANKED,
)
from jasper.audio_measurement.rear_evidence import POLARITY_INVERTED, POLARITY_SAME, POLARITY_UNCLEAR
from jasper.audio_measurement.recorded_impulse import RecordedImpulse
from jasper.audio_measurement.seat_figures import (
    BAND_SOURCE_DECLARED_GEOMETRY, BAND_SOURCE_MEASURED_DIP, band_level_changes,
)
from jasper.cli import round_views
from tests.crossover_v2_banked_round import (
    SEAT_BAND_HZ, SEAT_GRID_HZ, _reopen, bank_seat_round,
)
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.room_median_fixture import analyzed_room_documents as analyzed_room_documents
from tests.test_active_speaker_audition import _applied_profile
from tests.active_speaker_fixtures import _active_topology, rear_seed_document
from tests.test_crossover_v2_round_frequency_view import summed_capture_bundle as summed_capture_bundle

#: The declared cabinet, and the wall bounce it predicts (ADR-0317): a rigid
#: image source one excess path length away nulls at ``c / 4d``.
_CABINET = {"cabinet_back_wall_m": 0.2032, "cabinet_depth_m": 0.3, "toe_in_degrees": 0}
_WALL_M = _CABINET["cabinet_back_wall_m"] + _CABINET["cabinet_depth_m"]
_DIP_HZ = DEFAULT_SOUND_SPEED_M_S / (4.0 * _WALL_M)

#: The incumbent's branch corners, and the hand-over they name. Chosen so the
#: hand-over window sits below the comparison band the wall dip opens.
_BASS_LOWPASS_HZ = 90.0
_CANCELLATION_BAND_HZ = [40.0, 300.0]
_HANDOVER_HZ = math.sqrt(_BASS_LOWPASS_HZ * _CANCELLATION_BAND_HZ[0])

_MUTED = "muted-fingerprint"
_VARIANT = "variant-fingerprint"
_SAMPLE_RATE_HZ = 48000

#: A pair round's ONE played candidate: its parent, which every take played with
#: its rear calibration cleared (ADR-0386).
_PARENT = "parent-fingerprint"
#: A pair round banks one set per captured role; the SUM's is the document's.
_PAIR_SET_ID = f"{_PARENT}-{rear_views.PAIR_ROLES[-1]}"

#: The pair fixture's two woofers: the rear arrives this much later, inverted,
#: and this much quieter. SHAPE knobs — no real cabinet is claimed.
_PAIR_GAP_MS = 0.8
_PAIR_LEVEL_GAP_DB = -2.0
_FRONT_ARRIVAL_S = 0.005
_PULSE_SAMPLES = 4096


def _combo(kind: str, freq_hz: float) -> dict:
    return {"type": "BiquadCombo",
            "parameters": {"type": kind, "freq": freq_hz, "order": 4}}


def _rear_document(*, muted: bool = False, bass_lowpass_hz: float = _BASS_LOWPASS_HZ) -> dict:
    document = rear_seed_document()
    document["rear_muted"] = muted
    document["rear"]["bass"]["filters"] = [_combo("LinkwitzRileyLowpass", bass_lowpass_hz)]
    document["rear"]["cancellation"]["filters"] = [
        _combo("LinkwitzRileyHighpass", _CANCELLATION_BAND_HZ[0]),
        _combo("LinkwitzRileyLowpass", _CANCELLATION_BAND_HZ[1]),
    ]
    return document


#: Each played candidate's rear section; the variant moves ONE control family
#: (the bass branch's corner) and the incumbent is the saved tune.
_SECTIONS = {
    BASE_CANDIDATE: _rear_document(),
    _MUTED: _rear_document(muted=True),
    _VARIANT: _rear_document(bass_lowpass_hz=120.0),
}


#: A low room-mode-like dip added to the reference curve below (issue #5330's
#: own jts3 round): deeper than the wall dip, so an unwindowed search picks
#: it over the true wall dip unless the search is windowed to the geometry.
_LOW_DIP_HZ = 38.0
_LOW_DIP_DB = 20.0


def _wall_curve_db(strength: float, *, output_db: float = 0.0, hole_db: float = 0.0,
                   low_dip_db: float = 0.0) -> list[float]:
    """Direct sound plus one rigid image source below 300 Hz, minus an optional hand-over
    hole and an optional deeper low-frequency room-mode notch."""
    excess_s = 2.0 * _WALL_M / DEFAULT_SOUND_SPEED_M_S
    summed = 1.0 + strength * np.exp(-2j * np.pi * SEAT_GRID_HZ * excess_s)
    summed = np.where(SEAT_GRID_HZ >= 300.0, 1.0, summed)
    hole = hole_db * np.exp(-0.5 * (np.log2(SEAT_GRID_HZ / _HANDOVER_HZ) / 0.12) ** 2)
    low = low_dip_db * np.exp(-0.5 * (np.log2(SEAT_GRID_HZ / _LOW_DIP_HZ) / 0.2) ** 2)
    return (-30.0 + output_db + 20.0 * np.log10(np.abs(summed)) - hole - low).tolist()


#: A muted rear radiates into the wall and digs a deep dip (plus the room's
#: own low-frequency feature, deeper still); the incumbent's cardioid leaves
#: a shallow one; the variant keeps that dip but loses output and opens a
#: hole where its moved corner no longer hands over.
_CURVES = {
    BASE_CANDIDATE: _wall_curve_db(0.4),
    _MUTED: _wall_curve_db(0.95, low_dip_db=_LOW_DIP_DB),
    _VARIANT: _wall_curve_db(0.4, output_db=-3.0, hole_db=8.0),
}


#: A candidate composed with ``"rear_calibration": null``: it plays no rear stage.
_CLEARED = "cleared-fingerprint"


@pytest.fixture
def banked_candidates(monkeypatch):
    """The candidate bank, which answers a fingerprint with its rear section."""
    banked = {**_SECTIONS, _CLEARED: {}}

    def find(fingerprint, *, root=None):
        if fingerprint not in banked:
            raise CandidateBankRefusal("not_found", fingerprint)
        return SimpleNamespace(candidate=SimpleNamespace(
            rear_calibration=banked[fingerprint], analysis={}))

    monkeypatch.setattr(rear_views, "find_banked_candidate", find)


def _banked(store: Any, records: Sequence[Mapping[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
    """Every record through the product's own banker, with the path it filed it at."""
    async def bank() -> list[str]:
        return [await store.bank(record) for record in records]

    return list(zip(asyncio.run(bank()), records))


def _round_source(root: Path) -> tuple[dict, Any]:
    """The seat round's own record as a take template, and its reopened store."""
    inputs = round_inputs(root)
    source = next(record for _, record in measurement_documents(inputs.session_dir))
    store = _reopen(root)
    return {key: value for key, value in source.items()
            # The store writes these two and refuses a record that carries them.
            if key not in ("schema_version", "capture_session_id")}, store


def _round_environment(root: Path, *, applied: Mapping[str, Any] | None) -> None:
    """The declared cabinet and the applied tune a rear round reads; ``None`` applies no rear stage."""
    DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0,
                     **_CABINET).save(root / "declared-geometry.json")
    profile = _applied_profile(_active_topology("mono", "active_2_way"))
    profile.update(kind=BASELINE_PROFILE_KIND, artifact_schema_version=SCHEMA_VERSION)
    if applied is not None:
        profile["recomposition_snapshot"]["rear_calibration"] = applied
    (root / "applied-profile.json").write_text(json.dumps(profile))


def _poses(repeats: int) -> list[tuple[int, int]]:
    """The bearings a rear round walks, on-axis repeated."""
    return [(0, index + 1) for index in range(repeats)] + [(-20, 1), (20, 1)]


def rear_round(tmp_path: Path, *, candidates=(BASE_CANDIDATE, _MUTED, _VARIANT),
               repeats: int = 2, missing: Mapping[str, Sequence[int]] = {},
               on_axis_kind: str = "bearing", name: str = "r3-seat",
               curves: Mapping[str, list[float] | None] = _CURVES, retake: Mapping[str, float] = {},
               unlisted: Mapping[str, Sequence[int]] = {}, failed: Mapping[str, Sequence[int]] = {},
               band_hz: Sequence[float] | None = None, superseded: bool = False,
               behind: Mapping[str, tuple[Sequence[float], Sequence[tuple[float, float]] | None]] = {},
               probe: bool = False) -> Path:
    """One banked ``rear`` round, ``name`` in the one rear store: every
    candidate at every pose, on-axis repeated, each playing its ``curves``
    over ``band_hz`` (the seat round's by default). A candidate's ``None``
    curve banks none, as the banker did before ADR-0383; ``failed`` banks a
    failed analysis instead at named bearings, as the banker does since.
    ``superseded`` banks every take's program as schema 2 did, before #5991
    renamed its ``program_id``.

    ``missing`` drops a candidate's take at named bearings, which is how a
    reference take goes missing where other candidates measured. ``retake``
    adds one on-axis retake, that many dB off, that the manifest deselects;
    ``unlisted`` leaves a candidate's takes at named bearings out of its
    manifest set. ``on_axis_kind`` banks every azimuth-0 take as a non-bearing
    pose, for the on-axis-reference guard (review, PR #5362). ``behind`` adds
    one take behind the cabinet per candidate it names: its ungated curve,
    and the ``(seconds, dB)`` arrivals its kept impulse holds, which only the
    windowed read sees (``None`` keeps no impulse). ``probe`` banks the run's
    probe of the first take first, in a set of its own.
    """
    root = bank_seat_round(tmp_path / "rear", name=name)
    source, store = _round_source(root)
    base = {key: value for key, value in source.items() if key != "curves"}
    if superseded:
        base["program"] = {**{key: value for key, value in source["program"].items() if key != "stimulus_id"},
                           "schema_version": 2, "program_id": source["program"]["stimulus_id"]}
    groups = []
    for candidate in candidates:
        records = []
        poses = [*_poses(repeats), *([(0, repeats + 1)] if candidate in retake else [])]
        for degrees, repeat in poses:
            if degrees in missing.get(candidate, ()):
                continue
            take_id = f"{candidate}-{degrees}-{repeat}"
            offset = retake[candidate] if repeat > repeats else 0.0
            analysed = curves[candidate] is not None and degrees not in failed.get(candidate, ())
            records.append({**base, "take_id": take_id, "position_id": take_id, "repeat": repeat,
                            "pose_kind": on_axis_kind if degrees == 0 else "bearing",
                            "position_deg": degrees, "vertical_deg": 0,
                            "mark_distance_m": 1.0, "measurement_purpose": "rear",
                            "gating_applied": False, "graph_scope": "candidate",
                            "candidate_id": candidate, "level_db": -30.0,
                            "seat_offset_m": [0.0, 0.0, 0.0] if on_axis_kind == "seat" and degrees == 0 else None,
                            **({"analysis_error": {"code": "internal_error", "error_type": "ValueError"}}
                               if degrees in failed.get(candidate, ()) else {}),
                            **({} if not analysed else {"curves": [{
                                **source["curves"][0], **({"band_hz": list(band_hz)} if band_hz else {}),
                                "magnitude_db": (np.asarray(curves[candidate]) + offset).tolist(),
                                "late_energy": {
                                    "t0_ms": 5.0, "energy_db": -20.0,
                                    "early_late_db": 1.0 if candidate == _MUTED else 4.0,
                                    "centroid_ms": 6.0 if candidate == _MUTED else 5.0,
                                }}]})})
        if candidate in behind:
            curve_db, arrivals = behind[candidate]
            take_id = f"{candidate}-behind-1"
            records.append({**base, "take_id": take_id, "position_id": take_id, "repeat": 1,
                            "pose_kind": POSE_KIND_BEHIND, "position_deg": 0, "vertical_deg": 0,
                            "mark_distance_m": 0.5, "measurement_purpose": "rear",
                            "gating_applied": False, "graph_scope": "candidate",
                            "candidate_id": candidate, "level_db": -30.0, "seat_offset_m": None,
                            **({} if arrivals is None else {"impulses": _kept_impulse(root, take_id, arrivals)}),
                            "curves": [{**source["curves"][0], "magnitude_db": list(curve_db)}]})
        group = manifest_set(_banked(store, records), set_id=candidate)
        group["base"] = candidate == BASE_CANDIDATE
        group["takes"] = [dict(take, selected=take["selected"] and take["take_id"] != f"{candidate}-0-{repeats + 1}")
                          for take in group["takes"] if take["pose"]["azimuth_deg"] not in unlisted.get(candidate, ())]
        groups.append(group)
    write_manifest(root, program="rear/express", groups=groups, probe=probe)
    _round_environment(root, applied=_SECTIONS[BASE_CANDIDATE])
    return root


def _pulse(arrival_s: float, *, gain: float = 1.0, inverted: bool = False) -> list[float]:
    """One arrival at ``arrival_s`` and nothing else in the window, from linear
    phase so a fractional-sample arrival is exact."""
    freqs = np.fft.rfftfreq(_PULSE_SAMPLES, d=1.0 / _SAMPLE_RATE_HZ)
    pulse = np.fft.irfft(np.exp(-2j * np.pi * freqs * arrival_s), n=_PULSE_SAMPLES)
    return (pulse * (-gain if inverted else gain)).tolist()


def _kept_impulse(root: Path, take_id: str, arrivals: Sequence[tuple[float, float]]) -> dict:
    """The block a take keeps its summed impulse under (ADR-0354), one pulse per
    ``(seconds, dB)`` arrival, written by the product's own writer."""
    samples = np.sum([_pulse(at_s, gain=10.0 ** (level_db / 20.0)) for at_s, level_db in arrivals], axis=0)
    impulse = RecordedImpulse(samples, _SAMPLE_RATE_HZ, 0, "sweep_verify")
    summed = SimpleNamespace(role="summed", repeat_index=0, impulse=impulse, repeat_responses=())
    return write_take_impulses(round_inputs(root).session_dir, take_id,
                               SimpleNamespace(driver_responses=(), summed_response=summed), recording=None)


def _pair_curves(band_hz: Sequence[float] = SEAT_BAND_HZ, *, noise_below_hz: float | None = None) -> list[dict]:
    """The three segments a pair take banks, in the product's own curve shape:
    each woofer alone and their exact sum, so the trust number reads zero.
    ``noise_below_hz`` puts the woofers in phase, with an inverted rear below it,
    as the rumble in jts3's 22–45 Hz bands read (#5404 item 3)."""
    gain = 10.0 ** (_PAIR_LEVEL_GAP_DB / 20.0)
    front = np.ones_like(SEAT_GRID_HZ, dtype=np.complex128)
    sign = -1.0 if noise_below_hz is None else np.where(SEAT_GRID_HZ < noise_below_hz, -1.0, 1.0)
    rear = sign * gain * np.exp(-2j * np.pi * SEAT_GRID_HZ * _PAIR_GAP_MS / 1000.0)
    return [pose_curve_record(LateralPoseCurve(role=role, freqs_hz=SEAT_GRID_HZ,
                                               complex_tf=transfer,
                                               band_hz=(band_hz[0], band_hz[1])))
            for role, transfer in zip(rear_views.PAIR_ROLES, (front, rear, front + rear))]


def _branch_program(summed: Mapping[str, Any]) -> dict:
    """The two-channel branch program a pair take plays, through the PRODUCTION
    composer so the schedule's own ``stimulus_id`` stays valid — the shape the
    summed analyzer refuses (``channels == 2``)."""
    front, rear, _summed = rear_views.PAIR_ROLES
    return build_branch_program(
        ExcitationProgram.from_dict(dict(summed)), {front: 0, rear: 1}).to_dict()


def _branch_diagnostic(gap_ms: float = _PAIR_GAP_MS) -> dict:
    front = np.asarray(_pulse(_FRONT_ARRIVAL_S))
    rear = np.asarray(_pulse(_FRONT_ARRIVAL_S + gap_ms / 1000.0,
                             gain=10.0 ** (_PAIR_LEVEL_GAP_DB / 20.0), inverted=True))
    return {"sample_rate_hz": _SAMPLE_RATE_HZ, "responses": [
        {"role": role, "clock_shift_samples": 0.0, "band_hz": list(SEAT_BAND_HZ),
         "pre_guard_samples": round(_FRONT_ARRIVAL_S * _SAMPLE_RATE_HZ), "impulse": impulse.tolist()}
        for role, impulse in zip(rear_views.PAIR_ROLES, (front, rear, front + rear))
    ]}


def test_pair_takes_clamp_the_window_and_skip_incomplete_solos():
    diagnostic = _branch_diagnostic()
    diagnostic["responses"] = diagnostic["responses"][:2]
    diagnostic["responses"][0]["pre_guard_samples"] = 0
    take, = rear_views.pair_takes([{"branch_diagnostic": diagnostic, "pose_kind": "bearing"}])
    assert all(len(impulse) == _PULSE_SAMPLES for impulse in take.impulses.values())
    for rate in (None, 0, -1, float("nan"), float("inf"), "48000"):
        assert rear_views.pair_takes([{"branch_diagnostic": {**diagnostic, "sample_rate_hz": rate}}]) == []
    for key in ("role", "pre_guard_samples", "impulse"):
        missing = {**diagnostic, "responses": [
            {field: value for field, value in response.items() if field != key}
            for response in diagnostic["responses"]]}
        assert rear_views.pair_takes([{"branch_diagnostic": missing}]) == []


def _band_snr(snr_db: Callable[[float], float]) -> dict:
    """The band SNR a take's summed sweep banks on the bass ladder (#5737 C4), by band floor."""
    return {"segment_id": "sweep_verify", "quiet_samples": [0, _SAMPLE_RATE_HZ],
            "bands": [{"band_hz": list(band), "estimated_snr_db": snr_db(band[0])} for band in BASS_BANDS_HZ]}


def pair_round(tmp_path: Path, *, repeats: int = 2, missing: Sequence[int] = (),
               applied: str = BASE_CANDIDATE, diagnostic: bool = True,
               swept_hz: Sequence[float] = SEAT_BAND_HZ,
               off_axis_gap_ms: float | None = None,
               behind_gap_ms: float | None = None, noise_below_hz: float | None = None,
               snr_db: Callable[[float], float] | None = lambda lo_hz: 40.0) -> Path:
    """One banked ``rear/pair`` round: its parent at every pose,
    each take banking both woofers alone, their sum and the branch diagnostic.

    Every take carries the shape the runner really banks — a two-channel
    ``candidate_branches`` program, which the summed analyzer refuses outright.
    The manifest carries the THREE role-scoped sets, each row pointing at its
    take's record, which banks every role's curve. ``missing`` drops the solo
    segments at named bearings; ``diagnostic`` false banks takes that analyzed
    no branches, the shape jts3 produced before #5361; ``swept_hz`` narrows the
    curves' own band so the band figures run out of bands to read.
    ``off_axis_gap_ms`` gives every off-axis bearing its own measured gap,
    distinct from on-axis, so a document-level pooling figure can be told apart
    from a single shared gap. ``behind_gap_ms`` additionally banks one pose
    behind the cabinet (kind ``behind``, 0.1 m) with its own gap — the
    ``rear/pair_behind`` shape (#5362) — to pin that a document-level figure
    pools bearing positions only (review, PR #5362). ``noise_below_hz`` shapes
    the woofers as :func:`_pair_curves` does, and ``snr_db`` banks each take's
    band SNR by band floor, ``None`` none.
    """
    root = bank_seat_round(tmp_path / "pair")
    source, store = _round_source(root)
    branch_program = _branch_program(source["program"])
    snr = {} if snr_db is None else {"analysis": {"bass": _band_snr(snr_db)}}
    records = []
    for degrees, repeat in _poses(repeats):
        take_id = f"{_PARENT}-{degrees}-{repeat}"
        curves = source["curves"] if degrees in missing else _pair_curves(swept_hz, noise_below_hz=noise_below_hz)
        gap_ms = off_axis_gap_ms if degrees != 0 and off_axis_gap_ms is not None else _PAIR_GAP_MS
        records.append({**source, "take_id": take_id, "position_id": take_id, "repeat": repeat,
                        "pose_kind": "bearing", "position_deg": degrees, "vertical_deg": 0,
                        "mark_distance_m": 1.0, "measurement_purpose": "rear",
                        "gating_applied": False, "graph_scope": "candidate_branches",
                        "program": branch_program,
                        "candidate_id": _PARENT, "cleared_layers": ["rear_calibration"],
                        "level_db": -30.0, "seat_offset_m": None,
                        **({"regime": "branches",
                            "branch_diagnostic": _branch_diagnostic(gap_ms)} if diagnostic else {}),
                        **snr, "curves": curves})
    if behind_gap_ms is not None:
        take_id = f"{_PARENT}-behind-1"
        records.append({**source, "take_id": take_id, "position_id": take_id, "repeat": 1,
                        "pose_kind": POSE_KIND_BEHIND, "position_deg": 0, "vertical_deg": 0,
                        "mark_distance_m": 0.1, "measurement_purpose": "rear",
                        "gating_applied": False, "graph_scope": "candidate_branches",
                        "program": branch_program,
                        "candidate_id": _PARENT, "cleared_layers": ["rear_calibration"],
                        "level_db": -30.0, "seat_offset_m": None,
                        **({"regime": "branches",
                            "branch_diagnostic": _branch_diagnostic(behind_gap_ms)}
                           if diagnostic else {}),
                        **snr, "curves": _pair_curves(swept_hz, noise_below_hz=noise_below_hz)})
    banked = _banked(store, records)
    groups = []
    # Role order as the runner banks it, the SUM first — so a reader that kept
    # whichever set iterated last would name a solo woofer's instead.
    for role in sorted(rear_views.PAIR_ROLES):
        group = manifest_set(banked, set_id=f"{_PARENT}-{role}")
        group["capture_basis"].update(role=role, candidate_id=_PARENT)
        groups.append(group)
    write_manifest(root, program="rear/pair", groups=groups)
    _round_environment(root, applied=_SECTIONS[applied])
    return root


def packet_of(root: Path) -> tuple[dict, list[dict]]:
    inputs = round_inputs(root)
    manifest_path, views = _bookkeeping(root, inputs.session_dir, round_views.run_bookkeeping)
    return write_round_packet(root, manifest_path, views), views


def test_newest_front_pair_round_ignores_identity_and_skips_newer_non_pairs(tmp_path):
    pair = pair_round(tmp_path)
    root = pair.parent
    paths = [pair, shutil.copytree(pair, root / "newer-pair"),
             shutil.copytree(pair, root / "behind-only"), shutil.copytree(pair, root / "no-pair")]
    for index, path in enumerate(paths):
        (path / "provenance.json").write_text(json.dumps({"banked_at_utc": f"2026-09-{17 + index}T12:00:00Z"}))
        (path / "packet.json").write_text(json.dumps({"applied": {"candidate": "old-identity"}}))
        if index >= 2:
            for row, record in room_selection.purpose_take_records(round_inputs(path).session_dir, purpose="rear"):
                record = dict(record)
                if index == 2:
                    record["pose_kind"] = "behind"
                else:
                    record.pop("branch_diagnostic", None)
                (round_inputs(path).session_dir / record_path(row)).write_text(json.dumps(record))
        os.utime(path, (100 + index, 100 + index))
    selected = rear_pair_round.newest_rear_pair_round(root)
    assert selected == {"round_dir": paths[1], "round_id": paths[1].name,
                        "banked_at": "2026-09-18T12:00:00Z"}
    assert rear_pair_round.newest_rear_pair_round(root, limit=2) is None
    os.utime(pair, (200, 200))
    assert rear_pair_round.newest_rear_pair_round(root) == selected
    pending = shutil.copytree(pair, root / "banking")
    provenance = pending / "provenance.json"
    provenance.unlink()
    assert rear_pair_round.newest_rear_pair_round(root) == selected
    provenance.write_text(json.dumps({"banked_at_utc": "2026-09-21T12:00:00Z"}))
    assert rear_pair_round.newest_rear_pair_round(root)["round_id"] == "banking"


def test_pair_round_window_ignores_authored_entries(tmp_path):
    pair = pair_round(tmp_path)
    (pair / "provenance.json").write_text("{}")
    os.utime(pair, (100, 100))
    for index in range(40):
        authored = pair.parent / f"authored-{index:064x}"
        authored.mkdir()
        os.utime(authored, (200 + index, 200 + index))
    selected = rear_pair_round.newest_rear_pair_round(pair.parent, limit=32)
    assert selected is not None and selected["round_dir"] == pair


@pytest.mark.parametrize("limit", [4, 128])
@pytest.mark.parametrize("inside", [True, False])
def test_pair_round_window_bounds_rounds_by_banked_time(tmp_path, limit, inside):
    pair = pair_round(tmp_path)
    (pair / "provenance.json").write_text(json.dumps({"banked_at_utc": "2026-09-20T12:00:00Z"}))
    os.utime(pair, (200, 200))
    for index in range(limit + 1):
        path = pair.parent / f"round-{index}"
        (path / "bundle" / "session").mkdir(parents=True)
        newer = index < limit - int(inside)
        (path / "provenance.json").write_text(json.dumps({
            "banked_at_utc": "2026-09-21T12:00:00Z" if newer else "2026-09-19T12:00:00Z"}))
        os.utime(path, (100, 100))
    selected = rear_pair_round.newest_rear_pair_round(pair.parent, **({"limit": limit} if limit == 4 else {}))
    assert (selected["round_dir"] if selected else None) == (pair if inside else None)
    assert rear_pair_round.newest_rear_pair_round(pair.parent, limit=0) is None


@pytest.mark.parametrize("case,expected", [
    ("pair", True), ("behind", False), ("no_rear", False),
    ("split_roles", False), ("missing_impulse", False), ("invalid_rate", False),
])
def test_front_pair_round_uses_records_without_spectra(tmp_path, monkeypatch, case, expected):
    pair = pair_round(tmp_path, repeats=1)
    (pair / "provenance.json").write_text("{}")
    session = round_inputs(pair).session_dir
    for index, (row, record) in enumerate(room_selection.purpose_take_records(session, purpose="rear")):
        record = dict(record)
        if case == "behind":
            record["pose_kind"] = "behind"
        elif case == "no_rear":
            record["measurement_purpose"] = "room"
        elif case == "split_roles":
            record["branch_diagnostic"]["responses"] = [record["branch_diagnostic"]["responses"][index % 2]]
        elif case == "missing_impulse":
            record["branch_diagnostic"]["responses"][0].pop("impulse")
        elif case == "invalid_rate":
            record["branch_diagnostic"]["sample_rate_hz"] = 0
        (session / record_path(row)).write_text(json.dumps(record))
    spectra = Mock(side_effect=AssertionError("selector built spectra"))
    monkeypatch.setattr(rear_views, "pair_takes", spectra)
    monkeypatch.setattr(np.fft, "rfft", spectra)
    records = Mock(wraps=rear_pair_round.purpose_take_records)
    monkeypatch.setattr(rear_pair_round, "purpose_take_records", records)
    for _ in range(2):
        assert rear_pair_round._front_pair_round(pair) is expected
        selected = rear_pair_round.newest_rear_pair_round(pair.parent)
        assert (selected is not None) is expected
    records.assert_called_once()
    spectra.assert_not_called()


@pytest.mark.parametrize("error", [OSError(), ValueError(), EvidenceUnavailable("refused", {})])
def test_pair_round_walk_continues_after_refusal(tmp_path, monkeypatch, error):
    pair = pair_round(tmp_path)
    (pair / "provenance.json").write_text("{}")
    refused = pair.parent / "refused"
    (refused / "bundle" / "session").mkdir(parents=True)
    (refused / "provenance.json").write_text("{}")
    os.utime(pair, (100, 100))
    os.utime(refused, (200, 200))
    records = rear_pair_round.purpose_take_records

    def read(session, *, purpose):
        if session == refused / "bundle" / "session":
            raise error
        return records(session, purpose=purpose)

    reads = Mock(side_effect=read)
    monkeypatch.setattr(rear_pair_round, "purpose_take_records", reads)
    assert rear_pair_round.newest_rear_pair_round(pair.parent)["round_dir"] == pair
    assert reads.call_count == 2


@pytest.mark.parametrize("probe", [False, True], ids=["", "probed"])
def test_a_rear_round_packets_one_comparison_for_the_whole_batch(tmp_path, banked_candidates, probe):
    """The packet names the rear view's first measured set, never the run
    probe's, which banks first (ADR-0403 §4)."""
    root = rear_round(tmp_path, probe=probe)

    packet, views = packet_of(root)
    entry, = packet["rear"]
    unnamed = [{key: value for key, value in row.items() if key != "set_id"} for row in views]
    assert write_round_packet(root, packet["artifacts"]["manifest"], unnamed)["rear"][0]["set_id"] == BASE_CANDIDATE
    comparison = entry["comparison"]
    by_candidate = {row["candidate_id"]: row for row in entry["candidates"]}
    incumbent, muted, variant = (by_candidate[name] for name in (BASE_CANDIDATE, _MUTED, _VARIANT))

    assert set(entry) == {"set_id", "comparison", "candidates", "stage", "geometry",
                          "geometry_reason", "stack", "out", "schema"}
    assert entry["set_id"] == BASE_CANDIDATE
    assert [row["set_id"] for row in entry["candidates"]] == sorted(_SECTIONS)
    # One band, frozen from the reference take's measured dip and then held:
    # every candidate's figures are read over the same band and hand-over.
    # The reference also carries a deeper LOW room-mode-like dip (_LOW_DIP_HZ,
    # _LOW_DIP_DB): the geometry-windowed search still brackets the wall dip,
    # not that low one, because the search window is anchored on the declared
    # geometry rather than searching the whole coverage (issue #5330).
    assert comparison["band_source"] == BAND_SOURCE_MEASURED_DIP
    assert comparison["band_dip_hz"] == pytest.approx(_DIP_HZ, rel=0.05)
    assert comparison["band_dip_hz"] > _LOW_DIP_HZ * 2.0
    assert comparison["band_hz"] == pytest.approx(
        [comparison["band_dip_hz"] * 0.5, comparison["band_dip_hz"] * 2.0])
    assert len({(tuple(row["low_bass"]["band_hz"]), tuple(row["handover"]["window_hz"]))
                for candidate in entry["candidates"]
                for row in candidate["positions"].values()}) == 1
    assert comparison["reference"] == {"candidate_id": _MUTED, "kind": "rear_muted",
                                       "set_id": _MUTED}
    assert comparison["positions"] == sorted(incumbent["positions"])
    assert comparison["positions_unscored"] == {}
    assert comparison["level"]["level_db"] == -30.0
    assert comparison["level"]["levels_differ"] is False
    assert entry["stage"] == {"band_hz": _CANCELLATION_BAND_HZ,
                              "bass_lowpass_hz": _BASS_LOWPASS_HZ,
                              "handover_hz": pytest.approx(_HANDOVER_HZ)}
    assert entry["geometry_reason"] == ""
    assert entry["stack"]["rear"] is True
    # The roles and the changed control family are disclosures, not verdicts.
    assert [incumbent["role"], muted["role"], variant["role"]] == [
        "incumbent", "rear_muted", "variant"]
    assert (incumbent["changed"], incumbent["change_family"]) == ([], "")
    assert (muted["changed"], muted["change_family"]) == (["rear_muted"], "mute")
    assert variant["change_family"] == "band_edge"
    assert variant["changed"] == ["rear.bass.filters.0.parameters.freq"]
    # A rear section's program charge is judge --preview's to report (#5909).
    assert [set(row) for row in entry["candidates"]] == [{
        "candidate_id", "set_id", "role", "changed", "change_family", "section_reason",
        "level_db", "repeats", "positions", "across_positions", "rear_score"}] * len(entry["candidates"])
    # A round with no pose behind the cabinet has no front-to-back gain to score.
    assert {row["rear_score"]["reason"] for row in entry["candidates"]} == {REASON_TOO_FEW_POSITIONS}
    # The variant's hole AND its lower output are both reported, and the worst
    # regression names the shape figure rather than the level it also lost.
    on_axis = min(comparison["positions"])
    row = variant["positions"][on_axis]
    assert row["late_energy"]["early_late_change_db"] == 3.0
    assert row["late_energy"]["arrival_shift_ms"] == -1.0
    assert comparison["coverage_hz"][1] == comparison["ceiling"]["ceiling_hz"]
    assert [band["change_db"] for band in row["front_guard"]] == pytest.approx([-3.0] * len(FRONT_GUARD_BANDS_HZ))
    assert variant["positions"][on_axis]["handover"]["hole_db"] > (
        incumbent["positions"][on_axis]["handover"]["hole_db"] + 5.0)
    assert variant["positions"][on_axis]["band_level_db"] == pytest.approx(
        incumbent["positions"][on_axis]["band_level_db"] - 3.0, abs=0.2)
    assert variant["across_positions"]["worst_regression"]["figure"] == "handover.hole_db"
    assert incumbent["across_positions"]["worst_regression"]["change_db"] == 0.0
    assert {row["view"] for row in views if row["status"] == "written"} == {"rear", "frequency"}
    assert json.loads((root / ARTIFACT_BY_VIEW["rear"].artifact).read_text()) == {
        key: value for key, value in entry.items() if key != "out"}


def test_the_front_guard_shows_a_narrow_dip_the_band_mean_hid(tmp_path, banked_candidates):
    """A 1.2 dB dip at 500 Hz in front, as the cancellation branch's delay dug on jts3 (#5404
    item 4): the 350–700 Hz band mean reads it inside the playbook's 0.4 dB; its third octave does not."""
    dip = 1.2 * np.exp(-0.5 * (np.log2(SEAT_GRID_HZ / 500.0) / 0.1) ** 2)
    curves = {**_CURVES, BASE_CANDIDATE: (np.asarray(_CURVES[BASE_CANDIDATE]) - dip).tolist()}
    entry, = packet_of(rear_round(tmp_path, curves=curves))[0]["rear"]
    incumbent = next(row for row in entry["candidates"] if row["candidate_id"] == BASE_CANDIDATE)

    band_mean, = band_level_changes(SEAT_GRID_HZ, curves[BASE_CANDIDATE], reference_db=curves[_MUTED],
                                    coverage_hz=SEAT_BAND_HZ, bands_hz=UPPER_BANDS_HZ[:1])
    for row in incumbent["positions"].values():
        guard = {tuple(band["band_hz"]): band["change_db"] for band in row["front_guard"]}
        assert band_mean["change_db"] > -0.4 > guard.pop(FRONT_GUARD_BANDS_HZ[1]) + 0.2
        assert max(map(abs, guard.values())) < 0.1


def test_the_rear_score_caps_each_band_so_one_deep_null_cannot_buy_it(tmp_path, banked_candidates):
    """Behind the box, the variant nulls 87-115 Hz by 40 dB, which the old 90-350 Hz mean hid
    (#5404 item 2), and the incumbent every band by 4 dB, ungated and in its kept impulse alike.
    Uncapped, that one band buys the variant the better mean gain; capped, the broad null wins."""
    flat = np.full(SEAT_GRID_HZ.shape, -30.0)
    null = flat - 40.0 * (np.abs(np.log2(SEAT_GRID_HZ / 100.0)) < 0.2)
    entry, = packet_of(rear_round(tmp_path, curves=dict.fromkeys(_SECTIONS, flat.tolist()), behind={
        _MUTED: (flat, [(_FRONT_ARRIVAL_S, 0.0)]), BASE_CANDIDATE: (flat - 4.0, [(_FRONT_ARRIVAL_S, -4.0)]),
        _VARIANT: (null, [(_FRONT_ARRIVAL_S, 0.0)])}))[0]["rear"]
    by_candidate = {row["candidate_id"]: row for row in entry["candidates"]}
    broad, deep = (by_candidate[name]["rear_score"] for name in (BASE_CANDIDATE, _VARIANT))
    behind_row, = (row for key, row in by_candidate[_VARIANT]["positions"].items() if key.startswith("behind_"))

    assert next(band["change_db"] for band in behind_row["bands"] if band["band_hz"] == [90.0, 350.0]) > -1.0
    assert [tuple(band["band_hz"]) for band in deep["bands"]] == list(BAND_LADDERS[deep["ladder"]])
    assert [band["window_ms"] for band in deep["bands"]] == [None, None, 10.0, 10.0, 10.0, 10.0]
    assert [band["gain_db"] for band in broad["bands"]] == pytest.approx([4.0] * 6, abs=0.01)
    assert deep["bands"][0]["gain_db"] > 3 * REAR_SCORE_CAP_DB
    assert np.mean([band["gain_db"] for band in deep["bands"]]) > np.mean([band["gain_db"] for band in broad["bands"]])
    capped = np.mean([min(band["gain_db"], REAR_SCORE_CAP_DB) for band in deep["bands"]])
    assert (broad["score_db"], deep["score_db"]) == (pytest.approx(4.0, abs=0.01), pytest.approx(capped))
    assert deep["score_db"] < broad["score_db"]


#: A room behind the box: ten arrivals from 15 ms after the direct sound, the same for every
#: candidate, each below the quietest direct arrival, so every window opens on the direct one.
_ROOM_ARRIVALS = [(_FRONT_ARRIVAL_S + 0.015 + 0.004 * index, -14.0) for index in range(10)]


@pytest.mark.parametrize("variant_impulse,reason", [
    ("absent", TAKE_CURVES_NOT_BANKED), ("corrupt", REASON_UNREADABLE)])
def test_the_rear_score_reads_its_upper_bands_behind_through_the_window(
    tmp_path, banked_candidates, variant_impulse, reason,
):
    """Behind the box the room refills the incumbent's null in the ungated curve, while its direct
    sound is 12 dB down: the bands from 160 Hz read that through the 10 ms window only (#5404 item 2).
    A behind take whose impulse is missing or does not read back gaps its own score, and the rear
    view still writes (ADR-0101)."""
    flat = np.full(SEAT_GRID_HZ.shape, -30.0)
    root = rear_round(tmp_path, curves=dict.fromkeys(_SECTIONS, flat.tolist()), behind={
        _MUTED: (flat, [(_FRONT_ARRIVAL_S, 0.0), *_ROOM_ARRIVALS]),
        BASE_CANDIDATE: (flat, [(_FRONT_ARRIVAL_S, -12.0), *_ROOM_ARRIVALS]),
        _VARIANT: (flat, None if variant_impulse == "absent" else [(_FRONT_ARRIVAL_S, 0.0)])})
    if variant_impulse == "corrupt":
        (round_inputs(root).session_dir / "impulses" / f"{_VARIANT}-behind-1.npz").write_bytes(b"not an npz")

    entry, = packet_of(root)[0]["rear"]
    by_candidate = {row["candidate_id"]: row for row in entry["candidates"]}
    score = by_candidate[BASE_CANDIDATE]["rear_score"]

    assert [band["gain_db"] for band in score["bands"]] == pytest.approx([0.0, 0.0, 12.0, 12.0, 12.0, 12.0], abs=0.1)
    assert [band["below_trusted_floor"] for band in score["bands"]] == [False, False, True, True, True, False]
    assert score["below_trusted_floor_bands"] == 3
    assert by_candidate[_VARIANT]["rear_score"]["reason"] == reason


@pytest.mark.parametrize("summed_capture_bundle,covered_bands", [(20000, 7), (200, 2)], indirect=["summed_capture_bundle"])
@pytest.mark.parametrize("pose_kind", ["behind", "seat"])
def test_rear_views_banked_non_bearing_trial(summed_capture_bundle, covered_bands, tmp_path, banked_candidates, monkeypatch, pose_kind):
    bundle, _, _, bank = summed_capture_bundle
    monkeypatch.setattr(room_selection, "analyzed_measurements", measurement_analysis.analyzed_measurements)
    gains = {BASE_CANDIDATE: -3.0, _VARIANT: -6.0, _MUTED: 0.0}
    for candidate, gain in gains.items():
        for kind, distance in (("bearing", 1.0), (pose_kind, 0.1)):
            asyncio.run(bank(f"{candidate}-{kind}", candidate=candidate, measurement_purpose="rear", phase="lateral",
                             pose_kind=kind, mark_distance_m=distance, vertical_deg=0,
                             seat_offset_m=[0.0, 0.0, 0.0] if kind == "seat" else None,
                             capture_gain_db=gain if kind == pose_kind else 0.0, gating_applied=False,
                             keep_impulses=True))
    groups = []
    records = [(row.path, record) for row, record in measurement_documents(bundle)]
    for candidate in gains:
        group = manifest_set([(path, record) for path, record in records
                              if record["candidate_id"] == candidate], set_id=candidate)
        group["base"] = candidate == BASE_CANDIDATE
        groups.append(group)
    write_manifest(bundle, program="rear/seat" if pose_kind == "seat" else "rear/express", groups=groups)
    _round_environment(bundle, applied=_SECTIONS[BASE_CANDIDATE])
    mark_state(bundle, "applied")
    banked = bank_round(bundle, campaign_root=tmp_path / "bank", view_runner=round_views.run_bookkeeping,
                        applied_profile_path=bundle / "applied-profile.json",
                        declared_geometry_path=bundle / "declared-geometry.json")
    entry, = json.loads((banked.path / "packet.json").read_text())["rear"]
    assert entry["comparison"]["reference"]["candidate_id"] == _MUTED
    assert entry["comparison"]["positions_unscored"] == {}
    for candidate in entry["candidates"]:
        positions = candidate["positions"]
        placed, = (row for key, row in positions.items() if key.startswith(f"{pose_kind}_"))
        front, = (row for key, row in positions.items() if not key.startswith(f"{pose_kind}_"))
        assert set(front) == {"reason", "dip", "dip_shift", "ripple_db", "own_trend_ripple_db", "handover", "low_bass",
                              "band_level_db", "late_energy", "front_guard", "ladder"}
        assert [band["band_hz"] for band in front["front_guard"]] == [list(band) for band in FRONT_GUARD_BANDS_HZ]
        for band in front["front_guard"]:
            assert band == ({"status": "available", "band_hz": band["band_hz"], "change_db": pytest.approx(0.0, abs=0.01)}
                            if covered_bands == 7 else
                            {"status": "unavailable", "reason": REASON_COVERAGE_SHORT, "band_hz": band["band_hz"]})
        assert placed["front_guard"] == []
        assert placed["reason"] == ""
        assert isinstance(placed["ripple_db"], float)
        assert isinstance(placed["own_trend_ripple_db"], float)
        assert isinstance(placed["band_level_db"], float)
        assert isinstance(placed["handover"], dict) and isinstance(placed["low_bass"], dict)
        assert placed["trough_fill_db"] is None
        assert placed["trough_fill_reason"] == rear_views.REASON_NON_BEARING
        assert [row["band_hz"] for row in placed["bands"]] == [list(band) for band in LEVEL_BANDS_HZ]
        expected = gains[candidate["candidate_id"]]
        for index, band in enumerate(placed["bands"]):
            assert set(band) == {"band_hz", "level_db", "reference_db", "change_db", "reason"}
            if index < covered_bands:
                assert band["reason"] == ""
                assert band["change_db"] == pytest.approx(expected, abs=0.01)
                assert band["level_db"] - band["reference_db"] == pytest.approx(expected, abs=0.01)
            else:
                assert band["change_db"] is band["level_db"] is band["reference_db"] is None
                assert band["reason"] == REASON_COVERAGE_SHORT
        assert set(placed) == set(front) | {"trough_fill_db", "trough_fill_reason", "bands"}
        assert candidate["repeats"] == dict.fromkeys(positions, 1)
        # Front minus behind, behind read in part through a 10 ms window of each real kept impulse.
        score = candidate["rear_score"]
        if pose_kind != "behind" or covered_bands < 7:
            assert score["reason"] == (REASON_TOO_FEW_POSITIONS if pose_kind != "behind" else REASON_COVERAGE_SHORT)
        else:
            assert [band["gain_db"] for band in score["bands"]] == pytest.approx(
                [-gains[candidate["candidate_id"]]] * 6, abs=0.01)


def test_a_position_the_reference_missed_is_disclosed_rather_than_dropped(
    tmp_path, banked_candidates,
):
    """Every advertised position is scored, and every other one names a reason."""
    root = rear_round(tmp_path, missing={_MUTED: (20,), _VARIANT: (-20,)})

    entry, = packet_of(root)[0]["rear"]
    comparison = entry["comparison"]
    by_candidate = {row["candidate_id"]: row for row in entry["candidates"]}
    off_axis = [key for key in comparison["positions"] if key != min(comparison["positions"])]

    # The reference measured neither +20 nor anything at it, so no candidate is
    # read there; the incumbent and the variant did measure it.
    assert set(comparison["positions_unscored"].values()) == {"no_reference_take"}
    assert set(comparison["positions_unscored"]) == (
        set(by_candidate[BASE_CANDIDATE]["repeats"]) - set(comparison["positions"]))
    assert all(set(row["positions"]) <= set(comparison["positions"])
               for row in entry["candidates"])
    # The variant missed an advertised position itself, which is its own reason.
    assert by_candidate[_VARIANT]["across_positions"]["positions_unavailable"] == {
        key: "no_row" for key in off_axis}
    assert set(by_candidate[BASE_CANDIDATE]["positions"]) == set(comparison["positions"])
    assert by_candidate[BASE_CANDIDATE]["across_positions"]["positions_unavailable"] == {}


#: A later muted reference 3 dB below the earlier one, with a 20 dB notch at
#: 143 Hz: the one bad reference take #5404 09-20 item 7 names. The ladder's
#: band power mean reads the notch as about 0.3 dB more in (90, 350) Hz only.
_LATER_MUTED = (np.asarray(_CURVES[_MUTED]) - 3.0
                - 20.0 * np.exp(-0.5 * (np.log2(SEAT_GRID_HZ / 143.0) / 0.12) ** 2)).tolist()


#: The code an earlier reference this build cannot read discloses, by how it was banked.
_UNREAD = {"curves_unbanked": TAKE_CURVES_NOT_BANKED, "analysis_failed": TAKE_CURVES_NOT_BANKED,
           "program_superseded": REASON_UNREADABLE}


@pytest.mark.parametrize("earlier", ["banked", *_UNREAD, "manifest_unreadable", "absent"])
def test_a_rear_round_discloses_its_reference_against_the_previous_reference(
    tmp_path, banked_candidates, monkeypatch, earlier,
):
    """ADR-0391: at each position, the reference against the newest earlier
    banked round's reference there, on the whole ``rear_level`` ladder. Both
    sides are built as the rear view builds its own reference, from the takes
    its round kept: a deselected retake is on neither side, and a take its set
    does not list leaves that position unscored. A position without an earlier
    reference names its reason, an earlier reference this build cannot read
    names its round, and the rear view writes either way."""
    # The real reader, so an earlier round's takes refuse or are passed over as they are on a speaker.
    monkeypatch.setattr(room_selection, "analyzed_measurements", measurement_analysis.analyzed_measurements)
    at = {deg: doc_pose_key({"position_deg": deg, "vertical_deg": 0, "mark_distance_m": 1.0, "pose_kind": "bearing"})
          for deg in (0, -20, 20)}
    if earlier != "absent":
        before = rear_round(tmp_path, name="earlier", missing={_MUTED: (20,)}, retake={_MUTED: 6.0},
                            curves={**_CURVES, _MUTED: None} if earlier == "curves_unbanked" else _CURVES,
                            failed={_MUTED: (0,)} if earlier == "analysis_failed" else {},
                            superseded=earlier == "program_superseded")
        (before / "provenance.json").write_text(json.dumps({"banked_at_utc": "2026-09-20T12:00:00Z"}))
        if earlier in ("curves_unbanked", "program_superseded"):
            # A rear round banked from 09-26 to 09-28 declared its reference in the packet that era wrote.
            (before / "packet.json").write_text(json.dumps({
                "schema": "jts_round_packet/3", "rear": [{"comparison": {"reference": {"set_id": _MUTED}}}]}))
        else:
            packet_of(before)
        if earlier == "manifest_unreadable":
            manifest, = round_inputs(before).session_dir.rglob(RUN_MANIFEST_FILENAME)
            manifest.write_text("{")
    root = rear_round(tmp_path, name="later", curves={**_CURVES, _MUTED: _LATER_MUTED}, retake={_MUTED: 6.0},
                      unlisted={_MUTED: (-20,)}, band_hz=(40.0, 20_000.0))
    (root / "provenance.json").write_text(json.dumps({"banked_at_utc": "2026-09-21T12:00:00Z"}))

    comparison = packet_of(root)[0]["rear"][0]["comparison"]
    previous = comparison["previous_reference"]
    on_axis = previous.pop(at[0])

    assert comparison["positions_unscored"] == {at[-20]: "no_reference_take"}
    assert previous == {at[20]: {"status": "unavailable", "reason": REASON_NO_EARLIER_REFERENCE}}
    named = {"round_id": "earlier", "set_id": _MUTED}
    if earlier != "banked":
        assert {key: value for key, value in on_axis.items() if key != "detail"} == (
            {"status": "unavailable", "reason": _UNREAD[earlier], **named} if earlier in _UNREAD
            else {"status": "unavailable", "reason": REASON_NO_EARLIER_REFERENCE})
        return
    bands = {tuple(band["band_hz"]): band for band in on_axis["bands"]}
    assert {key: on_axis[key] for key in ("status", "round_id", "set_id", "ladder")} == {
        "status": "available", **named, "ladder": "rear_level"}
    assert sorted(on_axis["take_ids"]) == [f"{_MUTED}-0-{repeat}" for repeat in (1, 2)]
    assert list(bands) == list(LEVEL_BANDS_HZ)
    # The later round swept from 40 Hz, so the shared band leaves (30, 60) Hz uncovered.
    assert bands.pop((30.0, 60.0)) == {"status": "unavailable", "reason": REASON_COVERAGE_SHORT,
                                       "band_hz": [30.0, 60.0]}
    assert {band["status"] for band in bands.values()} == {"available"}
    assert bands.pop((90.0, 350.0))["change_db"] < -3.25
    assert [band["change_db"] for band in bands.values()] == pytest.approx([-3.0] * len(bands), abs=0.01)
    assert set(on_axis["basis"]) == {"basis_status", "intervention_fields", "incompatible_fields",
                                     "mismatched_fields", "unknown_fields"}


def test_the_repeat_spread_comes_from_the_repeated_pose(tmp_path, banked_candidates):
    root = rear_round(tmp_path)

    entry, = packet_of(root)[0]["rear"]
    spread = entry["comparison"]["repeat_spread"]

    assert (spread["candidate_id"], spread["n_repeats"], spread["reason"]) == (
        BASE_CANDIDATE, 2, "")
    assert spread["position"] == min(entry["comparison"]["positions"])
    assert set(spread["spread_db"]) == {"ripple_db", "dip.depth_db", "handover.hole_db",
                                        "band_level_db", "low_bass.level_db"}
    # Repeats of one candidate at one pose are the same curve here, so the
    # spread is zero and no difference may be called inconclusive against it.
    assert spread["spread_db"]["band_level_db"] == pytest.approx(0.0)
    assert all(row["across_positions"]["worst_regression"]["exceeds_repeat_spread"] is not None
               for row in entry["candidates"])


def test_a_batch_without_repeats_or_a_muted_candidate_falls_back_and_says_so(
    tmp_path, banked_candidates,
):
    root = rear_round(tmp_path, candidates=(BASE_CANDIDATE, _VARIANT), repeats=1)

    entry, = packet_of(root)[0]["rear"]

    assert entry["comparison"]["reference"] == {"candidate_id": BASE_CANDIDATE,
                                                "kind": "incumbent", "set_id": BASE_CANDIDATE}
    assert entry["comparison"]["repeat_spread"]["reason"] == REASON_NO_REPEATS
    for candidate in entry["candidates"]:
        assert candidate["rear_score"] == {"status": "unavailable", "reason": REASON_NO_COMPARISON}
        for row in candidate["positions"].values():
            assert row["late_energy"]["reason"] == REASON_NO_COMPARISON
            assert row["late_energy"]["early_late_change_db"] is None
            assert row["front_guard"] == []
    assert set(entry["comparison"]["repeat_spread"]["spread_db"].values()) == {None}
    assert all(row["across_positions"]["worst_regression"]["exceeds_repeat_spread"] is None
               for row in entry["candidates"])


@pytest.mark.parametrize("applied,candidates,reference", [
    (None, (BASE_CANDIDATE, _VARIANT), BASE_CANDIDATE),
    (None, (BASE_CANDIDATE, _CLEARED, _VARIANT), _CLEARED),
    (_SECTIONS[BASE_CANDIDATE], (BASE_CANDIDATE, _CLEARED, _VARIANT), _CLEARED),
], ids=["a base with no rear stage", "a cleared candidate over a rear-off base", "a cleared candidate over a rear base"])
def test_a_set_that_plays_no_rear_stage_is_the_rear_off_reference(
    tmp_path, banked_candidates, applied, candidates, reference,
):
    """A set whose rear section is empty plays its rear output muted, as a muted section does: on
    a first build the base, and a candidate composed with "rear_calibration": null. A rear-off
    candidate is the reference before the base, and the other candidates' late energy and front
    guard read against it (ADR-0436). A base that plays a rear stage, with no rear-off candidate,
    stays the incumbent reference (above)."""
    root = rear_round(tmp_path, candidates=candidates, repeats=1, curves={**_CURVES, _CLEARED: _CURVES[_MUTED]})
    _round_environment(root, applied=applied)

    entry, = packet_of(root)[0]["rear"]

    assert entry["comparison"]["reference"] == {"candidate_id": reference, "kind": "rear_muted", "set_id": reference}
    assert {row["candidate_id"]: row["role"] for row in entry["candidates"]} == {
        BASE_CANDIDATE: "incumbent", _VARIANT: "variant", **({_CLEARED: "rear_muted"} if _CLEARED in candidates else {})}
    variant, = (row for row in entry["candidates"] if row["candidate_id"] == _VARIANT)
    for row in variant["positions"].values():
        assert row["late_energy"]["reason"] == "" and row["late_energy"]["early_late_change_db"] is not None
        assert row["front_guard"]


@pytest.mark.parametrize("pose_kind", [POSE_KIND_BEHIND, "seat"])
def test_a_non_bearing_take_has_figures_without_becoming_the_on_axis_reference(
    tmp_path, banked_candidates, pose_kind,
):
    root = rear_round(tmp_path, on_axis_kind=pose_kind)
    entry, = packet_of(root)[0]["rear"]
    assert entry["comparison"]["band_source"] == BAND_SOURCE_DECLARED_GEOMETRY
    assert entry["comparison"]["band_dip_hz"] is None
    for candidate in entry["candidates"]:
        row, = (row for key, row in candidate["positions"].items() if key.startswith(f"{pose_kind}_"))
        bearing = next(row for key, row in candidate["positions"].items() if key.startswith("az"))
        assert row["reason"] == ""
        assert isinstance(row["ripple_db"], float)
        assert (row["ripple_db"], row["dip"]) == (bearing["ripple_db"], bearing["dip"])
        assert row["front_guard"] == []
        assert row["trough_fill_db"] is None
        assert row["trough_fill_reason"] == rear_views.REASON_NON_BEARING


def test_a_pair_round_packets_each_woofer_alone_and_the_trust_number(
    tmp_path, banked_candidates,
):
    root = pair_round(tmp_path)

    packet, views = packet_of(root)
    entry, = packet["rear"]
    comparison = entry["comparison"]
    on_axis = min(comparison["positions"])
    row = entry["pair"]["positions"][on_axis]

    # The summed entry's shape plus the pair block, and NO candidate
    # comparison: one played candidate has nothing to be compared against.
    assert set(entry) == {"set_id", "comparison", "candidates", "pair", "stage",
                          "geometry", "geometry_reason", "stack", "out", "schema"}
    # Three role-scoped sets, one candidate: the document names the SUM's set
    # rather than whichever role a candidate-keyed dict iterated last.
    assert (entry["candidates"], entry["set_id"]) == ([], _PAIR_SET_ID)
    # The takes played their parent with its rear stage cleared, and the packet says so.
    assert (entry["pair"]["candidate_id"], entry["pair"]["cleared_layers"]) == (_PARENT, ["rear_calibration"])
    assert comparison["reference"] == {"candidate_id": _PARENT, "kind": "pair",
                                       "set_id": _PAIR_SET_ID}
    assert comparison["positions"] == sorted(entry["pair"]["positions"])
    assert comparison["positions_unscored"] == {}
    assert comparison["band_hz"] == pytest.approx(
        [row["bands"][0]["band_hz"][0], row["bands"][-1]["band_hz"][1]])
    # Each woofer alone at each band, the rear's level gap, and the sum played.
    assert [band["level_gap_db"] for band in row["bands"]] == pytest.approx(
        [_PAIR_LEVEL_GAP_DB] * len(row["bands"]))
    assert [band["front_db"] for band in row["bands"]] == pytest.approx(
        [0.0] * len(row["bands"]), abs=1e-6)
    assert (row["superposition_residual_db"], row["reason"]) == (pytest.approx(0.0, abs=0.01), "")
    assert [band["superposition_residual_db"] for band in row["bands"]] == pytest.approx(
        [0.0] * len(row["bands"]), abs=0.01)
    assert row["arrival_gap"]["ms"] == pytest.approx(_PAIR_GAP_MS, abs=0.05)
    assert (row["arrival_gap"]["n_repeats"], row["arrival_gap"]["at_edge"]) == (2, False)
    assert row["arrival_gap"]["repeat_spread_us"] == pytest.approx(0.0, abs=1.0)
    assert row["rear_polarity"]["state"] == POLARITY_INVERTED
    # The stage is the APPLIED document's, read at the measured gap.
    assert entry["stage"]["band_hz"] == _CANCELLATION_BAND_HZ
    assert isinstance(entry["stage"]["gradient_residual_db"], float)
    # One candidate, so no figure spread for a difference to be real against.
    # How the index renders that line is pinned with the other index cases.
    assert comparison["repeat_spread"]["reason"] == REASON_NO_COMPARISON
    assert {r["view"] for r in views if r["status"] == "written"} == {"rear", "frequency"}
    assert json.loads((root / ARTIFACT_BY_VIEW["rear"].artifact).read_text()) == {
        key: value for key, value in entry.items() if key != "out"}


#: The lowest three third octaves in the rumble (25-40 Hz) and above it (63-100 Hz).
_RUMBLE_BANDS_HZ, _ABOVE_RUMBLE_HZ = THIRD_OCTAVE_BASS_BANDS_HZ[1:4], THIRD_OCTAVE_BASS_BANDS_HZ[5:8]


@pytest.mark.parametrize("noise_below_hz,snr_db,state,bands_hz,reason", [
    (45.0, lambda lo_hz: 40.0, POLARITY_INVERTED, _RUMBLE_BANDS_HZ, ""),
    (45.0, lambda lo_hz: 0.0 if lo_hz < 50.0 else 40.0, POLARITY_SAME, _ABOVE_RUMBLE_HZ, ""),
    (None, lambda lo_hz: 12.0 if lo_hz < 50.0 else 40.0, POLARITY_INVERTED, _RUMBLE_BANDS_HZ, ""),
    (45.0, lambda lo_hz: 0.0, POLARITY_UNCLEAR, (), REASON_POLARITY_SNR_SHORT),
    (45.0, None, POLARITY_UNCLEAR, (), TAKE_CURVES_NOT_BANKED),
], ids=["noise_trusted", "noise_untrusted", "sum_cancels", "none_trusted", "not_banked"])
def test_rear_polarity_reads_only_the_bands_where_each_woofer_clears_the_noise(
    tmp_path, banked_candidates, noise_below_hz, snr_db, state, bands_hz, reason,
):
    """Two woofers in phase whose 22–45 Hz bands are rumble that reads inverted (#5404 item 3):
    the rumble decides only where each woofer alone clears the noise. A woofer's SNR is the sum's
    banked SNR plus its level over the sum's, so an inverted pair, whose sum cancels at 20-50 Hz,
    still reads its lowest bands."""
    entry, = packet_of(pair_round(tmp_path, noise_below_hz=noise_below_hz, snr_db=snr_db))[0]["rear"]

    for row in entry["pair"]["positions"].values():
        polarity = row["rear_polarity"]
        assert (polarity["state"], polarity["reason"], polarity["n_bands"]) == (state, reason, len(bands_hz))
        assert [tuple(band) for band in polarity["bands_hz"]] == list(bands_hz)


def test_a_muted_rear_is_the_whole_ideal_gradient_away_from_one(tmp_path, banked_candidates):
    """The gradient residual is a fact about the APPLIED document: a muted rear
    cancels nothing, so the ideal gradient term stands alone at 0 dB."""
    root = pair_round(tmp_path, applied=_MUTED)

    entry, = packet_of(root)[0]["rear"]

    assert entry["stage"]["gradient_residual_db"] == pytest.approx(0.0, abs=1e-6)


def test_a_behind_positions_gap_never_pools_into_the_gradient_residual(
    tmp_path, banked_candidates,
):
    """A mic behind the cabinet (0.1 m) measures a different physical gap than
    one in front (1 m): the document-level gradient residual's pooled median
    must read the front bearing positions alone (review, PR #5362). The two
    front positions get DIFFERENT gaps so a leaked behind gap would visibly
    shift the pooled median rather than hide behind a tied pair."""
    front_only = pair_round(tmp_path / "front", repeats=1, missing=(20,),
                            off_axis_gap_ms=1.6)
    with_behind = pair_round(tmp_path / "with_behind", repeats=1, missing=(20,),
                             off_axis_gap_ms=1.6, behind_gap_ms=-1.2)

    front_entry, = packet_of(front_only)[0]["rear"]
    behind_entry, = packet_of(with_behind)[0]["rear"]

    assert behind_entry["stage"]["gradient_residual_db"] == pytest.approx(
        front_entry["stage"]["gradient_residual_db"])
    behind_key = next(key for key in behind_entry["pair"]["positions"]
                      if key.startswith("behind_"))
    assert behind_entry["pair"]["positions"][behind_key]["arrival_gap"]["ms"] == pytest.approx(
        -1.2, abs=0.1)


def test_a_pair_position_without_its_segments_is_disclosed(tmp_path, banked_candidates):
    root = pair_round(tmp_path, missing=(20,))

    entry, = packet_of(root)[0]["rear"]
    comparison = entry["comparison"]

    assert set(comparison["positions_unscored"].values()) == {rear_views.REASON_SEGMENT_MISSING}
    assert len(comparison["positions_unscored"]) == 1
    assert set(entry["pair"]["positions"]) == set(comparison["positions"])
    assert not set(comparison["positions_unscored"]) & set(entry["pair"]["positions"])


def test_a_pair_take_that_banked_no_curve_refuses_by_field(tmp_path, banked_candidates):
    """A take that banked an empty ``curves`` banked no segment at all, so the
    pair view refuses by field and role, not as a missing segment (#2902)."""
    root = pair_round(tmp_path)
    session = round_inputs(root).session_dir
    for row, record in measurement_documents(session):
        if record.get("measurement_purpose") == "rear":
            (session / record_path(row)).write_text(json.dumps({**record, "curves": []}))

    _, views = packet_of(root)

    row = next(view for view in views if view["view"] == "rear")
    assert (row["status"], row["reason"], row["detail"]["field"], row["detail"]["role"]) == (
        "unavailable", TAKE_CURVES_NOT_BANKED, "curves", rear_views.PAIR_ROLES[0])


def test_a_pair_round_never_asks_the_summed_analyzer(tmp_path, banked_candidates, monkeypatch):
    """A branch take's program is two-channel and ``candidate_branches``-scoped,
    which the summed analyzer refuses outright — so the pair path must not ask
    it. Read with the REAL analyzer restored, which is what the round hits on
    the box, over takes that bank their role curves, as every take does
    (ADR-0383)."""
    monkeypatch.setattr(room_selection, "analyzed_measurements",
                        measurement_analysis.analyzed_measurements)
    root = pair_round(tmp_path)
    inputs = round_inputs(root)

    packet, views = packet_of(root)
    entry, = packet["rear"]

    # The fixture really does carry the shape the analyzer cannot read.
    with pytest.raises(EvidenceUnavailable) as refused:
        list(measurement_analysis.analyzed_measurements(inputs.session_dir))
    assert refused.value.reason == "measurement_analysis_program_unsupported"
    assert next(v for v in views if v["view"] == "rear")["status"] == "written"
    assert entry["comparison"]["positions_unscored"] == {}
    assert len(entry["pair"]["positions"]) == len(entry["comparison"]["positions"]) == 3
    row = entry["pair"]["positions"][min(entry["comparison"]["positions"])]
    assert (row["reason"], len(row["bands"])) == ("", 10)
    assert row["superposition_residual_db"] == pytest.approx(0.0, abs=0.01)
    assert row["arrival_gap"]["ms"] == pytest.approx(_PAIR_GAP_MS, abs=0.05)


def test_a_pair_round_that_analyzed_no_branches_says_that_and_not_a_missing_incumbent(
    tmp_path, banked_candidates,
):
    """jts3's shape: the round asked for pair takes, the takes banked no branch
    diagnostic. Falling through to the summed path would report a missing
    incumbent, a question a pair round never asked."""
    root = pair_round(tmp_path, diagnostic=False)

    packet, views = packet_of(root)
    row = next(view for view in views if view["view"] == "rear")

    assert (row["status"], row["reason"]) == ("unavailable",
                                              rear_views.REFUSE_NO_BRANCH_DIAGNOSTIC)
    assert row["detail"] == {"candidates": [_PARENT], "takes": 4}
    assert packet["rear"] == []


@pytest.mark.parametrize("applied,swept_hz,expected_band,reason", [
    (False, SEAT_BAND_HZ, list(ARRIVAL_GAP_BAND_HZ), ""),
    (False, (20.0, 100.0), None, REASON_COVERAGE_SHORT),
    (True, SEAT_BAND_HZ, _CANCELLATION_BAND_HZ, ""),
    (True, (60.0, 280.0), [60.0, 280.0], ""),
    (True, (150.0, 500.0), None, REASON_COVERAGE_SHORT),
    (True, (20.0, 21.0), None, REASON_COVERAGE_SHORT),
    (True, (20.0, 100.0), None, REASON_COVERAGE_SHORT),
])
def test_pair_arrival_gap_uses_the_applied_band_or_default_and_refuses_short_coverage(
    tmp_path, banked_candidates, applied, swept_hz, expected_band, reason,
):
    root = pair_round(tmp_path, swept_hz=swept_hz)
    _round_environment(root, applied=_rear_document() if applied else {})

    entry, = packet_of(root)[0]["rear"]
    row = entry["pair"]["positions"][min(entry["comparison"]["positions"])]
    gap = row["arrival_gap"]

    assert gap["arrival_gap_band_source"] == ("rear_document" if applied else "default")
    assert (gap["band_hz"], gap["reason"]) == (expected_band, reason)
    if reason:
        assert (gap["ms"], gap["search_ms"]) == (None, None)
        assert entry["stage"]["gradient_residual_db"] is None
    else:
        assert gap["ms"] == pytest.approx(_PAIR_GAP_MS, abs=0.05)
    if swept_hz[1] == 21.0:
        assert (row["bands"], row["band_hz"]) == ([], None)
        assert row["superposition_residual_db"] is None
        assert row["reason"] == REASON_COVERAGE_SHORT
        assert entry["comparison"]["band_reason"] == REASON_COVERAGE_SHORT


@pytest.mark.parametrize("program", ["room", "bass"])
def test_another_purpose_gets_no_rear_entry(tmp_path, program):
    root = bank_seat_round(tmp_path / program)
    write_manifest(root, program=program)

    packet, views = packet_of(root)

    assert packet["rear"] == []
    assert packet["artifacts"]["rear_views"] == []
    assert "rear" not in {row["view"] for row in views}
    assert not (root / ARTIFACT_BY_VIEW["rear"].artifact).exists()


def test_a_near_field_round_moves_no_tuning_reader(tmp_path):
    """A near-field round banked after a rear pair round is reference evidence
    (ADR-0360): its packet carries no views, fits or bass table, and the latest
    round of every tuning program and the rear-pair level match stay the pair
    round's."""
    pair = pair_round(tmp_path)
    (pair / "packet.json").write_text(json.dumps({"preset": "rear/pair", "sets": [{"takes": [{"selected": True}]}]}))
    near = bank_seat_round(pair.parent, name="nearfield")
    source, store = _round_source(near)
    layout = [(driver, mm) for driver in ("woofer", "woofer:rear") for mm in (15, 30, 15)]
    records = [{**source, "take_id": f"nearfield-{index}", "position_id": f"nearfield-{index}", "repeat": 1,
                "pose_kind": "close", "position_deg": 0, "vertical_deg": 0, "mark_distance_m": mm / 1000,
                "pose_driver": driver, "measurement_purpose": "reference", "gating_applied": False,
                "graph_scope": "drivers", "regime": "near_field"}
               for index, (driver, mm) in enumerate(layout)]
    group = manifest_set(_banked(store, records))
    for take, (driver, _mm) in zip(group["takes"], layout):
        take["role"] = driver
    write_manifest(near, program="nearfield/each", groups=[group])

    def readers():
        session_dir = round_inputs(pair).session_dir
        return (latest_banked_rounds({}, session_dir, include_stale=True),
                rear_pair_round.newest_rear_pair_round(pair.parent))

    (pair / "provenance.json").write_text(json.dumps({"banked_at_utc": "2026-09-23T12:00:00Z"}))
    before = readers()
    (near / "provenance.json").write_text(json.dumps({"banked_at_utc": "2026-09-24T12:00:00Z"}))
    packet, views = packet_of(near)

    assert views == []
    assert packet["fits"] == packet["series"] == packet["room"] == packet["bass"] == packet["rear"] == []
    assert readers() == before
    assert before[0]["rear"]["round_id"] == before[1]["round_id"] == pair.name
