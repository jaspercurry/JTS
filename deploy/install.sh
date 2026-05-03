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
        build-essential libasound2-dev portaudio19-dev \
        libsndfile1
}

install_camilladsp() {
    if [[ ! -x "${CAMILLA_DIR}/camilladsp" ]]; then
        echo "CamillaDSP binary not found at ${CAMILLA_DIR}/camilladsp."
        echo "Download v4.1.3 from https://github.com/HEnquist/camilladsp/releases"
        echo "and place the binary at ${CAMILLA_DIR}/camilladsp before re-running."
        exit 1
    fi
    install -d -m 0755 "${CAMILLA_CONF}"
    install -m 0644 "${REPO_DIR}/deploy/camilladsp/v1.yml" "${CAMILLA_CONF}/v1.yml"
}

install_alsa_loopback() {
    install -d -m 0755 /etc/modules-load.d
    install -m 0644 \
        "${REPO_DIR}/deploy/modules-load.d/snd-aloop.conf" \
        /etc/modules-load.d/snd-aloop.conf
    modprobe snd-aloop || true
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
    install_alsa_loopback
    install_jasper
    install_systemd_units
}

main "$@"
