# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install sequencing contracts for the SHM ring platform."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from jasper.multiroom.dac_content_ring import DAC_CONTENT_RING_FILE
from jasper.ring_assets import (
    RING_A_PROGRAM_FILE,
    RING_ACTIVE_CONTENT_FILE,
    RING_B_CONTENT_FILE,
)
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


def test_ring_platform_clears_owned_rings_after_installing_assets(
    tmp_path: Path,
) -> None:
    """Run the shipped function with command shims and observe its effects."""
    log = tmp_path / "calls.log"
    script = f"""
set -euo pipefail
set -f
REPO_DIR={RING_PLATFORM_SH.parents[3]}
source {RING_PLATFORM_SH}
build_install_jts_ring_ioplug() {{ echo build >> "$JTS_TEST_LOG"; }}
install_jts_ring_conf_assets() {{ echo conf >> "$JTS_TEST_LOG"; }}
rm() {{ printf 'rm' >> "$JTS_TEST_LOG"; printf ' %s' "$@" >> "$JTS_TEST_LOG"; printf '\n' >> "$JTS_TEST_LOG"; }}
install_jts_ring_platform
"""
    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "JTS_TEST_LOG": str(log)},
    )

    assert result.returncode == 0, result.stderr
    rings = (
        RING_A_PROGRAM_FILE,
        RING_B_CONTENT_FILE,
        RING_ACTIVE_CONTENT_FILE,
        DAC_CONTENT_RING_FILE,
    )
    assert log.read_text().splitlines() == [
        "build",
        "conf",
        *(f"rm -f {ring}" for ring in rings),
    ]


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
