#!/usr/bin/env bash
# Install jasper voice daemon + always-on CamillaDSP on a Raspberry Pi.
#
# Source-builds shairport-sync (AirPlay 2) + nqptp, drops in
# librespot (rust, via raspotify .deb) + bluez-alsa + JTS no-code
# Bluetooth pairing agent,
# owns the full systemd unit per renderer.
#
# Idempotent: re-running upgrades the venv and re-applies configs.
#
# Pre-reqs the operator handles by hand:
#   - Raspberry Pi OS Lite (Trixie, 64-bit) on a Pi 5 (2GB recommended,
#     1GB also fits). SSH + Wi-Fi pre-configured via Imager.
#   - Apple USB-C dongle plugged in. Speakers connected and the amp
#     turned on.
#   - /etc/jasper/jasper.env populated from .env.example with
#     GEMINI_API_KEY set.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INSTALL_DIR="/opt/jasper"
CAMILLA_DIR="/opt/camilladsp"
CAMILLA_CONF="/etc/camilladsp"
ENV_DIR="/etc/jasper"
STATE_DIR="/var/lib/jasper"
SYSTEMD_DIR="/etc/systemd/system"

source "${REPO_DIR}/deploy/lib/jasper-asound-render.sh"
source "${REPO_DIR}/deploy/lib/install/env-migrations.sh"
source "${REPO_DIR}/deploy/lib/install/memory-resilience.sh"
source "${REPO_DIR}/deploy/lib/install/renderers.sh"
source "${REPO_DIR}/deploy/lib/install/web-assets.sh"

CAMILLA_VERSION="v4.1.3"
CAMILLA_TARBALL="camilladsp-linux-aarch64.tar.gz"
CAMILLA_SHA256="d9a17092923ebfe5d20a770c6b6a7eb2268f9700f999bf604b9db09f518aca5a"
CAMILLA_URL="https://github.com/HEnquist/camilladsp/releases/download/${CAMILLA_VERSION}/${CAMILLA_TARBALL}"

# Versions for source builds (debian backend only).
# raspotify ships librespot (rust) 0.8.0 as an arm64 .deb. We use
# this instead of go-librespot because rust librespot supports
# `--volume-ctrl log` for a perceptually linear volume slider —
# go-librespot has a hardcoded cubic curve that concentrates
# dynamic range at the top of the slider (unusable on real
# speakers). See docs/HANDOFF-volume.md for full rationale.
RASPOTIFY_VERSION="0.48.1"
RASPOTIFY_URL="https://github.com/dtcooper/raspotify/releases/download/${RASPOTIFY_VERSION}/raspotify_${RASPOTIFY_VERSION}.librespot.v0.8.0-ea81314_arm64.deb"
RASPOTIFY_SHA256="dc1bc4d209378ef1f8348fd7aa6d1a7865fa83abc30c08990d171012d038a717"
SHAIRPORT_SYNC_VERSION="4.3.7"
SHAIRPORT_SYNC_COMMIT="0b1c4391ffd398e7b145eb4b98416261380adeea"
NQPTP_COMMIT="c925f27c1fd12e4033ac477e5a405969b0b0260b"
NQPTP_ARCHIVE_URL="https://github.com/mikebrady/nqptp/archive/${NQPTP_COMMIT}.tar.gz"
NQPTP_SHA256="d2c2fe5d2574d447a817b1585e82c38f4c98774dac8284e5a3f17e188a3a75f9"
SHAIRPORT_SYNC_ARCHIVE_URL="https://github.com/mikebrady/shairport-sync/archive/${SHAIRPORT_SYNC_COMMIT}.tar.gz"
SHAIRPORT_SYNC_SHA256="7ef3a6ba1cbd67bb200f018ddcd3e8dbe40da98b3c1776aee6c7b832632c6865"
WEBRTC_AEC3_VERSION="v2.1"
WEBRTC_AEC3_COMMIT="846fe90a289f58b7c9303a635142aa2c7caa93e5"
WEBRTC_AEC3_ARCHIVE_URL="https://gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing/-/archive/${WEBRTC_AEC3_COMMIT}/webrtc-audio-processing-${WEBRTC_AEC3_COMMIT}.tar.gz"
WEBRTC_AEC3_SHA256="ddf4e540b9f4291e140cc2ab4560f3eb4fce07ef6212a94d980843bfbf9a4588"

print_install_usage() {
    cat <<'EOF'
Usage: bash deploy/install.sh [--dry-run|--plan]

Options:
  --dry-run, --plan   Print the install plan and exit without requiring root.
  -h, --help          Show this help.

Environment:
  JASPER_INSTALL_DRY_RUN=1   Same as --dry-run.
  JASPER_HOSTNAME=<name>.local
                             Speaker identity/cert hostname for direct
                             Pi-local installs. scripts/deploy-to-pi.sh
                             forwards this automatically.
EOF
}

# Pi-generated pip constraints (scripts/generate-pi-constraints.sh).
# Echoes the file path when the repo carries one, nothing otherwise —
# the install path turns that into `-c <file>` args for the unpinned
# pip installs, and a missing file is a graceful no-op (open-range
# resolution, the pre-constraints behavior). Kept as a tiny helper so
# tests can source install.sh and pin the contract.
jasper_pip_constraints_file() {
    local constraints="${REPO_DIR}/deploy/constraints-pi.txt"
    if [[ -f "${constraints}" ]]; then
        printf '%s\n' "${constraints}"
    fi
}

