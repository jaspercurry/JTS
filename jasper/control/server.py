"""HTTP control surface for external clients (dial, future wall switches,
home automation). Bound to LAN so an ESP32 dial on the household network
can drive volume / transport / session.

Stack: stdlib http.server (ThreadingHTTPServer), pycamilladsp client,
VolumeCoordinator (source-aware dispatch).

The route table is in `_make_handler` below — `do_GET` and `do_POST`
own the dispatch in one place rather than mirroring the list here
(that mirror went stale several times). Highlights:

- Volume + transport + session-bypass: dial-driven actions.
- /state: cross-daemon JSON snapshot — voice / audio / renderers /
  satellites; consumable from the /voice web UI, jasper-doctor, or
  `curl`.
- /cue/play: proxy to voice_daemon's UDS so a cue plays through
  the daemon's already-correctly-gained TtsPlayout.
- /dial/status: focused dial heartbeat (subset of /state.satellites.dial,
  kept because jasper-doctor calls it directly).

Volume dispatch: requests build a fresh VolumeCoordinator per call
(matches the per-request _toggle_transport pattern). The coordinator
reads the canonical listening_level from /var/lib/jasper/speaker_volume.json,
applies the change, dispatches to the active source (or CamillaDSP
when idle), persists. This daemon doesn't run inbound observers —
that's voice_daemon's job. Both daemons converge through persistence.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

logger = logging.getLogger(__name__)
dial_log = logging.getLogger("jasper.dial")


# Most-recent dial heartbeat. Updated by the UDP log listener every
# time a datagram arrives; read by GET /dial/status. Kept module-level
# so jasper-doctor can ask "is a dial actually talking to us?" without
# parsing the journal. Lock isn't needed — Python dict assignment is
# atomic and a stale read is harmless for a heartbeat.
_dial_heartbeat: dict[str, Any] = {
    "last_seen_at": None,    # float epoch seconds, or None
    "last_seen_ip": None,    # str IPv4, or None
    "last_message": None,    # str (last UDP payload), or None
}


# Same range jasper.tools.audio uses for the voice-driven volume tools.
# 0% = silent at the speaker; 100% = full digital scale.
VOLUME_MIN_DB = -50.0
VOLUME_MAX_DB = 0.0


def _clamp_db(db: float) -> float:
    return max(VOLUME_MIN_DB, min(VOLUME_MAX_DB, float(db)))


def _db_to_percent(db: float) -> int:
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return max(0, min(100, round((float(db) - VOLUME_MIN_DB) / span * 100.0)))


def _percent_to_db(percent: int) -> float:
    p = max(0, min(100, int(percent)))
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return VOLUME_MIN_DB + (span * p / 100.0)


def _delta_db_to_delta_percent(delta_db: float) -> int:
    """Convert a legacy-scale dB delta to a listening-level percent
    delta. The dial firmware sends fixed deltas like ±2.5 dB per
    encoder tick; we map those onto the 0-100 percent scale using
    the same 50 dB span the camilla-only path used. ±5 dB == ±10pp."""
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return round(float(delta_db) / span * 100.0)


# ---------- peering daemon supervisor ----------

# The peering daemon runs an asyncio event loop; jasper-control is
# stdlib threaded HTTP. Bridge by spawning a single background daemon
# thread that owns the asyncio loop for peering. When peering is OFF
# (the default), the thread is not even created — zero cost on a
# single-Pi household.
_peering_thread: threading.Thread | None = None


def _run_peering_loop() -> None:
    """Background thread target: own an asyncio loop, run the
    PeeringDaemon until the process exits."""
    # Lazy imports — keep jasper-control's import cost light when
    # peering is OFF and these modules never load.
    from ..peering import load_config
    from ..peering.daemon import PeeringDaemon

    cfg = load_config()
    if not cfg.enabled:
        logger.info(
            "event=peering.thread.exit mode=%s — daemon will not start",
            cfg.mode.value,
        )
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    daemon = PeeringDaemon(cfg)
    try:
        loop.run_until_complete(daemon.start())
        loop.run_forever()
    except Exception:  # noqa: BLE001
        logger.exception("peering daemon thread crashed")
    finally:
        try:
            loop.run_until_complete(daemon.stop())
        except Exception:  # noqa: BLE001
            logger.exception("peering daemon stop failed")
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def start_peering_daemon_if_enabled() -> None:
    """Start the peering daemon in a background thread iff peering
    is enabled in /var/lib/jasper/peering.env. Idempotent — repeated
    calls are no-ops once the thread exists.

    The check is done in the worker thread (not here) so that even
    when peering is OFF, we don't pay the cost of importing zeroconf.
    """
    global _peering_thread
    if _peering_thread is not None:
        return
    _peering_thread = threading.Thread(
        target=_run_peering_loop,
        name="peering-daemon",
        daemon=True,
    )
    _peering_thread.start()


def _read_cloud_activity() -> dict[str, Any]:
    """Roll up cloud LLM activity for the /system dashboard.

    Queries the UsageStore SQLite for per-provider session/token/cost
    aggregates this month + last successful turn timestamp. Returns
    {"available": False, ...} when the DB hasn't been created yet
    (fresh install, daemon never opened a session). All queries are
    local SQLite, sub-millisecond on a Pi 5.
    """
    db_path = os.environ.get(
        "JASPER_USAGE_DB", "/var/lib/jasper/usage.db",
    )
    if not os.path.exists(db_path):
        return {"available": False, "reason": "no usage.db yet"}
    try:
        from ..usage import UsageStore
        store = UsageStore(db_path)
        return {
            "available": True,
            "spend_month_to_date_usd": round(store.spend_month_to_date_usd(), 4),
            "spend_last_24h_usd": round(store.spend_last_24h_usd(), 4),
            "sessions_today": store.session_count_today_utc(),
            "last_successful_turn_at": store.last_successful_turn_at(),
            "by_provider": store.aggregate_by_provider(),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("cloud-activity read failed: %s", e)
        return {"available": False, "reason": str(e)}


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
        registry = Registry.load(os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            "/var/lib/jasper/spotify/accounts.json",
        ))
        maybe_migrate_legacy(
            registry,
            os.environ.get(
                "SPOTIFY_CACHE_PATH", "/var/lib/jasper/.spotify-cache",
            ),
            default_name="default",
        )
        hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
        clients = build_clients(
            registry,
            client_id=client_id,
            redirect_uri=os.environ.get(
                "SPOTIFY_REDIRECT_URI",
                f"https://jaspercurry.github.io/spotify-oauth-callback/?host={hostname}",
            ),
        )
        if not clients:
            return None
        return Router(clients=clients, default_name=registry.default_name)
    except Exception as e:  # noqa: BLE001
        logger.debug("control daemon spotify router build failed: %s", e)
        return None


async def _with_coordinator(
    op: Callable[[Any], Any],
    *,
    camilla_host: str,
    camilla_port: int,
) -> Any:
    """Build a VolumeCoordinator for one operation, run `op(coord)`,
    dispose. Mirrors `_toggle_transport`'s per-request pattern — each
    HTTP request creates and tears down its own async resources, so we
    don't have to manage a long-lived asyncio loop in this stdlib HTTP
    server.

    `op` is an async callable taking the live coordinator and
    returning the per-request result (dict or scalar)."""
    from ..camilla import CamillaController
    from ..renderer import RendererClient
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
        spotify_device_name=os.environ.get(
            "JASPER_SPOTIFY_DEVICE_NAME", "JTS",
        ),
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


async def _voice_socket_command(
    socket_path: str, cmd: str, *, timeout: float = 5.0,
) -> dict:
    """Send one ASCII line to voice_daemon's control socket and return
    the parsed JSON response. Used by /session/start, /session/end,
    and /cue/play. The default 5s timeout covers session-state
    commands; cue playback takes longer (~6s for a 5s cue plus
    duck/restore plus drain) and bumps timeout explicitly."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError("voice_daemon returned no response")
    return json.loads(line.decode("utf-8"))


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
) -> dict[str, Any]:
    """Aggregate state across daemons for GET /state. Each section
    fails soft — voice unreachable / camilla restarting / dial never
    connected → that section reports null instead of erroring out
    the whole response. Slow probes fan out in parallel so the call
    completes in ~200 ms typical."""
    from datetime import datetime, timezone

    from .. import librespot_state
    from ..camilla import CamillaController

    # Cheap synchronous reads first.
    voice_provider = os.environ.get("JASPER_VOICE_PROVIDER", "gemini")
    if voice_provider == "openai":
        voice_model = os.environ.get("JASPER_OPENAI_MODEL")
    elif voice_provider == "grok":
        voice_model = os.environ.get("JASPER_GROK_MODEL")
    else:
        voice_model = os.environ.get("JASPER_GEMINI_MODEL")

    listening_level: int | None = None
    try:
        path = os.environ.get(
            "JASPER_VOLUME_STATE_PATH",
            "/var/lib/jasper/speaker_volume.json",
        )
        with open(path) as f:
            blob = json.load(f)
        raw_level = blob.get("listening_level")
        if isinstance(raw_level, (int, float)) and 0 <= raw_level <= 100:
            listening_level = int(raw_level)
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    # Slow probes — fan out in parallel.
    async def _camilla_volume() -> float | None:
        try:
            cam = CamillaController(host=camilla_host, port=camilla_port)
            return await cam.get_volume_db(best_effort=True)
        except Exception:  # noqa: BLE001
            return None

    async def _airplay_playing() -> bool | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "busctl", "--system", "call",
                "org.mpris.MediaPlayer2.ShairportSync",
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties", "Get", "ss",
                "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=2.0,
            )
            if proc.returncode != 0:
                return None
            return b'"Playing"' in stdout
        except (FileNotFoundError, asyncio.TimeoutError):
            return None

    async def _voice_status() -> dict | None:
        try:
            return await _voice_socket_command(
                voice_socket_path, "STATUS", timeout=2.0,
            )
        except (FileNotFoundError, OSError, asyncio.TimeoutError, RuntimeError):
            return None

    cam_db, airplay, voice_st = await asyncio.gather(
        _camilla_volume(),
        _airplay_playing(),
        _voice_status(),
    )

    spotify_blob = librespot_state.read(
        os.environ.get("JASPER_LIBRESPOT_STATE", librespot_state.DEFAULT_PATH),
    )
    spotify = {
        "playing": bool(spotify_blob.get("playing", False)),
        "track_id": spotify_blob.get("track_id"),
        "uri": spotify_blob.get("uri"),
        "session_active": bool(spotify_blob.get("session_active", False)),
    }

    voice_session = bool(voice_st) and voice_st.get("state") == "SESSION"
    if voice_session:
        active_source: str = "voice"
    elif spotify["playing"]:
        active_source = "spotify"
    elif airplay:
        active_source = "airplay"
    else:
        active_source = "idle"

    dial = dict(_dial_heartbeat)
    if dial["last_seen_at"] is not None:
        age = round(time.time() - dial["last_seen_at"], 1)
        dial["age_seconds"] = age
        dial["online"] = age < 30.0
    else:
        dial["age_seconds"] = None
        dial["online"] = False

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "voice": {
            "provider": voice_provider,
            "model": voice_model,
            "session_active": voice_session,
            "spend_allowed": (voice_st or {}).get("spend_allowed"),
            "connection_paused": (voice_st or {}).get("connection_paused"),
            "mic_muted": (voice_st or {}).get("mic_muted"),
            "reachable": voice_st is not None,
        },
        "audio": {
            "main_volume_db": (
                round(cam_db, 2) if cam_db is not None else None
            ),
            "listening_level_percent": listening_level,
        },
        "renderers": {
            "spotify": spotify,
            "airplay": (
                None if airplay is None else {"playing": airplay}
            ),
        },
        "active_source": active_source,
        "satellites": {
            "dial": dial,
        },
    }


