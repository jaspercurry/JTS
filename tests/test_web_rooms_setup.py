# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /rooms/ wizard — the multi-room directory.

/rooms/ renders client-side: the GET / body is a static canonical
shell (an `#app` mount point + a `type="module"` loader) and ALL discovered,
untrusted data is delivered separately over GET /rooms.json (an
`application/json` document, not HTML) for the ES module to build via DOM/text
APIs. So the escaping contract this file asserts is two-pronged:

1. The server-rendered HTML shell interpolates NO discovered/untrusted data at
   all — there is no HTML-injection surface to escape.
2. /rooms.json is content-typed `application/json` and is structurally JSON, so
   a hostile peer name (`<script>…`) round-trips as a string value, never as
   markup.

It also pins the write scope (exactly one POST — /peering, the wake-response
toggle, reusing jasper.peering.config's env file + readers), the
/rooms.json payload shape (incl. the self `peering` block), self-exclusion from
`peers`, the canonical-page surface, the public module surface, and the wiring
contract (port 8785 / env var / route / ListenStream / nginx) that other agents
and the socket-activation host depend on. The cross-wizard registry checks
(unique route/env/port, socket-backed) live in tests/test_web_main_imports.py
via WIZARD_SPECS; the rooms-specific constants are pinned here so a rename can't
silently drift them.
"""
from __future__ import annotations

import asyncio
import ast
import json
import logging
import shutil
import subprocess
from email.message import Message
from io import BytesIO
from pathlib import Path

import pytest

from jasper.control import household_credential
from jasper.web import rooms_setup


_REPO = Path(__file__).resolve().parent.parent
_REAL_GET_MEMBER_ACTIVE_SPEAKER_SETUP = rooms_setup._get_member_active_speaker_setup

_ROOMS_SHARED_PUBLIC = {
    "self_addresses",
    "discover_speakers_cached",
    "lan_target",
    "request_control_token",
    "post_grouping_to_member",
    "resolve_bond_peer",
}
_ROOMS_SHARED_OLD_PRIVATE = {f"_{name}" for name in _ROOMS_SHARED_PUBLIC}


def test_package_shared_rooms_boundary_is_public_and_lazily_imported():
    rooms_tree = ast.parse(
        (_REPO / "jasper" / "web" / "rooms_setup.py").read_text(encoding="utf-8")
    )
    definitions = {
        node.name for node in rooms_tree.body if isinstance(node, ast.FunctionDef)
    }
    assert _ROOMS_SHARED_PUBLIC <= definitions
    assert not (_ROOMS_SHARED_OLD_PRIVATE & definitions)
    for private_name in _ROOMS_SHARED_OLD_PRIVATE:
        assert not hasattr(rooms_setup, private_name)

    for filename in ("pair_flow.py", "sync_flow.py", "balance_flow.py"):
        tree = ast.parse(
            (_REPO / "jasper" / "web" / filename).read_text(encoding="utf-8")
        )
        module_level = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "rooms_setup"
            for alias in node.names
        }
        assert module_level == set(), f"{filename} must preserve lazy rooms imports"
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "rooms_setup"
            for alias in node.names
        }
        assert imported
        assert imported <= _ROOMS_SHARED_PUBLIC, filename


@pytest.fixture(autouse=True)
def _isolate_household_secret(monkeypatch, tmp_path):
    """Point the household credential at a throwaway path for every test here.

    _save_bond now mints the household secret (household_credential.ensure()),
    which writes /var/lib/jasper/household_secret in production. Redirect it to a
    tmp file so the bond tests neither touch a real system path nor crash where
    that dir is absent — and so a test can assert the mint happened.
    """
    monkeypatch.setattr(
        household_credential, "SECRET_FILE", str(tmp_path / "household_secret"),
    )
    monkeypatch.setattr(
        rooms_setup,
        "_get_member_active_speaker_setup",
        lambda *a, **k: {
            "active": False,
            "configured": True,
            "volume_allowed": True,
            "grouping_allowed": True,
        },
    )


# A representative grouping-state snapshot (the shape
# jasper.multiroom.state.read_grouping_state returns). Default: solo/off.
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


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for driving do_GET."""

    def __init__(self, path: str, cookies: str = "") -> None:
        self.path = path
        self.headers = Message()
        if cookies:
            self.headers["Cookie"] = cookies
        self.rfile = BytesIO(b"")
        self.wfile = BytesIO()
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = int(status)

    def send_response_only(self, status):
        self.status = int(status)

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def address_string(self):
        return "127.0.0.1"

    def log_message(self, *a, **k):
        pass

    def header_values(self, name):
        return [v for n, v in self.sent_headers if n.lower() == name.lower()]


class _TrackingReader(BytesIO):
    def __init__(self, body: bytes, *, fail: bool = False) -> None:
        super().__init__(body)
        self.fail = fail
        self.read_calls: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if self.fail:
            raise OSError("request body read failed")
        return super().read(size)


def _patch_discovery(monkeypatch, *, speakers, grouping=None, airplay_fit=None,
                     self_name="JTS",
                     self_hostname="jts-living.local", self_room="living",
                     self_addrs=frozenset({"192.168.1.5"}),
                     peering=None):
    """Pin self identity + discovery so the page is deterministic and never
    touches the real network or /var/lib/jasper files.

    Name / room / hostname now come from ONE identity read in
    _build_rooms_payload (rooms_setup.identity.read_identity), so we patch
    that reader directly rather than the deleted per-field _self_* helpers.
    Address is NIC-derived (not identity), so self_addresses / _self_address
    stay patched separately. The wake-response (peering) block is stubbed too
    so a test never reads the real /var/lib/jasper/peering.env — default
    off/off, overridable via `peering=`."""
    fake_identity = rooms_setup.identity.SpeakerIdentity(
        name=self_name, room=self_room, hostname=self_hostname, peer_id="test-peer-id",
    )
    monkeypatch.setattr(rooms_setup.identity, "read_identity", lambda: fake_identity)
    monkeypatch.setattr(
        rooms_setup, "_read_peering_block",
        lambda: dict(peering if peering is not None
                     else {"enabled": False, "primary": False}),
    )
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set(self_addrs))
    monkeypatch.setattr(
        rooms_setup, "_self_address",
        lambda known=None: next(iter(sorted(self_addrs)), ""),
    )
    monkeypatch.setattr(
        rooms_setup, "read_grouping_state",
        lambda *a, **k: dict(grouping if grouping is not None else _OFF_GROUPING),
    )
    # The airplay-fit composer reads the real /var/lib/jasper files; stub it so
    # the page stays hermetic. Default {applicable: False}; pass airplay_fit= to
    # simulate an active bonded leader (e.g. a tight regime).
    _fit = dict(airplay_fit) if airplay_fit is not None else {"applicable": False}
    monkeypatch.setattr(
        rooms_setup, "with_airplay_latency_fit",
        lambda g: g if not isinstance(g, dict) else {**g, "airplay_latency_fit": _fit},
    )
    monkeypatch.setattr(rooms_setup, "_discover_speakers", lambda *a, **k: list(speakers))
    # _build_rooms_payload calls discover_speakers_cached(), which memoizes
    # the browse in a module-level TTL cache. Reset it per test so the cache
    # can't leak one test's speakers into the next (and so the patched
    # _discover_speakers above is what actually gets read).
    rooms_setup._disc_cache.update(at=0.0, result=[])


def _get(path: str):
    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler(path)
    handler_cls.do_GET(h)
    return h


# ----------------------------------------------------------------------
# GET / — the canonical, client-rendered shell.
# ----------------------------------------------------------------------


def test_get_root_renders_canonical_document(monkeypatch):
    _patch_discovery(monkeypatch, speakers=[])
    h = _get("/")
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    assert h.header_values("Content-Type") == ["text/html; charset=utf-8"]


def test_get_root_has_shared_app_header(monkeypatch):
    # The combined directory + wake-response surface is titled "Speakers"
    # ("my other speakers" is one household concern) — /rooms/ is canonical.
    _patch_discovery(monkeypatch, speakers=[])
    out = _get("/").wfile.getvalue().decode()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Speakers</h1>' in out
    assert '<use href="#icon-back"></use>' in out


def test_get_root_embeds_csrf_meta(monkeypatch):
    # Read-only page, but canonical_page still emits the CSRF meta tag so the
    # ES module can read it from <meta name="jts-csrf"> if a future POST lands.
    _patch_discovery(monkeypatch, speakers=[])
    out = _get("/").wfile.getvalue().decode()
    assert 'meta name="jts-csrf"' in out


def test_get_root_links_page_css_and_loads_es_module(monkeypatch):
    _patch_discovery(monkeypatch, speakers=[])
    out = _get("/").wfile.getvalue().decode()
    # Page-specific CSS is a cache-busted static link, never inline app.css.
    assert "/assets/rooms/rooms.css?v=" in out
    # Behaviour ships as a static ES module loaded by a type=module tag.
    assert '<script type="module" src="/assets/rooms/js/main.js"></script>' in out
    # The mount point the module clears on first render.
    assert 'id="app"' in out


def test_rooms_module_keeps_pair_hosts_local_not_raw_ip():
    js = (_REPO / "deploy" / "assets" / "rooms" / "js" / "main.js").read_text(
        encoding="utf-8"
    )

    assert 'import { localWebHost } from "/assets/shared/js/local-web-host.js";' in js
    assert "function localWebHost" not in js
    assert "IPV4_RE" not in js
    assert 'defRow("Leader", leaderHost || "leader")' in js
    assert 'h("code.bond-current__addr", null, leaderHost)' in js
    assert 'h("code.bond-current__addr", null, g.leader_addr)' not in js


def test_rooms_balance_slider_saves_on_input_not_only_release():
    js = (_REPO / "deploy" / "assets" / "rooms" / "js" / "main.js").read_text(
        encoding="utf-8"
    )

    assert "const BALANCE_LIVE_COMMIT_MS = 150;" in js
    assert 'import { createPairBalanceController } from "./pair-balance-controller.js";' in js
    assert "createPairBalanceController({" in js
    assert 'postTrim: (request) => postJSON("trim", request),' in js
    assert 'balanceRange.addEventListener("input", () => {' in js
    input_handler = js.split('balanceRange.addEventListener("input", () => {', 1)[1]
    input_handler = input_handler.split("});", 1)[0]
    assert "balanceController.input(balanceRange.value);" in input_handler
    assert 'balanceRange.addEventListener("change", () => {' in js
    assert "void balanceController.change();" in js


def test_get_root_shell_interpolates_no_discovered_data(monkeypatch):
    """The server-rendered HTML carries NO peer/grouping data — every untrusted
    field is delivered over /rooms.json for the module to render with DOM/text
    APIs. A hostile peer therefore has no server-side HTML-injection surface.
    """
    hostile = {
        "name": "<script>alert(1)</script>",
        "room": "<img src=x onerror=alert(2)>",
        "address": "10.0.0.7",
        "port": 8780,
    }
    _patch_discovery(
        monkeypatch,
        speakers=[hostile],
        grouping={**_OFF_GROUPING, "enabled": True, "role": "leader",
                  "channel": "left", "bond_id": "ZZBONDZZ", "error": None},
    )
    out = _get("/").wfile.getvalue().decode()
    # No raw peer markup, and no escaped-but-present copy either: the shell
    # simply never receives this data.
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)" not in out
    assert "onerror=alert(2)" not in out
    assert "10.0.0.7" not in out
    assert "ZZBONDZZ" not in out  # not even the (trusted) grouping bond id


def test_get_root_does_not_open_a_socket_for_discovery(monkeypatch):
    """GET / must render from the static shell only — it must NOT trigger the
    mDNS browse (that work is deferred to the module's /rooms.json fetch). A
    page open should never block on a 2 s discovery scan."""
    calls = {"discover": 0}

    def _boom(*a, **k):
        calls["discover"] += 1
        raise AssertionError("GET / must not run discovery")

    monkeypatch.setattr(rooms_setup, "_discover_speakers", _boom)
    h = _get("/")
    assert h.status == 200
    assert calls["discover"] == 0


# ----------------------------------------------------------------------
# GET /rooms.json — the data the module fetches.
# ----------------------------------------------------------------------


def test_rooms_json_is_application_json(monkeypatch):
    _patch_discovery(monkeypatch, speakers=[])
    h = _get("/rooms.json")
    assert h.status == 200
    assert h.header_values("Content-Type") == ["application/json"]
    body = h.wfile.getvalue()
    assert h.header_values("Content-Length") == [str(len(body))]
    # No-store: the directory is live state, never a cached snapshot.
    assert "no-store" in (h.header_values("Cache-Control") or [""])[0]


def test_rooms_json_shape(monkeypatch):
    _patch_discovery(
        monkeypatch,
        speakers=[
            {"name": "jts-bedroom", "room": "bedroom",
             "hostname": "jts-bedroom", "address": "192.168.1.9",
             "port": 8780},
        ],
        grouping=dict(_OFF_GROUPING),
    )
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert sorted(data.keys()) == ["peers", "self", "view"]

    # self: name / hostname / room / address / grouping (the read_grouping_state
    # dict) / peering (the wake-response {enabled, primary} block).
    s = data["self"]
    assert sorted(s.keys()) == [
        "address", "grouping", "hostname", "name", "peering", "room",
    ]
    assert s["name"] == "JTS"  # unset speaker name -> built-in default
    assert s["hostname"] == "jts-living.local"
    assert s["room"] == "living"
    assert s["address"] == "192.168.1.5"
    # grouping = the read_grouping_state dict + the airplay-fit composer's key
    # ({applicable: false} on a solo speaker).
    assert s["grouping"] == {**_OFF_GROUPING, "airplay_latency_fit": {"applicable": False}}
    # peering defaults to off/off when no peering.env exists (patched empty here).
    assert s["peering"] == {"enabled": False, "primary": False}

    # peers: name / room / address / home_url / system_url. Raw address stays
    # for backend control fan-out; browser links use the stable .local host.
    assert len(data["peers"]) == 1
    p = data["peers"][0]
    assert sorted(p.keys()) == ["address", "home_url", "name", "room", "system_url"]
    assert p["name"] == "jts-bedroom"
    assert p["room"] == "bedroom"
    assert p["address"] == "192.168.1.9"
    assert p["home_url"] == "http://jts-bedroom.local/"
    assert p["system_url"] == "http://jts-bedroom.local/system/"
    assert data["view"] == {
        "state": "solo",
        "bonded": False,
        "can_create_pair": True,
        "can_balance_pair": False,
        "show_subwoofer_controls": False,
    }


