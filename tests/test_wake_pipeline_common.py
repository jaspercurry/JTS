# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for wake-pipeline launchers and destructive-output checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts._wake_pipeline_common import is_safe_wake_pipeline_output


@pytest.mark.parametrize("script", [
    "export_wake_corpus_bundle", "prepare_wake_training_workdir",
    "prepare_wake_livekit_smoke", "run_wake_training_phase0",
])
@pytest.mark.parametrize("mode", ["file", "module", "shell"])
def test_launch_without_installed_package(tmp_path: Path, script: str, mode: str) -> None:
    repo = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if mode == "shell":
        python = tmp_path / "python"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
        python.chmod(0o755)
        env["PYTHON"] = str(python)
        command = ["bash", str(repo / "scripts" / f"{script.replace('_', '-')}.sh")]
    else:
        target = ["-m", f"scripts._{script}"] if mode == "module" else [
            str(repo / "scripts" / f"_{script}.py")
        ]
        command = [sys.executable, "-S", *target]
    result = subprocess.run(
        [*command, "--help"], cwd=repo if mode == "module" else tmp_path,
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def _matches_marker(data: Mapping[str, object]) -> bool:
    artifacts = data.get("artifacts")
    return (
        data.get("schema_version") == 1
        and data.get("tool") == "test-tool"
        and artifacts == {"summary": "owner.json"}
    )


def _write_marker(path: Path, *, output_dir: Path | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "owner.json").write_text(json.dumps({
        "schema_version": 1,
        "tool": "test-tool",
        "output_dir": str(output_dir or path),
        "artifacts": {"summary": "owner.json"},
    }))


def _safe(path: Path, *, owner: Path, protected: Path) -> bool:
    return is_safe_wake_pipeline_output(
        path,
        owned_root=owner,
        protected_paths=(protected,),
        marker_name="owner.json",
        marker_matches=_matches_marker,
    )


def test_owned_root_and_descendants_are_safe(tmp_path: Path) -> None:
    protected = tmp_path / "input"
    owner = protected / "tool-output"

    assert _safe(owner, owner=owner, protected=protected)
    assert _safe(owner / "partial-retry", owner=owner, protected=protected)


def test_root_home_cwd_protected_paths_and_protected_ancestors_are_rejected(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "input" / "source"
    owner = protected / "tool-output"

    for candidate in (Path("/"), Path.home(), Path.cwd(), protected, tmp_path / "input"):
        assert not _safe(candidate, owner=owner, protected=protected)


def test_custom_directory_requires_valid_self_bound_object_marker(tmp_path: Path) -> None:
    protected = tmp_path / "input"
    owner = protected / "tool-output"
    custom = tmp_path / "custom"
    _write_marker(custom)

    assert _safe(custom, owner=owner, protected=protected)

    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / "owner.json").write_bytes((custom / "owner.json").read_bytes())
    assert not _safe(copied, owner=owner, protected=protected)


def test_final_symlink_is_rejected_before_resolution(tmp_path: Path) -> None:
    protected = tmp_path / "input"
    owner = protected / "tool-output"
    custom = tmp_path / "custom"
    _write_marker(custom)
    alias = tmp_path / "alias"
    alias.symlink_to(custom, target_is_directory=True)

    assert not _safe(alias, owner=owner, protected=protected)


def test_marker_and_validator_errors_fail_closed(tmp_path: Path) -> None:
    protected = tmp_path / "input"
    owner = protected / "tool-output"
    custom = tmp_path / "custom"
    custom.mkdir()

    for invalid in ("[]", "{", '{"output_dir": 7}'):
        (custom / "owner.json").write_text(invalid)
        assert not _safe(custom, owner=owner, protected=protected)

    _write_marker(custom)

    def raises(_data):
        raise RuntimeError("validator failed")

    assert not is_safe_wake_pipeline_output(
        custom,
        owned_root=owner,
        protected_paths=(protected,),
        marker_name="owner.json",
        marker_matches=raises,
    )


def test_symlinked_marker_fails_closed(tmp_path: Path) -> None:
    protected = tmp_path / "input"
    owner = protected / "tool-output"
    custom = tmp_path / "custom"
    external = tmp_path / "external-marker.json"
    custom.mkdir()
    external.write_text(json.dumps({
        "schema_version": 1,
        "tool": "test-tool",
        "output_dir": str(custom),
        "artifacts": {"summary": "owner.json"},
    }))
    (custom / "owner.json").symlink_to(external)

    assert not _safe(custom, owner=owner, protected=protected)
