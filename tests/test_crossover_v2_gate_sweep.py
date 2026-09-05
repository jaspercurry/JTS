# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The gate sweep's discriminator, on synthetic rounds with known answers.

Every fixture here is built, not banked: a program sweep, a known impulse
response per pose, and the convolution of the two written as a capture. That
makes the answer knowable in advance — a common-mode late arrival CANNOT
produce across-pose divergence, a pose-varying one must, and an injected
notch's window bias is exactly what the null model has to subtract.

No hardware, no banked captures, no network.
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import lfilter

from jasper.active_speaker.crossover_v2 import feature_classification, gate_sweep
from jasper.active_speaker.crossover_v2.feature_classifier import (
    add_delayed_copy,
    biquad_peaking,
)
from jasper.active_speaker.crossover_v2.feature_optics import CENTRE_SEARCH_OCT
from jasper.active_speaker.crossover_v2.gate_sweep import (
    moved_routes,
    sweep_features,
    sweep_round,
)
from jasper.active_speaker.crossover_v2.round_captures import (
    PoseCapture,
    RoundCapturesRefused,
    discover_captures,
)
from tests.crossover_v2_fixtures import (
    CAPTURE_AZIMUTHS_DEG as AZIMUTHS_DEG,
    CAPTURE_RATE as RATE,
    bank_capture_round,
)

PEAK_IDX = 480
IR_LEN = 9600
#: The band-0 feature every fixture carries, so the band's worst bin is a
#: known frequency rather than whichever comb null happened to be deepest.
#: Its depth is the r9 dip's own (-4.5 dB, P1 §5d): a much deeper one stops
#: being additive with the null model's injected twin, which is a real limit
#: of the method and not a regime the product measures in.
FEATURE_HZ = 800.0
FEATURE_DEPTH_DB = -4.5
FEATURE_Q = 8.0
LATE_COPY_GAIN = 0.20
LOW_BAND = 0  # SPEC_BANDS[0] == 250-2000 Hz
#: A named bin that is resolution-valid at 20 ms and not at 5 ms.
ANCHOR_UNRESOLVED_HZ = 300.0

#: A round varying BOTH axes: (azimuth, elevation, late arrival). The three
#: poses at ear height share one late arrival and the two above and below it
#: each see their own, so the feature moves with HEIGHT alone -- the case an
#: azimuth-only cloud cannot attribute (#3503). The (0, 0) anchor is in both
#: families, which is why each reads three poses.
MIXED_AXIS_POSES = (
    (0.0, 0.0, 8.0),
    (20.0, 0.0, 8.0),
    (-20.0, 0.0, 8.0),
    (0.0, 10.0, 8.9),
    (0.0, -10.0, 9.8),
)


def _pose_ir(index: int, *, late_copy_ms: float | None) -> np.ndarray:
    """One pose's impulse response: direct, a baffle echo, maybe a room one.

    The 0.3 ms copy differs slightly per pose and sits inside even the 3 ms
    rung, so across-pose sigma is non-zero at EVERY rung — the growth ratio
    has a denominator, and a window-invariant difference is present for the
    discriminator to have to ignore.
    """
    ir = np.zeros(IR_LEN, dtype=np.float64)
    ir[PEAK_IDX] = 1.0
    ir = add_delayed_copy(ir, 0.20 + 0.02 * index, 0.3, RATE)
    if late_copy_ms is not None:
        ir = add_delayed_copy(ir, LATE_COPY_GAIN, late_copy_ms, RATE)
    b, a = biquad_peaking(FEATURE_HZ, FEATURE_DEPTH_DB, FEATURE_Q, RATE)
    return np.asarray(lfilter(b, a, ir), dtype=np.float64)


def _low_band(report: dict) -> dict:
    band = report["bands"][LOW_BAND]
    assert band["band_hz"] == [250.0, 2000.0]
    return band


