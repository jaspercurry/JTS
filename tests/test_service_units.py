# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper.service_units: the shared ``systemctl show`` reader and its
record predicates (ADR-0233 rule 1 — one parser, one roster)."""
from __future__ import annotations

import subprocess
import time
from types import SimpleNamespace

import pytest

from jasper import service_units


@pytest.mark.parametrize(
    ("record", "loaded", "active", "activating"),
    [
        (None, False, False, False),
        ({}, False, False, False),
        ({"load_state": "loaded", "active_state": "active"}, True, True, False),
        ({"load_state": "loaded", "active_state": "activating"}, True, False, True),
        ({"load_state": "loaded", "active_state": "inactive"}, True, False, False),
        ({"load_state": "loaded", "active_state": "failed"}, True, False, False),
        ({"load_state": "not-found", "active_state": "inactive"}, False, False, False),
    ],
    ids=["absent", "empty", "active", "activating", "inactive", "failed", "not-found"],
)
def test_unit_predicates_match_read_unit_states_record_shape(
    record, loaded, active, activating,
):
    """``unit_loaded``/``unit_active``/``unit_activating`` read the same
    ``load_state``/``active_state`` fields ``unit_failed`` does, and are
    None-safe the same way (a missing/empty record is every predicate's
    False, never a crash)."""
    assert service_units.unit_loaded(record) is loaded
    assert service_units.unit_active(record) is active
    assert service_units.unit_activating(record) is activating


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (None, "missing"),
        ({}, None),
        ({"load_state": "not-found", "active_state": "inactive"}, "missing"),
        (
            {"load_state": "loaded", "unit_file_state": "disabled",
             "active_state": "inactive"},
            "not_enabled",
        ),
        (
            {"load_state": "loaded", "unit_file_state": "enabled",
             "active_state": "inactive"},
            "inactive",
        ),
        (
            {"load_state": "loaded", "unit_file_state": "enabled",
             "active_state": "activating"},
            "starting",
        ),
        (
            {"load_state": "loaded", "unit_file_state": "enabled",
             "active_state": "reloading"},
            "starting",
        ),
        (
            {"load_state": "loaded", "unit_file_state": "enabled",
             "active_state": "failed"},
            "inactive",
        ),
        (
            {"load_state": "loaded", "unit_file_state": "enabled",
             "active_state": "active"},
            None,
        ),
        (
            {"load_state": "error", "unit_file_state": "enabled",
             "active_state": "inactive"},
            "inactive",
        ),
    ],
    ids=[
        "absent", "empty", "not-found", "not-enabled", "loaded-inactive",
        "activating", "reloading", "failed", "active", "load-state-error",
    ],
)
def test_unit_not_running_reads_the_unit_record(record, code):
    """The one classification :mod:`jasper.control.audio_health` and
    jasper-doctor's ``_service_state_failure`` share for "this unit is not
    doing its job". ``load_state == "error"`` is NOT ``"missing"`` (#2163):
    origin/main's ladder only treats ``"not-found"`` that way."""
    assert service_units.unit_not_running(record) == code


# The two cases jasper.web._unit_snapshot's now-retired parser pinned that
# read_unit_states' own contract did not yet have a test for (ADR-0233 rule
# 1 fold-in): tolerating systemctl's returncode 1 for a partially-answered
# batch, and failing closed (None) when the call itself never completes.


def test_read_unit_states_tolerates_returncode_one_with_partial_output(monkeypatch):
    """systemctl show exits 1 when a requested unit is not found, but still
    emits Id=/LoadState=/... for every unit it does have an answer for."""
    monkeypatch.setattr(
        service_units.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1,
            stdout="Id=a.service\nLoadState=loaded\nActiveState=active\n",
            stderr="Unit b.service could not be found.",
        ),
    )

    states = service_units.read_unit_states(("a.service", "b.service"))

    assert states is not None
    assert states["a.service"]["active_state"] == "active"
    assert "b.service" not in states


def test_read_unit_states_is_none_when_systemctl_exits_unexpectedly(monkeypatch):
    monkeypatch.setattr(
        service_units.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr="boom"),
    )

    assert service_units.read_unit_states(("a.service",)) is None


def test_read_unit_states_is_none_when_the_subprocess_itself_fails(monkeypatch):
    def fail(*_a, **_k):
        raise subprocess.TimeoutExpired("systemctl", 2)

    monkeypatch.setattr(service_units.subprocess, "run", fail)

    assert service_units.read_unit_states(("a.service",)) is None


@pytest.mark.parametrize(
    ("record", "is_none"),
    [
        (None, True),
        ({}, True),
        ({"active_enter_timestamp_monotonic": None}, True),
        ({"active_enter_timestamp_monotonic": 0}, True),
        ({"active_enter_timestamp_monotonic": -1}, True),
    ],
    ids=["absent", "empty", "unset", "zero", "negative"],
)
def test_unit_uptime_sec_is_none_without_a_usable_timestamp(record, is_none):
    assert (service_units.unit_uptime_sec(record) is None) is is_none


def test_unit_uptime_sec_reads_the_monotonic_clock_shared_with_systemd():
    now_us = time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6
    started_us = int(now_us - 90.0 * 1e6)

    uptime = service_units.unit_uptime_sec(
        {"active_enter_timestamp_monotonic": started_us}
    )

    assert uptime == pytest.approx(90.0, abs=1.0)
