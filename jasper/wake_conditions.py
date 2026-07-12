# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single source of truth for wake-detection acoustic conditions + corpus axes.

A *condition* names the acoustic situation a wake event happens in
(``quiet`` / ``ambient`` / ``music``). It is the shared vocabulary across
three consumers that would otherwise drift — and the only way collected data
maps onto the thresholds that consume it:

  * the corpus recorder (:mod:`jasper.web.wake_corpus_setup`), where the
    operator *labels* each capture session;
  * the runtime ``ConditionContext`` estimator in the voice daemon, which
    *infers* the condition (music from the playback-reference RMS the AEC
    bridge already computes; quiet vs ambient from a VAD-negative
    noise-floor proxy); and
  * the wake-event telemetry (:mod:`jasper.wake_events` ``condition_class``),
    which records the inferred condition per fire.

One definition means the corpus's "settings" axis, the fuser's per-condition
thresholds, and the telemetry labels are the *same set by construction*.

Stability contract — mirrors :mod:`jasper.wake_legs`' frozen-token rule, so
evolving the taxonomy later (e.g. as the corpus tool changes) can never
corrupt already-collected data:

  * Recorded data stores the condition as a plain **string**, never an index
    into these tuples. Corpus metadata and ``wake_events.condition_class`` use
    the semantic label. Corpus directories retain the historical ``nomusic``
    alias for ``quiet`` via :data:`CORPUS_DIR_BY_CONDITION`. Historical
    rows/files keep their label even if this set changes.
  * Consumers MUST tolerate a value outside the current set (older data or a
    forward-compat label). Use :func:`normalize_condition` when *consuming* a
    stored/inferred label; the fuser applies no condition-specific threshold
    change for an unknown condition rather than failing.
  * **ADD** a condition freely — old data keeps its label, new data gets the
    new one, and you tune its threshold once you've collected it. **RENAMING**
    a condition orphans historical data labelled with the old name; treat it
    like renaming a frozen ``wake_legs`` token — avoid it, or ship an alias.

``DISTANCES`` is a corpus/training-only axis (the runtime fuser cannot
estimate distance from a wake frame), so changing it only affects the offline
training pipeline that slices on it — never the fuser.
"""
from __future__ import annotations

# Acoustic conditions, ordered quietest -> loudest interference. "ambient" is
# the realistic-home floor (AC, fridge, TV murmur; no music we control).
CONDITIONS: tuple[str, ...] = ("quiet", "ambient", "music")

# On-disk corpus directory condition tokens. ``quiet`` predates the shared
# semantic taxonomy and was already stored as ``nomusic`` by the wake-events
# extractor and enrollment CLI, so that alias is a compatibility contract.
# Writers and directory readers share this mapping; metadata continues to use
# the semantic CONDITIONS values above.
CORPUS_DIR_BY_CONDITION: dict[str, str] = {
    "quiet": "nomusic",
    "ambient": "ambient",
    "music": "music",
}
CORPUS_DIR_CONDITIONS: tuple[str, ...] = tuple(
    CORPUS_DIR_BY_CONDITION[condition] for condition in CONDITIONS
)

# Corpus capture distance (operator-labelled). Corpus/training only — the
# runtime fuser does not consume it.
DISTANCES: tuple[str, ...] = ("near", "mid", "far")

# Safe fallback for an unclassifiable or unknown-to-this-build condition: the
# base condition applies no threshold relaxation, so a misread can only make
# wake *less* eager, never spuriously more.
DEFAULT_CONDITION: str = "quiet"


def normalize_condition(value: str | None) -> str:
    """Resolve a stored/inferred label to a known condition.

    Tolerant by design: a label from older data, or a future taxonomy this
    build doesn't recognise, resolves to :data:`DEFAULT_CONDITION` instead of
    raising — consuming code (the fuser especially) must never crash on an
    unknown condition. Use this when *consuming* a label; validate operator
    input against :data:`CONDITIONS` directly (the corpus wizard should reject
    typos, not silently coerce them).
    """
    return value if value in CONDITIONS else DEFAULT_CONDITION
