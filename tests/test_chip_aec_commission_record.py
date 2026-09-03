# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The commissioning outcome record: writer/reader agreement and the
projection jasper-control publishes as `/aec`'s `commission` object."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jasper.chip_aec_commission_record import CommissionOutcome, read, write

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "outcome",
    [
        CommissionOutcome(),
        CommissionOutcome(state="passed", detail="", updated_at="2026-09-02T18:04:11Z"),
        CommissionOutcome(
            state="failed",
            detail="timing peak ratio 1.02 below 1.10",
            updated_at="2026-09-02T18:41:59Z",
        ),
    ],
)
def test_write_then_read_returns_the_same_record(
    tmp_path: Path, outcome: CommissionOutcome
) -> None:
    path = tmp_path / "chip-aec-commission.json"

    write(path, outcome)

    assert read(path) == outcome


@pytest.mark.parametrize(
    ("payload", "running", "expected"),
    [
        # The two current-schema shapes the commissioner writes.
        (
            {
                "schema_version": 1,
                "state": "passed",
                "detail": "",
                "updated_at": "2026-09-02T18:04:11Z",
            },
            False,
            {"running": False, "state": "passed", "detail": ""},
        ),
        (
            {
                "schema_version": 1,
                "state": "failed",
                "detail": "timing peak ratio 1.02 below 1.10",
                "updated_at": "2026-09-02T18:41:59Z",
            },
            True,
            {
                "running": True,
                "state": "failed",
                "detail": "timing peak ratio 1.02 below 1.10",
            },
        ),
        # A record written before schema_version/updated_at existed still
        # projects the same three public keys.
        (
            {"state": "failed", "detail": "timing peak ratio 1.02 below 1.10"},
            False,
            {
                "running": False,
                "state": "failed",
                "detail": "timing peak ratio 1.02 below 1.10",
            },
        ),
    ],
)
def test_public_projection_matches_the_published_commission_object(
    tmp_path: Path, payload: dict, running: bool, expected: dict
) -> None:
    path = tmp_path / "chip-aec-commission.json"
    path.write_text(json.dumps(payload))

    record = read(path)

    assert record is not None
    assert record.to_public(running=running) == expected


@pytest.mark.parametrize("text", [None, "{not json", "[1, 2]"])
def test_an_unusable_record_reads_as_absent(tmp_path: Path, text: str | None) -> None:
    path = tmp_path / "chip-aec-commission.json"
    if text is not None:
        path.write_text(text)

    assert read(path) is None
    assert CommissionOutcome().to_public(running=False) == {
        "running": False,
        "state": "",
        "detail": "",
    }


def test_control_reads_the_record_without_the_measurement_stack() -> None:
    # The record is a shared contract precisely so the long-lived control
    # daemon never imports the commissioner, which pulls numpy.
    code = (
        "import sys; "
        "import jasper.control.aec_endpoints; "
        "raise SystemExit(1 if {'numpy', 'scipy', 'sounddevice'} & set(sys.modules)"
        " else 0)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, check=False, timeout=60,
    )

    assert result.returncode == 0
