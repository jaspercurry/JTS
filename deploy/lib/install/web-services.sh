#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Management web surface for deploy/install.sh: TLS, landing page, nginx, polkit rules, writable dirs.

provision_correction_tls() {
    # The measurement pages require HTTPS because getUserMedia (mic capture)
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
    # See deploy/nginx-jasper.conf "Why HTTPS is added back" for context.
    local hostname="${JASPER_HOSTNAME:-jts.local}"
    local ca_dir=/var/lib/jasper/ca
    local ssl_dir=/etc/nginx/ssl
    install -d -m 0700 "${ca_dir}"
    install -d -m 0755 "${ssl_dir}"

    if [[ ! -f "${ca_dir}/ca.crt" || ! -f "${ca_dir}/ca.key" ]]; then
        echo "  generating measurement-page private CA at ${ca_dir}/ca.crt"
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
    echo "  measurement-page TLS provisioned (server cert for ${hostname}, CA at /usr/share/jasper-web/jts-root-ca.crt)"
}

install_management_static_assets() {
    local index_src="$1"
    local app_css_ver

    # Static landing page served at /. Plain HTML, no daemon — nginx
    # reads it directly via the `location = /` block in jasper.conf.
    # Updates require an `nginx -s reload` (handled by the caller)
    # but no service restart.
    install -d -m 0755 /usr/share/jasper-web
    install -m 0644 "${index_src}" /usr/share/jasper-web/index.html
    # Resolve the cache-bust SHA directly (deploy env, then the checkout, then
    # the prior manifest) rather than reading build.txt: the manifest is written
    # LAST, as the verified-install marker, so it still holds the PRIOR SHA at
    # this point in the run. resolve_build_sha_short returns the same value the
    # manifest will record, so the cache key matches the installed build.
    app_css_ver="$(resolve_build_sha_short)"
    [[ -n "${app_css_ver}" && "${app_css_ver}" != "unknown" ]] || app_css_ver="dev"
    # The renderer reads the control token itself, so it never reaches a shell
    # argument or the process table. The profile marker it needs was persisted
    # earlier in this run.
    if ! PYTHONPATH="${REPO_DIR}" python3 -m jasper.web.landing \
            /usr/share/jasper-web/index.html \
            --app-css-version "${app_css_ver}" \
            --hub-dir /usr/share/jasper-web; then
        echo "  ERROR: failed to render the landing page; refusing to ship a broken page" >&2
        return 1
    fi
    echo "  landing page + /sound/ and /assistant/ hubs: rendered (capabilities, control token, icon sprite)"
    # All /assets/ content (app.css, fonts, per-page CSS + ES modules) +
    # the .install-manifest the doctor verifies — see
    # deploy/lib/install/web-assets.sh for the copy shape and the
    # manifest contract.
    install_web_assets
}

tune_nginx_worker_processes() {
    # `worker_processes auto` starts one CPU-pinned worker per core to serve a
    # loopback-only management proxy: four workers, ~14 MB Pss on a Pi 5 and
    # three extra core-pinned processes on a realtime audio box. One is enough.
    #
    # This rewrites the packaged nginx.conf rather than dropping a file into
    # /etc/nginx/modules-enabled/, which nginx does include at main context:
    # worker_processes is a main-context directive and nginx rejects a second
    # copy with "is duplicate", which would fail the `nginx -t` gate on every
    # install. The cost of that choice is that nginx.conf is an nginx-common
    # dpkg conffile, so a later package upgrade reports it as locally modified
    # and keeps this copy. To undo on a box that already ran this, put
    # `worker_processes auto;` back in /etc/nginx/nginx.conf — dropping the
    # call here does not revert an installed box.
    #
    # Worker count is a comfort optimisation, so every failure path below
    # leaves the packaged value in place instead of failing the deploy.
    local main="${JTS_NGINX_MAIN_CONF:-/etc/nginx/nginx.conf}"
    if [[ ! -f "${main}" ]]; then
        echo "  ${main} not present; skipping nginx worker tuning."
        return 0
    fi
    local tmp mode
    if ! tmp="$(mktemp "${main}.jts.XXXXXX" 2>/dev/null)"; then
        echo "  WARN: could not stage an ${main} rewrite; workers left as packaged."
        return 0
    fi
    if ! sed -E 's/^([[:space:]]*)worker_processes[[:space:]]+[^;]+;/\1worker_processes 1;/' \
            "${main}" > "${tmp}"; then
        rm -f "${tmp}"
        echo "  WARN: could not rewrite ${main}; workers left as packaged."
        return 0
    fi
    if ! grep -qE '^[[:space:]]*worker_processes[[:space:]]+1;' "${tmp}"; then
        printf 'worker_processes 1;\n' >> "${tmp}"
    fi
    if cmp -s "${tmp}" "${main}"; then
        rm -f "${tmp}"
        return 0
    fi
    mode="$(stat -c '%a' "${main}" 2>/dev/null || stat -f '%Lp' "${main}" 2>/dev/null || true)"
    if ! { chmod "${mode:-644}" "${tmp}" && mv -f "${tmp}" "${main}"; }; then
        rm -f "${tmp}"
        echo "  WARN: could not publish ${main}; workers left as packaged."
        return 0
    fi
    echo "  nginx worker_processes pinned to 1 in ${main}"
}

