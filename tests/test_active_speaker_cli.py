# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import jasper.active_speaker.startup_load as startup_load_mod
from jasper.active_speaker import (
    HARDWARE_PROBE_EVIDENCE_SOURCE,
    OPERATOR_EVIDENCE_SOURCE,
    PATH_SAFETY_EVIDENCE_KIND,
    requirements_payload,
)
from jasper.cli.active_speaker import main


@pytest.fixture(autouse=True)
def _stub_audio_hardware_reconcile(monkeypatch):
    def fake_manage_units(*units: str, **kwargs):
        return {"ok": True, "rc": 0}

    monkeypatch.setattr(startup_load_mod, "manage_units", fake_manage_units)


def _preset() -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "cli-test-v1",
        "name": "CLI test preset",
        "way_count": 2,
        "channel_map": {
            "layout": "mono",
            "outputs": [
                {
                    "index": 0,
                    "side": "mono",
                    "driver_role": "woofer",
                    "label": "mono woofer",
                    "startup_muted": True,
                },
                {
                    "index": 1,
                    "side": "mono",
                    "driver_role": "tweeter",
                    "label": "mono tweeter",
                    "startup_muted": True,
                },
            ],
        },
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "tweeter": {"manufacturer": "Example", "model": "Tweeter"},
        },
        "crossover_regions": [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": 1600,
            "target_type": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "delay_range_ms": [0.0, 0.5],
            "null_depth_threshold_db": 25,
        }],
        "safety": {
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    }


def _write_preset(path: Path) -> Path:
    path.write_text(json.dumps(_preset()), encoding="utf-8")
    return path


def _passing_path_evidence() -> dict:
    paths = {}
    for requirement in requirements_payload()["requirements"]:
        paths[requirement["id"]] = {
            check: True for check in requirement["checks"]
        }
    return {
        "artifact_schema_version": 1,
        "kind": PATH_SAFETY_EVIDENCE_KIND,
        "evidence_source": OPERATOR_EVIDENCE_SOURCE,
        "paths": paths,
    }


def test_startup_template_cli_writes_yaml_without_check(tmp_path: Path, capsys):
    preset = _write_preset(tmp_path / "preset.json")
    out = tmp_path / "active.yml"

    code = main([
        "startup-template",
        str(preset),
        "--playback-device",
        "hw:ActiveDAC",
        "--output",
        str(out),
        "--baseline-id",
        "baseline-cli",
        "--no-check",
    ])

    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "baseline_id=baseline-cli" in text
    assert 'device: "hw:ActiveDAC"' in text
    assert "mute: true" in text
    assert "Validation: skipped" in capsys.readouterr().out


