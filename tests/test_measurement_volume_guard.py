# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.measurement.volume_guard import (
    VolumeGuardError,
    normalized_pair_volumes,
)


class FakeCamilla:
    def __init__(self, initial_db=-31.5):
        self.initial_db = initial_db
        self.sets = []

    async def get_volume_db(self, **_kwargs):
        return self.initial_db

    async def set_volume_db(self, db, **_kwargs):
        self.sets.append(float(db))
        return True


def _members():
    return {
        "left": {
            "is_self": True,
            "label": "this speaker (jts.local)",
            "snapcast_name": "jts",
            "grouping": {},
        },
        "right": {
            "is_self": False,
            "label": "Kitchen",
            "snapcast_name": "jts3",
            "grouping": {},
        },
    }


def _row(name, *, client_id, group_id, percent, muted, group_muted):
    return {
        "name": name,
        "client_id": client_id,
        "group_id": group_id,
        "connected": True,
        "stream_id": "jts",
        "volume_percent": percent,
        "muted": muted,
        "group_muted": group_muted,
    }


async def test_normalized_pair_volumes_restores_camilla_and_snapcast(
    monkeypatch,
):
    import jasper.multiroom.snapcast_rpc as snapcast_rpc

    rows = [
        _row(
            "jts", client_id="cid-left", group_id="gid-left",
            percent=42, muted=True, group_muted=False,
        ),
        _row(
            "jts3", client_id="cid-right", group_id="gid-right",
            percent=17, muted=False, group_muted=True,
        ),
    ]
    group_calls = []
    volume_calls = []

    monkeypatch.setattr(snapcast_rpc, "read_stream_clients", lambda: rows)
    monkeypatch.setattr(
        snapcast_rpc,
        "set_group_mute",
        lambda group_id, muted: group_calls.append((group_id, muted)) or True,
    )
    monkeypatch.setattr(
        snapcast_rpc,
        "set_client_volume",
        lambda client_id, *, percent, muted: (
            volume_calls.append((client_id, percent, muted)) or True
        ),
    )

    camilla = FakeCamilla()
    async with normalized_pair_volumes(
        hostname="jts.local",
        members=_members(),
        camilla=camilla,
    ) as report:
        assert report.snapshot.main_volume_db == -31.5
        assert camilla.sets == [-12.0]
        assert group_calls == [("gid-left", False), ("gid-right", False)]
        assert volume_calls == [
            ("cid-left", 100, False),
            ("cid-right", 100, False),
        ]

    assert group_calls == [
        ("gid-left", False),
        ("gid-right", False),
        ("gid-left", False),
        ("gid-right", True),
    ]
    assert volume_calls == [
        ("cid-left", 100, False),
        ("cid-right", 100, False),
        ("cid-left", 42, True),
        ("cid-right", 17, False),
    ]
    assert camilla.sets == [-12.0, -31.5]


async def test_normalized_pair_volumes_restores_after_partial_snapcast_failure(
    monkeypatch,
):
    import jasper.multiroom.snapcast_rpc as snapcast_rpc

    rows = [
        _row(
            "jts", client_id="cid-left", group_id="gid-left",
            percent=42, muted=True, group_muted=False,
        ),
        _row(
            "jts3", client_id="cid-right", group_id="gid-right",
            percent=17, muted=False, group_muted=True,
        ),
    ]
    group_calls = []
    volume_calls = []

    def set_client_volume(client_id, *, percent, muted):
        volume_calls.append((client_id, percent, muted))
        if client_id == "cid-right" and percent == 100:
            return False
        return True

    monkeypatch.setattr(snapcast_rpc, "read_stream_clients", lambda: rows)
    monkeypatch.setattr(
        snapcast_rpc,
        "set_group_mute",
        lambda group_id, muted: group_calls.append((group_id, muted)) or True,
    )
    monkeypatch.setattr(snapcast_rpc, "set_client_volume", set_client_volume)

    camilla = FakeCamilla()
    with pytest.raises(VolumeGuardError, match="could not set snapcast volume"):
        async with normalized_pair_volumes(
            hostname="jts.local",
            members=_members(),
            camilla=camilla,
        ):
            pass

    assert group_calls == [
        ("gid-left", False),
        ("gid-right", False),
        ("gid-left", False),
        ("gid-right", True),
    ]
    assert volume_calls == [
        ("cid-left", 100, False),
        ("cid-right", 100, False),
        ("cid-left", 42, True),
        ("cid-right", 17, False),
    ]
    assert camilla.sets == [-12.0, -31.5]


async def test_restore_attempts_all_clients_when_a_middle_client_fails(
    monkeypatch, caplog,
):
    """A restore-phase RPC failure on one client must not leave later clients
    stuck at the calibration volume — every client is still attempted."""
    import logging

    import jasper.multiroom.snapcast_rpc as snapcast_rpc

    rows = [
        _row(
            "jts", client_id="cid-left", group_id="gid-left",
            percent=42, muted=True, group_muted=False,
        ),
        _row(
            "jts3", client_id="cid-right", group_id="gid-right",
            percent=17, muted=False, group_muted=True,
        ),
    ]
    volume_calls = []

    def set_client_volume(client_id, *, percent, muted):
        volume_calls.append((client_id, percent, muted))
        # Fail ONLY the first client's RESTORE write (percent==42). If the
        # loop aborted here, cid-right would never be restored.
        if client_id == "cid-left" and percent == 42:
            return False
        return True

    monkeypatch.setattr(snapcast_rpc, "read_stream_clients", lambda: rows)
    monkeypatch.setattr(
        snapcast_rpc, "set_group_mute", lambda group_id, muted: True,
    )
    monkeypatch.setattr(snapcast_rpc, "set_client_volume", set_client_volume)

    camilla = FakeCamilla()
    # The finally swallows restore errors (a failed restore must not mask the
    # measurement result), so no exception propagates out of the guard.
    with caplog.at_level(logging.ERROR):
        async with normalized_pair_volumes(
            hostname="jts.local",
            members=_members(),
            camilla=camilla,
        ):
            pass

    # Restore phase = the calls with the ORIGINAL percents. cid-right (17) is
    # attempted despite cid-left (42) failing first.
    assert ("cid-left", 42, True) in volume_calls
    assert ("cid-right", 17, False) in volume_calls
    # camilla restore still runs after the snapcast best-effort restore.
    assert camilla.sets == [-12.0, -31.5]
    # The per-client failure is logged, not silently dropped.
    assert any(
        "cid" in r.getMessage() or "jts" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.ERROR
    )


async def test_normalized_pair_volumes_fails_when_snapcast_client_missing(
    monkeypatch,
):
    import jasper.multiroom.snapcast_rpc as snapcast_rpc

    monkeypatch.setattr(
        snapcast_rpc,
        "read_stream_clients",
        lambda: [_row(
            "jts", client_id="cid-left", group_id="gid-left",
            percent=100, muted=False, group_muted=False,
        )],
    )

    with pytest.raises(VolumeGuardError, match="could not find"):
        async with normalized_pair_volumes(
            hostname="jts.local",
            members=_members(),
            camilla=FakeCamilla(),
        ):
            pass
