# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One renderer-native package owner shared by both install profiles."""

from pathlib import Path
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
    "libswresample-dev", "xxd", "libplist-utils",
    "bluez-alsa-utils", "rfkill", "avahi-daemon",
    "avahi-utils",
]


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