async def _toggle_transport() -> dict:
    """Build renderer + Spotify-router clients in the current event
    loop, dispatch a 'toggle' transport action, then close. We rebuild
    per request because httpx's AsyncClient is loop-bound: a persistent
    instance would be tied to the first request's loop and error on
    every subsequent one. The cost is small (~50 ms) and dial clicks
    are rare."""
    # Import inside the function so jasper-control doesn't import the
    # full voice-daemon dependency tree at startup.
    from ..accounts import Registry, maybe_migrate_legacy
    from ..renderer import RendererClient
    from ..spotify_router import Router, build_clients
    from ..tools.transport import make_transport_dispatcher

    renderer = RendererClient(
        librespot_state_path=os.environ.get(
            "JASPER_LIBRESPOT_STATE", "/run/librespot/state.json",
        ),
    )
    router: Router | None = None
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if client_id:
        accounts_path = os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            "/var/lib/jasper/spotify/accounts.json",
        )
        legacy_cache = os.environ.get(
            "SPOTIFY_CACHE_PATH", "/var/lib/jasper/.spotify-cache",
        )
        hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
        redirect_uri = os.environ.get(
            "SPOTIFY_REDIRECT_URI",
            f"https://jaspercurry.github.io/spotify-oauth-callback/?host={hostname}",
        )
        accounts = Registry.load(accounts_path)
        maybe_migrate_legacy(accounts, legacy_cache, default_name="default")
        clients = build_clients(
            accounts,
            client_id=client_id,
            redirect_uri=redirect_uri,
        )
        if clients:
            router = Router(
                clients=clients, default_name=accounts.default_name,
            )

    dispatch = make_transport_dispatcher(renderer, router)
    return await dispatch("toggle")


