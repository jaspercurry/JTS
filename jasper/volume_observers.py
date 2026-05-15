"""Inbound source-volume observers for the volume coordinator.

When the user drags the Spotify app's slider or hits the BT volume
buttons on their phone, the corresponding receiver-side daemon sees the
change immediately. We poll those daemons at 1 Hz so the coordinator's
canonical `listening_level` reflects user-side movements without
requiring the user to also tell Jarvis.

AirPlay is intentionally different. shairport-sync can observe
sender-side AirPlay volume, but cannot reliably reflect receiver-side
volume changes back to modern iOS/macOS AirPlay 2 senders. JTS treats
AirPlay sender volume as upstream trim and keeps AirPlay speaker volume
on CamillaDSP, so AirPlay observations are read only for diagnostics and
are not fed into the canonical volume.

Why polling (not DBus PropertiesChanged subscriptions). The codebase
already uses `busctl` subprocess for DBus one-shot calls (renderer.py,
mux.py). A live subscription would require a different DBus library
(dbus-next). For our use case the ergonomic wins of subscriptions
don't materialize: source-side volume changes happen at finger-touch
speed (1 Hz polling captures everything), and a polling loop is
simpler to reason about — one well-placed sleep, one error path per
source, no long-lived subscription state to manage.

Cadence: 1 Hz, mirroring jasper-mux's source-state poll. Each tick
fans out to three concurrent state probes (Spotify HTTP, AirPlay
busctl, Bluetooth busctl); the whole tick is well under 100 ms typical.

Echo prevention. The coordinator tracks the timestamp of every
outbound write per source. When an observer reports a value it just
wrote (within ECHO_WINDOW_SEC), the coordinator ignores it as its
own echo. So pushing 50% to Spotify → polling sees 50% on next tick →
ignored; pushing 50% then user touches slider to 30% → polling sees
30% outside the window → propagated.

"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx

from . import librespot_state
from .volume_coordinator import (
    AIRPLAY_DB_MAX,
    AIRPLAY_DB_MIN,
    Source,
    VolumeCoordinator,
)

logger = logging.getLogger(__name__)


class VolumeObserver:
    """Polls each source's current volume at a fixed cadence and
    feeds detected changes into the coordinator. One instance covers
    all three sources; per-source last-seen state makes change
    detection cheap.

    The observer runs as one asyncio task and is started/stopped via
    voice_daemon's lifecycle. Cancelling the task is the documented
    shutdown path."""

    POLL_INTERVAL_SEC = 1.0

    def __init__(
        self,
        coordinator: VolumeCoordinator,
        *,
        librespot_state_path: str = librespot_state.DEFAULT_PATH,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._coord = coordinator
        self._librespot_state_path = librespot_state_path
        # HTTP client is no longer used for Spotify (librespot has no
        # control HTTP API), but kept around in case future observers
        # need it (e.g. an MPRIS-over-HTTP probe).
        self._http = http_client or httpx.AsyncClient(timeout=2.0)
        self._owns_http = http_client is None
        # Last value seen per source (in source-native units), so we
        # only fire `observe_source_volume` on actual change. None
        # means "haven't observed this source yet" → first observed
        # value will sync the coordinator to the source's current
        # level (correct behavior for source-just-became-active).
        self._last_seen: dict[Source, Optional[float]] = {
            Source.AIRPLAY: None,
            Source.SPOTIFY: None,
            Source.BLUETOOTH: None,
        }
        # Last observed active_source (idle / airplay / spotify / bt).
        # When this changes we fire the coordinator's transition
        # handler, which manages camilla across the boundary so
        # idle⇄source-active doesn't leave camilla compounding with
        # the source's slider.
        self._last_active_source: Source | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        logger.info(
            "volume observer started (poll=%.1fs)", self.POLL_INTERVAL_SEC,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
        if self._owns_http:
            await self._http.aclose()

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("volume observer tick failed: %s", e)
            try:
                await asyncio.sleep(self.POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        # Active-source change detection. Fires the coordinator's
        # transition handler when a source comes online or goes
        # offline, so camilla stays consistent with the boundary.
        try:
            current_active = await self._coord._active_source()
        except Exception as e:  # noqa: BLE001
            logger.debug("active_source query failed: %s", e)
            current_active = None
        if (
            current_active is not None
            and self._last_active_source is not None
            and current_active != self._last_active_source
        ):
            await self._coord.apply_active_source_transition(
                self._last_active_source, current_active,
            )
        if current_active is not None:
            self._last_active_source = current_active

        airplay_db, spotify_pct, bt_vol = await asyncio.gather(
            self._read_airplay_db(),
            self._read_spotify_percent(),
            self._read_bluetooth_volume(),
            return_exceptions=False,
        )
        if current_active == Source.AIRPLAY and airplay_db is not None:
            self._last_seen[Source.AIRPLAY] = airplay_db
            logger.debug(
                "airplay sender volume observed at %.1f dB "
                "(ignored; AirPlay uses camilla-as-master)",
                airplay_db,
            )
        if current_active == Source.SPOTIFY and spotify_pct is not None:
            await self._maybe_observe(Source.SPOTIFY, float(spotify_pct))
        if current_active == Source.BLUETOOTH and bt_vol is not None:
            await self._maybe_observe(Source.BLUETOOTH, float(bt_vol))

    async def _maybe_observe(self, source: Source, value: float) -> None:
        last = self._last_seen[source]
        # First observation per source DOES propagate. Each source
        # owns its own remembered volume (Spotify cloud restores
        # per-account; macOS restores per-AirPlay-device; phone
        # restores per-BT-device), so the source's reality on first
        # contact is the right value for listening_level to reflect.
        # Subsequent observations propagate only on a real change
        # (>0.5 unit delta) — AirPlay's dB is fractional and can
        # jitter; we don't want polling churn.
        if last is None or abs(value - last) > 0.5:
            self._last_seen[source] = value
            await self._coord.observe_source_volume(source, value)

    # ------------------------------------------------------------------
    # Per-source readers — each returns None on "source not active /
    # not reachable" rather than raising, so a missing daemon doesn't
    # crash the observer.
    # ------------------------------------------------------------------

    async def _read_airplay_db(self) -> Optional[float]:
        """Read shairport-sync's current AirplayVolume (double dB).
        Returns None on any error. shairport reports -144 when muted —
        the coordinator clamps that to 0% via its airplay_db_to_listening_level.
        """
        out = await _busctl_get_property_value(
            "org.gnome.ShairportSync",
            "/org/gnome/ShairportSync",
            "org.gnome.ShairportSync.RemoteControl",
            "AirplayVolume",
        )
        if out is None:
            return None
        # busctl Get returns "v d <number>" — parse the trailing number.
        m = re.search(r"-?\d+(?:\.\d+)?", out)
        if not m:
            return None
        try:
            db = float(m.group(0))
        except ValueError:
            return None
        # Clamp -144 (mute sentinel) up to AIRPLAY_DB_MIN. Anything
        # outside the documented range is suspicious and we'd rather
        # ignore than feed garbage into the coordinator.
        if db < -150 or db > AIRPLAY_DB_MAX + 1:
            return None
        return max(AIRPLAY_DB_MIN, min(AIRPLAY_DB_MAX, db))

    async def _read_spotify_percent(self) -> Optional[int]:
        """Read librespot's current volume from the state file written
        by the --onevent hook. Returns None when librespot has no
        active session (no recent volume event) or when the file
        doesn't exist yet."""
        return librespot_state.volume_percent(self._librespot_state_path)

    async def _read_bluetooth_volume(self) -> Optional[int]:
        """Read the active A2DP transport's MediaTransport1.Volume
        (uint16 0..127). Returns None on no active transport or any
        DBus error."""
        path = await _bluez_alsa_active_transport_path()
        if path is None:
            return None
        out = await _busctl_get_property_value(
            "org.bluealsa", path,
            "org.bluez.MediaTransport1", "Volume",
            bus="--system",
        )
        if out is None:
            return None
        # busctl Get returns "v q <number>" for uint16.
        m = re.search(r"\d+", out)
        if not m:
            return None
        try:
            v = int(m.group(0))
        except ValueError:
            return None
        return max(0, min(127, v))


# ----------------------------------------------------------------------
# DBus helpers — re-used from coordinator's pattern. Subprocess+busctl
# is the proven low-dep approach in this codebase.
# ----------------------------------------------------------------------

async def _busctl_get_property_value(
    bus_name: str,
    object_path: str,
    interface: str,
    prop: str,
    *,
    bus: str = "--system",
) -> Optional[str]:
    """Run `busctl get-property` and return the raw stdout, or None
    on any error. Caller parses the typed-variant value."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", bus, "get-property",
            bus_name, object_path, interface, prop,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("busctl get-property %s.%s failed: %s", interface, prop, e)
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", "replace").strip()


_BLUEZ_TRANSPORT_PATH_RE = re.compile(
    rb"(/org/bluealsa/hci\d+/dev_[A-F0-9_]+/a2dpsnk/source)"
)


async def _bluez_alsa_active_transport_path() -> Optional[str]:
    """Find an active A2DP-sink transport via bluealsa-cli. Returns
    None when no BT transport is open."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluealsa-cli", "list-pcms",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("bluealsa-cli list-pcms failed: %s", e)
        return None
    m = _BLUEZ_TRANSPORT_PATH_RE.search(stdout)
    return m.group(1).decode("ascii") if m else None
