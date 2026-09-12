# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-round``: the four round verbs, driven from the speaker.

Every request is served by a fake opener -- :class:`WizardClient`'s own
transport seam -- so these pin what the CLI SENDS, what it ANSWERS on stdout
and what it EXITS with, without a wizard, a network or a speaker.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from jasper.active_speaker import wizard_client as wc
from jasper.cli import round as cli
from jasper.cli._refusal import STATUS_BY_CODE
from tests.test_crossover_v2_tuning_scope import tuning_profile as tuning_profile, _room_candidate

_FINGERPRINT = "a" * 64
_OTHER = "b" * 64


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Serves canned bodies by path suffix; records every request it saw.

    ``envelopes`` is a queue consumed by the FIRST status reads, so a wait test
    can age a session across polls before the steady page takes over.
    """

    def __init__(
        self,
        pages: dict[str, str],
        envelopes: list[str] | None = None,
        raises: dict[str, Exception] | None = None,
    ):
        self.pages = pages
        self.envelopes = list(envelopes or [])
        self.raises = dict(raises or {})
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        for path, error in self.raises.items():
            if request.full_url.endswith(path):
                raise error
        if request.full_url.endswith(wc.STATUS_PATH) and self.envelopes:
            return _FakeResponse(self.envelopes.pop(0))
        for path, page in self.pages.items():
            if request.full_url.endswith(path):
                return _FakeResponse(page)
        return _FakeResponse("")

    def posted_to(self, path: str) -> list:
        return [
            request
            for request in self.requests
            if request.data is not None and request.full_url.endswith(path)
        ]

    def posts(self) -> list:
        return [r for r in self.requests if r.data is not None]


def _envelope(**block) -> str:
    return json.dumps({"crossover_v2": block})


def _opener(*, v2=None, envelopes=None, raises=None, **pages) -> _FakeOpener:
    return _FakeOpener(
        {
            wc.CSRF_PAGE_PATH: '<meta name="jts-csrf" content="tok-abcd1234">',
            wc.STATUS_PATH: _envelope(**(v2 or {})),
            wc.SESSION_PATH: pages.get("session", "{}"),
            wc.APPLY_PATH: pages.get("apply", '{"status": "applied"}'),
            cli.REPUBLISH_PATH: pages.get("republish", '{"status": "republished"}'),
        },
        envelopes=envelopes,
        raises=raises,
    )


def _lost(code: int) -> Exception:
    """What a dead daemon (0) and a refusing one (403) raise at the opener."""
    if not code:
        return urllib.error.URLError("[Errno 111] Connection refused")
    return urllib.error.HTTPError(
        "http://127.0.0.1", code, "Forbidden", {}, io.BytesIO(b"nope")
    )


def _run(argv, opener, monkeypatch, capsys):
    """The verb's exit code and the ONE document it answered with on stdout."""
    monkeypatch.setattr(cli, "read_identity", lambda: _Identity())
    monkeypatch.setattr(cli, "speaker_url", lambda path: f"http://jts3.local{path}")
    code = cli.main(list(argv), opener=opener)
    return code, json.loads(capsys.readouterr().out)


class _Identity:
    hostname = "jts3.local"


# --------------------------------------------------------------------------- #
# the vocabulary is the product's own
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# open
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "live",
    [
        {"candidate": {"fingerprint": _OTHER}},
        {"phase": "review"},
        {},
    ],
)
def test_apply_selects_a_banked_fingerprint_before_using_the_full_apply_path(
    live, monkeypatch, capsys
):
    opener = _opener(
        v2={"candidate": {"fingerprint": _FINGERPRINT}},
        envelopes=[_envelope(**live)],
    )
    code, receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_OK
    assert receipt["candidate_fingerprint"] == _FINGERPRINT
    assert [json.loads(request.data) for request in opener.posts()] == [
        {"fingerprint": _FINGERPRINT},
        {"expected_candidate_fingerprint": _FINGERPRINT},
    ]


