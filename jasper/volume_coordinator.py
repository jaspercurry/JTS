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
Outbound commands (voice tool, dial, "louder") push this level to
whichever source is currently active; inbound observations (iPhone
slider movement, Spotify app slider, BT slider) update the canonical
level in real time. CamillaDSP main_volume is pinned at 0 dB while a
source is active (no double-attenuation) and used directly as the
volume control when idle (no renderer producing audio).

Echo prevention. Every outbound write timestamps itself per source.
When an inbound observer sees a change for that source within
`ECHO_WINDOW_SEC` (500 ms), it is treated as our own echo and ignored.
This is the standard "PropertiesChanged-on-our-own-write" loop guard.

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


@dataclass
class _OutboundStamp:
    """Per-source last-outbound timestamp + the value we wrote.
    Observers cross-check both fields: a change of magnitude > epsilon
    OR an arrival outside the echo window means it's a real user input."""
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
        """Re-read listening_level from disk into the in-memory cache.
        Used by sync callers (control daemon HTTP handlers) that build
        a fresh coordinator per request — they want the current
        canonical level, not the constructor default. Returns the
        loaded level."""
        record = self._persistence.load()
        if record is not None and record.listening_level is not None:
            self._level = int(record.listening_level)
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
            # Make camilla consistent with the boot mode (see
            # `_camilla_carries_level`): camilla-as-master → camilla
            # tracks listening_level; push-mode → camilla pinned at
            # 0 dB and source carries the level.
            if await self._camilla_carries_level(source):
                await self._set_camilla(target_level)
                if source != Source.IDLE:
                    logger.info(
                        "boot: %s active (camilla-as-master); "
                        "camilla → %d%%",
                        source.value, target_level,
                    )
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
            await self._dispatch(target, persist=True)
        return target

    async def mute(self) -> int:
        """Silence the speaker. Saves pre-mute level for unmute.
        Returns the saved (pre-mute) level."""
        async with self._lock:
            self._refresh_from_disk()
            if self._pre_mute_level is None and self._level > 0:
                self._pre_mute_level = self._level
            saved = self._pre_mute_level or 0
            self._level = 0
            await self._dispatch(0, persist=False)
            return saved

    async def unmute(self, fallback_level: int = 50) -> int:
        """Restore pre-mute level (or fallback if no prior mute).
        Returns the restored level."""
        async with self._lock:
            target = self._pre_mute_level if self._pre_mute_level is not None else fallback_level
            target = max(0, min(100, int(target)))
            self._pre_mute_level = None
            self._level = target
            await self._dispatch(target, persist=True)
            return target

    def _refresh_from_disk(self) -> None:
        """Sync in-memory _level with the persistence file. Cheap (~1ms
        sync read of a small JSON file) and called on every public
        operation so cross-process writes (jasper-control via dial)
        don't leave voice_daemon's coordinator with stale state."""
        record = self._persistence.load()
        if record is not None and record.listening_level is not None:
            self._level = int(record.listening_level)

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
        canonical level if this isn't an echo."""
        if source == Source.AIRPLAY:
            # AirPlay is always camilla-as-master in this codebase;
            # the sender's slider isn't the user's master-volume
            # intent. Honoring it would bounce listening_level around
            # with whatever the phone/Mac is showing (often pre-
            # attenuated and disconnected from what the speaker is
            # actually doing). Always skip.
            logger.debug(
                "observe airplay: ignoring sender slider "
                "(camilla is master via dial)",
            )
            return
        if source == Source.SPOTIFY:
            level = spotify_percent_to_listening_level(int(native_value))
        elif source == Source.BLUETOOTH:
            level = bt_volume_to_listening_level(int(native_value))
        else:
            logger.debug("observe_source_volume: unknown source %s", source)
            return
        if self._is_own_echo(source, level):
            logger.debug(
                "observe %s: %d%% within echo window — ignoring (own write)",
                source.value, level,
            )
            return
        async with self._lock:
            if level == self._level:
                return  # no-op; nothing to update
            logger.info(
                "observe %s: user-side change %d%% → %d%%",
                source.value, self._level, level,
            )
            self._level = level
            self._pre_mute_level = None
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

        Camilla is NOT touched in source-active mode. The ducker
        (jasper.camilla.Ducker) operates on camilla via additive
        delta around voice sessions; if the coordinator also
        muscled camilla mid-dispatch, the ducker's restore would
        overshoot by the duck delta — exactly the bug that put
        camilla at +15 dB after a voice command during AirPlay
        playback. Camilla coordination across the idle⇄source
        boundary happens in `apply_active_source_transition`,
        called by the observer when active_source changes.
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

        - camilla-as-master → push-mode (e.g. AirPlay → Spotify):
          clear the residual camilla attenuation that was carrying
          our master volume, and push level to the new source.
        - push-mode → camilla-as-master (e.g. Spotify → AirPlay):
          hand camilla back the current listening_level so the dial
          keeps doing real work.
        - push → push (e.g. spotify → bt): camilla already at 0 dB;
          just enforce listening_level on the new source.
        - camilla → camilla (idle ↔ AirPlay): no change.

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
                # Camilla-as-master → push-mode. Clear the residual
                # camilla attenuation that was carrying our master
                # volume, and push the level to the new source's
                # slider. This is the AirPlay → Spotify edge.
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
                # Push-mode → camilla-as-master. Hand listening_level
                # back to camilla so the dial keeps doing real work.
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
                # Camilla → camilla (idle ↔ AirPlay). No change.
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
        # Push mode (Spotify, BT): camilla is pinned at 0 dB; the
        # source's own slider carries listening_level.
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
        for IDLE (nothing playing) and AIRPLAY (always — see below).

        False (push-mode): camilla pinned at 0 dB and the source's own
        slider carries listening_level. Used for SPOTIFY (Web API push)
        and BLUETOOTH (AVRCP).

        Why AirPlay is unconditionally camilla-as-master: empirically,
        Apple's AirPlay 2 receivers (iOS 17+ and macOS Sequoia) accept
        shairport's SetAirplayVolume DBus call but silently no-op the
        sender slider. The DACP `Available` flag isn't a reliable
        predictor — it flips true and false during a session, and even
        when true the Set still no-ops on Apple senders. Camilla
        (downstream of shairport's receiver in the audio chain) always
        works, so we attenuate there.

        Trade: the iPhone/Mac AirPlay slider on the sender doesn't
        visibly move when the dial turns. Audio at the speaker does.
        Recommended pairing: leave the sender slider at 100% so audio
        isn't pre-attenuated upstream of camilla.
        """
        if source == Source.IDLE:
            return True
        if source == Source.AIRPLAY:
            return True
        return False

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
        # Within the window: treat as echo if the observed value
        # matches what we wrote (allow ±1% rounding slack across the
        # source-unit conversions).
        return abs(observed_level - stamp.level) <= 1

    # ------------------------------------------------------------------
    # Source-side dispatchers
    # ------------------------------------------------------------------

    async def _set_airplay(self, level: int) -> None:
        # AirPlay is always camilla-as-master in this codebase — see
        # `_camilla_carries_level` for the why. We don't even try
        # SetAirplayVolume; Apple's AirPlay 2 receivers accept the
        # call and silently no-op the sender slider, regardless of
        # what DACP `Available` reports. Camilla (downstream of
        # shairport's receiver in the audio chain) always works.
        # We deliberately don't _stamp_outbound(AIRPLAY) — we didn't
        # write to AirPlay, and observe_source_volume always skips
        # AirPlay observations in this model.
        logger.info("airplay → camilla as master for %d%%", level)
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
