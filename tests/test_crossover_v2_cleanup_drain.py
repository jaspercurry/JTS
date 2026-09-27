# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The host persists this run's restore fact on every terminal arm."""
from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

import asyncio
from types import SimpleNamespace

import pytest

from jasper.active_speaker import plan_run
from jasper.active_speaker.crossover_v2.capture_source import CaptureStopped
from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
from jasper.web.correction_crossover_v2_wired import build_v2_wired_run_and_consume
from tests._async_wait import wait_signalled
from tests._lock_holder import spawn_lock_holder
from tests._log_events import event_field_maps


@pytest.mark.parametrize("failure", [None, CaptureStopped, RuntimeError, asyncio.CancelledError, OSError])
@pytest.mark.parametrize("restore", [None, *SessionVolumeRestoreResult])
async def test_terminal_restore_replaces_the_previous_run(monkeypatch, tmp_path, failure, restore):
    state = {"session_id": "run", "execution": {"volume_restore": "stale"}}
    saved = []
    door = SimpleNamespace(isolation=None)
    monkeypatch.setattr(v2state, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(v2state, "load_v2_state", lambda: state)
    monkeypatch.setattr(v2state, "save_v2_state", lambda value, **kw: saved.append(value["execution"].copy()))
    monkeypatch.setattr(v2state, "persist_terminal_failure", lambda *a, **kw: None)
    def persist(*args, **kwargs):
        if failure is OSError:
            raise OSError(28, "disk full")
    monkeypatch.setattr(v2state, "persist_conductor_state", persist)

    async def execute(*args, **kwargs):
        door.isolation = SimpleNamespace(restore_result=restore)
        if failure is not None and failure is not OSError:
            raise failure()
        return SimpleNamespace(reason="", cancelled=False)
    monkeypatch.setattr(plan_run, "run_plan", execute)
    runner = build_v2_wired_run_and_consume(
        SimpleNamespace(measure_gain_ceiling_db={}), door=door,
        signals=plan_run.RunSignals(), ceiling_s=30,
        manifest=None, request=None, captures=None, analyze=None, assessor=None,
    )
    if failure:
        with pytest.raises(failure):
            await runner(SimpleNamespace(session_id="run"))
    else:
        await runner(SimpleNamespace(session_id="run"))
    assert saved == [{"volume_restore": restore.value if restore else "not_opened"}]


async def test_a_cancelled_run_stays_cancelled_when_its_terminal_persists_time_out(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(v2state, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(v2state, "STATE_LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(v2state, "POST_COMMIT_STATE_LOCK_TIMEOUT_S", 0.05)
    started = asyncio.Event()

    async def execute(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(plan_run, "run_plan", execute)
    runner = build_v2_wired_run_and_consume(
        SimpleNamespace(measure_gain_ceiling_db={}), door=SimpleNamespace(isolation=None),
        signals=plan_run.RunSignals(), ceiling_s=30,
        manifest=None, request=None, captures=None, analyze=None, assessor=None,
    )

    with spawn_lock_holder(tmp_path / "state.json", hold_seconds=60):
        run = asyncio.ensure_future(runner(SimpleNamespace(session_id="run")))
        await wait_signalled(started, "the run's plan start", producer=run)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    failed = event_field_maps(caplog, "correction.crossover_v2_terminal_persist_failed")
    assert [(fields["persist"], fields["reason"]) for fields in failed] == [
        ("execution_result", "crossover_v2_state_busy"), ("terminal_failure", "crossover_v2_state_busy"),
    ]
