# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /sound/pair/ wizard — the multi-room directory.

/sound/pair/ renders client-side: GET / is a static canonical shell, and every
discovered (untrusted) field travels over GET /rooms.json as JSON for the ES
module to build with DOM/text APIs. So the shell interpolates no discovered
data, and /rooms.json keeps a hostile peer name a JSON string value.

Also pinned here: the write routes (/peering, /bond, /unbond, /swap, /trim)
and the cross-speaker control calls behind them (the SSRF guard, the household
credential, credential redaction). CSRF-before-body and 404-before-CSRF for
every route live in tests/test_web_wizard_conventions.py, and the socket
ListenStream per wizard port in tests/test_web_main_imports.py.
"""
from __future__ import annotations

import ast
import json
import logging
import shutil
import subprocess
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest

from jasper.control import household_credential
from jasper.net.mdns import DiscoveredService
from jasper.platform.control_client import (
    PEER_DETAIL_MAX_CHARS,
    PEER_RESPONSE_MAX_BYTES,
    ControlError,
    ControlResponse,
    ControlResponseTooLarge,
)
from jasper.web import _common, rooms_peers, rooms_setup

from ._log_events import event_field_maps
from ._web_test_helpers import assert_canonical_page, make_real_handler

_REPO = Path(__file__).resolve().parent.parent
_ROOMS_LOGGER = "jasper.web.rooms_setup"

_ROOMS_SHARED_PUBLIC = {
    "rooms_peers": {
        "self_addresses",
        "discover_speakers_cached",
        "lan_target",
        "request_control_token",
        "post_grouping_to_member",
    },
    "rooms_setup": {"resolve_bond_peer"},
}
_ROOMS_MODULES = {"rooms_peers": rooms_peers, "rooms_setup": rooms_setup}


def test_package_shared_rooms_boundary_is_public_and_lazily_imported():
    for module_name, public in _ROOMS_SHARED_PUBLIC.items():
        tree = ast.parse(
            (_REPO / "jasper" / "web" / f"{module_name}.py").read_text(encoding="utf-8")
        )
        definitions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        old_private = {f"_{name}" for name in public}
        assert public <= definitions, module_name
        assert not (old_private & definitions), module_name
        for private_name in old_private:
            assert not hasattr(_ROOMS_MODULES[module_name], private_name)

    for filename in ("pair_flow.py", "sync_flow.py"):
        tree = ast.parse(
            (_REPO / "jasper" / "web" / filename).read_text(encoding="utf-8")
        )
        module_level = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module in _ROOMS_SHARED_PUBLIC
            for alias in node.names
        }
        assert module_level == set(), f"{filename} must preserve lazy rooms imports"
        imported = {
            (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in _ROOMS_SHARED_PUBLIC
            for alias in node.names
        }
        assert imported
        for module_name, name in imported:
            assert name in _ROOMS_SHARED_PUBLIC[module_name], (filename, name)


# A solo speaker's grouping state, as jasper.multiroom.state.read_grouping_state
# returns it.
_OFF_GROUPING = {
    "enabled": False,
    "role": "",
    "channel": "stereo",
    "bond_id": "",
    "leader_addr": "",
    "buffer_ms": 400,
    "codec": "flac",
    "mains_highpass_enabled": True,
    "subwoofer_present": False,
    "error": None,
}


def _leader(**fields) -> dict:
    return {**_OFF_GROUPING, "enabled": True, "role": "leader", "channel": "left",
            "bond_id": "bond-1", "leader_addr": "", **fields}


def _follower(**fields) -> dict:
    return {**_OFF_GROUPING, "enabled": True, "role": "follower", "channel": "right",
            "bond_id": "bond-1", "leader_addr": "jts.local", **fields}


def _identity(hostname: str = "jts-living.local"):
    return rooms_setup.identity.SpeakerIdentity(
        name="JTS", room="living", hostname=hostname, peer_id="test-peer-id",
    )


_PAIR = [
    {"addr": "192.168.1.5", "role": "leader", "channel": "left"},
    {"addr": "192.168.1.9", "role": "follower", "channel": "right"},
]


class _Lan:
    """This speaker and its household LAN, as the rooms routes reach them.

    Every attribute is read at request time, so a test sets what it needs
    first: `grouping` is this speaker's grouping state, `speakers` the mDNS
    directory, `peer_groupings` each peer's GET /grouping answer,
    `readiness` a member's join verdict (default ready), and `replies` each
    /grouping/set answer by address (a list answers one call per entry).
    """

    def __init__(self) -> None:
        self.identity = _identity()
        self.own = {"192.168.1.5"}
        self.grouping = dict(_OFF_GROUPING)
        self.speakers: list[dict] = []
        self.browses = 0
        self.peer_groupings: dict[str, dict | None] = {}
        self.read_timeouts: list[float] = []
        self.readiness: dict[str, tuple] = {}
        self.replies: dict[str, object] = {}
        self.posts: list[tuple[str, dict, str | None, str | None]] = []

    def discover(self) -> list[dict]:
        self.browses += 1
        return list(self.speakers)

    def read_grouping(
        self, addr, known=None, *, timeout=rooms_peers.CONTROL_HTTP_TIMEOUT_SEC,
    ):
        self.read_timeouts.append(timeout)
        return self.peer_groupings.get(addr)

    def read_readiness(self, addr, known=None, **_kwargs):
        return self.readiness.get(addr, ({"allowed": True, "detail": "ready"}, None))

    def post(self, addr, body, known=None, *, token=None, household=None):
        self.posts.append((addr, body, token, household))
        reply = self.replies.get(addr, (True, "HTTP 200"))
        return reply.pop(0) if isinstance(reply, list) else reply

    def posted(self) -> dict[str, dict]:
        """The last /grouping/set body each address received."""
        return {addr: body for addr, body, *_ in self.posts}


@pytest.fixture
def lan(monkeypatch) -> _Lan:
    fake = _Lan()
    monkeypatch.setattr(rooms_setup.identity, "read_identity", lambda: fake.identity)
    monkeypatch.setattr(rooms_peers, "self_addresses", lambda: set(fake.own))
    monkeypatch.setattr(
        rooms_setup, "read_grouping_state", lambda *_a, **_k: dict(fake.grouping),
    )
    # The real composer reads /var/lib/jasper; what the payload carries is
    # whatever it returns.
    monkeypatch.setattr(
        rooms_setup, "with_airplay_latency_fit",
        lambda g: {**g, "airplay_latency_fit": {"applicable": False}},
    )
    monkeypatch.setattr(rooms_peers, "discover_speakers_cached", fake.discover)
    monkeypatch.setattr(rooms_peers, "get_member_grouping", fake.read_grouping)
    monkeypatch.setattr(
        rooms_peers, "_get_member_grouping_readiness", fake.read_readiness,
    )
    monkeypatch.setattr(rooms_peers, "post_grouping_to_member", fake.post)
    return fake


@pytest.fixture
def restarts(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        rooms_setup, "restart_voice_daemon", lambda: seen.append("jasper-voice"),
    )
    monkeypatch.setattr(
        rooms_setup, "restart_systemd_units", lambda *units: seen.extend(units),
    )
    return seen


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path, restarts):
    """The household secret (a bond mints it) and peering.env (/peering
    rewrites it) live in tmp_path, no daemon restarts, and CSRF is granted:
    its gate is pinned for every wizard route elsewhere."""
    monkeypatch.setattr(
        household_credential, "SECRET_FILE", str(tmp_path / "household_secret"),
    )
    monkeypatch.setattr(
        rooms_setup.peering_config, "PEERING_ENV_FILE", str(tmp_path / "peering.env"),
    )
    for name in ("JASPER_PEERING", "JASPER_PEER_PRIMARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_common, "guard_mutating_request", lambda *_a, **_k: True)


@pytest.fixture
def effects(monkeypatch, restarts) -> list[str]:
    """Every write or state read a POST route could reach, by name."""
    for owner, name in (
        (household_credential, "ensure"),
        (rooms_peers, "fan_out_grouping"),
        (rooms_peers, "post_grouping_to_member"),
        (rooms_setup, "read_grouping_state"),
        (rooms_setup, "_set_pair_balance"),
        (rooms_setup, "locked_transform_env_file"),
    ):
        monkeypatch.setattr(
            owner, name, lambda *_a, _name=name, **_k: restarts.append(_name),
        )
    return restarts


class _TrackingReader(BytesIO):
    def __init__(self, body: bytes, *, fail: bool = False) -> None:
        super().__init__(body)
        self.fail = fail
        self.read_calls: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        self.read_calls.append(size)
        if self.fail:
            raise OSError("request body read failed")
        return super().read(size)


def _get(path: str):
    handler, _ = make_real_handler(rooms_setup._make_handler(), path, body=None)
    handler.do_GET()
    return handler


def _rooms() -> dict:
    return json.loads(_get("/rooms.json").wfile.getvalue())


def _post(path: str, body: dict | bytes = b"{}", *, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    handler, _ = make_real_handler(
        rooms_setup._make_handler(), path, body=raw, content_type=None,
        headers=headers,
    )
    handler.do_POST()
    return handler


def _reply(handler) -> dict:
    return json.loads(handler.wfile.getvalue())


# --- GET / and GET /rooms.json --------------------------------------------


def test_the_page_is_a_static_shell_that_carries_no_discovered_data(lan):
    lan.speakers = [{
        "name": "<script>alert(1)</script>",
        "room": "<img src=x onerror=alert(2)>",
        "address": "10.0.0.7",
    }]
    lan.grouping = _leader(bond_id="ZZBONDZZ")

    handler = _get("/")

    out = handler.wfile.getvalue().decode()
    assert handler.status == 200
    assert handler.header_values("Content-Type") == ["text/html; charset=utf-8"]
    assert_canonical_page(out)
    for marker in (
        '<h1 class="app-header__title">Stereo pair</h1>',
        '<use href="#icon-back"></use>',
        'meta name="jts-csrf"',
        "/assets/rooms/rooms.css?v=",
        '<script type="module" src="/assets/rooms/js/main.js"></script>',
        'id="app"',
    ):
        assert marker in out
    # A page open never waits on a 2 s mDNS browse, and no peer or grouping
    # field reaches the HTML: /rooms.json carries them.
    assert lan.browses == 0
    for leaked in ("alert(1)", "onerror=alert(2)", "10.0.0.7", "ZZBONDZZ"):
        assert leaked not in out


def test_rooms_json_is_live_json_self_plus_every_sibling(lan):
    lan.speakers = [
        {"name": "jts-bedroom", "room": "bedroom", "hostname": "jts-bedroom",
         "address": "192.168.1.9"},
        {"name": "Kitchen", "room": "kitchen", "hostname": "",
         "address": "192.168.1.10"},
    ]

    handler = _get("/rooms.json")

    body = handler.wfile.getvalue()
    assert handler.status == 200
    assert handler.header_values("Content-Type") == ["application/json"]
    assert handler.header_values("Content-Length") == [str(len(body))]
    assert "no-store" in handler.header_values("Cache-Control")[0]
    data = json.loads(body)
    assert data["self"] == {
        "name": "JTS",
        "hostname": "jts-living.local",
        "room": "living",
        "address": "192.168.1.5",
        "grouping": {**_OFF_GROUPING, "airplay_latency_fit": {"applicable": False}},
        "peering": {"enabled": False, "primary": False},
    }
    # The raw address stays for the control fan-out; browser links use the
    # advertised .local host and never fall back to the raw IP.
    assert data["peers"] == [
        {"name": "jts-bedroom", "room": "bedroom", "address": "192.168.1.9",
         "home_url": "http://jts-bedroom.local/",
         "system_url": "http://jts-bedroom.local/system/"},
        {"name": "Kitchen", "room": "kitchen", "address": "192.168.1.10",
         "home_url": "", "system_url": ""},
    ]
    assert data["view"] == {
        "state": "solo", "bonded": False,
        "can_create_pair": True, "can_balance_pair": False,
    }


@pytest.mark.parametrize(
    ("grouping", "view"),
    [
        (_OFF_GROUPING, {"state": "solo", "bonded": False,
                         "can_create_pair": False, "can_balance_pair": False}),
        (_follower(leader_addr="",
                   error="JASPER_GROUPING_LEADER_ADDR is empty for role=follower"),
         {"state": "degraded", "bonded": True,
          "can_create_pair": False, "can_balance_pair": False}),
        (_leader(channel="mono"), {"state": "paired", "bonded": True,
                                   "can_create_pair": False, "can_balance_pair": False}),
    ],
    ids=["alone-cannot-pair", "invalid-config-is-degraded", "mono-bond-has-no-balance"],
)
def test_the_view_model_follows_the_grouping_state(lan, grouping, view):
    lan.grouping = grouping

    data = _rooms()

    assert data["view"] == view
    assert data["self"]["grouping"]["error"] == grouping["error"]
    assert "balance" not in data["self"]["grouping"]


@pytest.mark.parametrize("recorded", [False, True], ids=["by-bond-id", "by-roster"])
def test_rooms_json_carries_the_live_pair_balance(lan, recorded):
    """The slider shows the peer's live trim, read on the short snapshot
    timeout so a powered-off peer cannot stall the 7 s poll."""
    roster = {"peer_addr": "192.168.1.9", "peer_name": "Right"} if recorded else {}
    lan.grouping = _leader(trim_db=-3.0, **roster)
    if not recorded:
        lan.speakers = [{"name": "Right", "hostname": "jts3", "address": "192.168.1.9"}]
    lan.peer_groupings = {"192.168.1.9": _follower(trim_db=0.0)}

    data = _rooms()

    assert data["self"]["grouping"]["balance"] == {
        "applicable": True, "ok": True,
        "left_trim_db": -3.0, "right_trim_db": 0.0, "balance_db": 3.0,
        "self_channel": "left", "peer_channel": "right",
        "peer_addr": "192.168.1.9",
    }
    assert lan.read_timeouts == [rooms_setup.BALANCE_SNAPSHOT_PEER_TIMEOUT_SEC]
    assert data["view"] == {
        "state": "paired", "bonded": True,
        "can_create_pair": False, "can_balance_pair": True,
    }


@pytest.mark.parametrize(
    ("hostname", "own", "speakers", "peers"),
    [
        pytest.param(
            "jts.local", {"192.168.1.74"},
            [{"name": "JTS3", "hostname": "jts3", "room": "", "address": "192.168.1.92"}],
            ["JTS3"],
            id="substring-must-not-exclude",
        ),
        pytest.param(
            "jts.local", {"192.168.1.74"},
            [{"name": "JTS", "hostname": "jts", "room": "", "address": "127.0.1.1"},
             {"name": "JTS3", "hostname": "jts3", "room": "", "address": "192.168.1.92"}],
            ["JTS3"],
            id="exact-hostname-excludes-despite-address-miss",
        ),
        pytest.param(
            "jts-living.local", {"192.168.1.5"},
            [{"name": "Living Room", "hostname": "jts-living", "room": "",
              "address": "192.168.1.99"},
             {"name": "jts-bedroom", "hostname": "jts-bedroom", "room": "bedroom",
              "address": "192.168.1.9"}],
            ["jts-bedroom"],
            id="exact-hostname-beats-decoy-display-name",
        ),
        pytest.param(
            "jts-living.local", {"192.168.1.5"},
            [{"name": "jts-living", "room": "", "address": "192.168.1.5"},
             {"name": "jts-kitchen", "room": "kitchen", "address": "192.168.1.8"}],
            ["jts-kitchen"],
            id="own-address-excludes",
        ),
    ],
)
def test_self_is_dropped_by_address_or_exact_hostname_label(
    lan, hostname, own, speakers, peers,
):
    """Never by substring or display name: "jts" once dropped a peer "jts3"."""
    lan.identity = _identity(hostname)
    lan.own = own
    lan.speakers = speakers

    assert [p["name"] for p in _rooms()["peers"]] == peers


def test_a_hostile_peer_name_stays_a_json_string_value(lan):
    lan.speakers = [{"name": "</script><script>alert(1)</script>",
                     "room": "<b>x</b>", "address": "10.0.0.2"}]

    assert [(p["name"], p["room"]) for p in _rooms()["peers"]] == [
        ("</script><script>alert(1)</script>", "<b>x</b>"),
    ]


def test_a_slow_snapshot_logs_its_stage_timings(lan, monkeypatch, caplog):
    monkeypatch.setattr(rooms_setup, "ROOMS_SNAPSHOT_SLOW_MS", 0)

    with caplog.at_level(logging.INFO, logger=_ROOMS_LOGGER):
        assert _get("/rooms.json").status == 200

    (fields,) = event_field_maps(caplog, "rooms.snapshot")
    assert fields["peer_count"] == "0"
    assert {
        "identity_ms", "self_addr_ms", "grouping_ms", "balance_ms",
        "peering_ms", "discovery_ms", "total_ms",
    } <= set(fields)


def test_unknown_get_route_404s(lan):
    assert _get("/does-not-exist").status == 404


# --- POST /peering and request framing ------------------------------------


@pytest.mark.parametrize(
    ("seeded", "request_body", "written"),
    [
        ("JASPER_PEERING=off\nJASPER_PEER_ROOM=kitchen\n"
         "JASPER_PEER_ARB_WINDOW_MS=250\n",
         {"enabled": True, "primary": True},
         {"JASPER_PEERING": "on", "JASPER_PEER_PRIMARY": "1",
          "JASPER_PEER_ROOM": "kitchen", "JASPER_PEER_ARB_WINDOW_MS": "250"}),
        ("JASPER_PEERING=on\nJASPER_PEER_PRIMARY=1\nJASPER_PEER_ROOM=den\n",
         {"enabled": True, "primary": False},
         {"JASPER_PEERING": "on", "JASPER_PEER_ROOM": "den"}),
        ("JASPER_PEERING=on\nJASPER_PEER_PRIMARY=1\nJASPER_PEER_ROOM=den\n",
         {"enabled": False, "primary": False},
         {"JASPER_PEERING": "off", "JASPER_PEER_ROOM": "den"}),
    ],
    ids=["enable-primary", "drop-primary", "disable"],
)
def test_post_peering_rewrites_only_the_wake_response_keys(
    lan, restarts, tmp_path, seeded, request_body, written,
):
    """The legacy JASPER_PEER_ROOM fallback and operator tuning knobs survive,
    both daemons restart to pick the mode up, and /rooms.json reads the new
    state back fresh."""
    env = tmp_path / "peering.env"
    env.write_text(seeded)

    handler = _post("/peering", request_body)

    assert (handler.status, _reply(handler)) == (
        200, {"ok": True, "peering": request_body},
    )
    assert restarts == ["jasper-voice", "jasper-control"]
    assert rooms_setup.peering_config.read_state(str(env)) == written
    assert _rooms()["self"]["peering"] == request_body


_BOND_BODY = b'{"members":[{"addr":"","role":"leader","channel":"left"}],"bond_id":"bond-1"}'
_TRIM_BODY = b'{"target":"pair","balance_db":1.5}'


@pytest.mark.parametrize(
    ("path", "body", "declared", "reads"),
    [
        ("/peering", b"{", "1", [1]),
        ("/peering", b"{}", "invalid", []),
        ("/peering", b"{}", str(rooms_setup._PEERING_BODY_LIMIT + 1), []),
        ("/bond", _BOND_BODY, str(len(_BOND_BODY) + 1), [len(_BOND_BODY) + 1]),
        ("/trim", _TRIM_BODY, str(len(_TRIM_BODY) + 1), [len(_TRIM_BODY) + 1]),
    ],
    ids=["not-json", "bad-length", "over-the-limit", "short-bond", "short-trim"],
)
def test_a_malformed_body_is_refused_before_any_write(
    effects, path, body, declared, reads,
):
    """The bond and trim bodies are otherwise valid for their routes, so a
    short body that was accepted would reach credential, state or control
    work. An oversized or unparseable length is refused before any read."""
    handler, _ = make_real_handler(
        rooms_setup._make_handler(), path, body=body, content_type=None,
        content_length=declared,
    )
    handler.rfile = _TrackingReader(body)

    handler.do_POST()

    assert handler.status == 400
    reply = _reply(handler)
    assert reply["ok"] is False and isinstance(reply["error"], str)
    assert handler.rfile.read_calls == reads
    assert effects == []


def test_a_failed_body_read_is_not_a_bad_request(effects):
    handler, _ = make_real_handler(
        rooms_setup._make_handler(), "/peering", body=b"{}", content_type=None,
    )
    handler.rfile = _TrackingReader(b"{}", fail=True)

    with pytest.raises(OSError):
        handler.do_POST()

    assert handler.status is None
    assert effects == []


@pytest.mark.parametrize("path", ["/bond", "/bond/", "/bond?x=1"])
def test_post_bond_route_matches_regardless_of_slash_or_query(monkeypatch, path):
    reached = []
    monkeypatch.setitem(
        rooms_setup._POST_ROUTES, "/bond", lambda h: reached.append(h.path),
    )

    _post(path)

    assert reached == [path]


# --- POST /bond -------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_body", "members"),
    [
        pytest.param(
            {"peer_addr": "192.168.1.9"},
            {
                "": {"role": "leader", "channel": "left", "leader_addr": "",
                     "peer_addr": "192.168.1.9", "peer_name": "Right Box",
                     "roster": [{"addr": "192.168.1.9", "name": "Right Box",
                                 "channel": "right"}]},
                "192.168.1.9": {"role": "follower", "channel": "right",
                                "leader_addr": "jts-living.local",
                                "peer_addr": "", "peer_name": "", "roster": []},
            },
            id="stereo-pair-from-peer-addr",
        ),
        pytest.param(
            {"members": [
                {"addr": "", "role": "leader", "channel": "left"},
                {"addr": "192.168.1.9", "role": "follower", "channel": "right",
                 "name": "Right"},
                {"addr": "192.168.1.8", "role": "follower", "channel": "sub",
                 "name": "Sub", "crossover_hz": 90},
            ]},
            {
                "": {"role": "leader", "leader_addr": "",
                     "peer_addr": "192.168.1.9", "peer_name": "Right",
                     "roster": [
                         {"addr": "192.168.1.9", "name": "Right", "channel": "right"},
                         {"addr": "192.168.1.8", "name": "Sub", "channel": "sub"},
                     ]},
                "192.168.1.9": {"role": "follower", "channel": "right",
                                "leader_addr": "jts-living.local",
                                "peer_addr": "", "peer_name": "", "roster": []},
                "192.168.1.8": {"role": "follower", "channel": "sub",
                                "leader_addr": "jts-living.local",
                                "peer_addr": "", "peer_name": "", "roster": []},
            },
            id="2.1-roster-keeps-the-lr-sibling-primary",
        ),
    ],
)
def test_a_new_bond_configures_every_member(lan, request_body, members):
    """One minted bond id and a fresh 0 dB trim on every member. Followers
    get the leader's stable .local handle (DHCP-proof), the leader's roster
    names every follower while peer_addr stays its left/right sibling, and a
    follower's stale roster is cleared. The household credential is minted."""
    lan.speakers = [{"address": "192.168.1.9", "name": "Right Box"}]
    assert household_credential.is_paired() is False

    handler = _post("/bond", request_body)

    reply = _reply(handler)
    assert (handler.status, reply["ok"]) == (200, True)
    assert reply["bond_id"].startswith("bond-")
    posted = lan.posted()
    assert set(posted) == set(members)
    for addr, expected in members.items():
        body = posted[addr]
        assert {key: body[key] for key in expected} == expected
        assert (body["enabled"], body["bond_id"], body["trim_db"]) == (
            True, reply["bond_id"], 0.0,
        )
    assert household_credential.is_paired() is True


