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


def _fake_run(monkeypatch, *, stdout="", returncode=0, raises=None, calls=None,
              stderr=""):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((list(argv), kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=stderr,
        )

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
    ("query", "state", "expected"),
    [
        ("is-active", "active", True),
        ("is-active", "inactive", False),
        ("is-active", "failed", False),
        ("is-active", "activating", None),  # a transitional word: unresolved
        ("is-enabled", "enabled", True),
        ("is-enabled", "enabled-runtime", True),
        ("is-enabled", "disabled", False),
        ("is-enabled", "alias", False),
        ("is-enabled", "static", False),
        ("is-enabled", "indirect", False),
        ("is-enabled", "generated", False),
        ("is-enabled", "transient", False),
        ("is-enabled", "linked", False),
        ("is-enabled", "linked-runtime", False),
        ("is-enabled", "masked", False),
        ("is-enabled", "masked-runtime", False),
        ("is-enabled", "not-found", False),
        ("is-failed", "failed", True),
        ("is-failed", "active", False),
        ("is-failed", "activating", False),
        ("is-failed", "deactivating", False),
        ("is-failed", "inactive", False),
        ("is-failed", "maintenance", False),
        ("is-failed", "reloading", False),
        ("is-failed", "bad-word", None),
    ],
)
def test_unit_query_verdict_matrix(monkeypatch, query, state, expected):
    # rc is non-zero for a legitimate not-true word too (`is-enabled`/
    # `is-failed` exit non-zero for most of their false-ish states); this
    # probe must classify by stdout TEXT alone, same as `unit_active` above.
    _fake_run(monkeypatch, stdout=state.upper() + "\n", returncode=1, stderr="why")
    result = systemd_probe.unit_state(query, "u.service", timeout=1.0)
    assert systemd_probe.unit_query(result) is expected
    # The diagnostic an unresolved caller logs instead of a verdict.
    assert (result.word, result.rc, result.stderr, result.error) == (
        state, 1, "why", None,
    )


def test_unit_state_argv_and_timeout(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    _fake_run(monkeypatch, stdout="active\n", calls=calls)
    result = systemd_probe.unit_state("is-active", "u.service", timeout=3.5)
    assert systemd_probe.unit_query(result) is True
    assert calls[0][0] == ["systemctl", "is-active", "u.service"]
    assert calls[0][1]["timeout"] == 3.5


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("systemctl"),
        OSError("cannot allocate process"),
        subprocess.TimeoutExpired(["systemctl"], 1.0),
        subprocess.SubprocessError("boom"),
    ],
)
def test_unit_state_spawn_failure_is_unresolved_with_error(monkeypatch, failure):
    _fake_run(monkeypatch, raises=failure)
    result = systemd_probe.unit_state("is-enabled", "u.service", timeout=1.0)
    assert systemd_probe.unit_query(result) is None
    assert result.word is None
    assert result.rc is None
    assert result.error is failure
