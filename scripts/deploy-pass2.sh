#!/usr/bin/env bash
# Targeted deploy script for the multi-user Spotify web setup feature.
# Idempotent — re-runs are safe.
#
# Run via:
#   ssh pi@jasper.local "echo 'pipass' | sudo -S bash /tmp/deploy-pass2.sh"
#
# Assumes /home/pi/jts/ has a current rsync of the repo.

set -euo pipefail

SRC="${SRC:-/home/pi/jts}"
INSTALL_DIR="/opt/jasper"
SYSTEMD_DIR="/etc/systemd/system"
ENV_FILE="/etc/jasper/jasper.env"

echo "==> rsync /opt/jasper/jasper from /home/pi/jts/jasper"
rsync -a --delete --exclude __pycache__ "${SRC}/jasper/" "${INSTALL_DIR}/jasper/"

echo "==> install jasper-web.service systemd unit"
install -m 0644 \
    "${SRC}/deploy/jasper-web.service" \
    "${SYSTEMD_DIR}/jasper-web.service"

echo "==> ensure self-signed cert at /etc/nginx/ssl/jasper.{crt,key}"
install -d -m 0755 /etc/nginx/ssl
if [[ ! -f /etc/nginx/ssl/jasper.crt ]]; then
    openssl req -x509 -nodes -days 3650 \
        -newkey rsa:2048 \
        -keyout /etc/nginx/ssl/jasper.key \
        -out /etc/nginx/ssl/jasper.crt \
        -subj "/CN=jasper.local" \
        -addext "subjectAltName=DNS:jasper.local,DNS:jasper,IP:127.0.0.1" \
        2>/dev/null
    chmod 0644 /etc/nginx/ssl/jasper.crt
    chmod 0640 /etc/nginx/ssl/jasper.key
    chgrp www-data /etc/nginx/ssl/jasper.key 2>/dev/null || true
fi

echo "==> install /etc/nginx/jasper-locations.conf and HTTPS site"
install -m 0644 \
    "${SRC}/deploy/nginx-jasper.conf" \
    /etc/nginx/jasper-locations.conf
install -m 0644 \
    "${SRC}/deploy/nginx-jasper-https.conf" \
    /etc/nginx/sites-enabled/jasper-https.conf

MOODE_SITE="/etc/nginx/sites-enabled/moode-http.conf"
INCLUDE_LINE='	include /etc/nginx/jasper-locations.conf;'

if [[ -f "${MOODE_SITE}" ]] && ! grep -qF "jasper-locations.conf" "${MOODE_SITE}"; then
    echo "==> add include directive to ${MOODE_SITE}"
    # Backup must NOT land in /etc/nginx/sites-enabled/ — nginx
    # includes everything there and would error on duplicate server.
    install -d -m 0755 /etc/nginx/backups
    cp "${MOODE_SITE}" "/etc/nginx/backups/moode-http.conf.pre-jasper.$(date +%s)"
    awk -v line="${INCLUDE_LINE}" '
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
    ' "${MOODE_SITE}" > "${MOODE_SITE}.tmp"
    mv "${MOODE_SITE}.tmp" "${MOODE_SITE}"
fi

echo "==> nginx -t && reload"
nginx -t
systemctl reload nginx

# Update SPOTIFY_REDIRECT_URI to the public URL so the OAuth callback
# arrives via nginx → jasper-web. Idempotent.
if [[ -f "${ENV_FILE}" ]]; then
    NEW_URI="https://jasper.local/spotify/callback"
    if ! grep -qF "SPOTIFY_REDIRECT_URI=${NEW_URI}" "${ENV_FILE}"; then
        echo "==> update SPOTIFY_REDIRECT_URI in ${ENV_FILE}"
        cp "${ENV_FILE}" "${ENV_FILE}.pre-pass2.$(date +%s)"
        sed -i -E \
            "s|^SPOTIFY_REDIRECT_URI=.*|SPOTIFY_REDIRECT_URI=${NEW_URI}|" \
            "${ENV_FILE}"
        if ! grep -q '^SPOTIFY_REDIRECT_URI=' "${ENV_FILE}"; then
            echo "SPOTIFY_REDIRECT_URI=${NEW_URI}" >> "${ENV_FILE}"
        fi
    fi
fi

echo "==> systemd daemon-reload + enable + start jasper-web"
systemctl daemon-reload
systemctl enable jasper-web.service
systemctl restart jasper-web.service

sleep 1
systemctl is-active jasper-web.service

echo
echo "Deployed. Test:"
echo "  curl -s http://127.0.0.1:8765/                # local: jasper-web direct"
echo "  curl -k -s https://jasper.local/spotify/      # public: through nginx (cert is self-signed)"
echo
echo "MANUAL STEPS REQUIRED (one-time):"
echo "  1. Add this redirect URI to your Spotify Developer App:"
echo "       https://jasper.local/spotify/callback"
echo "     https://developer.spotify.com/dashboard → your app → Edit settings"
echo "  2. Paste your app's Client ID and Client Secret into /etc/jasper/jasper.env"
echo "       (lines for SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)"
echo "  3. systemctl restart jasper-web jasper-voice"
echo "  4. Visit https://jasper.local/spotify on your phone (accept cert warning once)"
