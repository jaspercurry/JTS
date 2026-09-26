# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bank live sessions and read the resulting evidence through its consumers."""

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

import asyncio
import errno
import json
import re
import hashlib
import wave
from itertools import combinations
from unittest.mock import Mock

import numpy as np
from pathlib import Path

import pytest

from jasper.audio_measurement.gating import f_trusted_floor_hz
from jasper.audio_measurement.program_analysis import analyze_program_capture
from jasper.active_speaker import baseline_profile as bp
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.frequency_view import FrequencyRun, build_frequency_view, frequency_series
from jasper.active_speaker.measurement_analysis import analyze_measurement_bundle
from jasper.active_speaker.crossover_v2 import gate_sweep
from jasper.cli.round_views import main as round_views_main
from jasper.active_speaker.round_bookkeeping import run_bookkeeping
from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_CYCLE_FILENAME,
    read_position_cycle,
    takes_by_position,
)
from jasper.active_speaker.crossover_v2.round_inputs import CAPTURE_STATE_FILENAME, RoundSetRefused, resolve_set, round_artifact_dir, round_inputs
from jasper.active_speaker.crossover_v2.round_views import load_banked_round
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_KEY, POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.feature_classifier import load_round_captures
from jasper.active_speaker.crossover_v2.harmonic_evidence import _bind_measure_captures, _scope_captures
from jasper.active_speaker.crossover_v2.evidence_packet.offline_reads import round_program_dir
from jasper.attribution.session_identity import read_session_identity
from jasper.active_speaker.crossover_v2.round_inputs import INDEX_FILENAME
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.test_crossover_v2_round_frequency_view import summed_capture_bundle  # noqa: F401
from jasper.active_speaker import measurement_programs, round_view_artifacts
from jasper.active_speaker.round_view_artifacts import bookkeeping_views

from jasper.active_speaker.round_bank import (
    CAPTURE_RING_DIR,
    REASON_NOT_A_BUNDLE,
    REASON_SESSION_UNFINISHED,
    SKIP_NO_CAPTURED_AT,
    SKIP_NO_PHASE,
    SKIP_NO_WAV_PATH,
    SKIP_WAV_ESCAPES_BUNDLE,
    SKIP_WAV_MISSING,
    RoundBankError,
    bank_round,
)

from tests.crossover_v2_banked_round import bank_executor_take, bank_measure_round, bank_seat_round


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

    document = read_position_cycle(banked.path / POSITION_CYCLE_FILENAME)
    assert takes_by_position(document) == {(7, 0): ("lateral_03_a01",)}
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

    with pytest.raises(measurement_programs.UnknownProgramError):
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


@pytest.mark.parametrize("round_id", [".", "..", "../escape", "a/b"])
def test_a_round_id_that_is_not_a_plain_token_falls_back_to_the_session_id(
    tmp_path, round_id
):
    session_dir, state_path = _live_session(tmp_path)
    round_dir, _why = round_artifact_dir(session_dir)
    receipt_path = round_dir / "round_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["round_id"] = round_id
    receipt_path.write_text(json.dumps(receipt))

    banked = bank_round(
        session_dir, campaign_root=tmp_path / "campaigns", state_path=state_path
    )

    assert banked.path == tmp_path / "campaigns" / session_dir.name


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
    assert packet["entry_baseline"]["available"] is True
    assert packet["identity"]["calibration"] == (calibration if snapshot else {})
    assert packet["verify"]["available"] is False
    assert ("state.json" in banked.provenance["missing"]) is not snapshot
    (banked.path / "state.json").write_text(state_path.read_text())
    reread = load_banked_round(banked.path).packet
    assert reread["identity"]["calibration"] == (calibration if snapshot else {})
    assert reread["verify"]["available"] is False

SR = 48000

