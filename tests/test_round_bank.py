# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bank live sessions and read the resulting evidence through its consumers."""

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

import asyncio
from dataclasses import replace
import errno
import json
import re
from itertools import combinations
from unittest.mock import Mock

from pathlib import Path

import pytest

from jasper.audio_measurement.gating import f_trusted_floor_hz
from jasper.active_speaker import baseline_profile as bp
from jasper.active_speaker.applied_identity import layer_fingerprints
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.candidate_bank import publish_authored_candidate
from jasper.active_speaker.frequency_reference import band_limited_curve
from jasper.active_speaker.frequency_view import FrequencyRun, build_frequency_view, frequency_series
from jasper.active_speaker.crossover_v2 import evidence_packet
from jasper.active_speaker.crossover_v2.evidence_packet import EVIDENCE_KEY, EVIDENCE_NOT_BANKED
from jasper.cli import crossover_prescriber
from jasper.cli.round_views import main as round_views_main
from jasper.active_speaker.round_bookkeeping import run_bookkeeping
from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_CYCLE_FILENAME,
    take_artifact_path,
)
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.audio_measurement.evidence_reasons import (
    CAPTURE_UNREADABLE_SIDECAR, TAKE_CURVES_NOT_BANKED, EvidenceUnavailable, unavailable,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    CAPTURE_STATE_FILENAME, RoundSetRefused, RoundViewsError, packet_purposes, resolve_set, round_artifact_dir,
    round_inputs,
)
from jasper.active_speaker.crossover_v2.round_views import load_banked_round
from jasper.active_speaker.crossover_v2.round_inputs import INDEX_FILENAME
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from tests.run_manifest_fixture import manifest_set, write_bundle_manifest, write_manifest
from tests.test_crossover_v2_round_frequency_view import summed_capture_bundle  # noqa: F401
from jasper.active_speaker import angle_capture, measurement_programs, round_view_artifacts
from jasper.active_speaker.round_view_artifacts import bookkeeping_views

from jasper.active_speaker.round_bank import (
    REASON_NOT_A_BUNDLE,
    REASON_SESSION_UNFINISHED,
    RoundBankError,
    bank_round,
)

from tests.crossover_v2_banked_round import bank_measure_round, bank_seat_round
from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.test_crossover_v2_driver_prescription import _draft


def _live_session(tmp_path: Path, *, state: str = "applied") -> tuple[Path, Path]:
    source = bank_measure_round(tmp_path / "live")
    session_dir = round_inputs(source).session_dir
    mark_state(session_dir, state)
    return session_dir, source / "state.json"


def _ssot(tmp_path: Path, *, present: bool, absent: str = "") -> dict[str, Path]:
    paths = {
        "design_draft_path": tmp_path / "ssot" / "design_draft.json",
        "applied_profile_path": tmp_path / "ssot" / "applied_profile.json",
        "repeat_floor_path": tmp_path / "ssot" / "repeat_floor.json",
        "declared_geometry_path": tmp_path / "ssot" / "declared_geometry.json",
        "statefile_path": tmp_path / "ssot" / "statefile.yml",
    }
    if present:
        for key, path in paths.items():
            if key == absent:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"kind": path.stem}))
    return paths


@pytest.mark.parametrize("present, absent, missing", [
    (True, "", []),
    (False, "", ["design-draft.json", "applied-profile.json", "repeat-floor.json",
                 "declared-geometry.json", "camilla-statefile.yml"]),
    (True, "repeat_floor_path", ["repeat-floor.json"]),
    (True, "declared_geometry_path", ["declared-geometry.json"]),
    (True, "statefile_path", ["camilla-statefile.yml"]),
])
def test_banked_tree_is_the_one_round_views_reads(tmp_path, present, absent, missing):
    session_dir, state_path = _live_session(tmp_path)

    banked = bank_round(
        session_dir,
        campaign_root=tmp_path / "campaigns",
        state_path=state_path,
        **_ssot(tmp_path, present=present, absent=absent),
    )

    assert banked.path == tmp_path / "campaigns" / "r1"
    assert round_inputs(banked.path).session_dir.name == session_dir.name
    assert (banked.path / "bundle" / session_dir.name / "info.json").is_file()
    assert load_banked_round(banked.path).session_dir == round_inputs(banked.path).session_dir
    provenance = json.loads((banked.path / "provenance.json").read_text())
    assert provenance == banked.provenance
    assert provenance["source"] == "on-box"
    assert provenance["session_id"] == session_dir.name
    assert provenance["missing"] == missing
    for name in (
        "state.json",
        "design-draft.json",
        "applied-profile.json",
        "repeat-floor.json",
        "declared-geometry.json",
        "camilla-statefile.yml",
    ):
        assert (banked.path / name).is_file() is (name not in missing)


def test_the_banked_round_carries_its_own_pose_index(tmp_path):
    session_dir, state_path = _live_session(tmp_path)

    banked = bank_round(
        session_dir,
        campaign_root=tmp_path / "campaigns",
        state_path=state_path,
        **_ssot(tmp_path, present=False),
    )

    document = json.loads((banked.path / POSITION_CYCLE_FILENAME).read_text())
    assert [(take["position_deg"], take["vertical_deg"], take["take_id"]) for take in document["takes"]] == [
        (7, 0, "lateral_03_a01")]
    assert POSITION_CYCLE_FILENAME not in banked.provenance["missing"]


