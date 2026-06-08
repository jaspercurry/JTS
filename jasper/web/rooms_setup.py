"""/rooms/ — the "Speakers" surface: directory + wake-response toggle.

To the household, "my other speakers" is ONE concern, so the read-only
multi-room directory and the wake-arbitration (peering) toggle live on
the same page. /peers/ 301-redirects here (see deploy/nginx-jasper.conf);
this page is canonical. The page title is "Speakers".

Two parts:

1. READ-ONLY directory (per docs/HANDOFF-multiroom.md §6): "see every JTS
   speaker on the LAN and click through to configure each on its own web
   UI, plus show this speaker's grouping status." Bond-forming controls
   (stereo pair / 2.1 / sub, role/leader/channel) would write grouping
   config that does nothing until the P1 sync engine lands — shipping a
   toggle that silently no-ops is dishonest — so the directory stays
   surface-and-links with an honest forward note (§8). The bond write
   surface arrives once the on-hardware sync spike validates the engine.

2. WAKE-RESPONSE toggle (peering): when several speakers hear "Hey
   Jarvis", only one answers. This is a real, working control. The page
   read-modify-writes /var/lib/jasper/peering.env via POST /peering,
   REUSING jasper/web/peering_setup.py's readers/constants/restart
   helpers — it does NOT duplicate the env parse/write/restart. The /peers/
   peering_setup module + its :8776 socket stay wired (they still serve
   those helpers and the peering daemon status); the redirect only stops
   routing users to its page.

Room is NOT edited here. Room lives in the speaker-identity home
(/speaker/); the self card shows it (read via identity.read_identity)
and links there to change name + room. Adding a room editor here would
reopen the two-homes drift docs/HANDOFF-multiroom.md §6 just closed.

Discovery uses the ALWAYS-ON `_jasper-control._tcp` mDNS service
(advertised unconditionally by deploy/avahi/jasper-control.service,
installed by install.sh independent of peering) — NOT the
wake-peering-gated `_jasper-peer._udp` (which only exists when
JASPER_PEERING=on). So the directory lists every speaker regardless of
whether wake-peering is enabled.

The page renders client-side: the body is a single `#app` mount point
plus a `type="module"` script. The static ES module at
/assets/rooms/js/main.js fetches /rooms.json on load + every 7 s and
builds the self card, the wake-response card, the per-peer click-through
cards, and the honest forward note — all via DOM/text APIs (every peer
field is mDNS-provided → untrusted), so this server does no interpolation
of discovered data.

URL surface (after nginx strips the /rooms/ prefix):
  GET  /            page render (mount point + ES module)
  GET  /rooms.json  the directory + self status incl. the wake-response
                    `peering` block (the module fetches this on a poll)
  POST /peering     write the wake-response state into peering.env +
                    restart voice/control (JSON body, CSRF-verified)
"""
from __future__ import annotations

import argparse
import asyncio  # noqa: F401 — kept so tests can patch rooms_setup.asyncio.run
import json
import logging
import os
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import identity
from ..mdns import browse_once
from ..multiroom.state import read_grouping_state
from . import peering_setup
from ._common import (
    begin_request,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    write_env_file,
)

logger = logging.getLogger(__name__)


ROOMS_PAGE_CSS_HREF = "/assets/rooms/rooms.css"

# The always-on jasper-control mDNS service. Fully-qualified with the
# trailing `.local.` per the python-zeroconf contract. Advertised by
# every JTS regardless of wake-peering state (see module docstring).
CONTROL_MDNS_TYPE = "_jasper-control._tcp.local."

# jasper-control's HTTP port — the SRV record carries it, but we default
# to it when an instance resolves without one. The management UI lives on
# port 80 (nginx), so the click-through URLs use the bare address.
CONTROL_HTTP_PORT = 8780

# How long to browse for sibling speakers. python-zeroconf re-broadcasts
# with backoff (1s, 2s, 4s); 2s captures the common PTR→SRV→TXT roundtrip
# on a home LAN without making the first paint feel slow.
DISCOVERY_TIMEOUT_SEC = 2.0

