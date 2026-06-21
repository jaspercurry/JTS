# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Helpers for assertions over deploy/install.sh plus sourced install libs."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "deploy" / "install.sh"
INSTALL_LIB_DIR = REPO / "deploy" / "lib" / "install"


def installer_shell_paths() -> list[Path]:
    """Return the root installer and every sourced deploy/lib/install lib."""
    return [INSTALL_SH, *sorted(INSTALL_LIB_DIR.glob("*.sh"))]


def installer_text() -> str:
    """Concatenate the install surface that can run during deploy."""
    return "\n".join(
        path.read_text(encoding="utf-8") for path in installer_shell_paths()
    )
