# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-mux — renderer source-arbiter.

Native producer notifications wake one host-owned reconciler, which re-reads
all source state and applies one source-neutral latest-start-wins policy. A
fixed 1 Hz patrol invokes that exact same reconciler as a lost-alert safety net.
Alerts are therefore hints, never routing commands: stale and duplicate alerts
are harmless, and the patrol cannot disagree with a separate alert policy
because none exists.

Renderer support:
  Spotify (librespot):
    detect: read /run/librespot/state.json (written by
            --onevent hook on every player event)
    pause:  Two-tier escalation. Tier 1 is Spotify Web API via
            spotipy — librespot 0.8.0 has no local control HTTP.
            We iterate household accounts and issue
            PUT /me/player/pause to any account that has the configured
            speaker device in its list. Tier 2 (added 2026-05-22) is
            `systemctl try-restart librespot.service` if Tier 1 fails
            — guarantees librespot releases its private fan-in lane
            when it is still active, without resurrecting a concurrently
            disabled or role-parked source. Tier 2 is still useful
            after the 2026-05-26 fan-in cutover: renderers no longer
            share one ALSA device, so an un-pauseable librespot would
            keep streaming into its own lane and be summed alongside
            the new winner. Off-switch:
            JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled.
  AirPlay (shairport-sync):
    detect: MPRIS PlaybackStatus == "Playing" AND non-empty MPRIS
            Metadata xesam:title (source_state.airplay_playing) — the
            metadata corroboration stops mux flapping on macOS's ~30 s
            AirPlay keepalive cycles, which report Playing with no
            audio actually reaching the speakers.
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
    detect: fan-in DIRECT-captures the gadget, so USB liveness comes
            from fan-in DIRECT-lane telemetry. See _usbsink_playing.
            NOTE: liveness is now purely "is the host streaming frames to
            us" — there is no audio-LEVEL gate. A faint sound is still a
            sound; if USB is the only source, we play it.
    pause:  MUTE the fan-in usbsink lane at its mix stage. When all
            other sources go idle, we release the preempt (unmute) so
            an already-streaming host can resume. A new USB start clears
            the mute before USB takes the selected lane.

Automatic source policy:
  Every source is an equal candidate. A confirmed inactive→active transition
  becomes the winner, including USB, so Auto has one explainable rule: the
  latest source to start wins. The losing sources keep their existing
  source-specific preemption behavior (AirPlay Stop, Spotify/BT pause, USB lane
  mute). Alerts only accelerate the authoritative re-read; alert arrival order
  never chooses the winner.

  Mux records a process-local activation sequence for every confirmed start,
  including starts observed while a manual pin owns the gate. That sequence
  chooses the most recently started still-active source when the winner stops
  or the user returns to Auto. Multiple starts first observed in one snapshot
  are ordered deterministically by MUSIC_SOURCES registry order because their
  real-world order is unknowable. A persistent manual pin overrides Auto, and
  /sources remains the lifecycle surface for disabling a source entirely.

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

from jasper.log_event import log_event

from . import librespot_state, mux_mode_persistence
from .bluetooth.avrcp import bluetooth_avrcp_call
from .control import restart_broker
from .audio_runtime_plan import (
    SourceRouteDecision,
    decide_source_low_latency_route,
    low_latency_feature_flags,
)
from .fanin.control import fanin_command
from .music_sources import MUSIC_SOURCES, SOURCE_TO_FANIN_LABEL, Source
from .source_state import (
    airplay_playing_observed as airplay_playing,
    bluetooth_playing_observed as bluetooth_playing,
    spotify_playing_observed as spotify_playing,
    usbsink_direct_frames_read,
    usbsink_direct_streaming,
)
from .spotify_oauth import default_spotify_redirect_uri

