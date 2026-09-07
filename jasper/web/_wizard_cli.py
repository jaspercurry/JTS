# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one CLI runner behind every socket-activated wizard's ``main()``.

Four wizards — bluetooth, chat, correction, system — are each a systemd
service whose ExecStart runs this same sequence. Everything they differ on
arrives through the parameters below (#4328).
"""
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
    """Run one wizard's whole service lifecycle; return its process exit code.

    ``start`` runs before the listener is adopted, so a raise there leaves
    ``main`` without ever serving — correction's claim boundary needs that.

    ``detail`` is a caller-written string: the caller chooses which fields are
    journal-safe.

    ``configure`` is overridable only for ``correction_setup``, a listed entry
    in ``tests/test_logging_setup.py``'s ``_ALLOWLIST`` whose stated removal
    condition (that set emptying) is not met yet.
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

    # When socket-activated by systemd, adopt the inherited listener instead
    # of binding fresh. Direct CLI invocation falls through.
    sockets = systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target, **kwargs)
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
    systemd.notify_stopping()
    return 0
