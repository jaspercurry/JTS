# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one CLI runner behind every socket-activated wizard's ``main()``.

Four wizards — bluetooth, chat, correction, system — are each a systemd
service whose ExecStart runs this same sequence. Everything they differ on
arrives through the parameters below (#4328).

Deliberately not in ``_systemd.py``: that module owns generic
socket-activation primitives with importers outside ``jasper.web``, while
this is web-wizard CLI surface — argparse prog names, start-up log copy —
that belongs beside the wizards it serves.
"""
from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Mapping, Sequence
from http.server import ThreadingHTTPServer
from typing import Any

from ..logging_setup import configure_logging
from . import _systemd

logger = logging.getLogger(__name__)


def build_parser(
    prog: str, description: str, default_port: int,
) -> argparse.ArgumentParser:
    """The argument surface every wizard shares. Add per-wizard flags to it."""

    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    return parser


def run_wizard_cli(
    parser: argparse.ArgumentParser,
    argv: Sequence[str] | None = None,
    *,
    make_server: Callable[..., ThreadingHTTPServer],
    tracker: _systemd.IdleShutdownTracker | None = None,
    start: Callable[
        [argparse.Namespace, _systemd.IdleShutdownTracker], Mapping[str, Any]
    ] | None = None,
    detail: Callable[[argparse.Namespace], str] | None = None,
    configure: Callable[[], None] = configure_logging,
) -> int:
    """Run one wizard's whole service lifecycle; return its process exit code.

    ``tracker`` exists before ``make_server`` so ``start`` can hand
    ``tracker.hold`` to the handler — see ``IdleShutdownTracker.hold`` for
    why a route needs one. Pass one only for a non-default threshold or an
    on-idle-exit hook.

    ``start`` is the wizard's pre-start work: after logging is up, before the
    listener is adopted, so a raise there leaves ``main`` without ever
    serving. It returns the keyword arguments for ``make_server``.

    ``detail`` renders the parenthesised note on the start-up log line. A
    caller-written string rather than the parsed namespace, so a future flag
    cannot put a secret in the journal (non-negotiable 3).
    """

    args = parser.parse_args(argv)
    configure()

    if tracker is None:
        tracker = _systemd.IdleShutdownTracker()
    kwargs = dict(start(args, tracker)) if start is not None else {}

    # When socket-activated by systemd, adopt the inherited listener instead
    # of binding fresh. Direct CLI invocation falls through.
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target, **kwargs)
    _systemd.install_request_idle_bump(server.RequestHandlerClass, tracker)
    tracker.start()

    note = f" ({detail(args)})" if detail is not None else ""
    if sockets:
        logger.info("%s adopting systemd fd%s", parser.prog, note)
    else:
        logger.info(
            "%s listening on http://%s:%d%s",
            parser.prog, args.host, args.port, note,
        )

    _systemd.notify_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0
