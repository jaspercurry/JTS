# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for UsbSinkDaemon's startup + cleanup lifecycle.

The interesting property here isn't audio (that's hardware-side) but
the startup-failure unwind: if any subsystem fails to start, every
subsystem that DID start must be cleanly stopped so we don't leak the
preempt HTTP port, the sounddevice streams, or the systemd watchdog
notifier.

Pre-PR2 the unwind only covered the bridge.start() failure. PR2
restructured run() around a cleanups list so heartbeat / task-creation
failures unwind correctly too.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from jasper.usbsink.audio_bridge import BridgeStats, BLOCK_FRAMES, QUEUE_MAXBLOCKS
from jasper.usbsink.daemon import (
    WATCHDOG_STALE_SEC,
    UsbSinkDaemon,
    DaemonConfig,
    _parse_latency,
)


class _FakeHeartbeat:
    def __init__(self):
        self.bumps = 0

    def bump(self):
        self.bumps += 1


def _make_daemon_with_stubs(*, bridge_fail=False, heartbeat_fail=False):
    """Build a UsbSinkDaemon with all collaborators replaced by mocks.

    `bridge_fail`: AudioBridge.start() raises RuntimeError
    `heartbeat_fail`: Heartbeat.start() raises RuntimeError

    Returns (daemon, mocks_dict) so tests can assert on stop() calls.
    """
    config = DaemonConfig()
    daemon = UsbSinkDaemon(config)

    bridge = MagicMock()
    listener = MagicMock()
    publisher = MagicMock()
    volume = MagicMock()
    heartbeat = MagicMock()

    if bridge_fail:
        bridge.start.side_effect = RuntimeError("bridge boom")
    if heartbeat_fail:
        heartbeat.start.side_effect = RuntimeError("heartbeat boom")

    daemon._bridge = bridge
    daemon._preempt_listener = listener
    daemon._state_publisher = publisher
    daemon._volume_bridge = volume

    return daemon, {
        "bridge": bridge,
        "listener": listener,
        "publisher": publisher,
        "volume": volume,
        "heartbeat": heartbeat,
    }


@pytest.mark.asyncio
async def test_run_unwinds_preempt_listener_on_bridge_start_failure():
    """If bridge.start() raises, the already-started preempt listener
    must be stopped so port 8781 doesn't stay bound for the next
    daemon start attempt."""
    daemon, mocks = _make_daemon_with_stubs(bridge_fail=True)

    with patch("jasper.watchdog.Heartbeat") as HeartbeatCls:
        HeartbeatCls.return_value = mocks["heartbeat"]
        exit_code = await daemon.run()

    assert exit_code == 1
    mocks["listener"].start.assert_called_once()
    mocks["listener"].stop.assert_called_once()
    mocks["bridge"].start.assert_called_once()
    # Bridge didn't start successfully — don't try to stop it.
    mocks["bridge"].stop.assert_not_called()
    # Heartbeat never reached start() because bridge failed first.
    mocks["heartbeat"].start.assert_not_called()


@pytest.mark.asyncio
async def test_run_unwinds_bridge_and_listener_on_heartbeat_failure():
    """If heartbeat.start() raises (e.g. sdnotify missing in an
    unexpected env), bridge and listener must both be stopped."""
    daemon, mocks = _make_daemon_with_stubs(heartbeat_fail=True)

    with patch("jasper.watchdog.Heartbeat") as HeartbeatCls:
        HeartbeatCls.return_value = mocks["heartbeat"]
        exit_code = await daemon.run()

    assert exit_code == 1
    # All three started (listener, bridge, heartbeat-attempted), and
    # the two that succeeded got stopped in reverse order.
    mocks["listener"].start.assert_called_once()
    mocks["bridge"].start.assert_called_once()
    mocks["heartbeat"].start.assert_called_once()
    mocks["bridge"].stop.assert_called_once()
    mocks["listener"].stop.assert_called_once()
    # Heartbeat never finished start(), don't stop it.
    mocks["heartbeat"].stop.assert_not_called()


@pytest.mark.asyncio
async def test_run_cleanup_continues_when_one_stop_raises():
    """If a stop() raises during unwind, the remaining stops still
    run. Catching exceptions per-subsystem prevents one bad actor
    from leaving siblings leaked."""
    daemon, mocks = _make_daemon_with_stubs(heartbeat_fail=True)
    # bridge.stop also raises — cleanup must still call listener.stop.
    mocks["bridge"].stop.side_effect = OSError("bridge stop boom")

    with patch("jasper.watchdog.Heartbeat") as HeartbeatCls:
        HeartbeatCls.return_value = mocks["heartbeat"]
        exit_code = await daemon.run()

    assert exit_code == 1
    mocks["bridge"].stop.assert_called_once()
    mocks["listener"].stop.assert_called_once()


