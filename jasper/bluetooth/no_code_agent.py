# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI entrypoint for the JTS Bluetooth no-code pairing agent."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import logging
import signal

from dbus_next import BusType  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore

from jasper.log_event import log_event

from .adapter import (
    BluezSession,
    set_discoverable,
    state as adapter_state,
    untrust_unbonded,
)
from .agent import NoCodeAgent, register_agent, unregister_agent
from ..logging_setup import configure_logging

logger = logging.getLogger(__name__)

# The floor closes on two observations at this interval, so the interval must
# outlast an outbound Pair() -- up to a 60 s timeout on a remote that needs a
# button press -- or the floor lowers the bondable flag mid-pair.
PAIRABLE_FLOOR_POLL_SEC = 60.0


async def _close_pairing_window_floor(
    reason: str,
    *,
    close_pairing_window=set_discoverable,
) -> bool:
    try:
        await close_pairing_window(False)
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger,
            "bluetooth_agent.close_pairing_window_failed",
            reason=reason,
            err=repr(exc),
            level=logging.WARNING,
        )
        return False
    log_event(
        logger,
        "bluetooth_agent.pairing_window_closed",
        reason=reason,
    )
    return True


async def _enforce_pairable_floor_once(
    *,
    read_state=adapter_state,
    close_pairing_window=set_discoverable,
) -> bool:
    try:
        snapshot = await read_state()
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger,
            "bluetooth_agent.pairable_floor_probe_failed",
            err=repr(exc),
            level=logging.WARNING,
        )
        return False

    if snapshot.get("pairable") and not snapshot.get("discoverable"):
        return await _close_pairing_window_floor(
            "pairable_outside_window",
            close_pairing_window=close_pairing_window,
        )
    return False


async def _sweep_unbonded_trust_once(
    *,
    sweep=untrust_unbonded,
) -> tuple[str, ...]:
    """Drop trust from any device whose bond is gone.

    Granting trust only to a bonded device is not enough: a pairing that was
    never bonded disappears on the next disconnect and leaves the trust
    behind, which is the stranding case. This also heals a device stranded
    before that guard existed.
    """
    try:
        dropped = await sweep()
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger,
            "bluetooth_agent.unbonded_trust_sweep_failed",
            err=repr(exc),
            level=logging.WARNING,
        )
        return ()
    for address in dropped:
        log_event(
            logger,
            "bluetooth_agent.untrusted_unbonded",
            address=address,
        )
    return dropped


async def _pairable_outside_window(*, read_state=adapter_state) -> bool:
    """One read-only observation of "pairable with no window open"."""
    try:
        snapshot = await read_state()
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger,
            "bluetooth_agent.pairable_floor_probe_failed",
            err=repr(exc),
            level=logging.WARNING,
        )
        return False
    return bool(snapshot.get("pairable") and not snapshot.get("discoverable"))


async def _pairable_floor_watch(
    stop: asyncio.Event,
    *,
    interval: float | None = None,
    session: BluezSession | None = None,
    read_state=None,
    close_pairing_window=set_discoverable,
    sweep=None,
) -> None:
    # Both probes run on the caller's connected bus and its cached proxies:
    # unbound, each iteration would open and tear down a system-bus
    # connection per probe and introspect BlueZ again, forever.
    interval = PAIRABLE_FLOOR_POLL_SEC if interval is None else interval
    read_state = read_state or functools.partial(adapter_state, session=session)
    sweep = sweep or functools.partial(untrust_unbonded, session=session)
    # Two consecutive observations before closing. An outbound pair raises the
    # bondable flag for the whole Pair() call -- up to its 60 s timeout on a
    # remote that needs a button press -- from a DIFFERENT process, and it is
    # pairable-without-discoverable the entire time. A single-observation
    # floor lowers the flag mid-pair and the bond silently does not form,
    # which is the defect this watch would otherwise cause rather than catch.
    # Costs one extra interval of exposure, and nothing else backstops it:
    # `_close_pairing_window` zeroes PairableTimeout, which BlueZ reads as no
    # timeout at all, so this watch is what lowers a Pairable that a caller
    # raised for an outbound pair and then died before restoring.
    armed = False
    while not stop.is_set():
        if await _pairable_outside_window(read_state=read_state):
            if armed:
                await _enforce_pairable_floor_once(
                    read_state=read_state,
                    close_pairing_window=close_pairing_window,
                )
                armed = False
            else:
                armed = True
        else:
            armed = False
        await _sweep_unbonded_trust_once(sweep=sweep)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _run() -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    session = BluezSession(bus)
    close_window = functools.partial(set_discoverable, session=session)
    stop = asyncio.Event()
    agent = NoCodeAgent(bus, on_release=stop.set)
    floor_task: asyncio.Task[None] | None = None

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: stop.set())

    await register_agent(bus, agent)
    try:
        await _close_pairing_window_floor(
            "startup", close_pairing_window=close_window,
        )
        floor_task = asyncio.create_task(
            _pairable_floor_watch(
                stop, session=session, close_pairing_window=close_window,
            ),
        )
        log_event(
            logger,
            "bluetooth_agent.ready",
            capability="NoInputNoOutput",
        )
        await stop.wait()
    finally:
        if floor_task is not None:
            floor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await floor_task
        log_event(logger, "bluetooth_agent.stopping")
        await _close_pairing_window_floor(
            "stopping", close_pairing_window=close_window,
        )
        await unregister_agent(bus)
        bus.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-bluetooth-agent",
        description="JTS no-code BlueZ pairing agent",
    )
    parser.parse_args(argv)
    configure_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
