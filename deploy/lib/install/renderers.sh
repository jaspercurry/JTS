#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Renderer install steps for deploy/install.sh: librespot (raspotify
# .deb), nqptp + shairport-sync (AirPlay 2) source builds, bluez
# config, the USB gadget dtoverlay, and the AirPlay WiFi power-save
# tweak.
#
# Extracted verbatim from install.sh (the installer remains the only
# caller; it sources this file REPO_DIR-relative from the rsync
# checkout). Functions assume install.sh's globals (REPO_DIR, the
# RASPOTIFY_*/NQPTP_*/SHAIRPORT_SYNC_* pins), its
# fetch_verified_source_archive helper, and `set -euo pipefail` from
# the sourcing shell.

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
        # Bounded retries + transfer cap: same rationale as
        # fetch_verified_source_archive (multi-MB fetch on flaky WiFi).
        curl -fsSL --retry 3 --retry-connrefused --max-time 300 \
            -o "${tmpdir}/raspotify.deb" "${RASPOTIFY_URL}"
        echo "${RASPOTIFY_SHA256}  ${tmpdir}/raspotify.deb" | sha256sum -c -
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
        fetch_verified_source_archive \
            "${NQPTP_ARCHIVE_URL}" \
            "${NQPTP_SHA256}" \
            "${tmpdir}/nqptp" \
            "nqptp (${NQPTP_COMMIT})"
        (
            cd "${tmpdir}/nqptp" || exit 1
            autoreconf -fi
            ./configure --with-systemd-startup
            # RAM-bounded + cgroup-contained C build (BUILD_SANDBOX_KB_PER_JOB_C
            # budget) so an OOM kills only the build, not a live daemon.
            run_contained_build "nqptp" -- \
                make -j"$(build_sandbox_jobs "${BUILD_SANDBOX_KB_PER_JOB_C}")"
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
    elif ! /usr/local/bin/shairport-sync -V 2>&1 | grep -qE -- '-pipe(-|$)'; then
        # The pipe output backend (--with-pipe) was added to the configure
        # line; an older source build (or apt AP1) lacks it. shairport-sync's
        # -V feature string gains a "pipe" feature token only when the backend
        # is compiled in, so this forces exactly one rebuild on upgrade and is
        # a no-op once the pipe-capable binary is installed. The pattern
        # matches the token whether it is followed by another "-<feature>"
        # token or sits at the end of the string, so a future trim of the -V
        # feature list cannot leave us rebuilding on every deploy.
        need_build=1
    fi
    if [[ "$need_build" == "1" ]]; then
        echo "Building shairport-sync ${SHAIRPORT_SYNC_VERSION} with AirPlay 2..."
        # Fetch + build FIRST, touch the live install only after the
        # build succeeded. The previous order stopped shairport-sync and
        # apt-removed the old binary before the download/compile — under
        # `set -e` a fetch or build failure stranded the Pi with no
        # AirPlay at all. Now a failure leaves whatever was previously
        # installed (apt AP1 build or older source build) still serving.
        local tmpdir
        tmpdir="$(mktemp -d)"
        fetch_verified_source_archive \
            "${SHAIRPORT_SYNC_ARCHIVE_URL}" \
            "${SHAIRPORT_SYNC_SHA256}" \
            "${tmpdir}/sps" \
            "shairport-sync ${SHAIRPORT_SYNC_VERSION} (${SHAIRPORT_SYNC_COMMIT})"
        (
            cd "${tmpdir}/sps" || exit 1
            autoreconf -fi
            ./configure --sysconfdir=/etc \
                --with-alsa --with-soxr --with-avahi \
                --with-ssl=openssl --with-systemd \
                --with-airplay-2 \
                --with-pipe \
                --with-metadata --with-dbus-interface \
                --with-mpris-interface
            # RAM-bounded + cgroup-contained C build (BUILD_SANDBOX_KB_PER_JOB_C
            # budget) so an OOM kills only the build, not a live daemon.
            run_contained_build "shairport-sync" -- \
                make -j"$(build_sandbox_jobs "${BUILD_SANDBOX_KB_PER_JOB_C}")"
        )
        # Build succeeded — now remove the apt-installed AP1 build if
        # present. Keep /etc/shairport-sync.conf — apt remove preserves
        # it; apt purge doesn't, so we use remove.
        systemctl stop shairport-sync 2>/dev/null || true
        apt-get remove -y shairport-sync 2>/dev/null || true
        (
            cd "${tmpdir}/sps" || exit 1
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
    #   - __AIRPLAY_NAME__ from /var/lib/jasper/speaker_name.env
    #   - __AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__ from the active CamillaDSP
    #     samplerate/chunksize/target_level.
    # shairport-sync.service's ExecStartPre re-renders on every active-only
    # try-restart, so toggling the mode (via /airplay/ web UI or
    # jasper-airplay-mode CLI) is an env-file write + try-restart. A household
    # Off source stays stopped.
    install -m 0644 \
        "${REPO_DIR}/deploy/shairport-sync.conf.template" \
        /etc/shairport-sync.conf.template
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-apply-airplay-mode" \
        /usr/local/sbin/jasper-apply-airplay-mode
    # The old dmix/fanin topology switcher was retired when fan-in
    # became the only supported renderer path. Remove stale installed
    # copies so operators do not accidentally reintroduce split-brain
    # audio state after an upgrade.
    rm -f /usr/local/sbin/jasper-audio-topology
    rm -rf /etc/jasper/audio-topology
    rm -f /usr/local/sbin/jasper-derive-device-name
    # Default to synced: with shairport-sync.conf.template setting
    # resync_threshold_in_seconds=0.2, synced mode is glitch-free on
    # this chain (empirically verified over multiple 5-min samples
    # after the fix; see docs/HANDOFF-airplay.md). Synced is the
    # right default because it gives users video A/V sync + multi-room
    # AirPlay sync for free. Users can still flip to free-running via
    # /airplay/ if they hit DAC-specific issues. Existing env files
    # are preserved across reinstalls.
    if [[ ! -e /var/lib/jasper/airplay_mode.env ]]; then
        ensure_state_dir
        printf 'JASPER_AIRPLAY_FREE_RUNNING=no\n' \
            > /var/lib/jasper/airplay_mode.env
        chmod 0644 /var/lib/jasper/airplay_mode.env
        echo "  /var/lib/jasper/airplay_mode.env defaulted to synced."
    fi
    # Seed /etc/shairport-sync.conf so the first start of shairport-sync
    # has a valid config. ExecStartPre re-renders on every subsequent
    # restart, picking up any changes made via the web UI / CLI.
    /usr/local/sbin/jasper-apply-airplay-mode

    # bluez-alsa-utils was apt-installed in install_deps.
    # Configure /etc/bluetooth/main.conf for speaker-mode (Just Works
    # pairing, audio-class device).
    bash "${REPO_DIR}/deploy/configure-bluez.sh"
}

set_usb_gadget_mode() {
    # Write the two config.txt settings a USB-C gadget box needs: (1) the
    # dtoverlay that puts the board's OTG-capable USB controller into
    # peripheral mode so it can present as a USB gadget to a connected host,
    # and (2) usb_max_current_enable so a Pi 5 actually boots when powered
    # through a USB-C power/data splitter that doesn't pass PD negotiation
    # (see step 2 below for the full rationale). This is the precondition for
    # the jasper-usbsink feature — a fourth music source where a computer
    # plugged into the Pi via an appropriate power/data splitter sees the
    # configured speaker name as a USB audio output device.
    #
    # We only set the dtoverlay here. libcomposite / the ConfigFS gadget are
    # composed by jasper-usbgadget.service (enabled by install.sh for the
    # always-on USB management network). USB *audio* stays gated behind the
    # /sources/ wizard toggle. When both the network is kill-switched and audio
    # is off, jasper-usbgadget's ExecCondition skips the unit and libcomposite
    # never loads, so RAM stays at baseline (~50 KB dwc2 kernel module).
    #
    # Requires a reboot to take effect — the dwc2 module is loaded by
    # the kernel via the dtoverlay at boot. Subsequent runs of
    # install.sh are no-ops once the line is present.
    #
    # Side effect to document: the OTG data port is no longer available
    # for ordinary USB host devices while this overlay is active. On a
    # Pi 5 the USB-A ports remain host-mode outputs; on a Zero-class
    # board this can conflict with using the same OTG port for a USB DAC
    # unless the operator has a powered/split-role hardware topology that
    # proves both legs work.
    local cfg="${JTS_BOOT_CONFIG_FILE:-/boot/firmware/config.txt}"
    if [[ ! -f "$cfg" ]]; then
        echo "  $cfg not present; skipping USB gadget dtoverlay."
        return 0
    fi
    # 1. Peripheral dtoverlay — the gadget precondition.
    if grep -qE '^[[:space:]]*dtoverlay=dwc2,dr_mode=peripheral' "$cfg"; then
        echo "  USB gadget dtoverlay already present in $cfg."
    else
        cat >> "$cfg" <<'EOF'

# JTS install — required for the composite USB gadget (management network +
# optional audio). Puts the board's OTG-capable USB controller into peripheral
# mode so a connected host can reach this speaker over USB and (when USB audio
# is enabled) see it as a USB audio output device. jasper-usbgadget.service
# modprobes libcomposite on demand and composes the descriptor; when both the
# network is kill-switched and audio is off, its ExecCondition skips and
# libcomposite never loads, so RAM stays at baseline. On Zero-class
# streamboxes this is intentionally allowed for
# powered splitter validation, but the same OTG port may be needed for
# the DAC unless the hardware topology proves both roles can coexist.
# Reboot required to take effect. See docs/HANDOFF-usbsink.md.
[all]
dtoverlay=dwc2,dr_mode=peripheral
EOF
        echo "  USB gadget dtoverlay added to $cfg (reboot required to apply)."
    fi

    # 2. usb_max_current_enable — checked INDEPENDENTLY of the dtoverlay above so
    #    a box that already had the overlay from a prior install still picks this
    #    up on a re-run (that early-return used to skip it). On a Pi 5 the USB-C
    #    port both powers the board AND carries the gadget data; when power comes
    #    through a USB-C power/data splitter, the splitter typically does NOT pass
    #    the USB-C PD negotiation, so the Pi 5 cannot confirm a 5A supply, runs
    #    power-restricted, and can halt at the firmware stage before the OS boots
    #    (solid red LED, no journal) even with a capable PSU behind the splitter.
    #    This flag tells the firmware to allow full USB current without that PD
    #    confirmation, letting a gadget box boot through the splitter. No-op on a
    #    box powered by a normal PD supply (PD negotiates 5A anyway); safe as long
    #    as the supply is genuinely capable — the Pi's own undervoltage detection
    #    still guards a marginal one. Verified on jts.local 2026-07-06.
    if grep -qE '^[[:space:]]*usb_max_current_enable=1' "$cfg"; then
        echo "  usb_max_current_enable already present in $cfg."
    else
        cat >> "$cfg" <<'EOF'

# JTS install — allow full USB current without 5A PD confirmation so a Pi 5
# gadget box boots when powered through a USB-C power/data splitter (which
# doesn't pass PD negotiation). No-op with a normal PD supply; safe with a
# capable supply — undervoltage protection still guards a marginal one.
# Reboot required to take effect. See set_usb_gadget_mode() +
# docs/HANDOFF-usbsink.md.
[all]
usb_max_current_enable=1
EOF
        echo "  usb_max_current_enable=1 added to $cfg (reboot required to apply)."
    fi
}

tune_wifi_for_airplay() {
    # Disable WiFi power-save and make NetworkManager keep retrying on the
    # active wlan0 connection.
    # Pi's brcmfmac driver defaults to power-save ON, which causes
    # micro-stalls in WiFi RX during radio sleeps. AirPlay 2 streams
    # over unicast UDP and has no application-level retransmit; even
    # a few-ms WiFi stall correlates with shairport-sync sync errors
    # and underruns. nmcli value 2 = disable. `connection.autoconnect-retries
    # 0` means retry forever; the default `-1` delegates to NM's global retry
    # budget, which can be exhausted by a long router/ISP flap. `ipv6.method
    # link-local` preserves fast mDNS `.local` resolution for iOS/macOS without
    # enabling routed IPv6; profiles set to `ignore` make clients wait on IPv6
    # mDNS before falling back to IPv4. All settings persist in the
    # NetworkManager keyfile, so a future reinstall is a no-op.
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
    nmcli c modify "$wlan_conn" \
        connection.autoconnect yes \
        connection.autoconnect-retries 0 \
        802-11-wireless.powersave 2 \
        ipv6.method link-local \
        2>/dev/null || true
    # Apply without dropping the connection. If the driver doesn't
    # accept a live reapply (some brcmfmac variants), the change
    # still takes effect on the next reconnect/reboot.
    nmcli dev reapply wlan0 2>/dev/null || true
    echo "  WiFi power-save disabled, autoconnect retries set to forever, and link-local IPv6 enabled on connection '$wlan_conn'."
}
