# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Banked captures and lateral pose curves for feature classification."""

from __future__ import annotations

import json
import wave
from collections.abc import (
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.audio_measurement.bundles import sha256_file

from ..evidence_packet.offline_reads import RING_SIDECAR_GLOB
from ..journey import (
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
    PHASE_VERIFY,
)
from ..position_cycle import (
    parse_curve_magnitude,
    read_take_curves,
)
from ..record_index import (
    Measurement,
    bundle_measurements,
)
from ..round_captures import radiated_band_of
from ..spatial import take_stop_id

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
    ROUND_SHAPE_INADMISSIBLE,
    NO_ADMISSIBLE_CAPTURES,
    CAPTURES_UNREADABLE,
    PROGRAM_MISSING,
    NO_FEATURES_DETECTED,
})


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
    CAPTURE_PHASE_NOT_ADMISSIBLE: ROUND_SHAPE_INADMISSIBLE,
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
        "this round banked no admissible capture shape"
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


ADMISSIBLE_PHASES = frozenset({PHASE_VERIFY, PHASE_CLOUD_VERIFY, PHASE_LATERAL})


@dataclass(frozen=True)
class RoundCapture:
    """One banked capture, bound to the program that produced it."""

    wav: Path
    program: Path
    phase: str
    #: Capture stamp, seconds. The dump filename's own microsecond stamp.
    stamp: float
    #: Commanded angle from the take, or a matching turntable walk release.
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
    through the same constant.

    ``session_id`` scopes the ring to this round. It is the BUNDLE session id
    (``info.json``'s), which is what a sidecar stamps into
    ``jts_session_identity``; the capture id that names ``round_dir`` is a
    different namespace. Omitting it admits every capture in the ring, which
    is correct only when the ring holds one round. ``walk_logs`` supply angles
    only for captures without a banked position.

    Raises :class:`FeatureClassificationRefused` — never returns empty, "no
    captures" being a finding a caller must be told by name. EVERY refusal
    carries ``captures``: one row per sidecar the ring listed, the reason drawn
    from :data:`CAPTURE_ADMISSIBILITY_REASONS`, and which name a no-capture
    round refuses under is read off those rows through
    :data:`_REFUSAL_FOR_CAPTURE_REASON`.
    """
    # Different takes and legacy aliases may have identical program bytes.
    banked_programs = sorted(round_dir.glob("*_program.wav"))
    programs = {
        sha256_file(path): path
        for path in banked_programs
    }
    releases = _walk_releases(walk_logs)

    captures: list[RoundCapture] = []
    seen_phases: dict[str, int] = {}
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
                degrees=(doc["position_deg"] if type(doc.get("position_deg")) is int
                         else _bind_angle(stamp, releases)),
                # One owner for the E5 rule, and one parse of it: the sibling
                # round loader the gate sweep reads through owns this.
                radiated_band_hz=radiated_band_of(doc),
            )
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
