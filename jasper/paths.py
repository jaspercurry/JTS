# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared paths and state-file resolution."""
from __future__ import annotations

import os
from pathlib import Path


CANONICAL_CAMILLA_CONFIG_DIR = Path("/var/lib/camilladsp/configs")
# camilla#1 (outputd) and camilla#2 (crossover); the units' --statefile flags
# in deploy/systemd/ spell the same paths.
DEFAULT_CAMILLA_STATEFILE = Path("/var/lib/camilladsp/outputd-statefile.yml")
DEFAULT_CAMILLA2_STATEFILE = Path("/var/lib/camilladsp/crossover-statefile.yml")
OUTPUT_HARDWARE_STATE_PATH = "/run/jasper-output-hardware/output_hardware.json"
OUTPUT_TOPOLOGY_PATH = "/var/lib/jasper/output_topology.json"


def resolve_state_path(
    explicit: str | os.PathLike[str] | None,
    env_name: str | None,
    default: str | os.PathLike[str],
) -> Path:
    """Resolve a state-file path: ``explicit``, else ``$env_name``, else ``default``.

    ``env_name=None`` skips the environment lookup. The environment is read
    at call time — a long-lived daemon must see a later change to a
    wizard-owned override (AGENTS.md).
    """
    env_value = os.environ.get(env_name) if env_name else None
    return Path(explicit or env_value or default)


def camilla_statefile(path: str | os.PathLike[str] | None = None) -> Path:
    return resolve_state_path(path, "JASPER_CAMILLA_STATEFILE", DEFAULT_CAMILLA_STATEFILE)


def crossover_statefile(path: str | os.PathLike[str] | None = None) -> Path:
    return resolve_state_path(path, "JASPER_CAMILLA2_STATEFILE", DEFAULT_CAMILLA2_STATEFILE)
