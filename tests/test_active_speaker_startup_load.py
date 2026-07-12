# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import jasper.active_speaker.startup_load as startup_load_mod
from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.path_safety import (
    build_startup_load_path_safety_evidence,
    write_path_safety_evidence,
)
from jasper.active_speaker.staging import stage_protected_startup_config
from jasper.active_speaker.startup_load import (
    STARTUP_LOAD_PREFLIGHT_KIND,
    build_startup_load_preflight,
    load_protected_startup_config,
    load_startup_load_state,
    rollback_protected_startup_config,
)
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology


class FakeCamilla:
    def __init__(self, current_path: str) -> None:
        self.current_path = current_path
        self.loaded_paths: list[str] = []

    async def get_config_file_path(self) -> str:
        return self.current_path

    async def set_config_file_path(self, path: str) -> bool:
        self.current_path = path
        self.loaded_paths.append(path)
        return True


class SnapshotFailingCamilla(FakeCamilla):
    async def get_config_file_path(self) -> str:
        raise RuntimeError("camilla unavailable")


def _record_reconcile_triggers(monkeypatch, *, ok: bool = True) -> list[dict]:
    calls: list[dict] = []

    def fake_manage_units(*units: str, **kwargs):
        calls.append({"units": units, **kwargs})
        return {"ok": ok, "rc": 0 if ok else 3}

    monkeypatch.setattr(startup_load_mod, "manage_units", fake_manage_units)
    return calls


def _topology(*, identity_verified: bool = True) -> OutputTopology:
    return mono_output_topology(identity_verified=identity_verified)


def _valid_config(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(
        status=ValidationStatus.VALID,
        path=str(path),
    )


def _staged(tmp_path: Path) -> dict:
    return stage_protected_startup_config(
        _topology(),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )


def _protected_prior(tmp_path: Path, staged: dict, name: str = "prior_active.yml") -> Path:
    prior = tmp_path / name
    prior.write_text(
        Path(staged["config"]["path"]).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return prior


def _normal_prior(tmp_path: Path, name: str = "prior_stereo.yml") -> Path:
    prior = tmp_path / name
    prior.write_text(
        "# Source: jasper.sound.camilla_yaml.emit_sound_config\n"
        "devices:\n"
        "  volume_limit: 0\n"
        "  playback:\n"
        "    type: Alsa\n"
        "    device: outputd_content_playback\n"
        "    channels: 2\n",
        encoding="utf-8",
    )
    return prior


def _write_path_safety(
    path: Path,
    *,
    topology: OutputTopology | None = None,
    staged: dict,
    current_config_path: str | Path | None = None,
    require_physical_identity: bool = True,
) -> Path:
    evidence = build_startup_load_path_safety_evidence(
        topology or _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=current_config_path or staged["config"]["path"],
        require_physical_identity=require_physical_identity,
    )
    return write_path_safety_evidence(evidence, path=path)


def test_startup_load_preflight_blocks_without_path_safety(
    tmp_path: Path,
) -> None:
    report = build_startup_load_preflight(
        _topology(),
        staged_config=_staged(tmp_path),
        validate=_valid_config,
    )

    assert report["kind"] == STARTUP_LOAD_PREFLIGHT_KIND
    assert report["status"] == "blocked"
    assert report["load_allowed"] is False
    assert "path_safety_evidence_missing" in {
        issue["code"] for issue in report["issues"]
    }


def test_startup_load_preflight_requires_level_floor(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    report = build_startup_load_preflight(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(requested_level_dbfs=-70),
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "path_safety.json",
            staged=staged,
        ),
        validate=_valid_config,
    )

    assert report["status"] == "blocked"
    assert report["calibration_level"]["at_floor"] is False
    assert "calibration_level_not_at_floor" in {
        issue["code"] for issue in report["issues"]
    }


def test_startup_load_preflight_blocks_stale_staged_topology(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][1]["protection_status"] = "present"
    topology = OutputTopology.from_mapping(raw)

    report = build_startup_load_preflight(
        topology,
        staged_config=staged,
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "path_safety.json",
            staged=staged,
        ),
        validate=_valid_config,
    )
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert report["staged_topology"]["matched"] is False
    assert gates["staged_topology_matches_current"] is False
    assert "staged_targets_mismatch" in {
        issue["code"] for issue in report["issues"]
    }


def test_startup_load_preflight_allows_identity_audition_mode(
    tmp_path: Path,
) -> None:
    topology = _topology(identity_verified=False)
    staged = stage_protected_startup_config(
        topology,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )

    strict = build_startup_load_preflight(
        topology,
        staged_config=staged,
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "strict_path_safety.json",
            topology=topology,
            staged=staged,
        ),
        validate=_valid_config,
    )
    audition = build_startup_load_preflight(
        topology,
        staged_config=staged,
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "audition_path_safety.json",
            topology=topology,
            staged=staged,
            require_physical_identity=False,
        ),
        require_physical_identity=False,
        validate=_valid_config,
    )
    strict_gates = {gate["id"]: gate["passed"] for gate in strict["required_gates"]}
    audition_gates = {
        gate["id"]: gate["passed"] for gate in audition["required_gates"]
    }

    assert strict["status"] == "blocked"
    assert strict_gates["physical_identity_verified"] is False
    assert audition["status"] == "ready"
    assert audition["load_allowed"] is True
    assert audition["identity"]["physical_identity_required"] is False
    assert audition_gates["physical_identity_verified"] is True
    assert audition["path_safety"]["binding"]["checks"][
        "target_assignment_signature"
    ] is True


