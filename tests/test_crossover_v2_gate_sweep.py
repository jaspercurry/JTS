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

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import lfilter

from jasper.active_speaker.crossover_v2 import gate_sweep
from jasper.active_speaker.crossover_v2.feature_classifier import (
    add_delayed_copy,
    biquad_peaking,
)
from jasper.active_speaker.crossover_v2.gate_sweep import (
    GateSweepRefused,
    sweep_round,
)
from jasper.audio_measurement.sweep import synchronized_swept_sine, write_sweep_wav

RATE = 48_000
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
AZIMUTHS_DEG = (-22.0, -7.0, 0.0, 7.0, 22.0)
LOW_BAND = 0  # SPEC_BANDS[0] == 250-2000 Hz
#: A named bin that is resolution-valid at 20 ms and not at 5 ms.
ANCHOR_UNRESOLVED_HZ = 300.0


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


def _write_round(
    root: Path,
    irs: list[np.ndarray],
    *,
    declared_sha: str | None = None,
    vertical_deg: float = 0.0,
    distance_m: float | None = 1.0,
) -> Path:
    """A banked-round-shaped directory whose captures are known convolutions.

    Two programs are written and the sidecars declare the CLOUD one's hash
    while their ``provenance.stimulus.phase`` says ``verify`` — the live
    mislabel #3504 documents. A consumer that trusted the label would
    deconvolve against the wrong sweep.
    """
    bundle = root / "bundle" / "b0"
    programs = bundle / "crossover_v2" / "wired-test"
    summed = bundle / "summed"
    programs.mkdir(parents=True)
    summed.mkdir(parents=True)

    played, _ = synchronized_swept_sine(duration_approx_s=1.0, sample_rate=RATE)
    other, _ = synchronized_swept_sine(f1=30.0, duration_approx_s=1.0, sample_rate=RATE)
    write_sweep_wav(programs / "cloud_verify_program.wav", played, RATE)
    write_sweep_wav(programs / "verify_program.wav", other, RATE)
    played_sha = hashlib.sha256(
        (programs / "cloud_verify_program.wav").read_bytes()
    ).hexdigest()

    for index, ir in enumerate(irs):
        capture = np.convolve(played.astype(np.float64), ir)
        capture = 0.5 * capture / float(np.max(np.abs(capture)))
        stem = f"summed_cloud_verify_{index:02d}"
        write_sweep_wav(summed / f"{stem}.wav", capture.astype(np.float32), RATE)
        (summed / f"{stem}.json").write_text(
            json.dumps(
                {
                    "position_id": f"cloud_verify_{index:02d}",
                    "phase": "cloud_verify",
                    "wav_path": f"summed/{stem}.wav",
                    "position_deg": AZIMUTHS_DEG[index % len(AZIMUTHS_DEG)],
                    "vertical_deg": vertical_deg,
                    "mark_distance_m": distance_m,
                    "curves": [{"role": "summed", "band_hz": [150.0, 20000.0]}],
                    "provenance": {
                        "stimulus": {
                            "phase": "verify",
                            "wav_sha256": declared_sha or played_sha,
                        }
                    },
                }
            )
        )
    return root


def _low_band(report: dict) -> dict:
    band = report["bands"][LOW_BAND]
    assert band["band_hz"] == [250.0, 2000.0]
    return band


@pytest.fixture(scope="module")
def common_mode_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Every pose sees the SAME late arrival."""
    root = _write_round(
        tmp_path_factory.mktemp("common"),
        [_pose_ir(i, late_copy_ms=8.0) for i in range(len(AZIMUTHS_DEG))],
    )
    return sweep_round(root)


@pytest.fixture(scope="module")
def pose_varying_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Each pose sees the late arrival at its own delay."""
    root = _write_round(
        tmp_path_factory.mktemp("varying"),
        [_pose_ir(i, late_copy_ms=8.0 + 0.9 * i) for i in range(len(AZIMUTHS_DEG))],
    )
    return sweep_round(root)


