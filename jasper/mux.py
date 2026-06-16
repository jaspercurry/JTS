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
            PUT /me/player/pause to any account that has the configured
            speaker device in its list. Tier 2 (added 2026-05-22) is
            `systemctl restart librespot.service` if Tier 1 fails
            — guarantees librespot releases its private fan-in lane
            so the new winner is heard alone. Tier 2 is still useful
            after the 2026-05-26 fan-in cutover: renderers no longer
            share one ALSA device, so an un-pauseable librespot would
            keep streaming into its own lane and be summed alongside
            the new winner. Off-switch:
            JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled.
  AirPlay (shairport-sync):
    detect: MPRIS PlaybackStatus == "Playing"
    preempt: MPRIS Stop method, falling back to Pause if Stop is not
            available. Stop asks shairport-sync to tear down playback
            instead of leaving a hidden paused AirPlay session behind
            while another renderer owns the fan-in gate.
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
            usbsink_substream). When all other sources go idle, we
            release the preempt so user-host transitions (pause
            then play on Mac) can re-take the speaker.

"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from jasper.log_event import log_event

from . import librespot_state, mux_mode_persistence
from .control import restart_broker
from .music_sources import MUSIC_SOURCES, SOURCE_TO_FANIN_LABEL, Source
from .source_state import (
    airplay_playing,
    bluetooth_playing,
    spotify_playing,
    usbsink_playing,
)

logger = logging.getLogger(__name__)


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
FANIN_CONTROL_SOCKET = os.environ.get(
    "JASPER_FANIN_CONTROL_SOCKET", "/run/jasper-fanin/control.sock",
)
MUX_CONTROL_SOCKET = os.environ.get(
    "JASPER_MUX_CONTROL_SOCKET", "/run/jasper-mux/control.sock",
)
# Durable home for the source-selection mode (auto vs manual + the
# pinned source). Persisted so a household's manual pin survives the
# Restart=always deploy/restart cycle. RuntimeDirectory is wiped on
# restart, so this lives under /var/lib/jasper, not /run.
MUX_MODE_STATE_PATH = os.environ.get(
    "JASPER_MUX_MODE_STATE_PATH", mux_mode_persistence.DEFAULT_PATH,
)
SHAIRPORT_MPRIS_BUS = "org.mpris.MediaPlayer2.ShairportSync"
SHAIRPORT_MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


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
        default_factory=lambda: {s: False for s in MUSIC_SOURCES},
    )


