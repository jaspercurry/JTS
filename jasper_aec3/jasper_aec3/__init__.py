"""WebRTC AEC3 Python binding for jasper-aec-bridge.

Wraps the C++ AudioProcessing module from Debian Trixie's
`libwebrtc-audio-processing-dev` (v1.3-3 — the modern AEC3 API; the
1.x version number reflects the upstream package's API-stability
versioning, not the algorithm version).

The Aec3 class accepts mic and ref byte buffers of int16 mono PCM at
16 kHz and returns AEC'd mic bytes. Buffers must be a multiple of 10
ms (160 samples = 320 bytes) — the binding splits internally into the
WebRTC API's required 10 ms frames.

Usage:
    from jasper_aec3 import Aec3
    aec = Aec3()
    clean_mic = aec.process(mic_bytes, ref_bytes)
"""
from ._aec3 import Aec3

__all__ = ["Aec3"]