@pytest.fixture(scope="module")
def common_mode_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Every pose sees the SAME late arrival."""
    root = bank_capture_round(
        tmp_path_factory.mktemp("common"),
        [_pose_ir(i, late_copy_ms=8.0) for i in range(len(AZIMUTHS_DEG))],
    )
    return sweep_round(root)


@pytest.fixture(scope="module")
def pose_varying_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Each pose sees the late arrival at its own delay."""
    root = bank_capture_round(
        tmp_path_factory.mktemp("varying"),
        [_pose_ir(i, late_copy_ms=8.0 + 0.9 * i) for i in range(len(AZIMUTHS_DEG))],
    )
    return sweep_round(root)


@pytest.fixture(scope="module")
def direct_only_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """No late arrival at all: whatever the long rung adds is the window."""
    root = bank_capture_round(
        tmp_path_factory.mktemp("direct"),
        [_pose_ir(i, late_copy_ms=None) for i in range(len(AZIMUTHS_DEG))],
    )
    return sweep_round(root)


@pytest.fixture(scope="module")
def anchored_reports(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, dict]:
    """One round read twice: bands alone, then bands plus caller-named bins.

    ``ANCHOR_UNRESOLVED_HZ`` has 1.5 cycles in the 5 ms rung and 6 in the
    20 ms one, so it leaves exactly one resolution-valid rung and exercises
    the null-reason branch a named bin can land in.
    """
    root = bank_capture_round(
        tmp_path_factory.mktemp("anchored"),
        [_pose_ir(i, late_copy_ms=8.0 + 0.9 * i) for i in range(3)],
    )
    rungs = (5.0, 20.0)
    return (
        sweep_round(root, rungs_ms=rungs),
        sweep_round(root, rungs_ms=rungs, at_hz=(FEATURE_HZ, ANCHOR_UNRESOLVED_HZ)),
    )


@pytest.fixture(scope="module")
def in_memory_round(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, tuple[PoseCapture, ...]]:
    """One round on disk, and the same captures stripped to what is computed on.

    The stripped copies carry no WAV, no program and no phase — a number that
    moved with those would be a number the engine had no business reading.
    """
    root = bank_capture_round(
        tmp_path_factory.mktemp("in_memory"),
        [_pose_ir(i, late_copy_ms=8.0 + 0.9 * i) for i in range(3)],
    )
    stripped = tuple(
        replace(capture, phase=None, wav=None, program=None, program_sha256="")
        for capture in discover_captures(root)
    )
    return root, stripped


@pytest.fixture(scope="module")
def height_varying_feature() -> dict:
    """One bin of a mixed-axis round whose late arrival moves with height."""
    captures = [
        PoseCapture(
            capture_id=f"mixed_{index:02d}",
            phase=None,
            wav=None,
            program=None,
            program_sha256="",
            azimuth_deg=azimuth_deg,
            vertical_deg=vertical_deg,
            mark_distance_m=1.0,
            radiated_band_hz=(150.0, 20000.0),
            sample_rate=RATE,
            ir=_pose_ir(index, late_copy_ms=late_copy_ms),
            peak_idx=PEAK_IDX,
        )
        for index, (azimuth_deg, vertical_deg, late_copy_ms) in enumerate(
            MIXED_AXIS_POSES
        )
    ]
    (feature,) = sweep_features(captures, rungs_ms=(5.0, 20.0), at_hz=(FEATURE_HZ,))
    return feature


def test_common_mode_arrival_leaves_across_pose_sigma_flat(
    common_mode_report: dict,
) -> None:
    """An arrival identical at every pose cannot make the poses disagree."""
    band = _low_band(common_mode_report)
    sensitivity = band["sensitivity"]
    assert sensitivity is not None
    assert band["worst_bin_hz"] == pytest.approx(FEATURE_HZ, rel=0.06)
    assert sensitivity["sigma_growth_ratio"] == pytest.approx(1.0, abs=0.4)


