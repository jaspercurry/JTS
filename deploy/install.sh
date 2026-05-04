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
        libsndfile1 curl ca-certificates rsync
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

    # Render v1.yml with playback device chosen by JASPER_AEC_MODE.
    # Reads existing /etc/jasper/jasper.env if present (re-install path),
    # else defaults to "hardware" — XVF3800 jack as speaker. Either
    # value renders to a valid pcm name in /root/.asoundrc.
    local aec_mode="hardware"
    if [[ -f "${ENV_DIR}/jasper.env" ]]; then
        aec_mode=$(grep -E '^JASPER_AEC_MODE=' "${ENV_DIR}/jasper.env" 2>/dev/null \
                   | tail -1 | cut -d= -f2- | tr -d ' "' || true)
        [[ -z "$aec_mode" ]] && aec_mode="hardware"
    fi
    local playback_device
    case "$aec_mode" in
        hardware) playback_device="jasper_xvf" ;;
        software) playback_device="jasper_dongle" ;;
        *)
            echo "  WARN: unknown JASPER_AEC_MODE='${aec_mode}', defaulting to 'hardware'"
            playback_device="jasper_xvf"
            ;;
    esac
    echo "  CamillaDSP playback device (AEC mode '${aec_mode}'): ${playback_device}"
    sed "s|__PLAYBACK_DEVICE__|${playback_device}|g" \
        "${REPO_DIR}/deploy/camilladsp/v1.yml" > "${CAMILLA_CONF}/v1.yml"
    chmod 0644 "${CAMILLA_CONF}/v1.yml"
}

detect_card() {
    # detect_card "<aplay|arecord>" "<grep regex>" "<fallback>"
    local tool="$1" regex="$2" fallback="$3"
    local card
    card=$("$tool" -L 2>/dev/null \
        | grep -B1 -iE "$regex" \
        | grep -oE 'CARD=[^,]+' \
        | head -1 \
        | sed 's/CARD=//')
    if [[ -n "$card" ]]; then
        echo "$card"
    else
        echo "$fallback"
    fi
}

