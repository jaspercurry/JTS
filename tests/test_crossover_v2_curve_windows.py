# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The window each reader of a take's curves reads (ADR-0383 §2)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import room_selection
from jasper.active_speaker.crossover_v2.candidate_ladder import _lateral_takes
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.feature_classifier import load_round_pose_curves
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.position_cycle import select_pose_curve_pair, take_curve, take_window
from jasper.active_speaker.crossover_v2.rear_views import PAIR_ROLES, _pair_segments
from jasper.active_speaker.crossover_v2.record_index import Measurement
from jasper.active_speaker.crossover_v2.room_views import room_ceiling
from jasper.active_speaker.crossover_v2.round_captures import record_captures
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.measurement_programs import PURPOSE_SPEAKER
from jasper.active_speaker.round_packet import _packet_takes
from jasper.active_speaker.round_verdicts import common_measured_band
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


def _banked(windows: tuple[str, ...]) -> dict:
    """A kept speaker take at 0°, each role banked through ``windows``, in that order."""
    return {**_record(1, 0), "kind": POSITION_EVIDENCE_KIND, "measurement_purpose": PURPOSE_SPEAKER, "selected": True,
            "curves": [_curve(role, window) for window in windows for role in ROLES],
            "branch_diagnostic": {"sample_rate_hz": 48000, "timing_reference": "schedule", "clock_epsilon_ppm": 0.0,
                                  "global_offset_samples": 0, "responses": [
                                      {"role": role, "impulse": [0.0, 1.0, 0.0], "band_hz": [100.0, 10000.0],
                                       "clock_shift_samples": 0.0, "segment_id": role, "pre_guard_samples": 1,
                                       "scheduled_start_sample": 0} for role in ROLES]}}


def _bundle(root: Path, take: dict) -> Path:
    _bank(root, [take])
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


_ROW = Measurement("p", "sess-1", "", PHASE_LATERAL, 0, 0, "", None, "", "", "bearing")

#: Each changed reader, as the window it read from one take: the rear pair reads
#: ungated, the room ceiling gated, every other reader the take's own window.
READERS = {
    "take_curve": (lambda take, root: take_curve(take, "summed")["window"], "gated"),
    "room_selection": (lambda take, root: _by_level(room_selection._take(_ROW, take).magnitude_db[0]), "gated"),
    "rear_pair": (lambda take, root: _by_level(20 * np.log10(abs(_pair_segments(take)[1]["summed"][0]))), "ungated"),
    "frequency_series": (lambda take, root: _only(_by_level(series.magnitude_db[0]) for series in
                                                  frequency_run_from_documents(run_id="r", documents=[take]).series),
                         "gated"),
    "round_verdicts": (lambda take, root: _by_floor(common_measured_band([take], "woofer")[0]), "gated"),
    "round_packet": (lambda take, root: "gated" if _packet_takes({"set_id": "s", "capture_basis": {"role": "woofer"},
                                                                  "takes": [take]})[0]["gate_window_ms"] else "ungated",
                     "gated"),
    "round_captures": (lambda take, root: record_captures(take, ("woofer",), root, record_path=Path("take.json"),
                                                          wav=Path("take.wav"))[0].curve["window"], "gated"),
    "candidate_ladder": (lambda take, root: _only(curve["window"] for curve in next(_lateral_takes(
        _bundle(root, take), root / "none.json")).curves), "gated"),
    "delay_pair": (lambda take, root: select_pose_curve_pair(
        _bundle(root, take), phases=(PHASE_LATERAL,), position_deg=0, roles=("woofer", "tweeter")).lower["window"],
                   "gated"),
    "pose_bank": (lambda take, root: _only(_by_level(curve.magnitude_db[0]) for curve in
                                           load_round_pose_curves(_bundle(root, take))), "gated"),
    "room_ceiling": (lambda take, root: _by_floor(room_ceiling(_bundle(root, take)).trusted_floor_hz), "gated"),
}


@pytest.mark.parametrize("order", [("gated", "ungated"), ("ungated", "gated")])
@pytest.mark.parametrize("reader", READERS)
def test_each_reader_names_the_window_it_reads(tmp_path, reader, order):
    """A take whose gate applied banks each role through both windows; each
    reader reads the one it names in either order, so none reads a role's
    first curve (ADR-0383 §2)."""
    read, window = READERS[reader]
    assert read(_banked(order), tmp_path) == window


@pytest.mark.parametrize("windows,own", [(("gated", "ungated"), "gated"), (("ungated",), "ungated")])
def test_a_take_is_read_through_the_window_its_analysis_graded(windows, own):
    """A take read ungated, a seat or near-field one, banks that window alone."""
    take = _banked(windows)
    assert take_window(take) == own
    assert take_curve(take, "woofer")["window"] == own
    assert take_curve(take, "woofer", "gated") == (_curve("woofer", "gated") if "gated" in windows else None)