# The module re-polls /rooms.json every 7 s. A *fresh* zeroconf browse per
# poll would stand up + tear down a multicast listener ~8×/min for as long
# as the page is open — needless socket churn that also contends with the
# host's avahi responder. The household speaker set is near-static, so we
# cache the browse result in-process and only re-browse when it ages out
# (mirrors the GBFS feed cache in jasper/citibike.py). A new speaker shows
# within one TTL; on page open the first poll still does a live browse.
DISCOVERY_CACHE_TTL_SEC = 30.0


# ----------------------------------------------------------------------
# Self identity.
# ----------------------------------------------------------------------
#
# Name / room / hostname all come from the single shared identity reader
# (jasper.identity.read_identity), read ONCE per request in
# _build_rooms_payload — there are deliberately no per-field _self_name /
# _self_room / _self_hostname helpers, since each would re-read identity and
# the three fields must agree within one render. Only the LAN address is a
# /rooms-local concern (NIC-derived, NOT part of identity), so _self_addresses
# / _self_address stay below.


def _self_addresses() -> set[str]:
    """Best-effort set of this host's own LAN IPv4 addresses, used to drop
    self from the discovered-peer list. Never raises — a failure just
    yields a smaller set (worst case: a self-row leaks in, which the page
    renders harmlessly)."""
    addrs: set[str] = set()
    # Primary outbound interface — the UDP-connect trick. No packet is
    # sent; connect() on a datagram socket just picks the route's source
    # address. 192.0.2.0/24 is TEST-NET-1 (RFC 5737), never routed.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        addrs.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    # Everything getaddrinfo knows about our hostname (catches a second
    # NIC the route trick missed).
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return {a for a in addrs if a and not a.startswith("127.")}


def _self_address(known: set[str] | None = None) -> str:
    """A representative LAN address for the self card. Empty string when we
    genuinely can't resolve one (the module renders it as a dash)."""
    pool = known if known is not None else _self_addresses()
    return next(iter(sorted(pool)), "")


# ----------------------------------------------------------------------
# Discovery — browse the always-on _jasper-control._tcp service.
# ----------------------------------------------------------------------


def _strip_service_type(full_name: str) -> str:
    """Turn a full mDNS instance name into a display label. Mirrors
    jasper.speaker_name_discovery._strip_service_type: drop the trailing
    `._jasper-control._tcp.local.` suffix and unescape avahi's `\\032`
    space encoding."""
    name = full_name.rstrip(".")
    suffix = "." + CONTROL_MDNS_TYPE.rstrip(".")
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name.replace("\\032", " ").strip()


def _hostname_label(server: str) -> str:
    """A clean speaker label from an mDNS SRV target host, e.g.
    `"jts3.local."` -> `"jts3"`. Empty string when there's nothing usable."""
    host = (server or "").rstrip(".")
    if host.endswith(".local"):
        host = host[: -len(".local")]
    return host.strip()


def _peer_label(props: dict, server: str, full_name: str) -> str:
    """Pick the directory label for a discovered speaker, best-first:
      1. an explicit `name=` TXT record (none on `_jasper-control._tcp`
         today, but honoured so a future advertisement Just Works),
      2. the resolved SRV hostname (`jts3.local.` -> `jts3`),
      3. the raw mDNS instance name as a last resort.
    Without this, the default instance name (`"JTS jasper-control on jts"`)
    leaks into the UI as the speaker's label — verbose and confusing."""
    txt = (props.get("name") or "").strip()
    if txt:
        return txt
    host = _hostname_label(server)
    if host:
        return host
    return _strip_service_type(full_name)