def _ring_wav(path: Path, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    samples = rng.normal(0, 0.2, SR // 10)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def _capture_bundle(root: Path, *, takes: tuple[tuple[str, str, object], ...]) -> Path:
    bundle = root / "bundle/bank-session"
    positions = bundle / "evidence/v1/artifacts/crossover_v2/capture-id/positions"
    positions.mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": "bank-session"}))
    programs = bundle / "crossover_v2/capture-id"
    for index, (take_id, phase, captured_at) in enumerate(takes):
        program = programs / f"{phase}_program.wav"
        _ring_wav(program, seed=index + 1)
        wav = bundle / f"summed/{take_id}.wav"
        _ring_wav(wav, seed=index + 2)
        (positions / f"{take_id}.json").write_text(json.dumps({
            "kind": POSITION_EVIDENCE_KIND, MEASURE_KIND_KEY: "verify",
            "take_id": take_id, "phase": phase, "captured_at": captured_at,
            "session_id": "capture-id", "wav_path": str(wav.relative_to(bundle)),
            "wav_sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
            "diagnostic": {"epsilon_ppm": 1.0 + index},
            "capture_integrity": {"capture_chain": "alsa_s32le"},
            "frame_ledger": {"received_frames": 4800},
            "provenance": {"stimulus": {"wav_sha256": hashlib.sha256(program.read_bytes()).hexdigest()}},
        }))
    return bundle


def test_a_take_the_capture_host_banked_reaches_the_ring(tmp_path, monkeypatch):
    bank_executor_take(tmp_path, monkeypatch)
    session, = (tmp_path / "sessions").iterdir()
    mark_state(session, "closed")

    bank = bank_round(session, campaign_root=tmp_path / "bank", **_ssot(tmp_path, present=False))

    assert bank.provenance["capture_ring"] == {"written": 1, "skipped": []}


@pytest.mark.parametrize("fallback", [None, errno.EXDEV, errno.EPERM, errno.EACCES])
def test_banking_writes_the_ring_both_instruments_read(tmp_path, monkeypatch, fallback):
    session = _capture_bundle(tmp_path / "live", takes=(
        ("verify-a", "verify", "2026-08-31T00:19:52Z"),
        ("lateral-b", "lateral", 1788135592.4),
        ("measure-c", "measure", 1788135592.9),
    ))
    if fallback:
        def denied(*args, **kwargs):
            raise OSError(fallback, "link unavailable")
        monkeypatch.setattr("jasper.active_speaker.round_bank.os.link", denied)
    bank = bank_round(session, campaign_root=tmp_path / "bank", **_ssot(tmp_path, present=False))
    bundle = round_inputs(bank.path).session_dir
    for source in session.rglob("*"):
        if source.is_file():
            copy = bundle / source.relative_to(session)
            assert copy.read_bytes() == source.read_bytes()
            assert (copy.stat().st_ino == source.stat().st_ino) is (fallback is None)
    ring = bundle / CAPTURE_RING_DIR
    directory, _ = round_artifact_dir(bundle)
    programs = round_program_dir(bundle, directory, ("verify", "lateral", "measure"))
    captures = load_round_captures(programs, ring, session_id="bank-session")
    assert {c.phase for c in captures} == {"verify", "lateral"}
    assert {c.stamp for c in captures} == {1788135592.0}
    bound, scope = _scope_captures(_bind_measure_captures(ring), "bank-session")
    assert len(bound) == 1 and scope["session_id"] == "bank-session"
    assert bank.provenance["capture_ring"] == {"written": 3, "skipped": []}
    assert len(list((ring / "wav").glob("*.wav"))) == 3
    for sidecar in (ring / "sidecar").glob("*.json"):
        document = json.loads(sidecar.read_text())
        identity = read_session_identity(document)
        assert identity.session_id == "bank-session"
        assert identity.aliases["capture_session_id"] == "capture-id"
        assert all(key in document for key in ("diagnostic", "capture_integrity", "frame_ledger", "wav_sha256"))
        wav = ring / "wav" / f"{sidecar.stem}.wav"
        source = bundle / document["wav_path"]
        assert wav.read_bytes() == source.read_bytes()
        assert (wav.stat().st_ino == source.stat().st_ino) is (fallback is None)


@pytest.mark.parametrize("fault,reason", [
    ("timestamp", SKIP_NO_CAPTURED_AT), ("phase", SKIP_NO_PHASE),
    ("path", SKIP_NO_WAV_PATH), ("escape", SKIP_WAV_ESCAPES_BUNDLE),
    ("missing", SKIP_WAV_MISSING),
])
def test_banking_discloses_captures_missing_from_the_ring(tmp_path, fault, reason):
    session = _capture_bundle(tmp_path / "live", takes=(("take", "verify", "2026-08-31T00:19:52Z"),))
    take = next(session.glob("evidence/v1/artifacts/**/positions/*.json"))
    document = json.loads(take.read_text())
    if fault == "timestamp":
        document["captured_at"] = "invalid"
    elif fault == "phase":
        document.pop("phase")
    elif fault == "path":
        document.pop("wav_path")
    elif fault == "escape":
        document["wav_path"] = "../outside.wav"
    else:
        (session / document["wav_path"]).unlink()
    take.write_text(json.dumps(document))
    bank = bank_round(session, campaign_root=tmp_path / "bank", **_ssot(tmp_path, present=False))
    assert bank.provenance["capture_ring"]["written"] == 0
    assert bank.provenance["capture_ring"]["skipped"] == [{
        "path": "crossover_v2/capture-id/positions/take.json", "reason": reason,
    }]


@pytest.mark.parametrize("view,reason", [("unregistered-view", "verb_not_registered"), ("bass-compare", "inputs_required")])
def test_bookkeeping_unavailable_does_not_fail_the_bank(tmp_path, monkeypatch, view, reason):
    session, state = _live_session(tmp_path)
    artifacts, _ = round_artifact_dir(session)
    (artifacts / RUN_MANIFEST_FILENAME).write_text(json.dumps({"program": "bass/cloud", "run_id": session.name}))
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
    assert banked.provenance["views"] == [{"view": "inventory", **({"status": "written", "set_id": "speaker"} if real_set else
        {"status": "unavailable", "reason": "view_runner_unavailable"})}]
    if not real_set:
        with pytest.raises(RoundSetRefused) as refused:
            resolve_set(round_inputs(banked.path))
        assert refused.value.reason == "round_set_unknown"


@pytest.mark.parametrize("purpose,expected", [
    ("speaker", ("inventory",)),
    ("room", ("room", "room-grade", "frequency", "inventory")),
    ("bass", ("bass", "frequency", "inventory")),
])
def test_bank_runs_the_programs_registered_views(tmp_path, purpose, expected):
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
    assert views[-1]["status"] == "written"
    inventory = json.loads(Path(views[-1]["out"]).read_text())
    present = {row["view"] for row in inventory["artifacts"] if row["present"]}
    assert {row["view"] for row in views[:-1] if row["status"] == "written"} <= present
    if purpose == "speaker":
        packet = json.loads((banked.path / "packet.json").read_text())
        assert packet["room"] == packet["bass"] == []


@pytest.mark.parametrize("purpose,base", [("room", False), ("room", True), ("speaker", True), ("rear/seat", True)])
def test_bank_fans_out_views_with_the_base(tmp_path, request, purpose, base):
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
            for index, pose in enumerate(measurement_programs.program("rear", "seat").poses):
                record_id = asyncio.run(bank(
                    f"{group['set_id']}-{index}", candidate=group["capture_basis"]["candidate_id"],
                    phase="lateral", measurement_purpose="rear", gating_applied=False,
                    pose_kind=pose.kind, seat_offset_m=pose.seat_offset_m, vertical_deg=0, mark_distance_m=1.0,
                ))
                records.append((record_id, json.loads(gate_sweep.take_artifact_path(session, record_id).read_text())))
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
        assert calls == [("inventory", row["set_id"], None) for row in groups]
    else:
        assert calls == [("room", row["set_id"], None) for row in groups] + [
            ("room-grade", row["set_id"], None) for row in trials if base] + [
            (view, None, None) for view in (("rear", "frequency") if purpose == "rear/seat" else ("frequency",))] + [
            ("inventory", row["set_id"], None) for row in groups]
        assert [{key: row[key] for key in ("view", "set_id", "status", "incumbent_set_id", "reason") if key in row}
                for row in banked.provenance["views"] if row["view"] == "room-grade"] == [
            {"view": "room-grade", "set_id": row["set_id"], **(
                {"status": "written", "incumbent_set_id": "base"} if base else
                {"status": "unavailable", "reason": "room_incumbent_set_unavailable"})} for row in trials]
    if purpose == "rear/seat":
        packet = json.loads((banked.path / "packet.json").read_text())
        assert len(packet["room"]) == 2 and packet["rear"]
        sets = {row["set_id"]: row["candidate_id"] for row in packet["sets"]}
        assert {sets[row["set_id"]] for row in packet["room"]} == {"base-graph", "trial-0"}
        for entry in packet["room"]:
            assert entry["median"]["n_positions"] == 3
            assert set(entry["median"]["evidence"]["take_ids"]) == {f"{entry['set_id']}-{index}" for index in range(3)}
        for view in banked.provenance["views"]:
            if view["view"] == "inventory":
                inventory = json.loads(Path(view["out"]).read_text())
                assert {row["view"] for row in inventory["artifacts"] if row["present"]} >= {"room", "rear"}
                assert {row["view"] for row in inventory["artifacts"]} >= {"room", "room-grade"}


@pytest.mark.parametrize("purpose,view", [
    (purpose, view)
    for purpose in ("speaker", "room", "bass")
    for view, _, _ in bookkeeping_views(purpose)
])
def test_every_bookkeeping_view_writes_from_one_run(tmp_path, monkeypatch, request, purpose, view):
    from tests.test_active_speaker_crossover_v2_round_views import _make_round_dir, _flat_curve

    monkeypatch.chdir(tmp_path)
    if purpose == "bass":
        target, _, _, bank = request.getfixturevalue("summed_capture_bundle")
        asyncio.run(bank("baseline"))
        write_manifest(target, program=purpose)
    elif purpose == "room":
        target = bank_seat_round(tmp_path)
        if view == "room-grade":
            assert run_bookkeeping("room", target)["status"] == "written"
    else:
        target = _make_round_dir(tmp_path, "run", position_curves={
            "cloud_verify_02": ("onax", _flat_curve()),
            "cloud_verify_04": ("offax", _flat_curve(offset_db=-3)),
        }, position_degrees={"cloud_verify_02": 0, "cloud_verify_04": 20})
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
        assert len(answer["series"]) == (14 if purpose == "room" else 1)


@pytest.mark.parametrize("purpose", ["room", "bass"])
@pytest.mark.parametrize("failed", [False, True])
def test_packet_keeps_program_analysis_views_limits_and_series_stats(tmp_path, request, purpose, failed):
    if purpose == "room":
        source = bank_seat_round(tmp_path / "source")
    else:
        source, _, _, bank = request.getfixturevalue("summed_capture_bundle")
        asyncio.run(bank("baseline"))
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
    assert packet["program"] == purpose and packet["fits"] == []
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
    assert len(packet["series"]) == (14 if purpose == "room" else 1)
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


@pytest.mark.parametrize("window,level,ripple,expected", [
    (None, 7, [0, 0, 0, 0], 0), (None, -3, [-1, 1, -1, 1], 1),
    (7.0, 7, [0, 0, 0, 0], 0), (7.0, -3, [-1, 1, -1, 1], 1),
])
def test_packet_stats_measure_flatness_about_the_series_mean(tmp_path, window, level, ripple, expected):

    session, state = _live_session(tmp_path)
    group = {"set_id": "set", "base": True, "capture_basis": {"candidate_id": "base"},
             "takes": [{"take_id": "take", "role": "summed", "selected": True, "pose": {"kind": "seat"}}]}
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
                                      gate_window_ms=window,
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
        "applied_at": applied["applied_at"],
        "layers": {"driver": True, "room": True, "bass": True, "rear": False},
    }
    match = re.search(r"^Applied: candidate ([0-9a-f]{12}) · record ([0-9a-f]{12}) · (.+)$",
                      (banked.path / INDEX_FILENAME).read_text(), re.MULTILINE)
    assert match is not None
    assert match.groups()[:2] == (packet["applied"]["candidate"][:12], packet["applied"]["record"])
    assert len(packet["applied"]["candidate"]) == 64
    assert json.loads(match[3]) == packet["applied"]["layers"]


@pytest.mark.parametrize("purpose", ["room", "speaker"], ids=["trial", "speaker-room-sweep"])
def test_banked_candidate_has_gated_and_ungated_sum(request, tmp_path, monkeypatch, purpose):
    bundle, _, program, bank = request.getfixturevalue("summed_capture_bundle")
    record_id = asyncio.run(bank("candidate-take", candidate="trial-fp", gating_applied=False,
                                measurement_purpose="room", vertical_deg=0, mark_distance_m=1.0))
    _, wav = gate_sweep.reopen_measurement_capture(
        bundle, gate_sweep.take_artifact_path(bundle, record_id))
    samples, rate = gate_sweep.decode_wav_to_mono(wav)
    reference = analyze_program_capture(program, samples, rate).summed_response
    assert reference is not None and reference.gating["applied"]
    original, = analyze_measurement_bundle(bundle).series
    manifest = write_manifest(bundle, program=purpose)
    group, = manifest["sets"]
    take, = group["takes"]
    take.update(role="summed", curve={key: original.to_dict()[key] for key in (
        "freqs_hz", "magnitude_db", "gate_window_ms", "validity_floor_hz", "smoothing_fractional_octave",
    )})
    write_manifest(bundle, program=purpose, groups=[group])
    mark_state(bundle, "applied")
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    deconvolve = Mock(wraps=gate_sweep._deconvolve_window)
    monkeypatch.setattr(gate_sweep, "_deconvolve_window", deconvolve)
    banked = bank_round(bundle, campaign_root=tmp_path / "bank", view_runner=run_bookkeeping,
                        **_ssot(tmp_path, present=False))
    assert deconvolve.call_count == 1
    view = json.loads((banked.path / "frequency_view.json").read_text())
    ungated, gated = view["runs"][0]["series"]
    packet = json.loads((banked.path / "packet.json").read_text())
    assert len(packet["series"]) == 2
    for curves in ((ungated, gated), packet["series"]):
        assert [row["window"] for row in curves] == ["ungated", "gated"]
        assert {row["set_id"] for row in curves} == {group["set_id"]}
        assert {row["take_id"] for row in curves} == {"candidate-take"}
        assert {row["role"] for row in curves} == {"summed"}
        assert curves[0]["gate_window_ms"] is curves[0]["trusted_floor_hz"] is None
        assert curves[1]["gate_window_ms"] == reference.gating["window_ms"]
        assert curves[1]["trusted_floor_hz"] == reference.gating["f_trusted_hz"]
        assert curves[1]["floor_source"] == reference.gating["floor_source"]
    assert ungated["position"] == gated["position"]
    assert [row["pose"] for row in packet["series"]] == [take["pose"], take["pose"]]
    assert ungated["freqs_hz"] == list(original.freqs_hz)
    assert ungated["magnitude_db"] == list(original.magnitude_db)
    hz, db = np.asarray(gated["freqs_hz"]), np.asarray(gated["magnitude_db"])
    keep = (reference.freqs_hz >= gate_sweep.GRID_LO_HZ * 0.7) & (
        reference.freqs_hz <= gate_sweep.GRID_HI_HZ * 1.3)
    expected_db = np.interp(hz, reference.freqs_hz[keep], gate_sweep.smooth_fractional_octave(
        reference.freqs_hz[keep], reference.magnitude_db[keep], gated["smoothing_fractional_octave"]))
    np.testing.assert_array_equal(db, expected_db)
    band = db[(hz >= gated["trusted_floor_hz"]) & (hz <= 10000)]
    assert packet["series"][1]["stats"]["flatness_rms_db"] == {
        "band_hz": [gated["trusted_floor_hz"], 10000],
        "value": pytest.approx(float(np.std(band))),
    }
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
                gating_applied=False, measurement_purpose="room", position_deg=position,
                vertical_deg=0, mark_distance_m=1.0,
                capture_gain_db=6.0 if candidate == "candidate-b" else 0.0,
            ))
            record = json.loads(gate_sweep.take_artifact_path(bundle, record_id).read_text())
            assert "curves" not in record
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
    assert [table["deg"] for table in document["tables"]] == [-20, 0, 20]
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
                if table["deg"] == 0 and row["window"] == "gated" and delta["a"] == "candidate-a":
                    assert delta["b"] == "candidate-b"
                    assert delta["level_offset_db"] == pytest.approx(-6.0, abs=0.05)


@pytest.mark.parametrize("failure", [False, True])
def test_finish_round_banks_packet_or_records_save_failure(tmp_path, monkeypatch, failure):
    from jasper.active_speaker import round_bank

    def bank(*args, **kwargs):
        if failure:
            raise OSError("disk unavailable")
        return round_bank.BankedRound(tmp_path, {})
    monkeypatch.setattr(round_bank, "bank_round", bank)
    manifest = tmp_path / "evidence/v1/artifacts/crossover_v2/run" / RUN_MANIFEST_FILENAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"status": "complete", "sets": []}))
    logged = Mock()
    monkeypatch.setattr(round_bank, "log_event", logged)
    banked, error = round_bank.finish_round(tmp_path)
    assert (banked is None) == failure
    assert isinstance(error, OSError) if failure else error is None
    if banked:
        assert banked.path == tmp_path
    assert json.loads(manifest.read_text()) == {"status": "complete", "sets": [],
        **({"packet_error_detail": "OSError: disk unavailable"} if failure else {})}
    if failure:
        assert logged.call_args.kwargs["detail"] == json.loads(manifest.read_text())["packet_error_detail"]
    else:
        logged.assert_not_called()
