# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-declare-geometry``: unit handling and refusal exit codes.

The math itself is pinned in ``tests/test_audio_measurement_geometry.py``;
these tests are the CLI seam -- inches convert to exact meters, exactly one
unit per field is enforced by argparse, and a validation refusal from
:class:`~jasper.audio_measurement.measurement_geometry.DeclaredGeometry`
surfaces as a non-zero exit code naming the field.
"""
from __future__ import annotations

import json

import pytest

from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.cli import declare_geometry


def _set_argv(path, **fields):
    argv = ["set", "--path", str(path)]
    for flag, value in fields.items():
        argv += [flag, str(value)]
    return argv


def test_set_converts_inches_to_exact_meters(tmp_path):
    path = tmp_path / "geometry.json"
    code = declare_geometry.main(
        _set_argv(
            path,
            **{
                "--speaker-height-in": 33.0,
                "--mic-height-m": 0.84,
                "--distance-m": 1.0,
            },
        )
    )
    assert code == declare_geometry.EXIT_OK

    saved = json.loads(path.read_text())
    assert saved["speaker_height_m"] == pytest.approx(
        33.0 * declare_geometry.METERS_PER_INCH, rel=0, abs=1e-12
    )


def test_set_then_show_round_trips_the_declared_geometry(tmp_path):
    path = tmp_path / "geometry.json"
    code = declare_geometry.main(
        _set_argv(
            path,
            **{
                "--speaker-height-m": 0.84,
                "--mic-height-m": 0.5,
                "--distance-m": 1.2,
                "--ceiling-height-m": 2.4,
            },
        )
    )
    assert code == declare_geometry.EXIT_OK

    show_code = declare_geometry.main(["show", "--path", str(path)])
    assert show_code == declare_geometry.EXIT_OK

    loaded = DeclaredGeometry.load(path)
    assert loaded == DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.5, distance_m=1.2, ceiling_height_m=2.4,
    )


@pytest.mark.parametrize(
    "missing_flag",
    [
        pytest.param("--speaker-height-m", id="speaker_height_missing"),
        pytest.param("--mic-height-m", id="mic_height_missing"),
        pytest.param("--distance-m", id="distance_missing"),
    ],
)
def test_set_requires_exactly_one_unit_for_each_required_field(tmp_path, missing_flag):
    fields = {
        "--speaker-height-m": 0.84,
        "--mic-height-m": 0.84,
        "--distance-m": 1.0,
    }
    del fields[missing_flag]
    with pytest.raises(SystemExit):
        declare_geometry.main(_set_argv(tmp_path / "geometry.json", **fields))


def test_set_refuses_both_units_given_for_one_field(tmp_path):
    argv = _set_argv(
        tmp_path / "geometry.json",
        **{
            "--speaker-height-in": 33.0,
            "--speaker-height-m": 0.84,
            "--mic-height-m": 0.84,
            "--distance-m": 1.0,
        },
    )
    with pytest.raises(SystemExit):
        declare_geometry.main(argv)


def test_set_refuses_both_units_given_for_the_optional_ceiling(tmp_path):
    argv = _set_argv(
        tmp_path / "geometry.json",
        **{
            "--speaker-height-m": 0.84,
            "--mic-height-m": 0.84,
            "--distance-m": 1.0,
            "--ceiling-height-in": 94.0,
            "--ceiling-height-m": 2.4,
        },
    )
    with pytest.raises(SystemExit):
        declare_geometry.main(argv)


def test_set_refuses_an_out_of_range_field_with_a_named_field(tmp_path, capsys):
    path = tmp_path / "geometry.json"
    code = declare_geometry.main(
        _set_argv(
            path,
            **{
                "--speaker-height-m": 0.84,
                "--mic-height-m": 0.84,
                "--distance-m": 5.0,
            },
        )
    )
    assert code == declare_geometry.EXIT_REFUSED
    assert "distance_m" in capsys.readouterr().err
    assert not path.exists()


def test_show_of_a_missing_file_returns_not_found(tmp_path):
    code = declare_geometry.main(["show", "--path", str(tmp_path / "absent.json")])
    assert code == declare_geometry.EXIT_NOT_FOUND
