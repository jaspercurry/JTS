# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
import sys
import types

from jasper.cli import aec_init
from jasper.mics import xvf3800


class _Closeable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeXvfDevice:
    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        readback_overrides: dict[str, list[int | float]] | None = None,
    ) -> None:
        self.dev = _Closeable()
        self.fail_on = fail_on or set()
        self.readback_overrides = readback_overrides or {}
        self.values: dict[str, tuple[int | float, ...]] = {"VERSION": (2, 0, 8)}
        self.writes: list[tuple[str, list[int | float]]] = []

    def write(self, name: str, values: list[int | float]) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"{name} failed")
        self.writes.append((name, list(values)))
        self.values[name] = tuple(self.readback_overrides.get(name, values))

    def read(self, name: str) -> tuple[int | float, ...]:
        return self.values[name]


def _install_fake_xvf(monkeypatch, dev: _FakeXvfDevice) -> None:
    fake_xvf = types.ModuleType("jasper.xvf")
    fake_xvf.xvf_host = types.SimpleNamespace(
        find=lambda: dev,
        XvfControlError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "jasper.xvf", fake_xvf)


def _stub_amixer(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stderr="")

    monkeypatch.setattr(aec_init.subprocess, "run", fake_run)
    return calls


def _write_map(dev: _FakeXvfDevice) -> dict[str, list[int | float]]:
    return {name: values for name, values in dev.writes}


def test_production_profile_restores_corpus_mutated_chip_state(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    _stub_amixer(monkeypatch)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.delenv("JASPER_AEC_CHIP_AEC_ENABLED", raising=False)

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [1]
    assert writes["AEC_ASROUTONOFF"] == [0]
    assert writes["AEC_FIXEDBEAMSONOFF"] == [0]
    assert writes["AEC_FIXEDBEAMSGATING"] == [0]
    assert writes["AEC_AECEMPHASISONOFF"] == [0]
    assert writes["AEC_FAR_EXTGAIN"] == [0.0]
    assert writes["AUDIO_MGR_OP_L"] == [8, 0]
    assert writes["AUDIO_MGR_OP_R"] == [8, 0]
    production_bridge_route = {
        0: writes["AUDIO_MGR_OP_L"],
        1: writes["AUDIO_MGR_OP_R"],
    }[xvf3800.MIC_CHANNEL_INDEX]
    assert production_bridge_route[0] != 0
    assert dev.dev.closed is True


def test_corpus_profile_applies_and_verifies_expected_chip_routes(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    _stub_amixer(monkeypatch)
    monkeypatch.setenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_CORPUS_CHIP_SYS_DELAY", "233")

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [0]
    assert writes["AUDIO_MGR_SYS_DELAY"] == [233]
    assert writes["AEC_ASROUTONOFF"] == [1]
    assert writes["AEC_FIXEDBEAMSONOFF"] == [1]
    assert writes["AEC_FIXEDBEAMSGATING"] == [1]
    assert writes["AUDIO_MGR_OP_L"] == [7, 0]
    assert writes["AUDIO_MGR_OP_R"] == [7, 1]
    assert dev.dev.closed is True


def test_production_chip_profile_uses_chip_flag_and_delay(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    _stub_amixer(monkeypatch)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_CHIP_SYS_DELAY", "12")

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [0]
    assert writes["AUDIO_MGR_SYS_DELAY"] == [12]
    assert writes["AEC_ASROUTONOFF"] == [1]
    assert writes["AEC_FIXEDBEAMSONOFF"] == [1]
    assert writes["AEC_FIXEDBEAMSGATING"] == [1]
    assert writes["AUDIO_MGR_OP_L"] == [7, 0]
    assert writes["AUDIO_MGR_OP_R"] == [7, 1]
    assert dev.dev.closed is True


def test_chip_profile_refuses_linear_geometry_without_beam_plan(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    amixer_calls = _stub_amixer(monkeypatch)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    monkeypatch.setenv("JASPER_XVF_GEOMETRY", "linear")
    monkeypatch.delenv("JASPER_XVF_CHIP_BEAM_PLAN", raising=False)

    assert aec_init.main() == 1
    assert "AEC_FIXEDBEAMSAZIMUTH_VALUES" not in _write_map(dev)
    assert amixer_calls == []
    assert dev.dev.closed is True


def test_corpus_profile_fails_when_critical_write_fails(monkeypatch) -> None:
    dev = _FakeXvfDevice(fail_on={"AUDIO_MGR_OP_L"})
    _install_fake_xvf(monkeypatch, dev)
    amixer_calls = _stub_amixer(monkeypatch)
    monkeypatch.setenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "1")

    assert aec_init.main() == 1
    assert "AEC_HPFONOFF" not in _write_map(dev)
    assert amixer_calls == []
    assert dev.dev.closed is True


def test_corpus_profile_fails_when_readback_does_not_match(monkeypatch) -> None:
    dev = _FakeXvfDevice(readback_overrides={"AUDIO_MGR_OP_R": [0, 0]})
    _install_fake_xvf(monkeypatch, dev)
    amixer_calls = _stub_amixer(monkeypatch)
    monkeypatch.setenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "1")

    assert aec_init.main() == 1
    assert "AEC_HPFONOFF" not in _write_map(dev)
    assert amixer_calls == []
    assert dev.dev.closed is True


def test_init_reports_missing_xvf_control_dependency(monkeypatch, caplog) -> None:
    class MissingXvfControlError(RuntimeError):
        pass

    def fail_find():
        raise MissingXvfControlError("XVF3800 USB control dependencies missing")

    fake_xvf = types.ModuleType("jasper.xvf")
    fake_xvf.xvf_host = types.SimpleNamespace(
        find=fail_find,
        XvfControlError=MissingXvfControlError,
    )
    monkeypatch.setitem(sys.modules, "jasper.xvf", fake_xvf)
    caplog.set_level("ERROR", logger="jasper.aec_init")

    assert aec_init.main() == 1

    assert "event=xvf_control_unavailable" in caplog.text
    assert "dependencies missing" in caplog.text
