# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-gate-sweep``'s door: exit codes, where it writes, what it refuses.

The engine's own numbers are pinned in tests/test_crossover_v2_gate_sweep.py.
What is pinned here is only what an operator or a script sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker.crossover_v2 import round_captures
from jasper.cli import gate_sweep as cli
from jasper.cli._refusal import EXIT_WRITE_FAILED
from tests.crossover_v2_fixtures import bank_capture_round
from tests.test_crossover_v2_gate_sweep import _pose_ir


@pytest.fixture
def round_dir(tmp_path: Path) -> Path:
    return bank_capture_round(
        tmp_path, [_pose_ir(i, late_copy_ms=8.0) for i in range(3)]
    )


def test_a_swept_round_writes_its_report_beside_the_round(
    round_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([str(round_dir), "--rungs-ms", "5", "20"]) == cli.EXIT_OK

    out = round_dir / cli.DEFAULT_OUT_NAME
    report = json.loads(out.read_text())
    assert report["frame"]["rungs_ms"] == [5.0, 20.0]
    assert len(report["poses"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "swept"


def test_out_puts_the_report_where_it_is_told(round_dir: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "scratch" / "sweep.json"
    assert (
        cli.main([str(round_dir), "--rungs-ms", "5", "20", "--out", str(elsewhere)])
        == cli.EXIT_OK
    )
    assert elsewhere.is_file()
    assert not (round_dir / cli.DEFAULT_OUT_NAME).exists()


def test_a_refusal_is_an_output_naming_the_missing_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([str(tmp_path)]) == cli.EXIT_REFUSED

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused"
    assert payload["reason"] == round_captures.REFUSE_NO_CAPTURES


def test_a_ladder_of_one_rung_is_an_input_error(
    round_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([str(round_dir), "--rungs-ms", "7"]) == cli.EXIT_UNREADABLE
    assert not (round_dir / cli.DEFAULT_OUT_NAME).exists()

    # The unreadable arm publishes the same record the refusal arm does.
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unreadable"
    assert payload["reason"] == cli.REFUSE_UNUSABLE_REQUEST


def test_at_hz_reports_the_named_bin(round_dir: Path) -> None:
    assert (
        cli.main([str(round_dir), "--rungs-ms", "5", "20", "--at-hz", "800"])
        == cli.EXIT_OK
    )

    report = json.loads((round_dir / cli.DEFAULT_OUT_NAME).read_text())
    (feature,) = report["features"]
    assert feature["requested_hz"] == 800.0


def test_an_unwritable_out_is_the_write_exit_not_an_unreadable_round(
    round_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The round read and the sweep ran; only the filing failed."""
    blocker = round_dir / "not-a-dir"
    blocker.write_text("")

    rc = cli.main([str(round_dir), "--rungs-ms", "5", "20", "--out", str(blocker / "x.json")])

    assert rc == EXIT_WRITE_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unwritable"
    assert payload["reason"] == cli.REFUSE_UNWRITABLE_OUT


def test_at_hz_off_the_analysis_grid_is_an_input_error(round_dir: Path) -> None:
    assert cli.main([str(round_dir), "--at-hz", "100"]) == cli.EXIT_UNREADABLE
    assert not (round_dir / cli.DEFAULT_OUT_NAME).exists()
