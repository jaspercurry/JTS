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

from unittest.mock import MagicMock, patch

import pytest

from jasper.usbsink.daemon import UsbSinkDaemon, DaemonConfig


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
