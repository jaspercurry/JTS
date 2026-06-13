"""Endpoint install-tier guardrails.

The Zero 2 W endpoint plan deliberately keeps one JTS package and splits
by install profile instead of creating a second endpoint codebase. These
tests are the cheap walls that make that decision safe.
"""
from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.install_surface import installer_text


REPO_ROOT = Path(__file__).parent.parent
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-to-pi.sh"
BRINGUP_SH = REPO_ROOT / "scripts" / "bringup-endpoint.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"
SYSTEM_STATUS_ACTIONS_JS = (
    REPO_ROOT / "deploy" / "assets" / "system-status" / "js" / "actions.js"
)
SYSTEM_STATUS_VIEWS_JS = (
    REPO_ROOT / "deploy" / "assets" / "system-status" / "js" / "views.js"
)


def _run_install_plan(*, profile: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if profile is not None:
        env["JASPER_INSTALL_PROFILE"] = profile
    else:
        env.pop("JASPER_INSTALL_PROFILE", None)
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )


def _run_install_helper(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(INSTALL_SH))} >/dev/null && {script}"],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_endpoint_import_surface_avoids_brain_only_dependencies():
    """Endpoint-tier modules must import without voice/DSP extras.

    CI has the full environment installed, so simulate the endpoint tier
    with a meta-path blocker that fails if a brain-only dependency is
    imported while loading the modules the endpoint runs.
    """
    modules = [
        "jasper.control.server",
        "jasper.cli.doctor",
        "jasper.multiroom.config",
        "jasper.multiroom.reconcile",
        "jasper.multiroom.state",
        "jasper.web.sources_setup",
        "jasper.web.system_setup",
    ]
    blocked = [
        "camilladsp",
        "dbus_next",
        "evdev",
        "onnxruntime",
        "openwakeword",
        "scipy",
        "sounddevice",
        "jasper_aec3",
        "google.genai",
        "openai",
        "spotipy",
        "websockets",
        "zeroconf",
    ]
    code = f"""
import importlib
import importlib.abc
import sys

blocked = {blocked!r}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        for name in blocked:
            if fullname == name or fullname.startswith(name + "."):
                raise ImportError(f"blocked endpoint-tier dependency: {{fullname}}")
        return None

sys.meta_path.insert(0, Blocker())
for module in {modules!r}:
    importlib.import_module(module)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, result.stderr


def test_full_install_plan_is_unchanged_when_profile_is_unset():
    """Endpoint work must not leak into the normal speaker dry-run path."""
    unset = _run_install_plan()
    explicit_full = _run_install_plan(profile="full")

    assert unset.returncode == 0, unset.stderr
    assert explicit_full.returncode == 0, explicit_full.stderr
    assert unset.stdout == explicit_full.stdout
    assert unset.stdout.startswith("==> JTS install plan (dry run)\n")


def test_endpoint_marker_maps_to_satellite_role():
    from jasper.install_profile import (
        install_profile_allows_local_sources,
        install_role_for_profile,
        is_satellite_install_profile,
    )

    assert install_role_for_profile("endpoint") == "satellite"
    assert install_role_for_profile("satellite") == "satellite"
    assert is_satellite_install_profile("endpoint")
    assert is_satellite_install_profile("satellite")
    assert not install_profile_allows_local_sources("endpoint")
    assert not install_profile_allows_local_sources("satellite")
    assert install_profile_allows_local_sources("full")
    with pytest.raises(ValueError, match="invalid install profile"):
        install_profile_allows_local_sources("streambox")


def test_endpoint_install_plan_excludes_brain_build_surfaces():
    result = _run_install_plan(profile="endpoint")
    satellite_alias = _run_install_plan(profile="satellite")

    assert result.returncode == 0, result.stderr
    assert satellite_alias.returncode == 0, satellite_alias.stderr
    assert satellite_alias.stdout == result.stdout
    assert result.stdout.startswith("==> JTS endpoint install plan (dry run)\n")
    for expected in [
        "Resolve JASPER_INSTALL_PROFILE=endpoint",
        "Persist the install profile tier",
        "Minimal runtime packages",
        "jasper-control",
        "managed JTS snapserver/snapclient units",
        "endpoint-scoped nginx",
        "socket-activated endpoint web for /system/ and /sources/",
        "memory and cgroup tuning",
        "voice/wake/DSP",
    ]:
        assert expected in result.stdout
    for forbidden in [
        "cargo build --release --locked",
        "jasper-fanin Rust daemon",
        "jasper-outputd daemon",
        "shairport-sync source archive",
        "Raspotify/librespot deb",
        "CamillaDSP:",
        "openWakeWord ONNX assets",
        "Enable socket-activated setup wizards",
    ]:
        assert forbidden not in result.stdout


def test_pyproject_base_install_is_endpoint_light():
    data = tomllib.loads(PYPROJECT.read_text())
    base = data["project"]["dependencies"]
    full = data["project"]["optional-dependencies"]["full"]

    assert base == ["sdnotify>=0.3.2"]
    for dep_prefix in [
        "camilladsp",
        "google-genai",
        "openai",
        "onnxruntime",
        "scipy",
        "sounddevice",
        "spotipy",
        "zeroconf",
    ]:
        assert not any(dep.startswith(dep_prefix) for dep in base)
        assert any(dep.startswith(dep_prefix) for dep in full)


def test_install_profile_marker_is_reused_when_env_is_unset(tmp_path: Path):
    marker = tmp_path / "install_profile"

    write = _run_install_helper(
        f"persist_install_profile endpoint {shlex.quote(str(marker))}"
    )
    read = _run_install_helper(
        f"unset JASPER_INSTALL_PROFILE && resolve_install_profile {shlex.quote(str(marker))}"
    )

    assert write.returncode == 0, write.stderr
    assert read.returncode == 0, read.stderr
    assert read.stdout.strip() == "endpoint"


def test_install_profile_refuses_implicit_tier_change(tmp_path: Path):
    marker = tmp_path / "install_profile"
    setup = _run_install_helper(
        f"persist_install_profile endpoint {shlex.quote(str(marker))}"
    )
    change = _run_install_helper(
        "JASPER_INSTALL_PROFILE=full "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )
    override = _run_install_helper(
        "JASPER_INSTALL_PROFILE=full JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=1 "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )

    assert setup.returncode == 0, setup.stderr
    assert change.returncode == 2
    assert "install profile mismatch" in change.stderr
    assert override.returncode == 0, override.stderr
    assert override.stdout.strip() == "full"


def test_live_endpoint_profile_has_real_installer_branch():
    text = INSTALL_SH.read_text()

    assert "endpoint is not implemented for live installs yet" not in text
    assert "install_endpoint_deps" in text
    assert "install_endpoint_jasper" in text
    assert "install_endpoint_systemd_units" in text
    assert "install_endpoint_nginx_site" in text
    assert '"${install_profile}" == "endpoint"' in text


def test_endpoint_nginx_site_exposes_only_endpoint_safe_routes():
    conf = (REPO_ROOT / "deploy/nginx-jasper-endpoint.conf").read_text()

    for expected in [
        "location = /",
        "location /assets/",
        "location /sources/",
        "location /system/",
        "location /volume",
        "location = /grouping",
        "location /debug",
    ]:
        assert expected in conf

    for forbidden in [
        "location /voice/",
        "location /sound/",
        "location /spotify/",
        "location /correction/",
        "location /wake/",
        "location /bluetooth/",
        "location /rooms/",
    ]:
        assert forbidden not in conf


def test_system_dashboard_honors_endpoint_capabilities():
    views = SYSTEM_STATUS_VIEWS_JS.read_text()
    actions = SYSTEM_STATUS_ACTIONS_JS.read_text()

    assert "snap.system_capabilities" in views
    assert "applySystemCapabilities" in views
    assert "restart_voice" in views
    assert "restart_audio" in views
    assert "audio_quality" in views
    assert "audio_quality === false" in actions


def test_endpoint_profile_installs_endpoint_web_not_combined_bundle():
    install_sh = INSTALL_SH.read_text()
    text = installer_text()
    endpoint_block = install_sh.split(
        'if [[ "${install_profile}" == "endpoint" ]]; then', 1,
    )[1].split("return 0", 1)[0]

    assert "install_endpoint_nginx_site" in endpoint_block
    assert "jasper-system-web.socket jasper-sources-web.socket" in text
    assert "jasper-web.socket" in text
    assert "combined full-speaker jasper-web bundle disabled" in text


def test_full_profile_disables_endpoint_sources_socket_before_combined_web():
    text = installer_text()
    full_systemd = text.split("install_systemd_units() {", 1)[1].split(
        "\n}",
        1,
    )[0]
    disable = (
        "systemctl disable --now jasper-sources-web.socket "
        "jasper-sources-web.service"
    )

    assert disable in full_systemd
    disable_block = full_systemd.split(disable, 1)[1].split("# Migrate", 1)[0]
    assert ">/dev/null 2>&1 || true" in disable_block
    assert full_systemd.index("systemctl daemon-reload") < full_systemd.index(
        disable
    )
    assert full_systemd.index(disable) < full_systemd.index("# Migrate")


def test_deploy_script_forwards_endpoint_profile_and_verifies_management_surface():
    text = DEPLOY_SH.read_text()

    assert "JASPER_INSTALL_PROFILE=$(shell_quote" in text
    assert "JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=$(shell_quote" in text
    assert "REMOTE_INSTALL_PROFILE" in text
    assert "cat /var/lib/jasper/install_profile' \\" in text
    assert "cat /var/lib/jasper/install_profile 2>/dev/null || echo full" not in text
    assert "invalid installed profile" in text
    assert "systemctl restart jasper-grouping-reconcile.service" in text
    assert "http://127.0.0.1:8780/healthz" in text
    assert "jasper-aec-reconcile.service" in text
    assert "endpoint management surface" in text
    assert "/system/data.json" in text
    assert "/sources/state" in text
    assert "endpoint probes failed" in text


def test_endpoint_nginx_config_failure_is_fatal():
    text = INSTALL_SH.read_text()
    endpoint_nginx = text.split("install_endpoint_nginx_site() {", 1)[1].split(
        "\n}",
        1,
    )[0]

    assert "ERROR: endpoint nginx config test failed" in endpoint_nginx
    assert "return 1" in endpoint_nginx


def test_endpoint_install_profile_seeds_wifi_guardian_stash():
    text = INSTALL_SH.read_text()
    endpoint_block = text.split(
        'if [[ "${install_profile}" == "endpoint" ]]; then', 1,
    )[1].split("return 0", 1)[0]

    assert "migrate_wifi_guardian" in endpoint_block
    assert endpoint_block.index("migrate_wifi_guardian") < endpoint_block.index(
        "run_doctor_summary"
    )


def test_endpoint_bringup_script_is_syntax_valid():
    result = subprocess.run(
        ["bash", "-n", str(BRINGUP_SH)],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_endpoint_bringup_script_wraps_paved_path():
    text = BRINGUP_SH.read_text()

    assert 'bash "${SCRIPT_DIR}/onboard.sh" "${onboard_args[@]}"' in text
    assert 'onboard_args=("$HOST" "--no-install")' in text
    assert "JASPER_INSTALL_PROFILE=endpoint" in text
    assert 'bash "${SCRIPT_DIR}/deploy-to-pi.sh"' in text
    assert "--no-reboot" in text
    assert "/sys/fs/cgroup/cgroup.controllers" in text
    assert "/sys/block/zram0/disksize" in text
    assert "http://127.0.0.1:8780/healthz" in text
    assert "snapclient --version" in text
    assert "aplay -l" in text
    assert "wifi_guardian.env" in text
    assert "audio test: not run automatically" in text


def test_endpoint_bringup_doctor_summary_python_is_valid():
    text = BRINGUP_SH.read_text()
    match = re.search(
        r"summarize_doctor_json\(\) \{\n    python3 -c '\n(.*?)\n'\n\}",
        text,
        re.DOTALL,
    )
    assert match is not None
    payload = {
        "fails": 1,
        "warns": 1,
        "results": [
            {"name": "a", "status": "ok", "detail": "fine"},
            {"name": "b", "status": "warn", "detail": "watch"},
            {"name": "c", "status": "fail", "detail": "broken"},
        ],
    }
    result = subprocess.run(
        [sys.executable, "-c", match.group(1)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert "doctor: 1 failed / 1 warnings" in result.stdout
    assert "[warn] b: watch" in result.stdout
    assert "[fail] c: broken" in result.stdout


def test_cgroup_memory_migration_removes_disable_token(tmp_path: Path):
    """Fresh Pi images can ship cgroup_disable=memory in cmdline.txt.

    The enable tokens alone did not win on the real Zero 2 W endpoint, so
    the migration must remove the conflicting disable token.
    """
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_text(
        "console=tty1 root=PARTUUID=abc cgroup_disable=memory "
        "cfg80211.ieee80211_regdom=US\n"
    )

    result = _run_install_helper(
        f"JTS_BOOT_CMDLINE_FILE={shlex.quote(str(cmdline))} "
        "migrate_cgroup_memory_enabled >/dev/null && "
        f"cat {shlex.quote(str(cmdline))}"
    )

    assert result.returncode == 0, result.stderr
    tokens = result.stdout.split()
    assert "cgroup_disable=memory" not in tokens
    assert "cgroup_enable=memory" in tokens
    assert "cgroup_memory=1" in tokens
    assert "psi=1" in tokens
    assert tokens.count("cgroup_enable=memory") == 1


def test_zram_target_uses_actual_memtotal_not_one_gb_constant():
    result = _run_install_helper("_compute_target_zram_bytes 426000")

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == 426000 * 1024 // 2


def test_endpoint_rendered_snap_units_drop_empty_full_speaker_dependencies(tmp_path: Path):
    rendered = tmp_path / "jasper-snapclient.service"
    result = _run_install_helper(
        "render_endpoint_unit "
        f"{shlex.quote(str(REPO_ROOT / 'deploy/systemd/jasper-snapclient.service'))} "
        f"{shlex.quote(str(rendered))}"
    )

    assert result.returncode == 0, result.stderr
    text = rendered.read_text()
    assert "jasper-camilla.service" not in text
    assert "jasper-fanin.service" not in text
    assert "Wants=" not in text
    assert "After= sound.target" not in text
    assert "After=sound.target" in text or "After=network-online.target" in text


def test_endpoint_unit_validator_rejects_empty_dependency_directive(tmp_path: Path):
    unit = tmp_path / "bad.service"
    unit.write_text("[Unit]\nWants=\n[Service]\nExecStart=/bin/true\n")
    result = _run_install_helper(
        f"validate_endpoint_unit_file {shlex.quote(str(unit))}"
    )

    assert result.returncode == 1
    assert "empty dependency directive" in result.stderr


def test_endpoint_systemd_verify_skips_absent_snapserver_binary():
    text = installer_text()

    assert 'if [[ -x /usr/bin/snapserver ]]' in text
    assert 'verify_units+=("${SYSTEMD_DIR}/jasper-snapserver.service")' in text