def test_a_round_with_no_walk_to_index_is_banked_without_one(tmp_path):
    session_dir, state_path = _live_session(tmp_path)
    for take in session_dir.rglob("positions/lateral_*.json"):
        take.unlink()

    banked = bank_round(
        session_dir,
        campaign_root=tmp_path / "campaigns",
        state_path=state_path,
        **_ssot(tmp_path, present=False),
    )

    assert POSITION_CYCLE_FILENAME in banked.provenance["missing"]
    assert not (banked.path / POSITION_CYCLE_FILENAME).exists()
    assert (banked.path / "provenance.json").is_file()
    assert round_inputs(banked.path).session_dir.name == session_dir.name


def test_a_take_banked_before_its_pose_kind_is_named_and_the_round_still_banks(tmp_path):
    """The pose index and the stored evidence are best-effort: each names the
    field the take lacks, and neither unwinds the round (#2902)."""
    session_dir, state_path = _live_session(tmp_path)
    take = next(session_dir.rglob("positions/lateral_*.json"))
    take.write_text(json.dumps({key: value for key, value in json.loads(take.read_text()).items() if key != "pose_kind"}))

    banked = bank_round(
        session_dir,
        campaign_root=tmp_path / "campaigns",
        state_path=state_path,
        **_ssot(tmp_path, present=False),
    )

    packet = json.loads((banked.path / "packet.json").read_text())
    assert POSITION_CYCLE_FILENAME in banked.provenance["missing"]
    assert [(row["reason"], row["detail"]["field"]) for row in packet["unavailable"]
            if row["artifact"] == "evidence"] == [(TAKE_CURVES_NOT_BANKED, "pose_kind")]


@pytest.mark.parametrize(
    "sha, git_absent", [("abc1234", False), (None, True)], ids=["sha", "no-sha"]
)
def test_provenance_records_the_installed_build_or_says_it_cannot(
    tmp_path, monkeypatch, sha, git_absent
):
    session_dir, state_path = _live_session(tmp_path)
    monkeypatch.setattr(
        "jasper.active_speaker.round_bank._detect_build_sha", lambda: sha
    )

    banked = bank_round(
        session_dir,
        campaign_root=tmp_path / "campaigns",
        state_path=state_path,
        **_ssot(tmp_path, present=False),
    )

    assert banked.provenance["installed_sha"] == sha
    assert banked.provenance["git_absent"] is git_absent
    assert banked.provenance["banked_at_utc"].endswith("Z")


def test_a_banked_round_is_never_overwritten(tmp_path):
    session_dir, state_path = _live_session(tmp_path)
    kwargs = {
        "campaign_root": tmp_path / "campaigns",
        "state_path": state_path,
        **_ssot(tmp_path, present=False),
    }
    first = bank_round(session_dir, **kwargs)

    assert bank_round(session_dir, **kwargs) == first
    assert (first.path / "provenance.json").is_file()


def test_a_round_its_bookkeeping_refuses_leaves_no_half_built_round(tmp_path):
    session_dir, state_path = _live_session(tmp_path)
    write_manifest(session_dir, program="close/woofer")

    with pytest.raises(measurement_programs.UnknownPresetError):
        bank_round(session_dir, campaign_root=tmp_path / "campaigns", state_path=state_path)

    assert not any((tmp_path / "campaigns").iterdir())


def test_a_directory_that_is_not_a_bundle_is_refused(tmp_path):
    not_a_bundle = tmp_path / "empty"
    not_a_bundle.mkdir()

    with pytest.raises(RoundBankError) as excinfo:
        bank_round(not_a_bundle, campaign_root=tmp_path / "campaigns")

    assert excinfo.value.reason == REASON_NOT_A_BUNDLE
    assert not (tmp_path / "campaigns").exists()


@pytest.mark.parametrize("state", ["open", "proposal_ready"])
def test_an_unfinished_session_is_refused_rather_than_claiming_its_round_id(
    tmp_path, state
):
    session_dir, state_path = _live_session(tmp_path, state=state)

    with pytest.raises(RoundBankError) as excinfo:
        bank_round(
            session_dir, campaign_root=tmp_path / "campaigns", state_path=state_path
        )

    assert excinfo.value.reason == REASON_SESSION_UNFINISHED
    assert not (tmp_path / "campaigns").exists()


@pytest.mark.parametrize("snapshot", [True, False])
def test_delayed_bank_preserves_capture_state_without_borrowing_a_later_round(
    tmp_path, monkeypatch, snapshot,
):

    session, state_path = _live_session(tmp_path)
    calibration = {"measure": {"calibration_id": "capture-1"}}
    if snapshot:
        monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: session.parent)
        from tests.crossover_v2_fixtures import FakeSeams, _conductor

        conductor = _conductor(FakeSeams())
        conductor.session_id = "capture-1"
        v2state.set_state_path_for_tests(state_path)
        try:
            v2state.persist_conductor_state(conductor, failure_code=None, evidence={
                "bundle_session_id": session.name, "calibration": calibration})
        finally:
            v2state.set_state_path_for_tests(None)
        assert json.loads((session / CAPTURE_STATE_FILENAME).read_text())["session_id"] == "capture-1"
    state_path.write_text(json.dumps({
        "session_id": "capture-B", "verify": {"outcome": "fail"},
        "evidence": {"calibration": {"measure": {"calibration_id": "capture-B"}}},
    }))
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state_path)
    packet = load_banked_round(banked.path).packet
    assert packet["session"]["capture_session_id"] == "capture-1"
    assert ("state.json" in banked.provenance["missing"]) is not snapshot

    def banked_calibration():
        state = round_inputs(banked.path).state_path
        return json.loads(state.read_text())["evidence"]["calibration"] if state else {}

    assert banked_calibration() == (calibration if snapshot else {})
    (banked.path / "state.json").write_text(state_path.read_text())
    assert banked_calibration() == (calibration if snapshot else {})

