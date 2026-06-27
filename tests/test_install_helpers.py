# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

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


def _webrtc_compile_jobs(memtotal_kb: int, ncpu: int) -> int:
    """Invoke the bash `_webrtc_compile_jobs` helper (MemTotal kB,
    nproc) and return the bounded job count it prints."""
    result = subprocess.run(
        ["bash", "-c",
         f"source {_INSTALL_SH} >/dev/null && "
         f"_webrtc_compile_jobs {memtotal_kb} {ncpu}"],
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
    output_dac_recognized: str = "1",
) -> tuple[str, str]:
    source = tmp_path / "asoundrc.jasper"
    dest = tmp_path / "asoundrc.rendered"
    source.write_text(
        "__OUTPUTD_DAC_PCM_BLOCK__\n"
        "__OUTPUTD_DAC_CTL_BLOCK__\n"
        "pcm.jasper_out { card __DONGLE_CARD__ }\n",
        encoding="utf-8",
    )
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        "DONGLE_CARD=A && "
        f"OUTPUT_DAC_CARD={shlex.quote(output_dac_card)} && "
        f"OUTPUT_DAC_ID={shlex.quote(output_dac_id)} && "
        f"OUTPUT_DAC_RECOGNIZED={shlex.quote(output_dac_recognized)} && "
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


def _assert_no_empty_alsa_card(rendered: str) -> None:
    assert not re.search(r"(?m)^\s*card\s*$", rendered)
    assert not re.search(r"\bcard\s+}", rendered)


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


# --- _webrtc_compile_jobs: RAM-bounded WebRTC AEC3 build parallelism ---
# Regression: the unbounded `meson compile` fanned out to nproc (4 on a
# Pi 5) -O3 cc1plus jobs and the OOM killer aborted the deploy on a 1 GB
# Pi (jts2, 2026-06-21), taking nginx + jasper-voice down with it. The
# helper budgets ~1.5 GB/job and clamps to [1, nproc].

def test_webrtc_jobs_1gb_pi_is_single_job():
    """The load-bearing case: a 1 GB Pi must build at -j1 (the OOM we
    fixed). 991 MB / 1.5 GB-per-job floors to 0 → clamped up to 1."""
    assert _webrtc_compile_jobs(_PI5_1GB_MEMTOTAL_KB, 4) == 1


def test_webrtc_jobs_2gb_pi_is_single_job():
    """2 GB / 1.5 GB-per-job = 1."""
    assert _webrtc_compile_jobs(_PI5_2GB_MEMTOTAL_KB, 4) == 1


def test_webrtc_jobs_4gb_pi_is_two_jobs():
    """4 GB / 1.5 GB-per-job = 2 (under the 4-core cap)."""
    assert _webrtc_compile_jobs(_PI5_4GB_MEMTOTAL_KB, 4) == 2


def test_webrtc_jobs_8gb_pi_uses_full_nproc():
    """A roomy Pi isn't throttled: 8 GB budgets 5 jobs, clamped to the
    4 cores available."""
    assert _webrtc_compile_jobs(_PI5_8GB_MEMTOTAL_KB, 4) == 4


def test_webrtc_jobs_clamped_to_nproc_not_ram():
    """RAM allows more jobs than cores → clamp to nproc."""
    assert _webrtc_compile_jobs(_PI5_16GB_MEMTOTAL_KB, 2) == 2


def test_webrtc_jobs_never_zero_on_garbage_input():
    """A meson `-j0` would be invalid (or unbounded). Zero/garbage
    MemTotal must still floor to 1 job."""
    assert _webrtc_compile_jobs(0, 4) == 1
    assert _webrtc_compile_jobs(1024, 4) == 1


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


