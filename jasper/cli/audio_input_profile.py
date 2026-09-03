# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI shim for the audio input profile vocabulary used by shell reconcilers.

`jasper.audio_profile_state` owns the profile aliases, the profile -> wake-leg
vectors and which profiles seek chip-AEC at all. `deploy/bin/jasper-aec-reconcile`
reads them here instead of carrying a second copy in bash.
"""
from __future__ import annotations

import argparse
import shlex
import sys

from ..audio_profile_state import (
    ALL_PROFILES, AEC_MODE_ENV, PROFILE_AUTO, PROFILE_CUSTOM,
    PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING, AecIntent,
    infer_audio_input_profile, normalize_audio_input_profile, parse_env_bool,
    profile_env_updates, resolve_profile_wake_legs,
)


# Env key -> the shell variable deploy/bin/jasper-aec-reconcile evals it into.
SHELL_VARS = {
    AEC_MODE_ENV: "AEC_MODE",
    "JASPER_WAKE_LEG_RAW": "LEG_RAW",
    "JASPER_WAKE_LEG_DTLN": "LEG_DTLN",
    "JASPER_WAKE_LEG_CHIP_AEC": "LEG_CHIP_AEC",
    "JASPER_WAKE_LEG_CHIP_AEC_150": "LEG_CHIP_AEC_150",
    "JASPER_WAKE_LEG_CHIP_AEC_210": "LEG_CHIP_AEC_210",
}
_UNMAPPED = {
    key for profile in ALL_PROFILES for key in profile_env_updates(profile)
} - {"JASPER_AUDIO_INPUT_PROFILE", *SHELL_VARS}
assert not _UNMAPPED, f"no shell variable for {sorted(_UNMAPPED)}"

# Profiles whose legs depend on whether chip-AEC can arm, and the subset that
# also needs a 6-channel mic before it will leave software AEC3. The shell
# runs the ALSA/DAC probes; which profiles those probes even apply to is here.
_CHIP_SEEKING = (PROFILE_AUTO, PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING)
_NEEDS_MIC_READY = (PROFILE_AUTO,)

# --infer takes the mode file's raw strings, one option per wake leg.
_LEG_OPTIONS = (
    "--leg-raw",
    "--leg-dtln",
    "--leg-chip-aec",
    "--leg-chip-aec-150",
    "--leg-chip-aec-210",
)


def _quoted(values: dict[str, str]) -> str:
    return "\n".join(f"{name}={shlex.quote(value)}" for name, value in values.items())


def selection_assignments(profile: str, chip_available: str | None) -> str:
    """The profile's normalized name, its chip-AEC facts and, once the shell
    has answered `--chip-available`, the wake-leg vector it lands on."""

    recognized = normalize_audio_input_profile(profile, default="")
    normalized = recognized or PROFILE_CUSTOM
    legs = resolve_profile_wake_legs(normalized, chip_available=chip_available == "1")
    values = {
        "AUDIO_INPUT_PROFILE": normalized,
        "AUDIO_INPUT_PROFILE_KNOWN": "1" if recognized else "0",
        # `custom` has no vector of its own, so the shell can skip asking.
        "AUDIO_INPUT_PROFILE_HAS_LEGS": "1" if legs else "0",
        "CHIP_AEC_TESTING_REQUESTED": (
            "1" if normalized == PROFILE_XVF_CHIP_AEC_TESTING else "0"
        ),
        "CHIP_AEC_PROFILE_SEEKS": "1" if normalized in _CHIP_SEEKING else "0",
        "CHIP_AEC_PROFILE_NEEDS_MIC_READY": (
            "1" if normalized in _NEEDS_MIC_READY else "0"
        ),
    }
    if chip_available is not None:
        values.update(
            {SHELL_VARS[key]: value for key, value in legs.items() if key in SHELL_VARS}
        )
    return _quoted(values)


def inferred_assignment(args: argparse.Namespace) -> str:
    """The closest profile for a mode file written before profiles existed."""

    intent = AecIntent(
        mode=args.mode,
        raw_enabled=parse_env_bool(args.leg_raw, True),
        dtln_enabled=parse_env_bool(args.leg_dtln, False),
        chip_aec_enabled=parse_env_bool(args.leg_chip_aec, False),
        chip_aec_150_enabled=parse_env_bool(args.leg_chip_aec_150, False),
        chip_aec_210_enabled=parse_env_bool(args.leg_chip_aec_210, False),
    )
    return _quoted({"AUDIO_INPUT_PROFILE": infer_audio_input_profile(intent)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="")
    parser.add_argument("--chip-available", choices=("0", "1"))
    parser.add_argument("--infer", action="store_true")
    parser.add_argument("--mode", default="auto")
    for option in _LEG_OPTIONS:
        parser.add_argument(option, default="")
    args = parser.parse_args(argv)

    if args.infer:
        print(inferred_assignment(args))
    else:
        print(selection_assignments(args.profile, args.chip_available))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
