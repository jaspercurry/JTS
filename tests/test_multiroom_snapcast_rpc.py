"""snapcast_rpc — the group→stream binding pin + the health probe.

The 2026-06-11 silent-bond incident class: snapcast persists
group→stream assignments in server.json; a stale binding (the
distro-snapserver era's "default") makes a client play zeros behind
green health. All logic is exercised through an injected fake
transport — no snapserver, no network.
"""
from __future__ import annotations

from jasper.multiroom.snapcast_rpc import (
    ensure_groups_on_stream,
    read_stream_clients,
    summarize_groups,
)


def _status(groups):
    return {"server": {"groups": groups}}


def _group(gid, stream, clients):
    return {"id": gid, "stream_id": stream, "clients": clients}


def _client(name, connected=True, muted=False, percent=100):
    return {
        "host": {"name": name},
        "connected": connected,
        "config": {"volume": {"muted": muted, "percent": percent}},
    }


class FakeTransport:
    """Canned Server.GetStatus responses + a recorder for SetStream."""

    def __init__(self, statuses):
        self.statuses = list(statuses)  # popped per GetStatus call
        self.set_calls: list[tuple[str, str]] = []

    def __call__(self, method, params=None, *, url=None):
        if method == "Server.GetStatus":
            return self.statuses.pop(0) if self.statuses else None
        if method == "Group.SetStream":
            self.set_calls.append((params["id"], params["stream_id"]))
            return {"stream_id": params["stream_id"]}
        raise AssertionError(f"unexpected method {method}")


def test_summarize_groups_flattens_and_defaults_safe():
    rows = summarize_groups(_status([
        _group("g1", "jts", [_client("jts")]),
        _group("g2", "default", [_client("jts3", connected=False, muted=True, percent=0)]),
        {"id": "g3", "clients": [{}]},  # snapcast drift: missing keys
    ]))
    assert rows[0] == {
        "group_id": "g1", "stream_id": "jts", "name": "jts",
        "connected": True, "muted": False, "volume_percent": 100,
    }
    assert rows[1]["stream_id"] == "default"
    assert rows[1]["muted"] is True and rows[1]["volume_percent"] == 0
    # Missing keys default safe, never raise.
    assert rows[2]["name"] == "" and rows[2]["connected"] is False


def test_ensure_rebinds_wrong_groups_including_disconnected():
    """THE incident pin: every persisted group lands on our stream —
    disconnected clients' groups too (a follower reconnecting tomorrow
    must not land in a stale binding)."""
    t = FakeTransport([_status([
        _group("good", "jts", [_client("jts")]),
        _group("stale-live", "default", [_client("jts3")]),
        _group("stale-idle", "default", [_client("kitchen", connected=False)]),
    ])])
    report = ensure_groups_on_stream("jts", transport=t, sleep=lambda s: None)
    assert report == {"reachable": True, "groups": 3, "fixed": 2, "failed": 0}
    assert ("stale-live", "jts") in t.set_calls
    assert ("stale-idle", "jts") in t.set_calls
    assert all(gid != "good" for gid, _ in t.set_calls)  # correct group untouched


def test_ensure_retries_while_snapserver_boots():
    """The reconciler runs the pin right after starting snapserver — the
    first GetStatus may land before the RPC socket is up."""
    t = FakeTransport([None, None, _status([_group("g", "default", [_client("jts")])])])
    slept = []
    report = ensure_groups_on_stream(
        "jts", transport=t, attempts=4, sleep=slept.append,
    )
    assert report["reachable"] is True and report["fixed"] == 1
    assert len(slept) == 2  # one sleep per failed attempt, none after success


def test_ensure_unreachable_reports_honestly():
    t = FakeTransport([None, None])
    report = ensure_groups_on_stream(
        "jts", transport=t, attempts=2, sleep=lambda s: None,
    )
    assert report == {"reachable": False, "groups": 0, "fixed": 0, "failed": 0}


def test_ensure_counts_failed_setstream():
    class RefusingTransport(FakeTransport):
        def __call__(self, method, params=None, *, url=None):
            if method == "Group.SetStream":
                return None  # snapserver refused / dropped
            return super().__call__(method, params, url=url)

    t = RefusingTransport([_status([_group("g", "default", [_client("jts")])])])
    report = ensure_groups_on_stream("jts", transport=t, sleep=lambda s: None)
    assert report["failed"] == 1 and report["fixed"] == 0


def test_read_stream_clients_fail_soft():
    assert read_stream_clients(transport=lambda *a, **k: None) is None
    rows = read_stream_clients(
        transport=lambda *a, **k: _status([_group("g", "jts", [_client("jts")])]),
    )
    assert rows and rows[0]["name"] == "jts"
