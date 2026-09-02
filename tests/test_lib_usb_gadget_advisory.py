# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Test scripts/_lib.sh's USB-gadget-network deploy-advisory helpers.

Issue #2340 (2026-08-11 U2 deploy): PI_HOST resolved to 10.12.194.1, the
USB gadget's management subnet (usb0).
install.sh rebuilds the composite USB gadget mid-install, which tore down
that link out from under the deploy's own ssh session: the transport died
with no FIN, the local ssh hung, and the transcript froze around the
install's gadget step (then `event=install.usb_gadget_baseline`, now
`event=usb_gadget.converge`) — while the install itself had already
succeeded on the Pi. scripts/deploy-to-pi.sh now warns (never blocks)
before rsync when PI_HOST is about to resolve into that subnet, and bounds
the ssh transport with keepalives so a real sever surfaces as a ~60s error
instead of an unbounded hang.

These tests pin the pure classification helpers in _lib.sh:

* ``usb_gadget_management_cidrs`` — asks ``jasper.usb_network``, the address
  plan owner, for both the derived allocation range and legacy migration
  subnet. A varied fake module proves the shell helper has no copied range;
* ``ipv4_in_cidr`` — pure IPv4 subnet-membership arithmetic, including its
  documented "malformed input reads as false, never an error" contract.

The final section drives the real ``warn_if_pi_host_on_gadget_network``
body out of deploy-to-pi.sh (extracted and sourced, mirroring
test_lib_deploy_direction.py's preflight harness) to pin the wiring: the
advisory lands on stderr, an out-of-subnet or syntactically-unresolvable
PI_HOST stays quiet, and — the specific regression this guards — a
resolver failure never aborts the script. That last case is a real bash
`set -e` hazard: a bare `var=$(failing_cmd)` propagates the failure and
aborts the whole script, which is exactly why resolve_pi_host_ipv4_addrs's
caller guards the substitution with `|| true`. Two different failure
shapes exercise two different layers of that: a syntactically-invalid
hostname fails inside Python (caught there, so the guard isn't even
reached), while the python3-shim test (mirroring test_lib_deploy_
direction.py's `_git_shim_dir` PATH-prepend pattern) makes the external
`python3` command itself fail, which the guard alone stands between and a
script-aborting regression — verified by removing the guard and watching
that same case abort with rc=1 (see the PR body).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from jasper.usb_network import ALLOCATION_SUPERNET, LEGACY_MANAGEMENT_CIDR

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "_lib.sh"
DEPLOY = ROOT / "scripts" / "deploy-to-pi.sh"


# ── usb_gadget_management_cidrs ───────────────────────────────────────────


def _management_cidrs(*, repo_root: Path | None = None) -> subprocess.CompletedProcess[str]:
    script = f'JTS_LIB_TARGET_OPTIONAL=1 source "{LIB}"; '
    if repo_root is not None:
        script += f'REPO_ROOT="{repo_root}"; '
    script += "usb_gadget_management_cidrs"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30,
    )


def test_management_cidrs_match_the_plan_owner():
    proc = _management_cidrs()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        str(ALLOCATION_SUPERNET),
        LEGACY_MANAGEMENT_CIDR,
    ]


def test_management_cidrs_follow_the_module_rather_than_hardcoded_copies(tmp_path):
    package = tmp_path / "jasper"
    package.mkdir()
    (package / "usb_network.py").write_text(
        'print("10.99.0.0/16")\nprint("10.98.7.1/25")\n'
    )

    proc = _management_cidrs(repo_root=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["10.99.0.0/16", "10.98.7.1/25"]


def test_management_cidrs_missing_module_is_a_clean_skip(tmp_path):
    proc = _management_cidrs(repo_root=tmp_path)
    assert proc.returncode != 0
    assert proc.stdout == ""


# ── ipv4_in_cidr ───────────────────────────────────────────────────────────


def _in_cidr(ip: str, cidr: str) -> subprocess.CompletedProcess[str]:
    script = f'JTS_LIB_TARGET_OPTIONAL=1 source "{LIB}"; ipv4_in_cidr "$1" "$2"'
    return subprocess.run(
        ["bash", "-c", script, "bash", ip, cidr],
        capture_output=True, text=True, timeout=30,
    )


def test_addresses_inside_the_gadget_subnet_match():
    for ip in ("10.12.194.1", "10.12.194.55", "10.12.194.254"):
        proc = _in_cidr(ip, "10.12.194.1/24")
        assert proc.returncode == 0, f"{ip} should be inside 10.12.194.1/24: {proc.stderr}"


def test_addresses_outside_the_gadget_subnet_do_not_match():
    # 10.12.195.1 / 10.12.193.255 are one network past the boundary, not
    # just "an obviously different address" — this catches an off-by-one
    # mask, not only a grossly wrong one.
    for ip in ("192.168.1.55", "10.12.195.1", "10.12.193.255"):
        proc = _in_cidr(ip, "10.12.194.1/24")
        assert proc.returncode == 1, f"{ip} should be outside 10.12.194.1/24"
        assert proc.stdout == ""


def test_garbage_input_does_not_match_and_does_not_error():
    cases = [
        ("not-an-ip", "10.12.194.1/24"),
        ("10.12.194", "10.12.194.1/24"),        # too few octets
        ("10.12.194.1.5", "10.12.194.1/24"),    # too many octets
        ("999.1.1.1", "10.12.194.1/24"),        # octet out of range
        ("", "10.12.194.1/24"),                 # empty IP
        ("10.12.194.1", "10.12.194.1"),         # cidr missing /prefix
        ("10.12.194.1", "10.12.194.1/"),        # empty prefix
        ("10.12.194.1", "10.12.194.1/33"),      # prefix out of range
        ("10.12.194.1", "garbage"),             # cidr not an address at all
    ]
    for ip, cidr in cases:
        proc = _in_cidr(ip, cidr)
        assert proc.returncode == 1, f"{ip!r} in {cidr!r} should not match: {proc.stderr}"
        assert proc.stdout == ""


# ── Integration: the deploy advisory wiring in deploy-to-pi.sh ───────────
#
# Drives the real warn_if_pi_host_on_gadget_network body — extracted from
# the script and sourced — against the real gadget CIDR (no stubbing of
# usb_gadget_management_cidr needed, the repo checkout has the real file).
# None of these depend on real DNS/mDNS reachability: PI_HOST is either an
# IP literal, a hostname that fails to parse before any lookup happens, or
# a hostname behind a python3 shim that never actually resolves anything.

_ADVISORY_HARNESS = r"""
set -euo pipefail
JTS_LIB_TARGET_OPTIONAL=1 source "@LIB@"
eval "$(awk '/^resolve_pi_host_ipv4_addrs\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "@DEPLOY@")"
eval "$(awk '/^warn_if_pi_host_on_gadget_network\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "@DEPLOY@")"
declare -F warn_if_pi_host_on_gadget_network >/dev/null || { echo "harness: extraction failed" >&2; exit 99; }
PI_HOST="@HOST@"
warn_if_pi_host_on_gadget_network
echo "REACHED_END"
"""


def _run_advisory(
    host: str, *, path_prepend: Path | None = None
) -> subprocess.CompletedProcess[str]:
    script = _ADVISORY_HARNESS.replace("@LIB@", str(LIB)).replace(
        "@DEPLOY@", str(DEPLOY)
    ).replace("@HOST@", host)
    env = None
    if path_prepend is not None:
        env = os.environ.copy()
        env["PATH"] = f"{path_prepend}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, env=env,
    )


