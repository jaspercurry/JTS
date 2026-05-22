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
        libasound2-plugins \
        libsndfile1 curl ca-certificates rsync \
        dfu-util \
        libwebrtc-audio-processing-dev pkg-config \
        nginx-light openssl
    # libasound2-plugins is REQUIRED for the rate_converter line in
    # deploy/alsa/asoundrc.jasper. Without it ALSA silently falls back
    # to the linear resampler which loses ~12 dB of 4-8 kHz content
    # during 44.1→48 conversion, which sabotages AEC speech-band
    # performance. See docs/HANDOFF-aec.md "Resampler quality".

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
    # has placeholders substituted by /usr/local/sbin/jasper-apply-airplay-mode:
    #   - __DISABLE_SYNCHRONIZATION__ from /var/lib/jasper/airplay_mode.env
    #   - __AIRPLAY_NAME__ from /etc/jasper/jasper.env / hostname fallback
    #   - __AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__ from the active CamillaDSP
    #     samplerate/chunksize/target_level.
    # shairport-sync.service's ExecStartPre re-renders on every restart, so
    # toggling the mode (via /airplay/ web UI or jasper-airplay-mode CLI) is
    # just an env-file write + systemctl restart shairport-sync.
    install -m 0644 \
        "${REPO_DIR}/deploy/shairport-sync.conf.template" \
        /etc/shairport-sync.conf.template
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-apply-airplay-mode" \
        /usr/local/sbin/jasper-apply-airplay-mode
    # jasper-derive-device-name maps the system hostname to a display
    # name shown in Spotify Connect / AirPlay device pickers. Called
    # by jasper-apply-airplay-mode (and by the jasper.env seeding
    # block below) as a hostname-driven default so a second/third Pi
    # on the same LAN doesn't collide with the first on Avahi. See
    # the script for the mapping (jts → JTS, jts2 → JTS-2, etc.).
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-derive-device-name" \
        /usr/local/sbin/jasper-derive-device-name
    # Default to synced: with shairport-sync.conf.template setting
    # resync_threshold_in_seconds=0.2, synced mode is glitch-free on
    # this chain (empirically verified over multiple 5-min samples
    # after the fix; see docs/HANDOFF-airplay.md). Synced is the
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
    # Four sources, in priority order:
    #   1. JASPER_DEPLOY_SHA / JASPER_DEPLOY_SHA_FULL / JASPER_DEPLOY_BRANCH
    #      env vars — set by scripts/deploy-to-pi.sh on the laptop before
    #      sudo-running install.sh. This is the only source that works
    #      when the standard rsync deploy excludes .git/ (which it does).
    #   2. Local git checkout in REPO_DIR — when install.sh is run
    #      directly against a fresh `git clone` on the Pi.
    #   3. Existing build.txt — preserve a previously-correct SHA when
    #      install.sh is re-run directly (e.g. `sudo bash install.sh`
    #      to regen the TLS cert after a hostname change) without the
    #      DEPLOY env vars. Otherwise the manifest gets clobbered back
    #      to "unknown" on every such re-run, surprising the dashboard.
    #   4. "unknown" — tarball deploys with no git info available.
    local git_sha="${JASPER_DEPLOY_SHA:-unknown}"
    local git_full="${JASPER_DEPLOY_SHA_FULL:-unknown}"
    local git_branch="${JASPER_DEPLOY_BRANCH:-unknown}"
    if [[ "${git_sha}" == "unknown" ]] && \
       { [[ -d "${REPO_DIR}/.git" ]] || git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; }; then
        git_sha=$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)
        git_full=$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)
        git_branch=$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
    fi
    if [[ "${git_sha}" == "unknown" && -f "${STATE_DIR}/build.txt" ]]; then
        local prior_sha
        prior_sha=$(grep -E '^JASPER_GIT_SHA=' "${STATE_DIR}/build.txt" | head -1 | cut -d= -f2-)
        if [[ -n "${prior_sha}" && "${prior_sha}" != "unknown" ]]; then
            git_sha="${prior_sha}"
            git_full=$(grep -E '^JASPER_GIT_SHA_FULL=' "${STATE_DIR}/build.txt" | head -1 | cut -d= -f2-)
            git_branch=$(grep -E '^JASPER_GIT_BRANCH=' "${STATE_DIR}/build.txt" | head -1 | cut -d= -f2-)
            echo "  preserving build manifest from prior install: ${git_sha} on ${git_branch}"
        fi
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

    # Curated non-bundled wake-word models. The registry lives in
    # jasper/wake_models.py (single source of truth — same data drives
    # the /wake/ picker UI). install.sh reads it via the venv's Python
    # so adding a new model is one edit in wake_models.py + a deploy.
    # Each download is idempotent (skip when the file already exists
    # with non-zero size) and best-effort: a failed download leaves
    # the daemon on the bundled "hey_jarvis" fallback rather than
    # blocking install. See README.md "Acoustic echo cancellation"
    # for the broader wake/AEC architecture.
    install -d -m 0755 -o root -g root /var/lib/jasper/wake
    # Wake-event telemetry directory (HANDOFF-wake-telemetry.md PR 3).
    # Holds wake-events.sqlite3 + per-event WAVs. jasper-voice (running
    # as root via the service unit) creates files mode 0644; future
    # /wake-review/ web UI reads via the standard nginx proxy. Owner
    # root:root mirrors the wake-models dir above.
    install -d -m 0755 -o root -g root /var/lib/jasper/wake-events
    if ! "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import os
