# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Round CLI responses and requests through the wizard's fake HTTP transport."""
from __future__ import annotations

import io
import errno
import json
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import subprocess
import sys
import urllib.error
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest

from jasper.active_speaker import round_bank, round_packet, wizard_client as wc
from jasper.active_speaker.angle_capture import AngleCaptureRequest
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.candidate_bank import publish_authored_candidate
from jasper.active_speaker.candidate_parts import candidate_from_design_draft
from jasper.active_speaker.crossover_v2.evidence_packet import CrossoverEvidencePacketError
from jasper.active_speaker.crossover_v2.round_inputs import RoundSetRefused, round_inputs
from jasper.active_speaker.measurement_programs import run_program
from jasper.active_speaker.movers import MOVERS
from jasper.cli import _run_request, round as cli
from jasper.cli._refusal import STATUS_BY_CODE
from tests.active_speaker_fixtures import isolated_candidate_bank as isolated_candidate_bank
from tests.active_speaker_fixtures import mono_output_topology, standard_design_draft
from tests.crossover_v2_banked_round import bank_measure_round
from tests.run_manifest_fixture import write_manifest
from tests.test_crossover_v2_tuning_scope import tuning_profile as tuning_profile, _room_candidate
from tests.test_preflight import ready_facts
from tests.test_arm_walk import FakeWalkClock
from tests.test_active_speaker_measurement_door import box as box  # noqa: F401
from tests.test_crossover_v2_frequency_view import bass_fit_pairs as bass_fit_pairs  # noqa: F401

_FINGERPRINT = "a" * 64
_OTHER = "b" * 64


