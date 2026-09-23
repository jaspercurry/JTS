# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Each take's measured impulses, kept in its bundle beside the recording.

A take's analysis deconvolves every sweep it played; this keeps those
impulses so a later reader derives magnitude, phase, group delay or decay
from what was measured instead of from a coarse curve. One ``.npz`` per take
holds the arrays; the take record's ``impulses`` block indexes them, and the
bundle's artifact manifest carries the file's hash. See ADR-0354.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.atomic_io import atomic_write_bytes
from jasper.audio_measurement.bundles import record_artifact
from jasper.audio_measurement.program_analysis import RecordedImpulse
from jasper.json_fields import sha256_file

from ..bundles import BUNDLE_FILE_MODE

IMPULSES_DIR = "impulses"
IMPULSES_KIND = "jts_speaker_take_impulses"
IMPULSES_SCHEMA = "jts_take_impulses/1"
#: The take record's key for the block :func:`write_take_impulses` returns.
IMPULSES_KEY = "impulses"


class TakeImpulsesUnreadable(ValueError):
    """A take names impulses its bundle cannot give back as written."""


REFUSE_TAKE_IMPULSES_UNREADABLE = "take_impulses_unreadable"


@dataclass(frozen=True)
class TakeImpulse:
    """One response's impulse within a take: its role, which occurrence, the samples."""

    role: str
    repeat_index: int
    impulse: RecordedImpulse


def analysis_impulses(analysis: Any) -> tuple[TakeImpulse, ...]:
    """Every impulse an analysis kept, primaries before their repeats."""
    responses = [*analysis.driver_responses]
    if analysis.summed_response is not None:
        responses.append(analysis.summed_response)
    return tuple(
        TakeImpulse(one.role, one.repeat_index or 0, one.impulse)
        for response in responses
        for one in (response, *response.repeat_responses)
        if one.impulse is not None
    )


def write_take_impulses(
    bundle_dir: Path, take_id: str, analysis: Any, *, recording: str | None,
) -> dict[str, Any] | None:
    """Write a take's impulses and return the block its record carries.

    ``None`` when the analysis kept none (a CHECK take). ``recording`` is the
    bundle-relative capture WAV the impulses were deconvolved from.
    """
    impulses = analysis_impulses(analysis)
    if not impulses:
        return None
    arrays: dict[str, Any] = {f"r{index}": one.impulse.samples.astype(np.float32)
                              for index, one in enumerate(impulses)}
    buffer = io.BytesIO()
    np.savez(buffer, **arrays)
    relative = f"{IMPULSES_DIR}/{take_id}.npz"
    atomic_write_bytes(bundle_dir / relative, buffer.getvalue(), mode=BUNDLE_FILE_MODE)
    entry = record_artifact(
        bundle_dir, relative, kind=IMPULSES_KIND, sensitivity="derived", recomputable=True,
        generated_by=__name__, dependencies=[recording] if recording else (),
    )
    return {
        "schema": IMPULSES_SCHEMA,
        "path": relative,
        "sha256": entry["sha256"],
        "responses": [
            {
                "key": f"r{index}",
                "role": one.role,
                "repeat_index": one.repeat_index,
                "segment_id": one.impulse.segment_id,
                "sample_rate_hz": one.impulse.sample_rate_hz,
                "samples": int(one.impulse.samples.size),
                "origin_index": one.impulse.origin_index,
                "peak_index": one.impulse.peak_index,
                "clock_shift_samples": round(one.impulse.clock_shift_samples, 4),
            }
            for index, one in enumerate(impulses)
        ],
    }


def take_impulses(bundle_dir: Path, document: Mapping[str, Any]) -> tuple[TakeImpulse, ...]:
    """The impulses a take record names, read back and checked against its hash.

    Empty when the take kept none (banked before impulses were kept, or a CHECK).
    """
    block = document.get(IMPULSES_KEY)
    if not isinstance(block, Mapping):
        return ()
    path = Path(bundle_dir) / str(block.get("path") or "")
    try:
        if sha256_file(path) == block.get("sha256"):
            with np.load(path, allow_pickle=False) as arrays:
                return tuple(
                    TakeImpulse(
                        str(row["role"]), int(row["repeat_index"]),
                        RecordedImpulse(
                            samples=np.asarray(arrays[row["key"]], dtype=np.float64),
                            sample_rate_hz=int(row["sample_rate_hz"]),
                            origin_index=int(row["origin_index"]),
                            peak_index=int(row["peak_index"]),
                            segment_id=str(row["segment_id"]),
                            clock_shift_samples=float(row["clock_shift_samples"]),
                        ),
                    )
                    for row in block["responses"]
                )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise TakeImpulsesUnreadable(f"{path}: {exc}") from exc
    raise TakeImpulsesUnreadable(f"{path}: content does not match the take record's hash")


def impulse_for(
    impulses: tuple[TakeImpulse, ...], role: str, repeat_index: int = 0,
) -> RecordedImpulse | None:
    """The one impulse a take kept for ``role`` at ``repeat_index``, if any."""
    return next((one.impulse for one in impulses
                 if one.role == role and one.repeat_index == repeat_index), None)
