# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One renderer-native package owner shared by both install profiles."""

from pathlib import Path
import re
import shlex
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy/install.sh"
SHARED_PACKAGES = [
    "autoconf", "automake", "libtool", "pkg-config",
    "libpopt-dev", "libconfig-dev", "libavahi-client-dev",
    "libssl-dev", "libsoxr-dev", "libplist-dev", "libsodium-dev",
    "libgcrypt20-dev", "uuid-dev", "libmbedtls-dev", "libglib2.0-dev",
    "libavutil-dev", "libavcodec-dev", "libavformat-dev",
    "libswresample-dev", "xxd", "bluez-alsa-utils", "rfkill", "avahi-daemon",
    "avahi-utils",
]


def _function_body(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start)
    return source[start:end]


def test_full_and_streambox_profiles_call_one_package_owner() -> None:
    source = INSTALL.read_text()
    assert "_install_renderer_native_deps() {" in source
    for name in ("install_deps", "install_streambox_deps"):
        body = _function_body(source, name)
        assert body.count("_install_renderer_native_deps") == 1
        for package in SHARED_PACKAGES:
            assert package not in body, f"{name} duplicates {package}"


def test_shared_owner_installs_the_complete_renderer_package_set() -> None:
    command = f"""
source {shlex.quote(str(INSTALL))} >/dev/null
apt-get() {{ printf '%s\\n' "$*"; }}
_install_renderer_native_deps
"""
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    argv = shlex.split(result.stdout.strip())
    assert argv == ["install", "-y", "--no-install-recommends", *SHARED_PACKAGES]


def test_dry_run_plans_assign_shared_packages_to_the_shared_owner() -> None:
    source = INSTALL.read_text()
    plans = (
        (
            "print_install_plan",
            "Core runtime/build packages:",
            "Renderer and Bluetooth/AirPlay build packages:",
        ),
        (
            "print_streambox_install_plan",
            "Streambox renderer/DSP stack runtime/build packages:",
            "Renderer/Bluetooth/AirPlay packages and build inputs:",
        ),
    )
    for function, profile_heading, shared_heading in plans:
        body = _function_body(source, function)
        profile_section = body.split(profile_heading, 1)[1].split(shared_heading, 1)[0]
        shared_section = body.split(shared_heading, 1)[1].split("\n\n", 1)[0]
        for package in SHARED_PACKAGES:
            pattern = rf"(?<![\w-]){re.escape(package)}(?![\w-])"
            assert not re.search(pattern, profile_section), (
                f"{function} profile package section duplicates {package}"
            )
            assert re.search(pattern, shared_section), (
                f"{function} shared package section omits {package}"
            )
