# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor peering domain."""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.cli.doctor import peering

_TWO_SIBLINGS = (
    "+ eth0 IPv4 JTSpeer_alice _jasper-peer._udp local\n"
    "= eth0 IPv4 JTSpeer_alice _jasper-peer._udp local\n"
    "  hostname = [alice.local]\n"
    '  txt = ["peer_id=alice-uuid" "room=kitchen" "primary=1" "proto=1"]\n'
    "+ eth0 IPv4 JTSpeer_bob _jasper-peer._udp local\n"
    "= eth0 IPv4 JTSpeer_bob _jasper-peer._udp local\n"
    "  hostname = [bob.local]\n"
    '  txt = ["peer_id=bob-uuid" "room=bedroom" "primary=0" "proto=1"]\n'
)


@pytest.mark.parametrize(
    "body, status, reason",
    [
        # Absent file: peering is off by design, the default.
        (None, "ok", peering.REASON_PEERING_OFF),
        ("JASPER_PEERING=off\n", "ok", peering.REASON_PEERING_OFF),
        (
            "JASPER_PEERING=on\nJASPER_PEER_ROOM=kitchen\n",
            "ok",
            peering.REASON_PEERING_ON,
        ),
        # A typo (JASPER_PEERING=banana) resolves to off; silence would leave
        # the operator believing peering is on.
        ("JASPER_PEERING=banana\n", "warn", peering.REASON_PEERING_MODE_UNKNOWN),
        # Unbalanced quote: only a matching pair is stripped, so the doctor
        # reports the same value peering.config.load_config resolves.
        ("JASPER_PEERING='on\n", "warn", peering.REASON_PEERING_MODE_UNKNOWN),
    ],
    ids=["absent", "off", "on", "malformed", "unbalanced-quote"],
)
def test_check_peering_mode_verdicts(monkeypatch, tmp_path, body, status, reason):
    env = tmp_path / "peering.env"
    if body is not None:
        env.write_text(body)
    monkeypatch.setattr(
        "jasper.cli.doctor.peering.Path",
        lambda p: env if "peering.env" in p else Path(p),
    )

    r = peering.check_peering_mode()

    assert r.status == status
    assert r.reason == reason


def test_check_peering_mode_reports_unreadable_env(monkeypatch, tmp_path):
    env = tmp_path / "peering.env"
    env.mkdir()  # reading a directory as text raises OSError

    monkeypatch.setattr(
        "jasper.cli.doctor.peering.Path",
        lambda p: env if "peering.env" in p else Path(p),
    )

    r = peering.check_peering_mode()

    assert r.status == "warn"
    assert r.reason == peering.REASON_PEERING_ENV_UNREADABLE


@pytest.mark.parametrize(
    "output, local_peer_id, must_name",
    [
        ("+ eth0 IPv4 SomeOtherService _foo._tcp local\n", None, "0 sibling"),
        # Two advertised peers, one of them us — self is excluded.
        (_TWO_SIBLINGS, "alice-uuid", "1 sibling"),
    ],
    ids=["no-peers", "one-sibling"],
)
def test_check_peering_discovery_counts_siblings_excluding_self(
    monkeypatch, output, local_peer_id, must_name
):
    """The sibling count is a formatted number the reason vocabulary does not
    carry — this is the pure-formatting exception AGENTS.md allows to keep a
    `.detail` assertion."""
    monkeypatch.setattr("shutil.which", lambda p: "/usr/bin/avahi-browse")
    monkeypatch.setattr(
        "jasper.cli.doctor.peering._run",
        lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": output})(),
    )
    if local_peer_id is not None:
        monkeypatch.setattr(
            "jasper.cli.doctor.peering._local_peer_id", lambda: local_peer_id
        )

    r = peering.check_peering_discovery()

    assert r.status == "ok"
    assert must_name in r.detail


def test_check_peering_discovery_warns_without_avahi_browse(monkeypatch):
    """avahi-browse is an optional dep — unverifiable, not broken."""
    monkeypatch.setattr("shutil.which", lambda p: None)

    r = peering.check_peering_discovery()

    assert r.status == "warn"
    assert r.reason == peering.REASON_DISCOVERY_TOOL_MISSING


def test_check_peering_discovery_warns_on_browse_failure(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda p: "/usr/bin/avahi-browse")
    monkeypatch.setattr(
        "jasper.cli.doctor.peering._run",
        lambda *a, **kw: type("P", (), {"returncode": 1, "stdout": ""})(),
    )

    r = peering.check_peering_discovery()

    assert r.status == "warn"
    assert r.reason == peering.REASON_DISCOVERY_BROWSE_FAILED