logger = logging.getLogger(__name__)


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
# The fan-in input-lane label for the USB source. USB preempt is a MUTE/UNMUTE
# of THIS lane over the fan-in control socket — the only USB-silencing primitive
# now that fan-in DIRECT-captures the gadget as its sole live ingress owner.
# Derived from the same
# source→label map fan-in SELECT uses, so the two never drift.
USBSINK_FANIN_LABEL = SOURCE_TO_FANIN_LABEL[Source.USBSINK]
FANIN_TEST_LABELS = frozenset({"correction"})
FANIN_TEST_OWNERS = frozenset({
    "active-speaker-commissioning",
    "correction-measurement",
})
# A diagnostic owner must renew before this monotonic deadline. This is long
# enough for the 35 s commissioning tone; correction renews every 20 s. A web
# worker crash therefore self-recovers instead of pinning household music off.
FANIN_TEST_LEASE_SEC = 60.0
SHAIRPORT_MPRIS_BUS = "org.mpris.MediaPlayer2.ShairportSync"
SHAIRPORT_MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


def _spotify_preempt_restart_disabled() -> bool:
    """Env-var escape hatch for the Spotify-preempt Tier 2 escalation
    (the active-only systemctl try-restart fallback added 2026-05-22).

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
    short-circuit `_usbsink_set_preempt` — mux stops MUTE/UNMUTE-ing the
    fan-in usbsink lane when another source wins (the only USB-silencing
    primitive; jasper-fanin DIRECT-captures the gadget as its sole live ingress
    owner).
    USB then behaves like an unsupported source (audio briefly mixes when a
    new source starts). Operator escape hatch for cases where the lane mute
    is causing unexpected disruption, without requiring a redeploy or daemon
    restart. Default: enabled.

    Mirrors JASPER_AIRPLAY_METADATA_GATE / JASPER_MUX_SPOTIFY_PREEMPT_RESTART
    / JASPER_SHAIRPORT_SUPERVISOR.
    """
    return os.environ.get(
        "JASPER_USBSINK_PREEMPT", "",
    ).strip().lower() == "disabled"


# Combo-mode USB streaming debounce (mux ticks at POLL_INTERVAL_SEC = 1 Hz).
# The liveness signal is fan-in's DIRECT-lane host-input counter, read via
# usbsink_direct_frames_read(). The debounce rides through a brief delivery gap
# (a status miss / a momentary stall) so the source doesn't flap; a real pause
# stops the frames and macOS tears the stream down, so the counter genuinely
# stalls and USB releases after this many ticks.
USBSINK_COMBO_STOP_TICKS = 2
ALERT_COALESCE_SEC = 0.05
# A transient unreadable probe must not synthesize stop/start flutter, but a
# permanently dead adapter must not pin a vanished winner forever. Hold an
# active last-known state for this bounded grace, then fail inactive until a
# successful observation re-establishes it.
UNKNOWN_ACTIVE_HOLD_SEC = 5.0


