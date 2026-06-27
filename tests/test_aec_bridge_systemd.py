# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lock down jasper-aec-bridge.service's corpus env-file chain."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-aec-bridge.service"


def _value_for(unit_text: str, key: str) -> str | None:
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def test_bridge_elects_rt_in_the_dsp_tier_and_bounds_rttime() -> None:
    """Audio-latency foundation G6. LimitRTPRIO=99 was wired but no RT policy
    was ever elected, so the bridge's mic/AEC loop ran SCHED_OTHER and got
    preempted under load. Elect SCHED_FIFO at priority 28: above Camilla (25,
    the DSP tier it belongs with) but below the fan-in (30) and outputd (35)
    sinks, so the final-output path still wins a starved CPU. LimitRTTIME bounds
    a runaway RT thread to SIGXCPU instead of a watchdog reboot (G4, mandatory
    with any FIFO election)."""
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "CPUSchedulingPolicy") == "fifo"
    bridge_prio = int(_value_for(unit, "CPUSchedulingPriority"))
    assert bridge_prio == 28
    assert _value_for(unit, "LimitRTPRIO") == "99"
    assert _value_for(unit, "LimitRTTIME") == "200000"

    systemd_dir = UNIT_PATH.parent
    camilla_prio = int(
        _value_for(
            (systemd_dir / "jasper-camilla.service").read_text(),
            "CPUSchedulingPriority",
        )
    )
    fanin_prio = int(
        _value_for(
            (systemd_dir / "jasper-fanin.service").read_text(),
            "CPUSchedulingPriority",
        )
    )
    assert camilla_prio < bridge_prio < fanin_prio, (
        f"AEC bridge ({bridge_prio}) must sit above Camilla ({camilla_prio}) "
        f"and below the fan-in sink ({fanin_prio})."
    )


def test_bridge_sources_wake_corpus_env_after_system_env() -> None:
    """The recorder UI writes optional corpus-output flags under
    /var/lib/jasper because jasper-web cannot write /etc. The bridge
    must source that file after /etc/jasper/jasper.env so UI-enabled
    corpus outputs take effect on restart.
    """
    unit = UNIT_PATH.read_text()
    env_files = [
        line.strip().split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("EnvironmentFile=")
    ]

    assert "-/etc/jasper/jasper.env" in env_files
    assert "-/var/lib/jasper/wake_corpus_bridge.env" in env_files
    assert (
        env_files.index("-/etc/jasper/jasper.env")
        < env_files.index("-/var/lib/jasper/wake_corpus_bridge.env")
    )
