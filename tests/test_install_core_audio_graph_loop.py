# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavior pins for transactional unit installation."""

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
BUILD_SANDBOX = ROOT / "deploy" / "lib" / "install" / "build-sandbox.sh"
_REAL_INSTALL = shutil.which("install") or "/usr/bin/install"

EXPECTED_DSTS = (
    "jasper-camilla-topology-gate",
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
    "jasper-unpark",
    "jasper-camilla-guard-common.sh",
    "jasper-camilla-pipe-guard",
    "jasper-camilla-recover",
    "jasper-camilla-crossover-guard",
    "jasper-fanin-pitch-neutralize",
)


# Shim `rm`: confine every call to the temp root, so a destructive absolute
# path anywhere in the fragment can never delete the real file on a host that
# ran install.sh. A call outside `{tmp_path}` is logged and refused (not
# executed) rather than silently allowed through.
_RM_ESCAPE_GUARD_SHIM = """\
rm() {{
  local arg
  for arg in "$@"; do
    case "$arg" in
      -*) continue ;;
      "{tmp_path}"/*) continue ;;
      *) echo "REFUSED $arg" >> "{rm_log}"; return 1 ;;
    esac
  done
  command rm "$@"
}}
"""


def _assert_no_rm_escaped(tmp_path: Path) -> None:
    log = tmp_path / "rm.log"
    if not log.exists():
        return
    escaped = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert not escaped, f"rm escaped the temp root: {escaped}"


def _harness(tmp_path: Path, *, fail_basename: str | None) -> str:
    systemd_dir = tmp_path / "systemd"
    install_log = tmp_path / "install.log"
    reload_log = tmp_path / "reload.log"
    fail_clause = ""
    if fail_basename:
        fail_clause = (
            f'  case "$dst" in *{fail_basename}) echo "FAIL $dst" >> '
            f'"{install_log}"; return 1 ;; esac\n'
        )
    local_sbin_dir = tmp_path / "usrlocalsbin"
    rm_log = tmp_path / "rm.log"
    return f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{systemd_dir}"
LOCAL_SBIN_DIR="{local_sbin_dir}"
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
{_RM_ESCAPE_GUARD_SHIM.format(tmp_path=tmp_path, rm_log=rm_log)}
source "{FRAGMENT}"
install_local_audio_graph_unit_files
"""


def _run(tmp_path: Path, *, fail_basename: str | None):
    script = _harness(tmp_path, fail_basename=fail_basename)
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    _assert_no_rm_escaped(tmp_path)
    return result


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
    assert attempted == set(EXPECTED_DSTS), (
        "core audio-graph install rows drifted from EXPECTED_DSTS: "
        f"missing={set(EXPECTED_DSTS) - attempted}, "
        f"unexpected={attempted - set(EXPECTED_DSTS)}"
    )
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


def _function_body(source: str, name: str) -> str:
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


def test_midloop_failure_still_attempts_every_later_unit(tmp_path):
    r = _run(tmp_path, fail_basename="jasper-fanin.service")
    assert r.returncode != 0, "the loop must surface the row failure"
    attempted = _attempted_dsts(tmp_path)
    for dst in EXPECTED_DSTS:
        assert dst in attempted, (
            f"{dst} should still be attempted after a mid-loop failure"
        )
    assert "jasper-camilla-crossover-guard" in attempted
    assert "jasper-fanin-pitch-neutralize" in attempted
    assert (tmp_path / "reload.log").exists()
    assert "jasper-fanin.service" in r.stderr


def test_last_unit_failure_still_runs_daemon_reload(tmp_path):
    r = _run(tmp_path, fail_basename="jasper-fanin-pitch-neutralize")
    assert r.returncode != 0
    assert (tmp_path / "reload.log").exists()


def test_graph_park_retires_a_stale_record_before_stopping_active_outputd(tmp_path):
    park = tmp_path / "outputd.park"
    park.write_text("old\n")
    local_sbin = tmp_path / "sbin"
    local_sbin.mkdir()
    unpark = local_sbin / "jasper-unpark"
    unpark.write_text(
        "#!/usr/bin/env bash\n"
        f'"{ROOT}/deploy/bin/jasper-unpark" "$@"\n'
        f'echo unpark >> "{tmp_path}/calls.log"\n'
    )
    unpark.chmod(0o755)
    script = f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path}/systemd"
LOCAL_SBIN_DIR="{local_sbin}"
source "{FRAGMENT}"
OUTPUTD_FAILURE_PARK_RECORD="{park}"
JASPER_CORE_GRAPH_PARK_UNITS=(jasper-outputd.service)
_record_parked_unit() {{ :; }}
systemctl() {{
  if [[ "$1" == "is-active" ]]; then return 0; fi
  echo "$*" >> "{tmp_path}/calls.log"
}}
park_audio_clients_for_core_graph_restart
"""
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=20
    )

    assert result.returncode == 0, result.stderr
    assert not park.exists()
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert calls.index("unpark") < calls.index("stop jasper-outputd.service")


