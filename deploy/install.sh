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
    # State + emitted-correction-config dirs. The systemd unit's
    # --statefile points at /var/lib/camilladsp/statefile.yml so
    # corrections survive Pi restarts; the room-correction wizard
    # writes correction_<id>_<unixtime>.yml under configs/.
    install -d -m 0755 /var/lib/camilladsp /var/lib/camilladsp/configs

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
    # to be incompatible with our external-DAC topology, so we run
    # WebRTC AEC3 on the host using the XVF chip's raw mic 0
    # (channel 2 of 6-ch firmware) + the dsnoop-tapped music
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

    # shairport-sync config is templated: deploy/shairport-sync.conf.template
    # has a __DISABLE_SYNCHRONIZATION__ placeholder substituted by
    # /usr/local/sbin/jasper-apply-airplay-mode based on
    # /var/lib/jasper/airplay_mode.env. shairport-sync.service's
    # ExecStartPre re-renders on every restart, so toggling the mode
    # (via /airplay/ web UI or jasper-airplay-mode CLI) is just an
    # env-file write + systemctl restart shairport-sync.
    install -m 0644 \
        "${REPO_DIR}/deploy/shairport-sync.conf.template" \
        /etc/shairport-sync.conf.template
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-apply-airplay-mode" \
        /usr/local/sbin/jasper-apply-airplay-mode
    # Default to synced: with shairport-sync.conf.template setting
    # resync_threshold_in_seconds=0.2, synced mode is glitch-free on
    # this chain (empirically verified over multiple 5-min samples
    # after the fix; see docs/HANDOFF-airplay-sync.md). Synced is the
    # right default because it gives users video A/V sync + multi-room
    # AirPlay sync for free. Users can still flip to free-running via
    # /airplay/ if they hit DAC-specific issues. Existing env files
    # are preserved across reinstalls.
    if [[ ! -e /var/lib/jasper/airplay_mode.env ]]; then
        install -d -m 0755 /var/lib/jasper
        printf 'JASPER_AIRPLAY_FREE_RUNNING=no\n' \
            > /var/lib/jasper/airplay_mode.env
        chmod 0644 /var/lib/jasper/airplay_mode.env
        echo "  /var/lib/jasper/airplay_mode.env defaulted to synced."
    fi
    # Seed /etc/shairport-sync.conf so the first start of shairport-sync
    # has a valid config. ExecStartPre re-renders on every subsequent
    # restart, picking up any changes made via the web UI / CLI.
    /usr/local/sbin/jasper-apply-airplay-mode

    # bluez-alsa-utils + bluez-tools were apt-installed in install_deps.
    # Configure /etc/bluetooth/main.conf for speaker-mode (Just Works
    # pairing, audio-class device).
    bash "${REPO_DIR}/deploy/configure-bluez.sh"
}

tune_wifi_for_airplay() {
    # Disable WiFi power-save on the active wlan0 connection.
    # Pi's brcmfmac driver defaults to power-save ON, which causes
    # micro-stalls in WiFi RX during radio sleeps. AirPlay 2 streams
    # over unicast UDP and has no application-level retransmit; even
    # a few-ms WiFi stall correlates with shairport-sync sync errors
    # and underruns. nmcli value 2 = disable; the setting persists in
    # the NetworkManager keyfile, so a future reinstall is a no-op.
    if ! command -v nmcli >/dev/null 2>&1; then
        echo "  nmcli not present; skipping WiFi power-save tweak."
        return 0
    fi
    local wlan_conn
    wlan_conn=$(nmcli -t -f NAME,DEVICE c show --active 2>/dev/null \
        | awk -F: '$2=="wlan0" {print $1; exit}')
    if [[ -z "$wlan_conn" ]]; then
        echo "  no active wlan0 connection; skipping WiFi power-save tweak."
        return 0
    fi
    nmcli c modify "$wlan_conn" 802-11-wireless.powersave 2 \
        2>/dev/null || true
    # Apply without dropping the connection. If the driver doesn't
    # accept a live reapply (some brcmfmac variants), the change
    # still takes effect on the next reconnect/reboot.
    nmcli dev reapply wlan0 2>/dev/null || true
    echo "  WiFi power-save disabled on connection '$wlan_conn'."
}