import sys
import urllib.request

from jasper.wake_models import downloadable

failures = 0
for entry in downloadable():
    dest = entry.model
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  wake model present: {entry.key} -> {dest}")
        continue
    print(f"  downloading wake model: {entry.key}")
    print(f"    from: {entry.download_url}")
    print(f"    to:   {dest}")
    tmp = dest + ".tmp"
    try:
        urllib.request.urlretrieve(entry.download_url, tmp)
        os.replace(tmp, dest)
        os.chmod(dest, 0o644)
    except Exception as e:  # noqa: BLE001
        print(f"  failed: {e}", file=sys.stderr)
        if os.path.exists(tmp):
            os.unlink(tmp)
        failures += 1

sys.exit(1 if failures else 0)
PY
    then
        echo "  warning: one or more wake-word model downloads failed"
        echo "  the daemon will fall back to the bundled hey_jarvis model"
        echo "  re-run install.sh once you're online to retry the downloads"
    fi

    # Seed /var/lib/jasper/wake_model.env with the recommended default
    # on FIRST install only. Existing files are left alone — the /wake/
    # picker owns this file once the user has touched it, and we never
    # want to trample an explicit choice. The recommended default lives
    # in jasper/wake_models.py (DEFAULT_KEY).
    "${INSTALL_DIR}/.venv/bin/python" - <<'PY' || true
import os
import sys

from jasper.wake_models import WAKE_MODEL_FILE, default

if os.path.exists(WAKE_MODEL_FILE):
    sys.exit(0)
entry = default()
if not os.path.exists(entry.model):
    print(f"  skipping wake_model.env seed: default file missing ({entry.model})")
    sys.exit(0)
os.makedirs(os.path.dirname(WAKE_MODEL_FILE), exist_ok=True)
tmp = WAKE_MODEL_FILE + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "w") as f:
    f.write(f"JASPER_WAKE_MODEL={entry.model}\n")
