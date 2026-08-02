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
publicly-trusted HTTPS origin. Set the base to one of the explicit disable
sentinels below to use the existing on-Pi same-origin capture; the doctor check
then skips cleanly rather than warning.
"""
from __future__ import annotations

import urllib.error
import urllib.request

from jasper.capture_relay.client import RELAY_USER_AGENT
from jasper.capture_relay_config import (
    DISABLED_RELAY_BASE_VALUES as DISABLED_RELAY_BASE_VALUES,
    ENV_RELAY_BASE as ENV_RELAY_BASE,
    ENV_RELAY_REGISTRATION_TOKEN as ENV_RELAY_REGISTRATION_TOKEN,
    relay_base_from_env as relay_base_from_env,
    relay_config_from_env as relay_config_from_env,
    relay_registration_token_from_env as relay_registration_token_from_env,
)


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


def probe_relay_plan_capacity(
    base_url: str, *, timeout: float = 2.0
) -> tuple[int | None, str]:
    """The deployed relay's advertised capture-plan ceiling.

    Returns ``(ceiling, human_detail)``; ``ceiling`` is ``None`` when the relay
    answers no usable capability document — including the pre-capacity Worker,
    which serves no ``/capabilities`` endpoint at all and whose ABSENCE is the
    only honest version signal available (see
    ``capture_relay.spec.LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS``).

    Why the doctor asks at all: the crossover-v2 cloud session emits a plan
    larger than that legacy ceiling, so a stale Worker refuses it — correctly
    and before any tone plays (``session._assert_relay_plan_capacity``), but
    only at the moment a household taps Start. One extra bounded GET during a
    diagnostic run turns that into something an operator can find and fix
    beforehand.

    Same posture as :func:`probe_relay_health`: outbound HTTPS only, bounded,
    on-demand — never on the ``/state`` path.
    """
    from jasper.capture_relay.client import RelayClient
    from jasper.capture_relay.session import advertised_plan_ceiling

    try:
        probe = RelayClient(base_url, timeout=timeout).capabilities()
    except (OSError, ValueError, RuntimeError) as exc:
        return None, f"capability probe failed: {exc}"
    if not probe.served:
        return None, "serves no /capabilities endpoint (pre-capacity relay)"
    ceiling = advertised_plan_ceiling(probe.document)
    if ceiling is None:
        return None, "/capabilities carries no usable capture-plan ceiling"
    return ceiling, f"capture-plan ceiling {ceiling}"
