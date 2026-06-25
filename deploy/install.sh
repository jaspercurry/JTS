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
# WS1 Phase 4a — the group-`jasper-secrets` secret compartment, a SIBLING of
# STATE_DIR (not under it): STATE_DIR is jasper-voice/-mux's StateDirectory,
# whose recursive chown would force this tree's group back to `jasper`.
SECRETS_DIR="/var/lib/jasper-secrets"
INTSECRETS_DIR="/var/lib/jasper-intsecrets"
SYSTEMD_DIR="/etc/systemd/system"
INSTALL_PROFILE_DEFAULT="full"
INSTALL_PROFILE_MARKER="${STATE_DIR}/install_profile"

source "${REPO_DIR}/deploy/lib/jasper-asound-render.sh"
source "${REPO_DIR}/deploy/lib/install/env-migrations.sh"
source "${REPO_DIR}/deploy/lib/install/service-users.sh"
source "${REPO_DIR}/deploy/lib/install/memory-resilience.sh"
source "${REPO_DIR}/deploy/lib/install/build-sandbox.sh"
source "${REPO_DIR}/deploy/lib/install/renderers.sh"
source "${REPO_DIR}/deploy/lib/install/web-assets.sh"
source "${REPO_DIR}/deploy/lib/install/model-staging.sh"
source "${REPO_DIR}/deploy/lib/install/rust-daemons.sh"
source "${REPO_DIR}/deploy/lib/install/python-runtime.sh"
source "${REPO_DIR}/deploy/lib/install/systemd-units.sh"

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
# Upstream provenance (auto-generated archive, not fetched by install.sh):
# https://github.com/mikebrady/nqptp/archive/${NQPTP_COMMIT}.tar.gz
NQPTP_ARCHIVE_URL="https://github.com/jaspercurry/JTS/releases/download/build-deps-v1/nqptp-c925f27c1fd1.tar.gz"
NQPTP_SHA256="d2c2fe5d2574d447a817b1585e82c38f4c98774dac8284e5a3f17e188a3a75f9"
# Upstream provenance (auto-generated archive, not fetched by install.sh):
# https://github.com/mikebrady/shairport-sync/archive/${SHAIRPORT_SYNC_COMMIT}.tar.gz
SHAIRPORT_SYNC_ARCHIVE_URL="https://github.com/jaspercurry/JTS/releases/download/build-deps-v1/shairport-sync-0b1c4391ffd3.tar.gz"
SHAIRPORT_SYNC_SHA256="7ef3a6ba1cbd67bb200f018ddcd3e8dbe40da98b3c1776aee6c7b832632c6865"
WEBRTC_AEC3_VERSION="v2.1"
WEBRTC_AEC3_COMMIT="846fe90a289f58b7c9303a635142aa2c7caa93e5"
# Upstream provenance (auto-generated archive, not fetched by install.sh):
# https://gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing/-/archive/${WEBRTC_AEC3_COMMIT}/webrtc-audio-processing-${WEBRTC_AEC3_COMMIT}.tar.gz
WEBRTC_AEC3_ARCHIVE_URL="https://github.com/jaspercurry/JTS/releases/download/build-deps-v1/webrtc-audio-processing-846fe90a289f.tar.gz"
WEBRTC_AEC3_SHA256="ddf4e540b9f4291e140cc2ab4560f3eb4fce07ef6212a94d980843bfbf9a4588"

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
# _webrtc_compile_jobs' ~1.5 GB/job -j cap). Converging those knobs onto
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

    # The low boundary REUSES rust-daemons.sh's threshold (one source of
    # truth) so the label can't drift from the build knob it describes —
    # below it, the Rust low-memory build profile is already active.
    # install.sh always sources rust-daemons.sh, so the var is set; the
    # :- fallback only guards a partial source in a stray test context.
    # The 2 GB split is the one tier-owned constant: it separates the jts2
    # OOM band (where _webrtc_compile_jobs caps at -j1) from parallel-build
    # headroom.
    local low_kb="${RUST_LOW_MEMORY_BUILD_THRESHOLD_KB:-786432}"
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
    logger -t jasper-install -- "event=hardware_tier.detected ${tier_line}" 2>/dev/null || true

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
    install -d -m 0750 "$(dirname "${marker}")"
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
    local constraints="${REPO_DIR}/deploy/constraints-pi.txt"
    if [[ -f "${constraints}" ]]; then
        printf '%s\n' "${constraints}"
    fi
}

