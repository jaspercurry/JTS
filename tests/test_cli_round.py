# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Round CLI responses and requests through the wizard's fake HTTP transport."""
from __future__ import annotations

import io
import errno
import json
import asyncio
from collections import Counter
from copy import deepcopy
from types import SimpleNamespace
import subprocess
import sys
import threading
import signal
import urllib.error
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from jasper import output_topology
from jasper.active_speaker import arm_walk as aw, candidate_bank, graph_safety, round_bank, round_packet, wizard_client as wc
from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.candidate_bank import publish_authored_candidate
from jasper.active_speaker.candidate_parts import candidate_from_design_draft
from jasper.active_speaker import baseline_profile
from jasper.active_speaker.crossover_v2.prescription_document import judge_prescription_document
from jasper.active_speaker.crossover_v2 import prescription_document as prescription_document_mod
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverAlignment, compile_candidate_config
from jasper.active_speaker.design_draft import load_design_draft
from jasper.web import correction_capture, correction_crossover_v2_apply as v2apply
from jasper.active_speaker.crossover_v2.evidence_packet import CrossoverEvidencePacketError
from jasper.active_speaker.crossover_v2.round_inputs import RoundSetRefused, round_inputs, resolve_set
from jasper.active_speaker.measurement_programs import run_program
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.movers import MOVERS
from jasper.active_speaker.round_copy import round_lines
from jasper.cli import _run_request, crossover_prescriber, round as cli
from jasper.cli._refusal import STATUS_BY_CODE
from tests.active_speaker_fixtures import isolated_candidate_bank as isolated_candidate_bank
from tests.active_speaker_fixtures import mono_output_topology, standard_design_draft
from tests.crossover_v2_banked_round import bank_measure_round
from tests.run_manifest_fixture import write_manifest
from tests.test_crossover_v2_tuning_scope import BASS_EXTENSION, tuning_profile as tuning_profile, _room_candidate
from tests.test_active_speaker_measured_crossover_candidate import _candidate, _room_correction
from tests.test_rear_output_foundation import _rear_document, _rear_pair
from tests.test_preflight import ready_facts
from tests.test_arm_walk import (
    FakeMover, FakeSession, FakeWalkClock, LiveThen, _COMPLETE, _STOPPED,
    _IN_FLIGHT_QUIET, _RecordingTrail, _own_signals,
)
from tests.test_correction_crossover_v2_endpoints import _FakeApplyCam, _seed_baseline_apply_environment
from tests.test_prescription_document import document, timing_evidence
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
    monkeypatch.setattr(cli, "speaker_url", lambda path: f"http://jts3.local{path}")
    code = cli.main(list(argv), opener=opener)
    return code, json.loads(capsys.readouterr().out)


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


def test_apply_sends_no_explicit_host_header_by_default(monkeypatch, capsys):
    """A same-box loopback client no longer resolves this speaker's own name
    into a synthetic Host header -- with no --hostname, urllib derives Host
    from base_url, and the management-host guard accepts loopback IPs with
    no identity lookup."""
    monkeypatch.setenv("JASPER_HOSTNAME", "other.local")
    opener = _opener(v2={"candidate": {"fingerprint": _FINGERPRINT}})
    code, _ = _run(["apply", _FINGERPRINT], opener, monkeypatch, capsys)

    assert code == cli.EXIT_OK
    assert opener.requests
    assert all(not request.has_header("Host") for request in opener.requests)


@pytest.mark.parametrize("cardioid", [False, True])
@pytest.mark.parametrize("program,keep_timing,sections", [
    (None, False, {"driver", "blend", "alignment", "rear_calibration", "bass", "room"}),
    (None, True, {"driver", "blend", "rear_calibration", "bass", "room"}),
    ("speaker", False, {"driver", "blend", "alignment"}),
    ("speaker", True, {"driver", "blend"}),
    ("rear", False, {"rear_calibration"}),
    ("bass", False, {"bass"}),
    ("room", False, {"room"}),
])
def test_reset_composes_and_applies_the_selected_scope(
    program, keep_timing, sections, cardioid, monkeypatch, capsys, tmp_path, isolated_candidate_bank,
):
    topology, _ = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    preset = None
    if cardioid:
        preset, topology = _rear_pair("mono")
    base = publish_authored_candidate(replace(_candidate(
        preset=preset, alignment=MeasuredCrossoverAlignment(22, "tweeter", "keep"),
        linearization={"tweeter": {"filters": [
            {"biquad_type": "Peaking", "freq": 4000, "q": 1, "gain": -1},
        ]}}, room_correction=_room_correction(), bass_extension=BASS_EXTENSION,
        rear_calibration=_rear_document() if cardioid else {},
    ), analysis={"measurement_status": "unmeasured"},
        blend_correction=[{"biquad_type": "Peaking", "freq": 1000, "q": 1, "gain": -1}]))
    base = publish_authored_candidate(judge_prescription_document(
        document(base.fingerprint), base=base, base_profile={},
    ))
    trims_db = base.candidate.role_attenuations_db
    timing = {"delay_us": 22, "polarity": "normal", "provenance": "measured"}
    applied = {"status": "applied", "source": {"measured_candidate_fingerprint": base.fingerprint},
               "timing": {**timing, "provenance": "incumbent"}, **(
        {"corrections": {role: {"gain_db": db} for role, db in trims_db.items()}}
        if trims_db else {}
    )}
    timing_kept = "alignment" not in sections
    persisted = {"timing": timing} if timing_kept else {}
    pre_apply_read = Mock(side_effect=[applied])
    post_apply_read = Mock(side_effect=[persisted])
    monkeypatch.setattr(prescription_document_mod, "load_applied_baseline_profile_state", pre_apply_read)
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", post_apply_read)
    monkeypatch.setattr(output_topology, "load_output_topology_strict", lambda: topology)
    real_compose = crossover_prescriber.compose_prescription_document
    composed_with: dict = {}
    def _spy_compose(document, *, base, evidence=None, base_profile=None):
        composed_with["document"] = document
        composed_with["base_profile"] = base_profile
        return real_compose(document, base=base, evidence=evidence, base_profile=base_profile)
    monkeypatch.setattr(crossover_prescriber, "compose_prescription_document", _spy_compose)

    opener = _opener()
    code, body = _run(["reset", *(["--program", program] if program else []),
                      *(["--keep-timing"] if keep_timing else [])],
                      opener, monkeypatch, capsys)

    assert code == cli.EXIT_OK, body
    candidate = candidate_bank.find_banked_candidate(body["candidate_fingerprint"]).candidate
    assert candidate.role_attenuations_db == base.candidate.role_attenuations_db
    assert set(composed_with["document"]["sections"]) == sections
    for section, field in (("driver", "linearization"), ("blend", "blend_correction"),
                           ("alignment", "alignment"), ("rear_calibration", "rear_calibration"),
                           ("bass", "bass_extension"), ("room", "room_correction"), ("topology", "source_preset")):
        if section not in sections:
            assert json.dumps(candidate.to_dict().get(field), sort_keys=True) == json.dumps(
                base.candidate.to_dict().get(field), sort_keys=True)
        elif section == "alignment":
            assert candidate.alignment == MeasuredCrossoverAlignment()
        else:
            assert not getattr(candidate, field)
    if "driver" in sections:
        assert composed_with["document"]["sections"]["driver"]["pinned_trim_db"] == trims_db
    if "rear_calibration" in sections:
        assert composed_with["document"]["sections"]["rear_calibration"] is None
        if cardioid:
            graph = yaml.safe_load(compile_candidate_config(candidate, playback_device="null"))
            assert graph_safety.output_terminally_muted(
                graph, graph_safety.view_from_yaml_dict(graph), 2,
                mute_name="as_out2_rear_pending_mute", mute_gain_db=-120.0,
            )
    assert body["timing"] == {"saved": timing_kept,
                              "provenance": "measured" if timing_kept else None}
    assert body["trims_db"] == trims_db
    assert pre_apply_read.call_count == 1
    assert post_apply_read.call_count == 1
    assert composed_with["base_profile"] is applied
    assert [json.loads(request.data) for request in opener.posts()] == [
        {"expected_candidate_fingerprint": candidate.fingerprint},
    ]


