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

import re
import subprocess
from pathlib import Path

from jasper import source_intent

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"

# Every destination the install table should attempt, regardless of mid-loop
# failure. Kept as the asserted contract so a future row addition is caught.
EXPECTED_DSTS = (
    "jasper-camilla.service",
    "jasper-camilla-recover.service",
    "jasper-camilla-crossover.service",
    "jasper-fanin.service",
    "jasper-fanin-coupling-auto.service",
    "jasper-source-intent-reconcile.service",
    "jasper-fanin-combo-health.service",
    "jasper-fanin-combo-health.timer",
    "jasper-outputd.service",
    "jasper-control.service",
    "jasper-doctor-json.service",
    "jasper-xvf-firmware-update.service",
    "jasper-audio-hardware-reconcile.service",
    "jasper-audio-hardware-reconcile",
    "jasper-output-hardware-hotplug",
    "jasper-outputd-failure-reconcile",
    "jasper-camilla-guard-common.sh",
    "jasper-camilla-pipe-guard",
    "jasper-camilla-recover",
    "jasper-camilla-crossover-guard",
    "jasper-fanin-pitch-neutralize",
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
        capture_output=True,
        text=True,
        timeout=20,
    )


def _attempted_dsts(tmp_path: Path) -> set[str]:
    log = tmp_path / "install.log"
    if not log.exists():
        return set()
    return {
        Path(line.replace("FAIL ", "").strip()).name
        for line in log.read_text().splitlines()
        if line.strip()
    }


def test_all_units_installed_on_clean_run(tmp_path):
    r = _run(tmp_path, fail_basename=None)
    assert r.returncode == 0, r.stderr
    attempted = _attempted_dsts(tmp_path)
    # Set-equality (not just a subset): EXPECTED_DSTS is the asserted contract,
    # so a future SSOT row added to JASPER_CORE_AUDIO_GRAPH_INSTALL_ROWS without
    # updating this tuple fails here — making good on the docstring promise that
    # "a future row addition is caught," not only a removal.
    assert attempted == set(EXPECTED_DSTS), (
        "core audio-graph install rows drifted from EXPECTED_DSTS: "
        f"missing={set(EXPECTED_DSTS) - attempted}, "
        f"unexpected={attempted - set(EXPECTED_DSTS)}"
    )
    # daemon-reload ran.
    assert (tmp_path / "reload.log").exists()


def test_common_library_failure_does_not_overwrite_guard_consumers(tmp_path):
    r = _run(tmp_path, fail_basename="jasper-camilla-guard-common.sh")
    assert r.returncode != 0
    attempted = _attempted_dsts(tmp_path)
    assert "jasper-camilla-guard-common.sh" in attempted
    assert "jasper-camilla-pipe-guard" not in attempted
    assert "jasper-camilla-crossover-guard" not in attempted