@pytest.mark.parametrize("outcome", ["refused", "lost", "changed"])
def test_apply_stops_when_the_selected_candidate_cannot_be_published(
    outcome, monkeypatch, capsys,
):
    opener = _opener(
        v2={"candidate": {"fingerprint": _OTHER}},
        republish='{"code":"candidate_trial_required"}' if outcome == "refused" else '{"status":"republished"}',
        raises={cli.REPUBLISH_PATH: _lost(0)} if outcome == "lost" else {},
    )
    code, _receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )
    assert code == (cli.EXIT_UNREADABLE if outcome == "lost" else cli.EXIT_REFUSED)
    assert opener.posted_to(wc.APPLY_PATH) == []


def test_apply_posts_the_named_fingerprint_when_it_is_the_live_one(
    monkeypatch, capsys
):
    opener = _opener(v2={"candidate": {"fingerprint": _FINGERPRINT}})
    code, receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_OK
    assert receipt["candidate_fingerprint"] == _FINGERPRINT
    assert "status" not in receipt
    posted = opener.posted_to(wc.APPLY_PATH)
    assert [json.loads(r.data.decode()) for r in posted] == [
        {"expected_candidate_fingerprint": _FINGERPRINT}
    ]
    assert opener.posted_to(cli.REPUBLISH_PATH) == []


@pytest.mark.parametrize("body", ['{"status": "apply_failed"}', '{"ok": false}'])
def test_an_apply_that_answered_but_did_not_apply_is_a_refusal(
    body, monkeypatch, capsys
):
    """200 alone passes `apply_failed`; only 200 AND `applied` is right."""
    opener = _opener(v2={"candidate": {"fingerprint": _FINGERPRINT}}, apply=body)
    code, receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_REFUSED
    assert receipt["status"] == STATUS_BY_CODE[cli.EXIT_REFUSED]
    assert receipt["reason"] == wc.REASON_NOT_APPLIED
    assert receipt["detail"]["refused_by"] == "wizard"


# --------------------------------------------------------------------------- #
# wait
# --------------------------------------------------------------------------- #


def test_an_apply_whose_answer_is_lost_is_not_a_wizard_refusal(
    monkeypatch, capsys
):
    """Nothing refused: the POST left and no answer came back (#3498)."""
    opener = _opener(
        v2={"candidate": {"fingerprint": _FINGERPRINT}},
        raises={wc.APPLY_PATH: _lost(0)},
    )
    code, receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_UNREADABLE
    assert receipt["status"] == STATUS_BY_CODE[cli.EXIT_UNREADABLE]
    assert receipt["reason"] == wc.REASON_ANSWER_LOST
    assert receipt["detail"]["refused_by"] == ""
    assert receipt["detail"]["http"] == 0


@pytest.fixture
def preflight_ready(monkeypatch):
    from jasper.cli import _run_request
    from tests.test_preflight import ready_facts
    monkeypatch.setattr(_run_request, "read_preflight_facts", ready_facts)


@pytest.mark.parametrize("candidates,shape", [(None, "measure"), ("base", "trial")])
@pytest.mark.parametrize("source", ["flags", "file", "confirmed"])
def test_run_posts_inline_and_returns_without_a_status_read(preflight_ready, monkeypatch, capsys, tmp_path, candidates, shape, source):
    opener = _opener(session=json.dumps({"capture": {"session_id": "run-1", "first_prompt": {"title": "Place mic"}}}))
    argv = ["run", "--program", "room", "--poses", "seat_express"]
    if candidates:
        argv += ["--candidates", candidates]
    if source == "confirmed":
        argv += ["--mover", "confirmed"]
    if source == "file":
        from jasper.cli._run_request import resolve_run
        request = resolve_run(cli.build_parser().parse_args(argv)).plan
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(request.to_dict()))
        argv = ["run", "--plan", str(path)]
    code, body = _run(argv, opener, monkeypatch, capsys)
    assert code == 0
    plan = json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"]
    assert plan["candidates"] == ([] if candidates is None else [candidates])
    assert plan["artifact_schema_version"] == 3
    assert body["run_id"] == "run-1"
    assert body["link"].endswith(wc.CSRF_PAGE_PATH)
    assert body["shape"] == shape
    assert not any(r.full_url.endswith(wc.STATUS_PATH) for r in opener.requests)


