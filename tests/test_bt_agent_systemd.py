"""Lock down bt-agent.service ownership."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "bt-agent.service"


def _value_for(unit_text: str, key: str) -> str | None:
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        name, sep, value = stripped.partition("=")
        if sep and name == key:
            return value
    return None


def test_bt_agent_uses_jts_no_code_agent() -> None:
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "ExecStart") == (
        "/opt/jasper/.venv/bin/jasper-bluetooth-agent"
    )


def test_bt_agent_has_short_stop_timeout() -> None:
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "TimeoutStopSec") == "10s"


def test_bt_agent_restarts_when_bluez_releases_it() -> None:
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "Restart") == "always"
