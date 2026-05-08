#!/usr/bin/env bash
# Install jasper voice daemon + always-on CamillaDSP on a Raspberry Pi.
#
# Source-builds shairport-sync (AirPlay 2) + nqptp, drops in
# librespot (rust, via raspotify .deb) + bluez-alsa + bt-agent,
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

echo "==> install.sh starting"

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

# Versions for source builds (debian backend only).
# raspotify ships librespot (rust) 0.8.0 as an arm64 .deb. We use
# this instead of go-librespot because rust librespot supports
# `--volume-ctrl log` for a perceptually linear volume slider —
# go-librespot has a hardcoded cubic curve that concentrates
# dynamic range at the top of the slider (unusable on real
# speakers). See docs/HANDOFF-volume.md for full rationale.
RASPOTIFY_VERSION="0.48.1"
RASPOTIFY_URL="https://github.com/dtcooper/raspotify/releases/download/${RASPOTIFY_VERSION}/raspotify_${RASPOTIFY_VERSION}.librespot.v0.8.0-ea81314_arm64.deb"
SHAIRPORT_SYNC_VERSION="4.3.7"
NQPTP_REPO="https://github.com/mikebrady/nqptp.git"
SHAIRPORT_SYNC_REPO="https://github.com/mikebrady/shairport-sync.git"

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
        libspeexdsp-dev libspeexdsp1 swig \
        libwebrtc-audio-processing-dev pkg-config \
        nginx-light openssl

    # Source-build deps for shairport-sync (AirPlay 2) + nqptp, plus
    # the bluez-alsa userspace and the bt-agent helper. All of these
    # are absent on a stock Trixie Lite image.
    apt-get install -y --no-install-recommends \
        autoconf automake libtool pkg-config \
        libpopt-dev libconfig-dev libavahi-client-dev \
        libssl-dev libsoxr-dev libplist-dev libsodium-dev \
        libgcrypt20-dev uuid-dev libmbedtls-dev libglib2.0-dev \
        libavutil-dev libavcodec-dev libavformat-dev libswresample-dev \
        xxd \
        bluez-alsa-utils bluez-tools avahi-utils
}

install_camilladsp() {
    # Belt-and-suspenders: any pre-existing camilladsp.service from a
    # different install lineage shouldn't fight our copy over
    # /etc/asoundrc or the dmix lock.
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

    # CamillaDSP captures plughw:Loopback,1,0 and writes to
    # pcm.jasper_out (defined in /root/.asoundrc with __DONGLE_CARD__
    # substituted). The yaml itself doesn't need substitution —
    # install_alsa() handles the dongle name in /root/.asoundrc.
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
    install -m 0644 \
        "${REPO_DIR}/deploy/modprobe.d/snd-aloop.conf" \
        /etc/modprobe.d/snd-aloop.conf
    # Reload module so the new card config takes effect (idempotent).
    rmmod snd_aloop 2>/dev/null || true
    modprobe snd-aloop || true

    # Detect Apple USB-C dongle card name. Falls back to "A" (the
    # literal default on PiOS Trixie). If the dongle isn't plugged
    # in at install time, the fallback is fine — jasper-doctor will
    # catch a real mismatch. Exported so install_camilladsp can pick
    # it up for __DONGLE_CARD__ substitution.
    DONGLE_CARD=$(detect_card aplay 'usb-c to 3.5mm' 'A')
    echo "  Apple dongle: CARD=${DONGLE_CARD}"
    export DONGLE_CARD

    # /root/.asoundrc provides jasper_out (dmix on the dongle).
    # CamillaDSP captures plughw:Loopback,1,0 directly (no dsnoop
    # fan-out until AEC bridge is reintroduced); both CamillaDSP
    # music and jasper-voice TTS write to jasper_out so dmix sums
    # them before the speakers.
    if [[ -f /root/.asoundrc && ! -L /root/.asoundrc ]]; then
        if ! grep -q "jasper_out" /root/.asoundrc; then
            cp /root/.asoundrc "/root/.asoundrc.pre-jasper.$(date +%s)"
        fi
    fi
    sed -e "s/__DONGLE_CARD__/${DONGLE_CARD}/g" \
        "${REPO_DIR}/deploy/alsa/asoundrc.jasper" \
        > /root/.asoundrc
    chmod 0600 /root/.asoundrc
    echo "  Wrote /root/.asoundrc with jasper_out"
}

