# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-mux — renderer source-arbiter.

Native producer notifications wake one host-owned reconciler, which re-reads
source state and applies one source-neutral latest-start-wins policy. A
fixed 1 Hz patrol invokes that exact same reconciler as a lost-alert safety net.
Alerts are therefore hints, never routing commands: stale and duplicate alerts
are harmless, and the patrol cannot disagree with a separate alert policy
because none exists. A patrol re-probes the two subprocess-backed sources only
every EVENT_BACKED_PROBE_SEC (see that constant); an alert naming one of them
still probes it on the spot.

Renderer support:
  Spotify (librespot):
    detect: read /run/librespot/state.env (written by
            --onevent hook on every player event)
    pause:  Two-tier escalation. Tier 1 is Spotify Web API via
            spotipy — librespot 0.8.0 has no local control HTTP.
            We iterate household accounts and issue
            PUT /me/player/pause to any account that has the configured
            speaker device in its list. Tier 2 is
            `systemctl try-restart librespot.service` if Tier 1 fails:
            renderers each own a private fan-in lane, so an
            un-pauseable librespot keeps streaming and is summed
            alongside the new winner. try-restart releases that lane
            only while the source is still active, so a concurrently
            disabled or role-parked source is not resurrected.
            Off-switch: JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled.
  AirPlay (shairport-sync):
    detect: MPRIS PlaybackStatus == "Playing" AND non-empty MPRIS
            Metadata xesam:title (source_state.airplay_playing) — the
            metadata corroboration stops mux flapping on macOS's ~30 s
            AirPlay keepalive cycles, which report Playing with no
            audio actually reaching the speakers.
    preempt: native DropSession method, falling back to MPRIS Stop on
            older/unavailable native interfaces. DropSession tears down
            the receiver-owned AirPlay session instead of relying on the
            sender to honor a remote transport request, avoiding a hidden
            AirPlay session while another renderer owns the fan-in gate.
  Bluetooth (bluez-alsa):
    detect: presence of an a2dpsnk source PCM (best-effort —
            doesn't distinguish "phone connected, not playing"
            from "phone connected and streaming")
    pause:  BlueZ AVRCP MediaPlayer1 Pause when the source phone/player
            exposes a player object. If no AVRCP player exists, log and
            degrade to phone-side pause.
  USB sink (jasper-usbsink):
    detect: fan-in DIRECT-captures the gadget, so USB liveness comes
            from fan-in DIRECT-lane telemetry. See _usbsink_playing.
            Liveness is purely "is the host streaming frames to us" —
            there is no audio-LEVEL gate. A faint sound is still a
            sound; if USB is the only source, we play it.
    pause:  MUTE the fan-in usbsink lane at its mix stage. When all
            other sources go idle, we release the preempt (unmute) so
            an already-streaming host can resume. A new USB start clears
            the mute before USB takes the selected lane.

Automatic source policy:
  Every source is an equal candidate, including USB: a confirmed
  inactive→active transition becomes the winner, so Auto has one explainable
  rule — the latest source to start wins. Losing sources get their
  source-specific preemption (AirPlay DropSession, Spotify/BT pause, USB lane
  mute). Alert arrival order never chooses the winner; alerts only accelerate
  the authoritative re-read.

  A process-local activation sequence records every confirmed start, including
  starts observed while a manual pin owns the gate, and chooses the most
  recently started still-active source when the winner stops or the user
  returns to Auto. Starts first seen in one snapshot tie-break on
  MUSIC_SOURCES registry order because their real-world order is unknowable.
  A persistent manual pin overrides Auto; /sources remains the lifecycle
  surface for disabling a source entirely.

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
from typing import Any, Callable, Optional

from jasper.log_event import log_event

from . import librespot_state, mux_mode_persistence
from .bluetooth.avrcp import bluetooth_avrcp_call
from .busctl import system_busctl
from .control import restart_broker
from .fanin.control import fanin_command
from .music_sources import MUSIC_SOURCES, SOURCE_TO_FANIN_LABEL, Source
from .route_latency.status_socket import FANIN_STATUS_SOCKET, MUX_CONTROL_SOCKET_PATH
from .source_state import (
    airplay_playing_observed as airplay_playing,
    bluetooth_playing_observed as bluetooth_playing,
    spotify_playing_observed as spotify_playing,
    usbsink_direct_frames_read,
    usbsink_direct_streaming,
)
from .spotify_oauth import resolved_spotify_redirect_uri

logger = logging.getLogger(__name__)


FANIN_CONTROL_SOCKET = os.environ.get(
    "JASPER_FANIN_CONTROL_SOCKET", FANIN_STATUS_SOCKET,
)
# Persisted so a household's manual pin survives the Restart=always
# deploy/restart cycle. RuntimeDirectory is wiped on restart, so this lives
# under /var/lib/jasper, not /run.
MUX_MODE_STATE_PATH = os.environ.get(
    "JASPER_MUX_MODE_STATE_PATH", mux_mode_persistence.DEFAULT_PATH,
)
# USB preempt is a MUTE/UNMUTE of THIS fan-in lane — the only USB-silencing
# primitive, since fan-in DIRECT-captures the gadget as its sole live ingress
# owner. Derived from the map fan-in SELECT uses, so the two never drift.
USBSINK_FANIN_LABEL = SOURCE_TO_FANIN_LABEL[Source.USBSINK]
FANIN_TEST_LABELS = frozenset({"correction"})
FANIN_TEST_OWNERS = frozenset({
    "active-speaker-commissioning",
    "chip-aec-commission",
    "correction-measurement",
    "doctor-aec-probe",
    "jasper-measure",
    "jasper-null",
    "seat-level",
})
# A diagnostic owner must renew before this monotonic deadline. This is long
# enough for the commissioning tone (web_commissioning.COMMISSION_TONE_DURATION_S);
# correction renews every measurement_window.MEASUREMENT_GATE_REFRESH_SEC. A web
# worker crash therefore self-recovers instead of pinning household music off.
FANIN_TEST_LEASE_SEC = 60.0
SHAIRPORT_MPRIS_BUS = "org.mpris.MediaPlayer2.ShairportSync"
SHAIRPORT_MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
SHAIRPORT_NATIVE_BUS = "org.gnome.ShairportSync"
SHAIRPORT_NATIVE_PATH = "/org/gnome/ShairportSync"
SHAIRPORT_NATIVE_IFACE = "org.gnome.ShairportSync"


