# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavioral fidelity pins for the sound-page structured-event migration."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from collections import Counter
from pathlib import Path

from jasper.web import sound_setup


def _sound_event_calls() -> list[ast.Call]:
    source = Path(sound_setup.__file__).read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "log_event"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and node.args[1].value.startswith("sound.")
    ]
    return sorted(calls, key=lambda node: node.lineno)


def test_sound_setup_migrates_the_complete_event_vocabulary():
    calls = _sound_event_calls()

    # 96 / 41. The topology transaction contributes one INFO completion event
    # under the existing sound.output_topology_reset name, and the same-shape
    # composite re-pin (#2814) adds the 41st name, sound.output_topology_repin,
    # emitted twice: an INFO completion and the dispatcher's ERROR branch, the
    # same pair the reset already has. The save path keeps its failure-only
    # sound.output_topology_save_reconcile WARNING; moving broker mechanics
    # into output_topology_runtime must not silently delete the
    # household-facing event contract.
    #
    # One additional event is delegated rather than emitted here: the shared
    # summed-test rollback owner receives sound.active_speaker_summed_test by
    # name. The separate assertion below keeps that handoff in this contract.
    assert len(calls) == 96
    assert len({call.args[1].value for call in calls}) == 41

    # The delegated half of the vocabulary: an event this file no longer emits
    # itself but still NAMES, handed to the shared owner. Without this the
    # walker above would silently stop covering it.
    delegated = {
        keyword.value.value
        for node in ast.walk(ast.parse(Path(sound_setup.__file__).read_text()))
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "log_event_name"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    }
    assert "sound.active_speaker_summed_test" in delegated

    levels: Counter[str] = Counter()
    for call in calls:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        level = keywords.get("level")
        if level is None:
            levels["INFO"] += 1
            assert "exc_info" not in keywords
            continue
        assert isinstance(level, ast.Attribute)
        levels[level.attr] += 1
        if level.attr == "ERROR":
            exc_info = keywords.get("exc_info")
            assert isinstance(exc_info, ast.Constant)
            assert exc_info.value is True
        else:
            assert "exc_info" not in keywords

    # The reset and re-pin completions are the INFO calls of the topology
    # transaction; each also owns one ERROR branch in the POST dispatcher.
    # The warning count stays fixed.
    assert levels == {"INFO": 56, "WARNING": 11, "ERROR": 29}


def test_every_bool_or_optional_percent_s_field_is_prerendered_as_text():
    """Pin all 125 affected parent `%s` positions, not hand-picked examples.

    This includes the topology transaction wrappers and #2603's
    ``safety_profile_evaluation`` design-draft field.
    """
    wrapped_fields: list[str] = []
    for call in _sound_event_calls():
        event = call.args[1].value
        for keyword in call.keywords:
            value = keyword.value
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "str"
            ):
                continue
            assert len(value.args) == 1
            assert not value.keywords
            wrapped_fields.append(f"{event}:{keyword.arg}")

    # The transaction lifecycle adds fifteen required wrappers: three stopped
    # audio-session statuses on save, hardware/cleanup/reconcile plus three
    # stopped-session statuses on reset, reconcile plus the same three
    # stopped-session statuses on the composite re-pin (#2814), and that same
    # change's `park_needed`/`parked` pair on the channel-identity write. Those
    # two are the one event that says whether a household action silenced the
    # speaker AND whether the silence actually landed, so both stay greppable.
    # The digest catches a missed, swapped, or newly invented wrapper without
    # checking in the full tuple. The combined test adds two more: the
    # `play_budget_s` it honoured (a float or None) and the `stop_reason` the
    # play ended on, which together say why one run was captured and another
    # was not.
    signature = "\n".join(wrapped_fields).encode()
    assert len(wrapped_fields) == 125
    assert hashlib.sha256(signature).hexdigest() == (
        "08ed98ecd464607da40d39bab5571d6e0db8e552566c26a0c3532503ed30d5bc"
    )


