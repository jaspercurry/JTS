# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The fan-in ALSA lane shared by correction and commissioning playback.

**This module is the single source of truth for the ALSA pcm alias name**
room correction's measurement sweeps/tones and active-speaker commissioning's
isolated-driver/summed captures both play into:

    WAV -> correction_substream -> jasper-fanin -> CamillaDSP -> outputd

``correction_substream`` is one private snd-aloop lane (``/etc/asound.conf``,
backed by ``hw:Loopback,0,4``) shared by two features. Before this module
existed the name was independently re-declared as ``DEFAULT_ALSA_DEVICE``,
``CORRECTION_SUBSTREAM``, ``COMMISSION_TONE_ALSA_DEVICE``,
``VOLUME_FLOOR_TONE_ALSA_DEVICE``, and two identically-named
``PLAYBACK_DEVICE`` constants across ``jasper.correction``,
``jasper.active_speaker``, ``jasper.web``, and ``jasper.cli`` — eight-plus
sites with no shared constant and no test pinning that the copies agreed
(issue #1789). A rename would have silently diverged.
``tests/test_correction_substream_ssot.py`` is the drift guard.

Why this module lives in ``jasper.audio_measurement`` and not
``jasper.correction``: ``jasper.correction`` and ``jasper.active_speaker``
import each other (the same fact
:mod:`jasper.audio_measurement.room_boundary` documents for the room-
correction band edge), so neither package is "below" the other and homing a
shared constant in either would be an arbitrary pick some consumer has to
reach sideways for. ``jasper.audio_measurement`` is imported by both while
importing neither, and is already the documented shared kernel for
primitives every tuning layer reuses (see this package's own
``__init__.py``).

It is also the *lightweight* choice, which is why the constant does not
simply stay in ``jasper.correction.playback`` (the previous "canonical"
site): that module, and ``jasper.audio_measurement.playback`` beneath it,
import ``numpy`` at module scope for WAV/DSP mechanics. Several consumers of
this constant are socket-activated wizard modules that must stay light
(``jasper.web.sound_setup``, ``jasper.web.sync_flow``,
``jasper.web.balance_flow`` — see AGENTS.md "Web wizard conventions") plus
``jasper.active_speaker.web_commissioning``, which none of them imported
numpy before this refactor and must not start now. This module has no
imports beyond ``__future__``, so reading the constant costs those wizards
nothing.
"""
from __future__ import annotations

# The snd-aloop pcm alias correction/commissioning WAV and tone playback is
# routed into before jasper-fanin -> CamillaDSP -> outputd. Declared exactly
# once — every other reference imports this name rather than re-declaring
# the string.
DEFAULT_ALSA_DEVICE = "correction_substream"
