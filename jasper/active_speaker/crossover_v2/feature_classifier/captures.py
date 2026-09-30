# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The phases classification reads, and the lateral pose curves it pools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ...measurement_programs import PURPOSE_SPEAKER
from ...run_manifest import kept_measurements
from ..journey import (
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from ..position_cycle import (
    OWN_WINDOW,
    parse_curve_magnitude,
    take_curves,
)


ADMISSIBLE_PHASES = frozenset({PHASE_VERIFY, PHASE_CLOUD_VERIFY, PHASE_LATERAL})


@dataclass(frozen=True)
class RoundPoseCurve:
    """One banked speaker take's one driver-role curve at its pose, magnitude only.

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
    """Every pose curve of a MEASURE or lateral speaker take this bundle's
    round kept, magnitude only.

    ``bundle_dir`` is the commissioning bundle, not the round's own artifact
    directory. Reused, not re-walked:
    :func:`~jasper.active_speaker.run_manifest.kept_measurements` is the take
    index, and :func:`~.position_cycle.take_curves` the
    banked-curve reader the delay pair uses. Phase is dropped — a persistence
    read is magnitude-only.

    One entry per (kept take, role). The run manifest keeps one take per
    stop: a retake's superseded attempts stay banked as the honest walk record
    but never speak for their stop, so a pooling read never averages a retake
    with the noise it replaced. Empty when this round kept no such take,
    which is what lets :func:`classify_round` tell "no pose" from a
    directory error.
    """

    out: list[RoundPoseCurve] = []
    for row, record in kept_measurements(bundle_dir, phases=(PHASE_MEASURE, PHASE_LATERAL), purposes=(PURPOSE_SPEAKER,)):
        pose_id = Path(row.path).stem
        for curve in take_curves(record, OWN_WINDOW) or ():
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