class Mux:
    POLL_INTERVAL_SEC = 1.0

    def __init__(
        self,
        librespot_state_path: str = librespot_state.DEFAULT_PATH,
        volume_coordinator: Any | None = None,
        mode_state_path: str = MUX_MODE_STATE_PATH,
    ) -> None:
        self._librespot_state_path = librespot_state_path
        self._mode_state_path = mode_state_path
        self._state = _State()
        self._winner: Optional[Source] = None
        # Restore a household's manual source pin across restarts. Fails
        # open to None (auto / latest-source-wins) on a missing or
        # corrupt file — the pre-persistence behaviour. The fan-in gate
        # is reasserted from this on the first tick (_reassert_manual_source).
        self._manual_source: Optional[Source] = mux_mode_persistence.read_manual_source(
            mode_state_path,
        )
        if self._manual_source is not None:
            log_event(
                logger,
                "source.manual_restored",
                **{
                    "source": self._manual_source.value,
                    "from": mode_state_path,
                },
            )
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
        self._volume_coordinator = volume_coordinator
        self._last_handoff: dict[str, Any] | None = None
        self._handoff_seq = 0
        self._transition_lock = asyncio.Lock()
        self._pending_auto_target: Source | None = None

    async def run(self) -> None:
        logger.info(
            "jasper-mux starting (poll=%.1fs, librespot_state=%s)",
            self.POLL_INTERVAL_SEC, self._librespot_state_path,
        )
        await self._fanin_none_best_effort(reason="startup")
        control_task = asyncio.create_task(self._run_control_server())
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
            control_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await control_task
            if self._volume_coordinator is not None:
                with contextlib.suppress(Exception):
                    await self._volume_coordinator.aclose()
            with contextlib.suppress(Exception):
                await self._http.aclose()

    async def _probe_sources(self) -> dict[Source, bool]:
        spotify, airplay, bluetooth, usbsink = await asyncio.gather(
            spotify_playing(self._librespot_state_path),
            airplay_playing(),
            bluetooth_playing(),
            usbsink_playing(),
        )
        return {
            Source.SPOTIFY: spotify,
            Source.AIRPLAY: airplay,
            Source.BLUETOOTH: bluetooth,
            Source.USBSINK: usbsink,
        }

    async def _tick(self) -> None:
        current = await self._probe_sources()

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

        if self._manual_source is not None:
            await self._reassert_manual_source()
            return

        target: Source | None = None
        transition_reason = ""
        if (
            self._pending_auto_target is not None
            and current.get(self._pending_auto_target, False)
            and self._pending_auto_target != self._winner
        ):
            target = self._pending_auto_target
            transition_reason = "auto_retry"
        elif self._pending_auto_target is not None:
            self._pending_auto_target = None

        if target is None and newly_started:
            target = newly_started[-1]
            transition_reason = "auto_new_source"
            logger.info(
                "source transition: %s started (was %s, age=%d ticks)",
                target.value,
                self._winner.value if self._winner else "none",
                self._winner_age_ticks,
            )
        elif self._winner is not None and not current.get(self._winner, False):
            active_sources = self._active_sources(current)
            target = active_sources[-1] if active_sources else None
            transition_reason = "auto_winner_stopped"
        elif self._winner is None:
            active_sources = self._active_sources(current)
            if active_sources:
                target = active_sources[-1]
                transition_reason = "auto_startup_active"

        if target is not None and target != self._winner:
            async with self._transition_lock:
                if self._manual_source is not None:
                    return
                # If the new winner is USBSINK and it's currently in our
                # preempted set, the daemon's bridge is silent. The fresh
                # inactive→active edge means the user did "pause then
                # play" on the host — release the preempt so we forward
                # audio again. Inside the lock (like select_source /
                # auto_select) so a concurrent manual selection can't
                # interleave between the release and the handoff.
                if target == Source.USBSINK and self._usbsink_preempted:
                    await self._usbsink_set_preempt(
                        False, reason="new_transition",
                    )
                prev_winner = self._winner or Source.IDLE
                selected = await self._transition_to_source_locked(
                    prev_winner, target, reason=transition_reason,
                )
                if selected:
                    self._winner = target
                    self._pending_auto_target = None
                    self._winner_age_ticks = 0
                else:
                    self._pending_auto_target = target
                    if self._winner is None:
                        await self._fanin_none_best_effort(
                            reason="handoff_prepare_failed",
                        )
            if not selected:
                self._state.playing = current
                return

            # Pause every OTHER source that's currently active after
            # the fan-in gate has moved. Slow cloud/Web API pause
            # paths should not delay a safe source switch. Best-effort
            # per source: one renderer's pause raising (Web API error,
            # busctl gone) must not abort pausing the rest.
            for source, is_playing in current.items():
                if source != target and is_playing:
                    await self._pause_best_effort(
                        source, reason=transition_reason,
                    )
        elif target is None:
            if self._winner is not None and current.get(self._winner, False):
                await self._reassert_auto_winner(current)
            else:
                self._winner = None
                self._pending_auto_target = None
                await self._fanin_none_best_effort(reason="auto_idle")

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

    async def select_source(self, source: Source) -> dict[str, Any]:
        """Manual source selection from the web UI.

        Fan-in enforces the audible lane. This deliberately does not
        pause, disconnect, or disable any renderer: the source selector
        chooses what the speaker passes through, while the `/sources/`
        wizard remains the on/off surface.
        """
        async with self._transition_lock:
            previous = self._winner or self._manual_source or Source.IDLE
            self._pending_auto_target = None
            selected = await self._transition_to_source_locked(
                previous, source, reason="manual",
            )
            if selected:
                self._manual_source = source
                self._pending_auto_target = None
                self._winner = source
                self._winner_age_ticks = 0
                mux_mode_persistence.write_mode(self._mode_state_path, source)
                if self._usbsink_preempted:
                    await self._usbsink_set_preempt(
                        False, reason="manual_select",
                    )
            elif self._winner is None:
                await self._fanin_none_best_effort(
                    reason="manual_handoff_failed",
                )
        if not selected:
            current = await self._probe_sources()
            self._state.playing = current
            log_event(
                logger,
                "source.manual_select_failed",
                source=source.value,
                level=logging.WARNING,
            )
            return self._status_payload(current)
        current = await self._probe_sources()
        self._state.playing = current
        log_event(logger, "source.manual_select", source=source.value)
        return self._status_payload(current)

    async def auto_select(self) -> dict[str, Any]:
        """Return to latest-source-wins behavior."""
        current = await self._probe_sources()
        active_sources = self._active_sources(current)
        if active_sources:
            new_winner = active_sources[-1]
            async with self._transition_lock:
                previous = self._winner or self._manual_source or Source.IDLE
                selected = await self._transition_to_source_locked(
                    previous, new_winner, reason="auto_select",
                )
                if selected:
                    if new_winner == Source.USBSINK and self._usbsink_preempted:
                        await self._usbsink_set_preempt(
                            False, reason="auto_select",
                        )
                    self._winner = new_winner
                    self._manual_source = None
                    self._pending_auto_target = None
                    self._winner_age_ticks = 0
                    mux_mode_persistence.write_mode(self._mode_state_path, None)
                else:
                    self._pending_auto_target = new_winner
                    if self._winner is None:
                        await self._fanin_none_best_effort(
                            reason="auto_select_handoff_failed",
                        )
            if not selected:
                self._state.playing = current
                log_event(
                    logger,
                    "source.auto_select_failed",
                    source=new_winner.value,
                    level=logging.WARNING,
                )
                return self._status_payload(current)
            for source in active_sources:
                if source != new_winner:
                    await self._pause_best_effort(
                        source, reason="auto_select",
                    )
        else:
            async with self._transition_lock:
                self._winner = None
                self._manual_source = None
                self._pending_auto_target = None
                mux_mode_persistence.write_mode(self._mode_state_path, None)
                await self._fanin_none()

        self._state.playing = current
        if self._usbsink_preempted:
            others_playing = any(
                playing
                for src, playing in current.items()
                if src != Source.USBSINK
            )
            if not others_playing:
                await self._usbsink_set_preempt(
                    False, reason="manual_auto_others_idle",
                )
        log_event(logger, "source.auto_select")
        return self._status_payload(current)

    def _status_payload(
        self, current: dict[Source, bool] | None = None,
    ) -> dict[str, Any]:
        current = current or self._state.playing
        active = self._active_source_name(current)
        return {
            "mode": "manual" if self._manual_source is not None else "auto",
            "selected_source": (
                self._manual_source.value if self._manual_source else None
            ),
            "active_source": active,
            "winner": self._winner.value if self._winner else None,
            "last_handoff": self._last_handoff,
            "sources": {
                source.value: {"playing": bool(current.get(source, False))}
                for source in MUSIC_SOURCES
            },
        }

    def _active_source_name(self, current: dict[Source, bool]) -> str:
        if self._manual_source is not None:
            return self._manual_source.value
        if self._winner is not None and current.get(self._winner, False):
            return self._winner.value
        return "idle"

    def _active_sources(self, current: dict[Source, bool]) -> list[Source]:
        return [source for source in MUSIC_SOURCES if current.get(source, False)]

    async def _reassert_manual_source(self) -> None:
        async with self._transition_lock:
            source = self._manual_source
            if source is None:
                return
            await self._fanin_select_best_effort(
                source, reason="manual_tick",
            )
            if self._usbsink_preempted:
                await self._usbsink_set_preempt(False, reason="manual_mode")
            self._winner = source
            self._pending_auto_target = None

    async def _reassert_auto_winner(
        self, current: dict[Source, bool],
    ) -> None:
        async with self._transition_lock:
            if self._manual_source is not None:
                return
            winner = self._winner
            if winner is None or not current.get(winner, False):
                return
            await self._fanin_select_best_effort(winner, reason="auto_tick")

    def _ensure_volume_coordinator(self) -> Any:
        if self._volume_coordinator is not None:
            return self._volume_coordinator
        from .camilla import CamillaController
        from .renderer import RendererClient
        from .speaker_name import runtime_name as speaker_runtime_name
        from .volume_coordinator import VolumeCoordinator
        from .volume_persistence import VolumePersistence

        camilla = CamillaController(
            host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
            port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
        )
        persistence = VolumePersistence(
            os.environ.get(
                "JASPER_VOLUME_STATE_PATH",
                "/var/lib/jasper/speaker_volume.json",
            ),
        )
        backend = RendererClient(librespot_state_path=self._librespot_state_path)
        coordinator = VolumeCoordinator(
            camilla=camilla,
            persistence=persistence,
            backend=backend,
            spotify_router=self._ensure_spotify_router(),
            spotify_device_name=speaker_runtime_name(),
            duck_active_probe=_make_duck_active_probe(),
            handoff_settle_sec=float(os.environ.get(
                "JASPER_SOURCE_HANDOFF_SETTLE_SEC", "0.45",
            )),
            push_settle_sec=float(os.environ.get(
                "JASPER_SOURCE_PUSH_SETTLE_SEC", "0.75",
            )),
        )
        coordinator.load_persisted_level()
        self._volume_coordinator = coordinator
        return coordinator

    async def _transition_to_source(
        self, prev_source: Source, source: Source, *, reason: str,
    ) -> bool:
        async with self._transition_lock:
            return await self._transition_to_source_locked(
                prev_source, source, reason=reason,
            )

    async def _transition_to_source_locked(
        self, prev_source: Source, source: Source, *, reason: str,
    ) -> bool:
        started = time.monotonic()
        handoff_id = self._next_handoff_id()
        log_event(
            logger,
            "source.handoff_start",
            **{
                "id": handoff_id,
                "from": prev_source.value,
                "to": source.value,
                "reason": reason,
            },
        )
        coordinator = self._ensure_volume_coordinator()
        handoff = await coordinator.prepare_source_handoff(
            prev_source, source, reason=reason,
        )
        if not getattr(handoff, "ok", False):
            self._record_handoff(
                handoff, started, handoff_id=handoff_id, result=handoff.result,
            )
            log_event(
                logger,
                "source.handoff",
                **{
                    "id": handoff_id,
                    "from": prev_source.value,
                    "to": source.value,
                    "reason": reason,
                    "result": handoff.result,
                    "detail": handoff.detail,
                },
                level=logging.WARNING,
            )
            return False
        try:
            await self._fanin_select(source)
        except Exception as e:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await coordinator.abort_source_handoff(handoff)
            self._record_handoff(
                handoff, started,
                handoff_id=handoff_id,
                result="fanin_select_failed",
            )
            log_event(
                logger,
                "source.handoff",
                **{
                    "id": handoff_id,
                    "from": prev_source.value,
                    "to": source.value,
                    "reason": reason,
                    "result": "fanin_select_failed",
                    "detail": str(e),
                },
                level=logging.WARNING,
            )
            return False
        try:
            finalized = await coordinator.finalize_source_handoff(handoff)
        except Exception as e:  # noqa: BLE001
            finalized = False
            log_event(
                logger,
                "source.handoff_finalize_failed",
                **{
                    "id": handoff_id,
                    "from": prev_source.value,
                    "to": source.value,
                    "reason": reason,
                    "detail": str(e),
                },
                level=logging.WARNING,
            )
        result = handoff.result if finalized else "finalize_failed"
        self._record_handoff(
            handoff, started, handoff_id=handoff_id, result=result,
        )
        log_event(
            logger,
            "source.handoff",
            # `from` is a Python keyword and `level` (the volume level)
            # collides with log_event's reserved level= param, so every
            # field rides the explicit fields= mapping (order preserved).
            fields={
                "id": handoff_id,
                "from": prev_source.value,
                "to": source.value,
                "reason": reason,
                "level": handoff.level,
                "guard_db": _fmt_db(handoff.guard_db),
                "camilla_before": _fmt_db(handoff.camilla_before_db),
                "prev_mode": handoff.prev_mode.value,
                "target_mode": handoff.current_mode.value,
                "push_ok": handoff.push_ok,
                "settled_ms": handoff.settled_ms,
                "result": result,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return True

    def _next_handoff_id(self) -> int:
        self._handoff_seq += 1
        return self._handoff_seq

    def _record_handoff(
        self, handoff: Any, started: float, *, handoff_id: int, result: str,
    ) -> None:
        self._last_handoff = {
            "id": handoff_id,
            "from": handoff.prev_source.value,
            "to": handoff.current_source.value,
            "reason": handoff.reason,
            "level": handoff.level,
            "guard_db": handoff.guard_db,
            "camilla_before_db": handoff.camilla_before_db,
            "prev_mode": handoff.prev_mode.value,
            "target_mode": handoff.current_mode.value,
            "push_ok": handoff.push_ok,
            "settled_ms": handoff.settled_ms,
            "result": result,
            "detail": handoff.detail,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    async def _pause_best_effort(self, source: Source, *, reason: str) -> None:
        try:
            await self._pause(source)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "source preempt failed source=%s reason=%s: %s",
                source.value, reason, e,
            )

    async def _fanin_select(self, source: Source) -> dict[str, Any]:
        label = SOURCE_TO_FANIN_LABEL[source]
        return await _fanin_command(f"SELECT {label}")

    async def _fanin_auto(self) -> dict[str, Any]:
        return await _fanin_command("AUTO")

    async def _fanin_none(self) -> dict[str, Any]:
        return await _fanin_command("NONE")

    async def _fanin_select_best_effort(
        self, source: Source, *, reason: str,
    ) -> None:
        try:
            await self._fanin_select(source)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "fanin source gate reassert failed source=%s reason=%s: %s",
                source.value, reason, e,
            )

    async def _fanin_auto_best_effort(self, *, reason: str) -> None:
        try:
            await self._fanin_auto()
        except Exception as e:  # noqa: BLE001
            logger.warning("fanin AUTO reset failed reason=%s: %s", reason, e)

    async def _fanin_none_best_effort(self, *, reason: str) -> None:
        try:
            await self._fanin_none()
        except Exception as e:  # noqa: BLE001
            logger.warning("fanin NONE failed reason=%s: %s", reason, e)

    async def _run_control_server(self) -> None:
        try:
            parent = os.path.dirname(MUX_CONTROL_SOCKET)
            if parent:
                os.makedirs(parent, exist_ok=True)
            try:
                os.unlink(MUX_CONTROL_SOCKET)
            except FileNotFoundError:
                pass
            server = await asyncio.start_unix_server(
                self._handle_control_client,
                path=MUX_CONTROL_SOCKET,
            )
            logger.info("mux control socket listening at %s", MUX_CONTROL_SOCKET)
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("mux control socket unavailable: %s", e)

    async def _handle_control_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            command = raw.decode("utf-8", "replace").strip()
            if command == "STATUS":
                payload = self._status_payload()
            elif command == "AUTO":
                payload = await self.auto_select()
            elif command.startswith("SELECT "):
                source_name = command.split(" ", 1)[1].strip()
                try:
                    source = Source(source_name)
                except ValueError:
                    payload = {"error": f"unknown source {source_name!r}"}
                else:
                    if source not in MUSIC_SOURCES:
                        payload = {
                            "error": (
                                f"not a selectable source {source_name!r}"
                            ),
                        }
                    else:
                        payload = await self.select_source(source)
            else:
                payload = {"error": f"unknown command {command!r}"}
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as e:  # noqa: BLE001
            logger.warning("mux control request failed: %s", e)
            with contextlib.suppress(Exception):
                writer.write(
                    (json.dumps({"error": str(e)}) + "\n").encode("utf-8"),
                )
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

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
            # Tier 1 failed. With fan-in, an un-pauseable librespot
            # owns its private lane and does not crash on ALSA EBUSY —
            # it just keeps streaming and mixes with the new winner.
            # The user's contract ("we cannot have both played at the
            # same time") requires us to force a release. systemctl
            # restart kills librespot's FD on its fan-in lane; the
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
                "release of the fan-in spotify lane",
            )
            await self._spotify_force_restart_librespot()
        elif source == Source.AIRPLAY:
            await self._airplay_stop_for_preempt()
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

    async def _airplay_stop_for_preempt(self) -> None:
        """Drop AirPlay playback when another source wins the speaker.

        Voice transport still exposes "pause" semantics through
        RendererClient.pause_airplay(). Mux preemption is different:
        once Spotify/Bluetooth/USB has the audible lane, keeping a
        paused AP2 session alive makes status ambiguous and lets sender
        resumes race with the next source switch. shairport-sync exposes
        MPRIS Stop, which is the narrowest renderer-owned way to end the
        current playback session without restarting the whole service.
        """
        ok = await _busctl(
            "call",
            SHAIRPORT_MPRIS_BUS,
            SHAIRPORT_MPRIS_PATH,
            MPRIS_PLAYER_IFACE,
            "Stop",
        )
        if ok is not None:
            log_event(logger, "airplay.preempt_stop", method="Stop", result="ok")
            return

        log_event(
            logger,
            "airplay.preempt_stop_failed",
            method="Stop",
            action="pause_fallback",
            level=logging.WARNING,
        )
        pause_ok = await _busctl(
            "call",
            SHAIRPORT_MPRIS_BUS,
            SHAIRPORT_MPRIS_PATH,
            MPRIS_PLAYER_IFACE,
            "Pause",
        )
        if pause_ok is None:
            log_event(
                logger,
                "airplay.preempt_pause_failed",
                method="Pause",
                level=logging.WARNING,
            )

    # ------------------------------------------------------------------
    # USB sink preempt protocol — POSTs to the daemon's local HTTP
    # endpoint. The daemon flips its internal `preempted` flag,
    # making its audio callback emit silence into usbsink_substream.
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
            log_event(
                logger,
                "usbsink.preempt_skipped",
                silenced=silenced,
                reason=reason,
                via="JASPER_USBSINK_PREEMPT=disabled",
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
                log_event(
                    logger,
                    "usbsink.preempt_set",
                    silenced=silenced,
                    reason=reason,
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
            default_redirect_uri = (
                f"https://jaspercurry.github.io/spotify-oauth-callback/?host={hostname}"
            )
            result = build_clients(
                registry,
                client_id=client_id,
                redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI") or default_redirect_uri,
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
        from .speaker_name import runtime_name as _speaker_runtime_name
        device_name = _speaker_runtime_name()
        # Two-pass: first prefer is_active devices (lowest-latency
        # path); fall through to any JTS-named device if that fails.
        for prefer_active in (True, False):
            for ac in router.clients.values():
                try:
                    devices = await asyncio.wait_for(
                        asyncio.to_thread(ac.sp.devices), timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "spotify devices() timed out for %s — "
                        "skipping (does not block mux tick)",
                        ac.account.name,
                    )
                    continue
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
                        await asyncio.wait_for(
                            asyncio.to_thread(
                                ac.sp.pause_playback, device_id=d.get("id"),
                            ),
                            timeout=5.0,
                        )
                        logger.info(
                            "spotify pause via Web API: "
                            "account=%s device=%s active=%s",
                            ac.account.name, d.get("id"),
                            d.get("is_active"),
                        )
                        return True
                    except asyncio.TimeoutError:
                        logger.warning(
                            "spotify pause_playback timed out for %s — "
                            "skipping (does not block mux tick)",
                            ac.account.name,
                        )
                        continue
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "spotify pause failed for %s: %s",
                            ac.account.name, e,
                        )
                        continue
        return False

    async def _spotify_force_restart_librespot(self) -> bool:
        """Tier 2 escalation: restart librespot.service to force it
        to drop its FD on the Spotify fan-in lane.

        Effects observed at the audio layer: librespot exits and closes
        its private `librespot_substream` writer; fanin then reads
        silence on that lane while the new winner's lane continues.
        systemd respawns librespot in ~2-3 s (Restart=always); during
        that gap, the new winner (AirPlay / Bluetooth) is heard alone.
        After respawn, librespot is back as an idle Spotify Connect
        device — the credential cache (--system-cache
        /var/cache/librespot) persists, so the user's phone re-sees the
        speaker in the Connect picker without re-authenticating. The catch: any
        state inside librespot's current session (track position, queue)
        is lost — the next Spotify Connect cast picks up fresh.

        We use `systemctl restart` rather than `kill -TERM` so the
        same `Restart=always` policy that handles every other
        librespot exit also handles this one — no special-case
        recovery path.

        Returns True on a successful restart. Logged but not retried on
        failure (the only thing that would happen on retry is more log
        spam — the failure mode is "restart unavailable" which doesn't
        self-heal).

        WS1 Phase 3: routed through jasper-control's restart broker
        (off-thread, since the broker client is blocking) so jasper-mux
        needs no privilege of its own once dropped to a non-root service
        user. While mux is still root the broker client falls back to a
        direct systemctl if the broker is unreachable.
        """
        resp = await asyncio.to_thread(
            restart_broker.manage_units,
            "librespot.service", verb="restart",
            reason="spotify Tier-2 recovery", no_block=False, timeout=8.0,
        )
        if not resp.get("ok"):
            logger.warning(
                "spotify force-restart: librespot restart failed: %s",
                resp.get("error") or f"rc={resp.get('rc')}",
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


async def _fanin_command(cmd: str) -> dict[str, Any]:
    """Send one line to jasper-fanin's control socket.

    Fan-in owns the hot-path audio gate; mux owns policy. Keeping the
    IPC here as a one-command UDS call mirrors jasper-control's voice
    socket helper without importing any Rust-specific detail.
    """
    reader, writer = await asyncio.open_unix_connection(FANIN_CONTROL_SOCKET)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError("jasper-fanin returned no response")
    payload = json.loads(line.decode("utf-8"))
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload, dict):
        raise RuntimeError("jasper-fanin returned non-object JSON")
    return payload


def _fmt_db(value: float | None) -> str:
    return "none" if value is None else f"{value:.1f}"


async def _voice_socket_command(
    socket_path: str, cmd: str, *, timeout: float = 1.0,
) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError("voice daemon returned no response")
    payload = json.loads(line.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("voice daemon returned non-object JSON")
    return payload


def _make_duck_active_probe() -> Any:
    socket_path = os.environ.get(
        "JASPER_VOICE_CONTROL_SOCKET", "/run/jasper/voice.sock",
    )

    async def probe() -> bool | None:
        try:
            response = await _voice_socket_command(
                socket_path, "STATUS", timeout=1.0,
            )
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None
        duck_active = response.get("duck_active")
        return duck_active if isinstance(duck_active, bool) else None

    return probe


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
