# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared typing and logging boundary for control-server route mixins."""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler
from typing import Any

# Not `__name__`: one journal name for every route body, not one per mixin.
logger = logging.getLogger("jasper.control")


class ControlHandlerMixin(BaseHTTPRequestHandler):
    """Methods and factory-owned state consumed by concern route mixins.

    The concrete nested handler in ``server._make_handler`` supplies these
    methods and attributes. Keeping the contract here lets mypy check the
    extracted route bodies without changing their runtime dispatch shape.
    """

    _airplay_health_sampler: Any
    _adjust_op: Any
    _audio_health_sampler: Any
    _camilla_host: str
    _camilla_port: int
    _get_op: Any
    _ha_status_cache: Any
    _mute_set_op: Any
    _mute_toggle_op: Any
    _observe_op: Any
    _sampler: Any
    _set_op: Any
    _state_response_cache: Any
    _voice_socket_path: str

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