# ----------------------------------------------------------------------
# DaemonConfig: capture_device and mixer_card MUST be distinct.
# The same ALSA card is referenced by two different names depending
# on which tool is talking to it (PortAudio wants the long name with
# underscore, amixer wants the short name without). If they ever
# collapse to one value, one of the two tools breaks silently —
# this happened in production on 2026-05-23 when the daemon passed
# its capture_device to VolumeBridge as the amixer card name.
# ----------------------------------------------------------------------


def test_daemon_config_default_capture_device_is_portaudio_long_name():
    """sounddevice/PortAudio substring-matches against
    `sd.query_devices()` which shows "UAC2_Gadget: PCM (hw:N,0)" — the
    underscore form. The bare short name "UAC2Gadget" (no underscore)
    fails the match."""
    cfg = DaemonConfig()
    assert cfg.capture_device == "UAC2_Gadget"
    assert cfg.capture_device != "UAC2Gadget"


def test_daemon_config_default_mixer_card_is_alsa_short_name():
    """amixer -c <name> and /proc/asound/<name>/ both use the kernel
    "short" name — set by the ConfigFS descriptor in
    deploy/usbsink/jasper-usbsink-gadget-up, no underscore."""
    cfg = DaemonConfig()
    assert cfg.mixer_card == "UAC2Gadget"
    assert cfg.mixer_card != "UAC2_Gadget"


def test_daemon_config_capture_and_mixer_are_different():
    """Belt-and-suspenders pin against a future refactor that
    consolidates them by mistake. They reference the same card but
    by different names; collapsing them breaks one tool or the
    other depending on which name wins."""
    cfg = DaemonConfig()
    assert cfg.capture_device != cfg.mixer_card


def test_daemon_config_from_env_supports_independent_overrides(monkeypatch):
    """Each setting is independently overridable. Used by operators
    setting a custom gadget descriptor name."""
    monkeypatch.setenv("JASPER_USBSINK_CAPTURE_DEVICE", "my-pa-name")
    monkeypatch.setenv("JASPER_USBSINK_MIXER_CARD", "my-short")
    cfg = DaemonConfig.from_env()
    assert cfg.capture_device == "my-pa-name"
    assert cfg.mixer_card == "my-short"


def test_daemon_config_latency_knobs_default_to_bridge_constants(monkeypatch):
    """Unset env preserves the historical behavior byte-for-byte."""
    for k in (
        "JASPER_USBSINK_QUEUE_MAXBLOCKS",
        "JASPER_USBSINK_BLOCK_FRAMES",
        "JASPER_USBSINK_LATENCY",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = DaemonConfig.from_env()
    assert cfg.queue_maxblocks == QUEUE_MAXBLOCKS
    assert cfg.block_frames == BLOCK_FRAMES
    assert cfg.latency == ""  # -> _parse_latency -> None (PortAudio default)


def test_daemon_config_latency_knobs_overridable(monkeypatch):
    monkeypatch.setenv("JASPER_USBSINK_QUEUE_MAXBLOCKS", "3")
    monkeypatch.setenv("JASPER_USBSINK_BLOCK_FRAMES", "240")
    monkeypatch.setenv("JASPER_USBSINK_LATENCY", "low")
    cfg = DaemonConfig.from_env()
    assert cfg.queue_maxblocks == 3
    assert cfg.block_frames == 240
    assert cfg.latency == "low"


def test_daemon_config_latency_knobs_clamp_to_floor(monkeypatch):
    """queue_maxblocks=0 -> unbounded queue.Queue (OOM risk); floor at 1."""
    monkeypatch.setenv("JASPER_USBSINK_QUEUE_MAXBLOCKS", "0")
    monkeypatch.setenv("JASPER_USBSINK_BLOCK_FRAMES", "0")
    cfg = DaemonConfig.from_env()
    assert cfg.queue_maxblocks == 1
    assert cfg.block_frames == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", None),
        ("   ", None),
        ("low", "low"),
        ("HIGH", "high"),
        ("0.02", 0.02),
        ("garbage", None),  # fail-soft: a typo must never crash the daemon
    ],
)
def test_parse_latency(raw, expected):
    assert _parse_latency(raw) == expected


