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
``jasper.active_speaker``, ``jasper.web``, and ``jasper.cli`` — five distinct
names spread across eleven files with no shared constant and no test pinning
that the copies agreed (issue #1789). A rename would have silently diverged.
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
``jasper.active_speaker.web_commissioning`` — none of which imported numpy
before this refactor, and none should start now. This module has no
imports beyond ``__future__``, so reading the constant costs those wizards
nothing. That promise is not just stated: it is pinned by
``tests/test_correction_substream_ssot.py``'s poisoned-subprocess guard,
which imports each of those four modules with ``numpy``/``scipy`` poisoned
in ``sys.modules`` and asserts the import still succeeds — the same
technique catches a stray convenience re-export added to THIS module's own
package ``__init__.py`` (``jasper/audio_measurement/__init__.py``), not just
a direct import in a consumer.

Scope of the drift guard
-------------------------
``tests/test_correction_substream_ssot.py`` only scans ``jasper/``. Two
things legitimately spell the literal outside that scope, on purpose:

* ``deploy/alsa/asoundrc.jasper`` declares the actual ``pcm.correction_substream``
  block the system's ALSA config reads at runtime. It is not Python, so it
  cannot import this constant — but it is not disconnected from this module
  either: :func:`jasper.cli.doctor.audio_runtime.check_fanin_asound_wiring` reads
  :data:`CORRECTION_SUBSTREAM` as the expected-alias dict key it checks
  ``/etc/asound.conf`` against, and ``tests/test_doctor_audio_runtime.py``'s
  hand-maintained
  ``_FANIN_ASOUND`` fixture mirrors ``asoundrc.jasper``'s
  ``pcm.correction_substream`` block byte-for-byte. So the Python constant and
  the checked-in ALSA config stay cross-validated through that doctor check,
  even though the AST guard here never reads either non-Python file.
* Five stdlib-only AEC lab-probe scripts under ``scripts/`` — ``aec-probe-
  timing.py``, ``aec-probe-latency.sh``, ``aec-probe-xvf-ref-level.sh``,
  ``aec-probe-pinknoise.sh``, ``chip-aec-baseline-check.sh`` — spell the
  literal by design. They are meant to run standalone (bash, or Python using
  only the stdlib), often copied straight onto the Pi for isolated hardware
  debugging outside the project venv; importing ``jasper`` would defeat that.

What is NOT owned here
-----------------------
* **A default on the neutral playback surface.**
  :mod:`jasper.audio_measurement.playback`'s ``play_wav`` and
  ``ensure_sine_wav`` deliberately take no ``alsa_device`` / ``cache_dir``
  default — that neutral leaf is policy-free by design (see its own module
  docstring: "Feature owners choose the ALSA lane"). Giving it a default,
  even by importing :data:`CORRECTION_SUBSTREAM` into it, would re-inject a
  feature-owned opinion into shared mechanics every tuning layer reuses. This
  module is where the constant lives, never a reason to reach INTO the
  neutral leaf and give it one. Pinned by
  ``tests/test_audio_measurement_playback.py::
  test_neutral_surface_requires_owner_policy``, which asserts
  ``not hasattr(playback, "DEFAULT_ALSA_DEVICE")``.
* **The derived commission-tone backend tags.** ``COMMISSION_TONE_BACKEND``,
  ``SUMMED_COMMISSION_SPEECH_BACKEND``, ``DRIVER_CAPTURE_SWEEP_BACKEND``, and
  ``SUMMED_CAPTURE_SWEEP_BACKEND`` in
  :mod:`jasper.active_speaker.web_commissioning` hold compound strings like
  ``"correction_substream_continuous_tone"``. They share this lane's name as
  a prefix but are a different vocabulary (event/backend tags, not the ALSA
  device), so they are deliberately NOT routed here. (One of the four,
  ``SUMMED_COMMISSION_SPEECH_BACKEND``, has its own independent-declaration
  drift bug — see issue #1957 — which is a separate fix, not a reason to
  fold that family into this module.)
"""
from __future__ import annotations

# The snd-aloop pcm alias correction/commissioning WAV and tone playback is
# routed into before jasper-fanin -> CamillaDSP -> outputd. Declared exactly
# once — every other reference imports this name rather than re-declaring
# the string. jasper.correction.playback re-exports it as the historical
# DEFAULT_ALSA_DEVICE name for one external compatibility import
# (jasper.active_speaker.commissioning_host); every other consumer imports
# CORRECTION_SUBSTREAM directly from here.
CORRECTION_SUBSTREAM = "correction_substream"
