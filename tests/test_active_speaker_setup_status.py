# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

import jasper.active_speaker.setup_status as setup_mod
from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    OutputTopologyError,
    save_output_topology,
)
from tests.test_active_speaker_baseline_profile import (
    _draft,
    _dual_apple_topology,
    _measurements,
    _valid_config,
)


def _active_topology() -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
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


def _passive_topology() -> OutputTopology:
    raw = _active_topology().to_dict()
    raw["topology_id"] = "passive_stereo"
    raw["speaker_groups"] = [
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "full_range_passive",
            "channels": [
                {
                    "role": "full_range",
                    "physical_output_index": 0,
                    "identity_verified": True,
                }
            ],
        },
        {
            "id": "right",
            "label": "Right speaker",
            "kind": "right",
            "mode": "full_range_passive",
            "channels": [
                {
                    "role": "full_range",
                    "physical_output_index": 1,
                    "identity_verified": True,
                }
            ],
        },
    ]
    raw["routing"] = {
        "main_left_group_id": "left",
        "main_right_group_id": "right",
        "mono_group_id": None,
        "subwoofer_group_ids": [],
    }
    return OutputTopology.from_mapping(raw)


def _save_topology(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, topology) -> Path:
    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    save_output_topology(topology, path)
    return path


def _candidate(*, status: str, config_path: Path, issues: list[dict] | None = None):
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "status": status,
        "source": {"fingerprint": "source-fp"},
        "config": {
            "path": str(config_path),
            "basename": config_path.name,
            "exists": config_path.exists(),
        },
        "provisional": False,
        "issues": list(issues or []),
    }


def test_passive_speaker_is_ready_without_active_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _save_topology(monkeypatch, tmp_path, _passive_topology())
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: pytest.fail("passive topology must not need baseline"),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path="/var/lib/camilladsp/configs/sound_current.yml",
    )

    assert status["active"] is False
    assert status["configured"] is True
    assert status["volume_allowed"] is True
    assert status["grouping_allowed"] is True


def test_active_speaker_blocks_volume_and_grouping_until_baseline_is_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _save_topology(monkeypatch, tmp_path, _active_topology())
    config_path = tmp_path / "active_speaker_baseline.yml"
    config_path.write_text("pipeline: []\n", encoding="utf-8")
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(
            status="blocked",
            config_path=config_path,
            issues=[
                {
                    "severity": "blocker",
                    "code": "baseline_summed_validation_missing",
                    "message": (
                        "validate the combined crossover before saving the active "
                        "profile"
                    ),
                }
            ],
        ),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["active"] is True
    assert status["configured"] is False
    assert status["volume_allowed"] is False
    assert status["grouping_allowed"] is False
    assert status["safety_muted"] is True
    assert status["reason"] == "baseline_summed_validation_missing"
    assert "validate the combined crossover" in status["detail"]


def test_active_speaker_allows_volume_and_grouping_after_applied_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _save_topology(monkeypatch, tmp_path, _active_topology())
    config_path = tmp_path / "active_speaker_baseline.yml"
    config_path.write_text("pipeline: []\n", encoding="utf-8")
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=config_path),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["active"] is True
    assert status["configured"] is True
    assert status["volume_allowed"] is True
    assert status["grouping_allowed"] is True
    assert status["safety_muted"] is False
    assert status["reason"] is None


def test_active_speaker_loaded_commissioning_graph_still_blocks_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _save_topology(monkeypatch, tmp_path, _active_topology())
    config_path = tmp_path / "active_speaker_baseline.yml"
    config_path.write_text("pipeline: []\n", encoding="utf-8")
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=config_path),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path="/var/lib/camilladsp/configs/active_speaker_staged_startup.yml",
    )

    assert status["configured"] is False
    assert status["volume_allowed"] is False
    assert status["grouping_allowed"] is False
    assert status["reason"] == "active_speaker_commissioning_config_loaded"


def test_active_speaker_ready_to_apply_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _save_topology(monkeypatch, tmp_path, _active_topology())
    config_path = tmp_path / "active_speaker_baseline.yml"
    config_path.write_text("pipeline: []\n", encoding="utf-8")
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="ready_to_apply", config_path=config_path),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["configured"] is False
    assert status["volume_allowed"] is False
    assert status["grouping_allowed"] is False
    assert status["reason"] == "active_baseline_profile_not_applied"


def test_active_speaker_setup_rederives_baseline_freshness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    _save_topology(monkeypatch, tmp_path, topology)

    draft = _draft(topology)
    draft_path = tmp_path / "design_draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE", str(draft_path))

    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    preview_path = tmp_path / "crossover_preview.json"
    preview_path.write_text(json.dumps(preview), encoding="utf-8")
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(preview_path),
    )

    _measurements(topology, tmp_path)
    measurements_path = tmp_path / "measurements.json"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(measurements_path),
    )

    baseline_state_path = tmp_path / "baseline_profile.json"
    baseline_config_path = tmp_path / "active_speaker_baseline.yml"
    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=setup_mod.load_measurement_state(topology),
        write=True,
        state_path=baseline_state_path,
        config_path=baseline_config_path,
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )
    assert payload["status"] == "ready_to_apply"

    saved = json.loads(baseline_state_path.read_text(encoding="utf-8"))
    saved["status"] = "applied"
    baseline_state_path.write_text(json.dumps(saved), encoding="utf-8")

    ready = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(baseline_config_path),
        baseline_state_path=baseline_state_path,
    )
    assert ready["configured"] is True
    assert ready["volume_allowed"] is True
    assert ready["grouping_allowed"] is True

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(tmp_path / "missing_measurements.json"),
    )

    stale = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(baseline_config_path),
        baseline_state_path=baseline_state_path,
    )

    assert stale["configured"] is False
    assert stale["volume_allowed"] is False
    assert stale["grouping_allowed"] is False
    assert stale["reason"] in {
        "baseline_driver_measurements_missing",
        "baseline_summed_validation_missing",
    }
    assert stale["baseline_profile"]["revalidation"]["required"] is True


# --- C3b-2: the two documented fail-closed branches ---
#
# The module docstring promises "an unreadable topology OR unreadable baseline
# profile returns a blocked snapshot instead of silently treating the speaker as
# ready." Both branches were asserted by no test, so a refactor that turned
# either catch fail-OPEN (e.g. returning volume_allowed=True) would silently
# unblock volume/mute/grouping on a misconfigured active speaker. These pin them.


def test_unreadable_topology_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> OutputTopology:
        raise OutputTopologyError("topology JSON is corrupt")

    monkeypatch.setattr(setup_mod, "load_output_topology_strict", _raise)

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path="/var/lib/camilladsp/configs/sound_current.yml",
    )

    assert status["volume_allowed"] is False
    assert status["grouping_allowed"] is False
    assert status["safety_muted"] is True
    assert status["reason"] == "output_topology_unreadable"
    assert "output_topology_unreadable" in {
        issue["code"] for issue in status["issues"]
    }


def test_unreadable_baseline_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _save_topology(monkeypatch, tmp_path, _active_topology())
    config_path = tmp_path / "active_speaker_baseline.yml"
    config_path.write_text("pipeline: []\n", encoding="utf-8")

    def _raise(*_args, **_kwargs):
        raise ValueError("baseline candidate could not be derived")

    monkeypatch.setattr(setup_mod, "build_baseline_profile_candidate", _raise)

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["volume_allowed"] is False
    assert status["grouping_allowed"] is False
    assert status["safety_muted"] is True
    assert "active_baseline_profile_unreadable" in {
        issue["code"] for issue in status["issues"]
    }