def test_rooms_json_logs_slow_snapshot_timing(monkeypatch):
    _patch_discovery(monkeypatch, speakers=[], grouping=dict(_OFF_GROUPING))
    events = []

    def fake_log_event(logger, event, **fields):
        events.append((logger, event, fields))

    monkeypatch.setattr(rooms_setup, "ROOMS_SNAPSHOT_SLOW_MS", 0)
    monkeypatch.setattr(rooms_setup, "log_event", fake_log_event)

    h = _get("/rooms.json")
    assert h.status == 200
    assert events
    _logger, event, fields = events[-1]
    assert event == "rooms.snapshot"
    assert fields["peer_count"] == 0
    assert {
        "identity_ms",
        "self_addr_ms",
        "grouping_ms",
        "balance_ms",
        "peering_ms",
        "discovery_ms",
        "total_ms",
    } <= set(fields)


def test_rooms_json_carries_tight_airplay_fit_for_a_bonded_leader(monkeypatch):
    """An active bonded leader in the tight regime surfaces airplay_latency_fit
    on /rooms.json so the bond card can warn (the rooms JS renders it)."""
    leader = {**_OFF_GROUPING, "enabled": True, "role": "leader", "bond_id": "lr"}
    _patch_discovery(
        monkeypatch, speakers=[], grouping=leader,
        airplay_fit={
            "applicable": True, "tight": True, "residual_lag_sec": 0.55,
            "buffer_ms": 400, "budget_sec": 0.36, "need_sec": 0.55,
        },
    )
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    fit = data["self"]["grouping"]["airplay_latency_fit"]
    assert fit["applicable"] is True
    assert fit["tight"] is True
    assert fit["residual_lag_sec"] == 0.55


def test_rooms_json_carries_live_pair_balance_snapshot(monkeypatch):
    """A left/right bond includes the peer's live trim so the slider does not
    guess or display stale peer state."""
    self_g = {
        **_OFF_GROUPING,
        "enabled": True,
        "role": "leader",
        "channel": "left",
        "bond_id": "b",
        "trim_db": -3.0,
    }
    peer_g = {
        **_OFF_GROUPING,
        "enabled": True,
        "role": "follower",
        "channel": "right",
        "bond_id": "b",
        "leader_addr": "jts.local",
        "trim_db": 0.0,
    }
    _patch_discovery(
        monkeypatch,
        speakers=[{
            "name": "Right", "room": "", "hostname": "jts3",
            "address": "192.168.1.9", "port": 8780,
        }],
        grouping=self_g,
    )
    monkeypatch.setattr(
        rooms_setup, "_get_member_grouping",
        lambda a, known=None, *, timeout=rooms_setup.CONTROL_HTTP_TIMEOUT_SEC: (
            dict(peer_g) if a == "192.168.1.9" else None
        ),
    )

    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    balance = data["self"]["grouping"]["balance"]

    assert balance["applicable"] is True
    assert balance["ok"] is True
    assert balance["left_trim_db"] == -3.0
    assert balance["right_trim_db"] == 0.0
    assert balance["balance_db"] == 3.0
    assert balance["peer_addr"] == "192.168.1.9"
    assert data["view"]["state"] == "paired"
    assert data["view"]["bonded"] is True
    assert data["view"]["can_balance_pair"] is True
    assert data["view"]["show_subwoofer_controls"] is False


def test_rooms_json_balance_snapshot_uses_short_peer_read(monkeypatch):
    """The balance display is read-only and polled every 7 s, so an offline peer
    must not make the whole Speakers page wait on the full mutation timeout."""
    self_g = {
        **_OFF_GROUPING,
        "enabled": True,
        "role": "leader",
        "channel": "left",
        "bond_id": "b",
        "peer_addr": "192.168.1.9",
        "peer_name": "Right",
        "trim_db": 0.0,
    }
    peer_g = {
        **_OFF_GROUPING,
        "enabled": True,
        "role": "follower",
        "channel": "right",
        "bond_id": "b",
        "leader_addr": "jts.local",
        "trim_db": -2.0,
    }
    timeouts: list[float] = []
    _patch_discovery(monkeypatch, speakers=[], grouping=self_g)

    def fake_get_grouping(
        addr, known=None, *, timeout=rooms_setup.CONTROL_HTTP_TIMEOUT_SEC,
    ):
        timeouts.append(timeout)
        return dict(peer_g) if addr == "192.168.1.9" else None

    monkeypatch.setattr(rooms_setup, "_get_member_grouping", fake_get_grouping)

    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())

    assert data["self"]["grouping"]["balance"]["ok"] is True
    assert timeouts == [rooms_setup.BALANCE_SNAPSHOT_PEER_TIMEOUT_SEC]


def test_rooms_json_peer_links_never_fall_back_to_raw_ip(monkeypatch):
    _patch_discovery(
        monkeypatch,
        speakers=[
            {"name": "Kitchen", "room": "kitchen", "hostname": "",
             "address": "192.168.1.9", "port": 8780},
        ],
    )

    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    p = data["peers"][0]
    assert p["address"] == "192.168.1.9"
    assert p["home_url"] == ""
    assert p["system_url"] == ""


def test_self_exclusion_uses_exact_hostname_not_substring(monkeypatch):
    """Regression (found on hardware): a speaker whose hostname is a SUBSTRING
    of a peer's must NOT drop that peer. `jts` was excluding `jts3` because
    "jts" is a substring of "jts3" — and asymmetrically (jts3 kept jts), which
    is the tell. Self-exclusion must be an EXACT hostname-label match, not a
    substring of the free-form display name."""
    _patch_discovery(
        monkeypatch,
        self_hostname="jts.local",            # our hostname label is "jts"
        self_addrs=frozenset({"192.168.1.74"}),
        speakers=[
            {"name": "JTS3", "hostname": "jts3", "room": "",
             "address": "192.168.1.92", "port": 8780},
        ],
    )
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    names = [p["name"] for p in data["peers"]]
    assert names == ["JTS3"], f"jts must not exclude jts3 as self; got {names}"


def test_self_excluded_by_exact_hostname_when_address_missed(monkeypatch):
    """The hostname fallback still catches self when the route trick missed our
    address (e.g. a loopback/secondary advert): an EXACT hostname-label match
    on a peer not in our address set is dropped."""
    _patch_discovery(
        monkeypatch,
        self_hostname="jts.local",
        self_addrs=frozenset({"192.168.1.74"}),
        speakers=[
            # Same hostname "jts", different address (our own advert the route
            # trick didn't list) -> must be excluded as self.
            {"name": "JTS", "hostname": "jts", "room": "",
             "address": "127.0.1.1", "port": 8780},
            {"name": "JTS3", "hostname": "jts3", "room": "",
             "address": "192.168.1.92", "port": 8780},
        ],
    )
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    names = [p["name"] for p in data["peers"]]
    assert names == ["JTS3"], f"exact-hostname self must drop, jts3 stays; got {names}"


def test_rooms_json_self_has_name_key(monkeypatch):
    """The self block carries a `name` field — the speaker's friendly display
    name (the SAME jasper/speaker_name identity now advertised as the `name=`
    TXT on `_jasper-control._tcp`, see docs/HANDOFF-multiroom.md §6). The
    self-card must show the same name peers see, so this key is part of the
    /rooms.json contract, not just incidental. Pinned separately from
    test_rooms_json_shape so an edit there can't silently drop it.

    The name now flows from the single identity read (read_identity().name);
    we drive it via _patch_discovery's self_name rather than a deleted
    _self_name helper."""
    _patch_discovery(monkeypatch, speakers=[], self_name="Living Room")
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert "name" in data["self"]
    assert data["self"]["name"] == "Living Room"


def test_rooms_json_self_name_defaults_when_unset(monkeypatch):
    """With no speaker name configured, the self `name` resolves to the
    built-in default ('JTS') — never empty/missing — mirroring the
    hostname fallback the `name=` TXT advert uses (control_advert).

    read_identity().name returns 'JTS' when unset; we model that via
    _patch_discovery's default self_name."""
    _patch_discovery(monkeypatch, speakers=[], self_name="JTS")
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert data["self"]["name"] == "JTS"


def test_rooms_json_self_hostname_and_room_flow_from_identity(monkeypatch):
    """Wiring contract: the self block sources name + hostname + room from the
    shared identity reader (jasper.identity.read_identity), read ONCE in
    _build_rooms_payload — NOT ad-hoc env/file reads or per-field helpers.
    Patch read_identity at the source so this genuinely exercises the single
    identity read. `room` now lives in the speaker-identity home — see
    docs/HANDOFF-multiroom.md §6."""
    fake = rooms_setup.identity.SpeakerIdentity(
        name="Sun Room", room="Sunroom", hostname="jts-sun.local", peer_id="pid",
    )
    monkeypatch.setattr(rooms_setup.identity, "read_identity", lambda: fake)
    # Stub only the non-identity bits (network + grouping + own NICs + peering);
    # name / hostname / room all flow from the patched read_identity above.
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_self_address", lambda known=None: "")
    monkeypatch.setattr(rooms_setup, "read_grouping_state", lambda *a, **k: dict(_OFF_GROUPING))
    monkeypatch.setattr(rooms_setup, "_read_peering_block",
                        lambda: {"enabled": False, "primary": False})
    monkeypatch.setattr(rooms_setup, "_discover_speakers", lambda *a, **k: [])
    rooms_setup._disc_cache.update(at=0.0, result=[])

    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert data["self"]["name"] == "Sun Room"
    assert data["self"]["hostname"] == "jts-sun.local"
    assert data["self"]["room"] == "Sunroom"


def test_rooms_json_excludes_self_by_address(monkeypatch):
    """A discovered instance whose address is one of our own NIC IPs is
    dropped — it's this speaker answering its own browse."""
    _patch_discovery(
        monkeypatch,
        speakers=[
            {"name": "jts-living", "room": "", "address": "192.168.1.5", "port": 8780},
            {"name": "jts-kitchen", "room": "kitchen", "address": "192.168.1.8", "port": 8780},
        ],
        self_addrs=frozenset({"192.168.1.5"}),
    )
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    addrs = [p["address"] for p in data["peers"]]
    assert "192.168.1.5" not in addrs
    assert addrs == ["192.168.1.8"]


def test_rooms_json_excludes_self_by_hostname_label(monkeypatch):
    """When the route trick missed our own address, self is still dropped by an
    EXACT match of the advert's SRV hostname label against ours — and crucially
    by hostname, NOT by the free-form display name (which here is "Living Room",
    nothing like the hostname)."""
    _patch_discovery(
        monkeypatch,
        speakers=[
            # Our OWN advert on an address the route trick didn't list: same
            # hostname "jts-living", a friendly display name -> excluded as self.
            {"name": "Living Room", "hostname": "jts-living", "room": "",
             "address": "192.168.1.99", "port": 8780},
            {"name": "jts-bedroom", "hostname": "jts-bedroom", "room": "bedroom",
             "address": "192.168.1.9", "port": 8780},
        ],
        self_hostname="jts-living.local",
        self_addrs=frozenset({"192.168.1.5"}),  # does NOT include .99
    )
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    names = [p["name"] for p in data["peers"]]
    assert names == ["jts-bedroom"]


def test_rooms_json_hostile_peer_name_is_a_json_string_not_markup(monkeypatch):
    """A hostile mDNS name survives only as a JSON *string value*, never as
    structural markup. The transport defence is the content type
    (`application/json`, asserted in test_rooms_json_is_application_json) plus
    the module rendering every value through createTextNode; this test pins
    that the server doesn't leak the hostile string into the JSON *structure*
    (it stays a quoted scalar that round-trips through json.loads unchanged).
    """
    _patch_discovery(
        monkeypatch,
        speakers=[
            {"name": "</script><script>alert(1)</script>", "room": "<b>x</b>",
             "address": "10.0.0.2", "port": 8780},
        ],
    )
    raw = _get("/rooms.json").wfile.getvalue().decode()
    # Parses as JSON with the hostile bytes confined to a string value — no
    # markup escaped into the document structure.
    data = json.loads(raw)
    assert isinstance(data["peers"], list) and len(data["peers"]) == 1
    p = data["peers"][0]
    assert p["name"] == "</script><script>alert(1)</script>"
    assert p["room"] == "<b>x</b>"
    # The hostile chars live inside the quoted "name" value, not as a sibling
    # key/structure — i.e. they didn't break out of the string literal. (The
    # self block also carries a "name" key, so count peer rows, not raw bytes.)
    assert sum(1 for q in data["peers"] if "name" in q) == 1


def test_rooms_json_fail_loud_grouping_error_passes_through(monkeypatch):
    """An enabled-but-invalid grouping config keeps enabled=True and surfaces
    its `error` string (the fail-LOUD path) verbatim in /rooms.json."""
    broken = {
        **_OFF_GROUPING,
        "enabled": True,
        "role": "follower",
        "channel": "left",
        "bond_id": "b1",
        "leader_addr": "",
        "error": "JASPER_GROUPING_LEADER_ADDR is empty for role=follower",
    }
    _patch_discovery(monkeypatch, speakers=[], grouping=broken)
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert data["self"]["grouping"]["enabled"] is True
    assert data["self"]["grouping"]["error"] == (
        "JASPER_GROUPING_LEADER_ADDR is empty for role=follower"
    )
    assert data["view"]["state"] == "degraded"
    assert data["view"]["bonded"] is True


def test_rooms_json_empty_when_no_siblings(monkeypatch):
    _patch_discovery(monkeypatch, speakers=[])
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert data["peers"] == []
    # self still renders (the page is useful with zero peers).
    assert data["self"]["hostname"] == "jts-living.local"
    assert data["view"]["can_create_pair"] is False


def _raise_run(*a, **k):
    """asyncio.run replacement that raises — but first closes the coroutine it
    was handed so pytest doesn't warn 'coroutine was never awaited'. The real
    _discover_speakers passes the live _browse() coroutine as the first arg."""
    for arg in a:
        if asyncio.iscoroutine(arg):
            arg.close()
    raise RuntimeError("zeroconf exploded")


