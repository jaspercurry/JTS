# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import jasper.active_speaker.setup_status as setup_mod
from jasper.active_speaker.baseline_profile import (
    baseline_candidate_fingerprint,
    build_baseline_profile_candidate,
    recompose_applied_baseline_yaml,
    topology_config_fingerprint,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.measurement import (
    active_driver_targets,
    load_measurement_state,
    record_driver_measurement,
    record_summed_test_artifact,
    record_summed_validation,
    start_active_comparison_set,
)
from jasper.output_topology import (
    OutputTopology,
    OutputTopologyError,
    save_output_topology,
)
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_baseline_profile import (
    _draft,
    _dual_apple_topology,
    _measurements,
    _safe_session,
    _valid_config,
)


def _active_topology() -> OutputTopology:
    return mono_output_topology(topology_name="Bench mono")


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


def _candidate(
    *,
    status: str,
    config_path: Path,
    issues: list[dict] | None = None,
    measured: bool = False,
    incomparable: bool = False,
):
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
        "level_match": {
            "groups_measured": 1 if measured else 0,
            "incomparable_groups": (
                [{"speaker_group_id": "mono", "reason": "effective_excitation_mismatch"}]
                if incomparable
                else []
            ),
            "applied": measured,
        },
        "issues": list(issues or []),
    }


def _applied_acoustic_profile(
    *,
    measured: bool = True,
    config_path: Path | None = None,
    with_snapshot: bool = True,
    tuning_owner: str = "manual",
) -> dict:
    profile = {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "status": "applied",
        "baseline_id": "baseline-bench_mono",
        "source": {
            "fingerprint": "source-fp",
        },
        "config": {
            "path": str(config_path) if config_path is not None else "",
        },
        "provisional": not measured,
        "tuning_owner": tuning_owner,
    }
    if with_snapshot:
        profile["candidate_fingerprint"] = "candidate-fp"
        preset = json.loads(
            Path(
                "jasper/active_speaker/presets/"
                "bc_de250_dayton_e150he44_v1.json"
            ).read_text(encoding="utf-8")
        )
        profile["recomposition_snapshot"] = {
            "schema_version": 1,
            "topology_id": "bench_mono",
            "topology_fingerprint": topology_config_fingerprint(_active_topology()),
            "domain": "full",
            "preset": preset,
            "playback_device": "hw:Loopback,0",
            "corrections": {
                "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
                "tweeter": {"gain_db": -10.0, "delay_ms": 0.0, "inverted": False},
            },
            "level_match": {
                "applied": measured,
                "groups_measured": 1 if measured else 0,
            },
            "corrections_source": {
                "woofer": "measured" if measured else "none",
                "tweeter": "measured" if measured else "sensitivity",
            },
            "tuning_owner": tuning_owner,
        }
    return profile


def _write_applied_graph(
    topology: OutputTopology,
    profile: dict,
    path: Path,
) -> None:
    text, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=profile,
    )
    assert issues == []
    assert text is not None
    path.write_text(text, encoding="utf-8")


