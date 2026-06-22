# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-speaker commissioning ↔ measurement mutual exclusion.

Pins the cooperative serialization that keeps the active-crossover commission
flow from running at the same time as room correction / pair balance / pair
sync (all measure through the production graph).
"""
from __future__ import annotations

import json
import time

from jasper.web import active_speaker_flow


def _write_safe_state(path, status: str, *, expires_in_s: float | None = None) -> None:
    state: dict = {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_safe_playback",
        "status": status,
    }
    if expires_in_s is not None:
        state["expires_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in_s)
        )
    path.write_text(json.dumps(state), encoding="utf-8")


def test_active_phase_commissioning_when_armed(tmp_path, monkeypatch):
    state_path = tmp_path / "safe_playback.json"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(state_path))

    # No file yet -> not commissioning (fail open).
    assert active_speaker_flow.active_phase() is None

    _write_safe_state(state_path, "armed", expires_in_s=120)
    assert active_speaker_flow.active_phase() == "commissioning"


def test_active_phase_self_heals_on_expiry(tmp_path, monkeypatch):
    state_path = tmp_path / "safe_playback.json"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(state_path))
    # Armed but past its TTL -> load reports "expired", so the exclusion lifts.
    _write_safe_state(state_path, "armed", expires_in_s=-1)
    assert active_speaker_flow.active_phase() is None


def test_active_phase_idle_or_stopped_is_none(tmp_path, monkeypatch):
    state_path = tmp_path / "safe_playback.json"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(state_path))
    _write_safe_state(state_path, "idle")
    assert active_speaker_flow.active_phase() is None
    _write_safe_state(state_path, "stopped")
    assert active_speaker_flow.active_phase() is None


def test_blocking_measurement_phase_reports_each_flow(monkeypatch):
    from jasper.web import balance_flow, correction_setup, sync_flow

    # Nothing active -> None.
    monkeypatch.setattr(balance_flow, "active_phase", lambda: None)
    monkeypatch.setattr(sync_flow, "active_phase", lambda: None)
    monkeypatch.setattr(correction_setup, "active_correction_phase", lambda: None)
    assert active_speaker_flow.blocking_measurement_phase() is None

    monkeypatch.setattr(correction_setup, "active_correction_phase", lambda: "sweeping")
    assert active_speaker_flow.blocking_measurement_phase() == "correction:sweeping"

    monkeypatch.setattr(sync_flow, "active_phase", lambda: "measuring")
    assert active_speaker_flow.blocking_measurement_phase() == "sync:measuring"

    monkeypatch.setattr(balance_flow, "active_phase", lambda: "measuring")
    # Balance is checked first.
    assert active_speaker_flow.blocking_measurement_phase() == "balance:measuring"


def test_correction_reserve_slot_blocked_by_commissioning(tmp_path, monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(active_speaker_flow, "active_phase", lambda: "commissioning")
    # _reserve_start_slot must refuse a correction /start while commissioning.
    blocking = correction_setup._reserve_start_slot()
    assert blocking == "active_speaker:commissioning"
