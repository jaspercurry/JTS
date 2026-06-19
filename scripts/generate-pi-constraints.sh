#!/usr/bin/env bash
# Generate deploy/constraints-pi.txt from the LIVE Pi's resolved
# dependency set, so install.sh can replay exactly those versions on
# the next deploy instead of letting open-ranged pyproject deps
# (openai>=, scipy>=, onnxruntime>=, ...) drift to whatever PyPI has
# newest that morning. Closes the "Known limitation / follow-up" note
# above install.sh's pip pin.
#
# Why generated ON the Pi (and never on the laptop): arm64 + the Pi's
# Python (3.13 on PiOS Trixie) resolve different wheels and sometimes
# different versions than a macOS/x86 laptop. A laptop-side `pip
# freeze` would be a lie about what the speaker actually runs. This
# script only SSHes and reads — the lockfile content is entirely
# Pi-produced.
#
# Workflow (generate → review → commit):
#   1. Deploy + verify a known-good build on the Pi (jasper-doctor green).
#   2. bash scripts/generate-pi-constraints.sh
#   3. git diff deploy/constraints-pi.txt   # review version movements
#   4. Commit the file. From then on, every install.sh run that sees
#      deploy/constraints-pi.txt passes it to pip via `-c`, pinning the
#      whole tree. No file → installs behave exactly as before.
#
# Regenerate whenever you deliberately move dependencies (pyproject
# range bump, new package, Python upgrade on the Pi). If a later
# pyproject change conflicts with a stale pin, pip fails loudly at
# install time — that's the prompt to re-run this script.
#
# Filtering: `pip freeze` on the Pi includes the editable jasper
# install and locally-built bindings as direct references
# (`jasper-speaker @ file:///opt/jasper`, `-e ...`). Those aren't valid
# in a pip constraints file (and we never want to pin ourselves), so
# anything that isn't a plain `name==version` pin is dropped. Debian's
# python3-flatbuffers package can also leak into the venv as
# `flatbuffers==20181003210633`; pip cannot replay that non-PyPI version
# from PyPI, so drop it and let onnxruntime resolve a published
# flatbuffers wheel.
#
# Usage:
#   bash scripts/generate-pi-constraints.sh
#   PI_HOST=192.168.1.42 bash scripts/generate-pi-constraints.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${SCRIPT_DIR}/_lib.sh"

PI_PIP="/opt/jasper/.venv/bin/pip"
OUT_FILE="${REPO_ROOT}/deploy/constraints-pi.txt"

echo "==> reading resolved dependency set from ${PI_USER}@${PI_HOST}:${PI_PIP}"

# One SSH round-trip for everything we need: the freeze plus the
# provenance facts (python/pip versions, deployed build). `pip freeze`
# already excludes pip/setuptools/wheel; install.sh pins those two
# explicitly and separately.
remote_out="$(ssh -o ConnectTimeout=10 "${PI_USER}@${PI_HOST}" \
    "set -e
     echo \"PY: \$(/opt/jasper/.venv/bin/python --version 2>&1)\"
     echo \"PIP: \$(${PI_PIP} --version)\"
     echo \"BUILD: \$(sudo cat /var/lib/jasper/build.txt 2>/dev/null | tr '\n' ' ' || echo unknown)\"
     echo FREEZE-BEGIN
     ${PI_PIP} freeze")"

py_version="$(sed -n 's/^PY: //p' <<<"${remote_out}" | head -1)"
pip_version="$(sed -n 's/^PIP: //p' <<<"${remote_out}" | head -1)"
build_info="$(sed -n 's/^BUILD: //p' <<<"${remote_out}" | head -1)"
build_info="$(sed 's/[[:space:]]*$//' <<<"${build_info}")"
freeze="$(sed -n '/^FREEZE-BEGIN$/,$p' <<<"${remote_out}" | tail -n +2)"

# Keep only plain `name==version` pins. Editable installs (`-e ...`)
# and direct references (`pkg @ file://...`) are invalid constraint
# lines; comments/blank lines carry nothing. Drop known distro-only
# freeze values that pip cannot install from PyPI.
pins="$(
    grep -E '^[A-Za-z0-9._-]+==' <<<"${freeze}" \
        | grep -v -Fx 'flatbuffers==20181003210633' \
        | LC_ALL=C sort -f || true
)"

if [[ -z "${pins}" ]]; then
    echo "error: pip freeze on ${PI_HOST} produced no name==version pins" >&2
    exit 1
fi

{
    echo "# deploy/constraints-pi.txt — Pi-generated pip constraints."
    echo "#"
    echo "# GENERATED FILE — do not hand-edit version pins. Regenerate with:"
    echo "#   bash scripts/generate-pi-constraints.sh"
    echo "# then review the diff and commit (see the script header for the"
    echo "# full generate -> review -> commit workflow)."
    echo "#"
    echo "# Provenance:"
    echo "#   generated-at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "#   source-pi:    ${PI_USER}@${PI_HOST}"
    echo "#   pi-python:    ${py_version:-unknown}"
    echo "#   pi-pip:       ${pip_version:-unknown}"
    echo "#   pi-build:     ${build_info:-unknown}"
    echo "#"
    echo "# install.sh passes this file to pip via -c ONLY when it exists;"
    echo "# deleting it reverts deploys to open-range resolution."
    echo "${pins}"
} > "${OUT_FILE}"

count="$(grep -c '==' "${OUT_FILE}")"
echo "==> wrote ${OUT_FILE} (${count} pins)"
echo "    review:  git diff deploy/constraints-pi.txt"
echo "    then commit — the next deploy picks it up automatically."
