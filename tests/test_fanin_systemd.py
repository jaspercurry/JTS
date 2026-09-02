# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lock down the jasper-fanin.service systemd unit shape.

The unit's resilience-contract fields are load-bearing — they're
the JTS-standard Tier 1+2 / Stage 1+2 protections.

A future config edit that drops `WatchdogSec=`, lowers
`OOMScoreAdjust=` priority, or removes the `Slice=` assignment
would silently regress these protections. These tests catch that.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.systemd_unit_helpers import (
    assignments_for as _assignments_for,
    value_for as _value_for,
    values_for as _values_for,
)


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-fanin.service"


def _read_unit() -> str:
    return UNIT_PATH.read_text()


def test_unit_file_exists():
    assert UNIT_PATH.exists(), (
        f"jasper-fanin.service missing at {UNIT_PATH}. "
        f"install.sh's install_systemd_units block needs this file."
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # With Type=simple systemd ignores READY=1/WATCHDOG=1 — silently
        # disabling Tier 2.
        pytest.param("Type", "notify", id="type_notify_for_sd_notify_contract"),
        # Matches the project-wide Tier 2 cadence (camilla, aec-bridge, voice,
        # control all use this value).
        pytest.param("WatchdogSec", "30s", id="watchdog_sec_set"),
        # A daemon with blocked I/O sits on SIGTERM for systemd's default 90s
        # and corrupts kernel ALSA state before SIGKILL fires. 5s escalates fast.
        pytest.param("TimeoutStopSec", "5s", id="timeout_stop_sec_short"),
        # Covers exit-nonzero + signal + watchdog timeout without restarting on
        # a clean signal-shutdown (Restart=always would).
        pytest.param("Restart", "on-failure", id="restart_on_failure"),
        # T5.1 escalation on repeated wedges. Not reboot-force — clean reboot
        # lets zram dirty pages sync on a 1 GB Pi.
        pytest.param(
            "StartLimitAction", "reboot", id="start_limit_action_reboot"
        ),
        # -800 sits between Camilla (-900, silence-critical) and the AEC
        # bridge (-700, capture-critical) on the OOM kill ladder.
        pytest.param(
            "OOMScoreAdjust",
            "-800",
            id="oom_score_adj_between_camilla_and_aec_bridge",
        ),
        # Stage 2 audio-protection cgroup (MemorySwapMax=0) shields the work
        # loop's pages from zram decompression jitter on a 1 GB Pi 5.
        pytest.param("Slice", "jts-audio.slice", id="slice_assignment"),
    ],
)
def test_unit_field_value(key, expected):
    unit = _read_unit()
    val = _value_for(unit, key)
    assert val == expected, (
        f"jasper-fanin.service must declare {key}={expected}. Got {val!r}"
    )


def test_ring_config_class_failures_park_instead_of_rebooting():
    """A config-class ring failure must PARK the unit, not climb the restart
    burst into `StartLimitAction=reboot` above.

    fan-in's Ring A geometry declaration has ways to be wrong that no RESTART
    can fix, in two shapes:

    1. A rejected `JASPER_FANIN_RING_WIRE_FORMAT` / `_RING_SLOTS` /
       slot-shearing period. Re-read from the env file on every start, so it is
       identical across restarts AND across reboots.
    2. A ring file on disk whose header geometry differs from the one the
       daemon builds (a stale ring surviving a deploy, or a conf.d and daemon
       that disagree about the wire). `/dev/shm` is tmpfs, so this one DOES
       clear on a reboot — but not on a restart, which is the loop that
       matters here.

    Before `RestartPreventExitStatus=78` both exited non-zero like any other
    fault, so five starts in five minutes escalated to
    `StartLimitAction=reboot`. jasper-outputd took the same treatment for the
    same reason (jts3, 2026-06-11: a guard-rejected env combination
    crash-looped it into three Pi reboots).

    Rust unit tests cover the mapping from concrete error classes to exit 78.
    """
    unit = _read_unit()
    assert "78" in _values_for(unit, "RestartPreventExitStatus"), (
        "jasper-fanin.service must declare RestartPreventExitStatus=78 so a "
        "config-class ring failure parks instead of escalating to "
        "StartLimitAction=reboot"
    )
    # Mirrors jasper-outputd: no SuccessExitStatus for 78, so the park is
    # visible as `failed` on /state + doctor rather than a quiet inactive unit.
    assert "78" not in _values_for(unit, "SuccessExitStatus"), (
        "an exit-78 park must stay FAILED (visible on /state + doctor), "
        "matching jasper-outputd's precedent"
    )

