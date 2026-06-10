"""Tests for scripts/_run_wake_training_phase0.py."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "_run_wake_training_phase0.py"
)
_spec = importlib.util.spec_from_file_location("run_wake_training_phase0", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
phase0 = importlib.util.module_from_spec(_spec)
sys.modules["run_wake_training_phase0"] = phase0
_spec.loader.exec_module(phase0)


def _parse(args: list[str]):
    return phase0._build_parser().parse_args(args)


class FakeRunner:
    def __init__(self, *, fail_step: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.fail_step = fail_step

    def __call__(self, command: list[str], cwd: Path):
        del cwd
        self.commands.append(command)
        script = Path(command[1]).name
        returncode = 1 if script == self.fail_step else 0
        if returncode == 0:
            self._write_outputs(script, command)
        return phase0.CommandResult(
            command=command,
            returncode=returncode,
            duration_sec=0.01,
            stdout=f"{script} stdout",
            stderr=f"{script} stderr" if returncode else "",
        )

    def _write_outputs(self, script: str, command: list[str]) -> None:
        if script == "export-wake-corpus-bundle.sh":
            out = Path(command[3])
            out.mkdir(parents=True, exist_ok=True)
            (out / "bundle.json").write_text(json.dumps({
                "schema_version": 1,
                "manifest_row_count": 2,
            }) + "\n")
            (out / "manifest.jsonl").write_text("")
            return
        if script == "build-wake-feature-bank.sh":
            out = Path(command[3])
            out.mkdir(parents=True, exist_ok=True)
            (out / "feature_bank.json").write_text(json.dumps({
                "schema_version": 1,
                "artifacts": {"features": {
                    "train": "positive_features_train.npy",
                    "eval": "positive_features_eval.npy",
                }},
            }) + "\n")
            return
        if script == "build-wake-negative-feature-bank.sh":
            out = Path(command[3])
            out.mkdir(parents=True, exist_ok=True)
            (out / "negative_features_train.npy").write_bytes(b"train")
            (out / "negative_features_eval.npy").write_bytes(b"eval")
            (out / "negative_feature_bank.json").write_text(json.dumps({
                "schema_version": 1,
                "kind": "negative_feature_bank",
                "artifacts": {"features": {
                    "train": "negative_features_train.npy",
                    "eval": "negative_features_eval.npy",
                }},
            }) + "\n")
            return
        if script == "prepare-wake-training-workdir.sh":
            out = Path(command[3])
            out.mkdir(parents=True, exist_ok=True)
            (out / "training_workdir.json").write_text(json.dumps({
                "schema_version": 1,
                "artifacts": {"features": {
                    "train": "feature_data/positive_features_train.npy",
                    "test": "feature_data/positive_features_test.npy",
                }},
            }) + "\n")
            return
        if script == "prepare-wake-livekit-smoke.sh":
            out = Path(command[3])
            out.mkdir(parents=True, exist_ok=True)
            (out / "livekit_smoke.json").write_text(json.dumps({
                "schema_version": 1,
                "run_livekit": {"requested": "--run-livekit" in command},
            }) + "\n")


def test_phase0_runner_uses_real_negative_features(tmp_path: Path) -> None:
    out = tmp_path / "run"
    fake = FakeRunner()
    args = _parse([
        str(out),
        "--positive-corpus-dir", str(tmp_path / "positives"),
        "--negative-corpus-dir", str(tmp_path / "negatives"),
        "--positive-leg", "chip_aec_150",
        "--negative-label-kind", "hard_negative",
        "--model-name", "hey_jarvis_jts",
    ])

    summary = phase0._run_phase0(args, runner=fake)

    assert summary["status"] == "pass"
    assert summary["quality_evidence"]["real_negative_features"] is True
    assert [step["step"] for step in summary["steps"]] == [
        "export_positive_bundle",
        "build_positive_features",
        "export_negative_bundle",
        "build_negative_features",
        "prepare_training_workdir",
        "prepare_livekit_phase0",
    ]
    livekit_command = fake.commands[-1]
    assert "--negative-train-features" in livekit_command
    assert str(out / "negative-features" / "negative_features_train.npy") in livekit_command
    assert (out / "phase0_run.json").is_file()
    assert len((out / "command_log.jsonl").read_text().splitlines()) == 6


def test_phase0_runner_requires_negative_evidence_by_default(tmp_path: Path) -> None:
    out = tmp_path / "run"
    args = _parse([str(out), "--positive-corpus-dir", str(tmp_path / "positives")])

    try:
        phase0._run_phase0(args, runner=FakeRunner())
    except phase0.Phase0Error as e:
        assert "require --negative-corpus-dir or --negative-bundle-dir" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected missing negative evidence failure")
    manifest = json.loads((out / "phase0_run.json").read_text())
    assert manifest["status"] == "fail"
    assert manifest["steps"] == []


def test_phase0_runner_allows_placeholder_negative_smoke(tmp_path: Path) -> None:
    out = tmp_path / "run"
    fake = FakeRunner()
    args = _parse([
        str(out),
        "--positive-corpus-dir", str(tmp_path / "positives"),
        "--allow-placeholder-negatives",
    ])

    summary = phase0._run_phase0(args, runner=fake)

    assert summary["status"] == "pass"
    assert summary["quality_evidence"]["placeholder_negatives"] is True
    assert [Path(command[1]).name for command in fake.commands] == [
        "export-wake-corpus-bundle.sh",
        "build-wake-feature-bank.sh",
        "prepare-wake-training-workdir.sh",
        "prepare-wake-livekit-smoke.sh",
    ]
    livekit_command = fake.commands[-1]
    assert "--negative-train-features" not in livekit_command


def test_phase0_runner_can_use_existing_bundles(tmp_path: Path) -> None:
    positive_bundle = tmp_path / "positive-bundle"
    negative_bundle = tmp_path / "negative-bundle"
    positive_bundle.mkdir()
    negative_bundle.mkdir()
    (positive_bundle / "bundle.json").write_text('{"schema_version": 1}\n')
    (negative_bundle / "bundle.json").write_text('{"schema_version": 1}\n')
    fake = FakeRunner()
    args = _parse([
        str(tmp_path / "run"),
        "--positive-bundle-dir", str(positive_bundle),
        "--negative-bundle-dir", str(negative_bundle),
    ])

    summary = phase0._run_phase0(args, runner=fake)

    assert summary["status"] == "pass"
    assert "export-wake-corpus-bundle.sh" not in {
        Path(command[1]).name for command in fake.commands
    }


def test_phase0_runner_records_failed_step(tmp_path: Path) -> None:
    out = tmp_path / "run"
    args = _parse([
        str(out),
        "--positive-corpus-dir", str(tmp_path / "positives"),
        "--negative-corpus-dir", str(tmp_path / "negatives"),
    ])

    try:
        phase0._run_phase0(
            args,
            runner=FakeRunner(fail_step="build-wake-negative-feature-bank.sh"),
        )
    except phase0.Phase0Error as e:
        assert "build_negative_features failed" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected failed command")
    manifest = json.loads((out / "phase0_run.json").read_text())
    assert manifest["status"] == "fail"
    assert manifest["steps"][-1]["step"] == "build_negative_features"
    assert manifest["steps"][-1]["returncode"] == 1


def test_force_remove_guard_allows_tool_owned_custom_output(tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "phase0_run.json").write_text(json.dumps({
        "schema_version": phase0.SCHEMA_VERSION,
        "tool": "run-wake-training-phase0",
    }) + "\n")

    assert phase0._safe_to_remove_output(custom)
    assert not phase0._safe_to_remove_output(tmp_path)
    assert not phase0._safe_to_remove_output(Path.cwd())
