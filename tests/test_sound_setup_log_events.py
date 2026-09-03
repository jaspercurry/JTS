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

from jasper.web import sound_setup, volume_floor_tone


def _sound_event_calls() -> list[ast.Call]:
    """Every ``sound.*`` event the page emits, across the modules it spans.

    The volume-floor tone owns its own module; its events stay inside this
    contract so the split cannot quietly retire them.
    """
    calls: list[ast.Call] = []
    for module in (sound_setup, volume_floor_tone):
        tree = ast.parse(Path(module.__file__).read_text())
        calls.extend(
            sorted(
                (
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "log_event"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                    and node.args[1].value.startswith("sound.")
                ),
                key=lambda node: node.lineno,
            )
        )
    return calls


def _sound_route_failure_calls() -> list[ast.Call]:
    """The route-failure events this file names but no longer renders itself.

    ``send_route_failure`` is the shared owner of "log the failure, answer
    502"; it fixes ``level=logging.ERROR`` and ``exc_info=True`` for every
    caller, so each call site contributes one ERROR event under the name in
    its ``event=`` keyword.
    """
    source = Path(sound_setup.__file__).read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "send_route_failure"
    ]
    return sorted(calls, key=lambda node: node.lineno)


def _route_failure_event_name(call: ast.Call) -> str:
    """The call site's own event name, or ``""`` when a table supplies it."""
    names = [
        keyword.value.value
        for keyword in call.keywords
        if keyword.arg == "event"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ]
    return names[0] if names else ""


def _dispatch_route_events() -> list[str]:
    """Event names the shared read-route dispatch owns.

    ``_GET_JSON_ROUTES`` collapsed the identical per-route try/except blocks
    into one call site, so those names live in a table instead of at a call
    site. They are read from the table for exactly the reason the call sites
    were walked: a route dropped from it is a retired event.
    """
    return [
        element.elts[1].value
        for node in ast.walk(ast.parse(Path(sound_setup.__file__).read_text()))
        if isinstance(node, ast.Dict)
        for element in node.values
        if isinstance(element, ast.Tuple)
        and len(element.elts) == 2
        and isinstance(element.elts[1], ast.Constant)
        and isinstance(element.elts[1].value, str)
        and element.elts[1].value.startswith("sound.")
    ]


def test_sound_setup_migrates_the_complete_event_vocabulary():
    calls = _sound_event_calls()
    route_failures = _sound_route_failure_calls()
    dispatch_events = _dispatch_route_events()
    # The identical read routes share ONE route-failure call site, whose event
    # comes from _GET_JSON_ROUTES; every other site still names its own. Exactly
    # one table-fed site, so a second dynamic one cannot hide behind this.
    named_failures = [call for call in route_failures if _route_failure_event_name(call)]
    assert len(route_failures) - len(named_failures) == 1

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
    #
    # The 97th call is the server-side identity-audition refusal (#2821) — an
    # INFO under the existing sound.active_speaker_commission name, so the
    # distinct-name count is unchanged.
    #
    # The 42nd name is the tuning handoff card's mint (#2883), which adds the
    # usual INFO/route-failure pair.
    #
    # The route-failure half of the vocabulary is emitted by the shared
    # send_route_failure owner rather than rendered here; the totals span both
    # so converging a call site can never quietly retire its event.
    assert len(calls) + len(named_failures) + len(dispatch_events) == 99
    names = {call.args[1].value for call in calls}
    names |= {_route_failure_event_name(call) for call in named_failures}
    names |= set(dispatch_events)
    assert len(names) == 42

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
    levels["ERROR"] += len(named_failures) + len(dispatch_events)
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
    assert levels == {"INFO": 58, "WARNING": 11, "ERROR": 30}


def test_every_bool_or_optional_percent_s_field_is_prerendered_as_text():
    """Pin all 130 affected parent `%s` positions, not hand-picked examples.

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
    # was not. The reset and re-pin completions add a final pair: each gains
    # a `reconcile_converging` field alongside its existing `reconcile_ok`,
    # extending the save path's still-running-past-the-wait-budget distinction
    # (#3094) to the two siblings that share `trigger_reconcile`. The tuning
    # handoff mint (#2883) adds three: its status, its optional not-ready
    # reason, and the declaration revision the prompt was bound to.
    signature = "\n".join(wrapped_fields).encode()
    assert len(wrapped_fields) == 130
    assert hashlib.sha256(signature).hexdigest() == (
        "76935a3d2040d4bae94526f39bae7b5f020be28c20c2ed61ed8757679bb7bd82"
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
    monkeypatch.setattr(volume_floor_tone.subprocess, "Popen", _raise_oserror)
    runner = volume_floor_tone._LoopingVolumeFloorTone(tmp_path / "tone.wav")

    with caplog.at_level(logging.ERROR, logger=volume_floor_tone.__name__):
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