def test_the_two_park_lists_overlap_only_by_the_crossover(tmp_path):
    """The core-graph park and the low-memory build park write ONE record, and
    forget_core_graph_park_record drops every entry the core-graph list names.
    jasper-camilla-crossover sits in both and is safe to drop because the
    grouping reconciler the tail runs re-arms it. Another unit added to the
    overlap would be dropped with no such owner, so pin the overlap exactly."""
    r = subprocess.run(
        [
            "bash",
            "-c",
            f'REPO_DIR="{ROOT}"; SYSTEMD_DIR="{tmp_path}"; source "{FRAGMENT}"; '
            'printf "%s\\n" "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"; '
            'echo "---"; '
            'printf "%s\\n" "${JASPER_LOW_MEMORY_BUILD_PARK_UNITS[@]}"',
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r.returncode == 0, r.stderr
    core_block, _, build_block = r.stdout.partition("---\n")
    core = {ln.strip() for ln in core_block.splitlines() if ln.strip()}
    build = {ln.strip() for ln in build_block.splitlines() if ln.strip()}
    assert core & build == {"jasper-camilla-crossover.service"}


def _stateful_systemctl(tmp_path: Path) -> str:
    """A `systemctl` that keeps real active/inactive state under
    `<tmp_path>/down`, so `is-active` answers truthfully across a park and
    `try-restart` stays the no-op it is on an inactive unit. Every argv is
    appended to one ordered call log."""
    return f"""
mkdir -p "{tmp_path}/down"
systemctl() {{
  local verb="${{1:-}}" arg now=0
  local -a units=()
  echo "systemctl $*" >> "{tmp_path}/calls.log"
  shift || true
  for arg in ${{1+"$@"}}; do
    case "$arg" in
      --now) now=1 ;;
      -*) ;;
      *) units+=("$arg") ;;
    esac
  done
  (( ${{#units[@]}} )) || return 0
  case "${{verb}}:${{now}}" in
    is-active:*)
      if [[ -e "{tmp_path}/down/${{units[0]}}" ]]; then return 1; fi ;;
    is-enabled:*) echo enabled ;;
    stop:*) for arg in "${{units[@]}}"; do : > "{tmp_path}/down/$arg"; done ;;
    start:*|restart:*|enable:1)
      for arg in "${{units[@]}}"; do rm -f "{tmp_path}/down/$arg"; done ;;
  esac
  return 0
}}
"""


def _abort_mid_tail_harness(tmp_path: Path) -> str:
    """install.sh's shape around the core-graph park: the REAL EXIT trap entry
    (`install_exit_cleanup`, which reaches the unpark through
    `_call_if_defined`), the park, then the first restart-tail command made to
    fail under `set -euo pipefail`."""
    return f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path}/systemd"
STATE_DIR="{tmp_path}/state"
mkdir -p "$SYSTEMD_DIR" "$STATE_DIR"
{_stateful_systemctl(tmp_path)}
source "{FRAGMENT}"
source "{BUILD_SANDBOX}"
# install.sh's first unguarded restart-tail command, made to fail.
ensure_outputd_camilla_statefile() {{ return 1; }}
trap install_exit_cleanup EXIT
park_audio_clients_for_core_graph_restart
ensure_outputd_camilla_statefile
"""


def test_an_abort_mid_tail_leaves_no_parked_core_graph_unit_stopped(tmp_path):
    """F-S2-1: `park_audio_clients_for_core_graph_restart` stops voice, the
    output owner, mux and every renderer, and the restart tail below it runs
    unguarded under `set -e`. An abort there must not leave a silent speaker:
    once the trap has run, nothing the park stopped is still stopped."""
    r = subprocess.run(
        ["bash", "-c", _abort_mid_tail_harness(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode != 0, r.stdout
    calls = (tmp_path / "calls.log").read_text().splitlines()
    stopped = {c.split()[2] for c in calls if c.startswith("systemctl stop ")}
    # Not vacuous: the park has to have taken the speaker down first.
    assert {"jasper-voice.service", "jasper-outputd.service"} <= stopped, calls
    still_down = {p.name for p in (tmp_path / "down").iterdir()}
    assert not still_down, f"left stopped after the trap: {sorted(still_down)}"


def _shim_preamble(tmp_path: Path, *, errexit: bool = True) -> str:
    return f"""
set -{"euo" if errexit else "uo"} pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path}/systemd"
STATE_DIR="{tmp_path}/state"
INSTALL_DIR="{tmp_path}/opt/jasper"
LOCAL_SBIN_DIR="{tmp_path}/usrlocalsbin"
APPLE_DONGLE_SERVICE_CARD="auto"
mkdir -p "$SYSTEMD_DIR" "$STATE_DIR" "$LOCAL_SBIN_DIR"
{_RM_ESCAPE_GUARD_SHIM.format(tmp_path=tmp_path, rm_log=tmp_path / "rm.log")}
source "{FRAGMENT}"
"""


# `rm` rides along here (not a recorder's own shim, but excluded from the stub
# loop for the same reason): _shim_preamble's temp-root guard has to survive
# the loop, because _stateful_systemctl's own start/restart branch below calls
# real `rm -f "{tmp_path}/down/$arg"` to clear a unit's down-marker, and a
# stub swallowing that call would leave every unit reading as still-down.
_RECORDER_SHIMS = ("systemctl", "clear_install_in_progress", "mktemp", "rm")


def _transaction_recorder(tmp_path: Path) -> str:
    return f"""