print_install_plan() {
    cat <<EOF
==> JTS install plan (dry run)

No host changes are made in this mode. The plan is intentionally static:
it describes the installer surfaces and conditional checks, then exits
before the root check, apt, downloads, file writes, systemd, or restarts.
The real installer remains the source of truth for exact host-specific
no-op decisions.

Run for real from a Pi-local checkout:
  sudo JASPER_HOSTNAME=<hostname>.local bash deploy/install.sh

1. System packages
   - apt-get update.
   - Core runtime/build packages:
     python3 python3-venv python3-dev build-essential libasound2-dev
     libasound2 portaudio19-dev libasound2-plugins libsndfile1 curl
     ca-certificates rsync dfu-util libwebrtc-audio-processing-dev
     pkg-config meson ninja-build nginx-light openssl rustc cargo.
   - Renderer and Bluetooth/AirPlay build packages:
     autoconf automake libtool libpopt-dev libconfig-dev
     libavahi-client-dev libssl-dev libsoxr-dev libplist-dev
     libsodium-dev libgcrypt20-dev uuid-dev libmbedtls-dev
     libglib2.0-dev libavutil-dev libavcodec-dev libavformat-dev
     libswresample-dev xxd bluez-alsa-utils avahi-daemon
     avahi-utils.

2. Downloaded or built inputs
   - CamillaDSP: ${CAMILLA_URL}
     sha256=${CAMILLA_SHA256}
   - Raspotify/librespot deb: ${RASPOTIFY_URL}
     sha256=${RASPOTIFY_SHA256}
   - nqptp source archive: ${NQPTP_ARCHIVE_URL}
     commit=${NQPTP_COMMIT}
     sha256=${NQPTP_SHA256}
   - shairport-sync source archive: ${SHAIRPORT_SYNC_ARCHIVE_URL}
     ref=${SHAIRPORT_SYNC_VERSION}, commit=${SHAIRPORT_SYNC_COMMIT}
     sha256=${SHAIRPORT_SYNC_SHA256}
   - WebRTC AEC3 v2 source archive: ${WEBRTC_AEC3_ARCHIVE_URL}
     ref=${WEBRTC_AEC3_VERSION}, commit=${WEBRTC_AEC3_COMMIT}
     sha256=${WEBRTC_AEC3_SHA256}
   - CamillaGUI 4.1.0 bundle selected by uname -m, sha256-checked.
   - openWakeWord ONNX assets, curated wake models, and DTLN AEC models
     from the Python registries, sha256-checked before staging.
   - Python runtime dependencies from pyproject.toml; openwakeword is
     preinstalled without tflite-runtime because Pi OS Trixie ships
     Python 3.13. When deploy/constraints-pi.txt exists (generated by
     scripts/generate-pi-constraints.sh), the unpinned pip installs
     pass it via -c to replay the reviewed on-Pi resolve.
   - jasper-fanin Rust daemon from rust/jasper-fanin with
     cargo build --release --locked.
   - jasper-outputd daemon from rust/jasper-outputd with
     cargo build --release --locked; enabled as the mainline final-output
     owner.
   - Optional ESP32 dial/satellite firmware only when
     JASPER_BUILD_OPTIONAL_FIRMWARE=1.

3. Runtime files and state
   - Create/update /opt/jasper, /etc/jasper, /var/lib/jasper,
     /opt/camilladsp, /etc/camilladsp, /var/lib/camilladsp,
     /usr/share/jasper-web, and feature-specific state directories.
   - Write /var/lib/jasper/build.txt with deploy SHA/branch metadata
     when available.
   - Write /var/lib/jasper/voice_provider_ids from the Python voice
     catalog so boot/hotplug shell can validate providers without
     importing Python.
   - Copy Python source, jasper_aec3, pyproject.toml, firmware sources,
     landing pages, nginx config, Avahi service templates, systemd
     units, udev rules, ALSA templates, and helper binaries.
   - Render /etc/asound.conf through /usr/local/sbin/jasper-render-asound-conf.

4. Config and migrations
   - Seed /etc/jasper/jasper.env on fresh installs.
   - Migrate wizard-owned keys out of /etc/jasper/jasper.env into
     /var/lib/jasper/* env files for voice provider, transit, weather,
     wake detection legs, multi-room grouping, and WiFi guardian
     recovery.
   - Seed JASPER_SPEAKER_ROOM in /var/lib/jasper/speaker_name.env from
     the legacy peering room (JASPER_PEER_ROOM in peering.env) when the
     identity room is unset; one-time, never overwrites a set room.
   - Seed defaults for speaker name, AirPlay mode, ALSA quality,
     wake model, AEC mode, peer_id, journald persistence, memory
     resilience, and correction TLS CA/cert files.
   - Add Pi boot/config changes when needed: USB gadget dtoverlay,
     memory cgroup/PSI kernel args, MGLRU tmpfiles, sysctl values,
     and rpi-swap zram sizing.
   - Disable WiFi power-save on the active wlan0 connection (nmcli)
     so AirPlay's unicast UDP stream avoids radio-sleep stalls.
   - Remove stale legacy audio-topology state (audio_topology.env,
     dmix-era asound.conf backups); fan-in is the only topology.
   - Remove legacy self-signed HTTPS artifacts (the old
     /etc/nginx/ssl/jasper.* cert and previous-generation nginx
     site files) superseded by the GitHub Pages OAuth bounce page.

5. Services and live actions
   - Reload udev and systemd.
   - Enable socket-activated setup wizards and always-on audio/control
     services.
   - Enable/start or restart renderer services, jasper-fanin,
     jasper-outputd, audio-hardware reconciliation, DAC init,
     headphone monitor, nginx, Avahi, CamillaGUI socket, the WiFi
     guardian, and the boot-loop guard.
   - Require jasper-outputd to be active and answering STATUS before
     voice starts against the final-output path.
   - Seed or validate the outputd Camilla statefile while preserving
     the normal production statefile. Rollback to a pre-outputd
     release/branch must also stop/disable jasper-outputd because that
     older code does not know about the outputd unit.
   - Run the AEC/mic reconciler so voice follows attached hardware.
   - Install the multi-room grouping units (snapserver, snapclient,
     grouping-reconcile) DISABLED. Grouping is never auto-enabled and
     the snapcast apt packages are NOT installed on a solo speaker;
     the /grouping opt-in owns enabling units and fetching binaries.
   - Regenerate audio cues if jasper-cues is installed.
   - Run jasper-doctor as a final non-blocking health summary.

6. Provenance/checks
   - Direct downloads and source-build inputs above are tracked in
     deploy/provenance.toml and checked by:
       python3 scripts/check-provenance.py
   - This dry run is a planning aid for contributors; it is not a
     substitute for a real Pi install/deploy validation before release.
EOF
}

_is_truthy() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

_is_falsey_or_empty() {
    case "${1:-}" in
        ""|0|false|FALSE|no|NO|off|OFF) return 0 ;;
        *) return 1 ;;
    esac
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "this script must be run as root (use sudo)" >&2
        exit 1
    fi
}

# The user the Rust daemon builds run as. build_install_jasper_fanin /
# build_install_jasper_outputd chown their cargo cache dirs to this user
# and `sudo -u` the builds — the appliance-standard account, NOT the
# laptop-side PI_USER deploy transport setting (custom appliance users
# are out of scope; see "Custom user boundary" in AGENTS.md).
BUILD_USER="pi"

require_build_user() {
    # Fail fast, BEFORE any host mutation. Without this preflight a
    # custom-user install died ~15 minutes in, at the first
    # `chown pi:pi` in build_install_jasper_fanin — after apt packages
    # and the renderer stack had already been mutated.
    if getent passwd "${BUILD_USER}" >/dev/null 2>&1; then
        return 0
    fi
    cat >&2 <<EOF
ERROR: required build user '${BUILD_USER}' does not exist on this host.

install.sh builds the Rust audio daemons (jasper-fanin, jasper-outputd)
as the appliance-standard '${BUILD_USER}' user. Custom appliance
users are not supported (PI_USER only covers the deploy/onboarding
transport). Create the user, then re-run the install:

    sudo adduser --disabled-password --gecos "" ${BUILD_USER}

Failing now, before any packages or services were modified.
EOF
    return 1
}

fetch_verified_source_archive() {
    # Fetch-to-temp-then-swap: download, hash-check, and extract into a
    # staging dir first; only replace ${dest_dir} once everything
    # succeeded. The previous shape rm -rf'd the destination BEFORE the
    # curl, so under `set -e` a transient network failure aborted the
    # install with the prior source tree already destroyed. Bounded
    # retries absorb flaky Pi WiFi; --max-time caps a stalled transfer
    # (these archives are a few MB) so the install can't hang forever.
    local url="$1"
    local expected_sha="$2"
    local dest_dir="$3"
    local label="$4"
    local tmpdir archive staging

    tmpdir="$(mktemp -d)"
    archive="${tmpdir}/source.tar.gz"
    staging="${tmpdir}/extracted"

    echo "    fetching ${label} source archive"
    echo "    from: ${url}"
    curl -fsSL --retry 3 --retry-connrefused --max-time 300 \
        -o "${archive}" "${url}"
    echo "${expected_sha}  ${archive}" | sha256sum -c -
    mkdir -p "${staging}"
    tar -xzf "${archive}" -C "${staging}" --strip-components=1
    rm -rf "${dest_dir}"
    mkdir -p "$(dirname "${dest_dir}")"
    mv "${staging}" "${dest_dir}"
    rm -rf "${tmpdir}"
}

install_deps() {
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        build-essential libasound2-dev libasound2 portaudio19-dev \
        libasound2-plugins \
        libsndfile1 curl ca-certificates rsync \
        dfu-util \
        libwebrtc-audio-processing-dev pkg-config \
        meson ninja-build \
        nginx-light openssl \
        rustc cargo
    # rustc + cargo are required to build the Rust audio daemons
    # (rust/jasper-fanin/ and rust/jasper-outputd/). Trixie ships rustc 1.85, comfortably above
    # our crate's rust-version=1.75 floor. See
    # docs/HANDOFF-fan-in-daemon.md "Why Rust" for the language choice.
    # meson + ninja-build are needed by build_webrtc_v2_for_aec3() to
    # compile webrtc-audio-processing v2.1 statically from source. The
    # resulting static archive is what jasper_aec3/setup.py links the
    # `_aec3_v2` binding against (BEST_A AEC config). See
    # docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture plan".
    # libasound2-plugins is REQUIRED for the rate_converter line in
    # deploy/alsa/asoundrc.jasper. Without it ALSA silently falls back
    # to the linear resampler which loses ~12 dB of 4-8 kHz content
    # during 44.1→48 conversion, which sabotages AEC speech-band
    # performance. See docs/HANDOFF-aec.md "Resampler quality".

    # Source-build deps for shairport-sync (AirPlay 2) + nqptp, plus
    # the bluez-alsa userspace and the JTS Bluetooth agent. All of these
    # are absent on a stock Trixie Lite image.
    #
    # `avahi-daemon` is the mDNS *publisher* — Pi OS Lite ships
    # `libnss-mdns` (resolution only) by default but does NOT install
    # the daemon, so without this line `<hostname>.local` from another
    # device fails to find us, `_jasper-control._tcp` isn't advertised
    # to the dial, and `avahi-utils` tools have no daemon to talk to.
    # `avahi-utils` provides avahi-browse / avahi-publish for diagnostics.
    apt-get install -y --no-install-recommends \
        autoconf automake libtool pkg-config \
        libpopt-dev libconfig-dev libavahi-client-dev \
        libssl-dev libsoxr-dev libplist-dev libsodium-dev \
        libgcrypt20-dev uuid-dev libmbedtls-dev libglib2.0-dev \
        libavutil-dev libavcodec-dev libavformat-dev libswresample-dev \
        xxd \
        bluez-alsa-utils avahi-daemon avahi-utils
}

build_webrtc_v2_for_aec3() {
    # Build webrtc-audio-processing v2.1 statically into
    # /opt/jasper/.cache/webrtc-aec3-v2/src/builddir/, then export
    # JASPER_WEBRTC_V2_PREFIX for the caller. jasper_aec3/setup.py
    # reads this env var (as WEBRTC_AEC3_V2_PREFIX) and links its
    # `_aec3_v2` binding against the static archive.
    #
    # Why v2.1 vendored + static: Debian Trixie's apt
    # libwebrtc-audio-processing-1 v1.3-3 doesn't expose
    # EchoCanceller3Factory in its public headers, so the deep
    # suppressor / ERLE / stationarity knobs that BEST_A relies on
    # are unreachable. Mirroring PipeWire 1.4's pattern, we vendor
    # v2.1 from the upstream pulseaudio fork and link statically —
    # we own both sides of the ABI boundary; no Debian-rebuild risk.
    # See HANDOFF-aec.md section E + experiments/aec3-v2-deep-tune-spike/.
    #
    # First-run cost: ~3-5 min on Pi 5. Re-runs are no-ops thanks to
    # the static-archive existence check.
    local cache_dir="/opt/jasper/.cache/webrtc-aec3-v2"
    local src_dir="${cache_dir}/src"
    local build_dir="${src_dir}/builddir"
    local static_archive="${build_dir}/webrtc/modules/audio_processing/libwebrtc-audio-processing-2.a"
    local provenance_marker="${cache_dir}/source.archive"
    local source_id="${WEBRTC_AEC3_COMMIT}:${WEBRTC_AEC3_SHA256}"
    local repo_tag="${WEBRTC_AEC3_VERSION}"

    if [[ -f "${static_archive}" ]]; then
        if [[ -f "${provenance_marker}" ]] \
           && [[ "$(cat "${provenance_marker}")" == "${source_id}" ]]; then
            echo "  webrtc-audio-processing v2.1 already built at ${static_archive}"
            export JASPER_WEBRTC_V2_PREFIX="${cache_dir}"
            return 0
        fi
        echo "  webrtc-audio-processing cache lacks expected provenance; rebuilding"
        rm -rf "${src_dir}"
    fi

    echo "  building webrtc-audio-processing ${repo_tag} statically (first run, ~3-5 min)..."
    mkdir -p "${cache_dir}"

    fetch_verified_source_archive \
        "${WEBRTC_AEC3_ARCHIVE_URL}" \
        "${WEBRTC_AEC3_SHA256}" \
        "${src_dir}" \
        "webrtc-audio-processing ${repo_tag} (${WEBRTC_AEC3_COMMIT})"

    if [[ ! -f "${build_dir}/build.ninja" ]]; then
        echo "    meson setup builddir/"
        (cd "${src_dir}" && meson setup builddir \
            -Ddefault_library=static \
            -Db_pie=true \
            -Dc_args=-fPIC \
            -Dcpp_args=-fPIC \
            --buildtype=release)
    fi

    echo "    meson compile -C builddir/"
    (cd "${src_dir}" && meson compile -C builddir)

    if [[ ! -f "${static_archive}" ]]; then
        echo "  ERROR: meson compile finished but ${static_archive} is missing" >&2
        echo "  WEBRTC_AEC3_V2_PREFIX will not be set; setup.py will skip _aec3_v2" >&2
        return 1
    fi

    echo "${source_id}" > "${provenance_marker}"
    echo "  → static archive: ${static_archive} ($(du -h "${static_archive}" | cut -f1))"
    export JASPER_WEBRTC_V2_PREFIX="${cache_dir}"
}

build_install_jasper_fanin() {
    # Build the jasper-fanin Rust daemon (rust/jasper-fanin/) and
    # install the release binary to /opt/jasper/bin/jasper-fanin.
    #
    # See docs/HANDOFF-fan-in-daemon.md for the daemon's design and
    # resilience contract. Fan-in is the production renderer topology;
    # install_systemd_units enables the daemon and install_alsa writes
    # the matching /etc/asound.conf directly.
    #
    # Build is intentionally done as the appliance-standard `pi` user
    # in a persistent cache dir at /var/cache/jasper-fanin-build so
    # cargo's incremental compilation keeps re-runs fast (~5 s on no
    # source change, ~20 s incremental, ~90 s first run). This is not
    # keyed off the laptop-side PI_USER transport setting: custom
    # users are supported for onboarding/deploy only until the rest of
    # the appliance scripts/services have been audited for non-pi
    # runtime assumptions. Target/ directory stays in the cache; only
    # the release binary is copied to /opt/jasper/bin.
    local src_dir="${REPO_DIR}/rust/jasper-fanin"
    local cache_dir="/var/cache/jasper-fanin-build"
    local bin_dest="/opt/jasper/bin/jasper-fanin"

    if [[ ! -d "${src_dir}" ]]; then
        echo "  jasper-fanin source missing at ${src_dir}; skipping build"
        return 0
    fi

    echo "  building jasper-fanin (Rust daemon)..."
    mkdir -p "${cache_dir}"
    chown "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    # rsync the source tree into the cache dir, preserving cargo's
    # incremental compile state in target/ between runs. --delete
    # removes stale source files (e.g., a renamed module).
    rsync -a --delete \
        --exclude='target/' \
        "${src_dir}/" "${cache_dir}/"
    chown -R "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    # Build as pi so cargo's user cache (~pi/.cargo) is used and the
    # generated artifacts under target/ are pi-owned (operator can
    # clean up without sudo).
    sudo -u "${BUILD_USER}" -H bash -c "cd '${cache_dir}' && cargo build --release --locked --quiet" \
        || { echo "  jasper-fanin build failed; see cargo output above"; return 1; }

    local built_bin="${cache_dir}/target/release/jasper-fanin"
    if [[ ! -x "${built_bin}" ]]; then
        echo "  ERROR: cargo build finished but ${built_bin} is missing" >&2
        return 1
    fi

    mkdir -p /opt/jasper/bin
    install -m 0755 -o root -g root "${built_bin}" "${bin_dest}"
    echo "  → installed ${bin_dest} ($(du -h "${bin_dest}" | cut -f1))"
}

build_install_jasper_outputd() {
    # Build the jasper-outputd Rust daemon and install it to
    # /opt/jasper/bin/jasper-outputd. The systemd unit is enabled as
    # the mainline final-output owner. Pre-outputd rollback must stop
    # and disable this persistent unit before returning to the legacy
    # jasper_out path. Build ownership mirrors jasper-fanin above:
    # the beginner appliance path uses username `pi`, and custom
    # PI_USER is currently a deploy/onboarding transport option rather
    # than full appliance-user support.
    local src_dir="${REPO_DIR}/rust/jasper-outputd"
    local cache_dir="/var/cache/jasper-outputd-build"
    local bin_dest="/opt/jasper/bin/jasper-outputd"

    if [[ ! -d "${src_dir}" ]]; then
        echo "  ERROR: jasper-outputd source missing at ${src_dir}" >&2
        echo "  This tree requires jasper-outputd as the final output owner." >&2
        return 1
    fi

    echo "  building jasper-outputd (Rust daemon)..."
    mkdir -p "${cache_dir}"
    chown "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    rsync -a --delete \
        --exclude='target/' \
        "${src_dir}/" "${cache_dir}/"
    chown -R "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    sudo -u "${BUILD_USER}" -H bash -c "cd '${cache_dir}' && cargo build --release --locked --quiet" \
        || { echo "  jasper-outputd build failed; see cargo output above"; return 1; }

    local built_bin="${cache_dir}/target/release/jasper-outputd"
    if [[ ! -x "${built_bin}" ]]; then
        echo "  ERROR: cargo build finished but ${built_bin} is missing" >&2
        return 1
    fi

    mkdir -p /opt/jasper/bin
    install -m 0755 -o root -g root "${built_bin}" "${bin_dest}"
    echo "  -> installed ${bin_dest} ($(du -h "${bin_dest}" | cut -f1))"
}

require_outputd_ready() {
    if [[ ! -x /opt/jasper/bin/jasper-outputd ]]; then
        echo "  ERROR: /opt/jasper/bin/jasper-outputd is missing or not executable" >&2
        return 1
    fi
    systemctl restart jasper-outputd.service
    systemctl is-active --quiet jasper-outputd.service || {
        echo "  ERROR: jasper-outputd.service did not become active" >&2
        journalctl -u jasper-outputd.service -n 40 --no-pager >&2 || true
        return 1
    }
    python3 - <<'PY'
import json
import socket
import sys
import time

path = "/run/jasper-outputd/control.sock"
deadline = time.monotonic() + 3.0
last_error = None
while time.monotonic() < deadline:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(path)
            sock.sendall(b"STATUS\n")
            body = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
        data = json.loads(body.decode("utf-8", errors="replace"))
        if data.get("backend") != "alsa":
            raise RuntimeError(f"backend={data.get('backend')!r}, expected 'alsa'")
        if data.get("dac", {}).get("pcm") != "outputd_dac":
            raise RuntimeError(f"dac.pcm={data.get('dac', {}).get('pcm')!r}")
        if data.get("content", {}).get("pcm") != "outputd_content_capture":
            raise RuntimeError(f"content.pcm={data.get('content', {}).get('pcm')!r}")
        sys.exit(0)
    except Exception as e:
        last_error = e
        time.sleep(0.1)
print(f"jasper-outputd STATUS probe failed: {last_error}", file=sys.stderr)
sys.exit(1)
PY
}

install_camilladsp() {
    # Belt-and-suspenders: any pre-existing camilladsp.service from a
    # different install lineage shouldn't fight our copy over
    # /etc/asoundrc or the dmix lock.
    systemctl stop camilladsp.service 2>/dev/null || true
    systemctl disable camilladsp.service 2>/dev/null || true

    install -d -m 0755 "${CAMILLA_DIR}" "${CAMILLA_CONF}"
    # State + emitted-correction-config dirs. The legacy/pre-outputd
    # Camilla unit uses /var/lib/camilladsp/statefile.yml so corrections
    # survive Pi restarts; outputd uses outputd-statefile.yml and
    # preserves the normal statefile for rollback. The room-correction
    # wizard writes correction_<id>_<unixtime>.yml under configs/.
    install -d -m 0755 /var/lib/camilladsp /var/lib/camilladsp/configs
    install -d -m 0750 \
        /var/lib/jasper/correction \
        /var/lib/jasper/correction/sweeps \
        /var/lib/jasper/correction/captures \
        /var/lib/jasper/correction/sessions \
        /var/lib/jasper/correction/calibration_mics

    # Seed the statefile if missing. The unit's ExecStart deliberately
    # has NO positional CONFIGFILE — CamillaDSP would clobber the
    # statefile with the positional path on every start, defeating
    # the whole persistence story. Instead, on first install, we
    # write a minimal statefile pointing at v1.yml. Subsequent
    # `set_config_file_path()` calls from the room-correction wizard
    # update this file in place; future restarts read it back.
    # NOTE: this block is idempotent — we never overwrite an existing
    # statefile, so a user who's applied a correction won't have it
    # silently reset by a re-run of install.sh.
    if [[ ! -f /var/lib/camilladsp/statefile.yml ]]; then
        cat > /var/lib/camilladsp/statefile.yml <<'EOF'
config_path: /etc/camilladsp/v1.yml
mute:
- false
- false
- false
- false
- false
volume:
- 0.0
- 0.0
- 0.0
- 0.0
- 0.0
EOF
        chmod 0644 /var/lib/camilladsp/statefile.yml
        echo "  Seeded /var/lib/camilladsp/statefile.yml → v1.yml (no correction yet)"
    fi
    if [[ ! -x "${CAMILLA_DIR}/camilladsp" ]]; then
        local tmpdir
        tmpdir="$(mktemp -d)"
        echo "Fetching CamillaDSP ${CAMILLA_VERSION}..."
        # Bounded retries + transfer cap: same rationale as
        # fetch_verified_source_archive (multi-MB fetch on flaky WiFi).
        curl -fsSL --retry 3 --retry-connrefused --max-time 300 \
            -o "${tmpdir}/${CAMILLA_TARBALL}" "${CAMILLA_URL}"
        echo "${CAMILLA_SHA256}  ${tmpdir}/${CAMILLA_TARBALL}" | sha256sum -c -
        tar -xzf "${tmpdir}/${CAMILLA_TARBALL}" -C "${CAMILLA_DIR}" camilladsp
        chmod +x "${CAMILLA_DIR}/camilladsp"
        rm -rf "${tmpdir}"
        echo "Installed CamillaDSP to ${CAMILLA_DIR}/camilladsp"
    fi

    # CamillaDSP captures plug:jasper_capture (fan-in summed substream 7).
    # v1.yml writes to pcm.jasper_out for pre-outputd rollback;
    # outputd-cutover.yml writes to outputd_content_playback so
    # jasper-outputd owns the DAC on current main. Neither yaml needs
    # substitution — install_alsa() handles the dongle name in
    # /etc/asound.conf.
    install -m 0644 \
        "${REPO_DIR}/deploy/camilladsp/v1.yml" \
        "${CAMILLA_CONF}/v1.yml"
    install -m 0644 \
        "${REPO_DIR}/deploy/camilladsp/outputd-cutover.yml" \
        "${CAMILLA_CONF}/outputd-cutover.yml"

    seed_outputd_statefile() {
        cat > /var/lib/camilladsp/outputd-statefile.yml <<'EOF'
config_path: /etc/camilladsp/outputd-cutover.yml
mute:
- false
- false
- false
- false
- false
volume:
- 0.0
- 0.0
- 0.0
- 0.0
- 0.0
EOF
        chmod 0644 /var/lib/camilladsp/outputd-statefile.yml
    }

    camilla_config_has_safe_volume_limit() {
        local config_path="$1"
        awk '
            /^[[:space:]]*volume_limit:/ {
                value = $0
                sub(/^[^:]*:[[:space:]]*/, "", value)
                sub(/[[:space:]]*#.*/, "", value)
                gsub(/["'\''"]/, "", value)
                found = 1
                if (value ~ /^[-+]?[0-9]+([.][0-9]+)?$/ && value + 0 <= 0) {
                    safe = 1
                }
                exit
            }
            END {
                if (!found || !safe) {
                    exit 1
                }
            }
        ' "${config_path}"
    }

    # The outputd topology uses a separate Camilla statefile
    # instead of overwriting /var/lib/camilladsp/statefile.yml. Preserve
    # a valid outputd correction/sound profile across redeploys, but
    # self-heal if the statefile is missing, points at a deleted config,
    # points at a legacy jasper_out config that would bypass outputd, or
    # omits the 0 dB Camilla volume ceiling.
    if [[ ! -f /var/lib/camilladsp/outputd-statefile.yml ]]; then
        seed_outputd_statefile
        echo "  Seeded /var/lib/camilladsp/outputd-statefile.yml → outputd-cutover.yml"
    else
        local outputd_config
        outputd_config="$(
            awk '/^[[:space:]]*config_path:/ {print $2; exit}' \
                /var/lib/camilladsp/outputd-statefile.yml || true
        )"
        if [[ -z "${outputd_config}" || ! -f "${outputd_config}" ]]; then
            seed_outputd_statefile
            echo "  Reset outputd Camilla statefile → outputd-cutover.yml (missing config)"
        elif ! grep -q 'outputd_content_playback' "${outputd_config}"; then
            seed_outputd_statefile
            echo "  Reset outputd Camilla statefile → outputd-cutover.yml (legacy playback path)"
        elif ! camilla_config_has_safe_volume_limit "${outputd_config}"; then
            seed_outputd_statefile
            echo "  Reset outputd Camilla statefile → outputd-cutover.yml (unsafe volume_limit)"
        else
            echo "  Preserved outputd Camilla statefile → ${outputd_config}"
        fi
    fi

    # NOTE: aec-bridge is no longer a CamillaDSP instance — it's
    # now a Python software AEC daemon (jasper-aec-bridge, see
    # jasper/cli/aec_bridge.py). The chip's on-chip AEC turned out
    # to be incompatible with our external-DAC topology, so we run
    # WebRTC AEC3 on the host using the XVF chip's ASR beam
    # (channel 1 of 6-ch firmware) + the dsnoop-tapped music
    # reference. Old aec-bridge.yml is removed if present from a
    # prior install.
    rm -f "${CAMILLA_CONF}/aec-bridge.yml"
}

