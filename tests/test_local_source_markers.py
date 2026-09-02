# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest
from pathlib import Path
from types import SimpleNamespace

from jasper.local_sources import local_source_lifecycles
from jasper.local_sources import markers
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


def test_marker_denies_valid_bonded_follower(monkeypatch):
    monkeypatch.setattr(markers, "load_config", lambda: _cfg())

    allowed, reason = markers.local_sources_allowed()

    assert allowed is False
    assert reason == LOCAL_SOURCES_PARK_REASON_BONDED_FOLLOWER


def test_marker_allows_leader_and_solo(monkeypatch):
    monkeypatch.setattr(markers, "load_config", lambda: _cfg(role="leader"))

    assert markers.local_sources_allowed() == (True, None)

    monkeypatch.setattr(markers, "load_config", lambda: _cfg(enabled=False))
    assert markers.local_sources_allowed() == (True, None)


def test_marker_keeps_dumb_follower_sources_parked_during_solo_transition(
    monkeypatch,
):
    prior_follower = _cfg()
    requested_solo = _cfg(enabled=False)
    prior_status = {
        "active_follower": False,
        "requested_fingerprint": grouping_request_fingerprint(prior_follower),
        "local_sources_allowed": False,
    }
    monkeypatch.setattr(markers, "load_config", lambda: requested_solo)
    monkeypatch.setattr(
        markers,
        "effective_local_sources_park_reason",
        lambda cfg: effective_local_sources_park_reason(cfg, status=prior_status),
    )

    assert markers.local_sources_allowed() == (
        False,
        "role_transition_in_progress",
    )


def test_marker_fails_open_on_unexpected_config_read_error(monkeypatch):
    def boom():
        raise OSError("state unavailable")

    monkeypatch.setattr(markers, "load_config", boom)
    monkeypatch.setattr(markers, "read_effective_role_status", lambda: {})

    assert markers.local_sources_allowed() == (True, None)


def test_marker_preserves_prior_deny_on_unexpected_config_read_error(monkeypatch):
    def boom():
        raise OSError("state unavailable")

    monkeypatch.setattr(markers, "load_config", boom)
    monkeypatch.setattr(
        markers,
        "read_effective_role_status",
        lambda: {
            "local_sources_allowed": False,
            "blocked_reason": "role_transition_in_progress",
        },
    )

    assert markers.local_sources_allowed() == (
        False,
        "role_transition_in_progress",
    )


def test_marker_uses_transition_reason_when_prior_deny_has_no_reason(monkeypatch):
    def boom():
        raise OSError("state unavailable")

    monkeypatch.setattr(markers, "load_config", boom)
    monkeypatch.setattr(
        markers,
        "read_effective_role_status",
        lambda: {"local_sources_allowed": False},
    )

    assert markers.local_sources_allowed() == (
        False,
        "role_transition_in_progress",
    )


@pytest.mark.parametrize(
    "source",
    tuple(lifecycle.source for lifecycle in local_source_lifecycles()),
)
def test_source_marker_allows_only_current_canonical_on(monkeypatch, source):
    """A stale enabled unit cannot make household Off true at start time."""

    monkeypatch.setattr(markers, "load_config", lambda: _cfg(role="leader"))
    intents = {source: True}
    monkeypatch.setattr(
        markers,
        "source_intent_enabled",
        lambda candidate: intents[candidate],
    )
    monkeypatch.setattr(
        markers,
        "current_usb_data_role",
        lambda: SimpleNamespace(gadget_available=True, reason="available"),
    )

    assert markers.local_source_allowed(source) == (True, None)

    intents[source] = False
    assert markers.local_source_allowed(source) == (
        False,
        "source_intent_disabled",
    )


def test_source_marker_fails_closed_on_malformed_intent(monkeypatch):
    monkeypatch.setattr(markers, "load_config", lambda: _cfg(role="leader"))

    def invalid(_source):
        raise RuntimeError("invalid source intent value")

    monkeypatch.setattr(markers, "source_intent_enabled", invalid)

    assert markers.local_source_allowed(Source.SPOTIFY) == (
        False,
        "source_intent_invalid",
    )


def test_source_marker_checks_role_before_intent(monkeypatch):
    """Follower parking stays authoritative without touching intent I/O."""

    monkeypatch.setattr(markers, "load_config", lambda: _cfg())

    def must_not_read(_source):
        raise AssertionError("intent must not be read after role denied start")

    monkeypatch.setattr(markers, "source_intent_enabled", must_not_read)

    allowed, reason = markers.local_source_allowed(Source.SPOTIFY)
    assert allowed is False
    assert reason == LOCAL_SOURCES_PARK_REASON_BONDED_FOLLOWER


def test_usb_source_marker_denies_output_owned_shared_port(monkeypatch):
    monkeypatch.setattr(markers, "load_config", lambda: _cfg(role="leader"))
    monkeypatch.setattr(markers, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(
        markers,
        "current_usb_data_role",
        lambda: SimpleNamespace(
            gadget_available=False,
            reason="shared_otg_usb_output_requires_host",
        ),
    )

    assert markers.local_source_allowed(Source.USBSINK) == (
        False,
        "shared_otg_usb_output_requires_host",
    )


def test_publish_writes_one_marker_per_label(monkeypatch, tmp_path):
    """Marker presence mirrors the verdict for every label, including shared."""

    monkeypatch.setattr(markers, "MARKER_DIR", str(tmp_path / "allowed"))
    monkeypatch.setattr(markers, "load_config", lambda: _cfg(role="leader"))
    monkeypatch.setattr(
        markers,
        "current_usb_data_role",
        lambda: SimpleNamespace(gadget_available=True, reason="available"),
    )
    enabled = {lifecycle.source: True for lifecycle in local_source_lifecycles()}
    monkeypatch.setattr(markers, "source_intent_enabled", lambda s: enabled[s])

    verdicts = markers.publish_allowed_markers()

    assert set(verdicts) == {markers.SHARED_LABEL} | {
        lifecycle.source.value for lifecycle in local_source_lifecycles()
    }
    for label in verdicts:
        assert Path(markers.marker_path(label)).exists()

    for source in enabled:
        enabled[source] = False
    markers.publish_allowed_markers()

    assert Path(markers.marker_path(markers.SHARED_LABEL)).exists()
    for lifecycle in local_source_lifecycles():
        assert not Path(markers.marker_path(lifecycle.source.value)).exists()

