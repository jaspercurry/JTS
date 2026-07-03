# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where the ``jts_ring`` transport platform assets live, and are they present.

The single source of truth for the three inert ring-platform assets P1 ships:
the compiled ioplug ``.so``, the conf.d PCM definitions
(``pcm.jts_ring_capture`` / ``pcm.jts_ring_playback``), and the
``/dev/shm/jts-ring`` tmpfs directory. Two consumers share this SSOT so the
"which files must exist" contract never drifts:

- ``jasper.cli.doctor.audio.check_ring_platform_assets`` — the deploy-time health
  probe (also open-probes the PCMs; that lives in the doctor because it needs
  ``arecord``/``aplay``).
- ``jasper.fanin.coupling_reconcile`` — the ``shm_ring`` **activation gate**: the
  reconciler refuses to ARM the ring coupling when an asset is missing and
  fail-safes to loopback, so a half-installed ring platform can never strand the
  realtime path (the ioplug would fail to resolve and CamillaDSP would crash-loop
  on its statefile). Presence-only here — an open-probe from the reconciler could
  disturb a live arm, and the doctor already owns the deep probe.

Import-cheap (stdlib only) so the reconciler and the socket-activated web
surfaces can resolve asset presence without pulling in the doctor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The aarch64 ALSA plugin dir the ioplug ``.so`` installs into (the Pi 5 target).
# Duplicated as a literal in ``jasper.cli.doctor.audio`` historically; this is now
# the shared home. The build/install path is ``deploy/lib/install/ring-platform.sh``.
RING_ALSA_PLUGIN_DIR = "/usr/lib/aarch64-linux-gnu/alsa-lib"
RING_IOPLUG_SO = "libasound_module_pcm_jts_ring.so"
RING_CONF_D = "/etc/alsa/conf.d/60-jts-ring.conf"
# The tmpfs directory the ring files live in (shipped by
# ``deploy/tmpfiles/jts-ring.conf``, mode 2775 root:jasper).
RING_SHM_DIR = "/dev/shm/jts-ring"


def ring_ioplug_so_path(*, plugin_dir: str = RING_ALSA_PLUGIN_DIR) -> str:
    """Absolute path of the installed ioplug ``.so``."""
    return os.path.join(plugin_dir, RING_IOPLUG_SO)


@dataclass(frozen=True)
class RingAssetPresence:
    """Which ring-platform assets are present on disk. Presence, not health."""

    so_present: bool
    conf_present: bool
    shm_dir_present: bool

    @property
    def all_present(self) -> bool:
        return self.so_present and self.conf_present and self.shm_dir_present

    def missing(self) -> tuple[str, ...]:
        """Human-readable list of the absent assets (empty when all present)."""
        out: list[str] = []
        if not self.so_present:
            out.append(f"ioplug .so absent ({ring_ioplug_so_path()})")
        if not self.conf_present:
            out.append(f"conf.d absent ({RING_CONF_D})")
        if not self.shm_dir_present:
            out.append(f"{RING_SHM_DIR} absent")
        return tuple(out)


def ring_asset_presence(
    *,
    plugin_dir: str = RING_ALSA_PLUGIN_DIR,
    conf_d: str = RING_CONF_D,
    shm_dir: str = RING_SHM_DIR,
) -> RingAssetPresence:
    """Snapshot which of the three ring-platform assets are present on disk.

    Pure filesystem stat — no ALSA open, no subprocess, leaves no residue. Args
    are injectable so tests can repoint the paths at a tmpdir.
    """
    return RingAssetPresence(
        so_present=os.path.exists(os.path.join(plugin_dir, RING_IOPLUG_SO)),
        conf_present=os.path.exists(conf_d),
        shm_dir_present=os.path.isdir(shm_dir),
    )
