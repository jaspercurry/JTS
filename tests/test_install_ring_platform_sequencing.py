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
    must remove the explicit ring files before any systemd unit restart can
    observe stale geometry.

    #2285 P2 added the ACTIVE ring to the set. Its other deleter
    (``_delete_stale_ring_files``, inside ``_converge_ring``) is bypassed on an
    operator-pinned box, and every armed fleet box is pinned — so before this,
    the one ring file a roleful box actually runs on was the one ring file no
    deploy cleared.

    The set is asserted EXACTLY, not by membership: a new ring must join this
    contract deliberately rather than inherit a stale-geometry deploy by
    omission, which is the shape of the gap this closed. #3118 added the
    DAC-content return ring that way — its reader is outputd, which carries the
    same ``StartLimitAction=reboot`` as the fan-in case above.
    """

    body = _function_body(
        RING_PLATFORM_SH.read_text(encoding="utf-8"),
        "install_jts_ring_platform",
    )

    unlinked = re.findall(r"^\s*rm -f (\S+)$", body, re.M)
    assert unlinked == [
        "/dev/shm/jts-ring/program.ring",
        "/dev/shm/jts-ring/content.ring",
        "/dev/shm/jts-ring/active-content.ring",
        "/dev/shm/jts-ring/dac-content.ring",
    ]
    assert "/dev/shm/jts-ring/*" not in body, "ring cleanup must not use globs"
    # RING FILES ONLY. A `.writer.lock` / `.open.lock` unlink would open a
    # silent inode-tear window between two holders, so the deleter must never
    # grow one.
    assert all(path.endswith(".ring") for path in unlinked), unlinked
    assert body.index("install_jts_ring_conf_assets") < body.index(
        "rm -f /dev/shm/jts-ring/program.ring"
    )


def test_the_installer_clears_every_ring_the_platform_knows_about():
    """The unlink set is the ring-asset SSOT's own set, not a hand-kept list.

    Derived from ``jasper.ring_assets`` rather than restated, so a ring added
    there and not here fails HERE — the direction that matters, since the file
    that motivated this (the ACTIVE ring) was added to the platform years after
    the deleter was written and silently never joined it.

    The DAC-content return ring (#3118) is named separately because it is
    deliberately NOT a registry member — like the grouping ring it is neither the
    coupling's wire nor a renderer lane, so it owns its identity in
    :mod:`jasper.multiroom.dac_content_ring`. Naming it here keeps the derived
    half derived: a registry ring that skips the deleter still fails.
    """
    from jasper.multiroom.dac_content_ring import DAC_CONTENT_RING_FILE
    from jasper.ring_assets import (
        RING_ACTIVE_CONTENT_FILE,
        RING_B_CONTENT_FILE,
        RING_A_PROGRAM_FILE,
    )

    body = _function_body(
        RING_PLATFORM_SH.read_text(encoding="utf-8"),
        "install_jts_ring_platform",
    )
    unlinked = set(re.findall(r"^\s*rm -f (\S+)$", body, re.M))
    assert unlinked == {
        RING_A_PROGRAM_FILE,
        RING_B_CONTENT_FILE,
        RING_ACTIVE_CONTENT_FILE,
        DAC_CONTENT_RING_FILE,
    }


def test_full_install_runs_ring_platform_before_systemd_units():
    body = _function_body(INSTALL_SH.read_text(encoding="utf-8"), "main")

    full_start = body.index("    fi\n    require_root")
    full_body = body[full_start:]
    assert _call_pos(full_body, "install_jts_ring_platform") < _call_pos(
        full_body, "install_systemd_units"
    )


def test_streambox_install_runs_ring_platform_before_streambox_systemd_units():
    body = _function_body(INSTALL_SH.read_text(encoding="utf-8"), "main")

    streambox_start = body.index('if [[ "${install_profile}" == "streambox" ]]')
    streambox_end = body.index("    fi\n    require_root", streambox_start)
    streambox_body = body[streambox_start:streambox_end]
    assert _call_pos(streambox_body, "install_jts_ring_platform") < _call_pos(
        streambox_body, "install_streambox_systemd_units"
    )


def _assert_camilla_restart_stays_after_dsp_reconcile(function_name: str):
    body = _function_body(
        SYSTEMD_UNITS_SH.read_text(encoding="utf-8"),
        function_name,
    )
    fanin_restart = body.index("systemctl restart jasper-fanin.service")
    reconcile = body.index("reconcile_sound_dsp_state", fanin_restart)
    # The restart step is a named helper shared by both install paths; ADR-0100
    # deleted the width-flip release that used to make it choose `start` over
    # `try-restart`, so today it is unconditional — the ordering contract is
    # about that step, whatever it is spelled.
    camilla_restart = body.index(
        "restart_core_camilla_after_dsp_reconcile", reconcile
    )
    vulnerable_window = body[fanin_restart:reconcile]

    assert fanin_restart < reconcile < camilla_restart
    assert "JASPER_RESTART_CAMILLA_ON_STATEFILE_REPAIR=1" not in vulnerable_window
    assert "try-restart jasper-camilla.service" not in vulnerable_window
    assert "restart jasper-camilla.service" not in vulnerable_window
    assert "start jasper-camilla.service" not in vulnerable_window
    assert "restart_core_camilla_after_dsp_reconcile" not in vulnerable_window


def test_camilla_restart_stays_after_dsp_reconcile_in_systemd_units():
    """Camilla must not restart in the fan-in-restart to DSP-reconcile window.

    With stale ring files deleted early, fan-in creates fresh 2-slot rings. If
    Camilla restarts before reconcile_sound_dsp_state re-emits the ring config,
    it can still load an old chunk-256 statefile against that fresh 2-slot ring.
    Keep the restart after the reconcile so the deploy window is bounded by the
    existing core-audio bounce contract instead of a second geometry race.
    """

    _assert_camilla_restart_stays_after_dsp_reconcile("install_systemd_units")


def test_camilla_restart_stays_after_dsp_reconcile_in_streambox_units():
    _assert_camilla_restart_stays_after_dsp_reconcile("start_streambox_runtime_units")
