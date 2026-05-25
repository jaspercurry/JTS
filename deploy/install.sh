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
        meson ninja-build \
        nginx-light openssl \
        rustc cargo
    # rustc + cargo are required to build the jasper-fanin Rust daemon
    # (rust/jasper-fanin/). Trixie ships rustc 1.85, comfortably above
    # our crate's rust-version=1.75 floor. See
    # docs/HANDOFF-fan-in-daemon.md "Why Rust" for the language choice.
    # meson + ninja-build are needed by build_webrtc_v2_for_aec3() to
    # compile webrtc-audio-processing v2.1 statically from source. The
    # resulting static archive is what jasper_aec3/setup.py links the
    # `_aec3_v2` binding against (BEST_A AEC config). See
    # docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture plan".
    # libasound2-plugins is REQUIRED for the rate_converter line in
    # deploy/alsa/asoundrc.jasper. Without it ALSA silently falls back
    # to the linear resampler which loses ~12 dB of 4-8 kHz content
    # during 44.1→48 conversion, which sabotages AEC speech-band
    # performance. See docs/HANDOFF-aec.md "Resampler quality".

    # Source-build deps for shairport-sync (AirPlay 2) + nqptp, plus
    # the bluez-alsa userspace and the bt-agent helper. All of these
    # are absent on a stock Trixie Lite image.
    #
    # `avahi-daemon` is the mDNS *publisher* — Pi OS Lite ships
    # `libnss-mdns` (resolution only) by default but does NOT install
    # the daemon, so without this line `<hostname>.local` from another
    # device fails to find us, `_jasper-control._tcp` isn't advertised
    # to the dial, and `avahi-utils` tools have no daemon to talk to.
    # `avahi-utils` provides avahi-browse / avahi-publish for diagnostics.
    apt-get install -y --no-install-recommends \
        autoconf automake libtool pkg-config \
        libpopt-dev libconfig-dev libavahi-client-dev \
        libssl-dev libsoxr-dev libplist-dev libsodium-dev \
        libgcrypt20-dev uuid-dev libmbedtls-dev libglib2.0-dev \
        libavutil-dev libavcodec-dev libavformat-dev libswresample-dev \
        xxd \
        bluez-alsa-utils bluez-tools avahi-daemon avahi-utils
}

build_webrtc_v2_for_aec3() {
    # Build webrtc-audio-processing v2.1 statically into
    # /opt/jasper/.cache/webrtc-aec3-v2/src/builddir/, then export
    # JASPER_WEBRTC_V2_PREFIX for the caller. jasper_aec3/setup.py
    # reads this env var (as WEBRTC_AEC3_V2_PREFIX) and links its
    # `_aec3_v2` binding against the static archive.
    #
    # Why v2.1 vendored + static: Debian Trixie's apt
    # libwebrtc-audio-processing-1 v1.3-3 doesn't expose
    # EchoCanceller3Factory in its public headers, so the deep
    # suppressor / ERLE / stationarity knobs that BEST_A relies on
    # are unreachable. Mirroring PipeWire 1.4's pattern, we vendor
    # v2.1 from the upstream pulseaudio fork and link statically —
    # we own both sides of the ABI boundary; no Debian-rebuild risk.
    # See HANDOFF-aec.md section E + experiments/aec3-v2-deep-tune-spike/.
    #
    # First-run cost: ~3-5 min on Pi 5. Re-runs are no-ops thanks to
    # the static-archive existence check.
    local cache_dir="/opt/jasper/.cache/webrtc-aec3-v2"
    local src_dir="${cache_dir}/src"
    local build_dir="${src_dir}/builddir"
    local static_archive="${build_dir}/webrtc/modules/audio_processing/libwebrtc-audio-processing-2.a"
    local repo_url="https://gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing.git"
    local repo_tag="v2.1"

    if [[ -f "${static_archive}" ]]; then
        echo "  webrtc-audio-processing v2.1 already built at ${static_archive}"
        export JASPER_WEBRTC_V2_PREFIX="${cache_dir}"
        return 0
    fi

    echo "  building webrtc-audio-processing ${repo_tag} statically (first run, ~3-5 min)..."
    mkdir -p "${cache_dir}"

    if [[ ! -d "${src_dir}/.git" ]]; then
        echo "    cloning ${repo_url}#${repo_tag} → ${src_dir}"
        git clone --depth 1 --branch "${repo_tag}" "${repo_url}" "${src_dir}"
    else
        echo "    source tree present; reusing"
    fi

    if [[ ! -f "${build_dir}/build.ninja" ]]; then
        echo "    meson setup builddir/"
        (cd "${src_dir}" && meson setup builddir \
            -Ddefault_library=static \
            -Db_pie=true \
            -Dc_args=-fPIC \
            -Dcpp_args=-fPIC \
            --buildtype=release)
    fi

    echo "    meson compile -C builddir/"
    (cd "${src_dir}" && meson compile -C builddir)

    if [[ ! -f "${static_archive}" ]]; then
        echo "  ERROR: meson compile finished but ${static_archive} is missing" >&2
        echo "  WEBRTC_AEC3_V2_PREFIX will not be set; setup.py will skip _aec3_v2" >&2
        return 1
    fi

    echo "  → static archive: ${static_archive} ($(du -h "${static_archive}" | cut -f1))"
    export JASPER_WEBRTC_V2_PREFIX="${cache_dir}"
}