def test_re_posting_an_existing_bond_keeps_its_balance(lan):
    """Adding a subwoofer must not reset a calibrated left/right balance."""
    sub = {"addr": "192.168.1.8", "role": "follower", "channel": "sub",
           "crossover_hz": 90}

    handler = _post("/bond", {"bond_id": "bond-existing", "members": [*_PAIR, sub]})

    assert handler.status == 200
    assert all("trim_db" not in body for body in lan.posted().values())


@pytest.mark.parametrize("token", ["browser-token", None])
def test_bond_relays_the_browser_control_token_to_every_member(lan, token):
    """The fan-out runs server-side, so the member's control-token gate only
    sees what the leader relays."""
    handler = _post(
        "/bond", {"members": _PAIR}, headers={"X-JTS-Token": token} if token else None,
    )

    assert handler.status == 200
    assert [sent for _addr, _body, sent, _household in lan.posts] == [token, token]


@pytest.mark.parametrize(
    "readiness",
    [
        ({"allowed": False, "detail": "validate the combined crossover first"}, None),
        (None, "speaker software does not provide grouping readiness"),
    ],
    ids=["not-allowed", "verdict-unavailable"],
)
def test_a_member_that_is_not_ready_blocks_the_whole_bond(lan, readiness):
    lan.readiness["192.168.1.9"] = readiness
    verdict, error = readiness

    handler = _post("/bond", {"members": _PAIR})

    reply = _reply(handler)
    assert (handler.status, reply["ok"]) == (409, False)
    assert reply["results"] == [{
        "addr": "192.168.1.9", "role": "follower", "ok": False,
        "detail": verdict["detail"] if verdict else error,
    }]
    assert lan.posts == []