def test_sched_fifo_and_mlockall_settings():
    """Real-time scheduling: SCHED_FIFO at priority 30 +
    LimitMEMLOCK=infinity (the latter lets in-process mlockall
    succeed even when systemd's per-unit default RLIMIT_MEMLOCK
    is too small for the daemon's stacks + audio buffers).
    OSPERT 2024 measured Pi 5 stock-kernel worst-case scheduling
    latency at 36.8ms under stress; SCHED_FIFO + mlockall is the
    floor protection before considering PREEMPT_RT."""
    unit = _read_unit()
    assert _value_for(unit, "CPUSchedulingPolicy") == "fifo"
    assert _value_for(unit, "CPUSchedulingPriority") == "30"
    assert _value_for(unit, "LimitMEMLOCK") == "infinity"


def test_rt_runtime_bounded_by_limit_rttime():
    """Every FIFO unit caps RT-thread runaway with LimitRTTIME (audio-latency
    foundation G4). Without it, a spinning FIFO thread can starve PID 1 on the
    1 GB Pi and trip the hardware watchdog into a full reboot; the 200 ms
    SIGXCPU bound reduces that whole-system wedge to one crashed daemon. This is
    mandatory wherever CPUSchedulingPolicy=fifo is set."""
    unit = _read_unit()
    assert _value_for(unit, "CPUSchedulingPolicy") == "fifo"
    assert _value_for(unit, "LimitRTTIME") == "200000"


def test_runtime_directory():
    """`RuntimeDirectory=jasper-fanin` makes systemd create
    /run/jasper-fanin/ on start and remove it on stop. The UDS
    socket lives in that dir; without this, the socket would leak
    across daemon-restart events and the bind would race against
    stale-socket cleanup."""
    unit = _read_unit()
    values = _values_for(unit, "RuntimeDirectory")
    assert values == ("jasper-fanin",), (
        f"jasper-fanin.service must declare RuntimeDirectory=jasper-fanin "
        f"so /run/jasper-fanin/ is auto-managed. Got {values!r}"
    )


def test_environment_files():
    """Config-file chain matches the voice / AEC daemons:
    /etc/jasper/jasper.env (system-wide, required) then an
    optional wizard-owned file (`-` prefix = optional). Same
    pattern lets operators override defaults via either."""
    unit = _read_unit()
    env_files = [
        line.strip().split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("EnvironmentFile=")
    ]
    assert "/etc/jasper/jasper.env" in env_files, (
        "jasper-fanin.service must source /etc/jasper/jasper.env"
    )
    # Optional wizard file with the `-` prefix (= no error if missing).
    assert any(
        ef.startswith("-") and "fanin.env" in ef for ef in env_files
    ), "jasper-fanin.service must reference an optional fanin.env wizard file"


def test_exec_start_points_at_installed_binary():
    """`ExecStart=/opt/jasper/bin/jasper-fanin` matches where
    install.sh's build_install_jasper_fanin installs the release
    binary. A divergence between unit and install.sh would let
    systemd start a stale binary or fail with ENOENT."""
    unit = _read_unit()
    commands = _assignments_for(unit, "ExecStart")
    assert commands == ("/opt/jasper/bin/jasper-fanin",), (
        f"jasper-fanin.service ExecStart must be "
        f"/opt/jasper/bin/jasper-fanin (matches install.sh's "
        f"build_install_jasper_fanin destination). Got {commands!r}"
    )


