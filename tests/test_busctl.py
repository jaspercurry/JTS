# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from jasper import busctl


class _HungProcess:
    returncode = None

    def __init__(self) -> None:
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return b"", b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return -9


async def test_external_cancellation_kills_and_reaps_child(monkeypatch) -> None:
    process = _HungProcess()

    async def fake_spawn(*args: object, **kwargs: object) -> _HungProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    task = asyncio.create_task(busctl.run_busctl("tree", "org.example"))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.waited is True


class _FakeProcess:
    def __init__(
        self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.mark.parametrize(
    ("signature", "value"),
    [
        ("q", "64"),
        ("n", "-64"),  # a dash-prefixed value must not be parsed as an option
    ],
)
async def test_set_property_argv_carries_system_bus_signature_and_double_dash(
    monkeypatch, signature, value,
) -> None:
    """Pin for #4806: the Bluetooth volume write's exact shape (bus,
    verb, signature, and the `--` guard before the typed value, even
    when the value itself starts with `-`)."""
    captured: list[tuple[object, ...]] = []

    async def fake_spawn(*args: object, **kwargs: object) -> _FakeProcess:
        captured.append(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    ok = await busctl.set_property(
        "org.bluealsa", "/org/bluealsa/hci0/dev_X/a2dpsnk/source",
        "org.bluez.MediaTransport1", "Volume", signature, value,
    )

    assert ok is True
    assert captured == [(
        "busctl", "--system", "set-property",
        "org.bluealsa", "/org/bluealsa/hci0/dev_X/a2dpsnk/source",
        "org.bluez.MediaTransport1", "Volume", signature, "--", value,
    )]


async def test_get_property_returns_none_on_nonzero_exit(monkeypatch) -> None:
    async def fake_spawn(*args: object, **kwargs: object) -> _FakeProcess:
        return _FakeProcess(returncode=1, stderr=b"unknown object")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    assert await busctl.get_property(
        "org.example", "/path", "org.example.Iface", "Volume",
    ) is None
