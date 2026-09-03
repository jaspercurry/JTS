# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from importlib.resources import files
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import jasper.active_speaker._common as _common
import jasper.active_speaker.setup_status as setup_mod
from jasper.active_speaker import commissioning_verification
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
    new_topology_draft,
    save_output_topology,
)
from tests.active_speaker_fixtures import (
    dual_apple_output_topology as _dual_apple_topology,
    mono_output_topology,
    safe_measurement_session as _safe_session,
    standard_design_draft as _draft,
    standard_measurements as _measurements,
    valid_camilla_config as _valid_config,
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


def _subwoofer_topology() -> OutputTopology:
    raw = _active_topology().to_dict()
    raw["topology_id"] = "subwoofer_only"
    raw["speaker_groups"] = [
        {
            "id": "sub",
            "label": "Subwoofer",
            "kind": "subwoofer",
            "mode": "subwoofer",
            "channels": [
                {
                    "role": "subwoofer",
                    "physical_output_index": 0,
                    "identity_verified": True,
                }
            ],
        }
    ]
    raw["routing"] = {
        "main_left_group_id": None,
        "main_right_group_id": None,
        "mono_group_id": None,
        "subwoofer_group_ids": ["sub"],
    }
    return OutputTopology.from_mapping(raw)


def _invalid_passive_topology() -> OutputTopology:
    raw = _passive_topology().to_dict()
    raw["topology_id"] = "invalid_passive_stereo"
    raw["speaker_groups"][1]["channels"][0]["physical_output_index"] = 0
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
            (
                Path(str(files("jasper.active_speaker")))
                / "presets"
                / "bc_de250_dayton_e150he44_v1.json"
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


def test_active_config_path_from_statefile_reads_through_canonical_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket 2.11: setup_status must delegate, not keep a second parser.

    Before the fold, ``active_config_path_from_statefile`` held its own copy
    of the ``JASPER_CAMILLA_STATEFILE`` env-var name, default statefile path,
    and ``config_path:`` regex. This monkeypatches
    ``read_camilla_statefile_config_path`` under the name ``setup_status``
    imports it as, then asserts both the passed-through argument and the
    passed-through return value -- pinning the delegation: a reintroduced
    private parser would never call this patched name, so it would not
    observe the fake return value and this test would fail.
    """

    calls: list[str | Path | None] = []

    def fake_reader(path: str | Path | None = None) -> str | None:
        calls.append(path)
        return "/from/canonical/reader.yml"

    monkeypatch.setattr(setup_mod, "read_camilla_statefile_config_path", fake_reader)

    result = setup_mod.active_config_path_from_statefile("/some/statefile.yml")

    assert result == "/from/canonical/reader.yml"
    assert calls == ["/some/statefile.yml"]


def test_active_config_path_from_statefile_none_becomes_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical reader's ``None`` (not-found) folds to this wrapper's ``""``.

    Preserves ``active_config_path_from_statefile``'s pre-fold ``str`` return
    contract (empty-string sentinel, never ``None``) so its one caller inside
    :func:`jasper.active_speaker.setup_status.read_active_speaker_setup_status`
    (``if not config_path`` / ``config_path or ""`` / ``config_path or None``)
    needed no change.
    """

    monkeypatch.setattr(
        setup_mod, "read_camilla_statefile_config_path", lambda path=None: None
    )

    assert setup_mod.active_config_path_from_statefile() == ""


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


def test_unconfigured_speaker_is_not_passive_or_room_eligible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    topology = new_topology_draft(hardware=_passive_topology().hardware)
    _save_topology(monkeypatch, tmp_path, topology)
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: pytest.fail("unconfigured topology must not build baseline"),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path="/var/lib/camilladsp/configs/sound_current.yml",
    )

    assert status["active"] is False
    assert status["active_group_count"] == 0
    assert status["status"] == "blocked"
    assert status["configured"] is False
    assert status["volume_allowed"] is False
    assert status["grouping_allowed"] is False
    assert status["room_correction_allowed"] is False
    assert status["safety_muted"] is True
    assert status["reason"] == "output_topology_unconfigured"
    assert status["acoustic_commissioning"] == {
        "decision_schema_version": 1,
        "authority": None,
        "required": True,
        "status": "incomplete",
        "allowed": False,
        "reason": "output_topology_unconfigured",
        "detail": "Choose and save a speaker layout before room correction.",
        "setup_href": "/sound/setup/",
    }


@pytest.mark.parametrize(
    ("topology_factory", "contract_issue"),
    [
        pytest.param(_subwoofer_topology, None, id="subwoofer-only"),
        pytest.param(
            _invalid_passive_topology,
            "duplicate_physical_output",
            id="invalid-passive",
        ),
    ],
)
def test_zero_active_layout_requires_flat_dac_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    topology_factory: Callable[[], OutputTopology],
    contract_issue: str | None,
) -> None:
    _save_topology(monkeypatch, tmp_path, topology_factory())
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: pytest.fail("zero-active blocked topology must not build baseline"),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path="/var/lib/camilladsp/configs/sound_current.yml",
    )

    assert status["active"] is False
    assert status["active_group_count"] == 0
    assert status["status"] == "blocked"
    assert status["configured"] is False
    assert status["volume_allowed"] is False
    assert status["grouping_allowed"] is False
    assert status["room_correction_allowed"] is False
    assert status["safety_muted"] is True
    assert status["reason"] == "output_topology_not_ready"
    assert status["acoustic_commissioning"]["authority"] is None
    assert status["acoustic_commissioning"]["allowed"] is False
    assert status["acoustic_commissioning"]["setup_href"] == "/sound/setup/"
    issue_codes = {item["code"] for item in status["issues"]}
    assert "output_topology_not_ready" in issue_codes
    if contract_issue is not None:
        assert contract_issue in issue_codes


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


