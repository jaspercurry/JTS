# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grouping prerequisite provisioning — install Snapcast on the grouping opt-in.

``install.sh`` ships the JTS snap *units* (``jasper-snapserver`` /
``jasper-snapclient``) but deliberately does **not** install the snapcast
binaries — an off-by-default footprint posture (most speakers are solo; the
snapcast apt packages pull extra deps + a rogue auto-enabled distro daemon).
The install.sh comment says installing the binaries is "the grouping opt-in's
job", but that opt-in never had an implementation: a box where grouping was
enabled but snapcast was never installed had the units present yet failing on
every start — invisible until a bond, and on an active leader it was the
2026-06-23 reboot-loop trigger.

This module IS that opt-in. The grouping reconciler calls
:func:`ensure_snapcast_installed` whenever grouping is enabled and the binaries
are missing, so the household's "set up multi-room" click installs Snapcast
automatically — ``apt-get`` from the distro repos (GPL-3.0 snapcast run as a
separate process; never bundled into a JTS artifact, never linked, so no
copyleft reaches JTS and no redistribution obligation attaches), with a status
the ``/rooms`` wizard surfaces ("Installing Snapcast…").

**Total + fail-soft + bounded.** A present install is a fast no-op, so the
reconciler's gating (it calls this only when grouping is enabled AND snapcast is
missing) makes this a ONE-TIME op per box, not a recurring network cost. The
install refreshes a stale index + waits out the dpkg lock first (the two most
common Pi apt failures); if it still fails — genuinely offline, a broken mirror
— it is logged + recorded as a ``failed`` status (which
``/state.grouping.provision`` and the doctor's
``check_grouping_snapcast_installed`` surface) and NEVER raises. The reconcile
continues, the snap units simply fail to start, and the box stays solo-safe (the
same fail-closed posture the #965 active-leader gate guarantees). Offline, apt
fails fast (no multi-minute boot hang); the next reconcile / boot retries.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .. import atomic_io
from ..log_event import log_event

logger = logging.getLogger(__name__)

# The two binaries grouping needs: snapserver (the leader's timing master) and
# snapclient (every member's player). Both are in Trixie's apt repos
# (`snapserver` / `snapclient`, v0.31.0).
SNAPCAST_BINARIES = ("snapserver", "snapclient")

# Live progress status for the /rooms wizard. /run (transient) — the durable
# truth is the binaries themselves (the doctor reads those directly); this file
# only carries the "installing…/installed/failed" progress for the live UI, and
# is recreated each boot in the reconciler-owned ARGS_DIR.
PROVISION_STATUS_FILE = "/run/jasper-grouping/provision-status.json"

# The distro's snapserver/snapclient apt packages ship enabled-by-default units
# that squat :1704 + advertise _snapcast._tcp — a rogue second server JTS never
# manages. install.sh neutralises them; a live apt-install re-enables them, so
# we must neutralise again here (mirror systemd-units.sh).
_DISTRO_UNITS = ("snapserver.service", "snapclient.service")

# apt is bounded so a stuck apt never wedges the reconcile (a timeout is a soft
# failure → status=failed → the next reconcile retries). Two robustness knobs
# address the two most common Pi apt failures: a best-effort `apt-get update`
# first refreshes a stale package index (the #1 cause of a spurious "Unable to
# locate package"), and the install waits out the dpkg lock
# (`DPkg::Lock::Timeout` — unattended-upgrades holds it daily) instead of
# instantly failing. Offline, BOTH fail fast (the update can't reach the mirrors,
# the install finds no index), so a misprovisioned + offline boot never sits in
# a multi-minute hang — the install error is the actionable one.
_APT_UPDATE_TIMEOUT_SEC = 120  # best-effort index refresh; result IGNORED
_APT_LOCK_TIMEOUT_SEC = 120    # apt waits this long for the dpkg lock
_APT_TIMEOUT_SEC = 300         # install subprocess ceiling (covers the lock wait)


def snapcast_present(*, which=shutil.which) -> bool:
    """True when BOTH snapcast binaries resolve on PATH. ``which`` is injectable
    for tests; production uses :func:`shutil.which` (the same check the doctor +
    the active-leader precheck use, so they can't disagree)."""
    return all(which(b) is not None for b in SNAPCAST_BINARIES)


def read_provision_status(path: str = PROVISION_STATUS_FILE) -> dict[str, str]:
    """Fresh-read the provision progress for ``/state`` / the wizard, or ``{}``
    when absent/unreadable. Total + fail-soft; never raises."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        "state": str(raw.get("state") or ""),
        "detail": str(raw.get("detail") or ""),
    }


def _write_status(path: str, state: str, detail: str) -> None:
    """Atomically record the install progress. Fail-soft — a lost status write
    must not affect the install or the reconcile (the binaries are the truth)."""
    try:
        atomic_io.atomic_write_text(
            path,
            json.dumps({"state": state, "detail": detail}, sort_keys=True) + "\n",
            mode=0o644,
        )
    except OSError as e:
        log_event(
            logger,
            "multiroom.provision.status_write_failed",
            path=path,
            error=e,
            level=logging.WARNING,
        )


def _neutralise_distro_units(runner) -> bool:
    """Disable the distro's auto-enabled snapserver/snapclient units (mirror
    install.sh). Best-effort — a failure here does not fail the install (JTS owns
    jasper-snapserver/-snapclient; the distro units are merely dead weight)."""
    try:
        result = runner(
            ["systemctl", "disable", "--now", *_DISTRO_UNITS],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log_event(
            logger,
            "multiroom.provision.distro_unit_neutralise_failed",
            error=str(e),
            level=logging.WARNING,
        )
        return False
    rc = int(getattr(result, "returncode", 0) or 0)
    if rc != 0:
        detail = (
            ((getattr(result, "stderr", "") or "")
             + (getattr(result, "stdout", "") or "")).strip()[-300:]
            or f"systemctl exited {rc}"
        )
        log_event(
            logger,
            "multiroom.provision.distro_unit_neutralise_failed",
            rc=rc,
            detail=detail,
            level=logging.WARNING,
        )
        return False
    return True


def _apt_update(runner, *, timeout) -> None:
    """Best-effort ``apt-get update`` — refresh a stale package index (the #1
    cause of a spurious "Unable to locate package"). Bounded + fail-soft: its
    result is IGNORED (the index may already be fresh; offline it fails fast and
    the install's own error is the actionable one)."""
    try:
        runner(
            ["apt-get", "update"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
    except (OSError, subprocess.SubprocessError) as e:
        log_event(
            logger,
            "multiroom.provision.apt_update_failed",
            error=str(e),
            level=logging.WARNING,
        )


def ensure_snapcast_installed(
    *,
    runner=subprocess.run,
    which=shutil.which,
    status_path: str = PROVISION_STATUS_FILE,
    apt_timeout: int = _APT_TIMEOUT_SEC,
    apt_update_timeout: int = _APT_UPDATE_TIMEOUT_SEC,
    apt_lock_timeout: int = _APT_LOCK_TIMEOUT_SEC,
) -> dict[str, str]:
    """Install the snapcast binaries if missing — the grouping opt-in.

    TOTAL + FAIL-SOFT: never raises. Returns ``{"state": ..., "detail": ...}``
    where ``state`` is one of ``present`` (already installed, no-op), ``installed``
    (just installed OK), or ``failed`` (apt error / missing after install — the
    detail carries the reason). Idempotent: a present install short-circuits to
    a ``snapcast_present`` check with no apt call. Writes the live progress to
    ``status_path`` for the ``/rooms`` wizard.

    ``runner`` / ``which`` / ``status_path`` / ``apt_timeout`` are injectable for
    tests; production drives real ``apt-get`` + ``shutil.which``.
    """
    if snapcast_present(which=which):
        _write_status(status_path, "present", "snapserver + snapclient already installed")
        return {"state": "present", "detail": ""}

    _write_status(status_path, "installing", "installing snapserver + snapclient (~1-2 min)")
    log_event(logger, "multiroom.provision.snapcast_install_start")
    # Refresh a stale index first (best-effort), then install — waiting out the
    # dpkg lock rather than failing instantly if unattended-upgrades holds it.
    _apt_update(runner, timeout=apt_update_timeout)
    try:
        result = runner(
            [
                "apt-get", "install", "-y",
                "-o", f"DPkg::Lock::Timeout={apt_lock_timeout}",
                "snapserver", "snapclient",
            ],
            capture_output=True,
            text=True,
            timeout=apt_timeout,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
    except (OSError, subprocess.SubprocessError) as e:
        detail = f"apt-get could not run/complete: {e}"
        _write_status(status_path, "failed", detail)
        log_event(
            logger,
            "multiroom.provision.snapcast_install_failed",
            error=str(e),
            level=logging.ERROR,
        )
        return {"state": "failed", "detail": detail}

    if result.returncode != 0:
        detail = (
            ((result.stderr or "") + (result.stdout or "")).strip()[-300:]
            or f"apt-get exited {result.returncode}"
        )
        _write_status(status_path, "failed", detail)
        log_event(
            logger,
            "multiroom.provision.snapcast_install_failed",
            rc=result.returncode,
            detail=detail,
            level=logging.ERROR,
        )
        return {"state": "failed", "detail": detail}

    # apt-get returned 0 — but verify the binaries actually resolve (a partial
    # index / held package can exit 0 without providing the binary).
    if not snapcast_present(which=which):
        detail = "apt-get succeeded but snapserver/snapclient are still not on PATH"
        _write_status(status_path, "failed", detail)
        log_event(
            logger,
            "multiroom.provision.snapcast_install_failed",
            detail=detail,
            level=logging.ERROR,
        )
        return {"state": "failed", "detail": detail}

    _neutralise_distro_units(runner)
    _write_status(status_path, "installed", "snapserver + snapclient installed")
    log_event(logger, "multiroom.provision.snapcast_install_ok")
    return {"state": "installed", "detail": ""}