def _make_handler(
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    sampler: Any = None,
) -> type[BaseHTTPRequestHandler]:

    async def _set_op(percent: int):
        async def _op(coord):
            return await coord.set_listening_level(percent)
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
        )

    async def _adjust_op(delta_percent: int):
        async def _op(coord):
            return await coord.adjust_listening_level(delta_percent)
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
        )

    async def _get_op():
        async def _op(coord):
            return coord.get_listening_level()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
        )

    async def _mute_toggle_op():
        async def _op(coord):
            # If currently muted, unmute and return restored level.
            # Otherwise mute and return 0 (the new actual level).
            if coord.is_muted():
                return await coord.unmute()
            await coord.mute()
            return 0
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}

        def _volume_payload(self, percent: int) -> dict[str, Any]:
            # `db` is computed for back-compat with the dial firmware
            # which reads `percent` but logs `db`. The legacy 50 dB
            # scale is still the lingua franca for clients that haven't
            # been updated.
            return {"db": round(_percent_to_db(percent), 3), "percent": percent}

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_json({"ok": True})
                return
            if self.path == "/volume":
                try:
                    percent = asyncio.run(_get_op())
                except Exception as e:  # noqa: BLE001
                    logger.exception("get volume failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                self._send_json(self._volume_payload(percent))
                return
            if self.path == "/mic":
                # Read mic mute state from the voice daemon's STATUS
                # response. If the daemon isn't reachable, surface that
                # explicitly so the UI can grey out the toggle instead
                # of pretending we know the state.
                try:
                    st = asyncio.run(_voice_socket_command(
                        voice_socket_path, "STATUS", timeout=2.0,
                    ))
                except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                    self._send_json(
                        {"error": f"voice_daemon unreachable: {e}"},
                        status=503,
                    )
                    return
                except Exception as e:  # noqa: BLE001
                    logger.exception("mic STATUS failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                self._send_json({"muted": bool(st.get("mic_muted", False))})
                return
            if self.path == "/state":
                # Cross-daemon snapshot — voice / audio / renderers /
                # satellites. Polled by the /voice web UI for live
                # status, used by jasper-doctor for one-shot health,
                # and consumable from `curl jts.local:8780/state | jq`
                # for ad-hoc debugging. ~200 ms typical (mostly the
                # parallel busctl + camilla WS probes).
                try:
                    state = asyncio.run(_get_state(
                        camilla_host=camilla_host,
                        camilla_port=camilla_port,
                        voice_socket_path=voice_socket_path,
                    ))
                except Exception as e:  # noqa: BLE001
                    logger.exception("/state aggregation failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                self._send_json(state)
                return
            if self.path == "/dial/status":
                # Heartbeat snapshot — used by jasper-doctor's
                # "is the dial actually talking to us?" check.
                snap = dict(_dial_heartbeat)
                if snap["last_seen_at"] is not None:
                    snap["age_seconds"] = round(
                        time.time() - snap["last_seen_at"], 1,
                    )
                else:
                    snap["age_seconds"] = None
                self._send_json(snap)
                return
            if self.path == "/system/snapshot":
                # Snapshot for the /system dashboard. Current values +
                # 60-min ring buffers for the sparklines + build info
                # + cloud activity rolled up from UsageStore (per-
                # provider sessions/tokens/cost month-to-date).
                # Sampler may be None in tests / direct CLI invocation;
                # surface an empty history rather than 500.
                from .system_metrics import read_build_info
                payload: dict[str, Any] = {
                    "build": read_build_info(),
                    "metrics": (
                        sampler.snapshot() if sampler is not None else None
                    ),
                    "cloud": _read_cloud_activity(),
                    "voice_provider": os.environ.get(
                        "JASPER_VOICE_PROVIDER", "gemini",
                    ),
                }
                self._send_json(payload)
                return
            if self.path == "/system/diagnostics":
                # Run jasper-doctor --json and proxy its output. ~3-5 s
                # on a Pi 5; the dashboard surfaces a spinner during
                # the call. Single-flight semantics not enforced here
                # (the dashboard disables the button while in flight).
                try:
                    proc = subprocess.run(
                        ["/opt/jasper/.venv/bin/jasper-doctor", "--json"],
                        capture_output=True, text=True, timeout=30,
                    )
                except (subprocess.SubprocessError, FileNotFoundError) as e:
                    self._send_json(
                        {"error": f"jasper-doctor invocation failed: {e}"},
                        status=502,
                    )
                    return
                # jasper-doctor exits 1 when any check failed; that's
                # a normal "report has failures" outcome, not an HTTP
                # error. Parse stdout regardless.
                try:
                    body = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    self._send_json(
                        {"error": "doctor output not JSON",
                         "stdout": proc.stdout[:500],
                         "stderr": proc.stderr[:500]},
                        status=502,
                    )
                    return
                self._send_json(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/volume/adjust":
                body = self._read_json()
                # Support both legacy delta_db (dial firmware compat,
                # interpreted on the 50 dB camilla scale) and the
                # cleaner delta_percent for newer clients.
                if "delta_percent" in body:
                    try:
                        delta_pct = int(body["delta_percent"])
                    except (TypeError, ValueError):
                        self._send_json(
                            {"error": "delta_percent must be an integer"},
                            status=400,
                        )
                        return
                elif "delta_db" in body:
                    try:
                        delta_pct = _delta_db_to_delta_percent(
                            float(body["delta_db"]),
                        )
                    except (TypeError, ValueError):
                        self._send_json(
                            {"error": "delta_db must be a number"},
                            status=400,
                        )
                        return
                else:
                    self._send_json(
                        {"error": "missing delta_db or delta_percent"},
                        status=400,
                    )
                    return
                try:
                    new_pct = asyncio.run(_adjust_op(delta_pct))
                except Exception as e:  # noqa: BLE001
                    logger.exception("adjust volume failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                logger.info(
                    "event=volume.adjust delta_pct=%d new_pct=%d client=%s",
                    delta_pct, new_pct, self.address_string(),
                )
                self._send_json(self._volume_payload(new_pct))
                return

            if self.path == "/volume/set":
                body = self._read_json()
                # Support both legacy `db` (dial / older clients) and
                # the cleaner `percent`. Percent is the canonical unit
                # for listening_level.
                if "percent" in body:
                    try:
                        target_pct = int(body["percent"])
                    except (TypeError, ValueError):
                        self._send_json(
                            {"error": "percent must be an integer"}, status=400,
                        )
                        return
                elif "db" in body:
                    try:
                        target_pct = _db_to_percent(float(body["db"]))
                    except (TypeError, ValueError):
                        self._send_json(
                            {"error": "db must be a number"}, status=400,
                        )
                        return
                else:
                    self._send_json(
                        {"error": "missing db or percent"}, status=400,
                    )
                    return
                try:
                    new_pct = asyncio.run(_set_op(target_pct))
                except Exception as e:  # noqa: BLE001
                    logger.exception("set volume failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                logger.info(
                    "event=volume.set new_pct=%d client=%s",
                    new_pct, self.address_string(),
                )
                self._send_json(self._volume_payload(new_pct))
                return

            if self.path == "/volume/mute":
                # Toggle mute: muted → unmute (restore pre-mute level),
                # unmuted → mute (save current level, drop to 0).
                # Used by HID accessory clicks (jasper-input) and any
                # other one-shot toggle caller. State persistence and
                # the pre-mute level live inside VolumeCoordinator.
                try:
                    new_pct = asyncio.run(_mute_toggle_op())
                except Exception as e:  # noqa: BLE001
                    logger.exception("mute toggle failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                logger.info(
                    "event=volume.mute new_pct=%d client=%s",
                    new_pct, self.address_string(),
                )
                self._send_json(self._volume_payload(new_pct))
                return

            if self.path == "/transport/toggle":
                try:
                    result = asyncio.run(_toggle_transport())
                except Exception as e:  # noqa: BLE001
                    logger.exception("transport toggle failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                if "error" in result:
                    self._send_json(result, status=502)
                    return
                self._send_json(result)
                return

            if self.path == "/session/start" or self.path == "/session/end":
                cmd = "START" if self.path.endswith("start") else "END"
                try:
                    result = asyncio.run(
                        _voice_socket_command(voice_socket_path, cmd),
                    )
                except FileNotFoundError:
                    self._send_json(
                        {"error": "voice_daemon not running (socket not found)"},
                        status=503,
                    )
                    return
                except (OSError, asyncio.TimeoutError) as e:
                    self._send_json(
                        {"error": f"voice_daemon unreachable: {e}"},
                        status=503,
                    )
                    return
                except Exception as e:  # noqa: BLE001
                    logger.exception("session %s failed", cmd)
                    self._send_json({"error": str(e)}, status=502)
                    return
                # Result codes from voice_daemon's manual_session_*:
                #   OK / BUSY / CAP / PAUSED / NO_SESSION / ALREADY_ENDED / ERROR
                # Map non-OK outcomes to non-2xx so the dial's HTTP
                # error path can show the right LED color.
                http_status = 200
                if result.get("result") not in ("OK", None):
                    if result.get("result") in ("CAP", "PAUSED"):
                        http_status = 503
                    elif result.get("result") in ("BUSY", "NO_SESSION", "ALREADY_ENDED"):
                        http_status = 409
                    else:
                        http_status = 502
                self._send_json(result, status=http_status)
                return

            if self.path == "/cue/play":
                # POST /cue/play  body: {"slug": "<cue_slug>"}
                # Routes the request through voice_daemon's control
                # socket so the cue plays through the daemon's
                # already-correctly-gained TtsPlayout. A separate
                # standalone client (e.g., `jasper-cues play <slug>`)
                # would have to recreate the daemon's volume math
                # to match levels, and got it wrong (~20 dB too
                # loud). Centralising here keeps levels consistent.
                body = self._read_json()
                slug = (body.get("slug") or "").strip()
                if not slug:
                    self._send_json(
                        {"error": "missing 'slug' in body"}, status=400,
                    )
                    return
                try:
                    # Cues run ~5-6s of audio plus duck/restore plus
                    # drain. 30s gives generous headroom even for the
                    # longest reasonable cue.
                    result = asyncio.run(_voice_socket_command(
                        voice_socket_path, f"CUE_PLAY {slug}",
                        timeout=30.0,
                    ))
                except FileNotFoundError:
                    self._send_json(
                        {"error": "voice_daemon not running"}, status=503,
                    )
                    return
                except (OSError, asyncio.TimeoutError) as e:
                    self._send_json(
                        {"error": f"voice_daemon unreachable: {e}"},
                        status=503,
                    )
                    return
                except Exception as e:  # noqa: BLE001
                    logger.exception("cue play failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                http_status = 200
                if result.get("result") == "missing_slug":
                    http_status = 400
                elif result.get("result") == "unknown_slug":
                    http_status = 404
                elif result.get("result") == "cues_not_configured":
                    http_status = 503
                elif result.get("result") != "ok":
                    http_status = 502
                self._send_json(result, status=http_status)
                return

            if self.path == "/mic/mute":
                # POST /mic/mute  body: {"muted": bool}
                # Idempotent set. Forwards MUTE or UNMUTE to the voice
                # daemon's control socket, which drops mic frames at
                # the wake-loop gate (mute) or resumes (unmute) and
                # plays a short click on either edge for feedback.
                body = self._read_json()
                if "muted" not in body:
                    self._send_json(
                        {"error": "missing 'muted' in body"}, status=400,
                    )
                    return
                cmd = "MUTE" if bool(body["muted"]) else "UNMUTE"
                try:
                    result = asyncio.run(_voice_socket_command(
                        voice_socket_path, cmd, timeout=3.0,
                    ))
                except FileNotFoundError:
                    self._send_json(
                        {"error": "voice_daemon not running"}, status=503,
                    )
                    return
                except (OSError, asyncio.TimeoutError) as e:
                    self._send_json(
                        {"error": f"voice_daemon unreachable: {e}"},
                        status=503,
                    )
                    return
                except Exception as e:  # noqa: BLE001
                    logger.exception("mic %s failed", cmd)
                    self._send_json({"error": str(e)}, status=502)
                    return
                logger.info(
                    "event=mic.set muted=%s client=%s",
                    bool(body["muted"]), self.address_string(),
                )
                # Read back the truth from the daemon. STATUS is cheap
                # and the daemon's flag is authoritative.
                try:
                    st = asyncio.run(_voice_socket_command(
                        voice_socket_path, "STATUS", timeout=2.0,
                    ))
                    muted_now = bool(st.get("mic_muted", False))
                except Exception:  # noqa: BLE001
                    # If readback fails, trust the set and move on.
                    muted_now = bool(body["muted"])
                self._send_json({"muted": muted_now, "result": result.get("result")})
                return

            if self.path in ("/system/restart/voice",
                             "/system/restart/audio",
                             "/system/reboot"):
                # Action endpoints for the /system dashboard. All
                # shell out to systemctl; jasper-control already runs
                # as root so no sudo needed. Returns immediately —
                # the restart is async on systemd's side and the
                # dashboard polls /system/snapshot to know when
                # things are back up.
                #
                # Risk model: LAN-trust (consistent with the wizards).
                # Anyone on the WiFi can trigger these; the dashboard's
                # confirm dialogs are UX, not security.
                if self.path == "/system/restart/voice":
                    units = ["jasper-voice.service"]
                    action = "restart-voice"
                elif self.path == "/system/restart/audio":
                    units = [
                        "jasper-camilla.service",
                        "librespot.service",
                        "shairport-sync.service",
                        "bluealsa-aplay.service",
                    ]
                    action = "restart-audio"
                else:
                    units = []  # systemctl reboot — no units
                    action = "reboot"
                try:
                    if action == "reboot":
                        subprocess.Popen(["systemctl", "reboot"])
                    else:
                        # Use start-after-stop semantics. Don't block
                        # on the systemctl call (jasper-aec-bridge +
                        # jasper-voice both take up to 90s to stop
                        # cleanly under the SIGTERM timeout).
                        subprocess.Popen(["systemctl", "restart", *units])
                except (OSError, subprocess.SubprocessError) as e:
                    self._send_json(
                        {"error": f"systemctl invocation failed: {e}"},
                        status=502,
                    )
                    return
                self._send_json({
                    "ok": True,
                    "action": action,
                    "units": units,
                })
                return

            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def build_server(
    host: str,
    port: int,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str = "/run/jasper/voice.sock",
    sampler: Any = None,
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        (host, port),
        _make_handler(camilla_host, camilla_port, voice_socket_path, sampler),
    )


def run_dial_log_listener(host: str, port: int) -> threading.Thread:
    """Listen for one-line UDP datagrams from the dial and re-emit them
    via the Python logger (so `journalctl -u jasper-control` shows them
    interleaved with the HTTP-side log). Fire-and-forget on the dial
    side — UDP loss is acceptable for diagnostic output, and the dial
    isn't blocked on a TCP handshake when the Pi is unreachable.

    The listener runs in a daemon thread so it doesn't block server
    shutdown."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(1.0)

    def _loop() -> None:
        logger.info("dial-log UDP listener bound to %s:%d", host, port)
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                logger.warning("dial-log socket error: %s", e)
                return
            try:
                msg = data.decode("utf-8", errors="replace").rstrip()
            except Exception:  # noqa: BLE001
                msg = repr(data)
            # Tag with sender IP so multi-dial setups don't get confused.
            dial_log.info("[%s] %s", addr[0], msg)
            # Heartbeat for jasper-doctor's "is the dial talking?" check.
            _dial_heartbeat["last_seen_at"] = time.time()
            _dial_heartbeat["last_seen_ip"] = addr[0]
            _dial_heartbeat["last_message"] = msg

    t = threading.Thread(target=_loop, name="dial-log-listener", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-control",
        description="HTTP control surface for the JTS speaker (dial, automation, etc.)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_CONTROL_HOST", "0.0.0.0"),
        help="bind host (default 0.0.0.0 — LAN-reachable)",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_CONTROL_PORT", "8780")),
    )
    parser.add_argument(
        "--camilla-host",
        default=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--camilla-port", type=int,
        default=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )
    parser.add_argument(
        "--dial-log-host",
        default=os.environ.get("JASPER_DIAL_LOG_HOST", "0.0.0.0"),
        help="bind host for the dial UDP log listener",
    )
    parser.add_argument(
        "--dial-log-port", type=int,
        default=int(os.environ.get("JASPER_DIAL_LOG_PORT", "5514")),
        help="UDP port for dial log datagrams (default 5514)",
    )
    parser.add_argument(
        "--voice-socket",
        default=os.environ.get(
            "JASPER_VOICE_CONTROL_SOCKET", "/run/jasper/voice.sock",
        ),
        help="path to voice_daemon's control UDS",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # System metrics sampler — 5 s ring buffer for the /system dashboard.
    # Daemon thread, exits with the process.
    from .system_metrics import SystemSampler
    sampler = SystemSampler()
    sampler.start()

    server = build_server(
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.voice_socket,
        sampler=sampler,
    )
    run_dial_log_listener(args.dial_log_host, args.dial_log_port)
    # Multi-device peering daemon. No-op (no thread, no asyncio loop,
    # no zeroconf import) when /var/lib/jasper/peering.env has
    # JASPER_PEERING=off — the default. The user enables it via the
    # /peers/ web wizard (added in a follow-up PR), which writes the
    # env file and restarts jasper-control to pick up the new mode.
    start_peering_daemon_if_enabled()
    logger.info(
        "jasper-control listening on http://%s:%d "
        "(camilla=%s:%d, dial-log=%s:%d/udp, voice=%s)",
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.dial_log_host, args.dial_log_port,
        args.voice_socket,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
