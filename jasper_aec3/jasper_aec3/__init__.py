# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WebRTC AEC3 Python bindings for jasper-aec-bridge.

Two engines:

- ``Aec3`` (the legacy binding) wraps Debian Trixie's apt-installed
  ``libwebrtc-audio-processing-1`` (v1.3-3). Public API is the
  top-level ``AudioProcessing::Config`` — deep ``EchoCanceller3Config``
  knobs are not reachable. Used as the fallback engine.

- ``Aec3V2`` (the BEST_A binding) wraps a statically-linked vendored
  build of ``webrtc-audio-processing`` v2.1 (Pulseaudio's upstream
  fork). Exposes the deep ``EchoCanceller3Config`` knobs via a custom
  ``EchoControlFactory`` subclass. Default kwargs reflect the BEST_A
  canonical config from the 2026-05-22 tuning campaign — see
  ``docs/HANDOFF-mic-quality-v2.md`` "Triple-stream architecture plan"
  for context, and ``experiments/aec3-v2-deep-tune-spike/README.md``
  for per-knob rationale.

Both engines take 16 kHz mono int16 mic + ref byte buffers (multiple
of 10 ms = 160 samples = 320 bytes) and return AEC'd mic bytes the
same size.

``Aec3V2`` is installed by the opt-in ``jasper-enhanced-aec-install``
background job.  The package deliberately does not import that native
module while loading ``Aec3``: runtime authorization first verifies the
installed marker, source fingerprint, and extension digest.  This lazy
boundary ensures an orphan or stale ``_aec3_v2`` file can never be loaded
as a side effect of constructing the mandatory v1 fallback. ``HAS_V2``
only reports whether a v2 module is discoverable; it is not authorization
to use that module.

Usage:
    from jasper_aec3 import Aec3V2  # preferred (BEST_A)
    aec = Aec3V2()
    clean_mic = aec.process(mic_bytes, ref_bytes)

    # Fallback path:
    from jasper_aec3 import Aec3
    aec = Aec3()
    clean_mic = aec.process(mic_bytes, ref_bytes)
"""
from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

from ._aec3 import Aec3

if TYPE_CHECKING:
    from ._aec3_v2 import Aec3V2

# ``find_spec`` inspects the package path without executing the extension.
# The control plane and bridge must still call
# ``jasper.enhanced_aec.runtime_v2_verified`` before accessing ``Aec3V2``.
HAS_V2 = importlib.util.find_spec(f"{__name__}._aec3_v2") is not None


def __getattr__(name: str) -> Any:
    if name != "Aec3V2":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if not HAS_V2:
        raise ImportError(
            "optional jasper_aec3._aec3_v2 extension is not installed"
        )
    module = importlib.import_module(f"{__name__}._aec3_v2")
    value = module.Aec3V2
    # Cache only after the explicit lazy access succeeds.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), "Aec3V2"})

__all__ = ["Aec3", "Aec3V2", "HAS_V2"]