@pytest.mark.parametrize("fallback", [None, errno.EXDEV, errno.EPERM, errno.EACCES])
def test_banking_hard_links_the_bundle_and_copies_where_a_link_is_refused(tmp_path, monkeypatch, fallback):
    session, state = _live_session(tmp_path / "live")
    if fallback:
        def denied(*args, **kwargs):
            raise OSError(fallback, "link unavailable")
        monkeypatch.setattr("jasper.active_speaker.round_bank.os.link", denied)
    bank = bank_round(session, campaign_root=tmp_path / "bank", state_path=state, **_ssot(tmp_path, present=False))
    bundle = round_inputs(bank.path).session_dir
    for source in session.rglob("*"):
        if source.is_file():
            copy = bundle / source.relative_to(session)
            assert copy.read_bytes() == source.read_bytes()
            assert (copy.stat().st_ino == source.stat().st_ino) is (fallback is None)


@pytest.mark.parametrize("view,reason", [("unregistered-view", "verb_not_registered"), ("compare", "inputs_required")])
def test_bookkeeping_unavailable_does_not_fail_the_bank(tmp_path, monkeypatch, view, reason):
    session, state = _live_session(tmp_path)
    artifacts, _ = round_artifact_dir(session)
    (artifacts / RUN_MANIFEST_FILENAME).write_text(json.dumps({"preset": "room/seat", "run_id": session.name}))
    monkeypatch.setattr(round_view_artifacts, "bookkeeping_views", lambda program, **kwargs: ((view, False, False),))
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state, view_runner=run_bookkeeping)
    assert banked.provenance["views"] == [{"view": view, "status": "unavailable", "reason": reason}]
    assert Path(banked.provenance["manifest"]).is_file()


@pytest.mark.parametrize("real_set", [False, True])
def test_bank_keeps_an_aggregate_view_beside_timing_evidence(tmp_path, real_set):
    session, state = _live_session(tmp_path)
    groups = [{"set_id": "timing", "capture_basis": {"graph_scope": "timing"}, "takes": []}]
    if real_set:
        groups.append({"set_id": "speaker", "capture_basis": {"graph_scope": "drivers"}, "takes": []})
    write_manifest(session, program="speaker", groups=groups)
    calls = []

    def run(view, target, *, set_id=None, incumbent=None):
        calls.append(set_id)
        assert resolve_set(round_inputs(target), set_id).set_id == "speaker"
        return {"view": view, "status": "written"}

    banked = bank_round(session, campaign_root=tmp_path / "bank", state_path=state, view_runner=run if real_set else None)
    assert calls == ([None] if real_set else [])
    answer = {"status": "written"} if real_set else {"status": "unavailable", "reason": "view_runner_unavailable"}
    assert banked.provenance["views"] == [{"view": "frequency", **answer}]
    if not real_set:
        with pytest.raises(RoundSetRefused) as refused:
            resolve_set(round_inputs(banked.path))
        assert refused.value.reason == "round_set_unknown"


@pytest.mark.parametrize("purpose,expected", [
    ("speaker", ("frequency",)),
    ("room", ("room", "room-grade", "bass", "frequency")),
    ("bass", ("bass", "frequency")),
])
def test_bank_runs_the_programs_registered_views(tmp_path, capsys, purpose, expected):
    session, state = _live_session(tmp_path)
    write_manifest(session, program=purpose)
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state, view_runner=run_bookkeeping)
    views = banked.provenance["views"]
    assert tuple(row["view"] for row in views) == expected
    for row in views:
        if row["status"] == "written":
            assert Path(row["out"]).is_file()
        else:
            assert row["status"] == "unavailable" and row["reason"]
            assert row["reason"] not in {"inputs_required", "verb_not_registered"}
    assert {row["view"] for row in views if row["status"] == "written"} <= _present(banked.path, capsys)
    if purpose == "speaker":
        packet = json.loads((banked.path / "packet.json").read_text())
        assert packet["room"] == packet["bass"] == []


def _present(round_dir: Path, capsys: pytest.CaptureFixture[str]) -> set[str]:
    """The views whose artifact ``catalog`` finds beside the round."""
    capsys.readouterr()
    assert round_views_main(["catalog", str(round_dir)]) == 0
    return {call["argv"][1] for tool in json.loads(capsys.readouterr().out)["tools"]
            for call in tool["calls"] if call["present"]}