@pytest.mark.parametrize("dry_run,ceiling,code", [(True, 80, 0), (True, 90, 1), (False, 90, 1)])
def test_preflight_answers_without_posting(preflight_ready, monkeypatch, capsys, dry_run, ceiling, code):
    opener = _opener()
    argv = ["run", "--ceiling", str(ceiling)] + (["--dry-run"] if dry_run else [])
    actual, body = _run(argv, opener, monkeypatch, capsys)
    assert actual == code
    if dry_run:
        assert body["dry_run"] is True
        assert bool(body["issues"]) == bool(code)
        if code:
            assert body["issues"][0]["code"] == "walk_ceiling_above_stop"
    else:
        assert body["status"] == STATUS_BY_CODE[code]
        assert body["reason"] == "walk_ceiling_above_stop"
    assert not opener.requests


@pytest.mark.parametrize("repeats", [None, 1, 2])
def test_run_repeats_replace_each_pose_count(preflight_ready, monkeypatch, capsys, repeats):
    from collections import Counter
    from jasper.active_speaker.angle_capture import AngleCaptureRequest
    from jasper.active_speaker.measurement_programs import run_program

    opener = _opener(session=json.dumps({"capture": {"session_id": "run-1"}}))
    argv = ["run", "--program", "speaker", "--poses", "baseline_express"]
    if repeats is not None:
        argv += ["--repeats", str(repeats)]
    code, _ = _run(argv, opener, monkeypatch, capsys)
    assert code == 0
    plan = AngleCaptureRequest.from_mapping(json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"])
    assert plan.repeats == 1
    assert Counter(stop.place for stop in plan.stops) == {
        pose.place: pose.repeats if repeats is None else repeats
        for pose in run_program("speaker", "baseline_express").poses
    }


def _run_opener(capture):
    opener = _opener()
    opener.pages[wc.STATUS_PATH] = json.dumps({"crossover_v2": {}, "capture": {"kind": "crossover_v2:session", "session_id": "run-1", **capture}})
    return opener


@pytest.mark.parametrize("joining", [True, False])
def test_placed_releases_the_pending_gate(joining, monkeypatch, capsys):
    from jasper.active_speaker.crossover_v2.position_gate import POSITION_READY_ENDPOINT
    action = {"endpoint": POSITION_READY_ENDPOINT, "body": {"index": 1, "attempt": 1}}
    opener = _run_opener({"status": "awaiting_join" if joining else "running",
                          "join" if joining else "position_pending": {"action": action}})
    opener.pages[POSITION_READY_ENDPOINT] = '{"ok": true}'
    code, body = _run(["placed", "--run", "run-1", "--pose", "1"], opener, monkeypatch, capsys)
    assert code == 0 and body["ok"] is True
    assert json.loads(opener.posted_to(POSITION_READY_ENDPOINT)[0].data) == {"index": 1, "attempt": 1, "run_id": "run-1"}


def test_status_reads_progress_once(monkeypatch, capsys):
    progress = {"pose": 2, "poses": 3, "config": 1, "configs": 2, "attempt": 3,
                "fault": "capture_clipped", "next_action": "retake_quieter", "faults": [{"fault": "capture_clipped"}]}
    opener = _run_opener({"status": "running", "run": progress})
    code, body = _run(["status", "--run", "run-1"], opener, monkeypatch, capsys)
    assert code == 0
    assert [body[key] for key in ("pose", "config", "attempt", "code")] == [2, 1, 3, "capture_clipped"]
    assert body["faults"] == progress["faults"]
    assert len(opener.requests) == 1


@pytest.mark.parametrize("verb", ["status", "placed", "wait"])
def test_named_run_never_reads_or_releases_a_different_run(verb, monkeypatch, capsys):
    opener = _run_opener({"status": "running"})
    code, body = _run([verb, "--run", "old"], opener, monkeypatch, capsys)
    assert code == 1
    assert body["reason"] == "run_not_current"
    assert not opener.posts()


def test_wait_banks_and_returns_manifest_and_views(monkeypatch, capsys, tmp_path):
    from jasper.active_speaker import round_bank
    views = [{"view": "bass", "status": "written", "out": "bass_view.json"}]
    banked = round_bank.BankedRound(tmp_path, {"manifest": "run_manifest.json", "views": views})
    calls = []
    monkeypatch.setattr(round_bank, "bank_round", lambda path, **kw: calls.append(path) or banked)
    monkeypatch.setattr(cli, "_round_session_dir", lambda run: str(tmp_path))
    opener = _run_opener({"status": "complete", "run": {"status": "complete", "manifest": "run_manifest.json"}})
    code, body = _run(["wait", "--run", "run-1"], opener, monkeypatch, capsys)
    assert code == 0 and calls == [tmp_path]
    assert body["manifest"] == "run_manifest.json" and body["views"] == views


@pytest.mark.parametrize("argv", [["run", "--tier", "express"], ["run", "--stage", "measure"], ["open"], ["bank"]])
def test_retired_round_arguments_are_usage_errors(argv):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("state", ["awaiting_join", "starting", "awaiting_capture", "stopping"])
def test_wait_does_not_bank_before_capture_cleanup(state, monkeypatch, capsys):
    opener = _run_opener({"status": state})
    code, body = _run(["wait", "--run", "run-1", "--timeout", "0"], opener, monkeypatch, capsys)
    assert code == 2
    assert body["reason"] == "wait_timeout"
    assert len(opener.requests) == 1


@pytest.mark.parametrize("argv,reason", [(["--repeats", "0"], "walk_level_policy_invalid"), (["--program", "room", "--mover", "arm"], "walk_over_mover_envelope")])
def test_run_shape_refusal_is_json(argv, reason, monkeypatch, capsys):
    code, body = _run(["run", *argv], _opener(), monkeypatch, capsys)
    assert code == 1
    assert body["reason"] == reason


def test_placement_cannot_join_a_replaced_run(monkeypatch):
    from types import SimpleNamespace
    from jasper.web import correction_capture as capture, correction_handlers as handlers
    pending = (SimpleNamespace(session_id="current"), None)
    monkeypatch.setattr(capture, "_pending_capture", pending)
    monkeypatch.setattr(capture, "_capture_slot", None)
    monkeypatch.setattr(handlers.correction_runtime, "read_json_body",
                        lambda handler: {"index": 1, "attempt": 1, "run_id": "old"})
    with pytest.raises(handlers.CrossoverV2Refused) as refused:
        handlers._handle_crossover_v2_position_ready(None)
    assert refused.value.code == "capture_slot_busy"
    assert capture._pending_capture is pending


def test_capture_slot_keeps_the_run_id_through_completion(monkeypatch):
    from jasper.web import correction_capture as capture
    monkeypatch.setattr(capture, "_capture_slot", None)
    monkeypatch.setattr(capture, "_capture_position_gate", None)
    assert capture._begin_capture_slot("crossover_v2:session", session_id="run-1")
    capture._publish_capture_waiting("crossover_v2:session")
    capture._set_capture_slot({"status": "complete", "kind": "crossover_v2:session"})
    assert capture._get_capture_slot()["session_id"] == "run-1"



def test_trial_posts_the_named_composed_candidate(tuning_profile, monkeypatch, capsys):
    from jasper.cli import _run_request
    from tests.test_preflight import ready_facts
    candidate = _room_candidate(tuning_profile)
    monkeypatch.setattr(_run_request, "read_preflight_facts", lambda plan: ready_facts(plan, candidates={candidate.fingerprint: candidate}))
    opener = _opener(session='{"session_id": "trial-1"}')
    code, body = _run(["run", "--program", "room", "--candidates", candidate.fingerprint], opener, monkeypatch, capsys)
    assert code == 0 and body["shape"] == "trial"
    plan = json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"]
    assert plan["candidates"] == [candidate.fingerprint]
    assert {stop["candidate_id"] for stop in plan["stops"]} == {candidate.fingerprint}


def test_run_names_a_lost_response(preflight_ready, monkeypatch, capsys):
    code, body = _run(["run"], _opener(session="bad json"), monkeypatch, capsys)
    assert code == 2 and body["reason"] == "run_answer_invalid"



def test_status_fault_history_keeps_each_code_once():
    from jasper.active_speaker.crossover_v2.position_gate import PositionGate
    gate = PositionGate()
    gate.publish({"fault": "capture_clipped", "attempt": 1})
    gate.publish({"fault": "capture_clipped", "attempt": 2})
    gate.publish({"fault": None, "attempt": 2})
    assert gate.published()["run"]["faults"] == ["capture_clipped"]
