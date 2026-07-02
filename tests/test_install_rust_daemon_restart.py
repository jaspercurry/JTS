# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard: every built Rust daemon's owning service is restarted on deploy.

`deploy/lib/install/rust-daemons.sh` builds and `install`s
/opt/jasper/bin/{jasper-fanin,jasper-outputd,jasper-usbsink-audio}, but a
freshly-installed binary only goes live when its owning service restarts:
`install` replaces the file on disk while the running process keeps
executing the OLD inode. On 2026-07-02 that gap bit on hardware — a deploy
installed a new jasper-usbsink-audio image while jasper-usbsink kept serving
the old build, and its new HTTP endpoints 404'd until a manual restart.

There are two mechanisms that make a new Rust binary live:

  1. the core-graph restart sequence in the systemd step
     (JASPER_CORE_GRAPH_PARK_UNITS parks + require_outputd_ready restarts
     jasper-outputd; JASPER_CORE_GRAPH_RESTART_TARGETS restarts jasper-fanin
     + jasper-camilla) — unconditional, every deploy;
  2. restart_services_for_changed_rust_daemons — a content-gated
     try-restart for the Rust daemons OUTSIDE that set (today only
     jasper-usbsink-audio -> jasper-usbsink.service).

Maintenance contract, enforced below: adding a
`build_install_rust_daemon "<name>"` call without covering its owning
service by one of those two mechanisms fails loudly here. Mirrors the
tone/structure of tests/test_install_plan_covers_main.py and
tests/test_core_graph_park_units_contract.py.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_RUST_DAEMONS = ROOT / "deploy" / "lib" / "install" / "rust-daemons.sh"
_SYSTEMD_UNITS = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
_PARK_FRAGMENT = ROOT / "deploy" / "lib" / "jasper-core-graph-park-units.sh"
_SYSTEMD_UNIT_DIR = ROOT / "deploy" / "systemd"
_DEPLOY_TO_PI = ROOT / "scripts" / "deploy-to-pi.sh"


# --------------------------------------------------------------------------
# Source parsing helpers
# --------------------------------------------------------------------------
def _built_daemon_names() -> set[str]:
    """Rust daemon names built via a `build_install_rust_daemon "<name>" "..."`
    call. The trailing quoted flag argument distinguishes the CALL sites from
    the function DEFINITION line (which has no argument after the name)."""
    text = _RUST_DAEMONS.read_text(encoding="utf-8")
    return set(
        re.findall(r'^\s*build_install_rust_daemon "([a-z0-9-]+)" "', text, re.MULTILINE)
    )


def _owning_service(name: str) -> str:
    """The single .service unit whose ExecStart runs /opt/jasper/bin/<name>.

    Fails naming the daemon if zero or more than one unit owns it — a new
    Rust daemon MUST have exactly one owning unit for the coverage guard to
    reason about it."""
    pattern = re.compile(rf"^ExecStart=/opt/jasper/bin/{re.escape(name)}(\s|$)", re.MULTILINE)
    owners = [
        unit.name
        for unit in sorted(_SYSTEMD_UNIT_DIR.glob("*.service"))
        if pattern.search(unit.read_text(encoding="utf-8"))
    ]
    assert len(owners) == 1, (
        f"expected exactly one systemd unit with "
        f"ExecStart=/opt/jasper/bin/{name}, found {owners}"
    )
    return owners[0]


def _extract_function_body(text: str, func: str) -> str:
    """Return the body of a bash function `func() {` up to the next line-start
    `}`. Same extraction style as _main_body in test_install_plan_covers_main."""
    match = re.search(rf"{re.escape(func)}\(\) \{{\n(.*?)\n\}}", text, re.DOTALL)
    assert match is not None, f"could not locate {func}() in the source"
    return match.group(1)


