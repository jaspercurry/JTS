#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Package, pip-constraint and pinned source-archive dependencies for deploy/install.sh.

# Pi-generated pip constraints (scripts/generate-pi-constraints.sh).
# Echoes the file path when the repo carries one, nothing otherwise —
# the install path turns that into `-c <file>` args for the unpinned
# pip installs, and a missing file is a graceful no-op (open-range
# resolution, the pre-constraints behavior). Kept as a tiny helper so
# tests can source install.sh and pin the contract.
jasper_pip_constraints_file() {
    local constraints="${REPO_DIR}/deploy/constraints-pi.pins"
    if [[ -f "${constraints}" ]]; then
        printf '%s\n' "${constraints}"
    fi
}

fetch_verified_source_archive() {
    # Fetch-to-temp-then-swap: download, hash-check, and extract into a
    # staging dir first, and replace ${dest_dir} only once all of that
    # succeeded, so a transient network failure under `set -e` never leaves
    # the prior source tree destroyed. Bounded retries absorb flaky Pi WiFi;
    # --max-time caps a stalled transfer (these archives are a few MB) so the
    # install can't hang forever.
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

_install_renderer_native_deps() {
    # Source-build deps for shairport-sync (AirPlay 2) + nqptp, plus
    # the bluez-alsa userspace and the JTS Bluetooth agent. All of these
    # are absent on a stock Trixie Lite image and are shared by full speakers
    # and streamboxes.
    #
    # `avahi-daemon` is the mDNS *publisher* — Pi OS Lite ships
    # `libnss-mdns` (resolution only) by default but does NOT install
    # the daemon, so without this line `<hostname>.local` from another
    # device fails to find us, `_jasper-control._tcp` isn't advertised
    # for speaker discovery, and `avahi-utils` tools have no daemon to talk to.
    # `avahi-utils` provides avahi-browse / avahi-publish for diagnostics.
    apt-get install -y --no-install-recommends \
        autoconf automake libtool pkg-config \
        libpopt-dev libconfig-dev libavahi-client-dev \
        libssl-dev libsoxr-dev libplist-dev libsodium-dev \
        libgcrypt20-dev uuid-dev libmbedtls-dev libglib2.0-dev \
        libavutil-dev libavcodec-dev libavformat-dev libswresample-dev \
        xxd libplist-utils \
        bluez-alsa-utils rfkill avahi-daemon avahi-utils
}

install_deps() {
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        build-essential libasound2-dev libasound2 portaudio19-dev \
        libasound2-plugins \
        libsndfile1 curl ca-certificates rsync \
        dfu-util \
        libwebrtc-audio-processing-dev \
        meson ninja-build \
        nginx-light openssl \
        dnsmasq-base \
        rustc cargo
    # dnsmasq-base is the DHCP server BINARY only — NOT the full `dnsmasq`
    # package, which would enable a global dnsmasq.service. The scoped,
    # device-activated jasper-usbnet-dhcp.service runs it against usb0 for the
    # hardware-gated USB management network.
    # rustc + cargo are required to build the Rust audio daemons (rust/jasper-fanin/,
    # rust/jasper-outputd/). Trixie ships rustc 1.85; the effective floor is 1.82
    # (jasper-daemon, jasper-tts-protocol).
    # meson + ninja-build are installed ahead of time for the optional
    # enhanced-AEC root oneshot. A normal deploy builds only the quick v1
    # binding; an explicit Advanced → Software action compiles v2 later in a
    # contained background job.
    # libasound2-plugins is REQUIRED for the rate_converter line in
    # deploy/alsa/asoundrc.jasper. Without it ALSA silently falls back
    # to the linear resampler which loses ~12 dB of 4-8 kHz content
    # during 44.1→48 conversion, which sabotages AEC speech-band
    # performance.

    _install_renderer_native_deps
}

install_streambox_deps() {
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        build-essential rustc cargo \
        libasound2-dev libasound2 portaudio19-dev \
        libasound2-plugins libsndfile1 \
        curl ca-certificates rsync \
        nginx-light openssl \
        dnsmasq-base \
        snapclient snapserver

    _install_renderer_native_deps
}