@pytest.mark.parametrize("role", ["leader", "follower"])
def test_manual_room_authority_explicitly_scopes_out_distributed_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
) -> None:
    """Fresh grouping state scopes comment-free leader/follower readback out."""
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
    running_text = (
        _program_bake_yaml()
        if role == "leader"
        else protected_path.read_text(encoding="utf-8")
    )
    current_path.write_text(running_text, encoding="utf-8")
    active_raw = yaml.safe_dump(yaml.safe_load(running_text), sort_keys=False)
    assert "# Source:" not in active_raw
    monkeypatch.setattr(
        "jasper.multiroom.config.load_config",
        lambda: SimpleNamespace(enabled=True, error=None, role=role),
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
        active_config_text=active_raw,
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
        "differences": [],
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


def test_durable_anchor_mismatch_names_the_field_and_both_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Blind-run finding F-6: a rejected delay left on the durable anchor.

    The apply repointed CamillaDSP's persisted config at a delayed candidate
    and the round ended on a rejection, so the applied profile still described
    the previous graph. Two fingerprints say only THAT they disagree; the
    operator acts on the delay itself, so the binding names it with both
    values. Read with no live readback, which is the durable-anchor level a
    runtime-only `set_active_config_raw` swap never moves.
    """
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    protected_path = tmp_path / "active_speaker_baseline.yml"
    anchor_path = tmp_path / "active_speaker_baseline_candidate_60205a8de2bf.yml"
    manual = _applied_acoustic_profile(measured=False, config_path=protected_path)
    _write_applied_graph(topology, manual, protected_path)
    anchor = yaml.safe_load(protected_path.read_text(encoding="utf-8"))
    anchor["filters"]["as_woofer_delay"]["parameters"]["delay"] = 0.1286
    anchor_path.write_text(yaml.safe_dump(anchor, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(
        setup_mod,
        "build_baseline_profile_candidate",
        lambda *a, **k: _candidate(status="applied", config_path=protected_path),
    )
    monkeypatch.setattr(
        setup_mod, "load_measurement_state", lambda _topology: {"summary": {}},
    )
    monkeypatch.setattr(
        setup_mod, "load_applied_baseline_profile_state", lambda _path=None: manual,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(anchor_path),
    )

    binding = status["protected_profile"]["layer_a_binding"]
    assert binding["status"] == "mismatch"
    assert binding["differences"] == [
        {"field": "as_woofer_delay.delay", "expected": "0.0", "loaded": "0.1286"},
    ]


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
        "differences": [],
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
        _common.ROOM_AUTHORITY_RECEIPT_ABSENT
    )
    assert status["applied_crossover"]["valid"] is True
    assert status["applied_crossover"]["owner"] == "automatic"
    assert status["automatic_candidate"]["ready"] is False


def _denied_receipt_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt_reason: str,
    cause: str = "",
) -> dict:
    """One real setup status whose receipt denial is ``receipt_reason``."""

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
    monkeypatch.setattr(
        commissioning_verification,
        "read_commissioning_room_authority",
        lambda _topology: {
            "allowed": False,
            "authority": "automatic_verified_receipt",
            "reason": receipt_reason,
            "cause": cause,
            "receipt_fingerprint": None,
        },
    )
    return setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )


@pytest.mark.parametrize(
    "receipt_reason",
    [
        _common.ROOM_AUTHORITY_RECEIPT_ABSENT,
        _common.ROOM_AUTHORITY_RECEIPT_STALE,
        _common.ROOM_AUTHORITY_RECEIPT_MALFORMED,
        _common.ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
        _common.ROOM_AUTHORITY_RECEIPT_UNREADABLE,
    ],
)
def test_receipt_denial_reason_reaches_the_room_decision_intact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt_reason: str,
) -> None:
    """Which denial it was survives the hop to the household surface.

    They used to collapse into one opaque code, so a doctor line could say
    only "no receipt" — never whether nothing was ever minted, something moved
    under one that was, an upgrade grew the schema past what an older mint
    recorded, the record could not be opened at all, or the bytes will not
    parse. Those have different remedies, and each carries its own detail —
    a reason with no entry of its own inherits the copy for a receipt that was
    never minted, which sends the household to the wrong remedy.
    """
    acoustic = _denied_receipt_status(monkeypatch, tmp_path, receipt_reason)[
        "acoustic_commissioning"
    ]

    assert acoustic["allowed"] is False
    assert acoustic["authority"] is None
    assert acoustic["receipt_fingerprint"] is None
    assert acoustic["reason"] == receipt_reason
    assert acoustic["detail"] == setup_mod._RECEIPT_DETAIL[receipt_reason]


@pytest.mark.parametrize(
    ("receipt_reason", "expected_code"),
    [
        (_common.ROOM_AUTHORITY_RECEIPT_ABSENT, "speaker_setup_incomplete"),
        (_common.ROOM_AUTHORITY_RECEIPT_STALE, "speaker_setup_incomplete"),
        (_common.ROOM_AUTHORITY_RECEIPT_MALFORMED, "speaker_setup_incomplete"),
        (_common.ROOM_AUTHORITY_RECEIPT_SUPERSEDED, "speaker_setup_incomplete"),
        (_common.ROOM_AUTHORITY_RECEIPT_UNREADABLE, "speaker_readiness_fault"),
    ],
)
def test_room_answers_an_unopenable_receipt_as_a_machine_fault(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt_reason: str,
    expected_code: str,
) -> None:
    """A receipt JTS cannot OPEN is not an unconfigured speaker.

    The four record-level denials are answered by finishing or re-running
    setup. UNREADABLE is not: Active's own detail for this decision says a
    machine-level fault is unlikely to change by re-running, so it routes to a
    non-retryable device-fault code and offers no "Check again" retry -- while
    the block still carries each reason's distinct detail and errno+path for
    the doctor, `/state`, and logs (ruling 11).
    """
    from jasper.web import correction_setup

    cause = "PermissionError:EACCES:/var/lib/jasper/receipt.json"
    status = _denied_receipt_status(monkeypatch, tmp_path, receipt_reason, cause)
    readiness = correction_setup._normalize_room_readiness(status)

    assert readiness.allowed is False
    assert readiness.reason == receipt_reason
    assert readiness.blocker["code"] == expected_code
    # Neither presentation is a retryable "Check again": UNREADABLE is a device
    # fault; the record-level four are answered by finishing setup.
    assert readiness.blocker["retryable"] is False
    # Active's reason-specific detail and the errno+path ride the block.
    assert readiness.blocker["detail"] == setup_mod._RECEIPT_DETAIL[receipt_reason]
    assert readiness.blocker["cause"] == cause
    if expected_code == "speaker_readiness_fault":
        # A machine fault must not send the owner into a retry loop or a wizard.
        assert readiness.blocker["recovery_action"] is None
    else:
        assert readiness.blocker["recovery_action"]["href"] == (
            status["acoustic_commissioning"]["setup_href"]
        )


def test_every_receipt_denial_carries_a_remedy_that_is_its_own() -> None:
    """Totality and distinctness, not presence.

    A reason missing from the table inherits ABSENT's remedy through the
    lookup default, and two reasons sharing a sentence make the split that
    distinguishes them cosmetic. Both are silent failures the parametrized
    test above cannot see, because it reads the same table production reads.
    """

    reasons = {
        _common.ROOM_AUTHORITY_RECEIPT_ABSENT,
        _common.ROOM_AUTHORITY_RECEIPT_STALE,
        _common.ROOM_AUTHORITY_RECEIPT_MALFORMED,
        _common.ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
        _common.ROOM_AUTHORITY_RECEIPT_UNREADABLE,
    }

    assert reasons <= set(setup_mod._RECEIPT_DETAIL)
    details = [setup_mod._RECEIPT_DETAIL[reason] for reason in reasons]
    assert len(set(details)) == len(details)


@pytest.mark.parametrize(
    "candidate_config_written",
    [
        pytest.param(True, id="candidate_config_on_disk"),
        pytest.param(False, id="candidate_config_never_written"),
    ],
)
def test_topology_change_since_the_applied_baseline_discloses_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    candidate_config_written: bool,
) -> None:
    """A rotated topology fingerprint is a notice, not a stop (wave 7j).

    `topology_config_fingerprint` hashes the whole topology dict bar
    `pairing_intent`, so a display-only string that reaches no clamp and no
    emitted filter — `human_output_label`, a speaker group's `label` — used
    to take the box to `blocked`/`safety_muted`, refuse volume and grouping,
    and refuse a v2 measure session. Ruling S10: playback stays on the applied
    graph, measuring stays open, and the fact surfaces as a disclosure.

    The `candidate_config_never_written` case pins the third arm this reaches:
    the candidate-side `active_baseline_config_missing` blocker is suppressed
    too, because a candidate pointing at a file nobody wrote is a pending
    edit, not a reason to mute a speaker whose own applied config is present.
    """
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    applied = _applied_acoustic_profile(config_path=config_path)
    applied["source"]["topology_fingerprint"] = "a" * 64
    _write_applied_graph(topology, applied, config_path)
    # The freshly-built candidate no longer equals the applied one — which is
    # the whole shape of a topology edit, and the second gate the block held:
    # a stale `protected_ready` made the un-applied candidate a blocker too.
    candidate_config_path = (
        config_path if candidate_config_written else tmp_path / "never_written.yml"
    )
    candidate = _candidate(status="draft", config_path=candidate_config_path)
    candidate["source"]["topology_fingerprint"] = "b" * 64
    monkeypatch.setattr(
        setup_mod, "build_baseline_profile_candidate", lambda *a, **k: candidate
    )
    monkeypatch.setattr(
        setup_mod, "load_applied_baseline_profile_state", lambda _path=None: applied
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["status"] == "ready"
    assert status["safety_muted"] is False
    assert status["volume_allowed"] is True
    assert status["grouping_allowed"] is True
    assert [issue["code"] for issue in status["issues"]] == [
        _common.BASELINE_TOPOLOGY_CHANGED
    ]
    assert [issue["severity"] for issue in status["issues"]] == ["warning"]
    assert status["protected_profile"]["topology_current"] is False


def test_a_blocker_outranks_a_notice_for_the_setup_headline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`reason`/`detail` must name what is stopping the box, not what is not.

    The issue list carries two severities now, and the topology notice is
    appended before every later blocker.
    """
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    applied = _applied_acoustic_profile(config_path=config_path)
    applied["source"]["topology_fingerprint"] = "a" * 64
    _write_applied_graph(topology, applied, config_path)
    # Config file still on disk, so the notice's arm is reached; the baseline
    # itself is not applied, so a real blocker lands after it.
    applied["status"] = "draft"
    candidate = _candidate(status="draft", config_path=config_path)
    candidate["source"]["topology_fingerprint"] = "b" * 64
    monkeypatch.setattr(
        setup_mod, "build_baseline_profile_candidate", lambda *a, **k: candidate
    )
    monkeypatch.setattr(
        setup_mod, "load_applied_baseline_profile_state", lambda _path=None: applied
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["status"] == "blocked"
    severities = {issue["severity"] for issue in status["issues"]}
    assert severities == {"warning", "blocker"}
    assert status["reason"] != _common.BASELINE_TOPOLOGY_CHANGED
    blockers = [i for i in status["issues"] if i["severity"] == "blocker"]
    assert status["reason"] == blockers[0]["code"]
    assert status["detail"] == blockers[0]["message"]


def test_verified_automatic_receipt_allows_room_with_loaded_layer_a(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jasper.active_speaker import commissioning_verification

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
    monkeypatch.setattr(
        commissioning_verification,
        "read_commissioning_room_authority",
        lambda _topology: {
            "allowed": True,
            "authority": "automatic_verified_receipt",
            "receipt_fingerprint": "9" * 64,
        },
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["room_correction_allowed"] is True
    assert status["acoustic_commissioning"]["authority"] == (
        setup_mod.ROOM_AUTHORITY_AUTOMATIC_COMMISSIONING_RECEIPT
    )
    assert status["acoustic_commissioning"]["receipt_fingerprint"] == "9" * 64
    assert status["acoustic_commissioning"]["layer_a_identity"] == (
        status["protected_profile"]["layer_a_binding"]["loaded_fingerprint"]
    )


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
        # Gauge fix (2026-07-24): absent from the fixture profile dict, so
        # the "" (never evaluated) default.
        "linearization_outcome": "",
        # PR-L4 item 8: which of `/state`'s two baseline blocks this is. The
        # sibling `baseline_profile` block is the /sound-page staging
        # candidate; this one reports what the speaker is actually running.
        "role": "applied_profile",
        "layer_a_binding": {
            "status": "unverifiable",
            "matches": False,
            "expected_fingerprint": None,
            "loaded_fingerprint": None,
            "differences": [],
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
    # #1666: the candidate lands on its own source-fingerprinted sibling, never
    # baseline_config_path directly -- that literal file is never written by
    # a bare build_baseline_profile_candidate() call (only the real apply
    # transaction's post-success promote publishes it). What CamillaDSP would
    # actually be running is the candidate's own reported path (mirrors
    # active_config_path_from_statefile() reading CamillaDSP's own statefile
    # in production, which always names the loaded sibling, never the
    # promoted canonical copy).
    applied_config_path = str(payload["config"]["path"])
    assert applied_config_path != str(baseline_config_path)

    saved = json.loads(baseline_state_path.read_text(encoding="utf-8"))
    saved["status"] = "applied"
    saved["candidate_fingerprint"] = "declared-wrong"
    expected_applied_fingerprint = baseline_candidate_fingerprint(saved)
    baseline_state_path.write_text(json.dumps(saved), encoding="utf-8")

    ready = setup_mod.read_active_speaker_setup_status(
        active_config_path=applied_config_path,
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
        active_config_path=applied_config_path,
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
    # PR-L4 item 8: exactly the pair that cost the 2026-07-27 forensics real
    # time — a re-derived staging candidate reporting one thing beside an
    # applied profile reporting another, with nothing saying they answer
    # different questions. Both blocks now name their role, point at the one
    # that reports what is audible, and state outright whether they agree.
    assert stale["baseline_profile"]["role"] == "staging_candidate"
    assert stale["baseline_profile"]["live_answer_key"] == "protected_profile"
    assert stale["protected_profile"]["role"] == "applied_profile"
    assert stale["baseline_profile"]["matches_applied"] is False
    assert (
        stale["baseline_profile"]["candidate_fingerprint"]
        != stale["protected_profile"]["candidate_fingerprint"]
    )


def test_state_says_when_the_staging_candidate_is_the_applied_one(
    tmp_path: Path, monkeypatch
) -> None:
    """The agreeing case: `matches_applied` is a real comparison, not a field
    that is always False. Without it a reader still has to diff two
    differently-shaped blocks to learn whether they are looking at one answer
    or two."""
    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(tmp_path / "absent.yml"),
        baseline_state_path=tmp_path / "absent_baseline.json",
    )
    baseline = status.get("baseline_profile")
    if not isinstance(baseline, dict):  # unreadable topology — nothing to compare
        return
    # No applied profile at all is "nothing to compare against", never a
    # disagreement — the same unknown-vs-zero rule the cloud blocks follow.
    assert baseline["matches_applied"] is None
    assert baseline["role"] == "staging_candidate"


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

    assert status["active"] is None
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
        # #2412 Wave 4. `None` here is the derivation answering honestly, not a
        # placeholder: this fixture's topology is a `SimpleNamespace` with only
        # a `topology_id`, so no route resolves and there is no transport to
        # name. The armed/unarmed polarities are pinned in
        # `test_commissioning_summary_transport_*` below.
        "transport": None,
    }


@pytest.mark.parametrize(
    "topology_factory, armed, expected",
    [
        # THE MARKER IS NO LONGER THE DISCRIMINATOR, and these four rows are
        # what say so. Both polarities of the reconciler's real ACTIVE-endpoint
        # marker are still driven, and both now answer `ring`: #2285 P2 deleted
        # `resolve_output_layout` case 2's marker read, so a box that resolves
        # the active outputd lane names the ring unconditionally.
        (_active_topology, False, "ring"),
        (_active_topology, True, "ring"),
        # ROLEFULNESS IS NOT THE DISCRIMINATOR EITHER, and this pair keeps
        # saying so: a passive box resolves the active outputd lane like any
        # other. Only an unreadable topology or a route with no device reports
        # null. A passive box is not a null case; these pins keep that sentence
        # true.
        (_passive_topology, False, "ring"),
        (_passive_topology, True, "ring"),
    ],
    ids=["active_unarmed", "active_armed", "passive_unarmed", "passive_armed"],
)
def test_commissioning_summary_transport_follows_the_box(
    monkeypatch, topology_factory, armed, expected
) -> None:
    """`/state` names the transport of the box it is asked about (#2412 Wave 4).

    `curl /state | jq .` is this campaign's standing probe, and a commissioning
    block that reported a device without its transport is the exact half-fact
    that produced #2412.

    **The production contract is SINGLE-TRANSPORT since #2285 P2** (post-seal
    correction 9). Wave 4 shipped this field as marker-following — `alsa`
    unarmed, `ring` armed — and P2's deletion of case 2's marker read made that
    unreachable: `/state.transport` is `ring` or `null` on a production box, and
    the `alsa` value survives only for a device string that is not the ring
    (the explicit lab/CI override route, pinned at the derivation by
    `test_commissioning_summary_transport_is_null_when_no_device_resolves`'s
    neighbours in `test_fanin_coupling`). Both marker polarities are still
    driven here precisely BECAUSE the answer no longer depends on them — a row
    pair that agrees is the evidence for the deletion, not a redundant case.

    NON-CONSTANCY lives on the ring/`null` axis now, and its rows are the two
    dedicated siblings below — `test_state_reports_null_when_the_chooser_answers_no_device`
    (the surface honours a no-device answer) and
    `test_commissioning_summary_transport_is_null_on_an_unreadable_topology`.
    They are NOT folded in as rows here: a null row cannot be driven from a
    topology — every registered `DacProfile` declares an active outputd lane, so
    `resolve_output_layout`'s no-device fall-through is unreachable by fixture
    and both siblings must stub a collaborator to reach it. Duplicating that
    stub as a fifth row would be a second owner of one fact. Verified by
    mutation instead: a constant `return TRANSPORT_RING` in
    `_commissioning_transport` is killed by this module.

    Driven through the reconciler's real ACTIVE-endpoint marker rather than by
    stubbing the answer, so this also pins that `/state` reads the SAME chooser
    commissioning emits through: a `/state` that answered from its own
    derivation could disagree with the journal about one box, which is the
    second-source-of-truth failure the single helper exists to prevent.
    """
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: armed
    )
    result = setup_mod.commissioning_summary(
        topology_factory(),
        profile=None,
        applied_profile=None,
        measurements=None,
    )
    assert result["transport"] == expected


def test_state_reports_null_when_the_chooser_answers_no_device(monkeypatch) -> None:
    """The SURFACE honours a no-device answer — the line between the two.

    `_commissioning_transport` is one `try` and one `return`, and only the `try`
    half is pinned elsewhere: the unreadable-topology test returns through the
    `except` branch and never reaches the return. So the return line would be
    unguarded, and a mutant that swallowed the null passed the entire affected
    set. This is the case that kills it.

    Stubs the COLLABORATOR'S CONTRACT (`resolve_active_playback_device` answering
    no device) rather than building a lane-less topology: the rule under test is
    "the surface honours a no-device answer", and a hand-built fixture would pin
    the fixture instead. The real branch this stands in for is
    `output_topology.resolve_output_layout`'s fall-through, which returns
    `playback_device=None` for a profile with no active outputd lane — reachable,
    not theoretical.
    """
    monkeypatch.setattr(
        "jasper.active_speaker.playback_route.resolve_active_playback_device",
        lambda topology, **kw: (None, "missing"),
    )

    result = setup_mod.commissioning_summary(
        _active_topology(), profile=None, applied_profile=None, measurements=None
    )

    assert result["transport"] is None
    # ...and the block still answers in full: a null transport is a reported
    # value, not a truncated payload.
    assert len(result) == 8


def test_commissioning_summary_transport_is_null_on_an_unreadable_topology() -> None:
    """An observability field must never be why `/state` stops answering.

    `resolve_output_layout` walks `topology.hardware` unguarded, so a topology
    object that cannot answer raises a class none of this module's sibling
    derivations do. The block reports `null` and keeps its other seven keys
    rather than propagating.
    """
    result = setup_mod.commissioning_summary(
        object(), profile=None, applied_profile=None, measurements=None
    )
    assert result["transport"] is None
    assert result["phase"] == "idle"
    assert len(result) == 8


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


def test_commissioning_summary_last_capture_is_the_newest_across_both_maps() -> None:
    """The newest record (by created_at) across BOTH maps wins, regardless of
    which map it came from -- and the winning record's `snr_db` is read from
    its `worst_relevant` SNR entry, not some other field.

    Deliberately absent: that entry's `band_id`. `last_capture` is a compact
    `/state` household card -- terminal display, not a forensic record -- and
    should not grow a fifth key. The exact-dict-equality assertion below is
    what holds that shape, so a fifth key fails here first. Band identity
    lives on the diagnostic surfaces instead: `{role}_snr_band` on
    `event=correction.crossover_v2_measure_diag` and in
    `analysis_diagnostic_summary` (#2613, PR #2618).
    """
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
