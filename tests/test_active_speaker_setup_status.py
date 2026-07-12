# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import jasper.active_speaker.setup_status as setup_mod
from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.measurement import (
    active_driver_targets,
    start_active_comparison_set,
)
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
    }
    if with_snapshot:
        preset = json.loads(
            Path(
                "jasper/active_speaker/presets/"
                "bc_de250_dayton_e150he44_v1.json"
            ).read_text(encoding="utf-8")
        )
        profile["recomposition_snapshot"] = {
            "schema_version": 1,
            "topology_id": "bench_mono",
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
        }
    return profile


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
    config_path.write_text("pipeline: []\n", encoding="utf-8")
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
        lambda _path=None: _applied_acoustic_profile(config_path=config_path),
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["configured"] is True
    assert status["room_correction_allowed"] is True
    assert status["acoustic_commissioning"]["status"] == "ready"
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
    # applied profile's source fingerprint is surfaced for correlation.
    assert status["commissioning"]["room_correction_allowed"] is True
    assert status["commissioning"]["room_correction_allowed"] == (
        status["acoustic_commissioning"]["allowed"]
    )
    assert status["commissioning"]["applied_profile_fingerprint"] == "source-fp"
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
    config_path.write_text("pipeline: []\n", encoding="utf-8")
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
    manual = _applied_acoustic_profile(
        measured=False,
        config_path=config_path,
    )
    manual["tuning_owner"] = "manual"
    manual["recomposition_snapshot"]["tuning_owner"] = "manual"
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
    }


def test_applied_automatic_snapshot_allows_room_after_measurement_store_clears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology()
    _save_topology(monkeypatch, tmp_path, topology)
    config_path = tmp_path / "active_speaker_baseline.yml"
    config_path.write_text("pipeline: []\n", encoding="utf-8")
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
    automatic = _applied_acoustic_profile(config_path=config_path)
    automatic["tuning_owner"] = "automatic"
    automatic["recomposition_snapshot"]["tuning_owner"] = "automatic"
    monkeypatch.setattr(
        setup_mod,
        "load_applied_baseline_profile_state",
        lambda _path=None: automatic,
    )

    status = setup_mod.read_active_speaker_setup_status(
        active_config_path=str(config_path),
    )

    assert status["room_correction_allowed"] is True
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
    config_path.write_text("pipeline: []\n", encoding="utf-8")
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
        "topology_current": True,
        "provisional": False,
        "recomposition_snapshot_available": False,
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
    config_path.write_text("pipeline: []\n", encoding="utf-8")
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
        lambda _path=None: _applied_acoustic_profile(config_path=config_path),
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
    config_path.write_text("pipeline: []\n", encoding="utf-8")
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
        lambda _path=None: _applied_acoustic_profile(config_path=config_path),
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
