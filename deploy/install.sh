#!/usr/bin/env bash
# Install jasper voice daemon + always-on CamillaDSP on a Pi running moOde.
# Run on the Pi after Phase 1A is verified working (moOde plays via dongle).
#
# Idempotent: re-running upgrades the venv and re-applies configs.
#
# Pre-reqs the operator handles by hand (see PLAN.md):
#   - moOde 10.1.2+ flashed and on the network
#   - Apple USB-C dongle plugged in, selected as moOde output, 48 kHz
#   - moOde "Custom" CamillaDSP mode enabled in the moOde web UI
#   - /etc/jasper/jasper.env populated from .env.example

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
INSTALL_DIR="/opt/jasper"
CAMILLA_DIR="/opt/camilladsp"
CAMILLA_CONF="/etc/camilladsp"
ENV_DIR="/etc/jasper"
STATE_DIR="/var/lib/jasper"
SYSTEMD_DIR="/etc/systemd/system"

CAMILLA_VERSION="v4.1.3"
CAMILLA_TARBALL="camilladsp-linux-aarch64.tar.gz"
CAMILLA_SHA256="d9a17092923ebfe5d20a770c6b6a7eb2268f9700f999bf604b9db09f518aca5a"
CAMILLA_URL="https://github.com/HEnquist/camilladsp/releases/download/${CAMILLA_VERSION}/${CAMILLA_TARBALL}"

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "this script must be run as root (use sudo)" >&2
        exit 1
    fi
}

install_deps() {
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        build-essential libasound2-dev libasound2 portaudio19-dev \
        libsndfile1 curl ca-certificates
}

install_camilladsp() {
    # moOde 10.1.2 ships CamillaDSP 3.0.1 as `camilladsp.service`. In Custom
    # CamillaDSP mode it should be stopped — but be belt-and-suspenders so a
    # previously-enabled instance doesn't fight ours over /etc/asoundrc or
    # the dmix lock. Errors are ignored (service may not exist on this
    # moOde version).
    systemctl stop camilladsp.service 2>/dev/null || true
    systemctl disable camilladsp.service 2>/dev/null || true

    install -d -m 0755 "${CAMILLA_DIR}" "${CAMILLA_CONF}"
    if [[ ! -x "${CAMILLA_DIR}/camilladsp" ]]; then
        local tmpdir
        tmpdir="$(mktemp -d)"
        echo "Fetching CamillaDSP ${CAMILLA_VERSION}..."
        curl -fsSL -o "${tmpdir}/${CAMILLA_TARBALL}" "${CAMILLA_URL}"
        echo "${CAMILLA_SHA256}  ${tmpdir}/${CAMILLA_TARBALL}" | sha256sum -c -
        tar -xzf "${tmpdir}/${CAMILLA_TARBALL}" -C "${CAMILLA_DIR}" camilladsp
        chmod +x "${CAMILLA_DIR}/camilladsp"
        rm -rf "${tmpdir}"
        echo "Installed CamillaDSP to ${CAMILLA_DIR}/camilladsp"
    fi
    install -m 0644 "${REPO_DIR}/deploy/camilladsp/v1.yml" "${CAMILLA_CONF}/v1.yml"
}

install_alsa() {
    install -d -m 0755 /etc/modules-load.d
    install -m 0644 \
        "${REPO_DIR}/deploy/modules-load.d/snd-aloop.conf" \
        /etc/modules-load.d/snd-aloop.conf
    modprobe snd-aloop || true

    # Install /root/.asoundrc so CamillaDSP and jasper-voice both see the
    # shared dmix `jasper_dongle` device. moOde/MPD is unaffected (different
    # uid). If a /root/.asoundrc already exists, back it up rather than
    # clobbering — operator may have customised it.
    if [[ -f /root/.asoundrc && ! -L /root/.asoundrc ]]; then
        if ! cmp -s "${REPO_DIR}/deploy/alsa/asoundrc.jasper" /root/.asoundrc; then
            cp /root/.asoundrc "/root/.asoundrc.pre-jasper.$(date +%s)"
        fi
    fi
    install -m 0600 "${REPO_DIR}/deploy/alsa/asoundrc.jasper" /root/.asoundrc
}

install_jasper() {
    install -d -m 0755 "${INSTALL_DIR}"
    install -d -m 0750 "${STATE_DIR}"
    install -d -m 0750 "${ENV_DIR}"

    rsync -a --delete \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='tests' --exclude='deploy' \
        "${REPO_DIR}/jasper" "${REPO_DIR}/pyproject.toml" \
        "${INSTALL_DIR}/"

    if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
        python3 -m venv "${INSTALL_DIR}/.venv"
    fi
    "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip wheel
    "${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

    # openWakeWord stock models (hey_jarvis + required feature models)
    # don't auto-download on first model load. Pull them now so the daemon
    # starts cleanly. Idempotent — re-running is fine.
    "${INSTALL_DIR}/.venv/bin/python" -c \
        "import openwakeword.utils as u; u.download_models()"

    if [[ ! -f "${ENV_DIR}/jasper.env" ]]; then
        install -m 0640 "${REPO_DIR}/.env.example" "${ENV_DIR}/jasper.env"
        echo
        echo "Created ${ENV_DIR}/jasper.env from template."
        echo "Edit it and set GEMINI_API_KEY before starting jasper-voice."
        echo
    fi
}

install_systemd_units() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-camilla.service" \
        "${SYSTEMD_DIR}/jasper-camilla.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-voice.service" \
        "${SYSTEMD_DIR}/jasper-voice.service"
    systemctl daemon-reload
    systemctl enable jasper-camilla.service jasper-voice.service
    echo
    echo "Units enabled. Start with: systemctl start jasper-camilla jasper-voice"
}

main() {
    require_root
    install_deps
    install_camilladsp
    install_alsa
    install_jasper
    install_systemd_units
}

main "$@"