install_nginx_site_conf() {
    # <site conf source> <nginx config root>. The site conf is a listener
    # shell; its routes live in deploy/nginx/ and go to ${root}/snippets/,
    # which it includes by absolute path, so both profiles share one body.
    # A conf in sites-enabled is on disk at once and the next nginx restart
    # loads it (Restart=always, see nginx.service.d/jts-recovery.conf), so
    # what `nginx -t` rejects is put back — site conf and every snippet —
    # from a fixed-name snapshot dir outside sites-enabled, which nginx.conf
    # includes unfiltered. Snippet basenames are unique, so the flat snapshot
    # is unambiguous. Drop this guard once the conf ships from a package that
    # tests before enabling.
    local src="${1}" root="${2}" prev="${2}/.jasper-site-prev" rel="" snip=""
    local site="sites-enabled/jasper.conf"
    local -a snips=("${REPO_DIR}"/deploy/nginx/*.conf)
    local -a rels=("${site}")
    for snip in "${snips[@]}"; do
        rels+=("snippets/${snip##*/}")
    done
    rm -rf "${prev}"
    install -d -m 0755 "${root}/snippets" "${prev}"
    for rel in "${rels[@]}"; do
        [[ -f "${root}/${rel}" ]] || continue
        cp -a "${root}/${rel}" "${prev}/"
    done
    for snip in "${snips[@]}"; do
        install -m 0644 "${snip}" "${root}/snippets/${snip##*/}"
    done
    install -m 0644 "${src}" "${root}/${site}"
    # nginx-light's enabled `default` site clashes with our default_server.
    rm -f "${root}/sites-enabled/default"
    if ! nginx -t; then
        echo "  ERROR: event=install.nginx_conf_rejected src=${src}" >&2
        for rel in "${rels[@]}"; do
            rm -f "${root}/${rel}"
            [[ -f "${prev}/${rel##*/}" ]] || continue
            cp -a "${prev}/${rel##*/}" "${root}/${rel}"
        done
        rm -rf "${prev}"
        return 1
    fi
    rm -rf "${prev}"
    systemctl enable --now nginx 2>/dev/null || true
    systemctl reload nginx
}

NGINX_PUBLIC_SURFACE="http://<host>/{,sources/,sound/,assistant/,system/} + https://<host>/sound/{speaker/crossover/,measurements/,bass/,pair/sync/} are live"

install_nginx_site() {
    # Standalone nginx site that reverse-proxies /spotify/ (multi-account
    # OAuth web flow) and /assistant/voice/ (voice-provider config wizard)
    # on plain HTTP. The /sound/* measurement routes are proxied on both
    # listeners, but browser mic capture only works on the HTTPS one:
    # getUserMedia grants mic access in a secure context only. That origin is
    # the installer's own self-signed cert, so it is entered deliberately and
    # never by redirect — a cert interstitial is un-automatable (issue #2632).
    # The legacy routes stay HTTP — Spotify's HTTPS requirement is satisfied
    # by the GitHub Pages bounce, and there's no point breaking working flows
    # for one feature. Google rejects mDNS redirect URIs, so /assistant/google/
    # stays HTTP and its bounce returns to /google/callback over HTTP. The
    # correction-only cert is provisioned by provision_correction_tls() before
    # this function runs.
    install_management_static_assets "${REPO_DIR}/deploy/index.html"
    tune_nginx_worker_processes
    install_nginx_site_conf "${REPO_DIR}/deploy/nginx-jasper.conf" /etc/nginx
    echo "  nginx reloaded — ${NGINX_PUBLIC_SURFACE}"
}