def test_mic_device_candidates_are_template_owned_for_fresh_install():
    """The install-time seed must not duplicate hot-swap mic candidates."""
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert (
        env_example.count("\nJASPER_MIC_DEVICE_CANDIDATES=Array,L16K6Ch\n")
        == 1
    )

    python_runtime = _INSTALL_LIB_DIR.joinpath("python-runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "JASPER_MIC_DEVICE_CANDIDATES=Array|" not in python_runtime


def test_wifi_tuning_persists_retry_forever_and_power_save_disable():
    """AirPlay's Wi-Fi tweak also owns NetworkManager retry resilience."""
    text = _RENDERERS_LIB.read_text(encoding="utf-8")
    match = re.search(
        r"tune_wifi_for_airplay\(\) \{(?P<body>.*?)\n\}",
        text,
        flags=re.S,
    )
    assert match is not None
    body = match.group("body")
    assert "connection.autoconnect yes" in body
    assert "connection.autoconnect-retries 0" in body
    assert "802-11-wireless.powersave 2" in body


def test_install_enables_wifi_recover_timer_with_now():
    """The low-footprint Wi-Fi recovery loop must be live from first deploy."""
    install_sh = installer_text()
    assert "jasper-wifi-recover.service" in install_sh
    assert "jasper-wifi-recover.timer" in install_sh
    assert "jasper-wifi-scan-repair.service" in install_sh
    assert "systemctl enable --now jasper-wifi-recover.timer" in install_sh


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
    assert "ctl.outputd_dac" in rendered
    assert "card sndrpihifiberry" in rendered
    assert "pcm.jasper_out { card A }" in rendered
    _assert_no_empty_alsa_card(rendered)


def test_install_asound_renderer_dual_apple_parks_outputd_dac(tmp_path: Path):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="dual_apple_usb_c_dac_4ch",
        output_dac_card="",
    )

    assert stderr == ""
    assert "type null" in rendered
    assert "type route" not in rendered
    assert "ctl.outputd_dac" not in rendered
    _assert_no_empty_alsa_card(rendered)


def test_install_asound_renderer_unknown_output_parks_outputd_dac(tmp_path: Path):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="A",
        output_dac_card="",
        output_dac_recognized="0",
    )

    assert stderr == ""
    assert "pcm.outputd_dac" in rendered
    assert "type null" in rendered
    assert "ctl.outputd_dac" not in rendered
    assert "pcm.jasper_out { card A }" in rendered
    _assert_no_empty_alsa_card(rendered)


def test_install_asound_renderer_rejects_empty_direct_outputd_dac_card(
    tmp_path: Path,
):
    source = tmp_path / "asoundrc.jasper"
    dest = tmp_path / "asoundrc.rendered"
    source.write_text(
        "__OUTPUTD_DAC_PCM_BLOCK__\n"
        "__OUTPUTD_DAC_CTL_BLOCK__\n",
        encoding="utf-8",
    )
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        "DONGLE_CARD=A && "
        "OUTPUT_DAC_CARD='' && "
        "OUTPUT_DAC_ID=hifiberry_dac8x && "
        "OUTPUT_DAC_RECOGNIZED=1 && "
        f"jasper_asound_render_template {shlex.quote(str(source))} {shlex.quote(str(dest))}"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 64
    assert "OUTPUT_DAC_CARD is required for hifiberry_dac8x" in result.stderr
    assert not dest.exists()


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

    # The compile (the contained shairport-sync build after the sps fetch)
    # must precede the stop/remove, which must precede `make install`.
    # The heavy build now routes through run_contained_build (RAM-bounded
    # + cgroup-contained) instead of a hardcoded `make -j4`.
    idx_build = text.index('run_contained_build "shairport-sync"', idx_fetch)
    assert idx_fetch < idx_build < idx_stop < idx_remove < idx_make_install


def test_shairport_configure_enables_airplay2_and_pipe_backend():
    """The shairport-sync source build must compile in BOTH AirPlay 2 and
    the pipe output backend. AirPlay 2 is the whole reason we source-build
    (Trixie apt is AP1-only); --with-pipe ships the pipe backend dormant so a
    future shairport->pipe->reader low-latency path needs no rebuild. The flag
    lives only on the ./configure line in renderers.sh; this contract test is
    what keeps a refactor of that long multi-line invocation from silently
    dropping a backend, and couples the flag to its rebuild-force trigger."""
    text = _RENDERERS_LIB.read_text(encoding="utf-8")

    # Isolate the shairport ./configure invocation (a backslash-continued
    # multi-line command) so we don't match nqptp's separate ./configure.
    match = re.search(
        r"\./configure --sysconfdir=/etc(?P<flags>(?:.*\\\n)*.*--with-mpris-interface)",
        text,
    )
    assert match is not None, "shairport ./configure line not found in renderers.sh"
    flags = match.group("flags")

    assert "--with-airplay-2" in flags
    assert "--with-pipe" in flags
    # --with-stdout is intentionally NOT built (no planned stdout pipeline;
    # the design target is a named-pipe reader, not shairport-as-a-pipe-stage).
    assert "--with-stdout" not in flags

    # The rebuild trigger must feature-detect the pipe backend, or a flag-only
    # change is a silent no-op on already-built Pis (-V already has "AirPlay2").
    # NOTE: if the on-device token check changes this literal (item 2 of the
    # Stage 5 spec), update it here in the same edit.
    assert 'grep -q -- "-pipe-"' in text


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