build_install_jasper_fanin() {
    # Build the jasper-fanin Rust daemon (rust/jasper-fanin/) and
    # install the release binary to /opt/jasper/bin/jasper-fanin.
    #
    # See docs/HANDOFF-fan-in-daemon.md for the daemon's design,
    # resilience contract, and 4-phase migration plan. As of Phase
    # 2 chunk 4 (this commit), the daemon is INSTALLED but NOT
    # ENABLED — the dmix-based renderer path is still the active
    # topology. Phase 3 (a future PR) flips the JASPER_AUDIO_TOPOLOGY
    # feature flag to opt-in operators onto the fanin path.
    #
    # Build is done as the `pi` user in a persistent cache dir at
    # /var/cache/jasper-fanin-build so cargo's incremental compilation
    # keeps re-runs fast (~5 s on no source change, ~20 s incremental,
    # ~90 s first run). Target/ directory stays in the cache; only
    # the release binary is copied to /opt/jasper/bin.
    local src_dir="${REPO_DIR}/rust/jasper-fanin"
    local cache_dir="/var/cache/jasper-fanin-build"
    local bin_dest="/opt/jasper/bin/jasper-fanin"

    if [[ ! -d "${src_dir}" ]]; then
        echo "  jasper-fanin source missing at ${src_dir}; skipping build"
        return 0
    fi

    echo "  building jasper-fanin (Rust daemon)..."
    mkdir -p "${cache_dir}"
    chown pi:pi "${cache_dir}"

    # rsync the source tree into the cache dir, preserving cargo's
    # incremental compile state in target/ between runs. --delete
    # removes stale source files (e.g., a renamed module).
    rsync -a --delete \
        --exclude='target/' \
        "${src_dir}/" "${cache_dir}/"
    chown -R pi:pi "${cache_dir}"

    # Build as pi so cargo's user cache (~pi/.cargo) is used and the
    # generated artifacts under target/ are pi-owned (operator can
    # clean up without sudo).
    sudo -u pi -H bash -c "cd '${cache_dir}' && cargo build --release --quiet" \
        || { echo "  jasper-fanin build failed; see cargo output above"; return 1; }

    local built_bin="${cache_dir}/target/release/jasper-fanin"
    if [[ ! -x "${built_bin}" ]]; then
        echo "  ERROR: cargo build finished but ${built_bin} is missing" >&2
        return 1
    fi

    mkdir -p /opt/jasper/bin
    install -m 0755 -o root -g root "${built_bin}" "${bin_dest}"
    echo "  → installed ${bin_dest} ($(du -h "${bin_dest}" | cut -f1))"
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

    # /etc/asound.conf provides the system-wide ALSA PCM definitions
    # (jasper_out, jasper_capture, jasper_renderer_in, etc.).
    #
    # Location matters: this file MUST be world-readable so that
    # renderer processes running as non-root users (shairport-sync as
    # `shairport-sync`, librespot as `pi`) can resolve the user-space
    # PCM names declared in it. The pre-2026-05-23 location
    # (/root/.asoundrc, mode 0600) was visible only to root, which
    # was fine while renderers wrote to plughw:Loopback,0,0 (a
    # kernel-built-in name needing no asoundrc to resolve) but broke
    # AirPlay and Spotify Connect after PR #214 switched them to
    # user-space PCM names. /etc/asound.conf at mode 0644 is the
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
    # see `jasper_renderer_in` and skip the backup (no .pre-jasper
    # spam). Symlinks are skipped (some setups symlink /etc/asound.conf
    # to /etc/alsa/asound.conf; back-up-then-overwrite would still
    # mutate the target).
    if [[ -f /etc/asound.conf && ! -L /etc/asound.conf ]] \
            && ! grep -q "jasper_renderer_in" /etc/asound.conf 2>/dev/null; then
        cp /etc/asound.conf "/etc/asound.conf.pre-jasper.$(date +%s)"
        echo "  Backed up pre-existing /etc/asound.conf (.pre-jasper.*); see PR #223."
    fi
    sed -e "s/__DONGLE_CARD__/${DONGLE_CARD}/g" \
        "${REPO_DIR}/deploy/alsa/asoundrc.jasper" \
        > /etc/asound.conf
    chmod 0644 /etc/asound.conf
    echo "  Wrote /etc/asound.conf with jasper_renderer_in + jasper_out"
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

set_usb_gadget_mode() {
    # Add the dtoverlay that puts the Pi 5's BCM2712 OTG controller
    # (the USB-C port) into peripheral mode so it can present as a USB
    # gadget to a connected host. This is the precondition for the
    # jasper-usbsink feature — a fourth music source where a computer
    # plugged into the Pi via the 8086 splitter sees JTS as a USB audio
    # output device.
    #
    # We only set the dtoverlay. We do NOT load libcomposite at boot,
    # auto-create the gadget descriptor, or enable jasper-usbsink. All
    # of that is gated behind the /sources/ wizard toggle so RAM stays
    # at baseline (~50 KB dwc2 kernel module) when the feature is off.
    #
    # Requires a reboot to take effect — the dwc2 module is loaded by
    # the kernel via the dtoverlay at boot. Subsequent runs of
    # install.sh are no-ops once the line is present.
    #
    # Side effect to document: the Pi 5 USB-C port is no longer
    # available for plugging USB host devices (e.g. flash drives).
    # The four USB-A ports remain in host mode unchanged.
    local cfg="/boot/firmware/config.txt"
    if [[ ! -f "$cfg" ]]; then
        echo "  $cfg not present; skipping USB gadget dtoverlay."
        return 0
    fi
    if grep -qE '^[[:space:]]*dtoverlay=dwc2,dr_mode=peripheral' "$cfg"; then
        echo "  USB gadget dtoverlay already present in $cfg."
        return 0
    fi
    # Prefer to append under an existing [pi5] section so the override
    # is cleanly scoped to Pi 5. If [pi5] isn't present, append a
    # fresh tagged block at the end.
    if grep -qE '^\[pi5\]' "$cfg"; then
        # GNU sed: insert after the [pi5] line.
        sed -i '/^\[pi5\]/a dtoverlay=dwc2,dr_mode=peripheral' "$cfg"
    else
        cat >> "$cfg" <<'EOF'

# JTS install — required for jasper-usbsink (USB audio gadget source).
# Puts the BCM2712 OTG controller into peripheral mode so a connected
# host can see JTS as a USB audio output device. libcomposite is NOT
# loaded at boot; the jasper-usbsink-init.service modprobes it on
# demand, so RAM stays at baseline when the USB sink is disabled.
# Reboot required to take effect. See docs/HANDOFF-usbsink.md.
[pi5]
dtoverlay=dwc2,dr_mode=peripheral
EOF
    fi
    echo "  USB gadget dtoverlay added to $cfg (reboot required to apply)."
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

# Rebuild a satellite firmware .bin from source if (a) it's missing,
# or (b) any source file is newer than the staged .bin AND
# PlatformIO is already installed on this Pi. We intentionally do NOT
# auto-install PIO: a fresh install pulls ~300-500 MB of ESP32-S3
# toolchain on first build, and most JTS households don't have any
# satellite devices. The /dial/ wizard surfaces a copy-paste install
# command for households that do.
#
# PIO can live in any of three places (mirrors build.sh's resolution
# order): on PATH, at /opt/jasper/.venv/bin/pio (the wizard's install
# target), or at /home/pi/.platformio/penv/bin/pio (PIO's own
# installer-script default). We accept any of them.
#
# Build runs as the pi user when /home/pi/.platformio exists, so the
# toolchain cache lands in one place and root doesn't end up with a
# duplicate copy. Otherwise we run as whoever invoked install.sh.
#
# Soft-fails: a failed build prints a warning and lets install.sh
# continue — the wizard surfaces the stale-bin state to the user.
_build_firmware_if_stale() {
    local fw_dir="$1"
    local bin_name="$2"
    local fw_root="${INSTALL_DIR}/firmware/${fw_dir}"
    local bin_path="${fw_root}/${bin_name}"
    local src_dir="${fw_root}/src"
    local build_script="${fw_root}/build.sh"

    [[ -f "$build_script" ]] || return 0
    [[ -d "$src_dir" ]] || return 0

    local need_build=0
    if [[ ! -f "$bin_path" ]]; then
        need_build=1
    elif [[ -n "$(find "$src_dir" -type f -newer "$bin_path" 2>/dev/null | head -1)" ]]; then
        need_build=1
    fi
    [[ $need_build -eq 1 ]] || return 0

    # Detect PIO anywhere build.sh would find it.
    local pio_found=0
    if command -v pio >/dev/null 2>&1 \
       || [[ -x "/opt/jasper/.venv/bin/pio" ]] \
       || [[ -x "/home/pi/.platformio/penv/bin/pio" ]]; then
        pio_found=1
    fi

    if [[ $pio_found -ne 1 ]]; then
        echo "==> ${fw_dir} firmware: source newer than staged .bin, but"
        echo "    PlatformIO is not installed on this Pi. The wizard will"
        echo "    skip flashing for ${fw_dir} until PIO is available."
        echo "    To enable, run once on the Pi:"
        echo "      sudo /opt/jasper/.venv/bin/pip install platformio"
        echo "    Then re-run deploy/install.sh."
        return 0
    fi

    # Pick the build user. Prefer pi when pi has any PIO state, so a
    # populated toolchain cache gets reused on each rebuild.
    local build_user build_cmd
    if [[ -d "/home/pi/.platformio" ]]; then
        build_user="pi"
        build_cmd="sudo -u pi -H bash ${build_script}"
    else
        build_user="$(id -un)"
        build_cmd="bash ${build_script}"
    fi

    echo "==> ${fw_dir} firmware: building as ${build_user} (~30 s incremental, ~5 min first run)"
    if eval "$build_cmd"; then
        echo "    Staged ${bin_path}"
    else
        echo "==> ${fw_dir} firmware: build FAILED — wizard will skip flash until next deploy"
    fi
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
    #
    # NO --delete: build.sh writes each .bin INTO ${INSTALL_DIR}/firmware/
    # (not into the source repo), so --delete would silently remove the
    # staged .bin on every deploy. Verified failure mode: the /dial/
    # wizard's "Force flash" silently skipped flashing after re-deploy
    # because jasper-dial-onboard saw no bin and fell through to its
    # creds-only path. Instead we (a) leave any locally-staged .bin in
    # place, and (b) rebuild from source via _build_firmware_if_stale
    # when the source is newer (PIO required; soft-fails without).
    if [[ -d "${REPO_DIR}/firmware" ]]; then
        rsync -a \
            --exclude='.pio' --exclude='.pioenvs' --exclude='.piolibdeps' \
            "${REPO_DIR}/firmware" "${INSTALL_DIR}/"

        _build_firmware_if_stale "dial" "jasper-dial.bin"
        _build_firmware_if_stale "satellite-amoled" "jasper-satellite-amoled.bin"
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

    # jasper_aec3 — pybind11 bindings for WebRTC AEC3. Two engines:
    #   - _aec3      → links against Debian Trixie's apt-installed
    #                  libwebrtc-audio-processing-1 (v1.3-3). Legacy
    #                  fallback engine.
    #   - _aec3_v2   → links statically against vendored
    #                  webrtc-audio-processing v2.1 (built by
    #                  build_webrtc_v2_for_aec3 below). Exposes the
    #                  deep EchoCanceller3Config knobs the v1
    #                  binding can't reach — required for the BEST_A
    #                  config. Built conditionally when the vendored
    #                  static archive exists.
    # See docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture
    # plan" and experiments/aec3-v2-deep-tune-spike/README.md for
    # the BEST_A canonical config + per-knob rationale.
    if [[ -d "${INSTALL_DIR}/jasper_aec3" ]]; then
        # Build vendored v2.1 first (cached after first run); exports
        # WEBRTC_AEC3_V2_PREFIX into the env that setup.py reads.
        build_webrtc_v2_for_aec3

        # Fingerprint-cache the C++ rebuild: skip pip install when
        # nothing the binding depends on has changed.
        #
        # Why this exists: --force-reinstall (kept below) forces a
        # full pip-side rebuild whose --no-cache-defeat is the only
        # way to guarantee setup.py sees WEBRTC_AEC3_V2_PREFIX (pip's
        # wheel cache doesn't key on env vars). But the actual C++
        # compile of aec3_binding_v2.cpp at -O3 takes 1-3 min on Pi 5
        # with ~430 MB peak RAM on cc1plus — wasteful on the ~80%
        # of deploys that don't touch jasper_aec3/.
        #
        # Fingerprint inputs (any change → rebuild):
        #   - mtime + name of every .cpp/.h/.py/pyproject.toml in
        #     jasper_aec3/
        #   - mtime of the vendored libwebrtc-audio-processing-2.a
        #     (rebuilt rarely by build_webrtc_v2_for_aec3)
        #   - Python version (ABI break → rebuild)
        #   - WEBRTC_AEC3_V2_PREFIX value (cache path change → rebuild)
        #
        # Defense-in-depth: even on cache hit, verify the module
        # imports cleanly — catches accidentally-deleted .so files
        # or partial installs between deploys.
        #
        # Escape hatch: `sudo rm /opt/jasper/.cache/jasper_aec3.installed.fingerprint`
        # then re-deploy → unconditional rebuild.
        local marker="${INSTALL_DIR}/.cache/jasper_aec3.installed.fingerprint"
        local fingerprint
        fingerprint=$(
            (
                find "${INSTALL_DIR}/jasper_aec3" -type f \
                    \( -name '*.cpp' -o -name '*.h' \
                       -o -name '*.py' -o -name 'pyproject.toml' \
                       -o -name 'setup.py' -o -name 'setup.cfg' \) \
                    -exec stat -c '%Y %n' {} \; 2>/dev/null | sort
                # Vendored static archive — null if build_webrtc_v2_for_aec3
                # didn't set the prefix, which means we'd be building
                # the v1-only binding (still want to fingerprint that).
                if [[ -n "${JASPER_WEBRTC_V2_PREFIX:-}" ]]; then
                    find "${JASPER_WEBRTC_V2_PREFIX}" -name 'libwebrtc-audio-processing-2.a' \
                        -exec stat -c '%Y %n' {} \; 2>/dev/null
                fi
                "${INSTALL_DIR}/.venv/bin/python" --version 2>&1
                echo "WEBRTC_PREFIX=${JASPER_WEBRTC_V2_PREFIX:-}"
            ) | sha256sum | awk '{print $1}'
        )

        local needs_rebuild=1
        if [[ -f "${marker}" ]] \
           && [[ "$(cat "${marker}")" == "${fingerprint}" ]] \
           && "${INSTALL_DIR}/.venv/bin/python" -c "import jasper_aec3" 2>/dev/null; then
            echo "==> jasper_aec3 source + env unchanged, skipping rebuild"
            echo "    (delete ${marker} to force)"
            needs_rebuild=0
        fi

        if [[ "${needs_rebuild}" == "1" ]]; then
            # --force-reinstall: pip wheel cache only keys on source hash
            # + setuptools metadata, not on env vars. Without --force-reinstall,
            # a previously-cached wheel built without WEBRTC_AEC3_V2_PREFIX
            # (i.e. with only the v1 extension) would be reused even after
            # the vendored v2 build completes. Forcing a rebuild is the
            # simplest way to guarantee setup.py sees the env var and builds
            # both extensions.
            WEBRTC_AEC3_V2_PREFIX="${JASPER_WEBRTC_V2_PREFIX:-}" \
                "${INSTALL_DIR}/.venv/bin/pip" install --force-reinstall --no-deps \
                "${INSTALL_DIR}/jasper_aec3"
            mkdir -p "$(dirname "${marker}")"
            echo "${fingerprint}" > "${marker}"
        fi
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

    # DTLN-aec ONNX model bundle for the triple-stream wake architecture.
    # Registry at jasper/aec_engines/dtln_models.py — same idempotency
    # + best-effort shape as the wake-model download above. The bridge's
    # _select_engine() runs with DTLN disabled when these files are
    # missing, so a failed download degrades gracefully (triple-stream
    # falls back to dual-stream: AEC ON + AEC OFF). Files are hash-
    # verified post-download so a corrupted partial fetch (truncated
    # ONNX = cryptic onnxruntime error at engine init) is caught here.
    install -d -m 0755 -o root -g root /var/lib/jasper/dtln
    if ! "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import hashlib
import os
import sys
import urllib.request

from jasper.aec_engines.dtln_models import REGISTRY, DTLN_MODELS_DIR

def sha256_file(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

failures = 0
for entry in REGISTRY:
    for path, url, expected_sha in entry.files(DTLN_MODELS_DIR):
        dest = str(path)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            if sha256_file(dest) == expected_sha:
                print(f"  dtln model present: {path.name}")
                continue
            print(f"  dtln model hash mismatch, re-downloading: {path.name}")
            os.unlink(dest)
        print(f"  downloading dtln model: {path.name}")
        print(f"    from: {url}")
        tmp = dest + ".tmp"
        try:
            urllib.request.urlretrieve(url, tmp)
            got = sha256_file(tmp)
            if got != expected_sha:
                print(f"  hash mismatch after download: got {got}, "
                      f"expected {expected_sha}", file=sys.stderr)
                os.unlink(tmp)
                failures += 1
                continue
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
        echo "  warning: one or more DTLN model downloads failed"
        echo "  the bridge will fall back to dual-stream (AEC ON + AEC OFF only)"
        echo
        echo "  Anonymous download from the release returns 404 while the"
        echo "  jaspercurry/JTS repo is still private. Manual install:"
        echo "    gh release download dtln-models-v1 --repo jaspercurry/JTS \\"
        echo "        --dir /var/lib/jasper/dtln"
        echo "    sudo systemctl restart jasper-aec-bridge"
        echo "  (one-time; once the repo goes public install.sh handles it)"
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
    migrate_wifi_guardian
    migrate_wake_legs_config
}

# Migrate hand-set wake-detection leg env vars from
# /etc/jasper/jasper.env into the wizard-owned
# /var/lib/jasper/aec_mode.env. The /system "Wake detection" card
# owns these as booleans (JASPER_WAKE_LEG_RAW, _DTLN); the
# reconciler maps them back to the underlying device/enable vars
# the bridge + voice each read at startup.
#
# Previously AGENTS.md instructed operators to paste raw lines into
# /etc/jasper/jasper.env for opt-in legs:
#   JASPER_MIC_DEVICE_RAW=udp:9877        (dual-stream)
#   JASPER_MIC_DEVICE_DTLN=udp:9878       (triple-stream extras)
#   JASPER_AEC_DTLN_ENABLED=1
# This function preserves an operator's prior intent on upgrade by
# translating those values into the new boolean form, then strips
# the underlying vars so the reconciler is the only writer going
# forward. Fresh installs (no underlying vars set) are a no-op here
# — the new defaults seeded in reconcile_aec_state take effect
# (RAW=1, DTLN=0).
#
# Idempotent — already-translated installs find nothing to migrate.
migrate_wake_legs_config() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/aec_mode.env"

    [[ -f "${jasper_env}" ]] || return 0

    local raw_line dtln_line dtln_enabled_line
    raw_line=$(grep -E '^JASPER_MIC_DEVICE_RAW=' "${jasper_env}" || true)
    dtln_line=$(grep -E '^JASPER_MIC_DEVICE_DTLN=' "${jasper_env}" || true)
    dtln_enabled_line=$(grep -E '^JASPER_AEC_DTLN_ENABLED=' "${jasper_env}" || true)

    if [[ -z "${raw_line}${dtln_line}${dtln_enabled_line}" ]]; then
        return 0
    fi

    install -d -m 0755 "${STATE_DIR}"

    local raw_value dtln_value dtln_enabled_value
    raw_value="${raw_line#JASPER_MIC_DEVICE_RAW=}"
    raw_value="${raw_value%[$'\r\n ']*}"
    dtln_value="${dtln_line#JASPER_MIC_DEVICE_DTLN=}"
    dtln_value="${dtln_value%[$'\r\n ']*}"
    dtln_enabled_value="${dtln_enabled_line#JASPER_AEC_DTLN_ENABLED=}"
    dtln_enabled_value="${dtln_enabled_value%[$'\r\n ']*}"

    # An operator running the dual-stream setup had RAW set to a
    # udp:* device. Empty value means they had explicitly cleared
    # it — treat as off so we don't silently turn things on.
    local want_raw="0"
    [[ -n "${raw_value}" ]] && want_raw="1"

    # An operator running DTLN had both MIC_DEVICE_DTLN and
    # AEC_DTLN_ENABLED=1. Either alone is enough signal to preserve.
    local want_dtln="0"
    if [[ -n "${dtln_value}" || "${dtln_enabled_value}" == "1" ]]; then
        want_dtln="1"
    fi

    touch "${wizard_env}"
    chmod 0644 "${wizard_env}"

    if ! grep -qE '^JASPER_WAKE_LEG_RAW=' "${wizard_env}"; then
        echo "JASPER_WAKE_LEG_RAW=${want_raw}" >> "${wizard_env}"
        echo "  migrate_wake_legs_config: set JASPER_WAKE_LEG_RAW=${want_raw}"
        echo "    from prior JASPER_MIC_DEVICE_RAW=${raw_value:-<unset>}"
    fi
    if ! grep -qE '^JASPER_WAKE_LEG_DTLN=' "${wizard_env}"; then
        echo "JASPER_WAKE_LEG_DTLN=${want_dtln}" >> "${wizard_env}"
        echo "  migrate_wake_legs_config: set JASPER_WAKE_LEG_DTLN=${want_dtln}"
        echo "    from prior JASPER_MIC_DEVICE_DTLN=${dtln_value:-<unset>}, JASPER_AEC_DTLN_ENABLED=${dtln_enabled_value:-<unset>}"
    fi

    sed -i.bak '/^JASPER_MIC_DEVICE_RAW=/d' "${jasper_env}"
    sed -i.bak '/^JASPER_MIC_DEVICE_DTLN=/d' "${jasper_env}"
    sed -i.bak '/^JASPER_AEC_DTLN_ENABLED=/d' "${jasper_env}"
    rm -f "${jasper_env}.bak"
}

# Migrate stale transit env vars from /etc/jasper/jasper.env into the
# wizard-owned /var/lib/jasper/transit.env. The wizard at /transit
# owns every transit env variable; operators who paste those into
# jasper.env (CI bootstrap, headless imaging, SSH-driven setup) get
# them moved automatically so the wizard's file stays the single
# source of truth.
#
# Idempotent. Safe on fresh installs (no-op) and on long-lived ones
# (already-migrated keys just clean up the jasper.env residue).
migrate_transit_config() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/transit.env"

    local keys=(
        JASPER_SUBWAY_STATION_ID
        JASPER_SUBWAY_DEFAULT_DIRECTION
        JASPER_MTA_BUSTIME_KEY
        JASPER_BUS_STOPS
        JASPER_CITIBIKE_STATIONS
        JASPER_CITIBIKE_EBIKE_ONLY
    )

    [[ -f "${jasper_env}" ]] || return 0

    install -d -m 0750 "${STATE_DIR}"

    local k line stale_value
    for k in "${keys[@]}"; do
        line=$(grep -E "^${k}=" "${jasper_env}" || true)
        [[ -z "${line}" ]] && continue
        stale_value="${line#${k}=}"
        # Trim ONLY CR/LF — NOT spaces. JASPER_BUS_STOPS labels
        # contain spaces (e.g. "39 ST/4 AV SE"); a `%[ \t\r\n]*`
        # glob would shred them at the first space.
        stale_value="${stale_value%$'\r'}"
        stale_value="${stale_value%$'\n'}"

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

# Seed /var/lib/jasper/wifi_guardian.env from the currently-active WiFi
# profile if no stash exists yet. This is the migration hook for the
# WiFi profile guardian (docs/HANDOFF-resilience.md "Hardware-event
# recovery" sidebar) — it covers the SSH-driven setup case where the
# operator brought up WiFi via raspi-config / nmcli before ever
# opening the /wifi/ wizard.
#
# Idempotent:
#   - stash already exists       -> no-op
#   - nmcli missing              -> no-op (no NM, nothing to recover)
#   - no active WiFi connection  -> no-op (Ethernet-only Pi)
#   - active profile is WPA-EAP  -> no-op (enterprise out of scope)
#
# PSK redaction: the stash file is mode 0600 (root-only). The PSK lands
# in it because NM's own keyfile is also plaintext at 0600 — encrypting
# our copy while NM's stays plaintext is theatre against a root-equiv
# attacker. The PSK does NOT appear in any `echo` from this function.
migrate_wifi_guardian() {
    local stash="${STATE_DIR}/wifi_guardian.env"

    # Stash already exists — wizard or a previous migrate seeded it.
    # Nothing to do.
    [[ -f "${stash}" ]] && return 0

    # No nmcli means no NetworkManager; the guardian is a no-op on this
    # host. Don't bother seeding.
    command -v nmcli >/dev/null 2>&1 || return 0

    # Find the active wifi profile NAME. `nmcli` field "TYPE" reports
    # `802-11-wireless` for wifi connections.
    local active
    active=$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null \
             | awk -F: '$2 ~ /wifi|wireless/ { print $1; exit }')
    [[ -z "${active}" ]] && return 0

    # Pull SSID + PSK + key-mgmt for the active profile. `-s` is
    # "show secrets" — requires root, which install.sh always has.
    # We parse with awk to keep the PSK off any intermediate
    # variable trace (this whole helper runs without `set -x`).
    local ssid="" psk="" key_mgmt=""
    while IFS=: read -r key value; do
        case "${key}" in
            "802-11-wireless.ssid")              ssid="${value}" ;;
            "802-11-wireless-security.psk")      psk="${value}" ;;
            "802-11-wireless-security.key-mgmt") key_mgmt="${value}" ;;
        esac
    done < <(
        nmcli -s -t -f \
            802-11-wireless.ssid,\
802-11-wireless-security.psk,\
802-11-wireless-security.key-mgmt \
            connection show "${active}" 2>/dev/null
    )

    [[ -z "${ssid}" ]] && return 0

    # Enterprise auth is out of scope — the guardian can't recreate it
    # (no cert/identity in our stash). Skip silently rather than write
    # a stash that the guardian itself would refuse.
    [[ "${key_mgmt}" == "wpa-eap" ]] && return 0

    # Default key-mgmt to `none` when nmcli reported nothing (open
    # network). Matches the wizard's behavior.
    [[ -z "${key_mgmt}" ]] && key_mgmt="none"

    # Write atomically: tempfile in same dir, chmod 0600, mv. We're
    # in bash, not Python, so no fsync — the wizard does fsync on
    # its own writes, and seeding from install.sh is a one-time event
    # whose durability matters less than its idempotency.
    install -d -m 0750 "${STATE_DIR}"
    local tmp
    tmp=$(mktemp "${STATE_DIR}/.wifi_guardian.XXXXXX")
    # umask + mode dance: write the file with the PSK never visible to
    # other processes via `ls`. The `chmod 0600` after write is the
    # belt; `umask 077` on the tempfile creation is the suspenders.
    (
        umask 077
        cat > "${tmp}" <<EOF
JASPER_WIFI_SSID=${ssid}
JASPER_WIFI_PSK=${psk}
JASPER_WIFI_KEY_MGMT=${key_mgmt}
EOF
    )
    chmod 0600 "${tmp}"
    mv "${tmp}" "${stash}"

    # PSK redaction: the SSID is fine to log (visible in every nmcli
    # output) but the PSK never appears in this echo or any other.
    echo "  migrate_wifi_guardian: seeded ${stash} from active profile (SSID=${ssid}, key-mgmt=${key_mgmt})"
}

