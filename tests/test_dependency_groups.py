# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the dependency groups declared in ``pyproject.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_dev_dependency_group_matches_dev_extra() -> None:
    """Keep ``uv sync`` and ``pip install -e '.[dev]'`` in lockstep."""

    data = _pyproject()
    assert data["dependency-groups"]["dev"] == (
        data["project"]["optional-dependencies"]["dev"]
    )


def test_openwakeword_onnx_group_covers_ci_helper_deps() -> None:
    """Only openWakeWord itself is installed outside uv sync in CI."""

    assert _pyproject()["dependency-groups"]["openwakeword-onnx"] == [
        "requests",
        "tqdm",
        "scikit-learn>=1,<2",
    ]


def test_fast_landing_dependency_group_is_minimal() -> None:
    """The narrow landing lane needs YAML, not the full audio/voice extras."""

    assert _pyproject()["dependency-groups"]["fast-landing"] == ["pyyaml==6.0.3"]