# Source-build / fetch librespot, nqptp, shairport-sync (AirPlay 2).
# Run only on debian backend. Each is idempotent — checks for the
# installed binary and skips the install if present.
install_renderers() {
    # ---- librespot (rust, via raspotify .deb) ----
    # We use the raspotify .deb because (a) it ships librespot 0.8.0
    # arm64 binaries; (b) the librespot project itself doesn't ship
    # binaries; (c) building from cargo on a Pi takes 20+ minutes.
    # raspotify's own systemd unit + config are disabled — we run
    # our own /etc/systemd/system/librespot.service with the flags
    # we want (--volume-ctrl log being the headline).
    if [[ ! -x /usr/bin/librespot ]]; then
        echo "Installing librespot via raspotify ${RASPOTIFY_VERSION}..."
        local tmpdir
        tmpdir="$(mktemp -d)"
        curl -fsSL -o "${tmpdir}/raspotify.deb" "${RASPOTIFY_URL}"
        DEBIAN_FRONTEND=noninteractive apt install -y "${tmpdir}/raspotify.deb"
        rm -rf "${tmpdir}"
        # Disable raspotify's default service; we run our own unit.
        systemctl disable --now raspotify.service 2>/dev/null || true
        echo "  Installed /usr/bin/librespot ($(librespot --version 2>&1 | head -1 || echo unknown))"
    fi
    # The --onevent hook script that writes /run/librespot/state.json
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-librespot-event" \
        /usr/local/bin/jasper-librespot-event

    # ---- nqptp ----
    if [[ ! -x /usr/local/bin/nqptp ]]; then
        echo "Building nqptp from source..."
        local tmpdir
        tmpdir="$(mktemp -d)"
        git clone --depth 1 "${NQPTP_REPO}" "${tmpdir}/nqptp"
        (
            cd "${tmpdir}/nqptp"
            autoreconf -fi
            ./configure --with-systemd-startup
            make -j4
            make install
        )
        rm -rf "${tmpdir}"
        echo "  Installed /usr/local/bin/nqptp"
    fi

    # ---- shairport-sync (AirPlay 2) ----
    # Trixie's apt package ships AirPlay 1 only. Source-build with
    # --with-airplay-2 for AP2. The version output (`shairport-sync -V`)
    # should contain "AirPlay2" if the build worked; we pattern-match
    # to detect a stale apt install and rebuild.
    local need_build=0
    if [[ ! -x /usr/local/bin/shairport-sync ]]; then
        need_build=1
    elif ! /usr/local/bin/shairport-sync -V 2>&1 | grep -q "AirPlay2"; then
        need_build=1
    fi
    if [[ "$need_build" == "1" ]]; then
        echo "Building shairport-sync ${SHAIRPORT_SYNC_VERSION} with AirPlay 2..."
        # Remove apt-installed AP1 build if present. Keep
        # /etc/shairport-sync.conf — apt remove preserves it; apt
        # purge doesn't, so we use remove.
        systemctl stop shairport-sync 2>/dev/null || true
        apt-get remove -y shairport-sync 2>/dev/null || true
        local tmpdir
        tmpdir="$(mktemp -d)"
        git clone --depth 1 --branch "${SHAIRPORT_SYNC_VERSION}" \
            "${SHAIRPORT_SYNC_REPO}" "${tmpdir}/sps"
        (
            cd "${tmpdir}/sps"
            autoreconf -fi
            ./configure --sysconfdir=/etc \
                --with-alsa --with-soxr --with-avahi \
                --with-ssl=openssl --with-systemd \
                --with-airplay-2 \
                --with-metadata --with-dbus-interface \
                --with-mpris-interface
            make -j4
            # `make install` may fail at the systemd step due to an
            # `install` flag mismatch on Trixie — the binary lands fine
            # at /usr/local/bin/shairport-sync. We deploy our own unit
            # file below regardless, so an install-systemd failure
            # is OK.
            make install || true
        )
        rm -rf "${tmpdir}"
        echo "  Installed /usr/local/bin/shairport-sync"
    fi

    # shairport-sync needs a dedicated user (the configure-time default
    # is shairport-sync:shairport-sync); apt's package would have
    # created it but we may not have apt-installed first.
    if ! getent group shairport-sync >/dev/null 2>&1; then
        groupadd -r shairport-sync
    fi
    if ! getent passwd shairport-sync >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g shairport-sync -G audio shairport-sync
    fi

    install -m 0644 \
        "${REPO_DIR}/deploy/shairport-sync.conf" \
        /etc/shairport-sync.conf

    # bluez-alsa-utils + bluez-tools were apt-installed in install_deps.
    # Configure /etc/bluetooth/main.conf for speaker-mode (Just Works
    # pairing, audio-class device).
    bash "${REPO_DIR}/deploy/configure-bluez.sh"
}

