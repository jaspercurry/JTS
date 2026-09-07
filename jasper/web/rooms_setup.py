# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""/sound/pair/ — the "Speakers" surface: directory + wake-response toggle.

Discovery browses the ALWAYS-ON `_jasper-control._tcp` mDNS service
(advertised unconditionally by deploy/avahi/jasper-control.service) — NOT
the wake-peering-gated `_jasper-peer._udp`, which only exists when
JASPER_PEERING=on. So the directory lists every speaker regardless of
whether wake-peering is enabled.

Room is NOT edited here: the speaker-identity home (/speaker/) owns name +
room; this page only reads identity and links there.

POST /peering read-modify-writes /var/lib/jasper/peering.env, REUSING
jasper.peering.config's readers/constants so the env parse contract keeps
one owner.

The page renders client-side: the body is a single `#app` mount point plus
the ES module at /assets/rooms/js/main.js, which fetches /rooms.json on load
and every 7 s. Every peer field is mDNS-provided (untrusted), so this server
interpolates none of the discovered data.

URL surface (after nginx strips the /sound/pair/ prefix); every POST is
CSRF-verified:
  GET  /            page render (mount point + ES module)
  GET  /rooms.json  the directory + self status incl. the wake-response
                    `peering` block
  POST /peering     write the wake-response state into peering.env +
                    restart voice/control
  POST /bond        form a stereo pair from {peer_addr}; the server mints a
                    bond id, builds the member plan, then fans the grouping
                    config out SERVER-side to each member's jasper-control.
                    Before any write, every enabled member must return
                    readiness.allowed=true from lightweight GET /grouping;
                    POST /grouping/set rechecks the same target-side guard.
                    Advanced callers may still post a full {members:[...]}
                    body for same-bond edits.
  POST /unbond      dissolve this speaker's bond: disable self + every
                    sibling sharing this bond_id
  POST /swap        exchange a 2-speaker pair's left/right channels; roles
                    and bond untouched
  POST /trim        set the pair balance absolutely (target=pair,
                    balance_db)
