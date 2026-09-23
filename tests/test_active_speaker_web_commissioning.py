# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free guards for secure active-speaker web measurement orchestration."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)


def test_automatic_measurement_source_peak_is_one_shared_default():
    from jasper.active_speaker import driver_acoustics
    from jasper.audio_measurement.sweep import synchronized_swept_sine

    sweep_default = inspect.signature(synchronized_swept_sine).parameters[
        "amplitude_dbfs"
    ].default
    assert AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS == -12.0
    assert sweep_default == AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
    assert driver_acoustics.DEFAULT_AMPLITUDE_DBFS == sweep_default


def test_resilient_restore_does_not_retry_cancelled_child(monkeypatch):
    # The wait now lives in restore_wait, so a caller that only needs to put a
    # graph back does not import this module's commissioning stack; this module
    # still consumes it as `_resilient`.
    from jasper.active_speaker import restore_wait

    shield_calls = 0

    async def fake_shield(_task):
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls > 1:
            raise AssertionError("cancelled cleanup task was retried")
        raise asyncio.CancelledError

    class CancelledTask:
        def cancelled(self):
            return True

    monkeypatch.setattr(restore_wait.asyncio, "shield", fake_shield)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(restore_wait.await_restore_task_resilient(CancelledTask()))
    assert shield_calls == 1
