#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Install jasper voice daemon + always-on CamillaDSP on a Raspberry Pi.
#
# Source-builds shairport-sync (AirPlay 2) + nqptp, drops in
# librespot (rust, via raspotify .deb) + bluez-alsa + JTS no-code
# Bluetooth pairing agent,
# owns the full systemd unit per renderer.
#
# Two install tiers, set via JASPER_INSTALL_PROFILE=full|streambox (default
# full): the streambox profile is the Zero-2-W-class local-renderer-only tier
# and skips voice/wake-word/GEMINI-dependent features — see the
# INSTALL_STEPS table below. The pre-reqs listed here are full-tier only.
#
# Idempotent: re-running upgrades the venv and re-applies configs.
#
# Pre-reqs the operator handles by hand (full tier):
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
# The group-`jasper-secrets` secret compartment, a SIBLING of
# STATE_DIR (not under it): STATE_DIR is jasper-voice/-mux's StateDirectory,
# whose recursive chown would force this tree's group back to `jasper`.
SECRETS_DIR="/var/lib/jasper-secrets"
INTSECRETS_DIR="/var/lib/jasper-intsecrets"
SYSTEMD_DIR="/etc/systemd/system"
INSTALL_PROFILE_DEFAULT="full"
INSTALL_PROFILE_MARKER="${STATE_DIR}/install_profile"

source "${REPO_DIR}/deploy/lib/jasper-sed-inplace.sh"
source "${REPO_DIR}/deploy/lib/jasper-env-file.sh"
source "${REPO_DIR}/deploy/lib/jasper-asound-render.sh"
source "${REPO_DIR}/deploy/lib/jasper-alsa-card.sh"
source "${REPO_DIR}/deploy/lib/install/env-migrations.sh"
source "${REPO_DIR}/deploy/lib/install/service-users.sh"
source "${REPO_DIR}/deploy/lib/install/memory-resilience.sh"
source "${REPO_DIR}/deploy/lib/install/build-sandbox.sh"
source "${REPO_DIR}/deploy/lib/install/renderers.sh"
source "${REPO_DIR}/deploy/lib/install/web-assets.sh"
source "${REPO_DIR}/deploy/lib/install/model-staging.sh"
source "${REPO_DIR}/deploy/lib/install/first-party-runtime.sh"
source "${REPO_DIR}/deploy/lib/install/rust-daemons.sh"
# Ring platform: builds the jts_ring ALSA ioplug + ships its conf.d/tmpfiles
# assets. Sourced after build-sandbox.sh (uses run_contained_build).
source "${REPO_DIR}/deploy/lib/install/ring-platform.sh"
source "${REPO_DIR}/deploy/lib/install/python-runtime.sh"
source "${REPO_DIR}/deploy/lib/install/systemd-units.sh"
# Hash-pinned vendored source for the optional enhanced AEC engine. This file
# is also parsed by jasper.enhanced_aec; do not duplicate these values here.
# shellcheck source=jasper_aec3/enhanced-aec-source.env
source "${REPO_DIR}/jasper_aec3/enhanced-aec-source.env"

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
# speakers).
RASPOTIFY_VERSION="0.48.1"
RASPOTIFY_URL="https://github.com/dtcooper/raspotify/releases/download/${RASPOTIFY_VERSION}/raspotify_${RASPOTIFY_VERSION}.librespot.v0.8.0-ea81314_arm64.deb"
RASPOTIFY_SHA256="dc1bc4d209378ef1f8348fd7aa6d1a7865fa83abc30c08990d171012d038a717"
SHAIRPORT_SYNC_VERSION="5.2.3"
SHAIRPORT_SYNC_COMMIT="7b1bee65b2b0f8fee2e34684db4e20a53cd6c13a"
NQPTP_COMMIT="c925f27c1fd12e4033ac477e5a405969b0b0260b"
# Upstream provenance (auto-generated archive, not fetched by install.sh):
# https://github.com/mikebrady/nqptp/archive/${NQPTP_COMMIT}.tar.gz
NQPTP_ARCHIVE_URL="https://github.com/jaspercurry/JTS/releases/download/build-deps-v1/nqptp-c925f27c1fd1.tar.gz"
NQPTP_SHA256="d2c2fe5d2574d447a817b1585e82c38f4c98774dac8284e5a3f17e188a3a75f9"
# Upstream provenance (auto-generated archive, not fetched by install.sh):
# https://github.com/mikebrady/shairport-sync/archive/${SHAIRPORT_SYNC_COMMIT}.tar.gz
SHAIRPORT_SYNC_ARCHIVE_URL="https://github.com/jaspercurry/JTS/releases/download/build-deps-v1/shairport-sync-7b1bee65b2b0.tar.gz"
SHAIRPORT_SYNC_SHA256="c8d860c68723d78aea3d3eef0861bfbd01aa2f52d81c768c4e359ccabf42cbb5"
# One structured journald line for the installer, tagged so a deploy can be
# replayed with `journalctl -t jasper-install`. Best-effort: never fails a run.
# _mem_log and _build_sandbox_log keep their own copies of this call: their
# libs are sourced standalone (deploy/bin/jasper-contained-build, and tests),
# where a function install.sh defines does not exist.
jasper_install_log() {
    logger -t jasper-install -- "$*" 2>/dev/null || true
}

INSTALL_CURRENT_STEP=""

# The installer's failure record, called from install_exit_cleanup's
# _call_if_defined. A journal line rather than a /run marker: journald is
# persistent, so the record survives the reboot an operator reaches for first.
record_install_outcome() {
    local rc="$1"
    if [[ "${rc}" == "0" ]]; then
        return 0
    fi
    local line="event=install.failed rc=${rc} step=${INSTALL_CURRENT_STEP}"
    jasper_install_log "${line}"
    echo "  ${line}"
}

print_install_usage() {
    cat <<'EOF'
Usage: bash deploy/install.sh [--dry-run|--plan]

Options:
  --dry-run, --plan   Print the install plan and exit without requiring root.
  -h, --help          Show this help.

Environment:
  JASPER_INSTALL_DRY_RUN=1   Same as --dry-run.
  JASPER_INSTALL_PROFILE=full|streambox
                             Install tier. Unset/default is full speaker.
                             streambox is the Zero-class local renderer tier.
                             Legacy endpoint/satellite tokens map to streambox.
  JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=1
                             Allow a persisted install-profile change.
  JASPER_HOSTNAME=<name>.local
                             Speaker identity/cert hostname for direct
                             Pi-local installs. scripts/deploy-to-pi.sh
                             forwards this automatically.
  JASPER_FIRST_PARTY_RUNTIME_BUNDLE=<directory>
                             Optional extracted, local ARM64 runtime bundle.
                             Verification is fail-closed; unset preserves the
                             existing source-build path.
EOF
}

normalize_install_profile() {
    # Legacy endpoint/satellite tokens map to streambox so a field box with
    # a persisted endpoint marker auto-migrates on its next deploy. Mirror
    # of jasper.install_profile.normalize_install_profile.
    case "${1:-}" in
        ""|full)
            printf 'full\n'
            ;;
        streambox|endpoint|satellite)
            printf 'streambox\n'
            ;;
        *)
            echo "invalid JASPER_INSTALL_PROFILE=${1:-<empty>}; use full or streambox" >&2
            return 2
            ;;
    esac
}

read_persisted_install_profile() {
    local marker="${1:-${INSTALL_PROFILE_MARKER}}"
    if [[ ! -f "${marker}" ]]; then
        return 0
    fi
    local raw
    raw="$(head -n1 "${marker}" | tr -d '[:space:]')"
    [[ -n "${raw}" ]] || return 0
    normalize_install_profile "${raw}"
}

detect_default_install_profile() {
    local model_file="${JASPER_PI_MODEL_FILE:-/proc/device-tree/model}"
    local model=""
    if [[ -r "${model_file}" ]]; then
        model="$(tr -d '\000' < "${model_file}" | tr -d '\r\n')"
    fi
    case "${model}" in
        *"Raspberry Pi Zero 2 W"*|*"Raspberry Pi Zero 2"*)
            printf 'streambox\n'
            ;;
        *)
            printf '%s\n' "${INSTALL_PROFILE_DEFAULT}"
            ;;
    esac
}

