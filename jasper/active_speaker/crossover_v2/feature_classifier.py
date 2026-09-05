# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What KIND of feature is that — measured, from a round's own banked captures.

The INSTRUMENT behind :mod:`.feature_classification`'s register: that module
owns the verdict names, the row schema and what ``depth_db`` means, this one
runs the tests that fill them in over captures a round already banked. It
imports every verdict string and never spells one.

**Three tests and a gate.** EXCESS GROUP DELAY — minimum-phase (a driver
defect, so a filter is at least the right kind of tool) or non-minimum-phase
(a cancellation, where a filter lowers the direct sound and its delayed copy
together), read as a fraction of what a genuine same-frequency cancellation
produces through this exact pipeline, which the C4 control measures. GATE
INVARIANCE — the window ladder, run by :mod:`.gate_sweep`; this module owns
no second one. TIMING SCATTER — the sub-sample arrival residual between
captures at the same angle, which needs a repeated angle to run and never
reads its own absence as evidence of tight timing.

**The controls certify the PHASE test, and only it.** Four known answers are
pushed through the identical pipeline on the round's own measured IR, and
none of them touches the magnitude ladder. A run whose controls fail still
writes its artifact and still publishes a real ``gate_verdict``; what it
loses is the phase class, which every row reports as ``ambiguous`` beside
``controls_ok: false`` and the raw reading in ``egd_verdict_raw``.
``defect-*`` is unreachable there — it requires a MIN-PHASE egd verdict — so
no filter can be vouched for by an instrument that failed its own
known-answer check.

**Only the summed post-apply capture shapes are admissible.** A ``lateral``
capture replays the per-driver MEASURE program one driver at a time, so it
cannot answer a question about the summed system's features and is a HARD
refusal by name (:data:`LATERAL_CAPTURE_SHAPE`) rather than a silent skip.
The pre-apply half of ``programs.SUMMED_SWEEP_PHASES``
(``entry_baseline`` / ``cloud_measure``) is not admitted either: it measures
a DIFFERENT graph, and pooling the two would classify a mixture.

It lives here rather than under :mod:`jasper.audio_measurement` because
``tests/test_correction_boundary_ssot.py``'s
``test_package_boundary_holds`` forbids that
package importing ``jasper.active_speaker``, which this module does for the
verdict register and the phase names. **If you are about to move this
module, read that test first.**
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
from jasper.audio_measurement.bundles import sha256_file
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
from .feature_optics import (
    CENTRE_SEARCH_OCT,
    FEATURE_HALF_OCT,
    MAGNITUDE_SMOOTH_FRACTION,
    NEIGHBOURHOOD_OCT,
    PHASE_GATE_LEAD_MS,
    biquad_peaking,
    detrend,
    feature_q,
    read_feature,
)
from .gate_sweep import (
    CENTRE_SHIFT_OCT,
    DEFAULT_RUNGS_MS,
    GATE_DELTA_SLACK_DB,
    SIGMA_GROWTH_MIN_SIGMA_DB,
    SIGMA_GROWTH_ROOM_RATIO,
    WINDOW_MOVED,
    WINDOW_STABLE,
    analysis_grid,
    frame_descriptor,
    sweep_features,
)
from .journey import PHASE_CLOUD_VERIFY, PHASE_LATERAL, PHASE_VERIFY
from .round_captures import (
    REFUSE_RADIATED_BAND_MISSING,
    PoseCapture,
    RoundCapturesRefused,
    radiated_band_of,
)

__all__ = [
    "ADMISSIBLE_PHASES",
    "CAPTURE_ADMISSIBILITY_REASONS",
    "CAPTURES_UNREADABLE",
    "classifiable_band_hz",
    "CLASSIFICATION_REFUSAL_REASONS",
    "CLASSIFICATION_SCHEMA_VERSION",
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
    "summary_lines",
]


#: A ``lateral`` capture was bound to this round. See the module docstring.
LATERAL_CAPTURE_SHAPE = "classification_lateral_capture_shape"

#: This round's captures were found, and every one of them names a phase this
#: instrument cannot read — a MEASURE-only round is the common case. The
#: remedy is a different ROUND: point at a verify-shaped one.
ROUND_SHAPE_INADMISSIBLE = "classification_round_shape_inadmissible"

#: No capture of this round reached the instrument at all — an empty ring,
#: one holding only other sessions' captures, or sidecars that will not
#: parse. The remedy is the RING or the bundle it was scoped to; no round
#: shape would satisfy this one. Narrowed from the slug that used to cover
#: :data:`ROUND_SHAPE_INADMISSIBLE` too (#3480): the two refuse from
#: different code paths and have different remedies.
NO_ADMISSIBLE_CAPTURES = "classification_no_admissible_captures"

#: This round banked a capture of an admissible shape and the ring cannot
#: hand it over: the WAV is not beside its sidecar, or the dump name lost
#: the stamp the take's timing is bound to. The round shape is RIGHT — the
#: remedy is the ring or the bank step that filled it (#3480).
CAPTURES_UNREADABLE = "classification_captures_unreadable"

#: No program in the round directory carries the bytes a capture's sidecar
#: banked, so it cannot be deconvolved against the signal that was actually
#: played. Matching is by content hash, never by phase label (#3504).
PROGRAM_MISSING = "classification_program_missing"

#: Nothing in the pooled response stood above the round's own capture-to-
#: capture scatter. Refused rather than reported as an empty verdict set: an
#: artifact with no rows and a broken detector look identical to a reader.
NO_FEATURES_DETECTED = "classification_no_features_detected"

CLASSIFICATION_REFUSAL_REASONS = frozenset({
    LATERAL_CAPTURE_SHAPE,
    ROUND_SHAPE_INADMISSIBLE,
    NO_ADMISSIBLE_CAPTURES,
    CAPTURES_UNREADABLE,
    PROGRAM_MISSING,
    NO_FEATURES_DETECTED,
})


#: A ladder of one rung. The window verdict compares the shortest and longest
#: resolution-valid rung, so one rung compares with nothing. Deliberately
#: not a :data:`CLASSIFICATION_REFUSAL_REASONS` member: it costs the window
#: verdict and nothing else.
GATE_LADDER_NEEDS_TWO_RUNGS = "gate_ladder_needs_two_rungs"

#: The engine declined this ladder or a bin on it for a reason of its own
#: (:exc:`ValueError`), carried through rather than raised: the same trade as
#: above, and for the same reason.
GATE_LADDER_UNUSABLE = "gate_ladder_unusable"


#: The capture is classifiable: an admissible shape, this round's session, the
#: program whose bytes it banked present, and a readable WAV under a stamped
#: name.
CAPTURE_ADMISSIBLE = "admissible"

#: The sidecar JSON did not read as an object carrying a ``phase`` string.
CAPTURE_UNREADABLE_SIDECAR = "unreadable_sidecar"