def test_discover_speakers_swallows_failure(monkeypatch):
    """Discovery is best-effort: if the mDNS browse raises, the real
    _discover_speakers must degrade to an empty list (so /rooms.json renders an
    empty directory, never 500s). Simulated at the asyncio.run boundary."""
    monkeypatch.setattr(rooms_setup.asyncio, "run", _raise_run)
    assert rooms_setup._discover_speakers() == []


def test_rooms_json_renders_empty_directory_when_discovery_fails(monkeypatch):
    """End-to-end: a failing browse leaves /rooms.json with self present and an
    empty peer list — the page degrades, it does not error."""
    fake = rooms_setup.identity.SpeakerIdentity(
        name="JTS", room="default", hostname="jts.local", peer_id="pid",
    )
    monkeypatch.setattr(rooms_setup.identity, "read_identity", lambda: fake)
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_self_address", lambda known=None: "")
    monkeypatch.setattr(rooms_setup, "read_grouping_state", lambda *a, **k: dict(_OFF_GROUPING))
    monkeypatch.setattr(rooms_setup, "_read_peering_block",
                        lambda: {"enabled": False, "primary": False})
    monkeypatch.setattr(rooms_setup.asyncio, "run", _raise_run)
    h = _get("/rooms.json")
    assert h.status == 200
    data = json.loads(h.wfile.getvalue().decode())
    assert data["peers"] == []
    assert data["self"]["hostname"] == "jts.local"


# ----------------------------------------------------------------------
# Write scope — the wake-response (peering) toggle is the ONE write
# surface. The bond-forming controls are still deferred (they'd no-op
# until the P1 sync engine exists); the only POST is /peering.
# ----------------------------------------------------------------------


def test_handler_has_do_post_for_peering():
    """Two write paths now: POST /peering (the wake-response toggle) and
    POST /bond (the bond-forming one-flow that fans config out to members)."""
    handler_cls = rooms_setup._make_handler()
    assert hasattr(handler_cls, "do_POST")


def test_unknown_get_route_404s(monkeypatch):
    _patch_discovery(monkeypatch, speakers=[])
    h = _get("/does-not-exist")
    assert h.status == 404


# ----------------------------------------------------------------------
# POST /peering — the wake-response write surface.
#
# Reuses jasper.peering.config's env file + readers; this suite pins the wiring
# (route-check before CSRF, read-modify-write preserving JASPER_PEER_ROOM,
# the daemon restarts, the response shape) without touching real
# /var/lib/jasper files.
# ----------------------------------------------------------------------


def _post(path: str, body: bytes, *, csrf_ok: bool, monkeypatch):
    """Drive do_POST with the CSRF guard stubbed and the daemon restarts
    captured. Returns (handler, restarts_dict)."""
    handler_cls = rooms_setup._make_handler()
    restarts = {"voice": 0, "control": 0}
    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: csrf_ok)
    monkeypatch.setattr(rooms_setup, "reject_csrf",
                        lambda h: h.send_response(403) or h.end_headers())
    monkeypatch.setattr(rooms_setup, "restart_voice_daemon",
                        lambda: restarts.__setitem__("voice", restarts["voice"] + 1))
    monkeypatch.setattr(
        rooms_setup, "restart_systemd_units",
        lambda *units: (
            restarts.__setitem__("control", restarts["control"] + 1)
            if units == ("jasper-control",) else None
        ),
    )
    h = _FakeHandler(path)
    h.headers["Content-Length"] = str(len(body))
    h.rfile = BytesIO(body)
    handler_cls.do_POST(h)
    return h, restarts


def _seed_peering_env(tmp_path, monkeypatch, text):
    envp = tmp_path / "peering.env"
    envp.write_text(text)
    monkeypatch.setattr(rooms_setup.peering_config, "PEERING_ENV_FILE", str(envp))
    return envp


def test_post_peering_unknown_path_404s_before_csrf(monkeypatch):
    """Route-check runs BEFORE the CSRF guard (project convention): a bogus
    POST path 404s without revealing CSRF state. The stub raises if CSRF is
    even consulted on the wrong path."""
    def _boom(*_a, **_k):
        raise AssertionError("CSRF guard must not run on an unknown POST path")

    monkeypatch.setattr(rooms_setup, "guard_mutating_request", _boom)
    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/not-peering")
    h.headers["Content-Length"] = "2"
    h.rfile = BytesIO(b"{}")
    handler_cls.do_POST(h)
    assert h.status == 404


def test_post_peering_rejects_bad_csrf(monkeypatch, tmp_path):
    _seed_peering_env(tmp_path, monkeypatch, "JASPER_PEERING=off\n")
    h, restarts = _post("/peering", b'{"enabled":true,"primary":false}',
                        csrf_ok=False, monkeypatch=monkeypatch)
    assert h.status == 403
    assert restarts == {"voice": 0, "control": 0}  # no write, no restart


@pytest.mark.parametrize(
    ("body", "content_length", "expected_error", "expected_reads"),
    [
        (b"{", 1, "invalid JSON body", [1]),
        (b"\xff", 1, "invalid JSON body", [1]),
        (b"[]", 2, "body must be a JSON object", [2]),
        (b"{}", 3, "invalid JSON body", [3]),
        (b"{}", "invalid", "invalid Content-Length", []),
        (b"{}", -1, "invalid body length", []),
        (b"{}", rooms_setup._PEERING_BODY_LIMIT + 1, "invalid body length", []),
    ],
)
def test_post_peering_rejects_invalid_json_framing_without_mutation(
    monkeypatch,
    body,
    content_length,
    expected_error,
    expected_reads,
):
    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *_a, **_k: True)
    monkeypatch.setattr(
        rooms_setup,
        "write_env_file",
        lambda *_a, **_k: pytest.fail("invalid request must not write config"),
    )
    monkeypatch.setattr(
        rooms_setup,
        "restart_voice_daemon",
        lambda: pytest.fail("invalid request must not restart daemons"),
    )
    handler_cls = rooms_setup._make_handler()
    handler = _FakeHandler("/peering")
    handler.headers["Content-Length"] = str(content_length)
    handler.rfile = _TrackingReader(body)

    handler_cls.do_POST(handler)

    assert handler.status == 400
    payload = json.loads(handler.wfile.getvalue())
    assert payload["ok"] is False
    if expected_error == "invalid JSON body":
        assert payload["error"].startswith(expected_error)
    else:
        assert payload["error"] == expected_error
    assert handler.rfile.read_calls == expected_reads


def test_post_peering_request_body_oserror_remains_distinct(monkeypatch):
    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *_a, **_k: True)
    monkeypatch.setattr(
        rooms_setup,
        "write_env_file",
        lambda *_a, **_k: pytest.fail("failed read must not write config"),
    )
    handler_cls = rooms_setup._make_handler()
    handler = _FakeHandler("/peering")
    handler.headers["Content-Length"] = "2"
    handler.rfile = _TrackingReader(b"{}", fail=True)

    with pytest.raises(OSError, match="request body read failed"):
        handler_cls.do_POST(handler)

    assert handler.status is None


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/bond",
            b'{"members":[{"addr":"","role":"leader","channel":"left"}],'
            b'"bond_id":"bond-1"}',
        ),
        ("/trim", b'{"target":"pair","balance_db":1.5}'),
        ("/mains-highpass", b'{"enabled":true}'),
    ],
)
def test_grouping_routes_reject_incomplete_json_before_state_or_control_mutation(
    monkeypatch,
    path,
    body,
):
    """Every grouping route sharing the reader rejects a short request body.

    The payloads are otherwise valid for their route, so the sentinels would
    observe credential/state/control work if an incomplete body were accepted.
    """
    effects: list[str] = []

    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *_a, **_k: True)
    monkeypatch.setattr(
        rooms_setup.household_credential,
        "ensure",
        lambda: effects.append("household_credential.ensure"),
    )
    monkeypatch.setattr(
        rooms_setup,
        "_fan_out_grouping",
        lambda targets, **_k: (
            effects.append("_fan_out_grouping")
            or [(True, "HTTP 200")] * len(targets)
        ),
    )
    monkeypatch.setattr(
        rooms_setup,
        "_set_pair_balance",
        lambda *_a, **_k: effects.append("_set_pair_balance"),
    )
    monkeypatch.setattr(
        rooms_setup,
        "read_grouping_state",
        lambda: effects.append("read_grouping_state") or {
            "enabled": True,
            "role": "leader",
            "channel": "left",
            "bond_id": "bond-1",
            "leader_addr": "",
            "subwoofer_present": True,
            "roster": [{"addr": "192.168.1.9", "channel": "sub"}],
        },
    )
    monkeypatch.setattr(
        rooms_setup,
        "post_grouping_to_member",
        lambda *_a, **_k: (
            effects.append("post_grouping_to_member") or (True, "HTTP 200")
        ),
    )
    monkeypatch.setattr(
        rooms_setup,
        "write_env_file",
        lambda *_a, **_k: effects.append("write_env_file"),
    )
    monkeypatch.setattr(
        rooms_setup,
        "restart_voice_daemon",
        lambda: effects.append("restart_voice_daemon"),
    )
    monkeypatch.setattr(
        rooms_setup,
        "restart_systemd_units",
        lambda *_a: effects.append("restart_systemd_units"),
    )

    handler_cls = rooms_setup._make_handler()
    handler = _FakeHandler(path)
    declared_length = len(body) + 1
    handler.headers["Content-Length"] = str(declared_length)
    handler.rfile = _TrackingReader(body)

    handler_cls.do_POST(handler)

    assert handler.status == 400
    assert json.loads(handler.wfile.getvalue()) == {
        "ok": False,
        "error": "invalid JSON body",
    }
    assert handler.rfile.read_calls == [declared_length]
    assert effects == []


def test_post_peering_enables_and_preserves_room(monkeypatch, tmp_path):
    """Turning peering on read-modify-writes peering.env: JASPER_PEERING flips
    to on, JASPER_PEER_PRIMARY is set, and JASPER_PEER_ROOM (owned by
    /speaker/) plus operator tuning knobs are PRESERVED."""
    envp = _seed_peering_env(
        tmp_path, monkeypatch,
        "JASPER_PEERING=off\nJASPER_PEER_ROOM=kitchen\n"
        "JASPER_PEER_ARB_WINDOW_MS=250\n",
    )
    h, restarts = _post("/peering", b'{"enabled":true,"primary":true}',
                        csrf_ok=True, monkeypatch=monkeypatch)
    assert h.status == 200
    assert json.loads(h.wfile.getvalue()) == {
        "ok": True, "peering": {"enabled": True, "primary": True},
    }
    # Both daemons restart so they pick up the new mode.
    assert restarts == {"voice": 1, "control": 1}
    txt = envp.read_text()
    assert "JASPER_PEERING=on" in txt
    assert "JASPER_PEER_PRIMARY=1" in txt
    assert "JASPER_PEER_ROOM=kitchen" in txt          # preserved
    assert "JASPER_PEER_ARB_WINDOW_MS=250" in txt     # operator knob preserved


def test_post_peering_primary_off_drops_key_keeps_room(monkeypatch, tmp_path):
    envp = _seed_peering_env(
        tmp_path, monkeypatch,
        "JASPER_PEERING=on\nJASPER_PEER_PRIMARY=1\nJASPER_PEER_ROOM=den\n",
    )
    h, _ = _post("/peering", b'{"enabled":true,"primary":false}',
                 csrf_ok=True, monkeypatch=monkeypatch)
    assert h.status == 200
    txt = envp.read_text()
    assert "JASPER_PEER_PRIMARY" not in txt   # dropped, not written =0
    assert "JASPER_PEER_ROOM=den" in txt      # preserved


def test_post_peering_disable_writes_off(monkeypatch, tmp_path):
    envp = _seed_peering_env(
        tmp_path, monkeypatch,
        "JASPER_PEERING=on\nJASPER_PEER_PRIMARY=1\nJASPER_PEER_ROOM=den\n",
    )
    h, _ = _post("/peering", b'{"enabled":false,"primary":false}',
                 csrf_ok=True, monkeypatch=monkeypatch)
    assert h.status == 200
    assert json.loads(h.wfile.getvalue())["peering"] == {
        "enabled": False, "primary": False,
    }
    txt = envp.read_text()
    assert "JASPER_PEERING=off" in txt
    assert "JASPER_PEER_ROOM=den" in txt


def test_post_peering_rejects_malformed_body(monkeypatch, tmp_path):
    """A non-JSON body is a 400 and writes nothing / restarts nothing."""
    envp = _seed_peering_env(tmp_path, monkeypatch, "JASPER_PEERING=off\n")
    h, restarts = _post("/peering", b"not json at all",
                        csrf_ok=True, monkeypatch=monkeypatch)
    assert h.status == 400
    assert restarts == {"voice": 0, "control": 0}
    assert envp.read_text() == "JASPER_PEERING=off\n"  # untouched


def test_post_peering_reuses_peering_config_constants():
    """Wiring contract: rooms_setup imports jasper.peering.config and writes
    through its PEERING_ENV_FILE constant — it does NOT re-derive the env
    path or parse logic."""
    assert rooms_setup.peering_config.PEERING_ENV_FILE.endswith("peering.env")
    assert callable(rooms_setup.peering_config.read_state)
    assert callable(rooms_setup.peering_config.state_enabled)


def test_rooms_json_peering_block_reflects_env(monkeypatch, tmp_path):
    """The /rooms.json self.peering block is read FRESH from peering.env via
    jasper.peering.config — an on/primary file shows on/primary."""
    _seed_peering_env(
        tmp_path, monkeypatch,
        "JASPER_PEERING=on\nJASPER_PEER_PRIMARY=1\n",
    )
    # Patch the rest of the payload deterministically, but let _read_peering_block
    # run for real against the seeded temp env (do NOT stub it here).
    fake = rooms_setup.identity.SpeakerIdentity(
        name="JTS", room="r", hostname="jts.local", peer_id="pid",
    )
    monkeypatch.setattr(rooms_setup.identity, "read_identity", lambda: fake)
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_self_address", lambda known=None: "")
    monkeypatch.setattr(rooms_setup, "read_grouping_state", lambda *a, **k: dict(_OFF_GROUPING))
    monkeypatch.setattr(rooms_setup, "_discover_speakers", lambda *a, **k: [])
    rooms_setup._disc_cache.update(at=0.0, result=[])
    # Clear any ambient env so peering_config's os.environ fallthrough can't lie.
    for k in ("JASPER_PEERING", "JASPER_PEER_PRIMARY"):
        monkeypatch.delenv(k, raising=False)

    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert data["self"]["peering"] == {"enabled": True, "primary": True}