install_jasper() {
    install -d -m 0755 "${INSTALL_DIR}"
    install -d -m 0750 "${STATE_DIR}"
    install -d -m 0750 "${ENV_DIR}"

    rsync -a --delete \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='tests' --exclude='deploy' \
        --exclude='build' --exclude='*.egg-info' \
        "${REPO_DIR}/jasper" "${REPO_DIR}/jasper_aec3" \
        "${REPO_DIR}/pyproject.toml" \
        "${INSTALL_DIR}/"

    # Stage firmware/ next to the package so jasper-dial-onboard
    # finds the bin (default --bin path: /opt/jasper/firmware/dial/
    # jasper-dial.bin). The .pio build dir is excluded — that's local
    # to whoever ran build.sh and contains absolute paths.
    if [[ -d "${REPO_DIR}/firmware" ]]; then
        rsync -a --delete \
            --exclude='.pio' --exclude='.pioenvs' --exclude='.piolibdeps' \
            "${REPO_DIR}/firmware" "${INSTALL_DIR}/"
    fi

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

    # jasper_aec3 — pybind11 binding around Trixie's
    # libwebrtc-audio-processing-1 (v1.3-3, AEC3). Compiled per-host
    # against the apt-installed library; pkg-config and the dev
    # package are installed by install_deps. The wheel is the
    # alternative AEC engine selected by JASPER_AEC_ENGINE=webrtc3.
    # No-op when the source dir is absent (e.g. an old checkout).
    if [[ -d "${INSTALL_DIR}/jasper_aec3" ]]; then
        "${INSTALL_DIR}/.venv/bin/pip" install \
            "${INSTALL_DIR}/jasper_aec3"
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
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-dial-web.service" \
        "${SYSTEMD_DIR}/jasper-dial-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-control.service" \
        "${SYSTEMD_DIR}/jasper-control.service"
    # AEC bridge + boot-time chip init (see asoundrc.jasper header).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-bridge.service" \
        "${SYSTEMD_DIR}/jasper-aec-bridge.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-init.service" \
        "${SYSTEMD_DIR}/jasper-aec-init.service"
    # Pin the Apple dongle's analog Headphone control to 100% at every
    # boot — the dynamic volume control happens in CamillaDSP (or the
    # source's own slider) and the dongle should never be limiting us.
    # DONGLE_CARD was set above by install_alsa.
    sed -e "s/__DONGLE_CARD__/${DONGLE_CARD}/g" \
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
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-headphone-monitor.service" \
        "${SYSTEMD_DIR}/jasper-headphone-monitor.service"

    # We own the full systemd units for each renderer + nqptp + bt-agent.
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

    systemctl daemon-reload
    systemctl enable jasper-camilla.service jasper-voice.service \
        jasper-web.service jasper-dial-web.service jasper-control.service \
        jasper-dac-init.service jasper-headphone-monitor.service
    # Apply the dongle Headphone-max pin immediately so a fresh
    # install gets the full analog ceiling without waiting for
    # next reboot.
    systemctl start jasper-dac-init.service || \
        echo "  WARN: jasper-dac-init failed (dongle not enumerated?). \
Will retry on next boot."
    # Restart the headphone monitor so it picks up post-init state.
    systemctl restart jasper-headphone-monitor.service 2>/dev/null || true

    systemctl enable nqptp.service shairport-sync.service \
        librespot.service bt-agent.service jasper-mux.service
    systemctl restart bluealsa-aplay.service 2>/dev/null || true
    systemctl restart nqptp.service shairport-sync.service \
        librespot.service bt-agent.service jasper-mux.service \
        2>/dev/null || true

    # NOTE: jasper-aec-bridge + jasper-aec-init are installed but
    # NOT enabled by default. Software AEC is opt-in — see CLAUDE.md
    # "Acoustic echo cancellation" section for the on/off procedure
    # and the trade-off rationale (modest attenuation, ~110 MB RAM
    # cost on 1GB Pi 5). To enable:
    #   systemctl enable --now jasper-aec-init jasper-aec-bridge
    #   sed -i 's|^JASPER_MIC_DEVICE=.*|JASPER_MIC_DEVICE=hw:5,1|' \
    #       /etc/jasper/jasper.env
    #   systemctl restart jasper-voice

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

install_nginx_site() {
    # Standalone nginx site that reverse-proxies /spotify/ (multi-account
    # OAuth web flow) and /dial/ (rotary-dial onboarding) to their
    # respective jasper-web services. /spotify/ requires HTTPS — Spotify
    # rejects non-loopback HTTP redirect URIs as of 2024.
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-jasper.conf" \
        /etc/nginx/sites-enabled/jasper.conf

    # Disable Debian's default site so it doesn't clash with our
    # default_server directives. nginx-light installs an enabled
    # `default` symlink; remove it idempotently.
    rm -f /etc/nginx/sites-enabled/default

    if nginx -t 2>/dev/null; then
        systemctl enable --now nginx 2>/dev/null || true
        systemctl reload nginx
        echo "  nginx reloaded — https://<host>/spotify and /dial are live"
    else
        echo "  WARNING: nginx config test failed; not reloading. Run 'nginx -t' to debug."
    fi
}

install_avahi_jasper_control() {
    # Advertise jasper-control over mDNS so the rotary dial can find
    # us via service discovery instead of a hardcoded hostname. See
    # deploy/avahi/jasper-control.service for the rationale and the
    # firmware-side counterpart in firmware/dial/src/discovery.cpp.
    install -d -m 0755 /etc/avahi/services
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
    if ! /bin/sh -c '. /etc/jasper/jasper.env && export $(grep -E "^[A-Z_]+=" /etc/jasper/jasper.env | cut -d= -f1) && /opt/jasper/.venv/bin/jasper-cues regenerate'; then
        echo "  WARNING: cue regenerate failed (network down or API key not set?). " \
             "Daemon will retry at startup. To force a refresh later: " \
             "sudo systemctl restart jasper-voice"
    fi
}

main() {
    require_root
    install_deps
    install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
    install_camilladsp
    install_renderers
    install_jasper
    install_systemd_units
    install_avahi_jasper_control
    install_self_signed_cert
    install_nginx_site
    regenerate_audio_cues
}

main "$@"