def _python3_failure_shim_dir(tmp_path: Path) -> Path:
    """A directory holding a `python3` wrapper that always exits 1,
    prepended to PATH so deploy-to-pi.sh's own `python3` lookup hits this
    stub while every other command the sourced scripts touch (awk, grep,
    dirname, ...) still resolves through the real PATH behind it. Mirrors
    test_lib_deploy_direction.py's `_git_shim_dir` — shadow one binary
    instead of enumerating every dependency to reconstruct a minimal one.
    Reproduces "python3 itself is broken/missing" without depending on any
    real DNS/mDNS reachability.
    """
    shim_dir = tmp_path / "python3shim"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shim.chmod(0o755)
    return shim_dir


def test_advisory_warns_on_stderr_for_an_in_subnet_ip():
    proc = _run_advisory("10.12.194.77")
    assert proc.returncode == 0, proc.stderr        # advisory never aborts
    assert "REACHED_END" in proc.stdout
    assert "10.12.194.77" in proc.stderr
    assert "#2340" in proc.stderr
    # It is a warning: it must not leak onto stdout.
    assert "10.12.194.77" not in proc.stdout


def test_advisory_warns_for_new_plan_allocation_range():
    proc = _run_advisory("10.64.0.2")
    assert proc.returncode == 0, proc.stderr
    assert "10.64.0.2" in proc.stderr
    assert str(ALLOCATION_SUPERNET) in proc.stderr


def test_advisory_is_quiet_for_an_out_of_subnet_ip():
    proc = _run_advisory("192.168.1.50")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert "REACHED_END" in proc.stdout


def test_advisory_is_quiet_for_a_syntactically_invalid_hostname():
    # A hostname with a space is syntactically invalid and fails locally
    # inside Python's getaddrinfo without any network round-trip (caught
    # by resolve_pi_host_ipv4_addrs's own `except OSError`), so this is
    # deterministic offline too. This exercises the "normal" resolution
    # failure path; the `|| true` guard below covers the rarer case where
    # python3 itself fails outright.
    proc = _run_advisory("not a valid host")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert "REACHED_END" in proc.stdout


def test_advisory_does_not_abort_when_python3_itself_fails(tmp_path):
    # The regression this pins: resolve_pi_host_ipv4_addrs's caller must
    # guard the command substitution (`|| true`), or a bash `set -e`
    # hazard turns "python3 failed" into a script-aborting failure instead
    # of a quiet skip. Shimming python3 to fail exercises that guard
    # directly — mutation-verified by removing the guard and watching this
    # same case abort with rc=1 (see the PR body).
    shim_dir = _python3_failure_shim_dir(tmp_path)
    proc = _run_advisory("some-hostname.example", path_prepend=shim_dir)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert "REACHED_END" in proc.stdout


# ── SSH_BATCH_OPTS keepalive bound ───────────────────────────────────────
#
# Deleting ServerAliveInterval/ServerAliveCountMax from SSH_BATCH_OPTS leaves
# every test above green: nothing else asserts a severed transport surfaces
# as an ssh error rather than an unbounded hang. Read the values out of the
# real file rather than restating them here.


def _ssh_batch_opts_line() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    m = re.search(r"^SSH_BATCH_OPTS=\((.*)\)\s*$", text, flags=re.M)
    assert m is not None, "could not find SSH_BATCH_OPTS=(...) in deploy-to-pi.sh"
    return m.group(1)


def test_ssh_keepalive_options_pinned():
    opts = _ssh_batch_opts_line()

    for opt_name in ("ServerAliveInterval", "ServerAliveCountMax"):
        code_match = re.search(rf"\b{opt_name}=(\d+)\b", opts)
        assert code_match, f"SSH_BATCH_OPTS is missing {opt_name}=<N>: {opts!r}"
        assert int(code_match.group(1)) > 0, (
            f"{opt_name} must be a positive count/interval: {code_match.group(1)!r}"
        )