@pytest.mark.parametrize("request_body", [{"members": []}, {}], ids=["empty", "absent"])
def test_a_bond_without_members_is_refused(lan, request_body):
    assert _post("/bond", request_body).status == 400
    assert lan.posts == []


def test_a_half_formed_bond_is_a_502_that_names_the_failed_member(lan, caplog):
    """The HTTP reply is no diagnostic surface on a headless speaker, so the
    journal names the failed member, and only it."""
    lan.replies["192.168.1.9"] = (False, "Connection refused")

    with caplog.at_level(logging.WARNING, logger=_ROOMS_LOGGER):
        handler = _post("/bond", {"members": _PAIR})

    reply = _reply(handler)
    assert (handler.status, reply["ok"]) == (502, False)
    assert [(r["addr"], r["ok"], r["detail"]) for r in reply["results"]] == [
        ("192.168.1.5", True, "HTTP 200"),
        ("192.168.1.9", False, "Connection refused"),
    ]
    assert [
        (fields["addr"], fields["detail"])
        for fields in event_field_maps(caplog, "rooms.bond.member_failed")
    ] == [("192.168.1.9", "Connection refused")]


# --- POST /unbond, /swap and /trim -----------------------------------------


@pytest.mark.parametrize("path", ["/unbond", "/swap", "/trim"])
@pytest.mark.parametrize(
    "grouping", [_OFF_GROUPING, _leader(bond_id="")],
    ids=["solo", "enabled-without-bond-id"],
)
def test_a_speaker_outside_a_bond_has_nothing_to_dissolve_swap_or_balance(
    lan, path, grouping,
):
    lan.grouping = grouping
    lan.speakers = [{"address": "192.168.1.9"}]

    handler = _post(path, {"target": "pair", "balance_db": 1.0})

    assert (handler.status, _reply(handler)["ok"]) == (400, False)
    assert lan.posts == []