def test_daemon_wires_mixer_card_to_volume_bridge_and_state_publisher():
    """Regression test for the production bug on 2026-05-23: the
    daemon was passing capture_device to VolumeBridge as the card
    name for amixer, which broke when capture_device flipped to the
    PortAudio long form (`UAC2_Gadget` with underscore). amixer
    rejected it with `Invalid card number 'UAC2_Gadget'`.

    Both volume_bridge AND state_publisher's host-card check must
    use mixer_card (the ALSA short name), never capture_device."""
    cfg = DaemonConfig(
        capture_device="long-form",
        mixer_card="short-form",
    )
    daemon = UsbSinkDaemon(cfg)
    assert daemon._volume_bridge._card_name == "short-form"
    # StatePublisher checks /proc/asound/<short>/ for host-connected.
    assert str(daemon._state_publisher._host_card_path) == "/proc/asound/short-form"


def test_setup_logging_installs_usbsink_flight_recorder(monkeypatch):
    """Default daemon startup should join the standard JTS observability
    path: INFO journal, DEBUG ring, dump on WARNING/SIGUSR1."""
    root = logging.getLogger()
    jasper = logging.getLogger("jasper")
    saved = (root.handlers[:], root.level, jasper.level)
    calls: list[str] = []
    try:
        root.handlers[:] = []
        monkeypatch.setattr(
            "jasper.flight_recorder.install",
            lambda subsystem: calls.append(subsystem),
        )
        UsbSinkDaemon(DaemonConfig())._setup_logging()
        assert calls == ["usbsink"]
    finally:
        root.handlers[:], root.level = saved[0], saved[1]
        jasper.setLevel(saved[2])


# ----------------------------------------------------------------------
# Watchdog policy: output progress is daemon health; capture progress is
# source activity. A USB host can be idle forever without causing restart.
# ----------------------------------------------------------------------


def test_watchdog_bumps_on_playback_progress_even_when_capture_idle(caplog):
    daemon = UsbSinkDaemon(DaemonConfig())
    hb = _FakeHeartbeat()
    now = 100.0
    daemon._last_capture_progress_mono = now - WATCHDOG_STALE_SEC - 1.0
    daemon._last_playback_progress_mono = now - 1.0
    stats = BridgeStats(
        playback_callbacks=1,
        frames_output=BLOCK_FRAMES,
    )

    with caplog.at_level(logging.INFO):
        daemon._observe_bridge_progress(stats, hb, now)

    assert hb.bumps == 1
    assert daemon._last_playback_callbacks_seen == 1
    assert daemon._capture_idle_logged is True
    messages = [r.getMessage() for r in caplog.records]
    assert any("event=usbsink.capture_idle" in m for m in messages)
    assert not any("event=usbsink.playback_no_progress" in m for m in messages)


def test_watchdog_suppresses_when_playback_callback_stalls(caplog):
    daemon = UsbSinkDaemon(DaemonConfig())
    hb = _FakeHeartbeat()
    now = 100.0
    daemon._last_playback_progress_mono = now - WATCHDOG_STALE_SEC - 0.1
    stats = BridgeStats()

    with caplog.at_level(logging.WARNING):
        daemon._observe_bridge_progress(stats, hb, now)

    assert hb.bumps == 0
    assert daemon._playback_stale_logged is True
    messages = [r.getMessage() for r in caplog.records]
    assert any("event=usbsink.playback_no_progress" in m for m in messages)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        daemon._observe_bridge_progress(stats, hb, now + 1.0)

    assert hb.bumps == 0
    assert not any(
        "event=usbsink.playback_no_progress" in r.getMessage()
        for r in caplog.records
    )


def test_fifo_mode_watchdog_bumps_on_no_reader_liveness_tick():
    """In fifo mode the watchdog sentinel is fifo_writes + fifo_waiting_reader.
    The bounded no-reader window at lean-enter (FIFO armed before CamillaDSP
    opens the read end) advances ONLY fifo_waiting_reader; the watchdog must
    still bump so the unit does not crash-loop while waiting for the reader."""
    daemon = UsbSinkDaemon(DaemonConfig(output_mode="fifo"))
    hb = _FakeHeartbeat()
    now = 100.0
    daemon._last_capture_progress_mono = now - WATCHDOG_STALE_SEC - 1.0
    daemon._last_playback_progress_mono = now - 1.0
    # No writes yet — only the no-reader liveness tick advanced.
    stats = BridgeStats(fifo_writes=0, fifo_waiting_reader=3)
    daemon._observe_bridge_progress(stats, hb, now)
    assert hb.bumps == 1
    assert daemon._last_playback_callbacks_seen == 3


