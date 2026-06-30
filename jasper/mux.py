# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
    pause:  BlueZ AVRCP MediaPlayer1 Pause when the source phone/player
            exposes a player object. If no AVRCP player exists, log and
            degrade to phone-side pause.
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
from .bluetooth.avrcp import bluetooth_avrcp_call
from .control import restart_broker
from .audio_runtime_plan import (
    SourceRouteDecision,
    decide_source_low_latency_route,
    low_latency_feature_flags,
)
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
FANIN_TEST_LABELS = frozenset({"correction"})
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
    behaves like an unsupported source (audio briefly mixes when a new
    source starts). Operator escape hatch for cases where
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
        # Non-music diagnostic lanes (currently the correction/test lane) can
        # temporarily own the fan-in gate without changing the household's
        # persisted manual-vs-auto source selection.
        self._test_fanin_label: str | None = None
        # Stage-4b lean lane (default-OFF). Parsed ONCE at construction so the
        # _tick hot path makes no env read per tick and the disabled path is
        # provably byte-identical (the flag never flips mid-process: a switch
        # is a deploy, which restarts mux). `_in_lean` tracks whether we have
        # swapped CamillaDSP onto the lean File-capture config + armed the
        # usbsink FIFO output, so enter/leave are idempotent across ticks.
        low_latency_flags = low_latency_feature_flags()
        self._lean_enabled = low_latency_flags.lean_lane
        self._in_lean = False
        # Re-arm backoff: once an enter-lean attempt fails (FIFO can't open /
        # lean config fails classify / arm restart failed), do not retry every
        # tick — that would restart-storm the usbsink daemon. Stay on buffered
        # until the source set changes (a fresh exclusive-USB edge clears it).
        self._lean_enter_blocked = False
        # Adaptive fan-in OUTPUT-buffer (default-OFF). The convergence
        # replacement for the lean lane: instead of swapping CamillaDSP onto a
        # File-capture config, it just shrinks fan-in's near-FULL output buffer
        # when USB is the sole exclusive winner, and restores the full default
        # otherwise. It consumes the same source-route decision as the lean lane;
        # the two mechanisms still stay separately feature-gated until the
        # measured FIFO/capture endgame lets us delete the older lane.
        # Parsed ONCE here so the _tick hot path makes no env read per tick and
        # the disabled path is provably byte-identical.
        # `_buffer_shrunk` tracks whether we have the shrunk override armed, so
        # shrink/restore are idempotent across ticks (act only on the edge).
        self._adaptive_buffer_enabled = low_latency_flags.adaptive_buffer
        self._buffer_shrunk = False
        # Re-arm backoff, mirroring the lean enter-block: a failed shrink (env
        # write or fanin restart failed) must not restart-storm the shared
        # daemon every tick. Stay full until the source set changes.
        self._buffer_shrink_blocked = False

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

        if self._test_fanin_label is not None:
            await self._reassert_test_fanin_label()
            # A diagnostic lane owns fan-in; never the lean exclusive-USB path.
            await self._settle_low_latency_audio(current)
            return

        if self._manual_source is not None:
            await self._reassert_manual_source()
            # Manual pin is not the auto exclusive-USB lean trigger; ensure we
            # are off the lean config if we were on it.
            await self._settle_low_latency_audio(current)
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
                # Handoff didn't settle — never enter lean on an unsettled
                # gate; leave lean if we were in it.
                await self._settle_low_latency_audio(current)
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

        await self._settle_low_latency_audio(current)

    async def _settle_low_latency_audio(self, current: dict[Source, bool]) -> None:
        """Drive optional low-latency consumers from one source-route decision."""
        if not (self._lean_enabled or self._adaptive_buffer_enabled):
            return
        decision = self._source_low_latency_decision(current)
        await self._settle_lean(decision)
        await self._settle_adaptive_buffer(decision)

    def _source_low_latency_decision(
        self,
        current: dict[Source, bool],
    ) -> SourceRouteDecision:
        """Single source-route policy for lean/adaptive low-latency consumers."""
        manual_or_test = (
            self._manual_source is not None or self._test_fanin_label is not None
        )
        if manual_or_test:
            return decide_source_low_latency_route(
                active_sources=(),
                winner=None,
                enabled=True,
                exclusive_source=Source.USBSINK.value,
            )
        return decide_source_low_latency_route(
            active_sources=tuple(self._active_sources(current)),
            winner=self._winner,
            enabled=True,
            exclusive_source=Source.USBSINK.value,
        )

    # ------------------------------------------------------------------
    # Adaptive fan-in output-buffer — consumer of the shared source-route policy
    # ------------------------------------------------------------------

    async def _settle_adaptive_buffer(
        self,
        decision: SourceRouteDecision,
    ) -> None:
        """Drive the adaptive fan-in output-buffer from the settled state.

        Default-OFF: when ``JASPER_FANIN_ADAPTIVE_BUFFER`` is not ``enabled``
        this returns immediately and ``_tick`` is byte-identical to the
        pre-adaptive behavior (the flag is parsed once at construction, so the
        hot path makes no env read).

        On the enabled path it consumes the shared source-route policy
        (exclusive wired-USB sole-active-winner -> ``low_latency`` -> shrink;
        any networked/mixed/idle/manual/test route -> restore the full buffer).
        Idempotent across ticks via ``self._buffer_shrunk`` (act only on the
        edge). FAIL-SAFE: every failure path keeps/restores the FULL buffer.

        SCOPE mirrors ``_settle_lean``: AUTO mode only for the shrink. A manual
        pin or an active diagnostic (test) lane is treated as non-lean -> it
        gets the restore-to-full path so a manual/correction run while the
        buffer was shrunk unwinds it.
        """
        if not self._adaptive_buffer_enabled:
            return

        if decision.route == "low_latency":
            if self._buffer_shrunk:
                return
            if self._buffer_shrink_blocked:
                # Already tried and failed for this exclusive-USB episode; stay
                # full until the source set changes (routes us to the restore
                # branch below, which clears the block).
                return
            await self._shrink_output_buffer()
        else:
            # Any non-lean route clears the shrink-block (a fresh exclusive-USB
            # edge gets a clean retry) and restores the full buffer if shrunk.
            self._buffer_shrink_blocked = False
            if self._buffer_shrunk:
                await self._restore_output_buffer(reason=decision.reason)

    async def _shrink_output_buffer(self) -> None:
        """Shrink fan-in's output buffer to the soak-sweepable target. FAIL-SAFE
        -> stays FULL on any failure (the buffer_reconcile rolls its env back on
        a restart failure, so the running daemon is never left ahead of the
        persisted value)."""
        from .fanin import buffer_reconcile as br

        target = br.shrunk_target_frames()
        result = await asyncio.to_thread(
            br.set_fanin_output_buffer, target, reason="adaptive_usb_exclusive",
        )
        if not result.ok:
            self._buffer_shrink_blocked = True
            log_event(
                logger,
                "mux.adaptive_buffer_shrink_failed",
                frames=target,
                detail=result.detail,
                level=logging.WARNING,
            )
            return
        self._buffer_shrunk = True
        log_event(logger, "mux.adaptive_buffer_shrunk", frames=result.frames)

    async def _restore_output_buffer(self, *, reason: str) -> None:
        """Restore the FULL default output buffer. FAIL-SAFE: on a restore
        failure we keep ``_buffer_shrunk`` set so the NEXT non-lean tick retries
        the unwind — convergent, and the buffer_reconcile's SF-1 rollback means
        the persisted value never leads the daemon either way."""
        from .fanin import buffer_reconcile as br

        result = await asyncio.to_thread(
            br.restore_fanin_output_buffer, reason=f"adaptive_{reason}",
        )
        if not result.ok:
            log_event(
                logger,
                "mux.adaptive_buffer_restore_failed",
                reason=reason,
                detail=result.detail,
                level=logging.WARNING,
            )
            return
        self._buffer_shrunk = False
        log_event(logger, "mux.adaptive_buffer_restored", reason=reason)

    # ------------------------------------------------------------------
    # Stage-4b lean lane — consumer of the shared source-route policy
    # ------------------------------------------------------------------

    async def _settle_lean(self, decision: SourceRouteDecision) -> None:
        """Drive the lean lane from the settled source state.

        Default-OFF: when ``JASPER_LEAN_LANE`` is not ``enabled`` this returns
        immediately and ``_tick`` is byte-identical to the pre-lean behavior.

        On the enabled path it consumes the shared source-route policy:
        ``low_latency`` runs the enter-lean ladder, anything else runs
        leave-lean (a NO-OP when we were never in lean). Both ladders are
        idempotent across ticks via ``self._in_lean``.

        SCOPE: AUTO mode only. A manual source pin or an active diagnostic
        (test) fan-in lane is treated as non-lean — those paths call this with
        their own gate already owning fan-in, so the lean exclusive-USB
        winner-takes-it semantics don't apply. They still get the leave-lean
        restore (so a manual pin or a correction run while lean was live
        unwinds the lean config).
        """
        if not self._lean_enabled:
            return

        if decision.route == "low_latency":
            # Clear a prior enter-block only when the world changed enough to be
            # worth another attempt — i.e. we are not already blocked-and-stable.
            if self._in_lean:
                return
            if self._lean_enter_blocked:
                # Already tried and failed for this exclusive-USB episode; stay
                # on buffered until the source set changes (which routes us to
                # the leave branch below and clears the block).
                return
            await self._enter_lean()
        else:
            # Any non-lean route clears the enter-block (a fresh exclusive-USB
            # edge later gets a clean retry) and restores buffered if needed.
            self._lean_enter_blocked = False
            if self._in_lean:
                await self._leave_lean(reason=decision.reason)

    async def _enter_lean(self) -> None:
        """Enter-lean ladder: arm the usbsink FIFO output, then swap CamillaDSP
        to the carrier-preserved lean File-capture config. FAIL-LOUD -> buffered
        on any failure (the speaker stays on the buffered path, which always
        works).

        Order matters: arm the FIFO output FIRST so the usbsink daemon is
        writing the lean pipe before CamillaDSP File-captures it (a reader-first
        File capture would otherwise spin on an empty pipe). If the config swap
        then fails, we disarm the FIFO back to aloop so usbsink keeps feeding
        the buffered fan-in lane.
        """
        from .usbsink import output_mode_reconcile as omr

        arm = await asyncio.to_thread(
            omr.set_output_mode, "fifo", reason="lean_enter",
        )
        if not arm.ok:
            self._lean_enter_blocked = True
            log_event(
                logger,
                "mux.lean_enter_failed",
                stage="arm_fifo",
                detail=arm.detail,
                level=logging.WARNING,
            )
            return

        try:
            await self._lean_apply_config()
        except Exception as e:  # noqa: BLE001
            # Config swap failed (CarrierCannotHostEq / DspApplyError / camilla
            # down). The apply engine already rolled CamillaDSP back to the prior
            # buffered config; disarm the FIFO so usbsink returns to the aloop
            # fan-in lane and we are fully back on buffered.
            self._lean_enter_blocked = True
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    omr.set_output_mode, "aloop", reason="lean_enter_rollback",
                )
            log_event(
                logger,
                "mux.lean_enter_failed",
                stage="apply_config",
                detail=str(e),
                level=logging.WARNING,
            )
            return

        self._in_lean = True
        log_event(logger, "mux.lean_entered")

    async def _leave_lean(self, *, reason: str) -> None:
        """Leave-lean ladder: restore the buffered config, then disarm the FIFO.

        Restore ALWAYS SUCCEEDS by construction (restore_buffered_config
        re-emits from saved intent and never REFUSES a stereo-host graph) — but
        a transient CamillaDSP/broker outage can still make the live reload
        raise. ORDERING IS LOAD-BEARING: only disarm the usbsink FIFO output
        AFTER the buffered config restore actually succeeds. Disarming first (or
        unconditionally) would leave CamillaDSP File-capturing the lean pipe
        while usbsink is back on the aloop fan-in lane — a dead pipe, silent
        music. So on a restore failure we keep the FIFO armed (the lean pipe
        stays fed → audio keeps flowing through the lean config) and leave
        ``_in_lean`` set so the NEXT non-lean tick retries the whole unwind.
        Convergent, never silent.
        """
        from .usbsink import output_mode_reconcile as omr

        try:
            await self._lean_restore_config()
        except Exception as e:  # noqa: BLE001
            # Keep _in_lean=True and the FIFO armed; the next tick retries.
            log_event(
                logger,
                "mux.lean_leave_config_failed",
                reason=reason,
                detail=str(e),
                level=logging.WARNING,
            )
            return
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                omr.set_output_mode, "aloop", reason=f"lean_leave_{reason}",
            )
        self._in_lean = False
        log_event(logger, "mux.lean_left", reason=reason)

    async def _lean_apply_config(self) -> None:
        """Swap CamillaDSP to the carrier-preserved lean File-capture config.
        Split out so tests can stub the CamillaDSP I/O."""
        from .sound.runtime import apply_lean_capture_config

        await apply_lean_capture_config()

    async def _lean_restore_config(self) -> None:
        """Restore the buffered sound config (NO-OP if not on lean). Split out
        so tests can stub the CamillaDSP I/O."""
        from .sound.runtime import restore_buffered_config

        await restore_buffered_config()

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

    async def select_test_fanin_label(self, label: str) -> dict[str, Any]:
        """Temporarily route a non-music diagnostic lane through fan-in.

        This is intentionally not persisted and does not change the household
        source selector. It exists for same-path tests such as active-speaker
        commissioning that enter through the correction lane while the speaker
        may otherwise be manually pinned to AirPlay/Spotify/etc.
        """

        label = str(label or "").strip()
        if label not in FANIN_TEST_LABELS:
            return {"error": f"not a selectable test fan-in label {label!r}"}
        async with self._transition_lock:
            self._test_fanin_label = label
            await self._fanin_select_label(label)
        log_event(logger, "source.test_select", label=label)
        return self._status_payload(self._state.playing)

    async def release_test_fanin_label(self) -> dict[str, Any]:
        async with self._transition_lock:
            released = self._test_fanin_label
            self._test_fanin_label = None
            if self._manual_source is not None:
                await self._fanin_select_best_effort(
                    self._manual_source, reason="test_release_manual",
                )
            elif self._winner is not None and self._state.playing.get(
                self._winner, False,
            ):
                await self._fanin_select_best_effort(
                    self._winner, reason="test_release_auto",
                )
            else:
                await self._fanin_none_best_effort(reason="test_release_idle")
        log_event(logger, "source.test_release", label=released)
        return self._status_payload(self._state.playing)

    def _status_payload(
        self, current: dict[Source, bool] | None = None,
    ) -> dict[str, Any]:
        current = current or self._state.playing
        active = self._active_source_name(current)
        # Local import mirrors the adaptive ladder's pattern (keeps the control
        # package off mux's module-load path); cached after first use.
        from jasper.fanin import buffer_reconcile as br

        return {
            "mode": "manual" if self._manual_source is not None else "auto",
            "selected_source": (
                self._manual_source.value if self._manual_source else None
            ),
            "test_source": self._test_fanin_label,
            "active_source": active,
            "winner": self._winner.value if self._winner else None,
            "last_handoff": self._last_handoff,
            "sources": {
                source.value: {"playing": bool(current.get(source, False))}
                for source in MUSIC_SOURCES
            },
            # Review should-fix #1: surface the adaptive output-buffer mode so an
            # operator sees the live shrink state from /state without ssh+journal.
            # This is the mux's INTENDED state; fan-in's actual running buffer is
            # in /state.renderers.fanin.output.buffer_frames (cross-check the two).
            # Read-only status field — does not touch _tick, so the default-OFF
            # byte-identical proof is unaffected; no env I/O while disabled.
            "fanin_output_buffer": {
                "adaptive_enabled": self._adaptive_buffer_enabled,
                "shrunk": self._buffer_shrunk,
                "frames": (
                    br.shrunk_target_frames()
                    if (self._adaptive_buffer_enabled and self._buffer_shrunk)
                    else br.DEFAULT_OUTPUT_BUFFER_FRAMES
                ),
            },
        }

    def _active_source_name(self, current: dict[Source, bool]) -> str:
        if self._test_fanin_label is not None:
            return self._test_fanin_label
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

    async def _reassert_test_fanin_label(self) -> None:
        async with self._transition_lock:
            label = self._test_fanin_label
            if label is None:
                return
            await self._fanin_select_label_best_effort(
                label, reason="test_tick",
            )

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
        return await self._fanin_select_label(label)

    async def _fanin_select_label(self, label: str) -> dict[str, Any]:
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

    async def _fanin_select_label_best_effort(
        self, label: str, *, reason: str,
    ) -> None:
        try:
            await self._fanin_select_label(label)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "fanin test gate reassert failed label=%s reason=%s: %s",
                label, reason, e,
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
            # 0660 (was the umask default ~0755): once mux runs as a non-root
            # service user with primary group `jasper` (WS1 Phase 3b), the
            # socket is jasper-mux:jasper and only root + the `jasper` group
            # (jasper-control / jasper-web clients) can connect — tighter than
            # the prior world-connectable default. Best-effort like the voice /
            # peering sockets' post-bind chmod.
            try:
                os.chmod(MUX_CONTROL_SOCKET, 0o660)
            except OSError as e:
                logger.warning("mux control socket chmod failed: %s", e)
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
            elif command.startswith("TEST_SELECT "):
                label = command.split(" ", 1)[1].strip()
                payload = await self.select_test_fanin_label(label)
            elif command == "TEST_RELEASE":
                payload = await self.release_test_fanin_label()
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
    # Pause actions — Spotify, AirPlay, and Bluetooth have receiver-side
    # APIs when their upstream sender exposes the needed control surface.
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
            try:
                await bluetooth_avrcp_call("Pause")
                log_event(
                    logger, "bluetooth.preempt_pause",
                    method="MediaPlayer1.Pause", result="ok",
                )
            except Exception as e:  # noqa: BLE001
                log_event(
                    logger,
                    "bluetooth.preempt_pause_failed",
                    method="MediaPlayer1.Pause",
                    action="phone_side_pause_required",
                    err=str(e),
                    level=logging.WARNING,
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
                "/var/lib/jasper-intsecrets/spotify/accounts.json",
            ))
            maybe_migrate_legacy(
                registry,
                os.environ.get(
                    "SPOTIFY_CACHE_PATH",
                    "/var/lib/jasper-intsecrets/.spotify-cache",
                ),
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
