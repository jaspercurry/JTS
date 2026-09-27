#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# mDNS discovery for deploy/install.sh: the jasper-control advert, the peering template and peer_id.

install_avahi_jasper_control() {
    # Advertise jasper-control over mDNS as the always-on discovery surface
    # used by the /rooms speaker directory and identity-aware automation.
    # The advertised file carries a name= TXT record with the
    # speaker's friendly display name (the /speaker identity), so the
    # /rooms directory shows friendly names. Because the name is a
    # per-runtime value, the file is RENDERED from a TEMPLATE rather
    # than copied statically: install the template OUT of
    # /etc/avahi/services/ (Avahi must not parse its __SPEAKER_NAME__
    # placeholder as XML — same reasoning as install_peering_template),
    # then let jasper.net.control_advert.render_control_advert substitute
    # the (XML-escaped) name and atomic-write the live file — Avahi picks
    # up the change via inotify. The /speaker save path re-renders on a
    # name change.
    install -d -m 0755 /etc/jasper/avahi-templates
    install -m 0644 \
        "${REPO_DIR}/deploy/avahi/jasper-control.service.template" \
        /etc/jasper/avahi-templates/jasper-control.service

    # A non-root jasper-control renders the peering advert
    # (jasper-peer.service) into this dir when /sound/pair/ peering is enabled
    # (off by default). os.replace needs WRITE on the parent dir, which
    # ReadWritePaths= does NOT grant (it only lifts ProtectSystem=strict;
    # POSIX dir perms still apply). So make the dir group-jasper writable +
    # setgid (new files inherit group jasper). The static control advert below
    # is still written by install.sh as root; a future avahi apt-upgrade could
    # reset this dir to root:root 0755, but every deploy re-applies it.
    install -d -m 2775 -g jasper /etc/avahi/services
    # Render the live service from the template via the Python module (it
    # does the XML-escape and atomic write; Avahi picks up the change via
    # inotify). The package is already pip-installed by install_jasper
    # above, so the import resolves here. render_control_advert is
    # fail-soft (returns False, never raises); we still guard the whole
    # call with `|| true` plus a static-file fallback so a render failure
    # can never leave _jasper-control._tcp un-advertised — /rooms and
    # jasper-doctor's "avahi: _jasper-control._tcp" check depend on it
    # always existing.
    local rendered=0
    if [[ -x "${INSTALL_DIR}/.venv/bin/python" ]] \
       && "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import sys

from jasper.net.control_advert import render_control_advert

# name=None -> read the current /speaker name (env-first then
# /var/lib/jasper/speaker_name.env), empty -> hostname default, so the
# TXT is never empty. render_control_advert only atomic-writes the file;
# Avahi picks up the change on its own via inotify.
sys.exit(0 if render_control_advert() else 1)
PY
    then
        rendered=1
        echo "  Advertised _jasper-control._tcp via avahi (port 8780, name= TXT)"
    fi

    if [[ "${rendered}" != "1" ]]; then
        # Fallback: the render didn't run (no venv yet) or failed. Drop
        # the static, name-less service file so the speaker still
        # advertises and the doctor check stays green. The friendly
        # name TXT is lost until the next successful render (e.g. the
        # next /speaker save or deploy), but discovery itself is intact.
        echo "  WARNING: control-advert render unavailable; installing static jasper-control.service (no name= TXT)"
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
    fi
}

ensure_peer_id() {
    # The identity peers key on across reboots. Regenerate only when there is
    # nothing usable: jasper/peering/config.py and scripts/_lib.sh's
    # verify_or_record_peer_id both accept any non-empty id, so replacing an
    # odd-looking one is what would abort the next deploy blaming a re-image.
    # The strip is that reader's twin, so a trailing CR is not "empty" here
    # and a recorded id there.
    local file="${STATE_DIR}/peer_id" pid tmp
    if [[ -f "${file}" ]] \
        && [[ -n "$(tr -d '[:space:]' 2>/dev/null < "${file}")" ]]; then
        return 0
    fi
    pid="$(tr -d '[:space:]' 2>/dev/null < /proc/sys/kernel/random/uuid || true)"
    if [[ -z "${pid}" ]]; then
        echo "  ERROR: could not generate peer_id (kernel uuid unreadable)" >&2
        exit 1
    fi
    tmp="$(mktemp "${STATE_DIR}/.peer_id.XXXXXX")"
    printf '%s\n' "${pid}" > "${tmp}"
    chmod 0644 "${tmp}"
    mv -f "${tmp}" "${file}"
    echo "  Generated stable peer_id at ${file}"
}

install_peering_template() {
    # Multi-device peering. The TEMPLATE goes under /etc/jasper/ so
    # Avahi doesn't try to parse it as a service file (the
    # placeholders __PEER_ID__ / __ROOM__ / __PRIMARY__ aren't valid
    # XML attribute values).
    #
    # jasper-control's peering daemon renders this template into
    # /etc/avahi/services/jasper-peer.service when JASPER_PEERING=on
    # is set in /var/lib/jasper/peering.env (via the /sound/pair/ Speakers
    # page). When peering is off (the default), no
    # rendered file exists and this Pi is invisible to siblings —
    # the goal property of "zero cost when alone".
    install -d -m 0755 /etc/jasper/avahi-templates
    install -m 0644 \
        "${REPO_DIR}/deploy/avahi/jasper-peer.service.template" \
        /etc/jasper/avahi-templates/jasper-peer.service
    ensure_state_dir
    ensure_peer_id
    echo "  Peering template installed; peering is OFF by default — enable at http://${JASPER_HOSTNAME:-jts.local}/sound/pair/"
}
