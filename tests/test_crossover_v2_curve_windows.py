# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The window each reader of a take's curves reads (ADR-0383 §2)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import room_selection
from jasper.active_speaker.crossover_v2.candidate_ladder import candidate_ladder
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.feature_classifier import load_round_pose_curves
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.position_cycle import (
    OWN_WINDOW, select_pose_curve_pair, take_curve, take_window,
)
from jasper.active_speaker.crossover_v2.rear_views import PAIR_ROLES, _pair_segments
from jasper.active_speaker.crossover_v2.record_index import Measurement
from jasper.active_speaker.crossover_v2.room_views import room_ceiling
from jasper.active_speaker.crossover_v2.round_captures import record_captures
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.measurement_programs import PURPOSE_SPEAKER
from jasper.active_speaker.round_packet import _packet_takes
from jasper.active_speaker.round_verdicts import common_measured_band
from jasper.active_speaker.speaker_fit import design_clouds
from tests.run_manifest_fixture import write_manifest
from tests.test_crossover_v2_position_cycle import _bank, _record

ROLES = ("woofer", "tweeter", *PAIR_ROLES[1:])
#: Each window's level and trusted floor, so a reader's answer names the window it read.
LEVEL_DB = {"gated": 0.0, "ungated": -6.0}
FLOOR_HZ = {"gated": 300.0, "ungated": 450.0}


def _curve(role: str, window: str) -> dict:
    return {"role": role, "window": window, "band_hz": [100.0, 10000.0], "freqs_hz": [100.0, 1000.0, 10000.0],
            "magnitude_db": [LEVEL_DB[window]] * 3, "phase_deg": [0.0] * 3, "trusted_floor_hz": FLOOR_HZ[window],
            "validity_floor_hz": None, "gate_window_ms": 5.0 if window == "gated" else None, "repeat_curves": []}


def _banked(order: tuple[str, ...], *, gate_missed: tuple[str, ...] = (), index: int = 1, candidate: str = "") -> dict:
    """A kept speaker take at 0°, each role banked through ``order``'s windows in
    that order; a role in ``gate_missed`` banks its ungated curve alone, as a
    response whose gate found no window."""
    return {**_record(index, 0, candidate_id=candidate), "kind": POSITION_EVIDENCE_KIND,
            "measurement_purpose": PURPOSE_SPEAKER, "selected": True,
            "curves": [_curve(role, window) for window in order for role in ROLES
                       if window == "ungated" or role not in gate_missed],
            "branch_diagnostic": {"sample_rate_hz": 48000, "timing_reference": "schedule", "clock_epsilon_ppm": 0.0,
                                  "global_offset_samples": 0, "responses": [
                                      {"role": role, "impulse": [0.0, 1.0, 0.0], "band_hz": [100.0, 10000.0],
                                       "clock_shift_samples": 0.0, "segment_id": role, "pre_guard_samples": 1,
                                       "scheduled_start_sample": 0} for role in ROLES]}}


def _bundle(root: Path, *takes: dict) -> Path:
    _bank(root, list(takes))
    write_manifest(root)
    return root / "bundle" / "sess-1"


def _by_level(level_db: float) -> str:
    return next(window for window, level in LEVEL_DB.items() if np.isclose(level, level_db))


def _by_floor(floor_hz: float) -> str:
    return next(window for window, floor in FLOOR_HZ.items() if floor == floor_hz)


def _only(windows) -> str:
    """The one window every curve a reader returned was read through."""
    (window,) = set(windows)
    return window


def _ladder_windows(order: tuple[str, ...], root: Path) -> str:
    """The windows the candidate ladder compares two candidates' takes through."""
    _bundle(root, _banked(order, index=1, candidate="cfg-a"), _banked(order, index=2, candidate="cfg-b"))
    ladder = candidate_ladder(root, round_inputs(root))
    return _only(row["window"] for table in ladder["tables"] for row in table["roles"])


def _cloud_windows(order: tuple[str, ...], root: Path) -> str:
    """The window a speaker fit's design cloud reads from two bearings' takes."""
    takes = [{**_banked(order, index=index), "phase": "measure", "captured_at": f"2026-09-29T12:00:0{index}Z",
              "pose": {"kind": "bearing", "deg": deg, "elevation_deg": 0}} for index, deg in ((1, 0), (2, 20))]
    cloud = design_clouds({"sets": [{"set_id": "woofer", "capture_basis": {"role": "woofer"}, "takes": takes}]})
    return _only(_by_level(response.magnitude_db[0]) for response in cloud["woofer"].boost_responses)


_ROW = Measurement("p", "sess-1", "", PHASE_LATERAL, 0, 0, "", None, "", "", "bearing")

