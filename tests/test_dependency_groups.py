from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_dev_dependency_group_matches_dev_extra() -> None:
    """Keep `uv sync` and `pip install -e '.[dev]'` dev deps in lockstep."""

    data = _pyproject()

    assert data["dependency-groups"]["dev"] == (
        data["project"]["optional-dependencies"]["dev"]
    )
