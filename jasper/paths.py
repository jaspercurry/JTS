# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared state-file paths and resolution."""
from __future__ import annotations

import os
from pathlib import Path


OUTPUT_HARDWARE_STATE_PATH = "/run/jasper-output-hardware/output_hardware.json"
OUTPUT_TOPOLOGY_PATH = "/var/lib/jasper/output_topology.json"


def resolve_state_path(
    explicit: str | os.PathLike[str] | None,
    env_name: str | None,
    default: str | os.PathLike[str],
) -> Path:
    """Resolve a state-file path: ``explicit``, else ``$env_name``, else ``default``.

    ``env_name=None`` skips the environment lookup entirely, for a caller
    with no override knob. The environment is read here, at call time, not
    cached at import — a long-lived daemon must see a later change to a
    wizard-owned override (AGENTS.md).
    """
    env_value = os.environ.get(env_name) if env_name else None
    return Path(explicit or env_value or default)
