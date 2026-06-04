from __future__ import annotations

import asyncio
import json
from pathlib import Path

from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.path_safety import (
    HARDWARE_PROBE_EVIDENCE_SOURCE,
    PATH_SAFETY_EVIDENCE_KIND,
    REQUIRED_PATHS,
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
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


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


def _topology() -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono cabinet",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })


def _valid_config(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(
        status=ValidationStatus.VALID,
        path=str(path),
    )


def _write_path_safety(path: Path) -> Path:
    path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": PATH_SAFETY_EVIDENCE_KIND,
            "evidence_source": HARDWARE_PROBE_EVIDENCE_SOURCE,
            "paths": {
                requirement.id: {check: True for check in requirement.checks}
                for requirement in REQUIRED_PATHS
            },
        }),
        encoding="utf-8",
    )
    return path


def _staged(tmp_path: Path) -> dict:
    return stage_protected_startup_config(
        _topology(),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )


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
    report = build_startup_load_preflight(
        _topology(),
        staged_config=_staged(tmp_path),
        calibration_level=calibration_level_payload(requested_level_dbfs=-70),
        path_safety_evidence_path=_write_path_safety(tmp_path / "path_safety.json"),
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
        path_safety_evidence_path=_write_path_safety(tmp_path / "path_safety.json"),
        validate=_valid_config,
    )
    gates = {gate["id"]: gate["passed"] for gate in report["required_gates"]}

    assert report["status"] == "blocked"
    assert report["staged_topology"]["matched"] is False
    assert gates["staged_topology_matches_current"] is False
    assert "staged_targets_mismatch" in {
        issue["code"] for issue in report["issues"]
    }


def test_startup_load_blocks_when_rollback_anchor_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _staged(tmp_path)
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
            path_safety_evidence_path=_write_path_safety(tmp_path / "path_safety.json"),
            state_path=state_path,
            validate=_valid_config,
        )
    )
    state = load_startup_load_state(state_path=state_path)

    assert result["preflight"]["load_allowed"] is True
    assert result["load"]["status"] == "blocked"
    assert fake.loaded_paths == []
    assert "rollback_anchor_missing" in {
        issue["code"] for issue in result["load"]["issues"]
    }
    assert state["status"] == "blocked"
    assert state["rollback_available"] is False


def test_startup_load_records_rollback_state(monkeypatch, tmp_path: Path) -> None:
    stage = _staged(tmp_path)
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    fake = FakeCamilla(str(prior))
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
            path_safety_evidence_path=_write_path_safety(tmp_path / "path_safety.json"),
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


def test_startup_load_rolls_back_to_prior_config(monkeypatch, tmp_path: Path) -> None:
    _staged(tmp_path)
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
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
            path_safety_evidence_path=_write_path_safety(tmp_path / "path_safety.json"),
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


def test_startup_rollback_reports_snapshot_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _staged(tmp_path)
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    fake = FakeCamilla(str(prior))
    state_path = tmp_path / "startup_load.json"
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
            path_safety_evidence_path=_write_path_safety(tmp_path / "path_safety.json"),
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
