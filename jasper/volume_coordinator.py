"""Source-aware volume coordinator.

The user perceives "speaker volume" as a single number 0-100. Underneath,
several attenuators sit on the audio chain:

    track_loudness × airplay_sender_vol × spotify_connect_vol
        × bt_avrcp_vol × camilla_main_volume → DAC

Most of these are upstream of CamillaDSP. If the iPhone slider is at 30%,
moving CamillaDSP's main_volume between 0% and 100% only spans the
remaining 70% of perceived loudness — and feels disconnected from the
"set volume to 80%" voice command that triggered it.

This module owns the coordination. There is one canonical
`listening_level` (0-100), persisted in /var/lib/jasper/speaker_volume.json.
Outbound commands (voice tool, dial, "louder") apply this level to the
current source's reliable volume surface. Spotify and Bluetooth are
push-mode: their own protocol sliders carry `listening_level` and
CamillaDSP stays at 0 dB. Idle and AirPlay are camilla-as-master:
CamillaDSP `main_volume` carries `listening_level`. Spotify and
Bluetooth inbound observations update the canonical level in real time;
AirPlay sender volume is treated as upstream trim and ignored.

Echo prevention. Every outbound write timestamps itself per source.
When an inbound observer sees that source within `ECHO_WINDOW_SEC`
(500 ms), it is treated as our own echo and ignored. This guards both
matching PropertiesChanged echoes and a short stale-read window where a
poll lands before the protocol surface has caught up with our write.

This file is the dispatch layer (Phase 1, "Paradigm B"). The
inbound observers live in `volume_observers.py` and are started by
voice_daemon at boot. The control daemon imports this module too —
both daemons read/write listening_level via the same persistence
file, so dial-driven changes converge with voice-driven changes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .volume_persistence import (
    VolumePersistence,
    percent_to_db,
    regress_listening_level_if_stale,
)

if TYPE_CHECKING:
    # Avoid loading camilladsp/dbus modules at unit-test time. The
    # coordinator is duck-typed against CamillaController and
    # RendererClient; the real Pi-side imports happen in voice_daemon
    # and jasper-control.
    from .camilla import CamillaController
    from .renderer import RendererClient

logger = logging.getLogger(__name__)


class Source(str, Enum):
    AIRPLAY = "airplay"
    SPOTIFY = "spotify"
    BLUETOOTH = "bluetooth"
    IDLE = "idle"  # nothing active → camilla main_volume drives output


# Source-unit mappings. Pure functions: clamp to [0, 100] first, then
# convert. These are 1:1 inverses of the corresponding _from_X helpers.

# AirPlay's volume range is -30..0 dB (shairport-sync RemoteControl).
# -144 is reserved as "muted" — we use 0% → -30 dB (effective silence)
# rather than the special mute value, since our coordinator owns mute
# state separately.
AIRPLAY_DB_MIN = -30.0
AIRPLAY_DB_MAX = 0.0


def listening_level_to_airplay_db(level: int) -> float:
    p = max(0, min(100, int(level)))
    return AIRPLAY_DB_MIN + (AIRPLAY_DB_MAX - AIRPLAY_DB_MIN) * p / 100.0


def airplay_db_to_listening_level(db: float) -> int:
    span = AIRPLAY_DB_MAX - AIRPLAY_DB_MIN
    p = (float(db) - AIRPLAY_DB_MIN) / span * 100.0
    return max(0, min(100, round(p)))


def listening_level_to_spotify_percent(level: int) -> int:
    return max(0, min(100, int(level)))


def spotify_percent_to_listening_level(pct: int) -> int:
    return max(0, min(100, int(pct)))


# Bluetooth's MediaTransport1.Volume is uint16 0..127 (AVRCP 1.6
# absolute-volume scale).
BT_VOLUME_MAX = 127


def listening_level_to_bt_volume(level: int) -> int:
    p = max(0, min(100, int(level)))
    return round(p * BT_VOLUME_MAX / 100.0)


def bt_volume_to_listening_level(vol: int) -> int:
    v = max(0, min(BT_VOLUME_MAX, int(vol)))
    return round(v * 100.0 / BT_VOLUME_MAX)


# Window during which an observed source-side change is treated as
# the echo of our own write and ignored. Long enough that DBus
# round-trip + bus latency on a busy Pi 5 is well within it; short
# enough that a real user-touched slider movement that happens to
# land just after our write isn't swallowed.
ECHO_WINDOW_SEC = 0.5
PERSISTENCE_ECHO_WINDOW_SEC = 2.0


@dataclass
class _OutboundStamp:
    """Per-source last-outbound timestamp + the value we wrote."""
    at_mono: float
    level: int


class VolumeCoordinator:
    """Owns the canonical listening_level and dispatches changes to
    the right attenuator based on which source is currently active.

    The coordinator does NOT cache active_renderers() across calls —
    RendererClient.active_renderers() is itself fast (<100 ms typical)
    and re-querying on each volume command keeps "I just hit pause"
    transitions correct.

    Instances are async-first. Sync callers (control daemon HTTP
    handlers) wrap with asyncio.run(...). That spins a fresh event
    loop per request — fine at dial-tick rate (~10/s peak), and
    avoids cross-daemon coordination of a shared loop.

    Observers are optional and started via `start_observers()` —
    voice_daemon does that; control daemon doesn't need them
    (it doesn't react to inbound source changes).
    """

    def __init__(
        self,
        *,
        camilla: "CamillaController",
        persistence: VolumePersistence,
        backend: "RendererClient",
        spotify_router: Any | None = None,
        spotify_device_name: str = "JTS",
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._camilla = camilla
        self._persistence = persistence
        self._backend = backend
        # Multi-account Spotify router for Web API volume control.
        # librespot 0.8.0 has no local HTTP control API, so to set
        # Spotify volume we go: coordinator → spotipy → Spotify
        # cloud → spirc → librespot. Optional; if None or empty,
        # _set_spotify is a no-op (logged as warning).
        self._spotify_router = spotify_router
        self._spotify_device_name = spotify_device_name
        # HTTP client kept for future source-side dispatchers that
        # might need it; current paths (DBus + spotipy) don't.
        self._http = http_client or httpx.AsyncClient(timeout=2.0)
        self._owns_http = http_client is None

        # Canonical level. Loaded from persistence by initialize();
        # before that, defaults to 50 (mid-scale, hearing-safe).
        self._level: int = 50
        # Mute state. None = not muted; int = pre-mute level to
        # restore on unmute.
        self._pre_mute_level: int | None = None
        # Echo-prevention timestamps, per source.
        self._last_outbound: dict[Source, _OutboundStamp] = {}
        # One lock for all level mutations — coordinator is async-
        # single-threaded but multiple consumers (voice tool, dial
        # via UDS, observer) can race.
        self._lock = asyncio.Lock()

        # Observer tasks (populated by start_observers). None when
        # observers aren't running.
        self._observer_tasks: list[asyncio.Task] = []

        # Voice-session gate: while True, the source-transition
        # handler is suppressed because the ducker has temporary
        # control of camilla. Set/cleared by voice_daemon's WakeLoop
        # via `note_voice_session(True/False)`.
        self._voice_session_active: bool = False

    # ------------------------------------------------------------------
    # Public API — read state
    # ------------------------------------------------------------------

    def get_listening_level(self) -> int:
        """Current canonical listening level (0-100). Reads in-memory
        cache; observers and writers keep it in sync with persistence."""
        return self._level

    def load_persisted_level(self) -> int:
        """Re-read state from disk into the in-memory cache. Used by
        sync callers (jasper-control HTTP handlers) that build a fresh
        coordinator per request — they want the current canonical
        level and mute state, not the constructor defaults.

        Refreshes both listening_level and pre_mute_level so a click
        on the volume-knob's mute button can correctly detect prior
        mute state set by an earlier click that ran in a different
        coordinator instance. Returns the loaded level."""
        self._refresh_from_disk()
        return self._level

    def is_muted(self) -> bool:
        return self._pre_mute_level is not None

    # ------------------------------------------------------------------
    # Public API — initialize / boot
    # ------------------------------------------------------------------

    async def initialize(
        self,
        *,
        stale_after_sec: float = 1800.0,
        safe_low_pct: int = 20,
        safe_high_pct: int = 70,
        first_boot_default_pct: int = 50,
    ) -> tuple[int, str]:
        """Read persistence, compute the boot listening_level (with
        idle-reset / safety regression), apply it. Returns the
        (target_level, reason) for logging.

        Apply-side: if a source is already active when we boot (rare —
        usually voice_daemon starts before any music), we still write
        through the dispatch path. If idle, we set camilla main_volume.

        Boot-time persistence does NOT bump last_used_at — that field
        tracks when the user (or an observed source slider) last
        touched volume. Bumping it on every restart would mask
        truly-stale levels and defeat the idle-reset.
        """
        record = self._persistence.load()
        target_level, reason = regress_listening_level_if_stale(
            record,
            stale_after_sec=stale_after_sec,
            safe_low_pct=safe_low_pct,
            safe_high_pct=safe_high_pct,
            first_boot_default_pct=first_boot_default_pct,
        )
        async with self._lock:
            self._level = target_level
            source = await self._active_source()
            # Make camilla consistent with the boot mode. Idle and
            # AirPlay use camilla as the remembered/audible volume;
            # Spotify and Bluetooth carry listening_level on their own
            # protocol surfaces.
            if await self._camilla_carries_level(source):
                await self._set_camilla(target_level)
            else:
                await self._camilla.set_volume_db(0.0, best_effort=True)
                self._persistence.save_now(0.0)
                logger.info(
                    "boot: %s already active (push-mode); camilla "
                    "pinned at 0 dB",
                    source.value,
                )
                await self._dispatch(
                    target_level, persist=False, user_change=False,
                )
            # Mute state is per-session — clear any persisted pre_mute
            # at boot so a power-cycle wakes us in the unmuted state.
            self._pre_mute_level = None
            self._persistence.save_pre_mute_level(None)
            self._persistence.save_listening_level(
                target_level, mark_user_change=False,
            )
        return target_level, reason

    # ------------------------------------------------------------------
    # Public API — set / adjust
    # ------------------------------------------------------------------

    async def set_listening_level(self, percent: int) -> int:
        """Set canonical listening_level to `percent` (clamped to 0..100).
        Dispatches to the active source (or camilla, if idle).
        Persists. Returns the level that was actually applied."""
        target = max(0, min(100, int(percent)))
        async with self._lock:
            self._refresh_from_disk()
            self._level = target
            self._pre_mute_level = None  # any explicit set clears mute state
            self._persistence.save_pre_mute_level(None)
            await self._dispatch(target, persist=True)
        return target

    async def adjust_listening_level(self, delta: int) -> int:
        """Bump current level by `delta` (positive = louder), clamped.
        Returns the new level. Refreshes the in-memory level from disk
        first so a recent dial/HTTP write from another process is
        visible — without this, voice "louder" right after a dial
        click would compute from a stale baseline."""
        async with self._lock:
            self._refresh_from_disk()
            target = max(0, min(100, self._level + int(delta)))
            self._level = target
            self._pre_mute_level = None
            self._persistence.save_pre_mute_level(None)
            await self._dispatch(target, persist=True)
        return target

    async def mute(self) -> int:
        """Silence the speaker. Saves pre-mute level for unmute.
        Returns the saved (pre-mute) level. Persisted so a later
        unmute on a different coordinator instance (jasper-control
        builds one per HTTP request) can still see it."""
        async with self._lock:
            self._refresh_from_disk()
            if self._pre_mute_level is None and self._level > 0:
                self._pre_mute_level = self._level
            saved = self._pre_mute_level or 0
            self._level = 0
            self._persistence.save_pre_mute_level(self._pre_mute_level)
            await self._dispatch(0, persist=False)
            return saved

    async def unmute(self, fallback_level: int = 50) -> int:
        """Restore pre-mute level (or fallback if no prior mute).
        Returns the restored level."""
        async with self._lock:
            self._refresh_from_disk()
            target = self._pre_mute_level if self._pre_mute_level is not None else fallback_level
            target = max(0, min(100, int(target)))
            self._pre_mute_level = None
            self._persistence.save_pre_mute_level(None)
            self._level = target
            await self._dispatch(target, persist=True)
            return target

    def _refresh_from_disk(self) -> None:
        """Sync in-memory state with the persistence file. Cheap (~1ms
        sync read of a small JSON file) and called on every public
        operation so cross-process writes (jasper-control via dial)
        don't leave voice_daemon's coordinator with stale state.

        Refreshes both `listening_level` and `pre_mute_level` — the
        latter so an unmute call on a per-request coordinator can see
        a prior mute() that ran in a different coordinator instance."""
        record = self._persistence.load()
        if record is None:
            return
        if record.listening_level is not None:
            self._level = int(record.listening_level)
        self._pre_mute_level = record.pre_mute_level

    # ------------------------------------------------------------------
    # Observer hook — called by inbound DBus/HTTP observers when they
    # see a source-side volume change. Updates listening_level if the
    # change isn't an echo of our own outbound write.
    # ------------------------------------------------------------------

    async def observe_source_volume(
        self, source: Source, native_value: float | int,
    ) -> None:
        """Inbound observer entrypoint. `native_value` is in the
        source's own units (dB for AirPlay, percent for Spotify,
        uint16 for BT). The coordinator converts and updates the
        canonical level if this isn't an echo.

        AirPlay is deliberately excluded. Modern AirPlay 2 senders
        expose inbound sender-side volume to shairport-sync, but
        receiver-originated volume reflection via shairport's DACP/DBus
        path is no longer reliable. JTS therefore treats AirPlay sender
        volume as upstream trim and keeps the JTS canonical volume on
        camilla for AirPlay sessions.
        """
        if source == Source.AIRPLAY:
            logger.debug(
                "observe airplay: ignoring sender-side %.1f dB "
                "(AirPlay uses camilla-as-master)",
                float(native_value),
            )
            return
        elif source == Source.SPOTIFY:
            level = spotify_percent_to_listening_level(int(native_value))
        elif source == Source.BLUETOOTH:
            level = bt_volume_to_listening_level(int(native_value))
        else:
            logger.debug("observe_source_volume: unknown source %s", source)
            return
        active = await self._active_source()
        if active != source:
            logger.debug(
                "observe %s: ignoring %d%% because active source is %s",
                source.value, level, active.value,
            )
            return
        if self._is_own_echo(source, level):
            logger.debug(
                "observe %s: %d%% within echo window — ignoring (own write)",
                source.value, level,
            )
            return
        async with self._lock:
            if self._is_recent_cross_process_write(level):
                self._refresh_from_disk()
                logger.debug(
                    "observe %s: %d%% within persistence echo window — "
                    "ignoring (recent external write)",
                    source.value, level,
                )
                return
            if level == self._level:
                return  # no-op; nothing to update
            logger.info(
                "observe %s: user-side change %d%% → %d%%",
                source.value, self._level, level,
            )
            self._level = level
            self._pre_mute_level = None
            self._persistence.save_pre_mute_level(None)
            self._persistence.save_listening_level(level)

    # ------------------------------------------------------------------
    # Internal dispatch — picks the right source and pushes
    # ------------------------------------------------------------------

    async def _dispatch(
        self, level: int, *, persist: bool, user_change: bool = True,
    ) -> None:
        """Push `level` to the active source (or camilla if idle)
        and (optionally) persist. Caller holds the lock.

        `user_change` is forwarded to `save_listening_level` —
        determines whether `last_used_at` is bumped. Default True
        for set/adjust/observe paths; False for boot-time restore.

        Camilla is not touched for push-mode sources (Spotify/BT). The
        ducker (jasper.camilla.Ducker) operates on camilla via additive
        delta around voice sessions; if the coordinator also muscled
        camilla mid-dispatch for a push-mode source, the ducker's
        restore would overshoot by the duck delta. AirPlay is the
        explicit exception: shairport-sync cannot reliably reflect
        receiver-originated AirPlay 2 volume back to iOS/macOS, so JTS
        uses camilla as the AirPlay speaker-volume surface.
        """
        source = await self._active_source()
        try:
            if source == Source.AIRPLAY:
                await self._set_airplay(level)
            elif source == Source.SPOTIFY:
                await self._set_spotify(level)
            elif source == Source.BLUETOOTH:
                await self._set_bluetooth(level)
            else:
                # Idle: camilla IS the volume control.
                await self._set_camilla(level)
        finally:
            if persist:
                self._persistence.save_listening_level(
                    level, mark_user_change=user_change,
                )

    async def apply_active_source_transition(
        self, prev_source: Source, current_source: Source,
    ) -> None:
        """Called by the observer when active_renderers reports a
        source-state change. Single point that touches camilla
        across the boundary, driven by `_camilla_carries_level`:

        - camilla-master → push-mode (AirPlay/idle → Spotify/BT):
          pin camilla to 0 dB and push the remembered listening_level
          to the new renderer.
        - push-mode → camilla-master (Spotify/BT → AirPlay/idle):
          hand camilla back the current listening_level.
        - push → push (e.g. Spotify → BT): camilla already at 0 dB;
          just enforce listening_level on the new source.
        - camilla-master → camilla-master (idle ↔ AirPlay): no
          volume handoff is needed; camilla already carries the level.

        We DON'T fire this mid-voice-session (ducked state — would
        race with Ducker.restore's additive math). The ducker hooks
        in via `note_voice_session` so this method can short-circuit.
        """
        if self._voice_session_active:
            logger.debug(
                "active_source transition %s→%s: deferred (voice "
                "session in progress)",
                prev_source.value, current_source.value,
            )
            return
        if prev_source == current_source:
            return
        prev_carries = await self._camilla_carries_level(prev_source)
        curr_carries = await self._camilla_carries_level(current_source)
        async with self._lock:
            # Pull the latest listening_level from disk before
            # dispatching. The control daemon (dial / HTTP) writes
            # the same file on every twist, but voice_daemon's in-
            # memory cache only re-syncs on its own set/adjust/mute
            # calls. Without this refresh, a dial twist that lands
            # between voice operations would be silently ignored
            # when the next source-state transition fires.
            self._refresh_from_disk()
            if prev_carries and not curr_carries:
                # Camilla-master → push-mode renderer. Clear the
                # residual camilla attenuation that carried our
                # remembered volume, then push the level to the new
                # source's slider.
                await self._camilla.set_volume_db(0.0, best_effort=True)
                self._persistence.save_now(0.0)
                logger.info(
                    "active source: %s → %s; camilla pinned at 0 dB, "
                    "pushing %d%% to source slider",
                    prev_source.value, current_source.value, self._level,
                )
                await self._dispatch(
                    self._level, persist=False, user_change=False,
                )
            elif curr_carries and not prev_carries:
                # Push-mode renderer → camilla-master. Hand
                # listening_level back to camilla so the remembered
                # level stays audible.
                target_db = percent_to_db(self._level)
                await self._camilla.set_volume_db(target_db, best_effort=True)
                self._persistence.save_now(target_db)
                logger.info(
                    "active source: %s → %s; camilla → %.1f dB (%d%%)",
                    prev_source.value, current_source.value,
                    target_db, self._level,
                )
            elif not curr_carries:
                # Push → push (e.g. spotify → bt). Camilla already at
                # 0 dB; just enforce listening_level on the new source.
                logger.info(
                    "active source: %s → %s (push→push); pushing %d%% "
                    "to new source slider",
                    prev_source.value, current_source.value, self._level,
                )
                await self._dispatch(
                    self._level, persist=False, user_change=False,
                )
            else:
                # Idle ↔ AirPlay: both are camilla-master modes, so
                # camilla already carries listening_level.
                logger.debug(
                    "active source: %s → %s (no camilla change)",
                    prev_source.value, current_source.value,
                )

    def note_voice_session(self, active: bool) -> None:
        """Called by voice_daemon's WakeLoop on session start/end.
        While a session is active, this coordinator suppresses its
        own writes to camilla — the Ducker has exclusive control,
        and `Ducker.restore()` reads back the canonical target via
        `get_camilla_target_db()` to land at the right value
        regardless of interleaved listening_level changes during
        the duck. Affected paths: `apply_active_source_transition`
        (no-ops mid-session) and `_set_camilla` (defers the camilla
        write; listening_level still persists)."""
        self._voice_session_active = bool(active)

    async def get_camilla_target_db(self) -> float:
        """The absolute camilla.main_volume that should be in effect
        right now, ignoring any active duck. Used by `Ducker.restore()`
        to land camilla at the canonical level regardless of what the
        duck delta was or what other writers did during the session.

        Refreshes from disk before reading `_level`. jasper-control
        and jasper-voice are separate processes that each cache
        listening_level in memory; without a refresh here, a dial
        twist that lands between this daemon's own set/adjust calls
        leaves `_level` stale. Real symptom: dial spun to 100% via
        the control daemon, voice-daemon's stale `_level` still
        reflected the boot value, and `Ducker.restore()` after a
        failed turn raised camilla by tens of dB to satisfy the
        out-of-date target. (Or, in the inverse case, dropped it
        below the user's intent.)"""
        self._refresh_from_disk()
        source = await self._active_source()
        if await self._camilla_carries_level(source):
            return percent_to_db(self._level)
        # Push-mode renderer: camilla is pinned at 0 dB; the source's
        # own slider carries listening_level.
        return 0.0

    async def _active_source(self) -> Source:
        """Pick the active source. Multiple-source-active is rare
        (mux preempts in <1 s) but possible during transitions; pick
        a stable priority: airplay > spotify > bluetooth > idle.
        """
        try:
            active = await self._backend.active_renderers()
        except Exception as e:  # noqa: BLE001
            logger.debug("active_renderers() failed (%s); treating as idle", e)
            return Source.IDLE
        if active.get("aplactive"):
            return Source.AIRPLAY
        if active.get("spotactive"):
            return Source.SPOTIFY
        if active.get("btactive"):
            return Source.BLUETOOTH
        return Source.IDLE

    async def _camilla_carries_level(self, source: Source) -> bool:
        """Whether camilla.main_volume IS the user-facing master volume
        for `source`, vs. delegating to a downstream slider.

        True (camilla-as-master): camilla tracks listening_level. Used
        for IDLE and AIRPLAY.

        False (push-mode): camilla pinned at 0 dB and the source's own
        slider carries listening_level. Used for SPOTIFY (Web API) and
        BLUETOOTH (AVRCP).
        """
        return source in (Source.IDLE, Source.AIRPLAY)

    def _stamp_outbound(self, source: Source, level: int) -> None:
        self._last_outbound[source] = _OutboundStamp(
            at_mono=time.monotonic(), level=level,
        )

    def _is_own_echo(self, source: Source, observed_level: int) -> bool:
        stamp = self._last_outbound.get(source)
        if stamp is None:
            return False
        if time.monotonic() - stamp.at_mono > ECHO_WINDOW_SEC:
            return False
        # Within the window, ignore even a different value. Polling can
        # race the source's own state update after an outbound write,
        # especially on source handoff. If the user really changed the
        # sender slider, the next 1 Hz poll will pick up the stable
        # value outside this short window.
        return True

    def _is_recent_cross_process_write(self, observed_level: int) -> bool:
        """Suppress stale polls after another process changed volume.

        jasper-control creates its own coordinator for LAN / hardware
        knob requests, so voice_daemon's observer does not see that
        coordinator's in-memory outbound stamp. `last_used_at` is the
        durable cross-process echo stamp: if disk moved ahead of this
        coordinator very recently and the source reports a different
        level, prefer the persisted knob/HTTP/voice truth for one short
        poll window.
        """
        record = self._persistence.load()
        if (
            record is None
            or record.last_used_at is None
            or record.listening_level is None
        ):
            return False
        if int(record.listening_level) == int(observed_level):
            return False
        if int(record.listening_level) == int(self._level):
            return False
        age = (datetime.now(timezone.utc) - record.last_used_at).total_seconds()
        # VolumePersistence writes timestamps at second precision, so
        # this needs to be wider than the in-memory monotonic window.
        return 0.0 <= age <= PERSISTENCE_ECHO_WINDOW_SEC

    # ------------------------------------------------------------------
    # Source-side dispatchers
    # ------------------------------------------------------------------

    async def _set_airplay(self, level: int) -> None:
        """AirPlay is camilla-as-master.

        shairport-sync still exposes SetAirplayVolume, but modern
        iOS/macOS AirPlay 2 sessions often omit DACP-ID/Active-Remote
        and receiver-originated volume reflection silently no-ops.
        The reliable product contract is therefore audible speaker
        volume via CamillaDSP, while the sender slider remains upstream
        trim.
        """
        await self._set_camilla(level)

    async def _set_spotify(self, level: int) -> None:
        """Set Spotify volume via Spotify Web API.

        librespot 0.8.0 has no local control HTTP — to change Spotify's
        volume we go through Spotify's cloud, which propagates back to
        librespot via spirc AND updates every Spotify client UI (your
        phone slider visibly moves). Latency ~200-800ms typical.

        We try every authorized account until one successfully claims
        the JTS device. On failure (no router configured, no account
        has the JTS device active, or all accounts return errors),
        log and no-op."""
        pct = listening_level_to_spotify_percent(level)
        if self._spotify_router is None or not getattr(
            self._spotify_router, "clients", {},
        ):
            logger.warning(
                "spotify volume set: no Web API router configured; "
                "voice/dial volume can't propagate to Spotify (set "
                "SPOTIFY_CLIENT_ID/SECRET and authorize at least one "
                "account via /spotify)",
            )
            return
        for ac in self._spotify_router.clients.values():
            try:
                devices = await asyncio.to_thread(ac.sp.devices)
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "spotify devices() failed for %s: %s",
                    ac.account.name, e,
                )
                continue
            for d in (devices.get("devices") or []):
                if d.get("name") == self._spotify_device_name:
                    try:
                        await asyncio.to_thread(
                            ac.sp.volume, pct, device_id=d.get("id"),
                        )
                        self._stamp_outbound(Source.SPOTIFY, level)
                        logger.info(
                            "spotify volume set: %d%% (account=%s)",
                            pct, ac.account.name,
                        )
                        return
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "spotify volume() failed for %s: %s",
                            ac.account.name, e,
                        )
                        continue
        logger.warning(
            "spotify volume set FAILED: %d%% — no account could write "
            "to device '%s' (is JTS still selected in Spotify?)",
            pct, self._spotify_device_name,
        )

    async def _set_bluetooth(self, level: int) -> None:
        vol = listening_level_to_bt_volume(level)
        # bluez-alsa exposes one MediaTransport1 path per active
        # transport; we have to find it before we can set the
        # property. Empty list = no active BT transport (caller
        # invoked us during a brief BT-active window that closed).
        path = await _bluez_alsa_active_transport_path()
        if path is None:
            logger.debug(
                "bluetooth volume set: no active transport, skipping",
            )
            return
        ok = await _busctl_set_property(
            "org.bluealsa", path,
            "org.bluez.MediaTransport1",
            "Volume",
            "q",
            str(vol),
            bus="--system",
        )
        if ok:
            self._stamp_outbound(Source.BLUETOOTH, level)
            logger.info("bluetooth volume set: %d%% (uint16=%d)", level, vol)
        else:
            logger.warning(
                "bluetooth volume set FAILED: %d%% (uint16=%d)", level, vol,
            )

    async def _set_camilla(self, level: int) -> None:
        db = percent_to_db(level)
        if self._voice_session_active:
            # Voice session in progress — Ducker owns camilla. Writing
            # here would either be clobbered by Ducker.restore (absolute
            # write) or, worse, get the duck delta added on top.
            # listening_level is still updated in self._level by the
            # caller and persisted by _dispatch's save_listening_level,
            # so the user's intent survives; Ducker.restore reads it
            # via get_camilla_target_db() and lands camilla there.
            # main_volume_db is intentionally NOT saved here — it'd
            # diverge from camilla's actual state until restore.
            logger.info(
                "camilla main_volume deferred to ducker.restore: "
                "%d%% (%.1f dB) — voice session active",
                level, db,
            )
            return
        # best_effort: dial twist arriving during a 2s camilla restart
        # blip should still update listening_level on disk and persist
        # main_volume_db, even if the actual write didn't land. The
        # next set_volume call (or a source-transition) will re-apply
        # once camilla is back.
        await self._camilla.set_volume_db(db, best_effort=True)
        # main_volume IS what the user is controlling in idle. Persist
        # it explicitly so the legacy regress_if_stale path still has
        # the right value if some future restart goes through it.
        self._persistence.save_now(db)
        # No echo prevention for camilla — there's no observer for
        # main_volume changes (no source generates them externally
        # while idle).
        logger.info("camilla main_volume set: %d%% (%.1f dB)", level, db)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        for t in self._observer_tasks:
            t.cancel()
        for t in self._observer_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._observer_tasks = []
        if self._owns_http:
            await self._http.aclose()


