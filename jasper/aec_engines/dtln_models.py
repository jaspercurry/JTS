# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Registry of DTLN-aec ONNX model bundles + download metadata.

DTLN-aec ships as a pair of ONNX files per model size (the network
has two cascaded LSTM stages, each its own graph). At runtime
[`DTLNEngine`](dtln.py) reads them from `JASPER_DTLN_MODEL_DIR`
(default `/var/lib/jasper/dtln/`).

This registry is the single source of truth for:
  - which sizes are available + recommended
  - where install.sh fetches them from
  - SHA-256 hashes to catch corrupted / partial downloads

Adding a new size:
  1. Run `bash scripts/convert-dtln-aec.sh <size>` to produce the
     two `.onnx` files (e.g. `dtln_aec_512_1.onnx`,
     `dtln_aec_512_2.onnx`).
  2. Compute hashes: `sha256sum dtln_aec_*.onnx`.
  3. Attach them to a release on this repo (typically a new tag
     like `dtln-models-v2`). Existing tag `dtln-models-v1` hosts
     the 256-unit pair.
  4. Add a `DTLNModelEntry(...)` below.
  5. Re-run `bash scripts/deploy-to-pi.sh` — install.sh's
     `download_dtln_models()` block fetches anything missing.

Why ONNX, not the upstream TFLite? Raspberry Pi OS Trixie ships
Python 3.13 and there is no `tflite-runtime` wheel for it. ONNX
runs on `onnxruntime` (which has working aarch64 wheels for 3.13).
The conversion + parity verification recipe is in
`scripts/convert-dtln-aec.sh`.

Upstream attribution: DTLN-aec is MIT-licensed work by Nils
Westhausen + Carl von Ossietzky Universität Oldenburg —
https://github.com/breizhn/DTLN-aec (paper:
https://arxiv.org/abs/2010.14337). The upstream MIT text is checked in
at `jasper/aec_engines/DTLN_LICENSE` and should travel with any
redistributed converted ONNX bundle.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Where install.sh stages downloaded DTLN models. Files here survive
# package reinstalls because they live under /var/lib, not /opt/jasper
# (which install.sh rewrites). Owner: root; mode 0644 so the bridge
# (also root) can mmap them at startup.
DTLN_MODELS_DIR = "/var/lib/jasper/dtln"

# Production DTLN size. 256-unit balances recall on whisper-music
# cells (the cases that motivated adding the DTLN leg) against CPU
# cost (~25% of one Pi 5 core when streaming at 50 fps). 128-unit is
# roughly half the CPU with slightly worse recall; the 512-unit
# upstream model is too heavy for the Pi 5. See
# `docs/HANDOFF-mic-quality-v2.md` "Triple-stream architecture plan"
# for the recall/CPU trade-off discussion.
DEFAULT_SIZE = 256


@dataclass(frozen=True)
class DTLNModelEntry:
    """One DTLN-aec model bundle (stage 1 + stage 2 ONNX pair).

    `size` is the LSTM unit count baked into the ONNX graph; pairs
    are not interchangeable across sizes. `*_sha256` lets install.sh
    detect a corrupted partial download and re-fetch — without it a
    half-downloaded ONNX would silently fail at engine init with a
    cryptic onnxruntime error.
    """

    size: int
    stage1_url: str
    stage1_sha256: str
    stage2_url: str
    stage2_sha256: str

    @property
    def stage1_filename(self) -> str:
        return f"dtln_aec_{self.size}_1.onnx"

    @property
    def stage2_filename(self) -> str:
        return f"dtln_aec_{self.size}_2.onnx"

    def files(self, base_dir: str | Path = DTLN_MODELS_DIR) -> list[tuple[Path, str, str]]:
        """Return [(local_path, url, sha256), ...] for both stages.

        Used by install.sh's download loop to fetch + hash-verify
        each file independently."""
        base = Path(base_dir)
        return [
            (base / self.stage1_filename, self.stage1_url, self.stage1_sha256),
            (base / self.stage2_filename, self.stage2_url, self.stage2_sha256),
        ]


# Ordered list of registered DTLN model sizes. Lowest size first so a
# new entry doesn't change the production default (which is keyed on
# `DEFAULT_SIZE`, not list position).
REGISTRY: tuple[DTLNModelEntry, ...] = (
    DTLNModelEntry(
        size=256,
        stage1_url=(
            "https://github.com/jaspercurry/JTS/releases/download/"
            "dtln-models-v1/dtln_aec_256_1.onnx"
        ),
        stage1_sha256="06cda79efdb7764cfb1dd87b2d3bae94d951dc3d41a4e73b09f324ffdf8f9a4d",
        stage2_url=(
            "https://github.com/jaspercurry/JTS/releases/download/"
            "dtln-models-v1/dtln_aec_256_2.onnx"
        ),
        stage2_sha256="06ce997599928ef181c3dbabfb63e27a00da9e47df73c80626fa82e9c43785a4",
    ),
)


def by_size(size: int) -> DTLNModelEntry | None:
    """Find a registry entry by LSTM unit count (e.g. 256)."""
    for entry in REGISTRY:
        if entry.size == size:
            return entry
    return None


def default() -> DTLNModelEntry:
    """The production DTLN model bundle. Raises if not registered."""
    entry = by_size(DEFAULT_SIZE)
    if entry is None:  # pragma: no cover — caught by registry-shape tests
        raise RuntimeError(
            f"DEFAULT_SIZE {DEFAULT_SIZE} not in REGISTRY — "
            "fix jasper/aec_engines/dtln_models.py"
        )
    return entry