"""
from __future__ import annotations

import asyncio  # noqa: F401 — kept so tests can patch rooms_setup.asyncio.run
import concurrent.futures
import http.client
import ipaddress
import json
import logging
import math
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
from ..control.client import (
    CONTROL_PORT,
    PEER_RESPONSE_MAX_BYTES,
    peer_detail,
)
from ..mdns import browse_once
from ..multiroom.airplay_latency import with_airplay_latency_fit
from ..multiroom.config import is_private_or_loopback_ipv4
from ..multiroom.state import (
    GROUPING_READINESS_KEY,
    parse_grouping_readiness,
    parse_grouping_response,
    read_grouping_state,
)
from ..peering import config as peering_config
from ..log_event import log_event
from ._common import (
    begin_request,
    canonical_header,
    canonical_page,
    guard_read_request,
    guard_mutating_request,
    reject_csrf,
    read_json_body,
    restart_voice_daemon,
    restart_systemd_units,
    send_html_response,
    send_json_response,
    write_env_file,
)

logger = logging.getLogger(__name__)


ROOMS_PAGE_CSS_HREF = "/assets/rooms/rooms.css"

# Fully-qualified with the trailing `.local.` per the python-zeroconf contract.
CONTROL_MDNS_TYPE = "_jasper-control._tcp.local."

CONTROL_HTTP_TIMEOUT_SEC = 5.0

# Short so a powered-off paired speaker cannot stall the 7 s /rooms.json poll.
# Mutations keep the full control timeout; the snapshot only needs
# fresh-enough display state and can surface "unavailable" quickly.
BALANCE_SNAPSHOT_PEER_TIMEOUT_SEC = 0.75

# python-zeroconf re-broadcasts with backoff (1s, 2s, 4s); 2s captures the
# common PTR→SRV→TXT roundtrip on a home LAN without slowing first paint.
DISCOVERY_TIMEOUT_SEC = 2.0

# The page re-polls /rooms.json every 7 s; a fresh zeroconf browse per poll
# would stand up and tear down a multicast listener ~8×/min, contending with
# the host's avahi responder. The household speaker set is near-static, so a
# new speaker showing up within one TTL is acceptable.
DISCOVERY_CACHE_TTL_SEC = 30.0

# Only slow snapshots log, so a left-open tab does not spam the journal.
ROOMS_SNAPSHOT_SLOW_MS = 1000


# ----------------------------------------------------------------------
# Self identity.
# ----------------------------------------------------------------------
#
# Name / room / hostname come from jasper.identity.read_identity, read ONCE
# per request in _build_rooms_payload so the three fields agree within one
# render. The LAN address is NOT part of identity (it is NIC-derived), which
# is why self_addresses / _self_address live here.


def self_addresses() -> set[str]:
    """Best-effort set of this host's own LAN IPv4 addresses, used to drop
    self from the discovered-peer list. Never raises — a failure just
    yields a smaller set (worst case: a self-row leaks in, which the page
    renders harmlessly)."""
    addrs: set[str] = set()
    # The UDP-connect trick: no packet is sent; connect() on a datagram socket
    # just picks the route's source address. 192.0.2.0/24 is TEST-NET-1
    # (RFC 5737), never routed.
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
    pool = known if known is not None else self_addresses()
    return next(iter(sorted(pool)), "")


def _leader_handle() -> str:
    """This speaker's STABLE address to hand a follower as ``leader_addr``.

    The mDNS .local FQDN, NOT a NIC IP: snapclient_argv in
    jasper/multiroom/reconcile.py passes leader_addr verbatim to
    ``snapclient --host``, which resolves a .local name, so the bond survives
    DHCP lease churn that would invalidate a baked-in IP. Distinct from
    _self_address, which stays NIC-derived for SSRF self-routing in
    post_grouping_to_member / lan_target."""
    return identity.read_identity().hostname


# ----------------------------------------------------------------------
# Discovery — browse the always-on _jasper-control._tcp service.
# ----------------------------------------------------------------------


def _strip_service_type(full_name: str) -> str:
    """Turn a full mDNS instance name into a display label: drop the trailing
    service-type suffix and unescape avahi's `\\032` space encoding. Mirrors
    jasper.speaker_name_discovery._strip_service_type."""
    name = full_name.rstrip(".")
    suffix = "." + CONTROL_MDNS_TYPE.rstrip(".")
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name.replace("\\032", " ").strip()


def _hostname_label(server: str) -> str:
    """A clean speaker label from an mDNS SRV target host, e.g.
    `"jts3.local."` -> `"jts3"`."""
    host = (server or "").rstrip(".")
    if host.endswith(".local"):
        host = host[: -len(".local")]
    return host.strip()


_LOCAL_HOST_LABEL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def _local_web_host(hostname: str) -> str:
    """Browser-safe management host for a discovered peer.

    Click-through URLs use the stable mDNS hostname so DHCP churn does not
    make the UI teach people raw IPs; the peer `address` stays the IP the
    server-side fan-out passes through the SSRF guard. An empty or invalid
    hostname fails closed to no link.
    """
    host = (hostname or "").strip().rstrip(".")
    if host.endswith(".local"):
        host = host[: -len(".local")]
    if not _LOCAL_HOST_LABEL_RE.match(host):
        return ""
    return f"{host}.local"


def _peer_label(props: dict, server: str, full_name: str) -> str:
    """Pick the directory label for a discovered speaker, best-first:
      1. the `name=` TXT record `jasper.control_advert` publishes on
         `_jasper-control._tcp` (the production path),
      2. the resolved SRV hostname (`jts3.local.` -> `jts3`),
      3. the raw mDNS instance name — a last resort, since the default
         (`"JTS jasper-control on jts"`) is verbose in the UI."""
    txt = (props.get("name") or "").strip()
    if txt:
        return txt
    host = _hostname_label(server)
    if host:
        return host
    return _strip_service_type(full_name)


def _discover_speakers(timeout: float = DISCOVERY_TIMEOUT_SEC) -> list[dict]:
    """Best-effort mDNS-SD browse of `_jasper-control._tcp`, at most one
    {name, hostname, room, address} dict per service name. Fail-soft:
    [] on any failure, so the page renders an empty state and never 500s.

    Self is NOT filtered here — the caller does that against self_addresses,
    so the filter stays testable. `room` is "" until a `room=` TXT record is
    added to the avahi advertisement; `name=` and `peer_id=` exist on
    `_jasper-control._tcp` today."""
    out: list[dict] = []
    for svc in browse_once(CONTROL_MDNS_TYPE, timeout=timeout):
        if not svc.addresses:
            continue
        out.append(
            {
                "name": _peer_label(svc.txt, svc.server, svc.name),
                # Self-exclusion matches on the stable SRV hostname rather
                # than the free-form display name: a speaker named "jts" must
                # not drop a peer named "jts3".
                "hostname": _hostname_label(svc.server),
                "room": (svc.txt.get("room") or "").strip(),
                "address": str(svc.addresses[0]),
            }
        )
    return out


# The wizard is a shared (socket-activated) process, so this cache persists
# across polls.
_disc_lock = threading.Lock()
_disc_cache: dict = {"at": 0.0, "result": []}


def discover_speakers_cached() -> list[dict]:
    """`_discover_speakers()` behind a TTL cache (DISCOVERY_CACHE_TTL_SEC).

    The lock serializes the live browse so concurrent /rooms.json requests
    don't each kick one off. Fail-soft: inherits `_discover_speakers`'s
    return-[] behaviour."""
    with _disc_lock:
        now = time.monotonic()
        if _disc_cache["result"] and (now - _disc_cache["at"]) < DISCOVERY_CACHE_TTL_SEC:
            return _disc_cache["result"]
        result = _discover_speakers()
        # A transient empty scan must not blank the directory for a full TTL,
        # so only a non-empty browse refreshes the cache.
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
      enabled  — JASPER_PEERING is on (the speaker joins wake arbitration so
                 only one device answers "Hey Jarvis").
      primary  — JASPER_PEER_PRIMARY is set (small bias to win ties).

    Fail-soft: peering_config.read_state returns {} on a missing/unreadable
    file, so this never raises.

    The path is passed explicitly so it resolves at call time — fresh on
    every poll, and patchable via peering_config.PEERING_ENV_FILE."""
    state = peering_config.read_state(peering_config.PEERING_ENV_FILE)
    return {
        "enabled": peering_config.state_enabled(state),
        "primary": peering_config.state_primary(state),
    }