# --- Stage 1 memory-pressure resilience helpers ---
#
# Split into focused per-step functions to make each step
# individually testable + readable. Coordinator is
# `migrate_memory_resilience` below. All log via stdout (deploy-to-pi
# transcript capture) AND `logger -t jasper-install` (structured
# journald lines tagged `event=memory_resilience.*` for later
# `journalctl -t jasper-install` queries).
#
# See docs/HANDOFF-resilience.md "Memory-pressure resilience".


# Emit a structured event line to both stdout and journald.
# Args: $1=event_name (without the prefix), $2=detail (free text).
_mem_log() {
    local event="$1" detail="$2"
    echo "  memory_resilience: ${detail}"
    # Best-effort journald log — never fails the install.
    logger -t jasper-install -- "event=memory_resilience.${event} ${detail}" 2>/dev/null || true
}


# Compute vm.min_free_kbytes from MemTotal_kB.
# Formula: clamp(0.02 × memtotal_kb, 8192, 262144) — 2% of total RAM,
# with an 8 MB floor (Pi Foundation default; never reduce below) and
# 256 MB ceiling. See deploy/sysctl/99-jts-vm.conf header for rationale.
# Args:  $1 = memtotal_kb (integer)
# Output: integer to stdout
#
# Extracted as a standalone function so tests can drive it with
# synthetic memtotal values across the full Pi 5 SKU range.
_compute_min_free_kbytes() {
    local memtotal_kb="$1"
    awk -v m="${memtotal_kb}" '
        BEGIN {
            v = int(m * 0.02 + 0.5)
            if (v < 8192) v = 8192
            if (v > 262144) v = 262144
            printf "%d\n", v
        }
    '
}