systemctl() {{ echo "systemctl $*" >> "{tmp_path}/calls.log"; return 0; }}
clear_install_in_progress() {{ echo "fn clear_install_in_progress" >> "{tmp_path}/calls.log"; }}
mktemp() {{ local d; d="{tmp_path}/txn"; mkdir -p "$d"; printf '%s\\n' "$d"; }}
"""


def _profile_runtime_harness(
    tmp_path: Path,
    function: str,
    keep: tuple[str, ...] = (),
    *,
    extra_shims: str = "",
    epilogue: str = "",
) -> str:
    real = " ".join(
        shlex.quote(name)
        for name in (function, *keep, *_RECORDER_SHIMS)
    )
    return f"""{_shim_preamble(tmp_path, errexit=False)}
LOG='{tmp_path}/calls.log'
for _stub in $(declare -F | awk '{{print $3}}'); do
    for _real in {real}; do
        [[ "$_stub" == "$_real" ]] && continue 2
    done
    eval "${{_stub}}() {{ echo \\"fn ${{_stub}}\\" >> \\"$LOG\\"; return 0; }}"
done
{_transaction_recorder(tmp_path)}
{extra_shims}
{function}
{epilogue}
"""


# The park/record/unpark chain, left real inside the profile harness so the
# trap sees the record the profile's own park built. The shared core-graph
# tail rides along: it is what both profiles reach the park through.
_PARK_RECORD_CHAIN = (
    "_start_core_graph_units",
    "park_audio_clients_for_core_graph_restart",
    "forget_core_graph_park_record",
    "unpark_recorded_units",
    "_record_parked_unit",
    "_unpark_one_unit",
    "_jasper_unit_in_list",
    "_jasper_unit_was_off_at_park",
)

# Units the restart tail deliberately leaves stopped in the scenario below.
_LEFT_OFF_BY_THE_TAIL = frozenset(
    {
        "jasper-outputd.service",
        "jasper-voice.service",
        "jasper-snapclient.service",
        "jasper-snapserver.service",
    }
)

# The reconcilers own the core-graph units once the tail has run.
# require_outputd_ready stands in for a park_output_audio that refused to
# validate the DAC lane. reconcile_grouping_state genuinely stops
# snapclient/snapserver: both ship disabled and are reconciler-started, so they
# are in OFF_AT_PARK and the unpark's "left off on purpose" skip can never
# protect them. jasper-audio-hardware-reconcile is an absolute-path binary that
# does not exist here; shim it converged so its own WARN arm is the one
# variable the degraded run below changes.
_TAIL_RECONCILER_SHIMS = """
install_run_bounded() { shift 2; "$@"; }
/usr/local/sbin/jasper-audio-hardware-reconcile() { return 0; }
require_outputd_ready() {
    systemctl stop jasper-outputd.service jasper-voice.service
    return 0
}
reconcile_grouping_state() {
    systemctl stop jasper-snapclient.service jasper-snapserver.service
}
# build-sandbox.sh is sourced only in the epilogue (the stub loop must not see
# it), so the fragment's journal helper needs a stand-in during the tail.
_build_sandbox_log() { :; }
"""

# The same tail with the hardware reconcile WARNing instead of converging: it
# runs to the end anyway (every one of its steps is non-fatal) but nothing has
# taken ownership of the parked units, so the record must survive to the trap.
_DEGRADED_TAIL_SHIM = """
/usr/local/sbin/jasper-audio-hardware-reconcile() { return 1; }
"""


def _run_tail(
    tmp_path: Path,
    function: str,
    *,
    low_memory: bool = False,
    degraded: bool = False,
) -> tuple[list[str], list[str]]:
    """Run one profile's whole restart tail with the park/record/unpark chain
    real, a stateful systemctl and reconcilers that leave
    `_LEFT_OFF_BY_THE_TAIL` stopped, then enter the installer's REAL EXIT trap
    entry. Returns the call log split at the sentinel: what the tail did, then
    what the trap did."""
    shims = _stateful_systemctl(tmp_path) + _TAIL_RECONCILER_SHIMS
    if degraded:
        shims += _DEGRADED_TAIL_SHIM
    if low_memory:
        # build_swap_required lives in build-sandbox.sh, which the stub loop
        # must not have seen; force the constrained-build park on.
        shims += (
            "build_swap_required() { return 0; }\npark_low_memory_build_units\n"
        )
    result = subprocess.run(
        [
            "bash",
            "-c",
            _profile_runtime_harness(
                tmp_path,
                function,
                keep=(*_PARK_RECORD_CHAIN, "park_low_memory_build_units"),
                extra_shims=shims,
                epilogue=(
                    f'echo TAIL_DONE >> "{tmp_path}/calls.log"\n'
                    f'source "{BUILD_SANDBOX}"\n'
                    "install_exit_cleanup\n"
                ),
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert "TAIL_DONE" in calls, calls
    cut = calls.index("TAIL_DONE")
    return calls[:cut], calls[cut + 1:]


@pytest.mark.parametrize(
    "function",
    ("start_streambox_runtime_units", "install_systemd_units"),
)
def test_the_exit_trap_starts_nothing_after_a_green_restart_tail(
    tmp_path, function
):
    """The park record exists for an abort, not for a success. Once the
    restart tail has run, its reconcilers own every parked core-graph unit and
    one they left stopped is stopped ON PURPOSE — an output lane the hardware
    reconciler refused to validate, a follower's snapserver. Replaying the
    record then would start a renderer against a rejected lane, and would start
    a second snapserver on a leader. After a green tail the trap must start
    nothing."""
    tail, trap = _run_tail(tmp_path, function)

    # Not vacuous: the park must have recorded these (the shim reports every
    # unit active until it is stopped) and the tail must have left them down.
    parked = {c.split()[2] for c in tail if c.startswith("systemctl stop ")}
    assert _LEFT_OFF_BY_THE_TAIL <= parked, tail
    still_down = {p.name for p in (tmp_path / "down").iterdir()}
    assert _LEFT_OFF_BY_THE_TAIL <= still_down, sorted(still_down)

    started = [
        call
        for call in trap
        if re.match(r"systemctl (start|restart|try-restart) ", call)
        or call.startswith("systemctl enable --now ")
    ]
    assert not started, f"the trap replayed the park after a green tail: {started}"


def test_a_green_tail_keeps_the_low_memory_build_park_restorable(tmp_path):
    """The other half of the same record. `park_low_memory_build_units` runs
    before the Rust builds and stops a phase the restart tail does NOT put
    back: bt-agent is reached only by a `try-restart`, a no-op while it is
    stopped. Dropping the core-graph entries at the end of the tail must not
    take that phase with them — the trap is still its only restore."""
    _, trap = _run_tail(tmp_path, "install_systemd_units", low_memory=True)
    started = {
        call.split()[2] for call in trap if call.startswith("systemctl start ")
    }
    assert "bt-agent.service" in started, trap
    assert not (_LEFT_OFF_BY_THE_TAIL & started), sorted(started)


@pytest.mark.parametrize(
    "function",
    ("start_streambox_runtime_units", "install_systemd_units"),
)
def test_a_degraded_tail_keeps_the_core_graph_park_restorable(tmp_path, function):
    """The forget is only earned by a tail that CONVERGED. Every step that
    justifies it is non-fatal (`|| WARN`), so one of them WARNing leaves the
    parked units down with no reconciler that owns them. Dropping the record
    there would end the install green on a silent speaker with nothing left to
    restore it, so a WARNed tail must reach the trap with the record intact."""
    tail, trap = _run_tail(tmp_path, function, degraded=True)

    # Not vacuous: the park has to have taken the speaker down first.
    parked = {c.split()[2] for c in tail if c.startswith("systemctl stop ")}
    assert _LEFT_OFF_BY_THE_TAIL <= parked, tail

    started = {c.split()[2] for c in trap if c.startswith("systemctl start ")}
    assert _LEFT_OFF_BY_THE_TAIL <= started, sorted(started)
    still_down = {p.name for p in (tmp_path / "down").iterdir()}
    assert not (_LEFT_OFF_BY_THE_TAIL & still_down), sorted(still_down)


@pytest.mark.parametrize(
    "function",
    ("start_streambox_runtime_units", "install_systemd_units"),
)
def test_a_fresh_outputd_park_blocks_direct_dependency_and_exit_starts(
    tmp_path, function
):
    park = tmp_path / "outputd.park"
    systemctl = f"""
