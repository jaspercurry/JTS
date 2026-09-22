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
from typing import Any, Mapping, Sequence
from unittest.mock import Mock

import numpy as np
import pytest

from jasper.active_speaker.angle_capture import BASE_CANDIDATE
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
from jasper.active_speaker.branch_chain import rear_branch_sum_headroom_db
from jasper.active_speaker.candidate_bank import CandidateBankRefusal
from jasper.active_speaker import measurement_analysis
from jasper.active_speaker.crossover_v2 import rear_pair_round, rear_views, room_selection
from jasper.active_speaker.crossover_v2.pose_curve import LateralPoseCurve, pose_curve_record
from jasper.active_speaker.crossover_v2.record_index import measurement_documents, record_path
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.measurement_programs import POSE_KIND_BEHIND
from jasper.active_speaker.rear_calibration import diagnostic_seed
from jasper.active_speaker.round_bank import _bookkeeping, bank_round
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.round_packet import write_round_packet
from jasper.active_speaker.round_view_artifacts import ARTIFACT_BY_VIEW
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.rear_evidence import (
    ARRIVAL_GAP_BAND_HZ, BAND_SOURCE_DECLARED_GEOMETRY, BAND_SOURCE_MEASURED_DIP,
    POLARITY_INVERTED, LEVEL_BANDS_HZ,
    REASON_COVERAGE_SHORT, REASON_NO_COMPARISON, REASON_NO_REPEATS,
)
from jasper.cli import round_views
from tests.crossover_v2_banked_round import (
    SEAT_BAND_HZ, SEAT_GRID_HZ, _reopen, bank_seat_round,
)
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.room_median_fixture import analyzed_room_documents as analyzed_room_documents
from tests.test_active_speaker_audition import _applied_profile
from tests.test_active_speaker_runtime_contract import _active_topology
from tests.test_crossover_v2_frequency_view import summed_capture_bundle as summed_capture_bundle

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

#: A pair round's ONE played candidate and how the run composed it: the applied
#: tune with its rear calibration cleared, which is what the bank records
#: against the composed fingerprint.
_COMPOSED = "composed-fingerprint"
_COMPOSED_ANALYSIS = {"base": {"fingerprint": BASE_CANDIDATE},
                      "resolution": {"rear_calibration": "cleared"}}
#: A pair round banks one set per captured role; the SUM's is the document's.
_PAIR_SET_ID = f"{_COMPOSED}-{rear_views.PAIR_ROLES[-1]}"

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
    document = diagnostic_seed(_SAMPLE_RATE_HZ)
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


@pytest.fixture
def banked_candidates(monkeypatch):
    """The candidate bank, which answers a fingerprint with its rear section
    and, for the composed candidate, what the run recorded about composing it."""
    def find(fingerprint, *, root=None):
        if fingerprint == _COMPOSED:
            return SimpleNamespace(candidate=SimpleNamespace(
                rear_calibration={}, analysis=_COMPOSED_ANALYSIS))
        if fingerprint not in _SECTIONS:
            raise CandidateBankRefusal("not_found", fingerprint)
        return SimpleNamespace(candidate=SimpleNamespace(
            rear_calibration=_SECTIONS[fingerprint], analysis={}))

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
    store, _identity = _reopen(root)
    return {key: value for key, value in source.items()
            # The store writes these two and refuses a record that carries them.
            if key not in ("schema_version", "capture_session_id")}, store


def _round_environment(root: Path, *, applied: Mapping[str, Any]) -> None:
    """The declared cabinet and the applied tune a rear round reads."""
    DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0,
                     **_CABINET).save(root / "declared-geometry.json")
    profile = _applied_profile(_active_topology("mono", "active_2_way"))
    profile.update(kind=BASELINE_PROFILE_KIND, artifact_schema_version=SCHEMA_VERSION)
    profile["recomposition_snapshot"]["rear_calibration"] = applied
    (root / "applied-profile.json").write_text(json.dumps(profile))


def _poses(repeats: int) -> list[tuple[int, int]]:
    """The bearings a rear round walks, on-axis repeated."""
    return [(0, index + 1) for index in range(repeats)] + [(-20, 1), (20, 1)]


