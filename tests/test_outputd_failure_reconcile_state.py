# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one reader of jasper-outputd's failure-reconcile stamp and its park.

The stamp is written by a shell ``ExecStopPost=`` helper and read back in
Python by two surfaces (jasper-doctor, ``/state.resilience``), so these pin
both halves: the reader's branches, and the literals it duplicates from the
script it reads.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper import outputd_failure_reconcile_state as reader

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-outputd-failure-reconcile"
UNIT = ROOT / "deploy" / "systemd" / "jasper-outputd.service"


def _stamp(tmp_path: Path, text: str) -> str:
    path = tmp_path / "failure-reconcile.stamp"
    path.write_text(text)
    return str(path)


FAILED = {"active_state": "failed", "result": "exit-code"}
RUNNING = {"active_state": "active", "result": "success"}


def test_absent_stamp_in_a_live_runtime_dir_is_no_reconcile(tmp_path):
    snap = reader.snapshot(RUNNING, path=str(tmp_path / "nope.stamp"))
    assert snap["reason"] == reader.REASON_NO_RECONCILE
    assert snap["present"] is False
    assert snap["parked"] is False


def test_absent_runtime_dir_is_distinct_from_an_absent_stamp(tmp_path):
    """systemd removes RuntimeDirectory on a full stop, so a missing
    /run/jasper-outputd is "no evidence", not "healthy"."""
    snap = reader.snapshot(RUNNING, path=str(tmp_path / "gone" / "x.stamp"))
    assert snap["reason"] == reader.REASON_RUNTIME_DIR_ABSENT
    assert snap["parked"] is False


def test_unreadable_stamp_does_not_read_as_healthy(tmp_path, monkeypatch):
    path = _stamp(tmp_path, "1000")

    def boom(*_a, **_kw):
        raise PermissionError("denied")

    monkeypatch.setattr("builtins.open", boom)
    snap = reader.snapshot(RUNNING, path=path)
    assert snap["reason"] == reader.REASON_UNREADABLE
    assert snap["parked"] is False


def test_unintelligible_stamp_is_reported_distinctly(tmp_path):
    snap = reader.snapshot(RUNNING, path=_stamp(tmp_path, "not-an-epoch"))
    assert snap["reason"] == reader.REASON_UNINTELLIGIBLE
    assert snap["present"] is True
    assert snap["parked"] is False


def test_no_systemd_view_cannot_rule_a_park_in_or_out(tmp_path):
    snap = reader.snapshot(None, path=_stamp(tmp_path, "1000"), now=1100.0)
    assert snap["reason"] == reader.REASON_UNIT_STATE_UNAVAILABLE
    assert snap["parked"] is False


@pytest.mark.parametrize(
    "unit_state, reason, parked",
    [
        (RUNNING, reader.REASON_RECONCILED, False),
        (FAILED, reader.REASON_PARKED, True),
    ],
    ids=["recovered", "parked"],
)
def test_park_is_a_reconcile_on_record_plus_a_failed_unit(
    tmp_path, unit_state, reason, parked,
):
    snap = reader.snapshot(unit_state, path=_stamp(tmp_path, "1000"), now=1100.0)
    assert snap["reason"] == reason
    assert snap["parked"] is parked
    assert snap["at"] == 1000
    assert snap["age_s"] == 100.0


@pytest.mark.parametrize(
    "now, spent",
    [(1100.0, True), (1000.0 + reader.DEFAULT_WINDOW_SEC, False)],
    ids=["inside-window", "window-elapsed"],
)
def test_window_spent_tracks_the_helper_dedup_bound(tmp_path, now, spent):
    snap = reader.snapshot(RUNNING, path=_stamp(tmp_path, "1000"), now=now)
    assert snap["window_spent"] is spent
    assert snap["window_sec"] == reader.DEFAULT_WINDOW_SEC


def test_env_overrides_match_the_scripts_own_names(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_OUTPUTD_CONFIG_RETRY_STATE", _stamp(tmp_path, "1000"))
    monkeypatch.setenv("JASPER_OUTPUTD_CONFIG_RETRY_WINDOW_SEC", "10")
    snap = reader.snapshot(RUNNING, now=1005.0)
    assert snap["window_sec"] == 10
    assert snap["window_spent"] is True


# --------------------------------------------------- literals shared with sh


def test_reader_defaults_equal_the_shell_writers_defaults():
    """A literal duplicated across a shell writer and a Python reader is
    exactly the pair that drifts."""
    text = SCRIPT.read_text()
    stamp = re.search(
        r'RECONCILE_STAMP="\$\{JASPER_OUTPUTD_CONFIG_RETRY_STATE:-([^}]+)\}"', text,
    )
    window = re.search(
        r'RECONCILE_WINDOW_SEC="\$\{JASPER_OUTPUTD_CONFIG_RETRY_WINDOW_SEC:-(\d+)\}"',
        text,
    )
    assert stamp and stamp.group(1) == reader.DEFAULT_STAMP_PATH
    assert window and int(window.group(1)) == reader.DEFAULT_WINDOW_SEC


def test_the_unit_this_reader_names_is_the_one_that_runs_the_helper():
    """``reader.UNIT`` is the unit whose ExecStopPost writes the stamp; the
    park verdict is meaningless if the two ever come apart."""
    assert UNIT.name == reader.UNIT
    assert f"ExecStopPost=-/usr/local/sbin/{SCRIPT.name}" in UNIT.read_text()