# Step 1 — vm.* sysctls. Renders the template (substituting the
# RAM-aware min_free_kbytes value) and applies it. Returns 0 on
# success, non-zero on a step-internal failure (caller increments
# error counter).
_apply_jts_sysctls() {
    local memtotal_kb
    memtotal_kb=$(awk '/MemTotal:/ { print $2 }' /proc/meminfo 2>/dev/null)
    local min_free_kb
    if [[ -n "${memtotal_kb}" && "${memtotal_kb}" =~ ^[0-9]+$ ]]; then
        min_free_kb=$(_compute_min_free_kbytes "${memtotal_kb}")
    fi
    if [[ -z "${min_free_kb}" || ! "${min_free_kb}" =~ ^[0-9]+$ ]]; then
        # Fallback if /proc/meminfo is unreadable.
        min_free_kb=16384
        _mem_log "sysctls.fallback" \
            "couldn't read MemTotal; using fallback min_free_kbytes=${min_free_kb}"
    fi
    if ! sed -e "s/__VM_MIN_FREE_KBYTES__/${min_free_kb}/g" \
            "${REPO_DIR}/deploy/sysctl/99-jts-vm.conf" \
            > /etc/sysctl.d/99-jts-vm.conf; then
        _mem_log "sysctls.render_failed" \
            "WARN — failed to render /etc/sysctl.d/99-jts-vm.conf"
        return 1
    fi
    chmod 0644 /etc/sysctl.d/99-jts-vm.conf
    if ! sysctl --system >/dev/null 2>&1; then
        _mem_log "sysctls.apply_failed" \
            "WARN — sysctl --system failed; tunings live after reboot"
        return 1
    fi
    _mem_log "sysctls.applied" \
        "vm.* sysctls applied (min_free_kbytes=${min_free_kb} kB per RAM)"
    return 0
}


