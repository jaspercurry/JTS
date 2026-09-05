# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The offline EMIT LOOP: grade what the DSP emits against what the fit claims.

**Stage: VERIFY (offline).** This subpackage closes R10b's "emit loop … (the
shelf-Q class)" deliverable.

**Why it exists.** On 2026-07-27 a linearization shipped whose emitted shelves
realized at Q 0.476 while every gate in the fit engine evaluated them at Q
0.707. The fit's realization gate, its residual, and its VERIFY prediction all
used the same wrong evaluator, so a shelf that missed its design by up to
1.70 dB scored as exact — *a model cannot audit itself*
(:mod:`jasper.active_speaker.delta_probe`;
:data:`jasper.camilla_config_contract.SHELF_Q`). A fix closed that one bug and
:mod:`~jasper.active_speaker.delta_probe` catches the class *in the room*, on a
household's post-apply measurement. This subpackage catches the same class
**offline, before anything is applied and without a microphone**, by rendering
the real emitted config through the real pinned CamillaDSP binary and grading
what came out against what the fit said it would be.

The instrument is a **two-render A/B**. The same preset is emitted twice — once
carrying the linearization under test, once carrying none — and both configs
are rendered through the same binary on the same stimulus. The difference of
the two rendered magnitudes is the linearization stage's *realized* transfer:
the crossover, the delay, the per-driver gain, the split mixer, the fader, and
the stimulus itself are byte-identically present in both and cancel out of it
exactly. Nothing about them has to be modelled, and no second model of the
crossover is introduced to be wrong in a new way. What is left is compared
against the fit's own claim,
:func:`jasper.active_speaker.linearization_fit.complex_correction_response`.

Module map
----------
* :mod:`~jasper.active_speaker.bench.derivation` — emitter-generated config
  text → an offline-renderable config (``WavFile`` capture, ``File`` playback,
  no async resampler, allowlisted stages). Pure; no subprocess, no analysis.
* :mod:`~jasper.active_speaker.bench.compare` — pure numpy: a rendered stream
  → a magnitude curve; a curve pair → the frame disclosure, the error
  statistics, and a verdict in
  :mod:`~jasper.active_speaker.delta_probe`'s vocabulary.
* :mod:`~jasper.active_speaker.bench.loop` — the orchestration: emit ×2, derive
  ×2, render through the real binary, extract per branch, compare, report.

What this subpackage does NOT own
---------------------------------
* **Binary identity, the render invocation, and channel extraction** — those
  are :mod:`jasper.bass_extension.bench.render` (R5/R8/R9), reused verbatim.
  There is no second binary resolver and no second argv contract.
* **The verdict vocabulary and its thresholds** — those are
  :mod:`jasper.active_speaker.delta_probe`, reused. This subpackage does not
  define a parallel set of verdicts, and it does not retune the classifier's
  tolerances for the offline case (see
  :mod:`~jasper.active_speaker.bench.compare`'s note on what that means for
  sensitivity, and why ``max_error_db`` is the number to read).
* **Any live-graph mutation.** Nothing here loads, applies, reloads, or writes
  a CamillaDSP config to a running daemon. Every render is a one-shot batch
  pass over files, and the only side effects are files under the caller's own
  work directory.
"""

from __future__ import annotations