find_card() {
    # find_card "<aplay|arecord>" "<grep regex>"
    local tool="$1" regex="$2"
    local card
    card=$("$tool" -L 2>/dev/null \
        | grep -B1 -iE "$regex" \
        | grep -oE 'CARD=[^,]+' \
        | head -1 \
        | sed 's/CARD=//' \
        || true)
    if [[ -n "$card" ]]; then
        echo "$card"
    fi
}

detect_card() {
    # detect_card "<aplay|arecord>" "<grep regex>" "<fallback>"
    local tool="$1" regex="$2" fallback="$3"
    local card
    card=$(find_card "$tool" "$regex" || true)
    if [[ -n "$card" ]]; then
        echo "$card"
    else
        echo "$fallback"
    fi
}

select_audio_hardware_roles() {
    # Hardware roles are intentionally separate. The reconciler owns
    # detection so install, boot, and udev-triggered changes share one
    # policy surface.
    eval "$(bash "${REPO_DIR}/deploy/bin/jasper-audio-hardware-reconcile" --print-env)"
    if [[ "${APPLE_DONGLE_PRESENT}" == "1" ]]; then
        echo "  Apple dongle: CARD=${DONGLE_CARD}"
    else
        echo "  Apple dongle: not detected (fallback CARD=${DONGLE_CARD} for legacy templates)"
    fi
    echo "  Output DAC: CARD=${OUTPUT_DAC_CARD}"
    echo "  Output DAC id: ${OUTPUT_DAC_ID}"
    if [[ -n "${OUTPUT_DAC_ROUTE:-}" ]]; then
        echo "  Output DAC route: $(jasper_asound_log_token "${OUTPUT_DAC_ROUTE}")"
    fi
    export DONGLE_CARD APPLE_DONGLE_PRESENT APPLE_DONGLE_SERVICE_CARD
    export OUTPUT_DAC_CARD OUTPUT_DAC_ID OUTPUT_DAC_RECOGNIZED OUTPUT_DAC_ROUTE
}

install_alsa() {
    install -d -m 0755 /etc/modules-load.d /etc/alsa/conf.d /etc/modprobe.d
    install -m 0644 \
        "${REPO_DIR}/deploy/modules-load.d/snd-aloop.conf" \
        /etc/modules-load.d/snd-aloop.conf
    install -m 0644 \
        "${REPO_DIR}/deploy/modprobe.d/snd-aloop.conf" \
        /etc/modprobe.d/snd-aloop.conf
    # Reload module so the new card config takes effect (idempotent).
    rmmod snd_aloop 2>/dev/null || true
    modprobe snd-aloop || true

    select_audio_hardware_roles

    # /etc/asound.conf provides the system-wide ALSA PCM definitions
    # (per-renderer fan-in lanes, jasper_capture, outputd lanes,
    # jasper_out rollback path, etc.).
    #
    # Location matters: this file MUST be world-readable so that
    # renderer processes running as non-root users (shairport-sync as
    # `shairport-sync`, librespot as `pi`) can resolve the user-space
    # PCM names declared in it. The pre-2026-05-23 location
    # (/root/.asoundrc, mode 0600) was visible only to root, which
    # was fine while renderers wrote to raw/plughw Loopback names (a
    # kernel-built-in shape needing no asoundrc to resolve) but broke
    # AirPlay and Spotify Connect once renderers switched to user-space
    # PCM names. /etc/asound.conf at mode 0644 is the
    # canonical Linux pattern for "ALSA config visible to all users."
    #
    # Migration: any existing /root/.asoundrc gets backed up
    # (.pre-jasper.<unix-ts>) and removed so it can't silently
    # shadow /etc/asound.conf for root processes (ALSA evaluates
    # ~/.asoundrc before /etc/asound.conf).
    if [[ -f /root/.asoundrc && ! -L /root/.asoundrc ]]; then
        cp /root/.asoundrc "/root/.asoundrc.pre-jasper.$(date +%s)"
        rm -f /root/.asoundrc
        echo "  Migrated old /root/.asoundrc to backup (.pre-jasper.*); see PR #223 for why."
    fi
    # Same backup discipline at the new location. Hand-edited or
    # apt-installed /etc/asound.conf files (rare on JTS, but possible)
    # shouldn't be silently overwritten. The grep guard makes this
    # idempotent — once our content is in place, subsequent deploys
    # see `shairport_substream` and skip the backup (no .pre-jasper
    # spam). Symlinks are not backed up here because JTS intentionally
    # replaces /etc/asound.conf with a symlink to its rendered, public
    # ALSA config below.
    if [[ -f /etc/asound.conf && ! -L /etc/asound.conf ]] \
            && ! grep -q "shairport_substream" /etc/asound.conf 2>/dev/null; then
        cp /etc/asound.conf "/etc/asound.conf.pre-jasper.$(date +%s)"
        echo "  Backed up pre-existing /etc/asound.conf (.pre-jasper.*); see PR #223."
    fi
    install -d -m 0755 "${ENV_DIR}" "${STATE_DIR}"
    jasper_asound_render_template \
        "${REPO_DIR}/deploy/alsa/asoundrc.jasper" \
        "${ENV_DIR}/asoundrc.jasper.template"
    chmod 0644 "${ENV_DIR}/asoundrc.jasper.template"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-render-asound-conf" \
        /usr/local/sbin/jasper-render-asound-conf
    if [[ ! -e "${STATE_DIR}/audio_quality.env" ]]; then
        printf 'JASPER_ALSA_RATE_CONVERTER=samplerate_medium\n' \
            > "${STATE_DIR}/audio_quality.env"
        chmod 0644 "${STATE_DIR}/audio_quality.env"
        echo "  /var/lib/jasper/audio_quality.env defaulted to samplerate_medium."
    fi
    install -d -m 0755 /var/lib/jasper-asound
    install -m 0644 \
        "${REPO_DIR}/deploy/alsa/asoundrc.jasper" \
        "${ENV_DIR}/asoundrc.jasper.source"
    /usr/local/sbin/jasper-render-asound-conf
    ln -sfn /var/lib/jasper-asound/asound.conf /etc/asound.conf
    chmod 0644 /var/lib/jasper-asound/asound.conf
    echo "  Wrote /etc/asound.conf with fan-in, outputd lanes, and jasper_out rollback path"
}

