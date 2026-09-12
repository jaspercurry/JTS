# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bank live sessions and read the resulting evidence through its consumers."""

from __future__ import annotations

import errno
import json
import hashlib
import wave

import numpy as np
from pathlib import Path

import pytest

from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_CYCLE_FILENAME,
    read_position_cycle,
    takes_by_position,
)
from jasper.active_speaker.crossover_v2.round_inputs import CAPTURE_STATE_FILENAME, round_inputs
from jasper.active_speaker.crossover_v2.round_views import load_banked_round
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_KEY, POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.feature_classifier import load_round_captures
from jasper.active_speaker.crossover_v2.harmonic_evidence import _bind_measure_captures, _scope_captures
from jasper.active_speaker.crossover_v2.evidence_packet import round_program_dir
from jasper.attribution.session_identity import read_session_identity
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

from tests.crossover_v2_banked_round import bank_measure_round


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
    from jasper.web import correction_crossover_v2 as host

    session, state_path = _live_session(tmp_path)
    if snapshot:
        monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: session.parent)
        from tests.crossover_v2_fixtures import FakeSeams, _conductor

        conductor = _conductor(FakeSeams())
        conductor.session_id = "capture-1"
        conductor._set_verify_outcome("pass", None, {})
        host.set_state_path_for_tests(state_path)
        try:
            host.persist_conductor_state(conductor, failure_code=None, evidence={"bundle_session_id": session.name})
        finally:
            host.set_state_path_for_tests(None)
        assert json.loads((session / CAPTURE_STATE_FILENAME).read_text())["session_id"] == "capture-1"
    state_path.write_text(json.dumps({"session_id": "capture-B", "verify": {"outcome": "fail"}}))
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state_path)
    packet = load_banked_round(banked.path).packet
    assert packet["session"]["capture_session_id"] == "capture-1"
    assert packet["entry_baseline"]["available"] is True
    assert packet["verify"]["available"] is snapshot
    if snapshot:
        assert packet["verify"]["outcome"] == "pass"
    else:
        assert "state.json" in banked.provenance["missing"]
    (banked.path / "state.json").write_text(state_path.read_text())
    assert load_banked_round(banked.path).packet["verify"]["available"] is snapshot

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


@pytest.mark.parametrize("view,reason", [("room", "verb_not_registered"), ("bass-compare", "inputs_required")])
def test_bookkeeping_unavailable_does_not_fail_the_bank(tmp_path, monkeypatch, view, reason):
    from jasper.active_speaker import measurement_programs
    from jasper.cli.round_views import run_bookkeeping
    from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
    session, state = _live_session(tmp_path)
    artifacts, _ = round_artifact_dir(session)
    (artifacts / RUN_MANIFEST_FILENAME).write_text(json.dumps({"program": "bass/cloud", "run_id": session.name}))
    monkeypatch.setattr(measurement_programs, "bookkeeping_views", lambda program: (view,))
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state, view_runner=run_bookkeeping)
    assert banked.provenance["views"] == [{"view": view, "status": "unavailable", "reason": reason}]
    assert Path(banked.provenance["manifest"]).is_file()


@pytest.mark.parametrize("purpose", ["speaker", "room", "bass"])
def test_bank_runs_the_programs_registered_views(tmp_path, purpose):
    from jasper.active_speaker.measurement_programs import bookkeeping_views
    from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
    from jasper.cli.round_views import run_bookkeeping
    session, state = _live_session(tmp_path)
    artifacts, _ = round_artifact_dir(session)
    (artifacts / RUN_MANIFEST_FILENAME).write_text(json.dumps({"program": purpose, "run_id": session.name}))
    banked = bank_round(session, campaign_root=tmp_path / "campaigns", state_path=state, view_runner=run_bookkeeping)
    views = banked.provenance["views"]
    assert tuple(row["view"] for row in views) == bookkeeping_views(purpose)
    for row in views:
        if row["status"] == "written":
            assert Path(row["out"]).is_file()
        else:
            assert row["status"] == "unavailable" and row["reason"]
    if purpose == "speaker":
        assert views[0]["status"] == "written"