install_alsa() {
    install -d -m 0755 /etc/modules-load.d /etc/alsa/conf.d
    install -m 0644 \
        "${REPO_DIR}/deploy/modules-load.d/snd-aloop.conf" \
        /etc/modules-load.d/snd-aloop.conf
    modprobe snd-aloop || true

    # Hijack moOde's pcm._audioout symbol to redirect all renderers
    # (MPD/shairport/librespot/bluealsa) into snd-aloop Loopback instead
    # of the physical DAC. moOde's UI blocks selecting Loopback directly
    # ("Device is reserved"); the ALSA-layer override sidesteps that.
    # See header comment in the conf file for the full rationale + the
    # required moOde UI settings.
    # Clean up older 99- prefix from earlier iterations — that prefix
    # loaded BEFORE _audioout.conf in ASCII order and didn't override.
    rm -f /etc/alsa/conf.d/99-jts-loopback.conf
    install -m 0644 \
        "${REPO_DIR}/deploy/alsa/zz-jts-loopback.conf" \
        /etc/alsa/conf.d/zz-jts-loopback.conf

    # Detect Apple USB-C dongle card name. Falls back to "A" (the literal
    # default on PiOS Trixie). If the dongle isn't plugged in at install
    # time, the fallback is fine — jasper-doctor will catch a real mismatch.
    local dongle_card
    dongle_card=$(detect_card aplay 'usb-c to 3.5mm' 'A')
    echo "  Apple dongle: CARD=${dongle_card}"

    # Detect XVF3800 / ReSpeaker card name for the jasper_xvf dmix slave.
    # Falls back to "Array" (PiOS literal). Used by JASPER_AEC_MODE=hardware.
    local mic_card
    mic_card=$(detect_card arecord 'xvf3800|respeaker.*array' 'Array')
    echo "  XVF3800 (AEC playback target): CARD=${mic_card}"

    # Render /root/.asoundrc from template with detected card names.
    # /root/.asoundrc is read by CamillaDSP + jasper-voice (both run as
    # root via systemd). moOde/MPD runs as a different uid, unaffected.
    if [[ -f /root/.asoundrc && ! -L /root/.asoundrc ]]; then
        if ! grep -q "jasper_dongle" /root/.asoundrc; then
            cp /root/.asoundrc "/root/.asoundrc.pre-jasper.$(date +%s)"
        fi
    fi
    sed -e "s/__DONGLE_CARD__/${dongle_card}/g" \
        -e "s/__MIC_CARD__/${mic_card}/g" \
        "${REPO_DIR}/deploy/alsa/asoundrc.jasper" > /root/.asoundrc
    chmod 0600 /root/.asoundrc
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

    # openwakeword 0.6.0 hard-requires tflite-runtime on Linux, but
    # tflite-runtime has no Python 3.13 wheel (and PiOS Trixie ships
    # python3.13 only — no python3.12 in apt). We use ONNX models
    # exclusively (onnxruntime is already in pyproject.toml), so
    # tflite-runtime is never imported at runtime. Pre-install
    # openwakeword without its declared deps, then install its non-tflite
    # runtime deps explicitly. The subsequent editable install of
    # jasper-speaker sees openwakeword==0.6.0 already satisfied.
    "${INSTALL_DIR}/.venv/bin/pip" install --no-deps openwakeword==0.6.0
    "${INSTALL_DIR}/.venv/bin/pip" install \
        requests tqdm 'scipy>=1.3,<2' 'scikit-learn>=1,<2'

    "${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

    # openWakeWord stock models (hey_jarvis + required feature models)
    # don't auto-download on first model load. Pull them now so the daemon
    # starts cleanly. Idempotent — re-running is fine.
    "${INSTALL_DIR}/.venv/bin/python" -c \
        "import openwakeword.utils as u; u.download_models()"

    if [[ ! -f "${ENV_DIR}/jasper.env" ]]; then
        # Detect ReSpeaker XVF3800 card name. Default "Array" (PiOS literal
        # name; product description matches and it's also a substring of
        # PortAudio's enumerated name "Array: USB Audio (hw:N,0)").
        # JASPER_MIC_DEVICE format is a PortAudio device name/substring,
        # NOT an ALSA pcm string — see jasper/config.py for the rationale.
        local mic_card
        mic_card=$(detect_card arecord 'xvf3800|respeaker.*array' 'Array')
        echo "  ReSpeaker mic: ${mic_card}"
        sed "s|JASPER_MIC_DEVICE=Array|JASPER_MIC_DEVICE=${mic_card}|" \
            "${REPO_DIR}/.env.example" > "${ENV_DIR}/jasper.env"
        chmod 0640 "${ENV_DIR}/jasper.env"
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

    # Drop-in override forcing the system shairport-sync.service onto
    # moOde's `_audioout` ALSA symbol. Without this it writes to ALSA
    # `default` and bypasses our zz-jts-loopback.conf hijack — see header
    # comment in shairport-sync-jts-output.conf for the full rationale.
    install -d -m 0755 "${SYSTEMD_DIR}/shairport-sync.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/shairport-sync-jts-output.conf" \
        "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf"

    systemctl daemon-reload
    systemctl enable jasper-camilla.service jasper-voice.service

    # On a fresh moOde, shairport-sync is spawned outside systemd by
    # moOde's startup mechanism (with its own ALSA args, and root-uid).
    # That instance is holding port 7000, so a `systemctl restart` of
    # the systemd unit would fail to bind. Kill any non-systemd
    # shairport-sync first, reset any prior failed state, then start
    # the systemd-managed instance (which inherits our drop-in's
    # `-- -d _audioout` ExecStart).
    pkill -x shairport-sync 2>/dev/null || true
    sleep 1
    systemctl reset-failed shairport-sync.service 2>/dev/null || true
    systemctl restart shairport-sync.service 2>/dev/null || true

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
