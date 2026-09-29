# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Volume coordinator and transport-dispatch helpers for jasper-control."""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from jasper.playback_state import librespot_state
from ..accounts import legacy_cache_path, registry_path
from ..camilla import CamillaController
from ..renderer import RendererClient
from ..spotify_oauth import resolved_spotify_redirect_uri
from jasper.audio_resources.volume_owner import volume_owner
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


def build_spotify_router_or_none():
    """Build a multi-account Spotify router for accessory-driven volume.
    Returns None if SPOTIFY_CLIENT_ID isn't set or no accounts have
    been authorized — volume_push_sources.push_spotify_volume treats None
    as "skip Spotify dispatch", logging a no-op."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        return None
    try:
        from ..spotify_router import build_router, load_registry  # lazy: import cost, see module header

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
) -> Any:
    """Build a VolumeCoordinator for one operation, run `op(coord)`, dispose.

    Per-request like `dispatch_transport`, so this stdlib HTTP server never
    holds a long-lived asyncio loop. `op` is an async callable taking the live
    coordinator and returning the request's result."""
    from ..volume_coordinator import build_volume_coordinator  # lazy: import cost, see module header

    coord = build_volume_coordinator(
        camilla=CamillaController(host=camilla_host, port=camilla_port),
        backend=RendererClient(
            librespot_state_path=librespot_state.configured_path(),
        ),
        # Web API because librespot 0.8.0 has no local HTTP control; None
        # (no client id / no authorized account) makes Spotify a no-op.
        spotify_router=build_spotify_router_or_none(),
        volume_owner=volume_owner(),
    )
    # Nothing here is closable: RendererClient is a stateless probe wrapper
    # and CamillaController's websocket reconnects on next use.
    return await op(coord)


async def dispatch_transport(
    action: str,
    *,
    spotify_router_factory: Callable[[], Any] = build_spotify_router_or_none,
) -> dict:
    """Dispatch one transport action against clients built in this loop.

    Rebuilt per request because httpx's AsyncClient is loop-bound; ~50 ms, and
    remote presses are rare. `action` is "toggle", "next" or "previous"."""
    from ..renderer import RendererClient  # lazy: test patch boundary (tests/test_control_server_volume.py)
    from ..tools.transport import make_transport_dispatcher  # lazy: import cost (rapidfuzz), see module header; test patch boundary (tests/test_control_server_volume.py)

    renderer = RendererClient(
        librespot_state_path=librespot_state.configured_path(),
    )

    dispatch = make_transport_dispatcher(renderer, spotify_router_factory())
    return await dispatch(action)
