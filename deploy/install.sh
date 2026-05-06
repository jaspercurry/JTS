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
        libsndfile1 curl ca-certificates rsync \
        dfu-util \
        libspeexdsp-dev libspeexdsp1 swig
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

    # v1.yml has no substitutions — CamillaDSP plays unconditionally
    # to pcm.jasper_out (defined in /root/.asoundrc), which fans the
    # stream out to the dongle + XVF3800 USB-IN. Just copy.
    install -m 0644 \
        "${REPO_DIR}/deploy/camilladsp/v1.yml" \
        "${CAMILLA_CONF}/v1.yml"

    # NOTE: aec-bridge is no longer a CamillaDSP instance — it's
    # now a Python software AEC daemon (jasper-aec-bridge, see
    # jasper/cli/aec_bridge.py). The chip's on-chip AEC turned out
    # to be incompatible with our external-DAC topology, so we do
    # SpeexDSP cancellation on the host using the XVF chip's raw
    # mic 0 (channel 2 of 6-ch firmware) + the dsnoop-tapped music
    # reference. Old aec-bridge.yml is removed if present from a
    # prior install.
    rm -f "${CAMILLA_CONF}/aec-bridge.yml"
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
    install -d -m 0755 /etc/modules-load.d /etc/alsa/conf.d /etc/modprobe.d
    install -m 0644 \
        "${REPO_DIR}/deploy/modules-load.d/snd-aloop.conf" \
        /etc/modules-load.d/snd-aloop.conf
    # Two snd-aloop cards: card 0 'Loopback' for the music chain,
    # card 1 'LoopbackAEC' for the AEC bridge → jasper-voice mic
    # path. Without the second card, PortAudio (no substream
    # selection) would address sub0 of the only loopback, which
    # collides with the music chain.
    install -m 0644 \
        "${REPO_DIR}/deploy/modprobe.d/snd-aloop.conf" \
        /etc/modprobe.d/snd-aloop.conf
    # Reload module so the new card config takes effect (idempotent).
    rmmod snd_aloop 2>/dev/null || true
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

    # Detect Apple USB-C dongle card name. Falls back to "A" (the
    # literal default on PiOS Trixie). If the dongle isn't plugged
    # in at install time, the fallback is fine — jasper-doctor will
    # catch a real mismatch. The XVF3800's card name ("Array") is
    # hardcoded in deploy/camilladsp/aec-bridge.yml because the AEC
    # bridge is only meaningful with the XVF (UMIK has no AEC).
    local dongle_card
    dongle_card=$(detect_card aplay 'usb-c to 3.5mm' 'A')
    echo "  Apple dongle: CARD=${dongle_card}"

    # Render /root/.asoundrc from template with detected dongle name.
    # /root/.asoundrc is read by CamillaDSP + jasper-voice (both run
    # as root via systemd). moOde/MPD runs as a different uid,
    # unaffected.
    if [[ -f /root/.asoundrc && ! -L /root/.asoundrc ]]; then
        if ! grep -q "jasper_dongle" /root/.asoundrc; then
            cp /root/.asoundrc "/root/.asoundrc.pre-jasper.$(date +%s)"
        fi
    fi
    sed -e "s/__DONGLE_CARD__/${dongle_card}/g" \
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

    # SpeexDSP Python bindings — used by jasper-aec-bridge for
    # software echo cancellation. The xiongyihui/speexdsp-python
    # repo's __init__.py is broken on Python 3.13 (tries to import
    # a SWIG-generated wrapper that isn't actually built), so we
    # patch __init__.py post-install to load the SWIG extension
    # module directly. swig + libspeexdsp-dev are installed by
    # install_deps.
    "${INSTALL_DIR}/.venv/bin/pip" install \
        "git+https://github.com/xiongyihui/speexdsp-python.git"
    local sx_init
    sx_init="${INSTALL_DIR}/.venv/lib/python3.13/site-packages/speexdsp/__init__.py"
    if [[ -f "${sx_init}" ]]; then
        echo "from ._speexdsp import *" > "${sx_init}"
    fi

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
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web.service" \
        "${SYSTEMD_DIR}/jasper-web.service"
    # AEC bridge + boot-time chip init (see asoundrc.jasper header).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-bridge.service" \
        "${SYSTEMD_DIR}/jasper-aec-bridge.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-init.service" \
        "${SYSTEMD_DIR}/jasper-aec-init.service"

    # Drop-in override forcing the system shairport-sync.service onto
    # moOde's `_audioout` ALSA symbol. Without this it writes to ALSA
    # `default` and bypasses our zz-jts-loopback.conf hijack — see header
    # comment in shairport-sync-jts-output.conf for the full rationale.
    install -d -m 0755 "${SYSTEMD_DIR}/shairport-sync.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/shairport-sync-jts-output.conf" \
        "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf"

    systemctl daemon-reload
    systemctl enable jasper-camilla.service jasper-voice.service \
        jasper-web.service
    # NOTE: jasper-aec-bridge + jasper-aec-init are installed but
    # NOT enabled by default. Software AEC is opt-in — see CLAUDE.md
    # "Acoustic echo cancellation" section for the on/off procedure
    # and the trade-off rationale (modest attenuation, ~110 MB RAM
    # cost on 1GB Pi 5). To enable:
    #   systemctl enable --now jasper-aec-init jasper-aec-bridge
    #   sed -i 's|^JASPER_MIC_DEVICE=.*|JASPER_MIC_DEVICE=hw:5,1|' \
    #       /etc/jasper/jasper.env
    #   systemctl restart jasper-voice

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

