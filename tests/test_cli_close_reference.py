# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-round-views close-reference``: the door, its refusals and its
exit codes.

The physics is pinned in ``test_crossover_v2_close_reference.py``. What is
pinned here is the door: a capture bound to its program by CONTENT (#3504
watched a phase label point five captures at the wrong sweep), a refusal that
names the missing input instead of raising, and a report that carries its own
frame.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.close_reference import (
    REFUSE_AT_HZ_OFF_SPEC_TABLE,
    REFUSE_GATE_NOT_POSITIVE,
    VERDICT_AGREEMENT,
    VERDICT_ROOM_DOMINATED,
    VERDICT_UNRESOLVED,
)
from jasper.active_speaker.crossover_v2.round_captures import (
    REFUSE_CLOSE_REFERENCE_NO_CAPTURE,
    REFUSE_PROGRAM_UNMATCHED,
)
from jasper.audio_measurement import gating
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.audio_measurement.sweep import synchronized_swept_sine
from jasper.cli.round_views import (
    ARTIFACT_BY_VIEW,
    EXIT_WRITE_FAILED,
    REASON_UNWRITABLE,
    build_parser,
    main,
)
from tests.crossover_v2_fixtures import CAPTURE_RATE as SAMPLE_RATE, bank_capture_round

EXIT_OK = 0
EXIT_REFUSED = 1


def _ir(distance_m: float) -> np.ndarray:
    """Direct arrival plus one floor bounce, at a mic distance."""
    ir = np.zeros(4096)
    ir[int(round(distance_m / 343.0 * SAMPLE_RATE)) + 64] = 1.0 / distance_m
    image = float(np.hypot(distance_m, 1.68))
    ir[int(round(image / 343.0 * SAMPLE_RATE)) + 64] = 1.0 / image
    return ir


def _round(root: Path, distance_m: float, *, take_id: str = "verify_01_a01") -> Path:
    """A banked round holding one on-axis summed capture and its program.

    A narrow, short program: the comparison this suite drives is graded over
    the band the sidecar declares, and 0.4 s keeps the door's tests quick. It
    is written near full scale so the deconvolution's own quantization floor
    sits well under the residuals the report states.
    """
    sweep, _meta = synchronized_swept_sine(
        f1=100.0, f2=8000.0, duration_approx_s=0.4, sample_rate=SAMPLE_RATE
    )
    program = np.asarray(sweep, dtype=np.float64)
    return bank_capture_round(
        root,
        [_ir(distance_m)],
        program=0.9 * program / float(np.max(np.abs(program))),
        phase="verify",
        capture_ids=[take_id],
        positions_deg=[0.0],
        radiated_band_hz=(100.0, 8000.0),
    )


@pytest.fixture
def rounds(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _round(tmp_path / "far", 1.0),
        _round(tmp_path / "close", 0.30, take_id="verify_02_a01"),
    )


def _compare_argv(
    rounds: tuple[Path, Path], out: Path, *, geometry: Path | None = None
) -> list[str]:
    """Every invocation names a geometry path, so no test reads /var/lib."""
    far, close = rounds
    return [
        "close-reference", "--far-round", str(far), "--close-round", str(close),
        "--close-m", "0.30", "--far-m", "1.0", "--fc-hz", "6000",
        "--driver-diameter-in", "5.5", "--out", str(out),
        "--geometry", str(geometry or out.parent / "undeclared.json"),
    ]


def test_compare_publishes_its_frame_and_its_binding(rounds, tmp_path, capsys):
    out = tmp_path / "report.json"
    assert main(_compare_argv(rounds, out)) == EXIT_OK
    report = json.loads(out.read_text())["close_reference"]
    assert report["schema_version"] == 1
    assert report["generated_by"] == "jasper-round-views close-reference"
    assert set(report["frame"]) >= {
        "window_kind", "taper_fraction", "gate_lead_ms", "smooth_fraction",
        "detrend_fraction", "grid_hz", "n_fft", "alignment_band_hz",
        "gcc_upsample",
    }
    assert report["captures"]["far"]["program"] == "verify_program.wav"
    # The declared distance wins and the sidecar's pinned 1.0 m is disclosed.
    assert report["geometry"]["declared_distance_source"] == "caller"
    assert report["geometry"]["sidecar_mark_distance_m"]["close"] == 1.0
    assert report["geometry"]["sidecar_disagrees"] is True