@pytest.mark.parametrize("purpose,base", [("room", False), ("room", True), ("speaker", True), ("rear/seat", True)])
def test_bank_fans_out_views_with_the_base(tmp_path, request, capsys, purpose, base):
    groups = [{"set_id": "base", "base": True, "capture_basis": {"candidate_id": "base-graph"},
               "takes": []}] if base else []
    groups += [{"set_id": f"trial-{i}", "base": False, "capture_basis": {"candidate_id": f"trial-{i}"},
                "takes": []} for i in range(1 if purpose == "rear/seat" else 2)]
    trials = [group for group in groups if not group["base"]]
    if purpose == "rear/seat":
        session, _, _, bank = request.getfixturevalue("summed_capture_bundle")
        state = None
        for group in groups:
            records = []
            for index, pose in enumerate(measurement_programs.preset("rear/seat").poses):
                record_id = asyncio.run(bank(
                    f"{group['set_id']}-{index}", candidate=group["capture_basis"]["candidate_id"],
                    phase="lateral", measurement_purpose="rear", gating_applied=False,
                    pose_kind=pose.kind, seat_offset_m=pose.seat_offset_m, vertical_deg=0, mark_distance_m=1.0,
                ))
                records.append((record_id, json.loads(take_artifact_path(session, record_id).read_text())))
            group.update(manifest_set(records, set_id=group["set_id"]))
        mark_state(session, "applied")
    else:
        session, state = _live_session(tmp_path)
    write_manifest(session, program=purpose, groups=groups)
    calls = []

    def run(view, target, *, set_id=None, incumbent=None):
        calls.append((view, set_id, incumbent))
        return (run_bookkeeping(view, target, set_id=set_id, incumbent=incumbent) if purpose == "rear/seat"
                else {"view": view, "status": "written"})

    banked = bank_round(session, campaign_root=tmp_path / "bank", state_path=state, view_runner=run)
    if purpose == "speaker":
        assert calls == [("frequency", None, None)]
    else:
        assert calls == [("room", row["set_id"], None) for row in groups] + [
            ("room-grade", row["set_id"], None) for row in trials if base] + [
            ("bass", row["set_id"], None) for row in groups] + [
            (view, None, None) for view in (("rear", "frequency") if purpose == "rear/seat" else ("frequency",))]
        assert [{key: row[key] for key in ("view", "set_id", "status", "incumbent_set_id", "reason") if key in row}
                for row in banked.provenance["views"] if row["view"] == "room-grade"] == [
            {"view": "room-grade", "set_id": row["set_id"], **(
                {"status": "written", "incumbent_set_id": "base"} if base else
                {"status": "unavailable", "reason": "room_incumbent_set_unavailable"})} for row in trials]
    if purpose == "rear/seat":
        packet = json.loads((banked.path / "packet.json").read_text())
        assert len(packet["room"]) == len(packet["bass"]) == 2 and packet["rear"]
        sets = {row["set_id"]: row["candidate_id"] for row in packet["sets"]}
        assert {sets[row["set_id"]] for row in packet["room"]} == {"base-graph", "trial-0"}
        for entry in packet["room"]:
            assert entry["median"]["n_positions"] == 3
            assert set(entry["median"]["evidence"]["take_ids"]) == {f"{entry['set_id']}-{index}" for index in range(3)}
        assert _present(banked.path, capsys) >= {"room", "room-grade"}


def test_the_in_room_round_files_the_room_and_the_bass_view_from_one_seat_set(tmp_path, request):
    """The in-room round's base plays with bass and room cleared, and its bank files the room view
    and the bass view from that one seat set, so the round counts for both programs (ADR-0429)."""
    plan = angle_capture.request_for_preset(measurement_programs.run_preset("room/seat"))
    cleared, = {angle_capture.played_layers(stop) for stop in plan.stops}
    assert set(cleared) == {"bass_extension", "room_correction"}
    session, _, _, bank = request.getfixturevalue("summed_capture_bundle")
    records = []
    for index, stop in enumerate(plan.stops):
        record_id = asyncio.run(bank(
            f"seat-{index}", phase="lateral", measurement_purpose="room", gating_applied=False,
            pose_kind=stop.pose.kind, seat_offset_m=stop.pose.seat_offset_m, vertical_deg=0, mark_distance_m=1.0,
        ))
        records.append((record_id, json.loads(take_artifact_path(session, record_id).read_text())))
    write_manifest(session, program="room/seat",
                   groups=[{**manifest_set(records, set_id="base", cleared_layers=cleared), "base": True}])
    mark_state(session, "applied")

    banked = bank_round(session, campaign_root=tmp_path / "bank", view_runner=run_bookkeeping)

    packet = json.loads((banked.path / "packet.json").read_text())
    (room,), (bass,) = packet["room"], packet["bass"]
    assert (room["set_id"], bass["set_id"]) == ("base", "base")
    assert set(room["median"]["evidence"]["take_ids"]) == {take["record"]["take_id"] for take in bass["takes"]} == {
        f"seat-{index}" for index in range(len(plan.stops))}
    assert packet_purposes(packet) == ("room", "bass")


@pytest.mark.parametrize("purpose,view", [
    (purpose, view)
    for purpose in ("speaker", "room", "bass")
    for view, _, _ in bookkeeping_views((purpose,))
])
def test_every_bookkeeping_view_writes_from_one_run(tmp_path, monkeypatch, request, purpose, view):
    from tests.test_active_speaker_crossover_v2_round_views import _make_round_dir

    monkeypatch.chdir(tmp_path)
    if purpose == "bass":
        target, _, _, bank = request.getfixturevalue("summed_capture_bundle")
        asyncio.run(bank("baseline", measurement_purpose=purpose))
        write_manifest(target, program=purpose)
    elif purpose == "room":
        target = bank_seat_round(tmp_path)
        if view == "room-grade":
            assert run_bookkeeping("room", target)["status"] == "written"
    else:
        target = _make_round_dir(tmp_path, "run", take=True)
        write_manifest(target, program=purpose)
    answer = run_bookkeeping(view, target)
    assert answer["status"] == "written", answer
    assert Path(answer["out"]).is_file()
    if view == "frequency":
        if answer["image"] is None:
            assert answer["reason"] == "plots_extra_missing"
        else:
            assert Path(answer["image"]) == target / "frequency.png"
            assert Path(answer["image"]).read_bytes().startswith(b"\x89PNG")
        assert len(answer["series"]) == (7 if purpose == "room" else 1)


