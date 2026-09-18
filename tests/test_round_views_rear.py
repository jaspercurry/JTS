# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The rear comparison a finished ``rear`` round carries in its packet.

The curves are synthesized from a direct sound plus one rigid image source at
the declared wall, so these pins prove arithmetic and plumbing only — no
figure here is evidence about a real cabinet.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import numpy as np
import pytest

from jasper.active_speaker.angle_capture import BASE_CANDIDATE
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.candidate_bank import CandidateBankRefusal
from jasper.active_speaker.crossover_v2 import rear_views
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.rear_calibration import diagnostic_seed
from jasper.active_speaker.round_bank import _bookkeeping
from jasper.active_speaker.round_packet import write_round_packet
from jasper.active_speaker.round_view_artifacts import ARTIFACT_BY_VIEW
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.rear_evidence import (
    BAND_SOURCE_MEASURED_DIP, REASON_NO_REPEATS,
)
from jasper.cli import round_views
from tests.crossover_v2_banked_round import SEAT_GRID_HZ, _reopen, bank_seat_round
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.room_median_fixture import analyzed_room_documents as analyzed_room_documents
from tests.test_active_speaker_audition import _applied_profile
from tests.test_active_speaker_runtime_contract import _active_topology

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


def _wall_curve_db(strength: float, *, output_db: float = 0.0, hole_db: float = 0.0) -> list[float]:
    """Direct sound plus one rigid image source, minus an optional hand-over hole."""
    excess_s = 2.0 * _WALL_M / DEFAULT_SOUND_SPEED_M_S
    summed = 1.0 + strength * np.exp(-2j * np.pi * SEAT_GRID_HZ * excess_s)
    hole = hole_db * np.exp(-0.5 * (np.log2(SEAT_GRID_HZ / _HANDOVER_HZ) / 0.12) ** 2)
    return (-30.0 + output_db + 20.0 * np.log10(np.abs(summed)) - hole).tolist()


#: A muted rear radiates into the wall and digs a deep dip; the incumbent's
#: cardioid leaves a shallow one; the variant keeps that dip but loses output
#: and opens a hole where its moved corner no longer hands over.
_CURVES = {
    BASE_CANDIDATE: _wall_curve_db(0.4),
    _MUTED: _wall_curve_db(0.95),
    _VARIANT: _wall_curve_db(0.4, output_db=-3.0, hole_db=8.0),
}


@pytest.fixture
def banked_candidates(monkeypatch):
    """The candidate bank, which answers a fingerprint with its rear section."""
    def find(fingerprint, *, root=None):
        if fingerprint not in _SECTIONS:
            raise CandidateBankRefusal("not_found", fingerprint)
        return SimpleNamespace(
            candidate=SimpleNamespace(rear_calibration=_SECTIONS[fingerprint]))

    monkeypatch.setattr(rear_views, "find_banked_candidate", find)


def _banked(store: Any, records: Sequence[Mapping[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
    """Every record through the product's own banker, with the path it filed it at."""
    async def bank() -> list[str]:
        return [await store.bank(record) for record in records]

    return list(zip(asyncio.run(bank()), records))


def rear_round(tmp_path: Path, *, candidates=(BASE_CANDIDATE, _MUTED, _VARIANT),
               repeats: int = 2, missing: Mapping[str, Sequence[int]] = {}) -> Path:
    """One banked ``rear`` round: every candidate at every pose, on-axis repeated.

    ``missing`` drops a candidate's take at named bearings, which is how a
    reference take goes missing where other candidates measured.
    """
    root = bank_seat_round(tmp_path / "rear")
    inputs = round_inputs(root)
    source = next(record for _, record in measurement_documents(inputs.session_dir))
    source = {key: value for key, value in source.items()
              # The store writes these two and refuses a record that carries them.
              if key not in ("schema_version", "capture_session_id")}
    store, _identity = _reopen(root)
    groups = []
    for candidate in candidates:
        records = []
        for degrees, repeat in [(0, index + 1) for index in range(repeats)] + [(-20, 1), (20, 1)]:
            if degrees in missing.get(candidate, ()):
                continue
            take_id = f"{candidate}-{degrees}-{repeat}"
            records.append({**source, "take_id": take_id, "position_id": take_id, "repeat": repeat,
                            "pose_kind": "bearing", "position_deg": degrees, "vertical_deg": 0,
                            "mark_distance_m": 1.0, "measurement_purpose": "rear",
                            "gating_applied": False, "graph_scope": "candidate",
                            "candidate_id": candidate, "level_db": -30.0,
                            "seat_offset_m": None,
                            "curves": [{**source["curves"][0],
                                        "magnitude_db": _CURVES[candidate]}]})
        group = manifest_set(_banked(store, records), set_id=candidate)
        group["base"] = candidate == BASE_CANDIDATE
        groups.append(group)
    write_manifest(root, program="rear/express", groups=groups)
    DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0,
                     **_CABINET).save(root / "declared-geometry.json")
    profile = _applied_profile(_active_topology("mono", "active_2_way"))
    profile.update(kind=BASELINE_PROFILE_KIND, artifact_schema_version=SCHEMA_VERSION)
    profile["recomposition_snapshot"]["rear_calibration"] = _SECTIONS[BASE_CANDIDATE]
    (root / "applied-profile.json").write_text(json.dumps(profile))
    return root


def packet_of(root: Path) -> tuple[dict, list[dict]]:
    inputs = round_inputs(root)
    manifest_path, views = _bookkeeping(root, inputs.session_dir, round_views.run_bookkeeping)
    return write_round_packet(root, manifest_path, views), views


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
    assert comparison["band_source"] == BAND_SOURCE_MEASURED_DIP
    assert comparison["band_dip_hz"] == pytest.approx(_DIP_HZ, rel=0.05)
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
    assert set(entry["comparison"]["repeat_spread"]["spread_db"].values()) == {None}
    assert all(row["across_positions"]["worst_regression"]["exceeds_repeat_spread"] is None
               for row in entry["candidates"])


@pytest.mark.parametrize("program", ["room", "bass"])
def test_another_purpose_gets_no_rear_entry(tmp_path, program):
    root = bank_seat_round(tmp_path / program)
    write_manifest(root, program=program)

    packet, views = packet_of(root)

    assert packet["rear"] == []
    assert packet["artifacts"]["rear_views"] == []
    assert "rear" not in {row["view"] for row in views}
    assert not (root / ARTIFACT_BY_VIEW["rear"].artifact).exists()
