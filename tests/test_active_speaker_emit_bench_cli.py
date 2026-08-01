# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The emit-loop CLI's argument contract and its no-binary preflight.

``--dry-run`` is the only path exercised here: the rendering path resolves the
binary from ``jasper-camilla.service`` and belongs to the on-device run, while
the loop itself is covered against a stand-in binary in
``tests/test_active_speaker_emit_bench_loop.py``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from jasper.cli import active_speaker_emit_bench as cli

SHELF_Q = 1.0 / math.sqrt(2.0)


def _fit_file(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "linearization.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fits() -> dict[str, object]:
    """The persisted ``{role: LinearizationFit.to_dict()}`` shape."""

    return {
        "tweeter": {
            "role": "tweeter",
            "filters": [
                {
                    "biquad_type": "Lowshelf",
                    "freq": 8400.0,
                    "q": SHELF_Q,
                    "gain": -11.0,
                }
            ],
        }
    }


def test_dry_run_prints_the_plan_and_renders_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(
        [
            "--linearization",
            str(_fit_file(tmp_path, _fits())),
            "--playback-device",
            "hw:CARD=DAC8x,DEV=0",
            "--out",
            str(tmp_path / "bundle"),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "tweeter: 1 filter(s) Lowshelf@8400Hz/-11.0dB" in out
    assert "analysis band:" in out
    assert "no binary resolved, nothing rendered" in out
    # A dry run must not create the bundle or touch a device.
    assert not (tmp_path / "bundle").exists()


def test_an_already_reduced_linearization_shape_is_refused(tmp_path: Path) -> None:
    """The reducer returns ``{}`` — not an error — on the reduced shape.

    A CLI that sniffed between the two shapes would silently grade nothing, so
    one shape is accepted and the other is named in the refusal.
    """

    reduced = {"tweeter": [{"biquad_type": "Lowshelf", "freq": 8400.0, "q": SHELF_Q, "gain": -11.0}]}
    with pytest.raises(SystemExit, match="already-reduced"):
        cli.main(
            [
                "--linearization",
                str(_fit_file(tmp_path, reduced)),
                "--playback-device",
                "hw:CARD=DAC8x,DEV=0",
                "--dry-run",
            ]
        )


def test_a_missing_or_malformed_linearization_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="could not read"):
        cli.main(
            [
                "--linearization",
                str(tmp_path / "absent.json"),
                "--playback-device",
                "x",
                "--dry-run",
            ]
        )
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SystemExit, match="keyed by driver role"):
        cli.main(
            ["--linearization", str(bad), "--playback-device", "x", "--dry-run"]
        )


def test_playback_device_and_linearization_are_required() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_there_is_no_binary_override_flag() -> None:
    """R5: the render binary is resolved from the running unit, never overridden.

    A ``--binary`` flag would be the obvious convenience and is deliberately
    absent — it would let a bench run grade a build that is not the one the
    speaker actually runs.
    """

    with pytest.raises(SystemExit):
        cli.main(
            [
                "--linearization",
                "x.json",
                "--playback-device",
                "y",
                "--binary",
                "/usr/bin/camilladsp",
            ]
        )