# Detect the box's hardware tier (RAM / CPU / arch) once, up front. This
# is ORTHOGONAL to the install profile above: the profile is the product
# role (does this box run the voice brain?), the tier is hardware
# capability (how do I build safely here?). jts2 — a 1 GB Pi 5 on the
# `full` profile — is the proof they differ: small hardware, full role.
#
# Pure reporter: prints one normalized line and mutates nothing, so the
# dry-run plan, the real-install preflight, and tests can all call it.
# The tier names the RAM region the box is in for OBSERVABILITY — an OOM
# in a later build step is then self-evident in the deploy transcript. It
# is the first step toward one shared tier vocabulary for the build knobs
# that today read RAM independently (rust-daemons.sh's low-memory flip;
# build_sandbox_jobs' ~1.5 GB/job -j cap). Converging those knobs onto
# this helper is Workstream A; this change does NOT alter any build behavior.
# See docs/install-hardware-tier-and-staleness.md.
#
# Seams (all default to the real system; injectable so tests can drive
# the whole SKU matrix with no hardware):
#   JASPER_HW_MEMINFO_FILE  (default /proc/meminfo)
#   JASPER_HW_NPROC         (default `nproc`)
#   JASPER_HW_ARCH          (default `uname -m`)
detect_hardware_tier() {
    local meminfo="${JASPER_HW_MEMINFO_FILE:-/proc/meminfo}"
    local mem_kb
    mem_kb="$(awk '/^MemTotal:/ { print $2; exit }' "${meminfo}" 2>/dev/null || true)"
    case "${mem_kb}" in
        ""|*[!0-9]*) mem_kb=0 ;;
    esac
    # Declare then assign (not `local x="$(...)"`) so ShellCheck SC2155
    # doesn't fire and a subshell failure can't be masked.
    local cpus
    cpus="${JASPER_HW_NPROC:-$(nproc 2>/dev/null || echo 1)}"
    case "${cpus}" in
        ""|*[!0-9]*) cpus=1 ;;
    esac
    local arch
    arch="${JASPER_HW_ARCH:-$(uname -m 2>/dev/null || echo unknown)}"
    [[ -n "${arch}" ]] || arch="unknown"

    # The low boundary REUSES build-sandbox.sh's threshold (one source of
    # truth) so the label can't drift from the build knob it describes —
    # below it, the Rust low-memory build profile is already active.
    local low_kb="${RUST_LOW_MEMORY_BUILD_THRESHOLD_KB}"
    local tier
    if (( mem_kb == 0 )); then
        tier="unknown"
    elif (( mem_kb < low_kb )); then
        tier="low"
    elif (( mem_kb < 2097152 )); then
        tier="constrained"
    else
        tier="standard"
    fi
    printf 'ram_mb=%d cpus=%s arch=%s tier=%s\n' "$(( mem_kb / 1024 ))" "${cpus}" "${arch}" "${tier}"
}

# True when the detected/injected arch is a 64-bit ARM target JTS ships
# prebuilt binaries for (CamillaDSP aarch64, librespot arm64 .deb,
# CamillaGUI aarch64). 32-bit Pi OS (armv7l/armhf) — an easy Imager
# mis-pick on a Zero 2 W, which is arm64-capable but often imaged 32-bit
# — has no prebuilt path and fails deep in a fetch today.
_hardware_tier_arch_supported() {
    local arch
    arch="${JASPER_HW_ARCH:-$(uname -m 2>/dev/null || echo unknown)}"
    case "${arch}" in
        aarch64|arm64) return 0 ;;
        *) return 1 ;;
    esac
}

# Real-install preflight: log the detected tier (so the deploy transcript
# names it — closes the "failure wasn't self-evident" gap when a later
# build OOMs) and fail fast on an unsupported architecture before any
# mutation. A read-only preflight, like require_root; runs after the
# --dry-run early return so it never trips on x86 CI dry-runs.
hardware_tier_preflight() {
    local tier_line
    tier_line="$(detect_hardware_tier)"
    echo "  hardware tier: ${tier_line}"
    jasper_install_log "event=hardware_tier.detected ${tier_line}"

    if _hardware_tier_arch_supported; then
        return 0
    fi
    local arch
    arch="${JASPER_HW_ARCH:-$(uname -m 2>/dev/null || echo unknown)}"
    if _is_truthy "${JASPER_ALLOW_UNSUPPORTED_ARCH:-0}"; then
        echo "  WARN: unsupported architecture '${arch}'; JASPER_ALLOW_UNSUPPORTED_ARCH=1 set —" >&2
        echo "  proceeding, but the prebuilt CamillaDSP/librespot/CamillaGUI fetches will likely fail" >&2
        return 0
    fi
    cat >&2 <<EOF
ERROR: unsupported architecture '${arch}'.

JTS ships prebuilt 64-bit ARM binaries (CamillaDSP aarch64, librespot
arm64, CamillaGUI aarch64) and is supported only on 64-bit Raspberry Pi
OS (Trixie). Re-flash with the 64-bit image, or set
JASPER_ALLOW_UNSUPPORTED_ARCH=1 to attempt the install anyway (expect
the prebuilt fetches to fail).
EOF
    return 2
}

# The RAW first line of the marker, before normalization. Used only to
# detect a legacy endpoint/satellite marker so the migration to streambox
# can be logged once. Mirrors jasper.install_profile._normalize_with_migration_log.
read_raw_persisted_install_profile() {
    local marker="${1:-${INSTALL_PROFILE_MARKER}}"
    [[ -f "${marker}" ]] || return 0
    head -n1 "${marker}" | tr -d '[:space:]'
}

# True when the persisted marker carries a legacy endpoint/satellite token —
# i.e. this deploy auto-migrates the box to streambox. Lets main() emit a
# single greppable log line WITHOUT polluting resolve_install_profile's
# captured stdout (which is the resolved profile value).
# Tests pass an alternate marker path; main() calls it with no args (the
# canonical marker). shellcheck only sees the no-arg production call.
# shellcheck disable=SC2120
install_profile_legacy_marker_migrating() {
    local marker="${1:-${INSTALL_PROFILE_MARKER}}"
    local raw
    raw="$(read_raw_persisted_install_profile "${marker}")" || return 1
    case "${raw}" in
        endpoint|satellite) return 0 ;;
        *) return 1 ;;
    esac
}

# Test helpers pass an alternate marker path directly; production calls use the
# canonical marker. Shellcheck only sees the production path.
# shellcheck disable=SC2120
resolve_install_profile() {
    local marker="${1:-${INSTALL_PROFILE_MARKER}}"
    local requested="${JASPER_INSTALL_PROFILE:-}"
    local persisted requested_profile

    persisted="$(read_persisted_install_profile "${marker}")" || return $?
    if [[ -n "${requested}" ]]; then
        requested_profile="$(normalize_install_profile "${requested}")" || return $?
    elif [[ -n "${persisted}" ]]; then
        requested_profile="${persisted}"
    else
        requested_profile="$(detect_default_install_profile)" || return $?
    fi

    if [[ -n "${persisted}" && "${persisted}" != "${requested_profile}" ]] \
            && ! _is_truthy "${JASPER_ACCEPT_INSTALL_PROFILE_CHANGE:-0}"; then
        cat >&2 <<EOF
ERROR: install profile mismatch.

Persisted profile: ${persisted}
Requested profile: ${requested_profile}

Refusing to switch install tiers implicitly. Set
JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=1 only when intentionally converting
this Pi between the full speaker and streambox tiers.
EOF
        return 2
    fi

    printf '%s\n' "${requested_profile}"
}

persist_install_profile() {
    local profile="$1"
    local marker="${2:-${INSTALL_PROFILE_MARKER}}"
    profile="$(normalize_install_profile "${profile}")" || return $?
    # `install -d -m` re-chmods an EXISTING dir — the marker's parent is
    # STATE_DIR itself on the default marker path, so this briefly narrowed
    # an already-widened 0770 STATE_DIR to 0750 on every deploy, the same
    # trap ensure_state_dir closed (#3879). Only create, never re-chmod.
    local marker_dir
    marker_dir="$(dirname "${marker}")"
    [[ -d "${marker_dir}" ]] || install -d -m 0750 "${marker_dir}"
    local tmp="${marker}.tmp.$$"
    printf '%s\n' "${profile}" > "${tmp}"
    chmod 0644 "${tmp}"
    mv "${tmp}" "${marker}"
}

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

# The user the Rust daemon builds run as. build_install_jasper_* helpers
# chown their cargo cache dirs to this user
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