def test_pose_varying_arrival_grows_across_pose_sigma(
    pose_varying_report: dict, common_mode_report: dict
) -> None:
    """The discriminator, in its direction: growth, and growth over the twin.

    This is the mutation pin. Inverting the ratio (short over long) or
    reading sigma at the wrong rung turns this assertion false while the
    common-mode fixture above still passes, so a broken direction cannot
    hide behind a symmetric test.
    """
    varying = _low_band(pose_varying_report)["sensitivity"]
    common = _low_band(common_mode_report)["sensitivity"]
    assert varying is not None and common is not None

    short = varying["shortest_valid_rung_ms"]
    long_ = varying["longest_valid_rung_ms"]
    sigma = _low_band(pose_varying_report)["sigma_db_by_rung"]
    assert long_ > short
    assert sigma[f"{long_:g}"] > sigma[f"{short:g}"]
    assert varying["sigma_growth_ratio"] > 3.0
    assert varying["sigma_growth_ratio"] > 3.0 * common["sigma_growth_ratio"]


def test_null_model_recovers_the_windows_own_bias(direct_only_report: dict) -> None:
    """With no room in the IR, the long-rung delta must correct to ~zero.

    The injected notch still READS deeper as the window lengthens — that is
    the window's bias, large and never vanishing (#3495 amendment 2). What
    the corrected delta must not do is call it room.
    """
    band = _low_band(direct_only_report)
    sensitivity = band["sensitivity"]
    assert sensitivity is not None
    raw = sensitivity["raw_delta_db"]
    corrected = sensitivity["corrected_delta_db"]
    synthetic_bias = sensitivity["bias_delta_synthetic_host_db"]

    assert raw < -0.5, "the window's own bias should deepen the notch"
    # Both hosts have to agree that the WINDOW deepens a notch as it grows.
    # A correction of the wrong sign would deepen the corrected delta
    # instead of shrinking it, and call the window's own doing the room's.
    assert sensitivity["bias_delta_db"] < 0.0
    assert synthetic_bias < 0.0
    # The narrow-Q bracket corrects nothing and must not contradict either:
    # a notch of the same depth and 1.5x the Q is still deepened by a longer
    # window, and a bracket of the opposite sign would be reporting noise.
    assert sensitivity["bias_delta_narrow_q_db"] < 0.0
    assert abs(corrected) < 0.75 * abs(raw)
    # The bare-impulse host cannot saturate against the capture's own
    # feature, so it recovers the bias more completely — the gap between the
    # two is the disclosure this fixture exists to keep honest.
    assert abs(raw - synthetic_bias) < 0.4
    fit = sensitivity["null_model"]
    assert fit["centre_hz"] == pytest.approx(FEATURE_HZ, rel=0.06)
    assert fit["depth_db"] < 0.0


def test_resolution_masks_gate_the_sensitivity_not_the_table(tmp_path: Path) -> None:
    """Too few resolution-valid rungs nulls the sensitivity, by name.

    The table itself still publishes every value with its cycles count: a
    read that cannot be trusted is flagged, never silently dropped.
    """
    root = bank_capture_round(tmp_path, [_pose_ir(i, late_copy_ms=None) for i in range(3)])
    report = sweep_round(root, rungs_ms=(1.0, 2.0))
    band = _low_band(report)

    assert band["sensitivity"] is None
    assert band["sensitivity_null_reason"] == gate_sweep.NULL_INSUFFICIENT_VALID_RUNGS
    assert band["n_valid_rungs"] < 2
    assert set(band["resolution_by_rung"].values()) == {"invalid"}
    assert len(band["poses"]) == 3
    for pose in band["poses"]:
        assert set(pose["value_db_by_rung"]) == {"1", "2"}
        # Only what varies with the BIN. Who the pose is is the round's fact
        # and is published once, in the report's own `poses` block.
        assert set(pose) == {"pose_key", "value_db_by_rung", "detrended_db_by_rung"}


