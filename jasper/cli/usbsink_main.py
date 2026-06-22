# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Entry point for `jasper-usbsink`.

Thin wrapper: builds a UsbSinkDaemon from env, installs signal
handlers for SIGINT/SIGTERM, runs the daemon's async event loop.
Registered in pyproject.toml's [project.scripts].
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

logger = logging.getLogger("jasper.usbsink.main")


def main(argv: list[str] | None = None) -> int:
    # Import inside main so `jasper-usbsink --help` (or import-time
    # smoke tests in CI) don't crash on dev laptops where sounddevice
    # is fine but the gadget card doesn't exist.
    from jasper.usbsink.daemon import UsbSinkDaemon

    daemon = UsbSinkDaemon.from_env()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            # Windows / non-Unix — never our case but fail gracefully.
            pass

    try:
        return loop.run_until_complete(daemon.run())
    except KeyboardInterrupt:
        return 0
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
