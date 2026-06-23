# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free guards for secure active-speaker web measurement orchestration."""

from __future__ import annotations

import asyncio

from jasper.active_speaker import web_commissioning as web


def test_driver_capture_sweep_requires_confirmed_driver(monkeypatch):
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: {})

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_required"


def test_driver_capture_sweep_refuses_expired_floor_confirmation(monkeypatch):
    measurements = {
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": {
                    "captured": True,
                    "playback_id": "play-woofer",
                    "test_level_dbfs": -72.0,
                    "issues": [],
                },
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "idle"})
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not load")),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_expired"


def test_summed_capture_sweep_arms_safe_session_for_mutual_exclusion(monkeypatch):
    armed = {}
    measurements = {
        "summary": {
            "latest_summed_tests": {
                "mono": {
                    "captured": True,
                    "audio_emitted": True,
                    "summed_test_id": "sum-1",
                    "tone": {"level_dbfs": -72.0},
                    "issues": [],
                },
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "idle"})
    monkeypatch.setattr(
        web,
        "arm_safe_playback_session",
        lambda report: armed.setdefault("report", report) or {"status": "armed"},
    )
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))

    async def fake_load(**kwargs):
        return {
            "load": {
                "status": "blocked",
                "issues": [{
                    "severity": "blocker",
                    "code": "test_block",
                    "message": "blocked in test",
                }],
            },
        }

    monkeypatch.setattr(web, "_load_summed_commissioning_config", fake_load)

    payload = asyncio.run(
        web.play_summed_capture_sweep(
            {"speaker_group_id": "mono"},
            camilla_factory=lambda: object(),
        )
    )

    assert armed["report"]["status"] == "ready"
    assert payload["status"] == "blocked"
    assert payload["reason"] == "summed_capture_sweep_load_failed"
