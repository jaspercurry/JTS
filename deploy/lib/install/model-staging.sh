#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Model asset staging helpers for deploy/install.sh.
#
# install_jasper calls these after the venv + editable install exist.
# The Python module owns the shared exists/hash/download/failure-count
# logic so pytest can exercise it without sourcing the root installer.

stage_openwakeword_assets() {
    local openwakeword_models_dir
    openwakeword_models_dir="$(
        "${INSTALL_DIR}/.venv/bin/python" -c 'import importlib.util, pathlib; spec = importlib.util.find_spec("openwakeword");
if spec is None or spec.origin is None:
    raise SystemExit("openwakeword package not installed")
print(pathlib.Path(spec.origin).resolve().parent / "resources" / "models")'
    )"
    install -d -m 0755 -o root -g root "${openwakeword_models_dir}"
    OPENWAKEWORD_MODELS_DIR="${openwakeword_models_dir}" \
        "${INSTALL_DIR}/.venv/bin/python" -m jasper.model_downloads \
            stage --registry openwakeword --required
}

stage_wake_models() {
    ensure_state_dir
    install -d -m 0755 -o root -g root /var/lib/jasper/wake
    # Wake-event telemetry directory (HANDOFF-wake-telemetry.md PR 3).
    # Holds wake-events.sqlite3 + per-event WAVs. jasper-voice (running
    # as root via the service unit) creates files mode 0644; future
    # /wake-review/ web UI reads via the standard nginx proxy.
    install -d -m 0755 -o root -g root /var/lib/jasper/wake-events
    if ! "${INSTALL_DIR}/.venv/bin/python" -m jasper.model_downloads \
            stage --registry wake --optional \
            --optional-timeout 30 --optional-retries 2; then
        echo "  warning: one or more wake-word model downloads failed"
        echo "  affected registry rows may remain unavailable"
        echo "  re-run install.sh once you're online to retry the downloads"
    fi
}

stage_dtln_models() {
    ensure_state_dir
    install -d -m 0755 -o root -g root /var/lib/jasper/dtln
    if ! "${INSTALL_DIR}/.venv/bin/python" -m jasper.model_downloads \
            stage --registry dtln --optional \
            --optional-timeout 30 --optional-retries 2; then
        echo "  warning: one or more DTLN model downloads failed"
        echo "  the bridge will fall back to dual-stream (AEC ON + AEC OFF only)"
        echo "  re-run install.sh once you're online to retry the downloads, or"
        echo "  fetch the two ONNX assets manually from the public release:"
        echo "    base=https://github.com/jaspercurry/JTS/releases/download/dtln-models-v1"
        echo "    sudo curl -fL \"\$base/dtln_aec_256_1.onnx\" -o /var/lib/jasper/dtln/dtln_aec_256_1.onnx"
        echo "    sudo curl -fL \"\$base/dtln_aec_256_2.onnx\" -o /var/lib/jasper/dtln/dtln_aec_256_2.onnx"
        echo "    # or: sudo gh release download dtln-models-v1 --repo jaspercurry/JTS --dir /var/lib/jasper/dtln"
        echo "    sudo systemctl restart jasper-aec-bridge"
    fi
}

seed_default_wake_model_env() {
    "${INSTALL_DIR}/.venv/bin/python" -m jasper.model_downloads seed-wake-default || true
}