os.replace(tmp, WAKE_MODEL_FILE)
print(f"  seeded {WAKE_MODEL_FILE} -> {entry.key} ({entry.model})")
PY

    if [[ ! -f "${ENV_DIR}/jasper.env" ]]; then
        # Detect ReSpeaker XVF3800 card name. Default "Array" (PiOS literal
        # name; product description matches and it's also a substring of
        # PortAudio's enumerated name "Array: USB Audio (hw:N,0)").
        # JASPER_MIC_DEVICE format is a PortAudio device name/substring,
        # NOT an ALSA pcm string — see jasper/config.py for the rationale.
        local mic_card
        mic_card=$(detect_card arecord 'xvf3800|respeaker.*array' 'Array')
        echo "  ReSpeaker mic: ${mic_card}"
        # Derive Spotify Connect / AirPlay display names from the
        # system hostname so a second/third Pi on the same LAN gets
        # a unique name out of the box (jts → JTS, jts2 → JTS-2).
        # See deploy/bin/jasper-derive-device-name for the mapping;
        # the operator can still override either var in jasper.env
        # for a custom display name like "Living Room".
        local device_name
        device_name=$("${REPO_DIR}/deploy/bin/jasper-derive-device-name")
        echo "  device name: ${device_name}"
        # Derive JASPER_HOSTNAME from the OS hostname so a fresh Pi
        # named "jts2" in Raspberry Pi Imager ends up with
        # JASPER_HOSTNAME=jts2.local — otherwise other devices on the
        # LAN type jts2.local but Spotify/AirPlay setup URLs advertise
        # the wrong name. Override path stays clean: deploy-to-pi.sh
        # exports JASPER_HOSTNAME explicitly, which wins over the
        # autodetected fallback.
        local hostname_value="${JASPER_HOSTNAME:-$(hostname).local}"
        echo "  hostname: ${hostname_value}"
        sed \
            -e "s|JASPER_MIC_DEVICE=Array|JASPER_MIC_DEVICE=${mic_card}|" \
            -e "s|^JASPER_SPOTIFY_DEVICE_NAME=.*|JASPER_SPOTIFY_DEVICE_NAME=${device_name}|" \
            -e "s|^JASPER_AIRPLAY_DEVICE_NAME=.*|JASPER_AIRPLAY_DEVICE_NAME=${device_name}|" \
            -e "s|^JASPER_HOSTNAME=.*|JASPER_HOSTNAME=${hostname_value}|" \
            "${REPO_DIR}/.env.example" > "${ENV_DIR}/jasper.env"
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo
        echo "Created ${ENV_DIR}/jasper.env from template."
        echo "Pick a voice provider at http://${hostname_value}/voice before"
        echo "starting jasper-voice — there is no default."
        echo
    fi
    migrate_voice_provider
    migrate_transit_config
}

# Migrate stale transit env vars from /etc/jasper/jasper.env into the
# wizard-owned /var/lib/jasper/transit.env, AND convert v1 schema to
# v2 schema along the way. The wizard at /transit owns every transit
# env variable; operators who pasted those into jasper.env on an older
# install (pre-wizard) get them moved automatically.
#
# v1 → v2 schema deltas:
#  - JASPER_BUS_STOP_ID (singular) → JASPER_BUS_STOPS (comma list with
#    optional embedded labels). Migration carries the single ID into a
#    one-element list with no label.
#  - JASPER_BUS_ROUTES (positive route filter) → removed. v2 has no
#    per-tool global route filter; the user picks direction-specific
#    stops which are already route-shaped.
#  - JASPER_SUBWAY_LINES (positive line filter) → removed. v2 returns
#    every line at the station by default, surfacing reroutes too.
#
# Idempotent. Safe on fresh installs (no-op) and on long-lived ones
# (sweeps any residue).
migrate_transit_config() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/transit.env"

    # Keys preserved by v2 — moved into the wizard file as-is.
    local keep_keys=(
        JASPER_SUBWAY_STATION_ID
        JASPER_SUBWAY_DEFAULT_DIRECTION
        JASPER_MTA_BUSTIME_KEY
    )
    # Keys removed in v2 — swept from both files.
    local drop_keys=(
        JASPER_SUBWAY_LINES
        JASPER_BUS_ROUTES
    )

    [[ -f "${jasper_env}" ]] || true

    install -d -m 0750 "${STATE_DIR}"

    # Move preserved keys.
    local k line stale_value
    for k in "${keep_keys[@]}"; do
        [[ -f "${jasper_env}" ]] || break
        line=$(grep -E "^${k}=" "${jasper_env}" || true)
        [[ -z "${line}" ]] && continue
        stale_value="${line#${k}=}"
        stale_value="${stale_value%[$'\r\n ']*}"

        if [[ -f "${wizard_env}" ]] && grep -qE "^${k}=" "${wizard_env}"; then
            sed -i.bak "/^${k}=/d" "${jasper_env}"
            rm -f "${jasper_env}.bak"
            echo "  migrate_transit_config: removed stale ${k} line from ${jasper_env}"
            continue
        fi

        if [[ -n "${stale_value}" ]]; then
            touch "${wizard_env}"
            chmod 0640 "${wizard_env}"
            echo "${k}=${stale_value}" >> "${wizard_env}"
            echo "  migrate_transit_config: moved ${k}=${stale_value}"
            echo "    from ${jasper_env} to ${wizard_env}"
        fi
        sed -i.bak "/^${k}=/d" "${jasper_env}"
        rm -f "${jasper_env}.bak"
    done

    # Drop deprecated v1 keys from both files.
    for k in "${drop_keys[@]}"; do
        if [[ -f "${jasper_env}" ]] && grep -qE "^${k}=" "${jasper_env}"; then
            sed -i.bak "/^${k}=/d" "${jasper_env}"
            rm -f "${jasper_env}.bak"
            echo "  migrate_transit_config: dropped v1 ${k} from ${jasper_env}"
        fi
        if [[ -f "${wizard_env}" ]] && grep -qE "^${k}=" "${wizard_env}"; then
            sed -i.bak "/^${k}=/d" "${wizard_env}"
            rm -f "${wizard_env}.bak"
            echo "  migrate_transit_config: dropped v1 ${k} from ${wizard_env}"
        fi
    done

    # v1 → v2 bus stop schema: singular JASPER_BUS_STOP_ID becomes a
    # one-element JASPER_BUS_STOPS list. Migrates in either file.
    local stop_file
    for stop_file in "${jasper_env}" "${wizard_env}"; do
        [[ -f "${stop_file}" ]] || continue
        line=$(grep -E "^JASPER_BUS_STOP_ID=" "${stop_file}" || true)
        [[ -z "${line}" ]] && continue
        stale_value="${line#JASPER_BUS_STOP_ID=}"
        stale_value="${stale_value%[$'\r\n ']*}"

        if [[ -n "${stale_value}" ]]; then
            # Only write the new list if no JASPER_BUS_STOPS already exists
            # in either file (avoid stomping a wizard-written value).
            if ! { [[ -f "${wizard_env}" ]] && grep -qE "^JASPER_BUS_STOPS=" "${wizard_env}"; }; then
                touch "${wizard_env}"
                chmod 0640 "${wizard_env}"
                echo "JASPER_BUS_STOPS=${stale_value}" >> "${wizard_env}"
                echo "  migrate_transit_config: converted JASPER_BUS_STOP_ID=${stale_value}"
                echo "    → JASPER_BUS_STOPS in ${wizard_env}"
            fi
        fi
        sed -i.bak "/^JASPER_BUS_STOP_ID=/d" "${stop_file}"
        rm -f "${stop_file}.bak"
    done
}

