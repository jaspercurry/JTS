# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI shim for the audio input profile vocabulary used by shell reconcilers.

`jasper.audio_profile_state` owns the profile aliases and the profile ->
wake-leg vectors; `deploy/bin/jasper-aec-reconcile` reads them here instead of
carrying a second copy in bash.
"""
from __future__ import annotations

import argparse
import shlex
import sys

from ..audio_profile_state import (
    PROFILE_CUSTOM, normalize_audio_input_profile, resolve_profile_wake_legs,
)


# Env key -> the shell variable deploy/bin/jasper-aec-reconcile evals it into.
SHELL_VARS = {
    "JASPER_AEC_MODE": "AEC_MODE",
    "JASPER_WAKE_LEG_RAW": "LEG_RAW",
    "JASPER_WAKE_LEG_DTLN": "LEG_DTLN",
    "JASPER_WAKE_LEG_CHIP_AEC": "LEG_CHIP_AEC",
    "JASPER_WAKE_LEG_CHIP_AEC_150": "LEG_CHIP_AEC_150",
    "JASPER_WAKE_LEG_CHIP_AEC_210": "LEG_CHIP_AEC_210",
}


def shell_assignments(profile: str, chip_available: str | None) -> str:
    """Shell assignments for `profile`, plus its legs when capability is known."""

    recognized = normalize_audio_input_profile(profile, default="")
    values = {
        "AUDIO_INPUT_PROFILE": recognized or PROFILE_CUSTOM,
        # An empty selection is the documented "no profile written yet", not a
        # typo, so it stays recognized.
        "AUDIO_INPUT_PROFILE_KNOWN": (
            "1" if recognized or not profile.strip().strip("'\"") else "0"
        ),
    }
    if chip_available is not None:
        legs = resolve_profile_wake_legs(
            values["AUDIO_INPUT_PROFILE"],
            chip_available=chip_available == "1",
        )
        values.update({SHELL_VARS[key]: value for key, value in legs.items()})
    return "\n".join(f"{name}={shlex.quote(value)}" for name, value in values.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="")
    parser.add_argument("--chip-available", choices=("0", "1"))
    args = parser.parse_args(argv)
    print(shell_assignments(args.profile, args.chip_available))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