#: The capture is stamped with a different bundle ``session_id``.
CAPTURE_OTHER_SESSION = "other_session"

#: The capture's phase is not in :data:`ADMISSIBLE_PHASES`.
CAPTURE_PHASE_NOT_ADMISSIBLE = "phase_not_admissible"

#: A ``lateral`` capture — the per-driver shape that refuses the whole round.
CAPTURE_LATERAL_SHAPE = "lateral_capture_shape"

#: No program in the round directory carries the bytes this capture's sidecar
#: banked as ``provenance.stimulus.wav_sha256``.
CAPTURE_PROGRAM_MISSING = "program_missing"

#: The sidecar banks no stimulus content hash, so no program can be proven to
#: be the one it heard.
CAPTURE_PROGRAM_UNIDENTIFIED = "program_unidentified"

#: The sidecar's WAV is not beside it in the ring.
CAPTURE_WAV_MISSING = "wav_missing"

#: The dump filename does not open with the microsecond stamp every capture's
#: timing residual is bound to.
CAPTURE_UNSTAMPED_NAME = "unstamped_name"

#: Which refusal each admissibility reason speaks for when a round ends with
#: no classifiable capture — the partition of the closed capture vocabulary
#: onto the refusal vocabulary, as data. A reason cannot be added to the
#: vocabulary below without being placed here, because the vocabulary IS
#: this table's keys.
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
    CAPTURE_PROGRAM_UNIDENTIFIED: CAPTURES_UNREADABLE,
    CAPTURE_WAV_MISSING: CAPTURES_UNREADABLE,
    CAPTURE_UNSTAMPED_NAME: CAPTURES_UNREADABLE,
}

#: Read in this order, so the most specific thing the census can say is what
#: the refusal says: a round whose admissible take the ring lost IS
#: verify-shaped, and naming its shape as the remedy is the #3480
#: misdirection. A census speaking for neither is
#: :data:`NO_ADMISSIBLE_CAPTURES`.
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


#: The capture shapes whose response IS the summed system, post-apply. The
#: pre-apply members of ``programs.SUMMED_SWEEP_PHASES`` are deliberately not
#: here — see the module docstring.
ADMISSIBLE_PHASES = frozenset({PHASE_VERIFY, PHASE_CLOUD_VERIFY})

#: The artifact's own version. Deliberately not a new number: the row shape is
#: the one the register already reads and the 2026-08-19 records already carry.
CLASSIFICATION_SCHEMA_VERSION = 1

GENERATED_BY = "jasper.active_speaker.crossover_v2.feature_classifier"


#: The primary analysis window. ``gating.SEARCH_T_MAX_MS`` is the product's own
#: reflection search ceiling AND the window it falls back to when no reflection
#: is found, so a fixed window of that length is the longest one the product
#: ever calls reflection-free. Overridable per run.
DEFAULT_GATE_MS = SEARCH_T_MAX_MS

#: What :func:`excess_group_delay` actually windows with. Research 03's
#: pitfall is real — a SHORT gate biases the Hilbert min-phase
#: reconstruction — but the window this instrument reads EGD through is
#: ``DEFAULT_GATE_MS`` (``== gating.SEARCH_T_MAX_MS``), the longest window
#: the product ever calls reflection-free, so there is no longer clean
#: window to move to. It is also the exact window the C1/C3 controls are
#: calibrated on, on THIS round's own IR.
EGD_WINDOW_KIND = "fixed_reflection_free_gate"

#: Complex smoothing for the phase chain, as a fraction of an octave. Finer
#: than the magnitude chain because excess phase is the quantity under test.
#: Smoothing magnitude and phase separately would break the pairing the whole
#: test depends on, which is why this is applied to the complex spectrum.
COMPLEX_SMOOTH_OCT = 1.0 / 24.0

#: Half-width of the span the group-delay slope is fitted over. The feature's
#: own half-width it is paired with is :data:`.feature_optics.FEATURE_HALF_OCT`.
GD_SPAN_OCT = 1.0 / 24.0

#: Band the smoothed curve is normalised on, and the trusted ceiling. The
#: trusted FLOOR is derived per run from the gate length
#: (``gating.f_trusted_floor_hz``), never spelled.
NORMALISE_BAND_HZ = (400.0, 8000.0)
TRUSTED_CEILING_HZ = 16000.0

#: Analysis grid: logarithmic, and dense enough that a 1/12-octave feature has
#: hundreds of samples across it at every frequency.
CLASSIFICATION_GRID_LO_HZ = 300.0
CLASSIFICATION_GRID_HI_HZ = 16000.0
CLASSIFICATION_GRID_POINTS = 12000

#: Zero-padded transform length for the phase chain. Interpolation, not
#: resolution: it gives the local slope fit samples to work with.
PHASE_NFFT = 1 << 16

#: Deep nulls clamp here, relative to the in-band maximum, before the cepstral
#: transform. Un-clamped, a single near-zero bin dominates the whole Hilbert
#: relation.
LOGMAG_FLOOR_DB = 60.0

#: Out-of-band log-magnitude is held at the nearest in-band value across this
#: crossfade. The cepstral transform is a Hilbert transform over the WHOLE
#: spectrum, so leaving the gate's low-frequency floor and the
#: deconvolution regulariser's roll-off raw injects a large fake
#: minimum-phase slope through the features. That holds for a capture, not
#: for magnitude a synthetic control puts down there deliberately — see
#: :func:`injection_excess_gd` and issue #3493.
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
#: :data:`~.feature_classification.VERDICT_MATCH_TOLERANCE_OCTAVES`: the
#: 2026-08-19 record has peak/dip pairs 0.143 and 0.157 octaves apart,
#: inside that tolerance, and merging them would delete four genuine
#: features.
FEATURE_MIN_SEPARATION_OCT = FEATURE_HALF_OCT

#: Half-width of the direct-sound window the timing residual is measured over.
#: Wide enough to hold the arrival's own main lobe, narrow enough to exclude
#: the first reflection at any usable gate.
DIRECT_SOUND_HALF_WINDOW_MS = 1.0

#: Bounded work per round. The 2026-08-19 record found nine; a response that
#: offers more than this is reporting texture rather than features. Each
#: feature costs two full excess-group-delay runs in the C4 control pair,
#: so this is the instrument's CPU bound as well as its honesty bound.
MAX_FEATURES = 12


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

# The room/speaker thresholds are :mod:`.gate_sweep`'s — the engine that runs
# the ladder owns what counts as the window having moved a feature, and this
# module maps its three-word verdict into the register (:func:`_gate_call`).


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

