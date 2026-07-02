# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Entry point for the non-real-time USB host-volume helper."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from jasper.usbsink.volume_bridge import (
    DEFAULT_CONTROL_URL,
    VolumeBridge,
)

logger = logging.getLogger("jasper.usbsink.volume")


async def _run() -> int:
    bridge = VolumeBridge(
        card_name=os.environ.get("JASPER_USBSINK_MIXER_CARD", "UAC2Gadget"),
        control_url=os.environ.get(
            "JASPER_USBSINK_CONTROL_URL",
            DEFAULT_CONTROL_URL,
        ),
    )
    task = asyncio.create_task(bridge.run())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    stop_task = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait(
        {task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for pending_task in pending:
        pending_task.cancel()
    if task in done:
        await task
        return 0
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=os.environ.get("JASPER_USBSINK_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
