# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI lifecycle for socket-activated wizards."""
from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Mapping, Sequence
from http.server import ThreadingHTTPServer
from typing import Any

from ..logging_setup import configure_logging
from ..platform import systemd

logger = logging.getLogger(__name__)


def run_wizard_cli(
    prog: str,
    description: str,
    default_port: int,
    argv: Sequence[str] | None = None,
    *,
    make_server: Callable[..., ThreadingHTTPServer],
    extra: Callable[[argparse.ArgumentParser], Any] | None = None,
    start: Callable[
        [argparse.Namespace, systemd.IdleShutdownTracker], Mapping[str, Any]
    ] | None = None,
    detail: Callable[[argparse.Namespace], str] | None = None,
    configure: Callable[[], None] = configure_logging,
    idle_threshold_sec: float = systemd.DEFAULT_IDLE_SHUTDOWN_SEC,
    on_idle_exit: Callable[[], None] | None = None,
) -> int:
    """Run one wizard and release its listener and timer on exit.

    ``start`` precedes socket adoption so correction's claim boundary can
    refuse to serve. Callers must keep ``detail`` safe for the journal.
    ``configure`` remains overridable for the correction_setup exception in
    tests/test_logging_setup.py's _ALLOWLIST until that set is empty.
    """

    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    if extra is not None:
        extra(parser)
    args = parser.parse_args(argv)
    configure()

    # Built before the server so ``start`` can hand ``tracker.hold`` to the
    # handler: background work a request never awaits has to take the busy
    # counter, or the process idle-exits out from under it (issue #1854).
    tracker = systemd.IdleShutdownTracker(
        idle_threshold_sec=idle_threshold_sec, on_idle_exit=on_idle_exit,
    )
    kwargs = start(args, tracker) if start is not None else {}

    sockets = systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target, **kwargs)
    try:
        systemd.install_request_idle_bump(server.RequestHandlerClass, tracker)
        tracker.start()
        note = f" ({detail(args)})" if detail is not None else ""
        if sockets:
            logger.info("%s adopting systemd fd%s", prog, note)
        else:
            logger.info(
                "%s listening on http://%s:%d%s", prog, args.host, args.port, note,
            )
        systemd.notify_ready()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        tracker.stop()
        systemd.notify_stopping()
        server.server_close()
    return 0
