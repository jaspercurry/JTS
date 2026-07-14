# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.local_sources import guard, local_source_lifecycles
from jasper.music_sources import Source
from jasper.multiroom.config import (
    DEFAULT_BUFFER_MS,
    DEFAULT_CODEC,
    LOCAL_SOURCES_PARK_REASON_BONDED_FOLLOWER,
    GroupingConfig,
)
from jasper.multiroom.effective_role import (
    effective_local_sources_park_reason,
    grouping_request_fingerprint,
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
    assert guard.main([]) == 1


def test_guard_allows_leader_and_solo(monkeypatch):
    monkeypatch.setattr(guard, "load_config", lambda: _cfg(role="leader"))

    assert guard.local_sources_allowed() == (True, None)
    assert guard.main([]) == 0

    monkeypatch.setattr(guard, "load_config", lambda: _cfg(enabled=False))
    assert guard.local_sources_allowed() == (True, None)
    assert guard.main([]) == 0


def test_guard_keeps_dumb_follower_sources_parked_during_solo_transition(
    monkeypatch,
):
    prior_follower = _cfg()
    requested_solo = _cfg(enabled=False)
    prior_status = {
        "active_follower": False,
        "requested_fingerprint": grouping_request_fingerprint(prior_follower),
        "local_sources_allowed": False,
    }
    monkeypatch.setattr(guard, "load_config", lambda: requested_solo)
    monkeypatch.setattr(
        guard,
        "effective_local_sources_park_reason",
        lambda cfg: effective_local_sources_park_reason(cfg, status=prior_status),
    )

    assert guard.local_sources_allowed() == (
        False,
        "role_transition_in_progress",
    )


def test_guard_fails_open_on_unexpected_config_read_error(monkeypatch):
    def boom():
        raise OSError("state unavailable")

    monkeypatch.setattr(guard, "load_config", boom)
    monkeypatch.setattr(guard, "read_effective_role_status", lambda: {})

    assert guard.local_sources_allowed() == (True, None)
    assert guard.main([]) == 0


def test_guard_preserves_prior_deny_on_unexpected_config_read_error(monkeypatch):
    def boom():
        raise OSError("state unavailable")

    monkeypatch.setattr(guard, "load_config", boom)
    monkeypatch.setattr(
        guard,
        "read_effective_role_status",
        lambda: {
            "local_sources_allowed": False,
            "blocked_reason": "role_transition_in_progress",
        },
    )

    assert guard.local_sources_allowed() == (
        False,
        "role_transition_in_progress",
    )
    assert guard.main([]) == 1


def test_guard_uses_transition_reason_when_prior_deny_has_no_reason(monkeypatch):
    def boom():
        raise OSError("state unavailable")

    monkeypatch.setattr(guard, "load_config", boom)
    monkeypatch.setattr(
        guard,
        "read_effective_role_status",
        lambda: {"local_sources_allowed": False},
    )

    assert guard.local_sources_allowed() == (
        False,
        "role_transition_in_progress",
    )


@pytest.mark.parametrize(
    "source",
    tuple(lifecycle.source for lifecycle in local_source_lifecycles()),
)
def test_source_guard_allows_only_current_canonical_on(monkeypatch, source):
    """A stale enabled unit cannot make household Off true at start time."""

    monkeypatch.setattr(guard, "load_config", lambda: _cfg(role="leader"))
    intents = {source: True}
    monkeypatch.setattr(
        guard,
        "source_intent_enabled",
        lambda candidate: intents[candidate],
    )

    assert guard.local_source_allowed(source) == (True, None)
    assert guard.main(["--source", source.value]) == 0

    intents[source] = False
    assert guard.local_source_allowed(source) == (
        False,
        "source_intent_disabled",
    )
    assert guard.main(["--source", source.value]) == 1


def test_source_guard_fails_closed_on_malformed_intent(monkeypatch, caplog):
    monkeypatch.setattr(guard, "load_config", lambda: _cfg(role="leader"))

    def invalid(_source):
        raise RuntimeError("invalid source intent value")

    monkeypatch.setattr(guard, "source_intent_enabled", invalid)

    assert guard.local_source_allowed(Source.SPOTIFY) == (
        False,
        "source_intent_invalid",
    )
    assert guard.main(["--source", "spotify"]) == 1
    assert "event=local_sources.guard_intent_failed" in caplog.text


def test_source_guard_checks_role_before_intent(monkeypatch):
    """Follower parking stays authoritative without touching intent I/O."""

    monkeypatch.setattr(guard, "load_config", lambda: _cfg())

    def must_not_read(_source):
        raise AssertionError("intent must not be read after role denied start")

    monkeypatch.setattr(guard, "source_intent_enabled", must_not_read)

    allowed, reason = guard.local_source_allowed(Source.SPOTIFY)
    assert allowed is False
    assert reason == LOCAL_SOURCES_PARK_REASON_BONDED_FOLLOWER