@pytest.fixture(scope="module")
def direct_only_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """No late arrival at all: whatever the long rung adds is the window."""
    root = _write_round(
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
    root = _write_round(
        tmp_path_factory.mktemp("anchored"),
        [_pose_ir(i, late_copy_ms=8.0 + 0.9 * i) for i in range(3)],
    )
    rungs = (5.0, 20.0)
    return (
        sweep_round(root, rungs_ms=rungs),
        sweep_round(root, rungs_ms=rungs, at_hz=(FEATURE_HZ, ANCHOR_UNRESOLVED_HZ)),
    )


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
    assert abs(corrected) < 0.75 * abs(raw)
    # The bare-impulse host cannot saturate against the capture's own
    # feature, so it recovers the bias more completely — the gap between the
    # two is the disclosure this fixture exists to keep honest.
    assert abs(raw - synthetic_bias) < 0.4
    fit = sensitivity["null_model"]
    assert fit["centre_hz"] == pytest.approx(FEATURE_HZ, rel=0.06)
    assert fit["depth_db"] < 0.0


def test_program_is_bound_by_content_hash_not_by_the_phase_label(
    common_mode_report: dict,
) -> None:
    """#3504: every sidecar's declared stimulus PHASE points at the wrong WAV."""
    assert {pose["program_wav"] for pose in common_mode_report["poses"]} == {
        "cloud_verify_program.wav"
    }


def test_a_hash_no_program_matches_is_refused_by_name(tmp_path: Path) -> None:
    """An unmatched capture refuses; it is never bound to a plausible program."""
    root = _write_round(
        tmp_path,
        [_pose_ir(i, late_copy_ms=None) for i in range(2)],
        declared_sha="0" * 64,
    )
    with pytest.raises(GateSweepRefused) as excinfo:
        sweep_round(root)
    assert excinfo.value.reason == gate_sweep.REFUSE_PROGRAM_UNMATCHED
    assert excinfo.value.detail["declared_stimulus_sha256"] == "0" * 64
    assert "cloud_verify_program.wav" in excinfo.value.detail["programs_present"]


def test_a_round_with_no_captures_refuses_by_name(tmp_path: Path) -> None:
    with pytest.raises(GateSweepRefused) as excinfo:
        sweep_round(tmp_path)
    assert excinfo.value.reason == gate_sweep.REFUSE_NO_CAPTURES


def test_resolution_masks_gate_the_sensitivity_not_the_table(tmp_path: Path) -> None:
    """Too few resolution-valid rungs nulls the sensitivity, by name.

    The table itself still publishes every value with its cycles count: a
    read that cannot be trusted is flagged, never silently dropped.
    """
    root = _write_round(tmp_path, [_pose_ir(i, late_copy_ms=None) for i in range(3)])
    report = sweep_round(root, rungs_ms=(1.0, 2.0))
    band = _low_band(report)

    assert band["sensitivity"] is None
    assert band["sensitivity_null_reason"] == gate_sweep.NULL_INSUFFICIENT_VALID_RUNGS
    assert band["n_valid_rungs"] < 2
    assert set(band["resolution_by_rung"].values()) == {"invalid"}
    assert len(band["poses"]) == 3
    for pose in band["poses"]:
        assert set(pose["value_db_by_rung"]) == {"1", "2"}


def test_poses_are_keyed_on_the_full_declared_pose(tmp_path: Path) -> None:
    """#3503: same azimuth, different height, is a DIFFERENT pose."""
    ground = _write_round(
        tmp_path / "ground",
        [_pose_ir(i, late_copy_ms=None) for i in range(2)],
        vertical_deg=0.0,
    )
    raised = _write_round(
        tmp_path / "raised",
        [_pose_ir(i, late_copy_ms=None) for i in range(2)],
        vertical_deg=12.0,
        distance_m=None,
    )
    ground_keys = [pose["pose_key"] for pose in sweep_round(ground)["poses"]]
    raised_keys = [pose["pose_key"] for pose in sweep_round(raised)["poses"]]

    assert len(set(ground_keys)) == 2
    assert not set(ground_keys) & set(raised_keys)
    assert all(key.endswith("_dna") for key in raised_keys)


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
