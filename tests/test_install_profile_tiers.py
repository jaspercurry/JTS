# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
import re
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
    assert install_profile_allows_voice_brain("endpoint")
    assert install_profile_allows_voice_brain("streambox")
    assert install_profile_allows_voice_brain("full")

    with pytest.raises(ValueError, match="invalid install profile"):
        normalize_install_profile("bogus")


def test_legacy_aliases_never_raise():
    from jasper.install_profile import normalize_install_profile

    # The whole point of the alias: a field box must never fail closed.
    for token in ("endpoint", "satellite"):
        assert normalize_install_profile(token) == "streambox"


def test_system_capabilities_map_per_profile():
    # The capability map is the single source of truth shared by the runtime
    # /system snapshot and the install-time landing-page bake; both derive the
    # boolean caps from the normalized role, so baked and live always agree.
    from jasper.install_profile import system_capabilities_for_profile as caps

    full = caps("full")
    streambox = caps("streambox")
    # Both tiers hold the voice brain now; only full has developer tools.
    # Both run the local audio graph (sources + DSP) and keep the
    # management surfaces.
    assert full["voice_brain"] is True and full["developer_tools"] is True
    assert streambox["voice_brain"] is True
    assert streambox["developer_tools"] is False
    for k in ("local_sources", "content_dsp"):
        assert full[k] is True and streambox[k] is True
    for k in ("network_settings", "speaker_settings", "pair_management", "reboot"):
        assert full[k] is True and streambox[k] is True

    # A legacy token passed DIRECTLY is echoed in install_profile while role +
    # booleans normalize to streambox. (In production read_install_profile
    # normalizes first, so /system and the baked page show streambox; this
    # pins the function's pass-through contract, not the live field value.)
    legacy = caps("endpoint")
    assert legacy["install_profile"] == "endpoint"
    assert legacy["role"] == "streambox"
    assert legacy["voice_brain"] is True and legacy["local_sources"] is True


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
        "wake-word, local microphone, or AEC",  # listed as out-of-scope
        # The assistant IS in scope, owned by the accessory reconciler.
        # See docs/adr/0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md
        "jasper-accessory-reconcile starts and stops jasper-voice",
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


def _distribution_name(requirement: str) -> str:
    """Distribution name of a PEP 508 requirement — the text before any
    version specifier, environment marker, or direct-reference URL."""
    return re.split(r"[<>=!;@ ]", requirement, maxsplit=1)[0].strip()


def test_pyproject_base_install_stays_minimal():
    data = tomllib.loads(PYPROJECT.read_text())
    base = data["project"]["dependencies"]
    full = data["project"]["optional-dependencies"]["full"]
    streambox = data["project"]["optional-dependencies"]["streambox"]

    assert base == ["sdnotify>=0.3.2"]
    for dep_prefix in ["camilladsp", "google-genai", "openai", "onnxruntime"]:
        assert not any(dep.startswith(dep_prefix) for dep in base)
        assert any(dep.startswith(dep_prefix) for dep in full)
    # A push-to-talk-only streambox runs the assistant, so it carries the
    # provider/tool SDKs; it never builds a wake detector or talks to the
    # XVF3800, so those runtimes stay full-only. pyalsaaudio is NOT one of
    # them: a streambox installs jasper-usbsink-volume.service, whose bridge
    # reads the gadget mixer through it.
    # See docs/adr/0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md
    assert set(streambox) < set(full)
    assert {_distribution_name(dep) for dep in set(full) - set(streambox)} == {
        "onnxruntime",
        "pyusb",
        "libusb_package",
    }


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
    assert "http://127.0.0.1/sound/setup/" in text
    assert "http://127.0.0.1/sound/ || echo 000" not in text


def test_streambox_parking_disables_brain_units():
    """The streambox->paired-follower conversion still parks brain units
    (cross-reference for the follower-parking invariant)."""
    text = installer_text()
    parking = text.split("park_streambox_brain_units() {", 1)[1].split("\n}", 1)[0]
    assert "jasper-voice.service" in parking
    assert "systemctl disable --now" in parking
    # #3697 (ADR-0226): the only path a streambox can carry these enabled is
    # a full->streambox conversion, since install_streambox_systemd_units
    # never stages or enables them itself.
    assert "jasper-enhanced-aec-install.service" in parking
    assert "jasper-enhanced-aec-reconcile.path" in parking