def test_pi_constraints_do_not_pin_non_pypi_flatbuffers_release():
    constraints = (Path(__file__).parent.parent / "deploy" / "constraints-pi.txt")
    text = constraints.read_text(encoding="utf-8")
    generator = _GENERATE_CONSTRAINTS_SH.read_text(encoding="utf-8")

    assert "flatbuffers==20181003210633" not in text
    assert "grep -v -Fx 'flatbuffers==20181003210633'" in generator


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


# ----------------------------------------------------------------------
# Build manifest = the VERIFIED-INSTALL success marker (Workstream B,
# problem #4). On jts2 (2026-06-21) the manifest was written EARLY, before
# an OOM-killed build aborted install.sh, so the box advertised a SHA it
# wasn't cleanly running and misled the deploy direction-guard. The fix:
# write the manifest as the FINAL mutation in main(), gated by set -e, so
# a mid-install abort leaves the prior good manifest untouched.
# ----------------------------------------------------------------------

_PYTHON_RUNTIME_LIB = _INSTALL_LIB_DIR / "python-runtime.sh"


def _run_install_snippet(
    snippet: str,
    *,
    state_dir: Path | None = None,
    repo_dir: Path | None = None,
    deploy_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source install.sh and run a snippet with a deterministic build-SHA
    environment. Clears any inherited JASPER_DEPLOY_* so precedence tests
    don't pick up a value from the CI runner, then exports the requested
    ones and overrides STATE_DIR/REPO_DIR to scratch paths."""
    prelude = [
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null",
        "unset JASPER_DEPLOY_SHA JASPER_DEPLOY_SHA_FULL JASPER_DEPLOY_BRANCH",
    ]
    for key, val in (deploy_env or {}).items():
        prelude.append(f"export {key}={shlex.quote(val)}")
    if state_dir is not None:
        prelude.append(f"STATE_DIR={shlex.quote(str(state_dir))}")
    if repo_dir is not None:
        prelude.append(f"REPO_DIR={shlex.quote(str(repo_dir))}")
    script = " && ".join([*prelude, snippet])
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_write_build_manifest_records_sha_and_verified_status(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    result = _run_install_snippet(
        "write_build_manifest",
        state_dir=state,
        deploy_env={
            "JASPER_DEPLOY_SHA": "abc1234",
            "JASPER_DEPLOY_SHA_FULL": "abc1234deadbeef",
            "JASPER_DEPLOY_BRANCH": "main",
        },
    )
    assert result.returncode == 0, result.stderr
    manifest = (state / "build.txt").read_text(encoding="utf-8")
    assert "JASPER_GIT_SHA=abc1234" in manifest
    assert "JASPER_GIT_SHA_FULL=abc1234deadbeef" in manifest
    assert "JASPER_GIT_BRANCH=main" in manifest
    assert "JASPER_INSTALL_AT=" in manifest
    # The honest "install process completed" claim the deploy verifier reads.
    assert "JASPER_INSTALL_STATUS=ok" in manifest


def test_write_build_manifest_preserves_prior_full_and_branch(tmp_path):
    """With no deploy env and no git, the full SHA + branch fall back to a
    prior manifest. Exercises the `[[ -z x ]] && x=$(grep …)` fallback
    lines under set -euo pipefail (sourcing install.sh enables it), proving
    they don't abort, and that the marker is still re-stamped status=ok."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "build.txt").write_text(
        "JASPER_GIT_SHA=cafe123\nJASPER_GIT_SHA_FULL=cafe123456789\n"
        "JASPER_GIT_BRANCH=release\nJASPER_INSTALL_AT=2026-01-01T00:00:00-00:00\n"
        "JASPER_INSTALL_STATUS=ok\n",
        encoding="utf-8",
    )
    result = _run_install_snippet(
        "write_build_manifest",
        state_dir=state,
        repo_dir=tmp_path / "not-a-git-repo",  # no env, no git → prior manifest
    )
    assert result.returncode == 0, result.stderr
    manifest = (state / "build.txt").read_text(encoding="utf-8")
    assert "JASPER_GIT_SHA=cafe123" in manifest
    assert "JASPER_GIT_SHA_FULL=cafe123456789" in manifest
    assert "JASPER_GIT_BRANCH=release" in manifest
    assert "JASPER_INSTALL_STATUS=ok" in manifest


