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

``Aec3V2`` is only importable when ``deploy/install.sh``'s
``build_webrtc_v2_for_aec3()`` has successfully produced the vendored
static archive — i.e. on the Pi after a deploy. On dev laptops (no
v2 build) only ``Aec3`` will import; ``HAS_V2`` reflects this.

Usage:
    from jasper_aec3 import Aec3V2  # preferred (BEST_A)
    aec = Aec3V2()
    clean_mic = aec.process(mic_bytes, ref_bytes)

    # Fallback path:
    from jasper_aec3 import Aec3
    aec = Aec3()
    clean_mic = aec.process(mic_bytes, ref_bytes)
"""
from ._aec3 import Aec3

try:
    from ._aec3_v2 import Aec3V2

    HAS_V2 = True
except ImportError:
    Aec3V2 = None  # type: ignore[assignment, misc]
    HAS_V2 = False

__all__ = ["Aec3", "Aec3V2", "HAS_V2"]