def _installer_function_body(name: str) -> str:
    return installer_text().split(f"{name}() {{", 1)[1].split("\n}", 1)[0]


def test_streambox_keeps_hid_accessory_bridge():
    """A volume remote is renderer-side, not a voice-brain feature.

    jasper-input translates HID key events into jasper-control HTTP calls and
    jasper-control runs on both profiles, so parking it as a brain surface
    silently costs a streambox its paired remote's buttons.
    """
    parking = _installer_function_body("park_streambox_brain_units")
    assert "jasper-input.service" not in parking


def test_hid_accessory_unit_files_actually_install(tmp_path):
    """Run the shared installer for real; assert the files land.

    `systemctl enable` on a unit whose file was never written exits 1, and
    install.sh runs under `set -euo pipefail`, so a missing file aborts the
    install partway. A box carrying the file from an earlier FULL install
    passes regardless -- which is exactly how the gap shipped unnoticed.
    """
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail\n'
            'source "${REPO_DIR}/deploy/lib/install/systemd-units.sh"\n'
            "install_hid_accessory_unit_files\n",
        ],
        env={
            **os.environ,
            "REPO_DIR": str(REPO_ROOT),
            "SYSTEMD_DIR": str(systemd_dir),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in systemd_dir.iterdir()} == {
        "jasper-input.service",
        "jasper-accessory-reconcile.service",
        # The watcher that turns an unprivileged request file into a pass.
        # Without the file on disk `enable --now` exits 1 under set -e.
        "jasper-accessory-reconcile.path",
        "jasper-wiim-remote-ce.service",
    }


def test_streambox_install_and_runtime_cover_the_accessory_bridge():
    """Both halves, because either alone still leaves a broken box.

    Structural rather than executed: install_streambox_systemd_units writes
    outside SYSTEMD_DIR (/usr/local/lib/jasper), so it cannot run
    unprivileged in a test the way the helper above can.

    Enabling without starting is its own failure: enable only arms the unit
    for the NEXT boot, while deploy health checks THIS one, so every
    streambox deploy would report a missing bridge until someone rebooted.
    """
    install_path = _installer_function_body("install_streambox_systemd_units")
    assert "install_hid_accessory_unit_files" in install_path

    runtime = _installer_function_body("start_streambox_runtime_units")
    assert "jasper-input.service" in runtime
    assert "systemctl restart jasper-input.service" in runtime
    assert "jasper-accessory-reconcile --reason install" in runtime
    assert "systemctl enable --now jasper-accessory-reconcile.path" in runtime


def test_streambox_keeps_coupling_auto_for_usb_direct_capture():
    """Fan-in coupling is data-plane ownership, not a voice-brain feature.

    Streambox supports USB Audio Input through the same direct fan-in lane, so
    full->streambox conversion must preserve coupling-auto and its runtime path
    must enable it before source-intent reapply.
    """
    text = installer_text()
    parking = text.split("park_streambox_brain_units() {", 1)[1].split("\n}", 1)[0]
    assert "jasper-fanin-coupling-auto.service" not in parking
    streambox_runtime = text.split("start_streambox_runtime_units() {", 1)[1].split(
        "\n}", 1
    )[0]
    coupling_idx = streambox_runtime.index(
        "systemctl enable jasper-fanin-coupling-auto.service"
    )
    reapply_idx = streambox_runtime.index("reapply_source_intent")
    assert coupling_idx < reapply_idx


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


def test_one_gb_full_speaker_rust_build_uses_low_memory_profile(tmp_path: Path):
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

    assert detected.returncode == 0, detected.stderr
    assert "CARGO_BUILD_JOBS=1" in env.stdout
    assert "CARGO_PROFILE_RELEASE_LTO=false" in env.stdout