# ----------------------------------------------------------------------
# Public module surface — other agents / __main__ wiring depend on these.
# ----------------------------------------------------------------------


def test_public_surface_is_stable():
    assert callable(rooms_setup.make_server)
    assert callable(rooms_setup.main)
    assert callable(rooms_setup._make_handler)
    assert callable(rooms_setup._build_rooms_payload)
    assert callable(rooms_setup._discover_speakers)


def test_make_server_binds_a_tuple_target():
    """make_server((host, port)) returns a live server bound to an ephemeral
    port; the rooms handler is its RequestHandlerClass."""
    server = rooms_setup.make_server(("127.0.0.1", 0))
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] != 0  # OS assigned a real port
        assert hasattr(server.RequestHandlerClass, "do_GET")
        # The combined surface adds POST /peering (the wake-response toggle).
        assert hasattr(server.RequestHandlerClass, "do_POST")
    finally:
        server.server_close()


# ----------------------------------------------------------------------
# Wiring contract — the names/paths/ports other agents + tests depend on.
# ----------------------------------------------------------------------


def test_default_port_and_env_var_are_pinned():
    # Honored exactly by deploy/jasper-web.socket, nginx, and __main__.py.
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int,
                   default=int(__import__("os").environ.get("JASPER_ROOMS_WEB_PORT", "8785")))
    assert p.parse_args([]).port == 8785


def test_wizard_registered_in_main_with_correct_route_env_port():
    from jasper.web import __main__ as web_main

    rooms = [s for s in web_main.WIZARD_SPECS if s.label == "/rooms"]
    assert len(rooms) == 1
    spec = rooms[0]
    assert spec.env_var == "JASPER_ROOMS_WEB_PORT"
    assert spec.default_port == 8785
    assert spec.make_server is web_main._make_rooms_server


def test_socket_unit_has_rooms_listenstream():
    socket_text = (_REPO / "deploy" / "jasper-web.socket").read_text()
    assert "ListenStream=127.0.0.1:8785" in socket_text


def test_nginx_proxies_rooms_to_8785():
    nginx_text = (_REPO / "deploy" / "nginx-jasper.conf").read_text()
    assert "location /rooms/ {" in nginx_text
    assert "proxy_pass http://127.0.0.1:8785/;" in nginx_text


def test_nginx_has_no_peers_route():
    """No legacy /peers redirect/page remains; /rooms is the only surface."""
    for conf in ("nginx-jasper.conf", "nginx-jasper-streambox.conf"):
        nginx_text = (_REPO / "deploy" / conf).read_text()
        assert "/peers" not in nginx_text
        assert "8776" not in nginx_text


# --- peer label derivation -------------------------------------------------
# A speaker with no name= TXT must NOT surface the verbose default mDNS
# instance name ("JTS jasper-control on jts") as its directory label.

def test_peer_label_prefers_txt_name():
    label = rooms_setup._peer_label(
        {"name": "Living Room"}, "jts.local.",
        "JTS jasper-control on jts._jasper-control._tcp.local.",
    )
    assert label == "Living Room"


def test_peer_label_falls_back_to_hostname_not_instance_name():
    # No TXT name → use the resolved SRV host, stripped to a clean label.
    label = rooms_setup._peer_label(
        {}, "jts3.local.",
        "JTS jasper-control on jts3._jasper-control._tcp.local.",
    )
    assert label == "jts3"


def test_peer_label_last_resort_is_stripped_instance_name():
    # No TXT, no server host → the stripped instance name (still better
    # than the full service-qualified string).
    label = rooms_setup._peer_label(
        {}, "", "Kitchen._jasper-control._tcp.local.",
    )
    assert label == "Kitchen"


def test_hostname_label_strips_local_suffix_and_dot():
    assert rooms_setup._hostname_label("jts3.local.") == "jts3"
    assert rooms_setup._hostname_label("speaker.local") == "speaker"
    assert rooms_setup._hostname_label("") == ""


# --- discovery cache (no per-poll mDNS churn) ------------------------------
# /rooms.json is polled every 7 s; a fresh zeroconf browse per poll would
# stand up + tear down a multicast listener ~8x/min. The TTL cache must
# collapse repeated calls within the window to a single underlying browse.

def test_discovery_cache_serves_within_ttl_without_rebrowsing(monkeypatch):
    calls = {"n": 0}

    def _counting_browse(*a, **k):
        calls["n"] += 1
        return [{"name": "jts3", "room": "", "address": "192.168.1.9", "port": 8780}]

    monkeypatch.setattr(rooms_setup, "_discover_speakers", _counting_browse)
    rooms_setup._disc_cache.update(at=0.0, result=[])

    first = rooms_setup.discover_speakers_cached()
    second = rooms_setup.discover_speakers_cached()
    third = rooms_setup.discover_speakers_cached()

    assert calls["n"] == 1, "cache must not re-browse within the TTL"
    assert first == second == third


def test_discovery_cache_rebrowses_after_ttl_expiry(monkeypatch):
    calls = {"n": 0}

    def _counting_browse(*a, **k):
        calls["n"] += 1
        return [{"name": "jts3", "room": "", "address": "192.168.1.9", "port": 8780}]

    monkeypatch.setattr(rooms_setup, "_discover_speakers", _counting_browse)
    rooms_setup._disc_cache.update(at=0.0, result=[])

    rooms_setup.discover_speakers_cached()
    # Age the cache past the TTL → next call re-browses.
    rooms_setup._disc_cache["at"] -= rooms_setup.DISCOVERY_CACHE_TTL_SEC + 1
    rooms_setup.discover_speakers_cached()

    assert calls["n"] == 2, "cache must re-browse once the TTL has elapsed"


def test_discovery_cache_empty_result_does_not_poison(monkeypatch):
    # A transient empty browse must not blank the directory for a full TTL —
    # the next call should retry rather than serve the empty result.
    seq = [[], [{"name": "jts3", "room": "", "address": "192.168.1.9", "port": 8780}]]

    def _flaky_browse(*a, **k):
        return seq.pop(0)

    monkeypatch.setattr(rooms_setup, "_discover_speakers", _flaky_browse)
    rooms_setup._disc_cache.update(at=0.0, result=[])

    assert rooms_setup.discover_speakers_cached() == []
    # Empty wasn't cached as fresh, so this re-browses and gets the speaker.
    assert rooms_setup.discover_speakers_cached() == [
        {"name": "jts3", "room": "", "address": "192.168.1.9", "port": 8780}
    ]


# ----------------------------------------------------------------------
# POST /bond — the bond-forming one-flow.
#
# The primary browser flow sends only peer_addr; rooms_setup owns the stereo
# topology and fans the config out SERVER-side to each member's /grouping/set.
# Advanced same-bond edits may still send the member list explicitly. These
# tests pin the orchestration (leader_addr wiring, per-member results, partial
# failure, CSRF, the SSRF guard) with the cross-speaker HTTP call stubbed.
# ----------------------------------------------------------------------


def _post_bond(body, *, csrf_ok=True, monkeypatch, member_results=None):
    """Drive POST /bond with the cross-speaker call stubbed. Returns
    (handler, calls) where calls is the list of (addr, body) fanned out."""
    calls: list[tuple[str, dict]] = []

    def fake_member_post(addr, member_body, known=None, *, token=None, household=None):
        calls.append((addr, member_body))
        if member_results and addr in member_results:
            return member_results[addr]
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", fake_member_post)
    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: csrf_ok)
    monkeypatch.setattr(rooms_setup, "reject_csrf",
                        lambda h: h.send_response(403) or h.end_headers())
    monkeypatch.setattr(rooms_setup, "_self_address", lambda known=None: "192.168.1.5")
    # Hermetic self-address set (the fan-out computes it once for the SSRF
    # guard); empty so no real socket probe / getaddrinfo runs under test.
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    # The follower's leader_addr is now the leader's STABLE mDNS handle
    # (read_identity().hostname), not a NIC IP — stub the helper so the bond
    # tests don't depend on the real identity reader.
    monkeypatch.setattr(rooms_setup, "_leader_handle", lambda: "jts-living.local")

    handler_cls = rooms_setup._make_handler()
    raw = json.dumps(body).encode()
    h = _FakeHandler("/bond")
    h.headers["Content-Length"] = str(len(raw))
    h.rfile = BytesIO(raw)
    handler_cls.do_POST(h)
    return h, calls


def test_bond_forwards_browser_control_token_to_members(monkeypatch):
    """The /rooms/ grouping fan-out runs SERVER-side, so the leader must
    forward the browser-supplied X-JTS-Token to each member's /grouping/set
    for the control-token gate. Captures the token each member call received."""
    seen_tokens: list[str | None] = []

    def capture(addr, member_body, known=None, *, token=None, household=None):
        seen_tokens.append(token)
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", capture)
    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_leader_handle", lambda: "jts-living.local")

    handler_cls = rooms_setup._make_handler()
    raw = json.dumps({"members": _stereo_pair_members()}).encode()
    h = _FakeHandler("/bond")
    h.headers["Content-Length"] = str(len(raw))
    h.headers["X-JTS-Token"] = "household-secret"
    h.rfile = BytesIO(raw)
    handler_cls.do_POST(h)

    assert h.status == 200
    # Both members got the forwarded token.
    assert seen_tokens == ["household-secret", "household-secret"]


def test_bond_forwards_no_token_when_browser_sent_none(monkeypatch):
    """Default-off: no X-JTS-Token on the request -> the leader forwards
    None, so the existing 3-arg call shape is preserved."""
    seen_tokens: list[str | None] = []

    def capture(addr, member_body, known=None, *, token=None, household=None):
        seen_tokens.append(token)
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", capture)
    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_leader_handle", lambda: "jts-living.local")

    handler_cls = rooms_setup._make_handler()
    raw = json.dumps({"members": _stereo_pair_members()}).encode()
    h = _FakeHandler("/bond")
    h.headers["Content-Length"] = str(len(raw))
    h.rfile = BytesIO(raw)
    handler_cls.do_POST(h)

    assert h.status == 200
    assert seen_tokens == [None, None]


def _stereo_pair_members():
    return [
        {"addr": "192.168.1.5", "role": "leader", "channel": "left"},
        {"addr": "192.168.1.9", "role": "follower", "channel": "right"},
    ]


def test_post_bond_peer_addr_intent_builds_stereo_pair(monkeypatch):
    monkeypatch.setattr(
        rooms_setup,
        "discover_speakers_cached",
        lambda: [{"address": "192.168.1.9", "name": "Right Box"}],
    )
    h, calls = _post_bond({"peer_addr": "192.168.1.9"}, monkeypatch=monkeypatch)
    assert h.status == 200

    bodies = {addr: body for addr, body in calls}
    assert bodies[""]["role"] == "leader"
    assert bodies[""]["channel"] == "left"
    assert bodies[""]["peer_addr"] == "192.168.1.9"
    assert bodies[""]["peer_name"] == "Right Box"
    assert bodies[""]["trim_db"] == 0.0
    assert bodies["192.168.1.9"]["role"] == "follower"
    assert bodies["192.168.1.9"]["channel"] == "right"
    assert bodies["192.168.1.9"]["leader_addr"] == "jts-living.local"
    assert bodies["192.168.1.9"]["trim_db"] == 0.0


def test_post_bond_configures_all_members_and_wires_leader_addr(monkeypatch):
    h, calls = _post_bond({"members": _stereo_pair_members()}, monkeypatch=monkeypatch)
    assert h.status == 200
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is True
    assert body["bond_id"].startswith("bond-")
    assert len(calls) == 2
    by_role = {c[1]["role"]: c[1] for c in calls}
    # leader gets no leader_addr; the follower gets the leader's STABLE mDNS
    # handle (read_identity().hostname), NOT a NIC IP — so the bond survives
    # DHCP lease churn. snapclient resolves the .local name via mDNS.
    assert by_role["leader"]["leader_addr"] == ""
    assert by_role["follower"]["leader_addr"] == "jts-living.local"
    assert by_role["follower"]["channel"] == "right"
    assert all(c[1]["enabled"] is True for c in calls)
    assert all(c[1]["trim_db"] == 0.0 for c in calls)
    # one shared bond_id across both members
    assert {c[1]["bond_id"] for c in calls} == {body["bond_id"]}


def test_post_bond_existing_bond_omits_trim_to_preserve_balance(monkeypatch):
    """Re-posting an existing bond, e.g. add-subwoofer, must not reset an
    already-calibrated left/right balance."""
    members = [
        {"addr": "192.168.1.5", "role": "leader", "channel": "left"},
        {"addr": "192.168.1.9", "role": "follower", "channel": "right"},
        {"addr": "192.168.1.8", "role": "follower", "channel": "sub",
         "crossover_hz": 90},
    ]
    h, calls = _post_bond(
        {"bond_id": "bond-existing", "members": members},
        monkeypatch=monkeypatch,
    )
    assert h.status == 200
    assert all("trim_db" not in body for _addr, body in calls)


def _sub_bond_members():
    """A leader (full-range "stereo") + a subwoofer follower with a corner."""
    return [
        {"addr": "192.168.1.5", "role": "leader", "channel": "stereo"},
        {"addr": "192.168.1.9", "role": "follower", "channel": "sub",
         "crossover_hz": 90},
    ]


def test_post_bond_forwards_crossover_hz_for_a_sub_member(monkeypatch):
    """A subwoofer bond forwards the same crossover to every member: the sub
    low-passes there, and mains high-pass at the matched corner by default."""
    h, calls = _post_bond({"members": _sub_bond_members()}, monkeypatch=monkeypatch)
    assert h.status == 200
    by_role = {c[1]["role"]: c[1] for c in calls}
    assert by_role["leader"]["channel"] == "stereo"
    assert by_role["follower"]["channel"] == "sub"
    # The corner is forwarded verbatim — validate_grouping (on the receiver)
    # owns clamping; the leader just passes the browser's number through.
    assert by_role["follower"]["crossover_hz"] == 90
    assert by_role["leader"]["crossover_hz"] == 90
    assert all(c[1]["subwoofer_present"] is True for c in calls)
    assert all(c[1]["mains_highpass_enabled"] is True for c in calls)


