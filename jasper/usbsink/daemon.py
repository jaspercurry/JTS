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

The watchdog heartbeat reflects forward progress in the audio bridge:
if the capture callback stops being invoked (PortAudio thread wedged,
the way jasper-aec-bridge wedged on 2026-05-11), the heartbeat stops
patting, systemd's `WatchdogSec=` fires, and `Restart=on-failure`
revives us with a fresh process. See jasper.watchdog for the
sentinel pattern.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

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

logger = logging.getLogger(__name__)


# Diagnostic-snapshot cadence. Every N seconds we log the bridge's
# stats counters so journald has a steady trickle of evidence even when
# nothing's wrong. Cheap — no work on the audio thread, just
# observation.
DIAG_INTERVAL_SEC = 30.0

# Watchdog progress-stale threshold. If the capture callback's
# `frames_captured` counter hasn't moved in this long while the
# bridge is supposed to be running, we stop patting the systemd
# watchdog and systemd restarts us. 8 s tolerates a host-side
# stall (Mac suspend, sleep wake) without triggering — UAC2 endpoint
# silence is normal during host pauses, so RMS / playing-state is
# the right "is the user using USB" signal, not watchdog timeout.
# This watchdog is purely for "the daemon itself is wedged".
WATCHDOG_STALE_SEC = 8.0


@dataclass
class DaemonConfig:
    """Environment-driven configuration, all overridable via env vars
    that begin with JASPER_USBSINK_*. Sensible defaults align with the
    UAC2 gadget descriptor in deploy/usbsink/jasper-usbsink-gadget-up.
    """
    capture_device: str = "UAC2Gadget"
    playback_device: str = "hw:CARD=Loopback,DEV=0"
    sample_rate: int = 48000
    channels: int = 2
    log_level: str = "INFO"
    state_path: str = STATE_DEFAULT_PATH
    preempt_port: int = PREEMPT_DEFAULT_PORT
    preempt_state_path: str = PREEMPT_DEFAULT_STATE_PATH

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        return cls(
            capture_device=os.environ.get(
                "JASPER_USBSINK_CAPTURE_DEVICE", "UAC2Gadget",
            ),
            playback_device=os.environ.get(
                "JASPER_USBSINK_PLAYBACK_DEVICE", "hw:CARD=Loopback,DEV=0",
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
        )


class UsbSinkDaemon:
    """Runs the audio bridge under systemd, with watchdog patting tied
    to forward progress in the capture callback.

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
        )
        self._stop = asyncio.Event()
        self._last_captured_seen = 0
        self._last_progress_mono = time.monotonic()

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

        # Bring the preempt listener up before the bridge so any
        # persisted "silenced" state is applied to the bridge before
        # its first audio block is produced.
        self._preempt_listener.start()

        try:
            self._bridge.start()
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "event=usbsink.bridge_start_failed error=%s", e,
            )
            self._preempt_listener.stop()
            return 1

        heartbeat.start()

        publish_task = asyncio.create_task(self._state_publisher.run())
        diag_task = asyncio.create_task(self._diagnostic_loop(heartbeat))
        try:
            await self._stop.wait()
        finally:
            logger.info("event=usbsink.daemon_stopping")
            for task in (diag_task, publish_task):
                task.cancel()
            for task in (diag_task, publish_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._bridge.stop()
            self._preempt_listener.stop()
            heartbeat.stop()
            logger.info("event=usbsink.daemon_stopped")
        return 0

    # ------------------------------------------------------------------
    # Diagnostic loop — periodic stats snapshot + watchdog pat
    # ------------------------------------------------------------------

    async def _diagnostic_loop(self, heartbeat) -> None:
        """1 Hz loop: pats the systemd watchdog when the capture
        callback shows forward progress, logs stats every
        DIAG_INTERVAL_SEC. Cancelled at shutdown.

        The watchdog isn't bumped on a fixed timer — that would mask
        the very wedges we want to catch. We bump only when
        `frames_captured` has moved since the last tick.
        """
        last_log_mono = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            stats: BridgeStats = self._bridge.stats
            now = time.monotonic()
            if stats.frames_captured > self._last_captured_seen:
                self._last_captured_seen = stats.frames_captured
                self._last_progress_mono = now
                heartbeat.bump()
            elif now - self._last_progress_mono > WATCHDOG_STALE_SEC:
                # Don't bump; let the systemd watchdog fire if the
                # bridge has truly wedged. `Heartbeat` won't notify
                # systemd because its `bump()` hasn't been called, so
                # the kernel-side WatchdogSec timer expires.
                logger.warning(
                    "event=usbsink.no_progress stale_sec=%.1f "
                    "frames_captured=%d (watchdog will fire)",
                    now - self._last_progress_mono, stats.frames_captured,
                )
            if now - last_log_mono >= DIAG_INTERVAL_SEC:
                logger.info(
                    "event=usbsink.diag rms_dbfs=%.1f "
                    "captured=%d played=%d underrun=%d dropped_full=%d "
                    "capture_errs=%d playback_errs=%d preempted=%s",
                    self._bridge.last_rms_dbfs,
                    stats.frames_captured, stats.frames_played,
                    stats.frames_underrun, stats.frames_dropped_full,
                    stats.capture_errors, stats.playback_errors,
                    "true" if self._bridge.is_preempted else "false",
                )
                last_log_mono = now

    def _setup_logging(self) -> None:
        # Avoid reconfiguring if logging is already set up (e.g. in
        # tests that import the daemon).
        if logging.getLogger().handlers:
            return
        logging.basicConfig(
            level=getattr(logging, self._config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
