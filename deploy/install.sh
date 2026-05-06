#!/usr/bin/env bash
# Install jasper voice daemon + always-on CamillaDSP on a Raspberry Pi.
#
# Two backends supported:
#   --backend=moode  (default): assumes moOde 10.1.2+ is already
#       running. Hijacks moOde's pcm._audioout to redirect renderers
#       into snd-aloop, then bridges to CamillaDSP. The historical
#       baseline.
#   --backend=debian: stock Debian Trixie Lite, no moOde. Source-builds
#       shairport-sync (AirPlay 2) + nqptp + go-librespot + bluez-alsa
#       + bt-agent, owns the full systemd unit per renderer, no
#       _audioout hijack. Validated on jts.local 2026-05-06.
#
# Idempotent: re-running upgrades the venv and re-applies configs.
#
# Pre-reqs the operator handles by hand:
#   moOde backend (see PLAN.md):
#     - moOde 10.1.2+ flashed and on the network
#     - Apple USB-C dongle plugged in, selected as moOde output, 48 kHz
#     - moOde "Custom" CamillaDSP mode enabled in the moOde web UI
#   debian backend (see deploy/debian-stack/README.md):
#     - Raspberry Pi OS Lite (Trixie, 64-bit) on a Pi 5 (2GB recommended,
#       1GB also fits). SSH + Wi-Fi pre-configured via Imager.
#     - Apple USB-C dongle plugged in. Speakers connected and the amp
#       turned on.
#   Both: /etc/jasper/jasper.env populated from .env.example,
#         GEMINI_API_KEY set.

set -euo pipefail

# Parse --backend= flag. Default to moode for backward compat.
BACKEND="moode"
for arg in "$@"; do
    case "$arg" in
        --backend=moode)  BACKEND="moode" ;;
        --backend=debian) BACKEND="debian" ;;
        --backend=*) echo "unknown backend: ${arg#--backend=}" >&2; exit 2 ;;
    esac
done
echo "==> install.sh starting (backend=${BACKEND})"

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
INSTALL_DIR="/opt/jasper"
CAMILLA_DIR="/opt/camilladsp"
CAMILLA_CONF="/etc/camilladsp"
ENV_DIR="/etc/jasper"
STATE_DIR="/var/lib/jasper"
SYSTEMD_DIR="/etc/systemd/system"
DEBIAN_STACK_DIR="${REPO_DIR}/deploy/debian-stack"

CAMILLA_VERSION="v4.1.3"
CAMILLA_TARBALL="camilladsp-linux-aarch64.tar.gz"
CAMILLA_SHA256="d9a17092923ebfe5d20a770c6b6a7eb2268f9700f999bf604b9db09f518aca5a"
CAMILLA_URL="https://github.com/HEnquist/camilladsp/releases/download/${CAMILLA_VERSION}/${CAMILLA_TARBALL}"

# Versions for source builds (debian backend only).
GO_LIBRESPOT_VERSION="v0.7.1"
GO_LIBRESPOT_URL="https://github.com/devgianlu/go-librespot/releases/download/${GO_LIBRESPOT_VERSION}/go-librespot_linux_arm64.tar.gz"
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
        libspeexdsp-dev libspeexdsp1 swig

    if [[ "$BACKEND" == "debian" ]]; then
        # Source-build deps for shairport-sync (AirPlay 2) + nqptp,
        # plus the bluez-alsa userspace and the bt-agent helper.
        # All of these are absent on a stock Trixie Lite image.
        apt-get install -y --no-install-recommends \
            autoconf automake libtool pkg-config \
            libpopt-dev libconfig-dev libavahi-client-dev \
            libssl-dev libsoxr-dev libplist-dev libsodium-dev \
            libgcrypt20-dev uuid-dev libmbedtls-dev libglib2.0-dev \
            libavutil-dev libavcodec-dev libavformat-dev libswresample-dev \
            xxd \
            bluez-alsa-utils bluez-tools avahi-utils
    fi
}

