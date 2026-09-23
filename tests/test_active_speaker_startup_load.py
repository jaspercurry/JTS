# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

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
)
from jasper.output_topology import (
    OutputTopology,
)
from tests.active_speaker_fixtures import (
    mono_output_topology,
    valid_camilla_config as _valid_config,
)


def _record_reconcile_triggers(monkeypatch, *, ok: bool = True) -> list[dict]:
    calls: list[dict] = []

    def fake_manage_units(*units: str, **kwargs):
        calls.append({"units": units, **kwargs})
        return {"ok": ok, "rc": 0 if ok else 3}

    monkeypatch.setattr(startup_load_mod, "manage_units", fake_manage_units)
    return calls


def _topology(*, identity_verified: bool = True) -> OutputTopology:
    return mono_output_topology(identity_verified=identity_verified)


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


def _write_path_safety(
    path: Path,
    *,
    topology: OutputTopology | None = None,
    staged: dict,
    current_config_path: str | Path | None = None,
) -> Path:
    evidence = build_startup_load_path_safety_evidence(
        topology or _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=current_config_path or staged["config"]["path"],
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
    assert "stop_control_available" not in {
        gate["id"] for gate in report["required_gates"]
    }


def test_startup_and_commission_load_artifacts_own_independent_schema_versions(
    monkeypatch,
    tmp_path: Path,
):
    assert startup_load_mod.STARTUP_LOAD_SCHEMA_VERSION == 1
    assert startup_load_mod.COMMISSION_LOAD_SCHEMA_VERSION == 1
    assert not hasattr(startup_load_mod, "SCHEMA_VERSION")

    monkeypatch.setattr(startup_load_mod, "STARTUP_LOAD_SCHEMA_VERSION", 2)
    monkeypatch.setattr(startup_load_mod, "COMMISSION_LOAD_SCHEMA_VERSION", 3)
    assert startup_load_mod._base_state(tmp_path / "startup.json")[
        "artifact_schema_version"
    ] == 2
    assert startup_load_mod._commission_base_state(tmp_path / "commission.json")[
        "artifact_schema_version"
    ] == 3


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
    raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = 3
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