def rear_round(tmp_path: Path, *, candidates=(BASE_CANDIDATE, _MUTED, _VARIANT),
               repeats: int = 2, missing: Mapping[str, Sequence[int]] = {},
               on_axis_kind: str = "bearing") -> Path:
    """One banked ``rear`` round: every candidate at every pose, on-axis repeated.

    ``missing`` drops a candidate's take at named bearings, which is how a
    reference take goes missing where other candidates measured. ``on_axis_kind``
    banks every azimuth-0 take as a non-bearing pose, for the on-axis-reference
    guard (review, PR #5362).
    """
    root = bank_seat_round(tmp_path / "rear")
    source, store = _round_source(root)
    groups = []
    for candidate in candidates:
        records = []
        for degrees, repeat in _poses(repeats):
            if degrees in missing.get(candidate, ()):
                continue
            take_id = f"{candidate}-{degrees}-{repeat}"
            records.append({**source, "take_id": take_id, "position_id": take_id, "repeat": repeat,
                            "pose_kind": on_axis_kind if degrees == 0 else "bearing",
                            "position_deg": degrees, "vertical_deg": 0,
                            "mark_distance_m": 1.0, "measurement_purpose": "rear",
                            "gating_applied": False, "graph_scope": "candidate",
                            "candidate_id": candidate, "level_db": -30.0,
                            "seat_offset_m": [0.0, 0.0, 0.0] if on_axis_kind == "seat" and degrees == 0 else None,
                            "curves": [{**source["curves"][0],
                                        "magnitude_db": _CURVES[candidate],
                                        "late_energy": {
                                            "t0_ms": 5.0, "energy_db": -20.0,
                                            "early_late_db": 1.0 if candidate == _MUTED else 4.0,
                                            "centroid_ms": 6.0 if candidate == _MUTED else 5.0,
                                        }}]})
        group = manifest_set(_banked(store, records), set_id=candidate)
        group["base"] = candidate == BASE_CANDIDATE
        groups.append(group)
    write_manifest(root, program="rear/express", groups=groups)
    _round_environment(root, applied=_SECTIONS[BASE_CANDIDATE])
    return root


def _pulse(arrival_s: float, *, gain: float = 1.0, inverted: bool = False) -> list[float]:
    """One arrival at ``arrival_s`` and nothing else in the window, from linear
    phase so a fractional-sample arrival is exact."""
    freqs = np.fft.rfftfreq(_PULSE_SAMPLES, d=1.0 / _SAMPLE_RATE_HZ)
    pulse = np.fft.irfft(np.exp(-2j * np.pi * freqs * arrival_s), n=_PULSE_SAMPLES)
    return (pulse * (-gain if inverted else gain)).tolist()


def _pair_curves(band_hz: Sequence[float] = SEAT_BAND_HZ) -> list[dict]:
    """The three segments a pair take banks, in the product's own curve shape:
    each woofer alone and their exact sum, so the trust number reads zero."""
    gain = 10.0 ** (_PAIR_LEVEL_GAP_DB / 20.0)
    front = np.ones_like(SEAT_GRID_HZ, dtype=np.complex128)
    rear = -gain * np.exp(-2j * np.pi * SEAT_GRID_HZ * _PAIR_GAP_MS / 1000.0)
    return [pose_curve_record(LateralPoseCurve(role=role, freqs_hz=SEAT_GRID_HZ,
                                               complex_tf=transfer,
                                               band_hz=(band_hz[0], band_hz[1])))
            for role, transfer in zip(rear_views.PAIR_ROLES, (front, rear, front + rear))]


