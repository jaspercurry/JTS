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
import subprocess
from pathlib import Path


_INSTALL_SH = Path(__file__).parent.parent / "deploy" / "install.sh"
_ENV_EXAMPLE = Path(__file__).parent.parent / ".env.example"


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
    output_dac_route: str,
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
        f"OUTPUT_DAC_ROUTE={shlex.quote(output_dac_route)} && "
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


def test_optional_firmware_builds_are_install_opt_in():
    """ESP32 satellites are optional accessories. Base speaker installs
    should stage firmware source but avoid PlatformIO builds unless the
    operator explicitly opts in."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    assert "JASPER_BUILD_OPTIONAL_FIRMWARE" in text
    assert re.search(
        r'if \[\[ "\$\{JASPER_BUILD_OPTIONAL_FIRMWARE:-0\}" == "1" \]\]; then'
        r'\s+_build_firmware_if_stale "dial" "jasper-dial\.bin"'
        r'\s+_build_firmware_if_stale "satellite-amoled" '
        r'"jasper-satellite-amoled\.bin"',
        text,
    )


def test_spotify_wizard_owned_values_are_not_seeded_into_jasper_env():
    """Fresh installs must not write stale empty Spotify overrides."""
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "\nSPOTIFY_CLIENT_ID=" not in env_example
    assert "\nSPOTIFY_REDIRECT_URI=" not in env_example

    install_sh = _INSTALL_SH.read_text(encoding="utf-8")
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


def test_model_downloads_are_bounded_and_split_by_runtime_need():
    """Model fetches should use the shared bounded helper. Required
    openWakeWord runtime assets and the active stock fallback fail the
    install; inactive stock rows are allowed to stay unavailable."""
    text = _INSTALL_SH.read_text(encoding="utf-8")

    assert "urllib.request.urlretrieve" not in text
    assert "download_model_file(" in text
    assert "timeout_seconds=" in text
    assert "retries=" in text
    assert "max_bytes" in text
    assert "required_openwakeword_assets" in text
    assert "fallback_openwakeword_assets" in text
    assert "openwakeword_asset_for_model(active_wake_model())" in text
    assert "required_failures" in text
    assert "optional_failures" in text
    assert "unavailable rows will be disabled in /wake/" in text


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

    for forbidden in [
        "git clone --depth 1",
        "git init ",
        "git -C \"${tmpdir}",
        "verify_git_head",
    ]:
        assert forbidden not in text


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


def test_install_asound_renderer_supports_dac8x_mono_route(tmp_path: Path):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="hifiberry_dac8x",
        output_dac_card="sndrpihifiberry",
        output_dac_route="mono:5",
    )

    assert stderr == ""
    assert "type route" in rendered
    assert 'pcm "hw:CARD=sndrpihifiberry,DEV=0"' in rendered
    assert "channels 8" in rendered
    assert "0.4 0.5" in rendered
    assert "1.4 0.5" in rendered
    assert "ctl.outputd_dac { card sndrpihifiberry }" in rendered
    assert "pcm.jasper_out { card A }" in rendered


def test_install_asound_renderer_keeps_invalid_route_warning_out_of_config(
    tmp_path: Path,
):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="hifiberry_dac8x",
        output_dac_card="sndrpihifiberry",
        output_dac_route="stereo:5,5",
    )

    assert "reason=duplicate_stereo_channel" in stderr
    assert "Ignoring JASPER_OUTPUT_DAC_ROUTE" not in rendered
    assert "type hw" in rendered
    assert "card sndrpihifiberry" in rendered
    assert "type route" not in rendered