# Migrate stale JASPER_VOICE_PROVIDER from /etc/jasper/jasper.env to
# /var/lib/jasper/voice_provider.env. The wizard at /voice owns this
# variable; previously the install template also set a default
# (JASPER_VOICE_PROVIDER=gemini), which created stale-vs-runtime
# confusion when the wizard had written a different value.
#
# This function:
#  - reads any JASPER_VOICE_PROVIDER= line out of /etc/jasper/jasper.env
#  - if the wizard file (/var/lib/jasper/voice_provider.env) doesn't
#    already define the variable, moves the value there
#  - removes the line from /etc/jasper/jasper.env either way
#
# Idempotent: running multiple times produces the same end state.
# Safe on fresh installs (where neither file has the var, this is a
# no-op) and on long-lived installs (where the wizard file already
# has the var, this just cleans up the stale line).
migrate_voice_provider() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/voice_provider.env"

    [[ -f "${jasper_env}" ]] || return 0
    local line
    line=$(grep -E '^JASPER_VOICE_PROVIDER=' "${jasper_env}" || true)
    [[ -z "${line}" ]] && return 0

    # value is everything after the first '='. Trim trailing CR/whitespace.
    local stale_value="${line#JASPER_VOICE_PROVIDER=}"
    stale_value="${stale_value%[$'\r\n ']*}"

    install -d -m 0750 "${STATE_DIR}"

    # If wizard file already declares the variable, just remove the
    # stale jasper.env line — the wizard's value wins per systemd's
    # EnvironmentFile load order regardless, this is just cleanup.
    if [[ -f "${wizard_env}" ]] && grep -qE '^JASPER_VOICE_PROVIDER=' "${wizard_env}"; then
        sed -i.bak '/^JASPER_VOICE_PROVIDER=/d' "${jasper_env}"
        rm -f "${jasper_env}.bak"
        echo "  migrate_voice_provider: removed stale JASPER_VOICE_PROVIDER"
        echo "    line from ${jasper_env} (wizard file already canonical)"
        return 0
    fi

    # Migrate the value to the wizard file. Empty stale value (we just
    # introduced this on a clean install) → don't write anything, just
    # remove the line from jasper.env. Non-empty → preserve the
    # operator's pre-cleanup choice so voice keeps working.
    if [[ -n "${stale_value}" ]]; then
        touch "${wizard_env}"
        chmod 0640 "${wizard_env}"
        echo "JASPER_VOICE_PROVIDER=${stale_value}" >> "${wizard_env}"
        echo "  migrate_voice_provider: moved JASPER_VOICE_PROVIDER=${stale_value}"
        echo "    from ${jasper_env} to ${wizard_env}"
    fi
    sed -i.bak '/^JASPER_VOICE_PROVIDER=/d' "${jasper_env}"
    rm -f "${jasper_env}.bak"
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
    # AEC bridge + boot-time chip init + reconciler. The reconciler is
    # the policy layer that keeps JASPER_MIC_DEVICE, AEC services, and
    # the currently attached mic hardware in sync.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-bridge.service" \
        "${SYSTEMD_DIR}/jasper-aec-bridge.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-init.service" \
        "${SYSTEMD_DIR}/jasper-aec-init.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-aec-reconcile.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-aec-reconcile" \
        /usr/local/sbin/jasper-aec-reconcile
    # Triggered by the udev rule installed below when the Apple dongle
    # re-enumerates: reset-failed, restart Camilla, then run the
    # mic/AEC reconciler so a hardware reconnect recovers without
    # manual intervention. See docs/HANDOFF-resilience.md.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-dongle-recover.service" \
        "${SYSTEMD_DIR}/jasper-dongle-recover.service"
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
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-aec-reconcile.rules" \
        /etc/udev/rules.d/99-jasper-aec-reconcile.rules
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
        # Restart (not just start) so a deploy that adds or moves a
        # ListenStream= port — e.g. a new wizard sharing this socket
        # like /sources/ on 8773 — actually re-binds. A bare `start`
        # is a no-op when the socket is already active and silently
        # leaves the old port set live; nginx then 502s on the new
        # route until the next reboot. Restart cascades through the
        # Requires=.socket service too, which the later wizard-stop
        # loop also covers, so the order is safe.
        systemctl restart "${unit}.socket" 2>/dev/null || true
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

    # Reconcile software AEC against whatever mic hardware is actually
    # present right now. This replaces the old one-way "enable if
    # Array is 6-ch" install step: if a previous install left voice on
    # udp:9876 but the Array is currently absent, reconcile actively
    # clears that stale state and parks voice instead of letting it
    # watchdog-loop on an unfed UDP socket.
    reconcile_aec_state
    echo
    echo "Units enabled. Start with: systemctl start jasper-camilla jasper-voice"
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
    install -d -m 0755 "${STATE_DIR}"
    if [[ ! -f "${STATE_DIR}/aec_mode.env" ]]; then
        printf 'JASPER_AEC_MODE=auto\n' > "${STATE_DIR}/aec_mode.env"
        chmod 0644 "${STATE_DIR}/aec_mode.env"
    fi
    systemctl enable jasper-aec-reconcile.service
    /usr/local/sbin/jasper-aec-reconcile --reason install || \
        echo "  WARN: AEC/mic reconcile failed. Check logs with: journalctl -u jasper-aec-reconcile -e"
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

install_peering_template() {
    # Multi-device peering. The TEMPLATE goes under /etc/jasper/ so
    # Avahi doesn't try to parse it as a service file (the
    # placeholders __PEER_ID__ / __ROOM__ / __PRIMARY__ aren't valid
    # XML attribute values).
    #
    # jasper-control's peering daemon renders this template into
    # /etc/avahi/services/jasper-peer.service when JASPER_PEERING=on
    # is set in /var/lib/jasper/peering.env (via the /peers/ web
    # wizard, in PR 2). When peering is off (the default), no
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
    install -d -m 0755 /var/lib/jasper
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
    echo "  Peering template installed; peering is OFF by default — enable at http://${JASPER_HOSTNAME:-jts.local}/peers/"
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
    require_root
    install_deps
    install_alsa  # exports DONGLE_CARD; must run before install_camilladsp
    install_camilladsp
    install_renderers
    tune_wifi_for_airplay
    install_jasper
    install_systemd_units
    install_journald_persistent_storage
    install_avahi_jasper_control
    install_peering_template
    remove_legacy_https_artifacts
    provision_correction_tls   # cert files must exist before nginx -t
    install_nginx_site
    install_camillagui
    regenerate_audio_cues
    run_doctor_summary
}

main "$@"