def test_full_install_uses_transactional_core_graph_installer():
    """The full profile must consume the same table as streambox installs.

    Otherwise a row can pass the table's unit tests yet never land on the
    production speaker path, which is how the combo-health timer was enabled
    before its unit file existed.
    """
    source = FRAGMENT.read_text()
    function_tail = source.split("install_systemd_units() {", 1)[1]
    commands = [
        line.strip()
        for line in function_tail.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert commands[:2] == [
        "install_jasper_support_files",
        "install_local_audio_graph_unit_files",
    ]


def test_full_profile_does_not_duplicate_shared_install_rows() -> None:
    source = FRAGMENT.read_text()
    full = source.split("install_systemd_units() {", 1)[1]
    table = source.split("JASPER_CORE_AUDIO_GRAPH_INSTALL_ROWS=(", 1)[1].split(
        "\n)\n",
        1,
    )[0]
    shared_sources = re.findall(r'"(?:0644|0755) ([^ ]+) ', table)
    assert shared_sources
    for shared_source in shared_sources:
        assert shared_source not in full, (
            f"full profile duplicates table-owned install source {shared_source}"
        )
    assert "jasper-fanin-pitch-neutralize" in table


def _function_body(source: str, name: str) -> str:
    """Extract a bash function body from the fragment. Functions here open with
    `name() {` and close with a `}` alone at column 0."""
    pattern = r"^" + re.escape(name) + r"\(\) \{\n(.*?)\n\}$"
    m = re.search(pattern, source, re.S | re.M)
    assert m, f"function {name} not found in systemd-units.sh"
    return m.group(1)


def test_both_profiles_refresh_only_active_sources_then_reapply_intent():
    """A deploy must never transiently start a household-Off renderer.

    The coordinator now owns persistent and runtime state for every source,
    including Bluetooth RF-kill recovery. Both profiles must enable its boot
    unit and run it after active-only refreshes. It alone starts desired-on
    sources and repairs any stale derived state.
    """
    source = FRAGMENT.read_text()
    for fn in ("start_streambox_runtime_units", "install_systemd_units"):
        body = _function_body(source, fn)
        baseline_idx = body.find("enable_usbgadget")
        assert baseline_idx != -1, f"{fn}: network-only USB baseline missing"
        restart_idx = body.find("systemctl try-restart bluealsa-aplay.service")
        assert restart_idx != -1, f"{fn}: active-only renderer refresh missing"
        assert "systemctl enable nqptp.service" not in body
        assert "systemctl restart nqptp.service" not in body
        reapply_idx = body.find("reapply_source_intent")
        assert reapply_idx != -1, f"{fn}: reapply_source_intent not called"
        assert reapply_idx > baseline_idx, (
            f"{fn}: installer must establish USB audio Off/NCM-only before the "
            "coordinator owns any canonical On transition"
        )
        assert reapply_idx > restart_idx, (
            f"{fn}: source-intent reconcile must run AFTER active renderer "
            "refreshes so desired-on sources converge on new code"
        )
        assert "jasper-source-intent-reconcile.service" in body, (
            f"{fn}: the coordinator must also be enabled for boot convergence"
        )
    # The shared helper is the ONE deploy path that runs the full coordinator.
    helper = _function_body(source, "reapply_source_intent")
    assert "jasper-source-intent-reconcile --reason install" in helper
    assert (
        "/usr/bin/timeout --foreground --kill-after=5s "
        f"{int(source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS)}s"
    ) in helper
    assert "mode=0o660" in helper
    assert "lock_mode=0o660" in helper
    assert "--stop-disabled" not in helper


def test_streambox_arms_usb_combo_supervision_before_source_intent_reapply():
    """Streambox uses the same direct USB data plane as a full speaker."""

    body = _function_body(
        FRAGMENT.read_text(),
        "start_streambox_runtime_units",
    )
    baseline_idx = body.find("enable_usbgadget")
    coupling_idx = body.find("systemctl enable jasper-fanin-coupling-auto.service")
    health_idx = body.find("systemctl enable --now jasper-fanin-combo-health.timer")
    reapply_idx = body.find("reapply_source_intent")

    assert -1 not in (baseline_idx, coupling_idx, health_idx, reapply_idx)
    assert baseline_idx < coupling_idx < health_idx < reapply_idx


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
        assert dst in attempted, (
            f"{dst} should still be attempted after a mid-loop failure"
        )
    # ...including later guards and the final pitch-neutralization helper.
    assert "jasper-camilla-crossover-guard" in attempted
    assert "jasper-fanin-pitch-neutralize" in attempted
    # daemon-reload ran despite the failure.
    assert (tmp_path / "reload.log").exists()
    assert "jasper-fanin.service" in r.stderr


def test_last_unit_failure_still_runs_daemon_reload(tmp_path):
    """A failure on the FINAL row must still leave a daemon-reload behind so the
    earlier units that landed are known to systemd."""
    r = _run(tmp_path, fail_basename="jasper-fanin-pitch-neutralize")
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
        capture_output=True,
        text=True,
        timeout=20,
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
        [
            "bash",
            "-c",
            f'REPO_DIR="{ROOT}"; SYSTEMD_DIR="{tmp_path}"; source "{FRAGMENT}"; '
            'printf "%s\\n" "${JASPER_CORE_GRAPH_RESTART_TARGETS[@]}"; '
            'echo "---"; '
            'printf "%s\\n" "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"',
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r.returncode == 0, r.stderr
    targets_block, _, park_block = r.stdout.partition("---\n")
    targets = {ln.strip() for ln in targets_block.splitlines() if ln.strip()}
    park = {ln.strip() for ln in park_block.splitlines() if ln.strip()}
    assert targets == {"jasper-fanin.service", "jasper-camilla.service"}
    assert targets.isdisjoint(park), (
        f"restart targets must not overlap parked clients: {targets & park}"
    )