@pytest.mark.parametrize("program", ["rear", "bass", "room"])
def test_reset_keep_timing_requires_timing_in_scope(program):
    opener = _opener()
    with pytest.raises(SystemExit) as caught:
        cli.main(["reset", "--program", program, "--keep-timing"], opener=opener)
    assert caught.value.code == 2
    assert not opener.requests


@pytest.mark.parametrize("source", ["saved", "measured"])
def test_apply_document_timing_reaches_record_and_loaded_graph(monkeypatch, tmp_path, capsys, source):
    topology, _ = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    base = publish_authored_candidate(candidate_from_design_draft(topology, load_design_draft(topology=topology)))
    saved = {"delay_us": 22, "polarity": "inverted", "provenance": "measured", "measured": {
        "round_id": "old", "take_id": "old-take", "graph_fingerprint": "old-graph", "at": "2026-09-14T12:00:00Z",
        "margin_db": .8, "residual_rms_db": .3, "repeat_spread_db": .2, "repeat_spread_us": 3, "repeat_count": 4,
    }} if source == "saved" else None
    if saved:
        (tmp_path / "baseline_profile.json").write_text(json.dumps({"status": "applied", "timing": saved,
            "artifact_schema_version": baseline_profile.SCHEMA_VERSION, "kind": baseline_profile.BASELINE_PROFILE_KIND}))
    evidence = timing_evidence(base, saved=saved)
    child = judge_prescription_document(document(base.fingerprint), base=base, evidence=evidence)
    publish_authored_candidate(child)
    cam = _FakeApplyCam()
    monkeypatch.setattr(baseline_profile, "_utc_now", lambda: "2026-09-15T12:00:00Z")
    opener = _opener()
    original_open = opener.open

    def open_and_apply(request, timeout=None):
        if request.full_url.endswith(wc.APPLY_PATH):
            payload = json.loads(request.data)
            result = asyncio.run(v2apply.apply_candidate(payload["expected_candidate_fingerprint"], camilla_factory=lambda: cam))
            opener.pages[wc.APPLY_PATH] = json.dumps(result)
        return original_open(request, timeout)

    opener.open = open_and_apply
    code, receipt = _run(["apply", child.fingerprint], opener, monkeypatch, capsys)
    assert code == 0 and receipt["candidate_fingerprint"] == child.fingerprint
    applied = baseline_profile.load_applied_baseline_profile_state()
    timing = applied["timing"]
    assert timing == (saved or {"delay_us": -37.5, "polarity": "inverted", "provenance": "measured", "measured": {
        "round_id": "r1", "take_id": "t2", "graph_fingerprint": "graph", "at": applied["applied_at"],
        "margin_db": .6, "residual_rms_db": .2, "repeat_spread_db": .1, "repeat_spread_us": 2, "repeat_count": 3}})
    filters = yaml.safe_load(Path(cam.path).read_text())["filters"]
    assert 1000 * (filters["as_tweeter_delay"]["parameters"]["delay"] - filters["as_woofer_delay"]["parameters"]["delay"]) == pytest.approx(timing["delay_us"])
    assert filters["as_tweeter_baseline_gain"]["parameters"]["inverted"] is True
    assert filters["as_woofer_baseline_gain"]["parameters"]["inverted"] is False


