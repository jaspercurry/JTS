# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The doctor's per-run evidence memo and the shared systemd reader."""
from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper import service_units
from jasper.cli.doctor import _evidence
from jasper.cli.doctor._evidence import Evidence, StatusRead

from .doctor_test_support import _fresh_cfg


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


@pytest.mark.parametrize("install_profile", ["full", "streambox"])
def test_grouping_config_and_crossover_status_are_read_once_per_registry_run(
    monkeypatch, install_profile,
):
    """ADR-0233 rule 4, end to end: whatever subset of the ~170 registered
    checks consumes the household's grouping config or the crossover-v2
    status block, each is read AT MOST ONCE per run — the whole point of
    routing every consumer through ``evidence.grouping_config()`` /
    ``evidence.crossover_v2_status_block()`` instead of calling the readers
    directly.

    ``build_audio_runtime_plan_from_system`` is faked out: it re-reads the
    same grouping.env for an unrelated fact (the audio-runtime route mode)
    via its own un-memoized call in ``audio_runtime_camilla.py`` — a real
    gap, but a different file's fix, so it is isolated here rather than
    inflating this guard's count.
    """
    import jasper.multiroom.config as mr_config
    import jasper.web.correction_crossover_v2_status as crossover_status
    from jasper.cli import doctor
    from jasper.cli.doctor import _cli, _harness

    load_config_calls: list[None] = []
    real_load_config = mr_config.load_config

    def counting_load_config(*args, **kwargs):
        load_config_calls.append(None)
        return real_load_config(*args, **kwargs)

    status_block_calls: list[None] = []
    real_status_block = crossover_status.crossover_v2_status_block

    def counting_status_block():
        status_block_calls.append(None)
        return real_status_block()

    monkeypatch.setattr(mr_config, "load_config", counting_load_config)
    monkeypatch.setattr(
        crossover_status, "crossover_v2_status_block", counting_status_block,
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.build_audio_runtime_plan_from_system",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(_harness, "read_install_profile", lambda: install_profile)

    cfg = (
        _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyTest")
        if install_profile == "full"
        else _cli._doctor_config_from_env("streambox")
    )

    results = asyncio.run(doctor.run_async(cfg))

    assert results, "registry is empty — nothing ran"
    assert len(load_config_calls) <= 1
    assert len(status_block_calls) <= 1


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
            unit: {
                "unit": unit,
                "active_state": "active",
                "load_state": "not-found" if unit == "ghost.service" else "loaded",
            }
            for unit in units
        }

    monkeypatch.setattr(_evidence, "read_unit_states", fake_read)
    ev = Evidence()
    assert ev.unit_active("jasper-fanin.service") is True
    assert ev.unit_active("jasper-outputd.service") is True
    assert ev.unit_state("ghost.service")["load_state"] == "not-found"
    assert ev.unit_state("ghost.service")["load_state"] == "not-found"
    assert calls == [service_units.DOCTOR_UNIT_ROSTER, ("ghost.service",)]


@pytest.mark.parametrize(
    "stdout, expected_keys",
    [
        # systemctl ran and answered nothing (no D-Bus, a host not booted with
        # systemd): unknown, not "none of these units exist".
        ("", None),
        ("Id=jasper-fanin.service\nLoadState=not-found\n", {"jasper-fanin.service"}),
    ],
    ids=["empty-body", "a-real-not-found-record"],
)
def test_read_unit_states_separates_no_answer_from_a_not_found_answer(
    monkeypatch, stdout, expected_keys
):
    monkeypatch.setattr(
        service_units.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )

    states = service_units.read_unit_states(("jasper-fanin.service",))

    assert (states if states is None else set(states)) == expected_keys


def test_a_reply_carrying_no_record_for_the_unit_is_unknown_not_not_found(
    monkeypatch,
):
    """A name systemd answers about at all comes back with its own
    ``not-found`` record. A reply with no record under the asked name (an
    alias whose canonical Id differs, a body that did not parse) is UNKNOWN —
    fabricating ``not-found`` there fails a running unit as "not installed"."""
    monkeypatch.setattr(
        _evidence, "read_unit_states", lambda units, *, timeout: {"other.service": {}},
    )

    assert Evidence().unit_state("ghost.service") is None


def test_unit_state_is_none_without_systemctl(monkeypatch):
    monkeypatch.setattr(_evidence, "read_unit_states", lambda units, *, timeout: None)
    ev = Evidence()
    assert ev.unit_states() is None
    assert ev.unit_state("jasper-fanin.service") is None
    assert ev.unit_active("jasper-fanin.service") is None


def test_unit_property_batches_and_memoizes(monkeypatch):
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_show(prop, units):
        calls.append((prop, tuple(units)))
        return [f"{prop}:{u}" for u in units]

    monkeypatch.setattr(_evidence, "_systemctl_show_property", fake_show)
    ev = Evidence()
    units = ("jasper-voice", "jasper-mux")
    expected = ["OOMScoreAdjust:jasper-voice", "OOMScoreAdjust:jasper-mux"]
    assert ev.unit_property("OOMScoreAdjust", units) == expected
    assert ev.unit_property("OOMScoreAdjust", units) == expected
    assert calls == [("OOMScoreAdjust", units)]