def _acoustic_measurement_state(*, summed: bool = True) -> dict:
    drivers = {}
    for role in ("woofer", "tweeter"):
        drivers[f"mono:{role}"] = {
            "speaker_group_id": "mono",
            "role": role,
            "captured": True,
            "mic_clipping": False,
            "excitation": {
                "schema_version": 1,
                "scope": "sweep_plus_role_varying_commission_gain",
                "sweep_peak_dbfs": -12.0,
                "commissioning_gain_db": -40.0,
                "effective_peak_dbfs": -52.0,
            },
            "acoustic": {
                "verdict": "present",
                "mic_clipping": False,
                "overlap_levels": [{"fc_hz": 2000.0, "usable": True}],
            },
        }
    summed_records = {
        "mono": {
            "speaker_group_id": "mono",
            "validated": True,
            "mic_clipping": False,
            "acoustic": {
                "verdict": "blend_ok",
                "mic_clipping": False,
            },
        }
    } if summed else {}
    return {
        "summary": {
            "required_driver_count": 2,
            "required_summed_group_count": 1,
            "summed_validation_complete": summed,
            "latest_driver_measurements": drivers,
            "latest_summed_validations": summed_records,
        }
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
    assert status["room_correction_allowed"] is True
    assert status["acoustic_commissioning"]["status"] == "not_required"
    assert status["acoustic_commissioning"]["decision_schema_version"] == 1
    assert status["acoustic_commissioning"]["authority"] == (
        "passive_not_required"
    )
    # A passive speaker has no commissioning session, but the "commissioning"
    # block is still present with a well-defined idle shape, and its
    # room_correction_allowed mirrors the top-level value exactly (design doc
    # "Runtime surface").
    assert status["commissioning"]["phase"] == "idle"
    assert status["commissioning"]["room_correction_allowed"] is True


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
    assert status["room_correction_allowed"] is False
    # Phase-derivation table (design doc "Structured events"): a profile whose
    # status is "applied" (not apply_failed, may_apply already false) with no
    # open comparison set falls through every specific branch to idle.
    assert status["commissioning"]["phase"] == "idle"
    assert status["commissioning"]["room_correction_allowed"] is False
    # No applied_profile was resolvable in this fixture (no state on disk),
    # so there is no fingerprint to surface.
    assert status["commissioning"]["applied_profile_fingerprint"] is None


def test_active_speaker_allows_room_correction_only_after_acoustic_commissioning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    applied = _applied_acoustic_profile(config_path=config_path)
    _write_applied_graph(topology, applied, config_path)
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(
            status="applied", config_path=config_path, measured=True
        ),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: _acoustic_measurement_state(),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: applied,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["configured"] is True
    assert status["room_correction_allowed"] is True
    assert status["acoustic_commissioning"]["status"] == "ready"
    assert status["acoustic_commissioning"]["authority"] == (
        "manual_applied_profile"
    )
    assert status["acoustic_commissioning"]["layer_a_identity"] == (
        status["protected_profile"]["layer_a_binding"]["loaded_fingerprint"]
    )
    assert status["acoustic_commissioning"]["drivers"] == {
        "required_groups": 1,
        "usable_groups": 1,
        "excitation_comparable": True,
    }
    assert status["acoustic_commissioning"]["summed"] == {
        "required": 1,
        "usable": 1,
    }
    # room_correction_allowed mirrors acoustic_commissioning.allowed exactly
    # in the wired /state payload (design doc "Runtime surface"), and the
    # applied candidate identity is surfaced for graph-context correlation.
    assert status["commissioning"]["room_correction_allowed"] is True
    assert status["commissioning"]["room_correction_allowed"] == (
        status["acoustic_commissioning"]["allowed"]
    )
    assert status["commissioning"]["applied_profile_fingerprint"] == "candidate-fp"
    # status="applied" with may_apply already false and no open comparison
    # set falls through every specific phase branch to idle.
    assert status["commissioning"]["phase"] == "idle"


def test_applied_manual_snapshot_allows_room_without_phone_measurements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    manual = _applied_acoustic_profile(
        measured=False,
        config_path=config_path,
    )
    manual["tuning_owner"] = "manual"
    manual["recomposition_snapshot"]["tuning_owner"] = "manual"
    _write_applied_graph(topology, manual, config_path)
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=config_path),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: {"summary": {}},
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: manual,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["room_correction_allowed"] is True
    assert status["acoustic_commissioning"]["applied_profile"] == {
        "available": True,
        "measured_level_match_applied": False,
        "tuning_owner": "manual",
        "snapshot_valid": True,
        "graph_matches_loaded": True,
    }


