# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one reader of jasper-outputd's park record.

The record is written by a shell ``ExecStopPost=`` helper, removed by the
unit's ``ExecStartPost=``, and read back in Python by two surfaces
(jasper-doctor, ``/state.resilience``), so these pin both halves: the reader's
branches, and the path literal it duplicates from the two files that own it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper import outputd_failure_reconcile_state as reader

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-outputd-failure-reconcile"
UNPARK_SCRIPT = ROOT / "deploy" / "bin" / "jasper-unpark"
UNIT = ROOT / "deploy" / "systemd" / "jasper-outputd.service"

FAILED = {"active_state": "failed", "result": "exit-code"}
RUNNING = {"active_state": "active", "result": "success"}
START_LIMITED = {"active_state": "inactive", "result": "start-limit-hit"}
NOT_INSTALLED = {"active_state": "inactive", "load_state": "not-found"}
ACTIVATING = {"active_state": "activating", "result": "success"}


def _record(tmp_path: Path, text: str = "parked_at=1000\nexit_status=78\nreason=recent\n") -> str:
    path = tmp_path / "failure-reconcile.park"
    path.write_text(text)
    return str(path)


@pytest.mark.parametrize(
    "record, unit_state, expected, parked",
    [
        (None, RUNNING, reader.REASON_OK, False),
        (None, FAILED, reader.REASON_UNIT_FAILED, False),
        (None, START_LIMITED, reader.REASON_UNIT_FAILED, False),
        (None, NOT_INSTALLED, reader.REASON_UNIT_FAILED, False),
        (None, ACTIVATING, reader.REASON_UNIT_UNSTABLE, False),
        (None, None, reader.REASON_UNOBSERVED, False),
        ("record", FAILED, reader.REASON_PARKED, True),
        ("record", None, reader.REASON_PARKED, True),
        ("record", RUNNING, reader.REASON_RECORD_STALE, False),
    ],
    ids=[
        "healthy", "failed-no-record", "start-limited-no-record",
        "not-installed-no-record", "unstable-no-record",
        "no-systemd-view", "parked", "parked-without-systemd-view", "stale",
    ],
)
def test_snapshot_reasons(tmp_path, record, unit_state, expected, parked):
    target = _record(tmp_path) if record else str(tmp_path / "absent.park")
    snap = reader.snapshot(unit_state, path=target)
    assert snap["reason"] == expected
    assert snap["parked"] is parked


def test_the_record_is_the_park_not_an_inference_from_a_failed_unit(tmp_path):
    """The regression this reader exists for: a leftover record plus a later,
    unrelated failure must not read as the park the record describes, and a
    failure with no record must not borrow one."""
    stale = reader.snapshot(RUNNING, path=_record(tmp_path))
    assert stale["reason"] == reader.REASON_RECORD_STALE
    assert stale["parked"] is False

    unrecorded = reader.snapshot(FAILED, path=str(tmp_path / "absent.park"))
    assert unrecorded["parked"] is False
    assert unrecorded["reason"] == reader.REASON_UNIT_FAILED


def test_a_park_carries_the_writers_own_fields(tmp_path):
    snap = reader.snapshot(FAILED, path=_record(tmp_path))
    assert (snap["parked_at"], snap["exit_status"], snap["park_reason"]) == (
        1000, "78", "recent",
    )


def test_a_partial_record_is_still_a_park(tmp_path):
    """The helper writes only on a park, so a record that lost its fields to a
    truncated write is still one — reported without the fields it lost."""
    snap = reader.snapshot(FAILED, path=_record(tmp_path, "parked_at=nope\n"))
    assert snap["reason"] == reader.REASON_PARKED
    assert snap["parked"] is True
    assert snap["parked_at"] is None


def test_an_unreadable_record_does_not_read_as_healthy(tmp_path, monkeypatch):
    target = _record(tmp_path)

    def boom(*_a, **_kw):
        raise PermissionError("denied")

    monkeypatch.setattr("builtins.open", boom)
    snap = reader.snapshot(RUNNING, path=target)
    assert snap["reason"] == reader.REASON_UNOBSERVED
    assert snap["parked"] is False


def test_the_env_override_matches_the_scripts_own_name(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_OUTPUTD_RECONCILE_PARK_STATE", _record(tmp_path))
    assert reader.snapshot(FAILED)["reason"] == reader.REASON_PARKED


# ------------------------------------- the path literal its writers duplicate


def test_the_record_path_is_the_one_the_script_writes_and_the_unit_removes():
    """A literal duplicated across a shell writer, a unit file and a Python
    reader is exactly the set that drifts.

    The retirer (jasper-unpark) takes the path as argv, not an env read of its
    own — it is shared with jasper-camilla.service's different record — so the
    unit is what has to honor the variable: one Environment= the writer's
    ExecStopPost= and the retirer's ExecStartPost= both run under.
    """
    script = SCRIPT.read_text()
    unit = UNIT.read_text()
    assert (
        f'PARK_RECORD="${{JASPER_OUTPUTD_RECONCILE_PARK_STATE:-{reader.DEFAULT_RECORD_PATH}}}"'
        in script
    )
    assert (
        f'Environment="JASPER_OUTPUTD_RECONCILE_PARK_STATE={reader.DEFAULT_RECORD_PATH}"'
        in unit
    )
    assert (
        f"ExecStartPost=-/usr/local/sbin/{UNPARK_SCRIPT.name} "
        "$JASPER_OUTPUTD_RECONCILE_PARK_STATE outputd.unparked" in unit
    )
    assert UNIT.name == reader.UNIT
    assert f"ExecStopPost=-/usr/local/sbin/{SCRIPT.name}" in unit


def test_the_record_lives_outside_the_runtime_directory_systemd_deletes():
    """systemd removes RuntimeDirectory=jasper-outputd when the unit stops
    without a restart — which is the very stop this record reports."""
    assert "RuntimeDirectory=jasper-outputd" in UNIT.read_text()
    assert not reader.DEFAULT_RECORD_PATH.startswith("/run/jasper-outputd/")


# ------------------------------------------------------- last_park (R15, #4416)


def test_last_park_is_none_with_no_last_sibling(tmp_path):
    snap = reader.snapshot(RUNNING, path=str(tmp_path / "absent.park"))
    assert snap["last_park"] is None


def test_last_park_surfaces_the_retired_record_including_unparked_at(tmp_path):
    target = str(tmp_path / "failure-reconcile.park")
    last = Path(f"{target}.last")
    last.write_text("parked_at=1000\nexit_status=78\nreason=recent\nunparked_at=1200\n")
    snap = reader.snapshot(RUNNING, path=target)
    assert snap["last_park"] == {
        "parked_at": 1000,
        "exit_status": "78",
        "park_reason": "recent",
        "unparked_at": 1200,
    }


def test_last_park_present_alongside_an_active_park(tmp_path):
    """A currently-live park and a previously-retired one are independent —
    the reader must not conflate them."""
    target = _record(tmp_path)
    Path(f"{target}.last").write_text("parked_at=1\nunparked_at=2\n")
    snap = reader.snapshot(FAILED, path=target)
    assert snap["parked"] is True
    assert snap["last_park"] == {
        "parked_at": 1,
        "exit_status": None,
        "park_reason": None,
        "unparked_at": 2,
    }
