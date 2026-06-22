# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard: every install step in main() is described by the dry run.

`bash deploy/install.sh --dry-run` is contributor-facing safety gear
(AGENTS.md: "Previewing install blast radius") — its whole value is that
the printed plan matches what the real installer does. print_install_plan
is hand-written prose, so a new step appended to main() silently vanishes
from the plan unless something checks. This test is that check: it parses
main()'s body for calls to functions defined in install.sh or in its
sourced deploy/lib/install/*.sh libraries (the function-group extraction
moved several step definitions there; main() still calls them), then
asserts each one is represented by a marker phrase in the actual `--dry-run`
output (run through bash, not regexed out of the source, so EOF-heredoc
or flag-handling breakage also fails here).

Maintenance contract, enforced by the meta-assertions below:
  * add a step to main()  -> add a marker (or an allowlist entry with a
    reason) or this test fails naming the step;
  * remove/rename a step  -> the stale mapping entry fails loudly too,
    so the table can't accumulate dead rows.

Markers are matched against whitespace-normalized plan text because the
plan hard-wraps at ~72 columns mid-phrase ("memory\\n     resilience").
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_INSTALL_SH = Path(__file__).parent.parent / "deploy" / "install.sh"
# Function-group libraries sourced by install.sh; step functions called
# from main() may be defined here instead of in install.sh itself.
_INSTALL_LIB_DIR = _INSTALL_SH.parent / "lib" / "install"

# step function called in main() -> phrase that must appear in the
# --dry-run plan output (after whitespace normalization).
_STEP_TO_PLAN_MARKER = {
    "create_jasper_service_users": "non-root service users",
    "install_deps": "apt-get update",
    "persist_install_profile": "Persist the install profile tier",
    "install_streambox_deps": "renderer/DSP stack",
    "install_streambox_jasper": "Python runtime dependencies from pyproject.toml [streambox]",
    "migrate_secrets_phase4b": "Move HA/Spotify integration secrets into /var/lib/jasper-intsecrets",
    "install_streambox_systemd_units": "Enable socket-activated streambox-safe web surfaces",
    "install_streambox_nginx_site": "streambox nginx",
    "install_alsa": "Render /etc/asound.conf through",
    "install_camilladsp": "CamillaDSP:",
    "ensure_outputd_camilla_statefile": "Seed or validate the outputd Camilla statefile",
    "install_renderers": "shairport-sync source archive",
    "set_usb_gadget_mode": "USB gadget dtoverlay",
    "tune_wifi_for_airplay": "Disable WiFi power-save on the active wlan0",
    "install_jasper": "Copy Python source",
    "build_install_jasper_fanin": "jasper-fanin Rust daemon",
    "build_install_jasper_outputd": "jasper-outputd daemon from rust/jasper-outputd",
    "install_systemd_units": "Enable socket-activated setup wizards",
    "retire_audio_topology_switch": "stale legacy audio-topology state",
    "migrate_wifi_guardian": "WiFi guardian recovery",
    "migrate_memory_resilience": "memory resilience",
    "migrate_cgroup_memory_enabled": "memory cgroup/PSI kernel args",
    "install_journald_persistent_storage": "journald persistence",
    "install_avahi_jasper_control": "Avahi service templates",
    "install_jasper_control_polkit": "49-jasper-control.rules",
    "install_jasper_web_polkit": "49-jasper-web.rules",
    "widen_jasper_web_writable_dirs": "generated sound profiles",
    "widen_control_secret_env_modes": "Widen the config/state files",
    "install_peering_template": "peer_id",
    "remove_legacy_https_artifacts": "legacy self-signed HTTPS artifacts",
    "provision_correction_tls": "correction TLS CA/cert files",
    "install_nginx_site": "nginx config",
    "install_camillagui": "CamillaGUI",
    "regenerate_audio_cues": "Regenerate audio cues",
    "write_build_manifest": "build.txt verified-install marker",
    "run_doctor_summary": "jasper-doctor as a final non-blocking health summary",
}

# Steps with no host blast radius — nothing for the plan to describe.
# Keep this list short and justified; a mutating step never belongs here.
_PLAN_EXEMPT = {
    # Read-only preflights: they check (root, the 'pi' build user) and
    # abort before any mutation; the plan's preamble already notes the
    # run-for-real sudo requirement.
    "require_root",
    "require_build_user",
    # The plan/usage printers themselves (the --dry-run / --help paths).
    "print_install_plan",
    "print_install_usage",
}


def _main_body() -> str:
    text = _INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(r"\nmain\(\) \{\n(.*?)\n\}", text, re.DOTALL)
    assert match is not None, "could not locate main() in install.sh"
    return match.group(1)


def _defined_functions() -> set[str]:
    sources = [_INSTALL_SH, *sorted(_INSTALL_LIB_DIR.glob("*.sh"))]
    functions: set[str] = set()
    for path in sources:
        text = path.read_text(encoding="utf-8")
        functions |= set(
            re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{", text, re.MULTILINE)
        )
    return functions


def _steps_called_in_main() -> list[str]:
    """Function calls in main(), in order: lines whose first token (after
    stripping trailing comments) names a function defined in install.sh
    or its sourced deploy/lib/install/*.sh libraries. Bash keywords /
    builtins / helpers used inside conditions (`if _is_truthy ...`) are
    not first tokens, so they don't register."""
    functions = _defined_functions()
    steps = []
    for line in _main_body().splitlines():
        code = line.split("#", 1)[0].strip()
        if not code:
            continue
        first = code.split()[0]
        if first in functions:
            steps.append(first)
    return steps


def _dry_run_plan_normalized(*, profile: str | None = None) -> str:
    env = os.environ.copy()
    if profile is None:
        env.pop("JASPER_INSTALL_PROFILE", None)
    else:
        env["JASPER_INSTALL_PROFILE"] = profile
    result = subprocess.run(
        ["bash", str(_INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return " ".join(result.stdout.split())


def test_main_steps_were_parsed():
    """Meta-check: the parser actually sees main()'s pipeline. If this
    shrinks below the install's real shape, the guard is vacuous."""
    steps = _steps_called_in_main()
    assert len(steps) >= 20, steps
    assert "install_deps" in steps
    assert "run_doctor_summary" in steps


def test_mapping_has_no_stale_or_overlapping_entries():
    """Every mapping/exemption row corresponds to a live main() step,
    and no step is both mapped and exempt."""
    steps = set(_steps_called_in_main())
    stale = (set(_STEP_TO_PLAN_MARKER) | _PLAN_EXEMPT) - steps
    assert not stale, f"mapping rows for steps no longer called in main(): {sorted(stale)}"
    overlap = set(_STEP_TO_PLAN_MARKER) & _PLAN_EXEMPT
    assert not overlap, f"steps both mapped and exempt: {sorted(overlap)}"


def test_every_main_step_is_described_by_the_dry_run_plan():
    """The ratchet: a step added to main() must land in the plan text
    (add a marker here) or be explicitly exempted with a reason."""
    plans = (
        _dry_run_plan_normalized(),
        _dry_run_plan_normalized(profile="streambox"),
    )
    missing_mapping = []
    missing_marker = []
    for step in _steps_called_in_main():
        if step in _PLAN_EXEMPT:
            continue
        marker = _STEP_TO_PLAN_MARKER.get(step)
        if marker is None:
            missing_mapping.append(step)
        elif not any(marker in plan for plan in plans):
            missing_marker.append(
                f"{step}: marker {marker!r} not in full or streambox plan output"
            )
    assert not missing_mapping, (
        "main() steps with no plan marker mapping (describe them in "
        "print_install_plan and add a marker here, or add a justified "
        f"_PLAN_EXEMPT entry): {missing_mapping}"
    )
    assert not missing_marker, "\n".join(missing_marker)
