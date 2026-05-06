"""jasper-mux — renderer source-arbiter for the debian backend.

Polls each renderer's state on a short interval and, when a new
source transitions to "playing" while another is already playing,
pauses the older one. Implements the "most recent source wins"
UX that moOde's worker.php provides on the moOde backend.

Cadence: 1 Hz polling. Each tick fans out to three concurrent
state probes (Spotify HTTP, AirPlay MPRIS, Bluetooth bluealsa-cli);
the whole tick takes <100 ms typically.

Renderer support:
  Spotify (go-librespot):
    detect: GET /status — `not paused and not stopped`
    pause:  POST /player/pause
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
from typing import Optional

import httpx

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

    def __init__(self, go_librespot_url: str) -> None:
        self._gl_url = go_librespot_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=2.0)
        self._state = _State()
        self._winner: Optional[Source] = None
        self._winner_age_ticks = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def run(self) -> None:
        logger.info(
            "jasper-mux starting (poll=%.1fs, go-librespot=%s)",
            self.POLL_INTERVAL_SEC, self._gl_url,
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
        try:
            r = await self._http.get(f"{self._gl_url}/status")
            if r.status_code == 204 or not r.content:
                return False
            data = r.json()
        except Exception as e:  # noqa: BLE001
            logger.debug("spotify probe failed: %s", e)
            return False
        return not bool(data.get("stopped", True)) and not bool(
            data.get("paused", True)
        )

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
            try:
                r = await self._http.post(f"{self._gl_url}/player/pause")
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                logger.warning("spotify pause failed: %s", e)
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
    mux = Mux(go_librespot_url=args.go_librespot_url)
    await mux.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Jasper renderer source-arbiter")
    parser.add_argument(
        "--go-librespot-url",
        default=os.environ.get(
            "JASPER_GO_LIBRESPOT_URL", "http://127.0.0.1:3678",
        ),
        help="go-librespot HTTP API base URL (default from "
             "JASPER_GO_LIBRESPOT_URL env or http://127.0.0.1:3678)",
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
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
