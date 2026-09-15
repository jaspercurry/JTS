# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the live active-speaker commissioning views."""

from __future__ import annotations

import asyncio

import jasper.web.sound_active_speaker as sound_active_speaker
import jasper.web.sound_setup as sound_setup


def test_commission_state_payload_is_idle_and_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
        str(tmp_path / "commission_load.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE", str(tmp_path / "ramp.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(tmp_path / "safe.json")
    )
    monkeypatch.setattr(
        "jasper.active_speaker.commission_load.build_driver_commission_load_preflight",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("preflight on a read")),
    )
    payload = asyncio.run(
        sound_setup._active_speaker_commission_state_payload(
            camilla_factory=lambda: (_ for _ in ()).throw(
                AssertionError("camilla should not be read while idle")
            )
        )
    )
    assert payload["commission_load"]["status"] == "idle"
    assert payload["ramp"]["confirmed_roles"] == []
    assert payload["ramp"]["pending"] is None
    assert payload["floor"]["status"] == "floor_required"


def test_summed_test_stop_marks_preparing_session(monkeypatch):
    session = {
        "playback_id": "pending-summed-test",
        "process": None,
        "stop_reason": None,
    }
    monkeypatch.setattr(sound_active_speaker, "_SUMMED_TEST_TONE_SESSION", session)
    try:
        payload = sound_active_speaker._active_speaker_stop_summed_test_tone(
            reason="test_stop"
        )
    finally:
        monkeypatch.setattr(sound_active_speaker, "_SUMMED_TEST_TONE_SESSION", None)

    assert payload == {
        "status": "stopping",
        "reason": "test_stop",
        "playback_id": "pending-summed-test",
        "phase": "preparing",
    }
    assert session["stop_reason"] == "test_stop"
