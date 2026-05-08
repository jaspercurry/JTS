#!/usr/bin/env bash
# Build the AMOLED satellite firmware and stage it to a known path.
# Idempotent. Safe to re-run. Mirrors firmware/dial/build.sh.
#
# First run downloads ~300 MB of PlatformIO toolchain (Espressif ESP32
# core + GCC for xtensa-esp32s3). Subsequent runs reuse the cache.
#
# Usage:
#   bash firmware/satellite-amoled/build.sh
#
# Then flash via either PlatformIO directly:
#   pio run -d firmware/satellite-amoled -t upload
# or (once it exists) via jasper-satellite-onboard.
#
# Where the bin lands (parallels firmware/dial/):
#   /opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin
#
# If you don't have PlatformIO yet:
#   pip install -U platformio
# Or use the project's venv:
#   /opt/jasper/.venv/bin/pip install platformio

set -euo pipefail

SAT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_NAME="waveshare-amoled-1p8"
DEST="/opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin"

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

echo "Building AMOLED satellite firmware via ${PIO}..."
"${PIO}" run --project-dir "${SAT_DIR}" -e "${ENV_NAME}"

# PlatformIO writes the merged-image binary here. esptool wants a single
# .bin starting at offset 0x0 — `firmware.bin` from PIO is exactly that
# (bootloader + partitions + app, merged at build time when the env
# uses the default partition table).
SRC="${SAT_DIR}/.pio/build/${ENV_NAME}/firmware.bin"
if [[ ! -f "${SRC}" ]]; then
    echo "Build succeeded but ${SRC} is missing. Inspect ${SAT_DIR}/.pio/build/${ENV_NAME}/." >&2
    exit 2
fi

# Stage to where future jasper-satellite-onboard will look, if we're
# root or have write access on the parent dir. Otherwise just print
# the path.
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
