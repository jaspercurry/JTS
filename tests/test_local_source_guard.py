# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.local_sources import guard
from jasper.multiroom.config import (
    DEFAULT_BUFFER_MS,
    DEFAULT_CODEC,
    LOCAL_SOURCES_PARK_REASON_BONDED_FOLLOWER,
    GroupingConfig,
)


def _cfg(**overrides):
    values = dict(
        enabled=True,
        role="follower",
        channel="right",
        bond_id="bond-1",
        leader_addr="jts3.local",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error=None,
    )
    values.update(overrides)
    return GroupingConfig(**values)


def test_guard_denies_valid_bonded_follower(monkeypatch):
    monkeypatch.setattr(guard, "load_config", lambda: _cfg())

    allowed, reason = guard.local_sources_allowed()

    assert allowed is False
    assert reason == LOCAL_SOURCES_PARK_REASON_BONDED_FOLLOWER
    assert guard.main() == 1


def test_guard_allows_leader_and_solo(monkeypatch):
    monkeypatch.setattr(guard, "load_config", lambda: _cfg(role="leader"))

    assert guard.local_sources_allowed() == (True, None)
    assert guard.main() == 0

    monkeypatch.setattr(guard, "load_config", lambda: _cfg(enabled=False))
    assert guard.local_sources_allowed() == (True, None)
    assert guard.main() == 0


def test_guard_fails_open_on_unexpected_config_read_error(monkeypatch):
    def boom():
        raise OSError("state unavailable")

    monkeypatch.setattr(guard, "load_config", boom)

    assert guard.local_sources_allowed() == (True, None)
    assert guard.main() == 0
