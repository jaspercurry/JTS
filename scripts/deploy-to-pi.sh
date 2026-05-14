#!/usr/bin/env bash
# Deploy the local checkout to the Pi and run install.sh, capturing
# the git SHA/branch up front so the /system dashboard's "Software"
# card shows the real version instead of "unknown".
#
# The standard rsync excludes .git/ for size + speed, which means
# install.sh can't read git info on the Pi side. This wrapper captures
# the info on the laptop *before* rsync and passes it through as
# JASPER_DEPLOY_SHA / JASPER_DEPLOY_SHA_FULL / JASPER_DEPLOY_BRANCH
# env vars on the sudo invocation. install.sh's build-manifest block
# honors those over its local-git fallback.
#
# Usage:
#   bash scripts/deploy-to-pi.sh
#   PI_HOST=192.168.1.42 bash scripts/deploy-to-pi.sh
#   PI_USER=pi PI_HOST=jts.local bash scripts/deploy-to-pi.sh
#
# Skip the install step (just rsync) with:
#   SKIP_INSTALL=1 bash scripts/deploy-to-pi.sh
#
# After install completes, prints the resulting build manifest from
# the Pi so you can verify the SHA landed.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$REPO_ROOT"

# Capture git info BEFORE rsync (which excludes .git/).
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "deploy-to-pi: $REPO_ROOT is not a git checkout" >&2
    exit 1
fi
SHA=$(git rev-parse --short HEAD)
SHA_FULL=$(git rev-parse HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
DIRTY=""
if ! git diff-index --quiet HEAD --; then
    DIRTY="-dirty"
fi

echo "==> deploy-to-pi: ${PI_USER}@${PI_HOST}"
echo "    branch: ${BRANCH}"
echo "    sha:    ${SHA}${DIRTY} (${SHA_FULL})"

# Rsync — same exclude set documented in CLAUDE.md.
# macOS ships BSD rsync 2.6.9 (no --info= flag); use --stats which
# works on both BSD and GNU rsync. Suppress per-file output with
# --quiet so the wrapper's output is just the start/end summary.
rsync -az --delete --stats --quiet \
    --exclude .venv --exclude __pycache__ --exclude '.git/' --exclude 'logs/*' \
    --exclude '.pio' --exclude '.claude/worktrees' --exclude '.claude/' \
    --exclude 'captures/*' --exclude '*.pyc' \
    --exclude 'jasper_speaker.egg-info' --exclude '*.egg-info' \
    ./ "${PI_USER}@${PI_HOST}:/home/pi/jts/"

if [[ "${SKIP_INSTALL:-}" == "1" ]]; then
    echo "==> SKIP_INSTALL=1 — rsync only, not running install.sh"
    exit 0
fi

# Run install.sh under sudo, passing the captured git info as env
# vars. sudo strips most env by default; explicitly preserve ours
# with `sudo VAR=value VAR=value command`. install.sh's build-manifest
# block reads these and prefers them over its REPO_DIR/.git fallback.
#
# JASPER_HOSTNAME is also forwarded so install.sh's TLS-cert block
# generates a server cert with the right CN/SAN for non-default
# speaker hostnames (jts2.local, jts-kitchen.local, etc.). Without
# this, every redeploy to a non-default Pi clobbers a previously
# correct cert with one for "jts.local". The fallback to PI_HOST means
# operators who set only one of the two env vars still get a coherent
# install — PI_HOST is what we just SSH'd to, so it's always the right
# answer for the speaker's hostname.
HOSTNAME_FOR_INSTALL="${JASPER_HOSTNAME:-${PI_HOST}}"
echo "==> Running install.sh on ${PI_HOST}..."
ssh "${PI_USER}@${PI_HOST}" \
    "sudo JASPER_DEPLOY_SHA='${SHA}${DIRTY}' \
          JASPER_DEPLOY_SHA_FULL='${SHA_FULL}${DIRTY}' \
          JASPER_DEPLOY_BRANCH='${BRANCH}' \
          JASPER_HOSTNAME='${HOSTNAME_FOR_INSTALL}' \
          bash /home/pi/jts/deploy/install.sh"

echo "==> Build manifest now on Pi:"
ssh "${PI_USER}@${PI_HOST}" 'sudo cat /var/lib/jasper/build.txt 2>/dev/null || echo "(not present)"'

# Restart/reconcile the Python daemons that run application code so a
# code change in this deploy actually takes effect. install.sh already
# restarts jasper-mux + jasper-input + the wizard sockets. Voice is
# mic-hardware-dependent, so do not restart jasper-voice directly here:
# `jasper-aec-reconcile` restarts it when a valid mic path exists and
# parks it cleanly when no configured mic is present.
#
# Notable omissions:
#   - jasper-camilla — runs the Rust camilladsp binary, no Python.
#     No restart needed for Python code changes.
#   - The wizard servers — socket-activated, naturally pick up new
#     code on the next request.
if [[ "${SKIP_RESTART:-}" == "1" ]]; then
    echo "==> SKIP_RESTART=1 — leaving daemons on prior code"
    echo "==> Done."
    exit 0
fi

echo "==> Restarting code daemon: jasper-control.service"
ssh "${PI_USER}@${PI_HOST}" "sudo systemctl restart jasper-control.service" || \
    echo "  (jasper-control restart returned non-zero — see scripts/fetch-pi-logs.sh)"

echo "==> Reconciling mic/AEC/voice state"
ssh "${PI_USER}@${PI_HOST}" "sudo systemctl start jasper-aec-reconcile.service" || \
    echo "  (jasper-aec-reconcile returned non-zero — see scripts/fetch-pi-logs.sh)"

echo "==> Done."
