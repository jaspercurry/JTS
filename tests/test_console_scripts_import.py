# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _project_scripts() -> dict[str, str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["scripts"]


def test_project_console_scripts_import() -> None:
    """Every advertised console script must import and expose its callable."""

    for script_name, target in _project_scripts().items():
        module_name, separator, attribute_path = target.partition(":")
        assert separator, f"{script_name}: entry point must be module:attribute"

        module = importlib.import_module(module_name)
        value = module
        for part in attribute_path.split("."):
            assert hasattr(value, part), (
                f"{script_name}: {target} is missing attribute {part!r}"
            )
            value = getattr(value, part)