def test_manual_room_authority_allows_program_filters_on_exact_layer_a(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    protected_path = tmp_path / "active_speaker_baseline.yml"
    current_path = tmp_path / "sound_current.yml"
    manual = _applied_acoustic_profile(
        measured=False,
        config_path=protected_path,
    )
    _write_applied_graph(topology, manual, protected_path)
    current = yaml.safe_load(protected_path.read_text(encoding="utf-8"))
    current["filters"]["room_peq_smoke"] = {
        "type": "Biquad",
        "parameters": {"type": "Peaking", "freq": 80.0, "q": 4.0, "gain": -3.0},
    }
    current["pipeline"].insert(0, {
        "type": "Filter",
        "channels": [0, 1],
        "names": ["room_peq_smoke"],
    })
    current_text = yaml.safe_dump(current, sort_keys=False)
    current_path.write_text(current_text, encoding="utf-8")
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=protected_path),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: {"summary": {}},
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: manual,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(current_path),
        active_config_text=current_text,
    )

    assert status["room_correction_allowed"] is True
    binding = status["protected_profile"]["layer_a_binding"]
    assert binding["status"] == "current"
    assert binding["matches"] is True
    assert binding["loaded_fingerprint"] == binding["expected_fingerprint"]


def test_manual_room_authority_explicitly_scopes_out_distributed_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bonded leader needs a future Active-owned two-daemon identity."""
    from tests.test_active_speaker_runtime_contract import _program_bake_yaml

    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    protected_path = tmp_path / "active_speaker_baseline.yml"
    current_path = tmp_path / "sound_current.yml"
    manual = _applied_acoustic_profile(
        measured=False,
        config_path=protected_path,
    )
    _write_applied_graph(topology, manual, protected_path)
    running_text = _program_bake_yaml()
    current_path.write_text(running_text, encoding="utf-8")
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=protected_path),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: {"summary": {}},
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: manual,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(current_path),
        active_config_text=running_text,
    )

    assert status["configured"] is True
    assert status["volume_allowed"] is True
    assert status["grouping_allowed"] is True
    assert status["room_correction_allowed"] is False
    acoustic = status["acoustic_commissioning"]
    assert acoustic["authority"] is None
    assert acoustic["layer_a_identity"] is None
    assert acoustic["status"] == "incomplete"
    assert acoustic["allowed"] is False
    assert acoustic["reason"] == "active_grouped_room_correction_not_supported"
    assert acoustic["setup_href"] == "/rooms/"
    assert "Turn grouping off" in acoustic["detail"]
    assert status["protected_profile"]["layer_a_binding"] == {
        "status": "distributed_active_unsupported",
        "matches": False,
        "expected_fingerprint": None,
        "loaded_fingerprint": None,
    }


def test_manual_room_authority_blocks_loaded_layer_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    protected_path = tmp_path / "active_speaker_baseline.yml"
    current_path = tmp_path / "sound_current.yml"
    manual = _applied_acoustic_profile(
        measured=False,
        config_path=protected_path,
    )
    _write_applied_graph(topology, manual, protected_path)
    current_path.write_text(
        protected_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    current = yaml.safe_load(protected_path.read_text(encoding="utf-8"))
    current["filters"]["as_tweeter_baseline_gain"]["parameters"]["gain"] = -9.0
    running_text = yaml.safe_dump(current, sort_keys=False)
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=protected_path),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: {"summary": {}},
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: manual,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(current_path),
        active_config_text=running_text,
    )

    assert status["configured"] is True
    assert status["volume_allowed"] is True
    assert status["room_correction_allowed"] is False
    assert status["acoustic_commissioning"]["authority"] is None
    assert status["acoustic_commissioning"]["reason"] == (
        "active_applied_profile_graph_mismatch"
    )
    binding = status["protected_profile"]["layer_a_binding"]
    assert binding["status"] == "mismatch"
    assert binding["matches"] is False
    assert binding["loaded_fingerprint"] != binding["expected_fingerprint"]


def test_manual_room_authority_blocks_unverifiable_loaded_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    protected_path = tmp_path / "active_speaker_baseline.yml"
    current_path = tmp_path / "sound_current.yml"
    manual = _applied_acoustic_profile(
        measured=False,
        config_path=protected_path,
    )
    _write_applied_graph(topology, manual, protected_path)
    current_path.write_text(
        protected_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=protected_path),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: {"summary": {}},
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: manual,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(current_path),
        active_config_text="pipeline: [\n",
    )

    assert status["configured"] is True
    assert status["volume_allowed"] is True
    assert status["room_correction_allowed"] is False
    assert status["acoustic_commissioning"]["reason"] == (
        "active_applied_profile_graph_unverifiable"
    )
    assert status["protected_profile"]["layer_a_binding"] == {
        "status": "unverifiable",
        "matches": False,
        "expected_fingerprint": None,
        "loaded_fingerprint": None,
    }


def test_applied_automatic_snapshot_requires_receipt_after_measurement_store_clears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    automatic = _applied_acoustic_profile(config_path=config_path)
    automatic["tuning_owner"] = "automatic"
    automatic["recomposition_snapshot"]["tuning_owner"] = "automatic"
    _write_applied_graph(topology, automatic, config_path)
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=config_path),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: {"summary": {}},
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: automatic,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["room_correction_allowed"] is False
    assert status["acoustic_commissioning"]["authority"] is None
    assert status["acoustic_commissioning"]["reason"] == (
        "active_automatic_commissioning_receipt_missing"
    )
    assert status["applied_crossover"]["valid"] is True
    assert status["applied_crossover"]["owner"] == "automatic"
    assert status["automatic_candidate"]["ready"] is False


def test_legacy_applied_profile_is_safe_but_requires_snapshot_reapply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    applied = _applied_acoustic_profile(config_path=config_path)
    _write_applied_graph(topology, applied, config_path)
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(
            status="applied", config_path=config_path, measured=True
        ),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: _acoustic_measurement_state(),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: _applied_acoustic_profile(
            config_path=config_path,
            with_snapshot=False,
        ),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["configured"] is True
    assert status["volume_allowed"] is True
    assert status["protected_profile"] == {
        "available": True,
        "status": "ready",
            "config_path": str(config_path),
            "source_fingerprint": "source-fp",
            "candidate_fingerprint": None,
            "topology_current": True,
        "provisional": False,
        "recomposition_snapshot_available": False,
        "layer_a_binding": {
            "status": "unverifiable",
            "matches": False,
            "expected_fingerprint": None,
            "loaded_fingerprint": None,
        },
    }
    assert status["room_correction_allowed"] is False
    assert status["acoustic_commissioning"]["reason"] == (
        "active_applied_profile_snapshot_missing"
    )


def test_manual_applied_snapshot_allows_room_without_summed_acoustic_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    applied = _applied_acoustic_profile(config_path=config_path)
    _write_applied_graph(topology, applied, config_path)
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(
            status="applied",
            config_path=config_path,
            measured=True,
        ),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: _acoustic_measurement_state(summed=False),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: applied,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["volume_allowed"] is True
    assert status["room_correction_allowed"] is True
    assert status["acoustic_commissioning"]["reason"] is None
    assert status["acoustic_commissioning"]["summed"]["usable"] == 0
    assert status["acoustic_commissioning"]["setup_href"] == (
        "/correction/crossover/"
    )


def test_applied_snapshot_remains_room_ready_when_mutable_driver_evidence_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    applied = _applied_acoustic_profile(config_path=config_path)
    _write_applied_graph(topology, applied, config_path)
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(
            status="applied",
            config_path=config_path,
            measured=True,
            incomparable=True,
        ),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_measurement_state",
        lambda _topology: _acoustic_measurement_state(),
    )
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: applied,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["room_correction_allowed"] is True
    assert status["acoustic_commissioning"]["reason"] is None
    assert status["acoustic_commissioning"]["drivers"][
        "excitation_comparable"
    ] is False


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
    saved["candidate_fingerprint"] = "declared-wrong"
    expected_applied_fingerprint = baseline_candidate_fingerprint(saved)
    baseline_state_path.write_text(json.dumps(saved), encoding="utf-8")

    ready = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(baseline_config_path),
        baseline_state_path=baseline_state_path,
    )
    assert ready["configured"] is True
    assert ready["volume_allowed"] is True
    assert ready["grouping_allowed"] is True
    assert (
        ready["protected_profile"]["candidate_fingerprint"]
        == expected_applied_fingerprint
    )
    assert (
        ready["commissioning"]["applied_profile_fingerprint"]
        == expected_applied_fingerprint
    )
    assert ready["baseline_profile"]["candidate_fingerprint"]
    assert ready["automatic_candidate"]["candidate_fingerprint"]
    assert (
        ready["baseline_profile"]["candidate_fingerprint"]
        != ready["automatic_candidate"]["candidate_fingerprint"]
    )

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(tmp_path / "missing_measurements.json"),
    )

    stale = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(baseline_config_path),
        baseline_state_path=baseline_state_path,
    )

    # The missing/current measurement set is a mutable candidate. It can require
    # revalidation without invalidating the immutable profile that still owns
    # ordinary playback and Room's Layer-A prerequisite.
    assert stale["configured"] is True
    assert stale["volume_allowed"] is True
    assert stale["grouping_allowed"] is True
    assert stale["reason"] is None
    assert stale["protected_profile"]["status"] == "ready"
    assert stale["room_correction_allowed"] is True
    assert stale["acoustic_commissioning"]["reason"] is None
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
    # No topology was ever readable, so commissioning degrades to its fail-soft
    # idle default; room_correction_allowed still mirrors the top-level value.
    assert status["commissioning"]["phase"] == "idle"
    assert status["commissioning"]["room_correction_allowed"] is False


def test_unreadable_baseline_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _save_topology(monkeypatch, tmp_path, _active_topology())
    config_path = tmp_path / "active_speaker_baseline.yml"
    config_path.write_text("pipeline: []\n", encoding="utf-8")

    def _raise(*_args, **_kwargs):
        raise ValueError("baseline candidate could not be derived")

    monkeypatch.setattr(setup_mod, "build_baseline_profile_candidate", _raise)
    # Deterministic measurement state so the commissioning-phase assertion
    # below isn't at the mercy of whatever (if anything) is on disk at the
    # real default measurements path.
    monkeypatch.setattr(
        setup_mod, "load_measurement_state", lambda _topology: {"summary": {}},
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["volume_allowed"] is False
    assert status["grouping_allowed"] is False
    assert status["safety_muted"] is True
    assert "active_baseline_profile_unreadable" in {
        issue["code"] for issue in status["issues"]
    }
    # profile is None after the caught exception (never apply_failed, never
    # may_apply); with no active comparison set either, phase falls to idle.
    assert status["commissioning"]["phase"] == "idle"
    assert status["commissioning"]["applied_profile_fingerprint"] is None


def test_commissioning_failed_phase_wired_through_full_status_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The "failed" phase is reachable through the real read path.

    The standalone table below (test_commissioning_summary_failed_surfaces_
    first_blocker_code) pins commissioning_summary's own phase-derivation
    priority order in isolation. This test pins that
    read_active_speaker_setup_status actually wires an apply_failed candidate
    through to that same result, not only a hand-built input.
    """
    _save_topology(monkeypatch, tmp_path, _active_topology())
    config_path = tmp_path / "active_speaker_baseline.yml"
    config_path.write_text("pipeline: []\n", encoding="utf-8")

    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *_a, **_k: {
            "status": "apply_failed",
            "source": {"fingerprint": "source-fp"},
            "permissions": {"may_apply": False},
            "issues": [
                {
                    "severity": "warning",
                    "code": "some_warning",
                    "message": "not the one",
                },
                {
                    "severity": "blocker",
                    "code": "baseline_profile_apply_failed",
                    "message": "camilladsp rejected the candidate",
                },
            ],
        },
    )
    # Deterministic measurement state so the commissioning-phase assertion
    # below isn't at the mercy of whatever (if anything) is on disk at the
    # real default measurements path.
    monkeypatch.setattr(
        setup_mod, "load_measurement_state", lambda _topology: {"summary": {}},
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["commissioning"]["phase"] == "failed"
    assert (
        status["commissioning"]["last_failure_code"]
        == "baseline_profile_apply_failed"
    )


# --- commissioning_summary (lane E, docs/active-crossover-information-design.md
# "Runtime surface") — standalone phase-derivation table ---------------------
#
# These call commissioning_summary directly (not through
# read_active_speaker_setup_status) with hand-built inputs, per state fixture,
# to pin the phase-derivation priority order independent of whether today's
# candidate-building code path can organically produce every input shape.


def test_commissioning_summary_idle_with_no_evidence() -> None:
    result = setup_mod.commissioning_summary(
        SimpleNamespace(topology_id="bench_mono"),
        profile=None,
        applied_profile=None,
        measurements=None,
    )
    assert result == {
        "phase": "idle",
        "session_id": None,
        "session_fingerprint": None,
        "applied_profile_fingerprint": None,
        "last_capture": None,
        "last_failure_code": None,
        "room_correction_allowed": False,
    }


def test_commissioning_summary_measuring_with_open_comparison_set(
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    driver_level_locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        }
        for target in active_driver_targets(topology)
    }
    comparison_set = start_active_comparison_set(
        topology,
        profile_context_id="ctx",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks=driver_level_locks,
        bundle_session_id="abc123def456",
        state_path=tmp_path / "measurements.json",
        now="2026-07-11T12:00:00Z",
    )

    result = setup_mod.commissioning_summary(
        topology,
        profile=None,
        applied_profile=None,
        measurements={"active_comparison_set": comparison_set},
    )

    assert result["phase"] == "measuring"
    assert result["session_id"] == "abc123def456"
    assert result["session_fingerprint"] == comparison_set["fingerprint"]


