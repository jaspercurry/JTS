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
from (the same value the future `correction_setup.py` adapter will pass to
`mint_session(relay_base=...)`). Until it is set, the speaker uses the existing
on-Pi same-origin capture; the doctor check skips cleanly rather than warning.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

ENV_RELAY_BASE = "JASPER_CAPTURE_RELAY_BASE"
DEFAULT_USER_AGENT = "JTS capture-relay/1"


def relay_base_from_env(env: dict[str, str] | None = None) -> str | None:
    """The configured relay origin (https://…), or None when unconfigured."""
    source = env if env is not None else os.environ
    base = (source.get(ENV_RELAY_BASE) or "").strip().rstrip("/")
    return base or None


def relay_config_from_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Fast, network-free config snapshot for `/state.capture_relay`."""
    base = relay_base_from_env(env)
    return {"configured": base is not None, "relay_base": base}


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
            url, method="GET", headers={"User-Agent": DEFAULT_USER_AGENT}
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