install_self_signed_cert() {
    # Self-signed cert for https://jasper.local — required because
    # Spotify (post-2024) rejects HTTP redirect URIs unless they're
    # the loopback exception (127.0.0.1). Each phone clicks through
    # the cert warning once. 10-year validity so we don't have to
    # think about renewal in our hobby-project lifespan.
    local crt="/etc/nginx/ssl/jasper.crt"
    local key="/etc/nginx/ssl/jasper.key"
    install -d -m 0755 /etc/nginx/ssl
    if [[ -f "${crt}" && -f "${key}" ]]; then
        echo "  (TLS cert already present at ${crt})"
        return 0
    fi
    openssl req -x509 -nodes -days 3650 \
        -newkey rsa:2048 \
        -keyout "${key}" \
        -out "${crt}" \
        -subj "/CN=jasper.local" \
        -addext "subjectAltName=DNS:jasper.local,DNS:jasper,IP:127.0.0.1" \
        2>/dev/null
    chmod 0644 "${crt}"
    chmod 0640 "${key}"
    chgrp www-data "${key}" 2>/dev/null || true
    echo "  Generated self-signed cert at ${crt}"
}

install_nginx_proxy() {
    # Reverse-proxy /spotify/ → http://127.0.0.1:8765/ so household
    # members can hit https://jasper.local/spotify on their phone to
    # link their Spotify account.
    #
    # Two pieces:
    # 1. /etc/nginx/jasper-locations.conf — the actual location block.
    # 2. include directive added idempotently to moOde's HTTP site
    #    AND a separate sites-enabled/jasper-https.conf serving on 443.
    #
    # We DO NOT replace moOde's nginx site config — it owns the host's
    # web surface. We add ONE line to its HTTP site (the include) and
    # we add a NEW site for HTTPS that doesn't touch moOde's files.

    local moode_site="/etc/nginx/sites-enabled/moode-http.conf"
    local jasper_locs="/etc/nginx/jasper-locations.conf"
    local jasper_https="/etc/nginx/sites-enabled/jasper-https.conf"
    local include_line='include /etc/nginx/jasper-locations.conf;'

    if [[ ! -f "${moode_site}" ]]; then
        echo "  (skipping nginx setup — moOde site config not found at ${moode_site})"
        return 0
    fi

    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-jasper.conf" \
        "${jasper_locs}"
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-jasper-https.conf" \
        "${jasper_https}"

    if ! grep -qF "jasper-locations.conf" "${moode_site}"; then
        # Insert the include just before the server block's closing brace.
        # awk acts on the LAST `}` line (the server's close). The backup
        # MUST go outside /etc/nginx/sites-enabled/ — nginx auto-loads
        # everything there as a server block, and a backup *.conf file
        # would trigger a duplicate-default-server error.
        install -d -m 0755 /etc/nginx/backups
        cp "${moode_site}" "/etc/nginx/backups/moode-http.conf.pre-jasper.$(date +%s)"
        awk -v line="	${include_line}" '
            { lines[NR] = $0 }
            END {
                for (i = NR; i >= 1; i--) {
                    if (lines[i] ~ /^[[:space:]]*}[[:space:]]*$/) { last = i; break }
                }
                for (i = 1; i <= NR; i++) {
                    if (i == last) print line
                    print lines[i]
                }
            }
        ' "${moode_site}" > "${moode_site}.tmp"
        mv "${moode_site}.tmp" "${moode_site}"
        echo "  Added include directive to ${moode_site}"
    fi

    if nginx -t 2>/dev/null; then
        systemctl reload nginx
        echo "  nginx reloaded — jasper.local/spotify is live (after jasper-web starts)"
    else
        echo "  WARNING: nginx config test failed; not reloading. Run 'nginx -t' to debug."
    fi
}

main() {
    require_root
    install_deps
    install_camilladsp
    install_alsa
    install_jasper
    install_systemd_units
    install_self_signed_cert
    install_nginx_proxy
}

main "$@"