def test_combo_gated_pitch_neutralize_exec_stop_post():
    """The combo-mode host-clock belt-and-braces (C6): on SIGKILL / OOM /
    watchdog abort — which skip the daemon's in-process pitch neutralize — the
    unit must reset the gadget's "Capture Pitch 1000000" ctl to neutral, but
    ONLY when THIS daemon is the configured clock owner.

    The gate + card-derive + neutralize now live in the shipped helper
    jasper-fanin-pitch-neutralize (moved OUT of an inline `sh -c` in defect E:
    the inline `${card%%,*}` collided with systemd's `%%` specifier escape). The
    unit must invoke that helper best-effort. The owner-gate semantics (BOTH
    flags, case-insensitive, device-derived card, neutral 1000000) are pinned by
    the helper's own tests in tests/test_fanin_pitch_neutralize.py.
    """
    unit = _read_unit()
    commands = _assignments_for(unit, "ExecStopPost")
    assert len(commands) == 1, (
        "jasper-fanin.service must carry an ExecStopPost pitch-neutralize belt "
        "for the combo-mode host-clock (C6)."
    )
    val = commands[0]
    # Best-effort (leading `-`): a missing card / combo-off must not fail stop.
    assert val.startswith("-"), (
        "the ExecStopPost must be best-effort (leading `-`) so a missing gadget "
        f"card can't fail the unit stop. Got {val!r}"
    )
    # It invokes the shipped helper at its installed path — NOT an inline sh -c
    # (defect E: the inline form's `${card%%,*}` collided with systemd's `%%`).
    assert val.lstrip("-").strip() == "/usr/local/sbin/jasper-fanin-pitch-neutralize", (
        "the ExecStopPost must invoke the shipped helper "
        "/usr/local/sbin/jasper-fanin-pitch-neutralize (installed by install.sh), "
        f"not an inline sh -c with %-expansion. Got {val!r}"
    )


def test_exec_stop_post_has_no_bare_percent_specifier_collision():
    """Defect E lint (fan-in unit): no ExecStopPost / ExecStartPost line may
    carry a bare shell `%%`/`${var%...}` expansion, which systemd mis-reads as a
    specifier escape ("Invalid environment variable name evaluates to an empty
    string"). The neutralize logic that used `${card%%,*}` moved into a shipped
    helper for exactly this reason; this pins that it does not creep back."""
    unit = _read_unit()
    for line in unit.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() not in ("ExecStopPost", "ExecStartPost", "ExecStartPre"):
            continue
        # A literal `%%` (or a lone `%` not part of a systemd specifier) in an
        # Exec* value is the collision class. systemd's own specifiers are
        # `%N`/`%%`; a shell parameter expansion like `${card%%,*}` reaches
        # systemd as a bare `%%` and is what broke. The fix ships helpers, so no
        # Exec* line should contain `%` at all.
        assert "%" not in value, (
            "an Exec* line carries a bare '%' — systemd treats it as a specifier "
            "escape (defect E). Move the shell logic into a shipped deploy/bin/ "
            f"helper instead. Offending line: {stripped!r}"
        )


_EXEC_KEYS = (
    "ExecStart",
    "ExecStartPre",
    "ExecStartPost",
    "ExecStop",
    "ExecStopPost",
    "ExecReload",
    "ExecCondition",
)


def _shipped_unit_files() -> list[Path]:
    """Every shipped systemd unit + drop-in the installer lays down.

    Both the top-level ``*.service`` files (under deploy/ and deploy/systemd/)
    and ``*.service.d/*.conf`` drop-ins carry Exec* lines, so both are in scope
    for the %-specifier collision lint.
    """
    systemd = REPO / "deploy"
    units = sorted(systemd.rglob("*.service"))
    dropins = sorted(systemd.rglob("*.service.d/*.conf"))
    return units + dropins


def test_no_shipped_unit_has_shell_param_expansion_in_exec():
    """Defect E lint (ALL shipped units + drop-ins): no Exec* line may carry a
    shell parameter expansion containing '%' — e.g. ``${card%%,*}`` — which
    systemd mis-reads as a specifier escape ("Invalid environment variable name
    evaluates to an empty string"). This is the exact class that broke the inline
    combo-mode neutralize before it moved to jasper-fanin-pitch-neutralize; the
    per-unit test above pins the fan-in unit, and this one is the repo-wide
    backstop the brief asked for ("no bare '%' expansion remains in shipped unit
    files", plural) so a NEW unit can't reintroduce it.

    Precise on purpose: it flags a '%' INSIDE a ``${...}`` expansion (always the
    bug), not a bare legitimate systemd specifier like ``%i`` / ``%N`` (which a
    future template unit may validly use). Today no shipped Exec* line carries any
    '%' at all — this keeps that clean while allowing genuine specifiers later.
    """
    # A `%` appearing inside a ${...} shell expansion on an Exec* line — the
    # `${var%pat}` / `${var%%pat}` collision class, never a systemd specifier.
    shell_expansion_with_percent = re.compile(r"\$\{[^}]*%[^}]*\}")
    offenders: list[str] = []
    files = _shipped_unit_files()
    assert files, "expected to find shipped unit files under deploy/"
    for path in files:
        for raw in path.read_text().splitlines():
            stripped = raw.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() not in _EXEC_KEYS:
                continue
            if shell_expansion_with_percent.search(value):
                offenders.append(f"{path.relative_to(REPO)}: {stripped!r}")
    assert not offenders, (
        "shipped unit Exec* line(s) carry a shell '%' parameter expansion that "
        "systemd mis-reads as a specifier escape (defect E). Move the shell logic "
        "into a deploy/bin/ helper (see jasper-fanin-pitch-neutralize). Offenders:\n"
        + "\n".join(offenders)
    )