def _build_rooms_payload() -> dict:
    """Assemble the /rooms.json body: this speaker's identity + grouping
    status + wake-response state, plus the sibling directory (self
    excluded). The discovery, grouping, and peering reads are each
    fail-soft, so this never raises.

    Shape (consumed by /assets/rooms/js/main.js):
      {
        "self": {name, hostname, room, address,
                 grouping: <read_grouping_state() dict
                            + airplay_latency_fit: {applicable, tight?, …}
                            + balance: {applicable, ok?, balance_db?, …}>,
                 peering: {enabled, primary}},
        "peers": [{name, room, address, home_url, system_url}, ...]
      }

    Peer `address` stays raw LAN IP for POST /bond / /swap / /trim control
    calls. Peer `home_url` / `system_url` are derived from the advertised
    hostname and end in `.local`, never from the IP address.
    """
    started = time.perf_counter()
    stages: dict[str, int] = {}

    stage = time.perf_counter()
    me = identity.read_identity()
    stages["identity_ms"] = round((time.perf_counter() - stage) * 1000)

    stage = time.perf_counter()
    own = self_addresses()
    stages["self_addr_ms"] = round((time.perf_counter() - stage) * 1000)

    stage = time.perf_counter()
    grouping = with_airplay_latency_fit(read_grouping_state())
    stages["grouping_ms"] = round((time.perf_counter() - stage) * 1000)

    stage = time.perf_counter()
    balance = _pair_balance_snapshot(grouping, own)
    stages["balance_ms"] = round((time.perf_counter() - stage) * 1000)
    if balance.get("applicable"):
        grouping = dict(grouping)
        grouping["balance"] = balance

    stage = time.perf_counter()
    peering = _read_peering_block()
    stages["peering_ms"] = round((time.perf_counter() - stage) * 1000)
    self_addr = _self_address(own)
    self_block = {
        "name": me.name,
        "hostname": me.hostname,
        "room": me.room,
        "address": self_addr,
        # with_airplay_latency_fit is the same composer /state uses, so the
        # bonded-leader lip-sync status is not re-derived here.
        "grouping": grouping,
        "peering": peering,
    }

    stage = time.perf_counter()
    discovered = discover_speakers_cached()
    stages["discovery_ms"] = round((time.perf_counter() - stage) * 1000)

    peers: list[dict] = []
    self_hostname_label = me.hostname.split(".")[0].casefold()
    for s in discovered:
        addr = s.get("address") or ""
        # Drop self by address, then by EXACT SRV-hostname match for the case
        # where the UDP-route trick missed our address. The hostname match
        # must stay exact and off the free-form display name: substring
        # matching once made a speaker "jts" drop a peer "jts3".
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
    peers.sort(
        key=lambda p: (p.get("room") or "", p.get("name") or "", p.get("address") or "")
    )
    payload = {
        "self": self_block,
        "peers": peers,
        "view": _rooms_view(grouping, peers, self_addr),
    }
    total_ms = round((time.perf_counter() - started) * 1000)
    if total_ms >= ROOMS_SNAPSHOT_SLOW_MS:
        log_event(
            logger,
            "rooms.snapshot",
            total_ms=total_ms,
            peer_count=len(peers),
            **stages,
        )
    return payload


def _rooms_view(grouping: dict, peers: list[dict], self_addr: str) -> dict:
    """Backend-owned view model for the rooms page, so the browser does not
    rediscover the grouping state machine or decide which advanced topology
    controls belong in the primary flow.
    """
    bonded = bool(grouping.get("enabled") and grouping.get("bond_id"))
    if bonded and grouping.get("error"):
        state = "degraded"
    elif bonded:
        state = "paired"
    else:
        state = "solo"
    balance = grouping.get("balance")
    can_balance = (
        state == "paired"
        and isinstance(balance, dict)
        and bool(balance.get("applicable"))
        and bool(balance.get("ok"))
    )
    return {
        "state": state,
        "bonded": bonded,
        "can_create_pair": (
            state == "solo"
            and bool(self_addr)
            and any(p.get("address") for p in peers)
        ),
        "can_balance_pair": can_balance,
    }


# ----------------------------------------------------------------------
# HTML rendering.
# ----------------------------------------------------------------------
#
# Page-specific visuals live in deploy/assets/rooms/rooms.css
# (page_css_href); shared component classes come from /assets/app.css. No
# inline <script> with behaviour — only the type="module" loader tag.


def _render_page(*, csrf_token: str = "") -> bytes:
    # `id="app"` is the mount contract with the ES module, which clears the
    # placeholder on first render so a failed module load degrades to a
    # message rather than a blank page.
    #
    # canonical_page emits the CSRF <meta name="jts-csrf"> tag the ES module
    # reads (via http.js jsonHeaders()) for the wake-response POST /peering.
    body = f"""
{canonical_header("Speakers", back_href="/sound/", back_label="Sound")}
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
    send_json_response(handler, payload, status=status)


# Max JSON body on the POST routes; the real payloads are ~30 B, so anything
# larger is rejected before it is read off the wire.
_PEERING_BODY_LIMIT = 4096


def _save_peering(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /peering: write the wake-response state into peering.env
    and restart voice + jasper-control so both daemons pick it up.

    REUSES jasper.peering.config for the PEERING_ENV_FILE and state readers
    so there is ONE owner of the peering env contract.

    Read-modify-write: write_env_file does a full-file replace, so without
    the merge a save would clobber JASPER_PEER_ROOM (owned by /speaker/) and
    operator-set arbitration knobs like JASPER_PEER_ARB_WINDOW_MS.

    Fail-soft: a parse/IO error returns a 4xx/5xx JSON error and never raises
    out of the handler."""
    parsed, err = read_json_body(handler, max_bytes=_PEERING_BODY_LIMIT)
    if err is not None:
        log_event(logger, "rooms.peering.save.reject", reason=err, level=logging.WARNING)
        _send_json(handler, {"ok": False, "error": err}, status=HTTPStatus.BAD_REQUEST)
        return

    enabled = bool(parsed.get("enabled"))
    primary = bool(parsed.get("primary"))

    # Resolve the path ONCE so the merge cannot read one file and write
    # another, which would clobber the keys it means to preserve.
    env_path = peering_config.PEERING_ENV_FILE

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

    # jasper-voice reads JASPER_PEERING to know whether to call the peering
    # UDS; jasper-control reads it to know whether to start its peering daemon
    # thread. Both restarts are best-effort and non-blocking.
    restart_voice_daemon()
    restart_systemd_units("jasper-control")

    _send_json(
        handler,
        {"ok": True, "peering": {"enabled": enabled, "primary": primary}},
    )


def _generate_bond_id() -> str:
    """A short, unique bond identifier — an opaque label shared by a bond's
    members; the user never types it."""
    return "bond-" + uuid.uuid4().hex[:8]


def lan_target(addr: str, known: set[str] | None = None) -> str | None:
    """Resolve ``addr`` to a host safe to call on the home LAN, or None to
    refuse it. The SSRF guard for every cross-speaker control call — shared
    by post_grouping_to_member (POST) and the GET /grouping readers so every
    peer-control operation applies the EXACT same guard.

    ``addr`` empty or one of this host's own addresses → ``"127.0.0.1"``. A
    remote target must parse as a PRIVATE or loopback IPv4: the control API
    is a home-LAN surface, never a public host, and bare hostnames are
    refused (no DNS rebind surface).

    ``known`` is this host's own addresses. Pass a precomputed set — as the
    fan-out callers do — to compute it ONCE per operation instead of per
    peer; ``self_addresses`` does a socket probe plus ``getaddrinfo``."""
    if known is None:
        known = self_addresses()
    if not addr:
        return "127.0.0.1"
    if not is_private_or_loopback_ipv4(addr):
        return None
    if addr in known:
        return "127.0.0.1"
    return addr


