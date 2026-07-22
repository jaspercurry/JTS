# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fresh effective-role facts derived by the grouping reconciler.

``grouping.env`` remains the household's requested bond.  A safe reconcile can
refuse that request and land in solo mode without erasing it.  Runtime consumers
therefore need one small, reconciler-owned fact that distinguishes "requested
follower" from "follower actually active".

The status file lives in a dedicated root-owned state directory. Every grant is
also bound to both a fingerprint of the exact parsed request and the Linux boot
that produced it. Missing, malformed, prior-boot, or otherwise stale status
never grants a follower local sources: callers park fail-safe until grouping
reconciles again. The persistent prior role also keeps a newly requested
solo/leader role parked across an interrupted transition or reboot until
grouping publishes the completed transition.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable
from typing import Any, Mapping

from jasper.atomic_io import read_regular_bytes_nofollow

from .config import (
    GroupingConfig,
    follower_leader_addr,
    local_sources_park_reason,
)


FOLLOWER_STATUS_FILE = "/var/lib/jasper-grouping/effective-role.json"
MAX_EFFECTIVE_ROLE_STATUS_BYTES = 4096
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
MAX_BOOT_ID_BYTES = 128


def normalise_boot_id(value: object) -> str:
    """Return one canonical UUID boot id, or ``""`` for untrusted input."""

    if not isinstance(value, str):
        return ""
    try:
        parsed = uuid.UUID(value.strip())
    except (AttributeError, ValueError):
        return ""
    # The all-zero UUID is not a real Linux boot identity. Rejecting it keeps a
    # fabricated placeholder from turning a persistent grant into a fresh one.
    return "" if parsed.int == 0 else str(parsed)


def read_current_boot_id(path: str = BOOT_ID_FILE) -> str:
    """Read Linux's current boot identity through the bounded safe reader.

    The helper is total: unavailable, oversized, non-UTF-8, or malformed input
    returns ``""``. Callers treat that as "freshness unproven" and therefore
    never use a stale status file to grant a requested follower sources.
    ``path`` is injectable for hardware-free tests.
    """

    try:
        data = read_regular_bytes_nofollow(path, max_bytes=MAX_BOOT_ID_BYTES)
        raw = data.decode("ascii")
    except (OSError, UnicodeError):
        return ""
    return normalise_boot_id(raw)


def grouping_request_fingerprint(cfg: GroupingConfig) -> str:
    """Stable identity for one fully parsed grouping request.

    ``GroupingConfig`` is a frozen dataclass whose repr includes every resolved
    field, including nested bond members.  A code change that alters that shape
    intentionally invalidates the old decision and therefore fails safe.
    """

    return hashlib.sha256(repr(cfg).encode("utf-8")).hexdigest()


def read_effective_role_status(
    path: str | None = None,
) -> dict[str, Any]:
    """Read the root-owned role status as untrusted data; total + fail-soft."""

    status_path = path or FOLLOWER_STATUS_FILE
    if status_path == FOLLOWER_STATUS_FILE:
        # This file authorizes source starts. Nofollow protects the final name,
        # but not replacement of that name inside a writable parent. The
        # packaged systemd unit creates /var/lib/jasper-grouping as root:root
        # 0755; refuse the fact if that ownership boundary drifts at runtime.
        try:
            parent = os.lstat(os.path.dirname(status_path))
            inode = os.lstat(status_path)
        except OSError:
            return {}
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != 0
            or parent.st_mode & 0o022
            or not stat.S_ISREG(inode.st_mode)
            or inode.st_uid != 0
            or inode.st_mode & 0o022
        ):
            return {}
    try:
        data = read_regular_bytes_nofollow(
            status_path,
            max_bytes=MAX_EFFECTIVE_ROLE_STATUS_BYTES,
        )
        raw = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return {}
    if not isinstance(raw, dict):
        return {}

    allowed = raw.get("local_sources_allowed")
    return {
        "active_follower": bool(raw.get("active_follower")),
        "active_leader": bool(raw.get("active_leader")),
        "blocked_reason": str(raw.get("blocked_reason") or ""),
        "requested_fingerprint": str(raw.get("requested_fingerprint") or ""),
        "local_sources_allowed": allowed if isinstance(allowed, bool) else None,
        "boot_id": normalise_boot_id(raw.get("boot_id")),
    }


def _status_matches_request(
    cfg: GroupingConfig,
    status: Mapping[str, Any],
) -> bool:
    return status.get("requested_fingerprint") == grouping_request_fingerprint(cfg)


def _status_matches_current_boot(
    status: Mapping[str, Any],
    boot_id_reader: Callable[[], str] | None,
) -> bool:
    """Whether a published decision is proven to belong to this Linux boot."""

    reader = boot_id_reader or read_current_boot_id
    try:
        current = normalise_boot_id(reader())
    except (OSError, RuntimeError, TimeoutError, ValueError):
        current = ""
    persisted = normalise_boot_id(status.get("boot_id"))
    return bool(current and persisted and current == persisted)


def effective_local_sources_park_reason(
    cfg: GroupingConfig,
    *,
    status: Mapping[str, Any] | None = None,
    boot_id_reader: Callable[[], str] | None = None,
) -> str | None:
    """Why the role that actually landed parks local sources, if any.

    A follower request is parked unless a matching, same-boot reconciler
    decision explicitly records that the bond was refused and the speaker
    safely landed solo. A new solo/leader request also remains parked while a
    prior role's explicit source deny is still present; grouping replaces that
    deny only after the non-follower transition has landed. The permission is
    authoritative for both active and dumb followers.
    """

    requested_reason = local_sources_park_reason(cfg)
    snapshot = read_effective_role_status() if status is None else status
    if requested_reason is None:
        if (
            _status_matches_request(cfg, snapshot)
            and snapshot.get("local_sources_allowed") is False
        ):
            return str(snapshot.get("blocked_reason") or "role_transition_in_progress")
        if snapshot.get(
            "local_sources_allowed"
        ) is False and not _status_matches_request(cfg, snapshot):
            return "role_transition_in_progress"
        return None
    if (
        _status_matches_request(cfg, snapshot)
        and _status_matches_current_boot(snapshot, boot_id_reader)
        and snapshot.get("local_sources_allowed") is True
    ):
        return None
    return requested_reason


def effective_follower_leader_addr(
    cfg: GroupingConfig,
    *,
    status: Mapping[str, Any] | None = None,
    boot_id_reader: Callable[[], str] | None = None,
) -> str | None:
    """Leader address only when the requested follower was not refused."""

    leader = follower_leader_addr(cfg)
    if leader is None:
        return None
    if (
        effective_local_sources_park_reason(
            cfg,
            status=status,
            boot_id_reader=boot_id_reader,
        )
        is None
    ):
        return None
    return leader


__all__ = [
    "FOLLOWER_STATUS_FILE",
    "BOOT_ID_FILE",
    "MAX_EFFECTIVE_ROLE_STATUS_BYTES",
    "MAX_BOOT_ID_BYTES",
    "effective_follower_leader_addr",
    "effective_local_sources_park_reason",
    "grouping_request_fingerprint",
    "normalise_boot_id",
    "read_current_boot_id",
    "read_effective_role_status",
]
