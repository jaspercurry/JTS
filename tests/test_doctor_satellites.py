# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free contracts for satellite doctor checks."""

from __future__ import annotations

import pytest

from jasper.cli.doctor import satellites
from jasper.control import client as control


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            control.ControlError("connection refused"),
            id="control-unreachable",
        ),
        pytest.param(ValueError("malformed JSON"), id="invalid-response"),
    ],
)
def test_check_dial_heartbeat_warns_when_control_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fake_get_dial_status(*, timeout: float) -> dict[str, object]:
        assert timeout == 3
        raise error

    monkeypatch.setattr(control, "get_dial_status", fake_get_dial_status)

    result = satellites.check_dial_heartbeat()

    assert result.name == "dial activity"
    assert result.status == "warn"
    assert result.detail == (
        f"jasper-control /dial/status unreachable: {error}. "
        "`systemctl status jasper-control`."
    )


def test_check_dial_heartbeat_warns_when_dial_was_never_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_dial_status(*, timeout: float) -> dict[str, object]:
        assert timeout == 3
        return {
            "last_seen_at": None,
            "last_seen_ip": None,
            "age_seconds": None,
        }

    monkeypatch.setattr(control, "get_dial_status", fake_get_dial_status)

    result = satellites.check_dial_heartbeat()

    assert result.name == "dial activity"
    assert result.status == "warn"
    assert result.detail == (
        "no dial seen since jasper-control started. If you don't "
        "have a dial, ignore. If you do, check that it's on Wi-Fi "
        "and resolving us via mDNS-SD."
    )


@pytest.mark.parametrize(
    ("age_seconds", "rendered_age"),
    [
        pytest.param(8.9, "8", id="recent"),
        pytest.param(172_800.4, "172800", id="idle-for-two-days"),
    ],
)
def test_check_dial_heartbeat_reports_activity_regardless_of_age(
    monkeypatch: pytest.MonkeyPatch,
    age_seconds: float,
    rendered_age: str,
) -> None:
    def fake_get_dial_status(*, timeout: float) -> dict[str, object]:
        assert timeout == 3
        return {
            "last_seen_at": 1_720_000_000.0,
            "last_seen_ip": "192.0.2.12",
            "age_seconds": age_seconds,
        }

    monkeypatch.setattr(control, "get_dial_status", fake_get_dial_status)

    result = satellites.check_dial_heartbeat()

    assert result.name == "dial activity"
    assert result.status == "ok"
    assert result.detail == (
        f"last contact from 192.0.2.12 {rendered_age}s ago "
        "(activity, not heartbeat — an idle dial won't show recent age)"
    )
