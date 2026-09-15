# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins for the active-speaker commission-tone owner."""

from __future__ import annotations

import pytest

import jasper.active_speaker.web_commissioning as web_commissioning
import jasper.mux as mux
import jasper.web.correction_crossover_backend as correction_backend


def test_correction_routes_through_web_commissioning_owner():
    assert correction_backend.web_commissioning is web_commissioning


def test_commissioning_uses_its_owner_scoped_mux_gate(monkeypatch):
    commands: list[str] = []

    def command(value: str):
        commands.append(value)
        return {"active_source": "correction", "test_source": "correction"}

    monkeypatch.setattr(web_commissioning, "_commission_tone_mux_command", command)

    web_commissioning._commission_tone_select_fanin_lane()
    web_commissioning._commission_tone_release_fanin_lane(reason="test")

    assert commands == [
        "TEST_SELECT correction active-speaker-commissioning",
        "TEST_RELEASE active-speaker-commissioning",
    ]


def test_commissioning_tone_fits_inside_mux_gate_lease():
    assert web_commissioning.COMMISSION_TONE_DURATION_S < mux.FANIN_TEST_LEASE_SEC


def test_indeterminate_commission_select_runs_owner_scoped_cleanup(monkeypatch):
    commands: list[str] = []

    def command(value: str):
        commands.append(value)
        if value.startswith("TEST_SELECT"):
            raise RuntimeError("response lost")
        return {"active_source": "idle", "test_source": None}

    monkeypatch.setattr(web_commissioning, "_commission_tone_mux_command", command)

    with pytest.raises(RuntimeError, match="response lost"):
        web_commissioning._commission_tone_select_fanin_lane()

    assert commands == [
        "TEST_SELECT correction active-speaker-commissioning",
        "TEST_RELEASE active-speaker-commissioning",
    ]
