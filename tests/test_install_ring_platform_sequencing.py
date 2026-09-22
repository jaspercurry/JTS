# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install sequencing contracts for the SHM ring platform."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from jasper.multiroom.dac_content_ring import DAC_CONTENT_RING_FILE
from jasper.ring_assets import (
    RING_A_PROGRAM_FILE,
    RING_ACTIVE_CONTENT_FILE,
    RING_B_CONTENT_FILE,
)
from tests.install_surface import INSTALL_LIB_DIR
from tests.test_install_core_audio_graph_loop import (
    _PARK_RECORD_CHAIN,
    _profile_runtime_harness,
    _stateful_systemctl,
)


RING_PLATFORM_SH = INSTALL_LIB_DIR / "ring-platform.sh"
SYSTEMD_UNITS_SH = INSTALL_LIB_DIR / "systemd-units.sh"


def _canonical_park_rosters() -> dict[str, list[str]]:
    script = f"""
set -euo pipefail
REPO_DIR={RING_PLATFORM_SH.parents[3]}
SYSTEMD_DIR=/etc/systemd/system
source {SYSTEMD_UNITS_SH}
printf 'restart:%s\n' "${{JASPER_CORE_GRAPH_RESTART_TARGETS[@]}}"
printf 'park:%s\n' "${{JASPER_CORE_GRAPH_PARK_UNITS[@]}}"
printf 'low:%s\n' "${{JASPER_LOW_MEMORY_BUILD_PARK_UNITS[@]}}"
"""
    result = subprocess.run(
        ["bash", "-c", script], check=True, capture_output=True, text=True
    )
    rosters = {"restart": [], "park": [], "low": []}
    for line in result.stdout.splitlines():
        name, unit = line.split(":", 1)
        rosters[name].append(unit)
    return rosters


def _run_profile(
    tmp_path: Path,
    profile: str,
    *,
    low_memory: bool = False,
    camilla_active: bool = True,
    cleanup_fails: bool = False,
    stop_failure: str | None = None,
    missing_unit: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], set[str]]:
    calls = tmp_path / "calls.log"
    state_setup = _stateful_systemctl(tmp_path)
    if not camilla_active:
        state_setup += f': > "{tmp_path}/down/jasper-camilla.service"\n'

    exceptional_unit = stop_failure or missing_unit
    if exceptional_unit:
        load_state = "not-found" if missing_unit else "loaded"
        state_setup += f"""
eval "$(declare -f systemctl | sed '1s/systemctl/_base_systemctl/')"
systemctl() {{
    if [[ "${{1:-}}" == stop && " $* " == *" {exceptional_unit} "* ]]; then
        echo "systemctl $*" >> "{calls}"
        return 5
    fi
    if [[ "${{1:-}}" == show && "${{!#}}" == "{exceptional_unit}" ]]; then
        echo "systemctl $*" >> "{calls}"
        echo {load_state}
        return 0
    fi
    _base_systemctl "$@"
}}
"""

    low_memory_park = "park_low_memory_build_units" if low_memory else ":"
    extra_shims = f"""
{state_setup}
remove_stale_jts_ring_data_files() {{
    echo ring-remove >> "{calls}"
    return {1 if cleanup_fails else 0}
}}
build_swap_required() {{ return 0; }}
install_run_bounded() {{ return 0; }}
require_outputd_ready() {{ return 0; }}
ensure_outputd_camilla_statefile() {{ return 0; }}
reconcile_sound_dsp_state() {{ echo "fn reconcile_sound_dsp_state" >> "{calls}"; }}
reconcile_aec_state() {{ return 0; }}
reconcile_grouping_state() {{ return 0; }}
resolve_fanin_coupling_default() {{ return 0; }}
_build_sandbox_log() {{ return 0; }}
trap 'unpark_recorded_units || true' EXIT
set -e
{low_memory_park}
"""
    script = _profile_runtime_harness(
        tmp_path,
        profile,
        keep=(
            *_PARK_RECORD_CHAIN,
            "park_low_memory_build_units",
            "restart_core_camilla_after_dsp_reconcile",
        ),
        extra_shims=extra_shims,
        epilogue="trap - EXIT\n",
    )
    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    recorded = calls.read_text(encoding="utf-8").splitlines()
    down = {path.name for path in (tmp_path / "down").iterdir()}
    return result, recorded, down


def test_ring_platform_installs_assets_without_unlinking_live_rings(
    tmp_path: Path,
) -> None:
    """Asset installation is safe while the current graph is still live."""
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
    assert log.read_text().splitlines() == ["build", "conf"]


