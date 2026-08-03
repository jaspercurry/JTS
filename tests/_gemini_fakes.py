# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small getattr-compatible stand-ins for Gemini SDK responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ServerContent:
    turn_complete: bool = False
    interrupted: bool = False


@dataclass
class Usage:
    prompt_token_count: int = 0
    response_token_count: int = 0


@dataclass
class ResumptionUpdate:
    new_handle: str | None = None


@dataclass
class GoAway:
    time_left: Any = None


@dataclass
class Response:
    data: bytes | None = None
    tool_call: Any = None
    server_content: ServerContent | None = None
    usage_metadata: Any = None
    session_resumption_update: ResumptionUpdate | None = None
    go_away: GoAway | None = None