install_jasper() {
    install -d -m 0755 "${INSTALL_DIR}"
    install -d -m 0750 "${STATE_DIR}"
    install -d -m 0750 "${ENV_DIR}"

    # Build manifest — captures the git SHA + install timestamp at the
    # moment install.sh ran. The /system dashboard reads this to show
    # "version: <sha>, installed <timestamp>". Falls back to "unknown"
    # if REPO_DIR isn't a git checkout (e.g. tarball deploy).
    #
    # Three sources, in priority order:
    #   1. JASPER_DEPLOY_SHA / JASPER_DEPLOY_SHA_FULL / JASPER_DEPLOY_BRANCH
    #      env vars — set by scripts/deploy-to-pi.sh on the laptop before
    #      sudo-running install.sh. This is the only source that works
    #      when the standard rsync deploy excludes .git/ (which it does).
    #   2. Local git checkout in REPO_DIR — when install.sh is run
    #      directly against a fresh `git clone` on the Pi.
    #   3. "unknown" — tarball deploys, no git info available.
    local git_sha="${JASPER_DEPLOY_SHA:-unknown}"
    local git_full="${JASPER_DEPLOY_SHA_FULL:-unknown}"
    local git_branch="${JASPER_DEPLOY_BRANCH:-unknown}"
    if [[ "${git_sha}" == "unknown" ]] && \
       { [[ -d "${REPO_DIR}/.git" ]] || git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; }; then
        git_sha=$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)
        git_full=$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)
        git_branch=$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
    fi
    cat > "${STATE_DIR}/build.txt" <<EOF
