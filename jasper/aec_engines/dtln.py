"""DTLN-aec streaming engine for jasper-aec-bridge.

Wraps breizhn/DTLN-aec (converted to ONNX via tf2onnx, see
`scripts/convert-dtln-aec.sh`) into the same `process(mic, ref) -> bytes`
shape the bridge expects for the AEC3 engines.

Algorithm verbatim from breizhn's `run_aec.py`:
- Sliding 512-sample window with 128-sample hop (8 ms hop @ 16 kHz)
- rfft on the current mic + ref windows → magnitude vectors
- Stage 1: mic_mag, ref_mag, LSTM_state_1 → mask, LSTM_state_1'
- Apply mask to mic_fft, irfft to time domain → "estimated block"
- Stage 2: estimated_block, ref_block, LSTM_state_2 → clean_block, LSTM_state_2'
- Overlap-add into a 512-sample output buffer; emit the leading 128 samples
- LSTM state is carried across all calls — that's the "streaming" part

Input/output:
- Each `process()` call accepts mic + ref bytes (int16 mono, 16 kHz,
  equal length, multiple of 2 bytes). For best alignment with DTLN's
  128-sample hop, callers should pass multiples of 128 samples
  (256 bytes); but the engine buffers fractional remainders so any
  multiple-of-2-byte input works.
- Output is the same length as input (delayed by the warmup pad
  built into the first call — the offline runner handled this with
  a pre/post pad; in streaming mode the first few hundred ms of
  output is zero-padded warmup).

Cost on Pi 5 (extrapolated from Pi 3B+ published numbers):
- 256-unit model: ~1.5 ms per 128-sample block, ~12% of one A76 core
- RAM: ~95 MB resident (ONNX runtime + models)
"""
from __future__ import annotations

import logging
import os
import wave
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# DTLN-aec algorithm constants (matches breizhn's run_aec.py).
SAMPLE_RATE = 16000
BLOCK_LEN = 512          # 32 ms @ 16 kHz — STFT window length
BLOCK_SHIFT = 128        # 8 ms hop — output cadence