#: Each changed reader, as the window it read from one take: room and rear
#: readers read ungated, speaker readers gated, and the take views, the page and
#: the packet the take's own window. The seat selection, the rear pair and the
#: packet are read through their private step: their public paths need a
#: recorded capture per take, a rear pair round and a whole banked packet.
READERS = {
    "take_curve": (lambda order, root: take_curve(_banked(order), "summed", OWN_WINDOW)["window"], "gated"),
    "room_selection": (lambda order, root: _by_level(room_selection._take(_ROW, _banked(order)).magnitude_db[0]),
                       "ungated"),
    "rear_pair": (lambda order, root: _by_level(20 * np.log10(abs(_pair_segments(_banked(order))[1]["summed"][0]))),
                  "ungated"),
    "frequency_series": (lambda order, root: _only(_by_level(series.magnitude_db[0]) for series in
                                                  frequency_run_from_documents(run_id="r", documents=[_banked(order)])
                                                  .series), "gated"),
    "round_verdicts": (lambda order, root: _by_floor(common_measured_band([_banked(order)], "woofer")[0]), "gated"),
    "design_cloud": (_cloud_windows, "gated"),
    "round_packet": (lambda order, root: "gated" if _packet_takes({
        "set_id": "s", "capture_basis": {"role": "woofer"}, "takes": [_banked(order)]})[0]["gate_window_ms"]
        else "ungated", "gated"),
    "round_captures": (lambda order, root: record_captures(_banked(order), ("woofer",), root, record_path=Path("t.json"),
                                                           wav=Path("t.wav"))[0].curve["window"], "gated"),
    "candidate_ladder": (_ladder_windows, "gated"),
    "delay_pair": (lambda order, root: select_pose_curve_pair(
        _bundle(root, _banked(order)), phases=(PHASE_LATERAL,), position_deg=0,
        roles=("woofer", "tweeter")).lower["window"], "gated"),
    "pose_bank": (lambda order, root: _only(_by_level(curve.magnitude_db[0]) for curve in
                                           load_round_pose_curves(_bundle(root, _banked(order)))), "gated"),
    "room_ceiling": (lambda order, root: _by_floor(room_ceiling(_bundle(root, _banked(order))).trusted_floor_hz),
                     "gated"),
}


@pytest.mark.parametrize("order", [("gated", "ungated"), ("ungated", "gated")])
@pytest.mark.parametrize("reader", READERS)
def test_each_reader_names_the_window_it_reads(tmp_path, reader, order):
    """A take whose gate applied banks each role through both windows; each
    reader reads the one it names in either order, so none reads a role's
    first curve (ADR-0383 §2)."""
    read, window = READERS[reader]
    assert read(order, tmp_path) == window


def _delay_pair(take: dict, root: Path) -> dict:
    pair = select_pose_curve_pair(_bundle(root, take), phases=(PHASE_LATERAL,), position_deg=0,
                                  roles=("woofer", "tweeter"))
    return {} if pair is None else {"woofer": pair.lower["window"], "tweeter": pair.upper["window"]}


OWN = {"woofer": "gated", "tweeter": "ungated"}
#: A reader's windows by role, read from one take, and what it should read.
MIXED_READERS = {
    "take_curve": (lambda take, root: {role: take_curve(take, role, OWN_WINDOW, required=True)["window"]
                                       for role in ("woofer", "tweeter")}, OWN),
    "frequency_series": (lambda take, root: {series.details["role"]: _by_level(series.magnitude_db[0]) for series in
                                             frequency_run_from_documents(run_id="r", documents=[take]).series
                                             if series.details["role"] in ("woofer", "tweeter")}, OWN),
    "delay_pair": (_delay_pair, {}),
    "round_verdicts": (lambda take, root: {role: _by_floor(band[0]) for role in ("woofer", "tweeter")
                                           if (band := common_measured_band([take], role))}, {"woofer": "gated"}),
    "pose_bank": (lambda take, root: {curve.role: _by_level(curve.magnitude_db[0]) for curve in
                                      load_round_pose_curves(_bundle(root, take)) if curve.role in ("woofer", "tweeter")},
                  {"woofer": "gated"}),
}


@pytest.mark.parametrize("reader", MIXED_READERS)
def test_each_role_is_read_through_the_window_its_reader_names(tmp_path, reader):
    """A take whose gate windowed the woofer but found no window for the
    tweeter banks woofer [gated, ungated] and tweeter [ungated]. A reader of
    the take's own window reads each role through that role's own; a speaker
    reader names the gated window, so it reads no tweeter, and a pair of both
    drivers finds none."""
    take = _banked(("gated", "ungated"), gate_missed=("tweeter",))
    read, expected = MIXED_READERS[reader]

    assert (take_window(take, "woofer"), take_window(take, "tweeter")) == ("gated", "ungated")
    assert read(take, tmp_path) == expected
