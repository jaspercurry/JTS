# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Observability helpers for the phone-mic capture relay subsystem.

The relay transport itself is a per-measurement, ephemeral library (no resident
daemon), so the operator-facing surfaces report **configuration + reachability**,
not live session state:

  - `/state.capture_relay` (jasper-control) shows the configured relay origin
    *without* a network call, so a poll stays fast and never hammers the relay.
  - `jasper-doctor`'s `check_capture_relay` actively probes the relay's
    `/healthz` on demand (it can afford the round-trip) and confirms the AEAD
    decrypt dependency is importable.

Both read `JASPER_CAPTURE_RELAY_BASE` — the deploy-time relay origin the Pi pulls
from — plus the optional `JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN` registration
gate. Fresh installs seed the Jasper Tech public relay
(`https://relay.jasper.tech`) because phone microphone capture needs a
publicly-trusted HTTPS origin; an operator may still clear the base to use the
existing on-Pi same-origin capture, in which case the doctor check skips cleanly
rather than warning.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

from jasper.capture_relay.client import RELAY_USER_AGENT

ENV_RELAY_BASE = "JASPER_CAPTURE_RELAY_BASE"
ENV_RELAY_REGISTRATION_TOKEN = "JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN"


def relay_base_from_env(env: dict[str, str] | None = None) -> str | None:
    """The configured relay origin (https://…), or None when unconfigured."""
    source = env if env is not None else os.environ
    base = (source.get(ENV_RELAY_BASE) or "").strip().rstrip("/")
    return base or None


def relay_registration_token_from_env(env: dict[str, str] | None = None) -> str | None:
    """Optional Pi-side registration secret, or None when unconfigured."""
    source = env if env is not None else os.environ
    token = (source.get(ENV_RELAY_REGISTRATION_TOKEN) or "").strip()
    return token or None


def relay_config_from_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Fast, network-free config snapshot for `/state.capture_relay`."""
    base = relay_base_from_env(env)
    token = relay_registration_token_from_env(env)
    return {
        "configured": base is not None,
        "relay_base": base,
        "registration_secret_configured": token is not None,
    }


def probe_relay_health(base_url: str, *, timeout: float = 2.0) -> tuple[bool, str]:
    """Outbound GET ``<base>/healthz``. Returns ``(ok, human_detail)``.

    Outbound-HTTPS-only and bounded — mirrors the Pi-side client posture. Used by
    the doctor (on-demand), not by the hot `/state` path.
    """
    if not base_url.startswith("https://"):
        return False, f"relay base must be https://, got {base_url!r}"
    url = base_url.rstrip("/") + "/healthz"
    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": RELAY_USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(64).decode("utf-8", "replace").strip()
            if resp.status == 200:
                return True, f"reachable ({body or 'ok'})"
            return False, f"unexpected status {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable: {exc}"