#: What a failed control suite costs, published at the top of the artifact.
#: All four controls calibrate the EGD scale, so a failure disables the PHASE
#: discriminator and leaves the magnitude ladder — whose verdict is the gate
#: sweep's, calibrated by its own null model — untouched.
CONTROLS_FAILED_DISCLOSURE = (
    "the known-answer controls did not pass on this round's own capture, so "
    "every row's egd_verdict is withheld as ambiguous (the raw reading is kept "
    "in egd_verdict_raw) and no row can classify as a defect; gate_verdict and "
    "every magnitude fact are unaffected, the controls calibrate the "
    "excess-group-delay scale alone"
)

#: A genuine cancellation must separate from its minimum-phase twin by this
#: multiple of the worst false positive above. Without it the two verdicts
#: would be a coin toss dressed as a threshold.
CONTROL_SEPARATION_MARGIN = 5.0


#: The drop time-to-decay is measured against. Named once so the field
#: (``time_to_neg20_db_ms``) and the reachability test (``below_floor``) read
#: the same number back rather than one of them drifting from the other.
DECAY_TARGET_DROP_DB = 20.0

#: How far beyond a feature's own +/-``FEATURE_HALF_OCT`` skirts a flanking
#: decay band's matching skirt sits. One third-octave: the flanking reads
#: show whether extended ringing is peculiar to the resonance or is the
#: room's tail nearby — the campaign's ~48 ms on the 898 Hz ridge against
#: ~6-10 ms a third-octave outside it. Each flank is the SAME width as the
#: centre band.
DECAY_FLANK_SKIRT_OFFSET_OCT = 1.0 / 3.0

