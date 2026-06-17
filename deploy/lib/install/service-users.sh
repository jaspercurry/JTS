# shellcheck shell=bash
# WS1 Phase 3b — dedicated non-root service users + the shared `jasper` group for
# the Tier-A daemons dropped from root: jasper-voice / jasper-mux / jasper-input
# (3b-1), jasper-control (3b-2, polkit-mediated restarts/reboots), and jasper-web
# (3b-3, polkit-mediated NetworkManager + bluetooth/systemd-journal groups). All
# share primary group `jasper` so the cross-daemon /run sockets (the broker) and
# /var/lib/jasper state are reachable. Mirrors the existing shairport-sync
# user-creation pattern in renderers.sh. All operations are idempotent and safe
# to re-run (useradd only when absent; supplementary-group adds via usermod -aG
# on the upgrade path).
#
# See docs/HANDOFF-privilege-separation.md.

# Create the shared `jasper` group and the 3b-1 service users. The group is the
# cross-daemon access boundary: it owns the /run UDS dirs (so a connector can
# traverse to a binder's socket) and group-shared /var/lib/jasper state.
create_jasper_service_users() {
    if ! getent group jasper >/dev/null 2>&1; then
        groupadd -r jasper
    fi
    # Primary group `jasper` for every dropped daemon. Supplementary groups
    # match each unit's SupplementaryGroups=: audio (ALSA) for voice, input
    # (/dev/input/event*) for input. -r = system account, -M = no home,
    # nologin shell. The unit's User=/Group=/SupplementaryGroups= are the
    # runtime authority; keeping the passwd entry consistent avoids surprises
    # for tools that read /etc/passwd.
    if ! getent passwd jasper-voice >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G audio jasper-voice
    fi
    if ! getent passwd jasper-mux >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper jasper-mux
    fi
    if ! getent passwd jasper-input >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G input jasper-input
    fi
    # WS1 Phase 3b-2 — jasper-control drops to non-root too. It binds TCP
    # (0.0.0.0:8780), opens a localhost WebSocket to CamillaDSP, and writes
    # /var/lib/jasper + /etc/avahi/services — no /dev/snd or /dev/input. Its
    # privileged restarts/reboots are granted by polkit
    # (deploy/polkit/49-jasper-control.rules), not a group. The one supplementary
    # group is `systemd-journal`: several /state cards (airplay_health, dial,
    # wifi_guardian last-action) read the journal. The unit's User=jasper-control
    # matches this exact name (the polkit rule keys on it).
    if ! getent passwd jasper-control >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G systemd-journal jasper-control
    fi
    # Ensure the systemd-journal membership on UPGRADE too — the useradd above is
    # skipped when the user already exists (e.g. a Pi from an earlier 3b-2 build
    # before the journal-reading /state cards needed it). Idempotent; takes
    # effect on jasper-control's next restart (the deploy restarts it).
    if getent group systemd-journal >/dev/null 2>&1; then
        usermod -aG systemd-journal jasper-control 2>/dev/null || true
    fi
    # WS1 Phase 3b-3 — jasper-web (the wizard HTTP server) drops to non-root too.
    # The /wifi/ page drives NetworkManager: its privileged restarts/reboots are
    # NOT needed, but its NM writes are granted by polkit
    # (deploy/polkit/49-jasper-web.rules), keyed on User=jasper-web. The two
    # supplementary groups are `bluetooth` (BlueZ Adapter1 Alias for the /speaker
    # rename — a D-Bus policy grant) and `systemd-journal` (journalctl -k for
    # Wi-Fi scan-suppression diagnostics). No netdev (polkit is authoritative on
    # modern NM), no CAP_NET_ADMIN (scan-repair degrades fail-soft).
    # systemd-journal is always present (systemd owns it), so it is safe in the
    # useradd -G. bluetooth is NOT: it is created by the bluez package, which
    # install_deps apt-installs AFTER create_jasper_service_users runs — so a
    # hard `-G bluetooth` would make `useradd` exit 6 ("group does not exist")
    # and, under `set -euo pipefail`, abort the whole fresh install on any base
    # image that doesn't already ship bluez. Standard Raspberry Pi OS does ship
    # bluez (the group is present here), so the add below lands in a single
    # install; on a bare base image jasper-web simply picks up bluetooth on the
    # next deploy, and the /speaker BlueZ-name rename degrades fail-soft until
    # then. Add both groups idempotently (also the upgrade path: useradd is
    # skipped when the user already exists).
    if ! getent passwd jasper-web >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G systemd-journal jasper-web
    fi
    usermod -aG systemd-journal jasper-web 2>/dev/null || true
    if getent group bluetooth >/dev/null 2>&1; then
        usermod -aG bluetooth jasper-web 2>/dev/null || true
    fi
    echo "  Service users ready: jasper-voice, jasper-mux, jasper-input, jasper-control, jasper-web (group: jasper)"

    # The /var/lib/jasper directory itself is widened to root:jasper 0770 by the
    # group-aware ensure_state_dir() (env-migrations.sh), which runs on every
    # install and now that the `jasper` group exists. Here we only widen a
    # pre-existing speaker_volume.json (upgrade path) so the now-non-root
    # voice/mux can read+write it before any daemon rewrites it. Owner stays
    # root (rollback-safe); the file carries no secret. Other state keeps its
    # owner-only modes — the dropped daemons read config via systemd
    # EnvironmentFile injection (root reads it pre-drop), never off disk.
    if [[ -f "${STATE_DIR}/speaker_volume.json" ]]; then
        chgrp jasper "${STATE_DIR}/speaker_volume.json" 2>/dev/null || true
        chmod 0660 "${STATE_DIR}/speaker_volume.json" 2>/dev/null || true
    fi
}