def _park_units() -> list[str]:
    """Source the park fragment under bash; return JASPER_CORE_GRAPH_PARK_UNITS.

    Mirrors _source_fragment_units in test_core_graph_park_units_contract."""
    script = (
        f'source "{_PARK_FRAGMENT}"\n'
        'printf "%s\\n" "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def _restart_targets() -> list[str]:
    """Source systemd-units.sh under bash (with REPO_DIR set so its top-level
    `source .../jasper-core-graph-park-units.sh` resolves) and return
    JASPER_CORE_GRAPH_RESTART_TARGETS."""
    env = os.environ.copy()
    env["REPO_DIR"] = str(ROOT)
    script = (
        f'source "{_SYSTEMD_UNITS}"\n'
        'printf "%s\\n" "${JASPER_CORE_GRAPH_RESTART_TARGETS[@]}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if line]


def _conditional_restart_services() -> set[str]:
    """Services (try-)restarted inside restart_services_for_changed_rust_daemons."""
    body = _extract_function_body(
        _RUST_DAEMONS.read_text(encoding="utf-8"),
        "restart_services_for_changed_rust_daemons",
    )
    return set(re.findall(r"(?:try-)?restart ([a-z0-9.-]+\.service)", body))


# --------------------------------------------------------------------------
# Static / drift-guard tests
# --------------------------------------------------------------------------
def test_built_daemon_names_are_parsed():
    """Meta-check: the CALL-site parser sees the three shipped daemons and
    skips the function definition line."""
    names = _built_daemon_names()
    assert {"jasper-fanin", "jasper-outputd", "jasper-usbsink-audio"} <= names, names
    assert len(names) >= 3, names


def test_every_built_daemon_has_exactly_one_owning_unit():
    for name in sorted(_built_daemon_names()):
        # _owning_service asserts exactly-one internally.
        _owning_service(name)


def test_every_built_daemon_service_is_restart_covered():
    """The core assertion: for every built Rust daemon, its owning unit is
    covered by ONE of the deploy restart mechanisms."""
    park = set(_park_units())
    targets = set(_restart_targets())
    conditional = _conditional_restart_services()
    covered = park | targets | conditional

    uncovered = {}
    for name in sorted(_built_daemon_names()):
        unit = _owning_service(name)
        if unit not in covered:
            uncovered[name] = unit
    assert not uncovered, (
        "built Rust daemons whose owning service is not restarted on deploy: "
        f"{uncovered}. Cover a new Rust daemon in ONE of three ways: add its "
        "unit to JASPER_CORE_GRAPH_PARK_UNITS (the core-graph park list, "
        "restarted via require_outputd_ready), to JASPER_CORE_GRAPH_RESTART_"
        "TARGETS (the always-restart core-graph set), or add a content-gated "
        "try-restart in restart_services_for_changed_rust_daemons."
    )


def test_conditional_and_core_graph_restart_sets_are_disjoint():
    """A unit restarted by both the core-graph sequence AND the conditional
    helper would be double-bounced every deploy — interrupting audio twice."""
    park = set(_park_units())
    targets = set(_restart_targets())
    conditional = _conditional_restart_services()
    overlap = conditional & (park | targets)
    assert not overlap, (
        f"units in both the conditional restart helper and the core-graph "
        f"restart sequence (double-bounce per deploy): {sorted(overlap)}"
    )


def test_both_install_paths_call_the_restart_helper():
    """Full-speaker (install_systemd_units) and streambox
    (start_streambox_runtime_units) both invoke the conditional helper."""
    text = _SYSTEMD_UNITS.read_text(encoding="utf-8")
    for func in ("install_systemd_units", "start_streambox_runtime_units"):
        body = _extract_function_body(text, func)
        assert "restart_services_for_changed_rust_daemons" in body, (
            f"{func}() does not call restart_services_for_changed_rust_daemons; "
            "a changed Rust binary outside the core-graph set would not go live"
        )


def test_build_install_records_binary_change():
    """build_install_rust_daemon sha256-compares before/after install and
    appends changed daemons to JASPER_RUST_CHANGED_BINS."""
    body = _extract_function_body(
        _RUST_DAEMONS.read_text(encoding="utf-8"),
        "build_install_rust_daemon",
    )
    # Hashed both before (pre_sha) and after (new_sha) the install.
    assert body.count('sha256sum "${bin_dest}"') >= 2, body
    # Appends the changed daemon name to the shared changed-set string.
    assert 'JASPER_RUST_CHANGED_BINS="${JASPER_RUST_CHANGED_BINS} ${name}"' in body, body


def test_skip_restart_forwarded_by_deploy_to_pi():
    """deploy-to-pi.sh forwards SKIP_RESTART into install.sh's env passthrough
    loop so restart_services_for_changed_rust_daemons can honor it."""
    text = _DEPLOY_TO_PI.read_text(encoding="utf-8")
    loop = re.search(r"for key in \\\n(.*?)\ndo\n", text, re.DOTALL)
    assert loop is not None, "could not locate the install_env passthrough for-loop"
    loop_body = loop.group(1)
    assert "JASPER_RUST_LOW_MEMORY_BUILD" in loop_body, (
        "parsed the wrong for-loop (expected the install_env passthrough list)"
    )
    assert "SKIP_RESTART" in loop_body, (
        "SKIP_RESTART is not forwarded into install.sh's env passthrough loop"
    )


# --------------------------------------------------------------------------
# Functional tests — bash subprocess with a PATH-shim systemctl
# --------------------------------------------------------------------------
def _run_restart_helper(
    tmp_path, changed_bins: str, extra_env: dict | None = None
) -> tuple[str, list[str]]:
    """Source rust-daemons.sh, set the changed-set, run the restart helper.

    Returns (stdout, systemctl-call-lines). rust-daemons.sh has no top-level
    `source` lines, so it sources standalone."""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    shim = shim_dir / "systemctl"
    shim.write_text(
        f'#!/usr/bin/env bash\necho "$@" >> {shlex.quote(str(log))}\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    env.pop("SKIP_RESTART", None)
    if extra_env:
        env.update(extra_env)
    script = (
        f'source {shlex.quote(str(_RUST_DAEMONS))}\n'
        f'JASPER_RUST_CHANGED_BINS={shlex.quote(changed_bins)}\n'
        'restart_services_for_changed_rust_daemons\n'
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result.stdout, calls


def test_helper_restarts_usbsink_when_changed(tmp_path):
    stdout, calls = _run_restart_helper(tmp_path, " jasper-usbsink-audio")
    assert calls == ["try-restart jasper-usbsink.service"], calls
    assert "try-restarting jasper-usbsink.service" in stdout


def test_helper_no_restart_when_nothing_changed(tmp_path):
    stdout, calls = _run_restart_helper(tmp_path, "")
    assert calls == [], calls


def test_helper_no_restart_for_core_graph_daemons(tmp_path):
    """jasper-fanin / jasper-outputd are covered by the core-graph sequence —
    the conditional helper must NOT touch them (pins the division of labor)."""
    stdout, calls = _run_restart_helper(tmp_path, " jasper-fanin jasper-outputd")
    assert calls == [], calls


def test_helper_skips_when_skip_restart(tmp_path):
    stdout, calls = _run_restart_helper(
        tmp_path, " jasper-usbsink-audio", extra_env={"SKIP_RESTART": "1"}
    )
    assert calls == [], calls
    assert "SKIP_RESTART=1" in stdout


def test_membership_test_is_token_exact():
    """rust_daemon_binary_changed matches whole tokens: a set containing
    'jasper-usbsink-audio' must NOT report 'jasper-usbsink' as changed."""
    script = (
        f'source {shlex.quote(str(_RUST_DAEMONS))}\n'
        'JASPER_RUST_CHANGED_BINS="jasper-usbsink-audio"\n'
        'if rust_daemon_binary_changed jasper-usbsink; then '
        'echo "usbsink=changed"; else echo "usbsink=unchanged"; fi\n'
        'if rust_daemon_binary_changed jasper-usbsink-audio; then '
        'echo "usbsink-audio=changed"; else echo "usbsink-audio=unchanged"; fi\n'
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    assert "usbsink=unchanged" in lines, result.stdout
    assert "usbsink-audio=changed" in lines, result.stdout
