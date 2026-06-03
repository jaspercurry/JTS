"""CLI entrypoint for the JTS Bluetooth no-code pairing agent."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from dbus_next import BusType  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore

from .adapter import set_discoverable
from .agent import NoCodeAgent, register_agent, unregister_agent

logger = logging.getLogger(__name__)


async def _run() -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    stop = asyncio.Event()
    agent = NoCodeAgent(bus, on_release=stop.set)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: stop.set())

    await register_agent(bus, agent)
    try:
        try:
            await set_discoverable(False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "event=bluetooth_agent.close_pairing_window_failed err=%r",
                exc,
            )
        logger.info("event=bluetooth_agent.ready capability=NoInputNoOutput")
        await stop.wait()
    finally:
        logger.info("event=bluetooth_agent.stopping")
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