def _discover_speakers(timeout: float = DISCOVERY_TIMEOUT_SEC) -> list[dict]:
    """Best-effort mDNS-SD browse of `_jasper-control._tcp`. Returns a list
    of {name, room, address, port} dicts — at most one per service name.
    Returns [] on any failure so the page renders an empty state, never
    500s.

    The browse/resolve/parse mechanics live in the shared one-shot primitive
    `jasper.mdns.browse_once` (which is fail-soft: any failure degrades to an
    empty list, never raises). What stays here is rooms-display *policy*:
    picking the directory label via `_peer_label` (TXT `name=` vs SRV host vs
    stripped instance name) and defaulting the port to jasper-control's.

    Self is NOT filtered here (the caller does it against _self_addresses so
    the filter stays testable). `room` is "" until TXT records are added to
    the avahi advertisement — today the service carries only type + port, so
    a discovered instance yields name + address + port and the page falls
    back to the SRV hostname as the label."""
    out: list[dict] = []
    for svc in browse_once(CONTROL_MDNS_TYPE, timeout=timeout):
        # browse_once drops address-less instances, so svc.addresses is
        # non-empty; guard anyway since this is the only field we hard-require.
        if not svc.addresses:
            continue
        out.append(
            {
                "name": _peer_label(svc.txt, svc.server, svc.name),
                "room": (svc.txt.get("room") or "").strip(),
                "address": str(svc.addresses[0]),
                "port": int(svc.port or CONTROL_HTTP_PORT),
            }
        )
    return out


# In-process TTL cache so the 7 s /rooms.json poll reuses one browse
# instead of churning a fresh zeroconf listener every request. The wizard
# is a shared (socket-activated) process, so this persists across polls.
_disc_lock = threading.Lock()
_disc_cache: dict = {"at": 0.0, "result": []}


def _discover_speakers_cached() -> list[dict]:
    """`_discover_speakers()` behind a TTL cache (DISCOVERY_CACHE_TTL_SEC).

    The lock serializes the live browse so concurrent /rooms.json requests
    don't each kick one off; whoever holds it refreshes, the rest get the
    just-cached value. Total/fail-soft: inherits `_discover_speakers`'s
    return-[] behaviour."""
    with _disc_lock:
        now = time.monotonic()
        if _disc_cache["result"] and (now - _disc_cache["at"]) < DISCOVERY_CACHE_TTL_SEC:
            return _disc_cache["result"]
        result = _discover_speakers()
        # Only refresh the timestamp/result on a non-empty browse so a
        # transient empty scan doesn't blank the directory for a full TTL;
        # an empty result falls through to a re-browse on the next poll.
        if result:
            _disc_cache["at"] = now
            _disc_cache["result"] = result
            return result
        return _disc_cache["result"]


def _read_peering_block() -> dict:
    """This speaker's wake-response (peering) state for /rooms.json, read
    FRESH from /var/lib/jasper/peering.env on every call. REUSES
    peering_setup's readers (_load_state / _is_on / _primary) — the env
    parse is NOT re-derived here.

    Returns {"enabled": bool, "primary": bool}:
      enabled  — JASPER_PEERING is on (the speaker participates in wake
                 arbitration so only one device answers "Hey Jarvis").
      primary  — JASPER_PEER_PRIMARY is set (small bias to win ties).

    Fail-soft: peering_setup._load_state already returns {} on a
    missing/unreadable file (→ enabled=False, primary=False), so this
    never raises.

    The path is passed explicitly (rather than relying on _load_state's
    def-time default) so it's resolved at call time — fresh on every poll,
    and overridable in tests by patching peering_setup.PEERING_ENV_FILE."""
    state = peering_setup._load_state(peering_setup.PEERING_ENV_FILE)
    return {
        "enabled": peering_setup._is_on(state),
        "primary": peering_setup._primary(state),
    }


