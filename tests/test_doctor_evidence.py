# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The doctor's per-run evidence memo and the shared systemd reader."""
from __future__ import annotations

import threading

import pytest

from jasper import service_units
from jasper.cli.doctor import _evidence
from jasper.cli.doctor._evidence import Evidence, StatusRead


def test_a_key_is_read_once_even_under_concurrent_readers():
    ev = Evidence()
    reads = []
    gate = threading.Barrier(4)

    def read():
        reads.append(threading.get_ident())
        return "value"

    def worker():
        gate.wait()
        assert ev.get("k", read) == "value"

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(reads) == 1


def test_reset_clears_and_seed_preempts_the_reader():
    ev = Evidence()
    ev.seed("k", 1)
    assert ev.get("k", lambda: 2) == 1
    ev.reset()
    assert ev.get("k", lambda: 2) == 2


def test_daemon_status_is_fail_soft_and_classifies_unreachable(monkeypatch):
    def unreachable(path, *, timeout):
        raise ConnectionRefusedError(path)

    monkeypatch.setattr(_evidence, "read_status_socket", unreachable)
    ev = Evidence()
    read = ev.fanin_status()
    assert read.payload is None
    assert read.unreachable is True

    def malformed(path, *, timeout):
        raise ValueError("root is not an object")

    monkeypatch.setattr(_evidence, "read_status_socket", malformed)
    read = Evidence().outputd_status()
    assert read.payload is None
    assert read.unreachable is False


def test_unit_state_batches_the_roster_and_reads_an_unlisted_unit_once(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_read(units, *, timeout):
        calls.append(tuple(units))
        return {
            unit: {"unit": unit, "active_state": "active", "load_state": "loaded"}
            for unit in units
            if unit != "ghost.service"
        }

    monkeypatch.setattr(_evidence, "read_unit_states", fake_read)
    ev = Evidence()
    assert ev.unit_active("jasper-fanin.service") is True
    assert ev.unit_active("jasper-outputd.service") is True
    assert ev.unit_state("ghost.service")["load_state"] == "not-found"
    assert ev.unit_state("ghost.service")["load_state"] == "not-found"
    assert calls == [service_units.DOCTOR_UNIT_ROSTER, ("ghost.service",)]


def test_unit_state_is_none_without_systemctl(monkeypatch):
    monkeypatch.setattr(_evidence, "read_unit_states", lambda units, *, timeout: None)
    ev = Evidence()
    assert ev.unit_states() is None
    assert ev.unit_state("jasper-fanin.service") is None
    assert ev.unit_active("jasper-fanin.service") is None


def test_parse_systemctl_show_units_shapes_one_record_per_unit():
    text = (
        "Id=a.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
        "UnitFileState=enabled\nNRestarts=2\nMainPID=41\nMemoryCurrent=[not set]\n"
        "\n"
        "Id=b.service\nLoadState=not-found\nActiveState=inactive\nNRestarts=\n"
    )
    parsed = service_units.parse_systemctl_show_units(text)
    assert parsed["a.service"]["unit_file_state"] == "enabled"
    assert parsed["a.service"]["n_restarts"] == 2
    assert parsed["a.service"]["main_pid"] == 41
    assert parsed["a.service"]["memory_current_bytes"] is None
    assert parsed["b.service"]["load_state"] == "not-found"
    assert parsed["b.service"]["n_restarts"] == 0


@pytest.mark.parametrize(
    "raw,expected",
    [("7", 7), ("", None), ("[not set]", None), (str(1 << 63), None), ("x", None)],
)
def test_systemd_int(raw, expected):
    assert service_units.systemd_int(raw) == expected


def test_status_read_default_is_reachable():
    assert StatusRead({"ok": True}).unreachable is False