# Rebuild an optional satellite firmware .bin from source if (a) it's
# missing, or (b) any firmware input is newer than the staged .bin. This
# is opt-in via JASPER_BUILD_OPTIONAL_FIRMWARE=1; the base speaker
# install should stay focused on appliance runtime, not accessory
# toolchains. Most JTS households won't have ESP32 satellites, and
# first-run PlatformIO pulls ~300-500 MB of ESP32-S3 toolchain.
#
# PIO can live in any of three places (mirrors build.sh's resolution
# order): on PATH, at /opt/jasper/.venv/bin/pio (the wizard's install
# target), or at /home/pi/.platformio/penv/bin/pio (PIO's own
# installer-script default). We accept any of them.
#
# Build runs as the pi user when /home/pi/.platformio exists, so the
# toolchain cache lands in one place and root doesn't end up with a
# duplicate copy. This follows the same user boundary as the Rust
# daemon builds: the public appliance path is username `pi`; custom
# PI_USER is currently onboarding/deploy-only. Otherwise we run as
# whoever invoked install.sh.
#
# Soft-fails: a failed build prints a warning and lets install.sh
# continue. The accessory wizards surface missing/stale bins to the
# user at the moment they choose to onboard accessory hardware.
_newer_firmware_input() {
    local fw_root="$1"
    local bin_path="$2"
    local -a inputs=()
    local input
    for input in "${fw_root}/src" "${fw_root}/include" \
                 "${fw_root}/platformio.ini" "${fw_root}/build.sh"; do
        [[ -e "$input" ]] && inputs+=("$input")
    done
    [[ ${#inputs[@]} -gt 0 ]] || return 0

    find "${inputs[@]}" -type f -newer "${bin_path}" -print -quit 2>/dev/null || true
}

_build_firmware_if_stale() {
    local fw_dir="$1"
    local bin_name="$2"
    local fw_root="${INSTALL_DIR}/firmware/${fw_dir}"
    local bin_path="${fw_root}/${bin_name}"
    local build_script="${fw_root}/build.sh"

    [[ -f "$build_script" ]] || return 0
    [[ -d "${fw_root}/src" ]] || return 0

    local need_build=0
    if [[ ! -f "$bin_path" ]]; then
        need_build=1
    elif [[ -n "$(_newer_firmware_input "$fw_root" "$bin_path")" ]]; then
        need_build=1
    fi
    [[ $need_build -eq 1 ]] || return 0

    # Detect PIO anywhere build.sh would find it.
    local pio_found=0
    if command -v pio >/dev/null 2>&1 \
       || [[ -x "/opt/jasper/.venv/bin/pio" ]] \
       || [[ -x "/home/pi/.platformio/penv/bin/pio" ]]; then
        pio_found=1
    fi

    if [[ $pio_found -ne 1 ]]; then
        echo "==> ${fw_dir} firmware: source newer than staged .bin, but"
        echo "    PlatformIO is not installed on this Pi. The wizard will"
        echo "    skip flashing for ${fw_dir} until PIO is available."
        echo "    To enable, run once on the Pi:"
        echo "      sudo /opt/jasper/.venv/bin/pip install platformio"
        echo "    Then run:"
        echo "      JASPER_BUILD_OPTIONAL_FIRMWARE=1 sudo -E bash deploy/install.sh"
        return 0
    fi

    # Pick the build user. Prefer pi when pi has any PIO state, so a
    # populated toolchain cache gets reused on each rebuild.
    local build_user
    local -a build_cmd
    if [[ -d "/home/pi/.platformio" ]]; then
        build_user="pi"
        build_cmd=(sudo -u pi -H bash "$build_script")
    else
        build_user="$(id -un)"
        build_cmd=(bash "$build_script")
    fi

    echo "==> ${fw_dir} firmware: building as ${build_user} (~30 s incremental, ~5 min first run)"
    if "${build_cmd[@]}"; then
        if [[ -f "$bin_path" ]] && [[ -z "$(_newer_firmware_input "$fw_root" "$bin_path")" ]]; then
            echo "    Staged ${bin_path}"
        else
            echo "==> ${fw_dir} firmware: build completed, but ${bin_path} is still missing or stale"
        fi
    else
        echo "==> ${fw_dir} firmware: build FAILED — wizard will skip flash until next deploy"
    fi
}

install_jasper() {
    install -d -m 0755 "${INSTALL_DIR}"
    install -d -m 0750 "${STATE_DIR}"
    install -d -m 0750 "${ENV_DIR}"
    # Non-secret, manually inspectable validation reports for mic/DAC/profile
    # readiness. Writers use atomic timestamped JSON files.
    install -d -m 0755 -o root -g root "${STATE_DIR}/audio-validation"

    # Build manifest — captures the git SHA + install timestamp at the
    # moment install.sh ran. The /system dashboard reads this to show
    # "version: <sha>, installed <timestamp>". Falls back to "unknown"
    # if REPO_DIR isn't a git checkout (e.g. tarball deploy).
    #
    # Four sources, in priority order:
    #   1. JASPER_DEPLOY_SHA / JASPER_DEPLOY_SHA_FULL / JASPER_DEPLOY_BRANCH
    #      env vars — set by scripts/deploy-to-pi.sh on the laptop before
    #      sudo-running install.sh. This is the only source that works
    #      when the standard rsync deploy excludes .git/ (which it does).
    #   2. Local git checkout in REPO_DIR, if git is installed — this
    #      is a developer convenience for direct checkout installs, not
    #      a base appliance dependency.
    #   3. Existing build.txt — preserve a previously-correct SHA when
    #      install.sh is re-run directly (e.g.
    #      `sudo JASPER_HOSTNAME=<hostname>.local bash deploy/install.sh`
    #      to regen the TLS cert after a hostname change) without the
    #      DEPLOY env vars. Otherwise the manifest gets clobbered back
    #      to "unknown" on every such re-run, surprising the dashboard.
    #   4. "unknown" — tarball deploys with no git info available.
    local git_sha="${JASPER_DEPLOY_SHA:-unknown}"
    local git_full="${JASPER_DEPLOY_SHA_FULL:-unknown}"
    local git_branch="${JASPER_DEPLOY_BRANCH:-unknown}"
    if [[ "${git_sha}" == "unknown" ]] && command -v git >/dev/null 2>&1 && \
       { [[ -d "${REPO_DIR}/.git" ]] || git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; }; then
        git_sha=$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)
        git_full=$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)
        git_branch=$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
    fi
    if [[ "${git_sha}" == "unknown" && -f "${STATE_DIR}/build.txt" ]]; then
        local prior_sha
        prior_sha=$(grep -E '^JASPER_GIT_SHA=' "${STATE_DIR}/build.txt" | head -1 | cut -d= -f2-)
        if [[ -n "${prior_sha}" && "${prior_sha}" != "unknown" ]]; then
            git_sha="${prior_sha}"
            git_full=$(grep -E '^JASPER_GIT_SHA_FULL=' "${STATE_DIR}/build.txt" | head -1 | cut -d= -f2-)
            git_branch=$(grep -E '^JASPER_GIT_BRANCH=' "${STATE_DIR}/build.txt" | head -1 | cut -d= -f2-)
            echo "  preserving build manifest from prior install: ${git_sha} on ${git_branch}"
        fi
    fi
    cat > "${STATE_DIR}/build.txt" <<EOF
JASPER_GIT_SHA=${git_sha}
JASPER_GIT_SHA_FULL=${git_full}
JASPER_GIT_BRANCH=${git_branch}
JASPER_INSTALL_AT=$(date -Iseconds)
EOF
    chmod 0644 "${STATE_DIR}/build.txt"
    echo "  Build manifest: ${git_sha} on ${git_branch}"


    # Per-account Google refresh tokens live under here at mode 0600.
    # Tighten the parent dirs too so non-root processes can't even
    # `ls` the per-household-member token filenames (the names are
    # PII-adjacent — they identify which household members linked
    # accounts). install -d resets perms on existing dirs, so this
    # also tightens any pre-existing 755 left from earlier installs.
    install -d -m 0700 "${STATE_DIR}/google" "${STATE_DIR}/google/tokens"

    rsync -a --delete \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='tests' --exclude='deploy' \
        --exclude='build' --exclude='*.egg-info' \
        "${REPO_DIR}/jasper" "${REPO_DIR}/jasper_aec3" \
        "${REPO_DIR}/pyproject.toml" \
        "${INSTALL_DIR}/"

    # Stage firmware/ next to the package so jasper-{dial,satellite}-onboard
    # find their respective bins (default --bin paths:
    # /opt/jasper/firmware/dial/jasper-dial.bin,
    # /opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin).
    # The .pio build dir is excluded — that's local to whoever ran the
    # per-firmware build.sh and contains absolute paths.
    #
    # NO --delete: build.sh writes each .bin INTO ${INSTALL_DIR}/firmware/
    # (not into the source repo), so --delete would silently remove the
    # staged .bin on every deploy. Verified failure mode: the /dial/
    # wizard's "Force flash" silently skipped flashing after re-deploy
    # because jasper-dial-onboard saw no bin and fell through to its
    # creds-only path. Instead we leave any locally-staged .bin in
    # place. Rebuilds are explicit accessory work: set
    # JASPER_BUILD_OPTIONAL_FIRMWARE=1 when intentionally refreshing
    # staged ESP32 firmware from source.
    if [[ -d "${REPO_DIR}/firmware" ]]; then
        rsync -a \
            --exclude='.pio' --exclude='.pioenvs' --exclude='.piolibdeps' \
            "${REPO_DIR}/firmware" "${INSTALL_DIR}/"

        if [[ "${JASPER_BUILD_OPTIONAL_FIRMWARE:-0}" == "1" ]]; then
            _build_firmware_if_stale "dial" "jasper-dial.bin"
            _build_firmware_if_stale "satellite-amoled" "jasper-satellite-amoled.bin"
        fi
    fi

    if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
        python3 -m venv "${INSTALL_DIR}/.venv"
    fi
    # Pin the installer toolchain exactly. The previous unpinned
    # `--upgrade pip wheel` made every deploy pull whatever PyPI had
    # newest that morning — silent behavior drift (resolver changes,
    # build-isolation changes) on the highest-blast-radius script in
    # the repo. Bump these deliberately, with a deploy to verify.
    #
    # The application dependency tree (pyproject.toml) is open-ranged
    # for several packages (openai>=, scipy>=, onnxruntime>=, ...).
    # When the repo carries a Pi-generated constraints file (arm64 +
    # Python 3.13 resolve different wheels than a laptop, so the lock
    # must be produced on-platform — see
    # scripts/generate-pi-constraints.sh), the unpinned installs below
    # pass it via `-c` so every deploy replays the reviewed resolve.
    # No file → empty args → installs behave exactly as before.
    "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip==26.1.2 wheel==0.47.0

    local -a pip_constraints=()
    local constraints_file
    constraints_file="$(jasper_pip_constraints_file)"
    if [[ -n "${constraints_file}" ]]; then
        echo "  applying Pi-generated pip constraints: ${constraints_file}"
        pip_constraints=(-c "${constraints_file}")
    fi

    # openwakeword 0.6.0 hard-requires tflite-runtime on Linux, but
    # tflite-runtime has no Python 3.13 wheel (and PiOS Trixie ships
    # python3.13 only — no python3.12 in apt). We use ONNX models
    # exclusively (onnxruntime is already in pyproject.toml), so
    # tflite-runtime is never imported at runtime. Pre-install
    # openwakeword without its declared deps, then install its non-tflite
    # runtime deps explicitly. The subsequent editable install of
    # jasper-speaker sees openwakeword==0.6.0 already satisfied.
    "${INSTALL_DIR}/.venv/bin/pip" install --no-deps openwakeword==0.6.0
    "${INSTALL_DIR}/.venv/bin/pip" install "${pip_constraints[@]}" \
        requests tqdm 'scipy>=1.3,<2' 'scikit-learn>=1,<2'

    "${INSTALL_DIR}/.venv/bin/pip" install "${pip_constraints[@]}" -e "${INSTALL_DIR}"

    # jasper_aec3 — pybind11 bindings for WebRTC AEC3. Two engines:
    #   - _aec3      → links against Debian Trixie's apt-installed
    #                  libwebrtc-audio-processing-1 (v1.3-3). Legacy
    #                  fallback engine.
    #   - _aec3_v2   → links statically against vendored
    #                  webrtc-audio-processing v2.1 (built by
    #                  build_webrtc_v2_for_aec3 below). Exposes the
    #                  deep EchoCanceller3Config knobs the v1
    #                  binding can't reach — required for the BEST_A
    #                  config. Built conditionally when the vendored
    #                  static archive exists.
    # See docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture
    # plan" and experiments/aec3-v2-deep-tune-spike/README.md for
    # the BEST_A canonical config + per-knob rationale.
    if [[ -d "${INSTALL_DIR}/jasper_aec3" ]]; then
        # Build vendored v2.1 first (cached after first run); exports
        # WEBRTC_AEC3_V2_PREFIX into the env that setup.py reads.
        build_webrtc_v2_for_aec3

        # Fingerprint-cache the C++ rebuild: skip pip install when
        # nothing the binding depends on has changed.
        #
        # Why this exists: --force-reinstall (kept below) forces a
        # full pip-side rebuild whose --no-cache-defeat is the only
        # way to guarantee setup.py sees WEBRTC_AEC3_V2_PREFIX (pip's
        # wheel cache doesn't key on env vars). But the actual C++
        # compile of aec3_binding_v2.cpp at -O3 takes 1-3 min on Pi 5
        # with ~430 MB peak RAM on cc1plus — wasteful on the ~80%
        # of deploys that don't touch jasper_aec3/.
        #
        # Fingerprint inputs (any change → rebuild):
        #   - mtime + name of every .cpp/.h/.py/pyproject.toml in
        #     jasper_aec3/
        #   - mtime of the vendored libwebrtc-audio-processing-2.a
        #     (rebuilt rarely by build_webrtc_v2_for_aec3)
        #   - Python version (ABI break → rebuild)
        #   - WEBRTC_AEC3_V2_PREFIX value (cache path change → rebuild)
        #
        # Defense-in-depth: even on cache hit, verify the module
        # imports cleanly — catches accidentally-deleted .so files
        # or partial installs between deploys.
        #
        # Escape hatch: `sudo rm /opt/jasper/.cache/jasper_aec3.installed.fingerprint`
        # then re-deploy → unconditional rebuild.
        local marker="${INSTALL_DIR}/.cache/jasper_aec3.installed.fingerprint"
        local fingerprint
        fingerprint=$(
            (
                find "${INSTALL_DIR}/jasper_aec3" -type f \
                    \( -name '*.cpp' -o -name '*.h' \
                       -o -name '*.py' -o -name 'pyproject.toml' \
                       -o -name 'setup.py' -o -name 'setup.cfg' \) \
                    -exec stat -c '%Y %n' {} \; 2>/dev/null | sort
                # Vendored static archive — null if build_webrtc_v2_for_aec3
                # didn't set the prefix, which means we'd be building
                # the v1-only binding (still want to fingerprint that).
                if [[ -n "${JASPER_WEBRTC_V2_PREFIX:-}" ]]; then
                    find "${JASPER_WEBRTC_V2_PREFIX}" -name 'libwebrtc-audio-processing-2.a' \
                        -exec stat -c '%Y %n' {} \; 2>/dev/null
                fi
                "${INSTALL_DIR}/.venv/bin/python" --version 2>&1
                echo "WEBRTC_PREFIX=${JASPER_WEBRTC_V2_PREFIX:-}"
            ) | sha256sum | awk '{print $1}'
        )

        local needs_rebuild=1
        if [[ -f "${marker}" ]] \
           && [[ "$(cat "${marker}")" == "${fingerprint}" ]] \
           && "${INSTALL_DIR}/.venv/bin/python" -c "import jasper_aec3" 2>/dev/null; then
            echo "==> jasper_aec3 source + env unchanged, skipping rebuild"
            echo "    (delete ${marker} to force)"
            needs_rebuild=0
        fi

        if [[ "${needs_rebuild}" == "1" ]]; then
            # --force-reinstall: pip wheel cache only keys on source hash
            # + setuptools metadata, not on env vars. Without --force-reinstall,
            # a previously-cached wheel built without WEBRTC_AEC3_V2_PREFIX
            # (i.e. with only the v1 extension) would be reused even after
            # the vendored v2 build completes. Forcing a rebuild is the
            # simplest way to guarantee setup.py sees the env var and builds
            # both extensions.
            WEBRTC_AEC3_V2_PREFIX="${JASPER_WEBRTC_V2_PREFIX:-}" \
                "${INSTALL_DIR}/.venv/bin/pip" install --force-reinstall --no-deps \
                "${INSTALL_DIR}/jasper_aec3"
            mkdir -p "$(dirname "${marker}")"
            echo "${fingerprint}" > "${marker}"
        fi
    fi

    # openWakeWord package-resource ONNX files. JTS uses ONNX only
    # (tflite-runtime has no Python 3.13 wheel), so install.sh stages
    # the exact package assets from the hash-checked manifest in
    # jasper/wake_models.py.
    local openwakeword_models_dir
    openwakeword_models_dir="$("${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import importlib.util
import pathlib

spec = importlib.util.find_spec("openwakeword")
if spec is None or spec.origin is None:
    raise SystemExit("openwakeword package not installed")
print(pathlib.Path(spec.origin).resolve().parent / "resources" / "models")
PY
)"
    install -d -m 0755 -o root -g root "${openwakeword_models_dir}"
    OPENWAKEWORD_MODELS_DIR="${openwakeword_models_dir}" \
    "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import os
import sys

from jasper.model_downloads import ModelDownloadError, download_model_file, sha256_file
from jasper.wake_models import (
    fallback_openwakeword_assets,
    openwakeword_asset_for_model,
    openwakeword_assets,
    required_openwakeword_assets,
)

REQUIRED_TIMEOUT_SEC = 30.0
REQUIRED_RETRIES = 3
OPTIONAL_TIMEOUT_SEC = 20.0
OPTIONAL_RETRIES = 1
MAX_MODEL_BYTES = 64 * 1024 * 1024


def read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return values


def active_wake_model() -> str:
    model = os.environ.get("JASPER_WAKE_MODEL", "").strip()
    model = read_env_file("/etc/jasper/jasper.env").get(
        "JASPER_WAKE_MODEL", model,
    ).strip()
    model = read_env_file("/var/lib/jasper/wake_model.env").get(
        "JASPER_WAKE_MODEL", model,
    ).strip()
    return model or "hey_jarvis"


def stage_asset(asset, *, required: bool) -> bool:
    dest = os.path.join(models_dir, asset.filename)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        if sha256_file(dest) == asset.download_sha256:
            print(f"  openWakeWord asset present: {asset.filename}")
            return True
        print(f"  openWakeWord asset hash mismatch, re-downloading: {asset.filename}")
        os.unlink(dest)
    print(f"  downloading openWakeWord asset: {asset.filename}")
    print(f"    from: {asset.download_url}")
    print(f"    to:   {dest}")
    try:
        download_model_file(
            asset.download_url,
            dest,
            expected_sha256=asset.download_sha256,
            label=f"openWakeWord asset {asset.filename}",
            timeout_seconds=REQUIRED_TIMEOUT_SEC if required else OPTIONAL_TIMEOUT_SEC,
            retries=REQUIRED_RETRIES if required else OPTIONAL_RETRIES,
            max_bytes=MAX_MODEL_BYTES,
        )
        return True
    except ModelDownloadError as e:
        kind = "required" if required else "optional"
        print(
            f"  {kind} openWakeWord asset failed: {asset.filename}: {e}",
            file=sys.stderr,
        )
        return False

models_dir = os.environ["OPENWAKEWORD_MODELS_DIR"]
required_by_key = {}
for asset in required_openwakeword_assets():
    required_by_key[asset.key] = asset
for asset in fallback_openwakeword_assets():
    required_by_key[asset.key] = asset
active_asset = openwakeword_asset_for_model(active_wake_model())
if active_asset is not None:
    required_by_key[active_asset.key] = active_asset

required_failures = 0
optional_failures = 0
for asset in openwakeword_assets():
    required = asset.key in required_by_key
    if not stage_asset(asset, required=required):
        if required:
            required_failures += 1
        else:
            optional_failures += 1

if optional_failures:
    print(
        f"  warning: {optional_failures} inactive openWakeWord stock asset(s) "
        "failed to download; unavailable rows will be disabled in /wake/.",
        file=sys.stderr,
    )
sys.exit(1 if required_failures else 0)
PY

    # Curated non-bundled wake-word models. The registry lives in
    # jasper/wake_models.py (single source of truth — same data drives
    # the /wake/ picker UI). install.sh reads it via the venv's Python
    # so adding a new model is one edit in wake_models.py + a deploy.
    # Each download is idempotent (skip when the file already exists
    # with non-zero size) and best-effort: a failed download leaves
    # the daemon on the bundled "hey_jarvis" fallback rather than
    # blocking install. See README.md "Acoustic echo cancellation"
    # for the broader wake/AEC architecture.
    install -d -m 0755 -o root -g root /var/lib/jasper/wake
    # Wake-event telemetry directory (HANDOFF-wake-telemetry.md PR 3).
    # Holds wake-events.sqlite3 + per-event WAVs. jasper-voice (running
    # as root via the service unit) creates files mode 0644; future
    # /wake-review/ web UI reads via the standard nginx proxy. Owner
    # root:root mirrors the wake-models dir above.
    install -d -m 0755 -o root -g root /var/lib/jasper/wake-events
    if ! "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import os
import sys

from jasper.model_downloads import ModelDownloadError, download_model_file, sha256_file
from jasper.wake_models import downloadable

failures = 0
for entry in downloadable():
    dest = entry.model
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        if not entry.download_sha256 or sha256_file(dest) == entry.download_sha256:
            print(f"  wake model present: {entry.key} -> {dest}")
            continue
        print(f"  wake model hash mismatch, re-downloading: {entry.key}")
        os.unlink(dest)
    print(f"  downloading wake model: {entry.key}")
    print(f"    from: {entry.download_url}")
    print(f"    to:   {dest}")
    try:
        download_model_file(
            entry.download_url,
            dest,
            expected_sha256=entry.download_sha256,
            label=f"wake model {entry.key}",
            timeout_seconds=30.0,
            retries=2,
            max_bytes=64 * 1024 * 1024,
        )
    except ModelDownloadError as e:
        print(f"  failed: {e}", file=sys.stderr)
        failures += 1

sys.exit(1 if failures else 0)
PY
    then
        echo "  warning: one or more wake-word model downloads failed"
        echo "  affected registry rows may remain unavailable"
        echo "  re-run install.sh once you're online to retry the downloads"
    fi

    # DTLN-aec ONNX model bundle for the triple-stream wake architecture.
    # Registry at jasper/aec_engines/dtln_models.py — same idempotency
    # + best-effort shape as the wake-model download above. The bridge's
    # _select_engine() runs with DTLN disabled when these files are
    # missing, so a failed download degrades gracefully (triple-stream
    # falls back to dual-stream: AEC ON + AEC OFF). Files are hash-
    # verified post-download so a corrupted partial fetch (truncated
    # ONNX = cryptic onnxruntime error at engine init) is caught here.
    install -d -m 0755 -o root -g root /var/lib/jasper/dtln
    if ! "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import os
import sys

from jasper.model_downloads import ModelDownloadError, download_model_file, sha256_file
from jasper.aec_engines.dtln_models import REGISTRY, DTLN_MODELS_DIR

failures = 0
for entry in REGISTRY:
    for path, url, expected_sha in entry.files(DTLN_MODELS_DIR):
        dest = str(path)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            if sha256_file(dest) == expected_sha:
                print(f"  dtln model present: {path.name}")
                continue
            print(f"  dtln model hash mismatch, re-downloading: {path.name}")
            os.unlink(dest)
        print(f"  downloading dtln model: {path.name}")
        print(f"    from: {url}")
        try:
            download_model_file(
                url,
                dest,
                expected_sha256=expected_sha,
                label=f"DTLN model {path.name}",
                timeout_seconds=30.0,
                retries=2,
                max_bytes=64 * 1024 * 1024,
            )
        except ModelDownloadError as e:
            print(f"  failed: {e}", file=sys.stderr)
            failures += 1

sys.exit(1 if failures else 0)
PY
    then
        echo "  warning: one or more DTLN model downloads failed"
        echo "  the bridge will fall back to dual-stream (AEC ON + AEC OFF only)"
        echo
        echo "  Anonymous download from the release returns 404 while the"
        echo "  jaspercurry/JTS repo is still private. Manual install:"
        echo "    gh release download dtln-models-v1 --repo jaspercurry/JTS \\"
        echo "        --dir /var/lib/jasper/dtln"
        echo "    sudo systemctl restart jasper-aec-bridge"
        echo "  (one-time; once the repo goes public install.sh handles it)"
    fi

    # Seed /var/lib/jasper/wake_model.env with the recommended default
    # on FIRST install only. Existing files are left alone — the /wake/
    # picker owns this file once the user has touched it, and we never
    # want to trample an explicit choice. The recommended default lives
    # in jasper/wake_models.py (DEFAULT_KEY).
    "${INSTALL_DIR}/.venv/bin/python" - <<'PY' || true
import os
import sys

from jasper.wake_models import WAKE_MODEL_FILE, default

if os.path.exists(WAKE_MODEL_FILE):
    sys.exit(0)
entry = default()
if not os.path.exists(entry.model):
    print(f"  skipping wake_model.env seed: default file missing ({entry.model})")
    sys.exit(0)
os.makedirs(os.path.dirname(WAKE_MODEL_FILE), exist_ok=True)
tmp = WAKE_MODEL_FILE + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "w") as f:
    f.write(f"JASPER_WAKE_MODEL={entry.model}\n")
os.replace(tmp, WAKE_MODEL_FILE)
print(f"  seeded {WAKE_MODEL_FILE} -> {entry.key} ({entry.model})")
PY

    if [[ ! -f "${ENV_DIR}/jasper.env" ]]; then
        # Detect ReSpeaker XVF3800 card name. Default "Array" (PiOS literal
        # name; product description matches and it's also a substring of
        # PortAudio's enumerated name "Array: USB Audio (hw:N,0)").
        # JASPER_MIC_DEVICE format is a PortAudio device name/substring,
        # NOT an ALSA pcm string — see jasper/config.py for the rationale.
        local mic_card
        mic_card=$(detect_card arecord 'xvf3800|respeaker.*array' 'Array')
        echo "  ReSpeaker mic: ${mic_card}"
        # Derive JASPER_HOSTNAME from the OS hostname so a fresh Pi
        # named "jts2" in Raspberry Pi Imager ends up with
        # JASPER_HOSTNAME=jts2.local — otherwise other devices on the
        # LAN type jts2.local but Spotify/AirPlay setup URLs advertise
        # the wrong name. Override path stays clean: deploy-to-pi.sh
        # exports JASPER_HOSTNAME explicitly, which wins over the
        # autodetected fallback. Direct Pi-local install.sh reruns
        # that need a non-default identity must pass it in the sudo
        # environment, e.g.:
        #   sudo JASPER_HOSTNAME=jts2.local bash deploy/install.sh
        local hostname_value="${JASPER_HOSTNAME:-$(hostname).local}"
        echo "  hostname: ${hostname_value}"
        sed \
            -e "s|JASPER_MIC_DEVICE=Array|JASPER_MIC_DEVICE=${mic_card}|" \
            -e "s|^JASPER_HOSTNAME=.*|JASPER_HOSTNAME=${hostname_value}|" \
            "${REPO_DIR}/.env.example" > "${ENV_DIR}/jasper.env"
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo
        echo "Created ${ENV_DIR}/jasper.env from template."
        echo "Pick a voice provider at http://${hostname_value}/voice before"
        echo "starting jasper-voice — there is no default."
        echo
    fi
    sed -i \
        -e '/^JASPER_SPOTIFY_DEVICE_NAME=/d' \
        -e '/^JASPER_AIRPLAY_DEVICE_NAME=/d' \
        -e '/^SPOTIFY_CLIENT_ID=/d' \
        -e '/^SPOTIFY_OAUTH_MODE=/d' \
        -e '/^SPOTIFY_REDIRECT_URI=/d' \
        -e '/^SPOTIPY_REDIRECT_URI=/d' \
        "${ENV_DIR}/jasper.env"
    if [[ -n "${OUTPUT_DAC_ID:-}" ]]; then
        sed -i.bak '/^JASPER_AUDIO_DAC_ID=/d' "${ENV_DIR}/jasper.env"
        rm -f "${ENV_DIR}/jasper.env.bak"
        printf 'JASPER_AUDIO_DAC_ID=%s\n' "${OUTPUT_DAC_ID}" >> "${ENV_DIR}/jasper.env"
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo "  audio DAC id: ${OUTPUT_DAC_ID}"
    fi
    if [[ ! -e "${STATE_DIR}/speaker_name.env" ]]; then
        install -d -m 0750 "${STATE_DIR}"
        printf 'JASPER_SPEAKER_NAME="JTS"\n' > "${STATE_DIR}/speaker_name.env"
        chmod 0644 "${STATE_DIR}/speaker_name.env"
        echo "  speaker name: JTS"
    fi
    migrate_voice_provider
    migrate_openai_noise_reduction_default
    migrate_tts_outputd_socket_default
    render_voice_provider_ids_manifest
    migrate_transit_config
    migrate_weather_config
    migrate_wifi_guardian
    migrate_wake_legs_config
    migrate_grouping
    migrate_speaker_room
    migrate_control_host_bind_seed
}