def test_ring_cleanup_removes_only_data_files(tmp_path: Path) -> None:
    """The stopped-graph caller owns timing; this helper owns the file set."""
    log = tmp_path / "calls.log"
    script = f"""
set -euo pipefail
set -f
REPO_DIR={RING_PLATFORM_SH.parents[3]}
source {RING_PLATFORM_SH}
rm() {{ printf 'rm' >> "$JTS_TEST_LOG"; printf ' %s' "$@" >> "$JTS_TEST_LOG"; printf '\n' >> "$JTS_TEST_LOG"; }}
remove_stale_jts_ring_data_files
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
    assert log.read_text().splitlines() == [f"rm -f {' '.join(rings)}"]


def _stop_units(calls: list[str]) -> list[str]:
    return [
        call.split()[-1]
        for call in calls
        if call.startswith("systemctl stop ")
    ]


@pytest.mark.parametrize(
    ("profile", "low_memory"),
    (
        ("install_systemd_units", False),
        ("install_systemd_units", True),
        ("start_streambox_runtime_units", False),
        ("start_streambox_runtime_units", True),
    ),
)
def test_runtime_profiles_stop_all_holders_before_one_cleanup(
    tmp_path: Path, profile: str, low_memory: bool
) -> None:
    result, calls, down = _run_profile(tmp_path, profile, low_memory=low_memory)
    assert result.returncode == 0, result.stderr
    assert calls.count("ring-remove") == 1

    rosters = _canonical_park_rosters()
    cleanup = calls.index("ring-remove")
    for unit in rosters["restart"] + rosters["park"]:
        stop = calls.index(f"systemctl stop {unit}")
        reset = calls.index(f"systemctl reset-failed {unit}")
        assert stop < reset < cleanup
    stops = _stop_units(calls[:cleanup])
    assert stops.index("jasper-fanin.service") < stops.index(
        "jasper-outputd.service"
    )

    fanin = calls.index("systemctl restart jasper-fanin.service", cleanup)
    dsp = calls.index("fn reconcile_sound_dsp_state", fanin)
    camilla = calls.index("systemctl restart jasper-camilla.service", dsp)
    assert cleanup < fanin < dsp < camilla
    assert not any(
        call.startswith(
            (
                "systemctl start jasper-camilla.service",
                "systemctl restart jasper-camilla.service",
                "systemctl try-restart jasper-camilla.service",
            )
        )
        for call in calls[fanin + 1 : dsp]
    )
    assert "jasper-camilla.service" not in down


def test_present_holder_stop_failure_prevents_ring_cleanup(tmp_path: Path) -> None:
    result, calls, _ = _run_profile(
        tmp_path,
        "install_systemd_units",
        stop_failure="jasper-outputd.service",
    )
    assert result.returncode != 0
    assert "ring-remove" not in calls


def test_missing_holder_does_not_block_ring_cleanup(tmp_path: Path) -> None:
    result, calls, _ = _run_profile(
        tmp_path,
        "install_systemd_units",
        missing_unit="jasper-outputd.service",
    )
    assert result.returncode == 0, result.stderr
    assert calls.count("ring-remove") == 1


@pytest.mark.parametrize("low_memory", (False, True))
def test_abort_after_cleanup_restores_entry_active_units(
    tmp_path: Path, low_memory: bool
) -> None:
    result, calls, down = _run_profile(
        tmp_path,
        "install_systemd_units",
        low_memory=low_memory,
        cleanup_fails=True,
    )
    assert result.returncode != 0
    assert calls.count("ring-remove") == 1
    rosters = _canonical_park_rosters()
    expected = set(rosters["restart"] + rosters["park"])
    if low_memory:
        expected.update(rosters["low"])
    assert not (expected & down)


@pytest.mark.parametrize(
    "profile", ("install_systemd_units", "start_streambox_runtime_units")
)
def test_entry_inactive_camilla_stays_inactive_after_dsp_reconcile(
    tmp_path: Path, profile: str
) -> None:
    result, calls, down = _run_profile(tmp_path, profile, camilla_active=False)
    assert result.returncode == 0, result.stderr
    cleanup = calls.index("ring-remove")
    dsp = calls.index("fn reconcile_sound_dsp_state")
    resume = calls.index("systemctl try-restart jasper-camilla.service", dsp)
    assert cleanup < dsp < resume
    assert "jasper-camilla.service" in down
