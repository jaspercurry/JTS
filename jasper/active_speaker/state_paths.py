# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Default on-disk locations of the active-speaker state files.

A leaf: stdlib only, and it must stay that way. Its point is that a caller
wanting one path — the boot classifier, a CLI, a doctor check — resolves it
without importing the module that reads and writes the file.
"""

from __future__ import annotations

import os
from pathlib import Path

BASELINE_PROFILE_STATE_ENV = "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE"
DEFAULT_BASELINE_PROFILE_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_baseline_profile.json"
)
STARTUP_LOAD_STATE_ENV = "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE"
DEFAULT_STARTUP_LOAD_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_startup_load.json"
)
COMMISSION_LOAD_STATE_ENV = "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE"
DEFAULT_COMMISSION_LOAD_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_commission_load.json"
)


def _resolved(path: str | Path | None, env: str, default: Path) -> Path:
    return Path(path or os.environ.get(env) or default)


def baseline_profile_state_path(path: str | Path | None = None) -> Path:
    return _resolved(
        path, BASELINE_PROFILE_STATE_ENV, DEFAULT_BASELINE_PROFILE_STATE_PATH
    )


def startup_load_state_path(path: str | Path | None = None) -> Path:
    return _resolved(
        path, STARTUP_LOAD_STATE_ENV, DEFAULT_STARTUP_LOAD_STATE_PATH
    )


def commission_load_state_path(path: str | Path | None = None) -> Path:
    return _resolved(
        path, COMMISSION_LOAD_STATE_ENV, DEFAULT_COMMISSION_LOAD_STATE_PATH
    )
