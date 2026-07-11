# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-usbsink.service contract: it MUST ride the zram-shielded audio tier.

usbsink is a real-time music SOURCE — its production Rust bridge runs
capture/playback on fixed ALSA periods. Diagnosed on 2026-06-28: usbsink was
the only music-path daemon left OUTSIDE ``jts-audio.slice``, so its pages could
swap to zram and zram-decompression jitter made the audio period miss deadlines
in bursts -> snd-aloop xruns -> drops. Slice membership (the
``MemorySwapMax=0`` swap shield) is a MEMORY policy, not a CPU cap, so it
respects the no-CPU-caps rule.

This pins the membership + the OOM band so a future unit edit can't silently drop
usbsink off the audio tier (the regression that caused the drops).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-usbsink.service"
PYPROJECT_PATH = REPO / "pyproject.toml"


def _value_for(unit_text: str, key: str) -> str | None:
    """Last value for ``key=`` in the unit's [Service] section (last wins, as
    systemd resolves it). Ignores comment lines."""
    val: str | None = None
    for ln in unit_text.splitlines():
        s = ln.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() == key:
            val = v.strip()
    return val


def _line_index(unit_text: str, needle: str) -> int:
    for idx, line in enumerate(unit_text.splitlines()):
        if needle in line:
            return idx
    raise AssertionError(f"{needle!r} not found in unit")


def _exec_condition_lines(unit_text: str) -> list[str]:
    return [
        s.partition("=")[2].strip()
        for ln in unit_text.splitlines()
        if not (s := ln.strip()).startswith("#") and s.startswith("ExecCondition=")
    ]


def test_unit_file_exists():
    assert UNIT_PATH.exists(), f"jasper-usbsink.service missing at {UNIT_PATH}"


def test_parks_cleanly_without_a_composed_uac2_function():
    """review core-4: the bridge must ExecCondition-gate on the composed uac2
    function so a pre-reboot enable (no UDC → gadget composed nothing) parks
    cleanly instead of restart-looping the 30 s wait-card forever.

    The path is a hardcoded literal (systemd ExecCondition can't expand the
    gadget scripts' JASPER_CONFIGFS_ROOT); it must match the gadget dir name
    jts-usb-audio + functions/uac2.usb0, and must be ordered BEFORE the
    wait-card ExecStartPre so the condition skip happens before any 30 s poll.
    """
    body = UNIT_PATH.read_text()
    conditions = _exec_condition_lines(body)
    uac2_gate = [
        c for c in conditions
        if "/sys/kernel/config/usb_gadget/jts-usb-audio/functions/uac2.usb0" in c
    ]
    assert uac2_gate, (
        "jasper-usbsink.service must ExecCondition on the composed uac2.usb0 "
        "function dir so a start without a composed audio function parks "
        "cleanly instead of looping through wait-card + Restart=on-failure."
    )
    assert uac2_gate[0].split()[0] == "/bin/test", (
        f"expected the uac2 gate to use /bin/test -d; got {uac2_gate[0]!r}"
    )
    # The existing local-source ExecCondition must remain (belt: both gates).
    assert any("jasper-local-source-allowed" in c for c in conditions), (
        "the local-source ExecCondition must remain alongside the uac2 gate."
    )
    # Ordering: the condition DIRECTIVE (which skips) must precede the 30 s
    # wait-card ExecStartPre directive so the skip short-circuits before any
    # poll. Match on the directive lines, not the surrounding comments.
    def _directive_index(prefix: str, needle: str) -> int:
        for idx, ln in enumerate(body.splitlines()):
            s = ln.strip()
            if s.startswith(prefix) and needle in s:
                return idx
        raise AssertionError(f"no {prefix!r} directive containing {needle!r}")

    assert _directive_index("ExecCondition=", "functions/uac2.usb0") < (
        _directive_index("ExecStartPre=", "jasper-usbsink-wait-card")
    ), "the uac2 ExecCondition must be listed before the wait-card ExecStartPre"


def test_rides_zram_shielded_audio_slice():
    # The swap shield is the load-bearing fix for the bursty USB-drop tail.
    body = UNIT_PATH.read_text()
    assert _value_for(body, "Slice") == "jts-audio.slice", (
        "jasper-usbsink.service must set Slice=jts-audio.slice (MemorySwapMax=0 "
        "swap shield) — off it, zram jitter makes the RT callback miss deadlines "
        "and the queue overflows. See the 2026-06-28 USB-drop diagnosis."
    )


def test_oom_band_below_the_output_chain():
    # A killed source restarts and only USB stops; a killed output (outputd -950
    # / camilla -900 / fanin -800) stops all audio — so usbsink sits ABOVE them
    # (less negative = less protected), but still protected vs the default 0.
    body = UNIT_PATH.read_text()
    raw = _value_for(body, "OOMScoreAdjust")
    assert raw is not None, "jasper-usbsink.service must set OOMScoreAdjust"
    val = int(raw)
    assert -800 < val < 0, (
        f"usbsink OOMScoreAdjust={val} must be negative (protected) but above the "
        f"output chain's -800 (fanin) — a music source is less critical than output."
    )


def test_no_unit_level_rt_scheduling():
    # Unit-level CPUSchedulingPolicy=fifo is what SIGKILL-crash-looped the AEC
    # bridge on 2026-06-27; RT priority, if ever needed, is elected in-thread
    # after start(). Guard against it regressing into this unit.
    body = UNIT_PATH.read_text()
    assert _value_for(body, "CPUSchedulingPolicy") is None, (
        "Do NOT set CPUSchedulingPolicy at the unit level (AEC-bridge crash-loop "
        "2026-06-27). Elect RT in-thread after start() if ever required."
    )


