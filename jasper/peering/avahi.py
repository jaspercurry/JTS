# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Render and install the Avahi service file for `_jasper-peer._udp`.

Avahi (the system mDNS-SD daemon installed on Pi OS by default) is
the only mDNS responder on the host — we never advertise from the
Python side, which would hit the dual-stack conflict
(see https://github.com/pyvisa/pyvisa-py/issues/378).

Static template at `/etc/jasper/avahi-templates/jasper-peer.service`
is installed by `deploy/install.sh`. At runtime, this module
substitutes `peer_id`, `room`, and `primary` and writes the rendered
file to `/etc/avahi/services/jasper-peer.service`. Avahi picks up the
change on its own via inotify.

When peering is turned off, the rendered file is removed. Removal is
the single switch that distinguishes "peering off" from "peering on"
at the network level — when off, we're invisible to other JTS speakers.
"""
from __future__ import annotations

import logging
import os

from jasper.net import avahi_service
from jasper.net.avahi_service import RenderResult
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
) -> bool:
    """Render the Avahi service template with this peer's metadata
    and atomic-write it into /etc/avahi/services/.

    Returns True if the file was written (or already up-to-date),
    False if the template is missing or unreadable (in which case the
    caller should log + fall back to running without advertising —
    still browses + arbitrates, just won't be visible to others).

    Unchanged content skips the write.
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
) -> None:
    """Remove the rendered Avahi service file (best-effort).

    Called when peering is turned off via the wizard. Other peers on
    the network stop seeing us in their browse results once Avahi's
    inotify watch picks up the removal. Idempotent: if the file is
    already missing, this is a no-op.
    """
    try:
        os.unlink(rendered_path)
        log_event(logger, "peering.avahi.uninstalled", path=rendered_path)
    except FileNotFoundError:
        return  # already gone — nothing to do
    except OSError as e:
        logger.warning("peering: could not remove %s: %s", rendered_path, e)
        return
