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
import re
import subprocess
from pathlib import Path

# Detector for any unresolved __FOO__ placeholder. Catches template
# drift (new token added without updating _TOKENS).
_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")

logger = logging.getLogger(__name__)


# Where install.sh drops the template. Lives outside /etc/avahi/services
# so Avahi doesn't try to parse it as-is (the placeholders aren't
# valid XML attributes). Owned by root, mode 0644.
DEFAULT_TEMPLATE_PATH = "/etc/jasper/avahi-templates/jasper-peer.service"

# Where the rendered file goes for Avahi to pick up.
DEFAULT_RENDERED_PATH = "/etc/avahi/services/jasper-peer.service"

# Token-substitution markers in the template. Keep these conspicuous so
# a partially-rendered file is obvious to humans staring at the disk.
_TOKENS = {
    "__PEER_ID__": "peer_id",
    "__ROOM__": "room",
    "__PRIMARY__": "primary",
}


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
    """
    substitutions = {
        "peer_id": peer_id,
        "room": room,
        "primary": "1" if primary else "0",
    }
    try:
        template = Path(template_path).read_text()
    except FileNotFoundError:
        logger.warning(
            "peering: avahi template missing at %s; advertising disabled. "
            "Re-run deploy/install.sh to install it.",
            template_path,
        )
        return False
    except OSError as e:
        logger.warning("peering: avahi template unreadable (%s); advertising disabled", e)
        return False

    rendered = template
    for token, key in _TOKENS.items():
        rendered = rendered.replace(token, substitutions[key])
    # Sanity check — refuse to install a half-rendered file. Catches
    # template edits that introduce a new placeholder we don't know
    # about, rather than letting Avahi reject the XML.
    stray = _PLACEHOLDER_RE.search(rendered)
    if stray:
        logger.error(
            "peering: avahi template still has unresolved placeholder %r after "
            "substitution — refusing to install. Edit _TOKENS in jasper/peering/avahi.py "
            "to add the substitution.", stray.group(0),
        )
        return False

    # If the rendered output matches what's already on disk, skip the
    # write + reload. Avoids spamming Avahi reloads on every restart.
    try:
        existing = Path(rendered_path).read_text()
        if existing == rendered:
            return True
    except FileNotFoundError:
        pass
    except OSError:
        pass

    try:
        os.makedirs(os.path.dirname(rendered_path), exist_ok=True)
        tmp = rendered_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(rendered)
        os.chmod(tmp, 0o644)
        os.replace(tmp, rendered_path)
    except OSError as e:
        logger.error("peering: could not write %s: %s", rendered_path, e)
        return False

    logger.info(
        "event=peering.avahi.installed path=%s peer_id=%s room=%s primary=%d",
        rendered_path, peer_id, room, int(primary),
    )
    if reload_avahi:
        _reload_avahi()
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
        logger.info("event=peering.avahi.uninstalled path=%s", rendered_path)
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