def test_input_buffer_frames_sized_for_wifi_burst_absorption():
    """Per-input ALSA ring buffer must be >= 4096 frames so it
    absorbs the worst-case 802.11 A-MPDU inter-burst gap (~40 ms
    observed; we want comfortable headroom, hence 4096 = ~85 ms).

    Below 4096, AirPlay sessions produce ~1 input EPIPE-overrun per
    30-60 s on real hardware, each injecting one period of silence
    into the mixer output.

    The dmix layer (PR #214, which fanin replaces) had buffer_size
    4096; fanin must match that to preserve the burst-absorption
    behaviour the dmix accidentally provided.
    """
    unit = _read_unit()
    # Look in the [Service] section for the Environment= directive
    # — it's the production default, even though operators can
    # override via /var/lib/jasper/fanin.env.
    match = re.search(
        r'^\s*Environment\s*=\s*"?JASPER_FANIN_INPUT_BUFFER_FRAMES=(\d+)"?',
        unit,
        re.MULTILINE,
    )
    assert match is not None, (
        "jasper-fanin.service must set Environment=\"JASPER_FANIN_INPUT_BUFFER_FRAMES=...\" "
        "(production default for per-input ALSA buffer sizing)."
    )
    val = int(match.group(1))
    assert val >= 4096, (
        f"JASPER_FANIN_INPUT_BUFFER_FRAMES={val} is below 4096 (~85 ms). "
        f"Below 4096, WiFi A-MPDU burst delivery overruns the input "
        f"ring at ~2 xruns/min on AirPlay."
    )


def test_hardening_directives_present():
    """Defense-in-depth filesystem hardening — matches the
    conventions of other jasper-* units. None of these are
    individually load-bearing, but together they constrain the
    blast radius of any compromise of the daemon."""
    unit = _read_unit()
    assert _value_for(unit, "NoNewPrivileges") == "true"
    assert _value_for(unit, "ProtectSystem") == "full"
    assert _value_for(unit, "ProtectHome") == "read-only"
    assert _value_for(unit, "PrivateTmp") == "true"


def test_read_write_paths_include_jasper_state_dirs():
    """ReadWritePaths grants write access to the paths the daemon
    needs even with ProtectSystem=full. /var/lib/jasper for the
    xrun_log ring, /run/jasper-fanin for the UDS socket."""
    unit = _read_unit()
    rwp_lines = [
        line.strip().split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("ReadWritePaths=")
    ]
    assert rwp_lines, "jasper-fanin.service must declare ReadWritePaths"
    rwp_combined = " ".join(rwp_lines)
    assert "/var/lib/jasper" in rwp_combined, (
        "ReadWritePaths must include /var/lib/jasper (for xrun_history.jsonl writes)"
    )
    assert "/run/jasper-fanin" in rwp_combined, (
        "ReadWritePaths must include /run/jasper-fanin "
        "(for the UDS socket; redundant with RuntimeDirectory "
        "but explicit for ProtectSystem=full)"
    )


def test_install_target_is_multi_user():
    """`WantedBy=multi-user.target` matches the conventions of
    other jasper-* daemons."""
    unit = _read_unit()
    assert _values_for(unit, "WantedBy") == ("multi-user.target",)


def test_fanin_starts_before_hot_path_consumers():
    """Fan-in must be initialized before Camilla/renderer consumers try
    to open the summed-reference graph."""
    unit = _read_unit()
    before = _values_for(unit, "Before")
    assert before
    for dep in (
        "jasper-camilla.service",
        "shairport-sync.service",
        "librespot.service",
        "bluealsa-aplay.service",
        "jasper-usbsink.service",
        "jasper-aec-bridge.service",
    ):
        assert dep in before
