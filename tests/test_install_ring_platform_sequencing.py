# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install sequencing contracts for the SHM ring platform."""

from __future__ import annotations

import re

from tests.install_surface import INSTALL_LIB_DIR, INSTALL_SH


RING_PLATFORM_SH = INSTALL_LIB_DIR / "ring-platform.sh"
SYSTEMD_UNITS_SH = INSTALL_LIB_DIR / "systemd-units.sh"


def _function_body(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\s*\{{\n(?P<body>.*?)\n\}}", text, re.S | re.M)
    assert match is not None, f"could not locate {name}()"
    return match.group("body")


def _call_pos(body: str, name: str) -> int:
    match = re.search(rf"^\s*{re.escape(name)}(?:\s|$)", body, re.M)
    assert match is not None, f"{name} is not called"
    return match.start()


def test_ring_platform_deletes_stale_tmpfs_rings_before_systemd_units():
    """A first deploy to an already-armed box must not reboot mid-install.

    Reboot trap: an old 8-slot /dev/shm/jts-ring/program.ring can survive until
    the deploy restarts jasper-fanin. If the binary default has flipped to 2
    slots and install_systemd_units restarts fan-in before the step-5 coupling
    reconciler deletes the stale ring, fan-in fatally attaches the old geometry,
    burns through StartLimitBurst, and jasper-fanin.service escalates to
    StartLimitAction=reboot before the install can write its build manifest. The
    ring files are tmpfs transport state, never user data, so the platform step
    must remove the explicit program/content files before any systemd unit
    restart can observe stale geometry.
    """

    body = _function_body(
        RING_PLATFORM_SH.read_text(encoding="utf-8"),
        "install_jts_ring_platform",
    )

    assert "rm -f /dev/shm/jts-ring/program.ring" in body
    assert "rm -f /dev/shm/jts-ring/content.ring" in body
    assert "/dev/shm/jts-ring/*" not in body, "ring cleanup must not use globs"
    assert body.index("install_jts_ring_conf_assets") < body.index(
        "rm -f /dev/shm/jts-ring/program.ring"
    )


def test_full_install_runs_ring_platform_before_systemd_units():
    body = _function_body(INSTALL_SH.read_text(encoding="utf-8"), "main")

    full_start = body.index("    fi\n    require_root")
    full_body = body[full_start:]
    assert _call_pos(full_body, "install_jts_ring_platform") < _call_pos(
        full_body, "install_systemd_units"
    )


def test_camilla_restart_stays_after_dsp_reconcile_in_systemd_units():
    """Camilla must not restart in the fan-in-restart to DSP-reconcile window.

    With stale ring files deleted early, fan-in creates fresh 2-slot rings. If
    Camilla restarts before reconcile_sound_dsp_state re-emits the ring config,
    it can still load an old chunk-256 statefile against that fresh 2-slot ring.
    Keep the restart after the reconcile so the deploy window is bounded by the
    existing core-audio bounce contract instead of a second geometry race.
    """

    body = _function_body(
        SYSTEMD_UNITS_SH.read_text(encoding="utf-8"),
        "install_systemd_units",
    )
    fanin_restart = body.index("systemctl restart jasper-fanin.service")
    reconcile = body.index("reconcile_sound_dsp_state", fanin_restart)
    camilla_restart = body.index("systemctl try-restart jasper-camilla.service")
    vulnerable_window = body[fanin_restart:reconcile]

    assert fanin_restart < reconcile < camilla_restart
    assert "JASPER_RESTART_CAMILLA_ON_STATEFILE_REPAIR=1" not in vulnerable_window
    assert "try-restart jasper-camilla.service" not in vulnerable_window
    assert "restart jasper-camilla.service" not in vulnerable_window