def test_unbond_disables_self_and_every_same_bond_peer_it_can_read(lan, caplog):
    """A peer in another bond is left alone and an unreadable one is counted,
    which explains a "dissolved, but a speaker stayed grouped" report. The
    household secret is read once before the fan-out, so each member's clear
    cannot race a peer out of the credential its own disable needs."""
    secret = household_credential.ensure()
    lan.grouping = _leader()
    lan.speakers = [{"address": a} for a in ("192.168.1.9", "192.168.1.20", "192.168.1.30")]
    lan.peer_groupings = {
        "192.168.1.9": {"enabled": True, "bond_id": "bond-1"},
        "192.168.1.20": {"enabled": True, "bond_id": "bond-OTHER"},
        "192.168.1.30": None,
    }

    with caplog.at_level(logging.INFO, logger=_ROOMS_LOGGER):
        handler = _post("/unbond")

    reply = _reply(handler)
    assert handler.status == 200
    assert (reply["ok"], reply["bond_id"], reply["dissolved"]) == (
        True, "bond-1", ["", "192.168.1.9"],
    )
    assert lan.posted() == {
        "": {"enabled": False, "trim_db": 0.0},
        "192.168.1.9": {"enabled": False, "trim_db": 0.0},
    }
    assert {household for *_, household in lan.posts} == {secret}
    (fields,) = event_field_maps(caplog, "rooms.unbond")
    assert (fields["path"], fields["peers"], fields["unreachable"]) == (
        "discovery", "1", "1",
    )


