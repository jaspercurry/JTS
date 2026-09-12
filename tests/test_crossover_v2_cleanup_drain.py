# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The host persists this run's restore fact on every terminal arm."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from jasper.active_speaker import plan_run
from jasper.active_speaker.crossover_v2.capture_source import CaptureStopped
from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
from jasper.web import correction_crossover_v2 as host
from jasper.web.correction_crossover_v2_wired import build_v2_wired_run_and_consume


@pytest.mark.parametrize("failure", [None, CaptureStopped, RuntimeError, asyncio.CancelledError, OSError])
@pytest.mark.parametrize("restore", [None, *SessionVolumeRestoreResult])
async def test_terminal_restore_replaces_the_previous_run(monkeypatch, failure, restore):
    state = {"session_id": "run", "execution": {"volume_restore": "stale"}}
    saved = []
    windows = SimpleNamespace(last_window=None)
    monkeypatch.setattr(host, "load_v2_state", lambda: state)
    monkeypatch.setattr(host, "save_v2_state", lambda value, **kw: saved.append(value["execution"].copy()))
    monkeypatch.setattr(host, "_persist_terminal_failure", lambda *a, **kw: None)
    def persist(*args, **kwargs):
        if failure is OSError:
            raise OSError(28, "disk full")
    monkeypatch.setattr(host, "persist_conductor_state", persist)

    async def execute(*args, **kwargs):
        windows.last_window = SimpleNamespace(restore_result=restore) if restore else None
        if failure is not None and failure is not OSError:
            raise failure()
        return SimpleNamespace(reason="", cancelled=False)
    monkeypatch.setattr(plan_run, "run_plan", execute)
    runner = build_v2_wired_run_and_consume(
        SimpleNamespace(_measure_gain_ceiling_db={}), windows=windows,
        stop_event=threading.Event(), stop_lock=threading.Lock(), ceiling_s=30,
        complete_event=threading.Event(), retake_event=threading.Event(),
        manifest=None, request=None, captures=None, analyze=None, assessor=None, candidate_scopes={},
    )
    if failure:
        with pytest.raises(failure):
            await runner(SimpleNamespace(session_id="run"))
    else:
        await runner(SimpleNamespace(session_id="run"))
    assert saved == [{"volume_restore": restore.value if restore else "failed"}]