install.sh builds the Rust audio daemons (jasper-fanin and jasper-outputd)
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
        sink_mode = data.get("sink_mode") or "single_alsa"
        expected_dac = (
            "dual_apple_usb_c_dac_4ch"
            if sink_mode == "dual_apple"
            else "outputd_dac"
        )
        if data.get("dac", {}).get("pcm") != expected_dac:
            raise RuntimeError(
                f"dac.pcm={data.get('dac', {}).get('pcm')!r}, expected {expected_dac!r}"
            )
        # CONTENT PCM: keyed on the BRIDGE, never on sink_mode. This derived the
        # expectation from sink_mode + ACTIVE_CHANNELS and demanded
        # `outputd_active_content_capture` from a composite or active box. That
        # lane is deleted (#2534) and the hardware reconciler now writes
        # explicit-EMPTY for both shapes, so the old expectation would have
        # WARNed "jasper-outputd is not ready" on every deploy to every armed
        # roleful box in the fleet, jts.local included.
        #
        # Under the ring outputd reads the ring FILE and opens no content PCM at
        # all, so there is nothing for this probe to compare; jasper-doctor's
        # check_outputd_service owns the ring-side rule (it rejects the retired
        # snd-aloop name) and runs later in this same install via
        # run_doctor_summary. Keeping a second copy of that rule here is what
        # let this one go stale in the first place.
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
    # State + emitted-correction-config dirs. outputd uses
    # outputd-statefile.yml so corrections survive Pi restarts. The
    # room-correction wizard writes correction_<id>_<unixtime>.yml
    # under configs/.
    install -d -m 0755 /var/lib/camilladsp
    # configs/ is written atomically (temp file in-dir + rename) by the non-root
    # jasper-web user for active-speaker staging and
    # room-correction configs, so it must be group-writable from its FIRST
    # creation — not only after the later widen step below. A deploy that stops
    # between here and that widen (or a future reorder) must not leave it
    # root-only, or non-root staging fails with PermissionError and surfaces to
    # the household as "could not load the silent active-speaker setup" (the
    # jts3 2026-07-06 incident). check_camilla_configs_writable pins this at
    # runtime.
    if getent group jasper >/dev/null 2>&1; then
        install -d -m 2775 -g jasper /var/lib/camilladsp/configs
    else
        install -d -m 0755 /var/lib/camilladsp/configs
    fi
    ensure_state_dir
    # Shared correction/test artifacts are written by the correction web flow and
    # by jasper-web's active-speaker commissioning tone path. Keep the tree
    # group-writable for the dropped service users instead of root-only.
    #
    # The active_speaker* paths below are the same capture/sweep/tone trees
    # /sound/ and /sound/room/ share; this list must stay in sync with
    # heal_shared_state_modes's allowlist (env-migrations.sh), which re-heals
    # the same seven paths on every deploy for boxes that pre-date this line.
    install -d -m 2770 -g jasper \
        /var/lib/jasper/correction \
        /var/lib/jasper/correction/sweeps \
        /var/lib/jasper/correction/captures \
        /var/lib/jasper/correction/sessions \
        /var/lib/jasper/correction/calibration_mics \
        /var/lib/jasper/correction/tones \
        /var/lib/jasper/active_speaker \
        /var/lib/jasper/active_speaker/campaigns \
        /var/lib/jasper/active_speaker/sessions \
        /var/lib/jasper/active_speaker_captures \
        /var/lib/jasper/active_speaker_sweeps \
        /var/lib/jasper/active_speaker_stimuli \
        /var/lib/jasper/active_speaker_tone_artifacts

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

    # The flat outputd startup graph is copied here as a fallback/template,
    # then regenerated after the Python package is installed and the current
    # output hardware state has been observed so the active DAC's latency
    # floor reaches fresh first boot. install_alsa() handles the dongle name
    # in /etc/asound.conf.
    install -m 0644 \
        "${REPO_DIR}/deploy/camilladsp/outputd-cutover.yml" \
        "${CAMILLA_CONF}/outputd-cutover.yml"

    # The outputd topology uses a separate Camilla statefile instead of
    # overwriting /var/lib/camilladsp/statefile.yml. Do not repair that
    # statefile here: the safe target depends on the saved output topology.
    # This flat graph maps full-range stereo directly to DAC outputs. It is
    # selectable only for an explicit passive mono/stereo layout; unconfigured,
    # incomplete, and any topology with a tweeter/protected role park instead.
    # After the Python package is installed,
    # ensure_outputd_camilla_statefile asks jasper.active_speaker's runtime
    # contract which graph is legal and fails closed if no protected graph
    # exists.

    # v1.yml (the pre-outputd rollback config, issue #2240) is no longer
    # installed by this function. Remove any copy left behind by a prior
    # install: an upgraded box that keeps it on disk indefinitely is still
    # selectable in camillagui's config picker (config_dir scans
    # /etc/camilladsp/*.yml) and can leave a flat-allowed statefile pointer
    # aimed at a file that writes to the now-removed pcm.jasper_out dmix.
    rm -f "${CAMILLA_CONF}/v1.yml"
}

run_captured_command() {
    # run_captured_command <output-variable> <command...>
    # Capture combined stdout/stderr, always replay it, and preserve the
    # command's success/failure for install steps that need the output again.
    local output_variable="$1"
    shift
    local command_output
    if ! command_output="$("$@" 2>&1)"; then
        printf -v "${output_variable}" '%s' "${command_output}"
        printf '%s\n' "${command_output}"
        return 1
    fi
    printf -v "${output_variable}" '%s' "${command_output}"
    printf '%s\n' "${command_output}"
}

ensure_output_hardware_state() {
    # The CamillaDSP latency floor resolver reads the same output-hardware
    # state file the reconciler owns. A fresh install must write it before the
    # flat startup graph is generated or the generator falls back to the
    # conservative global 1024/2048 default.
    local output
    echo "  Writing output hardware state before Camilla statefile seed"
    if ! run_captured_command output env \
        JASPER_OUTPUT_HARDWARE_STATE_PATH=/run/jasper-output-hardware/output_hardware.json \
        JASPER_APLAY="${JASPER_APLAY:-aplay}" \
        /opt/jasper/.venv/bin/python -m jasper.cli.output_hardware --write; then
        return 1
    fi
}

_render_outputd_cutover_configs() {
    # `jasper-sound render-flat-cutover` wraps
    # jasper.sound.camilla_yaml.render_flat_cutover_configs — the ONE writer of
    # this file. The root reconciler (jasper-audio-hardware-reconcile) and
    # jasper-output-topology-reset call the same command, so the graph a box
    # boots cannot depend on which writer ran last. An inline heredoc here is
    # exactly how a second spelling gets born.
    /opt/jasper/.venv/bin/jasper-sound render-flat-cutover
}

render_outputd_cutover_config() {
    # Design call for #27: generate the seeded flat startup config through the
    # production outputd graph, not a bypass or a hand-edited static YAML. The
    # active-speaker runtime contract below still decides whether flat is legal
    # for the saved topology.
    #
    # The active DAC profile's Camilla floor is deliberately NOT applied to this
    # graph: both its halves are the SHM ring, and the ioplug pins the ring's
    # period bytes min==max, so a profile floor would fail the open rather than
    # raise it. The floor still reaches every emit whose playback is an ordinary
    # ALSA device.
    local output
    echo "  Rendering outputd flat startup config (ring geometry)"
    if ! run_captured_command output _render_outputd_cutover_configs; then
        return 1
    fi
}

ensure_outputd_camilla_statefile() {
    # Runtime graph selection belongs to jasper.active_speaker, not install.sh.
    # This flat graph maps full-range stereo directly to DAC outputs. It is
    # selectable only for an explicit passive mono/stereo layout; unconfigured,
    # incomplete, and any topology with a tweeter/protected role park instead.
    local output
    echo "  Checking outputd Camilla statefile against active-speaker runtime contract"
    if ! run_captured_command output \
        /opt/jasper/.venv/bin/jasper-active-speaker runtime-safe-graph \
        --statefile /var/lib/camilladsp/outputd-statefile.yml \
        --flat-config "${CAMILLA_CONF}/outputd-cutover.yml" \
        --write-statefile; then
        return 1
    fi
    if [[ "${JASPER_RESTART_CAMILLA_ON_STATEFILE_REPAIR:-0}" == "1" ]] \
       && [[ "${output}" == *"statefile written: yes"* ]]; then
        echo "  Restarting jasper-camilla.service after statefile repair"
        systemctl restart jasper-camilla.service 2>/dev/null || \
            echo "  WARN: jasper-camilla restart failed after statefile repair. Check logs with: journalctl -u jasper-camilla -e"
    fi
}

reconcile_sound_dsp_state() {
    # Generated CamillaDSP YAML is a cache of saved JTS sound intent. After a
    # deploy changes DSP render semantics, refresh only JTS-owned/re-renderable
    # graphs through the normal sound apply transaction. Fail open: the safety
    # statefile guard above has already ensured the current graph is legal.
    local output
    if [[ ! -x /opt/jasper/.venv/bin/jasper-sound ]]; then
        echo "  WARN: jasper-sound CLI missing; skipping sound DSP reconcile"
        return 0
    fi
    echo "  Reconciling current sound DSP graph"
    local -a cmd=(/opt/jasper/.venv/bin/jasper-sound reconcile-current-dsp --fail-open)
    if command -v timeout >/dev/null 2>&1; then
        cmd=(timeout --kill-after=5s 30s "${cmd[@]}")
    else
        echo "  WARN: coreutils timeout missing; sound DSP reconcile may block"
    fi
    local status
    set +e
    output="$("${cmd[@]}" 2>&1)"
    status=$?
    set -e
    if (( status != 0 )); then
        printf '%s\n' "${output}"
        if (( status == 124 || status == 137 )); then
            echo "  WARN: sound DSP reconcile timed out after 30s; leaving current legal graph in place"
        else
            echo "  WARN: sound DSP reconcile command failed; leaving current legal graph in place"
        fi
        return 0
    fi
    printf '%s\n' "${output}"
}

