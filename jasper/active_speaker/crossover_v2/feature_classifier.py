# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What KIND of feature is that — measured, from a round's own banked captures.

The INSTRUMENT behind :mod:`.feature_classification`'s register. That module
owns the verdict names, the row schema and what ``depth_db`` means; this one
runs the tests that fill them in, over captures a round already banked. It
imports every verdict string and never spells one, so the producer and the
reader cannot drift.

**Three tests and a gate.**

* **Excess group delay** — is the feature minimum-phase (a driver defect, so a
  filter is at least the right kind of tool) or non-minimum-phase (a
  cancellation, where a filter lowers the direct sound and its delayed copy
  together)? Read as a fraction of what a genuine same-frequency cancellation
  produces through this exact pipeline, which is what the C4 control measures.
* **Gate invariance** — re-window at 3 / 5 / 7 ms against a MATCHED-Q
  minimum-phase null model, so the shrinkage a shorter gate does to *any*
  narrow feature is subtracted rather than misread as a room arrival.
* **Timing scatter** — the sub-sample arrival residual between captures at the
  same angle, which is what survives into any phase comparison. It needs a
  repeated angle to run, says so when it has none, and never reads its own
  absence as evidence of tight timing.

**Nothing is reported until the controls pass.** Known answers are pushed
through the identical pipeline on the round's own measured IR — a minimum-phase
peaking filter that must read flat, an all-pass that must recover its own group
delay, a quiet delayed copy that must ALSO read flat (``|g| < 1`` puts every
zero inside the unit circle, so it is minimum phase), and a loud one that must
not. A run whose controls fail raises :data:`CONTROLS_FAILED` and writes no
artifact: a verdict from an instrument that just failed its own known-answer
check is a number, not evidence.

**Only the summed post-apply capture shapes are admissible.** ``verify`` and
``cloud_verify`` play the mono summed sweep through the live production graph
(``programs.SUMMED_SWEEP_PHASES``); ``lateral`` replays the per-driver MEASURE
program one driver at a time, which is why :mod:`.journey` keeps it out of that
set. A per-driver curve cannot answer a question about the summed system's
features, so a lateral capture is a HARD refusal by name
(:data:`LATERAL_CAPTURE_SHAPE`) rather than a silent skip — a caller who
pointed this at a per-driver round should be told what is wrong with the
round, not handed an empty result. The pre-apply half of that set
(``entry_baseline`` / ``cloud_measure``) is not admitted either, for a
different reason: it measures a DIFFERENT graph, and pooling the two would
classify a mixture.

**Why it lives here and not in** :mod:`jasper.audio_measurement`, where the DSP
seams it uses live.  ``tests/test_correction_boundary_ssot.py``'s
``test_audio_measurement_imports_neither_consumer_package`` pins that no file
under ``jasper/audio_measurement/`` may import ``jasper.active_speaker`` or
``jasper.correction`` — that invariant is what earns that package the home of
the correction-boundary SSOT.  This module imports the verdict register and the
phase names from ``jasper.active_speaker``, so relocating it there fails that
guard with two offenders.  It reaches the ``audio_measurement`` seams from this
side instead, which is the direction ``spatial.py`` and ``capture_dispatch.py``
already run.  **If you are about to move this module, read that test first.**