def test_write_build_manifest_is_atomic_tempfile_rename():
    """The success marker must be written tempfile-then-rename so a torn
    write (power loss mid-`cat`) can't leave a half-line the direction
    guard misreads. Mirrors persist_install_profile."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    assert "build.txt.tmp.$$" in text
    assert 'mv -f "${tmp}" "${STATE_DIR}/build.txt"' in text


def test_resolve_build_sha_short_prefers_deploy_env(tmp_path):
    result = _run_install_snippet(
        "resolve_build_sha_short",
        state_dir=tmp_path,
        repo_dir=tmp_path,  # non-git, so the env var must be what's used
        deploy_env={"JASPER_DEPLOY_SHA": "feedface"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "feedface"


def test_resolve_build_sha_short_falls_back_to_prior_manifest(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "build.txt").write_text(
        "JASPER_GIT_SHA=deadbee\nJASPER_GIT_SHA_FULL=deadbeef00\n"
        "JASPER_GIT_BRANCH=main\nJASPER_INSTALL_STATUS=ok\n",
        encoding="utf-8",
    )
    result = _run_install_snippet(
        "resolve_build_sha_short",
        state_dir=state,
        repo_dir=tmp_path / "not-a-git-repo",  # no env, no git → prior manifest
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "deadbee"


def test_resolve_build_sha_short_unknown_when_nothing_available(tmp_path):
    result = _run_install_snippet(
        "resolve_build_sha_short",
        state_dir=tmp_path,  # no build.txt
        repo_dir=tmp_path / "not-a-git-repo",  # no env, no git
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unknown"


def test_build_manifest_not_written_during_python_runtime_install():
    """The early write (the bug) is gone: neither install_jasper nor
    install_streambox_jasper *calls* write_build_manifest — it's main()'s
    final step now. (Comment references to it are fine; a bare-command
    invocation is the regression we forbid.)"""
    python_runtime = _PYTHON_RUNTIME_LIB.read_text(encoding="utf-8")
    call_lines = [
        ln for ln in python_runtime.splitlines()
        if ln.strip() == "write_build_manifest"
    ]
    assert not call_lines, (
        "write_build_manifest must not be CALLED from the python-runtime "
        "install steps (it is main()'s final, verified-success step): "
        f"{call_lines}"
    )


def test_build_manifest_is_the_final_main_mutation():
    """In both main() paths the manifest is written immediately before the
    (non-mutating) run_doctor_summary, after the build steps, and nothing
    mutating runs after it — so reaching it means every build/install step
    succeeded under set -e (the load-bearing invariant behind problem #4)."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    # Adjacency: write_build_manifest directly precedes run_doctor_summary
    # in both the full and streambox paths (whitespace-tolerant).
    adjacency = re.findall(r"write_build_manifest\n\s*run_doctor_summary", text)
    assert len(adjacency) == 2, adjacency

    # Bounded main() body (def line to its column-0 closing brace), so the
    # file's trailing `if main "$@"` guard isn't counted.
    match = re.search(r"\nmain\(\) \{\n(.*?)\n\}", text, re.DOTALL)
    assert match is not None, "could not locate main() body"
    body = match.group(1)
    # The Rust final-output owner (a required, OOM-capable build) is built
    # before the manifest is stamped.
    assert body.index("build_install_jasper_outputd") < body.index(
        "write_build_manifest"
    )
    # run_doctor_summary is the TERMINAL step: nothing mutating follows the
    # manifest stamp. After the last run_doctor_summary call, only branch-
    # closing tokens remain (return 0 / fi / comments / whitespace).
    tail = body[body.rindex("run_doctor_summary") + len("run_doctor_summary"):]
    leftover = [
        ln.strip()
        for ln in tail.splitlines()
        if ln.strip()
        and not ln.strip().startswith("#")
        and ln.strip() not in {"return 0", "fi"}
    ]
    assert not leftover, f"unexpected steps after run_doctor_summary: {leftover}"


def test_landing_page_app_css_version_uses_resolved_build_sha():
    """Now that build.txt is written last, the landing-page cache-bust must
    resolve the SHA directly (deploy env → git → prior manifest), not read
    the not-yet-updated manifest — or a deploy would ship the prior SHA's
    cache key and browsers wouldn't bust the /assets cache."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("install_management_static_assets() {")
    fn = text[start: text.index("\ninstall_nginx_site() {", start)]
    assert 'app_css_ver="$(resolve_build_sha_short)"' in fn
    # The old direct manifest read for the cache-bust version is gone.
    assert "grep -E '^JASPER_GIT_SHA='" not in fn


# build-sandbox.sh — unified RAM-aware + cgroup-contained build policy
# ----------------------------------------------------------------------
# Workstream A of docs/install-update-resilience-plan.md. On jts2
# (1 GB Pi 5, 2026-06-21) an unbounded `meson compile` OOM-killed the
# build AND cascaded into nginx + jasper-voice. These tests pin the two
# levers that generalize the PR #899 point-fix: RAM-aware `-j`
# (_ram_bounded_jobs) and cgroup containment (run_contained_build), and
# the inverse-of-audio-daemon policy that makes a build the OOM victim
# instead of a daemon. Canonical doc: docs/HANDOFF-build-sandbox.md.

_BUILD_SANDBOX_LIB = _INSTALL_LIB_DIR / "build-sandbox.sh"
_RUST_DAEMONS_LIB = _INSTALL_LIB_DIR / "rust-daemons.sh"


def _ram_bounded_jobs(memtotal_kb: int, ncpu: int, kb_per_job: int) -> int:
    """Invoke the generalized `_ram_bounded_jobs` helper."""
    result = subprocess.run(
        ["bash", "-c",
         f"source {_INSTALL_SH} >/dev/null && "
         f"_ram_bounded_jobs {memtotal_kb} {ncpu} {kb_per_job}"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helper failed (rc={result.returncode}): {result.stderr}")
    return int(result.stdout.strip())


def _build_sandbox_props(
    tmp_path: Path,
    label: str = "demo",
    memtotal_kb: int = 1_014_768,
    extra_env: dict | None = None,
) -> str:
    """Return the systemd-run property list build_sandbox_props emits,
    with a synthetic MemTotal injected so MemoryHigh is deterministic."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {memtotal_kb} kB\n", encoding="utf-8")
    env = os.environ.copy()
    env["JASPER_BUILD_MEMINFO_FILE"] = str(meminfo)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
         f"build_sandbox_props {shlex.quote(label)}"],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _run_contained_build(
    script_tail: str, extra_env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && {script_tail}"],
        capture_output=True, text=True, timeout=10, env=env,
    )