def test_rust_audio_bridge_is_the_service_execstart():
    body = UNIT_PATH.read_text()
    assert _value_for(body, "ExecStart") == "/opt/jasper/bin/jasper-usbsink-audio"
    assert "jasper-usbsink-volume.service" in (_value_for(body, "Wants") or "")


def test_no_python_usb_bridge_console_scripts():
    # The legacy Python/PortAudio bridge (daemon.py / audio_bridge.py /
    # usbsink_main.py) and its lab entrypoint were deleted; the Rust
    # jasper-usbsink-audio binary is the only USB-audio data-plane. Only the
    # volume poller keeps a Python console script.
    data = tomllib.loads(PYPROJECT_PATH.read_text())
    scripts = data["project"]["scripts"]

    assert "jasper-usbsink-python-lab" not in scripts
    assert "jasper-usbsink" not in scripts
    assert scripts.get("jasper-usbsink-volume") == (
        "jasper.cli.usbsink_volume_main:main"
    )


def test_packaged_defaults_do_not_override_operator_or_generated_env():
    body = UNIT_PATH.read_text()

    default_idx = _line_index(body, "JASPER_USBSINK_CAPTURE_DEVICE=hw:UAC2Gadget")
    base_env_idx = _line_index(body, "EnvironmentFile=-/etc/jasper/jasper.env")
    generated_env_idx = _line_index(
        body,
        "EnvironmentFile=-/var/lib/jasper/usbsink.env",
    )

    assert default_idx < base_env_idx < generated_env_idx


# ----------------------------------------------------------------------
# Standby-only daemon: NO ExecStopPost pitch-neutralize belt.
# The old aloop solo path had the usbsink daemon drive the gadget's
# "Capture Pitch 1000000" ctl, with a belt-and-braces ExecStopPost= reset for
# the SIGKILL/OOM case. That path was deleted (2026-07-10): the daemon is
# standby-only and never opens the gadget or touches the pitch ctl — jasper-fanin
# owns both in combo mode. So the unit must carry NO Capture-Pitch ExecStopPost
# line; one would stomp fan-in's live pitch command on every stop.
# ----------------------------------------------------------------------


def _exec_stop_post_lines(unit_text: str) -> list[str]:
    """Every ``ExecStopPost=`` line's value (multiple are valid systemd
    syntax — unlike Environment=/ExecStart=, they do NOT "last wins"; all
    of them run in sequence on stop). Ignores comments."""
    values: list[str] = []
    for ln in unit_text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        if s.startswith("ExecStopPost="):
            values.append(s[len("ExecStopPost="):])
    return values


def test_no_execstoppost_pitch_belt_on_standby_only_daemon():
    # The standby-only daemon must NOT carry a Capture-Pitch ExecStopPost belt:
    # it never owns the gadget pitch ctl (fan-in does), so neutralizing on stop
    # would reset fan-in's live L0 command behind its back on every restart.
    body = UNIT_PATH.read_text()
    pitch_lines = [v for v in _exec_stop_post_lines(body) if "Capture Pitch" in v]
    assert not pitch_lines, (
        "jasper-usbsink.service must NOT carry an ExecStopPost= pitch-neutralize "
        "line — the daemon is standby-only and never owns the gadget pitch ctl; "
        f"fan-in does. Got: {pitch_lines!r}"
    )




def test_start_limit_widened_and_never_reboots():
    """Hardening rider (defect 2026-07-10): Restart=on-failure/RestartSec=2s with
    NO StartLimit* would let a fast ENODEV unplug/replug flap exhaust systemd's
    default 5-in-10s window and park the unit `failed` forever. The tolerance is
    RAISED VIA BURST COUNT, not a stricter interval: StartLimitIntervalSec=300 +
    StartLimitBurst=20 (a stricter interval alone, e.g. keeping burst at systemd's
    default-adjacent 5, would still park the unit on a slow flap — a cable jiggled
    every 30-60 s — well inside a 300 s window). With RestartSec=2s, burst=20 rides
    through that slow-flap case entirely, while a persistently-crashing bridge still
    burns ~20 bounded attempts (~40 s) before parking `failed` rather than
    restart-looping forever — the parked state is what jasper-doctor's
    check_usb_combo_fallback surfaces. And — unlike the core graph (jasper-fanin) —
    NEVER StartLimitAction=reboot: a repeatedly-failing USB bridge must not reboot
    the whole speaker."""
    body = UNIT_PATH.read_text()
    assert _value_for(body, "StartLimitIntervalSec") == "300", (
        "jasper-usbsink.service must set StartLimitIntervalSec=300 as the window "
        "over which StartLimitBurst is counted."
    )
    assert _value_for(body, "StartLimitBurst") == "20", (
        "jasper-usbsink.service must set StartLimitBurst=20 — tolerance is raised "
        "via burst count (rides out a slow replug flap) rather than a stricter "
        "interval, which would still park the unit on a slow flap."
    )
    # The load-bearing safety invariant: a bridge failure must never reboot the box.
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        assert not s.startswith("StartLimitAction="), (
            "jasper-usbsink.service must NOT set StartLimitAction (esp. =reboot): a "
            f"failing USB bridge must never reboot the speaker. Got: {s!r}"
        )
    # Restart policy that the StartLimit governs must still be present.
    assert _value_for(body, "Restart") == "on-failure"
