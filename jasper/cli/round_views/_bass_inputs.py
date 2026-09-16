# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Adapt CLI fit arguments to the engine's bass table inputs."""

import json
from pathlib import Path
from typing import Any, Mapping
from jasper.active_speaker.bass_table_inputs import fit_bass_rounds, join_bass_rounds as join_bass_rounds


def fit_run(args, *, target: Mapping[str, Any] | None = None) -> dict[str, Any]:
    target = args.target if target is None else target
    if isinstance(target, Path):
        target = json.loads(target.read_text())
    return fit_bass_rounds(args.round_dir, candidates=args.candidate, target=target,
                          tolerance_db=args.tolerance_db, reference_band_hz=(args.reference_band_hz[0], args.reference_band_hz[1]))