class DTLNEngine:
    """Streaming DTLN-aec engine. Maintains LSTM state across calls."""

    def __init__(
        self,
        model_dir: Path,
        model_size: int = 256,
    ) -> None:
        """
        Args:
            model_dir: directory containing dtln_aec_<size>_{1,2}.onnx
            model_size: 128 / 256 / 512 (number of LSTM units; bigger
                = more capacity, more CPU)
        """
        # Imported lazily so the bridge can decide DTLN-vs-not without
        # the import cost (onnxruntime + model warmup is ~500ms).
        import onnxruntime as ort

        m1 = model_dir / f"dtln_aec_{model_size}_1.onnx"
        m2 = model_dir / f"dtln_aec_{model_size}_2.onnx"
        if not m1.is_file() or not m2.is_file():
            raise FileNotFoundError(
                f"DTLN ONNX models missing in {model_dir}: looking for "
                f"{m1.name} + {m2.name}. install.sh normally fetches them "
                f"from the dtln-models-v1 release of jaspercurry/JTS — see "
                f"jasper/aec_engines/dtln_models.py for the registry. To "
                f"install by hand: `gh release download dtln-models-v1 "
                f"--repo jaspercurry/JTS --dir {model_dir}`."
            )

        # Single-threaded inference: more deterministic timing, less
        # contention with the rest of the daemon. ORT defaults to N=cores
        # which can starve the audio callback if it spikes.
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._sess1 = ort.InferenceSession(
            str(m1), sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._sess2 = ort.InferenceSession(
            str(m2), sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        # Discover input names by tensor shape (4-D = LSTM state).
        # The naming differs slightly between model sizes; this is
        # robust across all sizes.
        def find_state_input(sess):
            for inp in sess.get_inputs():
                if len(inp.shape) == 4:
                    return inp.name, tuple(inp.shape)
            raise RuntimeError("no 4-d LSTM state input found")

        self._s1_state_name, s1_state_shape = find_state_input(self._sess1)
        self._s2_state_name, s2_state_shape = find_state_input(self._sess2)
        # Stage 1: input_3 = mic mag, input_4 = ref mag
        s1_in_names = [i.name for i in self._sess1.get_inputs()]
        self._s1_mic_name = "input_3" if "input_3" in s1_in_names else s1_in_names[0]
        self._s1_ref_name = "input_4" if "input_4" in s1_in_names else s1_in_names[2]
        # Stage 2: input_6 = estimated_block, input_7 = ref time-domain
        s2_in_names = [i.name for i in self._sess2.get_inputs()]
        self._s2_est_name = "input_6" if "input_6" in s2_in_names else s2_in_names[0]
        self._s2_ref_name = "input_7" if "input_7" in s2_in_names else s2_in_names[2]

        # Streaming state — carried across process() calls.
        self._mic_buffer = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._ref_buffer = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._out_buffer = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._state1 = np.zeros(s1_state_shape, dtype=np.float32)
        self._state2 = np.zeros(s2_state_shape, dtype=np.float32)

        # Input-byte spill (when caller passes a size that isn't a
        # clean multiple of BLOCK_SHIFT samples).
        self._mic_spill = b""
        self._ref_spill = b""

        logger.info(
            "DTLN-aec ready: size=%d, models=%s + %s, "
            "state shapes %s / %s",
            model_size, m1.name, m2.name, s1_state_shape, s2_state_shape,
        )

    def process(self, mic_bytes: bytes, ref_bytes: bytes) -> bytes:
        """Process one chunk; return AEC'd mic bytes of the same length.

        Internally splits the input into 128-sample (256-byte) blocks
        and runs DTLN on each. Any trailing bytes that don't form a
        full block are buffered for the next call.
        """
        if len(mic_bytes) != len(ref_bytes):
            raise ValueError("mic and ref must be the same length")

        # Prepend spill from previous call
        mic_bytes = self._mic_spill + mic_bytes
        ref_bytes = self._ref_spill + ref_bytes

        n_samples = len(mic_bytes) // 2
        n_blocks = n_samples // BLOCK_SHIFT
        consumed_samples = n_blocks * BLOCK_SHIFT
        consumed_bytes = consumed_samples * 2

        # Save trailing fractional block for next call
        self._mic_spill = mic_bytes[consumed_bytes:]
        self._ref_spill = ref_bytes[consumed_bytes:]

        if n_blocks == 0:
            # Not enough samples yet; emit zeros at the input size
            # (callers expect equal-length output)
            return bytes(len(mic_bytes) - len(self._mic_spill))

        mic = np.frombuffer(mic_bytes[:consumed_bytes], dtype=np.int16).astype(np.float32) / 32768.0
        ref = np.frombuffer(ref_bytes[:consumed_bytes], dtype=np.int16).astype(np.float32) / 32768.0

        out_samples = np.zeros(consumed_samples, dtype=np.float32)

        for i in range(n_blocks):
            # Slide window left, fill last BLOCK_SHIFT
            self._mic_buffer[:-BLOCK_SHIFT] = self._mic_buffer[BLOCK_SHIFT:]
            self._mic_buffer[-BLOCK_SHIFT:] = mic[i * BLOCK_SHIFT : (i + 1) * BLOCK_SHIFT]
            self._ref_buffer[:-BLOCK_SHIFT] = self._ref_buffer[BLOCK_SHIFT:]
            self._ref_buffer[-BLOCK_SHIFT:] = ref[i * BLOCK_SHIFT : (i + 1) * BLOCK_SHIFT]

            # STFT magnitudes (rectangular window, raw rfft)
            mic_fft = np.fft.rfft(self._mic_buffer).astype(np.complex64)
            mic_mag = np.abs(mic_fft).reshape(1, 1, -1).astype(np.float32)
            ref_fft = np.fft.rfft(self._ref_buffer).astype(np.complex64)
            ref_mag = np.abs(ref_fft).reshape(1, 1, -1).astype(np.float32)

            # Stage 1: spectral mask
            s1_out = self._sess1.run(None, {
                self._s1_mic_name: mic_mag,
                self._s1_ref_name: ref_mag,
                self._s1_state_name: self._state1,
            })
            mask = s1_out[0]
            self._state1 = s1_out[1]

            # Apply mask to complex spectrum, iFFT
            est_complex = mic_fft * mask.flatten()
            est_block = np.fft.irfft(est_complex).astype(np.float32).reshape(1, 1, -1)
            ref_block = self._ref_buffer.reshape(1, 1, -1).astype(np.float32)

            # Stage 2: time-domain post-filter
            s2_out = self._sess2.run(None, {
                self._s2_est_name: est_block,
                self._s2_ref_name: ref_block,
                self._s2_state_name: self._state2,
            })
            out_block = s2_out[0].squeeze()
            self._state2 = s2_out[1]

            # Overlap-add: shift left, zero tail, accumulate
            self._out_buffer[:-BLOCK_SHIFT] = self._out_buffer[BLOCK_SHIFT:]
            self._out_buffer[-BLOCK_SHIFT:] = 0.0
            self._out_buffer += out_block
            out_samples[i * BLOCK_SHIFT : (i + 1) * BLOCK_SHIFT] = self._out_buffer[:BLOCK_SHIFT]

        # Back to int16
        out_i16 = np.clip(out_samples * 32768.0, -32768, 32767).astype(np.int16)
        return out_i16.tobytes()

    def close(self) -> None:
        # onnxruntime sessions release on GC.
        pass


def default_model_dir() -> Path:
    """Where install.sh places the converted ONNX models on the Pi."""
    return Path(os.environ.get("JASPER_DTLN_MODEL_DIR", "/var/lib/jasper/dtln"))