def _spotify_preempt_restart_disabled() -> bool:
    """Env-var escape hatch for the Spotify-preempt Tier 2 escalation.

    JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled reverts preempt to "Web API
    only, mix-on-failure". Default: enabled.
    """
    return os.environ.get(
        "JASPER_MUX_SPOTIFY_PREEMPT_RESTART", "",
    ).strip().lower() == "disabled"


def _usbsink_preempt_disabled() -> bool:
    """Env-var escape hatch for the USB-sink preempt mechanism.

    JASPER_USBSINK_PREEMPT=disabled in /etc/jasper/jasper.env short-circuits
    ``_usbsink_set_preempt``: mux stops MUTE/UNMUTE-ing the fan-in usbsink lane
    when another source wins, so USB behaves like an unsupported source (audio
    briefly mixes when a new source starts). Read fresh on every call, so it
    takes effect without a redeploy or daemon restart. Default: enabled.
    """
    return os.environ.get(
        "JASPER_USBSINK_PREEMPT", "",
    ).strip().lower() == "disabled"


# Combo-mode USB streaming debounce, in mux ticks (POLL_INTERVAL_SEC = 1 Hz).
# Rides through a brief delivery gap (a status miss / a momentary stall) so the
# source doesn't flap; a real pause stops the frames and macOS tears the stream
# down, so the counter genuinely stalls and USB releases after this many ticks.
USBSINK_COMBO_STOP_TICKS = 2
ALERT_COALESCE_SEC = 0.05
# A transient unreadable probe must not synthesize stop/start flutter, but a
# permanently dead adapter must not pin a vanished winner forever. Hold an
# active last-known state for this bounded grace, then fail inactive until a
# successful observation re-establishes it.
UNKNOWN_ACTIVE_HOLD_SEC = 5.0
# How stale a lost-signal repair may get on the two sources below. Matched to
# UNKNOWN_ACTIVE_HOLD_SEC, the window the arbiter already tolerates without a
# known observation before it drops an active source.
EVENT_BACKED_PROBE_SEC = UNKNOWN_ACTIVE_HOLD_SEC


def event_backed_probes() -> dict[Source, Callable[[], Any]]:
    """The only two source probes that fork a subprocess.

    busctl for the AirPlay MPRIS properties, bluealsa-cli for the A2DP PCM
    list. Both sources also have a live system-D-Bus signal adapter in
    jasper.source_events, so for them the patrol probe is a lost-signal repair
    rather than the detection path. Resolved per call so the probes stay
    patchable by name.
    """
    return {
        Source.AIRPLAY: airplay_playing,
        Source.BLUETOOTH: bluetooth_playing,
    }


@dataclass(frozen=True)
class ComboLiveness:
    """Temporal state for combo-mode USB frames-flowing detection.

    ``streaming`` is "is the host feeding us frames right now" — there is NO
    audio-LEVEL component. A faint sound and a loud one both stream frames and
    therefore produce the same authoritative source-start edge; level is
    display-only and does not participate in arbitration, so a quiet passage
    keeps the counter advancing rather than reading "stopped". New fan-in builds
    publish a 20 Hz-derived streaming edge; this state machine remains the
    rolling-upgrade fallback for older STATUS shapes. A host that actually tears
    the stream down stops frames and releases after the stop hysteresis.
    """

    prev_frames: int | None = None
    idle_ticks: int = 0
    streaming: bool = False


def step_combo_liveness(
    state: ComboLiveness,
    frames: int | None,
    *,
    stop_ticks: int,
) -> ComboLiveness:
    """Advance the combo-USB streaming state by one mux tick.

    A combo box is ``streaming`` on a tick iff the fan-in DIRECT-lane counter
    ``frames`` grew since the previous tick. A first reading or counter reset
    re-baselines without inventing a delta; flat frames drop after
    ``stop_ticks`` consecutive non-advancing patrols; missing frames are
    unknown and retain the complete prior state, because a STATUS miss is not
    evidence that a stream stopped.
    """
    prev = state.prev_frames
    if frames is None:
        return state
    advanced = frames is not None and prev is not None and frames > prev
    new_prev = frames if frames is not None else prev
    if advanced:
        return ComboLiveness(new_prev, 0, True)
    if not state.streaming:
        return ComboLiveness(new_prev, 0, False)
    idle = state.idle_ticks + 1
    return ComboLiveness(new_prev, idle, idle < stop_ticks)