# --- _ram_bounded_jobs: generalized RAM-aware parallelism --------------

def test_ram_bounded_jobs_cpp_budget_matches_webrtc_pin():
    """C++ -O3 budget (1.5 GB/job): the exact PR #899 values, now via the
    generalized helper."""
    assert _ram_bounded_jobs(_PI5_1GB_MEMTOTAL_KB, 4, 1_500_000) == 1
    assert _ram_bounded_jobs(_PI5_4GB_MEMTOTAL_KB, 4, 1_500_000) == 2
    assert _ram_bounded_jobs(_PI5_8GB_MEMTOTAL_KB, 4, 1_500_000) == 4


def test_ram_bounded_jobs_c_budget_is_gentler_than_old_j4_on_1gb():
    """C autotools budget (0.4 GB/job): a 1 GB Pi builds shairport at -j2,
    not the old hardcoded -j4 that helped drive the OOM. A ~400 MB
    Zero-class box drops to -j1."""
    assert _ram_bounded_jobs(_PI5_1GB_MEMTOTAL_KB, 4, 400_000) == 2
    assert _ram_bounded_jobs(409_600, 4, 400_000) == 1
    assert _ram_bounded_jobs(_PI5_4GB_MEMTOTAL_KB, 4, 400_000) == 4  # nproc cap