ensure_crossover_camilla_statefile() {
    # Seed camilla#2's OWN statefile (crossover-statefile.yml) so the
    # endpoint-crossover instance (jasper-camilla-crossover.service, :1235)
    # has a config to load on first start (the unit has no positional
    # config — same CamillaDSP-v4 statefile-clobber reason as camilla#1).
    #
    # Reuses the SAME active-speaker runtime contract as
    # ensure_outputd_camilla_statefile (jasper-active-speaker
    # runtime-safe-graph), which on a roleful/protected topology — the ONLY
    # topology where camilla#2 is meaningful — selects the DRIVER-DOMAIN
    # (Layer-A-intact) baseline / all-muted active startup graph and NEVER
    # the flat fallback (the contract's `select_flat` branch is gated by
    # `topology_allows_flat_dac_graph`; see
    # jasper/active_speaker/runtime_contract.py). So an active box gets a
    # tweeter-safe driver-domain seed.
    #
    # PARKED DEFAULT (issue #2135): a roleful box that has staged no startup
    # graph yet seeds the PARKED graph here instead — a File sink to /dev/null
    # with every output hard muted. Before #2135 this call BLOCKED on such a
    # box (exit 1), which failed the whole install. Same benign-seam reasoning
    # as the flat case below, and strictly safer than it: camilla#2 is INERT
    # until the grouping reconciler arms it, and `seed_crossover_statefile`
    # (jasper/multiroom/active_leader_config.py, called from the reconciler's
    # active-leader bake arm) repoints this statefile at the re-proven
    # driver-domain config immediately before enabling the unit. If camilla#2
    # ever DID start on the parked pointer it would emit silence, where the
    # flat pointer would send full range to a tweeter.
    #
    # SEAM FLAGGED FOR THE RECONCILER PR: on an explicit valid passive box
    # the contract returns flat, so this would seed flat into a file named
    # crossover-statefile.yml. That is BENIGN today because camilla#2 is
    # INERT there (the unit is never enabled), so the flat seed is never
    # loaded. The crossover guard does NOT convert a flat statefile —
    # it acts only on a dead bonded pipe — so the driver-domain guarantee
    # for an ARMED camilla#2 rests on the reconciler seeding it at arm time,
    # not on the guard. The later
    # reconciler PR — which knows when the box is actually an active
    # leader — should refine this to seed the EXACT driver-domain baseline
    # (not whatever runtime-safe-graph returns for a passive topology)
    # at the moment it arms the unit. We do NOT author that here: emitting
    # a precise driver-domain baseline is jasper/active_speaker/* code,
    # outside this unit's scope fence.
    #
    # We never restart the unit (it is not enabled), so there is no
    # JASPER_RESTART_* knob here — only the seed write.
    local output
    echo "  Seeding camilla#2 crossover statefile via active-speaker runtime contract"
    if ! run_captured_command output \
        /opt/jasper/.venv/bin/jasper-active-speaker runtime-safe-graph \
        --statefile /var/lib/camilladsp/crossover-statefile.yml \
        --flat-config "${CAMILLA_CONF}/outputd-cutover.yml" \
        --write-statefile; then
        return 1
    fi
}

find_card() {
    # find_card "<aplay|arecord>" "<grep regex>"
    jasper_find_alsa_card "$1" "$2"
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
        echo "  Apple dongle: not detected"
    fi
    echo "  Output DAC: CARD=${OUTPUT_DAC_CARD}"
    echo "  Output DAC id: ${OUTPUT_DAC_ID}"
    export DONGLE_CARD APPLE_DONGLE_PRESENT APPLE_DONGLE_SERVICE_CARD
    export OUTPUT_DAC_CARD OUTPUT_DAC_ID OUTPUT_DAC_RECOGNIZED
}

# snd-aloop binds index/pcm_substreams/pcm_notify at module load, and on a
# full-RAM box jasper-fanin holds the Loopback capture sides, so the unload is
# EBUSY (#4027) and changed options wait for the next boot. The low-RAM
# profiles park fanin first; there the reload succeeds, gated by #4218.
install_snd_aloop_options() {
    local shipped="${REPO_DIR}/deploy/modprobe.d/snd-aloop.conf"
    local installed=/etc/modprobe.d/snd-aloop.conf
    local changed=0 module_busy=0 sysfs="$1"
    cmp -s "${shipped}" "${installed}" || changed=1
    install -m 0644 "${shipped}" "${installed}"
    if [[ -d "${sysfs}" ]] && ! rmmod snd_aloop 2>/dev/null; then
        module_busy=1
    fi
    modprobe snd-aloop
    if (( ! module_busy )); then
        _set_reboot_required_reason snd_aloop ""
    elif (( changed )); then
        echo "  snd-aloop: options changed but module busy; deferred to reboot"
        _set_reboot_required_reason snd_aloop \
            "snd-aloop options changed, module busy — reboot to apply"
    fi
}

install_alsa() {
    install -d -m 0755 /etc/modules-load.d /etc/alsa/conf.d /etc/modprobe.d
    install -m 0644 \
        "${REPO_DIR}/deploy/modules-load.d/snd-aloop.conf" \
        /etc/modules-load.d/snd-aloop.conf
    install_snd_aloop_options /sys/module/snd_aloop

    select_audio_hardware_roles

    # /etc/asound.conf provides the system-wide ALSA PCM definitions; its own
    # header owns what they are and why (deploy/alsa/asoundrc.jasper).
    # It MUST stay world-readable: renderers run as non-root users
    # (shairport-sync, librespot as `pi`) and cannot otherwise resolve the
    # user-space PCM names it declares. The backup's grep guard keys on our
    # own content; a symlink is ours (created below) and is never backed up.
    if [[ -f /etc/asound.conf && ! -L /etc/asound.conf ]] \
            && ! grep -q "shairport_substream" /etc/asound.conf 2>/dev/null; then
        cp /etc/asound.conf "/etc/asound.conf.pre-jasper.$(date +%s)"
        echo "  Backed up pre-existing /etc/asound.conf (.pre-jasper.*); see PR #223."
    fi
    install -d -m 0755 "${ENV_DIR}"
    ensure_state_dir
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-render-asound-conf" \
        /usr/local/sbin/jasper-render-asound-conf
    if [[ ! -e "${STATE_DIR}/audio_quality.env" ]]; then
        jasper_env_file_set "${STATE_DIR}/audio_quality.env" \
            JASPER_ALSA_RATE_CONVERTER samplerate_medium 0644 0770
        echo "  /var/lib/jasper/audio_quality.env defaulted to samplerate_medium."
    fi
    install -d -m 0755 /var/lib/jasper-asound
    install -m 0644 \
        "${REPO_DIR}/deploy/alsa/asoundrc.jasper" \
        "${ENV_DIR}/asoundrc.jasper.source"
    jasper_asound_render_template \
        "${ENV_DIR}/asoundrc.jasper.source" \
        "${ENV_DIR}/asoundrc.jasper.template"
    chmod 0644 "${ENV_DIR}/asoundrc.jasper.template"
    /usr/local/sbin/jasper-render-asound-conf
    ln -sfn /var/lib/jasper-asound/asound.conf /etc/asound.conf
    chmod 0644 /var/lib/jasper-asound/asound.conf
    echo "  Wrote /etc/asound.conf with fan-in and outputd lanes"
}

# Resolve the short build SHA for THIS install run, with the same
# precedence write_build_manifest uses: deploy env var (the normal
# laptop-driven path) → git in the rsynced checkout (Pi-local installs) →
# the prior manifest → "unknown". Factored out so the landing page's
# app.css cache-bust and the build manifest agree by construction even
# though the manifest is now written LAST (see write_build_manifest).
resolve_build_sha_short() {
    local sha="${JASPER_DEPLOY_SHA:-}"
    if [[ -z "${sha}" ]] && command -v git >/dev/null 2>&1 && \
       { [[ -d "${REPO_DIR}/.git" ]] || git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; }; then
        sha=$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || true)
    fi
    if [[ -z "${sha}" && -f "${STATE_DIR}/build.txt" ]]; then
        sha=$(grep -E '^JASPER_GIT_SHA=' "${STATE_DIR}/build.txt" 2>/dev/null | head -1 | cut -d= -f2-)
    fi
    printf '%s\n' "${sha:-unknown}"
}

