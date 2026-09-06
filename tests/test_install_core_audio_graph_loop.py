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
so the loop is exercised hardware-free and root-free. The same harnesses cover
the profile staging transaction the two entry points share
(_with_unit_install_transaction over _stage_{full,streambox}_unit_files).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper import source_intent
from jasper.local_sources.registry import local_source_audio_refresh_units
from tests.install_surface import installer_shell_paths

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
_REAL_INSTALL = shutil.which("install") or "/usr/bin/install"

# Every destination the install table should attempt, regardless of mid-loop
# failure. Kept as the asserted contract so a future row addition is caught.
EXPECTED_DSTS = (
    "jasper-camilla.service",
    "jasper-camilla-recover.service",
    "jasper-camilla-crossover.service",
    "jasper-fanin.service",
    "jasper-fanin-coupling-auto.service",
    "jasper-source-intent-reconcile.service",
    "jasper-outputd.service",
    "jasper-control.service",
    "jasper-doctor-json.service",
    "jasper-xvf-firmware-update.service",
    "jasper-aec-commission.service",
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


def test_full_generation_rollback_restores_old_files_and_removes_new(tmp_path):
    existing = tmp_path / "existing.service"
    new = tmp_path / "new.service"
    transaction = tmp_path / "transaction"
    existing.write_text("old generation\n", encoding="utf-8")
    script = f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path / 'systemd'}"
source "{FRAGMENT}"
systemctl() {{ return 0; }}
install_transaction_dir="{transaction}"
mkdir -p "$install_transaction_dir"
declare -a install_transaction_paths=()
declare -a install_transaction_existed=()
_snapshot_unit_install_destination "{existing}"
printf 'mixed generation\n' > "{existing}"
_snapshot_unit_install_destination "{new}"
printf 'new generation\n' > "{new}"
_rollback_unit_install_transaction
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert existing.read_text(encoding="utf-8") == "old generation\n"
    assert not new.exists()
    assert not transaction.exists()


def test_full_generation_install_error_triggers_rollback(tmp_path):
    existing = tmp_path / "existing.service"
    staged = tmp_path / "staged.service"
    new = tmp_path / "new.service"
    transaction = tmp_path / "transaction"
    existing.write_text("old generation\n", encoding="utf-8")
    staged.write_text("new generation\n", encoding="utf-8")
    script = f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path / 'systemd'}"
source "{FRAGMENT}"
systemctl() {{ return 0; }}
install_transaction_dir="{transaction}"
mkdir -p "$install_transaction_dir"
declare -a install_transaction_paths=()
declare -a install_transaction_existed=()
set -E
trap '_rollback_unit_install_transaction' ERR
install() {{ _transactional_unit_install "$@"; }}
install -m 0644 "{staged}" "{existing}"
install -m 0644 "{tmp_path / 'missing.service'}" "{new}"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert existing.read_text(encoding="utf-8") == "old generation\n"
    assert not new.exists()
    assert not transaction.exists()


def test_usbgadget_forensics_units_both_roll_back_after_later_staging_failure(
    tmp_path,
):
    """Every file install must snapshot its exact destination, not a directory.

    GNU install accepts multiple sources plus a directory destination.  The full
    profile's transaction wrapper cannot safely roll that shape back because a
    directory snapshot can be restored *inside* the live directory, leaving a
    mixed generation.  Exercise the production USB helper and then fail a later
    staging row: both prior forensics units must return byte-for-byte and the
    systemd directory must contain no nested rollback artifact.
    """
    systemd_dir = tmp_path / "systemd"
    transaction = tmp_path / "transaction"
    systemd_dir.mkdir()
    service = systemd_dir / "jasper-usbgadget-forensics.service"
    path = systemd_dir / "jasper-usbgadget-forensics.path"
    service.write_text("old service generation\n", encoding="utf-8")
    path.write_text("old path generation\n", encoding="utf-8")
    script = f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{systemd_dir}"