JASPER_GIT_SHA=${git_sha}
JASPER_GIT_SHA_FULL=${git_full}
JASPER_GIT_BRANCH=${git_branch}
JASPER_INSTALL_AT=$(date -Iseconds)
EOF
    chmod 0644 "${STATE_DIR}/build.txt"
    echo "  Build manifest: ${git_sha} on ${git_branch}"


    # Per-account Google refresh tokens live under here at mode 0600.
    # Tighten the parent dirs too so non-root processes can't even
    # `ls` the per-household-member token filenames (the names are
    # PII-adjacent — they identify which household members linked
    # accounts). install -d resets perms on existing dirs, so this
    # also tightens any pre-existing 755 left from earlier installs.
    install -d -m 0700 "${STATE_DIR}/google" "${STATE_DIR}/google/tokens"

    rsync -a --delete \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='tests' --exclude='deploy' \
        --exclude='build' --exclude='*.egg-info' \
        "${REPO_DIR}/jasper" "${REPO_DIR}/jasper_aec3" \
        "${REPO_DIR}/pyproject.toml" \
        "${INSTALL_DIR}/"

    # Stage firmware/ next to the package so jasper-{dial,satellite}-onboard
    # find their respective bins (default --bin paths:
    # /opt/jasper/firmware/dial/jasper-dial.bin,
    # /opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin).
    # The .pio build dir is excluded — that's local to whoever ran the
    # per-firmware build.sh and contains absolute paths.
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
    # The 4 wizard daemons are SOCKET-ACTIVATED (each .service is paired
    # with a .socket unit that holds the port and re-spawns the daemon
    # on demand). systemd binds the listener; the daemon adopts the fd
    # via LISTEN_FDS and exits after 10 min idle, saving ~60-90 MB Pss
    # while no one is using a setup page. See jasper/web/_systemd.py.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web.service" \
        "${SYSTEMD_DIR}/jasper-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web.socket" \
        "${SYSTEMD_DIR}/jasper-web.socket"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-dial-web.service" \
        "${SYSTEMD_DIR}/jasper-dial-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-dial-web.socket" \
        "${SYSTEMD_DIR}/jasper-dial-web.socket"
    # /correction/ wizard. Phase 0 = mic-permission verify only;
    # future phases pull in heavy deps (numpy / scipy / pyfar) so
    # this lives in its own process rather than colocating with
    # jasper-web (Spotify + voice settings). Mirrors jasper-dial-web.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-correction-web.service" \
        "${SYSTEMD_DIR}/jasper-correction-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-correction-web.socket" \
        "${SYSTEMD_DIR}/jasper-correction-web.socket"
    # /bluetooth/ control panel — generic BT scan/pair/forget for
    # phones, knobs, headphones. Drives bluez via dbus-next; per-class
    # post-pair behaviour lives in jasper/bluetooth/handlers/.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-bluetooth-web.service" \
        "${SYSTEMD_DIR}/jasper-bluetooth-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-bluetooth-web.socket" \
        "${SYSTEMD_DIR}/jasper-bluetooth-web.socket"
    # /system/ dashboard — RAM/CPU/temp sparklines + cloud activity
    # + restart/diagnostics actions. Socket-activated like the other
    # wizards.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-system-web.service" \
        "${SYSTEMD_DIR}/jasper-system-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-system-web.socket" \
        "${SYSTEMD_DIR}/jasper-system-web.socket"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-control.service" \
        "${SYSTEMD_DIR}/jasper-control.service"
    # jasper-input: third-party HID accessory bridge (Anticater VK-01
    # volume knob today; future macro pads / foot pedals). Reads
    # /dev/input/event* via python-evdev, translates known devices'
    # key events into HTTP calls against jasper-control. Always-on
    # like jasper-mux — idle cost is negligible if no accessory is
    # attached. See jasper/accessories/.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-input.service" \
        "${SYSTEMD_DIR}/jasper-input.service"
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
    # Custom udev rule: re-pins the dongle's Headphone control to 100%
    # on every USB (re-)enumeration AND disables autosuspend on the
    # device. Compensates for two upstream issues:
    #   * Trixie's alsa-utils 1.2.14-1 ships a broken
    #     /usr/lib/udev/rules.d/90-alsa-restore.rules where a GOTO
    #     points at the wrong label, so `alsactl restore` never fires
    #     on hotplug (Debian bug #1093057, still open).
    #   * The Apple dongle's UAC firmware default for the Headphone
    #     control is 80/120 (-20 dB), surfaced via UAC GET_CUR each
    #     time the device probes. Without our rule, every speaker
    #     re-plug or USB resume that triggers re-enumeration costs
    #     the user 20 dB of analog attenuation until they reboot or
    #     run `systemctl start jasper-dac-init` manually.
    # Active reset is also done by jasper-headphone-monitor (1 Hz
    # poller); this rule is the fast path on hotplug, the monitor
    # catches anything the rule doesn't.
    install -d -m 0755 /etc/udev/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-apple-dongle.rules" \
        /etc/udev/rules.d/99-jasper-apple-dongle.rules
    udevadm control --reload-rules
    # Trigger the rule once for the currently-attached dongle so we
    # don't have to wait for the next replug. ATTR{} match is
    # idempotent: amixer setting Headphone=100% on an already-pinned
    # control is a no-op.
    udevadm trigger --action=add --subsystem-match=sound 2>/dev/null || true
    udevadm trigger --action=add --subsystem-match=usb 2>/dev/null || true

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

    # Migrate the 4 wizard services from always-on to socket-activated.
    # Older installs had jasper-X-web.service enabled directly; the new
    # topology enables the .socket instead, which pulls in the .service
    # on demand. Idempotent: re-running install.sh after migration is
    # already done is a no-op.
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web jasper-dial-web jasper-system-web; do
        if systemctl is-enabled "${unit}.service" --quiet 2>/dev/null; then
            # First time through this branch — disable the always-on
            # service. Stop it explicitly so the next request comes up
            # with the new socket-activated code rather than the
            # still-running old-process binding the port.
            systemctl stop "${unit}.service" 2>/dev/null || true
            systemctl disable "${unit}.service" 2>/dev/null || true
            echo "  migrated ${unit} to socket activation"
        fi
        systemctl enable "${unit}.socket"
        # Start the socket so it's listening immediately; the service
        # itself stays inactive until the first connection arrives.
        systemctl start "${unit}.socket" 2>/dev/null || true
    done

    systemctl enable jasper-camilla.service jasper-voice.service \
        jasper-control.service \
        jasper-dac-init.service jasper-headphone-monitor.service \
        jasper-input.service
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
    # The 5 wizard services are socket-activated now. Any currently-
    # running instance is on the old code; stop it so the next incoming
    # request brings up the new code via the .socket. Idempotent: if the
    # service is already inactive (post-idle-exit or never started), the
    # stop is a no-op.
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web jasper-dial-web jasper-system-web; do
        systemctl stop "${unit}.service" 2>/dev/null || true
    done
    # jasper-input is always-on (HID accessory bridge) — restart so any
    # already-plugged-in knob picks up new code without waiting for boot.
    systemctl restart jasper-input.service 2>/dev/null || true

    # Auto-enable software AEC when the chip is on the 6-channel
    # firmware that supports it. The bridge taps raw mic 0 (channel 2
    # of 6) — only present on the 6-ch variant; 2-ch firmware can't
    # support it and the daemon would no-op. The web wizard + voice
    # daemon diet (lazy imports, socket-activated wizards) freed up
    # ~120 MB Pss, so we can afford the ~85 MB AEC bridge by default
    # without pushing past ~770 MB on a 1 GB Pi.
    enable_aec_if_compatible
    echo
    echo "Units enabled. Start with: systemctl start jasper-camilla jasper-voice"
}

