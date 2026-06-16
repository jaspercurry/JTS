"""Install-profile guardrails after the endpoint tier was removed.

There are now exactly TWO install profiles — ``full`` and ``streambox``.
The legacy ``endpoint`` / ``satellite`` tokens are still ACCEPTED and map
to ``streambox`` so a field box with a persisted ``endpoint`` marker
auto-migrates on its next deploy instead of stranding. "Endpoint
behaviour" is now the multiroom follower runtime role.

These tests pin the NEW invariants:

1. ``normalize_install_profile`` maps endpoint/satellite -> streambox in
   BOTH Python and the bash mirror in deploy/install.sh; full/streambox
   pass through; bogus raises.
2. A persisted endpoint marker resolves to streambox (auto-migration)
   with NO implicit-tier-change error and NO accept flag.
3. The streambox dry-run plan includes the audio graph (fanin/outputd/
   camilla); the full plan includes voice.
4. install.sh has NO endpoint install functions, and the deleted endpoint
   artifacts do not exist.
5. A bonded follower parks the brain (cross-referenced with the multiroom
   reconcile tests).

The Zero-2-W -> streambox default and low-memory cargo build coverage is
preserved here.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.install_surface import installer_text


REPO_ROOT = Path(__file__).parent.parent
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-to-pi.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"


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


# ---------- (1) normalize maps endpoint/satellite -> streambox ----------


def test_python_normalize_maps_legacy_tokens_to_streambox():
    from jasper.install_profile import (
        VALID_INSTALL_PROFILES,
        install_profile_allows_voice_brain,
        install_role_for_profile,
        is_streambox_install_profile,
        normalize_install_profile,
    )

    assert VALID_INSTALL_PROFILES == frozenset({"full", "streambox"})
    assert normalize_install_profile("endpoint") == "streambox"
    assert normalize_install_profile("satellite") == "streambox"
    assert normalize_install_profile("streambox") == "streambox"
    assert normalize_install_profile("full") == "full"
    assert normalize_install_profile("") == "full"
    assert normalize_install_profile(None) == "full"

    # role == profile now; legacy tokens behave exactly like streambox.
    assert install_role_for_profile("endpoint") == "streambox"
    assert is_streambox_install_profile("satellite")
    assert not install_profile_allows_voice_brain("endpoint")
    assert not install_profile_allows_voice_brain("streambox")
    assert install_profile_allows_voice_brain("full")

    with pytest.raises(ValueError, match="invalid install profile"):
        normalize_install_profile("bogus")


def test_legacy_aliases_never_raise():
    from jasper.install_profile import normalize_install_profile

    # The whole point of the alias: a field box must never fail closed.
    for token in ("endpoint", "satellite"):
        assert normalize_install_profile(token) == "streambox"


def test_bash_normalize_maps_legacy_tokens_to_streambox():
    for token, expected in [
        ("endpoint", "streambox"),
        ("satellite", "streambox"),
        ("streambox", "streambox"),
        ("full", "full"),
        ("", "full"),
    ]:
        r = _run_install_helper(f"normalize_install_profile {shlex.quote(token)}")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == expected

    bogus = _run_install_helper("normalize_install_profile bogus")
    assert bogus.returncode == 2
    assert "use full or streambox" in bogus.stderr


def test_python_and_bash_normalize_agree_on_legacy_tokens():
    from jasper.install_profile import normalize_install_profile

    for token in ("", "full", "streambox", "endpoint", "satellite"):
        py = normalize_install_profile(token)
        bash = _run_install_helper(
            f"normalize_install_profile {shlex.quote(token)}"
        ).stdout.strip()
        assert py == bash, f"{token!r}: py={py} bash={bash}"


# ---------- (2) persisted endpoint marker auto-migrates to streambox -----


def test_persisted_endpoint_marker_resolves_to_streambox_without_error(
    tmp_path: Path,
):
    """A field box with a persisted endpoint marker auto-migrates: resolve
    returns streambox, rc=0, NO implicit-tier-change error, NO accept flag."""
    marker = tmp_path / "install_profile"
    marker.write_text("endpoint\n")

    read = _run_install_helper(
        "unset JASPER_INSTALL_PROFILE JASPER_ACCEPT_INSTALL_PROFILE_CHANGE; "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )

    assert read.returncode == 0, read.stderr
    assert read.stdout.strip() == "streambox"
    assert "install profile mismatch" not in read.stderr


def test_persisted_satellite_marker_resolves_to_streambox(tmp_path: Path):
    marker = tmp_path / "install_profile"
    marker.write_text("satellite\n")
    read = _run_install_helper(
        "unset JASPER_INSTALL_PROFILE JASPER_ACCEPT_INSTALL_PROFILE_CHANGE; "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )
    assert read.returncode == 0, read.stderr
    assert read.stdout.strip() == "streambox"


def test_endpoint_dry_run_produces_streambox_plan():
    """JASPER_INSTALL_PROFILE=endpoint must now produce the STREAMBOX plan,
    proving the alias drives the streambox install path."""
    endpoint = _run_install_plan(profile="endpoint")
    streambox = _run_install_plan(profile="streambox")
    satellite = _run_install_plan(profile="satellite")

    assert endpoint.returncode == 0, endpoint.stderr
    assert streambox.returncode == 0, streambox.stderr
    assert endpoint.stdout == streambox.stdout
    assert satellite.stdout == streambox.stdout
    assert endpoint.stdout.startswith("==> JTS streambox install plan (dry run)\n")


def test_legacy_marker_migration_logs_observable_line(tmp_path: Path):
    marker = tmp_path / "install_profile"
    marker.write_text("endpoint\n")
    r = _run_install_helper(
        f"install_profile_legacy_marker_migrating {shlex.quote(str(marker))} "
        "&& echo MIGRATING || echo STEADY"
    )
    assert r.returncode == 0, r.stderr
    assert "MIGRATING" in r.stdout

    marker.write_text("streambox\n")
    r2 = _run_install_helper(
        f"install_profile_legacy_marker_migrating {shlex.quote(str(marker))} "
        "&& echo MIGRATING || echo STEADY"
    )
    assert "STEADY" in r2.stdout


def test_persisted_install_profile_rewrites_legacy_marker(tmp_path: Path):
    """persist_install_profile normalizes, so a re-persist of an endpoint
    token writes streambox to disk."""
    marker = tmp_path / "install_profile"
    write = _run_install_helper(
        f"persist_install_profile endpoint {shlex.quote(str(marker))}"
    )
    assert write.returncode == 0, write.stderr
    assert marker.read_text().strip() == "streambox"


def test_genuine_full_to_streambox_change_still_errors(tmp_path: Path):
    """A REAL tier change (persisted full, requested streambox) still
    fails closed — only the legacy-alias path auto-migrates."""
    marker = tmp_path / "install_profile"
    marker.write_text("full\n")
    r = _run_install_helper(
        "unset JASPER_ACCEPT_INSTALL_PROFILE_CHANGE; "
        "JASPER_INSTALL_PROFILE=streambox "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )
    assert r.returncode == 2
    assert "install profile mismatch" in r.stderr


# ---------- (3) streambox plan has audio graph; full plan has voice ------


def test_full_install_plan_is_unchanged_when_profile_is_unset():
    unset = _run_install_plan()
    explicit_full = _run_install_plan(profile="full")

    assert unset.returncode == 0, unset.stderr
    assert explicit_full.returncode == 0, explicit_full.stderr
    assert unset.stdout == explicit_full.stdout
    assert unset.stdout.startswith("==> JTS install plan (dry run)\n")


def test_streambox_plan_includes_audio_graph_not_voice_brain():
    result = _run_install_plan(profile="streambox")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("==> JTS streambox install plan (dry run)\n")
    for expected in [
        "Resolve JASPER_INSTALL_PROFILE=streambox",
        "jasper-fanin Rust daemon",
        "jasper-outputd daemon",
        "CamillaDSP:",
        "AirPlay, Spotify Connect, Bluetooth, and USB Audio Input",
        "voice, wake-word, mic/AEC",  # listed as out-of-scope
    ]:
        assert expected in result.stdout, expected
    for forbidden in [
        "openWakeWord ONNX assets",
        "jasper-aec3",
    ]:
        assert forbidden not in result.stdout, forbidden


def test_full_plan_includes_voice_and_audio_graph():
    result = _run_install_plan(profile="full")

    assert result.returncode == 0, result.stderr
    for expected in [
        "CamillaDSP:",
        "jasper-fanin Rust daemon",
        "jasper-outputd daemon",
        "voice_provider_ids",  # voice brain wiring present in full plan
        "openWakeWord ONNX assets",
    ]:
        assert expected in result.stdout, expected


def test_pyproject_base_install_stays_minimal():
    data = tomllib.loads(PYPROJECT.read_text())
    base = data["project"]["dependencies"]
    full = data["project"]["optional-dependencies"]["full"]
    streambox = data["project"]["optional-dependencies"]["streambox"]

    assert base == ["sdnotify>=0.3.2"]
    for dep_prefix in ["camilladsp", "google-genai", "openai", "onnxruntime"]:
        assert not any(dep.startswith(dep_prefix) for dep in base)
        assert any(dep.startswith(dep_prefix) for dep in full)
    # streambox stays voice-brain-light.
    for voice_only in ["google-genai", "openai", "onnxruntime"]:
        assert not any(dep.startswith(voice_only) for dep in streambox)


# ---------- (4) no endpoint install functions / deleted artifacts --------


def test_installer_has_no_endpoint_install_functions():
    text = installer_text()
    for gone in [
        "install_endpoint_deps",
        "install_endpoint_jasper",
        "install_endpoint_systemd_units",
        "install_endpoint_nginx_site",
        "render_endpoint_unit",
        "validate_endpoint_unit_file",
        "validate_endpoint_systemd_units",
        "print_endpoint_install_plan",
        "install_profile_auto_streambox_upgrade_active",
    ]:
        assert f"{gone}() {{" not in text, gone
        # Also not referenced (called) anywhere in the install surface.
        assert gone not in text, gone


def test_deleted_endpoint_artifacts_do_not_exist():
    assert not (REPO_ROOT / "deploy" / "nginx-jasper-endpoint.conf").exists()
    assert not (REPO_ROOT / "scripts" / "bringup-endpoint.sh").exists()


def test_main_dispatch_has_no_endpoint_branch():
    text = INSTALL_SH.read_text()
    assert '"${install_profile}" == "endpoint"' not in text
    # Only full + streambox dispatch branches remain.
    assert '"${install_profile}" == "streambox"' in text


def test_deploy_script_accepts_full_and_streambox_only():
    text = DEPLOY_SH.read_text()
    assert "full|streambox)" in text
    assert "full|streambox|endpoint)" not in text
    # The bespoke endpoint verification path is gone.
    assert 'REMOTE_INSTALL_PROFILE" == "endpoint"' not in text


def test_streambox_parking_disables_brain_units():
    """The streambox->paired-follower conversion still parks brain units
    (cross-reference for the follower-parking invariant)."""
    text = installer_text()
    parking = text.split("park_streambox_brain_units() {", 1)[1].split("\n}", 1)[0]
    assert "jasper-voice.service" in parking
    assert "systemctl disable --now" in parking


# ---------- Zero-2-W default + low-memory cargo build (preserved) --------


def test_fresh_zero2w_defaults_to_streambox(tmp_path: Path):
    marker = tmp_path / "missing_profile_marker"
    model = tmp_path / "model"
    model.write_bytes(b"Raspberry Pi Zero 2 W Rev 1.0\x00")

    result = _run_install_helper(
        "unset JASPER_INSTALL_PROFILE; "
        f"JASPER_PI_MODEL_FILE={shlex.quote(str(model))} "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "streambox"


def test_unknown_hardware_defaults_to_full(tmp_path: Path):
    marker = tmp_path / "missing_profile_marker"
    model = tmp_path / "model"
    model.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\x00")

    result = _run_install_helper(
        "unset JASPER_INSTALL_PROFILE; "
        f"JASPER_PI_MODEL_FILE={shlex.quote(str(model))} "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "full"


def test_zero_class_rust_build_uses_low_memory_cargo_profile(tmp_path: Path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:         425984 kB\n")

    detected = _run_install_helper(
        f"JASPER_RUST_MEMINFO_FILE={shlex.quote(str(meminfo))} "
        "rust_low_memory_build_enabled"
    )
    env = _run_install_helper(
        f"JASPER_RUST_MEMINFO_FILE={shlex.quote(str(meminfo))} "
        "rust_cargo_build_env"
    )

    assert detected.returncode == 0, detected.stderr
    assert "CARGO_BUILD_JOBS=1" in env.stdout
    assert "CARGO_PROFILE_RELEASE_LTO=false" in env.stdout
    assert "CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16" in env.stdout
    assert "CARGO_PROFILE_RELEASE_OPT_LEVEL=2" in env.stdout


def test_full_speaker_rust_build_keeps_release_profile(tmp_path: Path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        1014768 kB\n")

    detected = _run_install_helper(
        f"JASPER_RUST_MEMINFO_FILE={shlex.quote(str(meminfo))} "
        "rust_low_memory_build_enabled"
    )
    env = _run_install_helper(
        f"JASPER_RUST_MEMINFO_FILE={shlex.quote(str(meminfo))} "
        "rust_cargo_build_env"
    )

    assert detected.returncode == 1
    assert env.returncode == 0, env.stderr
    assert env.stdout == ""


def test_rust_low_memory_build_can_be_forced_for_recovery(tmp_path: Path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        1014768 kB\n")

    forced = _run_install_helper(
        f"JASPER_RUST_MEMINFO_FILE={shlex.quote(str(meminfo))} "
        "JASPER_RUST_LOW_MEMORY_BUILD=1 rust_low_memory_build_enabled"
    )

    assert forced.returncode == 0, forced.stderr


# ---------- (5) follower parks the brain — pointer to the canonical test --


def test_follower_parks_renderer_stack_via_reconcile_plan():
    """Cross-reference: the dumb-follower runtime role (which provides the
    old "endpoint" behaviour) parks the renderer stack. The exhaustive
    per-unit coverage lives in tests/test_multiroom_reconcile.py."""
    from jasper.multiroom.config import (
        DEFAULT_BUFFER_MS,
        DEFAULT_CODEC,
        GroupingConfig,
    )
    from jasper.multiroom.reconcile import FOLLOWER_PARKED_UNITS, plan

    cfg = GroupingConfig(
        enabled=True, role="follower", channel="left",
        bond_id="bond-1", leader_addr="jts.local",
        buffer_ms=DEFAULT_BUFFER_MS, codec=DEFAULT_CODEC, error=None,
    )
    by_unit = {i.unit: i.desired for i in plan(cfg).intents}
    for unit in FOLLOWER_PARKED_UNITS:
        assert by_unit.get(unit) == "stop", unit