install_streambox_nginx_site() {
    # Streambox uses the normal JTS landing page with capability-gated cards,
    # plus an nginx route set limited to local sources, DSP, grouping, and
    # system health. That keeps the frontend shared while omitting voice/wake
    # surfaces whose daemons are intentionally absent from this profile.
    install_management_static_assets "${REPO_DIR}/deploy/index.html"
    tune_nginx_worker_processes
    install_nginx_site_conf "${REPO_DIR}/deploy/nginx-jasper-streambox.conf" /etc/nginx
    echo "  streambox nginx reloaded — ${NGINX_PUBLIC_SURFACE}"
}

install_jasper_control_polkit() {
    # The polkit grant for the non-root jasper-control user.
    # Without it, every systemctl/reboot/poweroff jasper-control runs (the
    # in-process restart broker + the system/shairport/grouping supervisors +
    # the /system buttons) is DENIED with "Interactive authentication required"
    # — silently breaking the Tier-3/Tier-5 recovery paths. polkitd monitors
    # /etc/polkit-1/rules.d and auto-reloads on change, so no reload/restart is
    # needed (a daemon-reload is for systemd units, not polkit). See
    # deploy/polkit/49-jasper-control.rules.
    install -d -m 0755 /etc/polkit-1/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/polkit/49-jasper-control.rules" \
        /etc/polkit-1/rules.d/49-jasper-control.rules
    echo "  Installed polkit rule for jasper-control (manage-units allowlist + reboot/power-off)"
}

install_jasper_web_polkit() {
    # The polkit grant for the non-root jasper-web user. The
    # /wifi/ wizard drives NetworkManager (scan / connect / forget / radio /
    # PSK re-read); NM's implicit defaults DENY a sessionless daemon for every
    # one of those, so without this rule a non-root jasper-web cannot manage
    # Wi-Fi — the worst-case brick for a headless, often Ethernet-less speaker.
    # polkitd monitors /etc/polkit-1/rules.d and auto-reloads (no restart). See
    # deploy/polkit/49-jasper-web.rules.
    install -d -m 0755 /etc/polkit-1/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/polkit/49-jasper-web.rules" \
        /etc/polkit-1/rules.d/49-jasper-web.rules
    echo "  Installed polkit rule for jasper-web (NetworkManager wifi management)"
}

widen_jasper_web_writable_dirs() {
    # The non-root jasper-web user atomically replaces files in
    # two root-owned dirs: /etc/bluetooth/main.conf (BlueZ name persistence
    # across a bluetooth.service restart — the /speaker rename) and generated
    # CamillaDSP sound profiles under /var/lib/camilladsp/configs (the /sound/
    # EQ editor). os.replace() needs WRITE on the *directory*, so make both
    # root:jasper 2775 (setgid → new files inherit group jasper). Mirrors
    # install_avahi_jasper_control's /etc/avahi/services widening. The
    # ordinary sound-profile files inside keep their own owners (root reads/writes
    # them fine; the group-writable dir is what lets the dropped daemon swap them
    # atomically). Every generated YAML is also read by jasper-control /state or
    # jasper-web, so repair stale root:root 0600 files from earlier builds to
    # root:jasper 0640. The shared DSP-apply lock is written by root CLIs and
    # non-root web flows, so it must be group-writable.
    # Idempotent.
    if [[ -d /etc/bluetooth ]]; then
        chgrp jasper /etc/bluetooth 2>/dev/null || true
        chmod 2775 /etc/bluetooth 2>/dev/null || true
    fi
    install -d -m 2775 -g jasper /var/lib/camilladsp/configs
    touch /var/lib/camilladsp/configs/.dsp_apply.lock
    chgrp jasper /var/lib/camilladsp/configs/.dsp_apply.lock 2>/dev/null || true
    chmod 0660 /var/lib/camilladsp/configs/.dsp_apply.lock 2>/dev/null || true
    find /var/lib/camilladsp/configs -maxdepth 1 -type f -name '*.yml' \
        -exec chgrp jasper {} + -exec chmod 0640 {} + 2>/dev/null || true
    # The Layer-A SSOT (active_speaker_baseline_profile.json) and the Active
    # run-record locks + records are NOT healed here: a path-following
    # chgrp/chmod under a group-writable /var/lib/jasper is a local priv-esc
    # (a group member can pre-create the name as a symlink onto a root file).
    # heal_shared_state_modes (deploy/lib/install/state-and-secrets.sh) owns
    # them and pins each inode with O_NOFOLLOW+fstat before touching it.
    echo "  Widened /etc/bluetooth + /var/lib/camilladsp/configs to root:jasper 2775 (jasper-web writes)"
}