@pytest.mark.parametrize("radiated_lo_hz", [1990.0, 1999.0])
def test_a_graded_band_narrower_than_the_grid_nulls_by_name(
    tmp_path: Path, radiated_lo_hz: float
) -> None:
    """A band the DUT radiates only a few Hz of holds no bin to be worst.

    The graded span is non-empty while the set of analysis bins inside it is,
    so the argmax that picks the worst bin has nothing to pick from. That is
    a named null, never a crash.
    """
    root = bank_capture_round(
        tmp_path,
        [_pose_ir(i, late_copy_ms=8.0) for i in range(3)],
        radiated_band_hz=(radiated_lo_hz, 20000.0),
    )
    band = _low_band(sweep_round(root, rungs_ms=(5.0, 20.0)))

    assert band["graded_band_hz"] == [radiated_lo_hz, 2000.0]
    assert band["sensitivity"] is None
    assert (
        band["sensitivity_null_reason"] == gate_sweep.NULL_BAND_BELOW_GRID_RESOLUTION
    )
    assert "worst_bin_hz" not in band


def test_the_frame_every_number_is_stated_in_is_published(
    common_mode_report: dict,
) -> None:
    """#3495 amendment 3: a number without its frame is the frame's number."""
    frame = common_mode_report["frame"]
    assert frame["window"]["taper_fraction"] == 0.25
    assert frame["window"]["lead_ms"] == 1.0
    assert frame["smoothing"]["magnitude_fraction"] == 12
    assert frame["reference"]["rung_ms"] == 7.0
    assert frame["reference"]["band_hz"] == [2500.0, 8000.0]
    assert frame["reference"]["intersected_with_radiated_band"] is True
    assert frame["rungs_ms"] == list(gate_sweep.DEFAULT_RUNGS_MS)


def test_the_summary_lines_label_the_bin_actually_read(
    anchored_reports: tuple[dict, dict],
) -> None:
    """One line per band then per named bin, each carrying the bin it read: a
    band's own worst bin, and a named request beside the grid bin it snapped
    to. The unresolved anchor exercises the no-sensitivity branch."""
    _plain, anchored = anchored_reports
    lines = gate_sweep.summary_lines(anchored)

    assert len(lines) == len(anchored["bands"]) + len(anchored["features"])
    low = anchored["bands"][0]
    assert lines[0].startswith(
        f"{low['band_hz'][0]:g}-{low['band_hz'][1]:g} Hz "
        f"(worst bin {low['worst_bin_hz']:.1f} Hz) "
        f"{low['window_verdict'].upper()} "
    )
    named, unresolved = anchored["features"]
    assert lines[-2].startswith(
        f"{named['requested_hz']:g} Hz (bin {named['bin_hz']:.1f} Hz) "
    )
    assert unresolved["sensitivity"] is None
    assert "no sensitivity" in lines[-1]


def test_a_named_frequency_is_read_the_way_a_worst_bin_is(
    anchored_reports: tuple[dict, dict],
) -> None:
    """``at_hz`` anchors on the caller's bin, snapped, with its own null model."""
    _plain, anchored = anchored_reports
    named, unresolved = anchored["features"]

    assert named["requested_hz"] == FEATURE_HZ
    assert named["bin_hz"] == pytest.approx(FEATURE_HZ, rel=0.02)
    assert named["band_hz"] == [250.0, 2000.0]
    assert named["sensitivity_null_reason"] is None
    assert named["valid_rungs_ms"] == [5.0, 20.0]
    assert named["sensitivity"]["sigma_growth_ratio"] > 1.0
    assert named["sensitivity"]["null_model"]["centre_hz"] == pytest.approx(
        FEATURE_HZ, rel=0.06
    )
    assert {pose["pose_key"] for pose in named["poses"]} == {
        pose["pose_key"] for pose in anchored["poses"]
    }

    assert unresolved["requested_hz"] == ANCHOR_UNRESOLVED_HZ
    assert unresolved["n_valid_rungs"] == 1
    assert unresolved["sensitivity"] is None
    assert (
        unresolved["sensitivity_null_reason"]
        == gate_sweep.NULL_INSUFFICIENT_VALID_RUNGS
    )


