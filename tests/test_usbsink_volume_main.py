# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle coverage for the systemd USB-volume helper entry point."""

from __future__ import annotations

import asyncio
import signal

from jasper.cli import usbsink_volume_main


def test_run_returns_when_bridge_finishes(monkeypatch):
    calls = []

    class Bridge:
        def __init__(self, *, card_name, control_url):
            calls.append((card_name, control_url))

        async def run(self):
            return None

    monkeypatch.setenv("JASPER_USBSINK_MIXER_CARD", "TestCard")
    monkeypatch.setenv("JASPER_USBSINK_CONTROL_URL", "http://test/control")
    monkeypatch.setattr(usbsink_volume_main, "VolumeBridge", Bridge)

    assert asyncio.run(usbsink_volume_main._run()) == 0
    assert calls == [("TestCard", "http://test/control")]


def test_run_signal_path_cancels_bridge(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Bridge:
        def __init__(self, **_kwargs):
            pass

        async def run(self):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def scenario():
        loop = asyncio.get_running_loop()
        handlers = {}
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, callback: handlers.setdefault(sig, callback),
        )
        monkeypatch.setattr(usbsink_volume_main, "VolumeBridge", Bridge)
        task = asyncio.create_task(usbsink_volume_main._run())
        await started.wait()
        assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
        handlers[signal.SIGTERM]()
        assert await task == 0
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_main_configures_logging_and_handles_keyboard_interrupt(monkeypatch):
    configured = []
    monkeypatch.setenv("JASPER_USBSINK_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(
        usbsink_volume_main.logging,
        "basicConfig",
        lambda **kwargs: configured.append(kwargs),
    )
    def interrupt(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(usbsink_volume_main.asyncio, "run", interrupt)

    assert usbsink_volume_main.main([]) == 0
    assert configured[0]["level"] == "DEBUG"