def test_post_bond_sub_corner_is_authoritative_for_all_members(monkeypatch):
    """A sub bond has one crossover corner by construction.

    Even if a non-browser caller supplies stale per-main crossover values, the
    sub member's corner is the bond-level value fanned out to every member.
    """
    members = [
        {"addr": "192.168.1.5", "role": "leader", "channel": "stereo",
         "crossover_hz": 55},
        {"addr": "192.168.1.9", "role": "follower", "channel": "sub",
         "crossover_hz": 90},
    ]
    h, calls = _post_bond({"members": members}, monkeypatch=monkeypatch)
    assert h.status == 200
    assert all(c[1]["crossover_hz"] == 90 for c in calls)


def test_post_bond_omits_crossover_hz_when_absent(monkeypatch):
    """A plain stereo pair sends no crossover_hz key — the fan-out only
    forwards it when the bond contains a sub, so non-sub members stay clean."""
    h, calls = _post_bond({"members": _stereo_pair_members()}, monkeypatch=monkeypatch)
    assert h.status == 200
    assert all("crossover_hz" not in c[1] for c in calls)
    assert all(c[1]["subwoofer_present"] is False for c in calls)
    assert all(c[1]["mains_highpass_enabled"] is True for c in calls)


def test_post_bond_preflights_active_speaker_setup_before_fanout(monkeypatch):
    def fake_setup(addr, *_args, **_kwargs):
        if addr == "192.168.1.9":
            return {
                "active": True,
                "configured": False,
                "volume_allowed": False,
                "grouping_allowed": False,
                "reason": "baseline_summed_validation_missing",
                "detail": "validate the combined crossover before saving the active profile",
            }
        return {
            "active": False,
            "configured": True,
            "volume_allowed": True,
            "grouping_allowed": True,
        }

    monkeypatch.setattr(rooms_setup, "_get_member_active_speaker_setup", fake_setup)

    h, calls = _post_bond({"members": _stereo_pair_members()}, monkeypatch=monkeypatch)

    assert h.status == 409
    assert calls == []
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is False
    assert body["error"] == "one or more speakers are not ready to join a group"
    assert body["results"] == [
        {
            "addr": "192.168.1.9",
            "role": "follower",
            "ok": False,
            "detail": "validate the combined crossover before saving the active profile",
        }
    ]


def test_post_bond_rejects_bad_csrf_without_fanning_out(monkeypatch):
    h, calls = _post_bond({"members": _stereo_pair_members()},
                          csrf_ok=False, monkeypatch=monkeypatch)
    assert h.status == 403
    assert calls == []  # nothing configured on a rejected request


def test_post_bond_empty_members_is_400(monkeypatch):
    h, calls = _post_bond({"members": []}, monkeypatch=monkeypatch)
    assert h.status == 400
    assert calls == []


def test_post_bond_partial_failure_is_502_with_per_member_results(monkeypatch):
    h, calls = _post_bond(
        {"members": _stereo_pair_members()},
        monkeypatch=monkeypatch,
        member_results={"192.168.1.9": (False, "Connection refused")},
    )
    assert h.status == 502
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is False
    results = {r["addr"]: r for r in body["results"]}
    assert results["192.168.1.5"]["ok"] is True
    assert results["192.168.1.9"]["ok"] is False
    assert "Connection refused" in results["192.168.1.9"]["detail"]


def test_post_bond_unknown_path_still_404s_before_csrf(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("CSRF must not run on an unknown POST path")

    monkeypatch.setattr(rooms_setup, "guard_mutating_request", _boom)
    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/bond-typo")
    h.headers["Content-Length"] = "2"
    h.rfile = BytesIO(b"{}")
    handler_cls.do_POST(h)
    assert h.status == 404


# ---- post_grouping_to_member: the cross-speaker call + SSRF guard ----


@pytest.mark.parametrize(
    ("address", "detail_token"),
    [
        ("8.8.8.8", "non-LAN"),
        ("evil.example.com", "not an IP"),
        ("::1", "non-LAN"),
        ("fd00::7", "non-LAN"),
        ("fe80::7", "non-LAN"),
    ],
)
def test_member_post_rejects_non_lan_or_ipv6_without_request(
    monkeypatch, address, detail_token,
):
    def fail_if_requested(*_args, **_kwargs):
        raise AssertionError("rejected targets must never reach urlopen")

    monkeypatch.setattr(rooms_setup.urllib.request, "urlopen", fail_if_requested)
    ok, detail = rooms_setup.post_grouping_to_member(address, {}, known=set())
    assert ok is False
    assert detail_token in detail


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
    assert rooms_setup.lan_target(address, known) == expected


def test_member_post_self_routes_to_loopback(monkeypatch):
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: {"192.168.1.5"})
    urls: list[str] = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(rooms_setup.urllib.request, "urlopen", fake_urlopen)
    ok, _detail = rooms_setup.post_grouping_to_member("192.168.1.5", {"x": 1})
    assert ok is True
    assert urls == ["http://127.0.0.1:8780/grouping/set"]


def test_member_post_lan_peer_targets_its_control_port(monkeypatch):
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: {"192.168.1.5"})
    urls: list[str] = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        rooms_setup.urllib.request, "urlopen",
        lambda req, timeout=None: urls.append(req.full_url) or FakeResp(),
    )
    ok, _detail = rooms_setup.post_grouping_to_member("192.168.1.9", {"x": 1})
    assert ok is True
    assert urls == ["http://192.168.1.9:8780/grouping/set"]


# ---- household credential on the fan-out (control-plane-auth §6) ----


def _capture_member_request_headers(monkeypatch):
    """Stub urlopen and return a dict that fills with the lowercased request
    headers of the LAST post_grouping_to_member call."""
    captured: dict[str, str] = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured.clear()
        captured.update({k.lower(): v for k, v in req.header_items()})
        return FakeResp()

    monkeypatch.setattr(rooms_setup.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_post_grouping_to_member_attaches_household_credential(monkeypatch):
    """The fan-out injects the household secret (X-JTS-Household) from disk —
    the device-to-device credential each member verifies — alongside any relayed
    browser X-JTS-Token."""
    secret = household_credential.ensure()  # pair this speaker (autouse tmp file)
    headers = _capture_member_request_headers(monkeypatch)
    ok, _ = rooms_setup.post_grouping_to_member(
        "192.168.1.9", {"x": 1}, token="browser-tok",
    )
    assert ok is True
    assert headers["x-jts-household"] == secret
    assert headers["x-jts-token"] == "browser-tok"


def test_post_grouping_to_member_omits_household_when_unpaired(monkeypatch):
    """A lone/unpaired speaker (no secret) attaches no X-JTS-Household — there is
    nothing to present, and the member fail-safe-accepts during bootstrap."""
    assert household_credential.is_paired() is False  # autouse tmp file is absent
    headers = _capture_member_request_headers(monkeypatch)
    ok, _ = rooms_setup.post_grouping_to_member("192.168.1.9", {"x": 1})
    assert ok is True
    assert "x-jts-household" not in headers


def test_post_grouping_to_member_explicit_household_overrides_live_read(monkeypatch):
    """An explicit household= (the race-free unbond path) is used verbatim,
    even over a different on-disk value."""
    household_credential.ensure()  # disk has some secret
    headers = _capture_member_request_headers(monkeypatch)
    rooms_setup.post_grouping_to_member(
        "192.168.1.9", {"x": 1}, household="pre-read-secret",
    )
    assert headers["x-jts-household"] == "pre-read-secret"


def test_save_bond_mints_household_credential(monkeypatch):
    """_save_bond mints the household secret on this leader BEFORE the fan-out,
    so the leader has it to distribute (control-plane-auth §6)."""
    assert household_credential.is_paired() is False

    def capture(addr, member_body, known=None, *, token=None, household=None):
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", capture)
    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_leader_handle", lambda: "jts-living.local")

    handler_cls = rooms_setup._make_handler()
    raw = json.dumps({"members": _stereo_pair_members()}).encode()
    h = _FakeHandler("/bond")
    h.headers["Content-Length"] = str(len(raw))
    h.rfile = BytesIO(raw)
    handler_cls.do_POST(h)

    assert h.status == 200
    assert household_credential.is_paired() is True  # minted


def test_unbond_reads_household_once_and_passes_it_to_fanout(monkeypatch):
    """Unbond reads the secret ONCE and passes it explicitly to the fan-out, so
    the concurrent per-member clears can't race a peer out of the credential it
    needs to authenticate its own unbond."""
    secret = household_credential.ensure()
    seen_household: list[str | None] = []

    def capture(addr, body, known=None, *, token=None, household=None):
        seen_household.append(household)
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "read_grouping_state",
                        lambda *a, **k: {"enabled": True, "role": "leader",
                                         "bond_id": "bond-1", "peer_addr": "192.168.1.9"})
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "resolve_bond_peer",
                        lambda *a, **k: ("192.168.1.9", None, None))
    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", capture)

    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/unbond")
    h.headers["Content-Length"] = "2"
    h.rfile = BytesIO(b"{}")
    handler_cls.do_POST(h)

    # Self + the matched peer both got the SAME pre-read secret (never None).
    assert seen_household and all(hh == secret for hh in seen_household)


# ---- _fan_out_grouping: concurrent, INPUT-ORDER-preserving fan-out ----


def test_fan_out_grouping_preserves_input_order_despite_slow_failing_member(monkeypatch):
    """A slow or failing member must NOT reorder results — the caller pairs
    results back to members positionally. We make the FIRST target slow + a
    failure and the SECOND fast + ok; the results must still come back in the
    input order ([first, second]), not completion order."""
    import threading
    import time as _time

    started = threading.Event()

    def fake_member_post(addr, body, known=None, *, token=None, household=None):
        if addr == "192.168.1.5":
            # First target: block until the second has had a chance to finish,
            # then fail. If results were ordered by completion this would land
            # second.
            started.set()
            _time.sleep(0.05)
            return (False, "slow boom")
        # Second target: wait for the first to have started, return fast + ok.
        started.wait(timeout=1.0)
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", fake_member_post)
    targets = [
        ("192.168.1.5", {"enabled": True}),
        ("192.168.1.9", {"enabled": True}),
    ]
    results = rooms_setup._fan_out_grouping(targets)
    # Strictly input order: the slow/failed first target stays first.
    assert results == [(False, "slow boom"), (True, "HTTP 200")]


def test_grouping_set_success_detail_distinguishes_live_and_scheduled_apply():
    assert rooms_setup._grouping_set_success_detail(
        200,
        b'{"ok":true,"live_apply":{"applied":true,"mode":"outputd","trim_db":-2.5}}',
    ) == "Applied live."
    assert rooms_setup._grouping_set_success_detail(
        200,
        b'{"ok":true,"reconciler_kicked":true,'
        b'"live_apply":{"applied":false,"mode":"outputd","trim_db":-2.5}}',
    ) == "Saved; audio update scheduled."
    assert rooms_setup._grouping_set_success_detail(200, b"") == "HTTP 200"


def test_fan_out_grouping_empty_targets_is_empty_list():
    assert rooms_setup._fan_out_grouping([]) == []


def test_fan_out_grouping_computes_self_addresses_once_not_per_member(monkeypatch):
    """The SSRF guard's self-address set is computed ONCE per fan-out and
    shared across members (self_addresses does a socket probe + getaddrinfo).
    A 3-member fan-out must call it once, not three times (the N-redundant-
    lookups smell the `known=` threading removes)."""
    calls = {"n": 0}

    def counting_self_addresses():
        calls["n"] += 1
        return {"192.168.1.5"}

    monkeypatch.setattr(rooms_setup, "self_addresses", counting_self_addresses)
    monkeypatch.setattr(
        rooms_setup, "post_grouping_to_member",
        lambda addr, body, known=None, *, token=None, household=None: (True, "HTTP 200"),
    )
    out = rooms_setup._fan_out_grouping([(f"192.168.1.{i}", {}) for i in (10, 11, 12)])
    assert len(out) == 3
    assert calls["n"] == 1  # computed once, shared across all three members


# ---- _map_peers: the one bounded-concurrency primitive ----


def test_map_peers_runs_concurrently_and_preserves_input_order():
    """_map_peers runs items concurrently (wall-clock << sum of per-item
    sleeps) and returns results in INPUT order regardless of completion
    order — the property both the fan-out and the discovery rely on."""
    import threading
    import time as _time

    started = threading.Event()

    def fn(i):
        if i == 0:
            started.set()
            _time.sleep(0.05)   # first item slow...
            return "a"
        started.wait(timeout=1.0)
        return "b"              # ...second finishes first

    t0 = _time.monotonic()
    out = rooms_setup._map_peers(fn, [0, 1])
    elapsed = _time.monotonic() - t0
    assert out == ["a", "b"]          # input order, not completion order
    assert elapsed < 0.2              # concurrent: not 0.05 + serialized wait


def test_map_peers_empty_is_empty_list():
    assert rooms_setup._map_peers(lambda x: x, []) == []


def test_map_peers_caps_worker_count():
    """The pool is bounded by _PEER_FANOUT_MAX_WORKERS — a large household
    can't spawn an unbounded number of blocking-HTTP threads."""
    import threading

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
        return None

    # 20 items, cap 8 -> never more than 8 run at once.
    items = list(range(20))
    done = []

    def run():
        done.append(rooms_setup._map_peers(fn, items))

    t = threading.Thread(target=run)
    t.start()
    # Let the first wave saturate the pool, then release.
    import time as _time
    _time.sleep(0.1)
    release.set()
    t.join(timeout=2.0)
    assert peak["n"] <= rooms_setup._PEER_FANOUT_MAX_WORKERS


# ---- _get_member_grouping: GET a peer's /grouping behind the SSRF guard ----


def test_get_member_grouping_refuses_non_lan_and_non_ip_target(monkeypatch):
    """Public, named, and IPv6 targets never reach the HTTP transport."""
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())

    def _boom(*_a, **_k):
        raise AssertionError("must not issue a request for a refused target")

    monkeypatch.setattr(rooms_setup.urllib.request, "urlopen", _boom)
    assert rooms_setup._get_member_grouping("8.8.8.8") is None         # non-LAN
    assert rooms_setup._get_member_grouping("evil.example.com") is None  # non-IP
    assert rooms_setup._get_member_grouping("::1") is None
    assert rooms_setup._get_member_grouping("fd00::7") is None


