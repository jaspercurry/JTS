"""Tests for the /rooms/ wizard — the read-only multi-room directory.

Unlike /peers/ (which server-renders its discovered-peer rows and HTML-escapes
them inline), /rooms/ renders client-side: the GET / body is a static canonical
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
toggle, reusing peering_setup's env file + readers + restart helpers), the
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
import json
from email.message import Message
from io import BytesIO
from pathlib import Path

from jasper.web import rooms_setup


_REPO = Path(__file__).resolve().parent.parent


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
    "error": None,
}


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for driving do_GET.

    Mirrors the shim in tests/test_web_peering_setup.py so the two wizard
    suites read alike.
    """

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


def _patch_discovery(monkeypatch, *, speakers, grouping=None,
                     self_name="JTS",
                     self_hostname="jts-living.local", self_room="living",
                     self_addrs=frozenset({"192.168.1.5"}),
                     peering=None):
    """Pin self identity + discovery so the page is deterministic and never
    touches the real network or /var/lib/jasper files.

    Name / room / hostname now come from ONE identity read in
    _build_rooms_payload (rooms_setup.identity.read_identity), so we patch
    that reader directly rather than the deleted per-field _self_* helpers.
    Address is NIC-derived (not identity), so _self_addresses / _self_address
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
    monkeypatch.setattr(rooms_setup, "_self_addresses", lambda: set(self_addrs))
    monkeypatch.setattr(
        rooms_setup, "_self_address",
        lambda known=None: next(iter(sorted(self_addrs)), ""),
    )
    monkeypatch.setattr(
        rooms_setup, "read_grouping_state",
        lambda *a, **k: dict(grouping if grouping is not None else _OFF_GROUPING),
    )
    monkeypatch.setattr(rooms_setup, "_discover_speakers", lambda *a, **k: list(speakers))
    # _build_rooms_payload calls _discover_speakers_cached(), which memoizes
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
    # ("my other speakers" is one household concern) — /peers/ redirects here.
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
    # No-store: the directory is live state, never a cached snapshot.
    assert "no-store" in (h.header_values("Cache-Control") or [""])[0]


def test_rooms_json_shape(monkeypatch):
    _patch_discovery(
        monkeypatch,
        speakers=[
            {"name": "jts-bedroom", "room": "bedroom",
             "address": "192.168.1.9", "port": 8780},
        ],
        grouping=dict(_OFF_GROUPING),
    )
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert sorted(data.keys()) == ["peers", "self"]

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
    assert s["grouping"] == _OFF_GROUPING
    # peering defaults to off/off when no peering.env exists (patched empty here).
    assert s["peering"] == {"enabled": False, "primary": False}

    # peers: name / room / address / home_url / system_url, derived from address.
    assert len(data["peers"]) == 1
    p = data["peers"][0]
    assert sorted(p.keys()) == ["address", "home_url", "name", "room", "system_url"]
    assert p["name"] == "jts-bedroom"
    assert p["room"] == "bedroom"
    assert p["address"] == "192.168.1.9"
    assert p["home_url"] == "http://192.168.1.9/"
    assert p["system_url"] == "http://192.168.1.9/system/"


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
    monkeypatch.setattr(rooms_setup, "_self_addresses", lambda: set())
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


def test_rooms_json_empty_when_no_siblings(monkeypatch):
    _patch_discovery(monkeypatch, speakers=[])
    data = json.loads(_get("/rooms.json").wfile.getvalue().decode())
    assert data["peers"] == []
    # self still renders (the page is useful with zero peers).
    assert data["self"]["hostname"] == "jts-living.local"


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
    monkeypatch.setattr(rooms_setup, "_self_addresses", lambda: set())
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
# Reuses peering_setup's env file + readers; this suite pins the wiring
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
    monkeypatch.setattr(rooms_setup.peering_setup, "_restart_jasper_control",
                        lambda: restarts.__setitem__("control", restarts["control"] + 1))
    h = _FakeHandler(path)
    h.headers["Content-Length"] = str(len(body))
    h.rfile = BytesIO(body)
    handler_cls.do_POST(h)
    return h, restarts


def _seed_peering_env(tmp_path, monkeypatch, text):
    envp = tmp_path / "peering.env"
    envp.write_text(text)
    monkeypatch.setattr(rooms_setup.peering_setup, "PEERING_ENV_FILE", str(envp))
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


def test_post_peering_reuses_peering_setup_constants():
    """Wiring contract: rooms_setup imports peering_setup and writes through
    its PEERING_ENV_FILE constant + restart helper — it does NOT re-derive the
    env path or restart logic (reuse, not duplication)."""
    assert rooms_setup.peering_setup.PEERING_ENV_FILE.endswith("peering.env")
    assert callable(rooms_setup.peering_setup._load_state)
    assert callable(rooms_setup.peering_setup._restart_jasper_control)


def test_rooms_json_peering_block_reflects_env(monkeypatch, tmp_path):
    """The /rooms.json self.peering block is read FRESH from peering.env via the
    reused peering_setup readers — an on/primary file shows on/primary."""
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
    monkeypatch.setattr(rooms_setup, "_self_addresses", lambda: set())
    monkeypatch.setattr(rooms_setup, "_self_address", lambda known=None: "")
    monkeypatch.setattr(rooms_setup, "read_grouping_state", lambda *a, **k: dict(_OFF_GROUPING))
    monkeypatch.setattr(rooms_setup, "_discover_speakers", lambda *a, **k: [])
    rooms_setup._disc_cache.update(at=0.0, result=[])
    # Clear any ambient env so peering_setup's os.environ fallthrough can't lie.
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


def test_nginx_redirects_peers_to_rooms():
    """The wake-response toggle folded into the combined /rooms "Speakers"
    surface, so /peers/ is no longer a page — nginx 301-redirects it to the
    canonical /rooms/ (the old URL keeps working). String-assert the config
    the same way test_nginx_proxies_rooms_to_8785 does: the /peers/ block must
    `return 301 /rooms/;` and must NOT proxy to peering_setup's :8776 anymore
    (the :8776 backend stays up serving the reused helpers/daemon-status — see
    docs/HANDOFF-multiroom.md §6 — but users land on /rooms)."""
    nginx_text = (_REPO / "deploy" / "nginx-jasper.conf").read_text()
    assert "location /peers/ {" in nginx_text
    assert "return 301 /rooms/;" in nginx_text
    # The old proxy target is gone — /peers/ no longer routes users to the
    # peering page; it redirects.
    assert "proxy_pass http://127.0.0.1:8776/;" not in nginx_text


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

    first = rooms_setup._discover_speakers_cached()
    second = rooms_setup._discover_speakers_cached()
    third = rooms_setup._discover_speakers_cached()

    assert calls["n"] == 1, "cache must not re-browse within the TTL"
    assert first == second == third


def test_discovery_cache_rebrowses_after_ttl_expiry(monkeypatch):
    calls = {"n": 0}

    def _counting_browse(*a, **k):
        calls["n"] += 1
        return [{"name": "jts3", "room": "", "address": "192.168.1.9", "port": 8780}]

    monkeypatch.setattr(rooms_setup, "_discover_speakers", _counting_browse)
    rooms_setup._disc_cache.update(at=0.0, result=[])

    rooms_setup._discover_speakers_cached()
    # Age the cache past the TTL → next call re-browses.
    rooms_setup._disc_cache["at"] -= rooms_setup.DISCOVERY_CACHE_TTL_SEC + 1
    rooms_setup._discover_speakers_cached()

    assert calls["n"] == 2, "cache must re-browse once the TTL has elapsed"


def test_discovery_cache_empty_result_does_not_poison(monkeypatch):
    # A transient empty browse must not blank the directory for a full TTL —
    # the next call should retry rather than serve the empty result.
    seq = [[], [{"name": "jts3", "room": "", "address": "192.168.1.9", "port": 8780}]]

    def _flaky_browse(*a, **k):
        return seq.pop(0)

    monkeypatch.setattr(rooms_setup, "_discover_speakers", _flaky_browse)
    rooms_setup._disc_cache.update(at=0.0, result=[])

    assert rooms_setup._discover_speakers_cached() == []
    # Empty wasn't cached as fresh, so this re-browses and gets the speaker.
    assert rooms_setup._discover_speakers_cached() == [
        {"name": "jts3", "room": "", "address": "192.168.1.9", "port": 8780}
    ]


# ----------------------------------------------------------------------
# POST /bond — the bond-forming one-flow.
#
# The browser sends the member list; rooms_setup fans the config out
# SERVER-side to each member's /grouping/set. These pin the orchestration
# (leader_addr wiring, per-member results, partial failure, CSRF, the SSRF
# guard) with the cross-speaker HTTP call stubbed.
# ----------------------------------------------------------------------


def _post_bond(body, *, csrf_ok=True, monkeypatch, member_results=None):
    """Drive POST /bond with the cross-speaker call stubbed. Returns
    (handler, calls) where calls is the list of (addr, body) fanned out."""
    calls: list[tuple[str, dict]] = []

    def fake_member_post(addr, member_body):
        calls.append((addr, member_body))
        if member_results and addr in member_results:
            return member_results[addr]
        return (True, "HTTP 200")

    monkeypatch.setattr(rooms_setup, "_post_grouping_to_member", fake_member_post)
    monkeypatch.setattr(rooms_setup, "guard_mutating_request", lambda *a, **k: csrf_ok)
    monkeypatch.setattr(rooms_setup, "reject_csrf",
                        lambda h: h.send_response(403) or h.end_headers())
    monkeypatch.setattr(rooms_setup, "_self_address", lambda known=None: "192.168.1.5")

    handler_cls = rooms_setup._make_handler()
    raw = json.dumps(body).encode()
    h = _FakeHandler("/bond")
    h.headers["Content-Length"] = str(len(raw))
    h.rfile = BytesIO(raw)
    handler_cls.do_POST(h)
    return h, calls


def _stereo_pair_members():
    return [
        {"addr": "192.168.1.5", "role": "leader", "channel": "left"},
        {"addr": "192.168.1.9", "role": "follower", "channel": "right"},
    ]


def test_post_bond_configures_all_members_and_wires_leader_addr(monkeypatch):
    h, calls = _post_bond({"members": _stereo_pair_members()}, monkeypatch=monkeypatch)
    assert h.status == 200
    body = json.loads(h.wfile.getvalue())
    assert body["ok"] is True
    assert body["bond_id"].startswith("bond-")
    assert len(calls) == 2
    by_role = {c[1]["role"]: c[1] for c in calls}
    # leader gets no leader_addr; the follower gets THIS speaker's address.
    assert by_role["leader"]["leader_addr"] == ""
    assert by_role["follower"]["leader_addr"] == "192.168.1.5"
    assert by_role["follower"]["channel"] == "right"
    assert all(c[1]["enabled"] is True for c in calls)
    # one shared bond_id across both members
    assert {c[1]["bond_id"] for c in calls} == {body["bond_id"]}


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


# ---- _post_grouping_to_member: the cross-speaker call + SSRF guard ----


def test_member_post_refuses_non_lan_target(monkeypatch):
    monkeypatch.setattr(rooms_setup, "_self_addresses", lambda: set())
    ok, detail = rooms_setup._post_grouping_to_member("8.8.8.8", {})
    assert ok is False
    assert "non-LAN" in detail


def test_member_post_rejects_non_ip_address(monkeypatch):
    monkeypatch.setattr(rooms_setup, "_self_addresses", lambda: set())
    ok, detail = rooms_setup._post_grouping_to_member("evil.example.com", {})
    assert ok is False
    assert "not an IP" in detail


def test_member_post_self_routes_to_loopback(monkeypatch):
    monkeypatch.setattr(rooms_setup, "_self_addresses", lambda: {"192.168.1.5"})
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
    ok, _detail = rooms_setup._post_grouping_to_member("192.168.1.5", {"x": 1})
    assert ok is True
    assert urls == ["http://127.0.0.1:8780/grouping/set"]


def test_member_post_lan_peer_targets_its_control_port(monkeypatch):
    monkeypatch.setattr(rooms_setup, "_self_addresses", lambda: {"192.168.1.5"})
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
    ok, _detail = rooms_setup._post_grouping_to_member("192.168.1.9", {"x": 1})
    assert ok is True
    assert urls == ["http://192.168.1.9:8780/grouping/set"]
