# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Volume coordinator and transport-dispatch helpers for jasper-control."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..platform import wire
from ..platform.uds import voice_socket_command
from ..spotify_oauth import resolved_spotify_redirect_uri
from ..volume_persistence import (
    VolumePersistence,
    configured_path as volume_state_path,
)
from ..volume_state import VolumeState

# Every `# lazy: import cost` below defers for one reason: jasper-control is
# resident, so the coordinator/actuator graph (~16 modules, ~1.5 MB) must stay
# off the resident set of a box that never reaches those endpoints.

logger = logging.getLogger(__name__)

_SPOTIFY_EMPTY_ROUTER_CACHE_TTL_SEC = 30.0


@dataclass
class _SpotifyEmptyRouterCache:
    fingerprint: tuple
    expires_at: float
    reason: str


_spotify_empty_router_cache: _SpotifyEmptyRouterCache | None = None
_spotify_empty_router_cache_lock = threading.Lock()


def read_volume_state() -> "VolumeState":
    """Read the canonical volume projection without constructing actuators.

    GET /volume is polled by the visible landing page. Its read path therefore
    stays persistence-only: no Camilla socket, renderer probe, Spotify account
    registry, or OAuth client construction.
    """
    persistence = VolumePersistence(volume_state_path())
    return VolumeState.from_record(persistence.load())


def _spotify_account_cache_fingerprint(registry) -> tuple:
    entries = []
    for account in registry.accounts:
        cache_path = account.cache_path or ""
        try:
            st = os.stat(cache_path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = (-1, -1)
        entries.append((account.name, cache_path, stamp))
    return tuple(entries)


def _build_spotify_router_or_none():
    """Build a multi-account Spotify router for accessory-driven volume.
    Returns None if SPOTIFY_CLIENT_ID isn't set or no accounts have
    been authorized — _set_spotify in the coordinator treats None as
    "skip Spotify dispatch", logging a no-op."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        return None
    try:
        # lazy: import cost — see module header
        from ..accounts import legacy_cache_path, registry_path
        from ..spotify_router import build_router, load_registry

        accounts_path = registry_path()
        cache_path = legacy_cache_path()
        redirect_uri = resolved_spotify_redirect_uri()
        registry = load_registry(accounts_path, cache_path)
        fingerprint = (
            client_id,
            redirect_uri,
            accounts_path,
            cache_path,
            registry.default_name,
            _spotify_account_cache_fingerprint(registry),
        )
        global _spotify_empty_router_cache
        now = time.monotonic()
        with _spotify_empty_router_cache_lock:
            cached = _spotify_empty_router_cache
        if (
            cached is not None
            and cached.fingerprint == fingerprint
            and now < cached.expires_at
        ):
            logger.debug(
                "control daemon spotify router empty build suppressed for %.1fs "
                "(%s)",
                cached.expires_at - now,
                cached.reason,
            )
            return None
        router = build_router(
            client_id=client_id, redirect_uri=redirect_uri, registry=registry,
        )
        if not router.clients:
            reason = ",".join(sorted({s.state for s in router.statuses}))
            reason = reason or "no_accounts"
            with _spotify_empty_router_cache_lock:
                _spotify_empty_router_cache = _SpotifyEmptyRouterCache(
                    fingerprint=fingerprint,
                    expires_at=now + _SPOTIFY_EMPTY_ROUTER_CACHE_TTL_SEC,
                    reason=reason,
                )
            return None
        with _spotify_empty_router_cache_lock:
            _spotify_empty_router_cache = None
        return router
    except Exception as e:  # noqa: BLE001
        logger.debug("control daemon spotify router build failed: %s", e)
        return None


async def with_coordinator(
    op: Callable[[Any], Any],
    *,
    camilla_host: str,
    camilla_port: int,
    duck_active_probe: Optional[Callable[[], Awaitable[Optional[bool]]]] = None,
) -> Any:
    """Build a VolumeCoordinator for one operation, run `op(coord)`, dispose.

    Per-request like `dispatch_transport`, so this stdlib HTTP server never
    holds a long-lived asyncio loop. `op` is an async callable taking the live
    coordinator and returning the request's result.

    `duck_active_probe` is forwarded into the coordinator: when set, the
    coordinator defers its camilla write iff the probe returns True. See
    `make_duck_active_probe` for the wire details."""
    # lazy: import cost — see module header
    from .. import librespot_state
    from ..camilla import CamillaController
    from ..renderer import RendererClient
    from ..volume_coordinator import build_volume_coordinator

    coord = build_volume_coordinator(
        camilla=CamillaController(host=camilla_host, port=camilla_port),
        backend=RendererClient(
            librespot_state_path=librespot_state.configured_path(),
        ),
        # Web API because librespot 0.8.0 has no local HTTP control; None
        # (no client id / no authorized account) makes Spotify a no-op.
        spotify_router=_build_spotify_router_or_none(),
        duck_active_probe=duck_active_probe,
    )
    # Nothing here is closable: RendererClient is a stateless probe wrapper
    # and CamillaController's websocket reconnects on next use.
    return await op(coord)


def make_duck_active_probe(
    voice_socket_path: str,
    *,
    voice_socket_command: Callable[..., Awaitable[dict]] = voice_socket_command,
) -> Callable[[], Awaitable[Optional[bool]]]:
    """Build the cross-daemon Camilla-ownership probe consumed by
    VolumeCoordinator._set_camilla in the per-request coordinators here.

    The probe asks jasper-voice over UDS whether a duck holder is
    currently holding camilla below the canonical listening_level
    target. True → skip the accessory's camilla write; listening_level
    still persists, so the user's intent is not lost. False → write
    camilla normally.
    None → unknown (UDS unreachable / voice wedged / response
    malformed); the coordinator treats this as fail-open and writes
    camilla — the accessory must never silently stop working because of
    an inter-daemon problem.

    Tight 1 s timeout: STATUS is a synchronous attribute read in
    voice_daemon (no I/O). If it doesn't return in 1 s the daemon
    is wedged and we'd rather fail-open than block accessory input."""
    async def probe() -> Optional[bool]:
        try:
            response = await voice_socket_command(
                voice_socket_path, wire.STATUS, timeout=1.0,
            )
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
        ):
            return None
        camilla_locked = response.get("camilla_volume_locked")
        if isinstance(camilla_locked, bool):
            return camilla_locked
        # Rolling-upgrade compatibility with a voice daemon that predates the
        # explicit lock field. Its only duck transport owned Camilla.
        duck_active = response.get("duck_active")
        if isinstance(duck_active, bool):
            return duck_active
        # Older jasper-voice without the field, or unexpected type —
        # fail-open. Same effect as voice unreachable.
        return None
    return probe


async def dispatch_transport(
    action: str,
    *,
    spotify_router_factory: Callable[[], Any] = _build_spotify_router_or_none,
) -> dict:
    """Dispatch one transport action against clients built in this loop.

    Rebuilt per request because httpx's AsyncClient is loop-bound; ~50 ms, and
    remote presses are rare. `action` is "toggle", "next" or "previous"."""
    # lazy: import cost — see module header
    from .. import librespot_state
    from ..renderer import RendererClient
    from ..tools.transport import make_transport_dispatcher

    renderer = RendererClient(
        librespot_state_path=librespot_state.configured_path(),
    )

    dispatch = make_transport_dispatcher(renderer, spotify_router_factory())
    return await dispatch(action)
