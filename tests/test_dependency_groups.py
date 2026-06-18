from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SUPPLY_CHAIN_DOC = ROOT / "docs" / "HANDOFF-supply-chain.md"


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


def test_ci_pytest_gate_is_parallel_and_hardware_free() -> None:
    """Keep the required Python merge gate fast without running paid voice-eval."""

    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "pytest -q --tb=short --ignore=tests/voice_eval -n 4" in workflow


def test_python_resolution_artifacts_are_committed_and_documented() -> None:
    """Local dev and Pi deploys intentionally use different Python
    resolution artifacts; keep both present and keep the canonical doc
    from drifting back to the old "choose later" language."""

    assert (ROOT / "uv.lock").is_file()
    assert (ROOT / "deploy" / "constraints-pi.txt").is_file()

    doc = SUPPLY_CHAIN_DOC.read_text(encoding="utf-8")
    assert "uv.lock" in doc
    assert "deploy/constraints-pi.txt" in doc
    assert "choose one shared artifact" not in doc
    assert "does not currently commit a shared Python lock artifact" not in doc


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


def test_documented_venv_build_commands_install_test_runtime_extras() -> None:
    """Every contributor-facing "build your test venv" instruction must install
    the runtime extras the hardware-free suite imports (numpy, httpx, scipy, ...).

    A bare `uv sync` (or `pip install -e '.[dev]'`) installs only the dev tools,
    so pytest dies with dozens of ModuleNotFoundError on a clean checkout. uv
    0.11 has no `[tool.uv] default-extras` knob to fix that from config, so the
    docs and help spell the extras out explicitly. Pin BOTH surfaces — the
    CONTRIBUTING.md quick start and the conftest wrong-Python rebuild hint — so
    the front door can't silently re-break (the 2026-06 OSS due-diligence
    finding, which regressed once because only one surface was fixed).
    """

    # Token-based, not an exact-substring match, so a future reformat of the
    # command (line wraps, flag reordering) doesn't false-fail as long as it
    # still invokes `uv sync` with both extras. The behavioural end-to-end check
    # (run the documented command and collect) belongs in CI; it's omitted here
    # only to avoid editing a workflow file from a non-`workflow`-scoped token.
    surfaces = {
        "CONTRIBUTING.md": (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"),
        "tests/conftest.py": (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"),
    }
    for name, text in surfaces.items():
        assert "uv sync" in text, f"{name} should document `uv sync`"
        assert "--extra full" in text, f"{name} `uv sync` must include `--extra full`"
        assert "--extra streambox" in text, (
            f"{name} `uv sync` must include `--extra streambox`"
        )

    # The conftest pip fallback must also pull the extras (`.[full,dev]`, not `.[dev]`).
    assert "'.[full,dev]'" in surfaces["tests/conftest.py"]
