# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI shim for the audio input profile vocabulary used by shell reconcilers.

`jasper.audio_profile_state` owns the profile aliases, the AEC-mode and
wake-leg boolean vocabularies, the profile -> wake-leg vectors and which
profiles seek chip-AEC at all. `deploy/bin/jasper-aec-reconcile` reads them
here instead of carrying a second copy in bash (ADR-0235 D1).
"""
from __future__ import annotations

import argparse
import shlex
import sys

from ..audio_profile_state import (
    ALL_PROFILES, AEC_MODE_ENV, PROFILE_AUTO, PROFILE_CUSTOM,
    PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING, AecIntent,
    infer_audio_input_profile, normalize_aec_mode,
    normalize_audio_input_profile, parse_env_bool, profile_env_updates,
    resolve_profile_wake_legs,
)
from ..env_load import parse_env_file


# Opt-in lab observation. The wizard-owned mode file is its only writer, so it
# is read from that file rather than from the pass's environment.
CHIP_REF_OBSERVE_ENV = "JASPER_AEC_CHIP_REF_OBSERVE"

# Env key -> the shell variable deploy/bin/jasper-aec-reconcile evals it into.
SHELL_VARS = {
    AEC_MODE_ENV: "AEC_MODE",
    "JASPER_WAKE_LEG_RAW": "LEG_RAW",
    "JASPER_WAKE_LEG_DTLN": "LEG_DTLN",
    "JASPER_WAKE_LEG_CHIP_AEC": "LEG_CHIP_AEC",
    "JASPER_WAKE_LEG_CHIP_AEC_150": "LEG_CHIP_AEC_150",
    "JASPER_WAKE_LEG_CHIP_AEC_210": "LEG_CHIP_AEC_210",
    CHIP_REF_OBSERVE_ENV: "CHIP_REF_OBSERVE",
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

# The wake legs, in the order both verbs use: the argparse destination the
# shell hands the raw string on, the aec_mode.env key it comes from, and the
# build's default for a value an older deploy's file omits. RAW defaults on
# (cheap OR-fusion wake-rate recovery); every other leg is opt-in.
_WAKE_LEGS = (
    ("leg_raw", "JASPER_WAKE_LEG_RAW", True),
    ("leg_dtln", "JASPER_WAKE_LEG_DTLN", False),
    ("leg_chip_aec", "JASPER_WAKE_LEG_CHIP_AEC", False),
    ("leg_chip_aec_150", "JASPER_WAKE_LEG_CHIP_AEC_150", False),
    ("leg_chip_aec_210", "JASPER_WAKE_LEG_CHIP_AEC_210", False),
)


def _quoted(values: dict[str, str]) -> str:
    return "\n".join(f"{name}={shlex.quote(value)}" for name, value in values.items())


def _leg_enabled(raw: str | None, default: bool) -> bool:
    """One wake-leg boolean. `None` is "nothing was written" and takes the
    build default; anything else the vocabulary does not have reads as off, so
    a typo never silently arms a leg."""

    return default if raw is None else parse_env_bool(raw, default=False)


def _shell_bool(value: bool) -> str:
    return "1" if value else "0"


def selection_assignments(
    profile: str, chip_available: str | None
) -> dict[str, str]:
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
    return values


def normalized_assignments(args: argparse.Namespace) -> dict[str, str]:
    """The master toggle and wake legs the shell read raw, normalized.

    Mode and legs come from the pass's environment, which the shell has
    already layered mode file over jasper.env; an empty string is its "unset",
    so the build default applies. The chip-ref observe opt-in is mode-file
    scoped instead, and is read straight from the file.
    """

    values = {SHELL_VARS[AEC_MODE_ENV]: normalize_aec_mode(args.mode)}
    for dest, key, default in _WAKE_LEGS:
        values[SHELL_VARS[key]] = _shell_bool(
            _leg_enabled(getattr(args, dest) or None, default)
        )
    observe = parse_env_file(args.mode_file).get(CHIP_REF_OBSERVE_ENV)
    values[SHELL_VARS[CHIP_REF_OBSERVE_ENV]] = _shell_bool(
        _leg_enabled(observe, False)
    )
    return values


def inferred_assignment(mode_file: str) -> dict[str, str]:
    """The closest profile for a mode file written before profiles existed."""

    values = parse_env_file(mode_file)
    legs = {
        dest: _leg_enabled(values.get(key), default)
        for dest, key, default in _WAKE_LEGS
    }
    intent = AecIntent(
        mode=values.get(AEC_MODE_ENV, ""),
        raw_enabled=legs["leg_raw"],
        dtln_enabled=legs["leg_dtln"],
        chip_aec_enabled=legs["leg_chip_aec"],
        chip_aec_150_enabled=legs["leg_chip_aec_150"],
        chip_aec_210_enabled=legs["leg_chip_aec_210"],
    )
    return {"AUDIO_INPUT_PROFILE": infer_audio_input_profile(intent)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="")
    parser.add_argument("--chip-available", choices=("0", "1"))
    parser.add_argument("--infer", action="store_true")
    parser.add_argument("--normalize", action="store_true")
    parser.add_argument("--mode", default="")
    parser.add_argument("--mode-file", default="")
    for dest, _key, _default in _WAKE_LEGS:
        parser.add_argument("--" + dest.replace("_", "-"), default="")
    args = parser.parse_args(argv)

    if args.infer:
        values = inferred_assignment(args.mode_file)
    else:
        values = normalized_assignments(args) if args.normalize else {}
        # A resolved profile owns its whole vector, so it lands last.
        values.update(selection_assignments(args.profile, args.chip_available))
    print(_quoted(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