def request_control_token(handler: BaseHTTPRequestHandler) -> str | None:
    """The browser-supplied X-JTS-Token to forward to each member, or None.

    /sound/pair/ grouping mutations fan out SERVER-side to each member's
    /grouping/set, so the browser's control token would be lost unless this
    leader forwards it. RELAY ONLY — never inject this token from disk, or
    the gate stops being real. A forwarded browser token authenticates
    browser→own-speaker only; cross-device fan-out auth is the DISTINCT
    household credential (``X-JTS-Household``), which
    ``post_grouping_to_member`` does read from disk."""
    token = handler.headers.get("X-JTS-Token")
    return token or None


def post_grouping_to_member(
    addr: str, body: dict, known: set[str] | None = None,
    *, token: str | None = None, household: str | None = None,
) -> tuple[bool, str]:
    """Configure ONE member by POSTing to its jasper-control /grouping/set.

    ``addr`` empty or one of this host's own addresses routes to loopback.
    SSRF guard via :func:`lan_target`; ``known`` is forwarded to it so a
    fan-out computes the self-address set once.

    ``token`` is the browser-supplied control token relayed to each member.
    A member mints its OWN control_token, so a relayed one authenticates the
    browser→its-own-speaker call only, never a cross-device POST.

    Cross-device auth is the HOUSEHOLD CREDENTIAL (``X-JTS-Household``):
    ``household=`` when the caller pre-read it (the unbond path reads it ONCE
    before it clears, so concurrent peer POSTs can't race the secret out from
    under each other), else a fresh ``household_credential.current()`` read.
    A member with no secret yet fail-safe-accepts and adopts it; a lone
    speaker has none to attach. Returns (ok, detail); never raises.
    """
    target = lan_target(addr, known)
    if target is None:
        try:
            ipaddress.ip_address(addr)
        except ValueError:
            return False, f"not an IP address: {addr!r}"
        return False, f"refusing non-LAN target {addr}"
    url = f"http://{target}:{CONTROL_PORT}/grouping/set"
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
        with urllib.request.urlopen(req, timeout=CONTROL_HTTP_TIMEOUT_SEC) as r:
            raw = _read_peer_response(r)
            if raw is None:
                return False, "peer response too large"
            return (
                200 <= r.status < 300,
                _grouping_set_success_detail(r.status, raw),
            )
    except urllib.error.HTTPError as e:
        try:
            raw = _read_peer_response(e) if e.fp else b""
        except (OSError, http.client.HTTPException):
            return False, f"HTTP {e.code}"
        if raw is None:
            return False, f"HTTP {e.code}: response too large"
        detail = peer_detail(raw, token or "", cred or "")
        return False, f"HTTP {e.code}: {detail}".strip()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # http.client.HTTPException (BadStatusLine / IncompleteRead) is NOT an
        # OSError subclass, and a malformed reply from one peer must not
        # escape and crash the fan-out batch.
        return False, str(e)