@pytest.mark.parametrize("purpose", ["room", "bass"])
@pytest.mark.parametrize("failed", [False, True])
def test_packet_keeps_program_analysis_views_limits_and_series_stats(tmp_path, request, purpose, failed):
    if purpose == "room":
        source = bank_seat_round(tmp_path / "source")
    else:
        source, _, _, bank = request.getfixturevalue("summed_capture_bundle")
        asyncio.run(bank("baseline", measurement_purpose=purpose))
        write_manifest(source, program=purpose)
    inputs = round_inputs(source)
    mark_state(inputs.session_dir, "applied")

    def views(view, target, **kwargs):
        answer = run_bookkeeping(view, target, **kwargs)
        if view == purpose:
            assert answer["status"] == "written", answer
            if failed:
                return {**answer, "status": "unavailable", "reason": "analysis_fixture_unavailable"}
        return answer

    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        view_runner=views, **_ssot(tmp_path, present=False))
    packet = json.loads((banked.path / "packet.json").read_text())
    assert packet["preset"] == purpose and packet["fits"] == []
    assert packet["bass" if purpose == "room" else "room"] == []
    assert "verdicts" not in packet
    pointers = packet["artifacts"][f"{purpose}_views"]
    assert {view["view"] for view in pointers} == ({"room", "room-grade"} if purpose == "room" else {"bass"})
    pointer, = [view for view in pointers if view["view"] == purpose]
    document = json.loads(Path(pointer["out"]).read_text())
    if failed:
        assert packet[purpose] == []
        assert pointer["status"] == "unavailable" and pointer["reason"] == "analysis_fixture_unavailable"
    else:
        entry, = packet[purpose]
        assert entry == {**{key: value for key, value in document.items() if purpose != "room" or key != "limits"},
                         "set_id": packet["sets"][0]["set_id"], "out": pointer["out"]}
        assert entry["set_id"] in packet["limits"]
        if purpose == "room":
            assert entry["room_median_sha256"] == document["room_median_sha256"]
            assert entry["median"]["ceiling_hz"] == document["median"]["ceiling_hz"]
            assert entry["median"]["n_positions"] == document["median"]["n_positions"] == 7
        else:
            take, = entry["takes"]
            saved, = document["takes"]
            assert take["record"]["take_id"] == "baseline" and take["record"]["level_db"] == -20
            assert len(take["bands"]) == len(saved["bands"]) > 0
            assert [band["fundamental_qualified"] for band in take["bands"]] == [
                band["fundamental_qualified"] for band in saved["bands"]]
    candidates = {group["set_id"]: group["candidate_id"] for group in packet["sets"]}
    assert all(candidates.values())
    assert len(packet["series"]) == (7 if purpose == "room" else 1)
    for series in packet["series"]:
        assert series["set_id"] in packet["limits"]
        assert series["candidate_id"] == candidates[series["set_id"]]
        assert series["stats"]["flatness_rms_db"]["value"] < 0.5
        assert abs(series["stats"]["tilt_db_per_decade"]["value"]) < 0.5
        assert series["stats"]["band_means_db"] and series["stats"]["low_end_means_db"]
        if purpose == "room":
            assert series["pose"]["kind"] == "seat"
    if purpose == "room":
        limits, = packet["limits"].values()
        assert limits["bounds"]["freqs_hz"] and limits["bounds"]["taper_knee_hz"] is not None
        assert len(limits["bounds"]["cut_floor_db"]) == len(limits["bounds"]["freqs_hz"])
    index = (banked.path / INDEX_FILENAME).read_text().splitlines()
    assert f"Fingerprint: {packet['packet_fingerprint']}" in index
    for series in packet["series"]:
        assert any(f"candidate {series['candidate_id']}; set {series['set_id']}; take {series['take_id']}" in line for line in index)
    heads = ("Measured:", "Applied:", "Result:", "## Decisions", "decision:", "gate ", "series ",
             "## Artifacts", "## Tools", "Fingerprint:")
    positions = [next(i for i, line in enumerate(index) if line.startswith(head)) for head in heads]
    assert positions == sorted(positions)


@pytest.mark.parametrize("probe", [False, True], ids=["", "probed"])
def test_a_one_set_bass_round_is_re_run_where_the_bank_filed_its_view(tmp_path, request, capsys, probe):
    """A round of one view set, with or without its run probe's set beside it, is banked with its bass view
    under no set name. The set names it all the same: a re-run files there, with --set and without it."""
    source, _, _, bank = request.getfixturevalue("summed_capture_bundle")
    asyncio.run(bank("baseline", measurement_purpose="bass"))
    write_manifest(source, program="bass", probe=probe)
    inputs = round_inputs(source)
    mark_state(inputs.session_dir, "applied")
    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        view_runner=run_bookkeeping, **_ssot(tmp_path, present=False))
    set_id = resolve_set(round_inputs(banked.path)).set_id
    filed, = (Path(row["out"]) for row in json.loads((banked.path / "packet.json").read_text())["artifacts"]["bass_views"])
    for flags in ([], ["--set", set_id]):
        assert round_views_main(["bass", str(banked.path), *flags]) == 0
        assert Path(json.loads(capsys.readouterr().out)["out"]) == filed


