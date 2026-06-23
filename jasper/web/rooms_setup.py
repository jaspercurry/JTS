# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""/rooms/ — the "Speakers" surface: directory + wake-response toggle.

To the household, "my other speakers" is ONE concern, so the read-only
multi-room directory and the wake-arbitration (peering) toggle live on
the same page. This page is canonical. The page title is "Speakers".

Two parts:

1. DIRECTORY + bond-forming (per docs/HANDOFF-multiroom.md §6): see every
   JTS speaker on the LAN, click through to configure each on its own web
   UI, see this speaker's grouping status (incl. the runtime-degraded health
   from §0), and **create a stereo pair in one flow** — pick the speaker for
   the right channel and Save. Bond-forming POSTs /bond, which fans the
   config out SERVER-side to each member's jasper-control /grouping/set (this
   speaker → leader/left, the picked one → follower/right). Configuration is
   automatic — no per-speaker tinkering. Perfect sample-lock across the pair
   is the remaining on-hardware validation, so the UI carries an honest
   "preview" note (§8) rather than pretending the audio half is done.

2. WAKE-RESPONSE toggle (peering): when several speakers hear "Hey
   Jarvis", only one answers. This is a real, working control. The page
   read-modify-writes /var/lib/jasper/peering.env via POST /peering,
   REUSING jasper.peering.config's readers/constants so it does NOT
   duplicate the env parse contract.

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
  POST /bond        form a stereo pair: mint a bond id, then fan the
                    grouping config out SERVER-side to each member's
                    jasper-control /grouping/set (JSON body, CSRF-verified)
  POST /unbond      dissolve the bond this speaker is in: disable self +
                    every sibling sharing this bond_id, SERVER-side via each
                    member's jasper-control /grouping/set (CSRF-verified)
  POST /swap        exchange a 2-speaker pair's left/right channels —
                    roles/bond untouched; partial failure rolls back, and a
                    stuck same-channel pair is REPAIRED to left/right rather
                    than rejected (CSRF-verified)
  POST /trim        nudge one member's pair-balance trim by ±delta_db
                    (target self|peer; attenuate-only, clamped; applied
                    via the member's /grouping/set) (CSRF-verified)
"""
from __future__ import annotations

import argparse
import asyncio  # noqa: F401 — kept so tests can patch rooms_setup.asyncio.run
import concurrent.futures
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import identity
from ..control import household_credential
from ..mdns import browse_once
from ..multiroom.airplay_latency import with_airplay_latency_fit
from ..multiroom.state import parse_grouping_response, read_grouping_state
from ..peering import config as peering_config
from ..log_event import log_event
from ._common import (
    begin_request,
    canonical_header,
    canonical_page,
    guard_read_request,
    guard_mutating_request,
    reject_csrf,
    restart_voice_daemon,
    restart_systemd_units,
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


def _leader_handle() -> str:
    """This speaker's STABLE address to hand a follower as ``leader_addr``.

    Returns the mDNS .local FQDN (jasper.identity.read_identity().hostname,
    e.g. ``jts.local``) — NOT a NIC IP. The follower's snapclient resolves it
    via mDNS, so the bond keeps working across DHCP lease churn that would
    invalidate a baked-in IP. (snapclient_argv in jasper/multiroom/reconcile.py
    passes leader_addr verbatim to ``snapclient --host``, which resolves a
    .local name fine — no reconcile change needed.) Distinct from
    _self_address, which stays NIC-derived for SSRF self-routing in
    _post_grouping_to_member / _lan_target."""
    return identity.read_identity().hostname


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


_LOCAL_HOST_LABEL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def _local_web_host(hostname: str) -> str:
    """Browser-safe management host for a discovered peer.

    The peer `address` remains the IP used by server-side grouping fan-out
    through the SSRF guard. User-facing click-through URLs should instead use
    the stable mDNS hostname so DHCP churn does not make the UI teach people
    raw IPs. Empty/invalid hostnames fail closed to no link.
    """
    host = (hostname or "").strip().rstrip(".")
    if host.endswith(".local"):
        host = host[: -len(".local")]
    if not _LOCAL_HOST_LABEL_RE.match(host):
        return ""
    return f"{host}.local"


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
                # The SRV hostname label (e.g. "jts3"), kept so self-exclusion
                # can match on the stable HOSTNAME (exact) rather than the
                # free-form display name — a speaker named "jts" must not drop
                # a peer named "jts3".
                "hostname": _hostname_label(svc.server),
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
    jasper.peering.config readers — the env parse is NOT re-derived here.

    Returns {"enabled": bool, "primary": bool}:
      enabled  — JASPER_PEERING is on (the speaker participates in wake
                 arbitration so only one device answers "Hey Jarvis").
      primary  — JASPER_PEER_PRIMARY is set (small bias to win ties).

    Fail-soft: peering_config.read_state already returns {} on a
    missing/unreadable file (→ enabled=False, primary=False), so this
    never raises.

    The path is passed explicitly (rather than relying on _load_state's
    def-time default) so it's resolved at call time — fresh on every poll,
    and overridable in tests by patching peering_config.PEERING_ENV_FILE."""
    state = peering_config.read_state(peering_config.PEERING_ENV_FILE)
    return {
        "enabled": peering_config.state_enabled(state),
        "primary": peering_config.state_primary(state),
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
    The `peering` block is read FRESH from peering.env each call (via
    jasper.peering.config) so a save through POST /peering reflects on the
    next 7 s poll.

    Shape (consumed by /assets/rooms/js/main.js):
      {
        "self": {name, hostname, room, address,
                 grouping: <read_grouping_state() dict
                            + airplay_latency_fit: {applicable, tight?, …}>,
                 peering: {enabled, primary}},
        "peers": [{name, room, address, home_url, system_url}, ...]
      }

    Peer `address` stays raw LAN IP for POST /bond / /swap / /trim control
    calls. Peer `home_url` / `system_url` are derived from the advertised
    hostname and end in `.local`, never from the IP address.
    """
    me = identity.read_identity()
    own = _self_addresses()
    self_block = {
        "name": me.name,
        "hostname": me.hostname,
        "room": me.room,
        "address": _self_address(own),
        # with_airplay_latency_fit attaches airplay_latency_fit ({applicable:
        # false} unless this speaker is an active bonded leader) — the same
        # composer /state uses, so /rooms shows the bonded-leader lip-sync
        # status without re-deriving it.
        "grouping": with_airplay_latency_fit(read_grouping_state()),
        "peering": _read_peering_block(),
    }

    peers: list[dict] = []
    self_hostname_label = me.hostname.split(".")[0].casefold()
    for s in _discover_speakers_cached():
        addr = s.get("address") or ""
        # Drop self two ways:
        #   1. by address — reliable when our own NIC IP is in `own`.
        #   2. by EXACT hostname-label match — catches the case where the
        #      UDP-route trick missed our address (e.g. a loopback/secondary
        #      advert). This MUST be an exact match on the SRV hostname, not
        #      a substring of the display name: a speaker "jts" once dropped a
        #      peer "jts3" because "jts" is a substring of "jts3" (and the
        #      display name is free-form, unrelated to identity).
        if addr and addr in own:
            continue
        peer_host = (s.get("hostname") or "").casefold()
        if self_hostname_label and peer_host == self_hostname_label:
            continue
        web_host = _local_web_host(s.get("hostname") or "")
        peers.append(
            {
                "name": s.get("name") or "",
                "room": s.get("room") or "",
                "address": addr,
                "home_url": f"http://{web_host}/" if web_host else "",
                "system_url": f"http://{web_host}/system/" if web_host else "",
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

    REUSES jasper.peering.config for the PEERING_ENV_FILE and state readers
    so there is ONE owner of the peering env contract.

    Read-modify-write: we load the existing file first and only touch the two
    wake-response keys, PRESERVING everything else (especially
    JASPER_PEER_ROOM, owned by /speaker/, plus operator-set arbitration knobs
    like JASPER_PEER_ARB_WINDOW_MS). write_env_file does a full-file replace,
    so without the merge a save would clobber those.

    Fail-soft: a parse/IO error returns a 4xx/5xx JSON error and never raises
    out of the handler. Structured event= logs on every outcome."""
    parsed, err = _read_json_body(handler)
    if err is not None:
        log_event(logger, "rooms.peering.save.reject", reason=err, level=logging.WARNING)
        _send_json(handler, {"ok": False, "error": err}, status=HTTPStatus.BAD_REQUEST)
        return

    enabled = bool(parsed.get("enabled"))
    primary = bool(parsed.get("primary"))

    # Read and write the SAME path expression so a merge can never read one
    # file and write another (which would clobber the keys we mean to
    # preserve). Resolve it once here rather than relying on _load_state's
    # def-time default.
    env_path = peering_config.PEERING_ENV_FILE

    # Load-then-merge so JASPER_PEER_ROOM (owned by /speaker/) and any
    # operator tuning knobs survive (write_env_file is a full-file replace).
    values: dict[str, str] = dict(peering_config.read_state(env_path))
    values["JASPER_PEERING"] = "on" if enabled else "off"
    if primary:
        values["JASPER_PEER_PRIMARY"] = "1"
    elif "JASPER_PEER_PRIMARY" in values:
        del values["JASPER_PEER_PRIMARY"]

    try:
        # mode=0o644 — no secrets, just config.
        write_env_file(env_path, values, mode=0o644)
    except OSError as e:
        log_event(logger, "rooms.peering.save.error", level=logging.ERROR, exc_info=True)
        _send_json(
            handler, {"ok": False, "error": f"write failed: {e}"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
        return

    log_event(
        logger,
        "rooms.peering.save",
        mode=values["JASPER_PEERING"],
        primary=int(primary),
    )

    # Restart both daemons — jasper-voice reads JASPER_PEERING to know whether
    # to call the peering UDS; jasper-control reads it to know whether to start
    # its peering daemon thread. Both restarts are best-effort / non-blocking
    # (restart_systemd_units uses systemctl --no-block).
    restart_voice_daemon()
    restart_systemd_units("jasper-control")

    _send_json(
        handler,
        {"ok": True, "peering": {"enabled": enabled, "primary": primary}},
    )


def _generate_bond_id() -> str:
    """A short, unique bond identifier. The bond_id is just an opaque label
    shared by a bond's members (the wizard auto-generates it; the user never
    types it)."""
    return "bond-" + uuid.uuid4().hex[:8]


def _lan_target(addr: str, known: set[str] | None = None) -> str | None:
    """Resolve ``addr`` to a host safe to call on the home LAN, or None to
    refuse it. The SSRF guard for every cross-speaker control call.

    ``addr`` empty or one of this host's own addresses → ``"127.0.0.1"``
    (configure self / talk to our own control API). A remote target must
    parse as a PRIVATE or loopback IPv4 — the control API is a home-LAN
    surface, never a public host, and bare hostnames are refused (no DNS
    rebind surface). Returns the host string on accept, None on refuse.
    Shared by _post_grouping_to_member (POST) and _get_member_grouping (GET)
    so both apply the EXACT same guard.

    ``known`` is this host's own addresses (the self-routing set). Pass a
    precomputed set — as the fan-out callers do — to compute it ONCE per
    operation instead of per peer (``_self_addresses`` does a socket probe +
    ``getaddrinfo``); mirrors :func:`_self_address`'s ``known=`` param. ``None``
    → computed fresh (the standalone-call default)."""
    if known is None:
        known = _self_addresses()
    if not addr or addr in known:
        return "127.0.0.1"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    if not (ip.is_private or ip.is_loopback):
        return None
    return addr


def _request_control_token(handler: BaseHTTPRequestHandler) -> str | None:
    """The browser-supplied X-JTS-Token to forward to each member, or None.

    The /rooms/ grouping mutations fan out SERVER-side to each member's
    /grouping/set, so the browser's control token (the mandatory gate since
    WS1 Phase 2) would be lost unless this leader forwards it. We relay only
    what the operator's browser sent — never inject THIS token from disk — so
    the CSRF gate stays real. A forwarded browser token authenticates only
    browser→own-speaker; the cross-device fan-out's own auth is the household
    credential — a DISTINCT bearer (``X-JTS-Household``) that
    ``_post_grouping_to_member`` injects from disk. That disk read is the
    intentional, scoped break of this relay-only rule for a DIFFERENT credential
    and trust domain (docs/HANDOFF-control-plane-auth.md §6); the control-token
    relay-only invariant here is unchanged."""
    token = handler.headers.get("X-JTS-Token")
    return token or None


def _post_grouping_to_member(
    addr: str, body: dict, known: set[str] | None = None,
    *, token: str | None = None, household: str | None = None,
) -> tuple[bool, str]:
    """Configure ONE member by POSTing to its jasper-control /grouping/set.

    This is the cross-speaker call that makes bond-forming one-flow: the
    browser hands us the member list, and we fan the config out SERVER-side
    (no CORS) to each member's control API on the LAN. ``addr`` empty or one of
    this host's own addresses routes
    to loopback (configure self). SSRF guard (via :func:`_lan_target`): a
    remote target must be a PRIVATE / loopback IPv4 — the control API is a
    home-LAN surface, never a public host. ``known`` is forwarded to the guard
    so a fan-out computes the self-address set once. ``token`` is the
    browser-supplied control token relayed to each member; a member mints its
    OWN distinct control_token, so it only authenticates the
    browser→its-own-speaker call, never a cross-device POST.

    Cross-device fan-out auth is the HOUSEHOLD CREDENTIAL (``X-JTS-Household``):
    the household secret, attached here — ``household=`` when the caller pre-read
    it (the unbond path reads it ONCE before it clears, so the concurrent peer
    POSTs can't race the secret out from under each other), else a fresh
    ``household_credential.current()`` read. Reading the secret from disk here is
    the intentional, documented break of ``_request_control_token``'s relay-only
    invariant — a DIFFERENT credential, so injecting IT from disk is correct
    (docs/HANDOFF-control-plane-auth.md §6). A member with no secret yet (unpaired
    or lost) fail-safe-accepts and adopts it. Attaches nothing on a lone speaker
    (no secret). Returns (ok, detail); never raises.
    """
    target = _lan_target(addr, known)
    if target is None:
        try:
            ipaddress.ip_address(addr)
        except ValueError:
            return False, f"not an IP address: {addr!r}"
        return False, f"refusing non-LAN target {addr}"
    url = f"http://{target}:{CONTROL_HTTP_PORT}/grouping/set"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-JTS-Token"] = token
    cred = household if household is not None else household_credential.current()
    if cred:
        headers["X-JTS-Household"] = cred
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace")[:200] if e.fp else ""
        return False, f"HTTP {e.code}: {detail}".strip()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # http.client.HTTPException (BadStatusLine / IncompleteRead) is NOT an
        # OSError subclass — a malformed/truncated reply from one peer must
        # still return (False, …), never escape and crash the fan-out batch.
        return False, str(e)


# Bounded concurrency for every cross-speaker fan-out / discovery. Caps the
# pool so a large household (or a wide bond) can't spawn an unbounded number of
# blocking-HTTP threads; 8 covers any realistic bond in a single wave.
_PEER_FANOUT_MAX_WORKERS = 8


def _map_peers(fn, items):
    """Run ``fn(item)`` over ``items`` on a bounded thread pool, returning
    results in INPUT order. The ONE concurrency primitive for cross-speaker
    I/O.

    Both the bond/unbond POST fan-out (:func:`_fan_out_grouping`) and the
    /unbond discovery GETs use it, so neither path serializes on a slow/offline
    peer — at six speakers a serial dissolve would otherwise block ~5 s PER
    unreachable peer (10–25 s of dead spinner) — and both share one bounded-pool
    policy instead of two hand-rolled executors. ``fn`` MUST NOT raise: the
    peer-call helpers (:func:`_post_grouping_to_member`, :func:`_get_member_grouping`)
    return a value on every failure, and ``pool.map`` would otherwise surface
    the first exception out of the batch. ``pool.map`` preserves submission
    order, so a slow item never reorders results (callers pair them back
    positionally). Empty input → empty list (and no pool is created)."""
    items = list(items)
    if not items:
        return []
    workers = min(_PEER_FANOUT_MAX_WORKERS, len(items))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def _fan_out_grouping(
    targets: list[tuple[str, dict]], *, known: set[str] | None = None,
    token: str | None = None, household: str | None = None,
) -> list[tuple[bool, str]]:
    """POST a grouping config to several members concurrently, ``(ok, detail)``
    results in INPUT order (the caller pairs them back positionally).

    A thin wrapper over :func:`_map_peers`. The self-address set is computed
    ONCE here and shared across every member's SSRF guard (``known=``) rather
    than recomputed per call inside the pool — ``_self_addresses`` does a
    socket probe + ``getaddrinfo``, so per-peer recompute was N redundant
    lookups across N pool threads. Callers that already hold the set (e.g.
    :func:`_unbond`, which also used it for discovery) pass it in. ``token``
    is the browser-supplied control token forwarded to every member's
    /grouping/set (mandatory gate since WS1 Phase 2; None when the request
    carried no X-JTS-Token). ``household`` is the household credential
    (X-JTS-Household) for the cross-device path; pass it explicitly (read ONCE)
    when the fan-out also mutates the secret — the unbond clear — so a per-member
    live read can't race the clear; leave it None for bond/swap/trim and each
    member reads the current secret itself."""
    if known is None:
        known = _self_addresses()
    return _map_peers(
        lambda t: _post_grouping_to_member(
            t[0], t[1], known, token=token, household=household,
        ),
        targets,
    )


def _get_member_grouping(addr: str, known: set[str] | None = None) -> dict | None:
    """Read ONE member's grouping state by GETting its jasper-control
    ``/grouping`` (the CSRF-free read on `jasper-control`; unlike the gated
    ``POST /grouping/set``, the ``GET`` read is genuinely unauthenticated).
    Used by :func:`_unbond` to find which siblings share this
    speaker's bond before dissolving it.

    Same SSRF guard as the POST path (via :func:`_lan_target`): a refused /
    non-LAN / non-IP target returns None; ``known`` is forwarded so the
    discovery fan-out computes the self-address set once. Returns the peer's
    grouping dict — UNWRAPPED from the ``{"grouping": …}`` envelope that GET
    /grouping emits (see jasper/control/server.py; the envelope lets a
    fail-soft read return ``{"grouping": null}`` unambiguously) — or None on
    ANY failure (refused, network error, non-2xx, malformed/truncated HTTP, a
    body that isn't a JSON object, or a null/absent grouping block) — never
    raises, so a single unreachable peer can't break a dissolve."""
    target = _lan_target(addr, known)
    if target is None:
        return None
    url = f"http://{target}:{CONTROL_HTTP_PORT}/grouping"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if not (200 <= r.status < 300):
                return None
            parsed = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, http.client.HTTPException,
            UnicodeDecodeError, json.JSONDecodeError):
        return None
    # Unwrap the {"grouping": …} envelope via the shared parser — the paired
    # inverse of jasper-control's grouping_response (one home for the shape,
    # so producer and consumer can't drift). None when absent/null/not-a-dict.
    return parse_grouping_response(parsed)


def _save_bond(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /bond: form a bond by configuring every member's role.

    One-flow Sonos-style: the browser sends ``{members: [{addr, role,
    channel}, …]}`` (this speaker + the picked speaker(s), with their roles
    and channels). We mint a bond_id, build one target per member, then fan
    the config out concurrently to each member's control API via
    :func:`_fan_out_grouping`. The leader is this speaker (it hosts this
    page), so followers get its STABLE mDNS handle (:func:`_leader_handle`,
    e.g. ``jts.local``) as ``leader_addr`` — a handle that survives DHCP IP
    churn, not a NIC IP. No env editing, no per-speaker tinkering.

    Per-member outcomes are returned so the UI can show exactly which
    speaker failed. A partial failure (some members configured, one
    unreachable) is surfaced, not auto-rolled-back — the household retries;
    the `/state` runtime health shows the half-formed bond as degraded.
    """
    parsed, err = _read_json_body(handler)
    if err is not None:
        log_event(logger, "rooms.bond.save.reject", reason=err, level=logging.WARNING)
        _send_json(handler, {"ok": False, "error": err}, status=HTTPStatus.BAD_REQUEST)
        return

    members = parsed.get("members")
    if not isinstance(members, list) or not members:
        _send_json(
            handler, {"ok": False, "error": "members must be a non-empty list"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    bond_id = str(parsed.get("bond_id") or "").strip() or _generate_bond_id()
    leader_addr = _leader_handle()  # stable mDNS handle of the leader (this speaker)

    # Build a target per member, recording each one's directory slot so the
    # positional results from _fan_out_grouping pair back to the right member.
    # Malformed (non-object) members short-circuit to a result with no call.
    results: list[dict] = [None] * len(members)  # type: ignore[list-item]
    targets: list[tuple[str, dict]] = []
    target_idx: list[int] = []
    for i, m in enumerate(members):
        if not isinstance(m, dict):
            results[i] = {"ok": False, "detail": "member must be an object"}
            continue
        addr = str(m.get("addr") or "").strip()
        role = str(m.get("role") or "").strip()
        channel = str(m.get("channel") or "").strip()
        body = {
            "enabled": True,
            "role": role,
            "channel": channel,
            "bond_id": bond_id,
            "leader_addr": "" if role == "leader" else leader_addr,
            # Explicit empties CLEAR any stale roster (a member that was
            # a leader in a previous bond must not keep pointing at its
            # old sibling); the leader gets the real peer/roster below.
            "peer_addr": "",
            "peer_name": "",
            # Likewise an explicit empty roster on every non-leader member,
            # so a member that LED a previous bond can't keep a stale roster.
            "roster": [],
        }
        # Subwoofer crossover: forward the corner Hz so the member's
        # /grouping/set persists it (validate_grouping clamps the range;
        # the env writer only emits it for channel=="sub"). Pass it through
        # only when the browser sent one — absent means "no crossover key",
        # which the receiving validator treats as the default for a sub.
        if "crossover_hz" in m:
            body["crossover_hz"] = m.get("crossover_hz")
        if role == "leader":
            # The LEADER records the full roster (any N): every OTHER member
            # as {addr,name,channel}, so _unbond can disable ALL of them (a
            # 2.1 system's sub is no longer orphaned). peer_addr/peer_name
            # stay the PRIMARY L/R sibling so swap/trim/balance keep operating
            # on the stereo pair, not the sub.
            roster: list[dict] = []
            for j, mm in enumerate(members):
                if j == i or not isinstance(mm, dict):
                    continue
                m_addr = str(mm.get("addr") or "").strip()
                if not m_addr:
                    continue
                roster.append({
                    "addr": m_addr,
                    "name": str(mm.get("name") or "").strip(),
                    "channel": str(mm.get("channel") or "").strip(),
                })
            body["roster"] = roster
            # Primary sibling = the first L/R follower (so swap/trim stay on
            # the stereo pair), else just the first follower.
            others = [
                mm for j, mm in enumerate(members)
                if j != i and isinstance(mm, dict)
                and str(mm.get("addr") or "").strip()
            ]
            primary = next(
                (mm for mm in others
                 if str(mm.get("channel") or "").strip() in ("left", "right")),
                others[0] if others else None,
            )
            if primary is not None:
                body["peer_addr"] = str(primary.get("addr") or "").strip()
                body["peer_name"] = str(primary.get("name") or "").strip()
        targets.append((addr, body))
        target_idx.append(i)

    # Mint the household credential on THIS leader before the fan-out, so each
    # member's /grouping/set carries it (X-JTS-Household, attached live by
    # _post_grouping_to_member) and adopts it on receipt — locking down every
    # subsequent cross-device grouping change. Idempotent: re-bonding the same
    # household reuses the existing secret (control-plane-auth §6).
    try:
        household_credential.ensure()
    except OSError as exc:
        # A write failure (e.g. the 2026-05-23 read-only-rootfs class) must not
        # fail the bond: members fail-safe-accept, so the bond still forms — the
        # cross-device credential is just not minted, leaving /grouping/set open
        # until a later bond succeeds. Visible, not silent: the WARN here plus
        # the doctor's "bonded but household credential missing" check surface
        # the degraded auth. Mirrors control_token's ensure_failed guard.
        log_event(
            logger, "household_credential.ensure_failed",
            error=str(exc), level=logging.WARNING,
        )
    token = _request_control_token(handler)
    for slot, (addr, body), (ok, detail) in zip(
        target_idx, targets, _fan_out_grouping(targets, token=token)
    ):
        results[slot] = {"addr": addr, "role": body["role"], "ok": ok, "detail": detail}

    all_ok = all(r["ok"] for r in results)
    # Name each failed member in the journal (not just the aggregate) — on a
    # headless speaker the HTTP response isn't a diagnostic surface, so a
    # half-formed bond must say WHICH member failed and WHY. Failures only
    # (a healthy pair logs nothing here — no journal spam).
    for r in results:
        if not r["ok"]:
            log_event(
                logger,
                "rooms.bond.member_failed",
                bond=bond_id,
                addr=r.get("addr") or "?",
                role=r.get("role") or "?",
                detail=r["detail"],
                level=logging.WARNING,
            )
    log_event(
        logger,
        "rooms.bond.save",
        bond=bond_id,
        members=len(members),
        ok=all_ok,
    )
    _send_json(
        handler,
        {"ok": all_ok, "bond_id": bond_id, "results": results},
        status=HTTPStatus.OK if all_ok else HTTPStatus.BAD_GATEWAY,
    )


def _unbond(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /unbond: dissolve the bond THIS speaker is in.

    The inverse of :func:`_save_bond`. We read this speaker's own grouping
    (read_grouping_state); if it isn't in a bond (not enabled, or no bond_id)
    there's nothing to dissolve → 400. Otherwise we browse the sibling
    directory, GET each peer's ``/grouping``, and collect the peers whose
    ``bond_id`` EQUALS ours — a peer in a DIFFERENT bond is left alone, never
    disabled. We then fan ``{enabled: false}`` out to self (empty addr → our
    own loopback control API) plus every matched peer via
    :func:`_fan_out_grouping`.

    Self is ALWAYS in the disable set, so "leave the bond" works locally even
    when no peer is reachable. HTTP 200 when self disabled OK (the local leave
    succeeded), 502 otherwise. ``dissolved`` lists the addresses that
    confirmed disabled; ``results`` carries the per-target outcomes."""
    grouping = read_grouping_state()
    bond_id = str(grouping.get("bond_id") or "").strip()
    if not grouping.get("enabled") or not bond_id:
        _send_json(
            handler, {"ok": False, "error": "not in a bond"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    # Find siblings sharing our bond_id. Read every candidate's /grouping
    # CONCURRENTLY (via _map_peers) behind ONE self-address computation: a
    # serial loop here would block ~5 s per slow/offline peer, so at six
    # speakers dissolving could hang 10–25 s. Self is excluded from the
    # candidates (it's in `known`) — it's disabled explicitly below, not
    # rediscovered. A peer we can't reach (GET → None) or one in a different
    # bond is simply not added to the disable set.
    known = _self_addresses()
    roster = grouping.get("roster")
    roster_addr = str(grouping.get("peer_addr") or "").strip()
    candidate_groupings: list = []
    if isinstance(roster, list) and roster:
        # Full-roster path (N-member bonds, e.g. a 2.1 system): the leader
        # recorded EVERY follower at bond time, so disable self + exactly
        # those — no orphaned sub, and no foreign-claimer ambiguity (the
        # roster is authoritative, so the discovery / peer_addr block is
        # skipped). Best-effort: aim the disable at each recorded address
        # even if offline, so a powered-off follower isn't left stranded.
        peer_addrs = [
            a for a in (
                str(m.get("addr") or "").strip()
                for m in roster if isinstance(m, dict)
            ) if a
        ]
    elif roster_addr:
        # Legacy pair-roster (no full roster): disable exactly the recorded
        # sibling — never a foreign device that happens to claim our bond_id
        # (a transient claimer here would get its grouping DISABLED, which is
        # worse than the read-path ambiguity). Best-effort: if the resolver
        # can't confirm the peer (offline), still aim the disable at its
        # last known address so a powered-off-then-on follower isn't
        # left stranded by design; the fan-out reports the failure.
        resolved_addr, _pg, _err = _resolve_bond_peer(grouping, known)
        peer_addrs = [resolved_addr or roster_addr]
    else:
        candidate_addrs = [
            a for a in (
                str(s.get("address") or "").strip()
                for s in _discover_speakers_cached()
            ) if a and a not in known
        ]
        candidate_groupings = _map_peers(
            lambda a: _get_member_grouping(a, known), candidate_addrs,
        )
        peer_addrs = [
            a for a, pg in zip(candidate_addrs, candidate_groupings)
            if pg is not None
            and str(pg.get("bond_id") or "").strip() == bond_id
        ]

    # Self first (empty addr → loopback), then each matching peer. Reuse the
    # same `known` set for the disable fan-out's SSRF guard.
    targets: list[tuple[str, dict]] = [("", {"enabled": False})]
    targets += [(addr, {"enabled": False}) for addr in peer_addrs]
    addrs = [t[0] for t in targets]

    # Read the household credential ONCE before the fan-out. Each member's
    # /grouping/set (enabled=false) clears its own secret — and self (loopback)
    # clears ours — so a per-member live read could race the clear and strip a
    # peer of the credential it needs to authenticate the very unbond that
    # dissolves it. Passing the pre-read value makes every peer POST carry it
    # regardless of clear ordering (control-plane-auth §6).
    household = household_credential.current()
    fan_results = _fan_out_grouping(
        targets, known=known, token=_request_control_token(handler),
        household=household,
    )
    results = [
        {"addr": addr, "ok": ok, "detail": detail}
        for addr, (ok, detail) in zip(addrs, fan_results)
    ]
    dissolved = [r["addr"] for r in results if r["ok"]]
    self_ok = results[0]["ok"]  # self is always targets[0]

    # Name each member we couldn't disable (self shows as "(self)") so a
    # half-dissolved bond — e.g. a follower offline at dissolve time, left
    # stranded — is visible in the journal, not just the aggregate. Failures
    # only; the empty-addr target is self.
    for r in results:
        if not r["ok"]:
            log_event(
                logger,
                "rooms.unbond.member_failed",
                bond=bond_id,
                addr=r["addr"] or "(self)",
                detail=r["detail"],
                level=logging.WARNING,
            )
    # `unreachable` = candidates whose discovery GET failed. A same-bond
    # follower offline at dissolve time lands here (we can't read its bond_id,
    # so it never becomes a disable target and is left stranded) — surfacing
    # the count explains a "I dissolved but a speaker stayed grouped" report
    # without a per-candidate line.
    unreachable = sum(1 for pg in candidate_groupings if pg is None)
    log_event(
        logger,
        "rooms.unbond",
        bond=bond_id,
        # Which containment path disabled the members (full N-member roster vs
        # the legacy single-sibling vs bond_id discovery) — keyed on the branch
        # taken, not on the legacy peer_addr which a full-roster bond also sets
        # to its primary L/R sibling.
        path=(
            "full" if (isinstance(roster, list) and roster)
            else "legacy" if roster_addr
            else "discovery"
        ),
        roster_n=len(roster or []),
        unreachable=unreachable,
        peers=len(peer_addrs),
        self_ok=self_ok,
        dissolved=len(dissolved),
    )
    _send_json(
        handler,
        {"ok": self_ok, "bond_id": bond_id, "dissolved": dissolved, "results": results},
        status=HTTPStatus.OK if self_ok else HTTPStatus.BAD_GATEWAY,
    )


def _resolve_bond_peer(
    grouping: dict, known: set[str] | None = None,
) -> tuple[str, dict | None, str]:
    """Resolve THIS speaker's one pair sibling → (addr, peer_grouping, err).

    Roster-first: the bond flow records the chosen peer on the leader
    (``peer_addr`` + ``peer_name`` in grouping.env), so pair operations
    resolve THE peer the household actually picked. Probe the recorded
    IP; if it no longer answers for OUR bond and a ``peer_name`` is
    recorded, re-find that name in the live directory (DHCP moved the
    IP) and accept the matching device. With a roster, a FOREIGN device
    transiently claiming our bond_id can never create ambiguity (the
    observed 2026-06-12 failure: an endpoint-tier test Pi cycling
    through bond states made swap/trim/balance all fail with
    "found 2") — and an unreachable roster peer is a hard, NAMED error,
    never an excuse to guess. Bonds recorded before the roster existed
    fall back to the legacy inference (every discovered device claiming
    our bond_id), which still errors on ambiguity.

    ``err`` is "" on success; on failure addr is "" and grouping None.
    """
    if known is None:
        known = _self_addresses()
    bond_id = str(grouping.get("bond_id") or "").strip()
    roster_addr = str(grouping.get("peer_addr") or "").strip()
    roster_name = str(grouping.get("peer_name") or "").strip()

    if roster_addr:
        pg = _get_member_grouping(roster_addr, known)
        if (pg is not None
                and str(pg.get("bond_id") or "").strip() == bond_id):
            return roster_addr, pg, ""
        if roster_name:
            for row in _discover_speakers_cached():
                if str(row.get("name") or "").strip() != roster_name:
                    continue
                addr = str(row.get("address") or "").strip()
                if not addr or addr in known or addr == roster_addr:
                    continue
                pg2 = _get_member_grouping(addr, known)
                if (pg2 is not None
                        and str(pg2.get("bond_id") or "").strip()
                        == bond_id):
                    log_event(
                        logger,
                        "rooms.peer_addr_drift",
                        name=roster_name,
                        old=roster_addr,
                        new=addr,
                    )
                    return addr, pg2, ""
        label = roster_name or roster_addr
        return "", None, (
            f"paired speaker '{label}' is unreachable (last known "
            f"{roster_addr}) — check its power and network, or re-pair "
            "at /rooms"
        )

    candidate_addrs = [
        a for a in (
            str(sp.get("address") or "").strip()
            for sp in _discover_speakers_cached()
        ) if a and a not in known
    ]
    candidate_groupings = _map_peers(
        lambda a: _get_member_grouping(a, known), candidate_addrs,
    )
    peers = [
        (a, pg) for a, pg in zip(candidate_addrs, candidate_groupings)
        if pg is not None
        and str(pg.get("bond_id") or "").strip() == bond_id
    ]
    if len(peers) != 1:
        return "", None, (
            "needs exactly one reachable paired speaker "
            f"(found {len(peers)}) — re-pairing at /rooms records the "
            "pair and removes the ambiguity"
        )
    return peers[0][0], peers[0][1], ""


def _swap_channels(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /swap: exchange the two members' channels (left ↔ right).

    The physical speakers stay where they are; each one simply plays the
    other channel — the leader keeps streaming the same stereo program and
    each member's outputd ChannelPick drops the other side after its
    reconciler applies the change (~a one-period blip per speaker). Roles,
    bond_id, and leader_addr are untouched: this is a channel-assignment
    edit, never a leadership change.

    Deliberately scoped to the 2-speaker left/right pair: discovery mirrors
    :func:`_unbond` (browse siblings, GET each ``/grouping``, match our
    bond_id), then requires exactly ONE same-bond peer and a {left, right}
    channel set between us — a mono/multi-member bond has no well-defined
    "swap" and 400s with the reason. Self's new channel is written via the
    loopback target (empty addr), the peer via its address, both through
    the same :func:`_fan_out_grouping` machinery as /bond."""
    grouping = read_grouping_state()
    bond_id = str(grouping.get("bond_id") or "").strip()
    if not grouping.get("enabled") or not bond_id:
        _send_json(
            handler, {"ok": False, "error": "not in a bond"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    known = _self_addresses()
    peer_addr_r, peer_grouping, perr = _resolve_bond_peer(grouping, known)
    if perr:
        _send_json(
            handler,
            {"ok": False, "error": f"channel swap {perr}"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    peers = [(peer_addr_r, peer_grouping)]

    peer_addr, peer_grouping = peers[0]
    self_channel = str(grouping.get("channel") or "").strip()
    peer_channel = str(peer_grouping.get("channel") or "").strip()
    repairing = (
        self_channel == peer_channel and self_channel in ("left", "right")
    )
    if repairing:
        # Same-channel pair ({left,left} / {right,right}) — the residue of
        # an interrupted swap whose rollback also failed. A strict
        # left/right precondition would make Swap the one button that
        # CANNOT fix the state Swap created, so this completes the
        # interrupted intent instead: self keeps its channel, the peer
        # takes the opposite. Any {left,right} assignment beats a stuck
        # same-channel pair; one more tap swaps again if it lands
        # backwards.
        swapped_self, swapped_peer = self_channel, (
            "right" if self_channel == "left" else "left"
        )
    elif {self_channel, peer_channel} == {"left", "right"}:
        swapped_self, swapped_peer = peer_channel, self_channel
    else:
        _send_json(
            handler,
            {"ok": False, "error": (
                "channel swap needs a left/right pair (this speaker is "
                f"{self_channel or '?'}, peer is {peer_channel or '?'})"
            )},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    def _body(g: dict, channel: str) -> dict:
        return {
            "enabled": True,
            "role": str(g.get("role") or ""),
            "channel": channel,
            "bond_id": bond_id,
            "leader_addr": str(g.get("leader_addr") or ""),
        }

    targets: list[tuple[str, dict]] = [
        ("", _body(grouping, swapped_self)),
        (peer_addr, _body(peer_grouping, swapped_peer)),
    ]
    token = _request_control_token(handler)
    fan_results = _fan_out_grouping(targets, known=known, token=token)
    results = [
        {"addr": addr, "channel": body["channel"], "ok": ok, "detail": detail}
        for (addr, body), (ok, detail) in zip(targets, fan_results)
    ]
    all_ok = all(r["ok"] for r in results)
    for r in results:
        if not r["ok"]:
            log_event(
                logger,
                "rooms.swap.member_failed",
                bond=bond_id,
                addr=r["addr"] or "(self)",
                detail=r["detail"],
                level=logging.WARNING,
            )
    # The two writes fan out CONCURRENTLY, so exactly-one-failed leaves the
    # pair on a SAME-channel state ({left,left} / {right,right}) — audibly
    # wrong, and it blocks a retry because the {left,right} precondition no
    # longer holds. Best-effort rollback: put the member that DID flip back
    # on its original channel so the bond returns to a consistent,
    # retryable state. Rollback failure is surfaced, never silent.
    rolled_back = None
    if not all_ok and any(r["ok"] for r in results):
        ok_idx = 0 if results[0]["ok"] else 1
        rb_addr = targets[ok_idx][0]
        rb_grouping = grouping if ok_idx == 0 else peer_grouping
        rb_channel = self_channel if ok_idx == 0 else peer_channel
        rb_ok, rb_detail = _post_grouping_to_member(
            rb_addr, _body(rb_grouping, rb_channel), known, token=token,
        )
        rolled_back = bool(rb_ok)
        log_event(
            logger,
            "rooms.swap.rollback",
            bond=bond_id,
            addr=rb_addr or "(self)",
            channel=rb_channel,
            ok=rb_ok,
            detail=rb_detail,
            level=logging.WARNING,
        )
    log_event(
        logger,
        "rooms.swap",
        bond=bond_id,
        self=f"{self_channel}->{swapped_self}",
        peer=f"{peer_channel}->{swapped_peer}",
        repaired=repairing,
        ok=all_ok,
    )
    payload = {"ok": all_ok, "bond_id": bond_id, "results": results}
    if repairing:
        payload["repaired"] = True
    if rolled_back is not None:
        payload["rolled_back"] = rolled_back
    _send_json(
        handler,
        payload,
        status=HTTPStatus.OK if all_ok else HTTPStatus.BAD_GATEWAY,
    )


TRIM_STEP_LIMIT_DB = 3.0  # max single nudge; UI sends ±0.5


def _set_member_trim(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /trim: nudge one member's pair-balance trim.

    Body ``{target: "self"|"peer", delta_db}`` — "peer" resolves the ONE
    same-bond sibling server-side (same discovery as /swap), so the page
    needs no peer addressing or trim state. Delta semantics (the UI
    sends ±0.5): we GET the member's current grouping, clamp
    current+delta into the validated range, and write it back through
    the SAME ``/grouping/set`` surface the bond flow uses (member-side
    validation + reconciler kick apply it to outputd's lane).
    Attenuate-only is enforced by the member's validate_grouping; the
    clamp here just keeps the arithmetic in range. Returns
    ``{ok, trim_db}`` so the row shows the new value."""
    from ..multiroom.config import TRIM_DB_MIN, TRIM_DB_MAX

    parsed, err = _read_json_body(handler)
    if err is not None:
        _send_json(handler, {"ok": False, "error": err},
                   status=HTTPStatus.BAD_REQUEST)
        return
    target = str(parsed.get("target") or "self").strip()
    try:
        delta = float(parsed.get("delta_db"))
    except (TypeError, ValueError):
        _send_json(handler, {"ok": False, "error": "delta_db must be a number"},
                   status=HTTPStatus.BAD_REQUEST)
        return
    if abs(delta) > TRIM_STEP_LIMIT_DB:
        _send_json(handler, {"ok": False,
                             "error": f"delta_db limited to ±{TRIM_STEP_LIMIT_DB}"},
                   status=HTTPStatus.BAD_REQUEST)
        return

    known = _self_addresses()
    addr = ""
    if target == "peer":
        own = read_grouping_state()
        bond_id = str(own.get("bond_id") or "").strip()
        if not own.get("enabled") or not bond_id:
            _send_json(handler, {"ok": False, "error": "not in a bond"},
                       status=HTTPStatus.BAD_REQUEST)
            return
        addr, current, perr = _resolve_bond_peer(own, known)
        if perr:
            _send_json(
                handler,
                {"ok": False, "error": f"trim {perr}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
    else:
        current = read_grouping_state()
    if not current.get("enabled") or current.get("error"):
        _send_json(handler, {"ok": False,
                             "error": "member is not in an active bond"},
                   status=HTTPStatus.BAD_REQUEST)
        return

    new_trim = round(
        max(TRIM_DB_MIN, min(TRIM_DB_MAX,
                             float(current.get("trim_db") or 0.0) + delta)),
        1,
    )
    body = {
        "enabled": True,
        "role": str(current.get("role") or ""),
        "channel": str(current.get("channel") or ""),
        "bond_id": str(current.get("bond_id") or ""),
        "leader_addr": str(current.get("leader_addr") or ""),
        "trim_db": new_trim,
    }
    ok, detail = _post_grouping_to_member(
        addr, body, known, token=_request_control_token(handler),
    )
    log_event(
        logger,
        "rooms.trim",
        addr=addr or "(self)",
        delta=f"{delta:.1f}",
        new=f"{new_trim:.1f}",
        ok=ok,
    )
    _send_json(
        handler,
        {"ok": ok, "trim_db": new_trim, "detail": detail},
        status=HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
    )


def _make_handler():
    """Build the request handler class. No state-path binding — the
    directory pulls everything live (mDNS browse + grouping + peering SSOT),
    and POST /peering writes through the reused peering config constant."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: ANN001, A003
            logger.info("rooms-wizard: " + fmt, *args)

        def do_GET(self):  # noqa: N802
            if self.path == "/" or self.path.startswith("/?"):
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(self, _render_page(csrf_token=ctx["csrf_token"]))
                return
            if self.path == "/rooms.json":
                if not guard_read_request(self):
                    return
                _send_json(self, _build_rooms_payload())
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            # Route-check BEFORE the CSRF guard (project convention): a bogus
            # path 404s without revealing CSRF state.
            if self.path not in ("/peering", "/bond", "/unbond", "/swap", "/trim"):
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
                return
            # JSON fetch POST: guard_mutating_request checks the Host/Origin
            # and the X-CSRF-Token header (no form). Mirrors system_setup.
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            if self.path == "/bond":
                _save_bond(self)
            elif self.path == "/unbond":
                _unbond(self)
            elif self.path == "/swap":
                _swap_channels(self)
            elif self.path == "/trim":
                _set_member_trim(self)
            else:
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