# Step 2 — MGLRU min_ttl_ms (thrashing prevention).
# On kernels without MGLRU (< 6.1), the `w-` tmpfiles directive
# silently skips the missing path — so this is safe even on older
# kernels.
_apply_jts_mglru() {
    if ! install -m 0644 "${REPO_DIR}/deploy/tmpfiles/jts-mglru.conf" \
            /etc/tmpfiles.d/; then
        _mem_log "mglru.install_failed" \
            "WARN — failed to install MGLRU tmpfiles config"
        return 1
    fi
    # --prefix scopes the apply to just our file (not the whole
    # tmpfiles tree, which would touch unrelated paths).
    if systemd-tmpfiles --create --prefix=/sys/kernel/mm/lru_gen \
            >/dev/null 2>&1; then
        _mem_log "mglru.applied" "MGLRU min_ttl_ms applied"
    else
        _mem_log "mglru.unsupported" \
            "MGLRU tmpfiles installed (no-op on kernels < 6.1)"
    fi
    return 0
}


# Step 3 — zram sizing via rpi-swap drop-in. rpi-swap is the Trixie
# standard zram manager (replaces dphys-swapfile); on older RPi OS
# the user may be on something else — skip gracefully there.
#
# IMPORTANT: rpi-swap is a systemd *generator*, not a service.
# `systemctl restart rpi-swap` is not a thing — the generator runs
# once at early boot and sizes the zram device. Per swap.conf(5):
# "After modifying any swap configuration, you must reboot the
# system for changes to take effect."
_apply_jts_zram_dropin() {
    if [[ ! -d /etc/rpi ]]; then
        _mem_log "zram.skip" \
            "/etc/rpi not present (rpi-swap not installed) — skipped zram sizing"
        return 0
    fi
    install -d -m 0755 /etc/rpi/swap.conf.d
    if ! install -m 0644 "${REPO_DIR}/deploy/rpi-swap/50-jts.conf" \
            /etc/rpi/swap.conf.d/; then
        _mem_log "zram.install_failed" \
            "WARN — failed to install rpi-swap drop-in"
        return 1
    fi
    # Check whether zram is already the target size.
    local cur_zram_bytes=0
    if [[ -r /sys/block/zram0/disksize ]]; then
        cur_zram_bytes=$(cat /sys/block/zram0/disksize 2>/dev/null || echo 0)
    fi
    local target_zram_bytes=$((520 * 1024 * 1024))
    local zram_diff=$((cur_zram_bytes - target_zram_bytes))
    # Within ±60 MB of target counts as "already correct."
    if [[ ${zram_diff#-} -lt 62914560 ]]; then
        _mem_log "zram.already_sized" \
            "zram drop-in installed; live size already ~50% RAM"
    else
        _mem_log "zram.reboot_required" \
            "zram drop-in installed; REBOOT REQUIRED to resize (current: $((cur_zram_bytes / 1024 / 1024)) MB → target: ~520 MB)"
    fi
    return 0
}


# Step 4 — live-write /proc/PID/oom_score_adj for each running
# critical daemon. The OOMScoreAdjust= directive in each .service
# file only takes effect on next process start; install.sh doesn't
# restart jasper-camilla (Rust binary, intentionally never auto-
# restarted per AGENTS.md) or jasper-mux (not in install.sh's
# restart list), so their running processes would sit at adj=0
# until reboot. Live-writing sets the kernel-visible value
# immediately — zero audio glitch, fully reversible.
#
# Reads the canonical target values from jasper._oom_adj.INSTALL_LIVE_WRITE
# (single source of truth shared with jasper-doctor).
_apply_jts_oom_score_adj_live() {
    # Read the canonical target values from the Python package.
    # Requires /opt/jasper/.venv to be installed (install_jasper
    # has already run by this point in main).
    local oom_adj_data
    if ! oom_adj_data=$(/opt/jasper/.venv/bin/python3 -c \
            'from jasper._oom_adj import INSTALL_LIVE_WRITE
for k, v in INSTALL_LIVE_WRITE.items():
    print(f"{k}={v}")' 2>/dev/null); then
        _mem_log "oom_score_adj.source_unavailable" \
            "WARN — couldn't read jasper._oom_adj; live-write skipped"
        return 1
    fi
    local live_writes=0 live_skips=0
    while IFS='=' read -r unit want; do
        [[ -z "${unit}" ]] && continue
        local pid
        pid=$(systemctl show -p MainPID --value "${unit}.service" 2>/dev/null || true)
        if [[ -z "${pid}" || "${pid}" == "0" ]]; then
            live_skips=$((live_skips+1))
            continue
        fi
        if [[ -w "/proc/${pid}/oom_score_adj" ]]; then
            if echo "${want}" > "/proc/${pid}/oom_score_adj" 2>/dev/null; then
                live_writes=$((live_writes+1))
            fi
        fi
    done <<< "${oom_adj_data}"
    _mem_log "oom_score_adj.applied" \
        "live-set oom_score_adj on ${live_writes} running daemon(s) (${live_skips} not running)"
    return 0
}


# Coordinator. Triggered by the 2026-05-23 incident: a PIO compile
# pushed the 1 GB Pi 5 into zram-thrash for 2+ minutes, kernel
# watchdog never fired because PID 1 stayed barely-alive.
#
# Each step is independent — a failure in one doesn't block the
# others. Stage 1 protections work today on the stock RPi kernel
# without enabling the memory cgroup controller (Stage 2 work).
# Idempotent under repeated runs.
migrate_memory_resilience() {
    local errors=0
    _apply_jts_sysctls           || errors=$((errors+1))
    _apply_jts_mglru             || errors=$((errors+1))
    _apply_jts_zram_dropin       || errors=$((errors+1))
    _apply_jts_oom_score_adj_live || errors=$((errors+1))
    if (( errors > 0 )); then
        _mem_log "summary.degraded" \
            "${errors} step(s) failed; system functional but degraded — see above"
    else
        _mem_log "summary.ok" "all 4 steps succeeded"
    fi
    return 0  # never fails install — best-effort migration
}

# Stage 2 audio-protection: enable the Linux memory cgroup controller
# so jts-audio.slice's `MemorySwapMax=0` actually enforces.
#
# Pi 5's device-tree blob (DTB) injects `cgroup_disable=memory` into the
# kernel's boot arguments — RPi Foundation chose this to save ~8 MB of
# accounting overhead. Override it by adding `cgroup_enable=memory
# cgroup_memory=1` to /boot/firmware/cmdline.txt. The kernel honors
# both flags and lets the explicit-enable win.
#
# Also adds `psi=1` defensively. RPi 6.12.x ships CONFIG_PSI=y +
# CONFIG_PSI_DEFAULT_DISABLED=y; the boot param turns PSI on. No-op
# on kernels that don't support PSI. Enables `/proc/pressure/`
# observability; not required for Stage 2 audio (which uses
# MemorySwapMax=0, not PSI), but useful for future Stage 3 work +
# `/system/` dashboard surface.
#
# IDEMPOTENT: grep guards each token. Existing cmdline.txt values
# are preserved unchanged. Operator-added tokens (custom kernel
# flags, etc.) survive.
#
# REBOOT REQUIRED: kernel command line only re-reads at boot.
# Function surfaces this loudly so the operator knows.
migrate_cgroup_memory_enabled() {
    local cmdline_file="/boot/firmware/cmdline.txt"
    if [[ ! -f "${cmdline_file}" ]]; then
        echo "  cgroup_memory: WARN — ${cmdline_file} missing (not RPi OS?); skipped"
        return 0
    fi
    local current
    current=$(cat "${cmdline_file}")
    local changed=0
    local to_add=()
    for token in "cgroup_enable=memory" "cgroup_memory=1" "psi=1"; do
        if ! grep -qE "(^|[[:space:]])${token}([[:space:]]|$)" "${cmdline_file}"; then
            to_add+=("${token}")
            changed=1
        fi
    done
    if (( changed == 0 )); then
        echo "  cgroup_memory: cmdline.txt already configured"
        return 0
    fi
    # cmdline.txt is a SINGLE line — preserve that. Append the new
    # tokens with spaces. Strip trailing newline if any.
    {
        printf '%s' "${current% }"
        for t in "${to_add[@]}"; do
            printf ' %s' "${t}"
        done
        printf '\n'
    } > "${cmdline_file}.tmp"
    mv "${cmdline_file}.tmp" "${cmdline_file}"
    chmod 0755 "${cmdline_file}"
    echo "  cgroup_memory: cmdline.txt updated; added: ${to_add[*]}"
    echo "  cgroup_memory: REBOOT REQUIRED for kernel to honor the new boot args"
    logger -t jasper-install -- "event=cgroup_memory.cmdline_updated added=${to_add[*]}" 2>/dev/null || true
    return 0
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

    # jasper-fanin: per-renderer snd-aloop substream fan-in daemon
    # (Tier 2A audio architecture). Installed but NOT enabled by
    # default — operator opts in via the JASPER_AUDIO_TOPOLOGY=fanin
    # feature flag in /etc/jasper/jasper.env once their hardware is
    # ready. See docs/HANDOFF-fan-in-daemon.md migration plan.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-fanin.service" \
        "${SYSTEMD_DIR}/jasper-fanin.service"

    # WiFi profile guardian. Type=oneshot boot-time recreate of a lost
    # /etc/NetworkManager/system-connections/<SSID>.nmconnection from
    # the wizard-owned stash at /var/lib/jasper/wifi_guardian.env. See
    # docs/HANDOFF-resilience.md "WiFi profile recovery" for the
    # design and the 2026-05-23 incident this defends against.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-guardian.service" \
        "${SYSTEMD_DIR}/jasper-wifi-guardian.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-wifi-guardian" \
        /usr/local/sbin/jasper-wifi-guardian

    # jasper-usbsink: fourth music source (USB gadget audio in). The
    # init unit owns the ConfigFS gadget descriptor lifecycle; the
    # main service is the Python daemon that bridges gadget capture
    # into hw:Loopback,0,0. Both ship DISABLED — the /sources/ wizard
    # toggle owns enable/disable, and the dtoverlay must be set + Pi
    # rebooted first (handled by set_usb_gadget_mode above).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink-init.service" \
        "${SYSTEMD_DIR}/jasper-usbsink-init.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink.service" \
        "${SYSTEMD_DIR}/jasper-usbsink.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-gadget-up" \
        /usr/local/sbin/jasper-usbsink-gadget-up
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-gadget-down" \
        /usr/local/sbin/jasper-usbsink-gadget-down
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-wait-card" \
        /usr/local/sbin/jasper-usbsink-wait-card
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

    # sshd OOM-protection drop-in: Debian's openssh-server package
    # ships ssh.service WITHOUT an OOMScoreAdjust= directive, so the
    # kernel's default (0) applies — making sshd a candidate for
    # OOM-kill under heavy pressure. JTS's resilience story relies
    # on sshd being the recovery path during failure events; the
    # drop-in forces OOMScoreAdjust=-1000 (immortal). Operators on
    # distros whose sshd unit is named differently (sshd.service on
    # RHEL/Fedora) should rename. See the file's header comment.
    install -d -m 0755 "${SYSTEMD_DIR}/ssh.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/ssh.service.d/oom-protection.conf" \
        "${SYSTEMD_DIR}/ssh.service.d/oom-protection.conf"

    # Stage 2 audio-protection slices: MemorySwapMax=0 on jts-audio.slice
    # (camilla + shairport-sync + librespot + bluealsa-aplay) and
    # jts-mic.slice (aec-bridge). Pages in these slices can NEVER be
    # swapped to zram — direct fix for the 2026-05-24 stress test that
    # caused audible audio glitches because aec-bridge accumulated 42 MB
    # of VmSwap. Requires cgroup memory controller enabled in
    # /boot/firmware/cmdline.txt (handled by migrate_cgroup_memory_enabled).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jts-audio.slice" \
        "${SYSTEMD_DIR}/jts-audio.slice"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jts-mic.slice" \
        "${SYSTEMD_DIR}/jts-mic.slice"
    # bluealsa-aplay's Slice= assignment lands as a drop-in (we don't
    # own that unit file fully — the package ships it). The 4 services
    # we DO own (jasper-camilla, jasper-aec-bridge, shairport-sync,
    # librespot) have Slice= directly in the .service file installed
    # above; no separate drop-in needed for them.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-slice.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-slice.conf"

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
    # WiFi profile guardian: oneshot at boot, gated by
    # ConditionPathExists= on the wizard's stash file. Enabling is safe
    # on fresh installs because the unit silently no-ops until the
    # wizard saves once. See migrate_wifi_guardian (called from
    # ensure_env_file above) for the SSH-driven-setup seed path.
    systemctl enable jasper-wifi-guardian.service
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
    # Three keys live in aec_mode.env, all owned by the /system
    # wake-detection card after the per-leg-toggle refactor:
    #   - JASPER_AEC_MODE             master AEC bridge toggle
    #   - JASPER_WAKE_LEG_RAW         additive raw chip-direct leg (~5 MB)
    #   - JASPER_WAKE_LEG_DTLN        additive DTLN neural leg (~75 MB)
    # Defaults: AEC on, raw on (dual-stream is the OSS baseline —
    # cheap wake-rate win), DTLN off (heavy, opt-in for 2 GB Pis with
    # a wake-event corpus). See the /system card and AGENTS.md
    # "AEC bridge — reconciler toggle" for the lever set.
    #
    # On upgrade, the reconciler's ensure_mode_file appends any
    # missing keys with these same defaults — preserving an
    # operator's hand-set JASPER_AEC_MODE while picking up the new
    # leg fields. Migration from hand-set underlying env vars in
    # /etc/jasper/jasper.env runs separately in migrate_wake_legs_config.
    if [[ ! -f "${STATE_DIR}/aec_mode.env" ]]; then
        printf 'JASPER_AEC_MODE=auto\nJASPER_WAKE_LEG_RAW=1\nJASPER_WAKE_LEG_DTLN=0\n' \
            > "${STATE_DIR}/aec_mode.env"
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
    set_usb_gadget_mode
    tune_wifi_for_airplay
    install_jasper
    build_install_jasper_fanin   # Rust daemon binary; installed but not enabled
    install_systemd_units
    migrate_memory_resilience   # Stage 1 OOM protection: sysctl + MGLRU + zram
    migrate_cgroup_memory_enabled  # Stage 2 audio-slice: cgroup memory + PSI in cmdline.txt
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

# Only run main when invoked directly. When sourced (e.g. by tests
# that want to call a single helper like `_compute_min_free_kbytes`),
# define the functions but don't execute main.
if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
    main "$@"
fi