@pytest.mark.parametrize("stored_evidence", [True, False, "old-schema"], ids=["stored", "not-stored", "old-schema"])
def test_a_round_answers_with_the_packet_its_bank_stored(tmp_path, monkeypatch, capsys, stored_evidence):
    """A round banked beside it later moves what a rebuild would read, so
    nothing rebuilds a banked round's packet (ADR-0371): one whose packet.json
    holds no evidence refuses by that key (ADR-0383), and so does one another
    packet schema wrote (#2902). ``status`` reads the code into each gap."""
    session, state = _live_session(tmp_path)
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state)
    path = banked.path / "packet.json"
    packet = json.loads(path.read_text())
    monkeypatch.setattr(evidence_packet, "build_crossover_evidence_packet", Mock(side_effect=AssertionError))
    if stored_evidence is not True:
        path.write_text(json.dumps({**packet, "schema": "jts_round_packet/3"} if stored_evidence else
                                   {key: value for key, value in packet.items() if key != EVIDENCE_KEY}))
        with pytest.raises(RoundViewsError) as refused:
            evidence_packet.round_evidence(round_inputs(banked.path))
        assert refused.value.code == EVIDENCE_NOT_BANKED
        assert crossover_prescriber.main(["status", str(banked.path)]) == 0
        status = json.loads(capsys.readouterr().out)
        gaps = (status["declared"], status["banked"], status["banked"]["walk"], status["banked"]["classification"],
                status["applied"]["from_applied_profile"])
        assert [(gap["status"], gap["reason"]) for gap in gaps] == [("unavailable", EVIDENCE_NOT_BANKED)] * len(gaps)
        assert all(gap["detail"] for gap in gaps)
        return
    later = bank_measure_round(tmp_path / "campaigns", name="r2-later")
    artifacts, _ = round_artifact_dir(round_inputs(later).session_dir)
    (artifacts / "candidate.json").write_text(json.dumps({"alignment": {"delay_us": 125.0}}))

    assert crossover_prescriber.main(["status", str(banked.path)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["packet_fingerprint"] == packet["packet_fingerprint"] is not None
    assert status["contracts"] == packet[EVIDENCE_KEY]["contracts"]


@pytest.mark.parametrize("named_by", ["bank", "bundle"])
@pytest.mark.parametrize("verb", ["status", "judge"])
@pytest.mark.parametrize("stale", [False, True], ids=["current", "stale"])
def test_a_banked_round_says_whether_its_stored_contracts_are_current(tmp_path, capsys, stale, verb, named_by):
    """A contract code change can move the contracts from the ones the bank stored (ADR-0371)."""
    session, state = _live_session(tmp_path)
    draft = tmp_path / "design-draft.json"
    draft.write_text(json.dumps(_draft()))
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state, design_draft_path=draft)
    path = banked.path / "packet.json"
    packet = json.loads(path.read_text())
    now = packet[EVIDENCE_KEY]["contracts"]
    if stale:
        packet[EVIDENCE_KEY]["contracts"] = {**now, next(iter(now)): "0" * 64}
        path.write_text(json.dumps(packet))
    root = tmp_path / "candidates"
    base = publish_authored_candidate(replace(_candidate(), analysis={"measurement_status": "unmeasured"}), root=root)
    document = tmp_path / "prescription.json"
    document.write_text(json.dumps({"kind": "jts_prescription", "schema": 1, "base": base.fingerprint,
                                    "rationale": "none", "sections": {}}))
    named = banked.path if named_by == "bank" else round_inputs(banked.path).session_dir
    argv = ["status", str(named)] if verb == "status" else [
        "judge", str(document), "--round", str(named), "--root", str(root)]

    assert crossover_prescriber.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["packet_contracts"] == {
        "contract_current": not stale, "stored": packet[EVIDENCE_KEY]["contracts"], "now": now}


@pytest.mark.parametrize("verb", ["contract", "judge", "status"])
def test_a_prescriber_verb_takes_a_banked_round_by_its_id_as_by_its_directory(tmp_path, monkeypatch, capsys, verb):
    """One verb for each round argument: ``contract --round``, ``judge --round`` and status's positional."""
    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.chdir(tmp_path)
    session, state = _live_session(tmp_path)
    draft = tmp_path / "design-draft.json"
    draft.write_text(json.dumps(_draft()))
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state, design_draft_path=draft)
    root = tmp_path / "candidates"
    base = publish_authored_candidate(replace(_candidate(), analysis={"measurement_status": "unmeasured"}), root=root)
    document = tmp_path / "prescription.json"
    document.write_text(json.dumps({"kind": "jts_prescription", "schema": 1, "base": base.fingerprint,
                                    "rationale": "none", "sections": {}}))
    argv = {"contract": ["contract", "--round"], "status": ["status"],
            "judge": ["judge", str(document), "--root", str(root), "--round"]}[verb]

    answers = []
    for ref in (banked.path.name, str(banked.path)):
        assert crossover_prescriber.main([*argv, ref]) == 0
        answers.append(json.loads(capsys.readouterr().out))

    assert answers[0] == answers[1]


@pytest.mark.parametrize("contents", [None, "{"], ids=["missing", "corrupt"])
def test_packet_skips_unreadable_written_room_artifact(tmp_path, contents):
    session, state = _live_session(tmp_path)
    manifest = write_manifest(session, program="room")
    artifact = tmp_path / "room.json"
    if contents is not None:
        artifact.write_text(contents)
    pointer = {"view": "room", "status": "written", "out": str(artifact)}

    def views(view, target, **kwargs):
        return pointer if view == "room" else {
            "view": view, "status": "unavailable", "reason": "view_runner_unavailable",
        }

    banked = bank_round(session, campaign_root=tmp_path / "bank", state_path=state,
                        view_runner=views, **_ssot(tmp_path, present=False))
    packet = json.loads((banked.path / "packet.json").read_text())
    assert packet["room"] == []
    assert {**pointer, "set_id": manifest["sets"][0]["set_id"]} in packet["artifacts"]["room_views"]


def test_a_kept_take_whose_record_cannot_be_read_is_listed_with_the_gap(tmp_path):
    """One unreadable record never costs the round (#5737 C1b): the packet lists
    its take unselected, with the gap as its record, beside the takes it read."""
    session, state = _live_session(tmp_path)
    take = {"take_id": "lost", "curves": [], "selected": True, "pose": {"kind": "seat"}}
    manifest = write_manifest(session, program="room", groups=[{
        "set_id": "set", "base": True, "capture_basis": {"candidate_id": "base"},
        "takes": [take, {**take, "take_id": "kept"}]}])
    record_id = manifest["sets"][0]["takes"][0]["record_id"]
    take_artifact_path(session, record_id).write_text("{")

    banked = bank_round(session, campaign_root=tmp_path / "bank", state_path=state)

    listed = {row["take_id"]: row for row in json.loads((banked.path / "packet.json").read_text())["sets"][0]["takes"]}
    assert (listed["lost"]["selected"], listed["lost"]["record"]) == (
        False, unavailable(CAPTURE_UNREADABLE_SIDECAR, {"record": record_id}))
    assert listed["kept"]["selected"] and "record" not in listed["kept"]