def test_round_parser_does_not_import_numpy():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json, sys\n"
            "from jasper.cli import round as cli\n"
            "imported = 'numpy' in sys.modules\n"
            "cli.build_parser()\n"
            "print(json.dumps([imported, 'numpy' in sys.modules]))\n"
        )], capture_output=True, text=True, check=True, timeout=10,
    )
    assert json.loads(result.stdout) == [False, False]


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
        ["apply", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_OK
    assert receipt["candidate_fingerprint"] == _FINGERPRINT
    assert [json.loads(request.data) for request in opener.posts()] == [
        {"expected_candidate_fingerprint": _FINGERPRINT},
    ]


def test_apply_posts_the_named_fingerprint_when_it_is_the_live_one(
    monkeypatch, capsys
):
    opener = _opener(v2={"candidate": {"fingerprint": _FINGERPRINT}})
    code, receipt = _run(
        ["apply", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_OK
    assert receipt["candidate_fingerprint"] == _FINGERPRINT
    assert "status" not in receipt
    posted = opener.posted_to(wc.APPLY_PATH)
    assert [json.loads(r.data.decode()) for r in posted] == [
        {"expected_candidate_fingerprint": _FINGERPRINT}
    ]
    assert len(opener.posts()) == 1


@pytest.mark.parametrize("payload,reason", [
    ({"status": "apply_failed", "issue": {"code": "apply_failed", "message": "Load failed."}}, "apply_failed"),
    ({"status": "blocked", "issue": {"id": "boost_over_declared_bound", "message": "Boost exceeded."}}, "boost_over_declared_bound"),
    ({"status": "blocked", "issue": {}, "issues": [{"code": "driver_safety_profile_not_confirmed", "message": "Confirm limits."}]}, "driver_safety_profile_not_confirmed"),
    ({"ok": False}, wc.REASON_NOT_APPLIED),
])
def test_an_apply_that_answered_but_did_not_apply_is_a_refusal(
    payload, reason, monkeypatch, capsys
):
    """200 alone passes `apply_failed`; only 200 AND `applied` is right."""
    opener = _opener(v2={"candidate": {"fingerprint": _FINGERPRINT}}, apply=json.dumps(payload))
    code, receipt = _run(
        ["apply", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_REFUSED
    assert receipt["status"] == STATUS_BY_CODE[cli.EXIT_REFUSED]
    assert receipt["reason"] == reason
    assert receipt["detail"]["refused_by"] == "wizard"
    if reason != wc.REASON_NOT_APPLIED:
        issue = payload.get("issue") or payload["issues"][0]
        assert receipt["code"] == reason
        assert receipt["detail"]["error"]["error"] == issue["message"]


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
        ["apply", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_UNREADABLE
    assert receipt["status"] == STATUS_BY_CODE[cli.EXIT_UNREADABLE]
    assert receipt["reason"] == wc.REASON_ANSWER_LOST
    assert receipt["detail"]["refused_by"] == ""
    assert receipt["detail"]["http"] == 0


@pytest.fixture
def preflight_ready(monkeypatch):
    monkeypatch.setattr(_run_request, "read_preflight_facts", ready_facts)


@pytest.fixture
def bank_trial(tuning_profile, isolated_candidate_bank, monkeypatch):
    def bank(resolution):
        candidate = replace(_room_candidate(tuning_profile), analysis={
            "measurement_status": "unmeasured", "resolution": resolution,
        })
        publish_authored_candidate(candidate)
        monkeypatch.setattr(_run_request, "read_preflight_facts",
                            lambda plan: ready_facts(plan, candidates={candidate.fingerprint: candidate}))
        return candidate.fingerprint
    return bank


@pytest.mark.parametrize("mover", [None, "arm", "human"])
@pytest.mark.parametrize("section,program,layout,default_mover", [
    ("driver", "room", "room_quick", "arm"),
    ("blend", "room", "room_quick", "arm"),
    ("alignment", "room", "room_quick", "arm"),
    ("topology", "room", "room_quick", "arm"),
    ("room", "room", "seat_express", "human"),
    ("bass", "bass", "bass_axis", "arm"),
])
def test_trial_uses_authored_section_and_keeps_candidates_at_each_pose(
    bank_trial, monkeypatch, capsys, section, program, layout, default_mover, mover,
):
    resolution = dict.fromkeys(("driver", "blend", "alignment", "topology", "room", "bass"), "base")
    fingerprint = bank_trial({**resolution, section: "document"})
    opener = _opener(session='{"session_id": "trial-1"}')
    argv = ["trial", fingerprint, *(["--mover", mover] if mover else [])]
    code, body = _run(argv, opener, monkeypatch, capsys)
    assert code == 0 and body["verb"] == "trial" and body["shape"] == "trial"
    plan = AngleCaptureRequest.from_mapping(json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"])
    expected = run_program(program, "room_quick" if section == "room" and mover == "arm" else layout)
    assert plan.program == f"{program}/{expected.size}"
    assert plan.mover == (mover or default_mover)
    assert plan.candidates == ("base", fingerprint)
    assert [(stop.place, stop.candidate_id, stop.regime) for stop in plan.stops] == [
        (pose.place, candidate, "summed") for pose in expected.poses for candidate in ("", fingerprint)
    ]
    assert plan.level.level_db is None and plan.level.resolved.reference_volume_db == -18


@pytest.mark.parametrize("mover", ["human", "arm"])
def test_declared_trial_uses_the_design_mark_speaker_experiment(isolated_candidate_bank, monkeypatch, capsys, mover):
    topology = mono_output_topology()
    candidate = candidate_from_design_draft(topology, standard_design_draft(topology))
    banked = publish_authored_candidate(candidate)
    monkeypatch.setattr(_run_request, "read_preflight_facts",
                        lambda plan: ready_facts(plan, candidates={candidate.fingerprint: candidate}))
    opener = _opener(session='{"session_id": "first-experiment"}')
    code, body = _run(["trial", banked.fingerprint, "--mover", mover], opener, monkeypatch, capsys)
    assert code == 0, body
    plan = AngleCaptureRequest.from_mapping(json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"])
    assert plan.program == "speaker/mark" and plan.mover == mover
    assert {(stop.angle_deg, stop.elevation_deg, stop.regime) for stop in plan.stops} == {(0, 0, "per_driver")}
    assert plan.candidates == () and body["shape"] == "measure"


@pytest.mark.parametrize("sections", [(), ("driver", "blend"), ("driver", "room", "bass")])
def test_trial_refuses_ambiguous_sections_without_opening_a_run(bank_trial, monkeypatch, capsys, sections):
    fingerprint = bank_trial(dict.fromkeys(sections, "document"))
    opener = _opener()
    code, body = _run(["trial", fingerprint], opener, monkeypatch, capsys)
    assert code == 1 and body["code"] == "trial_sections_ambiguous"
    assert body["detail"]["sections"] == sorted(sections)
    assert not opener.requests


def test_room_default_uses_the_human_seat_set(preflight_ready, monkeypatch, capsys):
    opener = _opener(session='{"session_id": "room-1"}')
    code, _ = _run(["run", "--program", "room"], opener, monkeypatch, capsys)
    assert code == 0
    plan = AngleCaptureRequest.from_mapping(json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"])
    assert (plan.program, plan.mover) == ("room/seat", "human")
    assert [(stop.kind, stop.seat_offset_m) for stop in plan.stops] == [
        ("seat", (0, 0, 0)), ("seat", (0.3, 0, 0)), ("seat", (0, 0.3, 0)),
    ]


@pytest.mark.parametrize("candidates,shape", [(None, "measure"), ("base", "trial")])
@pytest.mark.parametrize("source", ["flags", "file", "confirmed"])
def test_run_posts_inline_and_returns_without_a_status_read(preflight_ready, monkeypatch, capsys, tmp_path, candidates, shape, source):
    opener = _opener(session=json.dumps({"capture": {"session_id": "run-1", "first_prompt": {"title": "Place mic"}}}))
    argv = ["run", "--program", "room", "--poses", "seat_express", "--level-db", "-25"]
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
    assert (plan["artifact_schema_version"], body["run_id"]) == (5, "run-1")
    assert plan["level"]["level_db"] == -25
    assert body["link"].endswith(wc.CSRF_PAGE_PATH)
    assert body["shape"] == shape
    assert not any(r.full_url.endswith(wc.STATUS_PATH) for r in opener.requests)


def test_preflight_answers_without_posting(preflight_ready, monkeypatch, capsys):
    opener = _opener()
    code, body = _run(["run", "--dry-run"], opener, monkeypatch, capsys)
    assert code == 0 and body["dry_run"] is True and not body["issues"]
    assert not opener.requests


@pytest.mark.parametrize("path_owner", ["topology_path", "baseline_profile_state_path", "household_mic_path", "seat_level_reference_state_path"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_run_refuses_local_state_permission_fault(path_owner, dry_run, monkeypatch, capsys):
    path = getattr(_run_request, path_owner)()
    original_open = Path.open

    def open_state(self, *args, **kwargs):
        if self == path:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_state)
    facts = Mock(side_effect=AssertionError("preflight must not read missing facts"))
    monkeypatch.setattr(_run_request, "read_preflight_facts", facts)
    opener = _opener()
    code, body = _run(["run", "--program", "speaker", *(["--dry-run"] if dry_run else [])], opener, monkeypatch, capsys)
    assert code == cli.EXIT_REFUSED and body["code"] == "local_state_unreadable"
    assert body["detail"]["evidence"] == {"path": str(path)}
    assert body["next_action"]["id"] == "run_as_root"
    facts.assert_not_called()
    assert not opener.requests


@pytest.mark.parametrize("repeats", [None, 1, 2])
def test_run_repeats_replace_each_pose_count(preflight_ready, monkeypatch, capsys, repeats):
    from collections import Counter

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


@pytest.mark.parametrize("status", ["awaiting_join", "running", *sorted(wc.SESSION_ENDED_STATUSES)])
def test_stop_cancels_only_live_runs(status, monkeypatch, capsys):
    opener = _run_opener({"status": status})
    answer = {"capture": {"session_id": "run-1", "status": "stopping"}}
    opener.pages[wc.CAPTURE_CANCEL_PATH] = json.dumps(answer)
    code, body = _run(["stop", "--run", "run-1"], opener, monkeypatch, capsys)
    assert opener.requests[0].full_url.endswith(wc.STATUS_PATH)
    if status in wc.SESSION_ENDED_STATUSES:
        assert code == cli.EXIT_REFUSED and body["code"] == "run_not_live"
        assert body["detail"]["http"] == 409
        assert not opener.posts()
    else:
        assert code == cli.EXIT_OK and body == answer
        assert len(opener.posts()) == 1
        assert json.loads(opener.posted_to(wc.CAPTURE_CANCEL_PATH)[0].data) == {"reason": "user_stopped"}


@pytest.mark.parametrize("joining", [True, False])
@pytest.mark.parametrize("mover", MOVERS)
def test_placed_releases_only_confirmed_holds(joining, mover, monkeypatch, capsys):
    from jasper.active_speaker.crossover_v2.position_gate import POSITION_READY_ENDPOINT
    opener = _run_opener({"status": "awaiting_join" if joining else "running",
                          "join" if joining else "position_pending": {"index": 1, "attempt": 1, "mover": mover}})
    opener.pages[POSITION_READY_ENDPOINT] = '{"ok": true}'
    code, body = _run(["placed", "--run", "run-1", "--pose", "1"], opener, monkeypatch, capsys)
    posts = opener.posted_to(POSITION_READY_ENDPOINT)
    if mover == "confirmed":
        assert code == 0 and body["ok"] is True
        assert json.loads(posts[0].data) == {"index": 1, "attempt": 1, "run_id": "run-1"}
    else:
        assert code == 1 and body["code"] == "walk_mover_mismatch"
        assert not posts


def test_status_reads_progress_once(monkeypatch, capsys):
    progress = {"pose": 2, "poses": 3, "config": 1, "configs": 2, "attempt": 3,
                "fault": "capture_clipped", "next_action": "retake_quieter", "faults": [{"fault": "capture_clipped"}]}
    opener = _run_opener({"status": "running", "run": progress})
    code, body = _run(["status", "--run", "run-1"], opener, monkeypatch, capsys)
    assert code == 0
    assert [body[key] for key in ("pose", "config", "attempt", "code")] == [2, 1, 3, "capture_clipped"]
    assert body["faults"] == progress["faults"]
    assert len(opener.requests) == 1


@pytest.mark.parametrize("verb", ["status", "placed", "stop", "wait"])
def test_named_run_never_reads_or_releases_a_different_run(verb, monkeypatch, capsys):
    opener = _run_opener({"status": "running"})
    code, body = _run([verb, "--run", "old"], opener, monkeypatch, capsys)
    assert code == 1
    assert body["reason"] == "run_not_current"
    assert not opener.posts()


@pytest.mark.parametrize("captures,status,reason,result,polls,elapsed", [
    ([None, {"status": "complete", "run": {"status": "complete"}},
      {"session_id": "run-1", "status": "complete", "run": {"status": "complete"}}],
     "terminal", None, "complete", 3, 15),
    ([{"session_id": "run-2", "status": "running"}],
     "failed", "run_not_current", None, 1, 0),
    ([{}], "timed_out", "wait_timeout", None, 4, 20),
])
def test_wait_uses_live_capture_identity(captures, status, reason, result, polls, elapsed):
    envelopes = [json.dumps({
        "crossover_v2": {"session_id": "old", "phase": "review"},
        **({"capture": {"kind": "crossover_v2:session", **capture}} if capture is not None else {}),
    }) for capture in captures]
    opener = _FakeOpener({wc.STATUS_PATH: envelopes[-1]}, envelopes)
    client = wc.WizardClient(host_header="jts3.local", opener=opener)
    clock = FakeWalkClock(max_sleeps=4)
    start = clock.now()
    initial_http, initial = client.run_status("run-1")
    if status == "failed":
        assert initial_http == 409
        assert initial == {"run_id": "run-1", "code": reason, "current_run_id": "run-2"}
    else:
        assert initial_http == 200
        assert initial == {"run_id": "run-1", "status": "starting", "result": None,
                           "pending": None, "current": None, "code": None, "faults": []}
    opener.envelopes, opener.requests = list(envelopes), []
    answer = wc.wait_for_round(client, run_id="run-1", timeout_s=20,
                               now=clock.now, sleep=clock.sleep)
    assert answer["run_id"] == "run-1"
    assert (answer["status"], answer.get("reason"), answer.get("result")) == (status, reason, result)
    assert len(opener.requests) == polls
    assert clock.now() - start == elapsed
    assert not opener.posts()


@pytest.mark.parametrize("verb", ["wait", "run", "trial"])
@pytest.mark.parametrize("timeout", ["--timeout", "--timeout-s"])
@pytest.mark.parametrize("verbose", [False, True])
def test_wait_banks_and_returns_packet(preflight_ready, bank_trial, monkeypatch, capsys, tmp_path, verb, timeout, verbose):
    from jasper.cli.round_views import run_bookkeeping

    views = [{"view": "frequency", "status": "written", "out": "frequency_view.json", "image": "frequency.png"}]
    banked = round_bank.BankedRound(tmp_path, {"manifest": "run_manifest.json", "views": views})
    calls = []
    monkeypatch.setattr(round_bank, "bank_round", lambda path, **kw: calls.append((path, kw)) or banked)
    monkeypatch.setattr(cli, "_round_session_dir", lambda run: str(tmp_path))
    opener = _run_opener({"status": "complete", "run": {"status": "complete", "manifest": "run_manifest.json"}})
    opener.pages[wc.SESSION_PATH] = json.dumps({"capture": {"session_id": "run-1"}})
    argv = ["wait", "--run", "run-1"] if verb == "wait" else ["run", "--program", "room", "--poses", "seat_express", "--wait"]
    if verb == "trial":
        argv = ["trial", bank_trial({"room": "document"}), "--wait"]
    (tmp_path / "frequency.png").touch()
    code, body = _run([*argv, timeout, "0", *(["--verbose"] if verbose else [])], opener, monkeypatch, capsys)
    assert code == 0 and calls == [(tmp_path, {
        "view_runner": run_bookkeeping,
    })]
    assert list(body)[:5] == ["result", "reason", "round_dir", "packet", "picture"]
    assert body == {"result": "complete", "reason": None, "round_dir": str(tmp_path),
                    "packet": str(tmp_path / "packet.json"), "picture": str(tmp_path / "frequency.png"),
                    **({"views": views} if verbose else {})}


@pytest.mark.parametrize("stage", ["prescription_sources", "prescription_contracts"])
@pytest.mark.parametrize("error,reason", [
    (RoundSetRefused("round_set_unknown"), "round_set_unknown"),
    (CrossoverEvidencePacketError("missing evidence"), "evidence_unreadable"),
    (OSError("unreadable evidence"), "evidence_unreadable"),
    (ValueError("invalid contract"), "evidence_unreadable"),
])
def test_wait_banks_when_one_set_has_no_limits(tmp_path, monkeypatch, capsys, stage, error, reason):
    source = bank_measure_round(tmp_path / "live")
    inputs = round_inputs(source)
    mark_state(inputs.session_dir, "applied")
    write_manifest(source, program="room", groups=[
        {"set_id": set_id, "base": True, "capture_basis": {}, "takes": []}
        for set_id in ("unavailable", "available")
    ])
    contracts = round_packet.prescription_contracts

    def sources(inputs, *, set_id=None):
        if stage == "prescription_sources" and set_id == "unavailable":
            raise error
        return {"candidate": {"set_id": set_id}}

    def contract(**sources):
        if stage == "prescription_contracts" and sources["candidate"]["set_id"] == "unavailable":
            raise error
        return contracts(**sources)

    monkeypatch.setattr(round_packet, "prescription_sources", sources)
    monkeypatch.setattr(round_packet, "prescription_contracts", contract)
    monkeypatch.setattr(round_bank, "bank_round", partial(
        round_bank.bank_round, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
        **{name: tmp_path / name for name in ("design_draft_path", "applied_profile_path", "repeat_floor_path",
                                            "declared_geometry_path", "statefile_path")},
    ))
    monkeypatch.setattr(cli, "_round_session_dir", lambda run: str(inputs.session_dir))
    code, body = _run(["wait", "--run", "run-1", "--timeout", "0"],
                      _run_opener({"status": "complete", "run": {"status": "complete"}}), monkeypatch, capsys)
    assert code == 0 and body["result"] == "complete"
    packet = json.loads(Path(body["packet"]).read_text())
    assert packet["result"] == "complete"
    assert packet["limits"]["unavailable"] == {"status": "unavailable", "reason": reason}
    assert "schema" in packet["limits"]["available"]
    assert (Path(body["round_dir"]) / "provenance.json").is_file()


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


@pytest.mark.parametrize("address", ["http://jts3.local", "http://192.168.1.8", "http://[2001:db8::1]"])
def test_remote_dry_run_refuses_before_reading_local_facts(monkeypatch, capsys, address):
    def no_facts(*args, **kwargs):
        pytest.fail("remote dry-run read local facts")
    monkeypatch.setattr(_run_request, "read_preflight_facts", no_facts)
    opener = _opener()
    monkeypatch.setattr(cli, "read_identity", no_facts)
    code = cli.main(["run", "--dry-run", "--base-url", address], opener=opener)
    body = json.loads(capsys.readouterr().out)
    assert code == 1 and body["reason"] == "dry_run_requires_local_host"
    assert not opener.requests


@pytest.mark.parametrize("dry_run", [False, True])
def test_bass_axis_uses_the_registered_mover(preflight_ready, monkeypatch, capsys, dry_run):
    opener = _opener(session='{"session_id": "run-1"}')
    code, body = _run(["run", "--program", "bass", "--layout", "bass_axis", "--level-db", "-25", *(["--dry-run"] if dry_run else [])],
                      opener, monkeypatch, capsys)
    body = body if dry_run else body["schedule"]
    assert code == 0 and body["mic_moves"] == 1
    capture, = body["schedule"]
    assert capture["regime"] == "summed"
    assert body["level"]["level_db"] == -25
    assert not opener.requests if dry_run else "levels" not in json.loads(opener.posts()[0].data)


@pytest.mark.parametrize("noise_dbfs,levels", [(-60, [-18, -23]), (-100, [-18, -23, -28, -33]), (-20, [])])
def test_bass_dry_run_lists_admissible_session_offsets(monkeypatch, capsys, noise_dbfs, levels):
    def facts(plan):
        ready = ready_facts(plan)
        return replace(ready, anchor=replace(ready.anchor, record={**ready.anchor.record,
            "ambient_report": {"bands": [{"band_hz": [20, 80], "level_dbfs": noise_dbfs}]}}))

    monkeypatch.setattr(_run_request, "read_preflight_facts", facts)
    opener = _opener()
    code, body = _run(["run", "--program", "bass", "--layout", "bass_axis", "--dry-run"],
                      opener, monkeypatch, capsys)
    assert code == (0 if levels else 1)
    assert body["dry_run"] is True
    assert body["admissible_levels_db"] == levels
    assert [row["offset_db"] for row in body["levels"]] == [0, -5, -10, -15]
    assert [row["level_db"] for row in body["levels"]] == [-18, -23, -28, -33]
    assert not opener.requests


@pytest.mark.parametrize("verb,flags,noise,levels", [
    ("run", ["--level-db", "-18"], -60, [-18]),
    ("run", ["--levels=-18"], -60, [-18]),
    ("run", ["--levels", "auto"], -55, [-18]),
    ("run", ["--levels", "auto"], -60, [-18, -23]),
    ("run", ["--levels", "auto"], -100, [-18, -23, -28, -33]),
    ("run", ["--levels=-18,-28"], -100, [-18, -28]),
    ("trial", [], -60, [-18, -23]),
])
def test_bass_run_wait_banks_every_level_and_joins_only_multiple_levels(
    monkeypatch, capsys, tmp_path, box, bass_fit_pairs, tuning_profile, isolated_candidate_bank,
    verb, flags, noise, levels,
):
    from jasper.active_speaker import bundles, round_bank, plan_run
    from jasper.active_speaker.bass_levels import BassLevelLadder, preflight_levels, prepare_bass_captures
    from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore, EVIDENCE_ROOT
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
    from jasper.active_speaker.crossover_v2.round_inputs import round_inputs, default_out
    from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict
    from jasper.active_speaker.run_manifest import RunManifest
    from jasper.audio_measurement.calibration import MicSensitivity
    from jasper.cli import round_views
    from jasper.cli.round_views import _bass_inputs
    from jasper.web import correction_run_host as host, correction_crossover_v2_wired as wired
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.engine_twin import FakeSeams
    from tests.crossover_v2_fixtures import _conductor, FakeSeams as FlowSeams
    from tests.test_plan_run import AnsweredGate, _analysis
    from tests.test_crossover_v2_tuning_scope import BASS_EXTENSION

    candidate = replace(_room_candidate(tuning_profile), bass_extension=BASS_EXTENSION,
                        analysis={"resolution": {"bass": "document"}, "measurement_status": "unmeasured"})
    join = Mock(wraps=_bass_inputs.join_bass_rounds)
    monkeypatch.setattr(_bass_inputs, "join_bass_rounds", join)
    publish_authored_candidate(candidate)
    def facts(plan):
        ready = ready_facts(plan, candidates={candidate.fingerprint: candidate})
        return replace(ready, anchor=replace(ready.anchor, record={**ready.anchor.record,
            "ambient_report": {"bands": [{"band_hz": [20, 80], "level_dbfs": noise}]}}))
    monkeypatch.setattr(_run_request, "read_preflight_facts", facts)
    monkeypatch.setattr("jasper.active_speaker.candidate_parts.baseline_candidate_id", lambda: "baseline-fp")
    info = bundles.open_bundle(mono_output_topology(), calibration_id="", sessions_dir=tmp_path / "sessions")
    bundle = Path(info["bundle_dir"])
    store = CommissioningEvidenceStore.open(bundle, expected_session_id=info["session_id"])
    manifest = RunManifest("run-1", BankedRecordStore(store, "run-1"))
    fakes, gate = FakeSeams(), AnsweredGate()
    entry_volume, entry_loudness = box.volume_db, asyncio.run(box.get_loudness_volume_db())
    fakes.graph.entry_scope_fingerprint = "entry"
    monkeypatch.setattr(host, "resolved_household_sensitivity", lambda _: MicSensitivity(-12, 18, "1234"))
    monkeypatch.setattr(host, "bind_plan_analysis", lambda *a, **kw: (_analysis, lambda *a, **kw: TakeVerdict(True)))
    hold = host.isolation_hold
    monkeypatch.setattr(host, "isolation_hold", lambda **kw: hold(**{**kw, "plan": None, "volume_state_path": tmp_path / "volume.json"}))
    for name in ("persist_conductor_state", "_persist_execution_result", "_persist_terminal_failure"):
        monkeypatch.setattr(wired.v2state, name, lambda *a, **kw: None)

    def engine(**kw):
        async def capture_record(record):
            return await kw["records"].inner.bank({**record, "program_id": "sweep", "stimulus_dbfs": -20,
                "loudness_volume_db": record["level_db"], "phase": "lateral"})
        return replace(fakes, graph=kw["session_graph"], volume=kw["volume_claim"],
                       records=SimpleNamespace(bank=capture_record)).seams()

    opener = _run_opener({"status": "complete", "run": {"status": "complete"}})
    open_request = opener.open
    def open_and_execute(request, timeout=None):
        if request.full_url.endswith(wc.SESSION_PATH) and request.data:
            raw = json.loads(request.data)
            plan = AngleCaptureRequest.from_mapping(raw["plan"])
            report = preflight_levels(plan, facts(plan), raw.get("levels"))
            plan = report.plan
            conductor = _conductor(FlowSeams())
            door, analyze, assessor, execute = host.bind_run_door(
                host=SimpleNamespace(_wired_stimulus_capture=lambda *a, **kw: None, bind_v2_engine_seams=engine),
                device=SimpleNamespace(model_key="minidsp_umik2"), evidence_store=store, manifest=manifest,
                production=SimpleNamespace(graph=fakes.graph, compose=None),
                conductor=conductor, refs={}, trims={},
                ceiling_s=30, ceiling_db_spl=85, camilla_factory=lambda: box, verify_only=False,
                level=plan.level, ladder=report if isinstance(report, BassLevelLadder) else None,
            )
            runner = wired.build_v2_wired_run_and_consume(
                conductor, door=door, signals=plan_run.RunSignals(), position_gate=gate,
                ceiling_s=30, manifest=manifest, request=plan, analyze=analyze, assessor=assessor,
                captures=prepare_bass_captures(plan, roles_bands=conductor._roles), execute=execute,
            )
            asyncio.run(runner(SimpleNamespace(session_id=manifest.run_id)))
            bundles.mark_state(bundle, "closed")
            opener.pages[wc.SESSION_PATH] = '{"session_id": "run-1"}'
        return open_request(request, timeout)
    opener.open = open_and_execute

    def view(view, target, *, set_id=None, **kw):
        if view != "bass":
            return {"view": view, "status": "unavailable"}
        inputs = round_inputs(target)
        document = json.loads((inputs.session_dir / EVIDENCE_ROOT / "artifacts/crossover_v2/run-1/run_manifest.json").read_text())
        group = next(group for group in document["sets"] if set_id is None or group["set_id"] == set_id)
        takes = []
        for entry in group["takes"]:
            take = deepcopy(bass_fit_pairs[0][0])
            take["record_path"] = entry["artifacts"]["record_id"]
            take["record"] = json.loads((inputs.session_dir / EVIDENCE_ROOT / "artifacts" / take["record_path"]).read_text())
            take.update(freqs_hz=[20, 30, 40, 50, 60, 80, 100, 200], fundamental_qualified=[True] * 8,
                        fundamental_db=[take["record"]["level_db"] - (6 if group["base"] else 1)] * 8)
            take["frequency_curve"]["magnitude_db"] = [take["record"]["level_db"]] * 3
            takes.append(take)
        path = default_out(inputs, target, "bass_view.json", set_id)
        path.write_text(json.dumps({"schema": "jts_bass_view/1", "takes": takes}))
        return {"view": view, "status": "written", "out": str(path)}
    monkeypatch.setattr(round_views, "run_bookkeeping", view)
    bank = round_bank.bank_round
    monkeypatch.setattr(round_bank, "bank_round", lambda path, **kw: bank(path, campaign_root=tmp_path / "campaigns", **kw))
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    argv = ["trial", candidate.fingerprint] if verb == "trial" else ["run", "--program", "bass", "--layout", "bass_axis"]
    code, body = _run([*argv, *flags, "--wait"], opener, monkeypatch, capsys)
    assert code == 0, body
    assert [call["level_db"] for call in fakes.play.calls] == [level for level in levels for _ in range(2 if verb == "trial" else 1)]
    assert len(gate.grants) == fakes.graph.restores == 1
    assert (box.volume_db, asyncio.run(box.get_loudness_volume_db())) == (entry_volume, entry_loudness)
    packet = json.loads(Path(body["packet"]).read_text())
    assert packet["result"] == "complete" and len(packet.get("runs", [packet])) == len(levels)
    assert Path(body["packet"]) == Path(body["round_dir"]) / "packet.json"
    assert {"sets", "series", "limits", "applied", "artifacts", "unavailable"} <= packet.keys()
    assert len(packet["artifacts"]["bass_views"]) == len(levels) * (2 if verb == "trial" else 1)
    assert len(packet["bass"]) == len(packet["artifacts"]["bass_views"])
    assert {entry["set_id"] for entry in packet["bass"]} == {group["set_id"] for group in packet["sets"]}
    for entry in packet["bass"]:
        assert entry == {**json.loads(Path(entry["out"]).read_text()), "set_id": entry["set_id"], "out": entry["out"]}
    assert {take["record"]["level_db"] for entry in packet["bass"] for take in entry["takes"]} == set(levels)
    assert {take["record"]["level_db"] for view in packet["artifacts"]["bass_views"]
            for take in json.loads(Path(view["out"]).read_text())["takes"]} == set(levels)
    assert join.call_count == (1 if len(levels) > 1 else 0)
    if len(levels) == 1:
        assert "bass_table" not in packet
        return
    assert packet["bass_table"].get("schema") == "jts_bass_run_table/1", packet["bass_table"]
    table, = packet["bass_table"]["tables"]
    assert table["target"] == {"freqs_hz": [20, 60], "magnitude_db": [0, 0]}
    assert sorted(row["level_key"]["level_db"] for row in table["levels"]) == sorted(levels)
    assert {row["outcome"] for row in table["levels"]} == {"target_met" if verb == "trial" else "target_not_met"}
