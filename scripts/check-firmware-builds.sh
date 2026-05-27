#!/usr/bin/env bash
# Explicit maintainer check for optional ESP32 satellite firmware.
#
# Normal JTS installs do not build these projects: most speakers do not
# have a dial or AMOLED satellite attached, and first-run PlatformIO
# setup is a large accessory-specific download. Run this when touching
# firmware source, PlatformIO pins, or accessory onboarding code.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
    cat <<'EOF'
Usage: scripts/check-firmware-builds.sh [dial|satellite-amoled]...

Builds optional ESP32 firmware projects without flashing hardware.
With no arguments, builds all supported firmware projects.

Requires PlatformIO on PATH, /opt/jasper/.venv/bin/pio, or
/home/pi/.platformio/penv/bin/pio.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if ! command -v pio >/dev/null 2>&1 \
   && [[ ! -x /opt/jasper/.venv/bin/pio ]] \
   && [[ ! -x /home/pi/.platformio/penv/bin/pio ]]; then
    usage >&2
    echo >&2
    echo "PlatformIO not found. Install with:" >&2
    echo "    pip install -U platformio" >&2
    echo "or on the Pi:" >&2
    echo "    sudo /opt/jasper/.venv/bin/pip install platformio" >&2
    exit 1
fi

targets=("$@")
if [[ ${#targets[@]} -eq 0 ]]; then
    targets=(dial satellite-amoled)
fi

for target in "${targets[@]}"; do
    case "${target}" in
        dial)
            script="${REPO_ROOT}/firmware/dial/build.sh"
            ;;
        satellite-amoled)
            script="${REPO_ROOT}/firmware/satellite-amoled/build.sh"
            ;;
        *)
            echo "Unknown firmware target: ${target}" >&2
            usage >&2
            exit 2
            ;;
    esac

    echo "==> Building ${target} firmware"
    bash "${script}"
done
