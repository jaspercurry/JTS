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
import re
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


# The ring's slot geometry is NOT a hardcoded 128: the `jts_ring_playback` ioplug
# opens Ring B with the conf.d's ``period_frames``, and jasper-outputd's
# ``ShmRingSource`` attaches with ``JASPER_OUTPUTD_PERIOD_FRAMES`` (one slot per
# DAC period — see rust/jasper-outputd/src/config.rs "the ring's period_frames is
# always outputd's period_frames"). A geometry mismatch against an existing ring
# is a hard ``open()`` error (c/jts-ring-ioplug: "a geometry mismatch against an
# existing ring is an open() error"). The shipped ``60-jts-ring.conf`` pins a
# PLACEHOLDER 128 (the file says so); on a box whose resolved outputd period is
# not 128 (the packaged default is 1024, only the Apple-dongle latency floor is
# 128), CamillaDSP's ring open would fail and the arm would roll back with a
# confusing daemon-level error. So the coupling reconciler PREFLIGHTs this match
# and fail-closes to loopback with a crisp reason (mirrors scripts/ring-proto/
# arm.sh, which renders the conf period from outputd's resolved env per box).
_RING_CONF_PERIOD_RE = re.compile(r"^\s*period_frames\s+(\d+)\s*$", re.MULTILINE)


def ring_conf_period_frames(conf_d: str | None = None) -> int | None:
    """Parse the ``period_frames`` pinned in the ring conf.d, or None.

    Returns the single period value the ``jts_ring_*`` PCM blocks declare (both
    Ring A and Ring B share one slot geometry). ``None`` when the file is absent,
    unreadable, has no ``period_frames`` line, or declares *inconsistent* values
    across the two PCMs (a torn conf.d — the caller treats that as a mismatch, not
    a silent pick). Pure text parse, no ALSA. ``conf_d=None`` resolves
    :data:`RING_CONF_D` at CALL time (not a bound default) so a test / caller that
    repoints the module constant is honored.
    """
    path = RING_CONF_D if conf_d is None else conf_d
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    values = {int(m.group(1)) for m in _RING_CONF_PERIOD_RE.finditer(text)}
    if len(values) != 1:
        # No period line, or the two PCMs disagree — not a usable single geometry.
        return None
    return next(iter(values))


@dataclass(frozen=True)
class RingGeometryMatch:
    """Whether the conf.d ring period matches outputd's resolved DAC period."""

    ok: bool
    conf_period_frames: int | None
    outputd_period_frames: int
    detail: str = ""


def ring_geometry_matches_outputd(
    outputd_period_frames: int,
    *,
    conf_d: str | None = None,
) -> RingGeometryMatch:
    """Check the conf.d ring slot period equals outputd's resolved period.

    The ring slot IS one outputd DAC period (ping-pong: CamillaDSP writes one
    slot per period, outputd reads one slot per period). If the installed conf.d
    period differs from the period outputd will resolve, CamillaDSP's ring
    ``open()`` fails against outputd's existing ring — so arming must be refused
    with a crisp reason instead of a confusing rollback. A missing/torn conf.d
    period is a mismatch (fail-closed), not a pass.
    """
    conf_path = RING_CONF_D if conf_d is None else conf_d
    conf_period = ring_conf_period_frames(conf_path)
    if conf_period is None:
        return RingGeometryMatch(
            ok=False,
            conf_period_frames=None,
            outputd_period_frames=outputd_period_frames,
            detail=(
                f"ring conf.d ({conf_path}) has no single period_frames — the ring "
                "slot geometry is indeterminate; redeploy to reinstall it"
            ),
        )
    if conf_period != outputd_period_frames:
        return RingGeometryMatch(
            ok=False,
            conf_period_frames=conf_period,
            outputd_period_frames=outputd_period_frames,
            detail=(
                f"ring conf.d period_frames={conf_period} != outputd resolved "
                f"JASPER_OUTPUTD_PERIOD_FRAMES={outputd_period_frames}; the ring "
                "slot is one outputd DAC period, so CamillaDSP's ring open would "
                "fail against outputd's ring. Match them (set the conf.d period to "
                f"{outputd_period_frames} or the outputd period to {conf_period}) "
                "before arming"
            ),
        )
    return RingGeometryMatch(
        ok=True,
        conf_period_frames=conf_period,
        outputd_period_frames=outputd_period_frames,
    )