source "{FRAGMENT}"
systemctl() {{ return 0; }}
# The production helper also installs non-systemd support files.  They are
# outside this transaction regression's scope and must not touch the host.
install_usb_network_files() {{ return 0; }}
install_transaction_dir="{transaction}"
mkdir -p "$install_transaction_dir"
declare -a install_transaction_paths=()
declare -a install_transaction_existed=()
set -E
trap '_rollback_unit_install_transaction' ERR
install() {{
  local destination="${{!#}}"
  case "$destination" in
    "$SYSTEMD_DIR"/*|"$SYSTEMD_DIR"/) _transactional_unit_install "$@" ;;
    *) return 0 ;;
  esac
}}
install_usbsink_unit_files
install -m 0644 "{tmp_path / 'missing.service'}" \
    "$SYSTEMD_DIR/later.service"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert service.read_text(encoding="utf-8") == "old service generation\n"
    assert path.read_text(encoding="utf-8") == "old path generation\n"
    assert not (systemd_dir / "later.service").exists()
    assert sorted(item.name for item in systemd_dir.iterdir()) == [
        "NetworkManager.service.d",
        "jasper-usbgadget-forensics.path",
        "jasper-usbgadget-forensics.service",
    ]
    assert list((systemd_dir / "NetworkManager.service.d").iterdir()) == []
    assert not transaction.exists()


def test_later_install_failure_restores_usb_projections_and_gate_state(tmp_path):
    """The generated pair belongs to the full-profile rollback generation."""

    transaction = tmp_path / "transaction"
    nm = tmp_path / "jts-usb.nmconnection"
    dnsmasq = tmp_path / "usbnet-dnsmasq.conf"
    gate = tmp_path / "jasper-usb-network-plan.service"
    dropin_dir = tmp_path / "NetworkManager.service.d"
    dropin = dropin_dir / "jasper-usb-network-plan.conf"
    staged_gate = tmp_path / "staged-gate.service"
    staged_dropin = tmp_path / "staged-plan.conf"
    nm.write_text("old nm generation\n", encoding="utf-8")
    dnsmasq.write_text("old dnsmasq generation\n", encoding="utf-8")
    gate.write_text("old gate generation\n", encoding="utf-8")
    staged_gate.write_text("new gate generation\n", encoding="utf-8")
    staged_dropin.write_text("new drop-in generation\n", encoding="utf-8")
    script = f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path / 'systemd'}"
source "{FRAGMENT}"
systemctl() {{ return 0; }}
install_transaction_dir="{transaction}"
mkdir -p "$install_transaction_dir" "{dropin_dir}"
declare -a install_transaction_paths=()
declare -a install_transaction_existed=()
set -E
trap '_rollback_unit_install_transaction' ERR
install() {{ _transactional_unit_install "$@"; }}
install -m 0644 "{staged_gate}" "{gate}"
install -m 0644 "{staged_dropin}" "{dropin}"
_snapshot_unit_install_destination "{nm}"
_snapshot_unit_install_destination "{dnsmasq}"
printf 'new nm generation\n' > "{nm}"
printf 'new dnsmasq generation\n' > "{dnsmasq}"
install -m 0644 "{tmp_path / 'missing.service'}" "{tmp_path / 'later.service'}"
"""

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=20
    )

    assert result.returncode != 0
    assert nm.read_text(encoding="utf-8") == "old nm generation\n"
    assert dnsmasq.read_text(encoding="utf-8") == "old dnsmasq generation\n"
    assert gate.read_text(encoding="utf-8") == "old gate generation\n"
    assert not dropin.exists()
    assert not (tmp_path / "later.service").exists()
    assert not transaction.exists()


def test_full_profile_does_not_duplicate_shared_install_rows() -> None:
    source = FRAGMENT.read_text()
    full = source.split("_stage_full_unit_files() {", 1)[1]
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


def test_source_intent_reapply_runs_the_bounded_full_coordinator():
    """The coordinator owns persistent and runtime state for every source,
    including Bluetooth RF-kill recovery, and it alone starts desired-on
    sources and repairs stale derived state. Both profiles reach it through
    this one helper, so its invocation is pinned here and the per-profile
    ordering by the argv recorder below.
    """
    source = FRAGMENT.read_text()
    # The shared helper is the ONE deploy path that runs the full coordinator.
    helper = _function_body(source, "reapply_source_intent")
    assert "jasper-source-intent-reconcile" in helper
    assert "--reason install --invalidate-status-before" in helper
    assert (
        "/usr/bin/timeout --foreground --kill-after=5s "
        f"{int(source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS)}s"
    ) in helper
    assert "--stop-disabled" not in helper


def test_upgrade_retires_destructive_combo_health_watcher():
    """A deploy removes the obsolete observer and its persisted override state."""

    body = _function_body(
        FRAGMENT.read_text(),
        "install_local_audio_graph_unit_files",
    )
    assert "systemctl disable --now jasper-fanin-combo-health.timer" in body
    assert "systemctl stop jasper-fanin-combo-health.service" in body
    assert "systemctl reset-failed jasper-fanin-combo-health.service" in body
    assert '"${SYSTEMD_DIR}/jasper-fanin-combo-health.timer"' in body
    assert '"${SYSTEMD_DIR}/jasper-fanin-combo-health.service"' in body
    assert "/var/lib/jasper/usb_combo_fallback.json" in body
    assert "/var/lib/jasper/combo_health_tick.json" in body


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


def _shim_preamble(tmp_path: Path, *, errexit: bool = True) -> str:
    """The install.sh globals the fragment assumes, every mutable root pointed
    at tmp_path, and the fragment itself. `errexit` is off for the runtime
    harness alone: that path also calls install.sh helpers and on-box binaries
    under /usr/local/sbin, neither of which exists here and both non-fatal on
    the box too."""
    return f"""
set -{"euo" if errexit else "uo"} pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path}/systemd"
STATE_DIR="{tmp_path}/state"
APPLE_DONGLE_SERVICE_CARD="auto"
mkdir -p "$SYSTEMD_DIR" "$STATE_DIR"
source "{FRAGMENT}"
"""


def _transaction_recorder(tmp_path: Path) -> str:
    """One ordered log of the systemctl argv a profile issues plus
    clear_install_in_progress, which lives in install.sh and so is covered by
    no fragment stub. Also pins the transaction directory under tmp_path. Emit
    AFTER any `declare -F` stub loop, which would otherwise replace these."""
    return f"""
systemctl() {{ echo "systemctl $*" >> "{tmp_path}/calls.log"; return 0; }}
clear_install_in_progress() {{ echo "fn clear_install_in_progress" >> "{tmp_path}/calls.log"; }}
mktemp() {{ local d; d="{tmp_path}/txn"; mkdir -p "$d"; printf '%s\\n' "$d"; }}
"""


def _profile_runtime_harness(
    tmp_path: Path, function: str, keep: tuple[str, ...] = ()
) -> str:
    """Run one profile's unit-install function with every fragment-defined
    helper stubbed into a recorder, so the systemctl argv it issues is
    observable off-box. `keep` names further fragment functions to leave real."""
    real = " ".join(shlex.quote(name) for name in (function, *keep))
    return f"""{_shim_preamble(tmp_path, errexit=False)}
LOG='{tmp_path}/calls.log'
for _stub in $(declare -F | awk '{{print $3}}'); do
    for _real in {real}; do
        [[ "$_stub" == "$_real" ]] && continue 2
    done
    eval "${{_stub}}() {{ echo \\"fn ${{_stub}}\\" >> \\"$LOG\\"; return 0; }}"
done
{_transaction_recorder(tmp_path)}
{function}
"""


@pytest.mark.parametrize(
    "function",
    ("start_streambox_runtime_units", "install_systemd_units"),
)
def test_both_profiles_restart_control_and_refresh_the_source_roster(
    tmp_path, function
):
    """Both profiles must RESTART jasper-control: enabling alone leaves the
    control plane — and, through its Wants=, CamillaDSP — down until the next
    reboot. The try-restart set must cover every unit jasper's own local-source
    roster names, and the USB baseline, the active-only refresh and the
    source-intent coordinator must stay in that order.

    Remove when the installer stops managing unit lifecycle.
    """
    result = subprocess.run(
        ["bash", "-c", _profile_runtime_harness(tmp_path, function)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()

    def first(prefix: str) -> int:
        hits = [i for i, call in enumerate(calls) if call.startswith(prefix)]
        assert hits, f"{prefix!r} never issued: {calls}"
        return hits[0]

    # jasper-control carries StartLimitAction=reboot; a spent burst must be
    # cleared or the restart reboots the Pi mid-install.
    assert first("systemctl reset-failed jasper-control.service") < first(
        "systemctl restart jasper-control.service"
    )

    refreshed = {
        unit
        for call in calls
        if call.startswith("systemctl try-restart ")
        for unit in call.split()[2:]
    }
    assert set(local_source_audio_refresh_units()) <= refreshed

    # A deploy must never transiently start a household-Off renderer: only
    # the coordinator may make a canonical On transition, and it runs last.
    assert (
        first("fn enable_usbgadget")
        < first("systemctl try-restart ")
        < first("fn reapply_source_intent")
    )
    assert not [
        call
        for call in calls
        if "nqptp" in call and not call.startswith("systemctl try-restart ")
    ]
    assert any(
        call.startswith("systemctl enable ")
        and "jasper-source-intent-reconcile.service" in call
        for call in calls
    )
    # Streambox uses the same direct USB data plane as a full speaker, so it
    # arms the combo owner inline, between the two; the full profile leaves
    # that to resolve_fanin_coupling_default after the coordinator's pass.
    if function == "start_streambox_runtime_units":
        assert (
            first("fn enable_usbgadget")
            < first("systemctl enable jasper-fanin-coupling-auto.service")
            < first("fn reapply_source_intent")
        )


@pytest.mark.parametrize(
    ("entry", "stage", "first_runtime_call", "post_commit"),
    (
        (
            "install_systemd_units",
            "_stage_full_unit_files",
            "systemctl enable --now jts-audio.slice jts-mic.slice",
            ("mask_distro_background_units",),
        ),
        (
            "install_streambox_systemd_units",
            "_stage_streambox_unit_files",
            "systemctl enable --now jts-audio.slice",
            ("park_streambox_brain_units", "mask_distro_background_units"),
        ),
    ),
)
def test_both_profiles_close_the_install_window_between_staging_and_runtime(
    tmp_path, entry, stage, first_runtime_call, post_commit
):
    """Both positions the shared transaction has to preserve, read off the
    recorded argv order rather than the source. #4218: the install window
    closes AFTER daemon-reload has loaded the staged generation (so gated units
    see their Condition lines) and BEFORE the profile's first enable/start.
    #4222: jasper-control is restarted from the runtime tail, i.e. after the
    wrapper returned and committed — the wrapper issues nothing recordable past
    clear_install_in_progress, so anything logged later is outside it.

    Remove when the installer stops staging units transactionally.
    """
    keep = ("_with_unit_install_transaction",)
    if entry == "install_streambox_systemd_units":
        keep += ("start_streambox_runtime_units",)
    result = subprocess.run(
        ["bash", "-c", _profile_runtime_harness(tmp_path, entry, keep=keep)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()

    def first(entry_prefix: str) -> int:
        hits = [i for i, call in enumerate(calls) if call.startswith(entry_prefix)]
        assert hits, f"{entry_prefix!r} never issued: {calls}"
        return hits[0]

    mutations = [
        i
        for i, call in enumerate(calls)
        if call.startswith(("systemctl enable", "systemctl start", "systemctl restart"))
    ]
    assert mutations, calls
    assert calls[mutations[0]] == first_runtime_call
    cleared = first("fn clear_install_in_progress")
    assert (
        first("fn install_jasper_support_files")
        < first("fn install_local_audio_graph_unit_files")
        < first(f"fn {stage}")
        < first("systemctl daemon-reload")
        < cleared
        < mutations[0]
        <= first("systemctl restart jasper-control.service")
    )
    # Parking and masking issue `disable --now`/`mask`, which no rollback can
    # undo, so both profiles keep them outside the transaction.
    assert all(cleared < first(f"fn {name}") for name in post_commit)


def _stage_rollback_harness(tmp_path: Path, stage: str) -> str:
    """Drive one profile's stage function under the real transaction wrapper,
    with `install` a stub EXECUTABLE on PATH: the wrapper's interceptor promotes
    with `command install`, which bypasses shell functions. The stub really
    copies destinations under tmp_path (so rollback has bytes to restore) and
    skips every other destination, keeping the run off the host's /etc and
    /usr/local."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "install"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'dst="${!#}"\n'
        'printf \'%s\\t%s\\n\' "$1" "$dst" >> "$JTS_STUB_CALLS"\n'
        'if [[ "$dst" == "$JTS_STUB_FAIL" ]]; then exit 1; fi\n'
        f'case "$dst" in {shlex.quote(str(tmp_path))}/*)'
        f' exec {shlex.quote(_REAL_INSTALL)} "$@" ;; esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return f"""{_shim_preamble(tmp_path)}
{_transaction_recorder(tmp_path)}
_with_unit_install_transaction {stage}
"""


@pytest.mark.parametrize(
    ("stage", "seeded", "staged_new", "fail_unit"),
    (
        (
            "_stage_full_unit_files",
            "jasper-voice.service",
            "jasper-input.service",
            "jasper-enhanced-aec-reconcile.path",
        ),
        (
            "_stage_streambox_unit_files",
            "jasper-web.service",
            "jasper-wifi-guardian.service",
            "jasper-wifi-scan-repair.service",
        ),
    ),
)
def test_a_failed_stage_rolls_the_whole_profile_generation_back(
    tmp_path, stage, seeded, staged_new, fail_unit
):
    """Both profiles stage through one rollback domain: when the Nth `install`
    fails, every destination touched since the wrapper opened is restored to
    its prior bytes, destinations that never existed are gone, and the profile
    reaches none of its enable/start work — so PID 1 keeps the generation it
    was already running instead of a half-replaced one. Streambox had no
    transaction at all before this.

    Remove when the installer stops staging units transactionally.
    """
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    (systemd_dir / seeded).write_text("old generation\n", encoding="utf-8")
    calls = tmp_path / "install.calls"
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"
    env["JTS_STUB_CALLS"] = str(calls)
    env["JTS_STUB_FAIL"] = str(systemd_dir / fail_unit)

    result = subprocess.run(
        ["bash", "-c", _stage_rollback_harness(tmp_path, stage)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert result.returncode != 0
    assert (systemd_dir / seeded).read_text(encoding="utf-8") == "old generation\n"
    assert not (systemd_dir / staged_new).exists()
    assert not (tmp_path / "txn").exists()
    # The harness only restores what it really copied, so the chosen failure
    # point must come before the first destination outside tmp_path. Drop this
    # assertion if the stage functions ever stop installing to absolute paths.
    promoted = {
        destination
        for mode, destination in (
            line.split("\t") for line in calls.read_text().splitlines()
        )
        if mode != "-d"
    }
    assert promoted and all(d.startswith(str(tmp_path)) for d in promoted)
    log = tmp_path / "calls.log"
    issued = log.read_text().splitlines() if log.exists() else []
    assert not [
        call
        for call in issued
        if call.startswith(("systemctl enable", "systemctl start"))
        or call == "fn clear_install_in_progress"
    ], issued


def _destination_harness(tmp_path: Path, function: str) -> str:
    """Record every destination one install step promotes, with the copy itself
    suppressed — nothing here may write to the host's /etc or /usr/local. The
    helpers stubbed out mutate state through something other than `install`
    (NetworkManager projection, udev reload, systemd-analyze), so none of them
    contributes a destination."""
    calls = tmp_path / "destinations.log"
    return f"""{_shim_preamble(tmp_path)}
install_transaction_dir="{tmp_path}/txn"
mkdir -p "$install_transaction_dir"
install() {{
  local dst="${{!#}}"
  [[ "$1" == "-d" ]] || printf '%s\\n' "$dst" >> "{calls}"
  return 0
}}
systemctl() {{ return 0; }}
install_usb_network_files() {{ return 0; }}
validate_streambox_systemd_units() {{ return 0; }}
reload_audio_recovery_udev_rules_for_install() {{ return 0; }}
{function}
"""


def _destinations(tmp_path: Path, function: str) -> set[str]:
    result = subprocess.run(
        ["bash", "-c", _destination_harness(tmp_path, function)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    log = tmp_path / "destinations.log"
    return {
        line.replace(str(tmp_path), "")
        for line in log.read_text().splitlines()
        if line.strip()
    }


def test_only_the_contained_builder_policy_lands_in_the_install_lib_dir(tmp_path):
    """/usr/local/lib/jasper/install has exactly one reader on the box,
    deploy/bin/jasper-contained-build, and it sources build-sandbox.sh alone.
    Copying the rest of deploy/lib/install/ shipped installer internals to
    every speaker for nobody to read."""
    landed = {
        Path(destination).name
        for destination in _destinations(tmp_path, "install_jasper_support_files")
        if destination.startswith("/usr/local/lib/jasper/install/")
    }
    assert landed == {"build-sandbox.sh"}
    assert landed < {path.name for path in installer_shell_paths()}
    # The on-box renderer lib deploy/bin/jasper-audio-hardware-reconcile falls
    # back to has this one owner, ahead of every /usr/local/sbin reconciler run.
    assert "/usr/local/lib/jasper/jasper-asound-render.sh" in _destinations(
        tmp_path / "support", "install_jasper_support_files"
    )


def test_a_streambox_stages_a_subset_of_the_full_unit_generation(tmp_path):
    """One roster: every destination a streambox stages, a full speaker stages
    too. While the full profile re-inlined the shared helpers instead of calling
    them, bluealsa-aplay's jts-restart.conf was streambox-only — a full speaker
    never got the drop-in that restarts Bluetooth audio after a clean exit."""
    full = _destinations(tmp_path / "full", "_stage_full_unit_files")
    streambox = _destinations(tmp_path / "streambox", "_stage_streambox_unit_files")
    assert streambox
    assert streambox <= full, streambox - full
    # Two drop-ins that shipped on one profile only in the past: the nginx
    # recovery drop-in (the doctor's installed-settings drift check expects
    # OOMScoreAdjust=-450 regardless of profile) and bluetooth's discovery
    # timeout.
    assert {
        "/systemd/nginx.service.d/jts-recovery.conf",
        "/systemd/bluetooth.service.d/jts-timeout.conf",
    } <= streambox & full
