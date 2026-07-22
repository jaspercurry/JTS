# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for request-scoped batched systemd state reads."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from jasper.web import _unit_snapshot as mod


def test_probe_parses_loaded_missing_and_all_active_states(monkeypatch):
    run_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        run_calls.append(command)
        assert kwargs["timeout"] == 2.5
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "Id=a.service\nLoadState=loaded\nActiveState=active\n\n"
                "Id=b.service\nLoadState=loaded\nActiveState=activating\n\n"
                "Id=c.service\nLoadState=loaded\nActiveState=inactive\n\n"
                "Id=d.service\nLoadState=not-found\nActiveState=inactive\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    snapshot = mod.probe_unit_snapshot(
        ("a.service", "b.service", "c.service", "d.service"),
        timeout=2.5,
    )

    assert len(run_calls) == 1
    assert run_calls[0][-4:] == [
        "a.service", "b.service", "c.service", "d.service",
    ]
    assert snapshot.available("a.service") is True
    assert snapshot.active("a.service") is True
    assert snapshot.activating("b.service") is True
    assert snapshot.active("c.service") is False
    assert snapshot.available("d.service") is False
    assert snapshot.error == ""


def test_nonzero_partial_output_preserves_returned_records(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="Id=a.service\nLoadState=loaded\nActiveState=active\n",
            stderr="Unit b.service could not be found.",
        ),
    )

    snapshot = mod.probe_unit_snapshot(("a.service", "b.service"))

    assert snapshot.active("a.service") is True
    assert snapshot.available("b.service") is False
    assert "Unit b.service could not be found" in snapshot.error
    assert "no state returned for: b.service" in snapshot.error


def test_whole_probe_failure_is_unknown_and_fail_closed(monkeypatch):
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("systemctl", 5)

    monkeypatch.setattr(mod.subprocess, "run", fail)

    snapshot = mod.probe_unit_snapshot(("a.service",))

    assert snapshot.states == {}
    assert snapshot.available("a.service") is False
    assert snapshot.active("a.service") is False
    assert snapshot.activating("a.service") is False
    assert snapshot.error


def test_invalid_unit_name_never_reaches_subprocess(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid unit reached systemctl")
        ),
    )

    snapshot = mod.probe_unit_snapshot(("not a unit",))

    assert snapshot.states == {}
    assert "invalid systemd unit" in snapshot.error