def test_ram_bounded_jobs_clamps_to_nproc_and_floors_to_one():
    assert _ram_bounded_jobs(_PI5_16GB_MEMTOTAL_KB, 2, 1_500_000) == 2
    assert _ram_bounded_jobs(0, 4, 400_000) == 1
    assert _ram_bounded_jobs(1024, 4, 1_500_000) == 1


def test_webrtc_compile_jobs_delegates_to_ram_bounded_jobs():
    """The point-fix function stays value-identical to the generalized
    helper at the C++ budget across the SKU range — so the existing
    _webrtc_compile_jobs regression tests keep meaning the same thing."""
    for m in (
        _PI5_1GB_MEMTOTAL_KB, _PI5_2GB_MEMTOTAL_KB,
        _PI5_4GB_MEMTOTAL_KB, _PI5_8GB_MEMTOTAL_KB,
    ):
        assert _webrtc_compile_jobs(m, 4) == _ram_bounded_jobs(m, 4, 1_500_000)


# --- build_sandbox_props: the inverse-of-audio-daemon policy -----------

def test_build_sandbox_props_is_inverse_of_audio_daemon_policy(tmp_path):
    """A build is the opposite of an audio daemon: preferred OOM victim,
    yields CPU/IO, and — the load-bearing invariant — is ALLOWED to swap.
    jts-audio.slice and pi-run-diagnostic.sh forbid swap; a build must
    not, or a big -O3 TU can't complete on a 1 GB Pi."""
    props = _build_sandbox_props(tmp_path)
    # OOMScoreAdjust is an exec property that a `systemd-run --scope` unit
    # REJECTS ("Unknown assignment"), which broke every deploy; the positive
    # "kill me first" oom_score_adj is applied to the build process via choom
    # instead (see test_build_sandbox_oom_prefix_*). Pin its ABSENCE from the
    # scope property list so the regression cannot return.
    assert "OOMScoreAdjust" not in props
    assert "--property=CPUWeight=20" in props
    assert "--property=IOWeight=20" in props
    assert "--property=MemoryAccounting=yes" in props
    assert "MemorySwapMax" not in props  # swap allowed — the inverse invariant


@pytest.mark.skipif(
    shutil.which("choom") is None,
    reason="choom (util-linux) absent on this host; verified on the Linux Pi/CI",
)
def test_build_sandbox_oom_prefix_uses_choom_kill_me_first():
    """The build's positive "kill me first" oom_score_adj is applied via choom
    — the scope-compatible substitute for the OOMScoreAdjust exec property a
    `systemd-run --scope` unit cannot take (the #922 deploy-breaker). choom
    ships in util-linux, present on the Linux CI runner and the Pi.
    (Fail-open when choom is absent is a one-line `command -v` guard in
    build_sandbox_oom_prefix — the build runs without the bias, never blocked.)"""
    r = _run_contained_build("build_sandbox_oom_prefix")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["choom", "-n", "900", "--"]
    r2 = _run_contained_build(
        "build_sandbox_oom_prefix",
        extra_env={"JASPER_BUILD_SANDBOX_OOM_SCORE_ADJ": "750"},
    )
    assert r2.stdout.split() == ["choom", "-n", "750", "--"]


def test_build_sandbox_props_soft_memory_high_leaves_headroom(tmp_path):
    """MemoryHigh is a soft throttle at ~85% MemTotal (not a kill), so the
    build leans on swap rather than dying and ~15% stays for PID1/sshd."""
    props = _build_sandbox_props(tmp_path, memtotal_kb=1_000_000)
    m = re.search(r"--property=MemoryHigh=(\d+)K", props)
    assert m, props
    high_kb = int(m.group(1))
    assert 840_000 <= high_kb <= 860_000


