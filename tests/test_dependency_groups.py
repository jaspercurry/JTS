from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_dev_dependency_group_matches_dev_extra() -> None:
    """Keep `uv sync` and `pip install -e '.[dev]'` dev deps in lockstep."""

    data = _pyproject()

    assert data["dependency-groups"]["dev"] == (
        data["project"]["optional-dependencies"]["dev"]
    )


def test_ci_installs_full_runtime_with_dev_extra() -> None:
    """The full pytest suite imports optional runtime packages."""

    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "pip install -e '.[full,dev]'" in workflow


def test_linux_only_c_extensions_have_platform_markers() -> None:
    """Keep macOS contributor installs from trying to build Linux-only wheels."""

    data = _pyproject()
    dependencies = list(data["project"]["dependencies"])
    for group in data["project"]["optional-dependencies"].values():
        dependencies.extend(group)
    expected = {
        "pyalsaaudio": "pyalsaaudio>=0.11; sys_platform == 'linux'",
        "evdev": "evdev>=1.7; sys_platform == 'linux'",
    }

    for package, requirement in expected.items():
        matches = [dep for dep in dependencies if dep.startswith(f"{package}>=")]
        assert matches == [requirement]
