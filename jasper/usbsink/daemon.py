"""USB sink daemon orchestration.

Wires the audio bridge together with the systemd watchdog and the
sub-systems that bolt on in later phases (state publisher, preempt
listener, volume bridge). Phase 2 covers audio + watchdog only;
later phases extend `UsbSinkDaemon.run()` with the other concerns.

Lifecycle (Phase 2):
    daemon = UsbSinkDaemon.from_env()
    daemon.run()                      # blocking until signal

    [signal]
    daemon.request_stop()             # graceful shutdown

The watchdog heartbeat reflects forward progress in the audio bridge's
playback/output callback. That side is expected to keep emitting either
host audio or explicit silence while the feature is enabled. Capture
progress is *not* a health requirement: a USB host can be unplugged,
plugged in but paused, or using another source, and all of those are
normal idle states. See jasper.watchdog for the sentinel pattern.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from .audio_bridge import AudioBridge, BridgeStats
from .preempt_listener import (
    DEFAULT_PORT as PREEMPT_DEFAULT_PORT,
    DEFAULT_STATE_PATH as PREEMPT_DEFAULT_STATE_PATH,
    PreemptListener,
)
from .state_publisher import (
    DEFAULT_STATE_PATH as STATE_DEFAULT_PATH,
    StatePublisher,
)
from .volume_bridge import (
    DEFAULT_CONTROL_URL as VOLUME_DEFAULT_CONTROL_URL,
    VolumeBridge,
)

logger = logging.getLogger(__name__)


# Diagnostic-snapshot cadence. Every N seconds we log the bridge's
# stats counters so journald has a steady trickle of evidence even when
# nothing's wrong. Cheap — no work on the audio thread, just
# observation.
DIAG_INTERVAL_SEC = 30.0

# Watchdog progress-stale threshold. If the playback/output callback
# has not fired in this long while the bridge is supposed to be running,
# we stop patting the systemd watchdog and systemd restarts us. Capture
# silence is normal for an optional USB source; playback silence is
# still useful work because it proves the renderer lane is being serviced.
WATCHDOG_STALE_SEC = 8.0

# Capture-idle transition log threshold. Uses the same window as the
# watchdog stale threshold so the operator sees "USB host idle" around
# the time the old implementation used to begin failing, but this is
# INFO-only state, not a recovery decision.
CAPTURE_IDLE_LOG_SEC = WATCHDOG_STALE_SEC


@dataclass
class DaemonConfig:
    """Environment-driven configuration, all overridable via env vars
    that begin with JASPER_USBSINK_*. Sensible defaults align with the
    UAC2 gadget descriptor in deploy/usbsink/jasper-usbsink-gadget-up.

    The same ALSA card has TWO names that tools disagree about:

      kernel "short" name (in brackets in /proc/asound/cards, used by
        amixer -c <name>, used as the /proc/asound/<name> directory):
          "UAC2Gadget"          ← MIXER_CARD

      kernel "long" / driver name (used by PortAudio for substring
        matching against `sd.query_devices()` output, which formats
        the device as "UAC2_Gadget: PCM (hw:4,0)"):
          "UAC2_Gadget"         ← CAPTURE_DEVICE

    They differ because the u_audio driver registers itself as
    "UAC2_Gadget" (the underscore is the driver's convention) while
    our ConfigFS descriptor's short-name attribute is "UAC2Gadget"
    (no underscore, set in deploy/usbsink/jasper-usbsink-gadget-up).
    sounddevice/PortAudio doesn't honor the short name when matching
    device strings against the enumerated list, so we have to pass
    it the underscore form. amixer wants the short name.

    Keep these two as separate settings so each tool gets the form
    it can resolve. Hardcoding either in one place breaks the other.
    """
    # PortAudio device string for the gadget capture endpoint —
    # matched as a substring against sd.query_devices(). The display
    # name format is "UAC2_Gadget: PCM (hw:4,0)" so the underscore
    # form is what matches.
    capture_device: str = "UAC2_Gadget"
    # Private fan-in lane for the USB-audio renderer. jasper-fanin reads
    # the capture side and publishes the summed music stream.
    playback_device: str = "usbsink_substream"
    # ALSA "short" name (no underscore) — what amixer -c and
    # /proc/asound/<name> use. Volume bridge polls this; state
    # publisher reads /proc/asound/<this>/ to detect host-connected.
    mixer_card: str = "UAC2Gadget"
    sample_rate: int = 48000
    channels: int = 2
    log_level: str = "INFO"
    state_path: str = STATE_DEFAULT_PATH
    preempt_port: int = PREEMPT_DEFAULT_PORT
    preempt_state_path: str = PREEMPT_DEFAULT_STATE_PATH
    control_url: str = VOLUME_DEFAULT_CONTROL_URL

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        return cls(
            capture_device=os.environ.get(
                "JASPER_USBSINK_CAPTURE_DEVICE", "UAC2_Gadget",
            ),
            playback_device=os.environ.get(
                "JASPER_USBSINK_PLAYBACK_DEVICE", "usbsink_substream",
            ),
            mixer_card=os.environ.get(
                "JASPER_USBSINK_MIXER_CARD", "UAC2Gadget",
            ),
            sample_rate=int(os.environ.get(
                "JASPER_USBSINK_SAMPLE_RATE", "48000",
            )),
            channels=int(os.environ.get(
                "JASPER_USBSINK_CHANNELS", "2",
            )),
            log_level=os.environ.get(
                "JASPER_USBSINK_LOG_LEVEL", "INFO",
            ),
            state_path=os.environ.get(
                "JASPER_USBSINK_STATE_PATH", STATE_DEFAULT_PATH,
            ),
            preempt_port=int(os.environ.get(
                "JASPER_USBSINK_PREEMPT_PORT", str(PREEMPT_DEFAULT_PORT),
            )),
            preempt_state_path=os.environ.get(
                "JASPER_USBSINK_PREEMPT_STATE_PATH",
                PREEMPT_DEFAULT_STATE_PATH,
            ),
            control_url=os.environ.get(
                "JASPER_USBSINK_CONTROL_URL", VOLUME_DEFAULT_CONTROL_URL,
            ),
        )


class UsbSinkDaemon:
    """Runs the audio bridge under systemd, with watchdog patting tied
    to forward progress in the playback/output callback.

    Phase 2 scope: bridge lifecycle + watchdog + periodic diagnostic
    log. Later phases extend `run()` with the state publisher, preempt
    listener, and volume bridge — all of which observe the bridge's
    public surface (`last_rms_dbfs`, `set_preempted`, stats) without
    coupling tightly to its internals.
    """

    def __init__(self, config: DaemonConfig) -> None:
        self._config = config
        self._bridge = AudioBridge(
            capture_device=config.capture_device,
            playback_device=config.playback_device,
            sample_rate=config.sample_rate,
            channels=config.channels,
        )
        # Preempt listener restores prior preempt state in start() —
        # before the bridge has actually opened streams, so an
        # interrupted preempt-then-restart sequence comes back silent
        # rather than briefly leaking audio.
        self._preempt_listener = PreemptListener(
            self._bridge,
            port=config.preempt_port,
            state_path=config.preempt_state_path,
        )
        self._state_publisher = StatePublisher(
            self._bridge,
            state_path=config.state_path,
            host_card_path=f"/proc/asound/{config.mixer_card}",
        )
        self._volume_bridge = VolumeBridge(
            card_name=config.mixer_card,
            control_url=config.control_url,
        )
        self._stop = asyncio.Event()
        now = time.monotonic()
        self._last_capture_callbacks_seen = 0
        self._last_playback_callbacks_seen = 0
        self._last_capture_progress_mono = now
        self._last_playback_progress_mono = now
        self._capture_idle_logged = False
        self._playback_stale_logged = False

    @classmethod
    def from_env(cls) -> "UsbSinkDaemon":
        return cls(DaemonConfig.from_env())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """Signal-handler entry point; safe to call from any thread.
        The `call_soon_threadsafe` indirection lets a SIGTERM handler
        installed on the main thread flip an event that the asyncio
        loop is awaiting."""
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(self._stop.set)

    async def run(self) -> int:
        """Main entry point. Returns the daemon's exit code."""
        self._setup_logging()
        logger.info(
            "event=usbsink.daemon_starting capture=%s playback=%s rate=%d",
            self._config.capture_device, self._config.playback_device,
            self._config.sample_rate,
        )

        # Lazy import — `Heartbeat` pulls in sdnotify which is fine
        # but keeping the daemon import tree small at module-load
        # speeds up the unit test boot and keeps the venv reach
        # minimal on dev laptops.
        from ..watchdog import Heartbeat

        heartbeat = Heartbeat(
            stale_threshold_sec=WATCHDOG_STALE_SEC,
            interval_sec=2.0,
        )

        # Startup uses a started-subsystems list so any partial-startup
        # failure unwinds cleanly. Each subsystem registers its stop()
        # in reverse-order on success; on exception we undo whatever
        # got far enough to start. Without this, a heartbeat.start()
        # exception (sdnotify import failure on a non-Pi host, e.g.)
        # would leak the bridge and the preempt listener's port 8781
        # binding — caught manually in field debugging once already.
        cleanups: list[tuple[str, callable]] = []
        try:
            # Preempt listener first so any persisted "silenced" state
            # is applied to the bridge before its first audio block.
            self._preempt_listener.start()
            cleanups.append(("preempt_listener", self._preempt_listener.stop))

            self._bridge.start()
            cleanups.append(("bridge", self._bridge.stop))

            heartbeat.start()
            cleanups.append(("heartbeat", heartbeat.stop))

            publish_task = asyncio.create_task(self._state_publisher.run())
            diag_task = asyncio.create_task(self._diagnostic_loop(heartbeat))
            # Volume bridge runs in its own task — disabled-friendly:
            # if the mixer controls aren't present, the bridge logs and
            # returns, the rest of the daemon keeps going. So a botched
            # gadget descriptor doesn't take down the audio bridge.
            volume_task = asyncio.create_task(self._volume_bridge.run())
            tasks = [publish_task, diag_task, volume_task]
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "event=usbsink.startup_failed error=%s stage=%s",
                e, cleanups[-1][0] if cleanups else "before_first",
            )
            for name, stop in reversed(cleanups):
                try:
                    stop()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "event=usbsink.cleanup_failed subsystem=%s", name,
                    )
            return 1

        try:
            await self._stop.wait()
        finally:
            logger.info("event=usbsink.daemon_stopping")
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # Stop in reverse order of start (same contract as the
            # startup-failure path above).
            for name, stop in reversed(cleanups):
                try:
                    stop()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "event=usbsink.cleanup_failed subsystem=%s "
                        "phase=shutdown", name,
                    )
            logger.info("event=usbsink.daemon_stopped")
        return 0

    # ------------------------------------------------------------------
    # Diagnostic loop — periodic stats snapshot + watchdog pat
    # ------------------------------------------------------------------

    async def _diagnostic_loop(self, heartbeat) -> None:
        """1 Hz loop: pats the systemd watchdog when the playback
        callback shows forward progress, logs stats every
        DIAG_INTERVAL_SEC. Cancelled at shutdown.

        The watchdog isn't bumped on a fixed timer — that would mask
        the very wedges we want to catch. We bump only when the
        playback callback has moved since the last tick. Capture
        callback stalls are logged as idle state because an optional
        USB source can legitimately have no inbound stream.
        """
        last_log_mono = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            stats: BridgeStats = self._bridge.stats
            now = time.monotonic()
            self._observe_bridge_progress(stats, heartbeat, now)

            # Surface non-zero PortAudio CallbackFlags stashed by the
            # audio thread. The callback itself can't log (would risk
            # wedging the realtime thread on a stuck handler); we
            # consume the latched value here and reset to 0 so we
            # don't re-log the same flag every diag tick. CallbackFlags
            # bits: input_underflow (1), input_overflow (2),
            # output_underflow (4), output_overflow (8), priming (16).
            if stats.last_capture_status:
                logger.warning(
                    "event=usbsink.capture_status flags=0x%x count=%d",
                    stats.last_capture_status, stats.capture_errors,
                )
                stats.last_capture_status = 0
            if stats.last_playback_status:
                logger.warning(
                    "event=usbsink.playback_status flags=0x%x count=%d",
                    stats.last_playback_status, stats.playback_errors,
                )
                stats.last_playback_status = 0

            if now - last_log_mono >= DIAG_INTERVAL_SEC:
                capture_idle_sec = now - self._last_capture_progress_mono
                playback_idle_sec = now - self._last_playback_progress_mono
                logger.info(
                    "event=usbsink.diag rms_dbfs=%.1f "
                    "captured=%d played=%d output=%d underrun=%d "
                    "dropped_full=%d capture_callbacks=%d "
                    "playback_callbacks=%d capture_idle_sec=%.1f "
                    "playback_idle_sec=%.1f capture_errs=%d "
                    "playback_errs=%d preempted=%s",
                    self._bridge.last_rms_dbfs,
                    stats.frames_captured, stats.frames_played,
                    stats.frames_output, stats.frames_underrun,
                    stats.frames_dropped_full, stats.capture_callbacks,
                    stats.playback_callbacks, capture_idle_sec,
                    playback_idle_sec, stats.capture_errors,
                    stats.playback_errors,
                    "true" if self._bridge.is_preempted else "false",
                )
                last_log_mono = now

    def _observe_bridge_progress(
        self, stats: BridgeStats, heartbeat, now: float,
    ) -> None:
        """Translate bridge counters into watchdog and idle-state signals.

        Playback callback progress is the daemon-health sentinel. Capture
        callback progress is source activity evidence only; lack of it is a
        normal idle state for a USB gadget source.
        """
        if stats.playback_callbacks > self._last_playback_callbacks_seen:
            if self._playback_stale_logged:
                logger.info(
                    "event=usbsink.playback_resumed stale_sec=%.1f "
                    "playback_callbacks=%d output=%d",
                    now - self._last_playback_progress_mono,
                    stats.playback_callbacks, stats.frames_output,
                )
            self._last_playback_callbacks_seen = stats.playback_callbacks
            self._last_playback_progress_mono = now
            self._playback_stale_logged = False
            heartbeat.bump()
        elif now - self._last_playback_progress_mono > WATCHDOG_STALE_SEC:
            # Don't bump; let systemd's WatchdogSec fire if the output
            # callback has truly stopped. Log only once per stale episode;
            # Heartbeat will add its standard suppression breadcrumb.
            if not self._playback_stale_logged:
                logger.warning(
                    "event=usbsink.playback_no_progress stale_sec=%.1f "
                    "playback_callbacks=%d output=%d (watchdog will fire)",
                    now - self._last_playback_progress_mono,
                    stats.playback_callbacks, stats.frames_output,
                )
                self._playback_stale_logged = True

        if stats.capture_callbacks > self._last_capture_callbacks_seen:
            if self._capture_idle_logged:
                logger.info(
                    "event=usbsink.capture_resumed idle_sec=%.1f "
                    "capture_callbacks=%d captured=%d",
                    now - self._last_capture_progress_mono,
                    stats.capture_callbacks, stats.frames_captured,
                )
            self._last_capture_callbacks_seen = stats.capture_callbacks
            self._last_capture_progress_mono = now
            self._capture_idle_logged = False
        elif now - self._last_capture_progress_mono > CAPTURE_IDLE_LOG_SEC:
            if not self._capture_idle_logged:
                logger.info(
                    "event=usbsink.capture_idle idle_sec=%.1f "
                    "capture_callbacks=%d captured=%d host_card_present=%s",
                    now - self._last_capture_progress_mono,
                    stats.capture_callbacks, stats.frames_captured,
                    "true" if self._host_card_present() else "false",
                )
                self._capture_idle_logged = True

    def _host_card_present(self) -> bool:
        return os.path.isdir(f"/proc/asound/{self._config.mixer_card}")

    def _setup_logging(self) -> None:
        # Avoid reconfiguring if logging is already set up (e.g. in
        # tests that import the daemon).
        if logging.getLogger().handlers:
            return
        configured_level = getattr(
            logging, self._config.log_level.upper(), logging.INFO,
        )
        logging.basicConfig(
            level=configured_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        # Standard JTS observability path: keep the live journal at INFO,
        # buffer DEBUG in RAM, and dump context on WARNING+/SIGUSR1. If an
        # operator explicitly set the legacy usbsink log level to DEBUG,
        # preserve that live-journal verbosity after installing the ring.
        from .. import debug_mode, flight_recorder

        flight_recorder.install("usbsink")
        if configured_level <= logging.DEBUG:
            debug_mode.set_console_debug(True)