write_build_manifest() {
    # Build manifest = the VERIFIED-INSTALL success marker, NOT a "we
    # started installing X" note. It is written ONCE, as the final
    # mutation in main(), so `set -euo pipefail` guarantees every
    # build/install/migration step above ran to completion before this
    # line is reached. A mid-install abort (e.g. the OOM-killed WebRTC
    # build on jts2, 2026-06-21) therefore leaves the PRIOR good manifest
    # untouched — so the deploy direction-guard and the /system "Software"
    # card never advertise a SHA the box is not cleanly running. See
    # ADR-0172.
    #
    # JASPER_INSTALL_STATUS=ok records exactly that honest claim: the
    # install process for this SHA completed. (Runtime subsystem health —
    # is voice up? is the mic present? — is a separate layer the deploy
    # verifier surfaces post-restart; the install can't attest to it
    # because it doesn't restart the hardware-gated daemons.)
    local git_sha git_full git_branch
    git_sha="$(resolve_build_sha_short)"
    git_full="${JASPER_DEPLOY_SHA_FULL:-}"
    git_branch="${JASPER_DEPLOY_BRANCH:-}"
    if [[ ( -z "${git_full}" || -z "${git_branch}" ) ]] && command -v git >/dev/null 2>&1 && \
       { [[ -d "${REPO_DIR}/.git" ]] || git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; }; then
        [[ -z "${git_full}" ]] && git_full=$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || true)
        [[ -z "${git_branch}" ]] && git_branch=$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    fi
    if [[ ( -z "${git_full}" || -z "${git_branch}" ) && -f "${STATE_DIR}/build.txt" ]]; then
        [[ -z "${git_full}" ]] && git_full=$(grep -E '^JASPER_GIT_SHA_FULL=' "${STATE_DIR}/build.txt" 2>/dev/null | head -1 | cut -d= -f2-)
        [[ -z "${git_branch}" ]] && git_branch=$(grep -E '^JASPER_GIT_BRANCH=' "${STATE_DIR}/build.txt" 2>/dev/null | head -1 | cut -d= -f2-)
    fi
    git_full="${git_full:-unknown}"
    git_branch="${git_branch:-unknown}"

    # Atomic write: this is the success marker, so a torn write (power loss
    # mid-cat) must never leave a half-line the direction-guard misreads.
    # Mirrors persist_install_profile's tempfile+rename. STATE_DIR already
    # exists by the end of main(); we don't re-`install -d` it so we can't
    # clobber the group-writable widening done earlier in the run.
    local tmp="${STATE_DIR}/build.txt.tmp.$$"
    cat > "${tmp}" <<EOF
JASPER_GIT_SHA=${git_sha}
JASPER_GIT_SHA_FULL=${git_full}
JASPER_GIT_BRANCH=${git_branch}
JASPER_INSTALL_AT=$(date -Iseconds)
JASPER_INSTALL_STATUS=ok
EOF
    chmod 0644 "${tmp}"
    mv -f "${tmp}" "${STATE_DIR}/build.txt"
    echo "  Build manifest (verified install): ${git_sha} on ${git_branch}"
}

migrate_calibration_sign_convention() {
    # A measurement mic's vendor calibration file (miniDSP UMIK, Dayton)
    # states the MICROPHONE'S RESPONSE; the correction JTS applies is its
    # negation. Records fetched before 2026-07-27 were stored claiming the
    # opposite, so every measurement they calibrated carried twice the
    # file's value with the wrong sign. New fetches are fixed at the source
    # (jasper.audio_measurement.calibration.SUPPORTED_MODELS); this repairs
    # what is already on disk. Keyed on each record's own stored convention,
    # so it is idempotent and can never double-negate a correct record, and
    # it is a no-op on a speaker that never fetched a vendor calibration.
    if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
        # Pre-venv ordering (or a failed runtime install): say so rather than
        # returning silently, so "no line in the transcript" never has to be
        # read as either "nothing to repair" or "step vanished".
        echo "  mic calibration sign convention: skipped (no ${INSTALL_DIR}/.venv/bin/python yet)"
        return 0
    fi
    local output
    if output="$("${INSTALL_DIR}/.venv/bin/python" - <<'PY' 2>&1
from jasper.audio_measurement.calibration import migrate_stored_sign_conventions

counts = migrate_stored_sign_conventions()
# `uploads_untouched` is the household-visible number the doctor's
# "uploaded calibration sign" advisory follows up on: uploaded records carry
# the household's OWN sign declaration and are never flipped here.
print(
    "repaired={} scanned={} already_response={} uploads_untouched={} "
    "unreadable={} write_failed={}".format(
        counts["migrated_rederived"] + counts["migrated_negated"],
        counts["scanned"],
        counts["already_response"],
        counts["skipped_not_vendor"],
        counts["unreadable"],
        counts["write_failed"],
    )
)
PY
    )"; then
        echo "  mic calibration sign convention: ${output}"
    else
        # Non-fatal: a household with no stored vendor calibration loses
        # nothing, and aborting a deploy over a metadata repair would be a
        # worse outcome than a loud line. Records stay as they were.
        echo "  WARNING: mic calibration sign-convention migration failed: ${output}"
    fi
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
    ensure_state_dir
    # /wake owns the independent host-microphone preference. Seed it Off so a
    # fresh install never exports room audio merely because USB Audio Input is
    # enabled; the UI must record an explicit household choice first.
    if [[ ! -f "${STATE_DIR}/usb_mic.env" ]]; then
        printf 'JASPER_USB_MIC=disabled\nJASPER_USB_MIC_LEG=primary\n' \
            > "${STATE_DIR}/usb_mic.env"
        chmod 0644 "${STATE_DIR}/usb_mic.env"
    fi
    # aec_mode.env has one BASH writer: ensure_mode_file in the run below.
    local aec_bridge_marker="/run/jasper-aec-reconcile/aec-bridge-ready"
    systemctl enable jasper-aec-reconcile.service
    if ! /usr/local/sbin/jasper-aec-reconcile --reason install; then
        echo "  WARN: AEC/mic reconcile failed. Check logs with: journalctl -u jasper-aec-reconcile -e"
        if [[ -e "$aec_bridge_marker" ]]; then
            echo "  WARN: AEC bridge marker still present ($aec_bridge_marker) from a prior pass"
        else
            echo "  WARN: AEC bridge marker absent ($aec_bridge_marker); echo cancellation is off until the next reconcile"
        fi
    fi
}

reconcile_grouping_state() {
    # Grouping reconciler runs at BOOT (and on every install) so a BONDED
    # speaker survives reboots/deploys: it re-derives the snapcast args +
    # the outputd round-trip lane env, drives the CamillaDSP bonded/solo
    # config, pins the snapcast stream bindings, and (re)starts the snap
    # units per the wizard intent. On a solo speaker it is a no-op
    # oneshot (grouping off => stop both units, clear derived env) —
    # cost-free. This enables the RECONCILER, not grouping: snapserver/
    # snapclient still ship disabled and only the reconciler starts them
    # on explicit wizard opt-in.
    systemctl enable jasper-grouping-reconcile.service
    systemctl restart jasper-grouping-reconcile.service || \
        echo "  WARN: grouping reconcile failed. Check logs with: journalctl -u jasper-grouping-reconcile -e"
}

resolve_fanin_coupling_default() {
    # Enable the boot-time default-resolution unit AND run the pass once now so
    # this deploy converges the box onto the shipped defaults:
    #   - fan-in coupling: the ring, the only central transport (ADR-0100);
    #   - USB combo (JASPER_FANIN_USB_DIRECT + _HOST_CLOCK + _RESAMPLER_CUSHION_DECAY):
    #     enabled on a gadget box (dtoverlay=dwc2,dr_mode=peripheral present), else
    #     cleared.
    # An already-converged box remains a zero-churn confirm.
    # Mirrors reconcile_aec_state / reconcile_grouping_state: reconciler is the
    # single env writer; daemons read the resolved env. The reconciler CLI hydrates
    # its own env (load_env_files) so the camilla re-emit keeps the tuned chunksize.
    systemctl enable jasper-fanin-coupling-auto.service
    /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile --auto --reason install || \
        echo "  WARN: fan-in coupling default resolution failed. Check logs with: journalctl -u jasper-fanin-coupling-auto -e"
}

provision_correction_tls() {
    # /sound/room/ requires HTTPS because getUserMedia (mic capture)
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
    # See deploy/nginx-jasper.conf "Why HTTPS is added back" for context.
    local hostname="${JASPER_HOSTNAME:-jts.local}"
    local ca_dir=/var/lib/jasper/ca
    local ssl_dir=/etc/nginx/ssl
    install -d -m 0700 "${ca_dir}"
    install -d -m 0755 "${ssl_dir}"

    if [[ ! -f "${ca_dir}/ca.crt" || ! -f "${ca_dir}/ca.key" ]]; then
        echo "  generating /sound/room/ private CA at ${ca_dir}/ca.crt"
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
    echo "  /sound/room/ TLS provisioned (server cert for ${hostname}, CA at /usr/share/jasper-web/jts-root-ca.crt)"
}

