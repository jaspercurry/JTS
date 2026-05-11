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

# Rsync — same exclude set documented in CLAUDE.md. Adding --info=stats1
# to keep the output reasonable.
rsync -avz --delete --info=stats1 \
    --exclude .venv --exclude __pycache__ --exclude '.git/' --exclude 'logs/*' \
    --exclude '.pio' --exclude '.claude/worktrees' --exclude '.claude/' \
    --exclude 'captures/*' --exclude '*.pyc' \
    ./ "${PI_USER}@${PI_HOST}:/home/pi/jts/"

if [[ "${SKIP_INSTALL:-}" == "1" ]]; then
    echo "==> SKIP_INSTALL=1 — rsync only, not running install.sh"
    exit 0
fi

# Run install.sh under sudo, passing the captured git info as env
# vars. sudo strips most env by default; explicitly preserve ours
# with `sudo VAR=value VAR=value command`. install.sh's build-manifest
# block reads these and prefers them over its REPO_DIR/.git fallback.
echo "==> Running install.sh on ${PI_HOST}..."
ssh "${PI_USER}@${PI_HOST}" \
    "sudo JASPER_DEPLOY_SHA='${SHA}${DIRTY}' \
          JASPER_DEPLOY_SHA_FULL='${SHA_FULL}${DIRTY}' \
          JASPER_DEPLOY_BRANCH='${BRANCH}' \
          bash /home/pi/jts/deploy/install.sh"

echo "==> Build manifest now on Pi:"
ssh "${PI_USER}@${PI_HOST}" 'sudo cat /var/lib/jasper/build.txt 2>/dev/null || echo "(not present)"'

echo "==> Done."
