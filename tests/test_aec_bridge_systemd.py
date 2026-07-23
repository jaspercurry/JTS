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


def _values_for(unit_text: str, key: str) -> set[str]:
    values: set[str] = set()
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[")):
            continue
        k, separator, value = stripped.partition("=")
        if separator and k.strip() == key:
            values.update(value.split())
    return values


def test_bridge_camilla_dependency_is_startup_only_not_lifecycle_coupled() -> None:
    """Regression guard for #1264.

    A USB combo change deliberately pauses Camilla around a fan-in restart.
    The bridge's current inputs are the XVF mic plus outputd's final-reference
    UDP stream (or the pre-Camilla ALSA diagnostic fallback), so stopping it
    with Camilla only removes voice's UDP mic producer and causes a 30-second
    voice watchdog ABRT. Keep Camilla as soft startup ordering, while
    jasper-aec-reconcile remains the bridge lifecycle owner.
    """
    unit = UNIT_PATH.read_text()
    assert "jasper-camilla.service" in _values_for(unit, "After")
    assert "jasper-camilla.service" in _values_for(unit, "Wants")
    assert "jasper-camilla.service" not in _values_for(unit, "Requires")
    assert "jasper-camilla.service" not in _values_for(unit, "PartOf")


def test_bridge_does_not_elect_process_wide_rt_to_avoid_rttime_startup_kill() -> None:
    """Regression guard for the 2026-06-27 jts finding: the G6 attempt elected a
    process-wide SCHED_FIFO policy plus a 200 ms RTTIME cap, but the bridge's
    CPU-heavy startup (DTLN / WebRTC-AEC3 model load runs >200 ms) hit the RTTIME
    hard limit while scheduled FIFO and was SIGKILLed (status=9/KILL) in a crash
    loop. jts3 has no XVF mic, so process-wide bridge RT was never exercised
    pre-merge. The bridge must run SCHED_OTHER (it keeps Nice=-10 + realtime IO).
    A future RT election must set the policy on the steady-state audio thread
    AFTER model load, never process-wide via systemd."""
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "CPUSchedulingPolicy") is None, (
        "the AEC bridge must NOT set a process-wide RT policy — FIFO RTTIME-kills "
        "its model-loading startup (see the unit comment / 2026-06-27 jts finding)."
    )
    assert _value_for(unit, "CPUSchedulingPriority") is None
    assert _value_for(unit, "LimitRTTIME") is None
    # Pre-existing knobs stay (harmless with no policy elected); the softer
    # scheduling boosts the bridge always had remain.
    assert _value_for(unit, "LimitRTPRIO") == "99"
    assert _value_for(unit, "Nice") == "-10"


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
    assert "-/var/lib/jasper/usb_mic.env" in env_files
    assert "-/var/lib/jasper/wake_corpus_bridge.env" in env_files
    assert (
        env_files.index("-/etc/jasper/jasper.env")
        < env_files.index("-/var/lib/jasper/usb_mic.env")
        < env_files.index("-/var/lib/jasper/wake_corpus_bridge.env")
    )