def test_startup_load_preflight_blocks_stale_path_safety_rollback_binding(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    prior_a = _protected_prior(tmp_path, staged, "prior_a.yml")
    prior_b = _protected_prior(tmp_path, staged, "prior_b.yml")

    report = build_startup_load_preflight(
        _topology(),
        staged_config=staged,
        path_safety_evidence_path=_write_path_safety(
            tmp_path / "path_safety.json",
            staged=staged,
            current_config_path=prior_a,
        ),
        current_config_path=prior_b,
        validate=_valid_config,
    )
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert report["path_safety"]["load_gate"] == "evidence_stale"
    assert gates["path_safety_matches_current_startup_load"] is False
    assert "path_safety_evidence_stale" in {
        issue["code"] for issue in report["issues"]
    }


def test_startup_load_blocks_when_rollback_anchor_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    missing_prior = tmp_path / "missing-prior.yml"
    fake = FakeCamilla(str(missing_prior))
    state_path = tmp_path / "startup_load.json"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))

    result = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=missing_prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    state = load_startup_load_state(state_path=state_path)

    assert result["preflight"]["load_allowed"] is False
    assert result["load"]["status"] == "blocked"
    assert fake.loaded_paths == []
    assert "rollback_target_available_not_verified" in {
        issue["code"] for issue in result["preflight"]["issues"]
    }
    assert state["status"] == "blocked"
    assert state["rollback_available"] is False


def test_startup_load_records_normal_rollback_state(monkeypatch, tmp_path: Path) -> None:
    stage = _staged(tmp_path)
    prior = _normal_prior(tmp_path)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    reconcile_calls = _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))

    result = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=stage,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    state = load_startup_load_state(state_path=state_path)

    assert result["preflight"]["load_allowed"] is True
    assert result["load"]["status"] == "loaded"
    assert fake.loaded_paths == [stage["config"]["path"]]
    assert state["rollback_available"] is True
    assert state["previous_config_path"] == str(prior)
    assert state["candidate_config_path"] == stage["config"]["path"]
    assert reconcile_calls == [{
        "units": (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
        "verb": "start",
        "reason": "active_speaker_startup_load",
        "no_block": False,
        "timeout": 15.0,
    }]


def test_startup_load_rolls_back_to_prior_config(monkeypatch, tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    reconcile_calls = _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))

    load = asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    rollback = asyncio.run(
        rollback_protected_startup_config(
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            state_path=state_path,
            validate=_valid_config,
        )
    )
    state = load_startup_load_state(state_path=state_path)

    assert load["load"]["status"] == "loaded"
    assert rollback["rollback"]["status"] == "rolled_back"
    assert fake.loaded_paths[-1] == str(prior)
    assert state["status"] == "rolled_back"
    assert state["rollback_available"] is False
    assert [
        (call["units"], call["verb"], call["reason"], call["no_block"])
        for call in reconcile_calls
    ] == [
        (
            (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
            "start",
            "active_speaker_startup_load",
            False,
        ),
        (
            (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
            "start",
            "active_speaker_startup_rollback",
            False,
        ),
    ]


def test_startup_load_reconcile_trigger_warns_on_failed_broker_start(
    monkeypatch,
    caplog,
) -> None:
    calls = _record_reconcile_triggers(monkeypatch, ok=False)
    caplog.set_level(logging.INFO, logger=startup_load_mod.logger.name)

    startup_load_mod._trigger_audio_hardware_reconcile(source="unit_test")

    assert calls == [{
        "units": (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
        "verb": "start",
        "reason": "unit_test",
        "no_block": False,
        "timeout": 15.0,
    }]
    assert "event=active_speaker.audio_hardware_reconcile_trigger_failed" in caplog.text
    assert "error=rc=3" in caplog.text
    assert "event=active_speaker.audio_hardware_reconcile_triggered" not in caplog.text


def test_startup_rollback_reports_snapshot_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    prior = _protected_prior(tmp_path, staged)
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
    _record_reconcile_triggers(monkeypatch)
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    asyncio.run(
        load_protected_startup_config(
            _topology(),
            load_config=fake.set_config_file_path,
            get_current_config_path=fake.get_config_file_path,
            path_safety_evidence_path=_write_path_safety(
                tmp_path / "path_safety.json",
                staged=staged,
                current_config_path=prior,
            ),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    failing = SnapshotFailingCamilla(str(prior))

    rollback = asyncio.run(
        rollback_protected_startup_config(
            load_config=failing.set_config_file_path,
            get_current_config_path=failing.get_config_file_path,
            state_path=state_path,
            validate=_valid_config,
        )
    )

    assert rollback["rollback"]["status"] == "rollback_failed"
    assert "startup_rollback_failed" in {
        issue["code"] for issue in rollback["rollback"]["issues"]
    }