def _branch_program(summed: Mapping[str, Any]) -> dict:
    """The two-channel branch program a pair take plays, through the PRODUCTION
    composer so the schedule's own ``program_id`` stays valid — the shape the
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
    take, = rear_views.pair_takes([{"branch_diagnostic": diagnostic}])
    assert all(len(impulse) == _PULSE_SAMPLES for impulse in take.impulses.values())
    for rate in (None, 0, -1, float("nan"), float("inf"), "48000"):
        assert rear_views.pair_takes([{"branch_diagnostic": {**diagnostic, "sample_rate_hz": rate}}]) == []
    for key in ("role", "pre_guard_samples", "impulse"):
        missing = {**diagnostic, "responses": [
            {field: value for field, value in response.items() if field != key}
            for response in diagnostic["responses"]]}
        assert rear_views.pair_takes([{"branch_diagnostic": missing}]) == []


def pair_round(tmp_path: Path, *, repeats: int = 2, missing: Sequence[int] = (),
               applied: str = BASE_CANDIDATE, diagnostic: bool = True,
               swept_hz: Sequence[float] = SEAT_BAND_HZ, sidecar_curves: bool = True,
               off_axis_gap_ms: float | None = None,
               behind_gap_ms: float | None = None) -> Path:
    """One banked ``rear/pair`` round: the composed candidate at every pose,
    each take banking both woofers alone, their sum and the branch diagnostic.

    Every take carries the shape the runner really banks — a two-channel
    ``candidate_branches`` program, which the summed analyzer refuses outright.
    The manifest carries the THREE role-scoped sets. ``missing`` drops the solo
    segments at named bearings; ``diagnostic`` false banks takes that analyzed
    no branches, the shape jts3 produced before #5361; ``swept_hz`` narrows the
    curves' own band so the band figures run out of bands to read;
    ``sidecar_curves`` false leaves the sidecar's ``curves`` empty and the role
    curves only on the manifest's own set rows, as a real round banks them.
    ``off_axis_gap_ms`` gives every off-axis bearing its own measured gap,
    distinct from on-axis, so a document-level pooling figure can be told apart
    from a single shared gap. ``behind_gap_ms`` additionally banks one pose
    behind the cabinet (kind ``behind``, 0.1 m) with its own gap — the
    ``rear/pair_behind`` shape (#5362) — to pin that a document-level figure
    pools bearing positions only (review, PR #5362).
    """
    root = bank_seat_round(tmp_path / "pair")
    source, store = _round_source(root)
    branch_program = _branch_program(source["program"])
    records = []
    for degrees, repeat in _poses(repeats):
        take_id = f"{_COMPOSED}-{degrees}-{repeat}"
        curves = source["curves"] if degrees in missing else _pair_curves(swept_hz)
        gap_ms = off_axis_gap_ms if degrees != 0 and off_axis_gap_ms is not None else _PAIR_GAP_MS
        records.append({**source, "take_id": take_id, "position_id": take_id, "repeat": repeat,
                        "pose_kind": "bearing", "position_deg": degrees, "vertical_deg": 0,
                        "mark_distance_m": 1.0, "measurement_purpose": "rear",
                        "gating_applied": False, "graph_scope": "candidate_branches",
                        "program": branch_program,
                        "candidate_id": _COMPOSED, "level_db": -30.0, "seat_offset_m": None,
                        **({"regime": "branches",
                            "branch_diagnostic": _branch_diagnostic(gap_ms)} if diagnostic else {}),
                        "curves": curves if sidecar_curves else []})
    if behind_gap_ms is not None:
        take_id = f"{_COMPOSED}-behind-1"
        records.append({**source, "take_id": take_id, "position_id": take_id, "repeat": 1,
                        "pose_kind": POSE_KIND_BEHIND, "position_deg": 0, "vertical_deg": 0,
                        "mark_distance_m": 0.1, "measurement_purpose": "rear",
                        "gating_applied": False, "graph_scope": "candidate_branches",
                        "program": branch_program,
                        "candidate_id": _COMPOSED, "level_db": -30.0, "seat_offset_m": None,
                        **({"regime": "branches",
                            "branch_diagnostic": _branch_diagnostic(behind_gap_ms)}
                           if diagnostic else {}),
                        "curves": _pair_curves(swept_hz) if sidecar_curves else []})
    banked = _banked(store, records)
    by_role = {str(curve["role"]): curve for curve in _pair_curves(swept_hz)}
    groups = []
    # Role order as the runner banks it, the SUM first — so a reader that kept
    # whichever set iterated last would name a solo woofer's instead.
    for role in sorted(rear_views.PAIR_ROLES):
        group = manifest_set(banked, set_id=f"{_COMPOSED}-{role}")
        group["capture_basis"].update(role=role, candidate_id=_COMPOSED)
        if not sidecar_curves:
            for take in group["takes"]:
                take["curve"] = by_role[role]
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


@pytest.mark.parametrize("error", [OSError(), ValueError(), RoundCapturesRefused("refused", {})])
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


def test_a_rear_round_packets_one_comparison_for_the_whole_batch(tmp_path, banked_candidates):
    root = rear_round(tmp_path)

    packet, views = packet_of(root)
    entry, = packet["rear"]
    comparison = entry["comparison"]
    by_candidate = {row["candidate_id"]: row for row in entry["candidates"]}
    incumbent, muted, variant = (by_candidate[name] for name in (BASE_CANDIDATE, _MUTED, _VARIANT))

    assert set(entry) == {"set_id", "comparison", "candidates", "stage", "geometry",
                          "geometry_reason", "stack", "out"}
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
    assert incumbent["headroom_change_db"] == 0.0
    assert variant["headroom_charge_db"] == pytest.approx(
        rear_branch_sum_headroom_db(_SECTIONS[_VARIANT]))
    assert variant["headroom_change_db"] == pytest.approx(
        variant["headroom_charge_db"] - incumbent["headroom_charge_db"])
    # The variant's hole AND its lower output are both reported, and the worst
    # regression names the shape figure rather than the level it also lost.
    on_axis = min(comparison["positions"])
    row = variant["positions"][on_axis]
    assert row["late_energy"]["early_late_change_db"] == 3.0
    assert row["late_energy"]["arrival_shift_ms"] == -1.0
    assert len(row["upper_bands"]) == 3
    assert comparison["coverage_hz"][1] == comparison["ceiling"]["ceiling_hz"]
    assert [band["change_db"] for band in row["upper_bands"]] == pytest.approx([-3.0] * 3)
    assert variant["positions"][on_axis]["handover"]["hole_db"] > (
        incumbent["positions"][on_axis]["handover"]["hole_db"] + 5.0)
    assert variant["positions"][on_axis]["band_level_db"] == pytest.approx(
        incumbent["positions"][on_axis]["band_level_db"] - 3.0, abs=0.2)
    assert variant["across_positions"]["worst_regression"]["figure"] == "handover.hole_db"
    assert incumbent["across_positions"]["worst_regression"]["change_db"] == 0.0
    assert {row["view"] for row in views if row["status"] == "written"} == {
        "rear", "frequency", "inventory"}
    assert json.loads((root / ARTIFACT_BY_VIEW["rear"].artifact).read_text()) == {
        key: value for key, value in entry.items() if key != "out"}


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
                             capture_gain_db=gain if kind == pose_kind else 0.0, gating_applied=False))
    groups = []
    records = [(row.path, record) for row, record in measurement_documents(bundle)]
    for candidate in gains:
        group = manifest_set([(path, record) for path, record in records
                              if record["candidate_id"] == candidate], set_id=candidate)
        group["base"] = candidate == BASE_CANDIDATE
        groups.append(group)
    write_manifest(bundle, program=f"rear/{pose_kind}", groups=groups)
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
        assert set(front) == {"reason", "dip", "dip_shift", "ripple_db", "handover", "low_bass",
                              "band_level_db", "late_energy", "upper_bands", "ladder"}
        assert [band["band_hz"] for band in front["upper_bands"]] == (
            [list(band) for band in LEVEL_BANDS_HZ[-3:]] if covered_bands == 7 else [])
        for band in front["upper_bands"]:
            assert set(band) == {"band_hz", "level_db", "reference_db", "change_db"}
            assert band["change_db"] == pytest.approx(0.0, abs=0.01)
        assert placed["upper_bands"] == []
        assert placed["reason"] == ""
        assert isinstance(placed["ripple_db"], float)
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
        for row in candidate["positions"].values():
            assert row["late_energy"]["reason"] == REASON_NO_COMPARISON
            assert row["late_energy"]["early_late_change_db"] is None
            assert row["upper_bands"] == []
    assert set(entry["comparison"]["repeat_spread"]["spread_db"].values()) == {None}
    assert all(row["across_positions"]["worst_regression"]["exceeds_repeat_spread"] is None
               for row in entry["candidates"])


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
        assert row["upper_bands"] == []
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
                          "geometry", "geometry_reason", "stack", "out"}
    # Three role-scoped sets, one candidate: the document names the SUM's set
    # rather than whichever role a candidate-keyed dict iterated last.
    assert (entry["candidates"], entry["set_id"]) == ([], _PAIR_SET_ID)
    assert entry["pair"]["candidate_id"] == _COMPOSED
    # The run composed what it played, and the packet says from what.
    assert entry["pair"]["source"] == {"candidate_id": BASE_CANDIDATE,
                                       "resolution": "cleared", "reason": ""}
    assert comparison["reference"] == {"candidate_id": _COMPOSED, "kind": "pair",
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
    # The frequency view reads the summed analyzer, which refuses a branch
    # take's program, so a pair round banks none — as the real round does.
    assert {r["view"] for r in views if r["status"] == "written"} == {"rear", "inventory"}
    frequency_row = next(r for r in views if r["view"] == "frequency")
    assert (frequency_row["status"], frequency_row["reason"]) == (
        "unavailable", "measurement_analysis_program_unsupported")
    assert json.loads((root / ARTIFACT_BY_VIEW["rear"].artifact).read_text()) == {
        key: value for key, value in entry.items() if key != "out"}


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


def test_a_pair_round_never_asks_the_summed_analyzer(tmp_path, banked_candidates, monkeypatch):
    """A branch take's program is two-channel and ``candidate_branches``-scoped,
    which the summed analyzer refuses outright — so the pair path must not ask
    it. Read with the REAL analyzer restored, which is what the round hits on
    the box, and with the role curves only on the manifest's own set rows."""
    monkeypatch.setattr(room_selection, "analyzed_measurements",
                        measurement_analysis.analyzed_measurements)
    root = pair_round(tmp_path, sidecar_curves=False)
    inputs = round_inputs(root)

    packet, views = packet_of(root)
    entry, = packet["rear"]

    # The fixture really does carry the shape the analyzer cannot read.
    with pytest.raises(measurement_analysis.MeasurementAnalysisRefused) as refused:
        list(measurement_analysis.analyzed_measurements(inputs.session_dir))
    assert refused.value.code == "measurement_analysis_program_unsupported"
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
    assert row["detail"] == {"candidates": [_COMPOSED], "takes": 4}
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
