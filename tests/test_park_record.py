# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper.control.park_record: the one reader shared by every bash-written
park record.

One parametrized pin proves each spec reads its own fixture into the
shared shape. Each spec's own module (camilla_recover_state,
outputd_failure_reconcile_state) pins its reader-specific branching —
unintelligible, the unit-state cross-check — against its own fixtures.
``jasper-bootloop-guard``'s JSON marker carries no park timestamp and shares
only :func:`park_record.read_json`, pinned by ``tests/test_bootloop_guard_script.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper import outputd_failure_reconcile_state
from jasper.control import camilla_recover_state, park_record

_SPECS_AND_FIXTURES = (
    (
        camilla_recover_state.SPEC,
        "reason=camilla_start_failed\ndetail=d\naction=a\nre_arm=r\n"
        "parked_utc=2026-01-15T12:00:00Z\n",
        {
            "reason": "camilla_start_failed", "detail": "d", "action": "a",
            "re_arm": "r", "parked_at": 1768478400,
        },
    ),
    (
        outputd_failure_reconcile_state.SPEC,
        "exit_status=78\nreason=recent\nparked_at=1000\n",
        {"exit_status": "78", "reason": "recent", "parked_at": 1000},
    ),
)
_IDS = ("camilla_recover", "outputd_failure_reconcile")


@pytest.mark.parametrize(
    "spec, record_text, expected_fields", _SPECS_AND_FIXTURES, ids=_IDS,
)
def test_snapshot_reads_the_fixture_into_the_shared_shape(
    tmp_path: Path, spec, record_text, expected_fields
):
    record = tmp_path / "record"
    record.write_text(record_text, encoding="utf-8")

    snap = park_record.snapshot(spec, str(record))

    assert snap["status"] == "present"
    assert snap["parked"] is True
    for key, value in expected_fields.items():
        assert snap[key] == value


@pytest.mark.parametrize(
    "spec", [row[0] for row in _SPECS_AND_FIXTURES], ids=_IDS,
)
def test_snapshot_absent_never_reads_as_present(tmp_path: Path, spec):
    missing = str(tmp_path / "does-not-exist")
    assert park_record.snapshot(spec, missing) == {
        "status": "absent", "parked": False, "path": missing,
    }