install_management_static_assets() {
    local index_src="$1"
    local app_css_ver

    # Static landing page served at /. Plain HTML, no daemon — nginx
    # reads it directly via the `location = /` block in jasper.conf.
    # Updates require an `nginx -s reload` (handled by the caller)
    # but no service restart.
    install -d -m 0755 /usr/share/jasper-web
    install -m 0644 "${index_src}" /usr/share/jasper-web/index.html
    # Resolve the cache-bust SHA directly (deploy env, then the checkout, then
    # the prior manifest) rather than reading build.txt: the manifest is written
    # LAST, as the verified-install marker, so it still holds the PRIOR SHA at
    # this point in the run. resolve_build_sha_short returns the same value the
    # manifest will record, so the cache key matches the installed build.
    app_css_ver="$(resolve_build_sha_short)"
    [[ -n "${app_css_ver}" && "${app_css_ver}" != "unknown" ]] || app_css_ver="dev"
    # The renderer reads the control token itself, so it never reaches a shell
    # argument or the process table. The profile marker it needs was persisted
    # earlier in this run.
    if ! PYTHONPATH="${REPO_DIR}" python3 -m jasper.web.landing \
            /usr/share/jasper-web/index.html \
            --app-css-version "${app_css_ver}" \
            --hub-dir /usr/share/jasper-web; then
        echo "  ERROR: failed to render the landing page; refusing to ship a broken page" >&2
        return 1
    fi
    echo "  landing page + /sound/ and /assistant/ hubs: rendered (capabilities, control token, icon sprite)"
    # All /assets/ content (app.css, fonts, per-page CSS + ES modules) +
    # the .install-manifest the doctor verifies — see
    # deploy/lib/install/web-assets.sh for the copy shape and the
    # manifest contract.
    install_web_assets
}

tune_nginx_worker_processes() {
    # `worker_processes auto` starts one CPU-pinned worker per core to serve a
    # loopback-only management proxy: four workers, ~14 MB Pss on a Pi 5 and
    # three extra core-pinned processes on a realtime audio box. One is enough.
    #
    # This rewrites the packaged nginx.conf rather than dropping a file into
    # /etc/nginx/modules-enabled/, which nginx does include at main context:
    # worker_processes is a main-context directive and nginx rejects a second
    # copy with "is duplicate", which would fail the `nginx -t` gate on every
    # install. The cost of that choice is that nginx.conf is an nginx-common
    # dpkg conffile, so a later package upgrade reports it as locally modified
    # and keeps this copy. To undo on a box that already ran this, put
    # `worker_processes auto;` back in /etc/nginx/nginx.conf — dropping the
    # call here does not revert an installed box.
    #
    # Worker count is a comfort optimisation, so every failure path below
    # leaves the packaged value in place instead of failing the deploy.
    local main="${JTS_NGINX_MAIN_CONF:-/etc/nginx/nginx.conf}"
    if [[ ! -f "${main}" ]]; then
        echo "  ${main} not present; skipping nginx worker tuning."
        return 0
    fi
    local tmp mode
    if ! tmp="$(mktemp "${main}.jts.XXXXXX" 2>/dev/null)"; then
        echo "  WARN: could not stage an ${main} rewrite; workers left as packaged."
        return 0
    fi
    if ! sed -E 's/^([[:space:]]*)worker_processes[[:space:]]+[^;]+;/\1worker_processes 1;/' \
            "${main}" > "${tmp}"; then
        rm -f "${tmp}"
        echo "  WARN: could not rewrite ${main}; workers left as packaged."
        return 0
    fi
    if ! grep -qE '^[[:space:]]*worker_processes[[:space:]]+1;' "${tmp}"; then
        printf 'worker_processes 1;\n' >> "${tmp}"
    fi
    if cmp -s "${tmp}" "${main}"; then
        rm -f "${tmp}"
        return 0
    fi
    mode="$(stat -c '%a' "${main}" 2>/dev/null || stat -f '%Lp' "${main}" 2>/dev/null || true)"
    if ! { chmod "${mode:-644}" "${tmp}" && mv -f "${tmp}" "${main}"; }; then
        rm -f "${tmp}"
        echo "  WARN: could not publish ${main}; workers left as packaged."
        return 0
    fi
    echo "  nginx worker_processes pinned to 1 in ${main}"
}

install_nginx_site_conf() {
    # <site conf source> <nginx config root>. A conf in sites-enabled is on
    # disk at once and the next nginx restart loads it (Restart=always, see
    # nginx.service.d/jts-recovery.conf), so what `nginx -t` rejects is put
    # back — site conf and its snippet — from a fixed-name snapshot dir
    # outside sites-enabled, which nginx.conf includes unfiltered. Drop this
    # guard once the conf ships from a package that tests before enabling.
    local src="${1}" root="${2}" prev="${2}/.jasper-site-prev" rel=""
    local site="sites-enabled/jasper.conf" snip="snippets/jts-proxy-headers.conf"
    rm -rf "${prev}"
    install -d -m 0755 "${root}/snippets" "${prev}"
    for rel in "${site}" "${snip}"; do
        [[ -f "${root}/${rel}" ]] || continue
        cp -a "${root}/${rel}" "${prev}/"
    done
    install -m 0644 "${REPO_DIR}/deploy/nginx-proxy-headers.conf" "${root}/${snip}"
    install -m 0644 "${src}" "${root}/${site}"
    # nginx-light's enabled `default` site clashes with our default_server.
    rm -f "${root}/sites-enabled/default"
    if ! nginx -t; then
        echo "  ERROR: event=install.nginx_conf_rejected src=${src}" >&2
        for rel in "${site}" "${snip}"; do
            rm -f "${root}/${rel}"
            [[ -f "${prev}/${rel##*/}" ]] || continue
            cp -a "${prev}/${rel##*/}" "${root}/${rel}"
        done
        rm -rf "${prev}"
        return 1
    fi
    rm -rf "${prev}"
    systemctl enable --now nginx 2>/dev/null || true
    systemctl reload nginx
}

install_nginx_site() {
    # Standalone nginx site that reverse-proxies /spotify/ (multi-account
    # OAuth web flow) and /assistant/voice/ (voice-provider config wizard)
    # on plain HTTP. /sound/room/ and the /sound/* measurement routes are
    # proxied on both listeners, but browser mic capture only works on the
    # HTTPS one:
    # getUserMedia grants mic access in a secure context only. That origin is
    # the installer's own self-signed cert, so it is entered deliberately and
    # never by redirect — a cert interstitial is un-automatable (issue #2632).
    # The legacy routes stay HTTP — Spotify's HTTPS requirement is satisfied
    # by the GitHub Pages bounce, and there's no point breaking working flows
    # for one feature. /google/ stays HTTP here; Google rejects mDNS redirect
    # URIs, so it uses the same GitHub Pages bounce pattern as Spotify. The
    # correction-only cert is provisioned by provision_correction_tls() before
    # this function runs.
    install_management_static_assets "${REPO_DIR}/deploy/index.html"
    tune_nginx_worker_processes
    install_nginx_site_conf "${REPO_DIR}/deploy/nginx-jasper.conf" /etc/nginx
    echo "  nginx reloaded — http://<host>/{,spotify,voice} + https://<host>/{correction,google} are live"
}

install_streambox_nginx_site() {
    # Streambox uses the normal JTS landing page with capability-gated cards,
    # plus an nginx route set limited to local sources, DSP, grouping, and
    # system health. That keeps the frontend shared while omitting voice/wake
    # surfaces whose daemons are intentionally absent from this profile.
    install_management_static_assets "${REPO_DIR}/deploy/index.html"
    tune_nginx_worker_processes
    install_nginx_site_conf "${REPO_DIR}/deploy/nginx-jasper-streambox.conf" /etc/nginx
    echo "  streambox nginx reloaded — http://<host>/{,spotify,sources,sound,system,voice,google,transit,weather,ha,tools,chat} + https://<host>/{correction,sync} are live"
}

