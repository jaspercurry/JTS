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
# and skips voice/wake-word/GEMINI-dependent features — see
# print_streambox_install_plan() below. The pre-reqs listed here are full-tier
# only.
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
    local low_kb="${RUST_LOW_MEMORY_BUILD_THRESHOLD_KB:-1200000}"
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

print_streambox_install_plan() {
    cat <<EOF
==> JTS streambox install plan (dry run)

No host changes are made in this mode. This is the Raspberry Pi Zero-class
local-renderer tier: AirPlay, Spotify Connect, Bluetooth, and USB Audio Input,
CamillaDSP sound/EQ/correction, and the same grouping reconciler as full
speakers, plus the assistant on a paired mic-bearing remote — without
wake-word, local microphone, or AEC.

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
    Low-RAM hosts may enable temporary high-priority build swap for the
    heavy source/Rust build window, removed automatically on exit.
    See docs/install-hardware-tier-and-staleness.md.

2. System packages
   - apt-get update.
   - Streambox renderer/DSP stack runtime/build packages:
     python3 python3-venv python3-dev build-essential rustc cargo
     libasound2-dev libasound2 libasound2-plugins portaudio19-dev
     libsndfile1 curl ca-certificates rsync nginx-light
     openssl dnsmasq-base snapclient snapserver.
   - Renderer/Bluetooth/AirPlay packages and build inputs:
     autoconf automake libtool pkg-config libpopt-dev libconfig-dev
     libavahi-client-dev libssl-dev libsoxr-dev libplist-dev
     libsodium-dev libgcrypt20-dev uuid-dev libmbedtls-dev
     libglib2.0-dev libavutil-dev libavcodec-dev libavformat-dev
     libswresample-dev xxd libplist-utils bluez-alsa-utils rfkill
     avahi-daemon avahi-utils.

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
   - jts_ring ALSA ioplug from c/jts-ring-ioplug with make plugin
     (needs libasound2-dev), installed to the arch ALSA plugin dir,
     sha256-compared like the Rust daemons. Installing it opens nothing by
     itself, but the ring is this box's only transport (ADR-0100) and
     carries all of its audio. A build failure never fails the install. On
     a first-ever build failure the .so is absent and the doctor 'ring
     platform' check fails; on a REBUILD failure a prior good .so stays
     installed and the deploy REVOKES the installer's provenance record, so
     the doctor's 'ring ioplug provenance' check reports an unvouched
     plugin — an informational ok, or fail on a box whose wire needs a
     conf.d field only a vouched plugin is known to parse.
   - The shairport-sync/nqptp source builds and Rust daemon builds
     run RAM-bounded and cgroup-contained via
     deploy/lib/install/build-sandbox.sh, so an OOM kills only the build,
     never a live daemon.
   - On low-RAM hosts, park audio/runtime daemons before Rust builds so
     the build has room without inducing service restart storms.

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
   - Install the jts_ring device definitions (the /etc/alsa/conf.d
     drop-ins for the coupling rings, the renderer-ingress lanes and the
     grouping ingress) and the /dev/shm/jts-ring directory lifecycle
     (/etc/tmpfiles.d/jts-ring.conf). Placing them opens nothing.
   - Write output hardware state before Camilla statefile seed.
   - Render outputd flat startup config with active DAC latency floor.
   - Create, then re-assert ownership and modes on, the
     /var/lib/jasper-secrets compartment holding the assistant provider
     API keys jasper-voice reads, relocating any operator-seeded key out
     of the broad /etc/jasper/jasper.env.
   - Re-assert ownership and modes on the /var/lib/jasper-intsecrets
     integration-secret compartment holding the HA token and Spotify
     credentials/caches (streambox keeps only the Spotify side active,
     but shares the same compartment and forward path).

5. Services and live actions
   - Enable/start jasper-control, jasper-camilla, jasper-fanin,
     jasper-outputd, jasper-audio-hardware-reconcile, jasper-mux,
     renderer services, nginx, Avahi, identity reconciliation, and the
     multi-room grouping reconciler.
   - Install jasper-usbsink.service as a process-free readiness marker. Fan-in
     owns the USB data plane and is covered by the core-graph restart above.
   - Enable the hardware-gated composite USB gadget
     (jasper-usbgadget.service): where the resolved USB role permits, its USB
     management network (a CPU-serial-derived usb0 /30, no forwarding) makes
     http://<hostname>/ work over USB even with Wi-Fi off, alongside the
     wizard-toggled USB audio function. Install expresses no composition
     intent: it converges the descriptor against the gadget's own truth table
     and rebinds only on a real difference; the source-intent coordinator owns
     canonical On in direct-lane-before-advertising order. NM keyfile owns usb0 and the
     device-activated jasper-usbnet-dhcp.service (dnsmasq-base) serves DHCP.
     Kill switch: JASPER_USB_NETWORK=disabled.
   - Enable socket-activated streambox-safe web surfaces:
     /spotify/, /sources/, /airplay/, /sound/, /speaker/, /wifi/, /rooms/,
     /bluetooth/, /system/, and HTTPS /correction/. The assistant surfaces
     -- /voice/, /google/, /transit/, /weather/, /ha/, /tools/, /chat/ --
     are routed and socket-bound here too; jasper-web serves them only
     once the tier holds Capability.ASSISTANT.
   - Install the streambox nginx route set with the shared JTS landing
     page and capability-gated cards.
   - Preserve household /sources/ intent across pairing: grouping lands the
     role, then synchronously hands off to the canonical source coordinator,
     which parks local renderers on a follower and restores allowed sources
     after unpairing.
   - Stage jasper-voice.service without boot-enabling it:
     jasper-accessory-reconcile starts and stops jasper-voice as a
     mic-bearing remote pairs and unpairs, so the assistant is resident
     only while such a remote is present. Push-to-talk on that remote's
     mic — never a wake word, never the local mic.
   - Seed WiFi guardian recovery, memory/cgroup tuning, journald
     persistence, Avahi identity, correction TLS, and jasper-doctor.

6. Explicitly out of scope for the streambox tier
   - Wake-word detection and its ONNX runtime/models, wake corpus
     tooling, the local microphone array and AEC (including the XVF3800
     host), local TTS/cue regeneration, and CamillaGUI.

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
    RAM (the Rust low-memory profile under ~1.2 GB). The optional enhanced
    AEC job later uses the shared C++ budget of ~1.5 GB/job. The real install fails fast on a non-arm64
    architecture unless JASPER_ALLOW_UNSUPPORTED_ARCH=1. Low-RAM hosts
    may enable temporary high-priority build swap for the heavy source/Rust
    build window, removed automatically on exit.
    See docs/install-hardware-tier-and-staleness.md.

1. System packages
   - apt-get update.
   - Core runtime/build packages:
     python3 python3-venv python3-dev build-essential libasound2-dev
     libasound2 portaudio19-dev libasound2-plugins libsndfile1 curl
     ca-certificates rsync dfu-util libwebrtc-audio-processing-dev
     meson ninja-build nginx-light openssl dnsmasq-base
     rustc cargo.
   - Renderer and Bluetooth/AirPlay build packages:
     autoconf automake libtool pkg-config libpopt-dev libconfig-dev
     libavahi-client-dev libssl-dev libsoxr-dev libplist-dev
     libsodium-dev libgcrypt20-dev uuid-dev libmbedtls-dev
     libglib2.0-dev libavutil-dev libavcodec-dev libavformat-dev
     libswresample-dev xxd libplist-utils bluez-alsa-utils rfkill
     avahi-daemon avahi-utils.

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
   - Optional after setup — WebRTC AEC3 v2 source archive:
     ${WEBRTC_AEC3_ARCHIVE_URL}
     ref=${WEBRTC_AEC3_VERSION}, commit=${WEBRTC_AEC3_COMMIT}
     sha256=${WEBRTC_AEC3_SHA256}
   - CamillaGUI 4.1.0 bundle selected by uname -m, sha256-checked.
   - openWakeWord ONNX assets, curated wake models, and DTLN AEC models
     from the Python registries, sha256-checked before staging.
   - Python runtime dependencies from pyproject.toml; openwakeword is
     preinstalled without tflite-runtime because Pi OS Trixie ships
     Python 3.13. When deploy/constraints-pi.pins exists (generated by
     scripts/generate-pi-constraints.sh), the unpinned pip installs
     pass it via -c to replay the reviewed on-Pi resolve.
   - jasper-fanin Rust daemon from rust/jasper-fanin with
     cargo build --release --locked.
   - jasper-outputd daemon from rust/jasper-outputd with
     cargo build --release --locked; enabled as the mainline final-output
     owner.
   - jts_ring ALSA ioplug from c/jts-ring-ioplug with make plugin
     (needs libasound2-dev), installed to the arch ALSA plugin dir,
     sha256-compared like the Rust daemons. Installing it opens nothing by
     itself, but the ring is this box's only transport (ADR-0100) and
     carries all of its audio. A build failure never fails the install: a
     first-ever failure leaves the .so absent (doctor 'ring platform' check
     fails); a REBUILD failure leaves the prior .so installed and REVOKES
     the installer's provenance record, so the doctor's 'ring ioplug
     provenance' check reports an unvouched plugin — warn, or fail on a box
     whose wire needs a conf.d field only a vouched plugin is known to
     parse.
   - All heavy source builds above (jasper_aec3 v1, the Rust daemons,
     shairport-sync, nqptp) run RAM-bounded and cgroup-contained
     via deploy/lib/install/build-sandbox.sh, so an OOM during an
     in-service update kills only the build, never a live daemon.
     The optional v2 job reuses that same installed containment helper.
   - On low-RAM hosts, park audio/runtime daemons before Rust builds so
     the build has room without inducing service restart storms.

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
   - Copy Python source, jasper_aec3, pyproject.toml, the tuning operator
     docs, landing pages, nginx config, Avahi service templates, systemd
     units, udev rules, ALSA templates, and helper binaries.
   - Render /etc/asound.conf through /usr/local/sbin/jasper-render-asound-conf.
   - Install the jts_ring device definitions (the /etc/alsa/conf.d
     drop-ins for the coupling rings, the renderer-ingress lanes and the
     grouping ingress — each names itself in the transcript as it is
     placed) and the /dev/shm/jts-ring directory lifecycle
     (/etc/tmpfiles.d/jts-ring.conf, applied immediately). Placing them
     opens nothing.
   - Write output hardware state before Camilla statefile seed.
   - Render outputd flat startup config with active DAC latency floor.

4. Config and migrations
   - Seed /etc/jasper/jasper.env on fresh installs.
   - Sweep an operator-seeded LLM API key or Google Routes key out of
     /etc/jasper/jasper.env into the jasper-secrets compartment.
   - Seed defaults for speaker name, AirPlay mode, ALSA quality,
     wake model, AEC mode, peer_id, journald persistence, memory
     resilience, WiFi guardian recovery, and correction TLS CA/cert files.
   - Remove the retired dmix/fanin topology switch state file, which
     jasper-doctor warns about on presence.
   - Reconcile the USB data role from board topology and the registered
     output-DAC overlay; trim the boot config for headless operation
     (gpu_mem, vc4-kms-v3d CMA, HDMI audio); add other Pi boot/config
     changes when needed: memory cgroup/PSI kernel args, MGLRU tmpfiles,
     sysctl values, and rpi-swap zram sizing.
   - Disable WiFi power-save on the active wlan0 connection (nmcli)
     so AirPlay's unicast UDP stream avoids radio-sleep stalls.
   - Repair stored measurement-mic calibrations fetched under the wrong
     sign convention (vendor files state the mic's response; the
     correction is its negation). Keyed on each record's own stored
     convention, so it is idempotent and never touches a household's
     uploaded file or an already-correct record.

5. Services and live actions
   - Create the \`jasper\` group and the non-root service users
     (jasper-voice / jasper-mux / jasper-input / jasper-usbmic /
     jasper-control / jasper-web) the Tier-A daemons drop to, plus the
     secret-compartment groups.
   - Install /etc/polkit-1/rules.d/49-jasper-control.rules granting the
     non-root jasper-control its scoped systemctl (MANAGED_UNITS allowlist)
     + reboot/power-off — its restart broker + supervisors run as that uid.
     Make /etc/avahi/services group-jasper writable so it
     can render the peering advert.
   - Install /etc/polkit-1/rules.d/49-jasper-web.rules granting the non-root
     jasper-web the NetworkManager actions (scan / connect / forget / radio /
     PSK re-read) the /wifi/ wizard drives.
   - Widen /etc/bluetooth + /var/lib/camilladsp/configs to group-jasper 2775
     so the non-root jasper-web can atomically replace the BlueZ name and the
     generated sound profiles.
   - Widen the config/state files jasper-control reads off disk
     (jasper.env + voice_provider/control_token + non-secret sound state)
     to 0640 group jasper so the jasper-doctor it spawns + /state can read
     them. The secret compartments (jasper-secrets/jasper-intsecrets) stay
     isolated separately.
   - Reload udev and systemd.
   - Enable socket-activated setup wizards and always-on audio/control
     services.
   - Enable/start or restart renderer services, jasper-fanin,
     jasper-outputd, audio-hardware reconciliation, DAC init,
     headphone monitor, nginx, Avahi, CamillaGUI socket, the WiFi
     guardian, and the boot-loop guard.
   - Reconcile the USB Audio Input readiness marker from canonical source
     intent after fan-in and the composite gadget are installed.
   - Enable the hardware-gated composite USB gadget
     (jasper-usbgadget.service): where the resolved USB role permits, it carries
     a USB management network (ncm.usb0, CPU-serial-derived /30, no forwarding) so
     http://<hostname>/ works over USB even with Wi-Fi off, plus the
     wizard-toggled USB audio function. Install expresses no composition
     intent: it converges the descriptor against the gadget's own truth table
     and rebinds only on a real difference; the source-intent coordinator owns
     canonical On in direct-lane-before-advertising order. Install the
     NM keyfile owning usb0 and the scoped,
     device-activated jasper-usbnet-dhcp.service (dnsmasq-base — no global
     dnsmasq service). USB audio stays off by default. Skips cleanly
     pre-reboot when no UDC exists yet. Kill switch:
     JASPER_USB_NETWORK=disabled.
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
    # rustc + cargo are required to build the Rust audio daemons
    # (rust/jasper-fanin/ and rust/jasper-outputd/). Trixie ships rustc 1.85, comfortably above
    # our crate's rust-version=1.75 floor.
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
    # /sound/ and /correction/ share; this list must stay in sync with
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

    # aec-bridge is no longer a CamillaDSP instance. It is now a
    # Python bridge (`jasper-aec-bridge`, see jasper/cli/aec_bridge.py)
    # that either runs WebRTC AEC3 for the software fallback profile or,
    # in chip-AEC profiles, carries the selected XVF hardware-AEC beam to
    # jasper-voice while WebRTC AEC3 is bypassed. Old aec-bridge.yml is
    # removed if present from a prior install.
    rm -f "${CAMILLA_CONF}/aec-bridge.yml"
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
        /opt/jasper/.venv/bin/python -m jasper.output_hardware --write; then
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

    # The ring sibling collapsed into outputd-cutover.yml (ADR-0100), and the
    # renderer only SKIPS writing files — it never removes one. A box upgraded
    # across the collapse keeps a stale full-range outputd-cutover-ring.yml that
    # nothing selects but the camillagui config browser still lists. Remove it
    # here, on the one deploy path that owns these bytes. Best-effort: a failed
    # unlink is cosmetic, never a failed deploy.
    rm -f "${CAMILLA_CONF}/outputd-cutover-ring.yml" 2>/dev/null || true
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

    # /etc/asound.conf provides the system-wide ALSA PCM definitions; its own
    # header owns what they are and why (deploy/alsa/asoundrc.jasper).
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

# Generic "delete-and-append" rewrite of one KEY=value line in
# /etc/jasper/jasper.env. Shared by the streambox env-refresh path.
set_jasper_env_value() {
    local key="$1"
    local value="$2"
    sed_inplace "${ENV_DIR}/jasper.env" "/^${key}=/d"
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_DIR}/jasper.env"
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
    # These keys live in aec_mode.env, all owned by the /wake/
    # input-profile / wake-detection cards:
    #   - JASPER_AUDIO_INPUT_PROFILE  canonical profile selection
    #                                 (auto, xvf_chip_aec,
    #                                 xvf_chip_aec_testing,
    #                                 xvf_software_aec3, direct_mic,
    #                                 custom)
    #   - JASPER_AEC_MODE             master AEC bridge toggle
    #   - JASPER_WAKE_LEG_RAW         additive raw chip-direct leg (~5 MB)
    #   - JASPER_WAKE_LEG_DTLN        additive DTLN neural leg (~75 MB)
    #   - JASPER_WAKE_LEG_CHIP_AEC    XVF3800 chip-AEC profile gate
    #                                 (hardware-conditional, mutually
    #                                 exclusive with raw/DTLN)
    #   - JASPER_WAKE_LEG_CHIP_AEC_150 optional extra 150° chip-AEC wake
    #                                  detector (~30 MB)
    #   - JASPER_WAKE_LEG_CHIP_AEC_210 optional extra 210° chip-AEC wake
    #                                  detector (~30 MB)
    #   - JASPER_AEC_CHIP_REF_OBSERVE opt-in: on the software-AEC3 path,
    #                                 arm outputd's chip-ref writer FOR
    #                                 MEASUREMENT ONLY so the Layer-0 SRO
    #                                 drift estimator gets fed (mic path
    #                                 stays software AEC3). Default off.
    # Defaults: profile auto. A managed XVF3800 resolves to the commissioned
    # fixed chip-AEC profile only on supported mic/output hardware; otherwise
    # the reconciler parks voice with an actionable reason. Named testing,
    # software-AEC3, and direct-mic intents do not bypass that policy. Software
    # AEC3/direct fallback remains available for non-XVF microphones, while
    # low-level DTLN/raw/extra-beam lab work requires the explicit custom
    # profile.
    #
    # On upgrade, the reconciler's ensure_mode_file appends any
    # missing keys with these same defaults — preserving an
    # operator's hand-set JASPER_AEC_MODE/leg fields while inferring a
    # profile for pre-profile installs.
    if [[ ! -f "${STATE_DIR}/aec_mode.env" ]]; then
        printf 'JASPER_AUDIO_INPUT_PROFILE=auto\nJASPER_AEC_MODE=auto\nJASPER_WAKE_LEG_RAW=1\nJASPER_WAKE_LEG_DTLN=0\nJASPER_WAKE_LEG_CHIP_AEC=0\nJASPER_WAKE_LEG_CHIP_AEC_150=0\nJASPER_WAKE_LEG_CHIP_AEC_210=0\nJASPER_AEC_CHIP_REF_OBSERVE=0\n' \
            > "${STATE_DIR}/aec_mode.env"
        chmod 0644 "${STATE_DIR}/aec_mode.env"
    fi
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
    # See deploy/nginx-jasper.conf "Why HTTPS is added back" for context.
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
            --app-css-version "${app_css_ver}"; then
        echo "  ERROR: failed to render the landing page; refusing to ship a broken page" >&2
        return 1
    fi
    echo "  landing page: rendered (capabilities, control token, icon sprite)"
    # All /assets/ content (app.css, fonts, per-page CSS + ES modules) +
    # the .install-manifest the doctor verifies — see
    # deploy/lib/install/web-assets.sh for the copy shape and the
    # manifest contract.
    install_web_assets

    # Prune retired static pages from prior installs. Their nginx routes and
    # install copies are gone (the correction preflight's self-signed-HTTPS
    # hop was removed per issue #2632); remove the orphaned files so a
    # previously-deployed Pi does not keep unreachable pages on disk.
    rm -f /usr/share/jasper-web/integrations.html
    rm -f /usr/share/jasper-web/correction-preflight.html
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

install_nginx_site() {
    # Standalone nginx site that reverse-proxies /spotify/ (multi-account
    # OAuth web flow) and /voice/ (voice-provider config wizard) on plain
    # HTTP. /correction/ and the /sound/* measurement routes are proxied on
    # both listeners, but browser mic capture only works on the HTTPS one:
    # getUserMedia grants mic access in a secure context only. That origin is
    # the installer's own self-signed cert, so it is entered deliberately and
    # never by redirect — a cert interstitial is un-automatable (issue #2632).
    # The legacy routes stay HTTP — Spotify's HTTPS requirement is satisfied
    # by the GitHub Pages bounce, and there's no point breaking working flows
    # for one feature. /google/ stays HTTP here; Google rejects mDNS redirect
    # URIs, so it uses the same GitHub Pages bounce pattern as Spotify. The
    # correction-only cert is provisioned by provision_correction_tls() before
    # this function runs.
    install -d -m 0755 /etc/nginx/snippets
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-proxy-headers.conf" \
        /etc/nginx/snippets/jts-proxy-headers.conf
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-jasper.conf" \
        /etc/nginx/sites-enabled/jasper.conf

    install_management_static_assets "${REPO_DIR}/deploy/index.html"

    # Disable Debian's default site so it doesn't clash with our
    # default_server directives. nginx-light installs an enabled
    # `default` symlink; remove it idempotently.
    rm -f /etc/nginx/sites-enabled/default
    tune_nginx_worker_processes

    if nginx -t 2>/dev/null; then
        systemctl enable --now nginx 2>/dev/null || true
        systemctl reload nginx
        echo "  nginx reloaded — http://<host>/{,spotify,voice} + https://<host>/{correction,google} are live"
    else
        echo "  ERROR: nginx config test failed; not reloading. Run 'nginx -t' to debug." >&2
        return 1
    fi
}

install_streambox_nginx_site() {
    # Streambox uses the normal JTS landing page with capability-gated cards,
    # plus an nginx route set limited to local sources, DSP, grouping, and
    # system health. That keeps the frontend shared while omitting voice/wake
    # surfaces whose daemons are intentionally absent from this profile.
    install -d -m 0755 /etc/nginx/snippets
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-proxy-headers.conf" \
        /etc/nginx/snippets/jts-proxy-headers.conf
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-jasper-streambox.conf" \
        /etc/nginx/sites-enabled/jasper.conf

    install_management_static_assets "${REPO_DIR}/deploy/index.html"
    rm -f /etc/nginx/sites-enabled/default
    tune_nginx_worker_processes

    if nginx -t 2>/dev/null; then
        systemctl enable --now nginx 2>/dev/null || true
        systemctl reload nginx
        echo "  streambox nginx reloaded — http://<host>/{,spotify,sources,sound,system,voice,google,transit,weather,ha,tools,chat} + https://<host>/{correction,sync} are live"
    else
        echo "  ERROR: streambox nginx config test failed; not reloading. Run 'nginx -t' to debug." >&2
        return 1
    fi
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
    # then let jasper.control_advert.render_control_advert substitute
    # the (XML-escaped) name, atomic-write the live file, and reload
    # Avahi. The /speaker save path re-renders on a name change.
    install -d -m 0755 /etc/jasper/avahi-templates
    install -m 0644 \
        "${REPO_DIR}/deploy/avahi/jasper-control.service.template" \
        /etc/jasper/avahi-templates/jasper-control.service

    # A non-root jasper-control renders the peering advert
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
    # _jasper-control._tcp un-advertised — /rooms and jasper-doctor's
    # "avahi: _jasper-control._tcp" check depend on it always existing.
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

run_doctor_summary() {
    # Final pre-flight: run jasper-doctor so the operator sees status of
    # every subsystem (env file, mic, firmware, AEC bridge, renderers,
    # provider keys, …) at install time. Non-blocking — install is done
    # by the time we get here; this is just a status report.
    #
    # Critical for catching the "silent productization gaps" — e.g. an
    # XVF chip on 6-ch firmware but with the ALSA mixer's ch2-5 muted,
    # otherwise invisible until a wake-word test fails days later.
    if [[ ! -x /opt/jasper/.venv/bin/jasper-doctor ]]; then
        return 0
    fi
    echo
    if build_swap_required; then
        echo "=== low-memory deploy health pre-flight ==="
        if "${REPO_DIR}/deploy/bin/jasper-deploy-health"; then
            echo "✓ low-memory deploy health checks pass."
        else
            echo
            echo "─────────────────────────────────────────────────────────────"
            echo " low-memory deploy health reports failures (see above)."
            echo " Install finished, but core runtime health isn't clean."
            echo " Re-run after fixing: sudo ${REPO_DIR}/deploy/bin/jasper-deploy-health"
            echo "─────────────────────────────────────────────────────────────"
        fi
        return 0
    fi

    echo "=== jasper-doctor pre-flight ==="
    local doctor_status
    set +e
    /opt/jasper/.venv/bin/jasper-doctor
    doctor_status=$?
    set -e
    if (( doctor_status == 0 )); then
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
        echo "event=install_profile.migrate previous=$(read_raw_persisted_install_profile) profile=${install_profile} source=marker"
    fi
    hardware_tier_preflight  # log tier; fail fast on unsupported arch (before any mutation)
    if [[ "${install_profile}" == "streambox" ]]; then
        require_root
        persist_install_profile "${install_profile}"
        require_build_user  # Rust builds run as 'pi'; fail fast pre-mutation
        setup_build_swap_if_needed
        trap install_exit_cleanup EXIT
        create_jasper_service_users  # before unit install + state-dir creation
        park_low_memory_build_units
        install_streambox_deps
        install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
        install_camilladsp
        install_renderers
        reconcile_headless_boot_config
        reconcile_usb_data_role
        tune_wifi_for_airplay
        install_streambox_jasper
        migrate_calibration_sign_convention  # vendor mic cal files are response curves
        ensure_output_hardware_state
        render_outputd_cutover_config
        ensure_outputd_camilla_statefile
        ensure_crossover_camilla_statefile  # camilla#2 seed (INERT; unit not enabled)
        reassert_secrets_compartment_perms  # assistant provider keys jasper-voice reads
        reassert_intsecrets_compartment_perms  # streambox Spotify creds/cache perms
        build_install_jasper_fanin
        build_install_jasper_outputd
        install_jts_ring_platform  # jts_ring ioplug + conf.d + shm dir (staging only; arming is the coupling reconciler's)
        install_streambox_systemd_units
        remove_retired_audio_topology_state  # retired dmix/fanin switch state; doctor WARNs on its presence
        migrate_wifi_guardian
        migrate_memory_resilience
        migrate_cgroup_memory_enabled
        install_journald_persistent_storage
        install_avahi_jasper_control
        install_jasper_control_polkit  # grant non-root jasper-control its scoped systemctl/reboot
        install_jasper_web_polkit  # grant jasper-web NetworkManager wifi management
        widen_jasper_web_writable_dirs  # /etc/bluetooth + camilladsp/configs group-jasper writable
        install_peering_template
        provision_correction_tls
        install_streambox_nginx_site
        widen_control_secret_env_modes  # secret env group-jasper readable for the spawned doctor
        # Final mutation: stamp the verified-install manifest only now that
        # every step above succeeded (set -e). run_doctor_summary below is
        # non-mutating diagnostics — keep write_build_manifest the LAST
        # state change so a failure anywhere above leaves the prior good
        # manifest. See ADR-0172.
        write_build_manifest
        run_doctor_summary
        return 0
    fi
    require_root
    persist_install_profile "${install_profile}"
    require_build_user  # Rust builds run as 'pi'; fail fast pre-mutation
    setup_build_swap_if_needed
    trap install_exit_cleanup EXIT
    create_jasper_service_users  # before unit install + state-dir creation
    park_low_memory_build_units
    install_deps
    install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
    install_camilladsp
    install_renderers
    reconcile_headless_boot_config
    reconcile_usb_data_role
    tune_wifi_for_airplay
    install_jasper
    migrate_calibration_sign_convention  # vendor mic cal files are response curves
    ensure_output_hardware_state
    render_outputd_cutover_config
    ensure_outputd_camilla_statefile
    ensure_crossover_camilla_statefile  # camilla#2 seed (INERT; unit not enabled)
    build_install_jasper_fanin    # Rust daemon binary; enabled by install_systemd_units
    build_install_jasper_outputd  # Rust mainline final-output owner
    install_jts_ring_platform     # jts_ring ioplug + conf.d + shm dir (staging only; arming is the coupling reconciler's)
    install_systemd_units
    remove_retired_audio_topology_state  # retired dmix/fanin switch state; doctor WARNs on its presence
    migrate_memory_resilience   # Stage 1 OOM protection: sysctl + MGLRU + zram
    migrate_cgroup_memory_enabled  # Stage 2 audio-slice: cgroup memory + PSI in cmdline.txt
    install_journald_persistent_storage
    install_avahi_jasper_control
    install_jasper_control_polkit  # grant non-root jasper-control its scoped systemctl/reboot
    install_jasper_web_polkit  # grant jasper-web NetworkManager wifi management
    widen_jasper_web_writable_dirs  # /etc/bluetooth + camilladsp/configs group-jasper writable
    install_peering_template
    provision_correction_tls   # cert files must exist before nginx -t
    install_nginx_site
    install_camillagui
    regenerate_audio_cues
    widen_control_secret_env_modes  # secret env group-jasper readable for the spawned doctor
    # Final mutation: stamp the verified-install manifest only now that
    # every step above succeeded (set -e). run_doctor_summary below is
    # non-mutating diagnostics — keep write_build_manifest the LAST state
    # change so a failure anywhere above leaves the prior good manifest.
    # See ADR-0172.
    write_build_manifest
    run_doctor_summary
}

# Only run main when invoked directly. When sourced (e.g. by tests
# that want to call a single helper like `_compute_min_free_kbytes`),
# define the functions but don't execute main.
if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
    main "$@"
fi
