# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Inbound source-volume observers for the volume coordinator.

When the user drags the Spotify app's slider or hits the BT volume
buttons on their phone, the corresponding receiver-side daemon sees the
change immediately. We poll those daemons at 1 Hz so the coordinator's
canonical `listening_level` reflects user-side movements without
requiring the user to also tell Jarvis.

AirPlay is intentionally different: its reading is diagnostics-only, never
dispatched (see `_read_airplay_db`), and receiver→sender reflection stays
impossible on AirPlay 2 (ADR-0176), so JTS keeps AirPlay speaker volume on
CamillaDSP.

Why polling (not DBus PropertiesChanged subscriptions). The codebase
already uses `busctl` subprocess for DBus one-shot calls (renderer.py,
mux.py). A live subscription would require a different DBus library
(dbus-next). For our use case the ergonomic wins of subscriptions
don't materialize: source-side volume changes happen at finger-touch
speed (1 Hz polling captures everything), and a polling loop is
simpler to reason about — one well-placed sleep, one error path per
source, no long-lived subscription state to manage.

Cadence: 1 Hz, mirroring jasper-mux's source-state poll. A tick probes
only the source the coordinator reports active — every other reading
was discarded, and each probe but Spotify's forks a subprocess — so an
idle box runs none. USB sink is the exception even when active: its
daemon observes the host-side gadget mixer directly and posts
`source="usbsink"` changes to jasper-control, so this observer never
polls it.

Echo prevention belongs to the coordinator: an observation matching a
value it wrote within ECHO_WINDOW_SEC is ignored as its own echo.

"""
from __future__ import annotations

import asyncio
import logging
import re
from functools import partial
from typing import Optional

from . import librespot_state
from .bluealsa_probe import active_transport_path
from .busctl import run_busctl
from .log_event import log_event
from .volume_coordinator import (
    AIRPLAY_DB_MAX,
    AIRPLAY_DB_MIN,
    Source,
    VolumeCoordinator,
)

logger = logging.getLogger(__name__)
_bluez_alsa_active_transport_path = partial(active_transport_path, logger)


class VolumeObserver:
    """Polls the active source's current volume at a fixed cadence and
    feeds detected changes into the coordinator. One instance covers
    the built-in protocol-volume surfaces (AirPlay, Spotify,
    Bluetooth); USB sink's host-volume observer lives in
    `jasper.usbsink.volume_bridge`. Per-source last-seen state makes
    change detection cheap.

    The observer runs as one asyncio task and is started/stopped via
    voice_daemon's lifecycle. Cancelling the task is the documented
    shutdown path."""

    POLL_INTERVAL_SEC = 1.0

    def __init__(
        self,
        coordinator: VolumeCoordinator,
        *,
        librespot_state_path: str = librespot_state.DEFAULT_PATH,
    ) -> None:
        self._coord = coordinator
        self._librespot_state_path = librespot_state_path
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
        # Renderer values are not unique transition identities: two rapid
        # mutes can both present zero. Track the coordinator-owned revision
        # alongside the native value so a new mute token is observed even
        # when the renderer number did not change.
        self._last_seen_revision: dict[Source, str | None] = {
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

    async def _run(self) -> None:
        # Reported on its edges only: at 1 Hz a per-tick line is 3,600 an
        # hour, and a held fault says nothing the first one did not.
        consecutive_failures = 0
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if not consecutive_failures:
                    log_event(logger, "volume.observer_tick_failed",
                              level=logging.WARNING,
                              error=f"{type(e).__name__}: {e}")
                consecutive_failures += 1
            else:
                if consecutive_failures:
                    log_event(logger, "volume.observer_tick_recovered",
                              consecutive_failures=consecutive_failures)
                    consecutive_failures = 0
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
        if current_active is not None and current_active != self._last_active_source:
            if self._last_active_source is not None:
                await self._coord.apply_active_source_transition(
                    self._last_active_source, current_active,
                )
            if current_active in self._last_seen:
                # A source becoming audible is a fresh confirmation
                # point even if its protocol volume equals the last
                # cached value from an older session. Forward one
                # observation so push-mode guards can self-heal.
                self._last_seen[current_active] = None
                self._last_seen_revision[current_active] = None
            self._last_active_source = current_active

        # Exactly one probe, the active source's: the other readings were
        # discarded and each but Spotify's forks a busctl/bluealsa-cli child.
        if current_active == Source.AIRPLAY:
            airplay_db = await self._read_airplay_db()
            if airplay_db is not None:
                self._last_seen[Source.AIRPLAY] = airplay_db
                logger.debug(
                    "airplay sender volume observed at %.1f dB", airplay_db,
                )
        elif current_active == Source.SPOTIFY:
            spotify_pct = await self._read_spotify_percent()
            if spotify_pct is not None:
                await self._maybe_observe(Source.SPOTIFY, float(spotify_pct))
        elif current_active == Source.BLUETOOTH:
            bt_vol = await self._read_bluetooth_volume()
            if bt_vol is not None:
                await self._maybe_observe(Source.BLUETOOTH, float(bt_vol))

        # Self-healing convergence backstop; internally gated and idempotent.
        # See `VolumeCoordinator.maybe_reconcile_camilla` for the gates.
        try:
            await self._coord.maybe_reconcile_camilla()
        except Exception as e:  # noqa: BLE001
            # Should never raise (the method swallows internally),
            # but never let a reconcile bug bring down the observer
            # — observation is the more important responsibility.
            logger.warning("reconciler raised %s; skipping", e)

    async def _maybe_observe(self, source: Source, value: float) -> None:
        last = self._last_seen[source]
        revision = self._coord.source_observation_revision(source)
        last_revision = self._last_seen_revision[source]
        # First observation per source DOES propagate. Each source
        # owns its own remembered volume (Spotify cloud restores
        # per-account; macOS restores per-AirPlay-device; phone
        # restores per-BT-device), so the source's reality on first
        # contact is the right value for listening_level to reflect.
        # Subsequent observations propagate only on a real change
        # (>0.5 unit delta) — AirPlay's dB is fractional and can
        # jitter; we don't want polling churn.
        if (
            last is None
            or abs(value - last) > 0.5
            or revision != last_revision
        ):
            accepted = await self._coord.observe_source_volume(
                source,
                value,
                initial=last is None,
            )
            # A declined observation has not crossed the coordinator's
            # source/mute/echo policy. Do not cache it as truth: retry on the
            # next bounded poll even if the renderer value remains unchanged.
            if accepted:
                self._last_seen[source] = value
                self._last_seen_revision[source] = revision

    # ------------------------------------------------------------------
    # Per-source readers — each returns None on "source not active /
    # not reachable" rather than raising, so a missing daemon doesn't
    # crash the observer.
    # ------------------------------------------------------------------

    async def _read_airplay_db(self) -> Optional[float]:
        """Read shairport-sync's current AirplayVolume (double dB).

        Diagnostics only — this reading is logged, never dispatched. The
        canonical inbound path is shairport's own volume hook
        (deploy/bin/jasper-airplay-volume, ADR-0206), which is
        event-driven rather than polled and owns the dB→percent map.
        Returns None on any error; -144 (shairport's mute sentinel) is
        clamped up to AIRPLAY_DB_MIN so the log line stays in-range.
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
# DBus helpers — protocol parsing stays local; subprocess lifecycle is shared.
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
    result = await run_busctl(
        "get-property",
        bus_name, object_path, interface, prop,
        bus=bus,
    )
    if result is None:
        logger.debug("busctl get-property %s.%s failed", interface, prop)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()
