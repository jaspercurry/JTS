"""Read the speaker's *effective* network identity.

``jasper-identity-reconcile`` (a boot + 5-min systemd timer, the single
writer) snapshots the three names a speaker really has into
``/var/lib/jasper/identity.env``:

  * the OS hostname (what Avahi *tries* to advertise),
  * Avahi's effective mDNS hostname (what the LAN can actually resolve —
    differs after an RFC 6762 collision rename to ``<name>-2.local``),
  * ``JASPER_HOSTNAME`` (the *intended* identity that the TLS cert,
    OAuth bounce, and spoken URLs derive from).

This module is the read side, mirroring
:mod:`jasper.voice.provider_state`'s lesson: long-lived daemons must
re-read wizard/reconciler-owned files fresh, never trust the
``os.environ`` snapshot taken at process start. Two consumers with
different needs:

  * :func:`effective_hostnames` — called by
    :mod:`jasper.http_security` on **every management request**, so it
    keeps an mtime/size-keyed cache (one ``stat()`` per call, a re-parse
    only when the reconciler rewrote the file). A renamed speaker's
    management UI stays reachable because the allowlist learns
    ``jts-2.local`` within one reconciler period.
  * :func:`snapshot` — the ``/state.resilience.identity`` and doctor
    surface; reads fresh, derives a status, never raises.

A missing file (fresh install before the first reconciler run, dev
checkout) degrades to "no extra names / status=absent" — exactly the
pre-reconciler behavior.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from .env_load import parse_env_file
from .http_security import normalize_host

DEFAULT_PATH = "/var/lib/jasper/identity.env"


def identity_path() -> str:
    return os.environ.get("JASPER_IDENTITY_FILE", DEFAULT_PATH)


def read_identity(path: str | None = None) -> dict[str, str]:
    """Parse the identity file fresh. ``{}`` for missing/unreadable."""
    return parse_env_file(path or identity_path())


def _names_from(identity: dict[str, str]) -> frozenset[str]:
    """Derive the allowlist-shaped name set from a parsed identity file.

    For each recorded hostname both the bare and ``.local`` forms are
    included, mirroring ``http_security._configured_hostnames``'s
    treatment of the configured name — a browser may present either."""
    names: set[str] = set()
    for key in (
        "JASPER_IDENTITY_OS_HOSTNAME",
        "JASPER_IDENTITY_AVAHI_HOSTNAME",
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME",
    ):
        value = normalize_host(identity.get(key, ""))
        if not value:
            continue
        names.add(value)
        if value.endswith(".local"):
            names.add(value[: -len(".local")])
        else:
            names.add(f"{value}.local")
    return frozenset(names)


# (path, mtime_ns, size) -> names. One entry — there is one identity
# file per speaker; the tuple key just makes staleness detection exact.
_cache_lock = threading.Lock()
_cache_key: tuple[str, int, int] | None = None
_cache_names: frozenset[str] = frozenset()


def effective_hostnames(path: str | None = None) -> frozenset[str]:
    """Hostnames this speaker is *actually* reachable as, per the last
    reconciler run. Request-path cheap: one ``stat()`` unless the file
    changed. Empty set when the file is absent (reconciler hasn't run)."""
    global _cache_key, _cache_names
    resolved = path or identity_path()
    try:
        st = os.stat(resolved)
        key = (resolved, st.st_mtime_ns, st.st_size)
    except OSError:
        return frozenset()
    with _cache_lock:
        if key == _cache_key:
            return _cache_names
    names = _names_from(parse_env_file(resolved))
    with _cache_lock:
        _cache_key = key
        _cache_names = names
    return names


def snapshot(path: str | None = None) -> dict[str, Any]:
    """State surface for ``/state.resilience.identity`` and the doctor.

    Always returns a dict, never raises. ``status`` is one of:

      * ``absent``    — reconciler hasn't written the file yet
      * ``collision`` — Avahi renamed us; another device owns our name
      * ``drift``     — intended ``JASPER_HOSTNAME`` differs from what
                        the LAN resolves (stale env after a rename)
      * ``ok``        — all three names agree
    """
    identity = read_identity(path)
    if not identity:
        return {"status": "absent", "detail": "identity.env not written yet"}
    collision = identity.get("JASPER_IDENTITY_COLLISION") == "1"
    drift = identity.get("JASPER_IDENTITY_DRIFT") == "1"
    status = "collision" if collision else ("drift" if drift else "ok")
    return {
        "status": status,
        "os_hostname": identity.get("JASPER_IDENTITY_OS_HOSTNAME", ""),
        "avahi_hostname": identity.get("JASPER_IDENTITY_AVAHI_HOSTNAME", ""),
        "configured_hostname": identity.get(
            "JASPER_IDENTITY_CONFIGURED_HOSTNAME", ""),
        "avahi_available": identity.get(
            "JASPER_IDENTITY_AVAHI_AVAILABLE", "") == "1",
        "checked_at": identity.get("JASPER_IDENTITY_CHECKED_AT", ""),
    }
