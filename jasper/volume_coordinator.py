# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
CamillaDSP normally stays at 0 dB. At 0%, CamillaDSP also asserts
`main_mute` so content/music zero is a final-output mute rather than
source-side attenuation. Idle, AirPlay, and USB sink are camilla-as-master:
CamillaDSP `main_volume` carries `listening_level`. Spotify,
Bluetooth, and USB sink inbound observations update the canonical level
in real time; AirPlay sender volume is treated as upstream trim and
ignored.

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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from .log_event import log_event
from .music_sources import Source, VolumeMode, volume_mode
from . import bluealsa_probe
from . import volume_diagnostics
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


# Type alias for the cross-daemon duck-active probe. Returns True iff a
# Ducker is currently holding camilla.main_volume below the canonical
# listening_level target; False or None means safe to write camilla
# directly. None is the "unknown / probe failed" fallback — `_set_camilla`
# treats it as False (fail-open), so a wedged jasper-voice never freezes
# the dial. See docs/HANDOFF-volume.md "Cross-daemon defer signal".
DuckActiveProbe = Callable[[], Awaitable[Optional[bool]]]


# Reconciler thresholds. `maybe_reconcile_camilla` is the self-healing
# backstop in `VolumeObserver._tick`: when no session is active and the
# active source is camilla-as-master, it converges `main_volume_db`
# toward `percent_to_db(listening_level)` if they've drifted apart.
#
# `RECONCILE_DRIFT_DB` is the dead band — drift smaller than this is
# ignored (covers camilla's <0.1 dB jitter with safe margin). Below
# human-noticeable, well above any normal jitter.
#
# `RECONCILE_DUCK_SKIP_DB` is directional. CueDuck plays proactive cues
# without setting `_voice_session_active`, so the reconciler can race a
# CueDuck (which lowers camilla by JASPER_DUCK_DB, typically 25 dB).
# If Camilla is much QUIETER than expected, skip to avoid un-ducking.
# If Camilla is much LOUDER than expected, always correct it; that is
# exactly the safety case the reconciler exists to catch.
RECONCILE_DRIFT_DB = 1.0
RECONCILE_DUCK_SKIP_DB = 10.0
MUTE_DB_EPSILON = 1e-6


@dataclass
class _OutboundStamp:
    """Per-source last-outbound timestamp + the value we wrote."""
    at_mono: float
    level: int


