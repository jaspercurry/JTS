# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

from jasper.correction import playback

from ._async_wait import wait_signalled


@pytest.mark.asyncio
async def test_cancelled_sweep_kills_and_reaps_aplay(monkeypatch, tmp_path):
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")
    wait_started = asyncio.Event()
    killed = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False
        stderr = None

        def kill(self):
            self.killed = True
            self.returncode = -9
            killed.set()

        async def wait(self):
            wait_started.set()
            await killed.wait()
            self.waited = True
            return self.returncode

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(playback.play_sweep(wav_path, timeout_s=30.0))
    await wait_signalled(wait_started, "process wait() started", producer=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_repeated_cancellation_still_reaps_aplay(monkeypatch, tmp_path):
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")
    first_wait_started = asyncio.Event()
    kill_called = asyncio.Event()
    allow_wait = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False
        stderr = None

        def kill(self):
            self.killed = True
            self.returncode = -9
            kill_called.set()

        async def wait(self):
            self.wait_calls = getattr(self, "wait_calls", 0) + 1
            first_wait_started.set()
            await allow_wait.wait()
            self.waited = True
            return self.returncode

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(playback.play_sweep(wav_path, timeout_s=30.0))
    await wait_signalled(first_wait_started, "process wait() started", producer=task)
    task.cancel()
    await wait_signalled(kill_called, "process.kill() called", producer=task)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    allow_wait.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
    assert process.waited is True
