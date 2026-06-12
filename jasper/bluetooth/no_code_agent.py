"""CLI entrypoint for the JTS Bluetooth no-code pairing agent."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from dbus_next import BusType  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore

from .adapter import set_discoverable, state as adapter_state
from .agent import NoCodeAgent, register_agent, unregister_agent

logger = logging.getLogger(__name__)

PAIRABLE_FLOOR_POLL_SEC = 15.0


async def _close_pairing_window_floor(
    reason: str,
    *,
    close_pairing_window=set_discoverable,
) -> bool:
    try:
        await close_pairing_window(False)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "event=bluetooth_agent.close_pairing_window_failed reason=%s err=%r",
            reason,
            exc,
        )
        return False
    logger.info("event=bluetooth_agent.pairing_window_closed reason=%s", reason)
    return True


async def _enforce_pairable_floor_once(
    *,
    read_state=adapter_state,
    close_pairing_window=set_discoverable,
) -> bool:
    try:
        snapshot = await read_state()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "event=bluetooth_agent.pairable_floor_probe_failed err=%r",
            exc,
        )
        return False

    if snapshot.get("pairable") and not snapshot.get("discoverable"):
        return await _close_pairing_window_floor(
            "pairable_outside_window",
            close_pairing_window=close_pairing_window,
        )
    return False


async def _pairable_floor_watch(
    stop: asyncio.Event,
    *,
    interval: float = PAIRABLE_FLOOR_POLL_SEC,
    read_state=adapter_state,
    close_pairing_window=set_discoverable,
) -> None:
    while not stop.is_set():
        await _enforce_pairable_floor_once(
            read_state=read_state,
            close_pairing_window=close_pairing_window,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _run() -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
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
        await _close_pairing_window_floor("startup")
        floor_task = asyncio.create_task(_pairable_floor_watch(stop))
        logger.info("event=bluetooth_agent.ready capability=NoInputNoOutput")
        await stop.wait()
    finally:
        if floor_task is not None:
            floor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await floor_task
        logger.info("event=bluetooth_agent.stopping")
        await _close_pairing_window_floor("stopping")
        await unregister_agent(bus)
        bus.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-bluetooth-agent",
        description="JTS no-code BlueZ pairing agent",
    )
    parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
