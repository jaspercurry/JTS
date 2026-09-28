# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import nearfield_view as nv, round_inputs
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.audio_measurement.gating import f_trusted_floor_hz
from jasper.audio_measurement.level import piston_step_db
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.cli import round_views
from tests.run_manifest_fixture import write_manifest

# A banked curve's grid runs to 20 kHz; a near-field sweep stops at 2 kHz.
FREQS = np.geomspace(20.0, 20_000.0, 600)
STEP = piston_step_db(0.015, 0.030, 0.057)


def _take(take_id, driver, distance_mm, level_db, *, selected=True, first_low_db=0.0, seed=0, band_hz=(20.0, 2000.0),
          stimulus_dbfs=-32.0, kind="close", gate_ms=()):
    rng = np.random.default_rng(seed)
    sweeps = [np.full(FREQS.size, level_db) + rng.normal(0.0, 0.01, FREQS.size) for _ in range(3)]
    sweeps[0] = sweeps[0] + np.where(FREQS < 35.0, first_low_db, 0.0)
    # Each sweep's gate as its banked curve states it; ``None`` is a sweep left ungated.
    gates = [{"window": "ungated"} if ms is None else {"window": "gated", "gate_window_ms": ms, "validity_floor_hz": 1000.0 / ms,
                                                       "trusted_floor_hz": 2500.0 / ms, "floor_source": "measured_reflection"}
             for ms in gate_ms] or [{}] * 3
    curve = {"freqs_hz": FREQS.tolist(), "magnitude_db": sweeps[0].tolist(), "band_hz": list(band_hz), **gates[0],
             "repeat_curves": [{"freqs_hz": FREQS.tolist(), "magnitude_db": sweep.tolist(), **gate}
                               for sweep, gate in zip(sweeps[1:], gates[1:])]}
    return {"take_id": take_id, "selected": selected,
            "pose": {"kind": kind, "driver": driver, "distance_m": distance_mm / 1000},
            "quality": {"evidence": {"level_db_spl": 80.0}}, "curve": curve,
            "level": {"level_db": -30.0, "stimulus_dbfs": stimulus_dbfs},
            "artifacts": {"record_id": f"crossover_v2/run/positions/{take_id}.json"}}


def _graph(pad_db):
    """A played graph: program channel 0 to output 0 through a pad, output 1 parked."""
    return {"devices": {"samplerate": 48000, "capture": {"channels": 2}, "playback": {"channels": 2}},
            "mixers": {"route": {"channels": {"in": 2, "out": 2},
                                 "mapping": [{"dest": 0, "sources": [{"channel": 0, "gain": 0.0, "inverted": False}]}]}},
            "filters": {"pad": {"type": "Gain", "parameters": {"gain": pad_db}}},
            "pipeline": [{"type": "Mixer", "name": "route"}, {"type": "Filter", "channels": [0], "names": ["pad"]}]}


