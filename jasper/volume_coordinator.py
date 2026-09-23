# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Source-aware volume coordinator.

The user perceives "speaker volume" as one 0-100 number. Underneath,
several attenuators sit on the real audio chain:

    track_loudness × airplay_sender_vol × spotify_connect_vol
        × bt_avrcp_vol × camilla_main_volume → DAC

This module owns which one the number applies to. One canonical state
persists at ``volume_persistence.configured_path()``, interpreted by
``VolumeState`` as ``listening_level`` plus temporary mute intent. Spotify
and Bluetooth are push-mode: their own protocol sliders carry
`listening_level` and CamillaDSP stays at 0 dB, asserting `main_mute` at 0%
so content/music zero is a final-output mute rather than source-side
attenuation. Idle, AirPlay, and USB sink are camilla-as-master: CamillaDSP
`main_volume` carries `listening_level`. AirPlay's inbound observation
arrives from shairport's volume hook rather than a poller (ADR-0206).

Every outbound write timestamps itself per source; an inbound observation of
the same source within `ECHO_WINDOW_SEC` (500 ms) is treated as our own echo
and ignored — this also covers a short stale-read window where a poll lands
before the protocol surface has caught up with our write.

This file is the dispatch layer. Inbound observers live in
`volume_observers.py`, started by voice_daemon at boot; jasper-control's
per-request coordinators share the same persistence file, so remote- and
voice-driven changes converge.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from uuid import uuid4

from .assistant_volume import (
    EffectiveVolumeContext,
    VolumeContextPublisher,
    volume_context_publisher_for_runtime,
    volume_context_stamp_boot_ns,
)
from .assistant_loudness import tts_envelope_lufs_for_level
from .identity.speaker_name import runtime_name as speaker_runtime_name
from .log_event import log_event
from .music_sources import (
    MUSIC_SOURCE_VALUES,
    SOURCE_TO_ACTIVE_KEY,
    Source,
    VolumeMode,
    volume_mode,
)
from . import volume_push_sources
from .voice.measurement_hold import MEASUREMENT_AUTOCLEAR_SEC
from .volume_echo import (
    is_own_echo,
    is_recent_cross_process_write,
    stamp_outbound,
)
from .volume_owner import VolumeClaimRefused, VolumeOwner
from .volume_scales import native_to_listening_level
from .volume_curve import percent_to_db
from .volume_floor import RECONCILE_DRIFT_DB
from .volume_handoff import VolumeHandoff, main_mute_for_level
from .volume_state import VolumeState, OutboundStamp
from .volume_persistence import (
    VolumePersistence,
    configured_path as volume_state_path,
    regress_listening_level_if_stale,
)

if TYPE_CHECKING:
    from .volume_handoff import SourceHandoff
    from .camilla import CamillaController
    from .renderer import RendererClient

logger = logging.getLogger(__name__)

# Keep the measurement clock independent of asyncio clocks.
_measurement_monotonic = time.monotonic


# Cross-daemon Camilla-ownership probe. None fails open, so a wedged
# jasper-voice cannot freeze the remote.
CamillaLockProbe = Callable[[], Awaitable[Optional[bool]]]


# Reconciler thresholds. `maybe_reconcile_camilla` converges
# `main_volume_db` toward `percent_to_db(listening_level)` when it has
# drifted, no session is active, and the active source is camilla-as-master.
#
# `RECONCILE_DUCK_SKIP_DB` is directional: skip when Camilla is much
# QUIETER than expected (avoid un-ducking); always correct when much
# LOUDER (the safety case the reconciler exists to catch).
#
# A dB gap is not valid evidence of fader ownership (ADR-0177), and
# neither in-process duck needs this skip: the reconciler writes via the
# HOUSEHOLD claim, which a held `TRANSIENT_DUCK` outranks, and the
# graph-swap bracket is asked through the DSP writer lock instead
# (ADR-0213). What remains is the volume-floor audition in `jasper-web`,
# whose claim does not survive its own exit and whose owner this process
# cannot reach (#3038). REMOVE THIS THRESHOLD once that audition announces
# itself via a writer-lock hold or MEASURE_PAUSE — until then a duck
# stranded by a killed swap stays stranded.
RECONCILE_DUCK_SKIP_DB = 10.0
MUTE_DB_EPSILON = 1e-6