def test_the_null_model_shows_its_fit_at_every_pose(
    anchored_reports: tuple[dict, dict],
) -> None:
    """``per_pose_*`` discloses one fit per pose, each inside the search span."""
    _plain, anchored = anchored_reports
    named = anchored["features"][0]
    null_model = named["sensitivity"]["null_model"]
    n_poses = len(named["poses"])
    lo = named["bin_hz"] * 2.0**-CENTRE_SEARCH_OCT
    hi = named["bin_hz"] * 2.0**CENTRE_SEARCH_OCT

    assert n_poses > 1
    for field in ("per_pose_centre_hz", "per_pose_depth_db", "per_pose_q"):
        assert len(null_model[field]) == n_poses
    assert all(lo <= centre <= hi for centre in null_model["per_pose_centre_hz"])


def test_naming_a_frequency_does_not_move_the_bands(
    anchored_reports: tuple[dict, dict],
) -> None:
    """The per-band worst-bin report is what it was; ``features`` is additive."""
    plain, anchored = anchored_reports
    assert plain["features"] == []
    assert anchored["bands"] == plain["bands"]


@pytest.mark.parametrize(
    "hz", [gate_sweep.GRID_LO_HZ - 1.0, gate_sweep.GRID_HI_HZ + 1.0]
)
def test_a_frequency_off_the_analysis_grid_is_an_input_error(
    tmp_path: Path, hz: float
) -> None:
    with pytest.raises(ValueError):
        sweep_round(tmp_path, at_hz=(hz,))


def test_the_whole_sigma_surface_is_banked(anchored_reports: tuple[dict, dict]) -> None:
    """P1 §5b's artifact: any bin, any rung pair, without re-running the sweep."""
    _plain, anchored = anchored_reports
    sigma_map = anchored["sigma_map"]
    points = gate_sweep.analysis_grid().size

    assert len(sigma_map["grid_hz"]) == points
    assert set(sigma_map["sigma_db_by_rung"]) == {"5", "20"}
    assert all(
        len(values) == points for values in sigma_map["sigma_db_by_rung"].values()
    )


def test_the_in_memory_door_reads_what_the_round_door_reads(
    in_memory_round: tuple[Path, tuple[PoseCapture, ...]],
) -> None:
    """One engine, two doors: the same bins, read to the same numbers."""
    root, captures = in_memory_round
    rungs = (5.0, 20.0)
    at_hz = (FEATURE_HZ, ANCHOR_UNRESOLVED_HZ)

    assert sweep_features(captures, rungs_ms=rungs, at_hz=at_hz) == sweep_round(
        root, rungs_ms=rungs, at_hz=at_hz
    )["features"]


@pytest.mark.parametrize("n_poses", [0, 1])
def test_the_in_memory_door_refuses_fewer_than_two_poses(
    in_memory_round: tuple[Path, tuple[PoseCapture, ...]], n_poses: int
) -> None:
    """Across-pose sigma needs two poses, whichever door asked for it."""
    _root, captures = in_memory_round
    with pytest.raises(RoundCapturesRefused) as refusal:
        sweep_features(captures[:n_poses], at_hz=(FEATURE_HZ,))
    assert refusal.value.reason == gate_sweep.REFUSE_SINGLE_POSE


@pytest.mark.parametrize(
    "hz", [gate_sweep.GRID_LO_HZ - 1.0, gate_sweep.GRID_HI_HZ + 1.0]
)
def test_the_in_memory_door_rejects_a_frequency_off_the_grid(
    in_memory_round: tuple[Path, tuple[PoseCapture, ...]], hz: float
) -> None:
    _root, captures = in_memory_round
    with pytest.raises(ValueError):
        sweep_features(captures, at_hz=(hz,))


# --------------------------------------------------------------------------- #
# the room/speaker rule the engine now owns
# --------------------------------------------------------------------------- #


