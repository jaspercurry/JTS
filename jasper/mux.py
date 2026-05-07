"""jasper-mux — renderer source-arbiter for the debian backend.

Polls each renderer's state on a short interval and, when a new
source transitions to "playing" while another is already playing,
pauses the older one. Implements the "most recent source wins"
UX that moOde's worker.php provides on the moOde backend.

Cadence: 1 Hz polling. Each tick fans out to three concurrent
state probes; the whole tick takes <100 ms typically.

Renderer support:
  Spotify (librespot):
    detect: read /run/librespot/state.json (written by
            --onevent hook on every player event)
    pause:  Spotify Web API via spotipy — librespot 0.8.0 has no
            local control HTTP. We iterate household accounts and
            issue PUT /me/player/pause to whoever has the JTS
            device active.
  AirPlay (shairport-sync):
    detect: MPRIS PlaybackStatus == "Playing"
    pause:  MPRIS Pause method
  Bluetooth (bluez-alsa):
    detect: presence of an a2dpsnk source PCM (best-effort —
            doesn't distinguish "phone connected, not playing"
            from "phone connected and streaming")
    pause:  not gracefully supported. We log + no-op when
            asked to preempt BT. Practical impact: starting
            Spotify/AirPlay while a phone has BT open will mix
            audio for a moment until the user pauses on their
            phone. Better-than-nothing.

Backend gate: this daemon is debian-stack-only. On moOde,
worker.php handles preemption. install.sh only enables
jasper-mux when --backend=debian.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import httpx

from . import librespot_state

logger = logging.getLogger(__name__)


class Source(str, Enum):
    SPOTIFY = "spotify"
    AIRPLAY = "airplay"
    BLUETOOTH = "bluetooth"


@dataclass
class _State:
    """Per-source playing flag from the previous tick. The mux uses
    `prev → current` transitions to drive preemption — we only act
    when a source goes from not-playing to playing."""
    playing: dict[Source, bool] = field(
        default_factory=lambda: {s: False for s in Source},
    )


class Mux:
    POLL_INTERVAL_SEC = 1.0

    # When the current "winner" source dropped below this many ticks
    # ago, hold off on re-preempting toward a different source. Avoids
    # flapping when two sources both report "playing" briefly during
    # handover.
    DEBOUNCE_TICKS = 2

    def __init__(
        self,
        librespot_state_path: str = librespot_state.DEFAULT_PATH,
    ) -> None:
        self._librespot_state_path = librespot_state_path
        self._http = httpx.AsyncClient(timeout=2.0)
        self._state = _State()
        self._winner: Optional[Source] = None
        self._winner_age_ticks = 0
        # Lazy router for Web API pause. Built on first use, kept
        # for the daemon's lifetime. None means Spotify env vars
        # weren't set → pause-via-Web-API not available, log no-op.
        self._spotify_router: Any | None = None
        self._spotify_router_built = False

    async def aclose(self) -> None:
        await self._http.aclose()

    async def run(self) -> None:
        logger.info(
            "jasper-mux starting (poll=%.1fs, librespot_state=%s)",
            self.POLL_INTERVAL_SEC, self._librespot_state_path,
        )
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("mux tick failed: %s", e)
                await asyncio.sleep(self.POLL_INTERVAL_SEC)
        finally:
            await self.aclose()

    async def _tick(self) -> None:
        spotify, airplay, bluetooth = await asyncio.gather(
            self._spotify_playing(),
            self._airplay_playing(),
            self._bluetooth_playing(),
        )
        current = {
            Source.SPOTIFY: spotify,
            Source.AIRPLAY: airplay,
            Source.BLUETOOTH: bluetooth,
        }

        # Detect transitions inactive→active. Multiple in one tick
        # would be unusual but possible — we treat any of them as
        # the new winner (last-iteration wins by Source enum order,
        # which is fine in practice).
        newly_started: list[Source] = []
        for source, is_playing in current.items():
            if is_playing and not self._state.playing[source]:
                newly_started.append(source)

        self._state.playing = current
        self._winner_age_ticks += 1

        if not newly_started:
            return

        new_winner = newly_started[-1]
        prev_winner = self._winner
        logger.info(
            "source transition: %s started (was %s, age=%d ticks)",
            new_winner.value,
            prev_winner.value if prev_winner else "none",
            self._winner_age_ticks,
        )

        # Pause every OTHER source that's currently active. Note we
        # iterate over `current` not `newly_started` — if Spotify
        # started while AirPlay was already playing for 30s, we want
        # to pause AirPlay even though it didn't transition this tick.
        for source, is_playing in current.items():
            if source != new_winner and is_playing:
                await self._pause(source)

        self._winner = new_winner
        self._winner_age_ticks = 0

    # ------------------------------------------------------------------
    # State probes — each fail-soft. Returns False on any error rather
    # than raising; a missing daemon should never crash the mux.
    # ------------------------------------------------------------------

    async def _spotify_playing(self) -> bool:
        # State file is small; reading it on every tick is cheap.
        # is_playing() returns False on missing file / parse error,
        # which is the right "spotify not active" answer.
        return librespot_state.is_playing(self._librespot_state_path)

    async def _airplay_playing(self) -> bool:
        out = await _busctl(
            "call", "org.mpris.MediaPlayer2.ShairportSync",
            "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties", "Get", "ss",
            "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
        )
        return out is not None and '"Playing"' in out

    async def _bluetooth_playing(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluealsa-cli", "list-pcms",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=2.0,
            )
        except (FileNotFoundError, asyncio.TimeoutError) as e:
            logger.debug("bluealsa-cli probe failed: %s", e)
            return False
        return b"a2dpsnk/source" in stdout

    # ------------------------------------------------------------------
    # Pause actions — Spotify and AirPlay have clean APIs; Bluetooth
    # is a gap (no graceful pause from the receiver side).
    # ------------------------------------------------------------------

    async def _pause(self, source: Source) -> None:
        logger.info("preempting %s", source.value)
        if source == Source.SPOTIFY:
            ok = await self._spotify_pause_via_web_api()
            if not ok:
                logger.warning(
                    "spotify pause: no Web API account could pause the JTS "
                    "device; AirPlay and Spotify will mix briefly until "
                    "Spotify times out (~30s) or the user pauses on phone",
                )
        elif source == Source.AIRPLAY:
            ok = await _busctl(
                "call", "org.mpris.MediaPlayer2.ShairportSync",
                "/org/mpris/MediaPlayer2",
                "org.mpris.MediaPlayer2.Player", "Pause",
            )
            if ok is None:
                logger.warning("airplay pause failed (busctl returned None)")
        elif source == Source.BLUETOOTH:
            # No graceful pause API exposed by bluez-alsa. Phone
            # continues sending audio; we just don't have a way to
            # tell it to stop without disconnecting outright. User
            # pauses on phone, or we disconnect on phone.
            logger.info(
                "bluetooth: no graceful pause API. "
                "Audio may briefly mix until phone-side stops.",
            )


    # ------------------------------------------------------------------
    # Spotify Web API helpers — librespot 0.8.0 has no local control
    # HTTP, so to pause Spotify we drive Spotify's cloud → spirc →
    # librespot. Uses the same multi-account router voice tools
    # already use for Spotify queries.
    # ------------------------------------------------------------------

    def _ensure_spotify_router(self) -> Any | None:
        """Build the multi-account Spotify router on first use, or
        return the cached one. None means Spotify env vars aren't set
        and Web API isn't available."""
        if self._spotify_router_built:
            return self._spotify_router
        self._spotify_router_built = True
        client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
        client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        if not (client_id and client_secret):
            logger.debug(
                "spotify Web API: SPOTIFY_CLIENT_ID/SECRET not set; "
                "pause-via-Web-API disabled",
            )
            return None
        try:
            from .accounts import Registry, maybe_migrate_legacy
            from .spotify_router import Router, build_clients
            registry = Registry.load(os.environ.get(
                "JASPER_SPOTIFY_ACCOUNTS_PATH",
                "/var/lib/jasper/spotify/accounts.json",
            ))
            maybe_migrate_legacy(
                registry,
                os.environ.get("SPOTIFY_CACHE_PATH", "/var/lib/jasper/.spotify-cache"),
                default_name="default",
            )
            clients = build_clients(
                registry,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=os.environ.get(
                    "SPOTIFY_REDIRECT_URI",
                    "https://jts.local/spotify/callback",
                ),
            )
            if not clients:
                logger.debug("spotify Web API: no accounts authorized")
                return None
            self._spotify_router = Router(
                clients=clients, default_name=registry.default_name,
            )
            return self._spotify_router
        except Exception as e:  # noqa: BLE001
            logger.warning("spotify Web API router build failed: %s", e)
            return None

    async def _spotify_pause_via_web_api(self) -> bool:
        """Try every authorized account; pause whichever has the JTS
        device. Returns True if any account successfully paused."""
        router = self._ensure_spotify_router()
        if router is None:
            return False
        device_name = os.environ.get("JASPER_SPOTIFY_DEVICE_NAME", "JTS")
        for ac in router.clients.values():
            try:
                devices = await asyncio.to_thread(ac.sp.devices)
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "spotify devices() failed for %s: %s", ac.account.name, e,
                )
                continue
            for d in (devices.get("devices") or []):
                if d.get("name") == device_name and d.get("is_active"):
                    try:
                        await asyncio.to_thread(
                            ac.sp.pause_playback, device_id=d.get("id"),
                        )
                        logger.info(
                            "spotify pause via Web API: account=%s device=%s",
                            ac.account.name, d.get("id"),
                        )
                        return True
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "spotify pause failed for %s: %s",
                            ac.account.name, e,
                        )
                        continue
        return False


async def _busctl(*args: str) -> Optional[str]:
    """Run busctl on the system bus, return stdout on success or
    None on any error. Used for both PlaybackStatus polling and
    Pause method invocation."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", "replace")


async def _amain(args: argparse.Namespace) -> None:
    mux = Mux(librespot_state_path=args.librespot_state)
    await mux.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Jasper renderer source-arbiter")
    parser.add_argument(
        "--librespot-state",
        default=os.environ.get(
            "JASPER_LIBRESPOT_STATE", librespot_state.DEFAULT_PATH,
        ),
        help="path to librespot state file written by the --onevent "
             "hook (default from JASPER_LIBRESPOT_STATE env or "
             f"{librespot_state.DEFAULT_PATH})",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="root log level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Silence httpx's per-request INFO log — at 1 Hz polling, leaving
    # this on writes ~86k log lines/day which crowds out anything
    # interesting in the journal. We still see WARN+ from httpx
    # (which is what we'd want to debug a real issue).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