def _build_rooms_payload() -> dict:
    """Assemble the /rooms.json body: this speaker's identity + grouping
    status + wake-response state, plus the sibling directory (self
    excluded). Total — the discovery, grouping, and peering reads are each
    fail-soft, so this never raises.

    The self block's name / hostname / room all originate from ONE read of
    the shared identity reader (jasper.identity.read_identity, called once
    here) so /rooms agrees with control_advert and the rest of the speaker
    on "who is this speaker" and the three fields are internally consistent
    within a single render; `address` stays best-effort from this host's own
    NICs (_self_addresses) since that's a /rooms-local concern, not identity.
    The `peering` block is read FRESH from peering.env each call (via the
    reused peering_setup readers) so a save through POST /peering reflects on
    the next 7 s poll.

    Shape (consumed by /assets/rooms/js/main.js):
      {
        "self": {name, hostname, room, address,
                 grouping: <read_grouping_state() dict>,
                 peering: {enabled, primary}},
        "peers": [{name, room, address, home_url, system_url}, ...]
      }
    """
    me = identity.read_identity()
    own = _self_addresses()
    self_block = {
        "name": me.name,
        "hostname": me.hostname,
        "room": me.room,
        "address": _self_address(own),
        "grouping": read_grouping_state(),
        "peering": _read_peering_block(),
    }

    peers: list[dict] = []
    self_hostname_label = me.hostname.split(".")[0].casefold()
    for s in _discover_speakers_cached():
        addr = s.get("address") or ""
        # Drop self: by address (reliable — our own NIC IPs) or by the
        # instance-name label matching our hostname (catches the case where
        # the route trick missed our address).
        if addr and addr in own:
            continue
        label = (s.get("name") or "").casefold()
        if self_hostname_label and self_hostname_label in label:
            continue
        peers.append(
            {
                "name": s.get("name") or "",
                "room": s.get("room") or "",
                "address": addr,
                "home_url": f"http://{addr}/" if addr else "",
                "system_url": f"http://{addr}/system/" if addr else "",
            }
        )
    # Stable, human-friendly ordering for the directory.
    peers.sort(
        key=lambda p: (p.get("room") or "", p.get("name") or "", p.get("address") or "")
    )
    return {"self": self_block, "peers": peers}


# ----------------------------------------------------------------------
# HTML rendering.
# ----------------------------------------------------------------------
#
# Migrated to the canonical design system (canonical_page + canonical_header
# + /assets/app.css). The body is a single `#app` mount point plus the
# page's ES module — the module clears the mount and builds every card
# (self status, per-peer click-throughs, the honest forward note) from
# /rooms.json. Page-specific visuals live in deploy/assets/rooms/rooms.css
# (page_css_href); shared component classes (.app-header / .info-card /
# .deflist / .badge / .btn) come from app.css. No inline <script> with
# behaviour — only the type="module" loader tag.


def _render_page(*, csrf_token: str = "") -> bytes:
    # `id="app"` is the contract the ES module mounts on
    # (document.getElementById("app")); it clears the placeholder on first
    # render. A visible placeholder means a failed module load degrades to
    # a message, not a silent blank.
    #
    # canonical_page emits the CSRF <meta name="jts-csrf"> tag the ES module
    # reads (via http.js jsonHeaders()) for the wake-response POST /peering.
    body = f"""
{canonical_header("Speakers")}
<main class="page">
  <div id="app" aria-busy="true">
    <p class="rooms-loading">Looking for speakers on this network…</p>
  </div>
</main>
<script type="module" src="/assets/rooms/js/main.js"></script>
"""
    return canonical_page(
        "Speakers", body,
        csrf_token=csrf_token,
        page_css_href=ROOMS_PAGE_CSS_HREF,
    )


# ----------------------------------------------------------------------
# Handlers.
# ----------------------------------------------------------------------


def _send_json(handler: BaseHTTPRequestHandler, payload: dict, *, status: int = 200) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


# Max JSON body on POST /peering. The real payload is ~30 B
# ({"enabled": true, "primary": false}); anything bigger is malformed or
# abusive and is rejected before we read it off the wire.
_PEERING_BODY_LIMIT = 4096


def _read_json_body(handler: BaseHTTPRequestHandler) -> tuple[dict | None, str | None]:
    """Read + parse a small JSON object body. Returns `(parsed, error)` —
    exactly one is non-None. Hard-caps at `_PEERING_BODY_LIMIT`. Mirrors
    wake_setup._read_json_body."""
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError:
        return None, "invalid Content-Length"
    if length < 0 or length > _PEERING_BODY_LIMIT:
        return None, "invalid body length"
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"invalid JSON body: {e}"
    if not isinstance(parsed, dict):
        return None, "body must be a JSON object"
    return parsed, None