def test_live_draft_warning_quotes_free_text_and_preserves_format(
    monkeypatch,
    caplog,
):
    monkeypatch.delenv("JASPER_LOG_JSON", raising=False)
    monkeypatch.setattr(sound_setup.time, "monotonic", lambda: 100.0)
    sound_setup._live_draft_unavailable_log_at.clear()

    with caplog.at_level(logging.WARNING, logger=sound_setup.__name__):
        sound_setup._log_live_draft_unavailable(
            reason='unsafe reason=x "quoted"',
            output_trim_db=2.25,
            room_peq_count=3,
            sound_filter_count=4,
            error=ValueError('bad "thing"'),
        )

    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "event=sound.live_draft result=unavailable "
        'reason="unsafe reason=x \\"quoted\\"" output_trim=2.2 '
        "room_peqs=3 sound_filters=4 "
        'err="ValueError(\'bad \\"thing\\"\')"'
    )
    assert record.exc_info is None


def test_volume_floor_exception_keeps_error_level_and_traceback(
    tmp_path,
    monkeypatch,
    caplog,
):
    def _raise_oserror(*_args, **_kwargs):
        raise OSError("synthetic aplay failure")

    monkeypatch.delenv("JASPER_LOG_JSON", raising=False)
    monkeypatch.setattr(sound_setup.subprocess, "Popen", _raise_oserror)
    runner = sound_setup._LoopingVolumeFloorTone(tmp_path / "tone.wav")

    with caplog.at_level(logging.ERROR, logger=sound_setup.__name__):
        runner._run()

    records = [
        record
        for record in caplog.records
        if record.getMessage()
        == "event=sound.volume_floor_tone action=play result=error"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is not None
    assert records[0].exc_info[0] is OSError
    assert str(records[0].exc_info[1]) == "synthetic aplay failure"


def test_live_draft_event_uses_json_sink(monkeypatch, caplog):
    monkeypatch.setenv("JASPER_LOG_JSON", "1")
    monkeypatch.setattr(sound_setup.time, "monotonic", lambda: 200.0)
    sound_setup._live_draft_unavailable_log_at.clear()

    with caplog.at_level(logging.WARNING, logger=sound_setup.__name__):
        sound_setup._log_live_draft_unavailable(
            reason="unsafe reason=x",
            output_trim_db=3.25,
            room_peq_count=5,
            sound_filter_count=6,
            error=None,
        )

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload == {
        "event": "sound.live_draft",
        "result": "unavailable",
        "reason": "unsafe reason=x",
        "output_trim": "3.2",
        "room_peqs": 5,
        "sound_filters": 6,
        "err": "None",
    }
    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.records[-1].exc_info is None


def _environment_report():
    return {
        "status": None,
        "load_gate": "ready",
        "blocker_count": 0,
        "safe_playback": {"playback_allowed": False},
    }


def test_optional_and_bool_percent_s_fields_keep_legacy_logfmt(
    monkeypatch,
    caplog,
):
    from jasper.active_speaker import environment

    monkeypatch.delenv("JASPER_LOG_JSON", raising=False)
    monkeypatch.setattr(
        environment,
        "probe_active_speaker_environment",
        lambda **_kwargs: _environment_report(),
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_path_safety_evidence_path",
        lambda: None,
    )

    with caplog.at_level(logging.INFO, logger=sound_setup.__name__):
        sound_setup._active_speaker_environment_payload()

    assert caplog.records[-1].getMessage() == (
        "event=sound.active_speaker_environment status=None load_gate=ready "
        "blockers=0 safe_playback=False"
    )


def test_optional_and_bool_percent_s_fields_keep_legacy_text_in_json(
    monkeypatch,
    caplog,
):
    from jasper.active_speaker import environment

    monkeypatch.setenv("JASPER_LOG_JSON", "1")
    monkeypatch.setattr(
        environment,
        "probe_active_speaker_environment",
        lambda **_kwargs: _environment_report(),
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_path_safety_evidence_path",
        lambda: None,
    )

    with caplog.at_level(logging.INFO, logger=sound_setup.__name__):
        sound_setup._active_speaker_environment_payload()

    assert json.loads(caplog.records[-1].getMessage()) == {
        "event": "sound.active_speaker_environment",
        "status": "None",
        "load_gate": "ready",
        "blockers": 0,
        "safe_playback": "False",
    }