def test_remote_json_get_success_forwards_request_and_timeout(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=None):
        captured.update(
            url=req.full_url,
            method=req.get_method(),
            timeout=timeout,
        )
        return FakeResp()

    monkeypatch.setattr(rooms_setup.urllib.request, "urlopen", fake_urlopen)

    assert rooms_setup._get_remote_json(
        "192.168.1.9", "/state", timeout=0.375,
    ) == {"ok": True}
    assert captured == {
        "url": "http://192.168.1.9:8780/state",
        "method": "GET",
        "timeout": 0.375,
    }


def test_remote_json_get_rejects_oversized_peer_response(monkeypatch):
    class OversizedResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            assert size == rooms_setup.PEER_RESPONSE_MAX_BYTES + 1
            return b"x" * size

    monkeypatch.setattr(
        rooms_setup.urllib.request, "urlopen", lambda *_a, **_k: OversizedResp(),
    )

    assert rooms_setup._get_remote_json(
        "192.168.1.9", "/state", timeout=0.5,
    ) is None


def test_member_post_rejects_oversized_success_response(monkeypatch):
    class OversizedResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            return b"x" * size

    monkeypatch.setattr(
        rooms_setup.urllib.request, "urlopen", lambda *_a, **_k: OversizedResp(),
    )

    assert rooms_setup.post_grouping_to_member(
        "192.168.1.9", {"enabled": False}, known=set(), household="secret",
    ) == (False, "peer response too large")


def test_member_post_bounds_http_error_response(monkeypatch):
    error = rooms_setup.urllib.error.HTTPError(
        "http://192.168.1.9:8780/grouping/set",
        500,
        "failure",
        hdrs=None,
        fp=BytesIO(b"x" * (rooms_setup.PEER_RESPONSE_MAX_BYTES + 1)),
    )
    monkeypatch.setattr(
        rooms_setup.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error),
    )

    assert rooms_setup.post_grouping_to_member(
        "192.168.1.9", {"enabled": False}, known=set(), household="secret",
    ) == (False, "HTTP 500: response too large")


def test_member_post_contains_http_error_body_read_failure(monkeypatch):
    class BrokenBody:
        def read(self, _size):
            raise OSError("truncated peer body")

        def close(self):
            pass

    error = rooms_setup.urllib.error.HTTPError(
        "http://192.168.1.9:8780/grouping/set",
        502,
        "failure",
        hdrs=None,
        fp=BrokenBody(),
    )
    monkeypatch.setattr(
        rooms_setup.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error),
    )

    assert rooms_setup.post_grouping_to_member(
        "192.168.1.9", {}, known=set(), household="house-secret",
    ) == (False, "HTTP 502")


def test_member_post_redacts_echoed_credentials_from_http_error(monkeypatch):
    token = "browser-secret"
    household = "house-secret"
    error = rooms_setup.urllib.error.HTTPError(
        "http://192.168.1.9:8780/grouping/set",
        403,
        "failure",
        hdrs=None,
        fp=BytesIO(f"denied {token} {household}".encode()),
    )
    monkeypatch.setattr(
        rooms_setup.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error),
    )

    ok, detail = rooms_setup.post_grouping_to_member(
        "192.168.1.9", {}, known=set(), token=token, household=household,
    )
    assert ok is False
    assert detail == "HTTP 403: denied [redacted] [redacted]"
    assert token not in detail
    assert household not in detail


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, b'{"ok": true}'),
        (200, b"not-json"),
        (200, b"[]"),
        (200, b"\xff"),
    ],
)
def test_remote_json_get_fails_soft_on_status_decode_or_shape(
    monkeypatch, status, body,
):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return body

    response = FakeResp()
    response.status = status
    monkeypatch.setattr(
        rooms_setup.urllib.request, "urlopen", lambda *_a, **_k: response,
    )

    assert rooms_setup._get_remote_json(
        "192.168.1.9", "/grouping", timeout=0.5,
    ) is None


def test_remote_json_get_fails_soft_on_transport_timeout(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise TimeoutError("peer timed out")

    monkeypatch.setattr(rooms_setup.urllib.request, "urlopen", timeout)
    assert rooms_setup._get_remote_json(
        "192.168.1.9", "/grouping", timeout=0.125,
    ) is None


def test_get_member_grouping_forwards_timeout_then_parses_domain(monkeypatch):
    calls = []

    def fake_get(target, path, *, timeout):
        calls.append((target, path, timeout))
        return {"grouping": {"bond_id": "bond-a"}}

    monkeypatch.setattr(rooms_setup, "_get_remote_json", fake_get)

    assert rooms_setup._get_member_grouping(
        "192.168.1.9", known=set(), timeout=0.625,
    ) == {"bond_id": "bond-a"}
    assert calls == [("192.168.1.9", "/grouping", 0.625)]


def test_active_setup_remote_uses_shared_get_and_domain_parser(monkeypatch):
    expected = {"active": True, "grouping_allowed": False}
    calls = []

    def fake_get(target, path, *, timeout):
        calls.append((target, path, timeout))
        return {"active_speaker_setup": expected}

    monkeypatch.setattr(rooms_setup, "_get_remote_json", fake_get)

    assert _REAL_GET_MEMBER_ACTIVE_SPEAKER_SETUP(
        "192.168.1.9", known=set(), timeout=0.75,
    ) == expected
    assert calls == [("192.168.1.9", "/state", 0.75)]


def test_active_setup_local_branch_never_uses_remote_transport(monkeypatch):
    expected = {"active": False, "grouping_allowed": True}
    monkeypatch.setattr(
        rooms_setup, "read_active_speaker_setup_status", lambda: expected,
    )

    def fail_if_remote(*_args, **_kwargs):
        raise AssertionError("loopback readiness must use the local reader")

    monkeypatch.setattr(rooms_setup, "_get_remote_json", fail_if_remote)

    assert _REAL_GET_MEMBER_ACTIVE_SPEAKER_SETUP("", known=set()) == expected


def _fake_grouping_urlopen(monkeypatch, body, *, status=200):
    """Stub urlopen to return `body` (a dict, serialized) from a /grouping GET.
    Returns the list the URLs are appended to so tests can assert the path."""
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    urls: list[str] = []

    class FakeResp:
        def __init__(self):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, _size=-1):
            return json.dumps(body).encode()

    def fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(rooms_setup.urllib.request, "urlopen", fake_urlopen)
    return urls


def test_get_member_grouping_unwraps_grouping_envelope(monkeypatch):
    """The control-server GET /grouping nests the snapshot under a "grouping"
    key ({"grouping": {...}}; see test_control_server.py). _get_member_grouping
    must UNWRAP that envelope and return the inner flat dict — the bond_id the
    dissolve filter compares lives inside it, not at top level. (Regression:
    returning the raw body left bond_id unreadable, so /unbond matched no real
    peer and dissolved only self.)"""
    inner = {"enabled": True, "role": "follower", "bond_id": "bond-abc"}
    urls = _fake_grouping_urlopen(monkeypatch, {"grouping": inner})
    got = rooms_setup._get_member_grouping("192.168.1.9")
    assert got == inner
    assert urls == ["http://192.168.1.9:8780/grouping"]


def test_get_member_grouping_none_when_envelope_missing_or_null(monkeypatch):
    """A body without a dict `grouping` block (flat, null, or absent) reads as
    "unknown" → None, so it can never spuriously match a bond_id. Guards the
    unwrap against the pre-fix flat-shape assumption."""
    _fake_grouping_urlopen(monkeypatch, {"grouping": None})
    assert rooms_setup._get_member_grouping("192.168.1.9") is None
    # A flat body (no envelope) — the shape the live endpoint does NOT emit.
    _fake_grouping_urlopen(monkeypatch, {"enabled": True, "bond_id": "x"})
    assert rooms_setup._get_member_grouping("192.168.1.9") is None


# ----------------------------------------------------------------------
# POST /unbond — dissolve the bond this speaker is in.
#
# Reads self grouping (read_grouping_state), browses siblings, GETs each
# peer's /grouping, and disables self + every peer sharing this bond_id.
# A peer in a DIFFERENT bond is left alone; self is ALWAYS disabled so the
# local "leave the bond" always works.
# ----------------------------------------------------------------------


def _post_unbond(*, csrf_ok=True, monkeypatch, self_grouping,
                 speakers=(), peer_grouping=None, member_results=None):
    """Drive POST /unbond with the cross-speaker GET/POST stubbed. Returns
    (handler, posts) where posts is the list of (addr, body) disabled.

    self_grouping  — what read_grouping_state() returns for THIS speaker.
    speakers       — discovered siblings ({address: ...} dicts).
    peer_grouping  — {address: grouping_dict | None} the per-peer GET returns.
    member_results — {address: (ok, detail)} overriding the disable POST."""
    posts: list[tuple[str, dict]] = []

    def fake_member_post(addr, body, known=None, *, token=None, household=None):
        posts.append((addr, body))
        if member_results and addr in member_results:
            return member_results[addr]
        return (True, "HTTP 200")

    def fake_get_grouping(addr, known=None):
        return (peer_grouping or {}).get(addr)

    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: csrf_ok)
    monkeypatch.setattr(rooms_setup, "reject_csrf",
                        lambda h: h.send_response(403) or h.end_headers())
    monkeypatch.setattr(rooms_setup, "read_grouping_state",
                        lambda *a, **k: dict(self_grouping))
    monkeypatch.setattr(rooms_setup, "discover_speakers_cached",
                        lambda: list(speakers))
    # Empty self-address set: hermetic (no socket probe) and so the candidate
    # filter (`a not in known`) keeps every test speaker — none is "self".
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_get_member_grouping", fake_get_grouping)
    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", fake_member_post)

    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/unbond")
    h.headers["Content-Length"] = "2"
    h.rfile = BytesIO(b"{}")
    handler_cls.do_POST(h)
    return h, posts


def test_post_unbond_disables_self_and_matching_peer_only(monkeypatch):
    """Happy path: self + the peer sharing our bond_id get disabled and
    trim-reset; a peer in a DIFFERENT bond is NOT touched."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "bond_id": "bond-1"},
        speakers=[
            {"address": "192.168.1.9"},   # same bond -> disabled
            {"address": "192.168.1.20"},  # different bond -> left alone
        ],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "bond_id": "bond-1"},
            "192.168.1.20": {"enabled": True, "bond_id": "bond-OTHER"},
        },
    )
    assert h.status == 200
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is True
    assert body["bond_id"] == "bond-1"
    # Self ("") + the matching peer were disabled; the other-bond peer was not.
    disabled_addrs = [a for a, _b in posts]
    assert disabled_addrs == ["", "192.168.1.9"]
    assert all(b == {"enabled": False, "trim_db": 0.0} for _a, b in posts)
    assert "192.168.1.20" not in disabled_addrs
    assert set(body["dissolved"]) == {"", "192.168.1.9"}


def test_post_unbond_400_when_not_in_a_bond(monkeypatch):
    """If this speaker isn't in a bond, there's nothing to dissolve -> 400 and
    no cross-speaker calls."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": False, "role": "", "bond_id": ""},
        speakers=[{"address": "192.168.1.9"}],
    )
    assert h.status == 400
    assert json.loads(h.wfile.getvalue()) == {"ok": False, "error": "not in a bond"}
    assert posts == []  # never browsed/disabled anything


def test_post_unbond_enabled_but_no_bond_id_is_400(monkeypatch):
    """enabled=True but an empty bond_id is still 'not in a bond' (nothing to
    dissolve) — guards the `or not bond_id` branch."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "bond_id": ""},
    )
    assert h.status == 400
    assert posts == []


def test_post_unbond_still_disables_self_when_peer_unreachable(monkeypatch):
    """Self must ALWAYS be disabled so 'leave the bond' works locally even when
    a peer GET fails (returns None -> peer not in the disable set) AND a peer
    POST would fail. Here the matching peer is unreachable on GET, so only self
    is disabled — and self succeeds, so HTTP 200."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "follower", "bond_id": "bond-1"},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={"192.168.1.9": None},  # GET failed -> excluded
    )
    assert h.status == 200
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is True
    # Only self was disabled (peer GET failed, so it never joined the set).
    assert [a for a, _b in posts] == [""]
    assert body["dissolved"] == [""]


def test_post_unbond_502_when_self_disable_fails(monkeypatch):
    """If even the self-disable POST fails, the local leave didn't take -> 502,
    but the call still attempted self (no peers matched here)."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "bond_id": "bond-1"},
        speakers=[],
        member_results={"": (False, "write failed")},
    )
    assert h.status == 502
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is False
    assert body["dissolved"] == []  # self failed -> nothing confirmed disabled
    assert [a for a, _b in posts] == [""]


def test_post_unbond_rejects_bad_csrf(monkeypatch):
    """CSRF guard fires before any dissolve work."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        csrf_ok=False,
        self_grouping={"enabled": True, "role": "leader", "bond_id": "bond-1"},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={"192.168.1.9": {"enabled": True, "bond_id": "bond-1"}},
    )
    assert h.status == 403
    assert posts == []


def test_post_unbond_unknown_path_404s_before_csrf(monkeypatch):
    """/unbond is in the route allow-list; a near-miss path still 404s before
    the CSRF guard runs."""
    def _boom(*_a, **_k):
        raise AssertionError("CSRF must not run on an unknown POST path")

    monkeypatch.setattr(rooms_setup, "guard_mutating_request", _boom)
    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/unbond-typo")
    h.headers["Content-Length"] = "2"
    h.rfile = BytesIO(b"{}")
    handler_cls.do_POST(h)
    assert h.status == 404


# ----------------------------------------------------------------------
# Per-member fan-out failure observability — a half-formed/half-dissolved
# bond must NAME the failed member in the journal (the HTTP response isn't a
# diagnostic surface on a headless speaker). Failures only (no journal spam).
# ----------------------------------------------------------------------


def test_post_bond_logs_per_member_failure_only(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="jasper.web.rooms_setup")
    _post_bond(
        {"members": _stereo_pair_members()},
        monkeypatch=monkeypatch,
        member_results={"192.168.1.9": (False, "Connection refused")},
    )
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # The failed follower is named with its addr + reason...
    assert any(
        "event=rooms.bond.member_failed" in m
        and "192.168.1.9" in m and "Connection refused" in m
        for m in warns
    )
    # ...and the succeeding leader is NOT logged (failures only).
    assert not any("member_failed" in m and "192.168.1.5" in m for m in warns)