def test_with_no_out_the_report_lands_beside_the_far_round(rounds, tmp_path):
    """The far read is what the comparison explains, so its artifact travels
    with it -- and ``inventory`` reads presence at that path."""
    far, close = rounds
    assert main([
        "close-reference", "--far-round", str(far), "--close-round", str(close),
        "--close-m", "0.30", "--geometry", str(tmp_path / "undeclared.json"),
    ]) == EXIT_OK
    assert (far / ARTIFACT_BY_VIEW["close-reference"].artifact).is_file()


def test_an_ungraded_band_publishes_null_not_a_non_json_constant(
    rounds, tmp_path, capsys
):
    """An ungraded band publishes ``null``, never a non-JSON constant --
    ``parse_constant`` fires on exactly the tokens a strict reader rejects."""
    out = tmp_path / "report.json"
    assert main(_compare_argv(rounds, out)) == EXIT_OK
    capsys.readouterr()

    def _reject(token: str) -> None:
        raise AssertionError(f"the report carries a non-JSON constant: {token}")

    report = json.loads(out.read_text(), parse_constant=_reject)
    ungraded = [
        row
        for window in report["close_reference"]["windows"]
        for row in window["bands"]
        if row["graded_band_hz"] is None
    ]
    assert ungraded
    for row in ungraded:
        assert row["verdict"] == VERDICT_UNRESOLVED
        for field in (
            "rms_delta_db",
            "worst_far_bin_hz",
            "worst_far_deviation_db",
            "delta_at_worst_db",
            "residual_rel_direct_db",
            "residual_rel_far_db",
        ):
            assert row[field] is None, field


@pytest.mark.parametrize(
    "argv",
    [
        # naming both units is a usage error, not a silent precedence rule
        ["--distance", "--fc-hz", "2500",
         "--driver-diameter-in", "5.5", "--driver-diameter-mm", "140"],
        # each mode refuses without its own inputs, the other mode's not being
        # a substitute
        ["--distance", "--fc-hz", "2500"],
        ["--distance", "--driver-diameter-in", "5.5"],
        # --distance compares nothing: a comparison's inputs beside it would
        # exit 0 having neither compared nor filed
        ["--distance", "--driver-diameter-in", "5.5", "--fc-hz", "2500",
         "--far-round", "a", "--close-round", "b", "--close-m", "0.3"],
        ["--far-round", "a", "--close-round", "b"],
        ["--close-m", "0.3", "--fc-hz", "2500"],
    ],
)
def test_each_mode_refuses_without_its_own_inputs(argv):
    with pytest.raises(SystemExit) as excinfo:
        main(["close-reference", *argv])
    assert excinfo.value.code == 2


def test_a_comparison_still_runs_without_any_declared_diameter():
    """The control on the usage errors above: only ``--distance`` needs one."""
    args = build_parser().parse_args(
        ["close-reference", "--far-round", "a", "--close-round", "b",
         "--close-m", "0.3"]
    )
    assert args.driver_diameter_in is None and args.driver_diameter_mm is None


@pytest.mark.parametrize(
    "corrupt, reason",
    [("sha", REFUSE_PROGRAM_UNMATCHED), ("pose", REFUSE_CLOSE_REFERENCE_NO_CAPTURE)],
)
def test_an_unbindable_capture_is_a_refusal_not_a_traceback(
    rounds, tmp_path, capsys, corrupt, reason
):
    far, _close = rounds
    sidecar = next(far.glob("**/summed/summed_*.json"))
    doc = json.loads(sidecar.read_text())
    if corrupt == "sha":
        doc["provenance"]["stimulus"]["wav_sha256"] = "0" * 64
    else:
        doc["position_deg"] = 22
    sidecar.write_text(json.dumps(doc))
    assert main(_compare_argv(rounds, tmp_path / "report.json")) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused"
    assert payload["reason"] == reason