#: A ladder whose poses genuinely disagree, so the sigma route is readable at
#: all. Comfortably over the floor below which a ratio is capture noise.
_READABLE = {"sigma_growth_readable": True}
#: The two ends of a route that is NOT the one under test, held flat.
_FLAT = {"sigma_growth_ratio": 1.0, "corrected_delta_db": 0.0, "centre_shift_oct": 0.0}


@pytest.mark.parametrize(
    ("moved", "expected"),
    # Each case moves exactly ONE route and holds the other two flat, and every
    # number is a literal bracketing a shipped bar from one side: a case written
    # as a multiple of the constant it tests moves with the constant and pins
    # nothing. 4.0x / 1.2x bracket 2.0x; 3.0 / 0.1 dB bracket 0.5 dB; 1/12 and
    # 1/48 octave bracket 1/24.
    [
        pytest.param({"sigma_growth_ratio": 4.0}, [gate_sweep.ROUTE_SIGMA_GROWTH], id="sigma-grew"),
        pytest.param({"sigma_growth_ratio": 1.2}, [], id="sigma-flat"),
        pytest.param({"corrected_delta_db": 3.0}, [gate_sweep.ROUTE_DEPTH_DELTA], id="deeper"),
        pytest.param({"corrected_delta_db": -3.0}, [gate_sweep.ROUTE_DEPTH_DELTA], id="shallower"),
        pytest.param({"corrected_delta_db": 0.1}, [], id="depth-flat"),
        pytest.param({"centre_shift_oct": 1.0 / 12.0}, [gate_sweep.ROUTE_CENTRE_SHIFT], id="walked-up"),
        pytest.param({"centre_shift_oct": -1.0 / 12.0}, [gate_sweep.ROUTE_CENTRE_SHIFT], id="walked-down"),
        pytest.param({"centre_shift_oct": 1.0 / 48.0}, [], id="centre-flat"),
    ],
)
def test_each_route_alone_says_the_window_moved_the_feature(
    moved: dict[str, float], expected: list[str]
) -> None:
    """Three independent readings, any one alone. Signed either way.

    A feature the window makes deeper and one it makes shallower are both the
    window's, and so is one whose centre walks up or down. The third route is
    here because it is the only one that catches a feature the window re-makes
    WITHOUT deepening it.
    """
    assert moved_routes(**{**_FLAT, **_READABLE, **moved}) == expected


def test_a_sigma_growth_over_capture_noise_is_not_read_as_the_room() -> None:
    """A ratio of two noise figures is a coin toss with a room's name on it.

    Repeat takes at ONE pose produce across-pose sigma of ~1e-4 dB whose ratio
    across the ladder reads over the room bar on a known minimum-phase
    resonance (measured, on the classifier's own fixtures). Below the floor the
    ratio is not read at all, and the other two routes still decide.
    """
    growing = {**_FLAT, "sigma_growth_ratio": 4.0}
    assert moved_routes(**growing, sigma_growth_readable=True) == [
        gate_sweep.ROUTE_SIGMA_GROWTH
    ]
    assert moved_routes(**growing, sigma_growth_readable=False) == []


def test_the_ladder_verdict_rides_the_report_beside_its_sensitivity(
    pose_varying_report: dict, direct_only_report: dict
) -> None:
    """One word per feature, on every row, whether or not it could be priced.

    A caller must be able to read a verdict for every bin — including the ones
    the ladder refused to price, where there is no ``sensitivity`` block to
    carry it and the null reason is the reason.
    """
    varying = _low_band(pose_varying_report)
    assert varying["window_verdict"] == gate_sweep.WINDOW_MOVED
    assert gate_sweep.ROUTE_SIGMA_GROWTH in varying["window_verdict_reasons"]
    assert varying["sensitivity"]["sigma_growth_readable"] is True

    # The top band of the round with no late arrival at all: nothing in the IR
    # for a longer window to admit, so no route has anything to fire on.
    direct = direct_only_report["bands"][-1]
    assert direct["window_verdict"] == gate_sweep.WINDOW_STABLE
    assert direct["window_verdict_reasons"] == []

    unpriced = sweep_round(
        Path(direct_only_report["round_dir"]), rungs_ms=(1.0, 2.0)
    )["bands"][LOW_BAND]
    assert unpriced["sensitivity"] is None
    assert unpriced["window_verdict"] == gate_sweep.WINDOW_UNRESOLVED
    assert unpriced["window_verdict_reasons"] == [
        gate_sweep.NULL_INSUFFICIENT_VALID_RUNGS
    ]