mkdir -p "{tmp_path}/down"
_attempt_outputd() {{
  local via="$1"
  if [[ -e "{park}" ]]; then
    echo "outputd condition-skip via=$via" >> "{tmp_path}/calls.log"
    return 0
  fi
  echo "outputd exec-start via=$via" >> "{tmp_path}/calls.log"
  rm -f "{tmp_path}/down/jasper-outputd.service"
}}
systemctl() {{
  local verb="${{1:-}}" arg now=0
  local -a units=()
  echo "systemctl $*" >> "{tmp_path}/calls.log"
  shift || true
  for arg in ${{1+"$@"}}; do
    case "$arg" in
      --now) now=1 ;;
      -*) ;;
      *) units+=("$arg") ;;
    esac
  done
  (( ${{#units[@]}} )) || return 0
  case "$verb:$now" in
    is-active:*) [[ ! -e "{tmp_path}/down/${{units[0]}}" ]] && return 0 || return 1 ;;
    is-enabled:*) echo enabled; return 0 ;;
    stop:*) for arg in "${{units[@]}}"; do : > "{tmp_path}/down/$arg"; done ;;
    start:*|restart:*|try-restart:*|enable:1)
      for arg in "${{units[@]}}"; do
        case "$arg" in
          jasper-outputd.service) _attempt_outputd "$arg" ;;
          jasper-camilla.service|jasper-control.service|jasper-voice.service|jasper-accessory-reconcile.service)
            _attempt_outputd "$arg"
            rm -f "{tmp_path}/down/$arg"
            ;;
          *) rm -f "{tmp_path}/down/$arg" ;;
        esac
      done
      ;;
  esac
  return 0
}}
"""
    fresh_park = f"""