@pytest.mark.parametrize("diameters,rear_extra_db,verdicts", [
    ({"woofer": 114.0, "woofer:rear": 114.0}, -0.2, ("pass", "pass")),
    ({"woofer": 114.0, "woofer:rear": 114.0}, -1.0, ("pass", "fail")),
    ({}, -0.2, ("not_evaluated", "not_evaluated")),
])
def test_a_near_field_round_reads_band_by_band_and_self_tests_its_distances(diameters, rear_extra_db, verdicts):
    """Kept takes only, band by band: the first sweep against the two after it,
    and the SNR of the last two. Each driver's re-seat spread, and its level
    step between distances held to a piston of the declared cone (#5684)."""
    takes = [
        _take("w15", "woofer", 15, 90.0, first_low_db=-1.0), _take("w30", "woofer", 30, 90.15 + STEP - 0.1, seed=1),
        _take("w15again", "woofer", 15, 90.3, seed=2), _take("opener", "woofer", 15, 66.0, selected=False),
        _take("r15", "woofer:rear", 15, 84.0, seed=3), _take("r30", "woofer:rear", 30, 84.0 + STEP + rear_extra_db, seed=4),
    ]

    view = nv.nearfield_view(takes, radiating_diameter_mm_by_target=diameters)

    assert [row["take_id"] for row in view["takes"]] == ["w15", "w30", "w15again", "r15", "r30"]
    lowest = view["takes"][0]["bands"][0]
    assert (lowest["band_hz"], lowest["trusted"]) == ([20.0, 35.0], True)
    assert lowest["first_minus_rest_db"] == pytest.approx(-1.0, abs=0.05)
    woofer, rear = view["drivers"]
    assert (woofer["driver"], rear["driver"]) == ("woofer", "woofer:rear")
    assert tuple(driver["steps"][0]["verdict"] for driver in (woofer, rear)) == verdicts
    assert {(placement["trusted_band"]["high_source"], placement["trusted_band"]["undeclared"])
            for driver in (woofer, rear) for placement in driver["placements"]} == {
        ("near_field_limit", ()) if diameters else (None, ("driver_size_undeclared",))}
    assert [driver["steps"][0]["step_band_hz"] for driver in (woofer, rear)] == [[35.0, 400.0]] * 2
    top = view["takes"][0]["bands"][-1]
    assert (top["band_hz"], top["snr_db"] > 0, top["trusted"]) == ([800.0, 2000.0], True, not diameters)
    reseat = woofer["placements"][0]
    assert reseat["take_ids"] == ["w15", "w15again"]
    assert reseat["reseat_spread_db"][2] == pytest.approx(0.3, abs=0.05)


def test_a_drivers_distance_step_stops_at_its_own_trusted_band():
    """A 305 mm cone's ka reaches 1 at 358 Hz, under the step band's 400 Hz top,
    so a level change between them moves only a smaller cone's step (ADR-0366),
    and a rear woofer's band is its own cone's, not its front's (ADR-0384)."""
    far = _take("r30", "woofer:rear", 30, 90.0 + STEP, seed=1)
    for sweep in (far["curve"], *far["curve"]["repeat_curves"]):
        sweep["magnitude_db"] = [db + 6.0 * (360.0 < hz < 400.0) for hz, db in zip(FREQS, sweep["magnitude_db"])]
    steps = [nv.nearfield_view([_take("r15", "woofer:rear", 15, 90.0), far],
                               radiating_diameter_mm_by_target={"woofer": 114.0, "woofer:rear": mm})
             ["drivers"][0]["steps"][0]["step_db"] for mm in (305.0, 114.0)]
    assert steps[0] == pytest.approx(STEP, abs=0.05) and steps[1] > STEP + 0.3


def test_a_driver_in_front_and_behind_reads_apart_and_each_take_states_its_gate():
    """A driver's takes in front and behind at one distance are two placements,
    never pooled, and a step pairs two distances of one kind only. Each take
    states the gate its sweeps ran: gated only if every sweep was, at the
    shortest window (ADR-0383 §2)."""
    takes = [_take("front", "woofer", 500, 86.0, kind="bearing", gate_ms=(6.0, 5.0, 6.0)),
             _take("behind", "woofer", 500, 70.0, seed=1, kind="behind", gate_ms=(6.0, None, 6.0)),
             _take("behind_far", "woofer", 1000, 64.0, seed=2, kind="behind")]

    view = nv.nearfield_view(takes, radiating_diameter_mm_by_target={"woofer": 114.0})

    ungated = {"window": "ungated", **dict.fromkeys(("window_ms", "validity_floor_hz", "trusted_floor_hz", "floor_source"))}
    assert [row["gate"] for row in view["takes"]] == [
        {"window": "gated", "window_ms": 5.0, "validity_floor_hz": 200.0, "trusted_floor_hz": 500.0,
         "floor_source": "measured_reflection"}, ungated, ungated]
    driver, = view["drivers"]
    assert [(placement["distance_mm"], placement["kind"], placement["take_ids"]) for placement in driver["placements"]] == [
        (500.0, "bearing", ["front"]), (500.0, "behind", ["behind"]), (1000.0, "behind", ["behind_far"])]
    assert [(step["near_mm"], step["far_mm"]) for step in driver["steps"]] == [(500.0, 1000.0)]


