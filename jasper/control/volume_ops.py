# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Volume coordinator and transport-dispatch helpers for jasper-control."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .uds import _voice_socket_command
from ..spotify_oauth import (
    SPOTIFY_OAUTH_CALLBACK_BASE as _SHARED_SPOTIFY_OAUTH_CALLBACK_BASE,
    default_spotify_redirect_uri,
)
from ..volume_curve import (
    DEFAULT_VOLUME_FLOOR_DB,
    VOLUME_CEILING_DB,
    db_to_percent,
    delta_db_to_delta_percent,
    percent_to_db,
)

logger = logging.getLogger(__name__)

# Back-compat names for legacy clients/tests. The effective floor can be
# calibrated in /sound/; these constants are the shipped default.
VOLUME_MIN_DB = DEFAULT_VOLUME_FLOOR_DB
VOLUME_MAX_DB = VOLUME_CEILING_DB
# Compatibility re-export retained for server.py and older importers.
SPOTIFY_OAUTH_CALLBACK_BASE = _SHARED_SPOTIFY_OAUTH_CALLBACK_BASE
_SPOTIFY_EMPTY_ROUTER_CACHE_TTL_SEC = 30.0


@dataclass
class _SpotifyEmptyRouterCache:
    fingerprint: tuple
    expires_at: float
    reason: str


_spotify_empty_router_cache: _SpotifyEmptyRouterCache | None = None


def _clamp_db(db: float) -> float:
    return max(VOLUME_MIN_DB, min(VOLUME_MAX_DB, float(db)))


def _db_to_percent(db: float) -> int:
    return db_to_percent(db)


def _percent_to_db(percent: int) -> float:
    return percent_to_db(percent)


def _delta_db_to_delta_percent(delta_db: float) -> int:
    """Convert a legacy-scale dB delta to a listening-level percent
    delta. The dial firmware sends fixed deltas like ±2.5 dB per
    encoder tick; we map those onto the calibrated 1..100 percent span.
    With the default 50 dB floor, ±5 dB remains about ±10pp."""
    return delta_db_to_delta_percent(delta_db)


