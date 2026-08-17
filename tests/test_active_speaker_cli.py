# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
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


def test_persisted_candidate_source_is_owned_only_by_safe_graph_host() -> None:
    repo = Path(__file__).resolve().parents[1]
    owners: set[tuple[str, str]] = set()

    for path in (repo / "jasper").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr
                if isinstance(call.func, ast.Attribute)
                else None
            )
            if name != "classify_bass_extension_graph":
                continue
            source = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "evidence_source"),
                None,
            )
            if not isinstance(source, ast.Constant) or source.value != "persisted_candidate":
                continue
            enclosing = [
                function
                for function in functions
                if function.lineno <= call.lineno <= (function.end_lineno or call.lineno)
            ]
            owner = min(
                enclosing,
                key=lambda function: (function.end_lineno or function.lineno)
                - function.lineno,
            )
            owners.add((path.relative_to(repo).as_posix(), owner.name))

    assert owners == {
        (
            "jasper/active_speaker/runtime_contract.py",
            "safe_graph_for_current_topology",
        )
    }


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

from tests._armed_transport import arm_ring_transport
from tests.test_active_speaker_commission_load import _block
from tests.active_speaker_fixtures import (
    mono_output_topology as _topology,
)
from tests.test_active_speaker_startup_load import (
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
    # #2285 P2: this box is on the ring, so the load gate's liveness conjuncts
    # have to answer. `resolve_output_layout` case 2 now names the ring
    # unconditionally, and Wave 3's `commissioning_transport_armed` gate reads
    # fan-in's coupling and outputd's ACTIVE marker FRESH and fails SAFE — so a
    # harness declaring neither reads `loopback` with the marker false and every
    # commission-load call through this env blocks. See `tests/_armed_transport.py`.
    arm_ring_transport(monkeypatch)
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


@pytest.mark.parametrize(
    "explicit_staged_metadata",
    [True, False],
    ids=["explicit", "default"],
)
def test_runtime_safe_graph_cli_writes_staged_config_for_active_topology(
    tmp_path: Path,
    capsys,
    monkeypatch,
    explicit_staged_metadata: bool,
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

    argv = [
        "runtime-safe-graph",
        "--topology",
        str(topology_path),
        "--statefile",
        str(statefile),
        "--flat-config",
        str(flat),
    ]
    if explicit_staged_metadata:
        argv.extend(["--staged-metadata", str(metadata)])
    else:
        monkeypatch.setenv(
            "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
            str(metadata),
        )
    argv.extend(["--write-statefile", "--json"])

    code = main(argv)

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


def _parked_config_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point the parked graph's generated-config dir at tmp_path.

    ``parked_muted_config_path`` resolves it through
    ``staging.DEFAULT_CAMILLA_CONFIG_DIR`` (one spelling of the dir), so patching
    that constant relocates the whole parked path without a test-only flag.
    """
    from jasper.active_speaker import staging as staging_mod

    monkeypatch.setattr(staging_mod, "DEFAULT_CAMILLA_CONFIG_DIR", tmp_path)
    return tmp_path / "active_speaker_parked.yml"


def test_runtime_safe_graph_cli_parks_and_exits_success(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    # #2135: the install step invokes this CLI and fails the deploy on a nonzero
    # exit. A parked box must exit 0 so the deploy completes and the manifest
    # advances, and the transcript must print the two exits, not a blocker wall.
    from jasper.active_speaker.runtime_contract import PARKED_MUTED_EXITS
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _flat_yaml,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text("{}\n", encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {flat}\n", encoding="utf-8")
    parked = _parked_config_dir(monkeypatch, tmp_path)

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
    ])

    printed = capsys.readouterr().out
    assert code == 0
    assert "Runtime graph decision: parked_muted" in printed
    assert f"next: {PARKED_MUTED_EXITS}" in printed
    assert "[blocker]" not in printed
    assert parked.exists()
    assert f"config_path: {parked}" in statefile.read_text(encoding="utf-8")


def test_runtime_safe_graph_cli_parks_a_blocker_bearing_draft_and_prints_it(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    # #2145: a roleful topology carrying a topology-level blocker used to exit 1
    # here and abort every deploy, even though the parked graph it was refusing
    # is structurally silent. It now parks and exits 0 — but LOUDLY: the CLI
    # prints only `SafeGraphDecision.issues`, so this asserts the blocker
    # actually reaches the install transcript rather than vanishing with the
    # refusal that used to carry it.
    from dataclasses import replace

    from jasper.active_speaker.runtime_contract import PARKED_MUTED_EXITS
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _flat_yaml,
    )

    topology = _active_topology("mono", "active_2_way")
    draft = replace(
        topology,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    replace(channel, physical_output_index=None)
                    if channel.role == "tweeter"
                    else channel
                    for channel in group.channels
                ),
            )
            for group in topology.speaker_groups
        ),
    )
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(draft, path=topology_path)
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text("{}\n", encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {flat}\n", encoding="utf-8")
    parked = _parked_config_dir(monkeypatch, tmp_path)

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
    ])

    printed = capsys.readouterr().out
    # The deploy proceeds...
    assert code == 0
    assert "Runtime graph decision: parked_muted" in printed
    assert f"next: {PARKED_MUTED_EXITS}" in printed
    assert parked.exists()
    assert f"config_path: {parked}" in statefile.read_text(encoding="utf-8")
    # ...and the blocker is still on screen, named, exactly once.
    assert "[blocker] physical_output_unassigned:" in printed
    assert printed.count("physical_output_unassigned") == 1


def test_runtime_safe_graph_cli_names_capability_aware_exits(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    """The CLI must resolve the parked exits through the OWNED helper.

    On a DAC that declares no active outputd lane, "finish crossover preview"
    can never succeed, so `parked_muted_exits` drops it. The CLI used to print
    the bare `PARKED_MUTED_EXITS` constant, which offers that impossible action
    anyway — and because the constant and the helper agree on every ORDINARY
    DAC, only a passive-only profile can tell the two apart. That is what this
    fixture is for; without it a revert to the constant passes silently.
    """
    from jasper.active_speaker.runtime_contract import (
        PARKED_MUTED_EXITS,
        parked_muted_exits,
    )
    from jasper.output_topology import (
        OUTPUT_TOPOLOGY_KIND,
        OutputTopology,
        save_output_topology,
    )
    from tests.active_speaker_fixtures import register_passive_only_dac
    from tests.test_active_speaker_runtime_contract import _flat_yaml

    profile = register_passive_only_dac(monkeypatch)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench speaker",
        "status": "draft",
        "hardware": {
            "device_id": profile.id,
            "device_label": profile.label,
            "physical_output_count": profile.physical_output_count,
        },
        "speaker_groups": [{
            "id": "mono",
            "label": "Mono",
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
                    "protection_status": "present",
                },
            ],
        }],
        "routing": {"mono_group_id": "mono"},
    })
    # The fixture is only meaningful if the helper and the constant DISAGREE.
    assert parked_muted_exits(topology) != PARKED_MUTED_EXITS

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text("{}\n", encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {flat}\n", encoding="utf-8")
    _parked_config_dir(monkeypatch, tmp_path)

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
    ])

    printed = capsys.readouterr().out
    assert code == 0
    assert f"next: {parked_muted_exits(topology)}" in printed
    assert profile.label in printed


def test_runtime_safe_graph_cli_still_fails_on_an_unsafe_staged_graph(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    # The other half of the matrix: a staged graph that EXISTS but fails its
    # safety proof keeps exiting nonzero with its blockers. Parking is only for
    # "no staged graph at all".
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
    staged.write_text(_active_yaml("mono", 2, {1}), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(
        json.dumps(_staged_metadata(topology, staged)),
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {flat}\n", encoding="utf-8")
    parked = _parked_config_dir(monkeypatch, tmp_path)

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
    ])

    printed = capsys.readouterr().out
    assert code == 1
    assert "Runtime graph decision: blocked" in printed
    assert "[blocker]" in printed
    assert not parked.exists()
    assert f"config_path: {flat}" in statefile.read_text(encoding="utf-8")


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


def test_runtime_safe_graph_summary_names_hard_muted_outputs(capsys) -> None:
    """The operator line, pinned.

    It resolved the "a deliberately-silenced physical output has no operator
    trail" should-fix, and until this test existed it could be deleted with the
    whole suite green. Install's transcript is the only place a household or
    operator ever sees that a DAC output was intentionally muted; without the
    line a silent output reads as a fault later.
    """
    from jasper.cli.active_speaker import _print_runtime_safe_graph_summary

    payload = {
        "status": "preserve_current",
        "reason": "current CamillaDSP graph is legal for saved topology",
        "topology_contract": {
            "classification": "normal_mono_full_range",
            "requires_roleful_graph": False,
        },
        "current_graph": {
            "classification": "flat_full_range",
            "allowed": True,
            "config_path": "/etc/camilladsp/outputd-cutover.yml",
            "details": {"hard_muted_outputs": [1]},
        },
        "issues": [],
    }

    _print_runtime_safe_graph_summary(payload, wrote_statefile=False)
    printed = capsys.readouterr().out

    assert "current hard-muted outputs: 1 (not assigned by the saved topology)" in printed


def test_runtime_safe_graph_summary_is_silent_when_nothing_is_muted(capsys) -> None:
    """The ordinary stereo box's transcript is unchanged."""
    from jasper.cli.active_speaker import _print_runtime_safe_graph_summary

    payload = {
        "status": "select_flat",
        "reason": "saved topology has no roleful/protected outputs",
        "topology_contract": {
            "classification": "normal_stereo_full_range",
            "requires_roleful_graph": False,
        },
        "fallback_graph": {
            "classification": "flat_full_range",
            "allowed": True,
            "config_path": "/etc/camilladsp/outputd-cutover.yml",
            "details": {"hard_muted_outputs": []},
        },
        "issues": [],
    }

    _print_runtime_safe_graph_summary(payload, wrote_statefile=True)

    assert "hard-muted" not in capsys.readouterr().out


# --- #2285: baseline-reemit's own text must not teach a rejected endpoint ----


def _reemit_texts() -> dict[str, object]:
    """``baseline-reemit``'s two operator-facing strings, off the real parser.

    They live in different places, which is part of why they drifted apart:
    ``help=`` is stored on the PARENT's pseudo-action (it renders in the
    top-level listing) while ``description=`` is stored on the SUBPARSER (it
    renders on ``baseline-reemit --help``). Both are read here so neither can
    be trued up alone again.
    """

    from jasper.cli.active_speaker import build_parser

    for action in build_parser()._subparsers._group_actions:  # noqa: SLF001
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict) or "baseline-reemit" not in choices:
            continue
        sub = choices["baseline-reemit"]
        help_text = next(
            (
                pseudo.help or ""
                for pseudo in action._choices_actions  # noqa: SLF001
                if pseudo.dest == "baseline-reemit"
            ),
            "",
        )
        return {"help": help_text, "description": sub.description or "", "_sub": sub}
    raise AssertionError("baseline-reemit subparser not found")


def test_baseline_reemit_text_never_names_an_endpoint_argparse_rejects() -> None:
    """`help=` AND `description=` may only name endpoints the parser accepts.

    HOW THIS DRIFTED, which is why the guard covers both strings rather than
    the one that was wrong: `--endpoint` has `choices=("ring",)`, so argparse
    hard-errors anything else. The rollback direction (`aloop`) was retired
    with the snd-aloop ACTIVE lane, and the `help=` string was trued up while
    the `description=` beside it was missed — and `description=` is the one an
    operator running `baseline-reemit --help` actually reads.

    Derived from the parser's own `choices` rather than a hardcoded list, so
    adding a legal endpoint later does not have to touch this test; and driven
    off both attributes, because fixing one and not the other is the exact
    failure being pinned.
    """
    texts = _reemit_texts()
    endpoint = next(
        action for action in texts["_sub"]._actions  # noqa: SLF001
        if "--endpoint" in getattr(action, "option_strings", ())
    )
    legal = set(endpoint.choices or ())
    assert legal, "--endpoint must constrain its choices"

    # `aloop` names the retired rollback direction. It is the value that
    # actually drifted; `rejected` is derived against the live choices so this
    # test stops firing on its own if the endpoint is ever readmitted.
    rejected = {"aloop"} - legal
    assert rejected, "aloop is legal again — retire this guard"
    for attribute in ("help", "description"):
        text = str(texts[attribute])
        assert text, f"baseline-reemit has no {attribute}"
        for value in rejected:
            assert f"--endpoint {value}" not in text, (
                f"{attribute}= names `--endpoint {value}`, which argparse "
                f"rejects (choices={sorted(legal)})"
            )

    # NON-VACUITY: the surviving choice IS named in both strings, so a guard
    # that passed because both were empty or endpoint-free would fail here.
    for attribute in ("help", "description"):
        text = str(texts[attribute])
        assert any(f"--endpoint {value}" in text for value in legal), attribute