install_avahi_jasper_control() {
    # Advertise jasper-control over mDNS as the always-on discovery surface
    # used by the /rooms speaker directory and identity-aware automation.
    # The advertised file carries a name= TXT record with the
    # speaker's friendly display name (the /speaker identity), so the
    # /rooms directory shows friendly names. Because the name is a
    # per-runtime value, the file is RENDERED from a TEMPLATE rather
    # than copied statically: install the template OUT of
    # /etc/avahi/services/ (Avahi must not parse its __SPEAKER_NAME__
    # placeholder as XML — same reasoning as install_peering_template),
    # then let jasper.net.control_advert.render_control_advert substitute
    # the (XML-escaped) name, atomic-write the live file, and reload
    # Avahi. The /speaker save path re-renders on a name change.
    install -d -m 0755 /etc/jasper/avahi-templates
    install -m 0644 \
        "${REPO_DIR}/deploy/avahi/jasper-control.service.template" \
        /etc/jasper/avahi-templates/jasper-control.service

    # A non-root jasper-control renders the peering advert
    # (jasper-peer.service) into this dir when /sound/pair/ peering is enabled
    # (off by default). os.replace needs WRITE on the parent dir, which
    # ReadWritePaths= does NOT grant (it only lifts ProtectSystem=strict;
    # POSIX dir perms still apply). So when the `jasper` group exists, make the
    # dir group-jasper writable + setgid (new files inherit group jasper). The
    # static control advert below is still written by install.sh as root; a
    # future avahi apt-upgrade could reset this dir to root:root 0755, but every
    # deploy re-applies it. When the group is absent (pre-3b), stay 0755 root.
    if getent group jasper >/dev/null 2>&1; then
        install -d -m 2775 -g jasper /etc/avahi/services
    else
        install -d -m 0755 /etc/avahi/services
    fi
    # Render the live service from the template via the Python module
    # (it does the XML-escape, atomic write, and Avahi reload). The
    # package is already pip-installed by install_jasper above, so the
    # import resolves here. render_control_advert is fail-soft (returns
    # False, never raises); we still guard the whole call with `|| true`
    # plus a static-file fallback so a render failure can never leave
    # _jasper-control._tcp un-advertised — /rooms and jasper-doctor's
    # "avahi: _jasper-control._tcp" check depend on it always existing.
    local rendered=0
    if [[ -x "${INSTALL_DIR}/.venv/bin/python" ]] \
       && "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import sys

from jasper.net.control_advert import render_control_advert

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

install_jasper_control_polkit() {
    # The polkit grant for the non-root jasper-control user.
    # Without it, every systemctl/reboot/poweroff jasper-control runs (the
    # in-process restart broker + the system/shairport/grouping supervisors +
    # the /system buttons) is DENIED with "Interactive authentication required"
    # — silently breaking the Tier-3/Tier-5 recovery paths. polkitd monitors
    # /etc/polkit-1/rules.d and auto-reloads on change, so no reload/restart is
    # needed (a daemon-reload is for systemd units, not polkit). See
    # deploy/polkit/49-jasper-control.rules.
    install -d -m 0755 /etc/polkit-1/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/polkit/49-jasper-control.rules" \
        /etc/polkit-1/rules.d/49-jasper-control.rules
    echo "  Installed polkit rule for jasper-control (manage-units allowlist + reboot/power-off)"
}

install_jasper_web_polkit() {
    # The polkit grant for the non-root jasper-web user. The
    # /wifi/ wizard drives NetworkManager (scan / connect / forget / radio /
    # PSK re-read); NM's implicit defaults DENY a sessionless daemon for every
    # one of those, so without this rule a non-root jasper-web cannot manage
    # Wi-Fi — the worst-case brick for a headless, often Ethernet-less speaker.
    # polkitd monitors /etc/polkit-1/rules.d and auto-reloads (no restart). See
    # deploy/polkit/49-jasper-web.rules.
    install -d -m 0755 /etc/polkit-1/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/polkit/49-jasper-web.rules" \
        /etc/polkit-1/rules.d/49-jasper-web.rules
    echo "  Installed polkit rule for jasper-web (NetworkManager wifi management)"
}

widen_jasper_web_writable_dirs() {
    # The non-root jasper-web user atomically replaces files in
    # two root-owned dirs: /etc/bluetooth/main.conf (BlueZ name persistence
    # across a bluetooth.service restart — the /speaker rename) and generated
    # CamillaDSP sound profiles under /var/lib/camilladsp/configs (the /sound/
    # EQ editor). os.replace() needs WRITE on the *directory*, so make both
    # root:jasper 2775 (setgid → new files inherit group jasper). Mirrors
    # install_avahi_jasper_control's /etc/avahi/services widening (3b-2). The
    # ordinary sound-profile files inside keep their own owners (root reads/writes
    # them fine; the group-writable dir is what lets the dropped daemon swap them
    # atomically). Every generated YAML is also read by jasper-control /state or
    # jasper-web, so repair stale root:root 0600 files from earlier builds to
    # root:jasper 0640. The shared DSP-apply lock is written by root CLIs and
    # non-root web flows, so it must be group-writable.
    # Idempotent; harmless while jasper-web is still root.
    if getent group jasper >/dev/null 2>&1; then
        if [[ -d /etc/bluetooth ]]; then
            chgrp jasper /etc/bluetooth 2>/dev/null || true
            chmod 2775 /etc/bluetooth 2>/dev/null || true
        fi
        install -d -m 2775 -g jasper /var/lib/camilladsp/configs
        touch /var/lib/camilladsp/configs/.dsp_apply.lock
        chgrp jasper /var/lib/camilladsp/configs/.dsp_apply.lock 2>/dev/null || true
        chmod 0660 /var/lib/camilladsp/configs/.dsp_apply.lock 2>/dev/null || true
        find /var/lib/camilladsp/configs -maxdepth 1 -type f -name '*.yml' \
            -exec chgrp jasper {} + -exec chmod 0640 {} + 2>/dev/null || true
        # The Layer-A SSOT (active_speaker_baseline_profile.json) and the Active
        # run-record locks + records used to be healed here with path-following
        # chgrp/chmod. That is a local priv-esc under a group-writable
        # /var/lib/jasper (a group member can pre-create the name as a symlink
        # onto a root file), so it moved to heal_shared_state_modes, which pins
        # each inode with O_NOFOLLOW+fstat before touching it. See
        # deploy/lib/install/env-migrations.sh.
        echo "  Widened /etc/bluetooth + /var/lib/camilladsp/configs to root:jasper 2775 (jasper-web writes)"
    fi
}

ensure_peer_id() {
    # The identity peers key on across reboots. Regenerate only when there is
    # nothing usable: jasper/peering/config.py and scripts/_lib.sh's
    # verify_or_record_peer_id both accept any non-empty id, so replacing an
    # odd-looking one is what would abort the next deploy blaming a re-image.
    # The strip is that reader's twin, so a trailing CR is not "empty" here
    # and a recorded id there.
    local file="${STATE_DIR}/peer_id" pid tmp
    if [[ -f "${file}" ]] \
        && [[ -n "$(tr -d '[:space:]' 2>/dev/null < "${file}")" ]]; then
        return 0
    fi
    pid="$(tr -d '[:space:]' 2>/dev/null < /proc/sys/kernel/random/uuid || true)"
    if [[ -z "${pid}" ]]; then
        echo "  ERROR: could not generate peer_id (kernel uuid unreadable)" >&2
        exit 1
    fi
    tmp="$(mktemp "${STATE_DIR}/.peer_id.XXXXXX")"
    printf '%s\n' "${pid}" > "${tmp}"
    chmod 0644 "${tmp}"
    mv -f "${tmp}" "${file}"
    echo "  Generated stable peer_id at ${file}"
}

install_peering_template() {
    # Multi-device peering. The TEMPLATE goes under /etc/jasper/ so
    # Avahi doesn't try to parse it as a service file (the
    # placeholders __PEER_ID__ / __ROOM__ / __PRIMARY__ aren't valid
    # XML attribute values).
    #
    # jasper-control's peering daemon renders this template into
    # /etc/avahi/services/jasper-peer.service when JASPER_PEERING=on
    # is set in /var/lib/jasper/peering.env (via the /sound/pair/ Speakers
    # page). When peering is off (the default), no
    # rendered file exists and this Pi is invisible to siblings —
    # the goal property of "zero cost when alone".
    install -d -m 0755 /etc/jasper/avahi-templates
    install -m 0644 \
        "${REPO_DIR}/deploy/avahi/jasper-peer.service.template" \
        /etc/jasper/avahi-templates/jasper-peer.service
    ensure_state_dir
    ensure_peer_id
    echo "  Peering template installed; peering is OFF by default — enable at http://${JASPER_HOSTNAME:-jts.local}/sound/pair/"
}

regenerate_audio_cues() {
    # Bake the speaker's audible-failure cues so they're ready before
    # the daemon ever needs them. The daemon retries on every startup
    # if this fails, so a no-internet-at-install scenario is tolerated
    # — we just warn and continue.
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
    # deps, no pip resolution. Loopback-only since #2319
    # (deploy/systemd/camillagui.socket binds 127.0.0.1:5005) — unlike
    # the other unauthenticated, home-LAN-only management surfaces, this
    # one can author and live-apply CamillaDSP configs naming any device,
    # so it is not LAN-reachable. The landing page has no link to it (a
    # link that always connection-refuses is a silent failure); reach it
    # with `ssh -L 5005:localhost:5005 <pi-host>`.
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
    systemctl enable camillagui.socket
    # Restart (not just start/enable --now) so a ListenStream= change on
    # upgrade — e.g. the #2319 loopback rebind — actually takes effect. A
    # bare `start` is a no-op when the socket is already active from a
    # prior install and would silently leave the old bind (0.0.0.0:5005)
    # live until the next reboot: the same trap AGENTS.md documents for
    # jasper-web.socket (PR #118). Not swallowed with `|| true` like the
    # wizard-socket loop's restart — a failed rebind here leaves a
    # security-relevant posture unchanged (still LAN-reachable) and should
    # abort the install loudly rather than continue past it silently.
    systemctl restart camillagui.socket
    echo "  CamillaGUI listening on 127.0.0.1:5005 via socket-activated proxy"
    echo "  (backend exits 10 min after last access; ~50 MB Pss reclaimed)"
}

# See ADR-0242 for both properties. RAISE CONDITION for MemoryMax=96M: an
# OOM kill of this transient run-u*.service unit in the journal (the deploy
# wrapper's report_oom_collateral lists it).
run_doctor_summary() {
    echo; echo "=== jasper-doctor --core ==="
    local rc=0
    systemd-run --quiet --wait --pipe --collect \
        -p MemoryMax=96M -p RuntimeMaxSec=60 \
        /opt/jasper/.venv/bin/jasper-doctor --core || rc=$?
    jasper_install_log "event=install.doctor_core rc=${rc}"
    return "${rc}"
}

# The install, once. Rows are `name|profiles|fn|phrase` in execution order;
# `profiles` is full, streambox or both. main() runs every matching row and
# --dry-run renders every matching row, so the plan cannot drift from the run.
INSTALL_STEPS=(
    "build_user|both|require_build_user|require the 'pi' build user the Rust builds run as"
    "build_swap|both|setup_build_swap_if_needed|add temporary high-priority build swap on a low-RAM host"
    "service_users|both|create_jasper_service_users|create the jasper group and the non-root service users"
    "park_build_units|both|park_low_memory_build_units|park audio/runtime daemons before the Rust builds"
    "deps|full|install_deps|apt-get update and the full-tier runtime/build packages"
    "deps|streambox|install_streambox_deps|apt-get update and the streambox renderer/DSP packages"
    # install_alsa exports DONGLE_CARD, which install_systemd_units reads as
    # APPLE_DONGLE_SERVICE_CARD.
    "alsa|both|install_alsa|render /etc/asound.conf and apply the snd-aloop options"
    "camilladsp|both|install_camilladsp|fetch and install the pinned CamillaDSP binary"
    "renderers|both|install_renderers|build/install shairport-sync, nqptp, librespot and bluez-alsa"
    "headless_boot|both|reconcile_headless_boot_config|trim the Pi boot config for headless operation"
    "usb_role|both|reconcile_usb_data_role|reconcile the USB data role from board topology"
    "wifi_airplay|both|tune_wifi_for_airplay|disable WiFi power-save on the active wlan0 connection"
    "jasper|full|install_jasper|copy the Python package and build the full-tier venv"
    "jasper|streambox|install_streambox_jasper|copy the Python package and build the streambox venv"
    "secrets_perms|both|reassert_secrets_compartment_perms|re-assert the /var/lib/jasper-secrets compartment"
    "intsecrets_perms|both|reassert_intsecrets_compartment_perms|re-assert the /var/lib/jasper-intsecrets compartment"
    "mic_cal_sign|both|migrate_calibration_sign_convention|repair mic calibrations stored under the wrong sign convention"
    "output_hw_state|both|ensure_output_hardware_state|write output hardware state before the Camilla statefile seed"
    "outputd_config|both|render_outputd_cutover_config|render the outputd flat startup config"
    "outputd_statefile|both|ensure_outputd_camilla_statefile|seed or validate the outputd Camilla statefile"
    "crossover_statefile|both|ensure_crossover_camilla_statefile|seed the dormant camilla#2 crossover statefile"
    "fanin|both|build_install_jasper_fanin|build and install the jasper-fanin Rust daemon"
    "outputd|both|build_install_jasper_outputd|build and install the jasper-outputd Rust daemon"
    "ring_platform|both|install_jts_ring_platform|install the jts_ring ioplug, its conf.d drop-ins and the shm dir"
    # jasper-control renders its advert from the template and reads peer_id at
    # startup, so both land before the unit install restarts it.
    "avahi_control|both|install_avahi_jasper_control|install the Avahi service template for jasper-control"
    "peering_template|both|install_peering_template|seed peer_id and the peering advert template"
    # After every step that creates state, before the unit install restarts the
    # daemons that read /var/lib/jasper as group `jasper`.
    "state_modes|both|heal_shared_state_modes|heal the group modes on shared state files an upgrade left behind"
    "systemd_units|full|install_systemd_units|install, enable and start the full-tier systemd units"
    "systemd_units|streambox|install_streambox_systemd_units|install, enable and start the streambox systemd units"
    "retired_topology_state|both|remove_retired_audio_topology_state|remove the retired dmix/fanin topology switch state"
    "wifi_guardian|both|migrate_wifi_guardian|seed the WiFi guardian recovery stash"
    "memory_resilience|both|migrate_memory_resilience|apply the sysctl, MGLRU and zram memory resilience"
    "cgroup_memory|both|migrate_cgroup_memory_enabled|add the memory cgroup/PSI kernel args"
    "journald|both|install_journald_persistent_storage|enable persistent journald storage"
    "control_polkit|both|install_jasper_control_polkit|install the jasper-control polkit rules"
    "web_polkit|both|install_jasper_web_polkit|install the jasper-web NetworkManager polkit rules"
    "web_writable_dirs|both|widen_jasper_web_writable_dirs|widen /etc/bluetooth and the camilladsp configs for jasper-web"
    # provision_correction_tls first: the cert files must exist before nginx -t.
    "correction_tls|both|provision_correction_tls|provision the correction TLS CA and cert files"
    "nginx_site|full|install_nginx_site|install the full-tier nginx route set"
    "nginx_site|streambox|install_streambox_nginx_site|install the streambox nginx route set"
    "camillagui|full|install_camillagui|install the socket-activated CamillaGUI backend"
    "audio_cues|full|regenerate_audio_cues|regenerate the local audio cues"
    "control_env_modes|both|widen_control_secret_env_modes|widen the config/state files jasper-control reads"
    # ADR-0172: the manifest is the LAST mutation, so reaching it proves every
    # row above succeeded under set -e. The doctor row after it is read-only.
    "build_manifest|both|write_build_manifest|stamp the verified-install build manifest"
    "doctor|both|run_doctor_summary_advisory|run jasper-doctor --core as a non-blocking health summary"
)

# The doctor is advisory (ADR-0242): its rc is logged by run_doctor_summary and
# swallowed here so it cannot abort the install. Removal condition: drop this
# wrapper and point the row at run_doctor_summary when the core doctor gates.
run_doctor_summary_advisory() {
    run_doctor_summary || true
}

# The INSTALL_STEPS rows that run on `$1`, in order: the one owner of the match.
install_steps_for_profile() {
    local row profiles _
    for row in "${INSTALL_STEPS[@]}"; do
        IFS='|' read -r _ profiles _ _ <<<"${row}"
        case "${profiles}" in both|"$1") printf '%s\n' "${row}" ;; esac
    done
}

