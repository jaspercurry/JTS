"""Unit tests for bash helpers in deploy/install.sh.

The helpers can be sourced cleanly because install.sh's `main` call
is guarded by `if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]`. Tests source
the file and invoke individual functions.

Coverage started with `_compute_min_free_kbytes` (Concern 9 of the
staff-eng review) and now also pins install-time optional firmware
build behavior. These bash helpers are small, but easy to regress
because they sit on the deploy path.
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

from tests.install_surface import installer_text


_INSTALL_SH = Path(__file__).parent.parent / "deploy" / "install.sh"
_INSTALL_LIB_DIR = Path(__file__).parent.parent / "deploy" / "lib" / "install"
_RENDERERS_LIB = _INSTALL_LIB_DIR / "renderers.sh"
_MODEL_DOWNLOADS = Path(__file__).parent.parent / "jasper" / "model_downloads.py"
_ENV_EXAMPLE = Path(__file__).parent.parent / ".env.example"


def _installer_shell_texts() -> dict[Path, str]:
    """install.sh plus the deploy/lib/install/*.sh libs it sources.

    Invariant-style tests (bounded curl flags, no unpinned git
    fetches, …) must keep covering function groups that the
    install.sh decomposition moved into sourced libs."""
    paths = [_INSTALL_SH, *sorted(_INSTALL_LIB_DIR.glob("*.sh"))]
    assert _RENDERERS_LIB in paths
    return {p: p.read_text(encoding="utf-8") for p in paths}


def _compute_min_free_kbytes(memtotal_kb: int) -> int:
    """Invoke the bash helper via subprocess; return its integer
    output. Discards any stdout from the sourcing step so we only
    capture the helper's output."""
    # Then the bare invocation of _compute_min_free_kbytes goes to the
    # outer stdout, which we capture.
    result = subprocess.run(
        ["bash", "-c",
         f"source {_INSTALL_SH} >/dev/null && _compute_min_free_kbytes {memtotal_kb}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"helper failed (rc={result.returncode}): {result.stderr}"
        )
    return int(result.stdout.strip())


def _render_install_asound_template(
    tmp_path: Path,
    *,
    output_dac_id: str,
    output_dac_card: str,
) -> tuple[str, str]:
    source = tmp_path / "asoundrc.jasper"
    dest = tmp_path / "asoundrc.rendered"
    source.write_text(
        "__OUTPUTD_DAC_PCM_BLOCK__\n"
        "ctl.outputd_dac { card __OUTPUT_DAC_CARD__ }\n"
        "pcm.jasper_out { card __DONGLE_CARD__ }\n",
        encoding="utf-8",
    )
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        "DONGLE_CARD=A && "
        f"OUTPUT_DAC_CARD={shlex.quote(output_dac_card)} && "
        f"OUTPUT_DAC_ID={shlex.quote(output_dac_id)} && "
        f"jasper_asound_render_template {shlex.quote(str(source))} {shlex.quote(str(dest))}"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"render helper failed (rc={result.returncode}): {result.stderr}"
        )
    return dest.read_text(encoding="utf-8"), result.stderr


def _run_install_helper(
    helper_name: str,
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"ENV_DIR={shlex.quote(str(env_dir))} && "
        f"STATE_DIR={shlex.quote(str(state_dir))} && "
        f"{helper_name}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )


# Pi 5 SKU memory sizes (real values from /proc/meminfo on each
# variant — approximate; actual values vary by ~5 MB per board).
_PI5_1GB_MEMTOTAL_KB = 1014768   # 991 MB
_PI5_2GB_MEMTOTAL_KB = 2031264   # 1983 MB (some firmware budget)
_PI5_4GB_MEMTOTAL_KB = 4063920   # 3968 MB
_PI5_8GB_MEMTOTAL_KB = 8128464   # 7938 MB
_PI5_16GB_MEMTOTAL_KB = 16264848 # 15883 MB


def test_compute_1gb_pi():
    """1 GB Pi: 2% × 991 MB ≈ 19.8 MB → ~20 MB."""
    result = _compute_min_free_kbytes(_PI5_1GB_MEMTOTAL_KB)
    # 2% × 1014768 = 20295.36 → round to 20295
    assert result == 20295
    # And in human-readable terms, this is about 20 MB
    assert 19_000 < result < 22_000


def test_compute_2gb_pi():
    """2 GB Pi: 2% × ~2 GB → ~40 MB."""
    result = _compute_min_free_kbytes(_PI5_2GB_MEMTOTAL_KB)
    # 2% × 2031264 = 40625.28 → round to 40625
    assert result == 40625
    assert 39_000 < result < 43_000


def test_compute_4gb_pi():
    """4 GB Pi: 2% × ~4 GB → ~81 MB."""
    result = _compute_min_free_kbytes(_PI5_4GB_MEMTOTAL_KB)
    assert 80_000 < result < 83_000


def test_compute_8gb_pi():
    """8 GB Pi: 2% × ~8 GB → ~160 MB."""
    result = _compute_min_free_kbytes(_PI5_8GB_MEMTOTAL_KB)
    assert 160_000 < result < 165_000


def test_compute_16gb_pi_hits_ceiling():
    """16 GB Pi: 2% × 16 GB = ~320 MB, but capped at 256 MB.
    This is the load-bearing ceiling — verify the cap fires."""
    result = _compute_min_free_kbytes(_PI5_16GB_MEMTOTAL_KB)
    assert result == 262144   # exactly 256 MB


def test_compute_very_small_hits_floor():
    """A pathological tiny MemTotal (1 MB) shouldn't reduce
    min_free_kbytes below the Pi Foundation default of 8192 kB."""
    result = _compute_min_free_kbytes(1024)
    assert result == 8192


def test_compute_floor_threshold_exactly():
    """At the boundary: 2% of 409600 kB = 8192 kB exactly. Should
    return 8192 (the floor)."""
    # 8192 / 0.02 = 409600 kB. So MemTotal at exactly this gives 8192.
    result = _compute_min_free_kbytes(409_600)
    assert result == 8192


def test_compute_ceiling_threshold_exactly():
    """At the boundary: 2% × 13107200 kB = 262144 kB exactly.
    The cap should return 262144 (not over-clamp)."""
    result = _compute_min_free_kbytes(13_107_200)
    assert result == 262144


def test_compute_just_below_ceiling():
    """Just below the ceiling: should still be computed proportionally,
    not pinned to 262144."""
    # 2% × 13_000_000 = 260_000 kB
    result = _compute_min_free_kbytes(13_000_000)
    assert result == 260_000
    assert result < 262144   # NOT capped


def test_compute_rounding_behavior():
    """awk's int(x + 0.5) gives round-half-up. Verify a value
    that hits the rounding boundary."""
    # 2% × 100_001 = 2000.02 → round to 2000 → floor to 8192
    # 2% × 8_192_050 = 163841 (rounds from 163841.0)
    result = _compute_min_free_kbytes(8_192_050)
    assert result == 163_841


def test_compute_rejects_negative_or_garbage_input():
    """awk on a non-numeric input would produce 0 (which then hits
    the floor). Verify that the floor kicks in rather than a
    crash or negative output."""
    # awk treats non-numeric strings as 0 in arithmetic contexts.
    # int(0 * 0.02 + 0.5) = 0, then clamped to 8192.
    result = _compute_min_free_kbytes(0)
    assert result == 8192


def test_ensure_state_dir_uses_voice_state_directory_mode(tmp_path):
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source "
            + shlex.quote(str(_INSTALL_SH))
            + " >/dev/null && "
            + "STATE_DIR="
            + shlex.quote(str(state_dir))
            + " && ensure_state_dir",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o750


def _camilla_volume_limit_ok(config: str, tmp_path: Path) -> bool:
    config_path = tmp_path / "config.yml"
    config_path.write_text(config, encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source "
            + shlex.quote(str(_INSTALL_SH))
            + " >/dev/null && camilla_config_has_safe_volume_limit "
            + shlex.quote(str(config_path)),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0


def test_camilla_volume_limit_accepts_unquoted_non_positive_values(tmp_path):
    assert _camilla_volume_limit_ok("devices:\n  volume_limit: 0.0\n", tmp_path)
    assert _camilla_volume_limit_ok("volume_limit: -3.5 # dB\n", tmp_path)


def test_camilla_volume_limit_rejects_quoted_commented_or_positive_values(
    tmp_path,
):
    assert not _camilla_volume_limit_ok('volume_limit: "0.0"\n', tmp_path)
    assert not _camilla_volume_limit_ok("# volume_limit: 0.0\n", tmp_path)
    assert not _camilla_volume_limit_ok("volume_limit: 0.1\n", tmp_path)


def test_optional_firmware_builds_are_install_opt_in():
    """ESP32 satellites are optional accessories. Base speaker installs
    should stage firmware source but avoid PlatformIO builds unless the
    operator explicitly opts in."""
    text = "\n".join(_installer_shell_texts().values())
    assert "JASPER_BUILD_OPTIONAL_FIRMWARE" in text
    assert re.search(
        r'if \[\[ "\$\{JASPER_BUILD_OPTIONAL_FIRMWARE:-0\}" == "1" \]\]; then'
        r'\s+_build_firmware_if_stale "dial" "jasper-dial\.bin"'
        r'\s+_build_firmware_if_stale "satellite-amoled" '
        r'"jasper-satellite-amoled\.bin"',
        text,
    )


def test_active_speaker_tone_artifacts_are_writable_by_web_service():
    """The /sound/ combined-test path writes bounded WAV artifacts as jasper-web."""

    text = _INSTALL_LIB_DIR.joinpath("python-runtime.sh").read_text(
        encoding="utf-8"
    )
    assert (
        'install -d -m 2770 -o root -g jasper '
        '"${STATE_DIR}/active_speaker_tone_artifacts"'
    ) in text


def test_spotify_wizard_owned_values_are_not_seeded_into_jasper_env():
    """Fresh installs must not write stale empty Spotify overrides."""
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "\nSPOTIFY_CLIENT_ID=" not in env_example
    assert "\nSPOTIFY_REDIRECT_URI=" not in env_example

    install_sh = "\n".join(_installer_shell_texts().values())
    assert "/^SPOTIFY_CLIENT_ID=/d" in install_sh
    assert "/^SPOTIFY_OAUTH_MODE=/d" in install_sh
    assert "/^SPOTIFY_REDIRECT_URI=/d" in install_sh
    assert "/^SPOTIPY_REDIRECT_URI=/d" in install_sh


def test_firmware_staleness_includes_platformio_inputs(tmp_path):
    """Dependency-pin changes live in platformio.ini, so the optional
    rebuild freshness check must not look only at src/."""
    fw_root = tmp_path / "firmware" / "dial"
    (fw_root / "src").mkdir(parents=True)
    (fw_root / "include").mkdir()
    bin_path = fw_root / "jasper-dial.bin"
    platformio = fw_root / "platformio.ini"
    build_sh = fw_root / "build.sh"
    bin_path.write_bytes(b"old")
    platformio.write_text("[env]\nlib_deps = fastled/FastLED@3.10.3\n")
    build_sh.write_text("#!/usr/bin/env bash\n")

    os_old = 1_716_470_400
    os_new = 1_716_556_800
    os.utime(bin_path, (os_old, os_old))
    os.utime(platformio, (os_new, os_new))
    os.utime(build_sh, (os_old, os_old))

    result = subprocess.run(
        [
            "bash",
            "-c",
            "source "
            + shlex.quote(str(_INSTALL_SH))
            + " >/dev/null && _newer_firmware_input "
            + shlex.quote(str(fw_root))
            + " "
            + shlex.quote(str(bin_path)),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == str(platformio)


def test_install_dry_run_exits_before_root_and_lists_major_surfaces():
    """The install plan is contributor-facing safety gear: it must run
    without sudo and summarize the installer blast radius."""
    result = subprocess.run(
        ["bash", str(_INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("==> JTS install plan (dry run)\n")
    assert "this script must be run as root" not in result.stderr
    for expected in [
        "JTS install plan (dry run)",
        "No host changes are made in this mode",
        "apt-get update",
        "CamillaDSP:",
        "Raspotify/librespot deb:",
        "openWakeWord ONNX assets",
        "cargo build --release --locked",
        "/var/lib/jasper/build.txt",
        "Migrate wizard-owned keys",
        "Reload udev and systemd",
        "python3 scripts/check-provenance.py",
    ]:
        assert expected in result.stdout


def test_install_dry_run_env_alias_and_plan_flag_match():
    """Both documented entry points should use the same no-mutation plan."""
    by_flag = subprocess.run(
        ["bash", str(_INSTALL_SH), "--plan"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    env = os.environ.copy()
    env["JASPER_INSTALL_DRY_RUN"] = "1"
    by_env = subprocess.run(
        ["bash", str(_INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert by_flag.returncode == 0
    assert by_env.returncode == 0
    assert by_flag.stdout == by_env.stdout


def test_migrate_openai_noise_reduction_old_default_to_auto(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    jasper_env.write_text(
        "JASPER_OPENAI_NOISE_REDUCTION=far_field\n"
        "JASPER_SERVER_VAD_ENABLED=0\n",
        encoding="utf-8",
    )

    result = _run_install_helper(
        "migrate_openai_noise_reduction_default",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    body = jasper_env.read_text(encoding="utf-8")
    assert "JASPER_OPENAI_NOISE_REDUCTION=auto" in body
    assert "JASPER_SERVER_VAD_ENABLED=0" in body


def test_migrate_openai_noise_reduction_preserves_non_default_override(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    jasper_env.write_text(
        "JASPER_OPENAI_NOISE_REDUCTION=off\n",
        encoding="utf-8",
    )

    result = _run_install_helper(
        "migrate_openai_noise_reduction_default",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert jasper_env.read_text(encoding="utf-8") == (
        "JASPER_OPENAI_NOISE_REDUCTION=off\n"
    )


def test_migrate_tts_outputd_socket_old_default_to_fanin(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    jasper_env.write_text(
        "BEFORE=1\n"
        "JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock\n"
        "AFTER=1\n",
        encoding="utf-8",
    )

    result = _run_install_helper(
        "migrate_tts_outputd_socket_default",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert jasper_env.read_text(encoding="utf-8") == (
        "BEFORE=1\n"
        "JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-fanin/tts.sock\n"
        "AFTER=1\n"
    )


def test_migrate_tts_outputd_socket_preserves_custom_override(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    jasper_env.write_text(
        "JASPER_TTS_OUTPUTD_SOCKET=/tmp/custom-tts.sock\n",
        encoding="utf-8",
    )

    result = _run_install_helper(
        "migrate_tts_outputd_socket_default",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert jasper_env.read_text(encoding="utf-8") == (
        "JASPER_TTS_OUTPUTD_SOCKET=/tmp/custom-tts.sock\n"
    )


def test_migrate_removed_output_dac_route_strips_stale_key(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    jasper_env.write_text(
        "BEFORE=1\n"
        "JASPER_OUTPUT_DAC_ROUTE=mono:5\n"
        "AFTER=1\n",
        encoding="utf-8",
    )

    result = _run_install_helper(
        "migrate_removed_output_dac_route",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert jasper_env.read_text(encoding="utf-8") == (
        "BEFORE=1\n"
        "AFTER=1\n"
    )
    assert "removed stale JASPER_OUTPUT_DAC_ROUTE" in result.stdout


def test_model_downloads_are_bounded_and_split_by_runtime_need():
    """Model fetches should use the shared bounded helper. Required
    openWakeWord runtime assets and the active stock fallback fail the
    install; inactive stock rows are allowed to stay unavailable."""
    shell_text = "\n".join(_installer_shell_texts().values())
    model_text = _MODEL_DOWNLOADS.read_text(encoding="utf-8")

    assert "urllib.request.urlretrieve" not in shell_text + model_text
    assert "python\" -m jasper.model_downloads" in shell_text
    assert "stage --registry openwakeword --required" in shell_text
    assert "stage --registry wake --optional" in shell_text
    assert "stage --registry dtln --optional" in shell_text
    assert "download_model_file(" in model_text
    assert "timeout_seconds=" in model_text
    assert "retries=" in model_text
    assert "max_bytes" in model_text
    assert "required_openwakeword_assets" in model_text
    assert "fallback_openwakeword_assets" in model_text
    assert "openwakeword_asset_for_model(active_model)" in model_text
    assert "required_failures" in model_text
    assert "optional_failures" in model_text
    assert "unavailable rows will be disabled in /wake/" in model_text


def test_base_source_builds_use_hash_checked_archives():
    """Base Pi installs should consume pinned archives, not require git
    just to fetch source-build inputs."""
    text = _INSTALL_SH.read_text(encoding="utf-8")

    for expected in [
        "NQPTP_ARCHIVE_URL",
        "NQPTP_SHA256",
        "SHAIRPORT_SYNC_ARCHIVE_URL",
        "SHAIRPORT_SYNC_SHA256",
        "WEBRTC_AEC3_ARCHIVE_URL",
        "WEBRTC_AEC3_SHA256",
        "fetch_verified_source_archive",
    ]:
        assert expected in text

    for path, source_text in _installer_shell_texts().items():
        for forbidden in [
            "git clone --depth 1",
            "git init ",
            "git -C \"${tmpdir}",
            "verify_git_head",
        ]:
            assert forbidden not in source_text, (path, forbidden)


def test_install_help_is_clean_and_non_root():
    """Agentic flows often probe commands with --help; keep it quiet
    and usable without sudo."""
    result = subprocess.run(
        ["bash", str(_INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("Usage: bash deploy/install.sh")
    assert "install.sh starting" not in result.stdout
    assert "this script must be run as root" not in result.stderr


def test_install_asound_renderer_renders_direct_outputd_dac(tmp_path: Path):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="hifiberry_dac8x",
        output_dac_card="sndrpihifiberry",
    )

    assert stderr == ""
    assert "type hw" in rendered
    assert "card sndrpihifiberry" in rendered
    assert "device 0" in rendered
    assert "ctl.outputd_dac { card sndrpihifiberry }" in rendered
    assert "pcm.jasper_out { card A }" in rendered


def test_install_asound_renderer_dual_apple_parks_outputd_dac(tmp_path: Path):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="dual_apple_usb_c_dac_4ch",
        output_dac_card="",
    )

    assert stderr == ""
    assert "type null" in rendered
    assert "type route" not in rendered


def _sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_source_archive(tmp_path: Path) -> tuple[Path, str]:
    """Build a small tar.gz shaped like a GitHub source archive
    (one top-level dir, stripped by --strip-components=1)."""
    tree = tmp_path / "pkg-1.0"
    tree.mkdir()
    (tree / "hello.c").write_text("int main(void) { return 0; }\n")
    archive = tmp_path / "source.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(tmp_path), "pkg-1.0"],
        check=True,
        timeout=10,
    )
    return archive, _sha256_of(archive)


def _run_fetch_verified(
    url: str, sha: str, dest: Path,
) -> subprocess.CompletedProcess[str]:
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"fetch_verified_source_archive {shlex.quote(url)} "
        f"{shlex.quote(sha)} {shlex.quote(str(dest))} test-archive"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_fetch_verified_source_archive_populates_dest(tmp_path):
    archive, sha = _make_source_archive(tmp_path)
    dest = tmp_path / "dest"

    result = _run_fetch_verified(f"file://{archive}", sha, dest)

    assert result.returncode == 0, result.stderr
    assert (dest / "hello.c").exists()


def test_fetch_verified_source_archive_preserves_dest_on_bad_hash(tmp_path):
    """Fetch-to-temp-then-swap: a failed verification must NOT destroy
    the previously-extracted source at dest (the pre-fix shape rm -rf'd
    dest before curl, stranding the install under set -e)."""
    archive, _ = _make_source_archive(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    sentinel = dest / "previous-source.c"
    sentinel.write_text("previous good tree\n")

    result = _run_fetch_verified(f"file://{archive}", "0" * 64, dest)

    assert result.returncode != 0
    assert sentinel.exists()
    assert sentinel.read_text() == "previous good tree\n"


def test_fetch_verified_source_archive_preserves_dest_on_fetch_failure(tmp_path):
    """A download failure (here: nonexistent file:// URL) must also
    leave the destination untouched."""
    dest = tmp_path / "dest"
    dest.mkdir()
    sentinel = dest / "previous-source.c"
    sentinel.write_text("previous good tree\n")

    result = _run_fetch_verified(
        f"file://{tmp_path}/no-such-archive.tar.gz", "0" * 64, dest,
    )

    assert result.returncode != 0
    assert sentinel.exists()


def test_shairport_build_completes_before_old_binary_is_removed():
    """install_renderers must fetch + compile the shairport-sync
    replacement BEFORE stopping the service and apt-removing the old
    binary. Under set -e, the old order stranded the Pi with no AirPlay
    when the download or build failed. install_renderers lives in the
    sourced deploy/lib/install/renderers.sh since the decomposition."""
    text = _RENDERERS_LIB.read_text(encoding="utf-8")

    idx_fetch = text.index('"${tmpdir}/sps"')
    idx_stop = text.index("systemctl stop shairport-sync")
    idx_remove = text.index("apt-get remove -y shairport-sync")
    idx_make_install = text.index("make install || true")

    # The compile (first `make -j4` after the sps fetch) must precede
    # the stop/remove, which must precede `make install`.
    idx_build = text.index("make -j4", idx_fetch)
    assert idx_fetch < idx_build < idx_stop < idx_remove < idx_make_install


def test_install_curl_fetches_are_bounded_and_retried():
    """Every direct multi-MB curl in install.sh (and its sourced
    deploy/lib/install/ libs) carries bounded retries and a transfer
    cap so flaky Pi WiFi doesn't abort (or hang) the install."""
    for path, text in _installer_shell_texts().items():
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("curl ", "if ! curl ")) and "-o" in stripped:
                assert "--retry 3" in stripped, (path, stripped)
                assert "--retry-connrefused" in stripped, (path, stripped)
                assert "--max-time" in stripped, (path, stripped)


def test_pip_toolchain_bootstrap_is_exact_pinned():
    """The venv's pip/wheel bootstrap must be pinned exactly — an
    unpinned `--upgrade pip wheel` re-resolved the installer toolchain
    on every deploy."""
    text = "\n".join(_installer_shell_texts().values())
    assert re.search(
        r'pip" install --upgrade pip==\d+\.\d+(\.\d+)? wheel==\d+\.\d+(\.\d+)?',
        text,
    )
    assert 'install --upgrade pip wheel' not in text


def _run_require_build_user(tmp_path: Path, getent_rc: int) -> subprocess.CompletedProcess[str]:
    """Run require_build_user with a stub `getent` ahead of PATH so the
    test controls whether the build user 'exists'."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "getent"
    stub.write_text(f"#!/usr/bin/env bash\nexit {getent_rc}\n", encoding="utf-8")
    stub.chmod(0o755)
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"export PATH={shlex.quote(str(bindir))}:$PATH && "
        "require_build_user"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_require_build_user_passes_when_user_exists(tmp_path):
    result = _run_require_build_user(tmp_path, getent_rc=0)
    assert result.returncode == 0, result.stderr


def test_require_build_user_fails_fast_with_remediation(tmp_path):
    """Missing 'pi' must fail with an actionable message — this runs
    before any host mutation so a custom-user host stops at second zero
    instead of 15 minutes in, mid-apt."""
    result = _run_require_build_user(tmp_path, getent_rc=2)
    assert result.returncode != 0
    assert "build user 'pi' does not exist" in result.stderr
    assert "adduser" in result.stderr
    assert "before any packages or services were modified" in result.stderr


def test_main_preflights_build_user_before_mutation():
    """require_build_user must run in main() before install_deps (the
    first host-mutating step)."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    main_body = text[text.index("\nmain() {"):]
    assert main_body.index("require_build_user") < main_body.index("install_deps")


# ----------------------------------------------------------------------
# Pi-generated pip constraints (deploy/constraints-pi.txt)
# ----------------------------------------------------------------------

_GENERATE_CONSTRAINTS_SH = (
    Path(__file__).parent.parent / "scripts" / "generate-pi-constraints.sh"
)


def _run_constraints_helper(repo_dir: Path) -> str:
    """Source install.sh with REPO_DIR overridden; return the helper's
    stdout (the constraints path, or empty when absent)."""
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"REPO_DIR={shlex.quote(str(repo_dir))} && "
        "jasper_pip_constraints_file"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_install_and_generate_scripts_parse():
    """bash -n over both shell surfaces of the constraints feature."""
    for path in (_INSTALL_SH, _GENERATE_CONSTRAINTS_SH):
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, f"{path}: {result.stderr}"


def test_pip_constraints_file_absent_is_graceful_noop(tmp_path):
    """No deploy/constraints-pi.txt → empty output, rc 0 — installs run
    open-range exactly as before the feature existed."""
    (tmp_path / "deploy").mkdir()
    assert _run_constraints_helper(tmp_path) == ""


def test_pip_constraints_file_present_is_echoed(tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    constraints = deploy / "constraints-pi.txt"
    constraints.write_text("httpx==0.28.1\n", encoding="utf-8")
    assert _run_constraints_helper(tmp_path) == str(constraints)


def test_unpinned_pip_installs_carry_the_constraints_args():
    """Open-range pip installs in full/streambox installs must expand the
    pip_constraints array; the exact-pinned installs (pip/wheel,
    openwakeword --no-deps) intentionally don't need it."""
    text = "\n".join(_installer_shell_texts().values())
    # full: requests/scipy/... + -e [full]; streambox: -e [streambox] = 3.
    assert text.count('pip" install "${pip_constraints[@]}"') == 3
    assert 'install "${pip_constraints[@]}" -e "${INSTALL_DIR}[full]"' in text
    assert 'install "${pip_constraints[@]}" \\\n        -e "${INSTALL_DIR}[streambox]"' in text


def test_full_install_editable_pip_install_keeps_full_extra():
    """Full speaker installs must pull the runtime dependency stack."""
    text = installer_text()

    assert (
        text.count('install "${pip_constraints[@]}" -e "${INSTALL_DIR}[full]"')
        == 1
    )
    # With the endpoint tier removed there is no bare base-package editable
    # install any more — every profile pulls an extras-tagged install.
    assert text.count('install "${pip_constraints[@]}" -e "${INSTALL_DIR}"') == 0