@dataclass(frozen=True)
class SourceHandoff:
    """Preparation result for a mux-owned source transition."""
    prev_source: Source
    current_source: Source
    reason: str
    level: int
    prev_mode: VolumeMode
    current_mode: VolumeMode
    guard_db: float | None = None
    camilla_before_db: float | None = None
    push_ok: bool | None = None
    camilla_guarded: bool = False
    settled_ms: int = 0
    result: str = "ok"
    detail: str = ""
    started_at_mono: float = 0.0
    prepared_at_mono: float = 0.0

    @property
    def ok(self) -> bool:
        return self.result in {"ok", "degraded_safe", "noop"}


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

    Inbound observers are separate ``VolumeObserver`` instances owned by
    the voice daemon. The coordinator does not create or own observer tasks;
    short-lived control-daemon instances therefore need no observer cleanup.
    """

    def __init__(
        self,
        *,
        camilla: "CamillaController",
        persistence: VolumePersistence,
        backend: "RendererClient",
        spotify_router: Any | None = None,
        spotify_device_name: str = "JTS",
        duck_active_probe: DuckActiveProbe | None = None,
        handoff_settle_sec: float = 0.45,
        push_settle_sec: float = 0.75,
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

        # Voice-session gate: while True, the source-transition
        # handler is suppressed because the ducker has temporary
        # control of camilla. Set/cleared by voice_daemon's WakeLoop
        # via `note_voice_session(True/False)`. Only meaningful on
        # the long-lived coordinator owned by jasper-voice; per-
        # request coordinators in jasper-control always read False
        # and rely on `_duck_active_probe` instead.
        self._voice_session_active: bool = False
        # Correction-measurement gate for the voice daemon's own 1 Hz
        # reconciler. This is intentionally narrow: it does not turn this
        # process-local flag into a cross-daemon Camilla lock or block an
        # emergency user mute. It prevents the observed writer from replacing
        # a ramp value with persisted listening_level mid-measurement.
        self._measurement_active: bool = False
        # Serializes the final reconciler write with MEASURE_PAUSE acquisition.
        # Pause does not acknowledge until an already-started write has landed;
        # after the flag flips, no new reconcile write may enter this lock.
        self._reconcile_write_lock = asyncio.Lock()
        # Cross-daemon duck-active signal. jasper-control's per-
        # request coordinators set this to a UDS-probing callable
        # that asks jasper-voice's `session_status` whether the
        # Ducker is currently engaged. jasper-voice's own coordinator
        # leaves it None — `_voice_session_active` is the in-process
        # signal there. See docs/HANDOFF-volume.md "Cross-daemon
        # defer signal".
        self._duck_active_probe: DuckActiveProbe | None = duck_active_probe
        # CamillaDSP's default main-volume ramp is 400 ms. Mux source
        # handoff waits slightly beyond that after lowering camilla
        # before exposing a camilla-master lane.
        self._handoff_settle_sec = max(0.0, float(handoff_settle_sec))
        # Spotify/AVRCP volume writes can acknowledge before the
        # renderer has audibly applied the new attenuator. After mux
        # opens a push-mode lane, keep the old Camilla guard in place
        # briefly before clearing to 0 dB.
        self._push_settle_sec = max(0.0, float(push_settle_sec))

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
            # protocol surfaces. Push-mode 0% is the exception: still
            # assert Camilla main_mute as the content/music mute guarantee.
            if await self._camilla_carries_level(source):
                await self._set_camilla(target_level)
            else:
                pin_db = 0.0 if target_level > 0 else percent_to_db(0)
                await self._set_camilla_db(
                    pin_db,
                    context="boot_push_pin",
                    persist=True,
                )
                logger.info(
                    "boot: %s already active (push-mode); camilla "
                    "pinned at %.1f dB",
                    source.value, pin_db,
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
        elif source == Source.USBSINK:
            # USB gadget volume_bridge POSTs percent directly (already
            # normalized 0-100 from the gadget mixer's raw range).
            # Map identity to listening_level. The translation work
            # happens client-side in jasper.usbsink.volume_bridge so
            # the coordinator doesn't need to know about ALSA mixer
            # units or the gadget's range.
            level = max(0, min(100, int(native_value)))
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
                if await self._camilla_carries_level(source):
                    await self._sync_camilla_observed_level(source, level)
                else:
                    await self._confirm_push_mode_carrier(
                        source,
                        level,
                        context=f"observe_{source.value}_push_confirmed",
                        include_live_guard=True,
                    )
                return  # no-op; nothing to update
            logger.info(
                "observe %s: user-side change %d%% → %d%%",
                source.value, self._level, level,
            )
            self._level = level
            self._pre_mute_level = None
            self._persistence.save_pre_mute_level(None)
            self._persistence.save_listening_level(level)
            if await self._camilla_carries_level(source):
                await self._sync_camilla_observed_level(source, level)
            else:
                await self._confirm_push_mode_carrier(
                    source,
                    level,
                    context=f"observe_{source.value}_push_confirmed",
                    include_live_guard=True,
                )

    async def _sync_camilla_observed_level(
        self, source: Source, level: int,
    ) -> bool:
        """Apply an observed source-side level when Camilla is the carrier.

        USB sink observes the host slider/mute switch, but the host
        mixer is not the final speaker-volume carrier; CamillaDSP is.
        So an observation must update both the canonical
        ``listening_level`` and Camilla's ``main_volume``. This is
        deliberately separate from the push-mode guard-clear path used
        by Spotify/Bluetooth.
        """
        expected_db = percent_to_db(level)
        expected_mute = self._main_mute_for_level(level)
        current_db, current_mute = await self._read_camilla_volume_and_mute()
        mute_drift = (
            current_mute is not None
            and current_mute != expected_mute
        )
        if (
            current_db is not None
            and abs(expected_db - current_db) <= RECONCILE_DRIFT_DB
            and not mute_drift
        ):
            return False
        ok = await self._set_camilla(level)
        log_event(
            logger,
            "volume.observed_carrier_sync",
            # `level` is the volume level — a field name that collides with
            # log_event's reserved level= param, so all fields ride fields=.
            fields={
                "source": source.value,
                "level": f"{level}%",
                "current_db": "unknown" if current_db is None else f"{current_db:.2f}",
                "expected_db": f"{expected_db:.2f}",
                "drift_db": "unknown" if current_db is None else f"{expected_db - current_db:+.2f}",
                "current_mute": "unknown" if current_mute is None else str(current_mute).lower(),
                "expected_mute": str(expected_mute).lower(),
                "result": "accepted" if ok else "failed",
            },
        )
        return ok

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

        Camilla volume is normally not touched for push-mode sources
        (Spotify/BT). The exceptions are 0% content mute and degraded
        safety: if the source's own volume write fails, Camilla remains
        a fallback attenuator because every renderer lane still flows
        through it. AirPlay is
        camilla-master: shairport-sync cannot reliably reflect
        receiver-originated AirPlay 2 volume back to iOS/macOS, so JTS
        uses CamillaDSP as the AirPlay speaker-volume surface.
        """
        source = await self._active_source()
        try:
            if source == Source.AIRPLAY:
                await self._set_airplay(level)
            elif source == Source.SPOTIFY:
                ok = await self._set_spotify(level)
                if ok:
                    await self._confirm_push_mode_carrier(
                        source,
                        level,
                        context="dispatch_spotify_push_confirmed",
                    )
                else:
                    guard_db = percent_to_db(level)
                    previous_db = self._persisted_main_volume_db()
                    guarded = await self._set_camilla_db(
                        guard_db,
                        context="dispatch_spotify_degraded",
                        persist=True,
                    )
                    if guarded:
                        self._record_push_guard(
                            source,
                            level,
                            guard_db,
                            reason=volume_diagnostics.GUARD_PUSH_WRITE_FAILED,
                            context="dispatch_spotify_degraded",
                            previous_db=previous_db,
                        )
                        logger.warning(
                            "spotify volume dispatch failed; camilla "
                            "guarded at %.1f dB for %d%%",
                            guard_db, level,
                        )
                    else:
                        logger.warning(
                            "spotify volume dispatch failed and camilla "
                            "guard could not be confirmed for %.1f dB",
                            guard_db,
                        )
            elif source == Source.BLUETOOTH:
                ok = await self._set_bluetooth(level)
                if ok:
                    await self._confirm_push_mode_carrier(
                        source,
                        level,
                        context="dispatch_bluetooth_push_confirmed",
                    )
                else:
                    guard_db = percent_to_db(level)
                    previous_db = self._persisted_main_volume_db()
                    guarded = await self._set_camilla_db(
                        guard_db,
                        context="dispatch_bluetooth_degraded",
                        persist=True,
                    )
                    if guarded:
                        self._record_push_guard(
                            source,
                            level,
                            guard_db,
                            reason=volume_diagnostics.GUARD_PUSH_WRITE_FAILED,
                            context="dispatch_bluetooth_degraded",
                            previous_db=previous_db,
                        )
                        logger.warning(
                            "bluetooth volume dispatch failed; camilla "
                            "guarded at %.1f dB for %d%%",
                            guard_db, level,
                        )
                    else:
                        logger.warning(
                            "bluetooth volume dispatch failed and camilla "
                            "guard could not be confirmed for %.1f dB",
                            guard_db,
                        )
            else:
                # IDLE and USBSINK both land here. USBSINK is
                # camilla-master like AirPlay; we don't write back
                # to the gadget's mixer (the host's slider is
                # observed-only — see observe_source_volume above
                # and HANDOFF-usbsink.md §3.2).
                await self._set_camilla(level)
        finally:
            if persist:
                self._persistence.save_listening_level(
                    level, mark_user_change=user_change,
                )

    async def prepare_source_handoff(
        self, prev_source: Source, current_source: Source, *, reason: str,
    ) -> SourceHandoff:
        """Prepare downstream volume before mux exposes a new fan-in lane.

        This is the synchronous safety gate used by jasper-mux. It
        enforces the invariant that a new source is not made audible
        until its volume carrier is safe for the canonical
        `listening_level`.
        """
        started = time.monotonic()
        self._refresh_from_disk()
        level = self._level
        prev_mode = volume_mode(prev_source)
        current_mode = volume_mode(current_source)
        guard_db = percent_to_db(level)
        camilla_before, camilla_before_mute = (
            await self._read_camilla_volume_and_mute()
        )

        if prev_source == current_source:
            now = time.monotonic()
            return SourceHandoff(
                prev_source=prev_source,
                current_source=current_source,
                reason=reason,
                level=level,
                prev_mode=prev_mode,
                current_mode=current_mode,
                guard_db=guard_db,
                camilla_before_db=camilla_before,
                result="noop",
                started_at_mono=started,
                prepared_at_mono=now,
            )

        if current_mode == VolumeMode.CAMILLA_MASTER:
            settled_ms = 0
            expected_mute = self._main_mute_for_level(level)
            mute_drift = (
                camilla_before_mute is not None
                and camilla_before_mute != expected_mute
            )
            needs_guard = (
                camilla_before is None
                or camilla_before > guard_db + RECONCILE_DRIFT_DB
                or mute_drift
            )
            if needs_guard:
                ok = await self._set_camilla_db(
                    guard_db,
                    context="source_handoff_guard",
                    persist=True,
                )
                if not ok:
                    now = time.monotonic()
                    return SourceHandoff(
                        prev_source=prev_source,
                        current_source=current_source,
                        reason=reason,
                        level=level,
                        prev_mode=prev_mode,
                        current_mode=current_mode,
                        guard_db=guard_db,
                        camilla_before_db=camilla_before,
                        result="failed",
                        detail="camilla_guard_failed",
                        started_at_mono=started,
                        prepared_at_mono=now,
                    )
                level, guard_db, settled_ms, ok = (
                    await self._settle_handoff_guard(
                        level, guard_db,
                        context="source_handoff_guard_catchdown",
                    )
                )
                if not ok:
                    now = time.monotonic()
                    return SourceHandoff(
                        prev_source=prev_source,
                        current_source=current_source,
                        reason=reason,
                        level=level,
                        prev_mode=prev_mode,
                        current_mode=current_mode,
                        guard_db=guard_db,
                        camilla_before_db=camilla_before,
                        camilla_guarded=True,
                        settled_ms=settled_ms,
                        result="failed",
                        detail="camilla_guard_catchdown_failed",
                        started_at_mono=started,
                        prepared_at_mono=now,
                    )
            else:
                self._refresh_from_disk()
                latest_level = self._level
                latest_guard_db = percent_to_db(latest_level)
                if latest_guard_db < guard_db - RECONCILE_DRIFT_DB:
                    ok = await self._set_camilla_db(
                        latest_guard_db,
                        context="source_handoff_guard_catchdown",
                        persist=True,
                    )
                    if not ok:
                        now = time.monotonic()
                        return SourceHandoff(
                            prev_source=prev_source,
                            current_source=current_source,
                            reason=reason,
                            level=latest_level,
                            prev_mode=prev_mode,
                            current_mode=current_mode,
                            guard_db=latest_guard_db,
                            camilla_before_db=camilla_before,
                            result="failed",
                            detail="camilla_guard_catchdown_failed",
                            started_at_mono=started,
                            prepared_at_mono=now,
                        )
                    level, guard_db, settled_ms, ok = (
                        await self._settle_handoff_guard(
                            latest_level,
                            latest_guard_db,
                            context="source_handoff_guard_catchdown",
                        )
                    )
                    if not ok:
                        now = time.monotonic()
                        return SourceHandoff(
                            prev_source=prev_source,
                            current_source=current_source,
                            reason=reason,
                            level=level,
                            prev_mode=prev_mode,
                            current_mode=current_mode,
                            guard_db=guard_db,
                            camilla_before_db=camilla_before,
                            camilla_guarded=True,
                            settled_ms=settled_ms,
                            result="failed",
                            detail="camilla_guard_catchdown_failed",
                            started_at_mono=started,
                            prepared_at_mono=now,
                        )
            now = time.monotonic()
            return SourceHandoff(
                prev_source=prev_source,
                current_source=current_source,
                reason=reason,
                level=level,
                prev_mode=prev_mode,
                current_mode=current_mode,
                guard_db=guard_db,
                camilla_before_db=camilla_before,
                camilla_guarded=True,
                settled_ms=settled_ms,
                started_at_mono=started,
                prepared_at_mono=now,
            )

        push_ok = await self._set_push_source_for_handoff(current_source, level)
        if push_ok:
            self._refresh_from_disk()
            latest_level = self._level
            if latest_level != level:
                level = latest_level
                guard_db = percent_to_db(level)
                push_ok = await self._set_push_source_for_handoff(
                    current_source, level,
                )
        if push_ok:
            now = time.monotonic()
            return SourceHandoff(
                prev_source=prev_source,
                current_source=current_source,
                reason=reason,
                level=level,
                prev_mode=prev_mode,
                current_mode=current_mode,
                guard_db=guard_db,
                camilla_before_db=camilla_before,
                push_ok=True,
                started_at_mono=started,
                prepared_at_mono=now,
            )

        ok = await self._set_camilla_db(
            guard_db,
            context="source_handoff_push_degraded_guard",
            persist=True,
        )
        if not ok:
            now = time.monotonic()
            return SourceHandoff(
                prev_source=prev_source,
                current_source=current_source,
                reason=reason,
                level=level,
                prev_mode=prev_mode,
                current_mode=current_mode,
                guard_db=guard_db,
                camilla_before_db=camilla_before,
                push_ok=False,
                result="failed",
                detail="push_failed_and_camilla_guard_failed",
                started_at_mono=started,
                prepared_at_mono=now,
            )
        level, guard_db, settled_ms, settle_ok = (
            await self._settle_handoff_guard(
                level, guard_db,
                context="source_handoff_push_degraded_catchdown",
            )
        )
        if not settle_ok:
            now = time.monotonic()
            return SourceHandoff(
                prev_source=prev_source,
                current_source=current_source,
                reason=reason,
                level=level,
                prev_mode=prev_mode,
                current_mode=current_mode,
                guard_db=guard_db,
                camilla_before_db=camilla_before,
                push_ok=False,
                camilla_guarded=True,
                settled_ms=settled_ms,
                result="failed",
                detail="push_failed_camilla_guard_catchdown_failed",
                started_at_mono=started,
                prepared_at_mono=now,
            )
        self._record_push_guard(
            current_source,
            level,
            guard_db,
            reason=volume_diagnostics.GUARD_SOURCE_HANDOFF_PUSH_FAILED,
            context="source_handoff_push_degraded",
            previous_db=camilla_before,
        )
        now = time.monotonic()
        return SourceHandoff(
            prev_source=prev_source,
            current_source=current_source,
            reason=reason,
            level=level,
            prev_mode=prev_mode,
            current_mode=current_mode,
            guard_db=guard_db,
            camilla_before_db=camilla_before,
            push_ok=False,
            camilla_guarded=True,
            settled_ms=settled_ms,
            result="degraded_safe",
            detail="push_volume_failed_camilla_guarded",
            started_at_mono=started,
            prepared_at_mono=now,
        )

    async def finalize_source_handoff(self, handoff: SourceHandoff) -> bool:
        """Finish a mux source transition after fan-in has selected a lane."""
        if not handoff.ok:
            return False
        if handoff.current_mode == VolumeMode.PUSH:
            if handoff.push_ok:
                if self._push_settle_sec > 0:
                    await asyncio.sleep(self._push_settle_sec)
                self._refresh_from_disk()
                latest_level = self._level
                final_level = latest_level
                if latest_level != handoff.level:
                    if latest_level < handoff.level:
                        guard_db = percent_to_db(latest_level)
                        guard_ok = await self._set_camilla_db(
                            guard_db,
                            context="source_handoff_push_finalize_catchdown",
                            persist=True,
                        )
                        if not guard_ok:
                            return False
                    push_ok = await self._set_push_source_for_handoff(
                        handoff.current_source, latest_level,
                    )
                    if not push_ok:
                        guard_db = percent_to_db(latest_level)
                        previous_db = self._persisted_main_volume_db()
                        guarded = await self._set_camilla_db(
                            guard_db,
                            context="source_handoff_push_finalize_degraded",
                            persist=True,
                        )
                        if guarded:
                            reason = volume_diagnostics.GUARD_SOURCE_HANDOFF_PUSH_FAILED
                            self._record_push_guard(
                                handoff.current_source,
                                latest_level,
                                guard_db,
                                reason=reason,
                                context="source_handoff_push_finalize_degraded",
                                previous_db=previous_db,
                            )
                        return guarded
                    if self._push_settle_sec > 0:
                        await asyncio.sleep(self._push_settle_sec)
                return await self._confirm_push_mode_carrier(
                    handoff.current_source,
                    final_level,
                    context="source_handoff_push_finalize",
                )
            # Keep the guard in place when the push surface failed.
            return True
        if handoff.current_mode == VolumeMode.CAMILLA_MASTER:
            # If the guard had to be quieter than the canonical level,
            # converge back to the intended level after the selected
            # lane is open. Camilla's own ramp makes this smooth.
            return await self._set_camilla(handoff.level)
        return True

    async def _settle_handoff_guard(
        self, level: int, guard_db: float, *, context: str,
    ) -> tuple[int, float, int, bool]:
        """Wait for Camilla's volume ramp and catch a lowering user edit.

        The mux must not expose a camilla-master lane while Camilla is
        still ramping down. If the user lowers the canonical level
        during that settle window, lower Camilla again and settle once
        more before allowing the handoff. If the user keeps dragging
        continuously, fail safe rather than opening the lane at a stale
        louder level.
        """
        settled_ms = 0
        adjustments = 0
        while True:
            if self._handoff_settle_sec > 0:
                await asyncio.sleep(self._handoff_settle_sec)
                settled_ms += round(self._handoff_settle_sec * 1000)
            self._refresh_from_disk()
            latest_level = self._level
            latest_guard_db = percent_to_db(latest_level)
            if latest_guard_db >= guard_db - RECONCILE_DRIFT_DB:
                return latest_level, guard_db, settled_ms, True
            if adjustments >= 3:
                logger.warning(
                    "source handoff guard could not catch lowering "
                    "listening_level after %d adjustments", adjustments,
                )
                return latest_level, guard_db, settled_ms, False
            ok = await self._set_camilla_db(
                latest_guard_db, context=context, persist=True,
            )
            if not ok:
                return latest_level, latest_guard_db, settled_ms, False
            guard_db = latest_guard_db
            adjustments += 1

    async def apply_active_source_transition(
        self, prev_source: Source, current_source: Source,
    ) -> None:
        """Called by the observer when active_renderers reports a
        source-state change. Single point that touches camilla
        across the boundary, driven by `_camilla_carries_level`:

        - camilla-master → push-mode (AirPlay/idle → Spotify/BT):
          push the remembered listening_level to the new renderer,
          then pin camilla to 0 dB only if that push succeeds.
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
                # Camilla-master → push-mode renderer. Push the new
                # source first, then clear Camilla only after the
                # source-side write succeeds. If the push fails,
                # Camilla remains the safety carrier instead of
                # exposing a stale/full-scale source.
                push_ok = await self._set_push_source_for_handoff(
                    current_source, self._level,
                )
                if push_ok:
                    carrier_ok = await self._confirm_push_mode_carrier(
                        current_source,
                        self._level,
                        context="active_source_transition_push_clear",
                    )
                    logger.info(
                        "active source: %s → %s; pushed %d%% to source "
                        "slider and confirmed camilla push-mode carrier "
                        "result=%s",
                        prev_source.value, current_source.value, self._level,
                        "accepted" if carrier_ok else "failed",
                    )
                else:
                    guard_db = percent_to_db(self._level)
                    previous_db = self._persisted_main_volume_db()
                    guarded = await self._set_camilla_db(
                        guard_db,
                        context="active_source_transition_push_degraded",
                        persist=True,
                    )
                    if guarded:
                        self._record_push_guard(
                            current_source,
                            self._level,
                            guard_db,
                            reason=volume_diagnostics.GUARD_ACTIVE_SOURCE_PUSH_FAILED,
                            context="active_source_transition_push_degraded",
                            previous_db=previous_db,
                        )
                        logger.warning(
                            "active source: %s → %s; source volume push "
                            "failed, keeping camilla guarded at %.1f dB",
                            prev_source.value, current_source.value, guard_db,
                        )
                    else:
                        logger.warning(
                            "active source: %s → %s; source volume push "
                            "failed and camilla guard could not be confirmed "
                            "for %.1f dB",
                            prev_source.value, current_source.value, guard_db,
                        )
            elif curr_carries and not prev_carries:
                # Push-mode renderer → camilla-master. Hand
                # listening_level back to camilla so the remembered
                # level stays audible.
                ok = await self._set_camilla(self._level)
                logger.info(
                    "active source: %s → %s; camilla → %.1f dB (%d%%) "
                    "result=%s",
                    prev_source.value, current_source.value,
                    percent_to_db(self._level), self._level,
                    "accepted" if ok else "failed",
                )
            elif not curr_carries:
                # Push → push (e.g. spotify → bt). Camilla already at
                # 0 dB; enforce listening_level on the new source. If
                # the push fails, fall back to Camilla as a safety
                # carrier because all renderer lanes still flow
                # through Camilla.
                push_ok = await self._set_push_source_for_handoff(
                    current_source, self._level,
                )
                if push_ok:
                    await self._confirm_push_mode_carrier(
                        current_source,
                        self._level,
                        context="active_source_transition_push_push_confirmed",
                    )
                    logger.info(
                        "active source: %s → %s (push→push); pushed "
                        "%d%% to new source slider",
                        prev_source.value, current_source.value, self._level,
                    )
                else:
                    guard_db = percent_to_db(self._level)
                    previous_db = self._persisted_main_volume_db()
                    guarded = await self._set_camilla_db(
                        guard_db,
                        context="active_source_transition_push_push_degraded",
                        persist=True,
                    )
                    if guarded:
                        self._record_push_guard(
                            current_source,
                            self._level,
                            guard_db,
                            reason=volume_diagnostics.GUARD_ACTIVE_SOURCE_PUSH_FAILED,
                            context="active_source_transition_push_push_degraded",
                            previous_db=previous_db,
                        )
                        logger.warning(
                            "active source: %s → %s (push→push); source "
                            "volume push failed, camilla guarded at %.1f dB",
                            prev_source.value, current_source.value, guard_db,
                        )
                    else:
                        logger.warning(
                            "active source: %s → %s (push→push); source "
                            "volume push failed and camilla guard could not "
                            "be confirmed for %.1f dB",
                            prev_source.value, current_source.value, guard_db,
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

    async def note_measurement_active(self, active: bool) -> None:
        """Pause/resume this process's 1 Hz Camilla drift reconciler."""
        async with self._reconcile_write_lock:
            self._measurement_active = bool(active)

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
        if self._main_mute_for_level(self._level):
            return percent_to_db(0)
        # Push-mode sources normally run with Camilla pinned at 0 dB.
        # 0% content mute and failed handoffs are deliberate exceptions:
        # if the user asked for zero, preserve the mute floor; if we could not
        # push the source's own volume, mux leaves Camilla at a guarded
        # attenuation and records that in persistence. Preserve that
        # guard through Ducker.restore instead of unmasking a source we
        # already know might be too loud.
        record = self._persistence.load()
        if (
            record is not None
            and record.main_volume_db < -RECONCILE_DRIFT_DB
        ):
            return record.main_volume_db
        # Push-mode renderer: camilla is pinned at 0 dB; the source's
        # own slider carries listening_level.
        return 0.0

    async def maybe_reconcile_camilla(self) -> None:
        """Self-healing convergence: if `main_volume_db` has drifted
        from `percent_to_db(listening_level)` while no session is
        active, write the expected value back to camilla.

        Pure resilience backstop. The coordinator's normal write paths
        keep the two in sync; this only catches edge cases where some
        other writer or transient (camilla restart blip, room
        correction reverting, future code paths) leaves them
        divergent. Called from `VolumeObserver._tick` at 1 Hz.

        Gates (all must pass for a write to land):

        1. No voice session or correction measurement is active. The Ducker
           owns camilla during a voice session; the ramp owns it during a
           measurement. Reconciling would clobber either transient owner.
        2. Active source is camilla-as-master (idle / AirPlay /
           USBSINK). For push-mode sources (Spotify / Bluetooth)
           camilla is pinned at 0 dB by design and listening_level
           lives on the source's own slider; reconciling there would
           fight `apply_active_source_transition`.
        3. `|main_volume_db − expected| > RECONCILE_DRIFT_DB` — dead
           band around camilla's normal jitter so we don't write on
           every tick.
        4. Deep QUIET drift is skipped (`expected - current >=
           RECONCILE_DUCK_SKIP_DB`) because CueDuck can lower camilla
           without `_voice_session_active`. Deep LOUD drift is always
           corrected; a writer that left camilla far above the
           canonical level is unsafe, not a duck.

        Emits `event=volume.reconciled` on every write so drift
        is visible in journalctl. Failures are logged at WARN and
        non-fatal — the observer keeps ticking.
        """
        if self._voice_session_active or self._measurement_active:
            return
        try:
            source = await self._active_source()
        except Exception:  # noqa: BLE001
            return
        if not await self._camilla_carries_level(source):
            return
        # Refresh from disk so a dial twist that landed via
        # jasper-control between our own set/adjust calls reflects
        # in `_level` before we compute the expected dB.
        self._refresh_from_disk()
        expected_level = 0 if self._pre_mute_level is not None else self._level
        expected_db = percent_to_db(expected_level)
        expected_mute = self._main_mute_for_level(expected_level)
        current_db, current_mute = await self._read_camilla_volume_and_mute()
        # MEASURE_PAUSE can arrive while the Camilla read above is in flight.
        # Re-check at the write boundary so an already-running observer tick
        # cannot cross into the ramp after measurement has taken ownership.
        if self._measurement_active:
            return
        if current_db is None:
            # Camilla restart blip; next tick retries.
            return
        drift = expected_db - current_db
        mute_drift = (
            current_mute is not None
            and current_mute != expected_mute
        )
        if abs(drift) <= RECONCILE_DRIFT_DB and not mute_drift:
            return
        if drift >= RECONCILE_DUCK_SKIP_DB and not mute_drift:
            # Looks like a duck (CueDuck without _voice_session_active,
            # or some other deep attenuation we don't own). Leave it.
            # The loud direction intentionally does not skip.
            return
        # Converge. The lock closes the last race with MEASURE_PAUSE: lease
        # acquisition waits for an already-running Camilla write to finish,
        # while a write arriving after acquisition sees the flag and exits.
        async with self._reconcile_write_lock:
            if self._measurement_active:
                return
            log_event(
                logger,
                "volume.reconciled",
                # `level` collides with log_event's level= param → fields=.
                fields={
                    "source": source.value,
                    "level": f"{expected_level}%",
                    "current_db": f"{current_db:.2f}",
                    "expected_db": f"{expected_db:.2f}",
                    "drift_db": f"{drift:+.2f}",
                    "current_mute": (
                        "unknown"
                        if current_mute is None
                        else str(current_mute).lower()
                    ),
                    "expected_mute": str(expected_mute).lower(),
                },
            )
            try:
                ok = await self._write_camilla_db_with_mute(
                    expected_db,
                    context="reconcile",
                )
                if ok:
                    self._persistence.save_now(expected_db)
            except Exception as e:  # noqa: BLE001
                logger.warning("reconcile write failed (will retry): %s", e)

    async def _active_source(self) -> Source:
        """Pick the active source. Multiple-source-active is rare
        (mux preempts in <1 s) but possible during transitions; pick
        a stable priority: airplay > spotify > bluetooth > usbsink
        > idle.

        Manual source selection is an audible fan-in policy override:
        if mux reports one, prefer it even when raw renderer probes
        say a different source is active. Fail soft to raw probes when
        mux is unavailable or an older RendererClient lacks the method.

        USB sink comes last among the active priorities — between two
        camilla-master sources (AirPlay vs USBSINK), AirPlay was the
        first to ship and any in-flight session there is more likely
        to be intentional than a USB session that happens to be
        bouncing back from the host's idle state.
        """
        selected_source = getattr(self._backend, "selected_source", None)
        if selected_source is not None:
            try:
                selected = await selected_source()
                if selected:
                    return Source(selected)
            except (ValueError, TypeError):
                logger.debug("mux selected_source was unknown; ignoring")
            except Exception as e:  # noqa: BLE001
                logger.debug("selected_source() failed (%s); using probes", e)
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
        if active.get("usbsinkactive"):
            return Source.USBSINK
        return Source.IDLE

    async def _camilla_carries_level(self, source: Source) -> bool:
        """Whether camilla.main_volume IS the user-facing master volume
        for `source`, vs. delegating to a downstream slider.

        True (camilla-as-master): camilla tracks listening_level. Used
        for IDLE, AIRPLAY, and USBSINK — these camilla-master modes either
        can't reliably mirror receiver-side volume back to the
        controlling client (AirPlay 2 modern senders) or have no
        downstream slider to push to (the gadget's host-side slider
        is one-way input we observe, not a target we write).

        False (push-mode): the source's own slider carries
        listening_level and Camilla is pinned at 0 dB except for the
        explicit 0% content-mute floor. Used for SPOTIFY (Web API) and
        BLUETOOTH (AVRCP).
        """
        return volume_mode(source) == VolumeMode.CAMILLA_MASTER

    async def _duck_active(self) -> bool | None:
        if self._voice_session_active:
            return True
        if self._duck_active_probe is None:
            return False
        try:
            return await self._duck_active_probe()
        except Exception as e:  # noqa: BLE001
            logger.warning("duck_active_probe raised %s; treating as unknown", e)
            return None

    @staticmethod
    def _main_mute_for_level(level: int) -> bool:
        return int(level) <= 0

    @staticmethod
    def _main_mute_for_db(db: float) -> bool:
        return float(db) <= percent_to_db(0) + MUTE_DB_EPSILON

    async def _read_camilla_volume_and_mute(
        self,
    ) -> tuple[float | None, bool | None]:
        reader = getattr(self._camilla, "get_volume_and_mute", None)
        if reader is not None:
            result = await reader(best_effort=True)
            if result is not None:
                db, muted = result
                return float(db), bool(muted)
            return None, None
        return await self._camilla.get_volume_db(best_effort=True), None

    async def _set_camilla_main_mute(
        self, muted: bool, *, context: str,
    ) -> bool:
        target = bool(muted)
        setter = getattr(self._camilla, "set_main_mute", None)
        if setter is None:
            if target:
                log_event(
                    logger,
                    "volume.main_mute_unsupported",
                    muted=True,
                    context=context,
                    level=logging.WARNING,
                )
                return False
            return True
        ok = await setter(target, best_effort=True)
        if ok:
            log_event(
                logger,
                "volume.main_mute",
                muted=str(target).lower(),
                context=context,
                result="accepted",
                level=logging.DEBUG,
            )
            return True
        log_event(
            logger,
            "volume.main_mute",
            muted=str(target).lower(),
            context=context,
            result="failed",
            level=logging.WARNING,
        )
        return False

    async def _write_camilla_db_with_mute(
        self, db: float, *, context: str,
    ) -> bool:
        target_mute = self._main_mute_for_db(db)
        if target_mute:
            mute_ok = await self._set_camilla_main_mute(
                True, context=context,
            )
            _volume_ok = await self._camilla.set_volume_db(
                db, best_effort=True,
            )
            # Final content silence comes from main_mute. The dB floor is a
            # defense-in-depth fallback if the mute flag is later lost.
            return bool(mute_ok)

        volume_ok = await self._camilla.set_volume_db(db, best_effort=True)
        if not volume_ok:
            return False
        return await self._set_camilla_main_mute(False, context=context)

    async def _set_camilla_db(
        self, db: float, *, context: str, persist: bool,
    ) -> bool:
        """Set raw Camilla main_volume dB with the same duck gate as
        `_set_camilla`.

        Returns True when the target is written or when an active duck
        is already at/below the requested guard. Returns False when
        Camilla cannot be reached or a ducked value is still too loud
        for a source handoff. With `persist=True`, the target is still
        saved so Ducker.restore lands safe after the duck.
        """
        duck_active = await self._duck_active()
        if duck_active is True:
            target_mute = self._main_mute_for_db(db)
            if target_mute:
                mute_ok = await self._set_camilla_main_mute(
                    True, context=context,
                )
                if persist and mute_ok:
                    self._persistence.save_now(db)
                log_event(
                    logger,
                    "volume.deferred",
                    reason="session_signaled",
                    context=context,
                    target_db=f"{db:.1f}",
                    muted=True,
                    result="main_mute_applied" if mute_ok else "main_mute_failed",
                    persisted=bool(persist and mute_ok),
                )
                return bool(mute_ok)
            current_db, _current_mute = (
                await self._read_camilla_volume_and_mute()
            )
            if persist:
                self._persistence.save_now(db)
            if current_db is not None and current_db <= db + RECONCILE_DRIFT_DB:
                log_event(
                    logger,
                    "volume.deferred",
                    reason="session_signaled",
                    context=context,
                    target_db=f"{db:.1f}",
                    current_db=f"{current_db:.1f}",
                    result="already_safe",
                )
                return True
            log_event(
                logger,
                "volume.deferred",
                reason="session_signaled",
                context=context,
                target_db=f"{db:.1f}",
                current_db="unknown" if current_db is None else f"{current_db:.1f}",
                result="unsafe_for_handoff",
                persisted=bool(persist),
            )
            return False
        ok = await self._write_camilla_db_with_mute(db, context=context)
        if ok and persist:
            self._persistence.save_now(db)
        return bool(ok)

    async def _clear_confirmed_push_guard(
        self, source: Source, level: int, *, context: str,
    ) -> bool:
        """Clear a degraded Camilla guard after push-volume confirmation.

        A push-mode source proves it can carry `listening_level` in two
        ways: an outbound source write succeeds, or the observer sees the
        active source already sitting at the canonical level. In either
        case, keeping a stale downstream Camilla guard or final mute would
        create the "source says 100%, speaker is quiet" failure mode.
        """
        if volume_mode(source) != VolumeMode.PUSH:
            return False
        record = self._persistence.load()
        previous_db = record.main_volume_db if record is not None else None
        current_db, current_mute = await self._read_camilla_volume_and_mute()
        persisted_guard_active = (
            previous_db is not None
            and previous_db < -RECONCILE_DRIFT_DB
        )
        live_guard_active = (
            current_db is not None
            and current_db < -RECONCILE_DRIFT_DB
        )
        volume_guard_active = persisted_guard_active or live_guard_active
        mute_guard_active = current_mute is True
        if not volume_guard_active and not mute_guard_active:
            return False
        effective_previous_db = (
            previous_db if persisted_guard_active else current_db
        )
        if await self._duck_active() is True:
            volume_diagnostics.record_push_guard_clear(
                source,
                level=level,
                previous_db=effective_previous_db,
                reason=volume_diagnostics.GUARD_CLEAR_DEFERRED_DUCK_ACTIVE,
                context=context,
                ok=False,
            )
            log_event(
                logger,
                "volume.push_guard_clear_failed",
                level=logging.WARNING,
                # `level` field collides with log_event's level= param → fields=.
                fields={
                    "source": source.value,
                    "level": level,
                    "previous_db": (
                        "unknown"
                        if effective_previous_db is None
                        else f"{effective_previous_db:.1f}"
                    ),
                    "previous_mute": (
                        "unknown"
                        if current_mute is None
                        else str(current_mute).lower()
                    ),
                    "context": context,
                    "reason": "duck_active",
                },
            )
            return False
        cleared = await self._set_camilla_db(
            0.0,
            context=context,
            persist=True,
        )
        if cleared:
            volume_diagnostics.record_push_guard_clear(
                source,
                level=level,
                previous_db=effective_previous_db,
                context=context,
                ok=True,
            )
            log_event(
                logger,
                "volume.push_guard_cleared",
                # `level` collides with log_event's level= param → fields=.
                fields={
                    "source": source.value,
                    "level": level,
                    "previous_db": (
                        "unknown"
                        if effective_previous_db is None
                        else f"{effective_previous_db:.1f}"
                    ),
                    "previous_mute": (
                        "unknown"
                        if current_mute is None
                        else str(current_mute).lower()
                    ),
                    "context": context,
                },
            )
        else:
            volume_diagnostics.record_push_guard_clear(
                source,
                level=level,
                previous_db=effective_previous_db,
                context=context,
                ok=False,
            )
            log_event(
                logger,
                "volume.push_guard_clear_failed",
                level=logging.WARNING,
                # `level` field collides with log_event's level= param → fields=.
                fields={
                    "source": source.value,
                    "level": level,
                    "previous_db": (
                        "unknown"
                        if effective_previous_db is None
                        else f"{effective_previous_db:.1f}"
                    ),
                    "previous_mute": (
                        "unknown"
                        if current_mute is None
                        else str(current_mute).lower()
                    ),
                    "context": context,
                },
            )
        return bool(cleared)

    async def _confirm_push_mode_carrier(
        self,
        source: Source,
        level: int,
        *,
        context: str,
        include_live_guard: bool = False,
    ) -> bool:
        """Keep Camilla's final carrier consistent for push-mode sources.

        For 1-100%, Spotify/Bluetooth carry volume and Camilla returns
        to an unmuted 0 dB pin. At 0%, the source slider is still pushed
        to zero, but Camilla also asserts `main_mute` so content/music
        silence does not depend on the renderer's idea of "zero".
        """
        if volume_mode(source) != VolumeMode.PUSH:
            return False
        if self._main_mute_for_level(level):
            return await self._set_camilla_db(
                percent_to_db(0),
                context=f"{context}_zero_mute",
                persist=True,
            )

        current_db, current_mute = await self._read_camilla_volume_and_mute()
        record = self._persistence.load()
        previous_db = record.main_volume_db if record is not None else None
        needs_clear = (
            current_mute is True
            or (
                include_live_guard
                and current_db is not None
                and current_db < -RECONCILE_DRIFT_DB
            )
            or (
                previous_db is not None
                and previous_db < -RECONCILE_DRIFT_DB
            )
        )
        if not needs_clear:
            return True
        return await self._clear_confirmed_push_guard(
            source, level, context=context,
        )

    async def abort_source_handoff(self, handoff: SourceHandoff) -> bool:
        """Best-effort rollback when fan-in selection fails after prepare.

        Prepare may have changed Camilla to guard the target source.
        If the low-level fan-in gate does not move, restore the carrier
        expected by the source that is still audible.
        """
        if not handoff.ok:
            return True
        if handoff.prev_mode == VolumeMode.PUSH:
            return await self._confirm_push_mode_carrier(
                handoff.prev_source,
                handoff.level,
                context="source_handoff_abort_restore_push",
            )
        if handoff.prev_mode == VolumeMode.CAMILLA_MASTER:
            self._refresh_from_disk()
            return await self._set_camilla(self._level)
        return True

    async def _set_push_source_for_handoff(
        self, source: Source, level: int,
    ) -> bool:
        if source == Source.SPOTIFY:
            return bool(await self._set_spotify(level))
        if source == Source.BLUETOOTH:
            return bool(await self._set_bluetooth(level))
        logger.warning(
            "source handoff: %s is not a push-mode source", source.value,
        )
        return False

    def _persisted_main_volume_db(self) -> float | None:
        record = self._persistence.load()
        return record.main_volume_db if record is not None else None

    def _record_push_guard(
        self,
        source: Source,
        level: int,
        guard_db: float,
        *,
        reason: str,
        context: str,
        previous_db: float | None,
    ) -> None:
        volume_diagnostics.record_push_guard(
            source,
            level=level,
            guard_db=guard_db,
            reason=reason,
            context=context,
            previous_db=previous_db,
        )

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

    async def _set_airplay(self, level: int) -> bool:
        """AirPlay is camilla-as-master.

        shairport-sync still exposes SetAirplayVolume, but modern
        iOS/macOS AirPlay 2 sessions often omit DACP-ID/Active-Remote
        and receiver-originated volume reflection silently no-ops.
        The reliable product contract is therefore audible speaker
        volume via CamillaDSP, while the sender slider remains upstream
        trim.
        """
        return await self._set_camilla(level)

    async def _set_spotify(self, level: int) -> bool:
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
            volume_diagnostics.record_source_push(
                Source.SPOTIFY,
                level=level,
                ok=False,
                reason=volume_diagnostics.PUSH_MISSING_ROUTER,
            )
            logger.warning(
                "spotify volume set: no Web API router configured; "
                "voice/dial volume can't propagate to Spotify (set "
                "SPOTIFY_CLIENT_ID/SECRET and authorize at least one "
                "account via /spotify)",
            )
            return False
        saw_device = False
        write_failed = False
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
                    saw_device = True
                    try:
                        await asyncio.to_thread(
                            ac.sp.volume, pct, device_id=d.get("id"),
                        )
                        self._stamp_outbound(Source.SPOTIFY, level)
                        volume_diagnostics.record_source_push(
                            Source.SPOTIFY,
                            level=level,
                            ok=True,
                            reason=volume_diagnostics.PUSH_OK,
                            detail="device_visible",
                        )
                        logger.info(
                            "spotify volume set: %d%% (account=%s)",
                            pct, ac.account.name,
                        )
                        return True
                    except Exception as e:  # noqa: BLE001
                        write_failed = True
                        logger.debug(
                            "spotify volume() failed for %s: %s",
                            ac.account.name, e,
                        )
                        continue
        reason = (
            volume_diagnostics.PUSH_WRITE_FAILED
            if write_failed
            else volume_diagnostics.PUSH_NO_ACTIVE_DEVICE
        )
        volume_diagnostics.record_source_push(
            Source.SPOTIFY,
            level=level,
            ok=False,
            reason=reason,
            detail="device_visible" if saw_device else "device_not_visible",
        )
        logger.warning(
            "spotify volume set FAILED: %d%% — no account could write "
            "to device '%s' (is JTS still selected in Spotify?)",
            pct, self._spotify_device_name,
        )
        return False

    async def _set_bluetooth(self, level: int) -> bool:
        vol = listening_level_to_bt_volume(level)
        # bluez-alsa exposes one MediaTransport1 path per active
        # transport; we have to find it before we can set the
        # property. Empty list = no active BT transport (caller
        # invoked us during a brief BT-active window that closed).
        path = await _bluez_alsa_active_transport_path()
        if path is None:
            volume_diagnostics.record_source_push(
                Source.BLUETOOTH,
                level=level,
                ok=False,
                reason=volume_diagnostics.PUSH_NO_ACTIVE_TRANSPORT,
            )
            logger.debug(
                "bluetooth volume set: no active transport, skipping",
            )
            return False
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
            volume_diagnostics.record_source_push(
                Source.BLUETOOTH,
                level=level,
                ok=True,
                reason=volume_diagnostics.PUSH_OK,
                detail="transport_present",
            )
            logger.info("bluetooth volume set: %d%% (uint16=%d)", level, vol)
            return True
        else:
            volume_diagnostics.record_source_push(
                Source.BLUETOOTH,
                level=level,
                ok=False,
                reason=volume_diagnostics.PUSH_WRITE_FAILED,
                detail="transport_present",
            )
            logger.warning(
                "bluetooth volume set FAILED: %d%% (uint16=%d)", level, vol,
            )
            return False

    async def _set_camilla(self, level: int) -> bool:
        db = percent_to_db(level)
        target_mute = self._main_mute_for_level(level)
        # Defer gate #1: in-process voice-session flag. Set by
        # WakeLoop.note_voice_session on the long-lived coordinator
        # owned by jasper-voice. The Ducker has exclusive control of
        # camilla during a session; Ducker.restore() reads the
        # canonical target via get_camilla_target_db() on session end
        # and lands camilla at the user's intent. listening_level is
        # still updated in self._level by the caller and persisted by
        # _dispatch's finally block, so the user's intent survives;
        # main_volume_db is intentionally NOT saved here — it'd
        # diverge from camilla's actual state until restore.
        if self._voice_session_active:
            mute_ok = await self._set_camilla_main_mute(
                target_mute,
                context="set_camilla_voice_session",
            )
            log_event(
                logger,
                "volume.deferred",
                # `level` collides with log_event's level= param → fields=.
                fields={
                    "reason": "voice_session_active",
                    "level": f"{level}%",
                    "target_db": f"{db:.1f}",
                    "muted": str(target_mute).lower(),
                    "result": "main_mute_applied" if mute_ok else "main_mute_failed",
                },
            )
            return bool(mute_ok)
        # Defer gate #2: cross-daemon duck-active probe. The flag
        # above only fires on jasper-voice's long-lived coordinator.
        # jasper-control builds a fresh VolumeCoordinator per HTTP
        # request whose flag is always False, so it asks jasper-voice
        # over UDS whether the Ducker is currently engaged. Probe
        # returning True defers identically to the flag path —
        # Ducker.restore() will read listening_level off disk on
        # session end and converge camilla.
        #
        # Fail-open by design: probe returning None (UDS unreachable,
        # voice daemon wedged, timeout) means "unknown" → write
        # camilla normally. The dial must never silently stop working
        # because of an inter-daemon problem; better to occasionally
        # un-duck music for a moment than to leave the user with a
        # dead knob.
        if self._duck_active_probe is not None:
            try:
                duck_active = await self._duck_active_probe()
            except Exception as e:  # noqa: BLE001
                # Probe should never raise — it's expected to
                # convert errors to None internally. If it does
                # raise, treat as None (fail-open) and warn.
                logger.warning(
                    "duck_active_probe raised %s; treating as unknown",
                    e,
                )
                duck_active = None
            if duck_active is True:
                mute_ok = await self._set_camilla_main_mute(
                    target_mute,
                    context="set_camilla_session_signaled",
                )
                log_event(
                    logger,
                    "volume.deferred",
                    # `level` collides with log_event's level= param → fields=.
                    fields={
                        "reason": "session_signaled",
                        "level": f"{level}%",
                        "target_db": f"{db:.1f}",
                        "muted": str(target_mute).lower(),
                        "result": "main_mute_applied" if mute_ok else "main_mute_failed",
                    },
                )
                return bool(mute_ok)
        # best_effort: dial twist arriving during a 2s camilla restart
        # blip should still update listening_level on disk and persist
        # main_volume_db, even if the actual write didn't land. The
        # next set_volume call (or a source-transition) will re-apply
        # once camilla is back.
        ok = await self._write_camilla_db_with_mute(
            db, context="set_camilla",
        )
        # main_volume IS what the user is controlling in idle. Persist
        # it explicitly so the legacy regress_if_stale path still has
        # the right value if some future restart goes through it.
        self._persistence.save_now(db)
        # No echo prevention for camilla — there's no observer for
        # main_volume changes (no source generates them externally
        # while idle).
        log_event(
            logger,
            "volume.camilla_set",
            # `level` collides with log_event's level= param → fields=.
            fields={
                "level": f"{level}%",
                "target_db": f"{db:.1f}",
                "muted": str(target_mute).lower(),
                "result": "accepted" if ok else "failed",
            },
        )
        return bool(ok)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Retained lifecycle hook; this coordinator owns no async resources."""


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

    Probes go through `bluealsa_probe.list_pcms`, which adds a shared
    process-local backoff so a D-Bus permission denial here does not
    hammer the system bus on every BT volume set.
    """
    stdout = await bluealsa_probe.list_pcms(logger)
    if stdout is None:
        return None
    m = _BLUEZ_TRANSPORT_PATH_RE.search(stdout)
    return m.group(1).decode("ascii") if m else None