OUTPUTD_FAILURE_PARK_RECORD="{park}"
require_outputd_ready() {{
  systemctl restart jasper-outputd.service
  systemctl stop jasper-outputd.service
  printf 'parked_at=1\\nexit_status=78\\nreason=recent\\n' > "{park}"
  echo PARK_CREATED >> "{tmp_path}/calls.log"
  systemctl restart jasper-outputd.service
  return 1
}}
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            _profile_runtime_harness(
                tmp_path,
                function,
                keep=(
                    *_PARK_RECORD_CHAIN,
                    "restart_core_camilla_after_dsp_reconcile",
                    "restart_jasper_control_and_input",
                ),
                extra_shims=systemctl + _TAIL_RECONCILER_SHIMS + fresh_park,
                epilogue=(
                    f'echo TAIL_DONE >> "{tmp_path}/calls.log"\n'
                    f'source "{BUILD_SANDBOX}"\n'
                    f'_build_sandbox_log() {{ echo "event=$1 $2" >> "{tmp_path}/calls.log"; }}\n'
                    "install_exit_cleanup\n"
                    f'echo INSTALL_MARKER_CLEARED >> "{tmp_path}/calls.log"\n'
                    "systemctl restart jasper-control.service\n"
                    "systemctl start jasper-outputd.service\n"
                ),
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    cut = calls.index("PARK_CREATED")
    assert any(call == "outputd exec-start via=jasper-outputd.service" for call in calls[:cut])
    after = calls[cut + 1:]
    starts = [call for call in after if call.startswith("outputd ")]
    assert starts
    assert not [call for call in starts if call.startswith("outputd exec-start")]
    assert {
        "jasper-outputd.service",
        "jasper-camilla.service",
        "jasper-control.service",
    } <= {call.split("via=", 1)[1] for call in starts}
    tail_done = after.index("TAIL_DONE")
    cleared = after.index("INSTALL_MARKER_CLEARED")
    assert any(
        "via=jasper-outputd.service" in call
        for call in after[tail_done + 1:cleared]
    )
    assert any(
        call == "event=unpark_skip unit=jasper-outputd.service reason=config_fault_parked"
        for call in after[tail_done + 1:cleared]
    )
    assert any("via=jasper-control.service" in call for call in after[cleared + 1:])
    assert any("via=jasper-outputd.service" in call for call in after[cleared + 1:])


@pytest.mark.parametrize(
    "function",
    ("start_streambox_runtime_units", "install_systemd_units"),
)
def test_both_profiles_restart_control_and_refresh_the_source_roster(
    tmp_path, function
):
    result = subprocess.run(
        [
            "bash",
            "-c",
            _profile_runtime_harness(
                tmp_path,
                function,
                keep=("_start_core_graph_units", "restart_jasper_control_and_input"),
            ),
        ],
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
    # jasper-input's HID bridge posts key events to jasper-control, so it must
    # restart after, never before.
    assert first("systemctl restart jasper-control.service") < first(
        "systemctl restart jasper-input.service"
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
            ("activate_staged_unit_files", "mask_distro_background_units"),
        ),
        (
            "install_streambox_systemd_units",
            "_stage_streambox_unit_files",
            "systemctl enable --now jts-audio.slice",
            ("activate_staged_unit_files", "park_streambox_brain_units", "mask_distro_background_units"),
        ),
    ),
)
def test_both_profiles_close_the_install_window_between_staging_and_runtime(
    tmp_path, entry, stage, first_runtime_call, post_commit
):
    keep = ("_with_unit_install_transaction", "restart_jasper_control_and_input")
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
        first("fn install_local_audio_graph_unit_files")
        < first(f"fn {stage}")
        < first("systemctl daemon-reload")
        < first("fn validate_installed_systemd_units")
        < cleared
        < mutations[0]
        <= first("systemctl restart jasper-control.service")
    )
    # Parking and masking issue `disable --now`/`mask`, which no rollback can
    # undo, so both profiles keep them outside the transaction.
    assert all(cleared < first(f"fn {name}") for name in post_commit)


def _stage_rollback_harness(tmp_path: Path, stage: str, shims: str = "", tail: str = "") -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "install"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'args=(); while (( $# )); do\n'
        '  case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac\n'
        'done; set -- "${args[@]}"\n'
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
{shims}
_with_unit_install_transaction {stage}
{tail}
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
    _assert_no_rm_escaped(tmp_path)
    assert (systemd_dir / seeded).read_text(encoding="utf-8") == "old generation\n"
    assert not (systemd_dir / staged_new).exists()
    assert not (tmp_path / "txn").exists()
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
        if (call.startswith("systemctl ") and call != "systemctl daemon-reload")
        or call.startswith(("nmcli ", "udevadm "))
        or call == "fn clear_install_in_progress"
    ], issued


