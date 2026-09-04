# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# shellcheck shell=bash
# WS1 Phase 3b/first Tier-B slice — dedicated non-root service users + the
# shared `jasper` group. Tier-A daemons dropped from root: jasper-voice /
# jasper-mux / jasper-input (3b-1), jasper-control (3b-2, polkit-mediated
# restarts/reboots), and jasper-web (3b-3, polkit-mediated NetworkManager +
# bluetooth/systemd-journal groups). The first Tier-B DAC mixer slice adds
# jasper-recon for non-root amixer access via the `audio` group. The optional
# USB host-microphone relay has its own `jasper-usbmic` identity so it can use
# ALSA without inheriting `jasper-input`'s /dev/input authority. All share
# primary group `jasper` so the cross-daemon /run sockets (the broker) and
# /var/lib/jasper state are reachable. Mirrors the existing shairport-sync
# user-creation pattern in renderers.sh. All operations are idempotent and safe
# to re-run (useradd only when absent; supplementary-group adds via usermod -aG
# on the upgrade path).

# Create the shared `jasper` group and the 3b-1 service users. The group is the
# cross-daemon access boundary: it owns the /run UDS dirs (so a connector can
# traverse to a binder's socket) and group-shared /var/lib/jasper state.
create_jasper_service_users() {
    if ! getent group jasper >/dev/null 2>&1; then
        groupadd -r jasper
    fi
    # WS1 Phase 4a — `jasper-secrets` narrows the high-value secrets (LLM API
    # keys + Google OAuth client secret + token tree) to just the voice + web
    # service users. Created HERE, before the -G group lists below reference it,
    # so a fresh install adds members in one shot. Safe to put in a -G list
    # precisely because WE create it (unlike the package-created `bluetooth`
    # group, which must be added via a guarded usermod after install_deps).
    if ! getent group jasper-secrets >/dev/null 2>&1; then
        groupadd -r jasper-secrets
    fi
    # WS1 Phase 4b — `jasper-intsecrets` narrows integration secrets (Home
    # Assistant token + Spotify credentials/OAuth token cache) to the daemons
    # that use them: voice, control, mux, and web. jasper-input is deliberately
    # excluded. Created before useradd -G references it, mirroring
    # jasper-secrets above.
    if ! getent group jasper-intsecrets >/dev/null 2>&1; then
        groupadd -r jasper-intsecrets
    fi
    # U3 / P6 — `jts-ring` owns /dev/shm/jts-ring (mode 2775, see
    # deploy/tmpfiles/jts-ring.conf). It grants exactly one thing: write access
    # to the SHM slot rings. Membership is what lets a NON-ROOT renderer
    # (librespot as `pi`, shairport-sync as its own user) create and write its
    # lane's ring while jasper-fanin reads it — both ends write the ring
    # HEADER, so both need write on a file the other may have created.
    #
    # NOT every migrated renderer needs it. bluealsa-aplay (U3/P6b) runs as ROOT
    # — its packaged unit sets no User= and neither does the JTS drop-in — and
    # uid 0 writes this 2775 root-owned directory regardless, so it gets no
    # usermod here and demanding one would be ceremony. What it DOES need is
    # `UMask=0007`, which governs the ring FILE's mode rather than the
    # directory's group; that lives in its drop-in.
    #
    # A dedicated group rather than either obvious alternative, both of which the
    # tmpfiles header flagged for decision at P6:
    #   - NOT `jasper`: that group also carries /var/lib/jasper at mode 0770 plus
    #     control_token and household_secret at 0640, so adding a renderer user
    #     would grant write on all shared JTS state to buy write on one directory
    #     ("renderers do not join the writer group").
    #   - NOT `audio`: that is the ALSA DEVICE-access group (/dev/snd), which is
    #     orthogonal to SHM-file sharing, and it is far broader than the set of
    #     processes that should be able to write frames into the mix.
    # Created here, before the -G lists below, for the same reason as the two
    # groups above; the guarded usermod for the pre-existing `pi` account is at
    # the end of this function.
    if ! getent group jts-ring >/dev/null 2>&1; then
        groupadd -r jts-ring
    fi
    # Primary group `jasper` for every dropped daemon. Supplementary groups
    # match each unit's SupplementaryGroups=: audio (ALSA) for voice, input
    # (/dev/input/event*) for input. -r = system account, -M = no home,
    # nologin shell. The unit's User=/Group=/SupplementaryGroups= are the
    # runtime authority; keeping the passwd entry consistent avoids surprises
    # for tools that read /etc/passwd.
    if ! getent passwd jasper-voice >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G audio,jasper-secrets,jasper-intsecrets jasper-voice
    fi
    if ! getent passwd jasper-mux >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G jasper-intsecrets jasper-mux
    fi
    if ! getent passwd jasper-input >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G input jasper-input
    fi
    # `bluetooth` for the accessory mic adapter task this process runs
    # (ADR-0225): BlueZ's D-Bus policy grants that group the GATT access the
    # adapter needs. Guarded rather than hard-listed for the same reason as
    # jasper-web's below — bluez is apt-installed after this function runs, so
    # a hard `-G bluetooth` would exit 6 on a base image without it and abort
    # the install. Idempotent, and also the upgrade path (useradd is skipped
    # when the user already exists).
    if getent group bluetooth >/dev/null 2>&1; then
        usermod -aG bluetooth jasper-input 2>/dev/null || true
    fi
    # Optional Pi-to-host USB microphone relay. Keep this separate from
    # jasper-input: the relay needs ALSA playback plus group-readable Jasper
    # state, never HID event devices or the accessory daemon's signaling
    # boundary.
    if ! getent passwd jasper-usbmic >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G audio jasper-usbmic
    fi
    usermod -aG audio jasper-usbmic 2>/dev/null || true
    # WS1 Phase 3b-2 — jasper-control drops to non-root too. It binds TCP
    # (0.0.0.0:8780), opens a localhost WebSocket to CamillaDSP, and writes
    # /var/lib/jasper + /etc/avahi/services — no /dev/snd or /dev/input. Its
    # privileged restarts/reboots are granted by polkit
    # (deploy/polkit/49-jasper-control.rules), not a group. Its three
    # supplementary groups are `systemd-journal` (the airplay_health and
    # wifi_guardian last-action /state cards read the journal),
    # `jasper-intsecrets` (fresh HA reads + Spotify token-cache writes), and
    # `jts-ring` (#2786: /state's grouping `ring` block reads the grouping
    # ring's shared header — read-only, see the unit file). `jts-ring` is safe
    # to hard-list here because THIS function creates that group above, the
    # same guarantee the other hard-listed ring-group member relies on. The
    # unit's User=jasper-control matches this exact name (polkit keys on it).
    if ! getent passwd jasper-control >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G systemd-journal,jasper-intsecrets,jts-ring jasper-control
    fi
    # Ensure the systemd-journal membership on UPGRADE too — the useradd above is
    # skipped when the user already exists (e.g. a Pi from an earlier 3b-2 build
    # before the journal-reading /state cards needed it). Idempotent; takes
    # effect on jasper-control's next restart (the deploy restarts it).
    if getent group systemd-journal >/dev/null 2>&1; then
        usermod -aG systemd-journal jasper-control 2>/dev/null || true
    fi
    # WS1 Phase 3b-3 — jasper-web (the wizard HTTP servers) drops to non-root too.
    # One account serves every nginx-fronted wizard unit — jasper-web,
    # jasper-chat-web, jasper-correction-web, jasper-bluetooth-web,
    # jasper-system-web — because they share state files, the /run
    # staged-startup-hold directory, the jasper-secrets compartment the tuning
    # LLM key lives in, and the restart broker's closed client list.
    # The /wifi/ page drives NetworkManager: its privileged restarts/reboots are
    # NOT needed, but its NM writes are granted by polkit
    # (deploy/polkit/49-jasper-web.rules), keyed on User=jasper-web. Its
    # supplementary groups are `audio` (active-speaker commissioning tones write
    # the same-path correction_substream), `bluetooth` (BlueZ Adapter1 Alias for
    # the /speaker rename — a D-Bus policy grant), and `systemd-journal`
    # (journalctl -k for Wi-Fi scan-suppression diagnostics). No netdev (polkit
    # is authoritative on modern NM), no CAP_NET_ADMIN (scan-repair runs through
    # the root jasper-wifi-scan-repair.service helper).
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
        useradd -r -M -s /usr/sbin/nologin -g jasper -G audio,systemd-journal,jasper-secrets,jasper-intsecrets,jts-ring jasper-web
    fi
    usermod -aG audio jasper-web 2>/dev/null || true
    usermod -aG systemd-journal jasper-web 2>/dev/null || true
    if getent group bluetooth >/dev/null 2>&1; then
        usermod -aG bluetooth jasper-web 2>/dev/null || true
    fi
    # WS1 Tier-B DAC mixer slice — jasper-recon is the non-root service user
    # for hardware reconcilers that only need ALSA mixer access: the declared
    # DAC mixer pins (`jasper-dac-init`) and the Apple dongle drift monitor
    # (`jasper-headphone-monitor`). Primary group `jasper`, supplementary `audio`
    # for /dev/snd/controlC* access. No sudo/polkit grant is attached here.
    if ! getent passwd jasper-recon >/dev/null 2>&1; then
        useradd -r -M -s /usr/sbin/nologin -g jasper -G audio jasper-recon
    fi
    usermod -aG audio jasper-recon 2>/dev/null || true
    # WS1 Phase 4a — ensure jasper-secrets membership on UPGRADE too (the
    # useradd -G above is skipped when the user already exists, e.g. a Pi from a
    # pre-4a build). Idempotent; takes effect on the daemon's next start (the
    # deploy restarts jasper-voice; jasper-web is socket-activated so its next
    # spawn picks it up).
    if getent group jasper-secrets >/dev/null 2>&1; then
        usermod -aG jasper-secrets jasper-voice 2>/dev/null || true
        usermod -aG jasper-secrets jasper-web 2>/dev/null || true
    fi
    # WS1 Phase 4b — ensure jasper-intsecrets membership on UPGRADE too (the
    # useradd -G above is skipped when the user already exists). Idempotent;
    # takes effect on daemon restart/socket activation.
    if getent group jasper-intsecrets >/dev/null 2>&1; then
        usermod -aG jasper-intsecrets jasper-voice 2>/dev/null || true
        usermod -aG jasper-intsecrets jasper-control 2>/dev/null || true
        usermod -aG jasper-intsecrets jasper-mux 2>/dev/null || true
        usermod -aG jasper-intsecrets jasper-web 2>/dev/null || true
    fi
    # U3 / P6 — renderer users join `jts-ring` so a migrated lane's ioplug can
    # create and write its ring under /dev/shm/jts-ring. Guarded on the account
    # EXISTING: `pi` is the distro's login account, not one this installer
    # creates, and a box brought up with a custom user has no `pi` at all — a
    # bare usermod there would fail the install under `set -euo pipefail`. Same
    # shape as the `bluetooth` guard above, and idempotent on upgrade.
    #
    # This is INERT until a lane is armed: group membership grants the ability to
    # write the ring directory, it does not point any renderer at a ring. The
    # lane map (jasper.renderer_lanes) is what arms one.
    if getent group jts-ring >/dev/null 2>&1; then
        if getent passwd pi >/dev/null 2>&1; then
            usermod -aG jts-ring pi 2>/dev/null || true
        fi
        # U3/P6c — jasper-web's wizard-spawned aplay children create the
        # correction lane's ring while this box has the lane armed. The RUNTIME grant
        # comes from the unit's SupplementaryGroups= (systemd adds the
        # group to the process directly; the group need only exist). This
        # passwd record serves the non-systemd consumers — `sudo -u
        # jasper-web` probes, operator shells — and this file's own
        # convention that -G lists match each unit's SupplementaryGroups=.
        # The useradd -G above is skipped when the user already exists
        # (every pre-P6c-i box), hence this idempotent add — same shape as
        # the jasper-secrets/jasper-intsecrets upgrade blocks. It covers
        # jasper-correction-web too: that unit spawns the same aplay children
        # under this same account. The remaining correction-lane writer
        # identities (the streambox variant, operator CLIs) run as root and
        # write the 2775 root-owned directory regardless — the P6b root
        # exemption; their ring FILE mode comes from the shared spawn
        # helper's umask, not a group.
        usermod -aG jts-ring jasper-web 2>/dev/null || true
        # #2786 — jasper-control READS one ring header: /state's grouping
        # `ring` block opens /dev/shm/jts-ring/grouping.ring O_RDONLY for its
        # first 128 bytes. Unlike every other member here it never writes a
        # ring. Same shape as the jasper-web line above: the RUNTIME grant is
        # the unit's SupplementaryGroups=, and this passwd record serves the
        # non-systemd consumers plus this file's convention that the -G lists
        # match each unit. Takes effect on the daemon's next start, which the
        # deploy performs.
        usermod -aG jts-ring jasper-control 2>/dev/null || true
        # U3/P6d — shairport-sync writes the airplay lane's ring as its own
        # non-root user. Fresh boxes get the group at useradd time
        # (renderers.sh hard-lists it, which is safe because THIS function
        # creates the group first); this guarded usermod is the UPGRADE path
        # for boxes whose shairport-sync user predates P6d. Guarded on the
        # account existing because on a fresh box install_renderers has not
        # run yet at this point — same shape as the `pi` guard above.
        if getent passwd shairport-sync >/dev/null 2>&1; then
            usermod -aG jts-ring shairport-sync 2>/dev/null || true
        fi
    fi
    echo "  Service users ready: jasper-voice, jasper-mux, jasper-input, jasper-usbmic, jasper-control, jasper-web, jasper-recon (group: jasper; secrets: jasper-secrets = voice+web; intsecrets: jasper-intsecrets = voice+control+mux+web; ring writers: jts-ring = pi + jasper-web + shairport-sync, plus jasper-control as a header READER; bluealsa-aplay and the remaining root correction-lane identities write rings as root)"

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