def _spotify_redirect_uri() -> str:
    hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
    default_redirect_uri = default_spotify_redirect_uri(hostname)
    return os.environ.get("SPOTIFY_REDIRECT_URI") or default_redirect_uri


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
    """Build a multi-account Spotify router for dial-driven volume.
    Returns None if SPOTIFY_CLIENT_ID isn't set or no accounts have
    been authorized — _set_spotify in the coordinator treats None as
    "skip Spotify dispatch", logging a no-op."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        return None
    try:
        from ..accounts import Registry, maybe_migrate_legacy
        from ..spotify_router import Router, build_clients
        accounts_path = os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            "/var/lib/jasper-intsecrets/spotify/accounts.json",
        )
        legacy_cache_path = os.environ.get(
            "SPOTIFY_CACHE_PATH", "/var/lib/jasper-intsecrets/.spotify-cache",
        )
        redirect_uri = _spotify_redirect_uri()
        registry = Registry.load(accounts_path)
        maybe_migrate_legacy(
            registry,
            legacy_cache_path,
            default_name="default",
        )
        fingerprint = (
            client_id,
            redirect_uri,
            accounts_path,
            legacy_cache_path,
            registry.default_name,
            _spotify_account_cache_fingerprint(registry),
        )
        global _spotify_empty_router_cache
        now = time.monotonic()
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
        # build_clients returns BuildResult. The control daemon doesn't
        # surface revoked-vs-needs-oauth status to the user, so we use
        # the clients dict only — but still pass statuses through to the
        # Router so /state can introspect them if a future endpoint adds
        # a Spotify health probe.
        result = build_clients(
            registry,
            client_id=client_id,
            redirect_uri=redirect_uri,
        )
        if not result.clients:
            reason = ",".join(sorted({s.state for s in result.statuses}))
            reason = reason or "no_accounts"
            _spotify_empty_router_cache = _SpotifyEmptyRouterCache(
                fingerprint=fingerprint,
                expires_at=now + _SPOTIFY_EMPTY_ROUTER_CACHE_TTL_SEC,
                reason=reason,
            )
            return None
        _spotify_empty_router_cache = None
        return Router(
            clients=result.clients,
            default_name=registry.default_name,
            statuses=result.statuses,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("control daemon spotify router build failed: %s", e)
        return None


async def _with_coordinator(
    op: Callable[[Any], Any],
    *,
    camilla_host: str,
    camilla_port: int,
    duck_active_probe: Optional[Callable[[], Awaitable[Optional[bool]]]] = None,
) -> Any:
    """Build a VolumeCoordinator for one operation, run `op(coord)`,
    dispose. Mirrors `_dispatch_transport`'s per-request pattern — each
    HTTP request creates and tears down its own async resources, so we
    don't have to manage a long-lived asyncio loop in this stdlib HTTP
    server.

    `op` is an async callable taking the live coordinator and
    returning the per-request result (dict or scalar).

    `duck_active_probe` is forwarded into the coordinator. When set
    (callers that write camilla via the dial/web path), the
    coordinator defers its camilla write iff the probe returns True.
    See `_make_duck_active_probe` for the wire details and
    docs/HANDOFF-volume.md "Cross-daemon defer signal" for the why."""
    from ..camilla import CamillaController
    from ..renderer import RendererClient
    from ..speaker_name import runtime_name as _speaker_runtime_name
    from ..volume_coordinator import VolumeCoordinator
    from ..volume_persistence import VolumePersistence

    camilla = CamillaController(host=camilla_host, port=camilla_port)
    persistence = VolumePersistence(
        os.environ.get(
            "JASPER_VOLUME_STATE_PATH",
            "/var/lib/jasper/speaker_volume.json",
        ),
    )
    backend = RendererClient(
        librespot_state_path=os.environ.get(
            "JASPER_LIBRESPOT_STATE", "/run/librespot/state.json",
        ),
    )
    # Build a Spotify router per-request so dial volume can dispatch
    # to Spotify via Web API (librespot 0.8.0 has no local HTTP).
    # Best-effort: if env vars aren't set or no accounts authorized,
    # router is None and Spotify dispatch becomes a no-op.
    spotify_router = _build_spotify_router_or_none()
    coord = VolumeCoordinator(
        camilla=camilla,
        persistence=persistence,
        backend=backend,
        spotify_router=spotify_router,
        spotify_device_name=_speaker_runtime_name(),
        duck_active_probe=duck_active_probe,
    )
    coord.load_persisted_level()
    try:
        return await op(coord)
    finally:
        try:
            await coord.aclose()
        except Exception as e:  # noqa: BLE001
            logger.debug("coordinator aclose warning: %s", e)
        # RendererClient has no aclose — it's a stateless probe wrapper.
        # CamillaController has no aclose — sync websocket reconnects
        # on next use. GC handles cleanup of the cached client.


def _make_duck_active_probe(
    voice_socket_path: str,
    *,
    voice_socket_command: Callable[..., Awaitable[dict]] = _voice_socket_command,
) -> Callable[[], Awaitable[Optional[bool]]]:
    """Build the cross-daemon duck-active probe consumed by
    VolumeCoordinator._set_camilla in the per-request coordinators here.

    The probe asks jasper-voice over UDS whether the Ducker is
    currently holding camilla below the canonical listening_level
    target. True → defer the dial's camilla write (Ducker.restore
    will land it on session end). False → write camilla normally.
    None → unknown (UDS unreachable / voice wedged / response
    malformed); the coordinator treats this as fail-open and writes
    camilla — the dial must never silently stop working because of
    an inter-daemon problem.

    Tight 1 s timeout: STATUS is a synchronous attribute read in
    voice_daemon (no I/O). If it doesn't return in 1 s the daemon
    is wedged and we'd rather fail-open than block dial input. See
    docs/HANDOFF-volume.md "Cross-daemon defer signal"."""
    async def probe() -> Optional[bool]:
        try:
            response = await voice_socket_command(
                voice_socket_path, "STATUS", timeout=1.0,
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
        duck_active = response.get("duck_active")
        if isinstance(duck_active, bool):
            return duck_active
        # Older jasper-voice without the field, or unexpected type —
        # fail-open. Same effect as voice unreachable.
        return None
    return probe


async def _dispatch_transport(
    action: str,
    *,
    spotify_router_factory: Callable[[], Any] = _build_spotify_router_or_none,
) -> dict:
    """Build renderer + Spotify-router clients in the current event
    loop, dispatch a transport action, then close. We rebuild per
    request because httpx's AsyncClient is loop-bound: a persistent
    instance would be tied to the first request's loop and error on
    every subsequent one. The cost is small (~50 ms) and dial/remote
    presses are rare.

    `action` must be one of "toggle", "next", "previous" — the
    dispatcher's documented vocabulary."""
    # Import inside the function so jasper-control doesn't import the
    # full voice-daemon dependency tree at startup.
    from ..renderer import RendererClient
    from ..tools.transport import make_transport_dispatcher

    renderer = RendererClient(
        librespot_state_path=os.environ.get(
            "JASPER_LIBRESPOT_STATE", "/run/librespot/state.json",
        ),
    )

    dispatch = make_transport_dispatcher(renderer, spotify_router_factory())
    return await dispatch(action)
