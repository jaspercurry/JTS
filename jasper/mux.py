"""jasper-mux — renderer source-arbiter.

Polls each renderer's state on a short interval and, when a new
source transitions to "playing" while another is already playing,
pauses the older one. Implements "most recent source wins" UX
across the AirPlay / Spotify Connect / Bluetooth A2DP renderers.

Cadence: 1 Hz polling. Each tick fans out to three concurrent
state probes; the whole tick takes <100 ms typically.

Renderer support:
  Spotify (librespot):
    detect: read /run/librespot/state.json (written by
            --onevent hook on every player event)
    pause:  Two-tier escalation. Tier 1 is Spotify Web API via
            spotipy — librespot 0.8.0 has no local control HTTP.
            We iterate household accounts and issue
            PUT /me/player/pause to any account that has the JTS
            device in its list. Tier 2 (added 2026-05-22) is
            `systemctl restart librespot.service` if Tier 1 fails
            — guarantees librespot releases its FD on the renderer
            dmix so the new winner is heard alone. Tier 2 is
            necessary because after the 2026-05-22 dmix change,
            two renderers no longer contend for ALSA EBUSY; without
            Tier 2 an un-pauseable librespot would simply mix audio
            alongside the new winner. Off-switch:
            JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled.
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

"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from . import librespot_state
from .source_state import airplay_playing, bluetooth_playing, spotify_playing

logger = logging.getLogger(__name__)


class Source(str, Enum):
    SPOTIFY = "spotify"
    AIRPLAY = "airplay"
    BLUETOOTH = "bluetooth"


def _spotify_preempt_restart_disabled() -> bool:
    """Env-var escape hatch for the Spotify-preempt Tier 2 escalation
    (the systemctl restart librespot fallback added 2026-05-22).

    Set JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled to revert preempt
    to "Web API only, mix-on-failure" behaviour — useful if the
    restart is ever found to cause more disruption than the brief
    audio mix it was meant to avoid. Default: enabled.
    """
    return os.environ.get(
        "JASPER_MUX_SPOTIFY_PREEMPT_RESTART", "",
    ).strip().lower() == "disabled"


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
        self._state = _State()
        self._winner: Optional[Source] = None
        self._winner_age_ticks = 0
        # Lazy router for Web API pause. Built on first use, kept
        # for the daemon's lifetime. None means Spotify env vars
        # weren't set → pause-via-Web-API not available, log no-op.
        self._spotify_router: Any | None = None
        self._spotify_router_built = False

    async def run(self) -> None:
        logger.info(
            "jasper-mux starting (poll=%.1fs, librespot_state=%s)",
            self.POLL_INTERVAL_SEC, self._librespot_state_path,
        )
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("mux tick failed: %s", e)
            await asyncio.sleep(self.POLL_INTERVAL_SEC)

    async def _tick(self) -> None:
        spotify, airplay, bluetooth = await asyncio.gather(
            spotify_playing(self._librespot_state_path),
            airplay_playing(),
            bluetooth_playing(),
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
    # Pause actions — Spotify and AirPlay have clean APIs; Bluetooth
    # is a gap (no graceful pause from the receiver side).
    # ------------------------------------------------------------------

    async def _pause(self, source: Source) -> None:
        logger.info("preempting %s", source.value)
        if source == Source.SPOTIFY:
            ok = await self._spotify_pause_via_web_api()
            if ok:
                return
            # Tier 1 failed. After the 2026-05-22 renderer-dmix change,
            # an un-pauseable librespot doesn't crash on EBUSY anymore —
            # it just keeps streaming and mixes with the new winner.
            # The user's contract ("we cannot have both played at the
            # same time") requires us to force a release. systemctl
            # restart kills librespot's FD on the renderer dmix; the
            # new winner is then heard alone for the ~2-3 s before
            # systemd brings librespot back as an idle Connect device.
            if _spotify_preempt_restart_disabled():
                logger.warning(
                    "spotify pause: no Web API account could pause the "
                    "JTS device; escalation disabled — AirPlay and "
                    "Spotify will mix until the user pauses on phone",
                )
                return
            logger.warning(
                "spotify pause: Web API failed; escalating to "
                "`systemctl restart librespot.service` to force "
                "release of the renderer dmix",
            )
            await self._spotify_force_restart_librespot()
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
        if not client_id:
            logger.debug(
                "spotify Web API: SPOTIFY_CLIENT_ID not set; "
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
            hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
            # build_clients returns BuildResult (clients dict + per-account
            # statuses). mux only needs the clients dict — it doesn't read
            # statuses or surface revoked-vs-needs-oauth distinctions.
            result = build_clients(
                registry,
                client_id=client_id,
                redirect_uri=os.environ.get(
                    "SPOTIFY_REDIRECT_URI",
                    f"https://jaspercurry.github.io/spotify-oauth-callback/?host={hostname}",
                ),
            )
            if not result.clients:
                logger.debug("spotify Web API: no accounts authorized")
                return None
            self._spotify_router = Router(
                clients=result.clients,
                default_name=registry.default_name,
                statuses=result.statuses,
            )
            return self._spotify_router
        except Exception as e:  # noqa: BLE001
            logger.warning("spotify Web API router build failed: %s", e)
            return None

    async def _spotify_pause_via_web_api(self) -> bool:
        """Try every authorized account; pause whichever has the JTS
        device. Returns True if any account successfully paused.

        Pre-2026-05-22 this only tried devices where `is_active` was
        True. That left a real failure window: librespot can be
        emitting audio to JTS while the Web API's `is_active` flag
        still shows the previous device (the flag lags behind player
        state and is sometimes stale across multiple seconds).
        We now also try any device named JTS regardless of `is_active`
        — pause_playback will return an error if the device truly
        isn't reachable, which we swallow at debug level and continue.
        """
        router = self._ensure_spotify_router()
        if router is None:
            return False
        device_name = os.environ.get("JASPER_SPOTIFY_DEVICE_NAME", "JTS")
        # Two-pass: first prefer is_active devices (lowest-latency
        # path); fall through to any JTS-named device if that fails.
        for prefer_active in (True, False):
            for ac in router.clients.values():
                try:
                    devices = await asyncio.to_thread(ac.sp.devices)
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "spotify devices() failed for %s: %s",
                        ac.account.name, e,
                    )
                    continue
                for d in (devices.get("devices") or []):
                    if d.get("name") != device_name:
                        continue
                    if prefer_active and not d.get("is_active"):
                        continue
                    try:
                        await asyncio.to_thread(
                            ac.sp.pause_playback, device_id=d.get("id"),
                        )
                        logger.info(
                            "spotify pause via Web API: "
                            "account=%s device=%s active=%s",
                            ac.account.name, d.get("id"),
                            d.get("is_active"),
                        )
                        return True
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "spotify pause failed for %s: %s",
                            ac.account.name, e,
                        )
                        continue
        return False

    async def _spotify_force_restart_librespot(self) -> bool:
        """Tier 2 escalation: restart librespot.service to force it
        to drop its FD on the renderer dmix.

        Effects observed at the dmix layer: librespot exits, the dmix
        client count drops, the dmix slave (hw:Loopback,0,0) is
        released to the next writer. systemd respawns librespot in
        ~2-3 s (Restart=always); during that gap, the new winner
        (AirPlay / Bluetooth) is heard alone. After respawn, librespot
        is back as an idle Spotify Connect device — the credential
        cache (--system-cache /var/cache/librespot) persists, so the
        user's phone re-sees JTS in the Connect picker without
        re-authenticating. The catch: any state inside librespot's
        current session (track position, queue) is lost — the next
        Spotify Connect cast picks up fresh.

        We use `systemctl restart` rather than `kill -TERM` so the
        same `Restart=always` policy that handles every other
        librespot exit also handles this one — no special-case
        recovery path.

        Returns True on `systemctl restart` exit code 0. Logged but
        not retried on failure (the only thing that would happen on
        retry is more log spam — the failure mode is "systemctl
        unavailable" which doesn't self-heal).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "restart", "librespot.service",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=5.0,
            )
        except (FileNotFoundError, asyncio.TimeoutError) as e:
            logger.warning(
                "spotify force-restart: systemctl invocation failed: %s", e,
            )
            return False
        if proc.returncode != 0:
            logger.warning(
                "spotify force-restart: systemctl exit=%d stderr=%r",
                proc.returncode, stderr[:200],
            )
            return False
        logger.info(
            "spotify force-restart: librespot.service restarted "
            "(Tier 2 escalation succeeded)",
        )
        return True


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
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
