#!/usr/bin/env bash
# Build phase-1 dial firmware and stage it where jasper-dial-onboard
# expects to find the bin. Idempotent. Safe to re-run.
#
# First run downloads ~300 MB of PlatformIO toolchain (Espressif ESP32
# core + GCC for xtensa-esp32s3). Subsequent runs reuse the cache.
#
# Usage:
#   bash firmware/dial/build.sh
#
# Then provision a connected dial with:
#   sudo /opt/jasper/.venv/bin/jasper-dial-onboard
#
# Where the bin lands:
#   /opt/jasper/firmware/dial/jasper-dial.bin   (jasper-dial-onboard default)
#
# If you don't have PlatformIO yet:
#   pip install -U platformio
# Or use the project's venv:
#   /opt/jasper/.venv/bin/pip install platformio

set -euo pipefail

DIAL_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_NAME="crowpanel-128-rotary-hmi"
DEST="/opt/jasper/firmware/dial/jasper-dial.bin"

# Resolve `pio` — prefer the system one, fall back to the JTS venv.
if command -v pio >/dev/null 2>&1; then
    PIO=pio
elif [[ -x /opt/jasper/.venv/bin/pio ]]; then
    PIO=/opt/jasper/.venv/bin/pio
else
    echo "PlatformIO not found. Install with:" >&2
    echo "    /opt/jasper/.venv/bin/pip install platformio" >&2
    exit 1
fi

echo "Building dial firmware via ${PIO}..."
"${PIO}" run --project-dir "${DIAL_DIR}" -e "${ENV_NAME}"

# PlatformIO writes the merged-image binary here. esptool wants a single
# .bin starting at offset 0x0 — `firmware.bin` from PIO is exactly that
# (bootloader + partitions + app, merged at build time when the env
# uses the default partition table).
SRC="${DIAL_DIR}/.pio/build/${ENV_NAME}/firmware.bin"
if [[ ! -f "${SRC}" ]]; then
    echo "Build succeeded but ${SRC} is missing. Inspect ${DIAL_DIR}/.pio/build/${ENV_NAME}/." >&2
    exit 2
fi

# Stage to where jasper-dial-onboard expects, if we're root or have
# write access on the parent dir. Otherwise just print the path.
DEST_DIR="$(dirname "${DEST}")"
if [[ "${EUID}" -eq 0 ]] || { [[ -d "${DEST_DIR}" ]] && [[ -w "${DEST_DIR}" ]]; }; then
    install -d -m 0755 "${DEST_DIR}"
    install -m 0644 "${SRC}" "${DEST}"
    echo "Staged firmware to ${DEST}"
else
    echo
    echo "Build complete: ${SRC}"
    echo "Run as root to stage to ${DEST}, or copy manually:"
    echo "    sudo install -m 0644 ${SRC} ${DEST}"
fi