def test_placements_past_the_near_field_read_gated_in_the_rounds_room(tmp_path, monkeypatch, capsys):
    """One-driver takes past 100 mm read gated, each placement from the round's
    declared room at its own distance, a pose at the mark at 1 m. Both gate
    floors sit above the whole step band, so the step says so rather than
    grading, and no band row below a floor is trusted (ADR-0366)."""
    room = DeclaredGeometry(speaker_height_m=1.0, mic_height_m=1.0, distance_m=1.0)
    room.save(tmp_path / "geometry.json")
    monkeypatch.setattr(round_inputs, "DECLARED_GEOMETRY_DEFAULT_PATH", tmp_path / "geometry.json")
    bundle = tmp_path / "sessions" / "nearfield"
    bundle.mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": bundle.name}))
    mark = _take("mark", "woofer", 1000, 80.0, seed=1)
    mark["pose"]["distance_m"] = None
    write_manifest(bundle, program="nearfield/each",
                   groups=[{"set_id": "nearfield", "capture_basis": {}, "takes": [_take("w500", "woofer", 500, 86.0), mark]}])

    assert round_views.main(["nearfield", str(bundle), "--out", str(tmp_path / "nearfield.json")]) == 0
    answer = json.loads(capsys.readouterr().out)

    driver, = answer["drivers"]
    assert [(placement["distance_mm"], placement["trusted_band"]["low_hz"]) for placement in driver["placements"]] == [
        (distance_m * 1000, pytest.approx(f_trusted_floor_hz(room.first_bounce_s(distance_m)))) for distance_m in (0.5, 1.0)]
    assert [(step["step_band_hz"], step["step_db"], step["verdict"]) for step in driver["steps"]] == [
        (None, None, "not_evaluated")]
    rows = json.loads(Path(answer["out"]).read_text())["takes"][0]["bands"]
    assert [row["trusted"] for row in rows] == [False] * 6 + [True]


def test_a_placement_states_the_band_its_take_banked(tmp_path, monkeypatch, capsys):
    """A take states the band it banked, as the declarations stood when it was
    measured; a take banked without one states it from the round's
    declarations (ADR-0366 §3)."""
    monkeypatch.setattr(round_inputs, "DECLARED_GEOMETRY_DEFAULT_PATH", tmp_path / "undeclared.json")
    bundle = tmp_path / "sessions" / "nearfield"
    bundle.mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": bundle.name}))
    banked = {"low_hz": 123.0, "low_source": "gate_floor", "high_hz": 4000.0, "high_source": "far_field_ceiling",
              "undeclared": []}
    take, unbanked = _take("w500", "woofer", 500, 86.0), _take("w1000", "woofer", 1000, 80.0, seed=1)
    record = take_artifact_path(bundle, take["artifacts"]["record_id"])
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"trusted_band": banked}))
    write_manifest(bundle, program="drivers/each",
                   groups=[{"set_id": "drivers", "capture_basis": {}, "takes": [take, unbanked]}])

    assert round_views.main(["nearfield", str(bundle), "--out", str(tmp_path / "nearfield.json")]) == 0

    driver, = json.loads(capsys.readouterr().out)["drivers"]
    stated, stated_here = (placement["trusted_band"] for placement in driver["placements"])
    assert stated == banked
    assert (stated_here["low_source"], stated_here["undeclared"]) == ("gate_floor", ["room_undeclared", "driver_size_undeclared"])


