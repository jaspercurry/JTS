# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

import jasper.identity as identity


def test_read_identity_room_identity_home_wins(monkeypatch):
    """When the identity home (speaker_name.runtime_room) returns a room,
    it wins over the legacy peering env and the hostname-derived default."""
    monkeypatch.setattr(identity.speaker_name, "runtime_room", lambda: "Loft")
    monkeypatch.setenv("JASPER_PEER_ROOM", "legacy-bedroom")
    monkeypatch.setattr(identity.peering_config, "default_room", lambda: "fallback")
    assert identity.read_identity().room == "Loft"


def test_read_identity_room_falls_back_to_legacy_peer_env(monkeypatch):
    monkeypatch.setattr(identity.speaker_name, "runtime_room", lambda: "")
    monkeypatch.setenv("JASPER_PEER_ROOM", "legacy-bedroom")
    monkeypatch.setattr(identity.peering_config, "default_room", lambda: "fallback")
    assert identity.read_identity().room == "legacy-bedroom"


def test_read_identity_room_falls_back_to_default_room(monkeypatch):
    monkeypatch.setattr(identity.speaker_name, "runtime_room", lambda: "")
    monkeypatch.delenv("JASPER_PEER_ROOM", raising=False)
    monkeypatch.setattr(identity.peering_config, "default_room", lambda: "kitchen")
    assert identity.read_identity().room == "kitchen"


def test_read_identity_assembles_all_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(identity.speaker_name, "runtime_name", lambda: "Living Room")
    monkeypatch.setattr(identity.speaker_name, "runtime_room", lambda: "Downstairs")
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    peer_file = tmp_path / "peer_id"
    peer_file.write_text("abc-123\n")
    monkeypatch.setattr(identity, "PEER_ID_FILE", str(peer_file))

    ident = identity.read_identity()
    assert ident.name == "Living Room"
    assert ident.room == "Downstairs"
    assert ident.hostname == "jts3.local"
    assert ident.peer_id == "abc-123"


def test_read_identity_hostname_defaults_when_unset(monkeypatch, tmp_path):
    """Neither source names a hostname -> the literal default.

    The identity file is bound to an absent tmp path so the assertion does
    not depend on /var/lib/jasper/identity.env being missing (it is present
    on a Pi, and its recorded hostname now feeds this fallback)."""
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(tmp_path / "absent.env"))
    assert identity.read_identity().hostname == "jts.local"


def test_read_identity_hostname_falls_back_to_recorded_configured_hostname(
    monkeypatch, tmp_path,
):
    """A CLI run over ssh gets no EnvironmentFile, so JASPER_HOSTNAME is unset
    and the process must read the hostname the reconciler recorded — otherwise
    every box prints "jts.local" (F1: `jasper-crossover-prescriber status` on
    jts3)."""
    identity_file = tmp_path / "identity.env"
    identity_file.write_text(
        "JASPER_IDENTITY_OS_HOSTNAME=jts3\n"
        "JASPER_IDENTITY_AVAHI_HOSTNAME=jts3-2.local\n"
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts3.local\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(identity_file))
    # The CONFIGURED name, not the OS name and not what Avahi renamed us to:
    # identity reads the *intended* side of that file, never the observed one.
    assert identity.read_identity().hostname == "jts3.local"


def test_read_identity_hostname_env_wins_over_recorded(monkeypatch, tmp_path):
    """A daemon's EnvironmentFile is fresher than the reconciler's 5-minute
    snapshot, so a set JASPER_HOSTNAME wins over the recorded one."""
    identity_file = tmp_path / "identity.env"
    identity_file.write_text(
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=stale.local\n", encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_HOSTNAME", "jts5.local")
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(identity_file))
    assert identity.read_identity().hostname == "jts5.local"


def test_read_identity_hostname_ignores_a_blank_recorded_hostname(
    monkeypatch, tmp_path,
):
    """An identity.env whose configured line is empty is not a hostname —
    fall through to the default rather than returning ""."""
    identity_file = tmp_path / "identity.env"
    identity_file.write_text(
        "JASPER_IDENTITY_OS_HOSTNAME=jts3\n"
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(identity_file))
    assert identity.read_identity().hostname == identity.DEFAULT_HOSTNAME


def test_speaker_url_uses_the_recorded_hostname_when_env_is_unset(
    monkeypatch, tmp_path,
):
    """An off-unit CLI builds handoff URLs for the box it runs on, not for
    whatever "jts.local" happens to resolve to."""
    identity_file = tmp_path / "identity.env"
    identity_file.write_text(
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts3.local\n", encoding="utf-8",
    )
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(identity_file))
    assert identity.speaker_url("/sound/") == "http://jts3.local/sound/"


def test_read_identity_peer_id_empty_on_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "PEER_ID_FILE", str(tmp_path / "nope"))
    assert identity.read_identity().peer_id == ""


def test_read_identity_never_raises_when_name_read_blows_up(monkeypatch):
    def _boom():
        raise RuntimeError("state file unreadable")

    monkeypatch.setattr(identity.speaker_name, "runtime_name", _boom)
    monkeypatch.setattr(identity.speaker_name, "runtime_room", _boom)
    monkeypatch.setattr(identity.peering_config, "default_room", _boom)
    monkeypatch.delenv("JASPER_PEER_ROOM", raising=False)
    # Total: degrades to defaults rather than propagating.
    ident = identity.read_identity()
    assert ident.name == identity.speaker_name.DEFAULT_SPEAKER_NAME
    assert ident.room == ""


def test_read_identity_total_with_all_sources_genuinely_absent(monkeypatch, tmp_path):
    """The everything-unset path, driven through the REAL speaker_name readers
    against an empty tmp state file (not mocked-to-raise): no env vars, an
    empty/missing speaker_name.env, a missing peer_id, a missing identity.env,
    and default_room yielding "". read_identity composes a sensible
    all-defaults identity and never raises — the hermetic 'fresh install,
    nothing configured' case."""
    empty_state = tmp_path / "speaker_name.env"  # never created -> read_state hits FileNotFoundError
    # Bind the real readers to the empty tmp path so we don't depend on the
    # absence of a real /var/lib/jasper/speaker_name.env (present on a Pi).
    monkeypatch.setattr(
        identity.speaker_name, "runtime_name",
        lambda: identity.speaker_name.runtime_name(environ={}, path=str(empty_state)),
    )
    monkeypatch.setattr(
        identity.speaker_name, "runtime_room",
        lambda: identity.speaker_name.runtime_room(environ={}, path=str(empty_state)),
    )
    monkeypatch.setattr(identity.peering_config, "default_room", lambda: "")
    monkeypatch.setattr(identity, "PEER_ID_FILE", str(tmp_path / "peer_id"))  # absent
    monkeypatch.delenv("JASPER_PEER_ROOM", raising=False)
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(tmp_path / "identity.env"))  # absent

    ident = identity.read_identity()
    assert ident.name == identity.speaker_name.DEFAULT_SPEAKER_NAME  # "JTS"
    assert ident.room == ""
    assert ident.hostname == identity.DEFAULT_HOSTNAME  # "jts.local"
    assert ident.peer_id == ""


def test_speaker_identity_is_frozen():
    """SpeakerIdentity is an immutable value object — the boundary contract
    other agents (control_advert, rooms, future bond code) read against."""
    ident = identity.SpeakerIdentity(
        name="JTS", room="kitchen", hostname="jts.local", peer_id="abc",
    )
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        ident.room = "bedroom"  # type: ignore[misc]