def test_post_unbond_logs_per_member_failure(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="jasper.web.rooms_setup")
    _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "bond_id": "bond-1"},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={"192.168.1.9": {"enabled": True, "bond_id": "bond-1"}},
        member_results={"192.168.1.9": (False, "Connection refused")},
    )
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "event=rooms.unbond.member_failed" in m
        and "192.168.1.9" in m and "Connection refused" in m
        for m in warns
    )


def test_post_unbond_logs_unreachable_candidate_count(monkeypatch, caplog):
    """A same-bond follower unreachable during discovery (GET → None) is
    counted as `unreachable` in the aggregate log — it explains a "dissolved
    but a speaker stayed grouped" report (we couldn't read its bond_id, so it
    never became a disable target)."""
    caplog.set_level(logging.INFO, logger="jasper.web.rooms_setup")
    _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "bond_id": "bond-1"},
        speakers=[{"address": "192.168.1.9"}, {"address": "192.168.1.20"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "bond_id": "bond-1"},  # matched
            "192.168.1.20": None,                                   # unreachable
        },
    )
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "event=rooms.unbond " in m
        and "path=discovery" in m and "unreachable=1" in m and "peers=1" in m
        for m in infos
    )


# ----------------------------------------------------------------------
# POST /swap — exchange the pair's left/right channels.
# ----------------------------------------------------------------------


def _post_swap(*, monkeypatch, self_grouping, speakers=(), peer_grouping=None,
               member_results=None):
    """Drive POST /swap with the cross-speaker GET/POST stubbed. Returns
    (handler, posts) where posts is the list of (addr, body) written. Same
    stub set as _post_unbond — swap shares its discovery + fan-out path."""
    posts: list[tuple[str, dict]] = []

    def fake_member_post(addr, body, known=None, *, token=None, household=None):
        posts.append((addr, body))
        if member_results and addr in member_results:
            return member_results[addr]
        return (True, "HTTP 200")

    def fake_get_grouping(addr, known=None):
        return (peer_grouping or {}).get(addr)

    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "read_grouping_state",
                        lambda *a, **k: dict(self_grouping))
    monkeypatch.setattr(rooms_setup, "discover_speakers_cached",
                        lambda: list(speakers))
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_get_member_grouping", fake_get_grouping)
    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", fake_member_post)

    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/swap")
    h.headers["Content-Length"] = "2"
    h.rfile = BytesIO(b"{}")
    handler_cls.do_POST(h)
    return h, posts


def test_post_swap_exchanges_channels_and_keeps_roles(monkeypatch):
    """Happy path: leader/left + follower/right become leader/right +
    follower/left. Roles, bond_id, and each member's leader_addr are
    untouched — swap is a channel edit, never a leadership change."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1", "leader_addr": ""},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "role": "follower",
                            "channel": "right", "bond_id": "bond-1",
                            "leader_addr": "jts.local"},
        },
    )
    assert h.status == 200
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is True
    assert posts == [
        ("", {"enabled": True, "role": "leader", "channel": "right",
              "bond_id": "bond-1", "leader_addr": ""}),
        ("192.168.1.9", {"enabled": True, "role": "follower",
                         "channel": "left", "bond_id": "bond-1",
                         "leader_addr": "jts.local"}),
    ]


def test_post_swap_400_when_not_in_a_bond(monkeypatch):
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": False, "role": "", "bond_id": ""},
        speakers=[{"address": "192.168.1.9"}],
    )
    assert h.status == 400
    assert posts == []


def test_post_swap_400_when_peer_unreachable(monkeypatch):
    """Zero reachable same-bond peers -> 400 (swap needs both ends alive),
    and nothing is written — a half-swapped pair must be impossible."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1"},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={"192.168.1.9": None},  # GET failed
    )
    assert h.status == 400
    assert "exactly one" in json.loads(h.wfile.getvalue())["error"]
    assert posts == []


def test_post_swap_400_on_multi_member_bond(monkeypatch):
    """Two same-bond peers -> no well-defined pair swap -> 400, no writes."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1"},
        speakers=[{"address": "192.168.1.9"}, {"address": "192.168.1.10"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "bond_id": "bond-1",
                            "channel": "right"},
            "192.168.1.10": {"enabled": True, "bond_id": "bond-1",
                             "channel": "right"},
        },
    )
    assert h.status == 400
    assert posts == []


def test_post_swap_400_when_channels_are_not_left_right(monkeypatch):
    """A mono/mono bond has nothing to swap -> 400 with both channels named."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "mono",
                       "bond_id": "bond-1"},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "bond_id": "bond-1",
                            "channel": "mono"},
        },
    )
    assert h.status == 400
    assert "left/right" in json.loads(h.wfile.getvalue())["error"]
    assert posts == []


def test_post_swap_partial_failure_rolls_back_the_flipped_member(monkeypatch):
    """The two writes fan out concurrently; exactly-one-failed would leave
    BOTH speakers on the same channel — audibly wrong AND retry-blocked
    (the left/right precondition no longer holds). The succeeded member is
    rolled back to its original channel and the response says so."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1", "leader_addr": ""},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "role": "follower",
                            "channel": "right", "bond_id": "bond-1",
                            "leader_addr": "jts.local"},
        },
        member_results={"192.168.1.9": (False, "connection refused")},
    )
    assert h.status == 502
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is False
    assert body["rolled_back"] is True
    # swap writes (self ok, peer failed) + the self rollback to "left".
    assert len(posts) == 3
    rb_addr, rb_body = posts[2]
    assert rb_addr == ""
    assert rb_body["channel"] == "left"
    assert rb_body["role"] == "leader"


def test_post_swap_rollback_failure_is_surfaced(monkeypatch):
    """If the rollback itself fails, the response reports rolled_back=False
    (never a silent stuck pair) — the journal carries the per-member lines."""
    calls = {"n": 0}

    def flaky_self(addr, body, known=None, *, token=None, household=None):
        # self swap write succeeds; the later self ROLLBACK fails.
        calls["n"] += 1
        if addr == "" and calls["n"] >= 3:
            return (False, "control restarting")
        if addr == "192.168.1.9":
            return (False, "connection refused")
        return (True, "HTTP 200")

    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1", "leader_addr": ""},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "role": "follower",
                            "channel": "right", "bond_id": "bond-1",
                            "leader_addr": "jts.local"},
        },
        member_results=None,
    )
    # Drive again with the flaky poster: patch directly for this variant.
    import jasper.web.rooms_setup as rooms_setup_mod
    monkeypatch.setattr(rooms_setup_mod, "post_grouping_to_member", flaky_self)
    handler_cls = rooms_setup_mod._make_handler()
    h = _FakeHandler("/swap")
    h.headers["Content-Length"] = "2"
    h.rfile = BytesIO(b"{}")
    handler_cls.do_POST(h)
    body = json.loads(h.wfile.getvalue())
    assert h.status == 502
    assert body["rolled_back"] is False


def test_post_swap_repairs_a_same_channel_pair(monkeypatch):
    """A {left,left} pair (interrupted swap whose rollback also failed) must
    be repairable BY swap itself — a strict left/right precondition would
    make Swap the one button that can't fix the state Swap created. Self
    keeps its channel; the peer takes the opposite; response says repaired."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1", "leader_addr": ""},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "role": "follower",
                            "channel": "left", "bond_id": "bond-1",
                            "leader_addr": "jts.local"},
        },
    )
    assert h.status == 200
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is True
    assert body["repaired"] is True
    assert posts == [
        ("", {"enabled": True, "role": "leader", "channel": "left",
              "bond_id": "bond-1", "leader_addr": ""}),
        ("192.168.1.9", {"enabled": True, "role": "follower",
                         "channel": "right", "bond_id": "bond-1",
                         "leader_addr": "jts.local"}),
    ]


# ----------------------------------------------------------------------
# POST /trim — pair-balance writes (attenuate-only).
# ----------------------------------------------------------------------


def _post_trim(*, monkeypatch, body, self_grouping, speakers=(),
               peer_grouping=None, post_results=None):
    posts: list[tuple[str, dict]] = []
    outcomes = list(post_results or [])

    def _post(addr, body, known=None, *, token=None, household=None):
        posts.append((addr, body))
        if outcomes:
            return outcomes.pop(0)
        return True, "HTTP 200"

    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "read_grouping_state",
                        lambda *a, **k: dict(self_grouping))
    monkeypatch.setattr(rooms_setup, "discover_speakers_cached",
                        lambda: list(speakers))
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_get_member_grouping",
                        lambda a, known=None: (peer_grouping or {}).get(a))
    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", _post)
    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/trim")
    raw = json.dumps(body).encode()
    h.headers["Content-Length"] = str(len(raw))
    h.rfile = BytesIO(raw)
    handler_cls.do_POST(h)
    return h, posts


def test_post_trim_pair_writes_absolute_headroom_maximized_balance(monkeypatch):
    """The slider writes absolute pair trims: one side stays at 0 dB, the
    opposite side is attenuated. Positive balance_db means right is louder."""
    h, posts = _post_trim(
        monkeypatch=monkeypatch,
        body={"target": "pair", "balance_db": 3.0},
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "b", "leader_addr": "", "trim_db": 0.0},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={"192.168.1.9": {
            "enabled": True, "role": "follower", "channel": "right",
            "bond_id": "b", "leader_addr": "jts.local", "trim_db": -12.0,
        }},
    )

    payload = json.loads(h.wfile.getvalue())
    assert h.status == 200
    assert payload["balance"]["left_trim_db"] == -3.0
    assert payload["balance"]["right_trim_db"] == 0.0
    assert payload["balance"]["balance_db"] == 3.0
    assert [addr for addr, _ in posts] == ["192.168.1.9", ""]
    assert posts[0][1]["trim_db"] == 0.0       # right peer
    assert posts[1][1]["trim_db"] == -3.0      # left self


def test_post_trim_pair_clamps_at_the_attenuation_floor(monkeypatch):
    h, posts = _post_trim(
        monkeypatch=monkeypatch,
        body={"target": "pair", "balance_db": 30.0},
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "b", "leader_addr": "", "trim_db": 0.0},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={"192.168.1.9": {
            "enabled": True, "role": "follower", "channel": "right",
            "bond_id": "b", "leader_addr": "jts.local", "trim_db": 0.0,
        }},
    )

    payload = json.loads(h.wfile.getvalue())
    assert h.status == 200
    assert payload["balance"]["clamped"] is True
    assert payload["balance"]["left_trim_db"] == -24.0
    assert payload["balance"]["right_trim_db"] == 0.0
    assert posts[1][1]["trim_db"] == -24.0


def test_post_trim_pair_rolls_back_peer_when_self_apply_fails(monkeypatch):
    """A pair balance apply is two writes. If the peer accepted the new trim but
    the local write failed, restore the peer to its original trim so a failed
    request does not leave the pair audibly half-applied."""
    h, posts = _post_trim(
        monkeypatch=monkeypatch,
        body={"target": "pair", "balance_db": 3.0},
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "b", "leader_addr": "", "trim_db": -1.0},
        speakers=[{"address": "192.168.1.9"}],
        peer_grouping={"192.168.1.9": {
            "enabled": True, "role": "follower", "channel": "right",
            "bond_id": "b", "leader_addr": "jts.local", "trim_db": -2.0,
        }},
        post_results=[
            (True, "HTTP 200"),
            (False, "loopback busy"),
            (True, "HTTP 200"),
        ],
    )

    payload = json.loads(h.wfile.getvalue())
    assert h.status == 502
    assert payload["ok"] is False
    assert [addr for addr, _ in posts] == ["192.168.1.9", "", "192.168.1.9"]
    assert posts[0][1]["trim_db"] == 0.0     # peer accepted requested right trim
    assert posts[1][1]["trim_db"] == -3.0    # self failed requested left trim
    assert posts[2][1]["trim_db"] == -2.0    # peer restored to original trim
    assert payload["rollbacks"] == [{
        "addr": "192.168.1.9",
        "channel": "right",
        "trim_db": -2.0,
        "ok": True,
        "detail": "HTTP 200",
    }]


# ----------------------------------------------------------------------
# Bond roster — resolve_bond_peer (the 2026-06-12 "found 2" regression:
# a foreign endpoint-tier Pi transiently claiming the live bond_id made
# every pair operation ambiguous; the roster pins the household's
# actual choice).
# ----------------------------------------------------------------------


def test_swap_with_roster_ignores_foreign_bond_claimer(monkeypatch):
    """TWO devices claim our bond_id, but the leader's roster names its
    real sibling — swap resolves to the roster, never 'found 2'."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1", "leader_addr": "",
                       "peer_addr": "192.168.1.9", "peer_name": "JTS3"},
        speakers=[{"address": "192.168.1.9", "name": "JTS3"},
                  {"address": "192.168.1.162", "name": "JTS Endpoint"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "role": "follower",
                            "channel": "right", "bond_id": "bond-1",
                            "leader_addr": "jts.local"},
            # The interloper ALSO claims bond-1 — pre-roster this made
            # the operation fail with "found 2".
            "192.168.1.162": {"enabled": True, "role": "follower",
                              "channel": "right", "bond_id": "bond-1",
                              "leader_addr": "jts.local"},
        },
    )
    body = json.loads(h.wfile.getvalue().decode())
    assert body["ok"] is True
    peer_posts = [addr for addr, _ in posts if addr]
    assert peer_posts == ["192.168.1.9"]  # only the roster sibling


def test_roster_rediscovers_peer_by_name_after_dhcp_move(monkeypatch):
    """Recorded IP dead, but the directory shows the recorded NAME at a
    new address that answers for our bond — resolve to it."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1", "leader_addr": "",
                       "peer_addr": "192.168.1.9", "peer_name": "JTS3"},
        speakers=[{"address": "192.168.1.77", "name": "JTS3"}],
        peer_grouping={
            "192.168.1.9": None,  # old IP gone
            "192.168.1.77": {"enabled": True, "role": "follower",
                             "channel": "right", "bond_id": "bond-1",
                             "leader_addr": "jts.local"},
        },
    )
    body = json.loads(h.wfile.getvalue().decode())
    assert body["ok"] is True
    assert [a for a, _ in posts if a] == ["192.168.1.77"]


