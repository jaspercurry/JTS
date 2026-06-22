#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Convert breizhn/DTLN-aec TFLite models to ONNX so they can run with
# the onnxruntime that ships in the JTS Pi venv (tflite-runtime has no
# Python 3.13 wheel — see install.sh comment + docs/HANDOFF-mic-quality-v2.md
# Phase 1 "What to build" notes).
#
# Output ONNX files go to $OUT_DIR (default: ./dtln-aec-onnx/).
# These should then be either:
#   (a) attached to a GitHub release on the JTS repo and downloaded
#       by install.sh / a registry module (the pattern jasper/wake_models.py
#       uses for jarvis_v2.onnx), or
#   (b) for spike testing: SCP'd directly to /var/lib/jasper/dtln/
#       on the Pi.
#
# The conversion has been verified (2026-05-22): TFLite vs ONNX outputs
# match within ~5e-5 (float32 precision noise). tf2onnx 1.17 is the
# minimum working version; tflite2onnx 0.4.1 fails on the SQUARE op
# the DTLN-aec graph uses for spectrogram magnitudes.
#
# Usage:
#   bash scripts/convert-dtln-aec.sh                       # convert 128 + 256
#   bash scripts/convert-dtln-aec.sh 128                   # just 128-unit
#   OUT_DIR=/tmp/foo bash scripts/convert-dtln-aec.sh

set -euo pipefail

OUT_DIR="${OUT_DIR:-./dtln-aec-onnx}"
SIZES_ARG="${1:-128 256}"
TFLITE_URL_BASE="https://github.com/breizhn/DTLN-aec/raw/main/pretrained_models"

mkdir -p "$OUT_DIR"
WORK_DIR="$(mktemp -d -t dtln-conversion.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Working in: $WORK_DIR"
echo "Output to:  $OUT_DIR"
echo ""

# Need Python 3.11 (tf2onnx + tensorflow are fussy about Python version).
# Fall back to whatever python3 is available; warn if not 3.11.
PYTHON=python3.11
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python3
    echo "Warning: python3.11 not found, falling back to: $(command -v $PYTHON)" >&2
fi

VENV="$WORK_DIR/.venv"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet tf2onnx tensorflow onnx onnxruntime numpy 2>&1 | tail -3

cd "$WORK_DIR"

for SIZE in $SIZES_ARG; do
    case "$SIZE" in
        128|256|512) ;;
        *) echo "Unknown size: $SIZE (expected 128 / 256 / 512)"; exit 2 ;;
    esac

    for STAGE in 1 2; do
        TFLITE="dtln_aec_${SIZE}_${STAGE}.tflite"
        ONNX="dtln_aec_${SIZE}_${STAGE}.onnx"

        echo "→ Downloading $TFLITE ..."
        curl -fLso "$TFLITE" "${TFLITE_URL_BASE}/${TFLITE}"

        echo "→ Converting → $ONNX ..."
        "$VENV/bin/python" -m tf2onnx.convert \
            --tflite "$TFLITE" \
            --output "$ONNX" \
            --opset 17 2>&1 | grep -E "Successfully|ERROR|FAILED" | head -1
    done

    echo "→ Verifying outputs match (TFLite vs ONNX) for size=$SIZE ..."
    "$VENV/bin/python" - "$SIZE" <<'PYEOF'
import sys
import numpy as np
import onnxruntime as ort
import tensorflow as tf

size = sys.argv[1]
np.random.seed(0)
all_ok = True

for stage in [1, 2]:
    tflite_path = f'dtln_aec_{size}_{stage}.tflite'
    onnx_path = f'dtln_aec_{size}_{stage}.onnx'

    interp = tf.lite.Interpreter(model_path=tflite_path)
    interp.allocate_tensors()
    tf_in = interp.get_input_details()
    tf_out = interp.get_output_details()
    sess = ort.InferenceSession(onnx_path)
    onnx_in = sess.get_inputs()

    feeds = {}
    for det in tf_in:
        shape = [d if d > 0 else 1 for d in det['shape']]
        feeds[det['name']] = np.random.randn(*shape).astype(np.float32)

    for det in tf_in:
        interp.set_tensor(det['index'], feeds[det['name']])
    interp.invoke()
    tf_outs = [interp.get_tensor(d['index']) for d in tf_out]

    onnx_feed = {oi.name: feeds[ti['name']] for oi, ti in zip(onnx_in, tf_in)}
    onnx_outs = sess.run(None, onnx_feed)

    for i, (a, b) in enumerate(zip(tf_outs, onnx_outs)):
        diff = float(np.abs(a - b).max())
        if diff < 1e-3:
            print(f"  stage {stage} output[{i}]: max diff = {diff:.2e}  OK")
        else:
            print(f"  stage {stage} output[{i}]: max diff = {diff:.2e}  *** FAIL ***")
            all_ok = False

sys.exit(0 if all_ok else 1)
PYEOF

    # Copy verified ONNX out of the temp dir
    cp "dtln_aec_${SIZE}_1.onnx" "dtln_aec_${SIZE}_2.onnx" "$OLDPWD/$OUT_DIR/"
    echo "→ Staged: $OUT_DIR/dtln_aec_${SIZE}_{1,2}.onnx"
    echo ""
done

echo "Done. ONNX files:"
ls -lh "$OLDPWD/$OUT_DIR/"*.onnx