print_streambox_install_plan() {
    cat <<EOF
==> JTS streambox install plan (dry run)

No host changes are made in this mode. This is the Raspberry Pi Zero-class
local-renderer tier: AirPlay, Spotify Connect, Bluetooth, and USB Audio Input,
CamillaDSP sound/EQ/correction, and the same grouping reconciler as full
speakers — without voice, wake-word, mic/AEC, assistant providers, or
accessory firmware surfaces.

Run for real from a Pi-local checkout:
  sudo JASPER_INSTALL_PROFILE=streambox JASPER_HOSTNAME=<hostname>.local bash deploy/install.sh

1. Profile guard
   - Resolve JASPER_INSTALL_PROFILE=streambox.
   - Persist the install profile tier in ${INSTALL_PROFILE_MARKER}.
   - Refuse later full/streambox tier changes unless
     JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=1 is set deliberately.
   - A legacy persisted endpoint/satellite marker normalizes to
     streambox, so the box auto-migrates to the streambox install path.

Hardware tier (detected on this host): $(detect_hardware_tier)
  - Informational; orthogonal to the profile. The real install fails
    fast on a non-arm64 architecture unless JASPER_ALLOW_UNSUPPORTED_ARCH=1.
    See docs/install-hardware-tier-and-staleness.md.

2. System packages
   - apt-get update.
   - Streambox renderer/DSP stack runtime/build packages:
     python3 python3-venv python3-dev build-essential rustc cargo
     libasound2-dev libasound2 libasound2-plugins portaudio19-dev
     libsndfile1 curl ca-certificates rsync pkg-config nginx-light
     openssl snapclient snapserver.
   - Renderer/Bluetooth/AirPlay packages and build inputs:
     autoconf automake libtool libpopt-dev libconfig-dev
     libavahi-client-dev libssl-dev libsoxr-dev libplist-dev
     libsodium-dev libgcrypt20-dev uuid-dev libmbedtls-dev
     libglib2.0-dev libavutil-dev libavcodec-dev libavformat-dev
     libswresample-dev xxd bluez-alsa-utils avahi-daemon avahi-utils.

3. Downloaded or built inputs
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
   - Python runtime dependencies from pyproject.toml [streambox].
   - jasper-fanin Rust daemon from rust/jasper-fanin with
     cargo build --release --locked; Zero-class RAM uses the installer
     low-memory Cargo release overrides.
   - jasper-outputd daemon from rust/jasper-outputd with
     cargo build --release --locked; Zero-class RAM uses the installer
     low-memory Cargo release overrides.
   - The shairport-sync/nqptp source builds and both Rust daemon builds
     run RAM-bounded and cgroup-contained via
     deploy/lib/install/build-sandbox.sh, so an OOM kills only the build,
     never a live daemon. See docs/HANDOFF-build-sandbox.md.

4. Runtime files and state
   - Create/update /opt/jasper, /etc/jasper, /var/lib/jasper,
     /var/lib/jasper-intsecrets, /opt/camilladsp, /etc/camilladsp,
     /var/lib/camilladsp, /usr/share/jasper-web, and feature-specific
     state directories.
   - Write the /var/lib/jasper/build.txt verified-install marker
     (written LAST, only on full success) with deploy SHA/branch metadata.
   - Copy the jasper Python package, pyproject.toml, landing pages,
     docs, Avahi service templates, systemd units, renderer configs,
     udev rules, ALSA templates, and helper binaries.
   - Render /etc/asound.conf through /usr/local/sbin/jasper-render-asound-conf.
   - Move HA/Spotify integration secrets into
     /var/lib/jasper-intsecrets (streambox keeps only the Spotify side
     active, but shares the same migration/forward path).

5. Services and live actions
   - Enable/start jasper-control, jasper-camilla, jasper-fanin,
     jasper-outputd, jasper-audio-hardware-reconcile, jasper-mux,
     renderer services, nginx, Avahi, identity reconciliation, and the
     multi-room grouping reconciler.
   - Enable socket-activated streambox-safe web surfaces:
     /spotify/, /sources/, /sound/, /speaker/, /wifi/, /rooms/,
     /bluetooth/, /system/, and HTTPS /correction/.
   - Install the streambox nginx route set with the shared JTS landing
     page and capability-gated cards.
   - Reuse the existing grouping reconciler parking contract: when this
     box is bonded as a follower it stops local source renderers without
     disabling the household's /sources/ intent, and restores them after
     unpairing.
   - Seed WiFi guardian recovery, memory/cgroup tuning, journald
     persistence, Avahi identity, correction TLS, and jasper-doctor.

6. Explicitly out of scope for the streambox tier
   - Voice, wake-word, microphone/AEC, assistant provider SDKs, Google
     account tools, transit/weather voice tools, local TTS/cues, HID
     accessory bridge, dial/satellite firmware, wake corpus tooling, and
     CamillaGUI.

This dry run is a planning aid for contributors; it is not a substitute
for real Zero 2 W validation of first-run Rust build cost, memory pressure,
and simultaneous renderer/DSP behavior.
EOF
}

