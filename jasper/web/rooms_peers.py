# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Peer discovery and the cross-speaker control-plane RPC behind /sound/pair/.

:mod:`jasper.web.rooms_setup` owns the HTTP surface, the pairing/bond model
and the page; this module owns the mDNS directory browse and every call this
speaker makes to another speaker's jasper-control (the SSRF guard, the
bounded fan-out, and the GET /grouping readers).
"""
from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler

from ..identity import reader as identity
from ..control import household_credential
from ..platform.control_client import (
    CONTROL_PORT,
    PEER_RESPONSE_MAX_BYTES,
    peer_detail,
)
from ..net.mdns import browse_once
from ..multiroom.config import is_private_or_loopback_ipv4
from ..multiroom.state import (
    GROUPING_READINESS_KEY,
    parse_grouping_readiness,
    parse_grouping_response,
)


# Fully-qualified with the trailing `.local.` per the python-zeroconf contract.
CONTROL_MDNS_TYPE = "_jasper-control._tcp.local."

CONTROL_HTTP_TIMEOUT_SEC = 5.0

# python-zeroconf re-broadcasts with backoff (1s, 2s, 4s); 2s captures the
# common PTR→SRV→TXT roundtrip on a home LAN without slowing first paint.
DISCOVERY_TIMEOUT_SEC = 2.0

# The page re-polls /rooms.json every 7 s; a fresh zeroconf browse per poll
# would stand up and tear down a multicast listener ~8×/min, contending with
# the host's avahi responder. The household speaker set is near-static, so a
# new speaker showing up within one TTL is acceptable.
DISCOVERY_CACHE_TTL_SEC = 30.0


# ----------------------------------------------------------------------
# Self identity.
# ----------------------------------------------------------------------
#
# Name / room / hostname come from jasper.identity.reader.read_identity, read ONCE
# per request in rooms_setup._build_rooms_payload so the three fields agree
# within one render. The LAN address is NOT part of identity (it is
# NIC-derived), which is why self_addresses / _self_address live here.


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
    jasper.identity.speaker_name_discovery._strip_service_type."""
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
      1. the `name=` TXT record `jasper.net.control_advert` publishes on
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


# ----------------------------------------------------------------------
# Cross-speaker control plane.
# ----------------------------------------------------------------------


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
