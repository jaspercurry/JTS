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
# The one destructive (rm) path under /usr/local/sbin outside the install
# table's own `install` calls; a variable so a test harness can confine it to
# a temp root instead of touching the host's real one.
LOCAL_SBIN_DIR="/usr/local/sbin"
INSTALL_PROFILE_DEFAULT="full"
INSTALL_PROFILE_MARKER="${STATE_DIR}/install_profile"

source "${REPO_DIR}/deploy/lib/jasper-env-file.sh"
source "${REPO_DIR}/deploy/lib/jasper-asound-render.sh"
source "${REPO_DIR}/deploy/lib/install/state-and-secrets.sh"
source "${REPO_DIR}/deploy/lib/install/retirements.sh"
source "${REPO_DIR}/deploy/lib/install/service-users.sh"
source "${REPO_DIR}/deploy/lib/install/memory-resilience.sh"
source "${REPO_DIR}/deploy/lib/install/build-sandbox.sh"
source "${REPO_DIR}/deploy/lib/install/renderers.sh"
source "${REPO_DIR}/deploy/lib/install/web-assets.sh"
source "${REPO_DIR}/deploy/lib/install/model-staging.sh"
source "${REPO_DIR}/deploy/lib/install/rust-daemons.sh"
# Ring platform: builds the jts_ring ALSA ioplug + ships its conf.d/tmpfiles
# assets. Sourced after build-sandbox.sh (uses run_contained_build).
source "${REPO_DIR}/deploy/lib/install/ring-platform.sh"
source "${REPO_DIR}/deploy/lib/install/python-runtime.sh"
source "${REPO_DIR}/deploy/lib/install/systemd-units.sh"
source "${REPO_DIR}/deploy/lib/install/profile.sh"
source "${REPO_DIR}/deploy/lib/install/deps.sh"
source "${REPO_DIR}/deploy/lib/install/dsp-runtime.sh"
source "${REPO_DIR}/deploy/lib/install/alsa.sh"
source "${REPO_DIR}/deploy/lib/install/web-services.sh"
source "${REPO_DIR}/deploy/lib/install/discovery.sh"
source "${REPO_DIR}/deploy/lib/install/build-manifest.sh"
source "${REPO_DIR}/deploy/lib/install/journald.sh"
source "${REPO_DIR}/deploy/lib/install/cues.sh"
source "${REPO_DIR}/deploy/lib/install/doctor.sh"
# Hash-pinned vendored source for the optional enhanced AEC engine. This file
# is also parsed by jasper.audio_routes.enhanced_aec; do not duplicate these values here.
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
  JASPER_HOSTNAME=<name>.local
                             Speaker identity/cert hostname for direct
                             Pi-local installs. scripts/deploy-to-pi.sh
                             forwards this automatically.
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
# are out of scope).
BUILD_USER="pi"

require_build_user() {
    # Fail fast, BEFORE any host mutation: the Rust builds that chown to and
    # `sudo -u` this user run only after apt and the renderer stack have
    # already mutated the host.
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

install_run_bounded() {
    local seconds="$1" status=0
    shift 2
    timeout --kill-after=5s "${seconds}s" "$@" || status=$?
    if (( status == 124 || status == 137 )); then
        jasper_install_log "event=install.command_timeout command=${1##*/} timeout_s=${seconds}"
    fi
    return "${status}"
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
    "support_files|both|install_jasper_support_files|publish the shared shell libraries under /usr/local/lib/jasper"
    "renderers|both|install_renderers|build/install shairport-sync, nqptp, librespot and bluez-alsa"
    "headless_boot|both|reconcile_headless_boot_config|trim the Pi boot config for headless operation"
    "usb_role|both|reconcile_usb_data_role|reconcile the USB data role from board topology"
    "wifi_airplay|both|tune_wifi_for_airplay|disable WiFi power-save on the active wlan0 connection"
    "jasper|full|install_jasper|copy the Python package and build the full-tier venv"
    "jasper|streambox|install_streambox_jasper|copy the Python package and build the streambox venv"
    # install_jasper has jasper-cues on PATH by now, and this has to land
    # before systemd_units: jasper-aec-reconcile (inside install_systemd_units)
    # can restart jasper-voice, whose no-provider park would otherwise fire
    # before any cue WAV exists (AGENTS.md non-negotiable 6, issue #4814). A
    # row that produces something a unit needs sits before the unit rows.
    "audio_cues|full|regenerate_audio_cues|regenerate the local audio cues"
    "secrets_perms|both|reassert_secrets_compartment_perms|re-assert the /var/lib/jasper-secrets compartment"
    "intsecrets_perms|both|reassert_intsecrets_compartment_perms|re-assert the /var/lib/jasper-intsecrets compartment"
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
    "retired|both|retire_leftovers|retire the units and files earlier releases left behind"
    "control_polkit|both|install_jasper_control_polkit|install the jasper-control polkit rules"
    "systemd_units|full|install_systemd_units|install, enable and start the full-tier systemd units"
    "systemd_units|streambox|install_streambox_systemd_units|install, enable and start the streambox systemd units"
    "wifi_guardian|both|migrate_wifi_guardian|seed the WiFi guardian recovery stash"
    "memory_resilience|both|migrate_memory_resilience|apply the sysctl, MGLRU and zram memory resilience"
    "cgroup_memory|both|migrate_cgroup_memory_enabled|add the memory cgroup/PSI kernel args"
    "journald|both|install_journald_persistent_storage|enable persistent journald storage"
    "web_polkit|both|install_jasper_web_polkit|install the jasper-web NetworkManager polkit rules"
    "web_writable_dirs|both|widen_jasper_web_writable_dirs|widen /etc/bluetooth and the camilladsp configs for jasper-web"
    # provision_correction_tls first: the cert files must exist before nginx -t.
    "correction_tls|both|provision_correction_tls|provision the correction TLS CA and cert files"
    "nginx_site|full|install_nginx_site|install the full-tier nginx route set"
    "nginx_site|streambox|install_streambox_nginx_site|install the streambox nginx route set"
    "camillagui|full|install_camillagui|install the socket-activated CamillaGUI backend"
    "control_env_modes|both|widen_control_secret_env_modes|widen the config/state files jasper-control reads"
    # ADR-0172: the manifest is the LAST mutation, so reaching it proves every
    # row above succeeded under set -e. The doctor row after it is read-only.
    "build_manifest|both|write_build_manifest|stamp the verified-install build manifest"
    "doctor|both|run_doctor_summary_advisory|run jasper-doctor --core as a non-blocking health summary"
)

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
in ${INSTALL_PROFILE_MARKER}. An explicit JASPER_INSTALL_PROFILE selects a
conversion; an unset profile keeps the persisted tier.

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