def test_commissioning_summary_proposal_ready_when_may_apply() -> None:
    result = setup_mod.commissioning_summary(
        SimpleNamespace(topology_id="bench_mono"),
        profile={"status": "ready_to_apply", "permissions": {"may_apply": True}},
        applied_profile=None,
        measurements=None,
    )
    assert result["phase"] == "proposal_ready"


def test_commissioning_summary_failed_surfaces_first_blocker_code() -> None:
    result = setup_mod.commissioning_summary(
        SimpleNamespace(topology_id="bench_mono"),
        profile={
            "status": "apply_failed",
            "issues": [
                {
                    "severity": "warning",
                    "code": "some_warning",
                    "message": "not the one",
                },
                {
                    "severity": "blocker",
                    "code": "baseline_profile_apply_failed",
                    "message": "camilladsp rejected the candidate",
                },
            ],
        },
        applied_profile=None,
        measurements=None,
    )
    assert result["phase"] == "failed"
    assert result["last_failure_code"] == "baseline_profile_apply_failed"


def test_commissioning_summary_last_capture_surfaces_worst_relevant_band() -> None:
    # The newest record (by created_at) across BOTH maps wins, regardless of
    # which map it came from.
    measurements = {
        "latest_by_target": {
            "mono:woofer": {
                "created_at": "2026-07-11T10:00:00Z",
                "mic_clipping": False,
                "acoustic": {
                    "verdict": "present",
                    "snr": {"worst_relevant": {"estimated_snr_db": 22.5}},
                },
            },
        },
        "latest_summed_by_group": {
            "mono": {
                "created_at": "2026-07-11T11:00:00Z",
                "mic_clipping": True,
                "acoustic": {
                    "verdict": "blend_ok",
                    "snr": {"worst_relevant": {"estimated_snr_db": 18.0}},
                },
            },
        },
    }

    result = setup_mod.commissioning_summary(
        SimpleNamespace(topology_id="bench_mono"),
        profile=None,
        applied_profile=None,
        measurements=measurements,
    )

    assert result["last_capture"] == {
        "snr_db": 18.0,
        "verdict": "blend_ok",
        "clipping": True,
        "at": "2026-07-11T11:00:00Z",
    }


