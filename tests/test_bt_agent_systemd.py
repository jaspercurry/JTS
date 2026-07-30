# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lock down bt-agent.service ownership."""
from __future__ import annotations

from pathlib import Path

from tests.systemd_unit_helpers import (
    assignments_for as _assignments_for,
    value_for as _value_for,
)


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "bt-agent.service"


def test_bt_agent_uses_jts_no_code_agent() -> None:
    unit = UNIT_PATH.read_text()
    assert _assignments_for(unit, "ExecStart") == (
        "/opt/jasper/.venv/bin/jasper-bluetooth-agent",
    )


def test_bt_agent_has_short_stop_timeout() -> None:
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "TimeoutStopSec") == "10s"


def test_bt_agent_restarts_when_bluez_releases_it() -> None:
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "Restart") == "always"