@pytest.mark.parametrize(
    ("grouping", "peer_groupings", "disabled"),
    [
        pytest.param(
            _leader(peer_addr="192.168.1.9", peer_name="JTS3"),
            {"192.168.1.9": {"enabled": True, "bond_id": "bond-1"},
             "192.168.1.162": {"enabled": True, "bond_id": "bond-1"}},
            ["", "192.168.1.9"],
            id="recorded-sibling-not-the-foreign-claimer",
        ),
        pytest.param(
            _leader(peer_addr="192.168.1.9", peer_name="JTS3"),
            {"192.168.1.9": None},
            ["", "192.168.1.9"],
            id="offline-sibling-still-gets-its-disable",
        ),
        pytest.param(
            _leader(peer_addr="192.168.1.9", peer_name="Right", roster=[
                {"addr": "192.168.1.9", "name": "Right", "channel": "right"},
                {"addr": "192.168.1.8", "name": "Sub", "channel": "sub"},
            ]),
            {"192.168.1.162": {"enabled": True, "bond_id": "bond-1"}},
            ["", "192.168.1.8", "192.168.1.9"],
            id="full-roster-disables-every-member",
        ),
    ],
)
def test_unbond_follows_the_recorded_roster_never_a_foreign_claimer(
    lan, grouping, peer_groupings, disabled,
):
    """A foreign device claiming our bond_id must never be disabled (that
    would be cross-household sabotage), and an offline sibling is still sent
    its disable rather than silently skipped."""
    lan.grouping = grouping
    lan.speakers = [{"address": "192.168.1.9", "name": "JTS3"},
                    {"address": "192.168.1.162", "name": "JTS Endpoint"}]
    lan.peer_groupings = peer_groupings

    handler = _post("/unbond")

    assert (handler.status, _reply(handler)["ok"]) == (200, True)
    assert sorted(lan.posted()) == disabled
    assert all(body == {"enabled": False, "trim_db": 0.0} for body in lan.posted().values())


@pytest.mark.parametrize(
    ("speakers", "replies", "status", "dissolved", "failed"),
    [
        ([], {"": (False, "write failed")}, 502, [], "(self)"),
        ([{"address": "192.168.1.9"}], {"192.168.1.9": (False, "Connection refused")},
         200, [""], "192.168.1.9"),
    ],
    ids=["self-disable-failed", "peer-disable-failed"],
)
def test_unbond_succeeds_exactly_when_self_is_disabled(
    lan, caplog, speakers, replies, status, dissolved, failed,
):
    """Leaving the bond always works locally; a member left stranded is named
    in the journal."""
    lan.grouping = _leader()
    lan.speakers = speakers
    lan.peer_groupings = {"192.168.1.9": {"enabled": True, "bond_id": "bond-1"}}
    lan.replies = replies

    with caplog.at_level(logging.WARNING, logger=_ROOMS_LOGGER):
        handler = _post("/unbond")

    reply = _reply(handler)
    assert (handler.status, reply["ok"], reply["dissolved"]) == (
        status, status == 200, dissolved,
    )
    assert [
        fields["addr"] for fields in event_field_maps(caplog, "rooms.unbond.member_failed")
    ] == [failed]


@pytest.mark.parametrize(
    ("peer_channel", "self_to", "peer_to", "repaired"),
    [("right", "right", "left", False), ("left", "left", "right", True)],
    ids=["swapped", "same-channel-pair-repaired"],
)
def test_swap_exchanges_the_pairs_channels_and_keeps_roles(
    lan, peer_channel, self_to, peer_to, repaired,
):
    """A channel edit, never a leadership change. A {left, left} pair — an
    interrupted swap whose rollback failed too — must be repairable by swap
    itself: self keeps its channel and the peer takes the other."""
    lan.grouping = _leader()
    lan.speakers = [{"address": "192.168.1.9"}]
    lan.peer_groupings = {"192.168.1.9": _follower(channel=peer_channel)}

    handler = _post("/swap")

    reply = _reply(handler)
    assert (handler.status, reply["ok"], reply.get("repaired", False)) == (
        200, True, repaired,
    )
    assert lan.posted() == {
        "": {"enabled": True, "role": "leader", "channel": self_to,
             "bond_id": "bond-1", "leader_addr": ""},
        "192.168.1.9": {"enabled": True, "role": "follower", "channel": peer_to,
                        "bond_id": "bond-1", "leader_addr": "jts.local"},
    }


@pytest.mark.parametrize(
    ("speakers", "peer_groupings", "sibling"),
    [
        ([{"address": "192.168.1.9", "name": "JTS3"},
          {"address": "192.168.1.162", "name": "JTS Endpoint"}],
         {"192.168.1.9": _follower(), "192.168.1.162": _follower()},
         "192.168.1.9"),
        ([{"address": "192.168.1.77", "name": "JTS3"}],
         {"192.168.1.9": None, "192.168.1.77": _follower()},
         "192.168.1.77"),
    ],
    ids=["foreign-claimer-ignored", "sibling-found-by-name-after-dhcp-move"],
)
def test_swap_resolves_the_recorded_sibling(lan, speakers, peer_groupings, sibling):
    """Two devices claiming our bond_id once failed every pair operation with
    "found 2"; the leader's roster names the household's actual choice."""
    lan.grouping = _leader(peer_addr="192.168.1.9", peer_name="JTS3")
    lan.speakers = speakers
    lan.peer_groupings = peer_groupings

    handler = _post("/swap")

    assert _reply(handler)["ok"] is True
    assert sorted(lan.posted()) == ["", sibling]


@pytest.mark.parametrize(
    ("grouping", "speakers", "peer_groupings"),
    [
        pytest.param(_leader(), [{"address": "192.168.1.9"}], {"192.168.1.9": None},
                     id="peer-unreachable"),
        pytest.param(
            _leader(), [{"address": "192.168.1.9"}, {"address": "192.168.1.10"}],
            {"192.168.1.9": _follower(), "192.168.1.10": _follower()},
            id="two-same-bond-peers",
        ),
        pytest.param(
            _leader(channel="mono"), [{"address": "192.168.1.9"}],
            {"192.168.1.9": _follower(channel="mono")},
            id="not-a-left-right-pair",
        ),
        pytest.param(
            _leader(peer_addr="192.168.1.9", peer_name="JTS3"),
            [{"address": "192.168.1.162", "name": "JTS Endpoint"}],
            {"192.168.1.9": None, "192.168.1.162": _follower()},
            id="recorded-sibling-offline-never-inferred",
        ),
    ],
)
def test_swap_without_one_reachable_left_right_sibling_writes_nothing(
    lan, grouping, speakers, peer_groupings,
):
    """A half-swapped pair must be impossible, and an offline recorded
    sibling is never replaced by a bond_id claimer inference would pick."""
    lan.grouping = grouping
    lan.speakers = speakers
    lan.peer_groupings = peer_groupings

    handler = _post("/swap")

    assert (handler.status, _reply(handler)["ok"]) == (400, False)
    assert lan.posts == []