install_systemd_units() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-camilla.service" \
        "${SYSTEMD_DIR}/jasper-camilla.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-voice.service" \
        "${SYSTEMD_DIR}/jasper-voice.service"
    # The 4 wizard daemons are SOCKET-ACTIVATED (each .service is paired
    # with a .socket unit that holds the port and re-spawns the daemon
    # on demand). systemd binds the listener; the daemon adopts the fd
    # via LISTEN_FDS and exits after 10 min idle, saving ~60-90 MB Pss
    # while no one is using a setup page. See jasper/web/_systemd.py.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web.service" \
        "${SYSTEMD_DIR}/jasper-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web.socket" \
        "${SYSTEMD_DIR}/jasper-web.socket"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-dial-web.service" \
        "${SYSTEMD_DIR}/jasper-dial-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-dial-web.socket" \
        "${SYSTEMD_DIR}/jasper-dial-web.socket"
    # /correction/ wizard. Phase 0 = mic-permission verify only;
    # future phases pull in heavy deps (numpy / scipy / pyfar) so
    # this lives in its own process rather than colocating with
    # jasper-web (Spotify + voice settings). Mirrors jasper-dial-web.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-correction-web.service" \
        "${SYSTEMD_DIR}/jasper-correction-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-correction-web.socket" \
        "${SYSTEMD_DIR}/jasper-correction-web.socket"
    # /bluetooth/ control panel — generic BT scan/pair/forget for
    # phones, knobs, headphones. Drives bluez via dbus-next; per-class
    # post-pair behaviour lives in jasper/bluetooth/handlers/.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-bluetooth-web.service" \
        "${SYSTEMD_DIR}/jasper-bluetooth-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-bluetooth-web.socket" \
        "${SYSTEMD_DIR}/jasper-bluetooth-web.socket"
    # /system/ dashboard — RAM/CPU/temp sparklines + restart/diagnostics
    # actions. Socket-activated like the other wizards.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-system-web.service" \
        "${SYSTEMD_DIR}/jasper-system-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-system-web.socket" \
        "${SYSTEMD_DIR}/jasper-system-web.socket"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-control.service" \
        "${SYSTEMD_DIR}/jasper-control.service"
    # jasper-input: third-party HID accessory bridge (Anticater VK-01
    # volume knob today; future macro pads / foot pedals). Reads
    # /dev/input/event* via python-evdev, translates known devices'
    # key events into HTTP calls against jasper-control. Always-on
    # like jasper-mux — idle cost is negligible if no accessory is
    # attached. See jasper/accessories/.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-input.service" \
        "${SYSTEMD_DIR}/jasper-input.service"
    # AEC bridge + boot-time chip init + reconciler. The reconciler is
    # the policy layer that keeps JASPER_MIC_DEVICE, AEC services, and
    # the currently attached mic hardware in sync.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-bridge.service" \
        "${SYSTEMD_DIR}/jasper-aec-bridge.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-init.service" \
        "${SYSTEMD_DIR}/jasper-aec-init.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-aec-reconcile.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-aec-reconcile" \
        /usr/local/sbin/jasper-aec-reconcile
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-audio-hardware-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-audio-hardware-reconcile.service"
    install -d -m 0755 /usr/local/lib/jasper
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-asound-render.sh" \
        /usr/local/lib/jasper/jasper-asound-render.sh
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-env-file.sh" \
        /usr/local/lib/jasper/jasper-env-file.sh
    # Installer-only sourced libs (install.sh sources them REPO_DIR-
    # relative from the rsync checkout; the installed copies mirror the
    # other deploy/lib files for on-Pi inspection/consistency).
    install -d -m 0755 /usr/local/lib/jasper/install
    install -m 0644 \
        "${REPO_DIR}"/deploy/lib/install/*.sh \
        /usr/local/lib/jasper/install/
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-audio-hardware-reconcile" \
        /usr/local/sbin/jasper-audio-hardware-reconcile

    # jasper-fanin: per-renderer snd-aloop substream fan-in daemon.
    # **Production default** as of 2026-05-26 — replaces the
    # dmix-based topology that PR #214 introduced and that turned out
    # to cause periodic AirPlay drops via WiFi-burst + dmix-write-
    # timing interaction. This unit is mandatory for renderer audio;
    # enable/start happens below after daemon-reload. See
    # docs/HANDOFF-fan-in-daemon.md for the design + 2026-05-26
    # validation; docs/HANDOFF-airplay.md Pattern A3 for the dmix
    # failure mode that motivated the cutover.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-fanin.service" \
        "${SYSTEMD_DIR}/jasper-fanin.service"
    # jasper-outputd: mainline final-output owner.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-outputd.service" \
        "${SYSTEMD_DIR}/jasper-outputd.service"

    # WiFi profile guardian. Type=oneshot boot-time recreate of a lost
    # /etc/NetworkManager/system-connections/<SSID>.nmconnection from
    # the wizard-owned stash at /var/lib/jasper/wifi_guardian.env. See
    # docs/HANDOFF-resilience.md "WiFi profile recovery" for the
    # design and the 2026-05-23 incident this defends against.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-guardian.service" \
        "${SYSTEMD_DIR}/jasper-wifi-guardian.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-wifi-guardian" \
        /usr/local/sbin/jasper-wifi-guardian

    # Identity reconciler. Type=oneshot snapshot of the speaker's
    # effective mDNS identity (OS hostname vs Avahi's post-collision
    # name vs JASPER_HOSTNAME) into /var/lib/jasper/identity.env, on a
    # 5-min timer because a collision rename lands when the OTHER
    # device joins the LAN. jasper.http_security reads the file so a
    # renamed speaker's management UI stays reachable instead of
    # 403ing. See docs/HANDOFF-identity.md.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-identity-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-identity-reconcile.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-identity-reconcile.timer" \
        "${SYSTEMD_DIR}/jasper-identity-reconcile.timer"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-identity-reconcile" \
        /usr/local/sbin/jasper-identity-reconcile

    # Boot-loop guard. Type=oneshot cross-boot circuit breaker for the
    # T5.1 StartLimitAction=reboot ladder: on the Nth boot inside the
    # window it writes runtime drop-ins (StartLimitAction=none) so a
    # PERMANENT daemon failure parks the sick unit failed (visible to
    # systemctl/doctor; systemctl reset-failed + start to recover) but
    # leaves the Pi reachable instead of rebooting forever. Runtime
    # drop-ins live in /run and self-clear on the next healthy boot.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-bootloop-guard.service" \
        "${SYSTEMD_DIR}/jasper-bootloop-guard.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-bootloop-guard" \
        /usr/local/sbin/jasper-bootloop-guard

    # jasper-usbsink: fourth music source (USB gadget audio in). The
    # init unit owns the ConfigFS gadget descriptor lifecycle; the
    # main service is the Python daemon that bridges gadget capture
    # into usbsink_substream. Both ship DISABLED — the /sources/ wizard
    # toggle owns enable/disable, and the dtoverlay must be set + Pi
    # rebooted first (handled by set_usb_gadget_mode above).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink-init.service" \
        "${SYSTEMD_DIR}/jasper-usbsink-init.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink.service" \
        "${SYSTEMD_DIR}/jasper-usbsink.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-gadget-up" \
        /usr/local/sbin/jasper-usbsink-gadget-up
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-gadget-down" \
        /usr/local/sbin/jasper-usbsink-gadget-down
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-wait-card" \
        /usr/local/sbin/jasper-usbsink-wait-card
    # Makes the host-visible USB device name track the Speaker Name by
    # patching the kernel's hardcoded UAC2 AudioStreaming string into a
    # `updates/` module override (configfs can't set it on 6.12). Pure
    # bash + stdlib python3 — no kernel headers / dkms. Run as
    # jasper-usbsink-init's best-effort ExecStartPre. See
    # docs/HANDOFF-usbsink.md "Device name".
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-name-patch" \
        /usr/local/sbin/jasper-usbsink-name-patch
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/uac2_name_patch.py" \
        /usr/local/sbin/uac2_name_patch.py

    # jasper multi-room grouping (snapcast). snapserver is the timing
    # master; snapclient plays a single channel on each speaker. The
    # reconcile oneshot maps the wizard-owned /var/lib/jasper/grouping.env
    # role to which units run (leader => snapserver + snapclient;
    # follower => snapclient only; off/invalid => neither). All three ship
    # DISABLED — a solo speaker runs none of them, and the reconciler is
    # the only thing that enables/starts them on explicit opt-in. We do
    # NOT auto-enable grouping here. See docs/HANDOFF-multiroom.md and
    # jasper.multiroom.reconcile.
    #
    # Packages: we deliberately do NOT apt-install snapserver/snapclient
    # in the core install. The vast majority of speakers are solo, the
    # snapcast packages pull in extra runtime deps (libsoxr, libvorbis,
    # libflac, avahi client, etc.) and an enabled-by-default snapserver
    # daemon socket — pure dead weight + attack surface on a box that
    # will never group. Mirrors the off-by-default posture of the
    # usbsink dtoverlay (staged but inert until the wizard opts in) and
    # the optional ESP32 firmware (sources staged, build gated behind
    # JASPER_BUILD_OPTIONAL_FIRMWARE=1). The units reference
    # /usr/bin/snapserver and /usr/bin/snapclient (Trixie's `snapserver`
    # / `snapclient` apt packages); installing those is the grouping
    # opt-in's job, not every solo install's. The reconciler's plan is
    # fail-safe — if the binaries are absent the unit simply fails to
    # start and grouping stays off, never wedging a solo speaker.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapserver.service" \
        "${SYSTEMD_DIR}/jasper-snapserver.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapclient.service" \
        "${SYSTEMD_DIR}/jasper-snapclient.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-grouping-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-grouping-reconcile.service"

    # Triggered by the udev rule installed below when the Apple dongle
    # re-enumerates: reset-failed, restart Camilla, then run the
    # mic/AEC reconciler so a hardware reconnect recovers without
    # manual intervention. See docs/HANDOFF-resilience.md.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-dongle-recover.service" \
        "${SYSTEMD_DIR}/jasper-dongle-recover.service"
    # Pin the Apple dongle's analog Headphone control to 100% at every
    # boot — the dynamic volume control happens in CamillaDSP (or the
    # source's own slider) and the dongle should never be limiting us.
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-dac-init" \
        /usr/local/bin/jasper-dac-init
    # DONGLE_CARD was set above by install_alsa. Apple-only mixer helpers
    # receive APPLE_DONGLE_SERVICE_CARD, which is either the detected Apple
    # card or "auto" so they can no-op/wait safely when absent.
    sed -e "s/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g" \
        "${REPO_DIR}/deploy/systemd/jasper-dac-init.service" \
        > "${SYSTEMD_DIR}/jasper-dac-init.service"
    chmod 0644 "${SYSTEMD_DIR}/jasper-dac-init.service"
    # Diagnostic monitor: 1Hz poll on the dongle's Headphone control,
    # logs every change to journald. Companion to jasper-dac-init —
    # if something moves the control after boot, this surfaces when
    # and how often. See deploy/bin/jasper-headphone-monitor.
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-headphone-monitor" \
        /usr/local/bin/jasper-headphone-monitor
    sed -e "s/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g" \
        "${REPO_DIR}/deploy/systemd/jasper-headphone-monitor.service" \
        > "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
    chmod 0644 "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
    # Custom udev rule: re-pins the dongle's Headphone control to 100%
    # on every USB (re-)enumeration AND disables autosuspend on the
    # device. Compensates for two upstream issues:
    #   * Trixie's alsa-utils 1.2.14-1 ships a broken
    #     /usr/lib/udev/rules.d/90-alsa-restore.rules where a GOTO
    #     points at the wrong label, so `alsactl restore` never fires
    #     on hotplug (Debian bug #1093057, still open).
    #   * The Apple dongle's UAC firmware default for the Headphone
    #     control is 80/120 (-20 dB), surfaced via UAC GET_CUR each
    #     time the device probes. Without our rule, every speaker
    #     re-plug or USB resume that triggers re-enumeration costs
    #     the user 20 dB of analog attenuation until they reboot or
    #     run `systemctl start jasper-dac-init` manually.
    # Active reset is also done by jasper-headphone-monitor (1 Hz
    # poller); this rule is the fast path on hotplug, the monitor
    # catches anything the rule doesn't.
    install -d -m 0755 /etc/udev/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-apple-dongle.rules" \
        /etc/udev/rules.d/99-jasper-apple-dongle.rules
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-aec-reconcile.rules" \
        /etc/udev/rules.d/99-jasper-aec-reconcile.rules
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-audio-hardware-reconcile.rules" \
        /etc/udev/rules.d/99-jasper-audio-hardware-reconcile.rules
    udevadm control --reload-rules
    # Trigger the rule once for the currently-attached dongle so we
    # don't have to wait for the next replug. ATTR{} match is
    # idempotent: amixer setting Headphone=100% on an already-pinned
    # control is a no-op.
    udevadm trigger --action=add --subsystem-match=sound 2>/dev/null || true
    udevadm trigger --action=add --subsystem-match=usb 2>/dev/null || true

    # We own the full systemd units for each renderer + nqptp + the
    # no-code Bluetooth pairing agent.
    #
    # Defense in depth: a Pi installed against an older codepath could
    # still have /etc/systemd/system/shairport-sync.service.d/jts-output.conf
    # on disk, which would override our ExecStart with
    # /usr/bin/shairport-sync (the apt-package path) — that binary doesn't
    # exist on this stack and the service crash-loops. Actively remove
    # the drop-in on every install so it can't reappear after rsync.
    if [[ -e "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf" ]]; then
        rm -f "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf"
        rmdir "${SYSTEMD_DIR}/shairport-sync.service.d" 2>/dev/null || true
        echo "  removed stale shairport drop-in from a previous install"
    fi
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/librespot.service" \
        "${SYSTEMD_DIR}/librespot.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/shairport-sync.service" \
        "${SYSTEMD_DIR}/shairport-sync.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/nqptp.service" \
        "${SYSTEMD_DIR}/nqptp.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bt-agent.service" \
        "${SYSTEMD_DIR}/bt-agent.service"
    # jasper-mux: latest-source-wins preemption between Spotify,
    # AirPlay, and Bluetooth.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-mux.service" \
        "${SYSTEMD_DIR}/jasper-mux.service"
    # Drop-in routing bluealsa-aplay's output into the JTS loopback
    # instead of ALSA default (HDMI on a fresh Pi).
    install -d -m 0755 "${SYSTEMD_DIR}/bluealsa-aplay.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-output.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-output.conf"
    # Drop-in flipping bluealsa.service (the apt-installed system unit)
    # to Restart=always with a StartLimit guard. Same logic as the
    # source-built renderers' service files: a clean exit (status=0)
    # silently disables Bluetooth audio under the apt default.
    install -d -m 0755 "${SYSTEMD_DIR}/bluealsa.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa.service.d/jts-restart.conf" \
        "${SYSTEMD_DIR}/bluealsa.service.d/jts-restart.conf"

    # sshd OOM-protection drop-in: Debian's openssh-server package
    # ships ssh.service WITHOUT an OOMScoreAdjust= directive. JTS
    # gives sshd a moderate negative bias so it remains a good recovery
    # path, but keeps it killable because SSH-launched diagnostics
    # inherit this value. Heavy Pi-side diagnostics should run through
    # scripts/pi-run-diagnostic.sh. Operators on distros whose sshd
    # unit is named differently (sshd.service on RHEL/Fedora) should
    # rename. See the file's header comment.
    install -d -m 0755 "${SYSTEMD_DIR}/ssh.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/ssh.service.d/oom-protection.conf" \
        "${SYSTEMD_DIR}/ssh.service.d/oom-protection.conf"

    # Stage 2 audio-protection slices: MemorySwapMax=0 on jts-audio.slice
    # (camilla + shairport-sync + librespot + bluealsa-aplay) and
    # jts-mic.slice (aec-bridge). Pages in these slices can NEVER be
    # swapped to zram — direct fix for the 2026-05-24 stress test that
    # caused audible audio glitches because aec-bridge accumulated 42 MB
    # of VmSwap. Requires cgroup memory controller enabled in
    # /boot/firmware/cmdline.txt (handled by migrate_cgroup_memory_enabled).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jts-audio.slice" \
        "${SYSTEMD_DIR}/jts-audio.slice"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jts-mic.slice" \
        "${SYSTEMD_DIR}/jts-mic.slice"
    # bluealsa-aplay's Slice= assignment lands as a drop-in (we don't
    # own that unit file fully — the package ships it). The 4 services
    # we DO own (jasper-camilla, jasper-aec-bridge, shairport-sync,
    # librespot) have Slice= directly in the .service file installed
    # above; no separate drop-in needed for them.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-slice.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-slice.conf"

    systemctl daemon-reload

    # Migrate the 4 wizard services from always-on to socket-activated.
    # Older installs had jasper-X-web.service enabled directly; the new
    # topology enables the .socket instead, which pulls in the .service
    # on demand. Idempotent: re-running install.sh after migration is
    # already done is a no-op.
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web jasper-dial-web jasper-system-web; do
        if systemctl is-enabled "${unit}.service" --quiet 2>/dev/null; then
            # First time through this socket-activation migration —
            # disable the always-on
            # service. Stop it explicitly so the next request comes up
            # with the new socket-activated code rather than the
            # still-running old-process binding the port.
            systemctl stop "${unit}.service" 2>/dev/null || true
            systemctl disable "${unit}.service" 2>/dev/null || true
            echo "  migrated ${unit} to socket activation"
        fi
        systemctl enable "${unit}.socket"
        # Restart (not just start) so a deploy that adds or moves a
        # ListenStream= port — e.g. a new wizard sharing this socket
        # like /sources/ on 8773 — actually re-binds. A bare `start`
        # is a no-op when the socket is already active and silently
        # leaves the old port set live; nginx then 502s on the new
        # route until the next reboot. Restart cascades through the
        # Requires=.socket service too, which the later wizard-stop
        # loop also covers, so the order is safe.
        systemctl restart "${unit}.socket" 2>/dev/null || true
    done

    systemctl enable jasper-camilla.service jasper-fanin.service \
        jasper-outputd.service \
        jasper-audio-hardware-reconcile.service \
        jasper-voice.service \
        jasper-control.service \
        jasper-input.service

    # Stop the currently-running voice daemon before outputd claims the
    # direct DAC. On outputd deploys, the old voice process may still
    # hold a PortAudio stream to the legacy jasper_out path; if outputd
    # starts first, DAC ownership can fail with "device busy". The AEC
    # reconciler below restarts or parks voice once the output path is
    # coherent.
    systemctl stop jasper-voice.service 2>/dev/null || true
    systemctl reset-failed jasper-voice.service 2>/dev/null || true
    /usr/local/sbin/jasper-audio-hardware-reconcile --reason install || \
        echo "  WARN: audio hardware reconcile failed. Check logs with: journalctl -u jasper-audio-hardware-reconcile -e"

    systemctl restart jasper-fanin.service 2>/dev/null || true
    # CamillaDSP captures the fan-in output (`pcm.jasper_capture`).
    # Restart it after fan-in/asound wiring changes so it cannot keep
    # an old capture fd across topology updates.
    systemctl try-restart jasper-camilla.service 2>/dev/null || true
    # outputd owns the final DAC loop on current main. If it is not active
    # and answering STATUS, the voice daemon's outputd TTS socket points at a
    # silent path. Surface that LOUDLY, but do NOT abort the install: nginx,
    # TLS, cues, and the doctor summary are the operator's recovery surface
    # and must always be set up. A transient 3 s STATUS-probe miss or a slow
    # service settle on a loaded 1 GB Pi must not strand the box with no web
    # UI to diagnose it through. The systemd Wants=/After=jasper-outputd
    # dependency is the real runtime guard, and run_doctor_summary re-checks
    # outputd (check_outputd_service) at the end of the install. Mirrors the
    # non-fatal jasper-audio-hardware-reconcile handling a few lines above.
    require_outputd_ready || \
        echo "  WARN: jasper-outputd is not ready (see the STATUS-probe error above). Voice TTS may be silent until outputd recovers; check http://${JASPER_HOSTNAME:-jts.local}/system/ and 'journalctl -u jasper-outputd'. Continuing install so the web UI and doctor remain available."

    systemctl enable nqptp.service shairport-sync.service \
        librespot.service bt-agent.service jasper-mux.service
    systemctl restart bluealsa-aplay.service 2>/dev/null || true
    systemctl restart nqptp.service shairport-sync.service \
        librespot.service bt-agent.service jasper-mux.service \
        2>/dev/null || true
    # The 5 wizard services are socket-activated now. Any currently-
    # running instance is on the old code; stop it so the next incoming
    # request brings up the new code via the .socket. Idempotent: if the
    # service is already inactive (post-idle-exit or never started), the
    # stop is a no-op.
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web jasper-dial-web jasper-system-web; do
        systemctl stop "${unit}.service" 2>/dev/null || true
    done
    # jasper-input is always-on (HID accessory bridge) — restart so any
    # already-plugged-in knob picks up new code without waiting for boot.
    systemctl restart jasper-input.service 2>/dev/null || true

    # Reconcile software AEC against whatever mic hardware is actually
    # present right now. This replaces the old one-way "enable if
    # Array is 6-ch" install step: if a previous install left voice on
    # udp:9876 but the Array is currently absent, reconcile actively
    # clears that stale state and parks voice instead of letting it
    # watchdog-loop on an unfed UDP socket.
    reconcile_aec_state
    # WiFi profile guardian: oneshot at boot, gated by
    # ConditionPathExists= on the wizard's stash file. Enabling is safe
    # on fresh installs because the unit silently no-ops until the
    # wizard saves once. See migrate_wifi_guardian (called from
    # ensure_env_file above) for the SSH-driven-setup seed path.
    systemctl enable jasper-wifi-guardian.service
    # Boot-loop guard: oneshot at boot; records the boot timestamp and
    # disarms StartLimitAction=reboot via runtime drop-ins only when
    # boots are looping. Safe on fresh installs (first boots never trip).
    systemctl enable jasper-bootloop-guard.service
    # Identity reconciler: boot + 5-min timer; pure observer (writes
    # only /var/lib/jasper/identity.env). `enable --now`, NOT bare
    # `enable`: enable alone arms the timer for the NEXT boot but
    # leaves it inactive until then — the same enable-vs-start trap as
    # the wizard-socket lesson above. Caught on hardware 2026-06-11
    # (timer inactive after first deploy; doctor's snapshot-staleness
    # warn was the backstop). --now is idempotent on redeploys. The
    # one-shot service `start` keeps identity fresh immediately so the
    # allowlist/doctor don't wait for the first timer tick.
    systemctl enable --now jasper-identity-reconcile.timer
    systemctl start jasper-identity-reconcile.service || \
        echo "  (identity reconcile failed — non-fatal; doctor will flag)"
    echo
    echo "Units enabled. Start with: systemctl start jasper-fanin jasper-camilla jasper-outputd jasper-voice"
}

install_journald_persistent_storage() {
    # Raspberry Pi OS ships /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf
    # which forces Storage=volatile. With the kernel watchdog reaping wedged
    # userspace ~60s later, a volatile journal means the reset wipes all
    # evidence of what hung the box. Override with a 50- drop-in that flips
    # back to persistent, capped to bound SD-card writes.
    install -d -m 0755 /etc/systemd/journald.conf.d
    install -m 0644 \
        "${REPO_DIR}/deploy/journald/50-jts-persistent-storage.conf" \
        /etc/systemd/journald.conf.d/50-jts-persistent-storage.conf
    systemctl restart systemd-journald
    # systemd-journal-flush.service only runs at boot; do the runtime →
    # persistent transfer here so the live system starts writing to
    # /var/log/journal/ without needing a reboot to apply.
    journalctl --rotate >/dev/null 2>&1 || true
    journalctl --flush >/dev/null 2>&1 || true
}

reconcile_aec_state() {
    install -d -m 0755 "${STATE_DIR}"
    # Five keys live in aec_mode.env, all owned by the /wake/
    # input-profile / wake-detection cards:
    #   - JASPER_AUDIO_INPUT_PROFILE  canonical profile selection
    #                                 (auto, xvf_chip_aec,
    #                                 xvf_software_aec3, direct_mic,
    #                                 custom)
    #   - JASPER_AEC_MODE             master AEC bridge toggle
    #   - JASPER_WAKE_LEG_RAW         additive raw chip-direct leg (~5 MB)
    #   - JASPER_WAKE_LEG_DTLN        additive DTLN neural leg (~75 MB)
    #   - JASPER_WAKE_LEG_CHIP_AEC    XVF3800 chip-AEC beam legs (opt-in,
    #                                 hardware-conditional, mutually
    #                                 exclusive with raw/DTLN)
    # Defaults: profile auto. On the recommended XVF3800 6-channel
    # hardware that resolves to chip-AEC (no stacked software AEC/raw/DTLN).
    # When chip-AEC is unavailable it falls back to the software-AEC3
    # profile (AEC on, raw fallback on, DTLN off). DTLN remains an
    # explicit custom/lab leg because it is heavy on a 1 GB Pi.
    #
    # On upgrade, the reconciler's ensure_mode_file appends any
    # missing keys with these same defaults — preserving an
    # operator's hand-set JASPER_AEC_MODE/leg fields while inferring a
    # profile for pre-profile installs. Migration from hand-set underlying
    # env vars in /etc/jasper/jasper.env runs separately in
    # migrate_wake_legs_config.
    if [[ ! -f "${STATE_DIR}/aec_mode.env" ]]; then
        printf 'JASPER_AUDIO_INPUT_PROFILE=auto\nJASPER_AEC_MODE=auto\nJASPER_WAKE_LEG_RAW=1\nJASPER_WAKE_LEG_DTLN=0\nJASPER_WAKE_LEG_CHIP_AEC=0\n' \
            > "${STATE_DIR}/aec_mode.env"
        chmod 0644 "${STATE_DIR}/aec_mode.env"
    fi
    systemctl enable jasper-aec-reconcile.service
    /usr/local/sbin/jasper-aec-reconcile --reason install || \
        echo "  WARN: AEC/mic reconcile failed. Check logs with: journalctl -u jasper-aec-reconcile -e"
}

remove_legacy_https_artifacts() {
    # The old install topology served /spotify/ over HTTPS using a
    # self-signed cert at /etc/nginx/ssl/jasper.{crt,key} so Spotify's
    # OAuth-redirect-URI rules accepted it. The cert tripped scary
    # "connection not private" warnings in every browser, which we now
    # side-step by terminating Spotify's HTTPS requirement at a static
    # GitHub Pages bounce page (separate public repo
    # jaspercurry/spotify-oauth-callback). Sweep the old cert + key +
    # previous-generation nginx site files here so upgrading installs
    # end up with the new plain-HTTP topology for the legacy routes.
    #
    # NOTE: the new room-correction TLS lives at
    # /etc/nginx/ssl/jts.local.{crt,key} (different filenames),
    # provisioned by provision_correction_tls() below. Don't sweep
    # those.
    rm -f /etc/nginx/ssl/jasper.crt /etc/nginx/ssl/jasper.key
    rm -f /etc/nginx/sites-enabled/jasper-https.conf
    rm -f /etc/nginx/sites-available/jasper-https.conf
    rm -f /etc/nginx/jasper-locations.conf
}

provision_correction_tls() {
    # /correction/ requires HTTPS because getUserMedia (mic capture)
    # only works in a secure context. There's no way around this in
    # any browser, so we provision a private CA the user trusts once
    # on iOS, then issue a server cert from it for jts.local.
    #
    # CA is generated once and preserved across reinstalls so the
    # iOS trust survives upgrades. Server cert is re-issued every
    # install (cheap, and lets a hostname change propagate).
    #
    # 825-day server cert expiry is Apple's hard ceiling — Safari
    # rejects leaf certs valid longer than that since iOS 13. CA
    # cert can be longer (10 years).
    #
    # See deploy/nginx-jasper.conf "Why HTTPS is added back" and
    # docs/HANDOFF-correction.md "Decision 1 — TLS" for context.
    local hostname="${JASPER_HOSTNAME:-jts.local}"
    local ca_dir=/var/lib/jasper/ca
    local ssl_dir=/etc/nginx/ssl
    install -d -m 0700 "${ca_dir}"
    install -d -m 0755 "${ssl_dir}"

    if [[ ! -f "${ca_dir}/ca.crt" || ! -f "${ca_dir}/ca.key" ]]; then
        echo "  generating /correction/ private CA at ${ca_dir}/ca.crt"
        openssl genrsa -out "${ca_dir}/ca.key" 4096 2>/dev/null
        openssl req -x509 -new -nodes -key "${ca_dir}/ca.key" \
            -sha256 -days 3650 -out "${ca_dir}/ca.crt" \
            -subj "/CN=JTS Speaker Local CA" 2>/dev/null
        chmod 0600 "${ca_dir}/ca.key"
    fi

    local tmp_csr tmp_ext
    tmp_csr=$(mktemp)
    tmp_ext=$(mktemp)
    openssl genrsa -out "${ssl_dir}/jts.local.key" 2048 2>/dev/null
    openssl req -new -key "${ssl_dir}/jts.local.key" \
        -out "${tmp_csr}" -subj "/CN=${hostname}" 2>/dev/null
    # Always include "jts.local" + 127.0.0.1 in SANs so the cert
    # works whether the user typed the configured hostname or the
    # default mDNS name. Wildcard covers any future sub-host
    # (e.g. correction.jts.local if we split routes later).
    cat > "${tmp_ext}" <<EOF
subjectAltName = DNS:${hostname}, DNS:*.${hostname}, DNS:jts.local, IP:127.0.0.1
extendedKeyUsage = serverAuth
EOF
    openssl x509 -req -in "${tmp_csr}" -CA "${ca_dir}/ca.crt" \
        -CAkey "${ca_dir}/ca.key" -CAcreateserial \
        -out "${ssl_dir}/jts.local.crt" -days 825 -sha256 \
        -extfile "${tmp_ext}" 2>/dev/null
    chmod 0600 "${ssl_dir}/jts.local.key"
    rm -f "${tmp_csr}" "${tmp_ext}"

    # Publish CA for download by iOS (chicken-and-egg: user can't
    # trust HTTPS until they've installed this file, so it's served
    # over plain HTTP at http://<host>/jts-root-ca.crt — see the
    # location block in nginx-jasper.conf).
    install -d -m 0755 /usr/share/jasper-web
    install -m 0644 "${ca_dir}/ca.crt" /usr/share/jasper-web/jts-root-ca.crt
    echo "  /correction/ TLS provisioned (server cert for ${hostname}, CA at /usr/share/jasper-web/jts-root-ca.crt)"
}

install_nginx_site() {
    # Standalone nginx site that reverse-proxies /spotify/ (multi-account
    # OAuth web flow), /voice/ (voice-provider config wizard), and /dial/
    # (rotary-dial onboarding) on plain HTTP. /correction/ starts with
    # a plain-HTTP preflight page, then the measurement UI switches to
    # HTTPS. The legacy routes stay HTTP — Spotify's HTTPS requirement
    # is satisfied by the GitHub Pages bounce, and there's no point
    # breaking working flows for one feature.
    #
    # /correction/ requires HTTPS because getUserMedia needs a secure
    # context. /google/ stays HTTP here; Google rejects mDNS redirect
    # URIs, so it uses the same GitHub Pages bounce pattern as Spotify.
    # The correction-only cert is provisioned by provision_correction_tls()
    # before this function runs.
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-jasper.conf" \
        /etc/nginx/sites-enabled/jasper.conf

    # Static landing page served at /. Plain HTML, no daemon — nginx
    # reads it directly via the `location = /` block in jasper.conf.
    # Updates require an `nginx -s reload` (handled by the reload below)
    # but no service restart.
    install -d -m 0755 /usr/share/jasper-web
    install -m 0644 \
        "${REPO_DIR}/deploy/index.html" \
        /usr/share/jasper-web/index.html
    # Stamp the app.css cache-bust version (mirrors the wizards' build-SHA
    # query string) so a deploy busts the year-immutable /assets cache.
    # The landing page is static HTML, so we substitute at install time;
    # build.txt was written earlier in this run.
    app_css_ver="$(grep -E '^JASPER_GIT_SHA=' "${STATE_DIR}/build.txt" 2>/dev/null | head -1 | cut -d= -f2-)"
    [[ -n "${app_css_ver}" && "${app_css_ver}" != "unknown" ]] || app_css_ver="dev"
    sed -i "s/__APP_CSS_VERSION__/${app_css_ver}/g" /usr/share/jasper-web/index.html
    # All /assets/ content (app.css, fonts, per-page CSS + ES modules) +
    # the .install-manifest the doctor verifies — see
    # deploy/lib/install/web-assets.sh for the copy shape and the
    # manifest contract.
    install_web_assets
    # Plain-HTTP preflight before the HTTPS-only room-correction UI.
    # This gives the user context before the browser's self-signed-cert
    # interstitial while keeping the entry point on the normal HTTP
    # surface.
    install -m 0644 \
        "${REPO_DIR}/deploy/correction-preflight.html" \
        /usr/share/jasper-web/correction-preflight.html
    # Stamp the same app.css cache-bust version as the landing page — the
    # preflight is static HTML and links /assets/app.css directly, so it
    # needs the build-SHA query string to bust the immutable /assets cache.
    sed -i "s/__APP_CSS_VERSION__/${app_css_ver}/g" \
        /usr/share/jasper-web/correction-preflight.html
    # Prune the retired /integrations page from prior installs. Its nginx route
    # and install copy are gone (the landing page's inline Integrations section
    # replaced it); remove the orphaned file so a previously-deployed Pi does
    # not keep an unreachable page on disk. `-f` no-ops on fresh installs.
    rm -f /usr/share/jasper-web/integrations.html

    # Disable Debian's default site so it doesn't clash with our
    # default_server directives. nginx-light installs an enabled
    # `default` symlink; remove it idempotently.
    rm -f /etc/nginx/sites-enabled/default

    if nginx -t 2>/dev/null; then
        systemctl enable --now nginx 2>/dev/null || true
        systemctl reload nginx
        echo "  nginx reloaded — http://<host>/{,spotify,voice,dial} + https://<host>/{correction,google} are live"
    else
        echo "  WARNING: nginx config test failed; not reloading. Run 'nginx -t' to debug."
    fi
}

install_avahi_jasper_control() {
    # Advertise jasper-control over mDNS so the rotary dial can find
    # us via service discovery instead of a hardcoded hostname. See
    # deploy/avahi/jasper-control.service for the rationale and the
    # firmware-side counterpart in firmware/dial/src/discovery.cpp.
    #
    # The advertised file now also carries a name= TXT record with the
    # speaker's friendly display name (the /speaker identity), so the
    # /rooms directory shows friendly names. Because the name is a
    # per-runtime value, the file is RENDERED from a TEMPLATE rather
    # than copied statically: install the template OUT of
    # /etc/avahi/services/ (Avahi must not parse its __SPEAKER_NAME__
    # placeholder as XML — same reasoning as install_peering_template),
    # then let jasper.control_advert.render_control_advert substitute
    # the (XML-escaped) name, atomic-write the live file, and reload
    # Avahi. The /speaker save path re-renders on a name change.
    install -d -m 0755 /etc/jasper/avahi-templates
    install -m 0644 \
        "${REPO_DIR}/deploy/avahi/jasper-control.service.template" \
        /etc/jasper/avahi-templates/jasper-control.service

    install -d -m 0755 /etc/avahi/services
    # Render the live service from the template via the Python module
    # (it does the XML-escape, atomic write, and Avahi reload). The
    # package is already pip-installed by install_jasper above, so the
    # import resolves here. render_control_advert is fail-soft (returns
    # False, never raises); we still guard the whole call with `|| true`
    # plus a static-file fallback so a render failure can never leave
    # _jasper-control._tcp un-advertised — the dial (and jasper-doctor's
    # "avahi: _jasper-control._tcp" check) depend on it always existing.
    local rendered=0
    if [[ -x "${INSTALL_DIR}/.venv/bin/python" ]] \
       && "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import sys

from jasper.control_advert import render_control_advert

# name=None -> read the current /speaker name (env-first then
# /var/lib/jasper/speaker_name.env), empty -> hostname default, so the
# TXT is never empty. render_control_advert handles the reload itself.
sys.exit(0 if render_control_advert() else 1)
PY
    then
        rendered=1
        echo "  Advertised _jasper-control._tcp via avahi (port 8780, name= TXT)"
    fi

    if [[ "${rendered}" != "1" ]]; then
        # Fallback: the render didn't run (no venv yet) or failed. Drop
        # the static, name-less service file so the speaker still
        # advertises and the doctor check stays green. The friendly
        # name TXT is lost until the next successful render (e.g. the
        # next /speaker save or deploy), but discovery itself is intact.
        echo "  WARNING: control-advert render unavailable; installing static jasper-control.service (no name= TXT)"
        install -m 0644 \
            "${REPO_DIR}/deploy/avahi/jasper-control.service" \
            /etc/avahi/services/jasper-control.service
        # Reload — avahi-daemon picks up new service files via inotify
        # but a SIGHUP is more deterministic on first install. Best
        # effort: avahi-daemon may not be running yet on a fresh image.
        systemctl reload avahi-daemon 2>/dev/null \
            || systemctl restart avahi-daemon 2>/dev/null \
            || true
        echo "  Advertised _jasper-control._tcp via avahi (port 8780)"
    fi
}

install_peering_template() {
    # Multi-device peering. The TEMPLATE goes under /etc/jasper/ so
    # Avahi doesn't try to parse it as a service file (the
    # placeholders __PEER_ID__ / __ROOM__ / __PRIMARY__ aren't valid
    # XML attribute values).
    #
    # jasper-control's peering daemon renders this template into
    # /etc/avahi/services/jasper-peer.service when JASPER_PEERING=on
    # is set in /var/lib/jasper/peering.env (via the /peers/ web
    # wizard, in PR 2). When peering is off (the default), no
    # rendered file exists and this Pi is invisible to siblings —
    # the goal property of "zero cost when alone".
    #
    # Also generates the per-install stable peer_id (a UUID) if one
    # doesn't already exist. This ID persists across reboots and
    # package upgrades — peers don't see a "new" device on every
    # restart.
    install -d -m 0755 /etc/jasper/avahi-templates
    install -m 0644 \
        "${REPO_DIR}/deploy/avahi/jasper-peer.service.template" \
        /etc/jasper/avahi-templates/jasper-peer.service
    install -d -m 0755 /var/lib/jasper
    if [[ ! -f /var/lib/jasper/peer_id ]]; then
        # Guard the redirect: a `python3` failure (missing binary,
        # broken `uuid` import) without this would leave an empty
        # peer_id file. The daemon's load_config falls back to an
        # *ephemeral* per-process UUID in that case — peers would see
        # a new "device" on every restart, which silently breaks
        # session-stickiness across reboots.
        if ! pid="$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null)"; then
            echo "  ERROR: could not generate peer_id (python3 missing or uuid failed)" >&2
            exit 1
        fi
        printf '%s\n' "${pid}" > /var/lib/jasper/peer_id
        chmod 0644 /var/lib/jasper/peer_id
        echo "  Generated stable peer_id at /var/lib/jasper/peer_id"
    fi
    echo "  Peering template installed; peering is OFF by default — enable at http://${JASPER_HOSTNAME:-jts.local}/peers/"
}

regenerate_audio_cues() {
    # Bake the speaker's audible-failure cues so they're ready before
    # the daemon ever needs them. The daemon retries on every startup
    # if this fails, so a no-internet-at-install scenario is tolerated
    # — we just warn and continue. See docs/HANDOFF-audible-feedback.md
    # for what cues exist and why.
    if [[ ! -x /opt/jasper/.venv/bin/jasper-cues ]]; then
        echo "  (jasper-cues not on PATH yet — will run on first daemon boot)"
        return 0
    fi
    echo "  Regenerating audio cues..."
    # jasper-cues auto-loads /etc/jasper/jasper.env then
    # /var/lib/jasper/voice_provider.env (web-wizard overrides) via
    # jasper.env_load — same precedence as the daemon's systemd unit.
    # We deliberately do NOT pre-source jasper.env here: doing so puts
    # those vars into the shell's environment first, where load_env_files's
    # setdefault preserves them and the wizard file can't override.
    if ! /opt/jasper/.venv/bin/jasper-cues regenerate; then
        echo "  WARNING: cue regenerate failed (network down or API key not set?). " \
             "Daemon will retry at startup. To force a refresh later: " \
             "sudo systemctl restart jasper-voice"
    fi
}

install_camillagui() {
    # CamillaGUI — official web UI for CamillaDSP. Connects to the same
    # ws://127.0.0.1:1234 control socket the Python daemon already uses,
    # exposes a SPA for live config editing, signal levels, and config-
    # file management. We use the prebuilt PyInstaller bundle from the
    # upstream release rather than a venv/source install — bundle is
    # self-contained (Python 3.12 + frontend assets baked in), no apt
    # deps, no pip resolution. Listens on 0.0.0.0:5005 directly (parity
    # with /spotify, /voice, /dial — all unauthenticated, all home-LAN-
    # only). The landing page links straight to http://${HOSTNAME}:5005.
    local CAMILLAGUI_VERSION="4.1.0"
    local CAMILLAGUI_DIR="/opt/camillagui"
    local arch bundle bundle_sha256
    arch=$(uname -m)
    case "${arch}" in
        aarch64)
            bundle="bundle_linux_aarch64.tar.gz"
            bundle_sha256="9a5415b44dda58478f18de9fd572edf092f659fd5e45cbe8086ff5648dc089d7"
            ;;
        x86_64)
            bundle="bundle_linux_amd64.tar.gz"
            bundle_sha256="86fd3cde575038f312ede7bad0910dc5e46b974cafc048c26115ec3cb9f54792"
            ;;
        armv7l)
            bundle="bundle_linux_armv7.tar.gz"
            bundle_sha256="22b89033ebfe1e4d49afd80c0c745bb6bffec19bc2ac2a60279e565524d467d1"
            ;;
        *)
            echo "  WARNING: no CamillaGUI bundle for ${arch} — skipping"
            return 0
            ;;
    esac

    if [[ -x "${CAMILLAGUI_DIR}/camillagui_backend/camillagui_backend" ]]; then
        echo "  CamillaGUI already at ${CAMILLAGUI_DIR}"
    else
        echo "  Downloading CamillaGUI ${CAMILLAGUI_VERSION} (${arch})..."
        local tmpdir
        tmpdir=$(mktemp -d)
        local url="https://github.com/HEnquist/camillagui-backend/releases/download/v${CAMILLAGUI_VERSION}/${bundle}"
        if ! curl -fsSL --retry 3 --retry-connrefused --max-time 300 \
                -o "${tmpdir}/cg.tar.gz" "${url}"; then
            echo "  WARNING: CamillaGUI download failed — skipping"
            rm -rf "${tmpdir}"
            return 0
        fi
        if ! echo "${bundle_sha256}  ${tmpdir}/cg.tar.gz" | sha256sum -c -; then
            echo "  WARNING: CamillaGUI checksum mismatch — skipping" >&2
            rm -rf "${tmpdir}"
            return 0
        fi
        install -d -m 0755 "${CAMILLAGUI_DIR}"
        tar -xzf "${tmpdir}/cg.tar.gz" -C "${CAMILLAGUI_DIR}"
        rm -rf "${tmpdir}"
        echo "  Installed CamillaGUI to ${CAMILLAGUI_DIR}"
    fi

    # Config + state dirs. /etc/camilladsp/coeffs holds FIR-filter
    # coefficient files the GUI writes when convolving; we create it
    # so the GUI's first save doesn't fail with ENOENT.
    install -d -m 0755 /etc/camillagui /etc/camilladsp/coeffs /var/lib/camillagui
    install -m 0644 \
        "${REPO_DIR}/deploy/camillagui/config.yml" \
        /etc/camillagui/config.yml
    touch /var/log/camillagui.log
    chmod 0644 /var/log/camillagui.log

    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/camillagui.service" \
        "${SYSTEMD_DIR}/camillagui.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/camillagui-proxy.service" \
        "${SYSTEMD_DIR}/camillagui-proxy.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/camillagui.socket" \
        "${SYSTEMD_DIR}/camillagui.socket"

    # Migration: earlier installs ran camillagui.service directly,
    # always-on. We're switching to socket-activation via the
    # .socket + systemd-socket-proxyd. Disable the boot-time pull
    # of camillagui.service (it's dependency-activated now) so the
    # idle-exit lifecycle works as designed. Idempotent — re-runs
    # are a no-op once we're on the new layout.
    if systemctl is-enabled camillagui.service >/dev/null 2>&1; then
        systemctl disable camillagui.service
    fi
    # Stop the always-on instance so the next request goes through
    # the new socket-activation path. Safe whether it's running or
    # not — the socket activation will re-spawn on demand.
    systemctl stop camillagui.service 2>/dev/null || true

    systemctl daemon-reload
    systemctl enable --now camillagui.socket
    echo "  CamillaGUI listening on :5005 via socket-activated proxy"
    echo "  (backend exits 10 min after last access; ~50 MB Pss reclaimed)"
}

run_doctor_summary() {
    # Final pre-flight: run jasper-doctor so the operator sees status of
    # every subsystem (env file, mic, firmware, AEC bridge, renderers,
    # provider keys, …) at install time. Non-blocking — install is done
    # by the time we get here; this is just a status report.
    #
    # Critical for catching the "silent productization gaps" — e.g. an
    # XVF chip on 6-ch firmware but with the ALSA mixer's ch2-5 muted,
    # which used to be invisible until a wake-word test failed days
    # later. Doctor flags it inline now.
    if [[ ! -x /opt/jasper/.venv/bin/jasper-doctor ]]; then
        return 0
    fi
    echo
    echo "=== jasper-doctor pre-flight ==="
    if /opt/jasper/.venv/bin/jasper-doctor; then
        echo "✓ all critical doctor checks pass."
    else
        echo
        echo "─────────────────────────────────────────────────────────────"
        echo " jasper-doctor reports failures (see above)."
        echo " Install finished, but at least one subsystem isn't healthy."
        echo " Re-run after fixing: sudo /opt/jasper/.venv/bin/jasper-doctor"
        echo "─────────────────────────────────────────────────────────────"
    fi
}

main() {
    local dry_run="${JASPER_INSTALL_DRY_RUN:-0}"
    local arg
    for arg in "$@"; do
        case "${arg}" in
            --dry-run|--plan)
                dry_run=1
                ;;
            -h|--help)
                print_install_usage
                return 0
                ;;
            *)
                echo "unknown install.sh argument: ${arg}" >&2
                print_install_usage >&2
                return 2
                ;;
        esac
    done

    if _is_truthy "${dry_run}"; then
        print_install_plan
        return 0
    fi
    if ! _is_falsey_or_empty "${dry_run}"; then
        echo "invalid JASPER_INSTALL_DRY_RUN value: ${dry_run}" >&2
        echo "use 1/true/yes/on or 0/false/no/off" >&2
        return 2
    fi

    echo "==> install.sh starting"
    require_root
    require_build_user  # Rust builds run as 'pi'; fail fast pre-mutation
    install_deps
    install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
    install_camilladsp
    install_renderers
    set_usb_gadget_mode
    tune_wifi_for_airplay
    install_jasper
    build_install_jasper_fanin    # Rust daemon binary; enabled by install_systemd_units
    build_install_jasper_outputd  # Rust mainline final-output owner
    install_systemd_units
    retire_audio_topology_switch # Remove stale dmix/fanin state; fanin is canonical
    migrate_memory_resilience   # Stage 1 OOM protection: sysctl + MGLRU + zram
    migrate_cgroup_memory_enabled  # Stage 2 audio-slice: cgroup memory + PSI in cmdline.txt
    install_journald_persistent_storage
    install_avahi_jasper_control
    install_peering_template
    remove_legacy_https_artifacts
    provision_correction_tls   # cert files must exist before nginx -t
    install_nginx_site
    install_camillagui
    regenerate_audio_cues
    run_doctor_summary
}

# Only run main when invoked directly. When sourced (e.g. by tests
# that want to call a single helper like `_compute_min_free_kbytes`),
# define the functions but don't execute main.
if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
    main "$@"
fi
