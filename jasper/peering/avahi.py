# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Render and install the Avahi service file for `_jasper-peer._udp`.

Avahi (the system mDNS-SD daemon installed on Pi OS by default) is
the only mDNS responder on the host. python-zeroconf is used purely
for browse-side callbacks — we never advertise from the Python side
to avoid the dual-stack conflict
(see https://github.com/pyvisa/pyvisa-py/issues/378).

Static template at `/etc/jasper/avahi-templates/jasper-peer.service`
is installed by `deploy/install.sh`. At runtime, this module
substitutes `peer_id`, `room`, and `primary` and writes the rendered
file to `/etc/avahi/services/jasper-peer.service`. Avahi auto-reloads
via inotify (with SIGHUP as the deterministic fallback).

When peering is turned off, the rendered file is removed and Avahi
reloaded so other peers stop seeing us in their browse results.
Removal is the single switch that distinguishes "peering off" from
"peering on" at the network level — when off, we're invisible to
other JTS speakers.
"""
from __future__ import annotations

import logging
import os
import subprocess

from jasper import avahi_service
from jasper.avahi_service import RenderResult
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


# Where install.sh drops the template. Lives outside /etc/avahi/services
# so Avahi doesn't try to parse it as-is (the placeholders aren't
# valid XML attributes). Owned by root, mode 0644.
DEFAULT_TEMPLATE_PATH = "/etc/jasper/avahi-templates/jasper-peer.service"

# Where the rendered file goes for Avahi to pick up.
DEFAULT_RENDERED_PATH = "/etc/avahi/services/jasper-peer.service"


def render_and_install(
    *,
    peer_id: str,
    room: str,
    primary: bool,
    template_path: str = DEFAULT_TEMPLATE_PATH,
    rendered_path: str = DEFAULT_RENDERED_PATH,
    reload_avahi: bool = True,
) -> bool:
    """Render the Avahi service template with this peer's metadata
    and atomic-write it into /etc/avahi/services/.

    Returns True if the file was written (or already up-to-date),
    False if the template is missing or unreadable (in which case the
    caller should log + fall back to running without advertising —
    still browses + arbitrates, just won't be visible to others).

    The render/guard/atomic-write body is delegated to the shared
    ``jasper.avahi_service.render_service``, which now OWNS the reload: we
    pass ``reload=reload_avahi`` and it reloads avahi-daemon only when it
    actually wrote the file (``RenderResult.WROTE``). The peer metadata
    values (UUID peer_id, constrained room, ``0``|``1`` primary) are
    mDNS-safe, so ``escape=True`` is byte-identical to no escaping. Because
    ``render_service`` reports WROTE vs UNCHANGED vs FAILED directly, we no
    longer read the rendered file before/after to detect a write — a
    byte-stable re-render returns ``UNCHANGED`` and skips both the write
    and the reload.
    """
    substitutions = {
        "__PEER_ID__": peer_id,
        "__ROOM__": room,
        "__PRIMARY__": "1" if primary else "0",
    }

    result = avahi_service.render_service(
        template_path,
        rendered_path,
        substitutions,
        escape=True,
        reload=reload_avahi,
    )
    if result is RenderResult.FAILED:
        return False
    if result is RenderResult.WROTE:
        log_event(
            logger,
            "peering.avahi.installed",
            path=rendered_path,
            peer_id=peer_id,
            room=room,
            primary=int(primary),
        )
    return True


def uninstall(
    *,
    rendered_path: str = DEFAULT_RENDERED_PATH,
    reload_avahi: bool = True,
) -> None:
    """Remove the rendered Avahi service file (best-effort).

    Called when peering is turned off via the wizard. Other peers on
    the network stop seeing us in their browse results within ~1
    second of Avahi's next reload. Idempotent: if the file is already
    missing, this is a no-op.
    """
    try:
        os.unlink(rendered_path)
        log_event(logger, "peering.avahi.uninstalled", path=rendered_path)
    except FileNotFoundError:
        return  # already gone — nothing to do, no need to reload
    except OSError as e:
        logger.warning("peering: could not remove %s: %s", rendered_path, e)
        return
    if reload_avahi:
        _reload_avahi()


def _reload_avahi() -> None:
    """Best-effort SIGHUP to avahi-daemon. inotify usually catches
    changes on its own but reload is deterministic and fast (<100 ms).
    Same pattern used by deploy/install.sh's install_avahi_jasper_control.
    """
    try:
        subprocess.run(
            ["systemctl", "reload", "avahi-daemon"],
            check=False, timeout=4,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("peering: avahi-daemon reload failed: %s", e)