@pytest.mark.parametrize("profile,inherited", [("full", True), ("streambox", True), ("streambox", False)])
@pytest.mark.parametrize("fault", [None, "stage", "reload"])
def test_turntable_migration_preserves_the_stop_target_until_unit_commit(
    tmp_path, profile, inherited, fault
):
    fragment = tmp_path / "systemd-units.sh"
    source = FRAGMENT.read_text()
    for root in ("/etc/", "/usr/local/", "/var/lib/", "/sys/"):
        source = source.replace(root, f"{tmp_path}{root}")
    fragment.write_text(source)
    install_dir = tmp_path / "opt/jasper"
    old_tool = install_dir / "experiments/usb-turntable/jts_turntable.py"
    unrelated = install_dir / "experiments/unrelated/keep.txt"
    new_tool = install_dir / "jasper/turntable/jts_turntable.py"
    for path in (old_tool, unrelated, new_tool):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep\n")
    for directory in ("usr/local/sbin", "usr/local/bin", "usr/local/lib/jasper"):
        (tmp_path / directory).mkdir(parents=True)
    unit = tmp_path / "systemd/jasper-turntable-autostop@.service"
    rule = tmp_path / "etc/udev/rules.d/99-jasper-turntable-autostop.rules"
    old_unit = "[Service]\nExecStart=/usr/bin/python3 /opt/jasper/experiments/usb-turntable/jts_turntable.py\n"
    if inherited:
        unit.parent.mkdir(parents=True)
        unit.write_text(old_unit)
        rule.parent.mkdir(parents=True)
        rule.write_text("inherited rule\n")
    shims = f'''
INSTALL_DIR="{install_dir}"
install_usb_network_files() {{ :; }}
validate_installed_systemd_units() {{ return 0; }}
reload_audio_recovery_udev_rules_for_install() {{ :; }}
activate_usb_network() {{ :; }}
stage() {{
    _stage_{profile}_unit_files
    return {1 if fault == "stage" else 0}
}}
systemctl() {{
    [[ "$1" == daemon-reload ]] || return 0
    [[ -f "{old_tool}" ]] || return 99
    if [[ ! -d "{tmp_path}/txn" ]]; then
        return {1 if fault == "reload" else 0}
    fi
}}
'''
    script = _stage_rollback_harness(
        tmp_path, "stage", shims, "activate_staged_unit_files"
    ).replace(f'source "{FRAGMENT}"', f'source "{fragment}"')
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{tmp_path}/bin:{os.environ['PATH']}",
             "JTS_STUB_CALLS": str(tmp_path / "install.calls"), "JTS_STUB_FAIL": ""},
    )
    assert result.returncode == (1 if fault else 0), result.stderr
    _assert_no_rm_escaped(tmp_path)
    assert not (tmp_path / "txn").exists()
    assert unrelated.read_text() == new_tool.read_text() == "keep\n"
    if fault:
        assert old_tool.read_text() == "keep\n"
    else:
        assert not old_tool.parent.exists()
    if fault == "stage":
        assert unit.exists() is inherited
        if inherited:
            assert unit.read_text() == old_unit
            assert rule.read_text() == "inherited rule\n"
    else:
        assert unit.exists() is (inherited or profile == "full")
        if unit.exists():
            assert unit.read_bytes() == (ROOT / "deploy/systemd" / unit.name).read_bytes()
    assert rule.exists() is (inherited or (profile == "full" and fault != "stage"))