def test_the_bank_tags_every_series_and_draws_the_kept_takes(tmp_path, monkeypatch):
    """Every banked take's series carries its selection; the bank's picture and
    its index draw the takes the round kept (#5737 C1b)."""
    source = bank_seat_round(tmp_path / "source")
    session = round_inputs(source).session_dir
    refused = next(document["take_id"] for _, document in measurement_documents(session))
    write_bundle_manifest(session, program="room", refused={refused})
    mark_state(session, "applied")
    drawn: list[str] = []
    monkeypatch.setattr("jasper.active_speaker.round_view_builders.render_frequency_view",
                        lambda view, path, *, selected, **kwargs: drawn.extend(selected))

    banked = bank_round(session, campaign_root=tmp_path / "bank", state_path=source / "state.json",
                        view_runner=run_bookkeeping)

    packet = json.loads((banked.path / "packet.json").read_text())
    assert {series["take_id"] for series in packet["series"] if not series["selected"]} == {refused}
    view = json.loads((banked.path / "frequency_view.json").read_text())
    curves = {f"{run['slot']}:{curve['id']}": curve for run in view["runs"] for curve in run["series"]}
    assert drawn and all(curves[selector]["selected"] for selector in drawn)
    listed = [line for line in (banked.path / INDEX_FILENAME).read_text().splitlines() if line.startswith("series ")]
    assert listed and not any(f"take {refused};" in line or line.endswith(f"take {refused}") for line in listed)


@pytest.mark.parametrize("window,level,ripple,expected", [
    (None, 7, [0, 0, 0, 0], 0), (None, -3, [-1, 1, -1, 1], 1),
    (7.0, 7, [0, 0, 0, 0], 0), (7.0, -3, [-1, 1, -1, 1], 1),
])
def test_packet_stats_measure_flatness_about_the_series_mean(tmp_path, window, level, ripple, expected):

    session, state = _live_session(tmp_path)
    group = {"set_id": "set", "base": True, "capture_basis": {"candidate_id": "base"},
             "takes": [{"take_id": "take", "curves": [], "selected": True, "pose": {"kind": "seat"}}]}
    write_manifest(session, program="room", groups=[group])
    applied = {"kind": bp.BASELINE_PROFILE_KIND, "artifact_schema_version": bp.SCHEMA_VERSION,
               "source": {"measured_candidate_fingerprint": "a123456789bc" + "0" * 52},
               "config": {"sha256": "123456789abc" * 5 + "1234", "path": "/config.yml"},
               "status": "applied", "applied_at": "2026-09-13T12:00:00Z", "recomposition_snapshot": {
                   "linearization": {"woofer": [{"freq": 300, "gain": -2, "q": 1}]},
                   "room_correction": {"left": [{"freq": 80, "gain": -3, "q": 2}]},
                   "bass_extension": {"enabled": True}}}
    paths = _ssot(tmp_path, present=True)
    paths["applied_profile_path"].write_text(json.dumps(applied))

    def views(view, target, **kwargs):
        if view == "frequency":
            series = frequency_series(series_id="series", label="seat", kind="measured", role="summed",
                                      take_id="take", freqs_hz=[100, 200, 400, 1000, 4000, 10000],
                                      magnitude_db=[100, -100, *[level + value for value in ripple]],
                                      gate_window_ms=window, window="ungated" if window is None else "gated",
                                      reference_db=0, smoothing_fractional_octave=6)
            (target / "frequency_view.json").write_text(json.dumps(build_frequency_view(FrequencyRun(
                id="run", measurement_family="room", series=(series,)))))
        return {"view": view, "status": "unavailable"}

    banked = bank_round(session, campaign_root=tmp_path / "bank", state_path=state, view_runner=views, **paths)
    packet = json.loads((banked.path / "packet.json").read_text())
    stats = packet["series"][0]["stats"]
    assert stats["flatness_rms_db"] == {
        "value": pytest.approx(expected, abs=0.01),
        "band_hz": [f_trusted_floor_hz(window / 1000) if window else 400.0, 10000],
    }
    if window:
        assert abs(stats["tilt_db_per_decade"]["value"]) < 1
        assert stats["tilt_db_per_decade"]["below_trusted_floor"] is False
    assert stats["low_end_means_db"]["20_30"]["value"] is None
    assert packet["applied"] == {
        "candidate": "a123456789bc" + "0" * 52, "record": "123456789abc", "config_path": "/config.yml",
        "applied_at": applied["applied_at"], "layer_fingerprints": layer_fingerprints(applied),
        "layers": {"driver": True, "room": True, "bass": True, "rear": False},
    }
    match = re.search(r"^Applied: candidate ([0-9a-f]{12}) · record ([0-9a-f]{12}) · (.+)$",
                      (banked.path / INDEX_FILENAME).read_text(), re.MULTILINE)
    assert match is not None
    assert match.groups()[:2] == (packet["applied"]["candidate"][:12], packet["applied"]["record"])
    assert len(packet["applied"]["candidate"]) == 64
    assert json.loads(match[3]) == packet["applied"]["layers"]