@pytest.mark.parametrize(
    "extra, reason",
    [
        # a non-positive gate, before an ``+inf`` trusted floor can reach the
        # strict JSON writer
        (["--far-gate-ms", "0"], REFUSE_GATE_NOT_POSITIVE),
        # a bin no spec band covers has no tolerance to be graded against
        (["--at-hz", "20"], REFUSE_AT_HZ_OFF_SPEC_TABLE),
    ],
)
def test_an_ungradeable_input_refuses_by_name_before_any_computation(
    rounds, tmp_path, capsys, extra, reason
):
    argv = _compare_argv(rounds, tmp_path / "report.json") + extra
    assert main(argv) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused"
    assert payload["reason"] == reason


def test_a_declared_geometry_sets_each_windows_gate(rounds, tmp_path, capsys):
    """The close capture's own clean window is longer, and says where it came
    from: the first bounce's excess path grows as the mic nears the woofer."""
    geometry = tmp_path / "geometry.json"
    DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0
    ).save(geometry)
    out = tmp_path / "report.json"
    assert main(_compare_argv(rounds, out, geometry=geometry)) == EXIT_OK

    report = json.loads(out.read_text())["close_reference"]
    windows = {window["name"]: window for window in report["windows"]}
    assert {window["gate_source"] for window in windows.values()} == {
        gating.ENTANGLEMENT_SOURCE_DECLARED
    }
    for window in windows.values():
        assert window["gate_ms"] == pytest.approx(window["declared_clean_window_ms"])
    assert windows["close_window"]["gate_ms"] > windows["far_window"]["gate_ms"]
    assert report["geometry"]["declared_geometry"]["mic_height_m"] == 0.84


def test_an_unwritable_out_is_named_not_a_traceback(rounds, tmp_path, capsys):
    """Compared, and only the filing failed: its own exit code, not an
    ``OSError`` escaping the writer."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    rc = main(_compare_argv(rounds, blocker / "report.json"))

    assert rc == EXIT_WRITE_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unwritable"
    assert payload["reason"] == REASON_UNWRITABLE


def test_at_hz_grades_one_narrow_band_beside_the_spec_rows(rounds, tmp_path, capsys):
    """``--at-hz`` answers AT a frequency: a +/-1/3-octave row per window in
    the artifact, inside the spec band it is graded against, and the far
    window's verdict as the answer's own scalar."""
    out = tmp_path / "report.json"
    assert main(_compare_argv(rounds, out) + ["--at-hz", "500"]) == EXIT_OK
    answer = json.loads(capsys.readouterr().out)

    report = json.loads(out.read_text())["close_reference"]
    windows = {window["name"]: window for window in report["windows"]}
    for window in windows.values():
        (narrow,) = window["features"]
        assert narrow["requested_hz"] == 500.0
        assert narrow["nominal_band_hz"] == pytest.approx(
            [500.0 * 2 ** (-1 / 3), 500.0 * 2 ** (1 / 3)]
        )
        spec = window["bands"][0]
        assert narrow["tolerance_db"] == spec["tolerance_db"]
        assert narrow["graded_band_hz"][1] - narrow["graded_band_hz"][0] < (
            spec["graded_band_hz"][1] - spec["graded_band_hz"][0]
        )
        assert narrow["verdict"] in {
            VERDICT_AGREEMENT, VERDICT_ROOM_DOMINATED, VERDICT_UNRESOLVED
        }
    assert answer["at_hz_verdict"] == windows["far_window"]["features"][0]["verdict"]


def test_the_distance_flag_prints_both_terms(capsys):
    assert main(
        ["close-reference", "--distance", "--driver-diameter-in", "5.5",
         "--fc-hz", "2500"]
    ) == EXIT_OK
    # It writes no artifact, so the derivation's own fields ARE the answer.
    record = json.loads(capsys.readouterr().out)
    assert record["distance_in"] == pytest.approx(12.4, abs=0.1)
    assert record["margin_term_m"] > record["far_field_term_m"]
    assert record["placement_tolerance_db"] > 0.0
    assert "status" not in record