#: Fraction of a band-limited envelope's own tail read for its noise floor.
#: The IRs this reads are the full deconvolved capture, not a gated
#: fragment, so any decay this instrument could report has long since
#: finished by the last fifth of it.
DECAY_NOISE_FLOOR_TAIL_FRACTION = 0.2


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
    #: The band this capture's DUT actually radiates, off its own sidecar
    #: curves. ``None`` when the sidecar banks none, which refuses the window
    #: LADDER for the round rather than substituting a declared band: the
    #: un-intersected band priced a tweeter from 357 Hz where it has no output
    #: and over-reported by 3x (E5, #1969).
    radiated_band_hz: tuple[float, float] | None = None


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

    ``round_dir`` is the round's own artifact directory, where the
    ``<phase>_program.wav`` files live and where the verdict is filed.
    ``dumps_dir`` is the banked capture ring's root: sidecar JSON beside its
    WAV, found by :data:`~.evidence_packet.RING_SIDECAR_GLOB` —
    :func:`~.harmonic_evidence.read_round_harmonics` reads the same ring
    through the same constant, so ``--dumps`` cannot come to mean two
    different directories.

    ``session_id`` scopes the ring to this round. It is the BUNDLE session id
    (``info.json``'s), which is what a sidecar stamps into
    ``jts_session_identity``; the capture id that names ``round_dir`` is a
    different namespace. Omitting it admits every capture in the ring, which
    is correct only when the ring holds one round. ``walk_logs`` are optional:
    without them captures carry no angle and the timing test says it did not
    run.

    Raises :class:`FeatureClassificationRefused` — never returns empty, "no
    captures" being a finding a caller must be told by name. EVERY refusal
    carries ``captures``: one row per sidecar the ring listed, the reason drawn
    from :data:`CAPTURE_ADMISSIBILITY_REASONS`, and which name a no-capture
    round refuses under is read off those rows through
    :data:`_REFUSAL_FOR_CAPTURE_REASON`.
    """
    # The listing and the binding map are separate because a round directory
    # really does hold byte-identical programs (``_play`` banks the first
    # summed-sweep phase's bytes again as ``summed_program.wav``) that collapse
    # into ONE hash key, so a refusal naming the map's values would omit files
    # that are on disk.
    banked_programs = sorted(round_dir.glob("*_program.wav"))
    programs = {
        sha256_file(path): path
        for path in banked_programs
    }
    releases = _walk_releases(walk_logs)

    captures: list[RoundCapture] = []
    seen_phases: dict[str, int] = {}
    lateral: list[str] = []
    missing_program: list[str] = []
    census: list[dict[str, Any]] = []

    def note(
        sidecar: Path, phase: Any, reason: str, stimulus_sha: str | None = None,
    ) -> None:
        """One row of the per-capture admissibility table.

        Every drop below records why, so a refusal names the captures the ring
        listing just handed the caller instead of only counting them (#3480), and
        the banked stimulus digest rides along so a program-missing row says what
        it matched against.
        """
        census.append({
            "sidecar": sidecar.name,
            "phase": phase if isinstance(phase, str) else None,
            "admissible": reason == CAPTURE_ADMISSIBLE,
            "reason": reason,
            "stimulus_wav_sha256_12": stimulus_sha[:12] if stimulus_sha else None,
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
        # Which program played is the bytes' hash the capture banked, never
        # the phase label: a cloud position's stimulus is labelled "verify"
        # whatever it actually emitted (#3504).
        provenance = doc.get("provenance")
        stimulus = (
            provenance.get("stimulus") if isinstance(provenance, Mapping) else None
        )
        banked = stimulus.get("wav_sha256") if isinstance(stimulus, Mapping) else None
        banked = banked if isinstance(banked, str) and banked else None
        identity = doc.get("jts_session_identity")
        banked_session = (
            identity.get("session_id") if isinstance(identity, Mapping) else None
        )
        if session_id is not None and banked_session != session_id:
            note(sidecar, phase, CAPTURE_OTHER_SESSION, banked)
            continue
        seen_phases[phase] = seen_phases.get(phase, 0) + 1
        if phase == PHASE_LATERAL:
            lateral.append(sidecar.name)
            note(sidecar, phase, CAPTURE_LATERAL_SHAPE, banked)
            continue
        if phase not in ADMISSIBLE_PHASES:
            note(sidecar, phase, CAPTURE_PHASE_NOT_ADMISSIBLE, banked)
            continue
        if banked is None:
            note(sidecar, phase, CAPTURE_PROGRAM_UNIDENTIFIED)
            continue
        program = programs.get(banked)
        if program is None:
            missing_program.append(phase)
            note(sidecar, phase, CAPTURE_PROGRAM_MISSING, banked)
            continue
        wav = sidecar.parent.parent / "wav" / f"{sidecar.stem}.wav"
        if not wav.is_file():
            note(sidecar, phase, CAPTURE_WAV_MISSING, banked)
            continue
        # The dump filename opens with its own microsecond stamp.
        try:
            stamp = float(sidecar.stem.split("_")[0]) / 1e6
        except ValueError:
            note(sidecar, phase, CAPTURE_UNSTAMPED_NAME, banked)
            continue
        note(sidecar, phase, CAPTURE_ADMISSIBLE, banked)
        captures.append(
            RoundCapture(
                wav=wav,
                program=program,
                phase=phase,
                stamp=stamp,
                degrees=_bind_angle(stamp, releases),
                # One owner for the E5 rule, and one parse of it: the sibling
                # round loader the gate sweep reads through owns this.
                radiated_band_hz=radiated_band_of(doc),
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
        # Refused rather than pooled without them. A round whose bundle does
        # not carry the bytes one of its captures heard is incomplete,
        # and quietly classifying the half that survived would change the
        # answer without changing anything a reader could see.
        raise FeatureClassificationRefused(
            PROGRAM_MISSING,
            {
                "phases": sorted(set(missing_program)),
                "captures_dropped": len(missing_program),
                "round_dir": round_dir.name,
                "programs_present": [path.name for path in banked_programs],
                "matched_by": "provenance.stimulus.wav_sha256",
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
    (ruling S3) through the same reader :mod:`.delay_landscape` and
    ``jasper-round-views delay-landscape`` already use — never a raw lateral
    WAV, and never a second tree-walker over the bundle (house ruling R11). ``band_hz`` is the
    role's own driven sweep band, parsed by
    :func:`~.position_cycle.parse_curve_magnitude`.
    """

    pose_id: str
    position_deg: int | None
    role: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    band_hz: tuple[float, float]
    #: Signed whole-degree elevation above mark height. Carried beside
    #: ``position_deg`` because the two together are the pose key: a pooling
    #: read has to tell a raised seat from the bearing it shares.
    vertical_deg: int = 0


def load_round_pose_curves(bundle_dir: Path) -> tuple[RoundPoseCurve, ...]:
    """Every banked lateral-walk pose curve in this bundle, magnitude only.

    ``bundle_dir`` is the commissioning bundle, not the round's own artifact
    directory. Reused, not re-walked:
    :func:`~.record_index.bundle_measurements` is the same take index the
    evidence packet's ``lateral_poses`` block scans, and
    :func:`~.position_cycle.read_take_curves` is the same banked-curve reader
    :func:`~.spatial.pose_curve_record` writes. Phase is dropped — a
    persistence read is magnitude-only.

    One entry per (pose stop, role). **Latest attempt wins, per stop**: a
    retake's superseded attempts stay banked as the honest walk record, but
    only the newest readable take speaks for its stop, so a pooling read never
    averages a retake with the noise it replaced. Empty when this round ran no
    lateral walk, and never raises, which is what lets :func:`classify_round`
    tell "no lateral walk" from a directory error.
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
                    vertical_deg=row.vertical_deg,
                    role=role,
                    freqs_hz=freqs_arr,
                    magnitude_db=mag_arr,
                    band_hz=band_tuple,
                )
            )
    return tuple(out)


def gate(
    ir: np.ndarray,
    sample_rate: int,
    *,
    gate_ms: float,
    lead_ms: float = 0.0,
    peak: int | None = None,
) -> np.ndarray:
    """A FIXED-length window from the IR peak, optionally with a pre-peak lead.

    Deliberately not
    :func:`~jasper.audio_measurement.gating.gate_impulse_response`, whose
    window is sized from a DETECTED reflection: the length here is the
    caller's, so a control's known answer and the capture it is injected into
    are read through the identical window. Same shape family — flat to the
    peak, decaying half-Hann after it, raised-cosine fade into any lead.

    This is the PRIMARY and phase window only. The window LADDER has its own
    shape (:func:`~.gate_sweep.gated_segment`); the two are not
    interchangeable and their numbers are not comparable (P1 sec 6, rows D
    and F).
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


def classification_grid() -> np.ndarray:
    lo, hi = CLASSIFICATION_GRID_LO_HZ, CLASSIFICATION_GRID_HI_HZ
    return np.geomspace(lo, hi, CLASSIFICATION_GRID_POINTS)


def smoothed_curve(
    segment: np.ndarray, sample_rate: int, grid: np.ndarray
) -> np.ndarray:
    """Gated segment to a normalised fractional-octave dB curve on ``grid``.

    Smoothing is the product's own power-mean
    (:func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`), run
    on the linear rfft grid it expects and then interpolated onto the log
    grid. The lab harness used an arithmetic dB mean of its own, so these
    numbers are this instrument's, not a reproduction of the night's.
    """
    freqs, db = magnitude_response(segment.astype(np.float32), sample_rate)
    lo, hi = CLASSIFICATION_GRID_LO_HZ * 0.8, CLASSIFICATION_GRID_HI_HZ * 1.2
    keep = np.isfinite(db) & (freqs >= lo) & (freqs <= hi)
    smoothed = smooth_fractional_octave(
        freqs[keep], db[keep], MAGNITUDE_SMOOTH_FRACTION
    )
    curve = np.interp(grid, freqs[keep], smoothed)
    band = (grid >= NORMALISE_BAND_HZ[0]) & (grid <= NORMALISE_BAND_HZ[1])
    return curve - float(np.median(curve[band]))


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
    denominator ``n(n^2-1)/12`` is exact. Doing this by prefix sums rather
    than a Python loop is what keeps a run bounded: the in-band span is
    ~21 000 bins and the instrument fits a slope at every one of them,
    dozens of times per round.
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

    In the order that matters: transform with heavy zero-padding; estimate
    and remove bulk time-of-flight as a linear phase fit, without which the
    complex smoothing that follows would average rotating phasors to
    nothing; complex-smooth; take minimum phase from the smoothed
    log-magnitude with the band edges held; subtract it and remove any
    residual linear trend; differentiate by a local linear fit.

    **What it cannot see, permanently.** Bulk delay removal cannot tell a
    smooth band-wide all-pass from metres of air, so a crossover's own global
    phase rotation is removed along with the time of flight. Only LOCALISED
    excess phase is detectable, so "minimum-phase" from this instrument means
    "no local excess phase at this feature", never "the whole response is
    minimum-phase".
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
    median — a local detector, and a cancellation throws exactly that shape.
    ``p2p_us`` is peak-to-peak across the whole neighbourhood — a broad
    detector, because a gentle all-pass lifts the neighbourhood together and
    the local metric would read it as flat. ``clean`` is False when the
    neighbourhood runs off the trusted band.
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


def injection_excess_gd(
    gain: float,
    delay_ms: float,
    sample_rate: int,
    *,
    trusted_band_hz: tuple[float, float],
) -> ExcessPhase:
    """What this chain reports for a delayed-copy injection ON ITS OWN.

    ``|gain| < 1`` makes ``1 + g*z^-N`` minimum phase, so a correct reading
    is zero everywhere and whatever comes back is this chain's own artifact —
    which is what makes subtracting it sound rather than a rubber stamp.

    The artifact is :func:`_hold_band_edges`: it replaces the log-magnitude
    below :data:`EDGE_LO_HZ` with a constant while :func:`excess_group_delay`
    keeps the measured phase there. A NEGATIVE gain puts the comb's DC null
    inside that discarded band (-6.0 dB true against -3.1 dB held at
    ``g = -0.5``), and because the Hilbert relation is global the mismatch
    lands in the analysed band as excess GD rising roughly as 1/f^2 towards
    the low edge: 12.7 us at 465 Hz on a bare impulse, against C3's 10.0 us
    bar. C1 is unity at DC and C4's positive-gain combs sit on a broad DC
    maximum, so only C3 is touched. See issue #3493.

    Read through the identical chain, so the correction is made the way the
    reading is and goes to zero exactly when the hold does.
    """
    if abs(gain) >= 1.0:
        raise ValueError(
            f"injection gain {gain} is not minimum phase; its reading is "
            "signal, not artifact, and removing it would blind the control"
        )
    kernel = np.zeros(PHASE_NFFT)
    kernel[0] = 1.0
    return excess_group_delay(
        add_delayed_copy(kernel, gain, delay_ms, sample_rate),
        sample_rate,
        trusted_band_hz=trusted_band_hz,
    )


def _apply(ir: np.ndarray, coeffs: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    from scipy.signal import lfilter

    return np.asarray(lfilter(coeffs[0], coeffs[1], ir), dtype=np.float64)


def classifiable_band_hz(trusted_band_hz: tuple[float, float]) -> tuple[float, float]:
    """Where a verdict can be about the SPEAKER rather than about the band edge.

    A feature is read against its own +/-1/3-octave neighbourhood, so the
    neighbourhood has to fit inside the trusted band — otherwise
    :func:`egd_excursion` reports ``clean=False`` and the excursion is the
    edge of the measurement. This bounds detection AND an explicitly
    requested frequency.
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

    * **Two-sided prominence in the RAW pooled curve.** A real peak falls
      away on BOTH sides and a real dip rises on both, where the shoulder of
      a large resonance does neither. Removing the broad tilt MANUFACTURES
      those shoulders (a 1-octave baseline under a +3 dB resonance leaves a
      −0.7 dB trough on each flank), so prominence is read before the
      detrend. ``scipy``'s topographic prominence, bounded to the
      neighbourhood width so a distant feature cannot supply the col.
    * **A trend-removed size of the same sign.** When the two disagree about
      which way the feature points, neither is trustworthy.
    * **Stability across the round's captures**, :data:`FEATURE_STABILITY_Z`
      standard errors above the capture-to-capture scatter: below that it is
      a property of where the microphone was.
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
    # excess-GD curve carries an arbitrary constant, so a raw before/after
    # read at f0 would measure the detrend rather than the filter. The
    # expectation is the exact digital filter's own group delay over the
    # identical band, NOT the raw 4Q/w0 peak: a 2nd-order all-pass sits on a
    # plateau below f0.
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

    # C3 — the research spec's own interference case: |gain| = 0.5 is MINIMUM
    # phase, so a correct pipeline must read it flat, and flagging it would
    # be a false positive. "Flat" is the known answer for the FILTER, not for
    # this chain's reading of it: the declared edge hold discards the comb's
    # DC null and the reconstruction error lands in band.
    # :func:`injection_excess_gd` derives that from the injection and the edge
    # constants alone, so it is removed rather than tolerated by a wider bar
    # (#3493). Subtracted from the CURVE, because ``excursion_us`` peak-picks
    # and so does not carry an offset additively.
    echo = add_delayed_copy(ir, CONTROL_ECHO_GAIN, CONTROL_ECHO_MS, sample_rate)
    c3_bias = injection_excess_gd(
        CONTROL_ECHO_GAIN,
        CONTROL_ECHO_MS,
        sample_rate,
        trusted_band_hz=trusted_band_hz,
    )
    c3_ep = phase_of(echo)
    c3 = excursions(
        ExcessPhase(
            freqs=c3_ep.freqs,
            excess_phase=c3_ep.excess_phase - c3_bias.excess_phase,
            excess_gd_us=c3_ep.excess_gd_us - c3_bias.excess_gd_us,
            bulk_delay_us=c3_ep.bulk_delay_us,
        )
    )
    c3_delta = {
        key: c3[key]["excursion_us"] - baseline[key]["excursion_us"] for key in keys
    }
    c3_bias_removed = {
        key: reading["excursion_us"] for key, reading in excursions(c3_bias).items()
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
    # WHICH control missed, named once and reduced to ``passes`` — the same four
    # terms the boolean was spelled from. #3480 met the same suite passing on
    # one verify round of a rig and failing on another with nothing saying
    # which moved.
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
        # What the bar did NOT have to be widened by. Rides the verdict rather
        # than the block below because only the verdict reaches a refusal.
        "C3_injection_bias_removed_us": max(map(abs, c3_bias_removed.values())),
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
            "injection_bias_removed_us": c3_bias_removed,
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


def _sweep_ladder(
    captures: Sequence[RoundCapture],
    irs: Sequence[np.ndarray],
    peaks: Sequence[int],
    sample_rate: int,
    features: Sequence[float],
    rungs_ms: Sequence[float],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    """Every feature through :func:`~.gate_sweep.sweep_features`, plus its frame.

    Returns ``(by_feature, frame, poses, refusal)``. ``refusal`` is ``None``
    when the ladder ran; otherwise it names why it could not and
    ``by_feature`` is empty — the LADDER is refused for the round, never the
    classification. A round with one capture, with sidecars banking no
    radiated band, or with a ladder of one rung still gets its phase class,
    its decay reads and its per-pose facts, and every row says so by name
    rather than reading :data:`GATE_STABLE` off a test that never ran.

    ``poses`` is who each pose row of every feature IS, banked once for the
    round beside the frame, in the order those rows are in. Position is the
    join, not ``pose_key``: a ring capture carries an angle only when a walk
    log bound one.
    """
    rungs = tuple(sorted(float(rung) for rung in rungs_ms))
    frame = frame_descriptor(rungs, analysis_grid())
    # Everything but `radiated_band_hz`, `sample_rate`, `ir` and `peak_idx` is
    # disclosure: it names the capture a pose row was read from, and none of
    # it moves a number.
    poses: list[PoseCapture] = []
    unbanded: list[str] = []
    for capture, ir, peak in zip(captures, irs, peaks):
        band = capture.radiated_band_hz
        if band is None:
            unbanded.append(capture.wav.name)
            continue
        poses.append(
            PoseCapture(
                capture_id=capture.wav.stem,
                phase=capture.phase,
                wav=capture.wav,
                program=capture.program,
                program_sha256="",
                azimuth_deg=(
                    None if capture.degrees is None else float(capture.degrees)
                ),
                vertical_deg=None,
                mark_distance_m=None,
                radiated_band_hz=band,
                sample_rate=sample_rate,
                ir=ir,
                peak_idx=peak,
            )
        )
    if unbanded:
        return (
            {},
            frame,
            [],
            {
                "reason": REFUSE_RADIATED_BAND_MISSING,
                "captures": unbanded,
                "note": (
                    "the ladder normalises each capture on a reference band "
                    "intersected with the band its own DUT radiates, and no "
                    "declared band substitutes for one the capture did not "
                    "bank (E5, #1969)"
                ),
            },
        )
    banked_poses = [
        {
            "pose_key": pose.pose_key,
            "capture_id": pose.capture_id,
            "phase": pose.phase,
            "azimuth_deg": pose.azimuth_deg,
            "vertical_deg": pose.vertical_deg,
            "mark_distance_m": pose.mark_distance_m,
            "capture_wav": pose.wav.name if pose.wav is not None else None,
        }
        for pose in poses
    ]
    if len(rungs) < 2:
        # The engine raises on this, and a ladder is not worth the round: a
        # caller who asked for one rung still gets every other fact.
        return (
            {},
            frame,
            banked_poses,
            {
                "reason": GATE_LADDER_NEEDS_TWO_RUNGS,
                "rungs_ms": list(rungs),
                "note": (
                    "the window verdict compares the shortest and longest "
                    "resolution-valid rung, so one rung compares with nothing"
                ),
            },
        )
    try:
        swept = sweep_features(poses, rungs_ms=rungs, at_hz=list(features))
    except RoundCapturesRefused as refusal:
        return {}, frame, banked_poses, {"reason": refusal.reason, **refusal.detail}
    except ValueError as exc:
        # Anything else the engine's own input check refuses -- today only a bin off
        # its 200-20000 Hz grid, which this caller can reach with a long enough
        # `gate_ms`. Folded, not swallowed: the message rides the refusal into
        # the artifact. Remove when the engine refuses by name instead.
        return (
            {},
            frame,
            banked_poses,
            {"reason": GATE_LADDER_UNUSABLE, "detail": str(exc)},
        )
    return (
        {f"{fc:.0f}": result for fc, result in zip(features, swept)},
        frame,
        banked_poses,
        None,
    )


def _timing_scatter(
    captures: Sequence[RoundCapture],
    irs: Sequence[np.ndarray],
    peaks: Sequence[int],
    sample_rate: int,
    trusted_band_hz: tuple[float, float],
) -> dict[str, Any]:
    """Arrival-time scatter between captures at the SAME angle.

    The raw arrival spread is dominated by the capture's capture-start offset,
    which every other test removes by re-finding the peak. The SUB-SAMPLE
    residual is what survives into a phase comparison, measured by
    cross-spectrum phase slope over the direct-sound window. It needs an
    angle visited twice; with no pair the result says NOT RUN and carries no
    numbers, a dimension that did not run being a different fact from one
    that measured zero.
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


#: Fewest samples inside a centre-search span for its extremum to mean
#: anything, on the lateral walk's own coarse evidence grid
#: (``spatial.LATERAL_EVIDENCE_POINTS_PER_OCTAVE`` = 12/octave). Lower
#: than :data:`_MIN_SLOPE_SAMPLES`: this reads an extremum, not a slope.
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

    Direct on the curve's own banked ``(freqs_hz, magnitude_db)``, never a
    raw WAV, and detrended with this module's own :func:`detrend` so a
    departure read here means what it means in the primary detector.

    NOT-RESOLVED (``resolved=False``, both numbers ``None``) whenever this
    pose cannot answer at all: ``fc``'s neighbourhood falls outside the
    curve's driven ``band_hz``, or too few grid points land inside the
    centre-search span. Never a fabricated 0 dB — absence is not zero.
    ``detrended`` is computed once per curve by the caller.
    """
    lo = fc * 2**-NEIGHBOURHOOD_OCT
    hi = fc * 2**NEIGHBOURHOOD_OCT
    base: dict[str, Any] = {
        "pose_id": curve.pose_id,
        "position_deg": curve.position_deg,
        "vertical_deg": curve.vertical_deg,
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


def _pose_persistence_block(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """One feature's pose table, under the spread across the poses that
    RESOLVED it, so the position question is a number rather than N rows.

    ``sigma_pooled_db`` is the sample standard deviation (ddof=1) of those
    rows' ``pooled_db``, and ``None`` under two of them, where it is
    undefined: a fabricated 0.0 would read as a feature that held perfectly
    across a walk nobody took. A statistic, never a verdict -- what a spread
    means for a filter is the reader's, on the methodology's thresholds.
    """
    resolved = [row["pooled_db"] for row in readings if row["resolved"]]
    return {
        "n_poses": len(readings),
        "n_resolved": len(resolved),
        "sigma_pooled_db": (
            float(np.std(resolved, ddof=1)) if len(resolved) > 1 else None
        ),
        "poses": readings,
    }


def _pose_bank_block(pose_curves: Sequence[RoundPoseCurve]) -> dict[str, Any]:
    """This round's lateral-pose bank, once -- what every row's
    ``pose_persistence`` table is read against.

    Mirrors :func:`_timing_scatter`'s NOT-RUN shape: "no lateral poses
    banked" is a different fact from "every pose read as not-resolved".
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


def _decay_bands_hz(fc: float) -> dict[str, tuple[float, float]]:
    """The centre band and its two flanks, every one ``FEATURE_HALF_OCT`` wide.

    The centre band is :func:`read_feature`'s, restated so a magnitude read
    and a decay read agree on what "the feature's own band" means. Each
    flank is the SAME width, its own inner edge
    :data:`DECAY_FLANK_SKIRT_OFFSET_OCT` beyond the centre band's matching
    skirt, never narrower.
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
    module's own reflection-free deconvolution, already long and clean, so no
    taper is needed. :func:`~jasper.audio_measurement.gating.analytic_envelope`
    is REUSED, not duplicated.
    """
    mask = (host.freqs >= band_hz[0]) & (host.freqs <= band_hz[1])
    band_limited = np.fft.irfft(host.spectrum * mask, n=host.n)
    return analytic_envelope(band_limited)


def _decay_read(host: _DecayHost, band_hz: tuple[float, float]) -> dict[str, Any]:
    """Time-to-``DECAY_TARGET_DROP_DB`` in one band, or an honest non-answer.

    ``noise_floor_db`` is the envelope's own late-tail level, dB relative to
    THIS band's own peak, so it is directly comparable to
    :data:`DECAY_TARGET_DROP_DB`. ``below_floor`` is a property of that
    number alone. ``time_to_neg20_db_ms`` is ``None`` whenever
    ``below_floor`` is true OR the envelope never reaches the target within
    the IR's own length — never a fabricated time.
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


#: The two cycle counts research 03's Stage 3 names: a narrow one that best
#: rejects reflections and a wide one closer to the shipped primary window's
#: own length. ADR-0201 binds what this buys: re-analysis EVIDENCE only
#: -- no FDW output here may feed a target or a grade, and the diagnostic
#: reading rule (a dip that fills in under FDW but stays deep under the
#: fixed gate is reflection-caused) is guide content, not code.
FDW_CYCLES: tuple[float, ...] = (5.0, 15.0)

#: Half-width of the band an FDW rung is read over. Reused rather than
#: re-picked: :data:`NEIGHBOURHOOD_OCT` is already this module's standing
#: "local context" width and both feature readers' search spans fit
#: inside it, so they serve the FDW curve unmodified.
FDW_BAND_OCT = NEIGHBOURHOOD_OCT

#: Points across that band, log-spaced. Hundreds, not the ~2000/octave main
#: analysis grid: FDW re-picks its window at EVERY point, so each point
#: is its own direct frequency read rather than a shared FFT bin — an
#: offline, twice-per-feature cost, not a global one.
FDW_GRID_POINTS = 300

#: Taper shape for the FDW window. Hann: it is already the family
#: :func:`gate`'s own tail uses, so this instrument carries one taper family
#: rather than introducing a second one for a diagnostic reading.
FDW_TAPER = "hann"


def _fdw_read_hz(
    ir: np.ndarray, sample_rate: int, peak: int, freq_hz: float, cycles: float
) -> float:
    """One frequency's FDW magnitude, dB, from a window of ``cycles`` cycles
    of ``freq_hz`` (``cycles / freq_hz`` seconds) CENTRED on ``peak``.

    A direct single-bin read: the window differs at every frequency, so no
    one FFT serves the whole curve. :data:`FDW_TAPER`, then the
    exact-frequency DFT term, normalised by the taper's own coherent gain so
    a window-LENGTH change alone cannot move the level.
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
    and :func:`_extremum_reading` unmodified, rather than a second "how big is
    this feature" for one more window shape.
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

    Diagnostic only (ADR-0201) -- ``pooled_db`` / ``centre_hz`` per cycle
    count, pooled across the round's own captures the identical way the
    primary curve is. Never a target, never a grade: nothing downstream reads
    this key for either.
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


#: The engine's three words in the register's own. A mapping, never a second
#: rule: :mod:`.gate_sweep` decides what counts as the window having moved a
#: feature, and ``unresolved`` — the test did not answer — becomes
#: :data:`UNRESOLVED`, never :data:`GATE_STABLE`, which is a finding.
_GATE_VERDICT_OF = {WINDOW_STABLE: GATE_STABLE, WINDOW_MOVED: GATE_MOVED}


def _gate_call(
    sweep: Mapping[str, Any] | None, refusal: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One feature's window verdict and its working, off the engine's result.

    The verdict is :data:`~.gate_sweep.WINDOW_MOVED` and friends translated
    through :data:`_GATE_VERDICT_OF`; which routes fired, and the thresholds
    they fired against, are the engine's and are carried through in
    ``gate_sensitivity`` rather than re-derived here.
    """
    notes: list[str] = []
    if refusal is not None:
        notes.append(f"gate ladder refused: {refusal['reason']}")
    sensitivity = None if sweep is None else sweep.get("sensitivity")
    if sweep is not None and sensitivity is None:
        notes.append(
            f"no window sensitivity: {sweep.get('sensitivity_null_reason')}"
        )

    excess_loss: dict[str, float] = {}
    slack_by_rung: dict[str, float] = {}
    tension = False
    if sweep is None:
        gate_verdict = UNRESOLVED
    else:
        gate_verdict = _GATE_VERDICT_OF.get(sweep["window_verdict"], UNRESOLVED)
    if sweep is not None and sensitivity is not None:
        long_key = f"{sensitivity['longest_valid_rung_ms']:g}"
        delta = float(sensitivity["corrected_delta_db"])
        # Keyed by the rung the delta was read AT, against the shortest valid
        # one; both endpoints are named in `gate_sensitivity`.
        excess_loss[long_key] = delta
        slack_by_rung[long_key] = GATE_DELTA_SLACK_DB
        if not sensitivity["sigma_growth_readable"]:
            long_sigma = float(sweep["sigma_db_by_rung"][long_key])
            notes.append(
                f"across-pose sigma is {long_sigma:.3f} dB at {long_key} ms, "
                f"under the {SIGMA_GROWTH_MIN_SIGMA_DB:g} dB floor: these "
                "captures did not disagree, so the growth ratio is their own "
                "noise and is not read"
            )
        tension = (
            gate_verdict == GATE_STABLE and abs(delta) > 0.5 * GATE_DELTA_SLACK_DB
        )

    rungs = _gate_rungs(sweep)
    for rung_key, entry in rungs.items():
        if not entry["resolved"]:
            notes.append(
                f"{rung_key} ms is resolution-invalid at {entry['cycles']:.1f} "
                "cycles in the window: it is read but it bounds no sensitivity"
            )
    return {
        "gate_verdict": gate_verdict,
        "excess_loss_vs_null": excess_loss,
        "gate_slack": slack_by_rung,
        "gate_notes": notes,
        "resolved_gates": 0 if sweep is None else int(sweep["n_valid_rungs"]),
        "gate_rungs": rungs,
        "gate_sensitivity": (
            {"ladder_refused": dict(refusal or {})}
            if sweep is None
            else {
                "bin_hz": sweep["bin_hz"],
                "window_verdict": sweep["window_verdict"],
                "window_verdict_reasons": list(sweep["window_verdict_reasons"]),
                "sensitivity": sensitivity,
                "null_reason": sweep.get("sensitivity_null_reason"),
                "poses": sweep["poses"],
            }
        ),
        "tension": tension,
    }


def _gate_rungs(sweep: Mapping[str, Any] | None) -> dict[str, Any]:
    """Every rung's own facts, additive to the verdict the ladder composed.

    ``_gate_call`` carries only what the verdict turned on. A reader auditing
    the ladder itself needs every rung, including the ones whose own
    resolution bars them from bounding a sensitivity. The per-pose values are
    in ``gate_sensitivity``; this is their pooled form.
    """
    if sweep is None:
        return {}
    poses = sweep["poses"]
    return {
        rung_key: {
            "pooled_db": float(
                np.median([pose["detrended_db_by_rung"][rung_key] for pose in poses])
            ),
            "sigma_db": sigma,
            "cycles": sweep["cycles_by_rung"][rung_key],
            "resolution": sweep["resolution_by_rung"][rung_key],
            "resolved": sweep["resolution_by_rung"][rung_key] != "invalid",
        }
        for rung_key, sigma in sweep["sigma_db_by_rung"].items()
    }


def _compose(
    fc: float,
    egd: Mapping[str, Any],
    gate: Mapping[str, Any],
    control_pair: Mapping[str, Any],
    pooled_db: float,
    measured_q: float,
    *,
    controls_ok: bool,
    timing_available: bool,
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

    gate_verdict = gate["gate_verdict"]
    is_dip = pooled_db < 0
    if egd_verdict == EGD_NON_MIN_PHASE:
        classification = INTERFERENCE_BARRED
    elif gate_verdict == GATE_MOVED:
        classification = ROOM
    elif egd_verdict == EGD_MIN_PHASE and gate_verdict == GATE_STABLE:
        # A defect verdict is what a filter is vouched by, so it needs BOTH
        # tests to have answered. A ladder that did not run is not a stable
        # one, and reading it as one would vouch for a filter aimed at a
        # feature nothing checked for the room.
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
    resolved_gates = gate["resolved_gates"]
    # The three words are quality_model's shared TrustLevel — `medium` spelled in
    # full. An artifact banked before 2026-08-22 carries `med` and is
    # normalised on the way back in by
    # feature_classification.read_feature_verdicts.
    confidence: TrustLevel
    if classification == UNRESOLVED:
        confidence = "low"
    elif gate["tension"]:
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
        "excess_loss_vs_null": gate["excess_loss_vs_null"],
        "gate_slack": gate["gate_slack"],
        "gate_notes": gate["gate_notes"],
        "controls_ok": controls_ok,
        "timing_corroborated": timing_available,
    }


def classify_round(
    captures: Sequence[RoundCapture],
    *,
    at: Sequence[float] | None = None,
    gate_ms: float = DEFAULT_GATE_MS,
    gates_ms: Sequence[float] | None = None,
    pose_curves: Sequence[RoundPoseCurve] = (),
) -> dict[str, Any]:
    """Classify one round's features and return the artifact to bank.

    ``at`` pins the frequencies to classify; omitted, they are detected from
    the round's own pooled response (:func:`_detect_features`).

    ``gate_ms`` is the PRIMARY window — the one the phase test, the feature
    detector and the trusted band are read through. ``gates_ms`` is the
    window LADDER, which is :mod:`.gate_sweep`'s. The two are independent.

    ``pose_curves`` is this round's banked lateral-walk curves
    (:func:`load_round_pose_curves`), optional and orthogonal to
    ``captures``: a caller with none still gets every EGD/gate/timing fact,
    plus a ``pose_bank``/``pose_persistence`` NOT-RUN pair rather than a
    refusal.

    A round whose known-answer controls did not pass is REPORTED, not
    refused: every row keeps its real ``gate_verdict`` and gives up only its
    phase class, and ``controls_disclosure`` at the top of the artifact says
    so in words.

    Raises :class:`FeatureClassificationRefused` with
    :data:`NO_FEATURES_DETECTED` when nothing stood above the round's own
    scatter.
    """
    if not captures:
        raise FeatureClassificationRefused(NO_ADMISSIBLE_CAPTURES, {"n_captures": 0})
    ladder = tuple(sorted(float(g) for g in (gates_ms or DEFAULT_RUNGS_MS)))
    primary = float(gate_ms)
    trusted_band_hz = (f_trusted_floor_hz(primary * 1e-3), TRUSTED_CEILING_HZ)
    grid = classification_grid()

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

    # TEST 1. Two transforms per CAPTURE — the phase gate's lead, and the
    # zero-lead window whose disagreement is reported as
    # `lead_sensitivity_us` — and every feature's excursion read off each.
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

    # The feature's own pooled size and width, off the PRIMARY window — the
    # curves this function already built. Not the ladder's: the ladder's job is
    # what changes across windows, and the row's depth is what one window read.
    pooled = detrended.mean(axis=0)
    pooled_db = {f"{fc:.0f}": read_feature(pooled, grid, fc) for fc in features}
    measured_q = {f"{fc:.0f}": feature_q(pooled, grid, fc) for fc in features}

    swept, frame, ladder_poses, ladder_refusal = _sweep_ladder(
        captures, irs, peaks, sample_rate, features, ladder
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
        gate_call = _gate_call(swept.get(key), ladder_refusal)
        row = _compose(
            fc,
            egd_rows[key],
            gate_call,
            controls["C4_pair"]["at"][key],
            pooled_db[key],
            measured_q[key],
            controls_ok=controls_ok,
            timing_available=bool(timing["available"]),
        )
        row["gate_rungs"] = gate_call["gate_rungs"]
        row["gate_sensitivity"] = gate_call["gate_sensitivity"]
        row["pose_persistence"] = _pose_persistence_block([
            _pose_reading(curve, detrended, fc, pooled_db[key] < 0)
            for curve, detrended in zip(pose_curves, pose_detrended)
        ])
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
            "sigma_growth_room_ratio": SIGMA_GROWTH_ROOM_RATIO,
            "sigma_growth_min_sigma_db": SIGMA_GROWTH_MIN_SIGMA_DB,
            "gate_delta_slack_db": GATE_DELTA_SLACK_DB,
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
            "gate_ladder_frame": frame,
            # Who each feature's pose rows ARE, once for the round. The engine
            # keys them by `pose_key` and publishes nothing else per feature,
            # because who a pose is does not vary with the bin.
            "gate_ladder_poses": ladder_poses,
            "gate_ladder_refused": ladder_refusal,
            # P1 sec 6 measured one capture's 441.6 Hz feature through both window
            # families and they disagree by 1.72 dB on the same 7->20 ms change (row
            # D, this ladder's shape, against row F, the shape it replaced). Ladder
            # numbers banked before this instrument moved onto the engine are row F
            # and are not comparable with these.
            "gate_ladder_window_changed": (
                "the ladder's window family changed with the move onto "
                "jasper.active_speaker.crossover_v2.gate_sweep; ladder numbers "
                "banked earlier were read through a full-span half-Hann tail "
                "with no lead (P1 sec 6 row F) and are not comparable with "
                "these (row D). gate_ladder_frame states this frame in full"
            ),
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
        "controls_disclosure": None if controls_ok else CONTROLS_FAILED_DISCLOSURE,
        "controls": controls,
        "timing_scatter": timing,
        "pose_bank": _pose_bank_block(pose_curves),
        "rows": rows,
    }


def summary_lines(artifact: Mapping[str, Any]) -> list[str]:
    """One line per classified feature, under the round's controls disclosure.

    Here rather than in the CLI so the columns and the artifact they read stay
    in one module: an exit-0 round whose known-answer controls failed must not
    read as a clean one, which is what the first line says when there is one.
    """
    lines = []
    if artifact["controls_disclosure"] is not None:
        lines.append(f"  controls: {artifact['controls_disclosure']}")
    lines.extend(
        f"  {row['hz']:8.0f} Hz  {row['classification']:<34} "
        f"{row['confidence']:>6}  egd={row['egd_verdict']:<14} "
        f"gate={row['gate_verdict']:<7} depth={row['depth_db']:.2f} dB"
        for row in artifact["rows"]
    )
    return lines