@pytest.mark.parametrize("purpose", ["room", "speaker"])
def test_a_banked_take_shows_each_window_it_banked(request, tmp_path, purpose):
    """A take the gate read banks both windows, whatever its purpose; the
    bank's frequency view and packet show each one, named by its window,
    read from the record alone (ADR-0400)."""
    bundle, _, _, bank = request.getfixturevalue("summed_capture_bundle")
    record_id = asyncio.run(bank("candidate-take", candidate="trial-fp", exempt=None, measurement_purpose=purpose,
                                 vertical_deg=0, mark_distance_m=1.0))
    curves = json.loads(take_artifact_path(bundle, record_id).read_text())["curves"]
    group, = write_manifest(bundle, program=purpose)["sets"]
    mark_state(bundle, "applied")
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}

    banked = bank_round(bundle, campaign_root=tmp_path / "bank", view_runner=run_bookkeeping,
                        **_ssot(tmp_path, present=False))

    series = json.loads((banked.path / "frequency_view.json").read_text())["runs"][0]["series"]
    packet = json.loads((banked.path / "packet.json").read_text())
    assert [(row["window"], row["label"].rsplit(" · ", 1)[-1]) for row in series] == [
        ("gated", "Gated"), ("ungated", "Ungated")]
    assert [(row["window"], row["set_id"], row["take_id"]) for row in packet["series"]] == [
        (window, group["set_id"], "candidate-take") for window in ("gated", "ungated")]
    assert [row["gate_window_ms"] is None for row in series] == [False, True]
    for row, curve in zip(series, curves):
        assert (row["freqs_hz"], row["magnitude_db"]) == tuple(map(list, band_limited_curve(curve)))
    assert all(p.read_bytes() == content for p, content in before.items())


def test_candidates_reads_every_pose_and_window_of_a_banked_trial(request, tmp_path):
    bundle, _, _, bank = request.getfixturevalue("summed_capture_bundle")
    candidates = ("baseline-fp", "candidate-a", "candidate-b")
    groups = []
    for candidate in candidates:
        records = []
        for position in (-20, 0, 20):
            record_id = asyncio.run(bank(
                f"{candidate}-{position}", candidate=candidate, phase="lateral",
                exempt=None, measurement_purpose="room", position_deg=position,
                vertical_deg=0, mark_distance_m=1.0,
                capture_gain_db=6.0 if candidate == "candidate-b" else 0.0,
            ))
            record = json.loads(take_artifact_path(bundle, record_id).read_text())
            assert [curve["window"] for curve in record["curves"]] == ["gated", "ungated"]
            records.append((record_id, record))
        group = manifest_set(records)
        group["base"] = candidate == candidates[0]
        for take in group["takes"]:
            take["role"] = "summed"
        groups.append(group)
    write_manifest(bundle, program="room", groups=groups)
    mark_state(bundle, "applied")
    root = bank_round(bundle, campaign_root=tmp_path / "bank", view_runner=run_bookkeeping,
                      **_ssot(tmp_path, present=False)).path
    view = json.loads((root / "frequency_view.json").read_text())
    for wav in root.rglob("*.wav"):
        wav.unlink()
    assert round_views_main(["candidates", str(root)]) == 0
    document = json.loads((root / "candidates.json").read_text())
    assert document["summary"]["candidates"] == list(candidates)
    assert (document["summary"]["poses"], document["summary"]["pairs"]) == (3, 18)
    assert [table["azimuth_deg"] for table in document["tables"]] == [-20, 0, 20]
    for table in document["tables"]:
        assert table["played"] == list(candidates)
        assert {row["window"] for row in table["roles"]} == {
            curve["window"] for curve in view["runs"][0]["series"]} == {"gated", "ungated"}
        for row in table["roles"]:
            assert (row["role"], row["trusted"]) == ("summed", row["window"] == "gated")
            assert [c["candidate_id"] for c in row["candidates"]] == list(candidates)
            assert [(d["a"], d["b"]) for d in row["deltas"]] == list(combinations(candidates, 2))
            for delta in row["deltas"]:
                assert delta["bins"] > 0
                assert [delta[k] for k in ("mean_abs_db", "max_abs_db", "rms_db")] == pytest.approx([0] * 3, abs=0.05)
                if table["azimuth_deg"] == 0 and row["window"] == "gated" and delta["a"] == "candidate-a":
                    assert delta["b"] == "candidate-b"
                    assert delta["level_offset_db"] == pytest.approx(-6.0, abs=0.05)


@pytest.mark.parametrize("failure", [
    None, OSError("disk unavailable"), EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {"field": "curves", "role": "woofer"}),
], ids=["banked", "disk", "evidence"])
def test_finish_round_banks_packet_or_records_save_failure(tmp_path, monkeypatch, failure):
    """A bank that fails, on the disk or on evidence that refuses by code, is
    recorded on the run manifest and handed back, never raised (#5737 C1b)."""
    from jasper.active_speaker import round_bank

    def bank(*args, **kwargs):
        if failure is not None:
            raise failure
        return round_bank.BankedRound(tmp_path, {})
    monkeypatch.setattr(round_bank, "bank_round", bank)
    manifest = tmp_path / "evidence/v1/artifacts/crossover_v2/run" / RUN_MANIFEST_FILENAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"status": "complete", "sets": []}))
    logged = Mock()
    monkeypatch.setattr(round_bank, "log_event", logged)
    banked, error = round_bank.finish_round(tmp_path)
    assert (banked is None, error) == (failure is not None, failure)
    if banked:
        assert banked.path == tmp_path
    written = json.loads(manifest.read_text())
    assert written.pop("packet_error_detail", None) == (logged.call_args.kwargs["detail"] if failure else None)
    assert written == {"status": "complete", "sets": []}
    if failure is None:
        logged.assert_not_called()