def _grouping_set_success_detail(status: int, raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"HTTP {status}"
    if not isinstance(payload, dict):
        return f"HTTP {status}"
    live = payload.get("live_apply")
    if isinstance(live, dict):
        mode = str(live.get("mode") or "")
        if live.get("applied"):
            return "Applied live." if mode != "noop" else "Already applied."
        if payload.get("reconciler_kicked"):
            return "Saved; audio update scheduled."
    if payload.get("reconciler_kicked"):
        return "Saved; audio update scheduled."
    return f"HTTP {status}"


def _read_peer_response(response) -> bytes | None:
    """Read one small peer-control response, or None when it exceeds the cap."""
    if not hasattr(response, "read"):
        return b""
    raw = response.read(PEER_RESPONSE_MAX_BYTES + 1)
    return raw if len(raw) <= PEER_RESPONSE_MAX_BYTES else None


# Caps the pool so a large household can't spawn an unbounded number of
# blocking-HTTP threads; 8 covers any realistic bond in a single wave.
_PEER_FANOUT_MAX_WORKERS = 8


def _map_peers(fn, items):
    """Run ``fn(item)`` over ``items`` on a bounded thread pool, returning
    results in INPUT order. The ONE concurrency primitive for cross-speaker
    I/O.

    A serial loop would block ~5 s per unreachable peer, so at six speakers a
    dissolve could hang 10–25 s. ``fn`` MUST NOT raise: ``pool.map`` surfaces
    the first exception out of the batch, so the peer-call helpers
    (:func:`post_grouping_to_member`, :func:`_get_member_grouping`) return a
    value on every failure instead. ``pool.map`` preserves submission order,
    so callers can pair results back positionally."""
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

    The self-address set is computed ONCE here and shared across every
    member's SSRF guard (``known=``) rather than recomputed per pool thread;
    callers that already hold it pass it in. ``token`` is the
    browser-supplied control token forwarded to every member's /grouping/set
    (None when the request carried no X-JTS-Token). ``household`` is the
    household credential (X-JTS-Household); pass it explicitly (read ONCE)
    when the fan-out also mutates the secret — the unbond clear — so a
    per-member live read can't race the clear, and leave it None for
    bond/swap/trim so each member reads the current secret itself."""
    if known is None:
        known = self_addresses()
    return _map_peers(
        lambda t: post_grouping_to_member(
            t[0], t[1], known, token=token, household=household,
        ),
        targets,
    )


def _get_member_grouping_response(
    addr: str, known: set[str] | None = None, *,
    timeout: float = CONTROL_HTTP_TIMEOUT_SEC,
) -> tuple[dict | None, str | None]:
    """Read ONE member's lightweight GET /grouping envelope, under the same
    SSRF guard as the POST path.

    Returns ``(payload, None)`` or ``(None, household-facing reason)``.
    Membership discovery ignores the reason; bond preflight surfaces it so an
    old, unreachable, or malformed peer is actionable.
    """
    target = lan_target(addr, known)
    if target is None:
        return None, "speaker address is not on the private home network"
    return _get_remote_json_result(target, "/grouping", timeout=timeout)


def _get_member_grouping(
    addr: str, known: set[str] | None = None, *,
    timeout: float = CONTROL_HTTP_TIMEOUT_SEC,
) -> dict | None:
    """Read and unwrap one member's grouping snapshot.

    None on every failure, so one unreachable peer cannot break a dissolve.
    """
    parsed, _error = _get_member_grouping_response(addr, known, timeout=timeout)
    if parsed is None:
        return None
    return parse_grouping_response(parsed)


def _get_member_grouping_readiness(
    addr: str, known: set[str] | None = None, *,
    timeout: float = CONTROL_HTTP_TIMEOUT_SEC,
) -> tuple[dict | None, str | None]:
    """Read one member's pre-mutation verdict from GET /grouping.

    The target computes it through the same policy seam as POST
    /grouping/set's final active-speaker guard. Every failure is fail-closed:
    no grouping.env is written.
    """
    parsed, error = _get_member_grouping_response(addr, known, timeout=timeout)
    if parsed is None:
        return None, error
    readiness = parse_grouping_readiness(parsed)
    if readiness is None:
        if GROUPING_READINESS_KEY in parsed:
            return (
                None,
                "speaker could not determine grouping readiness — "
                "open its System page for diagnostics, then retry",
            )
        return (
            None,
            "speaker software does not provide grouping readiness — "
            "update both speakers, then retry",
        )
    return readiness, None


def _get_remote_json_result(
    target: str,
    path: str,
    *,
    timeout: float,
) -> tuple[dict | None, str | None]:
    """GET one bounded peer JSON object with a small diagnostic result."""
    url = f"http://{target}:{CONTROL_PORT}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if not (200 <= r.status < 300):
                return None, f"speaker returned HTTP {r.status}"
            raw = _read_peer_response(r)
            if raw is None:
                return None, "speaker returned an oversized response"
            parsed = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"speaker returned HTTP {exc.code}"
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return None, "speaker is unreachable — check its power and network"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "speaker returned an invalid response"
    if not isinstance(parsed, dict):
        return None, "speaker returned an invalid response"
    return parsed, None


def _preflight_grouping_target(
    addr: str, body: dict, known: set[str] | None = None,
) -> tuple[bool, str]:
    """Fail closed when a member cannot safely join this bond."""
    if not body.get("enabled"):
        return True, "disabled"
    readiness, error = _get_member_grouping_readiness(addr, known)
    if readiness is None:
        return False, error or "could not read grouping readiness"
    if not readiness["allowed"]:
        return False, readiness["detail"]
    return True, readiness["detail"]


def _peer_name_from_directory(addr: str) -> str:
    """Best-effort display name for a peer address from the cached directory."""
    target = str(addr or "").strip()
    if not target:
        return ""
    for peer in discover_speakers_cached():
        if str(peer.get("address") or "").strip() == target:
            return str(peer.get("name") or "").strip()
    return ""


def _stereo_pair_members_from_intent(peer_addr: str) -> list[dict]:
    """Server-owned topology for the primary "create stereo pair" intent."""
    return [
        {"addr": "", "role": "leader", "channel": "left"},
        {
            "addr": peer_addr,
            "role": "follower",
            "channel": "right",
            "name": _peer_name_from_directory(peer_addr),
        },
    ]


def _save_bond(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /bond: form a bond by configuring every member's role.

    The browser sends ``{peer_addr}`` and the backend builds the stereo
    topology (this speaker leader/left, peer follower/right); advanced
    same-bond edits may send ``{members: [...]}`` explicitly. The leader is
    always this speaker, so followers get its STABLE mDNS handle
    (:func:`_leader_handle`) as ``leader_addr``, never a NIC IP.

    A partial failure is surfaced per member, not auto-rolled-back — the
    household retries, and `/state` shows the half-formed bond as degraded.
    """
    parsed, err = read_json_body(handler, max_bytes=_PEERING_BODY_LIMIT)
    if err is not None:
        log_event(logger, "rooms.bond.save.reject", reason=err, level=logging.WARNING)
        _send_json(handler, {"ok": False, "error": err}, status=HTTPStatus.BAD_REQUEST)
        return

    members = parsed.get("members")
    if members is None:
        peer_addr = str(parsed.get("peer_addr") or "").strip()
        if not peer_addr:
            _send_json(
                handler,
                {"ok": False, "error": "peer_addr is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        members = _stereo_pair_members_from_intent(peer_addr)
    if not isinstance(members, list) or not members:
        _send_json(
            handler, {"ok": False, "error": "members must be a non-empty list"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    requested_bond_id = str(parsed.get("bond_id") or "").strip()
    fresh_bond = not requested_bond_id
    bond_id = requested_bond_id or _generate_bond_id()
    leader_addr = _leader_handle()

    # Record each member's slot so the positional results from
    # _fan_out_grouping pair back to the right member.
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
            # Explicit empties CLEAR stale state: a member that led a previous
            # bond must not keep pointing at its old sibling or roster. The
            # leader gets the real peer/roster below.
            "peer_addr": "",
            "peer_name": "",
            "roster": [],
        }
        if fresh_bond:
            # A new pair must not inherit stale balance trim from a previous
            # bond/unbond cycle. Existing-bond edits omit trim_db so a
            # calibrated L/R balance is preserved.
            body["trim_db"] = 0.0
        if role == "leader":
            # The LEADER records every OTHER member so _unbond can disable ALL
            # of them. peer_addr / peer_name stay the PRIMARY L/R sibling so
            # swap/trim/balance keep operating on the stereo pair.
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

    known = self_addresses()
    preflight = _map_peers(
        lambda t: _preflight_grouping_target(t[0], t[1], known),
        targets,
    )
    blocked = [
        {
            "addr": addr,
            "role": body.get("role"),
            "ok": ok,
            "detail": detail,
        }
        for (addr, body), (ok, detail) in zip(targets, preflight)
        if not ok
    ]
    if blocked:
        for r in blocked:
            log_event(
                logger,
                "rooms.bond.preflight_failed",
                bond=bond_id,
                addr=r.get("addr") or "?",
                role=r.get("role") or "?",
                detail=r["detail"],
                level=logging.WARNING,
            )
        _send_json(
            handler,
            {
                "ok": False,
                "bond_id": bond_id,
                "error": "one or more speakers are not ready to join a group",
                "results": blocked,
            },
            status=HTTPStatus.CONFLICT,
        )
        return

    # Mint the household credential BEFORE the fan-out so each member's
    # /grouping/set carries it and adopts it on receipt, locking down every
    # subsequent cross-device grouping change. Idempotent: re-bonding the same
    # household reuses the existing secret.
    try:
        household_credential.ensure()
    except OSError as exc:
        # A write failure must not fail the bond: members fail-safe-accept, so
        # the bond still forms with the credential unminted, leaving
        # /grouping/set open until a later bond succeeds. The WARN plus the
        # doctor's "bonded but household credential missing" check surface the
        # degraded auth.
        log_event(
            logger, "household_credential.ensure_failed",
            error=str(exc), level=logging.WARNING,
        )
    token = request_control_token(handler)
    for slot, (addr, body), (ok, detail) in zip(
        target_idx, targets, _fan_out_grouping(targets, known=known, token=token)
    ):
        results[slot] = {"addr": addr, "role": body["role"], "ok": ok, "detail": detail}

    all_ok = all(r["ok"] for r in results)
    # On a headless speaker the HTTP response is not a diagnostic surface, so
    # a half-formed bond must name WHICH member failed and WHY in the journal.
    # Failures only, so a healthy pair logs nothing here.
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

    A peer in a DIFFERENT bond is left alone, never disabled. Self is ALWAYS
    in the disable set, so "leave the bond" works locally even when no peer is
    reachable: HTTP 200 when self disabled OK, 502 otherwise."""
    grouping = read_grouping_state()
    bond_id = str(grouping.get("bond_id") or "").strip()
    if not grouping.get("enabled") or not bond_id:
        _send_json(
            handler, {"ok": False, "error": "not in a bond"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    # Self is excluded from the candidates (it is in `known`) and disabled
    # explicitly below, not rediscovered. An unreachable peer, or one in a
    # different bond, is simply not added to the disable set.
    known = self_addresses()
    roster = grouping.get("roster")
    roster_addr = str(grouping.get("peer_addr") or "").strip()
    candidate_groupings: list = []
    if isinstance(roster, list) and roster:
        # The leader recorded EVERY follower at bond time, so the roster is
        # authoritative: no orphaned follower and no foreign-claimer ambiguity.
        # The disable is aimed at each recorded address even when offline, so
        # a powered-off follower is not left stranded.
        peer_addrs = [
            a for a in (
                str(m.get("addr") or "").strip()
                for m in roster if isinstance(m, dict)
            ) if a
        ]
    elif roster_addr:
        # Legacy pair-roster: disable exactly the recorded sibling, never a
        # foreign device that happens to claim our bond_id — a transient
        # claimer would get its grouping DISABLED, worse than the read-path
        # ambiguity. When the resolver cannot confirm the peer, the disable is
        # still aimed at its last known address and the fan-out reports the
        # failure.
        resolved_addr, _pg, _err = resolve_bond_peer(grouping, known)
        peer_addrs = [resolved_addr or roster_addr]
    else:
        candidate_addrs = [
            a for a in (
                str(s.get("address") or "").strip()
                for s in discover_speakers_cached()
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

    # Self first (empty addr → loopback), then each matching peer.
    disabled_body = {"enabled": False, "trim_db": 0.0}
    targets: list[tuple[str, dict]] = [("", dict(disabled_body))]
    targets += [(addr, dict(disabled_body)) for addr in peer_addrs]
    addrs = [t[0] for t in targets]

    # Read the household credential ONCE before the fan-out: each member's
    # /grouping/set (enabled=false) clears its own secret, and self (loopback)
    # clears ours, so a per-member live read could race the clear and strip a
    # peer of the credential it needs to authenticate the very unbond that
    # dissolves it.
    household = household_credential.current()
    fan_results = _fan_out_grouping(
        targets, known=known, token=request_control_token(handler),
        household=household,
    )
    results = [
        {"addr": addr, "ok": ok, "detail": detail}
        for addr, (ok, detail) in zip(addrs, fan_results)
    ]
    dissolved = [r["addr"] for r in results if r["ok"]]
    self_ok = results[0]["ok"]  # self is always targets[0]

    # Name each member we could not disable so a half-dissolved bond — a
    # follower offline at dissolve time, left stranded — is visible in the
    # journal, not just in the aggregate.
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
    # Candidates whose discovery GET failed: a same-bond follower offline at
    # dissolve time never becomes a disable target and stays grouped, so the
    # count explains that report without a per-candidate line.
    unreachable = sum(1 for pg in candidate_groupings if pg is None)
    log_event(
        logger,
        "rooms.unbond",
        bond=bond_id,
        # Keyed on the branch taken, not on the legacy peer_addr, which a
        # full-roster bond also sets to its primary L/R sibling.
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


def resolve_bond_peer(
    grouping: dict, known: set[str] | None = None, *,
    grouping_reader=None,
) -> tuple[str, dict | None, str]:
    """Resolve THIS speaker's one pair sibling → (addr, peer_grouping, err).

    Roster-first: the bond flow records the chosen peer on the leader
    (``peer_addr`` + ``peer_name`` in grouping.env), so pair operations
    resolve THE peer the household picked. When the recorded IP no longer
    answers for OUR bond, a recorded ``peer_name`` is re-found in the live
    directory (DHCP moved the IP). With a roster, a FOREIGN device
    transiently claiming our bond_id cannot create ambiguity — that was the
    observed failure mode, a device cycling through bond states making
    swap/trim/balance fail with "found 2" — and an unreachable roster peer is
    a hard, NAMED error, never an excuse to guess. Bonds recorded before the
    roster existed fall back to the legacy inference (every discovered device
    claiming our bond_id), which still errors on ambiguity.

    ``grouping_reader`` is the one I/O policy seam: mutations use the default
    full-timeout reader, read-only UI snapshots a shorter one, without
    duplicating peer-resolution rules.

    ``err`` is "" on success; on failure addr is "" and grouping None.
    """
    if known is None:
        known = self_addresses()
    read_grouping = grouping_reader or _get_member_grouping
    bond_id = str(grouping.get("bond_id") or "").strip()
    roster_addr = str(grouping.get("peer_addr") or "").strip()
    roster_name = str(grouping.get("peer_name") or "").strip()

    if roster_addr:
        pg = read_grouping(roster_addr, known)
        if (pg is not None
                and str(pg.get("bond_id") or "").strip() == bond_id):
            return roster_addr, pg, ""
        if roster_name:
            for row in discover_speakers_cached():
                if str(row.get("name") or "").strip() != roster_name:
                    continue
                addr = str(row.get("address") or "").strip()
                if not addr or addr in known or addr == roster_addr:
                    continue
                pg2 = read_grouping(addr, known)
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
            for sp in discover_speakers_cached()
        ) if a and a not in known
    ]
    candidate_groupings = _map_peers(
        lambda a: read_grouping(a, known), candidate_addrs,
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

    A channel-assignment edit, never a leadership change: roles, bond_id and
    leader_addr are untouched. Each member's outputd ChannelPick drops the
    other side once its reconciler applies the change (about a one-period
    blip per speaker).

    Deliberately scoped to the 2-speaker left/right pair: it requires exactly
    ONE same-bond peer and a {left, right} channel set, since a mono or
    multi-member bond has no well-defined "swap" and 400s with the reason."""
    grouping = read_grouping_state()
    bond_id = str(grouping.get("bond_id") or "").strip()
    if not grouping.get("enabled") or not bond_id:
        _send_json(
            handler, {"ok": False, "error": "not in a bond"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    known = self_addresses()
    peer_addr_r, peer_grouping, perr = resolve_bond_peer(grouping, known)
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
        # A same-channel pair is the residue of an interrupted swap whose
        # rollback also failed. A strict left/right precondition would make
        # Swap the one button that CANNOT fix the state Swap created, so this
        # completes the interrupted intent instead: any {left,right}
        # assignment beats a stuck same-channel pair, and one more tap swaps
        # again if it lands backwards.
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
    token = request_control_token(handler)
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
    # pair SAME-channel — audibly wrong, and it blocks a retry because the
    # {left,right} precondition no longer holds. Best-effort rollback returns
    # the member that DID flip to its original channel; a failed rollback is
    # surfaced, never silent.
    rolled_back = None
    if not all_ok and any(r["ok"] for r in results):
        ok_idx = 0 if results[0]["ok"] else 1
        rb_addr = targets[ok_idx][0]
        rb_grouping = grouping if ok_idx == 0 else peer_grouping
        rb_channel = self_channel if ok_idx == 0 else peer_channel
        rb_ok, rb_detail = post_grouping_to_member(
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


def _trim_float(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _balance_trims_from_db(balance_db: float) -> tuple[float, float, bool]:
    """Map a signed balance slider to absolute trims.

    ``balance_db`` is positive toward RIGHT and negative toward LEFT. The
    louder side stays at 0 dB and the opposite side is attenuated, so the pair
    keeps as much digital headroom as the requested relative balance allows.
    """
    from ..multiroom.config import TRIM_DB_MIN, TRIM_DB_MAX

    requested = float(balance_db)
    left = min(TRIM_DB_MAX, -requested)
    right = min(TRIM_DB_MAX, requested)
    left_clamped = max(TRIM_DB_MIN, left)
    right_clamped = max(TRIM_DB_MIN, right)
    return round(left_clamped, 1), round(right_clamped, 1), (
        left != left_clamped or right != right_clamped
    )


def _balance_db_from_trims(left_trim_db: float, right_trim_db: float) -> float:
    """Signed slider value: positive means right is louder than left."""
    return round(float(right_trim_db) - float(left_trim_db), 1)


def _get_member_grouping_for_balance_snapshot(
    addr: str, known: set[str] | None = None,
) -> dict | None:
    return _get_member_grouping(
        addr, known, timeout=BALANCE_SNAPSHOT_PEER_TIMEOUT_SEC,
    )


def _pair_balance_snapshot(grouping: dict, known: set[str] | None = None) -> dict:
    """Compact live balance state for the /sound/pair/ slider.

    The snapshot is present only for a two-speaker left/right bond. It resolves
    the peer through the same roster-first path used by swap/trim so the UI does
    not display a stale peer trim.
    """
    if not grouping.get("enabled") or grouping.get("error"):
        return {"applicable": False}
    self_channel = str(grouping.get("channel") or "").strip()
    if self_channel not in ("left", "right"):
        return {"applicable": False}
    bond_id = str(grouping.get("bond_id") or "").strip()
    if not bond_id:
        return {"applicable": False}
    if known is None:
        known = self_addresses()
    peer_addr, peer_grouping, perr = resolve_bond_peer(
        grouping, known,
        grouping_reader=_get_member_grouping_for_balance_snapshot,
    )
    if perr:
        return {"applicable": True, "ok": False, "error": perr}
    assert peer_grouping is not None
    peer_channel = str(peer_grouping.get("channel") or "").strip()
    if {self_channel, peer_channel} != {"left", "right"}:
        return {
            "applicable": True,
            "ok": False,
            "error": (
                "balance needs one left and one right speaker "
                f"(this speaker is {self_channel or '?'}, peer is "
                f"{peer_channel or '?'})"
            ),
        }
    self_trim = round(_trim_float(grouping.get("trim_db")), 1)
    peer_trim = round(_trim_float(peer_grouping.get("trim_db")), 1)
    if self_channel == "left":
        left_trim, right_trim = self_trim, peer_trim
    else:
        left_trim, right_trim = peer_trim, self_trim
    return {
        "applicable": True,
        "ok": True,
        "left_trim_db": left_trim,
        "right_trim_db": right_trim,
        "balance_db": _balance_db_from_trims(left_trim, right_trim),
        "self_channel": self_channel,
        "peer_channel": peer_channel,
        "peer_addr": peer_addr,
    }


def _grouping_body_with_trim(grouping: dict, trim_db: float) -> dict:
    return {
        "enabled": True,
        "role": str(grouping.get("role") or ""),
        "channel": str(grouping.get("channel") or ""),
        "bond_id": str(grouping.get("bond_id") or ""),
        "leader_addr": str(grouping.get("leader_addr") or ""),
        "trim_db": trim_db,
    }


def _set_pair_balance(handler: BaseHTTPRequestHandler, parsed: dict) -> None:
    """Handle POST /trim with ``target=pair`` and absolute ``balance_db``.

    One slider value rewrites BOTH member trims to the loudness-maximizing
    attenuate-only pair: one side is always 0 dB, the other is <= 0 dB.
    """
    if "balance_db" not in parsed:
        _send_json(
            handler,
            {"ok": False, "error": "balance_db must be a number"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    try:
        balance_db = float(parsed.get("balance_db"))
    except (TypeError, ValueError):
        _send_json(
            handler,
            {"ok": False, "error": "balance_db must be a number"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    if not math.isfinite(balance_db):
        _send_json(
            handler,
            {"ok": False, "error": "balance_db must be finite"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    grouping = read_grouping_state()
    if (not grouping.get("enabled") or grouping.get("error")
            or not str(grouping.get("bond_id") or "").strip()):
        _send_json(
            handler, {"ok": False, "error": "not in a bond"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    self_channel = str(grouping.get("channel") or "").strip()
    if self_channel not in ("left", "right"):
        _send_json(
            handler,
            {"ok": False, "error": "balance needs a left/right pair"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    known = self_addresses()
    peer_addr, peer_grouping, perr = resolve_bond_peer(grouping, known)
    if perr:
        _send_json(
            handler,
            {"ok": False, "error": f"balance {perr}"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    assert peer_grouping is not None
    peer_channel = str(peer_grouping.get("channel") or "").strip()
    if {self_channel, peer_channel} != {"left", "right"}:
        _send_json(
            handler,
            {"ok": False, "error": (
                "balance needs a left/right pair (this speaker is "
                f"{self_channel or '?'}, peer is {peer_channel or '?'})"
            )},
            status=HTTPStatus.BAD_REQUEST,
        )
        return

    left_trim, right_trim, clamped = _balance_trims_from_db(balance_db)
    trims_by_channel = {"left": left_trim, "right": right_trim}
    members = [
        (
            "",
            grouping,
            trims_by_channel[self_channel],
            round(_trim_float(grouping.get("trim_db")), 1),
        ),
        (
            peer_addr,
            peer_grouping,
            trims_by_channel[peer_channel],
            round(_trim_float(peer_grouping.get("trim_db")), 1),
        ),
    ]
    members.sort(key=lambda item: item[0] == "")  # peer first

    token = request_control_token(handler)
    results: list[dict] = []
    applied: list[tuple[str, dict, float]] = []
    rollbacks: list[dict] = []
    all_ok = True
    for addr, member_grouping, trim, original_trim in members:
        ok, detail = post_grouping_to_member(
            addr,
            _grouping_body_with_trim(member_grouping, trim),
            known,
            token=token,
        )
        results.append({
            "addr": addr,
            "channel": str(member_grouping.get("channel") or ""),
            "trim_db": trim,
            "ok": ok,
            "detail": detail,
        })
        all_ok = all_ok and ok
        if not ok:
            break
        applied.append((addr, member_grouping, original_trim))

    if not all_ok and applied:
        for rb_addr, rb_grouping, original_trim in reversed(applied):
            rb_ok, rb_detail = post_grouping_to_member(
                rb_addr,
                _grouping_body_with_trim(rb_grouping, original_trim),
                known,
                token=token,
            )
            rollback = {
                "addr": rb_addr,
                "channel": str(rb_grouping.get("channel") or ""),
                "trim_db": original_trim,
                "ok": rb_ok,
                "detail": rb_detail,
            }
            rollbacks.append(rollback)
            log_event(
                logger,
                "rooms.balance.rollback",
                addr=rb_addr or "(self)",
                channel=rollback["channel"],
                trim=f"{original_trim:.1f}",
                ok=rb_ok,
                detail=rb_detail,
                level=logging.WARNING,
            )

    log_event(
        logger,
        "rooms.balance",
        requested=f"{balance_db:.1f}",
        left=f"{left_trim:.1f}",
        right=f"{right_trim:.1f}",
        clamped=clamped,
        ok=all_ok,
    )
    payload = {
        "ok": all_ok,
        "balance": {
            "applicable": True,
            "ok": all_ok,
            "left_trim_db": left_trim,
            "right_trim_db": right_trim,
            "balance_db": _balance_db_from_trims(left_trim, right_trim),
            "clamped": clamped,
        },
        "results": results,
    }
    if rollbacks:
        payload["rollbacks"] = rollbacks
    _send_json(
        handler,
        payload,
        status=HTTPStatus.OK if all_ok else HTTPStatus.BAD_GATEWAY,
    )


def _set_member_trim(handler: BaseHTTPRequestHandler) -> None:
    """Handle POST /trim: set the pair balance absolutely.

    Body ``{target: "pair", balance_db}`` — the rooms slider's only trim
    write."""
    parsed, err = read_json_body(handler, max_bytes=_PEERING_BODY_LIMIT)
    if err is not None:
        _send_json(handler, {"ok": False, "error": err},
                   status=HTTPStatus.BAD_REQUEST)
        return
    _set_pair_balance(handler, parsed)


def _make_handler():
    """Build the request handler class. No state paths are captured here, so
    every request re-reads mDNS, grouping and peering.env."""

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
            if self.path not in (
                "/peering",
                "/bond",
                "/unbond",
                "/swap",
                "/trim",
            ):
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