# --dry-run: the rows main() would run, in order, and nothing else.
print_install_plan() {
    local name phrase rows _
    rows="$(install_steps_for_profile "$1")"
    cat <<EOF
==> JTS install plan (dry run) - profile: $1
Nothing below is executed. Ahead of the table main() reports the hardware tier
(refusing a non-arm64 host unless JASPER_ALLOW_UNSUPPORTED_ARCH=1), requires
root, arms the exit trap, marks the install in progress, and persists the tier
in ${INSTALL_PROFILE_MARKER} (refusing a full/streambox change unless
JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=1).

Hardware tier (detected on this host): $(detect_hardware_tier)
Run for real: sudo JASPER_INSTALL_PROFILE=$1 JASPER_HOSTNAME=<hostname>.local bash deploy/install.sh

EOF
    while IFS='|' read -r name _ _ phrase; do
        printf '  %s: %s\n' "${name}" "${phrase}"
    done <<<"${rows}"
}

main() {
    local dry_run="${JASPER_INSTALL_DRY_RUN:-0}"
    local install_profile
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

    install_profile="$(resolve_install_profile)" || return $?

    if _is_truthy "${dry_run}"; then
        print_install_plan "${install_profile}"
        return 0
    fi
    if ! _is_falsey_or_empty "${dry_run}"; then
        echo "invalid JASPER_INSTALL_DRY_RUN value: ${dry_run}" >&2
        echo "use 1/true/yes/on or 0/false/no/off" >&2
        return 2
    fi

    echo "==> install.sh starting (profile: ${install_profile})"
    if install_profile_legacy_marker_migrating; then
        echo "event=install_profile.migrate previous=$(read_raw_persisted_install_profile) profile=${install_profile} source=marker"
    fi
    # Fixed prologue, not table rows: the tier report and the root check are
    # read-only and must precede every mutation, the trap can only be armed
    # once root is proven, and the gate the trap clears is set with it.
    hardware_tier_preflight
    require_root
    INSTALL_CURRENT_STEP="prologue"
    trap install_exit_cleanup EXIT
    mark_install_in_progress
    persist_install_profile "${install_profile}"

    # Rows on fd 3, so each step keeps the script's own stdin (apt, a build).
    # Assigned first: `set -e` does not check a substitution in a redirection.
    local rows row name fn phrase _
    rows="$(install_steps_for_profile "${install_profile}")"
    while IFS= read -r row <&3; do
        IFS='|' read -r name _ fn phrase <<<"${row}"
        INSTALL_CURRENT_STEP="${name}"
        jasper_install_log "event=install.step profile=${install_profile} step=${name} fn=${fn}"
        echo "==> ${name}: ${phrase}"
        "${fn}"
    done 3<<<"${rows}"
}

# Only run main when invoked directly. When sourced (e.g. by tests
# that want to call a single helper like `_compute_min_free_kbytes`),
# define the functions but don't execute main.
if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
    main "$@"
fi
