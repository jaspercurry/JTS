# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Sound-page event fields, levels, and exception details."""

from __future__ import annotations

import json
import logging

import pytest

from jasper.active_speaker import environment
from jasper.web import sound_active_speaker, sound_profile_apply, volume_floor_tone

from ._log_events import parse_event

pytestmark = pytest.mark.parametrize("json_logs", [False, True])


@pytest.mark.parametrize("error", [None, ValueError('bad "thing"')])
def test_live_draft_warning_preserves_fields(monkeypatch, caplog, json_logs, error):
    monkeypatch.setenv("JASPER_LOG_JSON", str(int(json_logs)))
    monkeypatch.setattr(sound_profile_apply.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(sound_profile_apply, "_live_draft_unavailable_log_at", {})
    reason = 'unsafe reason=x "quoted"'

    with caplog.at_level(logging.WARNING, logger=sound_profile_apply.__name__):
        sound_profile_apply._log_live_draft_unavailable(
            reason=reason,
            output_trim_db=3.25,
            room_peq_count=5,
            sound_filter_count=6,
            error=error,
        )

    (record,) = caplog.records
    expected = {
        "result": "unavailable",
        "reason": reason,
        "output_trim": "3.2",
        "room_peqs": 5,
        "sound_filters": 6,
        "err": repr(error),
    }
    if json_logs:
        assert json.loads(record.getMessage()) == {"event": "sound.live_draft", **expected}
    else:
        assert parse_event(record.getMessage()) == (
            "sound.live_draft", {key: str(value) for key, value in expected.items()},
        )
    assert record.levelno == logging.WARNING
    assert record.exc_info is None


def test_volume_floor_exception_keeps_error_level_and_traceback(
    tmp_path, monkeypatch, caplog, json_logs,
):
    error = OSError("synthetic aplay failure")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setenv("JASPER_LOG_JSON", str(int(json_logs)))
    monkeypatch.setattr(volume_floor_tone.subprocess, "Popen", fail)
    runner = volume_floor_tone._LoopingVolumeFloorTone(tmp_path / "tone.wav")

    with caplog.at_level(logging.ERROR, logger=volume_floor_tone.__name__):
        runner._run()

    (record,) = caplog.records
    expected = {"action": "play", "result": "error"}
    if json_logs:
        assert json.loads(record.getMessage()) == {
            "event": "sound.volume_floor_tone", **expected,
        }
    else:
        assert parse_event(record.getMessage()) == ("sound.volume_floor_tone", expected)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[0] is OSError
    assert record.exc_info[1] is error


@pytest.mark.parametrize(("status", "allowed"), [(None, False), ("ready", True)])
def test_environment_event_preserves_optional_and_bool_fields(
    monkeypatch, caplog, json_logs, status, allowed,
):
    report = {
        "status": status,
        "load_gate": "ready",
        "blocker_count": 0,
        "safe_playback": {"playback_allowed": allowed},
    }
    monkeypatch.setenv("JASPER_LOG_JSON", str(int(json_logs)))
    monkeypatch.setattr(
        environment, "probe_active_speaker_environment", lambda **_kwargs: report,
    )
    monkeypatch.setattr(
        sound_active_speaker, "_active_speaker_path_safety_evidence_path", lambda: None,
    )

    with caplog.at_level(logging.INFO, logger=sound_active_speaker.__name__):
        assert sound_active_speaker._active_speaker_environment_payload() == report

    (record,) = caplog.records
    expected = {
        "status": str(status),
        "load_gate": "ready",
        "blockers": 0,
        "safe_playback": str(allowed),
    }
    if json_logs:
        assert json.loads(record.getMessage()) == {
            "event": "sound.active_speaker_environment", **expected,
        }
    else:
        assert parse_event(record.getMessage()) == (
            "sound.active_speaker_environment",
            {key: str(value) for key, value in expected.items()},
        )
    assert record.levelno == logging.INFO
    assert record.exc_info is None