@pytest.mark.parametrize("payload,reason", [
    ({"status": "apply_failed", "issue": {"code": "apply_failed", "message": "Load failed."}}, "apply_failed"),
    ({"status": "blocked", "issue": {"id": "boost_over_declared_bound", "message": "Boost exceeded."}}, "boost_over_declared_bound"),
    ({"status": "blocked", "issue": {}, "issues": [{"code": "tweeter:required_highpass_missing", "message": "Declare the tweeter floor."}]}, "tweeter:required_highpass_missing"),
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


@pytest.fixture(autouse=True)
def arm_runtime(monkeypatch):
    mover, trail, clock = FakeMover(), _RecordingTrail(), FakeWalkClock()
    monkeypatch.setattr(mover, "available", Mock(return_value=True), raising=False)
    threads = []
    real_thread = threading.Thread
    def thread(**kw):
        worker = real_thread(**kw)
        worker.join = Mock(wraps=worker.join)
        threads.append(worker)
        return worker
    monkeypatch.setattr(aw.threading, "Thread", thread)
    pause = threading.Event()
    def sleep(seconds):
        clock.sleep(seconds)
        pause.wait(.001)
    monkeypatch.setattr(aw, "ArmWalk", partial(aw.ArmWalk, clock=clock.now, sleep=sleep))
    factory = Mock(return_value=mover, timeout_s=aw.TurntableMover.timeout_s)
    monkeypatch.setattr(aw, "TurntableMover", factory)
    session = Mock(side_effect=lambda **kw: LiveThen(_COMPLETE))
    monkeypatch.setattr(aw, "LoopbackSession", session)
    monkeypatch.setattr(aw, "Trail", lambda: trail)
    install = Mock(wraps=aw.install_park_on_signals)
    monkeypatch.setattr(aw, "install_park_on_signals", install)
    with _own_signals():
        yield SimpleNamespace(install=install, mover=mover, trail=trail, threads=threads, factory=factory, session=session)


@pytest.fixture
def arm_plan_answer(monkeypatch):
    def wait(client, args, **kw):
        plan = _run_request.resolve_run(args)
        return cli.answered({"verb": args.command, "shape": "trial" if plan.plan.candidates else "measure", "schedule": plan.to_dict()})
    monkeypatch.setattr(cli, "_cmd_wait", wait)


@pytest.fixture
def preflight_ready(monkeypatch):
    monkeypatch.setattr(_run_request, "read_preflight_facts", ready_facts)


@pytest.fixture
def bank_trial(tuning_profile, isolated_candidate_bank, monkeypatch):
    candidates = {}
    def bank(resolution):
        candidate = replace(_room_candidate(tuning_profile), analysis={
            "measurement_status": "unmeasured", "resolution": resolution,
        })
        publish_authored_candidate(candidate)
        candidates[candidate.fingerprint] = candidate
        monkeypatch.setattr(_run_request, "read_preflight_facts",
                            lambda plan, **kw: ready_facts(plan, **kw, candidates=candidates))
        return candidate.fingerprint
    return bank


@pytest.mark.parametrize("mover", [None, "arm", "human"])
@pytest.mark.parametrize("sections,program,layout,default_mover", [
    (("driver",), "room", "seat_express", "human"),
    (("blend",), "room", "seat_express", "human"),
    (("driver", "blend"), "room", "seat_express", "human"),
    (("alignment",), "room", "seat_express", "human"),
    (("topology",), "room", "seat_express", "human"),
    (("room",), "room", "seat_express", "human"),
    (("bass",), "bass", "bass_axis", "arm"),
    (("driver", "room"), "room", "seat_express", "human"),
])
def test_trial_uses_authored_section_and_keeps_candidates_at_each_pose(
    bank_trial, banked_session_level, monkeypatch, capsys, sections, program,
    layout, default_mover, mover, arm_plan_answer,
):
    resolution = dict.fromkeys(("driver", "blend", "alignment", "topology", "room", "bass"), "base")
    fingerprint = bank_trial({**resolution, **dict.fromkeys(sections, "document")})
    opener = _opener(session='{"session_id": "trial-1"}')
    argv = ["trial", fingerprint, "--wait", "--attest-rig-clear", *(["--mover", mover] if mover else [])]
    code, body = _run(argv, opener, monkeypatch, capsys)
    if mover == "human" and default_mover == "arm":
        assert code == 1 and body["reason"] == "walk_mover_mismatch"
        return
    assert code == 0 and body["verb"] == "trial" and body["shape"] == "trial"
    plan = AngleCaptureRequest.from_mapping(json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"])
    expected = run_program(program, "room_quick" if program == "room" and mover == "arm" else layout)
    assert plan.program == f"{program}/{expected.size}"
    assert plan.mover == (mover or default_mover)
    assert plan.candidates == ("base", fingerprint)
    assert [(stop.place, stop.candidate_id, stop.regime) for stop in plan.stops] == [
        (pose.place, candidate, "summed") for pose in expected.poses for candidate in ("", fingerprint)
    ]
    if expected.levels is None:
        assert plan.level.level_db == -20
        assert plan.level.resolved.reference_volume_db == -18
        assert plan.level_source == "seat_reference"
    else:
        assert plan.level.level_db is None
        assert plan.level_source == "program_default"


@pytest.mark.parametrize("mover", ["human", "arm"])
def test_declared_trial_uses_the_design_mark_speaker_experiment(isolated_candidate_bank, monkeypatch, capsys, mover, arm_plan_answer):
    topology = mono_output_topology()
    candidate = candidate_from_design_draft(topology, standard_design_draft(topology))
    banked = publish_authored_candidate(candidate)
    monkeypatch.setattr(_run_request, "read_preflight_facts",
                        lambda plan, **kw: ready_facts(plan, **kw, candidates={candidate.fingerprint: candidate}))
    opener = _opener(session='{"session_id": "first-experiment"}')
    code, body = _run(["trial", banked.fingerprint, "--mover", mover, "--wait", "--attest-rig-clear"], opener, monkeypatch, capsys)
    assert code == 0, body
    plan = AngleCaptureRequest.from_mapping(json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"])
    assert plan.program == "speaker/mark" and plan.mover == mover
    assert {(stop.angle_deg, stop.elevation_deg, stop.regime) for stop in plan.stops} == {
        (0, 0, "per_driver"), (0, 0, "summed")}
    assert plan.candidates == () and body["shape"] == "measure"


@pytest.mark.parametrize("sections,program", [
    ((), "room/seat"), (("driver", "blend"), "room/seat"),
    (("driver", "room", "bass"), "bass/axis"),
    (("rear_calibration", "bass", "room"), "rear/express"),
])
def test_trial_selects_program_by_section_precedence(bank_trial, monkeypatch, capsys, sections, program, arm_plan_answer):
    fingerprint = bank_trial(dict.fromkeys(sections, "document"))
    opener = _opener(session='{"session_id": "whole-document"}')
    code, body = _run(["trial", fingerprint, "--wait", "--attest-rig-clear"], opener, monkeypatch, capsys)
    assert code == 0 and body["shape"] == "trial"
    assert json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"]["program"] == program


def test_trial_posts_explicit_candidates(bank_trial, monkeypatch, capsys, arm_plan_answer):
    first = bank_trial({"driver": "document"})
    second = bank_trial({"driver": "document", "blend": "document"})
    opener = _opener(session='{"session_id": "variants"}')
    code, _ = _run(["trial", first, "--wait", "--attest-rig-clear", "--candidates", f"{second},base,{first}"], opener, monkeypatch, capsys)
    assert code == 0
    plan = AngleCaptureRequest.from_mapping(json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"])
    assert plan.candidates == (second, "base", first)
    assert [stop.candidate_id for stop in plan.stops] == [second, "", first] * 3


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
@pytest.mark.parametrize("source", ["flags", "file"])
def test_run_posts_inline_and_returns_without_a_status_read(preflight_ready, monkeypatch, capsys, tmp_path, candidates, shape, source):
    opener = _opener(session=json.dumps({"capture": {"session_id": "run-1", "first_prompt": {"title": "Place mic"}}}))
    argv = ["run", "--program", "room", "--poses", "seat_express", "--level-db", "-25"]
    if candidates:
        argv += ["--candidates", candidates]
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


@pytest.mark.parametrize("program,layout", [("speaker", "baseline_express"), ("rear", "rear/pair_behind")])
@pytest.mark.parametrize("repeats", [None, 1, 2])
def test_run_repeats_replace_each_pose_count(preflight_ready, bank_trial, monkeypatch, capsys, program, layout, repeats):
    opener = _opener(session=json.dumps({"capture": {"session_id": "run-1"}}))
    argv = ["run", "--program", program, "--poses", layout]
    if program == "rear":
        argv += ["--candidates", bank_trial({"rear_calibration": "document"})]
    selected = run_program(program, layout)
    if repeats is not None:
        argv += ["--repeats", str(repeats)]
    code, _ = _run(argv, opener, monkeypatch, capsys)
    assert code == 0
    plan = AngleCaptureRequest.from_mapping(json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)["plan"])
    assert plan.repeats == 1
    assert Counter(stop.place for stop in plan.stops) == {
        pose.place: (pose.repeats if repeats is None else repeats) + selected.room_sweep
        for pose in selected.poses
    }


@pytest.mark.parametrize("repeats", [None, 2])
def test_rear_behind_dry_run_counts_each_candidate_at_both_poses(monkeypatch, capsys, repeats):
    preset, topology = _rear_pair("mono")
    candidates = [_candidate(preset=preset, rear_calibration=_rear_document(rear_muted=muted),
                             program_id=f"rear-{index}") for index, muted in enumerate((False, False, True))]
    bank = {candidate.fingerprint: candidate for candidate in candidates}
    monkeypatch.setattr(_run_request, "read_preflight_facts", lambda plan, **kw: ready_facts(
        plan, **kw, candidates=bank, declared_target_ids=tuple(
            output_topology.measurement_target_id(t["role"], t.get("output_variant", "primary"))
            for t in active_driver_targets(topology))))
    names = ("base", *bank)
    assert len(names) == 4
    argv = ["run", "--program", "rear", "--poses", "rear/behind", "--candidates", ",".join(names), "--dry-run"]
    if repeats is not None:
        argv += ["--repeats", str(repeats)]
    opener = _opener()
    code, body = _run(argv, opener, monkeypatch, capsys)
    assert code == 0 and body["dry_run"] is True and body["issues"] == []
    assert not opener.requests
    poses = run_program("rear", "rear/behind").poses
    assert Counter((tuple(row["pose"]), row["candidate_id"]) for row in body["schedule"]) == {
        (pose.place, name): repeats or 1 for pose in poses for name in names}
    assert {row["regime"] for row in body["schedule"]} == {"summed"}
    assert body["mic_moves"] == 2
    assert len(body["schedule"]) == 8 * (repeats or 1)


def _run_opener(capture):
    opener = _opener()
    opener.pages[wc.STATUS_PATH] = json.dumps({"crossover_v2": {}, "capture": {"kind": "crossover_v2:session", "session_id": "run-1", **capture}})
    return opener


@pytest.mark.parametrize("status", ["awaiting_join", "running", *sorted(wc.SESSION_ENDED_STATUSES)])
@pytest.mark.parametrize("session_id", [None, "run-1"])
def test_stop_cancels_only_live_runs(status, session_id, monkeypatch, capsys):
    opener = _run_opener({"status": status, "session_id": session_id, "code": "user_stopped"})
    answer = {"capture": {"session_id": "run-1", "status": "stopping"}}
    opener.pages[wc.CAPTURE_CANCEL_PATH] = json.dumps(answer)
    code, body = _run(["stop", "--run", "run-1"], opener, monkeypatch, capsys)
    assert opener.requests[0].full_url.endswith(wc.STATUS_PATH)
    if status in wc.SESSION_ENDED_STATUSES and (session_id or status == "stopped"):
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


@pytest.mark.parametrize("verb", ["status", "wait"])
def test_commands_print_composed_measurement_lines(capsys, monkeypatch, verb):
    progress = {"measurement": 2, "measurements": 3, "pose": 2, "poses": 3, "sweep": 4, "sweeps_per_pose": [7, 7, 7], "pose_details": [{}, {}, {}], "role": "tweeter", "repeat": 2, "repeats": 3}
    client = SimpleNamespace(run_status=lambda run_id: (200, progress))
    if verb == "status":
        assert cli._cmd_status(client, SimpleNamespace(run="run-1")) == 0
    else:
        monkeypatch.setattr(cli, "wait_for_round", lambda *a, on_progress, **kw: (
            on_progress(progress), {"status": "terminal", "captured": False})[1])
        assert cli._cmd_wait(client, SimpleNamespace(run="run-1", timeout=1)) == 1
    output = capsys.readouterr()
    lines = round_lines(progress)
    assert output.err.splitlines()[:len(lines)] == lines
    if verb == "status":
        assert json.loads(output.out) == progress


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


def test_wait_publishes_operator_stop_reason(tmp_path):
    banked = round_bank.BankedRound(tmp_path, {})
    (tmp_path / "packet.json").write_text(json.dumps({"result": "partial", "reason": "user_stopped"}))
    answer = round_packet.wait_answer(banked, {"result": "failed", "reason": None}, verbose=False)
    assert answer["reason"] == "user_stopped"


@pytest.mark.parametrize("status", ["stopped", "failed", "complete", "awaiting_join", "running"])
@pytest.mark.parametrize("reason", [None, "user_stopped"])
def test_wizard_client_without_session_keeps_ended_status(status, reason):
    ended = status == "stopped" and reason is not None
    opener = _run_opener({"session_id": None, "status": status, "code": reason})
    client = wc.WizardClient(opener=opener)
    http, report = client.run_status("run-1")
    assert http == 200
    assert report["status"] == (status if ended else "starting")
    assert report["code"] == ("user_stopped" if ended else None)
    if ended:
        opener.requests.clear()
        clock = FakeWalkClock(max_sleeps=0)
        answer = wc.wait_for_round(client, run_id="run-1", timeout_s=20,
                                   now=clock.now, sleep=clock.sleep)
        assert answer == {**report, "status": "terminal"}
        assert len(opener.requests) == 1


@pytest.mark.parametrize("verb", ["wait", "status"])
@pytest.mark.parametrize("reason", ["user_stopped", "capture_clipped"])
def test_never_joined_end_reports_own_reason(verb, reason, monkeypatch, capsys):
    monkeypatch.setattr(correction_capture, "_capture_slot", None)
    monkeypatch.setattr(correction_capture, "_pending_capture",
                        (SimpleNamespace(label="crossover_v2:session"), None))
    stopped = correction_capture._request_capture_stop("crossover_v2:", reason)
    opener = _run_opener({**stopped, "session_id": None})
    lookup = Mock(side_effect=AssertionError("no capture bundle"))
    monkeypatch.setattr(cli, "_round_session_dir", lookup)
    code, body = _run([verb, "--run", "run-1", *(["--timeout", "0"] if verb == "wait" else [])],
                      opener, monkeypatch, capsys)
    expected = {"run_id": "run-1", "status": "stopped", "result": None, "pending": None,
                "current": None, "code": reason, "faults": [], "captured": False}
    if verb == "wait":
        assert code == cli.EXIT_REFUSED
        assert body == {"status": "refused", "reason": reason,
                        "detail": {**expected, "status": "terminal"}}
    else:
        assert code == cli.EXIT_OK and body == expected
    lookup.assert_not_called()
    assert len(opener.requests) == 1


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
    links = {"run_id": "run-1", "link": f"http://jts3.local{cli.CROSSOVER_PAGE_PATH}",
             "status_url": f"http://jts3.local{wc.STATUS_PATH}"}
    wait = cli.wait_for_round

    def wait_after_links(*args, **kwargs):
        captured = capsys.readouterr()
        assert captured.out == ""
        if verb != "wait":
            assert len(captured.err.splitlines()) == 1
            assert all(value in captured.err for value in links.values())
        return wait(*args, **kwargs)

    monkeypatch.setattr(cli, "wait_for_round", wait_after_links)
    (tmp_path / "frequency.png").touch()
    code, body = _run([*argv, timeout, "0", *(["--verbose"] if verbose else [])], opener, monkeypatch, capsys)
    assert code == 0 and calls == [(tmp_path, {
        "view_runner": run_bookkeeping,
    })]
    assert list(body)[:5] == ["result", "reason", "round_dir", "packet", "picture"]
    assert body == {"result": "complete", "reason": None, "round_dir": str(tmp_path),
                    "packet": str(tmp_path / "packet.json"), "picture": str(tmp_path / "frequency.png"),
                    **links,
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


def test_program_choices_include_rear():
    args = cli.build_parser().parse_args(["run", "--program", "rear"])
    assert args.program == "rear"


@pytest.mark.parametrize("poses, azimuths", [
    ("-30,-10,10,30", (-30, -10, 10, 30)),
    ("-20", (-20,)),
    ("rear/express", None),
])
def test_a_pose_set_reads_the_same_spaced_or_joined(poses, azimuths):
    spaced = cli.build_parser().parse_args(["run", "--program", "rear", "--poses", poses])
    joined = cli.build_parser().parse_args(["run", "--program", "rear", f"--poses={poses}"])
    assert spaced.poses == joined.poses == poses
    program = run_program("rear", spaced.poses)
    if azimuths is None:
        assert program.size != "custom"
    else:
        assert program.size == "custom"
        assert tuple(pose.azimuth_deg for pose in program.poses) == azimuths


@pytest.mark.parametrize("named", [False, True])
def test_a_rear_pair_run_composes_its_own_candidate_only_when_none_is_named(
    named, monkeypatch, tmp_path, isolated_candidate_bank,
):
    """The branches regime demands one candidate, and a rear pair take must
    measure the woofers raw: with no ``--candidates`` the run composes the
    applied tune with its rear calibration cleared (issue #5330).
    """
    from jasper.active_speaker import candidate_bank
    from jasper.active_speaker.crossover_v2 import prescription_document as prescription_document_mod
    from jasper import output_topology

    topology, _ = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    applied = publish_authored_candidate(
        candidate_from_design_draft(topology, load_design_draft(topology=topology))
    )
    monkeypatch.setattr(prescription_document_mod, "load_applied_baseline_profile_state",
                        lambda: {"status": "applied",
                                 "source": {"measured_candidate_fingerprint": applied.fingerprint}})
    monkeypatch.setattr(output_topology, "load_output_topology_strict", lambda: topology)
    monkeypatch.setattr(_run_request, "read_preflight_facts", lambda plan, **kw: ready_facts(
        plan, **kw, candidates={name: candidate_bank.find_banked_candidate(name).candidate
                          for name in plan.candidates}))
    argv = ["run", "--program", "rear", "--poses", "rear/pair",
            *(["--candidates", applied.fingerprint] if named else [])]

    plan = _run_request.resolve_run(cli.build_parser().parse_args(argv)).plan

    measured, = plan.candidates
    assert [stop.candidate_id for stop in plan.stops] == [measured] * len(plan.stops)
    assert {stop.regime for stop in plan.stops} == {"branches"}
    if named:
        assert measured == applied.fingerprint
        assert [row.fingerprint for row in candidate_bank.banked_candidates()] == [applied.fingerprint]
        return
    assert measured != applied.fingerprint
    composed = candidate_bank.find_banked_candidate(measured).candidate
    assert composed.analysis["resolution"]["rear_calibration"] == "cleared"
    assert composed.analysis["base"]["fingerprint"] == applied.fingerprint


@pytest.mark.parametrize("state", ["awaiting_join", "starting", "awaiting_capture", "stopping"])
def test_wait_does_not_bank_before_capture_cleanup(state, monkeypatch, capsys):
    opener = _run_opener({"status": state})
    code, body = _run(["wait", "--run", "run-1", "--timeout", "0"], opener, monkeypatch, capsys)
    assert code == 2
    assert body["reason"] == "wait_timeout"
    assert len(opener.requests) == 1


@pytest.mark.parametrize("argv,reason", [
    (["--repeats", "0"], "walk_level_policy_invalid"),
    (["--program", "room", "--mover", "arm"], "walk_mover_mismatch"),
])
def test_run_shape_refusal_is_json(preflight_ready, argv, reason, monkeypatch, capsys):
    code, body = _run(["run", "--wait", *argv], _opener(), monkeypatch, capsys)
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
    monkeypatch.setattr(_run_request, "read_preflight_facts", lambda plan, **kw: ready_facts(plan, **kw, candidates={candidate.fingerprint: candidate}))
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
    code = cli.main(["run", "--dry-run", "--base-url", address], opener=opener)
    body = json.loads(capsys.readouterr().out)
    assert code == 1 and body["reason"] == "dry_run_requires_local_host"
    assert not opener.requests


@pytest.mark.parametrize("dry_run", [False, True])
def test_bass_axis_uses_the_registered_mover(preflight_ready, monkeypatch, capsys, dry_run, arm_plan_answer):
    opener = _opener(session='{"session_id": "run-1"}')
    code, body = _run(["run", "--program", "bass", "--level-db", "-25", *(["--dry-run"] if dry_run else ["--wait", "--attest-rig-clear"])],
                      opener, monkeypatch, capsys)
    body = body if dry_run else body["schedule"]
    assert code == 0 and body["mic_moves"] == 1
    capture, = body["schedule"]
    assert capture["regime"] == "summed"
    assert body["level"]["level_db"] == -25
    assert not opener.requests if dry_run else "levels" not in json.loads(opener.posts()[0].data)


@pytest.mark.parametrize("program,noise_dbfs,levels", [
    ("bass", -60, [-33, -28, -23, -18]), ("bass", -100, [-33, -28, -23, -18]),
    ("bass", -20, [-33, -28, -23, -18]),
    ("bass", None, []),
])
def test_dry_run_lists_admissible_levels(monkeypatch, capsys, program, noise_dbfs, levels):
    def facts(plan, **kw):
        ready = ready_facts(plan, **kw)
        if noise_dbfs is None:
            return replace(ready, anchor=replace(ready.anchor, record={}))
        return replace(ready, anchor=replace(ready.anchor, record={**ready.anchor.record,
            "ambient_report": {"bands": [{"band_hz": [20, 80], "level_dbfs": noise_dbfs}]}}))

    monkeypatch.setattr(_run_request, "read_preflight_facts", facts)
    opener = _opener()
    code, body = _run(["run", "--program", program, "--dry-run"],
                      opener, monkeypatch, capsys)
    assert code == (0 if levels else 1)
    assert body["dry_run"] is True
    assert body["admissible_levels_db"] == levels
    expected = [None] * 4 if noise_dbfs is None else levels
    assert [row["offset_db"] for row in body["levels"]] == [level + 18 if level is not None else None for level in expected]
    assert [row["level_db"] for row in body["levels"]] == expected
    assert not opener.requests


def test_run_mover_flag_is_checked_against_registered_constraints(monkeypatch, capsys):
    seen = []

    def facts(plan, **kw):
        seen.append(plan.mover)
        return ready_facts(plan, **kw)

    monkeypatch.setattr(_run_request, "read_preflight_facts", facts)
    code, body = _run(
        ["run", "--program", "bass", "--mover", "human", "--dry-run"],
        _opener(), monkeypatch, capsys,
    )
    assert code == 1 and body["reason"] == "walk_mover_mismatch"

    for mover in ("arm", "human"):
        code, _ = _run(
            ["run", "--program", "speaker", "--mover", mover, "--dry-run"],
            _opener(), monkeypatch, capsys,
        )
        assert code == 0
    assert seen == ["arm", "human"]


@pytest.mark.parametrize("verb,flags,noise,levels", [
    ("run", ["--level-db", "-18"], -60, [-18]),
    ("trial", [], -60, [-18, -23, -28, -33]),
])
def test_bass_run_wait_banks_every_level_and_joins_only_multiple_levels(
    monkeypatch, capsys, tmp_path, box, bass_fit_pairs, tuning_profile, isolated_candidate_bank,
    verb, flags, noise, levels,
):
    from jasper.active_speaker import bundles, round_bank, plan_run
    from jasper.active_speaker.run_levels import LevelLadder, preflight_levels, prepare_level_captures
    from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore, EVIDENCE_ROOT
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
    from jasper.active_speaker.crossover_v2.round_inputs import round_inputs, default_out
    from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict
    from jasper.active_speaker.run_manifest import RunManifest
    from jasper.audio_measurement.calibration import MicSensitivity
    from jasper.active_speaker import round_bookkeeping, bass_table_inputs
    from jasper.web import correction_run_host as host, correction_crossover_v2_wired as wired
    from tests.active_speaker_fixtures import mono_output_topology
    from tests.engine_twin import FakeSeams
    from tests.crossover_v2_fixtures import _conductor, FakeSeams as FlowSeams
    from tests.test_plan_run import AnsweredGate, _analysis
    from tests.test_crossover_v2_tuning_scope import BASS_EXTENSION

    candidate = replace(_room_candidate(tuning_profile), bass_extension=BASS_EXTENSION,
                        analysis={"resolution": {"bass": "document"}, "measurement_status": "unmeasured"})
    join = Mock(wraps=bass_table_inputs.join_bass_rounds)
    monkeypatch.setattr(bass_table_inputs, "join_bass_rounds", join)
    publish_authored_candidate(candidate)
    def facts(plan, **kw):
        ready = ready_facts(plan, **kw, candidates={candidate.fingerprint: candidate})
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
                "loudness_volume_db": record["level_db"], "phase": record["program_phase"],
                "capture_integrity": {"spl": {"loudest_half_second_db_spl": 93 + record["level_db"],
                    "max_window_db_spl": 93 + record["level_db"], "ceiling_db_spl": 85}}})
        return replace(fakes, graph=kw["session_graph"], volume=kw["volume_claim"],
                       records=SimpleNamespace(bank=capture_record)).seams()

    opener = _run_opener({"status": "complete", "run": {"status": "complete"}})
    open_request = opener.open
    def open_and_execute(request, timeout=None):
        if request.full_url.endswith(wc.SESSION_PATH) and request.data:
            raw = json.loads(request.data)
            plan = AngleCaptureRequest.from_mapping(raw["plan"])
            report = preflight_levels(plan, facts(plan))
            plan = report.plan
            conductor = _conductor(FlowSeams())
            door, analyze, assessor, execute = host.bind_run_door(
                host=SimpleNamespace(_wired_stimulus_capture=lambda *a, **kw: None, bind_v2_engine_seams=engine),
                device=SimpleNamespace(model_key="minidsp_umik2"), evidence_store=store, manifest=manifest,
                production=SimpleNamespace(graph=fakes.graph, compose=None),
                conductor=conductor, refs={}, trims={},
                ceiling_s=30, ceiling_db_spl=85, camilla_factory=lambda: box,
                level=plan.level, ladder=report if isinstance(report, LevelLadder) else None,
            )
            runner = wired.build_v2_wired_run_and_consume(
                conductor, door=door, signals=plan_run.RunSignals(), position_gate=gate,
                ceiling_s=30, manifest=manifest, request=plan, analyze=analyze, assessor=assessor,
                captures=(prepare_level_captures if plan.levels else plan_run.prepare_plan_captures)(
                    plan, roles_bands=conductor._roles), execute=execute,
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
        selected = resolve_set(inputs, set_id, manifest=document)
        group = next(group for group in document["sets"] if group["set_id"] == selected.set_id)
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
    monkeypatch.setattr(round_bookkeeping, "run_bookkeeping", view)
    bank = round_bank.bank_round
    monkeypatch.setattr(round_bank, "bank_round", lambda path, **kw: bank(path, campaign_root=tmp_path / "campaigns", **kw))
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    argv = ["trial", candidate.fingerprint] if verb == "trial" else ["run", "--program", "bass", "--layout", "bass_axis"]
    code, body = _run([*argv, *flags, "--wait", "--attest-rig-clear"], opener, monkeypatch, capsys)
    assert code == 0, body
    expected = [(level, "lateral") for level in sorted(levels) for _ in range(2 if verb == "trial" else 1)]
    assert [(call["level_db"], call["spec"].program_phase) for call in fakes.play.calls] == expected
    assert len(gate.grants) == fakes.graph.restores == 1
    assert (box.volume_db, asyncio.run(box.get_loudness_volume_db())) == (entry_volume, entry_loudness)
    packet = json.loads(Path(body["packet"]).read_text())
    assert packet["result"] == "complete" and len(packet.get("runs", [packet])) == len(levels)
    assert Path(body["packet"]) == Path(body["round_dir"]) / "packet.json"
    assert {"sets", "series", "limits", "applied", "artifacts", "unavailable"} <= packet.keys()
    assert len(packet["artifacts"]["bass_views"]) == len(levels) * (2 if verb == "trial" else 1)
    assert len(packet["bass"]) == len(packet["artifacts"]["bass_views"])
    timing_sets = {group["set_id"] for group in json.loads(Path(packet["artifacts"]["manifest"]).read_text())["sets"]
                   if group["capture_basis"].get("graph_scope") == "timing"}
    assert {entry["set_id"] for entry in packet["bass"]} == {group["set_id"] for group in packet["sets"]} - timing_sets
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
    assert sorted(row["level_key"]["level_db"] for row in table["levels"]) == sorted(levels)


@pytest.mark.parametrize("source", ["flags", "plan"])
@pytest.mark.parametrize("dry_run,attested,available,reason", [
    (False, False, True, "walk_rig_clear_not_attested"),
    (False, True, False, "walk_mover_unavailable"),
    (True, True, False, "walk_mover_unavailable"),
])
def test_arm_preflight_refuses_before_opening(
    source, dry_run, attested, available, reason, arm_runtime, monkeypatch, capsys, tmp_path,
):
    arm_runtime.mover.available.return_value = available
    def facts(plan, **kw):
        return ready_facts(plan, **kw)
    monkeypatch.setattr(_run_request, "read_preflight_facts", facts)
    flags = ["--poses", "0", "--mover", "arm"]
    if source == "plan":
        plan = AngleCaptureRequest((AngleStop(0, "summed"),), mover="arm")
        path = tmp_path / "arm-plan.json"
        path.write_text(json.dumps(plan.to_dict()))
        flags = ["--plan", str(path)]
    opener = _opener()
    code, body = _run(["run", *flags, "--wait",
                      *(["--attest-rig-clear"] if attested else []),
                      *(["--dry-run"] if dry_run else [])], opener, monkeypatch, capsys)
    assert code == 1
    assert (body["issues"][0]["code"] if dry_run else body["code"]) == reason
    assert not opener.posts()
    assert not arm_runtime.mover.moves and not arm_runtime.threads


@pytest.mark.parametrize("command", ["run", "trial"])
def test_arm_requires_wait(command, monkeypatch, capsys):
    opener = _opener()
    with pytest.raises(SystemExit) as exc:
        cli.main([command, *([_FINGERPRINT] if command == "trial" else []),
                  "--mover", "arm", "--attest-rig-clear"], opener=opener)
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""
    assert not opener.requests


@pytest.mark.parametrize("ending", ["complete", "stopped", "timeout", "interrupt", "signal", "park_signal"])
def test_run_owns_arm_until_parked(ending, preflight_ready, arm_runtime, monkeypatch, capsys):
    started, ended = threading.Event(), threading.Event()
    if ending == "park_signal":
        real_event = threading.Event
        events = []
        def event():
            value = real_event()
            events.append(value)
            if len(events) == 2:
                original_wait = value.wait
                def wait_for_park(timeout=None):
                    value.wait = original_wait
                    assert not arm_runtime.threads[0].join.called
                    signal.raise_signal(signal.SIGINT)
                    return original_wait(timeout)
                value.wait = wait_for_park
            return value
        monkeypatch.setattr(aw.threading, "Event", event)
    trail = arm_runtime.trail
    emit = trail.emit
    def record(action, **kw):
        emit(action, **kw)
        if action == "up":
            started.set()
    monkeypatch.setattr(trail, "emit", record)
    monkeypatch.setattr(trail, "close", ended.set)
    if ending in {"complete", "stopped"}:
        arm_runtime.session.side_effect = lambda **kw: LiveThen(_COMPLETE if ending == "complete" else _STOPPED)
    else:
        arm_runtime.session.side_effect = lambda **kw: FakeSession([_IN_FLIGHT_QUIET])
    def wait(*args, **kw):
        assert started.wait(2)
        if ending in {"complete", "stopped"}:
            assert ended.wait(2)
            return {"status": "terminal", "captured": ending == "complete"}
        if ending == "interrupt":
            raise KeyboardInterrupt
        if ending == "signal":
            raise SystemExit(aw.EXIT_INTERRUPTED_PARKED)
        return {"status": "timeout", "reason": "wait_timeout"}
    monkeypatch.setattr(cli, "wait_for_round", wait)
    monkeypatch.setattr(cli, "_round_session_dir", lambda _: "/bank")
    monkeypatch.setattr(round_bank, "finish_round", lambda _: (SimpleNamespace(path=Path("/bank")), None))
    monkeypatch.setattr(round_packet, "wait_answer", lambda *a, **kw: {"round_dir": "/bank"})
    monkeypatch.setattr(cli, "packet_lines", lambda _: [])
    argv = ["run", "--poses", "0", "--mover", "arm", "--wait", "--attest-rig-clear",
            "--base-url", "http://127.0.0.1:8080", "--hostname", "jts.local"]
    opener = _opener(session='{"session_id": "run-1"}')
    if ending in {"interrupt", "signal", "park_signal"}:
        with pytest.raises(KeyboardInterrupt if ending == "interrupt" else SystemExit):
            cli.main(argv, opener=opener)
    else:
        code, body = _run(argv, opener, monkeypatch, capsys)
        assert code == {"complete": 0, "stopped": 1, "timeout": 2}[ending]
        arm = body["arm"] if ending == "complete" else body["detail"]["arm"]
        assert set(arm) == {"exit", "summary"}
        if ending in {"complete", "stopped"}:
            assert arm["exit"] == ("ok" if ending == "complete" else "session_stopped")
    assert "attest_rig_clear" not in json.loads(opener.posted_to(wc.SESSION_PATH)[0].data)
    worker, = arm_runtime.threads
    assert not worker.daemon and not worker.is_alive()
    worker.join.assert_called_once_with()
    assert arm_runtime.mover.moves[-1] == 0
    assert trail.one("parked")["ok"] is True
    assert trail.one("up")["rig_clear_attested"] is True
    arm_runtime.factory.assert_called_with(attest_rig_clear=True)
    arm_runtime.session.assert_called_once_with(host_header="jts.local", base_url="http://127.0.0.1:8080")
    arm_runtime.install.assert_called_once_with()


@pytest.mark.parametrize("flags", [[], ["--mover", "arm"]])
def test_arm_dry_run_needs_neither_wait_nor_attestation(flags, preflight_ready, arm_runtime, monkeypatch, capsys):
    opener = _opener()
    code, body = _run(["run", "--program", "bass", "--dry-run", *flags], opener, monkeypatch, capsys)
    assert code == 0 and body["dry_run"] is True
    assert "walk_rig_clear_not_attested" not in {issue["code"] for issue in body["issues"]}
    assert body["levels"] and not opener.requests and not arm_runtime.threads
    arm_runtime.mover.available.assert_called_once_with()
    arm_runtime.install.assert_not_called()


@pytest.mark.parametrize("mover", ["arm", "human"])
def test_cli_discovers_only_the_resolved_arm(mover, preflight_ready, arm_runtime, monkeypatch, capsys):
    code, _ = _run(["run", "--program", "speaker", "--dry-run", "--mover", mover], _opener(), monkeypatch, capsys)
    assert code == 0
    assert arm_runtime.mover.available.call_count == int(mover == "arm")


def test_arm_park_timeout_prints_one_unreadable_answer(preflight_ready, arm_runtime, monkeypatch, capsys):
    original_enter = aw.RunOwnedArm.__enter__
    def enter(arm):
        original_enter(arm)
        monkeypatch.setattr(arm._finished, "wait", Mock(return_value=False))
        return arm
    monkeypatch.setattr(aw.RunOwnedArm, "__enter__", enter)
    monkeypatch.setattr(cli, "wait_for_round", lambda *a, **kw: {"status": "terminal", "captured": True})
    code, body = _run(["run", "--poses", "0", "--mover", "arm", "--wait", "--attest-rig-clear"],
                      _opener(session='{"session_id": "run-1"}'), monkeypatch, capsys)
    assert code == 2 and body["code"] == body["reason"] == "arm_park_unconfirmed"
    assert body["status"] == "unreadable" and body["detail"]["arm"]["exit"] == "arm_park_unconfirmed"
    for worker in arm_runtime.threads:
        worker.join(2)
        assert not worker.is_alive()