def test_build_sandbox_props_hard_caps_are_opt_in(tmp_path):
    """MemoryMax (hard kill) and RuntimeMaxSec are off by default — a
    too-low cap would kill a legit single-TU compile or a slow Zero-2-W
    build — but available when an operator opts in."""
    default = _build_sandbox_props(tmp_path)
    assert "MemoryMax" not in default
    assert "RuntimeMaxSec" not in default

    opted = _build_sandbox_props(tmp_path, extra_env={
        "JASPER_BUILD_SANDBOX_MEMORY_MAX": "700M",
        "JASPER_BUILD_SANDBOX_RUNTIME_MAX": "45min",
    })
    assert "--property=MemoryMax=700M" in opted
    assert "--property=RuntimeMaxSec=45min" in opted


def test_build_sandbox_props_without_meminfo_still_protects(tmp_path):
    """If MemTotal is unreadable, MemoryHigh is omitted but the cgroup-
    controller-independent scope properties (CPU/IO weight + accounting) are
    always present. (The OOM bias rides choom, not a property — see
    test_build_sandbox_oom_prefix_uses_choom_kill_me_first.)"""
    env = os.environ.copy()
    env["JASPER_BUILD_MEMINFO_FILE"] = str(tmp_path / "does-not-exist")
    result = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && build_sandbox_props x"],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "--property=CPUWeight=20" in result.stdout
    assert "--property=IOWeight=20" in result.stdout
    assert "OOMScoreAdjust" not in result.stdout
    assert "MemoryHigh" not in result.stdout
    assert "MemorySwapMax" not in result.stdout


# --- run_contained_build: graceful degradation, no double-run ----------

