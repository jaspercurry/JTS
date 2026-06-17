"""prepare_driver_commissioning_config: emit + safety-assert the per-driver config.

The per-driver commissioning step emits the production graph with only the target
driver audible, runs the protection-while-audible gate, and returns evidence
WITHOUT loading CamillaDSP (the guarded load is a separate step). These tests pin
that a woofer target keeps the tweeter muted, a tweeter target keeps its
protection, unknown targets fail closed, and the transient config is never the
durable boot config.
"""

from __future__ import annotations

from pathlib import Path

from jasper.active_speaker import COMMISSIONING_CONFIG_KIND, prepare_driver_commissioning_config

# Reuse the canonical staging fixtures (mono 2-way DAC8x topology + a passing
# CamillaDSP validation stub).
from tests.test_active_speaker_staging import _topology, _valid_config


def _prepare(role: str, *, tmp_path: Path, group_id: str = "mono", topology=None):
    return prepare_driver_commissioning_config(
        topology or _topology(),
        speaker_group_id=group_id,
        role=role,
        config_path=tmp_path / "commission.yml",
        validate=_valid_config,
        created_at="2026-06-16T12:00:00Z",
    )


def test_woofer_target_prepared_with_tweeter_muted(tmp_path: Path):
    payload = _prepare("woofer", tmp_path=tmp_path)
    assert payload["kind"] == COMMISSIONING_CONFIG_KIND
    assert payload["status"] == "prepared"
    assert payload["target"]["role"] == "woofer"
    assert payload["issues"] == []
    ev = payload["audible_evidence"]
    assert ev["passed"] is True
    # Woofer target -> the tweeter is not audible (stays muted).
    assert ev["audible_tweeter_outputs"] == []
    assert (tmp_path / "commission.yml").exists()
    # The config is emitted but the load is a SEPARATE guarded step.
    assert payload["load"]["load_allowed"] is False


def test_tweeter_target_prepared_with_protection_intact(tmp_path: Path):
    payload = _prepare("tweeter", tmp_path=tmp_path)
    assert payload["status"] == "prepared"
    ev = payload["audible_evidence"]
    assert ev["passed"] is True
    assert ev["audible_tweeter_outputs"] != []
    assert ev["checks"]["tweeter_protected_while_audible"] is True
    # The protection-while-audible gate is recorded as a required gate.
    gate_ids = {g["id"] for g in payload["required_gates"]}
    assert "driver_protection_while_audible" in gate_ids


def test_unknown_role_blocks_closed(tmp_path: Path):
    payload = _prepare("subwoofer", tmp_path=tmp_path)
    assert payload["status"] == "blocked"
    codes = {issue["code"] for issue in payload["issues"]}
    assert "commissioning_target_role_unknown" in codes
    # No config is committed for an unresolvable target.
    assert payload["audible_evidence"] == {}


def test_unknown_group_blocks_closed(tmp_path: Path):
    payload = _prepare("woofer", group_id="not_a_group", tmp_path=tmp_path)
    assert payload["status"] == "blocked"
    codes = {issue["code"] for issue in payload["issues"]}
    assert "commissioning_target_group_unknown" in codes


def test_prepare_never_allows_load(tmp_path: Path):
    # prepare emits + asserts but NEVER loads; the guarded load is a separate
    # step gated on this evidence.
    payload = _prepare("woofer", tmp_path=tmp_path)
    assert payload["load"]["load_allowed"] is False
    assert (
        payload["load"]["load_gate"] == "driver_commissioning_load_preflight_required"
    )


def test_commissioning_config_path_is_not_the_boot_config_path():
    # The transient per-driver config must never overwrite the all-muted staged
    # boot config (the crash-recovery-MUTED invariant).
    from jasper.active_speaker.staging import (
        DEFAULT_COMMISSIONING_CONFIG_NAME,
        DEFAULT_STAGED_CONFIG_NAME,
        commissioning_config_path,
        staged_config_path,
    )

    assert DEFAULT_COMMISSIONING_CONFIG_NAME != DEFAULT_STAGED_CONFIG_NAME
    assert commissioning_config_path() != staged_config_path()


def _two_active_group_topology():
    """A topology with two active groups — invalid for the single-speaker model."""
    from jasper.output_topology import OutputTopology

    raw = _topology().to_dict()
    raw["speaker_groups"].append({
        "id": "mono_b",
        "label": "Second cabinet",
        "kind": "mono",
        "mode": "active_2_way",
        "channels": [
            {"role": "woofer", "physical_output_index": 2, "identity_verified": True},
            {
                "role": "tweeter",
                "physical_output_index": 3,
                "identity_verified": True,
                "startup_muted": True,
                "protection_required": True,
                "protection_status": "present",
            },
        ],
    })
    return OutputTopology.from_mapping(raw)


def test_multi_group_topology_fails_closed(tmp_path: Path):
    # prepare unmutes the bound preset's whole role; that is only correctly
    # scoped because _bind_preset_to_topology enforces a SINGLE active speaker
    # group. Pin that contract: a multi-group topology must fail closed (never
    # resolve a role across groups and unmute another speaker's drivers).
    payload = prepare_driver_commissioning_config(
        _two_active_group_topology(),
        speaker_group_id="mono",
        role="woofer",
        config_path=tmp_path / "commission.yml",
        validate=_valid_config,
        created_at="2026-06-16T12:00:00Z",
    )
    assert payload["status"] == "blocked"
    assert payload["audible_evidence"] == {}


def test_build_active_commissioning_context_resolves_single_group():
    # N1: direct coverage of the helper both staging entry points share.
    from jasper.active_speaker.staging import _build_active_commissioning_context
    from jasper.camilla_config_contract import ACTIVE_OUTPUTD_PLAYBACK_DEVICE

    ctx = _build_active_commissioning_context(
        _topology(), preset=None, crossover_preview=None, playback_device=None
    )
    assert ctx["bound_preset"] is not None
    assert [group.id for group in ctx["active_groups"]] == ["mono"]
    assert ctx["resolved_playback_device"] == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert ctx["issues"] == []
    gate_ids = {gate["id"] for gate in ctx["gates"]}
    assert "explicit_active_playback_device" in gate_ids