enable_aec_if_compatible() {
    # Hard precondition: chip on 6-channel firmware (the v2.0.8 6chl
    # DFU image — see BRINGUP.md Phase 2A.5). 2-channel firmware has
    # no raw-mic-0 stream for the bridge to consume.
    if ! [[ -f /proc/asound/Array/stream0 ]]; then
        echo "  AEC: ReSpeaker XVF3800 not detected. Skipping (no /proc/asound/Array)."
        return
    fi
    if ! grep -q "Channels: 6" /proc/asound/Array/stream0; then
        echo "  AEC: chip on 2-channel firmware. Leaving AEC disabled."
        echo "       Flash 6-channel (v2.0.8 6chl) via BRINGUP.md Phase 2A.5 to opt in."
        return
    fi
    # Discover the LoopbackAEC card index from arecord -l. The
    # snd-aloop two-card config loads them in deterministic order
    # (Loopback first, LoopbackAEC second), but the index depends on
    # how many other ALSA cards were enumerated before — so query
    # rather than hardcode.
    local aec_card
    aec_card=$(arecord -l 2>/dev/null \
        | awk -F'[: ]+' '/LoopbackAEC/ {print $2; exit}')
    if [[ -z "${aec_card}" ]]; then
        echo "  AEC: LoopbackAEC card not present in arecord -l."
        echo "       snd-aloop two-card setup may have failed; leaving AEC disabled."
        return
    fi
    # Respect any explicit user choice: only flip JASPER_MIC_DEVICE if
    # the user is still on the legacy "Array" default. Anything else
    # — including hw:N,1 (already on AEC) or a custom value — is left
    # alone. This makes the auto-enable safe on existing installs.
    local current
    current=$(grep -E "^JASPER_MIC_DEVICE=" "${ENV_DIR}/jasper.env" 2>/dev/null \
        | tail -1 | cut -d= -f2- | tr -d '[:space:]')
    if [[ "${current}" != "Array" ]] && [[ "${current}" != "hw:${aec_card},1" ]]; then
        echo "  AEC: user has JASPER_MIC_DEVICE=${current} — respecting existing config."
        # Still try to enable the services in case the user did the
        # env edit but not the systemctl enable. Idempotent.
        systemctl enable jasper-aec-init.service jasper-aec-bridge.service 2>/dev/null || true
        return
    fi
    echo "  AEC: 6-ch firmware detected — enabling software AEC (LoopbackAEC at hw:${aec_card},1)."
    sed -i "s|^JASPER_MIC_DEVICE=.*|JASPER_MIC_DEVICE=hw:${aec_card},1|" \
        "${ENV_DIR}/jasper.env"
    systemctl enable jasper-aec-init.service jasper-aec-bridge.service
    # aec-init is a oneshot that primes the chip's 6-ch firmware
    # config; run it now so the bridge has what it needs on its
    # first start.
    systemctl start jasper-aec-init.service 2>/dev/null || \
        echo "  WARN: jasper-aec-init failed at install time. Will retry on next boot."
    systemctl restart jasper-aec-bridge.service 2>/dev/null || \
        echo "  WARN: jasper-aec-bridge failed to start. Check logs with: journalctl -u jasper-aec-bridge -e"
    # jasper-voice needs to pick up the new JASPER_MIC_DEVICE value
    # — restart only if it's currently active (don't start a daemon
    # that the user has deliberately stopped).
    if systemctl is-active jasper-voice.service --quiet; then
        systemctl restart jasper-voice.service 2>/dev/null || true
    fi
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

install_nginx_site() {
    # Standalone nginx site that reverse-proxies /spotify/ (multi-account
    # OAuth web flow), /voice/ (voice-provider config wizard), and /dial/
    # (rotary-dial onboarding) on plain HTTP, plus /correction/ (room
    # correction wizard) and /google/ (Calendar + Gmail OAuth wizard) on
    # HTTPS. The legacy routes stay HTTP — Spotify's HTTPS requirement
    # is satisfied by the GitHub Pages bounce, and there's no point
    # breaking working flows for one new feature.
    #
    # /correction/ requires HTTPS because getUserMedia needs a secure
    # context; /google/ requires it because Google rejects non-loopback
    # OAuth redirect URIs over HTTP. Both ride the same self-signed
    # cert provisioned by provision_correction_tls() (called before
    # this from main); the user's one-time CA-install dance covers
    # both routes.
    install -m 0644 \
        "${REPO_DIR}/deploy/nginx-jasper.conf" \
        /etc/nginx/sites-enabled/jasper.conf

    # Static landing page served at /. Plain HTML, no daemon — nginx
    # reads it directly via the `location = /` block in jasper.conf.
    # Updates require an `nginx -s reload` (handled by the reload below)
    # but no service restart.
    install -d -m 0755 /usr/share/jasper-web
    install -m 0644 \
        "${REPO_DIR}/deploy/index.html" \
        /usr/share/jasper-web/index.html
    # /integrations sub-page (lists external services like Google).
    # Same static-HTML pattern as the landing page; nginx serves both
    # via exact-match `location =` blocks. Updates require an
    # `nginx -s reload` (handled below).
    install -m 0644 \
        "${REPO_DIR}/deploy/integrations.html" \
        /usr/share/jasper-web/integrations.html

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
    local arch bundle
    arch=$(uname -m)
    case "${arch}" in
        aarch64) bundle="bundle_linux_aarch64.tar.gz" ;;
        x86_64)  bundle="bundle_linux_amd64.tar.gz"   ;;
        armv7l)  bundle="bundle_linux_armv7.tar.gz"   ;;
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
        if ! curl -fsSL -o "${tmpdir}/cg.tar.gz" "${url}"; then
            echo "  WARNING: CamillaGUI download failed — skipping"
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
    systemctl daemon-reload
    systemctl enable --now camillagui.service
    echo "  CamillaGUI listening on :5005 (LAN-direct, no auth)"
}

main() {
    require_root
    install_deps
    install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
    install_camilladsp
    install_renderers
    tune_wifi_for_airplay
    install_jasper
    install_systemd_units
    install_avahi_jasper_control
    remove_legacy_https_artifacts
    provision_correction_tls   # cert files must exist before nginx -t
    install_nginx_site
    install_camillagui
    regenerate_audio_cues
}

main "$@"
