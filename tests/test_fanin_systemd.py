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
        # TimeoutStopSec=5s, Restart=on-failure and StartLimitAction=reboot are
        # pinned, with the rest of the restart ladder, by
        # tests/test_systemd_hardening.py's RESTART_POLICY table (R22, #4416).
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


# The config-class ring failure -> park (not StartLimitAction=reboot)
# contract (jasper-outputd took the same treatment after the jts3 2026-06-11
# reboot-loop incident) is pinned, with the rest of the restart ladder, by
# tests/test_systemd_hardening.py's RESTART_POLICY table (R22, #4416). Rust
# unit tests cover the mapping from concrete error classes to exit 78; the
# unit file's own comment above RestartPreventExitStatus= carries the why.


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


def test_no_inline_environment_overrides_the_operator_env_file():
    """systemd applies env in file order, so an `Environment=` literal here
    would beat /etc/jasper/jasper.env, the documented operator seam. The
    daemon's own compiled defaults are the bottom layer instead."""
    assert _values_for(_read_unit(), "Environment") == ()


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
    assistant volume reference, /run/jasper-fanin for the UDS socket."""
    unit = _read_unit()
    rwp_lines = [
        line.strip().split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("ReadWritePaths=")
    ]
    assert rwp_lines, "jasper-fanin.service must declare ReadWritePaths"
    rwp_combined = " ".join(rwp_lines)
    assert "/var/lib/jasper" in rwp_combined, (
        "ReadWritePaths must include /var/lib/jasper "
        "(for assistant_volume_reference.json writes)"
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