@pytest.mark.parametrize(
    ("self_replies", "rolled_back"),
    [
        ([(True, "HTTP 200"), (True, "HTTP 200")], True),
        ([(True, "HTTP 200"), (False, "control restarting")], False),
    ],
    ids=["rolled-back", "rollback-failed"],
)
def test_a_half_applied_swap_rolls_the_flipped_member_back(
    lan, self_replies, rolled_back,
):
    """The two writes run concurrently; exactly one failing would leave both
    speakers on one channel and block a retry. A failed rollback is reported,
    never silent."""
    lan.grouping = _leader()
    lan.speakers = [{"address": "192.168.1.9"}]
    lan.peer_groupings = {"192.168.1.9": _follower()}
    lan.replies = {"": list(self_replies), "192.168.1.9": (False, "connection refused")}

    handler = _post("/swap")

    reply = _reply(handler)
    assert (handler.status, reply["ok"], reply["rolled_back"]) == (502, False, rolled_back)
    assert len(lan.posts) == 3
    addr, body, *_ = lan.posts[-1]
    assert (addr, body["role"], body["channel"]) == ("", "leader", "left")


@pytest.mark.parametrize(
    ("balance_db", "left", "clamped"),
    [(3.0, -3.0, False), (30.0, -24.0, True)],
    ids=["within-range", "clamped-at-the-attenuation-floor"],
)
def test_pair_balance_writes_absolute_attenuate_only_trims(
    lan, balance_db, left, clamped,
):
    """Positive balance_db means right is louder: right stays at 0 dB and
    left is attenuated, peer written first."""
    lan.grouping = _leader(trim_db=0.0)
    lan.speakers = [{"address": "192.168.1.9"}]
    lan.peer_groupings = {"192.168.1.9": _follower(trim_db=-12.0)}

    handler = _post("/trim", {"target": "pair", "balance_db": balance_db})

    balance = _reply(handler)["balance"]
    assert handler.status == 200
    assert (balance["left_trim_db"], balance["right_trim_db"], balance["clamped"]) == (
        left, 0.0, clamped,
    )
    assert balance["balance_db"] == -left
    assert [(addr, body["trim_db"]) for addr, body, *_ in lan.posts] == [
        ("192.168.1.9", 0.0), ("", left),
    ]


def test_a_failed_local_trim_restores_the_peer(lan):
    """The peer took its new trim but self failed: a failed request must not
    leave the pair audibly half-applied."""
    lan.grouping = _leader(trim_db=-1.0)
    lan.speakers = [{"address": "192.168.1.9"}]
    lan.peer_groupings = {"192.168.1.9": _follower(trim_db=-2.0)}
    lan.replies = {"": (False, "loopback busy")}

    handler = _post("/trim", {"target": "pair", "balance_db": 3.0})

    reply = _reply(handler)
    assert (handler.status, reply["ok"]) == (502, False)
    assert [(addr, body["trim_db"]) for addr, body, *_ in lan.posts] == [
        ("192.168.1.9", 0.0), ("", -3.0), ("192.168.1.9", -2.0),
    ]
    assert reply["rollbacks"] == [{
        "addr": "192.168.1.9", "channel": "right", "trim_db": -2.0,
        "ok": True, "detail": "HTTP 200",
    }]


def test_balance_trim_python_matches_fixture():
    """The fixture is the contract the JS slider is held to as well, by
    scripts/check-balance-trim-parity.mjs in CI's js job."""
    fixture = json.loads(
        (_REPO / "tests" / "fixtures" / "balance_trim_parity_fixture.json").read_text(),
    )
    for case in fixture["cases"]:
        assert rooms_setup._balance_trims_from_db(case["balance_db"]) == (
            case["left"], case["right"], case["clamped"],
        ), case["balance_db"]


# --- Discovery ----------------------------------------------------------------


def _service(host: str, address: str, **txt: str) -> DiscoveredService:
    return DiscoveredService(
        name=f"{host}._jasper-control._tcp.local.", server=f"{host}.local.",
        addresses=(address,) if address else (), port=8780, txt=txt,
    )


def test_discovery_is_cached_for_its_ttl_and_never_blanked_by_an_empty_browse(
    monkeypatch,
):
    """/rooms.json polls every 7 s; a browse per poll would churn a multicast
    listener ~8x/min. A transient empty scan keeps the last directory and
    does not restart the TTL, so the next poll browses again."""
    monkeypatch.setitem(rooms_peers._disc_cache, "at", 0.0)
    monkeypatch.setitem(rooms_peers._disc_cache, "result", [])
    browses = [
        [_service("jts3", "192.168.1.9", name="JTS3", room="den"),
         _service("no-address", "")],
        [],
        [_service("jts4", "192.168.1.10")],
    ]
    monkeypatch.setattr(rooms_peers, "browse_once", lambda *_a, **_k: browses.pop(0))
    jts3 = [{"name": "JTS3", "hostname": "jts3", "room": "den", "address": "192.168.1.9"}]

    assert rooms_peers.discover_speakers_cached() == jts3
    assert rooms_peers.discover_speakers_cached() == jts3
    rooms_peers._disc_cache["at"] -= rooms_peers.DISCOVERY_CACHE_TTL_SEC
    assert rooms_peers.discover_speakers_cached() == jts3
    assert rooms_peers.discover_speakers_cached()[0]["address"] == "192.168.1.10"
    assert browses == []


@pytest.mark.parametrize(
    ("txt", "server", "label"),
    [
        ({"name": "Living Room"}, "jts.local.", "Living Room"),
        ({}, "jts3.local.", "jts3"),
        ({}, "speaker.local", "speaker"),
        ({}, "", "Kitchen"),
    ],
    ids=["txt-name", "srv-host", "srv-host-without-root-dot", "instance-name"],
)
def test_a_peer_is_labelled_by_txt_name_then_host_then_instance(txt, server, label):
    """Never the verbose default instance name while a better label exists."""
    assert rooms_peers._peer_label(
        txt, server, "Kitchen._jasper-control._tcp.local.",
    ) == label


def test_self_addresses_never_include_loopback():
    addresses = rooms_peers.self_addresses()

    assert all(isinstance(a, str) and not a.startswith("127.") for a in addresses)


# --- The cross-speaker control calls ----------------------------------------


def _stub_control_post(monkeypatch, result):
    """Stub the control-client POST behind `post_grouping_to_member`; `result`
    is the ControlResponse to return or an exception to raise. Each captured
    call carries the target, the body and the lowercased request headers."""
    calls: list[dict] = []

    def fake_post(path, body=None, *, base_url, timeout, headers, max_bytes=None):
        calls.append({
            "path": path, "body": body, "base_url": base_url, "timeout": timeout,
            "headers": {k.lower(): v for k, v in headers.items()},
        })
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(rooms_peers, "control_post", fake_post)
    return calls


def _stub_control_get(monkeypatch, result):
    """`_stub_control_post`'s counterpart for the GET /grouping readers."""
    calls: list[dict] = []

    def fake_get(path, *, base_url, timeout, max_bytes=None):
        calls.append({
            "path": path, "base_url": base_url, "timeout": timeout,
            "max_bytes": max_bytes,
        })
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(rooms_peers, "control_get", fake_get)
    return calls