def test_roster_unreachable_is_a_named_error_not_inference(monkeypatch):
    """Roster peer offline and not rediscoverable → a hard error naming
    the speaker — NEVER a fall-back to bond-id inference (which a
    foreign claimer could satisfy)."""
    h, posts = _post_swap(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader", "channel": "left",
                       "bond_id": "bond-1", "leader_addr": "",
                       "peer_addr": "192.168.1.9", "peer_name": "JTS3"},
        # A same-bond claimer IS reachable — inference would pick it.
        speakers=[{"address": "192.168.1.162", "name": "JTS Endpoint"}],
        peer_grouping={
            "192.168.1.9": None,
            "192.168.1.162": {"enabled": True, "role": "follower",
                              "channel": "right", "bond_id": "bond-1",
                              "leader_addr": "jts.local"},
        },
    )
    body = json.loads(h.wfile.getvalue().decode())
    assert body["ok"] is False
    assert "JTS3" in body["error"] and "unreachable" in body["error"]
    assert posts == []  # nothing written anywhere


def test_unbond_with_roster_never_disables_foreign_claimer(monkeypatch):
    """Dissolving a rostered pair disables self + the roster sibling —
    a third device claiming our bond_id is left alone (pre-roster it
    would have been DISABLED: cross-household sabotage)."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader",
                       "bond_id": "bond-1",
                       "peer_addr": "192.168.1.9", "peer_name": "JTS3"},
        speakers=[{"address": "192.168.1.9", "name": "JTS3"},
                  {"address": "192.168.1.162", "name": "JTS Endpoint"}],
        peer_grouping={
            "192.168.1.9": {"enabled": True, "bond_id": "bond-1"},
            "192.168.1.162": {"enabled": True, "bond_id": "bond-1"},
        },
    )
    body = json.loads(h.wfile.getvalue().decode())
    assert body["ok"] is True
    assert [a for a, _ in posts] == ["", "192.168.1.9"]  # self + roster only


def test_unbond_roster_peer_offline_still_aims_disable_at_it(monkeypatch):
    """Best-effort dissolve: the roster peer is offline, so the disable
    is still SENT to its last known address (and reported), rather than
    silently skipping it."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={"enabled": True, "role": "leader",
                       "bond_id": "bond-1",
                       "peer_addr": "192.168.1.9", "peer_name": "JTS3"},
        speakers=[],
        peer_grouping={"192.168.1.9": None},
    )
    assert [a for a, _ in posts] == ["", "192.168.1.9"]


def test_bond_create_records_roster_on_leader_and_clears_follower(monkeypatch):
    """A 2-member bond writes the follower's addr+name into the LEADER's
    body and explicit empty roster fields into the follower's (stale
    roster from a previous leadership must not survive a role flip)."""
    posts: list[tuple[str, dict]] = []

    def fake_member_post(addr, body, known=None, *, token=None, household=None):
        posts.append((addr, body))
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "guard_mutating_request",
                        lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "post_grouping_to_member",
                        fake_member_post)
    monkeypatch.setattr(rooms_setup, "_leader_handle", lambda: "jts.local")

    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/bond")
    payload = json.dumps({"members": [
        {"addr": "", "role": "leader", "channel": "left"},
        {"addr": "192.168.1.9", "role": "follower", "channel": "right",
         "name": "JTS3"},
    ]}).encode()
    h.headers["Content-Length"] = str(len(payload))
    h.rfile = BytesIO(payload)
    handler_cls.do_POST(h)

    bodies = {addr: body for addr, body in posts}
    leader_body = bodies[""]
    follower_body = bodies["192.168.1.9"]
    assert leader_body["peer_addr"] == "192.168.1.9"
    assert leader_body["peer_name"] == "JTS3"
    assert follower_body["peer_addr"] == ""
    assert follower_body["peer_name"] == ""
    assert leader_body["subwoofer_present"] is False
    assert follower_body["subwoofer_present"] is False


def _drive_bond(members, monkeypatch):
    """Drive POST /bond, returning {addr: body} of the fanned-out members."""
    posts: list[tuple[str, dict]] = []

    def fake_member_post(addr, body, known=None, *, token=None, household=None):
        posts.append((addr, body))
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "guard_mutating_request",
                        lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "post_grouping_to_member",
                        fake_member_post)
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_leader_handle", lambda: "jts.local")

    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/bond")
    payload = json.dumps({"members": members}).encode()
    h.headers["Content-Length"] = str(len(payload))
    h.rfile = BytesIO(payload)
    handler_cls.do_POST(h)
    return h, {addr: body for addr, body in posts}


def test_bond_three_members_records_full_roster_with_lr_primary(monkeypatch):
    """A 2.1 bond [leader/left, follower/right, follower/sub] sends the LEADER
    a roster of BOTH followers (right + sub) and a peer_addr pointing at the
    RIGHT follower (the L/R sibling) — NOT the sub — so swap/trim stay on the
    stereo pair. Each follower gets an explicit empty roster."""
    h, bodies = _drive_bond([
        {"addr": "", "role": "leader", "channel": "left"},
        {"addr": "192.168.1.9", "role": "follower", "channel": "right",
         "name": "Right"},
        {"addr": "192.168.1.8", "role": "follower", "channel": "sub",
         "name": "Sub", "crossover_hz": 90},
    ], monkeypatch)
    assert h.status == 200
    leader_body = bodies[""]
    assert leader_body["roster"] == [
        {"addr": "192.168.1.9", "name": "Right", "channel": "right"},
        {"addr": "192.168.1.8", "name": "Sub", "channel": "sub"},
    ]
    # Primary L/R sibling = the RIGHT follower, never the sub.
    assert leader_body["peer_addr"] == "192.168.1.9"
    assert leader_body["peer_name"] == "Right"
    # Both followers carry an explicit empty roster (no stale roster after a
    # role flip).
    assert bodies["192.168.1.9"]["roster"] == []
    assert bodies["192.168.1.8"]["roster"] == []
    assert all(body["subwoofer_present"] is True for body in bodies.values())
    assert all(body["mains_highpass_enabled"] is True for body in bodies.values())
    assert all(body["crossover_hz"] == 90 for body in bodies.values())


def test_bond_two_member_pair_sets_peer_and_single_roster(monkeypatch):
    """A 2-member stereo pair still sets peer_addr=the follower AND a
    one-entry roster naming it (so unbond's roster path disables it too)."""
    h, bodies = _drive_bond([
        {"addr": "", "role": "leader", "channel": "left"},
        {"addr": "192.168.1.9", "role": "follower", "channel": "right",
         "name": "JTS3"},
    ], monkeypatch)
    assert h.status == 200
    leader_body = bodies[""]
    assert leader_body["peer_addr"] == "192.168.1.9"
    assert leader_body["peer_name"] == "JTS3"
    assert leader_body["roster"] == [
        {"addr": "192.168.1.9", "name": "JTS3", "channel": "right"},
    ]


def test_unbond_with_full_roster_disables_self_and_all_members(monkeypatch):
    """When the leader's grouping carries a full roster (N followers), unbond
    disables self + EVERY roster member — even a sub — and skips the
    discovery/peer_addr path entirely (the roster is authoritative)."""
    h, posts = _post_unbond(
        monkeypatch=monkeypatch,
        self_grouping={
            "enabled": True, "role": "leader", "bond_id": "bond-1",
            "peer_addr": "192.168.1.9", "peer_name": "Right",
            "roster": [
                {"addr": "192.168.1.9", "name": "Right", "channel": "right"},
                {"addr": "192.168.1.8", "name": "Sub", "channel": "sub"},
            ],
        },
        # A foreign claimer is reachable on the SAME bond — the roster path
        # must ignore discovery entirely, so it is never disabled.
        speakers=[{"address": "192.168.1.162", "name": "Interloper"}],
        peer_grouping={
            "192.168.1.162": {"enabled": True, "bond_id": "bond-1"},
        },
    )
    assert h.status == 200
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is True
    # Self ("") + both roster members; the foreign claimer (192.168.1.162) is
    # untouched. The peer fan-out is concurrent (a thread pool), so the two
    # roster addrs arrive in a non-deterministic order — assert the disabled SET,
    # not a sequence (an exact-order assert flaked py3.12-vs-py3.13).
    assert {a for a, _ in posts} == {"", "192.168.1.9", "192.168.1.8"}
    assert len(posts) == 3
    assert all(b == {"enabled": False, "trim_db": 0.0} for _a, b in posts)


def test_mains_highpass_toggle_fans_out_to_full_roster(monkeypatch):
    posts: list[tuple[str, dict]] = []

    self_grouping = {
        **_OFF_GROUPING,
        "enabled": True,
        "role": "leader",
        "channel": "left",
        "bond_id": "bond-1",
        "leader_addr": "",
        "crossover_hz": 90.0,
        "subwoofer_present": True,
        "mains_highpass_enabled": True,
        "roster": [
            {"addr": "192.168.1.9", "name": "Right", "channel": "right"},
            {"addr": "192.168.1.8", "name": "Sub", "channel": "sub"},
        ],
    }
    peer_groupings = {
        "192.168.1.9": {
            **self_grouping,
            "role": "follower",
            "channel": "right",
            "leader_addr": "jts.local",
            "roster": [],
        },
        "192.168.1.8": {
            **self_grouping,
            "role": "follower",
            "channel": "sub",
            "leader_addr": "jts.local",
            "roster": [],
        },
    }

    def fake_post(addr, body, known=None, *, token=None, household=None):
        posts.append((addr, body))
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "guard_mutating_request",
                        lambda *a, **k: True)
    monkeypatch.setattr(rooms_setup, "read_grouping_state",
                        lambda *a, **k: self_grouping)
    monkeypatch.setattr(rooms_setup, "_get_member_grouping",
                        lambda addr, known=None: peer_groupings[addr])
    monkeypatch.setattr(rooms_setup, "post_grouping_to_member", fake_post)
    monkeypatch.setattr(rooms_setup, "self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_leader_handle", lambda: "jts.local")

    handler_cls = rooms_setup._make_handler()
    h = _FakeHandler("/mains-highpass")
    payload = json.dumps({"enabled": False}).encode()
    h.headers["Content-Length"] = str(len(payload))
    h.rfile = BytesIO(payload)
    handler_cls.do_POST(h)

    assert h.status == 200
    assert {addr for addr, _body in posts} == {"", "192.168.1.9", "192.168.1.8"}
    assert all(body["mains_highpass_enabled"] is False for _a, body in posts)
    assert all(body["subwoofer_present"] is True for _a, body in posts)
    assert all(body["crossover_hz"] == 90.0 for _a, body in posts)
    assert {body["channel"] for _a, body in posts} == {"left", "right", "sub"}


_NODE = shutil.which("node")
_GROUPING_VIEW_TEST = _REPO / "tests" / "js" / "rooms_grouping_view_test.mjs"
_PAIR_BALANCE_CONTROLLER_TEST = (
    _REPO / "tests" / "js" / "rooms_pair_balance_controller_test.mjs"
)
_DOM_APPEND_CHILDREN_TEST = _REPO / "tests" / "js" / "dom_test.mjs"


def test_dom_append_children_export_via_node():
    """main.js's bond-card summary calls appendChildren(currentSummary,
    summarize(g)); appendChildren must stay exported from the shared dom.js and
    flatten summarize()'s nested-array fallback (native Element.append() would
    stringify it). Un-exporting it reintroduced a ReferenceError on every
    bonded poll. Skips when node isn't on PATH."""
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_DOM_APPEND_CHILDREN_TEST)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True


def test_grouping_view_pure_helpers_via_node():
    """The /rooms bond-card presentation logic (grouping-view.js
    airplayLipSyncRow) is a dependency-free module unit-tested with node —
    mirroring active_speaker_ui_test.mjs. Covers the AirPlay-lip-sync row
    branches (no row off-leader, Synced vs Lagging, ms formatting) that the
    DOM-assembly in main.js consumes. Skips when node isn't on PATH."""
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_GROUPING_VIEW_TEST)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True


def test_pair_balance_controller_via_node():
    """The pair-balance save state machine is dependency-free and executable.

    This pins the user-visible contract main.js wires into the slider: live
    saves while dragging, queued latest-value commits, failed-write rollback to
    confirmed backend state, and poll reconciliation that does not snap a dirty
    slider backward.
    """
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_PAIR_BALANCE_CONTROLLER_TEST)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True


# ---------------------------------------------------------------------------
# C7-3: Python↔JS balance-trim parity contract
# ---------------------------------------------------------------------------

_BALANCE_TRIM_FIXTURE = _REPO / "tests" / "fixtures" / "balance_trim_parity_fixture.json"
_BALANCE_TRIM_PARITY_SCRIPT = _REPO / "scripts" / "check-balance-trim-parity.mjs"


def test_balance_trim_python_matches_fixture():
    """Python _balance_trims_from_db must agree with the shared contract fixture.

    The fixture is the bridge between the Python backend and the JS frontend.
    If this test fails, update the fixture from the authoritative Python
    implementation, then verify that check-balance-trim-parity.mjs also passes
    (i.e. the JS side was updated to match).
    """
    from jasper.web.rooms_setup import _balance_trims_from_db

    fixture = json.loads(_BALANCE_TRIM_FIXTURE.read_text())
    for case in fixture["cases"]:
        balance_db = case["balance_db"]
        left, right, clamped = _balance_trims_from_db(balance_db)
        assert left == case["left"], (
            f"Python left trim mismatch at balance_db={balance_db}: "
            f"got {left}, expected {case['left']}"
        )
        assert right == case["right"], (
            f"Python right trim mismatch at balance_db={balance_db}: "
            f"got {right}, expected {case['right']}"
        )
        assert clamped == case["clamped"], (
            f"Python clamped flag mismatch at balance_db={balance_db}: "
            f"got {clamped}, expected {case['clamped']}"
        )


def test_balance_trim_js_matches_fixture_via_node():
    """JS trimsForBalance must agree with the shared contract fixture (C7-3).

    Runs scripts/check-balance-trim-parity.mjs which imports grouping-view.js
    and sweeps the same balance_db values Python was tested against above.
    If this test fails alongside test_balance_trim_python_matches_fixture the
    fixture itself is stale; if only this test fails, grouping-view.js drifted.
    """
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_BALANCE_TRIM_PARITY_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