def test_two_gb_full_speaker_rust_build_keeps_release_profile(tmp_path: Path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        2031616 kB\n")

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


def test_follower_role_plan_hands_sources_to_canonical_owner():
    """Grouping owns Snapcast state and one blocking source handoff."""
    from jasper.multiroom.config import (
        DEFAULT_BUFFER_MS,
        DEFAULT_CODEC,
        GroupingConfig,
    )
    from jasper.multiroom.reconcile import SOURCE_INTENT_RECONCILE_UNIT, plan

    cfg = GroupingConfig(
        enabled=True, role="follower", channel="left",
        bond_id="bond-1", leader_addr="jts.local",
        buffer_ms=DEFAULT_BUFFER_MS, codec=DEFAULT_CODEC, error=None,
    )
    units = {intent.unit for intent in plan(cfg).intents}
    assert units == {"jasper-snapserver.service", "jasper-snapclient.service"}
    assert SOURCE_INTENT_RECONCILE_UNIT == "jasper-source-intent-reconcile.service"


def test_both_install_profiles_start_mux_on_first_install():
    """Enable-only + try-restart leaves a never-started mux inactive."""

    source = (
        REPO_ROOT / "deploy/lib/install/systemd-units.sh"
    ).read_text(encoding="utf-8")
    streambox = source.split("start_streambox_runtime_units() {", 1)[1].split(
        "\n}", 1
    )[0]
    full = source.split("install_systemd_units() {", 1)[1]

    assert "systemctl enable --now jasper-mux.service" in streambox
    assert "systemctl enable --now jasper-mux.service" in full
    assert "systemctl try-restart jasper-mux.service" not in streambox
    assert "systemctl try-restart jasper-mux.service" not in full


def test_streambox_resolves_coupling_after_grouping_during_install():
    """Do not leave USB/ring derived state stale until the next boot."""

    source = (
        REPO_ROOT / "deploy/lib/install/systemd-units.sh"
    ).read_text(encoding="utf-8")
    body = source.split("start_streambox_runtime_units() {", 1)[1].split(
        "\n}", 1
    )[0]

    assert "systemctl enable jasper-fanin-coupling-auto.service" in body
    assert body.index("reconcile_grouping_state") < body.index(
        "resolve_fanin_coupling_default"
    )


# ---------- assistant unit on the streambox profile (ADR-0217) -----------


def test_voice_unit_file_actually_installs(tmp_path: Path):
    """Run the streambox installer's voice helper for real; assert it lands.

    The accessory reconciler issues `systemctl start jasper-voice.service`
    when a mic-bearing remote pairs. That exits 1 on a box whose unit file
    was never written, and a streambox converted from a full install carries
    the file from the earlier profile -- which is exactly how a fresh-box
    gap ships unnoticed.
    """
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail\n'
            'source "${REPO_DIR}/deploy/lib/install/systemd-units.sh"\n'
            "install_voice_unit_files\n",
        ],
        env={
            **os.environ,
            "REPO_DIR": str(REPO_ROOT),
            "SYSTEMD_DIR": str(systemd_dir),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in systemd_dir.iterdir()} == {
        "jasper-voice.service",
    }


def test_streambox_stages_the_voice_unit_without_boot_enabling_it():
    """Staged, never enabled: the reconciler owns when the assistant runs.

    Structural rather than executed for the same reason as the accessory
    bridge above: install_streambox_systemd_units writes outside SYSTEMD_DIR
    and start_streambox_runtime_units drives /opt and /usr/local binaries,
    so neither can run unprivileged.

    Boot-enabling would leave a remote-less streambox running an assistant
    it has no microphone for, which is the state ADR-0217 exists to prevent.
    """
    install_path = _installer_function_body("install_streambox_systemd_units")
    assert "install_voice_unit_files" in install_path

    runtime = _installer_function_body("start_streambox_runtime_units")
    assert "jasper-voice.service" not in runtime


def test_streambox_parking_clears_the_stale_voice_input_marker(tmp_path: Path):
    """Parking the marker's writer must also drop the marker.

    jasper-voice.service carries
    ConditionPathExists=!/var/lib/jasper/voice-input-absent and only
    jasper-aec-reconcile -- parked here -- ever removes it. Left behind, every
    start the accessory reconciler issues for a paired mic remote
    condition-fails silently: the unit reports success and nothing runs.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = state_dir / "voice-input-absent"
    marker.write_text("", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    fake_systemctl = bin_dir / "systemctl"
    fake_systemctl.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>{shlex.quote(str(calls))}\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail\n'
            'source "${REPO_DIR}/deploy/lib/install/systemd-units.sh"\n'
            "park_streambox_brain_units\n",
        ],
        env={
            **os.environ,
            "REPO_DIR": str(REPO_ROOT),
            "SYSTEMD_DIR": str(tmp_path / "systemd"),
            "STATE_DIR": str(state_dir),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    # Harness self-check: a driver that died while sourcing would leave the
    # marker untouched for the wrong reason.
    assert calls.exists() and calls.read_text(encoding="utf-8").strip(), (
        f"parking never reached systemctl; stderr={result.stderr!r}"
    )
    assert not marker.exists()