@pytest.mark.parametrize(
    ("address", "known", "expected"),
    [
        ("", set(), "127.0.0.1"),
        ("192.168.1.5", {"192.168.1.5"}, "127.0.0.1"),
        ("192.168.1.9", set(), "192.168.1.9"),
        ("10.0.0.9", set(), "10.0.0.9"),
        ("127.0.0.2", set(), "127.0.0.2"),
        ("8.8.8.8", set(), None),
        ("jts3.local", set(), None),
        ("::1", set(), None),
        ("::1", {"::1"}, None),
        ("fd00::7", set(), None),
        ("fe80::7", set(), None),
        ("::ffff:192.168.1.9", set(), None),
    ],
)
def test_lan_target_acceptance_is_ipv4_only(address, known, expected):
    assert rooms_peers.lan_target(address, known) == expected


@pytest.mark.parametrize("address", ["8.8.8.8", "evil.example.com", "fd00::7"])
def test_a_refused_target_is_never_contacted(monkeypatch, address):
    never = AssertionError("a refused target must never reach the control client")
    _stub_control_post(monkeypatch, never)
    _stub_control_get(monkeypatch, never)

    assert rooms_peers.post_grouping_to_member(address, {}, known=set())[0] is False
    assert rooms_peers.get_member_grouping(address, known=set()) is None


@pytest.mark.parametrize(
    ("address", "base_url"),
    [
        ("192.168.1.5", "http://127.0.0.1:8780"),
        ("192.168.1.9", "http://192.168.1.9:8780"),
    ],
    ids=["self-over-loopback", "peer"],
)
def test_member_post_targets_the_control_port(monkeypatch, address, base_url):
    monkeypatch.setattr(rooms_peers, "self_addresses", lambda: {"192.168.1.5"})
    calls = _stub_control_post(monkeypatch, ControlResponse(200, b""))

    ok, _detail = rooms_peers.post_grouping_to_member(address, {"x": 1})

    assert ok is True
    assert [(c["base_url"], c["path"]) for c in calls] == [(base_url, "/grouping/set")]


@pytest.mark.parametrize("status", [303, 307])
def test_member_post_never_reports_a_redirect_as_applied(monkeypatch, status):
    """The transport never follows a 3xx, so a redirect that used to drop the
    body on the POST-to-GET downgrade and answer 200 cannot come back ok."""
    _stub_control_post(monkeypatch, ControlResponse(status, b""))

    ok, _detail = rooms_peers.post_grouping_to_member(
        "192.168.1.9", {"enabled": True}, known=set(), household="house-secret",
    )

    assert ok is False


@pytest.mark.parametrize(
    ("paired", "household", "token", "sent"),
    [
        (True, None, "browser-tok",
         {"x-jts-household": "<on disk>", "x-jts-token": "browser-tok"}),
        (False, None, None, {}),
        (True, "pre-read-secret", None, {"x-jts-household": "pre-read-secret"}),
    ],
    ids=["read-from-disk-beside-the-relayed-token", "unpaired-sends-none",
         "explicit-household-wins"],
)
def test_member_post_presents_the_household_credential(
    monkeypatch, paired, household, token, sent,
):
    """X-JTS-Household is the device-to-device credential each member
    verifies (control-plane-auth §6); a lone speaker has none to present."""
    secret = household_credential.ensure() if paired else None
    calls = _stub_control_post(monkeypatch, ControlResponse(200, b""))

    ok, _detail = rooms_peers.post_grouping_to_member(
        "192.168.1.9", {"x": 1}, known=set(), token=token, household=household,
    )

    assert ok is True
    headers = calls[0]["headers"]
    assert {k: v for k, v in headers.items() if k.startswith("x-jts-")} == {
        k: secret if v == "<on disk>" else v for k, v in sent.items()
    }


def test_member_post_fails_soft_and_redacts_a_transport_failure(monkeypatch):
    """A refused connection, a malformed reply and a body over the client's
    cap all arrive as ControlError. The fan-out must get (False, detail)
    rather than an exception, and the detail must not carry the credential
    the failed request presented."""
    household = "house-secret"
    _stub_control_post(
        monkeypatch,
        ControlError(f"jasper-control POST /grouping/set: sent {household}"),
    )

    ok, detail = rooms_peers.post_grouping_to_member(
        "192.168.1.9", {"enabled": False}, known=set(), household=household,
    )
    assert ok is False
    assert household not in detail
    assert "<redacted>" in detail


def test_member_post_redacts_echoed_credentials_from_http_error(monkeypatch):
    token = "browser-secret"
    household = "house-secret"
    _stub_control_post(
        monkeypatch,
        ControlResponse(403, f"denied {token} {household}".encode()),
    )

    ok, detail = rooms_peers.post_grouping_to_member(
        "192.168.1.9", {}, known=set(), token=token, household=household,
    )
    assert ok is False
    assert detail == "HTTP 403: denied <redacted> <redacted>"
    assert token not in detail
    assert household not in detail


def test_member_post_redacts_before_capping_the_http_error_body(monkeypatch):
    """A peer's error body can echo the household credential in a shape no
    `redact_secrets` pattern recognises (a bare JSON field, no `token=` or
    `X-JTS-Household:` neighbour), so only the literal pass removes it — and
    it must run BEFORE the detail cap: this credential straddles the cap
    boundary, so cap-then-redact would carry its head into the returned
    detail with no marker at all."""
    household = "kR3n9QpZ7sT2vX8b"
    prefix = '{"error":"household_mismatch","presented":"'
    pad = "f" * (PEER_DETAIL_MAX_CHARS - len(prefix) - len("<redacted>"))
    body = (prefix + pad + household + '"}').encode()
    _stub_control_post(monkeypatch, ControlResponse(403, body))

    ok, detail = rooms_peers.post_grouping_to_member(
        "192.168.1.9", {}, known=set(), household=household,
    )
    assert ok is False
    # The head is the fragment a cap-then-redact order would leave exposed.
    assert household[:8] not in detail
    assert len(detail) == len("HTTP 403: ") + PEER_DETAIL_MAX_CHARS


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b'{"ok":true,"live_apply":{"applied":true,"mode":"outputd","trim_db":-2.5}}',
         "Applied live."),
        (b'{"ok":true,"reconciler_kicked":true,'
         b'"live_apply":{"applied":false,"mode":"outputd","trim_db":-2.5}}',
         "Saved; audio update scheduled."),
        (b"", "HTTP 200"),
    ],
    ids=["applied-live", "apply-scheduled", "no-body"],
)
def test_grouping_set_success_detail_tells_live_from_scheduled_apply(body, detail):
    assert rooms_peers._grouping_set_success_detail(200, body) == detail


def test_fan_out_grouping_computes_self_addresses_once_not_per_member(monkeypatch):
    """self_addresses is a socket probe plus getaddrinfo; the SSRF guard
    shares one answer across every member of a fan-out."""
    calls = {"n": 0}

    def counting_self_addresses():
        calls["n"] += 1
        return {"192.168.1.5"}

    monkeypatch.setattr(rooms_peers, "self_addresses", counting_self_addresses)
    monkeypatch.setattr(
        rooms_peers, "post_grouping_to_member",
        lambda addr, body, known=None, *, token=None, household=None: (True, "HTTP 200"),
    )

    out = rooms_peers.fan_out_grouping([(f"192.168.1.{i}", {}) for i in (10, 11, 12)])

    assert len(out) == 3
    assert calls["n"] == 1


