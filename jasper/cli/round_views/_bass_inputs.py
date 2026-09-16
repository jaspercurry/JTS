# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Adapt CLI fit arguments to the engine's bass table inputs."""

from typing import Any
from jasper.active_speaker.bass_table_inputs import fit_bass_rounds, join_bass_rounds as join_bass_rounds


def fit_run(args) -> dict[str, Any]:
    return fit_bass_rounds(args.round_dir, candidates=args.candidate,
                          reference_band_hz=(args.reference_band_hz[0], args.reference_band_hz[1]))
