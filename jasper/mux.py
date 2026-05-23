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
  USB sink (jasper-usbsink):
    detect: read /run/jasper-usbsink/state.json (RMS-based
            playing flag, hysteresis-debounced, written by the
            daemon's state publisher)
    pause:  POST {"silenced": true} to
            http://127.0.0.1:JASPER_USBSINK_PREEMPT_PORT/preempt.
            The daemon silences its output (writes zeros to
            hw:Loopback,0,0). When all other sources go idle, we
            release the preempt so user-host transitions (pause
            then play on Mac) can re-take the speaker.

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
from .source_state import (
    airplay_playing,
    bluetooth_playing,
    spotify_playing,
    usbsink_playing,
)

logger = logging.getLogger(__name__)


class Source(str, Enum):
    SPOTIFY = "spotify"
    AIRPLAY = "airplay"
    BLUETOOTH = "bluetooth"
    # USBSINK comes last in the enum so its iteration order matches
    # the rest of the file. The enum order is also the tie-break for
    # multi-source-active-on-boot — last-defined wins. For USB
    # specifically this is reasonable: USB requires deliberate
    # hardware action (plug a cable in), so if both AirPlay and USB
    # somehow appear simultaneously on boot, USB taking the speaker
    # matches "the thing that was just plugged in".
    USBSINK = "usbsink"


# Host:port the usbsink daemon's preempt endpoint listens on. Keep
# in sync with jasper.usbsink.preempt_listener.DEFAULT_PORT — both
# are defaults the operator can override via env var. We duplicate
# the literal here (instead of importing) so jasper-mux doesn't pull
# the usbsink package into its dep graph (mux loads even on Pis
# where the gadget feature is off — RAM-bounded service).
USBSINK_PREEMPT_HOST = os.environ.get(
    "JASPER_USBSINK_PREEMPT_HOST", "127.0.0.1",
)
USBSINK_PREEMPT_PORT = int(os.environ.get(
    "JASPER_USBSINK_PREEMPT_PORT", "8781",
))
USBSINK_PREEMPT_URL = f"http://{USBSINK_PREEMPT_HOST}:{USBSINK_PREEMPT_PORT}/preempt"


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


def _usbsink_preempt_disabled() -> bool:
    """Env-var escape hatch for the USB-sink preempt mechanism.

    Set JASPER_USBSINK_PREEMPT=disabled in /etc/jasper/jasper.env to
    short-circuit `_usbsink_set_preempt` — mux no longer tells the
    daemon to silence its output when another source wins. USB then
    behaves like Bluetooth (no graceful pause API; audio briefly mixes
    when a new source starts). Operator escape hatch for cases where
    the localhost HTTP POST is causing unexpected disruption, without
    requiring a redeploy or daemon restart. Default: enabled.

    Mirrors JASPER_AIRPLAY_METADATA_GATE / JASPER_MUX_SPOTIFY_PREEMPT_RESTART
    / JASPER_SHAIRPORT_SUPERVISOR.
    """
    return os.environ.get(
        "JASPER_USBSINK_PREEMPT", "",
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
        # USB sink preempt state: True while we've told the
        # jasper-usbsink daemon to silence its output. Cleared when
        # all other sources go idle (so a host pause-then-resume can
        # re-take the speaker via a fresh inactive→active transition).
        self._usbsink_preempted = False
        # Short-lived httpx client for the localhost preempt POSTs.
        # The url is fixed; reusing the client across POSTs avoids
        # one socket-setup per tick when preempt is changing rapidly.
        self._http = httpx.AsyncClient(timeout=2.0)

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
        spotify, airplay, bluetooth, usbsink = await asyncio.gather(
            spotify_playing(self._librespot_state_path),
            airplay_playing(),
            bluetooth_playing(),
            usbsink_playing(),
        )
        current = {
            Source.SPOTIFY: spotify,
            Source.AIRPLAY: airplay,
            Source.BLUETOOTH: bluetooth,
            Source.USBSINK: usbsink,
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

        if newly_started:
            new_winner = newly_started[-1]
            prev_winner = self._winner
            logger.info(
                "source transition: %s started (was %s, age=%d ticks)",
                new_winner.value,
                prev_winner.value if prev_winner else "none",
                self._winner_age_ticks,
            )

            # If the new winner is USBSINK and it's currently in our
            # preempted set, the daemon's bridge is silent. The fresh
            # inactive→active edge means the user did "pause then
            # play" on the host — release the preempt so we forward
            # audio again.
            if new_winner == Source.USBSINK and self._usbsink_preempted:
                await self._usbsink_set_preempt(False, reason="new_transition")

            # Pause every OTHER source that's currently active. Note
            # we iterate over `current` not `newly_started` — if
            # Spotify started while AirPlay was already playing for
            # 30 s, we want to pause AirPlay even though it didn't
            # transition this tick.
            for source, is_playing in current.items():
                if source != new_winner and is_playing:
                    await self._pause(source)

            self._winner = new_winner
            self._winner_age_ticks = 0

        # Release USB preempt when all other sources have gone idle.
        # Without this, the daemon would stay silent indefinitely
        # after AirPlay/Spotify stop, even though the user might
        # still be playing on the host. The check excludes USBSINK
        # itself — its `playing` flag stays True (RMS-active) even
        # while preempted, so we look at the OTHER sources to decide.
        if self._usbsink_preempted:
            others_playing = any(
                playing
                for src, playing in current.items()
                if src != Source.USBSINK
            )
            if not others_playing:
                await self._usbsink_set_preempt(
                    False, reason="all_others_idle",
                )

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
        elif source == Source.USBSINK:
            await self._usbsink_set_preempt(True, reason="preempted_by_winner")


    # ------------------------------------------------------------------
    # USB sink preempt protocol — POSTs to the daemon's local HTTP
    # endpoint. The daemon flips its internal `preempted` flag,
    # making its audio callback emit silence into hw:Loopback,0,0.
    # ------------------------------------------------------------------

    async def _usbsink_set_preempt(self, silenced: bool, *, reason: str) -> None:
        """Tell the daemon to silence/un-silence its output. No-ops
        if the requested state matches our tracked state, so a tick
        that re-emits the same decision doesn't generate stale POSTs.

        Failure is logged but not fatal — the worst case is brief
        mixing on preempt (matches the existing BT fallback). The
        daemon's `preempt_listener` itself persists the most-recent
        state to /run/jasper-usbsink/preempt.state, so a future
        daemon restart picks up where it left off."""
        if self._usbsink_preempted == silenced:
            return
        if _usbsink_preempt_disabled():
            # Escape hatch active. Log once per state change so the
            # operator sees the preempt being skipped without spam.
            logger.info(
                "event=usbsink.preempt_skipped silenced=%s reason=%s "
                "via=JASPER_USBSINK_PREEMPT=disabled",
                silenced, reason,
            )
            self._usbsink_preempted = silenced
            return
        try:
            resp = await self._http.post(
                USBSINK_PREEMPT_URL,
                json={"silenced": silenced},
            )
            if resp.status_code == 200:
                self._usbsink_preempted = silenced
                logger.info(
                    "event=usbsink.preempt_set silenced=%s reason=%s",
                    silenced, reason,
                )
                return
            logger.warning(
                "usbsink preempt POST returned %d (silenced=%s); "
                "audio may briefly mix",
                resp.status_code, silenced,
            )
        except httpx.HTTPError as e:
            # Daemon not running? Likely cause: /sources/ wizard
            # turned USB sink off but didn't tell mux. The state file
            # probe will return playing=false on the next tick once
            # the daemon's RuntimeDirectory= cleans up, so we'll
            # converge.
            logger.warning(
                "usbsink preempt POST failed (silenced=%s reason=%s): %s",
                silenced, reason, e,
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