# ----------------------------------------------------------------------
# DBus helpers — mirror jasper.renderer's busctl wrappers, extended for
# property setters. Subprocess+busctl is the proven pattern in this
# codebase; observers in volume_observers.py use dbus-next for live
# subscriptions, but one-shot Set ops stay subprocess.
# ----------------------------------------------------------------------

async def _busctl_set_property(
    bus_name: str,
    object_path: str,
    interface: str,
    prop: str,
    signature: str,
    value: str,
    *,
    bus: str = "--system",
) -> bool:
    """Run `busctl set-property` for one property. Returns True on
    success, False on any error (logged at debug)."""
    try:
        # `--` before the typed value so busctl's getopt doesn't
        # parse leading-`-` values as flag options.
        proc = await asyncio.create_subprocess_exec(
            "busctl", bus, "set-property",
            bus_name, object_path, interface, prop, signature, "--", value,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("busctl set-property %s.%s failed: %s", interface, prop, e)
        return False
    if proc.returncode != 0:
        logger.debug(
            "busctl set-property %s.%s rc=%d stderr=%s",
            interface, prop, proc.returncode,
            stderr.decode("utf-8", "replace") if stderr else "",
        )
        return False
    return True


async def _busctl_call_method(
    bus_name: str,
    object_path: str,
    interface: str,
    method: str,
    signature: str,
    value: str,
    *,
    bus: str = "--system",
) -> bool:
    """Run `busctl call` for one method with one typed value.

    Returns True on success, False on any error. The `--` before the
    value keeps negative dB values from being parsed as busctl flags.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", bus, "call",
            bus_name, object_path, interface, method, signature, "--", value,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("busctl call %s.%s failed: %s", interface, method, e)
        return False
    if proc.returncode != 0:
        logger.debug(
            "busctl call %s.%s rc=%d stderr=%s",
            interface, method, proc.returncode,
            stderr.decode("utf-8", "replace") if stderr else "",
        )
        return False
    return True


_BLUEZ_TRANSPORT_PATH_RE = re.compile(
    rb"(/org/bluealsa/hci\d+/dev_[A-F0-9_]+/a2dpsnk/source)"
)


async def _bluez_alsa_active_transport_path() -> str | None:
    """Find an active A2DP-sink MediaTransport1 path via bluealsa-cli.

    bluealsa-cli list-pcms outputs lines like
        /org/bluealsa/hci0/dev_XX_../a2dpsnk/source PCM ...
    We grab the first matching path. Returns None if no transport
    is active (BT phone disconnected, or connected but not playing).
    """
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