def _save_peering(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /peering: write the wake-response state into peering.env
    and restart voice + jasper-control so both daemons pick it up.

    REUSES peering_setup throughout — the same PEERING_ENV_FILE, the same
    JASPER_PEERING / JASPER_PEER_PRIMARY keys, the same restart helpers — so
    there is ONE owner of the peering env contract.

    Read-modify-write: we load the existing file first and only touch the two
    wake-response keys, PRESERVING everything else (especially
    JASPER_PEER_ROOM, owned by /speaker/, plus operator-set arbitration knobs
    like JASPER_PEER_ARB_WINDOW_MS). write_env_file does a full-file replace,
    so without the merge a save would clobber those.

    Fail-soft: a parse/IO error returns a 4xx/5xx JSON error and never raises
    out of the handler. Structured event= logs on every outcome."""
    parsed, err = _read_json_body(handler)
    if err is not None:
        logger.warning("event=rooms.peering.save.reject reason=%s", err)
        _send_json(handler, {"ok": False, "error": err}, status=HTTPStatus.BAD_REQUEST)
        return

    enabled = bool(parsed.get("enabled"))
    primary = bool(parsed.get("primary"))

    # Read and write the SAME path expression so a merge can never read one
    # file and write another (which would clobber the keys we mean to
    # preserve). Resolve it once here rather than relying on _load_state's
    # def-time default.
    env_path = peering_setup.PEERING_ENV_FILE

    # Load-then-merge so JASPER_PEER_ROOM (owned by /speaker/) and any
    # operator tuning knobs survive (write_env_file is a full-file replace).
    values: dict[str, str] = dict(peering_setup._load_state(env_path))
    values["JASPER_PEERING"] = "on" if enabled else "off"
    if primary:
        values["JASPER_PEER_PRIMARY"] = "1"
    elif "JASPER_PEER_PRIMARY" in values:
        del values["JASPER_PEER_PRIMARY"]

    try:
        # mode=0o644 — no secrets, just config. Matches peering_setup._save.
        write_env_file(env_path, values, mode=0o644)
    except OSError as e:
        logger.exception("event=rooms.peering.save.error")
        _send_json(
            handler, {"ok": False, "error": f"write failed: {e}"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
        return

    logger.info(
        "event=rooms.peering.save mode=%s primary=%d",
        values["JASPER_PEERING"], int(primary),
    )

    # Restart both daemons — jasper-voice reads JASPER_PEERING to know whether
    # to call the peering UDS; jasper-control reads it to know whether to start
    # its peering daemon thread. Both restarts are best-effort / non-blocking
    # (the reused helpers use systemctl --no-block).
    restart_voice_daemon()
    peering_setup._restart_jasper_control()

    _send_json(
        handler,
        {"ok": True, "peering": {"enabled": enabled, "primary": primary}},
    )


def _make_handler():
    """Build the request handler class. No state-path binding — the
    directory pulls everything live (mDNS browse + grouping + peering SSOT),
    and POST /peering writes through the reused peering_setup file constant."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: ANN001, A003
            logger.info("rooms-wizard: " + fmt, *args)

        def do_GET(self):  # noqa: N802
            if self.path == "/" or self.path.startswith("/?"):
                ctx = begin_request(self)
                send_html_response(self, _render_page(csrf_token=ctx["csrf_token"]))
                return
            if self.path == "/rooms.json":
                _send_json(self, _build_rooms_payload())
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            # Route-check BEFORE the CSRF guard (project convention): a bogus
            # path 404s without revealing CSRF state.
            if self.path != "/peering":
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
                return
            # JSON fetch POST: guard_mutating_request checks the Host/Origin
            # and the X-CSRF-Token header (no form). Mirrors system_setup.
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            _save_peering(self)

    return _Handler


# ----------------------------------------------------------------------
# Server setup.
# ----------------------------------------------------------------------


def make_server(target) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer. `target` is either an (host, port)
    tuple (direct bind) or an already-bound socket (from systemd socket
    activation — see jasper/web/__main__.py)."""
    from ._systemd import make_http_server
    return make_http_server(target, _make_handler())


def main(argv: list[str] | None = None) -> int:
    """Direct CLI entrypoint — used for dev/testing outside systemd."""
    p = argparse.ArgumentParser(description="JTS rooms (multi-room directory) wizard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_ROOMS_WEB_PORT", "8785")),
    )
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port))
    logger.info("rooms wizard listening on http://%s:%d/", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
