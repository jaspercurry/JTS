# shellcheck shell=bash
# WS1 Phase 3b (3b-1) — dedicated non-root service users + the shared `jasper`
# group for the Tier-A daemons being dropped from root (jasper-voice,
# jasper-mux, jasper-input). jasper-control + jasper-web stay root in 3b-1 (their
# drops are the gated 3b-2 / 3b-3 fast-follows) but jasper-control still joins
# the `jasper` group via its unit so the broker socket is reachable by the
# now-non-root mux. Mirrors the existing shairport-sync user-creation pattern in
# renderers.sh. All operations are idempotent and safe to re-run.
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
    echo "  Service users ready: jasper-voice, jasper-mux, jasper-input (group: jasper)"

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