@dataclass
class _State:
    """Per-source playing flag from the previous tick. Preemption is driven by
    `prev → current` transitions — only a not-playing → playing edge acts."""
    playing: dict[Source, bool] = field(
        default_factory=lambda: {s: False for s in MUSIC_SOURCES},
    )
    observations: dict[Source, str] = field(
        default_factory=lambda: {s: "unknown" for s in MUSIC_SOURCES},
    )
    known_at: dict[Source, float] = field(
        default_factory=lambda: {s: 0.0 for s in MUSIC_SOURCES},
    )
    # Process-local order of confirmed inactive→active transitions. Sequence
    # order, rather than alert arrival time, is the source of truth for fallback
    # arbitration because alerts are lossy wake hints and may be duplicated.
    started_seq: dict[Source, int] = field(
        default_factory=lambda: {s: 0 for s in MUSIC_SOURCES},
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
        self._started_seq = 0
        self._observation_lock = asyncio.Lock()
        self._winner: Optional[Source] = None
        # Fails open to None (auto / latest-start-wins) on a missing or corrupt
        # file. The fan-in gate is reasserted from this on the first tick.
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
        self._spotify_router: Any | None = None
        self._spotify_router_built = False
        # True while fan-in has been told to silence the USB lane. Cleared
        # before USB becomes the winner and once all other sources go idle, so
        # source selection and the lane mute cannot disagree.
        self._usbsink_preempted = False
        self._usbsink_combo = ComboLiveness()
        self._volume_coordinator = volume_coordinator
        self._last_handoff: dict[str, Any] | None = None
        self._handoff_seq = 0
        self._transition_lock = asyncio.Lock()
        self._pending_auto_target: Source | None = None
        # Non-music diagnostic lanes (currently the correction/test lane) can
        # temporarily own the fan-in gate without changing the household's
        # persisted manual-vs-auto source selection.
        self._test_fanin_label: str | None = None
        self._test_fanin_owner: str | None = None
        self._test_fanin_expires_at: float | None = None
        # Alert/patrol reconciliation. Producers may only mark a source dirty
        # and wake this event; `_reconcile` is the single policy entry point.
        self._reconcile_wake = asyncio.Event()
        self._dirty_sources: set[Source] = set()
        self._notification_received = {s: 0 for s in MUSIC_SOURCES}
        self._notification_coalesced = {s: 0 for s in MUSIC_SOURCES}
        self._notification_last: dict[Source, tuple[str, float] | None] = {
            s: None for s in MUSIC_SOURCES
        }
        self._reconcile_seq = 0
        self._probe_due_at = {s: 0.0 for s in event_backed_probes()}
        self._patrol_count = 0
        self._patrol_repairs = 0
        self._last_reconcile: dict[str, Any] | None = None
        self._last_alert_reconcile_at = 0.0

    async def run(self) -> None:
        logger.info(
            "jasper-mux starting (alerts=native, patrol=%.1fs, librespot_state=%s)",
            self.POLL_INTERVAL_SEC, self._librespot_state_path,
        )
        await self._fanin_none_best_effort(reason="startup")
        control_task = asyncio.create_task(self._run_control_server())
        from .source_events import start_source_event_tasks

        event_tasks = start_source_event_tasks(
            self.notify_source_changed,
            spotify_state_path=self._librespot_state_path,
        )
        try:
            loop = asyncio.get_running_loop()
            next_patrol = loop.time() + self.POLL_INTERVAL_SEC
            startup_pending = True
            while True:
                try:
                    # Startup takes the same protected reconciliation path as
                    # every later patrol: a transient first probe must not exit
                    # into Restart=always while fan-in remains held at NONE.
                    if startup_pending:
                        startup_pending = False
                        await self._reconcile(
                            trigger="startup",
                            dirty_sources=set(),
                        )
                        continue

                    timeout = max(0.0, next_patrol - loop.time())
                    woke = False
                    # asyncio.timeout(), NOT asyncio.wait_for(): on CPython
                    # <= 3.11 wait_for SWALLOWS a CancelledError that arrives
                    # in the same tick its awaited future completes (Lib/
                    # asyncio/tasks.py: `except CancelledError: if fut.done():
                    # return fut.result()`). This wait sits on exactly that
                    # seam — an alert resolves the event constantly — so a
                    # cancellation delivered alongside an alert was eaten and
                    # run() became IMMORTAL: it kept patrolling forever and
                    # every awaiter of the task hung (#1935). 3.12 rewrote
                    # wait_for on top of asyncio.timeout(); using it directly
                    # gets the correct behaviour on 3.11 too. Do not "simplify"
                    # this back to wait_for while 3.11 is supported.
                    try:
                        async with asyncio.timeout(timeout):
                            await self._reconcile_wake.wait()
                        woke = True
                    except asyncio.TimeoutError:
                        pass

                    # Catch an alert that landed at the timeout boundary.
                    if self._reconcile_wake.is_set():
                        self._reconcile_wake.clear()
                        woke = True
                    if woke:
                        # Coalesce a short burst (e.g. MPRIS status + metadata)
                        # without imposing delay on the first alert after idle.
                        since_last = loop.time() - self._last_alert_reconcile_at
                        if since_last < ALERT_COALESCE_SEC:
                            await asyncio.sleep(ALERT_COALESCE_SEC - since_last)

                        # An alert may have landed during that sleep. Clear its
                        # level-triggered wake immediately before snapshotting
                        # the dirty set — no await between the two — so a later
                        # alert stays set for the next loop instead of causing
                        # an empty reconciliation.
                        self._reconcile_wake.clear()

                    now = loop.time()
                    patrol_due = now >= next_patrol
                    dirty = set(self._dirty_sources)
                    self._dirty_sources.clear()
                    if not dirty and not patrol_due:
                        continue
                    if patrol_due:
                        while next_patrol <= now:
                            next_patrol += self.POLL_INTERVAL_SEC
                    trigger = (
                        "alert+patrol" if dirty and patrol_due
                        else "alert" if dirty
                        else "patrol"
                    )
                    await self._reconcile(
                        trigger=trigger,
                        dirty_sources=dirty,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("mux reconcile failed: %s", e)
        finally:
            tasks = [control_task, *event_tasks]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._volume_coordinator is not None:
                with contextlib.suppress(Exception):
                    await self._volume_coordinator.aclose()

    def notify_source_changed(self, source: Source, via: str) -> None:
        """Record a wake hint without making or applying a routing decision."""
        if source not in MUSIC_SOURCES:
            return
        self._notification_received[source] += 1
        if source in self._dirty_sources:
            self._notification_coalesced[source] += 1
        self._dirty_sources.add(source)
        self._notification_last[source] = (via, time.monotonic())
        self._reconcile_wake.set()
        logger.debug("source alert source=%s via=%s", source.value, via)

    async def _reconcile(
        self,
        *,
        trigger: str,
        dirty_sources: set[Source],
    ) -> None:
        """The sole automatic arbitration entry point for alerts and patrols."""
        started = time.monotonic()
        before = (self._winner, tuple(self._state.playing.items()))
        await self._tick(defer_probes=self._deferrable_probes(dirty_sources))
        after = (self._winner, tuple(self._state.playing.items()))
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        changed = before != after
        patrol = "patrol" in trigger
        if patrol:
            self._patrol_count += 1
            if changed and not dirty_sources:
                self._patrol_repairs += 1
        self._reconcile_seq += 1
        if dirty_sources:
            self._last_alert_reconcile_at = asyncio.get_running_loop().time()
        self._last_reconcile = {
            "id": self._reconcile_seq,
            "trigger": trigger,
            "dirty_sources": sorted(s.value for s in dirty_sources),
            "changed": changed,
            "winner": self._winner.value if self._winner else None,
            "elapsed_ms": elapsed_ms,
        }
        if dirty_sources or (patrol and changed):
            log_event(
                logger,
                "mux.source_reconcile",
                level=logging.INFO if changed else logging.DEBUG,
                trigger=trigger,
                dirty=",".join(sorted(s.value for s in dirty_sources)) or "none",
                changed=changed,
                winner=self._winner.value if self._winner else "idle",
                elapsed_ms=elapsed_ms,
            )

    def _deferrable_probes(
        self, dirty_sources: set[Source],
    ) -> frozenset[Source]:
        """Subprocess-backed sources this reconcile does not need to re-probe.

        A source an alert named is always probed: the alert is why we woke.
        Otherwise the probe is only repairing a signal that never arrived, and
        one repair per EVENT_BACKED_PROBE_SEC is enough.
        """
        now = time.monotonic()
        return frozenset(
            source
            for source in event_backed_probes()
            if source not in dirty_sources
            and now < self._probe_due_at[source]
        )

    async def _probe_sources(
        self, *, defer_probes: frozenset[Source] = frozenset(),
    ) -> dict[Source, bool]:
        probes: dict[Source, Any] = {
            Source.SPOTIFY: spotify_playing(self._librespot_state_path),
            Source.USBSINK: self._usbsink_playing(),
        }
        probes.update(
            (source, probe())
            for source, probe in event_backed_probes().items()
            if source not in defer_probes
        )
        observed: dict[Source, bool | None] = dict(
            zip(probes, await asyncio.gather(*probes.values()), strict=True),
        )
        now = time.monotonic()
        for source in self._probe_due_at:
            if source in observed:
                # A probe that could not observe anything is not a repair, so
                # it does not buy the next one a full window.
                self._probe_due_at[source] = (
                    now + EVENT_BACKED_PROBE_SEC
                    if observed[source] is not None
                    else 0.0
                )
        # A deferred source is absent from `observed`, so it keeps the state and
        # the observation label its last real probe recorded.
        resolved = dict(self._state.playing)
        for source, value in observed.items():
            if value is None:
                known_age = now - self._state.known_at[source]
                if (
                    resolved[source]
                    and known_age >= UNKNOWN_ACTIVE_HOLD_SEC
                ):
                    resolved[source] = False
                    self._state.observations[source] = "unknown_expired"
                else:
                    self._state.observations[source] = "unknown"
                continue
            resolved[source] = bool(value)
            self._state.known_at[source] = now
            self._state.observations[source] = (
                "active" if value else "inactive"
            )
        return resolved

    async def _usbsink_playing(self) -> bool | None:
        """"Is USB streaming to us" for the source arbiter, off fan-in's DIRECT
        lane.

        New fan-in builds publish an edge-detected ``direct.streaming`` boolean
        from their existing frame counter; older builds fall back to counter
        deltas across patrols. There is NO audio-level gate. A missing or
        non-direct snapshot is unknown and retains the arbiter's last-known
        state; do not issue a second STATUS probe.
        """
        fanin = await self._fanin_status_best_effort()
        streaming = usbsink_direct_streaming(fanin)
        if streaming is not None:
            # Keep fallback state coherent for rolling upgrades/downgrades.
            frames = usbsink_direct_frames_read(fanin)
            self._usbsink_combo = ComboLiveness(
                prev_frames=(
                    frames
                    if frames is not None
                    else self._usbsink_combo.prev_frames
                ),
                idle_ticks=0,
                streaming=streaming,
            )
            return streaming
        frames = usbsink_direct_frames_read(fanin)
        if frames is None:
            return None
        self._usbsink_combo = step_combo_liveness(
            self._usbsink_combo,
            frames,
            stop_ticks=USBSINK_COMBO_STOP_TICKS,
        )
        return self._usbsink_combo.streaming

    async def _fanin_status_best_effort(self) -> dict[str, Any] | None:
        """Read jasper-fanin's STATUS snapshot over its control UDS, fail-soft."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(FANIN_CONTROL_SOCKET),
                timeout=1.0,
            )
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            asyncio.TimeoutError,
            OSError,
        ):
            return None
        try:
            writer.write(b"STATUS\n")
            await writer.drain()
            body = await asyncio.wait_for(reader.read(65536), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionResetError, OSError):
            return None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, AssertionError):
                pass
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _tick(
        self, *, defer_probes: frozenset[Source] = frozenset(),
    ) -> None:
        current, newly_started = await self._observe_sources(
            defer_probes=defer_probes,
        )
        self._winner_age_ticks += 1

        if (
            self._test_fanin_label is not None
            and self._test_fanin_owner is not None
            and self._test_fanin_expires_at is not None
            and time.monotonic() >= self._test_fanin_expires_at
        ):
            expired_owner = self._test_fanin_owner
            payload = await self.release_test_fanin_label(
                expired_owner, reason="lease_expired",
            )
            if "error" in payload:
                logger.warning(
                    "expired test fan-in gate restore failed owner=%s: %s",
                    expired_owner,
                    payload["error"],
                )

        if self._test_fanin_label is not None:
            await self._reassert_test_fanin_label()
            return

        if self._manual_source is not None:
            await self._reassert_manual_source()
            return

        target: Source | None = None
        transition_reason = ""
        pending = self._pending_auto_target
        pending_ok = (
            pending is not None
            and current.get(pending, False)
            and pending != self._winner
        )
        # A fresh start supersedes an older failed handoff retry. Otherwise the
        # retry would consume the new edge and violate latest-start-wins.
        if newly_started:
            self._pending_auto_target = None
            target = newly_started[-1]
            transition_reason = "auto_new_source"
        elif pending_ok:
            target = pending
            transition_reason = "auto_retry"
        else:
            if pending is not None:
                self._pending_auto_target = None
            if self._winner is not None and not current.get(self._winner, False):
                target = self._pick_winner(current)
                transition_reason = "auto_winner_stopped"
            elif self._winner is None:
                target = self._pick_winner(current)
                if target is not None:
                    transition_reason = "auto_startup_active"
        if transition_reason == "auto_new_source":
            logger.info(
                "source transition: %s started (was %s, age=%d ticks)",
                target.value if target else "none",
                self._winner.value if self._winner else "none",
                self._winner_age_ticks,
            )

        if target is not None and target != self._winner:
            async with self._transition_lock:
                if self._manual_source is not None:
                    return
                # Clear a lane mute an older transition left set before
                # selecting USB. Inside the lock so a concurrent manual
                # selection cannot interleave between release and handoff.
                if target == Source.USBSINK and self._usbsink_preempted:
                    await self._usbsink_set_preempt(
                        False, reason="new_transition",
                    )
                prev_winner = self._winner or Source.IDLE
                selected = await self._transition_to_source_locked(
                    prev_winner,
                    target,
                    reason=transition_reason,
                    commit_selection=lambda: self._commit_auto_winner(target),
                )
                if not selected:
                    self._pending_auto_target = target
                    if self._winner is None:
                        await self._fanin_none_best_effort(
                            reason="handoff_prepare_failed",
                        )
            if not selected:
                return

            # Pause every OTHER active source only after the fan-in gate has
            # moved: slow cloud/Web API pause paths must not delay the switch.
            # Best-effort per source — one renderer's pause raising must not
            # abort pausing the rest.
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

        # Release USB preempt once all other sources are idle; without this the
        # speaker would stay silent after AirPlay/Spotify stop while the host
        # is still playing. USBSINK is excluded from the check because the mute
        # is applied downstream of its telemetry, so a muted-but-streaming host
        # keeps reporting `playing`.
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

        # After the release above, so a just-released lane isn't re-muted this
        # tick.
        await self._reassert_usbsink_preempt_mute()

    async def select_source(self, source: Source) -> dict[str, Any]:
        """Manual source selection from the web UI.

        Fan-in enforces the audible lane. This deliberately does not
        pause, disconnect, or disable any renderer: the source selector
        chooses what the speaker passes through, while the `/sources/`
        wizard remains the on/off surface.
        """
        async with self._transition_lock:
            gate_error = self._test_gate_error("source selection")
            if gate_error is not None:
                return gate_error
            previous = self._winner or self._manual_source or Source.IDLE
            self._pending_auto_target = None
            selected = await self._transition_to_source_locked(
                previous,
                source,
                reason="manual",
                commit_selection=lambda: self._commit_manual_selection(source),
            )
            if selected:
                if self._usbsink_preempted:
                    await self._usbsink_set_preempt(
                        False, reason="manual_select",
                    )
            elif self._winner is None:
                await self._fanin_none_best_effort(
                    reason="manual_handoff_failed",
                )
        if not selected:
            current, _ = await self._observe_sources()
            log_event(
                logger,
                "source.manual_select_failed",
                source=source.value,
                level=logging.WARNING,
            )
            return self._status_payload(current)
        current, _ = await self._observe_sources()
        log_event(logger, "source.manual_select", source=source.value)
        return self._status_payload(current)

    async def auto_select(self) -> dict[str, Any]:
        """Return to source-neutral latest-start-wins behavior."""
        gate_error = self._test_gate_error("automatic selection")
        if gate_error is not None:
            return gate_error
        current, _ = await self._observe_sources()
        active_sources = self._active_sources(current)
        new_winner = self._pick_winner(current)
        if new_winner is not None:
            async with self._transition_lock:
                gate_error = self._test_gate_error("automatic selection")
                if gate_error is not None:
                    return gate_error
                previous = self._winner or self._manual_source or Source.IDLE
                selected = await self._transition_to_source_locked(
                    previous,
                    new_winner,
                    reason="auto_select",
                    commit_selection=lambda: self._commit_auto_selection(
                        new_winner,
                    ),
                )
                if selected:
                    if new_winner == Source.USBSINK and self._usbsink_preempted:
                        await self._usbsink_set_preempt(
                            False, reason="auto_select",
                        )
                else:
                    self._pending_auto_target = new_winner
                    if self._winner is None:
                        await self._fanin_none_best_effort(
                            reason="auto_select_handoff_failed",
                        )
            if not selected:
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
                gate_error = self._test_gate_error("automatic selection")
                if gate_error is not None:
                    return gate_error
                self._winner = None
                self._manual_source = None
                self._pending_auto_target = None
                mux_mode_persistence.write_mode(self._mode_state_path, None)
                await self._fanin_none()

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

    def _test_gate_error(self, action: str) -> dict[str, str] | None:
        if self._test_fanin_owner is None:
            return None
        return {
            "error": (
                f"{action} is unavailable while test gate is owned by "
                f"{self._test_fanin_owner!r}"
            ),
        }

    async def select_test_fanin_label(
        self, label: str, owner: str,
    ) -> dict[str, Any]:
        """Temporarily route a non-music diagnostic lane through fan-in.

        Intentionally not persisted and does not change the household source
        selector: same-path tests such as active-speaker commissioning enter
        through the correction lane while the speaker may otherwise be manually
        pinned to a music source.
        """

        label = str(label or "").strip()
        owner = str(owner or "").strip()
        if label not in FANIN_TEST_LABELS:
            return {"error": f"not a selectable test fan-in label {label!r}"}
        if owner not in FANIN_TEST_OWNERS:
            return {"error": f"not a recognized test fan-in owner {owner!r}"}
        async with self._transition_lock:
            if self._test_fanin_owner not in {None, owner}:
                return {
                    "error": (
                        "test fan-in gate is owned by "
                        f"{self._test_fanin_owner!r}"
                    ),
                }
            already_owned = self._test_fanin_owner == owner
            # Claim BEFORE low-level SELECT: its command may land even if the
            # response is lost. The owner-scoped release/lease can then recover
            # without risking another feature's gate.
            self._test_fanin_label = label
            self._test_fanin_owner = owner
            self._test_fanin_expires_at = time.monotonic() + FANIN_TEST_LEASE_SEC
            try:
                await self._fanin_select_label(label)
            except (OSError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
                if not already_owned:
                    try:
                        await self._restore_normal_fanin_gate()
                    except (
                        OSError,
                        asyncio.TimeoutError,
                        RuntimeError,
                        ValueError,
                    ) as rollback_exc:
                        log_event(
                            logger,
                            "source.test_select_rollback_failed",
                            label=label,
                            owner=owner,
                            reason=str(rollback_exc),
                            level=logging.ERROR,
                        )
                    else:
                        self._test_fanin_label = None
                        self._test_fanin_owner = None
                        self._test_fanin_expires_at = None
                return {"error": f"could not select the test source gate: {exc}"}
        log_event(logger, "source.test_select", label=label, owner=owner)
        return self._status_payload(self._state.playing)

    async def _restore_normal_fanin_gate(self) -> None:
        """Strictly restore the current household source gate."""

        if self._manual_source is not None:
            await self._fanin_select(self._manual_source)
        elif self._winner is not None and self._state.playing.get(
            self._winner, False,
        ):
            await self._fanin_select(self._winner)
        else:
            await self._fanin_none()

    async def release_test_fanin_label(
        self, owner: str, *, reason: str = "requested",
    ) -> dict[str, Any]:
        owner = str(owner or "").strip()
        if owner not in FANIN_TEST_OWNERS:
            return {"error": f"not a recognized test fan-in owner {owner!r}"}
        async with self._transition_lock:
            if self._test_fanin_owner not in {None, owner}:
                return {
                    "error": (
                        "test fan-in gate is owned by "
                        f"{self._test_fanin_owner!r}"
                    ),
                }
            released = self._test_fanin_label
            try:
                await self._restore_normal_fanin_gate()
            except (OSError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
                # Fail closed: retain owner + label so the caller can retry and
                # the per-tick diagnostic reassertion keeps music excluded.
                log_event(
                    logger,
                    "source.test_release_failed",
                    label=released,
                    owner=owner,
                    reason=str(exc),
                    level=logging.ERROR,
                )
                return {"error": f"could not restore the source gate: {exc}"}
            self._test_fanin_label = None
            self._test_fanin_owner = None
            self._test_fanin_expires_at = None
        log_event(
            logger,
            "source.test_release",
            label=released,
            owner=owner,
            reason=reason,
        )
        return self._status_payload(self._state.playing)

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
            "test_source": self._test_fanin_label,
            "test_owner": self._test_fanin_owner,
            "test_lease_remaining_sec": (
                max(0.0, self._test_fanin_expires_at - time.monotonic())
                if self._test_fanin_expires_at is not None
                else None
            ),
            "active_source": active,
            "winner": self._winner.value if self._winner else None,
            "last_handoff": self._last_handoff,
            "sources": {
                source.value: self._source_status_payload(source, current)
                for source in MUSIC_SOURCES
            },
            "reconciler": {
                "patrol_interval_sec": self.POLL_INTERVAL_SEC,
                "event_backed_probe_sec": EVENT_BACKED_PROBE_SEC,
                "patrols": self._patrol_count,
                "patrol_repairs": self._patrol_repairs,
                "pending_sources": sorted(
                    source.value for source in self._dirty_sources
                ),
                "last": self._last_reconcile,
            },
            "usbsink": {
                # Always true: fan-in DIRECT-captures the gadget on every box.
                "combo": True,
            },
        }

    def _source_status_payload(
        self,
        source: Source,
        current: dict[Source, bool],
    ) -> dict[str, Any]:
        last_notification = self._notification_last[source]
        return {
            "playing": bool(current.get(source, False)),
            "observation": self._state.observations[source],
            "notifications": self._notification_received[source],
            "notifications_coalesced": self._notification_coalesced[source],
            "last_notification_via": (
                last_notification[0] if last_notification is not None else None
            ),
            "last_notification_age_ms": (
                round((time.monotonic() - last_notification[1]) * 1000)
                if last_notification is not None
                else None
            ),
            # Process-local, monotonic evidence for latest-start-wins. Zero
            # means no confirmed inactive→active edge has been observed.
            "started_seq": self._state.started_seq[source],
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

    async def _observe_sources(
        self, *, defer_probes: frozenset[Source] = frozenset(),
    ) -> tuple[dict[Source, bool], list[Source]]:
        """Probe and record one source snapshot in serialized order.

        Automatic reconciliation and user control commands are separate event
        loop tasks. Keeping the awaitable probe inside this lock stops an older
        control-path snapshot from being committed after a newer patrol one and
        manufacturing a false stop/start edge.
        """
        async with self._observation_lock:
            current = await self._probe_sources(defer_probes=defer_probes)
            newly_started = self._record_source_observation(current)
            return current, newly_started

    def _record_source_observation(
        self,
        current: dict[Source, bool],
    ) -> list[Source]:
        """Record one authoritative source snapshot and return fresh starts.

        No await points: updating activation order and the previous-state
        snapshot is one event-loop-atomic operation shared by periodic/alert
        reconciliation and the explicit return-to-Auto path. Alerts never write
        this state; they only cause a new observation.

        Several starts first visible in one snapshot are sequenced in
        ``MUSIC_SOURCES`` order, so the last registry entry wins that tie.
        """
        newly_started: list[Source] = []
        for source in MUSIC_SOURCES:
            if current.get(source, False) and not self._state.playing[source]:
                self._started_seq += 1
                self._state.started_seq[source] = self._started_seq
                newly_started.append(source)
        self._state.playing = current
        return newly_started

    def _pick_winner(self, current: dict[Source, bool]) -> Source | None:
        """Choose the most recently started active source in Auto mode.

        ``started_seq`` is authoritative during this mux process. Registry
        order is a deterministic fallback for active sources with no observed
        start sequence.
        """
        active = self._active_sources(current)
        if not active:
            return None
        registry_order = {
            source: index for index, source in enumerate(MUSIC_SOURCES)
        }
        return max(
            active,
            key=lambda source: (
                self._state.started_seq[source],
                registry_order[source],
            ),
        )

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
        from .camilla import primary_controller
        from .assistant_volume import volume_context_publisher_for_runtime
        from .renderer import RendererClient
        from .speaker_name import runtime_name as speaker_runtime_name
        from .volume_coordinator import VolumeCoordinator
        from .volume_persistence import VolumePersistence
        from .volume_persistence import configured_path as volume_state_path

        camilla = primary_controller()
        persistence = VolumePersistence(volume_state_path())
        backend = RendererClient(librespot_state_path=self._librespot_state_path)
        coordinator = VolumeCoordinator(
            camilla=camilla,
            persistence=persistence,
            backend=backend,
            spotify_router=self._ensure_spotify_router(),
            spotify_device_name=speaker_runtime_name(),
            duck_active_probe=_make_duck_active_probe(),
            volume_context_publisher=volume_context_publisher_for_runtime(
                os.environ,
                dynamic_topology=True,
            ),
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

    async def _transition_to_source_locked(
        self,
        prev_source: Source,
        source: Source,
        *,
        reason: str,
        commit_selection: Callable[[], None],
    ) -> bool:
        """Move the lane and publish its mux owner as one volume operation.

        ``_transition_lock`` is held by the caller. The volume coordinator's
        cross-daemon lease additionally excludes source-volume observations
        until ``commit_selection`` has made STATUS authoritative for the lane
        fan-in now exposes.
        """
        coordinator = self._ensure_volume_coordinator()
        async with coordinator.source_handoff_operation():
            selected = await self._transition_volume_and_gate_locked(
                coordinator,
                prev_source,
                source,
                reason=reason,
            )
            if selected:
                # No await between publication and lease release: a queued
                # observer must see the new owner when it acquires the lease.
                commit_selection()
        # Snapshotting volume context takes the coordinator's local mutation
        # lock, so publish only after the handoff lease has released: the
        # ordering above requires carrier/gate/owner publication, not
        # observability IPC, to be atomic with source-volume writers.
        with contextlib.suppress(Exception):
            await coordinator.publish_volume_context()
        return selected

    async def _transition_volume_and_gate_locked(
        self,
        coordinator: Any,
        prev_source: Source,
        source: Source,
        *,
        reason: str,
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

    def _commit_auto_winner(self, source: Source) -> None:
        """Publish a reconciler-selected winner while the volume lease is held."""
        self._winner = source
        self._pending_auto_target = None
        self._winner_age_ticks = 0

    def _commit_manual_selection(self, source: Source) -> None:
        """Publish and persist a manual selection inside the handoff lease."""
        self._commit_auto_winner(source)
        self._manual_source = source
        mux_mode_persistence.write_mode(self._mode_state_path, source)

    def _commit_auto_selection(self, source: Source) -> None:
        """Publish return-to-auto state inside the handoff lease."""
        self._commit_auto_winner(source)
        self._manual_source = None
        mux_mode_persistence.write_mode(self._mode_state_path, None)

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
        return await fanin_command(
            f"SELECT {label}", socket_path=FANIN_CONTROL_SOCKET,
        )

    async def _fanin_auto(self) -> dict[str, Any]:
        return await fanin_command("AUTO", socket_path=FANIN_CONTROL_SOCKET)

    async def _fanin_none(self) -> dict[str, Any]:
        return await fanin_command("NONE", socket_path=FANIN_CONTROL_SOCKET)

    async def _fanin_lane_mute(
        self, label: str, muted: bool,
    ) -> dict[str, Any]:
        """MUTE/UNMUTE one fan-in input lane at its mix stage.

        A per-lane silence on the same mux→fan-in control channel as the
        selected-input gate (SELECT/AUTO/NONE), orthogonal to selection and to
        volume. Lane-general, like SELECT."""
        verb = "MUTE" if muted else "UNMUTE"
        return await fanin_command(
            f"{verb} {label}", socket_path=FANIN_CONTROL_SOCKET,
        )

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
            parent = os.path.dirname(MUX_CONTROL_SOCKET_PATH)
            if parent:
                os.makedirs(parent, exist_ok=True)
            try:
                os.unlink(MUX_CONTROL_SOCKET_PATH)
            except FileNotFoundError:
                pass
            server = await asyncio.start_unix_server(
                self._handle_control_client,
                path=MUX_CONTROL_SOCKET_PATH,
            )
            # 0660: mux runs as the non-root user jasper-mux with primary group
            # `jasper`, so the socket is jasper-mux:jasper and only root plus
            # the `jasper` group (jasper-control / jasper-web clients) can
            # connect. Best-effort, like the voice / peering sockets' post-bind
            # chmod.
            try:
                os.chmod(MUX_CONTROL_SOCKET_PATH, 0o660)
            except OSError as e:
                logger.warning("mux control socket chmod failed: %s", e)
            logger.info("mux control socket listening at %s", MUX_CONTROL_SOCKET_PATH)
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
            elif command.startswith("NOTIFY "):
                source_name = command.split(" ", 1)[1].strip()
                try:
                    source = Source(source_name)
                except ValueError:
                    payload = {"error": f"unknown source {source_name!r}"}
                else:
                    if source not in MUSIC_SOURCES:
                        payload = {
                            "error": f"not a music source {source_name!r}",
                        }
                    else:
                        self.notify_source_changed(source, "uds")
                        payload = {
                            "accepted": True,
                            "source": source.value,
                            "policy_applied": False,
                        }
            elif command == "AUTO":
                payload = await self.auto_select()
            elif command.startswith("TEST_SELECT "):
                parts = command.split()
                if len(parts) != 3:
                    payload = {
                        "error": "TEST_SELECT requires a label and owner",
                    }
                else:
                    payload = await self.select_test_fanin_label(
                        parts[1], parts[2],
                    )
            elif command.startswith("TEST_RELEASE"):
                parts = command.split()
                if len(parts) != 2:
                    payload = {"error": "TEST_RELEASE requires an owner"}
                else:
                    payload = await self.release_test_fanin_label(parts[1])
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
    # Losing-source cleanup. Handoff policy lives above; these helpers only
    # perform each renderer's source-specific I/O after the gate has moved.
    # ------------------------------------------------------------------

    async def _pause(self, source: Source) -> None:
        logger.info("preempting %s", source.value)
        if source == Source.SPOTIFY:
            ok = await self._spotify_pause_via_web_api()
            if ok:
                return
            # Tier 1 failed. An un-pauseable librespot owns its private fan-in
            # lane and keeps streaming, so it would be summed with the new
            # winner; escalate to force a release.
            if _spotify_preempt_restart_disabled():
                logger.warning(
                    "spotify pause: no Web API account could pause the "
                    "JTS device; escalation disabled — AirPlay and "
                    "Spotify will mix until the user pauses on phone",
                )
                return
            logger.warning(
                "spotify pause: Web API failed; escalating to "
                "`systemctl try-restart librespot.service` to force "
                "release of the fan-in spotify lane if still active",
            )
            await self._spotify_force_restart_librespot()
        elif source == Source.AIRPLAY:
            await self._airplay_drop_session_for_preempt()
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

    async def _airplay_drop_session_for_preempt(self) -> None:
        """Drop the AirPlay receiver session after another source wins.

        Keeping an AP2 session alive once another source owns the audible lane
        leaves the sender routed to an inaudible receiver. ``DropSession`` is
        receiver-owned and forcibly terminates that connection; MPRIS ``Stop``
        is only a remote request the sender may ignore, so it is retained
        solely as a compatibility fallback when the native method is
        unavailable. This cleanup runs after fan-in has moved; failure never
        rolls back or weakens the authoritative audible-lane handoff.
        """
        dropped = await _busctl(
            "call",
            SHAIRPORT_NATIVE_BUS,
            SHAIRPORT_NATIVE_PATH,
            SHAIRPORT_NATIVE_IFACE,
            "DropSession",
        )
        if dropped is not None:
            log_event(
                logger,
                "airplay.preempt_drop_session",
                method="DropSession",
                result="ok",
            )
            return

        log_event(
            logger,
            "airplay.preempt_drop_session_failed",
            method="DropSession",
            action="mpris_stop_fallback",
            level=logging.WARNING,
        )
        stopped = await _busctl(
            "call",
            SHAIRPORT_MPRIS_BUS,
            SHAIRPORT_MPRIS_PATH,
            MPRIS_PLAYER_IFACE,
            "Stop",
        )
        if stopped is not None:
            log_event(
                logger,
                "airplay.preempt_stop",
                method="Stop",
                result="fallback_ok",
            )
            return

        log_event(
            logger,
            "airplay.preempt_stop_failed",
            method="Stop",
            action="new_source_remains_authoritative",
            level=logging.WARNING,
        )

    # ------------------------------------------------------------------
    # USB sink preempt protocol — MUTE/UNMUTE the fan-in usbsink lane.
    # fan-in DIRECT-captures the gadget, so the lane's mix-stage mute is
    # the only USB-silencing primitive.
    # ------------------------------------------------------------------

    async def _usbsink_set_preempt(self, silenced: bool, *, reason: str) -> None:
        """Silence/un-silence the USB source when it loses/regains the speaker.

        The lane keeps reporting its pre-mute frames/level, so mux still sees a
        muted-but-streaming host as "playing" (no mute→release→mute flap).

        No-ops if the requested state matches the tracked state, so a tick that
        re-emits the same decision doesn't generate stale commands.
        ``self._usbsink_preempted`` advances only on success, so a failure is a
        bounded WARN plus graceful mixing and mux re-attempts on the next tick
        (1 Hz, no storm); the escape hatch degrades to never-silence."""
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
        await self._usbsink_set_preempt_fanin(silenced, reason=reason)

    async def _usbsink_set_preempt_fanin(
        self, silenced: bool, *, reason: str,
    ) -> None:
        """Preempt transport: MUTE/UNMUTE the fan-in usbsink lane.

        The mute is applied at fan-in's mix stage only; the lane's capture and
        per-lane telemetry (frames_read / rms_dbfs) are untouched, so combo
        liveness still reads the host's true activity. NOT persisted by fan-in
        — a fan-in restart comes up unmuted, and
        ``_reassert_usbsink_preempt_mute`` re-mutes on the next tick."""
        try:
            await self._fanin_lane_mute(USBSINK_FANIN_LABEL, silenced)
        except Exception as e:  # noqa: BLE001
            # Tracked flag deliberately NOT advanced, so the next tick
            # re-attempts.
            logger.warning(
                "usbsink fanin lane mute failed (muted=%s reason=%s): %s; "
                "audio may briefly mix",
                silenced, reason, e,
            )
            return
        self._usbsink_preempted = silenced
        log_event(
            logger,
            "usbsink.preempt_set",
            silenced=silenced,
            reason=reason,
            via="fanin_mute",
        )

    async def _reassert_usbsink_preempt_mute(self) -> None:
        """Re-issue the fan-in usbsink lane MUTE while USB is preempted.

        fan-in does NOT persist the mute (it comes up unmuted on restart), so a
        fan-in bounce mid-preempt would drop the silence while mux still tracks
        ``_usbsink_preempted=True`` and would never re-mute — the state guard in
        ``_usbsink_set_preempt`` short-circuits the unchanged decision. This
        per-reconcile reassertion closes that gap: the next alert or patrol
        re-mutes an unmuted-after-restart lane. Idempotent on the fan-in side
        (it logs only on a real flip, so no steady-state journal spam) and
        fail-soft.

        No-op when USB isn't preempted or when the escape hatch is set."""
        if not self._usbsink_preempted:
            return
        if _usbsink_preempt_disabled():
            return
        try:
            await self._fanin_lane_mute(USBSINK_FANIN_LABEL, True)
        except Exception as e:  # noqa: BLE001
            logger.warning("usbsink fanin lane mute reassert failed: %s", e)

    # ------------------------------------------------------------------
    # Spotify Web API helpers — librespot 0.8.0 has no local control
    # HTTP, so pausing Spotify means driving Spotify's cloud → spirc →
    # librespot, through the same multi-account router the voice tools use.
    # ------------------------------------------------------------------

    def _ensure_spotify_router(self) -> Any | None:
        """Build the multi-account Spotify router on first use, or return the
        cached one. None means Spotify env vars aren't set, so the Web API
        pause path is unavailable."""
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
            from .accounts import DEFAULT_REGISTRY_PATH, LEGACY_CACHE_PATH, Registry, maybe_migrate_legacy
            from .spotify_router import Router, build_clients
            registry = Registry.load(os.environ.get(
                "JASPER_SPOTIFY_ACCOUNTS_PATH",
                DEFAULT_REGISTRY_PATH,
            ))
            maybe_migrate_legacy(
                registry,
                os.environ.get("SPOTIFY_CACHE_PATH", LEGACY_CACHE_PATH),
                default_name="default",
            )
            result = build_clients(
                registry,
                client_id=client_id,
                redirect_uri=resolved_spotify_redirect_uri(),
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
        """Try every authorized account; pause whichever has the JTS device.
        Returns True if any account successfully paused.

        The Web API's `is_active` flag lags player state and can be stale for
        several seconds, so librespot may be emitting audio to JTS while the
        flag still names the previous device. Devices matching the speaker name
        are therefore tried regardless of `is_active`; an unreachable device
        just errors out of pause_playback, swallowed at debug level.
        """
        router = self._ensure_spotify_router()
        if router is None:
            return False
        from .speaker_name import runtime_name as _speaker_runtime_name
        device_name = _speaker_runtime_name()
        matches = await router.devices_named(device_name)
        # is_active devices first (lowest-latency path); fall through to
        # inactive JTS-named devices in the same pass — never retried twice.
        ordered = sorted(matches, key=lambda m: not m[1].get("is_active"))
        for ac, d in ordered:
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
        """Tier 2 escalation: try-restart librespot.service to force an
        active instance to drop its FD on the Spotify fan-in lane.

        librespot exits and closes its private Spotify lane writer
        (`librespot_substream`, or that lane's SHM ring on a box armed for ring
        ingress — the arbitration is identical either way); fan-in then reads
        silence on that lane while the new winner's continues. systemd respawns
        librespot in ~2-3 s (Restart=always), and during that gap the new winner
        is heard alone. After respawn librespot is back as an idle Spotify
        Connect device: the credential cache (--system-cache
        /var/cache/librespot) persists, so the phone re-sees the speaker in the
        Connect picker without re-authenticating, but any state inside the
        current session (track position, queue) is lost.

        `systemctl try-restart` rather than `restart` or `kill -TERM`, so the
        same `Restart=always` policy that handles every other active librespot
        exit handles this one, while a concurrent household Off or follower park
        wins the race and stays stopped.

        Returns True when the active-only mutation succeeds, including systemd's
        intentional no-op for an already-inactive unit. Logged but not retried
        on failure: the failure mode is "try-restart unavailable", which does
        not self-heal.

        Routed through jasper-control's restart broker (off-thread, since the
        broker client is blocking) so jasper-mux needs no privilege of its own.
        The broker client falls back to a direct systemctl if the broker is
        unreachable.
        """
        resp = await asyncio.to_thread(
            restart_broker.manage_units,
            "librespot.service", verb="try-restart",
            reason="spotify Tier-2 recovery", no_block=False, timeout=8.0,
        )
        if not resp.get("ok"):
            logger.warning(
                "spotify force-restart: librespot try-restart failed: %s",
                resp.get("error") or f"rc={resp.get('rc')}",
            )
            return False
        logger.info(
            "spotify force-restart: librespot.service try-restart completed "
            "(active-only Tier 2 escalation succeeded)",
        )
        return True


async def _busctl(*args: str) -> Optional[str]:
    """Run busctl on the system bus; stdout on success, None on any error."""
    stdout = await system_busctl(*args)
    if stdout is None:
        return None
    return stdout.decode("utf-8", "replace")


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
        camilla_locked = response.get("camilla_volume_locked")
        if isinstance(camilla_locked, bool):
            return camilla_locked
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
        default=librespot_state.configured_path(),
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
