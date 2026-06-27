# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""bluetooth.avrcp — bluealsa-cli probe goes through the shared backoff.

bluetooth_active_device_path runs in jasper-mux on every BT transport
command. It must reuse jasper.bluealsa_probe so a D-Bus permission denial
backs off process-wide instead of hammering the system bus once per
command. These tests fail if the helper reverts to its own raw
`bluealsa-cli list-pcms` subprocess.
"""
from __future__ import annotations

import pytest

from jasper import bluealsa_probe
from jasper.bluetooth import avrcp


@pytest.fixture(autouse=True)
def _reset_bluealsa_probe_state():
    bluealsa_probe._reset_for_tests()
    yield
    bluealsa_probe._reset_for_tests()


async def test_active_device_path_translates_bluealsa_to_bluez(monkeypatch):
    line = (
        b"/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source PCM ...\n"
    )

    class _Proc:
        returncode = 0

        async def communicate(self):
            return line, b""

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await avrcp.bluetooth_active_device_path() == (
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    )


async def test_active_device_path_none_when_no_a2dp_sink(monkeypatch):
    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"/org/bluealsa/hci0/dev_AA/a2dpsrc/sink PCM ...\n", b""

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await avrcp.bluetooth_active_device_path() is None


async def test_active_device_path_none_on_cli_failure(monkeypatch):
    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"permission denied"

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await avrcp.bluetooth_active_device_path() is None


async def test_active_device_path_suppresses_after_cli_failure(monkeypatch):
    """A rejection (rc!=0) must trip the shared backoff so the second
    probe is short-circuited and does NOT spawn a subprocess. Only true
    if the helper routes through bluealsa_probe.list_pcms."""
    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"permission denied"

    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await avrcp.bluetooth_active_device_path() is None
    assert await avrcp.bluetooth_active_device_path() is None
    assert calls["n"] == 1


async def test_active_device_path_shares_backoff_with_other_probes(monkeypatch):
    """The backoff is process-wide: a failure recorded by any
    bluealsa_probe consumer suppresses this helper's next probe without
    spawning. Pins the 'shared module', not a per-caller, contract."""
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("should not spawn while suppressed")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    # Pre-trip the shared backoff as if another consumer just failed.
    bluealsa_probe.note_probe_failure("rc=1", avrcp.logger)

    assert await avrcp.bluetooth_active_device_path() is None
    assert calls["n"] == 0


async def test_active_device_path_handles_spawn_oserror(monkeypatch):
    """avrcp keeps its never-raise contract: a spawn-time OSError that
    bluealsa_probe.list_pcms does not swallow (it only catches
    FileNotFoundError/timeout) is caught locally and returns None."""
    async def fake_exec(*args, **kwargs):
        raise OSError("EMFILE: too many open files")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await avrcp.bluetooth_active_device_path() is None
