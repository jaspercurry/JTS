"""Lock down bt-agent.service deploy-stop behavior."""
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


def test_bt_agent_uses_interrupt_for_cli_style_shutdown() -> None:
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "KillSignal") == "SIGINT"


def test_bt_agent_has_short_stop_timeout() -> None:
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "TimeoutStopSec") == "5s"