**What the lab harness did that this does not.** The 2026-08-19 campaign tools
(``captures/*/tools/classify_*.py``) also fitted a single-delay null ladder
through the dips and ran a 4000-trial randomisation against it, and read each
dip's depth through the shipped two-path reflection law. Both were reported and
neither entered a verdict — the run log's own conclusion was that four dips
with free rung integers pin nothing. They are left in the capture directories
as the archaeology they are.
"""

from __future__ import annotations

import json
import math
import wave
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.deconv import (
    magnitude_response,
    regularized_deconvolution_full,
)
from jasper.audio_measurement.gating import (
    SEARCH_T_MAX_MS,
    analytic_envelope,
    f_trusted_floor_hz,
)
from jasper.audio_measurement.quality_model import TrustLevel

from .feature_classification import (
    DEFECT_BOOSTABLE,
    DEFECT_CUTTABLE,
    EGD_AMBIGUOUS,
    EGD_MIN_PHASE,
    EGD_NON_MIN_PHASE,
    GATE_MOVED,
    GATE_STABLE,
    INTERFERENCE_BARRED,
    ROOM,
    UNRESOLVED,
)
from .evidence_packet import RING_SIDECAR_GLOB
from .journey import PHASE_CLOUD_VERIFY, PHASE_LATERAL, PHASE_VERIFY

__all__ = [
    "ADMISSIBLE_PHASES",
    "CAPTURE_ADMISSIBILITY_REASONS",
    "CAPTURES_UNREADABLE",
    "classifiable_band_hz",
    "CLASSIFICATION_REFUSAL_REASONS",
    "CLASSIFICATION_SCHEMA_VERSION",
    "CONTROLS_FAILED",
    "LATERAL_CAPTURE_SHAPE",
    "NO_ADMISSIBLE_CAPTURES",
    "NO_FEATURES_DETECTED",
    "PROGRAM_MISSING",
    "ROUND_SHAPE_INADMISSIBLE",
    "FeatureClassificationRefused",
    "RoundCapture",
    "RoundPoseCurve",
    "classify_round",
    "load_round_captures",
    "load_round_pose_curves",
]


# --------------------------------------------------------------------------- #
# refusals — a name for every way this instrument declines to report
# --------------------------------------------------------------------------- #

#: The known-answer controls did not pass on this round's own capture. Every
#: verdict is withheld, not merely annotated: the register's readers act on
#: ``classification`` and do not consult ``confidence``, so a ``defect-*``
#: string emitted beside a failed control would be acted on.
CONTROLS_FAILED = "classification_controls_failed"

#: A ``lateral`` capture was bound to this round. See the module docstring.
LATERAL_CAPTURE_SHAPE = "classification_lateral_capture_shape"

#: This round's captures were found, and every one of them names a phase this
#: instrument cannot read — a MEASURE-only round is the common case. The
#: remedy is a different ROUND: point at a verify-shaped one.
ROUND_SHAPE_INADMISSIBLE = "classification_round_shape_inadmissible"

#: No capture of this round reached the instrument at all — an empty ring, one
#: holding only other sessions' captures, or sidecars that will not parse (an
#: unreadable one carries no session id, so it is not attributable to this
#: round either). The remedy is the RING or the bundle it was scoped to; no
#: round shape would satisfy this one. Narrowed from the slug that used to
#: cover :data:`ROUND_SHAPE_INADMISSIBLE` too (#3480): the two situations
#: refuse from different code paths and have different remedies, and one name
#: for both sent a driver at the wrong fix.
NO_ADMISSIBLE_CAPTURES = "classification_no_admissible_captures"

#: This round banked a capture of an admissible shape and the ring cannot hand
#: it over: the WAV is not beside its sidecar, or the dump name lost the stamp
#: the take's timing is bound to. The round shape is RIGHT — the remedy is the
#: ring or the bank step that filled it, and pointing this driver at a
#: verify-shaped round (what :data:`ROUND_SHAPE_INADMISSIBLE` asks for) is the
#: #3480 misdirection rebuilt one vocabulary later.
CAPTURES_UNREADABLE = "classification_captures_unreadable"

#: A capture names a phase whose program WAV is not in the round directory, so
#: it cannot be deconvolved against the signal that was actually played.
PROGRAM_MISSING = "classification_program_missing"

#: Nothing in the pooled response stood above the round's own capture-to-
#: capture scatter. Refused rather than reported as an empty verdict set: an
#: artifact with no rows and a broken detector look identical to a reader.
NO_FEATURES_DETECTED = "classification_no_features_detected"

CLASSIFICATION_REFUSAL_REASONS = frozenset({
    CONTROLS_FAILED,
    LATERAL_CAPTURE_SHAPE,
    ROUND_SHAPE_INADMISSIBLE,
    NO_ADMISSIBLE_CAPTURES,
    CAPTURES_UNREADABLE,
    PROGRAM_MISSING,
    NO_FEATURES_DETECTED,
})

# --- per-capture admissibility — why each capture in the ring was dropped --- #

#: The capture is classifiable: an admissible shape, this round's session, its
#: program WAV present, and a readable WAV under a stamped name.
CAPTURE_ADMISSIBLE = "admissible"

#: The sidecar JSON did not read as an object carrying a ``phase`` string.
CAPTURE_UNREADABLE_SIDECAR = "unreadable_sidecar"

#: The capture is stamped with a different bundle ``session_id``.
CAPTURE_OTHER_SESSION = "other_session"

#: The capture's phase is not in :data:`ADMISSIBLE_PHASES`.
CAPTURE_PHASE_NOT_ADMISSIBLE = "phase_not_admissible"

#: A ``lateral`` capture — the per-driver shape that refuses the whole round.
CAPTURE_LATERAL_SHAPE = "lateral_capture_shape"

#: The phase's ``<phase>_program.wav`` is not in the round directory.
CAPTURE_PROGRAM_MISSING = "program_missing"

#: The sidecar's WAV is not beside it in the ring.
CAPTURE_WAV_MISSING = "wav_missing"

#: The dump filename does not open with the microsecond stamp every capture's
#: timing residual is bound to.
CAPTURE_UNSTAMPED_NAME = "unstamped_name"

#: Which refusal each admissibility reason speaks for when a round ends with no
#: classifiable capture — the partition of the closed capture vocabulary onto
#: the refusal vocabulary, as data. A row does not get to be a phase-shape
#: finding and a ring finding at once, and a reason cannot be added to the
#: vocabulary below without being placed here, because the vocabulary IS this
#: table's keys.
_REFUSAL_FOR_CAPTURE_REASON: dict[str, str | None] = {
    # Classifiable, so it cannot coexist with the refusal this table serves.
    CAPTURE_ADMISSIBLE: None,
    # Not attributable to this round at all. A sidecar that will not parse
    # carries no session id, exactly like one carrying somebody else's.
    CAPTURE_UNREADABLE_SIDECAR: NO_ADMISSIBLE_CAPTURES,
    CAPTURE_OTHER_SESSION: NO_ADMISSIBLE_CAPTURES,
    # The round answered a different question; a verify-shaped round answers
    # this one. (A lateral row is claimed earlier by LATERAL_CAPTURE_SHAPE.)
    CAPTURE_PHASE_NOT_ADMISSIBLE: ROUND_SHAPE_INADMISSIBLE,
    CAPTURE_LATERAL_SHAPE: ROUND_SHAPE_INADMISSIBLE,
    # The round is the right shape and the take is what cannot be read. (A
    # program-missing row is claimed earlier by PROGRAM_MISSING.)
    CAPTURE_PROGRAM_MISSING: CAPTURES_UNREADABLE,
    CAPTURE_WAV_MISSING: CAPTURES_UNREADABLE,
    CAPTURE_UNSTAMPED_NAME: CAPTURES_UNREADABLE,
}

#: Read in this order, so the most specific thing the census can say is what
#: the refusal says: a round whose admissible take the ring lost IS
#: verify-shaped, and naming its shape as the remedy is the #3480 misdirection.
#: A census speaking for neither says nothing of this round arrived, which is
#: :data:`NO_ADMISSIBLE_CAPTURES` — the empty census's answer too.
_REFUSAL_PRECEDENCE = (CAPTURES_UNREADABLE, ROUND_SHAPE_INADMISSIBLE)

#: What to fix, carried in the refusal a driver actually reads: the slug names
#: the state, this names the move (#3480 — the driver got the state alone).
_NO_CAPTURE_REMEDY = {
    NO_ADMISSIBLE_CAPTURES: (
        "no capture in this ring is attributable to this round; check the ring "
        "it was pointed at and the session id it was scoped by"
    ),
    ROUND_SHAPE_INADMISSIBLE: (
        "this round banked no capture shape carrying a summed-system response; "
        "point at a verify-shaped round"
    ),
    CAPTURES_UNREADABLE: (
        "the round shape is right and its takes are what cannot be read; the "
        "fix is the ring or the bank step that filled it, not another round"
    ),
}

#: The closed vocabulary a per-capture admissibility row's ``reason`` speaks.
CAPTURE_ADMISSIBILITY_REASONS = frozenset(_REFUSAL_FOR_CAPTURE_REASON)


class FeatureClassificationRefused(RuntimeError):
    """This round cannot be classified, and ``reason`` says why by name."""

    def __init__(self, reason: str, detail: Mapping[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail: dict[str, Any] = dict(detail or {})


# --------------------------------------------------------------------------- #
# what this instrument will read
# --------------------------------------------------------------------------- #

#: The capture shapes whose response IS the summed system, post-apply. The
#: pre-apply members of ``programs.SUMMED_SWEEP_PHASES`` are deliberately not
#: here — see the module docstring.
ADMISSIBLE_PHASES = frozenset({PHASE_VERIFY, PHASE_CLOUD_VERIFY})

#: The artifact's own version. Deliberately not a new number: the row shape is
#: the one the register already reads and the 2026-08-19 records already carry.
CLASSIFICATION_SCHEMA_VERSION = 1

GENERATED_BY = "jasper.active_speaker.crossover_v2.feature_classifier"


# --------------------------------------------------------------------------- #
# instrument constants — each anchored on something, none a taste
# --------------------------------------------------------------------------- #

#: The primary analysis window. ``gating.SEARCH_T_MAX_MS`` is the product's own
#: reflection search ceiling AND the window it falls back to when no reflection
#: is found, so a fixed window of that length is the longest one the product
#: ever calls reflection-free. Overridable per run.
DEFAULT_GATE_MS = SEARCH_T_MAX_MS

#: The gate-invariance ladder, shortest first. Every retention is read against
#: the commanded primary window (``gate_ms``) — the last entry here by
#: default. A commanded rung longer than the primary stays a rung: it
#: re-admits reflections as a readable fact and never displaces the primary.
GATE_LADDER_MS: tuple[float, ...] = (3.0, 5.0, DEFAULT_GATE_MS)

#: Pre-peak lead for the PHASE window only. A zero-lead window starts at
#: ``argmax(|ir|)`` and so splits the direct arrival's own main lobe — harmless
#: for magnitude, not harmless for phase, and at a crossover the other driver's
#: arrival can lead the peak. The zero-lead reading is measured alongside and
#: reported as ``lead_sensitivity_us`` so the choice is evidenced.
PHASE_GATE_LEAD_MS = 1.0

#: 6.11b's receipt: what :func:`excess_group_delay` actually windows with.
#: Research 03's pitfall is real — a SHORT gate biases the Hilbert min-phase
#: reconstruction — but the window this instrument reads EGD through is
#: ``DEFAULT_GATE_MS`` (``== gating.SEARCH_T_MAX_MS``), already documented
#: above as the longest window the product ever calls reflection-free: there
#: is no longer clean window to move to, so this names what is used rather
#: than changing it. It is also the exact window the C1/C3 controls are
#: calibrated on, on THIS round's own IR — a known minimum-phase change must
#: still read flat through it (``CONTROL_MAX_FALSE_POSITIVE_US`` /
#: ``CONTROL_MAX_ECHO_FALSE_POSITIVE_US``), which is the reflection-freeness
#: claim being tested rather than assumed. Investigated, not derived: judged
#: deliberate and defensible, so nothing about the window changed.
EGD_WINDOW_KIND = "fixed_reflection_free_gate"

#: Magnitude smoothing. 1/12 octave is fine enough to keep a feature's own
#: shape and coarse enough that grid noise does not become one.
MAGNITUDE_SMOOTH_FRACTION = 12

#: The broad tilt removed before a feature's size is read. One octave passes
#: the tilt and leaves a ~1/6-octave feature essentially intact; a half-octave
#: baseline would eat it.
DETREND_FRACTION = 1

#: Complex smoothing for the phase chain, as a fraction of an octave. Finer
#: than the magnitude chain because excess phase is the quantity under test.
#: Smoothing magnitude and phase separately would break the pairing the whole
#: test depends on, which is why this is applied to the complex spectrum.
COMPLEX_SMOOTH_OCT = 1.0 / 24.0

#: Half-width of a feature's own band, and of the span the group-delay slope is
#: fitted over.
FEATURE_HALF_OCT = 1.0 / 12.0
GD_SPAN_OCT = 1.0 / 24.0

#: The neighbourhood a feature's excess group delay is read against, and the
#: span its centre is searched over when the gate ladder asks whether it moved.
NEIGHBOURHOOD_OCT = 1.0 / 3.0
CENTRE_SEARCH_OCT = 1.0 / 6.0

#: Band the smoothed curve is normalised on, and the trusted ceiling. The
#: trusted FLOOR is derived per run from the gate length
#: (``gating.f_trusted_floor_hz``), never spelled.
NORMALISE_BAND_HZ = (400.0, 8000.0)
TRUSTED_CEILING_HZ = 16000.0

#: Analysis grid: logarithmic, and dense enough that a 1/12-octave feature has
#: hundreds of samples across it at every frequency.
GRID_LO_HZ = 300.0
GRID_HI_HZ = 16000.0
GRID_POINTS = 12000

#: Zero-padded transform length for the phase chain. Interpolation, not
#: resolution: it gives the local slope fit samples to work with.
PHASE_NFFT = 1 << 16

#: Deep nulls clamp here, relative to the in-band maximum, before the cepstral
#: transform. Un-clamped, a single near-zero bin dominates the whole Hilbert
#: relation.
LOGMAG_FLOOR_DB = 60.0

#: Out-of-band log-magnitude is held at the nearest in-band value across this
#: crossfade. The cepstral transform is a Hilbert transform over the WHOLE
#: spectrum, so leaving the gate's low-frequency floor and the deconvolution
#: regulariser's roll-off raw injects a large fake minimum-phase slope right
#: through the features. Holding them can only make excess phase FLATTER — it
#: cannot invent an excursion.
EDGE_LO_HZ = 250.0
EDGE_HI_HZ = 17000.0
EDGE_BLEND_OCT = 0.5

#: A feature must stand this many standard errors above the round's own
#: capture-to-capture scatter. Same z convention as :data:`Z_LOCAL_FLAT`: what
#: is being asked is whether the departure is a property of the speaker or of
#: which seat happened to be measured.
FEATURE_STABILITY_Z = 3.0

#: ...and this far from flat regardless. Below a tenth of a dB nothing
#: downstream can act on the departure: the per-driver seam's own narrowest
#: admissible filter is larger, so a "feature" this small can only produce a
#: refusal further down the line.
FEATURE_MIN_DEPARTURE_DB = 0.10

#: Two extrema closer together than half a feature band are one feature, not
#: two. Deliberately NOT
#: :data:`~.feature_classification.VERDICT_MATCH_TOLERANCE_OCTAVES`: the real
#: 2026-08-19 record has peak/dip pairs 0.143 and 0.157 octaves apart, inside
#: that tolerance, and merging them would delete four genuine features. The
#: register's own answer to close neighbours is the nearest-verdict-decides
#: rule, not a wider detector.
FEATURE_MIN_SEPARATION_OCT = FEATURE_HALF_OCT

#: Half-width of the direct-sound window the timing residual is measured over.
#: Wide enough to hold the arrival's own main lobe, narrow enough to exclude
#: the first reflection at any usable gate.
DIRECT_SOUND_HALF_WINDOW_MS = 1.0

#: Bounded work per round. The 2026-08-19 record found nine; a response that
#: offers more than this is reporting texture rather than features, and the
#: largest are the ones any filter would be aimed at. Each feature costs two
#: full excess-group-delay runs in the C4 control pair, so this is the
#: instrument's CPU bound as well as its honesty bound.
MAX_FEATURES = 12

# --- decision thresholds ---------------------------------------------------- #

#: Fraction of a genuine same-frequency non-minimum-phase comb's excursion (the
#: C4 control, measured on this round's own capture) below which the feature is
#: called minimum-phase, and at or above which it is called non-minimum-phase.
#: C4 and its minimum-phase twin separate by one to two orders of magnitude, so
#: the gap between these two is wide and nothing lands in it by accident.
FRAC_NMP_MIN_PHASE = 0.25
FRAC_NMP_NON_MIN_PHASE = 0.50

#: How far above the feature's own +/-1/3-octave excess-GD scatter an excursion
#: must sit before it is a feature of the response rather than of the noise.
Z_LOCAL_FLAT = 3.0

#: Floor on how far outside the null model's own retention range a feature may
#: drift before the gate is judged to have removed real energy. The live slack
#: is the larger of this and three times the retention's own paired scatter.
RETENTION_SLACK = 0.15

#: Centre movement, in octaves, that counts as the feature having moved.
CENTRE_SHIFT_OCT = 1.0 / 24.0

# --- control specifications and their bars ---------------------------------- #

CONTROL_PEAKING_GAIN_DB = 2.0
CONTROL_PEAKING_Q = 2.0
CONTROL_ALLPASS_HZ: tuple[float, ...] = (1000.0, 4500.0)
CONTROL_ALLPASS_Q = 1.0
CONTROL_ECHO_GAIN = -0.5
CONTROL_ECHO_MS = 0.4
CONTROL_COMB_NMP_GAIN = 1.2
CONTROL_COMB_MP_GAIN = 0.8

#: A known MINIMUM-phase magnitude change must move excess group delay by less
#: than this. It is the instrument's false-positive scale, and the 2026-08-19
#: passing run read 0.5 us against it.
CONTROL_MAX_FALSE_POSITIVE_US = 2.0

#: The quiet delayed copy is also minimum phase and must also read flat, at the
#: looser bar its much larger magnitude ripple earns.
CONTROL_MAX_ECHO_FALSE_POSITIVE_US = 10.0

#: The all-pass must recover its own group delay to within this band, read
#: against the exact digital filter's group delay over the same reference band
#: rather than against the raw 4Q/w0 peak — a 2nd-order all-pass sits on a
#: plateau below f0, so the raw peak marks a correct instrument wrong.
CONTROL_ALLPASS_RATIO_BAND = (0.85, 1.15)

#: A genuine cancellation must separate from its minimum-phase twin by this
#: multiple of the worst false positive above. Without it the two verdicts
#: would be a coin toss dressed as a threshold.
CONTROL_SEPARATION_MARGIN = 5.0

# --- narrow-band decay ------------------------------------------------------- #

#: The drop time-to-decay is measured against. Named once so the field
#: (``time_to_neg20_db_ms``) and the reachability test (``below_floor``) read
#: the same number back rather than one of them drifting from the other.
DECAY_TARGET_DROP_DB = 20.0

#: How far beyond a feature's own +/-``FEATURE_HALF_OCT`` skirts a flanking
#: decay band's matching skirt sits. One third-octave: the flanking reads
#: exist to show whether extended ringing is peculiar to the resonance or is
#: the room's tail everywhere nearby — the campaign's own ~48 ms on the
#: 898 Hz ridge against ~6-10 ms a third-octave outside it (same room, same
#: capture). Each flank is the SAME width as the centre band, not narrower.
DECAY_FLANK_SKIRT_OFFSET_OCT = 1.0 / 3.0

#: Fraction of a band-limited envelope's own tail read for its noise floor.
#: The IRs this reads are the full deconvolved capture, not a gated
#: fragment, so any decay this instrument could report has long since
#: finished by the last fifth of it; late enough to be floor, wide enough
#: that a handful of samples cannot set it.
DECAY_NOISE_FLOOR_TAIL_FRACTION = 0.2


# --------------------------------------------------------------------------- #
# banked round input
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoundCapture:
    """One banked capture, bound to the program that produced it."""

    wav: Path
    program: Path
    phase: str
    #: Capture stamp, seconds. The dump filename's own microsecond stamp.
    stamp: float
    #: Commanded turntable angle, when a walk log covered this capture.
    degrees: int | None


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Mono float samples and the file's own sample rate."""
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        width = handle.getsampwidth()
        channels = handle.getnchannels()
        rate = handle.getframerate()
        raw = handle.readframes(frames)
    dtype = {2: "<i2", 4: "<i4"}.get(width)
    if dtype is None:
        raise ValueError(f"{path.name}: unsupported {width * 8}-bit sample width")
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples / float(2 ** (8 * width - 1)), rate


def _walk_releases(walk_logs: Iterable[Path]) -> list[tuple[float, int]]:
    """Every ``released`` event a turntable walk log recorded, in time order."""
    releases: list[tuple[float, int]] = []
    for path in walk_logs:
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Walk trails carry occasional partial lines; a truncated one
                # is a missing angle, never a reason to abandon the log.
                continue
            if event.get("event") == "released":
                try:
                    releases.append((float(event["t"]), int(event["degrees"])))
                except (KeyError, TypeError, ValueError):
                    continue
    releases.sort()
    return releases


#: A capture is bound to the most recent turntable release at or before its own
#: stamp plus this much slack, and only if the release is no older than the
#: staleness bound. Both are the walk driver's own numbers.
WALK_BIND_SLACK_S = 2.0
WALK_BIND_MAX_AGE_S = 120.0


def _bind_angle(stamp: float, releases: Sequence[tuple[float, int]]) -> int | None:
    prior = [entry for entry in releases if entry[0] <= stamp + WALK_BIND_SLACK_S]
    if not prior or stamp - prior[-1][0] > WALK_BIND_MAX_AGE_S:
        return None
    return prior[-1][1]


def load_round_captures(
    round_dir: Path,
    dumps_dir: Path,
    *,
    session_id: str | None = None,
    walk_logs: Sequence[Path] = (),
) -> tuple[RoundCapture, ...]:
    """Bind one round's banked captures to the programs that produced them.

    ``round_dir`` is the round's own artifact directory — the
    ``evidence/v1/artifacts/crossover_v2/<relay-session-id>/`` one the packet
    reads, which is where the ``<phase>_program.wav`` files live and where the
    verdict is filed. ``dumps_dir`` is the banked capture ring's root: sidecar
    JSON beside its WAV, in either the flat ``wav/`` + ``sidecar/`` shape or a
    per-phase nesting of it, found by
    :data:`~.evidence_packet.RING_SIDECAR_GLOB` —
    :func:`~.harmonic_evidence.read_round_harmonics` reads the same ring
    through the same constant, so ``jasper-classify-features --dumps`` and
    ``jasper-read-distortion --dumps`` cannot come to mean different
    directories.

    ``session_id`` scopes the ring to this round. It is the BUNDLE session id
    (``info.json``'s ``session_id``), which is what a sidecar stamps into
    ``jts_session_identity``; the relay id that names ``round_dir`` is a
    different namespace. Omitting it admits every capture in the ring, which is
    correct only when the ring holds one round.

    ``walk_logs`` are turntable trails. They are optional: without them
    captures carry no angle and the timing test says it did not run.

    Raises :class:`FeatureClassificationRefused` — never returns an empty
    tuple, because "no captures" is a finding a caller must be told by name.
    EVERY refusal it raises carries ``captures``: one row per sidecar the ring
    listed (``sidecar`` / ``phase`` / ``admissible`` / ``reason``, the reason
    drawn from :data:`CAPTURE_ADMISSIBILITY_REASONS`), so a caller sees which
    capture was dropped and why rather than a count contradicting the ring
    listing it was just handed. Which name a no-capture round refuses under is
    read off those rows through :data:`_REFUSAL_FOR_CAPTURE_REASON`.
    """
    programs = {
        phase: path
        for phase in sorted(ADMISSIBLE_PHASES)
        if (path := round_dir / f"{phase}_program.wav").is_file()
    }
    releases = _walk_releases(walk_logs)

    captures: list[RoundCapture] = []
    seen_phases: dict[str, int] = {}
    lateral: list[str] = []
    missing_program: list[str] = []
    census: list[dict[str, Any]] = []

    def note(sidecar: Path, phase: Any, reason: str) -> None:
        """One row of the per-capture admissibility table.

        Every drop below records why, so a refusal names the captures the ring
        listing just handed the caller instead of only counting them (#3480).
        """
        census.append({
            "sidecar": sidecar.name,
            "phase": phase if isinstance(phase, str) else None,
            "admissible": reason == CAPTURE_ADMISSIBLE,
            "reason": reason,
        })

    for sidecar in sorted(dumps_dir.glob(RING_SIDECAR_GLOB)):
        try:
            doc = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError):
            note(sidecar, None, CAPTURE_UNREADABLE_SIDECAR)
            continue
        if not isinstance(doc, Mapping):
            note(sidecar, None, CAPTURE_UNREADABLE_SIDECAR)
            continue
        phase = doc.get("phase")
        if not isinstance(phase, str):
            note(sidecar, None, CAPTURE_UNREADABLE_SIDECAR)
            continue
        identity = doc.get("jts_session_identity")
        banked_session = (
            identity.get("session_id") if isinstance(identity, Mapping) else None
        )
        if session_id is not None and banked_session != session_id:
            note(sidecar, phase, CAPTURE_OTHER_SESSION)
            continue
        seen_phases[phase] = seen_phases.get(phase, 0) + 1
        if phase == PHASE_LATERAL:
            lateral.append(sidecar.name)
            note(sidecar, phase, CAPTURE_LATERAL_SHAPE)
            continue
        if phase not in ADMISSIBLE_PHASES:
            note(sidecar, phase, CAPTURE_PHASE_NOT_ADMISSIBLE)
            continue
        program = programs.get(phase)
        if program is None:
            missing_program.append(phase)
            note(sidecar, phase, CAPTURE_PROGRAM_MISSING)
            continue
        wav = sidecar.parent.parent / "wav" / f"{sidecar.stem}.wav"
        if not wav.is_file():
            note(sidecar, phase, CAPTURE_WAV_MISSING)
            continue
        # The dump filename opens with its own microsecond stamp.
        try:
            stamp = float(sidecar.stem.split("_")[0]) / 1e6
        except ValueError:
            note(sidecar, phase, CAPTURE_UNSTAMPED_NAME)
            continue
        note(sidecar, phase, CAPTURE_ADMISSIBLE)
        captures.append(
            RoundCapture(
                wav=wav,
                program=program,
                phase=phase,
                stamp=stamp,
                degrees=_bind_angle(stamp, releases),
            )
        )

    if lateral:
        raise FeatureClassificationRefused(
            LATERAL_CAPTURE_SHAPE,
            {
                "lateral_captures": len(lateral),
                "note": (
                    "a lateral capture replays the per-driver MEASURE program "
                    "one driver at a time, so it carries no summed-system "
                    "response for a feature verdict to be about"
                ),
                "captures": census,
            },
        )
    if missing_program:
        # Refused rather than pooled without them. A round whose bundle is
        # missing the program for a phase it banked captures of is incomplete,
        # and quietly classifying the half that survived would change the
        # answer without changing anything a reader could see.
        raise FeatureClassificationRefused(
            PROGRAM_MISSING,
            {
                "phases": sorted(set(missing_program)),
                "captures_dropped": len(missing_program),
                "round_dir": round_dir.name,
                "programs_present": sorted(programs),
                "captures": census,
            },
        )
    if not captures:
        # Read off the census, never off ``seen_phases``: a ring holding this
        # round's VERIFY sidecars whose WAVs are gone has a non-empty phase
        # count and a right round shape, and sending that driver at a
        # verify-shaped round is #3480 in the vocabulary that replaced it.
        speaks = {_REFUSAL_FOR_CAPTURE_REASON[row["reason"]] for row in census}
        reason = next(
            (slug for slug in _REFUSAL_PRECEDENCE if slug in speaks),
            NO_ADMISSIBLE_CAPTURES,
        )
        raise FeatureClassificationRefused(
            reason,
            {
                "admissible_phases": sorted(ADMISSIBLE_PHASES),
                "phases_seen": seen_phases,
                "session_id": session_id,
                "dumps_dir": dumps_dir.name,
                "note": _NO_CAPTURE_REMEDY[reason],
                "captures": census,
            },
        )
    return tuple(sorted(captures, key=lambda cap: cap.stamp))


@dataclass(frozen=True)
class RoundPoseCurve:
    """One banked lateral-walk pose's one driver-role curve, magnitude only.

    Read from :func:`~.spatial.pose_curve_record`'s magnitude+phase bank
    (Wave 4a / ruling S3) through the same reader :mod:`.delay_landscape` and
    ``jasper-delay-sweep`` already use to reconstruct a transfer function --
    never a raw lateral WAV, and never a second tree-walker over the bundle
    (house ruling R11). ``band_hz`` is the role's own driven sweep band,
    parsed by the shared :func:`~.position_cycle.parse_curve_magnitude` —
    the same step the delay-sweep reader layers phase onto.
    """

    pose_id: str
    position_deg: int | None
    role: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    band_hz: tuple[float, float]


def load_round_pose_curves(bundle_dir: Path) -> tuple[RoundPoseCurve, ...]:
    """Every banked lateral-walk pose curve in this bundle, magnitude only.

    ``bundle_dir`` is the commissioning bundle -- the same directory
    :func:`load_round_captures` and :func:`~.evidence_packet.round_artifact_dir`
    read, not the round's own artifact directory.

    Reused, not re-walked: :func:`~.record_index.bundle_measurements` is the
    same take index the evidence packet's ``lateral_poses`` block and
    ``jasper-delay-sweep propose`` scan, and
    :func:`~.position_cycle.read_take_curves` is the same banked-curve reader
    :func:`~.spatial.pose_curve_record` writes. Phase is dropped -- a
    persistence read is magnitude-only -- so a caller wanting the pair reads
    ``read_take_curves`` directly.

    Returns one entry per (pose stop, role): a stop that measured two driver
    curves banks two entries sharing one ``pose_id``. **Latest attempt wins,
    per stop** -- a retake's superseded attempts stay banked as the honest
    walk record, but only the newest readable take speaks for its stop
    (:func:`~.spatial.take_stop_id` groups them; the same rule
    :func:`~.position_cycle.read_pose_curve_pair` applies), so a persistence
    or pooling read never averages a retake with the noise it replaced.
    Empty when this round ran no lateral walk, or banked none that survived
    to a curve -- never raises, which is what lets :func:`classify_round`
    tell "no lateral walk" from "a walk that measured nothing usable" apart
    from a directory error.
    """
    from .position_cycle import parse_curve_magnitude, read_take_curves
    from .record_index import Measurement, bundle_measurements
    from .spatial import take_stop_id

    artifacts = Path(bundle_dir) / EVIDENCE_ROOT / "artifacts"
    # Rows arrive in path order, which the zero-padded attempt ids make
    # chronological, so the last readable write per stop IS the newest
    # readable attempt.
    latest_by_stop: dict[str, tuple[Measurement, list[Mapping[str, Any]]]] = {}
    for row in bundle_measurements(bundle_dir, phase=PHASE_LATERAL):
        curves = read_take_curves(artifacts / row.path, phase=PHASE_LATERAL)
        if curves is None:
            continue
        latest_by_stop[take_stop_id(Path(row.path).stem)] = (row, curves)
    out: list[RoundPoseCurve] = []
    for row, curves in latest_by_stop.values():
        pose_id = Path(row.path).stem
        for curve in curves:
            role = curve.get("role")
            if not isinstance(role, str):
                continue
            parsed = parse_curve_magnitude(curve)
            if parsed is None:
                continue
            freqs_arr, mag_arr, band_tuple = parsed
            out.append(
                RoundPoseCurve(
                    pose_id=pose_id,
                    position_deg=row.position_deg,
                    role=role,
                    freqs_hz=freqs_arr,
                    magnitude_db=mag_arr,
                    band_hz=band_tuple,
                )
            )
    return tuple(out)


# --------------------------------------------------------------------------- #
# gating and magnitude
# --------------------------------------------------------------------------- #


def gate(
    ir: np.ndarray,
    sample_rate: int,
    *,
    gate_ms: float,
    lead_ms: float = 0.0,
    peak: int | None = None,
) -> np.ndarray:
    """A FIXED-length window from the IR peak, optionally with a pre-peak lead.

    Deliberately not :func:`~jasper.audio_measurement.gating.gate_impulse_response`,
    whose window is sized from a DETECTED reflection. The gate-invariance test's
    whole subject is what a window of a stated length does to a feature, so the
    length must be the caller's, identical for the measured IR and for the null
    model it is compared against. The window shape is the same family: flat to
    the peak, decaying half-Hann after it, raised-cosine fade into any lead.
    """
    if peak is None:
        peak = int(np.argmax(np.abs(ir)))
    span = int(round(gate_ms * 1e-3 * sample_rate))
    lead = int(round(lead_ms * 1e-3 * sample_rate))
    start = max(0, peak - lead)
    lead = peak - start
    segment = np.asarray(ir[start:start + lead + span], dtype=np.float64)
    if segment.size < lead + span:
        segment = np.pad(segment, (0, lead + span - segment.size))
    window = np.ones(segment.size)
    window[lead:] = np.hanning(span * 2)[span:]
    if lead:
        window[:lead] = np.hanning(lead * 2)[:lead]
    return segment * window


def analysis_grid() -> np.ndarray:
    return np.geomspace(GRID_LO_HZ, GRID_HI_HZ, GRID_POINTS)


def smoothed_curve(
    segment: np.ndarray, sample_rate: int, grid: np.ndarray
) -> np.ndarray:
    """Gated segment to a normalised fractional-octave dB curve on ``grid``.

    Smoothing is the product's own power-mean
    (:func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`), run
    on the linear rfft grid it expects and then interpolated onto the log grid.
    The lab harness used an arithmetic dB mean of its own; the numbers here are
    therefore this instrument's, not a reproduction of the night's.
    """
    freqs, db = magnitude_response(segment.astype(np.float32), sample_rate)
    keep = np.isfinite(db) & (freqs >= GRID_LO_HZ * 0.8) & (freqs <= GRID_HI_HZ * 1.2)
    smoothed = smooth_fractional_octave(
        freqs[keep], db[keep], MAGNITUDE_SMOOTH_FRACTION
    )
    curve = np.interp(grid, freqs[keep], smoothed)
    band = (grid >= NORMALISE_BAND_HZ[0]) & (grid <= NORMALISE_BAND_HZ[1])
    return curve - float(np.median(curve[band]))


def detrend(curve: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Remove the broad tilt so a narrow feature's own size is what is read."""
    return curve - smooth_fractional_octave(grid, curve, DETREND_FRACTION)


def read_feature(det_curve: np.ndarray, grid: np.ndarray, fc: float) -> float:
    """A feature's size: the mean of the detrended curve over its own band."""
    band = (grid >= fc * 2 ** -FEATURE_HALF_OCT) & (grid <= fc * 2 ** FEATURE_HALF_OCT)
    return float(np.mean(det_curve[band]))


#: Width used when a feature never returns to half amplitude inside its search
#: span. Wide enough that the null model built from it is not accidentally
#: narrower than the feature, which is the error direction that makes a real
#: feature look reflection-contaminated.
_Q_FALLBACK = 4.0


def feature_q(det_curve: np.ndarray, grid: np.ndarray, fc: float) -> float:
    """Measured Q of a feature, from its own half-amplitude width.

    The gate null model is only as good as the width it assumes: too low a Q
    makes a speaker-own feature look reflection-contaminated by comparison. So
    the width is measured off the feature rather than guessed, and falls back
    to a wide-but-plausible value when the curve never returns to half
    amplitude inside the search span.
    """
    search = (grid >= fc * 2 ** -NEIGHBOURHOOD_OCT) & (
        grid <= fc * 2 ** NEIGHBOURHOOD_OCT
    )
    freqs, values = grid[search], det_curve[search]
    if freqs.size < 3:
        return _Q_FALLBACK
    apex = int(np.argmax(np.abs(values)))
    height = values[apex]
    if height == 0:
        return _Q_FALLBACK
    below = (values / height) < 0.5
    lo, hi = freqs[0], freqs[-1]
    for i in range(apex, -1, -1):
        if below[i]:
            lo = freqs[i]
            break
    for i in range(apex, freqs.size):
        if below[i]:
            hi = freqs[i]
            break
    if hi <= lo:
        return _Q_FALLBACK
    return float(np.sqrt(lo * hi) / (hi - lo))


# --------------------------------------------------------------------------- #
# excess group delay
# --------------------------------------------------------------------------- #


def _hold_band_edges(freqs: np.ndarray, logmag: np.ndarray) -> np.ndarray:
    """Blend out-of-band log-magnitude to the nearest in-band value."""
    out = logmag.copy()
    lo_ref = float(np.median(logmag[(freqs >= EDGE_LO_HZ) & (freqs <= EDGE_LO_HZ * 1.3)]))
    hi_ref = float(
        np.median(logmag[(freqs >= EDGE_HI_HZ / 1.3) & (freqs <= EDGE_HI_HZ)])
    )

    lo_start = EDGE_LO_HZ * 2 ** -EDGE_BLEND_OCT
    out[freqs <= lo_start] = lo_ref
    ramp = (freqs > lo_start) & (freqs < EDGE_LO_HZ)
    if ramp.any():
        t = np.log2(freqs[ramp] / lo_start) / EDGE_BLEND_OCT
        w = 0.5 - 0.5 * np.cos(np.pi * t)
        out[ramp] = (1 - w) * lo_ref + w * logmag[ramp]

    hi_end = EDGE_HI_HZ * 2 ** EDGE_BLEND_OCT
    out[freqs >= hi_end] = hi_ref
    ramp = (freqs > EDGE_HI_HZ) & (freqs < hi_end)
    if ramp.any():
        t = np.log2(freqs[ramp] / EDGE_HI_HZ) / EDGE_BLEND_OCT
        w = 0.5 - 0.5 * np.cos(np.pi * t)
        out[ramp] = (1 - w) * logmag[ramp] + w * hi_ref
    return out


def minimum_phase(logmag_half: np.ndarray, n_fft: int = PHASE_NFFT) -> np.ndarray:
    """Minimum phase, radians, from a half-spectrum natural-log magnitude.

    Real-cepstrum folding: ln|H| to cepstrum, zero the anticausal quefrencies
    and double the causal ones, back to frequency; the imaginary part is the
    minimum phase. Exact for a minimum-phase system.
    """
    full = np.concatenate([logmag_half, logmag_half[-2:0:-1]])
    if full.size != n_fft:
        raise ValueError(f"log-magnitude length {full.size} != n_fft {n_fft}")
    cepstrum = np.fft.ifft(full).real
    fold = np.zeros(n_fft)
    fold[0] = 1.0
    fold[n_fft // 2] = 1.0
    fold[1:n_fft // 2] = 2.0
    return np.fft.fft(cepstrum * fold).imag[: n_fft // 2 + 1]


def _complex_smooth(freqs: np.ndarray, spectrum: np.ndarray, frac: float) -> np.ndarray:
    """Running mean of a complex spectrum over a fractional-octave span.

    Only meaningful once bulk delay is removed — a rotating phasor averages
    towards zero — which :func:`excess_group_delay` guarantees before calling.
    """
    lo, hi = 2 ** (-frac / 2), 2 ** (frac / 2)
    csum = np.concatenate([[0.0 + 0j], np.cumsum(spectrum)])
    a = np.searchsorted(freqs, freqs * lo)
    b = np.maximum(np.searchsorted(freqs, freqs * hi), a + 1)
    return (csum[b] - csum[a]) / (b - a)


def _window_slopes(
    y: np.ndarray, lo_idx: np.ndarray, hi_idx: np.ndarray
) -> np.ndarray:
    """Least-squares slope of ``y`` against sample INDEX in each window.

    The transform grid is uniform, so the regressor is the index and the
    denominator ``n(n^2-1)/12`` is exact — no accumulation error, and no
    per-window ``polyfit`` call. Doing this by prefix sums rather than a Python
    loop is what keeps a run bounded: the in-band span is ~21 000 bins and the
    instrument fits a slope at every one of them, dozens of times per round.
    """
    n = y.size
    idx = np.arange(n, dtype=np.float64)
    cum_y = np.concatenate([[0.0], np.cumsum(y)])
    cum_iy = np.concatenate([[0.0], np.cumsum(idx * y)])
    count = (hi_idx - lo_idx).astype(np.float64)
    sum_y = cum_y[hi_idx] - cum_y[lo_idx]
    sum_iy = cum_iy[hi_idx] - cum_iy[lo_idx]
    mean_idx = (lo_idx + hi_idx - 1) / 2.0
    numerator = sum_iy - mean_idx * sum_y
    denominator = count * (count * count - 1.0) / 12.0
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = numerator / denominator
    return np.where(count >= _MIN_SLOPE_SAMPLES, slope, np.nan)


#: Fewest samples a local slope may be fitted through. Below this the fit is
#: reading two points and a rounding error.
_MIN_SLOPE_SAMPLES = 4


@dataclass(frozen=True)
class ExcessPhase:
    """One capture's excess phase, bulk time-of-flight removed."""

    freqs: np.ndarray
    excess_phase: np.ndarray
    excess_gd_us: np.ndarray
    bulk_delay_us: float


def excess_group_delay(
    segment: np.ndarray,
    sample_rate: int,
    *,
    trusted_band_hz: tuple[float, float],
    fit_band_hz: tuple[float, float] = (400.0, 12000.0),
) -> ExcessPhase:
    """Excess group delay of a gated response, in microseconds.

    In the order that matters: transform with heavy zero-padding; estimate and
    remove bulk time-of-flight as a linear phase fit, without which the complex
    smoothing that follows would average rotating phasors to nothing;
    complex-smooth; take minimum phase from the smoothed log-magnitude with the
    band edges held; subtract it and remove any residual linear trend; then
    differentiate by a local linear fit so the derivative is not a noise
    amplifier.

    **What it cannot see, permanently.** Bulk delay removal cannot tell a
    smooth band-wide all-pass from metres of air, so a crossover's own global
    phase rotation is removed along with the time of flight. Only LOCALISED
    excess phase is detectable — which is what a cancellation produces, and is
    the target — but "minimum-phase" from this instrument means "no local
    excess phase at this feature", never "the whole response is minimum-phase".
    """
    spectrum = np.fft.rfft(np.asarray(segment, dtype=np.float64), n=PHASE_NFFT)
    freqs = np.fft.rfftfreq(PHASE_NFFT, d=1.0 / sample_rate)
    omega = 2 * np.pi * freqs

    fit = (freqs >= fit_band_hz[0]) & (freqs <= fit_band_hz[1])
    phase = np.unwrap(np.angle(spectrum))
    tau0 = -float(np.polyfit(omega[fit], phase[fit], 1)[0])
    rotated = spectrum * np.exp(1j * omega * tau0)

    first = max(1, int(np.searchsorted(freqs, EDGE_LO_HZ * 2 ** -EDGE_BLEND_OCT / 2)))
    smoothed = rotated.copy()
    smoothed[first:] = _complex_smooth(
        freqs[first:], rotated[first:], COMPLEX_SMOOTH_OCT
    )

    in_band = (freqs >= trusted_band_hz[0]) & (freqs <= trusted_band_hz[1])
    magnitude = np.abs(smoothed)
    reference = float(np.max(magnitude[in_band])) if in_band.any() else 0.0
    magnitude = np.maximum(magnitude, reference * 10 ** (-LOGMAG_FLOOR_DB / 20))
    logmag = _hold_band_edges(freqs, np.log(magnitude))

    excess = np.unwrap(np.angle(smoothed)) - minimum_phase(logmag)
    residual = np.polyfit(omega[fit], excess[fit], 1)
    excess = excess - np.polyval(residual, omega)

    lo_idx = np.searchsorted(freqs, freqs * 2 ** -GD_SPAN_OCT)
    hi_idx = np.minimum(np.searchsorted(freqs, freqs * 2 ** GD_SPAN_OCT) + 1, freqs.size)
    d_omega = 2 * np.pi * sample_rate / PHASE_NFFT
    group_delay = -_window_slopes(excess, lo_idx, hi_idx) / d_omega
    group_delay[~in_band] = np.nan

    return ExcessPhase(
        freqs=freqs[in_band],
        excess_phase=excess[in_band],
        excess_gd_us=group_delay[in_band] * 1e6,
        bulk_delay_us=float((tau0 - residual[0]) * 1e6),
    )


def egd_excursion(
    ep: ExcessPhase, fc: float, trusted_band_hz: tuple[float, float]
) -> dict[str, Any]:
    """A feature's excess-GD excursion against its own neighbourhood.

    Two metrics, because one would miss half the physics. ``excursion_us`` is
    the SIGNED peak departure of the feature band from the neighbourhood
    median — a local detector, matched to the narrow features under test, and a
    cancellation throws exactly that shape. ``p2p_us`` is peak-to-peak across
    the whole neighbourhood — a broad detector, because a gentle all-pass lifts
    the neighbourhood together and the local metric would read it as flat.

    ``clean`` is False when the neighbourhood runs off the trusted band: a
    verdict there would be reading the band edge, not the speaker.
    """
    freqs, gd = ep.freqs, ep.excess_gd_us
    finite = np.isfinite(gd)
    feature = (
        (freqs >= fc * 2 ** -FEATURE_HALF_OCT)
        & (freqs <= fc * 2 ** FEATURE_HALF_OCT)
        & finite
    )
    wide = (
        (freqs >= fc * 2 ** -NEIGHBOURHOOD_OCT)
        & (freqs <= fc * 2 ** NEIGHBOURHOOD_OCT)
        & finite
    )
    neighbourhood = wide & ~feature
    if not (feature.any() and neighbourhood.any()):
        return {
            "excursion_us": float("nan"),
            "p2p_us": float("nan"),
            "nbhd_median_us": float("nan"),
            "nbhd_sd_us": float("nan"),
            "at_hz": float("nan"),
            "clean": False,
        }
    base = float(np.median(gd[neighbourhood]))
    departure = gd[feature] - base
    peak = int(np.argmax(np.abs(departure)))
    return {
        "excursion_us": float(departure[peak]),
        "p2p_us": float(np.max(gd[wide]) - np.min(gd[wide])),
        "at_hz": float(freqs[feature][peak]),
        "nbhd_median_us": base,
        "nbhd_sd_us": float(np.std(gd[neighbourhood], ddof=1)),
        "clean": bool(
            feature.sum() > 8
            and neighbourhood.sum() > 16
            and fc * 2 ** -NEIGHBOURHOOD_OCT >= trusted_band_hz[0]
            and fc * 2 ** NEIGHBOURHOOD_OCT <= trusted_band_hz[1]
        ),
    }


# --------------------------------------------------------------------------- #
# synthetic perturbations — the controls' known answers
# --------------------------------------------------------------------------- #


def biquad_peaking(
    f0: float, gain_db: float, q: float, sample_rate: int
) -> tuple[np.ndarray, np.ndarray]:
    """RBJ peaking EQ. Minimum phase by construction."""
    amp = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / sample_rate
    alpha = np.sin(w0) / (2 * q)
    b = np.array([1 + alpha * amp, -2 * np.cos(w0), 1 - alpha * amp])
    a = np.array([1 + alpha / amp, -2 * np.cos(w0), 1 - alpha / amp])
    return b / a[0], a / a[0]


def biquad_allpass(
    f0: float, q: float, sample_rate: int
) -> tuple[np.ndarray, np.ndarray]:
    """RBJ 2nd-order all-pass: flat magnitude, pure excess phase.

    The cleanest possible non-minimum-phase control — it changes nothing a
    magnitude measurement can see, so anything the pipeline reports here is
    excess phase and nothing else.
    """
    w0 = 2 * np.pi * f0 / sample_rate
    alpha = np.sin(w0) / (2 * q)
    b = np.array([1 - alpha, -2 * np.cos(w0), 1 + alpha])
    a = np.array([1 + alpha, -2 * np.cos(w0), 1 - alpha])
    return b / a[0], a / a[0]


def add_delayed_copy(
    ir: np.ndarray, gain: float, delay_ms: float, sample_rate: int
) -> np.ndarray:
    """``h(t) + gain*h(t - delay)``.

    ``|gain| < 1`` keeps every zero INSIDE the unit circle, so the result is
    MINIMUM phase; only ``|gain| > 1`` is genuinely non-minimum-phase. That is
    not a quibble — it is the physics the whole classification turns on, and it
    is why the research spec's own ``-0.5`` interference case is a NEGATIVE
    control here.
    """
    shift = int(round(delay_ms * 1e-3 * sample_rate))
    out = ir.copy()
    out[shift:] += gain * ir[: ir.size - shift]
    return out


def tau_for_first_null_ms(fc: float) -> float:
    """Delay whose first two-path null lands on ``fc``.

    ``h(t) + g*h(t-tau)`` with ``g > 0`` nulls at ``(n + 1/2)/tau``, so the
    first rung sits at ``1/(2*tau)`` — the same ladder law the shipped
    interference-null instrument fits.
    """
    return 1e3 / (2.0 * fc)


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


def _apply(ir: np.ndarray, coeffs: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    from scipy.signal import lfilter

    return np.asarray(lfilter(coeffs[0], coeffs[1], ir), dtype=np.float64)


def classifiable_band_hz(trusted_band_hz: tuple[float, float]) -> tuple[float, float]:
    """Where a verdict can be about the SPEAKER rather than about the band edge.

    A feature is read against its own +/-1/3-octave neighbourhood, so the
    neighbourhood has to fit inside the trusted band — otherwise
    :func:`egd_excursion` reports ``clean=False`` and the "excursion" is the
    edge of the measurement. This is that band, and it bounds detection AND an
    explicitly requested frequency: a caller asking about 15 kHz on a 7 ms gate
    is asking a question this instrument cannot answer.
    """
    lo, hi = trusted_band_hz
    return lo * 2**NEIGHBOURHOOD_OCT, hi * 2**-NEIGHBOURHOOD_OCT


def _detect_features(
    per_capture_detrended: np.ndarray,
    pooled_curve: np.ndarray,
    grid: np.ndarray,
    band_hz: tuple[float, float],
) -> list[float]:
    """Which frequencies this round has features at, from its own captures.

    Three things have to be true at once, and each rejects a different way of
    being wrong:

    * **Two-sided prominence in the RAW pooled curve.** A real peak falls away
      on BOTH sides and a real dip rises on both. The shoulder of a large
      resonance does neither — it is flat on one side and climbing on the
      other — and a one-sided test calls that shoulder a feature. Removing the
      broad tilt MANUFACTURES those shoulders (a 1-octave baseline under a
      +3 dB resonance leaves a −0.7 dB trough on each flank), so the
      prominence is read before the detrend, never after it. This is
      ``scipy``'s own topographic prominence, bounded to the neighbourhood
      width so a distant feature cannot supply the col.
    * **A trend-removed size of the same sign.** The size that is reported and
      that the gate test compares against is the detrended one; when the two
      disagree about which way the feature points, neither is trustworthy.
    * **Stability across the round's captures.** A departure that stands
      :data:`FEATURE_STABILITY_Z` standard errors above the capture-to-capture
      scatter is a property of the speaker; one that does not is a property of
      where the microphone was, and the register's readers are asking about
      the speaker.
    """
    from scipy.signal import find_peaks

    pooled = per_capture_detrended.mean(axis=0)
    n = per_capture_detrended.shape[0]
    scatter = (
        per_capture_detrended.std(axis=0, ddof=1) / math.sqrt(n)
        if n > 1
        else np.zeros_like(pooled)
    )
    # The grid is geometric, so an octave is a constant number of samples.
    samples_per_octave = (grid.size - 1) / math.log2(grid[-1] / grid[0])
    wlen = max(3, int(round(2 * NEIGHBOURHOOD_OCT * samples_per_octave)))

    candidates: list[int] = []
    for sign, curve in ((+1.0, pooled_curve), (-1.0, -pooled_curve)):
        found, _ = find_peaks(
            curve, prominence=FEATURE_MIN_DEPARTURE_DB, wlen=wlen
        )
        candidates.extend(
            int(i) for i in found if np.sign(pooled[i]) == sign
        )

    magnitude = np.abs(pooled)
    admitted = [
        i
        for i in sorted(set(candidates))
        if band_hz[0] <= grid[i] <= band_hz[1]
        and magnitude[i] >= FEATURE_MIN_DEPARTURE_DB
        and magnitude[i] >= FEATURE_STABILITY_Z * scatter[i]
    ]
    if not admitted:
        return []

    kept: list[float] = []
    for index in sorted(admitted, key=lambda i: -magnitude[i]):
        fc = float(grid[index])
        if any(
            abs(math.log2(fc / other)) < FEATURE_MIN_SEPARATION_OCT for other in kept
        ):
            continue
        kept.append(fc)
        if len(kept) == MAX_FEATURES:
            break
    return sorted(kept)


def _run_controls(
    ir: np.ndarray,
    sample_rate: int,
    features: Sequence[float],
    *,
    gate_ms: float,
    trusted_band_hz: tuple[float, float],
) -> dict[str, Any]:
    """Push known answers through the identical pipeline on a measured IR.

    A real IR rather than a synthetic impulse, so every control inherits the
    round's actual noise floor, gate truncation and room content.
    """

    # ONE transform per perturbed signal, then every feature's excursion read
    # off it. Recomputing the excess phase per (signal, feature) would multiply
    # the run by the feature count for no new information.
    def phase_of(signal: np.ndarray) -> ExcessPhase:
        return excess_group_delay(
            gate(signal, sample_rate, gate_ms=gate_ms, lead_ms=PHASE_GATE_LEAD_MS),
            sample_rate,
            trusted_band_hz=trusted_band_hz,
        )

    def excursions(ep: ExcessPhase) -> dict[str, dict[str, Any]]:
        return {
            f"{fc:.0f}": egd_excursion(ep, fc, trusted_band_hz) for fc in features
        }

    keys = [f"{fc:.0f}" for fc in features]
    base_ep = phase_of(ir)
    baseline = excursions(base_ep)

    # C1 — a known MINIMUM-phase magnitude change must not move excess GD. It
    # is placed on the round's own largest feature rather than at a lab
    # frequency, so the false-positive scale is measured where it matters.
    anchor = max(features, key=lambda fc: abs(baseline[f"{fc:.0f}"]["excursion_us"]))
    peaking = _apply(
        ir,
        biquad_peaking(anchor, CONTROL_PEAKING_GAIN_DB, CONTROL_PEAKING_Q, sample_rate),
    )
    c1 = excursions(phase_of(peaking))
    c1_delta = {
        key: c1[key]["excursion_us"] - baseline[key]["excursion_us"] for key in keys
    }

    # C2 — an all-pass, read as a difference curve referenced far from f0. The
    # excess-GD curve carries an arbitrary constant (bulk-delay removal absorbs
    # part of any added all-pass), so a raw before/after read at f0 would
    # measure the detrend rather than the filter. The expectation is the exact
    # digital filter's own group delay referenced over the identical band, NOT
    # the raw 4Q/w0 peak: a 2nd-order all-pass sits on a plateau below f0, and
    # comparing against the peak marks a correct instrument wrong.
    from scipy.signal import group_delay as _scipy_group_delay

    c2: dict[str, Any] = {}
    for f0 in CONTROL_ALLPASS_HZ:
        coeffs = biquad_allpass(f0, CONTROL_ALLPASS_Q, sample_rate)
        ep = phase_of(_apply(ir, coeffs))
        difference = ep.excess_gd_us - np.interp(
            ep.freqs, base_ep.freqs, base_ep.excess_gd_us
        )
        far = (ep.freqs < f0 * 2**-2) | (ep.freqs > f0 * 2**2)
        offset = float(np.nanmedian(difference[far])) if far.any() else 0.0
        recovered = float(np.interp(f0, ep.freqs, difference) - offset)
        _, filter_gd = _scipy_group_delay(coeffs, w=2 * np.pi * ep.freqs / sample_rate)
        filter_gd_us = filter_gd / sample_rate * 1e6
        expected = float(
            np.interp(f0, ep.freqs, filter_gd_us)
            - (np.nanmedian(filter_gd_us[far]) if far.any() else 0.0)
        )
        c2[f"{f0:.0f}"] = {
            "q": CONTROL_ALLPASS_Q,
            "recovered_us": recovered,
            "expected_same_reference_us": expected,
            "ratio": float(recovered / expected) if expected else float("nan"),
            "detrend_offset_removed_us": offset,
        }

    # C3 — the research spec's own interference case, run as specified and
    # reported honestly: |gain| = 0.5 is MINIMUM phase, so a correct pipeline
    # must read it flat. Flagging it would be a false positive, not a pass.
    echo = add_delayed_copy(ir, CONTROL_ECHO_GAIN, CONTROL_ECHO_MS, sample_rate)
    c3 = excursions(phase_of(echo))
    c3_delta = {
        key: c3[key]["excursion_us"] - baseline[key]["excursion_us"] for key in keys
    }

    # C4 / C4b — the discriminating pair, at every feature. Same comb geometry,
    # opposite phase class. If the instrument cannot separate them it cannot
    # classify anything, and the separation is also the SCALE every verdict is
    # read against.
    pair: dict[str, Any] = {}
    for key, fc in zip(keys, features):
        tau_ms = tau_for_first_null_ms(fc)
        nmp = egd_excursion(
            phase_of(add_delayed_copy(ir, CONTROL_COMB_NMP_GAIN, tau_ms, sample_rate)),
            fc,
            trusted_band_hz,
        )
        mp = egd_excursion(
            phase_of(add_delayed_copy(ir, CONTROL_COMB_MP_GAIN, tau_ms, sample_rate)),
            fc,
            trusted_band_hz,
        )
        pair[key] = {
            "tau_us": tau_ms * 1e3,
            "nmp_excursion_us": nmp["excursion_us"],
            "mp_excursion_us": mp["excursion_us"],
            "nmp_delta_us": nmp["excursion_us"] - baseline[key]["excursion_us"],
            "mp_delta_us": mp["excursion_us"] - baseline[key]["excursion_us"],
            "separation_us": abs(nmp["excursion_us"] - mp["excursion_us"]),
        }

    c1_max = max(abs(v) for v in c1_delta.values())
    c3_max = max(abs(v) for v in c3_delta.values())
    ratio_lo, ratio_hi = CONTROL_ALLPASS_RATIO_BAND
    c2_ok = all(ratio_lo <= entry["ratio"] <= ratio_hi for entry in c2.values())
    separation = min(entry["separation_us"] for entry in pair.values())
    required = CONTROL_SEPARATION_MARGIN * max(c1_max, c3_max, 1.0)
    # WHICH control missed, named once and reduced to ``passes`` — the same
    # four terms the boolean was spelled from. A run that fails is a fact
    # about one control, and #3480 met the same suite passing on one verify
    # round of a rig and failing on another with nothing saying which moved.
    failed = [
        name
        for name, ok in (
            ("C1_min_phase_peaking", c1_max < CONTROL_MAX_FALSE_POSITIVE_US),
            ("C2_allpass", c2_ok),
            ("C3_min_phase_echo", c3_max < CONTROL_MAX_ECHO_FALSE_POSITIVE_US),
            ("C4_pair", separation > required),
        )
        if not ok
    ]
    verdict = {
        "C1_max_false_positive_us": c1_max,
        "C1_limit_us": CONTROL_MAX_FALSE_POSITIVE_US,
        "C2_recovers_theory": c2_ok,
        "C2_ratio_band": [ratio_lo, ratio_hi],
        "C3_max_false_positive_us": c3_max,
        "C3_limit_us": CONTROL_MAX_ECHO_FALSE_POSITIVE_US,
        "C4_min_separation_us": separation,
        "C4_required_separation_us": required,
        "failed": failed,
        "passes": not failed,
    }
    return {
        "C0_untouched": baseline,
        "C1_min_phase_peaking": {
            "spec": (
                f"+{CONTROL_PEAKING_GAIN_DB} dB Q{CONTROL_PEAKING_Q} at "
                f"{anchor:.0f} Hz (RBJ peaking, minimum phase)"
            ),
            "expect": "flat excess GD — delta from C0 near zero",
            "at_hz": anchor,
            "delta_us": c1_delta,
        },
        "C2_allpass": {
            "spec": "RBJ 2nd-order all-pass; magnitude unchanged, pure excess phase",
            "expect": "recovered peak excess GD matches the filter's own",
            "at": c2,
        },
        "C3_min_phase_echo": {
            "spec": (
                f"h(t) {CONTROL_ECHO_GAIN:+}*h(t-{CONTROL_ECHO_MS}ms) — |g| < 1, "
                "so MINIMUM phase"
            ),
            "expect": "flat; flagging it would be a false positive, not a pass",
            "delta_us": c3_delta,
        },
        "C4_pair": {
            "spec": (
                f"h(t) + g*h(t-tau), tau = 1/(2*fc); g={CONTROL_COMB_NMP_GAIN} is "
                f"non-minimum phase, g={CONTROL_COMB_MP_GAIN} is minimum phase with "
                "the same comb geometry"
            ),
            "expect": "non-minimum-phase excursion far above its twin at every feature",
            "at": pair,
        },
        "verdict": verdict,
    }


def _gate_invariance(
    irs: Sequence[np.ndarray],
    peaks: Sequence[int],
    host: np.ndarray,
    host_peak: int,
    sample_rate: int,
    features: Sequence[float],
    grid: np.ndarray,
    gates_ms: Sequence[float],
    *,
    primary_ms: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, float]]:
    """Retention and centre movement across the gate ladder, against a null model.

    A shorter gate shrinks ANY narrow feature, reflection or not, so raw
    retention is uninterpretable alone. The null model is a minimum-phase
    peaking filter of the feature's OWN pooled size at its OWN measured Q,
    injected into a real IR and gated identically — speaker-own by
    construction, so whatever retention it loses is the gate alone. A
    1.5x-narrower companion brackets the width rather than betting on one.

    ``primary_ms`` is passed, never derived from the ladder: a commanded rung
    longer than the primary must stay a rung, or every verdict reference
    silently moves onto a reflection-admitting window.
    """
    primary = float(primary_ms)
    primary_key = f"{primary:.0f}"

    pooled = np.mean(
        [
            detrend(
                smoothed_curve(
                    gate(ir, sample_rate, gate_ms=primary, peak=peak), sample_rate, grid
                ),
                grid,
            )
            for ir, peak in zip(irs, peaks)
        ],
        axis=0,
    )
    measured_q = {f"{fc:.0f}": feature_q(pooled, grid, fc) for fc in features}
    pooled_db = {f"{fc:.0f}": read_feature(pooled, grid, fc) for fc in features}

    null_model: dict[str, Any] = {}
    for fc in features:
        key = f"{fc:.0f}"
        null_model[key] = {}
        for tag, q in (("matched", measured_q[key]), ("narrow", measured_q[key] * 1.5)):
            injected = _apply(
                host, biquad_peaking(fc, pooled_db[key], q, sample_rate)
            )
            recovered: dict[str, float] = {}
            for gate_ms in gates_ms:
                with_feature = detrend(
                    smoothed_curve(
                        gate(injected, sample_rate, gate_ms=gate_ms, peak=host_peak),
                        sample_rate,
                        grid,
                    ),
                    grid,
                )
                without = detrend(
                    smoothed_curve(
                        gate(host, sample_rate, gate_ms=gate_ms, peak=host_peak),
                        sample_rate,
                        grid,
                    ),
                    grid,
                )
                recovered[f"{gate_ms:.0f}"] = read_feature(
                    with_feature - without, grid, fc
                )
            at_primary = recovered[primary_key]
            null_model[key][tag] = {
                "q": float(q),
                "injected_db": pooled_db[key],
                "recovered_db": recovered,
                "retention": {
                    k: (v / at_primary if at_primary else float("nan"))
                    for k, v in recovered.items()
                },
            }

    sizes: dict[str, dict[float, list[float]]] = {
        f"{g:.0f}": {fc: [] for fc in features} for g in gates_ms
    }
    centres: dict[str, dict[float, list[float]]] = {
        f"{g:.0f}": {fc: [] for fc in features} for g in gates_ms
    }
    for ir, peak in zip(irs, peaks):
        for gate_ms in gates_ms:
            key = f"{gate_ms:.0f}"
            curve = detrend(
                smoothed_curve(
                    gate(ir, sample_rate, gate_ms=gate_ms, peak=peak), sample_rate, grid
                ),
                grid,
            )
            for fc in features:
                sizes[key][fc].append(read_feature(curve, grid, fc))
                _, centre_hz = _extremum_reading(
                    curve, grid, fc, pooled_db[f"{fc:.0f}"] < 0
                )
                centres[key][fc].append(centre_hz)

    table: dict[str, Any] = {}
    for fc in features:
        key = f"{fc:.0f}"
        cell: dict[str, Any] = {}
        for gate_ms in gates_ms:
            gate_key = f"{gate_ms:.0f}"
            values = np.array(sizes[gate_key][fc])
            centre = np.array(centres[gate_key][fc])
            width_hz = fc * (2**FEATURE_HALF_OCT - 2**-FEATURE_HALF_OCT)
            cell[gate_key] = {
                "db_mean": float(values.mean()),
                "db_values": [float(v) for v in values],
                "centre_hz_mean": float(centre.mean()),
                "resolution_hz": 1e3 / gate_ms,
                "feature_width_hz": width_hz,
                # 1/gate is a hard limit and is not negotiable: at 1037 Hz a
                # 3 ms gate resolves 333 Hz against a ~120 Hz feature. In an
                # unresolved cell the DEPTH is still comparable against the
                # null model (both are smeared identically); the CENTRE is not.
                "resolved": bool(1e3 / gate_ms <= width_hz),
            }
        reference = np.array(cell[primary_key]["db_values"])
        reference_mean = cell[primary_key]["db_mean"]
        for gate_ms in gates_ms:
            gate_key = f"{gate_ms:.0f}"
            entry = cell[gate_key]
            entry["retention"] = (
                float(entry["db_mean"] / reference_mean)
                if reference_mean
                else float("nan")
            )
            # PAIRED: each capture's retention against ITS OWN primary-gate
            # reading. Propagating the two gates' scatter as independent
            # inflated the slack enough that the test could never say MOVED.
            paired = np.array(entry["db_values"]) / reference
            entry["retention_per_capture_sd"] = (
                float(paired.std(ddof=1)) if paired.size > 1 else 0.0
            )
            model = [
                null_model[key][tag]["retention"][gate_key]
                for tag in ("matched", "narrow")
            ]
            entry["null_model_retention"] = model
            entry["excess_loss_vs_null"] = float(entry["retention"] - min(model))
            entry["centre_shift_oct"] = float(
                np.log2(entry["centre_hz_mean"] / cell[primary_key]["centre_hz_mean"])
            )
        table[key] = cell
    return table, null_model, pooled_db


def _timing_scatter(
    captures: Sequence[RoundCapture],
    irs: Sequence[np.ndarray],
    peaks: Sequence[int],
    sample_rate: int,
    trusted_band_hz: tuple[float, float],
) -> dict[str, Any]:
    """Arrival-time scatter between captures at the SAME angle.

    The raw arrival spread is dominated by the relay's capture-start offset,
    which every other test removes by re-finding the peak. The SUB-SAMPLE
    residual is what survives into a phase comparison, and it is measured by
    cross-spectrum phase slope over the direct-sound window.

    It needs an angle to have been visited twice. When no pair exists the
    result says NOT RUN and carries no numbers: a dimension that did not run is
    a different fact from one that measured zero, and confidence must not read
    the second off the first.
    """
    arrivals = [peak / sample_rate * 1e3 for peak in peaks]
    spread = {
        "min_ms": float(min(arrivals)),
        "max_ms": float(max(arrivals)),
        "spread_ms": float(max(arrivals) - min(arrivals)),
    }

    by_angle: dict[int, list[int]] = {}
    for index, capture in enumerate(captures):
        if capture.degrees is not None:
            by_angle.setdefault(capture.degrees, []).append(index)

    half = int(round(DIRECT_SOUND_HALF_WINDOW_MS * 1e-3 * sample_rate))

    def _direct(index: int) -> np.ndarray:
        start = max(0, peaks[index] - half)
        return irs[index][start : peaks[index] + half]

    residuals: list[float] = []
    for indexes in (by_angle[angle] for angle in sorted(by_angle)):
        for i in range(len(indexes)):
            for j in range(i + 1, len(indexes)):
                a, b = _direct(indexes[i]), _direct(indexes[j])
                if a.size != b.size:
                    # One capture's peak sits inside the first millisecond, so
                    # the two direct-sound windows are not the same shape and
                    # differencing them would measure the truncation.
                    continue
                residuals.append(
                    _subsample_delay_us(a, b, sample_rate, trusted_band_hz)
                )

    if not residuals:
        return {
            "available": False,
            "n_pairs": 0,
            "raw_arrival_ms": spread,
            "note": (
                "NOT RUN: no angle was captured twice, so no pair of captures "
                "shares a geometry to difference. This is an unmeasured "
                "dimension, not a measured zero."
            ),
        }
    values = np.array(residuals)
    return {
        "available": True,
        "n_pairs": int(values.size),
        "raw_arrival_ms": spread,
        "subsample_residual_us": {
            "sd": float(values.std(ddof=1)) if values.size > 1 else 0.0,
            "max_abs": float(np.abs(values).max()),
        },
        "phase_error_deg_at_2k": float(
            360 * 2000 * np.abs(values).max() * 1e-6
        ),
    }


def _subsample_delay_us(
    a: np.ndarray, b: np.ndarray, sample_rate: int, trusted_band_hz: tuple[float, float]
) -> float:
    n = 1 << 14
    spectrum_a = np.fft.rfft(a, n)
    spectrum_b = np.fft.rfft(b, n)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    band = (freqs >= trusted_band_hz[0]) & (freqs <= min(8000.0, trusted_band_hz[1]))
    cross = spectrum_a[band] * np.conj(spectrum_b[band])
    slope = np.polyfit(
        2 * np.pi * freqs[band], np.unwrap(np.angle(cross)), 1, w=np.abs(cross)
    )[0]
    return float(-slope * 1e6)


# --------------------------------------------------------------------------- #
# off-axis persistence -- a fact read from ALREADY-BANKED lateral pose curves
# --------------------------------------------------------------------------- #

#: Fewest samples inside a centre-search span for its extremum to mean
#: anything, on the lateral walk's own coarse evidence grid
#: (``spatial.LATERAL_EVIDENCE_POINTS_PER_OCTAVE`` = 12/octave -- "not a polar
#: measurement", by that module's own words). Lower than
#: :data:`_MIN_SLOPE_SAMPLES`: this reads an extremum, not a fitted slope.
_POSE_MIN_CENTRE_SAMPLES = 3


def _centre_search_mask(freqs: np.ndarray, fc: float) -> np.ndarray:
    return (freqs >= fc * 2**-CENTRE_SEARCH_OCT) & (freqs <= fc * 2**CENTRE_SEARCH_OCT)


def _extremum_reading(
    values: np.ndarray, freqs: np.ndarray, fc: float, is_dip: bool
) -> tuple[float, float]:
    """The extremum inside ``fc``'s centre-search window: (value, centre_hz).

    The one implementation of "where is this feature's centre" — the gate
    ladder and the pose-persistence read call it alike, so the same feature
    can never grow two centres.
    """
    search = _centre_search_mask(freqs, fc)
    window = values[search]
    apex = int(np.argmin(window) if is_dip else np.argmax(window))
    return float(window[apex]), float(freqs[search][apex])


def _pose_reading(
    curve: RoundPoseCurve, detrended: np.ndarray, fc: float, is_dip: bool
) -> dict[str, Any]:
    """One pose curve's own depth/centre near ``fc``, or NOT-RESOLVED.

    Direct on the curve's own banked ``(freqs_hz, magnitude_db)`` -- never a
    raw WAV. Detrended with this module's own :func:`detrend`, the same
    one-octave baseline the primary detector uses, so a departure read here
    means what it means there.

    NOT-RESOLVED (``resolved=False``, both numbers ``None``) whenever this
    pose cannot answer the question at all: ``fc``'s own neighbourhood falls
    outside the curve's driven ``band_hz``, or too few of the grid's sparse
    points land inside the centre-search span to read an extremum from. Never
    a fabricated 0 dB -- absence is not zero.

    ``detrended`` is the curve's own detrended magnitude, computed once per
    curve by the caller — it is feature-independent.
    """
    lo = fc * 2**-NEIGHBOURHOOD_OCT
    hi = fc * 2**NEIGHBOURHOOD_OCT
    base: dict[str, Any] = {
        "pose_id": curve.pose_id,
        "position_deg": curve.position_deg,
        "role": curve.role,
        "resolved": False,
        "pooled_db": None,
        "centre_hz": None,
    }
    if not (curve.band_hz[0] <= lo and hi <= curve.band_hz[1]):
        return base
    search = _centre_search_mask(curve.freqs_hz, fc)
    if int(np.count_nonzero(search)) < _POSE_MIN_CENTRE_SAMPLES:
        return base
    pooled, centre = _extremum_reading(detrended, curve.freqs_hz, fc, is_dip)
    return {**base, "resolved": True, "pooled_db": pooled, "centre_hz": centre}


def _pose_bank_block(pose_curves: Sequence[RoundPoseCurve]) -> dict[str, Any]:
    """This round's lateral-pose bank, once -- what every row's
    ``pose_persistence`` table is read against.

    Mirrors :func:`_timing_scatter`'s NOT-RUN shape for the same reason: "no
    lateral poses banked" is a different fact from "every pose read as
    not-resolved", and a reader must not have to infer the first from a row
    full of the second.
    """
    if not pose_curves:
        return {
            "available": False,
            "n_poses": 0,
            "note": (
                "NOT RUN: this round banked no lateral-walk pose curves, so "
                "no feature's off-axis persistence can be read. This is an "
                "unmeasured dimension, not evidence a feature is on-axis only."
            ),
        }
    return {
        "available": True,
        "n_poses": len({curve.pose_id for curve in pose_curves}),
    }


# --------------------------------------------------------------------------- #
# narrow-band decay -- from the IRs this instrument already deconvolved
# --------------------------------------------------------------------------- #


def _decay_bands_hz(fc: float) -> dict[str, tuple[float, float]]:
    """The centre band and its two flanks, every one ``FEATURE_HALF_OCT`` wide.

    The centre band is the feature's own -- :func:`read_feature`'s band,
    restated so a magnitude read and a decay read agree on what "the
    feature's own band" means. Each flank is the SAME width, its own inner
    edge :data:`DECAY_FLANK_SKIRT_OFFSET_OCT` beyond the centre band's
    matching skirt, never narrower.
    """
    half = FEATURE_HALF_OCT
    far = 3 * half + DECAY_FLANK_SKIRT_OFFSET_OCT
    near = half + DECAY_FLANK_SKIRT_OFFSET_OCT
    return {
        "center": (fc * 2**-half, fc * 2**half),
        "flank_lo": (fc * 2**-far, fc * 2**-near),
        "flank_hi": (fc * 2**near, fc * 2**far),
    }


@dataclass(frozen=True)
class _DecayHost:
    """One IR's forward FFT, computed once — every band read shares it.

    The per-band work is only the mask and the inverse transform; the
    spectrum itself is feature-independent, and a round reads three bands
    per feature off the same host IR.
    """

    n: int
    sample_rate: int
    spectrum: np.ndarray
    freqs: np.ndarray

    @classmethod
    def of(cls, ir: np.ndarray, sample_rate: int) -> "_DecayHost":
        return cls(
            n=ir.size,
            sample_rate=sample_rate,
            spectrum=np.fft.rfft(ir),
            freqs=np.fft.rfftfreq(ir.size, d=1.0 / sample_rate),
        )


def _band_limited_envelope(
    host: _DecayHost, band_hz: tuple[float, float]
) -> np.ndarray:
    """The analytic envelope of the host IR restricted to ``band_hz``.

    An FFT-domain brick-wall mask, zero outside the band: the IR here is the
    module's own reflection-free deconvolution, already long and clean, so
    no taper is needed to keep the result well-behaved.
    :func:`~jasper.audio_measurement.gating.analytic_envelope` is REUSED, not
    duplicated — the same Hilbert-magnitude envelope the gating half of this
    package already reads a ring-down with.
    """
    mask = (host.freqs >= band_hz[0]) & (host.freqs <= band_hz[1])
    band_limited = np.fft.irfft(host.spectrum * mask, n=host.n)
    return analytic_envelope(band_limited)


def _decay_read(host: _DecayHost, band_hz: tuple[float, float]) -> dict[str, Any]:
    """Time-to-``DECAY_TARGET_DROP_DB`` in one band, or an honest non-answer.

    ``noise_floor_db`` is the envelope's own late-tail level, dB relative to
    THIS band's own peak, so it is directly comparable to
    :data:`DECAY_TARGET_DROP_DB`. ``below_floor`` is a property of that
    number alone — the target drop is not resolvable above the floor,
    independent of whether a search happens to cross it.
    ``time_to_neg20_db_ms`` is ``None`` whenever ``below_floor`` is true OR
    the envelope never reaches the target within the IR's own length —
    never a fabricated time.
    """
    envelope = _band_limited_envelope(host, band_hz)
    peak_idx = int(np.argmax(envelope))
    peak_level = float(envelope[peak_idx])
    tail_start = int(envelope.size * (1.0 - DECAY_NOISE_FLOOR_TAIL_FRACTION))
    tail = envelope[tail_start:]
    floor_level = float(np.median(tail)) if tail.size else 0.0
    noise_floor_db = 20.0 * math.log10(
        max(floor_level, 1e-300) / max(peak_level, 1e-300)
    )
    below_floor = noise_floor_db > -DECAY_TARGET_DROP_DB
    target_level = peak_level * 10 ** (-DECAY_TARGET_DROP_DB / 20.0)
    crossings = np.flatnonzero(envelope[peak_idx:] <= target_level)
    time_ms = (
        float(crossings[0] / host.sample_rate * 1000.0)
        if not below_floor and crossings.size
        else None
    )
    return {
        "band_hz": [float(band_hz[0]), float(band_hz[1])],
        "noise_floor_db": noise_floor_db,
        "below_floor": bool(below_floor),
        "time_to_neg20_db_ms": time_ms,
    }


# --------------------------------------------------------------------------- #
# frequency-dependent windowing -- diagnostic evidence only (ADR-0201)
# --------------------------------------------------------------------------- #

#: The two cycle counts research 03's Stage 3 names: a narrow one that best
#: rejects reflections and a wide one closer to the shipped gate ladder's own
#: primary length. ADR-0201 binds what this buys: re-analysis EVIDENCE only
#: -- no FDW output here may feed a target or a grade, and the diagnostic
#: reading rule (a dip that fills in under FDW but stays deep under the
#: fixed gate is reflection-caused) is guide content, not code.
FDW_CYCLES: tuple[float, ...] = (5.0, 15.0)

#: Half-width of the band an FDW rung is read over. Reused rather than
#: re-picked: :data:`NEIGHBOURHOOD_OCT` is already this module's standing
#: "local context" width, and both :func:`read_feature`'s and
#: :func:`_extremum_reading`'s search spans fit inside it -- so the exact
#: readers :func:`_gate_rungs` is built from serve the FDW curve unmodified,
#: rather than a second "how big is this feature" implementation.
FDW_BAND_OCT = NEIGHBOURHOOD_OCT

#: Points across that band, log-spaced. Hundreds, not the ~2000/octave main
#: analysis grid: FDW re-picks its window at EVERY point (unlike the main
#: grid's one gate shared by all of them), so each point is its own direct
#: frequency read rather than a shared FFT bin -- an offline, twice-per-
#: feature cost, not a global one.
FDW_GRID_POINTS = 300

#: Taper shape for the FDW window. Hann: it is already the family
#: :func:`gate`'s own tail uses, so this instrument carries one taper family
#: rather than introducing a second one for a diagnostic reading.
FDW_TAPER = "hann"


def _fdw_read_hz(
    ir: np.ndarray, sample_rate: int, peak: int, freq_hz: float, cycles: float
) -> float:
    """One frequency's FDW magnitude, dB, from a window of ``cycles`` cycles
    of ``freq_hz`` (``cycles / freq_hz`` seconds) CENTRED on ``peak`` -- the
    same direct-arrival sample :func:`gate` windows from.

    A direct single-bin read: the window differs at every frequency, so no
    one FFT serves the whole curve. :data:`FDW_TAPER`, then the exact-
    frequency DFT term, normalised by the taper's own coherent gain so a
    window-LENGTH change alone cannot move the level -- what survives that
    normalisation and the caller's own :func:`detrend` is the feature, not
    the window's shrinking span.
    """
    half = max(1, int(round(cycles / freq_hz * sample_rate / 2.0)))
    start = max(0, peak - half)
    end = min(ir.size, peak + half)
    segment = ir[start:end]
    if segment.size < 2:
        return float("-inf")
    taper = np.hanning(segment.size)
    coherent_gain = float(taper.sum())
    if coherent_gain <= 0:
        return float("-inf")
    n = np.arange(start, end, dtype=np.float64)
    phasor = np.exp(-2j * np.pi * freq_hz * n / sample_rate)
    amplitude = abs(np.sum(segment * taper * phasor)) / coherent_gain
    return 20.0 * math.log10(max(amplitude, 1e-12))


def _fdw_local_curve(
    ir: np.ndarray, sample_rate: int, peak: int, fc: float, cycles: float
) -> tuple[np.ndarray, np.ndarray]:
    """One capture's FDW-``cycles`` curve across ``fc``'s own local band.

    ``(grid, db)``, so the caller reuses :func:`detrend`, :func:`read_feature`
    and :func:`_extremum_reading` unmodified -- the same readers
    :func:`_gate_rungs` is built from, rather than a second "how big is this
    feature" for one more window shape.
    """
    grid = np.geomspace(fc * 2**-FDW_BAND_OCT, fc * 2**FDW_BAND_OCT, FDW_GRID_POINTS)
    db = np.array(
        [_fdw_read_hz(ir, sample_rate, peak, float(f), cycles) for f in grid]
    )
    return grid, db


def _fdw_rungs(
    irs: Sequence[np.ndarray],
    peaks: Sequence[int],
    sample_rate: int,
    fc: float,
    is_dip: bool,
) -> dict[str, Any]:
    """This feature's :data:`FDW_CYCLES` variants, pooled like a gate rung.

    Diagnostic only (ADR-0201) -- shape analogous to :func:`_gate_rungs`
    (``pooled_db`` / ``centre_hz`` per cycle count), pooled across the
    round's own captures the identical way the primary curve is. Never a
    target, never a grade: nothing downstream reads this key for either.
    """
    out: dict[str, Any] = {}
    for cycles in FDW_CYCLES:
        pooled_values: list[float] = []
        centre_values: list[float] = []
        for ir, peak in zip(irs, peaks):
            grid, db = _fdw_local_curve(ir, sample_rate, peak, fc, cycles)
            det = detrend(db, grid)
            pooled_values.append(read_feature(det, grid, fc))
            _, centre = _extremum_reading(det, grid, fc, is_dip)
            centre_values.append(centre)
        out[f"{cycles:.0f}"] = {
            "pooled_db": float(np.mean(pooled_values)),
            "centre_hz": float(np.mean(centre_values)),
        }
    return out


def _compose(
    fc: float,
    egd: Mapping[str, Any],
    cell: Mapping[str, Any],
    control_pair: Mapping[str, Any],
    pooled_db: float,
    measured_q: float,
    *,
    controls_ok: bool,
    timing_available: bool,
    primary_key: str,
    gates_ms: Sequence[float],
) -> dict[str, Any]:
    """One feature's row: the two component verdicts, the composed one, and why."""
    excursion = egd["pooled_excursion_us"]
    nmp_scale = abs(control_pair["nmp_delta_us"])
    frac = abs(excursion) / nmp_scale if nmp_scale else float("nan")
    z_local = abs(excursion) / egd["nbhd_sd_us"] if egd["nbhd_sd_us"] else float("nan")

    if frac >= FRAC_NMP_NON_MIN_PHASE:
        raw_egd = EGD_NON_MIN_PHASE
    elif frac < FRAC_NMP_MIN_PHASE and z_local < Z_LOCAL_FLAT:
        raw_egd = EGD_MIN_PHASE
    else:
        raw_egd = EGD_AMBIGUOUS
    # An instrument that just failed its own known-answer check does not get to
    # assert a phase class. `egd_verdict_raw` keeps what the numbers said.
    egd_verdict = raw_egd if controls_ok else EGD_AMBIGUOUS

    moved = False
    tension = False
    notes: list[str] = []
    excess_loss: dict[str, float] = {}
    slack_by_gate: dict[str, float] = {}
    for gate_ms in gates_ms:
        gate_key = f"{gate_ms:.0f}"
        if gate_key == primary_key:
            continue
        entry = cell[gate_key]
        n = len(entry["db_values"])
        standard_error = entry["retention_per_capture_sd"] / math.sqrt(n)
        slack = max(RETENTION_SLACK, 3.0 * standard_error)
        loss = entry["excess_loss_vs_null"]
        excess_loss[gate_key] = loss
        slack_by_gate[gate_key] = slack
        if loss < -slack:
            moved = True
        elif loss < -0.5 * slack:
            tension = True
        if abs(entry["centre_shift_oct"]) > CENTRE_SHIFT_OCT and entry["resolved"]:
            moved = True
        if not entry["resolved"]:
            notes.append(
                f"{gate_key} ms unresolved ({entry['resolution_hz']:.0f} Hz vs "
                f"{entry['feature_width_hz']:.0f} Hz feature): depth is still "
                "comparable against the matched-Q null model, centre is not"
            )
    gate_verdict = GATE_MOVED if moved else GATE_STABLE
    resolved_gates = sum(1 for g in gates_ms if cell[f"{g:.0f}"]["resolved"])

    is_dip = pooled_db < 0
    if egd_verdict == EGD_NON_MIN_PHASE:
        classification = INTERFERENCE_BARRED
    elif gate_verdict == GATE_MOVED:
        classification = ROOM
    elif egd_verdict == EGD_MIN_PHASE:
        classification = DEFECT_BOOSTABLE if is_dip else DEFECT_CUTTABLE
    else:
        classification = UNRESOLVED

    # Confidence is deliberately NOT built on sign agreement across captures:
    # an excursion of essentially zero — the strongest possible minimum-phase
    # evidence — has random sign by construction, so a sign rule would punish
    # the cleanest results. Decisiveness is measured against the control scale.
    agree = gate_verdict == GATE_STABLE and egd_verdict in (
        EGD_MIN_PHASE,
        EGD_NON_MIN_PHASE,
    )
    decisive = (egd_verdict == EGD_MIN_PHASE and frac < 0.10 and z_local < 2.0) or (
        egd_verdict == EGD_NON_MIN_PHASE and frac > 0.75
    )
    # The three words are quality_model's shared TrustLevel — `medium` spelled
    # in full. This instrument wrote `med` until 2026-08-22, alone against
    # every sibling that answers "how much do I trust this number?"; a banked
    # artifact from before then still carries `med` and is normalised on the
    # way back in by feature_classification.read_feature_verdicts.
    confidence: TrustLevel
    if classification == UNRESOLVED:
        confidence = "low"
    elif tension:
        confidence = "medium"
    elif agree and decisive and resolved_gates >= 2 and timing_available:
        # A `high` reading is not earned while a corroborating test never ran.
        confidence = "high"
    elif agree:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "hz": fc,
        "classification": classification,
        "egd_verdict": egd_verdict,
        "egd_verdict_raw": raw_egd,
        "gate_verdict": gate_verdict,
        "confidence": confidence,
        "measured_q": measured_q,
        "depth_db": abs(pooled_db),
        "pooled_db": pooled_db,
        "is_dip": is_dip,
        "excursion_us": excursion,
        "excursion_sd_us": egd["sd_us"],
        "nbhd_sd_us": egd["nbhd_sd_us"],
        "p2p_us": egd["p2p_us"],
        "nmp_scale_us": nmp_scale,
        "frac_of_nmp": frac,
        "z_local": z_local,
        "lead_sensitivity_us": egd["lead_sensitivity_us"],
        "clean": egd["clean"],
        "resolved_gates": resolved_gates,
        "excess_loss_vs_null": excess_loss,
        "gate_slack": slack_by_gate,
        "centre_shift_oct": {
            f"{g:.0f}": cell[f"{g:.0f}"]["centre_shift_oct"]
            for g in gates_ms
            if f"{g:.0f}" != primary_key
        },
        "gate_notes": notes,
        "controls_ok": controls_ok,
        "timing_corroborated": timing_available,
    }


def _gate_rungs(cell: Mapping[str, Any]) -> dict[str, Any]:
    """Every commanded gate's own depth/centre, additive to the composed row.

    ``_compose`` only carries what a rung MOVED against, so it excludes the
    primary key -- there is nothing to compare the primary to. A reader
    auditing the ladder itself, rather than the STABLE/MOVED call, needs
    every rung including the primary, which is what this publishes.
    """
    return {
        gate_key: {
            "pooled_db": entry["db_mean"],
            "centre_hz": entry["centre_hz_mean"],
            "resolved": entry["resolved"],
        }
        for gate_key, entry in cell.items()
    }


def classify_round(
    captures: Sequence[RoundCapture],
    *,
    at: Sequence[float] | None = None,
    gate_ms: float = DEFAULT_GATE_MS,
    gates_ms: Sequence[float] = GATE_LADDER_MS,
    pose_curves: Sequence[RoundPoseCurve] = (),
) -> dict[str, Any]:
    """Classify one round's features and return the artifact to bank.

    ``at`` pins the frequencies to classify; omitted, they are detected from
    the round's own pooled response (:func:`_detect_features`).

    ``pose_curves`` is this round's banked lateral-walk curves
    (:func:`load_round_pose_curves`), optional and orthogonal to ``captures``:
    a caller with none still gets every EGD/gate/timing fact, plus a
    ``pose_bank``/``pose_persistence`` NOT-RUN pair rather than a refusal.

    Raises :class:`FeatureClassificationRefused` — with
    :data:`CONTROLS_FAILED` when the known-answer controls did not pass, and
    with :data:`NO_FEATURES_DETECTED` when nothing stood above the round's own
    scatter. Neither case returns a partial artifact.
    """
    if not captures:
        raise FeatureClassificationRefused(NO_ADMISSIBLE_CAPTURES, {"n_captures": 0})
    ladder = tuple(sorted({float(g) for g in gates_ms} | {float(gate_ms)}))
    primary = float(gate_ms)
    primary_key = f"{primary:.0f}"
    trusted_band_hz = (f_trusted_floor_hz(primary * 1e-3), TRUSTED_CEILING_HZ)
    grid = analysis_grid()

    irs: list[np.ndarray] = []
    peaks: list[int] = []
    sample_rate: int | None = None
    for capture in captures:
        signal, rate = _read_wav(capture.wav)
        program, program_rate = _read_wav(capture.program)
        if rate != program_rate:
            raise ValueError(
                f"{capture.wav.name}: capture is {rate} Hz but its program is "
                f"{program_rate} Hz"
            )
        if sample_rate is None:
            sample_rate = rate
        elif rate != sample_rate:
            raise ValueError(
                f"{capture.wav.name}: {rate} Hz among {sample_rate} Hz captures"
            )
        # Unwindowed on purpose: the timing test needs a t=0 defined by the
        # program that was played, not by a per-capture argmax that has already
        # absorbed the offset being measured.
        ir = regularized_deconvolution_full(
            signal.astype(np.float32), program.astype(np.float32), rate
        ).astype(np.float64)
        irs.append(ir)
        peaks.append(int(np.argmax(np.abs(ir))))
    assert sample_rate is not None

    curves = np.array(
        [
            smoothed_curve(
                gate(ir, sample_rate, gate_ms=primary, peak=peak), sample_rate, grid
            )
            for ir, peak in zip(irs, peaks)
        ]
    )
    detrended = np.array([detrend(curve, grid) for curve in curves])
    band_hz = classifiable_band_hz(trusted_band_hz)
    if at is None:
        features = _detect_features(detrended, curves.mean(axis=0), grid, band_hz)
    else:
        features = sorted(
            float(fc) for fc in at if band_hz[0] <= float(fc) <= band_hz[1]
        )
    if not features:
        raise FeatureClassificationRefused(
            NO_FEATURES_DETECTED,
            {
                "n_captures": len(captures),
                "classifiable_band_hz": list(band_hz),
                "trusted_band_hz": list(trusted_band_hz),
                "stability_z": FEATURE_STABILITY_Z,
                "min_departure_db": FEATURE_MIN_DEPARTURE_DB,
                "requested": list(at) if at is not None else None,
            },
        )

    # The controls' host: the round's earliest capture, so the same IR carries
    # every known answer and each inherits this round's own noise floor.
    host_index = 0
    controls = _run_controls(
        irs[host_index],
        sample_rate,
        features,
        gate_ms=primary,
        trusted_band_hz=trusted_band_hz,
    )
    controls_ok = bool(controls["verdict"]["passes"])
    if not controls_ok:
        raise FeatureClassificationRefused(
            CONTROLS_FAILED,
            {
                "verdict": controls["verdict"],
                "host_capture": captures[host_index].wav.name,
                "note": (
                    "no verdict is reportable from an instrument that failed "
                    "its own known-answer check on this round's capture"
                ),
            },
        )

    # TEST 1. Two transforms per CAPTURE — the phase gate's lead, and the
    # zero-lead window whose disagreement is reported as `lead_sensitivity_us`
    # — and every feature's excursion read off each. Per (capture, feature)
    # would be the same numbers at N times the cost.
    lead_by_feature: dict[str, list[dict[str, Any]]] = {
        f"{fc:.0f}": [] for fc in features
    }
    zero_by_feature: dict[str, list[float]] = {f"{fc:.0f}": [] for fc in features}
    for ir, peak in zip(irs, peaks):
        lead_ep = excess_group_delay(
            gate(
                ir, sample_rate, gate_ms=primary, lead_ms=PHASE_GATE_LEAD_MS, peak=peak
            ),
            sample_rate,
            trusted_band_hz=trusted_band_hz,
        )
        zero_ep = excess_group_delay(
            gate(ir, sample_rate, gate_ms=primary, peak=peak),
            sample_rate,
            trusted_band_hz=trusted_band_hz,
        )
        for fc in features:
            key = f"{fc:.0f}"
            lead_by_feature[key].append(egd_excursion(lead_ep, fc, trusted_band_hz))
            zero_by_feature[key].append(
                egd_excursion(zero_ep, fc, trusted_band_hz)["excursion_us"]
            )

    egd_rows: dict[str, dict[str, Any]] = {}
    for fc in features:
        key = f"{fc:.0f}"
        reads = lead_by_feature[key]
        values = np.array([r["excursion_us"] for r in reads])
        pooled = float(values.mean())
        zero_pooled = float(np.mean(zero_by_feature[key]))
        egd_rows[key] = {
            "pooled_excursion_us": pooled,
            "sd_us": float(values.std(ddof=1)) if values.size > 1 else 0.0,
            "p2p_us": float(np.mean([r["p2p_us"] for r in reads])),
            "nbhd_sd_us": float(np.mean([r["nbhd_sd_us"] for r in reads])),
            "zero_lead_pooled_us": zero_pooled,
            "lead_sensitivity_us": pooled - zero_pooled,
            "clean": all(bool(r["clean"]) for r in reads),
            "n": int(values.size),
        }

    gate_table, null_model, pooled_db = _gate_invariance(
        irs, peaks, irs[host_index], peaks[host_index], sample_rate, features, grid,
        ladder, primary_ms=primary,
    )
    timing = _timing_scatter(captures, irs, peaks, sample_rate, trusted_band_hz)

    # Feature-independent work, once per round: each pose curve's detrended
    # form, and the host IR's forward FFT — the rows loop only reads them.
    pose_detrended = [
        detrend(curve.magnitude_db, curve.freqs_hz) for curve in pose_curves
    ]
    decay_host = _DecayHost.of(irs[host_index], sample_rate)

    rows = []
    for fc in features:
        key = f"{fc:.0f}"
        row = _compose(
            fc,
            egd_rows[key],
            gate_table[key],
            controls["C4_pair"]["at"][key],
            pooled_db[key],
            null_model[key]["matched"]["q"],
            controls_ok=controls_ok,
            timing_available=bool(timing["available"]),
            primary_key=primary_key,
            gates_ms=ladder,
        )
        row["gate_rungs"] = _gate_rungs(gate_table[key])
        row["pose_persistence"] = [
            _pose_reading(curve, detrended, fc, pooled_db[key] < 0)
            for curve, detrended in zip(pose_curves, pose_detrended)
        ]
        row["decay"] = {
            name: _decay_read(decay_host, band)
            for name, band in _decay_bands_hz(fc).items()
        }
        row["fdw_rungs"] = _fdw_rungs(
            irs, peaks, sample_rate, fc, pooled_db[key] < 0
        )
        # 6.11a: how many cycles of THIS feature's own frequency fit inside
        # the primary gate -- the grey-zone read research 03 names (2.5/T is
        # prudence, not a law; a feature at 2-3 cycles is where a magnitude
        # estimate is taper-biased and low-resolution). A row field, not a
        # threshold: nothing here refuses or grades on it.
        row["cycles_in_primary_gate"] = fc * primary * 1e-3
        rows.append(row)

    return {
        "schema": CLASSIFICATION_SCHEMA_VERSION,
        "generated_by": GENERATED_BY,
        "thresholds": {
            "frac_nmp_min_phase": FRAC_NMP_MIN_PHASE,
            "frac_nmp_non_min_phase": FRAC_NMP_NON_MIN_PHASE,
            "z_local_flat": Z_LOCAL_FLAT,
            "retention_slack": RETENTION_SLACK,
            "centre_shift_oct": CENTRE_SHIFT_OCT,
            "feature_stability_z": FEATURE_STABILITY_Z,
            "feature_min_departure_db": FEATURE_MIN_DEPARTURE_DB,
            "feature_min_separation_oct": FEATURE_MIN_SEPARATION_OCT,
            "max_features": MAX_FEATURES,
            "decay_target_drop_db": DECAY_TARGET_DROP_DB,
        },
        "measurement": {
            "sample_rate": sample_rate,
            "gate_ms_primary": primary,
            "gate_ladder_ms": list(ladder),
            "phase_gate_lead_ms": PHASE_GATE_LEAD_MS,
            "egd_window_source": {
                "kind": EGD_WINDOW_KIND,
                "gate_ms": primary,
                "lead_ms": PHASE_GATE_LEAD_MS,
            },
            "trusted_band_hz": list(trusted_band_hz),
            "classifiable_band_hz": list(band_hz),
            "magnitude_smooth_fraction": MAGNITUDE_SMOOTH_FRACTION,
            "complex_smooth_oct": COMPLEX_SMOOTH_OCT,
            "fdw_cycles": list(FDW_CYCLES),
            "fdw_taper": FDW_TAPER,
            "n_captures": len(captures),
            "phases": sorted({c.phase for c in captures}),
            "captures": [c.wav.name for c in captures],
            "features_requested": list(at) if at is not None else None,
        },
        "controls_ok": controls_ok,
        "controls": controls,
        "gate_invariance_null_model": null_model,
        "timing_scatter": timing,
        "pose_bank": _pose_bank_block(pose_curves),
        "rows": rows,
    }