@pytest.mark.parametrize("profile", ["full", "streambox"])
@pytest.mark.parametrize("fault", ["network", "late", "verify", None])
@pytest.mark.parametrize("pending", [False, True])
def test_staging_faults_preserve_files_and_live_activation(tmp_path, profile, fault, pending):
    # Rewrite only host roots; all stage helpers and rollback run unchanged.
    fragment = tmp_path / "systemd-units.sh"
    source = FRAGMENT.read_text()
    for root in ("/etc/", "/usr/local/", "/var/lib/", "/sys/"):
        source = source.replace(root, f"{tmp_path}{root}")
    fragment.write_text(source)
    systemd = tmp_path / "systemd"
    retired = systemd / "jasper-wiim-remote-mic.service"
    link = systemd / "multi-user.target.wants/jasper-wiim-remote-mic.service"
    stale = systemd / "shairport-sync.service.d/jts-output.conf"
    helper = tmp_path / "usrlocalsbin/jasper-outputd-unpark"
    old_paths = [
        retired, stale, helper, systemd / "jasper-web.service",
        tmp_path / "etc/NetworkManager/system-connections/jts-usb.nmconnection",
        tmp_path / "etc/jasper/usbnet-dnsmasq.conf",
        tmp_path / "var/lib/jasper-usb-network/plan.json",
        tmp_path / "var/lib/jasper-usb-network/migration_pending",
    ]
    for path in old_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old generation\n")
        path.chmod(0o640)
    link.parent.mkdir(parents=True)
    link.symlink_to(retired)
    (tmp_path / "sys/class/net/usb0").mkdir(parents=True)
    for directory in ("usr/local/sbin", "usr/local/bin", "usr/local/lib/jasper"):
        (tmp_path / directory).mkdir(parents=True)
    late = (systemd / "jts-mic.slice" if profile == "full" else
            tmp_path / "etc/udev/rules.d/99-jasper-bluetooth-adapter.rules")
    shims = f'''
INSTALL_DIR="{tmp_path}/opt/jasper"
record_activation() {{
    local phase=staging
    [[ -d "{tmp_path}/txn" ]] || phase=committed
    echo "$phase $*" >> "{tmp_path}/activation.log"
}}
systemctl() {{
    record_activation systemctl "$@"
    [[ "$1" != list-unit-files ]] || echo "$2 enabled"
}}
nmcli() {{ record_activation nmcli "$@"; }}
udevadm() {{ record_activation udevadm "$@"; }}
validate_installed_systemd_units() {{ return {1 if fault == "verify" else 0}; }}
python3() {{
    local plan nm dnsmasq pending
    while (( $# )); do
        case "$1" in
            --plan) plan="$2"; shift ;;
            --nm) nm="$2"; shift ;;
            --dnsmasq) dnsmasq="$2"; shift ;;
            --pending) pending="$2"; shift ;;
        esac
        shift
    done
    echo new > "$plan"
    if {"true" if pending else "false"}; then
        echo deferred > "$pending"
    else
        echo new > "$nm"
        echo new > "$dnsmasq"
        rm -f "$pending"
    fi
    return {1 if fault == "network" else 0}
}}
'''
    script = _stage_rollback_harness(
        tmp_path, f"_stage_{profile}_unit_files", shims, "activate_staged_unit_files"
    ).replace(f'source "{FRAGMENT}"', f'source "{fragment}"')
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{tmp_path}/bin:{os.environ['PATH']}",
             "JTS_STUB_CALLS": str(tmp_path / "install.calls"),
             "JTS_STUB_FAIL": str(late) if fault == "late" else ""},
    )
    assert result.returncode == (1 if fault else 0), result.stderr
    _assert_no_rm_escaped(tmp_path)
    assert not (tmp_path / "txn").exists()
    copies = (tmp_path / "install.calls").read_text()
    if fault == "late":
        assert str(late) in copies
    if fault in ("verify", None):
        assert str(systemd / "jasper-headphone-monitor.service") in copies
    issued = (tmp_path / "activation.log").read_text().splitlines()
    mutations = [call for call in issued if not call.endswith("systemctl daemon-reload")]
    assert all(call.startswith("committed ") for call in mutations), issued
    if fault:
        assert not mutations
        for path in old_paths:
            assert path.read_text() == "old generation\n", path
            assert path.stat().st_mode & 0o777 == 0o640, path
        assert link.is_symlink() and link.readlink() == retired
        assert not (systemd / "jasper-input.service").exists()
        assert not (tmp_path / "etc/NetworkManager/conf.d/90-jasper-usbnet.conf").exists()
    else:
        assert not any(path.exists() or path.is_symlink() for path in (retired, link, stale, helper))
        assert (systemd / "jasper-input.service").is_file()
        assert {
            "committed systemctl disable --now jasper-wiim-remote-mic.service",
            "committed systemctl disable --now snapserver.service",
            "committed systemctl disable --now snapclient.service",
            "committed udevadm control --reload-rules",
        } <= set(mutations)
        nm_calls = [call for call in mutations if call.startswith("committed nmcli ")]
        assert bool(nm_calls) is not pending
        if not pending:
            assert nm_calls[-1] == "committed nmcli --wait 10 connection up jts-usb ifname usb0"


