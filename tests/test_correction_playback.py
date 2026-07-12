# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

from jasper.correction import playback


@pytest.mark.asyncio
async def test_cancelled_sweep_kills_and_reaps_aplay(monkeypatch, tmp_path):
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")
    communicate_started = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            communicate_started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(playback.play_sweep(wav_path, timeout_s=30.0))
    await communicate_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
    assert process.waited is True
