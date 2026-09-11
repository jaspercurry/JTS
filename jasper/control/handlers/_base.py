# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared typing and logging boundary for control-server route mixins."""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Not `__name__`: one journal name for every route body, not one per mixin.
logger = logging.getLogger("jasper.control")


class ControlHandlerMixin(BaseHTTPRequestHandler):
    """Methods and factory-owned state consumed by concern route mixins.

    The concrete nested handler in ``server._make_handler`` supplies these
    attributes and most of these methods; ``_maybe_forward_pair_action_to_leader``
    is instead implemented by ``PeeringRoutes``. Keeping the contract here lets
    mypy check the extracted route bodies without changing their runtime
    dispatch shape.
    """

    _adjust_op: Any
    _audio_health_sampler: Any
    _camilla_host: str
    _camilla_port: int
    _get_op: Any
    _ha_status_cache: Any
    _install_profile: Any
    _mute_set_op: Any
    _mute_toggle_op: Any
    _observe_op: Any
    _sampler: Any
    _set_op: Any
    _state_response_cache: Any
    _voice_socket_path: str
    server: ThreadingHTTPServer

    def _collect_state(
        self,
        *,
        camilla_host: str,
        camilla_port: int,
        voice_socket_path: str,
        airplay_playing_snapshot: Any = None,
        audio_health_snapshot: Any = None,
    ) -> Any:
        """The cross-daemon /state aggregate, as an awaitable.

        A whole-callable seam (not one of the per-op attributes above):
        route-level tests replace it outright to test caching/error
        handling without running the real aggregation.
        """
        raise NotImplementedError

    def _guard_control_token(self) -> bool:
        raise NotImplementedError

    def _maybe_forward_pair_action_to_leader(self) -> bool:
        raise NotImplementedError

    def _read_json(self) -> dict[str, Any]:
        raise NotImplementedError

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
    ) -> None:
        raise NotImplementedError

    def _send_accepted(self, **extra: Any) -> None:
        """Answer 202 for work handed to systemd but not yet confirmed done."""
        self._send_json({**extra, "ok": True, "status": "accepted"}, status=202)

    def _send_refused(self, *, error: str, code: str, **extra: Any) -> None:
        """Answer 502 for work systemd would not take: what stood, then why.

        The envelope keys are written last, so an `extra` field can never
        turn a refusal into an ok.
        """
        self._send_json({**extra, "error": error, "code": code}, status=502)

    def _voice_cmd_or_error(
        self,
        cmd: str,
        *,
        timeout: float | None = None,
        missing_error: str | None = "voice_daemon not running",
        log_label: str = "voice command",
        refusal_event: str | None = None,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def _volume_payload(self, state: Any) -> dict[str, Any]:
        raise NotImplementedError
