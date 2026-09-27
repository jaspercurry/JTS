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
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
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
from .volume_echo import (
    is_own_echo,
    is_recent_cross_process_write,
    stamp_outbound,
)
from .volume_carrier import CamillaCarrier
from .volume_measurement_gate import MeasurementGate
from .volume_owner import VolumeOwner
from .volume_scales import native_to_listening_level
from .volume_curve import (
    guard_in_effect,
    main_mute_for_level,
    percent_to_db,
)
from .volume_handoff import VolumeHandoff
from .volume_reconcile import VolumeReconciler, converged
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

    Short-lived builders in a process that registered a fader owner pass it
    as ``volume_owner``; ``None`` builds this coordinator's own
    (``CamillaCarrier``).
    """

    def __init__(
        self,
        *,
        camilla: "CamillaController",
        persistence: VolumePersistence,
        backend: "RendererClient",
        spotify_router: Any | None = None,
        spotify_device_name: str = "JTS",
        volume_context_publisher: VolumeContextPublisher | None = None,
        handoff_settle_sec: float = 0.45,
        push_settle_sec: float = 0.75,
        volume_owner: VolumeOwner | None = None,
    ) -> None:
        # The coordinator holds the HOUSEHOLD claim — the standing level the
        # speaker plays at when nothing outranks it.
        self._carrier = CamillaCarrier(camilla=camilla, volume_owner=volume_owner)
        self._persistence = persistence
        self._backend = backend
        # Multi-account Spotify router for Web API volume control.
        # librespot 0.8.0 has no local HTTP control API, so to set
        # Spotify volume we go: coordinator → spotipy → Spotify
        # cloud → spirc → librespot. Optional; if None or empty,
        # the Spotify push is a no-op (logged as warning).
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
        # request coordinators in jasper-control always read False.
        self._voice_session_active: bool = False
        self._measurement = MeasurementGate()
        self._reconciler = VolumeReconciler(
            carrier=self._carrier,
            measurement=self._measurement,
            graph_mutation_in_progress=lambda: camilla.graph_mutation_in_progress(),
            voice_session_active=lambda: self._voice_session_active,
            active_source=lambda: self._active_source(),
            refresh=lambda: self._refresh_from_disk(),
            effective_level=lambda: self._effective_level(),
            mutation=lambda: self._mutation(),
            save_now=lambda db: self._persistence.save_now(db),
        )
        self._volume_context_publisher = volume_context_publisher
        self._handoff = VolumeHandoff(
            effective_level=lambda: self.get_volume_state().effective_percent,
            read_carrier=lambda: self._carrier.read_volume_and_mute(),
            persisted_carrier=lambda: self._persisted_main_volume_db(),
            write_guard=lambda db, *, context, persist: self._set_camilla_db(
                db, context=context, persist=persist,
            ),
            push_source=lambda source, level: self._push_source(source, level),
            write_level=lambda level: self._set_camilla(level),
            voice_session_active=lambda: self._voice_session_active,
            active_source=lambda: self._active_source(),
            refresh=lambda: self._refresh_from_disk(),
            mutation=lambda: self._mutation(),
            publish=lambda: self.publish_volume_context(),
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
            if volume_mode(source) == VolumeMode.CAMILLA_MASTER:
                await self._set_camilla(target_level)
            else:
                pin_db = percent_to_db(0) if main_mute_for_level(target_level) else 0.0
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

    async def set_listening_level(self, percent: int) -> int:
        """Set canonical listening_level to `percent` (clamped to 0..100).
        Dispatches to the active source (or camilla, if idle).
        Persists. Returns the level that was actually applied."""
        target = max(0, min(100, int(percent)))
        async with self._mutation():
            self._refresh_from_disk()
            self._measurement.refuse_level_write()
            self._level = target
            self._pre_mute_level = None  # any explicit set clears mute state
            self._mute_token = None
            self._persistence.save_mute_state(None, None)
            await self._publish_then_dispatch(
                target, context="set_listening_level_intent",
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
            self._measurement.refuse_level_write()
            self._level = target
            self._pre_mute_level = None
            self._mute_token = None
            self._persistence.save_mute_state(None, None)
            await self._publish_then_dispatch(
                target, context="adjust_listening_level_intent",
            )
        await self.publish_volume_context(phase="converged")
        return target

    async def _publish_then_dispatch(
        self, level: int, *, context: str, restore_level: int | None = None,
    ) -> None:
        """The verbs' shared tail: publish intent, assert ``main_mute`` when
        the intent mutes, then dispatch ``level``. Caller holds ``_mutation``.

        A temporary mute passes its ``restore_level``: it always mutes, fan-in
        hears that level, muted, and the dispatched level is not persisted.
        """
        muted = restore_level is not None or main_mute_for_level(level)
        source = await self._active_source()
        # Fan-in is the immediate TTS stop and does not depend on Camilla
        # being healthy. Publish before touching the final-output backstop.
        await self._publish_user_intent_context(
            source,
            level if restore_level is None else restore_level,
            muted=muted,
        )
        # Final-output mute is local and safety-critical; never wait for a
        # Spotify/BT cloud or protocol round trip before asserting it.
        if muted:
            await self._carrier.write_main_mute(True, context=context)
        await self._dispatch(level, persist=restore_level is None, source=source)

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
        await self._publish_then_dispatch(
            0, context="mute_intent", restore_level=saved,
        )
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
        self._measurement.refuse_level_write()
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
        await self._publish_then_dispatch(target, context="unmute_intent")
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
            if level > 0 and self._measurement.holds_fader():
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
                if level == self._level and self._pre_mute_level is None:
                    if volume_mode(source) == VolumeMode.CAMILLA_MASTER:
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
                    if volume_mode(source) == VolumeMode.CAMILLA_MASTER:
                        await self._sync_camilla_observed_level(source, level)
                    else:
                        await self._handoff.confirm_push_mode_carrier(
                            source,
                            level,
                            context=f"observe_{source.value}_push_confirmed",
                            include_live_guard=True,
                        )
                    publish_needed = True
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
        current_db, current_mute = await self._carrier.read_volume_and_mute()
        if converged(expected_db, expected_mute, current_db, current_mute):
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
        uses CamillaDSP as the AirPlay speaker-volume surface (ADR-0176);
        the sender's slider reaches us through shairport's volume hook
        (ADR-0206).
        """
        source = source if source is not None else await self._active_source()
        try:
            if volume_mode(source) == VolumeMode.PUSH:
                await self._handoff.push_or_guard(
                    source,
                    level,
                    confirm_context=f"dispatch_{source.value}_push_confirmed",
                    guard_context=f"dispatch_{source.value}_degraded",
                    warning_prefix=f"{source.value} volume dispatch failed",
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
        """The observer's source transition: :meth:`VolumeHandoff.apply_transition`."""
        await self._handoff.apply_transition(prev_source, current_source)

    def note_voice_session(self, active: bool) -> None:
        """Called by voice_daemon's WakeLoop on session start/end. While a
        session is active, `apply_active_source_transition` and
        `maybe_reconcile_camilla` stand down; volume writes still land
        (ADR-0376)."""
        self._voice_session_active = bool(active)

    async def effective_volume_context(self) -> EffectiveVolumeContext:
        """Return one mutation-coherent snapshot without holding IPC open."""
        return await self._effective_volume_context()

    async def _effective_volume_context(self) -> EffectiveVolumeContext:
        """Return the absolute volume facts consumed by fan-in.

        The canonical dB value represents user intent. ``downstream_db`` is
        Camilla's actual gain when readable, with the coordinator's safe target
        as a fail-soft fallback.
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
            current_db, current_mute = await self._carrier.read_volume_and_mute()
            if current_db is None:
                source = await self._active_source()
                if volume_mode(source) == VolumeMode.CAMILLA_MASTER:
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
                    await self._carrier.read_volume_and_mute()
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
        level doors (see :meth:`MeasurementGate.refuse_level_write`)."""
        await self._measurement.note_active(active)

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
        if volume_mode(source) == VolumeMode.CAMILLA_MASTER:
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
        if guard_in_effect(persisted_db):
            return persisted_db
        return 0.0

    async def maybe_reconcile_camilla(self, source: Source | None = None) -> None:
        """The 1 Hz drift backstop: :meth:`VolumeReconciler.maybe_reconcile_camilla`."""
        await self._reconciler.maybe_reconcile_camilla(source)

    @property
    def reconcile_deferred(self) -> bool:
        """Whether the last reconcile tick stood down for a DSP writer or a
        measurement."""
        return self._reconciler.reconcile_deferred

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

    @property
    def volume_owner(self) -> VolumeOwner:
        """This process's fader owner, for the claim holders that share it.

        Coordinators built without one have separate claim ledgers.
        """
        return self._carrier.volume_owner

    async def _set_camilla_db(
        self, db: float, *, context: str, persist: bool,
    ) -> bool:
        """Set raw Camilla main_volume dB and its mute; False when a write
        fails. With `persist=True`, the dB is saved only once it lands."""
        ok = await self._carrier.write_db_with_mute(db, context=context)
        if ok and persist:
            self._persistence.save_now(db)
        return bool(ok)

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

    async def _push_source(self, source: Source, level: int) -> bool:
        """Push `level` to a push-mode source's own slider; stamp the echo."""
        if source == Source.SPOTIFY:
            ok = await volume_push_sources.push_spotify_volume(
                self._spotify_router, self._spotify_device_name, level,
            )
        elif source == Source.BLUETOOTH:
            ok = await volume_push_sources.push_bluetooth_volume(level)
        else:
            logger.warning(
                "source handoff: %s is not a push-mode source", source.value,
            )
            return False
        if ok:
            self._stamp_outbound(source)
        return bool(ok)

    async def _set_camilla(self, level: int) -> bool:
        db = percent_to_db(level)
        target_mute = main_mute_for_level(level)
        # best_effort: remote twist arriving during a 2s camilla restart
        # blip should still update listening_level on disk and persist
        # main_volume_db, even if the actual write didn't land. The
        # next set_volume call (or a source-transition) will re-apply
        # once camilla is back.
        ok = await self._carrier.write_db_with_mute(
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
    volume_owner: VolumeOwner | None = None,
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
        volume_context_publisher=volume_context_publisher_for_runtime(os.environ),
        volume_owner=volume_owner,
    )
    coordinator.load_persisted_level()
    return coordinator
