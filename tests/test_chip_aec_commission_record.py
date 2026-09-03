# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The commissioning outcome record: writer/reader agreement and the
projection jasper-control publishes as `/aec`'s `commission` object."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.chip_aec_commission_record import CommissionOutcome, read, write


@pytest.mark.parametrize(
    ("outcome", "running", "public"),
    [
        (
            CommissionOutcome(),
            False,
            {"running": False, "state": "", "detail": ""},
        ),
        (
            CommissionOutcome(
                state="passed", detail="", updated_at="2026-09-02T18:04:11Z"
            ),
            False,
            {"running": False, "state": "passed", "detail": ""},
        ),
        (
            CommissionOutcome(
                state="failed",
                detail="timing peak ratio 1.02 below 1.10",
                updated_at="2026-09-02T18:41:59Z",
            ),
            True,
            {
                "running": True,
                "state": "failed",
                "detail": "timing peak ratio 1.02 below 1.10",
            },
        ),
    ],
)
def test_a_written_record_reads_back_and_projects_the_commission_object(
    tmp_path: Path,
    outcome: CommissionOutcome,
    running: bool,
    public: dict,
) -> None:
    path = tmp_path / "chip-aec-commission.json"

    write(path, outcome)

    assert read(path) == outcome
    assert outcome.to_public(running=running) == public


def test_a_record_written_before_the_schema_keys_still_projects(
    tmp_path: Path,
) -> None:
    # The shape on disk before schema_version/updated_at existed. Its version
    # stays unknown rather than being stamped as the current one.
    path = tmp_path / "chip-aec-commission.json"
    path.write_text(
        json.dumps(
            {"state": "failed", "detail": "timing peak ratio 1.02 below 1.10"}
        )
    )

    record = read(path)

    assert record is not None
    assert record.schema_version is None
    assert record.to_public(running=False) == {
        "running": False,
        "state": "failed",
        "detail": "timing peak ratio 1.02 below 1.10",
    }


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