def test_a_driver_reads_raw_with_its_fader_and_played_graph_divided_out():
    """A placement's raw curve pools its takes' settled sweeps with the fader
    and the played graph divided out, so takes played through different pads
    read one driver on one reference, over the band they swept only. A take
    whose graph was not read back, or cannot be modelled, or whose curve sits
    on another grid stays out of it, and the rest of the view still reads (#5713)."""
    coarse = _take("coarse", "woofer", 15, 70.0, seed=3)
    coarse["curve"] = {**coarse["curve"], **{key: coarse["curve"][key][::2] for key in ("freqs_hz", "magnitude_db")},
                       "repeat_curves": [{key: value[::2] for key, value in sweep.items()}
                                         for sweep in coarse["curve"]["repeat_curves"]]}
    unmodelled = _graph(-6.0)
    unmodelled["pipeline"].append({"type": "Processor", "name": "compressor"})
    takes = [_take("a", "woofer", 15, 60.0, first_low_db=-3.0), _take("b", "woofer", 15, 54.0, seed=1),
             _take("unread", "woofer", 15, 70.0, seed=2), _take("unmodelled", "woofer", 15, 70.0, seed=4), coarse]

    view = nv.nearfield_view(takes, radiating_diameter_mm_by_target={}, played_graphs={
        "a": _graph(-6.0), "b": _graph(-12.0), "unmodelled": unmodelled, "coarse": _graph(-6.0)})

    raw = view["drivers"][0]["placements"][0]["raw"]
    assert raw["take_ids"] == ["a", "b"]
    assert raw["freqs_hz"] == pytest.approx(FREQS[FREQS <= 2000.0].tolist(), abs=1e-3)
    assert np.asarray(raw["level_db"]) == pytest.approx(96.0, abs=0.05)


def test_a_take_is_read_only_where_its_sweep_reached():
    """Outside its sweep a curve is noise: a take swept from 700 Hz reads only
    the bands it swept whole, and gives no distance step (#5684)."""
    takes = [_take("t15", "tweeter", 15, 80.0, band_hz=(700.0, 2000.0)),
             _take("t30", "tweeter", 30, 78.0, seed=1, band_hz=(700.0, 2000.0))]

    view = nv.nearfield_view(takes, radiating_diameter_mm_by_target={"tweeter": 25.0})

    assert [[band["band_hz"] for band in row["bands"]] for row in view["takes"]] == [[[800.0, 2000.0]]] * 2
    assert view["drivers"][0]["steps"] == []


@pytest.mark.parametrize("driver,distance_mm,stimulus_dbfs,kind,flagged", [
    ("woofer:rear", 15, -26.0, "close", True), ("woofer:rear", 15, -30.0, "close", False),
    ("tweeter", 15, -26.0, "close", False), ("woofer:rear", 30, -26.0, "close", False),
    ("woofer:rear", 15, -26.0, "bearing", False),
], ids=["6_db_apart", "2_db_apart", "another_role", "another_placement", "a_far_pose"])
def test_drivers_of_one_size_at_one_placement_are_flagged_when_they_play_apart(
        tmp_path, monkeypatch, capsys, driver, distance_mm, stimulus_dbfs, kind, flagged):
    """Two drivers of one role at one near-field position are compared per unit of
    drive: the median over each driver's kept takes of level less stimulus gain
    and fader. More than 3 dB apart is flagged; a take not kept, another role,
    another position or a far pose never compares (#5714)."""
    monkeypatch.setattr(round_inputs, "DECLARED_GEOMETRY_DEFAULT_PATH", tmp_path / "undeclared.json")
    bundle = tmp_path / "sessions" / "nearfield"
    bundle.mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": bundle.name}))
    # The compared driver's three kept takes read 1 dB over, at, and 6 dB under
    # its median, in that order, so a first, last or mean reading moves the spread.
    takes = [_take("w15", "woofer", 15, 90.0), _take("probe", driver, distance_mm, 60.0, selected=False, stimulus_dbfs=-44.0),
             *(_take(take_id, driver, distance_mm, 84.0, seed=1, stimulus_dbfs=stimulus_dbfs + offset, kind=kind)
               for take_id, offset in (("over", -1.0), ("other", 0.0), ("under", 6.0)))]
    write_manifest(bundle, program="nearfield/each", groups=[{"set_id": "nearfield", "capture_basis": {}, "takes": takes}])

    assert round_views.main(["nearfield", str(bundle), "--out", str(tmp_path / "nearfield.json")]) == 0

    assert json.loads(capsys.readouterr().out)["level_mismatches"] == ([{
        "role": "woofer", "pose": {"distance_m": 0.015, "kind": "close"}, "spread_db": 6.0,
        "unit_drive_db_spl": {"woofer": 142.0, "woofer:rear": 136.0}}] if flagged else [])