class VolumeCoordinator:
    """Owns canonical volume intent and dispatches its effective level.

    Persisted fields are interpreted through ``VolumeState``; the active
    source decides only which attenuator carries ``effective_percent``.

    The coordinator does NOT cache active_renderers() across calls —
    RendererClient.active_renderers() is itself fast (<100 ms typical)
    and re-querying on each volume command keeps "I just hit pause"
    transitions correct.

    Instances are async-first. Sync callers (control daemon HTTP
    handlers) wrap with asyncio.run(...). That spins a fresh event
    loop per request — fine at remote-tick rate (~10/s peak), and
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
        duck_active_probe: CamillaLockProbe | None = None,
        volume_context_publisher: VolumeContextPublisher | None = None,
        handoff_settle_sec: float = 0.45,
        push_settle_sec: float = 0.75,
    ) -> None:
        self._camilla = camilla
        # The one thing in this process that writes the main fader. The
        # coordinator holds the HOUSEHOLD claim — the standing level the
        # speaker plays at when nothing outranks it — and hands the same owner
        # to the transient-duck holders, so a duck and a volume twist are
        # arbitrated rather than racing.
        self._volume_owner = VolumeOwner(
            set_fader_db=self._write_fader_db,
            get_fader_db=self._read_fader_db,
        )
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
        # Durable identity of the current temporary-mute transition. Source
        # observers use it to prove they have seen this exact mute reach a
        # push-mode renderer before accepting a later nonzero user change.
        self._mute_token: str | None = None
        self._confirmed_push_mute_tokens: dict[Source, str] = {}
        # Echo-prevention timestamps, per source.
        self._last_outbound: dict[Source, OutboundStamp] = {}
        # One lock for all level mutations — coordinator is async-
        # single-threaded but multiple consumers (voice tool, remote
        # via UDS, observer) can race.
        self._lock = asyncio.Lock()

        # Voice-session gate: while True, the source-transition
        # handler is suppressed. Set/cleared by voice_daemon's WakeLoop
        # via `note_voice_session(True/False)`. Only meaningful on
        # the long-lived coordinator owned by jasper-voice; per-
        # request coordinators in jasper-control always read False
        # and rely on `_duck_active_probe` instead.
        self._voice_session_active: bool = False
        # A voice session does not necessarily lock Camilla. Current production
        # ducks renderer/program audio inside fan-in, leaving Camilla as a safe
        # user-volume surface for the final music+TTS mix.
        self._camilla_volume_locked: bool = False
        # Correction-measurement gate for the voice daemon's own 1 Hz
        # reconciler AND for the voice tools' level doors (set/adjust/unmute),
        # which reach this object in-process and so never pass jasper-control's
        # measurement hold. It does not turn this process-local flag into a
        # cross-daemon Camilla lock, and never blocks an emergency user MUTE.
        # It prevents any writer from replacing a ramp value with the persisted
        # listening_level mid-measurement.
        self._measurement_active: bool = False
        # When the flag above was raised, so a missed lower can lapse rather
        # than refuse for the life of the process. See
        # :meth:`_measurement_holds_fader`.
        self._measurement_active_at: float = 0.0
        self._measurement_lapse_logged: bool = False
        # Edge state for the three faults this reconciler re-evaluates every
        # tick; each is reported once per episode, never at 1 Hz.
        self._deep_quiet_skipped: bool = False
        self._write_failures: int = 0
        self._graph_probe_failures: int = 0
        # Serializes the final reconciler write with MEASURE_PAUSE acquisition.
        # Pause does not acknowledge until an already-started write has landed;
        # after the flag flips, no new reconcile write may enter this lock.
        self._reconcile_write_lock = asyncio.Lock()
        # Cross-daemon Camilla-ownership signal. jasper-control's per-
        # request coordinators set this to a UDS-probing callable
        # that asks jasper-voice's `session_status` whether a duck holder
        # owns Camilla. jasper-voice's own coordinator leaves it None and
        # uses `_camilla_volume_locked` in-process.
        self._duck_active_probe: CamillaLockProbe | None = duck_active_probe
        self._volume_context_publisher = volume_context_publisher
        self._handoff = VolumeHandoff(
            effective_level=lambda: self.get_volume_state().effective_percent,
            read_carrier=lambda: self._read_camilla_volume_and_mute(),
            persisted_carrier=lambda: self._persisted_main_volume_db(),
            write_guard=lambda db, *, context, persist: self._set_camilla_db(
                db, context=context, persist=persist,
            ),
            push_source=lambda source, level: self._set_push_source_for_handoff(source, level),
            camilla_locked=lambda: self._camilla_locked(),
            write_level=lambda level: self._set_camilla(level),
            handoff_settle_sec=handoff_settle_sec,
            push_settle_sec=push_settle_sec,
        )

    # ------------------------------------------------------------------
    # Public API — read state
    # ------------------------------------------------------------------

    def get_listening_level(self) -> int:
        """Current remembered listening level (0-100).

        This remains the restore target while temporarily muted. External
        callers that need the currently effective level must use
        ``get_volume_state()``.
        """
        return self._level

    def get_volume_state(self) -> VolumeState:
        """Return the fresh canonical state shared by every process/surface."""
        self._refresh_from_disk()
        return self._current_volume_state()

    def _current_volume_state(self) -> VolumeState:
        """Interpret the already-loaded fields without another disk read."""
        return VolumeState(
            listening_level=max(0, min(100, int(self._level))),
            pre_mute_level=self._pre_mute_level,
            mute_token=self._mute_token,
        )

    def source_observation_revision(self, source: Source) -> str | None:
        """Return state-machine identity relevant to a source observer.

        The native renderer value alone is not enough to deduplicate an
        observation: two rapid mute transitions can both present zero. Expose
        only the opaque revision needed by the observer; interpretation stays
        owned by this coordinator.
        """
        if volume_mode(source) != VolumeMode.PUSH:
            return None
        record = self._persistence.load()
        return record.mute_token if record is not None else None

    def _effective_level(self) -> int:
        return self._current_volume_state().effective_percent

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

    @asynccontextmanager
    async def _mutation(self, *, refresh: bool = True):
        """Serialize one volume intent locally and across JTS daemons."""
        async with self._lock:
            async with self._persistence.operation_lock():
                if refresh:
                    self._refresh_from_disk()
                yield

    @asynccontextmanager
    async def source_handoff_operation(self):
        """Serialize one mux lane handoff with every volume mutation.

        The mux must hold this lease from carrier preparation through fan-in
        selection, carrier finalization, and publication of its new winner.
        Otherwise an old source's observer can still look authoritative after
        fan-in has already exposed the new lane and can clear that lane's
        protective Camilla guard.
        """
        async with self._mutation():
            yield

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
        async with self._mutation():
            self._level = target_level
            source = await self._active_source()
            # Make camilla consistent with the boot mode. Idle and
            # AirPlay use camilla as the remembered/audible volume;
            # Spotify and Bluetooth carry listening_level on their own
            # protocol surfaces. Push-mode 0% is the exception: still
            # assert Camilla main_mute as the content/music mute guarantee.
            if await self._camilla_carries_level(source):
                await self._set_camilla(target_level)
                await self._set_loudness_level(target_level)
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
            self._mute_token = None
            self._persistence.save_mute_state(None, None)
            self._persistence.save_listening_level(
                target_level, mark_user_change=False,
            )
        await self.publish_volume_context()
        return target_level, reason

    # ------------------------------------------------------------------
    # Public API — set / adjust
    # ------------------------------------------------------------------

    def _measurement_holds_fader(self) -> bool:
        """True while a live measurement owns the fader; False once it lapses.

        A pure predicate: it must not clear ``_measurement_active``, or a
        volume write arriving after the lapse would also un-pause the 1 Hz
        reconciler — which reads the raw flag — in the middle of a window that
        is merely renewing late. The reconciler clears it on its own tick
        instead (:meth:`_lapse_stranded_measurement_flag`).

        The lapse itself exists because ``note_measurement_active(False)`` is
        best-effort on the voice daemon's rollback path: past its aggregate
        deadline the resume coroutine is closed unawaited and the safety task
        is already cancelled, so the flag can stay raised with nothing left to
        lower it. Treating it as lapsed after MEASUREMENT_AUTOCLEAR_SEC — the
        backstop that would have cleared it — bounds a stranded flag's effect
        on these doors to one window instead of the life of the process.
        """
        if not self._measurement_active:
            return False
        held_for = _measurement_monotonic() - self._measurement_active_at
        if held_for < MEASUREMENT_AUTOCLEAR_SEC:
            return True
        if not self._measurement_lapse_logged:
            self._measurement_lapse_logged = True
            log_event(
                logger,
                "volume.measurement_flag_expired",
                held_for_s=f"{held_for:.1f}",
                autoclear_s=f"{MEASUREMENT_AUTOCLEAR_SEC:.1f}",
                level=logging.WARNING,
            )
        return False

    def _lapse_stranded_measurement_flag(self) -> None:
        """Clear a stranded flag on the reconciler's OWN tick.

        ``_measurement_holds_fader`` may not clear it — a volume write is the
        wrong clock (see there). This 1 Hz tick is the right one: it runs on
        the same schedule the flag pauses, so applying the same
        MEASUREMENT_AUTOCLEAR_SEC bound here bounds a stranded flag's effect on
        drift reconciliation to one window instead of the life of the process.
        No await between the read and the write, so a window renewing
        concurrently cannot have its fresh flag cleared.
        """
        if self._measurement_active and not self._measurement_holds_fader():
            self._measurement_active = False

    def _refuse_level_write_while_measuring(self) -> None:
        """Refuse a level write while a measurement holds the fader.

        These doors are reached IN-PROCESS — `jasper.tools.audio` calls them on
        this coordinator whenever the box is not a bonded follower, so the
        request never crosses HTTP and jasper-control's measurement hold never
        sees it. While the hold is live the measurement OWNS the fader: it
        drives camilla's main_volume directly and never writes the persistence
        file, so no persisted level here can be compared against where the
        fader actually sits, and a write in EITHER direction can land a
        stimulus above the driver's declared cap. Raising is what makes the
        refusal visible: `tools` turns the exception into the tool's
        `{"error": ...}` payload, so the model says the speaker is busy. MUTE
        stays open as the emergency door; unmute does not, because restoring
        the household level is a level write.
        """
        if self._measurement_holds_fader():
            raise VolumeClaimRefused(
                "a measurement is in progress and holds the volume"
            )

    async def set_listening_level(self, percent: int) -> int:
        """Set canonical listening_level to `percent` (clamped to 0..100).
        Dispatches to the active source (or camilla, if idle).
        Persists. Returns the level that was actually applied."""
        target = max(0, min(100, int(percent)))
        async with self._mutation():
            self._refresh_from_disk()
            self._refuse_level_write_while_measuring()
            previous_level = self._effective_level()
            self._level = target
            self._pre_mute_level = None  # any explicit set clears mute state
            self._mute_token = None
            self._persistence.save_mute_state(None, None)
            source = await self._active_source()
            await self._publish_user_intent_context(
                source, target, muted=main_mute_for_level(target),
            )
            if main_mute_for_level(target):
                await self._set_camilla_main_mute(
                    True, context="set_listening_level_intent",
                )
            await self._dispatch(
                target, persist=True, source=source, previous_level=previous_level,
            )
        await self.publish_volume_context(phase="converged")
        return target

    async def adjust_listening_level(self, delta: int) -> int:
        """Bump current level by `delta` (positive = louder), clamped.
        Returns the new level. Refreshes the in-memory level from disk
        first so a recent remote/HTTP write from another process is
        visible — without this, voice "louder" right after a remote
        click would compute from a stale baseline."""
        async with self._mutation():
            self._refresh_from_disk()
            target = max(0, min(100, self._level + int(delta)))
            self._refuse_level_write_while_measuring()
            previous_level = self._effective_level()
            self._level = target
            self._pre_mute_level = None
            self._mute_token = None
            self._persistence.save_mute_state(None, None)
            source = await self._active_source()
            await self._publish_user_intent_context(
                source, target, muted=main_mute_for_level(target),
            )
            if main_mute_for_level(target):
                await self._set_camilla_main_mute(
                    True, context="adjust_listening_level_intent",
                )
            await self._dispatch(
                target, persist=True, source=source, previous_level=previous_level,
            )
        await self.publish_volume_context(phase="converged")
        return target

    async def _mute_locked(self) -> int:
        """Apply mute while ``_mutation`` is already held."""
        if self._pre_mute_level is None and self._level > 0:
            self._pre_mute_level = self._level
            self._mute_token = uuid4().hex
        elif self._pre_mute_level is not None and self._mute_token is None:
            # Repair a latch whose token was missing or rejected on load.
            self._mute_token = uuid4().hex
        saved = self._pre_mute_level or 0
        self._persistence.save_mute_state(
            self._pre_mute_level,
            self._mute_token,
        )
        source = await self._active_source()
        # Fan-in is the immediate TTS stop and does not depend on Camilla
        # being healthy. Publish before touching the final-output backstop.
        await self._publish_user_intent_context(source, saved, muted=True)
        # Final-output mute is local and safety-critical; never wait for a
        # Spotify/BT cloud or protocol round trip before asserting it.
        await self._set_camilla_main_mute(
            True, context="mute_intent",
        )
        await self._dispatch(0, persist=False, source=source)
        return saved

    async def mute(self) -> int:
        """Silence the speaker. Saves pre-mute level for unmute.
        Returns the saved (pre-mute) level. Persisted so a later
        unmute on a different coordinator instance (jasper-control
        builds one per HTTP request) can still see it."""
        async with self._mutation():
            saved = await self._mute_locked()
        await self.publish_volume_context(phase="converged")
        return saved

    async def _unmute_locked(self, fallback_level: int = 50) -> int:
        """Apply unmute while ``_mutation`` is already held.

        Every unmute path lands here, so this is the one place the measurement
        refusal has to sit for `unmute`, `set_muted(False)` and a toggle that
        resolves to unmuted.
        """
        self._refuse_level_write_while_measuring()
        target = (
            self._pre_mute_level
            if self._pre_mute_level is not None
            else fallback_level
        )
        target = max(0, min(100, int(target)))
        self._pre_mute_level = None
        self._mute_token = None
        self._persistence.save_mute_state(None, None)
        self._level = target
        source = await self._active_source()
        await self._publish_user_intent_context(
            source, target, muted=main_mute_for_level(target),
        )
        if main_mute_for_level(target):
            await self._set_camilla_main_mute(
                True, context="unmute_intent",
            )
        await self._dispatch(target, persist=True, source=source)
        return target

    async def unmute(self, fallback_level: int = 50) -> int:
        """Restore pre-mute level (or fallback if no prior mute).
        Returns the restored level."""
        async with self._mutation():
            target = await self._unmute_locked(fallback_level)
        await self.publish_volume_context(phase="converged")
        return target

    async def set_muted(
        self,
        want_muted: bool,
        *,
        fallback_level: int = 50,
    ) -> VolumeState:
        """Idempotently apply explicit mute intent under one atomic decision."""
        changed = False
        async with self._mutation():
            if want_muted and self._pre_mute_level is None:
                await self._mute_locked()
                changed = True
            elif not want_muted and self._pre_mute_level is not None:
                await self._unmute_locked(fallback_level)
                changed = True
            state = self._current_volume_state()
        if changed:
            await self.publish_volume_context(phase="converged")
        return state

    async def toggle_mute(self, *, fallback_level: int = 50) -> VolumeState:
        """Toggle temporary mute under one cross-process atomic decision."""
        async with self._mutation():
            if self._pre_mute_level is not None:
                await self._unmute_locked(fallback_level)
            else:
                await self._mute_locked()
            state = self._current_volume_state()
        await self.publish_volume_context(phase="converged")
        return state

    def _refresh_from_disk(self) -> None:
        """Sync in-memory state with the persistence file. Cheap (~1ms
        sync read of a small JSON file) and called on every public
        operation so cross-process writes (jasper-control via remote)
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
        self._mute_token = record.mute_token

    # ------------------------------------------------------------------
    # Observer hook — called by inbound DBus/HTTP observers when they
    # see a source-side volume change. Updates listening_level if the
    # change isn't an echo of our own outbound write.
    # ------------------------------------------------------------------

    async def observe_source_volume(
        self,
        source: Source,
        native_value: float | int,
        *,
        initial: bool = False,
    ) -> bool:
        """Inbound observer entrypoint. `native_value` is in the
        source's own units (percent for AirPlay, Spotify and USB sink,
        uint16 for BT). The coordinator converts and updates the
        canonical level if this isn't an echo. Returns True when the
        observation was accepted for the active source and False when
        it was intentionally declined.
        """
        level = native_to_listening_level(source, native_value)
        if level is None:
            logger.debug("observe_source_volume: unknown source %s", source)
            return False
        active = await self._active_source()
        if active != source:
            logger.debug(
                "observe %s: ignoring %d%% because active source is %s",
                source.value, level, active.value,
            )
            return False
        publish_needed = False
        loudness_first = False
        async with self._mutation(refresh=False):
            # A cross-process operation may have held the lease after the
            # optimistic check above. Revalidate source ownership at the
            # ordering point so a queued observation cannot update canonical
            # state after mux has moved to another lane.
            active = await self._active_source()
            if active != source:
                self._refresh_from_disk()
                logger.debug(
                    "observe %s: ignoring queued %d%% because active source "
                    "became %s",
                    source.value,
                    level,
                    active.value,
                )
                return False
            if level > 0 and self._measurement_holds_fader():
                return False
            # Source observers live in jasper-voice while HTTP/accessory mute
            # may have landed through jasper-control. Inspect the persisted mute
            # latch before interpreting an observation, but preserve the prior
            # cached level until `_is_recent_cross_process_write` has compared
            # it with disk — refreshing early would erase the evidence that
            # another process just moved the canonical level.
            record = self._persistence.load()
            persisted_pre_mute = (
                record.pre_mute_level if record is not None else None
            )
            persisted_mute_token = (
                record.mute_token if record is not None else None
            )
            push_mode = volume_mode(source) == VolumeMode.PUSH
            if (
                initial
                and persisted_pre_mute is not None
                and not push_mode
            ):
                # USB's bridge publishes the mixer's current value when it
                # starts or becomes active. That snapshot predates any proof
                # of user intent and must not erase a mute asserted elsewhere.
                self._refresh_from_disk()
                logger.debug(
                    "observe %s: deferring initial %d%% while mute is latched",
                    source.value,
                    level,
                )
                return False
            if persisted_pre_mute is None:
                self._confirmed_push_mute_tokens.pop(source, None)
            elif push_mode and persisted_mute_token is None:
                # Repair a latch whose token was missing or rejected on load.
                persisted_mute_token = uuid4().hex
                self._persistence.save_mute_state(
                    persisted_pre_mute,
                    persisted_mute_token,
                )
                self._mute_token = persisted_mute_token
            if (
                persisted_pre_mute is not None
                and push_mode
                and level == 0
            ):
                # A push-mode mute writes 0 to the renderer. Its observer will
                # echo that value from another process, where the in-memory
                # outbound stamp is unavailable. Treat it as confirmation of
                # this exact mute transition, not a new 0% edit that destroys
                # the restore level. This intentionally precedes own-echo
                # suppression: the first observed zero is the durable barrier
                # that makes a later nonzero observation trustworthy.
                assert persisted_mute_token is not None
                self._confirmed_push_mute_tokens[source] = persisted_mute_token
                self._refresh_from_disk()
                _, publish_needed = (
                    await self._handoff.confirm_push_mode_carrier_with_mutation(
                        source,
                        0,
                        context=f"observe_{source.value}_mute_confirmed",
                        include_live_guard=True,
                    )
                )
                accepted_muted_echo = True
            elif (
                persisted_pre_mute is not None
                and push_mode
                and self._confirmed_push_mute_tokens.get(source)
                != persisted_mute_token
            ):
                # mute() persists intent before the slow Spotify/BT push. Until
                # this observer has seen zero for the same durable token, a
                # nonzero renderer reading can only be the pre-push value (or
                # an ambiguous concurrent edit). Mute intent wins that race.
                assert persisted_mute_token is not None
                self._refresh_from_disk()
                logger.debug(
                    "observe %s: deferring %d%% until mute token %s reaches "
                    "renderer zero",
                    source.value,
                    level,
                    persisted_mute_token[:8],
                )
                return False
            else:
                accepted_muted_echo = False
            if not accepted_muted_echo:
                if self._is_own_echo(source, level):
                    logger.debug(
                        "observe %s: %d%% within echo window — "
                        "ignoring (own write)",
                        source.value,
                        level,
                    )
                    return False
                if self._is_recent_cross_process_write(level):
                    self._refresh_from_disk()
                    logger.debug(
                        "observe %s: %d%% within persistence echo window — "
                        "ignoring (recent external write)",
                        source.value, level,
                    )
                    return False
                self._refresh_from_disk()
                loudness_first = level >= self._effective_level()
                if loudness_first:
                    await self._set_loudness_level(level)
                if level == self._level and self._pre_mute_level is None:
                    if await self._camilla_carries_level(source):
                        publish_needed = await self._sync_camilla_observed_level(
                            source, level,
                        )
                    else:
                        result = (
                            await self._handoff.confirm_push_mode_carrier_with_mutation(
                                source,
                                level,
                                context=f"observe_{source.value}_push_confirmed",
                                include_live_guard=True,
                            )
                        )
                        carrier_ok, publish_needed = result
                        if not carrier_ok:
                            publish_needed = False
                else:
                    logger.info(
                        "observe %s: user-side change %d%% → %d%%",
                        source.value, self._level, level,
                    )
                    self._level = level
                    self._pre_mute_level = None
                    self._mute_token = None
                    self._persistence.save_mute_state(None, None)
                    self._confirmed_push_mute_tokens.pop(source, None)
                    self._persistence.save_listening_level(level)
                    if await self._camilla_carries_level(source):
                        await self._sync_camilla_observed_level(source, level)
                    else:
                        await self._handoff.confirm_push_mode_carrier(
                            source,
                            level,
                            context=f"observe_{source.value}_push_confirmed",
                            include_live_guard=True,
                        )
                    publish_needed = True
            if not loudness_first:
                await self._set_loudness_level(level)
        if publish_needed:
            # Camilla/socket reads and IPC happen after releasing the mutation
            # lock; volume commands must not queue behind observability work.
            await self.publish_volume_context()
        return True

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
        expected_mute = main_mute_for_level(level)
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
        self,
        level: int,
        *,
        persist: bool,
        user_change: bool = True,
        source: Source | None = None,
        previous_level: int | None = None,
    ) -> None:
        """Push `level` to the active source (or camilla if idle)
        and (optionally) persist. Caller holds the mutation lock. Live user
        calls persist and publish push-mode intent before entering this slow
        actuator path; ``source`` avoids probing the same routing fact twice.

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
        source = source if source is not None else await self._active_source()
        loudness_first = previous_level is None or level >= previous_level
        try:
            if loudness_first:
                await self._set_loudness_level(level)
            if source == Source.AIRPLAY:
                await self._set_airplay(level)
            elif source == Source.SPOTIFY:
                ok = await self._set_spotify(level)
                if ok:
                    await self._handoff.confirm_push_mode_carrier(
                        source,
                        level,
                        context="dispatch_spotify_push_confirmed",
                    )
                else:
                    await self._handoff.guard_camilla_after_push_failure(
                        level,
                        context="dispatch_spotify_degraded",
                        warning_prefix="spotify volume dispatch failed",
                        guarded_warning_suffix=(
                            "; camilla guarded at {guard_db:.1f} dB for "
                            "{level:d}%"
                        ),
                    )
            elif source == Source.BLUETOOTH:
                ok = await self._set_bluetooth(level)
                if ok:
                    await self._handoff.confirm_push_mode_carrier(
                        source,
                        level,
                        context="dispatch_bluetooth_push_confirmed",
                    )
                else:
                    await self._handoff.guard_camilla_after_push_failure(
                        level,
                        context="dispatch_bluetooth_degraded",
                        warning_prefix="bluetooth volume dispatch failed",
                        guarded_warning_suffix=(
                            "; camilla guarded at {guard_db:.1f} dB for "
                            "{level:d}%"
                        ),
                    )
            else:
                # USBSINK is camilla-master like AirPlay; we don't write
                # back to the gadget's mixer (the host's slider is
                # observed-only — see observe_source_volume above and
                # ADR-0281).
                await self._set_camilla(level)
            if not loudness_first:
                await self._set_loudness_level(level)
        finally:
            if persist:
                self._persistence.save_listening_level(
                    level, mark_user_change=user_change,
                )

    async def prepare_source_handoff(
        self, prev_source: Source, current_source: Source, *, reason: str,
    ) -> SourceHandoff:
        return await self._handoff.prepare_source_handoff(
            prev_source, current_source, reason=reason,
        )

    async def finalize_source_handoff(self, handoff: SourceHandoff) -> bool:
        return await self._handoff.finalize_source_handoff(handoff)

    async def abort_source_handoff(self, handoff: SourceHandoff) -> bool:
        return await self._handoff.abort_source_handoff(handoff)

    async def apply_active_source_transition(
        self, prev_source: Source, current_source: Source,
    ) -> None:
        """Called by the observer when active_renderers reports a
        source-state change. Single point that touches camilla
        across the boundary, driven by `_camilla_carries_level`:

        - camilla-master → push-mode (AirPlay/idle → Spotify/BT):
          push the effective level to the new renderer,
          then pin camilla to 0 dB only if that push succeeds.
        - push-mode → camilla-master (Spotify/BT → AirPlay/idle):
          hand camilla back the current effective level.
        - push → push (e.g. Spotify → BT): camilla already at 0 dB;
          just enforce the effective level on the new source.
        - camilla-master → camilla-master (idle ↔ AirPlay): no
          volume handoff is needed; camilla already carries the level.

        We DON'T fire this mid-voice-session: the ducker hooks in via
        `note_voice_session` so this method can short-circuit.
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
        async with self._mutation():
            # The verdict was resolved before the cross-daemon lease. Re-check
            # source ownership at the ordering point, as
            # `observe_source_volume` does, so a handoff that landed meanwhile
            # cannot pin camilla against a lane the mux has already left.
            active = await self._active_source()
            if active != current_source:
                self._refresh_from_disk()
                logger.debug(
                    "active_source transition %s→%s: dropped, active "
                    "source became %s",
                    prev_source.value, current_source.value, active.value,
                )
                return
            # Pull the latest listening_level from disk before
            # dispatching. The control daemon (remote / HTTP) writes
            # the same file on every twist, but voice_daemon's in-
            # memory cache only re-syncs on its own set/adjust/mute
            # calls. Without this refresh, a remote twist that lands
            # between voice operations would be silently ignored
            # when the next source-state transition fires.
            self._refresh_from_disk()
            level = self._effective_level()
            if prev_carries and not curr_carries:
                # Camilla-master → push-mode renderer. Push the new
                # source first, then clear Camilla only after the
                # source-side write succeeds. If the push fails,
                # Camilla remains the safety carrier instead of
                # exposing a stale/full-scale source.
                push_ok = await self._set_push_source_for_handoff(
                    current_source, level,
                )
                if push_ok:
                    carrier_ok = await self._handoff.confirm_push_mode_carrier(
                        current_source,
                        level,
                        context="active_source_transition_push_clear",
                    )
                    logger.info(
                        "active source: %s → %s; pushed %d%% to source "
                        "slider and confirmed camilla push-mode carrier "
                        "result=%s",
                        prev_source.value, current_source.value, level,
                        "accepted" if carrier_ok else "failed",
                    )
                else:
                    await self._handoff.guard_camilla_after_push_failure(
                        level,
                        context="active_source_transition_push_degraded",
                        warning_prefix=(
                            f"active source: {prev_source.value} → "
                            f"{current_source.value}; source volume push failed"
                        ),
                        guarded_warning_suffix=(
                            ", keeping camilla guarded at {guard_db:.1f} dB"
                        ),
                    )
            elif curr_carries and not prev_carries:
                # Push-mode renderer → camilla-master. Hand
                # effective volume back to camilla so a temporary mute
                # remains silent while its remembered level is preserved.
                ok = await self._set_camilla(level)
                logger.info(
                    "active source: %s → %s; camilla → %.1f dB (%d%%) "
                    "result=%s",
                    prev_source.value, current_source.value,
                    percent_to_db(level), level,
                    "accepted" if ok else "failed",
                )
            elif not curr_carries:
                # Push → push (e.g. spotify → bt). Camilla already at
                # 0 dB; enforce listening_level on the new source. If
                # the push fails, fall back to Camilla as a safety
                # carrier because all renderer lanes still flow
                # through Camilla.
                push_ok = await self._set_push_source_for_handoff(
                    current_source, level,
                )
                if push_ok:
                    await self._handoff.confirm_push_mode_carrier(
                        current_source,
                        level,
                        context="active_source_transition_push_push_confirmed",
                    )
                    logger.info(
                        "active source: %s → %s (push→push); pushed "
                        "%d%% to new source slider",
                        prev_source.value, current_source.value, level,
                    )
                else:
                    await self._handoff.guard_camilla_after_push_failure(
                        level,
                        context="active_source_transition_push_push_degraded",
                        warning_prefix=(
                            f"active source: {prev_source.value} → "
                            f"{current_source.value} (push→push); source "
                            "volume push failed"
                        ),
                        guarded_warning_suffix=(
                            ", camilla guarded at {guard_db:.1f} dB"
                        ),
                    )
            else:
                # Idle ↔ AirPlay: both are camilla-master modes, so
                # camilla already carries listening_level.
                logger.debug(
                    "active source: %s → %s (no camilla change)",
                    prev_source.value, current_source.value,
                )
        # Carrier handoffs change the downstream attenuation algebra even when
        # the canonical level is unchanged. Publish after releasing the
        # mutation lock; snapshotting re-acquires it, while socket IPC does not.
        await self.publish_volume_context()

    def note_voice_session(
        self,
        active: bool,
        *,
        camilla_volume_locked: bool | None = None,
    ) -> None:
        """Called by voice_daemon's WakeLoop on session start/end.
        While a session is active, this coordinator suppresses source
        handoffs. It suppresses Camilla writes only when the duck
        transport actually owns Camilla. Affected paths:
        `apply_active_source_transition` (no-ops mid-session) and
        `_set_camilla` (defers only while ``camilla_volume_locked``;
        listening_level still persists)."""
        self._voice_session_active = bool(active)
        self._camilla_volume_locked = bool(
            active and (
                True if camilla_volume_locked is None else camilla_volume_locked
            )
        )

    async def effective_volume_context(self) -> EffectiveVolumeContext:
        """Return one mutation-coherent snapshot without holding IPC open."""
        return await self._effective_volume_context()

    async def _effective_volume_context(self) -> EffectiveVolumeContext:
        """Return the absolute volume facts consumed by fan-in.

        The canonical dB value represents user intent. ``downstream_db`` is
        Camilla's actual gain when readable, with the coordinator's safe target
        as a fail-soft fallback. While a duck holder owns Camilla, use that
        unducked target rather than publishing the temporary duck attenuation
        as though it were user intent.
        """
        for _attempt in range(3):
            # The short lock sections serialize this process's mutations. Slow
            # Camilla/source probes remain outside the lock; the second read
            # detects a mutation and retries the whole absolute snapshot.
            async with self._lock:
                # This is the snapshot's ordering point. Keep it with the
                # immutable context so delayed IPC cannot make old truth look
                # newer than a later snapshot.
                stamp_boot_ns = volume_context_stamp_boot_ns()
                before = self._persistence.load()
                level = (
                    int(before.listening_level)
                    if before is not None and before.listening_level is not None
                    else self._level
                )
                pre_mute = (
                    before.pre_mute_level
                    if before is not None
                    else self._pre_mute_level
                )
            state = VolumeState(level, pre_mute)
            canonical_db = percent_to_db(state.listening_level)
            # The canonical state interpretation must win over a lagging or
            # unreadable Camilla observation. A false hardware read may never
            # lower an already-known mute assertion.
            muted = state.muted
            current_db: float | None = None
            current_mute: bool | None = None
            if not self._camilla_volume_locked:
                current_db, current_mute = (
                    await self._read_camilla_volume_and_mute()
                )
            if current_db is None:
                source = await self._active_source()
                if await self._camilla_carries_level(source):
                    downstream_db = percent_to_db(state.effective_percent)
                else:
                    downstream_db = self._push_carrier_target_db(
                        muted, before.main_volume_db if before is not None else None,
                    )
            else:
                downstream_db = current_db
            if current_mute is not None:
                muted = muted or current_mute

            async with self._lock:
                after = self._persistence.load()
            before_key = (
                None if before is None else before.listening_level,
                None if before is None else before.pre_mute_level,
                None if before is None else before.main_volume_db,
            )
            after_key = (
                None if after is None else after.listening_level,
                None if after is None else after.pre_mute_level,
                None if after is None else after.main_volume_db,
            )
            if before_key == after_key:
                return EffectiveVolumeContext(
                    canonical_db=float(canonical_db),
                    downstream_db=float(downstream_db),
                    tts_envelope_lufs=tts_envelope_lufs_for_level(
                        state.listening_level,
                    ),
                    muted=bool(muted),
                    stamp_boot_ns=stamp_boot_ns,
                )
        async with self._lock:
            stamp_boot_ns = volume_context_stamp_boot_ns()
            latest = self._persistence.load()
        level = (
            int(latest.listening_level)
            if latest is not None and latest.listening_level is not None
            else self._level
        )
        state = VolumeState(
            level,
            latest.pre_mute_level if latest is not None else self._pre_mute_level,
        )
        downstream_db = (
            latest.main_volume_db
            if latest is not None
            else percent_to_db(state.effective_percent)
        )
        log_event(
            logger,
            "volume.context_snapshot_degraded",
            reason="intent_churn",
            attempts=3,
            level=logging.WARNING,
        )
        return EffectiveVolumeContext(
            canonical_db=float(percent_to_db(state.listening_level)),
            downstream_db=float(downstream_db),
            tts_envelope_lufs=tts_envelope_lufs_for_level(
                state.listening_level,
            ),
            muted=state.muted,
            stamp_boot_ns=stamp_boot_ns,
        )

    async def _publish_user_intent_context(
        self,
        source: Source,
        level: int,
        *,
        muted: bool,
    ) -> None:
        """Publish known intent before a slow or safety-critical actuator.

        Caller holds ``_lock``. This deliberately does not call the ordinary
        snapshot method (which would re-enter that lock). Non-muted
        Camilla-master paths skip the provisional message because their local
        write is already fast and publishing against the old downstream gain
        would create a needless two-ramp transient. Mute always publishes first
        so a wedged Camilla cannot delay the immediate TTS stop.
        """
        if (
            self._volume_context_publisher is None
            or (not muted and volume_mode(source) != VolumeMode.PUSH)
        ):
            return
        try:
            stamp_boot_ns = volume_context_stamp_boot_ns()
            current_mute: bool | None = None
            current_db: float | None
            if muted:
                current_db = None
            else:
                current_db, current_mute = (
                    await self._read_camilla_volume_and_mute()
                )
            if current_db is None:
                record = self._persistence.load()
                current_db = (
                    float(record.main_volume_db)
                    if record is not None and record.main_volume_db is not None
                    else 0.0
                )
            context = EffectiveVolumeContext(
                canonical_db=float(percent_to_db(level)),
                downstream_db=float(current_db),
                tts_envelope_lufs=tts_envelope_lufs_for_level(level),
                muted=bool(muted or current_mute is True),
                stamp_boot_ns=stamp_boot_ns,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            self._log_volume_context_publish_failure(e, phase="intent")
            return
        await self._publish_effective_volume_context(context, phase="intent")

    async def publish_volume_context(self, *, phase: str = "snapshot") -> None:
        """Best-effort absolute context update; never breaks volume control."""
        if self._volume_context_publisher is None:
            return
        try:
            context = await self.effective_volume_context()
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            self._log_volume_context_publish_failure(e, phase=phase)
            return
        await self._publish_effective_volume_context(context, phase=phase)

    async def _publish_effective_volume_context(
        self,
        context: EffectiveVolumeContext,
        *,
        phase: str,
    ) -> None:
        """Publish one already-snapshotted context without taking ``_lock``."""
        publisher = self._volume_context_publisher
        if publisher is None:
            return
        try:
            if not await publisher(context):
                # The active route names no mix stage, so nothing was sent.
                return
            log_event(
                logger,
                "volume.context_published",
                canonical_db=f"{context.canonical_db:.1f}",
                downstream_db=f"{context.downstream_db:.1f}",
                muted=str(context.muted).lower(),
                phase=phase,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            self._log_volume_context_publish_failure(e, phase=phase)

    @staticmethod
    def _log_volume_context_publish_failure(
        exc: Exception,
        *,
        phase: str,
    ) -> None:
        log_event(
            logger,
            "volume.context_publish_failed",
            exc_type=type(exc).__name__,
            detail=str(exc),
            phase=phase,
            level=logging.WARNING,
        )

    async def note_measurement_active(self, active: bool) -> None:
        """Pause/resume this process's 1 Hz Camilla drift reconciler and its
        level doors (see :meth:`_refuse_level_write_while_measuring`)."""
        async with self._reconcile_write_lock:
            self._measurement_active = bool(active)
            if self._measurement_active:
                self._measurement_active_at = _measurement_monotonic()
                self._measurement_lapse_logged = False

    async def get_camilla_target_db(self) -> float:
        """The absolute camilla.main_volume that should be in effect
        right now, ignoring any active duck. A duck holder releases against
        this so the fader lands at the canonical level regardless of what the
        duck delta was or what other writers did during the session.

        Refreshes from disk before deriving the effective level: jasper-control
        and jasper-voice each cache listening_level in memory, and a stale
        in-process value here would land camilla tens of dB from the user's
        actual intent after a duck."""
        self._refresh_from_disk()
        effective_level = self._effective_level()
        source = await self._active_source()
        if await self._camilla_carries_level(source):
            return percent_to_db(effective_level)
        muted = main_mute_for_level(effective_level)
        return self._push_carrier_target_db(
            muted, None if muted else self._persisted_main_volume_db(),
        )

    @staticmethod
    def _push_carrier_target_db(muted: bool, persisted_db: float | None) -> float:
        # Preserve content mute and failed-push attenuation through duck release.
        if muted:
            return percent_to_db(0)
        if persisted_db is not None and persisted_db < -RECONCILE_DRIFT_DB:
            return persisted_db
        return 0.0

    async def maybe_reconcile_camilla(self, source: Source | None = None) -> None:
        """Self-healing convergence: write `percent_to_db(listening_level)`
        back to camilla when `main_volume_db` has drifted from it.

        Pure resilience backstop: the normal write paths keep the two in
        sync, and this catches the divergence some other writer or transient
        left behind. Called from `VolumeObserver._tick` at 1 Hz, which passes
        its own already-resolved ``source`` so the preflight below does not
        re-probe `active_renderers()`; a candidate write still re-resolves
        fresh once it holds the mutation lock (see below).

        Gates (all must pass for a write to land):

        1. No voice session or correction measurement is active — the duck
           holder and the ramp own camilla there, and a write would clobber
           them.
        2. Active source is camilla-as-master (idle / AirPlay / USBSINK).
           On push-mode sources camilla is pinned at 0 dB by design and
           listening_level lives on the source's own slider.
        3. `|main_volume_db − expected| > RECONCILE_DRIFT_DB` — a dead band
           around camilla's normal jitter.
        4. No DSP writer holds the graph-mutation lock, and that gate
           precedes the drift directions below so it also defers a mute
           correction: an unmute mid-swap is the loud write the graph-swap
           bracket exists to prevent (`_graph_mutation_in_progress`).
        5. Deep QUIET drift is skipped, deep LOUD always corrected — a
           writer that left camilla far above the canonical level is unsafe,
           not a duck (`_deep_quiet_skip`).

        A write failure is non-fatal: WARN on the episode's first, then the
        observer keeps ticking (`volume.reconcile_write_failed`).
        """
        # A deep-quiet episode spans consecutive evaluations of the drift, so
        # a tick that returns before reaching one ends it and the next unowned
        # duck opens a new episode.
        reported, self._deep_quiet_skipped = self._deep_quiet_skipped, False
        self._lapse_stranded_measurement_flag()
        if self._voice_session_active or self._measurement_active:
            return
        await self._reconcile_loudness_level()
        try:
            source = source if source is not None else await self._active_source()
        except Exception:  # noqa: BLE001
            return
        if not await self._camilla_carries_level(source):
            return
        # Refresh from disk so a remote twist that landed via
        # jasper-control between our own set/adjust calls reflects
        # in `_level` before we compute the expected dB.
        self._refresh_from_disk()
        expected_level = self._effective_level()
        expected_db = percent_to_db(expected_level)
        expected_mute = main_mute_for_level(expected_level)
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
        if self._graph_mutation_in_progress():
            return
        if self._deep_quiet_skip(drift, mute_drift, reported):
            return
        # The preflight above avoids taking the cross-daemon lease on every
        # healthy 1 Hz tick; a candidate write then joins the same ordered
        # writer set as user commands and mux handoffs, re-reading every
        # routing/intent/physical fact inside both leases in case a
        # control-daemon command landed while the preflight read was in flight.
        async with self._mutation():
            async with self._reconcile_write_lock:
                if self._voice_session_active or self._measurement_active:
                    return
                try:
                    source = await self._active_source()
                except Exception:  # noqa: BLE001
                    return
                if not await self._camilla_carries_level(source):
                    return
                self._refresh_from_disk()
                expected_level = self._effective_level()
                expected_db = percent_to_db(expected_level)
                expected_mute = main_mute_for_level(expected_level)
                current_db, current_mute = (
                    await self._read_camilla_volume_and_mute()
                )
                if (
                    self._voice_session_active
                    or self._measurement_active
                    or current_db is None
                ):
                    return
                drift = expected_db - current_db
                mute_drift = (
                    current_mute is not None
                    and current_mute != expected_mute
                )
                if abs(drift) <= RECONCILE_DRIFT_DB and not mute_drift:
                    return
                if self._graph_mutation_in_progress():
                    return
                if self._deep_quiet_skip(drift, mute_drift, reported):
                    return
                try:
                    ok = await self._write_camilla_db_with_mute(
                        expected_db,
                        context="reconcile",
                    )
                    failure: str | None = None if ok else "rejected"
                except Exception as e:  # noqa: BLE001
                    failure = f"{type(e).__name__}: {e}"
                if failure is not None:
                    # A camilla that refuses writes refuses them at 1 Hz, so
                    # the retry itself is not news; the episode's open and
                    # close are.
                    self._write_failures += 1
                    if self._write_failures == 1:
                        log_event(logger, "volume.reconcile_write_failed",
                                  level=logging.WARNING, error=failure)
                    return
                self._persistence.save_now(expected_db)
                if self._write_failures:
                    log_event(logger, "volume.reconcile_write_recovered",
                              consecutive_failures=self._write_failures)
                    self._write_failures = 0
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

    def _graph_mutation_in_progress(self) -> bool:
        """Stand this tick down while a DSP writer owns CamillaDSP's graph.

        The graph-swap bracket takes no `VolumeOwner` claim for the fader it
        ducks, and runs in whichever process is applying — so the answer has
        to cross processes, and the writer lock is the fact that already does
        (ADR-0213). Synchronous by contract, like the owner's own readers.

        Fails open — no probe, an unreadable lock, a raising controller —
        because the loud-direction correction is a safety backstop and must
        not go inert on an infrastructure problem (ADR-0177).
        """
        try:
            held = self._camilla.graph_mutation_in_progress()
        except Exception as e:  # noqa: BLE001
            self._graph_probe_failures += 1
            if self._graph_probe_failures == 1:
                log_event(logger, "volume.graph_probe_failed",
                          level=logging.WARNING,
                          error=f"{type(e).__name__}: {e}")
            return False
        if self._graph_probe_failures:
            log_event(logger, "volume.graph_probe_recovered",
                      consecutive_failures=self._graph_probe_failures)
            self._graph_probe_failures = 0
        if held is not True:
            return False
        log_event(logger, "volume.reconcile_deferred", reason="dsp_writer_lock")
        return True

    def _deep_quiet_skip(
        self, drift_db: float, mute_drift: bool, reported: bool,
    ) -> bool:
        """True when camilla sits far below its slider under a duck we do not
        own. Edge-reported, not per tick: the stranded audition that motivates
        the threshold holds as long as its owner lives. Delete with
        `RECONCILE_DUCK_SKIP_DB` (#3038).
        """
        skipping = drift_db >= RECONCILE_DUCK_SKIP_DB and not mute_drift
        if skipping and not reported:
            log_event(logger, "volume.reconcile_skipped",
                      reason="deep_quiet_unowned", drift_db=f"{drift_db:+.2f}")
        self._deep_quiet_skipped = skipping
        return skipping

    async def _active_source(self) -> Source:
        """Pick the active source. Multiple-source-active is rare
        (mux preempts in <1 s) but possible during transitions; pick
        a stable priority: airplay > spotify > bluetooth > usbsink
        > idle.

        Manual source selection is an audible fan-in policy override:
        if mux reports one, prefer it even when raw renderer probes
        say a different source is active. Fail soft to raw probes when
        mux is unavailable.
        """
        try:
            selected = await self._backend.selected_source()
            # A measurement lease returns a fan-in lane label, not a source;
            # only that case falls through to raw probes. During a handoff,
            # mux retains its last committed source, including true idle.
            if selected in MUSIC_SOURCE_VALUES:
                return Source(selected)
            if selected == Source.IDLE.value:
                return Source.IDLE
        except Exception as e:  # noqa: BLE001
            logger.debug("selected_source() failed (%s); using probes", e)
        try:
            active = await self._backend.active_renderers()
        except Exception as e:  # noqa: BLE001
            logger.debug("active_renderers() failed (%s); treating as idle", e)
            return Source.IDLE
        if active.get(SOURCE_TO_ACTIVE_KEY[Source.AIRPLAY]):
            return Source.AIRPLAY
        if active.get(SOURCE_TO_ACTIVE_KEY[Source.SPOTIFY]):
            return Source.SPOTIFY
        if active.get(SOURCE_TO_ACTIVE_KEY[Source.BLUETOOTH]):
            return Source.BLUETOOTH
        if active.get(SOURCE_TO_ACTIVE_KEY[Source.USBSINK]):
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

    async def _set_loudness_level(self, level: int) -> bool:
        if self._measurement_holds_fader():
            return False
        return await self._camilla.set_loudness_volume_db(
            percent_to_db(max(0, min(100, int(level)))),
            best_effort=True,
        )

    async def _reconcile_loudness_level(self) -> None:
        self._refresh_from_disk()
        target = percent_to_db(self._effective_level())
        current = await self._camilla.get_loudness_volume_db(best_effort=True)
        if current is None or abs(current - target) <= 0.01:
            return
        async with self._mutation():
            async with self._reconcile_write_lock:
                if (
                    self._voice_session_active
                    or self._measurement_active
                    or self._graph_mutation_in_progress()
                ):
                    return
                await self._set_loudness_level(self._effective_level())

    async def _camilla_locked(self) -> bool | None:
        if self._camilla_volume_locked:
            return True
        if self._duck_active_probe is None:
            return False
        try:
            return await self._duck_active_probe()
        except Exception as e:  # noqa: BLE001
            logger.warning("duck_active_probe raised %s; treating as unknown", e)
            return None

    @staticmethod
    def _main_mute_for_db(db: float) -> bool:
        return float(db) <= percent_to_db(0) + MUTE_DB_EPSILON

    async def _read_camilla_volume_and_mute(
        self,
    ) -> tuple[float | None, bool | None]:
        result = await self._camilla.get_volume_and_mute(best_effort=True)
        if result is not None:
            db, muted = result
            return float(db), bool(muted)
        return None, None

    async def _set_camilla_main_mute(
        self, muted: bool, *, context: str,
    ) -> bool:
        target = bool(muted)
        ok = await self._camilla.set_main_mute(target, best_effort=True)
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

    @property
    def volume_owner(self) -> VolumeOwner:
        """This process's fader owner, for the claim holders that share it.

        Transient-duck holders share this instance's owner; separate
        coordinators have separate claim ledgers.
        """
        return self._volume_owner

    async def _write_fader_db(self, db: float) -> bool:
        return await self._camilla.set_volume_db(db, best_effort=True)

    async def _read_fader_db(self) -> float | None:
        return await self._camilla.get_volume_db(best_effort=True)

    async def _write_camilla_db_with_mute(
        self, db: float, *, context: str,
    ) -> bool:
        """Land the household level, and the mute that goes with it.

        The dB half is the owner's — this is the coordinator declaring the
        HOUSEHOLD claim, and every fader write it makes goes through that one
        arbiter. The mute half stays here: ``main_mute`` is a separate flag
        with its own two writers, and folding it into a level claim would give
        the owner a second question to answer.
        """
        target_mute = self._main_mute_for_db(db)
        if target_mute:
            mute_ok = await self._set_camilla_main_mute(
                True, context=context,
            )
            _volume_ok = await self._volume_owner.declare_household_level_db(db)
            # Final content silence comes from main_mute. The dB floor is a
            # defense-in-depth fallback if the mute flag is later lost.
            return bool(mute_ok)

        volume_ok = await self._volume_owner.declare_household_level_db(db)
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
        saved so the duck release lands safe.
        """
        camilla_locked = await self._camilla_locked()
        if camilla_locked is True:
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

    def _stamp_outbound(self, source: Source) -> None:
        stamp_outbound(self._last_outbound, source)

    def _is_own_echo(self, source: Source, observed_level: int) -> bool:
        return is_own_echo(self._last_outbound, source, observed_level)

    def _is_recent_cross_process_write(self, observed_level: int) -> bool:
        return is_recent_cross_process_write(
            self._persistence, self._level, observed_level,
        )

    # ------------------------------------------------------------------
    # Source-side dispatchers
    # ------------------------------------------------------------------

    async def _set_airplay(self, level: int) -> bool:
        """AirPlay is camilla-as-master.

        shairport-sync still exposes SetAirplayVolume, but modern
        iOS/macOS AirPlay 2 sessions often omit DACP-ID/Active-Remote
        and receiver-originated volume reflection silently no-ops, so
        this direction stays closed (ADR-0176): a JTS-side volume change
        moves CamillaDSP and leaves the sender's slider where it was.
        The sender's slider reaches us the other way, through shairport's
        volume hook (ADR-0206).
        """
        return await self._set_camilla(level)

    async def _set_spotify(self, level: int) -> bool:
        ok = await volume_push_sources.push_spotify_volume(
            self._spotify_router, self._spotify_device_name, level,
        )
        if ok:
            self._stamp_outbound(Source.SPOTIFY)
        return ok

    async def _set_bluetooth(self, level: int) -> bool:
        ok = await volume_push_sources.push_bluetooth_volume(level)
        if ok:
            self._stamp_outbound(Source.BLUETOOTH)
        return ok

    async def _set_camilla(self, level: int) -> bool:
        db = percent_to_db(level)
        target_mute = main_mute_for_level(level)
        # The duck owns the fader, but mute and canonical intent still apply.
        # An unknown cross-daemon lock fails open so the remote remains usable.
        locally_locked = self._camilla_volume_locked
        if await self._camilla_locked() is True:
            context = (
                "set_camilla_voice_session" if locally_locked
                else "set_camilla_session_signaled"
            )
            mute_ok = await self._set_camilla_main_mute(target_mute, context=context)
            log_event(
                logger,
                "volume.deferred",
                # `level` collides with log_event's level= param → fields=.
                fields={
                    "reason": "camilla_volume_locked" if locally_locked else "session_signaled",
                    "level": f"{level}%",
                    "target_db": f"{db:.1f}",
                    "muted": str(target_mute).lower(),
                    "result": "main_mute_applied" if mute_ok else "main_mute_failed",
                },
            )
            return bool(mute_ok)
        # best_effort: remote twist arriving during a 2s camilla restart
        # blip should still update listening_level on disk and persist
        # main_volume_db, even if the actual write didn't land. The
        # next set_volume call (or a source-transition) will re-apply
        # once camilla is back.
        ok = await self._write_camilla_db_with_mute(
            db, context="set_camilla",
        )
        # main_volume IS what the user is controlling in idle. Persist
        # it explicitly so diagnostics and the on-disk schema keep a
        # coherent dB mirror of listening_level.
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


def build_volume_coordinator(
    *,
    camilla: "CamillaController",
    backend: "RendererClient",
    spotify_router: Any | None = None,
    duck_active_probe: CamillaLockProbe | None = None,
) -> VolumeCoordinator:
    """The daemon-side assembly (mux, jasper-control): persisted level loaded,
    speaker name and context publisher wired, around the actuators whose
    acquisition differs per process. One-shot readers build their own."""
    coordinator = VolumeCoordinator(
        camilla=camilla,
        persistence=VolumePersistence(volume_state_path()),
        backend=backend,
        spotify_router=spotify_router,
        spotify_device_name=speaker_runtime_name(),
        duck_active_probe=duck_active_probe,
        volume_context_publisher=volume_context_publisher_for_runtime(os.environ),
    )
    coordinator.load_persisted_level()
    return coordinator