def test_fifo_mode_watchdog_stale_when_neither_counter_moves():
    """If BOTH fifo_writes and fifo_waiting_reader are stalled (the writer
    thread is genuinely dead — not merely waiting for a reader), the watchdog
    must NOT bump so systemd can recover the unit."""
    daemon = UsbSinkDaemon(DaemonConfig(output_mode="fifo"))
    hb = _FakeHeartbeat()
    now = 100.0
    daemon._last_playback_progress_mono = now - WATCHDOG_STALE_SEC - 0.1
    stats = BridgeStats(fifo_writes=0, fifo_waiting_reader=0)
    daemon._observe_bridge_progress(stats, hb, now)
    assert hb.bumps == 0
    assert daemon._playback_stale_logged is True


def test_capture_resume_logs_once_after_idle(caplog):
    daemon = UsbSinkDaemon(DaemonConfig())
    hb = _FakeHeartbeat()
    now = 100.0
    daemon._last_capture_progress_mono = now - WATCHDOG_STALE_SEC - 1.0
    daemon._last_playback_progress_mono = now - 1.0

    with caplog.at_level(logging.INFO):
        daemon._observe_bridge_progress(
            BridgeStats(playback_callbacks=1, frames_output=BLOCK_FRAMES),
            hb,
            now,
        )
        caplog.clear()
        daemon._observe_bridge_progress(
            BridgeStats(
                capture_callbacks=1,
                playback_callbacks=2,
                frames_captured=BLOCK_FRAMES,
                frames_output=2 * BLOCK_FRAMES,
            ),
            hb,
            now + 1.0,
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("event=usbsink.capture_resumed" in m for m in messages)
    assert daemon._capture_idle_logged is False
    assert hb.bumps == 2


# ----------------------------------------------------------------------
# Watchdog sentinel selection by output_mode. In the Stage-4b lean lane
# (output_mode="fifo") there is no PortAudio playback callback, so the
# fifo writer thread's fifo_writes counter is the liveness sentinel; in
# aloop mode the sentinel is playback_callbacks and fifo_writes is moot.
# ----------------------------------------------------------------------


def test_watchdog_fifo_mode_bumps_on_fifo_writes_not_playback_callbacks():
    """fifo mode: advancing fifo_writes pats the heartbeat; advancing
    playback_callbacks alone (which never happens in fifo mode, but
    guard it anyway) does NOT."""
    daemon = UsbSinkDaemon(DaemonConfig(output_mode="fifo"))
    hb = _FakeHeartbeat()
    now = 100.0
    daemon._last_capture_progress_mono = now - 1.0
    daemon._last_playback_progress_mono = now - 1.0

    # fifo_writes advancing → heartbeat patted.
    daemon._observe_bridge_progress(
        BridgeStats(fifo_writes=1, frames_output=BLOCK_FRAMES),
        hb,
        now,
    )
    assert hb.bumps == 1
    assert daemon._last_playback_callbacks_seen == 1

    # playback_callbacks advancing while fifo_writes holds steady → NO
    # bump (fifo_writes is the sentinel in fifo mode).
    daemon._observe_bridge_progress(
        BridgeStats(fifo_writes=1, playback_callbacks=5),
        hb,
        now + 1.0,
    )
    assert hb.bumps == 1


def test_watchdog_aloop_mode_ignores_fifo_writes():
    """aloop mode (default): playback_callbacks is the sentinel;
    advancing fifo_writes alone does NOT pat the heartbeat, and
    advancing playback_callbacks does."""
    daemon = UsbSinkDaemon(DaemonConfig(output_mode="aloop"))
    hb = _FakeHeartbeat()
    now = 100.0
    daemon._last_capture_progress_mono = now - 1.0
    daemon._last_playback_progress_mono = now - 1.0

    # fifo_writes advancing while playback_callbacks holds → NO bump.
    daemon._observe_bridge_progress(
        BridgeStats(fifo_writes=7),
        hb,
        now,
    )
    assert hb.bumps == 0
    assert daemon._last_playback_callbacks_seen == 0

    # playback_callbacks advancing → heartbeat patted.
    daemon._observe_bridge_progress(
        BridgeStats(playback_callbacks=1, frames_output=BLOCK_FRAMES),
        hb,
        now + 1.0,
    )
    assert hb.bumps == 1
    assert daemon._last_playback_callbacks_seen == 1