#: A driver notch and a reflection whose comb null lands beside it. The 5 ms
#: copy is outside the 4 ms rung and inside the 20 ms one, and its nulls sit at
#: odd multiples of 100 Hz -- so 1100 Hz, an eighth of an octave above the
#: notch and well inside the 1/6-octave centre search, is a null the LONG
#: window admits and the short one does not.
_WALK_NOTCH_HZ = 1000.0
_WALK_NOTCH_DB = -6.0
_WALK_NOTCH_Q = 12.0
_WALK_COPY_MS = 5.0
_WALK_COPY_GAIN = 0.4


def test_a_feature_whose_centre_walks_between_the_rungs_reads_moved() -> None:
    """The route the two depth readings are blind to, on a known answer.

    A window that re-makes a feature at a DIFFERENT frequency has moved it,
    and the fit has to be run at both ends of the ladder to see that. One fit
    reused for both rungs reads a shift of exactly zero and this route can
    never fire.
    """
    captures = []
    for index in range(3):
        ir = np.zeros(IR_LEN, dtype=np.float64)
        ir[PEAK_IDX] = 1.0
        ir = add_delayed_copy(ir, 0.20 + 0.02 * index, 0.3, RATE)
        b, a = biquad_peaking(_WALK_NOTCH_HZ, _WALK_NOTCH_DB, _WALK_NOTCH_Q, RATE)
        ir = np.asarray(lfilter(b, a, ir), dtype=np.float64)
        ir = add_delayed_copy(ir, _WALK_COPY_GAIN, _WALK_COPY_MS, RATE)
        captures.append(
            PoseCapture(
                capture_id=f"walk_{index:02d}",
                phase=None,
                wav=None,
                program=None,
                program_sha256="",
                azimuth_deg=AZIMUTHS_DEG[index],
                vertical_deg=0.0,
                mark_distance_m=1.0,
                radiated_band_hz=(150.0, 20000.0),
                sample_rate=RATE,
                ir=ir,
                peak_idx=PEAK_IDX,
            )
        )
    (feature,) = sweep_features(
        captures, rungs_ms=(4.0, 20.0), at_hz=(_WALK_NOTCH_HZ,)
    )
    sensitivity = feature["sensitivity"]

    assert feature["window_verdict"] == gate_sweep.WINDOW_MOVED
    assert gate_sweep.ROUTE_CENTRE_SHIFT in feature["window_verdict_reasons"]
    # The short rung reads the driver's own notch; the long one reads the comb
    # null the reflection puts an eighth of an octave above it.
    centres = sensitivity["centre_hz_by_rung"]
    assert centres["4"] == pytest.approx(_WALK_NOTCH_HZ, rel=0.02)
    assert centres["20"] > _WALK_NOTCH_HZ * 2 ** (1.0 / 12.0)
    assert sensitivity["centre_shift_oct"] == pytest.approx(
        math.log2(centres["20"] / centres["4"])
    )