def test_map_peers_runs_concurrently_and_preserves_input_order():
    """The fan-out and the discovery reads pair results back to their inputs
    by position, whatever order the peers answer in."""
    started = threading.Event()

    def fn(i):
        if i == 0:
            started.set()
            time.sleep(0.05)
            return "a"
        started.wait(timeout=1.0)
        return "b"

    t0 = time.monotonic()
    out = rooms_peers.map_peers(fn, [0, 1])
    elapsed = time.monotonic() - t0

    assert out == ["a", "b"]
    assert elapsed < 0.2
    assert rooms_peers.map_peers(fn, []) == []


def test_map_peers_caps_worker_count():
    """A large household cannot spawn an unbounded number of blocking-HTTP
    threads."""
    peak = {"n": 0}
    live = {"n": 0}
    lock = threading.Lock()
    release = threading.Event()

    def fn(_i):
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        release.wait(timeout=1.0)
        with lock:
            live["n"] -= 1

    worker = threading.Thread(target=rooms_peers.map_peers, args=(fn, range(20)))
    worker.start()
    time.sleep(0.1)
    release.set()
    worker.join(timeout=2.0)

    assert peak["n"] <= rooms_peers._PEER_FANOUT_MAX_WORKERS


def test_a_peer_read_forwards_the_request_under_the_peer_cap(monkeypatch):
    calls = _stub_control_get(monkeypatch, ControlResponse(200, b'{"ok": true}'))

    assert rooms_peers._get_remote_json_result(
        "192.168.1.9", "/state", timeout=0.375,
    ) == ({"ok": True}, None)
    # A peer is not this box: it gets the small cap, not the local one.
    assert calls == [{
        "path": "/state", "base_url": "http://192.168.1.9:8780",
        "timeout": 0.375, "max_bytes": PEER_RESPONSE_MAX_BYTES,
    }]


def test_a_failed_peer_read_fails_soft_with_one_reason_per_failure_class(monkeypatch):
    """An oversized reply means the speaker ANSWERED: reusing the unreachable
    reason would send the operator after power and cabling that are fine."""
    failures = {
        "unreachable": ControlError("connection refused"),
        "oversized": ControlResponseTooLarge("over cap", status=200),
        "http-status": ControlResponse(503, b'{"ok": true}'),
        "not-json": ControlResponse(200, b"not-json"),
        "not-an-object": ControlResponse(200, b"[]"),
        "not-utf8": ControlResponse(200, b"\xff"),
    }
    reasons = {}
    for kind, result in failures.items():
        _stub_control_get(monkeypatch, result)
        parsed, reasons[kind] = rooms_peers._get_remote_json_result(
            "192.168.1.9", "/grouping", timeout=0.5,
        )
        assert parsed is None

    assert reasons["not-json"] == reasons["not-an-object"] == reasons["not-utf8"]
    distinct = {reasons[k] for k in ("unreachable", "oversized", "http-status", "not-json")}
    assert len(distinct) == 4 and all(distinct)
    assert "503" in reasons["http-status"]


@pytest.mark.parametrize(
    ("body", "grouping"),
    [
        ({"grouping": {"enabled": True, "role": "follower", "bond_id": "bond-abc"}},
         {"enabled": True, "role": "follower", "bond_id": "bond-abc"}),
        ({"grouping": None}, None),
        ({"enabled": True, "bond_id": "x"}, None),
    ],
    ids=["envelope-unwrapped", "null-envelope", "flat-body"],
)
def test_get_member_grouping_reads_inside_the_grouping_envelope(
    monkeypatch, body, grouping,
):
    """The bond_id a dissolve compares lives inside the envelope; returning
    the raw body once left /unbond matching no real peer. A body without a
    dict envelope reads as unknown, so it can never match a bond."""
    calls = _stub_control_get(
        monkeypatch, ControlResponse(200, json.dumps(body).encode()),
    )

    assert rooms_peers.get_member_grouping(
        "192.168.1.9", known=set(), timeout=0.625,
    ) == grouping
    assert [(c["base_url"], c["path"], c["timeout"]) for c in calls] == [
        ("http://192.168.1.9:8780", "/grouping", 0.625),
    ]


@pytest.mark.parametrize(
    ("addr", "target"),
    [("192.168.1.9", "192.168.1.9"), ("", "127.0.0.1")],
    ids=["peer", "self-over-loopback"],
)
def test_grouping_readiness_is_the_members_own_verdict(monkeypatch, addr, target):
    """Self and peers consume one public verdict, so policy cannot diverge."""
    verdict = {"allowed": False, "detail": "finish commissioning"}
    calls = _stub_control_get(
        monkeypatch, ControlResponse(200, json.dumps({"readiness": verdict}).encode()),
    )

    assert rooms_peers._get_member_grouping_readiness(
        addr, known=set(), timeout=0.75,
    ) == (verdict, None)
    assert [(c["base_url"], c["path"], c["timeout"]) for c in calls] == [
        (f"http://{target}:8780", "/grouping", 0.75),
    ]


def test_missing_or_undeterminable_readiness_fails_closed_with_distinct_reasons(
    monkeypatch,
):
    """An old peer (no readiness key) needs an update; a current one that
    answered null needs its diagnostics. Neither may join."""
    reasons = []
    for body in ({"grouping": {"enabled": False}}, {"grouping": None, "readiness": None}):
        _stub_control_get(monkeypatch, ControlResponse(200, json.dumps(body).encode()))
        readiness, reason = rooms_peers._get_member_grouping_readiness(
            "192.168.1.9", known=set(),
        )
        assert readiness is None
        reasons.append(reason)

    assert all(reasons) and reasons[0] != reasons[1]


# --- Wiring and the page's own modules -------------------------------------


def test_the_rooms_wizard_is_registered_once_on_its_port():
    from jasper.web import __main__ as web_main  # lazy: imports every wizard

    (spec,) = [s for s in web_main.WIZARD_SPECS if s.label == "/rooms"]
    assert (spec.env_var, spec.default_port) == ("JASPER_ROOMS_WEB_PORT", 8785)
    assert spec.make_server is rooms_setup.make_server


def test_make_server_binds_a_tuple_target():
    server = rooms_setup.make_server(("127.0.0.1", 0))
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] != 0
        assert server.RequestHandlerClass is rooms_setup._make_handler()
    finally:
        server.server_close()


_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "script",
    [
        # appendChildren stays exported and flattens summarize()'s nested
        # fallback; un-exporting it threw on every bonded poll.
        "dom_test.mjs",
        # No bond-card anchor leaves the household's origin (#1842, #2632).
        "rooms_bond_card_link_test.mjs",
        "rooms_grouping_view_test.mjs",
        # The slider's live saves, queued commits, failed-write rollback and
        # poll reconciliation.
        "rooms_pair_balance_controller_test.mjs",
    ],
)
def test_rooms_page_modules_via_node(script):
    proc = subprocess.run(
        [_NODE, str(_REPO / "tests" / "js" / script)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {"ok": True}