install_camilladsp() {
    # moOde 10.1.2 ships CamillaDSP 3.0.1 as `camilladsp.service`. In Custom
    # CamillaDSP mode it should be stopped — but be belt-and-suspenders so a
    # previously-enabled instance doesn't fight ours over /etc/asoundrc or
    # the dmix lock. Errors are ignored (service may not exist on this
    # moOde version, and on debian backend it never exists).
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

    if [[ "$BACKEND" == "debian" ]]; then
        # Debian-stack config captures from plughw:Loopback,1,0 and
        # writes to plughw:CARD=__DONGLE_CARD__ — no jasper_capture/
        # jasper_out PCMs (those need /root/.asoundrc which the debian
        # backend doesn't install). Substitute the dongle card in
        # place; install_alsa_debian() exports DONGLE_CARD for us.
        local dongle="${DONGLE_CARD:-A}"
        sed "s/__DONGLE_CARD__/${dongle}/g" \
            "${DEBIAN_STACK_DIR}/etc/camilladsp/v1.yml" \
            > "${CAMILLA_CONF}/v1.yml"
        chmod 0644 "${CAMILLA_CONF}/v1.yml"
    else
        # moOde-stack config: CamillaDSP plays unconditionally to
        # pcm.jasper_out (defined in /root/.asoundrc), which fans
        # the stream out to the dongle + XVF3800 USB-IN. No
        # substitution — the asoundrc has the substitution.
        install -m 0644 \
            "${REPO_DIR}/deploy/camilladsp/v1.yml" \
            "${CAMILLA_CONF}/v1.yml"
    fi

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

    if [[ "$BACKEND" == "debian" ]]; then
        # debian-stack snd-aloop options: index=6,7 (clears HDMI which
        # claims 0,1 on a fresh Pi OS Lite). The moOde variant uses
        # index=0,5 because moOde's kernel module ordering disables
        # HDMI audio early.
        install -m 0644 \
            "${DEBIAN_STACK_DIR}/etc/modprobe.d/snd-aloop.conf" \
            /etc/modprobe.d/snd-aloop.conf
    else
        install -m 0644 \
            "${REPO_DIR}/deploy/modprobe.d/snd-aloop.conf" \
            /etc/modprobe.d/snd-aloop.conf
    fi
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

    if [[ "$BACKEND" == "debian" ]]; then
        # No _audioout hijack — we own each renderer and point them
        # directly at hw:Loopback,0,0.
        rm -f /etc/alsa/conf.d/zz-jts-loopback.conf  # remove if leftover from moOde install

        # /root/.asoundrc on debian provides ONLY jasper_out (dmix on
        # the dongle). CamillaDSP captures plughw:Loopback,1,0 directly
        # (no dsnoop fan-out until AEC bridge is reintroduced); both
        # CamillaDSP music and jasper-voice TTS write to jasper_out so
        # dmix sums them before the speakers.
        if [[ -f /root/.asoundrc && ! -L /root/.asoundrc ]]; then
            if ! grep -q "jasper_out" /root/.asoundrc; then
                cp /root/.asoundrc "/root/.asoundrc.pre-jasper.$(date +%s)"
            fi
        fi
        sed -e "s/__DONGLE_CARD__/${DONGLE_CARD}/g" \
            "${DEBIAN_STACK_DIR}/etc/asoundrc-jasper.template" \
            > /root/.asoundrc
        chmod 0600 /root/.asoundrc
        echo "  (debian backend — wrote /root/.asoundrc with jasper_out)"
        return 0
    fi

    # ---- moOde-stack only below this line ----

    # Hijack moOde's pcm._audioout symbol to redirect all renderers
    # (MPD/shairport/librespot/bluealsa) into snd-aloop Loopback instead
    # of the physical DAC. moOde's UI blocks selecting Loopback directly
    # ("Device is reserved"); the ALSA-layer override sidesteps that.
    rm -f /etc/alsa/conf.d/99-jts-loopback.conf
    install -m 0644 \
        "${REPO_DIR}/deploy/alsa/zz-jts-loopback.conf" \
        /etc/alsa/conf.d/zz-jts-loopback.conf

    # Render /root/.asoundrc from template with detected dongle name.
    # /root/.asoundrc is read by CamillaDSP + jasper-voice (both run
    # as root via systemd). moOde/MPD runs as a different uid,
    # unaffected.
    if [[ -f /root/.asoundrc && ! -L /root/.asoundrc ]]; then
        if ! grep -q "jasper_dongle" /root/.asoundrc; then
            cp /root/.asoundrc "/root/.asoundrc.pre-jasper.$(date +%s)"
        fi
    fi
    sed -e "s/__DONGLE_CARD__/${DONGLE_CARD}/g" \
        "${REPO_DIR}/deploy/alsa/asoundrc.jasper" > /root/.asoundrc
    chmod 0600 /root/.asoundrc
}