@dataclass(frozen=True)
class ComboLiveness:
    """Temporal state for combo-mode USB frames-flowing detection.

    ``streaming`` is "is the host feeding us frames right now" — there is NO
    audio-LEVEL component (removed 2026-07-17). A faint sound and a loud one
    both stream frames and therefore produce the same authoritative source-start
    edge. The old ``rms_dbfs > -60`` gate attempted to infer intent from level;
    it instead dropped faint audio and caused quiet-passage routing dropouts, so
    level is display-only and does not participate in arbitration. Users who do
    not want computer audio to enter latest-start-wins Auto can persistently pin
    another source or disable USB Audio Input. Removing the level gate fixed
    dropped faint audio and level-driven quiet-passage dropouts: a quiet stretch
    keeps the counter advancing, so the lane no longer reads "stopped". New
    fan-in builds publish a 20 Hz-derived streaming edge; this state machine
    remains the rolling-upgrade fallback for older STATUS shapes. A host that
    actually tears the stream down still stops frames and releases after the
    stop hysteresis.
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
    ``frames`` grew since the previous tick (the host is feeding the lane).

    Semantics:

    - advanced -> streaming, idle reset.
    - first reading or counter reset -> re-baseline without inventing a delta.
    - flat frames -> drop after ``stop_ticks`` consecutive non-advancing patrols.
    - missing frames -> unknown; retain the complete prior state. A STATUS miss
      is not evidence that a stream stopped.
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
    """Per-source playing flag from the previous tick. The mux uses
    `prev → current` transitions to drive preemption — we only act
    when a source goes from not-playing to playing."""
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
        # Every caller that refreshes source state goes through
        # _observe_sources(). Serializing probe + record prevents a slower,
        # older control-path snapshot from overwriting a newer patrol snapshot
        # and manufacturing a false stop/start edge.
        self._observation_lock = asyncio.Lock()
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
        # USB sink preempt state: True while we've told fan-in to silence the
        # USB lane. Cleared before USB becomes the winner or after all other
        # sources go idle, so source selection and the defense-in-depth mute
        # cannot disagree.
        self._usbsink_preempted = False
        # USB liveness (see step_combo_liveness). fan-in DIRECT-captures the USB
        # gadget, so `_usbsink_playing` measures liveness off that DIRECT lane.
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
        low_latency_flags = low_latency_feature_flags()
        # Adaptive fan-in OUTPUT-buffer (default-OFF): shrink fan-in's near-FULL
        # output buffer when USB is the sole exclusive winner, and restore the
        # full default otherwise. Parsed ONCE here so the _tick hot path makes no
        # env read per tick and the disabled path is provably byte-identical.
        # `_buffer_shrunk` tracks whether we have the shrunk override armed, so
        # shrink/restore are idempotent across ticks (act only on the edge).
        self._adaptive_buffer_enabled = low_latency_flags.adaptive_buffer
        self._buffer_shrunk = False
        # Re-arm backoff: a failed shrink (env write or fanin restart failed)
        # must not restart-storm the shared daemon every tick. Stay full until
        # the source set changes.
        self._buffer_shrink_blocked = False
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
                    # Startup uses the same protected reconciliation path as
                    # every later patrol. A transient first probe must not exit
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
                    try:
                        await asyncio.wait_for(
                            self._reconcile_wake.wait(), timeout=timeout,
                        )
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
                        # the dirty set (there is no await between these
                        # operations). A later alert then remains set for the
                        # next loop instead of causing an empty reconciliation.
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
        await self._tick()
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

    async def _probe_sources(self) -> dict[Source, bool]:
        spotify, airplay, bluetooth, usbsink = await asyncio.gather(
            spotify_playing(self._librespot_state_path),
            airplay_playing(),
            bluetooth_playing(),
            self._usbsink_playing(),
        )
        observed: dict[Source, bool | None] = {
            Source.SPOTIFY: spotify,
            Source.AIRPLAY: airplay,
            Source.BLUETOOTH: bluetooth,
            Source.USBSINK: usbsink,
        }
        resolved = dict(self._state.playing)
        now = time.monotonic()
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

        fan-in DIRECT-captures the gadget as its sole live ingress owner. New
        builds publish an edge-detected ``direct.streaming`` boolean from their
        existing frame counter; older builds fall back to counter deltas across
        patrols. There is NO audio-level gate (see the module docstring's
        "Sticky sessions"). A missing/non-direct snapshot is unknown and retains
        the arbiter's last-known state; do not issue a second STATUS probe.
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

    async def _tick(self) -> None:
        current, newly_started = await self._observe_sources()
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
            # A diagnostic lane owns fan-in; never the exclusive-USB low-latency
            # path -> restore the full buffer if shrunk.
            await self._settle_low_latency_audio(current)
            return

        if self._manual_source is not None:
            await self._reassert_manual_source()
            # Manual pin is not the auto exclusive-USB low-latency trigger ->
            # restore the full buffer if it was shrunk.
            await self._settle_low_latency_audio(current)
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
                # If the new winner is USBSINK and an older source transition
                # left its defense-in-depth lane mute set, clear that mute
                # before selecting USB. Inside the lock (like select_source /
                # auto_select) so a concurrent manual selection cannot
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
                # Handoff didn't settle — never shrink on an unsettled gate;
                # restore the full buffer if it was shrunk.
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

        # Reassert the combo-box fan-in lane mute if still preempted (handles a
        # fan-in restart that came up unmuted). No-op on solo / not-preempted /
        # escape-hatch. After the release above so a just-released lane isn't
        # re-muted this tick.
        await self._reassert_usbsink_preempt_mute()

        await self._settle_low_latency_audio(current)

    async def _settle_low_latency_audio(self, current: dict[Source, bool]) -> None:
        """Drive optional low-latency consumers from one source-route decision."""
        if not self._adaptive_buffer_enabled:
            return
        decision = self._source_low_latency_decision(current)
        await self._settle_adaptive_buffer(decision)

    def _source_low_latency_decision(
        self,
        current: dict[Source, bool],
    ) -> SourceRouteDecision:
        """Single source-route policy for the adaptive low-latency consumer."""
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

        SCOPE: AUTO mode only for the shrink. A manual pin or an active
        diagnostic (test) lane is treated as non-exclusive -> it gets the
        restore-to-full path so a manual/correction run while the buffer was
        shrunk unwinds it.
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
            # Any non-exclusive route clears the shrink-block (a fresh
            # exclusive-USB edge gets a clean retry) and restores the full
            # buffer if shrunk.
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
        failure we keep ``_buffer_shrunk`` set so the NEXT non-exclusive tick
        retries the unwind — convergent, and the buffer_reconcile's SF-1
        rollback means the persisted value never leads the daemon either way."""
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

        This is intentionally not persisted and does not change the household
        source selector. It exists for same-path tests such as active-speaker
        commissioning that enter through the correction lane while the speaker
        may otherwise be manually pinned to AirPlay/Spotify/etc.
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
        # Local import mirrors the adaptive ladder's pattern (keeps the control
        # package off mux's module-load path); cached after first use.
        from jasper.fanin import buffer_reconcile as br

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
                "patrols": self._patrol_count,
                "patrol_repairs": self._patrol_repairs,
                "pending_sources": sorted(
                    source.value for source in self._dirty_sources
                ),
                "last": self._last_reconcile,
            },
            "usbsink": {
                # fan-in DIRECT-captures the gadget on every box now; the aloop
                # bridge path and its resident helper were removed.
                "combo": True,
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
        self,
    ) -> tuple[dict[Source, bool], list[Source]]:
        """Probe and record one source snapshot in serialized order.

        Automatic reconciliation and user control commands are separate event
        loop tasks. Keeping the awaitable probe inside this narrow lock ensures
        their snapshots cannot be committed out of probe order.
        """
        async with self._observation_lock:
            current = await self._probe_sources()
            newly_started = self._record_source_observation(current)
            return current, newly_started

    def _record_source_observation(
        self,
        current: dict[Source, bool],
    ) -> list[Source]:
        """Record one authoritative source snapshot and return fresh starts.

        This method has no await points: updating activation order and the
        previous-state snapshot is one event-loop-atomic operation shared by
        periodic/alert reconciliation and the explicit return-to-Auto path.
        Alerts never write this state; they only cause a new observation.

        If several starts are first visible in one snapshot, iteration follows
        ``MUSIC_SOURCES`` order. The last registry entry therefore wins the
        deterministic tie because the sources' real start order is unknowable.
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
        start sequence (possible only through direct state injection or future
        rolling-upgrade compatibility paths).
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
        from .camilla import CamillaController
        from .assistant_volume import volume_context_publisher_for_runtime
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
            with contextlib.suppress(Exception):
                await coordinator.publish_volume_context()
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
            with contextlib.suppress(Exception):
                await coordinator.publish_volume_context()
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
        # Prepare/finalize can mutate Camilla even when the handoff does not
        # complete. Always converge the pre-DSP TTS context to the carrier that
        # actually remains after success, rollback, or degraded failure.
        with contextlib.suppress(Exception):
            await coordinator.publish_volume_context()
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

        Extends the same mux→fan-in control channel used for the selected-input
        gate (SELECT/AUTO/NONE) with a per-lane silence that is orthogonal to
        selection and to volume. Today's only caller is the combo-box USB
        preempt; the command is lane-general (mirrors SELECT)."""
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
            # try-restart kills librespot's FD only while the source remains
            # active; the new winner is then heard alone for the ~2-3 s before
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
                "`systemctl try-restart librespot.service` to force "
                "release of the fan-in spotify lane if still active",
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
    # USB sink preempt protocol — MUTE/UNMUTE the fan-in usbsink lane.
    # fan-in DIRECT-captures the gadget, so the lane's mix-stage mute is
    # the only USB-silencing primitive.
    # ------------------------------------------------------------------

    async def _usbsink_set_preempt(self, silenced: bool, *, reason: str) -> None:
        """Silence/un-silence the USB source when it loses/regains the speaker.

        jasper-fanin DIRECT-captures the gadget as its sole live ingress owner,
        so USB is silenced by ``MUTE``/``UNMUTE`` of the fan-in usbsink lane at
        its mix stage. The lane keeps reporting its pre-mute
        frames/level, so mux still sees a muted-but-streaming host as "playing"
        (no mute→release→mute flap). See ``_usbsink_set_preempt_fanin``.

        No-ops if the requested state matches our tracked state, so a tick that
        re-emits the same decision doesn't generate stale commands.
        ``self._usbsink_preempted`` advances only on success, so a failure is a
        bounded WARN + graceful mixing and mux re-attempts on the next tick
        (1 Hz, no storm) — the escape hatch degrades to never-silence."""
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
        liveness (`step_combo_liveness`) still reads the host's true activity.
        NOT persisted by fan-in — a fan-in restart comes up unmuted, and
        ``_reassert_usbsink_preempt_mute`` re-mutes on the next tick."""
        try:
            await self._fanin_lane_mute(USBSINK_FANIN_LABEL, silenced)
        except Exception as e:  # noqa: BLE001
            # Bounded WARN, graceful mixing, and re-attempt next tick (tracked
            # flag NOT advanced) — no retry storm, no silent failure.
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
        ``_usbsink_preempted=True`` and would never re-mute (the state guard in
        ``_usbsink_set_preempt`` short-circuits the unchanged decision). This
        per-reconcile reassertion closes that gap — the next alert or patrol re-mutes an
        unmuted-after-restart lane. Idempotent on the fan-in side (it logs only
        on a real flip → no steady-state journal spam), alert-coalesced with a
        fixed 1 Hz patrol fallback, and fail-soft. Mirrors
        ``_reassert_auto_winner``'s
        SELECT reassertion.

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
            default_redirect_uri = default_spotify_redirect_uri(hostname)
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
        """Tier 2 escalation: try-restart librespot.service to force an
        active instance to drop its FD on the Spotify fan-in lane.

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

        We use `systemctl try-restart` rather than `restart` or `kill -TERM` so the
        same `Restart=always` policy that handles every other
        active librespot exit also handles this one, while a concurrent
        household Off or follower park wins the race and stays stopped.

        Returns True when the active-only mutation succeeds, including
        systemd's intentional no-op for an already-inactive unit. Logged but
        not retried on failure (the only thing that would happen on retry is
        more log spam — the failure mode is "try-restart unavailable" which
        doesn't self-heal).

        WS1 Phase 3: routed through jasper-control's restart broker
        (off-thread, since the broker client is blocking) so jasper-mux
        needs no privilege of its own once dropped to a non-root service
        user. While mux is still root the broker client falls back to a
        direct systemctl if the broker is unreachable.
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