def test_unit_property_is_none_when_the_reply_shape_mismatches(monkeypatch):
    monkeypatch.setattr(
        _evidence, "_systemctl_show_property", lambda prop, units: None,
    )
    ev = Evidence()
    assert ev.unit_property("StartLimitAction", ("jasper-voice",)) is None


@pytest.mark.parametrize(
    "stdout,units,expected",
    [
        ("User=root\n", ["a"], ["root"]),
        ("User=\n", ["a"], [""]),
        ("User=root\n\nUser=jasper\n", ["a", "b"], ["root", "jasper"]),
        # A unit whose value is empty still emits `<prop>=`, so it keeps its
        # slot whether it is first, in the middle, or last.
        (
            "User=root\n\nUser=\n\nUser=jasper\n",
            ["a", "b", "c"],
            ["root", "", "jasper"],
        ),
        (
            "User=root\n\nUser=jasper\n\nUser=\n",
            ["a", "b", "c"],
            ["root", "jasper", ""],
        ),
        ("User=\n\nUser=\n\nUser=\n", ["a", "b", "c"], ["", "", ""]),
    ],
)
def test_systemctl_show_property_yields_one_value_per_unit(
    monkeypatch, stdout, units, expected,
):
    monkeypatch.setattr(
        _evidence, "_run", lambda cmd, timeout=5.0: SimpleNamespace(stdout=stdout),
    )
    result = _evidence._systemctl_show_property("User", units)
    assert result == expected


def test_systemctl_show_property_is_none_when_blocks_do_not_cover_the_units(
    monkeypatch,
):
    monkeypatch.setattr(
        _evidence, "_run",
        lambda cmd, timeout=5.0: SimpleNamespace(stdout="User=root\n"),
    )
    assert _evidence._systemctl_show_property("User", ["a", "b"]) is None


def test_systemctl_show_property_is_none_without_systemctl(monkeypatch):
    def raises(cmd, timeout=5.0):
        raise FileNotFoundError("systemctl not found")

    monkeypatch.setattr(_evidence, "_run", raises)
    assert _evidence._systemctl_show_property("MainPID", ["unit-a"]) is None


def test_status_read_retries_once_when_the_socket_refuses(monkeypatch):
    attempts: list[str] = []

    def reader(path, *, timeout):
        attempts.append(path)
        if len(attempts) == 1:
            raise ConnectionRefusedError(path)
        return {"ok": True}

    monkeypatch.setattr(_evidence, "read_status_socket", reader)
    read = Evidence().fanin_status()
    assert read.payload == {"ok": True}
    assert len(attempts) == 2


def test_status_read_gives_up_after_a_second_refusal(monkeypatch):
    attempts: list[str] = []

    def reader(path, *, timeout):
        attempts.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(_evidence, "read_status_socket", reader)
    read = Evidence().outputd_status()
    assert read.payload is None
    assert read.unreachable is True
    assert len(attempts) == 2


def _literal_unit_arguments() -> dict[str, set[str]]:
    """Every string-literal unit name a doctor module passes to
    ``unit_state``/``unit_active``, keyed by module file name."""
    found: dict[str, set[str]] = {}
    for path in sorted(Path(_evidence.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                continue
            if name not in ("unit_state", "unit_active"):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.setdefault(path.name, set()).add(arg.value)
    return found


def test_every_literal_unit_the_doctor_asks_about_is_rostered():
    """A unit named by a literal costs a second ``systemctl show`` unless it
    rides the roster batch. Names built at runtime are exempt."""
    roster = set(service_units.DOCTOR_UNIT_ROSTER)
    off_roster = {
        module: sorted(units - roster)
        for module, units in _literal_unit_arguments().items()
        if units - roster
    }
    assert off_roster == {}


def test_control_state_wraps_the_control_client(monkeypatch):
    import jasper.control.client as control

    monkeypatch.setattr(control, "get_state", lambda **kw: {"resilience": {}})
    ev = Evidence()
    read = ev.control_state()
    assert read.payload == {"resilience": {}}
    assert read.error is None


def test_control_state_is_fail_soft_on_transport_error(monkeypatch):
    import jasper.control.client as control

    def raises(**kw):
        raise control.ControlError("connection refused")

    monkeypatch.setattr(control, "get_state", raises)
    ev = Evidence()
    read = ev.control_state()
    assert read.payload is None
    assert isinstance(read.error, control.ControlError)


def test_control_system_snapshot_wraps_the_control_client(monkeypatch):
    import jasper.control.client as control

    monkeypatch.setattr(
        control, "get_system_snapshot", lambda **kw: {"metrics": {"current": {}}},
    )
    ev = Evidence()
    read = ev.control_system_snapshot()
    assert read.payload == {"metrics": {"current": {}}}
    assert read.error is None


def test_control_system_snapshot_is_fail_soft_on_transport_error(monkeypatch):
    import jasper.control.client as control

    def raises(**kw):
        raise control.ControlError("connection refused")

    monkeypatch.setattr(control, "get_system_snapshot", raises)
    ev = Evidence()
    read = ev.control_system_snapshot()
    assert read.payload is None
    assert isinstance(read.error, control.ControlError)


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