# Source-build go-librespot, nqptp, shairport-sync (AirPlay 2). Run
# only on debian backend. Each is idempotent — checks for the
# installed binary and skips the build if present.
install_debian_renderers() {
    # ---- go-librespot ----
    if [[ ! -x /usr/local/bin/go-librespot ]]; then
        echo "Fetching go-librespot ${GO_LIBRESPOT_VERSION}..."
        local tmpdir
        tmpdir="$(mktemp -d)"
        curl -fsSL -o "${tmpdir}/glr.tar.gz" "${GO_LIBRESPOT_URL}"
        tar -xzf "${tmpdir}/glr.tar.gz" -C "${tmpdir}" go-librespot
        install -m 0755 "${tmpdir}/go-librespot" /usr/local/bin/go-librespot
        rm -rf "${tmpdir}"
        echo "  Installed /usr/local/bin/go-librespot"
    fi
    install -d -m 0755 /etc/go-librespot
    install -m 0644 \
        "${DEBIAN_STACK_DIR}/etc/go-librespot/config.yml" \
        /etc/go-librespot/config.yml
    chown -R pi:pi /etc/go-librespot 2>/dev/null || true

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
        "${DEBIAN_STACK_DIR}/etc/shairport-sync.conf" \
        /etc/shairport-sync.conf

    # bluez-alsa-utils + bluez-tools were apt-installed in install_deps.
    # Configure /etc/bluetooth/main.conf for speaker-mode (Just Works
    # pairing, audio-class device).
    bash "${DEBIAN_STACK_DIR}/configure-bluez.sh"
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
        "${REPO_DIR}/deploy/systemd/jasper-control.service" \
        "${SYSTEMD_DIR}/jasper-control.service"
    # AEC bridge + boot-time chip init (see asoundrc.jasper header).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-bridge.service" \
        "${SYSTEMD_DIR}/jasper-aec-bridge.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-init.service" \
        "${SYSTEMD_DIR}/jasper-aec-init.service"

    if [[ "$BACKEND" == "debian" ]]; then
        # We own the full systemd units for each renderer + nqptp +
        # bt-agent. No drop-in override on shairport-sync — it's our
        # unit pointing at our binary at /usr/local/bin/shairport-sync.
        install -m 0644 \
            "${DEBIAN_STACK_DIR}/systemd/go-librespot.service" \
            "${SYSTEMD_DIR}/go-librespot.service"
        install -m 0644 \
            "${DEBIAN_STACK_DIR}/systemd/shairport-sync.service" \
            "${SYSTEMD_DIR}/shairport-sync.service"
        install -m 0644 \
            "${DEBIAN_STACK_DIR}/systemd/nqptp.service" \
            "${SYSTEMD_DIR}/nqptp.service"
        install -m 0644 \
            "${DEBIAN_STACK_DIR}/systemd/bt-agent.service" \
            "${SYSTEMD_DIR}/bt-agent.service"
        # jasper-mux: latest-source-wins preemption between Spotify,
        # AirPlay, and Bluetooth. moOde does this via worker.php; on
        # debian we own the orchestration ourselves.
        install -m 0644 \
            "${DEBIAN_STACK_DIR}/systemd/jasper-mux.service" \
            "${SYSTEMD_DIR}/jasper-mux.service"
        # Drop-in routing bluealsa-aplay's output into the JTS loopback
        # instead of ALSA default (HDMI on a fresh Pi).
        install -d -m 0755 "${SYSTEMD_DIR}/bluealsa-aplay.service.d"
        install -m 0644 \
            "${DEBIAN_STACK_DIR}/systemd/bluealsa-aplay.service.d/jts-output.conf" \
            "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-output.conf"
    else
        # Drop-in override forcing the system shairport-sync.service onto
        # moOde's `_audioout` ALSA symbol. Without this it writes to ALSA
        # `default` and bypasses our zz-jts-loopback.conf hijack — see header
        # comment in shairport-sync-jts-output.conf for the full rationale.
        install -d -m 0755 "${SYSTEMD_DIR}/shairport-sync.service.d"
        install -m 0644 \
            "${REPO_DIR}/deploy/systemd/shairport-sync-jts-output.conf" \
            "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf"
    fi

    systemctl daemon-reload
    systemctl enable jasper-camilla.service jasper-voice.service \
        jasper-web.service jasper-control.service

    if [[ "$BACKEND" == "debian" ]]; then
        systemctl enable nqptp.service shairport-sync.service \
            go-librespot.service bt-agent.service jasper-mux.service
        systemctl restart bluealsa-aplay.service 2>/dev/null || true
        systemctl restart nqptp.service shairport-sync.service \
            go-librespot.service bt-agent.service jasper-mux.service \
            2>/dev/null || true
    else
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
    fi

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
    install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
    install_camilladsp
    if [[ "$BACKEND" == "debian" ]]; then
        install_debian_renderers
    fi
    install_jasper
    install_systemd_units
    install_self_signed_cert
    if [[ "$BACKEND" == "moode" ]]; then
        install_nginx_proxy
    else
        # TODO(debian-stack): write a stand-alone nginx site for
        # https://jts.local/spotify (no moOde site to edit). Until
        # then, household members can hit https://jts.local:8765/spotify
        # directly (jasper-web's bound port; HTTPS via the self-signed
        # cert).
        echo "  (debian backend — skipping moOde nginx integration; see TODO)"
    fi
}

main "$@"
