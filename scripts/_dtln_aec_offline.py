#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run DTLN-aec offline against a (mic, ref) WAV pair, writing the
cleaned mono output to a new WAV. Mirrors breizhn/DTLN-aec's
run_aec.py algorithm verbatim but uses onnxruntime instead of the
TFLite interpreter (the Pi can't load TFLite under Python 3.13).

Validates the conversion + the inference path against Jasper's
actual baseline audio BEFORE any bridge integration. If the cleaned
output sounds reasonable + scores well on the wake-word model, we
know the planned bridge changes are worth doing.

Usage:
  python scripts/_dtln_aec_offline.py \\
      --mic   reference-conditions/whisper-music/aec-off.wav \\
      --ref   reference-conditions/whisper-music/reference.wav \\
      --out   reference-conditions/whisper-music/aec-dtln-128.wav

  python scripts/_dtln_aec_offline.py \\
      --mic   reference-conditions/whisper-music/aec-off.wav \\
      --ref   reference-conditions/whisper-music/reference.wav \\
      --out   reference-conditions/whisper-music/aec-dtln-256.wav \\
      --model-size 256

Run scripts/convert-dtln-aec.sh first to produce the ONNX files in
./dtln-aec-onnx/.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort

BLOCK_LEN = 512   # 32 ms @ 16 kHz
BLOCK_SHIFT = 128  # 8 ms hop
SAMPLE_RATE = 16000


def load_wav_int16_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        if sr != SAMPLE_RATE or ch != 1 or sw != 2:
            raise SystemExit(
                f"{path}: expected 16 kHz mono int16, got sr={sr} ch={ch} sw={sw}"
            )
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def write_wav_int16_mono(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.clip(samples, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mic", type=Path, required=True, help="mic input WAV (16 kHz mono int16)")
    ap.add_argument("--ref", type=Path, required=True, help="reference input WAV (loopback)")
    ap.add_argument("--out", type=Path, required=True, help="output WAV path")
    ap.add_argument(
        "--model-size",
        type=int,
        default=128,
        choices=[128, 256, 512],
        help="DTLN-aec model size (LSTM units)",
    )
    ap.add_argument(
        "--models-dir",
        type=Path,
        default=Path("dtln-aec-onnx"),
        help="directory containing dtln_aec_<size>_{1,2}.onnx "
        "(default: ./dtln-aec-onnx, matching convert-dtln-aec.sh's OUT_DIR default)",
    )
    args = ap.parse_args()

    m1 = args.models_dir / f"dtln_aec_{args.model_size}_1.onnx"
    m2 = args.models_dir / f"dtln_aec_{args.model_size}_2.onnx"
    if not m1.is_file() or not m2.is_file():
        print(f"Models missing in {args.models_dir} (looking for {m1.name}, {m2.name})", file=sys.stderr)
        print("Run scripts/convert-dtln-aec.sh first.", file=sys.stderr)
        return 1

    print(f"Loading models from {args.models_dir} (size={args.model_size}) ...")
    sess1 = ort.InferenceSession(str(m1), providers=["CPUExecutionProvider"])
    sess2 = ort.InferenceSession(str(m2), providers=["CPUExecutionProvider"])

    # Discover the name of the LSTM-state input on each stage by
    # picking the one with the 4-d shape (the magnitude/feature inputs
    # are 3-d). This way the script works for any model size.
    def find_state_input(sess):
        for inp in sess.get_inputs():
            if len(inp.shape) == 4:
                return inp.name, tuple(inp.shape)
        raise RuntimeError("no 4-d input found (expected LSTM state)")

    state1_name, state1_shape = find_state_input(sess1)
    state2_name, state2_shape = find_state_input(sess2)

    # The two 3-d inputs to stage 1 are mic-magnitude and ref-magnitude.
    # Per breizhn's TFLite ordering verified at conversion time:
    # input[0] = mic mag (input_3), input[2] = ref mag (input_4).
    # The model uses the symmetric pair `input_3` (smaller name index)
    # for mic and `input_4` for ref consistently across all sizes.
    s1_inputs = sess1.get_inputs()
    s1_mic_name = [i.name for i in s1_inputs if i.name == "input_3"][0]
    s1_ref_name = [i.name for i in s1_inputs if i.name == "input_4"][0]

    # Stage 2: input_6 = estimated_block (post-mask iFFT), input_7 = ref time-domain.
    s2_inputs = sess2.get_inputs()
    s2_est_name = [i.name for i in s2_inputs if i.name == "input_6"][0]
    s2_ref_name = [i.name for i in s2_inputs if i.name == "input_7"][0]

    print(f"  stage 1: mic={s1_mic_name} ref={s1_ref_name} state={state1_name}{state1_shape}")
    print(f"  stage 2: est={s2_est_name} ref={s2_ref_name} state={state2_name}{state2_shape}")

    print("Loading audio ...")
    mic_i16 = load_wav_int16_mono(args.mic)
    ref_i16 = load_wav_int16_mono(args.ref)
    # The two streams should be the same length per the bridge's
    # time-aligned debug-record mode; pad ref with zeros if it's
    # short (e.g. quiet conditions where ref.wav is silent but
    # exists), or truncate if longer.
    n = min(len(mic_i16), len(ref_i16))
    mic_i16 = mic_i16[:n]
    ref_i16 = ref_i16[:n]

    # Normalize to float32 [-1, 1] per the breizhn pipeline.
    mic_f = mic_i16.astype(np.float32) / 32768.0
    ref_f = ref_i16.astype(np.float32) / 32768.0

    # Pre/post pad with (block_len - block_shift) zeros so the first
    # and last block_shift samples are also processed under a full
    # window of context.
    pad = np.zeros(BLOCK_LEN - BLOCK_SHIFT, dtype=np.float32)
    mic_f = np.concatenate([pad, mic_f, pad])
    ref_f = np.concatenate([pad, ref_f, pad])

    num_blocks = (len(mic_f) - (BLOCK_LEN - BLOCK_SHIFT)) // BLOCK_SHIFT

    in_buffer = np.zeros(BLOCK_LEN, dtype=np.float32)
    in_buffer_ref = np.zeros(BLOCK_LEN, dtype=np.float32)
    out_buffer = np.zeros(BLOCK_LEN, dtype=np.float32)
    states_1 = np.zeros(state1_shape, dtype=np.float32)
    states_2 = np.zeros(state2_shape, dtype=np.float32)
    out_samples = np.zeros(len(mic_f), dtype=np.float32)

    print(f"Processing {num_blocks} blocks ({num_blocks * BLOCK_SHIFT / SAMPLE_RATE:.1f}s) ...")
    for idx in range(num_blocks):
        # Slide window: shift left by block_shift, fill last block_shift samples.
        in_buffer[:-BLOCK_SHIFT] = in_buffer[BLOCK_SHIFT:]
        in_buffer[-BLOCK_SHIFT:] = mic_f[idx * BLOCK_SHIFT : idx * BLOCK_SHIFT + BLOCK_SHIFT]
        in_buffer_ref[:-BLOCK_SHIFT] = in_buffer_ref[BLOCK_SHIFT:]
        in_buffer_ref[-BLOCK_SHIFT:] = ref_f[idx * BLOCK_SHIFT : idx * BLOCK_SHIFT + BLOCK_SHIFT]

        # FFT (rectangular window, raw rfft on 512-sample buffer).
        in_fft = np.fft.rfft(in_buffer).astype(np.complex64)
        in_mag = np.abs(in_fft).reshape(1, 1, -1).astype(np.float32)
        ref_fft = np.fft.rfft(in_buffer_ref).astype(np.complex64)
        ref_mag = np.abs(ref_fft).reshape(1, 1, -1).astype(np.float32)

        # Stage 1: predict the spectral mask.
        s1_out = sess1.run(None, {
            s1_mic_name: in_mag,
            s1_ref_name: ref_mag,
            state1_name: states_1,
        })
        # Outputs returned in graph order: per the conversion verification
        # the first output is the mask (shape [1,1,257]), the second is
        # the updated LSTM state (shape [1,2,128,2]).
        mask = s1_out[0]
        states_1 = s1_out[1]

        # Apply mask in frequency domain (preserves original phase),
        # iFFT back to time domain.
        estimated_complex = in_fft * mask.flatten()
        estimated_block = np.fft.irfft(estimated_complex).astype(np.float32)
        estimated_block = estimated_block.reshape(1, 1, -1)
        ref_block = in_buffer_ref.reshape(1, 1, -1).astype(np.float32)

        # Stage 2: time-domain post-filter.
        s2_out = sess2.run(None, {
            s2_est_name: estimated_block,
            s2_ref_name: ref_block,
            state2_name: states_2,
        })
        out_block = s2_out[0].squeeze()
        states_2 = s2_out[1]

        # Overlap-add output: shift the buffer left, zero the tail,
        # add the new block, emit the leading block_shift samples.
        out_buffer[:-BLOCK_SHIFT] = out_buffer[BLOCK_SHIFT:]
        out_buffer[-BLOCK_SHIFT:] = 0.0
        out_buffer += out_block
        out_samples[idx * BLOCK_SHIFT : idx * BLOCK_SHIFT + BLOCK_SHIFT] = out_buffer[:BLOCK_SHIFT]

    # Strip the leading padding (we offset everything by block_len - block_shift).
    out_samples = out_samples[BLOCK_LEN - BLOCK_SHIFT : BLOCK_LEN - BLOCK_SHIFT + n]

    # Convert back to int16. Multiply by 32768 (matches breizhn's run_aec.py).
    out_i16 = (out_samples * 32768.0).astype(np.float32)

    # Stats
    peak = int(np.abs(out_i16).max())
    rms = float(np.sqrt(np.mean(out_i16 ** 2)))
    import math
    print(f"\nOutput: {args.out}")
    print(f"  duration: {len(out_i16) / SAMPLE_RATE:.2f}s")
    print(f"  peak: {peak} ({20 * math.log10(max(peak, 1) / 32768):+.1f} dBFS)")
    print(f"  RMS:  {rms:.0f} ({20 * math.log10(max(rms, 1) / 32768):+.1f} dBFS)")

    write_wav_int16_mono(args.out, out_i16)
    return 0


if __name__ == "__main__":
    sys.exit(main())
