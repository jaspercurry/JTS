# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Import-light capture-relay configuration parsing.

The control-plane state aggregator imports this module on its hot path, so it
must stay independent of the capture relay's audio and crypto runtime.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

ENV_RELAY_BASE = "JASPER_CAPTURE_RELAY_BASE"
ENV_RELAY_REGISTRATION_TOKEN = "JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN"
DISABLED_RELAY_BASE_VALUES = frozenset(
    {"0", "false", "off", "disable", "disabled", "none"}
)


def relay_base_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured relay origin, or ``None`` when disabled."""

    source = env if env is not None else os.environ
    base = (source.get(ENV_RELAY_BASE) or "").strip().rstrip("/")
    if base.lower() in DISABLED_RELAY_BASE_VALUES:
        return None
    return base or None


def relay_registration_token_from_env(
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the optional Pi-side registration secret."""

    source = env if env is not None else os.environ
    token = (source.get(ENV_RELAY_REGISTRATION_TOKEN) or "").strip()
    return token or None


def relay_config_from_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the network-free relay snapshot used by management surfaces."""

    base = relay_base_from_env(env)
    token = relay_registration_token_from_env(env)
    return {
        "configured": base is not None,
        "relay_base": base,
        "registration_secret_configured": token is not None,
    }