def test_build_sandbox_active_false_when_disabled():
    r = _run_contained_build(
        "_build_sandbox_active && echo active || echo inactive",
        extra_env={"JASPER_BUILD_SANDBOX": "0"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("inactive")


def test_build_sandbox_disabled_runs_command_directly():
    """When disabled (CI/macOS/container path), the command runs verbatim
    and unmodified."""
    r = _run_contained_build(
        "run_contained_build demo -- bash -c 'echo ran $((1+1))'",
        extra_env={"JASPER_BUILD_SANDBOX": "0"})
    assert r.returncode == 0, r.stderr
    assert "ran 2" in r.stdout


def test_build_sandbox_propagates_failure_without_double_run():
    """A real build failure surfaces verbatim and is never masked by a
    second uncontained run (the no-retry contract)."""
    r = _run_contained_build(
        "run_contained_build demo -- bash -c 'echo once; exit 3'",
        extra_env={"JASPER_BUILD_SANDBOX": "0"})
    assert r.returncode == 3
    assert r.stdout.count("once") == 1


# --- call-site wiring: every heavy build is bounded + contained --------

def test_install_sources_build_sandbox_lib():
    text = _INSTALL_SH.read_text(encoding="utf-8")
    assert 'source "${REPO_DIR}/deploy/lib/install/build-sandbox.sh"' in text


def test_heavy_builds_route_through_run_contained_build():
    install_sh = _INSTALL_SH.read_text(encoding="utf-8")
    renderers = _RENDERERS_LIB.read_text(encoding="utf-8")
    python_runtime = _PYTHON_RUNTIME_LIB.read_text(encoding="utf-8")
    rust = _RUST_DAEMONS_LIB.read_text(encoding="utf-8")
    assert 'run_contained_build "webrtc-aec3"' in install_sh
    assert 'run_contained_build "jasper-aec3"' in python_runtime
    assert 'run_contained_build "nqptp"' in renderers
    assert 'run_contained_build "shairport-sync"' in renderers
    assert 'run_contained_build "${name}"' in rust


def test_renderer_make_is_ram_bounded_not_hardcoded_j4():
    """The old hardcoded `make -j4` (which helped drive the 1 GB OOM) is
    gone; both renderer C builds derive -j from build_sandbox_jobs at the
    named C budget (not a bare literal)."""
    renderers = _RENDERERS_LIB.read_text(encoding="utf-8")
    assert "make -j4" not in renderers
    assert renderers.count(
        'make -j"$(build_sandbox_jobs "${BUILD_SANDBOX_KB_PER_JOB_C}")"'
    ) == 2


def test_build_sandbox_budget_constants_are_named_once():
    """The per-toolchain RAM/job budgets are the policy — named once in the
    lib (cf. RUST_LOW_MEMORY_BUILD_THRESHOLD_KB), not bare literals scattered
    across call sites."""
    lib = _BUILD_SANDBOX_LIB.read_text(encoding="utf-8")
    assert "BUILD_SANDBOX_KB_PER_JOB_CPP=1500000" in lib
    assert "BUILD_SANDBOX_KB_PER_JOB_C=400000" in lib
    # _webrtc_compile_jobs uses the named C++ budget, not a bare literal.
    assert 'BUILD_SANDBOX_KB_PER_JOB_CPP' in _INSTALL_SH.read_text(encoding="utf-8")


def test_build_sandbox_lib_parses():
    result = subprocess.run(
        ["bash", "-n", str(_BUILD_SANDBOX_LIB)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr


def _build_sandbox_jobs(kb_per_job_expr: str, memtotal_kb: int, ncpu: int,
                        tmp_path: Path) -> int:
    """Invoke the build_sandbox_jobs wrapper with MemTotal + nproc injected
    (mirrors rust-daemons.sh's JASPER_RUST_MEMINFO_FILE test pattern)."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {memtotal_kb} kB\n", encoding="utf-8")
    env = os.environ.copy()
    env["JASPER_BUILD_MEMINFO_FILE"] = str(meminfo)
    env["JASPER_BUILD_NPROC"] = str(ncpu)
    result = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
         f"build_sandbox_jobs {kb_per_job_expr}"],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_build_sandbox_jobs_reads_injected_meminfo_and_nproc(tmp_path):
    """The wrapper resolves the host's MemTotal/nproc and applies the named
    budget — the path the renderer makes actually call."""
    # C budget on a 1 GB Pi, 4 cores -> 2 jobs; 1 core clamps to 1.
    assert _build_sandbox_jobs(
        '"${BUILD_SANDBOX_KB_PER_JOB_C}"', 1_014_768, 4, tmp_path) == 2
    assert _build_sandbox_jobs(
        '"${BUILD_SANDBOX_KB_PER_JOB_C}"', 1_014_768, 1, tmp_path) == 1
    # C++ budget on the same box -> 1 job (the OOM we fixed).
    assert _build_sandbox_jobs(
        '"${BUILD_SANDBOX_KB_PER_JOB_CPP}"', 1_014_768, 4, tmp_path) == 1


def test_run_contained_build_invokes_systemd_run_scope_with_policy(tmp_path):
    """Force containment with a stub systemd-run on PATH and assert the
    invocation contract: --scope, the inverse-policy properties, the --
    separator, and the verbatim command. Without this, a refactor could
    silently re-add the OOMScoreAdjust property a --scope unit rejects, or
    add a swap cap, and the rest of the suite would stay green."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemd-run"
    stub.write_text(
        '#!/usr/bin/env bash\nfor a in "$@"; do printf "ARG=%s\\n" "$a"; done\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       1014768 kB\n", encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["JASPER_BUILD_SANDBOX"] = "1"  # force on; the stub satisfies command -v
    env["JASPER_BUILD_MEMINFO_FILE"] = str(meminfo)
    result = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
         "run_contained_build demo -- echo HELLO WORLD"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert result.returncode == 0, result.stderr
    args = [ln[len("ARG="):] for ln in result.stdout.splitlines()
            if ln.startswith("ARG=")]
    assert "--scope" in args
    # The OOMScoreAdjust exec property a --scope unit rejects must NOT be here
    # (the #922 deploy-breaker); the OOM bias rides choom in the command instead.
    assert not any("OOMScoreAdjust" in a for a in args)
    assert "--property=MemoryAccounting=yes" in args
    # Inverse-policy: a build must never get a swap cap.
    assert not any("MemorySwapMax" in a for a in args)
    # Command forwarded verbatim, after the -- separator.
    assert "--" in args
    assert args[-3:] == ["echo", "HELLO", "WORLD"]
