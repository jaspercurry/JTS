# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the transactional core audio-graph unit install loop in
deploy/lib/install/systemd-units.sh (install_local_audio_graph_unit_files).

The deploy hazard this guards: the function used to be a flat sequence of
`install -m` calls under the caller's `set -euo pipefail`, so a single failed
`install` aborted the whole sequence and silently skipped every LATER unit —
a newly-added unit at the end of the list would never land on the first deploy.
The loop now attempts EVERY row even if one fails, runs a daemon-reload
regardless, and re-raises at the end so a genuine error still surfaces.

The fragment is sourced into a harness with stub install.sh globals (REPO_DIR,
SYSTEMD_DIR) plus `install`/`systemctl` shimmed to record calls into log files,
so the loop is exercised hardware-free and root-free.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"

# Every destination the install table should attempt, regardless of mid-loop
# failure. Kept as the asserted contract so a future row addition is caught.
EXPECTED_DSTS = (
    "jasper-camilla.service",
    "jasper-camilla-recover.service",
    "jasper-camilla-crossover.service",
    "jasper-fanin.service",
    "jasper-outputd.service",
    "jasper-control.service",
    "jasper-doctor-json.service",
    "jasper-audio-hardware-reconcile.service",
    "jasper-audio-hardware-reconcile",
    "jasper-output-hardware-hotplug",
    "jasper-outputd-failure-reconcile",
    "jasper-camilla-pipe-guard",
    "jasper-camilla-recover",
    "jasper-camilla-crossover-guard",
)


def _harness(tmp_path: Path, *, fail_basename: str | None) -> str:
    """A bash script that sources the fragment with stub globals + shims and
    invokes the install loop. `fail_basename` makes the stub `install` return
    non-zero when the destination ends with that name (simulating a mid-loop
    failure)."""
    systemd_dir = tmp_path / "systemd"
    install_log = tmp_path / "install.log"
    reload_log = tmp_path / "reload.log"
    fail_clause = ""
    if fail_basename:
        fail_clause = (
            f'  case "$dst" in *{fail_basename}) echo "FAIL $dst" >> '
            f'"{install_log}"; return 1 ;; esac\n'
        )
    return f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{systemd_dir}"
# Shim `install`: record the final argument (destination) and the -d dir
# creates; honor the injected mid-loop failure.
install() {{
  local dst="${{!#}}"
  # -d directory creation: just succeed silently.
  if [[ "$1" == "-d" ]]; then return 0; fi
{fail_clause}  echo "$dst" >> "{install_log}"
  return 0
}}
# Shim `systemctl`: record daemon-reload invocations.
systemctl() {{
  if [[ "${{1:-}}" == "daemon-reload" ]]; then echo "daemon-reload" >> "{reload_log}"; fi
  return 0
}}
source "{FRAGMENT}"
install_local_audio_graph_unit_files
"""


def _run(tmp_path: Path, *, fail_basename: str | None):
    script = _harness(tmp_path, fail_basename=fail_basename)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=20,
    )


def _attempted_dsts(tmp_path: Path) -> set[str]:
    log = tmp_path / "install.log"
    if not log.exists():
        return set()
    return {Path(line.replace("FAIL ", "").strip()).name
            for line in log.read_text().splitlines() if line.strip()}


def test_all_units_installed_on_clean_run(tmp_path):
    r = _run(tmp_path, fail_basename=None)
    assert r.returncode == 0, r.stderr
    attempted = _attempted_dsts(tmp_path)
    for dst in EXPECTED_DSTS:
        assert dst in attempted, f"{dst} was not installed"
    # daemon-reload ran.
    assert (tmp_path / "reload.log").exists()


def test_midloop_failure_still_attempts_every_later_unit(tmp_path):
    """THE deploy hazard: a row in the MIDDLE fails. Every LATER row (including
    the newly-added guards at the end) must still be attempted, the function
    must report failure, and a daemon-reload must still run so the units that
    DID land take effect on this deploy."""
    # jasper-fanin.service is the 4th row — fail it and assert the tail still
    # gets attempted.
    r = _run(tmp_path, fail_basename="jasper-fanin.service")
    assert r.returncode != 0, "the loop must surface the row failure"
    attempted = _attempted_dsts(tmp_path)
    # Everything except the failed row was still attempted...
    for dst in EXPECTED_DSTS:
        assert dst in attempted, f"{dst} should still be attempted after a mid-loop failure"
    # ...including the LAST guard (the regression that motivated this).
    assert "jasper-camilla-crossover-guard" in attempted
    # daemon-reload ran despite the failure.
    assert (tmp_path / "reload.log").exists()
    assert "jasper-fanin.service" in r.stderr


def test_last_unit_failure_still_runs_daemon_reload(tmp_path):
    """A failure on the FINAL row must still leave a daemon-reload behind so the
    earlier units that landed are known to systemd."""
    r = _run(tmp_path, fail_basename="jasper-camilla-crossover-guard")
    assert r.returncode != 0
    assert (tmp_path / "reload.log").exists()


def _reset_failed_harness(tmp_path: Path) -> str:
    systemctl_log = tmp_path / "systemctl.log"
    return f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path / "systemd"}"
systemctl() {{ echo "$*" >> "{systemctl_log}"; return 0; }}
source "{FRAGMENT}"
reset_failed_core_graph_restart_targets
"""


def test_reset_failed_clears_fanin_and_camilla_before_restart(tmp_path):
    """Item 4 — deploy-churn StartLimit guard: jasper-fanin carries
    StartLimitAction=reboot, so a `systemctl restart` while it is `failed` with
    the burst exhausted would REBOOT the Pi mid-deploy. The install path must
    reset-failed both in-place restart targets first."""
    log = tmp_path / "systemctl.log"
    r = subprocess.run(
        ["bash", "-c", _reset_failed_harness(tmp_path)],
        capture_output=True, text=True, timeout=20,
    )
    assert r.returncode == 0, r.stderr
    calls = log.read_text() if log.exists() else ""
    assert "reset-failed jasper-fanin.service" in calls
    assert "reset-failed jasper-camilla.service" in calls


def test_reset_failed_targets_exclude_parked_units(tmp_path):
    """The restart-target reset set is DISJOINT from the parked-client set
    (which park_audio_clients_for_core_graph_restart already reset-failed):
    fanin/camilla are restarted in place, never parked."""
    r = subprocess.run(
        ["bash", "-c",
         f'REPO_DIR="{ROOT}"; SYSTEMD_DIR="{tmp_path}"; source "{FRAGMENT}"; '
         'printf "%s\\n" "${JASPER_CORE_GRAPH_RESTART_TARGETS[@]}"; '
         'echo "---"; '
         'printf "%s\\n" "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"'],
        capture_output=True, text=True, timeout=20,
    )
    assert r.returncode == 0, r.stderr
    targets_block, _, park_block = r.stdout.partition("---\n")
    targets = {ln.strip() for ln in targets_block.splitlines() if ln.strip()}
    park = {ln.strip() for ln in park_block.splitlines() if ln.strip()}
    assert targets == {"jasper-fanin.service", "jasper-camilla.service"}
    assert targets.isdisjoint(park), (
        f"restart targets must not overlap parked clients: {targets & park}"
    )
