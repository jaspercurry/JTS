# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The read-side ``systemctl`` probe owner's semantics matrix.

Every caller picks its verdict here by parameter, so this is the one place
the rc/stdout -> verdict mapping is pinned; a caller's own test pins only
which parameters it chose.
"""
from __future__ import annotations

import subprocess

import pytest

from jasper import systemd_probe


def _fake_run(monkeypatch, *, stdout="", returncode=0, raises=None, calls=None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((list(argv), kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(systemd_probe.subprocess, "run", run)


@pytest.mark.parametrize(
    ("state", "activating_is_live", "expected"),
    [
        ("active", False, True),
        ("active", True, True),
        ("reloading", False, True),
        ("reloading", True, True),
        ("activating", False, False),
        ("activating", True, True),
        ("deactivating", False, False),
        ("deactivating", True, False),
        ("inactive", False, False),
        ("inactive", True, False),
        ("failed", False, False),
        ("failed", True, False),
        (systemd_probe.UNKNOWN, False, False),
        (systemd_probe.UNKNOWN, True, False),
    ],
)
def test_unit_active_verdict_matrix(monkeypatch, state, activating_is_live, expected):
    # rc is non-zero for every non-active word and this probe must ignore it:
    # `is-active` exits non-zero whenever any unit is not active.
    _fake_run(monkeypatch, stdout=state + "\n", returncode=3)
    assert systemd_probe.unit_active(
        "u.service", timeout=1.0, activating_is_live=activating_is_live,
    ) is expected


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("systemctl"),
        PermissionError("systemctl"),
        subprocess.TimeoutExpired(["systemctl"], 1.0),
        subprocess.SubprocessError("boom"),
    ],
)
def test_unit_states_is_failsoft_and_never_live(monkeypatch, failure):
    _fake_run(monkeypatch, raises=failure)
    units = ["a.service", "b.service"]
    assert systemd_probe.unit_states(units, timeout=1.0) == {
        u: systemd_probe.UNKNOWN for u in units
    }
    assert systemd_probe.unit_active(
        "a.service", timeout=1.0, activating_is_live=True,
    ) is False


def test_unit_states_batches_one_spawn_in_argument_order(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    _fake_run(monkeypatch, stdout="active\nfailed\nactivating\n", returncode=3, calls=calls)
    units = ["a.service", "b.service", "c.service"]

    assert systemd_probe.unit_states(units, timeout=4.0) == {
        "a.service": "active",
        "b.service": "failed",
        "c.service": "activating",
    }
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["systemctl", "is-active", *units]
    assert kwargs["timeout"] == 4.0


@pytest.mark.parametrize("stdout", ["active\n", "active\nfailed\nactive\n", ""])
def test_unit_states_line_count_mismatch_is_unknown(monkeypatch, stdout):
    _fake_run(monkeypatch, stdout=stdout, returncode=3)
    units = ["a.service", "b.service"]
    assert systemd_probe.unit_states(units, timeout=1.0) == {
        u: systemd_probe.UNKNOWN for u in units
    }


def test_no_units_asks_systemd_nothing(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    _fake_run(monkeypatch, calls=calls)
    assert systemd_probe.unit_states([], timeout=1.0) == {}
    assert calls == []


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected_value", "expected_loaded"),
    [
        (0, "loaded\n", "loaded", True),
        (0, "not-found\n", "not-found", False),
        (0, "masked\n", "masked", False),
        (0, "\n", "", False),
        (1, "loaded\n", None, False),
    ],
)
def test_unit_property_and_loaded(
    monkeypatch, returncode, stdout, expected_value, expected_loaded,
):
    calls: list[tuple[list[str], dict]] = []
    _fake_run(monkeypatch, stdout=stdout, returncode=returncode, calls=calls)

    assert systemd_probe.unit_property(
        "u.service", "LoadState", timeout=2.0,
    ) == expected_value
    assert systemd_probe.unit_loaded("u.service", timeout=2.0) is expected_loaded
    assert calls[0][0] == [
        "systemctl", "show", "u.service", "--property=LoadState", "--value",
    ]


def test_unit_property_probe_failure_is_none(monkeypatch):
    _fake_run(monkeypatch, raises=subprocess.TimeoutExpired(["systemctl"], 1.0))
    assert systemd_probe.unit_property("u.service", "ActiveState", timeout=1.0) is None
    assert systemd_probe.unit_loaded("u.service", timeout=1.0) is False