def test_startup_template_cli_reports_missing_validator(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    preset = _write_preset(tmp_path / "preset.json")
    out = tmp_path / "active.yml"
    monkeypatch.setattr("jasper.dsp_apply._camilladsp_binary", lambda: None)

    code = main([
        "startup-template",
        str(preset),
        "--playback-device",
        "hw:ActiveDAC",
        "--output",
        str(out),
    ])

    assert code == 0
    assert out.exists()
    printed = capsys.readouterr().out
    assert "Validation: missing" in printed
    assert "syntax preflight skipped" in printed


def test_startup_template_cli_json_includes_validation_status(
    tmp_path: Path,
    capsys,
):
    preset = _write_preset(tmp_path / "preset.json")
    out = tmp_path / "active.yml"

    code = main([
        "startup-template",
        str(preset),
        "--playback-device",
        "hw:ActiveDAC",
        "--output",
        str(out),
        "--no-check",
        "--json",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["preset_id"] == "cli-test-v1"
    assert payload["output"] == str(out)
    assert payload["validation"]["status"] == "skipped"


def test_startup_template_cli_invalid_preset_exits_2(tmp_path: Path):
    preset = tmp_path / "preset.json"
    preset.write_text("[]", encoding="utf-8")

    try:
        main([
            "startup-template",
            str(preset),
            "--playback-device",
            "hw:ActiveDAC",
            "--output",
            str(tmp_path / "active.yml"),
        ])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected parser exit for invalid preset")


def test_startup_template_cli_missing_output_parent_exits_2(tmp_path: Path):
    preset = _write_preset(tmp_path / "preset.json")

    try:
        main([
            "startup-template",
            str(preset),
            "--playback-device",
            "hw:ActiveDAC",
            "--output",
            str(tmp_path / "missing" / "active.yml"),
        ])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected parser exit for missing output parent")


def test_path_audit_cli_prints_requirements(capsys):
    code = main(["path-audit", "--requirements"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "Active speaker path-safety requirements" in printed
    assert "tts_cues" in printed


def test_path_audit_cli_blocks_incomplete_evidence(tmp_path: Path, capsys):
    evidence = _passing_path_evidence()
    del evidence["paths"]["rollback_configs"]
    evidence_path = tmp_path / "path_safety.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    code = main(["path-audit", str(evidence_path)])

    assert code == 1
    printed = capsys.readouterr().out
    assert "Path safety: blocked" in printed
    assert "rollback_configs" in printed


def test_path_audit_cli_json_passes_complete_evidence(tmp_path: Path, capsys):
    evidence_path = tmp_path / "path_safety.json"
    evidence_path.write_text(json.dumps(_passing_path_evidence()), encoding="utf-8")

    code = main(["path-audit", str(evidence_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["requirements_met"] is True
    assert payload["ok_to_load_active_config"] is False
    assert payload["load_gate"] == "hardware_probe_required"


def test_path_audit_cli_requires_evidence_or_requirements():
    try:
        main(["path-audit"])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected parser exit without evidence")


def test_path_probe_cli_writes_probe_backed_evidence(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    output = tmp_path / "path_safety.json"

    def fake_evidence(*args, **kwargs):
        paths = {}
        for requirement in requirements_payload()["requirements"]:
            paths[requirement["id"]] = {
                check: True for check in requirement["checks"]
            }
        return {
            "artifact_schema_version": 1,
            "kind": PATH_SAFETY_EVIDENCE_KIND,
            "evidence_source": HARDWARE_PROBE_EVIDENCE_SOURCE,
            "evidence_mode": "startup_load_preflight",
            "paths": paths,
        }

    monkeypatch.setattr(
        "jasper.cli.active_speaker.load_output_topology_strict",
        lambda path=None: object(),
    )
    monkeypatch.setattr("jasper.cli.active_speaker.load_staged_startup_config", lambda: {})
    monkeypatch.setattr("jasper.cli.active_speaker.load_calibration_level_state", lambda: {})
    monkeypatch.setattr(
        "jasper.cli.active_speaker.build_startup_load_path_safety_evidence",
        fake_evidence,
    )

    code = main([
        "path-probe",
        "--current-config",
        "/tmp/protected.yml",
        "--output",
        str(output),
        "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["evidence_path"] == str(output)
    assert payload["report"]["load_gate"] == "ready"
    assert json.loads(output.read_text(encoding="utf-8"))["evidence_source"] == (
        HARDWARE_PROBE_EVIDENCE_SOURCE
    )


# --- commission-load / commission-rollback (the operator trigger) ------------
#
# These wire the dormant guarded per-driver load (exhaustively tested in
# tests/test_active_speaker_commission_load.py) to the CLI. The focus here is the
# OPERATOR SURFACE: the CamillaController seam wiring (inline set_active_config_raw,
# get_active_config_raw read-back, get_config_file_path anchor), single-flight,
# the dry-run preflight, and exit codes — not re-testing the load transaction.

from tests.test_active_speaker_commission_load import _block  # noqa: E402
from tests.active_speaker_fixtures import (  # noqa: E402
    mono_output_topology as _topology,
)
from tests.test_active_speaker_startup_load import (  # noqa: E402
    _staged,
)


class _FakeController:
    """Mimics the CamillaController seams the commission-load CLI uses.

    Inline transport: ``set_active_config_raw`` swaps the running graph (and
    block-style re-serializes it like real CamillaDSP active_raw()); the
    persisted ``config_file_path`` (the statefile anchor) never moves.
    """

    def __init__(self, persisted_path: str) -> None:
        self.persisted_path = str(persisted_path)
        self.running_raw: str | None = None
        self.applied_texts: list[str] = []

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.persisted_path

    async def set_active_config_raw(
        self, text: str, *, best_effort: bool = False
    ) -> bool:
        self.applied_texts.append(text)
        self.running_raw = _block(text)
        return True

    async def get_active_config_raw(self, *, best_effort: bool = False) -> str | None:
        return self.running_raw


def _commission_env(monkeypatch, tmp_path: Path, controller: _FakeController) -> dict:
    staged = _staged(tmp_path)
    staged_path = staged["config"]["path"]
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {staged_path}\nmute: false\n", encoding="utf-8")

    monkeypatch.setattr(
        "jasper.cli.active_speaker.load_output_topology_strict",
        lambda path=None: _topology(),
    )
    monkeypatch.setattr(
        "jasper.cli.active_speaker.load_staged_startup_config", lambda: staged
    )
    monkeypatch.setattr(
        "jasper.cli.active_speaker._camilla_controller", lambda: controller
    )
    # The transient commissioning config writes to /var/lib/camilladsp/configs by
    # default (correct on the Pi, unwritable in CI) — redirect it to tmp.
    monkeypatch.setattr(
        "jasper.active_speaker.staging.commissioning_config_path",
        lambda **kwargs: tmp_path / "commission.yml",
    )
    # Preset-fallback path (matches _staged, which stages from the bundled preset).
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft", lambda path=None: {}
    )
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_preview.load_crossover_preview",
        lambda path=None, current_design_draft=None: {"status": "not_prepared"},
    )
    # The startup-load gate requires a real camilladsp --check VALID (not the
    # "binary missing" skip). Point validation at a stub binary that exits 0 so
    # the load is deterministic without the real CamillaDSP toolchain.
    fake_camilla = tmp_path / "camilladsp"
    fake_camilla.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_camilla.chmod(0o755)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(fake_camilla))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE", str(tmp_path / "path_safety.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
        str(tmp_path / "commission_load.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    # Stage-5 ramp + per-driver floor tri-state live in their own files.
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE", str(tmp_path / "ramp.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(tmp_path / "safe.json")
    )
    return {"staged": staged, "staged_path": staged_path, "statefile": statefile}


def _arm_woofer(monkeypatch, tmp_path, capsys):
    from jasper.active_speaker.calibration_level import MIN_TEST_LEVEL_DBFS

    controller = _FakeController("placeholder")
    env = _commission_env(monkeypatch, tmp_path, controller)
    controller.persisted_path = env["staged_path"]
    assert main(["commission-load", "--group", "mono", "--role", "woofer"]) == 0
    capsys.readouterr()
    return controller, env, MIN_TEST_LEVEL_DBFS


def test_commission_ramp_step_then_ack_cli(monkeypatch, tmp_path: Path, capsys):
    controller, env, floor = _arm_woofer(monkeypatch, tmp_path, capsys)

    code = main([
        "commission-ramp", "step", "--group", "mono", "--role", "woofer", "--json"
    ])
    step = json.loads(capsys.readouterr().out)
    assert code == 0
    assert step["status"] == "stepped"
    assert step["next_gain_db"] == floor
    assert step["safe_playback"]["floor_status"] == "floor_pending_operator"
    # The running graph now carries the woofer un-muted at the audible floor.
    assert yaml.safe_load(controller.running_raw)["filters"]["as_out0_commission_mute"][
        "parameters"
    ]["gain"] == floor

    code = main([
        "commission-ramp", "ack", "--outcome", "heard_correct_driver", "--json"
    ])
    ack = json.loads(capsys.readouterr().out)
    assert code == 0
    assert ack["status"] == "confirmed"
    assert ack["safe_playback"]["floor_status"] == "floor_confirmed"


def test_commission_ramp_abort_cli_remutes(monkeypatch, tmp_path: Path, capsys):
    controller, env, _ = _arm_woofer(monkeypatch, tmp_path, capsys)
    assert main(["commission-ramp", "step", "--group", "mono", "--role", "woofer"]) == 0
    capsys.readouterr()

    code = main(["commission-ramp", "abort", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "aborted"
    # Re-muted: the last thing applied to the running graph is the staged config.
    assert controller.applied_texts[-1] == Path(env["staged_path"]).read_text(
        encoding="utf-8"
    )


def test_commission_ramp_tweeter_blocked_before_woofer_cli(
    monkeypatch, tmp_path: Path, capsys
):
    # Arm the tweeter and try to ramp it before the woofer is confirmed: the gate
    # blocks (woofer-before-tweeter) and the CLI exits non-zero.
    controller = _FakeController("placeholder")
    env = _commission_env(monkeypatch, tmp_path, controller)
    controller.persisted_path = env["staged_path"]
    assert main(["commission-load", "--group", "mono", "--role", "tweeter"]) == 0
    capsys.readouterr()
    loads_before = len(controller.applied_texts)

    code = main([
        "commission-ramp", "step", "--group", "mono", "--role", "tweeter", "--json"
    ])
    step = json.loads(capsys.readouterr().out)
    assert code == 1
    assert step["status"] == "gate_blocked"
    assert step["gate"]["checks"]["role_order_woofer_first"] is False
    assert len(controller.applied_texts) == loads_before  # loaded nothing more


def test_commission_load_cli_arms_woofer_at_floor(monkeypatch, tmp_path: Path, capsys):
    from jasper.active_speaker import load_commission_load_state

    controller = _FakeController("placeholder")
    env = _commission_env(monkeypatch, tmp_path, controller)
    controller.persisted_path = env["staged_path"]  # active graph IS the staged config

    code = main(["commission-load", "--group", "mono", "--role", "woofer", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["load"]["status"] == "loaded"
    assert payload["load"]["target"]["role"] == "woofer"
    assert payload["load"]["target"]["audible_outputs"] == [0]
    assert payload["load"]["live_evidence"]["passed"] is True
    # The inline seam applied exactly the woofer commissioning config (not the
    # boot config) into the running graph, woofer (index 0) un-muted.
    assert len(controller.applied_texts) == 1
    assert "audible_outputs=[0]" in controller.applied_texts[0]
    # Crash-recovery-MUTED: the persisted statefile still points at the staged
    # boot config.
    assert payload["load"]["durable_statefile_intact"] is True

    state = load_commission_load_state()
    assert state["status"] == "loaded"
    assert state["rollback_available"] is True


def test_commission_load_cli_single_flight_refuses_second_load(
    monkeypatch, tmp_path: Path, capsys
):
    controller = _FakeController("placeholder")
    env = _commission_env(monkeypatch, tmp_path, controller)
    controller.persisted_path = env["staged_path"]

    assert main(["commission-load", "--group", "mono", "--role", "woofer"]) == 0
    capsys.readouterr()
    assert len(controller.applied_texts) == 1

    # A second arm while one is already loaded must refuse and load nothing.
    code = main(["commission-load", "--group", "mono", "--role", "tweeter"])
    printed = capsys.readouterr().out
    assert code == 1
    assert "refused" in printed.lower()
    assert len(controller.applied_texts) == 1  # nothing new applied


def test_commission_load_cli_dry_run_loads_nothing(monkeypatch, tmp_path: Path, capsys):
    controller = _FakeController("placeholder")
    env = _commission_env(monkeypatch, tmp_path, controller)
    controller.persisted_path = env["staged_path"]

    code = main([
        "commission-load", "--group", "mono", "--role", "woofer", "--dry-run", "--json"
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["preflight"]["load_allowed"] is True
    # Dry-run preflight emits the candidate config but loads NOTHING.
    assert controller.applied_texts == []


def test_commission_rollback_cli_reloads_staged_all_muted(
    monkeypatch, tmp_path: Path, capsys
):
    from jasper.active_speaker import load_commission_load_state

    controller = _FakeController("placeholder")
    env = _commission_env(monkeypatch, tmp_path, controller)
    controller.persisted_path = env["staged_path"]

    assert main(["commission-load", "--group", "mono", "--role", "woofer"]) == 0
    capsys.readouterr()

    code = main(["commission-rollback", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["rollback"]["status"] == "rolled_back"
    # The last thing applied to the running graph is the all-muted staged config.
    assert controller.applied_texts[-1] == Path(env["staged_path"]).read_text(
        encoding="utf-8"
    )
    assert load_commission_load_state()["status"] == "rolled_back"


def test_environment_probe_cli_json_reports_payload(monkeypatch, capsys):
    monkeypatch.setattr(
        "jasper.cli.active_speaker.probe_active_speaker_environment",
        lambda **kwargs: {
            "status": "blocked",
            "load_gate": "path_safety_evidence_missing",
            "ok_to_load_active_config": False,
            "camilla_config": {
                "classification": "jts_outputd_stereo",
                "path": "/etc/camilladsp/outputd-cutover.yml",
                "label": "JTS outputd stereo config",
                "playback_device": "outputd_content_playback",
                "playback_channels": 2,
                "volume_limit_db": 0.0,
            },
            "camilla_validation": {"status": "valid"},
            "alsa": {"available": True, "devices": [1]},
            "path_safety": {
                "status": "missing",
                "load_gate": "evidence_missing",
            },
            "issues": [],
        },
    )

    code = main(["environment-probe", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["camilla_config"]["classification"] == "jts_outputd_stereo"


def test_runtime_safe_graph_cli_writes_staged_config_for_active_topology(
    tmp_path: Path,
    capsys,
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _active_yaml,
        _flat_yaml,
        _staged_metadata,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    staged = tmp_path / "active_speaker_staged_startup.yml"
    staged.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(
        json.dumps(_staged_metadata(topology, staged)),
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {flat}\n", encoding="utf-8")

    code = main([
        "runtime-safe-graph",
        "--topology",
        str(topology_path),
        "--statefile",
        str(statefile),
        "--flat-config",
        str(flat),
        "--staged-metadata",
        str(metadata),
        "--write-statefile",
        "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "select_active_startup"
    assert payload["statefile_written"] is True
    assert f"config_path: {staged}" in statefile.read_text(encoding="utf-8")


def test_runtime_safe_graph_cli_prefers_applied_baseline_state(
    tmp_path: Path,
    capsys,
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_baseline_yaml,
        _active_topology,
        _active_yaml,
        _flat_yaml,
        _staged_metadata,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    staged = tmp_path / "active_speaker_staged_startup.yml"
    staged.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    baseline = tmp_path / "active_speaker_baseline.yml"
    baseline.write_text(_active_baseline_yaml("mono", 2), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(
        json.dumps(_staged_metadata(topology, staged)),
        encoding="utf-8",
    )
    profile_state = tmp_path / "active_speaker_baseline_profile.json"
    profile_state.write_text(
        json.dumps({"status": "applied", "config": {"path": str(baseline)}}),
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {staged}\n", encoding="utf-8")

    code = main([
        "runtime-safe-graph",
        "--topology",
        str(topology_path),
        "--statefile",
        str(statefile),
        "--flat-config",
        str(flat),
        "--staged-metadata",
        str(metadata),
        "--applied-baseline-state",
        str(profile_state),
        "--write-statefile",
        "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "select_active_baseline"
    assert payload["preferred_graph"]["classification"] == "approved_active_runtime"
    assert payload["statefile_written"] is True
    assert f"config_path: {baseline}" in statefile.read_text(encoding="utf-8")


def test_runtime_safe_graph_cli_rejects_corrupt_topology_without_repair(
    tmp_path: Path,
    capsys,
):
    from tests.test_active_speaker_runtime_contract import _flat_yaml

    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text("config_path: /tmp/old.yml\nvolume: -20.0\n", encoding="utf-8")
    before = statefile.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main([
            "runtime-safe-graph",
            "--topology",
            str(topology_path),
            "--statefile",
            str(statefile),
            "--flat-config",
            str(flat),
            "--write-statefile",
            "--json",
        ])

    assert exc.value.code == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert statefile.read_text(encoding="utf-8") == before