def test_commissioning_summary_last_capture_none_without_any_record() -> None:
    result = setup_mod.commissioning_summary(
        SimpleNamespace(topology_id="bench_mono"),
        profile=None,
        applied_profile=None,
        measurements={"latest_by_target": {}, "latest_summed_by_group": {}},
    )
    assert result["last_capture"] is None


def test_commissioning_summary_is_fail_soft_never_raises() -> None:
    class _ExplodesOnGet(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("boom: unreadable measurement state")

    result = setup_mod.commissioning_summary(
        SimpleNamespace(topology_id="bench_mono"),
        profile=_ExplodesOnGet(),
        applied_profile=None,
        measurements=None,
    )

    # Degrades to the safest phase rather than propagating the exception.
    assert result["phase"] == "idle"
    assert result["room_correction_allowed"] is False


# --- Overwrite-bug regression (lane E, Slice 2 paired summed evidence) ------


def test_usable_summed_acoustic_gate_unaffected_by_later_reverse_capture(
    tmp_path: Path,
) -> None:
    """setup_status._usable_summed_acoustic is the room-correction blend gate
    -- it reads summary.latest_summed_validations, which measurement.py now
    defines as the latest IN-PHASE record per group specifically. Before
    that fix, latest_summed_validations kept whichever summed record was
    captured most recently regardless of polarity, so a reverse-polarity
    capture recorded AFTER a validated in-phase blend check -- which can
    ALSO read validated=True/verdict='blend_ok' (a formed reverse null IS
    the pass for a reverse capture) -- silently shadowed the in-phase
    evidence this gate needs. This pins the fix at the real consumer, through
    real persistence (not a hand-built measurements dict)."""
    topology = _active_topology()
    state_path = tmp_path / "measurements.json"
    for role in ("woofer", "tweeter"):
        output_index = 0 if role == "woofer" else 1
        playback_id = f"playback-{role}"
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42.0,
                "playback_id": playback_id,
            },
            safe_session=_safe_session(
                role=role, output_index=output_index, playback_id=playback_id,
            ),
            state_path=state_path,
            now=f"2026-06-14T12:0{1 if role == 'woofer' else 2}:00Z",
        )
    record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": "summed-playback-audible",
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "tone.wav",
                    "metadata_basename": "tone.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2500, "level_dbfs": -72},
            },
        },
        state_path=state_path,
        now="2026-06-14T12:02:30Z",
    )
    record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40.0,
            "summed_test_id": "summed-playback-audible",
            "acoustic": {
                "verdict": "blend_ok",
                "null_depth_db": 2.0,
                "expect_null": False,
                "calibrated": True,
            },
        },
        state_path=state_path,
        now="2026-06-14T12:03:00Z",
    )

    before = load_measurement_state(topology, state_path=state_path)
    before_record = before["summary"]["latest_summed_validations"]["mono"]
    assert setup_mod._usable_summed_acoustic(before_record) is True

    # A reverse-polarity capture, taken afterward, forms the expected null
    # (verdict=blend_ok, validated=True -- the pass case for a reverse
    # capture, indistinguishable from an in-phase pass by outcome alone).
    record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -55.0,
            "summed_test_id": "summed-playback-audible",
            "acoustic": {
                "verdict": "blend_ok",
                "null_depth_db": 22.0,
                "expect_null": True,
                "calibrated": True,
            },
        },
        state_path=state_path,
        now="2026-06-14T12:04:00Z",
    )

    after = load_measurement_state(topology, state_path=state_path)
    after_record = after["summary"]["latest_summed_validations"]["mono"]
    # Still the in-phase record -- the gate is unaffected by the reverse
    # capture.
    assert after_record["acoustic"]["expect_null"] is False
    assert after_record["acoustic"]["null_depth_db"] == 2.0
    assert setup_mod._usable_summed_acoustic(after_record) is True