# --------------------------------------------------------------------------- #
# which AXIS the feature moves with (#3503)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("axis", "lo_ratio", "hi_ratio", "readable"),
    [
        pytest.param(gate_sweep.AXIS_ELEVATION, 3.0, math.inf, True, id="height"),
        pytest.param(gate_sweep.AXIS_AZIMUTH, 0.0, 1.5, False, id="azimuth"),
    ],
)
def test_only_the_axis_a_feature_moves_with_grows_its_own_sigma(
    height_varying_feature: dict,
    axis: str,
    lo_ratio: float,
    hi_ratio: float,
    readable: bool,
) -> None:
    """The split #3503 asks for: one round, one bin, two answers.

    Pooled over every pose these five captures disagree and the blended sigma
    grows, which says the window admits something and not which axis it is.
    Split, the poses that differ only in azimuth are window-invariant and the
    ones that differ in height are not -- and the family that did not
    disagree says so, rather than reporting its own noise as a ratio.
    """
    family = height_varying_feature["sigma_by_axis"][axis]
    sigma = family["sigma_db_by_rung"]

    assert family["n_poses"] == 3
    assert lo_ratio < family["sigma_growth_ratio"] < hi_ratio
    assert family["sigma_growth_readable"] is readable
    assert set(sigma) == {"5", "20"}


def test_an_azimuth_only_round_reads_as_one_azimuth_family(
    pose_varying_report: dict,
) -> None:
    """The legacy round shape: the second axis was never sampled, and says so.

    Every pose declares elevation 0, so the azimuth family IS the pose cloud
    and its sigma is the all-pose sigma exactly -- the split adds a block and
    moves no number an azimuth-only round already published. The elevation
    axis is present carrying ``sampled: false``: rung 4's experiment is owed,
    which is not the same fact as a feature that does not move with height.
    """
    band = _low_band(pose_varying_report)
    by_axis = band["sigma_by_axis"]

    assert set(by_axis) == {gate_sweep.AXIS_AZIMUTH, gate_sweep.AXIS_ELEVATION}
    azimuth = by_axis[gate_sweep.AXIS_AZIMUTH]
    assert azimuth["sampled"] is True
    assert azimuth["n_poses"] == len(AZIMUTHS_DEG)
    assert azimuth["sigma_db_by_rung"] == band["sigma_db_by_rung"]

    elevation = by_axis[gate_sweep.AXIS_ELEVATION]
    assert elevation["sampled"] is False
    assert elevation["reason"] == gate_sweep.AXIS_NOT_VARIED
    assert "sigma_db_by_rung" not in elevation


def test_a_round_that_declares_no_height_names_both_axes_unsampled(
    in_memory_round: tuple[Path, tuple[PoseCapture, ...]],
) -> None:
    """An undeclared pose field is not a zero: neither axis has a member, and
    each publishes that reason rather than vanishing (#3503)."""
    _root, captures = in_memory_round
    unposed = tuple(
        replace(capture, vertical_deg=None, mark_distance_m=None)
        for capture in captures
    )

    (feature,) = sweep_features(unposed, rungs_ms=(5.0, 20.0), at_hz=(FEATURE_HZ,))
    by_axis = feature["sigma_by_axis"]
    assert set(by_axis) == {gate_sweep.AXIS_AZIMUTH, gate_sweep.AXIS_ELEVATION}
    for family in by_axis.values():
        assert family["sampled"] is False
        assert family["reason"] == gate_sweep.AXIS_NOT_DECLARED


def test_every_published_sigma_declares_the_kind_of_spread_it_is(
    pose_varying_report: dict,
) -> None:
    """The σ an attribution argument is built on carries the register the
    evidence packet already declares: an across-pose spread pools the field's
    real variation with each capture's noise and separates neither, and the
    growth RATIO is a discriminator rather than an error bar."""
    register = pose_varying_report["frame"]["uncertainty"]

    assert register["fields"] == {}
    assert set(register["unseparated"]) == {
        "sigma_db_by_rung", "band_mean_sigma_db_by_rung"
    }
    assert {
        entry["kind"] for entry in register["unseparated"].values()
    } == {feature_classification.UNCERTAINTY_UNSEPARATED}
    assert set(register["not_uncertainties"]) == {"sigma_growth_ratio"}
