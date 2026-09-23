# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Default on-disk locations of the active-speaker state files and the
baseline CamillaDSP config.

A leaf: the stdlib and :mod:`jasper.paths` only, and it must stay that way.
Its point is that a caller wanting one path — the boot classifier, a CLI, a
doctor check — resolves it without importing the module that reads and
writes the file.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from jasper.paths import CANONICAL_CAMILLA_CONFIG_DIR

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
BASELINE_CONFIG_PATH_ENV = "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH"
DEFAULT_BASELINE_CONFIG_PATH = CANONICAL_CAMILLA_CONFIG_DIR / "active_speaker_baseline.yml"


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



def baseline_config_path(path: str | Path | None = None) -> Path:
    return _resolved(path, BASELINE_CONFIG_PATH_ENV, DEFAULT_BASELINE_CONFIG_PATH)


def config_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def baseline_candidate_config_path(text: str, path: str | Path | None = None) -> Path:
    """The content-addressed sibling of :func:`baseline_config_path` a graph is applied from."""
    target = baseline_config_path(path)
    sha256 = config_text_sha256(text)
    return target.with_name(f"{target.stem}_candidate_{sha256[:12]}{target.suffix}")