print_install_plan() {
    local profile="${1:-full}"
    if [[ "${profile}" == "streambox" ]]; then
        print_streambox_install_plan
        return 0
    fi
    cat <<EOF
==> JTS install plan (dry run)

No host changes are made in this mode. The plan is intentionally static:
it describes the installer surfaces and conditional checks, then exits
before the root check, apt, downloads, file writes, systemd, or restarts.
The real installer remains the source of truth for exact host-specific
no-op decisions.

Run for real from a Pi-local checkout:
  sudo JASPER_HOSTNAME=<hostname>.local bash deploy/install.sh

Profile guard:
  - Resolve JASPER_INSTALL_PROFILE=full on unknown/Pi-5-class hardware
    unless a persisted profile marker says otherwise. Fresh Raspberry Pi
    Zero 2 W installs resolve to streambox instead of full.
  - Persist the install profile tier in ${INSTALL_PROFILE_MARKER}.
  - Refuse later full/streambox tier changes unless
    JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=1 is set deliberately.

Hardware tier (detected on this host): $(detect_hardware_tier)
  - Informational; orthogonal to the profile. Build strategy keys off
    RAM (the Rust low-memory profile under 768 MB; the WebRTC AEC3 -j
    cap budgets ~1.5 GB/job). The real install fails fast on a non-arm64
    architecture unless JASPER_ALLOW_UNSUPPORTED_ARCH=1. See
    docs/install-hardware-tier-and-staleness.md.

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
   - All heavy source builds above (webrtc AEC3, jasper_aec3, the Rust
     daemons, shairport-sync, nqptp) run RAM-bounded and cgroup-contained
     via deploy/lib/install/build-sandbox.sh, so an OOM during an
     in-service update kills only the build, never a live daemon.
     See docs/HANDOFF-build-sandbox.md.

3. Runtime files and state
   - Create/update /opt/jasper, /etc/jasper, /var/lib/jasper,
     /opt/camilladsp, /etc/camilladsp, /var/lib/camilladsp,
     /usr/share/jasper-web, and feature-specific state directories.
   - Write the /var/lib/jasper/build.txt verified-install marker
     (written LAST, only on full success) with deploy SHA/branch metadata
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
   - Create the \`jasper\` group and the non-root service users
     (jasper-voice / jasper-mux / jasper-input / jasper-control /
     jasper-web) the Tier-A daemons drop to, plus the Phase 4
     secret-compartment groups.
   - Install /etc/polkit-1/rules.d/49-jasper-control.rules granting the
     non-root jasper-control its scoped systemctl (MANAGED_UNITS allowlist)
     + reboot/power-off — its restart broker + supervisors run as that uid
     (WS1 Phase 3b-2). Make /etc/avahi/services group-jasper writable so it
     can render the peering advert.
   - Install /etc/polkit-1/rules.d/49-jasper-web.rules granting the non-root
     jasper-web the NetworkManager actions (scan / connect / forget / radio /
     PSK re-read) the /wifi/ wizard drives (WS1 Phase 3b-3).
   - Widen /etc/bluetooth + /var/lib/camilladsp/configs to group-jasper 2775
     so the non-root jasper-web can atomically replace the BlueZ name and the
     generated sound profiles (WS1 Phase 3b-3).
   - Widen the config/state files jasper-control reads off disk
     (jasper.env + voice_provider/control_token + non-secret sound state)
     to 0640 group jasper so the jasper-doctor it spawns + /state can read
     them. Secret compartments stay isolated by WS1 Phase 4.
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
   - Seed the camilla#2 crossover Camilla statefile (the dormant
     endpoint-crossover instance, :1235) through the same active-speaker
     runtime contract. Its unit is installed but NOT enabled — a later
     reconciler arms it only on an active leader.
   - Run the AEC/mic reconciler so voice follows attached hardware.
   - Install the multi-room grouping units: snapserver + snapclient
     DISABLED (grouping is never auto-enabled; the snapcast apt
     packages are NOT installed on a solo speaker; the wizard opt-in
     owns turning grouping on), and the grouping RECONCILER enabled +
     run — a boot/install no-op when grouping is off, and what lets a
     BONDED speaker survive reboots and deploys.
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

install_streambox_deps() {
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        build-essential rustc cargo \
        libasound2-dev libasound2 portaudio19-dev \
        libasound2-plugins libsndfile1 \
        curl ca-certificates rsync pkg-config \
        nginx-light openssl \
        snapclient snapserver

    apt-get install -y --no-install-recommends \
        autoconf automake libtool pkg-config \
        libpopt-dev libconfig-dev libavahi-client-dev \
        libssl-dev libsoxr-dev libplist-dev libsodium-dev \
        libgcrypt20-dev uuid-dev libmbedtls-dev libglib2.0-dev \
        libavutil-dev libavcodec-dev libavformat-dev libswresample-dev \
        xxd \
        bluez-alsa-utils avahi-daemon avahi-utils
}

_webrtc_compile_jobs() {
    # Bound the WebRTC AEC3 C++ build's parallelism to available RAM.
    # Each -O3 webrtc-audio-processing translation unit (notably
    # audio_processing_impl.cc) can peak well over 1 GB in cc1plus;
    # `meson compile` defaults to nproc jobs, so on a 1 GB Pi the four
    # parallel compiles exhaust RAM+swap and the OOM killer takes out
    # cc1plus *and* cascading victims (nginx, jasper-voice were both
    # OOM-killed on jts2, 2026-06-21), aborting the deploy mid-install.
    #
    # Budget ~1.5 GB per job and clamp to [1, nproc]: a 1 GB Pi builds
    # at -j1 (slower but survives), an 8 GB Pi still gets full nproc.
    # $1=MemTotal kB, $2=nproc (both injectable for tests).
    #
    # Now a thin caller of the unified _ram_bounded_jobs policy in
    # build-sandbox.sh, at the shared C++ budget. Kept as a named function
    # so its regression tests and the call site below stay stable.
    # Containment of the compile itself is run_contained_build.
    _ram_bounded_jobs "${1:-0}" "${2:-1}" "${BUILD_SANDBOX_KB_PER_JOB_CPP}"
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

    local compile_jobs
    compile_jobs="$(_webrtc_compile_jobs \
        "$(awk '/^MemTotal:/ { print $2; exit }' /proc/meminfo 2>/dev/null)" \
        "$(nproc 2>/dev/null || echo 1)")"
    echo "    meson compile -C builddir/ (-j ${compile_jobs}; RAM-bounded + cgroup-contained to avoid OOM-killing live daemons on low-memory Pis)"
    (cd "${src_dir}" && run_contained_build "webrtc-aec3" -- \
        meson compile -C builddir -j "${compile_jobs}")

    if [[ ! -f "${static_archive}" ]]; then
        echo "  ERROR: meson compile finished but ${static_archive} is missing" >&2
        echo "  WEBRTC_AEC3_V2_PREFIX will not be set; setup.py will skip _aec3_v2" >&2
        return 1
    fi

    echo "${source_id}" > "${provenance_marker}"
    echo "  → static archive: ${static_archive} ($(du -h "${static_archive}" | cut -f1))"
    export JASPER_WEBRTC_V2_PREFIX="${cache_dir}"
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
import os
import socket
import sys
import time

path = "/run/jasper-outputd/control.sock"
env_path = os.environ.get("JASPER_OUTPUTD_ENV_FILE", "/var/lib/jasper/outputd.env")

def parse_env(path):
    out = {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out

env = parse_env(env_path)
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
        active_channels = (env.get("JASPER_OUTPUTD_ACTIVE_CHANNELS") or "").strip()
        expected_content = (
            "outputd_active_content_capture"
            if sink_mode == "dual_apple" or (sink_mode == "single_alsa" and active_channels)
            else "outputd_content_capture"
        )
        expected_dac = (
            "dual_apple_usb_c_dac_4ch"
            if sink_mode == "dual_apple"
            else "outputd_dac"
        )
        if data.get("dac", {}).get("pcm") != expected_dac:
            raise RuntimeError(
                f"dac.pcm={data.get('dac', {}).get('pcm')!r}, expected {expected_dac!r}"
            )
        if data.get("content", {}).get("pcm") != expected_content:
            raise RuntimeError(
                f"content.pcm={data.get('content', {}).get('pcm')!r}, "
                f"expected {expected_content!r} "
                f"(sink_mode={sink_mode!r}, active_channels={active_channels!r})"
            )
        sys.exit(0)
    except Exception as e:
        last_error = e
        time.sleep(0.1)
print(f"jasper-outputd STATUS probe failed: {last_error}", file=sys.stderr)
sys.exit(1)
PY
}

camilla_config_has_safe_volume_limit() {
    local config_path="$1"
    awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*volume_limit:/ {
            value = $0
            sub(/^[^:]*:[[:space:]]*/, "", value)
            sub(/[[:space:]]*#.*/, "", value)
            sub(/^[[:space:]]*/, "", value)
            sub(/[[:space:]]*$/, "", value)
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
    ensure_state_dir
    # Shared correction/test artifacts are written by the correction web flow and
    # by jasper-web's active-speaker commissioning tone path. Keep the tree
    # group-writable for the dropped service users instead of root-only.
    install -d -m 2770 -g jasper \
        /var/lib/jasper/correction \
        /var/lib/jasper/correction/sweeps \
        /var/lib/jasper/correction/captures \
        /var/lib/jasper/correction/sessions \
        /var/lib/jasper/correction/calibration_mics \
        /var/lib/jasper/correction/tones

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

    # The outputd topology uses a separate Camilla statefile instead of
    # overwriting /var/lib/camilladsp/statefile.yml. Do not repair that
    # statefile here: the safe target depends on the saved output topology.
    # This flat graph maps full-range stereo directly to DAC outputs. It is
    # illegal when saved output topology assigns any physical output to
    # tweeter/protected role. After the Python package is installed,
    # ensure_outputd_camilla_statefile asks jasper.active_speaker's runtime
    # contract which graph is legal and fails closed if no protected graph
    # exists.

    # NOTE: aec-bridge is no longer a CamillaDSP instance. It is now a
    # Python bridge (`jasper-aec-bridge`, see jasper/cli/aec_bridge.py)
    # that either runs WebRTC AEC3 for the software fallback profile or,
    # in chip-AEC profiles, carries the selected XVF hardware-AEC beam to
    # jasper-voice while WebRTC AEC3 is bypassed. Old aec-bridge.yml is
    # removed if present from a prior install.
    rm -f "${CAMILLA_CONF}/aec-bridge.yml"
}

ensure_outputd_camilla_statefile() {
    # Runtime graph selection belongs to jasper.active_speaker, not install.sh.
    # This flat graph maps full-range stereo directly to DAC outputs. It is
    # illegal when saved output topology assigns any physical output to
    # tweeter/protected role.
    local output
    echo "  Checking outputd Camilla statefile against active-speaker runtime contract"
    if ! output="$(/opt/jasper/.venv/bin/jasper-active-speaker runtime-safe-graph \
        --statefile /var/lib/camilladsp/outputd-statefile.yml \
        --flat-config "${CAMILLA_CONF}/outputd-cutover.yml" \
        --write-statefile 2>&1)"; then
        printf '%s\n' "${output}"
        return 1
    fi
    printf '%s\n' "${output}"
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
    # the flat fallback (the contract's `select_flat` branch is gated on
    # `not requires_roleful_graph`; see
    # jasper/active_speaker/runtime_contract.py). So an active box gets a
    # tweeter-safe driver-domain seed.
    #
    # SEAM FLAGGED FOR THE RECONCILER PR: on an ORDINARY (non-active) box
    # the contract returns flat, so this would seed flat into a file named
    # crossover-statefile.yml. That is BENIGN today because camilla#2 is
    # INERT there (the unit is never enabled), so the flat seed is never
    # loaded. NOTE: the crossover guard does NOT convert a flat statefile —
    # it acts only on a dead bonded pipe — so the driver-domain guarantee
    # for an ARMED camilla#2 rests on the reconciler seeding it at arm time,
    # not on the guard. The later
    # reconciler PR — which knows when the box is actually an active
    # leader — should refine this to seed the EXACT driver-domain baseline
    # (not whatever runtime-safe-graph returns for a non-roleful topology)
    # at the moment it arms the unit. We do NOT author that here: emitting
    # a precise driver-domain baseline is jasper/active_speaker/* code,
    # outside this unit's scope fence.
    #
    # We never restart the unit (it is not enabled), so there is no
    # JASPER_RESTART_* knob here — only the seed write.
    local output
    echo "  Seeding camilla#2 crossover statefile via active-speaker runtime contract"
    if ! output="$(/opt/jasper/.venv/bin/jasper-active-speaker runtime-safe-graph \
        --statefile /var/lib/camilladsp/crossover-statefile.yml \
        --flat-config "${CAMILLA_CONF}/outputd-cutover.yml" \
        --write-statefile 2>&1)"; then
        printf '%s\n' "${output}"
        return 1
    fi
    printf '%s\n' "${output}"
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
    export DONGLE_CARD APPLE_DONGLE_PRESENT APPLE_DONGLE_SERVICE_CARD
    export OUTPUT_DAC_CARD OUTPUT_DAC_ID OUTPUT_DAC_RECOGNIZED
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
    install -d -m 0755 "${ENV_DIR}"
    ensure_state_dir
    install -d -m 0755 /usr/local/lib/jasper
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-asound-render.sh" \
        /usr/local/lib/jasper/jasper-asound-render.sh
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
    jasper_asound_render_template \
        "${ENV_DIR}/asoundrc.jasper.source" \
        "${ENV_DIR}/asoundrc.jasper.template"
    chmod 0644 "${ENV_DIR}/asoundrc.jasper.template"
    /usr/local/sbin/jasper-render-asound-conf
    ln -sfn /var/lib/jasper-asound/asound.conf /etc/asound.conf
    chmod 0644 /var/lib/jasper-asound/asound.conf
    echo "  Wrote /etc/asound.conf with fan-in, outputd lanes, and jasper_out rollback path"
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
    # card never advertise a SHA the box is not cleanly running. This
    # closes problem #4 in docs/install-update-resilience-plan.md, where
    # the manifest was written EARLY and lied after the build failed.
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

# Generic "delete-and-append" rewrite of one KEY=value line in
# /etc/jasper/jasper.env. Shared by the streambox env-refresh path.
set_jasper_env_value() {
    local key="$1"
    local value="$2"
    sed -i.bak "/^${key}=/d" "${ENV_DIR}/jasper.env"
    rm -f "${ENV_DIR}/jasper.env.bak"
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_DIR}/jasper.env"
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
    # Five keys live in aec_mode.env, all owned by the /wake/
    # input-profile / wake-detection cards:
    #   - JASPER_AUDIO_INPUT_PROFILE  canonical profile selection
    #                                 (auto, xvf_chip_aec,
    #                                 xvf_chip_aec_testing,
    #                                 xvf_software_aec3, direct_mic,
    #                                 custom)
    #   - JASPER_AEC_MODE             master AEC bridge toggle
    #   - JASPER_WAKE_LEG_RAW         additive raw chip-direct leg (~5 MB)
    #   - JASPER_WAKE_LEG_DTLN        additive DTLN neural leg (~75 MB)
    #   - JASPER_WAKE_LEG_CHIP_AEC    XVF3800 chip-AEC beam legs (opt-in,
    #                                 hardware-conditional, mutually
    #                                 exclusive with raw/DTLN)
    #   - JASPER_AEC_CHIP_REF_OBSERVE opt-in: on the software-AEC3 path,
    #                                 arm outputd's chip-ref writer FOR
    #                                 MEASUREMENT ONLY so the Layer-0 SRO
    #                                 drift estimator gets fed (mic path
    #                                 stays software AEC3). Default off.
    # Defaults: profile auto. On approved XVF3800 + output-DAC hardware that
    # resolves to chip-AEC (no stacked software AEC/raw/DTLN). When chip-AEC
    # is unavailable it falls back to the software-AEC3 profile (AEC on, raw
    # fallback on, DTLN off). Unapproved DACs use the explicit
    # xvf_chip_aec_testing profile; auto never selects testing. DTLN remains
    # an explicit custom/lab leg because it is heavy on a 1 GB Pi.
    #
    # On upgrade, the reconciler's ensure_mode_file appends any
    # missing keys with these same defaults — preserving an
    # operator's hand-set JASPER_AEC_MODE/leg fields while inferring a
    # profile for pre-profile installs. Migration from hand-set underlying
    # env vars in /etc/jasper/jasper.env runs separately in
    # migrate_wake_legs_config.
    if [[ ! -f "${STATE_DIR}/aec_mode.env" ]]; then
        printf 'JASPER_AUDIO_INPUT_PROFILE=auto\nJASPER_AEC_MODE=auto\nJASPER_WAKE_LEG_RAW=1\nJASPER_WAKE_LEG_DTLN=0\nJASPER_WAKE_LEG_CHIP_AEC=0\nJASPER_AEC_CHIP_REF_OBSERVE=0\n' \
            > "${STATE_DIR}/aec_mode.env"
        chmod 0644 "${STATE_DIR}/aec_mode.env"
    fi
    systemctl enable jasper-aec-reconcile.service
    /usr/local/sbin/jasper-aec-reconcile --reason install || \
        echo "  WARN: AEC/mic reconcile failed. Check logs with: journalctl -u jasper-aec-reconcile -e"
}

reconcile_grouping_state() {
    # Grouping reconciler runs at BOOT (and on every install) so a BONDED
    # speaker survives reboots/deploys: it re-derives the snapcast args +
    # the outputd round-trip lane env, drives the CamillaDSP bonded/solo
    # config, pins the snapcast stream bindings, and (re)starts the snap
    # units per the wizard intent. On a solo speaker it is a no-op
    # oneshot (grouping off => stop both units, clear derived env) —
    # cost-free. NOTE: this enables the RECONCILER, not grouping:
    # snapserver/snapclient still ship disabled and only the reconciler
    # starts them on explicit wizard opt-in. (Boot gap found in the
    # 2026-06-11 jts3 incident: a bonded follower rebooted and its
    # snapclient stayed down because nothing ran the reconciler at boot.)
    systemctl enable jasper-grouping-reconcile.service
    systemctl restart jasper-grouping-reconcile.service || \
        echo "  WARN: grouping reconcile failed. Check logs with: journalctl -u jasper-grouping-reconcile -e"
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

install_management_static_assets() {
    local index_src="$1"
    local include_correction_preflight="${2:-0}"
    local app_css_ver

    # Static landing page served at /. Plain HTML, no daemon — nginx
    # reads it directly via the `location = /` block in jasper.conf.
    # Updates require an `nginx -s reload` (handled by the caller)
    # but no service restart.
    install -d -m 0755 /usr/share/jasper-web
    install -m 0644 "${index_src}" /usr/share/jasper-web/index.html
    # Stamp the app.css cache-bust version (mirrors the wizards' build-SHA
    # query string) so a deploy busts the year-immutable /assets cache.
    # The landing page is static HTML, so we substitute at install time.
    # Resolve the SHA directly (deploy env → git → prior manifest) rather
    # than reading build.txt: the manifest is now written LAST, as the
    # verified-install marker, so it still holds the PRIOR SHA at this
    # point in the run. resolve_build_sha_short returns the same value the
    # manifest will record, so the cache key matches the installed build.
    app_css_ver="$(resolve_build_sha_short)"
    [[ -n "${app_css_ver}" && "${app_css_ver}" != "unknown" ]] || app_css_ver="dev"
    sed -i "s/__APP_CSS_VERSION__/${app_css_ver}/g" /usr/share/jasper-web/index.html
    # Bake the install profile's capability map into the landing page so its
    # capability-gated sections render correctly at FIRST PAINT — no
    # /system/data.json round-trip to lay out the page, and it stays correct
    # even if a backend daemon is down. Same map jasper-control serves at
    # runtime (system_capabilities_for_profile), so baked and live agree by
    # construction. The profile marker was persisted earlier in this run.
    # Python (not sed) so JSON quotes don't fight the shell; fail loud rather
    # than ship a page with an unreplaced placeholder. Also bakes the WS1
    # control token into <meta name="jts-control-token"> so the landing page's
    # mic-mute button can ride it on POST /mic/mute (the token-gated route);
    # ensure_token() generates-if-absent at 0640 group jasper, the same value
    # the wizards deliver. The token stays inside Python (never a shell arg / process
    # table); the base64url alphabet is HTML-safe, but escape defensively.
    if ! PYTHONPATH="${REPO_DIR}" python3 - /usr/share/jasper-web/index.html <<'PYBAKE'
import json
import sys
from html import escape as html_escape

from jasper.control import control_token
from jasper.install_profile import (
    read_install_profile,
    system_capabilities_for_profile,
)

path = sys.argv[1]
caps = json.dumps(system_capabilities_for_profile(read_install_profile()))
token = html_escape(control_token.ensure_token())
html = open(path, encoding="utf-8").read()
if "__JTS_CAPS_JSON__" not in html:
    sys.exit("landing page is missing the __JTS_CAPS_JSON__ placeholder")
if "__JTS_CONTROL_TOKEN__" not in html:
    sys.exit("landing page is missing the __JTS_CONTROL_TOKEN__ placeholder")
html = html.replace("__JTS_CAPS_JSON__", caps)
html = html.replace("__JTS_CONTROL_TOKEN__", token)
with open(path, "w", encoding="utf-8") as f:
    f.write(html)
PYBAKE
    then
        echo "  ERROR: failed to bake landing-page capabilities/token; refusing to ship a broken page" >&2
        return 1
    fi
    echo "  landing page: baked install-profile capabilities + control token for first-paint layout"
    # All /assets/ content (app.css, fonts, per-page CSS + ES modules) +
    # the .install-manifest the doctor verifies — see
    # deploy/lib/install/web-assets.sh for the copy shape and the
    # manifest contract.
    install_web_assets

    if [[ "${include_correction_preflight}" == "1" ]]; then
        # Plain-HTTP preflight before the HTTPS-only room-correction UI.
        # This gives the user context before the browser's self-signed-cert
        # interstitial while keeping the entry point on the normal HTTP
        # surface.
        install -m 0644 \
            "${REPO_DIR}/deploy/correction-preflight.html" \
            /usr/share/jasper-web/correction-preflight.html
        # Stamp the same app.css cache-bust version as the landing page —
        # the preflight is static HTML and links /assets/app.css directly,
        # so it needs the build-SHA query string to bust the immutable
        # /assets cache.
        sed -i "s/__APP_CSS_VERSION__/${app_css_ver}/g" \
            /usr/share/jasper-web/correction-preflight.html
    else
        rm -f /usr/share/jasper-web/correction-preflight.html
    fi

    # Prune the retired /integrations page from prior installs. Its nginx
    # route and install copy are gone (the landing page's inline Integrations
    # section replaced it); remove the orphaned file so a previously-deployed
    # Pi does not keep an unreachable page on disk.
    rm -f /usr/share/jasper-web/integrations.html
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

    install_management_static_assets "${REPO_DIR}/deploy/index.html" 1

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

install_streambox_nginx_site() {
    # Streambox uses the normal JTS landing page with capability-gated cards,
    # plus an nginx route set limited to local sources, DSP, grouping, and
    # system health. That keeps the frontend shared while omitting voice/wake
    # surfaces whose daemons are intentionally absent from this profile.
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-jasper-streambox.conf" \
        /etc/nginx/sites-enabled/jasper.conf

    install_management_static_assets "${REPO_DIR}/deploy/index.html" 1
    rm -f /etc/nginx/sites-enabled/default

    if nginx -t 2>/dev/null; then
        systemctl enable --now nginx 2>/dev/null || true
        systemctl reload nginx
        echo "  streambox nginx reloaded — http://<host>/{,spotify,sources,sound,system} + https://<host>/{correction,balance,sync} are live"
    else
        echo "  ERROR: streambox nginx config test failed; not reloading. Run 'nginx -t' to debug." >&2
        return 1
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

    # WS1 Phase 3b-2: a non-root jasper-control renders the peering advert
    # (jasper-peer.service) into this dir when /rooms/ peering is enabled
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

install_jasper_control_polkit() {
    # WS1 Phase 3b-2 — the polkit grant for the non-root jasper-control user.
    # Without it, every systemctl/reboot/poweroff jasper-control runs (the
    # in-process restart broker + the system/shairport/grouping supervisors +
    # the /system buttons) is DENIED with "Interactive authentication required"
    # — silently breaking the Tier-3/Tier-5 recovery paths. polkitd monitors
    # /etc/polkit-1/rules.d and auto-reloads on change, so no reload/restart is
    # needed (a daemon-reload is for systemd units, not polkit). See
    # deploy/polkit/49-jasper-control.rules + docs/HANDOFF-privilege-separation.md.
    install -d -m 0755 /etc/polkit-1/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/polkit/49-jasper-control.rules" \
        /etc/polkit-1/rules.d/49-jasper-control.rules
    echo "  Installed polkit rule for jasper-control (manage-units allowlist + reboot/power-off)"
}

install_jasper_web_polkit() {
    # WS1 Phase 3b-3 — the polkit grant for the non-root jasper-web user. The
    # /wifi/ wizard drives NetworkManager (scan / connect / forget / radio /
    # PSK re-read); NM's implicit defaults DENY a sessionless daemon for every
    # one of those, so without this rule a non-root jasper-web cannot manage
    # Wi-Fi — the worst-case brick for a headless, often Ethernet-less speaker.
    # polkitd monitors /etc/polkit-1/rules.d and auto-reloads (no restart). See
    # deploy/polkit/49-jasper-web.rules + docs/HANDOFF-privilege-separation.md.
    install -d -m 0755 /etc/polkit-1/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/polkit/49-jasper-web.rules" \
        /etc/polkit-1/rules.d/49-jasper-web.rules
    echo "  Installed polkit rule for jasper-web (NetworkManager wifi management)"
}

widen_jasper_web_writable_dirs() {
    # WS1 Phase 3b-3 — the non-root jasper-web user atomically replaces files in
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
        echo "  Widened /etc/bluetooth + /var/lib/camilladsp/configs to root:jasper 2775 (jasper-web writes)"
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
    # is set in /var/lib/jasper/peering.env (via the /rooms/ Speakers
    # page). When peering is off (the default), no
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
    ensure_state_dir
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
    echo "  Peering template installed; peering is OFF by default — enable at http://${JASPER_HOSTNAME:-jts.local}/rooms/"
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
        echo "event=install_profile.migrate previous=$(read_raw_persisted_install_profile) profile=streambox source=marker"
    fi
    hardware_tier_preflight  # log tier; fail fast on unsupported arch (before any mutation)
    if [[ "${install_profile}" == "streambox" ]]; then
        require_root
        persist_install_profile "${install_profile}"
        require_build_user  # Rust builds run as 'pi'; fail fast pre-mutation
        create_jasper_service_users  # WS1 Phase 3b: before unit install + state-dir creation
        install_streambox_deps
        install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
        install_camilladsp
        install_renderers
        set_usb_gadget_mode
        tune_wifi_for_airplay
        install_streambox_jasper
        ensure_outputd_camilla_statefile
        ensure_crossover_camilla_statefile  # camilla#2 seed (INERT; unit not enabled)
        migrate_secrets_phase4b  # WS1 Phase 4b: streambox Spotify creds/cache path
        build_install_jasper_fanin
        build_install_jasper_outputd
        install_streambox_systemd_units
        retire_audio_topology_switch
        migrate_wifi_guardian
        migrate_memory_resilience
        migrate_cgroup_memory_enabled
        install_journald_persistent_storage
        install_avahi_jasper_control
        install_jasper_control_polkit  # WS1 3b-2: grant non-root jasper-control its scoped systemctl/reboot
        install_jasper_web_polkit  # WS1 3b-3: grant jasper-web NetworkManager wifi management
        widen_jasper_web_writable_dirs  # WS1 3b-3: /etc/bluetooth + camilladsp/configs group-jasper writable
        install_peering_template
        remove_legacy_https_artifacts
        provision_correction_tls
        install_streambox_nginx_site
        widen_control_secret_env_modes  # WS1 3b-2: secret env group-jasper readable for the spawned doctor
        # Final mutation: stamp the verified-install manifest only now that
        # every step above succeeded (set -e). run_doctor_summary below is
        # non-mutating diagnostics — keep write_build_manifest the LAST
        # state change so a failure anywhere above leaves the prior good
        # manifest. See write_build_manifest + problem #4 in the plan.
        write_build_manifest
        run_doctor_summary
        return 0
    fi
    require_root
    persist_install_profile "${install_profile}"
    require_build_user  # Rust builds run as 'pi'; fail fast pre-mutation
    create_jasper_service_users  # WS1 Phase 3b: before unit install + state-dir creation
    install_deps
    install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
    install_camilladsp
    install_renderers
    set_usb_gadget_mode
    tune_wifi_for_airplay
    install_jasper
    ensure_outputd_camilla_statefile
    ensure_crossover_camilla_statefile  # camilla#2 seed (INERT; unit not enabled)
    build_install_jasper_fanin    # Rust daemon binary; enabled by install_systemd_units
    build_install_jasper_outputd  # Rust mainline final-output owner
    install_systemd_units
    retire_audio_topology_switch # Remove stale dmix/fanin state; fanin is canonical
    migrate_memory_resilience   # Stage 1 OOM protection: sysctl + MGLRU + zram
    migrate_cgroup_memory_enabled  # Stage 2 audio-slice: cgroup memory + PSI in cmdline.txt
    install_journald_persistent_storage
    install_avahi_jasper_control
    install_jasper_control_polkit  # WS1 3b-2: grant non-root jasper-control its scoped systemctl/reboot
    install_jasper_web_polkit  # WS1 3b-3: grant jasper-web NetworkManager wifi management
    widen_jasper_web_writable_dirs  # WS1 3b-3: /etc/bluetooth + camilladsp/configs group-jasper writable
    install_peering_template
    remove_legacy_https_artifacts
    provision_correction_tls   # cert files must exist before nginx -t
    install_nginx_site
    install_camillagui
    regenerate_audio_cues
    widen_control_secret_env_modes  # WS1 3b-2: secret env group-jasper readable for the spawned doctor
    # Final mutation: stamp the verified-install manifest only now that
    # every step above succeeded (set -e). run_doctor_summary below is
    # non-mutating diagnostics — keep write_build_manifest the LAST state
    # change so a failure anywhere above leaves the prior good manifest.
    # See write_build_manifest + problem #4 in the plan.
    write_build_manifest
    run_doctor_summary
}

# Only run main when invoked directly. When sourced (e.g. by tests
# that want to call a single helper like `_compute_min_free_kbytes`),
# define the functions but don't execute main.
if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
    main "$@"
fi
