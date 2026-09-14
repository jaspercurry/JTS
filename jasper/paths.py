# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared state-file path resolution.

Several platform modules persist one state file each and resolve its
location the same way: an explicit override, else a ``JASPER_*`` env
var, else a default. :func:`resolve_state_path` is that rule in one
place so it converges instead of drifting across callers (#4810).
"""
from __future__ import annotations

import os
from pathlib import Path


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
