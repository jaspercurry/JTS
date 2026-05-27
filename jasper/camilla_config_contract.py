"""Lightweight CamillaDSP config contract shared by DSP config emitters.

Keep this module import-cheap. Socket-activated web surfaces use these
defaults to build and inspect CamillaDSP YAML without pulling NumPy/SciPy
into the combined ``jasper-web`` process.
"""
from __future__ import annotations

from dataclasses import dataclass


# Defaults match deploy/camilladsp/v1.yml. Anything that diverges
# breaks the v1.yml <-> generated-config substitution at runtime.
DEFAULT_CAPTURE_DEVICE = "plug:jasper_capture"
DEFAULT_PLAYBACK_DEVICE = "jasper_out"
DEFAULT_CAPTURE_FORMAT = "S32_LE"
DEFAULT_PLAYBACK_FORMAT = "S16_LE"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHUNKSIZE = 1024
DEFAULT_TARGET_LEVEL = 2048
# CamillaDSP defaults the main fader's maximum to +50 dB when omitted.
# JTS treats 0 dB as the hard software ceiling; source/headroom logic
# should attenuate below this, never boost above full scale.
DEFAULT_VOLUME_LIMIT_DB = 0.0


@dataclass(frozen=True)
class PeqFilter:
    """Import-cheap representation of a CamillaDSP peaking EQ."""

    freq: float
    q: float
    gain: float
