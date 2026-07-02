# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lab-only entry point for the legacy Python USB sink bridge.

The production `jasper-usbsink.service` data plane is the Rust
`jasper-usbsink-audio` binary. This wrapper exists only for explicit
lab/fallback experiments around the old PortAudio bridge. It refuses
to run unless `JASPER_USBSINK_PYTHON_LAB_ALLOW=1` is set.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

logger = logging.getLogger("jasper.usbsink.main")
LAB_ALLOW_ENV = "JASPER_USBSINK_PYTHON_LAB_ALLOW"


def _lab_allowed() -> bool:
    return os.environ.get(LAB_ALLOW_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def main(argv: list[str] | None = None) -> int:
    if not _lab_allowed():
        print(
            "jasper-usbsink-python-lab is disabled by default; "
            f"set {LAB_ALLOW_ENV}=1 to run the legacy Python bridge. "
            "Production USB audio uses /opt/jasper/bin/jasper-usbsink-audio.",
            file=sys.stderr,
        )
        return 64

    # Import inside main so import-time smoke tests in CI don't crash on
    # dev laptops where sounddevice is fine but the gadget card doesn't exist.
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
