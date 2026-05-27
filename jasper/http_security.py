"""Small HTTP guardrails for JTS' LAN-local management surfaces.

JTS intentionally runs on a trusted household LAN, but browsers make
"LAN-only" less crisp than it sounds: a hostile public web page can try
to POST to private IPs, and DNS rebinding can put a private service
behind an attacker-controlled Host header. These helpers keep the
management endpoints ergonomic for curl, dials, and local dashboards
while rejecting the browser shapes that should never reach a speaker.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
from collections.abc import Mapping


DEFAULT_MANAGEMENT_HOSTNAME = "jts.local"

_RFC1918_IPV4_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_ULA_IPV6_NETWORK = ipaddress.ip_network("fc00::/7")


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is not None:
        return str(value)
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return str(value)
    return ""


def normalize_host(value: str | None) -> str:
    """Return a lowercase host without port or trailing dot.

    Accepts normal Host values (`jts.local:8780`), bracketed IPv6
    (`[::1]:8780`), and bare IPv6 (`::1`). Invalid or empty inputs
    normalize to the empty string, which callers treat as "no Host
    header" rather than as an allowed hostname.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end <= 0:
            return raw.lower().rstrip(".")
        return raw[1:end].lower().rstrip(".")
    if "@" in raw:
        # Host headers do not have userinfo. Return a value that will
        # fail the allowlist instead of trying to salvage it.
        return raw.lower().rstrip(".")
    if raw.count(":") == 1:
        raw = raw.rsplit(":", 1)[0]
    return raw.lower().rstrip(".")


def _configured_hostnames(configured_hostname: str | None = None) -> set[str]:
    configured = normalize_host(
        configured_hostname
        or os.environ.get("JASPER_HOSTNAME")
        or DEFAULT_MANAGEMENT_HOSTNAME,
    )
    names = {"localhost"}
    local_hostname = normalize_host(socket.gethostname())
    if local_hostname:
        names.add(local_hostname)
        names.add(f"{local_hostname}.local")
    if configured:
        names.add(configured)
        if configured.endswith(".local"):
            names.add(configured[:-len(".local")])
        else:
            names.add(f"{configured}.local")
    for raw in os.environ.get("JASPER_MANAGEMENT_ALLOWED_HOSTS", "").split(","):
        extra = normalize_host(raw)
        if extra:
            names.add(extra)
    return names


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_private_or_loopback_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if ip.is_loopback or ip.is_link_local:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in network for network in _RFC1918_IPV4_NETWORKS)
    return ip in _ULA_IPV6_NETWORK


def is_allowed_management_host(
    host: str | None,
    *,
    configured_hostname: str | None = None,
) -> bool:
    """Return True for hostnames/IPs a household should legitimately use."""
    normalized = normalize_host(host)
    if not normalized:
        # HTTP/1.0 clients and a few tiny embedded clients omit Host.
        # That is not a browser DNS-rebinding shape, so keep it working.
        return True
    if normalized in _configured_hostnames(configured_hostname):
        return True
    ip = _parse_ip(normalized)
    return bool(ip and _is_private_or_loopback_ip(ip))


def _origin_host(origin: str) -> str | None:
    raw = origin.strip()
    if not raw or raw == "null":
        return None
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return normalize_host(parsed.hostname)


def _is_loopback_name(host: str) -> bool:
    if host == "localhost":
        return True
    ip = _parse_ip(host)
    return bool(ip and ip.is_loopback)


def _same_request_origin(request_host: str, origin_host: str) -> bool:
    if request_host == origin_host:
        return True
    return _is_loopback_name(request_host) and _is_loopback_name(origin_host)


def _fetch_metadata_allowed(headers: Mapping[str, str]) -> tuple[bool, str]:
    fetch_site = _header(headers, "Sec-Fetch-Site").strip().lower()
    if fetch_site == "cross-site":
        return False, "cross_site_request"
    return True, "ok"


def management_read_allowed(
    headers: Mapping[str, str],
    *,
    configured_hostname: str | None = None,
) -> tuple[bool, str]:
    """Validate Host + Fetch Metadata for GET management requests."""
    request_host = normalize_host(_header(headers, "Host"))
    if request_host and not is_allowed_management_host(
        request_host, configured_hostname=configured_hostname,
    ):
        return False, "host_not_allowed"
    return _fetch_metadata_allowed(headers)


def mutating_request_allowed(
    headers: Mapping[str, str],
    *,
    configured_hostname: str | None = None,
) -> tuple[bool, str]:
    """Validate Host/Origin for a state-changing management request.

    Returns `(allowed, reason)`, where `reason` is stable enough for
    logs/tests. Browser requests with an Origin header must come from
    the same normalized host as the target request. Non-browser clients
    are allowed when their Host header is missing or on the management
    allowlist.
    """
    request_host = normalize_host(_header(headers, "Host"))
    if request_host and not is_allowed_management_host(
        request_host, configured_hostname=configured_hostname,
    ):
        return False, "host_not_allowed"

    ok, reason = _fetch_metadata_allowed(headers)
    if not ok:
        return ok, reason

    origin = _header(headers, "Origin").strip()
    if not origin:
        return True, "ok"
    origin_host = _origin_host(origin)
    if not origin_host:
        return False, "origin_not_allowed"
    if not is_allowed_management_host(
        origin_host, configured_hostname=configured_hostname,
    ):
        return False, "origin_not_allowed"
    if request_host and not _same_request_origin(request_host, origin_host):
        return False, "origin_host_mismatch"
    return True, "ok"