@pytest.mark.parametrize("verify_rc", [0, 1, 124])
def test_installed_unit_verification_commits_or_restores_the_generation(tmp_path, verify_rc):
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    names = {
        "jasper-camilla-crossover.service", "jasper-source-intent-reconcile.service",
        "jasper-fanin-coupling-auto.service", "jasper-web.socket", "jts-mic.slice",
        "jasper-custom.timer", "jasper-custom.path", "jasper-custom.target",
        "nginx.service",
    }
    for name in names:
        (systemd_dir / name).write_text("old generation\n")
    (systemd_dir / "masked.service").symlink_to("/dev/null")
    (systemd_dir / "unrelated.service").write_text("unrelated\n")
    (systemd_dir / "ignored.conf").write_text("unrelated\n")
    staged = tmp_path / "staged"
    staged.write_text("new generation\n")
    dropin = systemd_dir / "nginx.service.d"
    dropin.mkdir()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    analyzer = binary_dir / "systemd-analyze"
    analyzer.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/verify.args"\n'
        f'echo verify >> "{tmp_path}/calls.log"\n'
        f"exit {verify_rc}\n"
    )
    analyzer.chmod(0o755)
    script = f"""{_shim_preamble(tmp_path)}
{_transaction_recorder(tmp_path)}
udevadm() {{ :; }}
JASPER_CORE_AUDIO_GRAPH_INSTALL_ROWS=("0644 unused $SYSTEMD_DIR/jasper-camilla-crossover.service")
stage() {{
    local unit
    for unit in {" ".join(sorted(names - {"nginx.service"}))}; do
        install -m 0644 "{staged}" "$SYSTEMD_DIR/$unit"
    done
    install -m 0644 "{staged}" "$SYSTEMD_DIR/nginx.service.d/jts-recovery.conf"
}}
_with_unit_install_transaction stage
"""
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=20,
        env={**os.environ, "PATH": f"{binary_dir}:{os.environ['PATH']}"},
    )
    assert result.returncode == verify_rc, result.stderr
    assert (systemd_dir / "jasper-web.socket").read_text() == (
        "old generation\n" if verify_rc else "new generation\n"
    )
    args = (tmp_path / "verify.args").read_text().splitlines()
    assert args[0] == "verify"
    assert set(args[1:]) == names
    assert len(args[1:]) == len(names)
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert calls == ["systemctl daemon-reload", "verify", (
        "systemctl daemon-reload" if verify_rc else "fn clear_install_in_progress"
    )]
    assert not (tmp_path / "txn").exists()
    _assert_no_rm_escaped(tmp_path)


def _destination_harness(tmp_path: Path, function: str) -> str:
    calls = tmp_path / "destinations.log"
    return f"""{_shim_preamble(tmp_path)}
install_transaction_dir="{tmp_path}/txn"
mkdir -p "$install_transaction_dir"
install() {{
  local dst="${{!#}}"
  [[ "$1" == "-d" ]] || printf '%s\\t%s\\n' "${{@: -2:1}}" "$dst" >> "{calls}"
  return 0
}}
systemctl() {{ return 0; }}
install_usb_network_files() {{ return 0; }}
validate_streambox_web_socket() {{ return 0; }}
reload_audio_recovery_udev_rules_for_install() {{ return 0; }}
{function}
"""


def staged_file_copies(tmp_path: Path, function: str) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["bash", "-c", _destination_harness(tmp_path, function)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    _assert_no_rm_escaped(tmp_path)
    log = tmp_path / "destinations.log"
    return [tuple(line.split("\t", 1)) for line in log.read_text().splitlines()]


def _destinations(tmp_path: Path, function: str) -> set[str]:
    return {
        destination.replace(str(tmp_path), "")
        for _, destination in staged_file_copies(tmp_path, function)
    }


def test_only_the_contained_builder_policy_lands_in_the_install_lib_dir(tmp_path):
    landed = {
        Path(destination).name
        for destination in _destinations(tmp_path, "install_jasper_support_files")
        if destination.startswith("/usr/local/lib/jasper/install/")
    }
    assert landed == {"build-sandbox.sh"}
    assert landed < {path.name for path in installer_shell_paths()}
    support = _destinations(
        tmp_path / "support", "install_jasper_support_files"
    )
    assert {"/usr/local/lib/jasper/jasper-asound-render.sh"} <= support
    order = [line.split("\t", 1)[1] for line in (tmp_path / "support" / "destinations.log").read_text().splitlines()]
    assert order.index("/usr/local/sbin/jasper-wifi-guardian") < order.index(
        "/usr/local/lib/jasper/jasper-env-file.sh"
    )


def test_a_streambox_stages_a_subset_of_the_full_unit_generation(tmp_path):
    full = _destinations(tmp_path / "full", "_stage_full_unit_files")
    streambox = _destinations(tmp_path / "streambox", "_stage_streambox_unit_files")
    assert streambox
    assert streambox <= full, streambox - full
    assert {
        "/systemd/nginx.service.d/jts-recovery.conf",
        "/systemd/bluetooth.service.d/jts-timeout.conf",
    } <= streambox & full


@pytest.mark.parametrize("web_source", ["jasper-web", "jasper-web-streambox"])
def test_shared_web_units_and_recovery_dropins_install_exact_files(tmp_path, web_source):
    result = subprocess.run(
        ["bash", "-c", f'''set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path}"
source "{FRAGMENT}"
install_web_unit_files {web_source}
install_audio_slice_and_dropins
'''], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    for unit in ("jasper-web", "jasper-bluetooth-web", "jasper-correction-web",
                 "jasper-system-web", "jasper-chat-web"):
        source = web_source if unit == "jasper-web" else unit
        for extension in ("service", "socket"):
            assert (tmp_path / f"{unit}.{extension}").read_bytes() == (
                ROOT / "deploy" / f"{source}.{extension}"
            ).read_bytes()
    for relative in ("jts-audio.slice", "ssh.service.d/oom-protection.conf",
                     "nginx.service.d/jts-recovery.conf"):
        assert (tmp_path / relative).read_bytes() == (
            ROOT / "deploy/systemd" / relative
        ).read_bytes()
